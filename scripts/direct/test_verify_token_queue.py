"""Real verify.sh token queue behavior."""
from __future__ import annotations

import unittest
from typing import cast
from scripts.direct.verify_token_wait_test_support import VerifyTokenWaitFixture, TEST_HANG_GUARD_SECS


class VerifyTokenQueueTests(VerifyTokenWaitFixture):
    def test_a_newer_call_supersedes_an_older_call_still_waiting_for_the_token(self) -> None:
        board = self.root / "board"
        shared = {**self.environment, "UNIT_RUN_DIR": str(board)}
        holder = self.start_check({**shared, "UNIT_TEAM_ROLE": "peer", "TEST_BLOCK": "1"})
        try:
            self.wait_for_running(self.root / "running", holder)
            older = self.start_check({**shared, "UNIT_TEAM_ROLE": "impl"})
            self.wait_for_waiter(board, older)
            newer = self.start_check({**shared, "UNIT_TEAM_ROLE": "impl"})
            older_output, older_errors = older.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(older.returncode, 76, older_output + older_errors)
            self.assertIn("superseded by a newer check call", older_output)
            self.assertIn("nothing needs a repair", older_output)
            self.assertIsNone(newer.poll(), "the newer call must still wait for the peer's token")
            _ = (self.root / "release").touch()
            newer_output = newer.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(newer.returncode, 0, newer_output)
            holder_output = holder.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(holder.returncode, 0, holder_output)
        finally:
            _ = (self.root / "release").touch()
        superseded = [record for record in self.records() if f"-{older.pid}-" in cast(str, record["id"])]
        self.assertEqual([(record["outcome"], record["status"]) for record in superseded], [("superseded", 76)])
        self.assertEqual(list((board / "token_waiters").iterdir()), [])

    def test_a_call_that_holds_the_token_runs_on_when_a_newer_call_starts(self) -> None:
        board = self.root / "board"
        shared = {**self.environment, "UNIT_RUN_DIR": str(board), "UNIT_TEAM_ROLE": "impl"}
        holder = self.start_check({**shared, "TEST_BLOCK": "1"})
        try:
            self.wait_for_running(self.root / "running", holder)
            newer = self.start_check(shared)
            self.wait_for_waiter(board, newer)
            self.assertIsNone(holder.poll(), "the call that holds the token must run on")
            _ = (self.root / "release").touch()
            holder_output = holder.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(holder.returncode, 0, holder_output)
            newer_output = newer.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(newer.returncode, 0, newer_output)
        finally:
            _ = (self.root / "release").touch()
        self.assertEqual(sorted(cast(str, record["outcome"]) for record in self.records()), ["ran", "ran"])



if __name__ == "__main__":
    _ = unittest.main()
