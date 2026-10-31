"""Real verify.sh token ownership behavior."""
from __future__ import annotations

import json
import subprocess
from typing import cast
from scripts.direct.verify_token_wait_test_support import VERIFY, BOARD
from scripts.direct.verify_token_ownership_test_support import VerifyTokenOwnershipFixture, TEST_HANG_GUARD_SECS


class VerifyTokenOwnershipTests(VerifyTokenOwnershipFixture):
    def test_expired_live_holder_reclaim_keeps_its_build_running(self) -> None:
        board = self.root / "board"
        environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                       "UNIT_TEAM_ROLE": "first", "TEST_BLOCK": "1"}
        first = subprocess.Popen(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                                 env=environment, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.started_groups.add(first.pid)
        try:
            self.wait_for_running((self.root / "running"), first)
            self.assertTrue((self.root / "running").exists())
            lock = board / "locks" / "cargo.d"
            step_record = cast(dict[str, object], json.loads((lock / "owned").read_text()))
            step_group = cast(int, step_record["group"])
            self.started_groups.add(step_group)
            cargo_pid = int((self.root / "cargo-pid").read_text())
            self.assertIn(cargo_pid, self.live_group_processes(step_group))
            self.assertIsNone(first.poll(), "holder exited before its lock expired")

            _ = (lock / "expires").write_text("0")
            second = subprocess.run(["bash", str(BOARD), "acquire", str(board), "second",
                                     "cargo", "--wait", "0"], capture_output=True, text=True,
                                    check=False, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertIn("reclaimed from first", second.stdout)
            self.assertIn("after its hold expired", (board / "board.log").read_text())
            self.assertIsNone(first.poll(), "reclaim ended the live holder")
            self.assertIn(cargo_pid, self.live_group_processes(step_group),
                          "reclaim ended the live holder's cargo")

            reclaimed_release = subprocess.run(
                ["bash", str(BOARD), "release", str(board), "first", "cargo", "--pid", str(first.pid)],
                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(reclaimed_release.returncode, 3, reclaimed_release.stderr)
            self.assertIn("first does not hold cargo (holder is second)", reclaimed_release.stderr)
            wrong_process_release = subprocess.run(
                ["bash", str(BOARD), "release", str(board), "first", "cargo", "--pid", "1"],
                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(wrong_process_release.returncode, 1, wrong_process_release.stderr)
            unowned_release = subprocess.run(
                ["bash", str(BOARD), "release", str(board), "third", "cargo"],
                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(unowned_release.returncode, 1, unowned_release.stderr)

            _ = (self.root / "release").touch()
            _, first_error = first.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(first.returncode, 0, first_error)
            self.assertIn("first does not hold cargo (holder is second)", first_error)
            released = subprocess.run(["bash", str(BOARD), "release", str(board), "second",
                                       "cargo"], capture_output=True, text=True, check=False,
                                      timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(released.returncode, 0, released.stdout + released.stderr)
        finally:
            _ = (self.root / "release").touch()
            if first.poll() is None:
                self.end_group(first.pid)
            _ = first.wait(timeout=TEST_HANG_GUARD_SECS)
            if first.stderr is not None:
                first.stderr.close()
