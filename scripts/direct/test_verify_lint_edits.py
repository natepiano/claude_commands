"""Check concurrent edits prevent recording a lint judgment."""

from __future__ import annotations

import unittest
from scripts.direct import verify_lint_scope_test_support as support


class VerifyLintScopeTests(support.VerifyLintScopeFixture):
    def test_edit_while_clippy_fails_records_nothing_and_the_next_lint_runs(self) -> None:
        cache = self.dirty_cache()
        self.environment.update({"TEST_EDIT_DURING": "clippy", "TEST_CLIPPY_STATUS": "101"})
        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 101, result.stdout + result.stderr)
        self.assertIn("the tree changed while lint ran, so this result was not recorded", result.stdout)
        self.assertEqual(sorted(path.name for path in cache.iterdir()), [])

        self.environment.update({"TEST_EDIT_DURING": "", "TEST_CLIPPY_STATUS": "0"})
        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("(recorded)", result.stdout)
        self.assertTrue(self.clippy_ran())
        self.assertEqual(len(list(cache.glob("*.pass"))), 1)

    def test_edit_while_clippy_passes_records_nothing(self) -> None:
        cache = self.dirty_cache()
        self.environment["TEST_EDIT_DURING"] = "clippy"
        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("the tree changed while lint ran", result.stdout)
        self.assertEqual(sorted(path.name for path in cache.iterdir()), [])


if __name__ == "__main__":
    _ = unittest.main()
