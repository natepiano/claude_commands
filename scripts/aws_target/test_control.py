"""Lifecycle actions require queue evidence and durable spending permission."""
from __future__ import annotations

from collections.abc import Sequence
from contextlib import redirect_stderr
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
import fcntl
import json
import os
import sys
import subprocess
from pathlib import Path
from typing import TextIO, override
import unittest
from unittest import mock


from scripts.aws_target import aws_target, ledger
from scripts.aws_target.control_test_support import AwsReplies, TestClock
from scripts.aws_target.ledger_test_support import CostFixture
from scripts.aws_target.ledger_test_support import text_setting


class ControlTests(CostFixture):
    clock: TestClock = TestClock()
    aws: AwsReplies = AwsReplies()
    @override
    def setUp(self) -> None:
        super().setUp()
        self.clock = TestClock()
        self.aws = AwsReplies()


    def gate_price_controller(self, failure: str = '') -> tuple[aws_target.AccountRefresh, list[tuple[str, ...]]]:
        shipped = Path(__file__).resolve().parents[2] / 'config/cloud_gate.conf'
        values = self.root / 'cloud_gate.conf'
        _ = values.write_bytes(shipped.read_bytes())
        text_setting(values, 'spot_candidate_regions', 'us-west-2 us-east-2 us-west-2')
        text_setting(values, 'instance_types', 'm7a.8xlarge c7a.8xlarge')
        regions = ('us-east-2', 'us-east-1')
        _ = (self.state / 'gate-deployment.json').write_text(json.dumps({'regions': [
            dict(region=region, subnets=[dict(id='fixture', zone=region + suffix) for suffix in ('a', 'b')])
            for region in regions]}))
        calls: list[tuple[str, ...]] = []
        def execute(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
            if 'describe-spot-price-history' not in arguments or '--no-paginate' not in arguments:
                return self.aws(arguments, **options)
            calls.append(tuple(arguments))
            region = arguments[arguments.index('--region') + 1]
            if region == 'us-east-2' and failure == 'timeout':
                raise subprocess.TimeoutExpired('fixture', 1)
            if region == 'us-east-2' and failure == 'unavailable':
                return subprocess.CompletedProcess(arguments, 1, '', 'fixture price failure')
            rows = [dict(InstanceType=size, AvailabilityZone=region + suffix, SpotPrice='.25',
                         Timestamp=datetime.fromtimestamp(self.clock.now, UTC).isoformat())
                    for size in ('m7a.8xlarge', 'c7a.8xlarge') for suffix in ('a', 'b')]
            rows.append({**rows[0], 'SpotPrice': '.9',
                'Timestamp': datetime.fromtimestamp(self.clock.now - 3600, UTC).isoformat()})
            reply: dict[str, object] = {'SpotPriceHistory': rows}
            if region == 'us-east-2':
                if failure == 'missing-type':
                    reply['SpotPriceHistory'] = [row for row in rows if row['InstanceType'] == 'm7a.8xlarge']
                elif failure == 'missing-zone':
                    reply['SpotPriceHistory'] = [row for row in rows if row['AvailabilityZone'].endswith('a')]
                elif failure == 'empty':
                    reply['SpotPriceHistory'] = []
                elif failure == 'pagination':
                    reply['NextToken'] = 'fixture-page'
                elif failure == 'malformed':
                    rows[0]['SpotPrice'] = 'invalid'
            return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')
        controller = aws_target.AccountRefresh(self.settings, self.state, clock=self.clock)
        controller.execute = execute
        _ = self.enterContext(mock.patch.object(controller, 'refresh_charges'))
        return controller, calls

    def test_gate_price_refresh_reads_each_region_once_and_uses_the_latest_zone_price(self) -> None:
        controller, calls = self.gate_price_controller()
        controller.tick()
        self.assertEqual([args[args.index('--region') + 1] for args in calls], ['us-west-2', 'us-east-2', 'us-east-1'])
        for args in calls:
            self.assertEqual(args[args.index('--instance-types') + 1:args.index('--product-descriptions')],
                             ('m7a.8xlarge', 'c7a.8xlarge'))
            self.assertEqual(args[args.index('--product-descriptions') + 1], 'Linux/UNIX')
        prices = ledger.read_spot_prices(self.state)
        self.assertEqual(len(prices), 12)
        self.assertTrue(all(row.usd_per_hour == Decimal('.25') and row.read_at == self.clock.now for row in prices))
        self.clock.advance(3599)
        _ = controller.tick()
        self.assertEqual(len(calls), 3)
        self.clock.advance(1)
        _ = controller.tick()
        self.assertEqual(len(calls), 6)

    def test_failed_or_partial_price_reads_keep_the_previous_file_and_complete_the_tick(self) -> None:
        ledger.write_spot_prices(self.state, (
            ledger.RegionalSpotPrice('us-east-2', 'us-east-2a', 'm7a.8xlarge', Decimal('.4'), self.clock.now - 7201),))
        original = (self.state / 'spot-prices.json').read_bytes()
        for failure in ('timeout', 'unavailable', 'missing-type', 'missing-zone', 'empty', 'pagination', 'malformed'):
            with self.subTest(failure=failure):
                (self.state / 'spot-prices.lock').unlink(missing_ok=True)
                controller, calls = self.gate_price_controller(failure)
                with redirect_stderr(StringIO()):
                    controller.tick()
                self.assertEqual((self.state / 'spot-prices.json').read_bytes(), original)
                self.assertEqual(len(calls), 2)
                self.clock.advance(1)
                with redirect_stderr(StringIO()):
                    controller.tick()
                self.assertEqual(len(calls), 2)

    def test_gate_price_refresh_works_with_the_builder_removed_and_routing_disabled(self) -> None:
        controller, calls = self.gate_price_controller()
        controller.tick()
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(ledger.read_spot_prices(self.state)), 12)
        self.assertEqual(self.aws.count('describe-instances'), 0)


    def test_a_held_price_lock_makes_the_tick_skip_the_price_refresh(self) -> None:
        controller, calls = self.gate_price_controller()
        path = self.state / 'spot-prices.lock'
        flock = fcntl.flock
        with path.open('a+') as held:
            flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            inode = os.fstat(held.fileno()).st_ino
            def lock_operation(descriptor: int | TextIO, operation: int) -> None:
                number = descriptor if isinstance(descriptor, int) else descriptor.fileno()
                if os.fstat(number).st_ino == inode:
                    self.assertTrue(operation & fcntl.LOCK_NB)
                flock(number, operation)
            with mock.patch.object(fcntl, 'flock', side_effect=lock_operation):
                controller.tick()
            self.assertEqual(calls, [])
            self.assertFalse((self.state / 'spot-prices.json').exists())
            self.assertEqual(held.read(), '')
        controller.tick()
        self.assertEqual(len(calls), 3)

    def test_tick_uses_the_aws_state_directory_when_routing_is_disabled(self) -> None:
        controller, calls = self.gate_price_controller()
        with (mock.patch.object(aws_target, 'ssh_target_state_directory', return_value=self.state),
              mock.patch.object(aws_target, 'AccountRefresh', return_value=controller) as construct,
              mock.patch.object(sys, 'argv', ['aws_target.py', 'tick', '--settings', str(self.settings)]),
              mock.patch('sys.stdout', new_callable=StringIO)):
            self.assertEqual(aws_target.main(), 0)
        self.assertEqual(construct.call_args.args[1], self.state)
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    _ = unittest.main()
