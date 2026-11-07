"""Real verify.sh Mac copy results with disposable tools."""
from __future__ import annotations

import unittest
from typing import final
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture
import signal
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@final
class VerifyMacCopyResultsTests(VerifyMacOffloadFixture):
    def test_copy_failure_first_stops_natedev_and_exits_with_the_copy_status(self) -> None:
        process, _ = self.start_copy_call(copy={"TEST_COPY_MAC": "failed", "TEST_COPY_STATUS": "42"})
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 42, output + errors)
        self.assertEqual((self.runner_state / "cargo-stopped").read_text(), str(signal.SIGTERM))
        self.assertEqual(self.saved_results(), [])
        self.assert_copy_record("won", "mac")
        record = self.last_call()
        self.assertEqual((record["status"], record["mac"], record["mac_reason"], record["mac_s"]),
                         (42, "failed", "", 2.5))

    def test_copy_filtered_pass_first_stops_natedev_and_records_a_mac_pass(self) -> None:
        process, _ = self.start_copy_call("--filter", "alpha", copy={"TEST_COPY_MAC": "passed", "TEST_COPY_STATUS": "0"})
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertIn("PASS on the Mac (macOS), so nothing ran on natedev.", output)
        self.assertEqual((self.runner_state / "cargo-stopped").read_text(), str(signal.SIGTERM))
        saved = self.saved_results()
        self.assertEqual(len(saved), 1)
        self.assertIn("host=mac\n", saved[0].read_text())
        self.assert_copy_record("won", "mac")
        record = self.last_call()
        self.assertEqual((record["status"], record["mac"], record["mac_s"]), (0, "passed_filter", 2.5))

    def test_copy_full_pass_first_stops_natedev_and_ends_the_call(self) -> None:
        process, _ = self.start_copy_call(copy={"TEST_COPY_MAC": "passed", "TEST_COPY_STATUS": "0"})
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertIn("PASS on the Mac (macOS), so nothing ran on natedev.", output)
        self.assertEqual((self.runner_state / "cargo-stopped").read_text(), str(signal.SIGTERM))
        saved = self.saved_results()
        self.assertEqual(len(saved), 1)
        self.assertIn("host=mac\n", saved[0].read_text())
        self.assert_copy_record("won", "mac")
        record = self.last_call()
        self.assertEqual((record["status"], record["mac"], record["mac_s"]), (0, "passed_full", 2.5))


if __name__ == "__main__":
    _ = unittest.main()
