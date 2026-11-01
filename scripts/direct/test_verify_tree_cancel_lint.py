"""Real verify entry point cancellation with private Cargo and Git fixtures."""
from __future__ import annotations

import unittest

from scripts.direct.verify_tree_cancel_test_support import VerifyTreeCancelFixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class VerifyTreeCancelTests(VerifyTreeCancelFixture):
    def test_lint_rewrites_finish_without_cancelling(self) -> None:
        process = self.start_verify("lint", TEST_BLOCK_STAGE="clippy", TEST_FINISH="1",
                                    TEST_REWRITE_STAGES="mend,fix,fmt")
        self.release_lint_rewrites(process)
        self.wait_for_running(self.control / "running", process)
        self.assert_resources_held()
        _ = (self.control / "release").touch()
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertNotIn("cancelled", output + errors)
        for stage in ("mend", "fix", "fmt"):
            self.assertTrue((self.control / (stage + ".rewritten")).exists(), f"{stage} never rewrote an input")
        self.assertEqual(len(list((self.board / "verify_cache").glob("*.pass"))), 1,
                         "lint rewrites prevented its final verdict from being recorded")
        self.assert_resources_released()

    def test_edit_during_clippy_judgment_cancels_lint(self) -> None:
        process = self.start_verify("lint", TEST_BLOCK_STAGE="clippy")
        self.wait_for_running(self.control / "running", process)
        self.assert_resources_held()
        self.edit_source()
        self.assert_cancelled(process, "lint")
        judgments = [args for args in self.cargo_calls() if args[:1] == ["clippy"] and "--fix" not in args]
        self.assertEqual(len(judgments), 1, "cancelled call restarted clippy")


if __name__ == "__main__":
    _ = unittest.main()
