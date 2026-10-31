"""Real verify.sh Mac routing through memory admission, with disposable tools."""
from __future__ import annotations

import unittest
from typing import final
from scripts.direct.verify_mac_memory_retry_test_support import VerifyMacMemoryRetryFixture
from scripts.direct.verify_mac_offload_test_support import HANG_GUARD_S


@final
class VerifyMacMemoryHandoffTests(VerifyMacMemoryRetryFixture):
    def test_leaving_memory_wait_after_release_runs_on_the_mac(self) -> None:
        cycle = self.routing_fixture()
        _ = self.hold_memory()
        once = self.runner_state / "free-once"
        process = self.start_routing(once=once)
        self.release_local_hold()
        _ = (self.runner_state / "continue-sleep").write_text("continue\n")
        # Routing asks free-now once after its sleep; later calls are the hook's.
        self.wait_until(lambda: len(self.free_now_settings()) >= 2, "the leave hook", (process,))
        self.assertEqual(self.local_runs(), [])
        _ = (self.runner_state / "mac-free").write_text("free\n")
        _ = once.write_text("free\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(self.local_runs(), [])
        runners = self.runner_events()
        self.assertEqual(len(runners), 2)

        self.assert_no_pending_calls(cycle)
        self.assertEqual(self.routing_record()["target"], "mac")
        self.assertEqual(self.last_call()["mac"], "passed_filter")


if __name__ == "__main__":
    _ = unittest.main()
