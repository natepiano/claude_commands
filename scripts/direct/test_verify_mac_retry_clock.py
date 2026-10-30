"""Real verify.sh Mac retry clock with disposable tools."""
from __future__ import annotations

import unittest
from typing import final
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture


@final
class VerifyMacRetryClockTests(VerifyMacOffloadFixture):
    def test_idle_marker_bypasses_memory_retry_spacing(self) -> None:
        self.check_idle_marker("memory")

    def test_idle_marker_bypasses_routing_retry_spacing(self) -> None:
        self.check_idle_marker("routing")

    def test_idle_marker_bypasses_token_retry_spacing(self) -> None:
        self.check_idle_marker("token")


if __name__ == "__main__":
    _ = unittest.main()
