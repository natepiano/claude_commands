"""Real verify.sh token release behavior."""
from __future__ import annotations

import os
import unittest
from scripts.direct.verify_token_wait_test_support import VerifyTokenWaitFixture


class VerifyTokenReleaseTests(VerifyTokenWaitFixture):
    def test_step_group_is_recorded_before_cargo_starts(self) -> None:
        board = self.root / "board"
        environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                       "UNIT_TEAM_ROLE": "first", "TEST_REQUIRE_OWNED": "1",
                       "TEST_OWNED_RECORD": str(board / "locks" / "cargo.d" / "owned"),
                       "TEST_OWNED_CONFIRMED": str(self.root / "owned-confirmed"),
                       "TEST_CALLER_GROUP": str(os.getpgrp())}
        result = self.verify(environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.root / "owned-confirmed").exists())

    def test_a_seat_with_only_the_names_before_the_rename_takes_its_run_token(self) -> None:
        board = self.root / "board"
        environment = {**self.environment, "PLAN_DELEGATE_BOARD_DIR": str(board),
                       "PLAN_DELEGATE_TEAM_ROLE": "first", "TEST_REQUIRE_OWNED": "1",
                       "TEST_OWNED_RECORD": str(board / "locks" / "cargo.d" / "owned"),
                       "TEST_OWNED_CONFIRMED": str(self.root / "owned-confirmed"),
                       "TEST_CALLER_GROUP": str(os.getpgrp())}
        result = self.verify(environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.root / "owned-confirmed").exists())

    def test_failed_release_reports_reason_and_fails_successful_step(self) -> None:
        board = self.root / "board"
        environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                       "UNIT_TEAM_ROLE": "first", "TEST_CORRUPT_OWNER": "1",
                       "TEST_OWNED_RECORD": str(board / "locks" / "cargo.d" / "owned"),
                       "TEST_STEP_GROUP": str(self.root / "step-group")}
        result = self.verify(environment)
        group = int((self.root / "step-group").read_text())
        self.started_groups.add(group)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("board.sh: invalid step ownership record", result.stderr)
        self.assertTrue((board / "locks" / "cargo.d").exists())
        self.assertEqual(self.live_group_processes(group), [])

    def test_failed_release_preserves_step_failure(self) -> None:
        board = self.root / "board"
        environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                       "UNIT_TEAM_ROLE": "first", "TEST_CORRUPT_OWNER": "1",
                       "TEST_CARGO_STATUS": "42",
                       "TEST_OWNED_RECORD": str(board / "locks" / "cargo.d" / "owned"),
                       "TEST_STEP_GROUP": str(self.root / "step-group")}
        result = self.verify(environment)
        group = int((self.root / "step-group").read_text())
        self.started_groups.add(group)
        self.assertEqual(result.returncode, 42, result.stdout + result.stderr)
        self.assertIn("board.sh: invalid step ownership record", result.stderr)
        self.assertEqual(self.live_group_processes(group), [])



if __name__ == "__main__":
    _ = unittest.main()
