"""Measure trial lifecycles with a fake AWS command and clock."""
from __future__ import annotations

from collections.abc import Sequence
from contextlib import nullcontext, redirect_stdout
from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO, StringIO
import json
import os
import shlex
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import cast, final, override
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

from scripts.aws_target import admin_secrets, deploy, gate_trial as trial, ledger
from scripts.aws_target.aws_target import AwsCli, AwsUnavailable
from scripts.aws_target.ledger_test_support import CostFixture
from scripts.production import merge_gate


def trial_config() -> str:
    content = (Path(__file__).resolve().parents[2] / 'config/aws_target.conf').read_text()
    return content[content.index('# Gate trial limits.'):]


@final
class Clock:
    def __init__(self) -> None:
        self.now: float = 1000

    def read(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@final
class FakeAws:
    def __init__(self, clock: Clock) -> None:
        self.clock: Clock = clock
        self.calls: list[tuple[str, str, list[str]]] = []
        self.states: dict[str, str] = {}
        self.refusals: int = 0
        self.capacity_zones: set[str] = set()
        self.lost_reply: bool = False
        self.lost_replies: int = 0
        self.tokens: dict[str, str] = {}
        self.hide_instances: bool = False
        self.lost_bucket_reply: bool = False
        self.lost_group_reply: bool = False
        self.bucket_exists: bool = False
        self.group_exists: bool = False
        self.refuse_bucket: bool = False
        self.cleanup_signal: int = 0
        self.terminate_timeout: bool = False
        self.delete_dependencies: int = 0
        self.signal_number: int = 0
        self.missing_log: bool = False
        self.query_timeout: bool = False
        self.zone_prices: dict[str, float] = {}
        self.tags: dict[str, str] = {}
        self.failure: str = ''
        self.interrupt: bool = False
        self.launches: int = 0
        self.compute: float = 0.4
        self.gate_status: str = 'pass'
        self.payloads: list[str] = []
        self.requests: list[dict[str, object]] = []
        self.wait_missing: bool = False
        self.bundles: list[bytes] = []
        self.guest_payload: dict[str, object] | None = None

    def __call__(self, arguments: Sequence[str], *, capture_output: bool, text: bool,
                 timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        assert arguments[0] == 'aws'
        args = list(arguments)
        index = args.index('--region') + 2
        service, operation = args[index:index + 2]
        self.calls.append((service, operation, args))
        def option(name: str) -> str:
            return args[args.index(name) + 1]
        def reply(value: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 0, json.dumps(value), '')
        def error(value: str) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 1, '', value)
        if operation == 'get-products':
            filters = cast(list[dict[str, str]], json.loads(option('--filters')))
            fields = {row['Field']: row['Value'] for row in filters}
            unit, amount = ('Requests', 0.000005) if fields.get('group', '').startswith('S3-API') else (
                ('GB-Mo', 0.08) if 'volumeApiName' in fields or 'volumeType' in fields else (
                ('Hrs', 0.005) if service == 'pricing' and option('--service-code') == 'AmazonVPC' else ('Hrs', self.compute)))
            return reply({'PriceList': [json.dumps({'terms': {'OnDemand': {'term': {'priceDimensions': {
                'price': {'unit': unit, 'beginRange': '0', 'pricePerUnit': {'USD': str(amount)}}}}}}})]})
        if operation == 'describe-subnets':
            assert option('--filters') == 'Name=vpc-id,Values=vpc-fixture'
            return reply({'Subnets': [
                {'SubnetId': 'subnet-second', 'AvailabilityZone': 'us-east-1b', 'MapPublicIpOnLaunch': True},
                {'SubnetId': 'subnet-private', 'AvailabilityZone': 'us-east-1c', 'MapPublicIpOnLaunch': False},
                {'SubnetId': 'subnet-fixture', 'AvailabilityZone': 'us-east-1a', 'MapPublicIpOnLaunch': False}]})
        if operation == 'describe-spot-price-history':
            return reply({'SpotPriceHistory': [{'SpotPrice': str(self.zone_prices.get(option('--availability-zone'), self.compute))}]})
        if operation == 'create-security-group':
            self.group_exists = True
            if self.lost_group_reply:
                raise subprocess.TimeoutExpired(args, timeout)
            return reply({'GroupId': 'sg-fixture'})
        if operation == 'describe-security-groups':
            filters = args[args.index('--filters') + 1:args.index('--output')]
            assert any(value.startswith('Name=group-name,Values=gate-trial-') for value in filters)
            assert 'Name=vpc-id,Values=vpc-fixture' in filters
            return reply({'SecurityGroups': [{'GroupId': 'sg-fixture'}] if self.group_exists else []})
        if operation == 'create-bucket':
            if self.refuse_bucket:
                return error('AccessDenied: fixture bucket')
            self.bucket_exists = True
            if self.lost_bucket_reply:
                raise subprocess.TimeoutExpired(args, timeout)
        if operation == 'put-object':
            self.bundles.append(Path(option('--body')).read_bytes())
        if operation == 'run-instances':
            request = ledger.mapping(cast(object, json.loads(option('--cli-input-json'))))
            interface = ledger.rows(request, 'NetworkInterfaces')[0]
            zone = 'us-east-1a' if interface['SubnetId'] == 'subnet-fixture' else 'us-east-1b'
            blocked = signal.pthread_sigmask(signal.SIG_BLOCK, set())
            assert set(trial.TRIAL_SIGNALS) <= blocked
            if zone in self.capacity_zones:
                return error('InsufficientInstanceCapacity: ' + zone)
            if self.refusals:
                self.refusals -= 1
                return error('An error occurred (InstanceLimitExceeded): fixture refusal')
            token = ledger.text(request, 'ClientToken')
            if token not in self.tokens:
                assert not any(state != 'terminated' for state in self.states.values()), 'overlapping attempts'
                self.launches += 1
                instance = 'i-fixture-' + str(self.launches)
                self.tokens[token] = instance
                self.states[instance] = 'running'
                self.tags[instance] = ledger.text(ledger.rows(ledger.rows(request, 'TagSpecifications')[0], 'Tags')[0], 'Value')
            instance = self.tokens[token]
            self.clock.advance(2)
            self.payloads.append(Path(option('--user-data').removeprefix('file://')).read_text())
            self.requests.append(ledger.mapping(cast(object, json.loads(option('--cli-input-json')))))
            if self.lost_reply or self.lost_replies:
                self.lost_reply = False
                self.lost_replies = max(0, self.lost_replies - 1)
                raise subprocess.TimeoutExpired(args, timeout)
            return reply({'Instances': [{'InstanceId': instance}]})
        if operation == 'describe-instances':
            if '--filters' in args:
                if self.query_timeout:
                    raise subprocess.TimeoutExpired(args, timeout)
                filters = args[args.index('--filters') + 1:args.index('--output')]
                old = 'Name=tag-key,Values=GateTrial' in filters
                wanted = filters[0].partition('Values=')[2]
                states = next(value.partition('Values=')[2].split(',') for value in filters
                              if value.startswith('Name=instance-state-name,'))
                instances = [instance for instance, state in self.states.items()
                             if state in states and (instance in self.tags if old else self.tags.get(instance) == wanted)
                             and not self.hide_instances]
                return reply({'Reservations': [{'Instances': [{'InstanceId': instance} for instance in instances]}]})
            self.clock.advance(3)
            instance = option('--instance-ids')
            return reply({'Reservations': [{'Instances': [{'InstanceId': instance, 'LaunchTime': 'fixture-boot',
                          'State': {'Name': self.states[instance]}}]}]})
        if operation == 'get-object':
            if self.signal_number:
                number, self.signal_number = self.signal_number, 0
                os.kill(os.getpid(), number)
            if option('--key').endswith('/log') and self.missing_log:
                return error('NoSuchKey')
            if self.interrupt:
                self.interrupt = False
                raise KeyboardInterrupt
            if self.failure:
                return error(self.failure)
            if self.wait_missing:
                return error('NoSuchKey')
            destination = Path(args[-3])
            if option('--key').endswith('/log'):
                _ = destination.write_text('fixture gate log')
            else:
                at = self.clock.read()
                payload = self.guest_payload or {'tools_ready_at': at + 10, 'gate_started_at': at + 13,
                    'gate_finished_at': at + 23, 'tests': 19, 'skips': 2, 'verdict': self.gate_status,
                    'failed_modules': [] if self.gate_status == 'pass' else ['fixture/test_failure.py']}
                if '/ready-' in option('--key') and self.guest_payload is None:
                    payload = {'tools_ready_at': at + 10, 'verdict': self.gate_status}
                _ = destination.write_text(json.dumps(payload))
                self.clock.advance(25)
            return reply({})
        if operation == 'delete-security-group' and self.delete_dependencies:
            self.delete_dependencies -= 1
            return error('DependencyViolation: fixture interface')
        if operation == 'terminate-instances' and self.terminate_timeout:
            raise subprocess.TimeoutExpired(args, timeout)
        if operation in ('terminate-instances', 'stop-instances', 'start-instances'):
            self.states[option('--instance-ids')] = {'terminate-instances': 'terminated',
                'stop-instances': 'stopped', 'start-instances': 'running'}[operation]
            self.clock.advance(4)
        if operation == 'list-objects-v2':
            if not self.bucket_exists:
                return error('NoSuchBucket')
            return reply({'Contents': [{'Key': 'source.bundle', 'Size': 100}]})
        if operation == 'delete-bucket':
            if self.cleanup_signal:
                number, self.cleanup_signal = self.cleanup_signal, 0
                assert set(trial.TRIAL_SIGNALS) <= signal.pthread_sigmask(signal.SIG_BLOCK, set())
                os.kill(os.getpid(), number)
            if not self.bucket_exists:
                return error('NoSuchBucket')
            self.bucket_exists = False
        if operation == 'delete-security-group':
            self.group_exists = False
        assert operation in ('terminate-instances', 'stop-instances', 'start-instances', 'create-bucket',
            'put-public-access-block', 'put-bucket-lifecycle-configuration', 'put-object', 'delete-objects',
            'delete-bucket', 'delete-security-group'), args
        return reply({})


@final
class GateTrialTests(unittest.TestCase):
    root: Path = Path()
    repo: Path = Path()
    clock: Clock = Clock()
    fake: FakeAws = FakeAws(Clock())
    command: trial.GateTrial | None = None

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        for command in (['git', 'init', '-q', str(self.repo)],
                        ['git', '-C', str(self.repo), 'config', 'user.name', 'Fixture'],
                        ['git', '-C', str(self.repo), 'config', 'user.email', 'fixture@example.invalid']):
            _ = subprocess.run(command, check=True, capture_output=True, timeout=10)
        _ = (self.repo / 'source').write_text('committed source')
        _ = subprocess.run(['git', '-C', str(self.repo), 'add', 'source'], check=True, timeout=10)
        _ = subprocess.run(['git', '-C', str(self.repo), 'commit', '-qm', 'Fixture source'], check=True, timeout=10)
        self.clock = Clock()
        self.fake = FakeAws(self.clock)
        _ = (self.root / 'trial.conf').write_text(trial_config())

    def run_trial(self, *, step: str = 'size', sizes: tuple[int, ...] = (16,), runs: int = 1, reserve: float = 4) -> trial.GateTrial:
        settings = trial.TrialSettings('us-east-1', 'fixture', 'ami-fixture', '26.05', 'subnet-fixture',
            'vpc-fixture', 'us-east-1a', '/dev/xvda', 32, 'm7a', 10, self.root / 'trial.conf')
        command = trial.GateTrial(AwsCli('fixture', 'us-east-1', 1, self.fake), settings, self.root / 'state',
            trial.SigningKey('fixture-access', 'fixture-secret'), clock=self.clock.read, pause=self.clock.advance)
        self.command = command
        with redirect_stdout(StringIO()):
            _ = command.run(self.repo, step, sizes, runs, reserve)
        return command

    def operations(self) -> list[str]:
        return [operation for _, operation, _ in self.fake.calls]

    def assert_cleanup(self) -> None:
        self.assertTrue(all(state == 'terminated' for state in self.fake.states.values()))
        operations = self.operations()
        self.assertIn('delete-bucket', operations)
        self.assertIn('delete-security-group', operations)
        self.assertLess(operations.index('terminate-instances'), operations.index('delete-bucket'))

    def test_full_attempt_has_five_times_result_and_report(self) -> None:
        command = self.run_trial()
        row = command.attempts[0]
        self.assertEqual(row.verdict, 'pass')
        self.assertEqual(row.times['request_to_running_s'], 5)
        self.assertEqual(row.times['running_to_tools_ready_s'], 10)
        self.assertEqual(row.times['tools_ready_to_gate_start_s'], 3)
        self.assertEqual(row.times['gate_s'], 10)
        self.assertEqual(row.times['teardown_s'], 7)
        self.assertGreater(row.dollars, 0)
        self.assertEqual(row.result['tests'], 19)
        report, content = command.report()
        self.assertEqual(report['cheapest_size'], 16)
        self.assertEqual(report['Batch'], 'not measured')
        self.assertIn('Medians:', content)
        saved = command.directory / f'gate-trial-{command.run_id}.json'
        self.assertTrue(saved.exists())
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertIn('i-fixture', saved.read_text())
        self.assertNotIn('i-fixture', content)
        self.assert_cleanup()

    def test_failure_cleans_all_resources(self) -> None:
        self.fake.failure = 'AccessDenied: fixture'
        command = self.run_trial()
        self.assertEqual(command.attempts[0].verdict, 'error')
        self.assertIn('AccessDenied', command.attempts[0].error)
        self.assert_cleanup()

    def test_ctrl_c_cleans_all_resources_and_saves_report(self) -> None:
        self.fake.interrupt = True
        command = self.run_trial()
        self.assertTrue(command.interrupted)
        self.assertEqual(command.attempts[0].verdict, 'cancelled')
        self.assert_cleanup()

    def test_refused_launch_records_text_and_continues(self) -> None:
        self.fake.refusals = 1
        command = self.run_trial(runs=2)
        self.assertIn('InstanceLimitExceeded', command.attempts[0].error)
        self.assertEqual(command.attempts[1].verdict, 'pass')
        self.assertEqual(self.operations().count('run-instances'), 2)
        self.assert_cleanup()

    def test_reserve_stops_unfunded_attempts(self) -> None:
        self.fake.compute = 10
        command = self.run_trial(runs=3)
        self.assertNotIn('run-instances', self.operations())
        self.assertTrue(all(row.verdict == 'not run' for row in command.attempts))
        self.assertTrue(all('reserve' in row.error for row in command.attempts))
        self.assertIn('delete-bucket', self.operations())

    def test_running_cost_reduces_room_for_next_attempt(self) -> None:
        command = self.run_trial(runs=2, reserve=0.216)
        self.assertEqual(self.operations().count('run-instances'), 1)
        self.assertEqual(command.attempts[1].verdict, 'not run')

    def test_failed_suite_keeps_times_counts_and_modules(self) -> None:
        self.fake.gate_status = 'fail'
        command = self.run_trial()
        self.assertEqual(command.attempts[0].result['failed_modules'], ['fixture/test_failure.py'])
        self.assertEqual(command.attempts[0].times['gate_s'], 10)
        self.assertIsNone(command.report()[0]['cheapest_size'])

    def test_stopped_start_uses_on_demand_and_two_markers(self) -> None:
        self.fake.gate_status = 'ready'
        command = self.run_trial(step='start', reserve=3)
        self.assertEqual([row.method for row in command.attempts], ['cold launch', 'stopped start'])
        self.assertNotIn('InstanceMarketOptions', self.fake.requests[0])
        self.assertEqual(self.operations().count('stop-instances'), 1)
        self.assertEqual(self.operations().count('start-instances'), 1)
        keys = [args[args.index('--key') + 1] for _, operation, args in self.fake.calls if operation == 'get-object']
        self.assertEqual(keys, ['16-1/ready-cold', '16-1/ready-warm'])
        self.assertTrue(all(row.dollars > 0 for row in command.attempts))
        self.assertTrue(all(row.times['gate_s'] is None for row in command.attempts))
        self.assertTrue(all(row.times['request_to_ready_s'] is not None for row in command.attempts))
        self.assert_cleanup()

    def test_launch_has_no_role_or_inbound_rule(self) -> None:
        _ = self.run_trial()
        request = self.fake.requests[0]
        self.assertNotIn('IamInstanceProfile', request)
        self.assertEqual(request['ImageId'], 'ami-fixture')
        self.assertEqual(request['InstanceInitiatedShutdownBehavior'], 'terminate')
        self.assertEqual(request['InstanceMarketOptions'], {'MarketType': 'spot', 'SpotOptions': {'SpotInstanceType': 'one-time'}})
        self.assertNotIn('authorize-security-group-ingress', self.operations())
        self.assertNotIn('iam', [service for service, _, _ in self.fake.calls])

    def test_user_data_schedules_shutdown_first_and_contains_only_url_credentials(self) -> None:
        _ = self.run_trial()
        data = self.fake.payloads[0]
        self.assertEqual(data.splitlines()[:2], ['#!/bin/sh', 'shutdown -h +30'])
        self.assertNotIn('fixture-secret', data)
        self.assertNotIn('tailscale', data.lower())
        self.assertNotIn('AWS_ACCESS_KEY_ID', data)
        self.assertIn('X-Amz-Credential=fixture-access', data)
        self.assertIn('X-Amz-Signature=', data)
        self.assertIn('nix --extra-experimental-features', data)
        self.assertNotIn('/etc/', data)
        self.assertNotIn('systemctl', data)
        self.assertNotIn('exec nix', data)
        self.assertNotIn('gate-trial.service', data)
        self.assertIn('export PATH=/run/current-system/sw/bin:$PATH', data)
        self.assertIn('type_dirs=[\'scripts\'], full=True', data)
        self.assertIn('admission_service.py', data)
        _ = compile(trial.GUEST, '<guest>', 'exec')

    def test_capacity_refusal_tries_next_public_zone_with_its_price(self) -> None:
        self.fake.capacity_zones = {'us-east-1a'}
        self.fake.zone_prices = {'us-east-1a': 0.3, 'us-east-1b': 0.5}
        command = self.run_trial()
        row = command.attempts[0]
        self.assertEqual(row.verdict, 'pass')
        self.assertEqual(row.zone, 'us-east-1b')
        self.assertEqual(row.prices['compute'], 0.5)
        self.assertEqual(row.capacity_refusals, [{'zone': 'us-east-1a', 'message': 'InsufficientInstanceCapacity: us-east-1a'}])
        interface = ledger.rows(self.fake.requests[0], 'NetworkInterfaces')[0]
        self.assertEqual(interface['SubnetId'], 'subnet-second')
        zones = [args[args.index('--availability-zone') + 1] for _, op, args in self.fake.calls
                 if op == 'describe-spot-price-history']
        self.assertEqual(zones, ['us-east-1a', 'us-east-1b'])
        self.assertIn('InsufficientInstanceCapacity', command.report()[1])
        self.assert_cleanup()

    def test_all_zones_refuse_and_next_attempt_runs(self) -> None:
        self.fake.capacity_zones = {'us-east-1a', 'us-east-1b'}
        def remove_refusals(_attempt: trial.Attempt) -> None:
            self.fake.capacity_zones.clear()
        original = trial.GateTrial.row
        def show(attempt: trial.Attempt) -> str:
            if attempt.verdict == 'no capacity':
                remove_refusals(attempt)
            return original(attempt)
        with mock.patch.object(trial.GateTrial, 'row', side_effect=show):
            command = self.run_trial(runs=2)
        self.assertEqual(command.attempts[0].verdict, 'no capacity')
        self.assertEqual([row['zone'] for row in command.attempts[0].capacity_refusals], ['us-east-1a', 'us-east-1b'])
        self.assertEqual(command.attempts[1].verdict, 'pass')
        self.assertEqual(self.operations().count('run-instances'), 3)
        self.assert_cleanup()

    def test_next_zone_price_must_fit_reserve(self) -> None:
        self.fake.capacity_zones = {'us-east-1a'}
        self.fake.zone_prices = {'us-east-1b': 10}
        command = self.run_trial()
        self.assertEqual(command.attempts[0].verdict, 'not run')
        self.assertIn('reserve', command.attempts[0].error)
        self.assertEqual(self.operations().count('run-instances'), 1)
        self.assertEqual(self.fake.launches, 0)

    def test_lost_launch_reply_retries_with_same_token_and_continues(self) -> None:
        self.fake.lost_reply = True
        command = self.run_trial()
        self.assertEqual(command.attempts[0].verdict, 'pass')
        self.assertEqual(self.operations().count('run-instances'), 2)
        self.assertEqual(len(self.fake.tokens), 1)
        self.assertEqual(self.fake.requests[0], self.fake.requests[1])
        self.assertEqual(command.attempts[0].instance_id, 'i-fixture-1')
        self.assertGreater(command.attempts[0].dollars, 0)
        self.assertEqual(command.remaining(), [])
        self.assert_cleanup()

    def test_all_launch_replies_lost_stops_attempts_and_reports_timer(self) -> None:
        self.fake.lost_replies = 10
        self.fake.hide_instances = True
        command = self.run_trial(runs=2)
        self.assertEqual(self.operations().count('run-instances'), 3)
        self.assertEqual(len(self.fake.tokens), 1)
        self.assertEqual(command.attempts[0].verdict, 'error')
        self.assertEqual(command.attempts[1].verdict, 'not run')
        remaining = 'launch reply lost; the guest shutdown timer is the protection'
        self.assertEqual(command.report()[0]['remaining'], [remaining])
        saved = command.directory / f'gate-trial-{command.run_id}.txt'
        self.assertIn('Remaining: ' + remaining, saved.read_text())
        self.assertFalse(self.fake.bucket_exists)
        self.assertFalse(self.fake.group_exists)

    def test_sigterm_and_sighup_clean_all_resources(self) -> None:
        for number in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(number=number):
                self.fake = FakeAws(self.clock)
                self.fake.signal_number = number
                command = self.run_trial()
                self.assertTrue(command.interrupted)
                self.assertEqual(command.attempts[0].verdict, 'cancelled')
                self.assertEqual(command.remaining(), [])
                self.assert_cleanup()

    def test_start_removes_old_stopped_tagged_instances(self) -> None:
        self.fake.states = {'i-fixture-old': 'stopped'}
        self.fake.tags = {'i-fixture-old': 'earlier-trial'}
        command = self.run_trial()
        self.assertEqual(self.fake.states['i-fixture-old'], 'terminated')
        self.assertLess(self.operations().index('terminate-instances'), self.operations().index('run-instances'))
        self.assertEqual(command.attempts[0].verdict, 'pass')
        self.assert_cleanup()

    def test_start_removes_old_stopping_tagged_instance(self) -> None:
        self.fake.states = {'i-fixture-old': 'stopping'}
        self.fake.tags = {'i-fixture-old': 'earlier-trial'}
        command = self.run_trial()
        self.assertEqual(self.fake.states['i-fixture-old'], 'terminated')
        self.assertLess(self.operations().index('terminate-instances'), self.operations().index('run-instances'))
        self.assertEqual(command.attempts[0].verdict, 'pass')

    def test_cleanup_blocks_signals_until_resources_and_report_complete(self) -> None:
        for number in trial.TRIAL_SIGNALS:
            with self.subTest(number=number):
                self.fake = FakeAws(self.clock)
                self.fake.cleanup_signal = number
                command = self.run_trial()
                self.assertTrue(command.interrupted)
                self.assertFalse(self.fake.bucket_exists)
                self.assertFalse(self.fake.group_exists)
                record = command.directory / f'gate-trial-{command.run_id}.json'
                self.assertEqual(ledger.mapping(cast(object, json.loads(record.read_text())))['remaining'], [])
                self.assertIn('Remaining: none', record.with_suffix('.txt').read_text())
                self.assert_cleanup()

    def test_lost_resource_create_replies_still_remove_resources(self) -> None:
        for resource in ('bucket', 'group'):
            with self.subTest(resource=resource):
                self.fake = FakeAws(self.clock)
                self.fake.lost_bucket_reply = resource == 'bucket'
                self.fake.lost_group_reply = resource == 'group'
                with self.assertRaises(subprocess.TimeoutExpired):
                    _ = self.run_trial()
                command = self.command
                assert command is not None
                self.assertFalse(self.fake.bucket_exists)
                self.assertFalse(self.fake.group_exists)
                self.assertFalse(command.cleanup_errors)
                self.assertEqual(command.remaining(), [])
                record = command.directory / f'gate-trial-{command.run_id}.json'
                self.assertEqual(ledger.mapping(cast(object, json.loads(record.read_text())))['remaining'], [])
                self.assertIn('delete-bucket', self.operations())
                self.assertIn('describe-security-groups', self.operations())
                if resource == 'group':
                    self.assertIn('delete-security-group', self.operations())

    def test_bucket_missing_at_cleanup_counts_as_removed(self) -> None:
        self.fake.refuse_bucket = True
        with self.assertRaises(AwsUnavailable):
            _ = self.run_trial()
        command = self.command
        assert command is not None
        self.assertFalse(command.cleanup_errors)
        self.assertEqual(command.remaining(), [])

    def test_terminate_timeout_keeps_bucket_group_and_report_cleanup(self) -> None:
        self.fake.terminate_timeout = True
        command = self.run_trial()
        self.assertIn('delete-bucket', self.operations())
        self.assertIn('delete-security-group', self.operations())
        record, text = command.report()
        self.assertTrue((command.directory / f'gate-trial-{command.run_id}.json').exists())
        self.assertEqual(record['remaining'], ['instance i-fixture-1'])
        self.assertIn('Remaining: instance i-fixture-1', text)
        self.assertTrue(command.cleanup_errors)

    def test_group_dependency_retries_then_deletes(self) -> None:
        self.fake.delete_dependencies = 2
        command = self.run_trial()
        self.assertEqual(self.operations().count('delete-security-group'), 3)
        self.assertEqual(command.remaining(), [])
        self.assertFalse(command.cleanup_errors)

    def test_group_dependency_wait_is_bounded_and_reports_group(self) -> None:
        self.fake.delete_dependencies = 10000
        command = self.run_trial()
        self.assertLess(self.operations().count('delete-security-group'), 100)
        self.assertEqual(command.remaining(), ['group sg-fixture'])
        self.assertTrue(command.cleanup_errors)

    def test_error_log_download_for_both_steps_and_missing_log(self) -> None:
        self.fake.guest_payload = {'verdict': 'error', 'error': 'Tool preparation failed'}
        for step in ('size', 'start'):
            with self.subTest(step=step):
                command = self.run_trial(step=step)
                row = command.attempts[0]
                self.assertEqual(row.verdict, 'error')
                self.assertEqual(row.error, 'Tool preparation failed')
                self.assertEqual(Path(row.log_path).read_text(), 'fixture gate log')
                self.assertIn(row.log_path, command.row(row))
                self.assertLess(row.ended_at - row.request_at, 60)
        self.fake.missing_log = True
        command = self.run_trial()
        self.assertEqual(command.attempts[0].log_path, '')
        self.assertNotIn('Log:', command.attempts[0].error)

    def test_cost_starts_at_request_even_before_running(self) -> None:
        command = self.run_trial()
        row = command.attempts[0]
        prices = trial.Prices(**row.prices)
        self.assertGreater(row.running_at, row.request_at)
        self.assertAlmostEqual(row.dollars, prices.lifetime(row.terminated_at - row.request_at, 32) + command.s3_cost)

    def test_one_value_for_each_table_header(self) -> None:
        command = self.run_trial()
        _, text = command.report()
        header = next(line for line in text.splitlines() if line.startswith('size |'))
        self.assertEqual(len(command.row(command.attempts[0]).split(' | ')), len(header.split(' | ')))
        self.assertEqual(header.split(' | ').count('request to ready'), 1)
        self.assertIn('Remaining: none', text)

    def test_tunables_are_read_at_use(self) -> None:
        command = self.run_trial()
        self.assertEqual(command.option('target_gate_s'), 420)
        path = self.root / 'trial.conf'
        _ = path.write_text(path.read_text().replace('gate_trial_target_gate_s=420', 'gate_trial_target_gate_s=5'))
        self.assertIsNone(command.report()[0]['cheapest_size'])
        _ = path.write_text(path.read_text().replace('gate_trial_url_lifetime_s=7200', 'gate_trial_url_lifetime_s=3600'))
        self.assertEqual(parse_qs(urlsplit(command.links('fixture', 'size')['result']).query)['X-Amz-Expires'], ['3600'])
        _ = path.write_text(path.read_text().replace('gate_trial_lifetime_minutes=30', 'gate_trial_lifetime_minutes=1'))
        command.attempts.clear()
        command.discover_subnets()
        attempt = trial.Attempt(16, 2, 'Spot gate')
        command.attempts.append(attempt)
        with redirect_stdout(StringIO()):
            command.measure(attempt, 'size', 4)
        self.assertEqual(self.fake.payloads[-1].splitlines()[1], 'shutdown -h +1')

    def test_rendered_boot_trap_publishes_error_and_log(self) -> None:
        links = {'step': 'size', 'result': 'https://fixture.invalid/result', 'log': 'https://fixture.invalid/log',
                 'ready-cold': 'https://fixture.invalid/cold', 'ready-warm': 'https://fixture.invalid/warm'}
        for step, warm in (('size', False), ('start', False), ('start', True)):
            with self.subTest(step=step, warm=warm), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                links['step'] = step
                _ = (root / 'boot.log').write_text('fixture nix failure')
                if warm:
                    (root / 'prepared').touch()
                curl = root / 'curl'
                _ = curl.write_text('#!' + sys.executable + "\nimport json, pathlib, sys\n"
                    + "args=sys.argv[1:]\nbody=pathlib.Path(args[args.index('--data-binary')+1][1:]).read_text()\n"
                    + "with pathlib.Path(__file__).with_name('uploads').open('a') as out: out.write(json.dumps([args[-1],body])+'\\n')\n")
                curl.chmod(0o700)
                # Execute the rendered trap with temporary paths and a fake curl.
                script = 'set -e\n' + trial.boot_trap(links).replace('/var/lib/gate-trial', str(root)) + 'false\n'
                result = subprocess.run(['sh', '-c', script], env={**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH']},
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 1, result.stderr)
                uploads = [cast(list[str], json.loads(line)) for line in (root / 'uploads').read_text().splitlines()]
                self.assertEqual(uploads[0], [links['log'], 'fixture nix failure'])
                key = 'result' if step == 'size' else 'ready-warm' if warm else 'ready-cold'
                self.assertEqual(uploads[1][0], links[key])
                payload = ledger.mapping(cast(object, json.loads(uploads[1][1])))
                self.assertEqual(payload['verdict'], 'error')
                self.assertIn('exit 1', str(payload['error']))

    def test_source_bundle_contains_head_without_uncommitted_changes(self) -> None:
        _ = (self.repo / 'source').write_text('uncommitted change')
        command = self.run_trial()
        head = subprocess.check_output(['git', '-C', str(self.repo), 'rev-parse', 'HEAD'], text=True, timeout=10).strip()
        self.assertEqual(command.commit, head)
        bundle = self.root / 'captured.bundle'
        _ = bundle.write_bytes(self.fake.bundles[0])
        checkout = self.root / 'captured'
        _ = subprocess.run(['git', 'clone', '-q', str(bundle), str(checkout)], check=True, capture_output=True, timeout=10)
        self.assertEqual((checkout / 'source').read_text(), 'committed source')
        self.assertEqual(subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'HEAD'], text=True, timeout=10).strip(), head)
        self.assertEqual(subprocess.check_output(['git', '-C', str(self.repo), 'show', 'HEAD:source'], text=True, timeout=10), 'committed source')


@final
class GuestChild:
    def __init__(self) -> None:
        self.terminated: bool = False

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, *, timeout: float) -> int:
        assert timeout == 10
        return 0


@final
class GuestTests(unittest.TestCase):
    def test_failed_cold_publish_keeps_marker_absent_and_trap_uses_cold_link(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            links = {'step': 'start', 'log': 'https://fixture.invalid/log',
                     'ready-cold': 'https://fixture.invalid/cold', 'ready-warm': 'https://fixture.invalid/warm'}
            _ = (root / 'links.json').write_text(json.dumps(links))
            _ = (root / 'boot.log').write_text('fixture publish failure')
            curl = root / 'curl'
            _ = curl.write_text('#!' + sys.executable + "\nimport json, pathlib, sys\n"
                + "args=sys.argv[1:]\nbody=pathlib.Path(args[args.index('--data-binary')+1][1:]).read_text()\n"
                + "with pathlib.Path(__file__).with_name('uploads').open('a') as out: out.write(json.dumps([args[-1],body])+'\\n')\n")
            curl.chmod(0o700)
            guest = trial.GUEST.replace("'/var/lib/gate-trial'", repr(str(root)))
            wrapper = root / 'guest-test.py'
            _ = wrapper.write_text('from unittest import mock\nimport subprocess\n'
                + 'with (mock.patch("urllib.request.urlopen", side_effect=OSError("publish failed")),\n'
                + '      mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "version", ""))):\n'
                + f'    exec({guest!r}, {{}})\n')
            script = ('set -e\n' + trial.boot_trap(links).replace('/var/lib/gate-trial', str(root))
                      + shlex.join([sys.executable, str(wrapper)]) + '\n')
            result = subprocess.run(['sh', '-c', script], capture_output=True, text=True, timeout=10,
                                    env={**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH']})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('publish failed', result.stderr)
            self.assertFalse((root / 'prepared').exists())
            uploads = [cast(list[str], json.loads(line)) for line in (root / 'uploads').read_text().splitlines()]
            self.assertEqual(uploads[0], [links['log'], 'fixture publish failure'])
            self.assertEqual(uploads[1][0], links['ready-cold'])
            self.assertEqual(ledger.mapping(cast(object, json.loads(uploads[1][1])))['verdict'], 'error')

    def test_guest_runs_full_gate_with_types_and_keeps_failures(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'repo').mkdir()
            links = {'step': 'size', 'commit': 'fixture-head', 'source': 'https://fixture.invalid/source',
                     'log': 'https://fixture.invalid/log', 'result': 'https://fixture.invalid/result'}
            _ = (root / 'links.json').write_text(json.dumps(links))
            uploads: dict[str, bytes] = {}
            def http(request: str | Request, *, timeout: float) -> BytesIO:
                self.assertIn(timeout, (30, 60))
                if isinstance(request, Request):
                    uploads[request.full_url] = cast(bytes, request.data)
                    return BytesIO()
                return BytesIO(b'fixture bundle')
            jobs = (
                merge_gate.GateJobResult('fixture', 'test_broken.py', 1, 2, 'failed',
                    times={'wall_s': 2, 'cpu_s': 1, 'tests_s': 1, 'tests_run': 3, 'failures': 1,
                           'errors': 0, 'skipped': 1, 'tests': []}),
                merge_gate.GateJobResult('types', 'basedpyright', 0, 1, '0 errors'),
            )
            code = trial.GUEST.replace("'/var/lib/gate-trial'", repr(str(root))).replace("'/root/.claude'", repr(str(root / 'home'))).replace("'/root/.claude/config'", repr(str(root / 'home/config')))
            child = GuestChild()
            with (mock.patch.dict(os.environ), mock.patch.object(sys, 'path', list(sys.path)),
                  mock.patch('urllib.request.urlopen', side_effect=http),
                  mock.patch('subprocess.run', return_value=subprocess.CompletedProcess(['fixture'], 0, 'version', '')),
                  mock.patch('subprocess.Popen', return_value=child),
                  mock.patch.object(merge_gate, 'run_suites', return_value=merge_gate.GateRun(jobs, 4, 1)) as run,
                  mock.patch.object(merge_gate, 'rollback_signals', return_value=nullcontext()),
                  mock.patch.object(merge_gate, 'typecheck_python', return_value=root / 'python')):
                previous = Path.cwd()
                try:
                    exec(compile(code, '<guest>', 'exec'), {})
                finally:
                    os.chdir(previous)
            payload = ledger.mapping(cast(object, json.loads(uploads[links['result']])))
            self.assertEqual(payload['verdict'], 'fail')
            self.assertEqual(payload['tests'], 3)
            self.assertEqual(payload['skips'], 1)
            self.assertEqual(payload['failed_modules'], ['fixture/test_broken.py'])
            self.assertIn('gate_started_at', payload)
            self.assertIn('gate_finished_at', payload)
            run.assert_called_once_with(root / 'repo', type_dirs=['scripts'], full=True)
            self.assertTrue(child.terminated)
            self.assertIn(links['log'], uploads)

    def test_start_guest_repeats_ready_marker_without_a_gate(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            links = {'step': 'start', 'boot': 'cold', 'log': 'https://fixture.invalid/log',
                     'ready-cold': 'https://fixture.invalid/cold', 'ready-warm': 'https://fixture.invalid/warm'}
            _ = (root / 'links.json').write_text(json.dumps(links))
            uploads: dict[str, dict[str, object]] = {}
            def http(request: Request, *, timeout: float) -> BytesIO:
                self.assertEqual(timeout, 30)
                uploads[request.full_url] = ledger.mapping(cast(object, json.loads(cast(bytes, request.data))))
                return BytesIO()
            code = trial.GUEST.replace("'/var/lib/gate-trial'", repr(str(root)))
            with (mock.patch('urllib.request.urlopen', side_effect=http),
                  mock.patch('subprocess.run', return_value=subprocess.CompletedProcess(['fixture'], 0, 'version', '')),
                  mock.patch('subprocess.Popen', side_effect=AssertionError('admission during start trial')),
                  mock.patch.object(merge_gate, 'run_suites', side_effect=AssertionError('gate during start trial')),
                  mock.patch.object(merge_gate, 'typecheck_python', side_effect=AssertionError('types during start trial'))):
                exec(compile(code, '<guest>', 'exec'), {})
                exec(compile(code, '<guest>', 'exec'), {})
            self.assertEqual(set(uploads), {links['ready-cold'], links['ready-warm']})
            self.assertTrue(all(row['verdict'] == 'ready' for row in uploads.values()))
            self.assertTrue(all('gate_started_at' not in row for row in uploads.values()))


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
class SpendTests(CostFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        with self.settings.open('a') as output:
            _ = output.write('\n' + trial_config())

    def test_daily_and_monthly_room_stop_before_aws_or_credentials(self) -> None:
        for daily, monthly in ((17, 0), (0, 397), (17, 397)):
            with self.subTest(daily=daily, monthly=monthly):
                costs = ledger.CostEstimate(1000, ledger.DailyTotal('fixture', Decimal(daily)),
                    ledger.MonthlyTotal('fixture', Decimal(monthly)), (), 0, 0)
                with (mock.patch.object(ledger.CostLedger, 'read_snapshot', return_value=ledger.LedgerEvidence()),
                      mock.patch.object(ledger, 'costs_from_evidence', return_value=costs),
                      mock.patch.object(admin_secrets.AdminSecretSession, 'get', side_effect=AssertionError('credential read')),
                      mock.patch.object(AwsCli, 'call', side_effect=AssertionError('AWS call')),
                      redirect_stdout(StringIO())):
                    self.assertEqual(trial.main(['--step', 'size', '--repo', str(self.root), '--approve-resources'], self.settings, self.state), 1)

    def test_deploy_only_dispatches_trial(self) -> None:
        with (mock.patch('sys.argv', ['deploy.py', 'gate-trial', '--step', 'size', '--repo', '.', '--approve-resources']),
              mock.patch.object(trial, 'main', return_value=0) as dispatch):
            self.assertEqual(deploy.main(), 0)
            self.assertEqual(dispatch.call_args.args[0], ['--step', 'size', '--repo', '.', '--approve-resources'])
