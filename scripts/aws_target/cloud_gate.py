"""Run cloud gates through a scaler service and retain each attempt locally."""
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
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from types import FrameType
from typing import Literal, cast, final
from uuid import uuid4

from scripts.aws_target import gate_box, ledger
from scripts.aws_target.aws_target import AwsCli, AwsUnavailable, control_lock
from scripts.aws_target.config import money, read_settings
from scripts.build_queue import admission_client, admission_protocol as wire, cloud_progress, progress_profile
from scripts.build_queue.queue_place import defer_cleanup_signals
from scripts.build_targets import target_config
from scripts.buildlog import store
from scripts.lib import conf
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


def settings() -> conf.ConfigRead:
    return conf.read_or_repository('cloud_gate')


def read(path: Path) -> dict[str, object]:
    return ledger.mapping(cast(object, json.loads(path.read_text())))


def save(path: Path, row: dict[str, object]) -> None:
    write_text_atomic(path, json.dumps(row, sort_keys=True) + '\n')


def capacity_error(reason: str) -> bool:
    return any(code in reason for code in ('InsufficientInstanceCapacity', 'InsufficientSpotCapacity',
                                           'MaxSpotInstanceCountExceeded', 'VcpuLimitExceeded', 'SpotMaxPriceTooLow'))


def aws_down(reason: str) -> bool:
    return any(code in reason for code in ('Could not connect to the endpoint', 'EndpointConnectionError',
                                           'ServiceUnavailable', 'InternalError', 'RequestTimeout', 'Connection timed out'))


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


@final
class GateWorker:
    """Own one durable launch, result transfer, and confirmed termination."""
    def __init__(self, api: AwsCli, book: ledger.CostLedger, settings_path: Path, sidecar: Path, *,
                 clock: Callable[[], float] = time.time, pause: Callable[[float], None] = time.sleep) -> None:
        self.api = api
        self.book = book
        self.settings_path = settings_path
        self.sidecar = sidecar
        self.clock = clock
        self.pause = pause
        self.size_vcpus: int = 0

    def publish_log(self, folder: Path) -> None:
        text = (folder / 'gate.log').read_text()
        counter = log_progress(text)
        if isinstance(counter, progress_profile.ProgressCounter):
            progress_profile.publish_counter(self.sidecar, counter)
        cloud_progress.publish(self.sidecar, log_cloud_progress(text, self.size_vcpus, self.clock()))

    def charges(self) -> None:
        def fetch(start: int, end: int) -> Decimal:
            policy = read_settings(self.settings_path)
            period = ','.join((f'Start={datetime.fromtimestamp(start, UTC):%Y-%m-%d}',
                               f'End={datetime.fromtimestamp(end, UTC):%Y-%m-%d}'))
            reply = self.api.call('get-cost-and-usage', '--time-period', period, '--granularity', 'DAILY',
                                  '--metrics', 'UnblendedCost', '--filter', json.dumps({'Tags': {
                                      'Key': policy.cost_explorer_tag_key, 'Values': [policy.cost_explorer_tag_value]}}), service='ce')
            total = Decimal(0)
            for row in ledger.rows(reply, 'ResultsByTime'):
                cost = ledger.mapping(ledger.mapping(row['Total'])['UnblendedCost'])
                if cost.get('Unit') != 'USD':
                    raise ValueError('Cost Explorer did not return USD')
                total += money(ledger.text(cost, 'Amount'))
            return total
        _ = self.book.refresh_charges(self.settings_path, int(self.clock()), fetch, gate_workload=True)

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

    def terminate(self, attempt: str, instance: str) -> None:
        _ = self.api.call('terminate-instances', '--instance-ids', instance)
        deadline = self.clock() + settings().number('termination_timeout_s')
        while self.clock() < deadline:
            if self.api.observe(instance, int(self.clock())).state == 'terminated':
                self.book.resolve_gate(attempt, ledger.GateTerminated(int(self.clock()), instance))
                return
            self.pause(settings().number('result_poll_s'))
        raise AwsUnavailable('Termination is unconfirmed; the expiry function still owns cleanup')

    def reconcile(self) -> None:
        """Resolve expired launches and observe known gate instances."""
        for reservation in self.book.read_snapshot().gate_reservations:
            if isinstance(reservation.state, ledger.GateRunning):
                instance = reservation.state.instance_id
                if self.api.observe(instance, int(self.clock())).state == 'terminated':
                    self.book.resolve_gate(reservation.attempt, ledger.GateTerminated(int(self.clock()), instance))
            elif isinstance(reservation.state, ledger.GateLaunchPending) and reservation.expires <= int(self.clock()):
                reply = self.api.call('describe-instances', '--filters', 'Name=tag:GateAttempt,Values=' + reservation.attempt)
                instances = [instance for group in ledger.rows(reply, 'Reservations')
                             for instance in ledger.rows(group, 'Instances')]
                if not instances:
                    self.book.resolve_gate(reservation.attempt, ledger.GateAbsent(int(self.clock())))
                else:
                    instance = ledger.text(instances[0], 'InstanceId')
                    terminated = ledger.text(ledger.mapping(instances[0]['State']), 'Name') == 'terminated'
                    self.book.resolve_gate(reservation.attempt,
                        ledger.GateTerminated(int(self.clock()), instance) if terminated else ledger.GateRunning(instance))

    def record_launch(self, request: dict[str, object], size: str, zone: str,
                      result: Literal['launched', 'refused'], reason: str = '') -> None:
        error_code = launch_error_code(reason)
        row: dict[str, object] = {'time': self.clock(), 'attempt': request['attempt'], 'instance_type': size,
            'vcpus': self.size_vcpus, 'availability_zone': zone, 'result': result,
            'error_code': error_code, 'reason': reason}
        with store.locked_append(self.book.directory / 'cloud-gate-launches.jsonl') as handle:
            store.append_locked(handle, row)
        if result == 'refused':
            print(f'Cloud gate refused: {size} in {zone}: {error_code}', flush=True)

    def launch(self, folder: Path, request: dict[str, object], deployment: dict[str, object]) -> str | LocalFallback:
        path = folder / 'launch.json'
        launch: dict[str, object] = read(path) if path.exists() else {}
        if launch.get('instance'):
            return ledger.text(launch, 'instance')
        sizes = settings().words('instance_types')
        variants = [(size, row) for size in sizes for row in ledger.rows(deployment, 'subnets')]
        index = ledger.instant(launch, 'variant') if launch else 0
        for offset in range(index, len(variants)):
            size, subnet = variants[offset]
            if launch.get('arguments') and offset == index:
                arguments = ledger.mapping(launch['arguments'])
            else:
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
                policy = read_settings(self.settings_path)
                tags = [{'Key': 'GateDeployment', 'Value': ledger.text(deployment, 'deployment_id')},
                        {'Key': 'Workload', 'Value': 'cloud-claude-gate'}, {'Key': 'Name', 'Value': 'cloud-claude-gate'}, {'Key': 'GateAttempt', 'Value': ledger.text(request, 'attempt')},
                        {'Key': 'ExpiresAt', 'Value': str(request['expires'])},
                        {'Key': policy.cost_explorer_tag_key, 'Value': policy.cost_explorer_tag_value}]
                arguments: dict[str, object] = {
                    'LaunchTemplate': {'LaunchTemplateId': deployment['template'], 'Version': deployment['version']},
                    'MinCount': 1, 'MaxCount': 1, 'ClientToken': ledger.text(request, 'attempt') + '-' + str(offset),
                    'InstanceType': size,
                    'NetworkInterfaces': [{'DeviceIndex': 0, 'SubnetId': subnet['id'],
                                           'Groups': [deployment['security_group']], 'AssociatePublicIpAddress': True}],
                    'TagSpecifications': [{'ResourceType': kind, 'Tags': tags} for kind in ('instance', 'volume')],
                    'UserData': gate_box.user_data(links, ledger.text(deployment, 'revision'), lifetime,
                              settings().number('log_push_s'), float(str(request['module_time_limit_s'])))}
                launch = {'variant': offset, 'arguments': arguments}
                save(path, launch)
            self.size_vcpus = int(ledger.text(arguments, 'InstanceType').split('.')[1].removesuffix('xlarge')) * 4
            cloud_progress.publish(self.sidecar, cloud_progress.CloudProgress('requesting', self.size_vcpus, 0, self.clock()))
            if self.clock() >= ledger.instant(request, 'expires'):
                reply = self.api.call('describe-instances', '--filters',
                                      'Name=client-token,Values=' + ledger.text(arguments, 'ClientToken'))
                instances = [instance for group in ledger.rows(reply, 'Reservations')
                             for instance in ledger.rows(group, 'Instances')]
                if len(instances) == 1:
                    instance = ledger.text(instances[0], 'InstanceId')
                    save(path, {**launch, 'instance': instance})
                    self.book.resolve_gate(ledger.text(request, 'attempt'), ledger.GateRunning(instance))
                    return instance
                raise AwsUnavailable('The expired launch remains unconfirmed')
            try:
                for retry in range(settings().whole('launch_reply_tries')):
                    try:
                        reply = self.api.call('run-instances', '--cli-input-json', json.dumps(arguments))
                        self.record_launch(request, size, ledger.text(subnet, 'zone'), 'launched')
                        break
                    except AwsUnavailable as error:
                        self.record_launch(request, size, ledger.text(subnet, 'zone'), 'refused', str(error))
                        if not aws_down(str(error)) or retry + 1 == settings().whole('launch_reply_tries'):
                            raise
                    except subprocess.SubprocessError as error:
                        self.record_launch(request, size, ledger.text(subnet, 'zone'), 'refused',
                                           type(error).__name__ + ': ' + str(error))
                        raise
                else:
                    raise AssertionError('The launch retry count must be positive')
            except AwsUnavailable as error:
                # A saved request keeps its token and bytes after an uncertain reply.
                if capacity_error(str(error)):
                    launch = {}
                    save(path, {'variant': offset + 1})
                    continue
                self.book.resolve_gate(ledger.text(request, 'attempt'), ledger.GateAbsent(int(self.clock())))
                return LocalFallback(f'{size} in {ledger.text(subnet, "zone")}: {launch_error_code(str(error))}')
            instances = ledger.rows(reply, 'Instances')
            if len(instances) != 1:
                raise AwsUnavailable('The launch reply has no unique instance')
            instance = ledger.text(instances[0], 'InstanceId')
            save(path, {**launch, 'instance': instance})
            self.book.resolve_gate(ledger.text(request, 'attempt'), ledger.GateRunning(instance))
            return instance
        self.book.resolve_gate(ledger.text(request, 'attempt'), ledger.GateAbsent(int(self.clock())))
        return LocalFallback('Spot capacity is unavailable at both sizes')

    def execute(self, folder: Path, request: dict[str, object], deployment: dict[str, object]) -> dict[str, object]:
        attempt = ledger.text(request, 'attempt')
        row: dict[str, object] = {}
        try:
            cloud_progress.publish(self.sidecar, cloud_progress.CloudProgress('requesting', 0, 0, self.clock()))
            if not (folder / 'launch.json').exists():
                self.reconcile()
                self.charges()
                reservation = self.book.reserve_gate(self.settings_path, ledger.GateReservation(
                    attempt, int(self.clock()), ledger.instant(request, 'expires'),
                    money(str(settings().number('spot_max_usd_per_hour'))),
                    money(str(settings().number('attempt_allowance_usd')))))
                if isinstance(reservation, ledger.GateCapReached):
                    return {'status': 'fallback', 'reason': reservation.reason}
                bucket = ledger.text(deployment, 'bucket')
                if request['source_key']:
                    self.upload(bucket, ledger.text(request, 'source_key'), folder / 'source.bundle')
                self.upload(bucket, 'requests/' + attempt + '.json', folder / 'request.json')
                guest = folder / 'guest.py'
                _ = guest.write_text(gate_box.GUEST)
                self.upload(bucket, 'requests/' + attempt + '.guest.py', guest)
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
                    bucket = ledger.text(deployment, 'bucket')
                    if self.download(bucket, prefix + '/gate.log', folder / 'gate.log'):
                        self.publish_log(folder)
                    if self.download(bucket, prefix + '/boot-error.json', folder / 'boot-error.json'):
                        raise ValueError(ledger.text(read(folder / 'boot-error.json'), 'error'))
                    if self.download(bucket, prefix + '/result.json', folder / 'remote-result.json'):
                        if not self.download(bucket, prefix + '/gate.log', folder / 'gate.log'):
                            raise ValueError('The final log is absent')
                        result = read(folder / 'remote-result.json')
                        validate_result(result, request)
                        row = result
                        self.publish_log(folder)
                        break
                    state = self.api.observe(instance, int(self.clock())).state
                    if state not in ('pending', 'running'):
                        reply = self.api.call('describe-instances', '--instance-ids', instance)
                        stopped = ledger.rows(ledger.rows(reply, 'Reservations')[0], 'Instances')[0]
                        reason = ledger.mapping(stopped.get('StateReason', {}))
                        detail = ': '.join(ledger.text(reason, key) for key in ('Code', 'Message') if reason.get(key))
                        raise ValueError('The instance stopped without a final result' + (f' ({detail})' if detail else ''))
                    self.pause(settings().number('result_poll_s'))
                if not row:
                    raise ValueError('The instance expired without a final result')
            finally:
                self.terminate(attempt, instance)
            return {'status': 'complete', 'instance': instance, 'result': row}
        except (OSError, ValueError, KeyError, TypeError, AwsUnavailable, subprocess.SubprocessError) as error:
            launch = read(folder / 'launch.json') if (folder / 'launch.json').exists() else {}
            if not launch.get('instance'):
                self.book.resolve_gate(attempt, ledger.GateAbsent(int(self.clock())))
            if row and not gate_box.decode_run(row['gate']).passed:
                return {'status': 'complete', 'result': row}
            return {'status': 'fallback', 'reason': str(error)}


def validate_result(row: dict[str, object], request: dict[str, object]) -> None:
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


def prepare(repo: Path, folder: Path, mode: gate.SuiteMode, full: bool, limit: float) -> dict[str, object]:
    commit = gate.checked_git(repo, 'rev-parse', 'HEAD')
    tree = gate.checked_git(repo, 'rev-parse', 'HEAD^{tree}')
    if affected_modules.dirty_paths(repo):
        raise GateRefused('The cloud gate requires a clean tested tree')
    modules = discover_test_modules(repo)
    if mode is gate.SuiteMode.TARGETED_TEST and not full:
        book = gate.PassedTreeLedger(repo, 'tests-only', gate.CleanTestTree(tree, commit), sorted({s for s, _ in modules}))
        selection = affected_modules.select_affected(repo, book.certified_base(), [p for _, p in modules], set(), state_dir=gate.gate_state_dir())
        gate.report_selection(selection, len(modules))
        if isinstance(selection, affected_modules.AffectedSelection):
            modules = [(s, p) for s, p in modules if p in selection.modules]
    refs = ['HEAD']
    origin_main = gate.checked_git(repo, 'rev-parse', 'refs/remotes/origin/main')
    branch = gate.git(repo, 'symbolic-ref', '--quiet', 'HEAD').stdout.strip()
    if branch:
        refs.append(branch)
    origin = gate.checked_git(repo, 'remote', 'get-url', 'origin')
    from urllib.parse import urlsplit
    if urlsplit(origin).password or urlsplit(origin).query or ('://' in origin and urlsplit(origin).username):
        raise GateRefused('The origin URL must contain no credential')
    folder.mkdir(parents=True, exist_ok=True)
    source_key = ''
    if int(gate.checked_git(repo, 'rev-list', '--count', 'HEAD', '--not', 'refs/remotes/origin/main')):
        _ = gate.checked_git(repo, 'bundle', 'create', str(folder / 'source.bundle'), *refs, '--not', 'refs/remotes/origin/main')
        digest = sha256((folder / 'source.bundle').read_bytes()).hexdigest()
        source_key = 'source/' + digest + '.bundle'
    request: dict[str, object] = {'attempt': folder.name, 'commit': commit, 'tree': tree, 'origin': origin,
        'origin_main': origin_main, 'clone_url': settings().text('clone_url'),
        'mode': mode.value, 'modules': [str(p.relative_to(repo)) for _, p in modules], 'source_key': source_key,
        'expires': int(time.time()) + settings().whole('lifetime_minutes') * 60,
        'module_time_limit_s': min(limit, settings().number('module_time_limit_s'))}
    save(folder / 'request.json', request)
    return request


def run(repo: Path, *, mode: gate.SuiteMode, full: bool, module_time_limit_s: float,
        service: Callable[[Sequence[str]], int] = lambda command: subprocess.call(command)) -> LocalFallback | CloudCompleted:
    try:
        return run_cloud(repo, mode=mode, full=full, module_time_limit_s=module_time_limit_s, service=service)
    except (OSError, ValueError, KeyError, TypeError, AwsUnavailable, subprocess.SubprocessError, GateRefused) as error:
        return LocalFallback(str(error))


def run_cloud(repo: Path, *, mode: gate.SuiteMode, full: bool, module_time_limit_s: float,
              service: Callable[[Sequence[str]], int]) -> LocalFallback | CloudCompleted:
    directory = target_config.ssh_target_state_directory('aws')
    deployment_path = directory / 'gate-deployment.json'
    if not deployment_path.exists():
        return LocalFallback('image or stack is absent')
    deployment = read(deployment_path)
    if not deployment.get('image') or not deployment.get('template'):
        return LocalFallback('image or stack is absent')
    root = Path(gate.checked_git(repo, 'rev-parse', '--path-format=absolute', '--git-path', 'cloud-gates'))
    with control_lock(root):
        current = root / 'current.json'
        tree = gate.checked_git(repo, 'rev-parse', 'HEAD^{tree}')
        previous = read(current) if current.exists() else {}
        identity: dict[str, object] = {'tree': tree, 'commit': gate.checked_git(repo, 'rev-parse', 'HEAD'),
                                      'mode': mode.value, 'full': full, 'module_time_limit_s': module_time_limit_s}
        same_request = all(previous.get(key) == value for key, value in identity.items())
        folder = root / ledger.text(previous, 'attempt') if same_request else root / uuid4().hex
        if (folder / 'outcome.json').exists() and read(folder / 'outcome.json').get('status') != 'pending':
            folder = root / uuid4().hex
        folder.mkdir(parents=True, exist_ok=True)
        sidecar = folder / 'reservation.cgroup'
        credential = str(Path(settings().text('credential_file')).expanduser())
        command = ['systemd-run', '--user', '--wait', '--pipe', '--collect', '--unit=cloud-gate-' + folder.name,
                   '--setenv=AWS_SHARED_CREDENTIALS_FILE=' + credential,
                   '--setenv=CONF_DIR=' + str(conf.path('aws_target').parent),
                   '--property=KillMode=control-group', '--property=UMask=0077',
                   '--property=UnsetEnvironment=AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_CONFIG_FILE AWS_PROFILE',
                   # The installed tree launches the box; the checkout supplies only its source and tests.
                   '--working-directory=' + str(Path.home() / '.claude'),
                   sys.executable, '-m', 'scripts.aws_target.cloud_gate', '--worker', str(folder), '--directory', str(directory),
                   '--sidecar', str(sidecar)]
        with cloud_reservation(repo, sidecar, mode) as reservation:
            request = read(folder / 'request.json') if (folder / 'request.json').exists() else prepare(repo, folder, mode, full, module_time_limit_s)
            save(current, {'attempt': folder.name, **identity})
            save(folder / 'deployment.json', deployment)
            cloud_progress.publish(reservation.sidecar, cloud_progress.CloudProgress('requesting', 0, 0, time.time()))
            outcome = run_service(repo, folder, request, command, service)
            reservation.status = int(not outcome.run.passed) if isinstance(outcome, CloudCompleted) else 0
            return outcome


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
    if gate.checked_git(repo, 'rev-parse', 'HEAD') != request['commit']:
        return LocalFallback('The checkout changed during the cloud attempt')
    import_record(folder, row)
    print((folder / 'gate.log').read_text(), end='', flush=True)
    return CloudCompleted(gate_box.decode_run(row['gate']))


def main() -> int:
    def stop_worker(number: int, _frame: FrameType | None) -> None:
        raise SystemExit(128 + number)
    _ = signal.signal(signal.SIGTERM, stop_worker)
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument('--worker', required=True, type=Path)
    _ = parser.add_argument('--directory', required=True, type=Path)
    _ = parser.add_argument('--sidecar', required=True, type=Path)
    args = parser.parse_args()
    folder = cast(Path, args.worker)
    directory = cast(Path, args.directory)
    with control_lock(folder):
        if (folder / 'outcome.json').exists() and read(folder / 'outcome.json').get('status') != 'pending':
            return 0
        configuration = conf.read('aws_target')
        api = AwsCli(configuration.text('credentials_profile'), configuration.text('region'), settings().number('api_timeout_s'))
        worker = GateWorker(api, ledger.CostLedger(directory), conf.path('aws_target'), cast(Path, args.sidecar))
        try:
            outcome = worker.execute(folder, read(folder / 'request.json'), read(folder / 'deployment.json'))
        except SystemExit:
            save(folder / 'outcome.json', {
                'status': 'fallback', 'reason': 'The worker stopped, so its instance was terminated'})
            raise
        save(folder / 'outcome.json', outcome)
    return 0




def route(repo: Path, *, mode: gate.SuiteMode, full: bool, module_time_limit_s: float) -> gate.GateRun | str:
    """Return a completed gate or the reason for local fallback."""
    outcome = run(repo, mode=mode, full=full, module_time_limit_s=module_time_limit_s)
    return outcome.reason if isinstance(outcome, LocalFallback) else outcome.run


if __name__ == '__main__':
    raise SystemExit(main())
