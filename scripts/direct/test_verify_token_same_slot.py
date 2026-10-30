"""Real verify.sh token same slot behavior."""
from __future__ import annotations

import json
import subprocess
from typing import cast
from scripts.direct.verify_token_wait_test_support import VERIFY
from scripts.direct.verify_token_ownership_test_support import VerifyTokenOwnershipFixture, TEST_HANG_GUARD_SECS


class VerifyTokenSameSlotTests(VerifyTokenOwnershipFixture):
    def test_same_slot_reclaim_preserves_new_step_and_old_result(self) -> None:
        board = self.root / "board"
        lock = board / "locks" / "cargo.d"
        first_release = self.root / "first-release"
        second_release = self.root / "second-release"
        first_running = self.root / "first-running"
        second_running = self.root / "second-running"
        first_environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                             "UNIT_TEAM_ROLE": "holder", "TEST_BLOCK": "1",
                             "TEST_RELEASE": str(first_release),
                             "TEST_RUNNING": str(first_running),
                             "TEST_CARGO_PID": str(self.root / "first-cargo-pid")}
        second_environment = {**first_environment, "TEST_RELEASE": str(second_release),
                              "TEST_RUNNING": str(second_running),
                              "TEST_CARGO_PID": str(self.root / "second-cargo-pid")}
        first = subprocess.Popen(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                                 env=first_environment, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.started_groups.add(first.pid)
        second: subprocess.Popen[str] | None = None
        try:
            self.wait_for_running(first_running, first)
            self.assertTrue(first_running.exists(), "first cargo did not start")
            _ = (lock / "expires").write_text("0")
            second = subprocess.Popen(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                                      env=second_environment, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.PIPE, text=True, start_new_session=True)
            self.started_groups.add(second.pid)
            self.wait_for_waiter(board, second)
            self.assertEqual((lock / "holder_pid").read_text(), str(first.pid))
            self.assertFalse(second_running.exists(), "expired live step was reclaimed")
            # Reclaim remains supported when the owned identity no longer
            # describes the running group, even for two calls from one slot.
            old_record = cast(dict[str, object], json.loads((lock / "owned").read_text()))
            old_record["start"] = "not-the-running-group"
            _ = (lock / "owned").write_text(json.dumps(old_record))
            self.wait_for_running(second_running, second)
            self.assertTrue(second_running.exists(), "second cargo did not start")
            self.assertEqual((lock / "holder_pid").read_text(), str(second.pid))
            second_record = cast(dict[str, object], json.loads((lock / "owned").read_text()))
            second_group = cast(int, second_record["group"])
            self.started_groups.add(second_group)
            second_cargo_pid = int((self.root / "second-cargo-pid").read_text())

            first_release.touch()
            _, first_error = first.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(first.returncode, 0, first_error)
            self.assertEqual((lock / "holder_pid").read_text(), str(second.pid))
            self.assertIsNone(second.poll(), "old release ended the new verify")
            self.assertIn(second_cargo_pid, self.live_group_processes(second_group),
                          "old release ended the new cargo step")

            second_release.touch()
            _, second_error = second.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(second.returncode, 0, second_error)
        finally:
            first_release.touch()
            second_release.touch()
            if first.poll() is None:
                self.end_group(first.pid)
            _ = first.wait(timeout=TEST_HANG_GUARD_SECS)
            if first.stderr is not None:
                first.stderr.close()
            if second is not None:
                if second.poll() is None:
                    self.end_group(second.pid)
                _ = second.wait(timeout=TEST_HANG_GUARD_SECS)
                if second.stderr is not None:
                    second.stderr.close()
