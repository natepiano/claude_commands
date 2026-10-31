"""Build interventions, one hana population, and rates over explicit UTC windows."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import ClassVar, Literal, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import unit_runs  # noqa: E402


type MetricKey = Literal[
    "run_hours_all", "run_hours_hana", "wait_hours_all", "wait_hours_hana", "nextest",
    "failed", "reruns", "queue_hidden", "pass_reruns", "pass_hours", "fail_reruns", "fail_hours",
    "clippy", "clone_rebuilds", "clone_hours", "clone_repeats", "failed_pct", "reruns_pct",
    "queue_hidden_pct", "clone_repeats_pct", "nextest_per_phase", "clippy_per_phase",
]
type ComparisonSide = Literal["before", "after"]


@dataclass(frozen=True)
class NoSdlcBaseline:
    pass


@dataclass(frozen=True)
class SdlcRange:
    low: float
    high: float


@dataclass(frozen=True)
class BaselineFigure:
    key: MetricKey
    value: float | SdlcRange
    side: ComparisonSide = "before"
    basis: Literal["total", "rate"] = "rate"


@dataclass(frozen=True)
class SdlcBaseline:
    figures: tuple[BaselineFigure, ...]
    notes: tuple[str, ...] = ()
    window: Literal["adjacent", "week"] = "adjacent"


class MomentTrack(Enum):
    BUILD = "build"
    MEMORY = "memory"


@dataclass(frozen=True)
class BuildMoment:
    at: datetime
    name: str
    scope: Literal["all", "hana"]
    baseline: SdlcBaseline | NoSdlcBaseline = NoSdlcBaseline()
    track: ClassVar[MomentTrack] = MomentTrack.BUILD


@dataclass(frozen=True)
class MemoryMoment:
    at: datetime
    name: str
    track: ClassVar[MomentTrack] = MomentTrack.MEMORY


@dataclass(frozen=True)
class PendingMemoryMoment:
    name: str
    track: ClassVar[MomentTrack] = MomentTrack.MEMORY


type LiveMoment = BuildMoment | MemoryMoment
type Moment = LiveMoment | PendingMemoryMoment


MOMENTS: tuple[Moment, ...] = (
    BuildMoment(datetime(2026, 10, 28, 13, 1, tzinfo=UTC), "herdr cgroup move", "all"),
    BuildMoment(datetime(2026, 10, 28, 13, 24, tzinfo=UTC), "baloo content indexing off", "all"),
    BuildMoment(datetime(2026, 10, 29, 6, 59, tzinfo=UTC), "hana opt-level 1", "hana"),
    BuildMoment(datetime(2026, 10, 29, 9, 33, tzinfo=UTC), "hana Bevy dylib", "hana"),
    BuildMoment(datetime(2026, 10, 29, 18, 26, tzinfo=UTC), "Floor sweep (e3b8e391)", "all",
                SdlcBaseline((BaselineFigure("clone_repeats_pct", 52.7),
                              BaselineFigure("clone_repeats_pct", 29.8, "after"),
                              BaselineFigure("clone_rebuilds", 124, "after", "total")),
                             ("sdlc: clone repeats 52.7% before, 29.8% after; 124 clone rebuilds after.",))),
    BuildMoment(datetime(2026, 10, 31, 10, 50, tzinfo=UTC), "Build rules 1 (Moment 1)", "all",
                SdlcBaseline((
                    BaselineFigure("run_hours_all", 708, basis="total"),
                    BaselineFigure("run_hours_hana", 595, basis="total"),
                    BaselineFigure("wait_hours_all", 2125, basis="total"),
                    BaselineFigure("wait_hours_hana", 1329, basis="total"),
                    BaselineFigure("nextest", 246), BaselineFigure("reruns_pct", 16),
                    BaselineFigure("queue_hidden_pct", 46), BaselineFigure("failed_pct", 62),
                    BaselineFigure("clippy", 160), BaselineFigure("nextest_per_phase", 12.9),
                    BaselineFigure("clippy_per_phase", 8.4),
                    BaselineFigure("pass_reruns", 221, basis="total"),
                    BaselineFigure("pass_hours", 33.3, basis="total"),
                    BaselineFigure("fail_hours", SdlcRange(50, 55), basis="total"),
                    BaselineFigure("clone_rebuilds", 805, basis="total"),
                    BaselineFigure("clone_hours", 231, basis="total"),
                ), ("sdlc: clone rebuilds 805 runs, 231 of 327 hana nextest hours; 134 phases.",
                    "Fail then fail differs (ours about 80 h, sdlc 50–55 h); load ratios differ because sdlc's 134 phases include the Mac."), "week")),
    MemoryMoment(datetime(2026, 10, 31, 13, 33, tzinfo=UTC), "Build budget 21 → 24 GiB (e791376e)"),
)

HANA_PATTERNS = ("hana", "tool-based-ui", "catalyst", "fairy_dust")
OTHER_REPOS = ("cargo-liner", "cargo-handler", "cargo-tile", "cargo-berth", "bevy_brp", ".claude")
HANA_DEFINITION = (
    "Hana set: repo, repo_path, repo_identity, cwd, worktree, branch, target_folder, argv or build_base"
    " contains hana / tool-based-ui / catalyst / fairy_dust (case-insensitive), or a majority-hana"
    " session/unit_session or hana call, excluding cargo-liner, cargo-handler, cargo-tile, cargo-berth,"
    " bevy_brp and .claude from that inferred membership; classification uses the measured window."
)


def hana_name(value: str) -> bool:
    return any(pattern in value.lower() for pattern in HANA_PATTERNS)


def _matches(fields: tuple[str, ...], patterns: tuple[str, ...], separator: str = " ") -> str:
    text = "lower(" + f"||'{separator}'||".join(f"coalesce({field},'')" for field in fields) + ")"
    # CASE short-circuits even when this is a SELECT expression. Most rows
    # match the first name; do not concatenate their nine fields four times.
    return "(CASE " + " ".join(f"WHEN {text} GLOB '*{pattern}*' THEN 1" for pattern in patterns) + " ELSE 0 END)"


# sdlc_recheck.sql's nat predicate, with the measured window parameterized.
# Sessions include both hosts, as in that predicate; only the result is natedev.
HANA_CTE = f"""
WITH w AS (
    SELECT * FROM steps WHERE started_at >= :first_day AND started_at < :last_day
    AND julianday(started_at) >= julianday(:start) AND julianday(started_at) < julianday(:end)
), d AS MATERIALIZED (
    SELECT id, host, started_at, step, duration_s, mem_wait_s, status,
    crates_compiled, call_id, session, unit_session, cwd, repo,
    {_matches(('repo', 'repo_path', 'repo_identity', 'cwd', 'worktree', 'branch', 'target_folder', 'argv', 'build_base'), HANA_PATTERNS)} AS direct FROM w
), sess AS MATERIALIZED (
    SELECT k FROM (
        SELECT session k, direct FROM d WHERE session IS NOT NULL AND session <> ''
        UNION ALL SELECT unit_session, direct FROM d WHERE unit_session IS NOT NULL AND unit_session <> ''
    ) GROUP BY k HAVING sum(direct) > sum(1-direct)
), callh AS MATERIALIZED (
    SELECT id FROM calls WHERE started_at >= :first_day AND started_at < :last_day
    AND julianday(started_at) >= julianday(:start) AND julianday(started_at) < julianday(:end)
    AND ({_matches(('repo', 'repo_path', 'cwd', 'worktree', 'branch', 'package'), ('hana', 'tool-based-ui'), separator='')}
         OR session IN sess OR unit_session IN sess)
), population AS (
    SELECT d.*, CASE WHEN direct THEN 1
        WHEN NOT {_matches(('repo',), OTHER_REPOS)}
        AND (session IN sess OR unit_session IN sess OR call_id IN callh)
        THEN 1 ELSE 0 END AS is_hana FROM d
), nat AS MATERIALIZED (
    SELECT id, started_at, step, duration_s, mem_wait_s, status,
    crates_compiled, call_id, session, cwd FROM population WHERE is_hana AND host = 'natedev'
)
"""


def window_parameters(start: datetime, end: datetime) -> dict[str, str]:
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    # Indexed text bounds must admit offset-bearing spellings on adjacent
    # dates; the julianday predicates select the exact instants afterward.
    return {"start": start.isoformat(), "end": end.isoformat(),
            "first_day": (start.date() - timedelta(days=1)).isoformat(),
            "last_day": (end.date() + timedelta(days=2)).isoformat()}


def hana_step_ids(connection: sqlite3.Connection, start: datetime, end: datetime) -> frozenset[str]:
    rows = cast(list[tuple[str]], connection.execute(
        HANA_CTE + " SELECT id FROM nat", window_parameters(start, end)).fetchall())
    return frozenset(row[0] for row in rows)


@dataclass(frozen=True)
class PhaseFinished:
    timestamp_epoch: float
    phase_instance_id: str
    working_dir: str


def phase_events() -> tuple[PhaseFinished, ...]:
    """Read local natedev history; redirected build logs never read live history."""
    if os.environ.get("BUILDLOG_PROGRESS_DIR"):
        root = Path(os.environ["BUILDLOG_PROGRESS_DIR"])
    elif os.environ.get("BUILDLOG_DIR"):
        root = Path(os.environ["BUILDLOG_DIR"]) / "progress"
    else:
        import store

        if store.host_name() != "natedev":
            return ()
        root = unit_runs.history_root() / "runs"
    events: dict[str, PhaseFinished] = {}
    for path in root.glob("*.jsonl"):
        try:
            with path.open() as source:
                for line in source:
                    try:
                        value = cast(object, json.loads(line))
                    except ValueError:
                        continue
                    if not isinstance(value, dict):
                        continue
                    event = cast(dict[str, object], value)
                    at, instance, directory = (event.get(key) for key in ("timestamp_epoch", "phase_instance_id", "working_dir"))
                    if (event.get("event_type") == "phase_finished" and isinstance(at, (int, float))
                            and not isinstance(at, bool) and isinstance(instance, str) and isinstance(directory, str)
                            and event.get("host", "natedev") == "natedev" and hana_name(directory)):
                        events[instance] = PhaseFinished(float(at), instance, directory)
        except OSError:
            continue
    return tuple(events.values())


@dataclass(frozen=True)
class RateUnavailable:
    reason: str


@dataclass(frozen=True)
class RateRow:
    key: MetricKey
    label: str
    scope: Literal["all", "hana"]
    unit: Literal["hours", "runs", "percent", "per_phase"]


RATE_ROWS: tuple[RateRow, ...] = (
    RateRow("run_hours_all", "Run hours, all", "all", "hours"),
    RateRow("run_hours_hana", "Run hours, hana", "hana", "hours"),
    RateRow("wait_hours_all", "Wait hours, all", "all", "hours"),
    RateRow("wait_hours_hana", "Wait hours, hana", "hana", "hours"),
    RateRow("nextest", "Hana nextest steps", "hana", "runs"),
    RateRow("failed_pct", "Hana nextest failed", "hana", "percent"),
    RateRow("reruns_pct", "Hana nextest reruns", "hana", "percent"),
    RateRow("queue_hidden_pct", "Hana queue-hidden repeats", "hana", "percent"),
    RateRow("pass_reruns", "Same-worker after a pass, runs", "hana", "runs"),
    RateRow("pass_hours", "Same-worker after a pass, hours", "hana", "hours"),
    RateRow("fail_reruns", "Same-worker fail then fail, runs", "hana", "runs"),
    RateRow("fail_hours", "Same-worker fail then fail, hours", "hana", "hours"),
    RateRow("clippy", "Hana clippy steps", "hana", "runs"),
    RateRow("nextest_per_phase", "Hana nextest / phase", "hana", "per_phase"),
    RateRow("clippy_per_phase", "Hana clippy / phase", "hana", "per_phase"),
    RateRow("clone_rebuilds", "Hana clone rebuilds", "hana", "runs"),
    RateRow("clone_hours", "Hana clone rebuild hours", "hana", "hours"),
    RateRow("clone_repeats_pct", "Hana clone repeats", "hana", "percent"),
)


@dataclass(frozen=True)
class WindowRates:
    days: float
    totals: dict[MetricKey, float]
    rates: dict[MetricKey, float | RateUnavailable]
    phase_count: int


# One ordered read feeds all totals and both kinds of consecutive-pair history.
# Keep pair work out of SQLite's multiple window sorts and materializations.
RATE_SQL = HANA_CTE + """
SELECT step, duration_s, mem_wait_s, status, crates_compiled, call_id, session, cwd,
       unixepoch(started_at, 'subsec') t0, is_hana
FROM population WHERE host = 'natedev' ORDER BY t0, id
"""


@dataclass(frozen=True)
class UnrecordedIdentity:
    """SQL NULL identity; distinct from any recorded string, including empty."""


@dataclass(frozen=True)
class StepEndUnavailable:
    """A step without duration cannot supply an end for a later pair."""


@dataclass(frozen=True)
class PreviousNextest:
    ends_at: float | StepEndUnavailable
    call_id: str | UnrecordedIdentity
    outcome: Literal["passed", "failed", "unrecorded"]
    clone: bool


def window_totals(connection: sqlite3.Connection, start: datetime, end: datetime) -> dict[MetricKey, float]:
    """Accumulate every rate numerator in one pass over the measured population."""
    names: tuple[MetricKey, ...] = ("run_hours_all", "run_hours_hana", "wait_hours_all", "wait_hours_hana", "nextest",
             "failed", "reruns", "queue_hidden", "pass_reruns", "pass_hours", "fail_reruns", "fail_hours",
             "clippy", "clone_rebuilds", "clone_hours", "clone_repeats")
    totals: dict[MetricKey, float] = dict.fromkeys(names, 0.0)
    folders: dict[str, PreviousNextest] = {}
    sessions: dict[str, PreviousNextest] = {}
    # Nullable types stop at this sqlite boundary. Pair history gives absent
    # ends, identities and outcomes explicit domain states.
    rows = cast(Sequence[tuple[str, float | None, float | None, int | None, int | None,
                               str | None, str | None, str | None, float, int]],
                connection.execute(RATE_SQL, window_parameters(start, end)).fetchall())
    for kind, duration, wait, status, crates, call, session, cwd, at, is_hana in rows:
        hours, wait_hours = (duration or 0) / 3600, (wait or 0) / 3600
        totals["run_hours_all"] += hours
        totals["wait_hours_all"] += wait_hours
        if not is_hana:
            continue
        totals["run_hours_hana"] += hours
        totals["wait_hours_hana"] += wait_hours
        if kind == "clippy":
            totals["clippy"] += 1
        if kind != "nextest":
            continue
        totals["nextest"] += 1
        outcome: Literal["passed", "failed", "unrecorded"] = (
            "unrecorded" if status is None else "passed" if status == 0 else "failed")
        if outcome == "failed":
            totals["failed"] += 1
        clone = crates is not None and crates >= 300
        if clone:
            totals["clone_rebuilds"] += 1
            totals["clone_hours"] += hours
        call_id = call if call is not None else UnrecordedIdentity()
        submitted = at - (wait or 0)
        previous = folders.get(cwd.split("/target/verify-doc", 1)[0]) if cwd is not None else None
        if previous is not None and isinstance(previous.ends_at, float):
            if 0 <= at - previous.ends_at <= 600:
                totals["reruns"] += 1
            if submitted - previous.ends_at <= 600 and call_id != previous.call_id:
                totals["queue_hidden"] += 1
        previous = sessions.get(session) if session is not None else None
        if previous is not None and isinstance(previous.ends_at, float):
            if submitted - previous.ends_at <= 3600:
                if outcome == previous.outcome == "passed":
                    totals["pass_reruns"] += 1
                    totals["pass_hours"] += hours
                if outcome == previous.outcome == "failed":
                    totals["fail_reruns"] += 1
                    totals["fail_hours"] += hours
            if clone and previous.clone and at - previous.ends_at <= 14400:
                totals["clone_repeats"] += 1
        preceding = PreviousNextest(at + duration if duration is not None else StepEndUnavailable(),
                                    call_id, outcome, clone)
        if cwd is not None:
            folders[cwd.split("/target/verify-doc", 1)[0]] = preceding
        if session is not None:
            sessions[session] = preceding
    return totals


def window_rates(connection: sqlite3.Connection, start: datetime, end: datetime, *,
                 phases: Sequence[PhaseFinished] = ()) -> WindowRates:
    """One SQL population and consecutive-pair walk for every rate in this window."""
    days = (end - start).total_seconds() / 86400
    if days <= 0:
        raise ValueError("rate window must have positive duration")
    totals = window_totals(connection, start, end)
    phase_count = len({phase.phase_instance_id for phase in phases
                       if start.timestamp() <= phase.timestamp_epoch < end.timestamp() and hana_name(phase.working_dir)})
    rates: dict[MetricKey, float | RateUnavailable] = {
        row.key: totals[row.key] / days for row in RATE_ROWS if row.unit in ("hours", "runs")}
    percentages: tuple[tuple[MetricKey, MetricKey, MetricKey], ...] = (
        ("failed_pct", "failed", "nextest"), ("reruns_pct", "reruns", "nextest"),
        ("queue_hidden_pct", "queue_hidden", "nextest"), ("clone_repeats_pct", "clone_repeats", "clone_rebuilds"),
    )
    for key, numerator, denominator in percentages:
        rates[key] = (100 * totals[numerator] / totals[denominator] if totals[denominator]
                      else RateUnavailable(f"no {denominator}"))
    phase_ratios: tuple[tuple[MetricKey, MetricKey], ...] = (
        ("nextest", "nextest_per_phase"), ("clippy", "clippy_per_phase"))
    for kind, key in phase_ratios:
        rates[key] = (totals[kind] / phase_count if phase_count
                                      else RateUnavailable("no natedev hana phases"))
    return WindowRates(days, totals, rates, phase_count)


@dataclass(frozen=True)
class ComparisonWindow:
    moment: LiveMoment
    before_start: datetime
    after_end: datetime


def comparison_windows(moments: Sequence[Moment], end: datetime) -> tuple[ComparisonWindow, ...]:
    """Apply the before/after rule independently on each moment's track.

    Keep fixed interventions while their after window overlaps the last week.
    Observed profile changes retain their original seven-day display horizon.
    Earlier events still bound the before window even when no longer shown.
    """
    windows: list[ComparisonWindow] = []
    for track in MomentTrack:
        ordered = sorted((moment for moment in moments if not isinstance(moment, PendingMemoryMoment)
                          and moment.track == track and moment.at < end), key=lambda moment: moment.at)
        instants = sorted({moment.at for moment in ordered})
        boundaries = {at: (instants[i-1] if i else at-timedelta(days=7),
                           instants[i+1] if i+1 < len(instants) else end) for i, at in enumerate(instants)}
        windows.extend(ComparisonWindow(moment, max(moment.at-timedelta(days=7), boundaries[moment.at][0]),
                                        boundaries[moment.at][1]) for moment in ordered
                       if (boundaries[moment.at][1] > end-timedelta(days=7) if moment in MOMENTS
                           else moment.at >= end-timedelta(days=7)))
    return tuple(sorted(windows, key=lambda window: window.moment.at, reverse=True))
