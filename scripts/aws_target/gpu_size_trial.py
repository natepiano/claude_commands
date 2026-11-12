"""Prepare and compare a GPU trial without AWS access."""
from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
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
from typing import cast
from uuid import uuid4

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
                'LIBGL_DRIVERS_PATH', '__EGL_VENDOR_LIBRARY_FILENAMES', 'CARGO_TARGET_DIR'):
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


def build_system(folder: Path, source: Path, environment: Mapping[str, str]) -> tuple[str, str]:
    """Build the GPU system from the deployment host's locked nixpkgs input."""
    nixpkgs = checked(['nix', 'eval', '--impure', '--raw', '--expr',
                      '(builtins.getFlake "path:/etc/nixos").inputs.nixpkgs.outPath'], folder)
    rustc = Path(checked(['rustup', 'which', 'rustc'], source))
    toolchain = checked(['nix', 'store', 'add-path', str(rustc.parent.parent)], folder)
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
    system = checked(['nix', 'build', '--impure', '--no-link', '--print-out-paths', '--expr', expression], folder,
                     environment=environment)
    roots = checked(['nix-store', '--query', '--requisites', system, str(runtime.resolve()), toolchain], folder).splitlines()
    with (folder / 'closure.export').open('wb') as output:
        _ = subprocess.run(['nix-store', '--export', *sorted(set(roots))], stdout=output, check=True)
    write_json(folder / 'closure.json', {'system': system, 'toolchain': toolchain,
                                        'nixpkgs': nixpkgs, 'roots': roots})
    return system, toolchain


def cache_dependencies(folder: Path, source: Path, environment: Mapping[str, str]) -> None:
    online = {**environment, 'CARGO_NET_OFFLINE': 'false'}
    _ = checked(['nix', 'develop', '--no-write-lock-file', '.#default', '-c', 'cargo', 'fetch', '--locked'], source, environment=online)
    cargo_home = Path(os.environ.get('CARGO_HOME', str(Path.home() / '.cargo'))).expanduser()
    destination = folder / 'cargo-home'
    destination.mkdir()
    for name in ('registry', 'git'):
        if (cargo_home / name).is_dir():
            _ = shutil.copytree(cargo_home / name, destination / name, symlinks=False)
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


def measure_machine(folder: Path, machine: str, source: Path, environment: Mapping[str, str]) -> None:
    destination = folder / 'machines' / machine
    destination.mkdir(parents=True, exist_ok=False)
    prefix = ['nix', 'develop', '--no-write-lock-file', '.#default', '-c']
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
        command = [*prefix, 'cargo', 'nextest', 'run', *archive_arguments(folder, run_source),
                   '--extract-to', str(run / 'archive'), '--config-file', str(config), '--retries', '0', '--no-fail-fast']
        with (run / 'nextest.log').open('wb') as output:
            status = subprocess.run(command, cwd=run_source, env=run_environment,
                                    stdout=output, stderr=subprocess.STDOUT, check=False).returncode
        write_json(run / 'run.json', {'status': status, 'config_sha256': digest(config)})
        if status not in (0, 100):
            raise ValueError(f'Nextest could not complete run {number}; exit {status}')


def prepare_worker(folder: Path) -> None:
    request = read_json(folder / 'request.json')
    repo, commit = Path(str(request['repo'])), str(request['commit'])
    environment = trial_environment()
    source = export_source(repo, commit, folder)
    cache_dependencies(folder, source, environment)
    environment['CARGO_HOME'] = str(folder / 'cargo-home')
    prefix = ['nix', 'develop', '--no-write-lock-file', '.#default', '-c']
    # Refuse software Vulkan before the expensive archive build.
    _ = hardware_vulkan(checked([*prefix, 'vulkaninfo', '--summary'], source, environment=environment))
    archive = folder / 'hana-tests.tar.zst'
    _ = checked([*prefix, 'cargo', 'nextest', 'archive', '--profile', 'ci', '--locked',
                 '--workspace', '--all-features', '--tests', '--archive-file', str(archive)],
                source, environment=environment)
    _ = shutil.copyfile(WRAPPER, folder / WRAPPER.name)
    system, toolchain = build_system(folder, source, environment)
    listed = checked([*prefix, 'cargo', 'nextest', 'list', *archive_arguments(folder, source),
                      '--message-format', 'json'], source, environment=environment)
    write_json(folder / 'inventory.json', {'tests': inventory(mapping(cast(object, json.loads(listed))))})
    manifest: dict[str, object] = {'schema': 1, 'trial': folder.name, 'commit': commit,
        'tree': checked(['git', 'rev-parse', commit + '^{tree}'], repo),
        'created': datetime.now(UTC).isoformat(), 'system': system, 'toolchain': toolchain,
        'rustc': checked([*prefix, 'rustc', '-Vv'], source, environment=environment),
        'nextest': checked([*prefix, 'cargo', 'nextest', '--version'], source, environment=environment),
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


def main(arguments: Sequence[str]) -> int:
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
