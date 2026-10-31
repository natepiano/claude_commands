"""Real verify.sh Mac holder history with disposable tools."""
from __future__ import annotations

import unittest
from scripts.direct.verify_mac_offload_test_support import RUNNER_STUB
from typing import final
from scripts.build_hold import build_hold
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture
from scripts.direct.verify_mac_offload_test_support import HANG_GUARD_S as HANG_GUARD_S


@final
class VerifyMacHolderHistoryTests(VerifyMacOffloadFixture):
    def test_busy_retry_to_blocked_or_malformed_answer_clears_call_holder(self) -> None:
        for mode, reason, expected in (("declined", "blocked", "blocked"), ("none", "", "runner")):
            fixture = VerifyMacOffloadFixture("runTest")
            try:
                fixture.setUp()
                _ = fixture.routing_fixture()
                process = fixture.start_routing(pause_retry=True)
                # start_routing sets a successful final answer; select this case's answer here.
                fixture.write_executable(fixture.runner, RUNNER_STUB.replace(
                    'mode = os.environ.get("TEST_RUNNER_MAC", "declined")', f'mode = "{mode}"').replace(
                    'os.environ.get("TEST_RUNNER_REASON", "")', f'"{reason}"'))
                _ = (fixture.runner_state / "mac-free").write_text("free\n")
                _ = (fixture.runner_state / "continue-sleep").write_text("continue\n")
                fixture.wait_for_path(fixture.runner_state / "retry-running")
                with fixture.hold_environment():
                    build_hold.holder_path(build_hold.holder_directory(), "hana").unlink()
                _ = (fixture.runner_state / "continue-retry").write_text("continue\n")
                _ = (fixture.runner_state / "continue-sleep-2").write_text("continue\n")
                output, errors = process.communicate(timeout=HANG_GUARD_S)
                self.assertEqual(process.returncode, 0, output + errors)
                self.assertEqual(fixture.last_call()["mac_reason"], expected)
                self.assertEqual(fixture.last_call()["mac_holder"], "")
            finally:
                fixture.doCleanups()


if __name__ == "__main__":
    _ = unittest.main()
