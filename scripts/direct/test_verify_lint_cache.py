"""Check lint cache keys follow the final repaired tree."""

from __future__ import annotations

import unittest
from scripts.direct import verify_lint_scope_test_support as support


class VerifyLintScopeTests(support.VerifyLintScopeFixture):
    def test_passing_judgment_at_fix_pass_limit_is_cached(self) -> None:
        cache = self.dirty_cache()
        self.environment["TEST_FIX_APPEND"] = "// fix pass\n"
        _ = self.verify("lint", "sample")
        self.assert_fix_and_judgment_calls(fix_passes=3)
        self.assertEqual(len(list(cache.glob("*.pass"))), 1)

        result = self.verify("lint", "sample")
        self.assertIn("PASS (recorded)", result.stdout)
        self.assertFalse(self.clippy_ran())

    def test_fixed_and_formatted_tree_is_cached(self) -> None:
        cache = self.dirty_cache()
        formatted = "pub fn value() -> u8 {\n    2\n}\n"
        self.environment.update({
            "TEST_FIX_SOURCE": "pub fn value()->u8{2}\n",
            "TEST_FMT_SOURCE": formatted,
        })
        result = self.verify("lint", "sample")
        self.assert_fix_and_judgment_calls(fix_passes=2)
        self.assertEqual((self.root / "src/lib.rs").read_text(encoding="utf-8"), formatted)
        self.assertNotIn("the tree changed while lint ran", result.stdout)
        self.assertEqual(len(list(cache.glob("*.pass"))), 1)

        result = self.verify("lint", "sample")
        self.assertIn("PASS (recorded)", result.stdout)
        self.assertFalse(self.clippy_ran())

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
