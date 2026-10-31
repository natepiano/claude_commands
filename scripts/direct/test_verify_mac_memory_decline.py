"""Real verify.sh Mac routing through memory admission, with disposable tools."""
from __future__ import annotations

import unittest
from typing import final
from scripts.direct.verify_mac_memory_retry_test_support import VerifyMacMemoryRetryFixture
from scripts.direct.verify_mac_offload_test_support import HANG_GUARD_S


@final
class VerifyMacMemoryDeclineTests(VerifyMacMemoryRetryFixture):
    def test_permanent_mac_decline_has_no_leave_hook(self) -> None:
        meminfo = self.hold_memory()
        self.set_mac_answer("declined", "no_tests", 75)
        clock = self.control_routing_clock()
        self.environment["TEST_FREE_NOW_FREE"] = "1"
        process = self.start_verify("test", "sample", "--no-cache")
        self.addCleanup(self.stop_verify, process)
        self.wait_for_path(self.runner_state / "sleep-events", (process,))
        _ = clock.write_text("1000")
        self.release_memory(meminfo)
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(len(self.runner_events()), 1)
        self.assertEqual(self.free_now_settings(), [])
        self.assertEqual(len(self.local_runs()), 1)


if __name__ == "__main__":
    _ = unittest.main()
