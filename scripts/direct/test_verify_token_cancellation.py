"""Real verify.sh token cancellation behavior."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from unittest.mock import patch
from scripts.direct.verify_token_wait_test_support import VERIFY
from scripts.direct.verify_token_ownership_test_support import VerifyTokenOwnershipFixture, TEST_HANG_GUARD_SECS
from scripts.direct.wait_for_test_state import wait_until


class VerifyTokenCancellationTests(VerifyTokenOwnershipFixture):
    def test_group_cleanup_ends_without_pidfd_support(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()"],
                                 start_new_session=True)
        try:
            # Pin the race where a killed member is still visible in the first
            # snapshot, so portable cleanup must take its polling branch.
            with patch.object(os, "pidfd_open", side_effect=AttributeError("pidfd_open unavailable"),
                              create=True), patch.object(self, "live_group_processes",
                                                         side_effect=[[child.pid], []]):
                self.end_group(child.pid)
            self.assertNotEqual(child.wait(timeout=TEST_HANG_GUARD_SECS), 0)
            self.assertEqual(self.live_group_processes(child.pid), [])
        finally:
            if child.poll() is None:
                child.kill()
            _ = child.wait(timeout=TEST_HANG_GUARD_SECS)

    def test_sigterm_during_the_token_wait_exits_at_once_and_leaves_no_board_entry(self) -> None:
        board = self.root / "board"
        lock = board / "locks" / "cargo.d"
        first = subprocess.Popen(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                                 env={**self.environment, "UNIT_RUN_DIR": str(board),
                                      "UNIT_TEAM_ROLE": "first", "TEST_BLOCK": "1"},
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        self.started_groups.add(first.pid)
        second: subprocess.Popen[bytes] | None = None
        try:
            self.wait_for_running(self.root / "running", first)
            second = subprocess.Popen(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                                      env={**self.environment, "UNIT_RUN_DIR": str(board),
                                           "UNIT_TEAM_ROLE": "second"},
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      start_new_session=True)
            self.started_groups.add(second.pid)
            acquiring = self.root / "second-acquiring"
            wait_until(acquiring.exists, self.root, TEST_HANG_GUARD_SECS, processes=(second,))
            self.assertIsNone(second.poll(), "second verify exited before it waited for the token")

            # The signal goes to verify alone, as a caller's kill would.
            os.kill(second.pid, signal.SIGTERM)
            self.assertEqual(second.wait(timeout=10), -signal.SIGTERM)
            self.wait_for_group_exit(second.pid)
            self.assertEqual(self.live_group_processes(second.pid), [],
                             "the token wait outlived its verify")
            self.assertEqual((lock / "holder_pid").read_text(), str(first.pid))

            _ = (self.root / "release").touch()
            self.assertEqual(first.wait(timeout=TEST_HANG_GUARD_SECS), 0)
            self.assertFalse(lock.exists())
            self.assertNotIn("[second] claim", (board / "board.log").read_text())
        finally:
            _ = (self.root / "release").touch()
            for process in (second, first):
                if process is not None:
                    if process.poll() is None:
                        self.end_group(process.pid)
                    _ = process.wait(timeout=TEST_HANG_GUARD_SECS)
