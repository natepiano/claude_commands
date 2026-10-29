"""Real verify.sh Mac memory retry with disposable tools."""
from __future__ import annotations

import unittest
from pathlib import Path
from typing import final
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture, HANG_GUARD_S as HANG_GUARD_S


@final
class VerifyMacMemoryRetryTests(VerifyMacOffloadFixture):
    def release_memory(self, meminfo: Path) -> None:
        # A polling reader must not see the empty interval of a truncated file.
        ready = meminfo.with_suffix(".ready")
        _ = ready.write_text("MemAvailable: 67108864 kB\n", encoding="utf-8")
        _ = ready.replace(meminfo)

    def test_mac_busy_again_after_leaving_returns_to_the_local_wait(self) -> None:
        meminfo = self.hold_memory()
        process = self.start_verify("test", "sample", "--no-cache", extra={
            "TEST_RUNNER_BUSY_UNTIL": str(self.runner_state / "mac-free"), **self.leave_once()})
        self.addCleanup(self.stop_verify, process)
        # The Mac claim declined again; the local run waits with the hook still set.
        self.wait_for_path(self.runner_state / "free-now-after-leave", (process,))
        self.assertEqual(len(self.runner_events()), 2)
        self.assertEqual(self.local_runs(), [])
        self.release_memory(meminfo)
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(len(self.local_runs()), 1)
        self.assertEqual(len(self.runner_events()), 2)
        self.assertEqual(output.count("verify.sh: running on natedev"), 2)
        self.assertEqual(self.routing_record()["target"], "natedev")
        record = self.last_call()
        self.assertEqual((record["mac"], record["mac_reason"]), ("declined", "busy"))

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

    def test_permanent_mac_decline_has_no_leave_hook(self) -> None:
        meminfo = self.hold_memory()
        self.set_mac_answer("declined", "no_tests", 75)
        clock = self.control_routing_clock()
        self.environment["TEST_FREE_NOW_FREE"] = "1"
        process = self.start_verify("test", "sample", "--no-cache")
        self.addCleanup(self.stop_verify, process)
        self.wait_for_path(self.runner_state / "sleep-events", (process,))
        _ = clock.write_text("1000")
        self.release_memory(meminfo)
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(len(self.runner_events()), 1)
        self.assertEqual(self.free_now_settings(), [])
        self.assertEqual(len(self.local_runs()), 1)


if __name__ == "__main__":
    _ = unittest.main()
