"""Real verify.sh Mac memory retry clock with disposable tools."""
from __future__ import annotations

import unittest
from typing import final
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture


@final
class VerifyMacMemoryRetryClockTests(VerifyMacOffloadFixture):
    def test_lost_mac_run_retries_from_the_memory_wait(self) -> None:
        self.check_temporary_mac_decline("memory", "lost", "")


if __name__ == "__main__":
    _ = unittest.main()
