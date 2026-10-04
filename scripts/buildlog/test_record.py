#!/usr/bin/env python3
"""Tests for record.py, run as invoke.sh and verify.sh run it: a subprocess, BUILDLOG_DIR on a temporary root."""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import cast, override

from test_index import point_root_at

SCRIPT = Path(__file__).with_name("record.py")
TESTDATA = Path(__file__).with_name("testdata")
START = datetime(2026, 10, 2, 12, 0, tzinfo=UTC).timestamp()
CALL_ID = "natedev-20261002T120000-abcdef"
SWEEP = ["/home/u/.claude/scripts/lib/py", "/home/u/.claude/scripts/lint/sweep.py"]
NEXTEST = ["cargo", "nextest", "run", "--workspace", "--retries", "2"]
# What the ambient session would otherwise stamp on every record: the caller,
# the delegate seat, the session and the call id all come from these.
AMBIENT = (
    "BUILDLOG_CALLER",
    "BUILDLOG_CALL_ID",
    "LINT_OUTPUT_DIR",
    "VALIDATE_TARGET_DIR",
    "CLAUDECODE",
    "CODEX_THREAD_ID",
    "CLAUDE_CODE_SESSION_ID",
    "MANIFEST_PATH",
    "BUILDLOG_TREE_START",
)
Record = dict[str, object]


def ledger_line(**fields: object) -> str:
    event: Record = {
        "at": "2026-10-01T15:34:46+00:00",
        "machine": "natedev",
        "workspace": "/r/hana",
        "worktree": "/r/feature",
        "branch": "feature",
        "commit": "",
        "command": "test hana_diegetic",
        "outcome": "reused",
        "session": "",
        "wait_s": 0,
        "wall_s": 0,
        "build_s": 0,
        "saved_s": 19,
    }
    event.update(fields)
    return json.dumps(event)


class RecordTests(unittest.TestCase):
    base: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    repo: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.root = self.base / "buildlog"
        point_root_at(self, self.root)
        self.repo = self.base / "hana"
        self.repo.mkdir()
        _ = self.git("init", "-q", "-b", "main", cwd=self.repo)
        _ = self.git("commit", "-q", "--allow-empty", "-m", "first", cwd=self.repo)

    def environment(self, **extra: str) -> dict[str, str]:
        environment = {
            name: value
            for name, value in os.environ.items()
            if name not in AMBIENT and not name.startswith("PLAN_DELEGATE_")
        }
        environment["BUILDLOG_DIR"] = str(self.root)
        # No signing, hooks or identity from the user's own git config.
        environment["GIT_CONFIG_GLOBAL"] = os.devnull
        environment["GIT_CONFIG_NOSYSTEM"] = "1"
        environment["GIT_AUTHOR_NAME"] = environment["GIT_COMMITTER_NAME"] = "test"
        environment["GIT_AUTHOR_EMAIL"] = environment["GIT_COMMITTER_EMAIL"] = "test@example.com"
        environment.update(extra)
        return environment

    def git(self, *args: str, cwd: Path) -> str:
        result = subprocess.run(
            ["git", *args], cwd=cwd, env=self.environment(), capture_output=True, text=True, check=True
        )
        return result.stdout.strip()

    def record(
        self, *args: str, cwd: Path | None = None, extra: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=cwd or self.repo,
            env=self.environment(**(extra or {})),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def step(
        self, status: int, tty: str, handoff: str, peak: str, argv: list[str], extra: dict[str, str] | None = None
    ) -> Record:
        result = self.record(
            "step", str(status), str(START), str(START + 12.5), tty, handoff, peak, *argv, extra=extra
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.records()[-1]

    def records(self) -> list[Record]:
        found: list[Record] = []
        for path in sorted(self.root.glob("*/*.jsonl")):
            found += [cast(Record, json.loads(line)) for line in path.read_text().splitlines()]
        return found

    def handoff(self) -> Path:
        path = self.base / "handoff.log"
        _ = shutil.copyfile(TESTDATA / "nextest.txt", path)
        return path

    def peak_file(self) -> Path:
        path = self.base / "peak"
        _ = path.write_text("123456\n")
        return path

    def test_step_record_fields(self) -> None:
        handoff, peak = self.handoff(), self.peak_file()
        record = self.step(1, "0", str(handoff), str(peak), NEXTEST, {"BUILDLOG_CALL_ID": CALL_ID})
        self.assertEqual(record["kind"], "step")
        self.assertEqual(record["step"], "nextest")
        self.assertEqual(record["status"], 1)
        self.assertEqual(record["started_at"], "2026-10-02T12:00:00.000Z")
        self.assertEqual(record["duration_s"], 12.5)
        self.assertEqual(record["argv"], NEXTEST)
        self.assertEqual(record["peak_mem_bytes"], 123456)
        self.assertEqual(record["call_id"], CALL_ID)
        self.assertEqual(record["caller"], "unknown")
        self.assertIs(record["tty"], False)
        self.assertEqual((record["tests_run"], record["tests_failed"], record["tests_flaky"]), (5, 1, 1))
        tests = cast(list[Record], record["tests"])
        self.assertEqual(
            sorted((row["test"], row["status"]) for row in tests),
            [("tests::fails", "failed"), ("tests::flaky", "flaky"), ("tests::slow", "passed")],
        )
        self.assertEqual(record["repo_path"], str(self.repo))
        self.assertEqual(record["worktree"], str(self.repo))
        self.assertEqual(record["branch"], "main")
        self.assertEqual(record["sha"], self.git("rev-parse", "--short", "HEAD", cwd=self.repo))
        host = socket.gethostname().split(".")[0]
        self.assertEqual(record["host"], host)
        self.assertRegex(str(record["id"]), rf"^{re.escape(host)}-20261002T120000-[0-9a-f]{{6}}$")
        # A failed step keeps its output, gzipped, beside the records.
        log = record["log"]
        self.assertIsInstance(log, str)
        self.assertEqual(gzip.decompress((self.root / str(log)).read_bytes()), (TESTDATA / "nextest.txt").read_bytes())
        self.assertFalse(handoff.exists())
        self.assertFalse(peak.exists())

    def test_passing_step_keeps_no_log(self) -> None:
        handoff = self.handoff()
        record = self.step(0, "0", str(handoff), "", NEXTEST)
        self.assertIsNone(record["log"])
        self.assertIsNone(record["peak_mem_bytes"])
        self.assertEqual(record["tests_run"], 5)
        self.assertFalse(handoff.exists())
        self.assertEqual(list(self.root.glob("*/logs")), [])

    def test_tty_step_records_no_log_facts(self) -> None:
        handoff = self.handoff()
        record = self.step(1, "1", str(handoff), "", NEXTEST)
        self.assertIs(record["tty"], True)
        self.assertEqual(record["caller"], "alias")
        for fact in ("finished_s", "crates_compiled", "warnings", "errors", "tests_run", "tests", "log"):
            self.assertIsNone(record[fact], fact)
        self.assertFalse(handoff.exists())

    def test_caller_precedence(self) -> None:
        every = {
            "BUILDLOG_CALLER": "verify",
            "LINT_OUTPUT_DIR": "/tmp/lint",
            "VALIDATE_TARGET_DIR": "/tmp/validate",
            "CLAUDECODE": "1",
        }
        cases: list[tuple[dict[str, str], str, str]] = [
            (every, "0", "verify"),
            ({**every, "BUILDLOG_CALLER": ""}, "0", "cargo-port"),
            ({"VALIDATE_TARGET_DIR": "/tmp/validate", "CLAUDECODE": "1"}, "1", "validate_ci"),
            ({"CLAUDECODE": "1"}, "1", "agent"),
            ({"CODEX_THREAD_ID": "thread-9"}, "0", "agent"),
            ({}, "1", "alias"),
            ({}, "0", "unknown"),
        ]
        for extra, tty, expected in cases:
            with self.subTest(expected=expected, extra=extra):
                self.assertEqual(self.step(0, tty, "", "", SWEEP, extra)["caller"], expected)
        self.assertEqual(self.records()[4]["session"], "thread-9")

    def test_linked_worktree_and_detached_head(self) -> None:
        feature = self.base / "feature"
        _ = self.git("worktree", "add", "-q", "-b", "feature", str(feature), cwd=self.repo)
        record = self.record("step", "0", str(START), "", "0", "", "", *SWEEP, cwd=feature)
        self.assertEqual(record.returncode, 0, record.stderr)
        linked = self.records()[-1]
        self.assertEqual((linked["repo_path"], linked["worktree"], linked["branch"]), (str(self.repo), str(feature), "feature"))
        self.assertEqual(linked["cwd"], str(feature))
        self.assertIsNone(linked["rustc"])

        _ = self.git("checkout", "-q", "--detach", cwd=self.repo)
        detached = self.step(0, "0", "", "", SWEEP)
        self.assertEqual(detached["branch"], "detached")
        self.assertEqual(detached["sha"], self.git("rev-parse", "--short", "HEAD", cwd=self.repo))

    def test_manifest_path_names_the_repo(self) -> None:
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        argv = ["cargo", "clippy", "--manifest-path", str(self.repo / "Cargo.toml")]
        result = self.record("step", "0", str(START), "", "0", "", "", *argv, cwd=elsewhere)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.records()[-1]
        self.assertEqual((record["cwd"], record["repo_path"], record["branch"]), (str(elsewhere), str(self.repo), "main"))

    def key(self, *argv: str, cwd: Path | None = None) -> str:
        result = self.record("key", *argv, cwd=cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_tree_key_kept_only_when_the_files_held_still(self) -> None:
        key = self.key(*NEXTEST)
        self.assertRegex(key, r"^[0-9a-f]{64}$")
        held = self.step(0, "0", "", "", NEXTEST, {"BUILDLOG_TREE_START": key})
        self.assertEqual((held["tree_key"], held["tree_changed"]), (key, False))
        moved = self.step(0, "0", "", "", NEXTEST, {"BUILDLOG_TREE_START": "0" * 64})
        self.assertEqual((moved["tree_key"], moved["tree_changed"]), (None, True))
        unknown = self.step(0, "0", "", "", NEXTEST)
        self.assertEqual((unknown["tree_key"], unknown["tree_changed"]), (None, None))
        _ = (self.repo / "new.rs").write_text("\n")
        self.assertNotEqual(self.key(*NEXTEST), key)

    def test_key_follows_the_manifest_and_is_empty_outside_git(self) -> None:
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        self.assertEqual(self.key(cwd=elsewhere), "")
        manifest = ["cargo", "clippy", "--manifest-path", str(self.repo / "Cargo.toml")]
        self.assertEqual(self.key(*manifest, cwd=elsewhere), self.key())
        start = {"BUILDLOG_TREE_START": "0" * 64}
        result = self.record("step", "0", str(START), "", "0", "", "", *SWEEP, cwd=elsewhere, extra=start)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.records()[-1]
        self.assertEqual((record["tree_key"], record["tree_changed"]), (None, None))

    def test_outside_git_has_no_git_facts(self) -> None:
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        result = self.record("step", "0", str(START), "", "0", "", "", *SWEEP, cwd=elsewhere)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.records()[-1]
        self.assertEqual([record[key] for key in ("repo_path", "worktree", "branch", "sha")], [None] * 4)

    def test_call_record_fields(self) -> None:
        board = {"PLAN_DELEGATE_TEAM_ROLE": "impl2", "PLAN_DELEGATE_BOARD_DIR": "/x/boards/run-7/"}
        result = self.record(
            "call",
            *("reused", "0", "1", "3", "0", "", "42", "5", "test", "hana"),
            extra={"BUILDLOG_CALL_ID": CALL_ID, "CLAUDE_CODE_SESSION_ID": "session-1", **board},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.records()[-1]
        expected: Record = {
            "kind": "call",
            "id": CALL_ID,
            "tool": "verify.sh",
            "command": "test hana",
            "verb": "test",
            "package": "hana",
            "outcome": "reused",
            "status": 0,
            "cached": True,
            "wait_s": 3,
            "wall_s": 0,
            "build_s": None,
            "saved_s": 42,
            "cwd": str(self.repo),
            "repo_path": str(self.repo),
            "branch": "main",
            "seat": "impl2",
            "delegate_session": "run-7",
            "session": "session-1",
            "backfilled": False,
        }
        self.assertEqual({key: record[key] for key in expected}, expected)
        started = datetime.fromisoformat(str(record["started_at"]))
        ended = datetime.fromisoformat(str(record["ended_at"]))
        self.assertAlmostEqual((ended - started).total_seconds(), 5.0, delta=0.002)

        final = self.record("call", "interrupted", "", "0", "0", "12", "", "0", "12", "final")
        self.assertEqual(final.returncode, 0, final.stderr)
        record = self.records()[-1]
        self.assertEqual((record["verb"], record["package"], record["status"], record["build_s"]), ("final", None, None, None))
        self.assertIs(record["cached"], False)
        self.assertNotEqual(record["id"], CALL_ID)

    def test_backfill_is_idempotent(self) -> None:
        same = ledger_line()
        lines = [
            same,
            same,
            ledger_line(at="2026-09-30T23:00:00+00:00", outcome="ran", wait_s=5, wall_s=60, build_s=40, saved_s=0),
            ledger_line(machine="Nates-MacBook-Pro-2023", outcome="failed", commit="abc1234", session="run-3"),
        ]
        ledger = self.base / "events.jsonl"
        _ = ledger.write_text("\n".join(lines) + "\n\n")
        first = self.record("backfill-verify", str(ledger))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("4 added, 0 already present", first.stdout)
        records = self.records()
        self.assertEqual(len(records), 4)
        self.assertEqual(len({record["id"] for record in records}), 4)
        self.assertTrue((self.root / "natedev" / "2026-09.jsonl").is_file())
        self.assertTrue((self.root / "Nates-MacBook-Pro-2023" / "2026-10.jsonl").is_file())

        by_outcome = {str(record["outcome"]): record for record in records}
        ran = by_outcome["ran"]
        self.assertEqual((ran["started_at"], ran["ended_at"]), ("2026-09-30T22:58:55.000Z", "2026-09-30T23:00:00.000Z"))
        self.assertEqual((ran["status"], ran["build_s"], ran["cached"], ran["backfilled"]), (0, 40, True, True))
        failed = by_outcome["failed"]
        self.assertEqual((failed["host"], failed["status"], failed["sha"]), ("Nates-MacBook-Pro-2023", None, "abc1234"))
        self.assertEqual((failed["delegate_session"], failed["repo_path"]), ("run-3", "/r/hana"))
        self.assertIsNone(by_outcome["reused"]["sha"])

        second = self.record("backfill-verify", str(ledger))
        self.assertIn("0 added, 4 already present", second.stdout)
        self.assertEqual(len(self.records()), 4)

    def test_an_error_exits_zero_and_is_logged(self) -> None:
        handoff, peak = self.handoff(), self.peak_file()
        bad_start = self.record("step", "0", "not-a-number", "", "0", str(handoff), str(peak), *NEXTEST)
        short_call = self.record("call", "ran")
        unknown = self.record("bogus")
        for result in (bad_start, short_call, unknown):
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.records(), [])
        self.assertFalse(handoff.exists())
        self.assertFalse(peak.exists())
        errors = (self.root / "errors.log").read_text()
        self.assertIn("record.py step 0 not-a-number", errors)
        self.assertIn("ValueError", errors)
        self.assertIn("record.py call ran", errors)
        self.assertIn("unknown command bogus", errors)


if __name__ == "__main__":
    _ = unittest.main()
