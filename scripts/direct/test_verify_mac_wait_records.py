"""Real verify.sh Mac wait records with disposable tools."""
from __future__ import annotations

import unittest
from typing import final
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture
from scripts.direct.verify_mac_offload_test_support import HANG_GUARD_S as HANG_GUARD_S


@final
class VerifyMacWaitRecordsTests(VerifyMacOffloadFixture):
    def test_hold_wait_includes_declined_retry_seconds_and_excludes_running_retry(self) -> None:
        _ = self.routing_fixture()
        clock = self.control_routing_clock()
        self.environment.update({"TEST_DECLINED_RETRY_SECONDS": "4.5", "TEST_RUNNER_SECONDS": "20",
                                 "TEST_FREE_NOW_FREE": "1"})
        process = self.start_routing(step_sleep=True)
        _ = clock.write_text("102")
        self.resume_routing_sleep(1)
        self.wait_for_path(self.runner_state / "routing-sleep-2")
        _ = clock.write_text("105")
        _ = (self.runner_state / "mac-free").write_text("free\n")
        _ = (self.runner_state / "continue-sleep-2").write_text("continue\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(len(self.runner_events()), 3)
        self.assertEqual(self.routing_record()["hold_wait_s"], 2 + 4.5 + 3)
        self.assertEqual(self.last_call()["mac_s"], 20)

    def test_released_hold_keeps_only_routing_wait_seconds(self) -> None:
        _ = self.routing_fixture()
        clock = self.control_routing_clock()
        process = self.start_routing()
        _ = clock.write_text("103")
        self.release_local_hold()
        _ = (self.runner_state / "continue-sleep").write_text("continue\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        record = self.routing_record()
        self.assertEqual(record["hold_wait_s"], 3)
        self.assertEqual(record["mem_wait_s"], "0")
        self.assertFalse((self.runner_state / "local-wait-added").exists())


if __name__ == "__main__":
    _ = unittest.main()
