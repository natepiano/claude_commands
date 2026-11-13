"""Run the common trial and production guest on a disposable gate instance."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import hmac
import json
import inspect
import shlex
from urllib.parse import quote
from typing import TYPE_CHECKING
from enum import StrEnum
import math
from scripts.aws_target import ledger

if TYPE_CHECKING:
    from scripts.production.merge_gate import GateRun

from scripts.lib import conf
from scripts.lib.python_startup import sitecustomize as python_startup
from scripts.test_times import gate_record


@dataclass(frozen=True, repr=False)
class SigningKey:
    access: str
    secret: str
    token: str = ''


def presign(method: str, host: str, key: str, region: str, credentials: SigningKey,
            at: datetime, expires: int) -> str:
    """Sign one S3 object URL with the documented SigV4 query algorithm."""
    stamp = at.strftime('%Y%m%dT%H%M%SZ')
    scope = f'{stamp[:8]}/{region}/s3/aws4_request'
    parameters = {'X-Amz-Algorithm': 'AWS4-HMAC-SHA256',
                  'X-Amz-Credential': f'{credentials.access}/{scope}',
                  'X-Amz-Date': stamp, 'X-Amz-Expires': str(expires), 'X-Amz-SignedHeaders': 'host'}
    if credentials.token:
        parameters['X-Amz-Security-Token'] = credentials.token
    query = '&'.join(f'{quote(k, safe="")}={quote(v, safe="")}' for k, v in sorted(parameters.items()))
    path = '/' + quote(key, safe='/~')
    canonical = f'{method}\n{path}\n{query}\nhost:{host}\n\nhost\nUNSIGNED-PAYLOAD'
    signing = ('AWS4' + credentials.secret).encode()
    for value in (stamp[:8], region, 's3', 'aws4_request'):
        signing = hmac.digest(signing, value.encode(), 'sha256')
    content = f'AWS4-HMAC-SHA256\n{stamp}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}'
    signature = hmac.new(signing, content.encode(), hashlib.sha256).hexdigest()
    return f'https://{host}{path}?{query}&X-Amz-Signature={signature}'


class TrialVariant(StrEnum):
    BASE = 'base'
    CHANGED = 'changed'


RUN_FIELDS = {'module', 'variant', 'round', 'cpu_s', 'peak_mib', 'wall_s', 'status', 'tests',
              'failures', 'errors', 'instance_type', 'box'}


def validate_paired_result(row: dict[str, object], request: dict[str, object], instance: str) -> None:
    """Validate the task identity and each measured sample."""
    for key in ('attempt', 'commit', 'tree', 'base', 'changed', 'module', 'rounds'):
        if row.get(key) != request[key]:
            raise ValueError('The paired result differs from ' + key)
    seen: set[tuple[str, int]] = set()
    for sample in ledger.rows(row, 'runs'):
        if set(sample) != RUN_FIELDS or sample['module'] != request['module']:
            raise ValueError('The paired sample has different fields or module')
        variant = TrialVariant(ledger.text(sample, 'variant'))
        number = ledger.instant(sample, 'round')
        if not 1 <= number <= ledger.instant(request, 'rounds') or (variant, number) in seen:
            raise ValueError('The paired sample has a duplicate or invalid round')
        seen.add((variant, number))
        if type(sample['status']) is not int:
            raise ValueError('The paired status must be an integer')
        for field in ('cpu_s', 'peak_mib', 'wall_s', 'tests', 'failures', 'errors'):
            value = sample[field]
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError('The paired sample has an invalid ' + field)
            if field in ('tests', 'failures', 'errors') and type(value) is not int:
                raise ValueError('The paired count must be an integer')
        if sample['box'] not in ('', instance):
            raise ValueError('The paired sample has a different box')
        sample['box'] = instance
    if row.get('verdict') not in ('pass', 'fail', 'error'):
        raise ValueError('The paired result has no verdict')


# The installed controller supplies the supervisor for both tested trees.
PAIRED_GUEST = r'''from pathlib import Path
import math, signal, tempfile

def paired_trial():
    rows = []
    reply = {key: gate_request[key] for key in ('attempt', 'commit', 'tree', 'base', 'changed', 'module', 'rounds')}
    reply.update(timestamps, runs=rows, gate_s=0, verdict='error')
    started = time.monotonic()
    active = None
    adopt_descendants()
    log = root / 'gate.log'
    def put(url, content):
        subprocess.run(['curl', '--fail', '--silent', '--show-error', '--max-time', '30',
                        '-X', 'PUT', '--data-binary', '@-', url], input=content, check=True)
    def upload():
        put(links['log'], b''.join(path.read_bytes() for path in (root / 'boot.log', log, root / 'timeline.log') if path.exists()))
    try:
        repo = root / 'repo'
        subprocess.run(['git', 'clone', gate_request['clone_url'], str(repo)], check=True)
        trees = {}
        for variant in ('base', 'changed'):
            revision = gate_request[variant]
            path = root / variant
            subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '--detach', str(path), revision['commit']], check=True)
            tree = subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD^{tree}'], text=True).strip()
            if tree != revision['tree']:
                raise ValueError('The source tree differs from the request')
            trees[variant] = path
        (root / 'measure').touch()
        with (root / 'upload.lock').open('a') as lock:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
            for round_number in range(gate_request['rounds'] + 1):
                warming = round_number == 0
                variants = ('base', 'changed') if warming or round_number % 2 else ('changed', 'base')
                remaining = gate_request['expires'] - time.time() - gate_request['delivery_s']
                budget = min(gate_request['module_time_limit_s'], max(0, remaining / (2 * (gate_request['rounds'] - round_number + 1))))
                for variant in variants:
                    stage('warm-up ' + variant if warming else f"round {round_number} of {gate_request['rounds']} {variant}")
                    upload()
                    repo = trees[variant]
                    subprocess.run(['git', '-C', str(repo), 'reset', '--hard'], check=True, capture_output=True)
                    subprocess.run(['git', '-C', str(repo), 'clean', '-fdx', '-e', '__pycache__/'], check=True, capture_output=True)
                    with tempfile.TemporaryDirectory(dir=root) as temporary:
                        work = Path(temporary)
                        environment = test_module_environment(work, dict(os.environ))
                        if warming:
                            environment.pop('TEST_TIMES_OUT', None)
                        environment['CONF_DIR'] = str(repo / 'config')
                        home = work / 'home'
                        (home / '.claude').mkdir(parents=True)
                        (home / '.claude/config').symlink_to(repo / 'config', target_is_directory=True)
                        (home / '.claude/scripts').symlink_to(repo / 'scripts', target_is_directory=True)
                        environment['HOME'] = str(home)
                        folder, name = gate_request['module'].rsplit('/', 1)
                        row = dict(module=gate_request['module'], variant=variant, round=round_number,
                                   cpu_s=None, peak_mib=None, wall_s=None, status=124,
                                   tests=None, failures=None, errors=None,
                                   instance_type=links['instance_type'], box='')
                        (work / 'output').touch()
                        if budget > 0:
                            before = time.monotonic()
                            with (work / 'output').open('wb') as output:
                                active = subprocess.Popen(['python3', 'scripts/test_times/module_run.py', 'scripts/' + folder, name],
                                    cwd=repo, env=environment, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
                                peak = 0.0
                                timed_out = False
                                try:
                                    while active.poll() is None:
                                        if not warming:
                                            peak = max(peak, process_memory_mib({active.pid}).get(active.pid, 0.0))
                                        remaining = budget - (time.monotonic() - before)
                                        if remaining <= 0:
                                            timed_out = True
                                            send_process_group(active.pid, signal.SIGKILL)
                                            break
                                        try:
                                            active.wait(timeout=min(0.02, remaining))
                                        except subprocess.TimeoutExpired:
                                            pass
                                    status = active.wait()
                                    row.update(status=124 if timed_out else status, peak_mib=peak)
                                finally:
                                    group = active.pid
                                    try:
                                        send_process_group(group, signal.SIGKILL)
                                    except ProcessLookupError:
                                        pass
                                    active.wait()
                                    while True:
                                        try:
                                            os.waitpid(-group, 0)
                                        except ChildProcessError:
                                            break
                                    active = None
                        timing = work / 'test-times.json'
                        if not warming and timing.exists():
                            measured = json.loads(timing.read_text())
                            for field, source in (('cpu_s', 'cpu_s'), ('wall_s', 'wall_s'), ('tests', 'tests_run'),
                                                  ('failures', 'failures'), ('errors', 'errors')):
                                value = measured[source]
                                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                                    raise ValueError('The timing output has an invalid ' + source)
                                if field in ('tests', 'failures', 'errors') and type(value) is not int:
                                    raise ValueError('The timing count must be an integer')
                                row[field] = value
                        if not warming:
                            rows.append(row)
                        with log.open('a') as output:
                            output.write((work / 'output').read_text(errors='replace') + '\n')
                            if not warming:
                                output.write(json.dumps(row) + '\n')
        stage('finish')
        reply['verdict'] = 'pass' if len(rows) == 2 * gate_request['rounds'] and all(row['status'] == 0 and row['tests'] is not None for row in rows) else 'fail'
    except BaseException:
        reply['error'] = traceback.format_exc()
        with log.open('a') as output:
            output.write(reply['error'])
    finally:
        if active is not None:
            send_process_group(active.pid, signal.SIGKILL)
            active.wait()
        reply['gate_s'] = time.monotonic() - started
        try:
            upload()
        except Exception as error:
            reply['log_error'] = str(error)
        put(links['result'], json.dumps(reply).encode())

paired_trial()
'''


def paired_supervisor() -> str:
    """Embed the installed gate environment and memory sampler."""
    from scripts.production import merge_gate
    from scripts.lib.descendants import adopt_descendants
    from scripts.lib.process_group import check_process_group, send_process_group
    environment = inspect.getsource(merge_gate.test_module_environment).replace(
        'Path(__file__).resolve().parents[1] / "lib/py"', 'sys.executable')
    return ('from pathlib import Path\nimport ctypes\nfrom typing import cast\n'
            + inspect.getsource(adopt_descendants) + '\n' + environment + '\n'
            + inspect.getsource(merge_gate.swapped_kib) + '\n'
            + inspect.getsource(merge_gate.process_memory_mib) + '\n'
            + inspect.getsource(check_process_group) + '\n'
            + inspect.getsource(send_process_group) + '\n' + PAIRED_GUEST)


# This program runs inside the instance's tool shell, not on natedev.
LEGACY_GUEST = r'''import concurrent.futures, contextlib, datetime, json, os, pathlib, sqlite3, subprocess, sys, threading, time, traceback
root = pathlib.Path(os.environ.pop('GATE_BOX_DIR', '/var/lib/gate-trial'))
os.environ.pop('box_dir', None)
links = json.loads((root / 'links.json').read_text())
if links.get('production'):
    gate_request = json.loads((root / 'request.json').read_text())
timestamps = {'nix_ready_at': time.time()}
started_at = os.environ.pop('GATE_USER_DATA_STARTED_AT', '')
if started_at:
    timestamps['user_data_started_at'] = int(started_at)
try:
    timestamps['kernel_boot_at'] = next(int(line.split()[1]) for line in pathlib.Path('/proc/stat').read_text().splitlines()
                                        if line.startswith('btime '))
except (OSError, ValueError, StopIteration):
    pass
# Read cached metadata without a network request.
for metadata_path in ('/etc/ec2-metadata/instance-identity-document', '/run/cloud-init/instance-data.json'):
    try:
        metadata = json.loads(pathlib.Path(metadata_path).read_text())
        if 'ds' in metadata:
            metadata = metadata['ds']['dynamic']['instance-identity']['document']
        timestamps['instance_running_at'] = datetime.datetime.fromisoformat(metadata['pendingTime'].replace('Z', '+00:00')).timestamp()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        continue
    break
def stage(name):
    try:
        with (root / 'boot.log').open('a') as output:
            output.write(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()) + ' stage=' + name + '\n')
    except OSError:
        pass
links['boot'] = 'warm' if (root / 'prepared').exists() else 'cold'
result = {**timestamps, 'verdict': 'ready', 'tests': 0, 'skips': 0, 'failed_modules': [], 'memory_peak_bytes': None}
log = root / 'gate.log'
def get(url, destination):
    subprocess.run(['curl', '--fail', '--silent', '--show-error', '--max-time', '60', url, '-o', str(destination)], check=True)
def put(url, content):
    subprocess.run(['curl', '--fail', '--silent', '--show-error', '--max-time', '30', '-X', 'PUT', '--data-binary', '@-', url],
                   input=content, check=True)
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
    def tool_version(name):
        version = subprocess.run([name, '--version'], capture_output=True, text=True)
        return (version.stdout or version.stderr).strip()
    names = ('git', 'python3', 'cargo', 'rustc', 'rustdoc', 'rustup', 'cargo-nextest', 'uv', 'sqlite3')
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(names)) as tools:
        for name, version in zip(names, tools.map(tool_version, names)):
            result['tools'][name] = version
    stage('tools checked')
    result['tools']['python_sqlite'] = sqlite3.sqlite_version
    if links['step'] == 'start':
        result['tools_ready_at'] = time.time()
        links['boot'] = 'warm' if (root / 'prepared').exists() else 'cold'
    else:
        repo = root / 'repo'
        if links.get('production'):
            subprocess.run(['git', 'clone', gate_request['clone_url'], str(repo)], check=True)
            if links.get('source'):
                get(links['source'], root / 'source.bundle')
                subprocess.run(['git', '-C', str(repo), 'fetch', str(root / 'source.bundle'), links['commit']], check=True)
            subprocess.run(['git', '-C', str(repo), 'update-ref', 'refs/remotes/origin/main', gate_request['origin_main']], check=True)
        else:
            get(links['source'], root / 'source.bundle')
            subprocess.run(['git', 'clone', str(root / 'source.bundle'), str(repo)], check=True)
        subprocess.run(['git', '-C', str(repo), 'checkout', '--detach', links['commit']], check=True)
        if links.get('production'):
            from subprocess import check_output
            tree = check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD^{tree}'], text=True).strip()
            if tree != links['tree']:
                raise ValueError('The source tree differs from the request')
            subprocess.run(['git', '-C', str(repo), 'remote', 'set-url', 'origin', links['origin']], check=True)
        stage('source ready')
        home = pathlib.Path.home() / '.claude'
        home.mkdir(exist_ok=True)
        (home / 'config').symlink_to(repo / 'config', target_is_directory=True)
        os.chdir(repo)
        from types import ModuleType
        startup = ModuleType('_devtools_python_startup')
        startup.__file__ = __GATE_STARTUP_FILENAME__
        sys.modules[startup.__name__] = startup
        exec(__GATE_STARTUP_SOURCE__, startup.__dict__)
        startup.select_checkout(sys.argv)
        from scripts.production import merge_gate
        os.environ['HERDR_BIN'] = '/nonexistent/herdr'
        timings = {}
        if 'module_seconds' in links:
            get(links['module_seconds'], root / 'module_seconds.json')
            timings = json.loads((root / 'module_seconds.json').read_text())
        result['tools_ready_at'] = time.time()
        result['module_time_limit_s'] = links['module_time_limit_s']
        result['max_running_jobs'] = os.cpu_count() or 1
        result['vcpus'] = result['max_running_jobs']
        sample_memory()
        memory_thread = threading.Thread(target=monitor_memory, args=(memory_stop,), daemon=True)
        memory_thread.start()
        stage('tests start')
        with log.open('w') as output, contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with merge_gate.rollback_signals():
                cpu_started = busy_cpu_seconds()
                result['gate_started_at'] = time.time()
                if links.get('production'):
                    gate = merge_gate.run_suites(repo, full=True, machine=merge_gate.WholeMachine(timings),
                                                module_time_limit_s=links['module_time_limit_s'],
                                                selected=merge_gate.SelectedModules(tuple(gate_request['modules'])),
                                                mode=merge_gate.SuiteMode(links['mode']))
                else:
                    gate = merge_gate.run_suites(repo, full=True, machine=merge_gate.WholeMachine(timings),
                                                module_time_limit_s=links['module_time_limit_s'])
                if links.get('production'):
                    from scripts.aws_target.gate_box import encode_run, completion_record
                    result['record'] = completion_record(gate, links['attempt'], links['commit'], tree, os.uname().nodename,
                                                         result['gate_started_at'], time.time(), links['mode'])
                    dirty = gate_request['dirty_paths']
                    result['record'].update(head=gate_request['head'], dirty=bool(dirty), dirty_paths=dirty,
                                            dirty_path_count=len(dirty))
                    result.update(attempt=links['attempt'], commit=links['commit'], tree=tree,
                                  host=os.uname().nodename, gate=encode_run(gate))
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
        result['cpu_use_percent'] = 100 * result['busy_cpu_s'] / max(0.001, result['vcpus'] * result['gate_s'])
    if memory_thread is not None:
        memory_stop.set()
        memory_thread.join()
        sample_memory()
    try:
        if links.get('production'):
            with log.open('a') as output:
                output.write('Cloud gate: uploading\n')
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


GUEST = (LEGACY_GUEST.replace('__GATE_STARTUP_SOURCE__', repr(inspect.getsource(python_startup)))
    .replace('__GATE_STARTUP_FILENAME__', repr(python_startup.__file__)).replace("links['boot'] =",
    "if links.get('production') and gate_request.get('task') == 'paired':\n    exec(" + repr(paired_supervisor())
    + ")\n    raise SystemExit(0)\nlinks['boot'] =", 1))


def log_upload(links: dict[str, str]) -> str:
    transfer = f'curl --fail --silent --show-error --max-time 30 -X PUT --data-binary @- {shlex.quote(links["log"])}'
    return f'''{{ cat "${{box_dir:-/var/lib/gate-trial}}/boot.log" "${{box_dir:-/var/lib/gate-trial}}/gate.log" "${{box_dir:-/var/lib/gate-trial}}/timeline.log" 2>/dev/null || :
    ps -eo etimes,pcpu,rss,args --sort=-etimes 2>/dev/null | awk '!/curl/ {{print substr($0,1,200); if (++n==60) exit}}' || :
    printf '\\n'
    date -u '+UTC: %Y-%m-%dT%H:%M:%SZ'
}} | {transfer} || :'''


def log_push_loop(links: dict[str, str], interval_s: float) -> str:
    lock_start = ('if [ -e "$box_dir/measure" ]; then continue; fi\n        (\n        flock -x 9\n'
                  + '        if [ -e "$box_dir/measure" ]; then exit 0; fi\n'
                  if links.get('paired') else '')
    lock_end = ') 9> "$box_dir/upload.lock"' if links.get('paired') else ''
    return f'''push_logs() {{
    cpu_ticks() {{ awk '/^cpu / {{for (i=2;i<=9;i++) t+=$i; printf "%.0f %.0f\\n", t, $5+$6+$9; exit}}' /proc/stat; }}
    set -- $(cpu_ticks)
    previous_total=$1 previous_idle=$2
    while sleep {interval_s:g}; do
        {lock_start}        set -- $(cpu_ticks)
        busy=$(awk -v total="$(($1-previous_total))" -v idle="$(($2-previous_idle))" 'BEGIN {{printf "%.2f", (total>0 ? 100*(total-idle)/total : 0)}}')
        previous_total=$1 previous_idle=$2
        printf '%s loadavg=%s %s processes=%s cpu_busy_percent=%s\\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$(cat /proc/loadavg)" "$(awk '/^MemAvailable:/' /proc/meminfo)" "$(ps -e --no-headers | wc -l)" "$busy" >> "${{box_dir:-/var/lib/gate-trial}}/timeline.log" || :
        {log_upload(links)}
        {lock_end}
    done
}}
push_logs &
log_push_pid=$!
'''


def boot_trap(links: dict[str, str]) -> str:
    """Publish a shell failure before the tool shell can publish a result."""
    cold = links.get('boot_error', links['result']) if links['step'] == 'size' else links['ready-cold']
    warm = links.get('boot_error', links['result']) if links['step'] == 'size' else links['ready-warm']
    return f'''boot_failed() {{
    status=$?
    trap - EXIT
    if [ -n "${{log_push_pid:-}}" ]; then kill "$log_push_pid" 2>/dev/null || :; fi
    if [ "$status" -ne 0 ]; then
        destination={shlex.quote(cold)}
        if [ -e "${{box_dir:-/var/lib/gate-trial}}/prepared" ]; then destination={shlex.quote(warm)}; fi
        {log_upload(links)}
        printf '{{"verdict":"error","error":"Tool preparation failed (exit %s)"}}' "$status" > "${{box_dir:-/var/lib/gate-trial}}/boot-error.json"
        curl --fail --silent --show-error --max-time 30 -X PUT --data-binary @"${{box_dir:-/var/lib/gate-trial}}/boot-error.json" "$destination" || :
    fi
    exit "$status"
}}
trap boot_failed EXIT
'''


GATE_IMAGE_OWNER_TAG = 'cloud-claude-gate-image'


def user_data(links: dict[str, str], revision: str, lifetime_minutes: int, log_push_s: float,
              module_time_limit_s: float) -> str:
    """Run directly in amazon-init on each boot."""
    packages = conf.read_or_repository('cloud_gate').words('packages')
    tools = ' '.join(shlex.quote(f'github:NixOS/nixpkgs/{revision}#{name}') for name in packages)
    def download(key: str, name: str) -> str:
        url, destination = shlex.quote(links[key]), '"$box_dir/' + name + '"'
        return f'curl --fail --silent --show-error --max-time 60 {url} -o {destination}\n'
    downloads = download('guest', 'guest.py')
    if 'request' in links:
        downloads += download('request', 'request.json')
    # The boot script's 077 protects its files; tests expect the normal umask.
    session = '''umask 022
printf '%s stage=tools ready\\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
''' + downloads + f'''ln -sf "$(command -v bash)" /bin/bash
ln -sf "$(command -v zsh)" /bin/zsh
mkdir -p "$box_dir/tool-profile"
cp -a /run/current-system/sw/bin/. "$box_dir/tool-profile/"
ln -sf "$(command -v python3)" "$box_dir/tool-profile/python3"
ln -sf "$(command -v chromium)" "$box_dir/tool-profile/google-chrome"
mount --bind "$box_dir/tool-profile" /run/current-system/sw/bin
chown -R gate-trial:users "$box_dir"
runuser -u gate-trial -- env HOME=/home/gate-trial XDG_RUNTIME_DIR=/run/user/$(id -u gate-trial) DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u gate-trial)/bus bash -c '
tool_path=$PATH
unset __NIXOS_SET_ENVIRONMENT_DONE __ETC_PROFILE_SOURCED
. /etc/profile
export PATH="$tool_path:$PATH"
exec python3 "$box_dir/guest.py"'
{('shutdown -h now' if links.get('production') else '')}
'''
    deadline = (f'systemd-run --unit=cloud-gate-expiry --on-calendar=@{int(links["expires"])} /run/current-system/sw/bin/shutdown -h now'
                if links.get('production') else f'shutdown -h +{lifetime_minutes}')
    return f'''#!/bin/sh
GATE_USER_DATA_STARTED_AT=$(date -u +%s)
export GATE_USER_DATA_STARTED_AT
{deadline}
set -eu
umask 077
box_dir=${{GATE_BOX_DIR:-/var/lib/gate-trial}}
export box_dir GATE_BOX_DIR="$box_dir"
mkdir -p "$box_dir"
: > "$box_dir/boot.log"
exec >> "$box_dir/boot.log" 2>&1
export PATH=$PATH:/run/current-system/sw/bin
{boot_trap(links)}printf '%s stage=boot\\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
{log_upload(links)}
{log_push_loop(links, log_push_s)}cat > "$box_dir/links.json" <<'GATE_LINKS'
{json.dumps({**links, 'module_time_limit_s': module_time_limit_s})}
GATE_LINKS
touch "$box_dir/timeline.log"
id -u gate-trial >/dev/null 2>&1 || useradd -m -g users gate-trial
loginctl enable-linger gate-trial
export PYTHONUNBUFFERED=1
nix --extra-experimental-features 'nix-command flakes' shell {tools} --command bash -ec {shlex.quote(session)}
'''



def encode_run(run: object) -> dict[str, object]:
    """Return the complete gate result for object transfer."""
    from scripts.production.merge_gate import GateRun, GateJobResult
    from dataclasses import asdict
    if not isinstance(run, GateRun):
        raise TypeError('expected a completed gate')

    def encode_job(job: GateJobResult) -> dict[str, object]:
        row: dict[str, object] = asdict(job)
        row['kind'] = job.kind.name
        del row['wall_clock']
        if isinstance(job.wall_clock, gate_record.WallClockSpan):
            row['started_at'] = job.wall_clock.started_at
            row['ended_at'] = job.wall_clock.ended_at
        return row

    return {'jobs': [encode_job(job) for job in run.jobs],
            'executions': [encode_job(job) for job in run.executions],
            'seconds': run.seconds, 'peak_memory_mib': run.peak_memory_mib,
            'tests_reused': run.tests_reused, 'selection_certified': run.selection_certified}


def decode_run(value: object) -> GateRun:
    """Validate the transferred result before computing its verdict."""
    from scripts.aws_target.ledger import mapping, rows, text
    from scripts.production.merge_gate import GateRun, GateJobResult, GateJobKind
    import math
    from typing import cast

    def number(row: dict[str, object], key: str) -> float:
        value = row[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f'{key}: expected a finite nonnegative number')
        return float(value)

    def job(row: dict[str, object]) -> GateJobResult:
        status = row['returncode']
        if type(status) is not int:
            raise ValueError('expected an integer status')
        times = mapping(row['times'])
        measured = (cast(gate_record.ModuleTimes, cast(object, times)) if 'tests_run' in times
                    else gate_record.NoModuleTimes(str(times.get('reason', 'timing does not apply'))))
        restart = row['restart']
        if restart not in ('', 'hold'):
            raise ValueError('unknown restart')
        wall_clock_fields = {key: number(row, key) for key in ('started_at', 'ended_at') if key in row}
        if len(wall_clock_fields) == 2:
            wall_clock: gate_record.WallClockSpan | gate_record.WallClockNotRecorded = gate_record.WallClockSpan(
                wall_clock_fields['started_at'], wall_clock_fields['ended_at'])
        elif wall_clock_fields:
            wall_clock = gate_record.WallClockNotRecorded('box result has only one module time')
        else:
            wall_clock = gate_record.WallClockNotRecorded('box result has no module times')
        return GateJobResult(text(row, 'suite'), text(row, 'module'), status, number(row, 'seconds'),
                             str(row['output']), str(row['killed_for']), row['tripwire_tripped'] is True,
                             GateJobKind[text(row, 'kind')], number(row, 'cpu_seconds'), measured,
                             number(row, 'peak_mib'), restart, wall_clock=wall_clock)

    row = mapping(value)
    for key in ('tests_reused', 'selection_certified'):
        if type(row[key]) is not bool:
            raise ValueError(f'{key}: expected a boolean')
    return GateRun(tuple(job(item) for item in rows(row, 'jobs')), number(row, 'seconds'),
                   number(row, 'peak_memory_mib'), row['tests_reused'] is True,
                   row['selection_certified'] is True, tuple(job(item) for item in rows(row, 'executions')))


def execution_records(value: GateRun) -> list[gate_record.Execution]:
    """Keep every attempt's output for the gate record writer."""
    return [gate_record.Execution(job.suite, job.module, job.seconds, job.cpu_seconds, job.peak_mib,
                                  job.returncode, job.restart, job.times, job.output,
                                  wall_clock=job.wall_clock)
            for job in value.executions or value.jobs]


def completion_record(value: object, attempt: str, commit: str, tree: str, host: str,
                      started: float, ended: float, mode: str) -> dict[str, object]:
    """Return an explicit buildlog record with the remote host identity."""
    from datetime import UTC
    from scripts.buildlog import record
    from scripts.production.merge_gate import GateRun
    import os
    if not isinstance(value, GateRun):
        raise TypeError('expected a completed gate')
    modules = gate_record.module_runs(execution_records(value), gate_record.slow_test_s())
    suites = [{'suite': suite, 'wall_s': max((job.seconds for job in value.jobs if job.suite == suite), default=0),
               'status': int(any(job.returncode for job in value.jobs if job.suite == suite))}
              for suite in sorted({job.suite for job in value.jobs})]
    return {'kind': 'gate_run', 'v': record.RECORD_VERSION, 'id': 'gate-' + attempt, 'host': host,
            'started_at': datetime.fromtimestamp(started, UTC).isoformat(),
            'ended_at': datetime.fromtimestamp(ended, UTC).isoformat(), 'wall_s': value.seconds,
            'hold_wait_s': 0, 'selection': 'affected' if mode == 'targeted-test' else 'full',
            'reason': 'cloud gate', 'status': int(not value.passed), 'max_jobs': os.cpu_count() or 1,
            'peak_mib': value.peak_memory_mib, 'cap_mib': 0, 'checkout': '/var/lib/gate-trial/repo',
            'branch': 'HEAD', 'sha': commit, 'tree': tree, 'source': 'cloud-gate', 'step': mode,
            'aborted': False, 'dirty': False, 'dirty_paths': [], 'dirty_path_count': 0,
            'suites': suites, 'modules': modules, 'slow_test_s': gate_record.slow_test_s(),
            'tests_run': sum(module['tests_run'] for module in modules)}
