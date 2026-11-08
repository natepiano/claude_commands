"""Estimate AWS benefit from durable attempts and comparable local steps."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import sqlite3
from statistics import median
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from scripts.aws_target.measurement import MeasurementUnavailable, number
    from scripts.buildlog import record, store
    from scripts.mac_test import offload
else:
    from scripts.aws_target.measurement import MeasurementUnavailable, number
    from scripts.buildlog import record
    from scripts.buildlog import store
    from scripts.mac_test import offload


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
