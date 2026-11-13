"""Exercise box deployment with local executable fakes and an in-memory tailnet."""
from __future__ import annotations

from email.message import Message
from io import BytesIO
import json
import os
from pathlib import Path
import unittest
from typing import final
from unittest import mock
from urllib.error import HTTPError, URLError

from scripts.aws_target import box
from scripts.aws_target.ledger import mapping
from scripts.mac_test import mac_test
from scripts.lib import process_start

SYSTEM = '/nix/store/' + 'a' * 32 + '-nixos-system-aws-build'
IMAGE_TAGS = [{'Key': box.IMAGE_OWNER_TAG, 'Value': 'true'}]


def write_aws_claim(directory: Path, *, kept: bool, remote: bool = True) -> Path:
    owner = process_start.process_start(os.getpid())
    assert isinstance(owner, process_start.Started)
    record: mac_test.ProcessBoundClaim = {
        'claim_id': 'fixture-claim', 'episode': 'fixture-episode', 'attempt': 'fixture-attempt',
        'submitting_session': '',
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
    if op == 'get-service-quota':
        output({'Quota': {'Value': 128}})
    elif op == 'describe-instances':
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


if __name__ == '__main__':
    _ = unittest.main()
