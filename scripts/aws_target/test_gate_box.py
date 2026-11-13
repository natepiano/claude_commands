"""Test the shared guest and the disposable resource definitions."""
from __future__ import annotations

from collections.abc import Callable
import base64
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from typing import Self, cast, override
import unittest
from unittest import mock

from scripts.aws_target import gate_box, gate_trial, ledger
from scripts.aws_target import test_gate_trial as trial_tests
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production import merge_gate as gate
from scripts.test_times import gate_record


ROOT = Path(__file__).resolve().parents[2]


class GateBoxTests(unittest.TestCase):
    def test_paired_guest_uploads_warm_up_round_and_finish_stages_in_run_order(self) -> None:
        fixture = trial_tests.PairedTrialTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        reply, _ = fixture.guest()
        log = (fixture.root / 'box/uploaded-log').read_text()
        lines = [line for line in log.splitlines() if ' stage=' in line]
        self.assertEqual([line.split(' stage=', 1)[1] for line in lines], [
            'source ready', 'warm-up base', 'warm-up changed', 'round 1 of 3 base', 'round 1 of 3 changed',
            'round 2 of 3 changed', 'round 2 of 3 base', 'round 3 of 3 base', 'round 3 of 3 changed', 'finish'])
        for line in lines:
            self.assertRegex(line, r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ stage=')
        runs = ledger.rows(reply, 'runs')
        self.assertEqual(len(runs), 6)
        self.assertTrue(all(set(row) == gate_box.RUN_FIELDS for row in runs))
        self.assertEqual(sum(line.startswith('{"module":') for line in log.splitlines()), 6)

    def test_paired_guest_stage_write_errors_keep_all_runs_and_pass_verdict(self) -> None:
        failure = '''class FailedStageWrite:
    def __enter__(self):
        return self
    def __exit__(self, *error):
        pass
    def write(self, text):
        (root / 'failed-stage-write').write_text(text)
        raise OSError('The stage write failed')

'''
        for name in ('warm-up base', 'finish'):
            with self.subTest(stage=name):
                fixture = trial_tests.PairedTrialTests()
                self.addCleanup(fixture.doCleanups)
                fixture.setUp()
                path = fixture.repo / 'scripts/aws_target/gate_box.py'
                source = path.read_text().replace('def stage(name):', failure + 'def stage(name):')
                source = source.replace("(root / 'boot.log').open('a')",
                    f"(FailedStageWrite() if name == {name!r} else (root / 'boot.log').open('a'))")
                _ = path.write_text(source)
                _ = gate_trial.checked_git(fixture.repo, 'add', '.')
                _ = gate_trial.checked_git(fixture.repo, 'commit', '-qm', 'Fail the candidate stage writer')
                fixture.changed = gate_trial.checked_git(fixture.repo, 'rev-parse', 'HEAD')
                _ = gate_trial.checked_git(fixture.repo, 'push', str(fixture.origin), 'HEAD:refs/heads/stage')
                reply, traces = fixture.guest()
                self.assertIn(' stage=' + name + '\n', (fixture.root / 'box/failed-stage-write').read_text())
                self.assertEqual(len(traces), 8)
                runs = ledger.rows(reply, 'runs')
                self.assertEqual([(row['round'], row['variant']) for row in runs],
                                 [(1, 'base'), (1, 'changed'), (2, 'changed'), (2, 'base'), (3, 'base'), (3, 'changed')])
                self.assertTrue(all(row['status'] == 0 for row in runs))
                self.assertEqual(reply['verdict'], 'pass')
                self.assertNotIn('error', reply)

    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name in ('cloud_gate.conf', 'test_times.conf'):
            _ = (root / name).write_bytes((ROOT / 'config' / name).read_bytes())
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {'CONF_DIR': str(root)})))

    def test_trial_and_route_share_one_guest(self) -> None:
        self.assertIs(gate_trial.GUEST, gate_box.GUEST)
        _ = compile(gate_box.GUEST, '<gate guest>', 'exec')
        self.assertNotIn('basedpyright', gate_box.GUEST)
        self.assertNotIn('merge_gate', gate_box.GUEST)
        self.assertIn('merge_gate.SelectedModules', gate_box.CANDIDATE_GUEST)
        self.assertIn("'HEAD^{tree}'", gate_box.GUEST)
        self.assertIn("'update-ref', 'refs/remotes/origin/main', gate_request['origin_main']", gate_box.GUEST)
        self.assertIn("'fetch', str(root / 'source.bundle'), links['commit']", gate_box.GUEST)

    def test_guest_names_no_merge_gate_signal_function(self) -> None:
        for code in (gate_box.GUEST, gate_box.CANDIDATE_GUEST, gate_box.PAIRED_GUEST):
            self.assertNotIn('rollback_signals', code)
            self.assertNotIn('defer_signals', code)
            self.assertNotRegex(code, r'\bmerge_gate\.\w*signal\w*')
            self.assertNotIn('signal.signal', code)

    def test_boot_timestamps_use_local_kernel_and_cached_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _ = (root / 'links.json').write_text('{}')
            metadata = root / 'metadata.json'
            cloud_init = root / 'instance-data.json'
            stat = root / 'stat'
            _ = stat.write_text('cpu 1 2 3\nbtime 1768438700\n')
            prefix = gate_box.GUEST.split("links['boot'] =", 1)[0].replace(
                "'/proc/stat'", repr(str(stat))).replace(
                "'/etc/ec2-metadata/instance-identity-document'", repr(str(metadata))).replace(
                "'/run/cloud-init/instance-data.json'", repr(str(cloud_init)))
            document = {'pendingTime': '2026-01-15T01:00:00Z'}
            for cached in ('absent', 'identity', 'cloud-init', 'invalid'):
                with self.subTest(cached=cached), mock.patch.dict(os.environ,
                        {'GATE_BOX_DIR': str(root), 'GATE_USER_DATA_STARTED_AT': '1768438801'}):
                    metadata.unlink(missing_ok=True)
                    cloud_init.unlink(missing_ok=True)
                    if cached == 'identity':
                        _ = metadata.write_text(json.dumps(document))
                    elif cached == 'cloud-init':
                        _ = cloud_init.write_text(json.dumps({'ds': {'dynamic': {'instance-identity': {'document': document}}}}))
                    elif cached == 'invalid':
                        _ = metadata.write_text('{broken')
                    namespace: dict[str, object] = {}
                    exec(compile(prefix, '<boot timestamps>', 'exec'), namespace)
                    timestamps = ledger.mapping(namespace['timestamps'])
                    self.assertEqual(timestamps['kernel_boot_at'], 1768438700)
                    self.assertEqual(timestamps['user_data_started_at'], 1768438801)
                    self.assertGreater(float(str(timestamps['nix_ready_at'])), 1768438801)
                    if cached in ('identity', 'cloud-init'):
                        self.assertEqual(timestamps['instance_running_at'], 1768438800)
                    else:
                        self.assertNotIn('instance_running_at', timestamps)

    def test_production_boot_has_absolute_expiry_and_no_type_package(self) -> None:
        links = {'production': 'true', 'gate_time_limit_s': '2.5', 'expires': '1800000000', 'step': 'size',
                 'guest': 'https://fixture.invalid/requests/attempt.guest.py', 'request': 'https://fixture.invalid/requests/attempt.json',
                 'result': 'https://fixture.invalid/artifacts/attempt/result.json',
                 'log': 'https://fixture.invalid/artifacts/attempt/gate.log'}
        script = gate_box.user_data(links, 'fixture-revision', 20, 5, 1800)
        self.assertIn('--on-calendar=@1800000000', script)
        self.assertIn('shutdown -h now', script)
        self.assertIn('while sleep 5;', script)
        self.assertNotIn('aws s3 cp', script)
        self.assertNotIn('instance-id', script)
        self.assertNotIn('#awscli2', script)
        self.assertNotIn('#basedpyright', script)
        self.assertIn('#chromium', script)
        self.assertLessEqual(len(script.encode()), 16384)
        result = subprocess.run(['sh', '-n'], input=script, text=True, capture_output=True,
                                timeout=TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('base64 -d', script)
        self.assertNotIn(gate_box.GUEST, script)
        self.assertIn('curl --fail --silent --show-error --max-time 60 https://fixture.invalid/requests/attempt.guest.py -o "$box_dir/guest.py"', script)
        self.assertIn('curl --fail --silent --show-error --max-time 60 https://fixture.invalid/requests/attempt.json -o "$box_dir/request.json"', script)
        session = shlex.split(script[script.index('nix --extra-experimental-features'):])[-1]
        self.assertLess(session.index('curl --fail'), session.index('exec python3'))

    def test_full_gate_downloads_program_and_request_without_inlining_modules(self) -> None:
        modules = [f'scripts/fixture/test_module_{index:04d}.py' for index in range(740)]
        links = {'production': 'true', 'gate_time_limit_s': '2.5', 'expires': '1800000000', 'step': 'size',
                 'attempt': 'a' * 32, 'commit': 'b' * 40, 'tree': 'c' * 40, 'mode': 'merge-gate',
                 'origin': 'https://example.invalid/team/project.git', 'source': 'https://fixture.invalid/source/' + 'd' * 64 + '.bundle',
                 'guest': 'https://fixture.invalid/requests/attempt.guest.py', 'request': 'https://fixture.invalid/requests/attempt.json',
                 'log': 'https://fixture.invalid/artifacts/attempt/gate.log',
                 'result': 'https://fixture.invalid/artifacts/attempt/result.json'}
        script = gate_box.user_data(links, 'f' * 40, 20, 5, 1800)
        self.assertIn('set -eu\numask 077\n', script)
        self.assertLess(len(base64.b64encode(script.encode())), 25600)
        self.assertNotIn('test_module_', script)
        self.assertNotIn('"modules"', script)
        self.assertNotIn(base64.b64encode(gate_box.GUEST.encode()).decode(), script)
        session = shlex.split(script[script.index('nix --extra-experimental-features'):])[-1]
        downloads = session[:session.index('ln -sf')]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _ = (root / 'source.py').write_text(gate_box.GUEST)
            _ = (root / 'manifest.json').write_text(json.dumps({'modules': modules}))
            curl = root / 'curl'
            _ = curl.write_text('#!/bin/sh\ncase "$6" in *.guest.py) cp "$FIXTURE/source.py" "$8" ;;'
                                + ' *.json) cp "$FIXTURE/manifest.json" "$8" ;; *) exit 1 ;; esac\n')
            curl.chmod(0o700)
            result = subprocess.run(['sh', '-ec', 'umask 077\n' + downloads + 'mkdir "$box_dir/session-directory"\n'],
                env={**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH'], 'FIXTURE': str(root), 'box_dir': str(root)},
                text=True, capture_output=True, timeout=TEST_HANG_GUARD_SECS, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'session-directory').stat().st_mode & 0o777, 0o755)
            self.assertEqual((root / 'guest.py').read_text(), gate_box.GUEST)
            self.assertEqual(json.loads((root / 'request.json').read_text()), {'modules': modules})

    def test_production_boot_stages_and_errors_with_local_stubs(self) -> None:
        self.production_boot_stubs(('', 'nix', 'tools shell', 'tools ready', 'source ready', 'tests start'))

    def test_candidate_renamed_suite_runner_uses_its_own_entry_point(self) -> None:
        self.production_boot_stubs(('',), candidate_change='rename')

    def test_candidate_without_entry_point_publishes_named_refusal(self) -> None:
        self.production_boot_stubs(('',), candidate_change='missing')

    def test_bootstrap_renders_with_mock_process_memory_mib(self) -> None:
        code = '''from unittest import mock
from scripts.production import merge_gate
with mock.patch.object(merge_gate, 'process_memory_mib'):
    from scripts.aws_target import gate_box
    compile(gate_box.GUEST, '<bootstrap>', 'exec')
    assert 'merge_gate' not in gate_box.GUEST
    assert 'inspect.getsource' not in gate_box.GUEST
'''
        reply = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True,
                               timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(reply.returncode, 0, reply.stdout + reply.stderr)

    def production_boot_stubs(self, failures: tuple[str, ...], *, candidate_change: str = '') -> None:
        stages = ['boot', 'tools ready', 'tools checked', 'source ready', 'tests start']
        for failure in failures:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                tools, box, repo = root / 'bin', root / 'box', root / 'repo'
                tools.mkdir()
                (root / 'home').mkdir()
                for name in ('scripts', 'scripts/production', 'scripts/aws_target', 'scripts/lib', 'config'):
                    folder = repo / name
                    folder.mkdir(parents=True, exist_ok=True)
                    if name != 'config':
                        _ = (folder / '__init__.py').write_text('')
                _ = (repo / 'scripts/lib/cancellation.py').write_bytes((ROOT / 'scripts/lib/cancellation.py').read_bytes())
                _ = (repo / 'scripts/production/merge_gate.py').write_text(
                    'from types import SimpleNamespace\nimport os\n'
                    + 'WholeMachine = lambda times: times\nSelectedModules = tuple\nSuiteMode = str\n'
                    + 'gate_record = SimpleNamespace(GateTimeLimit=lambda seconds: seconds, NoGateTimeLimit=lambda reason: reason)\n'
                    + 'def run_suites(*args, **kwargs):\n'
                    + '    if os.environ["FAIL_STAGE"] == "tests start": raise RuntimeError("fixture tests start failure")\n'
                    + '    with open(os.environ["FIXTURE"] + "/received-limit", "w") as output: output.write(str(kwargs["time_limit"]))\n'
                    + '    return SimpleNamespace(passed=True, jobs=(), seconds=1, peak_memory_mib=0)\n')
                _ = (repo / 'scripts/aws_target/gate_box.py').write_text(
                    (ROOT / 'scripts/aws_target/gate_box.py').read_text()
                    + '\ndef encode_run(run): return {"passed": run.passed}\n'
                    + 'def completion_record(*args): return {"kind": "gate_run"}\n')
                if candidate_change == 'rename':
                    for relative in ('scripts/aws_target/gate_box.py', 'scripts/production/merge_gate.py'):
                        path = repo / relative
                        _ = path.write_text(path.read_text().replace('run_suites', 'candidate_suites'))
                elif candidate_change == 'missing':
                    path = repo / 'scripts/aws_target/gate_box.py'
                    _ = path.write_text(path.read_text().replace('def run_guest(', 'def old_guest('))
                _ = (root / 'source.py').write_text(gate_box.GUEST)
                _ = (root / 'request.json').write_text(json.dumps({
                    'clone_url': 'https://example.invalid/repo.git', 'origin_main': 'b' * 40, 'modules': [],
                    'head': 'e' * 40, 'dirty_paths': ['scripts/fixture/test_one.py']}))
                stub = '#!' + sys.executable + '\n' + r'''import json, os, pathlib, shutil, subprocess, sys
root = pathlib.Path(os.environ['FIXTURE'])
name, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
def event(value):
    with (root / 'trace').open('a') as output: output.write(value + '\n')
def fail(stage):
    marker = root / 'failed'
    if os.environ['FAIL_STAGE'] == stage and not marker.exists():
        marker.touch()
        sys.exit(7)
event(name)
if name == 'nix':
    command = args[args.index('--command') + 1:]
    event('tools shell')
    if os.environ['FAIL_STAGE'] == 'nix': sys.exit(7)
    fail('tools shell')
    sys.exit(subprocess.call(command))
elif name == 'curl':
    destination = next(word for word in args if word.startswith('https://'))
    if '-o' in args:
        fail('tools ready')
        shutil.copyfile(root / ('source.py' if destination.endswith('.guest.py') else 'request.json'), args[args.index('-o') + 1])
    else:
        source = args[args.index('--data-binary') + 1]
        body = sys.stdin.read() if source == '@-' else pathlib.Path(source[1:]).read_text()
        event('upload result' if destination.endswith(('result.json', 'boot-error.json')) else 'upload log')
        with (root / 'uploads').open('a') as output:
            output.write(json.dumps({'destination': destination, 'body': body}) + '\n')
elif name == 'runuser':
    command = args[args.index('--') + 1:]
    command = ['HOME=' + str(root / 'home') if word.startswith('HOME=') else word for word in command]
    sys.exit(subprocess.call(command))
elif name == 'id':
    if not (root / 'user').exists(): sys.exit(1)
    print('1000')
elif name == 'useradd':
    (root / 'user').touch()
elif name == 'git' and args[0] == 'clone':
    fail('source ready')
    print('Cloning the fixture repository.')
    shutil.copytree(root / 'repo', args[-1])
elif name == 'git' and 'fetch' in args:
    event('fetch ' + args[-1])
elif name == 'git' and 'rev-parse' in args:
    print('c' * 40)
elif name in ('systemd-run', 'loginctl', 'mount', 'chown', 'ln', 'cp', 'shutdown', 'git'):
    pass
else:
    print('fixture-version')
'''
                for name in ('nix', 'runuser', 'systemd-run', 'loginctl', 'useradd', 'mount', 'curl',
                             'id', 'git', 'chown', 'ln', 'cp', 'shutdown', 'cargo', 'rustc', 'rustdoc', 'rustup',
                             'cargo-nextest', 'uv', 'sqlite3'):
                    command = tools / name
                    _ = command.write_text(stub)
                    command.chmod(0o700)
                (tools / 'python3').symlink_to(sys.executable)
                links = {'production': 'true', 'gate_time_limit_s': '2.5', 'expires': '1800000000', 'step': 'size', 'region': 'us-west-2',
                         'attempt': 'attempt', 'commit': 'a' * 40, 'tree': 'c' * 40, 'mode': 'merge-gate',
                         'origin': 'https://example.invalid/repo.git', 'guest': 'https://fixture.invalid/requests/attempt.guest.py',
                         'source': 'https://fixture.invalid/source/snapshot.bundle',
                         'request': 'https://fixture.invalid/requests/attempt.json',
                         'log': 'https://fixture.invalid/artifacts/attempt/gate.log',
                         'result': 'https://fixture.invalid/artifacts/attempt/result.json',
                         'boot_error': 'https://fixture.invalid/artifacts/attempt/boot-error.json'}
                script = gate_box.user_data(links, 'f' * 40, 20, 5, 1800)
                self.assertLess(script.index('push_logs &'), script.rindex('nix --extra-experimental-features'))
                self.assertNotIn('#awscli2', script)
                self.assertNotIn('instance-id', script)
                reply = subprocess.run(['bash', '-c', script], text=True, capture_output=True, check=False,
                    timeout=TEST_HANG_GUARD_SECS, cwd=root, env={**os.environ, 'FIXTURE': str(root),
                    'GATE_BOX_DIR': str(box), 'FAIL_STAGE': failure, 'PATH': str(tools) + os.pathsep + os.environ['PATH']})
                log = (box / 'boot.log').read_text()
                uploads = [ledger.mapping(cast(object, json.loads(line))) for line in (root / 'uploads').read_text().splitlines()]
                results = [ledger.mapping(cast(object, json.loads(ledger.text(row, 'body'))))
                           for row in uploads if ledger.text(row, 'destination').endswith(('/result.json', '/boot-error.json'))]
                self.assertTrue(results, log + reply.stderr)
                self.assertEqual(results[-1]['verdict'], 'error' if failure or candidate_change == 'missing' else 'pass', log)
                if candidate_change == 'missing':
                    self.assertEqual(results[-1]['error'], 'candidate predates guest entry point')
                    refusals = [row for row in uploads if row['destination'] == links['boot_error']]
                    self.assertEqual(len(refusals), 1)
                    self.assertEqual(json.loads(ledger.text(refusals[0], 'body'))['error'],
                                     'candidate predates guest entry point')
                    self.assertNotIn('stage=tests start', log)
                if failure:
                    self.assertIn('error', results[-1])
                if failure == 'nix':
                    self.assertIn('stage=boot', ledger.text(uploads[0], 'body'))
                    self.assertEqual(ledger.text(uploads[-1], 'destination'), links['boot_error'])
                if not failure and candidate_change != 'missing':
                    self.assertEqual((root / 'received-limit').read_text(), '2.5')
                    self.assertEqual(reply.returncode, 0, log + reply.stderr)
                    result = results[-1]
                    self.assertGreaterEqual(float(str(result['nix_ready_at'])), int(str(result['user_data_started_at'])))
                    self.assertGreater(int(str(result['user_data_started_at'])), int(str(result['kernel_boot_at'])))
                    self.assertNotIn('source_ready_at', result)
                    self.assertEqual(list(ledger.mapping(results[-1]['tools'])),
                                     ['git', 'python3', 'cargo', 'rustc', 'rustdoc', 'rustup',
                                      'cargo-nextest', 'uv', 'sqlite3', 'python_sqlite'])
                    lines = [line for line in log.splitlines() if ' stage=' in line]
                    self.assertEqual([line.split(' stage=', 1)[1] for line in lines], stages)
                    for line in lines:
                        self.assertRegex(line, r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ stage=')
                    trace = (root / 'trace').read_text().splitlines()
                    self.assertIn('fetch ' + links['commit'], trace)
                    record = ledger.mapping(results[-1]['record'])
                    self.assertEqual(record['head'], 'e' * 40)
                    self.assertTrue(record['dirty'])
                    self.assertEqual(record['dirty_paths'], ['scripts/fixture/test_one.py'])
                    self.assertEqual(record['dirty_path_count'], 1)
                    self.assertLess(trace.index('upload log'), trace.index('tools shell'))
                    print('Production boot stages: ' + ' -> '.join(stages))

    def test_result_round_trip_preserves_reruns_and_test_failures(self) -> None:
        first = gate.GateJobResult('fixture', 'test_one.py', 1, 2, 'failed', cpu_seconds=1, peak_mib=3,
                                   wall_clock=gate_record.WallClockSpan(10, 12))
        second = gate.GateJobResult('fixture', 'test_one.py', 0, 1, 'passed', wall_clock=gate_record.WallClockSpan(13, 14))
        run = gate.GateRun((second,), 4, 3, executions=(first, second))
        self.assertEqual(gate_box.decode_run(gate_box.encode_run(run)), run)
        self.assertEqual([job.output for job in gate_box.execution_records(gate_box.decode_run(gate_box.encode_run(run)))],
                         ['failed', 'passed'])
        executions = gate_box.execution_records(gate_box.decode_run(gate_box.encode_run(run)))
        self.assertEqual([job.wall_clock for job in executions],
                         [gate_record.WallClockSpan(10, 12), gate_record.WallClockSpan(13, 14)])
        record = gate_box.completion_record(run, 'attempt', 'commit', 'tree', 'remote-host', 9, 15, 'merge-gate')
        self.assertEqual((record['kind'], record['host'], record['status']), ('gate_run', 'remote-host', 0))
        self.assertEqual(len(ledger.rows(record, 'modules')), 2)
        self.assertEqual([(row['started_at'], row['ended_at']) for row in ledger.rows(record, 'modules')],
                         [('1970-01-01T00:00:10.000Z', '1970-01-01T00:00:12.000Z'),
                          ('1970-01-01T00:00:13.000Z', '1970-01-01T00:00:14.000Z')])
        with self.subTest(recorded_times=False):
            legacy = gate_box.encode_run(run)
            for key in ('jobs', 'executions'):
                for job in ledger.rows(legacy, key):
                    self.assertNotIn('wall_clock', job)
                    del job['started_at']
                    del job['ended_at']
            decoded = gate_box.decode_run(legacy)
            absent = gate_record.WallClockNotRecorded('box result has no module times')
            self.assertEqual([job.wall_clock for job in decoded.jobs], [absent])
            self.assertEqual([job.wall_clock for job in gate_box.execution_records(decoded)], [absent, absent])
            self.assertEqual(gate_box.decode_run(gate_box.encode_run(decoded)), decoded)
            legacy_record = gate_box.completion_record(
                decoded, 'legacy', 'commit', 'tree', 'remote-host', 9, 15, 'merge-gate')
            for module in ledger.rows(legacy_record, 'modules'):
                self.assertNotIn('started_at', module)
                self.assertNotIn('ended_at', module)
        for finished in (first, second):
            for missing in ('started_at', 'ended_at'):
                with self.subTest(missing=missing, status=finished.returncode):
                    partial = gate_box.encode_run(gate.GateRun((finished,), finished.seconds, finished.peak_mib))
                    del ledger.rows(partial, 'jobs')[0][missing]
                    decoded = gate_box.decode_run(partial)
                    absent = gate_record.WallClockNotRecorded('box result has only one module time')
                    self.assertEqual(decoded.jobs[0].wall_clock, absent)
                    self.assertEqual(decoded.jobs[0].returncode, finished.returncode)
                    self.assertEqual(decoded.passed, finished.returncode == 0)
                    partial_record = gate_box.completion_record(
                        decoded, 'partial', 'commit', 'tree', 'remote-host', 9, 15, 'merge-gate')
                    self.assertEqual(partial_record['status'], finished.returncode)
                    for module in ledger.rows(partial_record, 'modules'):
                        self.assertNotIn('started_at', module)
                        self.assertNotIn('ended_at', module)

    def test_result_rejects_missing_or_invalid_status_and_metrics(self) -> None:
        run = gate.GateRun((gate.GateJobResult('fixture', 'test_one.py', 0, 1, ''),), 1, 1)
        for field, value in (('returncode', False), ('seconds', -1), ('seconds', float('nan')),
                             ('kind', 'UNKNOWN'), ('restart', 'other'),
                             ('started_at', -1), ('started_at', float('nan')),
                             ('ended_at', -1), ('ended_at', float('nan'))):
            with self.subTest(field=field, value=value):
                row = gate_box.encode_run(run)
                job = ledger.rows(row, 'jobs')[0]
                job[field] = value
                with self.assertRaises((ValueError, KeyError)):
                    _ = gate_box.decode_run(row)
        with self.assertRaises(KeyError):
            _ = gate_box.decode_run({})


class GateResourcesTests(unittest.TestCase):
    def template(self) -> dict[str, object]:
        return ledger.mapping(cast(object, json.loads((ROOT / 'docs/plans/builds-aws/cloudformation.json').read_text())))

    def resources(self) -> dict[str, object]:
        return ledger.mapping(self.template()['Resources'])

    def test_rust_resources_remain_identical(self) -> None:
        original = subprocess.run(['git', '-C', str(ROOT), 'show', 'HEAD:docs/plans/builds-aws/cloudformation.json'],
                                  check=True, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        before = ledger.mapping(cast(object, json.loads(original.stdout)))
        current = self.template()
        for section in ('Resources', 'Parameters', 'Outputs'):
            for name, row in ledger.mapping(before[section]).items():
                if name.startswith("Gate") or name.startswith("EnableGates"):
                    continue
                self.assertEqual(ledger.mapping(current[section])[name], row, (section, name))

    def test_cloud_resources_use_the_machine_name(self) -> None:
        names = {'GateBucket': 'BucketName', 'GateRole': 'RoleName', 'GateProfile': 'InstanceProfileName',
                 'GateSecurityGroup': 'GroupName', 'GateLaunchTemplate': 'LaunchTemplateName',
                 'GateExpiryRole': 'RoleName', 'GateExpiryFunction': 'FunctionName', 'GateExpirySchedule': 'Name',
                 'GateExpiryLog': 'LogGroupName'}
        for name, field in names.items():
            with self.subTest(resource=name):
                properties = ledger.mapping(ledger.mapping(self.resources()[name])['Properties'])
                suffix = '-expiry' if name in ('GateExpiryRole', 'GateExpiryFunction', 'GateExpirySchedule', 'GateExpiryLog') else ''
                prefix = '/aws/lambda/' if name == 'GateExpiryLog' else ''
                self.assertEqual(properties[field], {'Fn::Sub': prefix + 'cloud-claude-gate-${DeploymentId}' + suffix})
                if name in ('GateProfile', 'GateExpirySchedule'):
                    continue
                tags = (ledger.rows(properties, 'Tags') if name != 'GateLaunchTemplate' else
                        ledger.rows(ledger.rows(properties, 'TagSpecifications')[0], 'Tags'))
                self.assertIn({'Key': 'Workload', 'Value': 'cloud-claude-gate'}, tags)
                self.assertIn({'Key': 'Name', 'Value': 'cloud-claude-gate'}, tags)

    def test_launch_template_terminates_spot_and_deletes_its_root(self) -> None:
        resource = ledger.mapping(self.resources()['GateLaunchTemplate'])
        data = ledger.mapping(ledger.mapping(resource['Properties'])['LaunchTemplateData'])
        market = ledger.mapping(data['InstanceMarketOptions'])
        self.assertEqual(market['MarketType'], 'spot')
        options = ledger.mapping(market['SpotOptions'])
        self.assertEqual(options['SpotInstanceType'], 'one-time')
        self.assertEqual(options['InstanceInterruptionBehavior'], 'terminate')
        self.assertEqual(data['InstanceInitiatedShutdownBehavior'], 'terminate')
        self.assertIs(ledger.mapping(ledger.rows(data, 'BlockDeviceMappings')[0]['Ebs'])['DeleteOnTermination'], True)
        bucket = ledger.mapping(ledger.mapping(self.resources()['GateBucket'])['Properties'])
        rules = ledger.rows(ledger.mapping(bucket['LifecycleConfiguration']), 'Rules')
        self.assertEqual(rules[0]['ExpirationInDays'], {'Ref': 'GateArtifactDays'})

    def test_scaler_grants_have_workload_and_resource_scopes(self) -> None:
        properties = ledger.mapping(ledger.mapping(self.resources()['GateScalerPolicy'])['Properties'])
        statements = ledger.rows(ledger.mapping(properties['PolicyDocument']), 'Statement')
        by_action = {str(row['Action']): row for row in statements}
        self.assertEqual(properties['Users'], [{'Ref': 'ScalerUser'}])
        terminate = by_action[str(['ec2:TerminateInstances'])]
        self.assertEqual(ledger.mapping(ledger.mapping(terminate['Condition'])['StringEquals']), {
            'ec2:ResourceTag/GateDeployment': {'Ref': 'DeploymentId'}, 'ec2:ResourceTag/Workload': 'cloud-claude-gate'})
        role = by_action[str(['iam:PassRole'])]
        self.assertEqual(role['Resource'], {'Fn::GetAtt': ['GateRole', 'Arn']})
        self.assertEqual(role['Condition'], {'StringEquals': {'iam:PassedToService': 'ec2.amazonaws.com'}})
        for row in statements:
            if row['Resource'] == '*':
                self.assertIn(row['Action'], (['ec2:DescribeInstances'], ['ce:GetCostAndUsage']))
        launches = [row for row in statements if row['Action'] == ['ec2:RunInstances']]
        self.assertEqual(len(launches), 5)
        instance = next(row for row in launches if 'instance/*' in json.dumps(row['Resource']))
        condition = ledger.mapping(instance['Condition'])
        self.assertEqual(ledger.mapping(condition['StringEquals'])['ec2:InstanceType'], {'Ref': 'GateInstanceTypes'})
        self.assertEqual(ledger.mapping(condition['StringEquals'])['ec2:InstanceMarketType'], 'spot')
        self.assertIn('ec2:InstanceProfile', ledger.mapping(condition['ArnEquals']))
        self.assertIn({'Fn::Sub': '${GateBucket.Arn}/artifacts/*'},
                      ledger.rows(by_action[str(['s3:PutObject'])], 'Resource'))
        self.assertEqual(ledger.rows(by_action[str(['s3:GetObject'])], 'Resource'), (
            {'Fn::Sub': '${GateBucket.Arn}/artifacts/*'}, {'Fn::Sub': '${GateBucket.Arn}/source/*'},
            {'Fn::Sub': '${GateBucket.Arn}/requests/*'}))

    def test_guest_role_has_no_object_permissions(self) -> None:
        role = ledger.mapping(ledger.mapping(self.resources()['GateRole'])['Properties'])
        self.assertNotIn('Policies', role)
        self.assertEqual(ledger.mapping(self.template()['Outputs'])['GateRoleId'],
                         {'Condition': 'Gates', 'Value': {'Fn::GetAtt': ['GateRole', 'RoleId']}})

    def test_expiry_function_finds_all_pages_and_terminates_only_expired_instances(self) -> None:
        properties = ledger.mapping(ledger.mapping(self.resources()['GateExpiryFunction'])['Properties'])
        code = ledger.text(ledger.mapping(properties['Code']), 'ZipFile')
        expired = {'InstanceId': 'expired', 'Tags': [{'Key': 'ExpiresAt', 'Value': '99'}]}
        live = {'InstanceId': 'live', 'Tags': [{'Key': 'ExpiresAt', 'Value': '101'}]}
        invalid = {'InstanceId': 'invalid', 'Tags': [{'Key': 'ExpiresAt', 'Value': 'invalid'}]}
        class ExpiryClient:
            operation: str = ''
            filters: list[dict[str, object]] = []
            terminated: list[list[str]] = []

            def get_paginator(self, operation: str) -> Self:
                self.operation = operation
                return self

            def paginate(self, *, Filters: list[dict[str, object]]) -> list[dict[str, object]]:
                self.filters = Filters
                return [{'Reservations': [{'Instances': [live]}]},
                        {'Reservations': [{'Instances': [expired, invalid]}]}]

            def terminate_instances(self, *, InstanceIds: list[str]) -> None:
                self.terminated = [*self.terminated, InstanceIds]

        client = ExpiryClient()

        def aws_client(service: str) -> ExpiryClient:
            self.assertEqual(service, 'ec2')
            return client
        namespace: dict[str, object] = {}
        with (mock.patch.dict('sys.modules', {'boto3': SimpleNamespace(client=aws_client)}),
              mock.patch.dict(os.environ, {'DEPLOYMENT': 'fixture'}), mock.patch('time.time', return_value=100)):
            exec(compile(code, '<expiry>', 'exec'), namespace)
            cast(Callable[[object, object], None], namespace['handler'])({}, None)
        self.assertEqual(client.terminated, [['expired']])
        self.assertEqual(client.operation, 'describe_instances')
        filters = client.filters
        self.assertIn({'Name': 'tag:GateDeployment', 'Values': ['fixture']}, filters)
        self.assertIn({'Name': 'tag:Workload', 'Values': ['cloud-claude-gate']}, filters)
        schedule = ledger.mapping(ledger.mapping(self.resources()['GateExpirySchedule'])['Properties'])
        self.assertIn('rate(1 minute)', json.dumps(schedule))
