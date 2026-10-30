"""Merge gate suites contracts."""

from __future__ import annotations

import unittest
import io
import os
import subprocess
from typing import final
from contextlib import redirect_stdout
from unittest import mock
from scripts.production import merge_gate
from scripts.production.merge_gate_test_support import EXPECTED_SUITES, gate_suites
from scripts.production.merge_gate_suites_test_support import GateSuitesFixture


@final
class MergeGateRerunsTests(GateSuitesFixture):
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


if __name__ == "__main__":
    _ = unittest.main()
