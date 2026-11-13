#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Refresh regional gate prices and account charges."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
import fcntl
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Protocol, cast, final, get_args

from scripts.aws_target import ledger, measurement
from scripts.aws_target.config import gate_deployment, read_settings
from scripts.build_targets.target_config import ssh_target_state_directory
from scripts.lib import conf


class AwsExecutor(Protocol):
    def __call__(self, arguments: Sequence[str], *, capture_output: bool, text: bool,
                 timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]: ...


def execute_command(arguments: Sequence[str], *, capture_output: bool, text: bool,
                    timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check, env=env)


class AwsUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class InstanceEvidence:
    instance_id: str
    generation: str
    state: ledger.InstanceState
    observed_at: int


@final
class AwsCli:
    def __init__(self, profile: str, region: str, timeout_s: float,
                 execute: AwsExecutor = execute_command) -> None:
        self.profile = profile
        self.region = region
        self.timeout_s = timeout_s
        self.execute = execute

    def in_region(self, region: str) -> AwsCli:
        """Keep the executor and credentials when selecting a region."""
        return self if region == self.region else AwsCli(self.profile, region, self.timeout_s, self.execute)

    def call(self, operation: str, *arguments: str, service: str = 'ec2',
             timeout_s: float | None = None) -> dict[str, object]:
        result = self.execute(['aws', '--profile', self.profile, '--region', self.region,
                               service, operation, *arguments, '--output', 'json'], capture_output=True, text=True,
                              timeout=self.timeout_s if timeout_s is None else timeout_s, check=False)
        if result.returncode:
            raise AwsUnavailable(result.stderr.strip() or f'{operation}: failed')
        if not result.stdout.strip() or service == 'cloudformation' and operation == 'deploy':
            return {}
        raw = cast(object, json.loads(result.stdout))
        if not isinstance(raw, dict):
            raise AwsUnavailable(f'{operation}: invalid reply')
        return cast(dict[str, object], raw)

    def observe(self, instance_id: str, now: int) -> InstanceEvidence:
        row = self.call('describe-instances', '--instance-ids', instance_id)
        try:
            reservations = cast(list[dict[str, object]], row['Reservations'])
            instances = [instance for reservation in reservations
                         for instance in cast(list[dict[str, object]], reservation['Instances'])]
            if len(instances) != 1 or instances[0].get('InstanceId') != instance_id:
                raise AwsUnavailable('instance identity does not match')
            instance = instances[0]
            state = cast(dict[str, object], instance['State']).get('Name')
            generation = instance.get('LaunchTime')
            if state not in get_args(ledger.InstanceState) or state == 'unknown' or not isinstance(generation, str) or not generation:
                raise AwsUnavailable('instance lifecycle evidence is incomplete')
            return InstanceEvidence(instance_id, f'{instance_id}:{generation}', cast(ledger.InstanceState, state), now)
        except (KeyError, TypeError, AttributeError, IndexError) as error:
            raise AwsUnavailable('instance lifecycle reply is malformed') from error


def largest_gate_vcpus(instance_types: tuple[str, ...]) -> int:
    """Return the largest known count, or zero when all types have unknown counts."""
    largest = 0
    for name in instance_types:
        size = re.fullmatch(r'[^.]+\.(large|([1-9][0-9]*)?xlarge)', name)
        if size:
            count = 2 if size[1] == 'large' else 4 * int(size[2] or '1')
            largest = max(largest, count)
    return largest


USAGE_ONLY = json.dumps({'Not': {'Dimensions': {'Key': 'RECORD_TYPE', 'Values': ['Credit', 'Refund']}}})


def fetch_account_charges(api: AwsCli, start: int, end: int, now: int,
                          read_usd: Decimal) -> tuple[ledger.ChargeRead, ...]:
    """Read daily account costs by usage type and count every API page."""
    period = f'Start={datetime.fromtimestamp(start, UTC):%Y-%m-%d},End={datetime.fromtimestamp(end, UTC):%Y-%m-%d}'
    days: dict[str, ledger.ChargeRead] = {}
    token = ''
    pages = 0
    while True:
        arguments = ['--time-period', period, '--granularity', 'DAILY', '--metrics', 'UnblendedCost', 'UsageQuantity',
                     '--group-by', 'Type=DIMENSION,Key=USAGE_TYPE', '--filter', USAGE_ONLY, '--no-paginate']
        if token:
            arguments.extend(('--next-page-token', token))
        reply = api.call('get-cost-and-usage', *arguments, service='ce')
        items = ledger.rows(reply, 'ResultsByTime')
        if not items:
            raise ValueError('Cost Explorer daily reply is unavailable')
        pages += 1
        for item in items:
            bounds = ledger.mapping(item['TimePeriod'])
            day = ledger.text(bounds, 'Start')
            usage: list[ledger.UsageTypeCost] = []
            for group in ledger.rows(item, 'Groups'):
                keys = cast(list[str], group['Keys'])
                metrics = ledger.mapping(group['Metrics'])
                cost = ledger.mapping(metrics['UnblendedCost'])
                quantity = ledger.mapping(metrics['UsageQuantity'])
                if cost.get('Unit') != 'USD':
                    raise ValueError('Cost Explorer requires USD')
                usage.append(ledger.UsageTypeCost(keys[0], conf.decimal(ledger.text(cost, 'Amount')),
                                                  conf.decimal(ledger.text(quantity, 'Amount')), ledger.text(quantity, 'Unit')))
            previous = days.get(day)
            combined: dict[str, ledger.UsageTypeCost] = {}
            for row in (*previous.usage_types, *usage) if previous else usage:
                old = combined.get(row.usage_type, ledger.UsageTypeCost(row.usage_type, Decimal(0)))
                combined[row.usage_type] = replace(row, usd=old.usd + row.usd, quantity=old.quantity + row.quantity)
            types = tuple(row for _, row in sorted(combined.items()))
            days[day] = ledger.ChargeRead(day, now,
                int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp()),
                int(datetime.fromisoformat(ledger.text(bounds, 'End')).replace(tzinfo=UTC).timestamp()),
                sum((row.usd for row in types), Decimal(0)), Decimal(0), Decimal(0), types,
                item['Estimated'] is True or bool(previous and previous.provisional))
        token = str(reply.get('NextPageToken', ''))
        if not token:
            break
    readings = tuple(days[day] for day in sorted(days))
    return (*readings[:-1], replace(readings[-1], query_usd=read_usd * pages))


@contextmanager
def control_lock(directory: Path) -> Generator[None, None, None]:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'controller.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@final
class AccountRefresh:
    def __init__(self, settings_path: Path, directory: Path, *,
                 execute: AwsExecutor = execute_command, clock: Callable[[], float] = time.time) -> None:
        self.settings_path = settings_path
        self.directory = directory
        self.execute = execute
        self.clock = clock

    def tick(self) -> None:
        now = int(self.clock())
        configuration = conf.read(self.settings_path)
        self._gate_prices(configuration, now)
        api = AwsCli(configuration.text('credentials_profile'), configuration.text('region'),
                     configuration.number('aws_timeout_s'), self.execute)
        _ = self.refresh_charges(api, now)

    def _gate_prices(self, configuration: conf.ConfigRead, now: int) -> None:
        """Refresh gate prices even when the former builder route is disabled."""
        manifest = self.directory / 'gate-deployment.json'
        if not manifest.exists():
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / 'spot-prices.lock').open('a+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            _ = lock.seek(0)
            try:
                last = int(lock.read().strip() or '0')
            except ValueError:
                last = 0
            if last and 0 <= now - last < configuration.whole('price_refresh_seconds'):
                return
            _ = lock.seek(0)
            _ = lock.truncate()
            _ = lock.write(str(now))
            lock.flush()
            try:
                values = conf.read_or_repository('cloud_gate')
                home = configuration.text('region')
                deployed = ledger.rows(gate_deployment(manifest, home), 'regions')
                regions = tuple(dict.fromkeys([*values.words('spot_candidate_regions'),
                    *[ledger.text(row, 'region') for row in deployed]]))
                sizes = values.words('instance_types')
                api = AwsCli(configuration.text('credentials_profile'), home, configuration.number('aws_timeout_s'), self.execute)
                prices: list[ledger.RegionalSpotPrice] = []
                for region in regions:
                    raw = api.in_region(region).call('describe-spot-price-history', '--instance-types', *sizes,
                        '--product-descriptions', 'Linux/UNIX', '--start-time', datetime.fromtimestamp(now, UTC).isoformat(),
                        '--no-paginate', '--max-results', '1000')
                    regional_prices = ledger.regional_spot_prices(region, raw, sizes, now)
                    keys = {(row.zone, row.instance_type) for row in regional_prices}
                    zones = {ledger.text(subnet, 'zone') for row in deployed if row['region'] == region
                             for subnet in ledger.rows(row, 'subnets')}
                    if (not regional_prices or set(sizes) - {size for _, size in keys}
                            or any((zone, size) not in keys for zone in zones for size in sizes)):
                        raise ValueError('The regional Spot price reply omits a type or deployed zone')
                    prices.extend(regional_prices)
                ledger.write_spot_prices(self.directory, prices)
            except (AwsUnavailable, OSError, ValueError, KeyError, TypeError, ArithmeticError, subprocess.SubprocessError) as error:
                print('aws_target: gate price read unavailable: ' + type(error).__name__, file=sys.stderr)

    def refresh_charges(self, api: AwsCli, now: int) -> measurement.ChargeRead | measurement.MeasurementUnavailable:

        def fetch(start: int, end: int) -> tuple[ledger.ChargeRead, ...]:
            settings = read_settings(self.settings_path)
            return fetch_account_charges(api, start, end, now, settings.cost_explorer_read_usd)

        return measurement.read_charges(self.settings_path, self.directory, now, fetch)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument('command', choices=('tick',))
    _ = parser.add_argument('--settings', type=Path)
    _ = parser.add_argument('--directory', type=Path)
    args = parser.parse_args(namespace=RefreshArguments())
    AccountRefresh(args.settings, args.directory).tick()
    return 0


class RefreshArguments(argparse.Namespace):
    def __init__(self) -> None:
        super().__init__()
        self.settings: Path = conf.path('aws_target')
        self.directory: Path = ssh_target_state_directory('aws')


if __name__ == '__main__':
    raise SystemExit(main())
