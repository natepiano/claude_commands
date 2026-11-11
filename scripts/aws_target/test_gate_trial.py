"""Measure trial lifecycles with a fake AWS command and clock."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
import json
import os
import pathlib
import shlex
import signal
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from typing import cast, final, override
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from scripts.aws_target import admin_secrets, gate_trial as trial, ledger
from scripts.aws_target.ledger_test_support import CostFixture
from scripts.production import merge_gate
from scripts.lint.memory_test_support import config_environment


@final
class GuestTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, config_environment(root))))

    def test_user_data_fits_ec2_limit_with_module_times_and_400_character_signed_links(self) -> None:
        prefix = 'https://fixture.invalid/object?X-Amz-Signature='
        url = prefix + 'a' * (400 - len(prefix))
        links = {'step': 'size', 'commit': 'a' * 40,
                 **dict.fromkeys(('source', 'guest', 'log', 'result', 'ready-cold', 'ready-warm', 'module_seconds'), url)}
        data = trial.user_data(links, 'f' * 40, 30, 30, 600)
        self.assertLessEqual(len(data.encode()), 16384)

    def tool_session(self) -> str:
        links: dict[str, str] = dict.fromkeys(('source', 'guest', 'log', 'result', 'ready-cold', 'ready-warm'), 'https://fixture.invalid/object')
        links['step'] = 'size'
        data = trial.user_data(links, 'f' * 40, 30, 30, 600)
        return shlex.split(data[data.index('nix --extra-experimental-features'):])[-1]

    def test_tests_use_an_unprivileged_user_after_root_setup(self) -> None:
        session = self.tool_session()
        self.assertIn('runuser -u gate-trial -- env HOME=/home/gate-trial', session)
        self.assertLess(session.index('chown -R gate-trial:users'), session.index('runuser -u gate-trial'))
        self.assertLess(session.index('mount --bind'), session.index('runuser -u gate-trial'))
        self.assertNotIn('/root', trial.GUEST)
        self.assertIn("home = pathlib.Path.home() / '.claude'", trial.GUEST)
        self.assertIn('XDG_RUNTIME_DIR=/run/user/$(id -u gate-trial)', session)

    def test_initialized_environment_preserves_fixture_path_in_zsh(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            profile = root / 'profile'
            _ = profile.write_text('export __NIXOS_SET_ENVIRONMENT_DONE=1\nexport PATH=/run/current-system/sw/bin\n')
            tool = root / 'fixture-tool'
            _ = tool.write_text('#!/bin/sh\nexit 0\n')
            tool.chmod(0o700)
            basedpyright = root / 'basedpyright'
            _ = basedpyright.write_text('#!/bin/sh\necho basedpyright fixture-version\n')
            basedpyright.chmod(0o700)
            python = root / 'python3'
            _ = python.write_text('#!' + sys.executable + '\nimport json, os, subprocess\n'
                + "print(json.dumps({'initialized':os.environ.get('__NIXOS_SET_ENVIRONMENT_DONE'),"
                + "'tool':subprocess.check_output(['zsh','-c','command -v fixture-tool'],text=True).strip(),"
                + "'basedpyright':subprocess.check_output(['basedpyright','--version'],text=True).strip()}))\n")
            python.chmod(0o700)
            script = shlex.split(self.tool_session().rsplit('bash -c ', 1)[1])[0]
            script = script.replace('. /etc/profile', '. ' + shlex.quote(str(profile)))
            result = subprocess.run(['bash', '-c', script], capture_output=True, text=True, check=True, timeout=10,
                env={**os.environ, 'HOME': str(root), 'PATH': str(root) + os.pathsep + os.environ['PATH'],
                     '__NIXOS_SET_ENVIRONMENT_DONE': 'inherited-root'})
            payload = ledger.mapping(cast(object, json.loads(result.stdout)))
            self.assertEqual(payload['initialized'], '1')
            self.assertEqual(payload['tool'], str(tool))
            self.assertEqual(payload['basedpyright'], 'basedpyright fixture-version')

    def test_system_profile_copy_preserves_external_symlink_targets(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            outside = root / 'outside'
            _ = outside.write_text('fixture command')
            source = root / 'source'
            source.mkdir()
            original = source / 'command'
            original.symlink_to(outside)
            destination = root / 'destination'
            destination.mkdir()
            command = shlex.split(next(line for line in self.tool_session().splitlines() if line.startswith('cp ')))
            command[-2] = str(source) + '/.'
            command[-1] = str(destination) + '/'
            _ = subprocess.run(command, check=True, capture_output=True, text=True, timeout=10)
            copied_target = (destination / 'command').readlink()
            self.assertEqual(copied_target, original.readlink())
            self.assertNotIn(str(source), str(copied_target))

    def test_fixed_system_profile_path_resolves_the_tool_python(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tools = root / 'tools'
            tools.mkdir()
            system = root / 'system'
            system.mkdir()
            (tools / 'python3').symlink_to(sys.executable)
            (tools / 'chromium').symlink_to(sys.executable)
            mount = tools / 'mount'
            _ = mount.write_text('#!/bin/sh\ncp -a "$2"/. "$3"/\n')
            mount.chmod(0o700)
            session = self.tool_session()
            setup = session[session.index('ln -sf'):].split('chown -R', 1)[0]
            setup = setup.replace('/run/current-system/sw/bin', str(system)).replace('$box_dir/tool-profile', str(root / 'profile'))
            setup = setup.replace('/bin/bash', str(root / 'bash')).replace('/bin/zsh', str(root / 'zsh'))
            _ = subprocess.run(['bash', '-ec', setup], check=True, capture_output=True, text=True, timeout=10,
                env={**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH']})
            version = subprocess.check_output([str(system / 'python3'), '-c',
                'import json, sys; print(json.dumps(list(sys.version_info[:2])))'], text=True, timeout=10)
            self.assertGreaterEqual(cast(list[int], json.loads(version)), [3, 13])
            self.assertEqual((system / 'python3').resolve(), Path(sys.executable).resolve())
            self.assertEqual((system / 'google-chrome').resolve(), (tools / 'chromium').resolve())
            session = self.tool_session()
            self.assertLess(session.index('$box_dir/tool-profile/google-chrome'), session.index('mount --bind'))

    def test_guest_samples_peak_machine_memory_every_two_seconds(self) -> None:
        for present in (True, False):
            with self.subTest(present=present), tempfile.TemporaryDirectory() as folder:
                meminfo = Path(folder) / 'meminfo'
                def write(available: int) -> None:
                    if present:
                        _ = meminfo.write_text(f'MemTotal: 1000000 kB\nMemAvailable: {available} kB\n')
                write(900000)
                result: dict[str, object] = {'memory_peak_bytes': None}
                namespace: dict[str, object] = {'pathlib': pathlib, 'result': result}
                code = trial.GUEST[trial.GUEST.index('def sample_memory():'):trial.GUEST.index('memory_stop =')]
                code = code.replace("'/proc/meminfo'", repr(str(meminfo)))
                exec(compile(code, '<memory sampler>', 'exec'), namespace)
                waits: list[float] = []
                def pause(seconds: float) -> bool:
                    waits.append(seconds)
                    write(200000 if len(waits) == 1 else 700000)
                    return len(waits) == 3
                stop = threading.Event()
                with mock.patch.object(stop, 'wait', new=pause):
                    cast(Callable[[threading.Event], None], namespace['monitor_memory'])(stop)
                self.assertEqual(waits, [2, 2, 2])
                self.assertEqual(result['memory_peak_bytes'], 800000 * 1024 if present else None)


    def test_guest_uses_whole_machine_without_types_with_timeout_and_counts(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo = root / 'repo'
            suite = repo / 'scripts' / 'fixture'
            suite.mkdir(parents=True)
            shim = repo / 'scripts' / 'lib' / 'py'
            shim.parent.mkdir()
            shim.symlink_to(Path(__file__).resolve().parents[1] / 'lib' / 'py')
            (shim.parent / 'package_root.py').symlink_to(Path(__file__).resolve().parents[1] / 'lib/package_root.py')
            links = {'step': 'size', 'commit': 'fixture-head', 'source': 'https://fixture.invalid/source',
                     'log': 'https://fixture.invalid/log', 'result': 'https://fixture.invalid/result',
                     'module_seconds': 'https://fixture.invalid/module_seconds'}
            _ = (root / 'links.json').write_text(json.dumps({**links, 'module_time_limit_s': 2}))
            _ = (root / 'boot.log').write_text('fixture tool preparation\n')
            _ = (root / 'timeline.log').write_text('fixture timeline\n')
            runner = root / 'module_run.py'
            _ = runner.write_text('import json, os, pathlib, sys, time\n'
                + f'root=pathlib.Path({str(root)!r})\n'
                + "name=sys.argv[2]\n(root/name).write_text(json.dumps({'args':sys.argv[1:], 'env':dict(os.environ)}))\n"
                + "if name=='test_stuck.py': time.sleep(30)\n"
                + "pathlib.Path(os.environ['TEST_TIMES_OUT']).write_text(json.dumps({'wall_s':1, 'cpu_s':1, 'tests_s':1, 'tests_run':3, 'skipped':1, 'failures':1, 'errors':0, 'tests':[]}))\n"
                + "print('fixture failure')\nsys.exit(1)\n")
            types = root / 'basedpyright'
            _ = types.write_text('#!' + sys.executable + '\nimport json, pathlib, sys\n'
                + f'pathlib.Path({str(root / "types-args")!r}).write_text(json.dumps(sys.argv[1:]))\n'
                + "print('0 errors, 0 warnings, 0 notes')\n")
            types.chmod(0o700)
            modules = [('fixture', suite / 'test_stuck.py'), ('fixture', suite / 'test_broken.py')]
            uploads: dict[str, bytes] = {}
            timings = {'fixture/test_stuck.py': 20.0, 'fixture/test_broken.py': 1.0}
            def transport(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
                if arguments[0] == 'curl':
                    self.assertIn(arguments[arguments.index('--max-time') + 1], ('30', '60'))
                    url = next(word for word in arguments if word.startswith('https://'))
                    if '-o' in arguments:
                        content = json.dumps(timings).encode() if url == links['module_seconds'] else b'fixture bundle'
                        _ = Path(arguments[arguments.index('-o') + 1]).write_bytes(content)
                    else:
                        uploads[url] = cast(bytes, options['input'])
                return subprocess.CompletedProcess(arguments, 0, 'version', '')
            code = trial.GUEST.replace("'/proc/meminfo'", repr(str(root / 'meminfo'))).replace("'/var/lib/gate-trial'", repr(str(root)))
            code = code.replace("'/proc/stat'", repr(str(root / 'stat')))
            _ = (root / 'stat').write_text('cpu 100 0 100 10000 1000 0 0 0 9999 9999\n')
            _ = (root / 'meminfo').write_text('MemTotal: 1000000 kB\nMemAvailable: 200000 kB\n')
            original_run = merge_gate.run_suites
            def run_suites(repo: Path, *, full: bool, machine: merge_gate.WholeMachine,
                           module_time_limit_s: float) -> merge_gate.GateRun:
                self.assertEqual(os.environ['HERDR_BIN'], '/nonexistent/herdr')
                self.assertEqual(machine.module_seconds, timings)
                run = original_run(repo, full=full, machine=machine, module_time_limit_s=module_time_limit_s)
                _ = (root / 'stat').write_text('cpu 500 0 100 20000 5000 0 0 400 20000 20000\n')
                return run
            with (mock.patch.dict(os.environ, {'MERGE_GATE_BASEDPYRIGHT': str(types), 'MERGE_GATE_PYTHON': sys.executable,
                                                 'GATE_BOX_DIR': str(root), 'box_dir': str(root)}),
                  mock.patch.object(sys, 'path', list(sys.path)),
                  mock.patch.object(Path, 'home', return_value=root),
                  mock.patch('subprocess.run', side_effect=transport),
                  mock.patch('os.cpu_count', return_value=2),
                  mock.patch.object(merge_gate, 'run_suites', side_effect=run_suites),
                  mock.patch.object(merge_gate, 'module_runner', return_value=runner),
                  mock.patch.object(merge_gate, 'discover_test_modules', return_value=modules) as discover,
                  mock.patch.object(merge_gate, 'BuildHoldWait', side_effect=AssertionError('hold read')),
                  mock.patch.object(merge_gate, 'gate_reservation', side_effect=AssertionError('admission')),
                  mock.patch.object(merge_gate, 'shared_cpu_pool', side_effect=AssertionError('CPU pool')),
                  mock.patch.object(merge_gate, 'suite_scope_prefix', side_effect=AssertionError('scope')),
                  mock.patch.object(merge_gate, 'merge_gate_governor_gib', side_effect=AssertionError('governor')),
                  mock.patch.object(merge_gate, 'gate_state_dir', side_effect=AssertionError('test ledger')),
                  mock.patch.object(merge_gate, 'typecheck_python', side_effect=AssertionError('type preparation during tests-only trial'))):
                previous = Path.cwd()
                try:
                    exec(compile(code, '<guest>', 'exec'), {})
                finally:
                    os.chdir(previous)
            discover.assert_called_once_with(repo)
            payload = ledger.mapping(cast(object, json.loads(uploads[links['result']])))
            self.assertNotIn('governor_gib', payload)
            self.assertEqual(ledger.mapping(payload['tools'])['python_sqlite'], sqlite3.sqlite_version)
            self.assertEqual(payload['module_time_limit_s'], 2)
            self.assertEqual(payload['memory_peak_bytes'], 800000 * 1024)
            self.assertEqual(payload['max_running_jobs'], 2)
            self.assertEqual(payload['vcpus'], 2)
            busy = 400 / os.sysconf('SC_CLK_TCK')
            self.assertEqual(payload['busy_cpu_s'], busy)
            seconds = float(str(payload['gate_finished_at'])) - float(str(payload['gate_started_at']))
            self.assertEqual(payload['gate_s'], seconds)
            self.assertAlmostEqual(float(str(payload['cpu_use_percent'])), 100 * busy / (2 * seconds))
            self.assertEqual(payload['verdict'], 'fail')
            self.assertEqual(payload['tests'], 3)
            self.assertEqual(payload['skips'], 1)
            self.assertEqual(set(cast(list[str], payload['failed_modules'])), {'fixture/test_broken.py', 'fixture/test_stuck.py'})
            measured = {str(row['module']): row for value in cast(list[object], payload['modules']) if (row := ledger.mapping(value))}
            self.assertEqual(len(measured), 2)
            self.assertEqual(measured['fixture/test_broken.py']['status'], 1)
            self.assertNotIn('types/basedpyright', measured)
            self.assertEqual(measured['fixture/test_stuck.py']['stopped'], True)
            self.assertEqual(measured['fixture/test_stuck.py']['status'], -signal.SIGTERM)
            self.assertIn('gate_started_at', payload)
            self.assertIn('gate_finished_at', payload)
            for _, path in modules:
                call = ledger.mapping(cast(object, json.loads((root / path.name).read_text())))
                self.assertEqual(call['args'], [str(suite), path.name])
                environment = ledger.mapping(call['env'])
                for key in ('TEST_TIMES_OUT', 'TMPDIR', 'STAFF_FILE', 'BUILDLOG_DIR', 'BUILDLOG_LIVE_MEMORY_TRIPWIRE'):
                    self.assertIn(key, environment)
                self.assertNotIn('GATE_BOX_DIR', environment)
                self.assertNotIn('box_dir', environment)
            self.assertFalse((root / 'types-args').exists())
            content = uploads[links['log']].decode()
            self.assertLess(content.index('fixture/test_broken.py exit=1'), content.index('fixture/test_stuck.py exit=-15'))
            self.assertIn(' stopped', content)
            self.assertNotIn('types/basedpyright', content)
            self.assertEqual(sum(line.startswith('fixture/test_broken.py exit=') for line in content.splitlines()), 1)
            self.assertEqual(sum(line.startswith('fixture/test_stuck.py exit=') for line in content.splitlines()), 1)
            self.assertIn('fixture tool preparation\n', content)
            self.assertIn('fixture timeline\n', content)
            self.assertIn('UTC: ', content)


@final
class SigningTests(unittest.TestCase):
    def test_aws_published_query_vector_and_put_method(self) -> None:
        # AWS publishes these dummy credentials and the GET signature in its S3 query example.
        credentials = trial.SigningKey('AKIAIOSFODNN7EXAMPLE', 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY')
        at = datetime(2013, 5, 24, tzinfo=UTC)
        get = trial.presign('GET', 'examplebucket.s3.amazonaws.com', 'test.txt', 'us-east-1', credentials, at, 86400)
        self.assertEqual(parse_qs(urlsplit(get).query)['X-Amz-Signature'],
            ['aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404'])
        put = trial.presign('PUT', 'examplebucket.s3.amazonaws.com', 'test.txt', 'us-east-1', credentials, at, 86400)
        # The AWS SDK confirms this PUT vector with the same AWS example inputs.
        self.assertEqual(parse_qs(urlsplit(put).query)['X-Amz-Signature'],
            ['f4db56459304dafaa603a99a23c6bea8821890259a65c18ff503a4a72a80efd9'])


@final
class PairedTrialTests(CostFixture):
    repo: Path = Path()
    origin: Path = Path()
    base: str = ''
    changed: str = ''

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.settings.write_text(self.settings.read_text().replace('region=us-east-1', 'region=us-east-2'))
        _ = self.enterContext(mock.patch.object(trial, 'account_settings_path', return_value=self.settings))
        shipped = Path(__file__).resolve().parents[2]
        _ = (self.root / 'cloud_gate.conf').write_text((shipped / 'config/cloud_gate.conf').read_text())
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        for args in (('init', '-q'), ('config', 'user.name', 'Fixture'), ('config', 'user.email', 'fixture@example.invalid')):
            _ = trial.checked_git(self.repo, *args)
        for relative in ('scripts/test_times/module_run.py', 'scripts/lib/descendants.py', 'scripts/lib/package_root.py'):
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_bytes((shipped / relative).read_bytes())
        (self.repo / 'config').mkdir()
        (self.repo / 'scripts/fixture').mkdir()
        _ = (self.repo / 'scripts/fixture/test_sample.py').write_text('''import json, os, pathlib, unittest
class Sample(unittest.TestCase):
    def test_sample(self):
        self.assertFalse(pathlib.Path('generated').exists())
        pathlib.Path('generated').touch()
        with open(os.environ['PAIRED_TRACE'], 'a') as trace:
            trace.write(json.dumps(dict(pid=os.getpid(), cwd=os.getcwd(), home=os.environ['HOME'],
                                       temporary=os.environ['TMPDIR'], config=os.environ['CONF_DIR'])) + '\\n')
''')
        _ = trial.checked_git(self.repo, 'add', '.')
        _ = trial.checked_git(self.repo, 'commit', '-qm', 'Base fixture')
        self.base = trial.checked_git(self.repo, 'rev-parse', 'HEAD')
        _ = (self.repo / 'change').write_text('changed')
        _ = trial.checked_git(self.repo, 'add', '.')
        _ = trial.checked_git(self.repo, 'commit', '-qm', 'Changed fixture')
        self.changed = trial.checked_git(self.repo, 'rev-parse', 'HEAD')
        self.origin = self.root / 'origin.git'
        _ = subprocess.run(['git', 'clone', '--bare', str(self.repo), str(self.origin)], check=True, capture_output=True)
        path = self.root / 'cloud_gate.conf'
        _ = path.write_text(path.read_text().replace('clone_url=https://github.com/natepiano/claude_commands.git',
                                                   'clone_url=' + str(self.origin)))

    def request(self, modules: tuple[str, ...] = ('fixture/test_sample.py',), rounds: int = 3) -> trial.PairedTrialRequest:
        return trial.pushed_request(self.repo, self.base, self.changed, modules, rounds)

    def sample(self, variant: str, number: int, cpu: float) -> dict[str, object]:
        return {'module': 'fixture/test_sample.py', 'variant': variant, 'round': number, 'cpu_s': cpu,
                'peak_mib': 12.0, 'wall_s': 1.0, 'status': 0, 'tests': 1, 'failures': 0, 'errors': 0,
                'instance_type': 'm7a.8xlarge', 'box': 'i-fixture'}

    def test_pushed_refs_and_modules_do_not_change_checkout(self) -> None:
        before = trial.checked_git(self.repo, 'show-ref')
        request = self.request()
        self.assertEqual(request.base.commit, self.base)
        self.assertEqual(request.changed.commit, self.changed)
        self.assertEqual(trial.checked_git(self.repo, 'show-ref'), before)
        self.assertEqual(trial.checked_git(self.repo, 'status', '--porcelain'), '')
        _ = (self.repo / 'private').write_text('local only')
        _ = trial.checked_git(self.repo, 'add', '.')
        _ = trial.checked_git(self.repo, 'commit', '-qm', 'Local fixture')
        with self.assertRaises((ValueError, subprocess.SubprocessError)):
            _ = trial.pushed_request(self.repo, self.base, 'HEAD', request.modules, 3)

    def test_module_validation_rejects_duplicate_missing_and_traversal(self) -> None:
        for modules in (('fixture/test_sample.py',) * 2, ('fixture/missing.py',), ('../test_sample.py',), ('/fixture/test_sample.py',)):
            with self.subTest(modules=modules), self.assertRaises((ValueError, subprocess.SubprocessError)):
                _ = self.request(modules)
        with self.assertRaises(ValueError):
            _ = self.request(rounds=0)

    def test_paired_median_uses_pairs_and_keeps_partial_failures(self) -> None:
        runs = [self.sample(variant, number, cpu) for number, pair in enumerate(((1, 0), (10, 9), (100, 2)), 1)
                for variant, cpu in zip(('base', 'changed'), pair)]
        summary = trial.module_summary('fixture/test_sample.py', 3, runs)
        self.assertEqual(summary['paired_median_saving_cpu_s'], 1)
        self.assertEqual(summary['base_median_cpu_s'], 10)
        self.assertEqual(summary['changed_median_cpu_s'], 2)
        self.assertTrue(summary['all_passed'])
        runs[0]['status'] = 1
        runs[1]['tests'] = 2
        self.assertFalse(trial.module_summary('fixture/test_sample.py', 3, runs)['all_passed'])
        self.assertFalse(trial.module_summary('fixture/test_sample.py', 3, runs)['same_test_count'])
        partial = trial.module_summary('fixture/test_sample.py', 3, runs[1:])
        self.assertFalse(partial['all_passed'])
        self.assertFalse(partial['same_test_count'])
        self.assertIsNone(trial.module_summary('fixture/test_sample.py', 3, [])['base_median_cpu_s'])

    def test_result_validation_checks_identity_rounds_and_box(self) -> None:
        request = trial.module_request(self.request(), 'fixture/test_sample.py', 'attempt', int(time.time()) + 600)
        row = {**request, 'verdict': 'pass', 'runs': [self.sample('base', 1, 1)]}
        trial.validate_paired_result(row, request, 'i-fixture')
        for key, value in (('attempt', 'other'), ('base', dict[str, object]()), ('rounds', 99)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                trial.validate_paired_result({**row, key: value}, request, 'i-fixture')
        with self.assertRaises(ValueError):
            trial.validate_paired_result({**row, 'runs': [self.sample('base', 1, 1)] * 2}, request, 'i-fixture')
        with self.assertRaises(ValueError):
            trial.validate_paired_result(row, request, 'i-other')

    def guest(self, *, limit: float = 10) -> tuple[dict[str, object], list[dict[str, object]]]:
        from scripts.aws_target import gate_box, cloud_gate
        box = self.root / 'box'
        box.mkdir()
        request = trial.module_request(self.request(), 'fixture/test_sample.py', 'attempt', int(time.time()) + 600)
        request['module_time_limit_s'] = limit
        cloud_gate.save(box / 'request.json', request)
        cloud_gate.save(box / 'links.json', {'production': True, 'step': 'size', 'log': 'log', 'result': 'result',
                                           'instance_type': 'm7a.8xlarge'})
        tools = self.root / 'tools'
        tools.mkdir()
        curl = tools / 'curl'
        _ = curl.write_text(f'''#!{sys.executable}
import os, pathlib, sys
pathlib.Path({str(box)!r}, 'uploaded-' + sys.argv[-1]).write_bytes(sys.stdin.buffer.read())
''')
        curl.chmod(0o755)
        _ = (box / 'guest.py').write_text(gate_box.GUEST)
        trace = self.root / 'trace.jsonl'
        result = subprocess.run([sys.executable, str(box / 'guest.py')], capture_output=True, text=True, timeout=30,
            env={**os.environ, 'GATE_BOX_DIR': str(box), 'PAIRED_TRACE': str(trace),
                 'PATH': str(tools) + os.pathsep + os.environ['PATH']})
        self.assertEqual(result.returncode, 0, result.stderr)
        reply = cloud_gate.read(box / 'uploaded-result')
        traces = [ledger.mapping(cast(object, json.loads(line))) for line in trace.read_text().splitlines()] if trace.exists() else []
        return reply, traces

    def test_local_guest_uses_two_trees_alternating_isolated_processes(self) -> None:
        reply, traces = self.guest()
        runs = ledger.rows(reply, 'runs')
        self.assertEqual(reply['verdict'], 'pass', reply)
        self.assertEqual([row['variant'] for row in runs], ['base', 'changed', 'changed', 'base', 'base', 'changed'])
        self.assertEqual([row['round'] for row in runs], [1, 1, 2, 2, 3, 3])
        self.assertEqual(len({row['pid'] for row in traces}), 8)
        self.assertEqual(len({row['temporary'] for row in traces}), 8)
        self.assertEqual(len({row['home'] for row in traces}), 8)
        self.assertEqual(len({row['cwd'] for row in traces}), 2)
        self.assertTrue(all(row['tests'] == 1 and row['status'] == 0 for row in runs))
        self.assertTrue(all(isinstance(row['cpu_s'], float) and isinstance(row['peak_mib'], float) for row in runs))
        self.assertTrue((self.root / 'box/measure').exists())
        self.assertEqual(set(runs[0]), trial.RUN_FIELDS)

    def test_guest_warms_both_trees_without_result_rows_or_measurements(self) -> None:
        reply, traces = self.guest()
        self.assertEqual(len(traces), 8)
        self.assertEqual([Path(str(row['cwd'])).name for row in traces[:2]], ['base', 'changed'])
        runs = ledger.rows(reply, 'runs')
        self.assertEqual(len(runs), 6)
        self.assertTrue(all(row['round'] in (1, 2, 3) for row in runs))
        log = (self.root / 'box/gate.log').read_text()
        self.assertEqual(sum(line.startswith('{"module":') for line in log.splitlines()), 6)
        summary = trial.module_summary('fixture/test_sample.py', 3, runs)
        self.assertEqual(summary['rounds'], 3)
        self.assertTrue(summary['all_passed'])

    def test_guest_timeout_keeps_runs_with_missing_cpu(self) -> None:
        reply, _ = self.guest(limit=0)
        runs = ledger.rows(reply, 'runs')
        self.assertEqual(len(runs), 6)
        self.assertTrue(all(row['status'] == 124 and row['cpu_s'] is None for row in runs))
        self.assertFalse(trial.module_summary('fixture/test_sample.py', 3, runs)['all_passed'])

    def test_slot_limit_refuses_before_service_or_admin_access(self) -> None:
        with mock.patch.object(subprocess, 'call', side_effect=AssertionError('service start')):
            output = StringIO()
            with redirect_stdout(output):
                status = trial.paired_main(['--base', self.base, '--changed', self.changed,
                    '--modules', *[f'fixture/test_{number}.py' for number in range(10)]], self.state)
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(output.getvalue()), {'total_cost_usd': 0, 'cost_basis': 'estimate', 'cleanup_complete': True})

    def test_terminal_signals_stop_the_owning_service(self) -> None:
        from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            script = '''import signal, sys
from unittest import mock
from scripts.aws_target import gate_trial
with mock.patch.object(gate_trial.subprocess, 'call', side_effect=lambda command: signal.raise_signal(int(sys.argv[1]))), mock.patch.object(gate_trial.subprocess, 'run') as stop:
    assert gate_trial.paired_service(['fixture'], 'fixture-unit') == 1
    stop.assert_called_once_with(['systemctl', '--user', 'stop', 'fixture-unit'], check=False)
'''
            reply = subprocess.run([sys.executable, '-c', script, str(number)], capture_output=True, text=True,
                                   timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(reply.returncode, 0, reply.stderr)

    def test_guest_failed_sample_keeps_measurements_and_counts(self) -> None:
        module = self.repo / 'scripts/fixture/test_sample.py'
        _ = module.write_text(module.read_text().replace("self.assertFalse(pathlib.Path('generated').exists())", "self.fail('fixture failure')"))
        _ = trial.checked_git(self.repo, 'add', '.')
        _ = trial.checked_git(self.repo, 'commit', '-qm', 'Failing fixture')
        self.changed = trial.checked_git(self.repo, 'rev-parse', 'HEAD')
        _ = trial.checked_git(self.repo, 'push', str(self.origin), 'HEAD:refs/heads/failure')
        reply, _ = self.guest()
        runs = ledger.rows(reply, 'runs')
        self.assertEqual(len(runs), 6)
        self.assertEqual([row['status'] for row in runs], [0, 1, 1, 0, 0, 1])
        self.assertTrue(all(row['cpu_s'] is not None and row['tests'] == 1 for row in runs))
        self.assertFalse(trial.module_summary('fixture/test_sample.py', 3, runs)['all_passed'])

    def test_paired_cli_preserves_the_ordered_regional_deployment(self) -> None:
        from scripts.aws_target import cloud_gate
        regions = [{'region': region, 'template': 'fixture-' + region} for region in ('us-east-2', 'us-west-2')]
        cloud_gate.save(self.state / 'gate-deployment.json', {'regions': regions, 'bucket': 'fixture-bucket'})
        with mock.patch.object(trial, 'paired_service', return_value=0):
            self.assertEqual(trial.main(['--base', self.base, '--changed', self.changed, '--repo', str(self.repo),
                '--modules', 'fixture/test_sample.py'], self.settings, self.state), 0)
        path = next((self.state / 'paired-trials').glob('*/*/deployment.json'))
        self.assertEqual(cloud_gate.read(path)['regions'], regions)

    def test_paired_cli_uses_scaler_without_admin_or_new_settings(self) -> None:
        from scripts.aws_target import cloud_gate
        cloud_gate.save(self.state / 'gate-deployment.json', {'template': 'fixture'})
        with (mock.patch.object(admin_secrets.AdminSecretSession, 'get', side_effect=AssertionError('admin read')),
              mock.patch.object(trial, 'paired_service', return_value=0) as service):
            self.assertEqual(trial.main(['--base', self.base, '--changed', self.changed, '--repo', str(self.repo),
                '--modules', 'fixture/test_sample.py'], self.settings, self.state), 0)
        command = cast(list[str], service.call_args.args[0])
        self.assertIn('--property=KillMode=mixed', command)
        self.assertTrue(any(value.startswith('--setenv=AWS_SHARED_CREDENTIALS_FILE=') for value in command))
        paths = list((self.state / 'paired-trials').glob('*/*/request.json'))
        self.assertEqual(len(paths), 1)
        self.assertEqual(cloud_gate.read(paths[0])['rounds'], 3)
