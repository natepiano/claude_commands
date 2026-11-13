"""Real verify.sh Mac release with disposable tools."""
from __future__ import annotations

import unittest
from typing import final
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@final
class VerifyMacReleaseTests(VerifyMacOffloadFixture):
    def test_busy_mac_and_held_local_wait_then_use_first_free_mac(self) -> None:
        cycle = self.routing_fixture()
        process = self.start_routing()
        _ = (self.runner_state / "mac-free").write_text("free\n")
        _ = (self.runner_state / "continue-sleep").write_text("continue\n")
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertIn("waiting", errors)
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(len(self.runner_events()), 2)

        self.assert_no_pending_calls(cycle)
        commands = [event["command"] for event in self.routing_events()]
        self.assertNotIn("finish-local", commands)
        registrations = [event for event in self.routing_events() if event["command"] == "register-local"]
        self.assertTrue(registrations)
        self.assertTrue(all(event["pid"] == process.pid for event in registrations))
        self.assertEqual(self.routing_record()["target"], "mac")
        self.assertGreater(self.routing_record()["hold_wait_s"], 0)
        self.assertEqual(self.last_call()["mac_holder"], "")

    def test_blocked_mac_and_held_local_wait_for_local_release(self) -> None:
        cycle = self.routing_fixture()
        # Observe the placement poll after the first remote refusal.
        process = self.start_routing("blocked")
        self.release_local_hold()
        _ = (self.runner_state / "continue-sleep").write_text("continue\n")
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertIn("blocked", errors)
        self.assertEqual(len(self.runner_events()), 1)
        self.assertEqual(len(self.local_runs()), 1)
        self.assert_no_pending_calls(cycle)


if __name__ == "__main__":
    _ = unittest.main()
