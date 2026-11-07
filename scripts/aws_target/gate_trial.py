"""Measure disposable gate instances from the user's terminal."""
from __future__ import annotations

import argparse
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
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
import threading
import time
from typing import cast, final
from urllib.parse import quote
from uuid import uuid4

from scripts.aws_target import admin_secrets, ledger
from scripts.aws_target.aws_target import AwsCli, AwsUnavailable, execute_command
from scripts.aws_target.config import read_settings
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.production.merge_gate import gate_state_dir, read_measures


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


NIXOS_LOCK = Path('/etc/nixos/flake.lock')


def host_nixpkgs_revision() -> str:
    """Read the host's locked nixpkgs input before creating trial resources."""
    lock = ledger.mapping(cast(object, json.loads(NIXOS_LOCK.read_text())))
    nodes = ledger.mapping(lock['nodes'])
    inputs = ledger.mapping(ledger.mapping(nodes[ledger.text(lock, 'root')])['inputs'])
    locked = ledger.mapping(ledger.mapping(nodes[ledger.text(inputs, 'nixpkgs')])['locked'])
    return ledger.text(locked, 'rev')


def host_basedpyright_version() -> str:
    """Read the host's basedpyright version before creating trial resources."""
    return subprocess.check_output(['basedpyright', '--version'], text=True).split()[1]


# This program runs inside the instance's tool shell, not on natedev.
GUEST = r'''import contextlib, json, os, pathlib, sqlite3, subprocess, sys, threading, time, traceback, urllib.request
root = pathlib.Path('/var/lib/gate-trial')
links = json.loads((root / 'links.json').read_text())
links['boot'] = 'warm' if (root / 'prepared').exists() else 'cold'
result = {'nix_ready_at': time.time(), 'verdict': 'ready', 'tests': 0, 'skips': 0, 'failed_modules': [], 'memory_peak_bytes': None}
log = root / 'gate.log'
def put(url, content):
    with urllib.request.urlopen(urllib.request.Request(url, data=content, method='PUT'), timeout=30) as response:
        response.read()
def publish(name):
    put(links[name], json.dumps(result).encode())
def busy_cpu_seconds():
    ticks = list(map(int, pathlib.Path('/proc/stat').read_text().splitlines()[0].split()[1:9]))
    return (sum(ticks) - ticks[3] - ticks[4] - ticks[7]) / os.sysconf('SC_CLK_TCK')
def sample_memory():
    try:
        fields = dict(line.split(':', 1) for line in pathlib.Path('/proc/meminfo').read_text().splitlines())
        used = (int(fields['MemTotal'].split()[0]) - int(fields['MemAvailable'].split()[0])) * 1024
        result['memory_peak_bytes'] = max(result['memory_peak_bytes'] or 0, used)
    except (OSError, ValueError, KeyError):
        pass
def monitor_memory(stop):
    while True:
        sample_memory()
        if stop.wait(2):
            return
memory_stop = threading.Event()
memory_thread = None
cpu_started = None
try:
    result['tools'] = {}
    for name in ('git', 'python3', 'basedpyright', 'cargo', 'rustc', 'rustdoc', 'rustup', 'cargo-nextest', 'uv', 'sqlite3'):
        version = subprocess.run([name, '--version'], capture_output=True, text=True)
        result['tools'][name] = (version.stdout or version.stderr).strip()
    result['tools']['python_sqlite'] = sqlite3.sqlite_version
    if links['step'] == 'start':
        result['tools_ready_at'] = time.time()
        links['boot'] = 'warm' if (root / 'prepared').exists() else 'cold'
    else:
        with urllib.request.urlopen(links['source'], timeout=60) as response:
            (root / 'source.bundle').write_bytes(response.read())
        subprocess.run(['git', 'clone', str(root / 'source.bundle'), str(root / 'repo')], check=True)
        repo = root / 'repo'
        subprocess.run(['git', '-C', str(repo), 'checkout', '--detach', links['commit']], check=True)
        home = pathlib.Path.home() / '.claude'
        home.mkdir(exist_ok=True)
        (home / 'config').symlink_to(repo / 'config', target_is_directory=True)
        os.chdir(repo)
        sys.path.insert(0, str(repo))
        from scripts.production import merge_gate
        timings = {}
        if 'module_seconds' in links:
            with urllib.request.urlopen(links['module_seconds'], timeout=60) as response:
                timings = json.loads(response.read())
        preparation = merge_gate.typecheck_python()
        if isinstance(preparation, str):
            raise RuntimeError(preparation)
        result['tools_ready_at'] = time.time()
        result['module_time_limit_s'] = links['module_time_limit_s']
        result['max_running_jobs'] = os.cpu_count() or 1
        result['vcpus'] = result['max_running_jobs']
        sample_memory()
        memory_thread = threading.Thread(target=monitor_memory, args=(memory_stop,), daemon=True)
        memory_thread.start()
        with log.open('w') as output, contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with merge_gate.rollback_signals():
                cpu_started = busy_cpu_seconds()
                result['gate_started_at'] = time.time()
                gate = merge_gate.run_suites(repo, type_dirs=['scripts'], full=True, machine=merge_gate.WholeMachine(timings),
                                             module_time_limit_s=links['module_time_limit_s'])
                result['gate_finished_at'] = time.time()
                cpu_finished = busy_cpu_seconds()
        result['verdict'] = 'pass' if gate.passed else 'fail'
        result['modules'] = []
        for job in gate.jobs:
            measured = job.times if isinstance(job.times, dict) else {}
            result['tests'] += measured.get('tests_run', 0)
            result['skips'] += measured.get('skipped', 0)
            name = job.suite + '/' + job.module
            result['modules'].append({'module': name, 'status': job.returncode, 'tests': measured.get('tests_run', 0),
                                      'skips': measured.get('skipped', 0), 'seconds': job.seconds, 'stopped': bool(job.killed_for)})
            if job.returncode:
                result['failed_modules'].append(name)
except BaseException:
    if 'gate_started_at' in result:
        result['gate_finished_at'] = time.time()
        cpu_finished = busy_cpu_seconds()
    result['verdict'] = 'error'
    result['error'] = traceback.format_exc()
    with log.open('a') as output:
        output.write(result['error'])
finally:
    if cpu_started is not None:
        if 'gate_finished_at' not in result:
            result['gate_finished_at'] = time.time()
            cpu_finished = busy_cpu_seconds()
        result['gate_s'] = result['gate_finished_at'] - result['gate_started_at']
        result['busy_cpu_s'] = cpu_finished - cpu_started
        result['cpu_use_percent'] = 100 * result['busy_cpu_s'] / (result['vcpus'] * result['gate_s'])
    if memory_thread is not None:
        memory_stop.set()
        memory_thread.join()
        sample_memory()
    try:
        content = b''.join(path.read_bytes() for path in (root / 'boot.log', log, root / 'timeline.log') if path.exists())
        try:
            snapshot = subprocess.run(['ps', '-eo', 'etimes,pcpu,rss,args', '--sort=-etimes'], capture_output=True, text=True)
            lines = [line[:200] for line in snapshot.stdout.splitlines() if 'curl' not in line][:60]
            content += ('\n' + '\n'.join(lines) + '\n').encode()
        except (OSError, subprocess.SubprocessError):
            pass
        content += time.strftime('\nUTC: %Y-%m-%dT%H:%M:%SZ\n', time.gmtime()).encode()
        put(links['log'], content)
    except Exception as error:
        result['log_error'] = str(error)
    publish('result' if links['step'] == 'size' else 'ready-' + links['boot'])
    if links['step'] == 'start' and result['verdict'] == 'ready':
        (root / 'prepared').touch()
'''


def log_upload(links: dict[str, str]) -> str:
    return f'''{{ cat /var/lib/gate-trial/boot.log /var/lib/gate-trial/gate.log /var/lib/gate-trial/timeline.log 2>/dev/null || :
    ps -eo etimes,pcpu,rss,args --sort=-etimes 2>/dev/null | awk '!/curl/ {{print substr($0,1,200); if (++n==60) exit}}' || :
    printf '\\n'
    date -u '+UTC: %Y-%m-%dT%H:%M:%SZ'
}} | curl --fail --silent --show-error --max-time 30 -X PUT --data-binary @- {shlex.quote(links['log'])} || :'''


def log_push_loop(links: dict[str, str], interval_s: float) -> str:
    return f'''push_logs() {{
    cpu_ticks() {{ awk '/^cpu / {{for (i=2;i<=9;i++) t+=$i; printf "%.0f %.0f\\n", t, $5+$6+$9; exit}}' /proc/stat; }}
    set -- $(cpu_ticks)
    previous_total=$1 previous_idle=$2
    while sleep {interval_s:g}; do
        set -- $(cpu_ticks)
        busy=$(awk -v total="$(($1-previous_total))" -v idle="$(($2-previous_idle))" 'BEGIN {{printf "%.2f", (total>0 ? 100*(total-idle)/total : 0)}}')
        previous_total=$1 previous_idle=$2
        printf '%s loadavg=%s %s processes=%s cpu_busy_percent=%s\\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$(cat /proc/loadavg)" "$(awk '/^MemAvailable:/' /proc/meminfo)" "$(ps -e --no-headers | wc -l)" "$busy" >> /var/lib/gate-trial/timeline.log || :
        {log_upload(links)}
    done
}}
push_logs &
log_push_pid=$!
'''


def boot_trap(links: dict[str, str]) -> str:
    """Publish a shell failure before the tool shell can publish a result."""
    cold = links['result'] if links['step'] == 'size' else links['ready-cold']
    warm = links['result'] if links['step'] == 'size' else links['ready-warm']
    return f'''boot_failed() {{
    status=$?
    trap - EXIT
    if [ -n "${{log_push_pid:-}}" ]; then kill "$log_push_pid" 2>/dev/null || :; fi
    if [ "$status" -ne 0 ]; then
        destination={shlex.quote(cold)}
        if [ -e /var/lib/gate-trial/prepared ]; then destination={shlex.quote(warm)}; fi
        {log_upload(links)}
        printf '{{"verdict":"error","error":"Tool preparation failed (exit %s)"}}' "$status" > /var/lib/gate-trial/boot-error.json
        curl --fail --silent --show-error --max-time 30 -X PUT --data-binary @/var/lib/gate-trial/boot-error.json "$destination" || :
    fi
    exit "$status"
}}
trap boot_failed EXIT
'''


def user_data(links: dict[str, str], revision: str, basedpyright_version: str, lifetime_minutes: int, log_push_s: float,
              module_time_limit_s: float) -> str:
    """Run directly in amazon-init on each boot."""
    packages = ('git', 'python3', 'cargo', 'rustc', 'rustup', 'cargo-nextest',
                'uv', 'curl', 'openssh', 'rsync', 'gnupg', 'ripgrep', 'jq', 'gcc', 'pkg-config', 'tmux',
                'taplo', 'zsh', 'bash', 'gh', 'sqlite', 'chromium', 'util-linux')
    tools = ' '.join(shlex.quote(f'github:NixOS/nixpkgs/{revision}#{name}') for name in packages)
    session = f'''ln -sf "$(command -v bash)" /bin/bash
ln -sf "$(command -v zsh)" /bin/zsh
mkdir -p /run/gate-trial-bin
cp -as /run/current-system/sw/bin/. /run/gate-trial-bin/
ln -sf "$(command -v python3)" /run/gate-trial-bin/python3
ln -sf "$(command -v chromium)" /run/gate-trial-bin/google-chrome
mount --bind /run/gate-trial-bin /run/current-system/sw/bin
chown -R gate-trial:users /var/lib/gate-trial
runuser -u gate-trial -- env HOME=/home/gate-trial XDG_RUNTIME_DIR=/run/user/$(id -u gate-trial) DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u gate-trial)/bus bash -c '
tool_path=$PATH
unset __NIXOS_SET_ENVIRONMENT_DONE __ETC_PROFILE_SOURCED
. /etc/profile
export PATH="$HOME/.local/bin:$tool_path:$PATH"
uv tool install {shlex.quote('basedpyright==' + basedpyright_version)}
exec python3 /var/lib/gate-trial/guest.py'
'''
    return f'''#!/bin/sh
shutdown -h +{lifetime_minutes}
set -eu
umask 077
mkdir -p /var/lib/gate-trial
exec > /var/lib/gate-trial/boot.log 2>&1
export PATH=/run/current-system/sw/bin:$PATH
{log_push_loop(links, log_push_s)}{boot_trap(links)}cat > /var/lib/gate-trial/links.json <<'GATE_LINKS'
{json.dumps({**links, 'module_time_limit_s': module_time_limit_s})}
GATE_LINKS
cat > /var/lib/gate-trial/guest.py <<'GATE_GUEST'
{GUEST}
GATE_GUEST
touch /var/lib/gate-trial/timeline.log
id -u gate-trial >/dev/null 2>&1 || useradd -m -g users gate-trial
loginctl enable-linger gate-trial
export PYTHONUNBUFFERED=1
nix --extra-experimental-features 'nix-command flakes' shell {tools} --command bash -ec {shlex.quote(session)}
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
        self.basedpyright_version = ''

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
                + prices.put * (4 + math.floor(lifetime / self.option('log_push_s')) * (2 if step == 'start' else 1))
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
            _ = data.write_text(user_data(self.links(key, step), self.nixpkgs_revision, self.basedpyright_version, minutes,
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
                        self.puts += 2 if step == 'size' else 4
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
                self.basedpyright_version = host_basedpyright_version()
                print('Guest basedpyright version: ' + self.basedpyright_version, flush=True)
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
