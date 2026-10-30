"""buildlog report: one local day as markdown, a section per kind of step split by caller, then a summary.

The summary at the bottom comes three times, successes, failures and all: one
row per kind, every caller together, and a total. Steps under temp folders
appear as one scratch caller in each kind's table.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from typing import NamedTuple, cast

import ci
import holds
import index
import disk
import memory
import record
import store
import sync
import rust_release
from mac_outcome import (
    DECLINE_REASONS,
    CacheReuse,
    DurationNotRecorded,
    LocalRunRequested,
    MAC_STEP_CALLER,
    MacRunCompleted,
    MacRunLost,
    MacRunMatchedNoTest,
    NoOffloadRecord,
    OffloadDeclined,
    OffloadDeclineReason,
    OffloadRecord,
    RunnerResultUnavailable,
    is_mac_candidate,
    natedev_step_predicate,
    offload_record,
    offload_seconds,
    usable_share,
)

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from user_zone import user_zone  # noqa: E402

KIND_ORDER = ["fmt", "check", "clippy", "mend", "doc", "nextest", "sweep"]
CALLER_LABELS = {
    "verify": "verify.sh (agents)",
    "cargo-port": "cargo-port",
    "agent": "agent (direct)",
    "alias": "terminal",
    "validate_ci": "push gate",
    "brp-launch": "example launches (brp)",
    "hana-shot": "app launches (hana_shot)",
    "unknown": "unknown",
}
OUTCOME_ORDER = ["ran", "failed", "interrupted", "cancelled", "reused", "replayed", "superseded", "deferred"]
SCRATCH = "(cwd LIKE '/tmp/%' OR cwd LIKE '/var/folders/%' OR cwd LIKE '/private/var/folders/%')"
GROUP_AS_SCRATCH = f"({SCRATCH} AND caller != 'brp-launch')"
SCRATCH_LABEL = "scratch (temp folders)"
ON_DAY = "date(started_at, 'localtime') = ?"
COMMON_HEAD = ["Runs", "Failed", "Avg", "p95", "Range"]
COMMON_SQL = f"count(*), sum({index.COUNTED_FAILURE}), avg(duration_s), min(duration_s), max(duration_s), group_concat(duration_s)"
CI_STALE_AFTER_S = 2 * 3600
MAX_SAMPLE_GAP_S = 5 * 60
MIN_PROFILE_RUNS = 10
# 2026-10-01–04 log: 1 edit 12.54 min, 2–3 16.09, 4–7 20.94, 8+ 31.28.
# One test per two edits leaves headroom for most gaps to stay within 2–3;
# the 4–7 bin is where recovery time starts to climb.
TESTS_PER_EDIT_TARGET = 0.5
EDIT_BINS = ("0", "1", "2–3", "4–7", "8+")
NO_REBUILD_CRATES = 0
EDITED_CRATE_MIN_CRATES = NO_REBUILD_CRATES + 1
EDITED_CRATE_MAX_CRATES = 3
CASCADE_MIN_CRATES = EDITED_CRATE_MAX_CRATES + 1
CASCADE_MAX_CRATES = 49
COLD_BUILD_MIN_CRATES = CASCADE_MAX_CRATES + 1
NO_REBUILD_LABEL = "none"
EDITED_CRATE_LABEL = f"edited crate ({EDITED_CRATE_MIN_CRATES}–{EDITED_CRATE_MAX_CRATES} crates)"
CASCADE_LABEL = f"cascade ({CASCADE_MIN_CRATES}–{CASCADE_MAX_CRATES} crates)"
COLD_BUILD_LABEL = f"cold ({COLD_BUILD_MIN_CRATES}+ crates)"
REBUILD_LABELS = (NO_REBUILD_LABEL, EDITED_CRATE_LABEL, CASCADE_LABEL, COLD_BUILD_LABEL)
PACKAGE_PATTERN = re.compile(r"package\(([^)&|\s]+)\)")
COPY_WON = "won"
COPY_NATEDEV_WON = "natedev_won"
COPY_GAVE_WAY = "gave_way"
MIN_SAME_COMMAND_STEPS = 3

Row = tuple[object, ...]
ReportedWait = tuple[float, str, str, str]
HIDDEN_DECLINE_REASONS: tuple[OffloadDeclineReason, ...] = ("off", "repo")
DECLINE_LABELS: dict[OffloadDeclineReason, str] = {
    "off": "off",
    "repo": "repository",
    "linux_only": "Linux-only",
    "backoff": "backoff",
    "blocked": "blocked",
    "busy": "busy with our test",
    "state": "state unreadable",
    "unreachable": "unreachable",
    "mac_busy": "Mac busy",
    "battery": "on battery",
    "disk": "low disk space",
    "copy": "copy failed",
}


@dataclass(frozen=True)
class ReportedTestCall:
    """One test call with its execution and routing results."""

    call_id: str
    started_at: datetime
    package: str
    worktree: str
    command: str
    succeeded: bool
    ran_on_natedev: bool
    offload: OffloadRecord
    on_report_day: bool
    wall_seconds: float | DurationNotRecorded
    token_wait_seconds: float | DurationNotRecorded
    target_recorded: bool
    candidate: bool
    copy: str
    copy_seconds: float | DurationNotRecorded
    copy_count: int
    copy_natedev_seconds: float | DurationNotRecorded


@dataclass
class PackageMacTests:
    """Display values collected for one package's offload calls."""

    tried: int = 0
    failed: int = 0
    passed: int = 0
    mac_seconds: list[float] = field(default_factory=list)
    decline_counts: dict[OffloadDeclineReason, int] = field(default_factory=dict)
    local_requested: int = 0
    runner_unavailable: int = 0
    no_mac_entry: int = 0


@dataclass
class PackageNatedevTests:
    """Durations from test calls that ran a nextest step on natedev."""

    wall_seconds: list[float] = field(default_factory=list)
    token_wait_seconds: list[float] = field(default_factory=list)
    ran: int = 0


@dataclass(frozen=True)
class ComparableNatedevStep:
    """A natedev nextest step of a test call that ran to its end, for the copy saving estimate."""

    command: str
    package: str
    duration_s: float


@dataclass
class PackageMacCopies:
    """One package's Mac copies of still-compiling natedev tests."""

    copies: int = 0
    won: int = 0
    natedev_won: int = 0
    gave_way: int = 0
    other: int = 0
    saved_seconds: float = 0.0
    not_won_calls: int = 0
    not_won_mac_seconds: float = 0.0


@dataclass(frozen=True)
class DailyMacCopies:
    packages: dict[str, PackageMacCopies]

    @property
    def started(self) -> int:
        return sum(package.copies for package in self.packages.values())

    @property
    def won(self) -> int:
        return sum(package.won for package in self.packages.values())

    @property
    def saved_seconds(self) -> float:
        return sum(package.saved_seconds for package in self.packages.values())


def seconds(value: object) -> str:
    if not isinstance(value, int | float):
        return ""
    amount = float(value)
    if amount < 60:
        return f"{amount:.1f} s"
    if amount < 3600:
        return f"{amount / 60:.1f} min"
    return f"{amount / 3600:.1f} h"


def some_seconds(value: object) -> str:
    """seconds(), blank for zero: a table of outcomes where most rows saved nothing."""
    return seconds(value) if value else ""


def count(value: object) -> str:
    return str(int(value)) if isinstance(value, int | float) else ""


def gib(value: object) -> str:
    return f"{float(value) / 2**30:.1f} GiB" if isinstance(value, int | float) else ""


def nearest_rank(values: Sequence[float], percent: int) -> float:
    return sorted(values)[(percent * len(values) + 99) // 100 - 1]


def percentile_values(value: object) -> list[float]:
    return [float(part) for part in value.split(",")] if isinstance(value, str) and value else []


def p95(value: object) -> str:
    values = percentile_values(value)
    return seconds(nearest_rank(values, 95)) if values else ""


@dataclass(frozen=True)
class Column:
    title: str
    sql: str
    show: Callable[[object], str]


@dataclass(frozen=True)
class AverageColumn(Column):
    values_sql: str


@dataclass(frozen=True)
class KnownCallTrees:
    first: str
    last: str


@dataclass
class CallTreeHistory:
    first_step: tuple[str, str]
    last_step: tuple[str, str]
    first_tree: str
    last_tree: str


@dataclass
class DailyTestsPerEdit:
    tests: int = 0
    edits: int = 0


@dataclass
class FailureRecoveryBin:
    failures: int = 0
    recovered: int = 0
    minutes_to_green: float = 0.0
    recovery_minutes: list[float] = field(default_factory=list)


@dataclass
class TestsPerEditWindow:
    first_day: str
    daily: dict[str, DailyTestsPerEdit]
    recovery_bins: list[FailureRecoveryBin]


@dataclass(frozen=True)
class KnownCrateStep:
    crates_compiled: int
    duration_s: float
    compile_s: float
    compile_time_known: bool
    kind: str
    argv: str


@dataclass
class RebuildTiming:
    steps: int = 0
    compile_s: float = 0.0
    other_s: float = 0.0
    total_s: float = 0.0

    def add(self, step: KnownCrateStep) -> None:
        self.steps += 1
        self.compile_s += step.compile_s
        self.other_s += max(0.0, step.duration_s - step.compile_s)
        self.total_s += step.duration_s


@dataclass
class PackageRebuilds:
    steps: int = 0
    compile_s: float = 0.0
    compile_samples: list[float] = field(default_factory=list)

    def add(self, step: KnownCrateStep) -> None:
        self.steps += 1
        self.compile_s += step.compile_s
        if step.compile_time_known:
            self.compile_samples.append(step.compile_s)


@dataclass(frozen=True)
class TokenHolder:
    call_id: str
    unit_session: str
    worker: str
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True)
class TokenWaitAttribution:
    behind_another_worker_s: float
    behind_own_call_s: float


@dataclass(frozen=True)
class LongestWait:
    seconds: float
    name: str
    started_at: str


class NoWait:
    """No positive wait was recorded."""


NO_WAIT = NoWait()


@dataclass(frozen=True)
class WaitMeasurements:
    recorded: int
    waits: list[ReportedWait]
    longest: LongestWait | NoWait = field(init=False)

    def __post_init__(self) -> None:
        positive = [entry for entry in self.waits if entry[0] > 0]
        if positive:
            duration, _, name, at = max(positive, key=lambda entry: entry[0])
            object.__setattr__(self, "longest", LongestWait(duration, name, at))
        else:
            object.__setattr__(self, "longest", NO_WAIT)


@dataclass(frozen=True)
class WaitingRow:
    name: str
    measurements: WaitMeasurements
    unit: str


@dataclass(frozen=True)
class DailyWaits:
    another_worker: WaitMeasurements
    own_call: WaitMeasurements
    memory: WaitMeasurements
    ci_queue: WaitMeasurements
    held_target: WaitMeasurements
    hold_not_recorded: int
    rows: tuple[WaitingRow, ...] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", (
            WaitingRow("Build-folder turn, behind another worker", self.another_worker, "calls"),
            WaitingRow("Build-folder turn, behind its own call", self.own_call, "calls"),
            WaitingRow("Held target", self.held_target, "calls"),
            WaitingRow("Memory admission", self.memory, "steps"),
            WaitingRow("CI queue", self.ci_queue, "jobs"),
        ))


EXTRA: dict[str, list[Column]] = {
    "check": [AverageColumn("Build", "avg(finished_s)", seconds, "finished_s"), Column("Warnings", "sum(warnings)", count)],
    "clippy": [
        AverageColumn("Build", "avg(finished_s)", seconds, "finished_s"),
        Column("Warnings", "sum(warnings)", count),
        Column("Errors", "sum(errors)", count),
    ],
    "mend": [
        AverageColumn("Mend's own", "avg(mend_s)", seconds, "mend_s"),
        AverageColumn("Check", "avg(mend_check_s)", seconds, "mend_check_s"),
        Column("Fixes", "sum(mend_fixes)", count),
    ],
    "doc": [AverageColumn("Build", "avg(finished_s)", seconds, "finished_s"), Column("Warnings", "sum(warnings)", count)],
    "nextest": [
        AverageColumn("Build", "avg(finished_s)", seconds, "finished_s"),
        Column("Tests", "sum(tests_run)", count),
        Column("Tests failed", "sum(tests_failed)", count),
        Column("Flaky", "sum(tests_flaky)", count),
    ],
    "sweep": [Column("Freed", "sum(sweep_freed_bytes)", gib)],
}


def table(head: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(head) + " |", "|" + "|".join(["---"] + ["--:"] * (len(head) - 1)) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def common(row: Row) -> list[str]:
    runs, failed, average, low, high, values = row[:6]
    span = seconds(low) if low == high else f"{seconds(low)} – {seconds(high)}"
    return [count(runs), count(failed), seconds(average), p95(values), span]


def fetch(connection: sqlite3.Connection, sql: str, *params: object) -> list[Row]:
    return cast(list[Row], connection.execute(sql, params).fetchall())


def natedev_call_predicate(call: str = "calls") -> str:
    """SQL leaving out calls whose test command completed on the Mac."""
    return f"coalesce({call}.mac, '') NOT IN ('failed', 'passed_filter', 'passed_full')"


def kinds(connection: sqlite3.Connection, day: str) -> list[str]:
    found = [
        cast(str, row[0])
        for row in fetch(
            connection,
            f"SELECT DISTINCT step FROM steps WHERE {ON_DAY} AND {natedev_step_predicate()}",
            day,
        )
    ]
    return [kind for kind in KIND_ORDER if kind in found] + sorted(kind for kind in found if kind not in KIND_ORDER)


def caller_label(caller: object, host: object, hosts: int) -> str:
    label = CALLER_LABELS.get(str(caller), str(caller))
    return f"{label} ({host})" if hosts > 1 else label


def wait_row(row: WaitingRow) -> list[str]:
    """Summarize measured waits, keeping parallel workers as separate time."""
    label, measurements, unit = row.name, row.measurements, row.unit
    if not measurements.recorded:
        return [label, "none", "", "", "", ""]
    positive = [(duration, owner, name, at) for duration, owner, name, at in measurements.waits if duration > 0]
    waited = f"{len(positive)} of {measurements.recorded} {unit}"
    longest = measurements.longest
    if isinstance(longest, NoWait):
        return [label, "none", "0", waited, f"0.0 {'job' if unit == 'jobs' else 'worker'}-hours", ""]
    when = memory.local_time(call_time(longest.started_at))
    totals: dict[str, float] = {}
    for duration, owner, _, _ in positive:
        totals[owner] = totals.get(owner, 0.0) + duration
    worst = ", ".join(
        f"{owner} {seconds(duration)}" for owner, duration in sorted(totals.items(), key=lambda item: (-item[1], item[0]))[:3]
    )
    hours = f"{sum(duration for duration, _, _, _ in positive) / 3600:.1f} {'job' if unit == 'jobs' else 'worker'}-hours"
    return [label, f"{seconds(longest.seconds)} ({longest.name}, {when})", str(sum(duration > 300 for duration, _, _, _ in positive)), waited, hours, worst]


def wait_seconds(value: object) -> float:
    return float(value) if isinstance(value, int | float) else 0.0


def call_time(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def local_time(at: str) -> str:
    return call_time(at).astimezone(user_zone()).strftime("%Y-%m-%d %H:%M:%S %Z")


def token_holders(rows: list[Row]) -> list[TokenHolder]:
    holders: list[TokenHolder] = []
    for call_id, started_at, wait_s, ended_at, worker, unit_session in rows:
        starts_at = call_time(started_at) + timedelta(seconds=wait_seconds(wait_s))
        ends_at = call_time(ended_at)
        if starts_at < ends_at:
            holders.append(TokenHolder(str(call_id), str(unit_session), str(worker), starts_at, ends_at))
    return holders


def attribute_token_wait(
    call_id: str,
    unit_session: str,
    worker: str,
    starts_at: datetime,
    wait_s: float,
    token_wait_s: float,
    holders: Sequence[TokenHolder],
) -> TokenWaitAttribution:
    wait_ends_at = starts_at + timedelta(seconds=wait_s)
    wait_starts_at = wait_ends_at - timedelta(seconds=token_wait_s)
    overlapping = [
        holder for holder in holders
        if holder.call_id != call_id and holder.unit_session == unit_session
        and holder.starts_at < wait_ends_at and holder.ends_at > wait_starts_at
    ]
    boundaries = sorted({wait_starts_at, wait_ends_at, *(point for holder in overlapping for point in (holder.starts_at, holder.ends_at))})
    own_s = 0.0
    for left, right in zip(boundaries, boundaries[1:]):
        if left < wait_starts_at or right > wait_ends_at:
            continue
        covering = [holder for holder in overlapping if holder.starts_at < right and holder.ends_at > left]
        if covering and all(holder.worker == worker for holder in covering):
            own_s += (right - left).total_seconds()
    own_s = min(token_wait_s, own_s)
    return TokenWaitAttribution(token_wait_s - own_s, own_s)


def build_folder_waits(connection: sqlite3.Connection, day: str) -> tuple[WaitMeasurements, WaitMeasurements]:
    start, end = day_range(day)
    recorded = cast(int, fetch(
        connection,
        "SELECT count(*) FROM calls WHERE tool = 'verify.sh' AND started_at >= ? AND started_at < ?"
        + f" AND {natedev_call_predicate()}", start, end,
    )[0][0])
    calls = fetch(
        connection,
        "SELECT id, token_wait_s, coalesce(worktree_name, '(unknown worktree)'), started_at,"
        + " coalesce(wait_s, 0), coalesce(worker, '(unknown worker)'), unit_session FROM calls"
        + " WHERE tool = 'verify.sh' AND started_at >= ? AND started_at < ? AND token_wait_s > 0"
        + f" AND {natedev_call_predicate()}",
        start, end,
    )
    holder_rows = fetch(
        connection,
        "SELECT id, started_at, coalesce(wait_s, 0), ended_at, coalesce(worker, '(unknown worker)'), unit_session"
        + " FROM calls WHERE tool = 'verify.sh' AND ended_at IS NOT NULL AND unit_session IS NOT NULL"
        + " AND ended_at >= ?"
        + f" AND {natedev_call_predicate()}",
        start,
    ) if any(wait_seconds(row[1]) > 0 and isinstance(row[6], str) for row in calls) else []
    holders_by_session: dict[str, list[TokenHolder]] = {}
    for holder in token_holders(holder_rows):
        holders_by_session.setdefault(holder.unit_session, []).append(holder)
    another_waits: list[ReportedWait] = []
    own_waits: list[ReportedWait] = []
    for call_id, duration, tree, started_at, wait_s, worker, unit_session in calls:
        token_wait_s = wait_seconds(duration)
        attribution = TokenWaitAttribution(token_wait_s, 0.0) if token_wait_s <= 0 or not isinstance(unit_session, str) else attribute_token_wait(
            str(call_id), unit_session, str(worker), call_time(started_at), wait_seconds(wait_s), token_wait_s,
            holders_by_session.get(unit_session, []),
        )
        identity = (str(tree), str(tree), str(started_at))
        another_waits.append((attribution.behind_another_worker_s, *identity))
        own_waits.append((attribution.behind_own_call_s, *identity))
    return WaitMeasurements(recorded, another_waits), WaitMeasurements(recorded, own_waits)


def daily_waits(connection: sqlite3.Connection, day: str) -> DailyWaits:
    another_waits, own_waits = build_folder_waits(connection, day)
    start, end = day_range(day)
    steps = fetch(
        connection,
        "SELECT mem_wait_s, coalesce(worktree_name, '(unknown worktree)'), worker, started_at FROM steps"
        + " WHERE started_at >= ? AND started_at < ? AND mem_wait_s > 0"
        + f" AND {natedev_step_predicate()}",
        start, end,
    )
    jobs = fetch(
        connection,
        "SELECT j.queued_s, j.workflow, j.name, j.created_at FROM ci_jobs AS j"
        + " JOIN ci_runs AS r ON j.run_id = r.run_id AND j.attempt = r.attempt"
        + " WHERE date(r.started_at, 'localtime') = ? AND j.run_state = 'ran' AND j.queued_s IS NOT NULL",
        day,
    )
    held_waits, unrecorded = held_target_waits(connection, day)
    memory_recorded = cast(int, fetch(
        connection,
        "SELECT count(*) FROM steps WHERE started_at >= ? AND started_at < ? AND mem_wait_s IS NOT NULL"
        + f" AND {natedev_step_predicate()}", start, end,
    )[0][0])
    return DailyWaits(
        another_waits, own_waits,
        WaitMeasurements(memory_recorded, [(wait_seconds(duration), str(tree), f"{tree} {worker}" if worker else str(tree), str(at))
         for duration, tree, worker, at in steps]),
        WaitMeasurements(len(jobs), [(wait_seconds(duration), f"{workflow} / {name}", f"{workflow} / {name}", str(at))
         for duration, workflow, name, at in jobs]), held_waits, unrecorded,
    )


def waiting_section(connection: sqlite3.Connection, day: str) -> list[str]:
    return waiting_lines(daily_waits(connection, day))


def waiting_lines(waits: DailyWaits) -> list[str]:
    held_waits, unrecorded = waits.held_target, waits.hold_not_recorded
    rows = [wait_row(row) for row in waits.rows]
    held_row = next(row for row in rows if row[0] == "Held target")
    if unrecorded:
        if not held_waits.recorded:
            held_row[1:5] = ["not recorded"] * 4
        else:
            held_row[3] += f"; not recorded for {unrecorded} {'call' if unrecorded == 1 else 'calls'}"
    source = "Source: verify.sh calls, memory-gated steps and CI jobs; a worker's own calls run one at a time, so waiting behind its own call adds no delay, behind another worker does."
    return ["### Waiting", "", *table(["Wait", "Longest", "Over 5 min", "Waited", "Total", "Worst"], rows), source, ""]


def held_target_waits(connection: sqlite3.Connection, day: str) -> tuple[WaitMeasurements, int]:
    start, end = day_range(day)
    recorded, unrecorded = fetch(
        connection,
        "SELECT count(hold_wait_s), count(*) - count(hold_wait_s) FROM calls"
        + " WHERE started_at >= ? AND started_at < ? AND tool = 'verify.sh'", start, end,
    )[0]
    rows = fetch(
        connection,
        "SELECT hold_wait_s, coalesce(worktree_name, '(unknown worktree)'), started_at FROM calls"
        + " WHERE started_at >= ? AND started_at < ? AND tool = 'verify.sh' AND hold_wait_s > 0",
        start, end,
    )
    return (
        WaitMeasurements(cast(int, recorded), [(wait_seconds(duration), str(tree), str(tree), str(at))
         for duration, tree, at in rows]),
        cast(int, unrecorded),
    )


def rebuild_bin(crates_compiled: int) -> str:
    if crates_compiled == NO_REBUILD_CRATES:
        return NO_REBUILD_LABEL
    if crates_compiled <= EDITED_CRATE_MAX_CRATES:
        return EDITED_CRATE_LABEL
    if crates_compiled < COLD_BUILD_MIN_CRATES:
        return CASCADE_LABEL
    return COLD_BUILD_LABEL


def known_crate_steps(connection: sqlite3.Connection, day: str) -> list[KnownCrateStep]:
    rows = fetch(
        connection,
        f"SELECT crates_compiled, duration_s, finished_s, step, argv FROM steps WHERE {ON_DAY}"
        + " AND step <> 'sweep' AND crates_compiled IS NOT NULL"
        + f" AND {natedev_step_predicate()}",
        day,
    )
    steps: list[KnownCrateStep] = []
    for crates, duration, finished, kind, argv in rows:
        compile_time_known = isinstance(finished, int | float)
        steps.append(
            KnownCrateStep(
                cast(int, crates),
                float(duration) if isinstance(duration, int | float) else 0.0,
                float(finished) if compile_time_known else 0.0,
                compile_time_known,
                str(kind),
                str(argv or ""),
            )
        )
    return steps


def rebuild_timings(steps: Sequence[KnownCrateStep]) -> dict[str, RebuildTiming]:
    timings = {label: RebuildTiming() for label in REBUILD_LABELS}
    for step in steps:
        timings[rebuild_bin(step.crates_compiled)].add(step)
    return timings


def whole_percent(part: float, total: float) -> str:
    return f"{part / total:.0%}" if total > 0 else "0%"


def rebuild_rows(timings: dict[str, RebuildTiming]) -> list[list[str]]:
    all_compile = sum(timing.compile_s for timing in timings.values())
    all_time = sum(timing.total_s for timing in timings.values())
    rows: list[list[str]] = []
    for label in REBUILD_LABELS:
        timing = timings[label]
        if not timing.steps:
            rows.append([label, "0", "—", "—", "—", "—", "—"])
            continue
        rows.append(
            [
                label,
                count(timing.steps),
                seconds(timing.compile_s),
                seconds(timing.other_s),
                seconds(timing.total_s),
                whole_percent(timing.compile_s, all_compile),
                whole_percent(timing.total_s, all_time),
            ]
        )
    return rows


def nextest_rebuild_lines(steps: Sequence[KnownCrateStep]) -> list[str]:
    timing = RebuildTiming()
    for step in steps:
        if step.kind == "nextest":
            timing.add(step)
    if not timing.steps:
        return []
    measured = timing.compile_s + timing.other_s
    return [
        f"nextest: {seconds(timing.compile_s)} compiling, {seconds(timing.other_s)} running tests "
        + f"({whole_percent(timing.compile_s, measured)} compiling)."
    ]


def rebuild_package(argv: str) -> str:
    matched = PACKAGE_PATTERN.search(argv)
    return matched.group(1) if matched else "(unknown)"


def package_rebuild_rows(steps: Sequence[KnownCrateStep]) -> list[list[str]]:
    packages: dict[str, PackageRebuilds] = {}
    for step in steps:
        if step.kind != "nextest" or rebuild_bin(step.crates_compiled) != EDITED_CRATE_LABEL:
            continue
        packages.setdefault(rebuild_package(step.argv), PackageRebuilds()).add(step)
    ranked = sorted(packages.items(), key=lambda item: (-item[1].compile_s, item[0]))[:5]
    return [
        [
            name,
            count(rebuilds.steps),
            seconds(nearest_rank(rebuilds.compile_samples, 50)) if rebuilds.compile_samples else "—",
            seconds(nearest_rank(rebuilds.compile_samples, 95)) if rebuilds.compile_samples else "—",
            seconds(rebuilds.compile_s),
        ]
        for name, rebuilds in ranked
    ]


def rebuilds_section(connection: sqlite3.Connection, day: str) -> list[str]:
    steps = known_crate_steps(connection, day)
    timings = rebuild_timings(steps)
    packages = package_rebuild_rows(steps)
    section = ["### Rebuilds", ""]
    nextest = nextest_rebuild_lines(steps)
    if nextest:
        section += [*nextest, ""]
    section += [
        *table(["Rebuild", "Steps", "Compile", "Other", "Total", "Share of compile", "Share of time"], rebuild_rows(timings)),
        "Source: steps with a known crate count; compile is cargo's own \"Finished … in\" time, other is the rest of the step.",
        "",
    ]
    if packages:
        section += [
            "Edited-crate rebuilds by package (nextest):",
            "",
            *table(["Package", "Steps", "p50", "p95", "Compile"], packages),
            f"Source: nextest steps that compiled {EDITED_CRATE_MIN_CRATES}–{EDITED_CRATE_MAX_CRATES} crates, by the first package(…) in the test filter; p50 and p95 are of compile time.",
            "",
        ]
    return section


def kind_section(connection: sqlite3.Connection, day: str, kind: str, hosts: int) -> list[str]:
    start, end = day_range(day)
    extra = EXTRA.get(kind, [])
    expressions = [COMMON_SQL]
    for column in extra:
        expressions.append(column.sql)
        if isinstance(column, AverageColumn):
            expressions.append(f"group_concat({column.values_sql})")
    select = ", ".join(expressions)
    rows = fetch(
        connection,
        f"SELECT {GROUP_AS_SCRATCH} AS is_scratch, CASE WHEN {GROUP_AS_SCRATCH} THEN NULL ELSE host END AS caller_host,"
        + f" CASE WHEN {GROUP_AS_SCRATCH} THEN NULL ELSE caller END AS grouped_caller, {select}"
        + " FROM steps WHERE started_at >= ? AND started_at < ? AND step = ?"
        + f" AND {natedev_step_predicate()}"
        + " GROUP BY is_scratch, caller_host, grouped_caller ORDER BY count(*) DESC",
        start, end, kind,
    )
    head = ["Caller", *COMMON_HEAD]
    for column in extra:
        head.append(column.title)
        if isinstance(column, AverageColumn):
            head.append(f"{column.title} p95")
    body: list[list[str]] = []
    for row in rows:
        cells = [SCRATCH_LABEL if row[0] else caller_label(row[2], row[1], hosts), *common(row[3:9])]
        offset = 9
        for column in extra:
            cells.append(column.show(row[offset]))
            offset += 1
            if isinstance(column, AverageColumn):
                cells.append(p95(row[offset]))
                offset += 1
        body.append(cells)
    return [f"### {kind}", "", *table(head, body), ""]


class CounterNotRecorded(Enum):
    VALUE = "counter not recorded in both samples"


class SampleCounterInterval(NamedTuple):
    host: str
    elapsed_s: float
    deltas: dict[str, int | CounterNotRecorded]


def sample_counter_deltas(
    connection: sqlite3.Connection, day: str, columns: tuple[str, ...],
) -> list[SampleCounterInterval]:
    """Count adjacent samples per host, including the baseline before the day."""
    selected = "host, at, boot_id, " + ", ".join(columns)
    samples = fetch(connection, f"SELECT {selected} FROM samples"
                    + " WHERE date(at, 'localtime') = ? ORDER BY host, at", day)
    previous: dict[str, Row] = {}
    intervals: list[SampleCounterInterval] = []
    for row in samples:
        host = str(row[0])
        if host not in previous:
            earlier = fetch(connection, f"SELECT {selected} FROM samples"
                            + " WHERE host = ? AND at < ? ORDER BY at DESC LIMIT 1", host, row[1])
            if earlier:
                previous[host] = earlier[0]
        if host in previous:
            before = previous[host]
            elapsed = (datetime.fromisoformat(str(row[1])) - datetime.fromisoformat(str(before[1]))).total_seconds()
            if 0 <= elapsed <= MAX_SAMPLE_GAP_S:
                deltas: dict[str, int | CounterNotRecorded] = {}
                for column, old, new in zip(columns, before[3:], row[3:], strict=True):
                    if isinstance(old, int) and isinstance(new, int):
                        deltas[column] = max(0, new - old) if row[2] == before[2] else new
                    else:
                        deltas[column] = CounterNotRecorded.VALUE
                intervals.append(SampleCounterInterval(host, elapsed, deltas))
        previous[host] = row
    return intervals


def cpu_disk_pressure_section(connection: sqlite3.Connection, day: str) -> list[str]:
    """CPU and IO stall shares and CPU tick shares for each sampled host."""
    psi_columns = ("cpu_stall_some_us", "cpu_stall_full_us", "io_stall_some_us", "io_stall_full_us")
    tick_columns = ("cpu_idle_ticks", "cpu_iowait_ticks")
    intervals = sample_counter_deltas(connection, day, (*psi_columns, *tick_columns, "cpu_total_ticks"))
    by_host: dict[str, list[SampleCounterInterval]] = {}
    for interval in intervals:
        if any(isinstance(delta, int) for delta in interval.deltas.values()):
            by_host.setdefault(interval.host, []).append(interval)
    section = ["### CPU and disk pressure", "",
               "Source: 60 s machine samples (/proc/pressure/cpu, /proc/pressure/io, /proc/stat).", ""]
    for host, counted in by_host.items():
        shares: dict[str, str] = {}
        for column in (*psi_columns, *tick_columns):
            numerator = denominator = 0.0
            for interval in counted:
                delta = interval.deltas[column]
                total = interval.deltas["cpu_total_ticks"]
                if not isinstance(delta, int):
                    continue
                if column in psi_columns:
                    numerator += delta
                    denominator += interval.elapsed_s * 1_000_000
                elif isinstance(total, int):
                    numerator += delta
                    denominator += total
            shares[column] = f"{100 * numerator / denominator:.1f}%" if denominator > 0 else "—"
        sampled_s = sum(interval.elapsed_s for interval in counted
                        if any(isinstance(interval.deltas[column], int) for column in psi_columns))
        section += [f"{host}: CPU some {shares['cpu_stall_some_us']}, full {shares['cpu_stall_full_us']}; "
                    + f"IO some {shares['io_stall_some_us']}, full {shares['io_stall_full_us']}; "
                    + f"CPU idle {shares['cpu_idle_ticks']}, iowait {shares['cpu_iowait_ticks']} "
                    + f"(of {seconds(sampled_s)} sampled)", ""]
    if not by_host:
        section += ["CPU and IO samples: none.", ""]
    return section


def memory_pressure_section(connection: sqlite3.Connection, day: str, hosts: int) -> list[str]:
    """The largest step stalls and the day's sampled machine memory pressure."""
    waits = fetch(
        connection,
        "SELECT count(*), sum(mem_wait_s), max(mem_wait_s) FROM steps"
        + " WHERE date(started_at, 'localtime') = ? AND mem_wait_s > 0"
        + f" AND {natedev_step_predicate()}",
        day,
    )[0]
    longest = fetch(
        connection,
        f"SELECT {SCRATCH} AS is_scratch, caller, host FROM steps"
        + " WHERE date(started_at, 'localtime') = ? AND mem_wait_s > 0"
        + f" AND {natedev_step_predicate()}"
        + " ORDER BY mem_wait_s DESC LIMIT 1",
        day,
    )
    stalled = fetch(
        connection,
        f"SELECT {SCRATCH} AS is_scratch, s.step, s.caller, s.host, s.mem_stall_some_s,"
        + " (SELECT count(*) FROM steps other WHERE other.host = s.host"
        + " AND other.started_at <= s.started_at AND s.started_at < other.ended_at"
        + f" AND {natedev_step_predicate('other')})"
        + " FROM steps s WHERE date(s.started_at, 'localtime') = ? AND s.mem_stall_some_s > 0"
        + f" AND {natedev_step_predicate('s')}"
        + " ORDER BY s.mem_stall_some_s DESC LIMIT 5",
        day,
    )
    samples = fetch(
        connection,
        "SELECT host, at, boot_id, mem_used_bytes, swap_used_bytes, stall_some_us, stall_full_us,"
        + " builds_anon_bytes, ci_anon_bytes"
        + " FROM samples WHERE date(at, 'localtime') = ? ORDER BY host, at",
        day,
    )
    instrument_predicate = "date({column}, 'localtime') = ? AND " + natedev_step_predicate().removeprefix("steps.")
    instruments = memory.instrument_lines(connection, instrument_predicate, (day,))
    section = ["### Memory pressure", "", "Source: 60 s machine samples and step cgroup stall counters.", ""]
    if longest:
        is_scratch, caller, host = longest[0]
        who = SCRATCH_LABEL if is_scratch else caller_label(caller, host, hosts)
        section += [f"memory waits: {count(waits[0])} steps, total {seconds(waits[1])}, longest {seconds(waits[2])} ({who})", ""]
    else:
        section += ["memory waits: none", ""]
    section += [*instruments, ""]
    if stalled:
        body = [
            [SCRATCH_LABEL if is_scratch else caller_label(caller, host, hosts), str(step), seconds(stall), count(at_once)]
            for is_scratch, step, caller, host, stall, at_once in stalled
        ]
        section += [*table(["Caller", "Kind", "Stall", "At once"], body), ""]
    if not samples:
        section += ["60 s samples: none; machine stall: unavailable.", ""]
        return section

    intervals = sample_counter_deltas(connection, day, ("stall_some_us", "stall_full_us"))
    some_us = sum(interval.deltas["stall_some_us"] for interval in intervals if isinstance(interval.deltas["stall_some_us"], int))
    full_us = sum(interval.deltas["stall_full_us"] for interval in intervals if isinstance(interval.deltas["stall_full_us"], int))

    peak_memory = max(cast(int, row[3]) for row in samples)
    peak_swap = max(cast(int, row[4]) for row in samples)
    peak_builds = max((row[7] for row in samples if isinstance(row[7], int)), default=None)
    peak_ci = max((row[8] for row in samples if isinstance(row[8], int)), default=None)
    slice_peaks = f"peak builds {gib(peak_builds) if peak_builds is not None else 'unavailable'}, peak CI {gib(peak_ci) if peak_ci is not None else 'unavailable'} (process memory)"
    section += [
        f"60 s samples: peak used memory {gib(peak_memory)}, peak swap {gib(peak_swap)}; "
        + f"{slice_peaks}; machine stall: some {seconds(some_us / 1_000_000)}, full {seconds(full_us / 1_000_000)}.",
        "",
    ]
    return section


def outcomes(connection: sqlite3.Connection, day: str, tool: str) -> list[Row]:
    """outcome, calls, wall and saved seconds of one tool's calls, in OUTCOME_ORDER."""
    rows = fetch(
        connection,
        f"SELECT outcome, count(*), sum(wall_s), sum(saved_s) FROM calls WHERE {ON_DAY}"
        + f" AND tool = ? AND {natedev_call_predicate()} GROUP BY outcome",
        day,
        tool,
    )
    rows.sort(key=lambda row: OUTCOME_ORDER.index(str(row[0])) if row[0] in OUTCOME_ORDER else len(OUTCOME_ORDER))
    return rows


def calls_section(connection: sqlite3.Connection, day: str) -> tuple[list[str], str]:
    rows = outcomes(connection, day, "verify.sh")
    if not rows:
        return [], "Agent calls: none."
    body = [[str(row[0]), count(row[1]), some_seconds(row[2]), some_seconds(row[3])] for row in rows]
    total = sum(cast(int, row[1]) for row in rows)
    saved = sum(cast(int, row[3] or 0) for row in rows)
    parts = ", ".join(f"{count(row[1])} {row[0]}" for row in rows)
    section = ["### Agent calls (verify.sh)", "", *table(["Outcome", "Calls", "Wall", "Saved"], body), ""]
    return section, f"Agent calls: {total} ({parts}), {seconds(saved)} saved by pass records."


def test_builds_section(connection: sqlite3.Connection, day: str) -> list[str]:
    rows = fetch(
        connection,
        f"SELECT command, build_s FROM calls WHERE {ON_DAY} AND tool = 'verify.sh'"
        + f" AND verb = 'test' AND build_s IS NOT NULL AND {natedev_call_predicate()}",
        day,
    )
    if not rows:
        return []

    builds: dict[str, list[int]] = {"whole-package": [], "--filter": []}
    for command, build in rows:
        scope = "--filter" if "--filter" in str(command).split() else "whole-package"
        builds[scope].append(cast(int, build))

    body = [
        ["Baseline 2026-10-01/02", "whole-package", seconds(5.20 * 3600), seconds(137)],
        ["Baseline 2026-10-04 from 00:07 EDT", "whole-package", seconds(5.0 * 3600), seconds(103)],
        ["Baseline 2026-10-04 from 00:07 EDT", "--filter", seconds(14.4 * 3600), seconds(71)],
    ]
    for scope, values in builds.items():
        if values:
            body.append([day, scope, seconds(sum(values)), seconds(nearest_rank([float(value) for value in values], 75))])

    return [
        "### Test builds (temporary)",
        "",
        "Source: build log `verify.sh test` calls with measured `build_s`; p75 is the nearest rank.",
        "",
        *table(["Period", "Scope", "Build/day", "p75 build/call"], body),
        "",
        "Temporary: kept until the user calls the result settled.",
        "",
    ]


class ProfileChange(NamedTuple):
    at: str
    profile: str


def profile_changes(connection: sqlite3.Connection) -> list[ProfileChange]:
    """Keep the first observation of each distinct natedev profile run."""
    changes: list[ProfileChange] = []
    for at, profile, since in fetch(
        connection,
        "SELECT at, power_profile, power_profile_since FROM samples"
        + " WHERE host = 'natedev' AND power_profile IS NOT NULL ORDER BY julianday(at), rowid",
    ):
        name = cast(str, profile)
        if not changes or changes[-1].profile != name:
            changes.append(ProfileChange(cast(str, since if since is not None else at), name))
    return changes


def profile_step_durations(
    connection: sqlite3.Connection, start: datetime, end: datetime,
) -> dict[tuple[str, str, str], list[float]]:
    """Group passing steps by the kind, caller and rebuild class we compare."""
    groups: dict[tuple[str, str, str], list[float]] = {}
    for kind, caller, crates, duration in fetch(
        connection,
        "SELECT step, caller, crates_compiled, duration_s FROM steps"
        + " WHERE julianday(started_at) >= julianday(?) AND julianday(started_at) < julianday(?)"
        + f" AND {natedev_step_predicate()} AND status = 0 AND step <> 'sweep'"
        + " AND duration_s IS NOT NULL",
        start.isoformat(), end.isoformat(),
    ):
        rebuild = rebuild_bin(cast(int, crates)) if crates is not None else "—"
        key = (str(kind), str(caller), rebuild)
        groups.setdefault(key, []).append(cast(float, duration))
    return groups


def power_profile_section(connection: sqlite3.Connection, day: str) -> list[str]:
    """Compare like steps on each side of the recent observed profile changes."""
    zone = user_zone()
    end = datetime.combine(date.fromisoformat(day) + timedelta(days=1), datetime.min.time(), zone).astimezone(UTC)
    changes = [change for change in profile_changes(connection) if call_time(change.at) < end]
    if not changes:
        return []

    latest = changes[-1]
    lines = ["### Power profile", "", f"Profile now: {latest.profile}, since {local_time(latest.at)}.", ""]
    window_start = end - timedelta(days=7)
    for position in range(len(changes) - 1, -1, -1):
        change = changes[position]
        at = call_time(change.at)
        if at < window_start:
            continue
        before_start = at - timedelta(days=7)
        before_profile = "profile not recorded"
        if position:
            previous = changes[position - 1]
            before_start = max(before_start, call_time(previous.at))
            before_profile = previous.profile
        after_end = min(end, call_time(changes[position + 1].at)) if position + 1 < len(changes) else end
        before = profile_step_durations(connection, before_start, at)
        after = profile_step_durations(connection, at, after_end)
        rows: list[list[str]] = []
        for key in sorted(before.keys() & after.keys(), key=lambda key: (-len(after[key]), key)):
            before_runs, after_runs = before[key], after[key]
            before_p50 = nearest_rank(before_runs, 50)
            after_p50 = nearest_rank(after_runs, 50)
            difference = "—"
            if len(before_runs) >= MIN_PROFILE_RUNS and len(after_runs) >= MIN_PROFILE_RUNS and before_p50 > 0:
                difference = f"{(after_p50 - before_p50) / before_p50:+.0%}".replace("-", "−")
            kind, caller, rebuild = key
            rows.append([
                kind, caller_label(caller, "natedev", 1), rebuild,
                str(len(before_runs)), seconds(before_p50), str(len(after_runs)), seconds(after_p50), difference,
            ])
        lines += [
            f"Before {local_time(change.at)} ({before_profile}) vs after ({change.profile}):",
            "",
        ]
        if not rows:
            lines += ["No step ran with the same kind, caller and rebuild class on both sides.", ""]
            continue
        lines += [
            "| Kind | Caller | Rebuild | Before runs | Before p50 | After runs | After p50 | Change |",
            "|---|---|---|--:|--:|--:|--:|--:|",
            *("| " + " | ".join(row) + " |" for row in rows),
            "Source: natedev steps that passed, the 7 days before the change against the time since, up to the next change;"
            + f" p50 is the nearest rank; Change is shown only with at least {MIN_PROFILE_RUNS} runs on each side."
            + " Load and time of day are not matched.",
            "",
        ]
    return lines


def day_range(day: str) -> tuple[str, str]:
    """UTC bounds for a local day, including days that change the clocks."""
    first = datetime.combine(date.fromisoformat(day), datetime.min.time())
    last = first + timedelta(days=1)
    return (
        first.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
        last.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
    )


def reported_mac_calls(connection: sqlite3.Connection, day: str) -> list[ReportedTestCall]:
    start, end = day_range(day)
    pairing_end = (call_time(end).replace(tzinfo=UTC) + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = fetch(
        connection,
        "SELECT id, host, started_at, package, worktree, command, status, mac, mac_reason,"
        + " local_reason, mac_s, outcome, wall_s, token_wait_s, target,"
        + " coalesce(copy, ''), copy_s, copy_count, copy_natedev_s FROM calls"
        + " WHERE started_at >= ? AND started_at < ? AND tool = 'verify.sh' AND verb = 'test'"
        + " AND (started_at < ? OR (mac = 'declined' AND mac_reason = 'local_flag'))"
        + " ORDER BY started_at, id",
        start, pairing_end, end,
    )
    # Steps can finish on the next day; select by their call's start, once.
    steps = fetch(
        connection,
        "SELECT s.call_id, s.host FROM calls c JOIN steps s ON s.call_id = c.id"
        + " WHERE c.started_at >= ? AND c.started_at < ?"
        + " AND c.tool = 'verify.sh' AND c.verb = 'test' AND s.step = 'nextest'"
        + f" AND {natedev_step_predicate('s')}",
        start, pairing_end,
    )
    local_calls = {(str(call_id), str(host)) for call_id, host in steps}
    calls: list[ReportedTestCall] = []
    for (call_id, host, started_at, package, worktree, command, status, mac, mac_reason,
         local_reason, mac_s, outcome, wall_s, token_wait_s, target,
         copy, copy_s, copy_count, copy_natedev_s) in rows:
        result = offload_record(mac, mac_reason, local_reason, mac_s, outcome)
        if isinstance(result, CacheReuse):
            continue
        # A copy win stopped the natedev step, so the call's time is not a natedev run's.
        ran_on_natedev = (str(call_id), str(host)) in local_calls and copy != COPY_WON
        if isinstance(result, NoOffloadRecord) and not ran_on_natedev:
            continue
        calls.append(ReportedTestCall(
            str(call_id), call_time(started_at), str(package or '(unknown)'),
            str(worktree or ''), str(command or ''), status == 0,
            ran_on_natedev,
            result,
            str(started_at) < end,
            offload_seconds(wall_s), offload_seconds(token_wait_s),
            target is not None, is_mac_candidate(result),
            str(copy), offload_seconds(copy_s), copy_count if isinstance(copy_count, int) else 0,
            offload_seconds(copy_natedev_s),
        ))
    return calls


def package_natedev_tests(calls: Sequence[ReportedTestCall]) -> dict[str, PackageNatedevTests]:
    packages: dict[str, PackageNatedevTests] = {}
    for call in calls:
        if not call.on_report_day or not call.ran_on_natedev:
            continue
        durations = packages.setdefault(call.package, PackageNatedevTests())
        durations.ran += 1
        if isinstance(call.wall_seconds, float):
            durations.wall_seconds.append(call.wall_seconds)
        if isinstance(call.token_wait_seconds, float):
            durations.token_wait_seconds.append(call.token_wait_seconds)
    return packages


def matched_local_failures(
    calls: Sequence[ReportedTestCall],
) -> dict[str, LocalRunRequested]:
    matched: dict[str, LocalRunRequested] = {}
    failures = [
        call
        for call in calls
        if isinstance(call.offload, MacRunCompleted)
        and call.offload.outcome == "failed"
    ]
    for local_call in calls:
        if (
            not isinstance(local_call.offload, LocalRunRequested)
            or not local_call.succeeded
            or not local_call.ran_on_natedev
        ):
            continue
        candidates = [
            failure
            for failure in failures
            if failure.call_id not in matched
            and failure.started_at < local_call.started_at
            and local_call.started_at - failure.started_at <= timedelta(minutes=30)
            and failure.worktree == local_call.worktree
            and failure.package == local_call.package
            and failure.command == local_call.command
        ]
        if candidates:
            matched[max(candidates, key=lambda call: call.started_at).call_id] = (
                local_call.offload
            )
    return matched


def median_duration(values: Sequence[float]) -> float:
    return nearest_rank(values, 50) if values else 0.0


def mac_section(connection: sqlite3.Connection, day: str) -> list[str]:
    """Steps recorded on Mac hosts, including calls started there directly."""
    start, end = day_range(day)
    rows = fetch(
        connection,
        f"SELECT caller, step, count(*), sum({index.COUNTED_FAILURE}),"
        + " avg(duration_s), min(duration_s), max(duration_s), group_concat(duration_s),"
        + " avg(finished_s), sum(tests_run), sum(tests_failed), sum(duration_s)"
        + " FROM steps WHERE started_at >= ? AND started_at < ? AND host <> 'natedev'"
        + " GROUP BY caller, step ORDER BY count(*) DESC, caller, step",
        start, end,
    )
    if not rows:
        return ["### Mac", "", "No steps recorded on the Mac.", ""]
    body = [
        [
            "verify.sh offload" if row[0] == MAC_STEP_CALLER else caller_label(row[0], "", 1),
            str(row[1]),
            *common(row[2:8]),
            seconds(row[8]) or "—",
            count(row[9]) or "—",
            count(row[10]) or "—",
        ]
        for row in rows
    ]
    mac_table = table(["Caller", "Kind", *COMMON_HEAD, "Build", "Tests", "Tests failed"], body)
    mac_table[1] = "|---|---|--:|--:|--:|--:|--:|--:|--:|--:|"
    runs = sum(cast(int, row[2]) for row in rows)
    failed = sum(cast(int, row[3] or 0) for row in rows)
    duration = sum(cast(float, row[11] or 0) for row in rows)
    return [
        "### Mac", "", *mac_table,
        f"Mac total: {runs} steps, {seconds(duration)}; {failed} failed.",
        "Source: steps recorded on the Mac; failed follows the build log's failure rule, so a Mac nextest that matched no tests is not a failure.",
        "",
    ]


def comparable_natedev_steps(connection: sqlite3.Connection, day: str) -> list[ComparableNatedevStep]:
    """The day's natedev nextest steps of test calls that ran to their end; a step a copy win stopped is left out."""
    start, end = day_range(day)
    rows = fetch(
        connection,
        "SELECT c.command, c.package, s.duration_s FROM steps s JOIN calls c ON c.id = s.call_id"
        + " WHERE s.started_at >= ? AND s.started_at < ? AND s.step = 'nextest' AND s.status < 128"
        + f" AND s.duration_s IS NOT NULL AND {natedev_step_predicate('s')}"
        + f" AND c.tool = 'verify.sh' AND c.verb = 'test' AND coalesce(c.copy, '') <> '{COPY_WON}'",
        start, end,
    )
    return [
        ComparableNatedevStep(str(command or ''), str(package or '(unknown)'), float(cast(float, duration)))
        for command, package, duration in rows
    ]


def copy_saving_estimate(call: ReportedTestCall, steps: Sequence[ComparableNatedevStep]) -> float:
    """The time natedev would still have needed when the copy won: the median remaining time of comparable steps that lasted longer."""
    elapsed = call.copy_natedev_seconds
    if isinstance(elapsed, DurationNotRecorded):
        return 0.0
    same_command = [step.duration_s for step in steps if step.command == call.command]
    durations = same_command if len(same_command) >= MIN_SAME_COMMAND_STEPS else [
        step.duration_s for step in steps if step.package == call.package
    ]
    remaining = [duration - elapsed for duration in durations if duration > elapsed]
    return nearest_rank(remaining, 50) if remaining else 0.0


def daily_mac_copies(connection: sqlite3.Connection, day: str, calls: Sequence[ReportedTestCall]) -> DailyMacCopies:
    copied = [call for call in calls if call.on_report_day and call.copy]
    steps = comparable_natedev_steps(connection, day) if any(call.copy == COPY_WON for call in copied) else []
    packages: dict[str, PackageMacCopies] = {}
    for call in copied:
        package = packages.setdefault(call.package, PackageMacCopies())
        package.copies += max(1, call.copy_count)
        if call.copy == COPY_WON:
            package.won += 1
            package.saved_seconds += copy_saving_estimate(call, steps)
            continue
        if call.copy == COPY_NATEDEV_WON:
            package.natedev_won += 1
        elif call.copy == COPY_GAVE_WAY:
            package.gave_way += 1
        else:
            package.other += 1
        package.not_won_calls += 1
        if isinstance(call.copy_seconds, float):
            package.not_won_mac_seconds += call.copy_seconds
    return DailyMacCopies(packages)


def mac_copies_lines(copies: DailyMacCopies) -> list[str]:
    if not copies.started:
        return ["Mac copies: none started.", ""]
    body = [
        [
            name,
            count(package.copies),
            count(package.won),
            count(package.natedev_won),
            count(package.gave_way),
            count(package.other),
            seconds(package.saved_seconds) if package.won else "—",
            seconds(package.not_won_mac_seconds) if package.not_won_calls else "—",
        ]
        for name, package in sorted(copies.packages.items())
    ]
    head = ["Package", "Copies", "Mac won", "natedev won", "Gave way", "Other", "Est. saved",
            "Mac time on copies that did not win"]
    return [
        "### Mac copies", "", *table(head, body),
        "Source: `verify.sh test` calls whose natedev run was still compiling when a copy of it started on the Mac;"
        + " the outcome columns count calls by their last copy's outcome, and Other is a full-run Mac pass"
        + " (natedev still gates), a declined copy or a lost one.",
        "Est. saved is an estimate: for each Mac win, with t the time the stopped natedev step had run, the median"
        + " (nearest rank) of duration minus t over that day's natedev nextest steps that ran to their end and lasted"
        + f" longer than t, of the same command when there are at least {MIN_SAME_COMMAND_STEPS} such steps, else of"
        + " the same package; 0 when none lasted longer or t is unknown.",
        "",
    ]


def mac_copies_lead_lines(copies: DailyMacCopies) -> list[str]:
    if not copies.started:
        return []
    return [f"Mac copies: {copies.won} of {copies.started} finished first, about {seconds(copies.saved_seconds)} saved (estimated)."]


def mac_tests_section(connection: sqlite3.Connection, day: str) -> list[str]:
    return tests_by_target_lines(reported_mac_calls(connection, day))


def tests_by_target_lines(calls: Sequence[ReportedTestCall]) -> list[str]:
    if not calls:
        return []

    local_tests = package_natedev_tests(calls)
    packages: dict[str, PackageMacTests] = {}
    no_test_calls: list[ReportedTestCall] = []
    target_not_recorded = 0
    for call in calls:
        if not call.on_report_day:
            continue
        result = call.offload
        shown_decline = isinstance(result, OffloadDeclined) and result.reason not in HIDDEN_DECLINE_REASONS
        if not (call.ran_on_natedev or shown_decline or isinstance(result, (
            MacRunCompleted, MacRunLost, MacRunMatchedNoTest, LocalRunRequested, RunnerResultUnavailable,
        ))):
            continue
        report = packages.setdefault(call.package, PackageMacTests())
        if not call.target_recorded:
            target_not_recorded += 1
        if isinstance(result, MacRunCompleted):
            report.tried += 1
            report.mac_seconds.append(result.seconds)
            if result.outcome == "failed":
                report.failed += 1
            else:
                report.passed += 1
        elif isinstance(result, MacRunLost):
            report.tried += 1
        elif isinstance(result, MacRunMatchedNoTest):
            report.tried += 1
            no_test_calls.append(call)
        elif isinstance(result, OffloadDeclined) and shown_decline:
            report.decline_counts[result.reason] = report.decline_counts.get(result.reason, 0) + 1
        elif isinstance(result, LocalRunRequested):
            report.local_requested += 1
        elif isinstance(result, RunnerResultUnavailable):
            report.runner_unavailable += 1
        elif isinstance(result, NoOffloadRecord) and call.ran_on_natedev:
            report.no_mac_entry += 1

    if not packages:
        return []

    body: list[list[str]] = []
    for package, package_report in sorted(packages.items()):
        local = local_tests.get(package, PackageNatedevTests())
        decline_order = sorted(
            package_report.decline_counts,
            key=lambda reason: (
                -package_report.decline_counts[reason],
                DECLINE_REASONS.index(reason),
            ),
        )
        stayed_local = ", ".join(
            f"{DECLINE_LABELS[reason]} {package_report.decline_counts[reason]}"
            for reason in decline_order
        )
        extra = [
            f"{label} {amount}" for label, amount in (
                ("--local", package_report.local_requested),
                ("no Mac entry", package_report.no_mac_entry),
                ("runner", package_report.runner_unavailable),
            ) if amount
        ]
        stayed_local = ", ".join(filter(None, [stayed_local, *extra]))
        body.append(
            [
                package,
                count(package_report.tried),
                count(package_report.passed),
                count(package_report.failed),
                seconds(median_duration(package_report.mac_seconds))
                if package_report.mac_seconds
                else "",
                count(local.ran),
                seconds(median_duration(local.wall_seconds))
                if local.wall_seconds
                else "",
                stayed_local,
            ]
        )

    matched_failures = matched_local_failures(calls)
    avoided = [
        call
        for call in calls
        if isinstance(call.offload, MacRunCompleted)
        and call.copy != COPY_WON
        and (
            call.offload.outcome in ("passed_filter", "passed_full")
            or call.offload.outcome == "failed"
            and call.call_id not in matched_failures
        )
    ]
    avoided_by_package: dict[str, int] = {}
    for call in avoided:
        avoided_by_package[call.package] = avoided_by_package.get(call.package, 0) + 1
    avoided_wall_s = 0.0
    avoided_wait_s = 0.0
    for package, avoided_count in avoided_by_package.items():
        local = local_tests.get(package, PackageNatedevTests())
        avoided_wall_s += avoided_count * median_duration(local.wall_seconds)
        avoided_wait_s += avoided_count * median_duration(local.token_wait_seconds)

    section = [
        "### Tests by target",
        "",
        *table(
            [
                "Package",
                "Mac ran",
                "Mac passed",
                "Mac failed",
                "Mac p50",
                "natedev ran",
                "natedev p50",
                "Did not reach the Mac",
            ],
            body,
        ),
        *([f"Target not recorded for {target_not_recorded} {'call' if target_not_recorded == 1 else 'calls'} counted above."]
          if target_not_recorded else []),
        "Source: build log `verify.sh test` calls and their nextest steps; p50 is the nearest rank.",
        "",
        f"natedev runs avoided: {len(avoided)} — about {avoided_wall_s / 60:.1f} min of build and test time "
        + f"and {avoided_wait_s / 60:.1f} min of waiting, estimated from that day's natedev runs of the same packages",
    ]
    if matched_failures:
        matched_packages = sorted(
            {call.package for call in calls if call.call_id in matched_failures}
        )
        reasons = list(
            dict.fromkeys(
                requested.reason
                for requested in matched_failures.values()
                if isinstance(requested.reason, str)
            )
        )
        local_flag = "--local"
        if reasons:
            local_flag += " " + ", ".join(
                json.dumps(reason, ensure_ascii=False) for reason in reasons
            )
        section.append(
            f"Failed on the Mac, then passed on natedev with {local_flag}: "
            + f"{len(matched_failures)} ({', '.join(matched_packages)})"
        )
    if no_test_calls:
        no_test_packages = sorted({call.package for call in no_test_calls})
        section.append(
            f"No test matched on the Mac: {len(no_test_calls)} "
            + f"({', '.join(no_test_packages)})"
        )
    return [*section, ""]


def port_lint_section(connection: sqlite3.Connection, day: str) -> tuple[list[str], list[str]]:
    """cargo-port's lint calls that ran nothing; the ones that ran are its steps above. Nothing at all when none."""
    rows = outcomes(connection, day, "port-lint")
    if not rows:
        return [], []
    body = [[str(row[0]), count(row[1]), some_seconds(row[3])] for row in rows]
    total = sum(cast(int, row[1]) for row in rows)
    saved = sum(cast(int, row[3] or 0) for row in rows)
    parts = ", ".join(f"{count(row[1])} {row[0]}" for row in rows)
    section = ["### cargo-port calls (port-lint)", "", *table(["Outcome", "Calls", "Saved"], body), ""]
    return section, [f"cargo-port calls: {total} ({parts}), {seconds(saved)} saved by recorded steps."]


def ci_freshness(day: str) -> str:
    states = [ci.read_poll_state(repo) for repo in ci.REPOS]
    polled = [state for state in states if isinstance(state, ci.CompletedPoll | ci.CappedPoll)]
    if not polled:
        return "; CI never polled"
    newest = max(polled, key=lambda state: datetime.fromisoformat(state.polled_at.replace("Z", "+00:00")))
    now = datetime.now(user_zone())
    day_end = datetime.combine(date.fromisoformat(day) + timedelta(days=1), datetime.min.time(), user_zone())
    horizon = min(now, day_end)
    at = datetime.fromisoformat(newest.polled_at.replace("Z", "+00:00")).astimezone(user_zone())
    if day_end < now and at >= day_end:
        return ""
    stale = at < horizon - timedelta(seconds=CI_STALE_AFTER_S)
    if not stale and isinstance(newest, ci.CompletedPoll):
        return ""
    clause = f"; CI recorded through {sync.sync_time(newest.polled_at, now)}"
    if stale:
        clause += ", no poll since"
    if isinstance(newest, ci.CappedPoll):
        clause += ", the last poll was capped"
    return clause


def ci_section(connection: sqlite3.Connection, day: str) -> tuple[list[str], str]:
    freshness = ci_freshness(day)
    rows = fetch(
        connection,
        "SELECT workflow, count(*), coalesce(sum(conclusion = 'failure'), 0), coalesce(sum(conclusion = 'cancelled'), 0),"
        + " avg(duration_s), min(duration_s), max(duration_s), group_concat(duration_s), sum(duration_s)"
        + f" FROM ci_runs WHERE {ON_DAY} GROUP BY workflow ORDER BY count(*) DESC",
        day,
    )
    if not rows:
        return [], f"CI: no runs{freshness}."
    queue, left_out, queue_values = fetch(
        connection,
        "SELECT avg(queued_s), coalesce(sum(run_state != 'skipped' AND queued_s IS NULL), 0), group_concat(queued_s)"
        + " FROM ci_jobs AS j JOIN ci_runs AS r ON j.run_id = r.run_id AND j.attempt = r.attempt"
        + " WHERE date(r.started_at, 'localtime') = ?",
        day,
    )[0]
    body: list[list[str]] = []
    for row in rows:
        shared = common((row[1], row[2], row[4], row[5], row[6], row[7]))
        body.append([str(row[0]), *shared[:2], count(row[3]), *shared[2:]])
    runs = sum(cast(int, row[1]) for row in rows)
    failed = sum(cast(int, row[2] or 0) for row in rows)
    cancelled = sum(cast(int, row[3] or 0) for row in rows)
    total = sum(cast(float, row[8] or 0.0) for row in rows)
    section = ["### CI", "", *table(["Workflow", "Runs", "Failed", "Cancelled", "Avg", "p95", "Range"], body), ""]
    omitted = cast(int, left_out)
    if queue is None:
        queue_clause = "no job has a known queue time"
        if omitted:
            queue_clause += f" ({omitted} left out)"
    else:
        queue_clause = f"jobs queued {seconds(queue)} on average, p95 {p95(queue_values)}"
        if omitted:
            queue_clause += f", {omitted} without a known queue time left out"
    cancelled_clause = f", {cancelled} cancelled" if cancelled else ""
    return section, f"CI: {runs} runs, {failed} failed{cancelled_clause}, {seconds(total)} in all; {queue_clause}{freshness}."


def call_trees(connection: sqlite3.Connection, end_day: str) -> dict[str, KnownCallTrees]:
    """First and last known step trees in each verify call; unkeyed calls are absent."""
    rows = fetch(
        connection,
        " ".join(
            (
                "SELECT s.call_id, s.tree_key, s.started_at, s.id FROM steps s JOIN calls c ON c.id = s.call_id",
                "WHERE c.tool = 'verify.sh' AND s.tree_key IS NOT NULL",
                f"AND {natedev_step_predicate('s')} AND {natedev_call_predicate('c')}",
                "AND c.started_at < ?",
            )
        ),
        day_range(end_day)[1],
    )
    histories: dict[str, CallTreeHistory] = {}
    for call_id, tree_key, started_at, step_id in rows:
        call = cast(str, call_id)
        key = cast(str, tree_key)
        step_order = (str(started_at), str(step_id))
        previous = histories.get(call)
        if previous is None:
            histories[call] = CallTreeHistory(step_order, step_order, key, key)
        else:
            if step_order < previous.first_step:
                previous.first_step, previous.first_tree = step_order, key
            if step_order > previous.last_step:
                previous.last_step, previous.last_tree = step_order, key
    return {call: KnownCallTrees(history.first_tree, history.last_tree) for call, history in histories.items()}


def failure_bin(edits: int) -> int:
    if edits == 0:
        return 0
    if edits == 1:
        return 1
    if edits <= 3:
        return 2
    return 3 if edits <= 7 else 4


def tests_per_edit_data(connection: sqlite3.Connection, end_day: str) -> TestsPerEditWindow:
    """Track each worker through the query's last day, including calls before the displayed window."""
    rows = fetch(
        connection,
        " ".join(
            (
                "SELECT id, started_at, ended_at, date(started_at, 'localtime'),",
                "coalesce(nullif(unit_session, ''), nullif(session, '')), verb, status, outcome",
                "FROM calls WHERE tool = 'verify.sh'",
                f"AND {natedev_call_predicate()}",
                "AND started_at < ?",
                "ORDER BY started_at, id",
            )
        ),
        day_range(end_day)[1],
    )
    trees = call_trees(connection, end_day)
    first_day = (date.fromisoformat(end_day) - timedelta(days=6)).isoformat()
    daily: dict[str, DailyTestsPerEdit] = {}
    bins = [FailureRecoveryBin() for _ in EDIT_BINS]
    last_tree: dict[str, str] = {}
    edits_since_green: dict[str, int] = {}
    waiting_for_green: dict[str, list[tuple[int, datetime]]] = {}

    for call_id, started_at, ended_at, call_day, worker_value, verb, status, outcome in rows:
        if worker_value is None:
            continue
        worker = cast(str, worker_value)
        day = cast(str, call_day)
        in_window = day >= first_day
        activity = daily.setdefault(day, DailyTestsPerEdit()) if in_window else None
        span = trees.get(cast(str, call_id))
        if span is not None:
            previous = last_tree.get(worker)
            if previous is not None and previous != span.first:
                edits_since_green[worker] = edits_since_green.get(worker, 0) + 1
                if activity is not None:
                    activity.edits += 1
            last_tree[worker] = span.last

        if verb != "test":
            continue
        if activity is not None:
            activity.tests += 1
        finished_at = datetime.fromisoformat(cast(str, ended_at or started_at))
        if status == 0 and outcome in ("ran", "reused"):
            for bin_index, failed_at in waiting_for_green.pop(worker, []):
                recovery = bins[bin_index]
                recovery.recovered += 1
                minutes = max(0.0, (finished_at - failed_at).total_seconds() / 60)
                recovery.minutes_to_green += minutes
                recovery.recovery_minutes.append(minutes)
            edits_since_green[worker] = 0
        elif outcome not in ("interrupted", "superseded", "cancelled") and (outcome == "failed" or status is not None and status != 0) and in_window:
            edits = edits_since_green.get(worker, 0)
            bin_index = failure_bin(edits)
            bins[bin_index].failures += 1
            waiting_for_green.setdefault(worker, []).append((bin_index, finished_at))
    return TestsPerEditWindow(first_day, daily, bins)


def target_status(activity: DailyTestsPerEdit) -> str:
    if not activity.edits:
        return "—"
    ratio = f"{activity.tests / activity.edits:.2f}"
    target = f"{TESTS_PER_EDIT_TARGET:.2f}"
    if ratio == target:
        return "on target"
    return "above" if float(ratio) > TESTS_PER_EDIT_TARGET else "below"


def tests_per_edit_section(connection: sqlite3.Connection, day: str) -> list[str]:
    window = tests_per_edit_data(connection, day)
    report_day = date.fromisoformat(day)
    activity_rows: list[list[str]] = []
    for offset in range(7):
        activity_day = (report_day - timedelta(days=offset)).isoformat()
        activity = window.daily.get(activity_day, DailyTestsPerEdit())
        activity_rows.append([
            activity_day,
            count(activity.tests),
            count(activity.edits),
            f"{activity.tests / activity.edits:.2f}" if activity.edits else "—",
            target_status(activity),
        ])
    recovery_rows = [
        [label, count(recovery.failures), seconds(recovery.minutes_to_green * 60 / recovery.recovered) if recovery.recovered else "",
         seconds(nearest_rank(recovery.recovery_minutes, 95) * 60) if recovery.recovery_minutes else ""]
        for label, recovery in zip(EDIT_BINS, window.recovery_bins, strict=True)
    ]
    unresolved = sum(recovery.failures - recovery.recovered for recovery in window.recovery_bins)
    notes = [f"Source: verify.sh failed test calls and step tree keys, {window.first_day}–{day}"]
    if unresolved:
        notes.append(f"{unresolved} without a later green")
    return [
        "### Tests per edit",
        "",
        *table(["Day", "Tests", "Edits", "Tests/edit", "Target"], activity_rows),
        f"Source: verify.sh test calls and step tree keys, {window.first_day}–{day}; target {TESTS_PER_EDIT_TARGET:g} tests/edit; — means no observed edit.",
        "",
        *table(["Edits since green", "Failures", "Avg to next green", "p95 to next green"], recovery_rows),
        "; ".join(notes) + ".",
        "",
    ]


SUMMARIES = [("successes", "status = 0", False), ("failures", "status <> 0", False), ("all", "1", True)]


def summary(connection: sqlite3.Connection, day: str, found: list[str], which: str, with_failed: bool) -> list[str]:
    """One row per kind, every caller together, then the total; kinds with no runs in the set are left out."""
    select = "count(*), sum(status <> 0), sum(duration_s), avg(duration_s), group_concat(duration_s), max(peak_mem_bytes)"
    start, end = day_range(day)
    where = f"started_at >= ? AND started_at < ? AND {which} AND {natedev_step_predicate()}"
    rows = {
        cast(str, row[0]): row[1:]
        for row in fetch(connection, f"SELECT step, {select} FROM steps WHERE {where} GROUP BY step", start, end)
    }
    if not rows:
        return ["None."]
    durations = [value for row in rows.values() for value in percentile_values(row[4])]
    failed = [row[1] for row in rows.values() if isinstance(row[1], int)]
    peaks = [row[5] for row in rows.values() if isinstance(row[5], int | float)]
    total: Row = (
        sum(cast(int, row[0]) for row in rows.values()), sum(failed) if failed else None,
        sum(durations) if durations else None,
        sum(durations) / len(durations) if durations else None,
        ",".join(str(value) for value in durations), max(peaks) if peaks else None,
    )
    if not total[0]:
        return ["None."]

    def line(name: str, row: Row) -> list[str]:
        failed = [count(row[1])] if with_failed else []
        return [name, count(row[0]), *failed, seconds(row[2]), seconds(row[3]), p95(row[4]), gib(row[5])]

    body = [line(kind, rows[kind]) for kind in found if kind in rows] + [line("**All steps**", total)]
    head = ["Kind", "Runs", *(["Failed"] if with_failed else []), "Total", "Avg", "p95", "Peak memory"]
    return table(head, body)


def disk_section() -> list[str]:
    snapshot = disk.read_snapshot()
    if snapshot is None:
        return []
    rows = [[row["label"], gib(row["bytes"])] for row in snapshot["rows"]]
    rows.append(["other", gib(max(0, snapshot["used"] - sum(row["bytes"] for row in snapshot["rows"])))])
    floor = snapshot["floor"]
    free_label = f"free (floor {gib(floor)})" if floor is not None else "free"
    rows.append([free_label, gib(snapshot["free"])])
    measured = sync.sync_time(snapshot["measured_at"], datetime.now())
    return [
        f"### Disk: {snapshot['host']}",
        "",
        *table(["Where", "Size"], rows),
        "",
        f"Measured by the buildlog disk job at {measured}: allocated blocks, each hard-linked file once.",
        "",
    ]


def mac_note() -> str:
    paused = sync.read_pause()
    prefix = ""
    if isinstance(paused, sync.SyncPaused):
        since = sync.sync_time(paused.since, datetime.now()) if paused.since != "unknown" else paused.since
        prefix = f"Mac: sync paused since {since} ({paused.why}). "
    status = sync.read_status()
    if isinstance(status, sync.NeverSynced):
        return prefix + "Mac: not synced yet."
    last = status.last_good
    when = sync.sync_time(last.at, datetime.now()) if isinstance(last, sync.LastSynced) else "never"
    return prefix + (f"Mac rows as of the {when} sync." if status.ok else f"Mac: the last sync failed; last good sync {when}.")


def lead_block(
    connection: sqlite3.Connection, day: str, calls: Sequence[ReportedTestCall], waits: DailyWaits, copies: DailyMacCopies,
) -> list[str]:
    """The day's routing, waits, failures and saved work, before the tables."""
    start, end = day_range(day)
    share = usable_share(connection, start, end)
    lines = holds.now_lines(holds.read_current_holds())
    if share.candidates:
        lines.append(f"Mac when usable: {share.finished} of {share.candidates} candidates finished there ({share.finished / share.candidates:.0%}).")
    measured_rows = [(row.measurements.longest.seconds, row.name) for row in waits.rows
                     if isinstance(row.measurements.longest, LongestWait)]
    if measured_rows:
        longest, name = max(measured_rows, key=lambda entry: entry[0])
        lines.append(f"Longest wait: {seconds(longest)} for {name}.")
    failures, flaky = fetch(
        connection,
        f"SELECT sum({index.COUNTED_FAILURE}), sum(tests_flaky) FROM steps WHERE started_at >= ? AND started_at < ?"
        + f" AND {natedev_step_predicate()}",
        start, end,
    )[0]
    if failures or flaky:
        lines.append(f"Failed steps: {count(failures) or '0'}; flaky tests: {count(flaky) or '0'}.")
    saved = fetch(
        connection,
        "SELECT sum(saved_s) FROM calls WHERE started_at >= ? AND started_at < ?"
        + f" AND tool = 'verify.sh' AND {natedev_call_predicate()}",
        start, end,
    )[0][0]
    if isinstance(saved, int | float) and saved > 0:
        lines.append(f"Pass records saved: {seconds(saved)}.")
    candidates = [call for call in calls if call.on_report_day and call.candidate]
    if candidates:
        latest = candidates[-1]
        if isinstance(latest.offload, OffloadDeclined) and latest.offload.reason in ('backoff', 'unreachable'):
            instant = latest.started_at.astimezone(user_zone())
            shown = instant.strftime('%H:%M' if day == datetime.now(user_zone()).date().isoformat() else '%Y-%m-%d %H:%M %Z')
            lines.append(f"Mac unreachable as of {shown}")
    lines += mac_copies_lead_lines(copies)
    return [*lines, ""] if lines else []


def launches_section(connection: sqlite3.Connection, day: str) -> list[str]:
    rows = fetch(connection,
        "SELECT caller, step, count(*), count(mem_wait_s), sum(mem_wait_s), max(mem_wait_s) FROM steps"
        + f" WHERE {ON_DAY} AND (step = 'launch' OR caller = 'brp-launch') GROUP BY caller, step ORDER BY caller, step", day)
    lines = ["### App launches", "",
        "Source: recorded process starts and legacy BRP build-and-launch results. App launches take no memory admission; their null mem_wait_s means no admission.", ""]
    if not rows:
        return [*lines, "Launches: 0.", ""]
    body: list[list[str]] = []
    for caller, step, total, measured, wait, longest in rows:
        wait_cells = ["no admission"] * 3 if step == "launch" else [
            count(measured), seconds(wait) if measured else "not recorded", seconds(longest) if measured else "not recorded",
        ]
        body.append([CALLER_LABELS.get(str(caller), str(caller)), count(total), *wait_cells])
    total = sum(cast(int, row[2]) for row in rows)
    no_admission = sum(cast(int, row[2]) for row in rows if row[1] == "launch")
    measured = sum(cast(int, row[3]) for row in rows if row[1] != "launch")
    summary = f"Launches: {total}"
    if no_admission:
        summary += f"; no admission: {no_admission}"
    if total > no_admission:
        legacy = "legacy " if no_admission else ""
        summary += f"; {legacy}mem_wait_s recorded for {measured}, not recorded for {total - no_admission - measured}"
    return [*lines, summary + ".", "",
        *table(["Launch path", "Launches", "mem_wait_s recorded", "mem_wait_s total", "mem_wait_s max"], body), ""]


def recorder_health_lines(day: str) -> list[str]:
    """Show outstanding retries and errors; recovered pending errors stay only in the audit log."""
    since = datetime.combine(date.fromisoformat(day), datetime.min.time(), user_zone())
    pending = sum(1 for root in (record.pending_launches_root(), record.pending_steps_root())
                  for path in root.glob("*.json") if path.is_file())
    errors = 0
    try:
        with (store.root() / store.ERRORS_NAME).open() as handle:
            for line in handle:
                timestamp, separator, context = line.rstrip("\n").partition(" ")
                if not separator:
                    continue
                try:
                    instant = datetime.fromisoformat(timestamp)
                except ValueError:
                    continue
                if instant.tzinfo is None or instant < since:
                    continue
                if context.startswith("pending record ") and not Path(context.removeprefix("pending record ")).is_file():
                    continue
                errors += 1
    except FileNotFoundError:
        pass
    if not pending and not errors:
        return []
    return [f"{pending} records not published yet; {errors} recorder errors since {local_time(since.isoformat())}"]


def report(connection: sqlite3.Connection, day: str) -> str:
    found = kinds(connection, day)
    hosts = cast(
        int,
        fetch(
            connection,
            f"SELECT count(DISTINCT host) FROM steps WHERE {ON_DAY}"
            + f" AND NOT {GROUP_AS_SCRATCH} AND {natedev_step_predicate()}",
            day,
        )[0][0],
    )
    test_calls = reported_mac_calls(connection, day)
    lines = [f"## Builds, {date.fromisoformat(day).strftime('%A %Y-%m-%d')}", ""]
    lines += recorder_health_lines(day)
    waits = daily_waits(connection, day)
    copies = daily_mac_copies(connection, day, test_calls)
    lines += lead_block(connection, day, test_calls, waits, copies)
    lines += waiting_lines(waits)
    lines += rebuilds_section(connection, day)
    lines += launches_section(connection, day)
    for kind in found:
        lines += kind_section(connection, day, kind, hosts)
    lines += memory_pressure_section(connection, day, hosts)
    lines += cpu_disk_pressure_section(connection, day)
    calls, calls_line = calls_section(connection, day)
    port_lint, port_lint_line = port_lint_section(connection, day)
    ci, ci_line = ci_section(connection, day)
    lines += (
        calls
        + test_builds_section(connection, day)
        + power_profile_section(connection, day)
        + tests_by_target_lines(test_calls)
        + mac_section(connection, day)
        + mac_copies_lines(copies)
        + port_lint
        + ci
        + tests_per_edit_section(connection, day)
        + disk_section()
    )
    if found:
        for name, which, with_failed in SUMMARIES:
            lines += [f"### Summary: {name}", "", *summary(connection, day, found, which, with_failed), ""]
    else:
        lines += ["### Summary", "", "No build steps recorded.", ""]
    lines += [calls_line, *port_lint_line, ci_line, *rust_release.report_line()]
    notes = [
        mac_note(),
        "Peak memory is measured on natedev only. It counts files the step read or wrote that stayed in RAM, so it runs above what the step's processes used.",
    ]
    lines += ["", *(f"- {note}" for note in notes)]
    return "\n".join(lines)
