#!/usr/bin/env python3
"""buildlog: a permanent, queryable record of every build, lint and test step.

  buildlog query [--json] "<SQL>"   bring the index up to date, run SQL read-only
  buildlog schema                   tables, columns, views and example queries
  buildlog report [YYYY-MM-DD]      one day (default today) as markdown: each kind by caller, then a summary
  buildlog reindex                  rebuild the index from the JSON lines
  buildlog tree-key [DIR]           the tree key of DIR's worktree (default the current folder; treekey.py)
  buildlog sync                     exchange records with the Mac (natedev's hourly job)
  buildlog ci                       record new GitHub Actions run attempts
  buildlog sample                   record machine memory and stall counters once
  buildlog hourly                   sync, then ci, each whatever the other did
  buildlog backfill-verify [LEDGER] copy verify.sh's old events.jsonl ledger in, once

Records live in ~/.local/state/buildlog (BUILDLOG_DIR moves it); see store.py.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import date
from typing import cast

import ci
import index
import record
import report
import sample
import store
import sync
import treekey

EXAMPLES: list[tuple[str, str]] = [
    (
        "How long did clippy take per day this week, and how often did it fail?",
        "SELECT day, host, repo, runs, failed, total_s, avg_s FROM step_days"
        + " WHERE step = 'clippy' AND day >= date('now', 'localtime', '-6 days') ORDER BY day",
    ),
    (
        "Which callers spend the most build time? (verify, cargo-port, validate_ci, agent, alias, unknown)",
        "SELECT caller, count(*) AS runs, round(sum(duration_s) / 3600.0, 2) AS hours FROM steps"
        + " GROUP BY caller ORDER BY hours DESC",
    ),
    (
        "What failed today, and where is its log?",
        "SELECT at, repo, worktree_name, step, status, errors, tests_failed, log FROM failures"
        + " WHERE date(at) = date('now', 'localtime') ORDER BY at",
    ),
    ("Which tests are flaky?", "SELECT * FROM flaky_tests ORDER BY flaky DESC, failed DESC"),
    ("Which tests are slowest?", "SELECT repo, binary, test, runs, avg_s, max_s FROM slow_tests ORDER BY avg_s DESC LIMIT 20"),
    (
        "What did one verify.sh call run, step by step?",
        "SELECT c.command, c.outcome, s.step, s.duration_s, s.finished_s, s.status FROM calls c"
        + " JOIN steps s ON s.call_id = c.id WHERE c.id = (SELECT max(id) FROM calls WHERE outcome IN ('ran', 'failed'))",
    ),
    (
        "How much time did verify.sh pass records save per repo this week?",
        "SELECT repo, sum(calls) AS calls, sum(saved_s) AS saved_s FROM call_outcomes"
        + " WHERE tool = 'verify.sh' AND outcome IN ('reused', 'replayed')"
        + " AND day >= date('now', 'localtime', '-6 days') GROUP BY repo",
    ),
    (
        "Which steps used the most memory?",
        "SELECT datetime(started_at, 'localtime') AS at, repo, step, round(peak_mem_bytes / 1073741824.0, 1) AS gib"
        + " FROM steps WHERE peak_mem_bytes IS NOT NULL ORDER BY peak_mem_bytes DESC LIMIT 10",
    ),
    (
        "How long do CI jobs queue and run, per day?",
        "SELECT day, name, jobs, failed, avg_s, avg_queue_s FROM ci_job_days ORDER BY day DESC, avg_s DESC LIMIT 30",
    ),
]

STEP_NAMES = "clippy, mend, doc, fmt, nextest, check (cargo subcommands), sweep (lint sweep)"


def cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:g}" if abs(value) < 1e15 else str(value)
    return str(value)


def print_table(names: list[str], rows: list[tuple[object, ...]]) -> None:
    texts = [[cell(value) for value in row] for row in rows]
    numeric = [
        all(isinstance(row[column], int | float) or row[column] is None for row in rows) for column in range(len(names))
    ]
    widths = [max([len(name), *(len(text[column]) for text in texts)]) for column, name in enumerate(names)]

    def line(values: list[str]) -> str:
        parts = [
            value.rjust(width) if is_number else value.ljust(width)
            for value, width, is_number in zip(values, widths, numeric, strict=True)
        ]
        return "  ".join(parts).rstrip()

    print(line(names))
    print("  ".join("-" * width for width in widths))
    for text in texts:
        print(line(text))


def query(args: list[str]) -> int:
    as_json = "--json" in args
    words = [word for word in args if word != "--json"]
    if len(words) != 1:
        print('usage: buildlog query [--json] "<SQL>"', file=sys.stderr)
        return 2
    began = time.perf_counter()
    _ = index.update()
    updated = time.perf_counter()
    connection = index.read_only()
    try:
        cursor = connection.execute(words[0])
        rows = cast(list[tuple[object, ...]], cursor.fetchall())
        description = cast(list[tuple[str, ...]] | None, cursor.description)
    except sqlite3.Error as error:
        print(f"buildlog query: {error}", file=sys.stderr)
        return 1
    finally:
        connection.close()
    names = [column[0] for column in description] if description else []
    finished = time.perf_counter()
    if as_json:
        print(json.dumps([dict(zip(names, row, strict=True)) for row in rows]))
    else:
        print_table(names, rows)
    print(
        f"{len(rows)} rows in {(finished - began) * 1000:.0f} ms"
        + f" (index update {(updated - began) * 1000:.0f} ms)",
        file=sys.stderr,
    )
    return 0


def schema() -> int:
    print(f"buildlog: records in {store.root()}, index {index.index_path()}")
    print("Times are UTC ISO strings; use date(x, 'localtime') or datetime(x, 'localtime') for local.")
    print(f"Step names: {STEP_NAMES}.")
    print()
    for table, (columns, _, meaning) in index.TABLES.items():
        print(f"TABLE {table} -- {meaning}")
        width = max(len(name) for name, _, _ in columns)
        for name, kind, about in columns:
            print(f"  {name.ljust(width)}  {kind.split()[0]:<7}  {about}")
        print()
    print("VIEWS")
    for view, (about, _) in index.VIEWS.items():
        print(f"  {view}: {about}")
    print()
    print("EXAMPLES")
    for question, sql in EXAMPLES:
        print(f"  -- {question}")
        print(f"  buildlog query \"{sql}\"")
    return 0


def day_report(args: list[str]) -> int:
    day = args[0] if args else date.today().isoformat()
    try:
        _ = date.fromisoformat(day)
    except ValueError:
        print("usage: buildlog report [YYYY-MM-DD]", file=sys.stderr)
        return 2
    _ = index.update()
    connection = index.read_only()
    try:
        print(report.report(connection, day))
    finally:
        connection.close()
    return 0


def hourly() -> int:
    status = 0
    for name, job in (("sync", sync.sync), ("ci", ci.ci)):
        try:
            status |= job()
        except Exception as error:  # noqa: BLE001 -- one job's failure must not cost the other its run
            print(f"buildlog {name}: {error}")
            store.note_error(f"hourly {name}")
            status = 1
    try:
        _ = index.update()
    except Exception as error:  # noqa: BLE001
        print(f"buildlog index: {error}")
        status = 1
    return status


def main(argv: list[str]) -> int:
    command, rest = (argv[0], argv[1:]) if argv else ("", [])
    if command == "query":
        return query(rest)
    if command == "schema":
        return schema()
    if command == "report":
        return day_report(rest)
    if command == "tree-key":
        key = treekey.tree_key(rest[0] if rest else os.getcwd())
        if key is None:
            return 1
        print(key)
        return 0
    if command == "reindex":
        lines = index.reindex()
        print(f"buildlog reindex: {lines} records indexed in {index.index_path()}")
        return 0
    if command == "sync":
        return sync.sync()
    if command == "ci":
        return ci.ci()
    if command == "sample":
        if rest:
            print("usage: buildlog sample", file=sys.stderr)
            return 2
        sample.sample()
        return 0
    if command == "hourly":
        return hourly()
    if command == "backfill-verify":
        return record.main(["backfill-verify", *rest])
    print(__doc__, file=sys.stderr)
    return 0 if command in ("-h", "--help", "help") else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
