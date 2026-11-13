#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Reconcile Hana lifecycle state and refresh regional prices and account charges."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
import fcntl
import json
from pathlib import Path
import re
import os
import subprocess
import sys
import time
from typing import Protocol, cast, final, get_args

from scripts.aws_target import ledger, measurement
from scripts.aws_target.config import read_settings
from scripts.aws_target.controller_lock import control_lock as control_lock
from scripts.build_targets.target_config import ssh_target_state_directory
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic


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


def spot_quota(api: AwsCli, code: str = 'L-34B43A08') -> Decimal:
    """Read the applied regional Spot quota in vCPUs."""
    reply = api.call('get-service-quota', '--service-code', 'ec2',
                     '--quota-code', code, service='service-quotas')
    value = conf.decimal(str(ledger.mapping(reply['Quota'])['Value']))
    if not value.is_finite() or value < 0:
        raise ValueError('The Spot quota is invalid')
    return value


def fetch_spot_prices(api: AwsCli, sizes: Sequence[str], now: int) -> tuple[ledger.RegionalSpotPrice, ...]:
    """Read every page and select the latest price for each type and zone."""
    reply = api.call('describe-spot-price-history', '--instance-types', *sizes,
        '--product-descriptions', 'Linux/UNIX', '--start-time', datetime.fromtimestamp(now, UTC).isoformat())
    return ledger.regional_spot_prices(api.region, reply, sizes, now)


def read_snapshot(directory: Path) -> dict[str, object]:
    """Read the local price and quota snapshot without creating files."""
    try:
        return ledger.mapping(cast(object, json.loads((directory / 'spot-prices.json').read_text())))
    except FileNotFoundError:
        return {'prices': [], 'regions': []}


def snapshot(settings_path: Path, directory: Path, *, execute: AwsExecutor = execute_command,
             clock: Callable[[], float] = time.time) -> int:
    """Replace successful regional reads and preserve failed regional reads."""
    # Import here because administrator deployment also uses the shared read functions.
    from scripts.aws_target import admin_secrets

    configuration = conf.read(settings_path)
    values = conf.read_or_repository('cloud_gate')
    regions = tuple(dict.fromkeys(values.words('spot_candidate_regions')))
    sizes = tuple(dict.fromkeys((*values.words('instance_types'), *values.words('gpu_instance_types'))))
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'spot-prices.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        now = int(clock())
        _ = lock.seek(0)
        try:
            last = int(lock.read().strip() or '0')
        except ValueError:
            last = 0
        if last and 0 <= now - last < configuration.whole('price_refresh_seconds'):
            return 0
        environment = {key: value for key, value in os.environ.items() if key not in (
            'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_SECURITY_TOKEN',
            'AWS_CONFIG_FILE', 'AWS_PROFILE', 'AWS_DEFAULT_PROFILE')}
        environment['AWS_SHARED_CREDENTIALS_FILE'] = str(admin_secrets.credentials_file())
        environment['AWS_CONFIG_FILE'] = os.devnull
        environment['AWS_EC2_METADATA_DISABLED'] = 'true'

        def scaler_execute(arguments: Sequence[str], *, capture_output: bool, text: bool,
                           timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            _ = env
            return execute(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check, env=environment)

        api = AwsCli(configuration.text('credentials_profile'), configuration.text('region'),
                     configuration.number('aws_timeout_s'), scaler_execute)
        document = read_snapshot(directory)
        prices = list(ledger.rows(document, 'prices'))
        readings = list(ledger.rows(document, 'regions')) if 'regions' in document else []
        status = 0
        for region in regions:
            try:
                target = api.in_region(region)
                regional = fetch_spot_prices(target, sizes, now)
                standard = spot_quota(target)
                gpu = spot_quota(target, 'L-3819A6DF')
                prices = [row for row in prices if row.get('region') != region]
                prices.extend(dict(region=row.region, zone=row.zone, instance_type=row.instance_type,
                    usd_per_hour=str(row.usd_per_hour), read_at=row.read_at) for row in regional)
                readings = [row for row in readings if row.get('region') != region]
                readings.append(dict(region=region, read_at=now, standard_vcpus=str(standard), gpu_vcpus=str(gpu)))
            except (AwsUnavailable, OSError, ValueError, KeyError, TypeError, ArithmeticError, subprocess.SubprocessError) as error:
                print(f'aws_target: snapshot {region} unavailable: {type(error).__name__}', file=sys.stderr)
                status = 1
        write_text_atomic(directory / 'spot-prices.json', json.dumps(
            {'prices': prices, 'regions': readings}, sort_keys=True) + '\n')
        _ = lock.seek(0)
        _ = lock.truncate()
        _ = lock.write(str(now))
        return status


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
        api = AwsCli(configuration.text('credentials_profile'), configuration.text('region'),
                     configuration.number('aws_timeout_s'), self.execute)
        _ = self.refresh_charges(api, now)

    def refresh_charges(self, api: AwsCli, now: int) -> measurement.ChargeRead | measurement.MeasurementUnavailable:

        def fetch(start: int, end: int) -> tuple[ledger.ChargeRead, ...]:
            settings = read_settings(self.settings_path)
            return fetch_account_charges(api, start, end, now, settings.cost_explorer_read_usd)

        return measurement.read_charges(self.settings_path, self.directory, now, fetch)


def controller_tick(settings_path: Path, directory: Path, workload_path: Path, *,
                    lifecycle: Callable[[], object] | None = None, refresh: Callable[[], object] | None = None) -> int:
    """Recover lifecycle state before an independent account refresh attempt."""
    from scripts.aws_target.hana_box import HanaController, ProviderNotInstalled
    status = 0
    controller = HanaController(directory, settings_path, workload_path, ProviderNotInstalled())
    for name, step in (('Hana lifecycle', controller.tick if lifecycle is None else lifecycle),
                       ('account refresh', AccountRefresh(settings_path, directory).tick if refresh is None else refresh)):
        try:
            _ = step()
        except (OSError, ValueError, RuntimeError, ArithmeticError, subprocess.SubprocessError) as error:
            print(f'aws_target: {name} unavailable: {type(error).__name__}', file=sys.stderr)
            status = 1
    return status


def serve(settings_path: Path, directory: Path, workload_path: Path, *,
          run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
          pause: Callable[[float], None] = time.sleep, keep_running: Callable[[], bool] = lambda: True) -> int:
    """Bound each tick in a child process and release all tick locks between attempts."""
    from scripts.aws_target.hana_box import read_settings as lifecycle_settings
    while keep_running():
        settings = lifecycle_settings(conf.read(workload_path))
        try:
            _ = run([sys.executable, str(Path(__file__).resolve()), 'tick', '--settings', str(settings_path),
                '--directory', str(directory), '--hana-settings', str(workload_path)],
                timeout=settings.controller_tick_timeout_s, check=False, text=True)
        except (OSError, subprocess.SubprocessError) as error:
            print(f'aws_target: bounded tick failed: {type(error).__name__}', file=sys.stderr)
        pause(settings.tick_interval_s)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument('command', choices=('tick', 'snapshot', 'serve'))
    _ = parser.add_argument('--settings', type=Path)
    _ = parser.add_argument('--directory', type=Path)
    _ = parser.add_argument('--hana-settings', type=Path)
    args = parser.parse_args(namespace=RefreshArguments())
    if args.command == 'snapshot':
        return snapshot(args.settings, args.directory)
    if args.command == 'serve':
        return serve(args.settings, args.directory, args.hana_settings)
    return controller_tick(args.settings, args.directory, args.hana_settings)


class RefreshArguments(argparse.Namespace):
    def __init__(self) -> None:
        super().__init__()
        self.command: str = ''
        self.settings: Path = conf.path('aws_target')
        self.directory: Path = ssh_target_state_directory('aws')
        self.hana_settings: Path = conf.path('hana_aws')


if __name__ == '__main__':
    raise SystemExit(main())
