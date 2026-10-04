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

import sync

KIND_ORDER = ["fmt", "check", "clippy", "mend", "doc", "nextest", "sweep"]
CALLER_LABELS = {
    "verify": "verify.sh (agents)",
    "cargo-port": "cargo-port",
    "agent": "agent (direct)",
    "alias": "terminal",
    "validate_ci": "push gate",
    "unknown": "unknown",
}
OUTCOME_ORDER = ["ran", "failed", "interrupted", "reused", "replayed", "deferred"]
SCRATCH = "(cwd LIKE '/tmp/%' OR cwd LIKE '/var/folders/%' OR cwd LIKE '/private/var/folders/%')"
SCRATCH_LABEL = "scratch (temp folders)"
ON_DAY = "date(started_at, 'localtime') = ?"
COMMON_HEAD = ["Runs", "Failed", "Avg", "Range"]
COMMON_SQL = "count(*), sum(status <> 0), avg(duration_s), min(duration_s), max(duration_s)"
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


def kind_section(connection: sqlite3.Connection, day: str, kind: str, hosts: int) -> list[str]:
    extra = EXTRA.get(kind, [])
    select = ", ".join([COMMON_SQL, *(column.sql for column in extra)])
    rows = fetch(
        connection,
        f"SELECT {SCRATCH} AS is_scratch, CASE WHEN {SCRATCH} THEN NULL ELSE host END AS caller_host,"
        + f" CASE WHEN {SCRATCH} THEN NULL ELSE caller END AS grouped_caller, {select}"
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
    queue = fetch(connection, f"SELECT avg(queued_s) FROM ci_jobs WHERE {ON_DAY}", day)[0][0]
    body = [[str(row[0]), *common(row[1:6])] for row in rows]
    runs = sum(cast(int, row[1]) for row in rows)
    failed = sum(cast(int, row[2] or 0) for row in rows)
    total = sum(cast(float, row[6] or 0.0) for row in rows)
    section = ["### CI", "", *table(["Workflow", *COMMON_HEAD], body), ""]
    return section, f"CI: {runs} runs, {failed} failed, {seconds(total)} in all; jobs queued {seconds(queue)} on average."


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


def mac_note() -> str:
    status = sync.read_status()
    if status is None:
        return "Mac: not synced yet."
    last = status["last_ok"]
    when = sync_time(last, datetime.now()) if last else "never"
    return f"Mac rows as of the {when} sync." if status["ok"] else f"Mac: the last sync failed; last good sync {when}."


def report(connection: sqlite3.Connection, day: str) -> str:
    found = kinds(connection, day)
    hosts = cast(int, fetch(connection, f"SELECT count(DISTINCT host) FROM steps WHERE {ON_DAY} AND NOT {SCRATCH}", day)[0][0])
    lines = [f"## Builds, {date.fromisoformat(day).strftime('%A %Y-%m-%d')}", ""]
    for kind in found:
        lines += kind_section(connection, day, kind, hosts)
    calls, calls_line = calls_section(connection, day)
    port_lint, port_lint_line = port_lint_section(connection, day)
    ci, ci_line = ci_section(connection, day)
    lines += calls + test_builds_section(connection, day) + port_lint + ci + tests_per_edit_section(connection, day)
    if found:
        for name, which, with_failed in SUMMARIES:
            lines += [f"### Summary: {name}", "", *summary(connection, day, found, which, with_failed), ""]
    else:
        lines += ["### Summary", "", "No build steps recorded.", ""]
    lines += [calls_line, *port_lint_line, ci_line]
    notes = [
        mac_note(),
        "Peak memory is measured on natedev only. It counts files the step read or wrote that stayed in RAM, so it runs above what the step's processes used.",
    ]
    lines += ["", *(f"- {note}" for note in notes)]
    return "\n".join(lines)
