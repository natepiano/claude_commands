"""Real verify.sh Mac routing through memory admission, with disposable tools."""
from __future__ import annotations

import unittest
from pathlib import Path
from typing import final
from scripts.direct.verify_mac_memory_retry_test_support import VerifyMacMemoryRetryFixture
from scripts.direct.verify_mac_offload_test_support import HANG_GUARD_S


@final
class VerifyMacMemoryExcludedTests(VerifyMacMemoryRetryFixture):
    def test_excluded_mac_is_never_tried_while_local_memory_waits(self) -> None:
        meminfo = self.hold_memory()
        settings = Path(self.environment["MAC_TEST_CONFIG"])
        _ = settings.write_text(f"offload=on\nlinux_only.{self.root.name}=sample\n")
        self.environment["TEST_FREE_NOW_FREE"] = "1"
        process = self.start_verify("test", "sample", "--no-cache")
        self.addCleanup(self.stop_verify, process)
        self.wait_for_path(self.runner_state / "sleep-events", (process,))
        self.release_memory(meminfo)
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(self.free_now_settings(), [])
        self.assertEqual(len(self.local_runs()), 1)


if __name__ == "__main__":
    _ = unittest.main()
