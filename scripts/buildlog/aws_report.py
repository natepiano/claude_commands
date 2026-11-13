"""Estimate AWS benefit from durable attempts and comparable local steps."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import sqlite3
from pathlib import Path
from statistics import median
from typing import TYPE_CHECKING, cast

from scripts.aws_target import ledger

if TYPE_CHECKING:
    from scripts.aws_target.measurement import MeasurementUnavailable, number
    from scripts.buildlog import record, store
    from scripts.mac_test import offload
else:
    from scripts.aws_target.measurement import MeasurementUnavailable, number
    from scripts.buildlog import record
    from scripts.buildlog import store
    from scripts.mac_test import offload


def daily_spend_source(spend: ledger.DailySpend) -> str:
    if spend.provisional_bill:
        return 'provisional bill'
    return 'bill' if spend.source == 'AWS bill' else 'estimate'


def daily_spend_lines(spends: list[ledger.DailySpend], limit: Decimal, today: str) -> list[str]:
    if not spends:
        return ['No spend days.', '']
    latest = next((row for row in spends if row.day == today), spends[0])
    label = 'Today' if latest.day == today else 'Latest day'
    source = daily_spend_source(latest)
    lines = [f'**{label} (UTC {latest.day}, {source}): ${latest.usd:.2f} of the ${limit:.2f} daily limit. '
             + f'{latest.runs} gate runs.**', '']
    lines.extend(('| Day (UTC) | Total | 32-CPU compute | 16-CPU compute | Storage | Other | Gate runs | Source |',
                  '|---|---:|---:|---:|---:|---:|---:|---|'))
    groups = ledger.SPEND_GROUPS[:3]
    for spend in spends:
        amounts = {row.group: row.usd for row in spend.groups}
        cells = [f'${amounts.get(group, Decimal(0)):.2f}' for group in groups]
        other = sum((usd for group, usd in amounts.items() if group not in groups), Decimal(0))
        source = daily_spend_source(spend)
        lines.append(f'| {spend.day} | ${spend.usd:.2f} | ' + ' | '.join(cells)
                     + f' | ${other:.2f} | {spend.runs} | {source} |')
    return [*lines, '']


@dataclass(frozen=True)
class GateCostReport:
    tables: list[str]
    source: str


@dataclass(frozen=True)
class FinishedGateRun:
    instance_type: str
    mode: str
    day: str
    test_s: Decimal
    box_s: Decimal


def run_cost_cell(runs: list[FinishedGateRun], allocations: dict[tuple[str, str, str], Decimal]) -> str:
    if not runs:
        return '—'
    billed = [run for run in runs if (run.instance_type, run.day, run.mode) in allocations]
    if not billed:
        return f'not billed yet ({len(runs)})'
    return f'${billed_cost_per_run(billed, allocations):.4f} per run ({len(runs)})'


def billed_cost_per_run(runs: list[FinishedGateRun], allocations: dict[tuple[str, str, str], Decimal]) -> Decimal:
    groups = {(run.instance_type, run.day, run.mode) for run in runs}
    return sum((allocations[key] for key in groups), Decimal(0)) / len(runs)


def trend_cost_cell(runs: list[FinishedGateRun], allocations: dict[tuple[str, str, str], Decimal]) -> str:
    if not runs:
        return '—'
    billed = [run for run in runs if (run.instance_type, run.day, run.mode) in allocations]
    if not billed:
        return 'not billed yet'
    return f'${billed_cost_per_run(billed, allocations):.4f}'


def median_run_time(seconds: list[Decimal]) -> str:
    if not seconds:
        return '—'
    minutes, remainder = divmod(int(median(seconds)), 60)
    return f'{minutes}m {remainder}s'


def gate_type_report(directory: Path, day: str, days: int) -> GateCostReport:
    """Compare costs and times by machine type, run kind, and UTC completion day."""
    end = datetime.fromisoformat(day).replace(tzinfo=UTC) + timedelta(days=1)
    start = end - timedelta(days=days)
    book = ledger.CostLedger(directory)
    rows = book.launches()
    launched = {ledger.text(row, 'attempt'): number(row['time']) for row in rows
                if row['result'] == 'launched' and 'mode' in row}
    runs: list[FinishedGateRun] = []
    fallbacks: list[tuple[datetime, dict[str, object]]] = []
    omitted = 0
    for row in rows:
        at = datetime.fromtimestamp(float(str(row['time'])), UTC)
        if start <= at < end:
            if row['result'] == 'fallback':
                fallbacks.append((at, row))
            if 'mode' not in row:
                omitted += 1
            elif row['result'] == 'finished':
                if ledger.text(row, 'attempt') not in launched:
                    omitted += 1
                    continue
                runs.append(FinishedGateRun(ledger.text(row, 'instance_type'), ledger.text(row, 'mode'),
                    at.date().isoformat(), number(row['gate_s']), number(row['time']) - launched[ledger.text(row, 'attempt')]))
    costs: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    billed_days: set[str] = set()
    for reading in book.read_bills():
        if not (start.date().isoformat() <= reading.day < end.date().isoformat() and ledger.is_billed_day(reading)):
            continue
        billed_days.add(reading.day)
        for usage in reading.usage_types:
            size = usage.usage_type.partition('SpotUsage:')[2]
            if size:
                costs[size, reading.day] += usage.usd
    kinds = (('merge-gate', 'merge gates'), ('targeted-test', 'targeted tests'))
    groups: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    for run in runs:
        groups[run.instance_type, run.day, run.mode] += run.box_s
    allocations: dict[tuple[str, str, str], Decimal] = {}
    for (size, at, mode), seconds in groups.items():
        if at in billed_days:
            total = sum((groups.get((size, at, kind), Decimal(0)) for kind, _label in kinds), Decimal(0))
            allocations[size, at, mode] = costs[size, at] * seconds / total
    lines = ['| Machine type | Merge gates | Targeted tests | All runs |', '|---|---:|---:|---:|']
    sizes = sorted({run.instance_type for run in runs})
    for size in sizes:
        selected = [run for run in runs if run.instance_type == size]
        cells = [run_cost_cell([run for run in selected if run.mode == mode], allocations) for mode, _label in kinds]
        lines.append(f'| {size} | ' + ' | '.join([*cells, run_cost_cell(selected, allocations)]) + ' |')
    if len(sizes) >= 2:
        cells = [run_cost_cell([run for run in runs if run.mode == mode], allocations) for mode, _label in kinds]
        lines.append('| All types | ' + ' | '.join([*cells, run_cost_cell(runs, allocations)]) + ' |')
    source = (f'Source: cloud-gate-launches.jsonl and charges/<day>.json; UTC {start.date()} through {end.date() - timedelta(days=1)} '
              + f'({days} days); billed days only; {omitted} rows omitted with no kind or matching launch.')
    lines.extend(('', '| Day (UTC) | Merge gates | Merge gate test time | Merge gate box time | Targeted tests | Targeted test time | Targeted box time | Cost per run |',
        '|---|---:|---:|---:|---:|---:|---:|---:|'))
    for at in sorted({run.day for run in runs}, reverse=True):
        selected = [run for run in runs if run.day == at]
        cells: list[str] = []
        for mode, _label in kinds:
            matching = [run for run in selected if run.mode == mode]
            cells.extend((str(len(matching)), median_run_time([run.test_s for run in matching]),
                          median_run_time([run.box_s for run in matching])))
        cost = trend_cost_cell(selected, allocations)
        lines.append(f'| {at} | ' + ' | '.join([*cells, cost]) + ' |')
    totals: list[str] = []
    for mode, _label in kinds:
        matching = [run for run in runs if run.mode == mode]
        totals.extend((str(len(matching)), median_run_time([run.test_s for run in matching]),
                       median_run_time([run.box_s for run in matching])))
    lines.append('| All shown days | ' + ' | '.join([*totals, trend_cost_cell(runs, allocations)]) + ' |')
    for at, row in sorted(fallbacks, key=lambda entry: entry[0], reverse=True):
        reason = next((line.strip() for line in str(row.get('reason', '')).splitlines() if line.strip()),
                      'Reason unavailable')
        reason = ' '.join(reason.split())
        if len(reason) > 200:
            reason = reason[:197] + '...'
        mode = str(row.get('mode', 'unknown mode'))
        lines.extend(('', f'- Cloud gate uses natedev: {reason} ({at:%Y-%m-%d %H:%M:%S UTC}, {mode}).'))
    return GateCostReport([*lines, ''], source)


@dataclass(frozen=True)
class WaitSavingEstimate:
    steps: int
    queue_saved_s: Decimal | MeasurementUnavailable
    run_saved_s: Decimal | MeasurementUnavailable



@dataclass(frozen=True)
class LocalComparison:
    command: object
    duration: Decimal | MeasurementUnavailable
    wait: Decimal | MeasurementUnavailable


def comparison_number(value: object) -> Decimal | MeasurementUnavailable:
    try:
        return number(value)
    except (ValueError, ArithmeticError) as error:
        return MeasurementUnavailable(str(error))

def estimate_wait_saved(connection: sqlite3.Connection, day: str, target: str = 'aws', *,
                        bounds: tuple[str, str]) -> WaitSavingEstimate:
    """Compare executed attempts within the report day bounds."""
    hosts = record.target_hosts(target)
    start, end = (int(datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp()) for value in bounds)
    if not day:
        raise ValueError('report day is required')
    attempts = cast(list[tuple[object, ...]], connection.execute(
        'SELECT a.call_id,a.repo_identity,a.queued_at,a.departed_at,a.outcome,c.command,c.verb'
        + ' FROM ssh_attempts a LEFT JOIN calls c ON c.id=a.call_id'
        + " WHERE a.target=? AND a.outcome IN ('passed','failed','lost','no_tests')"
        + ' AND a.departed_at>=? AND a.departed_at<? ORDER BY a.departed_at,a.id', (target, start, end)).fetchall())
    local_identity = record.local_step_identity()
    local_rows = cast(list[tuple[object, ...]], connection.execute(
        'SELECT s.repo_identity,s.step,c.command,s.duration_s,coalesce(s.mem_wait_s,0)'
        + '+coalesce(c.hold_wait_s,0)+coalesce(c.token_wait_s,0),s.target,s.host,s.caller'
        + ' FROM steps s LEFT JOIN calls c ON c.id=s.call_id WHERE s.started_at>=? AND s.started_at<?',
        (store.utc_iso(start), store.utc_iso(end))).fetchall())
    comparable: dict[tuple[str, str], list[LocalComparison]] = defaultdict(list)
    for repository, kind, command, duration, wait, selected, host, caller in local_rows:
        if record.is_local_step(selected, host, caller, targets=local_identity.targets, hosts=local_identity.hosts):
            comparable[str(repository), str(kind)].append(
                LocalComparison(command, comparison_number(duration), comparison_number(wait)))
    remote_rows = cast(list[tuple[object, ...]], connection.execute(
        'SELECT s.call_id,s.step,s.duration_s,s.target,s.host FROM steps s'
        + ' WHERE s.call_id IN (SELECT call_id FROM ssh_attempts WHERE target=? AND departed_at>=? AND departed_at<?)',
        (target, start, end)).fetchall())
    remote_times: dict[tuple[str, str], Decimal | MeasurementUnavailable] = {}
    for call_id, kind, duration, selected, host in remote_rows:
        if selected == target or (not selected and host in hosts):
            key = str(call_id), str(kind)
            duration_value = comparison_number(duration)
            previous = remote_times.get(key, Decimal(0))
            remote_times[key] = (previous + duration_value
                                 if isinstance(previous, Decimal) and isinstance(duration_value, Decimal)
                                 else MeasurementUnavailable('remote run evidence is unavailable'))
    queue_total = Decimal(0)
    run_total = Decimal(0)
    missing_queue = missing_run = False
    for call_id, repository, queued, departed, outcome, command, verb in attempts:
        if outcome not in ('passed', 'failed'):
            missing_queue = missing_run = True
            continue
        try:
            departure = number(departed) - number(queued)
            if departure < 0 or not repository or repository == 'unknown':
                raise ValueError('attempt lacks queue or repository evidence')
            kind = offload.step_kind(str(verb))
            rows = comparable[str(repository), kind]
            if any(isinstance(row.wait, MeasurementUnavailable) for row in rows):
                raise ValueError('local queue evidence is unavailable')
            waits = [row.wait - departure for row in rows if isinstance(row.wait, Decimal) and row.wait > departure]
            if waits:
                queue_total += median(waits)
            else:
                missing_queue = True
            matching = [row for row in rows if row.command == command and command]
            if any(isinstance(row.duration, MeasurementUnavailable) for row in matching):
                raise ValueError('local run evidence is unavailable')
            durations = [row.duration for row in matching if isinstance(row.duration, Decimal)]
            key = str(call_id), kind
            remote_duration = remote_times.get(key)
            if isinstance(remote_duration, Decimal) and durations:
                run_total += median(durations) - remote_duration
            else:
                missing_run = True
        except (ValueError, ArithmeticError):
            missing_queue = missing_run = True
    return WaitSavingEstimate(len(attempts), MeasurementUnavailable('queue comparisons are missing') if missing_queue else queue_total,
                              MeasurementUnavailable('run comparisons are missing') if missing_run else run_total)
