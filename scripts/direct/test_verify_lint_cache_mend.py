"""Check mend rewrites are cached under the resulting tree."""

from __future__ import annotations

import unittest
from scripts.direct import verify_lint_cache_test_support as support


class VerifyLintScopeTests(support.VerifyLintCacheFixture):
    def test_mend_rewrite_is_recorded_under_the_tree_it_leaves(self) -> None:
        cache = self.dirty_cache()
        self.environment["TEST_EDIT_DURING"] = "mend"
        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("the tree changed", result.stdout)
        self.assertEqual(len(list(cache.glob("*.pass"))), 1)

        self.environment["TEST_EDIT_DURING"] = ""
        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS (recorded)", result.stdout)
        self.assertFalse(self.clippy_ran())


if __name__ == "__main__":
    _ = unittest.main()
