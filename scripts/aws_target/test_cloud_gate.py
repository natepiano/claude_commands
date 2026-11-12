"""Exercise durable cloud gates with private files and a fake AWS transport."""
from __future__ import annotations

from collections.abc import Callable, Generator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import asdict
from decimal import Decimal
from io import StringIO
import base64
import gzip
import json
import os
import re
import signal
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import cast, override
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from scripts.aws_target import cloud_gate, gate_box, gate_trial, ledger
from scripts.aws_target.aws_target import USAGE_ONLY, AwsCli, AwsUnavailable
from scripts.aws_target.config import read_settings
from scripts.aws_target.ledger_test_support import CostFixture, account_reply, instant, numeric_settings, text_setting
from scripts.build_queue import admission_client, admission_protocol as wire, cloud_progress, progress_profile
from scripts.build_queue.admission_test_support import AdmissionFixture, caller_admission
from scripts.buildlog import store
from scripts.build_targets import target_config
from scripts.production import affected_modules
from scripts.production import merge_gate as gate
from scripts.production.gate_modules import GateRefused
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.process_start import Gone, Started, Unreadable, process_start


class FakeGateAws:
    """Implement only the operations that a gate scaler can request."""
    def __init__(self) -> None:
        self.log: str = 'fixture cloud output\n'
        self.commands: list[list[str]] = []
        self.launches: list[dict[str, object]] = []
        self.refusals: list[str] = []
        self.result: dict[str, object] = {}
        self.has_result: bool = True
        self.has_log: bool = True
        self.state: str = 'running'
        self.state_reason: dict[str, str] = {}
        self.has_instance: bool = True
        self.terminated: bool = False
        self.terminated_instances: set[str] = set()
        self.confirm_termination: bool = True
        self.upload_error: str = ''
        self.upload_timeout: bool = False
        self.launch_timeouts: int = 0
        self.launch_errors: list[subprocess.SubprocessError] = []
        self.launch_reply: dict[str, object] = {'Instances': [{'InstanceId': 'i-fixture'}]}
        self.describe_errors: list[AwsUnavailable | subprocess.TimeoutExpired] = []
        self.read_timeouts: int = 0
        self.billing_pages: list[dict[str, object]] = []
        self.spot_history: dict[str, list[dict[str, object]]] = {}

    def __call__(self, arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
        args = list(arguments)
        self.commands.append(args)
        index = args.index('--region') + 2
        service, operation = args[index:index + 2]
        reply: dict[str, object] = {}
        error = ''
        if operation == 'get-cost-and-usage':
            reply = self.billing_pages.pop(0) if self.billing_pages else account_reply(arguments)
        elif operation == 'describe-spot-price-history':
            reply = {'SpotPriceHistory': self.spot_history.get(args[args.index('--region') + 1], [])}
        elif operation == 'export-credentials':
            assert service == 'configure'
            reply = {'AccessKeyId': 'fixture-access', 'SecretAccessKey': 'fixture-secret', 'SessionToken': 'fixture-token'}
        elif operation == 'put-object':
            if self.upload_timeout:
                raise subprocess.TimeoutExpired('aws s3api put-object', 30)
            error = self.upload_error
        elif operation == 'run-instances':
            self.launches.append(ledger.mapping(cast(object, json.loads(args[args.index('--cli-input-json') + 1]))))
            if self.launch_timeouts:
                self.launch_timeouts -= 1
                raise subprocess.TimeoutExpired('aws run-instances', 1)
            if self.launch_errors:
                raise self.launch_errors.pop(0)
            error = self.refusals.pop(0) if self.refusals else ''
            reply = self.launch_reply
        elif operation == 'get-object':
            if self.read_timeouts:
                self.read_timeouts -= 1
                raise subprocess.TimeoutExpired('aws s3api get-object', 30)
            key_at = args.index('--key') + 1
            key, destination = args[key_at], Path(args[key_at + 1])
            if key.endswith('/gate.log') and self.has_log:
                _ = destination.write_text(self.log)
            elif key.endswith('/result.json') and self.has_result:
                cloud_gate.save(destination, self.result)
            else:
                error = 'NoSuchKey'
        elif operation == 'terminate-instances':
            self.terminated = True
            self.terminated_instances.update(args[args.index('--instance-ids') + 1:args.index('--output')])
        elif operation == 'describe-instances':
            if self.describe_errors:
                raise self.describe_errors.pop(0)
            instance_ids = args[args.index('--instance-ids') + 1:args.index('--output')] if '--instance-ids' in args else ['i-fixture']
            instances: list[dict[str, object]] = []
            for instance_id in instance_ids:
                state = 'terminated' if instance_id in self.terminated_instances and self.confirm_termination else self.state
                instance: dict[str, object] = {'InstanceId': instance_id, 'State': {'Name': state},
                                              'LaunchTime': '2026-01-15T00:00:00+00:00'}
                if self.state_reason:
                    instance['StateReason'] = self.state_reason
                instances.append(instance)
            reply = {'Reservations': [{'Instances': instances}] if self.has_instance else []}
        else:
            raise AssertionError((service, operation))
        return subprocess.CompletedProcess(args, int(bool(error)), json.dumps(reply), error)


class CloudGateTests(CostFixture):
    now: float = 0
    book: ledger.CostLedger = ledger.CostLedger(Path())
    fake: FakeGateAws = FakeGateAws()
    folder: Path = Path()
    request: dict[str, object] = {}
    deployment: dict[str, object] = {}

    @override
    def setUp(self) -> None:
        super().setUp()
        repository = Path(__file__).resolve().parents[2]
        # The fixture keeps two sizes, so the shipped type list can change without these cases.
        shipped = (repository / 'config/cloud_gate.conf').read_text()
        _ = (self.root / 'cloud_gate.conf').write_text(
            re.sub(r'(?m)^instance_types=.*$', 'instance_types=m7a.8xlarge m7a.4xlarge', shipped))
        self.now = float(instant('2026-01-15T01:00:00+00:00'))
        self.book = ledger.CostLedger(self.state)
        self.book.record_inventory(ledger.ResourceInventory(int(self.now) - 1,
            (ledger.PricedResource('fixture-fixed', Decimal(0)),), 'fixture'))
        self.book.observe(ledger.InstanceObservation(int(self.now) - 1, 'stopped', 'fixture'))
        self.fake = FakeGateAws()
        self.folder = self.root / 'attempt'
        self.folder.mkdir()
        _ = (self.folder / 'source.bundle').write_bytes(b'private bundle')
        self.request = {'attempt': 'attempt', 'commit': 'commit', 'tree': 'tree', 'mode': 'merge-gate',
            'origin': '/private/origin', 'modules': ['scripts/fixture/test_ok.py'],
            'expires': int(self.now) + 1200, 'module_time_limit_s': 1800, 'source_key': 'source/private.bundle'}
        cloud_gate.save(self.folder / 'request.json', self.request)
        self.deployment = {'deployment_id': 'fixture', 'bucket': 'fixture-bucket', 'revision': 'fixture-revision',
            'regions': [{'region': 'fixture-region', 'template': 'fixture-template', 'version': '1',
                'security_group': 'fixture-group',
                'subnets': [{'id': 'fixture-a', 'zone': 'a'}, {'id': 'fixture-b', 'zone': 'b'}]}]}
        self.fake.result = self.result()

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def result(self, status: int = 0) -> dict[str, object]:
        run = gate.GateRun((gate.GateJobResult('fixture', 'test_ok.py', status, 1, 'OK'),), 1, 2)
        record = gate_box.completion_record(run, 'attempt', 'commit', 'tree', 'fixture-guest', self.now,
                                            self.now + 1, 'merge-gate')
        return {'attempt': 'attempt', 'commit': 'commit', 'tree': 'tree', 'host': 'fixture-guest',
                'verdict': 'pass' if run.passed else 'fail', 'gate': gate_box.encode_run(run), 'record': record, 'gate_s': 1.0}

    def execute(self) -> dict[str, object]:
        worker = cloud_gate.GateWorker(AwsCli('build-scaler', 'fixture-region', 1, execute=self.fake),
            self.book, self.settings, self.folder / 'reservation.cgroup', clock=lambda: self.now, pause=self.advance)
        return worker.execute(self.folder, self.request, self.deployment)

    def steering_fixture(self) -> tuple[cloud_gate.GateWorker, list[cloud_gate.GateLaunchVariant]]:
        types = ('m7a.8xlarge', 'c7a.8xlarge', 'm7a.4xlarge')
        text_setting(self.root / 'cloud_gate.conf', 'instance_types', ' '.join(types))
        numeric_settings(self.root / 'cloud_gate.conf', {'remote_region_transfer_usd': '0',
            'gate_wait_usd_per_minute': '0'})
        path = self.root / 'cloud_gate.conf'
        _ = path.write_text(re.sub(r'(?m)^spot_price_live_read_timeout_s=.*$',
                                  'spot_price_live_read_timeout_s=.25', path.read_text()))
        text_setting(self.root / 'cloud_gate.conf', 'region_extra_gate_seconds', '{}')
        self.deployment['regions'] = [{**ledger.rows(self.deployment, 'regions')[0], 'region': region,
            'subnets': [{'id': region + suffix, 'zone': region + suffix} for suffix in ('a', 'b')]}
            for region in ('us-east-2', 'us-west-2')]
        variants = [cloud_gate.GateLaunchVariant(region, size, region + suffix, region + suffix)
                    for region in ('us-east-2', 'us-west-2') for size in types for suffix in ('a', 'b')]
        worker = cloud_gate.GateWorker(AwsCli('fixture', 'us-east-2', 1, execute=self.fake), self.book,
            self.settings, self.folder / 'reservation', clock=lambda: self.now, pause=self.advance)
        self.assertIsInstance(self.book.reserve_gate(self.settings, ledger.GateReservation(
            'attempt', int(self.now), int(self.now) + 1200, Decimal('.8'), Decimal('.02'))), ledger.GateReserved)
        return worker, variants

    def variant_prices(self, variants: Sequence[cloud_gate.GateLaunchVariant],
                       rates: dict[tuple[str, str, str], str], *, age: int = 0) -> None:
        ledger.write_spot_prices(self.state, [ledger.RegionalSpotPrice(row.region, row.zone, row.instance_type,
            Decimal(rates.get((row.region, row.instance_type, row.zone), '.5')), int(self.now) - age) for row in variants])

    def live_price_fixture(self) -> tuple[cloud_gate.GateWorker, list[cloud_gate.GateLaunchVariant]]:
        worker, variants = self.steering_fixture()
        regions = ('us-east-2', 'us-west-2', 'eu-north-1', 'eu-south-2')
        regional = ledger.rows(self.deployment, 'regions')[0]
        self.deployment['regions'] = [{**regional, 'region': region,
            'subnets': [{'id': region + suffix, 'zone': region + suffix} for suffix in ('a', 'b')]}
            for region in regions]
        types = tuple(dict.fromkeys(row.instance_type for row in variants))
        variants = [cloud_gate.GateLaunchVariant(region, size, region + suffix, region + suffix)
                    for region in regions for size in types for suffix in ('a', 'b')]
        self.variant_prices(variants, {(row.region, row.instance_type, row.zone):
            '.1' if row.region == 'us-east-2' else '.5' for row in variants})
        for region in regions:
            self.fake.spot_history[region] = [dict(InstanceType=row.instance_type, AvailabilityZone=row.zone,
                SpotPrice='.05' if region == 'eu-south-2' else '.6', Timestamp='2026-01-15T00:59:00Z')
                for row in variants if row.region == region]
        return worker, variants

    def test_live_prices_change_launch_order_without_replacing_the_hourly_file(self) -> None:
        worker, _ = self.live_price_fixture()
        hourly = (self.state / 'spot-prices.json').read_bytes()
        self.assertEqual(worker.launch(self.folder, self.request, self.deployment), 'i-fixture')
        saved = cloud_gate.read(self.folder / 'launch.json')
        self.assertEqual(ledger.rows(saved, 'variants')[0]['region'], 'eu-south-2')
        self.assertEqual(saved['region'], 'eu-south-2')
        self.assertEqual((self.state / 'spot-prices.json').read_bytes(), hourly)
        reads = ledger.rows(saved, 'spot_price_reads')
        self.assertEqual([row['region'] for row in reads], ['us-east-2', 'us-west-2', 'eu-north-1', 'eu-south-2'])
        self.assertTrue(all(row['used'] and float(str(row['seconds'])) >= 0 for row in reads))
        launch = self.book.launches()[0]
        self.assertEqual(launch['spot_price_reads'], list(reads))
        calls = [args for args in self.fake.commands if 'describe-spot-price-history' in args]
        self.assertEqual(len(calls), 4)
        for args in calls:
            self.assertEqual(args[args.index('--instance-types') + 1:args.index('--product-descriptions')],
                             ['m7a.8xlarge', 'c7a.8xlarge', 'm7a.4xlarge'])
            self.assertEqual(args[args.index('--product-descriptions') + 1], 'Linux/UNIX')

    def test_live_price_timeout_keeps_hourly_rows_and_bounds_the_whole_read(self) -> None:
        worker, variants = self.live_price_fixture()
        release = threading.Event()
        stopped = threading.Event()
        path = self.root / 'cloud_gate.conf'
        _ = path.write_text(re.sub(r'(?m)^spot_price_live_read_timeout_s=.*$',
                                  'spot_price_live_read_timeout_s=.1', path.read_text()))
        def transport(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
            if 'describe-spot-price-history' in arguments and 'us-east-2' in arguments:
                self.assertLessEqual(float(str(options['timeout'])), .1)
                try:
                    _ = release.wait(2)
                    raise subprocess.TimeoutExpired('fixture price read', .1)
                finally:
                    stopped.set()
            return self.fake(arguments, **options)
        worker.api = AwsCli('fixture', 'us-east-2', 1, execute=transport)
        try:
            begin = time.monotonic()
            reads = worker.live_spot_prices(variants)
            elapsed = time.monotonic() - begin
            self.assertLess(elapsed, .5)
            self.assertGreaterEqual(elapsed, .08)
            east = next(row for row in reads if row.region == 'us-east-2')
            self.assertFalse(east.used)
            self.assertGreaterEqual(east.seconds, .08)
            prices = [price for reading in reads for price in reading.prices]
            ranked = worker.rank_variants(variants, 'merge-gate', prices)
            self.assertEqual(ranked[0].region, 'eu-south-2')
            self.assertEqual(ranked[4].region, 'us-east-2')
        finally:
            release.set()
            self.assertTrue(stopped.wait(2))

    def test_all_live_price_failures_keep_the_hourly_order(self) -> None:
        worker, variants = self.live_price_fixture()
        expected = worker.rank_variants(variants, 'merge-gate')
        calls: list[str] = []
        def transport(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
            if 'describe-spot-price-history' in arguments:
                calls.append(arguments[arguments.index('--region') + 1])
                raise AwsUnavailable('An error occurred (AccessDenied): fixture refusal')
            return self.fake(arguments, **options)
        worker.api = AwsCli('fixture', 'us-east-2', 1, execute=transport)
        self.assertEqual(worker.launch(self.folder, self.request, self.deployment), 'i-fixture')
        saved = cloud_gate.read(self.folder / 'launch.json')
        self.assertEqual(ledger.rows(saved, 'variants'), tuple(asdict(row) for row in expected))
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(not row['used'] for row in ledger.rows(saved, 'spot_price_reads')))

    def test_resumed_launch_keeps_live_price_evidence_and_does_not_read_again(self) -> None:
        worker, _ = self.live_price_fixture()
        save = cloud_gate.save
        def stop_before_request(path: Path, row: dict[str, object]) -> None:
            save(path, row)
            if 'arguments' in row:
                raise OSError('The fixture stopped before the launch request')
        with mock.patch.object(cloud_gate, 'save', side_effect=stop_before_request):
            with self.assertRaisesRegex(OSError, 'before the launch request'):
                _ = worker.launch(self.folder, self.request, self.deployment)
        saved = cloud_gate.read(self.folder / 'launch.json')
        with mock.patch.object(worker, 'live_spot_prices', side_effect=AssertionError('The resume must not read prices')):
            self.assertEqual(worker.launch(self.folder, self.request, self.deployment), 'i-fixture')
        current = cloud_gate.read(self.folder / 'launch.json')
        self.assertEqual(current['variants'], saved['variants'])
        self.assertEqual(current['spot_price_reads'], saved['spot_price_reads'])
        self.assertEqual(self.book.launches()[0]['spot_price_reads'], saved['spot_price_reads'])

    def test_regional_wait_cost_changes_the_order_only_when_enabled(self) -> None:
        worker, variants = self.live_price_fixture()
        self.variant_prices(variants, {(row.region, row.instance_type, row.zone):
            '.49' if row.region == 'eu-south-2' else '.5' for row in variants})
        text_setting(self.root / 'cloud_gate.conf', 'region_extra_gate_seconds', '{"eu-south-2": 60}')
        numeric_settings(self.root / 'cloud_gate.conf', {'gate_wait_usd_per_minute': '.01'})
        self.assertEqual(worker.rank_variants(variants, 'merge-gate')[0].region, 'us-east-2')
        numeric_settings(self.root / 'cloud_gate.conf', {'gate_wait_usd_per_minute': '0'})
        self.assertEqual(worker.rank_variants(variants, 'merge-gate')[0].region, 'eu-south-2')

    def test_live_price_reads_run_concurrently(self) -> None:
        worker, variants = self.live_price_fixture()
        path = self.root / 'cloud_gate.conf'
        _ = path.write_text(re.sub(r'(?m)^spot_price_live_read_timeout_s=.*$',
                                  'spot_price_live_read_timeout_s=1', path.read_text()))
        barrier = threading.Barrier(4, timeout=.5)
        delay = threading.Event()
        def transport(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
            if 'describe-spot-price-history' in arguments:
                _ = barrier.wait()
                _ = delay.wait(.05)
            return self.fake(arguments, **options)
        worker.api = AwsCli('fixture', 'us-east-2', 1, execute=transport)
        begin = time.monotonic()
        reads = worker.live_spot_prices(variants)
        elapsed = time.monotonic() - begin
        self.assertTrue(all(row.used for row in reads))
        self.assertLess(elapsed, sum(row.seconds for row in reads) * .75)
        self.assertLess(elapsed, max(row.seconds for row in reads) + .1)

    def test_variant_prices_prefer_the_cheapest_zone_and_preserve_equal_price_order(self) -> None:
        worker, variants = self.steering_fixture()
        self.variant_prices(variants, {})
        self.assertEqual(worker.rank_variants(variants, 'merge-gate')[:4], variants[:4])
        self.variant_prices(variants, {('us-west-2', 'm7a.8xlarge', 'us-west-2b'): '.1'})
        ranked = worker.rank_variants(variants, 'merge-gate')
        self.assertEqual(ranked[:2], [variants[7], variants[0]])
        self.assertFalse(any('describe-spot-price-history' in command for command in self.fake.commands))

    def test_equal_vcpu_types_compete_on_price_and_smaller_sizes_remain_fallbacks(self) -> None:
        worker, variants = self.steering_fixture()
        self.variant_prices(variants, {('us-east-2', 'c7a.8xlarge', 'us-east-2a'): '.2',
                                      ('us-east-2', 'm7a.4xlarge', 'us-east-2a'): '.001'})
        ranked = worker.rank_variants(variants, 'merge-gate')
        self.assertEqual(ranked[0], variants[2])
        self.assertTrue(all(row.instance_type.endswith('8xlarge') for row in ranked[:8]))
        self.assertTrue(all(row.instance_type.endswith('4xlarge') for row in ranked[8:]))

    def test_absent_stale_or_invalid_prices_preserve_configured_order(self) -> None:
        worker, variants = self.steering_fixture()
        self.assertEqual(worker.rank_variants(variants, 'merge-gate'), variants)
        self.variant_prices(variants, {}, age=7201)
        self.assertEqual(worker.rank_variants(variants, 'merge-gate'), variants)
        _ = (self.state / 'spot-prices.json').write_text('{')
        self.assertEqual(worker.rank_variants(variants, 'merge-gate'), variants)

    def test_missing_prices_follow_priced_variants_of_the_same_size(self) -> None:
        worker, variants = self.steering_fixture()
        self.variant_prices([variants[6]], {})
        ranked = worker.rank_variants(variants, 'merge-gate')
        self.assertEqual(ranked[0], variants[6])
        self.assertEqual(ranked[1:4], variants[:4][:3])
        self.assertEqual(ranked[-4:], [variants[4], variants[5], variants[10], variants[11]])

    def test_zone_capacity_cooldown_ends_after_the_configured_window(self) -> None:
        worker, variants = self.steering_fixture()
        self.variant_prices(variants, {('us-west-2', 'm7a.8xlarge', 'us-west-2b'): '.1'})
        for code in ('InsufficientInstanceCapacity', 'InsufficientSpotCapacity', 'SpotMaxPriceTooLow'):
            with self.subTest(code=code):
                _ = (self.state / 'cloud-gate-launches.jsonl').write_text(json.dumps(dict(
                    time=self.now - 600, result='refused', region='us-west-2', instance_type='m7a.8xlarge',
                    availability_zone='us-west-2b', error_code=code)) + '\n')
                self.assertEqual(worker.rank_variants(variants, 'merge-gate')[-1], variants[7])
        self.advance(1260)
        self.assertEqual(worker.rank_variants(variants, 'merge-gate')[0], variants[7])

    def test_quota_cooldown_moves_every_variant_of_the_region_last(self) -> None:
        worker, variants = self.steering_fixture()
        self.variant_prices(variants, {('us-west-2', 'm7a.8xlarge', 'us-west-2b'): '.1'})
        for code in ('MaxSpotInstanceCountExceeded', 'VcpuLimitExceeded'):
            with self.subTest(code=code):
                _ = (self.state / 'cloud-gate-launches.jsonl').write_text(json.dumps(dict(
                    time=self.now - 600, result='refused', region='us-west-2', instance_type='m7a.8xlarge',
                    availability_zone='us-west-2b', error_code=code)) + '\n')
                ranked = worker.rank_variants(variants, 'merge-gate')
                self.assertEqual([row.region for row in ranked], ['us-east-2'] * 6 + ['us-west-2'] * 6)
                self.assertEqual(len(ranked), len(variants))

    def test_saved_variant_order_survives_capacity_refusal_and_price_changes(self) -> None:
        worker, variants = self.steering_fixture()
        self.variant_prices(variants, {('us-west-2', 'm7a.8xlarge', 'us-west-2b'): '.1'})
        self.fake.refusals = ['InsufficientInstanceCapacity']
        save = cloud_gate.save
        def stop_after_refusal(path: Path, row: dict[str, object]) -> None:
            save(path, row)
            if row.get('variant') == 1 and 'arguments' not in row:
                raise OSError('fixture stop after refusal')
        with mock.patch.object(cloud_gate, 'save', side_effect=stop_after_refusal):
            with self.assertRaisesRegex(OSError, 'fixture stop after refusal'):
                _ = worker.launch(self.folder, self.request, self.deployment)
        saved = cloud_gate.read(self.folder / 'launch.json')
        self.assertEqual(saved['variant'], 1)
        self.assertEqual(len(ledger.rows(saved, 'variants')), len(variants))
        self.variant_prices(variants, {('us-west-2', 'c7a.8xlarge', 'us-west-2a'): '.001'})
        text_setting(self.root / 'cloud_gate.conf', 'instance_types', 'm7a.4xlarge')
        with mock.patch.object(worker, 'rank_variants', side_effect=AssertionError('The order must remain saved')):
            self.assertEqual(worker.launch(self.folder, self.request, self.deployment), 'i-fixture')
        self.assertEqual(self.fake.launches[-1]['ClientToken'], 'attempt-1')
        self.assertEqual(self.fake.launches[-1]['InstanceType'], 'm7a.8xlarge')
        self.assertEqual(self.fake.launches[-1]['NetworkInterfaces'], [{'DeviceIndex': 0, 'SubnetId': 'us-east-2a',
            'Groups': ['fixture-group'], 'AssociatePublicIpAddress': True}])
        self.assertEqual(cloud_gate.read(self.folder / 'launch.json')['variants'], saved['variants'])

    def test_old_variant_offsets_keep_configured_order_despite_new_prices(self) -> None:
        worker, variants = self.steering_fixture()
        self.variant_prices(variants, {('us-west-2', 'm7a.8xlarge', 'us-west-2b'): '.1'})
        cloud_gate.save(self.folder / 'launch.json', {'variant': 1})
        self.assertEqual(worker.launch(self.folder, self.request, self.deployment), 'i-fixture')
        self.assertEqual(self.fake.launches[-1]['ClientToken'], 'attempt-1')
        self.assertEqual(self.fake.launches[-1]['NetworkInterfaces'], [{'DeviceIndex': 0, 'SubnetId': 'us-east-2b',
            'Groups': ['fixture-group'], 'AssociatePublicIpAddress': True}])
        self.assertNotIn('variants', cloud_gate.read(self.folder / 'launch.json'))

    def test_remote_transfer_uses_the_recent_mode_median_or_the_lifetime(self) -> None:
        worker, variants = self.steering_fixture()
        numeric_settings(self.root / 'cloud_gate.conf', {'remote_region_transfer_usd': '.005'})
        self.variant_prices(variants, {('us-west-2', 'm7a.8xlarge', 'us-west-2b'): '.48'})
        self.assertEqual(worker.rank_variants(variants, 'merge-gate')[0], variants[7])
        rows = [dict(time=self.now - 10, result='finished', mode='merge-gate', gate_s=duration) for duration in (300, 600, 900)]
        rows.append(dict(time=self.now - 5, result='finished', mode='full', gate_s=36000))
        _ = (self.state / 'cloud-gate-launches.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
        self.assertEqual(worker.rank_variants(variants, 'merge-gate')[0], variants[0])

    def test_regions_try_home_types_and_zones_before_west_and_keep_home_transfers(self) -> None:
        self.deployment['regions'] = [{**ledger.rows(self.deployment, 'regions')[0], 'region': region, 'image': 'ami-' + suffix,
            'template': 'template-' + suffix} for region, suffix in (('us-east-2', 'home'), ('us-west-2', 'west'))]
        self.fake.refusals = ['InsufficientInstanceCapacity'] * 4
        worker = cloud_gate.GateWorker(AwsCli('fixture', 'us-east-2', 1, execute=self.fake), self.book,
            self.settings, self.folder / 'reservation', clock=lambda: self.now, pause=self.advance)
        reply = worker.execute(self.folder, self.request, self.deployment)
        self.assertEqual(reply['status'], 'complete')
        launches = [command for command in self.fake.commands if 'run-instances' in command]
        self.assertEqual([command[command.index('--region') + 1] for command in launches], ['us-east-2'] * 4 + ['us-west-2'])
        self.assertEqual([row['InstanceType'] for row in self.fake.launches],
                         ['m7a.8xlarge'] * 2 + ['m7a.4xlarge'] * 2 + ['m7a.8xlarge'])
        later = [command for command in self.fake.commands if 'terminate-instances' in command or 'describe-instances' in command]
        self.assertTrue(all(command[command.index('--region') + 1] == 'us-west-2' for command in later))
        transfers = [command for command in self.fake.commands if 's3api' in command]
        self.assertTrue(all(command[command.index('--region') + 1] == 'us-east-2' for command in transfers))
        self.assertEqual(self.book.read_snapshot().gate_reservations[0].region, 'us-west-2')
        self.assertEqual(cloud_gate.read(self.folder / 'launch.json')['region'], 'us-west-2')
        self.assertTrue(all(row['region'] in ('us-east-2', 'us-west-2') for row in self.book.launches()))

    def test_saved_west_launch_replays_its_region_and_token_after_type_change(self) -> None:
        regional = ledger.rows(self.deployment, 'regions')[0]
        self.deployment['regions'] = [{**regional, 'region': region} for region in ('us-east-2', 'us-west-2')]
        self.fake.refusals = ['InsufficientInstanceCapacity'] * 4
        interrupted = False
        def transport(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
            nonlocal interrupted
            reply = self.fake(arguments, **options)
            if 'run-instances' in arguments and arguments[arguments.index('--region') + 1] == 'us-west-2' and not interrupted:
                interrupted = True
                raise subprocess.TimeoutExpired('aws run-instances', 1)
            return reply
        worker = cloud_gate.GateWorker(AwsCli('fixture', 'us-east-2', 1, execute=transport), self.book,
            self.settings, self.folder / 'reservation', clock=lambda: self.now, pause=self.advance)
        self.assertEqual(worker.execute(self.folder, self.request, self.deployment)['status'], 'fallback')
        saved = cloud_gate.read(self.folder / 'launch.json')
        self.assertEqual(saved['region'], 'us-west-2')
        self.assertNotIn('instance', saved)
        path = self.root / 'cloud_gate.conf'
        _ = path.write_text(re.sub(r'(?m)^instance_types=.*$', 'instance_types=m7a.4xlarge', path.read_text()))
        self.assertEqual(worker.execute(self.folder, self.request, self.deployment)['status'], 'complete')
        calls = [args for args in self.fake.commands if 'run-instances' in args]
        self.assertEqual(calls[-1][calls[-1].index('--region') + 1], 'us-west-2')
        self.assertEqual(self.fake.launches[-1], saved['arguments'])
        self.assertEqual(self.fake.launches[-1]['ClientToken'], self.fake.launches[-2]['ClientToken'])
        self.assertEqual(self.fake.launches[-1]['InstanceType'], 'm7a.8xlarge')

    def test_regional_reservations_share_one_daily_cap(self) -> None:
        numeric_settings(self.settings, {'daily_limit_usd': '3.5'})
        first = self.book.reserve_gate(self.settings, ledger.GateReservation(
            'east', int(self.now), int(self.now) + 1200, Decimal('.8'), Decimal('.02'), region='us-east-2'))
        second = self.book.reserve_gate(self.settings, ledger.GateReservation(
            'west', int(self.now), int(self.now) + 1200, Decimal('.8'), Decimal('.02'), region='us-west-2'))
        self.assertIsInstance(first, ledger.GateReserved)
        self.assertIsInstance(second, ledger.GateCapReached)
        self.assertEqual([row.region for row in self.book.read_snapshot().gate_reservations], ['us-east-2'])

    def test_all_regions_refuse_before_local_fallback(self) -> None:
        self.deployment['regions'] = [{**ledger.rows(self.deployment, 'regions')[0], 'region': region} for region in ('us-east-2', 'us-west-2')]
        self.fake.refusals = ['InsufficientInstanceCapacity'] * 8
        reply = self.execute()
        self.assertEqual(reply['status'], 'fallback')
        self.assertEqual(len(self.fake.launches), 8)
        self.assertFalse(self.fake.terminated)

    def test_unsupported_type_skips_to_next_variant(self) -> None:
        self.fake.refusals = ['An error occurred (Unsupported): instance type is not supported'] * 2
        self.assertEqual(self.execute()['status'], 'complete')
        self.assertEqual(self.fake.launches[-1]['InstanceType'], 'm7a.4xlarge')

    def test_expired_pending_sweep_searches_both_regions(self) -> None:
        path = self.root / 'cloud_gate.conf'
        _ = path.write_text(re.sub(r'(?m)^(regions|spot_candidate_regions)=.*$',
                                  r'\1=us-east-2 us-west-2', path.read_text()))
        _ = self.book.reserve_gate(self.settings, ledger.GateReservation(
            'lost', int(self.now), int(self.now) + 1, Decimal('.8'), Decimal('.02')))
        self.advance(2)
        self.fake.has_instance = False
        worker = cloud_gate.GateWorker(AwsCli('fixture', 'us-east-2', 1, execute=self.fake), self.book,
            self.settings, self.folder / 'reservation', clock=lambda: self.now)
        with mock.patch.object(cloud_gate, 'process_start', return_value=Gone()):
            worker.reconcile()
        calls = [command for command in self.fake.commands if 'describe-instances' in command]
        self.assertEqual([command[command.index('--region') + 1] for command in calls], ['us-east-2', 'us-west-2'])
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateAbsent)

    def test_complete_result_keeps_log_and_confirms_termination(self) -> None:
        outcome = self.execute()
        self.assertEqual(outcome['status'], 'complete')
        self.assertEqual((self.folder / 'gate.log').read_text(), 'fixture cloud output\n')
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateTerminated)
        self.assertTrue(self.fake.terminated)

    def test_box_end_uses_result_receipt_only_with_a_valid_final_result(self) -> None:
        for result_state in ('valid', 'absent', 'invalid'):
            with self.subTest(result_state=result_state):
                attempt = 'end-' + result_state
                folder = self.root / attempt
                folder.mkdir()
                request = {**self.request, 'attempt': attempt, 'source_key': ''}
                cloud_gate.save(folder / 'request.json', request)
                self.fake = FakeGateAws()
                self.fake.result = {**self.result(), 'attempt': attempt}
                ledger.mapping(self.fake.result['record'])['id'] = 'gate-' + attempt
                self.fake.has_result = result_state != 'absent'
                if result_state == 'invalid':
                    self.fake.result['tree'] = 'other-tree'
                self.fake.state = 'shutting-down'
                received_at = int(self.now)

                def transport(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
                    if 'terminate-instances' in arguments:
                        self.advance(16)
                    return self.fake(arguments, **options)

                worker = cloud_gate.GateWorker(AwsCli('build-scaler', 'fixture-region', 1, execute=transport),
                    self.book, self.settings, folder / 'reservation', clock=lambda: self.now, pause=self.advance)
                outcome = worker.execute(folder, request, self.deployment)
                self.assertEqual(outcome['status'], 'complete' if result_state == 'valid' else 'fallback')
                reservation = next(row for row in self.book.read_snapshot().gate_reservations if row.attempt == attempt)
                self.assertEqual(reservation.state, ledger.GateTerminated(
                    received_at if result_state == 'valid' else received_at + 16, 'i-fixture'))
                self.assertTrue(self.fake.terminated)

    def test_saved_image_changes_only_the_launch_image(self) -> None:
        self.assertEqual(self.execute()['status'], 'complete')
        official = self.fake.launches[-1]
        ledger.rows(self.deployment, 'regions')[0]['image'] = 'ami-saved-gate'
        self.folder = self.root / 'saved-image-attempt'
        self.folder.mkdir()
        self.request = {**self.request, 'attempt': self.folder.name, 'source_key': ''}
        self.fake.result = {**self.result(), 'attempt': self.folder.name}
        ledger.mapping(self.fake.result['record'])['id'] = 'gate-' + self.folder.name
        self.assertEqual(self.execute()['status'], 'complete')
        saved = self.fake.launches[-1]
        self.assertEqual(saved['ImageId'], 'ami-saved-gate')
        self.assertEqual({key: value for key, value in saved.items() if key not in ('ImageId', 'ClientToken', 'UserData', 'TagSpecifications')},
                         {key: value for key, value in official.items() if key not in ('ImageId', 'ClientToken', 'UserData', 'TagSpecifications')})

    def test_poll_read_timeout_retries_and_completes(self) -> None:
        self.fake.read_timeouts = 1
        self.assertEqual(self.execute()['status'], 'complete')
        reads = [command[command.index('--key') + 1] for command in self.fake.commands if 'get-object' in command]
        self.assertEqual(reads[:2], ['artifacts/attempt/gate.log', 'artifacts/attempt/gate.log'])
        self.assertIn('artifacts/attempt/result.json', reads)
        self.assertEqual(len(self.fake.launches), 1)
        self.assertTrue(self.fake.terminated)

    def test_worker_kills_recover_through_cleanup_or_the_next_start(self) -> None:
        _ = self.book.reserve_gate(self.settings, ledger.GateReservation(
            'other-live', int(self.now), int(self.now) + 1200, Decimal('.8'), Decimal('.02')))
        identity = process_start(os.getpid())
        self.assertIsInstance(identity, Started)
        assert isinstance(identity, Started)
        self.book.gate_owner('other-live', ledger.GateWorkerOwner(os.getpid(), identity.start, str(self.root / 'other-live')))
        self.book.resolve_gate('other-live', ledger.GateRunning('i-other-live'))
        for number, during_launch in ((signal.SIGTERM, -1), (signal.SIGTERM, False), (signal.SIGTERM, True),
                                      (signal.SIGKILL, False), (signal.SIGKILL, True)):
            with self.subTest(number=number, during_launch=during_launch):
                folder = self.root / f'killed-{number}-{during_launch}'
                folder.mkdir()
                attempt = folder.name
                request = {**self.request, 'attempt': attempt}
                cloud_gate.save(folder / 'request.json', request)
                cloud_gate.save(folder / 'deployment.json', self.deployment)
                script = r'''import json, signal, sys
from pathlib import Path
from unittest import mock
from scripts.aws_target import cloud_gate, ledger
from scripts.aws_target.aws_target import AwsCli
from scripts.aws_target.test_cloud_gate import FakeGateAws
folder, state, configuration = map(Path, sys.argv[1:4])
at, number, during_launch = float(sys.argv[4]), int(sys.argv[5]), int(sys.argv[6])
fake = FakeGateAws()
fake.has_result = False
handlers = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
def execute(arguments, **options):
    assert handlers == {number: signal.getsignal(number) for number in handlers}
    reply = fake(arguments, **options)
    raise_now = 'put-object' in arguments if during_launch < 0 else 'run-instances' in arguments if during_launch else 'get-object' in arguments and arguments[arguments.index('--key') + 1].endswith('/result.json')
    if raise_now:
        signal.raise_signal(number)
    return reply
api = AwsCli('build-scaler', 'fixture-region', 1, execute=execute)
worker = cloud_gate.GateWorker(api, ledger.CostLedger(state), configuration, folder / 'reservation.cgroup', clock=lambda: at)
with (mock.patch.object(cloud_gate, 'AwsCli', return_value=api),
      mock.patch.object(cloud_gate, 'GateWorker', return_value=worker),
      mock.patch.object(signal, 'signal', side_effect=AssertionError('handler')),
      mock.patch.object(sys, 'argv', ['cloud-gate', '--worker', str(folder), '--directory', str(state),
                                   '--sidecar', str(folder / 'reservation.cgroup'), '--settings', str(configuration)])):
    raise SystemExit(cloud_gate.main())
'''
                reply = subprocess.run([sys.executable, '-c', script, str(folder), str(self.state), str(self.settings),
                    str(self.now), str(number), str(int(during_launch))], capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(reply.returncode, -number, reply.stderr)
                reservation = next(row for row in self.book.read_snapshot().gate_reservations if row.attempt == attempt)
                self.assertIsInstance(reservation.owner, ledger.GateWorkerOwner)
                self.assertIsInstance(reservation.state, ledger.GateLaunchPending if during_launch else ledger.GateRunning)
                fake = FakeGateAws()
                api = AwsCli('fixture', 'fixture-region', 1, execute=fake)
                worker = cloud_gate.GateWorker(api, self.book, self.settings, folder / 'reservation',
                    clock=lambda: self.now, pause=self.advance)
                command = ['cloud-gate', '--cleanup', str(folder), '--directory', str(self.state),
                           '--sidecar', str(folder / 'reservation'), '--settings', str(self.settings)]
                with (mock.patch.object(cloud_gate, 'AwsCli', return_value=api),
                      mock.patch.object(cloud_gate, 'GateWorker', return_value=worker),
                      mock.patch.object(signal, 'signal', side_effect=AssertionError('handler'))):
                    if number == signal.SIGKILL:
                        outcome = worker.execute(folder, request, self.deployment)
                        self.assertEqual(outcome['status'], 'fallback')
                    else:
                        with mock.patch.object(sys, 'argv', command):
                            self.assertEqual(cloud_gate.main(), 0)
                    before = len(fake.commands)
                    with mock.patch.object(sys, 'argv', command):
                        self.assertEqual(cloud_gate.main(), 0)
                    self.assertEqual(len(fake.commands), before)
                reservation = next(row for row in self.book.read_snapshot().gate_reservations if row.attempt == attempt)
                self.assertIsInstance(reservation.state, ledger.GateAbsent if during_launch == -1 else ledger.GateTerminated)
                self.assertEqual(fake.terminated, during_launch != -1)
                self.assertFalse((folder / 'outcome.json').exists())
                if during_launch == -1:
                    self.assertFalse((folder / 'launch.json').exists())
                    before_restart = len(fake.commands)
                    self.assertEqual(worker.execute(folder, request, self.deployment),
                        {'status': 'fallback', 'reason': 'The saved attempt has ended'})
                    self.assertEqual(len(fake.commands), before_restart)
                other = next(row for row in self.book.read_snapshot().gate_reservations if row.attempt == 'other-live')
                self.assertEqual(other.state, ledger.GateRunning('i-other-live'))
                self.assertNotIn('i-other-live', fake.terminated_instances)

    def test_reconcile_preserves_live_and_unreadable_worker_owners(self) -> None:
        worker = self.worker_with_pending_gate()
        owner = ledger.GateWorkerOwner(os.getpid(), 'fixture-start', str(self.folder))
        self.book.gate_owner('orphan', owner)
        self.advance(1201)
        for identity in (Started(owner.start), Unreadable('fixture outage')):
            with mock.patch.object(cloud_gate, 'process_start', return_value=identity):
                worker.reconcile()
        self.assertEqual(self.fake.commands, [])
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateLaunchPending)

    def test_trial_sweep_preserves_an_expired_trial_with_a_live_worker(self) -> None:
        worker = self.worker_with_pending_gate()
        current = self.root / 'trials' / 'current'
        previous = current.parent / 'previous' / 'orphan'
        current.mkdir(parents=True)
        previous.mkdir(parents=True)
        cloud_gate.save(previous / 'request.json', {'attempt': 'orphan', 'expires': int(self.now) + 1200})
        cloud_gate.save(previous / 'launch.json', {'instance': 'i-live'})
        self.book.gate_owner('orphan', ledger.GateWorkerOwner(os.getpid(), 'fixture-start', str(previous)))
        self.advance(1201)
        with mock.patch.object(cloud_gate, 'process_start', return_value=Started('fixture-start')):
            gate_trial.sweep_trial_resources(current, worker)
        self.assertEqual(self.fake.commands, [])
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateLaunchPending)

    def test_trial_sweep_terminates_only_the_dead_owner_in_an_expired_trial(self) -> None:
        worker = self.worker_with_pending_gate()
        self.assertIsInstance(self.book.reserve_gate(self.settings, ledger.GateReservation(
            'live', int(self.now), int(self.now) + 1200, Decimal('.8'), Decimal(0))), ledger.GateReserved)
        current = self.root / 'trials' / 'current'
        current.mkdir(parents=True)
        for attempt, pid, instance in (('orphan', 9001, 'i-dead'), ('live', 9002, 'i-live')):
            folder = current.parent / 'previous' / attempt
            folder.mkdir(parents=True)
            cloud_gate.save(folder / 'request.json', {'attempt': attempt, 'expires': int(self.now) + 1200})
            cloud_gate.save(folder / 'launch.json', {'instance': instance})
            self.book.gate_owner(attempt, ledger.GateWorkerOwner(pid, 'fixture-start', str(folder)))
            self.book.resolve_gate(attempt, ledger.GateRunning(instance))
        self.advance(1201)
        def owner_state(pid: int) -> Gone | Started:
            return Gone() if pid == 9001 else Started('fixture-start')
        with mock.patch.object(cloud_gate, 'process_start', side_effect=owner_state):
            gate_trial.sweep_trial_resources(current, worker)
        self.assertEqual(self.fake.terminated_instances, {'i-dead'})
        states = {row.attempt: row.state for row in self.book.read_snapshot().gate_reservations}
        self.assertEqual(states['orphan'], ledger.GateTerminated(int(self.now), 'i-dead'))
        self.assertEqual(states['live'], ledger.GateRunning('i-live'))

    def test_cleanup_failure_retains_booking_and_retry_closes_it(self) -> None:
        worker = self.worker_with_pending_gate()
        cloud_gate.save(self.folder / 'request.json', {'attempt': 'orphan', 'expires': int(self.now) + 1200})
        cloud_gate.save(self.folder / 'launch.json', {'instance': 'i-fixture'})
        self.book.resolve_gate('orphan', ledger.GateRunning('i-fixture'))
        self.fake.describe_errors = [AwsUnavailable('fixture outage')]
        self.fake.confirm_termination = False
        self.assertFalse(cloud_gate.cleanup_folder(self.folder, worker))
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateRunning)
        self.fake.confirm_termination = True
        self.assertTrue(cloud_gate.cleanup_folder(self.folder, worker))
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateTerminated)

    def test_each_log_download_publishes_module_progress(self) -> None:
        self.fake.log = ('Runner: 1 modules, whole machine, 32 CPUs\n'
                         + 'fixture/test_ok.py exit=0 seconds=1.00\n')
        with mock.patch.object(progress_profile, 'publish_counter', wraps=progress_profile.publish_counter) as publish:
            self.assertEqual(self.execute()['status'], 'complete')
        sidecar = self.folder / 'reservation.cgroup'
        counter = progress_profile.ProgressCounter('modules', 1, 1)
        self.assertEqual(progress_profile.read_counter(sidecar), counter)
        self.assertEqual(publish.call_count, 2)
        publish.assert_has_calls([mock.call(sidecar, counter), mock.call(sidecar, counter)])

    def test_module_progress_counts_each_module_once(self) -> None:
        text = ('Runner: 2 modules, whole machine, 16 CPUs\n'
                + 'fixture/test_one.py exit=1 seconds=1.00\n'
                + 'fixture/test_two.py exit=-15 seconds=2.00 stopped\n'
                + 'fixture/test_one.py exit=0 seconds=1.00\n')
        self.assertEqual(cloud_gate.log_progress(text), progress_profile.ProgressCounter('modules', 2, 2))
        self.assertIsInstance(cloud_gate.log_progress('Preparing tools\n'), progress_profile.NoCounter)
        self.assertIsInstance(cloud_gate.log_progress('Runner: 0 modules, whole machine, 32 CPUs\n'),
                              progress_profile.NoCounter)

    def test_each_log_download_publishes_cloud_state_and_latest_failures(self) -> None:
        self.fake.log = ('Runner: 2 modules, whole machine, 16 CPUs\n'
                        + 'fixture/test_one.py exit=1 seconds=1.00\n'
                        + 'fixture/test_two.py exit=-15 seconds=2.00 stopped\n'
                        + 'fixture/test_one.py exit=0 seconds=1.00\n'
                        + 'Cloud gate: uploading\n')
        with mock.patch.object(cloud_progress, 'publish', wraps=cloud_progress.publish) as publish:
            self.assertEqual(self.execute()['status'], 'complete')
        expected = cloud_progress.CloudProgress('uploading', 16, 1, self.now,
            stage='uploading', region='fixture-region', zone='a')
        sidecar = self.folder / 'reservation.cgroup'
        self.assertEqual(cloud_progress.read(sidecar), expected)
        self.assertEqual(publish.call_args_list[-2:], [mock.call(sidecar, expected), mock.call(sidecar, expected)])
        states = [cast(cloud_progress.CloudProgress, call.args[1]).state for call in publish.call_args_list]
        self.assertEqual(states, ['requesting', 'requesting', 'requesting', 'uploading', 'uploading'])

    def test_paired_log_uses_the_last_stage_without_changing_merge_failure_counts(self) -> None:
        boot = ('2026-11-12T13:57:00Z stage=boot\n'
                '2026-11-12T13:57:01Z stage=tools ready\n'
                '2026-11-12T13:57:02Z stage=tools checked\n'
                '2026-11-12T13:57:03Z stage=source ready\n')
        cases = (
            (boot, 'setup', 'source ready'),
            (boot + '2026-11-12T13:57:04Z stage=warm-up base\n', 'setup', 'warm-up base'),
            (boot + '2026-11-12T13:57:04Z stage=round 2 of 3 base\n', 'running', 'round 2 of 3 base'),
            (boot + '2026-11-12T13:57:04Z stage=round 2 of 3 base\n2026-11-12T13:57:05Z stage=finish\n',
             'running', 'finish'),
        )
        for log, state, stage in cases:
            with self.subTest(stage=stage):
                result = cloud_gate.log_cloud_progress(log, 32, 100)
                self.assertEqual((result.state, result.stage, result.failed_modules), (state, stage, 0))

    def test_merge_log_clears_stages_after_the_runner_and_keeps_its_counter(self) -> None:
        log = ('2026-11-12T13:57:00Z stage=tests start\n'
               'Runner: 3 modules, whole machine, 16 CPUs\n'
               'fixture/test_one.py exit=1 seconds=1.0\n'
               'fixture/test_two.py exit=0 seconds=1.0\n'
               '2026-11-12T13:57:01Z stage=round 2 of 3 base\n')
        result = cloud_gate.log_cloud_progress(log, 32, 100)
        self.assertEqual(result, cloud_progress.CloudProgress('running', 16, 1, 100))
        self.assertEqual(cloud_gate.log_progress(log), progress_profile.ProgressCounter('modules', 2, 3))
        result = cloud_gate.log_cloud_progress(log + 'Cloud gate: uploading\n', 32, 101)
        self.assertEqual(result, cloud_progress.CloudProgress('uploading', 16, 1, 101, stage='uploading'))

    def test_empty_log_shows_launch_until_the_first_box_stage(self) -> None:
        self.assertEqual(cloud_gate.log_cloud_progress('', 32, 100),
                         cloud_progress.CloudProgress('requesting', 32, 0, 100, stage='launch'))
        self.assertEqual(cloud_gate.log_cloud_progress('2026-11-12T13:57:00Z stage=boot\n', 32, 101),
                         cloud_progress.CloudProgress('setup', 32, 0, 101, stage='boot'))

    def test_launched_gate_publishes_its_region_and_zone_after_launch(self) -> None:
        worker, _ = self.steering_fixture()
        self.fake.refusals = ['InsufficientInstanceCapacity'] * 6
        self.fake.log = '2026-11-12T13:57:00Z stage=round 2 of 3 base\n'
        with mock.patch.object(cloud_progress, 'publish', wraps=cloud_progress.publish) as publish:
            self.assertEqual(worker.execute(self.folder, self.request, self.deployment)['status'], 'complete')
        updates = [cast(cloud_progress.CloudProgress, call.args[1]) for call in publish.call_args_list]
        self.assertTrue(all((row.region, row.zone) == ('', '') for row in updates[:-3]))
        self.assertEqual(updates[-3], cloud_progress.CloudProgress('requesting', 32, 0, self.now,
            stage='launch', region='us-west-2', zone='us-west-2a'))
        self.assertTrue(all((row.region, row.zone, row.stage) == ('us-west-2', 'us-west-2a', 'round 2 of 3 base')
                            for row in updates[-2:]))
        saved = cloud_gate.read(self.folder / 'launch.json')
        self.assertEqual((saved['region'], saved['zone']), ('us-west-2', 'us-west-2a'))

    def test_cloud_state_tracks_setup_running_and_capacity_fallback_size(self) -> None:
        self.assertEqual(cloud_gate.log_cloud_progress('Preparing tools\n', 32, 1),
                         cloud_progress.CloudProgress('setup', 32, 0, 1))
        self.assertEqual(cloud_gate.log_cloud_progress('Runner: 3 modules, whole machine, 16 CPUs\n', 32, 2),
                         cloud_progress.CloudProgress('running', 16, 0, 2))
        self.fake.refusals = ['An error occurred (InsufficientInstanceCapacity) when calling the RunInstances operation: fixture'] * 3
        with mock.patch.object(cloud_progress, 'publish', wraps=cloud_progress.publish) as publish:
            self.assertEqual(self.execute()['status'], 'complete')
        sizes = [cast(cloud_progress.CloudProgress, call.args[1]).size_vcpus for call in publish.call_args_list]
        self.assertEqual(sizes, [0, 32, 32, 16, 16, 16, 16, 16])

    def test_instance_and_volume_tags_use_the_machine_name(self) -> None:
        self.assertEqual(self.execute()['status'], 'complete')
        for resource in ledger.rows(self.fake.launches[0], 'TagSpecifications'):
            tags = {str(row['Key']): row['Value'] for row in ledger.rows(resource, 'Tags')}
            self.assertEqual(tags['Name'], 'cloud-claude-gate')
            self.assertEqual(tags['Workload'], 'cloud-claude-gate')

    def test_first_gate_reconciles_the_period_before_deployment(self) -> None:
        self.assertEqual(self.execute()['status'], 'complete')
        charge_files = list((self.state / 'charges').glob('*.json'))
        self.assertEqual(len(charge_files), 1)
        reading = ledger.read_charge(charge_files[0])
        self.assertLess(reading.period_end, self.book.read_snapshot().inventories[0].at)
        self.assertEqual(reading.estimated_usd, Decimal(0))
        self.assertEqual(reading.actual_usd, Decimal(0))
        commands = [row for row in self.fake.commands if 'get-cost-and-usage' in row]
        self.assertEqual(commands[0][commands[0].index('--filter') + 1], USAGE_ONLY)
        self.assertEqual(commands[0][commands[0].index('--group-by') + 1], 'Type=DIMENSION,Key=USAGE_TYPE')
        self.fake.billing_pages = [account_reply(commands[0], '-0.0000036')]
        worker = cloud_gate.GateWorker(AwsCli('build-scaler', 'fixture-region', 1, execute=self.fake),
                                      self.book, self.settings, self.folder / 'reservation.cgroup',
                                      clock=lambda: self.now + 86400)
        _ = worker.charges()
        self.assertLess(min(row.usd for row in self.book.read_snapshot().reconciliations), Decimal(0))
        self.fake.billing_pages = [{'ResultsByTime': []}]
        worker = cloud_gate.GateWorker(AwsCli('build-scaler', 'fixture-region', 1, execute=self.fake),
                                      self.book, self.settings, self.folder / 'reservation.cgroup',
                                      clock=lambda: self.now + 172800)
        with self.assertRaisesRegex(ValueError, 'daily reply is unavailable'):
            worker.charges()

    def test_remote_failure_remains_red_without_fallback(self) -> None:
        self.fake.result = self.result(1)
        outcome = self.execute()
        self.assertEqual(outcome['status'], 'complete')
        result = ledger.mapping(outcome['result'])
        self.assertFalse(gate_box.decode_run(result['gate']).passed)
        self.assertEqual(self.book.launches()[-1]['result'], 'finished')

    def test_invalid_result_does_not_record_finished_time(self) -> None:
        self.fake.result['tree'] = 'other-tree'
        self.assertEqual(self.execute()['status'], 'fallback')
        self.assertEqual([row['result'] for row in self.book.launches()], ['launched'])

    def test_capacity_tries_every_zone_before_the_smaller_size(self) -> None:
        self.fake.refusals = ['An error occurred (InsufficientInstanceCapacity) when calling the RunInstances operation: fixture'] * 3
        self.assertEqual(self.execute()['status'], 'complete')
        choices = [(row['InstanceType'], ledger.rows(row, 'NetworkInterfaces')[0]['SubnetId'])
                   for row in self.fake.launches]
        self.assertEqual(choices, [('m7a.8xlarge', 'fixture-a'), ('m7a.8xlarge', 'fixture-b'),
                                   ('m7a.4xlarge', 'fixture-a'), ('m7a.4xlarge', 'fixture-b')])
        self.assertEqual(self.book.launches()[-1], {'time': self.now, 'attempt': 'attempt',
            'instance_type': 'm7a.4xlarge', 'vcpus': 16, 'result': 'finished', 'gate_s': 1.0, 'mode': 'merge-gate', 'region': 'fixture-region'})
        spends = self.book.spend_days(read_settings(self.settings), int(self.now))
        self.assertEqual(sum(day.runs for day in spends), 1)
        self.assertEqual(self.book.read_snapshot().gate_reservations[0].instance_type, 'm7a.4xlarge')

    def test_targeted_run_records_mode_in_every_event(self) -> None:
        self.request['mode'] = 'targeted-test'
        cloud_gate.save(self.folder / 'request.json', self.request)
        self.fake.refusals = ['An error occurred (InsufficientInstanceCapacity) when calling the RunInstances operation: fixture']
        self.assertEqual(self.execute()['status'], 'complete')
        rows = self.book.launches()
        self.assertEqual([row['result'] for row in rows], ['refused', 'launched', 'finished'])
        self.assertTrue(all(row['mode'] == 'targeted-test' for row in rows))

    def test_all_capacity_refusals_allow_local_fallback(self) -> None:
        self.fake.refusals = ['An error occurred (InsufficientInstanceCapacity) when calling the RunInstances operation: fixture'] * 4
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(self.execute()['status'], 'fallback')
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateAbsent)
        rows = [ledger.mapping(cast(object, json.loads(line)))
                for line in (self.state / 'cloud-gate-launches.jsonl').read_text().splitlines()]
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row['mode'] == 'merge-gate' for row in rows))
        self.assertTrue(all(row['result'] == 'refused' for row in rows))
        self.assertEqual(output.getvalue().count('Cloud gate refused:'), 4)

    def test_rejected_parameter_falls_back_and_records_a_refusal(self) -> None:
        reason = ('An error occurred (InvalidParameterValue) when calling the RunInstances operation: '
                  + 'Encoded User data is limited to 25600 bytes; account 123456789012 subnet-fixture i-private')
        self.fake.refusals = [reason]
        with redirect_stdout(StringIO()) as output:
            outcome = self.execute()
        self.assertEqual(outcome['status'], 'fallback')
        self.assertIn('InvalidParameterValue', str(outcome['reason']))
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateAbsent)
        rows = [ledger.mapping(cast(object, json.loads(line)))
                for line in (self.state / 'cloud-gate-launches.jsonl').read_text().splitlines()]
        evidence = cloud_gate.read(self.folder / 'launch.json')['spot_price_reads']
        self.assertEqual(rows, [{'time': self.now, 'attempt': 'attempt', 'instance_type': 'm7a.8xlarge',
            'vcpus': 32, 'availability_zone': 'a', 'result': 'refused', 'mode': 'merge-gate', 'error_code': 'InvalidParameterValue',
            'reason': reason, 'region': 'fixture-region', 'spot_price_reads': evidence}])
        self.assertEqual(output.getvalue(), 'Cloud gate refused: m7a.8xlarge in a: InvalidParameterValue\n')
        for private in ('123456789012', 'subnet-fixture', 'i-private', 'RED'):
            self.assertNotIn(private, output.getvalue() + str(outcome))

    def test_launched_box_records_vcpus_and_download_links(self) -> None:
        self.assertEqual(self.execute()['status'], 'complete')
        rows = [ledger.mapping(cast(object, json.loads(line)))
                for line in (self.state / 'cloud-gate-launches.jsonl').read_text().splitlines()]
        evidence = cloud_gate.read(self.folder / 'launch.json')['spot_price_reads']
        self.assertEqual(rows, [{'time': self.now, 'attempt': 'attempt', 'instance_type': 'm7a.8xlarge',
            'vcpus': 32, 'availability_zone': 'a', 'result': 'launched', 'error_code': '', 'reason': '', 'mode': 'merge-gate',
            'region': 'fixture-region', 'spot_price_reads': evidence},
            {'time': self.now, 'attempt': 'attempt', 'instance_type': 'm7a.8xlarge',
             'vcpus': 32, 'result': 'finished', 'gate_s': 1.0, 'mode': 'merge-gate', 'region': 'fixture-region'}])
        data = ledger.text(self.fake.launches[0], 'UserData')
        self.assertTrue(data.startswith('#!/bin/sh\n'))
        links = ledger.mapping(cast(object, json.loads(data.split("<<'GATE_LINKS'\n", 1)[1].split('\nGATE_LINKS', 1)[0])))
        for key in ('guest', 'request', 'source', 'log', 'result', 'boot_error'):
            parsed = urlsplit(ledger.text(links, key))
            query = parse_qs(parsed.query)
            self.assertEqual(parsed.scheme, 'https')
            self.assertEqual(query['X-Amz-Expires'], ['1200'])
            self.assertEqual(query['X-Amz-Security-Token'], ['fixture-token'])
            self.assertTrue(query['X-Amz-Credential'][0].startswith('fixture-access/'))
        self.assertEqual(urlsplit(ledger.text(links, 'log')).path, '/artifacts/attempt/gate.log')
        self.assertNotIn('fixture-secret', data)
        self.assertNotIn('s3://', data)
        self.assertLess(len(base64.b64encode(data.encode())), 25600)
        self.assertNotIn('test_module_0000.py', data)
        self.assertIn('requests/attempt.guest.py', data)
        self.assertIn('requests/attempt.json', data)
        self.assertEqual((self.folder / 'guest.py').read_text(), gate_box.GUEST)

    def test_aws_outage_before_launch_allows_fallback(self) -> None:
        self.fake.upload_error = 'EndpointConnectionError'
        self.assertEqual(self.execute()['status'], 'fallback')
        self.assertEqual(self.fake.launches, [])
        self.assertEqual(self.book.read_snapshot().gate_reservations[0].state, ledger.GateAbsent(int(self.now)))

    def test_saved_variant_without_arguments_records_absence_before_launch(self) -> None:
        self.fake.launch_timeouts = 1
        self.assertEqual(self.execute()['status'], 'fallback')
        cloud_gate.save(self.folder / 'launch.json', {'variant': 1})
        self.fake.launches.clear()
        with mock.patch.object(gate_box, 'presign', side_effect=OSError('Cannot prepare the launch')):
            self.assertEqual(self.execute()['status'], 'fallback')
        self.assertEqual(self.fake.launches, [])
        self.assertEqual(cloud_gate.read(self.folder / 'launch.json'), {'variant': 1})
        self.assertEqual(self.book.read_snapshot().gate_reservations[0].state, ledger.GateAbsent(int(self.now)))

    def test_missing_image_allows_fallback(self) -> None:
        self.fake.refusals = ['An error occurred (InvalidAMIID.NotFound) when calling the RunInstances operation: fixture']
        self.assertEqual(self.execute()['status'], 'fallback')

    def test_access_denial_allows_local_fallback(self) -> None:
        self.fake.upload_error = 'AccessDenied'
        self.assertEqual(self.execute()['status'], 'fallback')
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateAbsent)

    def test_upload_timeout_allows_local_fallback(self) -> None:
        self.fake.upload_timeout = True
        outcome = self.execute()
        self.assertEqual(outcome['status'], 'fallback')
        self.assertIn('timed out after 30 seconds', str(outcome['reason']))
        self.assertEqual(self.fake.launches, [])
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateAbsent)

    def test_lost_launch_reply_reuses_exact_arguments_and_token(self) -> None:
        self.fake.refusals = ['EndpointConnectionError']
        self.assertEqual(self.execute()['status'], 'complete')
        self.assertEqual(len(self.fake.launches), 2)
        self.assertEqual(self.fake.launches[0], self.fake.launches[1])

    def test_uncertain_launch_keeps_reservation_before_local_fallback(self) -> None:
        self.fake.launch_timeouts = 1
        self.assertEqual(self.execute()['status'], 'fallback')
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateLaunchPending)
        self.assertEqual(self.execute()['status'], 'complete')
        self.assertEqual(self.fake.launches[0], self.fake.launches[-1])
        self.assertEqual(len(self.book.read_snapshot().gate_reservations), 1)

    def test_uncertain_launch_subprocess_error_keeps_reservation(self) -> None:
        self.fake.launch_errors = [subprocess.CalledProcessError(1, 'aws run-instances')]
        self.assertEqual(self.execute()['status'], 'fallback')
        self.assertEqual(len(self.fake.launches), 1)
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateLaunchPending)
        self.assertFalse(self.fake.terminated)

    def test_uncertain_launch_cli_error_keeps_reservation(self) -> None:
        reasons = [('Read timeout on endpoint URL: "https://fixture"', 1), ('run-instances: invalid reply', 1)]
        reasons.extend((f'An error occurred ({code}) when calling the RunInstances operation: fixture',
                        cloud_gate.settings().whole('launch_reply_tries'))
                       for code in ('InternalFailure', 'ServerInternal', 'Unavailable'))
        for reason, tries in reasons:
            with self.subTest(reason=reason):
                self.fake.refusals = [reason] * tries
                self.fake.launches.clear()
                self.assertEqual(self.execute(), {'status': 'fallback', 'reason': reason})
                self.assertEqual(len(self.fake.launches), tries)
                self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateLaunchPending)
                self.assertFalse(self.fake.terminated)

    def test_uncertain_launch_reply_keeps_reservation(self) -> None:
        replies: tuple[dict[str, object], ...] = (
            {}, {'Instances': []}, {'Instances': [{'InstanceId': 'i-first'}, {'InstanceId': 'i-second'}]})
        for reply in replies:
            with self.subTest(reply=reply):
                self.fake.launch_reply = reply
                self.assertEqual(self.execute()['status'], 'fallback')
                self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateLaunchPending)
                self.assertFalse(self.fake.terminated)
        self.assertEqual(len(self.fake.launches), len(replies))

    def test_uncertain_launch_log_write_error_keeps_reservation(self) -> None:
        with mock.patch.object(cloud_gate.GateWorker, 'record_launch', side_effect=OSError('Cannot write the launch log')):
            self.assertEqual(self.execute()['status'], 'fallback')
        self.assertEqual(len(self.fake.launches), 1)
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateLaunchPending)
        self.assertFalse(self.fake.terminated)

    def test_uncertain_launch_outage_reconciles_a_running_instance_at_expiry(self) -> None:
        tries = cloud_gate.settings().whole('launch_reply_tries')
        self.fake.refusals = ['EndpointConnectionError'] * tries
        self.assertEqual(self.execute()['status'], 'fallback')
        self.assertEqual(len(self.fake.launches), tries)
        self.assertTrue(all(row == self.fake.launches[0] for row in self.fake.launches))
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateLaunchPending)
        self.advance(1200)
        worker = cloud_gate.GateWorker(AwsCli('build-scaler', 'fixture-region', 1, execute=self.fake),
            self.book, self.settings, self.folder / 'reservation.cgroup', clock=lambda: self.now, pause=self.advance)
        with mock.patch.object(cloud_gate, 'process_start', return_value=Gone()):
            worker.reconcile()
        self.assertEqual(self.book.read_snapshot().gate_reservations[0].state, ledger.GateTerminated(int(self.now), 'i-fixture'))
        queries = [command for command in self.fake.commands if '--filters' in command]
        self.assertTrue(queries)

    def test_uncertain_launch_timeout_reconciles_no_instance_at_expiry(self) -> None:
        self.fake.launch_timeouts = 1
        self.assertEqual(self.execute()['status'], 'fallback')
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateLaunchPending)
        self.fake.has_instance = False
        self.advance(1200)
        worker = cloud_gate.GateWorker(AwsCli('build-scaler', 'fixture-region', 1, execute=self.fake),
            self.book, self.settings, self.folder / 'reservation.cgroup', clock=lambda: self.now, pause=self.advance)
        with mock.patch.object(cloud_gate, 'process_start', return_value=Gone()):
            worker.reconcile()
        self.assertEqual(self.book.read_snapshot().gate_reservations[0].state, ledger.GateAbsent(int(self.now)))
        queries = [command for command in self.fake.commands if '--filters' in command]
        self.assertTrue(queries)

    def test_expired_uncertain_launch_keeps_reservation_until_reconciliation(self) -> None:
        self.fake.launch_timeouts = 1
        self.assertEqual(self.execute()['status'], 'fallback')
        self.fake.has_instance = False
        self.advance(1200)
        outcome = self.execute()
        self.assertEqual(outcome, {'status': 'fallback', 'reason': 'The expired launch remains unconfirmed'})
        self.assertEqual(len(self.fake.launches), 1)
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateLaunchPending)
        for error in (subprocess.TimeoutExpired('aws describe-instances', 1),
                      AwsUnavailable('EndpointConnectionError')):
            with self.subTest(error=type(error).__name__):
                self.fake.describe_errors = [error]
                self.assertEqual(self.execute()['status'], 'fallback')
                self.assertEqual(len(self.fake.launches), 1)
                self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateLaunchPending)

    def test_unauthorized_launch_records_absence_at_once(self) -> None:
        self.fake.refusals = ['An error occurred (UnauthorizedOperation) when calling the RunInstances operation: fixture']
        self.assertEqual(self.execute()['status'], 'fallback')
        self.assertEqual(len(self.fake.launches), 1)
        self.assertEqual(self.book.read_snapshot().gate_reservations[0].state, ledger.GateAbsent(int(self.now)))

    def test_invalid_result_identity_verdict_or_modules_is_rejected(self) -> None:
        for field, value in (('attempt', 'other'), ('tree', 'other'), ('commit', 'other'),
                             ('host', ''), ('verdict', 'fail')):
            with self.subTest(field=field):
                row = {**self.result(), field: value}
                with self.assertRaises(ValueError):
                    cloud_gate.validate_result(row, self.request)
        run = gate.GateRun((gate.GateJobResult('fixture', 'test_other.py', 0, 1, ''),), 1, 1)
        with self.assertRaises(ValueError):
            cloud_gate.validate_result({**self.result(), 'gate': gate_box.encode_run(run)}, self.request)

    def test_missing_final_log_allows_local_fallback(self) -> None:
        self.fake.has_log = False
        self.assertEqual(self.execute()['status'], 'fallback')
        self.assertTrue(self.fake.terminated)

    def test_dead_instance_without_result_allows_local_fallback(self) -> None:
        self.fake.has_result = False
        self.fake.state = 'terminated'
        self.assertEqual(self.execute(), {'status': 'fallback', 'reason': 'The instance stopped without a final result'})
        self.assertTrue(self.fake.terminated)

    def test_stopped_instance_fallback_includes_aws_state_reason(self) -> None:
        self.fake.has_result = False
        self.fake.state = 'shutting-down'
        code = 'Server.SpotInstanceTermination'
        message = 'Server.SpotInstanceTermination: Spot instance termination'
        self.fake.state_reason = {'Code': code, 'Message': message}
        self.assertEqual(self.execute(), {
            'status': 'fallback', 'reason': f'The instance stopped without a final result ({code}: {message})'})
        descriptions = [command for command in self.fake.commands if 'describe-instances' in command]
        self.assertEqual(len(descriptions), 3)
        self.assertTrue(self.fake.terminated)

    def test_expired_instance_without_result_allows_local_fallback(self) -> None:
        self.fake.has_result = False
        self.request['expires'] = int(self.now) + 1
        self.assertEqual(self.execute()['status'], 'fallback')
        self.assertTrue(self.fake.terminated)

    def test_unconfirmed_termination_keeps_charging(self) -> None:
        self.fake.confirm_termination = False
        self.assertEqual(self.execute()['status'], 'fallback')
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateRunning)
        before = self.book.estimate(read_settings(self.settings), int(self.now)).monthly.usd
        self.advance(60)
        self.assertGreater(self.book.estimate(read_settings(self.settings), int(self.now)).monthly.usd, before)

    def worker_with_pending_gate(self) -> cloud_gate.GateWorker:
        reservation = ledger.GateReservation('orphan', int(self.now), int(self.now) + 1200, Decimal('.8'), Decimal(0))
        self.assertIsInstance(self.book.reserve_gate(self.settings, reservation), ledger.GateReserved)
        return cloud_gate.GateWorker(AwsCli('build-scaler', 'fixture-region', 1, execute=self.fake),
            self.book, self.settings, self.folder / 'reservation.cgroup', clock=lambda: self.now, pause=self.advance)

    def test_expired_pending_gate_without_instance_stops_charging(self) -> None:
        path = self.root / 'cloud_gate.conf'
        _ = path.write_text(re.sub(r'(?m)^(regions|spot_candidate_regions)=.*$',
                                  r'\1=us-east-2 us-west-2', path.read_text()))
        worker = self.worker_with_pending_gate()
        self.fake.has_instance = False
        self.advance(8 * 3600)
        self.assertGreater(self.book.estimate(read_settings(self.settings), int(self.now)).monthly.usd, Decimal(0))
        with mock.patch.object(cloud_gate, 'process_start', return_value=Gone()):
            worker.reconcile()
        self.assertEqual(self.book.read_snapshot().gate_reservations[0].state, ledger.GateAbsent(int(self.now)))
        self.assertEqual(self.book.estimate(read_settings(self.settings), int(self.now)).monthly.usd, Decimal(0))
        self.advance(3600)
        self.assertEqual(self.book.estimate(read_settings(self.settings), int(self.now)).monthly.usd, Decimal(0))
        self.assertEqual(len(self.fake.commands), 2)
        for query in self.fake.commands:
            self.assertEqual(query[query.index('--filters') + 1], 'Name=tag:GateAttempt,Values=orphan')

    def test_expired_pending_gate_with_terminated_instance_stops_charging(self) -> None:
        worker = self.worker_with_pending_gate()
        self.fake.state = 'terminated'
        self.advance(1200)
        with mock.patch.object(cloud_gate, 'process_start', return_value=Gone()):
            worker.reconcile()
        self.assertEqual(self.book.read_snapshot().gate_reservations[0].state,
                         ledger.GateTerminated(int(self.now), 'i-fixture'))
        charged = self.book.estimate(read_settings(self.settings), int(self.now)).monthly.usd
        self.advance(3600)
        self.assertEqual(self.book.estimate(read_settings(self.settings), int(self.now)).monthly.usd, charged)

    def test_expired_pending_gate_with_live_instance_is_observed_next_time(self) -> None:
        worker = self.worker_with_pending_gate()
        self.advance(1200)
        with mock.patch.object(cloud_gate, 'process_start', return_value=Gone()):
            worker.reconcile()
        self.assertEqual(self.book.read_snapshot().gate_reservations[0].state, ledger.GateTerminated(int(self.now), 'i-fixture'))
        self.fake.state = 'terminated'
        with mock.patch.object(cloud_gate, 'process_start', return_value=Gone()):
            worker.reconcile()
        self.assertEqual(self.book.read_snapshot().gate_reservations[0].state,
                         ledger.GateTerminated(int(self.now), 'i-fixture'))
        self.assertIn('--instance-ids', self.fake.commands[-1])

    def test_pending_gate_before_expiry_is_unchanged(self) -> None:
        worker = self.worker_with_pending_gate()
        self.advance(1199)
        with mock.patch.object(cloud_gate, 'process_start', return_value=Gone()):
            worker.reconcile()
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateLaunchPending)
        self.assertEqual(self.fake.commands, [])

    def test_combined_rust_cost_refuses_gate_before_launch(self) -> None:
        self.book.observe(ledger.InstanceObservation(int(self.now), 'running', 'fixture'))
        self.advance(18 * 3600)
        self.request['expires'] = int(self.now) + 1200
        self.assertEqual(self.execute(), {'status': 'fallback', 'reason': 'daily_limit'})
        self.assertEqual(self.fake.launches, [])

    def test_parallel_admissions_share_one_cost_reservation_lock(self) -> None:
        self.costs({'daily_limit_usd': '4', 'limit_reserve_usd': '3'})
        def reserve(index: int) -> ledger.GateReserved | ledger.GateCapReached | ledger.GateAttemptEnded:
            return self.book.reserve_gate(self.settings, ledger.GateReservation(
                str(index), int(self.now), int(self.now) + 3600, Decimal('.6'), Decimal(0)))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, range(2)))
        self.assertEqual(sum(isinstance(row, ledger.GateReserved) for row in results), 1)
        self.assertEqual(len(self.book.read_snapshot().gate_reservations), 1)

    def test_future_reservation_checks_the_next_day_and_month(self) -> None:
        self.costs({'daily_limit_usd': '4', 'monthly_limit_usd': '4', 'limit_reserve_usd': '3'})
        boundary = instant('2026-01-31T23:59:59+00:00')
        result = self.book.reserve_gate(self.settings, ledger.GateReservation(
            'boundary', boundary, boundary + 3600, Decimal('1.1'), Decimal(0)))
        self.assertIsInstance(result, ledger.GateCapReached)
        self.assertEqual(self.book.read_snapshot().gate_reservations, [])

    def test_gate_reservation_also_restricts_rust_admission(self) -> None:
        self.costs({'daily_limit_usd': '4', 'limit_reserve_usd': '3'})
        _ = self.book.reserve_gate(self.settings, ledger.GateReservation(
            'busy', int(self.now), int(self.now) + 3600, Decimal('.99'), Decimal(0)))
        self.book.observe(ledger.InstanceObservation(int(self.now), 'running', 'fixture'))
        decision = self.book.guard(self.settings, 'admission', int(self.now) + 60, notify=lambda _event: True)
        self.assertIsInstance(decision, ledger.SpendingRefused)

    def test_import_uses_real_remote_host_and_appends_once(self) -> None:
        buildlog = self.root / 'buildlog'
        destination = buildlog / 'fixture-guest.jsonl'
        destination.parent.mkdir()
        traceback = 'Traceback (most recent call last):\nAssertionError: retained cloud failure\n'
        for retried in (False, True):
            with self.subTest(retried=retried):
                passed = gate.GateJobResult('fixture', 'test_ok.py', 0, 1, 'OK')
                failed = gate.GateJobResult('fixture', 'test_ok.py', 1, 2, traceback)
                run = gate.GateRun((passed,), 3, 2, executions=(failed, passed) if retried else (passed,))
                result = self.result()
                result['gate'] = gate_box.encode_run(run)
                result['record'] = gate_box.completion_record(run, 'attempt', 'commit', 'tree', 'fixture-guest',
                                                             self.now, self.now + 3, 'merge-gate')
                _ = destination.write_text('{"interrupted":')
                with (mock.patch.dict(os.environ, {'BUILDLOG_DIR': str(buildlog)}),
                      mock.patch.object(store, 'host_file', return_value=destination) as host_file):
                    cloud_gate.import_record(self.folder, result)
                    cloud_gate.import_record(self.folder, result)
                self.assertEqual(host_file.call_args.args[0], 'fixture-guest')
                records = [ledger.mapping(cast(object, json.loads(line))) for line in destination.read_text().splitlines()]
                self.assertEqual([row['id'] for row in records], ['gate-attempt'])
                self.assertEqual(records[0]['host'], 'fixture-guest')
                modules = ledger.rows(records[0], 'modules')
                if retried:
                    first, second = modules
                    self.assertNotEqual(first['log'], '')
                    kept = gzip.decompress((buildlog / ledger.text(first, 'log')).read_bytes()).decode('utf-8')
                    self.assertIn(traceback, kept)
                    self.assertEqual((first['attempt'], first['status']), (1, 1))
                    self.assertEqual((second['attempt'], second['status'], second['log']), (2, 0, ''))
                else:
                    self.assertEqual(modules[0]['log'], '')

    def test_remote_red_never_calls_the_local_runner(self) -> None:
        remote = gate.GateRun((gate.GateJobResult('fixture', 'test_ok.py', 1, 1, ''),), 1, 1)
        for mode, full in ((gate.SuiteMode.MERGE_GATE, False), (gate.SuiteMode.TARGETED_TEST, True)):
            with (self.subTest(mode=mode, full=full),
                  mock.patch.object(cloud_gate, 'run', return_value=cloud_gate.CloudCompleted(remote)) as run,
                  mock.patch.object(gate, 'select_affected', side_effect=AssertionError('The full suite needs no selection')),
                  mock.patch.object(gate, 'run_suites') as local):
                self.assertEqual(gate.run_gate_suites(self.root, mode=mode, full=full), remote)
            local.assert_not_called()
            self.assertIsInstance(cast(object, run.call_args.kwargs['selected']), gate.AllModules)

    def test_allowed_fallback_calls_the_original_local_runner(self) -> None:
        with (mock.patch.object(cloud_gate, 'run', return_value=cloud_gate.LocalFallback('AWS is down')),
              mock.patch.object(gate, 'run_suites') as local, redirect_stdout(StringIO())):
            _ = gate.run_gate_suites(self.root, full=True, mode=gate.SuiteMode.TARGETED_TEST)
        self.assertTrue(local.call_args.kwargs['full'])
        self.assertEqual(local.call_args.kwargs['mode'], gate.SuiteMode.TARGETED_TEST)


    def test_paired_result_uses_shared_worker_and_confirms_cleanup(self) -> None:
        request = self.request
        request.update(task='paired', base={'commit': 'base', 'tree': 'base-tree'},
                       changed={'commit': 'commit', 'tree': 'tree'}, module='fixture/test_ok.py', rounds=1)
        sample = {'module': 'fixture/test_ok.py', 'variant': 'base', 'round': 1, 'cpu_s': 1.0,
                  'peak_mib': 1.0, 'wall_s': 1.0, 'status': 0, 'tests': 1, 'failures': 0, 'errors': 0,
                  'instance_type': 'm7a.8xlarge', 'box': ''}
        self.fake.result = {**request, 'runs': [sample, {**sample, 'variant': 'changed'}], 'verdict': 'pass', 'gate_s': 2.0}
        cloud_gate.save(self.folder / 'request.json', request)
        reply = self.execute()
        self.assertEqual(reply['status'], 'complete')
        self.assertTrue(self.fake.terminated)
        result = ledger.mapping(reply['result'])
        self.assertTrue(all(row['box'] == 'i-fixture' for row in ledger.rows(result, 'runs')))
        self.assertNotIn('record', result)

    def test_paired_cancellation_after_recorded_launch_cleans_before_slot_release(self) -> None:
        import multiprocessing
        for confirmed in (True, False):
            with self.subTest(confirmed=confirmed):
                attempt = 'launch-cancel-' + str(confirmed)
                folder = self.root / attempt
                folder.mkdir()
                request = {**self.request, 'attempt': attempt, 'task': 'paired', 'source_key': '',
                           'expires': int(self.now) + 1200}
                cloud_gate.save(folder / 'request.json', request)
                cloud_gate.save(folder / 'deployment.json', self.deployment)
                self.fake = FakeGateAws()
                self.fake.confirm_termination = confirmed
                def transport(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
                    if 'terminate-instances' in arguments:
                        blocked = signal.pthread_sigmask(signal.SIG_BLOCK, set())
                        self.assertTrue(set(gate_trial.TRIAL_SIGNALS) <= blocked)
                    return self.fake(arguments, **options)
                api = AwsCli('fixture', 'us-east-1', 1, execute=transport)
                worker = cloud_gate.GateWorker(api, self.book, self.settings, folder / 'reservation',
                                              clock=lambda: self.now, pause=self.advance)
                resolve = self.book.resolve_gate
                def cancel(attempt: str, state: ledger.GateRunning | ledger.GateAbsent | ledger.GateTerminated, *, instance_type: str = '') -> None:
                    if isinstance(state, ledger.GateRunning):
                        self.assertEqual(cloud_gate.read(folder / 'launch.json')['instance'], 'i-fixture')
                        raise KeyboardInterrupt
                    resolve(attempt, state, instance_type=instance_type)
                @contextmanager
                def slot(_repo: Path, sidecar: Path, _mode: gate.SuiteMode) -> Generator[cloud_gate.CloudReservation]:
                    try:
                        yield cloud_gate.CloudReservation(sidecar)
                    finally:
                        self.assertTrue(self.fake.terminated)
                        _, cleanup = gate_trial.trial_cost(self.book, {attempt}, self.settings, int(self.now))
                        self.assertEqual(cleanup, confirmed)
                parent, child = multiprocessing.get_context('fork').Pipe()
                parent.send('launch')
                previous = {number: signal.getsignal(number) for number in gate_trial.TRIAL_SIGNALS}
                try:
                    with (mock.patch.object(gate_trial, 'AwsCli', return_value=api),
                          mock.patch.object(cloud_gate, 'GateWorker', return_value=worker),
                          mock.patch.object(cloud_gate, 'cloud_reservation', side_effect=slot),
                          mock.patch.object(self.book, 'resolve_gate', side_effect=cancel),
                          mock.patch.object(signal, 'signal', side_effect=AssertionError('handler'))):
                        gate_trial.paired_module(folder, self.state, self.settings, child)
                finally:
                    parent.close()
                    child.close()
                    for number, handler in previous.items():
                        self.assertEqual(signal.getsignal(number), handler)
                self.assertEqual(cloud_gate.read(folder / 'outcome.json')['status'], 'fallback')

    def test_paired_cleanup_resolves_boxes_and_absent_launches_once(self) -> None:
        cohort = self.root / 'cleanup-cohort'
        for attempt, launched in (('first', True), ('second', True), ('absent', False)):
            folder = cohort / attempt
            folder.mkdir(parents=True)
            cloud_gate.save(folder / 'request.json', {**self.request, 'attempt': attempt})
            _ = self.book.reserve_gate(self.settings, ledger.GateReservation(
                attempt, int(self.now), int(self.now) + 1200, Decimal('.8'), Decimal('.02')))
            if launched:
                cloud_gate.save(folder / 'launch.json', {'instance': 'i-' + attempt, 'region': 'us-east-1', 'arguments': {}})
                self.book.resolve_gate(attempt, ledger.GateRunning('i-' + attempt))
        handlers = {number: signal.getsignal(number) for number in gate_trial.TRIAL_SIGNALS}
        api = AwsCli('fixture', 'us-east-1', 1, execute=self.fake)
        with (mock.patch.object(gate_trial, 'AwsCli', return_value=api),
              mock.patch.object(signal, 'signal', side_effect=AssertionError('handler'))):
            self.assertEqual(gate_trial.main(['--paired-cleanup', str(cohort)], self.settings, self.state), 0)
            calls = len(self.fake.commands)
            self.assertEqual(gate_trial.main(['--paired-cleanup', str(cohort)], self.settings, self.state), 0)
            self.assertEqual(len(self.fake.commands), calls)
            empty = self.root / 'empty-trial'
            empty.mkdir()
            with mock.patch.object(cloud_gate.GateWorker, 'charges'):
                self.assertEqual(gate_trial.coordinate_trial(empty, self.state, self.settings), 0)
        self.assertEqual({number: signal.getsignal(number) for number in handlers}, handlers)
        terminated = [command[command.index('--instance-ids') + 1] for command in self.fake.commands
                      if 'terminate-instances' in command]
        self.assertEqual(terminated, ['i-first'])
        self.assertTrue({'i-first', 'i-second'} <= self.fake.terminated_instances)
        states = {row.attempt: row.state for row in self.book.read_snapshot().gate_reservations}
        self.assertIsInstance(states['absent'], ledger.GateAbsent)
        self.assertIsInstance(states['first'], ledger.GateTerminated)
        self.assertIsInstance(states['second'], ledger.GateTerminated)

    def test_cleanup_terminates_five_recorded_boxes_before_four_slow_launch_lookups(self) -> None:
        for lookups_timeout in (True, False):
            with self.subTest(lookups_timeout=lookups_timeout):
                cohort = self.root / ('mixed-' + str(lookups_timeout))
                recorded = {'i-recorded-' + str(number) for number in range(5)}
                for number in range(9):
                    attempt = cohort.name + '-' + str(number)
                    folder = cohort / str(number)
                    folder.mkdir(parents=True)
                    cloud_gate.save(folder / 'request.json', {**self.request, 'attempt': attempt})
                    _ = self.book.reserve_gate(self.settings, ledger.GateReservation(
                        attempt, int(self.now), int(self.now) + 1200, Decimal('.8'), Decimal('.02')))
                    if number < 4:
                        launch: dict[str, object] = {'arguments': {'ClientToken': str(number)}}
                    else:
                        instance = 'i-recorded-' + str(number - 4)
                        launch = {'instance': instance, 'arguments': {}}
                        self.book.resolve_gate(attempt, ledger.GateRunning(instance))
                    cloud_gate.save(folder / 'launch.json', launch)
                self.fake = FakeGateAws()
                started = self.now
                found: set[str] = set()
                lookups: list[str] = []
                def transport(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
                    if 'describe-instances' in arguments and '--filters' in arguments:
                        self.assertTrue(recorded | found <= self.fake.terminated_instances)
                        token = arguments[arguments.index('--filters') + 1]
                        lookups.append(token)
                        timeout = cast(float, options['timeout'])
                        self.assertLessEqual(timeout, cloud_gate.settings().number('termination_timeout_s') - (self.now - started))
                        if lookups_timeout:
                            self.advance(timeout)
                            raise subprocess.TimeoutExpired('fixture lookup', timeout)
                        self.advance(min(5, timeout))
                        instance = 'i-found-' + str(len(lookups))
                        found.add(instance)
                        reply = {'Reservations': [{'Instances': [{'InstanceId': instance}]}]}
                        return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')
                    return self.fake(arguments, **options)
                worker = cloud_gate.GateWorker(AwsCli('fixture', 'us-east-1', 1, execute=transport),
                    self.book, self.settings, cohort / 'reservation', clock=lambda: self.now, pause=self.advance)
                self.assertEqual(gate_trial.cleanup_trial(cohort, worker), not lookups_timeout)
                self.assertEqual(len(lookups), 4)
                self.assertTrue(recorded | found <= self.fake.terminated_instances)
                self.assertLessEqual(self.now - started, cloud_gate.settings().number('termination_timeout_s'))
                if lookups_timeout:
                    self.assertEqual(self.now - started, cloud_gate.settings().number('termination_timeout_s'))
                else:
                    self.assertEqual(len(found), 4)

    def test_nine_box_cleanup_requests_all_terminations_before_one_deadline(self) -> None:
        cohort = self.root / 'nine-boxes'
        instances = {'i-' + str(number) for number in range(9)}
        for number in range(9):
            attempt = str(number)
            folder = cohort / attempt
            folder.mkdir(parents=True)
            cloud_gate.save(folder / 'request.json', {**self.request, 'attempt': attempt})
            cloud_gate.save(folder / 'launch.json', {'instance': 'i-' + attempt, 'arguments': {}})
            _ = self.book.reserve_gate(self.settings, ledger.GateReservation(
                attempt, int(self.now), int(self.now) + 1200, Decimal('.8'), Decimal('.02')))
            self.book.resolve_gate(attempt, ledger.GateRunning('i-' + attempt))
        started = self.now
        timeout = cloud_gate.settings().number('termination_timeout_s')
        self.fake.confirm_termination = False
        def transport(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
            if 'describe-instances' in arguments:
                self.assertTrue(instances <= self.fake.terminated_instances)
                remaining = timeout - (self.now - started)
                self.assertLessEqual(cast(float, options['timeout']), remaining)
                self.advance(min(7, cast(float, options['timeout'])))
            return self.fake(arguments, **options)
        worker = cloud_gate.GateWorker(AwsCli('fixture', 'us-east-1', 1, execute=transport),
            self.book, self.settings, cohort / 'reservation', clock=lambda: self.now, pause=self.advance)
        self.assertFalse(gate_trial.cleanup_trial(cohort, worker))
        self.assertEqual(self.now - started, timeout)
        self.assertTrue(all(isinstance(row.state, ledger.GateRunning) for row in self.book.read_snapshot().gate_reservations))
        command = cloud_gate.service_command('scripts.aws_target.gate_trial',
            ['--paired-worker', str(cohort)], 'fixture', paired=True)
        stop = next(value for value in command if value.startswith('--property=TimeoutStopSec='))
        self.assertEqual(float(stop.rsplit('=', 1)[1]), timeout + 2 * cloud_gate.settings().number('api_timeout_s'))
        self.assertGreater(float(stop.rsplit('=', 1)[1]), timeout)

    def test_paired_cleanup_failure_keeps_the_reservation(self) -> None:
        cloud_gate.save(self.folder / 'request.json', self.request)
        cloud_gate.save(self.folder / 'launch.json', {'instance': 'i-fixture', 'arguments': {}})
        _ = self.book.reserve_gate(self.settings, ledger.GateReservation(
            'attempt', int(self.now), int(self.now) + 1200, Decimal('.8'), Decimal('.02')))
        self.book.resolve_gate('attempt', ledger.GateRunning('i-fixture'))
        worker = cloud_gate.GateWorker(AwsCli('fixture', 'us-east-1', 1, execute=self.fake),
            self.book, self.settings, self.folder / 'reservation', clock=lambda: self.now)
        with mock.patch.object(worker, 'confirm_terminations', return_value=False):
            self.assertFalse(gate_trial.cleanup_trial(self.folder, worker))
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateRunning)

    def test_trial_start_sweeps_only_expired_unresolved_trials(self) -> None:
        trials = self.root / 'trials'
        current = trials / 'current'
        current.mkdir(parents=True)
        for attempt, expiry in (('expired', int(self.now) + 1), ('live', int(self.now) + 1200)):
            folder = trials / attempt / attempt
            folder.mkdir(parents=True)
            cloud_gate.save(folder / 'request.json', {**self.request, 'attempt': attempt, 'expires': expiry})
            cloud_gate.save(folder / 'launch.json', {'instance': 'i-' + attempt, 'arguments': {}})
            _ = self.book.reserve_gate(self.settings, ledger.GateReservation(
                attempt, int(self.now), expiry, Decimal('.8'), Decimal('.02')))
            self.book.resolve_gate(attempt, ledger.GateRunning('i-' + attempt))
        self.advance(2)
        worker = cloud_gate.GateWorker(AwsCli('fixture', 'us-east-1', 1, execute=self.fake),
            self.book, self.settings, current / 'reservation', clock=lambda: self.now)
        with (mock.patch.object(gate_trial, 'AwsCli', return_value=worker.api),
              mock.patch.object(time, 'time', return_value=self.now),
              mock.patch.object(cloud_gate.GateWorker, 'charges')):
            self.assertEqual(gate_trial.coordinate_trial(current, self.state, self.settings), 0)
        states = {row.attempt: row.state for row in self.book.read_snapshot().gate_reservations}
        self.assertIsInstance(states['expired'], ledger.GateTerminated)
        self.assertIsInstance(states['live'], ledger.GateRunning)
        self.assertTrue((trials / 'expired' / 'expired' / 'launch.json').exists())

    def test_paired_cost_records_each_refresh_when_saved_query_fee_is_unchanged(self) -> None:
        numeric_settings(self.settings, {'cost_explorer_read_usd': '.01', 'cost_explorer_interval_seconds': '3600'})
        api = AwsCli('fixture', 'us-east-1', 1, execute=self.fake)
        worker = cloud_gate.GateWorker(api, self.book, self.settings, self.folder / 'reservation', clock=lambda: self.now)
        worker.charges()
        initial = sum((row.query_usd for row in self.book.read_bills()), Decimal(0))
        self.assertEqual(initial, Decimal('.01'))
        cohort = self.root / 'query-cohort'
        cohort.mkdir()
        for advance, expected in ((3601, '.01'), (0, '.01'), (3601, '.02')):
            with self.subTest(advance=advance, expected=expected):
                self.advance(advance)
                output = StringIO()
                with (mock.patch.object(gate_trial, 'AwsCli', return_value=api),
                      mock.patch.object(time, 'time', return_value=self.now), redirect_stdout(output)):
                    self.assertEqual(gate_trial.coordinate_trial(cohort, self.state, self.settings), 0)
                total = ledger.mapping(cast(object, json.loads(output.getvalue())))
                self.assertEqual(Decimal(str(total['total_cost_usd'])), Decimal(expected))
                self.assertEqual(Decimal(str(cloud_gate.read(cohort / 'query-cost.json')['usd'])), Decimal(expected))
                self.assertEqual(sum((row.query_usd for row in self.book.read_bills()), Decimal(0)), initial)

    def test_paired_cohort_daily_and_monthly_limits_rollback_before_launch(self) -> None:
        worker = cloud_gate.GateWorker(AwsCli('fixture', 'us-east-1', 1, execute=self.fake), self.book,
            self.settings, self.folder / 'reservation', clock=lambda: self.now, pause=self.advance)
        for limit in ('daily_limit_usd', 'monthly_limit_usd'):
            with self.subTest(limit=limit):
                numeric_settings(self.settings, {'daily_limit_usd': '20', 'monthly_limit_usd': '400', limit: '3.5'})
                requests = [{**self.request, 'attempt': limit + str(number)} for number in range(2)]
                with mock.patch.object(cloud_gate.GateWorker, 'charges'), self.assertRaises(GateRefused):
                    gate_trial.reserve_trial(worker, requests)
                self.assertEqual(self.fake.launches, [])
                self.assertTrue(all(isinstance(row.state, ledger.GateAbsent) for row in self.book.read_snapshot().gate_reservations))

    def test_paired_cohort_reserves_the_highest_regional_rate(self) -> None:
        reading = ledger.ChargeRead('2026-01-14', int(self.now), int(self.now) - 90000, int(self.now) - 3600,
            Decimal('.6'), Decimal(0), Decimal(0), (
                ledger.UsageTypeCost('USE2-SpotUsage:m7a.8xlarge', Decimal('.1'), Decimal(1), 'Hrs'),
                ledger.UsageTypeCost('USW2-SpotUsage:m7a.8xlarge', Decimal('.5'), Decimal(1), 'Hrs')))
        path = self.root / 'cloud_gate.conf'
        _ = path.write_text(re.sub(r'(?m)^instance_types=.*$', 'instance_types=m7a.8xlarge', path.read_text()))
        _ = path.write_text(re.sub(r'(?m)^(regions|spot_candidate_regions)=.*$',
                                  r'\1=us-east-2 us-west-2', path.read_text()))
        worker = cloud_gate.GateWorker(AwsCli('fixture', 'us-east-2', 1, execute=self.fake), self.book,
            self.settings, self.folder / 'reservation', clock=lambda: self.now)
        with (mock.patch.object(worker, 'reconcile'), mock.patch.object(worker, 'charges'),
              mock.patch.object(self.book, 'read_bills', return_value=(reading,)), redirect_stderr(StringIO()) as output):
            gate_trial.reserve_trial(worker, [self.request])
        reserved = self.book.read_snapshot().gate_reservations[0]
        self.assertEqual(reserved.rate.usd_per_hour, Decimal('.5'))
        self.assertEqual(reserved.region, '')
        estimate = ledger.box_cost(reserved.rate, 1200, read_settings(self.settings).box_minimum_seconds) + Decimal('.02')
        self.assertIn(f'Paired trial estimate: ${estimate};', output.getvalue())

    def test_paired_cohort_reserves_highest_type_rate_and_prices_actual_minimum(self) -> None:
        worker = cloud_gate.GateWorker(AwsCli('fixture', 'us-east-1', 1, execute=self.fake), self.book,
            self.settings, self.folder / 'reservation', clock=lambda: self.now)
        reading = ledger.ChargeRead('2026-01-14', int(self.now), int(self.now) - 90000, int(self.now) - 3600,
            Decimal('2.8'), Decimal(0), Decimal(0), (
                ledger.UsageTypeCost('USE2-SpotUsage:m7a.8xlarge', Decimal('.8'), Decimal(1), 'Hrs'),
                ledger.UsageTypeCost('USE2-SpotUsage:m7a.4xlarge', Decimal('2'), Decimal(1), 'Hrs')))
        with (mock.patch.object(cloud_gate.GateWorker, 'charges'),
              mock.patch.object(self.book, 'read_bills', return_value=(reading,))):
            gate_trial.reserve_trial(worker, [self.request])
            reserved = self.book.read_snapshot().gate_reservations[0]
            self.assertEqual(reserved.usd_per_hour, Decimal('0.8'))
            self.assertEqual(reserved.rate.usd_per_hour, Decimal(2))
            self.assertEqual(reserved.instance_type, 'm7a.4xlarge')
            self.book.resolve_gate('attempt', ledger.GateTerminated(int(self.now) + 1, 'i-fixture'), instance_type='m7a.8xlarge')
            cost, cleanup = gate_trial.trial_cost(self.book, {'attempt'}, self.settings, int(self.now) + 1)
        self.assertTrue(cleanup)
        self.assertEqual(cost, ledger.box_cost(ledger.BoxRate(Decimal('0.8'), 'actual'), 1,
            read_settings(self.settings).box_minimum_seconds) + Decimal('0.02'))

    def test_paired_coordinator_launches_modules_concurrently_and_keeps_json_order(self) -> None:
        import multiprocessing
        from contextlib import contextmanager, redirect_stdout
        barrier = multiprocessing.get_context('fork').Barrier(2)
        cohort = self.root / 'cohort'
        cohort.mkdir()
        for name in ('first', 'second'):
            folder = cohort / name
            folder.mkdir()
            request = {**self.request, 'attempt': name, 'task': 'paired', 'rounds': 1, 'module': name + '/test_ok.py',
                       'base': {'commit': 'base', 'tree': 'base-tree'}, 'changed': {'commit': 'commit', 'tree': 'tree'}}
            cloud_gate.save(folder / 'request.json', request)
            cloud_gate.save(folder / 'deployment.json', self.deployment)
        @contextmanager
        def slot(_repo: Path, sidecar: Path, _mode: gate.SuiteMode) -> Generator[cloud_gate.CloudReservation]:
            yield cloud_gate.CloudReservation(sidecar)
        def execute(worker: cloud_gate.GateWorker, folder: Path, request: dict[str, object],
                    _deployment: dict[str, object]) -> dict[str, object]:
            attempt = ledger.text(request, 'attempt')
            cloud_gate.save(folder / 'launch.json', {'instance': 'i-' + attempt, 'arguments': {'InstanceType': 'm7a.8xlarge'}})
            worker.book.resolve_gate(attempt, ledger.GateRunning('i-' + attempt), instance_type='m7a.8xlarge')
            _ = barrier.wait(timeout=10)
            samples = [dict(module=request['module'], variant=variant, round=1, cpu_s=1.0, peak_mib=2.0,
                wall_s=1.0, status=0, tests=1, failures=0, errors=0, instance_type='m7a.8xlarge', box='i-' + attempt)
                for variant in ('base', 'changed')]
            row = {**request, 'runs': samples, 'verdict': 'pass', 'gate_s': 2.0}
            cloud_gate.save(folder / 'remote-result.json', row)
            worker.book.resolve_gate(attempt, ledger.GateTerminated(int(self.now) + 60, 'i-' + attempt))
            return {'status': 'complete', 'result': row}
        output = StringIO()
        with (mock.patch.object(cloud_gate, 'cloud_reservation', side_effect=slot),
              mock.patch.object(cloud_gate.GateWorker, 'execute', new=execute),
              mock.patch.object(cloud_gate.GateWorker, 'charges'), mock.patch.object(cloud_gate.GateWorker, 'reconcile'),
              mock.patch.object(time, 'time', return_value=self.now), redirect_stdout(output)):
            self.assertEqual(gate_trial.coordinate_trial(cohort, self.state, self.settings), 0)
            first = output.getvalue()
            _ = output.seek(0)
            _ = output.truncate(0)
            self.assertEqual(gate_trial.coordinate_trial(cohort, self.state, self.settings), 0)
            self.assertEqual(output.getvalue().splitlines(), first.splitlines()[-3:])
        rows = [ledger.mapping(cast(object, json.loads(line))) for line in first.splitlines()]
        self.assertEqual(len(rows), 7)
        self.assertTrue(all('variant' in row for row in rows[:4]))
        self.assertTrue(all(row['all_passed'] for row in rows[4:6]))
        self.assertEqual(set(rows[-1]), {'total_cost_usd', 'cost_basis', 'cleanup_complete'})
        self.assertTrue(rows[-1]['cleanup_complete'])


    def test_paired_uncertain_cost_keeps_full_reservation_and_absent_cost_is_zero(self) -> None:
        reservation = ledger.GateReservation('uncertain', int(self.now), int(self.now) + 1200,
            Decimal('0.8'), Decimal('0.02'))
        _ = self.book.reserve_gate(self.settings, reservation)
        cost, complete = gate_trial.trial_cost(self.book, {'uncertain'}, self.settings, int(self.now) + 1)
        self.assertFalse(complete)
        self.assertEqual(cost, Decimal('0.8') / 3 + Decimal('0.02'))
        self.book.resolve_gate('uncertain', ledger.GateAbsent(int(self.now) + 2))
        self.assertEqual(gate_trial.trial_cost(self.book, {'uncertain'}, self.settings, int(self.now) + 2), (Decimal(0), True))

    def test_paired_coordinator_interrupt_and_kill_cleanup(self) -> None:
        script = r'''import json, multiprocessing, os, signal, sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from unittest import mock
from scripts.aws_target import cloud_gate, gate_trial, ledger
from scripts.aws_target.aws_target import AwsCli
from scripts.aws_target.test_cloud_gate import FakeGateAws
folder, directory, settings = map(Path, sys.argv[1:4])
at, number = float(sys.argv[4]), int(sys.argv[5])
coordinator = os.getpid()
barrier = multiprocessing.get_context('fork').Barrier(2)
@contextmanager
def slot(repo, sidecar, mode):
    try:
        yield cloud_gate.CloudReservation(sidecar)
    finally:
        (repo / 'released').touch()
def create_api(*args, **kwargs):
    fake = FakeGateAws()
    fake.has_result = False
    def execute(arguments, **options):
        reply = fake(arguments, **options)
        if 'get-object' in arguments and arguments[arguments.index('--key') + 1].endswith('/result.json'):
            turn = barrier.wait(timeout=10)
            if turn == 0:
                os.kill(coordinator, number)
            if number != signal.SIGINT:
                os._exit(0)
            signal.pause()
        return reply
    return AwsCli('fixture', 'us-east-1', 1, execute=execute)
with (mock.patch.object(gate_trial, 'AwsCli', side_effect=create_api),
      mock.patch.object(cloud_gate, 'cloud_reservation', side_effect=slot),
      mock.patch.object(cloud_gate.GateWorker, 'charges'), mock.patch.object(cloud_gate.GateWorker, 'reconcile'),
      mock.patch.object(gate_trial.time, 'time', return_value=at)):
    raise SystemExit(gate_trial.coordinate_trial(folder, directory, settings))
'''
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
            with self.subTest(number=number):
                cohort = self.root / ('cancel-cohort-' + str(number))
                cohort.mkdir()
                for name in (str(number) + '-first', str(number) + '-second'):
                    folder = cohort / name
                    folder.mkdir()
                    request = {**self.request, 'attempt': name, 'task': 'paired', 'rounds': 1, 'module': name + '/test_ok.py',
                               'base': {'commit': 'base', 'tree': 'base-tree'}, 'changed': {'commit': 'commit', 'tree': 'tree'}}
                    cloud_gate.save(folder / 'request.json', request)
                    cloud_gate.save(folder / 'deployment.json', self.deployment)
                reply = subprocess.run([sys.executable, '-c', script, str(cohort), str(self.state), str(self.settings),
                                        str(self.now), str(number)], capture_output=True, text=True, timeout=30)
                self.assertEqual(reply.returncode, 1 if number == signal.SIGINT else -number, reply.stderr)
                api = AwsCli('fixture', 'us-east-1', 1, execute=FakeGateAws())
                with mock.patch.object(gate_trial, 'AwsCli', return_value=api):
                    self.assertEqual(gate_trial.main(['--paired-cleanup', str(cohort)], self.settings, self.state), 0)
                reservations = [row for row in self.book.read_snapshot().gate_reservations
                                if row.attempt in (str(number) + '-first', str(number) + '-second')]
                self.assertEqual(len(reservations), 2)
                self.assertTrue(all(isinstance(row.state, ledger.GateTerminated) for row in reservations),
                    repr(reservations) + reply.stderr + repr([cloud_gate.read(path) for path in cohort.glob('*/outcome.json')]))
                rows = [ledger.mapping(cast(object, json.loads(line))) for line in reply.stdout.splitlines()]
                if number == signal.SIGINT:
                    self.assertEqual(len(rows), 3)
                    self.assertTrue(rows[-1]['cleanup_complete'])
                    self.assertTrue(all(row['all_passed'] is False for row in rows[:-1]))
                for path in cohort.glob('*/*.json'):
                    if path.name not in ('request.json', 'deployment.json'):
                        path.unlink()

class CloudRouteTests(CostFixture):
    repo: Path = Path()
    modules: list[tuple[str, Path]] = []
    admission: AdmissionFixture = cast(AdmissionFixture, cast(object, None))

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.settings.write_text(self.settings.read_text().replace('region=us-east-1', 'region=us-east-2'))
        _ = self.enterContext(mock.patch.object(cloud_gate, 'account_settings_path', side_effect=lambda: Path(os.environ.get('AWS_TARGET_CONFIG', str(self.settings)))))
        repository = Path(__file__).resolve().parents[2]
        for name in ('merge_gate', 'cloud_gate'):
            _ = (self.root / (name + '.conf')).write_bytes((repository / 'config' / (name + '.conf')).read_bytes())
        self.repo = self.root / 'repo'
        folder = self.repo / 'scripts/fixture'
        folder.mkdir(parents=True)
        for name in ('test_one.py', 'test_two.py'):
            _ = (folder / name).write_text('import unittest\n')
        for args in (('init', '-q', '-b', 'fixture'), ('config', 'user.name', 'Fixture'),
                     ('config', 'user.email', 'fixture@example.invalid'), ('add', '.'), ('commit', '-qm', 'Fixture'),
                     ('remote', 'add', 'origin', '/private/origin'), ('update-ref', 'refs/remotes/origin/main', 'HEAD')):
            _ = gate.checked_git(self.repo, *args)
        self.modules = [('fixture', path) for path in sorted(folder.glob('test_*.py'))]
        _ = self.enterContext(mock.patch.object(cloud_gate, 'discover_test_modules', return_value=self.modules))
        _ = self.enterContext(mock.patch.object(target_config, 'ssh_target_state_directory', return_value=self.state))
        self.admission = caller_admission(self, self.root, {'BUILDLOG_DIR': str(self.root / 'buildlog')})

    def test_main_commit_needs_no_bundle(self) -> None:
        folder = self.root / 'attempt'
        request = cloud_gate.prepare(self.repo, folder, gate.SuiteMode.MERGE_GATE, 1800)
        self.assertEqual(request['tree'], gate.checked_git(self.repo, 'rev-parse', 'HEAD^{tree}'))
        self.assertEqual(request['commit'], gate.checked_git(self.repo, 'rev-parse', 'HEAD'))
        self.assertEqual(request['head'], request['commit'])
        self.assertEqual(request['origin_main'], request['commit'])
        self.assertEqual(request['clone_url'], 'https://github.com/natepiano/claude_commands.git')
        self.assertEqual(request['source_key'], '')
        self.assertFalse((folder / 'source.bundle').exists())
        self.assertEqual(request['modules'], ['scripts/fixture/test_one.py', 'scripts/fixture/test_two.py'])

    def test_bundle_contains_only_the_commit_ahead_of_main(self) -> None:
        base = gate.checked_git(self.repo, 'rev-parse', 'refs/remotes/origin/main')
        _ = (self.repo / 'new-file').write_text('one new commit\n')
        _ = gate.checked_git(self.repo, 'add', 'new-file')
        _ = gate.checked_git(self.repo, 'commit', '-qm', 'New commit')
        folder = self.root / 'attempt'
        request = cloud_gate.prepare(self.repo, folder, gate.SuiteMode.MERGE_GATE, 1800)
        bundle = folder / 'source.bundle'
        header = bundle.read_bytes().split(b'\n\n', 1)[0].decode()
        self.assertIn('-' + base + ' ', header)
        self.assertEqual(request['origin_main'], base)
        refs = gate.checked_git(self.repo, 'bundle', 'list-heads', str(bundle))
        self.assertEqual(refs.splitlines(), [str(request['commit']) + ' refs/cloud-gates/attempt'])
        pack = bundle.read_bytes().split(b'\n\n', 1)[1]
        indexed = subprocess.run(['git', '-C', str(self.repo), 'index-pack', '--stdin', '--fix-thin'],
            input=pack, check=True, capture_output=True, timeout=10)
        pack_hash = indexed.stdout.decode().split()[-1]
        objects = gate.checked_git(self.repo, 'verify-pack', '-v', str(self.repo / '.git/objects/pack' / ('pack-' + pack_hash + '.idx')))
        commits = [line.split()[0] for line in objects.splitlines() if len(line.split()) > 1 and line.split()[1] == 'commit']
        self.assertEqual(commits, [request['commit']])

    def test_snapshot_preserves_checkout_and_fetches_into_clone(self) -> None:
        _ = (self.repo / '.gitignore').write_text('ignored\n')
        _ = gate.checked_git(self.repo, 'add', '.gitignore')
        _ = gate.checked_git(self.repo, 'commit', '-qm', 'Ignore fixture output')
        _ = gate.checked_git(self.repo, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
        clone = self.root / 'clone'
        _ = gate.checked_git(self.repo, 'clone', '-q', str(self.repo), str(clone))
        _ = self.modules[0][1].write_text('tracked edit\n')
        _ = self.modules[1][1].write_text('staged edit\n')
        _ = gate.checked_git(self.repo, 'add', str(self.modules[1][1]))
        _ = (self.repo / 'untracked').write_text('untracked file\n')
        _ = (self.repo / 'ignored').write_text('ignored file\n')
        status = gate.checked_git(self.repo, 'status', '--porcelain')
        index = (self.repo / '.git/index').read_bytes()
        head = gate.checked_git(self.repo, 'rev-parse', 'HEAD')
        refs = gate.checked_git(self.repo, 'for-each-ref')
        folder = self.root / 'attempt'
        request = cloud_gate.prepare(self.repo, folder, gate.SuiteMode.MERGE_GATE, 1800)
        commit = ledger.text(request, 'commit')
        self.assertNotEqual(commit, head)
        self.assertEqual(request['head'], head)
        self.assertEqual(request['tree'], gate.checked_git(self.repo, 'rev-parse', commit + '^{tree}'))
        self.assertEqual(gate.checked_git(self.repo, 'rev-parse', commit + '^'), head)
        for name, content in (('scripts/fixture/test_one.py', 'tracked edit'),
                              ('scripts/fixture/test_two.py', 'staged edit'), ('untracked', 'untracked file')):
            self.assertEqual(gate.checked_git(self.repo, 'show', commit + ':' + name), content)
        self.assertNotIn('ignored', gate.checked_git(self.repo, 'ls-tree', '--name-only', commit).splitlines())
        self.assertEqual(gate.checked_git(self.repo, 'status', '--porcelain'), status)
        self.assertEqual((self.repo / '.git/index').read_bytes(), index)
        self.assertEqual(gate.checked_git(self.repo, 'rev-parse', 'HEAD'), head)
        self.assertEqual(gate.checked_git(self.repo, 'for-each-ref'), refs)
        _ = gate.checked_git(clone, 'fetch', str(folder / 'source.bundle'), commit)
        _ = gate.checked_git(clone, 'checkout', '--detach', commit)
        self.assertEqual(gate.checked_git(clone, 'rev-parse', 'HEAD^{tree}'), request['tree'])
        self.assertEqual((clone / 'untracked').read_text(), 'untracked file\n')

    def test_working_tree_reads_a_same_size_edit_at_the_index_mtime(self) -> None:
        _ = gate.checked_git(self.repo, 'config', 'core.trustctime', 'false')
        source = self.repo / 'racy'
        _ = source.write_text('before\n')
        timestamp = 1_600_000_000_000_000_000
        os.utime(source, ns=(timestamp, timestamp))
        _ = gate.checked_git(self.repo, 'add', 'racy')
        _ = gate.checked_git(self.repo, 'commit', '-qm', 'Add the racy file')
        index = Path(gate.checked_git(self.repo, 'rev-parse', '--path-format=absolute', '--git-path', 'index'))
        os.utime(index, ns=(timestamp, timestamp))
        _ = source.write_text('edited\n')
        index_mtime = index.stat().st_mtime_ns
        os.utime(source, ns=(index_mtime, index_mtime))
        environment = {**os.environ, 'GIT_INDEX_FILE': str(self.root / 'expected-index')}
        for arguments in (('read-tree', 'HEAD'), ('add', '-A')):
            _ = subprocess.run(['git', *arguments], cwd=self.repo, env=environment,
                               check=True, capture_output=True)
        expected = subprocess.run(['git', 'write-tree'], cwd=self.repo, env=environment,
                                  check=True, capture_output=True, text=True).stdout.strip()
        self.assertNotEqual(expected, gate.checked_git(self.repo, 'rev-parse', 'HEAD^{tree}'))
        tree, dirty = cloud_gate.working_tree(self.repo)
        self.assertIn('racy', dirty)
        self.assertEqual(tree, expected)

    def test_targeted_selection_runs_locally_and_sends_only_selected_paths(self) -> None:
        for dirty, paths, later in ((False, ('test_one.py',), ''), (True, ('test_one.py',), ''),
                                    (False, ('test_two.py',), ''), (True, (), ''),
                                    (False, ('test_one.py',), 'edit'), (False, ('test_one.py',), 'commit')):
            selection = affected_modules.AffectedSelection(
                affected_modules.CertifiedBase('base', '2026-01-15T00:00:00+00:00'),
                ('scripts/fixture/test_one.py',), tuple(self.repo / 'scripts/fixture' / name for name in paths))
            with self.subTest(dirty=dirty, paths=paths, later=later):
                _ = gate.checked_git(self.repo, 'reset', '--hard', 'HEAD')
                if dirty:
                    _ = self.modules[0][1].write_text('import os\n')
                expected_tree = gate.clean_test_tree(self.repo)
                with (mock.patch.object(gate.PassedTreeLedger, 'certified_base', autospec=True,
                                        return_value=selection.base) as certified,
                      mock.patch.object(gate, 'select_affected', return_value=selection) as select,
                      redirect_stdout(StringIO())):
                    chosen, book = gate.select_targeted_modules(self.repo, expected_tree, self.modules)
                    self.assertEqual(book.tree, expected_tree)
                    self.assertIsInstance(chosen, affected_modules.AffectedSelection)
                    assert isinstance(chosen, affected_modules.AffectedSelection)
                    selected = gate.TreeSelection(tuple(str(path.relative_to(self.repo)) for path in chosen.modules),
                                                  cloud_gate.working_tree(self.repo)[0])
                    self.assertEqual(select.call_count, 1)
                    if later:
                        _ = self.modules[1][1].write_text('import sys\n')
                        if later == 'commit':
                            _ = gate.checked_git(self.repo, 'add', '.')
                            _ = gate.checked_git(self.repo, 'commit', '-qm', 'Change the selected tree')
                        folder = self.root / ('refused-' + later)
                        with (mock.patch.object(cloud_gate, 'save') as save,
                              mock.patch.object(gate, 'checked_git', wraps=gate.checked_git) as git):
                            with self.assertRaisesRegex(GateRefused, 'The checkout changed after the test selection'):
                                _ = cloud_gate.prepare(self.repo, folder, gate.SuiteMode.TARGETED_TEST, 1800,
                                                       selected=selected)
                        save.assert_not_called()
                        self.assertFalse((folder / 'request.json').exists())
                        self.assertFalse(any('bundle' in call.args for call in git.call_args_list))
                        cloud_gate.save(self.state / 'gate-deployment.json', {'image': 'fixture-image', 'template': 'fixture-template'})
                        with mock.patch.object(cloud_gate, 'run_service', side_effect=AssertionError('No service may start')) as service:
                            outcome = cloud_gate.run(self.repo, mode=gate.SuiteMode.TARGETED_TEST, full=False,
                                                     module_time_limit_s=1800, selected=selected)
                        self.assertEqual(outcome, cloud_gate.LocalFallback('The checkout changed after the test selection'))
                        service.assert_not_called()
                        continue
                    request = cloud_gate.prepare(self.repo, self.root / 'attempt', gate.SuiteMode.TARGETED_TEST, 1800,
                                                selected=selected)
                    self.assertEqual(select.call_count, 1)
                self.assertEqual(select.call_args.args[0], self.repo)
                self.assertEqual(select.call_args.args[3], affected_modules.dirty_paths(self.repo))
                self.assertEqual(cast(gate.PassedTreeLedger, certified.call_args.args[0]).tree, expected_tree)
                self.assertEqual(request['modules'], ['scripts/fixture/' + name for name in paths])

    def test_retry_uses_a_new_attempt_when_the_booking_closed_without_an_outcome(self) -> None:
        cloud_gate.save(self.state / 'gate-deployment.json', {'image': 'fixture-image', 'template': 'fixture-template'})
        folders: list[Path] = []
        book = ledger.CostLedger(self.state)
        now = int(time.time())
        book.record_inventory(ledger.ResourceInventory(now, (ledger.PricedResource('fixture', Decimal(0)),), 'fixture'))
        book.observe(ledger.InstanceObservation(now, 'stopped', 'fixture'))
        def service(command: Sequence[str]) -> int:
            folder = Path(command[command.index('--worker') + 1])
            folders.append(folder)
            request = cloud_gate.read(folder / 'request.json')
            _ = book.reserve_gate(self.settings, ledger.GateReservation(
                folder.name, now, ledger.instant(request, 'expires'), Decimal('.8'), Decimal('.02')))
            book.resolve_gate(folder.name, ledger.GateAbsent(now))
            return 0
        for _ in range(2):
            _ = cloud_gate.run(self.repo, mode=gate.SuiteMode.MERGE_GATE, full=False,
                               module_time_limit_s=1800, service=service)
        self.assertNotEqual(folders[0], folders[1])
        self.assertFalse(any((folder / 'outcome.json').exists() for folder in folders))

    def test_pending_attempt_reuses_only_the_same_working_tree(self) -> None:
        cloud_gate.save(self.state / 'gate-deployment.json', {'image': 'fixture-image', 'template': 'fixture-template'})
        folders: list[Path] = []
        def service(command: Sequence[str]) -> int:
            folder = Path(command[command.index('--worker') + 1])
            folders.append(folder)
            cloud_gate.save(folder / 'outcome.json', {'status': 'pending', 'reason': 'fixture pending'})
            return 0
        for content in ('first edit\n', 'first edit\n', 'second edit\n'):
            _ = self.modules[0][1].write_text(content)
            _ = cloud_gate.run(self.repo, mode=gate.SuiteMode.MERGE_GATE, full=False,
                               module_time_limit_s=1800, service=service)
        self.assertEqual(folders[0], folders[1])
        self.assertNotEqual(folders[1], folders[2])
        paths = ('scripts/fixture/test_one.py',)
        tree, dirty = cloud_gate.working_tree(self.repo)
        self.assertTrue(dirty)
        selected = gate.TreeSelection(paths, tree)
        _ = cloud_gate.run(self.repo, mode=gate.SuiteMode.TARGETED_TEST, full=False,
                           module_time_limit_s=1800, service=service, selected=selected)
        stale = gate.TreeSelection(paths, gate.checked_git(self.repo, 'rev-parse', 'HEAD^{tree}'))
        with self.subTest(selection='different tree with a saved request'):
            count = len(folders)
            outcome = cloud_gate.run(self.repo, mode=gate.SuiteMode.TARGETED_TEST, full=False,
                                     module_time_limit_s=1800, service=service, selected=stale)
            self.assertEqual(outcome, cloud_gate.LocalFallback('The checkout changed after the test selection'))
            self.assertEqual(len(folders), count)
        reserve = cloud_gate.cloud_reservation

        @contextmanager
        def changed_reservation(repo: Path, sidecar: Path, mode: gate.SuiteMode) -> Generator[cloud_gate.CloudReservation]:
            with reserve(repo, sidecar, mode) as reservation:
                _ = self.modules[1][1].write_text('import sys\n')
                yield reservation

        with (self.subTest(selection='tree changed during reservation'),
              mock.patch.object(cloud_gate, 'cloud_reservation', side_effect=changed_reservation),
              mock.patch.object(cloud_gate, 'run_service', side_effect=AssertionError('No service may start')) as start):
            outcome = cloud_gate.run(self.repo, mode=gate.SuiteMode.TARGETED_TEST, full=False,
                                     module_time_limit_s=1800, service=service,
                                     selected=gate.TreeSelection(('scripts/fixture/test_two.py',), tree))
        self.assertEqual(outcome, cloud_gate.LocalFallback('The checkout changed after the test selection'))
        start.assert_not_called()

    def test_both_service_types_have_stop_cleanup(self) -> None:
        folder = self.root / 'trial with spaces'
        for paired in (False, True):
            module = 'scripts.aws_target.gate_trial' if paired else 'scripts.aws_target.cloud_gate'
            options = ['--paired-worker', str(folder)] if paired else ['--worker', str(folder),
                '--directory', str(self.state), '--sidecar', str(folder / 'reservation'), '--settings', str(self.settings)]
            command = cloud_gate.service_command(module, options, 'fixture', paired=paired)
            cleanup = [argument for argument in command if argument.startswith('--property=ExecStopPost=')]
            self.assertEqual(len(cleanup), 1)
            import shlex
            arguments = shlex.split(cleanup[0].split('=', 2)[2])
            expected = ['--paired-cleanup', str(folder)] if paired else ['--cleanup' if value == '--worker' else value for value in options]
            self.assertEqual(arguments, [sys.executable, '-m', module, *expected])

    def test_service_command_omits_conf_dir_without_caller_override(self) -> None:
        configuration = cloud_gate.settings()
        for paired in (False, True):
            with (self.subTest(paired=paired), mock.patch.dict(os.environ),
                  mock.patch.object(cloud_gate, 'settings', return_value=configuration)):
                _ = os.environ.pop('CONF_DIR', None)
                command = cloud_gate.service_command('scripts.aws_target.gate_trial', ['--paired-worker', str(self.root / 'trial')], 'fixture', paired=paired)
                self.assertFalse(any(argument.startswith('--setenv=CONF_DIR=') for argument in command))

    def test_service_command_preserves_caller_conf_dir(self) -> None:
        configuration = cloud_gate.settings()
        for paired in (False, True):
            for directory in ('', str(self.root / 'caller-config')):
                with (self.subTest(paired=paired, directory=directory),
                      mock.patch.dict(os.environ, {'CONF_DIR': directory}),
                      mock.patch.object(cloud_gate, 'settings', return_value=configuration)):
                    command = cloud_gate.service_command('scripts.aws_target.gate_trial', ['--paired-worker', str(self.root / 'trial')], 'fixture', paired=paired)
                    overrides = [argument for argument in command if argument.startswith('--setenv=CONF_DIR=')]
                    self.assertEqual(overrides, ['--setenv=CONF_DIR=' + directory])

    def test_session_runs_only_scaler_service_and_imports_explicit_record(self) -> None:
        for dirty, later_edit, changed_head in ((False, False, False), (True, False, False),
                                               (True, True, False), (True, False, True)):
            with self.subTest(dirty=dirty, later_edit=later_edit, changed_head=changed_head):
                if dirty:
                    _ = self.modules[0][1].write_text('snapshot edit\n')
                cloud_gate.save(self.state / 'gate-deployment.json', {'image': 'fixture-image', 'template': 'fixture-template'})
                commands: list[list[str]] = []

                def service(command: Sequence[str]) -> int:
                    commands.append(list(command))
                    folder = Path(command[command.index('--worker') + 1])
                    request = cloud_gate.read(folder / 'request.json')
                    run = gate.GateRun(tuple(gate.GateJobResult('fixture', path.name, 0, 1, '') for _, path in self.modules), 1, 1)
                    row: dict[str, object] = {key: request[key] for key in ('attempt', 'commit', 'tree')}
                    row.update(host='remote-host', verdict='pass', gate=gate_box.encode_run(run), record=gate_box.completion_record(
                        run, ledger.text(request, 'attempt'), ledger.text(request, 'commit'), ledger.text(request, 'tree'),
                        'remote-host', 0, 1, 'merge-gate'))
                    record = ledger.mapping(row['record'])
                    record.update(head=request['head'], dirty=bool(request['dirty_paths']), dirty_paths=request['dirty_paths'])
                    if later_edit:
                        _ = self.modules[0][1].write_text('later edit\n')
                    if changed_head:
                        _ = gate.checked_git(self.repo, 'add', '.')
                        _ = gate.checked_git(self.repo, 'commit', '-qm', 'Later HEAD')
                    cloud_gate.save(folder / 'outcome.json', {'status': 'complete', 'result': row})
                    _ = (folder / 'gate.log').write_text('cloud log\n')
                    return 0

                destination = self.root / f'record-{dirty}-{later_edit}-{changed_head}.jsonl'
                with (mock.patch.object(cloud_gate, 'AwsCli', side_effect=AssertionError('The session must not call AWS')),
                      mock.patch.object(store, 'host_file', return_value=destination),
                      mock.patch.dict(os.environ, {'MERGE_GATE_PASSED_DIR': str(self.root / 'passed')}),
                      redirect_stdout(StringIO()) as output):
                    outcome = cloud_gate.run(self.repo, mode=gate.SuiteMode.MERGE_GATE, full=False,
                                             module_time_limit_s=1800, service=service)
                if changed_head:
                    self.assertEqual(outcome, cloud_gate.LocalFallback('The checkout changed during the cloud attempt'))
                    self.assertFalse(destination.exists())
                    continue
                self.assertIsInstance(outcome, cloud_gate.CloudCompleted)
                self.assertIn('cloud log', output.getvalue())
                self.assertEqual(commands[0][0], 'systemd-run')
                sidecar = Path(commands[0][commands[0].index('--sidecar') + 1])
                folder = Path(commands[0][commands[0].index('--worker') + 1])
                self.assertEqual(sidecar, folder / 'reservation.cgroup')
                self.assertIn('--setenv=AWS_SHARED_CREDENTIALS_FILE=' + str(self.root / '.config/aws-build/credentials'), commands[0])
                self.assertIn('--property=KillMode=control-group', commands[0])
                self.assertIn('--working-directory=' + str(self.root / '.claude'), commands[0])
                self.assertEqual(ledger.mapping(cast(object, json.loads(destination.read_text())))['host'], 'remote-host')

                record = cloud_gate.read(destination)
                request = cloud_gate.read(folder / 'request.json')
                self.assertEqual(record['sha'], request['commit'])
                self.assertEqual(record['head'], request['head'])
                self.assertEqual(record['dirty'], dirty)
                self.assertFalse((self.root / 'passed/passed-trees.jsonl').exists())

    def test_absent_stack_needs_no_service_or_aws_call(self) -> None:
        service = mock.Mock(side_effect=AssertionError('An absent stack needs no service'))
        outcome = cloud_gate.run(self.repo, mode=gate.SuiteMode.MERGE_GATE, full=False,
                                 module_time_limit_s=1800, service=service)
        self.assertIsInstance(outcome, cloud_gate.LocalFallback)
        service.assert_not_called()

    def test_cloud_admission_uses_route_identity_and_no_local_memory(self) -> None:
        cloud_gate.save(self.state / 'gate-deployment.json', {'image': 'fixture-image', 'template': 'fixture-template'})
        self.admission.free(0)
        requests: list[wire.AdmitRequest] = []
        admit = admission_client.admit
        def record(request: wire.AdmitRequest, on_wait: Callable[[wire.Wait], None],
                   refresh: Callable[[], wire.AdmitRequest]) -> wire.Admitted | wire.Refused | admission_client.LeftQueue:
            requests.append(request)
            return admit(request, on_wait, refresh)
        def service(command: Sequence[str]) -> int:
            record, = self.admission.caller_reservations().values()
            self.assertEqual(record.need, 0)
            sidecar = Path(command[command.index('--sidecar') + 1])
            self.assertEqual(str(sidecar), requests[-1].sidecar)
            initial = cloud_progress.read(sidecar)
            self.assertIsInstance(initial, cloud_progress.CloudProgress)
            assert isinstance(initial, cloud_progress.CloudProgress)
            self.assertEqual(initial.state, 'requesting')
            self.assertEqual((initial.stage, initial.region, initial.zone), ('launch', '', ''))
            folder = Path(command[command.index('--worker') + 1])
            cloud_gate.save(folder / 'outcome.json', {'status': 'fallback', 'reason': 'capacity'})
            return 0
        with mock.patch.object(admission_client, 'admit', side_effect=record):
            for mode in (gate.SuiteMode.MERGE_GATE, gate.SuiteMode.TARGETED_TEST):
                outcome = cloud_gate.run(self.repo, mode=mode, full=True, module_time_limit_s=1800, service=service)
                self.assertIsInstance(outcome, cloud_gate.LocalFallback)
                self.assertTrue(self.admission.callers_released())
                request = requests[-1]
                self.assertEqual(request.place, wire.CloudPlace('aws'))
                self.assertEqual(request.identity.step, mode.value)
                self.assertEqual(request.owner.pid, os.getpid())
                self.assertEqual(request.run, wire.RunIdentity(request.owner.pid, request.owner.start_time))

    def test_cloud_fallback_releases_before_local_memory_admission(self) -> None:
        cloud_gate.save(self.state / 'gate-deployment.json', {'image': 'fixture-image', 'template': 'fixture-template'})
        def service(command: Sequence[str]) -> int:
            record, = self.admission.caller_reservations().values()
            self.assertEqual(record.need, 0)
            folder = Path(command[command.index('--worker') + 1])
            cloud_gate.save(folder / 'outcome.json', {'status': 'fallback', 'reason': 'capacity'})
            return 0
        run = cloud_gate.run
        def route(repo: Path, **_kwargs: object) -> cloud_gate.LocalFallback | cloud_gate.CloudCompleted:
            return run(repo, mode=gate.SuiteMode.TARGETED_TEST, full=True, module_time_limit_s=1800, service=service)
        def local(repo: Path, *_args: object, **_kwargs: object) -> gate.GateRun:
            self.assertTrue(self.admission.callers_released())
            with gate.gate_reservation(repo, self.root, gate.BuildHoldWait(), mode=gate.SuiteMode.TARGETED_TEST) as place:
                record, = self.admission.caller_reservations().values()
                self.assertGreater(record.need, 0)
                place.status = 0
            return gate.GateRun((), 0, 0)
        with (mock.patch.object(cloud_gate, 'run', side_effect=route),
              mock.patch.object(gate, 'run_suites', side_effect=local), redirect_stdout(StringIO())):
            _ = gate.run_gate_suites(self.repo, full=True, mode=gate.SuiteMode.TARGETED_TEST)
        self.assertTrue(self.admission.callers_released())

    def test_cloud_service_error_releases_its_slot(self) -> None:
        cloud_gate.save(self.state / 'gate-deployment.json', {'image': 'fixture-image', 'template': 'fixture-template'})
        for status_error in (False, True):
            with (self.subTest(status_error=status_error),
                  mock.patch.object(affected_modules, 'dirty_paths', return_value=set(),
                                    side_effect=affected_modules.GraphError('fixture status error') if status_error else None)):
                service = mock.Mock(side_effect=OSError('fixture service error'))
                outcome = cloud_gate.run(self.repo, mode=gate.SuiteMode.MERGE_GATE, full=True,
                                         module_time_limit_s=1800, service=service)
                self.assertIsInstance(outcome, cloud_gate.LocalFallback)
                self.assertTrue(self.admission.callers_released())
                if status_error:
                    self.assertEqual(outcome, cloud_gate.LocalFallback('fixture status error'))
                    service.assert_not_called()

    def test_route_uses_installed_limit_and_handles_missing_results(self) -> None:
        cloud_gate.save(self.state / 'gate-deployment.json', {'image': 'fixture-image', 'template': 'fixture-template'})
        outcome = cloud_gate.run(self.repo, mode=gate.SuiteMode.MERGE_GATE, full=True, module_time_limit_s=1800,
                                 service=lambda _command: 0)
        self.assertIsInstance(outcome, cloud_gate.LocalFallback)
        self.assertTrue(self.admission.callers_released())

        installed = self.root / '.claude/config/aws_target.conf'
        installed.parent.mkdir(parents=True)
        _ = installed.write_bytes(self.settings.read_bytes())
        numeric_settings(installed, {'daily_limit_usd': '1'})
        checkout = self.repo / 'config'
        checkout.mkdir()
        for source in self.root.glob('*.conf'):
            _ = (checkout / source.name).write_bytes(source.read_bytes())
        for missing_minimum in (True, False):
            with self.subTest(missing_minimum=missing_minimum):
                contents = self.settings.read_text()
                if missing_minimum:
                    contents = '\n'.join(line for line in contents.splitlines()
                                         if not line.startswith('box_minimum_seconds=')) + '\n'
                _ = (checkout / 'aws_target.conf').write_text(contents)
                fake = FakeGateAws()
                now = int(time.time())
                book = ledger.CostLedger(self.state)
                book.record_inventory(ledger.ResourceInventory(now, (ledger.PricedResource('fixture-fixed', Decimal(0)),), 'fixture'))
                book.observe(ledger.InstanceObservation(now, 'stopped', 'fixture'))
                def service(command: Sequence[str]) -> int:
                    folder = Path(command[command.index('--worker') + 1])
                    request = cloud_gate.read(folder / 'request.json')
                    run = gate.GateRun(tuple(gate.GateJobResult('fixture', path.name, 0, 1, '')
                                             for _, path in self.modules), 1, 1)
                    fake.result = {key: request[key] for key in ('attempt', 'commit', 'tree')}
                    fake.result.update(host='fixture-guest', verdict='pass', gate=gate_box.encode_run(run),
                        record=gate_box.completion_record(run, ledger.text(request, 'attempt'),
                            ledger.text(request, 'commit'), ledger.text(request, 'tree'), 'fixture-guest', now, now + 1, 'merge-gate'))
                    api = AwsCli('build-scaler', 'fixture-region', 1, execute=fake)
                    arguments = list(command[command.index('--worker'):])
                    with (mock.patch.object(cloud_gate, 'AwsCli', return_value=api),
                          mock.patch.object(sys, 'argv', ['cloud-gate', *arguments])):
                        return cloud_gate.main()
                deployment: dict[str, object] = {'image': 'fixture-image', 'template': 'fixture-template',
                    'deployment_id': 'fixture', 'bucket': 'fixture-bucket', 'version': '1',
                    'security_group': 'fixture-group', 'revision': 'fixture-revision',
                    'subnets': [{'id': 'fixture-a', 'zone': 'a'}]}
                cloud_gate.save(self.state / 'gate-deployment.json', deployment)
                with mock.patch.dict(os.environ, {'CONF_DIR': str(checkout), 'AWS_TARGET_CONFIG': str(installed)}):
                    outcome = cloud_gate.run(self.repo, mode=gate.SuiteMode.MERGE_GATE, full=True,
                                             module_time_limit_s=1800, service=service)
                self.assertEqual(outcome, cloud_gate.LocalFallback('daily_limit'))
                self.assertEqual(fake.launches, [])
                self.assertTrue(self.admission.callers_released())
