"""Exercise scoped lifecycle calls without a network or live deployment state."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
import base64
import fcntl
from io import StringIO
import json
from pathlib import Path
import subprocess
from typing import cast, override
from unittest import mock

from scripts.aws_target import aws_target, hana_box as box, hana_guest, hana_provider as provider, hana_receiver, deploy, ledger
from scripts.aws_target.control_test_support import TestClock
from scripts.aws_target.ledger_test_support import CostFixture
from scripts.lib import conf

REPOSITORY = Path(__file__).resolve().parents[2]
IDENTITY = box.DeploymentIdentity('fixture-deployment', 'fixture-instance', 'fixture-request', 'us-east-2')


@dataclass
class FakeAws:
    clock: TestClock
    state: str = 'stopped'
    userdata: str = ''
    failure: str = ''
    lose_response: str = ''
    status: str = 'instance-stopped-by-user'
    ownership: str = IDENTITY.deployment
    objects: dict[str, dict[str, object]] = field(default_factory=dict)
    calls: list[tuple[str, str, float]] = field(default_factory=list)
    environments: list[dict[str, str]] = field(default_factory=list)

    def __call__(self, arguments: Sequence[str], *, capture_output: bool, text: bool,
                 timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        _ = capture_output, text, check
        offset = arguments.index('--region') + 2
        service, operation = arguments[offset:offset + 2]
        self.calls.append((service, operation, timeout))
        if env is not None:
            self.environments.append(env)
        value: dict[str, object] = {}
        if self.failure == operation:
            return subprocess.CompletedProcess(list(arguments), 1, '', 'private-provider-id signed-secret')
        if operation == 'describe-instances':
            value = {'Reservations': [{'Instances': [{'InstanceId': IDENTITY.instance, 'SpotInstanceRequestId': IDENTITY.spot_request,
                'State': {'Name': self.state}, 'LaunchTime': 'fixture-launch',
                'Tags': [{'Key': 'HanaDeployment', 'Value': self.ownership}, {'Key': 'Workload', 'Value': 'hana'}]}]}]}
        elif operation == 'describe-spot-instance-requests':
            value = {'SpotInstanceRequests': [{'SpotInstanceRequestId': IDENTITY.spot_request, 'InstanceId': IDENTITY.instance,
                'Type': 'persistent', 'InstanceInterruptionBehavior': 'stop', 'Status': {'Code': self.status}}]}
        elif operation == 'export-credentials':
            value = {'AccessKeyId': 'fixture-access', 'SecretAccessKey': 'fixture-secret'}
        elif operation == 'modify-instance-attribute':
            self.userdata = arguments[arguments.index('--value') + 1]
        elif operation == 'describe-instance-attribute':
            value = {'UserData': {'Value': self.userdata}}
        elif operation == 'start-instances':
            self.state = 'running'
        elif operation == 'stop-instances' or operation == 'wait':
            self.state = 'stopped'
            self.clock.now += 1
        elif operation in ('put-object', 'get-object'):
            key = arguments[arguments.index('--key') + 1]
            if operation == 'put-object':
                self.objects[key] = ledger.mapping(cast(object, json.loads(Path(arguments[arguments.index('--body') + 1]).read_text())))
            elif key in self.objects:
                _ = Path(arguments[arguments.index('--key') + 2]).write_text(json.dumps(self.objects[key]))
            else:
                return subprocess.CompletedProcess(list(arguments), 1, '', 'NoSuchKey')
        else:
            raise AssertionError('Unexpected fake AWS operation: ' + operation)
        if self.lose_response == operation:
            self.lose_response = ''
            raise subprocess.TimeoutExpired('private-provider-id', timeout)
        return subprocess.CompletedProcess(list(arguments), 0, json.dumps(value), '')


class HanaProviderTests(CostFixture):
    clock: TestClock = TestClock()
    workload: Path = Path()

    def __init__(self, methodName: str = 'runTest') -> None:
        super().__init__(methodName)
        self.fake: FakeAws = FakeAws(self.clock)
        self.provider: provider.AwsHanaProvider = provider.AwsHanaProvider(aws_target.AwsCli('', '', 1, self.fake), Path(), '', 1)

    @override
    def setUp(self) -> None:
        super().setUp()
        self.clock = TestClock()
        self.workload = self.root / 'hana_aws.conf'
        _ = self.workload.write_text((REPOSITORY / 'config/hana_aws.conf').read_text())
        self.fake = FakeAws(self.clock)
        settings = conf.read(self.workload)
        self.provider = provider.AwsHanaProvider(aws_target.AwsCli('fixture-scaler', IDENTITY.region,
            settings.whole('aws_timeout_s'), self.fake), self.state, 'fixture-bucket',
            settings.whole('control_url_seconds'), clock=self.clock)

    def operation(self, action: box.Action) -> box.PendingOperation:
        return box.PendingOperation(action, 'fixture-' + action.value, self.clock.now, 1)

    def start(self) -> box.DemandStart:
        return box.DemandStart('fixture-start', self.clock.now, self.clock.now + conf.read(self.workload).whole('start_timeout_seconds'))

    def test_bootstrap_start_observe_publish_stop_and_policy_cover_the_same_calls(self) -> None:
        start = self.start()
        for action in (box.Action.BOOTSTRAP, box.Action.START):
            result = self.provider.step(IDENTITY, start, self.operation(action))
            self.assertIsInstance(result, box.OperationAccepted)
        descriptor = ledger.mapping(cast(object, json.loads(base64.b64decode(self.fake.userdata))))
        self.assertEqual(descriptor['start_token'], start.token)
        prefix = 'control/' + IDENTITY.deployment + '/' + start.token
        self.fake.objects[prefix + '/report.json'] = {'deployment': IDENTITY.deployment,
            'start_token': start.token, 'boot_id': 'fixture-boot', 'at': self.clock.now}
        observed = self.provider.step(IDENTITY, start, self.operation(box.Action.OBSERVE))
        self.assertIsInstance(observed, box.BoxObservation)
        assert isinstance(observed, box.BoxObservation)
        self.assertEqual(observed.boot, box.VerifiedBoot('fixture-boot', start.token))
        permit = hana_guest.WorkPermit(self.clock.now + conf.read(self.workload).whole('permit_max_seconds'), '1', 'fixture-boot')
        publication = box.PendingOperation(box.Action.PUBLISH, 'fixture-publication', self.clock.now, 1, permit, 0, 'fixture-execution')
        with self.assertRaises(provider.ProviderPending):
            _ = self.provider.step(IDENTITY, start, publication)
        self.fake.objects[prefix + '/report.json'].update(token=publication.token, generation=1, execution=publication.execution)
        self.assertIsInstance(self.provider.step(IDENTITY, start, publication), box.PermitAccepted)
        _ = self.provider.step(IDENTITY, start, self.operation(box.Action.STOP))
        self.assertEqual(self.fake.state, 'stopped')
        actions = {provider.PROVIDER_OPERATIONS[op] for service, op, _timeout in self.fake.calls if service != 'configure'}
        template = ledger.mapping(cast(object, json.loads(deploy.HANA_TEMPLATE.read_text())))
        policy = ledger.mapping(ledger.mapping(ledger.mapping(template['Resources'])['ScalerPolicy'])['Properties'])
        statements = ledger.rows(ledger.mapping(policy['PolicyDocument']), 'Statement')
        granted: set[str] = set()
        for row in statements:
            values = row['Action']
            assert isinstance(values, list)
            for action in cast(list[object], values):
                assert isinstance(action, str)
                granted.add(action)
        self.assertTrue(actions <= granted)
        self.assertEqual(set(provider.PROVIDER_OPERATIONS.values()), granted)
        self.assertTrue(all(timeout == conf.read(self.workload).whole('aws_timeout_s') for _, _, timeout in self.fake.calls))
        self.assertEqual([op for _, op, _ in self.fake.calls].count('put-object'), 2)

    def test_lost_start_response_recovers_without_another_start(self) -> None:
        operation, start = self.operation(box.Action.START), self.start()
        self.fake.lose_response = 'start-instances'
        with self.assertRaises(provider.ProviderPending):
            _ = self.provider.step(IDENTITY, start, operation)
        self.assertIsInstance(self.provider.recover(IDENTITY, start, operation), box.MutationSettled)
        _ = self.provider.step(IDENTITY, start, operation)
        self.assertEqual([op for _, op, _ in self.fake.calls].count('start-instances'), 1)

    def test_uncertain_stopped_start_remains_unresolved(self) -> None:
        operation, start = self.operation(box.Action.START), self.start()
        self.fake.failure = 'start-instances'
        with self.assertRaises(provider.ProviderPending):
            _ = self.provider.step(IDENTITY, start, operation)
        self.fake.failure = ''
        self.assertIsInstance(self.provider.recover(IDENTITY, start, operation), box.MutationUnresolved)

    def test_recovery_fences_a_call_that_has_not_started(self) -> None:
        operation, start = self.operation(box.Action.START), self.start()
        self.assertIsInstance(self.provider.recover(IDENTITY, start, operation), box.MutationSettled)
        with self.assertRaises(provider.ProviderPending):
            _ = self.provider.step(IDENTITY, start, operation)
        self.assertNotIn('start-instances', [op for _, op, _ in self.fake.calls])

    def test_in_flight_lock_preserves_an_unresolved_mutation(self) -> None:
        operation = self.operation(box.Action.START)
        folder = self.state / 'hana/provider'
        folder.mkdir(parents=True)
        with (folder / 'mutation.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertIsInstance(self.provider.recover(IDENTITY, self.start(), operation), box.MutationUnresolved)

    def test_bootstrap_recovery_reads_the_saved_user_data(self) -> None:
        operation, start = self.operation(box.Action.BOOTSTRAP), self.start()
        self.fake.lose_response = 'modify-instance-attribute'
        with self.assertRaises(provider.ProviderPending):
            _ = self.provider.step(IDENTITY, start, operation)
        self.assertIsInstance(self.provider.recover(IDENTITY, start, operation), box.MutationSettled)
        self.assertIn('describe-instance-attribute', [op for _, op, _ in self.fake.calls])

    def test_ownership_mismatch_and_errors_reveal_no_private_data(self) -> None:
        self.fake.ownership = 'other-deployment'
        with self.assertRaisesRegex(provider.ProviderPending, '^Hana instance ownership differs$'):
            _ = self.provider.step(IDENTITY, self.start(), self.operation(box.Action.OBSERVE))
        self.fake.failure = 'stop-instances'
        with self.assertRaisesRegex(provider.ProviderPending, '^Hana provider call failed: ec2:StopInstances$'):
            _ = self.provider.step(IDENTITY, self.start(), self.operation(box.Action.STOP))

    def test_scaler_environment_removes_inherited_admin_credentials(self) -> None:
        with mock.patch.dict('os.environ', {'AWS_ACCESS_KEY_ID': 'inherited-admin', 'AWS_SESSION_TOKEN': 'inherited-token'}):
            subject = provider.AwsHanaProvider.from_settings(self.settings, self.state, self.workload, execute=self.fake)
            _ = subject.step(IDENTITY, box.NoDemandStart(), self.operation(box.Action.OBSERVE))
        self.assertNotIn('AWS_ACCESS_KEY_ID', self.fake.environments[-1])
        self.assertNotIn('AWS_SESSION_TOKEN', self.fake.environments[-1])
        self.assertEqual(self.fake.environments[-1]['AWS_CONFIG_FILE'], '/dev/null')

    def test_receiver_checks_boot_generation_expiry_and_shutdown_fence(self) -> None:
        folder = self.root / 'guest'
        folder.mkdir()
        boot = self.root / 'boot-id'
        _ = boot.write_text('fixture-boot')
        paths = hana_guest.GuestPaths(folder, boot)
        settings = hana_guest.read_settings(conf.read(self.workload))
        descriptor: dict[str, object] = {'version': 1, 'deployment': IDENTITY.deployment, 'start_token': 'fixture-start',
            'expires': self.clock.now + conf.read(self.workload).whole('control_url_seconds')}
        control: dict[str, object] = {'deployment': IDENTITY.deployment, 'start_token': 'fixture-start', 'token': 'fixture-token',
            'expected_generation': 0, 'execution': 'fixture-execution',
            'permit': {'expires_at': self.clock.now + settings.permit_max_seconds, 'generation': '1', 'boot_id': 'fixture-boot'}}
        reply = hana_receiver.receive(settings, descriptor, control, paths=paths, clock=self.clock)
        self.assertEqual(reply['generation'], 1)
        self.assertEqual(hana_guest.read_identity(paths.generation), '1')
        self.assertEqual(hana_receiver.receive(settings, descriptor, control, paths=paths, clock=self.clock), reply)
        _ = paths.fence.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'shutdown is fenced'):
            _ = hana_receiver.receive(settings, descriptor, control, paths=paths, clock=self.clock)

    def test_receiver_program_compiles_without_repository_imports(self) -> None:
        rendered = hana_receiver.receiver_program()
        _ = compile(rendered, '<hana receiver>', 'exec')
        self.assertNotIn('scripts.', rendered)

    def test_no_approval_prints_only_the_plan_and_makes_no_external_call(self) -> None:
        subject = deploy.Deployment(self.settings, self.state, self.root / 'output', execute=self.fake)
        with mock.patch.object(conf, 'read_or_repository', return_value=conf.read(self.workload)), \
                mock.patch('sys.stdout', new_callable=StringIO) as output:
            self.assertFalse(subject.hana_setup())
        self.assertIn('persistent Spot', output.getvalue())
        self.assertIn('placement remains disabled', output.getvalue())
        self.assertEqual(self.fake.calls, [])
        self.assertFalse((self.state / 'hana/setup.json').exists())
