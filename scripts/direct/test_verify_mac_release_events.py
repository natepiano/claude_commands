"""Real verify.sh Mac release events with disposable tools."""
from __future__ import annotations

import unittest
import os
from pathlib import Path
from typing import final
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture
import ctypes
import time
from unittest import mock
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@final
class VerifyMacReleaseEventsTests(VerifyMacOffloadFixture):
    def test_local_release_before_routing_watcher_starts_needs_no_retry_wait(self) -> None:
        from scripts.direct import local_release_wait

        cycle = self.routing_fixture()
        call_id = "already-released"
        with mock.patch.dict(os.environ, self.environment), self.hold_environment():
            self.release_local_hold()
            with mock.patch.object(local_release_wait.LocalReleaseEvents, "wait",
                                   side_effect=AssertionError("an admitted call must not wait for another event")):
                local_release_wait.wait_for_local_release(3600, call_id)
        self.assert_no_pending_calls(cycle)

    def test_routing_without_inotify_polls_until_local_release(self) -> None:
        from scripts.direct import local_release_wait

        cycle = self.routing_fixture()
        call_id = "polling-routing"
        def release_on_poll(_seconds: float) -> None:
            self.release_local_hold()

        with mock.patch.dict(os.environ, self.environment), self.hold_environment():
            with mock.patch.object(ctypes, "CDLL", return_value=object()), \
                 mock.patch.object(time, "monotonic", return_value=100), \
                 mock.patch.object(time, "sleep", side_effect=release_on_poll) as polling:
                local_release_wait.wait_for_local_release(15, call_id)
            polling.assert_called_once_with(15)
            self.assertFalse((Path(self.environment["BUILD_HOLD_DIR"]) / "hana").exists())
        self.assert_no_pending_calls(cycle)

    def test_free_mac_wins_when_local_hold_was_released_in_the_same_pass(self) -> None:
        cycle = self.routing_fixture()
        process = self.start_routing()
        self.release_local_hold()
        _ = (self.runner_state / "mac-free").write_text("free\n")
        _ = (self.runner_state / "continue-sleep").write_text("continue\n")
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(len(self.runner_events()), 2)

        self.assert_no_pending_calls(cycle)
        self.assertEqual(self.routing_record()["target"], "mac")
        self.assertEqual(self.last_call()["mac"], "passed_filter")


if __name__ == "__main__":
    _ = unittest.main()
