"""Check formatting and judgment after a failed clippy repair."""

from __future__ import annotations

import unittest
from scripts.direct.test_verify_lint_judgment import VerifyLintJudgmentFixture


class VerifyLintScopeTests(VerifyLintJudgmentFixture):
    def test_failed_fix_still_formats_and_returns_judgment_status(self) -> None:
        self.environment["TEST_CLIPPY_FIRST_STATUS"] = "101"
        self.environment["TEST_CLIPPY_FIX_STATUS"] = "41"
        for judgment_status in (0, 43):
            with self.subTest(judgment_status=judgment_status):
                self.environment["TEST_CLIPPY_STATUSES"] = f"[101, {judgment_status}]"
                result = self.invoke_verify("lint", "sample", cwd=self.root)
                self.assertEqual(result.returncode, judgment_status, result.stdout + result.stderr)
                self.assert_fix_and_judgment_calls()
                stages = self.lint_stages()
                self.assertEqual(stages, [
                    ["+nightly", "fmt", "-p", "sample"],
                    ["clippy", *self.calls("clippy")[0]],
                    ["clippy", *self.calls("clippy")[1]],
                    ["+nightly", "fmt", "-p", "sample"],
                    ["clippy", *self.calls("clippy")[2]],
                ])
                self.assertEqual(self.calls("doc"), [])


if __name__ == "__main__":
    _ = unittest.main()
