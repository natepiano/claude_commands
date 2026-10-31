"""Check shell invocation output and failures of tee and lint configuration."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import final
import unittest

from scripts.direct.verify_lint_scope_test_support import SCRIPTS, TEST_HANG_GUARD_SECS, VERIFY


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
