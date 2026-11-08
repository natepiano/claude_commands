"""Measure trial lifecycles with a fake AWS command and clock."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import redirect_stdout
from datetime import UTC, datetime
from decimal import Decimal
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
from typing import cast, final, override
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from scripts.aws_target import admin_secrets, deploy, gate_trial as trial, ledger
from scripts.aws_target.aws_target import AwsCli, AwsUnavailable
from scripts.aws_target.ledger_test_support import CostFixture
from scripts.production import merge_gate
from scripts.lib import conf


def trial_config() -> str:
    content = (Path(__file__).resolve().parents[2] / 'config/aws_target.conf').read_text()
    return content[content.index('# Gate trial limits.'):]


@final
class Clock:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.now: float = 1000

    def read(self) -> float:
        with self.lock:
            return self.now

    def advance(self, seconds: float) -> None:
        with self.lock:
            self.now += seconds


@final
class FakeAws:
    def __init__(self, clock: Clock) -> None:
        self.lock = threading.RLock()
        self.before_result: Callable[[str], None] | None = None
        self.attempt_tags: dict[str, str] = {}
        self.instance_sizes: dict[str, int] = {}
        self.peak_vcpus: int = 0
        self.lost_tokens: set[str] = set()
        self.lost_attempts: set[str] = set()
        self.clock: Clock = clock
        self.calls: list[tuple[str, str, list[str]]] = []
        self.states: dict[str, str] = {}
        self.refusals: int = 0
        self.capacity_zones: set[str] = set()
        self.lost_reply: bool = False
        self.refuse_after_lost_reply: bool = False
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
        self.termination_transition: bool = False
        self.observed_states: list[str] = []
        self.delete_dependencies: int = 0
        self.signal_number: int = 0
        self.handler_thread: int = threading.get_ident()
        self.signal_handled: Callable[[], None] | None = None
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
        args = list(arguments)
        if 'get-object' in args:
            with self.lock:
                number, self.signal_number = self.signal_number, 0
            if number:
                signal.pthread_kill(self.handler_thread, number)
                if self.signal_handled is not None:
                    self.signal_handled()
        if 'get-object' in args and self.before_result is not None:
            key = args[args.index('--key') + 1]
            if not key.endswith('/log'):
                self.before_result(key)
        with self.lock:
            return self.command(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check, env=env)

    def command(self, arguments: Sequence[str], *, capture_output: bool, text: bool,
                timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        _ = (capture_output, text, check, env)
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
        if operation == 'get-service-quota':
            assert service == 'service-quotas'
            assert option('--service-code') == 'ec2'
            assert option('--quota-code') == 'L-34B43A08'
            return reply({'Quota': {'Value': 128}})
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
            if self.refuse_after_lost_reply and self.requests:
                return error('An error occurred (RequestLimitExceeded): fixture retry refusal')
            if self.refusals:
                self.refusals -= 1
                return error('An error occurred (InstanceLimitExceeded): fixture refusal')
            token = ledger.text(request, 'ClientToken')
            if token not in self.tokens:
                self.launches += 1
                instance = 'i-fixture-' + str(self.launches)
                self.tokens[token] = instance
                self.states[instance] = 'running'
                tags = {ledger.text(row, 'Key'): ledger.text(row, 'Value')
                        for row in ledger.rows(ledger.rows(request, 'TagSpecifications')[0], 'Tags')}
                self.tags[instance] = tags['GateTrial']
                self.attempt_tags[instance] = tags['GateTrialAttempt']
                self.instance_sizes[instance] = int(ledger.text(request, 'InstanceType').split('.')[1].removesuffix('xlarge')) * 4
                self.peak_vcpus = max(self.peak_vcpus, sum(size for item, size in self.instance_sizes.items()
                                                          if self.states[item] != 'terminated'))
            instance = self.tokens[token]
            self.clock.advance(2)
            self.payloads.append(Path(option('--user-data').removeprefix('file://')).read_text())
            self.requests.append(ledger.mapping(cast(object, json.loads(option('--cli-input-json')))))
            if (self.lost_reply or self.lost_replies or ledger.text(request, 'ClientToken') in self.lost_tokens
                    or self.attempt_tags[instance] in self.lost_attempts):
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
                attempt = next((value.partition('Values=')[2] for value in filters
                                if value.startswith('Name=tag:GateTrialAttempt,')), None)
                instances = [instance for instance, state in self.states.items()
                             if state in states and (instance in self.tags if old else self.tags.get(instance) == wanted)
                             and not self.hide_instances and (attempt is None or self.attempt_tags.get(instance) == attempt)]
                return reply({'Reservations': [{'Instances': [{'InstanceId': instance} for instance in instances]}]})
            self.clock.advance(3)
            instance = option('--instance-ids')
            state = self.states[instance]
            self.observed_states.append(state)
            if state == 'shutting-down':
                self.states[instance] = 'terminated'
            return reply({'Reservations': [{'Instances': [{'InstanceId': instance, 'LaunchTime': 'fixture-boot',
                          'State': {'Name': state}}]}]})
        if operation == 'get-object':
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
                    'gate_finished_at': at + 23, 'memory_peak_bytes': 123456789, 'tests': 19, 'skips': 2, 'verdict': self.gate_status,
                    'modules': [{'module': 'fixture/test_failure.py', 'status': int(self.gate_status != 'pass'), 'seconds': 10}],
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
            if operation == 'terminate-instances' and self.termination_transition:
                self.states[option('--instance-ids')] = 'shutting-down'
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
    output: str = ''

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
        _ = (self.root / 'flake.lock').write_text(json.dumps({'root': 'host', 'nodes': {
            'host': {'inputs': {'nixpkgs': 'tools'}}, 'tools': {'locked': {'rev': 'f' * 40}}}}))

    def run_trial(self, *, step: str = 'size', sizes: tuple[int, ...] = (16,), runs: int = 1, reserve: float = 4,
                  vcpus: int = 16, family: str = 'm7a') -> trial.GateTrial:
        settings = trial.TrialSettings('us-east-1', 'fixture', 'ami-fixture', '26.05', 'subnet-fixture',
            'vpc-fixture', 'us-east-1a', '/dev/xvda', 32, family, 10, self.root / 'trial.conf')
        command = trial.GateTrial(AwsCli('fixture', 'us-east-1', 1, self.fake), settings, self.root / 'state',
            trial.SigningKey('fixture-access', 'fixture-secret'), clock=self.clock.read, pause=self.clock.advance)
        self.command = command
        def signal_handled() -> None:
            with command.changed:
                self.assertTrue(command.changed.wait_for(lambda: command.interrupted, timeout=10))
        self.fake.signal_handled = signal_handled
        output = StringIO()
        try:
            with (redirect_stdout(output), mock.patch.dict(os.environ, {'MERGE_GATE_STATE_DIR': str(self.root / 'measures')}),
                  mock.patch.object(trial, 'NIXOS_LOCK', self.root / 'flake.lock')):
                _ = command.run(self.repo, step, sizes, runs, reserve, vcpus)
        finally:
            self.output = output.getvalue()
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
        settings = self.root / 'trial.conf'
        _ = settings.write_text(trial_config().replace('gate_trial_module_limit_s=600', 'gate_trial_module_limit_s=450'))
        command = self.run_trial()
        links = ledger.mapping(cast(object, json.loads(self.fake.payloads[0].split("<<'GATE_LINKS'\n", 1)[1].split('\nGATE_LINKS', 1)[0])))
        self.assertEqual(links['module_time_limit_s'], 450)
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
        saved_report = ledger.mapping(cast(object, json.loads(saved.read_text())))
        saved_attempt = ledger.mapping(cast(list[object], saved_report['attempts'])[0])
        self.assertEqual(ledger.mapping(saved_attempt['result'])['modules'], row.result['modules'])
        self.assert_cleanup()

    def test_report_keeps_cpu_measurements_and_shows_use_percent(self) -> None:
        payload: dict[str, object] = {'verdict': 'pass', 'busy_cpu_s': 120, 'gate_s': 10,
                                      'vcpus': 16, 'cpu_use_percent': 75.0}
        self.fake.guest_payload = payload
        command = self.run_trial()
        report, content = command.report()
        self.assertIn('CPU use percent', content)
        self.assertIn(' | 75.0 | ', command.row(command.attempts[0]))
        attempts = cast(list[dict[str, object]], report['attempts'])
        self.assertEqual(ledger.mapping(attempts[0]['result']), payload)

    def test_terminate_waits_through_shutting_down_and_runs_next_attempt(self) -> None:
        self.fake.termination_transition = True
        command = self.run_trial(runs=2)
        self.assertEqual([row.verdict for row in command.attempts], ['pass', 'pass'])
        self.assertEqual(self.fake.observed_states, ['running', 'shutting-down', 'terminated'] * 2)
        self.assertEqual(command.cleanup_errors, [])
        self.assertEqual(command.remaining(), [])
        self.assert_cleanup()

    def test_log_upload_interval_changes_projection_and_s3_cost(self) -> None:
        settings = self.root / 'trial.conf'
        _ = settings.write_text(trial_config().replace('gate_trial_log_push_s=30', 'gate_trial_log_push_s=15'))
        for step, boots in (('size', 1), ('start', 2)):
            with self.subTest(step=step):
                self.fake.gate_status = 'ready' if step == 'start' else 'pass'
                command = self.run_trial(step=step)
                prices = trial.Prices(**command.attempts[0].prices)
                lifetime = 30 * 60
                uploads = lifetime // 15 * boots
                self.assertEqual(command.puts, 2 + 3 * boots + uploads)
                expected = (prices.lifetime(lifetime, 32) * boots + prices.put * (5 + boots + uploads)
                            + prices.get * (lifetime * 2 / command.option('poll_pause_s') + 100))
                self.assertAlmostEqual(command.projection(prices, step), expected)
                self.assertIn('while sleep 15; do', self.fake.payloads[-1])

    def test_lost_launch_then_refusal_keeps_reservation_and_sweeps(self) -> None:
        self.fake.lost_reply = True
        self.fake.refuse_after_lost_reply = True
        sweep = trial.GateTrial.sweep
        checked: list[bool] = []
        def check(command: trial.GateTrial, *, stopped: bool = False, attempt: trial.Attempt | None = None) -> None:
            if attempt is not None:
                self.assertFalse(attempt.clear_refusal)
                self.assertIn((attempt.size, attempt.attempt), command.reservations)
                checked.append(True)
            sweep(command, stopped=stopped, attempt=attempt)
        with mock.patch.object(trial.GateTrial, 'sweep', new=check):
            command = self.run_trial(runs=2)
        self.assertEqual(checked, [True])
        self.assertTrue(command.launch_reply_lost)
        self.assertEqual([row.verdict for row in command.attempts], ['error', 'not run'])
        self.assertIn('RequestLimitExceeded', command.attempts[0].error)
        self.assertEqual(self.fake.launches, 1)
        self.assert_cleanup()

    def test_duplicate_sizes_have_one_identity_per_attempt(self) -> None:
        command = self.run_trial(sizes=(16, 16, 32), vcpus=32)
        self.assertEqual([row.size for row in command.attempts], [16, 32])
        self.assertEqual(self.fake.launches, 2)
        self.assertTrue(all(row.verdict == 'pass' for row in command.attempts))

    def test_cancel_before_warm_request_bills_no_warm_lifetime(self) -> None:
        record = trial.GateTrial.record
        def cancel(command: trial.GateTrial, attempt: trial.Attempt, *, ended: bool = False) -> None:
            record(command, attempt, ended=ended)
            if attempt.method == 'cold launch' and attempt.stopped_at:
                command.cancel()
        self.fake.gate_status = 'ready'
        with mock.patch.object(trial.GateTrial, 'record', new=cancel):
            command = self.run_trial(step='start')
        warm = next(row for row in command.attempts if row.method == 'stopped start')
        self.assertEqual(warm.request_at, 0)
        self.assertEqual(warm.verdict, 'cancelled')
        self.assertEqual(warm.dollars, 0)
        self.assertGreater(command.spent, 0)
        self.assertLess(command.spent, 1)
        self.assert_cleanup()

    def test_all_sizes_overlap_within_vcpu_limit(self) -> None:
        barrier = threading.Barrier(4, timeout=10)
        def result(_key: str) -> None:
            _ = barrier.wait()
        self.fake.before_result = result
        command = self.run_trial(sizes=(8, 16, 32, 64), vcpus=120)
        self.assertEqual(self.fake.peak_vcpus, 120)
        self.assertTrue(all(row.verdict == 'pass' for row in command.attempts))
        self.assertEqual(len(command.attempts), 4)
        self.assert_cleanup()

    def test_vcpu_limit_holds_for_multiple_waves_and_start_children(self) -> None:
        for step in ('size', 'start'):
            with self.subTest(step=step):
                self.fake = FakeAws(self.clock)
                if step == 'start':
                    self.fake.gate_status = 'ready'
                barrier = threading.Barrier(2, timeout=10)
                def result(_key: str) -> None:
                    _ = barrier.wait()
                self.fake.before_result = result
                command = self.run_trial(step=step, runs=4, vcpus=32)
                self.assertEqual(self.fake.peak_vcpus, 32)
                self.assertTrue(all(row.verdict == ('pass' if step == 'size' else 'ready') for row in command.attempts))
                self.assertEqual(self.fake.launches, 4)
                self.assertEqual(len(command.attempts), 4 if step == 'size' else 8)
                self.assert_cleanup()

    def test_attempt_larger_than_vcpu_limit_is_not_run(self) -> None:
        command = self.run_trial(sizes=(64,), vcpus=32)
        self.assertEqual(command.attempts[0].verdict, 'not run')
        self.assertIn('--vcpus', command.attempts[0].error)
        self.assertNotIn('run-instances', self.operations())

    def test_reserve_waits_for_running_attempts_before_next_launch(self) -> None:
        barrier = threading.Barrier(2, timeout=10)
        def result(key: str) -> None:
            if key.startswith(('8-', '16-')):
                _ = barrier.wait()
                with self.fake.lock:
                    self.assertEqual(self.fake.launches, 2)
                _ = barrier.wait()
            else:
                with self.fake.lock:
                    self.assertTrue(any(self.fake.states[item] == 'terminated' for item in ('i-fixture-1', 'i-fixture-2')))
        self.fake.before_result = result
        command = self.run_trial(sizes=(8, 16, 32), vcpus=120, reserve=0.45)
        self.assertTrue(all(row.verdict == 'pass' for row in command.attempts))
        self.assertEqual(self.fake.launches, 3)
        self.assertGreaterEqual(self.fake.peak_vcpus, 32)
        self.assertLessEqual(self.fake.peak_vcpus, 48)
        self.assertLess(command.spent + command.s3_cost, 0.45)

    def test_signal_cancels_parallel_waits_and_prints_final_report(self) -> None:
        main_thread = threading.get_ident()
        for number in trial.TRIAL_SIGNALS:
            with self.subTest(number=number):
                self.fake = FakeAws(self.clock)
                barrier = threading.Barrier(2, timeout=10)
                def result(key: str) -> None:
                    _ = barrier.wait()
                    if key.startswith('8-'):
                        # Wake the Python handler's thread instead of an unrelated helper thread.
                        signal.pthread_kill(main_thread, number)
                    command = self.command
                    assert command is not None
                    with command.changed:
                        self.assertTrue(command.changed.wait_for(lambda: command.interrupted, timeout=10))
                self.fake.before_result = result
                command = self.run_trial(sizes=(8, 16, 32), vcpus=24)
                self.assertEqual(self.fake.launches, 2)
                self.assertTrue(command.interrupted)
                self.assertEqual([row.verdict for row in command.attempts].count('cancelled'), 2)
                self.assertEqual([row.verdict for row in command.attempts].count('not run'), 1)
                self.assertIn('Remaining: none', self.output)
                self.assert_cleanup()

    def test_lost_reply_stops_new_attempts_and_preserves_other_worker(self) -> None:
        self.fake.lost_attempts = {'16-1'}
        def result(key: str) -> None:
            if not key.startswith('8-1/'):
                return
            command = self.command
            assert command is not None
            with command.changed:
                self.assertTrue(command.changed.wait_for(
                    lambda: any(row.size == 16 and row.verdict == 'error' for row in command.attempts), timeout=10))
                self.assertTrue(command.launch_reply_lost)
            with self.fake.lock:
                own = next(item for item, name in self.fake.attempt_tags.items() if name == '8-1')
                self.assertEqual(self.fake.states[own], 'running')
        self.fake.before_result = result
        command = self.run_trial(sizes=(8, 16), runs=2, vcpus=24)
        verdicts = {(row.size, row.attempt): row.verdict for row in command.attempts}
        self.assertEqual(verdicts[(8, 1)], 'pass')
        self.assertEqual(verdicts[(16, 1)], 'error')
        self.assertEqual(verdicts[(8, 2)], 'not run')
        self.assertEqual(verdicts[(16, 2)], 'not run')
        self.assertEqual(self.fake.launches, 2)
        self.assertTrue(any('Name=tag:GateTrialAttempt,Values=16-1' in args
                            for _, op, args in self.fake.calls if op == 'describe-instances'))
        self.assert_cleanup()

    def test_family_and_peak_memory_reach_launch_row_and_json(self) -> None:
        command = self.run_trial(family='c7a')
        self.assertEqual(self.fake.requests[0]['InstanceType'], 'c7a.4xlarge')
        row = command.attempts[0]
        self.assertEqual(row.memory_peak_bytes, 123456789)
        self.assertIn('123456789', command.row(row))
        record = ledger.mapping(cast(object, json.loads(
            (command.directory / f'gate-trial-{command.run_id}.json').read_text())))
        self.assertEqual(record['family'], 'c7a')
        self.assertEqual(ledger.rows(record, 'attempts')[0]['memory_peak_bytes'], 123456789)

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
        saved = command.directory / f'gate-trial-{command.run_id}.json'
        saved_report = ledger.mapping(cast(object, json.loads(saved.read_text())))
        saved_attempt = ledger.mapping(cast(list[object], saved_report['attempts'])[0])
        self.assertEqual(ledger.mapping(saved_attempt['result'])['modules'],
                         [{'module': 'fixture/test_failure.py', 'status': 1, 'seconds': 10}])

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
        for package in ('git', 'taplo', 'zsh', 'bash', 'gh', 'python3', 'sqlite', 'chromium'):
            self.assertIn(f'github:NixOS/nixpkgs/{"f" * 40}#{package}', data)
        self.assertNotIn('#basedpyright', data)
        self.assertNotIn('#google-chrome', data)
        self.assertNotIn('#nodejs', data)
        self.assertNotIn('uv tool install', data)
        self.assertNotIn('nodejs_wheel', data)
        self.assertNotIn('NIXPKGS_ALLOW_UNFREE', data)
        self.assertNotIn('--impure', data)
        self.assertIn('--command bash -ec', data)
        for shell in ('bash', 'zsh'):
            link = f'ln -sf "$(command -v {shell})" /bin/{shell}'
            self.assertIn(link, data)
            self.assertLess(data.index(link), data.index('exec python3 "$box_dir/guest.py"'))
        self.assertIn('. /etc/profile', data)
        self.assertNotIn('systemctl', data)
        self.assertNotIn('exec nix', data)
        self.assertNotIn('gate-trial.service', data)
        self.assertIn('export PATH=$PATH:/run/current-system/sw/bin', data)
        self.assertEqual(self.fake.bundles[-1].decode(), trial.GUEST)
        self.assertIn('guest.py?X-Amz-', data)
        self.assertIn('-o "$box_dir/guest.py"', data)
        self.assertNotIn('base64 -d', data)
        self.assertNotIn(trial.GUEST, data)
        self.assertIn('machine=merge_gate.WholeMachine(timings)', trial.GUEST)
        self.assertLess(data.index('trap boot_failed EXIT'), data.index('push_logs &'))
        self.assertNotIn('admission_service.py', data)
        self.assertNotIn('governor_gib', data)
        self.assertNotIn('RERUN_LIMIT', data)
        _ = compile(trial.GUEST, '<guest>', 'exec')

    def test_host_lock_pins_every_tool_to_the_host_revision(self) -> None:
        _ = self.run_trial()
        self.assertIn('Guest nixpkgs revision: ' + 'f' * 40, self.output)
        command = shlex.split(self.fake.payloads[0].split("nix --extra-experimental-features", 1)[1])
        tools = [word for word in command if word.startswith('github:NixOS/nixpkgs/')]
        self.assertGreater(len(tools), 20)
        self.assertTrue(all(word.startswith('github:NixOS/nixpkgs/' + 'f' * 40 + '#') for word in tools))
        self.assertNotIn('nixos-26.05#', self.fake.payloads[0])

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
        self.assertEqual(zones, ['us-east-1a', 'us-east-1a', 'us-east-1b'])
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
                    + "args=sys.argv[1:]\nsource=args[args.index('--data-binary')+1]\n"
                    + "body=sys.stdin.read() if source=='@-' else pathlib.Path(source[1:]).read_text()\n"
                    + "with pathlib.Path(__file__).with_name('uploads').open('a') as out: out.write(json.dumps([args[-1],body])+'\\n')\n")
                curl.chmod(0o700)
                # Execute the rendered trap with temporary paths and a fake curl.
                script = 'set -e\n' + trial.boot_trap(links).replace('/var/lib/gate-trial', str(root)) + 'false\n'
                result = subprocess.run(['sh', '-c', script], env={**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH']},
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 1, result.stderr)
                uploads = [cast(list[str], json.loads(line)) for line in (root / 'uploads').read_text().splitlines()]
                self.assertEqual(uploads[0][0], links['log'])
                self.assertIn('fixture nix failure', uploads[0][1])
                self.assertIn('\nUTC: ', uploads[0][1])
                key = 'result' if step == 'size' else 'ready-warm' if warm else 'ready-cold'
                self.assertEqual(uploads[1][0], links[key])
                payload = ledger.mapping(cast(object, json.loads(uploads[1][1])))
                self.assertEqual(payload['verdict'], 'error')
                self.assertIn('exit 1', str(payload['error']))

    def test_log_loop_uploads_available_files_and_survives_failed_curl(self) -> None:
        root = self.root
        _ = (root / 'boot.log').write_text('fixture boot\n')
        sleep = root / 'sleep'
        _ = sleep.write_text('#!' + sys.executable + '\nimport pathlib, sys, time\n'
            + "root=pathlib.Path(__file__).parent\ncount=root/'sleeps'\n"
            + "number=int(count.read_text())+1 if count.exists() else 1\ncount.write_text(str(number))\n"
            + "samples=['cpu 150 0 0 230 70 0 0 100 1000 0', 'cpu 230 0 0 240 80 0 0 200 2000 0']\n"
            + "if number<=2: (root/'stat').write_text(samples[number-1]+'\\n')\n"
            + "assert sys.argv[1]=='0.001'\ntime.sleep(float(sys.argv[1]))\nsys.exit(number>2)\n")
        sleep.chmod(0o700)
        _ = (root / 'loadavg').write_text('1.00 2.00 3.00 1/10 42\n')
        _ = (root / 'meminfo').write_text('MemAvailable: 123456 kB\n')
        _ = (root / 'stat').write_text('cpu 100 0 0 200 50 0 0 0 0 0\n')
        ps = root / 'ps'
        _ = ps.write_text('#!' + sys.executable + '\nimport pathlib, sys\n'
            + "root=pathlib.Path(__file__).parent\nnumber=(root/'sleeps').read_text()\n"
            + "if sys.argv[1:] == ['-e', '--no-headers']: print('process1\\nprocess2')\n"
            + "else:\n"
            + "    assert sys.argv[1:]==['-eo','etimes,pcpu,rss,args','--sort=-etimes']\n"
            + "    print('HIDDEN'+'x'*210+'curl signed-link')\n"
            + "    for n in range(65): print('SNAP'+number+' '+str(n)+' '+'x'*220)\n")
        ps.chmod(0o700)
        curl = root / 'curl'
        _ = curl.write_text('#!' + sys.executable + '\nimport json, pathlib, sys\n'
            + "root=pathlib.Path(__file__).parent\nargs=sys.argv[1:]\n"
            + "assert args[args.index('--data-binary')+1]=='@-'\n"
            + "body=sys.stdin.read()\nfirst=not (root/'uploads').exists()\n"
            + "with (root/'uploads').open('a') as out: out.write(json.dumps([args[-1],body])+'\\n')\n"
            + "(root/'gate.log').write_text('fixture gate\\n')\nsys.exit(22 if first else 0)\n")
        curl.chmod(0o700)
        links = {'log': 'https://fixture.invalid/log'}
        script = 'set -eu\n' + trial.log_push_loop(links, 0.001).replace('/var/lib/gate-trial', str(root))
        script = script.replace('/proc/loadavg', str(root / 'loadavg')).replace('/proc/meminfo', str(root / 'meminfo'))
        script = script.replace('/proc/stat', str(root / 'stat'))
        result = subprocess.run(['sh', '-c', script + 'wait "$log_push_pid"\n'], capture_output=True,
                                text=True, timeout=10, env={**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH']})
        self.assertEqual(result.returncode, 0, result.stderr)
        uploads = [cast(list[str], json.loads(line)) for line in (root / 'uploads').read_text().splitlines()]
        self.assertEqual(len(uploads), 2)
        self.assertEqual([row[0] for row in uploads], [links['log']] * 2)
        self.assertNotIn('fixture gate', uploads[0][1])
        self.assertIn('fixture gate', uploads[1][1])
        timeline = (root / 'timeline.log').read_text().splitlines()
        self.assertEqual(len(timeline), 2)
        for line, busy in zip(timeline, ('25.00', '40.00'), strict=True):
            self.assertRegex(line, r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ loadavg=1.00 2.00 3.00 1/10 42 MemAvailable: 123456 kB processes=\s*2 cpu_busy_percent=')
            self.assertTrue(line.endswith('cpu_busy_percent=' + busy), line)
        for number, (_, content) in enumerate(uploads, 1):
            self.assertIn('fixture boot\n', content)
            self.assertIn(timeline[number - 1], content)
            snapshots = [line for line in content.splitlines() if line.startswith('SNAP')]
            self.assertEqual(len(snapshots), 60)
            self.assertTrue(all(len(line) == 200 and line.startswith(f'SNAP{number} ') for line in snapshots))
            self.assertNotIn('HIDDEN', content)
            self.assertNotIn('curl', content)
            self.assertRegex(content, r'\nUTC: \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\n$')

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

    def test_host_sends_module_times_outside_user_data(self) -> None:
        directory = self.root / 'measures'
        directory.mkdir()
        timings = {'fixture/test_slow.py': 200.0, 'fixture/test_fast.py': 1.0}
        path = directory / 'module_seconds.json'
        content = json.dumps(timings)
        _ = path.write_text(content)
        command = self.run_trial()
        self.assertEqual(command.module_seconds, timings)
        self.assertEqual(cast(object, json.loads(self.fake.bundles[1])), timings)
        self.assertIn('module_seconds.json?X-Amz-', self.fake.payloads[0])
        self.assertNotIn('fixture/test_slow.py', self.fake.payloads[0])
        self.assertEqual(path.read_text(), content)

    def test_missing_or_unreadable_host_times_keep_the_present_order(self) -> None:
        directory = self.root / 'measures'
        directory.mkdir()
        path = directory / 'module_seconds.json'
        for case in ('missing', 'invalid', 'unreadable'):
            with self.subTest(case=case):
                if case == 'invalid':
                    _ = path.write_text('{')
                elif case == 'unreadable':
                    path.unlink()
                    path.mkdir()
                command = self.run_trial()
                self.assertEqual(command.module_seconds, {})
                self.assertNotIn('module_seconds.json?X-Amz-', self.fake.payloads[-1])
                self.assertEqual(command.attempts[0].verdict, 'pass')
        self.assertEqual(subprocess.check_output(['git', '-C', str(self.repo), 'show', 'HEAD:source'], text=True, timeout=10), 'committed source')


@final
class GuestTests(unittest.TestCase):
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

    def test_failed_cold_publish_keeps_marker_absent_and_trap_uses_cold_link(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            links = {'step': 'start', 'log': 'https://fixture.invalid/log',
                     'ready-cold': 'https://fixture.invalid/cold', 'ready-warm': 'https://fixture.invalid/warm'}
            _ = (root / 'links.json').write_text(json.dumps(links))
            _ = (root / 'boot.log').write_text('fixture publish failure')
            curl = root / 'curl'
            _ = curl.write_text('#!' + sys.executable + "\nimport json, pathlib, sys\n"
                + "args=sys.argv[1:]\nsource=args[args.index('--data-binary')+1]\n"
                + "body=sys.stdin.read() if source=='@-' else pathlib.Path(source[1:]).read_text()\n"
                + "with pathlib.Path(__file__).with_name('uploads').open('a') as out: out.write(json.dumps([args[-1],body])+'\\n')\n")
            curl.chmod(0o700)
            guest = trial.GUEST.replace("'/proc/meminfo'", repr(str(root / 'meminfo'))).replace("'/var/lib/gate-trial'", repr(str(root)))
            wrapper = root / 'guest-test.py'
            _ = wrapper.write_text('from unittest import mock\nimport subprocess\n'
                + 'def command(args, **kwargs):\n'
                + '    if args[0] == "curl": raise OSError("publish failed")\n'
                + '    return subprocess.CompletedProcess([], 0, "version", "")\n'
                + 'with mock.patch("subprocess.run", side_effect=command):\n'
                + f'    exec({guest!r}, {{}})\n')
            script = ('set -e\n' + trial.boot_trap(links).replace('/var/lib/gate-trial', str(root))
                      + shlex.join([sys.executable, str(wrapper)]) + '\n')
            result = subprocess.run(['sh', '-c', script], capture_output=True, text=True, timeout=10,
                                    env={**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH']})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('publish failed', result.stderr)
            self.assertFalse((root / 'prepared').exists())
            uploads = [cast(list[str], json.loads(line)) for line in (root / 'uploads').read_text().splitlines()]
            self.assertEqual(uploads[0][0], links['log'])
            self.assertIn('fixture publish failure', uploads[0][1])
            self.assertIn('\nUTC: ', uploads[0][1])
            self.assertEqual(uploads[1][0], links['ready-cold'])
            self.assertEqual(ledger.mapping(cast(object, json.loads(uploads[1][1])))['verdict'], 'error')

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
            with (mock.patch.dict(os.environ, {'MERGE_GATE_BASEDPYRIGHT': str(types), 'MERGE_GATE_PYTHON': sys.executable}),
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

    def test_start_guest_repeats_ready_marker_without_a_gate(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            links = {'step': 'start', 'boot': 'cold', 'log': 'https://fixture.invalid/log',
                     'ready-cold': 'https://fixture.invalid/cold', 'ready-warm': 'https://fixture.invalid/warm'}
            _ = (root / 'links.json').write_text(json.dumps(links))
            uploads: dict[str, dict[str, object]] = {}
            def transport(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
                if arguments[0] == 'curl':
                    self.assertEqual(arguments[arguments.index('--max-time') + 1], '30')
                    url = arguments[-1]
                    if url != links['log']:
                        uploads[url] = ledger.mapping(cast(object, json.loads(cast(bytes, options['input']))))
                return subprocess.CompletedProcess(arguments, 0, 'version', '')
            code = trial.GUEST.replace("'/proc/meminfo'", repr(str(root / 'meminfo'))).replace("'/var/lib/gate-trial'", repr(str(root)))
            with (mock.patch('subprocess.run', side_effect=transport),
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
                    self.assertEqual(trial.main(['--step', 'size', '--repo', str(self.root), '--approve-resources', '--vcpus', '64'], self.settings, self.state), 1)

    def test_quota_read_sets_limit_before_resources_and_failure_creates_none(self) -> None:
        for failure in (False, True):
            with self.subTest(failure=failure):
                costs = ledger.CostEstimate(1000, ledger.DailyTotal('fixture', Decimal(0)),
                    ledger.MonthlyTotal('fixture', Decimal(0)), (), 0, 0)
                fake = FakeAws(Clock())
                limits: list[int] = []
                def run(command: trial.GateTrial, repo: Path, step: str, sizes: Sequence[int],
                        runs: int, reserve: float, vcpus: int) -> int:
                    _ = (command, repo, step, sizes, runs, reserve)
                    self.assertEqual([op for _, op, _ in fake.calls], ['get-service-quota'])
                    limits.append(vcpus)
                    return 0
                original = fake.command
                def aws(arguments: Sequence[str], *, capture_output: bool, text: bool,
                        timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
                    if failure and 'get-service-quota' in arguments:
                        return subprocess.CompletedProcess(list(arguments), 1, '', 'AccessDenied: fixture quota')
                    return original(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check, env=env)
                output = StringIO()
                with (mock.patch.object(ledger.CostLedger, 'read_snapshot', return_value=ledger.LedgerEvidence()),
                      mock.patch.object(ledger, 'costs_from_evidence', return_value=costs),
                      mock.patch.object(admin_secrets.AdminSecretSession, 'get', return_value=admin_secrets.AdminCredentials(
                          'fixture-access', 'fixture-secret', 'fixture-client', 'fixture-client-secret')),
                      mock.patch.object(trial, 'execute_command', new=aws),
                      mock.patch.object(trial.GateTrial, 'run', new=run), redirect_stdout(output)):
                    status = trial.main(['--step', 'size', '--repo', str(self.root), '--approve-resources'], self.settings, self.state)
                if failure:
                    self.assertEqual(status, 1)
                    self.assertEqual(limits, [])
                    self.assertIn('AccessDenied: fixture quota', output.getvalue())
                    self.assertIn('Pass --vcpus', output.getvalue())
                else:
                    self.assertEqual(status, 0)
                    box = int(conf.read(self.settings).text('instance_type').split('.')[1].removesuffix('xlarge')) * 4
                    self.assertEqual(limits, [128 - box])
                    self.assertIn(f'Spot quota: 128 vCPUs; trial limit: {128 - box} vCPUs', output.getvalue())

    def test_main_passes_vcpu_limit_and_family_override(self) -> None:
        costs = ledger.CostEstimate(1000, ledger.DailyTotal('fixture', Decimal(0)),
            ledger.MonthlyTotal('fixture', Decimal(0)), (), 0, 0)
        settings = trial.TrialSettings('us-east-1', 'fixture', 'ami-fixture', '26.05', 'subnet-fixture',
            'vpc-fixture', 'us-east-1a', '/dev/xvda', 32, 'm7a', 10, self.settings)
        captured: list[tuple[str, int]] = []
        def run(command: trial.GateTrial, repo: Path, step: str, sizes: Sequence[int],
                runs: int, reserve: float, vcpus: int) -> int:
            self.assertEqual((repo, step, sizes, runs, reserve), (self.root.resolve(), 'size', [16, 32], 1, 4))
            captured.append((command.settings.family, vcpus))
            return 0
        with (mock.patch.object(ledger.CostLedger, 'read_snapshot', return_value=ledger.LedgerEvidence()),
              mock.patch.object(ledger, 'costs_from_evidence', return_value=costs),
              mock.patch.object(trial.TrialSettings, 'read', return_value=settings),
              mock.patch.object(admin_secrets.AdminSecretSession, 'get', return_value=admin_secrets.AdminCredentials(
                  'fixture-access', 'fixture-secret', 'fixture-client', 'fixture-client-secret')),
              mock.patch.object(trial.GateTrial, 'run', new=run),
              mock.patch.object(AwsCli, 'call', side_effect=AssertionError('quota read during override')), redirect_stdout(StringIO())):
            self.assertEqual(trial.main(['--step', 'size', '--repo', str(self.root), '--approve-resources',
                '--vcpus', '24', '--family', 'c7a', '--sizes', '16,16,32', '--runs', '1'], self.settings, self.state), 0)
        self.assertEqual(captured, [('c7a', 24)])

    def test_deploy_only_dispatches_trial(self) -> None:
        with (mock.patch('sys.argv', ['deploy.py', 'gate-trial', '--step', 'size', '--repo', '.', '--approve-resources', '--vcpus', '120']),
              mock.patch.object(trial, 'main', return_value=0) as dispatch):
            self.assertEqual(deploy.main(), 0)
            self.assertEqual(dispatch.call_args.args[0], ['--step', 'size', '--repo', '.', '--approve-resources', '--vcpus', '120'])
