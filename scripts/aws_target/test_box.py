"""Exercise box deployment with local executable fakes and an in-memory tailnet."""
from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from typing import cast, final, override
from unittest import mock

from scripts.aws_target import admin_secrets, box, deploy
from scripts.aws_target.ledger import mapping
from scripts.build_targets import target_config
from scripts.mac_test import mac_test, offload
from scripts.lib import conf

SYSTEM = '/nix/store/' + 'a' * 32 + '-nixos-system-aws-build'

FAKE = r'''import json, os, pathlib, shlex, sys
root = pathlib.Path(os.environ['BOX_FIXTURE'])
path = root / 'live.json'
s = json.loads(path.read_text())
args = sys.argv[1:]
name = pathlib.Path(sys.argv[0]).name
with (root / 'commands').open('a') as log:
    log.write(json.dumps([name, *args]) + '\n')
def save(): path.write_text(json.dumps(s))
def output(value): print(json.dumps(value))
def versions(command, remote=False):
    if 'rustc' in command: print('rustc fixture')
    elif 'nextest' in command: print('cargo-nextest fixture\n  release fixture  ' if not s.get('mismatch') or not remote else 'cargo-nextest wrong')
    elif 'mend' in command: print('mend fixture')
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
    elif op == 'describe-instance-information':
        output({'InstanceInformationList': [{'InstanceId': 'i-box', 'PingStatus': 'Online'}] if s['online'] else []})
    elif op == 'send-command':
        s['ssm_command'] = json.loads(args[args.index('--parameters') + 1])['commands'][0]
        save(); output({'Command': {'CommandId': 'command-box'}})
    elif op == 'get-command-invocation':
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
elif name in ('rustc', 'cargo'):
    versions([name, *args])
elif name == 'bash':
    words = shlex.split(args[-1])
    source = pathlib.Path(words[words.index('cd') + 1])
    assert source.name == 'proof_crate' and (source / 'Cargo.toml').is_file()
    assert 'DOC_REPO_TOP=' + str(source) in words
    assert words[words.index('test') + 1] == 'aws-routing-proof'
    if s.get('proof_failure'): sys.exit(1)
    if not s.get('missing_record'):
        log = pathlib.Path(os.environ['BUILDLOG_DIR']) / 'aws-build' / '2026-11.jsonl'
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open('a') as handle: handle.write(json.dumps({'kind': 'step', 'id': 'proof-step', 'target': 'aws', 'host': 'aws-build', 'package': 'aws-routing-proof', 'status': 0, 'pinned': True}) + '\n')
elif name == 'tailscale':
    output({'Self': {'HostName': 'natedev', 'TailscaleIPs': ['100.64.0.1']},
            'Peer': {'mac': {'HostName': 'MacBook-Pro', 'TailscaleIPs': ['100.64.0.2']}}})
elif name == 'git':
    if 'branch' in args: print('main')
    elif s.get('dirty'): print(' M hosts/aws-build.nix')
elif name == 'nix':
    if 'eval' in args or 'build' in args: print(s['desired'])
    elif 'copy' in args:
        assert '--substitute-on-destination' in args
        if s.get('copy_failure'): sys.exit(1)
        s['copies'] = s.get('copies', 0) + 1; save()
    else: raise AssertionError(args)
elif name == 'ssh':
    command = args[-1]
    if 'aws-build-setup-stop --on-active' in command:
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
    elif command in ('rustc -V', 'cargo nextest --version', 'cargo mend --version'): versions(shlex.split(command), True)
    else: raise AssertionError(command)
else: raise AssertionError(name)
'''


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
class BoxTests(unittest.TestCase):
    def __init__(self, methodName: str = 'runTest') -> None:
        super().__init__(methodName)
        self.root = Path()
        self.settings = Path()
        self.directory = Path()
        self.tailnet = FakeTailnet()
        self.starts = 0
        self.subject = box.BoxSetup(self.settings, self.directory, self.start, admin=admin_secrets.AdminSecretSession(self.settings), tailnet=self.tailnet)

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
        for name in ('aws', 'ssh', 'git', 'nix', 'tailscale', 'systemctl', 'bash', 'rustc', 'cargo'):
            path = bin_path / name
            _ = path.write_text(f'#!{sys.executable}\n' + FAKE)
            path.chmod(0o700)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {'BOX_FIXTURE': str(self.root),
                                      'BUILDLOG_DIR': str(self.root / 'buildlog'), 'PATH': str(bin_path) + os.pathsep + os.environ['PATH']})))
        _ = cast(object, self.enterContext(mock.patch.object(target_config, 'ssh_target_state_directory', return_value=self.directory)))
        _ = cast(object, self.enterContext(mock.patch('target_config.ssh_target_state_directory', return_value=self.directory)))
        _ = cast(object, self.enterContext(mock.patch.object(target_config, 'read_targets', return_value=(target_config.SshTarget('aws', 'linux', self.settings),))))
        _ = cast(object, self.enterContext(mock.patch.object(mac_test, 'live_claims', return_value=())))
        self.save({'self_stop': True, 'stop_after': 2, 'online': True, 'joined': False, 'active': True, 'system': '/nix/store/old',
                   'desired': SYSTEM})
        self.tailnet = FakeTailnet()
        self.starts = 0
        self.subject = box.BoxSetup(self.settings, self.directory, self.start, admin=admin_secrets.AdminSecretSession(self.settings), tailnet=self.tailnet)

    def start(self) -> str:
        self.starts += 1
        state = self.live()
        state['online'] = True
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
            self.change(switch_failure=False)
            output = StringIO()
            with redirect_stdout(output):
                self.assertTrue(subject.up(approve_resources=True))
            self.assertIn('tailscale-policy: present', output.getvalue())
            self.assertIn('box-join: present', output.getvalue())
            self.assertIn('box-configuration: done', output.getvalue())
            self.assertIn('checks: done', output.getvalue())
            commands = len(self.commands())
            output = StringIO()
            with redirect_stdout(output):
                self.assertTrue(subject.up(approve_resources=True))
            self.assertTrue(all(line.endswith(': present') for line in output.getvalue().splitlines()))
            later = self.commands()[commands:]
            self.assertFalse(any('systemd-run' in c[-1] or c[0] == 'nix' and 'copy' in c for c in later))
            results = box.json_record((self.directory / 'up.json').read_text())
            self.assertEqual(len(results), 11)
            self.assertTrue(all(mapping(result)['status'] == 'present' for result in results.values()))

    def test_policy_changes_only_box_rules_and_then_reports_present(self) -> None:
        unrelated = mapping(cast(list[object], self.tailnet.policy['acls'])[0])
        self.assertFalse(self.subject.policy_present())
        output = StringIO()
        with redirect_stdout(output):
            self.subject.policy()
        self.assertIn('tag:build-aws', output.getvalue())
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
        self.subject.configure()
        self.assertTrue(self.subject.configuration_present())
        commands = self.commands()
        hold = next(i for i, c in enumerate(commands) if 'aws-build-setup-stop --on-active' in c[-1])
        copy = next(i for i, c in enumerate(commands) if c[0] == 'nix' and 'copy' in c)
        switch = next(i for i, c in enumerate(commands) if 'systemd-run --unit=aws-build-switch' in c[-1])
        self.assertLess(hold, copy)
        self.assertLess(copy, switch)
        self.assertIn('--substitute-on-destination', commands[copy])
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
                            and 'sudo systemctl start' in c[-1] for c in self.commands()))

    def test_checks_compare_plain_environment_versions_without_another_repository(self) -> None:
        self.change(joined=True)
        self.assertFalse(self.subject.checks_present())
        self.subject.checks()
        self.assertTrue(self.subject.checks_present())
        before = [c for c in self.commands() if c[0] == 'nix']
        self.assertTrue(self.subject.checks_present())
        self.assertEqual([c for c in self.commands() if c[0] == 'nix'], before)
        for command in (['rustc', '-V'], ['cargo', 'nextest', '--version'], ['cargo', 'mend', '--version']):
            self.assertIn(command, self.commands())
        self.assertFalse(any(c[0] == 'nix' for c in self.commands()))
        self.change(mismatch=True)
        self.assertFalse(self.subject.checks_present())
        with self.assertRaisesRegex(box.BoxRefused, 'nextest box=cargo-nextest wrong natedev=cargo-nextest fixture release fixture'):
            self.subject.checks()
        self.change(mismatch=False)
        self.subject.checks()
        self.assertTrue(self.subject.checks_present())

    def test_checks_refuse_a_wrong_home_volume_and_an_inactive_remote_service(self) -> None:
        for values in ({'wrong_volume': True, 'active': True}, {'wrong_volume': False, 'active': False}):
            with self.subTest(values=values):
                self.change(joined=True, **values)
                with self.assertRaises(box.BoxRefused):
                    self.subject.checks()
                self.assertFalse(any(c[0] in ('rustc', 'cargo') for c in self.commands()))


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

    def test_idle_stop_deadline_stops_the_instance_and_refuses_without_a_proof(self) -> None:
        self.change(joined=True, self_stop=False)
        with self.assertRaisesRegex(box.BoxRefused, 'up stopped the instance'):
            self.subject.idle_stop()
        self.assertEqual(self.live()['ec2'], 'stopped')
        self.assertTrue(self.live()['forced_stop'])
        self.assertFalse(self.subject.idle_stop_present())
        self.change(ec2='running', self_stop=True, observations=0)
        self.subject.idle_stop()
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

    def test_routing_proof_requires_a_new_successful_aws_step_and_resumes(self) -> None:
        self.subject.activate()
        self.assertFalse(self.subject.routing_proof_present())
        self.change(proof_failure=True)
        with self.assertRaises(box.BoxRefused):
            self.subject.routing_proof()
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
        self.assertIn(str(Path(box.__file__).resolve().parent / 'proof_crate'), command)
        self.assertNotIn('cargo-liner', command)

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
