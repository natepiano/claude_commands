"""Exercise guarded deployment with a private AWS executable and no network."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import redirect_stdout
import base64
from datetime import UTC, datetime
from decimal import Decimal
import gzip
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from typing import ClassVar, cast, override
from unittest import mock
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mac_test'))

from scripts.aws_target import admin_secrets, aws_target, config as aws_config, deploy, ledger, measurement, remote
from scripts.aws_target.config import read_control_settings
from scripts.aws_target.ledger_test_support import CostFixture
from scripts.aws_target.measurement_test_support import DAY, narrow
from scripts.buildlog import sync
from scripts.lib import conf
from scripts.build_targets import target_config
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


FAKE_AWS = r'''import datetime, json, os, pathlib, sys
root = pathlib.Path(os.environ['DEPLOY_FIXTURE'])
args = sys.argv[1:]
service_index = args.index('--region') + 2
service = args[service_index]
operation = args[service_index + 1]
state_path = root / 'fake-stack.json'
state = json.loads(state_path.read_text()) if state_path.exists() else {}
clock = int((root / 'clock').read_text())
book = root / 'state/cost-ledger.json'
evidence = json.loads(book.read_text()) if book.exists() else {}
with (root / 'commands.jsonl').open('a') as output:
    output.write(json.dumps({'args': args if '--profile' in args else ['--profile', 'environment', *args], 'decision': evidence.get('decision', {}),
                             'compute_requests': evidence.get('compute_requests', [])}) + '\n')
def reply(value):
    print(json.dumps(value))
override = root / (operation + '.json')
if override.exists():
    reply(json.loads(override.read_text()))
    sys.exit(0)
keys_path = root / 'fake-keys.json'
keys = json.loads(keys_path.read_text()) if keys_path.exists() else ['old-key']
if service == 'sts' and operation == 'get-caller-identity':
    import configparser
    credentials = configparser.ConfigParser()
    credentials.read(os.environ['AWS_SHARED_CREDENTIALS_FILE'])
    profile = args[args.index('--profile') + 1]
    valid = credentials.get(profile, 'aws_access_key_id', fallback='') == 'new-key'
    if not valid or (root / 'fail-scaler').exists():
        print('The profile is not ready', file=sys.stderr)
        sys.exit(1)
    reply({'Arn': 'arn:aws:iam::123456789012:user/build-scaler'})
elif service == 'iam' and operation == 'create-access-key':
    keys.append('new-key')
    keys_path.write_text(json.dumps(keys))
    reply({'AccessKey': {'AccessKeyId': 'new-key', 'SecretAccessKey': 'fixture-secret-only-in-credentials'}})
elif service == 'iam' and operation == 'list-access-keys':
    reply({'AccessKeyMetadata': [{'AccessKeyId': key} for key in keys]})
elif service == 'iam' and operation == 'delete-access-key':
    keys.remove(args[args.index('--access-key-id') + 1])
    keys_path.write_text(json.dumps(keys))
    reply({})
elif service == 'cloudformation' and operation == 'describe-stacks':
    if not state and (root / 'review-stack').exists():
        reply({'Stacks': [{'StackStatus': 'REVIEW_IN_PROGRESS'}]})
        sys.exit(0)
    if not state or state.get('stack_name') != args[args.index('--stack-name') + 1]:
        print('ValidationError: Stack does not exist', file=sys.stderr)
        sys.exit(1)
    reply({'Stacks': [{'StackStatus': 'UPDATE_COMPLETE', 'Outputs': [
        {'OutputKey': key, 'OutputValue': value} for key, value in state['outputs'].items()]}]})
elif service == 'cloudformation' and operation == 'deploy':
    if 'EnableCompute=true' in args and (root / 'fail-compute').exists():
        print('Compute deploy failed', file=sys.stderr)
        sys.exit(1)
    start = args.index('--parameter-overrides') + 1
    params = dict(word.split('=', 1) for word in args[start:] if '=' in word)
    outputs = {'ComputeEnabled': params['EnableCompute'], 'BudgetName': 'fixture-monthly',
               'ExistingDataInput': params['ExistingDataVolumeId']}
    if params['EnableCompute'] == 'true':
        outputs.update(InstanceId='i-fixture', DataVolumeId=params['ExistingDataVolumeId'] or 'vol-created')
    state = {'outputs': outputs, 'stack_name': args[args.index('--stack-name') + 1]}
    state_path.write_text(json.dumps(state))
    if params['EnableCompute'] == 'false' and (root / 'slow-foundation').exists():
        (root / 'clock').write_text(str(clock + 301))
    reply({})
elif operation == 'describe-images':
    reply({'Images': [{'ImageId': 'ami-fixture', 'OwnerId': '123456789012', 'Architecture': 'x86_64',
                       'State': 'available', 'Name': 'nixos-26.05-x86_64', 'RootDeviceName': '/dev/xvda'}]})
elif operation == 'describe-subnets':
    reply({'Subnets': [{'VpcId': 'vpc-fixture', 'AvailabilityZone': 'us-east-1a'}]})
elif operation == 'describe-volumes':
    reply({'Volumes': [{'AvailabilityZone': 'us-east-1a', 'Encrypted': True, 'State': 'available',
                        'Size': 500, 'VolumeType': 'gp3', 'Iops': 6000, 'Throughput': 500,
                        'Tags': [{'Key': 'Project', 'Value': 'fixture-build'}]}]})
elif operation == 'describe-instances':
    if (root / 'missing-instance').exists():
        print('InvalidInstanceID.NotFound: instance does not exist', file=sys.stderr)
        sys.exit(1)
    reply({'Reservations': [{'Instances': [{'InstanceId': 'i-fixture', 'LaunchTime': 'fixture-boot',
                                            'State': {'Name': state.get('instance_state', 'stopped')}}]}]})
elif operation == 'start-instances':
    assert args[args.index('--instance-ids') + 1] == state['outputs']['InstanceId']
    state['instance_state'] = 'pending'
    state_path.write_text(json.dumps(state))
    reply({'StartingInstances': [{'InstanceId': 'i-fixture', 'CurrentState': {'Name': 'pending'}}]})
elif operation == 'describe-spot-price-history':
    reply({'SpotPriceHistory': [{'Timestamp': datetime.datetime.fromtimestamp(clock, datetime.UTC).isoformat(),
                                 'SpotPrice': '0.1'}]})
elif operation == 'get-cost-and-usage':
    period = args[args.index('--time-period') + 1]
    assert all(len(part.split('=')[1]) == 10 for part in period.split(','))
    reply({'ResultsByTime': [{'Total': {'UnblendedCost': {'Amount': '0.1', 'Unit': 'USD'}}}]})
else:
    raise AssertionError((service, operation))
'''


class DeploymentTests(CostFixture):
    now: int = 0
    bundle: Path = Path()
    credentials: Path = Path()
    notification_file: Path = Path()
    price_file: Path = Path()
    patch_sections: ClassVar[dict[str, str]] = {}

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.settings.write_text((deploy.TEMPLATE.parents[3] / 'config/aws_target.conf').read_text())
        self.now = int(datetime.fromisoformat(DAY + "T23:59:00+00:00").timestamp())
        self.bundle = self.root / 'bundle'
        self.bundle.mkdir()
        bootstrap = self.bundle / 'bootstrap.sh'
        _ = bootstrap.write_text('#!/usr/bin/env bash\ntrue\n')
        hosts = self.root / 'nixos/hosts'
        hosts.mkdir(parents=True)
        modules = hosts.parent / 'modules/linux'
        modules.mkdir(parents=True)
        _ = (modules / 'tailscale.nix').write_text(
            '# The module enables Tailscale SSH.\n{ services.tailscale = {\n' +
            '  enable = true;\n  extraSetFlags = [ "--ssh" ];\n  openFirewall = true;\n}; }\n')
        bootstrap_template = hosts / 'bootstrap.sh.in'
        _ = bootstrap_template.write_text('\n'.join((
            '#!/usr/bin/env bash', '# services.amazon-ssm-agent.enable = true',
            'systemd-run --unit=aws-build-first-boot-stop --on-active=@IDLE_STOP_SECONDS@s systemctl poweroff',
            '# udevadm wait --timeout=@DATA_VOLUME_WAIT_SECONDS@',
            '# @SCRIPTS_SOURCE_URL_JSON@ @SCRIPTS_SOURCE_SHA256_JSON@ @SCRIPTS_SOURCE_COMMIT_JSON@',
            '# @REMOTE_SETTINGS_JSON@ @EXISTING_DATA_VOLUME_JSON@', '# @INPUT@', '',
        )))
        _ = (hosts / 'aws-build-base.nix').write_text(
            '# The base shares the transport module.\n{ ... }: { imports = [ ../modules/linux/tailscale.nix ]; }')
        self.notification_file = self.root / 'notification-email'
        _ = self.notification_file.write_text('build@example.invalid\n')
        self.text_setting('notification_email_file', str(self.notification_file))
        self.price_file = self.root / 'deployment-price'
        self.text_setting('deployment_price_file', str(self.price_file))
        self.write_price()
        values = {
            'admin_secret_file': str(self.root / 'admin.gpg'), 'offload': 'off', 'deployment_profile': 'build-admin', 'stack_name': 'fixture-build',
            'ami_id': 'ami-fixture', 'ami_owner_id': '123456789012', 'nixos_release': '26.05',
            'availability_zone': 'us-east-1a', 'vpc_id': 'vpc-fixture', 'subnet_id': 'subnet-fixture',
            'root_device_name': '/dev/xvda', 'scaler_user_name': 'build-scaler',
            'bootstrap_template_path': str(bootstrap_template),
            'scripts_source_url': 'https://example.invalid/public/commit.tar.gz',
            'scripts_source_sha256': 'sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=',
            'scripts_source_commit': 'a' * 40,
        }
        values.update({
            'root_volume_gib': '32', 'data_volume_gib': '500', 'data_volume_iops': '6000',
            'data_volume_throughput': '500', 'spot_max_usd_per_hour': '0.5',
            'budget_notice_low_percent': '50', 'budget_notice_high_percent': '80', 'budget_stop_percent': '100',
            'deployment_timeout_s': '30', 'preflight_max_age_s': '300', 'fixed_usd_per_month': '0.001',
            'retained_storage_usd_per_month': '0', 'root_storage_usd_per_month': '0',
            'ipv4_usd_per_month': '0', 'gp3_iops_usd_per_month': '0', 'gp3_throughput_usd_per_month': '0',
            'region': 'us-east-1',
            'cost_explorer_tag_key': 'Project', 'cost_explorer_tag_value': 'fixture-build', 'accounting_zone': 'UTC',
        })
        for key, value in values.items():
            self.replace(key, value)
        _ = cast(object, self.enterContext(mock.patch.object(admin_secrets.AdminSecretSession, 'get', return_value=
            admin_secrets.AdminCredentials('fixture-admin-id', 'fixture-admin-secret', 'fixture-client', 'fixture-oauth-secret'))))
        self.credentials = self.root / 'credentials'
        _ = self.credentials.write_text('')
        self.credentials.chmod(0o600)
        bin_path = self.root / 'bin'
        bin_path.mkdir()
        self.fake_commands(f'#!{sys.executable}\n' + FAKE_AWS, 'aws')
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            'PATH': f'{bin_path}{os.pathsep}{os.environ["PATH"]}', 'DEPLOY_FIXTURE': str(self.root),
            'AWS_SHARED_CREDENTIALS_FILE': str(self.credentials),
        })))
        _ = cast(object, self.enterContext(mock.patch.object(target_config, 'ssh_target_state_directory', return_value=self.state)))
        self.set_clock(self.now)
        # These tests cover the resource stages. test_box covers the box stages.
        _ = cast(object, self.enterContext(mock.patch.object(deploy.Deployment, '_box_stages', return_value=())))

    @override
    def replace(self, key: str, value: str) -> None:
        if key in ('instance_id', 'existing_data_volume_id'):
            saved: dict[str, object] = aws_config.deployment_record(self.state)
            if not saved:
                saved = {'stack': 'previous-build', 'region': 'us-east-1',
                         'outputs': {'InstanceId': 'i-fixture'}}
            outputs = ledger.mapping(saved.get('outputs'))
            if key == 'instance_id':
                outputs['InstanceId'] = value
            else:
                outputs['ExistingDataInput'] = value
                outputs['DataVolumeId'] = value
            saved['outputs'] = outputs
            _ = (self.state / 'deployment.json').write_text(json.dumps(saved))
            return
        super().replace(key, value)

    def set_clock(self, at: int) -> None:
        self.now = at
        _ = (self.root / 'clock').write_text(str(at))

    def write_price(self, price: str = '0.1') -> None:
        _ = self.price_file.write_text(f'deployment_price_usd_per_hour={price}\n'
                                       + f'deployment_price_at={self.now}\n')

    def deployment(self) -> deploy.Deployment:
        return deploy.Deployment(self.settings, self.state, self.bundle, clock=lambda: self.now,
                                 notify=lambda _event: True)

    def commands(self, operation: str = '') -> list[dict[str, object]]:
        path = self.root / 'commands.jsonl'
        rows = [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []
        return [row for row in rows if not operation or operation in cast(list[str], row['args'])]

    def prepare_stopped_instance(self) -> int:
        self.set_clock(int(datetime(2026, 1, 15, 12, tzinfo=UTC).timestamp()))
        self.write_price()
        for key, value in (('fallback_usd_per_hour', '1'),
                           ('price_max_age_seconds', '86400'), ('observation_max_age_seconds', '86400'),
                           ('cost_explorer_read_usd', '0')):
            self.replace(key, value)
        ledger.CostLedger(self.state).observe(ledger.InstanceObservation(self.now, 'stopped', 'prior stopped evidence'))
        _ = self.deployment().prepare()
        return self.now

    def failing_compute(self, failure: str) -> deploy.Deployment:
        compute_returned = False

        def execute(arguments: Sequence[str], *, capture_output: bool, text: bool,
                    timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            nonlocal compute_returned
            operation = arguments[arguments.index('--region') + 3]
            if compute_returned and ((failure in ('stack', 'timeout') and operation == 'describe-stacks')
                                     or (failure == 'instance' and operation == 'describe-instances')):
                return subprocess.CompletedProcess(arguments, 1, '', 'the requested observation is unavailable')
            result = aws_target.execute_command(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check, env=env)
            if operation == 'deploy' and 'EnableCompute=true' in arguments:
                compute_returned = True
                if failure == 'timeout':
                    raise subprocess.TimeoutExpired(arguments, timeout)
            return result

        return deploy.Deployment(self.settings, self.state, self.bundle, execute=execute,
                                 clock=lambda: self.now, notify=lambda _event: True)

    def instance_intervals(self, at: int) -> list[tuple[int, int, Decimal, int]]:
        costs = ledger.costs_from_evidence(ledger.CostLedger(self.state).read_snapshot(),
                                           aws_config.read_settings(self.settings), at, current_window=False)
        return [(r.start, r.end, r.usd, r.confirmed_uptime_s) for r in costs.intervals if r.kind == 'instance']

    def assert_compute_failure_accounting(self, failure: str, recovery: str) -> None:
        start = self.prepare_stopped_instance()
        subject = self.failing_compute(failure)
        with self.assertRaises((subprocess.TimeoutExpired, aws_target.AwsUnavailable)):
            _ = subject.apply(approve_resources=True)
        book = ledger.CostLedger(self.state)
        self.assertEqual(len(book.pending_compute_requests()), 1)
        self.assertEqual((self.state / 'deployment.json').exists(), failure == 'instance')
        self.set_clock(start + 7200)
        self.assertEqual(self.instance_intervals(self.now), [(start, self.now, Decimal('2'), 0)])
        self.write_price()
        with self.assertRaises(aws_target.AwsUnavailable):
            _ = subject.prepare()
        self.assertEqual(len(book.pending_compute_requests()), 1)
        self.assertEqual(self.instance_intervals(self.now), [(start, self.now, Decimal('2'), 0)])
        _ = (self.root / 'describe-instances.json').write_text(json.dumps({
            'Reservations': [{'Instances': [{'InstanceId': 'i-fixture', 'LaunchTime': 'recovered-boot',
                                            'State': {'Name': 'running'}}]}]}))
        recovered = self.deployment()
        if recovery != 'prepare':
            with self.assertRaisesRegex(deploy.DeploymentRefused, 'expired'):
                if recovery == 'apply':
                    _ = recovered.apply(approve_resources=True)
                else:
                    _ = recovered.setup_start(approve_resources=True)
            self.assertEqual(book.pending_compute_requests(), ())
        _ = recovered.prepare()
        self.assertEqual(book.pending_compute_requests(), ())
        decision = ledger.mapping(remote.read_record(self.bundle / 'preflight.json')['decision'])
        self.assertEqual([(ledger.instant(r, 'start'), ledger.instant(r, 'end'), Decimal(ledger.text(r, 'usd')))
                          for r in ledger.rows(decision, 'intervals') if r.get('kind') == 'instance'],
                         [(start, self.now, Decimal('2'))])
        self.assertEqual(self.instance_intervals(self.now + 3600),
                         [(self.now, self.now + 3600, Decimal('0.1'), 3600),
                          (start, self.now, Decimal('2'), 0)])

    def test_compute_command_starts_with_durable_unconfirmed_accounting(self) -> None:
        start = self.prepare_stopped_instance()
        _ = self.deployment().apply(approve_resources=True)
        compute = self.commands('deploy')[-1]
        requests = ledger.rows(compute, 'compute_requests')
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]['at'], start)
        self.assertEqual(requests[0]['stack'], conf.read(self.settings).text('stack_name'))
        self.assertEqual(requests[0]['region'], conf.read(self.settings).text('region'))
        self.assertEqual(ledger.mapping(requests[0]['resolution'])['outcome'], 'unconfirmed')

    def test_compute_timeout_keeps_exact_instance_costs_until_preparation_recovers(self) -> None:
        self.assert_compute_failure_accounting('timeout', 'prepare')

    def test_stack_lookup_failure_keeps_exact_instance_costs_until_apply_recovers(self) -> None:
        self.assert_compute_failure_accounting('stack', 'apply')

    def test_instance_lookup_failure_saves_identity_and_costs_until_setup_recovers(self) -> None:
        self.assert_compute_failure_accounting('instance', 'setup')

    def test_old_terminated_instance_cannot_end_a_replacement_request(self) -> None:
        start = self.prepare_stopped_instance()
        subject = self.deployment()
        outputs = subject.apply(approve_resources=True)
        (self.root / 'fake-stack.json').unlink()
        self.replace('stack_name', 'replacement-build')
        self.replace('existing_data_volume_id', outputs['DataVolumeId'])
        _ = subject.prepare()
        with self.assertRaises(subprocess.TimeoutExpired):
            _ = self.failing_compute('timeout').apply(approve_resources=True)
        book = ledger.CostLedger(self.state)
        book.observe(ledger.InstanceObservation(start + 60, 'terminated', 'old instance deletion'))
        self.assertEqual(self.instance_intervals(start + 3600), [(start, start + 3600, Decimal('1'), 0)])
        self.set_clock(start + 3600)
        self.write_price()
        _ = (self.root / 'describe-instances.json').write_text(json.dumps({
            'Reservations': [{'Instances': [{'InstanceId': outputs['InstanceId'], 'LaunchTime': 'old-boot',
                                            'State': {'Name': 'terminated'}}]}]}))
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'previous instance'):
            _ = subject.prepare()
        self.assertEqual(len(book.pending_compute_requests()), 1)
        self.assertEqual(self.instance_intervals(self.now), [(start, self.now, Decimal('1'), 0)])
        stack_path = self.root / 'fake-stack.json'
        stack = remote.read_record(stack_path)
        ledger.mapping(stack['outputs'])['InstanceId'] = 'i-new'
        _ = stack_path.write_text(json.dumps(stack))
        _ = (self.root / 'describe-instances.json').write_text(json.dumps({
            'Reservations': [{'Instances': [{'InstanceId': 'i-new', 'LaunchTime': 'replacement-boot',
                                            'State': {'Name': 'running'}}]}]}))
        self.set_clock(start + 7200)
        self.write_price()
        _ = subject.prepare()
        self.assertEqual(ledger.mapping(remote.read_record(self.state / 'deployment.json')['outputs'])['InstanceId'], 'i-new')
        self.assertEqual(book.pending_compute_requests(), ())
        self.assertEqual(self.instance_intervals(self.now + 3600),
                         [(self.now, self.now + 3600, Decimal('0.1'), 3600), (start, self.now, Decimal('2'), 0)])

    def test_confirmed_absence_ends_only_the_unconfirmed_request_interval(self) -> None:
        start = self.prepare_stopped_instance()
        with self.assertRaises(subprocess.TimeoutExpired):
            _ = self.failing_compute('timeout').apply(approve_resources=True)
        (self.root / 'fake-stack.json').unlink()
        self.set_clock(start + 3600)
        self.write_price()
        _ = self.deployment().prepare()
        self.assertFalse(ledger.CostLedger(self.state).pending_compute_requests())
        self.assertEqual(self.instance_intervals(self.now + 3600), [(start, self.now, Decimal('1'), 0)])

    def test_compute_request_preserves_conflict_checks_for_confirmed_observations(self) -> None:
        start = self.prepare_stopped_instance()
        book = ledger.CostLedger(self.state)
        book.record_compute_request(ledger.ComputeRequest(start, 'fixture-build', 'us-east-1', Decimal('0.5')))
        with self.assertRaisesRegex(ledger.LedgerUnavailable, 'conflicting instance observations'):
            book.observe(ledger.InstanceObservation(start, 'running', 'contradictory confirmed state'))
        self.assertEqual(book.read_snapshot().observations[-1].state, 'stopped')
        self.assertEqual(self.instance_intervals(start + 3600), [(start, start + 3600, Decimal('1'), 0)])

    def prepare_running_costs(self) -> tuple[int, ledger.CostLedger]:
        start = int(datetime(2026, 1, 15, tzinfo=UTC).timestamp())
        self.costs({'fallback_usd_per_hour': '0.10', 'fixed_usd_per_month': '300',
                    'price_max_age_seconds': '86400', 'observation_max_age_seconds': '86400',
                    'cost_explorer_read_usd': '0'})
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(start, (ledger.PricedResource('storage', Decimal('300')),),
                                                        'retained storage'))
        book.record_price(ledger.PriceSample(start, Decimal('0.483'), 'fresh Spot price'))
        book.observe(ledger.InstanceObservation(start, 'running', 'confirmed running instance'))
        self.set_clock(start + 23 * 3600)
        return start, book

    def test_unconfirmed_request_adds_costs_without_reversing_a_limit_refusal(self) -> None:
        start, book = self.prepare_running_costs()
        before = book.guard(self.settings, 'deployment', self.now, notify=lambda _event: True)
        self.assertIsInstance(before, ledger.SpendingRefused)
        self.assertIsInstance(before.evidence.costs, ledger.CostEstimate)
        if not isinstance(before.evidence.costs, ledger.CostEstimate):
            self.fail('the guard must price the running instance')
        book.record_compute_request(ledger.ComputeRequest(start, 'fixture-build', 'us-east-1', Decimal('0.5')))
        after = book.guard(self.settings, 'deployment', self.now, notify=lambda _event: True)
        self.assertIsInstance(after, ledger.SpendingRefused)
        self.assertIsInstance(after.evidence.costs, ledger.CostEstimate)
        if not isinstance(after.evidence.costs, ledger.CostEstimate):
            self.fail('the guard must price the running instance and request')
        request_usd = Decimal('0.5') * Decimal(23)
        self.assertGreaterEqual(after.evidence.costs.daily.usd, before.evidence.costs.daily.usd + request_usd)
        self.assertGreaterEqual(after.evidence.costs.monthly.usd, before.evidence.costs.monthly.usd + request_usd)
        self.assertEqual([r for r in after.evidence.costs.intervals if r.source != 'unconfirmed compute request'],
                         list(before.evidence.costs.intervals))

    def test_unconfirmed_request_keeps_confirmed_running_uptime(self) -> None:
        start, book = self.prepare_running_costs()
        before = self.instance_intervals(self.now)
        book.record_compute_request(ledger.ComputeRequest(start, 'fixture-build', 'us-east-1', Decimal('0.5')))
        after = self.instance_intervals(self.now)
        self.assertEqual(sum(r[3] for r in before), 23 * 3600)
        self.assertEqual(sum(r[3] for r in after), sum(r[3] for r in before))
        self.assertEqual(after, [*before, (start, self.now, Decimal('11.5'), 0)])

    def test_request_price_keeps_the_submitted_cap_after_live_cap_changes(self) -> None:
        start = self.prepare_stopped_instance()
        self.replace('fallback_usd_per_hour', '0.10')
        _ = self.deployment().prepare()
        with self.assertRaises(subprocess.TimeoutExpired):
            _ = self.failing_compute('timeout').apply(approve_resources=True)
        compute = self.commands('deploy')[-1]
        arguments = cast(list[str], compute['args'])
        submitted_cap = Decimal(next(word.partition('=')[2] for word in arguments if word.startswith('SpotMaxPrice=')))
        self.assertEqual(submitted_cap, Decimal('0.5'))
        self.assertEqual(ledger.CostLedger(self.state).pending_compute_requests()[0].spot_max_usd_per_hour, submitted_cap)
        self.set_clock(start + 25 * 3600)
        midnight = int(datetime(2026, 1, 16, tzinfo=UTC).timestamp())
        for live_cap in ('0.05', '2'):
            with self.subTest(live_cap=live_cap):
                self.replace('spot_max_usd_per_hour', live_cap)
                costs = ledger.costs_from_evidence(ledger.CostLedger(self.state).read_snapshot(),
                                                   aws_config.read_settings(self.settings), self.now,
                                                   current_window=False)
                requests = [r for r in costs.intervals if r.source == 'unconfirmed compute request']
                self.assertEqual([(r.start, r.end, r.usd, r.confirmed_uptime_s) for r in requests],
                                 [(start, midnight, Decimal('6'), 0), (midnight, self.now, Decimal('6.5'), 0)])

    def up_output(self, subject: deploy.Deployment) -> tuple[bool, str]:
        output = StringIO()
        with redirect_stdout(output):
            success = subject.up(approve_resources=True)
        return success, output.getvalue()

    def test_up_creates_each_stage_and_the_second_run_only_checks_live_state(self) -> None:
        _ = self.credentials.write_text('[unrelated]\naws_access_key_id=other-key\naws_secret_access_key=other-value\n')
        self.price_file.unlink()
        settings = self.settings.read_bytes()
        success, output = self.up_output(self.deployment())
        self.assertTrue(success, output)
        self.assertEqual(output.splitlines(), ['price: done', 'bootstrap: done', 'stacks: done', 'scaler-key: done'])
        commands = self.commands()
        operations = [cast(list[str], row['args'])[5] for row in commands]
        self.assertLess(operations.index('describe-spot-price-history'), operations.index('deploy'))
        self.assertLess(operations.index('deploy'), operations.index('create-access-key'))
        self.assertEqual(self.credentials.stat().st_mode & 0o777, 0o600)
        self.assertIn('other-key', self.credentials.read_text())
        self.assertIn('[build-scaler]', self.credentials.read_text())
        self.assertEqual(json.loads((self.root / 'fake-keys.json').read_text()), ['new-key'])
        before = len(self.commands())
        success, output = self.up_output(self.deployment())
        self.assertTrue(success, output)
        self.assertEqual(output.splitlines(), ['price: present', 'bootstrap: present', 'stacks: present', 'scaler-key: present'])
        later = self.commands()[before:]
        self.assertEqual(sum('describe-spot-price-history' in cast(list[str], row['args']) for row in later), 1)
        for row in later:
            self.assertFalse({'deploy', 'create-access-key', 'delete-access-key', 'list-access-keys'} & set(cast(list[str], row['args'])))
        self.assertEqual(self.settings.read_bytes(), settings)
        self.assertNotIn('fixture-secret-only-in-credentials', output)
        for file in self.state.rglob('*.json'):
            self.assertNotIn('fixture-secret-only-in-credentials', file.read_text())
        results = remote.read_record(self.state / 'up.json')
        self.assertEqual(set(results), {'price', 'bootstrap', 'stacks', 'scaler-key'})
        self.assertTrue(all(ledger.mapping(result)['status'] == 'present' for result in results.values()))

    def test_up_retries_failed_compute_without_a_second_foundation(self) -> None:
        failure = self.root / 'fail-compute'
        _ = failure.write_text('fail')
        success, output = self.up_output(self.deployment())
        self.assertFalse(success)
        self.assertIn('stacks: refused:', output)
        self.assertNotIn('scaler-key:', output)
        failure.unlink()
        success, output = self.up_output(self.deployment())
        self.assertTrue(success, output)
        foundations = [row for row in self.commands('deploy') if 'EnableCompute=false' in cast(list[str], row['args'])]
        self.assertEqual(len(foundations), 1)
        self.assertEqual(len(self.commands('create-access-key')), 1)
        self.assertFalse(ledger.CostLedger(self.state).pending_compute_requests())

    def test_review_in_progress_stack_has_no_resources_to_reconcile(self) -> None:
        _ = (self.root / 'review-stack').write_text('empty change set')
        success, output = self.up_output(self.deployment())
        self.assertTrue(success, output)
        self.assertEqual(len(self.commands('deploy')), 2)

    def test_foundation_wait_does_not_expire_unchanged_compute_preparation(self) -> None:
        _ = (self.root / 'slow-foundation').write_text('slow')
        subject = deploy.Deployment(self.settings, self.state, self.bundle,
            clock=lambda: float((self.root / 'clock').read_text()), notify=lambda _event: True)
        success, output = self.up_output(subject)
        self.assertTrue(success, output)
        self.assertEqual(len(self.commands('deploy')), 2)
        self.assertEqual(int((self.root / 'clock').read_text()), self.now + 301)

    def test_unconfirmed_scaler_key_is_deleted_and_never_printed(self) -> None:
        self.replace('scaler_key_check_attempts', '2')
        self.replace('scaler_key_check_delay_s', '0.001')
        before = self.credentials.read_bytes()
        _ = (self.root / 'fail-scaler').write_text('fail')
        success, output = self.up_output(self.deployment())
        self.assertFalse(success)
        self.assertIn('scaler-key: refused:', output)
        self.assertNotIn('fixture-secret-only-in-credentials', output)
        self.assertEqual(self.credentials.read_bytes(), before)
        self.assertEqual(json.loads((self.root / 'fake-keys.json').read_text()), ['old-key'])
        self.assertEqual(len(self.commands('get-caller-identity')), 3)
        self.assertEqual(len(self.commands('delete-access-key')), 1)

    def test_up_refuses_without_approval_before_any_price_or_resource_read(self) -> None:
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'approve-resources'):
            _ = self.deployment().up()
        self.assertEqual(self.commands(), [])

    def test_price_command_writes_private_state_for_the_selected_zone_and_admin_environment(self) -> None:
        path = self.root / '.config/aws-build/deployment-price'
        self.replace('deployment_price_file', '~/.config/aws-build/deployment-price')
        settings = self.settings.read_bytes()
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    path.chmod(0o644)
                output = StringIO()
                before = int(time.time())
                with (mock.patch.object(sys, 'argv', ['deploy.py', 'price', '--settings', str(self.settings)]),
                      redirect_stdout(output)):
                    self.assertEqual(deploy.main(), 0)
                after = int(time.time())
                result = ledger.mapping(cast(object, json.loads(output.getvalue())))
                at = ledger.instant(result, 'deployment_price_at')
                self.assertGreaterEqual(at, before)
                self.assertLessEqual(at, after)
                self.assertEqual(result['deployment_price_usd_per_hour'], '0.1')
                self.assertEqual(result['file'], str(path))
                self.assertEqual(aws_config.deployment_price(self.settings, at), (at, Decimal('0.1')))
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(self.settings.read_bytes(), settings)
        calls = self.commands()
        self.assertEqual(len(calls), 2)
        for call in calls:
            args = cast(list[str], call['args'])
            self.assertEqual(args[args.index('--profile') + 1], 'environment')
            self.assertEqual(args[args.index('--region') + 1], 'us-east-1')
            self.assertIn('describe-spot-price-history', args)
            self.assertEqual(args[args.index('--instance-types') + 1], 'm7a.4xlarge')
            self.assertEqual(args[args.index('--availability-zone') + 1], 'us-east-1a')
            self.assertEqual(args[args.index('--product-descriptions') + 1], 'Linux/UNIX')
            self.assertIn('--start-time', args)

    def test_price_command_requires_private_credentials_before_any_aws_read(self) -> None:
        self.credentials.chmod(0o644)
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'AWS_SHARED_CREDENTIALS_FILE'):
            _ = self.deployment().price()
        self.assertEqual(self.commands(), [])

    def test_price_command_preserves_saved_price_after_an_invalid_reply(self) -> None:
        saved = self.price_file.read_bytes()
        for prices in ([], [{'SpotPrice': '0'}], [{'SpotPrice': '-1'}], [{'SpotPrice': 'NaN'}]):
            with self.subTest(prices=prices):
                _ = (self.root / 'describe-spot-price-history.json').write_text(json.dumps({'SpotPriceHistory': prices}))
                with self.assertRaises(ValueError):
                    _ = self.deployment().price()
                self.assertEqual(self.price_file.read_bytes(), saved)

    def test_prepare_refuses_a_missing_price_file_with_the_refresh_command(self) -> None:
        self.price_file.unlink()
        with self.assertRaisesRegex(ValueError, 'deploy.py price') as failure:
            _ = self.deployment().prepare()
        self.assertIn('deployment_price_file', str(failure.exception))
        self.assertIn(str(self.price_file), str(failure.exception))
        self.assertEqual(self.commands(), [])

    def test_prepare_refuses_price_paths_inside_the_public_repository(self) -> None:
        tracked_path = Path(__file__).resolve()
        self.price_file.unlink()
        self.price_file.symlink_to(tracked_path)
        for path in (tracked_path, self.price_file):
            with self.subTest(path=path):
                self.replace('deployment_price_file', str(path))
                with self.assertRaisesRegex(ValueError, 'outside the public repository') as failure:
                    _ = self.deployment().prepare()
                self.assertIn('deployment_price_file', str(failure.exception))
                self.assertIn(str(tracked_path), str(failure.exception))
        self.assertEqual(self.commands(), [])

    def test_prepare_refuses_a_stale_price_file(self) -> None:
        self.set_clock(self.now + aws_config.read_settings(self.settings).price_max_age_seconds + 1)
        with self.assertRaisesRegex(ValueError, 'fresh positive public price'):
            _ = self.deployment().prepare()
        self.assertEqual(self.commands(), [])

    def test_bootstrap_renders_without_a_price_file(self) -> None:
        self.price_file.unlink()
        rendered = self.reviewed_bundle()
        self.assertIn('aws-build-first-boot-stop', rendered)
        self.assertIn('/etc/aws-build/remote.conf', rendered)
        self.assertEqual(self.commands(), [])

    def test_apply_and_setup_start_require_the_saved_price_after_preparation(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        _ = subject.apply(approve_resources=True)
        self.price_file.unlink()
        for action in (subject.apply, subject.setup_start):
            with self.subTest(action=action.__name__):
                with self.assertRaisesRegex(ValueError, 'deploy.py price'):
                    _ = action(approve_resources=True)
        self.assertEqual(len(self.commands('deploy')), 2)
        self.assertEqual(self.commands('start-instances'), [])

    def test_bootstrap_renders_without_a_notification_address_file(self) -> None:
        self.notification_file.unlink()
        rendered = self.reviewed_bundle()
        self.assertIn('aws-build-first-boot-stop', rendered)
        self.assertIn('/etc/aws-build/remote.conf', rendered)

    def test_prepare_refuses_missing_or_unreadable_notification_files(self) -> None:
        self.notification_file.unlink()
        for unreadable in (False, True):
            with self.subTest(unreadable=unreadable):
                if unreadable:
                    self.notification_file.mkdir()
                with self.assertRaises((deploy.DeploymentRefused, ValueError)) as failure:
                    _ = self.deployment().prepare()
                self.assertIn('notification_email_file', str(failure.exception))
                self.assertIn(str(self.notification_file), str(failure.exception))

    def test_prepare_refuses_invalid_notification_content_without_printing_it(self) -> None:
        for content in ('', ' \n', 'build example@example.invalid', 'build.example.invalid',
                        'build@example.invalid\nother@example.invalid\n'):
            with self.subTest(content=content):
                _ = self.notification_file.write_text(content)
                with self.assertRaises((deploy.DeploymentRefused, ValueError)) as failure:
                    _ = self.deployment().prepare()
                message = str(failure.exception)
                self.assertIn('notification_email_file', message)
                self.assertIn(str(self.notification_file), message)
                if content.strip():
                    self.assertNotIn(content.strip(), message)

    def test_prepare_refuses_notification_paths_inside_the_public_repository(self) -> None:
        tracked_path = Path(__file__).resolve()
        self.notification_file.unlink()
        self.notification_file.symlink_to(tracked_path)
        for path in (tracked_path, self.notification_file):
            with self.subTest(path=path):
                self.replace('notification_email_file', str(path))
                with self.assertRaises((deploy.DeploymentRefused, ValueError)) as failure:
                    _ = self.deployment().prepare()
                self.assertIn('notification_email_file', str(failure.exception))
                self.assertIn(str(path.resolve()), str(failure.exception))

    def test_prepare_uses_the_private_notification_address_in_saved_parameters(self) -> None:
        self.replace('notification_email_file', '~/notification-email')
        _ = self.notification_file.write_text('  build@example.invalid\n')
        prepared = self.deployment().prepare()
        self.assertEqual(prepared.parameters['NotificationEmail'], 'build@example.invalid')
        saved = cast(list[dict[str, str]], json.loads((self.bundle / 'parameters.json').read_text()))
        self.assertEqual([row['ParameterValue'] for row in saved if row['ParameterKey'] == 'NotificationEmail'],
                         ['build@example.invalid'])
        _ = self.notification_file.write_text('other@example.invalid\n')
        updated = self.deployment().prepare()
        self.assertEqual(updated.parameters['NotificationEmail'], 'other@example.invalid')
        self.assertNotEqual(updated.fingerprint, prepared.fingerprint)

    def test_apply_refuses_when_the_private_notification_file_disappears_after_prepare(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        self.notification_file.unlink()
        with self.assertRaises((deploy.DeploymentRefused, ValueError)) as failure:
            _ = subject.apply(approve_resources=True)
        self.assertIn('notification_email_file', str(failure.exception))
        self.assertIn(str(self.notification_file), str(failure.exception))
        self.assertEqual(self.commands('deploy'), [])

    def test_prepare_is_local_and_first_boot_needs_no_observation(self) -> None:
        prepared = self.deployment().prepare()
        self.assertEqual(self.commands(), [])
        self.assertEqual(prepared.parameters['MonthlyLimitUsd'], '400')
        self.assertNotIn('EnableCompute', prepared.parameters)
        book = ledger.CostLedger(self.state).read_snapshot()
        self.assertEqual(book.observations, [])
        self.assertEqual(book.decision['status'], 'allowed')
        self.assertTrue(book.inventories)
        self.assertNotIn('credentials', (self.bundle / 'parameters.json').read_text())
        self.assertEqual(gzip.decompress(base64.b64decode(prepared.parameters['Bootstrap'])).decode(),
                         (self.bundle / 'bootstrap.sh').read_text())
        self.assertEqual(conf.read(self.settings).text('offload'), 'off')

    def test_apply_guards_foundation_before_compute_and_preserves_identity_on_rerun(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        before = ledger.CostLedger(self.state).read_snapshot().inventories
        paths = ('claims/job.json', 'affinity.json', 'mirror/warm', 'peers/aws.json')
        for name in paths:
            path = self.state / name
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_text('keep')
        outputs = subject.apply(approve_resources=True)
        self.assertEqual(outputs['InstanceId'], 'i-fixture')
        calls = self.commands('deploy')
        self.assertEqual(len(calls), 2)
        self.assertIn('EnableCompute=false', cast(list[str], calls[0]['args']))
        self.assertIn('EnableCompute=true', cast(list[str], calls[1]['args']))
        for row in calls:
            decision = cast(dict[str, object], row['decision'])
            self.assertEqual(decision['status'], 'allowed')
            self.assertEqual(decision['purpose'], 'deployment')
        _ = subject.prepare()
        self.assertEqual(subject.apply(approve_resources=True), outputs)
        calls = self.commands('deploy')
        self.assertEqual(len(calls), 3)
        self.assertIn('EnableCompute=true', cast(list[str], calls[-1]['args']))
        self.assertEqual(ledger.CostLedger(self.state).read_snapshot().inventories, before)
        for name in paths:
            self.assertEqual((self.state / name).read_text(), 'keep')

    def test_a_live_limit_change_after_preparation_refuses_every_resource(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        self.replace('daily_limit_usd', '1')
        with self.assertRaises(deploy.DeploymentRefused):
            _ = subject.apply(approve_resources=True)
        self.assertEqual(self.commands(), [])

    def test_a_limit_change_during_foundation_refuses_compute(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        execute = subject.execute

        def change_limit(arguments: Sequence[str], *, capture_output: bool, text: bool,
                         timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            result = execute(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check, env=env)
            if 'EnableCompute=false' in arguments:
                self.replace('monthly_limit_usd', '1')
            return result

        subject.execute = change_limit
        with self.assertRaises(deploy.DeploymentRefused):
            _ = subject.apply(approve_resources=True)
        calls = self.commands('deploy')
        self.assertEqual(len(calls), 1)
        self.assertIn('EnableCompute=false', cast(list[str], calls[0]['args']))

    def test_apply_requires_explicit_user_gate_and_private_credentials(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        with self.assertRaises(deploy.DeploymentRefused):
            _ = subject.apply()
        self.credentials.chmod(0o644)
        with self.assertRaises(deploy.DeploymentRefused):
            _ = subject.apply(approve_resources=True)
        self.assertEqual(self.commands(), [])

    def test_setup_restart_uses_the_guard_without_enabling_offload_or_duplicate_starts(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        _ = subject.apply(approve_resources=True)
        self.set_clock(self.now + 1)
        self.assertEqual(subject.setup_start(approve_resources=True), {'InstanceId': 'i-fixture', 'State': 'pending'})
        self.assertEqual(subject.setup_start(approve_resources=True), {'InstanceId': 'i-fixture', 'State': 'pending'})
        starts = self.commands('start-instances')
        self.assertEqual(len(starts), 1)
        self.assertEqual(cast(dict[str, object], starts[0]['decision'])['purpose'], 'deployment')
        self.assertEqual(conf.read(self.settings).text('offload'), 'off')
        self.assertFalse((self.state / 'admission.json').exists())
        self.assertEqual(ledger.CostLedger(self.state).read_snapshot().observations[-1].state, 'pending')

    def test_setup_restart_uses_prepared_parameters_without_the_private_address_file(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        _ = subject.apply(approve_resources=True)
        self.notification_file.unlink()
        self.assertEqual(subject.setup_start(approve_resources=True), {'InstanceId': 'i-fixture', 'State': 'pending'})
        self.assertEqual(len(self.commands('start-instances')), 1)

    def test_setup_restart_refuses_a_changed_limit_before_any_resource_request(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        _ = subject.apply(approve_resources=True)
        count = len(self.commands())
        self.replace('daily_limit_usd', '1')
        with self.assertRaises(deploy.DeploymentRefused):
            _ = subject.setup_start(approve_resources=True)
        self.assertTrue(all('describe-instances' in cast(list[str], row['args']) for row in self.commands()[count:]))
        self.assertEqual(self.commands('start-instances'), [])

    def test_prepare_preserves_existing_cost_and_claim_evidence(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        book = ledger.CostLedger(self.state)
        book.observe(ledger.InstanceObservation(self.now, 'stopped', 'fixture'))
        self.set_clock(self.now + 1)
        _ = subject.prepare()
        self.assertEqual(book.read_snapshot().observations, [ledger.InstanceObservation(self.now - 1, 'stopped', 'fixture')])

    def test_retained_volume_reuse_keeps_the_volume_tag_and_creates_no_new_identity(self) -> None:
        self.replace('existing_data_volume_id', 'vol-retained')
        subject = self.deployment()
        _ = subject.prepare()
        result = subject.apply(approve_resources=True)
        self.assertEqual(result['DataVolumeId'], 'vol-retained')
        calls = self.commands()
        self.assertTrue(any('describe-volumes' in cast(list[str], row['args']) for row in calls))
        self.assertEqual(subject.apply(approve_resources=True)['DataVolumeId'], 'vol-retained')

    def test_template_enforces_budget_tags_retention_network_and_single_instance_policy(self) -> None:
        template = remote.read_record(deploy.TEMPLATE)
        resources = cast(dict[str, dict[str, object]], template['Resources'])
        cost_tag = {'Key': {'Ref': 'CostTagKey'}, 'Value': {'Ref': 'CostTagValue'}}
        for name in ('DataVolume', 'MonthlyBudget'):
            resource = resources[name]
            self.assertEqual(resource['DeletionPolicy'], 'Retain')
            self.assertEqual(resource['UpdateReplacePolicy'], 'Retain')
        self.assertNotIn('DeletionPolicy', resources['Instance'])
        self.assertNotIn('UpdateReplacePolicy', resources['Instance'])
        self.assertNotIn('Tags', cast(dict[str, object], resources['Instance']['Properties']))
        for name in ('Instance', 'DataVolume', 'LaunchTemplate'):
            self.assertEqual(resources[name]['DependsOn'], ['BudgetEnforcement'])
        properties = cast(dict[str, object], resources['DataVolume']['Properties'])
        self.assertIn(cost_tag, cast(list[object], properties['Tags']))
        launch = cast(dict[str, object], resources['LaunchTemplate']['Properties'])
        data = cast(dict[str, object], launch['LaunchTemplateData'])
        self.assertEqual(data['InstanceMarketOptions'], {
            'MarketType': 'spot', 'SpotOptions': {'SpotInstanceType': 'persistent',
            'InstanceInterruptionBehavior': 'stop', 'MaxPrice': {'Ref': 'SpotMaxPrice'}}})
        self.assertEqual(data['InstanceInitiatedShutdownBehavior'], 'stop')
        mappings = cast(list[dict[str, object]], data['BlockDeviceMappings'])
        self.assertIs(cast(dict[str, object], mappings[0]['Ebs'])['DeleteOnTermination'], True)
        self.assertEqual(cast(dict[str, object], data['MetadataOptions'])['HttpTokens'], 'required')
        tag_specs = cast(list[dict[str, object]], data['TagSpecifications'])
        self.assertEqual({row['ResourceType'] for row in tag_specs}, {'instance', 'volume', 'spot-instances-request'})
        for spec in tag_specs:
            self.assertIn(cost_tag, cast(list[object], spec['Tags']))
        budget = cast(dict[str, object], resources['MonthlyBudget']['Properties'])
        budget_data = cast(dict[str, object], budget['Budget'])
        self.assertEqual(budget_data['BudgetLimit'], {'Amount': {'Ref': 'MonthlyLimitUsd'}, 'Unit': 'USD'})
        self.assertIn('AWS_BUILD_DATA_VOLUME_ID', str(data['UserData']))
        self.assertIn("'Ref': 'DataVolume'", str(data['UserData']))
        ingress = cast(dict[str, object], resources['SecurityGroup']['Properties'])['SecurityGroupIngress']
        self.assertEqual(ingress, [{'IpProtocol': 'udp', 'FromPort': 41641, 'ToPort': 41641, 'CidrIp': '0.0.0.0/0'}])
        policy = cast(dict[str, object], resources['ScalerPolicy']['Properties'])
        statements = cast(list[dict[str, object]], cast(dict[str, object], policy['PolicyDocument'])['Statement'])
        self.assertEqual(statements[0]['Action'], ['ec2:StartInstances', 'ec2:StopInstances'])
        self.assertIn('${Instance}', str(statements[0]['Resource']))
        self.assertIn('ec2:DescribeSpotInstanceRequests', cast(list[str], statements[1]['Action']))
        self.assertNotIn('AccessKey', json.dumps(template))
        self.assertNotIn('mkfs', json.dumps(template))

    def test_repository_configuration_enables_approved_aws_control_and_exchange(self) -> None:
        root = Path(__file__).resolve().parents[2]
        self.assertEqual(conf.read(root / 'config/aws_target.conf').text('offload'), 'on')
        self.assertEqual(conf.read(root / 'config/build_targets.conf').text('targets').split(','),
                         ['mac', 'natedev', 'aws'])

    def refuse_command(self, arguments: Sequence[str], *, capture_output: bool, text: bool,
                       timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        self.assertIsNone(env)
        _ = (capture_output, text, timeout, check)
        raise AssertionError(f'disabled tick called AWS: {arguments}')

    def test_scheduled_exchange_includes_aws_while_control_stays_off(self) -> None:
        registry = self.root / 'build_targets.conf'
        _ = registry.write_text('\n'.join((
            'targets=aws,natedev', 'kind.aws=ssh', 'platform.aws=linux', 'settings.aws=aws_target.conf',
            'kind.natedev=local', 'platform.natedev=linux', 'sync.hosts.aws=aws-build',
            'sync.connect_timeout_s=5', 'sync.reach_timeout_s=5', 'sync.rsync_timeout_s=5', '',
        )))
        commands: list[list[str]] = []
        logs = self.root / 'buildlog'

        def exchange(arguments: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
            self.assertIn(arguments[0], ('ssh', 'rsync'))
            commands.append(arguments)
            return subprocess.CompletedProcess(arguments, 0, 'rc=0\n' if arguments[0] == 'ssh' else '', '')

        with (mock.patch.dict(os.environ, {'BUILDLOG_PEER': ''}),
              mock.patch('scripts.buildlog.sync.store.root', return_value=logs),
              mock.patch('scripts.buildlog.sync.store.host_name', return_value='natedev'),
              mock.patch('scripts.buildlog.sync.health.write_snapshot'),
              mock.patch.object(sync, 'run', exchange)):
            self.assertEqual(sync.sync(), 0)
            status = sync.read_target_status('aws')
            self.assertIsInstance(status, sync.SyncStatus)
            if isinstance(status, sync.SyncStatus):
                self.assertTrue(status.ok)
        self.assertTrue(commands)
        self.assertEqual(conf.read(self.settings).text('offload'), 'off')
        self.assertEqual(self.commands(), [])

    def test_service_command_and_repeated_ticks_share_charge_recovery_across_utc_midnight(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        _ = subject.apply(approve_resources=True)
        self.replace('offload', 'on')
        registry = self.root / 'build_targets.conf'
        _ = registry.write_text('targets=aws,natedev\nkind.aws=ssh\nplatform.aws=linux\nsettings.aws=aws_target.conf\nkind.natedev=local\nplatform.natedev=linux\n')
        controller = aws_target.Controller(self.settings, self.state, self.root / 'queue', clock=lambda: self.now,
                                           notify=lambda _event: True, publish=lambda _envelope: True)
        self.assertEqual(controller.tick().state, 'stopped')
        self.assertEqual(controller.tick().state, 'stopped')
        self.set_clock(self.now + 120)
        save = cast(Callable[[ledger.CostLedger, ledger.LedgerEvidence], None], getattr(ledger.CostLedger, '_save'))

        def fail_reconciliation(book: ledger.CostLedger, state: ledger.LedgerEvidence) -> None:
            if state.reconciliations:
                raise OSError('the reconciliation write failed')
            save(book, state)

        with mock.patch.object(ledger.CostLedger, '_save', fail_reconciliation):
            self.assertEqual(controller.tick().state, 'stopped')
        book = ledger.CostLedger(self.state)
        self.assertEqual(book.read_snapshot().reconciliations, [])
        self.assertEqual(controller.tick().state, 'stopped')
        queries = self.commands('get-cost-and-usage')
        self.assertEqual(len(queries), 1)
        self.assertEqual(len(book.read_snapshot().reconciliations), 1)
        self.assertNotIn('charge_error', (self.state / 'controller.json').read_text())
        settings = read_control_settings(self.settings)
        api = aws_target.AwsCli(settings.credentials_profile, settings.region, settings.aws_timeout_s)
        self.assertIsInstance(controller.refresh_charges(api, self.now), ledger.ChargeRead)
        self.assertEqual(len(self.commands('get-cost-and-usage')), 1)

    def test_bootstrap_remote_idle_stop_requires_no_published_permission(self) -> None:
        configuration = deploy.bootstrap_configuration(self.settings, deploy.remote_configuration(self.settings))
        self.assertIn('services.amazon-ssm-agent.enable = true', configuration)
        self.assertIn('aws-build-first-boot-stop', configuration)
        self.assertNotIn('@IDLE_STOP_SECONDS@', configuration)
        self.assertNotIn('mkfs', configuration)
        remote_settings = self.root / 'remote.conf'
        _ = remote_settings.write_text(deploy.remote_configuration(self.settings))
        self.assertEqual(conf.read(self.settings).text('offload'), 'off')
        self.assertTrue(read_control_settings(remote_settings).enabled)
        stops: list[int] = []
        directory = self.root / 'remote'
        agent = remote.RemoteAgent(remote_settings, directory, clock=lambda: self.now, imds=lambda: False,
                                   process_groups=lambda: (), shutdown=lambda: stops.append(self.now), boot=lambda: 'new-boot')
        self.assertEqual(agent.tick(), 'running')
        self.assertIsInstance(remote.execution_permission(directory, 'aws', now=self.now, boot_id='new-boot'), ledger.PermissionUnavailable)
        self.set_clock(self.now + int(read_control_settings(remote_settings).idle_stop_minutes * 60))
        self.assertEqual(agent.tick(), 'stopping')
        self.assertEqual(stops, [self.now])
        self.assertIsInstance(remote.execution_permission(directory, 'aws', now=self.now, boot_id='new-boot'), ledger.PermissionUnavailable)

    def test_reviewed_bootstrap_template_renders_and_fits_the_deployment_parameter(self) -> None:
        patch = (deploy.TEMPLATE.parent / 'nixos.patch').read_text()
        for statement in (r'^\+install -d -m ([0-7]{3,4}) /var/lib/aws-build$',
                          r'^\+\s+\$\{pkgs.coreutils\}/bin/install -d -m ([0-7]{3,4}) /var/lib/aws-build$'):
            modes = list(re.finditer(statement, patch, re.MULTILINE))
            self.assertEqual(len(modes), 1, statement)
            self.assertEqual(int(modes[0].group(1), 8) & 0o005, 0o005)
        file_modes = list(re.finditer(r'^\+\s+\$\{pkgs.coreutils\}/bin/chmod ([0-7]{3,4}) /var/lib/aws-build/remote.conf$',
                                     patch, re.MULTILINE))
        self.assertEqual(len(file_modes), 1)
        self.assertEqual(int(file_modes[0].group(1), 8) & 0o004, 0o004)
        self.install_reviewed_template()
        for volume in ('', 'vol-0123456789abcdef0'):
            with self.subTest(volume=volume):
                self.replace('existing_data_volume_id', volume)
                rendered = deploy.bootstrap_configuration(self.settings, deploy.remote_configuration(self.settings))
                self.assertTrue(rendered.startswith('#!'))
                self.assertNotRegex(rendered, r'@[A-Z_]+@')
                self.assertIn('nvme-Amazon_Elastic_Block_Store_', rendered)
                self.assertIn('/etc/aws-build/remote.conf', rendered)
                self.assertNotIn('../modules/linux/tailscale.nix', rendered)
                self.assertIn('extraSetFlags = [ "--ssh" ];', rendered)
                self.assertNotIn('# The module enables Tailscale SSH.', rendered)
                bootstrap = self.bundle / 'bootstrap.sh'
                _ = bootstrap.write_text(rendered)
                parameters = self.deployment().prepare().parameters
                self.assertLessEqual(len(parameters['Bootstrap']), 4096)
                self.assertEqual(gzip.decompress(base64.b64decode(parameters['Bootstrap'])).decode(), rendered)

    def test_controller_service_uses_the_tick_command_and_reads_each_interval(self) -> None:
        self.replace('offload', 'on')
        calls: list[list[str]] = []
        pauses: list[float] = []

        def execute(arguments: Sequence[str], *, capture_output: bool, text: bool,
                    timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            self.assertFalse(capture_output)
            self.assertTrue(text)
            self.assertFalse(check)
            self.assertEqual(timeout, conf.read(self.settings).number('controller_tick_timeout_s'))
            self.assertIsNone(env)
            self.assertEqual(os.environ['AWS_SHARED_CREDENTIALS_FILE'], str(self.credentials))
            calls.append(list(arguments))
            self.replace('tick_interval_s', str(7 * len(calls)))
            return subprocess.CompletedProcess(arguments, 0, '', '')

        def pause(seconds: float) -> None:
            pauses.append(seconds)
            if len(pauses) == 2:
                self.replace('offload', 'off')

        aws_target.serve(self.settings, self.state, execute=execute, pause=pause)
        expected = [sys.executable, str(Path(aws_target.__file__)), 'tick',
                    '--settings', str(self.settings), '--directory', str(self.state)]
        self.assertEqual(calls, [expected, expected])
        self.assertEqual(pauses, [7, 14])
        aws_target.serve(self.settings, self.state, execute=self.refuse_command, pause=pause)


    def test_replacement_reuses_the_saved_volume_and_preserves_cost_evidence(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        outputs = subject.apply(approve_resources=True)
        before = ledger.CostLedger(self.state).read_snapshot()
        claim = self.state / 'retained-claim.json'
        _ = claim.write_text('keep')
        (self.root / 'fake-stack.json').unlink()
        self.replace('stack_name', 'replacement-build')
        self.replace('existing_data_volume_id', outputs['DataVolumeId'])
        _ = subject.prepare()
        replacement = subject.apply(approve_resources=True)
        self.assertEqual(replacement['DataVolumeId'], outputs['DataVolumeId'])
        self.assertEqual(claim.read_text(), 'keep')
        after = ledger.CostLedger(self.state).read_snapshot()
        self.assertEqual(after.inventories, before.inventories)
        self.assertEqual(after.prices, before.prices)
        self.assertTrue(all(row in after.observations for row in before.observations))
        self.assertEqual(len(self.commands('deploy')), 4)

    def test_missing_stack_refuses_new_resources_without_the_saved_volume(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        _ = subject.apply(approve_resources=True)
        saved = (self.state / 'deployment.json').read_bytes()
        (self.root / 'fake-stack.json').unlink()
        _ = subject.prepare()
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'saved DataVolumeId'):
            _ = subject.apply(approve_resources=True)
        self.assertEqual(len(self.commands('deploy')), 2)
        self.assertEqual((self.state / 'deployment.json').read_bytes(), saved)

    def test_volume_selection_change_requires_deleting_the_old_stack(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        outputs = subject.apply(approve_resources=True)
        self.replace('existing_data_volume_id', outputs['DataVolumeId'])
        _ = subject.prepare()
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'delete the old stack'):
            _ = subject.apply(approve_resources=True)
        self.replace('stack_name', 'replacement-build')
        _ = subject.prepare()
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'delete the old stack'):
            _ = subject.apply(approve_resources=True)
        self.assertEqual(len(self.commands('deploy')), 2)

    def test_setup_after_idle_stop_refreshes_stale_state_before_the_guard(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        _ = subject.apply(approve_resources=True)
        self.set_clock(self.now + 1)
        _ = subject.setup_start(approve_resources=True)
        stack_path = self.root / 'fake-stack.json'
        stack = remote.read_record(stack_path)
        stack['instance_state'] = 'stopped'
        _ = stack_path.write_text(json.dumps(stack))
        self.set_clock(self.now + 901)
        _ = subject.prepare()
        self.assertEqual(ledger.CostLedger(self.state).read_snapshot().observations[-1].state, 'stopped')
        self.assertEqual(subject.setup_start(approve_resources=True)['State'], 'pending')
        self.assertEqual(len(self.commands('start-instances')), 2)

    def test_compute_update_revalidates_the_selected_image(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        _ = subject.apply(approve_resources=True)
        self.replace('ami_id', 'ami-unverified')
        _ = subject.prepare()
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'image identity'):
            _ = subject.apply(approve_resources=True)
        self.assertEqual(len(self.commands('describe-images')), 2)
        self.assertEqual(len(self.commands('deploy')), 2)

    def test_retained_volume_update_accepts_only_its_current_attachment(self) -> None:
        self.replace('existing_data_volume_id', 'vol-retained')
        subject = self.deployment()
        _ = subject.prepare()
        _ = subject.apply(approve_resources=True)
        volume = {'AvailabilityZone': 'us-east-1a', 'Encrypted': True, 'State': 'in-use',
                  'Size': 500, 'VolumeType': 'gp3', 'Iops': 6000, 'Throughput': 500,
                  'Tags': [{'Key': 'Project', 'Value': 'fixture-build'}],
                  'Attachments': [{'InstanceId': 'i-fixture'}]}
        override_path = self.root / 'describe-volumes.json'
        _ = override_path.write_text(json.dumps({'Volumes': [volume]}))
        _ = subject.prepare()
        self.assertEqual(subject.apply(approve_resources=True)['DataVolumeId'], 'vol-retained')
        volume['Attachments'] = [{'InstanceId': 'i-other'}]
        _ = override_path.write_text(json.dumps({'Volumes': [volume]}))
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'retained volume'):
            _ = subject.apply(approve_resources=True)
        self.assertEqual(len(self.commands('deploy')), 3)

    def test_compute_deployment_records_runtime_before_controller_activation(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        _ = (self.root / 'describe-instances.json').write_text(json.dumps({
            'Reservations': [{'Instances': [{'InstanceId': 'i-fixture', 'LaunchTime': 'boot',
                                            'State': {'Name': 'running'}}]}]}))
        _ = subject.apply(approve_resources=True)
        book = ledger.CostLedger(self.state)
        self.assertEqual(book.read_snapshot().observations[-1].state, 'running')
        self.set_clock(self.now + 600)
        _ = subject.prepare()
        decision = ledger.mapping(remote.read_record(self.bundle / 'preflight.json')['decision'])
        self.assertGreater(float(ledger.text(decision, 'daily_usd')), 0.01)
        self.assertEqual(conf.read(self.settings).text('offload'), 'off')

    def test_resource_requests_write_no_unused_intent_record(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        _ = subject.apply(approve_resources=True)
        self.set_clock(self.now + 1)
        _ = subject.setup_start(approve_resources=True)
        self.assertFalse((self.state / 'deployment-intent.json').exists())

    def test_remote_settings_omit_an_absent_instance_identity(self) -> None:
        self.replace('instance_id', '')
        rendered = deploy.remote_configuration(self.settings)
        self.assertNotIn('instance_id=', rendered)
        self.assertNotIn('unpublished', rendered)
        self.assertEqual(rendered.count('offload='), 1)
        self.assertEqual(rendered.count('ci_repo='), 1)

    def test_bootstrap_settings_bind_imds_identity_without_admission(self) -> None:
        self.replace('instance_id', '')
        source = self.root / 'bootstrap.conf'
        _ = source.write_text(deploy.remote_configuration(self.settings))
        destination = self.root / 'lifecycle/remote.conf'
        requests: list[str] = []

        def reply(request: Request, *, timeout: float) -> BytesIO:
            self.assertEqual(timeout, conf.read(source).number('imds_timeout_s'))
            requests.append(request.full_url)
            if request.get_method() == 'PUT':
                return BytesIO(b'fixture-token')
            self.assertEqual(request.get_header('X-aws-ec2-metadata-token'), 'fixture-token')
            return BytesIO(b'{"instanceId":"i-abcdef","region":"us-east-2"}')

        with mock.patch.object(remote, 'urlopen', reply):
            remote.write_bootstrap_settings(source, destination)
            first = destination.read_bytes()
            remote.write_bootstrap_settings(source, destination)
        self.assertEqual(destination.read_bytes(), first)
        settings = read_control_settings(destination, remote_identity=True)
        self.assertEqual(settings.instance_id, 'i-abcdef')
        self.assertEqual(settings.region, 'us-east-2')
        self.assertTrue(settings.enabled)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o644)
        self.assertEqual(len(requests), 4)
        self.assertFalse((destination.parent / 'admission.json').exists())

    def test_remote_settings_preserve_the_source_limit_lines(self) -> None:
        source = self.settings.read_text().replace('# range: 0.001..\nidle_stop_minutes=',
                                                   '# range: 10..20\nidle_stop_minutes=')
        _ = self.settings.write_text(source)
        self.assertIn('# range: 10..20\nidle_stop_minutes=15', deploy.remote_configuration(self.settings))

    def fake_commands(self, source: str, *names: str) -> None:
        for name in names:
            path = self.root / 'bin' / name
            _ = path.write_text(source)
            path.chmod(0o700)

    def run_bash(self, path: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(['bash', str(path)], capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)

    def patch_source(self, path: str) -> str:
        if not self.patch_sections:
            patch = (deploy.TEMPLATE.parent / 'nixos.patch').read_text()
            for section in patch.split('diff --git ')[1:]:
                name = section.splitlines()[0].split(' b/', 1)[1]
                self.patch_sections[name] = '\n'.join(line[1:] for line in section.splitlines()
                                                     if line.startswith('+') and not line.startswith('+++')) + '\n'
        return self.patch_sections[path]

    def install_reviewed_template(self) -> None:
        hosts = self.root / 'nixos/hosts'
        template = hosts / 'reviewed-bootstrap.sh.in'
        _ = template.write_text(self.patch_source('hosts/aws-bootstrap.sh.in'))
        _ = (hosts / 'aws-build-base.nix').write_text(self.patch_source('hosts/aws-build-base.nix'))
        self.replace('bootstrap_template_path', str(template))

    def reviewed_bundle(self) -> str:
        self.install_reviewed_template()
        with mock.patch.object(sys, 'argv', ['deploy.py', 'bootstrap', '--settings', str(self.settings),
                                           '--output', str(self.bundle)]):
            self.assertEqual(deploy.main(), 0)
        return (self.bundle / 'bootstrap.sh').read_text()

    def test_bootstrap_retry_keeps_the_existing_watchdog_deadline(self) -> None:
        rendered = self.reviewed_bundle()
        watchdog = rendered.split('# Bound the first boot', 1)[1].split('\ncat > ', 1)[0]
        watchdog = watchdog[watchdog.index('\n') + 1:]
        script = self.root / 'watchdog.sh'
        _ = script.write_text('set -euo pipefail\n' + watchdog)
        fake = r'''import json, os, pathlib, sys
root = pathlib.Path(os.environ['DEPLOY_FIXTURE'])
state = root / 'watchdog-deadline.json'
if pathlib.Path(sys.argv[0]).name == 'systemctl':
    assert sys.argv[1:] == ['show', '--property=LoadState', '--value', 'aws-build-first-boot-stop.timer']
    print('loaded' if state.exists() else 'not-found')
else:
    with (root / 'watchdog-calls.jsonl').open('a') as output:
        output.write(json.dumps(sys.argv[1:]) + '\n')
    seconds = next(arg.split('=')[1].removesuffix('s') for arg in sys.argv[1:] if arg.startswith('--on-active='))
    state.write_text(json.dumps(int((root / 'clock').read_text()) + int(seconds)))
'''
        self.fake_commands(f'#!{sys.executable}\n' + fake, 'systemctl', 'systemd-run')
        first = self.run_bash(script)
        self.assertEqual(first.returncode, 0, first.stderr)
        deadline = (self.root / 'watchdog-deadline.json').read_bytes()
        self.assertEqual(json.loads(deadline), self.now + int(read_control_settings(self.settings).idle_stop_minutes * 60))
        self.set_clock(self.now + 60)
        second = self.run_bash(script)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual((self.root / 'watchdog-deadline.json').read_bytes(), deadline)
        self.assertEqual(len((self.root / 'watchdog-calls.jsonl').read_text().splitlines()), 1)

    def test_controller_service_restarts_after_a_failed_interval_read(self) -> None:
        module = self.patch_source('modules/linux/aws-build.nix')
        unit = module.split('systemd.user.services.aws-target = {', 1)[1]
        service = unit.split('serviceConfig = {', 1)[1].split('};', 1)[0]
        self.assertRegex(service, r'Restart\s*=\s*"on-failure"\s*;')
        delay = re.search(r'RestartSec\s*=\s*"([0-9]+(?:\.[0-9]+)?)s"\s*;', service)
        if delay is None:
            self.fail('the controller needs a restart delay')
        self.assertGreater(Decimal(delay.group(1)), 0)

    def install_volume_fakes(self) -> None:
        state = self.root / 'volume-state'
        state.mkdir()
        _ = (state / 'data-volume-id').write_text('vol-0123456789abcdef0\n')
        (self.root / 'device').touch()
        fake = r'''#!/bin/sh
set -eu
root=$DEPLOY_FIXTURE
case ${0##*/} in
  udevadm) test "$1" = wait ;;
  readlink)
    test "$1" = -f
    test "$2" = /dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol0123456789abcdef0
    printf '%s/device\n' "$root" ;;
  lsblk)
    case "$3" in
      SERIAL) printf '%s\n' "$(cat "$root/serial")" ;;
      TYPE) printf 'disk\n' ;;
      *) test "$3" = MOUNTPOINTS ;;
    esac ;;
  blkid)
    test "$1" = -s
    test "$3" = -o
    test "$4" = value
    test "$5" = "$root/device"
    if test -f "$root/TYPE" || test -f "$root/LABEL"; then
      if test -f "$root/$2"; then cat "$root/$2"; else printf '\n'; fi
    else
      printf '\n'
      exit 2
    fi ;;
  wipefs)
    if test -f "$root/TYPE"; then cat "$root/TYPE"; else printf '\n'; fi ;;
  mkfs.ext4)
    test "$#" -eq 3
    test "$1" = -L
    test "$2" = aws-home
    test "$3" = "$root/device"
    printf 'format\n' >> "$root/formats"
    printf 'ext4\n' > "$root/TYPE"
    printf 'aws-home\n' > "$root/LABEL" ;;
  *) exit 1 ;;
esac
'''
        self.fake_commands(fake, 'udevadm', 'readlink', 'lsblk', 'blkid', 'wipefs', 'mkfs.ext4')

    def volume_script(self, *, bootstrap: bool, existing: str = '') -> Path:
        base = self.patch_source('hosts/aws-build-base.nix')
        host = self.patch_source('hosts/aws-build.nix')
        self.assertRegex(host, r'imports\s*=\s*\[\s*\(import\s+\./aws-build-base\.nix\b')
        self.assertIn('existingVolume = if bootstrap then installation.existingVolume else "";', base)
        if bootstrap:
            self.replace('existing_data_volume_id', existing)
            source = self.reviewed_bundle()
            installation = source.split('    installation = {', 1)[1].split('    };', 1)[0]
            volume = re.search(r'existingVolume = ("[^"\n]*");', installation)
            delay = re.search(r'volumeWaitSeconds = ([0-9]+);', installation)
            if volume is None or delay is None:
                self.fail('the rendered installation needs volume settings')
            existing = cast(str, json.loads(volume.group(1)))
            wait = '--timeout=' + delay.group(1)
        else:
            source, existing, wait = base, '', ''
        volume_script = source.split('systemd.services.aws-build-volume = {', 1)[1]
        script = volume_script.split("script = ''\n", 1)[1].split("\n    '';", 1)[0]
        bindings = {
            '${pkgs.systemd}/bin/udevadm': 'udevadm',
            '${lib.optionalString bootstrap "--timeout=${toString installation.volumeWaitSeconds}"}': wait,
            '${if bootstrap then "false" else "true"}': 'false' if bootstrap else 'true',
            '${lib.escapeShellArg existingVolume}': shlex.quote(existing),
            '/var/lib/aws-build': str(self.root / 'volume-state'),
            'test -b "$device"': 'test -f "$device"',
        }
        self.assertEqual(script.count('test -b "$device"'), 1)
        for name, value in bindings.items():
            script = script.replace(name, value)
        self.assertNotIn('${', script)
        path = self.root / ('bootstrap-volume.sh' if bootstrap else 'host-volume.sh')
        _ = path.write_text(script)
        return path

    def test_both_volume_modes_refuse_wrong_serials_and_preserve_existing_filesystems(self) -> None:
        self.install_volume_fakes()
        for bootstrap in (True, False):
            script = self.volume_script(bootstrap=bootstrap, existing='vol-0123456789abcdef0')
            for serial, expected_status in (('vol-wrong', 1), ('vol0123456789abcdef0', 0)):
                with self.subTest(bootstrap=bootstrap, serial=serial):
                    _ = (self.root / 'serial').write_text(serial)
                    original = {'TYPE': b'ext4\n', 'LABEL': b'aws-home\n', 'evidence': b'keep'}
                    for key, value in original.items():
                        _ = (self.root / key).write_bytes(value)
                    result = self.run_bash(script)
                    self.assertEqual(result.returncode, expected_status, result.stderr)
                    self.assertEqual({key: (self.root / key).read_bytes() for key in original}, original)
                    self.assertFalse((self.root / 'formats').exists())

    def test_bootstrap_formats_matching_blank_media_once_and_full_host_refuses_it(self) -> None:
        self.install_volume_fakes()
        _ = (self.root / 'serial').write_text('vol0123456789abcdef0')
        full = self.volume_script(bootstrap=False)
        refused = self.run_bash(full)
        self.assertNotEqual(refused.returncode, 0)
        self.assertFalse((self.root / 'formats').exists())
        bootstrap = self.volume_script(bootstrap=True)
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                result = self.run_bash(bootstrap)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((self.root / 'formats').read_text(), 'format\n')
        kept = self.run_bash(full)
        self.assertEqual(kept.returncode, 0, kept.stderr)
        self.assertEqual((self.root / 'formats').read_text(), 'format\n')

    def test_earlier_day_request_costs_do_not_reduce_the_next_days_reconciled_guard_totals(self) -> None:
        self.text_setting('accounting_zone', 'America/New_York')
        self.costs({'fixed_usd_per_month': '0', 'cost_explorer_read_usd': '0',
                    'observation_max_age_seconds': '259200', 'limit_reserve_usd': '0', 'daily_limit_usd': '18'})
        start = int(datetime(2026, 2, 1, tzinfo=UTC).timestamp())
        end = start + 86400
        self.set_clock(end + 3600)
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(start, (ledger.PricedResource('volume', Decimal(0)),), 'fixture'))
        book.observe(ledger.InstanceObservation(start, 'stopped', 'confirmed stopped'))
        book.reconcile(ledger.CostReconciliation(self.now, start, end, Decimal(24), 'UTC bill'))
        before = book.guard(self.settings, 'deployment', self.now, lambda _event: True)
        before_costs = narrow(self, before.evidence.costs, ledger.CostEstimate)
        self.assertEqual(before_costs.daily.usd, Decimal(19))
        self.assertEqual(before_costs.monthly.usd, Decimal(19))
        self.assertIsInstance(before, ledger.SpendingRefused)
        request = ledger.ComputeRequest(start, 'replacement', 'us-east-1', Decimal(2))
        book.record_compute_request(request)
        book.resolve_compute_request(request, ledger.ComputeResourcesAbsent(start + 5 * 3600))
        after = book.guard(self.settings, 'deployment', self.now, lambda _event: True)
        after_costs = narrow(self, after.evidence.costs, ledger.CostEstimate)
        self.assertGreaterEqual(after_costs.daily.usd, before_costs.daily.usd)
        self.assertGreaterEqual(after_costs.monthly.usd, before_costs.monthly.usd)
        self.assertIsInstance(after, ledger.SpendingRefused)
        self.assertEqual(sum(row.confirmed_uptime_s for row in after_costs.intervals), 0)

    def test_lower_price_keeps_each_established_day_and_the_guard_refusal(self) -> None:
        self.text_setting('accounting_zone', 'America/New_York')
        self.costs({'fixed_usd_per_month': '0', 'cost_explorer_read_usd': '0',
                    'observation_max_age_seconds': '259200', 'price_max_age_seconds': '259200',
                    'fallback_usd_per_hour': '1', 'limit_reserve_usd': '0', 'daily_limit_usd': '20'})
        start = int(datetime(2026, 2, 1, tzinfo=UTC).timestamp())
        end = start + 86400
        self.set_clock(end)
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(start, (ledger.PricedResource('volume', Decimal(0)),), 'fixture'))
        book.observe(ledger.InstanceObservation(start, 'running', 'confirmed running'))
        book.observe(ledger.InstanceObservation(end, 'stopped', 'confirmed stopped'))
        book.record_price(ledger.PriceSample(start, Decimal('0.1'), 'earlier day price'))
        book.record_price(ledger.PriceSample(start + 5 * 3600, Decimal(1), 'later day price'))
        book.reconcile(ledger.CostReconciliation(self.now, start, end, Decimal(24), 'Cost Explorer'))
        before = book.guard(self.settings, 'deployment', self.now, lambda _event: True)
        self.assertIsInstance(before, ledger.SpendingRefused)
        before_costs = narrow(self, before.evidence.costs, ledger.CostEstimate)
        book.record_price(ledger.PriceSample(start + 6 * 3600, Decimal('0.1'), 'lower later price'))
        reopened = ledger.CostLedger(self.state)
        after = reopened.guard(self.settings, 'deployment', self.now, lambda _event: True)
        after_costs = narrow(self, after.evidence.costs, ledger.CostEstimate)
        self.assertGreaterEqual(after_costs.daily.usd, before_costs.daily.usd)
        self.assertGreaterEqual(after_costs.monthly.usd, before_costs.monthly.usd)
        self.assertIsInstance(after, ledger.SpendingRefused)
        saved = reopened.path.read_bytes()
        snapshot = reopened.read_snapshot()
        readonly = ledger.costs_from_evidence(snapshot, aws_config.read_settings(self.settings), self.now)
        self.assertEqual(reopened.path.read_bytes(), saved)
        self.assertEqual(snapshot, reopened.read_snapshot())
        self.assertEqual(readonly.daily, after_costs.daily)

        self.replace('offload', 'on')
        charges = self.state / 'charges'
        charges.mkdir()
        reading = ledger.ChargeRead('2026-02-01', self.now + 1, start, end, Decimal(24), Decimal(0), Decimal(0))
        _ = (charges / '2026-02-01.json').write_text(json.dumps(ledger.charge_record(reading)))
        recovered = reopened.refresh_charges(self.settings, self.now + 1,
                                             lambda _start, _end: self.fail('recover the saved reply first'))
        self.assertEqual(recovered, reading)
        resolved = reopened.guard(self.settings, 'deployment', self.now + 1, lambda _event: True)
        resolved_costs = narrow(self, resolved.evidence.costs, ledger.CostEstimate)
        self.assertGreaterEqual(resolved_costs.daily.usd, before_costs.daily.usd)
        self.assertGreaterEqual(resolved_costs.monthly.usd, before_costs.monthly.usd)
        self.assertIsInstance(resolved, ledger.SpendingRefused)

    def test_matching_bill_adds_no_reconciliation_charge(self) -> None:
        self.costs({'fixed_usd_per_month': '0', 'cost_explorer_read_usd': '0',
                    'observation_max_age_seconds': '259200', 'price_max_age_seconds': '259200'})
        start = self.now - 86400
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(start, (ledger.PricedResource('volume', Decimal(0)),), 'fixture'))
        book.observe(ledger.InstanceObservation(start, 'running', 'confirmed running'))
        book.record_price(ledger.PriceSample(start, Decimal(1), 'confirmed price'))
        book.reconcile(ledger.CostReconciliation(self.now, start, self.now, Decimal(24), 'UTC bill'))
        costs = book.estimate(aws_config.read_settings(self.settings), self.now)
        self.assertEqual(ledger.period_cost(costs.intervals, start, self.now), Decimal(24))
        self.assertEqual(sum(r.usd for r in costs.intervals if r.kind == 'reconciliation'), Decimal(0))

    def test_readonly_measurements_leave_reconciled_day_totals_unestablished(self) -> None:
        self.costs({'fixed_usd_per_month': '0', 'cost_explorer_read_usd': '0'})
        start = self.now - 3600
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(start, (ledger.PricedResource('volume', Decimal(0)),), 'fixture'))
        book.observe(ledger.InstanceObservation(start, 'stopped', 'confirmed stopped'))
        book.observe(ledger.InstanceObservation(self.now, 'stopped', 'fresh confirmed stopped'))
        book.reconcile(ledger.CostReconciliation(self.now, start, self.now, Decimal(2), 'UTC bill'))
        saved = book.path.read_bytes()
        snapshot = book.read_snapshot()
        costs = ledger.costs_from_evidence(snapshot, aws_config.read_settings(self.settings), self.now)
        self.assertEqual(costs.daily.usd, Decimal(2))
        self.assertEqual(snapshot.reconciliations[0].established_days, ())
        self.assertEqual(book.path.read_bytes(), saved)
        _ = book.estimate(aws_config.read_settings(self.settings), self.now)
        reopened = ledger.CostLedger(self.state).read_snapshot()
        self.assertEqual(reopened.reconciliations[0].established_days,
                         (ledger.EstablishedDayCost(DAY, Decimal(2)),))

    def test_estimate_preserves_distinct_replies_for_the_same_period(self) -> None:
        self.costs({'fixed_usd_per_month': '0', 'cost_explorer_read_usd': '0'})
        start = self.now - 60
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(start, (ledger.PricedResource('volume', Decimal(0)),), 'fixture'))
        book.observe(ledger.InstanceObservation(start, 'stopped', 'confirmed stopped'))
        readings = (ledger.CostReconciliation(self.now, start, self.now, Decimal(2), 'UTC bill'),
                    ledger.CostReconciliation(self.now, start, self.now, Decimal(1), 'UTC bill'))
        for reading in readings:
            book.reconcile(reading)
        costs = book.estimate(aws_config.read_settings(self.settings), self.now)
        self.assertEqual(costs.daily.usd, Decimal(2))
        self.assertEqual(tuple(ledger.CostLedger(self.state).read_snapshot().reconciliations), readings)

    def test_money_and_measurement_use_the_configuration_finite_number_rule(self) -> None:
        for value in ('1e400', 'nan', 'inf'):
            for parser in (aws_config.money, measurement.number):
                with self.subTest(value=value, parser=parser.__name__):
                    with self.assertRaises(ValueError):
                        _ = parser(value)

    def test_spending_caps_refuse_values_above_the_configured_maximum(self) -> None:
        for key, value, normal in (('monthly_limit_usd', '400.01', '400'), ('daily_limit_usd', '20.01', '20'),
                                   ('monthly_limit_usd', '400.0000000000000001', '400'),
                                   ('daily_limit_usd', '20.0000000000000001', '20')):
            with self.subTest(key=key, value=value):
                self.replace(key, value)
                with self.assertRaises(ValueError):
                    _ = aws_config.read_settings(self.settings)
                self.replace(key, normal)

    def test_fixed_deployment_values_use_the_source_limits(self) -> None:
        for key, value, normal in (('data_volume_gib', '501', '500'), ('data_volume_iops', '6001', '6000'),
                                   ('data_volume_throughput', '501', '500'), ('budget_stop_percent', '99', '100'),
                                   ('instance_type', 'm7a.8xlarge', 'm7a.4xlarge')):
            with self.subTest(key=key):
                self.replace(key, value)
                with self.assertRaises(ValueError):
                    _ = aws_config.read_deployment_settings(self.settings)
                self.replace(key, normal)

    def test_durable_decision_check_rejects_a_superseded_decision(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        book = ledger.CostLedger(self.state)
        allowed = ledger.spending_guard(self.settings, self.state, 'deployment', clock=lambda: self.now)
        self.assertTrue(book.decision_is_current(allowed))
        self.replace('daily_limit_usd', '1')
        _ = ledger.spending_guard(self.settings, self.state, 'deployment', clock=lambda: self.now, notify=lambda _event: True)
        self.assertFalse(book.decision_is_current(allowed))
        self.assertIsInstance(book.permission_for_decision(allowed), ledger.PermissionUnavailable)

    def test_service_retains_its_interval_after_a_bad_live_read(self) -> None:
        self.replace('offload', 'on')
        calls: list[int] = []
        delays: list[float] = []
        number = conf.ConfigRead.number
        def action() -> None:
            calls.append(len(calls))
            if len(calls) == 3:
                self.replace('offload', 'off')
        def interval(settings: conf.ConfigRead, key: str) -> float:
            if len(calls) == 2:
                raise ValueError('bad live interval')
            return number(settings, key)
        with mock.patch.object(conf.ConfigRead, 'number', interval):
            deploy_service_loop = aws_config.service_loop
            deploy_service_loop(self.settings, 'tick_interval_s', action, pause=delays.append)
        self.assertEqual(len(calls), 3)
        self.assertEqual(delays, [30, 30])

    def test_service_refuses_work_when_the_first_interval_read_fails(self) -> None:
        self.replace('offload', 'on')
        calls: list[str] = []
        delays: list[float] = []
        with mock.patch.object(conf.ConfigRead, 'number', side_effect=ValueError('bad initial interval')):
            with self.assertRaisesRegex(ValueError, 'bad initial interval'):
                aws_config.service_loop(self.settings, 'tick_interval_s', lambda: calls.append('tick'), pause=delays.append)
        self.assertEqual(calls, [])
        self.assertEqual(delays, [])

    def test_remote_serve_uses_the_configured_state_directory(self) -> None:
        self.replace('offload', 'on')
        directory = Path.home() / read_control_settings(self.settings).remote_state_directory
        agent = remote.RemoteAgent(self.settings, directory)
        with (mock.patch.object(sys, 'argv', ['remote.py', 'serve', '--settings', str(self.settings)]),
              mock.patch.object(remote, 'RemoteAgent', return_value=agent) as factory,
              mock.patch.object(remote, 'service_loop') as loop):
            self.assertEqual(remote.main(), 0)
            factory.assert_called_once_with(self.settings, directory)
            loop.assert_called_once_with(self.settings, 'interruption_poll_s', agent.tick)

    def test_prepare_reads_the_template_once_and_uses_its_guard_decision(self) -> None:
        original = Path.read_bytes
        reads: list[Path] = []
        def read(path: Path) -> bytes:
            if path == deploy.TEMPLATE:
                reads.append(path)
            return original(path)
        with mock.patch.object(Path, 'read_bytes', read):
            _ = self.deployment().prepare()
        self.assertEqual(reads, [deploy.TEMPLATE])
        self.assertEqual(ledger.mapping(remote.read_record(self.bundle / 'preflight.json')['decision'])['purpose'], 'deployment')

    def test_deployment_settings_validate_the_public_source_inputs(self) -> None:
        for key, value in (('scripts_source_url', 'https://user:secret@example.invalid/a'),
                           ('scripts_source_commit', 'main'), ('scripts_source_sha256', 'unverified')):
            with self.subTest(key=key):
                settings = aws_config.read_deployment_settings(self.settings)
                self.replace(key, value)
                with self.assertRaises(ValueError):
                    _ = aws_config.read_deployment_settings(self.settings)
                self.replace(key, str(cast(object, getattr(settings, key))))

    def test_shared_aws_cli_rejects_a_malformed_stack_reply(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        _ = (self.root / 'describe-stacks.json').write_text('[]')
        with self.assertRaisesRegex(aws_target.AwsUnavailable, 'invalid reply'):
            _ = subject.apply(approve_resources=True)
        self.assertEqual(self.commands('deploy'), [])


    def test_replacement_preparation_accepts_a_confirmed_deleted_instance(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        outputs = subject.apply(approve_resources=True)
        (self.root / 'fake-stack.json').unlink()
        _ = (self.root / 'missing-instance').write_text('deleted')
        self.replace('stack_name', 'replacement-build')
        self.replace('existing_data_volume_id', outputs['DataVolumeId'])
        self.set_clock(self.now + 600)
        _ = subject.prepare()
        self.assertEqual(ledger.CostLedger(self.state).read_snapshot().observations[-1].state, 'terminated')
        self.assertEqual(len(self.commands('deploy')), 2)
