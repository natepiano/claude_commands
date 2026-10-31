"""Real verify.sh Mac pacing with disposable tools."""
from __future__ import annotations

import unittest
from typing import final
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture
import signal
from scripts.direct.verify_mac_offload_test_support import HANG_GUARD_S as HANG_GUARD_S


@final
class VerifyMacPacingTests(VerifyMacOffloadFixture):
    def test_default_routing_retry_is_paced_fifteen_seconds(self) -> None:
        _ = self.routing_fixture()
        _ = self.environment.pop("BUILD_TARGETS_POLL_S")
        process = self.start_routing()
        self.assertEqual((self.runner_state / "sleep-events").read_text().splitlines(), ["15"])
        self.release_local_hold()
        _ = (self.runner_state / "continue-sleep").write_text("continue\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)

    def test_interrupted_routing_sleep_records_wait_and_cleans_pending_call(self) -> None:
        cycle = self.routing_fixture()
        process = self.start_routing()
        process.send_signal(signal.SIGTERM)
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 143, output + errors)
        self.assert_no_pending_calls(cycle)
        self.assertGreater(self.routing_record()["hold_wait_s"], 0)
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.last_call()["outcome"], "interrupted")
        self.assertEqual(list((self.root / "tmp").glob("verify-hold.*")), [])


if __name__ == "__main__":
    _ = unittest.main()
