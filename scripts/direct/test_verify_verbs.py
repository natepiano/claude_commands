"""Print help and refuse unknown verbs before verification starts."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from typing import cast
import unittest

from scripts.direct.verify_final_pass_test_support import FinalPassFixture
from scripts.direct.verify_lint_cache_test_support import VerifyLintCacheFixture
from scripts.direct.verify_lint_scope_test_support import VERIFY


class VerifyVerbTests(VerifyLintCacheFixture):
    def test_help_forms_print_the_same_short_usage_with_short_lines(self) -> None:
        missing = self.invoke_verify(cwd=self.root)
        self.assertEqual(missing.returncode, 2)
        self.assertEqual(missing.stdout, "")
        self.assertTrue(missing.stderr.startswith("Usage:\n"), missing.stderr)
        for flag in ("-h", "--help"):
            with self.subTest(flag=flag):
                result = self.invoke_verify(flag, cwd=self.root)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertEqual(result.stdout, missing.stderr)
        for line in missing.stderr.splitlines():
            self.assertLess(len(line), 80, line)
        commands, flags = missing.stderr.split("Commands:\n", 1)[1].split("\nFlags:\n", 1)
        self.assertEqual([line.split()[0] for line in commands.splitlines()], [
            "check", "test", "lint", "doc", "example", "build", "final",
            "help",
        ])
        self.assertEqual([line.split()[0] for line in flags.splitlines()], [
            "--test", "--name", "--check", "--saved", "--tree",
            "--filter", "--run-ignored", "--features", "--no-cache",
            "--target", "--mac", "--local", "--session-dir",
        ])

    def test_each_command_has_separate_help(self) -> None:
        for command in (
            "check", "test", "lint", "doc", "example", "build", "final",
            "help", "example-test",
        ):
            with self.subTest(command=command):
                result = self.invoke_verify("help", command, cwd=self.root)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertTrue(result.stdout.startswith(f"Usage: verify {command}"), result.stdout)
                self.assertNotIn("Commands:", result.stdout)
                self.assertGreater(len(result.stdout.splitlines()), 3)
                for line in result.stdout.splitlines():
                    self.assertLess(len(line), 80, line)

    def test_removed_forms_name_the_new_form(self) -> None:
        for arguments, form in (
            (("final-pass",), "verify final [<package>] [--saved] [--tree <tree>]"),
            (("lint", "--workspace"), "verify lint [<package>] [--check]"),
            (("example", "sample", "demo"), "verify example [<package>] [--name <name>]"),
            (("test", "sample", "smoke"), "verify test [<package>] [--test <name>]"),
            (("final", "--tree", "HEAD"), "verify final [<package>] [--saved] [--tree <tree>]"),
        ):
            with self.subTest(arguments=arguments):
                result = self.invoke_verify(*arguments, cwd=self.root)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(form, result.stderr)
                self.assertEqual(self.cargo_calls(), [])

    def test_missing_or_unknown_help_command_keeps_the_usage_error(self) -> None:
        usage = self.invoke_verify(cwd=self.root).stderr
        for arguments in (("help",), ("help", "fmt"), ("help", "test", "sample")):
            with self.subTest(arguments=arguments):
                result = self.invoke_verify(*arguments, cwd=self.root)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, usage)

    def test_help_runs_before_tools_with_only_the_script_outside_a_repository(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        script = outside / "verify.sh"
        _ = shutil.copyfile(VERIFY, script)
        tools = outside / "tools"
        tools.mkdir()
        sed = shutil.which("sed")
        bash = shutil.which("bash")
        self.assertIsNotNone(sed)
        self.assertIsNotNone(bash)
        if sed is None or bash is None:
            self.fail("The help fixture requires Bash and sed.")
        (tools / "sed").symlink_to(sed)
        called = outside / "unexpected-tool"
        stub = f"printf '%s\\n' called >> '{called}'\nexit 99\n"
        for name in ("git", "cargo", "build-targets.py"):
            self.write_shell(tools / name, stub)
        environment = {
            **self.environment,
            "PATH": str(tools),
            "BUILD_TARGETS_SCRIPT": str(tools / "build-targets.py"),
            "VERIFY_PY_TRACE": str(outside / "unexpected-trace"),
        }
        _ = environment.pop("IN_NIX_SHELL", None)
        for arguments in ((), ("-h",), ("--help",), ("help", "test")):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [bash, str(script), *arguments], cwd=outside, env=environment,
                    capture_output=True, text=True, check=False, timeout=5,
                )
                self.assertEqual(result.returncode, 0 if arguments else 2, result.stdout + result.stderr)
                self.assertIn("Usage:", result.stdout if arguments else result.stderr)
        self.assertFalse(called.exists())
        self.assertEqual(sorted(path.name for path in outside.iterdir()), ["tools", "verify.sh"])
        self.assertEqual(list((self.root / "verify-steps").glob("*.json")), [])
        self.assertEqual(self.cargo_calls(), [])

    def test_unknown_verb_from_native_worker_prints_usage_without_creating_a_run(self) -> None:
        detached_runs = self.root / "detached_runs"
        detached_script = self.root / "detached-runs.py"
        self.write_executable(
            detached_script,
            f"from pathlib import Path\nruns = Path({str(detached_runs)!r})\n"
            + "runs.mkdir()\n(runs / 'unexpected.json').write_text('{}')\n",
        )
        self.environment.update({
            "CODEX_THREAD_ID": "test-native-worker",
            "VERIFY_DETACHED_RUNS_SCRIPT": str(detached_script),
        })
        result = self.invoke_verify("fmt", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("Usage:", result.stderr)
        self.assertEqual(list(detached_runs.glob("*.json")), [])
        self.assertEqual(list((self.root / "verify-steps").glob("*.json")), [])
        self.assertEqual(self.cargo_calls(), [])

    def test_unknown_verb_prints_usage_before_target_query_or_step_record(self) -> None:
        target_query = self.root / "target-query"
        self.write_executable(
            self.root / "build-targets.py",
            f"from pathlib import Path\nPath({str(target_query)!r}).touch()\nraise SystemExit(2)\n",
        )
        result = self.invoke_verify("fmt", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("Usage:", result.stderr)
        self.assertFalse(target_query.exists())
        self.assertEqual(list((self.root / "verify-steps").glob("*.json")), [])
        self.assertEqual(self.cargo_calls(), [])


class VerifyWorkspaceFormTests(FinalPassFixture):
    def recorded_calls(self) -> list[dict[str, object]]:
        rows = [cast(dict[str, object], json.loads(line))
                for path in Path(self.environment["BUILDLOG_DIR"]).rglob("*.jsonl")
                for line in path.read_text().splitlines()]
        return [row for row in rows if row.get("kind") == "call" and row.get("tool") == "verify.sh"]

    def test_check_without_package_preserves_features_and_records_workspace(self) -> None:
        self.environment["BUILDLOG_OFF"] = "0"
        result = self.verify("check", "--features", "foo")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("check --workspace --lib --bins --features foo,extra", self.calls_made())
        self.assertEqual([(row["package"], row["command"]) for row in self.recorded_calls()],
                         [("workspace", "check --features foo")])

    def test_test_without_package_runs_workspace_tests(self) -> None:
        result = self.verify("test")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = [call.split() for call in self.calls_made() if call.startswith("nextest run ")]
        self.assertEqual(len(calls), 1)
        self.assertIn("--workspace", calls[0])
        self.assertIn("--lib", calls[0])
        self.assertIn("--bins", calls[0])
        self.assertIn("--tests", calls[0])
        self.assertNotIn("-E", calls[0])

    def test_named_final_records_its_package_without_a_saved_pass(self) -> None:
        self.environment["BUILDLOG_OFF"] = "0"
        result = self.verify("final", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("check --workspace --all-targets --features extra", self.calls_made())
        calls = [call for call in self.calls_made() if call.startswith("nextest run ")]
        self.assertEqual(len(calls), 1)
        self.assertIn("--workspace --tests -E package(sample)", calls[0])
        self.assertEqual([row["package"] for row in self.recorded_calls()], ["sample"])
        self.assertEqual(self.pass_files(), [])


if __name__ == "__main__":
    _ = unittest.main()
