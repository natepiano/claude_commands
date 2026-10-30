#!/usr/bin/env python3
"""Tests for record.py, run as invoke.sh and verify.sh run it: a subprocess, BUILDLOG_DIR on a temporary root."""

from __future__ import annotations

import gzip
import json
import os
import re
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast, override

if TYPE_CHECKING:
    from ..lib.socket_test_support import short_socket_directory
else:
    from scripts.lib.socket_test_support import short_socket_directory

import record as recorder
import sample
import store

if TYPE_CHECKING:
    from ..direct.test_verify_untested_examples import FIXED_RUSTC, rust_tool_path
    from ..lint.memory_test_support import fake_memory_readings
else:
    from scripts.direct.test_verify_untested_examples import FIXED_RUSTC, rust_tool_path
    from scripts.lint.memory_test_support import fake_memory_readings
from test_index import point_root_at
if TYPE_CHECKING:
    from ..lib import test_hang_guard
else:
    from scripts.lib import test_hang_guard

SCRIPT = Path(__file__).with_name("record.py")
TESTDATA = Path(__file__).with_name("testdata")
START = datetime(2026, 10, 2, 12, 0, tzinfo=UTC).timestamp()
CALL_ID = "natedev-20261002T120000-abcdef"
SWEEP = ["/home/u/.claude/scripts/lib/py", "/home/u/.claude/scripts/lint/sweep.py"]
NEXTEST = ["cargo", "nextest", "run", "--workspace", "--retries", "2"]
# What the ambient session would otherwise stamp on every record: the caller,
# the worker, the session and the call id all come from these.
AMBIENT = (
    "BUILDLOG_CALLER",
    "BUILDLOG_CALL_ID",
    "BUILDLOG_LOCAL_REASON",
    "BUILDLOG_MAC_HOLDER",
    "BUILDLOG_MAC_COPY",
    "BUILDLOG_MAC_COPY_S",
    "BUILDLOG_MAC_COPY_COUNT",
    "BUILDLOG_MAC_COPY_NATEDEV_S",
    "BUILDLOG_TARGET",
    "BUILDLOG_HOLD_WAIT_S",
    "LINT_OUTPUT_DIR",
    "VALIDATE_TARGET_DIR",
    "CLAUDECODE",
    "CODEX_THREAD_ID",
    "CLAUDE_CODE_SESSION_ID",
    "MANIFEST_PATH",
    "BUILDLOG_TREE_START",
    "BUILDLOG_SENT_TREE_KEY",
    "BUILDLOG_MEM_WAIT_S",
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


def setUpModule() -> None:
    """The times these tests expect are Pacific; the user's zone setting must not change them."""
    unittest.addModuleCleanup(time.tzset)
    unittest.enterModuleContext(mock.patch.dict(os.environ, {"TZ": "America/Los_Angeles", "USER_ZONE": "America/Los_Angeles"}))
    time.tzset()


class RecordTests(unittest.TestCase):
    base: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    repo: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(mock.patch.dict(os.environ, fake_memory_readings(self.base)))
        tools = rust_tool_path(self.base / "tools")
        self.enterContext(mock.patch.dict(os.environ, {"PATH": tools}))
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
            if name not in AMBIENT and not name.startswith("UNIT_")
        }
        environment["BUILDLOG_DIR"] = str(self.root)
        environment["BUILD_HOLD_DIR"] = str(self.base / "holders")
        environment["BUILD_HOLD_RELEASE_DIR"] = str(self.base / "release")
        environment["UNIT_ACTIVE_DIR"] = str(self.base / "active")
        environment["BUILDLOG_CALL_ID"] = environment["BUILDLOG_CALLER"] = ""
        environment["BUILDLOG_HOLD_WAIT_FILE"] = environment["BUILDLOG_HOLD_ADMISSION_FILE"] = ""
        environment["BUILDLOG_HOLD_STARTED_FILE"] = environment["BUILDLOG_HOLD_WAITER_FILE"] = ""
        environment["BUILDLOG_HOLD_REGISTRATION_FILE"] = str(self.base / "registration")
        environment["MAC_TEST_STATE_DIR"] = str(self.base / "mac-state")
        environment["NOTIFIER_SESSIONS_DIR"] = str(self.base / "sessions")
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
            timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
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
        self.assertEqual(record["rustc"], FIXED_RUSTC)
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

    def test_step_records_cgroup_memory_stall_seconds(self) -> None:
        peak = self.base / "peak-pressure"
        _ = peak.write_text("\n".join([
            "123456",
            "some avg10=0.00 avg60=0.01 avg300=0.02 total=1250000",
            "full avg10=0.00 avg60=0.00 avg300=0.00 total=250000",
            "",
        ]))
        record = self.step(0, "0", "", str(peak), SWEEP)
        self.assertEqual(record["peak_mem_bytes"], 123456)
        self.assertEqual(record["mem_stall_some_s"], 1.25)
        self.assertEqual(record["mem_stall_full_s"], 0.25)
        self.assertFalse(peak.exists())

    def test_step_records_memory_wait_separately_from_build_duration(self) -> None:
        record = self.step(0, "0", "", "", SWEEP, {"BUILDLOG_MEM_WAIT_S": "23"})
        self.assertEqual(record["mem_wait_s"], 23)
        self.assertEqual(record["duration_s"], 12.5)

    def test_piped_run_records_memory_wait_outside_step_duration(self) -> None:
        meminfo = self.base / "meminfo"
        _ = meminfo.write_text("MemAvailable: 11534336 kB\n")
        runtime = Path(self.enterContext(short_socket_directory())) / "systemd"
        runtime.mkdir(parents=True)
        with socket.socket(socket.AF_UNIX) as manager:
            manager.bind(str(runtime / "private"))
            systemd_run = self.base / "systemd-run"
            _ = systemd_run.write_text(
                '#!/bin/sh\nwhile [ "$1" != -- ]; do shift; done\n'
                + 'shift\npeak="$4"\nshift 4\nprintf "123\\n" > "$peak"\n"$@"\n'
            )
            systemd_run.chmod(0o755)
            command = (
                'source "$1"; BUILDLOG_RECORD="$2"; sweep_after_step() { :; }; '
                'buildlog_now() { '
                'if [[ -e "$TEST_STEP_FINISHED" ]]; then BUILDLOG_NOW=1790942507; '
                'elif [[ "$(< "$BUILDLOG_MEMINFO")" == "MemAvailable: 12582912 kB" ]]; '
                'then BUILDLOG_NOW=1790942500; else BUILDLOG_NOW=1790942400; fi; }; '
                "run_once bash -c 'printf \"step\\n\"; touch \"$TEST_STEP_FINISHED\"'"
            )
            process = subprocess.Popen(
                ["bash", "-c", command, "bash", str(SCRIPT.parent.parent / "lint" / "invoke.sh"), str(SCRIPT)],
                cwd=self.repo,
                env=self.environment(
                    PATH=f"{self.base}:{os.environ['PATH']}",
                    XDG_RUNTIME_DIR=str(runtime.parent),
                    BUILDLOG_OFF="0", BUILDLOG_SCOPE="1",
                    BUILDLOG_MEMINFO=str(meminfo), BUILDLOG_MEM_POLL_S="1", BUILDLOG_MEM_WAIT_LIMIT_S="5",
                    BUILDLOG_SYNC="1", TEST_STEP_FINISHED=str(self.base / "step-finished"),
                ),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                assert process.stderr is not None
                readable, _, _ = select.select([process.stderr], [], [], test_hang_guard.TEST_HANG_GUARD_SECS)
                self.assertTrue(readable, "memory wait message did not arrive")
                first_message = cast(str, process.stderr.readline())
                self.assertIn("waiting for memory since ", first_message)
                time.sleep(1.1)
                _ = meminfo.write_text("MemAvailable: 12582912 kB\n")
                output, remaining_errors = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
                errors = first_message + remaining_errors
            finally:
                if process.poll() is None:
                    process.kill()
                    _ = process.communicate()
        self.assertEqual(process.returncode, 0, errors)
        self.assertIn("step", output)
        self.assertEqual(errors.count("waiting for memory since "), 1)
        self.assertNotIn("memory wait limit reached", errors)
        record = self.records()[-1]
        wait = record["mem_wait_s"]
        duration = record["duration_s"]
        started = datetime.fromisoformat(str(record["started_at"]).replace("Z", "+00:00")).timestamp()
        assert isinstance(wait, int)
        assert isinstance(duration, (int, float))
        self.assertGreaterEqual(wait, 1)
        self.assertEqual(started, 1790942500)
        self.assertEqual(duration, 7)

    def test_launch_memory_wait_limit_refuses_once(self) -> None:
        meminfo = self.base / "meminfo"
        _ = meminfo.write_text("MemAvailable: 11010048 kB\n")
        command = ('source "$1"; sleep() { SECONDS=$((SECONDS + 1)); }; '
                   'status=0; buildlog_wait_for_memory || status=$?; '
                   'printf "%s\\n" "$BUILDLOG_MEM_WAIT_S"; exit "$status"')
        environment = self.environment(BUILDLOG_MEMINFO=str(meminfo), BUILDLOG_MEM_POLL_S="1", BUILDLOG_MEM_WAIT_LIMIT_S="1")
        result = subprocess.run(
            ["bash", "-c", command, "bash", str(SCRIPT.parent.parent / "lint" / "invoke.sh")],
            env=environment, capture_output=True, text=True, check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertRegex(result.stderr, r"waiting for memory since \d\d:\d\d P[DS]T: the machine has 10\.5 GiB free; a build starts at 12\.0")
        self.assertEqual(result.stderr.count("waiting for memory since "), 1)
        reason = "memory admission check failed (launch wait limit reached without admission); step refused"
        self.assertEqual(result.stderr.count(reason), 1)
        self.assertNotIn("starting", result.stderr)
        self.assertGreaterEqual(int(result.stdout.strip()), 1)

    def test_memory_admission_refuses_invalid_readings_and_allows_at_floor(self) -> None:
        command = 'set -e; source "$1"; buildlog_wait_for_memory; printf "%s\\n" "$BUILDLOG_MEM_WAIT_S"'
        cases = {
            "missing": self.base / "missing-meminfo",
            "unreadable": self.base / "directory",
            "no available line": self.base / "without-available",
            "non-numeric available": self.base / "invalid-available",
            "at floor": self.base / "at-floor",
            "above floor": self.base / "above-floor",
        }
        cases["unreadable"].mkdir()
        _ = cases["no available line"].write_text("MemFree: 1000 kB\n")
        _ = cases["non-numeric available"].write_text("MemAvailable: unknown kB\n")
        _ = cases["at floor"].write_text("MemAvailable: 12582912 kB\n")
        _ = cases["above floor"].write_text("MemAvailable: 13631488 kB\n")
        for name, meminfo in cases.items():
            with self.subTest(name=name):
                result = subprocess.run(
                    ["bash", "-c", command, "bash", str(SCRIPT.parent.parent / "lint" / "invoke.sh")],
                    env=self.environment(BUILDLOG_MEMINFO=str(meminfo)),
                    capture_output=True, text=True, check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
                )
                if name in ("at floor", "above floor"):
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, "")
                    self.assertEqual(result.stdout.strip(), "0")
                else:
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stderr, f"memory admission check failed (cannot read MemAvailable from {meminfo}); step refused\n")
                    self.assertEqual(result.stdout, "")

    def test_memory_admission_does_not_count_a_slow_read_above_floor(self) -> None:
        meminfo = self.base / "meminfo-fifo"
        os.mkfifo(meminfo)
        command = 'source "$1"; SECONDS=0; printf "ready\\n"; buildlog_wait_for_memory; printf "%s\\n" "$BUILDLOG_MEM_WAIT_S"'
        process = subprocess.Popen(
            ["bash", "-c", command, "bash", str(SCRIPT.parent.parent / "lint" / "invoke.sh")],
            env=self.environment(BUILDLOG_MEMINFO=str(meminfo)),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            assert process.stdout is not None
            self.assertEqual(process.stdout.readline(), "ready\n")
            time.sleep(1.1)
            with meminfo.open("w") as writer:
                _ = writer.write("MemAvailable: 12582912 kB\n")
            output, errors = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
        finally:
            if process.poll() is None:
                process.kill()
                _ = process.communicate()
        self.assertEqual(process.returncode, 0, errors)
        self.assertEqual(errors, "")
        self.assertEqual(output.strip(), "0")

    def test_sweep_step_starts_without_memory_wait(self) -> None:
        sweep = self.base / "sweep.py"
        _ = sweep.write_text('print("sweep started")\n')
        self.assert_step_starts_without_memory_wait([sys.executable, str(sweep)], "sweep started")

    def test_cargo_fmt_step_starts_without_memory_wait(self) -> None:
        cargo = self.base / "cargo"
        _ = cargo.write_text(
            '#!/bin/sh\ncase "$*" in\n'
            + 'fmt|"+nightly fmt") printf "fmt started\\n" ;;\n'
            + '*) echo "Unexpected cargo call: $*" >&2; exit 99 ;;\nesac\n'
        )
        cargo.chmod(0o755)
        for toolchain in ([], ["+nightly"]):
            with self.subTest(toolchain=toolchain):
                self.assert_step_starts_without_memory_wait(["cargo", *toolchain, "fmt"], "fmt started")
                self.assertEqual(self.records()[-1]["rustc"], FIXED_RUSTC)

    def assert_step_starts_without_memory_wait(self, argv: list[str], output_line: str) -> None:
        meminfo = self.base / "meminfo"
        _ = meminfo.write_text("MemAvailable: 11534336 kB\n")
        command = (
            'source "$1"; BUILDLOG_RECORD="$2"; sweep_after_step() { :; }; '
            'shift 2; run_once "$@"; printf "wait=%s\\n" "$BUILDLOG_MEM_WAIT_S"'
        )
        result = subprocess.run(
            ["bash", "-c", command, "bash", str(SCRIPT.parent.parent / "lint" / "invoke.sh"), str(SCRIPT), *argv],
            cwd=self.repo,
            env=self.environment(PATH=f"{self.base}:{os.environ['PATH']}", BUILDLOG_OFF="0", BUILDLOG_SCOPE="0",
                                 BUILDLOG_MEMINFO=str(meminfo), BUILDLOG_MEM_POLL_S="1",
                                 BUILDLOG_MEM_WAIT_LIMIT_S="0", BUILDLOG_SYNC="1"),
            capture_output=True, text=True, check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(output_line, result.stdout)
        self.assertIn("wait=0", result.stdout)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.records()[-1]["mem_wait_s"], 0)

    def test_unscoped_step_waits_for_memory(self) -> None:
        meminfo = self.base / "meminfo"
        _ = meminfo.write_text("MemAvailable: 11534336 kB\n")
        marker = self.base / "step-started"
        step = self.base / "step.sh"
        _ = step.write_text('#!/bin/sh\n: > "$STEP_MARKER"\nprintf "step\\n"\n')
        step.chmod(0o755)
        command = (
            'source "$1"; BUILDLOG_RECORD="$2"; sweep_after_step() { :; }; '
            'run_once "$3"; printf "wait=%s\\n" "$BUILDLOG_MEM_WAIT_S"'
        )
        process = subprocess.Popen(
            ["bash", "-c", command, "bash", str(SCRIPT.parent.parent / "lint" / "invoke.sh"), str(SCRIPT), str(step)],
            cwd=self.repo,
            env=self.environment(BUILDLOG_OFF="0", BUILDLOG_SCOPE="0", BUILDLOG_MEMINFO=str(meminfo),
                                 BUILDLOG_MEM_POLL_S="1", BUILDLOG_MEM_WAIT_LIMIT_S="5", BUILDLOG_SYNC="1",
                                 STEP_MARKER=str(marker)),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            assert process.stderr is not None
            readable, _, _ = select.select([process.stderr], [], [], test_hang_guard.TEST_HANG_GUARD_SECS)
            self.assertTrue(readable, "memory wait message did not arrive")
            first_message = cast(str, process.stderr.readline())
            self.assertIn("waiting for memory since ", first_message)
            self.assertFalse(marker.exists())
            time.sleep(1.1)
            released_at = time.time()
            _ = meminfo.write_text("MemAvailable: 12582912 kB\n")
            output, remaining_errors = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
            errors = first_message + remaining_errors
        finally:
            if process.poll() is None:
                process.kill()
                _ = process.communicate()
        self.assertEqual(process.returncode, 0, errors)
        self.assertIn("step", output)
        self.assertRegex(output, r"wait=[1-9][0-9]*")
        self.assertNotIn("memory wait limit reached", errors)
        self.assertGreaterEqual(marker.stat().st_mtime, released_at)
        record = self.records()[-1]
        started = datetime.fromisoformat(str(record["started_at"]).replace("Z", "+00:00")).timestamp()
        self.assertGreaterEqual(started, released_at - 0.01)
        self.assertEqual(record["mem_wait_s"], int(output.split("wait=")[-1].strip()))

    def test_scope_memory_names_its_values_and_marks_missing_stalls_unmeasured(self) -> None:
        peak = self.peak_file()
        memory = recorder.peak_and_stall(str(peak))
        self.assertIsInstance(memory, recorder.StepScopeMemory)
        self.assertEqual(memory.peak_bytes, 123456)
        self.assertIs(memory.stall_some_s, sample.Unmeasured.VALUE)
        self.assertIs(memory.stall_full_s, sample.Unmeasured.VALUE)

    def test_scope_memory_uses_shared_pressure_parser_for_partial_counters(self) -> None:
        peak = self.base / "partial-pressure"
        _ = peak.write_text("123456\nsome avg10=0.00 total=1250000\n")
        memory = recorder.peak_and_stall(str(peak))
        self.assertEqual(memory.stall_some_s, 1.25)
        self.assertIs(memory.stall_full_s, sample.Unmeasured.VALUE)
        recorded = self.step(0, "0", "", str(peak), SWEEP)
        self.assertEqual(recorded["mem_stall_some_s"], 1.25)
        self.assertIsNone(recorded["mem_stall_full_s"])

    def test_step_accepts_older_peak_only_file_without_stalls(self) -> None:
        record = self.step(0, "0", "", str(self.peak_file()), SWEEP)
        self.assertEqual(record["peak_mem_bytes"], 123456)
        self.assertIsNone(record.get("mem_stall_some_s"))
        self.assertIsNone(record.get("mem_stall_full_s"))

    def test_passing_step_keeps_no_log(self) -> None:
        handoff = self.handoff()
        record = self.step(0, "0", str(handoff), "", NEXTEST)
        self.assertIsNone(record["log"])
        self.assertIsNone(record["peak_mem_bytes"])
        self.assertIsNone(record.get("mem_stall_some_s"))
        self.assertIsNone(record.get("mem_stall_full_s"))
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

    def test_session_prefers_codex_thread_id(self) -> None:
        cases: list[tuple[dict[str, str], str | None]] = [
            ({"CODEX_THREAD_ID": "thread-9", "CLAUDE_CODE_SESSION_ID": "session-1"}, "thread-9"),
            ({"CLAUDE_CODE_SESSION_ID": "session-1"}, "session-1"),
            ({}, None),
        ]
        for environment, expected in cases:
            with self.subTest(environment=environment):
                record = self.step(0, "0", "", "", SWEEP, environment)
                self.assertEqual(record["session"], expected)

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

    def test_verify_mac_step_without_git_records_the_sent_tree_key_and_call_id(self) -> None:
        mirror = self.base / "mirror"
        mirror.mkdir()
        sent_key = "a" * 64
        result = self.record(
            "step", "1", str(START), str(START + 1), "0", "", "", *NEXTEST,
            cwd=mirror,
            extra={"BUILDLOG_CALLER": "verify-mac", "BUILDLOG_CALL_ID": CALL_ID,
                   "BUILDLOG_SENT_TREE_KEY": sent_key},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        row = self.records()[-1]
        self.assertEqual((row["caller"], row["call_id"], row["step"]), ("verify-mac", CALL_ID, "nextest"))
        self.assertEqual((row["tree_key"], row["tree_changed"]), (sent_key, None))

    def assert_unverified_mac_step_uses_sent_key(self) -> None:
        # The fallback must work where the preceding step's guard does not apply.
        mirror = self.base / "mirror-control"
        mirror.mkdir()
        sent_key = "a" * 64
        result = self.record(
            "step", "1", str(START + 20), str(START + 21), "0", "", "", *NEXTEST,
            cwd=mirror, extra={"BUILDLOG_CALLER": "verify-mac", "BUILDLOG_SENT_TREE_KEY": sent_key},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        row = self.records()[-1]
        self.assertEqual((row["tree_key"], row["tree_changed"]), (sent_key, None))

    def test_local_step_with_changed_tree_does_not_use_the_sent_tree_key(self) -> None:
        end_key = self.key(*NEXTEST)
        start_key = "0" * 64
        self.assertNotEqual(start_key, end_key)
        row = self.step(0, "0", "", "", NEXTEST, {
            "BUILDLOG_CALLER": "verify", "BUILDLOG_TREE_START": start_key,
            "BUILDLOG_SENT_TREE_KEY": "a" * 64,
        })
        self.assertEqual((row["tree_key"], row["tree_changed"]), (None, True))
        self.assert_unverified_mac_step_uses_sent_key()

    def test_verify_mac_step_with_changed_tree_does_not_use_the_sent_tree_key(self) -> None:
        end_key = self.key(*NEXTEST)
        start_key = "0" * 64
        self.assertNotEqual(start_key, end_key)
        row = self.step(1, "0", "", "", NEXTEST, {
            "BUILDLOG_CALLER": "verify-mac", "BUILDLOG_TREE_START": start_key,
            "BUILDLOG_SENT_TREE_KEY": "a" * 64,
        })
        self.assertEqual((row["tree_key"], row["tree_changed"]), (None, True))
        self.assert_unverified_mac_step_uses_sent_key()

    def test_verify_mac_step_keeps_its_own_verified_key_over_a_different_sent_key(self) -> None:
        own_key = self.key(*NEXTEST)
        sent_key = "a" * 64
        self.assertNotEqual(own_key, sent_key)
        row = self.step(1, "0", "", "", NEXTEST, {
            "BUILDLOG_CALLER": "verify-mac", "BUILDLOG_TREE_START": own_key,
            "BUILDLOG_SENT_TREE_KEY": sent_key,
        })
        self.assertEqual((row["tree_key"], row["tree_changed"]), (own_key, False))
        self.assert_unverified_mac_step_uses_sent_key()

    def test_local_step_without_git_does_not_use_the_sent_tree_key(self) -> None:
        mirror = self.base / "local-without-git"
        mirror.mkdir()
        result = self.record(
            "step", "0", str(START), str(START + 1), "0", "", "", *NEXTEST,
            cwd=mirror, extra={"BUILDLOG_CALLER": "verify", "BUILDLOG_SENT_TREE_KEY": "a" * 64},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        row = self.records()[-1]
        self.assertEqual((row["tree_key"], row["tree_changed"]), (None, None))
        self.assert_unverified_mac_step_uses_sent_key()

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
        board = {"UNIT_TEAM_ROLE": "impl2", "UNIT_RUN_DIR": "/x/boards/run-7/"}
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
            "worker": "impl2",
            "unit_session": "run-7",
            "session": "session-1",
            "backfilled": False,
        }
        self.assertEqual({key: record[key] for key in expected}, expected)
        self.assertEqual(record["token_wait_s"], 0)
        started = datetime.fromisoformat(str(record["started_at"]))
        ended = datetime.fromisoformat(str(record["ended_at"]))
        self.assertAlmostEqual((ended - started).total_seconds(), 5.0, delta=0.002)

        final = self.record("call", "interrupted", "", "0", "0", "12", "", "0", "12", "final")
        self.assertEqual(final.returncode, 0, final.stderr)
        record = self.records()[-1]
        self.assertEqual((record["verb"], record["package"], record["status"], record["build_s"]), ("final", None, None, None))
        self.assertIs(record["cached"], False)
        self.assertNotEqual(record["id"], CALL_ID)

    def test_call_records_cargo_token_wait_separately_from_total_wait(self) -> None:
        result = self.record(
            "call", "ran", "0", "0", "9", "1", "", "0", "10", "test", "hana",
            extra={"BUILDLOG_TOKEN_WAIT_S": "4"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        call_record = self.records()[-1]
        self.assertEqual(call_record["token_wait_s"], 4)
        self.assertEqual(call_record["wait_s"], 9)

    def test_call_records_zero_token_wait_when_value_is_invalid(self) -> None:
        for value in ("", "abc"):
            with self.subTest(value=value):
                before = len(self.records())
                result = self.record(
                    "call", "ran", "0", "0", "9", "1", "", "0", "10", "test", "hana",
                    extra={"BUILDLOG_TOKEN_WAIT_S": value},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                records = self.records()
                self.assertEqual(len(records), before + 1)
                self.assertEqual(records[-1]["token_wait_s"], 0)

    def test_busy_call_stores_the_claim_what_as_mac_holder(self) -> None:
        result = self.record(
            "call", "ran", "0", "1", "0", "1", "1", "0", "1", "test", "hana",
            extra={"BUILDLOG_MAC": "declined", "BUILDLOG_MAC_REASON": "busy",
                   "BUILDLOG_MAC_S": "1", "BUILDLOG_MAC_HOLDER": "hana nextest"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.records()[-1]["mac_holder"], "hana nextest")

    def test_queue_bump_uses_the_hold_event_log_and_names_the_move(self) -> None:
        recorder.queue_bump("actor-id", "moved-id", "nextest", "/tmp/tree", 4, START)
        row = self.records()[-1]
        self.assertEqual({key: row[key] for key in ("kind", "actor", "session", "step", "folder", "old_position")},
                         {"kind": "queue_bump", "actor": "actor-id", "session": "moved-id", "step": "nextest",
                          "folder": "/tmp/tree", "old_position": 4})
        self.assertEqual(row["at"], store.utc_iso(START))
        self.assertTrue(store.host_file(store.host_name(), START).exists())

    def test_hold_event_keeps_the_key_and_the_locked_utc_time(self) -> None:
        result = self.record("hold-event", "began", "mac", "block-key", "hana", "render",
                             "2026-10-12T12:00:00.123456+00:00", "2026-10-12T14:00:00+00:00")
        self.assertEqual(result.returncode, 0, result.stderr)
        row = self.records()[-1]
        self.assertEqual(row, {
            "kind": "hold_event", "v": recorder.RECORD_VERSION,
            "at": "2026-10-12T12:00:00.123456Z", "host": socket.gethostname().split(".")[0],
            "target": "mac", "event": "began", "hold_key": "block-key", "holder": "hana",
            "reason": "render", "until": "2026-10-12T14:00:00+00:00",
        })
        result = self.record("hold-event", "ended", "mac", "block-key", "hana", "render",
                             "2026-10-12T14:00:00+00:00")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.records()[-1]["until"], "")

    def test_hold_event_write_failure_returns_zero_and_adds_no_event(self) -> None:
        with mock.patch.object(store, "append_line", side_effect=OSError("write failed")):
            self.assertEqual(recorder.main(["hold-event", "began", "natedev", "key", "hana", "render",
                                            "2026-10-12T12:00:00+00:00"]), 0)
        self.assertEqual(self.records(), [])
        self.assertIn("write failed", (self.root / "errors.log").read_text())

    def test_hold_event_refuses_invalid_or_naive_planned_ends_without_failing_its_caller(self) -> None:
        for until in ("later", "2026-10-12T14:00:00"):
            with self.subTest(until=until):
                result = self.record("hold-event", "began", "mac", "block-key", "hana", "render",
                                     "2026-10-12T12:00:00Z", until)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.records(), [])

    def test_hold_event_accepts_an_empty_planned_end(self) -> None:
        result = self.record("hold-event", "began", "mac", "block-key", "hana", "render",
                             "2026-10-12T12:00:00Z", "")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.records()[-1]["until"], "")

    def test_hold_build_deduplicates_lifetimes_and_holds_in_permanent_log(self) -> None:
        since = "2026-10-28T12:00:00+00:00"
        arguments = ["hold-build", "holder", since, "boot-one", "123", "456", "other_session",
                     "session", "cargo test", "/repo"]
        for _ in range(2):
            result = self.record(*arguments)
            self.assertEqual(result.returncode, 0, result.stderr)
        row, = self.records()
        self.assertEqual((row["kind"], row["hold_key"], row["pid"], row["proc_start"], row["class"]),
                         ("hold_build", "holder", 123, "456", "other_session"))
        self.assertEqual((row["session"], row["command"], row["cwd"]), ("session", "cargo test", "/repo"))
        arguments[3] = "boot-two"
        _ = self.record(*arguments)
        arguments[5] = "457"
        _ = self.record(*arguments)
        arguments[2] = "2026-10-28T13:00:00+00:00"
        _ = self.record(*arguments)
        self.assertEqual(len(self.records()), 4)

    def test_hold_build_refuses_holder_and_admitted_rows(self) -> None:
        for classification in ("holder", "admitted"):
            result = self.record("hold-build", "holder", "2026-10-28T12:00:00Z", "boot", "123", "456",
                                 classification, "session", "cargo test", "/repo")
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.records(), [])

    def test_call_records_mac_result_and_local_reason(self) -> None:
        local_reason = 'needs Linux path handling with a "quoted" mount'
        result = self.record(
            "call", "ran", "0", "0", "9", "1", "", "0", "10", "test", "hana",
            extra={
                "BUILDLOG_MAC": "passed",
                "BUILDLOG_MAC_REASON": "",
                "BUILDLOG_MAC_S": "12.5",
                "BUILDLOG_LOCAL_REASON": local_reason,
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        call_record = self.records()[-1]
        self.assertEqual(call_record["mac"], "passed")
        self.assertEqual(call_record["mac_reason"], "")
        self.assertEqual(call_record["mac_s"], 12.5)
        self.assertEqual(call_record["local_reason"], local_reason)

    def test_call_records_the_mac_copy_outcome_and_times(self) -> None:
        result = self.record(
            "call", "failed", "1", "0", "9", "100", "", "0", "110", "test", "hana",
            extra={
                "BUILDLOG_MAC_COPY": "won",
                "BUILDLOG_MAC_COPY_S": "41.5",
                "BUILDLOG_MAC_COPY_COUNT": "2",
                "BUILDLOG_MAC_COPY_NATEDEV_S": "95.25",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.records()[-1]
        self.assertEqual(
            [record[key] for key in ("copy", "copy_s", "copy_count", "copy_natedev_s")], ["won", 41.5, 2, 95.25]
        )

    def test_call_without_a_copy_records_empty_outcome_and_null_times(self) -> None:
        for value in ("", "abc", "-1", "nan"):
            with self.subTest(value=value):
                result = self.record(
                    "call", "ran", "0", "0", "9", "1", "", "0", "10", "test", "hana",
                    extra={
                        "BUILDLOG_MAC_COPY_S": value,
                        "BUILDLOG_MAC_COPY_COUNT": "" if value == "-1" else value,
                        "BUILDLOG_MAC_COPY_NATEDEV_S": value,
                    },
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                record = self.records()[-1]
                self.assertEqual(
                    [record[key] for key in ("copy", "copy_s", "copy_count", "copy_natedev_s")], ["", None, None, None]
                )

    def test_call_local_reason_defaults_to_empty(self) -> None:
        result = self.record(
            "call", "ran", "0", "0", "9", "1", "", "0", "10", "test", "hana"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.records()[-1]["local_reason"], "")

    def test_call_records_target_and_fractional_hold_wait(self) -> None:
        for target in ("mac", "natedev", ""):
            with self.subTest(target=target):
                result = self.record(
                    "call", "ran", "0", "0", "9", "1", "", "0", "10", "test", "hana",
                    extra={"BUILDLOG_TARGET": target, "BUILDLOG_HOLD_WAIT_S": "0.4"},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.records()[-1]["target"], target)
                self.assertEqual(self.records()[-1]["hold_wait_s"], 0.4)

    def test_call_records_null_hold_wait_for_invalid_numbers(self) -> None:
        for value in ("", "abc", "-0.4", "nan", "inf"):
            with self.subTest(value=value):
                result = self.record(
                    "call", "ran", "0", "0", "9", "1", "", "0", "10", "test", "hana",
                    extra={"BUILDLOG_HOLD_WAIT_S": value},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIsNone(self.records()[-1]["hold_wait_s"])

    def test_call_records_empty_target_and_null_hold_wait_when_unset(self) -> None:
        result = self.record("call", "ran", "0", "0", "9", "1", "", "0", "10", "test", "hana")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.records()[-1]["target"], "")
        self.assertIsNone(self.records()[-1]["hold_wait_s"])

    def test_call_records_null_mac_seconds_when_value_is_unusable(self) -> None:
        for value in ("", "abc", "-1"):
            with self.subTest(value=value):
                before = len(self.records())
                result = self.record(
                    "call", "ran", "0", "0", "9", "1", "", "0", "10", "test", "hana",
                    extra={
                        "BUILDLOG_MAC": "declined",
                        "BUILDLOG_MAC_REASON": "runner",
                        "BUILDLOG_MAC_S": value,
                    },
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                records = self.records()
                self.assertEqual(len(records), before + 1)
                self.assertEqual(records[-1]["mac"], "declined")
                self.assertEqual(records[-1]["mac_reason"], "runner")
                self.assertIsNone(records[-1]["mac_s"])

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
        self.assertEqual((failed["unit_session"], failed["repo_path"]), ("run-3", "/r/hana"))
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
