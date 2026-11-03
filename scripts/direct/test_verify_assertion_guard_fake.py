"""Exercise both verifier fixtures' guard CLI and production cache protocol."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import unittest

from scripts.direct import verify_assertion_guard_test_support as fake_guard
from scripts.direct import verify_lint_scope_test_support as lint_support
from scripts.direct.tree_watch import AssertionGuard
from scripts.direct.verify_tree_cancel_test_support import VerifyTreeCancelFixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


# Check the same interface that tree_watch loads from each private script.
GUARD: AssertionGuard = fake_guard


class VerifyAssertionGuardFakeTests(unittest.TestCase):
    def test_both_private_guards_support_cache_keys_and_cli_execution(self) -> None:
        for fixture_type in (lint_support.VerifyLintScopeFixture, VerifyTreeCancelFixture):
            with self.subTest(fixture=fixture_type.__name__):
                fixture_type.setUpClass()
                self.addCleanup(fixture_type.doClassCleanups)
                fixture = fixture_type()
                self.addCleanup(fixture.doCleanups)
                fixture.setUp()
                cwd = fixture.workspace if isinstance(fixture, VerifyTreeCancelFixture) else fixture.root
                script = Path(fixture.environment["VERIFY_ASSERTION_GUARD_SCRIPT"])
                self.assertEqual(script.read_bytes(), Path(fake_guard.__file__).read_bytes())
                key = subprocess.run(
                    [sys.executable, str(lint_support.VERIFY.with_name("tree_watch.py")), "--key",
                     "--assertion-guard", str(script), "--", "lint", "sample"],
                    input=fixture.environment["TEST_METADATA"], cwd=cwd, env=fixture.environment,
                    capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS,
                )
                self.assertEqual(key.returncode, 0, key.stdout + key.stderr)
                self.assertRegex(key.stdout, r"\A[0-9a-f]{64}\n\Z")
                self.assertEqual(key.stderr, "")
                cli = subprocess.run(
                    [sys.executable, str(script), "--repo", str(cwd)],
                    cwd=cwd, env=fixture.environment, capture_output=True, text=True,
                    timeout=TEST_HANG_GUARD_SECS,
                )
                self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)
                self.assertEqual(cli.stderr, "")


if __name__ == "__main__":
    _ = unittest.main()
