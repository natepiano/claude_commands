"""Check changing trees stop after the lint repair pass limit."""

from __future__ import annotations

import unittest
from scripts.direct import verify_lint_scope_test_support as support


class VerifyLintScopeTests(support.VerifyLintScopeFixture):
    def test_changing_tree_stops_after_three_fix_passes_and_failed_lint_runs_again(self) -> None:
        cache = self.enable_cache()
        self.environment.update({
            "TEST_FIX_FILE": "src/fix-progress.rs",
            "TEST_GIT_UNTRACKED": "src/fix-progress.rs",
            "TEST_FIX_APPEND": "// fix pass\n",
            "TEST_CLIPPY_STATUS": "101",
        })
        for call in (1, 2):
            with self.subTest(call=call):
                result = self.invoke_verify("lint", "sample", cwd=self.root)
                self.assertEqual(result.returncode, 101, result.stdout + result.stderr)
                self.assertNotIn("FAIL (recorded)", result.stdout)
                self.assert_fix_and_judgment_calls(fix_passes=3)
                self.assertEqual(list(cache.glob("*.fail")), [])
                self.assertEqual(self.calls("doc"), [])
                self.assertEqual((self.root / "src/fix-progress.rs").read_text(encoding="utf-8"),
                                 "// fix pass\n" * (3 * call))


if __name__ == "__main__":
    _ = unittest.main()
