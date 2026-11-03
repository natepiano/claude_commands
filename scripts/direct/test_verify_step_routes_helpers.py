"""Real verify step routing: helpers."""
from __future__ import annotations

import unittest

from scripts.direct.verify_step_routes_test_support import VerifyStepRoutesFixture


class VerifyStepRoutesTests(VerifyStepRoutesFixture):
    def test_hold_after_local_fix_keeps_fmt_probe_and_metadata_running(self) -> None:
        self.hold_after_local_mend(0)
        result = self.run_verify("lint", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_lint_helpers_protected()

    def test_hold_after_failed_local_judgment_keeps_helpers_and_failure_status(self) -> None:
        self.hold_after_local_mend(7)
        result = self.run_verify("lint", "--workspace", "--check")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("style-lint: rustfmt passed", result.stdout)
        self.assertIn("style-lint: clippy passed", result.stdout)
        self.assert_lint_helpers_protected()

if __name__ == "__main__":
    _ = unittest.main()
