"""Merge gate suites contracts."""

from __future__ import annotations

import unittest
import json
import contextlib
import io
import os
from pathlib import Path
from typing import cast, final
from unittest import mock
from scripts.production import merge_gate
from scripts.production.merge_gate_test_support import SUITE_MODULE, gate_suites
from scripts.production.merge_gate_suites_test_support import GateSuitesFixture


@final
class MergeGateSuitesTests(GateSuitesFixture):
    def test_recovery_suites_include_their_real_regression_modules(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        found = {(suite, merge_gate.test_module_name(repo, suite, path))
                 for suite, path in merge_gate.discover_test_modules(repo)}
        self.assertTrue({
            ("release", "test_release.py"), ("claude_to_codex", "test_sync.py"),
            ("new_rust_project", "test_rust_generate.py"), ("validate_and_push", "test_rerun.py"),
            ("exclude", "test_exclude.py"), ("prioritize", "tests/test_renumber.py"),
            ("prioritize", "tests/test_run_watcher.py"),
        }.issubset(found), found)

    @gate_suites("prioritize")
    def test_a_nested_suite_module_runs_and_retries_at_its_discovered_path(self) -> None:
        flat = self.checkout / "scripts/prioritize/test_first.py"
        nested = flat.parent / "tests/test_first.py"
        # The shared job fixture finds its suite from the checkout path.
        self.write(nested, SUITE_MODULE)
        flat.unlink()
        self.write(self.state / "signal-prioritize", "once")
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Rerun alone: prioritize/tests/test_first.py passed", result.stdout)
        self.assertEqual(len([row for row in self.module_events() if row["event"] == "start"]), 2)

    @gate_suites("types")
    def test_a_suite_named_types_runs_tests_and_reports_them_apart_from_the_type_check(self) -> None:
        module = self.checkout / "scripts/types/test_first.py"
        _ = module.rename(module.with_name("test_x.py"))
        self.write(self.state / "signal-types", "once")
        with mock.patch.dict(os.environ, self.environment), contextlib.redirect_stdout(io.StringIO()) as output:
            run = merge_gate.run_suites(self.checkout, type_dirs=("scripts",), full=True)
        report = output.getvalue()
        self.assertTrue(run.passed, report)
        self.assertCountEqual([(job.kind, job.suite, job.module, job.returncode) for job in run.jobs], [
            (merge_gate.GateJobKind.TEST_MODULE, "types", "test_x.py", 0),
            (merge_gate.GateJobKind.TYPE_CHECK, "types", "basedpyright", 0),
        ])
        self.assertEqual([(row["suite"], row["module"], row["event"]) for row in self.module_events()], [
            ("types", "test_x.py", "start"), ("types", "test_x.py", "start"), ("types", "test_x.py", "finish"),
        ])
        self.assertIn("Rerun alone: types/test_x.py passed", report)
        self.assertRegex(report, r"(?m)^types: OK \d+\.\d+s$")
        self.assertIn("basedpyright: OK", report)
        self.assertEqual(len([call for call in self.calls() if call["tool"] == "basedpyright"]), 1)
        self.assertIn("types/test_x.py", merge_gate.read_measures(self.state / "runner/module_seconds.json"))

    def test_a_test_file_directly_under_scripts_is_refused_before_jobs_start(self) -> None:
        self.write(self.checkout / "scripts/test_x.py", SUITE_MODULE)
        result = self.run_gate("--tests-only")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("test module must be in a suite folder under scripts/: test_x.py", result.stderr)
        self.assertEqual(self.module_events(), [])
        self.assertFalse(any(call["tool"] == "basedpyright" for call in self.calls()))

    @gate_suites("hooks", "notify")
    def test_folder_with_its_last_module_deleted_is_not_a_suite(self) -> None:
        (self.checkout / "scripts" / "notify" / "test_first.py").unlink()
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        starts = [(row["suite"], row["module"]) for row in self.module_events() if row["event"] == "start"]
        self.assertEqual(starts, [("hooks", "test_first.py")])
        self.assertRegex(result.stdout, r"(?m)^hooks: OK \d+\.\d+s$")
        self.assertNotRegex(result.stdout, r"(?m)^notify:")

    def test_modules_preserve_the_callers_pythonpath(self) -> None:
        caller_path = self.root / "caller-pythonpath"
        caller_path.mkdir()
        self.environment.update({"PYTHONPATH": str(caller_path), "MERGE_GATE_TEST_PYTHONPATH": str(caller_path),
                                 "MERGE_GATE_TEST_ISOLATION": "1"})
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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
        self.assertEqual(set(caller_log.iterdir()), {caller_log / "keep", caller_log / "admission"})
        self.assertEqual((caller_log / "keep").read_text(), "caller log")
        self.assertEqual(list((caller_log / "admission").glob("*.reservation")), [])
        self.assertEqual(list((caller_log / "admission").glob("*.waiting")), [])

    def test_tests_only_fails_when_reused_tests_have_a_red_type_job(self) -> None:
        first = self.run_gate("--tests-only")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        records = self.pass_records()
        self.clear_gate_events()
        self.write(self.state / "types-red", "red")

        result = self.run_gate("--tests-only")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("tests reused from", result.stdout)
        self.assertIn("basedpyright: RED", result.stdout)
        self.assertIn("fixture type error", result.stdout)
        self.assertEqual(len([call for call in self.calls() if call["tool"] == "basedpyright"]), 1)
        self.assertEqual(self.module_events(), [])
        self.assertEqual(self.pass_records(), records)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        self.assert_not_pushed()

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
        ordered = merge_gate.module_order(modules, timings, self.checkout)
        self.assertEqual([(suite, path.name) for suite, path in ordered[:2]],
                         [("lib", "test_second.py"), ("direct", "test_first.py")])
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        updated = cast(object, json.loads((self.state / "runner" / "module_seconds.json").read_text()))
        self.assertIsInstance(updated, dict)
        assert isinstance(updated, dict)
        self.assertEqual(len(cast(dict[str, object], updated)), self.module_count)

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


if __name__ == "__main__":
    _ = unittest.main()
