"""Real verify entry point cancellation with private Cargo and Git fixtures."""
from __future__ import annotations

import unittest

from scripts.direct.verify_tree_cancel_test_support import VerifyTreeCancelFixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class VerifyTreeCancelTests(VerifyTreeCancelFixture):
    def test_mac_offload_tree_edit_uses_runner_cancel_path_without_local_rerun(self) -> None:
        process = self.start_verify("test", **self.mac_environment())
        self.wait_for_running(self.control / "running", process)
        if not (self.control / "mac-attempt").exists():
            self.end_group(process.pid)
            output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.fail("verify never used the offload runner: " + output + errors)
        self.edit_source()
        self.assert_cancelled(process, "test")
        self.assertEqual((self.control / "mac-cancelled").read_text(), "15")
        self.assertEqual([args for args in self.cargo_calls() if args[:2] == ["nextest", "run"]], [],
                         "Mac cancellation fell back to a local test run")

    def test_local_memory_wait_to_mac_preserves_existing_watcher(self) -> None:
        environment = self.mac_environment(local_first=True)
        low_memory = self.root / "low-memory"
        _ = low_memory.write_text("MemAvailable: 1024 kB\n")
        free_now = self.root / "free-now.py"
        self.write_tool(free_now, "raise SystemExit(0)\n")
        process = self.start_verify("test", **environment, BUILDLOG_MEMINFO=str(low_memory),
                                    BUILDLOG_MEM_POLL_S="1", VERIFY_MAC_TEST_SCRIPT=str(free_now),
                                    TEST_ROUTING_READY_EVENT=str(free_now))
        self.wait_for_running(self.control / "running", process)
        self.assertTrue((self.control / "mac-attempt").exists(), "memory wait never transferred to the Mac")
        self.edit_source()
        self.assert_cancelled(process, "test")
        self.assertEqual((self.control / "mac-cancelled").read_text(), "15")
        self.assertEqual([args for args in self.cargo_calls() if args[:2] == ["nextest", "run"]], [],
                         "memory-blocked local step reached Cargo")


if __name__ == "__main__":
    _ = unittest.main()
