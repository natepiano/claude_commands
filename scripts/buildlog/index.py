"""The SQLite index over the build log's JSON lines.

<root>/index.sqlite is derived and disposable: the JSON lines are the record,
and `buildlog reindex` rebuilds this from nothing, as does a schema version
change. Every query first brings it up to date: for each <root>/*/*.jsonl it
reads only the complete lines past the offset it stopped at last time. sync
replaces a file by rename, so a file is known by its path, its first bytes and
the bytes just before its offset, never by its inode: a file that shrank, or
whose bytes there changed, is read again from the start, its old rows dropped
first. The first bytes hold the first record's id, so a file that started over
differs there even when its records end alike.

One updater at a time: an exclusive flock on <root>/index.lock, then BEGIN
IMMEDIATE with a busy timeout, so a reader never waits on a half-made index.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, cast
from urllib.parse import quote

import store

SCHEMA_VERSION = 23
# Step and call fields under a former name, renamed in the file on its next read.
FORMER_FIELDS = (("seat", "worker"), ("delegate_session", "unit_session"))
LOCK_NAME = "index.lock"
MARK_BYTES = 256
BUSY_TIMEOUT_MS = 30_000
# A Mac "no tests to run" (status 4) and a natedev nextest step stopped because
# a Mac copy of its call finished first keep their raw status without counting.
COUNTED_FAILURE = (
    "status != 0 AND NOT (caller IS 'verify-mac' AND step IS 'nextest' AND status = 4)"
    + " AND NOT (host IS 'natedev' AND step IS 'nextest'"
    + " AND call_id IN (SELECT id FROM calls WHERE copy = 'won'))"
)

Column = tuple[str, str, str]


class JobRunState(StrEnum):
    RAN = "ran"
    SKIPPED = "skipped"
    CARRIED_OVER = "carried_over"


STEP_COLUMNS: list[Column] = [
    ("id", "TEXT PRIMARY KEY", "host-UTC time-random"),
    ("src", "TEXT", "the JSON lines file the row came from, relative to the root"),
    ("host", "TEXT", "natedev, or the Mac's short host name"),
    ("started_at", "TEXT", "UTC ISO time; date(started_at,'localtime') gives the local day"),
    ("ended_at", "TEXT", "UTC ISO time"),
    ("duration_s", "REAL", "wall time of the step"),
    ("mem_wait_s", "INTEGER", "seconds the step waited for memory before it started"),
    ("slice", "TEXT", "builds when the scope ran, fallback when it failed, none when no scope was tried"),
    ("step", "TEXT", "clippy, mend, doc, fmt, nextest, check, sweep, or the program run"),
    ("argv", "TEXT", "the command as a JSON array"),
    ("cwd", "TEXT", "where it ran"),
    ("repo_path", "TEXT", "the repo every worktree shares (parent of the git common dir)"),
    ("repo", "TEXT", "repo_path's folder name: hana, cargo-port, ..."),
    ("worktree", "TEXT", "the worktree's top folder"),
    ("worktree_name", "TEXT", "worktree's folder name"),
    ("branch", "TEXT", "branch, or 'detached'"),
    ("sha", "TEXT", "short commit"),
    (
        "tree_key",
        "TEXT",
        "sha256 of the files the step saw (HEAD's tree plus every changed path's content, treekey.py);"
        + " NULL when they changed during the step or are unknown. `buildlog tree-key [dir]` prints a folder's",
    ),
    ("tree_changed", "INTEGER", "1 when the files changed during the step (fmt, mend --fix, an edit), 0 when not, NULL when unknown"),
    ("caller", "TEXT", "verify, cargo-port, validate_ci (push gate), brp-launch (build and app start), agent, alias, unknown"),
    ("worker", "TEXT", "worker role (UNIT_TEAM_ROLE)"),
    ("unit_session", "TEXT", "unit session folder name"),
    ("session", "TEXT", "Claude Code session id or Codex thread id"),
    ("call_id", "TEXT", "calls.id of the verify.sh call that ran this step"),
    ("tty", "INTEGER", "1 when a terminal watched it; then no log facts are recorded"),
    ("status", "INTEGER", "exit status; 128+n for signal n"),
    ("rustc", "TEXT", "rustc -V for the step's toolchain"),
    ("peak_mem_bytes", "INTEGER", "cgroup memory.peak of the step (page cache included); natedev only"),
    ("mem_stall_some_s", "REAL", "seconds at least one task waited on memory; natedev only"),
    ("mem_stall_full_s", "REAL", "seconds every task waited on memory; natedev only"),
    ("finished_s", "REAL", "cargo's own 'Finished ... in' build time (mend: its check time)"),
    ("crates_compiled", "INTEGER", "Compiling/Checking/Documenting lines"),
    ("warnings", "INTEGER", "distinct warning diagnostics"),
    ("errors", "INTEGER", "distinct error diagnostics"),
    ("tests_run", "INTEGER", "nextest Summary: tests run"),
    ("tests_passed", "INTEGER", "passed, flaky included"),
    ("tests_failed", "INTEGER", "failed, timed out included"),
    ("tests_skipped", "INTEGER", "skipped"),
    ("tests_flaky", "INTEGER", "passed on a retry"),
    ("tests_retried", "INTEGER", "tests that took more than one attempt"),
    ("mend_fixes", "INTEGER", "fixes 'mend: applied' reports; 0 when none were available"),
    ("mend_check_s", "REAL", "mend's cargo check time"),
    ("mend_s", "REAL", "mend's own analysis time"),
    ("sweep_freed_bytes", "INTEGER", "bytes lint sweep removed"),
    ("log", "TEXT", "gzipped output of a failed step, relative to the root"),
    ("test_build_key", "TEXT", "verify test Cargo build-input key, excluding nextest run-only selection; NULL for older or changed builds"),
]

TEST_COLUMNS: list[Column] = [
    ("step_id", "TEXT", "steps.id"),
    ("src", "TEXT", "source file"),
    ("host", "TEXT", "host"),
    ("started_at", "TEXT", "the step's start"),
    ("repo", "TEXT", "repo name"),
    ("branch", "TEXT", "branch"),
    ("binary", "TEXT", "nextest binary id: crate, crate::test_target, crate::bin/name"),
    ("test", "TEXT", "test path, tests::name"),
    ("status", "TEXT", "passed, failed, flaky (passed on a retry), timed_out"),
    ("attempts", "INTEGER", "attempts made"),
    ("duration_s", "REAL", "the last attempt's time"),
    ("slow", "INTEGER", "1 when nextest reported it SLOW"),
]

CALL_COLUMNS: list[Column] = [
    ("id", "TEXT PRIMARY KEY", "BUILDLOG_CALL_ID; steps.call_id points here"),
    ("src", "TEXT", "source file"),
    ("host", "TEXT", "host"),
    ("started_at", "TEXT", "UTC ISO"),
    ("ended_at", "TEXT", "UTC ISO"),
    ("tool", "TEXT", "verify.sh (an agent's call), port-lint (cargo-port's lint command)"),
    ("caller", "TEXT", "verify for verify.sh (NULL before 2026-10-02), cargo-port for port-lint"),
    ("command", "TEXT", "'lint hana', 'test hana_diegetic', 'port-lint clippy', ..."),
    ("verb", "TEXT", "verify.sh: check, test, lint, fmt, example, example-test, final; port-lint: clippy, mend, sweep"),
    ("package", "TEXT", "the package named"),
    (
        "outcome",
        "TEXT",
        "ran, failed, interrupted, reused (a recorded pass), replayed (a recorded failure),"
        + " deferred (port-lint: an agent or a cargo was busy in the worktree),"
        + " superseded (a newer call of the same verb from the same worker replaced it in the token queue)",
    ),
    ("status", "INTEGER", "exit status, NULL when unknown or interrupted"),
    ("cached", "INTEGER", "1 when the call could use a pass record (a worker session's test or lint)"),
    ("wait_s", "INTEGER", "seconds from script start until the run began"),
    ("token_wait_s", "INTEGER", "seconds spent acquiring the cargo token, a wait that timed out included; 0 when verify.sh sought none"),
    ("mac", "TEXT", "passed (natedev confirmed it; before 2026-10-16), passed_filter, passed_full (ended the call), failed, lost or declined when Mac offload ran or was considered"),
    ("mac_reason", "TEXT", "why Mac offload declined or had no usable result"),
    ("mac_holder", "TEXT", "busy decline: the claim what, leftovers or unreadable; NULL in old rows"),
    (
        "local_reason",
        "TEXT",
        "verify.sh test --local: why the test must run on Linux; empty otherwise",
    ),
    ("mac_s", "REAL", "seconds spent deciding or running on the Mac"),
    ("target", "TEXT", "where the call was when it ended: mac, natedev, or empty"),
    ("hold_wait_s", "REAL", "seconds waited on a held target, each declined Mac retry included; memory waits are not counted"),
    (
        "copy",
        "TEXT",
        "Mac copy of a still-compiling natedev test: won, natedev_won, gave_way, declined or lost (passed_full before 2026-10-16)"
        + " (the last copy's outcome); empty when no copy started, NULL in old rows",
    ),
    ("copy_s", "REAL", "seconds Mac copies of this call held the Mac, every copy summed"),
    ("copy_count", "INTEGER", "Mac copies started for this call"),
    ("copy_natedev_s", "REAL", "seconds the natedev nextest step had run (from its first cargo line) when the result was decided"),
    ("wall_s", "INTEGER", "seconds the run took"),
    ("build_s", "INTEGER", "cargo's own build seconds, NULL when unmeasured"),
    ("saved_s", "INTEGER", "seconds a reused or replayed record saved"),
    ("mem_kills", "INTEGER", "memory-killed attempts during this verify.sh call"),
    ("mem_kill_stopped", "INTEGER", "1 when a step was killed for memory twice and stopped this verify.sh call"),
    ("reuses", "TEXT", "port-lint: steps.id of the step a reused or replayed call stood in for"),
    ("reason", "TEXT", "port-lint: the line a deferred call printed, naming who was busy"),
    ("cwd", "TEXT", "where it ran"),
    ("repo_path", "TEXT", "repo every worktree shares"),
    ("repo", "TEXT", "repo name"),
    ("worktree", "TEXT", "worktree top folder"),
    ("worktree_name", "TEXT", "worktree folder name"),
    ("branch", "TEXT", "branch"),
    ("sha", "TEXT", "short commit"),
    ("worker", "TEXT", "worker role"),
    ("unit_session", "TEXT", "unit session folder name"),
    ("session", "TEXT", "Claude Code session or Codex thread"),
    ("backfilled", "INTEGER", "1 when copied from the old events.jsonl ledger"),
]

CI_RUN_COLUMNS: list[Column] = [
    ("run_id", "INTEGER", "GitHub workflow run id"),
    ("attempt", "INTEGER", "run attempt; (run_id, attempt) is the key"),
    ("src", "TEXT", "source file"),
    ("repo", "TEXT", "owner/name"),
    ("workflow", "TEXT", "workflow name"),
    ("event", "TEXT", "push, pull_request, ..."),
    ("branch", "TEXT", "head branch"),
    ("sha", "TEXT", "head commit"),
    ("status", "TEXT", "completed"),
    ("conclusion", "TEXT", "success, failure, cancelled, skipped"),
    ("created_at", "TEXT", "UTC ISO"),
    ("started_at", "TEXT", "UTC ISO"),
    ("updated_at", "TEXT", "UTC ISO"),
    ("duration_s", "REAL", "updated_at - started_at"),
    ("url", "TEXT", "run page"),
    ("jobs", "INTEGER", "jobs in the attempt"),
]

CI_JOB_COLUMNS: list[Column] = [
    ("job_id", "INTEGER PRIMARY KEY", "GitHub job id"),
    ("src", "TEXT", "source file"),
    ("run_id", "INTEGER", "ci_runs.run_id"),
    ("attempt", "INTEGER", "ci_runs.attempt"),
    ("workflow", "TEXT", "workflow name"),
    ("name", "TEXT", "job name"),
    ("branch", "TEXT", "head branch"),
    ("status", "TEXT", "completed"),
    ("conclusion", "TEXT", "success, failure, cancelled, skipped"),
    (
        "run_state",
        "TEXT",
        "ran, skipped, or carried_over; an attempt above 1 is carried over when started_at precedes created_at",
    ),
    ("created_at", "TEXT", "queued at, UTC ISO"),
    ("started_at", "TEXT", "UTC ISO"),
    ("completed_at", "TEXT", "UTC ISO"),
    ("duration_s", "REAL", "completed_at - started_at; NULL when skipped or carried over"),
    ("queued_s", "REAL", "started_at - created_at; NULL when skipped, carried over, or stamps give no queue time"),
    ("runner", "TEXT", "runner name"),
    ("labels", "TEXT", "runner labels, comma separated"),
]

CI_STEP_COLUMNS: list[Column] = [
    ("job_id", "INTEGER", "ci_jobs.job_id"),
    ("number", "INTEGER", "step number; (job_id, number) is the key"),
    ("src", "TEXT", "source file"),
    ("run_id", "INTEGER", "ci_runs.run_id"),
    ("name", "TEXT", "step name"),
    ("status", "TEXT", "completed"),
    ("conclusion", "TEXT", "success, failure, skipped"),
    ("started_at", "TEXT", "UTC ISO"),
    ("completed_at", "TEXT", "UTC ISO"),
    ("duration_s", "REAL", "completed_at - started_at"),
]

SAMPLE_COLUMNS: list[Column] = [
    ("src", "TEXT", "source file"),
    ("host", "TEXT", "machine's short host name"),
    ("at", "TEXT", "UTC ISO sample time"),
    ("boot_id", "TEXT", "Linux boot ID; changed ID marks a reboot"),
    ("mem_used_bytes", "INTEGER", "MemTotal minus MemAvailable"),
    ("swap_used_bytes", "INTEGER", "SwapTotal minus SwapFree"),
    ("builds_anon_bytes", "INTEGER", "anon memory in the user builds.slice; process memory"),
    ("sccache_in_service", "INTEGER", "1 when sccache.service has a process, 0 when empty, NULL without builds.slice"),
    ("ci_anon_bytes", "INTEGER", "anon memory in hana-ci.slice; process memory"),
    ("stall_some_us", "INTEGER", "machine memory some stall counter since boot, microseconds"),
    ("stall_full_us", "INTEGER", "machine memory full stall counter since boot, microseconds"),
    ("power_profile", "TEXT", "power-profiles-daemon profile; NULL when not recorded"),
    ("power_profile_since", "TEXT", "UTC ISO mtime of the daemon's state file: when the profile was set"),
]

MEMORY_SNAPSHOT_COLUMNS: list[Column] = [
    ("src", "TEXT", "source file"),
    ("host", "TEXT", "machine's short host name"),
    ("at", "TEXT", "UTC ISO snapshot time"),
    ("slices", "TEXT", "JSON cgroup events, peaks and limits by slice"),
    ("zram", "TEXT", "JSON zram storage at the snapshot time"),
]

HOLD_EVENT_COLUMNS: list[Column] = [
    ("src", "TEXT", "the source file; forgetting it removes this event only"),
    ("at", "TEXT", "UTC instant of the file change, taken under its lock"),
    ("host", "TEXT", "the machine that ran the command"),
    ("target", "TEXT", "mac or natedev"),
    ("event", "TEXT", "began or ended"),
    ("hold_key", "TEXT", "the hold identity on its host and target: holder file key or Mac block since"),
    ("holder", "TEXT", "the name to show"),
    ("reason", "TEXT", "the reason given when the hold began"),
    ("until", "TEXT", "the planned end; empty when none was given"),
]

HOLD_BUILD_COLUMNS: list[Column] = [
    ("action", "TEXT", "audit or stopped; old records read as audit"),
    ("src", "TEXT", "source file for this hold's audit rows"),
    ("id", "TEXT", "build identity from boot id, pid and process start time"),
    ("at", "TEXT", "UTC instant when the audit first recorded this build for this hold"),
    ("host", "TEXT", "machine that ran the build"),
    ("hold_key", "TEXT", "holder file key"),
    ("hold_since", "TEXT", "holder file since instant; a new hold gets new rows"),
    ("boot_id", "TEXT", "kernel boot id; process start times are relative to this boot"),
    ("pid", "INTEGER", "top cargo or nextest pid, or a compiler with no cargo ancestor"),
    ("proc_start", "TEXT", "process start time in clock ticks since boot; guards pid reuse"),
    ("class", "TEXT", "other_session or no_session; holder and admitted builds are not recorded"),
    ("session", "TEXT", "owning session id or ancestor identity; empty for no_session"),
    ("command", "TEXT", "build arguments as shell text"),
    ("cwd", "TEXT", "build working directory; empty when it cannot be read"),
]

MAC_IDLE_COLUMNS: list[Column] = [
    ("id", "TEXT PRIMARY KEY", "host, call and cause identity; one row per cause per call"),
    ("src", "TEXT", "source file"),
    ("host", "TEXT", "watch host"),
    ("at", "TEXT", "UTC instant when the watch acted"),
    ("call_id", "TEXT", "verify call; joins calls.id after the call ends"),
    ("pid", "INTEGER", "verify process id"),
    ("proc_start", "TEXT", "process start ticks; guards pid reuse"),
    ("session", "TEXT", "live call session"),
    ("repo", "TEXT", "call repository path"),
    ("package", "TEXT", "test package"),
    ("wait", "TEXT", "memory, build_hold, cargo_token or routing; backed by live wait evidence"),
    ("cause", "TEXT", "missing_temp_root, missing_leave_hook, retry_delayed, call_has_no_marker_hook or idle_with_waiter"),
    ("action", "TEXT", "created_session_temp_root, retry_marker, message_builds_unit or message_pending (send retried; row stays once)"),
]

QUEUE_BUMP_COLUMNS: list[Column] = [
    ("id", "TEXT PRIMARY KEY", "one queue move"),
    ("src", "TEXT", "source file"),
    ("host", "TEXT", "queue host"),
    ("at", "TEXT", "UTC instant of the move"),
    ("actor", "TEXT", "session or user who moved the waiter"),
    ("session", "TEXT", "moved session id"),
    ("step", "TEXT", "moved build step"),
    ("folder", "TEXT", "moved build folder"),
    ("old_position", "INTEGER", "waiting position before the move, starting at one"),
]

TABLES: dict[str, tuple[list[Column], str, str]] = {
    "queue_bumps": (QUEUE_BUMP_COLUMNS, "", "one row per user or session move to the head of the memory queue"),
    "mac_idle": (MAC_IDLE_COLUMNS, "", "one Mac idle case per cause per verify call; actions keep the call as the test owner"),
    "hold_builds": (HOLD_BUILD_COLUMNS, "", "one row per build, hold and action: audit or stopped"),
    "hold_events": (HOLD_EVENT_COLUMNS, "", "one row per began or ended event; current holds come from coordination files"),
    "steps": (STEP_COLUMNS, "", "one row per step invoke.sh's run() ran: clippy, mend, doc, fmt, nextest, check, sweep"),
    "tests": (
        TEST_COLUMNS,
        "PRIMARY KEY (step_id, binary, test)",
        "per-test rows of a nextest step, kept only for tests that failed, were retried, took over 1 s or hit SLOW",
    ),
    "calls": (
        CALL_COLUMNS,
        "",
        "one row per verify.sh call (its steps carry call_id), and per port-lint call that ran nothing",
    ),
    "ci_runs": (CI_RUN_COLUMNS, "PRIMARY KEY (run_id, attempt)", "GitHub Actions run attempts of natepiano/hana"),
    "ci_jobs": (CI_JOB_COLUMNS, "", "their jobs"),
    "ci_steps": (CI_STEP_COLUMNS, "PRIMARY KEY (job_id, number)", "the jobs' steps"),
    "samples": (SAMPLE_COLUMNS, "", "one 60 s machine memory sample; counters reset on reboot"),
    "memory_snapshots": (MEMORY_SNAPSHOT_COLUMNS, "", "slice and zram counters at a requested instant"),
}

VIEWS: dict[str, tuple[str, str]] = {
    "holds": (
        "one interval per began row; ends when the next row of its key is ended; a next began or no later row leaves ended_at NULL; an ended row without a preceding began has began_at NULL; previous_at and next_at bound missing events; later_row is 1 when a later row exists; no row proves a hold is in force now",
        """WITH ordered AS (
SELECT *, LAG(event) OVER hold_order AS previous_event,
       LAG(at) OVER hold_order AS previous_at,
       LEAD(event) OVER hold_order AS next_event,
       LEAD(at) OVER hold_order AS next_at
FROM hold_events
WINDOW hold_order AS (PARTITION BY host, target, hold_key ORDER BY at, CASE event WHEN 'began' THEN 0 ELSE 1 END)
)
SELECT host, target, hold_key, holder, reason, until,
       CASE WHEN event = 'began' THEN at END AS began_at,
       CASE WHEN event = 'ended' THEN at WHEN next_event = 'ended' THEN next_at END AS ended_at,
       CASE WHEN next_event IS NOT NULL THEN 1 ELSE 0 END AS later_row,
       previous_at, next_at
FROM ordered WHERE event = 'began' OR (event = 'ended' AND previous_event IS NOT 'began')""",
    ),
    "step_days": (
        "per local day, host, repo, step and caller: runs, failed, total_s, avg_s, max_s, avg_build_s, max_peak_gib",
        f"""SELECT date(started_at, 'localtime') AS day, host, repo, step, caller,
       count(*) AS runs, sum({COUNTED_FAILURE}) AS failed,
       round(sum(duration_s), 1) AS total_s, round(avg(duration_s), 1) AS avg_s,
       round(max(duration_s), 1) AS max_s, round(avg(finished_s), 1) AS avg_build_s,
       round(max(peak_mem_bytes) / 1073741824.0, 2) AS max_peak_gib
FROM steps GROUP BY day, host, repo, step, caller""",
    ),
    "call_outcomes": (
        "calls per local day, host, repo, tool, verb and outcome: calls, wall_s, saved_s",
        """SELECT date(started_at, 'localtime') AS day, host, repo, tool, verb, outcome,
       count(*) AS calls, sum(wall_s) AS wall_s, sum(saved_s) AS saved_s
FROM calls GROUP BY day, host, repo, tool, verb, outcome""",
    ),
    "slow_tests": (
        "tests over 1 s or reported SLOW, per repo and test: runs, avg_s, max_s, slow_runs, last_at",
        """SELECT repo, binary, test, count(*) AS runs, round(avg(duration_s), 2) AS avg_s,
       round(max(duration_s), 2) AS max_s, sum(slow) AS slow_runs, max(started_at) AS last_at
FROM tests WHERE duration_s > 1 OR slow GROUP BY repo, binary, test""",
    ),
    "flaky_tests": (
        "tests that were retried, or both passed and failed: runs, flaky, retried, passed, failed, last_at",
        """SELECT repo, binary, test, count(*) AS runs, sum(status = 'flaky') AS flaky,
       sum(attempts > 1) AS retried, sum(status IN ('passed', 'flaky')) AS passed,
       sum(status IN ('failed', 'timed_out')) AS failed, max(started_at) AS last_at
FROM tests GROUP BY repo, binary, test
HAVING retried > 0 OR (passed > 0 AND failed > 0)""",
    ),
    "failures": (
        "failed steps, newest last: local time, where, step, caller, status, errors, tests_failed, log (absolute), argv",
        f"""SELECT datetime(started_at, 'localtime') AS at, host, repo, worktree_name, branch, step, caller,
       status, errors, tests_failed,
       CASE WHEN log IS NULL THEN NULL ELSE (SELECT value FROM meta WHERE key = 'root') || '/' || log END AS log,
       argv, id
FROM steps WHERE {COUNTED_FAILURE}""",
    ),
    "ci_job_days": (
        "CI jobs per local day, workflow and job name: jobs, failed, avg_s, max_s, avg_queue_s, max_queue_s",
        """SELECT date(r.started_at, 'localtime') AS day, j.workflow, j.name, count(*) AS jobs,
       sum(j.conclusion = 'failure') AS failed, round(avg(j.duration_s)) AS avg_s, max(j.duration_s) AS max_s,
       round(avg(j.queued_s)) AS avg_queue_s, max(j.queued_s) AS max_queue_s
FROM ci_jobs AS j JOIN ci_runs AS r ON j.run_id = r.run_id AND j.attempt = r.attempt
WHERE j.run_state != 'carried_over' GROUP BY day, j.workflow, j.name""",
    ),
}


def index_path() -> Path:
    return store.root() / store.INDEX_NAME


def read_only_uri() -> str:
    return f"file:{quote(str(index_path()))}?mode=ro"


@contextmanager
def update_lock() -> Generator[None]:
    lock = store.root() / LOCK_NAME
    lock.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def create_schema(connection: sqlite3.Connection) -> None:
    statements = ["CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)"]
    statements.append("CREATE TABLE files (path TEXT PRIMARY KEY, offset INTEGER, marks TEXT)")
    for table, (columns, key, _) in TABLES.items():
        body = [f"{name} {kind}" for name, kind, _ in columns]
        if key:
            body.append(key)
        statements.append(f"CREATE TABLE {table} ({', '.join(body)})")
        statements.append(f"CREATE INDEX {table}_src ON {table} (src)")
    statements.append("CREATE INDEX hold_events_key_at ON hold_events (host, target, hold_key, at, event)")
    statements.append("CREATE INDEX hold_events_target_at ON hold_events (target, at)")
    statements.append("CREATE INDEX steps_started ON steps (started_at)")
    statements.append("CREATE INDEX steps_host_started ON steps (host, started_at)")
    statements.append("CREATE INDEX steps_tree ON steps (worktree, step, tree_key)")
    statements.append("CREATE INDEX steps_call ON steps (call_id)")
    statements.append("CREATE INDEX tests_step ON tests (step_id)")
    statements.append("CREATE INDEX calls_started ON calls (started_at)")
    statements.append("CREATE INDEX ci_jobs_run ON ci_jobs (run_id, attempt)")
    statements.append("CREATE INDEX samples_host_at ON samples (host, at)")
    statements.append("CREATE INDEX memory_snapshots_at ON memory_snapshots (at)")
    for view, (_, select) in VIEWS.items():
        statements.append(f"CREATE VIEW {view} AS {select}")
    for statement in statements:
        _ = connection.execute(statement)
    _ = connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def open_for_update() -> sqlite3.Connection:
    """The index at the current schema; any other version is deleted and made again."""
    path = index_path()
    connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    version = cast(tuple[int], connection.execute("PRAGMA user_version").fetchone())[0]
    if version != SCHEMA_VERSION:
        connection.close()
        path.unlink(missing_ok=True)
        connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        _ = connection.execute("BEGIN IMMEDIATE")
        create_schema(connection)
        _ = connection.execute("COMMIT")
    _ = connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return connection


def elapsed_seconds(start: object, end: object) -> float | None:
    if not isinstance(start, str) or not isinstance(end, str) or not start or not end:
        return None
    try:
        first = datetime.fromisoformat(start.replace("Z", "+00:00"))
        last = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (last - first).total_seconds()


def seconds_between(start: object, end: object) -> float | None:
    elapsed = elapsed_seconds(start, end)
    return round(elapsed, 3) if elapsed is not None else None


def folder_name(path: object) -> str | None:
    return Path(path).name if isinstance(path, str) and path else None


def insert(connection: sqlite3.Connection, table: str, row: dict[str, object]) -> None:
    names = list(row)
    marks = ", ".join("?" for _ in names)
    _ = connection.execute(
        f"INSERT OR REPLACE INTO {table} ({', '.join(names)}) VALUES ({marks})", [row[name] for name in names]
    )


def pick(record: dict[str, object], columns: list[Column]) -> dict[str, object]:
    row: dict[str, object] = {}
    for name, _, _ in columns:
        value = record.get(name)
        row[name] = json.dumps(value) if isinstance(value, list | dict) else value
    return row


def add_step(connection: sqlite3.Connection, record: dict[str, object], src: str) -> None:
    record = {
        **record,
        "src": src,
        "repo": folder_name(record.get("repo_path")),
        "worktree_name": folder_name(record.get("worktree")),
    }
    insert(connection, "steps", pick(record, STEP_COLUMNS))
    _ = connection.execute("DELETE FROM tests WHERE step_id = ?", (record.get("id"),))
    tests = record.get("tests")
    if not isinstance(tests, list):
        return
    for test in cast(list[object], tests):
        if not isinstance(test, dict):
            continue
        row = cast(dict[str, object], test)
        insert(
            connection,
            "tests",
            pick(
                {
                    **row,
                    "step_id": record.get("id"),
                    "src": src,
                    "host": record.get("host"),
                    "started_at": record.get("started_at"),
                    "repo": record.get("repo"),
                    "branch": record.get("branch"),
                },
                TEST_COLUMNS,
            ),
        )


def add_call(connection: sqlite3.Connection, record: dict[str, object], src: str) -> None:
    record = {
        **record,
        "token_wait_s": record.get("token_wait_s", 0),
        "src": src,
        "repo": folder_name(record.get("repo_path")),
        "worktree_name": folder_name(record.get("worktree")),
    }
    insert(connection, "calls", pick(record, CALL_COLUMNS))


def add_sample(connection: sqlite3.Connection, record: dict[str, object], src: str) -> None:
    insert(connection, "samples", pick({**record, "src": src}, SAMPLE_COLUMNS))


def add_memory_snapshot(connection: sqlite3.Connection, record: dict[str, object], src: str) -> None:
    insert(connection, "memory_snapshots", pick({**record, "src": src}, MEMORY_SNAPSHOT_COLUMNS))


def add_ci_run(connection: sqlite3.Connection, record: dict[str, object], src: str) -> None:
    jobs = record.get("jobs")
    job_list = cast(list[object], jobs) if isinstance(jobs, list) else []
    run = {
        **record,
        "src": src,
        "duration_s": seconds_between(record.get("started_at"), record.get("updated_at")),
        "jobs": len(job_list),
    }
    insert(connection, "ci_runs", pick(run, CI_RUN_COLUMNS))
    for item in job_list:
        if not isinstance(item, dict):
            continue
        job = cast(dict[str, object], item)
        labels = job.get("labels")
        label_text = ",".join(str(label) for label in cast(list[object], labels)) if isinstance(labels, list) else None
        attempt = record.get("attempt")
        # On a re-run, GitHub copies each finished job under a new job_id, with
        # created_at at the re-run and started_at/completed_at from the earlier attempt.
        queue_elapsed = elapsed_seconds(job.get("created_at"), job.get("started_at"))
        carried_over = isinstance(attempt, int) and attempt > 1 and queue_elapsed is not None and queue_elapsed < 0
        if carried_over:
            run_state = JobRunState.CARRIED_OVER
        elif job.get("conclusion") == "skipped":
            run_state = JobRunState.SKIPPED
        else:
            run_state = JobRunState.RAN
        job_row = {
            **job,
            "src": src,
            "run_id": record.get("run_id"),
            "attempt": record.get("attempt"),
            "workflow": record.get("workflow"),
            "branch": record.get("branch"),
            "run_state": run_state,
            "labels": label_text,
        }
        if run_state is JobRunState.RAN:
            job_row["duration_s"] = seconds_between(job.get("started_at"), job.get("completed_at"))
            if queue_elapsed is not None and queue_elapsed >= 0:
                job_row["queued_s"] = round(queue_elapsed, 3)
        insert(connection, "ci_jobs", pick(job_row, CI_JOB_COLUMNS))
        steps = job.get("steps")
        for entry in cast(list[object], steps) if isinstance(steps, list) else []:
            if not isinstance(entry, dict):
                continue
            ci_step = cast(dict[str, object], entry)
            step_row = {
                **ci_step,
                "job_id": job.get("job_id"),
                "src": src,
                "run_id": record.get("run_id"),
                "duration_s": seconds_between(ci_step.get("started_at"), ci_step.get("completed_at")),
            }
            insert(connection, "ci_steps", pick(step_row, CI_STEP_COLUMNS))


def add_hold_event(connection: sqlite3.Connection, record: dict[str, object], src: str) -> None:
    insert(connection, "hold_events", pick({**record, "src": src}, HOLD_EVENT_COLUMNS))


def add_hold_build(connection: sqlite3.Connection, record: dict[str, object], src: str) -> None:
    insert(connection, "hold_builds", pick({"action": "audit", **record, "src": src}, HOLD_BUILD_COLUMNS))


def add_mac_idle(connection: sqlite3.Connection, record: dict[str, object], src: str) -> None:
    insert(connection, "mac_idle", pick({**record, "src": src}, MAC_IDLE_COLUMNS))


def add_queue_bump(connection: sqlite3.Connection, record: dict[str, object], src: str) -> None:
    insert(connection, "queue_bumps", pick({**record, "src": src}, QUEUE_BUMP_COLUMNS))


ADDERS = {"queue_bump": add_queue_bump, "mac_idle": add_mac_idle, "hold_build": add_hold_build, "hold_event": add_hold_event, "step": add_step, "call": add_call, "ci_run": add_ci_run, "sample": add_sample,
          "memory_snapshot": add_memory_snapshot}


def forget(connection: sqlite3.Connection, src: str) -> None:
    for table in TABLES:
        _ = connection.execute(f"DELETE FROM {table} WHERE src = ?", (src,))
    _ = connection.execute("DELETE FROM files WHERE path = ?", (src,))


def marks(handle: BinaryIO, offset: int) -> str:
    """The first MARK_BYTES of the file and the last before offset, which an append leaves alone."""
    _ = handle.seek(0)
    head = handle.read(min(offset, MARK_BYTES))
    start = max(0, offset - MARK_BYTES)
    _ = handle.seek(start)
    return head.hex() + ":" + handle.read(offset - start).hex()


def migrate_worker_fields(path: Path) -> None:
    """Rename old step/call fields once under the same lock as appenders."""
    with path.open("r+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        data = handle.read()
        end = data.rfind(b"\n") + 1
        lines: list[bytes] = []
        changed = False
        for raw in data[:end].splitlines(keepends=True):
            try:
                value = cast(object, json.loads(raw))
            except ValueError:
                lines.append(raw)
                continue
            if isinstance(value, dict):
                fields = cast(dict[str, object], value)
                renamed = [(old, new) for old, new in FORMER_FIELDS if old in fields]
                if fields.get("kind") in ("step", "call") and renamed:
                    for old, new in renamed:
                        _ = fields.setdefault(new, fields[old])
                        del fields[old]
                    raw = (json.dumps(fields, ensure_ascii=False) + "\n").encode()
                    changed = True
            lines.append(raw)
        if changed:
            _ = handle.seek(0)
            _ = handle.write(b"".join(lines) + data[end:])
            _ = handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())


def read_file(connection: sqlite3.Connection, path: Path, src: str, known: tuple[int, str] | None) -> int:
    """Index the complete lines past the stored offset; returns the lines read."""
    migrate_worker_fields(path)
    with path.open("rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        offset = 0
        if known is not None:
            offset, stored = known
            if offset > size or marks(handle, offset) != stored:
                forget(connection, src)
                offset = 0
        if offset == size and known is not None:
            return 0
        _ = handle.seek(offset)
        data = handle.read(size - offset)
        end = data.rfind(b"\n") + 1
        complete = data[:end]
        lines = 0
        for raw in complete.splitlines():
            if not raw.strip():
                continue
            try:
                record = cast(object, json.loads(raw))
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            typed = cast(dict[str, object], record)
            adder = ADDERS.get(str(typed.get("kind")))
            if adder is not None:
                adder(connection, typed, src)
                lines += 1
        new_offset = offset + end
        stored = marks(handle, new_offset)
    _ = connection.execute(
        "INSERT OR REPLACE INTO files (path, offset, marks) VALUES (?, ?, ?)", (src, new_offset, stored)
    )
    return lines


def update() -> int:
    """Bring the index up to date with the JSON lines; returns the lines read."""
    root = store.root()
    root.mkdir(parents=True, exist_ok=True)
    with update_lock():
        connection = open_for_update()
        try:
            _ = connection.execute("BEGIN IMMEDIATE")
            known = {
                path: (offset, stored)
                for path, offset, stored in cast(
                    list[tuple[str, int, str]], connection.execute("SELECT path, offset, marks FROM files").fetchall()
                )
            }
            _ = connection.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('root', ?)", (str(root),))
            lines = 0
            present: set[str] = set()
            for path in sorted(root.glob("*/*.jsonl")):
                src = path.relative_to(root).as_posix()
                present.add(src)
                lines += read_file(connection, path, src, known.get(src))
            for gone in set(known) - present:
                forget(connection, gone)
            _ = connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                _ = connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
    return lines


def reindex() -> int:
    with update_lock():
        index_path().unlink(missing_ok=True)
    return update()


def read_only() -> sqlite3.Connection:
    connection = sqlite3.connect(read_only_uri(), uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
    _ = connection.execute("PRAGMA query_only = ON")
    return connection
