"""Test offline GPU preparation with private files and fake build commands."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
from io import BytesIO, StringIO
import base64
from datetime import UTC, datetime
from decimal import Decimal
import tarfile
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import tomllib
from typing import BinaryIO, cast, final, override
import unittest
from unittest import mock

from scripts.aws_target import cloud_gate, gpu_size_trial as trial, ledger
from scripts.aws_target.aws_target import AwsCli, AwsUnavailable, largest_gate_vcpus
from scripts.aws_target.ledger_test_support import CostFixture
from scripts.lib import conf
from scripts.lint.memory_test_support import config_environment, write_config_fixture
from scripts.production import merge_gate
from scripts.production.gate_modules import GateRefused


HARDWARE = 'deviceType = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU\ndeviceName = Fixture GPU\n'
SOFTWARE = 'deviceType = PHYSICAL_DEVICE_TYPE_CPU\ndeviceName = llvmpipe\n'


def fake_tool_path(name: str, *, path: str | None = None) -> str:
    _ = path
    return name


class LogReadProbe:
    """Record bounded reads from an in-memory log."""
    def __init__(self, payload: bytes) -> None:
        self.content: BytesIO = BytesIO(payload)
        self.read_sizes: list[int] = []
        self.offsets: list[int] = []

    def __enter__(self) -> LogReadProbe:
        return self

    def __exit__(self, _kind: object, _value: object, _traceback: object) -> None:
        self.content.close()

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        return self.content.seek(offset, whence)

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        self.offsets.append(self.content.tell())
        return self.content.read(size)


@final
class GpuTrialTests(unittest.TestCase):
    root: Path = Path()
    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {**config_environment(self.root / 'config'),
                'HOME': str(self.root), 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'})))

    def test_limits_follow_live_configuration(self) -> None:
        before = trial.settings()
        self.assertEqual(before.limit_usd, conf.decimal(conf.read('cloud_gate').text('gpu_size_trial_limit_usd')))
        config = conf.path('cloud_gate')
        _ = write_config_fixture(config, 'gpu_size_trial_limit_usd=4\ngpu_size_trial_lifetime_minutes=25\n'
                                  + 'gpu_size_trial_box_extra_usd=0.07\n'
                                  + 'gpu_size_trial_spot_max_usd_per_hour={"g6.2xlarge":0.73}')
        after = trial.settings()
        source = conf.read('cloud_gate')
        self.assertEqual(after.lifetime_minutes, source.whole('gpu_size_trial_lifetime_minutes'))
        self.assertEqual(after.limit_usd, conf.decimal(source.text('gpu_size_trial_limit_usd')))
        self.assertEqual(after.box_extra_usd, conf.decimal(source.text('gpu_size_trial_box_extra_usd')))
        self.assertEqual(list(after.spot_max_usd_per_hour), list(trial.mapping(cast(object, json.loads(source.text('gpu_size_trial_spot_max_usd_per_hour'))))))
        self.assertNotEqual(before, after)

    def test_price_map_refuses_bad_values_and_types(self) -> None:
        for value in ('{}', '{"g6.xlarge":0}', '{"g6.xlarge":11}', '{"g6.xlarge":"NaN"}',
                      '{"m7a.8xlarge":0.5}', '[]'):
            with self.subTest(value=value):
                _ = write_config_fixture(conf.path('cloud_gate'),
                                         'gpu_size_trial_spot_max_usd_per_hour=' + value)
                with self.assertRaises(ValueError):
                    _ = trial.settings()

    def test_hardware_check_requires_at_least_one_hardware_adapter(self) -> None:
        cpu = {'type': 'PHYSICAL_DEVICE_TYPE_CPU', 'name': 'llvmpipe'}
        for kind in ('DISCRETE', 'INTEGRATED', 'VIRTUAL'):
            gpu = {'type': f'PHYSICAL_DEVICE_TYPE_{kind}_GPU', 'name': 'Fixture GPU'}
            payload = HARDWARE.replace('DISCRETE', kind)
            for text, adapters in ((payload, (gpu,)), (payload + SOFTWARE, (gpu, cpu)),
                                   (SOFTWARE + payload, (cpu, gpu))):
                with self.subTest(kind=kind, payload=text):
                    self.assertEqual(trial.hardware_vulkan(text), adapters)
        for payload in (SOFTWARE, '', 'deviceType = unknown'):
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, 'at least one hardware Vulkan adapter'):
                _ = trial.hardware_vulkan(payload)

    def test_generated_guest_executes_both_runs_and_publishes_results(self) -> None:
        folder = self.root / 'guest'
        folder.mkdir()
        source = self.root / 'guest-source'
        (source / '.config').mkdir(parents=True)
        _ = (source / '.config/nextest.toml').write_text('[profile.ci]\n')
        with tarfile.open(folder / 'source.tar', 'w') as archive:
            archive.add(source / '.config', arcname='.config')
        downloaded_archive = self.root / 'downloaded-archive'
        _ = downloaded_archive.write_bytes(b'fixture archive')
        trial.write_json(folder / 'request.json', {'commit': 'a' * 40})
        trial.write_json(folder / 'manifest.json', {'nextest': 'cargo-nextest fixture', 'sha256': {
            'source.tar': trial.digest(folder / 'source.tar'),
            'hana-tests.tar.zst': trial.digest(downloaded_archive)}})
        trial.write_json(folder / 'setup.json', {'boot': 2, 'import_start': 3, 'import_end': 4, 'reboot': 5})
        uploaded = self.root / 'uploaded'
        uploaded.mkdir()
        trial.write_json(folder / 'links.json', {'started': '1', 'archive': str(downloaded_archive),
            'instance_type': 'g6.xlarge', 'result-1': str(uploaded / 'result-1.tar.gz'),
            'result-2': str(uploaded / 'result-2.tar.gz'), 'finished': str(uploaded / 'finished.json')})
        tools = self.root / 'guest-tools'
        tools.mkdir()
        fake = '''import json, os, shutil, sys, tomllib
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
if name == 'vulkaninfo':
    print('deviceType = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU\\ndeviceName = Fixture GPU\\n'
          'deviceType = PHYSICAL_DEVICE_TYPE_CPU\\ndeviceName = llvmpipe')
elif name == 'curl':
    if '-T' in args:
        shutil.copyfile(args[args.index('-T') + 1], args[-1])
    else:
        index = args.index('-o')
        shutil.copyfile(args[index - 1], args[index + 1])
elif name == 'cargo':
    if '--version' in args:
        print('cargo-nextest fixture')
    else:
        assert args[:2] == ['nextest', 'run'], args
        assert Path(args[args.index('--extract-to') + 1]).is_dir(), 'The extraction directory must exist'
        config = Path(args[args.index('--config-file') + 1])
        junit = Path(tomllib.loads(config.read_text())['profile']['ci']['junit']['path'])
        junit.write_text('<testsuites/>')
        samples = Path(os.environ['GPU_TRIAL_SAMPLES'])
        samples.mkdir()
        (samples / 'test.json').write_text(json.dumps({'binary': 'fixture', 'test': 'test',
            'status': 0, 'wall_s': 1, 'largest_process_peak_rss_kib': 1024}))
'''
        for name in ('cargo', 'vulkaninfo', 'curl'):
            executable = tools / name
            _ = executable.write_text('#!' + sys.executable + '\n' + fake)
            executable.chmod(0o700)
        guest = folder / 'guest.py'
        _ = guest.write_text(trial.guest_program().replace(
            "root = Path('/var/lib/gpu-size-trial')", 'root = Path(' + repr(str(folder)) + ')', 1))
        result = subprocess.run([sys.executable, str(guest)], capture_output=True, text=True, timeout=10,
            env={**os.environ, 'PATH': str(tools) + os.pathsep + os.environ.get('PATH', ''),
                 'GPU_TRIAL_CARGO': str(tools / 'cargo')})
        self.assertEqual(result.returncode, 0, result.stderr)
        finished = trial.read_json(uploaded / 'finished.json')
        self.assertEqual(finished['status'], 'complete', finished)
        for number in (1, 2):
            with tarfile.open(uploaded / f'result-{number}.tar.gz') as bundle:
                names = bundle.getnames()
                machine = bundle.extractfile('machines/g6.xlarge/machine.json')
                assert machine is not None
                with machine:
                    record = trial.mapping(cast(object, json.load(machine)))
                self.assertEqual(record['adapters'], [
                    {'type': 'PHYSICAL_DEVICE_TYPE_DISCRETE_GPU', 'name': 'Fixture GPU'},
                    {'type': 'PHYSICAL_DEVICE_TYPE_CPU', 'name': 'llvmpipe'}])
            self.assertIn('machines/g6.xlarge/vulkan.txt', names)
            self.assertIn(f'machines/g6.xlarge/{number}/samples/test.json', names)
            self.assertIn(f'machines/g6.xlarge/{number}/junit.xml', names)

    def test_environment_removes_software_overrides(self) -> None:
        with mock.patch.dict(os.environ, {'VK_DRIVER_FILES': 'lavapipe.json', 'LIBGL_ALWAYS_SOFTWARE': '1',
                                         'CARGO_ENCODED_RUSTFLAGS': '-Ctarget-cpu=native'}):
            environment = trial.trial_environment()
        self.assertNotIn('VK_DRIVER_FILES', environment)
        self.assertNotIn('LIBGL_ALWAYS_SOFTWARE', environment)
        self.assertNotIn('CARGO_ENCODED_RUSTFLAGS', environment)
        self.assertEqual(environment['WGPU_BACKEND'], 'vulkan')
        self.assertNotIn('native', environment['RUSTFLAGS'])

    def test_wrapper_keeps_exit_status_and_kernel_rss(self) -> None:
        script = self.root / 'test.py'
        _ = script.write_text('import sys\ndata = bytearray(24 * 1024 * 1024)\nsys.exit(int(sys.argv[1]))\n')
        for status in (0, 7):
            folder = self.root / str(status)
            result = subprocess.run([sys.executable, str(trial.WRAPPER), sys.executable, str(script), str(status)],
                                    env={**os.environ, 'GPU_TRIAL_SAMPLES': str(folder), 'NEXTEST_BINARY_ID': 'fixture'},
                                    check=False, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, status, result.stderr)
            row = trial.read_json(next(folder.glob('*.json')))
            self.assertEqual(row['status'], status)
            self.assertGreater(float(cast(float, row['largest_process_peak_rss_kib'])), 24 * 1024)
            self.assertGreater(float(cast(float, row['wall_s'])), 0)

    def test_wrapper_preserves_signal_status(self) -> None:
        script = self.root / 'signal_child.py'
        _ = script.write_text('import os, signal\nos.kill(os.getpid(), signal.SIGTERM)\n')
        result = subprocess.run([sys.executable, str(trial.WRAPPER), sys.executable, str(script)],
                                env={**os.environ, 'GPU_TRIAL_SAMPLES': str(self.root / 'samples'),
                                     'NEXTEST_BINARY_ID': 'fixture'}, check=False, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, -signal.SIGTERM, result.stderr)
        row = trial.read_json(next((self.root / 'samples').glob('*.json')))
        self.assertEqual(row['status'], -signal.SIGTERM)

    def test_wrapper_does_not_measure_listing(self) -> None:
        script = self.root / 'list_child.py'
        _ = script.write_text('print("fixture: test")\n')
        folder = self.root / 'listing'
        result = subprocess.run([sys.executable, str(trial.WRAPPER), sys.executable, str(script), '--list'],
                                env={**os.environ, 'GPU_TRIAL_SAMPLES': str(folder)},
                                check=False, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b'fixture: test\n')
        self.assertFalse(folder.exists())

    def test_comparison_rules(self) -> None:
        good = trial.TestSample(0, 1.2, 100)
        bad = trial.TestSample(1, 1.4, 110)
        same = trial.MachineTestPair(good, good)
        different = trial.MachineTestPair(bad, bad)
        flaky = trial.MachineTestPair(good, bad)
        incomplete = trial.MachineTestPair(good, trial.IncompleteTest('The test did not finish'))
        for candidate, verdict in ((same, 'same'), (different, 'difference'),
                                   (flaky, 'flaky'), (incomplete, 'incomplete')):
            with self.subTest(verdict=verdict):
                self.assertEqual(trial.comparison(same, candidate), verdict)
                self.assertEqual(trial.comparison(flaky, candidate), 'inconclusive')
        self.assertEqual(trial.comparison(incomplete, same), 'incomplete')

    def test_new_prepare_removes_previous_folders_without_following_links(self) -> None:
        root = trial.trial_root()
        root.mkdir(parents=True)
        outside = self.root / 'outside'
        outside.mkdir()
        _ = (outside / 'keep').write_text('fixture outside data')
        for name in ('prepared', 'failed', 'abandoned'):
            previous = root / name
            previous.mkdir()
            _ = (previous / 'closure.export').write_bytes(b'fixture old closure')
            (previous / 'profile').symlink_to(outside, target_is_directory=True)
        first = root / 'first'
        with trial.new_trial(first):
            self.assertEqual([path.name for path in root.iterdir() if path.is_dir() and not path.name.startswith('.')], ['first'])
            _ = (first / 'manifest.json').write_text('fixture prepared')
        second = root / 'second'
        with trial.new_trial(second):
            self.assertFalse(first.exists())
            self.assertTrue(second.is_dir())
        self.assertEqual((outside / 'keep').read_text(), 'fixture outside data')

    def test_new_prepare_refuses_to_stack_while_a_folder_is_held(self) -> None:
        root = trial.trial_root()
        current = root / 'held'
        with trial.new_trial(current):
            _ = (current / 'closure.export').write_bytes(b'fixture held closure')
            abandoned = root / 'abandoned'
            abandoned.mkdir()
            _ = (abandoned / 'source.tar').write_bytes(b'fixture old source')
            with self.assertRaisesRegex(ValueError, 'held by a live run'):
                with trial.new_trial(root / 'next'):
                    self.fail('A second preparation must not start')
            self.assertEqual((current / 'closure.export').read_bytes(), b'fixture held closure')
            self.assertFalse(abandoned.exists())
            self.assertFalse((root / 'next').exists())
        with trial.new_trial(root / 'next'):
            self.assertFalse(current.exists())

    def test_invalid_host_folders_never_prune_or_remove_inputs(self) -> None:
        root = trial.trial_root()
        current = root / 'current'
        current.mkdir(parents=True)
        outside = self.root / 'outside'
        outside.mkdir()
        nested = root / 'group/trial'
        nested.mkdir(parents=True)
        alias = root / 'alias'
        alias.symlink_to(current, target_is_directory=True)
        outside_alias = root / 'outside-alias'
        outside_alias.symlink_to(outside, target_is_directory=True)
        parent_alias = self.root / 'root-alias'
        parent_alias.symlink_to(root, target_is_directory=True)
        for folder in (current, outside, nested):
            _ = (folder / 'closure.export').write_text('fixture input')
        before = set(root.iterdir())
        for folder in (nested, outside, alias, outside_alias, parent_alias / 'current', root / '.locks'):
            with self.subTest(folder=folder):
                with self.assertRaisesRegex(ValueError, 'canonical direct child'):
                    with trial.new_trial(folder):
                        self.fail('An invalid preparation must not start')
                with self.assertRaisesRegex(ValueError, 'canonical direct child'):
                    with trial.held_trial(folder):
                        self.fail('An invalid folder must not acquire a lock')
                with self.assertRaisesRegex(ValueError, 'canonical direct child'):
                    trial.remove_run_inputs(folder, 1)
                for command in ('prepare', 'compare'):
                    with redirect_stderr(StringIO()):
                        self.assertEqual(trial.main([command, '--trial-folder', str(folder)]), 1)
                self.assertEqual(set(root.iterdir()), before)
                for saved in (current, outside, nested):
                    self.assertEqual((saved / 'closure.export').read_text(), 'fixture input')
                    self.assertFalse((saved / 'inputs-removed.json').exists())
        with self.assertRaisesRegex(ValueError, 'canonical direct child'):
            with trial.new_trial(root / 'next'):
                self.fail('A linked prior folder must prevent pruning')
        self.assertEqual(set(root.iterdir()), before | {root / 'controller.lock'})
        self.assertTrue((current / 'closure.export').is_file())
        self.assertTrue((nested / 'closure.export').is_file())
        self.assertTrue(alias.is_symlink())
        self.assertTrue(outside_alias.is_symlink())

    def test_measurement_removes_large_workspaces_on_every_exit(self) -> None:
        source = self.root / 'measurement-source'
        (source / '.config').mkdir(parents=True)
        _ = (source / '.config/nextest.toml').write_text('[profile.ci]\n')
        for status in (0, 100, 96, 'error'):
            with self.subTest(status=status):
                folder = trial.trial_root() / ('measurement-' + str(status))
                folder.mkdir(parents=True)
                trial.write_json(folder / 'request.json', {'commit': 'fixture-commit'})
                _ = (folder / 'hana-tests.tar.zst').write_bytes(b'fixture archive')
                calls: list[Path] = []
                def execute(argv: Sequence[str], **options: object) -> subprocess.CompletedProcess[bytes]:
                    run = Path(argv[argv.index('--extract-to') + 1]).parent
                    for previous in calls:
                        self.assertFalse((previous / 'archive').exists())
                        self.assertFalse((previous / 'source').exists())
                    calls.append(run)
                    _ = (run / 'archive/binary').write_bytes(b'fixture extracted binary')
                    (run / 'cargo-target').mkdir()
                    _ = (run / 'cargo-target/output').write_bytes(b'fixture build output')
                    _ = (run / 'junit.xml').write_text('<testsuites/>')
                    (run / 'samples').mkdir()
                    trial.write_json(run / 'samples/test.json', {'fixture': True})
                    _ = cast(BinaryIO, options['stdout']).write(b'fixture nextest log\n')
                    if status == 'error':
                        raise OSError('fixture nextest process error')
                    return subprocess.CompletedProcess(argv, int(status), b'', b'')
                with (mock.patch.object(trial, 'checked', return_value=HARDWARE),
                      mock.patch.object(subprocess, 'run', side_effect=execute)):
                    if status == 'error':
                        with self.assertRaisesRegex(OSError, 'fixture nextest process error'):
                            trial.measure_machine(folder, 'natedev', source, {}, prefix=())
                    elif status == 96:
                        with self.assertRaisesRegex(ValueError, 'exit 96'):
                            trial.measure_machine(folder, 'natedev', source, {}, prefix=())
                    else:
                        trial.measure_machine(folder, 'natedev', source, {}, prefix=())
                self.assertEqual(len(calls), 2 if status in (0, 100) else 1)
                for run in calls:
                    for name in ('archive', 'source', 'cargo-target'):
                        self.assertFalse((run / name).exists())
                    for name in ('junit.xml', 'samples/test.json', 'nextest.log'):
                        self.assertTrue((run / name).is_file())

    def test_prepare_stops_before_build_when_hold_refuses(self) -> None:
        outside = self.root / 'outside'
        outside.mkdir()
        _ = (outside / 'keep').write_text('fixture')

        def export(_repo: Path, _commit: str, folder: Path) -> Path:
            source = folder / 'source'
            source.mkdir()
            _ = (source / 'artifact').write_text('fixture')
            _ = (folder / 'source.tar').write_bytes(b'fixture')
            for name in ('toolchain-profile', 'runtime-profile', 'runtime-profile-1-link', 'system-profile'):
                (folder / name).symlink_to(outside, target_is_directory=True)
            return source

        with (mock.patch.object(trial, 'checked', return_value='a' * 40) as command,
              mock.patch.object(trial, 'export_source', side_effect=export),
              mock.patch.object(trial, 'preflight', return_value={}),
              mock.patch.object(merge_gate.BuildHoldWait, 'wait_until_free', side_effect=GateRefused('held')),
              mock.patch.object(merge_gate, 'gate_reservation') as admission):
            with self.assertRaisesRegex(GateRefused, 'held'):
                trial.prepare(self.root, 'HEAD', trial.trial_root() / 'trial')
        admission.assert_not_called()
        self.assertEqual(command.call_count, 1)
        self.assertEqual(command.call_args.args[0][0], 'git')
        self.assertFalse((trial.trial_root() / 'trial/closure.export').exists())
        self.assertEqual(list((trial.trial_root() / 'trial').iterdir()), [trial.trial_root() / 'trial/prepare.log'])
        self.assertIn('held', (trial.trial_root() / 'trial/prepare.log').read_text())
        self.assertTrue((outside / 'keep').exists())

    def test_prepare_checks_hold_again_after_admission(self) -> None:
        with (mock.patch.object(trial, 'checked', return_value='a' * 40),
              mock.patch.object(trial, 'export_source'),
              mock.patch.object(trial, 'preflight', return_value={}),
              mock.patch.object(merge_gate.BuildHoldWait, 'wait_until_free', side_effect=[None, GateRefused('held again')]),
              mock.patch.object(merge_gate, 'gate_reservation') as admission):
            with self.assertRaisesRegex(GateRefused, 'held again'):
                trial.prepare(self.root, 'HEAD', trial.trial_root() / 'trial')
        place = cast(mock.MagicMock, cast(mock.MagicMock, cast(mock.MagicMock, admission.return_value).__enter__).return_value)
        popen = cast(mock.MagicMock, place.popen)
        popen.assert_not_called()

    def test_prepare_uses_admitted_worker(self) -> None:
        folder = trial.trial_root() / 'trial'
        with (mock.patch.object(trial, 'checked', return_value='a' * 40),
              mock.patch.object(trial, 'export_source'),
              mock.patch.object(trial, 'preflight', return_value={'vulkaninfo': '/nix/store/fixture/bin/vulkaninfo'}),
              mock.patch.object(merge_gate.BuildHoldWait, 'wait_until_free'),
              mock.patch.object(merge_gate, 'gate_reservation') as admission,
              redirect_stdout(StringIO())):
            place = cast(mock.MagicMock, cast(mock.MagicMock, cast(mock.MagicMock, admission.return_value).__enter__).return_value)
            popen = cast(mock.MagicMock, place.popen)
            wait = cast(mock.MagicMock, cast(mock.MagicMock, popen.return_value).wait)
            wait.return_value = 0
            trial.prepare(self.root, 'HEAD', folder)
        admission.assert_called_once()
        assert popen.call_args is not None
        argv = cast(list[str], popen.call_args.args[0])
        self.assertIn('--prepare-worker', argv)
        self.assertEqual(argv[-1], str(folder))
        self.assertEqual(trial.read_json(folder / 'request.json')['commit'], 'a' * 40)
        self.assertEqual(trial.read_json(folder / 'request.json')['tools'],
                         {'vulkaninfo': '/nix/store/fixture/bin/vulkaninfo'})
        self.assertTrue((folder / 'prepare.log').exists())

    def test_prepare_worker_failure_keeps_only_log(self) -> None:
        folder = trial.trial_root() / 'trial'
        with (mock.patch.object(trial, 'checked', return_value='a' * 40),
              mock.patch.object(trial, 'export_source'),
              mock.patch.object(trial, 'preflight', return_value={}),
              mock.patch.object(merge_gate.BuildHoldWait, 'wait_until_free'),
              mock.patch.object(merge_gate, 'gate_reservation') as admission):
            place = cast(mock.MagicMock, cast(mock.MagicMock, cast(mock.MagicMock, admission.return_value).__enter__).return_value)
            popen = cast(mock.MagicMock, place.popen)
            wait = cast(mock.MagicMock, cast(mock.MagicMock, popen.return_value).wait)
            wait.return_value = 1
            with self.assertRaisesRegex(GateRefused, 'exit 1'):
                trial.prepare(self.root, 'HEAD', folder)
        self.assertEqual(list(folder.iterdir()), [folder / 'prepare.log'])
        self.assertIn('exit 1', (folder / 'prepare.log').read_text())

    def test_prepare_failure_retains_failed_log_tails_before_cleanup(self) -> None:
        folder = trial.trial_root() / 'trial'

        def fail() -> int:
            for machine, status in (('setup-error', 96), ('test-failure', 100), ('passed', 0), ('interrupted', None)):
                run = folder / 'machines' / machine / '1'
                run.mkdir(parents=True)
                if status is not None:
                    trial.write_json(run / 'run.json', {'status': status})
                content = ''.join(f'{machine} line {number}\n' for number in range(250))
                _ = (run / 'nextest.log').write_text(content)
            return 1

        with (mock.patch.object(trial, 'checked', return_value='a' * 40),
              mock.patch.object(trial, 'export_source'),
              mock.patch.object(trial, 'preflight', return_value={}),
              mock.patch.object(merge_gate.BuildHoldWait, 'wait_until_free'),
              mock.patch.object(merge_gate, 'gate_reservation') as admission):
            place = cast(mock.MagicMock, cast(mock.MagicMock, cast(mock.MagicMock, admission.return_value).__enter__).return_value)
            popen = cast(mock.MagicMock, place.popen)
            wait = cast(mock.MagicMock, cast(mock.MagicMock, popen.return_value).wait)
            wait.side_effect = fail
            with self.assertRaisesRegex(GateRefused, 'exit 1'):
                trial.prepare(self.root, 'HEAD', folder)
        self.assertEqual(list(folder.iterdir()), [folder / 'prepare.log'])
        diagnostic = (folder / 'prepare.log').read_text()
        self.assertNotIn('passed line', diagnostic)
        for machine in ('setup-error', 'test-failure', 'interrupted'):
            self.assertIn(f'machines/{machine}/1/nextest.log (last 200 lines)', diagnostic)
            lines = [line for line in diagnostic.splitlines() if line.startswith(machine + ' line ')]
            self.assertEqual(lines, [f'{machine} line {number}' for number in range(50, 250)])

    def test_failed_log_tail_accepts_short_logs_invalid_text_and_missing_status(self) -> None:
        run = self.root / 'machines/natedev/2'
        run.mkdir(parents=True)
        _ = (run / 'nextest.log').write_bytes(b'nextest setup error\ninvalid: \xff')
        _ = (run / 'run.json').write_text('invalid JSON')
        trial.append_failed_nextest_logs(self.root)
        diagnostic = (self.root / 'prepare.log').read_text()
        self.assertIn('machines/natedev/2/nextest.log', diagnostic)
        self.assertIn('nextest setup error\ninvalid: \ufffd', diagnostic)

    def test_nextest_tail_stops_at_the_byte_limit_on_a_large_log(self) -> None:
        payload = b'outside the byte limit\n' + b'x' * (3 * trial.NEXTEST_LOG_TAIL_BYTES) + b'\nfinal diagnostic\n'
        reader = LogReadProbe(payload)
        with mock.patch.object(Path, 'open', return_value=reader) as opened:
            tail = trial.nextest_log_tail(self.root / 'nextest.log')
        opened.assert_called_once_with('rb')
        sizes = reader.read_sizes
        self.assertEqual(sum(sizes), trial.NEXTEST_LOG_TAIL_BYTES)
        self.assertTrue(all(0 < size <= trial.NEXTEST_LOG_TAIL_CHUNK_BYTES for size in sizes))
        offsets = reader.offsets
        self.assertEqual(min(offsets), len(payload) - trial.NEXTEST_LOG_TAIL_BYTES)
        self.assertEqual(offsets, sorted(offsets, reverse=True))
        self.assertTrue(tail.endswith('\nfinal diagnostic\n'))
        self.assertNotIn('outside the byte limit', tail)

    def test_nextest_tail_stops_after_enough_lines_and_preserves_chunk_boundaries(self) -> None:
        for ending in ('', '\n'):
            with self.subTest(ending=ending):
                lines = [f'{number}: ' + 'x' * 1000 for number in range(500)]
                payload = ('\n'.join(lines) + ending).encode()
                reader = LogReadProbe(payload)
                with mock.patch.object(Path, 'open', return_value=reader):
                    tail = trial.nextest_log_tail(self.root / 'nextest.log')
                self.assertEqual(tail, '\n'.join(lines[-200:]) + ending)
                sizes = reader.read_sizes
                self.assertLess(sum(sizes), len(payload))
                self.assertGreater(len(sizes), 1)
        empty = self.root / 'empty.log'
        _ = empty.write_bytes(b'')
        self.assertEqual(trial.nextest_log_tail(empty), '')

    def test_preflight_refuses_missing_tools_before_any_hold_wait(self) -> None:
        for missing in ('git', 'nix', 'nix-store', 'python', 'sh', 'cargo', 'rustc',
                        'rustdoc', 'cargo-nextest', 'zstd', 'vulkaninfo', 'dev shell'):
            with self.subTest(missing=missing):
                def checked(argv: Sequence[str], _cwd: Path, *,
                            environment: Mapping[str, str] = os.environ, timeout: float | None = None) -> str:
                    _ = environment, timeout
                    if 'rev-parse' in argv:
                        return 'a' * 40
                    if missing == 'dev shell' and argv[:2] == ['nix', 'develop']:
                        raise subprocess.CalledProcessError(1, argv)
                    executable = sys.executable if missing == 'python' else missing
                    if executable in argv or (missing == 'vulkaninfo' and argv[-1] == '--summary'):
                        raise FileNotFoundError(executable)
                    if argv[-1] == 'nixpkgs#vulkan-tools':
                        return '/nix/store/fixture-vulkan-tools'
                    if 'command -v "$1"' in argv:
                        return argv[-1]
                    return 'fixture version'

                with (mock.patch.object(trial, 'checked', side_effect=checked),
                      mock.patch('shutil.which', side_effect=fake_tool_path),
                      mock.patch.object(trial, 'export_source', return_value=self.root),
                      mock.patch.object(merge_gate.BuildHoldWait, 'wait_until_free') as hold,
                      mock.patch.object(merge_gate, 'gate_reservation') as admission):
                    with self.assertRaisesRegex(ValueError, missing):
                        trial.prepare(self.root, 'HEAD', trial.trial_root() / missing)
                hold.assert_not_called()
                admission.assert_not_called()
                self.assertEqual(list((trial.trial_root() / missing).iterdir()), [trial.trial_root() / missing / 'prepare.log'])

    def test_preflight_refuses_software_vulkan_and_command_timeout(self) -> None:
        for failure in ('software', 'timeout'):
            with self.subTest(failure=failure):
                expected_timeout = conf.read('aws_target').number('gpu_size_trial_preflight_timeout_s') + 1
                _ = write_config_fixture(conf.path('aws_target'),
                                         f'gpu_size_trial_preflight_timeout_s={expected_timeout}')

                def checked(argv: Sequence[str], _cwd: Path, *,
                            environment: Mapping[str, str] = os.environ, timeout: float | None = None) -> str:
                    _ = environment
                    self.assertEqual(timeout, expected_timeout)
                    if argv[-1] == 'nixpkgs#vulkan-tools':
                        return '/nix/store/fixture-vulkan-tools'
                    if 'command -v "$1"' in argv:
                        return argv[-1]
                    if argv[-1] == '--summary':
                        if failure == 'timeout':
                            raise subprocess.TimeoutExpired(argv, expected_timeout)
                        return SOFTWARE
                    return 'fixture version'

                with (mock.patch.object(trial, 'checked', side_effect=checked),
                      mock.patch('shutil.which', side_effect=fake_tool_path)):
                    with self.assertRaisesRegex(ValueError, 'vulkaninfo'):
                        _ = trial.preflight(self.root, {})

    def test_junit_config_preserves_groups_and_existing_options(self) -> None:
        original = '[profile.ci]\ndefault-filter="all()"\n[profile.ci.junit]\npath="old.xml"\nstore-success-output=true\n[test-groups.slow]\nmax-threads=1\n'
        parsed = trial.mapping(cast(object, tomllib.loads(trial.junit_config(original, self.root / 'new.xml'))))
        profile = trial.mapping(trial.mapping(parsed['profile'])['ci'])
        junit = trial.mapping(profile['junit'])
        self.assertEqual(junit['path'], str(self.root / 'new.xml'))
        self.assertTrue(junit['store-success-output'])
        self.assertIn('slow', trial.mapping(parsed['test-groups']))

    def test_inventory_excludes_ignored_and_filtered_tests(self) -> None:
        case = {'ignored': False, 'filter-match': {'status': 'matches'}}
        listed: dict[str, object] = {'rust-suites': {'fixture': {'testcases': {
            'kept': case, 'ignored': {**case, 'ignored': True},
            'filtered': {**case, 'filter-match': {'status': 'mismatch'}}}}}}
        self.assertEqual(trial.inventory(listed), [{'binary': 'fixture', 'test': 'kept'}])

    def test_archive_config_includes_shared_libraries_and_keeps_existing_includes(self) -> None:
        source = self.root / 'source'
        (source / '.config').mkdir(parents=True)
        target = self.root / 'custom-target'
        libraries = [target / 'debug/deps/libbevy_dylib-fixture.so', target / 'debug/build/native/out/libother.so.1']
        for path in libraries:
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_bytes(b'fixture shared library')
        _ = (libraries[0].parent / 'libfixture.rlib').write_bytes(b'fixture static library')
        (target / 'directory.so').mkdir()
        entries = [
            '',
            '[profile.default.archive]\ninclude = [{ path = "fixtures", relative-to = "target" }]\n',
            '[profile.ci.archive]\ninclude = [{ path = "fixtures", relative-to = "target" }]\n',
            '[[profile.ci.archive.include]]\npath = "fixtures"\nrelative-to = "target"\n',
            '[profile.ci.archive]\ninclude = [{ path = '
            + json.dumps(libraries[0].relative_to(target).as_posix()) + ', relative-to = "target" }]\n',
        ]
        for existing in entries:
            with self.subTest(existing=existing):
                original = '[profile.ci]\ndefault-filter="all()"\n' + existing
                path = source / '.config/nextest.toml'
                _ = path.write_text(original)
                result = trial.mapping(cast(object, tomllib.loads(trial.archive_config(source, target))))
                profile = trial.mapping(trial.mapping(result['profile'])[trial.ARCHIVE_PROFILE])
                self.assertEqual(profile['inherits'], 'ci')
                self.assertIn(str(profile['inherits']), trial.mapping(result['profile']))
                archive = trial.mapping(profile['archive'])
                includes = [trial.mapping(entry) for entry in cast(list[object], archive['include'])]
                paths = [str(entry['path']) for entry in includes]
                self.assertEqual(len(paths), len(set(paths)))
                self.assertTrue(all(path.relative_to(target).as_posix() in paths for path in libraries))
                self.assertFalse(any(path.endswith('.rlib') or path == 'directory.so' for path in paths))
                if 'fixtures' in existing:
                    self.assertIn({'path': 'fixtures', 'relative-to': 'target'}, includes)
                self.assertEqual(path.read_text(), original)

    def test_archive_validation_reports_the_missing_library(self) -> None:
        source = self.root / 'source'
        (source / '.config').mkdir(parents=True)
        _ = (source / '.config/nextest.toml').write_text('[profile.ci]\n')
        library = 'libbevy_dylib-fixture.so'

        def checked(argv: Sequence[str], _cwd: Path, *, environment: Mapping[str, str] = os.environ,
                    capture_stderr: bool = False) -> str:
            _ = environment
            if 'binaries-only' in argv:
                return json.dumps({'rust-build-meta': {'target-directory': str(self.root / 'target')}})
            if 'list' in argv:
                self.assertTrue(capture_stderr)
                raise subprocess.CalledProcessError(104, argv, stderr='error while loading shared libraries: ' + library)
            return ''

        with mock.patch.object(trial, 'checked', side_effect=checked):
            with self.assertRaisesRegex(ValueError, library):
                _ = trial.archive_tests(self.root, source, {}, ())

    def test_complete_prepare_and_comparison_use_fake_builds(self) -> None:
        repo = self.root / 'repo'
        repo.mkdir()
        for name in ('Cargo.lock', 'flake.lock'):
            _ = (repo / name).write_text('fixture lock\n')
        (repo / '.config').mkdir()
        _ = (repo / '.config/nextest.toml').write_text('[profile.ci]\ndefault-filter="all()"\n')
        for args in (['init', '-q'], ['add', '.'], ['-c', 'user.name=Fixture', '-c',
                     'user.email=fixture@example.invalid', 'commit', '-qm', 'Fixture source']):
            _ = subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True, timeout=10)
        commit = trial.checked(['git', 'rev-parse', 'HEAD'], repo)
        folder = trial.trial_root() / 'trial'
        folder.mkdir(parents=True)
        outside = self.root / 'profile-target'
        outside.mkdir()
        _ = (outside / 'keep').write_text('fixture runtime data')
        for name in ('toolchain-profile', 'runtime-profile', 'runtime-profile-1-link', 'system-profile'):
            (folder / name).symlink_to(outside, target_is_directory=True)
        limits = trial.settings()
        trial.write_json(folder / 'request.json', {'repo': str(repo), 'commit': commit,
            'limits': {'spot_max_usd_per_hour': {key: str(value) for key, value in limits.spot_max_usd_per_hour.items()}}})
        cache = self.root / 'cargo'
        (cache / 'registry').mkdir(parents=True)
        _ = (cache / 'registry/dependency').write_text('cached source')
        _ = (cache / 'credentials.toml').write_text('private fixture token')
        commands: list[tuple[str, ...]] = []
        real_checked = trial.checked
        real_run = subprocess.run
        nix = '/nix/store/fixture-nix/bin/nix'
        nix_store = '/nix/store/fixture-nix/bin/nix-store'

        def tool_path(name: str, *, path: str | None = None) -> str:
            _ = path
            return {'nix': nix, 'nix-store': nix_store}.get(name, name)

        def checked(argv: Sequence[str], cwd: Path, *, environment: Mapping[str, str] = os.environ,
                    timeout: float | None = None, capture_stderr: bool = False) -> str:
            _ = timeout, capture_stderr
            commands.append(tuple(argv))
            if argv[0] in (nix, nix_store):
                argv = [Path(argv[0]).name, *argv[1:]]
            if argv[0] == 'git':
                with mock.patch.object(subprocess, 'run', real_run):
                    return real_checked(argv, cwd, environment=environment)
            if argv[:2] == ['nix', 'eval']:
                self.assertTrue((folder / 'inventory.json').exists())
                return '/nix/store/fixture-nixpkgs'
            if argv[:2] == ['nix', 'print-dev-env']:
                return 'export LD_LIBRARY_PATH=/nix/store/fixture-runtime/lib'
            if argv[:2] == ['nix', 'build']:
                if argv[-1] == 'nixpkgs#vulkan-tools':
                    return '/nix/store/fixture-vulkan-tools'
                return '/nix/store/fixture-system' if 'gpu-trial.nix' in argv[-1] else '/nix/store/fixture-rust'
            if argv[0] == 'nix-store':
                return '/nix/store/fixture-system\n/nix/store/fixture-runtime\n/nix/store/fixture-rust'
            if 'command -v "$1"' in argv:
                return argv[-1]
            if argv[0] == '/nix/store/fixture-rust/bin/cargo' and '--version' in argv:
                self.assertEqual(list(Path(environment['HOME']).iterdir()), [])
            if 'fetch' in argv:
                fresh = Path(environment['CARGO_HOME']) / 'registry'
                fresh.mkdir()
                _ = (fresh / 'dependency').write_text('fresh source')
            if argv[-2:] == ['/nix/store/fixture-vulkan-tools/bin/vulkaninfo', '--summary']:
                return HARDWARE + SOFTWARE
            if 'binaries-only' in argv:
                target = folder / 'source/target'
                library = target / 'debug/deps/libbevy_dylib-fixture.so'
                library.parent.mkdir(parents=True)
                _ = library.write_bytes(b'fixture shared library')
                self.assertIn('--all-features', argv)
                return json.dumps({'rust-build-meta': {'target-directory': str(target)}})
            if 'archive' in argv:
                config = Path(argv[argv.index('--tool-config-file') + 1].split(':', 1)[1])
                self.assertIn('libbevy_dylib-fixture.so', config.read_text())
                self.assertIn('--all-features', argv)
                self.assertEqual(argv[argv.index('--config-file') + 1], str(folder / 'source/.config/nextest.toml'))
                self.assertEqual(argv[argv.index('--profile') + 1], trial.ARCHIVE_PROFILE)
                _ = (folder / 'hana-tests.tar.zst').write_bytes(b'fixture archive')
            if 'list' in argv:
                return json.dumps({'rust-suites': {'fixture': {'testcases': {'test': {
                    'ignored': False, 'filter-match': {'status': 'matches'}}}}}})
            return 'fixture version'

        def execute(argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            if argv[0] == 'git':
                return real_run(argv, cwd=cast(Path, kwargs['cwd']), stdout=cast(BinaryIO, kwargs['stdout']), check=True)
            if argv[0] == nix_store:
                _ = cast(BinaryIO, kwargs['stdout']).write(b'fixture closure')
            else:
                commands.append(tuple(argv))
                extraction = Path(argv[argv.index('--extract-to') + 1])
                self.assertTrue(extraction.is_dir())
                self.assertFalse((folder / 'source/target').exists())
                if extraction.parent.name == '2':
                    self.assertFalse((extraction.parent.parent / '1/archive').exists())
                    self.assertFalse((extraction.parent.parent / '1/source').exists())
                _ = (extraction / 'binary').write_bytes(b'fixture extracted binary')
                environment = cast(dict[str, str], kwargs['env'])
                samples = Path(environment['GPU_TRIAL_SAMPLES'])
                samples.mkdir()
                trial.write_json(samples / 'test.json', {'binary': 'fixture', 'test': 'test',
                    **asdict(trial.TestSample(0, 1.2, 1024))})
                _ = (samples.parent / 'junit.xml').write_text('<testsuites/>')
                self.assertNotEqual(cast(Path, kwargs['cwd']), folder / 'source')
            return subprocess.CompletedProcess(argv, 0, b'', b'')

        with (mock.patch.object(trial, 'checked', side_effect=checked),
              mock.patch.object(subprocess, 'run', side_effect=execute),
              mock.patch.object(trial, 'scan_toolchain_wrappers'),
              mock.patch('shutil.which', side_effect=tool_path),
              mock.patch.dict(os.environ, {'CARGO_HOME': str(cache)})):
            source = trial.export_source(repo, commit, folder)
            request = trial.read_json(folder / 'request.json')
            request['tools'] = trial.preflight(source, trial.trial_environment())
            trial.write_json(folder / 'request.json', request)
            trial.prepare_worker(folder)
        manifest = trial.read_json(folder / 'manifest.json')
        self.assertEqual(manifest['state'], 'prepared')
        self.assertTrue({path.name for path in folder.iterdir()} <= trial.PREPARED_NAMES)
        self.assertEqual((outside / 'keep').read_text(), 'fixture runtime data')
        for name in ('source', 'archive-nextest.toml', 'toolchain-probe-home',
                     'machines/natedev/1/archive', 'machines/natedev/2/archive'):
            self.assertFalse((folder / name).exists())
        self.assertEqual(manifest['commit'], commit)
        self.assertEqual(manifest['vulkan_tools'], '/nix/store/fixture-vulkan-tools')
        self.assertEqual(trial.read_json(folder / 'machines/natedev/machine.json')['adapters'], [
            {'type': 'PHYSICAL_DEVICE_TYPE_DISCRETE_GPU', 'name': 'Fixture GPU'},
            {'type': 'PHYSICAL_DEVICE_TYPE_CPU', 'name': 'llvmpipe'}])
        self.assertFalse(any(command[0] in ('nix', 'nix-store') for command in commands))
        self.assertEqual([command for command in commands if command[-1] == 'nixpkgs#vulkan-tools'], [
            (nix, 'build', '--no-link', '--print-out-paths', '--inputs-from', str(folder / 'source'),
             'nixpkgs#vulkan-tools')])
        self.assertEqual([command for command in commands if command[-1] == '--summary'], [
            (nix, 'develop', '--no-write-lock-file', '.#default', '-c',
             '/nix/store/fixture-vulkan-tools/bin/vulkaninfo', '--summary')] * 3)
        for name, value in trial.mapping(manifest['sha256']).items():
            self.assertEqual(trial.digest(folder / name), value)
        self.assertFalse((folder / 'cargo-home/credentials.toml').exists())
        self.assertEqual((folder / 'cargo-home/registry/dependency').read_text(), 'fresh source')
        self.assertFalse(any('rustup' in command or 'add-path' in command for command in commands))
        runs = [command for command in commands if 'run' in command]
        self.assertEqual(len(runs), 2)
        self.assertTrue(all(command[command.index('--retries') + 1] == '0' for command in runs))
        candidate = next(iter(limits.spot_max_usd_per_hour))
        machine = folder / 'machines' / candidate
        import shutil
        _ = shutil.copytree(folder / 'machines/natedev', machine)
        report = trial.compare(folder)
        self.assertIn('| same |', report)
        self.assertIn('| incomplete |', report)
        self.assertIn('largest single process', report)
        sample = machine / '2/samples/test.json'
        row = trial.read_json(sample)
        row['status'] = 1
        trial.write_json(sample, row)
        self.assertIn('| flaky |', trial.compare(folder))
        baseline_sample = folder / 'machines/natedev/2/samples/test.json'
        trial.write_json(baseline_sample, row)
        self.assertIn('| inconclusive |', trial.compare(folder))
        baseline_sample.unlink()
        self.assertNotIn('| same |', trial.compare(folder))
        self.assertIn('0 / missing', trial.compare(folder))
        (machine / 'machine.json').unlink()
        self.assertIn('| incomplete |', trial.compare(folder))

    def test_toolchain_wrapper_has_no_home_dependency_and_uses_empty_home(self) -> None:
        toolchain = self.root / 'toolchain'
        (toolchain / 'bin').mkdir(parents=True)
        cargo = toolchain / 'bin/cargo'
        _ = cargo.write_text('#!/bin/sh\nprintf "cargo fixture-version\\n"\n')
        cargo.chmod(0o700)
        trial.scan_toolchain_wrappers(toolchain)
        empty = self.root / 'empty-home'
        empty.mkdir()
        result = trial.checked([str(cargo), '--version'], self.root,
                               environment={**os.environ, 'HOME': str(empty)})
        self.assertEqual(result, 'cargo fixture-version')
        self.assertEqual(list(empty.iterdir()), [])
        _ = cargo.write_text('#!/bin/sh\nexec /home/fixture/.cargo/bin/cargo-tile-capture "$@"\n')
        with self.assertRaisesRegex(ValueError, 'depends on a user home'):
            trial.scan_toolchain_wrappers(toolchain)

    def test_bad_measurements_are_incomplete(self) -> None:
        run = self.root / 'run'
        (run / 'samples').mkdir(parents=True)
        trial.write_json(run / 'run.json', {'status': 0})
        _ = (run / 'junit.xml').write_text('<testsuites/>')
        for value in (float('nan'), -1, True):
            trial.write_json(run / 'samples/test.json', {'binary': 'fixture', 'test': 'test',
                'status': 0, 'wall_s': value, 'largest_process_peak_rss_kib': 123})
            self.assertEqual(trial.read_run(run), {})


@final
class FakeGpuAws:
    def __init__(self, manifest: dict[str, object], types: Sequence[str], region: str) -> None:
        self.manifest = manifest
        self.types = tuple(types)
        self.region = region
        self.now = 1768478400.0
        self.quotas: dict[str, int] = {region: largest_gate_vcpus(tuple(trial.settings().spot_max_usd_per_hour))}
        self.missing: set[str] = set()
        self.expensive: set[str] = set()
        self.events: list[str] = []
        self.objects: dict[str, bytes] = {}
        self.instances: dict[str, str] = {}
        self.launches: list[dict[str, object]] = []
        self.fail_result = False
        self.unconfirmed = False

    def clock(self) -> float:
        return self.now

    def pause(self, seconds: float) -> None:
        self.now += max(1, seconds)

    def results(self, attempt: str, size: str) -> None:
        for number in (1, 2):
            payloads = {'machine.json': {'commit': self.manifest['commit'],
                                        'archive_sha256': trial.mapping(self.manifest['sha256'])['hana-tests.tar.zst']},
                        str(number) + '/run.json': {'status': 0},
                        str(number) + '/samples/test.json': {'binary': 'fixture', 'test': 'test',
                            **asdict(trial.TestSample(0, 1.0, 4096))}}
            output = BytesIO()
            with tarfile.open(fileobj=output, mode='w:gz') as archive:
                files = {name: json.dumps(value).encode() for name, value in payloads.items()}
                files.update({'vulkan.txt': HARDWARE.encode(), str(number) + '/junit.xml': b'<testsuites/>'})
                for name, data in files.items():
                    entry = tarfile.TarInfo('machines/' + size + '/' + name)
                    entry.size = len(data)
                    archive.addfile(entry, BytesIO(data))
            self.objects['artifacts/gpu-size-trial/' + attempt + '/result-' + str(number) + '.tar.gz'] = output.getvalue()
        self.objects['artifacts/gpu-size-trial/' + attempt + '/finished.json'] = b'{"status":"complete"}'

    def execute(self, arguments: Sequence[str], *, capture_output: bool, text: bool,
                timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        del capture_output, text, timeout, check, env
        if arguments[0] != 'aws':
            raise AssertionError('Unexpected external command: ' + str(arguments))
        offset = arguments.index('--region')
        region, service, operation = arguments[offset + 1:offset + 4]
        self.events.append(operation)
        reply: dict[str, object]
        if operation == 'get-service-quota':
            self.events[-1] += ':' + region
            reply = {'Quota': {'Value': self.quotas.get(region, 0)}}
        elif operation == 'describe-instance-type-offerings':
            size = arguments[arguments.index('--filters') + 1].split('Values=')[1]
            reply = {'InstanceTypeOfferings': [] if size in self.missing else [
                {'InstanceType': size, 'Location': region + 'a'}]}
        elif operation == 'describe-spot-price-history':
            size = arguments[arguments.index('--instance-types') + 1]
            maximum = trial.settings().spot_max_usd_per_hour[size]
            reply = {'SpotPriceHistory': [{'InstanceType': size, 'AvailabilityZone': region + 'a',
                'SpotPrice': str(maximum * (2 if size in self.expensive else Decimal('0.5'))),
                'Timestamp': datetime.fromtimestamp(self.now, UTC).isoformat()}]}
        elif operation == 'put-object':
            key = arguments[arguments.index('--key') + 1]
            self.objects[key] = Path(arguments[arguments.index('--body') + 1]).read_bytes()
            reply = {}
        elif operation == 'get-object':
            if self.fail_result:
                self.fail_result = False
                return subprocess.CompletedProcess(arguments, 1, '', 'An error occurred (AccessDenied) when calling GetObject')
            key = arguments[arguments.index('--key') + 1]
            self.events[-1] += ':' + key.rsplit('/', 1)[-1]
            if key not in self.objects:
                return subprocess.CompletedProcess(arguments, 1, '', 'An error occurred (NoSuchKey) when calling GetObject')
            _ = Path(arguments[arguments.index('--key') + 2]).write_bytes(self.objects[key])
            reply = {}
        elif operation == 'export-credentials' and service == 'configure':
            reply = {'AccessKeyId': 'fixture-access', 'SecretAccessKey': 'fixture-secret'}
        elif operation == 'run-instances':
            if any(state != 'terminated' for state in self.instances.values()):
                raise AssertionError('The next box started before confirmed termination')
            launch = trial.mapping(cast(object, json.loads(arguments[arguments.index('--cli-input-json') + 1])))
            self.launches.append(launch)
            size = str(launch['InstanceType'])
            self.events[-1] += ':' + size
            instance = 'i-fixture-' + str(len(self.launches))
            self.instances[instance] = 'running'
            self.results(str(launch['ClientToken']), size)
            reply = {'Instances': [{'InstanceId': instance}]}
            self.now += 60
        elif operation == 'terminate-instances':
            for instance in arguments[arguments.index('--instance-ids') + 1:arguments.index('--output')]:
                if not self.unconfirmed:
                    self.instances[instance] = 'terminated'
            reply = {}
        elif operation == 'describe-instances':
            if '--filters' in arguments:
                reply = {'Reservations': []}
            else:
                ids = arguments[arguments.index('--instance-ids') + 1:arguments.index('--output')]
                reply = {'Reservations': [{'Instances': [{'InstanceId': instance, 'State': {'Name': self.instances[instance]}}
                                                         for instance in ids]}]}
        else:
            raise AssertionError('Unexpected AWS command: ' + str(arguments))
        return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')


@final
class GpuOnlineTests(CostFixture):
    folder: Path = Path()
    book: ledger.CostLedger = ledger.CostLedger(Path())
    fake: FakeGpuAws = cast(FakeGpuAws, object())
    worker: trial.GpuTrialWorker = cast(trial.GpuTrialWorker, object())
    deployment: dict[str, object] = {}

    @override
    def setUp(self) -> None:
        super().setUp()
        candidates = conf.read('cloud_gate').words('spot_candidate_regions')
        region = candidates[0]
        self.folder = trial.trial_root() / 'gpu-trial'
        self.folder.mkdir(parents=True)
        limits = trial.settings()
        prices = dict(list(limits.spot_max_usd_per_hour.items())[:2])
        _ = write_config_fixture(conf.path('cloud_gate'), 'gpu_size_trial_spot_max_usd_per_hour='
                                  + json.dumps({key: str(value) for key, value in prices.items()}))
        for name in ('source.tar', 'runtime.env', 'gpu-trial.nix', 'closure.json', 'request.json', 'gpu_trial_wrapper.py',
                     'hana-tests.tar.zst', 'closure.export'):
            _ = (self.folder / name).write_text('fixture ' + name)
        (self.folder / 'cargo-home').mkdir()
        trial.write_json(self.folder / 'inventory.json', {'tests': [{'binary': 'fixture', 'test': 'test'}]})
        manifest: dict[str, object] = {'schema': 2, 'toolchain_source': 'nix', 'state': 'prepared', 'trial': 'stable-fixture-trial', 'commit': 'a' * 40,
            'system': '/nix/store/fixture-system', 'toolchain': '/nix/store/fixture-rust',
            'limits': {'limit_usd': str(limits.limit_usd), 'spot_max_usd_per_hour': {key: str(value) for key, value in prices.items()}},
            'sha256': {name: trial.digest(path) for path in self.folder.iterdir() if path.is_file()
                       for name in (path.name,)}}
        trial.write_json(self.folder / 'manifest.json', manifest)
        self.fake = FakeGpuAws(manifest, tuple(prices), region)
        api = AwsCli('fixture-scaler', 'us-east-1', 5, self.fake.execute)
        self.book = ledger.CostLedger(self.state)
        self.book.record_inventory(ledger.ResourceInventory(int(self.fake.now) - 1, (), 'fixture', builder_removed=True))
        gates = cloud_gate.GateWorker(api, self.book, self.settings, self.folder / 'sidecar',
                                     clock=self.fake.clock, pause=self.fake.pause)
        _ = self.enterContext(mock.patch.object(gates, 'charges'))
        self.worker = trial.GpuTrialWorker(gates)
        self.deployment = {'bucket': 'fixture-bucket', 'deployment_id': 'fixture-deployment', 'regions': [
            {'region': candidate, 'template': 'lt-fixture', 'version': '1', 'image': 'ami-nixos',
             'security_group': 'sg-fixture', 'subnets': [{'id': 'subnet-fixture', 'zone': candidate + 'a'}]}
            for candidate in candidates]}

    def execute(self) -> None:
        with redirect_stdout(StringIO()):
            self.worker.execute(self.folder, self.deployment)

    def test_online_completion_keeps_results_and_removes_large_inputs(self) -> None:
        self.execute()
        self.assertEqual(trial.read_json(self.folder / 'online-result.json')['status'], 'complete')
        for name in trial.RUN_INPUT_NAMES:
            self.assertFalse((self.folder / name).exists())
        for size in self.fake.types:
            self.assertTrue((self.folder / 'machines' / size / '1/junit.xml').is_file())
            self.assertTrue((self.folder / 'boxes' / size / 'outcome.json').is_file())
        self.assertIn('GPU size trial', trial.compare(self.folder))
        self.assertEqual(trial.validate_prepared(self.folder)['trial'], 'stable-fixture-trial')

    def test_invalid_online_folders_never_execute_or_remove_inputs(self) -> None:
        nested = trial.trial_root() / 'group/trial'
        nested.mkdir(parents=True)
        outside = self.root / 'outside-trial'
        outside.mkdir()
        alias = trial.trial_root() / 'alias'
        alias.symlink_to(self.folder, target_is_directory=True)
        for folder in (nested, outside):
            _ = (folder / 'closure.export').write_text('fixture input')
        for folder in (nested, outside, alias):
            with self.subTest(folder=folder):
                original = (folder / 'closure.export').read_bytes()
                with mock.patch.object(self.worker, 'run_trial') as run:
                    with self.assertRaisesRegex(ValueError, 'canonical direct child'):
                        self.worker.execute(folder, self.deployment)
                    run.assert_not_called()
                with mock.patch.object(trial, 'AwsCli') as api, redirect_stderr(StringIO()):
                    for mode in ('--worker', '--cleanup'):
                        self.assertEqual(trial.online_main([mode, str(folder), '--directory', str(self.state),
                                                           '--settings', str(self.settings)]), 1)
                    api.assert_not_called()
                service = mock.Mock(return_value=0)
                with self.assertRaisesRegex(ValueError, 'canonical direct child'):
                    _ = trial.start_service(folder, self.state, self.settings, service)
                service.assert_not_called()
                self.assertEqual((folder / 'closure.export').read_bytes(), original)
                for name in ('run-error.json', 'inputs-removed.json'):
                    self.assertFalse((folder / name).exists())
        self.assertEqual(self.fake.events, [])

    def test_online_failure_saves_the_error_before_removing_inputs(self) -> None:
        self.fake.fail_result = True
        with self.assertRaises(AwsUnavailable):
            self.execute()
        self.assertEqual(trial.read_json(self.folder / 'run-error.json')['status'], 'failed')
        self.assertTrue((self.folder / 'boxes' / self.fake.types[0] / 'launch.json').is_file())
        for name in trial.RUN_INPUT_NAMES:
            self.assertFalse((self.folder / name).exists())

    def test_stop_cleanup_saves_an_error_and_removes_interrupted_run_inputs(self) -> None:
        with (mock.patch.object(trial, 'AwsCli', return_value=self.worker.gates.api),
              mock.patch.object(trial.GpuTrialWorker, 'cleanup', return_value=True) as cleanup):
            status = trial.online_main(['--cleanup', str(self.folder), '--directory', str(self.state),
                                        '--settings', str(self.settings)])
        self.assertEqual(status, 0)
        cleanup.assert_called_once_with(self.folder)
        self.assertIn('before it saved a final result', str(trial.read_json(self.folder / 'run-error.json')['reason']))
        for name in trial.RUN_INPUT_NAMES:
            self.assertFalse((self.folder / name).exists())
        self.assertTrue((self.folder / 'manifest.json').is_file())

    def test_upload_failure_removes_the_payload_and_requires_new_preparation(self) -> None:
        with mock.patch.object(self.worker, 'upload', side_effect=AwsUnavailable('fixture upload failed')):
            with self.assertRaisesRegex(AwsUnavailable, 'fixture upload failed'):
                self.execute()
        for name in trial.RUN_INPUT_NAMES:
            self.assertFalse((self.folder / name).exists())
        with self.assertRaisesRegex(ValueError, 'inputs were removed before upload'):
            _ = trial.validate_prepared(self.folder)

    def test_old_preparation_requires_the_nix_toolchain_fix(self) -> None:
        manifest = trial.read_json(self.folder / 'manifest.json')
        manifest['schema'] = 1
        _ = manifest.pop('toolchain_source')
        trial.write_json(self.folder / 'manifest.json', manifest)
        with self.assertRaisesRegex(ValueError, 'Prepare the trial again with the Nix toolchain'):
            self.execute()
        self.assertEqual(self.fake.events, [])

    def test_no_region_granted_stops_before_upload_or_launch(self) -> None:
        self.fake.quotas.clear()
        with self.assertRaisesRegex(ValueError, 'No configured region has an applied G and VT Spot quota'):
            self.execute()
        self.assertEqual(self.fake.launches, [])
        self.assertNotIn('put-object', self.fake.events)
        regions = conf.read('cloud_gate').words('spot_candidate_regions')
        self.assertEqual(self.fake.events, ['get-service-quota:' + region for region in regions])

    def selection_fixture(self, types: str) -> tuple[tuple[str, ...], int]:
        price = next(iter(trial.settings().spot_max_usd_per_hour.values()))
        _ = write_config_fixture(conf.path('cloud_gate'),
            'gpu_size_trial_spot_max_usd_per_hour='
            + json.dumps({name: str(price) for name in types.split()}) + '\n'
            + 'spot_candidate_regions=fixture-first fixture-second fixture-last')
        values = conf.read('cloud_gate')
        regions = values.words('spot_candidate_regions')
        self.deployment['regions'] = [{'region': region} for region in regions]
        prices = trial.mapping(cast(object, json.loads(values.text('gpu_size_trial_spot_max_usd_per_hour'))))
        return regions, largest_gate_vcpus(tuple(prices))

    def test_small_types_accept_a_region_with_room_for_two_boxes(self) -> None:
        regions, needed = self.selection_fixture('g6.xlarge g4dn.xlarge')
        self.fake.quotas = {regions[0]: 2 * needed}
        self.assertEqual(self.worker.select_region(self.deployment, trial.settings())['region'], regions[0])
        self.assertEqual(self.fake.events, ['get-service-quota:' + regions[0]])

    def test_region_below_the_largest_configured_gpu_type_is_skipped(self) -> None:
        regions, needed = self.selection_fixture('g6.xlarge g6.8xlarge')
        smaller = largest_gate_vcpus((next(iter(trial.settings().spot_max_usd_per_hour)),))
        self.fake.quotas = {regions[0]: smaller, regions[1]: needed, regions[2]: needed * 2}
        self.assertEqual(self.worker.select_region(self.deployment, trial.settings())['region'], regions[1])
        self.assertEqual(self.fake.events, ['get-service-quota:' + region for region in regions[:2]])

    def test_region_selection_uses_captured_limits_after_configuration_changes(self) -> None:
        regions, needed = self.selection_fixture('g6.xlarge g6.8xlarge')
        limits = trial.settings()
        _, smaller = self.selection_fixture('g6.xlarge')
        self.assertLess(smaller, needed)
        self.fake.quotas = {regions[0]: smaller, regions[1]: needed}
        self.assertEqual(self.worker.select_region(self.deployment, limits)['region'], regions[1])
        self.assertEqual(self.fake.events, ['get-service-quota:' + region for region in regions[:2]])

    def test_quota_refusal_names_the_requirement_and_every_applied_value(self) -> None:
        regions, needed = self.selection_fixture('g6.xlarge g6.8xlarge')
        self.fake.quotas = {region: needed - index - 1 for index, region in enumerate(regions)}
        with self.assertRaises(ValueError) as refused:
            _ = self.worker.select_region(self.deployment, trial.settings())
        message = str(refused.exception)
        self.assertIn(f'at least {needed} vCPUs', message)
        for region in regions:
            self.assertIn(f'{region}={self.fake.quotas[region]}', message)
        self.assertEqual(self.fake.events, ['get-service-quota:' + region for region in regions])
        self.assertEqual(self.fake.launches, [])

    def test_unknown_gpu_vcpu_count_refuses_before_aws_reads(self) -> None:
        limits = trial.settings()
        _ = self.selection_fixture('fixture.unknown')
        price = next(iter(limits.spot_max_usd_per_hour.values()))
        unknown = replace(limits, spot_max_usd_per_hour={'fixture.unknown': price})
        with self.assertRaisesRegex(ValueError, 'No vCPU count is available for configured GPU type: fixture.unknown'):
            _ = self.worker.select_region(self.deployment, unknown)
        self.assertEqual(self.fake.events, [])

    def test_not_offered_type_is_reported_and_next_type_runs(self) -> None:
        first, second = self.fake.types
        self.fake.missing.add(first)
        self.execute()
        self.assertEqual([row['InstanceType'] for row in self.fake.launches], [second])
        self.assertEqual(trial.read_json(self.folder / 'boxes' / first / 'outcome.json')['status'], 'not offered')

    def test_price_above_max_is_reported_without_launch(self) -> None:
        first, second = self.fake.types
        self.fake.expensive.add(first)
        self.execute()
        self.assertEqual([row['InstanceType'] for row in self.fake.launches], [second])
        self.assertEqual(trial.read_json(self.folder / 'boxes' / first / 'outcome.json')['status'], 'price above max')

    def test_results_and_resolution_precede_next_launch(self) -> None:
        self.execute()
        self.assertEqual([row['InstanceType'] for row in self.fake.launches], list(self.fake.types))
        events = self.fake.events
        first, second = self.fake.types
        self.assertLess(events.index('get-object:result-2.tar.gz'), events.index('terminate-instances'))
        self.assertLess(events.index('describe-instances'), events.index('run-instances:' + second))
        rows = self.book.read_snapshot().gate_reservations
        self.assertTrue(all(isinstance(row.state, ledger.GateTerminated) for row in rows))
        self.assertTrue(all(row.mode == 'gpu-size-trial' for row in rows))
        for size in (first, second):
            self.assertTrue((self.folder / 'machines' / size / '2/samples/test.json').exists())
        for launch in self.fake.launches:
            bootstrap = base64.b64decode(str(launch['UserData'])).decode()
            self.assertIn('switch-to-configuration', bootstrap)
            self.assertIn('reboot', bootstrap)
            self.assertNotIn('fixture-secret', bootstrap)
            self.assertNotIn('AWS_SHARED_CREDENTIALS_FILE', bootstrap)
            self.assertEqual(launch['ImageId'], 'ami-nixos')
            self.assertEqual(trial.mapping(trial.mapping(launch['InstanceMarketOptions'])['SpotOptions'])['MaxPrice'],
                             str(trial.settings().spot_max_usd_per_hour[str(launch['InstanceType'])]))
            self.assertEqual(trial.mapping(trial.mapping(cast(list[object], launch['BlockDeviceMappings'])[0])['Ebs'])['VolumeSize'],
                             conf.read('cloud_gate').whole('gpu_size_trial_root_gib'))

    def test_budget_counts_previous_open_reservation(self) -> None:
        artifacts = self.worker.artifacts
        def uploaded(folder: Path, online: dict[str, object]) -> None:
            artifacts(folder, online)
            raise ValueError('fixture stops after upload')
        with mock.patch.object(self.worker, 'artifacts', side_effect=uploaded):
            with self.assertRaisesRegex(ValueError, 'fixture stops after upload'):
                self.execute()
        limits = trial.settings()
        first = self.fake.types[0]
        next_cost = limits.spot_max_usd_per_hour[first] * limits.lifetime_minutes / 60 + limits.box_extra_usd
        remaining = limits.limit_usd - next_cost / 2
        reservation = ledger.GateReservation('older-attempt', int(self.fake.now), int(self.fake.now) + 3600,
            remaining, Decimal(0), mode='gpu-size-trial', region=self.fake.region,
            trial=ledger.GateTrialBudget('stable-fixture-trial', limits.limit_usd))
        self.assertIsInstance(self.book.reserve_gate(self.settings, reservation), ledger.GateReserved)
        self.execute()
        self.assertEqual(self.fake.launches, [])
        self.assertEqual(trial.read_json(self.folder / 'boxes' / first / 'outcome.json')['status'], 'budget stop')
        self.assertEqual(len(self.book.read_snapshot().gate_reservations), 1)
        self.execute()
        self.assertEqual(self.fake.launches, [])

    def test_restart_reuses_trial_and_does_not_repeat_the_first_launch(self) -> None:
        self.fake.fail_result = True
        with self.assertRaises(AwsUnavailable):
            self.execute()
        first = self.book.read_snapshot().gate_reservations[0]
        self.assertIsInstance(first.state, ledger.GateTerminated)
        uploads = self.fake.events.count('put-object')
        self.execute()
        self.assertEqual([row['InstanceType'] for row in self.fake.launches], list(self.fake.types))
        self.assertEqual(self.fake.events.count('put-object'), uploads)
        rows = self.book.read_snapshot().gate_reservations
        self.assertEqual({row.trial.trial_id for row in rows if isinstance(row.trial, ledger.GateTrialBudget)},
                         {'stable-fixture-trial'})
        self.assertEqual(rows[0].attempt, first.attempt)

    def test_restart_cleans_the_booked_instance_without_local_launch_files(self) -> None:
        self.fake.fail_result = True
        self.fake.unconfirmed = True
        with self.assertRaisesRegex(ValueError, 'termination remains unconfirmed'):
            self.execute()
        first = self.fake.types[0]
        box = self.folder / 'boxes' / first
        (box / 'request.json').unlink()
        (box / 'launch.json').unlink()
        self.fake.unconfirmed = False
        self.execute()
        self.assertTrue(all(value == 'terminated' for value in self.fake.instances.values()))
        self.assertEqual(len(self.fake.launches), len(self.fake.types))

    def test_uncertain_termination_prevents_next_launch(self) -> None:
        self.fake.unconfirmed = True
        with self.assertRaisesRegex(ValueError, 'termination remains unconfirmed'):
            self.execute()
        self.assertEqual(len(self.fake.launches), 1)
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateRunning)

    def test_guest_program_is_standalone_and_has_no_aws_client(self) -> None:
        guest = trial.guest_program()
        _ = compile(guest, 'guest.py', 'exec')
        self.assertNotIn('scripts.aws_target', guest)
        self.assertNotIn('signal.signal', guest)
        self.assertIn('publish(run)', guest)

    def test_service_has_scaler_credentials_and_stop_cleanup(self) -> None:
        commands: list[list[str]] = []
        def execute(arguments: Sequence[str]) -> int:
            commands.append(list(arguments))
            with trial.held_trial(self.folder):
                with self.assertRaisesRegex(ValueError, 'held by a live run'):
                    with trial.new_trial(trial.trial_root() / 'next'):
                        self.fail('The service must hold its prepared folder')
            return 0
        credential = self.root / 'override-credentials'
        with mock.patch.dict(os.environ, {'AWS_SHARED_CREDENTIALS_FILE': str(credential)}):
            self.assertEqual(trial.start_service(self.folder, self.state, self.settings, execute), 0)
        command, = commands
        cleanup, = [value for value in command if value.startswith('--property=ExecStopPost=')]
        self.assertIn('--cleanup', cleanup)
        self.assertIn('scripts.aws_target.gpu_size_trial', cleanup)
        self.assertIn('--setenv=AWS_SHARED_CREDENTIALS_FILE=' + str(credential), command)


if __name__ == '__main__':
    _ = unittest.main()
