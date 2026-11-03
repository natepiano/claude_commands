"""Rank recorded build costs without changing the index or progress history."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

import index
import mac_outcome
import moments


type Cause = Literal["Memory wait", "Cold rebuilds", "Reruns after a pass", "Failed runs", "Cancelled CI", "Mac idle while natedev waits"]


@dataclass(frozen=True)
class RankedCost:
    machine: str
    cause: Cause
    seconds: float
    unknown_seconds: float = 0.0


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
        import record

        targets = set(record.configured_targets()) | {
            key.removeprefix("sync.hosts.") for key in record.sync_config() if key.startswith("sync.hosts.")
        }
        legacy_hosts.update((name, target) for target in targets for name in record.target_hosts(target))
    return legacy_hosts.get(host or "", host or "unknown")


def finished_phases(start: datetime, end: datetime) -> tuple[moments.ProgressFinish, ...]:
    lower, upper = start.timestamp(), end.timestamp()
    return tuple(event for event in moments.progress_events() if lower <= event.timestamp_epoch < upper)


def mac_hold_events(connection: sqlite3.Connection, start: datetime, end: datetime) -> MacHoldEvents:
    import holds

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


def collect_costs(connection: sqlite3.Connection, start: datetime, end: datetime) -> BuildCosts:
    """Read each window step once; keep only pair history and interval endpoints."""
    if end <= start:
        raise ValueError("cost window must have positive duration")
    parameters = moments.window_parameters(start, end)
    # The calls CTE shadows main.calls for index.COUNTED_FAILURE.
    # The unbounded fragment took 764 ms over 14,460 window rows.
    rows = cast(Sequence[tuple[str | None, str | None, str, float | None, float | None, int | None,
                               int | None, str | None, str | None, str | None, str | None,
                               str | None, float, int, str | None, str | None, str | None, float | None, str | None]],
        connection.execute(
            "WITH window_steps AS MATERIALIZED (SELECT target,host,step,duration_s,mem_wait_s,status,crates_compiled,session,"
            + " worktree,repo,build_base,tree_key,started_at,caller,call_id,id FROM steps WHERE "
            + moments.window_bounds("started_at") + "),"
            + " calls AS MATERIALIZED (SELECT c.id,c.copy,c.mac,c.mac_reason,c.local_reason,c.mac_s,c.outcome"
            + " FROM window_steps s CROSS JOIN main.calls c WHERE c.id = s.call_id GROUP BY c.id)"
            + " SELECT target, host, step, duration_s, mem_wait_s, status, crates_compiled, session,"
            + " worktree, repo, build_base, tree_key, unixepoch(started_at, 'subsec'), (" + index.COUNTED_FAILURE + "),"
            + " c.mac,c.mac_reason,c.local_reason,c.mac_s,c.outcome"
            + " FROM window_steps AS steps LEFT JOIN calls c ON c.id = steps.call_id"
            + " ORDER BY unixepoch(started_at, 'subsec'), steps.id", parameters).fetchall())
    totals: defaultdict[tuple[str, Cause], float] = defaultdict(float)
    sessions: dict[tuple[str, str], tuple[moments.PreviousNextest, moments.RecordedBuildTree | moments.UnrecordedIdentity]] = {}
    intervals: list[tuple[float, int, int]] = []
    lower, upper = start.timestamp(), end.timestamp()
    legacy_hosts: dict[str, str] = {}
    for target, host, kind, duration, wait, status, crates, session, worktree, repo, base, tree, at, failed, mac, reason, local_reason, mac_s, outcome_text in rows:
        machine = machine_name(target, host, legacy_hosts)
        seconds, wait_s = max(0.0, duration or 0.0), max(0.0, wait or 0.0)
        totals[machine, "Memory wait"] += wait_s
        if crates is not None and crates >= moments.COLD_BUILD_MIN_CRATES:
            totals[machine, "Cold rebuilds"] += seconds
        if failed:
            totals[machine, "Failed runs"] += seconds
        if machine == "mac" and seconds:
            intervals.extend(((max(lower, at), 0, 1), (min(upper, at + seconds), 0, -1)))
        decision = mac_outcome.offload_record(mac, reason, local_reason, mac_s, outcome_text)
        if machine == "natedev" and wait_s and mac_outcome.is_mac_candidate(decision):
            intervals.extend(((max(lower, at - wait_s), 1, 0), (min(upper, at), -1, 0)))
        if kind != "nextest" or session is None:
            continue
        key = (moments.RecordedBuildTree(repo or "", worktree, base, tree)
               if worktree and base and tree else moments.UnrecordedIdentity())
        preceding = sessions.get((machine, session))
        if (preceding is not None
                and moments.passed_tree_rerun(preceding[0], moments.nextest_outcome(status), at - wait_s, preceding[1], key)):
            totals[machine, "Reruns after a pass"] += seconds
        sessions[machine, session] = (moments.previous_nextest(at, duration, moments.nextest_outcome(status)), key)
    candidate_wait = idle_seconds([(event[0], event[1], 0) for event in intervals if event[1]])
    idle = idle_seconds(intervals)
    execution_overlap = candidate_wait - idle
    hold_seconds = unknown_seconds = 0.0
    if idle > 0:
        holds = mac_hold_events(connection, start, end)
        intervals.extend(holds.recorded)
        after_holds = idle_seconds(intervals)
        hold_seconds = idle - after_holds
        idle = after_holds
        unknown_seconds = idle - idle_seconds(intervals + holds.unknown)
    mac_idle = MacIdleEstimate(candidate_wait, execution_overlap, hold_seconds, idle, unknown_seconds)
    totals["mac", "Mac idle while natedev waits"] = idle
    ci_rows = cast(Sequence[tuple[str, float]], connection.execute(
        "SELECT r.conclusion, coalesce((SELECT sum(j.duration_s) FROM ci_jobs j"
        + " WHERE j.run_id = r.run_id AND j.attempt = r.attempt AND j.run_state = 'ran'), r.duration_s, 0)"
        + " FROM ci_runs r WHERE " + moments.window_bounds("r.started_at")
        + " AND r.conclusion IN ('cancelled', 'failure')", parameters).fetchall())
    for conclusion, seconds in ci_rows:
        totals["CI", "Cancelled CI" if conclusion == "cancelled" else "Failed runs"] += max(0, seconds)
    ranked = tuple(sorted((RankedCost(host, cause, seconds, unknown_seconds if cause == "Mac idle while natedev waits" else 0.0)
                           for (host, cause), seconds in totals.items() if seconds > 0),
                          key=lambda row: (-row.seconds, row.machine, row.cause)))
    return BuildCosts(start, end, finished_phases(start, end), ranked, mac_idle)


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
        lines.append(f"{row.machine}: {row.cause}: {row.seconds / 3600 / days:.2f} h/day; {per_phase}{uncertainty}.")
    if len(build_costs.ranked) > MAX_RANKED_ROWS:
        lines.append(f"{len(build_costs.ranked) - MAX_RANKED_ROWS} smaller machine/cause costs omitted.")
    if not build_costs.ranked:
        lines.append("No recorded costs in this window.")
    lines.extend(FOOTER)
    return "\n".join(lines)


def main(args: list[str]) -> int:
    from number_option import positive_window

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
