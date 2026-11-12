"""Run cloud gates through a scaler service and retain each attempt locally."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import shlex
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Literal, cast, final
from uuid import uuid4

from scripts.aws_target import gate_box, ledger
from scripts.aws_target.aws_target import AwsCli, AwsUnavailable, control_lock, fetch_account_charges
from scripts.aws_target.config import account_settings_path, gate_deployment, gate_regions, money, read_settings
from scripts.build_queue import admission_client, admission_protocol as wire, cloud_progress, progress_profile
from scripts.build_queue.queue_place import defer_cleanup_signals
from scripts.build_targets import target_config
from scripts.buildlog import store
from scripts.lib import conf, git_index
from scripts.lib.process_start import process_start, Started, Unreadable
from scripts.lib.atomic_file import write_text_atomic
from scripts.build_queue import admission_records
from scripts.production import affected_modules
from scripts.production.gate_modules import GateRefused, discover_test_modules
from scripts.production import merge_gate as gate
from scripts.test_times import gate_record


@dataclass(frozen=True)
class LocalFallback:
    reason: str


@dataclass(frozen=True)
class CloudCompleted:
    run: gate.GateRun


@dataclass(frozen=True)
class GateLaunchVariant:
    region: str
    instance_type: str
    subnet_id: str
    zone: str


def settings() -> conf.ConfigRead:
    return conf.read_or_repository('cloud_gate')


def read(path: Path) -> dict[str, object]:
    return ledger.mapping(cast(object, json.loads(path.read_text())))


def save(path: Path, row: dict[str, object]) -> None:
    write_text_atomic(path, json.dumps(row, sort_keys=True) + '\n')


def capacity_error(reason: str) -> bool:
    return any(code in reason for code in ('InsufficientInstanceCapacity', 'InsufficientSpotCapacity',
                                           'MaxSpotInstanceCountExceeded', 'VcpuLimitExceeded', 'SpotMaxPriceTooLow',
                                           '(Unsupported)', '(InvalidInstanceType)', '(InvalidInstanceType.NotFound)'))


def aws_down(reason: str) -> bool:
    return any(code in reason for code in ('Could not connect to the endpoint', 'EndpointConnectionError',
                                           'ServiceUnavailable', 'InternalError', 'RequestTimeout', 'Connection timed out',
                                           'InternalFailure', 'ServerInternal', '(Unavailable)'))


def definite_refusal(reason: str) -> bool:
    return 'An error occurred (' in reason and not aws_down(reason)


def launch_error_code(reason: str) -> str:
    code = re.search(r'\(([A-Za-z0-9_.-]+)\)', reason) or re.match(r'[A-Za-z0-9_.-]+', reason)
    return code[1] if code and code.lastindex else code[0] if code else ''


def log_progress(text: str) -> progress_profile.ProgressCounter | progress_profile.NoCounter:
    """Read module completion lines from the current box log."""
    total = 0
    finished: set[str] = set()
    for line in text.splitlines():
        start = re.fullmatch(r'Runner: (\d+) modules, whole machine, \d+ CPUs', line)
        if start:
            total = int(start[1])
        result = re.fullmatch(r'(\S+) exit=-?\d+ seconds=\d+\.\d+(?: stopped)?', line)
        if result:
            finished.add(result[1])
    return (progress_profile.ProgressCounter('modules', min(len(finished), total), total)
            if total else progress_profile.NoCounter('The box has no module counter yet'))


def log_cloud_progress(text: str, size_vcpus: int, updated_at: float) -> cloud_progress.CloudProgress:
    """Read the current phase and each module's latest result."""
    state: cloud_progress.CloudState = 'setup'
    failed: set[str] = set()
    for line in text.splitlines():
        start = re.fullmatch(r'Runner: \d+ modules, whole machine, (\d+) CPUs', line)
        if start:
            state, size_vcpus = 'running', int(start[1])
        result = re.fullmatch(r'(\S+) exit=(-?\d+) seconds=\d+\.\d+(?: stopped)?', line)
        if result:
            if int(result[2]):
                failed.add(result[1])
            else:
                failed.discard(result[1])
        if line == 'Cloud gate: uploading':
            state = 'uploading'
    return cloud_progress.CloudProgress(state, size_vcpus, len(failed), updated_at)


@dataclass(frozen=True)
class GateTermination:
    attempt: str
    instance: str
    region: str


@dataclass(frozen=True)
class GateCleanupAttempt:
    reservation: ledger.GateReservation
    launch: dict[str, object]
    launch_record_checked: bool


@dataclass(frozen=True)
class UncertainGateLaunch:
    attempt: str
    regions: tuple[str, ...]
    filter: str
    expires: int


def cleanup_folder(folder: Path, worker: GateWorker) -> bool:
    """Clean one gate folder or all module folders in a trial."""
    paths = [folder / 'request.json'] if (folder / 'request.json').exists() else sorted(folder.glob('*/request.json'))
    return cleanup_requests(paths, worker)


def cleanup_requests(paths: Sequence[Path], worker: GateWorker) -> bool:
    """Clean only the attempts in the selected request files."""
    attempts: list[GateCleanupAttempt] = []
    complete = True
    for path in paths:
        try:
            attempt = ledger.text(read(path), 'attempt')
            reservation = next((row for row in worker.book.read_snapshot().gate_reservations if row.attempt == attempt), None)
            if reservation is not None:
                launch_path = path.parent / 'launch.json'
                attempts.append(GateCleanupAttempt(reservation, read(launch_path) if launch_path.exists() else {}, True))
        except (OSError, ValueError, KeyError, TypeError) as error:
            print('gate cleanup: ' + str(error), file=sys.stderr)
            complete = False
    return cleanup_attempts(attempts, worker) and complete


def cleanup_attempts(attempts: Sequence[GateCleanupAttempt], worker: GateWorker) -> bool:
    """Request terminations before discovery and retain unconfirmed bookings."""
    complete = True
    deadline = worker.clock() + settings().number('termination_timeout_s')
    boxes: list[GateTermination] = []
    uncertain: list[UncertainGateLaunch] = []
    for item in attempts:
        reservation, launch = item.reservation, item.launch
        if isinstance(reservation.state, (ledger.GateAbsent, ledger.GateTerminated)):
            continue
        try:
            region = str(launch.get('region', reservation.region or worker.api.region))
            instance = str(launch.get('instance', ''))
            if not instance and isinstance(reservation.state, ledger.GateRunning):
                instance = reservation.state.instance_id
            if instance:
                boxes.append(GateTermination(reservation.attempt, instance, region))
            elif 'arguments' in launch:
                token = ledger.text(ledger.mapping(launch['arguments']), 'ClientToken')
                uncertain.append(UncertainGateLaunch(reservation.attempt, (region,),
                    'Name=client-token,Values=' + token, reservation.expires))
            elif item.launch_record_checked:
                worker.book.resolve_gate(reservation.attempt, ledger.GateAbsent(int(worker.clock())))
            else:
                regions = (reservation.region,) if reservation.region else tuple(settings().words('regions'))
                uncertain.append(UncertainGateLaunch(reservation.attempt, regions,
                    'Name=tag:GateAttempt,Values=' + reservation.attempt, reservation.expires))
        except (ValueError, KeyError, TypeError) as error:
            print('gate cleanup: ' + str(error), file=sys.stderr)
            complete = False
    worker.request_terminations(boxes, deadline)
    for launch in uncertain:
        try:
            found = False
            for region in launch.regions:
                remaining = deadline - worker.clock()
                if remaining <= 0:
                    raise AwsUnavailable('The gate cleanup deadline passed')
                reply = worker.regional_api(region).call('describe-instances', '--filters', launch.filter,
                    timeout_s=min(settings().number('api_timeout_s'), remaining))
                instances = [row for group in ledger.rows(reply, 'Reservations') for row in ledger.rows(group, 'Instances')]
                if instances:
                    discovered = [GateTermination(launch.attempt, ledger.text(row, 'InstanceId'), region) for row in instances]
                    boxes.extend(discovered)
                    worker.book.gate_region(launch.attempt, region)
                    worker.request_terminations(discovered, deadline)
                    found = True
                    break
            if not found:
                if worker.clock() < launch.expires:
                    complete = False
                else:
                    worker.book.resolve_gate(launch.attempt, ledger.GateAbsent(int(worker.clock())))
        except (OSError, ValueError, KeyError, TypeError, AwsUnavailable, subprocess.SubprocessError) as error:
            print('gate cleanup: ' + str(error), file=sys.stderr)
            complete = False
    return worker.confirm_terminations(boxes, deadline) and complete


@dataclass
class CloudReservation:
    sidecar: Path
    status: int = 1


@contextmanager
def cloud_reservation(repo: Path, sidecar: Path, mode: gate.SuiteMode) -> Generator[CloudReservation]:
    """Keep the route's cloud slot until its result or fallback returns."""
    pid = os.getpid()
    start = admission_records.process_start_time(pid)
    if start is None:
        raise GateRefused('The cloud route has no live process identity')
    owner, episode, since = wire.Owner(pid, start), uuid4().hex, time.time()
    profile = admission_records.ProgramProfile(mode.value, str(Path(__file__).resolve().parents[2]), str(repo.resolve()),
        admission_records.HistoryPeakSizing(0, admission_records.FirstProgramPeak(0), 0.0))
    def refresh() -> wire.AdmitRequest:
        request = admission_client.step_request([mode.value], str(repo), owner, since, str(sidecar), episode,
            profile, run=wire.RunIdentity(pid, start))
        return replace(request, place=wire.CloudPlace('aws'))
    output = admission_client.WaitOutput()
    reply = admission_client.admit(refresh(), output, refresh)
    if isinstance(reply, wire.Refused):
        raise GateRefused(reply.reason)
    if isinstance(reply, admission_client.LeftQueue):
        raise GateRefused('The cloud route left the admission queue')
    reservation = CloudReservation(sidecar)
    try:
        yield reservation
    finally:
        with defer_cleanup_signals():
            _ = admission_client.release(reply.reservation, reservation.status, reply.cleanup_timing)


@dataclass(frozen=True)
class _ResultNotReceived:
    """No valid final result establishes an earlier box end time."""


_RESULT_NOT_RECEIVED = _ResultNotReceived()


@final
class GateWorker:
    """Own one durable launch, result transfer, and confirmed termination."""
    def __init__(self, api: AwsCli, book: ledger.CostLedger, settings_path: Path, sidecar: Path, *,
                 clock: Callable[[], float] = time.time, pause: Callable[[float], None] = time.sleep) -> None:
        self.api = api
        self.box_region = api.region
        self.apis: dict[str, AwsCli] = {api.region: api}
        self.book = book
        self.settings_path = settings_path
        self.sidecar = sidecar
        self.clock = clock
        self.pause = pause
        self.size_vcpus: int = 0

    def regional_api(self, region: str) -> AwsCli:
        if region not in self.apis:
            self.apis[region] = self.api.in_region(region)
        return self.apis[region]

    def publish_log(self, folder: Path) -> None:
        text = (folder / 'gate.log').read_text()
        counter = log_progress(text)
        if isinstance(counter, progress_profile.ProgressCounter):
            progress_profile.publish_counter(self.sidecar, counter)
        cloud_progress.publish(self.sidecar, log_cloud_progress(text, self.size_vcpus, self.clock()))

    def charges(self, record_query: Callable[[Decimal], None] = lambda _fee: None) -> None:
        now = int(self.clock())
        def fetch(start: int, end: int) -> tuple[ledger.ChargeRead, ...]:
            policy = read_settings(self.settings_path)
            readings = fetch_account_charges(self.api, start, end, now, policy.cost_explorer_read_usd)
            record_query(sum((reading.query_usd for reading in readings), Decimal(0)))
            return readings
        _ = self.book.refresh_charges(self.settings_path, now, fetch, gate_workload=True)

    def upload(self, bucket: str, key: str, path: Path) -> None:
        _ = self.api.call('put-object', '--bucket', bucket, '--key', key, '--body', str(path), service='s3api')

    def download(self, bucket: str, key: str, path: Path) -> bool:
        try:
            _ = self.api.call('get-object', '--bucket', bucket, '--key', key, str(path), service='s3api')
            return True
        except AwsUnavailable as error:
            if any(code in str(error) for code in ('NoSuchKey', '(404)', 'Not Found')):
                return False
            raise

    def terminate(self, attempt: str, instance: str, *,
                  result_seen_at: int | _ResultNotReceived = _RESULT_NOT_RECEIVED) -> None:
        api = self.regional_api(self.box_region)
        _ = api.call('terminate-instances', '--instance-ids', instance)
        deadline = self.clock() + settings().number('termination_timeout_s')
        while self.clock() < deadline:
            if api.observe(instance, int(self.clock())).state == 'terminated':
                ended_at = int(self.clock()) if isinstance(result_seen_at, _ResultNotReceived) else result_seen_at
                self.book.resolve_gate(attempt, ledger.GateTerminated(ended_at, instance))
                return
            self.pause(settings().number('result_poll_s'))
        raise AwsUnavailable('Termination is unconfirmed; the expiry function still owns cleanup')

    def termination_call(self, region: str, operation: str, instances: Sequence[str], deadline: float) -> dict[str, object]:
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise AwsUnavailable('The trial cleanup deadline passed')
        return self.regional_api(region).call(operation, '--instance-ids', *instances,
            timeout_s=min(settings().number('api_timeout_s'), remaining))

    def request_terminations(self, boxes: Sequence[GateTermination], deadline: float) -> None:
        """Request termination without waiting for a final instance state."""
        regions = tuple(dict.fromkeys(box.region for box in boxes))
        for region in regions:
            try:
                _ = self.termination_call(region, 'terminate-instances',
                    [box.instance for box in boxes if box.region == region], deadline)
            except (AwsUnavailable, OSError, subprocess.SubprocessError) as error:
                print('paired cleanup: ' + str(error), file=sys.stderr)

    def confirm_terminations(self, boxes: Sequence[GateTermination], deadline: float) -> bool:
        """Record confirmed terminations under one shared deadline."""
        regions = tuple(dict.fromkeys(box.region for box in boxes))
        pending = list(boxes)
        while pending and self.clock() < deadline:
            for region in regions:
                instances = [box.instance for box in pending if box.region == region]
                if not instances or self.clock() >= deadline:
                    continue
                try:
                    reply = self.termination_call(region, 'describe-instances', instances, deadline)
                    terminated = {ledger.text(row, 'InstanceId') for group in ledger.rows(reply, 'Reservations')
                                  for row in ledger.rows(group, 'Instances')
                                  if ledger.text(ledger.mapping(row['State']), 'Name') == 'terminated'}
                    for box in pending:
                        if box.region == region and box.instance in terminated:
                            self.book.resolve_gate(box.attempt, ledger.GateTerminated(int(self.clock()), box.instance))
                    pending = [box for box in pending if box.region != region or box.instance not in terminated]
                except (AwsUnavailable, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
                    print('paired cleanup: ' + str(error), file=sys.stderr)
            if pending:
                self.pause(max(0, min(settings().number('result_poll_s'), deadline - self.clock())))
        return not pending

    def cleanup_due(self, reservation: ledger.GateReservation) -> bool:
        """Preserve live or unreadable workers before checking expiry."""
        if isinstance(reservation.owner, ledger.GateWorkerOwner):
            current = process_start(reservation.owner.pid)
            return not (isinstance(current, Unreadable)
                or isinstance(current, Started) and current.start == reservation.owner.start)
        return reservation.expires <= int(self.clock())

    def reconcile(self) -> None:
        """Clean expired or dead attempts while preserving live workers."""
        attempts: list[GateCleanupAttempt] = []
        for reservation in self.book.read_snapshot().gate_reservations:
            if isinstance(reservation.state, (ledger.GateAbsent, ledger.GateTerminated)) or not self.cleanup_due(reservation):
                continue
            launch: dict[str, object] = {}
            checked = False
            if isinstance(reservation.owner, ledger.GateWorkerOwner):
                folder = Path(reservation.owner.folder)
                launch_path = folder / 'launch.json'
                checked = (folder / 'request.json').exists()
                launch = read(launch_path) if launch_path.exists() else {}
            attempts.append(GateCleanupAttempt(reservation, launch, checked))
        _ = cleanup_attempts(attempts, self)

    def record_launch(self, request: dict[str, object], size: str, zone: str,
                      result: Literal['launched', 'refused', 'finished'], reason: str = '', *, gate_s: float = 0) -> None:
        error_code = launch_error_code(reason)
        row: dict[str, object] = {'time': self.clock(), 'attempt': request['attempt'], 'instance_type': size,
            'vcpus': self.size_vcpus, 'result': result, 'mode': request['mode'], 'region': self.box_region}
        if result == 'finished':
            row['gate_s'] = gate_s
        else:
            row.update({'availability_zone': zone, 'error_code': error_code, 'reason': reason})
        with store.locked_append(self.book.directory / 'cloud-gate-launches.jsonl') as handle:
            store.append_locked(handle, row)
        if result == 'refused':
            print(f'Cloud gate refused: {size} in {zone}: {error_code}', flush=True)

    def rank_variants(self, variants: Sequence[GateLaunchVariant], mode: str) -> list[GateLaunchVariant]:
        """Prefer available capacity, larger sizes, and the lowest total gate cost."""
        values = settings()
        policy = conf.read(self.settings_path)
        now = self.clock()
        max_age = policy.whole('price_max_age_seconds')
        cooldown = values.whole('capacity_cooldown_seconds')
        try:
            history = ledger.recent_gate_launches(self.book.directory, now - max(max_age, cooldown))
        except OSError:
            history = ()
        recent = [row for row in history if float(str(row['time'])) <= now]
        zone_codes = {'InsufficientInstanceCapacity', 'InsufficientSpotCapacity', 'SpotMaxPriceTooLow'}
        quota_codes = {'MaxSpotInstanceCountExceeded', 'VcpuLimitExceeded'}
        refused = [row for row in recent if row.get('result') == 'refused' and float(str(row['time'])) >= now - cooldown]
        zones = {(str(row.get('region', '')), str(row.get('instance_type', '')), str(row.get('availability_zone', '')))
                 for row in refused if row.get('error_code') in zone_codes}
        regions = {str(row.get('region', '')) for row in refused if row.get('error_code') in quota_codes}
        def cooled(variant: GateLaunchVariant) -> bool:
            return variant.region in regions or (variant.region, variant.instance_type, variant.zone) in zones
        try:
            prices = {(row.region, row.instance_type, row.zone): row.usd_per_hour
                      for row in sorted(ledger.read_spot_prices(self.book.directory), key=lambda row: row.read_at)
                      if 0 <= now - row.read_at <= max_age}
        except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
            prices = {}
        if not any((row.region, row.instance_type, row.zone) in prices for row in variants):
            return sorted(variants, key=cooled)
        durations: list[float] = []
        for row in recent:
            if row.get('result') != 'finished' or row.get('mode') != mode or float(str(row['time'])) < now - max_age:
                continue
            try:
                duration = float(str(row['gate_s']))
                if math.isfinite(duration) and duration > 0:
                    durations.append(duration)
            except (ValueError, KeyError, TypeError):
                continue
        hours = (Decimal(str(statistics.median(durations))) / 3600 if durations
                 else Decimal(values.whole('lifetime_minutes')) / 60)
        transfer = conf.decimal(values.text('remote_region_transfer_usd'))
        def order(item: tuple[int, GateLaunchVariant]) -> tuple[bool, int, bool, Decimal, int]:
            position, variant = item
            size = re.fullmatch(r'[^.]+\.(large|([1-9][0-9]*)?xlarge)', variant.instance_type)
            vcpus = (2 if size[1] == 'large' else 4 * int(size[2] or '1')) if size else 0
            key = (variant.region, variant.instance_type, variant.zone)
            cost = (prices[key] * hours + (transfer if variant.region != self.api.region else Decimal(0))
                    if key in prices else Decimal(0))
            return cooled(variant), -vcpus, key not in prices, cost, position
        return [variant for _, variant in sorted(enumerate(variants), key=order)]

    def launch(self, folder: Path, request: dict[str, object], deployment: dict[str, object]) -> str | LocalFallback:
        path = folder / 'launch.json'
        launch: dict[str, object] = read(path) if path.exists() else {}
        if launch.get('instance'):
            self.box_region = str(launch.get('region', self.api.region))
            return ledger.text(launch, 'instance')
        sizes = settings().words('instance_types')
        variants = [GateLaunchVariant(ledger.text(regional, 'region'), size, ledger.text(row, 'id'), ledger.text(row, 'zone'))
                    for regional in ledger.rows(deployment, 'regions') for size in sizes for row in ledger.rows(regional, 'subnets')]
        if not path.exists():
            variants = self.rank_variants(variants, ledger.text(request, 'mode'))
            launch = {'variant': 0, 'variants': [asdict(row) for row in variants]}
            save(path, launch)
        elif 'variants' in launch:
            variants = [GateLaunchVariant(ledger.text(row, 'region'), ledger.text(row, 'instance_type'),
                ledger.text(row, 'subnet_id'), ledger.text(row, 'zone')) for row in ledger.rows(launch, 'variants')]
        saved_order = {'variants': launch['variants']} if 'variants' in launch else {}
        regional_rows = {ledger.text(row, 'region'): row for row in ledger.rows(deployment, 'regions')}
        index = ledger.instant(launch, 'variant') if launch else 0
        stop = max(index + 1, len(variants)) if launch.get('arguments') else len(variants)
        for offset in range(index, stop):
            if launch.get('arguments') and offset == index:
                arguments = ledger.mapping(launch['arguments'])
                self.box_region = str(launch.get('region', self.api.region))
                size = ledger.text(arguments, 'InstanceType')
                zone = str(launch.get('zone', ''))
            else:
                variant = variants[offset]
                regional = regional_rows[variant.region]
                size = variant.instance_type
                subnet = {'id': variant.subnet_id, 'zone': variant.zone}
                self.box_region = ledger.text(regional, 'region')
                zone = variant.zone
                lifetime = max(1, (ledger.instant(request, 'expires') - int(self.clock()) + 59) // 60)
                prefix = 'artifacts/' + ledger.text(request, 'attempt')
                links = {key: ledger.text(request, key) for key in ('attempt', 'commit', 'tree', 'mode', 'origin')}
                credentials = self.api.call('export-credentials', '--format', 'process', service='configure')
                signing = gate_box.SigningKey(ledger.text(credentials, 'AccessKeyId'), ledger.text(credentials, 'SecretAccessKey'),
                    ledger.text(credentials, 'SessionToken') if 'SessionToken' in credentials else '')
                at = int(self.clock())
                def url(method: str, key: str) -> str:
                    return gate_box.presign(method, ledger.text(deployment, 'bucket') + '.s3.' + self.api.region + '.amazonaws.com',
                        key, self.api.region, signing, datetime.fromtimestamp(at, UTC), max(1, ledger.instant(request, 'expires') - at))
                links.update(production='true', step='size', expires=str(request['expires']),
                             guest=url('GET', 'requests/' + ledger.text(request, 'attempt') + '.guest.py'),
                             request=url('GET', 'requests/' + ledger.text(request, 'attempt') + '.json'),
                             log=url('PUT', prefix + '/gate.log'), result=url('PUT', prefix + '/result.json'),
                             boot_error=url('PUT', prefix + '/boot-error.json'))
                if request['source_key']:
                    links['source'] = url('GET', ledger.text(request, 'source_key'))
                if request.get('task') == 'paired':
                    links['paired'] = 'true'
                policy = read_settings(self.settings_path)
                tags = [{'Key': 'GateDeployment', 'Value': ledger.text(deployment, 'deployment_id')},
                        {'Key': 'Workload', 'Value': 'cloud-claude-gate'}, {'Key': 'Name', 'Value': 'cloud-claude-gate'}, {'Key': 'GateAttempt', 'Value': ledger.text(request, 'attempt')},
                        {'Key': 'ExpiresAt', 'Value': str(request['expires'])},
                        {'Key': policy.cost_explorer_tag_key, 'Value': policy.cost_explorer_tag_value}]
                arguments: dict[str, object] = {
                    'LaunchTemplate': {'LaunchTemplateId': regional['template'], 'Version': regional['version']},
                    'MinCount': 1, 'MaxCount': 1, 'ClientToken': ledger.text(request, 'attempt') + '-' + str(offset),
                    'InstanceType': size,
                    'NetworkInterfaces': [{'DeviceIndex': 0, 'SubnetId': subnet['id'],
                                           'Groups': [regional['security_group']], 'AssociatePublicIpAddress': True}],
                    'TagSpecifications': [{'ResourceType': kind, 'Tags': tags} for kind in ('instance', 'volume')],
                    'UserData': gate_box.user_data({**links, 'instance_type': size}, ledger.text(deployment, 'revision'), lifetime,
                              settings().number('log_push_s'), float(str(request['module_time_limit_s'])))}
                if regional.get('image'):
                    arguments['ImageId'] = ledger.text(regional, 'image')
                launch = {**saved_order, 'variant': offset, 'arguments': arguments, 'region': self.box_region, 'zone': zone}
                save(path, launch)
            api = self.regional_api(self.box_region)
            self.book.gate_region(ledger.text(request, 'attempt'), self.box_region)
            self.size_vcpus = int(ledger.text(arguments, 'InstanceType').split('.')[1].removesuffix('xlarge')) * 4
            cloud_progress.publish(self.sidecar, cloud_progress.CloudProgress('requesting', self.size_vcpus, 0, self.clock()))
            if self.clock() >= ledger.instant(request, 'expires'):
                reply = api.call('describe-instances', '--filters',
                                      'Name=client-token,Values=' + ledger.text(arguments, 'ClientToken'))
                instances = [instance for group in ledger.rows(reply, 'Reservations')
                             for instance in ledger.rows(group, 'Instances')]
                if len(instances) == 1:
                    instance = ledger.text(instances[0], 'InstanceId')
                    save(path, {**launch, 'instance': instance})
                    self.book.resolve_gate(ledger.text(request, 'attempt'), ledger.GateRunning(instance), instance_type=size)
                    return instance
                raise AwsUnavailable('The expired launch remains unconfirmed')
            try:
                for retry in range(settings().whole('launch_reply_tries')):
                    try:
                        reply = api.call('run-instances', '--cli-input-json', json.dumps(arguments))
                        self.record_launch(request, size, zone, 'launched')
                        break
                    except AwsUnavailable as error:
                        self.record_launch(request, size, zone, 'refused', str(error))
                        if not aws_down(str(error)) or retry + 1 == settings().whole('launch_reply_tries'):
                            raise
                    except subprocess.SubprocessError as error:
                        self.record_launch(request, size, zone, 'refused',
                                           type(error).__name__ + ': ' + str(error))
                        raise
                else:
                    raise AssertionError('The launch retry count must be positive')
            except AwsUnavailable as error:
                # A saved request keeps its token and bytes after an uncertain reply.
                if capacity_error(str(error)):
                    launch = {**saved_order, 'variant': offset + 1}
                    save(path, launch)
                    continue
                if not definite_refusal(str(error)):
                    raise
                self.book.resolve_gate(ledger.text(request, 'attempt'), ledger.GateAbsent(int(self.clock())))
                return LocalFallback(f'{size} in {zone}: {launch_error_code(str(error))}')
            instances = ledger.rows(reply, 'Instances')
            if len(instances) != 1:
                raise AwsUnavailable('The launch reply has no unique instance')
            instance = ledger.text(instances[0], 'InstanceId')
            save(path, {**launch, 'instance': instance})
            self.book.resolve_gate(ledger.text(request, 'attempt'), ledger.GateRunning(instance), instance_type=size)
            return instance
        self.book.resolve_gate(ledger.text(request, 'attempt'), ledger.GateAbsent(int(self.clock())))
        return LocalFallback('Spot capacity is unavailable in every configured region')

    def execute(self, folder: Path, request: dict[str, object], deployment: dict[str, object]) -> dict[str, object]:
        attempt = ledger.text(request, 'attempt')
        row: dict[str, object] = {}
        result_seen_at: int | _ResultNotReceived = _RESULT_NOT_RECEIVED
        try:
            cloud_progress.publish(self.sidecar, cloud_progress.CloudProgress('requesting', 0, 0, self.clock()))
            self.reconcile()
            previous = next((row for row in self.book.read_snapshot().gate_reservations if row.attempt == attempt), None)
            if previous is not None and isinstance(previous.state, (ledger.GateAbsent, ledger.GateTerminated)):
                return {'status': 'fallback', 'reason': 'The saved attempt has ended'}
            if not (folder / 'launch.json').exists():
                if request.get('task') != 'paired':
                    self.charges()
                reservation = self.book.reserve_gate(self.settings_path, ledger.GateReservation(
                    attempt, int(self.clock()), ledger.instant(request, 'expires'),
                    money(str(settings().number('spot_max_usd_per_hour'))),
                    money(str(settings().number('attempt_allowance_usd'))),
                    instance_type=settings().words('instance_types')[0]))
                if isinstance(reservation, (ledger.GateCapReached, ledger.GateAttemptEnded)):
                    return {'status': 'fallback', 'reason': reservation.reason}
                identity = process_start(os.getpid())
                if not isinstance(identity, Started):
                    raise ValueError('The worker has no readable process identity')
                self.book.gate_owner(attempt, ledger.GateWorkerOwner(os.getpid(), identity.start, str(folder.resolve())))
                bucket = ledger.text(deployment, 'bucket')
                if request['source_key']:
                    self.upload(bucket, ledger.text(request, 'source_key'), folder / 'source.bundle')
                self.upload(bucket, 'requests/' + attempt + '.json', folder / 'request.json')
                guest = folder / 'guest.py'
                _ = guest.write_text(gate_box.GUEST)
                self.upload(bucket, 'requests/' + attempt + '.guest.py', guest)
            identity = process_start(os.getpid())
            if not isinstance(identity, Started):
                raise ValueError('The worker has no readable process identity')
            self.book.gate_owner(attempt, ledger.GateWorkerOwner(os.getpid(), identity.start, str(folder.resolve())))
            launched = self.launch(folder, request, deployment)
            if isinstance(launched, LocalFallback):
                return {'status': 'fallback', 'reason': launched.reason}
            instance = launched
            prefix = 'artifacts/' + attempt
            try:
                arguments = ledger.mapping(read(folder / 'launch.json')['arguments'])
                self.size_vcpus = int(ledger.text(arguments, 'InstanceType').split('.')[1].removesuffix('xlarge')) * 4
                cloud_progress.publish(self.sidecar, cloud_progress.CloudProgress('setup', self.size_vcpus, 0, self.clock()))
                while self.clock() < ledger.instant(request, 'expires'):
                    try:
                        bucket = ledger.text(deployment, 'bucket')
                        if self.download(bucket, prefix + '/gate.log', folder / 'gate.log'):
                            self.publish_log(folder)
                        if self.download(bucket, prefix + '/boot-error.json', folder / 'boot-error.json'):
                            raise ValueError(ledger.text(read(folder / 'boot-error.json'), 'error'))
                        if self.download(bucket, prefix + '/result.json', folder / 'remote-result.json'):
                            received_at = int(self.clock())
                            if not self.download(bucket, prefix + '/gate.log', folder / 'gate.log'):
                                raise ValueError('The final log is absent')
                            result = read(folder / 'remote-result.json')
                            validate_result(result, request, instance=instance)
                            if request.get('task') == 'paired' and any(
                                    sample['instance_type'] != arguments['InstanceType'] for sample in ledger.rows(result, 'runs')):
                                raise ValueError('The sample instance type differs from the launch')
                            result_seen_at = received_at
                            self.record_launch(request, ledger.text(arguments, 'InstanceType'), '', 'finished',
                                               gate_s=float(str(result['gate_s'])))
                            row = result
                            self.publish_log(folder)
                            break
                        state = self.regional_api(self.box_region).observe(instance, int(self.clock())).state
                        if state not in ('pending', 'running'):
                            reply = self.regional_api(self.box_region).call('describe-instances', '--instance-ids', instance)
                            stopped = ledger.rows(ledger.rows(reply, 'Reservations')[0], 'Instances')[0]
                            reason = ledger.mapping(stopped.get('StateReason', {}))
                            detail = ': '.join(ledger.text(reason, key) for key in ('Code', 'Message') if reason.get(key))
                            raise ValueError('The instance stopped without a final result' + (f' ({detail})' if detail else ''))
                    except subprocess.TimeoutExpired:
                        continue
                    self.pause(settings().number('result_poll_s'))
                if not row:
                    raise ValueError('The instance expired without a final result')
            finally:
                with defer_cleanup_signals():
                    self.terminate(attempt, instance, result_seen_at=result_seen_at)
            return {'status': 'complete', 'instance': instance, 'result': row}
        except (OSError, ValueError, KeyError, TypeError, AwsUnavailable, subprocess.SubprocessError) as error:
            launch = read(folder / 'launch.json') if (folder / 'launch.json').exists() else {}
            # Saved arguments mean AWS may have accepted the launch; a saved variant alone does not.
            if 'arguments' not in launch:
                self.book.resolve_gate(attempt, ledger.GateAbsent(int(self.clock())))
            if row and request.get('task') != 'paired' and not gate_box.decode_run(row['gate']).passed:
                return {'status': 'complete', 'result': row}
            return {'status': 'fallback', 'reason': str(error)}


def validate_result(row: dict[str, object], request: dict[str, object], *, instance: str = '') -> None:
    if request.get('task') == 'paired':
        gate_box.validate_paired_result(row, request, instance)
        return
    for key in ('attempt', 'commit', 'tree'):
        if row.get(key) != request[key]:
            raise ValueError('The result differs from the current ' + key)
    run = gate_box.decode_run(row['gate'])
    if row.get('log_error') or row.get('verdict') != ('pass' if run.passed else 'fail'):
        raise ValueError('The result has no complete verdict and log')
    modules = ['scripts/' + job.suite + '/' + job.module for job in run.jobs]
    if (any(job.kind is not gate.GateJobKind.TEST_MODULE for job in run.jobs)
            or len(modules) != len(set(modules)) or set(modules) != set(cast(list[str], request['modules']))):
        raise ValueError('The result has a different module list')
    host = ledger.text(row, 'host')
    if not host or Path(host).name != host or host in ('.', '..'):
        raise ValueError('Invalid remote host')
    record = ledger.mapping(row['record'])
    if (record.get('id') != 'gate-' + ledger.text(request, 'attempt') or record.get('tree') != request['tree']
            or record.get('sha') != request['commit'] or record.get('host') != row.get('host')
            or record.get('kind') != 'gate_run' or record.get('status') != int(not run.passed)):
        raise ValueError('The buildlog record differs from the result')


def import_record(folder: Path, row: dict[str, object]) -> None:
    record = ledger.mapping(row['record'])
    host = ledger.text(record, 'host')
    if not host or Path(host).name != host or host in ('.', '..'):
        raise ValueError('Invalid remote host')
    at = datetime.fromisoformat(ledger.text(record, 'started_at')).timestamp()
    destination = store.host_file(host, at)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with store.locked_append(destination) as handle:
        _ = handle.seek(0)
        records = [ledger.mapping(cast(object, json.loads(line))) for line in handle if line.endswith(b'\n')]
        if not any(previous.get('id') == record['id'] for previous in records):
            run = gate_box.decode_run(row['gate'])
            gate_record.keep_failure_logs(ledger.text(record, 'id'), host, at,
                cast(list[gate_record.ModuleRun], record['modules']), gate_box.execution_records(run))
            store.append_locked(handle, record)
    save(folder / 'imported.json', {'id': record['id']})


def working_tree(repo: Path) -> tuple[str, set[str]]:
    dirty = affected_modules.dirty_paths(repo)
    if not dirty:
        return gate.checked_git(repo, 'rev-parse', 'HEAD^{tree}'), dirty
    with tempfile.TemporaryDirectory(prefix='cloud-gate-index-') as temporary:
        index = Path(temporary) / 'index'
        git_index.private_copy(Path(gate.checked_git(repo, 'rev-parse', '--path-format=absolute', '--git-path', 'index')), index)
        environment = {**os.environ, 'GIT_INDEX_FILE': str(index)}
        _ = subprocess.run(['git', 'add', '-A'], cwd=repo, env=environment, check=True, capture_output=True)
        tree = subprocess.run(['git', 'write-tree'], cwd=repo, env=environment, check=True, capture_output=True, text=True)
        return tree.stdout.strip(), dirty


def prepare(repo: Path, folder: Path, mode: gate.SuiteMode, limit: float, *,
            selected: gate.AllModules | gate.TreeSelection = gate.ALL_MODULES) -> dict[str, object]:
    head = gate.checked_git(repo, 'rev-parse', 'HEAD')
    tree, dirty = working_tree(repo)
    if isinstance(selected, gate.TreeSelection) and tree != selected.tree:
        raise GateRefused('The checkout changed after the test selection')
    commit = gate.checked_git(repo, 'commit-tree', tree, '-p', head, '-m', 'Cloud gate snapshot') if dirty else head
    modules = discover_test_modules(repo)
    if isinstance(selected, gate.TreeSelection):
        paths = set(selected.paths)
        if not paths <= {str(path.relative_to(repo)) for _, path in modules}:
            raise GateRefused('selected modules are absent from the tested tree')
        modules = [(suite, path) for suite, path in modules if str(path.relative_to(repo)) in paths]
    origin_main = gate.checked_git(repo, 'rev-parse', 'refs/remotes/origin/main')
    origin = gate.checked_git(repo, 'remote', 'get-url', 'origin')
    from urllib.parse import urlsplit
    if urlsplit(origin).password or urlsplit(origin).query or ('://' in origin and urlsplit(origin).username):
        raise GateRefused('The origin URL must contain no credential')
    folder.mkdir(parents=True, exist_ok=True)
    source_key = ''
    if int(gate.checked_git(repo, 'rev-list', '--count', commit, '--not', 'refs/remotes/origin/main')):
        ref = 'refs/cloud-gates/' + folder.name
        _ = gate.checked_git(repo, 'update-ref', ref, commit)
        try:
            _ = gate.checked_git(repo, 'bundle', 'create', str(folder / 'source.bundle'), ref, '--not', 'refs/remotes/origin/main')
        finally:
            _ = gate.checked_git(repo, 'update-ref', '-d', ref)
        digest = sha256((folder / 'source.bundle').read_bytes()).hexdigest()
        source_key = 'source/' + digest + '.bundle'
    request: dict[str, object] = {'attempt': folder.name, 'commit': commit, 'head': head, 'tree': tree, 'dirty_paths': sorted(dirty), 'origin': origin,
        'origin_main': origin_main, 'clone_url': settings().text('clone_url'),
        'mode': mode.value, 'modules': [str(p.relative_to(repo)) for _, p in modules], 'source_key': source_key,
        'expires': int(time.time()) + settings().whole('lifetime_minutes') * 60,
        'module_time_limit_s': min(limit, settings().number('module_time_limit_s'))}
    save(folder / 'request.json', request)
    return request


def run(repo: Path, *, mode: gate.SuiteMode, full: bool, module_time_limit_s: float,
        selected: gate.AllModules | gate.TreeSelection = gate.ALL_MODULES,
        service: Callable[[Sequence[str]], int] = lambda command: subprocess.call(command)) -> LocalFallback | CloudCompleted:
    try:
        return run_cloud(repo, mode=mode, full=full, module_time_limit_s=module_time_limit_s, selected=selected, service=service)
    except (OSError, ValueError, KeyError, TypeError, AwsUnavailable, subprocess.SubprocessError,
            GateRefused, affected_modules.GraphError) as error:
        return LocalFallback(str(error))


def run_cloud(repo: Path, *, mode: gate.SuiteMode, full: bool, module_time_limit_s: float,
              service: Callable[[Sequence[str]], int],
              selected: gate.AllModules | gate.TreeSelection = gate.ALL_MODULES) -> LocalFallback | CloudCompleted:
    directory = target_config.ssh_target_state_directory('aws')
    deployment_path = directory / 'gate-deployment.json'
    if not deployment_path.exists():
        return LocalFallback('image or stack is absent')
    home = conf.read(account_settings_path()).text('region')
    _ = gate_regions(home)
    deployment = gate_deployment(deployment_path, home)
    regional = ledger.rows(deployment, 'regions')[0]
    if not regional.get('image') or not regional.get('template'):
        return LocalFallback('image or stack is absent')
    root = Path(gate.checked_git(repo, 'rev-parse', '--path-format=absolute', '--git-path', 'cloud-gates'))
    with control_lock(root):
        current = root / 'current.json'
        tree, _dirty = working_tree(repo)
        previous = read(current) if current.exists() else {}
        identity: dict[str, object] = {'tree': tree, 'commit': gate.checked_git(repo, 'rev-parse', 'HEAD'),
                                      'mode': mode.value, 'full': full, 'module_time_limit_s': module_time_limit_s,
                                      'modules': list(selected.paths) if isinstance(selected, gate.TreeSelection) else 'all',
                                      'selection_tree': selected.tree if isinstance(selected, gate.TreeSelection) else 'all'}
        same_request = all(previous.get(key) == value for key, value in identity.items())
        folder = root / ledger.text(previous, 'attempt') if same_request else root / uuid4().hex
        book = ledger.CostLedger(directory)
        ended = book.path.exists() and any(
            row.attempt == folder.name and isinstance(row.state, (ledger.GateAbsent, ledger.GateTerminated))
            for row in book.read_snapshot().gate_reservations)
        if ended or (folder / 'outcome.json').exists() and read(folder / 'outcome.json').get('status') != 'pending':
            folder = root / uuid4().hex
        folder.mkdir(parents=True, exist_ok=True)
        sidecar = folder / 'reservation.cgroup'
        command = service_command('scripts.aws_target.cloud_gate',
            ['--worker', str(folder), '--directory', str(directory), '--sidecar', str(sidecar),
             '--settings', str(account_settings_path())], 'cloud-gate-' + folder.name)
        with cloud_reservation(repo, sidecar, mode) as reservation:
            request = (read(folder / 'request.json') if (folder / 'request.json').exists()
                       else prepare(repo, folder, mode, module_time_limit_s, selected=selected))
            save(current, {'attempt': folder.name, **identity})
            save(folder / 'deployment.json', deployment)
            cloud_progress.publish(reservation.sidecar, cloud_progress.CloudProgress('requesting', 0, 0, time.time()))
            outcome = run_service(repo, folder, request, command, service)
            reservation.status = int(not outcome.run.passed) if isinstance(outcome, CloudCompleted) else 0
            return outcome


def service_command(module: str, arguments: Sequence[str], unit: str, *, paired: bool = False) -> list[str]:
    """Use the installed scaler environment for each gate controller."""
    credential = str(Path(settings().text('credential_file')).expanduser())
    configuration = ['--setenv=CONF_DIR=' + os.environ['CONF_DIR']] if 'CONF_DIR' in os.environ else []
    cleanup: list[str] = []
    stop_timeout = settings().number('termination_timeout_s') + 2 * settings().number('api_timeout_s')
    if paired:
        folder = arguments[arguments.index('--paired-worker') + 1]
        command = shlex.join([sys.executable, '-m', module, '--paired-cleanup', folder])
    else:
        cleanup_arguments = ['--cleanup' if value == '--worker' else value for value in arguments]
        command = shlex.join([sys.executable, '-m', module, *cleanup_arguments])
    cleanup = ['--property=ExecStopPost=' + command.replace('%', '%%').replace('$', '$$')]
    return ['systemd-run', '--user', '--wait', '--pipe', '--collect', '--unit=' + unit,
            '--setenv=AWS_SHARED_CREDENTIALS_FILE=' + credential,
            *configuration, *cleanup,
            '--property=KillMode=' + ('mixed' if paired else 'control-group'), '--property=UMask=0077',
            '--property=TimeoutStopSec=' + str(stop_timeout),
            '--property=UnsetEnvironment=AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_CONFIG_FILE AWS_PROFILE',
            '--working-directory=' + str(Path.home() / '.claude'), sys.executable, '-m', module, *arguments]


def run_service(repo: Path, folder: Path, request: dict[str, object], command: Sequence[str],
                service: Callable[[Sequence[str]], int]) -> LocalFallback | CloudCompleted:
    # The service owns every AWS call. The session only reads local evidence.
    _ = service(command)
    if not (folder / 'outcome.json').exists():
        return LocalFallback('The scaler service has no final result')
    outcome = read(folder / 'outcome.json')
    if outcome.get('status') == 'fallback':
        return LocalFallback(ledger.text(outcome, 'reason'))
    if outcome.get('status') != 'complete':
        return LocalFallback(ledger.text(outcome, 'reason'))
    row = ledger.mapping(outcome['result'])
    try:
        validate_result(row, request)
    except (ValueError, KeyError, TypeError) as error:
        return LocalFallback(str(error))
    if gate.checked_git(repo, 'rev-parse', 'HEAD') != request['head']:
        return LocalFallback('The checkout changed during the cloud attempt')
    import_record(folder, row)
    print((folder / 'gate.log').read_text(), end='', flush=True)
    return CloudCompleted(gate_box.decode_run(row['gate']))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    _ = group.add_argument('--worker', type=Path)
    _ = group.add_argument('--cleanup', type=Path)
    _ = parser.add_argument('--directory', required=True, type=Path)
    _ = parser.add_argument('--sidecar', required=True, type=Path)
    _ = parser.add_argument('--settings', type=Path, default=account_settings_path())
    args = parser.parse_args()
    cleanup = cast(Path | None, args.cleanup)
    folder = cleanup if cleanup is not None else cast(Path, args.worker)
    directory = cast(Path, args.directory)
    with control_lock(folder):
        settings_path = cast(Path, args.settings)
        configuration = conf.read(settings_path)
        api = AwsCli(configuration.text('credentials_profile'), configuration.text('region'), settings().number('api_timeout_s'))
        worker = GateWorker(api, ledger.CostLedger(directory), settings_path, cast(Path, args.sidecar))
        if cleanup is not None:
            with defer_cleanup_signals():
                return int(not cleanup_folder(folder, worker))
        if (folder / 'outcome.json').exists() and read(folder / 'outcome.json').get('status') != 'pending':
            return 0
        outcome = worker.execute(folder, read(folder / 'request.json'), read(folder / 'deployment.json'))
        save(folder / 'outcome.json', outcome)
    return 0




def route(repo: Path, *, mode: gate.SuiteMode, full: bool, module_time_limit_s: float,
          selected: gate.AllModules | gate.TreeSelection = gate.ALL_MODULES) -> gate.GateRun | str:
    """Return a completed gate or the reason for local fallback."""
    outcome = run(repo, mode=mode, full=full, module_time_limit_s=module_time_limit_s, selected=selected)
    return outcome.reason if isinstance(outcome, LocalFallback) else outcome.run


if __name__ == '__main__':
    raise SystemExit(main())
