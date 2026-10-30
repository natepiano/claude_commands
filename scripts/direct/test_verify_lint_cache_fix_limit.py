"""Check a passing judgment at the repair limit is reused."""

from __future__ import annotations

import unittest
from scripts.direct import verify_lint_cache_test_support as support


class VerifyLintScopeTests(support.VerifyLintCacheFixture):
    def test_passing_judgment_at_fix_pass_limit_is_cached(self) -> None:
        cache = self.dirty_cache()
        self.environment["TEST_FIX_APPEND"] = "// fix pass\n"
        _ = self.verify("lint", "sample")
        self.assert_fix_and_judgment_calls(fix_passes=3)
        self.assertEqual(len(list(cache.glob("*.pass"))), 1)

        result = self.verify("lint", "sample")
        self.assertIn("PASS (recorded)", result.stdout)
        self.assertFalse(self.clippy_ran())


if __name__ == "__main__":
    _ = unittest.main()
