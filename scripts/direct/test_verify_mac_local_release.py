"""Real verify.sh Mac local release with disposable tools."""
from __future__ import annotations

import unittest
from pathlib import Path
from typing import final
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture
from scripts.direct.verify_mac_offload_test_support import HANG_GUARD_S as HANG_GUARD_S


@final
class VerifyMacLocalReleaseTests(VerifyMacOffloadFixture):
    def test_local_release_wins_before_another_mac_attempt(self) -> None:
        for released_by_command in (False, True):
            with self.subTest(released_by_command=released_by_command):
                # This test owns a separate fixture for each release variant.
                fixture = VerifyMacOffloadFixture("runTest")
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                cycle = fixture.routing_fixture()
                process = fixture.start_routing()
                if released_by_command:
                    fixture.release_local_hold()
                else:
                    (Path(fixture.environment["BUILD_HOLD_DIR"]) / "hana").unlink()
                _ = (fixture.runner_state / "continue-sleep").write_text("continue\n")
                output, errors = process.communicate(timeout=HANG_GUARD_S)
                self.assertEqual(process.returncode, 0, output + errors)
                self.assertEqual(len(fixture.runner_events()), 1)
                self.assertEqual(len(fixture.local_runs()), 1)
                self.assertFalse((Path(fixture.environment["BUILD_HOLD_DIR"]) / "hana").exists())
                fixture.assert_no_pending_calls(cycle)
                self.assertEqual(fixture.routing_record()["target"], "natedev")


if __name__ == "__main__":
    _ = unittest.main()
