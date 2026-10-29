"""Check lint command scope and invocation output through the real shell pipeline."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import final
import unittest

from scripts.direct import verify_lint_scope_test_support as support
from scripts.direct.verify_lint_scope_test_support import CARGO_STUB as CARGO_STUB
from scripts.direct.verify_lint_scope_test_support import SCRIPTS, TEST_HANG_GUARD_SECS, VERIFY


class VerifyLintScopeTests(support.VerifyLintScopeFixture):
    def test_unchanged_tree_runs_one_fix_pass_before_judgment_without_rustdoc(self) -> None:
        result = self.verify("lint", "--workspace")
        self.assert_fix_and_judgment_calls()
        self.assertEqual(self.lint_stages(), [
            ["+nightly", "fmt", "--all"],
            ["clippy", *self.calls("clippy")[0]],
            ["+nightly", "fmt", "--all"],
            ["clippy", *self.calls("clippy")[1]],
        ])
        self.assertIn("style-lint: clippy start", result.stdout.splitlines())
        self.assertIn("style-lint: clippy passed", result.stdout.splitlines())

    def test_check_lint_only_judges_without_fixes(self) -> None:
        _ = self.verify("lint", "--workspace", "--check")
        self.assertEqual(self.calls("clippy"), [[
            "--workspace", "--all-targets", "--all-features", "--", "-D", "warnings",
        ]])
        self.assertEqual(self.calls("mend"), [["--all-targets", "--workspace", "--fail-on-warn"]])
        self.assertEqual(self.calls("+nightly"), [["fmt", "--version"], ["fmt", "--all", "--check"]])
        self.assertEqual(self.calls("doc"), [])
        self.assert_workspace_taplo_check()

    def test_lint_matches_ci_and_preserves_other_stages(self) -> None:
        _ = self.verify("lint", "sample")
        self.assert_workspace_taplo_check()
        self.assert_fix_and_judgment_calls()
        self.assertEqual(self.calls("mend"), [["--all-targets", "--workspace", "--fix"]])
        self.assertEqual(self.calls("+nightly"), [
            ["fmt", "--version"], ["fmt", "-p", "sample"],
            ["fmt", "--version"], ["fmt", "-p", "sample"],
        ])
        self.assertEqual(self.calls("doc"), [])

    def test_lint_rejects_features_now_owned_by_doc(self) -> None:
        result = self.invoke_verify("lint", "sample", "--features", "a", cwd=self.root)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("--features", result.stdout + result.stderr)
        self.assertEqual(self.calls("mend"), [])
        self.assertEqual(self.calls("clippy"), [])
        self.assertEqual(self.calls("doc"), [])
        self.assertEqual(self.calls("+nightly"), [])

    def test_example_keeps_its_single_target_and_required_features(self) -> None:
        for example, features in (("plain", []), ("featured", ["--features", "sample/a"])):
            with self.subTest(example=example):
                _ = self.verify("example", "sample", example)
                self.assertEqual(self.calls("clippy"), [[
                    "--workspace", "--example", example, *features, "--", "-D", "warnings"
                ]])
                self.assertEqual(self.calls("mend"), [])
                self.assertEqual(self.calls("doc"), [])
                self.assertEqual(self.calls("+nightly"), [])
                self.assertEqual(self.taplo_calls(), [])

    def test_clippy_off_names_the_full_scope_without_running_it(self) -> None:
        self.write_config(clippy="off")
        result = self.verify("lint", "sample")
        skip_lines = [line for line in result.stdout.splitlines()
                      if line.startswith("SKIPPED:") and "clippy" in line]
        self.assertEqual(skip_lines, [
            "SKIPPED: cargo clippy --workspace --all-targets --all-features"
            + f" — clippy=off in {self.config}"
        ])
        self.assertEqual(self.calls("clippy"), [])
        self.assertEqual(len(self.calls("mend")), 1)
        self.assertEqual(self.calls("doc"), [])
        self.assert_workspace_taplo_check()


@final
class InvokeOutputTests(unittest.TestCase):
    def function_body(self, name: str) -> str:
        source = (SCRIPTS / "lint" / "invoke.sh").read_text(encoding="utf-8")
        match = re.search(rf"^{name}\(\) [{{(]\n.*?^[}})]$", source, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(match, name)
        assert match is not None
        return match.group()

    def shell(self, script: str, root: Path) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ, HOME=str(root), TMPDIR=str(root))
        return subprocess.run(
            ["bash", "-c", script], cwd=root, env=environment,
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
        )

    def command_stubs(self) -> str:
        return r'''
set -euo pipefail
SANDBOX_SIGNATURE=not-a-sandbox-failure
buildlog_step_compiles() { return 1; }
buildlog_begin() { :; }
buildlog_exec() { "$@"; }
buildlog_finish_memory() { :; }
buildlog_end() { :; }
sweep_after_step() { :; }
'''

    def test_shell_libraries_use_no_named_descriptor_redirections(self) -> None:
        for path in (SCRIPTS / "lint" / "invoke.sh", VERIFY):
            with self.subTest(path=path):
                self.assertNotRegex(path.read_text(encoding="utf-8"), r"\{[A-Za-z_][A-Za-z_0-9]*\}[<>]")

    def test_ordinary_step_returns_command_status_when_tee_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "bin"
            binary.mkdir()
            tee = binary / "tee"
            _ = tee.write_text("#!/bin/sh\ncat\nexit 7\n", encoding="utf-8")
            tee.chmod(0o755)
            for status in (0, 47):
                with self.subTest(status=status):
                    script = self.command_stubs() + self.function_body("run_once")
                    script += f'\nexport PATH="$PWD/bin:$PATH"\nif run_once bash -c "exit {status}"; then result=0; else result=$?; fi\nexit "$result"\n'
                    result = self.shell(script, root)
                    self.assertEqual(result.returncode, status, result.stdout + result.stderr)

    def test_clippy_fix_preserves_extra_lints_and_run_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = "set -euo pipefail\n"
            script += self.function_body("invoke_clippy") + "\n"
            script += self.function_body("invoke_clippy_fix") + r'''
lint_config_enabled() { return 0; }
run() { printf '%s\n' "$@"; return 47; }
invoke_clippy_fix -p sample --lib -- -A clippy::type_complexity
'''
            result = self.shell(script, root)
            self.assertEqual(result.returncode, 47, result.stdout + result.stderr)
            self.assertEqual(result.stdout.splitlines(), [
                "cargo", "clippy", "--fix", "--allow-dirty", "--allow-staged", "--allow-no-vcs",
                "-p", "sample", "--lib", "--", "-D", "warnings", "-A", "clippy::type_complexity",
            ])

    def test_clippy_fix_off_prints_skip_notice_without_running(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = "set -euo pipefail\n"
            script += self.function_body("invoke_clippy") + "\n"
            script += self.function_body("invoke_clippy_fix") + r'''
lint_config_enabled() { return 1; }
lint_config_skip_notice() { printf 'SKIPPED: %s\n' "$2"; }
run() { printf 'unexpected run\n'; return 99; }
invoke_clippy_fix --workspace --all-targets --all-features --keep-going
'''
            result = self.shell(script, root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.splitlines(), [
                "SKIPPED: cargo clippy --fix --allow-dirty --allow-staged --allow-no-vcs"
                + " --workspace --all-targets --all-features --keep-going",
            ])


if __name__ == "__main__":
    _ = unittest.main()
