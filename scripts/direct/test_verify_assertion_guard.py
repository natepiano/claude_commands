"""Run verify's lint case with the real assertion guard and git, without build admission."""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from typing import override

from scripts.direct.test_assertion_guard import GitRepositoryFixture, TILE_NEW, TILE_OLD
from scripts.direct import test_verify_lint_scope as verify_fixture

VERIFY = Path(__file__).with_name("verify.sh")


class VerifyAssertionGuardTests(GitRepositoryFixture):
    home: Path = Path()
    session: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.home = self.root / "home"
        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".claude/scripts").symlink_to(VERIFY.parent.parent, target_is_directory=True)
        self.session = self.root / "session"
        self.session.mkdir()
        self.change("src/lib.rs", self.rust(TILE_OLD), self.rust(TILE_NEW))

    def lint(self, *arguments: str, with_session: bool = False, member_cwd: bool = False) -> subprocess.CompletedProcess[str]:
        # Use the real lint case; stub only the unrelated build tools and their setup.
        source = VERIFY.read_text(encoding="utf-8")
        case = source.split("\n    lint)\n", 1)[1].split("\n    doc)\n", 1)[0]
        rulings_setup = "assertion_rulings_args() {" + source.split(
            "assertion_rulings_args() {", 1)[1].split("# A Codex mesh worker", 1)[0]
        script = r'''
set -euo pipefail
FEATURE_FLAGS=()
HOLD_LINT=0
RUN_LOG=""
CHANGED_MEMBERS_PY='print("")'
require_member() { :; }
require_taplo() { :; }
usage() { exit 2; }
lint_config_enabled() { return 0; }
lint_config_skip_notice() { :; }
invoke_mend() { echo 'fixture: mend'; }
fmt_cargo() { :; }
invoke_clippy_fix() { :; }
invoke_clippy() { :; }
tree_key() { echo fixed-tree; }
taplo() { :; }
run() { "$@"; }
cargo() { printf '{"workspace_root":"%s"}\n' "$TEST_REPO"; }
'''
        script += rulings_setup + "\ncase lint in\n    lint)\n" + case + "\nesac\n"
        environment = {**self.environment, "HOME": str(self.home), "PY": sys.executable, "TEST_REPO": str(self.repo),
                       "ASSERTION_GUARD_SCRIPT": str(VERIFY.with_name("assertion_guard.py"))}
        for name in ("UNIT_SESSION_DIR", "WORKSPACE_ROOT"):
            _ = environment.pop(name, None)
        if with_session:
            environment["UNIT_SESSION_DIR"] = str(self.session)
        return subprocess.run(
            ["bash", "-c", script, "lint-test", *arguments],
            cwd=self.repo / "src" if member_cwd else self.repo, env=environment,
            capture_output=True, text=True, check=False, timeout=5,
        )

    def test_full_verify_entry_point_uses_guard_override_and_stops_before_lint_tools(self) -> None:
        fixture = verify_fixture.VerifyLintScopeTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        guard = fixture.root / "refuse-assertion-change.py"
        fixture.write_executable(guard, 'print("fixture: assertion guard refused")\nraise SystemExit(1)\n')
        fixture.environment["VERIFY_ASSERTION_GUARD_SCRIPT"] = str(guard)
        result = fixture.invoke_verify("lint", "--workspace", "--check", "--no-cache", cwd=fixture.root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("fixture: assertion guard refused", result.stdout)
        self.assertIn("style-lint: assertion guard start", result.stdout.splitlines())
        self.assertNotIn("style-lint: assertion guard passed", result.stdout.splitlines())
        self.assertEqual(fixture.calls("mend"), [])
        self.assertEqual(fixture.calls("+nightly"), [])
        self.assertEqual(fixture.calls("clippy"), [])
        self.assertEqual(fixture.taplo_calls(), [])

    def test_lint_refuses_weaker_assertion_before_mend_in_all_modes(self) -> None:
        for arguments in (("sample",), ("--workspace",), ("--workspace", "--check")):
            with self.subTest(arguments=arguments):
                result = self.lint(*arguments, member_cwd=True)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("src/lib.rs:3 test_value weaker:", result.stdout)
                self.assertNotIn("fixture: mend", result.stdout)
                if arguments[0] == "--workspace":
                    self.assertIn("style-lint: assertion guard start", result.stdout.splitlines())
                    self.assertNotIn("style-lint: assertion guard passed", result.stdout.splitlines())

    def test_lint_accepts_ruling_from_unit_session_directory(self) -> None:
        _ = (self.session / "work-order.md").write_text(
            "Assertion-Change: src/lib.rs::test_value — Approved bound contract.\n", encoding="utf-8")
        for arguments in (("sample",), ("--workspace",), ("--workspace", "--check")):
            with self.subTest(arguments=arguments):
                result = self.lint(*arguments, with_session=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("ruled: Approved bound contract.", result.stdout)
                self.assertIn("fixture: mend", result.stdout)
                if arguments[0] == "--workspace":
                    lines = result.stdout.splitlines()
                    self.assertLess(lines.index("style-lint: assertion guard start"),
                                    lines.index("style-lint: assertion guard passed"))
                    self.assertLess(lines.index("style-lint: assertion guard passed"), lines.index("fixture: mend"))

    def test_lint_fails_closed_when_guard_cannot_resolve_base(self) -> None:
        _ = self.git("remote", "remove", "origin")
        result = self.lint("--workspace", "--check")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn("VERIFY_ASSERTION_BASE", result.stderr)
        self.assertNotIn("fixture: mend", result.stdout)

    def test_lint_passes_explicit_base_to_guard_in_all_modes(self) -> None:
        # Upstream contains the stronger assertion. An explicit base at the new
        # assertion should let lint proceed; dropping --base would block it.
        _ = self.git("add", "src/lib.rs")
        _ = self.git("commit", "-q", "-m", "branch assertion change")
        _ = self.git("tag", "assertion-base")
        self.environment["VERIFY_ASSERTION_BASE"] = "assertion-base"
        for arguments in (("sample",), ("--workspace",), ("--workspace", "--check")):
            with self.subTest(arguments=arguments):
                result = self.lint(*arguments, member_cwd=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("assertion guard: no removed or weaker assertions", result.stdout)
                self.assertIn("fixture: mend", result.stdout)

    def test_invalid_explicit_base_is_not_silently_replaced_with_upstream(self) -> None:
        self.environment["VERIFY_ASSERTION_BASE"] = "missing-assertion-base"
        result = self.lint("--workspace", "--check")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertNotIn("fixture: mend", result.stdout)


if __name__ == "__main__":
    _ = unittest.main()
