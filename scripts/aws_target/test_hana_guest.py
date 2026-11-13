"""Test Hana guest safety without a controller, systemd manager, or AWS calls."""
from __future__ import annotations

from dataclasses import asdict, replace
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import override
import unittest
from unittest import mock

from scripts.aws_target import hana_guest as guest
from scripts.lib import conf
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


ROOT = Path(__file__).resolve().parents[2]


class HanaGuestTests(unittest.TestCase):
    def __init__(self, methodName: str = 'runTest') -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.config_path: Path = ROOT / 'config/hana_aws.conf'
        self.settings: guest.SafetySettings = guest.read_settings(conf.read(self.config_path))
        self.now: float = 1000.0
        self.generation: str = 'fixture_generation'
        self.boot_id: str = 'fixture-boot'
        self.permit: Path = Path()
        self.monotonic: float = 0.0
        self.paths: guest.GuestPaths = guest.GuestPaths(Path(), Path())

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config_path = self.root / 'hana_aws.conf'
        _ = self.config_path.write_text((ROOT / 'config/hana_aws.conf').read_text())
        self.settings = guest.read_settings(conf.read(self.config_path))
        self.paths = guest.GuestPaths(self.root / 'state', self.root / 'boot-id')
        self.paths.directory.mkdir()
        _ = self.paths.generation.write_text(self.generation)
        _ = self.paths.boot_id.write_text(self.boot_id)
        self.permit = self.paths.permit

    def clock(self) -> float:
        return self.now

    def uptime(self) -> float:
        return self.monotonic

    def observe(self, permit: guest.Permit) -> guest.BoundPermit:
        return guest.observe_permit(self.settings, permit, guest.FirstPermitRead(),
            self.generation, self.boot_id, clock=self.clock, uptime=self.uptime)

    def watchdog(self, permit: guest.Permit) -> guest.WatchdogAction:
        return guest.watchdog_action(self.settings, self.observe(permit), clock=self.clock, uptime=self.uptime)

    def fresh(self) -> guest.WorkPermit:
        return guest.WorkPermit(self.now + self.settings.guard_grace_seconds, self.generation, self.boot_id)

    def files(self) -> dict[str, guest.GuestFile]:
        return {file.path.name: file for file in guest.render_files(
            self.settings, device=Path('/dev/disk/by-label/hana-data'), tool_setup=Path(sys.executable))}

    def guard(self, permit: guest.Permit, elapsed: float) -> guest.GuardAction:
        return guest.guard_action(self.settings, self.observe(permit),
                                  boot_elapsed=elapsed, clock=self.clock, uptime=self.uptime)

    def test_guard_releases_only_a_fresh_permit(self) -> None:
        _ = self.permit.write_text(json.dumps(asdict(self.fresh())))
        self.assertEqual(self.guard(guest.read_permit(self.permit), 0), guest.GuardAction.RELEASE)
        self.assertEqual(self.guard(self.fresh(), self.settings.guard_grace_seconds), guest.GuardAction.RELEASE)

    def test_guard_bounds_grace_for_expired_missing_and_unreadable_permits(self) -> None:
        expired = replace(self.fresh(), expires_at=self.now)
        with mock.patch.object(Path, 'read_text', side_effect=PermissionError('private fixture')):
            unreadable = guest.read_permit(self.permit)
        for permit in (expired, guest.read_permit(self.permit), unreadable):
            with self.subTest(permit=permit):
                self.assertEqual(self.guard(permit, 0), guest.GuardAction.WAIT)
                self.assertEqual(self.guard(permit, self.settings.guard_grace_seconds), guest.GuardAction.STOP)

    def test_permit_decoder_rejects_invalid_fields(self) -> None:
        for field, invalid in (('expires_at', True), ('expires_at', float('nan')),
                               ('expires_at', float('inf')), ('generation', ''),
                               ('generation', '../other'), ('boot_id', ''), ('expires_at', 'later')):
            with self.subTest(field=field, value=invalid):
                _ = self.permit.write_text(json.dumps({**asdict(self.fresh()), field: invalid}))
                self.assertIsInstance(guest.read_permit(self.permit), guest.NoWorkPermit)
        for text in ('{', '[]', '{}'):
            _ = self.permit.write_text(text)
            self.assertIsInstance(guest.read_permit(self.permit), guest.NoWorkPermit)

    def test_prior_boot_and_generation_cannot_authorize_work(self) -> None:
        for permit in (replace(self.fresh(), boot_id='prior-boot'),
                       replace(self.fresh(), generation='prior_generation')):
            self.assertEqual(self.guard(permit, self.settings.guard_grace_seconds), guest.GuardAction.STOP)
            self.assertEqual(self.watchdog(permit), guest.WatchdogAction.STOP)

    def test_watchdog_stops_at_expiry_with_or_without_a_live_controller(self) -> None:
        permit = self.fresh()
        for controller_alive in (True, False):
            with self.subTest(controller_alive=controller_alive):
                controller = self.root / 'controller'
                if controller_alive:
                    controller.touch()
                else:
                    controller.unlink(missing_ok=True)
                self.now = permit.expires_at - self.settings.watchdog_check_seconds
                self.assertEqual(self.watchdog(permit), guest.WatchdogAction.KEEP)
                self.now = permit.expires_at
                self.assertEqual(self.watchdog(permit), guest.WatchdogAction.STOP)

    def simulate(self, mode: str, permit: guest.Permit) -> list[list[str]]:
        commands: list[list[str]] = []
        self.paths.fence.unlink(missing_ok=True)
        self.paths.deadline.unlink(missing_ok=True)
        self.monotonic = 0
        if isinstance(permit, guest.WorkPermit):
            guest.write_permit(self.settings, permit, paths=self.paths, clock=self.clock)
        else:
            self.permit.unlink(missing_ok=True)

        def pause(seconds: float) -> None:
            self.assertGreater(seconds, 0)
            self.now += seconds
            self.monotonic += seconds

        guest.run_safety(mode, self.settings, clock=self.clock, paths=self.paths,
                         uptime=self.uptime, pause=pause, run=commands.append)
        return commands

    def test_watchdog_kills_a_command_that_never_exits_before_poweroff(self) -> None:
        permit = self.fresh()
        commands = self.simulate('watchdog', permit)
        self.assertEqual(self.now, permit.expires_at)
        self.assertEqual(commands, [
            ['systemctl', 'kill', '--kill-whom=all', '--signal=KILL', guest.WORK_SLICE],
            ['systemctl', 'poweroff', '--no-block']])

    def test_guard_stops_despite_failed_tool_setup_or_missing_data_disk(self) -> None:
        for failure in ('failed tool setup', 'missing data disk'):
            with self.subTest(failure=failure):
                self.now = 1000
                commands = self.simulate('guard', guest.NoWorkPermit())
                self.assertEqual(self.now, 1000 + self.settings.guard_grace_seconds)
                self.assertEqual(commands[-1], ['systemctl', 'poweroff', '--no-block'])
        units = self.files()
        self.assertNotIn('hana-tools', units['hana-guard.service'].content)
        self.assertNotIn('hana-tools', units['hana-watchdog.service'].content)
        self.assertNotIn('var-lib-hana.mount', units['hana-guard.service'].content)

    def test_kill_failure_still_requests_poweroff(self) -> None:
        commands: list[list[str]] = []

        def run(command: list[str]) -> None:
            commands.append(command)
            if command[1] == 'kill':
                raise OSError('The command manager is unavailable')

        with self.assertRaises(OSError):
            guest.stop_work(run)
        self.assertEqual(commands[-1], ['systemctl', 'poweroff', '--no-block'])

    def test_command_uses_its_own_slice_and_configured_resource_limits(self) -> None:
        settings = replace(self.settings, enabled=True)
        guest.write_permit(settings, self.fresh(), paths=self.paths, clock=self.clock)
        arguments = guest.admit_command(settings, ('cargo', 'check'), guest.DATA / 'slot/source',
            mounted=True, clock=self.clock, uptime=self.uptime, paths=self.paths, start=lambda argv: argv)
        self.assertIn('--slice=hana-work-' + self.generation + '.slice', arguments)
        self.assertIn('--uid=hana-runner', arguments)
        self.assertIn('--property=CPUQuota=' + str(settings.step_cpu_limit * 100) + '%', arguments)
        self.assertIn('--property=MemoryMax=' + str(settings.step_memory_gib * 1024**3), arguments)
        self.assertEqual(arguments[-3:], ['--', 'cargo', 'check'])
        parent = self.files()[guest.WORK_SLICE].content
        self.assertIn('CPUQuota=' + str(settings.step_cpu_limit * 100) + '%', parent)
        self.assertIn('MemoryMax=' + str(settings.step_memory_gib * 1024**3), parent)

    def test_command_refuses_missing_disk_disabled_mode_and_stale_permission(self) -> None:
        enabled = replace(self.settings, enabled=True)
        for settings, mounted, permit in ((enabled, False, self.fresh()),
                                          (replace(enabled, enabled=False), True, self.fresh()),
                                          (enabled, True, replace(self.fresh(), expires_at=self.now))):
            _ = self.permit.write_text(json.dumps(asdict(permit)))
            with self.assertRaises(ValueError):
                _ = guest.admit_command(settings, ('cargo', 'check'), guest.DATA / 'slot/source',
                    mounted=mounted, clock=self.clock, uptime=self.uptime, paths=self.paths, start=lambda argv: argv)

    def test_units_keep_guard_independent_and_bound_device_and_mount_waits(self) -> None:
        files = self.files()
        guard = files['hana-guard.service'].content
        self.assertNotIn('After=', guard)
        self.assertNotIn('Requires=', guard)
        self.assertNotIn('network', guard)
        self.assertNotIn('local-fs', guard)
        self.assertIn('After=hana-guard.service var-lib-hana.mount', files['hana-tools.service'].content)
        mount = files['var-lib-hana.mount'].content
        self.assertIn(f'JobTimeoutSec={self.settings.mount_timeout_seconds}s', mount)
        self.assertIn(f'TimeoutSec={self.settings.mount_timeout_seconds}s', mount)
        self.assertIn('Options=nofail', mount)
        self.assertIn('ConditionPathIsMountPoint=' + str(guest.DATA), files['hana-tools.service'].content)
        self.assertEqual(files['safety.py'].mode, 0o600)
        self.assertEqual(files['safety.json'].mode, 0o600)

    def test_rendered_program_runs_without_repository_imports(self) -> None:
        control = self.root / 'control'
        control.mkdir()
        settings = control / 'safety.json'
        _ = settings.write_text(json.dumps(asdict(self.settings)))
        program = self.root / 'safety.py'
        _ = program.write_text(guest.guest_program().replace(str(guest.CONTROL), str(control))
                               .replace(str(guest.PERMIT), str(self.permit)))
        binary = self.root / 'bin'
        binary.mkdir()
        systemctl = binary / 'systemctl'
        _ = systemctl.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_SYSTEMCTL"\n')
        systemctl.chmod(0o700)
        events = self.root / 'events'
        environment = {**os.environ, 'PATH': str(binary), 'TEST_SYSTEMCTL': str(events)}
        result = subprocess.run([sys.executable, '-I', str(program), 'watchdog'], env=environment,
                                text=True, capture_output=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(events.read_text().splitlines(),
                         ['kill --kill-whom=all --signal=KILL ' + guest.WORK_SLICE, 'poweroff --no-block'])

    def test_systemd_verifies_rendered_units(self) -> None:
        analyzer = shutil.which('systemd-analyze')
        if analyzer is None:
            self.skipTest('systemd-analyze is absent')
        units: list[str] = []
        for name, file in self.files().items():
            if file.path.parent == Path('/etc/systemd/system'):
                path = self.root / name
                _ = path.write_text(file.content)
                units.append(str(path))
        result = subprocess.run([analyzer, 'verify', '--man=no', *units],
                                text=True, capture_output=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_config_values_drive_the_rendered_settings(self) -> None:
        files = self.files()
        self.assertEqual(json.loads(files['safety.json'].content), asdict(self.settings))
        changed = self.config_path.read_text().replace(
            f'watchdog_check_seconds={self.settings.watchdog_check_seconds}',
            f'watchdog_check_seconds={self.settings.watchdog_check_seconds + 1}')
        _ = self.config_path.write_text(changed)
        settings = guest.read_settings(conf.read(self.config_path))
        self.assertEqual(settings.watchdog_check_seconds, self.settings.watchdog_check_seconds + 1)

    def test_far_future_expiry_grants_no_permission(self) -> None:
        permit = replace(self.fresh(), expires_at=self.now + self.settings.permit_max_seconds
                         + self.settings.watchdog_check_seconds)
        self.assertEqual(self.guard(permit, 0), guest.GuardAction.WAIT)
        self.assertEqual(self.guard(permit, self.settings.guard_grace_seconds), guest.GuardAction.STOP)
        self.assertEqual(self.watchdog(permit), guest.WatchdogAction.STOP)
        with self.assertRaises(ValueError):
            guest.write_permit(self.settings, permit, paths=self.paths, clock=self.clock)
        self.assertFalse(self.permit.exists())

    def test_backward_clock_stops_at_persisted_monotonic_deadline(self) -> None:
        permit = self.fresh()
        guest.write_permit(self.settings, permit, paths=self.paths, clock=self.clock)
        with guest.guest_lock(self.paths):
            first = guest.bound_permit(self.settings, self.paths, clock=self.clock, uptime=self.uptime)
        self.assertIsInstance(first, guest.PermitDeadline)
        assert isinstance(first, guest.PermitDeadline)
        self.assertLessEqual(first.monotonic_deadline - self.monotonic, self.settings.permit_max_seconds)
        # A restarted reader must retain the first reader's deadline after a clock change.
        self.now -= self.settings.watchdog_check_seconds
        commands: list[list[str]] = []

        def pause(seconds: float) -> None:
            self.now += seconds
            self.monotonic += seconds

        guest.run_safety('watchdog', self.settings, clock=self.clock, uptime=self.uptime,
                         pause=pause, run=commands.append, paths=self.paths)
        self.assertEqual(self.monotonic, first.monotonic_deadline)
        self.assertLess(self.now, permit.expires_at)
        self.assertEqual(commands[-1], ['systemctl', 'poweroff', '--no-block'])

    def test_renewal_before_the_read_keeps_work_alive_until_the_new_deadline(self) -> None:
        permit = self.fresh()
        guest.write_permit(self.settings, permit, paths=self.paths, clock=self.clock)
        renewed = False
        commands: list[list[str]] = []
        next_expiry = permit.expires_at + self.settings.guard_grace_seconds

        def pause(seconds: float) -> None:
            nonlocal renewed
            self.now += seconds
            self.monotonic += seconds
            if not renewed and self.now >= permit.expires_at:
                self.assertFalse(self.paths.fence.exists())
                guest.write_permit(self.settings, replace(permit, expires_at=next_expiry),
                                   paths=self.paths, clock=self.clock)
                renewed = True

        guest.run_safety('watchdog', self.settings, clock=self.clock, uptime=self.uptime,
                         pause=pause, run=commands.append, paths=self.paths)
        self.assertTrue(renewed)
        self.assertEqual(self.now, next_expiry)
        self.assertEqual(len(commands), 2)

    def test_shutdown_fence_precedes_kill_and_refuses_renewal(self) -> None:
        commands: list[list[str]] = []

        def run(command: list[str]) -> None:
            self.assertTrue(self.paths.fence.exists())
            with self.assertRaisesRegex(ValueError, 'Shutdown is fenced'):
                guest.write_permit(self.settings, self.fresh(), paths=self.paths, clock=self.clock)
            commands.append(command)

        guest.run_safety('watchdog', self.settings, clock=self.clock, uptime=self.uptime,
                         run=run, paths=self.paths)
        self.assertEqual(commands[0][1], 'kill')
        self.assertFalse(self.permit.exists())

    def test_admission_after_shutdown_fence_refuses_a_fresh_permit(self) -> None:
        settings = replace(self.settings, enabled=True)
        guest.write_permit(settings, self.fresh(), paths=self.paths, clock=self.clock)
        with guest.guest_lock(self.paths):
            guest.atomic_record(self.paths.fence, {'state': 'shutdown'})
        started: list[list[str]] = []
        with self.assertRaisesRegex(ValueError, 'Shutdown is fenced'):
            guest.admit_command(settings, ('cargo', 'check'), guest.DATA / 'slot/source', mounted=True,
                                paths=self.paths, clock=self.clock, uptime=self.uptime, start=started.append)
        self.assertEqual(started, [])

    def test_permit_read_and_command_start_hold_the_guest_lock(self) -> None:
        settings = replace(self.settings, enabled=True)
        guest.write_permit(settings, self.fresh(), paths=self.paths, clock=self.clock)
        read_permit = guest.read_permit

        def assert_locked() -> None:
            with self.paths.lock.open('a') as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        def read(path: Path) -> guest.Permit:
            assert_locked()
            return read_permit(path)

        def start(arguments: list[str]) -> list[str]:
            assert_locked()
            return arguments

        with mock.patch.object(guest, 'read_permit', side_effect=read):
            _ = guest.admit_command(settings, ('cargo', 'check'), guest.DATA / 'slot/source', mounted=True,
                                   paths=self.paths, clock=self.clock, uptime=self.uptime, start=start)
            self.now = self.fresh().expires_at + self.settings.permit_max_seconds
            guest.run_safety('watchdog', settings, clock=self.clock, uptime=self.uptime,
                             run=lambda _command: None, paths=self.paths)
        self.assertTrue(self.paths.fence.exists())


if __name__ == '__main__':
    _ = unittest.main()
