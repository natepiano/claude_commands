"""Real verify entry point cancellation with private Cargo and Git fixtures."""
from __future__ import annotations

import subprocess
import unittest

from scripts.direct.verify_tree_cancel_test_support import CANCEL_LINE as CANCEL_LINE, VerifyTreeCancelFixture as VerifyTreeCancelFixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class VerifyTreeCancelTests(VerifyTreeCancelFixture):
    def test_source_edit_cancels_once_releases_resources_and_records_no_verdict(self) -> None:
        process = self.start_verify("test")
        self.wait_for_running(self.control / "running", process)
        self.assert_resources_held()
        self.edit_source()
        self.assert_cancelled(process, "test")
        attempts = [args for args in self.cargo_calls() if args[:2] == ["nextest", "run"]]
        self.assertEqual(len(attempts), 1, "cancelled call restarted its tests")

    def test_external_document_edit_does_not_cancel_test(self) -> None:
        process = self.start_verify("test", TEST_FINISH="1")
        self.wait_for_running(self.control / "running", process)
        _ = (self.workspace / "docs/design.md").write_text("new documentation\n")
        with self.assertRaises(subprocess.TimeoutExpired):
            _ = process.communicate(timeout=1)
        # A Cargo-owned handshake makes this a live span, not an already-ended run.
        _ = (self.control / "release").touch()
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertNotIn("cancelled", output + errors)
        self.assert_no_cache_verdict()
        self.assert_resources_released()


if __name__ == "__main__":
    _ = unittest.main()
