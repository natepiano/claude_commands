"""Test hourly Spot snapshots with private files and fake AWS reads."""
from __future__ import annotations

from collections.abc import Sequence
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import cast, override
import unittest
from unittest import mock

from scripts.aws_target import admin_secrets, aws_target, deploy, ledger, spot_table
from scripts.aws_target.control_test_support import TestClock
from scripts.aws_target.ledger_test_support import CostFixture, text_setting
from scripts.lib import atomic_file, conf


class ControlTests(CostFixture):
    clock: TestClock = TestClock()
    calls: list[tuple[str, ...]] = []
    regions: tuple[str, ...] = ()
    sizes: tuple[str, ...] = ()
    standard: tuple[str, ...] = ()
    gpu: tuple[str, ...] = ()
    failure: str = ''
    omitted: str = ''
    quota: int = 0

    @override
    def setUp(self) -> None:
        super().setUp()
        self.clock = TestClock()
        self.calls = []
        self.failure = ''
        self.omitted = ''
        source = Path(__file__).resolve().parents[2] / 'config/cloud_gate.conf'
        values_path = self.root / 'cloud_gate.conf'
        _ = values_path.write_bytes(source.read_bytes())
        # Use different regions and types to detect dependencies on shipped values.
        text_setting(values_path, 'spot_candidate_regions', 'us-east-1 us-west-1 us-east-1')
        text_setting(values_path, 'instance_types', 'c7a.4xlarge m7a.2xlarge')
        text_setting(values_path, 'gpu_instance_types', 'g6.2xlarge g4dn.xlarge')
        values = conf.read(values_path)
        self.regions = tuple(dict.fromkeys(values.words('spot_candidate_regions')))
        self.standard = values.words('instance_types')
        self.gpu = values.words('gpu_instance_types')
        self.sizes = (*self.standard, *self.gpu)
        self.quota = deploy.largest_gate_vcpus(self.sizes) * 3 + 1
        self.enterContext(mock.patch.dict(os.environ, {
            'AWS_ACCESS_KEY_ID': 'fixture-admin-id', 'AWS_SECRET_ACCESS_KEY': 'fixture-admin-secret',
            'AWS_SESSION_TOKEN': 'fixture-token', 'AWS_CONFIG_FILE': '/fixture/admin-config',
            'AWS_PROFILE': 'fixture-admin', 'AWS_DEFAULT_PROFILE': 'fixture-admin',
            'AWS_SHARED_CREDENTIALS_FILE': str(self.root / 'private-scaler'),
        }))

    def execute(self, arguments: Sequence[str], *, capture_output: bool, text: bool,
                timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        self.assertTrue(capture_output and text)
        self.assertFalse(check)
        configuration = conf.read(self.settings)
        self.assertEqual(timeout, configuration.number('aws_timeout_s'))
        self.assertEqual(arguments[arguments.index('--profile') + 1], configuration.text('credentials_profile'))
        self.assertIsNotNone(env)
        environment = cast(dict[str, str], env)
        self.assertEqual(environment['AWS_SHARED_CREDENTIALS_FILE'], str(self.root / 'private-scaler'))
        self.assertEqual(environment['AWS_CONFIG_FILE'], os.devnull)
        for key in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_PROFILE', 'AWS_DEFAULT_PROFILE'):
            self.assertNotIn(key, environment)
        region = arguments[arguments.index('--region') + 1]
        self.calls.append(tuple(arguments))
        operation = arguments[6]
        self.assertIn(operation, ('describe-spot-price-history', 'get-service-quota'))
        self.assertNotIn('--no-paginate', arguments)
        self.assertNotIn('--max-items', arguments)
        if region == self.regions[0] and self.failure == 'timeout':
            raise subprocess.TimeoutExpired('fixture-secret', timeout)
        if region == self.regions[0] and (self.failure == operation or
                self.failure == 'gpu-quota' and operation == 'get-service-quota' and 'L-3819A6DF' in arguments):
            return subprocess.CompletedProcess(arguments, 1, '', 'fixture-secret denied')
        reply: dict[str, object]
        if operation == 'get-service-quota':
            code = arguments[arguments.index('--quota-code') + 1]
            self.assertIn(code, ('L-34B43A08', 'L-3819A6DF'))
            reply = {'Quota': {'Value': self.quota if code == 'L-34B43A08' else self.quota - 1}}
        else:
            self.assertEqual(tuple(arguments[arguments.index('--instance-types') + 1:arguments.index('--product-descriptions')]), self.sizes)
            stamp = datetime.fromtimestamp(self.clock.now, UTC).isoformat()
            rows = [dict(InstanceType=size, AvailabilityZone=region + zone, SpotPrice=price, Timestamp=stamp)
                    for size in self.sizes if size != self.omitted for zone, price in (('a', '.25'), ('b', '.3'))]
            rows.append(dict(InstanceType=self.sizes[0], AvailabilityZone=region + 'a', SpotPrice='.9',
                             Timestamp=datetime.fromtimestamp(self.clock.now - 1, UTC).isoformat()))
            reply = {'SpotPriceHistory': rows}
            if region == self.regions[0] and self.failure == 'pagination':
                reply['NextToken'] = 'fixture-page'
        return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')

    def snapshot(self) -> int:
        return aws_target.snapshot(self.settings, self.state, execute=self.execute, clock=self.clock)

    def test_snapshot_writes_prices_and_quotas_with_one_atomic_write_and_limits_repeats(self) -> None:
        self.assertFalse((self.state / 'gate-deployment.json').exists())
        with mock.patch.object(aws_target, 'write_text_atomic', wraps=atomic_file.write_text_atomic) as write:
            self.assertEqual(self.snapshot(), 0)
        write.assert_called_once()
        self.assertEqual(write.call_args.args[0], self.state / 'spot-prices.json')
        prices = ledger.read_spot_prices(self.state)
        self.assertEqual(len(prices), len(self.regions) * len(self.sizes) * 2)
        self.assertTrue(all(row.read_at == self.clock.now for row in prices))
        self.assertEqual({row.usd_per_hour for row in prices}, {Decimal('.25'), Decimal('.3')})
        readings = ledger.rows(aws_target.read_snapshot(self.state), 'regions')
        self.assertEqual([row['region'] for row in readings], list(self.regions))
        for row in readings:
            self.assertEqual(row['standard_vcpus'], str(self.quota))
            self.assertEqual(row['gpu_vcpus'], str(self.quota - 1))
            self.assertEqual(row['read_at'], self.clock.now)
        count = len(self.regions) * 3
        self.assertEqual(len(self.calls), count)
        interval = conf.read(self.settings).whole('price_refresh_seconds')
        self.clock.advance(interval - 1)
        self.assertEqual(self.snapshot(), 0)
        self.assertEqual(len(self.calls), count)
        self.clock.advance(1)
        self.assertEqual(self.snapshot(), 0)
        self.assertEqual(len(self.calls), 2 * count)

    def test_regional_failure_preserves_its_values_and_time_but_refreshes_other_regions(self) -> None:
        self.assertEqual(self.snapshot(), 0)
        original = aws_target.read_snapshot(self.state)
        for failure in ('timeout', 'describe-spot-price-history', 'get-service-quota', 'gpu-quota', 'pagination'):
            with self.subTest(failure=failure):
                self.clock.advance(conf.read(self.settings).whole('price_refresh_seconds'))
                self.failure = failure
                with redirect_stderr(StringIO()) as errors:
                    self.assertEqual(self.snapshot(), 1)
                self.assertNotIn('fixture-secret', errors.getvalue())
                document = aws_target.read_snapshot(self.state)
                for field in ('prices', 'regions'):
                    old = [row for row in ledger.rows(original, field) if row['region'] == self.regions[0]]
                    current = [row for row in ledger.rows(document, field) if row['region'] == self.regions[0]]
                    self.assertEqual(current, old)
                    good = [row for row in ledger.rows(document, field) if row['region'] == self.regions[1]]
                    self.assertTrue(all(row['read_at'] == self.clock.now for row in good))

    def test_held_lock_skips_the_snapshot_without_calls(self) -> None:
        with (self.state / 'spot-prices.lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.snapshot(), 0)
            self.assertEqual(self.calls, [])
            self.assertFalse((self.state / 'spot-prices.json').exists())
        self.assertEqual(self.snapshot(), 0)

    def test_table_reads_only_local_files_and_shows_minimum_prices_ages_and_quota_fit(self) -> None:
        self.omitted = self.gpu[-1]
        self.assertEqual(self.snapshot(), 0)
        old = self.clock.now
        self.clock.advance(conf.read(self.settings).whole('price_max_age_seconds') + 1)
        before = {path: path.read_bytes() for path in self.state.iterdir()}
        with (mock.patch.object(subprocess, 'run', side_effect=AssertionError('No external command')),
              mock.patch.object(admin_secrets.AdminSecretSession, 'get', side_effect=AssertionError('No administrator key'))):
            table = spot_table.render(self.state, int(self.clock.now), width=50)
            argv = ['deploy.py', 'spot-table', '--settings', str(self.settings), '--directory', str(self.state)]
            with mock.patch.object(sys, 'argv', argv), redirect_stdout(StringIO()) as output:
                self.assertEqual(deploy.main(), 0)
        self.assertIn('Spot prices (USD/h; lowest zone)', output.getvalue())
        self.assertTrue(all(len(line) <= 50 for line in table.splitlines()))
        self.assertIn('0.2500', table)
        self.assertNotIn('0.3000', table)
        self.assertIn(f'snapshot age {int(self.clock.now - old)}s (stale)', table)
        for region in self.regions:
            self.assertIn(region, table)
        for size in self.sizes:
            vcpus = deploy.largest_gate_vcpus((size,))
            quota = self.quota - 1 if size in self.gpu else self.quota
            self.assertIn(f'{size} ({vcpus} vCPUs): {quota // vcpus} boxes', table)
        missing = next(line for line in table.splitlines() if line.startswith(self.omitted))
        self.assertTrue(all(cell == '-' for cell in missing.split()[1:]))
        self.assertEqual(before, {path: path.read_bytes() for path in self.state.iterdir()})

    def test_absent_snapshot_prints_unknown_values_without_creating_files(self) -> None:
        table = spot_table.render(self.state, int(self.clock.now))
        self.assertIn('snapshot age unknown', table)
        self.assertNotIn('0.0000', table)
        self.assertEqual(list(self.state.iterdir()), [])

    def test_tick_reads_only_account_charges(self) -> None:
        controller = aws_target.AccountRefresh(self.settings, self.state, execute=self.execute, clock=self.clock)
        with mock.patch.object(controller, 'refresh_charges') as charges:
            controller.tick()
        charges.assert_called_once()
        self.assertEqual(self.calls, [])

    def test_snapshot_cli_uses_the_aws_state_directory(self) -> None:
        with (mock.patch.object(aws_target, 'ssh_target_state_directory', return_value=self.state),
              mock.patch.object(aws_target, 'snapshot', return_value=1) as read,
              mock.patch.object(sys, 'argv', ['aws_target.py', 'snapshot', '--settings', str(self.settings)])):
            self.assertEqual(aws_target.main(), 1)
        read.assert_called_once_with(self.settings, self.state)

    def test_scaler_policy_allows_snapshot_reads_in_all_regions(self) -> None:
        document = ledger.mapping(cast(object, json.loads(deploy.TEMPLATE.read_text())))
        resources = ledger.mapping(document['Resources'])
        policy = ledger.mapping(ledger.mapping(resources['GateGpuTrialScalerPolicy'])['Properties'])
        statements = ledger.rows(ledger.mapping(policy['PolicyDocument']), 'Statement')
        for action in ('ec2:DescribeSpotPriceHistory', 'service-quotas:GetServiceQuota'):
            self.assertTrue(any(action in cast(list[str], row['Action']) and row['Resource'] == '*'
                                and row['Effect'] == 'Allow' and 'Condition' not in row for row in statements))


if __name__ == '__main__':
    _ = unittest.main()
