"""Real verify.sh Mac retry hold with disposable tools."""
from __future__ import annotations

import unittest
from typing import final
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture, VERIFY as VERIFY
from scripts.direct.verify_mac_offload_test_support import TARGETS_WRAPPER


@final
class VerifyMacRetryHoldTests(VerifyMacOffloadFixture):
    def test_runner_decline_retries_from_the_routing_wait(self) -> None:
        self.check_temporary_mac_decline("routing", "none", "")

    def test_lost_mac_run_retries_from_a_later_hold_wait(self) -> None:
        wrapper = self.root / "targets-wrapper.py"
        self.write_executable(wrapper, TARGETS_WRAPPER)
        self.environment.update({"BUILD_TARGETS_SCRIPT": str(wrapper),
            "TEST_REAL_TARGETS": str(VERIFY.parent.parent / "build_targets" / "build_targets.py"),
            "BUILD_TARGETS_POLL_S": "0.05"})
        self.check_temporary_mac_decline("hold", "lost", "", repeat_decline=False)

    def test_lost_mac_run_retries_when_a_hold_starts_after_routing(self) -> None:
        self.check_temporary_mac_decline("first_hold", "lost", "", repeat_decline=False)


if __name__ == "__main__":
    _ = unittest.main()
