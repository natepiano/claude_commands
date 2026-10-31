"""Real verify.sh Mac lifecycle with disposable tools."""
from __future__ import annotations

import unittest
import subprocess
from datetime import datetime
from pathlib import Path
from typing import final
from scripts.build_hold import build_hold
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture, HANG_GUARD_S as HANG_GUARD_S, VERIFY as VERIFY


@final
class VerifyMacLifecycleTests(VerifyMacOffloadFixture):
    def release_memory(self, meminfo: Path) -> None:
        # A polling reader must not see the empty interval of a truncated file.
        ready = meminfo.with_suffix(".ready")
        _ = ready.write_text("MemAvailable: 67108864 kB\n", encoding="utf-8")
        _ = ready.replace(meminfo)

    def test_queued_local_test_leaves_for_a_freed_mac_and_ends_with_its_failure(self) -> None:
        _ = self.hold_memory()
        mac_free = self.runner_state / "mac-free"
        process = self.start_verify("test", "sample", "--no-cache", extra={
            "TEST_RUNNER_BUSY_UNTIL": str(mac_free),
            "TEST_RUNNER_MAC": "failed", "TEST_RUNNER_STATUS": "42"})
        self.addCleanup(self.stop_verify, process)
        self.wait_for_path(self.runner_state / "free-now-events", (process,))
        self.assertEqual(len(self.runner_events()), 1)
        _ = mac_free.write_text("free\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 42, output + errors)
        self.assertIn("another machine is free after", output)
        self.assertEqual(self.local_runs(), [])
        runners = self.runner_events()
        self.assertEqual(len(runners), 2)
        # The step gave the token back for its memory wait and left for the Mac
        # without taking it again.
        self.assertFalse(runners[1]["token_held"])
        arguments = runners[1]["args"]
        self.assertEqual(arguments[arguments.index("--last-decline") + 1], "busy")
        self.assertEqual(set(self.free_now_settings()), {self.environment["MAC_TEST_CONFIG"]})
        self.assertEqual(set(self.free_now_roots()), {f"--repo-root {self.root}"})
        self.assertEqual(self.routing_record()["target"], "mac")
        record = self.last_call()
        self.assertEqual((record["status"], record["mac"], record["mac_reason"], record["mac_s"]),
                         (42, "failed", "", 3.25))

    def test_a_step_that_left_for_the_mac_ends_without_the_token_a_peer_now_holds(self) -> None:
        _ = self.hold_memory()
        mac_free = self.runner_state / "mac-free"
        process = self.start_verify("test", "sample", "--no-cache", extra={
            "TEST_RUNNER_BUSY_UNTIL": str(mac_free),
            "TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0"})
        self.addCleanup(self.stop_verify, process)
        self.wait_for_path(self.runner_state / "free-now-events", (process,))
        # The step gave the token back for its memory wait, and a peer takes it.
        lock = self.board / "locks" / "cargo.d"
        self.assertFalse(lock.exists())
        peer = subprocess.Popen(["bash", "-c", 'read -r -t "$1" _; :', "peer", str(HANG_GUARD_S)],
                                stdin=subprocess.PIPE)

        def stop_peer() -> None:
            assert peer.stdin is not None
            peer.stdin.close()
            _ = peer.wait(timeout=HANG_GUARD_S)

        self.addCleanup(stop_peer)
        acquired = subprocess.run(["bash", str(VERIFY.with_name("board.sh")), "acquire", str(self.board),
                                   "peer", "cargo", "--pid", str(peer.pid)],
                                  capture_output=True, text=True, check=False, timeout=HANG_GUARD_S)
        self.assertEqual(acquired.returncode, 0, acquired.stderr)
        _ = mac_free.write_text("free\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.last_call()["mac"], "passed_full")
        self.assertEqual((lock / "holder_pid").read_text(), str(peer.pid))

    def test_hold_that_lands_during_the_memory_wait_still_leaves_for_a_freed_mac(self) -> None:
        meminfo = self.hold_memory()
        mac_free = self.runner_state / "mac-free"
        process = self.start_verify("test", "sample", "--no-cache", extra={
            "TEST_RUNNER_BUSY_UNTIL": str(mac_free),
            "TEST_RUNNER_MAC": "failed", "TEST_RUNNER_STATUS": "42"})
        self.addCleanup(self.stop_verify, process)
        self.wait_for_path(self.runner_state / "free-now-events", (process,))
        with self.hold_environment():
            _ = build_hold.start_hold(Path(self.environment["BUILD_HOLD_DIR"]), build_hold.HolderIdentity("hana", "hana"),
                                      "render checks", build_hold.NoReleaseEta(), datetime.now().astimezone())
            cycle = build_hold.read_cycle()
            assert cycle is not None
        cycle_path = self.root / "release" / cycle["id"] / "cycle.json"

        def waiting_on_the_hold() -> bool:
            return any(Path(self.environment["TMPDIR"]).glob("verify-hold.*/waiter"))

        # Memory frees while natedev is held: the step moves to the hold wait,
        # where the Mac is still busy.
        self.release_memory(meminfo)
        self.wait_until(waiting_on_the_hold, "the hold wait", (process,))
        self.assertEqual(len(self.runner_events()), 1)
        _ = mac_free.write_text("free\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 42, output + errors)
        self.assertIn("leaving natedev's hold wait to run there", output + errors)
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(len(self.runner_events()), 2)
        self.assert_no_pending_calls(cycle_path)
        self.assertEqual(self.routing_record()["target"], "mac")
        self.assertEqual(self.last_call()["mac"], "failed")


if __name__ == "__main__":
    _ = unittest.main()
