#!/usr/bin/env python3
"""Tell a unit's showrunner when its agents spend their usage waiting, or a Codex worker stops compacting.

The collector runs this script after each good pass. By hand:
`python3 scripts/usage/waste_watch.py [--db PATH] [--production-docs DIR] [--dry-run] [--now ISO]`.
Only the default store sends; a run over any other store prints what it would send.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.usage import collect, outcomes, usage_report, weights
from scripts.usage.agents_log import state_dir
from scripts.usage.usage_report import UnitWorktree


WINDOW = timedelta(hours=2)
WAIT_SHARE_LIMIT = 0.10
# At the measured median of 2.1M weighted tokens per folder per 2 hours and about 19K weighted per
# request, 1M is about 50 requests. Below it a folder is mostly idle and a handful of polls sets the
# share; at 10% the waste at the minimum is at least 100K weighted tokens, about five requests, more
# than the showrunner turn the alert costs.
MINIMUM_WEIGHTED = 1_000_000
# Codex compacts workers at 200,000 tokens (codex_mesh.py AUTO_COMPACT_TOKENS), so five requests in
# a row above 250,000 mean compaction is not happening.
CONTEXT_LIMIT = 250_000
CONTEXT_RUN = 5
WORKER_ROLES = frozenset({"impl", "test", "fix", "review", "worker"})
ALERT_INTERVAL = timedelta(hours=6)
WORST_THREADS = 3
SENDER = "usage-waste-watch"
REPOSITORY = Path(__file__).resolve().parents[2]


def send_script() -> Path:
    return Path(os.environ.get("WASTE_WATCH_SEND") or REPOSITORY / "scripts/message/send.py")


def state_file() -> Path:
    return state_dir() / "waste_watch.json"


@dataclass(frozen=True)
class Thread:
    machine: str
    family: str
    parent_id: str
    agent_id: str

    def label(self) -> str:
        where = "" if self.machine == "natedev" else f" on {self.machine}"
        return f"{self.family} {self.agent_id[-8:]}{where}"


@dataclass(frozen=True)
class ContextRun:
    longest: int
    peak: int


@dataclass(frozen=True)
class ThreadUsage:
    thread: Thread
    folder: str | None
    role: str | None
    weighted: float
    wait_weighted: float
    wait_actions: dict[str, int]
    context_run: ContextRun | None

    def codex_worker(self) -> bool:
        return self.thread.family == "codex" and (self.role is None or self.role in WORKER_ROLES)

    def context_flagged(self) -> bool:
        return self.context_run is not None and self.codex_worker()


def row_thread(row: sqlite3.Row) -> Thread:
    return Thread(cast(str, row["machine"]), cast(str, row["family"]), cast(str, row["parent_id"]),
                  cast(str, row["agent_id"]))


def thread_usage(db: sqlite3.Connection, start: datetime, end: datetime) -> list[ThreadUsage]:
    """Group the window's requests by thread in SQL; Python sees one row per thread."""
    relation = weights.sql_weights("t.model", "t.at")
    prefix = f"""
        WITH weight_entries AS ({relation.entries}),
        window_turns AS MATERIALIZED (
            SELECT t.machine, t.family, t.parent_id, t.agent_id, t.at, t.turn_key, t.role, t.activity,
                   t.wait_action, t.input + t.cache_read AS context, {usage_report.weighted_sql('t')} AS weighted
            FROM turns t LEFT JOIN weight_entries w ON w.weight_entry = {relation.active_entry}
            WHERE t.at >= ? AND t.at < ?)
    """
    bounds = (start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat())
    totals = cast(Iterable[sqlite3.Row], db.execute(prefix + """
        , latest AS (SELECT machine, family, parent_id, agent_id, role FROM (
            SELECT *, row_number() OVER (PARTITION BY machine, family, parent_id, agent_id
                                         ORDER BY at DESC, turn_key DESC) AS position
            FROM window_turns) WHERE position = 1)
        SELECT g.machine, g.family, g.parent_id, g.agent_id, f.folder, l.role, g.weighted, g.wait_weighted
        FROM (SELECT machine, family, parent_id, agent_id, total(weighted) AS weighted,
                     total(CASE WHEN activity = 'wait' THEN weighted END) AS wait_weighted
              FROM window_turns GROUP BY machine, family, parent_id, agent_id) g
        JOIN latest l USING (machine, family, parent_id, agent_id)
        LEFT JOIN folders f USING (machine, family, parent_id, agent_id)
    """, bounds))
    actions: dict[Thread, dict[str, int]] = {}
    for row in cast(Iterable[sqlite3.Row], db.execute(prefix + """
        SELECT machine, family, parent_id, agent_id, wait_action, count(*) AS requests FROM window_turns
        WHERE activity = 'wait' GROUP BY machine, family, parent_id, agent_id, wait_action
    """, bounds)):
        thread = row_thread(row)
        actions.setdefault(thread, {})[cast(str, row["wait_action"])] = cast(int, row["requests"])
    runs: dict[Thread, ContextRun] = {}
    # Gaps and islands: requests above the limit that are consecutive share one position difference.
    for row in cast(Iterable[sqlite3.Row], db.execute(prefix + f"""
        , numbered AS (SELECT *, row_number() OVER (PARTITION BY machine, family, parent_id, agent_id
                                                    ORDER BY at, turn_key) AS position
                       FROM window_turns WHERE family = 'codex'),
        above AS (SELECT *, position - row_number() OVER (PARTITION BY machine, family, parent_id, agent_id
                                                          ORDER BY at, turn_key) AS island
                  FROM numbered WHERE context > {CONTEXT_LIMIT})
        SELECT machine, family, parent_id, agent_id, max(requests) AS longest, max(peak) AS peak FROM (
            SELECT machine, family, parent_id, agent_id, count(*) AS requests, max(context) AS peak
            FROM above GROUP BY machine, family, parent_id, agent_id, island HAVING count(*) >= {CONTEXT_RUN})
        GROUP BY machine, family, parent_id, agent_id
    """, bounds)):
        thread = row_thread(row)
        runs[thread] = ContextRun(cast(int, row["longest"]), cast(int, row["peak"]))
    found: list[ThreadUsage] = []
    for row in totals:
        thread = row_thread(row)
        found.append(ThreadUsage(thread, cast(str | None, row["folder"]), cast(str | None, row["role"]),
                                 cast(float, row["weighted"]), cast(float, row["wait_weighted"]),
                                 actions.get(thread, {}), runs.get(thread)))
    return found


@dataclass(frozen=True)
class Showrunner:
    slug: str
    # `uds:<socket>`, or empty while the showrunner is not running.
    address: str
    units: tuple[UnitWorktree, ...]


@dataclass(frozen=True)
class ShowrunnersFound:
    showrunners: tuple[Showrunner, ...]


@dataclass(frozen=True)
class ShowrunnerLookupFailed:
    reason: str


ShowrunnerLookup = ShowrunnersFound | ShowrunnerLookupFailed


def registered_showrunners() -> ShowrunnerLookup:
    """Every registered showrunner with its production doc's units, as the production tooling finds them."""
    from scripts.production import showrunners

    try:
        runners = showrunners.registered_showrunners()
    except OSError as error:
        return ShowrunnerLookupFailed(f"showrunner lookup failed: {' '.join(str(error).split())}")
    return ShowrunnersFound(tuple(
        Showrunner(runner["slug"], f"uds:{runner['socket']}" if runner["socket"] else "",
                   usage_report.document_units(Path(runner["doc"]))) for runner in runners))


def folder_unit(folder: str | None, worktrees: tuple[UnitWorktree, ...]) -> str:
    if folder is None:
        return "unknown"
    path = Path(folder)
    return next((row.unit for row in worktrees if path.is_relative_to(row.worktree)), path.name or "unknown")


@dataclass
class UnitUsage:
    unit: str
    threads: list[ThreadUsage] = field(default_factory=list)

    # Only Codex workers count toward the wait alert. A Claude session reads a background task's
    # output once after its completion notice, which the classifier labels wait but is the required
    # step, not a poll; Claude waiting stays visible in wait_share.
    @property
    def worker_threads(self) -> list[ThreadUsage]:
        return [thread for thread in self.threads if thread.codex_worker()]

    @property
    def weighted(self) -> float:
        return sum(thread.weighted for thread in self.worker_threads)

    @property
    def wait_weighted(self) -> float:
        return sum(thread.wait_weighted for thread in self.worker_threads)

    def wait_alert(self) -> bool:
        return self.weighted >= MINIMUM_WEIGHTED and self.wait_weighted > WAIT_SHARE_LIMIT * self.weighted

    def context_threads(self) -> list[ThreadUsage]:
        return [thread for thread in self.threads if thread.context_flagged()]


def thousands(value: float) -> str:
    return f"{value:,.0f}"


def alert_text(usage: UnitUsage, start: datetime) -> str:
    since = start.astimezone(timezone.utc).isoformat(timespec="seconds")
    flagged = usage.context_threads()
    lines: list[str] = []
    if usage.wait_alert():
        share = usage.wait_weighted / usage.weighted
        lines.append(f"Message from {SENDER}: {usage.unit} spent {share:.0%} of its weighted usage in the last "
                     + "2 hours on requests that only waited")
        lines.append(f"Window from {since}: {thousands(usage.weighted)} weighted tokens, "
                     + f"{thousands(usage.wait_weighted)} of them on waiting requests.")
        worst = sorted((thread for thread in usage.worker_threads if thread.wait_weighted > 0),
                       key=lambda thread: thread.wait_weighted, reverse=True)[:WORST_THREADS]
        lines.append("Threads waiting most:")
        lines.extend(f"- {thread.role or 'unassigned'} {thread.thread.label()}: "
                     + f"{thread.wait_weighted / thread.weighted:.0%} of {thousands(thread.weighted)} weighted"
                     for thread in worst)
        actions: Counter[str] = Counter()
        for thread in usage.worker_threads:
            actions.update(thread.wait_actions)
        if actions:
            action, requests = max(actions.items(), key=lambda item: (item[1], item[0]))
            lines.append(f"Most common waiting action: {action} ({requests} requests).")
    else:
        lines.append(f"Message from {SENDER}: {usage.unit} has {len(flagged)} Codex worker "
                     + f"thread{'s' if len(flagged) != 1 else ''} whose context stayed above "
                     + f"{thousands(CONTEXT_LIMIT)} tokens for {CONTEXT_RUN} or more requests in a row "
                     + "in the last 2 hours")
    if flagged:
        lines.append(f"Context above {thousands(CONTEXT_LIMIT)} tokens, so compaction is not happening:")
        for thread in flagged:
            run = cast(ContextRun, thread.context_run)
            lines.append(f"- {thread.role or 'unassigned'} {thread.thread.label()}: {run.longest} requests "
                         + f"in a row, peak {thousands(run.peak)} tokens")
    lines.append("See more: python3 ~/.claude/scripts/usage/usage_report.py wait_share --by unit,role "
                 + f"--since {since}")
    return "\n".join(lines)


def read_state(path: Path) -> dict[str, str]:
    try:
        data = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        print(f"waste_watch: alert times unreadable, starting empty: {' '.join(str(error).split())}",
              file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: value for key, value in cast(dict[object, object], data).items()
            if isinstance(key, str) and isinstance(value, str)}


def write_state(path: Path, alerts: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    _ = temporary.write_text(json.dumps(alerts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _ = temporary.replace(path)


def alerted_recently(alerts: dict[str, str], unit: str, now: datetime) -> bool:
    try:
        last = datetime.fromisoformat(alerts[unit])
    except (KeyError, ValueError):
        return False
    return now - last < ALERT_INTERVAL


@dataclass(frozen=True)
class Delivered:
    pass


@dataclass(frozen=True)
class NotSent:
    reason: str


def send(address: str, text: str) -> Delivered | NotSent:
    """send.py exit 0 is delivered and exit 1 is queued for the session; both count as delivered."""
    try:
        result = subprocess.run([sys.executable, str(send_script()), "--to", address, "--from", SENDER,
                                 "--text", text], capture_output=True, text=True, check=False, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as error:
        return NotSent(f"send.py failed: {' '.join(str(error).split())}")
    if result.returncode in (0, 1):
        return Delivered()
    detail = " ".join((result.stderr or result.stdout).split())
    return NotSent(f"send.py exited {result.returncode}" + (f": {detail}" if detail else ""))


def default_store(db: Path) -> bool:
    return db.absolute() == collect.store_path().absolute()


def watch(db: Path, production_docs: Path, *, now: datetime | None = None, dry_run: bool | None = None) -> None:
    """Alert each unit over a threshold in the last 2 hours, at most once per unit per 6 hours.

    `dry_run` None sends only for the default store; dry runs print, write no state and ignore the 6 hours.
    """
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    printing = dry_run if dry_run is not None else not default_store(db)
    start = moment - WINDOW
    with closing(usage_report.open_read_store(db)) as store:
        usage_report.check_store_version(store)
        threads = thread_usage(store, start, moment)
    # A unit alerts on waiting only above 10% of at least 1M, so less waiting in total cannot alert.
    if (sum(thread.wait_weighted for thread in threads if thread.codex_worker())
            <= WAIT_SHARE_LIMIT * MINIMUM_WEIGHTED
            and not any(thread.context_flagged() for thread in threads)):
        return
    lookup = registered_showrunners()
    runners = lookup.showrunners if isinstance(lookup, ShowrunnersFound) else ()
    worktrees = usage_report.longest_first([
        *(row for runner in runners for row in runner.units), *usage_report.unit_worktrees(production_docs)])
    units: dict[str, UnitUsage] = {}
    for thread in threads:
        unit = folder_unit(thread.folder, worktrees)
        units.setdefault(unit, UnitUsage(unit)).threads.append(thread)
    path = state_file()
    alerts = read_state(path)
    changed = False
    for usage in sorted(units.values(), key=lambda usage: usage.unit):
        if not usage.wait_alert() and not usage.context_threads():
            continue
        if not printing and alerted_recently(alerts, usage.unit, moment):
            continue
        text = alert_text(usage, start)
        listing = [runner for runner in runners if any(row.unit == usage.unit for row in runner.units)]
        # A running showrunner that lists the unit wins over one that is not running.
        runner = next((runner for runner in listing if runner.address), listing[0] if listing else None)
        if isinstance(lookup, ShowrunnerLookupFailed):
            reason = lookup.reason
        elif runner is None:
            reason = f"no registered showrunner lists unit {usage.unit}"
        elif not runner.address:
            reason = f"showrunner {runner.slug} is not running"
        else:
            reason = ""
        if printing:
            destination = f"stdout ({reason})" if reason or runner is None else runner.address
            print(f"waste_watch dry run: would send to {destination}:\n{text}")
            continue
        if reason or runner is None:
            print(f"waste_watch: {reason}; alert for the journal:\n{text}")
        else:
            sent = send(runner.address, text)
            if isinstance(sent, NotSent):
                print(f"waste_watch: could not tell {runner.slug}: {sent.reason}; alert for the journal:\n{text}")
                continue
        alerts[usage.unit] = moment.isoformat()
        changed = True
    if changed:
        write_state(path, alerts)


class WatchArguments(argparse.Namespace):
    db: Path | None
    production_docs: Path | None
    dry_run: bool
    now: str | None

    def __init__(self) -> None:
        super().__init__()
        self.db = None
        self.production_docs = None
        self.dry_run = False
        self.now = None


def main(argv: Sequence[str] = ()) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    _ = parser.add_argument("--db", type=Path, help="the store (default: the collector's)")
    _ = parser.add_argument("--production-docs", type=Path, help="directory of *-production.md docs")
    _ = parser.add_argument("--dry-run", action="store_true", help="print each alert and its destination; send nothing")
    _ = parser.add_argument("--now", help="end of the 2-hour window, ISO datetime (default: now)")
    args = parser.parse_args(argv, namespace=WatchArguments())
    try:
        now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else None
    except ValueError:
        parser.error(f"invalid --now {args.now!r}; accepted: ISO datetime")
    if now is not None and now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    db = args.db or collect.store_path()
    production_docs = args.production_docs or outcomes.default_sources().production_docs
    try:
        watch(db, production_docs, now=now, dry_run=True if args.dry_run else None)
    except (OSError, sqlite3.Error, usage_report.StoreVersionMismatch) as error:
        print("waste_watch: " + " ".join(str(error).split()), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
