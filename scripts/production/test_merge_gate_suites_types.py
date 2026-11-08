"""Merge gate suites contracts."""

from __future__ import annotations

import unittest
import io
import tempfile
from collections.abc import Generator
from contextlib import contextmanager, nullcontext, redirect_stdout
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast, final, override
from unittest import mock
from scripts.copies import copies
from scripts.production import merge_gate
from scripts.production.gate_modules import GateRefused
from scripts.production.cli_fixture import invoke_cli
from scripts.production.merge_gate_test_support import gate_suites, wait_for_file
from scripts.production.merge_gate_suites_test_support import GateSuitesFixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@final
class MergeGateTypesTests(GateSuitesFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(mock.patch.object(merge_gate, "push_nightly_result")))

    def run_nightly(self) -> subprocess.CompletedProcess[str]:
        configuration = self.root / "copies.conf"
        self.write(configuration, f"[copies]\nroot={self.root / 'copies'}\n"
                   + "wait_seconds=1\ngit_timeout_seconds=60\nguard_timeout_seconds=10\n")
        self.environment["COPIES_CONFIG"] = str(configuration)
        arguments = ["--nightly", "--repo", str(self.checkout), "--merge-branch", "integration"]
        with mock.patch.object(merge_gate, "nightly_owner", return_value="fixture-showrunner"):
            return invoke_cli(merge_gate, lambda: merge_gate.main(arguments), arguments,
                              self.environment, self.root)

    def test_nightly_checks_all_scripts_with_the_fake_type_checker(self) -> None:
        result = self.run_nightly()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        type_call, = [call for call in self.calls() if call["tool"] == "basedpyright"]
        python = self.state / "runner/typecheck-venv/bin/python"
        self.assertEqual(type_call["args"], ["--pythonpath", str(python), "scripts"])
        self.assertNotEqual(type_call["cwd"], str(self.checkout))
        self.assertTrue(Path(type_call["cwd"]).is_relative_to(self.root / "copies"))
        self.assert_all_modules_started()
        self.assertIn("basedpyright: OK", result.stdout)
        history, = self.gate_history()
        self.assertEqual(history["shape"], "types")
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)

    def test_nightly_type_error_names_the_file_without_requiring_full_test_runs(self) -> None:
        from scripts.production import showrunners

        failing_file = "scripts/fixture.py"
        self.write(self.state / "types-red", "red")
        self.write(self.state / "runner/full-required", "older marker\n")
        with mock.patch.object(showrunners, "tell", return_value="sent to fixture-showrunner") as notice:
            result = self.run_nightly()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(failing_file, result.stdout)
        self.assert_all_modules_started()
        notice.assert_called_once()
        arguments = notice.call_args
        assert arguments is not None
        tree = self.git(self.checkout, "rev-parse", "HEAD^{tree}")
        self.assertEqual(arguments.args[:2], ("fixture-showrunner", f"merge-gate-nightly:{tree}"))
        message = cast(str, arguments.args[2])
        self.assertIn(failing_file, message)
        self.assertNotIn("requires the full suite", message)
        self.assertFalse((self.state / "runner/full-required").exists())
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)

    def test_nightly_notice_limits_a_large_type_report_and_keeps_its_summary(self) -> None:
        from scripts.production import showrunners

        diagnostics = [f"scripts/broken_{index}.py:1: error: " + "incompatible type " * 8
                       for index in range(2000)]
        summary = "2000 errors, 0 warnings, 0 notes"
        job = merge_gate.GateJobResult("types", "basedpyright", 1, 0,
            "\n".join([*diagnostics, summary]), kind=merge_gate.GateJobKind.TYPE_CHECK)
        with (mock.patch.object(merge_gate, "nightly_owner", return_value="fixture-showrunner"),
              mock.patch.object(showrunners, "tell", return_value="sent to fixture-showrunner") as notice):
            merge_gate.tell_nightly_red(merge_gate.GateArguments(merge_branch="integration"),
                                        "fixture-tree", [], type_failures=(job,))

        notice.assert_called_once()
        arguments = notice.call_args
        assert arguments is not None
        message = cast(str, arguments.args[2])
        self.assertLess(len(message.encode()), 8000)
        for line in diagnostics[:20]:
            self.assertIn(line, message)
        self.assertNotIn(diagnostics[20], message)
        self.assertIn("1980 other diagnostic lines omitted.", message)
        self.assertIn(summary, message)

    def test_nightly_notice_without_a_failed_job_says_the_full_suite_is_required(self) -> None:
        from scripts.production import showrunners

        with (mock.patch.object(merge_gate, "nightly_owner", return_value="fixture-showrunner"),
              mock.patch.object(showrunners, "tell", return_value="sent to fixture-showrunner") as notice):
            merge_gate.tell_nightly_red(merge_gate.GateArguments(merge_branch="integration"), "fixture-tree", [])

        arguments = notice.call_args
        assert arguments is not None
        message = cast(str, arguments.args[2])
        self.assertIn("(runner failure)", message)
        self.assertIn("requires the full suite", message)
        self.assertNotIn("Tests passed", message)

    def test_type_check_requires_exit_zero_and_a_clean_summary(self) -> None:
        for status, summary, expected in (
            (0, "0 errors, 0 warnings, 0 notes", 0),
            (3, "0 errors, 0 warnings, 0 notes", 1),
            (1, "1 error, 0 warnings, 0 notes", 1),
            (0, "1 error, 0 warnings, 0 notes", 1),
        ):
            with self.subTest(status=status, summary=summary):
                self.write(self.bin / "basedpyright",
                           f"#!{sys.executable}\nprint({summary!r})\nraise SystemExit({status})\n")
                result = self.run_gate("--tests-only")
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertIn("basedpyright: " + ("OK" if expected == 0 else "RED"), result.stdout)

    @gate_suites("hooks")
    def test_types_owns_a_shared_slot_until_it_finishes(self) -> None:
        self.supply_jobserver(b"T")
        start = merge_gate.start_gate_job
        types_pids: list[int] = []

        def observed(suite: str, path: Path, argv: list[str], repo: Path,
                     environment: dict[str, str], log: Path, *,
                kind: merge_gate.GateJobKind = merge_gate.GateJobKind.TEST_MODULE) -> merge_gate.RunningGateJob:
            if suite == "types":
                self.assertEqual(self.jobserver_tokens(), b"")
            job = start(suite, path, argv, repo, environment, log, kind=kind)
            if suite == "types":
                types_pids.append(job.process.pid)
                wait_for_file(self.state / "types-ran")
            return job

        with mock.patch.object(merge_gate, "start_gate_job", side_effect=observed):
            result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(types_pids), 1)
        self.assertEqual(self.jobserver_tokens(), b"T")

    def test_the_type_check_venv_is_built_once_with_the_pinned_stubs(self) -> None:
        with mock.patch.dict(os.environ, self.environment):
            first, second = merge_gate.typecheck_python(), merge_gate.typecheck_python()
        venv = self.state / "runner" / "typecheck-venv"
        self.assertEqual((first, second), (venv / "bin" / "python", venv / "bin" / "python"))
        self.assertEqual([call["args"] for call in self.calls() if call["tool"] == "uv"],
                         [["venv", "--clear", str(venv)],
                          ["pip", "install", "--python", str(venv / "bin" / "python"),
                           *merge_gate.TYPECHECK_REQUIREMENTS]])

    def test_tests_only_runs_types_for_each_requested_directory(self) -> None:
        result = self.run_gate("--tests-only", "--type-dirs", "scripts/production", "scripts/hooks")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        type_call, = [call for call in self.calls() if call["tool"] == "basedpyright"]
        python = self.state / "runner/typecheck-venv/bin/python"
        self.assertEqual(type_call["args"], ["--pythonpath", str(python), "scripts/production", "scripts/hooks"])
        self.assertEqual(type_call["cwd"], str(self.checkout))
        self.assertIn("basedpyright: OK", result.stdout)
        self.assert_all_modules_started()
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        self.assert_not_pushed()

    def test_tests_only_runs_modules_without_a_default_type_check(self) -> None:
        result = subprocess.run(
            [*self.gate_command(), "--tests-only", "--repo", str(self.checkout)],
            cwd=self.root, env=self.environment, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call for call in self.calls() if call["tool"] == "basedpyright"], [])
        self.assert_all_modules_started()
        self.assert_not_pushed()
        history, = self.gate_history()
        self.assertEqual(history["shape"], "tests")

    def test_merge_pushes_modules_without_a_default_type_check(self) -> None:
        result = self.run_gate(type_dirs=())

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call for call in self.calls() if call["tool"] == "basedpyright"], [])
        self.assert_all_modules_started()
        self.assertEqual(self.git(self.origin, "rev-parse", "integration"),
                         self.git(self.checkout, "rev-parse", "HEAD"))
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)

    @gate_suites("lib")
    def test_a_type_check_venv_that_cannot_be_built_is_red_and_names_the_command(self) -> None:
        self.write(self.state / "uv-fails", "fail")
        result = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("basedpyright: RED\ntype-check venv missing: stderr: fixture: no network; build it with: ", result.stdout)
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


@final
class NightlyPushTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = self.root / "config"
        _ = shutil.copytree(Path(__file__).resolve().parents[2] / "config", config)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.root), "CONF_DIR": str(config),
            "MERGE_GATE_STATE_DIR": str(self.root / "state"), "USER_ZONE": "America/New_York"})))

    @contextmanager
    def execution(self, run: merge_gate.GateRun | Exception) -> Generator[mock.MagicMock, None, None]:
        with (mock.patch.object(merge_gate, "nightly_git", return_value=""),
              mock.patch.object(merge_gate, "checked_git", return_value="fixture-tree"),
              mock.patch.object(merge_gate, "nightly_owner", return_value="fixture"),
              mock.patch.object(copies, "standing", return_value=nullcontext(self.root)),
              mock.patch.object(merge_gate, "run_suites", return_value=run,
                                side_effect=run if isinstance(run, Exception) else None),
              mock.patch.object(merge_gate, "tell_nightly_red"),
              mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                  [], 0, "SENT: fixture\n", "")) as sent):
            yield sent

    def nightly(self) -> int:
        return merge_gate.nightly(merge_gate.GateArguments(repo=str(self.root), merge_branch="main"))

    def test_nightly_push_describes_the_run_and_limits_failed_modules(self) -> None:
        good = merge_gate.GateJobResult("message", "test_send.py", 0, 0, "")
        types = merge_gate.GateJobResult("types", "basedpyright", 0, 0, "",
                                        kind=merge_gate.GateJobKind.TYPE_CHECK)
        red_types = merge_gate.GateJobResult("types", "basedpyright", 1, 0,
            "5 errors, 2 warnings, 0 notes\ndiagnostic\n3 errors, 1 warning, 0 notes",
            kind=merge_gate.GateJobKind.TYPE_CHECK)
        red_jobs = tuple(merge_gate.GateJobResult("message", f"test_{i}.py", 1, 0, "") for i in range(10))
        cases: tuple[tuple[str, merge_gate.GateRun | Exception, str, str, str], ...] = (
            ("green", merge_gate.GateRun((good, types), 0, 0), "passed", "passed", "passed"),
            ("test red", merge_gate.GateRun((red_jobs[0], types), 0, 0), "failed", "passed",
             "failed in message/test_0.py"),
            ("many red", merge_gate.GateRun((*red_jobs, types), 0, 0), "failed", "passed",
             "failed in " + ", ".join(f"message/test_{i}.py" for i in range(8)) + " and 2 more"),
            ("types red", merge_gate.GateRun((good, red_types), 0, 0), "failed",
             "failed with 3 errors, 1 warning, 0 notes", "passed"),
            ("runner", GateRefused("runner failed"), "failed", "did not run", "did not finish"),
            ("empty", merge_gate.GateRun((), 0, 0), "failed", "did not run", "did not finish"),
        )
        for name, run, outcome, expected_types, tests in cases:
            with self.subTest(name=name), self.execution(run) as sent, redirect_stdout(io.StringIO()):
                self.assertEqual(self.nightly(), 0 if outcome == "passed" else 1)
                sent.assert_called_once()
                command = cast(list[str], sent.call_args.args[0])
                for flag, value in (("--text-id", "nightly.result"), ("--need", "note"),
                                    ("--key", "nightly-result")):
                    self.assertEqual(command[command.index(flag) + 1], value)
                values = dict(command[i + 1].split("=", 1) for i, word in enumerate(command) if word == "--value")
                self.assertEqual((values["types"], values["tests"], values["outcome"]),
                                 (expected_types, tests, outcome))
                self.assertEqual(values["branch"], "main")
                self.assertRegex(values["time"], r"^\d{2}:\d{2} E[SD]T$")
                self.assertNotIn("--priority", command)

    def test_off_disables_both_push_texts_at_use(self) -> None:
        from scripts.notify import push_texts

        for run in (merge_gate.GateRun((merge_gate.GateJobResult("test", "ok", 0, 0, ""),), 0, 0),
                    GateRefused("runner failed")):
            with self.execution(run) as sent, mock.patch.object(push_texts, "render") as render:
                _ = (self.root / "config/merge_gate.conf").write_text("nightly_result_push=off\n")
                _ = self.nightly()
                sent.assert_not_called()
                render.assert_not_called()
                with mock.patch.object(merge_gate, "nightly_git", side_effect=OSError("fetch failed")):
                    with self.assertRaisesRegex(OSError, "fetch failed"):
                        _ = self.nightly()
                sent.assert_not_called()
                render.assert_not_called()

    def test_failed_module_limit_is_read_again_for_each_run(self) -> None:
        jobs = tuple(merge_gate.GateJobResult("test", f"test_{i}.py", 1, 0, "") for i in range(4))
        for limit in (1, 3):
            _ = (self.root / "config/merge_gate.conf").write_text(
                f"nightly_result_push=on\n# whole: 1..\nnightly_result_modules={limit}\n")
            with self.execution(merge_gate.GateRun(jobs, 0, 0)) as sent, redirect_stdout(io.StringIO()):
                self.assertEqual(self.nightly(), 1)
                command = cast(list[str], sent.call_args.args[0])
                self.assertIn("tests=failed in " + ", ".join(f"test/test_{i}.py" for i in range(limit))
                              + f" and {4 - limit} more", command)

    def test_failed_push_preserves_green_and_red_exit_and_marker(self) -> None:
        for status in (0, 1):
            run = merge_gate.GateRun((merge_gate.GateJobResult("test", "example", status, 0, ""),), 0, 0)
            for error in (subprocess.CompletedProcess([], 3, "", "unavailable\nmore detail"),
                          OSError("cannot start sender")):
                with self.subTest(status=status, error=error), self.execution(run) as sent:
                    if isinstance(error, OSError):
                        sent.side_effect = error
                    else:
                        sent.return_value = error
                    marker = self.root / "state/full-required"
                    marker.parent.mkdir(exist_ok=True)
                    _ = marker.write_text("older marker")
                    with redirect_stdout(io.StringIO()) as output:
                        self.assertEqual(self.nightly(), status)
                    self.assertEqual(marker.exists(), bool(status))
                    self.assertIn("Nightly result push could not be sent:", output.getvalue())
                    if status:
                        self.assertIn('"failed_modules": ["test/example"]', marker.read_text())

    def test_render_import_error_preserves_green_exit_and_marker(self) -> None:
        from scripts.notify import push_texts

        run = merge_gate.GateRun((merge_gate.GateJobResult("test", "ok", 0, 0, ""),), 0, 0)
        marker = self.root / "state/full-required"
        marker.parent.mkdir()
        _ = marker.write_text("older marker")
        with self.execution(run) as sent, mock.patch.object(push_texts, "render", side_effect=ImportError("render unavailable")):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(self.nightly(), 0)
            self.assertFalse(marker.exists())
            self.assertIn("Nightly result push could not be sent: render unavailable", output.getvalue())
            sent.assert_not_called()

    def test_setup_failure_push_preserves_the_exception(self) -> None:
        good = merge_gate.GateRun((merge_gate.GateJobResult("test", "ok", 0, 0, ""),), 0, 0)
        for stage in ("fetch", "standing"):
            failure = GateRefused(stage + " failed\nconnection closed")
            with self.subTest(stage=stage), self.execution(good) as sent:
                with (mock.patch.object(merge_gate, "nightly_git", side_effect=["", failure]) if stage == "fetch"
                      else mock.patch.object(copies, "standing", side_effect=failure)):
                    with self.assertRaises(GateRefused) as caught, redirect_stdout(io.StringIO()):
                        _ = self.nightly()
                self.assertIs(caught.exception, failure)
                sent.assert_called_once()
                command = cast(list[str], sent.call_args.args[0])
                self.assertEqual(command[command.index("--text-id") + 1], "nightly.not_run")
                self.assertEqual(command[command.index("--key") + 1], "nightly-result")
                self.assertEqual(command[command.index("--need") + 1], "note")
                self.assertIn(f"reason={stage} failed connection closed", command)
                self.assertIn("action=None. The next nightly run tries again.", command)
                self.assertNotIn("--priority", command)

    def test_interrupt_push_preserves_the_exception(self) -> None:
        run = merge_gate.GateRun((), 0, 0)
        for stage in ("nightly_git", "run_suites"):
            failure = KeyboardInterrupt()
            with self.subTest(stage=stage), self.execution(run) as sent:
                with mock.patch.object(merge_gate, stage, side_effect=failure):
                    with self.assertRaises(KeyboardInterrupt) as caught, redirect_stdout(io.StringIO()):
                        _ = self.nightly()
                self.assertIs(caught.exception, failure)
                sent.assert_called_once()
                command = cast(list[str], sent.call_args.args[0])
                self.assertEqual(command[command.index("--text-id") + 1], "nightly.not_run")
                self.assertIn("reason=interrupted by SIGINT", command)
                self.assertEqual(command[command.index("--key") + 1], "nightly-result")

    def test_empty_exception_reason_uses_its_class_name(self) -> None:
        run = merge_gate.GateRun((), 0, 0)
        failure = OSError()
        with self.execution(run) as sent, mock.patch.object(merge_gate, "nightly_git", side_effect=failure):
            with self.assertRaises(OSError) as caught, redirect_stdout(io.StringIO()):
                _ = self.nightly()
            self.assertIs(caught.exception, failure)
            sent.assert_called_once()
            command = cast(list[str], sent.call_args.args[0])
            self.assertEqual(command[command.index("--text-id") + 1], "nightly.not_run")
            self.assertIn("reason=OSError", command)

    def test_cleanup_failure_does_not_send_a_second_push(self) -> None:
        @contextmanager
        def failing_cleanup() -> Generator[Path, None, None]:
            try:
                yield self.root
            finally:
                raise OSError("cleanup failed")

        run = merge_gate.GateRun((merge_gate.GateJobResult("test", "ok", 0, 0, ""),), 0, 0)
        with self.execution(run) as sent, mock.patch.object(copies, "standing", return_value=failing_cleanup()):
            with self.assertRaisesRegex(OSError, "cleanup failed"), redirect_stdout(io.StringIO()):
                _ = self.nightly()
            sent.assert_called_once()
            self.assertIn("nightly.result", cast(list[str], sent.call_args.args[0]))

    def test_not_run_reason_removes_final_periods(self) -> None:
        from scripts.notify import push_texts

        run = merge_gate.GateRun((), 0, 0)
        with self.execution(run) as sent, mock.patch.object(merge_gate, "nightly_git", side_effect=OSError("fetch\nfailed...")):
            with self.assertRaises(OSError), redirect_stdout(io.StringIO()):
                _ = self.nightly()
            command = cast(list[str], sent.call_args.args[0])
            values = dict(command[i + 1].split("=", 1) for i, word in enumerate(command) if word == "--value")
            self.assertEqual(values["reason"], "fetch failed")
            self.assertIn("action=None. The next nightly run tries again.", command)
            self.assertIn("Why: fetch failed.", push_texts.render("nightly.not_run", **values).message)

    def test_not_run_reason_fits_the_message_limit(self) -> None:
        from scripts.notify import push_texts
        from scripts.notify.pushover import MESSAGE_MAX

        run = merge_gate.GateRun((), 0, 0)
        with self.execution(run) as sent, mock.patch.object(merge_gate, "nightly_git", side_effect=OSError("word\n" * 2000)):
            with self.assertRaises(OSError), redirect_stdout(io.StringIO()):
                _ = self.nightly()
            command = cast(list[str], sent.call_args.args[0])
            values = dict(command[i + 1].split("=", 1) for i, word in enumerate(command) if word == "--value")
            rendered = push_texts.render("nightly.not_run", **values)
            self.assertLessEqual(len(rendered.message), MESSAGE_MAX)
            self.assertNotIn("\n", values["reason"])
            self.assertIn("word word", values["reason"])


if __name__ == "__main__":
    _ = unittest.main()
