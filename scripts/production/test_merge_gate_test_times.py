"""Keep gate records separate from the gate verdict and test output."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from collections.abc import Sequence
from datetime import datetime
import gzip
import io
import os
from pathlib import Path
import re
import time
from typing import final, override
import unittest
from unittest import mock

from scripts.buildlog import store
from scripts.build_hold import build_hold
from scripts.lint.memory_test_support import write_config_fixture
from scripts.production import merge_gate
from scripts.production.gate_modules import GateRefused
from scripts.production.merge_gate_test_support import MergeGateFixture, SUITE_MODULE, gate_suites
from scripts.test_times import gate_record


# The fixture reports fixed durations through unittest's duration hook.
# This tests the threshold without waits or limits on real test times.
FIXED_DURATION = '''
    def _addDuration(self, result: unittest.TestResult, elapsed: float) -> None:
        seconds = {"test_slow": 2.0, "test_slower": 4.0}.get(self._testMethodName, 0.25)
        result.addDuration(self, seconds)
'''

SLOW_TESTS = '''
    def test_fast(self) -> None:
        self.assertTrue(Path(os.environ["TMPDIR"]).is_dir())

    def test_slow(self) -> None:
        self.assertNotIn("TEST_TIMES_OUT", os.environ)

    def test_slower(self) -> None:
        self.assertEqual(Path.cwd() / "scripts" / suite, module.parent)
'''


def report_without_measurements(report: str) -> str:
    """Keep report text while removing changing time, memory and load values."""
    normalized = re.sub(r"\b\d+\.\d+(?=s?\b)", "<measurement>", report)

    def cpu_order(match: re.Match[str]) -> str:
        return match[1] + ", ".join(sorted(match[2].split(", "))) + match[3]

    return re.sub(r"(?m)^(Most CPU: )(.+)(; total .+)$", cpu_order, normalized)


@final
class MergeGateTestTimesTests(MergeGateFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        config = self.root / "config"
        self.environment["CONF_DIR"] = str(config)
        _ = write_config_fixture(config / "build_admission.conf")
        _ = write_config_fixture(config / "build_folders.conf")
        _ = write_config_fixture(config / "lint.conf")
        _ = write_config_fixture(config / "test_times.conf", "SLOW_TEST_S=1.0")

    def only_record(self) -> gate_record.GateRunRecord:
        records = self.gate_run_records()
        self.assertEqual(len(records), 1, records)
        return records[0]

    @gate_suites("hooks", "notify", "direct")
    def test_record_keeps_each_attempt_and_only_failed_or_slow_tests(self) -> None:
        self.configure_gate("merge_gate_governor_gib=5.5")
        for suite in self.suites:
            extra = SLOW_TESTS if suite == "direct" else ""
            self.write(self.checkout / "scripts" / suite / "test_first.py", SUITE_MODULE + FIXED_DURATION + extra)
        _ = self.commit(self.checkout, "fixed test durations")
        self.write(self.state / "fail-first-hooks", "fail")
        launches: list[tuple[list[str], Path, dict[str, str]]] = []
        finished: list[merge_gate.GateJobResult] = []
        start = merge_gate.start_gate_job
        finish = merge_gate.finish_gate_job

        def observe_start(suite: str, path: Path, argv: list[str], repo: Path,
                          environment: dict[str, str], log: Path, *,
                          kind: merge_gate.GateJobKind = merge_gate.GateJobKind.TEST_MODULE
                          ) -> merge_gate.RunningGateJob:
            job = start(suite, path, argv, repo, environment, log, kind=kind)
            # Supply a measured peak to test its path into the record.
            job.peak_memory_mib = 12.5
            if kind is merge_gate.GateJobKind.TEST_MODULE:
                launches.append((argv, log.parent, environment))
            return job

        def observe_finish(job: merge_gate.RunningGateJob, status: int) -> merge_gate.GateJobResult:
            result = finish(job, status)
            finished.append(result)
            return result

        with mock.patch.object(merge_gate, "start_gate_job", side_effect=observe_start), \
                mock.patch.object(merge_gate, "finish_gate_job", side_effect=observe_finish):
            result = self.run_gate("--tests-only", "--full")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = self.only_record()
        self.assertEqual((record["selection"], record["reason"], record["status"], record["aborted"]),
                         ("full", "--full", 0, False))
        self.assertEqual((record["checkout"], record["branch"], record["source"], record["dirty"]),
                         (str(self.checkout), "integration", "tests-only", False))
        self.assertEqual(record["sha"], self.git(self.checkout, "rev-parse", "HEAD"))
        self.assertEqual(record["tree"], self.git(self.checkout, "rev-parse", "HEAD^{tree}"))
        self.assertLessEqual(datetime.fromisoformat(record["started_at"]), datetime.fromisoformat(record["ended_at"]))
        for stamp in (record["started_at"], record["ended_at"]):
            self.assertEqual(stamp, store.utc_iso(datetime.fromisoformat(stamp).timestamp()))
        self.assertGreaterEqual(record["wall_s"], 0)
        self.assertEqual(record["hold_wait_s"], 0)
        self.assertGreater(record["max_jobs"], 0)
        self.assertEqual(record["cap_mib"], 5632)
        self.assertGreaterEqual(record["peak_mib"], 0)
        self.assertCountEqual([(suite["suite"], suite["status"]) for suite in record["suites"]],
                              [(suite, 0) for suite in self.suites])
        self.assertTrue(all(suite["wall_s"] >= 0 for suite in record["suites"]))
        modules = {(row["suite"], row["module"], row["attempt"]): row for row in record["modules"]}
        self.assertCountEqual(modules, [
            ("hooks", "test_first.py", 1), ("hooks", "test_first.py", 2),
            ("notify", "test_first.py", 1), ("direct", "test_first.py", 1),
            (*gate_record.TYPE_CHECK_JOB, 1),
        ])
        first = modules[("hooks", "test_first.py", 1)]
        retried = modules[("hooks", "test_first.py", 2)]
        self.assertEqual((first["status"], first["tests_run"], first["failures"], first["errors"], first["skipped"]),
                         (1, 1, 1, 0, 0))
        self.assertEqual(first["tests"], [{"test": "FixtureTest.test_gate_fixture", "s": 0.25, "status": "fail"}])
        self.assertEqual(first["tests_s"], 0.25)
        self.assertEqual((retried["status"], retried["tests_run"], retried["failures"], retried["tests_s"]), (0, 1, 0, 0.25))
        self.assertEqual(retried["tests"], [])
        kept = gzip.decompress((Path(self.environment["BUILDLOG_DIR"]) / first["log"]).read_bytes()).decode("utf-8")
        self.assertIn("Traceback (most recent call last):", kept)
        self.assertIn("AssertionError: fixture first run in hooks/test_first.py", kept)
        self.assertEqual(retried["log"], "")
        passing = modules[("notify", "test_first.py", 1)]
        self.assertEqual((passing["status"], passing["tests_run"], passing["tests_s"], passing["tests"]), (0, 1, 0.25, []))
        self.assertEqual(passing["log"], "")
        slow = modules[("direct", "test_first.py", 1)]
        self.assertEqual((slow["status"], slow["tests_run"], slow["tests_s"]), (0, 4, 6.5))
        self.assertEqual(slow["tests"], [
            {"test": "FixtureTest.test_slower", "s": 4.0, "status": "pass"},
            {"test": "FixtureTest.test_slow", "s": 2.0, "status": "pass"},
        ])
        for row in record["modules"]:
            execution = next(job for job in finished
                             if (job.suite, job.module, job.returncode) == (row["suite"], row["module"], row["status"]))
            self.assertEqual((row["wall_s"], row["cpu_s"]), (execution.seconds, execution.cpu_seconds))
            self.assertGreaterEqual(row["peak_mib"], 12.5)
        runner = str(self.checkout / "scripts/test_times/module_run.py")
        self.assertEqual(len(launches), 4)
        for argv, root, environment in launches:
            self.assertEqual(argv[1], runner)
            self.assertEqual(environment["TEST_TIMES_OUT"], str(root / "test-times.json"))
            self.assertFalse(root.exists())

    @gate_suites("hooks", "notify")
    def test_failed_module_tracebacks_survive_cleanup_and_passing_modules_keep_no_log(self) -> None:
        self.write(self.state / "red-hooks", "fail")
        result = self.run_gate("--tests-only", "--full")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        record = self.only_record()
        failed = [module for module in record["modules"] if module["suite"] == "hooks"]
        self.assertEqual([module["attempt"] for module in failed], [1, 2])
        self.assertEqual(len({module["log"] for module in failed}), 2)
        for module in failed:
            kept = gzip.decompress((Path(self.environment["BUILDLOG_DIR"]) / module["log"]).read_bytes()).decode("utf-8")
            self.assertIn("Traceback (most recent call last):", kept)
            self.assertIn("AssertionError: fixture failure in hooks/test_first.py", kept)
        passing, = [module for module in record["modules"] if module["suite"] == "notify"]
        self.assertEqual((passing["status"], passing["log"]), (0, ""))

    def test_a_dead_module_has_a_missing_times_reason_on_each_attempt(self) -> None:
        self.write(self.state / "signal-hooks", "always")
        result = self.run_gate("--tests-only", "--full")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        record = self.only_record()
        self.assertEqual((record["status"], record["aborted"]), (1, False))
        self.assertEqual([(suite["suite"], suite["status"]) for suite in record["suites"]], [("hooks", 1)])
        modules = [row for row in record["modules"] if (row["suite"], row["module"]) != gate_record.TYPE_CHECK_JOB]
        self.assertEqual([row["attempt"] for row in modules], [1, 2])
        for row in modules:
            self.assertNotEqual(row["status"], 0)
            self.assertIn("test-times.json", row["times_missing"])
            self.assertEqual((row["restart"], row["tests_run"], row["tests_s"], row["tests"]), ("", 0, 0.0, []))

    def test_full_and_reused_runs_keep_the_type_job_without_missing_times(self) -> None:
        first = self.run_gate("--full")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        full = self.only_record()
        self.assertEqual((full["selection"], full["source"], full["status"]), ("full", "checkpoint", 0))
        self.assertEqual(full["sha"], self.git(self.checkout, "rev-parse", "HEAD"))
        self.clear_gate_events()
        second = self.run_gate("--tests-only")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        records = self.gate_run_records()
        self.assertEqual(len(records), 2)
        reused = records[1]
        self.assertEqual((reused["selection"], reused["reason"], reused["source"], reused["status"]),
                         ("reused", "", "tests-only", 0))
        self.assertEqual(reused["suites"], [])
        self.assertEqual(self.module_events(), [])
        self.assertEqual(len(reused["modules"]), 1)
        for record in (full, reused):
            types, = [row for row in record["modules"] if (row["suite"], row["module"]) == gate_record.TYPE_CHECK_JOB]
            self.assertEqual((types["attempt"], types["status"], types["restart"], types["times_missing"]), (1, 0, "", ""))
            self.assertEqual((types["tests"], types["tests_run"], types["failures"], types["errors"], types["skipped"],
                              types["tests_s"]), ([], 0, 0, 0, 0, 0))

    def test_record_write_and_config_failures_keep_the_output_and_verdict(self) -> None:
        # Each compared run uses the same completed type environment.
        with mock.patch.dict(os.environ, self.environment), redirect_stdout(io.StringIO()):
            _ = merge_gate.typecheck_python()
        with mock.patch.object(merge_gate, "process_memory_mib", return_value={os.getpid(): 16.0}), \
                mock.patch.object(merge_gate, "one_minute_load", return_value=0.0):
            baseline = self.run_gate("--tests-only", "--full")
            self.assertEqual(baseline.returncode, 0, baseline.stdout + baseline.stderr)
            write = gate_record.write_gate_run
            blocked_log = self.root / "blocked-buildlog"
            self.write(blocked_log, "This file cannot hold a build log.\n")

            def blocked_write(run: gate_record.GateRun, executions: Sequence[gate_record.Execution]) -> None:
                with mock.patch.dict(os.environ, {"BUILDLOG_DIR": str(blocked_log)}):
                    write(run, executions)

            for failure in ("write", "config"):
                with self.subTest(failure=failure):
                    if failure == "write":
                        with mock.patch.object(gate_record, "write_gate_run", side_effect=blocked_write):
                            result = self.run_gate("--tests-only", "--full")
                    else:
                        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "test_times.conf",
                                                 "SLOW_TEST_S=invalid")
                        result = self.run_gate("--tests-only", "--full")
                    self.assertEqual(result.returncode, baseline.returncode, result.stdout + result.stderr)
                    self.assertEqual(report_without_measurements(result.stdout), report_without_measurements(baseline.stdout))
                    warnings = [line for line in result.stderr.splitlines() if line.startswith("gate run not written:")]
                    self.assertEqual(len(warnings), 1, result.stderr)
                    remaining = "".join(line + "\n" for line in result.stderr.splitlines()
                                        if not line.startswith(("gate run not written:", "warning:")))
                    self.assertEqual(remaining, baseline.stderr)
        self.assertEqual(len(self.gate_run_records()), 1)
        self.assertEqual(blocked_log.read_text(), "This file cannot hold a build log.\n")

    @gate_suites("hooks", "notify", "direct")
    def test_an_exception_keeps_completed_executions_and_leaves_with_the_same_exception(self) -> None:
        failure = RuntimeError("injected stop after a completed module")
        start = merge_gate.start_gate_job
        launched: list[merge_gate.RunningGateJob] = []
        self.write(self.checkout / "subject.txt", "dirty test tree\n")

        def stop_second_start(suite: str, path: Path, argv: list[str], repo: Path,
                              environment: dict[str, str], log: Path, *,
                              kind: merge_gate.GateJobKind = merge_gate.GateJobKind.TEST_MODULE
                              ) -> merge_gate.RunningGateJob:
            if launched:
                raise failure
            job = start(suite, path, argv, repo, environment, log, kind=kind)
            launched.append(job)
            return job

        with mock.patch.dict(os.environ, self.environment), \
                mock.patch.object(merge_gate, "start_gate_job", side_effect=stop_second_start), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(RuntimeError) as caught:
                _ = merge_gate.run_suites(self.checkout, max_running_jobs=1, full=True)
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(launched), 1)
        self.assertIsNotNone(launched[0].process.returncode)
        record = self.only_record()
        self.assertEqual((record["aborted"], record["status"], record["dirty"], record["tree"]), (True, 1, True, ""))
        self.assertEqual(len(record["modules"]), 1)
        module = record["modules"][0]
        self.assertEqual((module["suite"], module["module"], module["status"]),
                         (launched[0].suite, launched[0].module, 0))

    def test_a_hold_keeps_the_stopped_attempt_before_the_requeued_attempt(self) -> None:
        self.write(self.state / "signal-hooks", "once")
        start = merge_gate.start_gate_job
        held = False
        starts = 0
        hold_waits: list[float] = []

        def hold_first_start(suite: str, path: Path, argv: list[str], repo: Path,
                             environment: dict[str, str], log: Path, *,
                             kind: merge_gate.GateJobKind = merge_gate.GateJobKind.TEST_MODULE
                             ) -> merge_gate.RunningGateJob:
            nonlocal held, starts
            starts += 1
            held = starts == 1
            return start(suite, path, argv, repo, environment, log, kind=kind)

        def release_hold(hold: merge_gate.BuildHoldWait) -> None:
            nonlocal held
            if held:
                began = time.monotonic()
                held = False
                hold.seconds += time.monotonic() - began
                hold_waits.append(hold.seconds)

        with mock.patch.dict(os.environ, self.environment), \
                mock.patch.object(merge_gate, "start_gate_job", side_effect=hold_first_start), \
                mock.patch.object(merge_gate.BuildHoldWait, "admits", side_effect=lambda: not held), \
                mock.patch.object(merge_gate.BuildHoldWait, "wait_until_free", autospec=True, side_effect=release_hold), \
                redirect_stdout(io.StringIO()) as output:
            run = merge_gate.run_suites(self.checkout, full=True)
        self.assertTrue(run.passed, output.getvalue())
        record = self.only_record()
        self.assertEqual((record["aborted"], record["status"]), (False, 0))
        self.assertEqual(len(record["modules"]), 2)
        stopped, requeued = record["modules"]
        self.assertEqual((stopped["suite"], stopped["module"], stopped["attempt"], stopped["restart"]),
                         ("hooks", "test_first.py", 1, "hold"))
        self.assertNotEqual(stopped["status"], 0)
        self.assertEqual((requeued["attempt"], requeued["restart"], requeued["status"]), (2, "", 0))
        self.assertEqual([(suite["suite"], suite["status"]) for suite in record["suites"]], [("hooks", 0)])
        self.assertEqual(hold_waits, [record["hold_wait_s"]])
        self.assertGreater(record["hold_wait_s"], 0)
        self.assertGreaterEqual(record["wall_s"], 0)

    def test_a_failed_marker_unlink_does_not_write_an_aborted_record(self) -> None:
        marker = self.state / "runner" / "full-required"
        marker.mkdir(parents=True)
        with mock.patch.dict(os.environ, self.environment), redirect_stdout(io.StringIO()):
            with self.assertRaises(IsADirectoryError):
                _ = merge_gate.run_suites(self.checkout, full=True)
        record = self.only_record()
        self.assertEqual((record["aborted"], record["status"]), (False, 0))
        self.assertTrue(marker.is_dir())

    def test_a_hold_read_failure_keeps_the_finished_signal_attempt(self) -> None:
        self.write(self.state / "signal-hooks", "always")
        finish = merge_gate.finish_gate_job
        finished: list[merge_gate.GateJobResult] = []

        def fail_hold_read_after_finish(job: merge_gate.RunningGateJob, status: int) -> merge_gate.GateJobResult:
            result = finish(job, status)
            finished.append(result)
            _ = self.enterContext(mock.patch.object(build_hold, "natedev_holds",
                                                    side_effect=OSError("fixture hold state is unreadable")))
            return result

        with mock.patch.dict(os.environ, self.environment), \
                mock.patch.object(merge_gate, "finish_gate_job", side_effect=fail_hold_read_after_finish), \
                redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(GateRefused, "hold state cannot be read"):
                _ = merge_gate.run_suites(self.checkout, full=True)
        self.assertEqual(len(finished), 1)
        self.assertLess(finished[0].returncode, 0)
        record = self.only_record()
        self.assertEqual((record["aborted"], record["status"]), (True, 1))
        self.assertEqual([(row["suite"], row["module"], row["status"]) for row in record["modules"]],
                         [(finished[0].suite, finished[0].module, finished[0].returncode)])

    def test_an_abort_during_a_hold_records_the_current_wait(self) -> None:
        self.write(self.state / "signal-hooks", "once")
        finish = merge_gate.finish_gate_job
        admits = merge_gate.BuildHoldWait.admits
        completed = False
        wait_started: list[float] = []
        failure = GateRefused("fixture interrupts the hold wait")

        def finish_before_hold(job: merge_gate.RunningGateJob, status: int) -> merge_gate.GateJobResult:
            nonlocal completed
            result = finish(job, status)
            completed = True
            return result

        def admit_until_finished(hold: merge_gate.BuildHoldWait) -> bool:
            if completed:
                hold.waiting = True
                return False
            return admits(hold)

        def stop_current_wait(hold: merge_gate.BuildHoldWait) -> None:
            if not completed:
                return
            hold.started = time.monotonic()
            wait_started.append(hold.started)
            hold.waiting = True
            frozen = hold.started + 3.0
            _ = self.enterContext(mock.patch.object(time, "monotonic", return_value=frozen))
            raise failure

        before = time.monotonic()
        with mock.patch.dict(os.environ, self.environment), \
                mock.patch.object(merge_gate, "finish_gate_job", side_effect=finish_before_hold), \
                mock.patch.object(merge_gate.BuildHoldWait, "admits", autospec=True, side_effect=admit_until_finished), \
                mock.patch.object(merge_gate.BuildHoldWait, "wait_until_free", autospec=True, side_effect=stop_current_wait), \
                redirect_stdout(io.StringIO()):
            with self.assertRaises(GateRefused) as caught:
                _ = merge_gate.run_suites(self.checkout, full=True)
        self.assertIs(caught.exception, failure)
        record = self.only_record()
        self.assertEqual((record["aborted"], record["status"], record["hold_wait_s"]), (True, 1, 3.0))
        self.assertEqual([suite["status"] for suite in record["suites"]], [0])
        self.assertGreaterEqual(record["wall_s"], 0)
        self.assertLessEqual(record["wall_s"], wait_started[0] - before)
        self.assertEqual(len(record["modules"]), 1)


class BuildHoldWaitTests(unittest.TestCase):
    def test_waited_seconds_include_only_an_active_wait(self) -> None:
        hold = merge_gate.BuildHoldWait()
        hold.seconds = 4.0
        hold.started = 10.0
        hold.waiting = True
        with mock.patch.object(time, "monotonic", return_value=17.0):
            self.assertEqual(hold.waited_s(), 11.0)
        hold.waiting = False
        with mock.patch.object(time, "monotonic", side_effect=AssertionError("The wait has ended")):
            self.assertEqual(hold.waited_s(), 4.0)


if __name__ == "__main__":
    _ = unittest.main()
