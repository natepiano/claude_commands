"""Real verify.sh Mac routing through memory admission, with disposable tools."""
from __future__ import annotations

import unittest
from typing import final
from scripts.direct.verify_mac_memory_retry_test_support import VerifyMacMemoryRetryFixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@final
class VerifyMacMemoryRetryTests(VerifyMacMemoryRetryFixture):
    def test_mac_busy_again_after_leaving_returns_to_the_local_wait(self) -> None:
        meminfo = self.hold_memory()
        process = self.start_verify("test", "sample", "--no-cache", extra={
            "TEST_RUNNER_BUSY_UNTIL": str(self.runner_state / "mac-free"), **self.leave_once()})
        self.addCleanup(self.stop_verify, process)
        # The Mac claim declined again; the local run waits with the hook still set.
        self.wait_for_path(self.runner_state / "free-now-after-leave", (process,))
        self.assertEqual(len(self.runner_events()), 2)
        self.assertEqual(self.local_runs(), [])
        self.release_memory(meminfo)
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(len(self.local_runs()), 1)
        self.assertEqual(len(self.runner_events()), 2)
        self.assertEqual(output.count("verify.sh: running on natedev"), 2)
        self.assertEqual(self.routing_record()["target"], "natedev")
        record = self.last_call()
        self.assertEqual((record["mac"], record["mac_reason"]), ("declined", "busy"))


if __name__ == "__main__":
    _ = unittest.main()
