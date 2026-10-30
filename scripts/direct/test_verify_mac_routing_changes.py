"""Real verify.sh Mac routing changes with disposable tools."""
from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from typing import final
from scripts.build_hold import build_hold
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@final
class VerifyMacRoutingChangesTests(VerifyMacOffloadFixture):
    def test_routing_wait_shows_changed_holder_and_decline_reason_once(self) -> None:
        _ = self.routing_fixture()
        self.environment["TEST_FREE_NOW_FREE"] = "1"
        process = self.start_routing(step_sleep=True)
        with self.hold_environment():
            holders = Path(self.environment["BUILD_HOLD_DIR"])
            _ = build_hold.start_hold(holders, build_hold.HolderIdentity("julian", "julian"), "restore CI", build_hold.NoReleaseEta(), datetime.now().astimezone())
            (holders / "hana").unlink()
        self.resume_routing_sleep(1)
        self.resume_routing_sleep(2)
        _ = (self.runner_state / "busy-reason").write_text("blocked\n")
        self.resume_routing_sleep(3)
        self.resume_routing_sleep(4)
        _ = (self.runner_state / "mac-free").write_text("free\n")
        _ = (self.runner_state / "continue-sleep-5").write_text("continue\n")
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        for line in ("verify.sh: natedev is held by hana for render checks.\n",
                     "verify.sh: natedev is held by julian for restore CI.\n",
                     "verify.sh: waiting for the Mac (busy) or natedev's release.\n",
                     "verify.sh: waiting for the Mac (blocked) or natedev's release.\n"):
            self.assertEqual(errors.count(line), 1, output + errors)
        self.assertEqual(output.count("mac_test: not running on the Mac (busy)\n"), 1)
        self.assertEqual(output.count("mac_test: not running on the Mac (blocked)\n"), 1)
        attempts = self.runner_events()
        self.assertEqual([event["args"][event["args"].index("--last-decline") + 1] for event in attempts[1:]],
                         ["busy", "busy", "busy", "blocked", "blocked"])


if __name__ == "__main__":
    _ = unittest.main()
