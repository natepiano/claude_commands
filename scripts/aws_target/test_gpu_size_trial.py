"""Test offline GPU preparation with private files and fake build commands."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import redirect_stdout
from dataclasses import asdict
from io import StringIO
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

from scripts.aws_target import gpu_size_trial as trial
from scripts.lib import conf
from scripts.lint.memory_test_support import config_environment, write_config_fixture
from scripts.production import merge_gate
from scripts.production.gate_modules import GateRefused


HARDWARE = 'deviceType = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU\ndeviceName = Fixture GPU\n'


@final
class GpuTrialTests(unittest.TestCase):
    root: Path = Path()
    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {**config_environment(self.root / 'config'),
                'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'})))

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

    def test_hardware_check_refuses_cpu_or_missing_adapter(self) -> None:
        self.assertEqual(trial.hardware_vulkan(HARDWARE), ('Fixture GPU',))
        cpu = 'deviceType = PHYSICAL_DEVICE_TYPE_CPU\ndeviceName = llvmpipe\n'
        for payload in (cpu, HARDWARE + cpu, '', 'deviceType = unknown'):
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, 'CPU adapters are refused'):
                _ = trial.hardware_vulkan(payload)

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

    def test_prepare_stops_before_build_when_hold_refuses(self) -> None:
        with (mock.patch.object(trial, 'checked', return_value='a' * 40) as command,
              mock.patch.object(merge_gate.BuildHoldWait, 'wait_until_free', side_effect=GateRefused('held')),
              mock.patch.object(merge_gate, 'gate_reservation') as admission):
            with self.assertRaisesRegex(GateRefused, 'held'):
                trial.prepare(self.root, 'HEAD', self.root / 'trial')
        admission.assert_not_called()
        self.assertEqual(command.call_count, 1)
        self.assertEqual(command.call_args.args[0][0], 'git')
        self.assertFalse((self.root / 'trial/closure.export').exists())

    def test_prepare_checks_hold_again_after_admission(self) -> None:
        with (mock.patch.object(trial, 'checked', return_value='a' * 40),
              mock.patch.object(merge_gate.BuildHoldWait, 'wait_until_free', side_effect=[None, GateRefused('held again')]),
              mock.patch.object(merge_gate, 'gate_reservation') as admission):
            with self.assertRaisesRegex(GateRefused, 'held again'):
                trial.prepare(self.root, 'HEAD', self.root / 'trial')
        place = cast(mock.MagicMock, cast(mock.MagicMock, cast(mock.MagicMock, admission.return_value).__enter__).return_value)
        popen = cast(mock.MagicMock, place.popen)
        popen.assert_not_called()

    def test_prepare_uses_admitted_worker(self) -> None:
        folder = self.root / 'trial'
        with (mock.patch.object(trial, 'checked', return_value='a' * 40),
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
        folder = self.root / 'trial'
        folder.mkdir()
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

        def checked(argv: Sequence[str], cwd: Path, *, environment: Mapping[str, str] = os.environ) -> str:
            commands.append(tuple(argv))
            if argv[0] == 'git':
                with mock.patch.object(subprocess, 'run', real_run):
                    return real_checked(argv, cwd, environment=environment)
            if argv[:2] == ['rustup', 'which']:
                return '/nix/store/fixture-rust/bin/rustc'
            if argv[:2] == ['nix', 'eval']:
                return '/nix/store/fixture-nixpkgs'
            if argv[:3] == ['nix', 'store', 'add-path']:
                return '/nix/store/fixture-rust'
            if argv[:2] == ['nix', 'print-dev-env']:
                return 'export LD_LIBRARY_PATH=/nix/store/fixture-runtime/lib'
            if argv[:2] == ['nix', 'build']:
                self.assertIn('gpu-trial.nix', argv[-1])
                return '/nix/store/fixture-system'
            if argv[0] == 'nix-store':
                return '/nix/store/fixture-system\n/nix/store/fixture-runtime\n/nix/store/fixture-rust'
            if 'vulkaninfo' in argv:
                return HARDWARE
            if 'archive' in argv:
                _ = (folder / 'hana-tests.tar.zst').write_bytes(b'fixture archive')
            if 'list' in argv:
                return json.dumps({'rust-suites': {'fixture': {'testcases': {'test': {
                    'ignored': False, 'filter-match': {'status': 'matches'}}}}}})
            return 'fixture version'

        def execute(argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            if argv[0] == 'git':
                return real_run(argv, cwd=cast(Path, kwargs['cwd']), stdout=cast(BinaryIO, kwargs['stdout']), check=True)
            if argv[0] == 'nix-store':
                _ = cast(BinaryIO, kwargs['stdout']).write(b'fixture closure')
            else:
                commands.append(tuple(argv))
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
              mock.patch.dict(os.environ, {'CARGO_HOME': str(cache)})):
            trial.prepare_worker(folder)
        manifest = trial.read_json(folder / 'manifest.json')
        self.assertEqual(manifest['state'], 'prepared')
        self.assertEqual(manifest['commit'], commit)
        for name, value in trial.mapping(manifest['sha256']).items():
            self.assertEqual(trial.digest(folder / name), value)
        self.assertFalse((folder / 'cargo-home/credentials.toml').exists())
        self.assertTrue((folder / 'cargo-home/registry/dependency').exists())
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

    def test_bad_measurements_are_incomplete(self) -> None:
        run = self.root / 'run'
        (run / 'samples').mkdir(parents=True)
        trial.write_json(run / 'run.json', {'status': 0})
        _ = (run / 'junit.xml').write_text('<testsuites/>')
        for value in (float('nan'), -1, True):
            trial.write_json(run / 'samples/test.json', {'binary': 'fixture', 'test': 'test',
                'status': 0, 'wall_s': value, 'largest_process_peak_rss_kib': 123})
            self.assertEqual(trial.read_run(run), {})


if __name__ == '__main__':
    _ = unittest.main()
