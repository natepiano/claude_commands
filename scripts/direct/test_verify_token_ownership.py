"""Real verify.sh token ownership behavior."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from unittest.mock import patch
import unittest
from pathlib import Path
from typing import cast
from scripts.direct.verify_token_wait_test_support import VerifyTokenWaitFixture, VERIFY, BOARD


class VerifyTokenOwnershipTests(VerifyTokenWaitFixture):
    def test_killed_verify_holder_is_reclaimed_promptly(self) -> None:
        sleep = self.root / "stubs" / "sleep"
        _ = sleep.write_text('''#!/bin/sh
if [ "$1" = 3 ]; then
    printf '%s\\n' "$1" >> "$TEST_RECLAIM_POLLS"
fi
exit 0
''')
        sleep.chmod(0o755)
        polls = self.root / "reclaim-polls"
        board = self.root / "board"
        first_environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                             "UNIT_TEAM_ROLE": "first", "TEST_BLOCK": "1"}
        second_environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                              "UNIT_TEAM_ROLE": "second", "TEST_RECLAIM_POLLS": str(polls)}
        first = subprocess.Popen(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                                 env=first_environment, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.started_groups.add(first.pid)
        try:
            self.wait_for_running((self.root / "running"), first)
            self.assertTrue((self.root / "running").exists(),
                            "first verify call did not reach cargo check")
            recorded_pid = (board / "locks" / "cargo.d" / "holder_pid").read_text()
            self.assertEqual(recorded_pid, str(first.pid))
            step_record = cast(dict[str, object], json.loads(
                (board / "locks" / "cargo.d" / "owned").read_text()))
            self.started_groups.add(cast(int, step_record["group"]))

            self.end_group(first.pid)
            _ = first.wait(timeout=300)
            second = self.verify(second_environment)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            records = self.records()
            self.assertEqual(len(records), 1)
            self.assertFalse(polls.exists(), "reclaimed holder must not require a token polling sleep")
            self.assertIn(f"token cargo reclaimed from first: holder pid {first.pid} is gone",
                          (board / "board.log").read_text())
        finally:
            _ = (self.root / "release").touch()
            if first.poll() is None:
                self.end_group(first.pid)
            _ = first.wait(timeout=300)
            if first.stdout is not None:
                first.stdout.close()
            if first.stderr is not None:
                first.stderr.close()

    def test_reclaim_stops_old_build_before_next_build(self) -> None:
        board = self.root / "board"
        first_environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                             "UNIT_TEAM_ROLE": "first", "TEST_BLOCK": "1"}
        second_environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                              "UNIT_TEAM_ROLE": "second", "TEST_RECLAIM_CHECK": "1"}
        first = subprocess.Popen(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                                 env=first_environment, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        self.started_groups.add(first.pid)
        try:
            self.wait_for_running((self.root / "running"), first)
            self.assertTrue((self.root / "running").exists())
            step_record = cast(dict[str, object], json.loads(
                (board / "locks" / "cargo.d" / "owned").read_text()))
            step_group = cast(int, step_record["group"])
            self.started_groups.add(step_group)
            self.assertNotEqual(step_group, first.pid)
            first.kill()
            _ = first.wait(timeout=300)
            second = self.verify(second_environment)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertEqual(self.live_group_processes(first.pid), [])
            self.assertEqual(self.live_group_processes(step_group), [])
        finally:
            self.end_group(first.pid)

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
                                    check=False, timeout=300)
            self.assertNotEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertNotIn("reclaimed from first", second.stdout)
            self.assertEqual((lock / "holder_pid").read_text(), str(first.pid))
            # A changed group identity removes the proof of a live step; expiry
            # can then be reclaimed without signalling an unrelated group.
            step_record["start"] = "not-the-running-group"
            _ = (lock / "owned").write_text(json.dumps(step_record))
            second = subprocess.run(["bash", str(BOARD), "acquire", str(board), "second",
                                     "cargo", "--wait", "0"], capture_output=True, text=True,
                                    check=False, timeout=300)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertIn("reclaimed from first", second.stdout)
            self.assertIn("after its hold expired", (board / "board.log").read_text())
            self.assertIsNone(first.poll(), "reclaim ended the live holder")
            self.assertIn(cargo_pid, self.live_group_processes(step_group),
                          "reclaim ended the live holder's cargo")

            reclaimed_release = subprocess.run(
                ["bash", str(BOARD), "release", str(board), "first", "cargo", "--pid", str(first.pid)],
                capture_output=True, text=True, check=False, timeout=300)
            self.assertEqual(reclaimed_release.returncode, 3, reclaimed_release.stderr)
            self.assertIn("first does not hold cargo (holder is second)", reclaimed_release.stderr)
            wrong_process_release = subprocess.run(
                ["bash", str(BOARD), "release", str(board), "first", "cargo", "--pid", "1"],
                capture_output=True, text=True, check=False, timeout=300)
            self.assertEqual(wrong_process_release.returncode, 1, wrong_process_release.stderr)
            unowned_release = subprocess.run(
                ["bash", str(BOARD), "release", str(board), "third", "cargo"],
                capture_output=True, text=True, check=False, timeout=300)
            self.assertEqual(unowned_release.returncode, 1, unowned_release.stderr)

            _ = (self.root / "release").touch()
            _, first_error = first.communicate(timeout=300)
            self.assertEqual(first.returncode, 0, first_error)
            self.assertIn("first does not hold cargo (holder is second)", first_error)
            released = subprocess.run(["bash", str(BOARD), "release", str(board), "second",
                                       "cargo"], capture_output=True, text=True, check=False,
                                      timeout=300)
            self.assertEqual(released.returncode, 0, released.stdout + released.stderr)
        finally:
            _ = (self.root / "release").touch()
            if first.poll() is None:
                self.end_group(first.pid)
            _ = first.wait(timeout=300)
            if first.stderr is not None:
                first.stderr.close()

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
            _, first_error = first.communicate(timeout=300)
            self.assertEqual(first.returncode, 0, first_error)
            self.assertEqual((lock / "holder_pid").read_text(), str(second.pid))
            self.assertIsNone(second.poll(), "old release ended the new verify")
            self.assertIn(second_cargo_pid, self.live_group_processes(second_group),
                          "old release ended the new cargo step")

            second_release.touch()
            _, second_error = second.communicate(timeout=300)
            self.assertEqual(second.returncode, 0, second_error)
        finally:
            first_release.touch()
            second_release.touch()
            if first.poll() is None:
                self.end_group(first.pid)
            _ = first.wait(timeout=300)
            if first.stderr is not None:
                first.stderr.close()
            if second is not None:
                if second.poll() is None:
                    self.end_group(second.pid)
                _ = second.wait(timeout=300)
                if second.stderr is not None:
                    second.stderr.close()

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
            self.assertNotEqual(child.wait(timeout=300), 0)
            self.assertEqual(self.live_group_processes(child.pid), [])
        finally:
            if child.poll() is None:
                child.kill()
            _ = child.wait(timeout=300)

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
            waiting = second

            def acquiring() -> bool:
                for pid in self.live_group_processes(waiting.pid):
                    try:
                        words = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                    except OSError:
                        continue
                    if b"acquire" in words and b"second" in words:
                        return True
                return waiting.poll() is not None

            deadline = time.monotonic() + 300
            while not acquiring() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsNone(second.poll(), "second verify exited before it waited for the token")

            # The signal goes to verify alone, as a caller's kill would.
            os.kill(second.pid, signal.SIGTERM)
            self.assertEqual(second.wait(timeout=10), -signal.SIGTERM)
            deadline = time.monotonic() + 10
            while self.live_group_processes(second.pid) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(self.live_group_processes(second.pid), [],
                             "the token wait outlived its verify")
            self.assertEqual((lock / "holder_pid").read_text(), str(first.pid))

            _ = (self.root / "release").touch()
            self.assertEqual(first.wait(timeout=300), 0)
            self.assertFalse(lock.exists())
            self.assertNotIn("[second] claim", (board / "board.log").read_text())
        finally:
            _ = (self.root / "release").touch()
            for process in (second, first):
                if process is not None:
                    if process.poll() is None:
                        self.end_group(process.pid)
                    _ = process.wait(timeout=300)

    def test_sigterm_stops_step_before_token_release(self) -> None:
        self.assert_interrupted_holder_stops_step_before_release(signal.SIGTERM)

    def test_sigint_stops_step_before_token_release(self) -> None:
        self.assert_interrupted_holder_stops_step_before_release(signal.SIGINT)



if __name__ == "__main__":
    _ = unittest.main()
