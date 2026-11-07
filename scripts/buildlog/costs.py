"""Rank recorded build costs without changing the index or progress history."""

from __future__ import annotations

import sqlite3
import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from pathlib import Path

from scripts.buildlog import index
from scripts.buildlog import mac_outcome
from scripts.buildlog import moments
from scripts.buildlog import store


type Cause = Literal["Memory wait", "Cold rebuilds", "Reruns after a pass", "Failed runs", "Cancelled CI", "Mac idle while natedev waits"]


@dataclass(frozen=True)
class CostUnavailable:
    reason: str


@dataclass(frozen=True)
class RankedCost:
    machine: str
    cause: Cause
    seconds: float
    unknown_seconds: float = 0.0
    missing_waits: int = 0


@dataclass(frozen=True)
class MacIdleEstimate:
    candidate_wait_seconds: float
    execution_overlap_seconds: float
    recorded_hold_seconds: float
    upper_bound_seconds: float
    unknown_seconds: float


@dataclass(frozen=True)
class MacHoldEvents:
    recorded: list[tuple[float, int, int]]
    unknown: list[tuple[float, int, int]]


@dataclass(frozen=True)
class BuildCosts:
    start: datetime
    end: datetime
    phases: tuple[moments.ProgressFinish, ...]
    ranked: tuple[RankedCost, ...]
    mac_idle: MacIdleEstimate


FOOTER = (
    "Definitions: phase wall time = finish minus phase_started_at; finishes select the window. Costs can overlap.",
    f"Memory wait = recorded wait; cold = {moments.COLD_BUILD_MIN_CRATES}+ crates; failed = counted nonzero step status or failed CI.",
    f"Reruns after a pass = same-worker nextest within {moments.SAME_WORKER_REPEAT_SECONDS // 60} min, same recorded tree, worktree, and build input.",
    "CI = summed job wall time (run wall time if jobs are absent); cancelled = cancelled workflow attempts.",
    "Mac idle while natedev waits = candidate wait intervals minus Mac execution and recorded holds; missing records remain unknown.",
)
OUTPUT_LINE_LIMIT = 24
MAX_RANKED_ROWS = OUTPUT_LINE_LIMIT - 4 - len(FOOTER) - 1


def machine_name(target: str | None, host: str | None, legacy_hosts: dict[str, str]) -> str:
    if target:
        return target
    if not legacy_hosts:
        from scripts.buildlog import record

        values = record.sync_config()
        targets = set(record.configured_targets()) | {
            key.removeprefix("sync.hosts.") for key in values if key.startswith("sync.hosts.")
        }
        legacy_hosts.update((name, target) for target in targets for name in record._target_hosts(values, target))
    return legacy_hosts.get(host or "", host or "unknown")


def finished_phases(start: datetime, end: datetime) -> tuple[moments.ProgressFinish, ...]:
    lower, upper = start.timestamp(), end.timestamp()
    return tuple(event for event in moments.progress_events() if lower <= event.timestamp_epoch < upper)


def mac_hold_events(connection: sqlite3.Connection, start: datetime, end: datetime) -> MacHoldEvents:
    from scripts.buildlog import holds

    current = holds.CurrentHolds("", holds.NatedevHoldsNow(()), holds.MacHoldsNow(()))
    history = holds.read_hold_history(connection, current, end)
    recorded: list[tuple[float, int, int]] = []
    unknown: list[tuple[float, int, int]] = []
    for interval in history.intervals:
        if interval.target != "mac":
            continue
        began = interval.began if isinstance(interval.began, datetime) else interval.began.earliest
        complete = isinstance(interval.began, datetime) and isinstance(interval.ended, datetime)
        if isinstance(interval.ended, datetime):
            through = interval.ended
        elif isinstance(interval.ended, holds.OpenHold):
            through = interval.ended.through
        elif isinstance(interval.ended.next_event, holds.NextHoldEvent):
            through = interval.ended.next_event.at
        else:
            through = end
        low, high = max(start, began), min(end, through)
        if high > low:
            events = recorded if complete else unknown
            events.extend(((low.timestamp(), 0, 1), (high.timestamp(), 0, -1)))
    return MacHoldEvents(recorded, unknown)


def idle_seconds(events: list[tuple[float, int, int]]) -> float:
    """Count each wall second once, with a local waiter and no recorded Mac step."""
    waiting = running = 0
    previous = total = 0.0
    for at, wait_delta, run_delta in sorted(events):
        if waiting > 0 and running == 0:
            total += at - previous
        waiting += wait_delta
        running += run_delta
        previous = at
    return total


@dataclass(frozen=True)
class CostContribution:
    at: float
    machine: str
    cause: Cause
    seconds: float | CostUnavailable


@dataclass(frozen=True)
class SourceObservation:
    at: float
    machine: str


@dataclass(frozen=True)
class IndexedSourceCoverage:
    machine: str
    start: float
    end: float


@dataclass(frozen=True)
class IndexedMonthlySource:
    machine: str
    start: float
    end: float
    complete: bool
    path: str


def indexed_monthly_sources(connection: sqlite3.Connection) -> tuple[IndexedMonthlySource, ...]:
    """Keep nonempty indexed sources, including missing or incomplete files."""
    rows = cast(list[tuple[str, int]], connection.execute("SELECT path, offset FROM files").fetchall())
    sources: list[IndexedMonthlySource] = []
    legacy_hosts: dict[str, str] = {}
    for name, offset in rows:
        path = Path(name)
        if len(path.parts) != 2 or offset == 0:
            continue
        try:
            low = datetime.strptime(path.name, "%Y-%m.jsonl").replace(tzinfo=UTC)
            high = (low.replace(day=28)+timedelta(days=4)).replace(day=1)
        except ValueError:
            continue
        try:
            complete = (store.root()/path).stat().st_size == offset
        except OSError:
            complete = False
        machine = "CI" if path.parent.name == "ci" else machine_name(None, path.parent.name, legacy_hosts)
        sources.append(IndexedMonthlySource(machine, low.timestamp(), high.timestamp(), complete, name))
    return tuple(sources)


def first_source_times(connection: sqlite3.Connection, sources: Sequence[IndexedMonthlySource]) -> dict[str, float]:
    """Find each machine's first indexed event through the source indexes."""
    event_queries: list[str] = []
    for kind in index.ADDERS:
        table = kind if kind in index.TABLES else kind + "s"
        columns = index.TABLES[table][0]
        names = {name for name, _, _ in columns}
        stamp = "at" if "at" in names else "started_at" if "started_at" in names else ""
        if stamp:
            event_queries.append(f"SELECT min(unixepoch({stamp},'subsec')) AS at FROM {table} WHERE src = :src")
    query = "SELECT min(at) FROM (" + " UNION ALL ".join(event_queries) + ")"
    first: dict[str, float] = {}
    for source in sorted(sources, key=lambda item: item.start):
        if source.machine in first and source.start >= first[source.machine]:
            continue
        row = cast(tuple[float | None], connection.execute(query, {"src": source.path}).fetchone())
        if row[0] is not None:
            first[source.machine] = min(first.get(source.machine, row[0]), row[0])
    return first


def source_coverage(sources: Sequence[IndexedMonthlySource], observations: Sequence[SourceObservation]) -> tuple[IndexedSourceCoverage, ...]:
    """Recognize complete indexed monthly logs through their latest recorded event."""
    latest: dict[str, float] = {}
    for observation in observations:
        latest[observation.machine] = max(latest.get(observation.machine, 0), observation.at)
    coverage = [IndexedSourceCoverage(source.machine, source.start, min(source.end, latest.get(source.machine, 0)))
                for source in sources if source.complete and latest.get(source.machine, 0) > source.start]
    merged: list[IndexedSourceCoverage] = []
    for item in sorted(coverage, key=lambda span: (span.machine, span.start)):
        if merged and item.machine == merged[-1].machine and item.start <= merged[-1].end:
            previous = merged.pop()
            merged.append(IndexedSourceCoverage(item.machine, previous.start, max(previous.end, item.end)))
        else:
            merged.append(item)
    return tuple(merged)


@dataclass(frozen=True)
class IdleSpan:
    start: float
    end: float
    part: Literal["execution", "hold", "unknown", "known"]


@dataclass(frozen=True)
class CostReading:
    contributions: tuple[CostContribution, ...]
    observations: tuple[SourceObservation, ...]
    idle_spans: tuple[IdleSpan, ...]
    phases: tuple[moments.ProgressFinish, ...]
    coverage: tuple[IndexedSourceCoverage, ...] = ()
    first_sources: dict[str, float] = field(default_factory=dict)


def sweep_idle(events: list[tuple[float, int, int, int, int]],
               coverage: Sequence[IndexedSourceCoverage]) -> tuple[IdleSpan, ...]:
    """Sweep the full timeline before any bucket clips its disjoint spans."""
    waiting = running = held = unknown = covered = 0
    timeline = [(*event, 0) for event in events]
    for span in coverage:
        if span.machine == "mac":
            timeline.extend(((span.start, 0, 0, 0, 0, 1), (span.end, 0, 0, 0, 0, -1)))
    previous = 0.0
    spans: list[IdleSpan] = []
    for at, wait_delta, run_delta, hold_delta, unknown_delta, coverage_delta in sorted(timeline):
        if waiting > 0 and at > previous:
            part: Literal["execution", "hold", "unknown", "known"] = "execution" if running else "hold" if held else "unknown" if unknown or not covered else "known"
            spans.append(IdleSpan(previous, at, part))
        waiting += wait_delta
        running += run_delta
        held += hold_delta
        unknown += unknown_delta
        covered += coverage_delta
        previous = at
    return tuple(spans)


def read_costs(connection: sqlite3.Connection, start: datetime, end: datetime, *,
               boundary_context: bool = False) -> CostReading:
    """Read and pair costs once; retain timestamps for either presentation."""
    if end <= start:
        raise ValueError("cost window must have positive duration")
    parameters: dict[str, str | int | float] = dict(moments.window_parameters(start, end))
    lower, upper = start.timestamp(), end.timestamp()
    parameters.update(lower_epoch=lower, upper_epoch=upper, lower_us=int(lower * 1_000_000))
    legacy_hosts: dict[str, str] = {}
    context_sql = ""
    selected_sql = "SELECT id FROM window_steps"
    if boundary_context:
        _ = machine_name(None, None, legacy_hosts)
        parameters["legacy_hosts"] = json.dumps(legacy_hosts)
        machine_sql = "coalesce(nullif(target,''), json_extract(:legacy_hosts, '$.' || json_quote(host)), host, 'unknown')"
        context_sql = (
            ", seed_keys AS (SELECT DISTINCT session, " + machine_sql
            + " AS machine FROM window_steps WHERE step='nextest' AND session IS NOT NULL)"
            + ", context_ids AS MATERIALIZED (SELECT (SELECT id FROM steps AS prior"
            + " WHERE step='nextest' AND session=seed_keys.session AND started_at_us < :lower_us"
            + " AND " + machine_sql + " = seed_keys.machine"
            + " ORDER BY started_at_us DESC, id DESC LIMIT 1) AS id FROM seed_keys)"
        )
        selected_sql += (
            " UNION SELECT id FROM context_ids WHERE id IS NOT NULL"
            + " UNION SELECT id FROM steps WHERE duration_s > 0"
            + " AND unixepoch(started_at,'subsec')+duration_s > :lower_epoch"
            + " AND unixepoch(started_at,'subsec') < :lower_epoch"
            + " UNION SELECT id FROM steps WHERE mem_wait_s > 0"
            + " AND unixepoch(started_at,'subsec') >= :upper_epoch"
            + " AND unixepoch(started_at,'subsec')-mem_wait_s < :upper_epoch"
        )
    rows = cast(Sequence[tuple[str | None, str | None, str, float | None, float | None, int | None,
                               int | None, str | None, str | None, str | None, str | None,
                               str | None, float, int, str | None, str | None, str | None, float | None, str | None]],
        connection.execute(
            "WITH window_steps AS MATERIALIZED (SELECT id,target,host,session,step FROM steps WHERE "
            + moments.window_bounds("started_at") + ")" + context_sql
            + ", selected_ids AS (" + selected_sql + ")"
            + " SELECT target, host, step, duration_s, mem_wait_s, status, crates_compiled, session,"
            + " worktree, repo, build_base, tree_key, unixepoch(started_at, 'subsec'),"
            + " CASE WHEN unixepoch(started_at,'subsec') >= :lower_epoch THEN (" + index.COUNTED_FAILURE + ") ELSE 0 END,"
            + " c.mac,c.mac_reason,c.local_reason,c.mac_s,c.outcome"
            + " FROM steps LEFT JOIN"
            + " (SELECT id,mac,mac_reason,local_reason,mac_s,outcome FROM main.calls) c"
            + " ON c.id = steps.call_id AND unixepoch(started_at,'subsec') >= :lower_epoch"
            + " WHERE steps.id IN (SELECT id FROM selected_ids)"
            + " ORDER BY unixepoch(started_at, 'subsec'), steps.id", parameters).fetchall())
    contributions: list[CostContribution] = []
    observations: list[SourceObservation] = []
    sessions: dict[tuple[str, str], tuple[moments.PreviousNextest, moments.RecordedBuildTree | moments.UnrecordedIdentity]] = {}
    intervals: list[tuple[float, int, int]] = []
    for target, host, kind, duration, wait, status, crates, session, worktree, repo, base, tree, at, failed, mac, reason, local_reason, mac_s, outcome_text in rows:
        machine = machine_name(target, host, legacy_hosts)
        seconds, wait_s = max(0.0, duration or 0.0), max(0.0, wait or 0.0)
        selected = lower <= at < upper
        if selected:
            observations.append(SourceObservation(at, machine))
            if kind != "launch":
                wait_value = CostUnavailable("memory wait not recorded") if wait is None else wait_s
                contributions.append(CostContribution(at, machine, "Memory wait", wait_value))
            if crates is not None and crates >= moments.COLD_BUILD_MIN_CRATES:
                contributions.append(CostContribution(at, machine, "Cold rebuilds", seconds))
            if failed:
                contributions.append(CostContribution(at, machine, "Failed runs", seconds))
        if machine == "mac" and seconds and at < upper and at + seconds > lower:
            intervals.extend(((max(lower, at), 0, 1), (min(upper, at + seconds), 0, -1)))
        if machine == "natedev" and wait_s and at > lower and at - wait_s < upper:
            decision = mac_outcome.offload_record(mac, reason, local_reason, mac_s, outcome_text)
            if mac_outcome.is_mac_candidate(decision):
                intervals.extend(((max(lower, at - wait_s), 1, 0), (min(upper, at), -1, 0)))
        if kind != "nextest" or session is None:
            continue
        key = (moments.RecordedBuildTree(repo or "", worktree, base, tree)
               if worktree and base and tree else moments.UnrecordedIdentity())
        preceding = sessions.get((machine, session))
        if (selected and preceding is not None
                and moments.passed_tree_rerun(preceding[0], moments.nextest_outcome(status), at - wait_s, preceding[1], key)):
            contributions.append(CostContribution(at, machine, "Reruns after a pass", seconds))
        sessions[machine, session] = (moments.previous_nextest(at, duration, moments.nextest_outcome(status)), key)
    timeline = [(at, wait, run, 0, 0) for at, wait, run in intervals]
    if idle_seconds(intervals) > 0:
        holds = mac_hold_events(connection, start, end)
        timeline.extend((event[0], 0, 0, event[2], 0) for event in holds.recorded)
        timeline.extend((event[0], 0, 0, 0, event[2]) for event in holds.unknown)
    ci_rows = cast(Sequence[tuple[str | None, float, float]], connection.execute(
        "SELECT r.conclusion, coalesce((SELECT sum(j.duration_s) FROM ci_jobs j"
        + " WHERE j.run_id = r.run_id AND j.attempt = r.attempt AND j.run_state = 'ran'), r.duration_s, 0),"
        + " unixepoch(r.started_at,'subsec') FROM ci_runs r WHERE " + moments.window_bounds("r.started_at"),
        parameters).fetchall())
    for conclusion, seconds, at in ci_rows:
        observations.append(SourceObservation(at, "CI"))
        if conclusion in ("cancelled", "failure"):
            contributions.append(CostContribution(at, "CI", "Cancelled CI" if conclusion == "cancelled" else "Failed runs", max(0, seconds)))
    sources = indexed_monthly_sources(connection)
    coverage = source_coverage(sources, observations)
    first_sources = first_source_times(connection, sources)
    return CostReading(tuple(contributions), tuple(observations), sweep_idle(timeline, coverage),
                       finished_phases(start, end), coverage,
                       first_sources)


def aggregate_costs(reading: CostReading, start: datetime, end: datetime) -> BuildCosts:
    totals: defaultdict[tuple[str, Cause], float] = defaultdict(float)
    lower, upper = start.timestamp(), end.timestamp()
    missing: defaultdict[tuple[str, Cause], int] = defaultdict(int)
    for row in reading.contributions:
        if lower <= row.at < upper:
            key = row.machine, row.cause
            if isinstance(row.seconds, CostUnavailable):
                missing[key] += 1
                totals[key] += 0
            else:
                totals[key] += row.seconds
    parts: defaultdict[str, float] = defaultdict(float)
    for span in reading.idle_spans:
        parts[span.part] += max(0.0, min(upper, span.end)-max(lower, span.start))
    idle = parts["known"] + parts["unknown"]
    mac_idle = MacIdleEstimate(sum(parts.values()), parts["execution"], parts["hold"], idle, parts["unknown"])
    totals["mac", "Mac idle while natedev waits"] = idle
    ranked = tuple(sorted((RankedCost(host, cause, seconds, parts["unknown"] if cause == "Mac idle while natedev waits" else 0.0, missing[host, cause])
                           for (host, cause), seconds in totals.items() if seconds > 0 or missing[host, cause]),
                          key=lambda row: (-row.seconds, row.machine, row.cause)))
    phases = tuple(phase for phase in reading.phases if lower <= phase.timestamp_epoch < upper)
    return BuildCosts(start, end, phases, ranked, mac_idle)


def collect_costs(connection: sqlite3.Connection, start: datetime, end: datetime) -> BuildCosts:
    return aggregate_costs(read_costs(connection, start, end), start, end)


def phase_line(label: str, phases: Sequence[moments.ProgressFinish]) -> str:
    durations = [phase.wall_s for phase in phases if isinstance(phase.wall_s, float)]
    if not durations:
        return f"{label}: {len(phases)} finished; median unavailable; p90 unavailable."
    missing = f"; {len(phases) - len(durations)} without a recorded start" if len(durations) < len(phases) else ""
    return (f"{label}: {len(phases)} finished; median {moments.nearest_rank(durations, 50):.0f} s/phase; "
            + f"p90 {moments.nearest_rank(durations, 90):.0f} s/phase{missing}.")


def render_costs(build_costs: BuildCosts) -> str:
    days = (build_costs.end - build_costs.start).total_seconds() / 86400
    count = len(build_costs.phases)
    lines = [f"Build costs: {days:g} days ending {build_costs.end.isoformat()}.",
             phase_line("Hana phases", [phase for phase in build_costs.phases if moments.hana_name(phase.working_dir)]),
             phase_line("All phases", build_costs.phases), "Cost by machine: hours/day; hours/finished phase."]
    for row in build_costs.ranked[:MAX_RANKED_ROWS]:
        per_phase = f"{row.seconds / 3600 / count:.3f} h/phase" if count else "unavailable h/phase (no finished phases)"
        uncertainty = (f" upper bound; {row.unknown_seconds / 3600 / days:.2f} h/day unknown"
                       if row.unknown_seconds > 0 else "")
        if row.missing_waits:
            uncertainty += f"; subtotal with {row.missing_waits} applicable waits missing"
        lines.append(f"{row.machine}: {row.cause}: {row.seconds / 3600 / days:.2f} h/day; {per_phase}{uncertainty}.")
    if len(build_costs.ranked) > MAX_RANKED_ROWS:
        lines.append(f"{len(build_costs.ranked) - MAX_RANKED_ROWS} smaller machine/cause costs omitted.")
    if not build_costs.ranked:
        lines.append("No recorded costs in this window.")
    lines.extend(FOOTER)
    return "\n".join(lines)


def main(args: list[str]) -> int:
    from scripts.buildlog.number_option import positive_window

    window = positive_window(args, "--days", 7, "costs", integer=True)
    if window is None:
        return 2
    start, end = window[:2]
    connection = index.read_only()
    try:
        print(render_costs(collect_costs(connection, start, end)))
    finally:
        connection.close()
    return 0
