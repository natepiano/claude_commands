"""buildlog report: one local day as markdown, a section per kind of step split by caller, then a summary.

The summary at the bottom comes three times, successes, failures and all: one
row per kind, every caller together, and a total. Steps that ran under a temp folder (scratch crates, buildlog's own test
runs) are left out and counted in the footer.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
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
OUTCOME_ORDER = ["ran", "failed", "interrupted", "reused", "replayed"]
SCRATCH = "(cwd LIKE '/tmp/%' OR cwd LIKE '/var/folders/%' OR cwd LIKE '/private/var/folders/%')"
ON_DAY = "date(started_at, 'localtime') = ?"
COMMON_HEAD = ["Runs", "Failed", "Avg", "Range"]
COMMON_SQL = "count(*), sum(status <> 0), avg(duration_s), min(duration_s), max(duration_s)"

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
    found = [cast(str, row[0]) for row in fetch(connection, f"SELECT DISTINCT step FROM steps WHERE {ON_DAY} AND NOT {SCRATCH}", day)]
    return [kind for kind in KIND_ORDER if kind in found] + sorted(kind for kind in found if kind not in KIND_ORDER)


def caller_label(caller: object, host: object, hosts: int) -> str:
    label = CALLER_LABELS.get(str(caller), str(caller))
    return f"{label} ({host})" if hosts > 1 else label


def kind_section(connection: sqlite3.Connection, day: str, kind: str, hosts: int) -> list[str]:
    extra = EXTRA.get(kind, [])
    select = ", ".join([COMMON_SQL, *(column.sql for column in extra)])
    rows = fetch(
        connection,
        f"SELECT host, caller, {select} FROM steps WHERE {ON_DAY} AND step = ? AND NOT {SCRATCH}"
        + " GROUP BY host, caller ORDER BY count(*) DESC",
        day,
        kind,
    )
    body = [
        [caller_label(row[1], row[0], hosts), *common(row[2:7]), *(column.show(value) for column, value in zip(extra, row[7:], strict=True))]
        for row in rows
    ]
    return [f"### {kind}", "", *table(["Caller", *COMMON_HEAD, *(column.title for column in extra)], body), ""]


def calls_section(connection: sqlite3.Connection, day: str) -> tuple[list[str], str]:
    rows = fetch(
        connection,
        f"SELECT outcome, count(*), sum(wall_s), sum(saved_s) FROM calls WHERE {ON_DAY} GROUP BY outcome",
        day,
    )
    if not rows:
        return [], "Agent calls: none."
    rows.sort(key=lambda row: OUTCOME_ORDER.index(str(row[0])) if row[0] in OUTCOME_ORDER else len(OUTCOME_ORDER))
    body = [[str(row[0]), count(row[1]), some_seconds(row[2]), some_seconds(row[3])] for row in rows]
    total = sum(cast(int, row[1]) for row in rows)
    saved = sum(cast(int, row[3] or 0) for row in rows)
    parts = ", ".join(f"{count(row[1])} {row[0]}" for row in rows)
    section = ["### Agent calls (verify.sh)", "", *table(["Outcome", "Calls", "Wall", "Saved"], body), ""]
    return section, f"Agent calls: {total} ({parts}), {seconds(saved)} saved by pass records."


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


SUMMARIES = [("successes", "status = 0", False), ("failures", "status <> 0", False), ("all", "1", True)]


def summary(connection: sqlite3.Connection, day: str, found: list[str], which: str, with_failed: bool) -> list[str]:
    """One row per kind, every caller together, then the total; kinds with no runs in the set are left out."""
    select = "count(*), sum(status <> 0), sum(duration_s), avg(duration_s), max(peak_mem_bytes)"
    where = f"{ON_DAY} AND NOT {SCRATCH} AND {which}"
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


def mac_note() -> str:
    status = sync.read_status()
    if status is None:
        return "Mac: not synced yet."
    last = status["last_ok"]
    when = datetime.fromisoformat(last).astimezone().strftime("%H:%M") if last else "never"
    return f"Mac rows as of the {when} sync." if status["ok"] else f"Mac: the last sync failed; last good sync {when}."


def report(connection: sqlite3.Connection, day: str) -> str:
    found = kinds(connection, day)
    hosts = cast(int, fetch(connection, f"SELECT count(DISTINCT host) FROM steps WHERE {ON_DAY} AND NOT {SCRATCH}", day)[0][0])
    scratch = cast(int, fetch(connection, f"SELECT count(*) FROM steps WHERE {ON_DAY} AND {SCRATCH}", day)[0][0])
    lines = [f"## Builds, {date.fromisoformat(day).strftime('%A %Y-%m-%d')}", ""]
    for kind in found:
        lines += kind_section(connection, day, kind, hosts)
    calls, calls_line = calls_section(connection, day)
    ci, ci_line = ci_section(connection, day)
    lines += calls + ci
    if found:
        for name, which, with_failed in SUMMARIES:
            lines += [f"### Summary: {name}", "", *summary(connection, day, found, which, with_failed), ""]
    else:
        lines += ["### Summary", "", "No build steps recorded.", ""]
    lines += [calls_line, ci_line]
    footer = mac_note() + " Peak memory includes file cache."
    if scratch:
        footer += f" {scratch} steps under a temp folder (scratch and test builds) are left out."
    lines += ["", footer]
    return "\n".join(lines)
