"""Print help and refuse unknown verbs before verification starts."""

from __future__ import annotations

import shutil
import subprocess
import unittest

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
            "final-pass", "help",
        ])
        self.assertEqual([line.split()[0] for line in flags.splitlines()], [
            "--filter", "--features", "--no-cache", "--target", "--mac",
            "--local", "--session-dir",
        ])

    def test_each_command_has_separate_help(self) -> None:
        for command in (
            "check", "test", "lint", "doc", "example", "build", "final",
            "final-pass", "help", "example-test",
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

    def test_unknown_verb_from_mesh_worker_prints_usage_without_creating_a_run(self) -> None:
        mesh_runs = self.root / "mesh_runs"
        mesh_script = self.root / "mesh-runs.py"
        self.write_executable(
            mesh_script,
            f"from pathlib import Path\nruns = Path({str(mesh_runs)!r})\n"
            + "runs.mkdir()\n(runs / 'unexpected.json').write_text('{}')\n",
        )
        self.environment.update({
            "CODEX_THREAD_ID": "test-mesh-worker",
            "VERIFY_MESH_RUNS_SCRIPT": str(mesh_script),
        })
        result = self.invoke_verify("fmt", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("Usage:", result.stderr)
        self.assertEqual(list(mesh_runs.glob("*.json")), [])
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


if __name__ == "__main__":
    _ = unittest.main()
