"""Exercise guarded deployment with a private AWS executable and no network."""
from __future__ import annotations

from collections.abc import Callable, Sequence
import base64
from datetime import UTC, datetime
from decimal import Decimal
import gzip
from io import BytesIO
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import cast, override
from unittest import mock
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mac_test'))

from scripts.aws_target import aws_target, config as aws_config, deploy, ledger, remote
from scripts.aws_target.config import read_control_settings
from scripts.aws_target.ledger_test_support import CostFixture
from scripts.buildlog import sync
from scripts.lib import conf


FAKE_AWS = r'''import datetime, json, os, pathlib, sys
root = pathlib.Path(os.environ['DEPLOY_FIXTURE'])
args = sys.argv[1:]
service = args[4]
operation = args[5]
state_path = root / 'fake-stack.json'
state = json.loads(state_path.read_text()) if state_path.exists() else {}
clock = int((root / 'clock').read_text())
book = root / 'state/cost-ledger.json'
evidence = json.loads(book.read_text()) if book.exists() else {}
with (root / 'commands.jsonl').open('a') as output:
    output.write(json.dumps({'args': args, 'decision': evidence.get('decision', {}),
                             'compute_requests': evidence.get('compute_requests', [])}) + '\n')
def reply(value):
    print(json.dumps(value))
override = root / (operation + '.json')
if override.exists():
    reply(json.loads(override.read_text()))
    sys.exit(0)
if service == 'cloudformation' and operation == 'describe-stacks':
    if not state or state.get('stack_name') != args[args.index('--stack-name') + 1]:
        print('ValidationError: Stack does not exist', file=sys.stderr)
        sys.exit(1)
    reply({'Stacks': [{'StackStatus': 'UPDATE_COMPLETE', 'Outputs': [
        {'OutputKey': key, 'OutputValue': value} for key, value in state['outputs'].items()]}]})
elif service == 'cloudformation' and operation == 'deploy':
    start = args.index('--parameter-overrides') + 1
    params = dict(word.split('=', 1) for word in args[start:] if '=' in word)
    outputs = {'ComputeEnabled': params['EnableCompute'], 'BudgetName': 'fixture-monthly',
               'ExistingDataInput': params['ExistingDataVolumeId']}
    if params['EnableCompute'] == 'true':
        outputs.update(InstanceId='i-fixture', DataVolumeId=params['ExistingDataVolumeId'] or 'vol-created')
    state = {'outputs': outputs, 'stack_name': args[args.index('--stack-name') + 1]}
    state_path.write_text(json.dumps(state))
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

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.settings.write_text((deploy.TEMPLATE.parents[3] / 'config/aws_target.conf').read_text())
        self.now = int(datetime(2026, 1, 15, 23, 59, tzinfo=UTC).timestamp())
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
        values = {
            'offload': 'off', 'deployment_profile': 'build-admin', 'stack_name': 'fixture-build',
            'ami_id': 'ami-fixture', 'ami_owner_id': '123456789012', 'nixos_release': '26.05',
            'availability_zone': 'us-east-1a', 'vpc_id': 'vpc-fixture', 'subnet_id': 'subnet-fixture',
            'notification_email': 'build@example.invalid', 'existing_data_volume_id': '',
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
            'deployment_timeout_s': '30', 'preflight_max_age_s': '300', 'deployment_price_at': str(self.now),
            'deployment_price_usd_per_hour': '0.1', 'fixed_usd_per_month': '0.001',
            'retained_storage_usd_per_month': '0', 'root_storage_usd_per_month': '0',
            'ipv4_usd_per_month': '0', 'gp3_iops_usd_per_month': '0', 'gp3_throughput_usd_per_month': '0',
            'region': 'us-east-1', 'instance_id': 'i-fixture',
            'cost_explorer_tag_key': 'Project', 'cost_explorer_tag_value': 'fixture-build', 'accounting_zone': 'UTC',
        })
        for key, value in values.items():
            self.replace(key, value)
        self.credentials = self.root / 'credentials'
        _ = self.credentials.write_text('')
        self.credentials.chmod(0o600)
        bin_path = self.root / 'bin'
        bin_path.mkdir()
        fake = bin_path / 'aws'
        _ = fake.write_text(f'#!{sys.executable}\n' + FAKE_AWS)
        fake.chmod(0o700)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            'PATH': f'{bin_path}{os.pathsep}{os.environ["PATH"]}', 'DEPLOY_FIXTURE': str(self.root),
            'AWS_SHARED_CREDENTIALS_FILE': str(self.credentials),
        })))
        self.set_clock(self.now)

    def set_clock(self, at: int) -> None:
        self.now = at
        _ = (self.root / 'clock').write_text(str(at))

    def deployment(self) -> deploy.Deployment:
        return deploy.Deployment(self.settings, self.state, self.bundle, clock=lambda: self.now,
                                 notify=lambda _event: True)

    def commands(self, operation: str = '') -> list[dict[str, object]]:
        path = self.root / 'commands.jsonl'
        rows = [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []
        return [row for row in rows if not operation or operation in cast(list[str], row['args'])]

    def prepare_stopped_instance(self) -> int:
        self.set_clock(int(datetime(2026, 1, 15, 12, tzinfo=UTC).timestamp()))
        for key, value in (('deployment_price_at', str(self.now)), ('fallback_usd_per_hour', '1'),
                           ('price_max_age_seconds', '86400'), ('observation_max_age_seconds', '86400'),
                           ('cost_explorer_read_usd', '0')):
            self.replace(key, value)
        ledger.CostLedger(self.state).observe(ledger.InstanceObservation(self.now, 'stopped', 'prior stopped evidence'))
        _ = self.deployment().prepare()
        return self.now

    def failing_compute(self, failure: str) -> deploy.Deployment:
        compute_returned = False

        def execute(arguments: Sequence[str], *, capture_output: bool, text: bool,
                    timeout: float, check: bool) -> subprocess.CompletedProcess[str]:
            nonlocal compute_returned
            operation = arguments[6]
            if compute_returned and ((failure in ('stack', 'timeout') and operation == 'describe-stacks')
                                     or (failure == 'instance' and operation == 'describe-instances')):
                return subprocess.CompletedProcess(arguments, 1, '', 'the requested observation is unavailable')
            result = aws_target.execute_command(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check)
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
        self.replace('deployment_price_at', str(self.now))
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
        self.replace('deployment_price_at', str(self.now))
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
        self.replace('deployment_price_at', str(self.now))
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
        self.replace('deployment_price_at', str(self.now))
        _ = self.deployment().prepare()
        self.assertEqual(ledger.CostLedger(self.state).pending_compute_requests(), ())
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
                         timeout: float, check: bool) -> subprocess.CompletedProcess[str]:
            result = execute(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check)
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

    def test_repository_defaults_leave_control_and_exchange_inactive(self) -> None:
        root = Path(__file__).resolve().parents[2]
        self.assertEqual(conf.read(root / 'config/aws_target.conf').text('offload'), 'off')
        self.assertNotIn('aws', conf.read(root / 'config/build_targets.conf').text('targets').split(','))

    def refuse_command(self, arguments: Sequence[str], *, capture_output: bool, text: bool,
                       timeout: float, check: bool) -> subprocess.CompletedProcess[str]:
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
        section = patch.split('diff --git a/hosts/aws-bootstrap.sh.in b/hosts/aws-bootstrap.sh.in\n', 1)[1]
        section = section.split('\ndiff --git ', 1)[0]
        base = patch.split('diff --git a/hosts/aws-build-base.nix b/hosts/aws-build-base.nix\n', 1)[1]
        base = base.split('\ndiff --git ', 1)[0]
        hosts = self.root / 'nixos/hosts'
        _ = (hosts / 'aws-build-base.nix').write_text('\n'.join(
            line[1:] for line in base.splitlines() if line.startswith('+') and not line.startswith('+++')) + '\n')
        source = '\n'.join(line[1:] for line in section.splitlines()
                           if line.startswith('+') and not line.startswith('+++')) + '\n'
        template = hosts / 'reviewed-bootstrap.sh.in'
        _ = template.write_text(source)
        self.replace('bootstrap_template_path', str(template))
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
                    timeout: float, check: bool) -> subprocess.CompletedProcess[str]:
            self.assertFalse(capture_output)
            self.assertTrue(text)
            self.assertFalse(check)
            self.assertEqual(timeout, conf.read(self.settings).number('controller_tick_timeout_s'))
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
        settings = read_control_settings(destination)
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

    def test_spending_caps_refuse_values_above_the_configured_maximum(self) -> None:
        for key, value, normal in (('monthly_limit_usd', '400.01', '400'), ('daily_limit_usd', '20.01', '20')):
            with self.subTest(key=key):
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
