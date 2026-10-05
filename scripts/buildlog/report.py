"""buildlog report: one local day as markdown, a section per kind of step split by caller, then a summary.

The summary at the bottom comes three times, successes, failures and all: one
row per kind, every caller together, and a total. Steps under temp folders
appear as one scratch caller in each kind's table.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import cast

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
COMMON_HEAD = ["Runs", "Failed", "Avg", "Range"]
COMMON_SQL = "count(*), sum(status <> 0), avg(duration_s), min(duration_s), max(duration_s)"
MAX_SAMPLE_GAP_S = 5 * 60
# 2026-10-01–04 log: 1 edit 12.54 min, 2–3 16.09, 4–7 20.94, 8+ 31.28.
# One test per two edits leaves headroom for most gaps to stay within 2–3;
# the 4–7 bin is where recovery time starts to climb.
TESTS_PER_EDIT_TARGET = 0.5
EDIT_BINS = ("0", "1", "2–3", "4–7", "8+")

Row = tuple[object, ...]


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


@dataclass(frozen=True)
class Column:
    title: str
    sql: str
    show: Callable[[object], str]


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


@dataclass
class TestsPerEditWindow:
    first_day: str
    daily: dict[str, DailyTestsPerEdit]
    recovery_bins: list[FailureRecoveryBin]


EXTRA: dict[str, list[Column]] = {
    "check": [Column("Build", "avg(finished_s)", seconds), Column("Warnings", "sum(warnings)", count)],
    "clippy": [
        Column("Build", "avg(finished_s)", seconds),
        Column("Warnings", "sum(warnings)", count),
        Column("Errors", "sum(errors)", count),
    ],
    "mend": [
        Column("Mend's own", "avg(mend_s)", seconds),
        Column("Check", "avg(mend_check_s)", seconds),
        Column("Fixes", "sum(mend_fixes)", count),
    ],
    "doc": [Column("Build", "avg(finished_s)", seconds), Column("Warnings", "sum(warnings)", count)],
    "nextest": [
        Column("Build", "avg(finished_s)", seconds),
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
    runs, failed, average, low, high = row[:5]
    span = seconds(low) if low == high else f"{seconds(low)} – {seconds(high)}"
    return [count(runs), count(failed), seconds(average), span]


def fetch(connection: sqlite3.Connection, sql: str, *params: object) -> list[Row]:
    return cast(list[Row], connection.execute(sql, params).fetchall())


def kinds(connection: sqlite3.Connection, day: str) -> list[str]:
    found = [cast(str, row[0]) for row in fetch(connection, f"SELECT DISTINCT step FROM steps WHERE {ON_DAY}", day)]
    return [kind for kind in KIND_ORDER if kind in found] + sorted(kind for kind in found if kind not in KIND_ORDER)


def caller_label(caller: object, host: object, hosts: int) -> str:
    label = CALLER_LABELS.get(str(caller), str(caller))
    return f"{label} ({host})" if hosts > 1 else label


def wait_row(label: str, waits: list[tuple[float, str, str, str]], unit: str) -> list[str]:
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


def waiting_section(connection: sqlite3.Connection, day: str) -> list[str]:
    calls = fetch(
        connection,
        "SELECT token_wait_s, coalesce(worktree_name, '(unknown worktree)'), started_at FROM calls"
        + " WHERE date(started_at, 'localtime') = ?",
        day,
    )
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
        wait_row("Build-folder turn", [(wait_seconds(duration), str(tree), str(tree), str(at)) for duration, tree, at in calls], "calls"),
        wait_row(
            "Memory admission",
            [(wait_seconds(duration), str(tree), f"{tree} {seat}" if seat else str(tree), str(at)) for duration, tree, seat, at in steps],
            "steps",
        ),
        wait_row("CI queue", [(wait_seconds(duration), f"{workflow} / {name}", f"{workflow} / {name}", str(at)) for duration, workflow, name, at in jobs], "jobs"),
    ]
    return ["### Waiting", "", *table(["Wait", "Longest", "Over 5 min", "Waited", "Total", "Worst"], rows), ""]


def kind_section(connection: sqlite3.Connection, day: str, kind: str, hosts: int) -> list[str]:
    extra = EXTRA.get(kind, [])
    select = ", ".join([COMMON_SQL, *(column.sql for column in extra)])
    rows = fetch(
        connection,
        f"SELECT {GROUP_AS_SCRATCH} AS is_scratch, CASE WHEN {GROUP_AS_SCRATCH} THEN NULL ELSE host END AS caller_host,"
        + f" CASE WHEN {GROUP_AS_SCRATCH} THEN NULL ELSE caller END AS grouped_caller, {select}"
        + f" FROM steps WHERE {ON_DAY} AND step = ?"
        + " GROUP BY is_scratch, caller_host, grouped_caller ORDER BY count(*) DESC",
        day,
        kind,
    )
    body = [
        [
            SCRATCH_LABEL if row[0] else caller_label(row[2], row[1], hosts),
            *common(row[3:8]),
            *(column.show(value) for column, value in zip(extra, row[8:], strict=True)),
        ]
        for row in rows
    ]
    return [f"### {kind}", "", *table(["Caller", *COMMON_HEAD, *(column.title for column in extra)], body), ""]


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
            ordered = sorted(values)
            body.append([day, scope, seconds(sum(values)), seconds(ordered[(3 * len(ordered) - 1) // 4])])

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


def ci_section(connection: sqlite3.Connection, day: str) -> tuple[list[str], str]:
    rows = fetch(
        connection,
        "SELECT workflow, count(*), sum(conclusion = 'failure'), avg(duration_s), min(duration_s), max(duration_s), sum(duration_s)"
        + f" FROM ci_runs WHERE {ON_DAY} GROUP BY workflow ORDER BY count(*) DESC",
        day,
    )
    if not rows:
        return [], "CI: no runs."
    queue, left_out = fetch(
        connection,
        "SELECT avg(queued_s), coalesce(sum(run_state != 'skipped' AND queued_s IS NULL), 0)"
        + " FROM ci_jobs AS j JOIN ci_runs AS r ON j.run_id = r.run_id AND j.attempt = r.attempt"
        + " WHERE date(r.started_at, 'localtime') = ?",
        day,
    )[0]
    body = [[str(row[0]), *common(row[1:6])] for row in rows]
    runs = sum(cast(int, row[1]) for row in rows)
    failed = sum(cast(int, row[2] or 0) for row in rows)
    total = sum(cast(float, row[6] or 0.0) for row in rows)
    section = ["### CI", "", *table(["Workflow", *COMMON_HEAD], body), ""]
    omitted = cast(int, left_out)
    if queue is None:
        queue_clause = "no job has a known queue time"
        if omitted:
            queue_clause += f" ({omitted} left out)"
    else:
        queue_clause = f"jobs queued {seconds(queue)} on average"
        if omitted:
            queue_clause += f", {omitted} without a known queue time left out"
    return section, f"CI: {runs} runs, {failed} failed, {seconds(total)} in all; {queue_clause}."


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
                recovery.minutes_to_green += max(0.0, (finished_at - failed_at).total_seconds() / 60)
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
        [label, count(recovery.failures), seconds(recovery.minutes_to_green * 60 / recovery.recovered) if recovery.recovered else ""]
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
        *table(["Edits since green", "Failures", "Avg to next green"], recovery_rows),
        "; ".join(notes) + ".",
        "",
    ]


SUMMARIES = [("successes", "status = 0", False), ("failures", "status <> 0", False), ("all", "1", True)]


def summary(connection: sqlite3.Connection, day: str, found: list[str], which: str, with_failed: bool) -> list[str]:
    """One row per kind, every caller together, then the total; kinds with no runs in the set are left out."""
    select = "count(*), sum(status <> 0), sum(duration_s), avg(duration_s), max(peak_mem_bytes)"
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
        return [name, count(row[0]), *failed, seconds(row[2]), seconds(row[3]), gib(row[4])]

    body = [line(kind, rows[kind]) for kind in found if kind in rows] + [line("**All steps**", total)]
    head = ["Kind", "Runs", *(["Failed"] if with_failed else []), "Total", "Avg", "Peak memory"]
    return table(head, body)


def sync_time(last: str, now: datetime) -> str:
    """Local time with its zone, and the date too when it is not today."""
    at = datetime.fromisoformat(last).astimezone()
    return at.strftime("%H:%M %Z" if at.date() == now.astimezone().date() else "%Y-%m-%d %H:%M %Z")


def disk_section() -> list[str]:
    snapshot = disk.read_snapshot()
    if snapshot is None:
        return []
    rows = [[row["label"], gib(row["bytes"])] for row in snapshot["rows"]]
    rows.append(["other", gib(max(0, snapshot["used"] - sum(row["bytes"] for row in snapshot["rows"])))])
    floor = snapshot["floor"]
    free_label = f"free (floor {gib(floor)})" if floor is not None else "free"
    rows.append([free_label, gib(snapshot["free"])])
    measured = sync_time(snapshot["measured_at"], datetime.now())
    return [
        f"### Disk: {snapshot['host']}",
        "",
        *table(["Where", "Size"], rows),
        "",
        f"Measured by the buildlog disk job at {measured}: allocated blocks, each hard-linked file once.",
        "",
    ]


def mac_note() -> str:
    status = sync.read_status()
    if status is None:
        return "Mac: not synced yet."
    last = status["last_ok"]
    when = sync_time(last, datetime.now()) if last else "never"
    return f"Mac rows as of the {when} sync." if status["ok"] else f"Mac: the last sync failed; last good sync {when}."


def report(connection: sqlite3.Connection, day: str) -> str:
    found = kinds(connection, day)
    hosts = cast(int, fetch(connection, f"SELECT count(DISTINCT host) FROM steps WHERE {ON_DAY} AND NOT {GROUP_AS_SCRATCH}", day)[0][0])
    lines = [f"## Builds, {date.fromisoformat(day).strftime('%A %Y-%m-%d')}", ""]
    lines += waiting_section(connection, day)
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
