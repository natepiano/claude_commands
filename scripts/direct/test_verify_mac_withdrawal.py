"""Real verify.sh Mac withdrawal with disposable tools."""
from __future__ import annotations

import unittest
from pathlib import Path
from typing import final
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture
import signal
from scripts.direct.verify_mac_offload_test_support import HANG_GUARD_S as HANG_GUARD_S


@final
class VerifyMacWithdrawalTests(VerifyMacOffloadFixture):
    def test_mac_run_withdraws_local_demand_before_another_waiter_is_released(self) -> None:
        cycle_path = self.routing_fixture()
        process = self.start_routing(pause_retry=True)
        _ = (self.runner_state / "mac-free").write_text("free\n")
        _ = (self.runner_state / "continue-sleep").write_text("continue\n")
        self.wait_for_path(self.runner_state / "retry-running")
        self.assert_no_pending_calls(cycle_path)
        self.release_local_hold()
        self.assertFalse((Path(self.environment["BUILD_HOLD_DIR"]) / "hana").exists())
        self.assertIsNone(process.poll())

        _ = (self.runner_state / "continue-retry").write_text("continue\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(self.local_runs(), [])

    def test_interrupted_mac_retry_withdraws_registration_and_preserves_only_wait_time(self) -> None:
        cycle_path = self.routing_fixture()
        clock = self.control_routing_clock()
        process = self.start_routing(pause_retry=True)
        _ = clock.write_text("102")
        _ = (self.runner_state / "mac-free").write_text("free\n")
        _ = (self.runner_state / "continue-sleep").write_text("continue\n")
        self.wait_for_path(self.runner_state / "retry-running")
        _ = clock.write_text("500")
        process.send_signal(signal.SIGTERM)
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 143, output + errors)
        self.assert_no_pending_calls(cycle_path)
        record = self.routing_record()
        self.assertEqual(record["hold_wait_s"], 2)
        self.assertEqual(self.last_call()["outcome"], "interrupted")
        self.assertEqual(self.last_call()["mac_holder"], "other tests")


if __name__ == "__main__":
    _ = unittest.main()
