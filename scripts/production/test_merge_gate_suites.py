"""Merge gate suites contracts."""

from __future__ import annotations

import unittest

import json
import contextlib
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import cast, final
from contextlib import redirect_stdout
from unittest import mock
from scripts.production import merge_gate
from scripts.direct.wait_for_test_state import wait_until
from scripts.production.merge_gate_test_support import EXPECTED_SUITES, MergeGateFixture, TEST_HANG_GUARD_SECS, gate_suites, wait_for_file


@final
class MergeGateSuitesTests(MergeGateFixture):
    @gate_suites("lib")
    def test_memory_cap_admits_two_overlapping_module_processes(self) -> None:
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
        self.assertFalse(any(call["tool"] in ("basedpyright", "ssh") for call in self.calls()))

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
        # Two jobs need two 96 MiB reservations, so a cap under 192 MiB runs one module at a time;
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

    @gate_suites("hooks", "notify")
    def test_missing_suite_refuses_instead_of_silently_shrinking_gate(self) -> None:
        (self.checkout / "scripts" / "notify" / "test_first.py").unlink()
        result = self.run_gate("--tests-only")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("notify", result.stdout + result.stderr)
        self.assertEqual(self.module_events(), [])

    def test_modules_preserve_the_callers_pythonpath(self) -> None:
        caller_path = self.root / "caller-pythonpath"
        caller_path.mkdir()
        self.environment.update({"PYTHONPATH": str(caller_path), "MERGE_GATE_TEST_PYTHONPATH": str(caller_path),
                                 "MERGE_GATE_TEST_ISOLATION": "1"})
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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

    @gate_suites("lib")
    def test_modules_use_private_build_logs_and_remove_them(self) -> None:
        caller_log = self.root / "caller-buildlog"
        self.write(caller_log / "keep", "caller log")
        self.environment["BUILDLOG_DIR"] = str(caller_log)
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = [cast(dict[str, str], json.loads(line))
                for line in (self.state / "buildlogs.jsonl").read_text().splitlines()]
        self.assertEqual(len(rows), self.module_count)
        self.assertEqual(len({row["buildlog"] for row in rows}), self.module_count)
        for row in rows:
            self.assertTrue(Path(row["buildlog"]).is_relative_to(row["tmpdir"]))
            self.assertFalse(Path(row["tmpdir"]).exists())
        self.assertEqual(list(caller_log.iterdir()), [caller_log / "keep"])
        self.assertEqual((caller_log / "keep").read_text(), "caller log")

    @gate_suites("lib", "hooks")
    def test_red_modules_fail_again_print_both_outputs_and_reset(self) -> None:
        self.write(self.state / "red-lib", "red")
        self.write(self.state / "red-hooks", "red")
        result = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assert_not_pushed()
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")
        for suite, module in (("lib", "test_first.py"), ("lib", "test_second.py"), ("hooks", "test_first.py")):
            self.assertIn(f"fixture failure in {suite}/{module}", result.stdout + result.stderr)
            first = result.stdout.index(f"--- {suite}/{module} first run exit=1 ---")
            second = result.stdout.index(f"--- {suite}/{module} rerun alone exit=1 ---")
            self.assertLess(first, second)
            self.assertIn(f"fixture failure in {suite}/{module}", result.stdout[first:second])
            self.assertIn(f"fixture rerun in {suite}/{module}", result.stdout[second:])
        starts = [row for row in self.module_events() if row["event"] == "start"]
        self.assertEqual(len(starts), self.module_count + 3)

    @gate_suites("lib", "hooks")
    def test_failed_modules_pass_alone_and_keep_first_output_and_times(self) -> None:
        self.write(self.state / "fail-first-lib", "fail")
        self.environment["BUILDLOG_SCOPE"] = "1"
        output = io.StringIO()
        first_results: list[merge_gate.GateJobResult] = []
        finish = merge_gate.finish_gate_job

        def record_finish(job: merge_gate.RunningGateJob, status: int) -> merge_gate.GateJobResult:
            result = finish(job, status)
            first_results.append(result)
            return result

        with mock.patch.dict(os.environ, self.environment), \
                mock.patch.object(merge_gate, "in_builds_slice", return_value=False), \
                mock.patch.object(merge_gate, "finish_gate_job", side_effect=record_finish), redirect_stdout(output):
            run = merge_gate.run_suites(self.checkout, type_dirs=("scripts/production",))
        report = output.getvalue()
        self.assertTrue(run.passed, report)
        self.assertEqual(len(run.jobs), self.module_count + 1)
        for module in ("test_first.py", "test_second.py"):
            self.assertRegex(report, rf"Rerun alone: lib/{module} passed \(first run exit 1, [\d.]+ s\)")
            self.assertIn(f"--- lib/{module} passed on rerun alone; first run exit=1 ---", report)
            self.assertIn(f"fixture first run in lib/{module}", report)
        self.assertRegex(report, r"(?m)^lib: OK [\d.]+s$")
        starts = [row for row in self.module_events() if row["event"] == "start"]
        self.assertEqual(len(starts), self.module_count + 2)
        self.assertTrue(all(row["suite"] == "lib" for row in starts[self.module_count:]))
        events = self.module_events()
        first_rerun = events.index(starts[self.module_count])
        self.assertEqual(sum(row["event"] == "finish" for row in events[:first_rerun]), self.module_count - 2)
        self.assertEqual([row["event"] for row in events[first_rerun:]], ["start", "finish", "start", "finish"])
        runner = self.state / "runner"
        timings = merge_gate.read_measures(runner / "module_seconds.json")
        history = merge_gate.read_history(runner / "module_history.json")
        for first in first_results[:self.module_count + 1]:
            if first.suite != "types":
                key = f"{first.suite}/{first.module}"
                self.assertEqual(timings[key], first.seconds)
                self.assertEqual(history[key], [first.seconds])
        scopes = [call for call in self.calls() if call["tool"] == "systemd-run"]
        self.assertEqual(len(scopes), self.module_count + 3)
        flags = ["--user", "--scope", "--quiet", "--collect", "--expand-environment=no", "--slice=builds.slice", "--"]
        self.assertEqual(scopes[0]["args"], [*flags, "true"])
        for call in scopes[1:]:
            self.assertEqual(call["args"][:len(flags)], flags)
        for call in scopes[-2:]:
            self.assertEqual(sum(previous["args"] == call["args"] for previous in scopes), 2)
        self.assertFalse(any("basedpyright" in " ".join(call["args"]) for call in scopes))

    def test_scope_skips_disabled_failed_probe_and_existing_builds_slice(self) -> None:
        for reason in ("disabled", "probe", "existing"):
            with self.subTest(reason=reason):
                self.environment["MERGE_GATE_PASSED_DIR"] = str(self.state / ("passes-" + reason))
                self.environment["BUILDLOG_SCOPE"] = "0" if reason == "disabled" else "1"
                self.write(self.state / "scope-fails", "fail")
                (self.state / "tools.jsonl").unlink(missing_ok=True)
                output = io.StringIO()
                with mock.patch.dict(os.environ, self.environment), \
                        mock.patch.object(merge_gate, "TEST_SUITES", ("hooks",)), \
                        mock.patch.object(merge_gate, "in_builds_slice", return_value=reason == "existing"), \
                        redirect_stdout(output):
                    run = merge_gate.run_suites(self.checkout)
                self.assertTrue(run.passed, output.getvalue())
                scopes = [call for call in self.calls() if call["tool"] == "systemd-run"]
                self.assertEqual([call["args"][-1] for call in scopes], ["true"] if reason == "probe" else [])
                message = {"disabled": "BUILDLOG_SCOPE=0", "probe": "probe failed (exit 1)",
                           "existing": "gate already runs in builds.slice"}[reason]
                self.assertIn("Module scope skipped: " + message, output.getvalue())

    def test_scope_probe_timeout_or_missing_command_runs_without_prefix(self) -> None:
        for error in (subprocess.TimeoutExpired("systemd-run", 10), FileNotFoundError("systemd-run")):
            with self.subTest(error=type(error).__name__), mock.patch.dict(os.environ, {"BUILDLOG_SCOPE": "1"}), \
                    mock.patch.object(merge_gate, "in_builds_slice", return_value=False), \
                    mock.patch("scripts.production.merge_gate.subprocess.run", side_effect=error) as probe, \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(merge_gate.suite_scope_prefix(), [])
                self.assertEqual(probe.call_args.kwargs["timeout"], 10)
                self.assertIn("Module scope skipped: probe failed", output.getvalue())

    def test_builds_slice_detection_reads_the_gates_cgroup(self) -> None:
        for cgroup, expected in (("0::/user.slice/builds.slice/run-123.scope\n", True),
                                 ("0::/user.slice/app.slice/session.scope\n", False)):
            with self.subTest(cgroup=cgroup), mock.patch.object(Path, "read_text", autospec=True, return_value=cgroup) as read:
                self.assertEqual(merge_gate.in_builds_slice(), expected)
                read.assert_called_once_with(Path("/proc/self/cgroup"))

    @gate_suites(*EXPECTED_SUITES[:merge_gate.RERUN_LIMIT])
    def test_more_than_the_rerun_limit_keeps_all_first_failures(self) -> None:
        failed_suites = self.suites[:merge_gate.RERUN_LIMIT]
        for suite in failed_suites:
            self.write(self.state / ("red-" + suite), "red")
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"{merge_gate.RERUN_LIMIT + 1} modules failed; this is a real breakage, not load", result.stdout)
        self.assertNotIn("Rerun alone:", result.stdout)
        starts = [row for row in self.module_events() if row["event"] == "start"]
        self.assertEqual(len(starts), self.module_count)

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
                mock.patch.object(merge_gate, "TEST_SUITES", ("hooks",)), \
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

    def test_a_sample_counts_swapped_memory(self) -> None:
        with mock.patch.object(merge_gate, "swapped_kib", return_value=1024 * 1024):
            sample = merge_gate.process_memory_mib({os.getpid()})
        self.assertGreater(sample[os.getpid()], 1024.0)

    def test_the_type_check_venv_is_built_once_with_the_pinned_stubs(self) -> None:
        with mock.patch.dict(os.environ, self.environment):
            first, second = merge_gate.typecheck_python(), merge_gate.typecheck_python()
        venv = self.state / "runner" / "typecheck-venv"
        self.assertEqual((first, second), (venv / "bin" / "python", venv / "bin" / "python"))
        self.assertEqual([call["args"] for call in self.calls() if call["tool"] == "uv"],
                         [["venv", "--clear", str(venv)],
                          ["pip", "install", "--python", str(venv / "bin" / "python"),
                           *merge_gate.TYPECHECK_REQUIREMENTS]])

    @gate_suites("lib")
    def test_a_type_check_venv_that_cannot_be_built_is_red_and_names_the_command(self) -> None:
        self.write(self.state / "uv-fails", "fail")
        result = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("basedpyright: RED\ntype-check venv missing: fixture: no network; build it with: ", result.stdout)
        self.assertIn(" pip install --python ", result.stdout)
        self.assertEqual([call for call in self.calls() if call["tool"] == "basedpyright"], [])
        self.assertRegex(result.stdout, r"(?m)^lib: OK \d+\.\d+s$")
        self.assert_not_pushed()

    def test_type_failure_resets_merge_and_does_not_push(self) -> None:
        self.write(self.state / "types-red", "red")
        result = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("fixture type error", result.stdout + result.stderr)
        self.assert_not_pushed()
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")

    def test_red_gate_restores_tracked_debris_before_rollback(self) -> None:
        self.write(self.state / "modify-tracked", "modify")
        result = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")
        self.assertIn("merge undone", result.stderr)
        self.assert_not_pushed()

    def test_failed_rollback_names_merge_commit_and_blocking_files(self) -> None:
        with mock.patch.object(merge_gate, "git", return_value=subprocess.CompletedProcess([], 1, " M unit.txt", "blocked")), mock.patch.object(
                merge_gate, "checked_git", side_effect=["merge-commit", "unit.txt\0"]):
            with self.assertRaisesRegex(merge_gate.GateRefused, "merge NOT undone; merge commit merge-commit; blocking files: M unit.txt"):
                merge_gate.rollback_merge(self.checkout, self.base)

    def test_finished_module_leaves_no_running_child(self) -> None:
        self.write(self.state / "leak-child", "leak")
        result = self.run_gate("--tests-only")
        child = int((self.state / "child-pid").read_text())
        try:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assert_process_stopped(child)
        finally:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_explicit_fetch_updates_refs_without_matching_remote_configuration(self) -> None:
        _ = self.git(self.checkout, "config", "--unset-all", "remote.origin.fetch")
        _ = self.git(self.checkout, "update-ref", "refs/remotes/origin/unit-phase", self.base)
        result = self.run_gate("--promote")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.checkout, "rev-parse", "origin/unit-phase"), self.checkpoint)

    @gate_suites("lib", "direct")
    def test_recorded_durations_start_unknown_then_slowest_modules(self) -> None:
        timings = {f"{suite}/test_first.py": 1.0 for suite in self.suites}
        timings["direct/test_first.py"] = 100.0
        self.write(self.state / "runner" / "module_seconds.json", json.dumps(timings))
        scripts = self.checkout / "scripts"
        modules = [*((suite, scripts / suite / "test_first.py") for suite in self.suites),
                   ("lib", scripts / "lib" / "test_second.py")]
        ordered = merge_gate.module_order(modules, timings)
        self.assertEqual([(suite, path.name) for suite, path in ordered[:2]],
                         [("lib", "test_second.py"), ("direct", "test_first.py")])
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        updated = cast(object, json.loads((self.state / "runner" / "module_seconds.json").read_text()))
        self.assertIsInstance(updated, dict)
        assert isinstance(updated, dict)
        self.assertEqual(len(cast(dict[str, object], updated)), self.module_count)

    def test_termination_signals_roll_back_and_clean_module_groups(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(sig=sig):
                child = 0
                (self.state / "module-held").unlink(missing_ok=True)
                self.write(self.state / "hold-module", "hold")
                process = subprocess.Popen(
                    [*self.gate_command(), "--repo", str(self.checkout), "--merge-branch", "integration",
                     "--unit-branch", "unit-phase", "--checkpoint", self.checkpoint, "--previous-checkpoint", self.base,
                     "--unit", "fixture", "--phase", "repair", "--settings-blob", self.settings_blob,
                     "--production", str(self.quiet_production), "--type-dirs", "scripts"], env=self.environment, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    wait_until(lambda: (self.state / "module-held").exists() or process.poll() is not None,
                               self.state, timeout=TEST_HANG_GUARD_SECS, processes=(process,))
                    if not (self.state / "module-held").exists():
                        self.fail("fixture module never became ready")
                    child = int((self.state / "module-held").read_text())
                    process.send_signal(sig)
                    output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                    self.assertEqual(process.returncode, 1, output + errors)
                    self.assertIn("merge undone", errors)
                    self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
                    self.assertFalse(Path(f"/proc/{child}").exists())
                    self.assert_not_pushed()
                finally:
                    if process.poll() is None:
                        process.kill()
                    _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                    if child:
                        try:
                            os.killpg(child, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    _ = self.git(self.checkout, "reset", "--keep", self.base)

    def test_signal_during_merge_aborts_pending_merge_and_its_hook(self) -> None:
        ready = self.state / "merge-hook-ready"
        hook = self.checkout / ".git" / "hooks" / "prepare-commit-msg"
        # The pid is published by rename, so the test never reads a created but
        # still empty file.
        self.write(hook, f"#!{sys.executable}\nimport os, signal\nfrom pathlib import Path\n"
                   + f"Path({str(ready) + '.tmp'!r}).write_text(str(os.getpid()))\n"
                   + f"os.replace({str(ready) + '.tmp'!r}, {str(ready)!r})\nsignal.pause()\n")
        hook.chmod(0o755)
        process = subprocess.Popen(
            [*self.gate_command(), "--repo", str(self.checkout), "--merge-branch", "integration",
             "--unit-branch", "unit-phase", "--checkpoint", self.checkpoint, "--previous-checkpoint", self.base,
             "--unit", "fixture", "--phase", "repair", "--settings-blob", self.settings_blob,
             "--production", str(self.quiet_production)],
            env=self.environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        hook_pid = 0
        try:
            wait_until(lambda: ready.exists() or process.poll() is not None,
                       self.state, timeout=TEST_HANG_GUARD_SECS, processes=(process,))
            if not ready.exists():
                self.fail("merge hook did not become ready")
            hook_pid = int(ready.read_text())
            process.send_signal(signal.SIGTERM)
            output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(process.returncode, 1, output + errors)
            self.assertIn("merge undone", errors)
            self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
            self.assertFalse((self.checkout / ".git" / "MERGE_HEAD").exists())
            self.assert_process_stopped(hook_pid)
            self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")
            self.assert_not_pushed()
        finally:
            if process.poll() is None:
                process.kill()
            _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            if hook_pid:
                try:
                    os.kill(hook_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_sampled_admission_learns_headroom_and_reserves_between_scans(self) -> None:
        admission = merge_gate.SampledRSSAdmission(512)
        admission.observe(100.0, 300.0, {1: 100.0, 2: 30.0})
        self.assertTrue(admission.admits(True))
        admission.reserve(3)
        self.assertFalse(admission.admits(True))
        self.assertTrue(admission.admits(False))
        admission.observe(101.0, 450.0, {1: 80.0})
        self.assertFalse(admission.admits(True))
        self.assertEqual(admission.headroom_mib, 100.0)
        self.assertEqual(admission.peak_mib, 450.0)

    def test_starting_job_keeps_its_reservation_after_a_small_rss_sample(self) -> None:
        admission = merge_gate.SampledRSSAdmission(256)
        admission.observe(100.0, 24.0, {})
        admission.reserve(1)
        admission.reserve(2)
        self.assertFalse(admission.admits(True))
        admission.observe(100.25, 40.0, {1: 8.0, 2: 8.0})
        self.assertEqual(admission.reserved_mib, 176.0)
        self.assertFalse(admission.admits(True))
        # At the reservation size, real RSS replaces padding without lowering
        # the charged total; a completed job releases its padding.
        admission.observe(100.5, 128.0, {1: 96.0, 2: 8.0})
        self.assertEqual(admission.reserved_mib, 88.0)
        self.assertFalse(admission.admits(True))
        admission.release(2)
        self.assertEqual(admission.reserved_mib, 0.0)
        self.assertTrue(admission.admits(True))

    def test_larger_job_peak_increases_every_running_jobs_reservation(self) -> None:
        admission = merge_gate.SampledRSSAdmission(512)
        admission.observe(100.0, 280.0, {1: 240.0, 2: 16.0})
        self.assertEqual(admission.headroom_mib, 240.0)
        self.assertEqual(admission.reserved_mib, 224.0)
        self.assertFalse(admission.admits(True))

    def test_a_job_reserved_at_a_known_size_holds_it_and_leaves_module_headroom_alone(self) -> None:
        admission = merge_gate.SampledRSSAdmission(4096)
        admission.observe(100.0, 0.0, {})
        admission.reserve(1, 3072.0)
        self.assertEqual(admission.reserved_mib, 3072.0)
        self.assertTrue(admission.admits(True))
        admission.observe(100.25, 2700.0, {1: 2700.0})
        self.assertEqual(admission.headroom_mib, 96.0)
        self.assertEqual(admission.reserved_mib, 372.0)
        self.assertTrue(admission.admits(True))
        admission.release(1)
        self.assertEqual(admission.fixed_mib, {})

    def test_a_module_three_times_slower_than_its_record_and_a_minute_over_is_slow(self) -> None:
        self.assertTrue(merge_gate.slower_than_recorded(1264.0, 49.0))
        self.assertTrue(merge_gate.slower_than_recorded(90.0, 30.0))
        self.assertFalse(merge_gate.slower_than_recorded(89.0, 30.0))
        self.assertFalse(merge_gate.slower_than_recorded(12.0, 1.0))

    @gate_suites("lib", "hooks")
    def test_the_report_names_a_module_slower_than_its_record(self) -> None:
        runner = self.state / "runner"
        runner.mkdir()
        self.write(runner / "module_history.json", json.dumps({
            "lib/test_second.py": [0.0, 0.0, 1000.0], "hooks/test_first.py": [1000.0] * 5}))
        with mock.patch.object(merge_gate, "SLOW_MODULE_FLOOR_S", 0.0), \
                mock.patch.dict(os.environ, self.environment), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            run = merge_gate.run_suites(self.checkout)
        self.assertTrue(run.passed, output.getvalue())
        self.assertRegex(output.getvalue(), r"(?m)^Slow module: lib/test_second\.py \d+ s, median of the last 3 runs 0 s \(3x or more\)$")
        history = cast(dict[str, list[float]], json.loads((runner / "module_history.json").read_text()))
        self.assertEqual(history["lib/test_second.py"][:3], [0.0, 0.0, 1000.0])
        self.assertEqual(len(history["lib/test_second.py"]), 4)
        self.assertEqual(history["hooks/test_first.py"][:4], [1000.0] * 4)
        self.assertEqual(len(history["hooks/test_first.py"]), 5)
        self.assertNotIn("Slow module: hooks/", output.getvalue())

    def test_memory_scan_schedule_uses_controlled_quarter_second_clock(self) -> None:
        admission = merge_gate.SampledRSSAdmission(3072)
        self.assertTrue(admission.sample_due(10.0))
        admission.observe(10.0, 30.0, {})
        for tick in (10.05, 10.10, 10.15, 10.20):
            self.assertFalse(admission.sample_due(tick))
        self.assertTrue(admission.sample_due(10.25))

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
