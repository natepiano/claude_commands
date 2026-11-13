"""Merge gate suites contracts."""

from __future__ import annotations

import unittest
import json
import contextlib
import io
import os
import sys
from pathlib import Path
from typing import cast, final
from unittest import mock
from scripts.production import affected_modules, merge_gate
from scripts.lint.memory_test_support import write_config_fixture
from scripts.production.gate_modules import discover_test_modules, test_module_name
from scripts.production.merge_gate_test_support import SUITE_MODULE, gate_suites, install_test_runner
from scripts.production.merge_gate_suites_test_support import GateSuitesFixture


@final
class MergeGateSuitesTests(GateSuitesFixture):
    @gate_suites("hooks", "message")
    def test_targeted_selection_precedes_the_route_and_survives_fallback(self) -> None:
        from scripts.aws_target import cloud_gate
        from scripts.production import targeted_test

        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "merge_gate.conf", "scan_roots=\n")
        subject, base = self.prepare_affected_subject()
        first = self.run_gate("--tests-only", type_dirs=())
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        rows = (
            ("unchanged", False, (), False, 0),
            ("document", False, (), False, 0),
            ("subject", False, (), False, 0),
            ("subject", True, (), False, 0),
            ("committed-subject", False, ("scripts",), False, 1),
            ("document", False, ("scripts",), False, 0),
            ("document", False, ("missing-types",), False, 1),
            ("document", False, ("scripts",), True, 1),
            ("fallback-edit", True, (), False, 1),
            ("fallback-commit", True, (), False, 1),
            ("fallback-full", True, (), False, 1),
        )
        for change, fallback, type_dirs, red_types, status in rows:
            with self.subTest(change=change, fallback=fallback, type_dirs=type_dirs, red_types=red_types):
                _ = self.git(self.checkout, "reset", "--hard", base)
                (self.checkout / "scripts/hooks/test_second.py").unlink(missing_ok=True)
                if change in ("subject", "committed-subject") or change.startswith("fallback-"):
                    self.write(subject, "VALUE = 2\n")
                    if change == "committed-subject":
                        _ = self.commit(self.checkout, "Change the subject")
                elif change == "document":
                    self.write(self.checkout / "subject.txt", "unrelated document\n")
                self.clear_gate_events()
                (self.state / "types-red").unlink(missing_ok=True)
                if red_types:
                    self.write(self.state / "types-red", "red")
                if change == "fallback-full":
                    (Path(self.environment["MERGE_GATE_PASSED_DIR"]) / "passed-trees.jsonl").unlink()
                    for ref in self.git(self.checkout, "for-each-ref", "--format=%(refname)", "refs/remotes/origin").splitlines():
                        _ = self.git(self.checkout, "update-ref", "-d", ref)
                expected_tree = cloud_gate.working_tree(self.checkout)[0]
                routed: list[merge_gate.AllModules | merge_gate.TreeSelection] = []

                def route(repo: Path, *, mode: merge_gate.SuiteMode, full: bool,
                          module_time_limit_s: float,
                          selected: merge_gate.AllModules | merge_gate.TreeSelection = merge_gate.ALL_MODULES
                          ) -> merge_gate.GateRun | str:
                    self.assertEqual(repo, self.checkout)
                    self.assertEqual(mode, merge_gate.SuiteMode.TARGETED_TEST)
                    self.assertFalse(full)
                    self.assertEqual(module_time_limit_s, merge_gate.DEFAULT_MODULE_TIME_LIMIT_S)
                    routed.append(selected)
                    if change.startswith("fallback-"):
                        added = self.checkout / "scripts/hooks/test_second.py"
                        self.write(added, SUITE_MODULE)
                        self.write(added, added.read_text() + '\nclass NewFailure(unittest.TestCase):\n'
                                   + '    def test_new_failure(self):\n        self.fail("new failing module")\n')
                        if change in ("fallback-commit", "fallback-full"):
                            _ = self.commit(self.checkout, "Add a failing test during the route")
                    if fallback:
                        return "fixture cloud unavailable"
                    return merge_gate.GateRun((merge_gate.GateJobResult(
                        "hooks", "test_first.py", int(change == "committed-subject"), 1, ""),), 1, 0)

                arguments = ["--repo", str(self.checkout), *(["--type-dirs", *type_dirs] if type_dirs else [])]
                with (mock.patch.dict(os.environ, self.environment),
                      mock.patch.object(cloud_gate, "route", side_effect=route),
                      mock.patch.object(merge_gate, "select_affected", wraps=affected_modules.select_affected) as select,
                      contextlib.redirect_stdout(io.StringIO()) as output,
                      contextlib.redirect_stderr(io.StringIO()) as errors):
                    result = targeted_test.main(arguments)
                self.assertEqual(result, status, output.getvalue() + errors.getvalue())
                if type_dirs == ("missing-types",):
                    self.assertIn("type dirs not found", errors.getvalue())
                    self.assertEqual(routed, [])
                    continue
                local_selection = fallback or bool(type_dirs) and change != "committed-subject"
                self.assertEqual(select.call_count, 2 if local_selection else 1)
                if change in ("subject", "committed-subject") or change.startswith("fallback-"):
                    selected, = routed
                    self.assertIsInstance(selected, merge_gate.TreeSelection)
                    assert isinstance(selected, merge_gate.TreeSelection)
                    self.assertEqual(selected.tree, expected_tree)
                    if change == "fallback-full":
                        self.assertCountEqual(selected.paths, ("scripts/hooks/test_first.py", "scripts/message/test_first.py"))
                        self.assertEqual(output.getvalue().splitlines().count("full suite: no certified base"), 2)
                    else:
                        self.assertEqual(selected.paths, ("scripts/hooks/test_first.py",))
                        self.assertIn("affected: 1 of 2 modules", output.getvalue())
                else:
                    self.assertEqual(routed, [])
                    self.assertIn("affected: 0 of 2 modules", output.getvalue())
                    self.assertIn("(none)", output.getvalue())
                starts = [(row["suite"], row["module"]) for row in self.module_events() if row["event"] == "start"]
                if change.startswith("fallback-"):
                    expected = {("hooks", "test_first.py"), ("hooks", "test_second.py")}
                    if change == "fallback-full":
                        expected.add(("message", "test_first.py"))
                    self.assertEqual(set(starts), expected)
                    self.assertIn("new failing module", output.getvalue())
                    self.assertFalse(any(record["tree"] == cloud_gate.working_tree(self.checkout)[0]
                                         for record in self.pass_records()))
                else:
                    self.assertEqual(starts, [("hooks", "test_first.py")] if fallback else [])
                self.assertEqual(len([call for call in self.calls() if call["tool"] == "basedpyright"]), int(bool(type_dirs)))
                if change == "committed-subject":
                    tree = self.git(self.checkout, "rev-parse", "HEAD^{tree}")
                    self.assertFalse(any(record["tree"] == tree and record["kind"] == "full"
                                         and record["suites"] == list(self.suites) for record in self.pass_records()))

    @gate_suites("hooks", "direct")
    def test_whole_machine_orders_modules_from_supplied_times_without_writing_them(self) -> None:
        path = self.state / "module_seconds.json"
        content = json.dumps({"hooks/test_first.py": 100.0, "direct/test_first.py": 1.0})
        self.write(path, content)
        machine = merge_gate.WholeMachine(merge_gate.read_measures(path))
        with (mock.patch.dict(os.environ, self.environment),
              mock.patch.object(os, "cpu_count", return_value=1),
              mock.patch.object(merge_gate, "save_measures", side_effect=AssertionError("measure write")),
              contextlib.redirect_stdout(io.StringIO()) as output):
            run = merge_gate.run_suites(self.checkout, machine=machine)
        self.assertTrue(run.passed, output.getvalue())
        starts = [event["suite"] for event in self.module_events() if event["event"] == "start"]
        self.assertEqual(starts, ["hooks", "direct"])
        self.assertEqual(path.read_text(), content)
        self.assertEqual(machine.module_seconds, {"hooks/test_first.py": 100.0, "direct/test_first.py": 1.0})

    @gate_suites("hooks", "direct")
    def test_whole_machine_without_times_keeps_the_discovery_order(self) -> None:
        with (mock.patch.dict(os.environ, self.environment),
              mock.patch.object(os, "cpu_count", return_value=1),
              contextlib.redirect_stdout(io.StringIO()) as output):
            run = merge_gate.run_suites(self.checkout, machine=merge_gate.WholeMachine())
        self.assertTrue(run.passed, output.getvalue())
        starts = [event["suite"] for event in self.module_events() if event["event"] == "start"]
        self.assertEqual(starts, ["direct", "hooks"])

    @gate_suites("lib")
    def test_whole_machine_uses_all_cpus_without_shared_admission_or_holds(self) -> None:
        self.write(self.state / "barrier", "two")
        output = io.StringIO()
        with (mock.patch.dict(os.environ, self.environment),
              mock.patch.object(os, "cpu_count", return_value=2),
              mock.patch.object(merge_gate, "typecheck_python", return_value=Path(sys.executable)),
              contextlib.ExitStack() as blocked, contextlib.redirect_stdout(output)):
            for name in ("BuildHoldWait", "gate_reservation", "queue_place", "shared_cpu_pool",
                         "suite_scope_prefix", "merge_gate_governor_gib", "SampledRSSAdmission",
                         "PassedTreeLedger", "clean_test_tree", "gate_state_dir", "process_memory_mib"):
                _ = blocked.enter_context(mock.patch.object(merge_gate, name, side_effect=AssertionError(name)))
            run = merge_gate.run_suites(self.checkout, type_dirs=("scripts",), max_running_jobs=1,
                                       machine=merge_gate.WholeMachine())
        self.assertTrue(run.passed, output.getvalue())
        self.assertEqual(len(run.jobs), 3)
        self.assertIn("at most 2 modules at once", output.getvalue())
        active: set[int] = set()
        peak = 0
        for event in self.module_events():
            if event["event"] == "start":
                active.add(event["pid"])
            else:
                active.remove(event["pid"])
            peak = max(peak, len(active))
        self.assertEqual(peak, 2)
        self.assertEqual(active, set())
        self.assertFalse(any(call["tool"] in ("systemd-run", "ssh") for call in self.calls()))
        for job in run.jobs:
            self.assertIn(f"{job.suite}/{job.module} exit=0 seconds=", output.getvalue())

    @gate_suites("hooks")
    def test_whole_machine_does_not_rerun_a_failed_module(self) -> None:
        self.write(self.state / "fail-first-hooks", "once")
        with mock.patch.dict(os.environ, self.environment), contextlib.redirect_stdout(io.StringIO()) as output:
            run = merge_gate.run_suites(self.checkout, machine=merge_gate.WholeMachine())
        self.assertFalse(run.passed, output.getvalue())
        self.assertEqual(len(run.jobs), 1)
        self.assertEqual(run.jobs[0].returncode, 1)
        self.assertEqual(len([event for event in self.module_events() if event["event"] == "start"]), 1)
        self.assertIn("hooks/test_first.py exit=1 seconds=", output.getvalue())
        self.assertNotIn("Rerun alone", output.getvalue())

    @gate_suites("hooks")
    def test_whole_machine_stops_a_module_at_its_time_limit(self) -> None:
        self.write(self.state / "hold-module", "held")
        with mock.patch.dict(os.environ, self.environment), contextlib.redirect_stdout(io.StringIO()) as output:
            run = merge_gate.run_suites(self.checkout, module_time_limit_s=2,
                                       machine=merge_gate.WholeMachine())
        self.assertFalse(run.passed, output.getvalue())
        self.assertEqual(len(run.jobs), 1)
        self.assertTrue(run.jobs[0].killed_for)
        self.assertIn("seconds=", output.getvalue())
        self.assertIn("stopped", output.getvalue())

    def test_modules_run_the_checkout_runner_and_import_from_the_checkout(self) -> None:
        runner = install_test_runner(self.checkout)
        marker = self.state / "checkout-runner"
        self.write(runner, runner.read_text().replace(
            'if __name__ == "__main__":',
            f'Path({str(marker)!r}).write_text(str(Path(__file__).resolve()))\n\n'
            + 'if __name__ == "__main__":'))
        self.write(self.checkout / "checkout_identity.py", 'VALUE = "tested checkout"\n')
        self.write(self.checkout / "scripts/hooks/test_first.py", SUITE_MODULE + '''
    def test_imports_use_the_checkout(self):
        import checkout_identity
        self.assertEqual(checkout_identity.VALUE, "tested checkout")
''')
        result = self.run_gate("--tests-only", "--full")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(marker.read_text(), str(runner))
        self.assert_all_modules_started()

    def test_a_missing_checkout_runner_is_refused_before_any_job_starts(self) -> None:
        runner = self.checkout / "scripts/test_times/module_run.py"
        runner.unlink(missing_ok=True)
        result = self.run_gate("--tests-only", "--full")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr.strip(), f"REFUSED: test runner not found: {runner}")
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.module_events(), [])

    def test_recovery_suites_include_their_real_regression_modules(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        found = {(suite, test_module_name(repo, suite, path))
                 for suite, path in discover_test_modules(repo)}
        self.assertTrue({
            ("release", "test_release.py"), ("claude_to_codex", "test_sync.py"),
            ("new_rust_project", "test_rust_generate.py"), ("validate_and_push", "test_rerun.py"),
            ("exclude", "test_exclude.py"), ("prioritize", "tests/test_renumber.py"),
            ("prioritize", "tests/test_run_watcher.py"),
        }.issubset(found), found)

    def test_discovery_keeps_path_order_and_skips_cache_folders(self) -> None:
        repo = self.checkout / "discovery"
        for name in ("z/test_last.py", "a/test_last.py", "a/nested/test_first.py",
                     "a/__pycache__/test_hidden.py", "__pycache__/test_hidden.py"):
            self.write(repo / "scripts" / name, "# A fixture module.\n")
        found = discover_test_modules(repo)
        self.assertEqual(found, [
            ("a", repo / "scripts/a/nested/test_first.py"),
            ("a", repo / "scripts/a/test_last.py"),
            ("z", repo / "scripts/z/test_last.py"),
        ])
        self.assertEqual([test_module_name(repo, suite, path) for suite, path in found],
                         ["nested/test_first.py", "test_last.py", "test_last.py"])

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

    def test_modules_use_only_the_checked_tree_pythonpath(self) -> None:
        caller_path = self.root / "caller-pythonpath"
        caller_path.mkdir()
        self.environment.update({"PYTHONPATH": str(caller_path), "MERGE_GATE_TEST_PYTHONPATH": str(caller_path),
                                 "MERGE_GATE_TEST_ISOLATION": "1"})
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @gate_suites("hooks", "message")
    def test_caller_modules_fail_and_plain_python_children_import_the_checked_tree(self) -> None:
        caller_path = self.root / "caller-pythonpath"
        self.write(caller_path / "caller_only.py", 'VALUE = "caller"\n')
        self.environment["PYTHONPATH"] = str(caller_path)
        del self.environment["MERGE_GATE_PYTHON"]
        self.write(self.checkout / "scripts/hooks/test_first.py", "import caller_only\n")
        identity = self.checkout / "scripts/message/checkout_identity.py"
        self.write(identity, 'VALUE = "checked tree"\n')
        marker = self.state / "plain-python-import"
        self.write(self.checkout / "scripts/message/test_first.py", f'''import subprocess
import sys
import unittest
from pathlib import Path

class ChildImports(unittest.TestCase):
    def test_child_imports_the_checked_tree(self):
        result = subprocess.run(
            [sys.executable, "-c", "from scripts.message import checkout_identity; print(checkout_identity.__file__)"],
            capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), {str(identity)!r})
        Path({str(marker)!r}).write_text(result.stdout.strip())
''')
        result = self.run_gate("--tests-only", "--full", own_process=True, type_dirs=())
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("No module named 'caller_only'", result.stdout)
        self.assertIn("hooks: RED", result.stdout)
        self.assertIn("message: OK", result.stdout)
        self.assertEqual(marker.read_text(), str(identity))

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
        gate_records = [cast(dict[str, object], json.loads(line))
                        for path in caller_log.rglob("*")
                        if path.is_file() and path != caller_log / "keep"
                        for line in path.read_text().splitlines()]
        self.assertEqual([record.get("kind") for record in gate_records], ["gate_run"])
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
