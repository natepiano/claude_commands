"""Merge gate suites contracts."""

from __future__ import annotations

import unittest
import io
import os
from pathlib import Path
import time
from typing import final
from contextlib import redirect_stdout
from unittest import mock
from scripts.production import merge_gate
from scripts.production.merge_gate_test_support import TEST_HANG_GUARD_SECS, gate_suites, wait_for_file
from scripts.production.merge_gate_suites_test_support import GateSuitesFixture


@final
class MergeGateResourcesTests(GateSuitesFixture):
    @gate_suites("lib")
    def test_memory_cap_admits_two_overlapping_module_processes(self) -> None:
        self.record_module_peaks({"lib/test_first.py": 48.0, "lib/test_second.py": 48.0})
        self.write(self.state / "barrier", "two")
        result = self.run_gate("--tests-only", "--memory-cap-mib", "268", ignored_signals=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        active: set[int] = set()
        peak = 0
        events = self.module_events()
        for event in events:
            if event["event"] == "start":
                active.add(event["pid"])
            else:
                active.remove(event["pid"])
            peak = max(peak, len(active))
        self.assertGreaterEqual(peak, 2)
        self.assertEqual(active, set())
        self.assertEqual(len(events), 2 * self.module_count)
        self.assertIn("sampled memory cap 268 MiB", result.stdout)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        self.assert_not_pushed()
        self.assertEqual(len([call for call in self.calls() if call["tool"] == "basedpyright"]), 1)
        self.assertFalse(any(call["tool"] == "ssh" for call in self.calls()))

    @gate_suites("lib", "hooks")
    def test_running_job_limit_bounds_overlap_without_dropping_modules(self) -> None:
        self.write(self.state / "concurrency-barrier", "held")
        started: list[merge_gate.RunningGateJob] = []
        start = merge_gate.start_gate_job
        sample = merge_gate.process_memory_mib

        def start_blocked(suite: str, path: Path, argv: list[str], repo: Path,
                          environment: dict[str, str], log: Path) -> merge_gate.RunningGateJob:
            job = start(suite, path, argv, repo, environment, log)
            started.append(job)
            wait_for_file(self.state / f"{job.process.pid}.started")
            return job

        def release_at_sample(roots: set[int]) -> dict[int, float]:
            # No job can finish during the start loop. Without the job limit,
            # memory admission would start more than two before this sample.
            for job in started:
                (self.state / f"{job.process.pid}.released").touch()
            return sample(roots)

        output = io.StringIO()
        with mock.patch.dict(os.environ, self.environment), \
                mock.patch.object(merge_gate, "start_gate_job", side_effect=start_blocked), \
                mock.patch.object(merge_gate, "process_memory_mib", side_effect=release_at_sample), \
                redirect_stdout(output):
            run = merge_gate.run_suites(self.checkout, max_running_jobs=2)
        self.assertTrue(run.passed, output.getvalue())
        expected = {(suite, "test_first.py") for suite in self.suites}
        expected.add(("lib", "test_second.py"))
        self.assertEqual({(job.suite, job.module) for job in run.jobs}, expected)
        self.assertEqual(len(run.jobs), self.module_count)
        active: set[int] = set()
        peak = 0
        events = self.module_events()
        for event in events:
            if event["event"] == "start":
                self.assertNotIn(event["pid"], active)
                active.add(event["pid"])
            else:
                active.remove(event["pid"])
            peak = max(peak, len(active))
            self.assertLessEqual(len(active), 2, events)
        self.assertEqual(peak, 2)
        self.assertEqual(active, set())
        self.assertEqual(len(events), 2 * self.module_count)
        self.assertRegex(output.getvalue(), r"(?m)^Gate tests: [\d.]+s; at most 2 modules at once; peak memory:")
        self.assertEqual(output.getvalue().count("at most 2 modules at once"), 1)

    @gate_suites("lib")
    def test_smaller_memory_cap_serializes_modules_without_dropping_any(self) -> None:
        # Each unrecorded job reserves 640 MiB, so this cap runs one module at a time;
        # 128 MiB stays far above the measured peak of the runner and one module (about 48 MiB).
        result = self.run_gate("--tests-only", "--memory-cap-mib", "128")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        events = self.module_events()
        self.assertEqual(len(events), 2 * self.module_count)
        for start, finish in zip(events[::2], events[1::2], strict=True):
            self.assertEqual((start["event"], finish["event"]), ("start", "finish"))
            self.assertEqual(start["pid"], finish["pid"])
        self.assertIn("sampled memory cap 128 MiB", result.stdout)

    def test_nonpositive_memory_cap_refuses_without_running_modules(self) -> None:
        result = self.run_gate("--tests-only", "--memory-cap-mib", "0")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.module_events(), [])

    def test_a_module_that_grows_past_the_cap_is_killed_and_the_gate_is_red(self) -> None:
        self.write(self.state / "grow-memory", "grow")
        result = self.run_gate("--tests-only", "--memory-cap-mib", "256")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"(?m)^hooks/test_first\.py killed: sampled memory [\d.]+ GiB is over the 0\.250 GiB cap$")
        self.assertRegex(result.stdout, r"(?m)^hooks: RED ")
        self.assertNotIn("never killed", result.stdout)
        starts = [row for row in self.module_events() if row["event"] == "start"]
        self.assertEqual(len(starts), self.module_count)

    def test_a_module_still_running_at_the_time_limit_is_killed_and_the_gate_is_red(self) -> None:
        self.write(self.state / "hold-module", "hold")
        self.environment["BUILDLOG_SCOPE"] = "1"
        output = io.StringIO()
        monotonic = time.monotonic
        deadline = monotonic() + TEST_HANG_GUARD_SECS
        sampled: set[int] = set()
        killed: list[int] = []
        kill = merge_gate.kill_job_group

        def module_clock() -> float:
            if monotonic() >= deadline:
                raise TimeoutError("gate did not stop the held module")
            ready = self.state / "module-held"
            return monotonic() + (2000 if ready.exists() and int(ready.read_text()) in sampled else 0)

        def sample(roots: set[int]) -> dict[int, float]:
            sampled.update(roots)
            return dict.fromkeys(roots, 1.0)

        def kill_module(job: merge_gate.RunningGateJob) -> None:
            killed.append(job.process.pid)
            kill(job)

        with mock.patch.dict(os.environ, self.environment), \
                mock.patch.object(merge_gate, "in_builds_slice", return_value=False), \
                mock.patch("scripts.production.merge_gate.time.monotonic", side_effect=module_clock), \
                mock.patch.object(merge_gate, "process_memory_mib", side_effect=sample), \
                mock.patch.object(merge_gate, "kill_job_group", side_effect=kill_module), redirect_stdout(output):
            result = merge_gate.run_suites(self.checkout)
        self.assertFalse(result.passed, output.getvalue())
        self.assertIn("hooks/test_first.py killed: still running after 1800 s, the module time limit", output.getvalue())
        self.assertRegex(output.getvalue(), r"(?m)^hooks: RED ")
        held = int((self.state / "module-held").read_text())
        self.assert_process_stopped(held)
        self.assertIn(held, sampled)
        self.assertEqual(set(killed), {held})
        scopes = [call for call in self.calls() if call["tool"] == "systemd-run" and call["args"][-1] != "true"]
        self.assertEqual([call["pid"] for call in scopes], [held])
        starts = [row for row in self.module_events() if row["event"] == "start"]
        self.assertEqual(len(starts), 1)

    def test_nonpositive_module_time_limit_refuses_without_running_modules(self) -> None:
        result = self.run_gate("--tests-only", "--module-time-limit-s", "0")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("module time limit must be positive", result.stdout + result.stderr)
        self.assertEqual(self.module_events(), [])

    @gate_suites("lib", "hooks")
    def test_sampled_overshoot_is_red_but_every_module_still_runs(self) -> None:
        output = io.StringIO()
        with mock.patch.dict(os.environ, self.environment), mock.patch.object(merge_gate, "process_memory_mib", return_value={os.getpid(): 100.0}), redirect_stdout(output):
            result = merge_gate.run_suites(self.checkout, 48)
        self.assertFalse(result.passed)
        self.assertEqual(len(result.jobs), self.module_count)
        self.assertIn("sampled memory exceeded the cap", output.getvalue())

    @gate_suites("lib", "hooks")
    def test_runner_reports_load_at_its_own_start_and_end(self) -> None:
        output = io.StringIO()
        calls = 0

        def measured_load() -> float:
            nonlocal calls
            calls += 1
            if calls == 1:
                return 10.0
            events = self.state / "modules.jsonl"
            return 20.0 if events.exists() and len(events.read_text().splitlines()) == 2 * self.module_count else 60.0

        with mock.patch.dict(os.environ, self.environment), mock.patch.object(merge_gate, "one_minute_load", side_effect=measured_load), redirect_stdout(output):
            result = merge_gate.run_suites(self.checkout)
        self.assertTrue(result.passed, output.getvalue())
        self.assertIn("One-minute load: start=10.00 max=60.00 end=20.00", output.getvalue())


if __name__ == "__main__":
    _ = unittest.main()
