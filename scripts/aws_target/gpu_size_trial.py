"""Prepare local GPU trials and run their scaler service."""
from __future__ import annotations

import argparse
import base64
import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
from typing import cast, final
from uuid import uuid4

from scripts.aws_target import cloud_gate, gate_box, ledger
from scripts.aws_target.aws_target import AwsCli, AwsUnavailable, control_lock
from scripts.aws_target.config import gate_deployment, read_settings
from scripts.lib import conf
from scripts.production import merge_gate
from scripts.production.gate_modules import GateRefused


ROOT = Path(__file__).resolve().parents[2]
SYSTEM_MODULE = ROOT / 'docs/plans/builds-aws/gpu-trial.nix'
WRAPPER = Path(__file__).with_name('gpu_trial_wrapper.py')


@dataclass(frozen=True)
class TrialLimits:
    limit_usd: Decimal
    lifetime_minutes: int
    box_extra_usd: Decimal
    spot_max_usd_per_hour: dict[str, Decimal]


def mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError('Expected an object with text keys')
    if any(not isinstance(key, str) for key in cast(dict[object, object], value)):
        raise ValueError('Expected an object with text keys')
    return cast(dict[str, object], value)


def read_json(path: Path) -> dict[str, object]:
    return mapping(cast(object, json.loads(path.read_text())))


def write_json(path: Path, value: object) -> None:
    _ = path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def settings() -> TrialLimits:
    source = conf.read_or_repository('cloud_gate')
    prices = mapping(cast(object, json.loads(source.text('gpu_size_trial_spot_max_usd_per_hour'))))
    maximums: dict[str, Decimal] = {}
    for name, value in prices.items():
        if not re.fullmatch(r'(?:g6\.(?:[248]xlarge|xlarge)|g4dn\.xlarge)', name):
            raise ValueError('The GPU trial has an unsupported type: ' + name)
        price = conf.decimal(str(value))
        if not 0 < price <= 10:
            raise ValueError('GPU trial prices must be greater than zero and at most 10 USD/hour')
        maximums[name] = price
    if not maximums:
        raise ValueError('The GPU trial needs at least one type')
    return TrialLimits(conf.decimal(source.text('gpu_size_trial_limit_usd')),
                       source.whole('gpu_size_trial_lifetime_minutes'),
                       conf.decimal(source.text('gpu_size_trial_box_extra_usd')), maximums)


def digest(path: Path) -> str:
    with path.open('rb') as content:
        return hashlib.file_digest(content, 'sha256').hexdigest()


def checked(arguments: Sequence[str], cwd: Path, *, environment: Mapping[str, str] = os.environ) -> str:
    result = subprocess.run(arguments, cwd=cwd, env=environment, check=True,
                            stdout=subprocess.PIPE, text=True)
    return result.stdout.strip()


def hardware_vulkan(payload: str) -> tuple[str, ...]:
    """Accept hardware adapters only, including hosts with several devices."""
    devices = cast(list[str], re.findall(r'deviceType\s*=\s*(\S+)', payload))
    if not devices or any(value not in ('PHYSICAL_DEVICE_TYPE_DISCRETE_GPU',
                                       'PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU',
                                       'PHYSICAL_DEVICE_TYPE_VIRTUAL_GPU') for value in devices):
        raise ValueError('The trial needs a hardware Vulkan adapter; CPU adapters are refused')
    return tuple(cast(list[str], re.findall(r'deviceName\s*=\s*(.+)', payload)))


def trial_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for key in ('VK_DRIVER_FILES', 'VK_ICD_FILENAMES', 'LIBGL_ALWAYS_SOFTWARE',
                'LIBGL_DRIVERS_PATH', '__EGL_VENDOR_LIBRARY_FILENAMES', 'CARGO_TARGET_DIR',
                'RUSTUP_HOME', 'RUSTC_WRAPPER', 'RUSTC_WORKSPACE_WRAPPER', 'CARGO_BUILD_RUSTC_WRAPPER'):
        _ = environment.pop(key, None)
    environment.update(WGPU_BACKEND='vulkan', CARGO_NET_OFFLINE='true',
                       CARGO_INCREMENTAL='0', RUSTFLAGS='-C target-cpu=x86-64',
                       CARGO_BUILD_TARGET='x86_64-unknown-linux-gnu')
    _ = environment.pop('CARGO_ENCODED_RUSTFLAGS', None)
    return environment


def prepare(repo: Path, commit: str, folder: Path) -> None:
    """Admit the complete local preparation as one supervised program."""
    limits = settings()
    resolved = checked(['git', 'rev-parse', '--verify', commit + '^{commit}'], repo)
    if not re.fullmatch('[0-9a-f]{40}', resolved):
        raise ValueError('The trial needs a resolved Git commit')
    folder.mkdir(parents=True, exist_ok=False)
    write_json(folder / 'request.json', {'repo': str(repo), 'commit': resolved,
                                       'limits': {**asdict(limits),
                                                  'limit_usd': str(limits.limit_usd),
                                                  'box_extra_usd': str(limits.box_extra_usd),
                                                  'spot_max_usd_per_hour': {
                                                      key: str(value) for key, value in limits.spot_max_usd_per_hour.items()}}})
    hold = merge_gate.BuildHoldWait()
    hold.wait_until_free()
    with merge_gate.gate_reservation(repo, folder, hold, mode=merge_gate.SuiteMode.TARGETED_TEST) as place:
        hold.wait_until_free()
        command = [sys.executable, '-m', 'scripts.aws_target.gpu_size_trial', '--prepare-worker', str(folder)]
        with (folder / 'prepare.log').open('wb') as output:
            process = place.popen(command, cwd=ROOT, env=os.environ, stdout=output, stderr=subprocess.STDOUT)
            status = process.wait()
        if status:
            raise GateRefused(f'GPU trial preparation failed with exit {status}: {folder / "prepare.log"}')
    print(folder)


def export_source(repo: Path, commit: str, folder: Path) -> Path:
    source = folder / 'source'
    source.mkdir()
    archive = folder / 'source.tar'
    with archive.open('wb') as output:
        _ = subprocess.run(['git', 'archive', '--format=tar', commit], cwd=repo, stdout=output, check=True)
    with tarfile.open(archive) as bundle:
        bundle.extractall(source, filter='data')
    return source


def nix_string(value: str) -> str:
    return json.dumps(value).replace('${', '\\${')


def scan_toolchain_wrappers(toolchain: Path) -> None:
    """Refuse wrappers that depend on files in a user's home."""
    for path in (toolchain / 'bin').iterdir():
        if not path.is_file():
            continue
        with path.open('rb') as content:
            prefix = content.read(1024 * 1024)
        if prefix.startswith(b'#!') and any(value in prefix for value in (
                b'/home/', b'/Users/', b'/.rustup', b'cargo-tile-capture', str(Path.home()).encode())):
            raise ValueError('The Nix toolchain wrapper depends on a user home: ' + path.name)


def nix_toolchain(folder: Path, source: Path, environment: Mapping[str, str]) -> str:
    expression = ('let host = builtins.getFlake "path:/etc/nixos"; '
        + 'hana = builtins.getFlake ' + nix_string('path:' + str(source)) + '; '
        + 'pkgs = import host.inputs.nixpkgs { system = "x86_64-linux"; overlays = [ '
        + 'host.inputs.lanzaboote.inputs.rust-overlay.overlays.default ]; }; '
        + 'fallback = if builtins.pathExists ' + nix_string(str(source / 'rust-toolchain.toml'))
        + ' then pkgs.rust-bin.fromRustupToolchainFile (builtins.toPath '
        + nix_string(str(source / 'rust-toolchain.toml')) + ') else pkgs.symlinkJoin { '
        + 'name = "gpu-trial-toolchain"; paths = [ pkgs.rustc pkgs.cargo ]; }; '
        + 'chosen = hana.packages.x86_64-linux.rust-toolchain or (hana.packages.x86_64-linux.toolchain or fallback); '
        + 'in pkgs.symlinkJoin { name = "gpu-trial-toolchain-with-nextest"; paths = [ chosen pkgs.cargo-nextest ]; }')
    toolchain = checked(['nix', 'build', '--impure', '--out-link', str(folder / 'toolchain-profile'),
                         '--print-out-paths', '--expr', expression], folder,
                        environment=environment)
    if not re.fullmatch(r'/nix/store/[A-Za-z0-9+._?-]+', toolchain):
        raise ValueError('The trial toolchain must come from the Nix store')
    scan_toolchain_wrappers(Path(toolchain))
    empty_home = folder / 'toolchain-probe-home'
    empty_home.mkdir()
    _ = checked([toolchain + '/bin/cargo', '--version'], source,
                environment={**environment, 'HOME': str(empty_home), 'CARGO_HOME': str(empty_home),
                             'RUSTUP_HOME': str(empty_home)})
    return toolchain


def build_system(folder: Path, source: Path, environment: Mapping[str, str], toolchain: str) -> str:
    """Build the GPU system from the deployment host's locked nixpkgs input."""
    nixpkgs = checked(['nix', 'eval', '--impure', '--raw', '--expr',
                      '(builtins.getFlake "path:/etc/nixos").inputs.nixpkgs.outPath'], folder)
    runtime = folder / 'runtime-profile'
    shell = checked(['nix', 'print-dev-env', '--no-write-lock-file', '--profile', str(runtime), '.#default'], source,
                    environment=environment)
    _ = (folder / 'runtime.env').write_text(shell + '\n')
    module = folder / 'gpu-trial.nix'
    _ = shutil.copyfile(SYSTEM_MODULE, module)
    expression = ('let pkgsPath = builtins.toPath ' + nix_string(nixpkgs) + '; in '
                  + '(import (pkgsPath + "/nixos/lib/eval-config.nix") { system = "x86_64-linux"; modules = [ '
                  + '(import (builtins.toPath ' + nix_string(str(module)) + ') { runtimeRoots = [ (builtins.toPath '
                  + nix_string(str(runtime.resolve())) + ') ]; toolchain = builtins.toPath '
                  + nix_string(toolchain) + '; }) ]; }).config.system.build.toplevel')
    system = checked(['nix', 'build', '--impure', '--out-link', str(folder / 'system-profile'),
                      '--print-out-paths', '--expr', expression], folder,
                     environment=environment)
    roots = checked(['nix-store', '--query', '--requisites', system, str(runtime.resolve()), toolchain], folder).splitlines()
    with (folder / 'closure.export').open('wb') as output:
        _ = subprocess.run(['nix-store', '--export', *sorted(set(roots))], stdout=output, check=True)
    write_json(folder / 'closure.json', {'system': system, 'toolchain': toolchain,
                                        'nixpkgs': nixpkgs, 'roots': roots})
    return system


def cache_dependencies(folder: Path, source: Path, environment: Mapping[str, str]) -> None:
    destination = folder / 'cargo-home'
    destination.mkdir()
    online = {**environment, 'CARGO_HOME': str(destination), 'CARGO_NET_OFFLINE': 'false'}
    _ = checked(['nix', 'develop', '--no-write-lock-file', '.#default', '-c',
                 environment['GPU_TRIAL_CARGO'], 'fetch', '--locked'], source, environment=online)
    _ = (destination / 'config.toml').write_text('[net]\noffline = true\n')


def inventory(payload: dict[str, object]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for binary, raw in mapping(payload['rust-suites']).items():
        suite = mapping(raw)
        for name, item in mapping(suite['testcases']).items():
            case = mapping(item)
            if case.get('ignored') is False and mapping(case['filter-match']).get('status') == 'matches':
                result.append({'binary': binary, 'test': name})
    if not result:
        raise ValueError('The archive contains no selected tests')
    return result


def archive_arguments(folder: Path, source: Path) -> list[str]:
    return ['--archive-file', str(folder / 'hana-tests.tar.zst'), '--workspace-remap', str(source),
            '--profile', 'ci']


def junit_config(text: str, path: Path) -> str:
    section = re.compile(r'(?ms)^\[profile\.ci\.junit\]\s*\n(.*?)(?=^\[|\Z)')
    value = 'path = ' + json.dumps(str(path)) + '\n'
    found = section.search(text)
    if found is None:
        return text + '\n[profile.ci.junit]\n' + value
    body = re.sub(r'(?m)^path\s*=.*\n?', '', found[1])
    return text[:found.start()] + '[profile.ci.junit]\n' + value + body + text[found.end():]


def no_publish(_run: Path) -> None:
    pass


def measure_machine(folder: Path, machine: str, source: Path, environment: Mapping[str, str], *,
                    prefix: Sequence[str] = ('nix', 'develop', '--no-write-lock-file', '.#default', '-c'),
                    publish: Callable[[Path], None] = no_publish) -> None:
    destination = folder / 'machines' / machine
    destination.mkdir(parents=True, exist_ok=False)
    payload = checked([*prefix, 'vulkaninfo', '--summary'], source, environment=environment)
    adapters = hardware_vulkan(payload)
    _ = (destination / 'vulkan.txt').write_text(payload + '\n')
    write_json(destination / 'machine.json', {'adapters': adapters, 'commit': read_json(folder / 'request.json')['commit'],
                                            'archive_sha256': digest(folder / 'hana-tests.tar.zst')})
    for number in (1, 2):
        run = destination / str(number)
        run.mkdir()
        run_source = run / 'source'
        _ = shutil.copytree(source, run_source, ignore=shutil.ignore_patterns('target', '.git'))
        config = run / 'nextest.toml'
        text = (source / '.config/nextest.toml').read_text()
        # Keep all existing groups, priorities, and timeouts.
        text = junit_config(text, run / 'junit.xml')
        _ = config.write_text(text)
        wrapper = shlex.join([sys.executable, str(folder / 'gpu_trial_wrapper.py')])
        run_environment = {**environment, 'GPU_TRIAL_SAMPLES': str(run / 'samples'),
                           'CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUNNER': wrapper,
                           'CARGO_TARGET_DIR': str(run / 'cargo-target')}
        command = [*prefix, environment.get('GPU_TRIAL_CARGO', 'cargo'), 'nextest', 'run', *archive_arguments(folder, run_source),
                   '--extract-to', str(run / 'archive'), '--config-file', str(config), '--retries', '0', '--no-fail-fast']
        with (run / 'nextest.log').open('wb') as output:
            status = subprocess.run(command, cwd=run_source, env=run_environment,
                                    stdout=output, stderr=subprocess.STDOUT, check=False).returncode
        write_json(run / 'run.json', {'status': status, 'config_sha256': digest(config)})
        publish(run)
        if status not in (0, 100):
            raise ValueError(f'Nextest could not complete run {number}; exit {status}')


def prepare_worker(folder: Path) -> None:
    request = read_json(folder / 'request.json')
    repo, commit = Path(str(request['repo'])), str(request['commit'])
    environment = trial_environment()
    source = export_source(repo, commit, folder)
    toolchain = nix_toolchain(folder, source, environment)
    environment.update(GPU_TRIAL_CARGO=toolchain + '/bin/cargo', CARGO=toolchain + '/bin/cargo',
                       RUSTC=toolchain + '/bin/rustc', RUSTDOC=toolchain + '/bin/rustdoc', PATH=toolchain + '/bin:' + environment.get('PATH', ''))
    cache_dependencies(folder, source, environment)
    environment['CARGO_HOME'] = str(folder / 'cargo-home')
    prefix = ['nix', 'develop', '--no-write-lock-file', '.#default', '-c']
    # Refuse software Vulkan before the expensive archive build.
    _ = hardware_vulkan(checked([*prefix, 'vulkaninfo', '--summary'], source, environment=environment))
    archive = folder / 'hana-tests.tar.zst'
    _ = checked([*prefix, environment.get('GPU_TRIAL_CARGO', 'cargo'), 'nextest', 'archive', '--profile', 'ci', '--locked',
                 '--workspace', '--all-features', '--tests', '--archive-file', str(archive)],
                source, environment=environment)
    _ = shutil.copyfile(WRAPPER, folder / WRAPPER.name)
    system = build_system(folder, source, environment, toolchain)
    listed = checked([*prefix, environment.get('GPU_TRIAL_CARGO', 'cargo'), 'nextest', 'list', *archive_arguments(folder, source),
                      '--message-format', 'json'], source, environment=environment)
    write_json(folder / 'inventory.json', {'tests': inventory(mapping(cast(object, json.loads(listed))))})
    manifest: dict[str, object] = {'schema': 2, 'toolchain_source': 'nix', 'trial': folder.name, 'commit': commit,
        'tree': checked(['git', 'rev-parse', commit + '^{tree}'], repo),
        'created': datetime.now(UTC).isoformat(), 'system': system, 'toolchain': toolchain,
        'rustc': checked([*prefix, environment['RUSTC'], '-Vv'], source, environment=environment),
        'nextest': checked([*prefix, environment.get('GPU_TRIAL_CARGO', 'cargo'), 'nextest', '--version'], source, environment=environment),
        'limits': request['limits'], 'state': 'preparing',
        'sha256': {name: digest(folder / name) for name in (
            'hana-tests.tar.zst', 'closure.export', 'source.tar', 'runtime.env', 'gpu-trial.nix',
            'gpu_trial_wrapper.py', 'inventory.json')},
        'locks': {name: digest(source / name) for name in ('Cargo.lock', 'flake.lock')}}
    write_json(folder / 'manifest.json', manifest)
    measure_machine(folder, 'natedev', source, environment)
    manifest['state'] = 'prepared'
    write_json(folder / 'manifest.json', manifest)


@dataclass(frozen=True)
class TestSample:
    status: int
    wall_s: float
    largest_process_peak_rss_kib: float


@dataclass(frozen=True)
class IncompleteTest:
    reason: str


@dataclass(frozen=True)
class MachineTestPair:
    first: TestSample | IncompleteTest
    second: TestSample | IncompleteTest

    def verdict(self) -> str:
        if isinstance(self.first, IncompleteTest) or isinstance(self.second, IncompleteTest):
            return 'incomplete'
        return 'flaky' if self.first.status != self.second.status else 'stable'


def sample(row: dict[str, object]) -> TestSample:
    if type(row['status']) is not int:
        raise ValueError('The test status must be an integer')
    values: list[float] = []
    for name in ('wall_s', 'largest_process_peak_rss_kib'):
        value = row[name]
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
            raise ValueError('The test has an invalid ' + name)
        values.append(float(value))
    return TestSample(row['status'], values[0], values[1])


def comparison(baseline: MachineTestPair, candidate: MachineTestPair) -> str:
    if baseline.verdict() == 'flaky':
        return 'inconclusive'
    if baseline.verdict() == 'incomplete' or candidate.verdict() == 'incomplete':
        return 'incomplete'
    if candidate.verdict() == 'flaky':
        return 'flaky'
    assert isinstance(baseline.first, TestSample) and isinstance(candidate.first, TestSample)
    return 'same' if baseline.first.status == candidate.first.status else 'difference'


def read_run(folder: Path) -> dict[tuple[str, str], TestSample]:
    rows: dict[tuple[str, str], TestSample] = {}
    try:
        status = read_json(folder / 'run.json')['status']
        if type(status) is not int or status not in (0, 100) or not (folder / 'junit.xml').is_file():
            return rows
        for path in sorted((folder / 'samples').glob('*.json')):
            row = read_json(path)
            key = str(row['binary']), str(row['test'])
            if key in rows:
                raise ValueError('The run contains a duplicate test')
            rows[key] = sample(row)
    except (OSError, KeyError, ValueError):
        return {}
    return rows


def sample_columns(item: TestSample | IncompleteTest) -> tuple[str, str, str]:
    if isinstance(item, IncompleteTest):
        return 'missing', 'missing', 'missing'
    return str(item.status), str(item.wall_s), str(item.largest_process_peak_rss_kib)


def compare(folder: Path) -> str:
    manifest = read_json(folder / 'manifest.json')
    expected = read_json(folder / 'inventory.json')['tests']
    if not isinstance(expected, list):
        raise ValueError('The trial inventory must be a list')
    machines: dict[str, tuple[dict[tuple[str, str], TestSample], dict[tuple[str, str], TestSample]]] = {}
    hashes = mapping(manifest['sha256'])
    if digest(folder / 'inventory.json') != hashes['inventory.json']:
        raise ValueError('The inventory differs from the manifest')
    for machine in sorted((folder / 'machines').glob('*')):
        if not machine.is_dir():
            continue
        try:
            identity = read_json(machine / 'machine.json')
            valid = identity.get('commit') == manifest['commit'] and identity.get('archive_sha256') == hashes['hana-tests.tar.zst']
            _ = hardware_vulkan((machine / 'vulkan.txt').read_text())
        except (OSError, ValueError):
            valid = False
        machines[machine.name] = ((read_run(machine / '1'), read_run(machine / '2')) if valid else ({}, {}))
    missing = IncompleteTest('No completed test measurement')
    baseline = machines.get('natedev', ({}, {}))
    _ = machines.setdefault('natedev', ({}, {}))
    for name in mapping(mapping(manifest['limits'])['spot_max_usd_per_hour']):
        _ = machines.setdefault(name, ({}, {}))
    lines = ['# GPU size trial', '', 'Peak RSS means the kernel peak RSS of the largest single process of the test.',
             '', '| Binary | Test | Machine | Status 1 / 2 | Wall seconds 1 / 2 | Peak RSS KiB 1 / 2 | Result |',
             '|---|---|---|---|---|---|---|']
    for raw in cast(list[object], expected):
        test = mapping(raw)
        key = str(test['binary']), str(test['test'])
        base_pair = MachineTestPair(*(run.get(key, missing) for run in baseline))
        for name, runs in machines.items():
            pair = MachineTestPair(*(run.get(key, missing) for run in runs))
            verdict = pair.verdict() if name == 'natedev' else comparison(base_pair, pair)
            first, second = sample_columns(pair.first), sample_columns(pair.second)
            values = [value for pair_values in zip(first, second) for value in pair_values]
            escaped = [value.replace('|', '\\|').replace('\n', ' ') for value in (key[0], key[1], name)]
            lines.append('| ' + ' | '.join([*escaped, *(values[index] + ' / ' + values[index + 1]
                                                        for index in (0, 2, 4)), verdict]) + ' |')
    report = '\n'.join(lines) + '\n'
    _ = (folder / 'comparison.md').write_text(report)
    return report


def validate_prepared(folder: Path) -> dict[str, object]:
    manifest = read_json(folder / 'manifest.json')
    if manifest.get('schema') != 2 or manifest.get('toolchain_source') != 'nix':
        raise ValueError('Prepare the trial again with the Nix toolchain and current GPU system module')
    if manifest.get('state') != 'prepared' or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', str(manifest['trial'])):
        raise ValueError('The trial folder has no completed preparation')
    for name, checksum in mapping(manifest['sha256']).items():
        if Path(name).name != name or digest(folder / name) != checksum:
            raise ValueError('The prepared artifact differs from the manifest: ' + name)
    return manifest


def start_service(folder: Path, directory: Path, settings_path: Path,
                  execute: Callable[[Sequence[str]], int]) -> int:
    manifest = validate_prepared(folder)
    unit = 'gpu-size-trial-' + hashlib.sha256(str(manifest['trial']).encode()).hexdigest()[:24]
    command = cloud_gate.service_command('scripts.aws_target.gpu_size_trial',
        ['--worker', str(folder), '--directory', str(directory), '--settings', str(settings_path)], unit)
    return execute(command)


GUEST = r'''
root = Path('/var/lib/gpu-size-trial')
os.chdir(root)
links = read_json(root / 'links.json')
manifest = read_json(root / 'manifest.json')


def publish(run):
    machine = run.parent
    bundle = root / ('result-' + run.name + '.tar.gz')
    with tarfile.open(bundle, 'w:gz') as output:
        for name in ('machine.json', 'vulkan.txt'):
            output.add(machine / name, arcname='machines/' + machine.name + '/' + name)
        for name in ('samples', 'run.json', 'junit.xml', 'nextest.log', 'nextest.toml'):
            path = run / name
            if path.exists():
                output.add(path, arcname='machines/' + machine.name + '/' + run.name + '/' + name)
    subprocess.run(['curl', '--fail', '--silent', '--show-error', '--max-time', '60',
                    '-T', str(bundle), str(links['result-' + run.name])], check=True)


status = 'incomplete'
reason = ''
try:
    setup = read_json(root / 'setup.json')
    setup['runner_start'] = time.time()
    setup['boot_seconds'] = setup['boot'] - float(links['started'])
    setup['import_seconds'] = setup['import_end'] - setup['import_start']
    setup['reboot_seconds'] = setup['runner_start'] - setup['reboot']
    setup['setup_seconds'] = setup['runner_start'] - float(links['started'])
    write_json(root / 'setup.json', setup)
    subprocess.run(['curl', '--fail', '--silent', '--show-error', '--max-time', '120',
                    str(links['archive']), '-o', str(root / 'hana-tests.tar.zst')], check=True)
    for name, checksum in mapping(manifest['sha256']).items():
        if Path(name).name != name or digest(root / name) != checksum:
            raise ValueError('The artifact differs from the manifest: ' + name)
    source = root / 'source'
    source.mkdir()
    with tarfile.open(root / 'source.tar') as archive:
        archive.extractall(source, filter='data')
    version = subprocess.check_output(['cargo', 'nextest', '--version'], text=True).strip()
    if version != manifest['nextest']:
        raise ValueError('The box nextest version differs from the prepared version')
    environment = trial_environment()
    environment['CARGO_HOME'] = str(root / 'cargo-home')
    measure_machine(root, str(links['instance_type']), source, environment, prefix=(), publish=publish)
    status = 'complete'
except Exception as error:
    reason = str(error)
finally:
    finished = root / 'finished.json'
    write_json(finished, {'status': status, 'reason': reason,
                         'setup': read_json(root / 'setup.json'), 'finished': time.time()})
    subprocess.run(['curl', '--fail', '--silent', '--show-error', '--max-time', '60',
                    '-T', str(finished), str(links['finished'])], check=True)
'''


def guest_program() -> str:
    functions = (mapping, read_json, write_json, digest, checked, hardware_vulkan, trial_environment,
                 archive_arguments, junit_config, no_publish, measure_machine)
    return ('from __future__ import annotations\n'
            + 'from pathlib import Path\nfrom collections.abc import Callable, Mapping, Sequence\n'
            + 'from typing import cast\nimport hashlib, json, os, re, shlex, shutil, subprocess, sys, tarfile, time\n'
            + '\n'.join(inspect.getsource(function) for function in functions) + GUEST)


def gpu_user_data(links: dict[str, str], system: str, toolchain: str, expires: int, stop_at: int) -> str:
    """Import the prepared system and reboot once before the unit starts."""
    if not re.fullmatch(r'/nix/store/[A-Za-z0-9+._?-]+', system) or not re.fullmatch(r'/nix/store/[A-Za-z0-9+._?-]+', toolchain):
        raise ValueError('The prepared system or toolchain path is invalid')
    script = f'''#!/bin/sh
set -eu
export PATH=/run/current-system/sw/bin:$PATH
root=/var/lib/gpu-size-trial
# amazon-init can read the same user data after the reboot.
[ ! -e "$root/imported" ] || exit 0
mkdir -p "$root"
exec >> "$root/boot.log" 2>&1
systemd-run --unit=gpu-trial-expiry --on-calendar=@{stop_at} /run/current-system/sw/bin/shutdown -h now
boot=$(date +%s)
curl --fail --silent --show-error --max-time {max(1, stop_at - int(links['started']))} {shlex.quote(links['payload'])} -o "$root/payload.tar"
echo {shlex.quote(links['payload_sha256'] + '  /var/lib/gpu-size-trial/payload.tar')} | sha256sum -c -
tar -xf "$root/payload.tar" -C "$root"
curl --fail --silent --show-error --max-time {max(1, stop_at - int(links['started']))} {shlex.quote(links['closure'])} -o "$root/closure.export"
echo {shlex.quote(links['closure_sha256'] + '  /var/lib/gpu-size-trial/closure.export')} | sha256sum -c -
import_start=$(date +%s)
nix-store --import < "$root/closure.export"
import_end=$(date +%s)
cat > "$root/links.json" <<'GPU_LINKS'
{json.dumps(links)}
GPU_LINKS
printf '{{"boot":%s,"import_start":%s,"import_end":%s,"reboot":%s}}\\n' "$boot" "$import_start" "$import_end" "$(date +%s)" > "$root/setup.json"
cat > "$root/run" <<'GPU_RUN'
#!/run/current-system/sw/bin/bash
set -euo pipefail
cd /var/lib/gpu-size-trial
. ./runtime.env
export PATH={shlex.quote(toolchain + '/bin')}:/run/current-system/sw/bin:$PATH
export HOME=/home/gpu-trial CARGO_HOME=/var/lib/gpu-size-trial/cargo-home
export TMPDIR=/var/lib/gpu-size-trial/tmp TMP=/var/lib/gpu-size-trial/tmp TEMP=/var/lib/gpu-size-trial/tmp
export XDG_CACHE_HOME=/var/lib/gpu-size-trial/cache XDG_RUNTIME_DIR=/var/lib/gpu-size-trial/runtime
mkdir -p "$TMPDIR" "$XDG_CACHE_HOME" "$XDG_RUNTIME_DIR"
export RUSTC={shlex.quote(toolchain + '/bin/rustc')} RUSTDOC={shlex.quote(toolchain + '/bin/rustdoc')}
export GPU_TRIAL_CARGO={shlex.quote(toolchain + '/bin/cargo')}
exec /run/current-system/sw/bin/python3 ./guest.py
GPU_RUN
chmod 700 "$root/run"
{shlex.quote(system + '/bin/switch-to-configuration')} boot
printf '%s\\n' {stop_at} > "$root/stop-at"
touch "$root/imported"
reboot
'''
    if len(script.encode()) > 16384 or stop_at >= expires:
        raise ValueError('The GPU bootstrap exceeds its size or time limit')
    return base64.b64encode(script.encode()).decode()


@final
class GpuTrialWorker:
    """Run sequential boxes with durable bookings and shared gate cleanup."""
    def __init__(self, gates: cloud_gate.GateWorker) -> None:
        self.gates = gates

    def select_region(self, deployment: dict[str, object]) -> dict[str, object]:
        regions = {str(row['region']): row for row in ledger.rows(deployment, 'regions')}
        for region in conf.read_or_repository('cloud_gate').words('spot_candidate_regions'):
            reply = self.gates.regional_api(region).call('get-service-quota', '--service-code', 'ec2',
                '--quota-code', 'L-3819A6DF', service='service-quotas')
            if conf.decimal(str(mapping(reply['Quota'])['Value'])) >= 32:
                if region not in regions:
                    raise ValueError('The granted GPU region has no gate deployment: ' + region)
                return regions[region]
        raise ValueError('No configured region has an applied G and VT Spot quota of at least 32')

    def offered_zones(self, regional: dict[str, object], size: str) -> list[dict[str, object]]:
        api = self.gates.regional_api(str(regional['region']))
        offered: set[str] = set()
        token = ''
        while True:
            arguments = ['--location-type', 'availability-zone', '--filters', 'Name=instance-type,Values=' + size,
                         '--no-paginate']
            if token:
                arguments += ['--next-token', token]
            reply = api.call('describe-instance-type-offerings', *arguments)
            offered.update(str(row['Location']) for row in ledger.rows(reply, 'InstanceTypeOfferings')
                           if row['InstanceType'] == size)
            token = str(reply.get('NextToken', ''))
            if not token:
                break
        return [row for row in ledger.rows(regional, 'subnets') if row['zone'] in offered]

    def price_zone(self, regional: dict[str, object], size: str, maximum: Decimal,
                   subnets: Sequence[dict[str, object]]) -> dict[str, object] | str:
        api = self.gates.regional_api(str(regional['region']))
        reply = api.call('describe-spot-price-history', '--instance-types', size, '--product-descriptions', 'Linux/UNIX',
                         '--start-time', datetime.fromtimestamp(self.gates.clock(), UTC).isoformat())
        prices = ledger.regional_spot_prices(api.region, reply, (size,), int(self.gates.clock()))
        available = [(price.usd_per_hour, subnet) for subnet in subnets for price in prices
                     if price.zone == subnet['zone'] and price.instance_type == size and price.usd_per_hour <= maximum]
        if not available:
            return 'price above max' if prices else 'price unavailable'
        return min(available, key=lambda item: item[0])[1]

    def upload(self, bucket: str, key: str, path: Path) -> None:
        if path.stat().st_size < 5 * 1024**3:
            _ = self.gates.api.call('put-object', '--bucket', bucket, '--key', key, '--body', str(path),
                service='s3api', timeout_s=settings().lifetime_minutes * 60)
        else:
            _ = self.gates.api.call('cp', str(path), 's3://' + bucket + '/' + key, '--only-show-errors', service='s3', timeout_s=settings().lifetime_minutes * 60)

    def artifacts(self, folder: Path, online: dict[str, object]) -> None:
        if online.get('uploaded'):
            return
        payload = folder / 'payload.tar'
        _ = (folder / 'guest.py').write_text(guest_program())
        with tarfile.open(payload, 'w') as archive:
            for name in ('source.tar', 'cargo-home', 'runtime.env', 'gpu-trial.nix', 'closure.json', 'request.json',
                         'manifest.json', 'inventory.json', 'gpu_trial_wrapper.py', 'guest.py'):
                archive.add(folder / name, arcname=name)
        prefix = 'requests/gpu-size-trial/' + str(online['trial'])
        for name in ('payload.tar', 'closure.export', 'hana-tests.tar.zst', 'manifest.json'):
            self.upload(str(online['bucket']), prefix + '/' + name, folder / name)
        online['uploaded'] = True
        online['payload_sha256'] = digest(payload)
        cloud_gate.save(folder / 'online.json', online)

    def signed_links(self, folder: Path, online: dict[str, object], request: dict[str, object]) -> dict[str, str]:
        reply = self.gates.api.call('export-credentials', '--format', 'process', service='configure')
        signing = gate_box.SigningKey(str(reply['AccessKeyId']), str(reply['SecretAccessKey']), str(reply.get('SessionToken', '')))
        now = int(self.gates.clock())
        home = self.gates.api.region
        prefix = 'requests/gpu-size-trial/' + str(online['trial'])
        def url(method: str, key: str) -> str:
            return gate_box.presign(method, str(online['bucket']) + '.s3.' + home + '.amazonaws.com', key,
                                    home, signing, datetime.fromtimestamp(now, UTC), max(1, int(str(request['expires'])) - now))
        result_prefix = 'artifacts/gpu-size-trial/' + str(request['attempt'])
        return {'payload': url('GET', prefix + '/payload.tar'), 'closure': url('GET', prefix + '/closure.export'),
                'archive': url('GET', prefix + '/hana-tests.tar.zst'),
                'result-1': url('PUT', result_prefix + '/result-1.tar.gz'),
                'result-2': url('PUT', result_prefix + '/result-2.tar.gz'),
                'finished': url('PUT', result_prefix + '/finished.json'),
                'payload_sha256': str(online['payload_sha256']),
                'closure_sha256': digest(folder / 'closure.export'), 'archive_sha256': digest(folder / 'hana-tests.tar.zst'),
                'instance_type': str(request['instance_type']), 'started': str(request['started'])}

    def cleanup(self, folder: Path) -> bool:
        trial_id = str(read_json(folder / 'manifest.json')['trial'])
        attempts: list[cloud_gate.GateCleanupAttempt] = []
        for reservation in self.gates.book.read_snapshot().gate_reservations:
            if not isinstance(reservation.trial, ledger.GateTrialBudget) or reservation.trial.trial_id != trial_id:
                continue
            box = folder / 'boxes' / reservation.instance_type
            path = box / 'launch.json'
            attempts.append(cloud_gate.GateCleanupAttempt(reservation, read_json(path) if path.exists() else {},
                                                          (box / 'request.json').is_file()))
        return cloud_gate.cleanup_attempts(attempts, self.gates)

    def collect(self, folder: Path, box: Path, request: dict[str, object], online: dict[str, object]) -> None:
        prefix = 'artifacts/gpu-size-trial/' + str(request['attempt'])
        finished = False
        while self.gates.clock() < int(str(request['stop_at'])):
            finished = self.gates.download(str(online['bucket']), prefix + '/finished.json', box / 'finished.json')
            for number in (1, 2):
                local = box / ('result-' + str(number) + '.tar.gz')
                temporary = local.with_suffix('.partial')
                if not local.exists() and self.gates.download(str(online['bucket']), prefix + '/' + local.name, temporary):
                    with tarfile.open(temporary) as archive:
                        # Results contain only this machine's report files.
                        allowed = 'machines/' + str(request['instance_type']) + '/'
                        if any(not member.name.startswith(allowed) or '..' in Path(member.name).parts
                               or not (member.isfile() or member.isdir()) for member in archive.getmembers()):
                            raise ValueError('The result archive contains an unexpected machine')
                        archive.extractall(folder, filter='data')
                    _ = temporary.replace(local)
            if finished:
                break
            self.gates.pause(cloud_gate.settings().number('result_poll_s'))
        if finished and read_json(box / 'finished.json').get('status') == 'complete' and not all(
                (box / ('result-' + str(number) + '.tar.gz')).is_file() for number in (1, 2)):
            raise ValueError('The completed box has missing result files')
        cloud_gate.save(box / 'result.json', read_json(box / 'finished.json') if finished
                        else {'status': 'incomplete', 'reason': 'The box reached its early stop time'})

    def execute(self, folder: Path, deployment: dict[str, object]) -> None:
        manifest = validate_prepared(folder)
        path = folder / 'online.json'
        if path.exists():
            online = read_json(path)
            if online['trial'] != manifest['trial']:
                raise ValueError('The saved trial ID differs from the manifest')
        else:
            if any(isinstance(row.trial, ledger.GateTrialBudget) and row.trial.trial_id == manifest['trial']
                   for row in self.gates.book.read_snapshot().gate_reservations):
                raise ValueError('The trial already has bookings; resume its original trial folder')
            limits = settings()
            regional = self.select_region(deployment)
            online = {'trial': manifest['trial'], 'regional': regional, 'bucket': deployment['bucket'],
                      'deployment_id': deployment['deployment_id'],
                      'limit_usd': str(min(limits.limit_usd, conf.decimal(str(mapping(manifest['limits'])['limit_usd'])))),
                      'lifetime_minutes': limits.lifetime_minutes, 'box_extra_usd': str(limits.box_extra_usd),
                      'root_gib': conf.read_or_repository('cloud_gate').whole('gpu_size_trial_root_gib'),
                      'types': list(limits.spot_max_usd_per_hour),
                      'prices': {key: str(value) for key, value in limits.spot_max_usd_per_hour.items()}}
            cloud_gate.save(path, online)
        if (folder / 'online-result.json').exists():
            return
        regional = mapping(online['regional'])
        if 'offerings' not in online:
            online['offerings'] = {size: self.offered_zones(regional, size)
                                   for size in cast(list[str], online['types'])}
            cloud_gate.save(path, online)
        self.gates.charges()
        self.artifacts(folder, online)
        for size in cast(list[str], online['types']):
            price = mapping(online['prices'])[size]
            box = folder / 'boxes' / size
            box.mkdir(parents=True, exist_ok=True)
            if (box / 'outcome.json').exists():
                continue
            attempt = hashlib.sha256((str(online['trial']) + ':' + size).encode()).hexdigest()
            previous = next((row for row in self.gates.book.read_snapshot().gate_reservations if row.attempt == attempt), None)
            if previous is not None:
                if not self.cleanup(folder):
                    raise ValueError('The previous GPU box termination remains unconfirmed')
                cloud_gate.save(box / 'outcome.json', {'status': 'incomplete', 'reason': 'The service restarted'})
                continue
            subnets = ledger.rows(mapping(online['offerings']), size)
            reason = 'not offered' if not subnets else ''
            maximum = conf.decimal(str(price))
            subnet = self.price_zone(regional, size, maximum, subnets) if subnets else reason
            if isinstance(subnet, str):
                print(size + ': ' + subnet, flush=True)
                cloud_gate.save(box / 'outcome.json', {'status': subnet})
                continue
            now = int(self.gates.clock())
            lifetime = int(str(online['lifetime_minutes'])) * 60
            expires = now + lifetime
            stop_at, tag_expires = now + max(1, lifetime - 300), now + max(2, lifetime - 120)
            request: dict[str, object] = {'attempt': attempt, 'trial': online['trial'], 'mode': 'gpu-size-trial',
                'commit': manifest['commit'], 'instance_type': size, 'started': now, 'expires': expires, 'stop_at': stop_at}
            cloud_gate.save(box / 'request.json', request)
            reservation = ledger.GateReservation(attempt, now, expires, maximum, conf.decimal(str(online['box_extra_usd'])),
                instance_type=size, region=str(regional['region']), mode='gpu-size-trial', root_gib=int(str(online['root_gib'])),
                trial=ledger.GateTrialBudget(str(online['trial']), conf.decimal(str(online['limit_usd']))))
            self.gates.charges()
            decision = self.gates.book.reserve_gate(self.gates.settings_path, reservation)
            if not isinstance(decision, ledger.GateReserved):
                reason = decision.reason
                cloud_gate.save(box / 'outcome.json', {'status': 'budget stop', 'reason': reason})
                print('GPU size trial stopped: ' + reason, flush=True)
                cloud_gate.save(folder / 'online-result.json', {'trial': online['trial'], 'status': 'budget stop', 'reason': reason})
                return
            links = self.signed_links(folder, online, request)
            account = conf.read(self.gates.settings_path)
            policy = read_settings(self.gates.settings_path)
            tags = [{'Key': 'GateDeployment', 'Value': str(online['deployment_id'])},
                    {'Key': 'Workload', 'Value': 'cloud-claude-gate'}, {'Key': 'GateAttempt', 'Value': attempt},
                    {'Key': 'ExpiresAt', 'Value': str(tag_expires)}, {'Key': 'GpuSizeTrial', 'Value': str(online['trial'])},
                    {'Key': policy.cost_explorer_tag_key, 'Value': policy.cost_explorer_tag_value}]
            arguments: dict[str, object] = {
                'LaunchTemplate': {'LaunchTemplateId': regional['template'], 'Version': regional['version']},
                'ImageId': regional['image'], 'MinCount': 1, 'MaxCount': 1, 'ClientToken': attempt,
                'InstanceType': size,
                'InstanceMarketOptions': {'MarketType': 'spot', 'SpotOptions': {'SpotInstanceType': 'one-time',
                    'InstanceInterruptionBehavior': 'terminate', 'MaxPrice': str(maximum)}},
                'BlockDeviceMappings': [{'DeviceName': account.text('root_device_name'),
                    'Ebs': {'VolumeSize': online['root_gib'], 'VolumeType': 'gp3', 'Encrypted': True, 'DeleteOnTermination': True}}],
                'NetworkInterfaces': [{'DeviceIndex': 0, 'SubnetId': subnet['id'], 'Groups': [regional['security_group']],
                                       'AssociatePublicIpAddress': True}],
                'TagSpecifications': [{'ResourceType': kind, 'Tags': tags} for kind in ('instance', 'volume')],
                'UserData': gpu_user_data(links, str(manifest['system']), str(manifest['toolchain']), tag_expires, stop_at)}
            cloud_gate.save(box / 'launch.json', {'region': regional['region'], 'zone': subnet['zone'], 'arguments': arguments})
            self.gates.box_region = str(regional['region'])
            self.gates.size_vcpus = 4 * int(size.split('.')[1].removesuffix('xlarge') or '1')
            launched = False
            try:
                reply = self.gates.regional_api(str(regional['region'])).call('run-instances', '--cli-input-json', json.dumps(arguments))
                launched = True
                instance = ledger.text(ledger.rows(reply, 'Instances')[0], 'InstanceId')
                cloud_gate.save(box / 'launch.json', {'region': regional['region'], 'zone': subnet['zone'],
                                'arguments': arguments, 'instance': instance})
                self.gates.book.resolve_gate(attempt, ledger.GateRunning(instance), instance_type=size)
                self.gates.record_launch(request, size, str(subnet['zone']), 'launched')
                self.collect(folder, box, request, online)
            except AwsUnavailable as error:
                if not launched and cloud_gate.definite_refusal(str(error)):
                    self.gates.book.resolve_gate(attempt, ledger.GateAbsent(int(self.gates.clock())))
                    cloud_gate.save(box / 'result.json', {'status': 'refused', 'reason': cloud_gate.launch_error_code(str(error))})
                else:
                    raise
            finally:
                if not self.cleanup(folder):
                    raise ValueError('GPU box termination remains unconfirmed; the next box cannot launch')
            cloud_gate.save(box / 'outcome.json', read_json(box / 'result.json'))
        cloud_gate.save(folder / 'online-result.json', {'trial': online['trial'], 'status': 'complete'})


def online_main(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description='Run the GPU trial service')
    modes = parser.add_mutually_exclusive_group(required=True)
    _ = modes.add_argument('--worker', type=Path)
    _ = modes.add_argument('--cleanup', type=Path)
    _ = parser.add_argument('--directory', type=Path, required=True)
    _ = parser.add_argument('--settings', type=Path, required=True)
    args = parser.parse_args(arguments)
    directory, settings_path = cast(Path, args.directory), cast(Path, args.settings)
    cleanup = cast(Path | None, args.cleanup)
    folder = cleanup if cleanup is not None else cast(Path, args.worker)
    configuration = conf.read(settings_path)
    api = AwsCli(configuration.text('credentials_profile'), configuration.text('region'),
                 cloud_gate.settings().number('api_timeout_s'))
    worker = GpuTrialWorker(cloud_gate.GateWorker(api, ledger.CostLedger(directory), settings_path,
                                                folder / 'reservation.cgroup'))
    with control_lock(directory / 'gpu-size-trial'):
        try:
            if cleanup is not None:
                return int(not worker.cleanup(folder))
            deployment = gate_deployment(directory / 'gate-deployment.json', api.region)
            worker.execute(folder, deployment)
            return 0
        except (OSError, ValueError, KeyError, AwsUnavailable, subprocess.SubprocessError) as error:
            print('GPU size trial refused: ' + str(error), file=sys.stderr)
            return 1


def main(arguments: Sequence[str]) -> int:
    if '--worker' in arguments or '--cleanup' in arguments:
        return online_main(arguments)
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument('--prepare-worker', type=Path, help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest='command')
    prepare_parser = commands.add_parser('prepare')
    _ = prepare_parser.add_argument('--commit', default='refs/remotes/origin/main')
    _ = prepare_parser.add_argument('--repo', type=Path, default=Path.home() / 'rust/hana')
    _ = prepare_parser.add_argument('--trial-folder', type=Path,
                                   default=Path.home() / '.local/state/gpu-size-trial' / uuid4().hex)
    compare_parser = commands.add_parser('compare')
    _ = compare_parser.add_argument('--trial-folder', type=Path, required=True)
    args = parser.parse_args(arguments)
    try:
        worker = cast(Path | None, args.prepare_worker)
        command = cast(str | None, args.command)
        if worker is not None:
            prepare_worker(worker.resolve())
        elif command == 'prepare':
            prepare(cast(Path, args.repo).expanduser().resolve(), cast(str, args.commit),
                    cast(Path, args.trial_folder).expanduser().resolve())
        elif command == 'compare':
            print(compare(cast(Path, args.trial_folder).expanduser().resolve()), end='')
        else:
            parser.error('Choose prepare or compare')
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, GateRefused) as error:
        print('GPU size trial refused: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
