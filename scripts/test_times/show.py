#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Show repository test times and gate module times for the current host."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections import defaultdict
from collections.abc import Iterator, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import sqlite3
import statistics
import sys
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from scripts.buildlog import index as index, parse, store, treekey
    from scripts.buildlog.mac_outcome import DURATION_NOT_RECORDED, DurationNotRecorded
else:
    from scripts.buildlog import index
    from scripts.buildlog import parse
    from scripts.buildlog import store
    from scripts.buildlog import treekey
    from scripts.buildlog.mac_outcome import DURATION_NOT_RECORDED, DurationNotRecorded

TOOL_REPOSITORY = Path(__file__).resolve().parents[2]
from scripts.lib import conf, file_lock
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.terminal_cells import print_table, seconds
from scripts.lib.user_zone import user_zone


def query(connection: sqlite3.Connection, sql: str,
          parameters: Sequence[object] = ()) -> list[dict[str, object]]:
    return list(query_rows(connection, sql, parameters))


def query_rows(connection: sqlite3.Connection, sql: str,
               parameters: Sequence[object] = ()) -> Iterator[dict[str, object]]:
    """Name the columns at the SQLite boundary."""
    cursor = connection.execute(sql, parameters)
    columns = cast(list[tuple[str, ...]], cursor.description)
    names = [column[0] for column in columns]
    try:
        for row in cast(Iterator[tuple[object, ...]], cursor):
            yield dict(zip(names, row, strict=True))
    finally:
        cursor.close()


@dataclass(frozen=True)
class ThresholdNotRecorded:
    """The legacy run does not record its test threshold."""


@dataclass(frozen=True)
class ModuleAttempt:
    run_id: str
    started_at: str
    suite: str
    module: str
    attempt: int
    restart: str
    times_missing: str
    wall_s: float
    cpu_s: float
    peak_mib: float
    status: int
    tests_run: int
    tests_s: float
    aborted: bool
    slow_test_s: float | ThresholdNotRecorded

    @classmethod
    def from_row(cls, row: dict[str, object]) -> ModuleAttempt:
        threshold = row["slow_test_s"]
        return cls(str(row["run_id"]), str(row["started_at"]), str(row["suite"]), str(row["module"]),
                   int(str(row["attempt"])), str(row["restart"]), str(row["times_missing"]),
                   float(str(row["wall_s"])), float(str(row["cpu_s"])), float(str(row["peak_mib"])),
                   int(str(row["status"])), int(str(row["tests_run"])), float(str(row["tests_s"])),
                   bool(row["aborted"]),
                   ThresholdNotRecorded() if threshold is None else float(str(threshold)))

    @property
    def key(self) -> tuple[str, str]:
        return self.suite, self.module

    @property
    def name(self) -> str:
        return f"{self.suite}/{self.module}"


MODULE_ATTEMPTS_QUERY = """
    SELECT m.*, r.aborted, r.slow_test_s FROM gate_modules m JOIN gate_runs r ON r.id = m.run_id
"""


@dataclass(frozen=True)
class KeptTest:
    test: str
    duration_s: float


def kept_tests(connection: sqlite3.Connection, attempt: ModuleAttempt) -> list[KeptTest]:
    return [KeptTest(str(row["test"]), float(str(row["duration_s"])))
            for row in query(connection, """
                SELECT test, duration_s FROM gate_tests
                WHERE run_id = ? AND suite = ? AND module = ? AND attempt = ?
                ORDER BY duration_s DESC, test
            """, (attempt.run_id, attempt.suite, attempt.module, attempt.attempt))]


def local_time(value: str) -> str:
    return datetime.fromisoformat(value).astimezone(user_zone()).strftime("%m-%d %H:%M")


def verdict(status: int) -> str:
    return "PASS" if status == 0 else "RED"


def run_mark(aborted: bool) -> str:
    return "aborted" if aborted else ""


def mebibytes(value: float) -> str:
    return f"{value:.3f}"


def top(connection: sqlite3.Connection, host: str, limit: int, repo: Path) -> None:
    from scripts.production.gate_modules import discover_test_modules, test_module_name
    from scripts.test_times.gate_record import TYPE_CHECK_JOB

    modules = {(suite, test_module_name(repo, suite, path))
               for suite, path in discover_test_modules(repo)}
    config = conf.read("test_times")
    count = config.whole("TOP_RUNS")
    checkout = Path(config.text("TOP_CHECKOUT")).expanduser().resolve()
    checkouts = [str(row["checkout"]) for row in query(connection,
                 "SELECT DISTINCT checkout FROM gate_runs WHERE host = ?", (host,))
                 if Path(str(row["checkout"])).expanduser().resolve() == checkout]
    checkout_parameters = ", ".join(["?"] * len(checkouts))
    oldest = cutoff(config.whole("TOP_MAX_AGE_DAYS"))
    histories: list[list[ModuleAttempt]] = []
    type_history: list[ModuleAttempt] = []
    for folder, filename in sorted(modules | {TYPE_CHECK_JOB}):
        history: list[ModuleAttempt] = []
        seen: set[str] = set()
        for row in query_rows(connection, MODULE_ATTEMPTS_QUERY + f"""
            WHERE m.suite = ? AND m.module = ? AND m.host = ? AND m.restart = ''
              AND m.status = 0 AND r.aborted = 0 AND r.checkout IN ({checkout_parameters})
              AND m.started_at >= ?
            ORDER BY m.started_at DESC, m.run_id DESC, m.attempt ASC
        """, (folder, filename, host, *checkouts, oldest)):
            attempt = ModuleAttempt.from_row(row)
            if attempt.run_id in seen:
                continue
            seen.add(attempt.run_id)
            history.append(attempt)
            if len(history) == count:
                break
        if (folder, filename) == TYPE_CHECK_JOB:
            type_history = history
        elif history:
            histories.append(history)
    unmeasured = [f"{folder}/{filename}"
                  for folder, filename in sorted(modules - {history[0].key for history in histories})]
    ranked = sorted(((statistics.median(attempt.cpu_s for attempt in history), history)
                     for history in histories), key=lambda item: (-item[0], item[1][0].name))[:limit]
    output: list[list[str]] = []
    for rank, (median, history) in enumerate(ranked, 1):
        last = history[0]
        tests_run = str(last.tests_run)
        slowest = ""
        if last.times_missing:
            tests_run = slowest = "times missing"
        else:
            tests = kept_tests(connection, last)
            if tests:
                slowest = f"{tests[0].test} {seconds(tests[0].duration_s)} s"
        output.append([str(rank), last.name, seconds(median),
                       seconds(statistics.median(attempt.wall_s for attempt in history)), seconds(last.wall_s),
                       tests_run, slowest, str(len(history))])
    print_table(["rank", "suite/module", "median CPU s", "median s", "last s", "tests run",
                 "slowest kept test", "runs counted"], output, "no modules recorded yet")
    if output:
        print(f"{len(output)} modules; sum of median CPU {seconds(sum(item[0] for item in ranked))} s")
    if type_history:
        last_type = type_history[0]
        print(f"{last_type.name} runs nightly, so it is not ranked: "
              + f"last run {seconds(last_type.cpu_s)} CPU s, {seconds(last_type.wall_s)} s")
    if unmeasured:
        print(f"unmeasured: {len(unmeasured)} live modules: {', '.join(unmeasured)}")


def cutoff(days: int) -> str:
    return store.utc_iso((datetime.now(UTC) - timedelta(days=days)).timestamp())


@dataclass
class NextestHistory:
    binary: str
    test: str
    passed_seconds: list[float] = field(default_factory=list)
    runs_kept: int = 0
    failures: int = 0
    last_seconds: float | DurationNotRecorded = DURATION_NOT_RECORDED

    @property
    def median_seconds(self) -> float | DurationNotRecorded:
        return statistics.median(self.passed_seconds) if self.passed_seconds else DURATION_NOT_RECORDED

    @property
    def rank(self) -> tuple[bool, float, str, str]:
        duration = self.median_seconds if self.passed_seconds else self.last_seconds
        value = duration if isinstance(duration, float) else 0.0
        return not self.passed_seconds, -value, self.binary, self.test


def nextest_top(connection: sqlite3.Connection, host: str, limit: int, repo: Path, identity: str) -> None:
    """Rank retained nextest results within one repository, host and age limit."""
    config = conf.read("test_times")
    threshold = parse.nextest_slow_test_s()
    histories: dict[tuple[str, str], NextestHistory] = {}
    for row in query_rows(connection, """
        SELECT t.binary, t.test, t.status, t.duration_s FROM steps s INDEXED BY steps_host_started
        JOIN tests t ON t.step_id = s.id
        WHERE s.repo_identity = ? AND s.host = ? AND s.started_at >= ?
        ORDER BY s.started_at
    """, (identity, host, cutoff(config.whole("TOP_MAX_AGE_DAYS")))):
        binary, name = str(row["binary"]), str(row["test"])
        key = binary, name
        if key not in histories:
            histories[key] = NextestHistory(binary, name)
        history = histories[key]
        history.runs_kept += 1
        status = str(row["status"])
        recorded_duration = row["duration_s"]
        duration = recorded_duration if isinstance(recorded_duration, float) else DURATION_NOT_RECORDED
        if status in ("passed", "flaky") and isinstance(duration, float):
            history.passed_seconds.append(duration)
        if status in ("failed", "timed_out"):
            history.failures += 1
        history.last_seconds = duration
    ranked = sorted(histories.values(), key=lambda history: history.rank)[:limit]
    print(f"repository: {repo.name} ({identity}); host: {host}; recorded threshold: > {seconds(threshold)} s")
    print_table(["rank", "binary", "test", "median s", "runs kept", "failed / timed out", "last s"], [
        [str(rank), history.binary, history.test,
         seconds(median) if isinstance(median := history.median_seconds, float) else "",
         str(history.runs_kept), str(history.failures),
         seconds(history.last_seconds) if isinstance(history.last_seconds, float) else ""]
        for rank, history in enumerate(ranked, 1)], "no nextest tests recorded yet")


def suites(connection: sqlite3.Connection, host: str, days: int) -> None:
    rows = query(connection, """
        SELECT r.*, (
            SELECT m.suite || '/' || m.module FROM gate_modules m
            WHERE m.run_id = r.id ORDER BY m.wall_s DESC, m.suite, m.module LIMIT 1
        ) AS slowest FROM gate_runs r
        WHERE r.host = ? AND r.started_at >= ? ORDER BY r.started_at DESC, r.id DESC
    """, (host, cutoff(days)))
    print_table(["start", "host", "selection", "modules run", "tests run", "wall s", "status", "slowest module", "run"], [
        [local_time(str(row["started_at"])), str(row["host"]), str(row["selection"]),
         str(row["modules_run"]), "" if row["tests_run"] is None else str(row["tests_run"]),
         seconds(float(str(row["wall_s"]))), verdict(int(str(row["status"]))),
         str(row["slowest"] or ""), run_mark(bool(row["aborted"]))] for row in rows])


def suite(connection: sqlite3.Connection, host: str, folder: str, days: int) -> None:
    rows = query(connection, """
        SELECT s.*, r.aborted FROM gate_suites s JOIN gate_runs r ON r.id = s.run_id
        WHERE s.host = ? AND s.suite = ? AND s.started_at >= ?
        ORDER BY s.started_at DESC, s.run_id DESC
    """, (host, folder, cutoff(days)))
    print_table(["start", "suite", "wall s", "status", "run"], [
        [local_time(str(row["started_at"])), str(row["suite"]), seconds(float(str(row["wall_s"]))),
         verdict(int(str(row["status"]))), run_mark(bool(row["aborted"]))] for row in rows],
        f"no gate runs recorded for suite {folder}")


def module(connection: sqlite3.Connection, host: str, name: str, runs: int) -> None:
    from scripts.test_times.gate_record import TYPE_CHECK_JOB

    folder, filename = name.split("/", 1)
    attempts = [ModuleAttempt.from_row(row) for row in query(connection, MODULE_ATTEMPTS_QUERY + """
        WHERE m.host = ? AND m.suite = ? AND m.module = ?
        ORDER BY m.started_at DESC, m.run_id DESC, m.attempt DESC LIMIT ?
    """, (host, folder, filename, runs))]
    output: list[list[str]] = []
    for attempt in attempts:
        if attempt.key == TYPE_CHECK_JOB:
            tests_run = tests_s = overhead = slowest = ""
        elif attempt.times_missing:
            tests_run = "times missing"
            tests_s = overhead = slowest = f"times missing: {attempt.times_missing}"
        else:
            tests_run = str(attempt.tests_run)
            tests_s = seconds(attempt.tests_s)
            overhead = seconds(attempt.wall_s - attempt.tests_s)
            slowest = "; ".join(f"{test.test} {seconds(test.duration_s)} s"
                                for test in kept_tests(connection, attempt)[:3])
        output.append([local_time(attempt.started_at), str(attempt.attempt) + (" hold" if attempt.restart else ""),
                       seconds(attempt.wall_s), seconds(attempt.cpu_s), mebibytes(attempt.peak_mib),
                       verdict(attempt.status), tests_run, tests_s, overhead, slowest,
                       run_mark(attempt.aborted)])
    print_table(["start", "attempt", "wall s", "CPU s", "peak MiB", "status", "tests run", "tests s",
                 "import and fixture s", "slowest kept tests", "run"], output,
                f"no gate runs recorded for module {name}")


def test(connection: sqlite3.Connection, host: str, text: str, runs: int) -> None:
    # instr treats the search text as a literal substring.
    identities: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
    for row in query(connection, """
        SELECT suite, module, test, min(started_at) AS first_at FROM gate_tests
        WHERE host = ? AND instr(test, ?) > 0 GROUP BY suite, module, test
    """, (host, text)):
        identities[str(row["suite"]), str(row["module"])][str(row["test"])] = str(row["first_at"])
    candidates: list[ModuleAttempt] = []
    for (folder, filename), names in identities.items():
        seen: set[str] = set()
        for row in query_rows(connection, MODULE_ATTEMPTS_QUERY + """
            WHERE m.suite = ? AND m.module = ? AND m.host = ? AND m.started_at >= ?
            ORDER BY m.started_at DESC, m.run_id DESC, m.attempt DESC
        """, (folder, filename, host, min(names.values()))):
            attempt = ModuleAttempt.from_row(row)
            if attempt.run_id not in seen and len(seen) == runs:
                break
            seen.add(attempt.run_id)
            candidates.append(attempt)
    newest = sorted({(attempt.started_at, attempt.run_id) for attempt in candidates}, reverse=True)[:runs]
    selected = {item[1] for item in newest}
    output: list[list[str]] = []
    for attempt in sorted(candidates, key=lambda item: (
            item.started_at, item.run_id, item.attempt), reverse=True):
        if attempt.run_id not in selected:
            continue
        kept = {str(row["test"]): row for row in query(connection, """
            SELECT test, duration_s, status FROM gate_tests
            WHERE run_id = ? AND suite = ? AND module = ? AND attempt = ?
        """, (attempt.run_id, attempt.suite, attempt.module, attempt.attempt))}
        for name, first_at in sorted(identities[attempt.key].items()):
            if attempt.started_at < first_at:
                continue
            duration = ""
            if attempt.times_missing:
                status = "times missing"
            elif name in kept:
                row = kept[name]
                duration, status = seconds(float(str(row["duration_s"]))), str(row["status"])
            elif isinstance(attempt.slow_test_s, ThresholdNotRecorded):
                status = "not kept (slow_test_s unknown)"
            else:
                status = f"not kept (faster than {seconds(attempt.slow_test_s)} s or did not run)"
            output.append([local_time(attempt.started_at), attempt.name, name, duration, status,
                           run_mark(attempt.aborted)])
    print_table(["start", "suite/module", "test", "duration s", "status", "run"],
                output, f"no tests recorded matching {text}")


TREND_CACHE_VERSION = 2
TREND_CACHE_NAME = "gate-trend.json"
TREND_HEADERS = ["day", "runs", "CPU mean", "CPU p95", "wall mean", "wall p95",
                 "cost mean", "cost p95", "cost total"]


@dataclass(frozen=True)
class CostNotRecorded:
    """The run has no confirmed lifetime and rate in the cost ledger."""


@dataclass(frozen=True)
class TrendRun:
    cpu_s: float
    wall_s: float
    modules: int
    cost: Decimal | CostNotRecorded


@dataclass(frozen=True)
class TrendDay:
    place: str
    selection: str
    cells: tuple[str, ...]
    uncosted_runs: int = 0


@dataclass
class TrendCache:
    zone: str
    completed_refresh_started_at: float = 0.0
    days: dict[str, list[TrendDay]] = field(default_factory=dict)


def read_trend_cache() -> TrendCache:
    zone = str(user_zone())
    try:
        document = cast(dict[str, object], json.loads((store.root() / TREND_CACHE_NAME).read_text()))
        if document["version"] != TREND_CACHE_VERSION or document["zone"] != zone:
            return TrendCache(zone)
        days = cast(dict[str, list[dict[str, object]]], document["days"])
        return TrendCache(zone, float(str(document["completed_refresh_started_at"])), {
            day: [TrendDay(str(row["place"]), str(row["selection"]),
                           tuple(cast(list[str], row["cells"])), int(str(row["uncosted_runs"]))) for row in rows]
            for day, rows in days.items()})
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return TrendCache(zone)


def write_trend_cache(cache: TrendCache) -> None:
    write_text_atomic(store.root() / TREND_CACHE_NAME, json.dumps({
        "version": TREND_CACHE_VERSION, "zone": cache.zone,
        "completed_refresh_started_at": cache.completed_refresh_started_at,
        "days": {day: [{"place": row.place, "selection": row.selection, "cells": row.cells, "uncosted_runs": row.uncosted_runs}
                       for row in rows] for day, rows in cache.days.items()},
    }, sort_keys=True) + "\n")


def refresh_trend_index() -> None:
    """Certify the start only after the index update completes."""
    started = datetime.now(UTC).timestamp()
    _ = index.update()
    with file_lock.exclusive(store.root() / (TREND_CACHE_NAME + ".lock")):
        if (store.root() / TREND_CACHE_NAME).exists():
            cache = read_trend_cache()
            cache.completed_refresh_started_at = max(cache.completed_refresh_started_at, started)
            write_trend_cache(cache)


def gate_costs() -> dict[str, Decimal | CostNotRecorded]:
    """Join reservations to runs and price confirmed lifetimes with applied rates."""
    from scripts.aws_target import ledger
    from scripts.build_targets import target_config

    try:
        evidence = ledger.CostLedger(target_config.ssh_target_state_directory("aws")).read_snapshot()
    except ledger.LedgerUnavailable:
        return {}
    return {"gate-" + row.attempt:
            row.rate.usd_per_hour * Decimal(row.state.at - row.at) / Decimal(3600) + row.allowance_usd
            if isinstance(row.state, ledger.GateTerminated) and row.rate.source != "not priced" else CostNotRecorded()
            for row in evidence.gate_reservations}


def nearest_rank(values: Sequence[float] | Sequence[Decimal]) -> float | Decimal:
    return sorted(values)[math.ceil(len(values) * 0.95) - 1]


def trend_day(connection: sqlite3.Connection, day: date,
              costs: dict[str, Decimal | CostNotRecorded]) -> list[TrendDay]:
    """Sum all module attempts before grouping the passing runs."""
    zone = user_zone()
    start = datetime.combine(day, datetime.min.time(), zone)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), zone)
    groups: dict[tuple[str, str], list[TrendRun]] = defaultdict(list)
    for row in query_rows(connection, """
        SELECT r.id, r.host, r.selection, r.wall_s, r.modules_run,
               (SELECT sum(m.cpu_s) FROM gate_modules m WHERE m.run_id = r.id) AS cpu_s
        FROM gate_runs r
        WHERE r.started_at >= ? AND r.started_at < ? AND r.status = 0
          AND coalesce(r.aborted, 0) = 0 AND r.modules_run > 0
          AND r.selection IN ('full', 'affected')
    """, (start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
          end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"))):
        if row["cpu_s"] is None:
            continue
        host = str(row["host"])
        place = "aws" if str(row["id"]) in costs or host.startswith("ip-") or "compute.internal" in host else host
        cost = costs.get(str(row["id"]), CostNotRecorded()) if place == "aws" else CostNotRecorded()
        groups[place, str(row["selection"])].append(TrendRun(
            float(str(row["cpu_s"])), float(str(row["wall_s"])), int(str(row["modules_run"])), cost))
    output: list[TrendDay] = []
    for (place, selection), runs in sorted(groups.items()):
        cpu = [run.cpu_s for run in runs]
        wall = [run.wall_s for run in runs]
        charged = [run.cost for run in runs if isinstance(run.cost, Decimal)]
        costs_text = ["—", "—", "—"]
        if charged:
            total = sum(charged, Decimal(0))
            costs_text = [f"${total / len(charged):.3f}", f"${nearest_rank(charged):.3f}", f"${total:.3f}"]
        cells = [day.isoformat(), str(len(runs)), f"{statistics.mean(cpu):.0f}", f"{nearest_rank(cpu):.0f}",
                 f"{statistics.mean(wall):.0f}", f"{nearest_rank(wall):.0f}", *costs_text]
        if selection == "affected":
            cells.append(f"{statistics.median(run.modules for run in runs):g}")
        output.append(TrendDay(place, selection, tuple(cells), len(runs) - len(charged) if place == "aws" else 0))
    return output


def trend(connection: sqlite3.Connection, days: int) -> None:
    """Read finished days from the cache and compute the other days."""
    today = datetime.now(user_zone()).date()
    settle = conf.read("test_times").number("TREND_SETTLE_MINUTES") * 60
    costs = gate_costs()
    rows: list[TrendDay] = []
    with file_lock.exclusive(store.root() / (TREND_CACHE_NAME + ".lock")):
        cache = read_trend_cache()
        for offset in range(days - 1, -1, -1):
            day = today - timedelta(days=offset)
            key = day.isoformat()
            if key in cache.days:
                rows.extend(cache.days[key])
                continue
            daily = trend_day(connection, day, costs)
            rows.extend(daily)
            end = datetime.combine(day + timedelta(days=1), datetime.min.time(), user_zone()).timestamp()
            if cache.completed_refresh_started_at >= end + settle:
                cache.days[key] = daily
        write_trend_cache(cache)
    groups: dict[tuple[str, str], list[TrendDay]] = defaultdict(list)
    for row in rows:
        groups[row.place, row.selection].append(row)
    if not groups:
        print("no gate runs recorded yet")
    for (place, selection), daily_rows in sorted(groups.items()):
        print(f"{place} {selection}")
        headers = TREND_HEADERS + (["median modules run"] if selection == "affected" else [])
        print_table(headers, [row.cells for row in daily_rows])
        uncosted = sum(row.uncosted_runs for row in daily_rows)
        if uncosted:
            print(f"{uncosted} of {sum(int(row.cells[1]) for row in daily_rows)} runs have no cost record.")


def positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("use a positive whole number")
    return number


ReportCommand = Literal["top", "suites", "suite", "module", "test", "trend"]


@dataclass(init=False)
class ReportRequestArguments(argparse.Namespace):
    command: ReportCommand
    limit: int
    repo: Path
    host: str
    days: int
    runs: int
    subject: str


def arguments(argv: list[str]) -> ReportRequestArguments:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    top_parser = subcommands.add_parser("top", help="rank gate modules by median CPU or nextest tests by median time")
    _ = top_parser.add_argument("--limit", type=positive, default=20)
    _ = top_parser.add_argument("--repo", type=Path, default=Path.cwd())
    _ = top_parser.add_argument("--host", default=store.host_name())
    trend_parser = subcommands.add_parser("trend", help="show daily gate CPU, wall time and cost")
    _ = trend_parser.add_argument("--days", type=positive, default=conf.read("test_times").whole("TREND_DAYS"))
    suites_parser = subcommands.add_parser("suites", help="show gate runs")
    _ = suites_parser.add_argument("--days", type=positive, default=7)
    suite_parser = subcommands.add_parser("suite", help="show one folder suite")
    _ = suite_parser.add_argument("subject", metavar="folder")
    _ = suite_parser.add_argument("--days", type=positive, default=7)
    module_parser = subcommands.add_parser("module", help="show each execution of one module")
    _ = module_parser.add_argument("subject", metavar="folder/file")
    _ = module_parser.add_argument("--runs", type=positive, default=20)
    test_parser = subcommands.add_parser("test", help="find kept test names by substring")
    _ = test_parser.add_argument("subject", metavar="text")
    _ = test_parser.add_argument("--runs", type=positive, default=20)
    if not argv or argv[0].startswith("--") and argv[0] != "--help":
        argv = ["top", *argv]
    request = parser.parse_args(argv, namespace=ReportRequestArguments())
    if request.command == "module" and ("/" not in request.subject or not all(request.subject.split("/", 1))):
        parser.error("module needs <folder>/<file>")
    return request


def open_report_index() -> sqlite3.Connection:
    """Read a complete index and build one only when none qualifies."""
    path = index.complete_gate_index_in(store.root())
    if isinstance(path, index.NoCompleteGateIndex):
        _ = index.update()
        path = index.index_path()
    connection = sqlite3.connect(index.read_only_path_uri(path), uri=True,
                                 timeout=index.BUSY_TIMEOUT_MS / 1000)
    _ = connection.execute("PRAGMA query_only = ON")
    try:
        connection.execute(f"SELECT 1 FROM {', '.join(index.GATE_TABLES)} LIMIT 0").close()
    except BaseException:
        connection.close()
        raise
    return connection


def rows_as_of(connection: sqlite3.Connection) -> None:
    """Show the time of the last row change in the user's zone."""
    completed = cast(tuple[str] | None, connection.execute(
        "SELECT value FROM meta WHERE key = 'updated_at'"
    ).fetchone())
    if completed is not None:
        zone = user_zone()
        moment = datetime.fromtimestamp(float(completed[0]), zone)
        stamp = moment.strftime("%H:%M %Z" if moment.date() == datetime.now(zone).date() else "%m-%d %H:%M %Z")
        print(f"rows as of {stamp}", file=sys.stderr)


def refresh_after_report() -> None:
    """Refresh in a detached child after the report releases its output."""
    _ = sys.stdout.flush()
    _ = sys.stderr.flush()
    if os.fork():
        return
    status = 1
    try:
        _ = os.setsid()
        descriptor = os.open(os.devnull, os.O_RDWR)
        for target in (0, 1, 2):
            _ = os.dup2(descriptor, target)
        if descriptor > 2:
            os.close(descriptor)
        refresh_trend_index()
        status = 0
    finally:
        os._exit(status)


def main(argv: list[str] | None = None) -> int:
    request = arguments(sys.argv[1:] if argv is None else argv)
    if request.command == "trend":
        store.root().mkdir(parents=True, exist_ok=True)
        with file_lock.exclusive(store.root() / (TREND_CACHE_NAME + ".lock")):
            cache = read_trend_cache()
            if not cache.completed_refresh_started_at:
                started = datetime.now(UTC).timestamp()
                _ = index.update()
                cache.completed_refresh_started_at = started
                write_trend_cache(cache)
    try:
        connection = open_report_index()
    except sqlite3.OperationalError as error:
        if not str(error).startswith("no such table: gate_"):
            raise
        print(f"test times unavailable: {error}; run `buildlog reindex`", file=sys.stderr)
        return 1
    with closing(connection):
        connection.execute("BEGIN").close()
        host = store.host_name()
        match request.command:
            case "top":
                from scripts.buildlog import repository

                repo = request.repo.expanduser().resolve()
                repo = Path(treekey.worktree(str(repo)) or str(repo))
                identity = repository.repository_key(str(repo))
                if identity == store.UNKNOWN_IDENTITY:
                    print(f"repository identity unavailable for {repo}", file=sys.stderr)
                    return 1
                if identity == repository.repository_key(str(TOOL_REPOSITORY)):
                    top(connection, request.host, request.limit, repo)
                else:
                    nextest_top(connection, request.host, request.limit, repo, identity)
            case "trend":
                trend(connection, request.days)
            case "suites":
                suites(connection, host, request.days)
            case "suite":
                suite(connection, host, request.subject, request.days)
            case "module":
                module(connection, host, request.subject, request.runs)
            case "test":
                test(connection, host, request.subject, request.runs)
        rows_as_of(connection)
    refresh_after_report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
