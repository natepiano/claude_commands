"""Check formatting and judgment after a failed clippy repair."""

from __future__ import annotations

import unittest
from scripts.direct import verify_lint_scope_test_support as support


class VerifyLintScopeTests(support.VerifyLintScopeFixture):
    def test_failed_fix_still_formats_and_returns_judgment_status(self) -> None:
        self.environment["TEST_CLIPPY_FIX_STATUS"] = "41"
        for judgment_status in (0, 43):
            with self.subTest(judgment_status=judgment_status):
                self.environment["TEST_CLIPPY_STATUS"] = str(judgment_status)
                result = self.invoke_verify("lint", "sample", cwd=self.root)
                self.assertEqual(result.returncode, judgment_status, result.stdout + result.stderr)
                self.assert_fix_and_judgment_calls()
                stages = self.lint_stages()
                self.assertEqual(stages[:4], [
                    ["+nightly", "fmt", "-p", "sample"],
                    ["clippy", *self.calls("clippy")[0]],
                    ["+nightly", "fmt", "-p", "sample"],
                    ["clippy", *self.calls("clippy")[1]],
                ])
                self.assertEqual(self.calls("doc"), [])


if __name__ == "__main__":
    _ = unittest.main()
