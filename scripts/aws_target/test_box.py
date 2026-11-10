"""Exercise box deployment with local executable fakes and an in-memory tailnet."""
from __future__ import annotations

from base64 import b64encode
from collections.abc import Mapping, Sequence
from contextlib import redirect_stdout
from email.message import Message
from io import BytesIO, StringIO
import json
import os
import shutil
import shlex
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from types import ModuleType, SimpleNamespace
from typing import cast, final, override
from unittest import mock
from urllib.error import HTTPError, URLError

from scripts.aws_target import admin_secrets, aws_target, box, deploy, routing_proof
from scripts.aws_target.ledger import mapping
from scripts.aws_target.aws_target import AwsUnavailable
from scripts.build_targets import target_config
from scripts.mac_test import mac_test, offload
from scripts.lib import conf, copy_record, process_start, scratch
from scripts.production.cli_fixture import invoke_cli, process_boundary, process_boundary_for_case

SYSTEM = '/nix/store/' + 'a' * 32 + '-nixos-system-aws-build'
IMAGE_TAGS = [{'Key': box.IMAGE_OWNER_TAG, 'Value': 'true'}]


def write_aws_claim(directory: Path, *, kept: bool, remote: bool = True) -> Path:
    owner = process_start.process_start(os.getpid())
    assert isinstance(owner, process_start.Started)
    record: mac_test.ProcessBoundClaim = {
        'pid': os.getpid(), 'proc_start': 'ended-owner' if kept else owner.start,
        'what': 'fixture tests', 'worktree': str(directory), 'since': '2026-11-06T09:00:00+00:00',
        'repo_name': 'fixture', 'repo_key': 'fixture',
    }
    path = directory / 'claims/job.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(json.dumps({**record, **({'remote_host': 'aws-build'} if remote else {})}))
    return path

FAKE = r'''import json, os, pathlib, shlex, subprocess, sys
root = pathlib.Path(os.environ['BOX_FIXTURE'])
path = root / 'live.json'
s = json.loads(path.read_text())
args = sys.argv[1:]
name = pathlib.Path(sys.argv[0]).name
with (root / 'commands').open('a') as log:
    log.write(json.dumps([name, *args]) + '\n')
def save(): path.write_text(json.dumps(s))
def output(value): print(json.dumps(value))
def filtered(rows):
    filters = json.loads(args[args.index('--filters') + 1])
    return [row for row in rows if all(any(tag['Key'] == f['Name'][4:] and tag['Value'] in f['Values']
            for tag in row.get('Tags', [])) for f in filters)]
def versions(command, remote=False):
    if 'rustc' in command: print(s.get('local_rustc', 'rustc fixture') if not remote else 'rustc fixture')
    elif 'nextest' in command: print(s.get('local_nextest', 'cargo-nextest fixture\n  release fixture  ') if not remote else 'cargo-nextest wrong' if s.get('mismatch') else 'cargo-nextest fixture\n  release fixture  ')
    elif 'mend' in command: print('mend wrong' if remote and s.get('mend_mismatch') else 'mend fixture')
    else: raise AssertionError(command)
if name == 'aws':
    op = args[args.index('--region') + 3]
    if op == 'describe-instances':
        s['observations'] = s.get('observations', 0) + 1
        if s.get('self_stop') and s['observations'] >= s.get('stop_after', 2): s['ec2'] = 'stopped'
        save()
        output({'Reservations': [{'Instances': [{'InstanceId': 'i-box', 'State': {'Name': s.get('ec2', 'running')}, 'LaunchTime': '2026-11-04T00:00:00Z'}]}]})
    elif op == 'stop-instances':
        s['ec2'] = 'stopped'; s['forced_stop'] = True; save(); output({})
    elif op == 'create-image':
        if s.get('image_failure'):
            print('fixture image failure', file=sys.stderr); sys.exit(1)
        s['image_reads'] = 0; s['image_created'] = True; save(); output({'ImageId': 'ami-cache'})
    elif op == 'describe-images':
        if s.get('image_missing'):
            print('InvalidAMIID.NotFound', file=sys.stderr); sys.exit(1)
        current = {'ImageId': 'ami-cache', 'State': s.get('image_state', 'available'),
                   'Tags': [{'Key': 'aws-build-image', 'Value': 'true'}],
                   'BlockDeviceMappings': [{'DeviceName': '/dev/xvda', 'Ebs': {'SnapshotId': 'snap-cache'}}]}
        if s.get('image_state') == 'failed': current['StateReason'] = {'Message': 'snapshot failed'}
        if '--image-ids' in args:
            s['image_reads'] = s.get('image_reads', 0) + 1; save()
            if s.get('snapshot_delayed') and s['image_reads'] == 1: current['BlockDeviceMappings'] = []
            if s.get('image_state') == 'pending' and s['image_reads'] > 1: current['State'] = 'available'
            output({'Images': [current]})
        else:
            output({'Images': filtered([current, *s.get('old_images', [])])})
    elif op == 'describe-snapshots':
        output({'Snapshots': filtered([{'SnapshotId': 'snap-cache', 'Tags': [
            {'Key': 'aws-build-image', 'Value': 'true'}]}, *s.get('old_snapshots', [])])})
    elif op == 'deregister-image':
        if s.pop('deregister_failure', False):
            save(); print('fixture deregister failure', file=sys.stderr); sys.exit(1)
        image = args[args.index('--image-id') + 1]
        s['old_images'] = [row for row in s.get('old_images', []) if row['ImageId'] != image]
        save(); output({})
    elif op == 'delete-snapshot':
        if s.pop('snapshot_failure', False):
            save(); print('fixture snapshot failure', file=sys.stderr); sys.exit(1)
        snapshot = args[args.index('--snapshot-id') + 1]
        s['old_snapshots'] = [row for row in s.get('old_snapshots', []) if row['SnapshotId'] != snapshot]
        save(); output({})
    elif op == 'describe-instance-information':
        output({'InstanceInformationList': [{'InstanceId': 'i-box', 'PingStatus': 'Online'}] if s['online'] else []})
    elif op == 'send-command':
        s['ssm_command'] = json.loads(args[args.index('--parameters') + 1])['commands'][0]
        save(); output({'Command': {'CommandId': 'command-box'}})
    elif op == 'get-command-invocation':
        if 'keep=/home/.aws-build-image-keep' in s.get('ssm_command', ''):
            if 'mkdir -p -m 0700' in s['ssm_command']: s['keep'] = True
            else:
                if s.pop('restore_failure', False):
                    save(); output({'Status': 'Failed', 'StandardErrorContent': 'fixture restore failure'}); sys.exit(0)
                s['keep'] = False
            save(); output({'Status': 'Success', 'StandardOutputContent': 'done'}); sys.exit(0)
        output({'Status': 'Success', 'StandardOutputContent': json.dumps({
            'running': s['joined'], 'ssh': s['joined'], 'tags': ['tag:build-aws'] if s['joined'] else [],
            'addresses': ['100.64.0.3'], 'id': 'node-box'})})
    elif op == 'start-session':
        print('AWS_JOIN_READY', flush=True)
        key = sys.stdin.readline().strip()
        s['received_key'] = key == 'tskey-auth-fixture'
        s['joined'] = not s.get('join_failure')
        save()
        if not s['joined']: sys.exit(1)
        print('AWS_JOIN_DONE')
    else: raise AssertionError(args)
elif name == 'systemctl':
    if 'enable' in args:
        if s.get('activation_failure'): sys.exit(1)
        s['controller'] = True; save()
    elif 'is-enabled' in args: print('enabled' if s.get('controller') else 'disabled')
    elif 'is-active' in args: print('active' if s.get('controller') else 'inactive')
elif name in ('rustc', 'cargo', 'cargo-nextest', 'cargo-mend'):
    if name == 'cargo' and args[:1] == ['metadata']:
        output({'workspace_root': str(pathlib.Path.cwd()), 'target_directory': str(pathlib.Path.cwd() / 'target')})
    else:
        versions([name, *args])
elif name == 'bash':
    words = shlex.split(args[-1])
    source = pathlib.Path(words[words.index('cd') + 1])
    assert source.parent == pathlib.Path(os.environ['SCRATCH_ROOT'])
    record = json.loads((source / '.scratch.json').read_text())
    assert record['task'] == 'gate:aws-routing-proof'
    assert record['generated_source'] is True
    s.setdefault('proof_tasks', []).append(record['task']); save()
    assert 'name = "aws-routing-proof"' in (source / 'Cargo.toml').read_text()
    assert not any(word.startswith('DOC_REPO_TOP=') for word in words)
    assert subprocess.check_output([os.environ['BOX_REAL_GIT'], '-C', str(source), 'status', '--porcelain']) == b''
    assert subprocess.check_output([os.environ['BOX_REAL_GIT'], '-C', str(source), 'show', 'HEAD:Cargo.toml']) == (source / 'Cargo.toml').read_bytes()
    assert words[words.index('test') + 1] == 'aws-routing-proof'
    if s.get('proof_failure'): sys.exit(1)
    if not s.get('missing_record'):
        log = pathlib.Path(os.environ['BUILDLOG_DIR']) / 'aws-build' / '2026-11.jsonl'
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open('a') as handle: handle.write(json.dumps({'kind': 'step', 'id': 'proof-step', 'target': 'aws', 'host': 'aws-build', 'package': 'aws-routing-proof', 'status': 0, 'pinned': True}) + '\n')
elif name == 'tailscale':
    output({'Self': {'HostName': 'natedev', 'DNSName': 'natedev.tail21e383.ts.net.', 'TailscaleIPs': ['100.64.0.1']},
            'Peer': {'mac': {'HostName': "Nate's MacBook Pro 2023", 'DNSName': 'nates-macbook-pro-2023.tail21e383.ts.net.', 'TailscaleIPs': ['100.64.0.2']},
                     'box': {'ID': 'node-box', 'TailscaleIPs': ['100.64.0.3'], 'sshHostKeys': s.get('host_keys', [])}}})
elif name == 'git':
    if 'core.hooksPath=/dev/null' in args:
        sys.exit(subprocess.call([os.environ['BOX_REAL_GIT'], *args]))
    if 'branch' in args: print('main')
    elif s.get('dirty'): print(' M hosts/aws-build.nix')
elif name == 'nix':
    if 'eval' in args or 'build' in args: print(s['desired'])
    elif 'copy' in args:
        assert '--substitute-on-destination' in args
        assert args[args.index('--to') + 1] == 'ssh://root@100.64.0.3'
        if s.get('copy_failure'): sys.exit(1)
        s['copies'] = s.get('copies', 0) + 1; save()
    else: raise AssertionError(args)
elif name == 'ssh':
    if 'ssh_stderr' in s:
        print(s['ssh_stderr'], file=sys.stderr); sys.exit(255)
    command = args[-1]
    if command == 'test -e /var/lib/aws-build/bootstrap-complete':
        s['bootstrap_reads'] = s.get('bootstrap_reads', 0) + 1; save()
        sys.exit(0 if s['bootstrap_reads'] > s.get('bootstrap_delay', 0) else 1)
    elif 'aws-build-setup-stop --on-active' in command:
        s['hold'] = True; s['active'] = False; save()
    elif 'systemd-run --unit=aws-build-switch' in command:
        assert s.get('hold') and s.get('copies')
        assert 'trap' in command and 'systemctl start aws-build-remote.service' in command
        assert 'rm -f -- /home/natepiano/.local/state/build-target/aws-control/idle.json' in command
        s['switches'] = s.get('switches', 0) + 1
        if not s.get('switch_failure'): s['system'] = s['desired']; s['active'] = True
        s['disconnects'] = 2; save()
        if s.get('switch_disconnect'): sys.exit(255)
    elif s.get('disconnects', 0):
        s['disconnects'] -= 1; save(); sys.exit(255)
    elif command == 'readlink -f /run/current-system': print(s['system'])
    elif command == 'systemctl is-active aws-build-remote.service':
        print('active' if s['active'] else 'inactive'); sys.exit(0 if s['active'] else 3)
    elif 'systemctl start aws-build-remote.service' in command:
        s['active'] = True; s['hold'] = False; save()
    elif 'findmnt' in command:
        assert 'volbox' in command and 'lsblk' in command and 'SERIAL' in command
        if s.get('wrong_volume'): sys.exit(1)
        print('healthy')
    elif command in ('cargo-nextest nextest --version', 'cargo-mend mend --version'): versions(shlex.split(command), True)
    else: raise AssertionError(command)
else: raise AssertionError(name)
'''
FAKE_CODE = compile(FAKE, '<box fake>', 'exec')


@final
class FakeTailnet:
    def __init__(self) -> None:
        self.policy: dict[str, object] = {'acls': [{'action': 'accept', 'src': ['100.64.0.9'], 'dst': ['100.64.0.8:22']}],
                                          'ssh': [], 'tagOwners': {'tag:other': ['autogroup:admin']}}
        self.requests: list[tuple[str, str, object]] = []
        self.fail = False

    def __call__(self, method: str, endpoint: str, body: bytes, headers: dict[str, str], timeout: float) -> box.PolicyReply:
        if self.fail:
            raise box.BoxRefused('Tailscale unavailable')
        if endpoint == 'oauth/token':
            return box.PolicyReply({'access_token': 'fixture-token'})
        value: object = json.loads(body) if body else {}
        self.requests.append((method, endpoint, value))
        if endpoint.endswith('/acl'):
            if method == 'POST':
                if headers.get('If-Match') != 'revision':
                    raise AssertionError('missing policy concurrency check')
                self.policy = mapping(value)
            return box.PolicyReply(self.policy, 'revision')
        if endpoint.endswith('/keys'):
            return box.PolicyReply({'id': 'fixture-key', 'key': 'tskey-auth-fixture'})
        if method == 'DELETE':
            return box.PolicyReply({})
        raise AssertionError(endpoint)


@final
class FakeMonotonicClock:
    def __init__(self) -> None:
        self.now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@final
class TailnetRequestTests(unittest.TestCase):
    def test_http_json_error_reports_validation_details_without_user_fields(self) -> None:
        content = json.dumps({'message': 'policy rejected', 'data': [
            {'user': 'x@example.com', 'errors': ['invalid SSH source', 'invalid rule']},
            {'user': 'other@example.com', 'errors': ['another error', 4]},
        ]}).encode()
        error = HTTPError('https://api.tailscale.com', 400, 'Bad Request', Message(), BytesIO(content))
        with mock.patch.object(box, 'urlopen', side_effect=error), \
             mock.patch.object(error, 'read', wraps=error.read) as read, \
             self.assertRaises(box.BoxRefused) as failure:
            _ = box.tailnet_request('POST', 'tailnet/-/acl', b'private request', {'Authorization': 'private token'}, 1)
        read.assert_called_once_with()
        self.assertEqual(str(failure.exception),
                         'Tailscale POST tailnet/-/acl: HTTP 400: policy rejected; invalid SSH source; invalid rule; another error')
        self.assertNotIn('x@example.com', str(failure.exception))
        self.assertNotIn('other@example.com', str(failure.exception))
        self.assertNotIn('private', str(failure.exception))

    def test_http_text_error_collapses_whitespace_and_limits_the_body(self) -> None:
        content = ('  invalid\n  policy\t' + 'x' * 600).encode()
        error = HTTPError('https://api.tailscale.com', 502, 'Bad Gateway', Message(), BytesIO(content))
        with mock.patch.object(box, 'urlopen', side_effect=error), self.assertRaises(box.BoxRefused) as failure:
            _ = box.tailnet_request('POST', 'tailnet/-/acl', b'', {}, 1)
        self.assertEqual(str(failure.exception),
                         'Tailscale POST tailnet/-/acl: HTTP 502: ' + ('invalid policy ' + 'x' * 600)[:500])

    def test_http_empty_error_reports_the_reason_phrase(self) -> None:
        error = HTTPError('https://api.tailscale.com', 503, 'Service Unavailable', Message(), BytesIO())
        with mock.patch.object(box, 'urlopen', side_effect=error), self.assertRaises(box.BoxRefused) as failure:
            _ = box.tailnet_request('GET', 'tailnet/-/acl', b'', {}, 1)
        self.assertEqual(str(failure.exception), 'Tailscale GET tailnet/-/acl: HTTP 503: Service Unavailable')

    def test_request_failure_reports_the_network_or_value_reason(self) -> None:
        for error in (URLError('timed out'), TimeoutError('timed out'), ValueError('invalid URL')):
            with self.subTest(error=type(error).__name__), \
                 mock.patch.object(box, 'urlopen', side_effect=error), self.assertRaises(box.BoxRefused) as failure:
                _ = box.tailnet_request('GET', 'tailnet/-/acl', b'', {}, 1)
            reason = 'invalid URL' if isinstance(error, ValueError) else 'timed out'
            self.assertEqual(str(failure.exception), f'Tailscale GET tailnet/-/acl: request failed: {reason}')


@final
class BoxTests(unittest.TestCase):
    def __init__(self, methodName: str = 'runTest') -> None:
        super().__init__(methodName)
        self.root = Path()
        self.settings = Path()
        self.directory = Path()
        self.tailnet = FakeTailnet()
        self.starts = 0
        self.monotonic = FakeMonotonicClock()
        self.read_live_claims = mac_test.live_claims
        self.subject = box.BoxSetup(self.settings, self.directory, self.start, admin=admin_secrets.AdminSecretSession(self.settings), tailnet=self.tailnet, monotonic=self.monotonic, pause=self.monotonic.advance)

    @override
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = self.root / 'aws_target.conf'
        self.directory = self.root / 'state'
        self.directory.mkdir()
        _ = (self.directory / 'deployment.json').write_text(json.dumps({'region': 'us-east-2', 'stack': 'aws-build',
                                    'outputs': {'InstanceId': 'i-box', 'DataVolumeId': 'vol-box'}}))
        _ = cast(object, self.enterContext(mock.patch.object(admin_secrets.AdminSecretSession, 'get', return_value=
            admin_secrets.AdminCredentials('fixture-admin-id', 'fixture-admin-secret', 'fixture-id', 'fixture-secret'))))
        config = (Path(__file__).resolve().parents[2] / 'config/aws_target.conf').read_text()
        values = {'admin_secret_file': str(self.root / 'admin.gpg'), 'box_check_attempts': '4', 'box_check_delay_s': '0.001', 'idle_stop_minutes': '0.001', 'idle_stop_margin_s': '0',
                  'nixos_configuration_directory': str(self.root / 'nixos')}
        _ = self.settings.write_text('\n'.join(key + '=' + values[key] if '=' in line and (key := line.partition('=')[0]) in values else line
                                             for line in config.splitlines()) + '\n')
        bin_path = self.root / 'bin'
        bin_path.mkdir()
        for name in ('aws', 'ssh', 'git', 'nix', 'tailscale', 'systemctl', 'bash', 'rustc', 'cargo', 'cargo-nextest', 'cargo-mend'):
            path = bin_path / name
            _ = path.write_text(f'#!{sys.executable}\n' + FAKE)
            path.chmod(0o700)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {'BOX_FIXTURE': str(self.root),
                                      'SCRATCH_ROOT': str(self.root / 'scratch'), 'SCRATCH_HOLDS': str(self.root / 'holds.json'),
                                      'HOME': str(self.root / 'home'), 'BOX_REAL_GIT': shutil.which('git') or 'git',
                                      'BUILDLOG_DIR': str(self.root / 'buildlog'), 'PATH': str(bin_path) + os.pathsep + os.environ['PATH']})))
        _ = cast(object, self.enterContext(mock.patch.object(target_config, 'ssh_target_state_directory', return_value=self.directory)))
        _ = cast(object, self.enterContext(mock.patch('scripts.build_targets.target_config.ssh_target_state_directory', return_value=self.directory)))
        _ = cast(object, self.enterContext(mock.patch.object(target_config, 'read_targets', return_value=(target_config.SshTarget('aws', 'linux', self.settings),))))
        registry = self.root / 'home/.claude/config/build_targets.conf'
        registry.parent.mkdir(parents=True)
        _ = registry.write_text('targets=aws\nkind.aws=ssh\nplatform.aws=linux\nsettings.aws=' + str(self.settings) + '\n')
        _ = cast(object, self.enterContext(mock.patch.object(mac_test, 'live_claims', return_value=())))
        self.save({'self_stop': True, 'stop_after': 2, 'online': True, 'joined': False, 'active': True, 'system': '/nix/store/old',
                   'desired': SYSTEM})
        self.tailnet = FakeTailnet()
        self.starts = 0
        self.monotonic = FakeMonotonicClock()
        self.subject = box.BoxSetup(self.settings, self.directory, self.start, admin=admin_secrets.AdminSecretSession(self.settings), tailnet=self.tailnet, monotonic=self.monotonic, pause=self.monotonic.advance)
        if not process_boundary_for_case(self):
            processes = SimpleNamespace(**{**cast(Mapping[str, object], vars(subprocess)),
                                           'run': self.run_tool})
            _ = self.enterContext(mock.patch.object(aws_target, 'subprocess', processes))

    def run_tool(self, arguments: Sequence[str], *, capture_output: bool, text: bool,
                 timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ) if env is None else env
        found = shutil.which(arguments[0], path=environment.get('PATH'))
        path = Path(found) if found else Path(arguments[0])
        if path.parent != self.root / 'bin':
            return subprocess.run(arguments, capture_output=capture_output, text=text,
                                  timeout=timeout, check=check, env=env)
        tool = ModuleType('box_fake')
        tool.__file__ = str(path)

        def main() -> int:
            exec(FAKE_CODE, {'__name__': '__main__', '__file__': str(path)})
            return 0

        result = invoke_cli(tool, main, list(arguments[1:]), environment, Path.cwd())
        result.args = list(arguments)
        if check:
            result.check_returncode()
        return result

    def start(self) -> str:
        self.starts += 1
        state = self.live()
        state.update(online=True, ec2='running', observations=0)
        self.save(state)
        return 'running'

    def live(self) -> dict[str, object]:
        return box.json_record((self.root / 'live.json').read_text())

    def save(self, state: dict[str, object]) -> None:
        _ = (self.root / 'live.json').write_text(json.dumps(state))

    def change(self, **values: object) -> None:
        self.save({**self.live(), **values})

    def commands(self) -> list[list[str]]:
        return [cast(list[str], json.loads(line)) for line in (self.root / 'commands').read_text().splitlines()]

    def test_up_records_all_box_stages_and_resumes_after_configuration_failure(self) -> None:
        self.assertFalse((self.directory / 'routing.json').exists())
        self.assertEqual(conf.read(self.settings).text('offload'), 'on')
        credentials = self.root / 'credentials'
        _ = credentials.write_text('')
        credentials.chmod(0o600)
        subject = deploy.Deployment(self.settings, self.directory, self.root / 'bundle')
        with mock.patch.dict(os.environ, {'AWS_SHARED_CREDENTIALS_FILE': str(credentials)}), \
             mock.patch.object(deploy.Deployment, '_price_present', return_value=True), \
             mock.patch.object(deploy.Deployment, 'price', return_value={}), \
             mock.patch.object(deploy.Deployment, '_bootstrap_present', return_value=True), \
             mock.patch.object(deploy.Deployment, '_stacks_present', return_value=True), \
             mock.patch.object(deploy.Deployment, '_scaler_stage_present', return_value=True), \
             mock.patch.object(box, 'BoxSetup', return_value=self.subject):
            self.change(switch_failure=True)
            output = StringIO()
            with redirect_stdout(output):
                self.assertFalse(subject.up(approve_resources=True))
            self.assertIn('box-configuration: refused:', output.getvalue())
            self.assertNotIn('checks:', output.getvalue())
            self.assertFalse(box.json_record((self.directory / 'routing.json').read_text())['enabled'])
            self.assertFalse(self.subject.activation_present())
            self.change(switch_failure=False)
            output = StringIO()
            with redirect_stdout(output):
                self.assertTrue(subject.up(approve_resources=True))
            self.assertIn('tailscale-policy: present', output.getvalue())
            self.assertIn('box-join: present', output.getvalue())
            self.assertIn('box-configuration: done', output.getvalue())
            self.assertIn('checks: done', output.getvalue())
            self.assertTrue(box.json_record((self.directory / 'routing.json').read_text())['enabled'])
            self.assertLess(output.getvalue().index('idle-stop: done'), output.getvalue().index('activation: done'))
            commands = len(self.commands())
            output = StringIO()
            with redirect_stdout(output):
                self.assertTrue(subject.up(approve_resources=True))
            stage_lines = [line for line in output.getvalue().splitlines() if not line.startswith('up: ')]
            self.assertEqual(len(stage_lines), 12)
            self.assertTrue(all(line.endswith(': present') for line in stage_lines))
            later = self.commands()[commands:]
            self.assertFalse(any('systemd-run' in c[-1] or c[0] == 'nix' and 'copy' in c for c in later))
            results = box.json_record((self.directory / 'up.json').read_text())
            self.assertEqual(len(results), 12)
            self.assertTrue(all(mapping(result)['status'] == 'present' for result in results.values()))

    def image_record(self, **values: object) -> None:
        _ = (self.directory / 'image.json').write_text(json.dumps({'image_id': 'ami-cache', 'system': SYSTEM, **values}))

    def test_image_creation_scrubs_restores_and_excludes_the_data_volume(self) -> None:
        self.change(joined=True, keep=True, snapshot_delayed=True)
        _ = (self.directory / 'image-scrub.json').write_text('{}')
        self.assertFalse(self.subject.image_present())
        self.subject.image()
        commands = self.commands()
        scrub = next(i for i, c in enumerate(commands) if 'mkdir -p -m 0700' in ' '.join(c))
        create = next(i for i, c in enumerate(commands) if 'create-image' in c)
        restores = [i for i, c in enumerate(commands) if 'rmdir --' in ' '.join(c)]
        self.assertLess(restores[0], scrub)
        self.assertLess(scrub, create)
        snapshot = next(i for i, c in enumerate(commands) if 'describe-images' in c)
        self.assertLess(create, snapshot)
        self.assertLess(snapshot, restores[-1])
        self.assertEqual(sum('describe-images' in c for c in commands[create:restores[-1]]), 2)
        args = commands[create]
        self.assertIn('--no-reboot', args)
        self.assertEqual(json.loads(args[args.index('--block-device-mappings') + 1]),
                         [{'DeviceName': '/dev/sdf', 'NoDevice': ''}])
        tags = cast(object, json.loads(args[args.index('--tag-specifications') + 1]))
        self.assertEqual(tags, [{'ResourceType': kind, 'Tags': [{'Key': conf.read(self.settings).text('cost_explorer_tag_key'),
            'Value': conf.read(self.settings).text('cost_explorer_tag_value')}, *IMAGE_TAGS]} for kind in ('image', 'snapshot')])
        scrub_command = cast(list[str], box.json_record(commands[scrub][commands[scrub].index('--parameters') + 1])['commands'])[0]
        self.assertIn('for item in /var/lib/tailscale /var/lib/aws-build/data-volume-id /var/lib/aws-build/bootstrap-complete;', scrub_command)
        self.assertNotIn('systemctl', scrub_command)
        self.assertNotIn('stop', scrub_command.replace('bootstrap', ''))
        steps = ['rm -rf -- "$keep/$name.partial"', 'cp -a -- "$item" "$keep/$name.partial"',
                 'mv -- "$keep/$name.partial" "$keep/$name"', 'rm -rf -- "$item"']
        positions = [scrub_command.index(step) for step in steps]
        self.assertEqual(positions, sorted(positions))
        restore_command = cast(list[str], box.json_record(commands[restores[-1]][
            commands[restores[-1]].index('--parameters') + 1])['commands'])[0]
        self.assertLess(restore_command.index('rm -rf -- "$keep"/*.partial'), restore_command.index('for item'))
        self.assertFalse(self.live()['keep'])
        self.assertFalse((self.directory / 'image-scrub.json').exists())
        self.assertEqual(box.json_record((self.directory / 'image.json').read_text()),
                         {'image_id': 'ami-cache', 'system': SYSTEM})

    def test_image_create_failure_restores_items_and_keeps_the_first_error(self) -> None:
        self.change(joined=True, image_failure=True)
        restore_items = self.subject._restore_image_items  # pyright: ignore[reportPrivateUsage]
        with mock.patch.object(self.subject, '_restore_image_items', wraps=restore_items) as restore, \
             self.assertRaisesRegex(AwsUnavailable, 'fixture image failure'):
            self.subject.image()
        self.assertEqual(restore.call_count, 1)
        self.assertFalse(self.live()['keep'])
        self.assertFalse((self.directory / 'image-scrub.json').exists())
        with mock.patch.object(self.subject, '_restore_image_items', side_effect=box.BoxRefused('restore failed')), \
             self.assertRaisesRegex(AwsUnavailable, 'fixture image failure'):
            self.subject.image()
        self.assertTrue((self.directory / 'image-scrub.json').exists())

    def test_image_restore_marker_resumes_available_image_before_cleanup_without_tailscale(self) -> None:
        self.image_record()
        marker = self.directory / 'image-scrub.json'
        _ = marker.write_text('{}')
        self.change(keep=True)
        self.assertFalse(self.subject.image_present())
        self.assertFalse((self.root / 'commands').exists())
        with mock.patch.object(self.subject, 'join_present', side_effect=AssertionError('Tailscale join')):
            self.subject.image()
        self.assertFalse(marker.exists())
        self.assertFalse(self.live()['keep'])
        self.assertEqual(self.starts, 1)
        commands = self.commands()
        self.assertIn('describe-instance-information', commands[0])
        restore = next(i for i, c in enumerate(commands) if 'rmdir --' in ' '.join(c))
        cleanup = next(i for i, c in enumerate(commands) if '--filters' in c and 'describe-images' in c)
        self.assertLess(restore, cleanup)
        self.assertFalse(any('create-image' in c or c[0] in ('ssh', 'tailscale') for c in commands))

    def test_image_restore_failure_keeps_marker_until_restore_succeeds(self) -> None:
        self.image_record()
        marker = self.directory / 'image-scrub.json'
        _ = marker.write_text('{}')
        self.change(keep=True, restore_failure=True)
        with self.assertRaisesRegex(box.BoxRefused, 'SSM command command-box: Failed'):
            self.subject.image()
        self.assertTrue(marker.exists())
        self.assertTrue(self.live()['keep'])
        self.assertFalse(any('describe-images' in c for c in self.commands()))
        self.subject.image()
        self.assertFalse(marker.exists())
        self.assertFalse(self.live()['keep'])
        self.assertFalse(any('create-image' in c for c in self.commands()))

    def test_image_marker_exists_before_scrub_and_survives_final_restore_failure(self) -> None:
        self.change(joined=True)
        marker = self.directory / 'image-scrub.json'
        ssm = self.subject._ssm  # pyright: ignore[reportPrivateUsage]
        def execute(command: str) -> str:
            if 'mkdir -p -m 0700' in command:
                self.assertTrue(marker.exists())
            return ssm(command)
        with mock.patch.object(self.subject, '_ssm', side_effect=execute), \
             mock.patch.object(self.subject, '_restore_image_items', side_effect=box.BoxRefused('restore failed')), \
             self.assertRaisesRegex(box.BoxRefused, 'restore failed'):
            self.subject.image()
        self.assertTrue(marker.exists())
        self.assertTrue((self.directory / 'image.json').exists())
        self.assertFalse(self.subject.image_present())
        self.subject.image()
        self.assertFalse(marker.exists())
        self.assertEqual(sum('create-image' in c for c in self.commands()), 1)

    def test_image_present_uses_only_ec2_for_the_same_system(self) -> None:
        self.image_record()
        self.assertTrue(self.subject.image_present())
        self.assertTrue(all(c[0] in ('git', 'nix', 'aws') for c in self.commands()))
        self.image_record(system='/nix/store/other')
        before = len(self.commands())
        self.assertFalse(self.subject.image_present())
        self.assertEqual(len(self.commands()), before)
        self.image_record()
        self.change(image_missing=True)
        self.assertFalse(self.subject.image_present())

    def test_pending_image_resumes_without_start_or_scrub(self) -> None:
        self.image_record()
        self.change(image_state='pending')
        self.subject.image()
        self.assertEqual(self.starts, 0)
        self.assertFalse(any('create-image' in c or 'send-command' in c for c in self.commands()))

    def test_image_removes_only_other_images_and_their_snapshots(self) -> None:
        self.change(joined=True, old_images=[{'ImageId': 'ami-old', 'Tags': IMAGE_TAGS, 'BlockDeviceMappings': [
            {'Ebs': {'SnapshotId': 'snap-old'}}]}], old_snapshots=[{'SnapshotId': 'snap-old', 'Tags': IMAGE_TAGS},
            {'SnapshotId': 'snap-unowned', 'Description': 'Created by CreateImage fixture', 'Tags': [
                {'Key': conf.read(self.settings).text('cost_explorer_tag_key'),
                 'Value': conf.read(self.settings).text('cost_explorer_tag_value')}]}])
        self.subject.image()
        commands = self.commands()
        self.assertEqual([c[c.index('--image-id') + 1] for c in commands if 'deregister-image' in c], ['ami-old'])
        self.assertEqual([c[c.index('--snapshot-id') + 1] for c in commands if 'delete-snapshot' in c], ['snap-old'])
        filters = next(c for c in commands if '--filters' in c and 'describe-images' in c)
        self.assertEqual(json.loads(filters[filters.index('--filters') + 1]),
                         [{'Name': 'tag:' + box.IMAGE_OWNER_TAG, 'Values': ['true']}])
        snapshots = next(c for c in commands if 'describe-snapshots' in c)
        self.assertEqual(snapshots[snapshots.index('--owner-ids') + 1], 'self')
        self.assertEqual(json.loads(snapshots[snapshots.index('--filters') + 1]), [
            {'Name': 'tag:' + box.IMAGE_OWNER_TAG, 'Values': ['true']}])
        self.assertEqual([mapping(row)['SnapshotId'] for row in cast(list[object], self.live()['old_snapshots'])], ['snap-unowned'])
        self.assertTrue(self.subject.image_present())

    def test_image_present_requires_no_old_images_or_orphan_snapshots(self) -> None:
        self.image_record()
        self.change(old_images=[{'ImageId': 'ami-old', 'Tags': IMAGE_TAGS}])
        self.assertFalse(self.subject.image_present())
        self.change(old_images=[], old_snapshots=[{'SnapshotId': 'snap-orphan', 'Tags': IMAGE_TAGS}])
        self.assertFalse(self.subject.image_present())
        self.change(old_snapshots=[])
        self.assertTrue(self.subject.image_present())
        self.assertEqual(self.starts, 0)
        self.assertFalse(any(c[0] in ('ssh', 'tailscale') or 'send-command' in c for c in self.commands()))

    def test_image_cleanup_resumes_after_deregister_failure_without_box_changes(self) -> None:
        self.assert_image_cleanup_resumes('deregister_failure', 'fixture deregister failure', True)

    def test_image_cleanup_resumes_after_snapshot_failure_without_box_changes(self) -> None:
        self.assert_image_cleanup_resumes('snapshot_failure', 'fixture snapshot failure', False)

    def assert_image_cleanup_resumes(self, failure: str, reason: str, image_remains: bool) -> None:
        self.change(joined=True, old_images=[{'ImageId': 'ami-old', 'Tags': IMAGE_TAGS, 'BlockDeviceMappings': [
            {'Ebs': {'SnapshotId': 'snap-old'}}]}], old_snapshots=[{'SnapshotId': 'snap-old', 'Tags': IMAGE_TAGS}], **{failure: True})
        with self.assertRaisesRegex(AwsUnavailable, reason):
            self.subject.image()
        self.assertEqual(bool(self.live()['old_images']), image_remains)
        self.assertEqual(self.live()['old_snapshots'], [{'SnapshotId': 'snap-old', 'Tags': IMAGE_TAGS}])
        self.assertFalse(self.subject.image_present())
        before = len(self.commands())
        starts = self.starts
        with mock.patch.object(self.subject, 'start', side_effect=AssertionError('box start')), \
             mock.patch.object(self.subject, 'join_present', side_effect=AssertionError('box join')), \
             mock.patch.object(self.subject, '_ssm', side_effect=AssertionError('box SSM')):
            self.subject.image()
        commands = self.commands()[before:]
        self.assertEqual(self.starts, starts)
        self.assertTrue(all(c[0] == 'aws' for c in commands))
        self.assertFalse(any('create-image' in c or 'send-command' in c for c in commands))
        self.assertEqual([c[c.index('--image-id') + 1] for c in commands if 'deregister-image' in c],
                         ['ami-old'] if image_remains else [])
        self.assertEqual([c[c.index('--snapshot-id') + 1] for c in commands if 'delete-snapshot' in c], ['snap-old'])
        self.assertEqual(self.live()['old_images'], [])
        self.assertEqual(self.live()['old_snapshots'], [])
        self.assertTrue(self.subject.image_present())
        self.assertEqual(sum('create-image' in c for c in self.commands()), 1)

    def test_failed_image_reports_its_state_reason(self) -> None:
        self.image_record()
        # The presence and resume checks see the pending image before the wait reports failure.
        with mock.patch.object(box, 'saved_image', side_effect=[{'State': 'pending'}, {'State': 'pending'},
                {'State': 'failed', 'StateReason': {'Message': 'snapshot failed'}}]), \
             self.assertRaisesRegex(box.BoxRefused, 'snapshot failed'):
            self.subject.image()

    def test_configure_waits_for_bootstrap_before_changes(self) -> None:
        self.change(joined=True, bootstrap_delay=2)
        self.subject.configure()
        ssh = [c for c in self.commands() if c[0] == 'ssh']
        reads = [i for i, c in enumerate(ssh) if c[-1] == 'test -e /var/lib/aws-build/bootstrap-complete']
        self.assertEqual(len(reads), 3)
        first_change = next(i for i, c in enumerate(ssh) if 'systemd-run' in c[-1])
        self.assertLess(reads[-1], first_change)

    def test_idle_stop_image_proof_covers_a_new_instance_with_the_same_system(self) -> None:
        self.image_record(idle_stop_system=SYSTEM)
        with mock.patch.object(self.subject, '_instance', return_value='i-new'):
            self.assertTrue(self.subject.idle_stop_present())
        self.image_record(idle_stop_system='/nix/store/old')
        self.assertFalse(self.subject.idle_stop_present())
        self.change(joined=True, self_stop=True, stop_after=4)
        self.subject.idle_stop()
        self.assertEqual(box.json_record((self.directory / 'image.json').read_text())['idle_stop_system'], SYSTEM)

    def test_policy_devices_match_machine_names_instead_of_hostnames(self) -> None:
        status: dict[str, object] = {
            'Self': {'HostName': 'natedev', 'DNSName': 'natedev.tail21e383.ts.net.', 'TailscaleIPs': ['100.64.0.1']},
            'Peer': {'mac': {'HostName': "Nate's MacBook Pro 2023", 'DNSName': 'nates-macbook-pro-2023.tail21e383.ts.net.', 'TailscaleIPs': ['100.64.0.2']}},
        }
        self.assertEqual(box.policy_device_bindings(box.policy_source(), status),
                         {'$natedev': '100.64.0.1', '$mac': '100.64.0.2'})
        with self.assertRaisesRegex(box.BoxRefused, "Nate's MacBook Pro 2023: no match"):
            _ = box.policy_device_bindings({'devices': {'$mac': "Nate's MacBook Pro 2023"}}, status)

    def test_policy_devices_report_every_bad_name_and_the_present_names(self) -> None:
        source: dict[str, object] = {'devices': {'$missing': 'missing', '$duplicate': 'duplicate', '$empty': 'empty'}}
        status: dict[str, object] = {
            'Self': {'HostName': 'duplicate', 'DNSName': 'duplicate.tail21e383.ts.net.', 'TailscaleIPs': ['100.64.0.1']},
            'Peer': {'duplicate': {'HostName': 'duplicate', 'DNSName': 'duplicate.tail21e383.ts.net.', 'TailscaleIPs': ['100.64.0.2']},
                     'empty': {'HostName': 'empty', 'DNSName': 'empty.tail21e383.ts.net.'},
                     'other': {'HostName': 'other', 'DNSName': 'other'},
                     'missing': {'HostName': 'missing', 'TailscaleIPs': ['100.64.0.3']}},
        }
        with self.assertRaises(box.BoxRefused) as failure:
            _ = box.policy_device_bindings(source, status)
        self.assertEqual(str(failure.exception),
                         'Tailscale policy devices: missing: no match; duplicate: more than one match; '
                         + 'empty: no address; machine names present: duplicate, empty, other')

    def test_policy_changes_only_box_rules_and_then_reports_present(self) -> None:
        unrelated = mapping(cast(list[object], self.tailnet.policy['acls'])[0])
        self.assertFalse(self.subject.policy_present())
        output = StringIO()
        with redirect_stdout(output):
            self.subject.policy()
        self.assertIn('tag:build-aws', output.getvalue())
        ssh_rules = [{'action': 'accept', 'src': ['autogroup:member'], 'dst': ['tag:build-aws'], 'users': ['natepiano', 'root']}]
        network_rule = {'action': 'accept', 'src': ['100.64.0.1', '100.64.0.2'], 'dst': ['tag:build-aws:22']}
        self.assertEqual(self.tailnet.policy['ssh'], ssh_rules)
        self.assertEqual(cast(list[object], self.tailnet.policy['acls'])[1], network_rule)
        self.assertIn('replace ssh rules for tag:build-aws: ' + json.dumps(ssh_rules, sort_keys=True), output.getvalue())
        self.assertIn('replace acls rules for tag:build-aws: ' + json.dumps([network_rule], sort_keys=True), output.getvalue())
        self.assertEqual(cast(list[object], self.tailnet.policy['acls'])[0], unrelated)
        self.assertEqual(mapping(self.tailnet.policy['tagOwners'])['tag:other'], ['autogroup:admin'])
        self.assertTrue(self.subject.policy_present())
        before = len([r for r in self.tailnet.requests if r[0] == 'POST'])
        self.subject.policy()
        self.assertEqual(len([r for r in self.tailnet.requests if r[0] == 'POST']), before)

    def test_policy_refuses_broad_outbound_access_without_editing_it(self) -> None:
        self.tailnet.policy['grants'] = [{'src': ['*'], 'dst': ['*'], 'ip': ['*']}]
        before = json.dumps(self.tailnet.policy)
        with self.assertRaisesRegex(box.BoxRefused, 'outbound'):
            self.subject.policy()
        self.assertEqual(json.dumps(self.tailnet.policy), before)
        self.assertFalse(any(r[0] == 'POST' for r in self.tailnet.requests))

    def test_policy_failure_can_resume_without_losing_unrelated_rules(self) -> None:
        self.tailnet.fail = True
        with self.assertRaisesRegex(box.BoxRefused, 'unavailable'):
            self.subject.policy()
        self.tailnet.fail = False
        with redirect_stdout(StringIO()):
            self.subject.policy()
        self.assertTrue(self.subject.policy_present())

    @process_boundary
    def test_join_sends_one_short_lived_key_only_through_ssm_stdin(self) -> None:
        self.assertFalse(self.subject.join_present())
        self.change(online=False)
        self.subject.join()
        self.assertTrue(self.live()['received_key'])
        self.assertTrue(self.subject.join_present())
        request = next(r for r in self.tailnet.requests if r[1].endswith('/keys'))
        self.assertEqual(mapping(request[2])['expirySeconds'], 300)
        create = mapping(mapping(mapping(mapping(request[2])['capabilities'])['devices'])['create'])
        self.assertEqual(create, {'reusable': False, 'ephemeral': False, 'preauthorized': True, 'tags': ['tag:build-aws']})
        self.assertEqual(self.tailnet.requests[-1][:2], ('DELETE', 'tailnet/-/keys/fixture-key'))
        self.assertEqual(self.starts, 1)
        for path in self.root.rglob('*'):
            if path.is_file() and path.parent.name != 'bin':
                self.assertNotIn('tskey-auth-fixture', path.read_text())
        commands = self.commands()
        self.assertTrue(any('AWS-StartInteractiveCommand' in c for c in commands))
        self.assertIn('file:/dev/stdin', box.JOIN_COMMAND)
        before = len(self.tailnet.requests)
        self.subject.join()
        self.assertEqual(len(self.tailnet.requests), before)

    def test_join_replaces_host_and_address_keys_and_preserves_other_hosts(self) -> None:
        keys = ['ssh-ed25519 ' + b64encode(b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20' + bytes([value]) * 32).decode()
                for value in (1, 2)]
        known_hosts = self.root / 'home/.ssh/known_hosts'
        known_hosts.parent.mkdir(parents=True)
        unrelated = 'other-host ' + keys[0] + '\n'
        _ = known_hosts.write_text('aws-build ' + keys[0] + '\n100.64.0.3 ' + keys[0] + '\n' + unrelated)
        self.change(host_keys=keys)
        self.subject.join()
        self.assertEqual(known_hosts.read_text(), unrelated + ''.join('aws-build,100.64.0.3 ' + key + '\n' for key in keys))
        self.assertEqual(box.json_record((self.directory / 'deployment.json').read_text())['tailscale_node_id'], 'node-box')

    def test_join_creates_private_host_key_paths(self) -> None:
        self.change(joined=True, host_keys=['ssh-ed25519 fixture-public-key'])
        self.assertTrue(self.subject.join_present())
        known_hosts = self.root / 'home/.ssh/known_hosts'
        self.assertEqual(known_hosts.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(known_hosts.stat().st_mode & 0o777, 0o600)
        self.assertEqual(known_hosts.read_text(), 'aws-build,100.64.0.3 ssh-ed25519 fixture-public-key\n')

    def test_join_without_host_keys_writes_nothing(self) -> None:
        self.change(joined=True)
        self.assertTrue(self.subject.join_present())
        known_hosts = self.root / 'home/.ssh/known_hosts'
        self.assertFalse(known_hosts.parent.exists())
        known_hosts.parent.mkdir(parents=True)
        _ = known_hosts.write_text('unrelated entry\n')
        self.assertTrue(self.subject.join_present())
        self.assertEqual(known_hosts.read_text(), 'unrelated entry\n')

    @process_boundary
    def test_box_command_refusal_reports_the_last_stderr_line(self) -> None:
        for stderr, detail in (('earlier error\n  Host key verification failed. \n\n', ': Host key verification failed.'),
                               (' earlier \n\tlast  ' + 'x' * 400 + '\n', ': ' + ('last ' + 'x' * 400)[:300]),
                               ('\n  \n', '')):
            with self.subTest(stderr=stderr):
                self.change(joined=True, ssh_stderr=stderr)
                with self.assertRaises(box.BoxRefused) as failure:
                    _ = self.subject.configuration_present()
                self.assertEqual(str(failure.exception), 'ssh failed during box setup (exit 255)' + detail)

    def test_stopped_join_proof_cannot_replace_a_missing_saved_node_identity(self) -> None:
        self.change(joined=True, ec2='stopped')
        record = {'idle-stop': {'instance_id': 'i-box', 'proved_at': 1}, 'box-join': {'status': 'done'}}
        _ = (self.directory / 'up.json').write_text(json.dumps(record))
        self.assertTrue(self.subject.join_present())
        saved = box.json_record((self.directory / 'deployment.json').read_text())
        self.assertEqual(saved['tailscale_node_id'], 'node-box')
        self.assertEqual(saved['tailscale_instance_id'], 'i-box')
        self.assertTrue(any('send-command' in command for command in self.commands()))

    def test_failed_join_revokes_the_key_and_a_retry_joins(self) -> None:
        self.change(join_failure=True)
        with self.assertRaisesRegex(box.BoxRefused, 'SSM join failed'):
            self.subject.join()
        self.assertEqual(self.tailnet.requests[-1][0], 'DELETE')
        self.change(join_failure=False)
        self.subject.join()
        self.assertTrue(self.subject.join_present())

    def test_configuration_holds_idle_stop_and_survives_the_switch_disconnect(self) -> None:
        self.change(joined=True, switch_disconnect=True)
        self.assertFalse(self.subject.configuration_present())
        before = len(self.commands())
        self.subject.configure()
        configure_commands = self.commands()[before:]
        ssh_commands = [command for command in configure_commands if command[0] == 'ssh']
        self.assertTrue(ssh_commands)
        read_commands = {'readlink -f /run/current-system', 'systemctl is-active aws-build-remote.service',
                         'test -e /var/lib/aws-build/bootstrap-complete'}
        self.assertTrue(read_commands.issubset({command[-1] for command in ssh_commands}))
        for command in ssh_commands:
            user = 'natepiano' if command[-1] in read_commands else 'root'
            self.assertEqual(command[-2], user + '@100.64.0.3')
            self.assertNotIn('sudo', command[-1])
        self.assertTrue(self.subject.configuration_present())
        self.assertEqual(self.commands()[-1][-2], 'natepiano@100.64.0.3')
        commands = self.commands()
        hold = next(i for i, c in enumerate(commands) if 'aws-build-setup-stop --on-active' in c[-1])
        copy = next(i for i, c in enumerate(commands) if c[0] == 'nix' and 'copy' in c)
        switch = next(i for i, c in enumerate(commands) if 'systemd-run --unit=aws-build-switch' in c[-1])
        self.assertLess(hold, copy)
        self.assertLess(copy, switch)
        self.assertIn('--substitute-on-destination', commands[copy])
        self.assertEqual(commands[copy][commands[copy].index('--to') + 1], 'ssh://root@100.64.0.3')
        self.assertIn('--collect --no-block', commands[switch][-1])
        self.assertTrue(self.live()['active'])
        self.assertFalse(self.live()['hold'])
        self.assertEqual(self.live()['switches'], 1)

    def test_failed_switch_restores_idle_stop_and_can_resume(self) -> None:
        self.change(joined=True, switch_failure=True)
        with self.assertRaisesRegex(box.BoxRefused, 'detached switch'):
            self.subject.configure()
        self.assertTrue(self.live()['active'])
        self.assertFalse(self.subject.configuration_present())
        self.change(switch_failure=False)
        self.subject.configure()
        self.assertTrue(self.subject.configuration_present())

    def test_copy_failure_restores_the_remote_service(self) -> None:
        self.change(joined=True, copy_failure=True)
        with self.assertRaisesRegex(box.BoxRefused, 'failed during box setup'):
            self.subject.configure()
        self.assertTrue(self.live()['active'])
        self.assertFalse(self.live()['hold'])
        self.assertTrue(any('rm -f -- /home/natepiano/.local/state/build-target/aws-control/idle.json' in c[-1]
                            and 'systemctl start' in c[-1] and 'sudo' not in c[-1]
                            and c[-2] == 'root@100.64.0.3' for c in self.commands()))

    def test_checks_compare_plain_environment_versions_without_another_repository(self) -> None:
        self.change(joined=True)
        self.assertFalse(self.subject.checks_present())
        self.subject.checks()
        self.assertTrue(self.subject.checks_present())
        before = [c for c in self.commands() if c[0] == 'nix']
        self.assertTrue(self.subject.checks_present())
        self.assertEqual([c for c in self.commands() if c[0] == 'nix'], before)
        for command in (['cargo-nextest', 'nextest', '--version'], ['cargo-mend', 'mend', '--version']):
            self.assertIn(command, self.commands())
        self.assertFalse(any(c[0] == 'nix' for c in self.commands()))
        recorded = mapping(box.json_record((self.directory / 'box-tools.json').read_text())['versions'])
        self.assertEqual(set(recorded), {'nextest', 'mend'})
        self.assertFalse(any(c[0] == 'rustc' or c[-1] == 'rustc -V' for c in self.commands()))
        self.change(mismatch=True)
        self.assertFalse(self.subject.checks_present())
        with self.assertRaisesRegex(box.BoxRefused, 'nextest box=cargo-nextest wrong natedev=cargo-nextest fixture release fixture'):
            self.subject.checks()
        self.change(mismatch=False)
        self.subject.checks()
        self.assertTrue(self.subject.checks_present())

    def test_checks_refuse_a_mend_version_mismatch(self) -> None:
        self.change(joined=True, mend_mismatch=True)
        with self.assertRaisesRegex(box.BoxRefused, 'mend box=mend wrong natedev=mend fixture'):
            self.subject.checks()
        self.assertFalse((self.directory / 'box-tools.json').exists())

    def test_checks_refuse_a_wrong_home_volume_and_an_inactive_remote_service(self) -> None:
        for values in ({'wrong_volume': True, 'active': True}, {'wrong_volume': False, 'active': False}):
            with self.subTest(values=values):
                self.change(joined=True, **values)
                with self.assertRaises(box.BoxRefused):
                    self.subject.checks()
                self.assertFalse(any(c[0] in ('rustc', 'cargo-nextest', 'cargo-mend') for c in self.commands()))


    def test_idle_stop_records_the_instance_and_never_waits_again(self) -> None:
        self.change(joined=True)
        self.assertFalse(self.subject.idle_stop_present())
        self.subject.idle_stop()
        count = len(self.commands())
        self.subject.idle_stop()
        self.assertEqual(len(self.commands()), count)
        self.assertTrue(self.subject.idle_stop_present())
        self.assertFalse(self.live().get('forced_stop', False))
        self.assertEqual(mapping(box.json_record((self.directory / 'up.json').read_text())['idle-stop'])['instance_id'], 'i-box')

    def test_idle_stop_starts_a_stopped_instance_before_recording_the_proof(self) -> None:
        self.change(ec2='stopped', joined=True, online=False, stop_after=3)
        self.assertFalse(self.subject.idle_stop_present())
        self.subject.idle_stop()
        self.assertEqual(self.starts, 1)
        self.assertEqual(self.live()['ec2'], 'stopped')
        self.assertTrue(self.subject.idle_stop_present())
        self.assertFalse(self.live().get('forced_stop', False))

    def test_idle_stop_waits_for_pending_and_health_before_proving_the_stop(self) -> None:
        with mock.patch.object(box.BoxSetup, '_instance_state', side_effect=['pending', 'running', 'stopped']) as state, \
             mock.patch.object(box.BoxSetup, '_health', side_effect=[False, True]) as health:
            self.subject.idle_stop()
        self.assertEqual(state.call_count, 3)
        self.assertEqual(health.call_count, 2)
        self.assertEqual(self.starts, 0)
        self.assertTrue(self.subject.idle_stop_present())

    def test_idle_stop_waits_for_stopping_before_starting_again(self) -> None:
        with mock.patch.object(box.BoxSetup, '_instance_state', side_effect=['stopping', 'stopped', 'running', 'stopped']) as state, \
             mock.patch.object(box.BoxSetup, '_health', return_value=True):
            self.subject.idle_stop()
        self.assertEqual(state.call_count, 4)
        self.assertEqual(self.starts, 1)
        self.assertTrue(self.subject.idle_stop_present())

    def test_idle_stop_waits_for_shutting_down_before_refusing_terminated(self) -> None:
        with (mock.patch.object(box.BoxSetup, '_instance_state', side_effect=['shutting-down', 'terminated']) as state,
              self.assertRaisesRegex(box.BoxRefused, 'state terminated')):
            self.subject.idle_stop()
        self.assertEqual(state.call_count, 2)
        self.assertEqual(self.starts, 0)
        self.assertFalse(self.subject.idle_stop_present())

    def test_idle_stop_waits_for_live_claims_before_starting_with_routing_closed(self) -> None:
        self.change(ec2='stopped', joined=True, stop_after=3)
        def pause(seconds: float) -> None:
            self.assertFalse(box.json_record((self.directory / 'routing.json').read_text())['enabled'])
            self.monotonic.advance(seconds)
        output = StringIO()
        with mock.patch.object(box, 'running_claims', side_effect=[('claim-1', 'claim-2'), ('claim-1',), (), (), ()]), \
             mock.patch.object(self.subject, 'pause', side_effect=pause), redirect_stdout(output):
            self.subject.idle_stop()
        self.assertEqual(self.starts, 1)
        self.assertTrue(self.subject.idle_stop_present())
        self.assertEqual(output.getvalue().splitlines(), [
            'routing: AWS takes no new work until up finishes', 'routing: waiting for 2 AWS jobs to finish'])

    def test_idle_stop_extends_the_deadline_for_a_claim_during_the_proof(self) -> None:
        def pause(_seconds: float) -> None:
            self.monotonic.advance(0.03)
        with mock.patch.object(box, 'running_claims', side_effect=[(), (), ('late-claim',), (), ()]), \
             mock.patch.object(self.subject, '_instance_state', side_effect=['running', 'running', 'running', 'running', 'stopped']), \
             mock.patch.object(self.subject, '_health', return_value=True), \
             mock.patch.object(self.subject, 'pause', side_effect=pause):
            self.subject.idle_stop()
        self.assertGreaterEqual(self.monotonic(), 0.09)
        self.assertTrue(self.subject.idle_stop_present())
        self.assertFalse((self.root / 'commands').exists())

    def test_configuration_and_image_wait_for_claims_with_routing_closed(self) -> None:
        self.change(joined=True)
        for stage in (self.subject.configure, self.subject.image):
            with self.subTest(stage=stage.__name__):
                starts = self.starts
                waiting = True
                def pause(seconds: float) -> None:
                    nonlocal waiting
                    if waiting:
                        self.assertEqual(self.starts, starts)
                        waiting = False
                    self.assertFalse(box.json_record((self.directory / 'routing.json').read_text())['enabled'])
                    self.monotonic.advance(seconds)
                with mock.patch.object(box, 'running_claims', side_effect=[('claim',), ()]), \
                     mock.patch.object(self.subject, 'pause', side_effect=pause):
                    stage()
                self.assertGreater(self.starts, starts)

    def test_setup_stages_ignore_a_kept_claim_and_preserve_its_file(self) -> None:
        self.change(joined=True, stop_after=3)
        path = write_aws_claim(self.directory, kept=True)
        original = path.read_bytes()
        paths = mac_test.StatePaths(self.directory, 'aws')
        with mock.patch.object(mac_test, 'state_paths', return_value=paths), \
             mock.patch.object(mac_test, 'live_claims', side_effect=self.read_live_claims), \
             redirect_stdout(StringIO()) as output:
            for stage in (self.subject.configure, self.subject.image, self.subject.idle_stop):
                with self.subTest(stage=stage.__name__):
                    stage()
                    self.assertEqual(path.read_bytes(), original)
        self.assertNotIn('waiting for', output.getvalue())
        self.assertTrue(self.subject.idle_stop_present())

    def test_setup_stages_ignore_a_reservation_and_preserve_its_file(self) -> None:
        self.change(joined=True, stop_after=3)
        path = write_aws_claim(self.directory, kept=False, remote=False)
        original = path.read_bytes()
        paths = mac_test.StatePaths(self.directory, 'aws')
        self.assertTrue(mac_test.run_is_live(mac_test.read_claims(paths)[0].record))
        self.assertEqual(box.running_claims(paths), ())
        with mock.patch.object(mac_test, 'state_paths', return_value=paths), \
             redirect_stdout(StringIO()) as output:
            for stage in (self.subject.configure, self.subject.image, self.subject.idle_stop):
                with self.subTest(stage=stage.__name__):
                    stage()
                    self.assertEqual(path.read_bytes(), original)
        self.assertNotIn('waiting for', output.getvalue())
        self.assertTrue(self.subject.idle_stop_present())

    def test_running_claims_preserves_an_ended_owners_reservation(self) -> None:
        path = write_aws_claim(self.directory, kept=True, remote=False)
        original = path.read_bytes()
        paths = mac_test.StatePaths(self.directory, 'aws')
        self.assertFalse(mac_test.run_is_live(mac_test.read_claims(paths)[0].record))
        self.assertEqual(box.running_claims(paths), ())
        self.assertEqual(path.read_bytes(), original)

    def test_kept_claim_does_not_extend_the_idle_stop_deadline(self) -> None:
        self.change(joined=True, self_stop=False)
        path = write_aws_claim(self.directory, kept=True)
        original = path.read_bytes()
        paths = mac_test.StatePaths(self.directory, 'aws')
        def pause(seconds: float) -> None:
            self.monotonic.advance(seconds)
            self.assertLessEqual(self.monotonic(), 0.061, 'a kept claim extended the deadline')
        with mock.patch.object(mac_test, 'state_paths', return_value=paths), \
             mock.patch.object(mac_test, 'live_claims', side_effect=self.read_live_claims), \
             mock.patch.object(self.subject, 'pause', side_effect=pause), \
             self.assertRaisesRegex(box.BoxRefused, 'up stopped the instance'):
            self.subject.idle_stop()
        self.assertAlmostEqual(self.monotonic(), 0.06)
        self.assertTrue(self.live()['forced_stop'])
        self.assertEqual(path.read_bytes(), original)

    def test_present_setup_stages_leave_routing_open(self) -> None:
        path = self.directory / 'routing.json'
        original = json.dumps({'enabled': True, 'settings': str(self.settings.resolve())})
        _ = path.write_text(original)
        with mock.patch.object(self.subject, 'configuration_present', return_value=True), \
             mock.patch.object(self.subject, 'image_present', return_value=True), \
             mock.patch.object(self.subject, 'idle_stop_present', return_value=True), \
             mock.patch.object(mac_test, 'live_claims', side_effect=AssertionError('present stage waited')):
            self.subject.configure()
            self.subject.image()
            self.subject.idle_stop()
        self.assertEqual(path.read_text(), original)
        self.assertEqual(self.starts, 0)

    def test_idle_stop_stops_its_restart_when_health_fails(self) -> None:
        self.change(ec2='stopped', self_stop=False)
        with mock.patch.object(box.BoxSetup, '_health', return_value=False):
            with self.assertRaisesRegex(box.BoxRefused, 'healthy running box'):
                self.subject.idle_stop()
        self.assertEqual(self.starts, 1)
        self.assertEqual(sum('stop-instances' in command for command in self.commands()), 1)
        self.assertEqual(self.live()['ec2'], 'stopped')
        self.assertFalse(self.subject.idle_stop_present())

    def test_idle_stop_stops_its_restart_when_health_wait_is_interrupted(self) -> None:
        self.change(ec2='stopped', self_stop=False)
        with mock.patch.object(box.BoxSetup, '_health', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.subject.idle_stop()
        self.assertEqual(self.starts, 1)
        self.assertEqual(sum('stop-instances' in command for command in self.commands()), 1)
        self.assertEqual(self.live()['ec2'], 'stopped')
        self.assertFalse(self.subject.idle_stop_present())

    def test_idle_stop_health_failure_keeps_an_instance_it_did_not_start(self) -> None:
        self.change(ec2='running', self_stop=False)
        with mock.patch.object(box.BoxSetup, '_health', return_value=False):
            with self.assertRaisesRegex(box.BoxRefused, 'healthy running box'):
                self.subject.idle_stop()
        self.assertEqual(self.starts, 0)
        self.assertFalse(any('stop-instances' in command for command in self.commands()))
        self.assertEqual(self.live()['ec2'], 'running')
        self.assertFalse(self.subject.idle_stop_present())

    def test_idle_stop_deadline_stops_the_instance_and_refuses_without_a_proof(self) -> None:
        self.change(joined=True, self_stop=False)
        with self.assertRaisesRegex(box.BoxRefused, 'up stopped the instance'):
            self.subject.idle_stop()
        self.assertEqual(self.live()['ec2'], 'stopped')
        self.assertTrue(self.live()['forced_stop'])
        self.assertGreaterEqual(self.monotonic(), 0.06)
        self.assertFalse(self.subject.idle_stop_present())
        self.change(self_stop=True)
        self.subject.idle_stop()
        self.assertEqual(self.starts, 1)
        self.assertTrue(self.subject.idle_stop_present())

    def test_activation_uses_external_state_and_recovers_after_service_failure(self) -> None:
        _ = self.settings.write_text(self.settings.read_text().replace('offload=on', 'offload=off'))
        before = self.settings.read_bytes()
        self.assertFalse(target_config.aws_routing_enabled(conf.read(self.settings)))
        self.assertFalse(self.subject.activation_present())
        self.change(activation_failure=True)
        with self.assertRaises(box.BoxRefused):
            self.subject.activate()
        self.assertFalse(self.subject.activation_present())
        self.change(activation_failure=False)
        self.subject.activate()
        self.assertTrue(self.subject.activation_present())
        commands = len(self.commands())
        self.subject.activate()
        self.assertFalse(any('enable' in c for c in self.commands()[commands:]))
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertTrue(target_config.aws_routing_enabled(conf.read(self.settings)))
        self.assertTrue(offload.read_config(self.settings).enabled)

    def test_written_routing_crate_has_a_clean_committed_repository(self) -> None:
        task = 'gate:aws-routing-proof-test'
        directory = scratch.new(task, 'routing-proof')
        try:
            _ = scratch.update_record(directory, lambda record: {
                **(record.fields if isinstance(record, copy_record.Registered) else {}), 'generated_source': True})
            self.assertTrue((directory / copy_record.RECORD).exists())
            routing_proof.write_crate(directory)
            git = [os.environ['BOX_REAL_GIT'], '-C', str(directory), '-c', 'core.hooksPath=/dev/null',
                   '-c', 'commit.gpgsign=false', '-c', 'user.name=AWS Routing Proof',
                   '-c', 'user.email=aws-routing-proof@example.invalid']
            _ = subprocess.run([*git, 'init', '--initial-branch=main'], capture_output=True, text=True, check=True)
            scratch.exclude_copy_metadata(directory)
            for command in (['add', '.'], ['commit', '-m', 'Create the routing proof crate']):
                _ = subprocess.run([*git, *command], capture_output=True, text=True, check=True)
            status = subprocess.run([*git, 'status', '--porcelain'], capture_output=True, text=True, check=True)
            self.assertEqual(status.stdout, '')
            for name, content in routing_proof.FILES.items():
                committed = subprocess.run([*git, 'show', 'HEAD:' + name], capture_output=True, text=True, check=True)
                self.assertEqual(committed.stdout, content)
            self.assertEqual(set(routing_proof.FILES),
                             {'Cargo.toml', 'Cargo.lock', 'flake.nix', 'flake.lock', '.gitignore', 'src/lib.rs'})
        finally:
            _ = scratch.end(task)
        self.assertFalse(directory.exists())

    def test_routing_proof_requires_a_new_successful_aws_step_and_resumes(self) -> None:
        self.subject.activate()
        self.assertFalse(self.subject.routing_proof_present())
        self.change(proof_failure=True)
        with self.assertRaises(box.BoxRefused):
            self.subject.routing_proof()
        failed_source = Path(shlex.split(next(c[-1] for c in self.commands() if c[0] == 'bash'))[1])
        self.assertFalse(failed_source.exists())
        self.change(proof_failure=False, missing_record=True)
        with self.assertRaisesRegex(box.BoxRefused, 'no new successful'):
            self.subject.routing_proof()
        self.change(missing_record=False)
        self.subject.routing_proof()
        self.assertTrue(self.subject.routing_proof_present())
        commands = len(self.commands())
        self.subject.routing_proof()
        self.assertEqual(len(self.commands()), commands)
        command = next(c[-1] for c in self.commands() if c[0] == 'bash')
        self.assertIn('test aws-routing-proof --target aws --no-cache', command)
        source = Path(shlex.split(command)[1])
        self.assertEqual(source.parent, Path(os.environ['SCRATCH_ROOT']))
        self.assertFalse(source.exists())
        self.assertNotIn('cargo-liner', command)
        sources = [Path(shlex.split(c[-1])[1]) for c in self.commands() if c[0] == 'bash']
        self.assertEqual(len(set(sources)), 3)
        tasks = cast(list[str], self.live()['proof_tasks'])
        self.assertEqual(tasks, ['gate:aws-routing-proof'] * 3)
        self.assertTrue(all(not path.exists() for path in sources))

    def test_routing_proof_saves_success_before_cleanup_failure(self) -> None:
        self.subject.activate()
        with mock.patch.object(scratch, 'end', side_effect=[[], OSError('cleanup failed')]) as end:
            with self.assertRaisesRegex(OSError, 'cleanup failed'):
                self.subject.routing_proof()
            self.assertEqual(end.call_count, 2)
        self.assertTrue(self.subject.routing_proof_present())
        commands = self.commands()
        self.assertEqual(sum(command[0] == 'bash' for command in commands), 1)
        self.subject.routing_proof()
        self.assertEqual(self.commands(), commands)

    def test_routing_proof_removes_a_dead_runs_repository_before_the_next_run(self) -> None:
        task = 'gate:aws-routing-proof'
        leftover = scratch.new(task, 'routing-proof')
        routing_proof.write_crate(leftover)
        git = [os.environ['BOX_REAL_GIT'], '-C', str(leftover), '-c', 'core.hooksPath=/dev/null',
               '-c', 'commit.gpgsign=false', '-c', 'user.name=AWS Routing Proof',
               '-c', 'user.email=aws-routing-proof@example.invalid']
        for arguments in (['init', '--initial-branch=main'], ['add', '.'], ['commit', '-m', 'Create the routing proof crate']):
            _ = subprocess.run([*git, *arguments], check=True, capture_output=True, text=True)
        self.subject.activate()
        self.subject.routing_proof()
        self.assertTrue(self.subject.routing_proof_present())
        self.assertFalse(leftover.exists())
        self.assertEqual(self.live()['proof_tasks'], [task])
        sources = [Path(shlex.split(command[-1])[1]) for command in self.commands() if command[0] == 'bash']
        self.assertEqual(len(sources), 1)
        self.assertNotEqual(sources[0], leftover)
        self.assertFalse(sources[0].exists())

    def test_stopped_setup_rechecks_changed_system_and_local_versions(self) -> None:
        self.change(joined=True)
        self.subject.configure()
        self.subject.checks()
        self.subject.idle_stop()
        self.subject.activate()
        self.subject.routing_proof()
        saved = (self.directory / 'up.json').read_bytes()
        tools = (self.directory / 'box-tools.json').read_bytes()
        for change in ('system', 'tools'):
            with self.subTest(change=change):
                _ = (self.directory / 'up.json').write_bytes(saved)
                _ = (self.directory / 'box-tools.json').write_bytes(tools)
                self.change(ec2='stopped', desired=SYSTEM if change == 'tools' else SYSTEM + '-new',
                            local_nextest='cargo-nextest new' if change == 'tools' else 'cargo-nextest fixture release fixture')
                subject = box.BoxSetup(self.settings, self.directory, self.start,
                                       admin=admin_secrets.AdminSecretSession(self.settings), tailnet=self.tailnet, monotonic=self.monotonic, pause=self.monotonic.advance)
                if change == 'system':
                    self.assertFalse(subject.configuration_present())
                else:
                    self.assertTrue(subject.configuration_present())
                    self.assertFalse(subject.checks_present())
                self.assertFalse(subject.idle_stop_present())
                self.assertFalse(subject.routing_proof_present())
        commands = self.commands()
        self.assertEqual(sum(c[0] == 'nix' and 'eval' in c for c in commands), 3)
        record = box.json_record((self.directory / 'deployment.json').read_text())
        self.assertEqual(record['tailscale_node_id'], 'node-box')
        self.assertEqual(record['tailscale_instance_id'], 'i-box')

    def test_replacement_instance_invalidates_both_proofs(self) -> None:
        self.change(joined=True)
        self.subject.idle_stop()
        self.subject.activate()
        self.subject.routing_proof()
        path = self.directory / 'deployment.json'
        record = box.json_record(path.read_text())
        mapping(record['outputs'])['InstanceId'] = 'i-replacement'
        _ = path.write_text(json.dumps(record))
        self.assertFalse(self.subject.idle_stop_present())
        self.assertFalse(self.subject.routing_proof_present())


if __name__ == '__main__':
    _ = unittest.main()
