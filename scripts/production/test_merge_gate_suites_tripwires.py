"""Merge gate suites contracts."""

from __future__ import annotations

import unittest
import json
import io
import os
from pathlib import Path
import signal
import sys
from typing import cast, final
from contextlib import redirect_stdout
from unittest import mock
from scripts.production import merge_gate
from scripts.production.merge_gate_test_support import gate_suites
from scripts.production.merge_gate_suites_test_support import GateSuitesFixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@final
class MergeGateTripwiresTests(GateSuitesFixture):
    def test_module_tool_calls_fail_even_when_its_tests_pass(self) -> None:
        commands = [["cargo", "check"], ["cargo", "metadata"], ["rustc", "--version"],
                    ["rustdoc", "--version"], ["rustup", "show"], ["cargo-nextest", "--version"]]
        self.write(self.state / "tool-commands.json", json.dumps(commands))
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(json.loads((self.state / "tool-statuses.json").read_text()), [99] * len(commands))
        for tool, argument in commands:
            self.assertIn(f"test_first.py: a test started real {tool}: {argument}", result.stdout)
        events = [event for event in self.module_events() if event["suite"] == "hooks"]
        self.assertEqual([event["event"] for event in events], ["start", "finish"])
        self.assertNotIn("Rerun alone:", result.stdout)

    def test_module_can_put_its_own_build_tool_stub_first(self) -> None:
        self.write(self.state / "tool-commands.json", json.dumps([["cargo", "check"]]))
        self.write(self.state / "own-tool-stubs", "stub cargo")
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads((self.state / "tool-statuses.json").read_text()), [0])
        self.assertNotIn("a test started real", result.stdout)

    def test_recorded_memory_readings_fail_a_successful_job_and_name_each_reading(self) -> None:
        root = self.root / "recorded-memory-job"
        root.mkdir()
        records = [
            {"reading": "/proc/meminfo", "argv": ["python3", "memory checker.py", "check"], "pid": 123},
            {"reading": "/proc/pressure/memory", "argv": ["python3", "queue.py", "two words"], "pid": 456},
        ]
        self.write(root / "live-memory-reads.jsonl", "".join(json.dumps(record) + "\n" for record in records))
        job = merge_gate.start_gate_job(
            "hooks", Path("test_memory_reader.py"), [sys.executable, "-c", "print('module passed')"],
            self.checkout, self.environment, root / "output.log")
        status = job.process.wait(timeout=TEST_HANG_GUARD_SECS)
        result = merge_gate.finish_gate_job(job, status)
        self.assertEqual(status, 0)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.tripwire_tripped)
        self.assertIn("module passed", result.output)
        self.assertIn(
            "test_memory_reader.py: a test read live memory /proc/meminfo: python3 'memory checker.py' check",
            result.output)
        self.assertIn(
            "test_memory_reader.py: a test read live memory /proc/pressure/memory: python3 queue.py 'two words'",
            result.output)

    def test_unreadable_memory_records_fail_the_job_without_stopping_the_runner(self) -> None:
        lines = ['{"reading": "/proc/meminfo", "argv": [', 'null', '{}',
                 '{"reading": "/proc/meminfo", "argv": [7], "pid": 123}']
        for index, line in enumerate(lines):
            with self.subTest(line=line):
                root = self.root / f"unreadable-memory-job-{index}"
                root.mkdir()
                self.write(root / "live-memory-reads.jsonl", line)
                job = merge_gate.start_gate_job(
                    "hooks", Path("test_memory_reader.py"), [sys.executable, "-c", "print('module passed')"],
                    self.checkout, self.environment, root / "output.log")
                status = job.process.wait(timeout=TEST_HANG_GUARD_SECS)
                result = merge_gate.finish_gate_job(job, status)
                self.assertEqual(status, 0)
                self.assertEqual(result.returncode, 1)
                self.assertTrue(result.tripwire_tripped)
                self.assertIn("module passed", result.output)
                self.assertIn(
                    f"test_memory_reader.py: a test read live memory (unreadable record): {line}", result.output)
                self.assertFalse(root.exists())

    @gate_suites("hooks")
    def test_memory_tripwire_survives_sigterm_during_a_hold_without_retry(self) -> None:
        module = self.checkout / "scripts" / "hooks" / "test_first.py"
        self.write(module, module.read_text() + '''

first_run = state / "memory-reader-started"
if not first_run.exists():
    first_run.touch()
    Path(os.environ["BUILDLOG_LIVE_MEMORY_TRIPWIRE"]).write_text(json.dumps({
        "reading": "/proc/pressure/memory", "argv": ["python3", "reader.py"], "pid": os.getpid()}) + "\\n")
    os.kill(os.getpid(), signal.SIGTERM)
''')
        held = False
        starts: list[str] = []
        statuses: list[int] = []
        start = merge_gate.start_gate_job
        finish = merge_gate.finish_gate_job

        def start_during_hold(suite: str, path: Path, argv: list[str], repo: Path,
                              environment: dict[str, str], log: Path) -> merge_gate.RunningGateJob:
            nonlocal held
            held = True
            starts.append(path.name)
            return start(suite, path, argv, repo, environment, log)

        def release_hold() -> None:
            nonlocal held
            held = False

        def record_finish(job: merge_gate.RunningGateJob, status: int) -> merge_gate.GateJobResult:
            self.assertTrue(held)
            statuses.append(status)
            return finish(job, status)

        output = io.StringIO()
        with mock.patch.dict(os.environ, self.environment), \
                mock.patch.object(merge_gate.BuildHoldWait, "admits", side_effect=lambda: not held), \
                mock.patch.object(merge_gate.BuildHoldWait, "wait_until_free", side_effect=release_hold), \
                mock.patch.object(merge_gate, "start_gate_job", side_effect=start_during_hold), \
                mock.patch.object(merge_gate, "finish_gate_job", side_effect=record_finish), redirect_stdout(output):
            run = merge_gate.run_suites(self.checkout)
        self.assertFalse(run.passed, output.getvalue())
        self.assertEqual(starts, ["test_first.py"])
        self.assertEqual(statuses, [-signal.SIGTERM])
        self.assertEqual(len(run.jobs), 1)
        self.assertEqual(run.jobs[0].returncode, 1)
        self.assertTrue(run.jobs[0].tripwire_tripped)
        self.assertIn(
            "test_first.py: a test read live memory /proc/pressure/memory: python3 reader.py", output.getvalue())
        self.assertNotIn("queued again", output.getvalue())
        self.assertNotIn("Rerun alone:", output.getvalue())

    def test_absent_or_empty_memory_record_preserves_the_module_status(self) -> None:
        for record_state in ("absent", "empty"):
            for exit_status in (0, 7):
                with self.subTest(record=record_state, status=exit_status):
                    root = self.root / f"memory-job-{record_state}-{exit_status}"
                    root.mkdir()
                    if record_state == "empty":
                        self.write(root / "live-memory-reads.jsonl", "")
                    job = merge_gate.start_gate_job(
                        "hooks", Path("test_memory_reader.py"),
                        [sys.executable, "-c", f"raise SystemExit({exit_status})"],
                        self.checkout, self.environment, root / "output.log")
                    status = job.process.wait(timeout=TEST_HANG_GUARD_SECS)
                    result = merge_gate.finish_gate_job(job, status)
                    self.assertEqual(status, exit_status)
                    self.assertEqual(result.returncode, exit_status)
                    self.assertFalse(result.tripwire_tripped)
                    self.assertNotIn("a test read live memory", result.output)

    @gate_suites("lib")
    def test_passing_module_memory_record_fails_once_with_private_paths_and_no_types_tripwire(self) -> None:
        caller_record = self.root / "caller-memory-reads.jsonl"
        self.environment["BUILDLOG_LIVE_MEMORY_TRIPWIRE"] = str(caller_record)
        for name in ("test_first.py", "test_second.py"):
            module = self.checkout / "scripts" / "lib" / name
            self.write(module, module.read_text() + '''

class MemoryRecordTest(unittest.TestCase):
    def test_record_path(self):
        record = Path(os.environ["BUILDLOG_LIVE_MEMORY_TRIPWIRE"])
        (state / (module.name + ".memory-path.json")).write_text(json.dumps({
            "record": str(record), "tmpdir": os.environ["TMPDIR"]}))
        if module.name == "test_first.py":
            record.write_text(json.dumps({"reading": "/proc/pressure/memory",
                "argv": ["python3", "memory checker.py", "check"], "pid": os.getpid()}) + "\\n")
''')
        self.write(self.bin / "basedpyright", "#!" + sys.executable + '''
import json
import os
from pathlib import Path
state = Path(os.environ["MERGE_GATE_TEST_STATE"])
(state / "types-memory-environment.json").write_text(json.dumps({
    "tripwire_present": "BUILDLOG_LIVE_MEMORY_TRIPWIRE" in os.environ}))
print("0 errors, 0 warnings, 0 notes")
raise SystemExit(3)
''')
        module_statuses: list[tuple[str, int]] = []
        finish = merge_gate.finish_gate_job

        def record_finish(job: merge_gate.RunningGateJob, status: int) -> merge_gate.GateJobResult:
            if job.suite == "lib":
                module_statuses.append((job.path.name, status))
            return finish(job, status)

        with mock.patch.object(merge_gate, "finish_gate_job", side_effect=record_finish):
            result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(
            "test_first.py: a test read live memory /proc/pressure/memory: python3 'memory checker.py' check",
            result.stdout)
        self.assertNotIn("test_second.py: a test read live memory", result.stdout)
        self.assertNotIn("Rerun alone:", result.stdout)
        self.assertCountEqual(module_statuses, [("test_first.py", 0), ("test_second.py", 0)])
        for name in ("test_first.py", "test_second.py"):
            events = [event["event"] for event in self.module_events() if event["module"] == name]
            self.assertEqual(events, ["start", "finish"])
        rows = [cast(dict[str, str], json.loads((self.state / (name + ".memory-path.json")).read_text()))
                for name in ("test_first.py", "test_second.py")]
        self.assertNotEqual(rows[0]["record"], rows[1]["record"])
        for row in rows:
            self.assertEqual(Path(row["record"]), Path(row["tmpdir"]) / "live-memory-reads.jsonl")
            self.assertNotEqual(Path(row["record"]), caller_record)
        types_environment = cast(dict[str, bool], json.loads((self.state / "types-memory-environment.json").read_text()))
        self.assertFalse(types_environment["tripwire_present"])
        self.assertFalse(caller_record.exists())


if __name__ == "__main__":
    _ = unittest.main()
