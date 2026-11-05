"""Check repaired and formatted trees are reused by the lint cache."""

from __future__ import annotations

import unittest
from scripts.direct.test_verify_lint_judgment import VerifyLintJudgmentFixture


class VerifyLintScopeTests(VerifyLintJudgmentFixture):
    def test_fixed_and_formatted_tree_is_cached(self) -> None:
        cache = self.dirty_cache()
        formatted = "pub fn value() -> u8 {\n    2\n}\n"
        self.environment.update({
            "TEST_CLIPPY_FIRST_STATUS": "101",
            "TEST_FIX_SOURCE": "pub fn value()->u8{2}\n",
            "TEST_FMT_SOURCE": formatted,
            "TEST_CLIPPY_EXPECT_SOURCE": formatted,
        })
        result = self.verify("lint", "sample")
        self.assert_fix_and_judgment_calls(fix_passes=2)
        self.assertEqual((self.root / "src/lib.rs").read_text(encoding="utf-8"), formatted)
        self.assertNotIn("the tree changed while lint ran", result.stdout)
        self.assertEqual(len(list(cache.glob("*.pass"))), 1)

        result = self.verify("lint", "sample")
        self.assertIn("PASS (recorded)", result.stdout)
        self.assertFalse(self.clippy_ran())


if __name__ == "__main__":
    _ = unittest.main()
