"""Measure disposable gate instances from the user's terminal."""
from __future__ import annotations

import argparse
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
import hashlib
import hmac
import json
import math
from pathlib import Path
import shlex
import signal
import statistics
from types import FrameType
import subprocess
import tempfile
import time
from typing import cast, final
from urllib.parse import quote
from uuid import uuid4

from scripts.aws_target import admin_secrets, ledger
from scripts.aws_target.aws_target import AwsCli, AwsUnavailable, execute_command
from scripts.aws_target.config import read_settings
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic


@dataclass(frozen=True, repr=False)
class SigningKey:
    access: str
    secret: str


def presign(method: str, host: str, key: str, region: str, credentials: SigningKey,
            at: datetime, expires: int) -> str:
    """Sign one S3 object URL with the documented SigV4 query algorithm."""
    stamp = at.strftime('%Y%m%dT%H%M%SZ')
    scope = f'{stamp[:8]}/{region}/s3/aws4_request'
    parameters = {'X-Amz-Algorithm': 'AWS4-HMAC-SHA256',
                  'X-Amz-Credential': f'{credentials.access}/{scope}',
                  'X-Amz-Date': stamp, 'X-Amz-Expires': str(expires), 'X-Amz-SignedHeaders': 'host'}
    query = '&'.join(f'{quote(k, safe="")}={quote(v, safe="")}' for k, v in sorted(parameters.items()))
    path = '/' + quote(key, safe='/~')
    canonical = f'{method}\n{path}\n{query}\nhost:{host}\n\nhost\nUNSIGNED-PAYLOAD'
    signing = ('AWS4' + credentials.secret).encode()
    for value in (stamp[:8], region, 's3', 'aws4_request'):
        signing = hmac.digest(signing, value.encode(), 'sha256')
    content = f'AWS4-HMAC-SHA256\n{stamp}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}'
    signature = hmac.new(signing, content.encode(), hashlib.sha256).hexdigest()
    return f'https://{host}{path}?{query}&X-Amz-Signature={signature}'


# This program runs inside the instance's tool shell, not on natedev.
GUEST = r'''import contextlib, json, os, pathlib, subprocess, sys, time, traceback, urllib.request
root = pathlib.Path('/var/lib/gate-trial')
links = json.loads((root / 'links.json').read_text())
links['boot'] = 'warm' if (root / 'prepared').exists() else 'cold'
result = {'nix_ready_at': time.time(), 'verdict': 'ready', 'tests': 0, 'skips': 0, 'failed_modules': []}
log = root / 'gate.log'
def put(url, content):
    with urllib.request.urlopen(urllib.request.Request(url, data=content, method='PUT'), timeout=30) as response:
        response.read()
def publish(name):
    put(links[name], json.dumps(result).encode())
try:
    result['tools'] = {}
    for name in ('git', 'python3', 'basedpyright', 'cargo', 'rustc', 'rustdoc', 'rustup', 'cargo-nextest', 'uv'):
        version = subprocess.run([name, '--version'], capture_output=True, text=True)
        result['tools'][name] = (version.stdout or version.stderr).strip()
    if links['step'] == 'start':
        result['tools_ready_at'] = time.time()
        links['boot'] = 'warm' if (root / 'prepared').exists() else 'cold'
    else:
        with urllib.request.urlopen(links['source'], timeout=60) as response:
            (root / 'source.bundle').write_bytes(response.read())
        subprocess.run(['git', 'clone', str(root / 'source.bundle'), str(root / 'repo')], check=True)
        repo = root / 'repo'
        subprocess.run(['git', '-C', str(repo), 'checkout', '--detach', links['commit']], check=True)
        pathlib.Path('/root/.claude').mkdir(exist_ok=True)
        pathlib.Path('/root/.claude/config').symlink_to(repo / 'config', target_is_directory=True)
        os.chdir(repo)
        # The gate uses its own admission service. It never contacts natedev's queue.
        admission = subprocess.Popen([sys.executable, 'scripts/build_queue/admission_service.py', 'serve'])
        try:
            sys.path.insert(0, str(repo))
            from scripts.production import merge_gate
            preparation = merge_gate.typecheck_python()
            if isinstance(preparation, str):
                raise RuntimeError(preparation)
            result['tools_ready_at'] = time.time()
            result['gate_started_at'] = time.time()
            with log.open('w') as output, contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                with merge_gate.rollback_signals():
                    gate = merge_gate.run_suites(repo, type_dirs=['scripts'], full=True)
            result['gate_finished_at'] = time.time()
            result['gate_s'] = gate.seconds
            result['verdict'] = 'pass' if gate.passed else 'fail'
            result['modules'] = []
            for job in gate.jobs:
                measured = job.times if isinstance(job.times, dict) else {}
                result['tests'] += measured.get('tests_run', 0)
                result['skips'] += measured.get('skipped', 0)
                name = job.suite + '/' + job.module
                result['modules'].append({'module': name, 'status': job.returncode, 'tests': measured.get('tests_run', 0),
                                          'skips': measured.get('skipped', 0), 'seconds': job.seconds})
                if job.returncode:
                    result['failed_modules'].append(name)
        finally:
            admission.terminate()
            admission.wait(timeout=10)
except BaseException:
    if 'gate_started_at' in result:
        result['gate_finished_at'] = time.time()
    result['verdict'] = 'error'
    result['error'] = traceback.format_exc()
    with log.open('a') as output:
        output.write(result['error'])
finally:
    if log.exists():
        try:
            put(links['log'], log.read_bytes())
        except Exception as error:
            result['log_error'] = str(error)
    publish('result' if links['step'] == 'size' else 'ready-' + links['boot'])
    if links['step'] == 'start' and result['verdict'] == 'ready':
        (root / 'prepared').touch()
'''


def boot_trap(links: dict[str, str]) -> str:
    """Publish a shell failure before the tool shell can publish a result."""
    cold = links['result'] if links['step'] == 'size' else links['ready-cold']
    warm = links['result'] if links['step'] == 'size' else links['ready-warm']
    return f'''boot_failed() {{
    status=$?
    trap - EXIT
    if [ "$status" -ne 0 ]; then
        destination={shlex.quote(cold)}
        if [ -e /var/lib/gate-trial/prepared ]; then destination={shlex.quote(warm)}; fi
        curl --fail --silent --show-error --max-time 30 -X PUT --data-binary @/var/lib/gate-trial/boot.log {shlex.quote(links['log'])} || :
        printf '{{"verdict":"error","error":"Tool preparation failed (exit %s)"}}' "$status" > /var/lib/gate-trial/boot-error.json
        curl --fail --silent --show-error --max-time 30 -X PUT --data-binary @/var/lib/gate-trial/boot-error.json "$destination" || :
    fi
    exit "$status"
}}
trap boot_failed EXIT
'''


def user_data(links: dict[str, str], release: str, lifetime_minutes: int) -> str:
    """Run directly in amazon-init on each boot."""
    packages = ('git', 'python3', 'basedpyright', 'cargo', 'rustc', 'rustup', 'cargo-nextest',
                'uv', 'curl', 'openssh', 'rsync', 'gnupg', 'ripgrep', 'jq', 'gcc', 'pkg-config', 'tmux')
    tools = ' '.join(shlex.quote(f'github:NixOS/nixpkgs/nixos-{release}#{name}') for name in packages)
    return f'''#!/bin/sh
shutdown -h +{lifetime_minutes}
set -eu
umask 077
mkdir -p /var/lib/gate-trial
exec > /var/lib/gate-trial/boot.log 2>&1
export PATH=/run/current-system/sw/bin:$PATH
{boot_trap(links)}cat > /var/lib/gate-trial/links.json <<'GATE_LINKS'
{json.dumps(links)}
GATE_LINKS
cat > /var/lib/gate-trial/guest.py <<'GATE_GUEST'
{GUEST}
GATE_GUEST
export HOME=/root
export XDG_RUNTIME_DIR=/run/user/0
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/0/bus
export PYTHONUNBUFFERED=1
loginctl enable-linger root
nix --extra-experimental-features 'nix-command flakes' shell {tools} --command python3 /var/lib/gate-trial/guest.py
'''


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
def trial_signals() -> Generator[None]:
    def interrupt(_number: int, _frame: FrameType | None) -> None:
        raise KeyboardInterrupt
    previous = {number: signal.signal(number, interrupt) for number in TRIAL_SIGNALS}
    try:
        yield
    finally:
        for number, handler in previous.items():
            _ = signal.signal(number, handler)


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
                   settings.number('deployment_timeout_s'), path)


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

    def tagged_instances(self, *, stopped: bool = False) -> list[str]:
        tag = 'Name=tag-key,Values=GateTrial' if stopped else 'Name=tag:GateTrial,Values=' + self.run_id
        states = 'stopping,stopped' if stopped else 'pending,running,stopping,stopped'
        reply = self.api.call('describe-instances', '--filters', tag, 'Name=instance-state-name,Values=' + states)
        return [ledger.text(row, 'InstanceId') for reservation in ledger.rows(reply, 'Reservations')
                for row in ledger.rows(reservation, 'Instances')]

    def sweep(self, *, stopped: bool = False, attempt: Attempt | None = None) -> None:
        try:
            instances = self.tagged_instances(stopped=stopped)
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
                **{name: url('PUT', key + '/' + name) for name in ('result', 'log', 'ready-cold', 'ready-warm')}}

    def state(self, instance: str) -> str:
        return self.api.observe(instance, int(self.clock())).state

    def wait_state(self, instance: str, wanted: str, deadline: float) -> None:
        while self.clock() < deadline:
            if self.state(instance) == wanted:
                return
            _ = self.pause(self.option('poll_pause_s'))
        raise TimeoutError(f'Instance did not reach {wanted} before the trial deadline')

    def read_object(self, key: str, destination: Path) -> dict[str, object] | None:
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
                row = self.read_object(key, Path(folder) / 'result.json')
                if row is not None:
                    return row
                if self.state(attempt.instance_id) == 'terminated':
                    raise RuntimeError('Instance terminated before it wrote its result')
                _ = self.pause(self.option('poll_pause_s'))
        raise TimeoutError('The guest did not publish its result before the trial deadline')

    def terminate(self, instance: str) -> None:
        _ = self.api.call('terminate-instances', '--instance-ids', instance)
        self.wait_state(instance, 'terminated', self.clock() + self.settings.timeout_s)
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
                {'Key': 'GateTrial', 'Value': self.run_id}, {'Key': 'Name', 'Value': 'gate-trial'}]}],
        }
        if step == 'size':
            request['InstanceMarketOptions'] = {'MarketType': 'spot', 'SpotOptions': {'SpotInstanceType': 'one-time'}}
        attempt.request_at = self.clock()
        with blocked_trial_signals():
            for retry in range(3):
                try:
                    reply = self.api.call('run-instances', '--cli-input-json', json.dumps(request), '--user-data', 'file://' + str(data))
                except subprocess.SubprocessError:
                    self.launch_reply_lost = True
                    if retry == 2:
                        raise
                else:
                    attempt.instance_id = ledger.text(ledger.rows(reply, 'Instances')[0], 'InstanceId')
                    self.launch_reply_lost = False
                    self.save()
                    return

    def download_log(self, attempt: Attempt, key: str) -> None:
        destination = self.directory / f'{self.run_id}-{key}.log'
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
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / 'user-data'
            minutes = conf.read(self.settings.path).whole('gate_trial_lifetime_minutes')
            _ = data.write_text(user_data(self.links(key, step), self.settings.release, minutes))
            lifetime = minutes * 60
            try:
                for subnet, zone in self.subnets:
                    attempt.subnet, attempt.zone = subnet, zone
                    prices = self.prices(attempt.size, step, zone)
                    projected = prices.lifetime(lifetime, self.settings.root_gib) * (2 if step == 'start' else 1)
                    transfer_reserve = prices.put * 4 + prices.get * (lifetime * 2 / self.option('poll_pause_s') + 100)
                    if self.spent + self.s3_cost + projected + transfer_reserve > reserve:
                        attempt.error = 'The next fixed lifetime exceeds the step reserve.'
                        attempt.verdict = 'not run'
                        return
                    attempt.prices = asdict(prices)
                    try:
                        attempt.clear_refusal = False
                        self.launch(attempt, data, step)
                        break
                    except AwsUnavailable as error:
                        attempt.clear_refusal = 'An error occurred (' in str(error) or 'InsufficientInstanceCapacity' in str(error)
                        attempt.error = str(error)
                        print(f'{zone}: {error}', flush=True)
                        if 'InsufficientInstanceCapacity' not in str(error):
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
                    self.spent += attempt.dollars
                    print(self.row(attempt), flush=True)
                    warm = Attempt(attempt.size, attempt.attempt, 'stopped start', instance_id=attempt.instance_id,
                                   zone=attempt.zone, subnet=attempt.subnet)
                    warm.prices = asdict(prices)
                    self.attempts.append(warm)
                    attempt = warm
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
            except KeyboardInterrupt:
                attempt.verdict = 'cancelled'
                raise
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
                        self.cleanup_errors.append('Attempt cleanup: ' + str(error))
                        if attempt.instance_id not in self.remaining_instances:
                            self.remaining_instances.append(attempt.instance_id)
                    if attempt.verdict not in ('pass', 'ready'):
                        try:
                            self.download_log(attempt, key)
                        except CLEANUP_ERRORS as error:
                            attempt.error += '; Log: ' + str(error)
                    if attempt.instance_id and prices:
                        billed_end = attempt.terminated_at or self.clock()
                        attempt.dollars += prices.lifetime(billed_end - attempt.request_at, self.settings.root_gib)
                    self.finish_times(attempt)
                    self.spent += attempt.dollars
                    print(self.row(attempt), flush=True)
                    self.save()

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
                + f'{attempt.result.get("tests", "-")} | {attempt.result.get("skips", "-")} | {attempt.verdict} | '
                + f'{attempt.result.get("failed_modules", [])} | {attempt.error.replace(chr(10), "; ")} | {attempt.log_path} | '
                + json.dumps(attempt.capacity_refusals))

    def report(self) -> tuple[dict[str, object], str]:
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
            'medians': medians, 'cheapest_size': min(qualified)[1] if qualified else None, 'Batch': 'not measured',
            'dollars': self.spent + self.s3_cost, 'cleanup_errors': self.cleanup_errors, 'interrupted': self.interrupted, 'remaining': self.remaining(),
            'cost_basis': 'Read prices and observed seconds. Final rows share the S3 cost. Confirm costs with the AWS bill.'}
        text = ('Commit: ' + self.commit + '\nsize | attempt | method | zone | request to running | running to tools ready | '
                + 'tools ready to gate start | gate | teardown | request to ready | dollars | tests | skips | verdict | failed modules | error | log | capacity refusals\n'
                + '\n'.join(self.row(row) for row in self.attempts) + '\nMedians:\n'
                + '\n'.join(name + ': ' + json.dumps(values, sort_keys=True) for name, values in medians.items())
                + f'\nCheapest size at most {self.option("target_gate_s"):g} s: {report["cheapest_size"]}\nBatch: not measured\n'
                + f'Total: ${self.spent + self.s3_cost:.4f}\nCleanup: {self.cleanup_errors}\n'
                + 'Remaining: ' + (', '.join(self.remaining()) or 'none') + '\n')
        return report, text

    def save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        record, content = self.report()
        write_text_atomic(self.directory / f'gate-trial-{self.run_id}.json', json.dumps(record, indent=2) + '\n', new_file_mode=0o600)
        write_text_atomic(self.directory / f'gate-trial-{self.run_id}.txt', content, new_file_mode=0o600)

    def remaining(self) -> list[str]:
        return ([f'instance {instance}' for instance in self.remaining_instances]
                + (['instances unknown'] if self.instance_query_failed else [])
                + (['launch reply lost; the guest shutdown timer is the protection'] if self.launch_reply_lost else [])
                + ([f'bucket {self.bucket}'] if self.bucket_created else [])
                + (['group lookup failed: gate-trial-' + self.run_id] if self.group_query_failed else [])
                + ([f'group {self.group}'] if self.group else []))

    def delete_group(self) -> None:
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

    def run(self, repo: Path, step: str, sizes: Sequence[int], runs: int, reserve: float) -> int:
        with trial_signals():
            try:
                return self.run_step(repo, step, sizes, runs, reserve)
            except KeyboardInterrupt:
                self.interrupted = True
                return 130

    def run_step(self, repo: Path, step: str, sizes: Sequence[int], runs: int, reserve: float) -> int:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        began = self.clock()
        bundle_bytes = 0
        try:
            with tempfile.TemporaryDirectory() as folder:
                bundle = Path(folder) / 'source.bundle'
                self.commit = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
                print('Commit: ' + self.commit, flush=True)
                _ = subprocess.run(['git', '-C', str(repo), 'bundle', 'create', str(bundle), 'HEAD'], check=True, capture_output=True)
                bundle_bytes = bundle.stat().st_size
                self.sweep(stopped=True)
                self.discover_subnets()
                self.setup(bundle)
                for number in range(1, runs + 1):
                    for size in sizes:
                        attempt = Attempt(size, number, 'Spot gate' if step == 'size' else 'cold launch')
                        self.attempts.append(attempt)
                        if self.remaining_instances or self.instance_query_failed or self.launch_reply_lost:
                            attempt.error = ('A launch reply remains lost.' if self.launch_reply_lost
                                             else 'Instance cleanup remains incomplete.')
                            print(self.row(attempt), flush=True)
                            continue
                        self.measure(attempt, step, reserve)
                        self.puts += 2 if step == 'size' else 4
                        if self.shared_prices:
                            _, _, put, get, _ = self.shared_prices
                            self.s3_cost = put * self.puts + get * self.gets
        except KeyboardInterrupt:
            self.interrupted = True
        finally:
            with blocked_trial_signals():
                self.cleanup()
                if self.shared_prices:
                    _, _, put, get, storage = self.shared_prices
                    self.s3_cost = (put * self.puts + get * self.gets + storage * max(bundle_bytes, self.object_bytes) / 2**30
                                    * max(0, self.clock() - began) / (730 * 3600))
                charged = [row for row in self.attempts if row.instance_id]
                for row in charged:
                    row.dollars += self.s3_cost / len(charged)
                self.save()
                _, content = self.report()
                print(content, flush=True)
                print(self.directory / f'gate-trial-{self.run_id}.json')
                print(self.directory / f'gate-trial-{self.run_id}.txt')
        return 130 if self.interrupted else 1 if self.cleanup_errors else 0


def main(argv: Sequence[str], settings_path: Path, directory: Path) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument('--step', choices=('size', 'start'), required=True)
    _ = parser.add_argument('--repo', type=Path, required=True)
    _ = parser.add_argument('--approve-resources', action='store_true', required=True)
    _ = parser.add_argument('--sizes')
    _ = parser.add_argument('--size', type=int)
    _ = parser.add_argument('--runs', type=int)
    class Arguments(argparse.Namespace):
        step: str = ''
        repo: Path = Path()
        sizes: str | None = None
        size: int | None = None
        runs: int | None = None
    args = parser.parse_args(list(argv), namespace=Arguments())
    if args.step == 'start' and args.size is None:
        parser.error('--step start requires --size')
    options = conf.read(settings_path)
    size_list = args.sizes if args.sizes is not None else options.text('gate_trial_default_sizes')
    sizes = [args.size] if args.size is not None else [int(value) for value in size_list.split(',')]
    runs = args.runs if args.runs is not None else options.whole('gate_trial_' + args.step + '_runs')
    if any(size not in (8, 16, 32, 64) for size in sizes) or runs <= 0:
        parser.error('Use sizes 8, 16, 32, or 64 and a positive run count.')
    reserve = options.number('gate_trial_' + args.step + '_reserve_usd')
    policy = read_settings(settings_path)
    costs = ledger.costs_from_evidence(ledger.CostLedger(directory).read_snapshot(), policy, int(time.time()))
    print(f'Today: ${costs.daily.usd}; this month: ${costs.monthly.usd}; step reserve: ${reserve}', flush=True)
    if min(policy.daily_limit_usd - costs.daily.usd, policy.monthly_limit_usd - costs.monthly.usd) < Decimal(str(reserve)):
        print('The remaining cap room is less than the step reserve.')
        return 1
    settings = TrialSettings.read(settings_path)
    admin = admin_secrets.AdminSecretSession(settings_path)
    credentials = admin.get()
    api = AwsCli(settings.profile, settings.region, settings.timeout_s, admin.executor(execute_command))
    try:
        return GateTrial(api, settings, directory, SigningKey(credentials.aws_access_key_id, credentials.aws_secret_access_key)).run(
            args.repo.resolve(), args.step, sizes, runs, reserve)
    except (AwsUnavailable, OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print('gate-trial: ' + admin.redact(str(error)))
        return 1
