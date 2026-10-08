#!/usr/bin/env python3
"""Tests for index.py: incremental reads of the JSON lines, rebuilds, views, and `buildlog query`."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import cast, override

import index
import parse

CLI = Path(__file__).with_name("cli.py")
TESTDATA = Path(__file__).with_name("testdata")
STAMP = "2026-10-02T12:00:00.000Z"
Record = dict[str, object]
_TEST_LOG = tempfile.TemporaryDirectory(prefix="buildlog-tests-")
_TEST_ROOT = Path(_TEST_LOG.name) / "buildlog"


def use_test_log() -> None:
    os.environ["BUILDLOG_DIR"] = str(_TEST_ROOT)
    os.environ["BUILDLOG_TRANSCRIPTS"] = str(Path(_TEST_LOG.name) / "transcripts")


use_test_log()


def local_day(stamp: str) -> str:
    return datetime.fromisoformat(stamp).astimezone().date().isoformat()


def step(record_id: str, **fields: object) -> Record:
    record: Record = {
        "kind": "step",
        "v": 1,
        "id": record_id,
        "host": "natedev",
        "started_at": STAMP,
        "ended_at": STAMP,
        "duration_s": 10.0,
        "step": "clippy",
        "argv": ["cargo", "clippy", "--workspace"],
        "cwd": "/r/feature",
        "repo_path": "/r/hana",
        "worktree": "/r/feature",
        "branch": "feature",
        "sha": "abc1234",
        "caller": "agent",
        "status": 0,
        **parse.no_facts(),
        "log": None,
    }
    record.update(fields)
    return record


def call(record_id: str, **fields: object) -> Record:
    record: Record = {
        "kind": "call",
        "v": 1,
        "id": record_id,
        "host": "natedev",
        "started_at": STAMP,
        "ended_at": STAMP,
        "tool": "verify.sh",
        "command": "test hana",
        "verb": "test",
        "package": "hana",
        "outcome": "ran",
        "status": 0,
        "cached": True,
        "wait_s": 0,
        "wall_s": 0,
        "build_s": None,
        "saved_s": 0,
        "repo_path": "/r/hana",
        "worktree": "/r/feature",
        "branch": "feature",
        "backfilled": False,
    }
    record.update(fields)
    return record


def sample(at: str, **fields: object) -> Record:
    record: Record = {
        "kind": "sample",
        "v": 1,
        "host": "natedev",
        "at": at,
        "boot_id": "boot-a",
        "mem_used_bytes": 4 * 2**30,
        "swap_used_bytes": 2 * 2**30,
        "stall_some_us": 1_000_000,
        "stall_full_us": 200_000,
    }
    record.update(fields)
    return record


def ci_job(job_id: int, name: str, conclusion: str, times: tuple[str, str, str]) -> Record:
    created, started, completed = times
    return {
        "job_id": job_id,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "created_at": created,
        "started_at": started,
        "completed_at": completed,
        "runner": "hana-linux-1",
        "labels": ["hana-linux"],
        "steps": [
            {
                "number": 1,
                "name": "Set up job",
                "status": "completed",
                "conclusion": conclusion,
                "started_at": started,
                "completed_at": completed,
            }
        ],
    }


def ci_run(run_id: int, attempt: int, jobs: list[Record]) -> Record:
    return {
        "kind": "ci_run",
        "v": 1,
        "repo": "natepiano/hana",
        "run_id": run_id,
        "attempt": attempt,
        "workflow": "CI",
        "event": "push",
        "branch": "main",
        "sha": "0" * 40,
        "status": "completed",
        "conclusion": "success",
        "created_at": "2026-10-02T12:00:00Z",
        "started_at": "2026-10-02T12:00:00Z",
        "updated_at": "2026-10-02T12:10:00Z",
        "url": f"https://github.com/natepiano/hana/actions/runs/{run_id}",
        "jobs": jobs,
    }


def point_root_at(test: unittest.TestCase, root: Path) -> None:
    """Override the suite's temporary log root for this test only."""
    previous = os.environ.get("BUILDLOG_DIR")
    os.environ["BUILDLOG_DIR"] = str(root)

    def restore() -> None:
        if previous is None:
            _ = os.environ.pop("BUILDLOG_DIR", None)
        else:
            os.environ["BUILDLOG_DIR"] = previous

    test.addCleanup(restore)


class LogIsolationTests(unittest.TestCase):
    def test_each_module_uses_a_temporary_log(self) -> None:
        environment = {key: value for key, value in os.environ.items() if key != "BUILDLOG_DIR"}
        real_root = Path.home() / ".local/state/buildlog"
        for module in sorted(Path(__file__).parent.glob("test_*.py")):
            with self.subTest(module=module.name):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "import importlib, store, sys; importlib.import_module(sys.argv[1]); print(store.root())",
                        module.stem,
                    ],
                    cwd=module.parent,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotEqual(Path(result.stdout.strip()), real_root, module.name)


def encode(record: Record) -> bytes:
    return (json.dumps(record, separators=(",", ":")) + "\n").encode()


class IndexTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    host_file: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temporary) / "buildlog"
        point_root_at(self, self.root)
        self.host_file = self.root / "natedev" / "2026-10.jsonl"
        self.host_file.parent.mkdir(parents=True)

    def write(self, path: Path, *records: Record) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_bytes(b"".join(encode(record) for record in records))

    def rows(self, sql: str, *parameters: object) -> list[tuple[object, ...]]:
        with closing(index.read_only()) as connection:
            return cast(list[tuple[object, ...]], connection.execute(sql, parameters).fetchall())

    def ids(self) -> list[object]:
        return [row[0] for row in self.rows("SELECT id FROM steps ORDER BY id")]

    def test_reads_only_new_complete_lines(self) -> None:
        first, second, third = (encode(step(f"s{number}")) for number in range(3))
        _ = self.host_file.write_bytes(first + second + third[:20])
        self.assertEqual(index.update(), 2)
        self.assertEqual(self.ids(), ["s0", "s1"])
        self.assertEqual(index.update(), 0)
        with self.host_file.open("ab") as handle:
            _ = handle.write(third[20:])
        self.assertEqual(index.update(), 1)
        self.assertEqual(self.ids(), ["s0", "s1", "s2"])

    def test_file_replaced_by_rename_continues(self) -> None:
        self.write(self.host_file, step("s0"), step("s1"))
        self.assertEqual(index.update(), 2)
        partial = self.host_file.with_name("incoming.tmp")
        self.write(partial, step("s0"), step("s1"), step("s2"))
        _ = partial.replace(self.host_file)
        self.assertEqual(index.update(), 1)
        self.assertEqual(self.ids(), ["s0", "s1", "s2"])

    def test_shrunk_file_is_read_again(self) -> None:
        nextest = parse.log_facts("nextest", (TESTDATA / "nextest.txt").read_text())
        self.write(self.host_file, step("s0", **nextest), step("s1"), step("s2"))
        _ = index.update()
        self.assertEqual(len(self.rows("SELECT * FROM tests")), 3)
        self.write(self.host_file, step("s3"))
        self.assertEqual(index.update(), 1)
        self.assertEqual(self.ids(), ["s3"])
        self.assertEqual(self.rows("SELECT * FROM tests"), [])

    def test_rewritten_file_is_read_again(self) -> None:
        self.write(self.host_file, step("s0"), step("s1"))
        _ = index.update()
        self.write(self.host_file, step("s5", status=1), step("s6", status=1), step("s7", status=1))
        self.assertEqual(index.update(), 3)
        self.assertEqual(self.ids(), ["s5", "s6", "s7"])

    def test_changed_last_record_is_read_again(self) -> None:
        self.write(self.host_file, step("s0"), step("s1"))
        _ = index.update()
        self.write(self.host_file, step("s0"), step("s9", log="natedev/logs/2026-10/s9.log.gz"))
        self.assertEqual(index.update(), 2)
        self.assertEqual(self.ids(), ["s0", "s9"])

    def test_deleted_file_rows_go(self) -> None:
        mac_file = self.root / "mac" / "2026-10.jsonl"
        nextest = parse.log_facts("nextest", (TESTDATA / "nextest.txt").read_text())
        self.write(self.host_file, step("s0"))
        self.write(mac_file, step("m0", host="mac", **nextest), call("c0", host="mac"))
        self.assertEqual(index.update(), 3)
        mac_file.unlink()
        self.assertEqual(index.update(), 0)
        self.assertEqual(self.ids(), ["s0"])
        self.assertEqual(self.rows("SELECT * FROM tests"), [])
        self.assertEqual(self.rows("SELECT * FROM calls"), [])
        self.assertEqual(self.rows("SELECT path FROM files"), [("natedev/2026-10.jsonl",)])

    def test_schema_version_mismatch_rebuilds(self) -> None:
        self.write(self.host_file, step("s0"), step("s1"))
        _ = index.update()
        with closing(sqlite3.connect(index.index_path())) as connection:
            _ = connection.execute("CREATE TABLE leftover (x)")
            _ = connection.execute(f"PRAGMA user_version = {index.SCHEMA_VERSION + 1}")
        self.assertEqual(index.update(), 2)
        self.assertEqual(self.rows("PRAGMA user_version"), [(index.SCHEMA_VERSION,)])
        self.assertEqual(self.rows("SELECT name FROM sqlite_master WHERE name = 'leftover'"), [])
        self.assertEqual(self.ids(), ["s0", "s1"])

    def test_schema_five_rebuilds_with_memory_admission_columns(self) -> None:
        self.write(self.host_file, step("s0", mem_wait_s=12))
        _ = index.update()
        with closing(sqlite3.connect(index.index_path())) as connection:
            _ = connection.execute("PRAGMA user_version = 5")
        self.assertEqual(index.update(), 1)
        self.assertEqual(self.rows("PRAGMA user_version"), [(index.SCHEMA_VERSION,)])
        self.assertEqual(self.rows("SELECT mem_wait_s FROM steps"), [(12,)])

    def test_memory_stall_columns_and_samples_ingest(self) -> None:
        self.assertGreater(index.SCHEMA_VERSION, 4)
        sample_file = self.root / "natedev" / "samples-2026-10.jsonl"
        self.write(
            self.host_file,
            step("s0", peak_mem_bytes=123456, mem_stall_some_s=1.25, mem_stall_full_s=0.25, mem_wait_s=7),
            step("s1", peak_mem_bytes=123456),
        )
        self.write(sample_file, sample(STAMP, builds_anon_bytes=2**30, ci_anon_bytes=2 * 2**30), sample("2026-10-02T12:01:00.000Z", stall_some_us=1_500_000))
        self.assertEqual(index.update(), 4)
        self.assertEqual(
            self.rows("SELECT id, mem_stall_some_s, mem_stall_full_s FROM steps ORDER BY id"),
            [("s0", 1.25, 0.25), ("s1", None, None)],
        )
        self.assertEqual(
            self.rows(
                "SELECT host, at, boot_id, mem_used_bytes, swap_used_bytes, stall_some_us, stall_full_us"
                + " FROM samples ORDER BY at"
            ),
            [
                ("natedev", STAMP, "boot-a", 4 * 2**30, 2 * 2**30, 1_000_000, 200_000),
                ("natedev", "2026-10-02T12:01:00.000Z", "boot-a", 4 * 2**30, 2 * 2**30, 1_500_000, 200_000),
            ],
        )
        self.assertEqual(index.update(), 0)
        self.assertEqual(self.rows("SELECT mem_wait_s FROM steps ORDER BY id"), [(7,), (None,)])
        self.assertEqual(self.rows("SELECT builds_anon_bytes, ci_anon_bytes FROM samples ORDER BY at"), [(2**30, 2 * 2**30), (None, None)])

    def test_schema_command_lists_samples_and_stall_columns(self) -> None:
        result = subprocess.run(
            [sys.executable, str(CLI), "schema"], capture_output=True, text=True, check=False, timeout=60
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TABLE samples", result.stdout)
        self.assertIn("mem_stall_some_s", result.stdout)
        self.assertIn("mem_stall_full_s", result.stdout)

    def test_reindex_reads_everything_again(self) -> None:
        self.write(self.host_file, step("s0"), step("s1"), call("c0"))
        self.assertEqual(index.update(), 3)
        self.assertEqual(index.reindex(), 3)
        self.assertEqual(self.ids(), ["s0", "s1"])

    def test_read_only_rejects_writes(self) -> None:
        self.write(self.host_file, step("s0"))
        _ = index.update()
        with closing(index.read_only()) as connection:
            with self.assertRaises(sqlite3.OperationalError):
                _ = connection.execute("DELETE FROM steps")
            with self.assertRaises(sqlite3.OperationalError):
                _ = connection.execute("CREATE TABLE extra (x)")
        self.assertEqual(self.ids(), ["s0"])

    def test_views(self) -> None:
        nextest = parse.log_facts("nextest", (TESTDATA / "nextest.txt").read_text())
        log = "natedev/logs/2026-10/n0.log.gz"
        self.write(
            self.host_file,
            step("c0", duration_s=10.0, peak_mem_bytes=2 * 1073741824),
            step("c1", duration_s=20.0, status=101, errors=1, log=log),
            step("n0", step="nextest", status=100, **nextest),
            call("v0", outcome="reused", saved_s=30),
            call("v1", outcome="reused", saved_s=12),
            call("v2", outcome="ran", wall_s=50),
        )
        skipped = ("2026-10-02T12:01:00Z", "2026-10-02T12:01:00Z", "2026-10-02T12:00:59Z")
        self.write(
            self.root / "ci" / "2026-10.jsonl",
            ci_run(
                1,
                1,
                [
                    ci_job(11, "Clippy", "success", ("2026-10-02T12:00:00Z", "2026-10-02T12:00:30Z", "2026-10-02T12:02:30Z")),
                    ci_job(12, "Benchmark", "skipped", skipped),
                ],
            ),
            ci_run(
                1,
                2,
                [ci_job(21, "Clippy", "failure", ("2026-10-02T12:20:00Z", "2026-10-02T12:20:10Z", "2026-10-02T12:21:10Z"))],
            ),
        )
        _ = index.update()
        day = local_day(STAMP)

        clippy = self.rows(
            "SELECT day, repo, runs, failed, total_s, avg_s, max_s, max_peak_gib FROM step_days WHERE step = 'clippy'"
        )
        self.assertEqual(clippy, [(day, "hana", 2, 1, 30.0, 15.0, 20.0, 2.0)])

        failures = self.rows("SELECT id, worktree_name, log FROM failures ORDER BY id")
        self.assertEqual([row[0] for row in failures], ["c1", "n0"])
        self.assertEqual(failures[0][1], "feature")
        failed_log = failures[0][2]
        self.assertEqual(failed_log, f"{self.root}/{log}")
        self.assertTrue(Path(str(failed_log)).is_absolute())
        self.assertIsNone(failures[1][2])

        flaky = self.rows("SELECT test, runs, flaky, retried, passed, failed FROM flaky_tests ORDER BY test")
        self.assertEqual(flaky, [("tests::fails", 1, 0, 1, 0, 1), ("tests::flaky", 1, 1, 1, 1, 0)])
        self.assertEqual(self.rows("SELECT test, max_s FROM slow_tests"), [("tests::slow", 1.5)])

        outcomes = self.rows("SELECT day, repo, verb, outcome, calls, wall_s, saved_s FROM call_outcomes ORDER BY outcome")
        self.assertEqual(outcomes, [(day, "hana", "test", "ran", 1, 50, 0), (day, "hana", "test", "reused", 2, 0, 42)])

        jobs = self.rows(
            "SELECT name, jobs, failed, avg_s, max_s, avg_queue_s, max_queue_s FROM ci_job_days ORDER BY name"
        )
        # A skipped job never ran: no time of its own, and none dragged into an average.
        self.assertEqual(jobs, [("Benchmark", 1, 0, None, None, None, None), ("Clippy", 2, 1, 90.0, 120.0, 20.0, 30.0)])
        runs = self.rows("SELECT run_id, attempt, jobs, duration_s FROM ci_runs ORDER BY attempt")
        self.assertEqual(runs, [(1, 1, 2, 600.0), (1, 2, 1, 600.0)])
        self.assertEqual(len(self.rows("SELECT * FROM ci_steps")), 3)

    def test_ci_jobs_distinguish_rerun_from_carried_over_and_exclude_carried_time(self) -> None:
        original = ("2026-10-02T12:00:00Z", "2026-10-02T12:00:30Z", "2026-10-02T12:02:30Z")
        carried = ("2026-10-03T12:20:00Z", original[1], original[2])
        rerun = ("2026-10-03T12:20:00Z", "2026-10-03T12:20:10Z", "2026-10-03T12:21:10Z")
        fresh = ("2026-10-03T12:30:00Z", "2026-10-03T12:30:05Z", "2026-10-03T12:31:05Z")
        second_attempt = ci_run(1, 2, [ci_job(21, "Clippy", "success", carried), ci_job(22, "Test", "failure", rerun)])
        second_attempt.update({"created_at": "2026-10-03T12:20:00Z", "started_at": "2026-10-03T12:20:00Z",
                               "updated_at": "2026-10-03T12:32:00Z"})
        other_run = ci_run(2, 1, [ci_job(31, "Clippy", "success", fresh)])
        other_run.update({"created_at": "2026-10-03T12:30:00Z", "started_at": "2026-10-03T12:30:00Z",
                          "updated_at": "2026-10-03T12:32:00Z"})
        self.write(
            self.root / "ci" / "2026-10.jsonl",
            second_attempt,
            ci_run(1, 1, [ci_job(11, "Clippy", "success", original)]),
            other_run,
        )
        _ = index.update()

        self.assertEqual(
            self.rows("SELECT job_id, run_state, duration_s, queued_s FROM ci_jobs ORDER BY job_id"),
            [(11, "ran", 120.0, 30.0), (21, "carried_over", None, None), (22, "ran", 60.0, 10.0), (31, "ran", 60.0, 5.0)],
        )
        self.assertEqual(
            self.rows(
                "SELECT day, name, jobs, failed, avg_s, max_s, avg_queue_s, max_queue_s"
                + " FROM ci_job_days ORDER BY day, name"
            ),
            [
                ("2026-10-02", "Clippy", 1, 0, 120.0, 120.0, 30.0, 30.0),
                ("2026-10-03", "Clippy", 1, 0, 60.0, 60.0, 5.0, 5.0),
                ("2026-10-03", "Test", 1, 1, 60.0, 60.0, 10.0, 10.0),
            ],
        )

    def test_ci_job_days_uses_run_attempt_day_across_local_midnight(self) -> None:
        before_midnight = datetime(2026, 10, 2, 23, 59).astimezone().isoformat()
        after_midnight = datetime(2026, 10, 3, 0, 1).astimezone().isoformat()
        started = datetime(2026, 10, 3, 0, 2).astimezone().isoformat()
        completed = datetime(2026, 10, 3, 0, 4).astimezone().isoformat()
        run = ci_run(3, 1, [ci_job(31, "Midnight", "success", (after_midnight, started, completed))])
        run.update({"created_at": before_midnight, "started_at": before_midnight, "updated_at": completed})
        self.write(self.root / "ci" / "2026-10.jsonl", run)
        _ = index.update()

        self.assertEqual(
            self.rows("SELECT day, name, jobs, avg_queue_s FROM ci_job_days"),
            [("2026-10-02", "Midnight", 1, 60.0)],
        )
        self.assertEqual(self.rows("SELECT queued_s, run_state FROM ci_jobs"), [(60.0, "ran")])

    def test_call_token_wait_defaults_to_zero_for_old_records(self) -> None:
        self.write(self.host_file, call("old"), call("new", token_wait_s=13))
        _ = index.update()
        self.assertEqual(
            self.rows("SELECT id, token_wait_s FROM calls ORDER BY id"),
            [("new", 13), ("old", 0)],
        )

    def test_mac_call_fields_reach_index_and_old_records_read_as_null(self) -> None:
        self.write(
            self.host_file,
            call("old"),
            call(
                "offloaded",
                mac="declined",
                mac_reason="no_tests",
                mac_s=12.5,
            ),
        )

        _ = index.update()

        self.assertEqual(
            self.rows(
                "SELECT id, mac, mac_reason, mac_s FROM calls ORDER BY id"
            ),
            [
                ("offloaded", "declined", "no_tests", 12.5),
                ("old", None, None, None),
            ],
        )
        self.assertEqual(index.SCHEMA_VERSION, 10)

    def test_ci_skipped_and_zero_queue_jobs_have_distinct_states(self) -> None:
        same_stamp = "2026-10-02T12:00:00Z"
        skipped = ("2026-10-02T12:01:00Z", "2026-10-02T12:01:00Z", "2026-10-02T12:00:59Z")
        self.write(
            self.root / "ci" / "2026-10.jsonl",
            ci_run(1, 1, [ci_job(11, "Zero", "success", (same_stamp, same_stamp, "2026-10-02T12:01:00Z")), ci_job(12, "Skipped", "skipped", skipped)]),
        )
        _ = index.update()

        self.assertEqual(
            self.rows("SELECT job_id, run_state, duration_s, queued_s FROM ci_jobs ORDER BY job_id"),
            [(11, "ran", 60.0, 0.0), (12, "skipped", None, None)],
        )

    def test_ci_job_run_states_name_the_persisted_outcomes(self) -> None:
        self.assertEqual(
            {state.value for state in index.JobRunState},
            {"ran", "skipped", "carried_over"},
        )

    def test_tree_and_port_lint_columns(self) -> None:
        self.write(
            self.host_file,
            step("s0", tree_key="k" * 64, tree_changed=False),
            step("s1", tree_key=None, tree_changed=True),
            step("s2"),
            call("p0", tool="port-lint", caller="cargo-port", outcome="reused", reuses="s0", saved_s=42),
            call("p1", tool="port-lint", caller="cargo-port", outcome="deferred", status=75, reason="port-lint: deferred"),
        )
        _ = index.update()
        steps = self.rows("SELECT id, tree_key, tree_changed FROM steps ORDER BY id")
        self.assertEqual(steps, [("s0", "k" * 64, 0), ("s1", None, 1), ("s2", None, None)])
        calls = self.rows("SELECT id, tool, caller, outcome, status, reuses, reason FROM calls ORDER BY id")
        self.assertEqual(
            calls,
            [
                ("p0", "port-lint", "cargo-port", "reused", 0, "s0", None),
                ("p1", "port-lint", "cargo-port", "deferred", 75, None, "port-lint: deferred"),
            ],
        )
        plan = self.rows(
            "EXPLAIN QUERY PLAN SELECT id FROM steps WHERE worktree = ? AND step = ? AND tree_key = ?",
            "/r/feature",
            "clippy",
            "k" * 64,
        )
        self.assertIn("steps_tree", str(plan))

    def test_cli_query_json(self) -> None:
        self.write(self.host_file, step("s0", status=0), step("s1", status=101))
        result = subprocess.run(
            [sys.executable, str(CLI), "query", "--json", "SELECT id, status, step FROM steps ORDER BY id"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = cast(list[dict[str, object]], json.loads(result.stdout))
        self.assertEqual(
            rows, [{"id": "s0", "status": 0, "step": "clippy"}, {"id": "s1", "status": 101, "step": "clippy"}]
        )
        self.assertIn("2 rows in", result.stderr)

        failed = subprocess.run(
            [sys.executable, str(CLI), "query", "DELETE FROM steps"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(failed.returncode, 1)
        self.assertIn("readonly", failed.stderr)


if __name__ == "__main__":
    _ = unittest.main()
