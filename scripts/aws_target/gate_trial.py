"""Compare pushed revisions on disposable gate instances."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections.abc import Callable, Sequence
from contextlib import redirect_stdout
from dataclasses import asdict, dataclass
from decimal import Decimal
from datetime import UTC, datetime
import json
import multiprocessing
import sys
from multiprocessing.process import BaseProcess
from pathlib import Path
import signal
import statistics
from types import FrameType
import subprocess
import time
from typing import Protocol, cast
from uuid import uuid4

from scripts.aws_target.gate_box import (
    GUEST as GUEST, SigningKey as SigningKey, presign as presign, user_data as user_data,
    boot_trap as boot_trap, log_push_loop as log_push_loop,
    RUN_FIELDS as RUN_FIELDS, validate_paired_result as validate_paired_result,
)
from scripts.aws_target import ledger, cloud_gate
from scripts.aws_target.aws_target import AwsCli, AwsUnavailable
from scripts.aws_target.config import read_settings, account_settings_path, gate_deployment, gate_regions
from scripts.lib import conf
from scripts.build_queue.queue_place import defer_cleanup_signals
from scripts.build_targets import target_config
from scripts.production.gate_modules import GateRefused
from scripts.production.merge_gate import SuiteMode


NIXOS_LOCK = Path('/etc/nixos/flake.lock')


def host_nixpkgs_revision() -> str:
    """Read the host's locked nixpkgs input before creating trial resources."""
    lock = ledger.mapping(cast(object, json.loads(NIXOS_LOCK.read_text())))
    nodes = ledger.mapping(lock['nodes'])
    inputs = ledger.mapping(ledger.mapping(nodes[ledger.text(lock, 'root')])['inputs'])
    locked = ledger.mapping(ledger.mapping(nodes[ledger.text(inputs, 'nixpkgs')])['locked'])
    return ledger.text(locked, 'rev')


TRIAL_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


class TrialCancelled(Exception):
    """The user stopped the trial."""


def main(argv: Sequence[str], settings_path: Path, directory: Path) -> int:
    if '--paired-worker' in argv:
        return paired_worker_main(argv, settings_path, directory)
    return paired_main(argv, directory)


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
    tips = {line.split()[0] for line in checked_git(repo, 'ls-remote', '--heads', '--tags', url).splitlines()}
    revisions: list[PushedRevision] = []
    for ref in (base, changed):
        commit = checked_git(repo, 'rev-parse', '--verify', ref + '^{commit}')
        if not any(pushed_in(repo, commit, tip) for tip in tips):
            raise ValueError('Both revisions must be pushed refs')
        tree = checked_git(repo, 'rev-parse', commit + '^{tree}')
        for module in modules:
            if checked_git(repo, 'cat-file', '-t', commit + ':scripts/' + module) != 'blob':
                raise ValueError('The module is absent from a requested tree')
        revisions.append(PushedRevision(commit, tree))
    return PairedTrialRequest(revisions[0], revisions[1], tuple(modules), rounds, url)


def pushed_in(repo: Path, commit: str, tip: str) -> bool:
    """A remote tip holds the commit; a tip absent locally holds nothing known."""
    if tip == commit:
        return True
    ancestor = subprocess.run(['git', '-C', str(repo), 'merge-base', '--is-ancestor', commit, tip], capture_output=True)
    return ancestor.returncode == 0


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
    day = datetime.fromtimestamp(worker.clock(), UTC).date().isoformat()
    rate, reserved_type = max((ledger.gate_rate(readings, size, fallback, int(worker.clock()), day).usd_per_hour, size)
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
    state = book.priced_snapshot(now)
    intervals = ledger.gate_compute_intervals(state, now, policy.box_minimum_seconds, reserve=True)
    for reservation in state.gate_reservations:
        if reservation.attempt not in attempts or isinstance(reservation.state, ledger.GateAbsent):
            continue
        confirmed = isinstance(reservation.state, ledger.GateTerminated)
        complete = complete and confirmed
        total += sum((row.usd for row in intervals if row.source == 'gate:' + reservation.attempt), Decimal(0))
        total += ledger.gate_allowance(reservation).usd
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
                            worker.box_region = str(launch.get('region', worker.api.region))
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
        home = conf.read(account_settings_path()).text('region')
        _ = gate_regions(home)
        deployment = gate_deployment(directory / 'gate-deployment.json', home)
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
