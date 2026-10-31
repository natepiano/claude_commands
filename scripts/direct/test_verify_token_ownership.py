"""Real verify.sh token ownership behavior."""
from __future__ import annotations

import json
from contextlib import contextmanager
from collections.abc import Iterator
import os
from pathlib import Path
import signal
import shlex
import subprocess
import sys
from typing import cast
from unittest.mock import patch
from scripts.direct import step_record as steps
import process_start
from scripts.direct.wait_for_test_state import wait_until
from scripts.direct.verify_token_wait_test_support import VERIFY, BOARD
from scripts.direct.verify_token_ownership_test_support import VerifyTokenOwnershipFixture, TEST_HANG_GUARD_SECS


class VerifyTokenOwnershipTests(VerifyTokenOwnershipFixture):
    def expired_owner(self, group: int, start: str) -> Path:
        board = self.root / "board"
        result = subprocess.run(["bash", str(BOARD), "acquire", str(board), "first", "cargo",
                                 "--pid", str(os.getpid())], env=self.environment,
                                capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lock = board / "locks/cargo.d"
        _ = (lock / "owned").write_text(json.dumps({"group": group, "start": start}))
        _ = (lock / "expires").write_text("0")
        return lock

    @contextmanager
    def legacy_darwin_owner(self) -> Iterator[tuple[subprocess.Popen[bytes], Path, dict[str, str]]]:
        leader = subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()"], start_new_session=True)
        self.started_groups.add(leader.pid)
        legacy = "Fri Oct 30 08:34:56 2026"
        lock = self.expired_owner(leader.pid, legacy)
        # Only board's inline Python runs as Darwin; ps still observes a real group.
        python = self.root / "stubs/python3"
        python.unlink()
        _ = python.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable)
                              + " -c 'import sys; sys.platform=\"darwin\"; sys.argv=sys.argv[1:]; exec(sys.stdin.read())' \"$@\"\n")
        python.chmod(0o755)
        ps = self.root / "stubs/ps"
        real_ps = ps.resolve()
        ps.unlink()
        _ = ps.write_text("#!/bin/sh\n"
                          + 'if [ "$1" = -p ] && [ "$4" = stat= ]; then\n'
                          + '  if [ "${TZ:-}" = UTC ]; then printf "S Fri Oct 30 12:34:56 2026\\n";\n'
                          + '  else printf "S ' + legacy + '\\n"; fi\n'
                          + 'else exec ' + shlex.quote(str(real_ps)) + ' "$@"; fi\n')
        ps.chmod(0o755)
        environment = {**self.environment, "TZ": "America/New_York"}
        try:
            yield leader, lock, environment
        finally:
            self.end_group(leader.pid)
            _ = leader.wait(timeout=TEST_HANG_GUARD_SECS)

    def test_legacy_darwin_owner_keeps_an_expired_hold(self) -> None:
        with self.legacy_darwin_owner() as (leader, lock, environment):
            refused = subprocess.run(["bash", str(BOARD), "acquire", str(self.root / "board"), "second", "cargo"],
                                     env=environment, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
            self.assertEqual((lock / "holder").read_text(), "first")
            self.assertIsNone(leader.poll())

    def test_legacy_darwin_owner_cleanup_ends_its_group(self) -> None:
        with self.legacy_darwin_owner() as (leader, lock, environment):
            released = subprocess.run(["bash", str(BOARD), "release", str(self.root / "board"), "first", "cargo",
                                       "--pid", str(os.getpid())], env=environment,
                                      capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(released.returncode, 0, released.stdout + released.stderr)
            self.assertEqual(self.live_group_processes(leader.pid), [])
            self.assertFalse(lock.exists())

    def test_unreadable_owner_start_keeps_an_expired_hold(self) -> None:
        lock = self.expired_owner(42, "123")
        proc = self.root / "proc"
        # A directory in place of stat is a read error, even for a privileged runner.
        (proc / "42/stat").mkdir(parents=True)
        result = subprocess.run(["bash", str(BOARD), "acquire", str(self.root / "board"), "second", "cargo"],
                                env={**self.environment, "VERIFY_PROC_ROOT": str(proc)},
                                capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((lock / "holder").read_text(), "first")
        self.assertTrue((lock / "owned").exists())

    def test_reused_owner_pid_frees_an_expired_hold(self) -> None:
        lock = self.expired_owner(42, "123")
        stat = self.root / "proc/42/stat"
        stat.parent.mkdir(parents=True)
        _ = stat.write_text("42 (new owner) S " + "0 " * 18 + "456 0\n")
        result = subprocess.run(["bash", str(BOARD), "acquire", str(self.root / "board"), "second", "cargo"],
                                env={**self.environment, "VERIFY_PROC_ROOT": str(stat.parents[1])},
                                capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("reclaimed from first", result.stdout)
        self.assertEqual((lock / "holder").read_text(), "second")

    def test_zombie_leader_is_stale_but_cleanup_still_ends_its_live_children(self) -> None:
        child_file = self.root / "live-child"
        leader = subprocess.Popen([sys.executable, "-c", '''import os, signal, sys
from pathlib import Path
if os.fork():
    os._exit(0)
Path(sys.argv[1]).write_text(str(os.getpid()))
signal.pause()
''', str(child_file)], start_new_session=True)
        self.started_groups.add(leader.pid)
        try:
            wait_until(lambda: child_file.exists() and bool(child_file.read_text())
                       and isinstance(process_start.read_process_start(leader.pid), process_start.Zombie),
                       self.root, timeout=TEST_HANG_GUARD_SECS, processes=(leader,))
            identity = process_start.read_process_start(leader.pid)
            if not isinstance(identity, process_start.Zombie):
                self.fail("leader did not become a zombie")
            child = int(child_file.read_text())
            self.assertIn(child, self.live_group_processes(leader.pid))
            lock = self.expired_owner(leader.pid, identity.start)
            with patch.dict(os.environ, {"VERIFY_STEPS_DIR": str(self.root / "steps")}):
                steps.write(steps.StepRecord("zombie", leader.pid, identity.start, self.root, "check", None,
                                             steps.MacAllowed(), steps.StepState.ON_NATEDEV, "now"))
                self.assertEqual(steps.read("zombie"), steps.StaleRecord("zombie"))
            result = subprocess.run(["bash", str(BOARD), "release", str(self.root / "board"), "first", "cargo",
                                     "--pid", str(os.getpid())], env=self.environment,
                                    capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(self.live_group_processes(leader.pid), [])
            self.assertFalse(lock.exists())
        finally:
            try:
                os.killpg(leader.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _ = leader.wait(timeout=TEST_HANG_GUARD_SECS)

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
            self.assertNotEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertNotIn("reclaimed from first", second.stdout)
            self.assertEqual((lock / "holder_pid").read_text(), str(first.pid))
            # A changed group identity removes the proof of a live step; expiry
            # can then be reclaimed without signalling an unrelated group.
            step_record["start"] = "not-the-running-group"
            _ = (lock / "owned").write_text(json.dumps(step_record))
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
