"""Check lint repairs converge before the first judgment."""

from __future__ import annotations

import unittest
from scripts.direct import verify_lint_cache_test_support as support
import json


class VerifyLintScopeTests(support.VerifyLintCacheFixture):
    def test_fix_exposes_another_fix_before_the_first_judgment(self) -> None:
        original = "pub fn sum(a: f64, b: f64, c: f64) -> f64 { a * b + c }\n"
        first_fix = original.replace("a * b + c", "a.mul_add(b, c)")
        final_fix = first_fix.replace("pub fn", "pub const fn")
        self.environment.update({
            "TEST_GIT_DIRTY": "1",
            "TEST_FIX_STEPS": json.dumps({original: first_fix, first_fix: final_fix}),
            "TEST_CLIPPY_EXPECT_SOURCE": final_fix,
        })
        for cache_enabled in (False, True):
            with self.subTest(cache_enabled=cache_enabled):
                if cache_enabled:
                    _ = self.enable_cache()
                _ = (self.root / "src/lib.rs").write_text(original, encoding="utf-8")
                _ = self.verify("lint", "sample")
                # Two passes change the source; the third confirms no change.
                self.assert_fix_and_judgment_calls(fix_passes=3)
                self.assertEqual((self.root / "src/lib.rs").read_text(encoding="utf-8"), final_fix)
                self.assertEqual(self.calls("doc"), [])
                self.assertEqual([args for args in self.calls("+nightly") if "--version" not in args],
                                 [["fmt", "-p", "sample"]] * 4)


if __name__ == "__main__":
    _ = unittest.main()
