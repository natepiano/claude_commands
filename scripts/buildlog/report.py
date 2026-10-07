"""buildlog report: one local day as markdown, a section per kind of step split by caller, then a summary.

The summary at the bottom comes three times, successes, failures and all: one
row per kind, every caller together, and a total. Steps under temp folders
appear as one scratch caller in each kind's table.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import cast

import ci
import disk
import memory
import sync
import rust_release

KIND_ORDER = ["fmt", "check", "clippy", "mend", "doc", "nextest", "sweep"]
CALLER_LABELS = {
    "verify": "verify.sh (agents)",
    "cargo-port": "cargo-port",
    "agent": "agent (direct)",
    "alias": "terminal",
    "validate_ci": "push gate",
    "brp-launch": "example launches (brp)",
    "unknown": "unknown",
}
OUTCOME_ORDER = ["ran", "failed", "interrupted", "reused", "replayed", "deferred"]
SCRATCH = "(cwd LIKE '/tmp/%' OR cwd LIKE '/var/folders/%' OR cwd LIKE '/private/var/folders/%')"
GROUP_AS_SCRATCH = f"({SCRATCH} AND caller != 'brp-launch')"
SCRATCH_LABEL = "scratch (temp folders)"
ON_DAY = "date(started_at, 'localtime') = ?"
COMMON_HEAD = ["Runs", "Failed", "Avg", "p95", "Range"]
COMMON_SQL = "count(*), sum(status <> 0), avg(duration_s), min(duration_s), max(duration_s), group_concat(duration_s)"
CI_STALE_AFTER_S = 2 * 3600
MAX_SAMPLE_GAP_S = 5 * 60
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

Row = tuple[object, ...]
ReportedWait = tuple[float, str, str, str]


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
    delegate_session: str
    seat: str
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True)
class TokenWaitAttribution:
    behind_another_seat_s: float
    behind_own_call_s: float


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


def kinds(connection: sqlite3.Connection, day: str) -> list[str]:
    found = [cast(str, row[0]) for row in fetch(connection, f"SELECT DISTINCT step FROM steps WHERE {ON_DAY}", day)]
    return [kind for kind in KIND_ORDER if kind in found] + sorted(kind for kind in found if kind not in KIND_ORDER)


def caller_label(caller: object, host: object, hosts: int) -> str:
    label = CALLER_LABELS.get(str(caller), str(caller))
    return f"{label} ({host})" if hosts > 1 else label


def wait_row(label: str, waits: list[ReportedWait], unit: str) -> list[str]:
    """Summarize measured waits, keeping parallel seats as separate time."""
    if not waits:
        return [label, "none", "", "", "", ""]
    positive = [(duration, owner, name, at) for duration, owner, name, at in waits if duration > 0]
    waited = f"{len(positive)} of {len(waits)} {unit}"
    if not positive:
        return [label, "none", "0", waited, f"0.0 {'job' if unit == 'jobs' else 'seat'}-hours", ""]
    longest, _, name, at = max(positive, key=lambda entry: entry[0])
    when = datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone().strftime("%H:%M %Z")
    totals: dict[str, float] = {}
    for duration, owner, _, _ in positive:
        totals[owner] = totals.get(owner, 0.0) + duration
    worst = ", ".join(
        f"{owner} {seconds(duration)}" for owner, duration in sorted(totals.items(), key=lambda item: (-item[1], item[0]))[:3]
    )
    hours = f"{sum(duration for duration, _, _, _ in positive) / 3600:.1f} {'job' if unit == 'jobs' else 'seat'}-hours"
    return [label, f"{seconds(longest)} ({name}, {when})", str(sum(duration > 300 for duration, _, _, _ in positive)), waited, hours, worst]


def wait_seconds(value: object) -> float:
    return float(value) if isinstance(value, int | float) else 0.0


def call_time(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def token_holders(rows: list[Row]) -> list[TokenHolder]:
    holders: list[TokenHolder] = []
    for call_id, started_at, wait_s, ended_at, seat, delegate_session in rows:
        starts_at = call_time(started_at) + timedelta(seconds=wait_seconds(wait_s))
        ends_at = call_time(ended_at)
        if starts_at < ends_at:
            holders.append(TokenHolder(str(call_id), str(delegate_session), str(seat), starts_at, ends_at))
    return holders


def attribute_token_wait(
    call_id: str,
    delegate_session: str,
    seat: str,
    starts_at: datetime,
    wait_s: float,
    token_wait_s: float,
    holders: Sequence[TokenHolder],
) -> TokenWaitAttribution:
    wait_ends_at = starts_at + timedelta(seconds=wait_s)
    wait_starts_at = wait_ends_at - timedelta(seconds=token_wait_s)
    overlapping = [
        holder for holder in holders
        if holder.call_id != call_id and holder.delegate_session == delegate_session
        and holder.starts_at < wait_ends_at and holder.ends_at > wait_starts_at
    ]
    boundaries = sorted({wait_starts_at, wait_ends_at, *(point for holder in overlapping for point in (holder.starts_at, holder.ends_at))})
    own_s = 0.0
    for left, right in zip(boundaries, boundaries[1:]):
        if left < wait_starts_at or right > wait_ends_at:
            continue
        covering = [holder for holder in overlapping if holder.starts_at < right and holder.ends_at > left]
        if covering and all(holder.seat == seat for holder in covering):
            own_s += (right - left).total_seconds()
    own_s = min(token_wait_s, own_s)
    return TokenWaitAttribution(token_wait_s - own_s, own_s)


def build_folder_waits(connection: sqlite3.Connection, day: str) -> tuple[list[ReportedWait], list[ReportedWait]]:
    calls = fetch(
        connection,
        "SELECT id, token_wait_s, coalesce(worktree_name, '(unknown worktree)'), started_at,"
        + " coalesce(wait_s, 0), coalesce(seat, '(unknown seat)'), delegate_session FROM calls"
        + " WHERE tool = 'verify.sh' AND date(started_at, 'localtime') = ?",
        day,
    )
    holder_rows = fetch(
        connection,
        "SELECT id, started_at, coalesce(wait_s, 0), ended_at, coalesce(seat, '(unknown seat)'), delegate_session"
        + " FROM calls WHERE tool = 'verify.sh' AND ended_at IS NOT NULL AND delegate_session IS NOT NULL"
        + " AND date(ended_at, 'localtime') >= ?",
        day,
    )
    holders_by_session: dict[str, list[TokenHolder]] = {}
    for holder in token_holders(holder_rows):
        holders_by_session.setdefault(holder.delegate_session, []).append(holder)
    another_waits: list[ReportedWait] = []
    own_waits: list[ReportedWait] = []
    for call_id, duration, tree, started_at, wait_s, seat, delegate_session in calls:
        token_wait_s = wait_seconds(duration)
        attribution = TokenWaitAttribution(token_wait_s, 0.0) if token_wait_s <= 0 or not isinstance(delegate_session, str) else attribute_token_wait(
            str(call_id), delegate_session, str(seat), call_time(started_at), wait_seconds(wait_s), token_wait_s,
            holders_by_session.get(delegate_session, []),
        )
        identity = (str(tree), str(tree), str(started_at))
        another_waits.append((attribution.behind_another_seat_s, *identity))
        own_waits.append((attribution.behind_own_call_s, *identity))
    return another_waits, own_waits


def waiting_section(connection: sqlite3.Connection, day: str) -> list[str]:
    another_waits, own_waits = build_folder_waits(connection, day)
    steps = fetch(
        connection,
        "SELECT mem_wait_s, coalesce(worktree_name, '(unknown worktree)'), seat, started_at FROM steps"
        + " WHERE date(started_at, 'localtime') = ? AND mem_wait_s IS NOT NULL",
        day,
    )
    jobs = fetch(
        connection,
        "SELECT j.queued_s, j.workflow, j.name, j.created_at FROM ci_jobs AS j"
        + " JOIN ci_runs AS r ON j.run_id = r.run_id AND j.attempt = r.attempt"
        + " WHERE date(r.started_at, 'localtime') = ? AND j.run_state = 'ran' AND j.queued_s IS NOT NULL",
        day,
    )
    rows = [
        wait_row("Build-folder turn, behind another seat", another_waits, "calls"),
        wait_row("Build-folder turn, behind its own call", own_waits, "calls"),
        wait_row(
            "Memory admission",
            [(wait_seconds(duration), str(tree), f"{tree} {seat}" if seat else str(tree), str(at)) for duration, tree, seat, at in steps],
            "steps",
        ),
        wait_row("CI queue", [(wait_seconds(duration), f"{workflow} / {name}", f"{workflow} / {name}", str(at)) for duration, workflow, name, at in jobs], "jobs"),
    ]
    source = "Source: verify.sh calls, memory-gated steps and CI jobs; a seat's own calls run one at a time, so waiting behind its own call adds no delay, behind another seat does."
    return ["### Waiting", "", *table(["Wait", "Longest", "Over 5 min", "Waited", "Total", "Worst"], rows), source, ""]


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
        + " AND step <> 'sweep' AND crates_compiled IS NOT NULL",
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
        + f" FROM steps WHERE {ON_DAY} AND step = ?"
        + " GROUP BY is_scratch, caller_host, grouped_caller ORDER BY count(*) DESC",
        day,
        kind,
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


def memory_pressure_section(connection: sqlite3.Connection, day: str, hosts: int) -> list[str]:
    """The largest step stalls and the day's sampled machine memory pressure."""
    waits = fetch(
        connection,
        "SELECT count(*), sum(mem_wait_s), max(mem_wait_s) FROM steps"
        + " WHERE date(started_at, 'localtime') = ? AND mem_wait_s > 0",
        day,
    )[0]
    longest = fetch(
        connection,
        f"SELECT {SCRATCH} AS is_scratch, caller, host FROM steps"
        + " WHERE date(started_at, 'localtime') = ? AND mem_wait_s > 0"
        + " ORDER BY mem_wait_s DESC LIMIT 1",
        day,
    )
    stalled = fetch(
        connection,
        f"SELECT {SCRATCH} AS is_scratch, s.step, s.caller, s.host, s.mem_stall_some_s,"
        + " (SELECT count(*) FROM steps other WHERE other.host = s.host"
        + " AND other.started_at <= s.started_at AND s.started_at < other.ended_at)"
        + " FROM steps s WHERE date(s.started_at, 'localtime') = ? AND s.mem_stall_some_s > 0"
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
    instruments = memory.instrument_lines(connection, "date({column}, 'localtime') = ?", (day,))
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

    previous: dict[str, tuple[str, str, int, int]] = {}
    some_us = full_us = 0
    for host, at, boot_id, _, _, some, full, _, _ in samples:
        machine = str(host)
        before = previous.get(machine)
        if before is None:
            earlier = fetch(
                connection,
                "SELECT at, boot_id, stall_some_us, stall_full_us FROM samples"
                + " WHERE host = ? AND at < ? ORDER BY at DESC LIMIT 1",
                machine,
                at,
            )
            before = (str(earlier[0][0]), str(earlier[0][1]), cast(int, earlier[0][2]), cast(int, earlier[0][3])) if earlier else None
        now_some, now_full = cast(int, some), cast(int, full)
        if before is not None and 0 <= (datetime.fromisoformat(str(at)) - datetime.fromisoformat(before[0])).total_seconds() <= MAX_SAMPLE_GAP_S:
            if boot_id == before[1]:
                some_us += max(0, now_some - before[2])
                full_us += max(0, now_full - before[3])
            else:
                some_us += now_some
                full_us += now_full
        previous[machine] = str(at), str(boot_id), now_some, now_full

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
        f"SELECT outcome, count(*), sum(wall_s), sum(saved_s) FROM calls WHERE {ON_DAY} AND tool = ? GROUP BY outcome",
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
        f"SELECT command, build_s FROM calls WHERE {ON_DAY} AND tool = 'verify.sh' AND verb = 'test' AND build_s IS NOT NULL",
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
    now = datetime.now().astimezone()
    day_end = datetime.combine(date.fromisoformat(day) + timedelta(days=1), datetime.min.time()).astimezone()
    horizon = min(now, day_end)
    at = datetime.fromisoformat(newest.polled_at.replace("Z", "+00:00")).astimezone()
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
                "SELECT s.call_id, s.tree_key FROM steps s JOIN calls c ON c.id = s.call_id",
                "WHERE c.tool = 'verify.sh' AND s.tree_key IS NOT NULL",
                "AND date(c.started_at, 'localtime') <= ?",
                "ORDER BY s.call_id, s.started_at, s.id",
            )
        ),
        end_day,
    )
    trees: dict[str, KnownCallTrees] = {}
    for call_id, tree_key in rows:
        call = cast(str, call_id)
        key = cast(str, tree_key)
        previous = trees.get(call)
        trees[call] = KnownCallTrees(previous.first if previous else key, key)
    return trees


def failure_bin(edits: int) -> int:
    if edits == 0:
        return 0
    if edits == 1:
        return 1
    if edits <= 3:
        return 2
    return 3 if edits <= 7 else 4


def tests_per_edit_data(connection: sqlite3.Connection, end_day: str) -> TestsPerEditWindow:
    """Track each seat through the query's last day, including calls before the displayed window."""
    rows = fetch(
        connection,
        " ".join(
            (
                "SELECT id, started_at, ended_at, date(started_at, 'localtime'),",
                "coalesce(nullif(delegate_session, ''), nullif(session, '')), verb, status, outcome",
                "FROM calls WHERE tool = 'verify.sh'",
                "AND date(started_at, 'localtime') <= ?",
                "ORDER BY started_at, id",
            )
        ),
        end_day,
    )
    trees = call_trees(connection, end_day)
    first_day = (date.fromisoformat(end_day) - timedelta(days=6)).isoformat()
    daily: dict[str, DailyTestsPerEdit] = {}
    bins = [FailureRecoveryBin() for _ in EDIT_BINS]
    last_tree: dict[str, str] = {}
    edits_since_green: dict[str, int] = {}
    waiting_for_green: dict[str, list[tuple[int, datetime]]] = {}

    for call_id, started_at, ended_at, call_day, seat_value, verb, status, outcome in rows:
        if seat_value is None:
            continue
        seat = cast(str, seat_value)
        day = cast(str, call_day)
        in_window = day >= first_day
        activity = daily.setdefault(day, DailyTestsPerEdit()) if in_window else None
        span = trees.get(cast(str, call_id))
        if span is not None:
            previous = last_tree.get(seat)
            if previous is not None and previous != span.first:
                edits_since_green[seat] = edits_since_green.get(seat, 0) + 1
                if activity is not None:
                    activity.edits += 1
            last_tree[seat] = span.last

        if verb != "test":
            continue
        if activity is not None:
            activity.tests += 1
        finished_at = datetime.fromisoformat(cast(str, ended_at or started_at))
        if status == 0 and outcome in ("ran", "reused"):
            for bin_index, failed_at in waiting_for_green.pop(seat, []):
                recovery = bins[bin_index]
                recovery.recovered += 1
                minutes = max(0.0, (finished_at - failed_at).total_seconds() / 60)
                recovery.minutes_to_green += minutes
                recovery.recovery_minutes.append(minutes)
            edits_since_green[seat] = 0
        elif outcome != "interrupted" and (outcome == "failed" or status is not None and status != 0) and in_window:
            edits = edits_since_green.get(seat, 0)
            bin_index = failure_bin(edits)
            bins[bin_index].failures += 1
            waiting_for_green.setdefault(seat, []).append((bin_index, finished_at))
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
    where = f"{ON_DAY} AND {which}"
    rows = {
        cast(str, row[0]): row[1:]
        for row in fetch(connection, f"SELECT step, {select} FROM steps WHERE {where} GROUP BY step", day)
    }
    total = fetch(connection, f"SELECT {select} FROM steps WHERE {where}", day)[0]
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


def report(connection: sqlite3.Connection, day: str) -> str:
    found = kinds(connection, day)
    hosts = cast(int, fetch(connection, f"SELECT count(DISTINCT host) FROM steps WHERE {ON_DAY} AND NOT {GROUP_AS_SCRATCH}", day)[0][0])
    lines = [f"## Builds, {date.fromisoformat(day).strftime('%A %Y-%m-%d')}", ""]
    lines += waiting_section(connection, day)
    lines += rebuilds_section(connection, day)
    for kind in found:
        lines += kind_section(connection, day, kind, hosts)
    lines += memory_pressure_section(connection, day, hosts)
    calls, calls_line = calls_section(connection, day)
    port_lint, port_lint_line = port_lint_section(connection, day)
    ci, ci_line = ci_section(connection, day)
    lines += calls + test_builds_section(connection, day) + port_lint + ci + tests_per_edit_section(connection, day) + disk_section()
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
