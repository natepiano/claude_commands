"""Measure disposable gate instances from the user's terminal."""
from __future__ import annotations

import argparse
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
import json
import math
import multiprocessing
import sys
from multiprocessing.process import BaseProcess
from pathlib import Path
import signal
import statistics
from types import FrameType
import subprocess
import tempfile
import threading
import time
from typing import Protocol, cast, final
from uuid import uuid4

from scripts.aws_target.gate_box import (
    GUEST as GUEST, SigningKey as SigningKey, presign as presign, user_data as user_data,
    boot_trap as boot_trap, log_push_loop as log_push_loop,
    RUN_FIELDS as RUN_FIELDS, validate_paired_result as validate_paired_result,
)
from scripts.aws_target import admin_secrets, ledger, cloud_gate
from scripts.aws_target.aws_target import AwsCli, AwsUnavailable, execute_command
from scripts.aws_target.config import read_settings, account_settings_path
from scripts.lib import conf
from scripts.build_queue.queue_place import defer_cleanup_signals
from scripts.build_targets import target_config
from scripts.production.gate_modules import GateRefused
from scripts.lib.atomic_file import write_text_atomic
from scripts.production.merge_gate import gate_state_dir, read_measures, SuiteMode


NIXOS_LOCK = Path('/etc/nixos/flake.lock')


def host_nixpkgs_revision() -> str:
    """Read the host's locked nixpkgs input before creating trial resources."""
    lock = ledger.mapping(cast(object, json.loads(NIXOS_LOCK.read_text())))
    nodes = ledger.mapping(lock['nodes'])
    inputs = ledger.mapping(ledger.mapping(nodes[ledger.text(lock, 'root')])['inputs'])
    locked = ledger.mapping(ledger.mapping(nodes[ledger.text(inputs, 'nixpkgs')])['locked'])
    return ledger.text(locked, 'rev')


CLEANUP_ERRORS = (AwsUnavailable, OSError, ValueError, RuntimeError, subprocess.SubprocessError)
TRIAL_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


@contextmanager
def blocked_trial_signals() -> Generator[None]:
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, set(TRIAL_SIGNALS))
    try:
        yield
    finally:
        _ = signal.pthread_sigmask(signal.SIG_SETMASK, previous)


@contextmanager
def trial_signals(cancel: Callable[[], None]) -> Generator[None]:
    def interrupt(_number: int, _frame: FrameType | None) -> None:
        cancel()
    previous = {number: signal.signal(number, interrupt) for number in TRIAL_SIGNALS}
    try:
        yield
    finally:
        for number, handler in previous.items():
            _ = signal.signal(number, handler)


class TrialCancelled(Exception):
    """The user stopped the trial."""


@dataclass(frozen=True)
class TrialSettings:
    region: str
    profile: str
    image: str
    release: str
    subnet: str
    vpc: str
    zone: str
    root_device: str
    root_gib: int
    family: str
    timeout_s: float
    path: Path

    @classmethod
    def read(cls, path: Path) -> TrialSettings:
        settings = conf.read(path)
        return cls(settings.text('region'), settings.text('deployment_profile'), settings.text('ami_id'),
                   settings.text('nixos_release'), settings.text('subnet_id'), settings.text('vpc_id'),
                   settings.text('availability_zone'), settings.text('root_device_name'),
                   settings.whole('root_volume_gib'), settings.text('instance_type').split('.')[0],
                   settings.whole('deployment_timeout_s'), path)


def instance_type(family: str, size: int) -> str:
    return f'{family}.{size // 4}xlarge'


@dataclass(frozen=True)
class Prices:
    compute: float
    disk_gib_month: float
    ipv4_hour: float
    put: float
    get: float
    s3_gib_month: float

    def lifetime(self, seconds: float, root_gib: int) -> float:
        return (self.compute + self.ipv4_hour) * max(60, math.ceil(seconds)) / 3600 + self.disk_gib_month * root_gib * seconds / (730 * 3600)


def product_price(api: AwsCli, service: str, filters: dict[str, str], unit: str) -> float:
    reply = api.call('get-products', '--service-code', service, '--filters', json.dumps([
        {'Type': 'TERM_MATCH', 'Field': key, 'Value': value} for key, value in filters.items()
    ]), service='pricing')
    for encoded in cast(list[str], reply.get('PriceList', [])):
        terms = ledger.mapping(ledger.mapping(cast(object, json.loads(encoded))).get('terms'))
        for term in ledger.mapping(terms.get('OnDemand')).values():
            for dimension in ledger.mapping(ledger.mapping(term).get('priceDimensions')).values():
                row = ledger.mapping(dimension)
                if row.get('unit') == unit and row.get('beginRange') == '0':
                    return float(str(ledger.mapping(row.get('pricePerUnit'))['USD']))
    raise ValueError(f'No {unit} price for {service}: {filters}')


@dataclass
class Attempt:
    size: int
    attempt: int
    method: str
    instance_id: str = ''
    zone: str = ''
    subnet: str = ''
    capacity_refusals: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    log_path: str = ''
    memory_peak_bytes: int | None = None
    clear_refusal: bool = False
    request_at: float = 0
    running_at: float = 0
    ended_at: float = 0
    terminated_at: float = 0
    stopped_at: float = 0
    dollars: float = 0
    verdict: str = 'not run'
    error: str = ''
    result: dict[str, object] = field(default_factory=dict[str, object])
    prices: dict[str, float] = field(default_factory=dict[str, float])
    times: dict[str, float | None] = field(default_factory=dict[str, float | None])


@final
class GateTrial:
    def __init__(self, api: AwsCli, settings: TrialSettings, directory: Path, signing: SigningKey, *,
                 clock: Callable[[], float] = time.time, pause: Callable[[float], object] = time.sleep) -> None:
        self.changed = threading.Condition(threading.RLock())
        self.reservations: dict[tuple[int, int], float] = {}
        self.active: dict[tuple[int, int], int] = {}
        self.recovering: set[tuple[int, int]] = set()
        self.worker_errors: list[BaseException] = []
        self.api = api
        self.settings = settings
        self.directory = directory
        self.signing = signing
        self.clock = clock
        self.pause = pause
        self.run_id = uuid4().hex
        self.bucket = 'gate-trial-' + self.run_id
        self.group = ''
        self.bucket_created = False
        self.remaining_instances: list[str] = []
        self.instance_query_failed = False
        self.group_query_failed = False
        self.launch_reply_lost = False
        self.subnets: list[tuple[str, str]] = []
        self.attempts: list[Attempt] = []
        self.cleanup_errors: list[str] = []
        self.spent = 0.0
        self.s3_cost = 0.0
        self.puts = 0
        self.gets = 0
        self.shared_prices: tuple[float, float, float, float, float] | None = None
        self.commit = ''
        self.object_bytes = 0
        self.interrupted = False
        self.module_seconds: dict[str, float] = {}
        self.nixpkgs_revision = ''

    def cancel(self) -> None:
        with self.changed:
            self.interrupted = True
            self.changed.notify_all()

    def check_cancelled(self) -> None:
        with self.changed:
            if self.interrupted:
                raise TrialCancelled

    def wait_pause(self, seconds: float, *, cancellable: bool = True) -> None:
        with self.changed:
            if cancellable:
                self.check_cancelled()
            if self.pause is time.sleep:
                _ = self.changed.wait(timeout=seconds)
            else:
                _ = self.pause(seconds)
            if cancellable:
                self.check_cancelled()

    def update_s3_cost(self) -> None:
        if self.shared_prices:
            _, _, put, get, _ = self.shared_prices
            self.s3_cost = put * self.puts + get * self.gets

    def projection(self, prices: Prices, step: str) -> float:
        lifetime = conf.read(self.settings.path).whole('gate_trial_lifetime_minutes') * 60
        return (prices.lifetime(lifetime, self.settings.root_gib) * (2 if step == 'start' else 1)
                + prices.put * (5 + (2 if step == 'start' else 1) + math.floor(lifetime / self.option('log_push_s')) * (2 if step == 'start' else 1))
                + prices.get * (lifetime * 2 / self.option('poll_pause_s') + 100))

    def reserve_attempt(self, identity: tuple[int, int], projected: float, reserve: float) -> bool:
        with self.changed:
            _ = self.reservations.pop(identity, None)
            self.changed.notify_all()
            while True:
                self.check_cancelled()
                if self.spent + self.s3_cost + sum(self.reservations.values()) + projected <= reserve:
                    self.reservations[identity] = projected
                    self.changed.notify_all()
                    return True
                if not self.reservations:
                    return False
                _ = self.changed.wait()

    def record(self, attempt: Attempt, *, ended: bool = False) -> None:
        with self.changed:
            peak = attempt.result.get('memory_peak_bytes')
            attempt.memory_peak_bytes = peak if isinstance(peak, int) else None
            identity = (attempt.size, attempt.attempt, attempt.method)
            for index, row in enumerate(self.attempts):
                if (row.size, row.attempt, row.method) == identity:
                    self.attempts[index] = deepcopy(attempt)
                    break
            else:
                self.attempts.append(deepcopy(attempt))
            if ended:
                print(self.row(attempt), flush=True)
            self.save()

    def worker(self, attempt: Attempt, step: str, reserve: float) -> None:
        identity = (attempt.size, attempt.attempt)
        try:
            self.measure(attempt, step, reserve)
        except BaseException as error:
            with self.changed:
                self.worker_errors.append(error)
        finally:
            with self.changed:
                _ = self.active.pop(identity, None)
                _ = self.reservations.pop(identity, None)
                self.recovering.discard(identity)
                self.changed.notify_all()

    def schedule(self, step: str, sizes: Sequence[int], runs: int, reserve: float, vcpus: int) -> None:
        pending = [Attempt(size, number, 'Spot gate' if step == 'size' else 'cold launch')
                   for number in range(1, runs + 1) for size in dict.fromkeys(sizes)]
        projected = {size: self.projection(self.prices(size, step, self.subnets[0][1]), step)
                     for size in sizes if size <= vcpus}
        threads: list[threading.Thread] = []
        try:
            with self.changed:
                while pending or self.active:
                    for attempt in list(pending):
                        reason = ''
                        if self.interrupted:
                            reason = 'The user stopped the trial.'
                        elif self.launch_reply_lost:
                            reason = 'A launch reply remains lost.'
                        elif self.remaining_instances or self.instance_query_failed or self.worker_errors:
                            reason = 'Instance cleanup remains incomplete.'
                        elif attempt.size > vcpus:
                            reason = 'The attempt exceeds --vcpus.'
                        elif self.recovering:
                            continue
                        elif sum(self.active.values()) + attempt.size > vcpus:
                            continue
                        elif self.spent + self.s3_cost + sum(self.reservations.values()) + projected[attempt.size] > reserve:
                            if self.active:
                                continue
                            reason = 'The next fixed lifetime exceeds the step reserve.'
                        if reason:
                            attempt.error = reason
                            self.record(attempt, ended=True)
                        else:
                            identity = (attempt.size, attempt.attempt)
                            self.active[identity] = attempt.size
                            self.reservations[identity] = projected[attempt.size]
                            self.record(attempt)
                            thread = threading.Thread(target=self.worker, args=(attempt, step, reserve))
                            threads.append(thread)
                            # Workers inherit this mask. Only the main thread handles trial signals.
                            with blocked_trial_signals():
                                thread.start()
                        pending.remove(attempt)
                    if self.active:
                        _ = self.changed.wait()
        finally:
            for thread in threads:
                thread.join()
        if self.worker_errors:
            raise self.worker_errors[0]

    def option(self, name: str) -> float:
        return conf.read(self.settings.path).number('gate_trial_' + name)

    def discover_subnets(self) -> None:
        reply = self.api.call('describe-subnets', '--filters', 'Name=vpc-id,Values=' + self.settings.vpc)
        rows = ledger.rows(reply, 'Subnets')
        self.subnets = [(ledger.text(row, 'SubnetId'), ledger.text(row, 'AvailabilityZone'))
                        for row in rows if row.get('SubnetId') == self.settings.subnet]
        zones = {zone for _, zone in self.subnets}
        for row in rows:
            zone = ledger.text(row, 'AvailabilityZone')
            if row.get('MapPublicIpOnLaunch') is True and zone not in zones:
                self.subnets.append((ledger.text(row, 'SubnetId'), zone))
                zones.add(zone)

    def tagged_instances(self, *, stopped: bool = False, attempt: Attempt | None = None) -> list[str]:
        tag = 'Name=tag-key,Values=GateTrial' if stopped else 'Name=tag:GateTrial,Values=' + self.run_id
        states = 'stopping,stopped' if stopped else 'pending,running,stopping,stopped'
        filters = [tag, 'Name=instance-state-name,Values=' + states]
        if attempt is not None:
            filters.append(f'Name=tag:GateTrialAttempt,Values={attempt.size}-{attempt.attempt}')
        reply = self.api.call('describe-instances', '--filters', *filters)
        return [ledger.text(row, 'InstanceId') for reservation in ledger.rows(reply, 'Reservations')
                for row in ledger.rows(reservation, 'Instances')]

    def sweep(self, *, stopped: bool = False, attempt: Attempt | None = None) -> None:
        with self.changed:
            try:
                instances = self.tagged_instances(stopped=stopped, attempt=attempt)
                if attempt is None:
                    self.instance_query_failed = False
            except CLEANUP_ERRORS as error:
                self.instance_query_failed = True
                self.cleanup_errors.append('Instance query: ' + str(error))
                return
            for instance in instances:
                if instance not in self.remaining_instances:
                    self.remaining_instances.append(instance)
                if attempt is not None:
                    attempt.instance_id = instance
                try:
                    if stopped:
                        print('Terminate stopped trial instance: ' + instance, flush=True)
                    self.terminate(instance)
                    if attempt is not None:
                        attempt.terminated_at = self.clock()
                except CLEANUP_ERRORS as error:
                    self.cleanup_errors.append('Terminate: ' + str(error))

    def prices(self, size: int, step: str, zone: str) -> Prices:
        with self.changed:
            pricing = AwsCli(self.api.profile, 'us-east-1', self.api.timeout_s, self.api.execute)
            region = {'regionCode': self.settings.region}
            if self.shared_prices is None:
                disk = product_price(pricing, 'AmazonEC2', {**region, 'volumeApiName': 'gp3'}, 'GB-Mo')
                ip = product_price(pricing, 'AmazonVPC', {**region, 'group': 'VPCPublicIPv4Address'}, 'Hrs')
                put = product_price(pricing, 'AmazonS3', {**region, 'group': 'S3-API-Tier1'}, 'Requests')
                get = product_price(pricing, 'AmazonS3', {**region, 'group': 'S3-API-Tier2'}, 'Requests')
                storage = product_price(pricing, 'AmazonS3', {**region, 'volumeType': 'Standard'}, 'GB-Mo')
                self.shared_prices = (disk, ip, put, get, storage)
            if step == 'size':
                reply = self.api.call('describe-spot-price-history', '--instance-types', instance_type(self.settings.family, size),
                                      '--availability-zone', zone, '--product-descriptions', 'Linux/UNIX',
                                      '--start-time', datetime.fromtimestamp(self.clock(), UTC).isoformat(), '--max-items', '1')
                compute = float(ledger.text(ledger.rows(reply, 'SpotPriceHistory')[0], 'SpotPrice'))
            else:
                compute = product_price(pricing, 'AmazonEC2', {**region, 'instanceType': instance_type(self.settings.family, size),
                    'operatingSystem': 'Linux', 'tenancy': 'Shared', 'preInstalledSw': 'NA', 'capacitystatus': 'Used'}, 'Hrs')
            return Prices(compute, *self.shared_prices)

    def setup(self, bundle: Path) -> None:
        with self.changed:
            arguments = ['--bucket', self.bucket]
            if self.settings.region != 'us-east-1':
                arguments += ['--create-bucket-configuration', json.dumps({'LocationConstraint': self.settings.region})]
            self.bucket_created = True
            _ = self.api.call('create-bucket', *arguments, service='s3api')
            _ = self.api.call('put-public-access-block', '--bucket', self.bucket, '--public-access-block-configuration',
                json.dumps(dict.fromkeys(('BlockPublicAcls', 'IgnorePublicAcls', 'BlockPublicPolicy', 'RestrictPublicBuckets'), True)), service='s3api')
            _ = self.api.call('put-bucket-lifecycle-configuration', '--bucket', self.bucket, '--lifecycle-configuration',
                json.dumps({'Rules': [{'ID': 'trial-expiry', 'Filter': {'Prefix': ''}, 'Status': 'Enabled', 'Expiration': {'Days': 1}}]}), service='s3api')
            _ = self.api.call('put-object', '--bucket', self.bucket, '--key', 'source.bundle', '--body', str(bundle), service='s3api')
            self.puts += 1
            if self.module_seconds:
                timings = bundle.with_name('module_seconds.json')
                _ = timings.write_text(json.dumps(self.module_seconds))
                _ = self.api.call('put-object', '--bucket', self.bucket, '--key', 'module_seconds.json',
                                  '--body', str(timings), service='s3api')
                self.puts += 1
            guest = bundle.with_name('guest.py')
            _ = guest.write_text(GUEST)
            _ = self.api.call('put-object', '--bucket', self.bucket, '--key', 'guest.py', '--body', str(guest), service='s3api')
            self.puts += 1
            reply = self.api.call('create-security-group', '--group-name', 'gate-trial-' + self.run_id,
                                 '--description', 'Disposable gate trial; egress only', '--vpc-id', self.settings.vpc)
            self.group = ledger.text(reply, 'GroupId')
            # A new VPC group has outbound access and no inbound rules.

    def links(self, key: str, step: str) -> dict[str, str]:
        host = f'{self.bucket}.s3.{self.settings.region}.amazonaws.com'
        def url(method: str, name: str) -> str:
            return presign(method, host, name, self.settings.region, self.signing, datetime.fromtimestamp(self.clock(), UTC),
                           conf.read(self.settings.path).whole('gate_trial_url_lifetime_s'))
        return {'step': step, 'commit': self.commit, 'boot': 'cold', 'source': url('GET', 'source.bundle'),
                'guest': url('GET', 'guest.py'),
                **({'module_seconds': url('GET', 'module_seconds.json')} if self.module_seconds else {}),
                **{name: url('PUT', key + '/' + name) for name in ('result', 'log', 'ready-cold', 'ready-warm')}}

    def state(self, instance: str) -> str:
        return self.api.observe(instance, int(self.clock())).state

    def wait_state(self, instance: str, wanted: str, deadline: float) -> None:
        while self.clock() < deadline:
            if wanted != 'terminated':
                self.check_cancelled()
            if self.state(instance) == wanted:
                return
            self.wait_pause(self.option('poll_pause_s'), cancellable=wanted != 'terminated')
        raise TimeoutError(f'Instance did not reach {wanted} before the trial deadline')

    def read_object(self, key: str, destination: Path) -> dict[str, object] | None:
        with self.changed:
            self.gets += 1
        try:
            _ = self.api.call('get-object', '--bucket', self.bucket, '--key', key, str(destination), service='s3api')
        except AwsUnavailable as error:
            if any(marker in str(error) for marker in ('NoSuchKey', 'Not Found', '404')):
                return None
            raise
        return ledger.mapping(cast(object, json.loads(destination.read_text())))

    def wait_result(self, attempt: Attempt, key: str, deadline: float) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as folder:
            while self.clock() < deadline:
                self.check_cancelled()
                row = self.read_object(key, Path(folder) / 'result.json')
                self.check_cancelled()
                if row is not None:
                    return row
                if self.state(attempt.instance_id) == 'terminated':
                    raise RuntimeError('Instance terminated before it wrote its result')
                self.wait_pause(self.option('poll_pause_s'))
        raise TimeoutError('The guest did not publish its result before the trial deadline')

    def terminate(self, instance: str) -> None:
        _ = self.api.call('terminate-instances', '--instance-ids', instance)
        self.wait_state(instance, 'terminated', self.clock() + self.settings.timeout_s)
        with self.changed:
            if instance in self.remaining_instances:
                self.remaining_instances.remove(instance)

    def launch(self, attempt: Attempt, data: Path, step: str) -> None:
        request: dict[str, object] = {
            'ImageId': self.settings.image, 'InstanceType': instance_type(self.settings.family, attempt.size),
            'MinCount': 1, 'MaxCount': 1, 'ClientToken': self.run_id + f'-{attempt.size}-{attempt.attempt}-{attempt.zone}',
            'InstanceInitiatedShutdownBehavior': 'terminate',
            'NetworkInterfaces': [{'DeviceIndex': 0, 'SubnetId': attempt.subnet, 'Groups': [self.group],
                                   'AssociatePublicIpAddress': True, 'DeleteOnTermination': True}],
            'BlockDeviceMappings': [{'DeviceName': self.settings.root_device, 'Ebs': {
                'VolumeSize': self.settings.root_gib, 'VolumeType': 'gp3', 'DeleteOnTermination': True}}],
            'TagSpecifications': [{'ResourceType': 'instance', 'Tags': [
                {'Key': 'GateTrial', 'Value': self.run_id},
                {'Key': 'GateTrialAttempt', 'Value': f'{attempt.size}-{attempt.attempt}'},
                {'Key': 'Name', 'Value': 'gate-trial'}]}],
        }
        if step == 'size':
            request['InstanceMarketOptions'] = {'MarketType': 'spot', 'SpotOptions': {'SpotInstanceType': 'one-time'}}
        self.check_cancelled()
        attempt.request_at = self.clock()
        identity = (attempt.size, attempt.attempt)
        with blocked_trial_signals():
            for retry in range(3):
                try:
                    reply = self.api.call('run-instances', '--cli-input-json', json.dumps(request), '--user-data', 'file://' + str(data))
                except AwsUnavailable:
                    with self.changed:
                        if identity in self.recovering:
                            self.launch_reply_lost = True
                        self.changed.notify_all()
                    raise
                except subprocess.SubprocessError:
                    with self.changed:
                        self.recovering.add(identity)
                        if retry == 2:
                            self.launch_reply_lost = True
                        self.changed.notify_all()
                    if retry == 2:
                        raise
                else:
                    attempt.instance_id = ledger.text(ledger.rows(reply, 'Instances')[0], 'InstanceId')
                    with self.changed:
                        self.recovering.discard(identity)
                        self.record(attempt)
                        self.changed.notify_all()
                    return

    def download_log(self, attempt: Attempt, key: str) -> None:
        destination = self.directory / f'{self.run_id}-{key}.log'
        with self.changed:
            self.gets += 1
        try:
            _ = self.api.call('get-object', '--bucket', self.bucket, '--key', key + '/log',
                              str(destination), service='s3api')
            attempt.log_path = str(destination)
        except AwsUnavailable as error:
            if not any(marker in str(error) for marker in ('NoSuchKey', 'Not Found', '404')):
                raise

    def measure(self, attempt: Attempt, step: str, reserve: float) -> None:
        key = f'{attempt.size}-{attempt.attempt}'
        prices: Prices | None = None
        identity = (attempt.size, attempt.attempt)
        rows = [attempt]
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / 'user-data'
            minutes = conf.read(self.settings.path).whole('gate_trial_lifetime_minutes')
            _ = data.write_text(user_data(self.links(key, step), self.nixpkgs_revision, minutes,
                                         self.option('log_push_s'), self.option('module_limit_s')))
            lifetime = minutes * 60
            try:
                for subnet, zone in self.subnets:
                    attempt.subnet, attempt.zone = subnet, zone
                    prices = self.prices(attempt.size, step, zone)
                    projected = self.projection(prices, step)
                    if not self.reserve_attempt(identity, projected, reserve):
                        attempt.error = 'The next fixed lifetime exceeds the step reserve.'
                        attempt.verdict = 'not run'
                        return
                    attempt.prices = asdict(prices)
                    try:
                        attempt.clear_refusal = False
                        self.launch(attempt, data, step)
                        break
                    except AwsUnavailable as error:
                        with self.changed:
                            unresolved = identity in self.recovering
                        attempt.clear_refusal = not unresolved and (
                            'An error occurred (' in str(error) or 'InsufficientInstanceCapacity' in str(error))
                        attempt.error = str(error)
                        with self.changed:
                            print(f'{zone}: {error}', flush=True)
                        if unresolved or 'InsufficientInstanceCapacity' not in str(error):
                            attempt.verdict = 'error'
                            return
                        attempt.capacity_refusals.append({'zone': zone, 'message': str(error)})
                else:
                    attempt.verdict = 'no capacity'
                    return
                attempt.clear_refusal = False
                attempt.error = ''
                deadline = attempt.request_at + lifetime
                self.wait_state(attempt.instance_id, 'running', deadline)
                attempt.running_at = self.clock()
                attempt.result = self.wait_result(attempt, key + ('/result' if step == 'size' else '/ready-cold'), deadline)
                attempt.verdict = str(attempt.result.get('verdict', 'error'))
                attempt.error = str(attempt.result.get('error', ''))
                attempt.ended_at = self.clock()
                if step == 'start' and attempt.verdict == 'ready':
                    _ = self.api.call('stop-instances', '--instance-ids', attempt.instance_id)
                    self.wait_state(attempt.instance_id, 'stopped', deadline)
                    stopped_at = self.clock()
                    attempt.stopped_at = stopped_at
                    attempt.dollars = prices.lifetime(stopped_at - attempt.request_at, self.settings.root_gib)
                    self.finish_times(attempt)
                    self.record(attempt)
                    warm = Attempt(attempt.size, attempt.attempt, 'stopped start', instance_id=attempt.instance_id,
                                   zone=attempt.zone, subnet=attempt.subnet)
                    warm.prices = asdict(prices)
                    rows.append(warm)
                    attempt = warm
                    self.check_cancelled()
                    attempt.request_at = self.clock()
                    _ = self.api.call('start-instances', '--instance-ids', attempt.instance_id)
                    self.wait_state(attempt.instance_id, 'running', attempt.request_at + lifetime)
                    attempt.running_at = self.clock()
                    attempt.result = self.wait_result(attempt, key + '/ready-warm', attempt.request_at + lifetime)
                    attempt.verdict = str(attempt.result.get('verdict', 'error'))
                    attempt.error = str(attempt.result.get('error', ''))
                    attempt.ended_at = self.clock()
                    attempt.dollars = prices.disk_gib_month * self.settings.root_gib * (attempt.request_at - stopped_at) / (730 * 3600)
            except CLEANUP_ERRORS as error:
                attempt.verdict = 'error'
                attempt.error = str(error)
            except TrialCancelled:
                attempt.verdict = 'cancelled'
            except KeyboardInterrupt:
                self.cancel()
                attempt.verdict = 'cancelled'
            finally:
                with blocked_trial_signals():
                    if not attempt.ended_at:
                        attempt.ended_at = self.clock()
                    try:
                        if attempt.instance_id:
                            self.terminate(attempt.instance_id)
                            attempt.terminated_at = self.clock()
                        elif not attempt.clear_refusal:
                            self.sweep(attempt=attempt)
                    except CLEANUP_ERRORS as error:
                        attempt.verdict = 'cleanup error'
                        attempt.error += '; ' + str(error)
                        with self.changed:
                            self.cleanup_errors.append('Attempt cleanup: ' + str(error))
                            if attempt.instance_id not in self.remaining_instances:
                                self.remaining_instances.append(attempt.instance_id)
                            self.changed.notify_all()
                    if attempt.verdict not in ('pass', 'ready'):
                        try:
                            self.download_log(attempt, key)
                        except CLEANUP_ERRORS as error:
                            attempt.error += '; Log: ' + str(error)
                    if attempt.instance_id and attempt.request_at and prices:
                        billed_end = attempt.terminated_at or self.clock()
                        attempt.dollars += prices.lifetime(billed_end - attempt.request_at, self.settings.root_gib)
                    self.finish_times(attempt)
                    with self.changed:
                        self.spent += sum(row.dollars for row in rows)
                        self.puts += 3 if step == 'size' else 6
                        self.puts += math.floor(lifetime / self.option('log_push_s')) * sum(
                            bool(row.instance_id and row.request_at) for row in rows)
                        _ = self.reservations.pop(identity, None)
                        self.update_s3_cost()
                        for row in rows:
                            self.record(row, ended=True)
                        self.changed.notify_all()

    @staticmethod
    def finish_times(attempt: Attempt) -> None:
        ready = attempt.result.get('tools_ready_at')
        start = attempt.result.get('gate_started_at')
        finish = attempt.result.get('gate_finished_at')
        attempt.times = {
            'request_to_running_s': attempt.running_at - attempt.request_at if attempt.running_at else None,
            'running_to_tools_ready_s': float(ready) - attempt.running_at if isinstance(ready, (int, float)) and attempt.running_at else None,
            'tools_ready_to_gate_start_s': float(start) - float(ready) if isinstance(start, (int, float)) and isinstance(ready, (int, float)) else None,
            'gate_s': float(finish) - float(start) if isinstance(finish, (int, float)) and isinstance(start, (int, float)) else None,
            'teardown_s': (attempt.terminated_at or attempt.stopped_at) - attempt.ended_at if attempt.terminated_at or attempt.stopped_at else None,
            'request_to_ready_s': float(ready) - attempt.request_at if isinstance(ready, (int, float)) else None,
        }

    @staticmethod
    def row(attempt: Attempt) -> str:
        times = ' | '.join('-' if (value := attempt.times.get(key)) is None else f'{value:.2f}' for key in (
            'request_to_running_s', 'running_to_tools_ready_s', 'tools_ready_to_gate_start_s', 'gate_s', 'teardown_s', 'request_to_ready_s'))
        return (f'{attempt.size} | {attempt.attempt} | {attempt.method} | {attempt.zone} | {times} | ${attempt.dollars:.4f} | '
                + f'{attempt.result.get("tests", "-")} | {attempt.result.get("skips", "-")} | {attempt.memory_peak_bytes} | '
                + f'{attempt.result.get("max_running_jobs", "-")} | {attempt.verdict} | '
                + f'{attempt.result.get("cpu_use_percent", "-")} | '
                + f'{attempt.result.get("failed_modules", [])} | {attempt.error.replace(chr(10), "; ")} | {attempt.log_path} | '
                + json.dumps(attempt.capacity_refusals))

    def report(self) -> tuple[dict[str, object], str]:
        with self.changed:
            medians: dict[str, object] = {}
            qualified: list[tuple[float, int]] = []
            for size in sorted({row.size for row in self.attempts}):
                for method in sorted({row.method for row in self.attempts if row.size == size}):
                    measured = [row for row in self.attempts if row.size == size and row.method == method and row.verdict != 'not run']
                    medians[f'{size} {method}'] = {key: statistics.median(values) for key in (
                        'request_to_running_s', 'running_to_tools_ready_s', 'tools_ready_to_gate_start_s', 'gate_s', 'teardown_s', 'request_to_ready_s'
                    ) if (values := [value for row in measured if (value := row.times.get(key)) is not None])}
                size_rows = [row for row in self.attempts if row.size == size and row.method == 'Spot gate']
                if size_rows and all(row.verdict == 'pass' and (row.times.get('gate_s') or math.inf) <= self.option('target_gate_s') for row in size_rows):
                    qualified.append((statistics.median(row.dollars for row in size_rows), size))
            report: dict[str, object] = {'commit': self.commit, 'run_id': self.run_id, 'attempts': [asdict(row) for row in self.attempts],
                'family': self.settings.family, 'medians': medians, 'cheapest_size': min(qualified)[1] if qualified else None, 'Batch': 'not measured',
                'dollars': self.spent + self.s3_cost, 'cleanup_errors': self.cleanup_errors, 'interrupted': self.interrupted, 'remaining': self.remaining(),
                'cost_basis': 'Read prices and observed seconds. Final rows share the S3 cost. Confirm costs with the AWS bill.'}
            text = ('Commit: ' + self.commit + '\nFamily: ' + self.settings.family + '\nsize | attempt | method | zone | request to running | running to tools ready | '
                    + 'tools ready to gate start | gate | teardown | request to ready | dollars | tests | skips | peak machine memory bytes | max_running_jobs | verdict | CPU use percent | failed modules | error | log | capacity refusals\n'
                    + '\n'.join(self.row(row) for row in self.attempts) + '\nMedians:\n'
                    + '\n'.join(name + ': ' + json.dumps(values, sort_keys=True) for name, values in medians.items())
                    + f'\nCheapest size at most {self.option("target_gate_s"):g} s: {report["cheapest_size"]}\nBatch: not measured\n'
                    + f'Total: ${self.spent + self.s3_cost:.4f}\nCleanup: {self.cleanup_errors}\n'
                    + 'Remaining: ' + (', '.join(self.remaining()) or 'none') + '\n')
            return report, text

    def save(self) -> None:
        with self.changed:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            record, content = self.report()
            write_text_atomic(self.directory / f'gate-trial-{self.run_id}.json', json.dumps(record, indent=2) + '\n', new_file_mode=0o600)
            write_text_atomic(self.directory / f'gate-trial-{self.run_id}.txt', content, new_file_mode=0o600)

    def remaining(self) -> list[str]:
        with self.changed:
            return ([f'instance {instance}' for instance in self.remaining_instances]
                    + (['instances unknown'] if self.instance_query_failed else [])
                    + (['launch reply lost; the guest shutdown timer is the protection'] if self.launch_reply_lost else [])
                    + ([f'bucket {self.bucket}'] if self.bucket_created else [])
                    + (['group lookup failed: gate-trial-' + self.run_id] if self.group_query_failed else [])
                    + ([f'group {self.group}'] if self.group else []))

    def delete_group(self) -> None:
        with self.changed:
            deadline = self.clock() + self.option('group_delete_wait_s')
            while True:
                try:
                    _ = self.api.call('delete-security-group', '--group-id', self.group)
                    self.group = ''
                    return
                except AwsUnavailable as error:
                    if 'DependencyViolation' not in str(error) or self.clock() >= deadline:
                        raise
                    _ = self.pause(min(self.option('poll_pause_s'), max(0, deadline - self.clock())))

    def cleanup(self) -> None:
        with self.changed:
            self.sweep()
            if self.bucket_created:
                objects: tuple[dict[str, object], ...] = ()
                try:
                    objects = ledger.rows(self.api.call('list-objects-v2', '--bucket', self.bucket, service='s3api'), 'Contents')
                    self.object_bytes = sum(int(str(row.get('Size', 0))) for row in objects)
                except CLEANUP_ERRORS as error:
                    if 'NoSuchBucket' in str(error):
                        self.bucket_created = False
                    else:
                        self.cleanup_errors.append('List objects: ' + str(error))
                for offset in range(0, len(objects), 1000):
                    try:
                        _ = self.api.call('delete-objects', '--bucket', self.bucket, '--delete', json.dumps({
                            'Objects': [{'Key': row['Key']} for row in objects[offset:offset + 1000]]}), service='s3api')
                    except CLEANUP_ERRORS as error:
                        self.cleanup_errors.append('Delete objects: ' + str(error))
                try:
                    _ = self.api.call('delete-bucket', '--bucket', self.bucket, service='s3api')
                    self.bucket_created = False
                except CLEANUP_ERRORS as error:
                    if 'NoSuchBucket' in str(error):
                        self.bucket_created = False
                    else:
                        self.cleanup_errors.append('Bucket: ' + str(error))
            try:
                self.group_query_failed = True
                reply = self.api.call('describe-security-groups', '--filters',
                                      'Name=group-name,Values=gate-trial-' + self.run_id,
                                      'Name=vpc-id,Values=' + self.settings.vpc)
                self.group_query_failed = False
                groups = ledger.rows(reply, 'SecurityGroups')
                self.group = ledger.text(groups[0], 'GroupId') if groups else ''
                if self.group:
                    self.delete_group()
            except CLEANUP_ERRORS as error:
                self.cleanup_errors.append('Group: ' + str(error))

    def run(self, repo: Path, step: str, sizes: Sequence[int], runs: int, reserve: float, vcpus: int) -> int:
        with trial_signals(self.cancel):
            try:
                return self.run_step(repo, step, sizes, runs, reserve, vcpus)
            except KeyboardInterrupt:
                self.cancel()
                return 130

    def run_step(self, repo: Path, step: str, sizes: Sequence[int], runs: int, reserve: float, vcpus: int) -> int:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        began = self.clock()
        bundle_bytes = 0
        try:
            with tempfile.TemporaryDirectory() as folder:
                self.nixpkgs_revision = host_nixpkgs_revision()
                print('Guest nixpkgs revision: ' + self.nixpkgs_revision, flush=True)
                self.module_seconds = read_measures(gate_state_dir() / 'module_seconds.json')
                bundle = Path(folder) / 'source.bundle'
                self.commit = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
                print('Commit: ' + self.commit, flush=True)
                _ = subprocess.run(['git', '-C', str(repo), 'bundle', 'create', str(bundle), 'HEAD'], check=True, capture_output=True)
                bundle_bytes = bundle.stat().st_size
                self.sweep(stopped=True)
                self.discover_subnets()
                self.setup(bundle)
                self.schedule(step, sizes, runs, reserve, vcpus)
        except KeyboardInterrupt:
            self.cancel()
        finally:
            with blocked_trial_signals():
                with self.changed:
                    self.cleanup()
                    if self.shared_prices:
                        _, _, put, get, storage = self.shared_prices
                        self.s3_cost = (put * self.puts + get * self.gets + storage * max(bundle_bytes, self.object_bytes) / 2**30
                                        * max(0, self.clock() - began) / (730 * 3600))
                    charged = [row for row in self.attempts if row.instance_id and row.request_at]
                    for row in charged:
                        row.dollars += self.s3_cost / len(charged)
                    self.save()
                    _, content = self.report()
                    print(content, flush=True)
                    print(self.directory / f'gate-trial-{self.run_id}.json')
                    print(self.directory / f'gate-trial-{self.run_id}.txt')
        return 130 if self.interrupted else 1 if self.cleanup_errors else 0


def main(argv: Sequence[str], settings_path: Path, directory: Path) -> int:
    if '--paired-worker' in argv:
        return paired_worker_main(argv, settings_path, directory)
    if '--base' in argv or '--changed' in argv or '--modules' in argv:
        return paired_main(argv, directory)
    return legacy_main(argv, settings_path, directory)


def legacy_main(argv: Sequence[str], settings_path: Path, directory: Path) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument('--step', choices=('size', 'start'), required=True)
    _ = parser.add_argument('--repo', type=Path, required=True)
    _ = parser.add_argument('--approve-resources', action='store_true', required=True)
    _ = parser.add_argument('--sizes')
    _ = parser.add_argument('--size', type=int)
    _ = parser.add_argument('--runs', type=int)
    _ = parser.add_argument('--vcpus', type=int)
    _ = parser.add_argument('--family')
    class Arguments(argparse.Namespace):
        step: str = ''
        repo: Path = Path()
        sizes: str | None = None
        size: int | None = None
        runs: int | None = None
        vcpus: int | None = None
        family: str | None = None
    args = parser.parse_args(list(argv), namespace=Arguments())
    if args.step == 'start' and args.size is None:
        parser.error('--step start requires --size')
    options = conf.read(settings_path)
    size_list = args.sizes if args.sizes is not None else options.text('gate_trial_default_sizes')
    sizes = list(dict.fromkeys([args.size] if args.size is not None else [int(value) for value in size_list.split(',')]))
    runs = args.runs if args.runs is not None else options.whole('gate_trial_' + args.step + '_runs')
    if any(size not in (8, 16, 32, 64) for size in sizes) or runs <= 0 or (args.vcpus is not None and args.vcpus <= 0):
        parser.error('Use sizes 8, 16, 32, or 64, a positive run count, and positive --vcpus.')
    reserve = options.number('gate_trial_' + args.step + '_reserve_usd')
    policy = read_settings(settings_path)
    costs = ledger.costs_from_evidence(ledger.CostLedger(directory).read_snapshot(), policy, int(time.time()))
    print(f'Today: ${costs.daily.usd}; this month: ${costs.monthly.usd}; step reserve: ${reserve}', flush=True)
    if min(policy.daily_limit_usd - costs.daily.usd, policy.monthly_limit_usd - costs.monthly.usd) < Decimal(str(reserve)):
        print('The remaining cap room is less than the step reserve.')
        return 1
    settings = TrialSettings.read(settings_path)
    if args.family is not None:
        settings = replace(settings, family=args.family)
    admin = admin_secrets.AdminSecretSession(settings_path)
    credentials = admin.get()
    api = AwsCli(settings.profile, settings.region, settings.timeout_s, admin.executor(execute_command))
    try:
        vcpus = args.vcpus
        if vcpus is None:
            try:
                reply = api.call('get-service-quota', '--service-code', 'ec2', '--quota-code', 'L-34B43A08', service='service-quotas')
                quota = float(str(ledger.mapping(reply.get('Quota'))['Value']))
            except (AwsUnavailable, OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                print('gate-trial: ' + admin.redact(str(error)))
                print('Pass --vcpus N to set the trial limit.')
                return 1
            box_vcpus = int(options.text('instance_type').split('.')[1].removesuffix('xlarge')) * 4
            vcpus = int(quota) - box_vcpus
            print(f'Spot quota: {quota:g} vCPUs; trial limit: {vcpus} vCPUs', flush=True)
        else:
            print(f'Spot quota: override; trial limit: {vcpus} vCPUs', flush=True)
        return GateTrial(api, settings, directory, SigningKey(credentials.aws_access_key_id, credentials.aws_secret_access_key)).run(
            args.repo.resolve(), args.step, sizes, runs, reserve, vcpus)
    except (AwsUnavailable, OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print('gate-trial: ' + admin.redact(str(error)))
        return 1


@dataclass(frozen=True)
class PushedRevision:
    commit: str
    tree: str


@dataclass(frozen=True)
class PairedTrialRequest:
    base: PushedRevision
    changed: PushedRevision
    modules: tuple[str, ...]
    rounds: int
    clone_url: str


def checked_git(repo: Path, *arguments: str) -> str:
    return subprocess.check_output(['git', '-C', str(repo), *arguments], text=True, stderr=subprocess.PIPE).strip()


def pushed_request(repo: Path, base: str, changed: str, modules: Sequence[str], rounds: int) -> PairedTrialRequest:
    """Resolve public history without changing the source checkout."""
    from urllib.parse import urlsplit
    if rounds <= 0 or not modules or len(set(modules)) != len(modules):
        raise ValueError('Use positive rounds and distinct modules')
    for module in modules:
        path = Path(module)
        if path.is_absolute() or '..' in path.parts or len(path.parts) != 2 or not path.name.endswith('.py'):
            raise ValueError('Use modules as <folder>/<file.py>')
    url = cloud_gate.settings().text('clone_url')
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query:
        raise ValueError('The clone URL must contain no credential')
    with tempfile.TemporaryDirectory(prefix='paired-history-') as temporary:
        public = Path(temporary) / 'public.git'
        _ = subprocess.run(['git', 'clone', '--bare', url, str(public)], check=True, capture_output=True)
        revisions: list[PushedRevision] = []
        for ref in (base, changed):
            commit = checked_git(repo, 'rev-parse', '--verify', ref + '^{commit}')
            if not checked_git(public, 'for-each-ref', '--contains=' + commit, '--format=%(refname)'):
                raise ValueError('Both revisions must be pushed refs')
            tree = checked_git(public, 'rev-parse', commit + '^{tree}')
            for module in modules:
                if checked_git(public, 'cat-file', '-t', commit + ':scripts/' + module) != 'blob':
                    raise ValueError('The module is absent from a requested tree')
            revisions.append(PushedRevision(commit, tree))
    return PairedTrialRequest(revisions[0], revisions[1], tuple(modules), rounds, url)


def module_request(trial: PairedTrialRequest, module: str, attempt: str, expires: int) -> dict[str, object]:
    return {'task': 'paired', 'attempt': attempt, 'base': asdict(trial.base), 'changed': asdict(trial.changed),
            'commit': trial.changed.commit, 'tree': trial.changed.tree, 'head': trial.changed.commit,
            'origin': trial.clone_url, 'clone_url': trial.clone_url, 'mode': 'targeted-test',
            'module': module, 'modules': ['scripts/' + module], 'rounds': trial.rounds, 'source_key': '',
            'expires': expires, 'module_time_limit_s': cloud_gate.settings().number('module_time_limit_s'),
            'delivery_s': cloud_gate.settings().number('termination_timeout_s') + 2 * cloud_gate.settings().number('api_timeout_s')}


def module_summary(module: str, rounds: int, runs: Sequence[dict[str, object]]) -> dict[str, object]:
    def median(variant: str, field: str) -> float | None:
        values = [float(str(row[field])) for row in runs if row['variant'] == variant and row.get(field) is not None]
        return statistics.median(values) if values else None
    pairs = {(str(row['variant']), int(str(row['round']))): row for row in runs}
    savings: list[float] = []
    for number in range(1, rounds + 1):
        base, changed = pairs.get(('base', number), {}), pairs.get(('changed', number), {})
        if base.get('cpu_s') is not None and changed.get('cpu_s') is not None:
            savings.append(float(str(base['cpu_s'])) - float(str(changed['cpu_s'])))
    complete = len(pairs) == 2 * rounds
    counts = {row.get('tests') for row in runs}
    return {'module': module, 'rounds': rounds, 'base_median_cpu_s': median('base', 'cpu_s'),
            'changed_median_cpu_s': median('changed', 'cpu_s'),
            'paired_median_saving_cpu_s': statistics.median(savings) if savings else None,
            'base_median_peak_mib': median('base', 'peak_mib'), 'changed_median_peak_mib': median('changed', 'peak_mib'),
            'same_test_count': complete and len(counts) == 1 and None not in counts,
            'all_passed': complete and all(row['status'] == 0 and row.get('tests') is not None
                                          and row.get('failures') == 0 and row.get('errors') == 0 for row in runs)}


def reserve_trial(worker: cloud_gate.GateWorker, requests: Sequence[dict[str, object]],
                  record_query: Callable[[Decimal], None] = lambda _fee: None) -> None:
    """Reserve every box before the coordinator permits any launch."""
    worker.reconcile()
    worker.charges(record_query)
    fallback = Decimal(str(cloud_gate.settings().number('spot_max_usd_per_hour')))
    readings = worker.book.read_bills()
    rate, reserved_type = max((ledger.box_rate(readings, size, fallback, int(worker.clock())).usd_per_hour, size)
                              for size in cloud_gate.settings().words('instance_types'))
    policy = read_settings(worker.settings_path)
    state = worker.book.read_snapshot()
    now = int(worker.clock())
    costs = ledger.costs_from_evidence(state, policy, now)
    daily_future, monthly_future = ledger.gate_future_costs(state, policy, now)
    daily_room = policy.daily_limit_usd - costs.daily.usd - daily_future - policy.limit_reserve_usd
    monthly_room = policy.monthly_limit_usd - costs.monthly.usd - monthly_future - policy.limit_reserve_usd
    estimate = sum((ledger.box_cost(ledger.BoxRate(rate, 'trial reserve'), ledger.instant(row, 'expires') - now,
                   policy.box_minimum_seconds) + Decimal(str(cloud_gate.settings().number('attempt_allowance_usd')))
                    for row in requests), Decimal(0))
    print(f'Paired trial estimate: ${estimate}; daily room: ${daily_room}; monthly room: ${monthly_room}', file=sys.stderr)
    reserved: list[str] = []
    try:
        for request in requests:
            attempt = ledger.text(request, 'attempt')
            # Use the highest rate until AWS confirms the actual type.
            reply = worker.book.reserve_gate(worker.settings_path, ledger.GateReservation(
                attempt, int(worker.clock()), ledger.instant(request, 'expires'), fallback,
                Decimal(str(cloud_gate.settings().number('attempt_allowance_usd'))), instance_type=reserved_type))
            if isinstance(reply, ledger.GateCapReached):
                raise GateRefused(reply.reason)
            reserved.append(attempt)
    except BaseException:
        for attempt in reserved:
            worker.book.resolve_gate(attempt, ledger.GateAbsent(int(worker.clock())))
        raise


def trial_cost(book: ledger.CostLedger, attempts: set[str], settings_path: Path, now: int) -> tuple[Decimal, bool]:
    """Price only this trial; retain uncertain launch costs."""
    policy = read_settings(settings_path)
    total = Decimal(0)
    complete = True
    for reservation in book.read_snapshot().gate_reservations:
        if reservation.attempt not in attempts or isinstance(reservation.state, ledger.GateAbsent):
            continue
        confirmed = isinstance(reservation.state, ledger.GateTerminated)
        complete = complete and confirmed
        end = reservation.state.at if isinstance(reservation.state, ledger.GateTerminated) else max(now, reservation.expires)
        total += ledger.box_cost(reservation.rate, end - reservation.at, policy.box_minimum_seconds) + reservation.allowance_usd
    return total, complete


def stop_paired(number: int, _frame: FrameType | None) -> None:
    raise TrialCancelled(number)


class LaunchBarrier(Protocol):
    def send(self, obj: object, /) -> None: ...
    def recv(self) -> object: ...
    def close(self) -> None: ...


def paired_module(folder: Path, directory: Path, settings_path: Path, connection: LaunchBarrier) -> None:
    """Keep one queue slot while the shared worker owns its box."""
    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        _ = signal.signal(number, stop_paired)
    request = cloud_gate.read(folder / 'request.json')
    policy = conf.read(settings_path)
    worker = cloud_gate.GateWorker(AwsCli(policy.text('credentials_profile'), policy.text('region'),
        cloud_gate.settings().number('api_timeout_s')), ledger.CostLedger(directory), settings_path, folder / 'reservation.cgroup', clock=time.time)
    try:
        with redirect_stdout(sys.stderr), cloud_gate.cloud_reservation(folder, worker.sidecar, SuiteMode.TARGETED_TEST) as slot:
            try:
                connection.send('ready')
                if connection.recv() != 'launch':
                    return
                if (folder / 'outcome.json').exists():
                    return
                outcome = worker.execute(folder, request, cloud_gate.read(folder / 'deployment.json'))
                cloud_gate.save(folder / 'outcome.json', outcome)
                slot.status = int(outcome.get('status') != 'complete')
            finally:
                with defer_cleanup_signals():
                    launch_path = folder / 'launch.json'
                    launch = cloud_gate.read(launch_path) if launch_path.exists() else {}
                    if launch.get('instance'):
                        instance = ledger.text(launch, 'instance')
                        attempt = ledger.text(request, 'attempt')
                        terminated = any(row.attempt == attempt and isinstance(row.state, ledger.GateTerminated)
                                         and row.state.instance_id == instance
                                         for row in worker.book.read_snapshot().gate_reservations)
                        if not terminated:
                            slot.status = 1
                            worker.terminate(attempt, instance)
    except BaseException as error:
        cloud_gate.save(folder / 'outcome.json', {'status': 'fallback', 'reason': str(error)})
    finally:
        connection.close()


def coordinate_trial(folder: Path, directory: Path, settings_path: Path) -> int:
    """Launch all modules after every slot and cost reservation succeeds."""
    from scripts.aws_target.aws_target import control_lock
    policy = conf.read(settings_path)
    book = ledger.CostLedger(directory)
    worker = cloud_gate.GateWorker(AwsCli(policy.text('credentials_profile'), policy.text('region'),
        cloud_gate.settings().number('api_timeout_s')), book, settings_path, folder / 'reservation.cgroup', clock=time.time)
    requests = [cloud_gate.read(path) for path in sorted(folder.glob('*/request.json'))]
    children: list[tuple[BaseProcess, LaunchBarrier]] = []
    previous = {number: signal.signal(number, stop_paired) for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    failed = False
    fee_path = folder / 'query-cost.json'
    query_fee = Decimal(str(cloud_gate.read(fee_path)['usd'])) if fee_path.exists() else Decimal(0)
    def record_query(fee: Decimal) -> None:
        nonlocal query_fee
        query_fee += fee
        cloud_gate.save(fee_path, {'usd': str(query_fee)})
    try:
        with control_lock(directory / 'paired-trial-slots'):
            context = multiprocessing.get_context('fork')
            for request in requests:
                parent, child = context.Pipe()
                process = context.Process(target=paired_module,
                    args=(folder / ledger.text(request, 'attempt'), directory, settings_path, child))
                process.start()
                child.close()
                children.append((process, parent))
            for _process, connection in children:
                if connection.recv() != 'ready':
                    raise GateRefused('The module has no cloud slot')
            reserve_trial(worker, requests, record_query)
            for _process, connection in children:
                connection.send('launch')
        for process, _connection in children:
            process.join()
    except (OSError, ValueError, EOFError, GateRefused, AwsUnavailable, subprocess.SubprocessError,
            TrialCancelled, KeyboardInterrupt) as error:
        print('paired trial: ' + str(error), file=sys.stderr)
        failed = True
    finally:
        with defer_cleanup_signals():
            for process, connection in children:
                if process.is_alive():
                    process.terminate()
                connection.close()
            for process, _connection in children:
                process.join()
            for number, handler in previous.items():
                _ = signal.signal(number, handler)
    summaries: list[dict[str, object]] = []
    cursor = folder / 'emitted.json'
    emitted = cloud_gate.read(cursor) if cursor.exists() else {}
    for request in requests:
        attempt = ledger.text(request, 'attempt')
        path = folder / attempt
        runs: list[dict[str, object]] = []
        if (path / 'remote-result.json').exists():
            row = cloud_gate.read(path / 'remote-result.json')
            launch = cloud_gate.read(path / 'launch.json')
            try:
                validate_paired_result(row, request, ledger.text(launch, 'instance'))
                runs = list(ledger.rows(row, 'runs'))
            except (ValueError, KeyError, TypeError) as error:
                print('paired trial: ' + str(error), file=sys.stderr)
        if not runs and (path / 'gate.log').exists():
            # A stopped guest can leave complete samples before its final reply.
            for line in (path / 'gate.log').read_text().splitlines():
                try:
                    sample = ledger.mapping(cast(object, json.loads(line)))
                    partial = {**request, 'runs': [*runs, sample], 'verdict': 'fail'}
                    launch = cloud_gate.read(path / 'launch.json')
                    validate_paired_result(partial, request, ledger.text(launch, 'instance'))
                    runs.append(sample)
                except (ValueError, KeyError, TypeError):
                    continue
        for sample in runs:
            identity = f"{attempt}:{sample['variant']}:{sample['round']}"
            if identity not in emitted:
                print(json.dumps(sample), flush=True)
                emitted[identity] = True
                cloud_gate.save(cursor, emitted)
        summary = module_summary(ledger.text(request, 'module'), ledger.instant(request, 'rounds'), runs)
        failed = failed or summary['all_passed'] is not True
        summaries.append(summary)
        launch_path = path / 'launch.json'
        launch = cloud_gate.read(launch_path) if launch_path.exists() else {}
        if 'arguments' not in launch:
            book.resolve_gate(attempt, ledger.GateAbsent(int(time.time())))
    for summary in summaries:
        print(json.dumps(summary), flush=True)
    cost, cleanup = trial_cost(book, {ledger.text(row, 'attempt') for row in requests}, settings_path, int(time.time()))
    cost += query_fee
    print(json.dumps({'total_cost_usd': float(cost), 'cost_basis': 'estimate', 'cleanup_complete': cleanup}), flush=True)
    return int(failed or not cleanup)


def paired_worker_main(argv: Sequence[str], settings_path: Path, directory: Path) -> int:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument('--paired-worker', type=Path, required=True)
    class WorkerArguments(argparse.Namespace):
        paired_worker: Path = Path()
    args = parser.parse_args(list(argv), namespace=WorkerArguments())
    return coordinate_trial(args.paired_worker, directory, settings_path)


def paired_service(command: Sequence[str], unit: str) -> int:
    """Stop the owning service when the terminal receives a signal."""
    previous = {number: signal.signal(number, stop_paired) for number in TRIAL_SIGNALS}
    try:
        return subprocess.call(command)
    except (TrialCancelled, KeyboardInterrupt):
        with defer_cleanup_signals():
            _ = subprocess.run(['systemctl', '--user', 'stop', unit], check=False)
        return 1
    finally:
        for number, handler in previous.items():
            _ = signal.signal(number, handler)


def paired_main(argv: Sequence[str], directory: Path) -> int:
    parser = argparse.ArgumentParser(description='Compare two pushed revisions on disposable gate boxes')
    _ = parser.add_argument('--base', required=True)
    _ = parser.add_argument('--changed', required=True)
    _ = parser.add_argument('--modules', nargs='+', required=True)
    _ = parser.add_argument('--rounds', type=int, default=3)
    _ = parser.add_argument('--repo', type=Path, default=Path.home() / '.claude')
    class PairedArguments(argparse.Namespace):
        base: str = ''
        changed: str = ''
        modules: list[str] = []
        rounds: int = 3
        repo: Path = Path()
    args = parser.parse_args(list(argv), namespace=PairedArguments())
    try:
        modules = args.modules
        if len(modules) > cloud_gate.settings().whole('slots'):
            raise GateRefused('The trial needs more cloud slots than configured')
        trial = pushed_request(args.repo, args.base, args.changed, modules, args.rounds)
        deployment = cloud_gate.read(directory / 'gate-deployment.json')
        folder = directory / 'paired-trials' / uuid4().hex
        folder.mkdir(parents=True)
        expires = int(time.time()) + cloud_gate.settings().whole('lifetime_minutes') * 60
        for module in trial.modules:
            path = folder / uuid4().hex
            path.mkdir()
            cloud_gate.save(path / 'request.json', module_request(trial, module, path.name, expires))
            cloud_gate.save(path / 'deployment.json', deployment)
        command = cloud_gate.service_command('scripts.aws_target.gate_trial', ['--paired-worker', str(folder)],
                                             'cloud-paired-trial-' + folder.name, paired=True)
        return paired_service(command, 'cloud-paired-trial-' + folder.name)
    except (OSError, ValueError, GateRefused, subprocess.SubprocessError) as error:
        print('paired trial: ' + str(error), file=sys.stderr)
        print(json.dumps({'total_cost_usd': 0, 'cost_basis': 'estimate', 'cleanup_complete': True}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:], account_settings_path(), target_config.ssh_target_state_directory('aws')))
