"""Real verify.sh Mac routing through memory admission, with disposable tools."""
from __future__ import annotations

import unittest
from typing import final
from scripts.direct.verify_mac_memory_retry_test_support import VerifyMacMemoryRetryFixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@final
class VerifyMacMemoryHandoffTests(VerifyMacMemoryRetryFixture):
    def test_leaving_memory_wait_after_release_runs_on_the_mac(self) -> None:
        cycle = self.routing_fixture()
        _ = self.hold_memory()
        process = self.start_verify("test", "sample", "--filter", "alpha", "--no-cache", extra={
            "TEST_RUNNER_BUSY_UNTIL": str(self.runner_state / "mac-free"),
            "TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0",
        })
        self.addCleanup(self.stop_verify, process)
        self.release_local_hold()
        self.wait_for_memory_wait(process)
        self.wait_until(lambda: sum(key.destination.provider == "mac"
                                   for key in self.provider_observations) >= 2,
                        "two private provider observations", (process,))
        self.assertEqual(self.local_runs(), [])
        _ = (self.runner_state / "mac-free").write_text("free\n")
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(self.local_runs(), [])
        runners = self.runner_events()
        self.assertEqual(len(runners), 1)

        self.assert_no_pending_calls(cycle)
        self.assertEqual(self.routing_record()["target"], "mac")
        self.assertEqual(self.last_call()["mac"], "passed_filter")


if __name__ == "__main__":
    _ = unittest.main()
