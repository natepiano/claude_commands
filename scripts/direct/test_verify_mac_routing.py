"""Real verify.sh Mac routing with disposable tools."""
from __future__ import annotations

import unittest
from typing import final
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@final
class VerifyMacRoutingTests(VerifyMacOffloadFixture):
    def test_repeated_routing_wait_shows_each_unchanged_diagnostic_once(self) -> None:
        _ = self.routing_fixture()
        self.environment.update({"BUILD_TARGETS_ESCALATE_S": "0", "TEST_FREE_NOW_FREE": "1"})
        process = self.start_routing(step_sleep=True)
        for step in range(1, 4):
            self.resume_routing_sleep(step)
        _ = (self.runner_state / "mac-free").write_text("free\n")
        _ = (self.runner_state / "continue-sleep-4").write_text("continue\n")
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(errors.count("verify.sh: natedev is held by hana for render checks.\n"), 1)
        self.assertEqual(errors.count("verify.sh: natedev has been held for 2 h by hana; ask them.\n"), 1)
        self.assertEqual(errors.count("verify.sh: waiting for the Mac (busy) or natedev's release.\n"), 1)
        self.assertEqual(errors.count("claim diagnostic: busy\n"), 1)
        self.assertEqual(output.count("mac_test: not running on the Mac (busy)\n"), 1)
        self.assertNotIn("verify.sh: running on the Mac", output + errors)
        attempts = self.runner_events()
        self.assertEqual(len(attempts), 5)
        self.assertNotIn("--last-decline", attempts[0]["args"])
        for event in attempts[1:]:
            arguments = event["args"]
            self.assertEqual(arguments[arguments.index("--last-decline") + 1], "busy")


if __name__ == "__main__":
    _ = unittest.main()
