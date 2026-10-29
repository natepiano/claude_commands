#!/usr/bin/env python3
"""Cargo token recovery keeps live holders and admits one replacement."""

from __future__ import annotations

import os
import re
import select
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import cast, override


# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

BOARD = Path(__file__).with_name("board.sh")


class BoardReclaimTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    lock: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.lock = self.root / "locks" / "cargo.d"

    @override
    def tearDown(self) -> None:
        self.temporary.cleanup()

    def board(self, command: str, *arguments: str, epoch: int = 1000) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(BOARD), command, str(self.root), *arguments],
            env={**os.environ, "UNIT_NOW_EPOCH": str(epoch)},
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
        )

    def holder_process(self) -> subprocess.Popen[bytes]:
        holder = subprocess.Popen(["sleep", "600"])
        self.addCleanup(self.stop_holder, holder)
        return holder

    def guard_process(self) -> subprocess.Popen[str]:
        guard = self.root / "locks" / "cargo.guard"
        code = "\n".join((
            "import fcntl, sys, time",
            "with open(sys.argv[1], 'a') as lock:",
            "    fcntl.flock(lock, fcntl.LOCK_EX)",
            "    print('locked', flush=True)",
            "    time.sleep(600)",
        ))
        helper = subprocess.Popen(
            ["python3", "-u", "-c", code, str(guard)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        self.addCleanup(self.stop_guard, helper)
        self.assertIsNotNone(helper.stdout)
        if helper.stdout is None:
            self.fail("guard helper has no stdout")
        ready, _, _ = select.select([helper.stdout], [], [], TEST_HANG_GUARD_SECS)
        self.assertTrue(ready, "guard helper did not start")
        self.assertEqual(cast(str, helper.stdout.readline()).strip(), "locked")
        return helper

    @staticmethod
    def stop_holder(holder: subprocess.Popen[bytes]) -> None:
        if holder.poll() is None:
            holder.kill()
        _ = holder.wait(timeout=TEST_HANG_GUARD_SECS)

    @staticmethod
    def stop_guard(helper: subprocess.Popen[str]) -> None:
        if helper.poll() is None:
            helper.kill()
        _ = helper.wait(timeout=TEST_HANG_GUARD_SECS)
        if helper.stdout is not None:
            helper.stdout.close()

    def assert_guard_file(self) -> None:
        self.assertFalse((self.root / "locks" / "cargo.reclaim").exists())
        guard = self.root / "locks" / "cargo.guard"
        if guard.exists():
            self.assertTrue(guard.is_file())

    def assert_acquired(self, result: subprocess.CompletedProcess[str], output: str) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), output)

    def assert_busy(self, result: subprocess.CompletedProcess[str], holder: str = "holder") -> None:
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"busy cargo held by {holder}", result.stderr)

    def test_reaps_gone_holder_without_waiting(self) -> None:
        holder = self.holder_process()
        self.assert_acquired(self.board("acquire", "holder", "cargo", "--pid", str(holder.pid)),
                             "acquired cargo")
        holder.kill()
        _ = holder.wait(timeout=TEST_HANG_GUARD_SECS)

        self.assert_acquired(self.board("acquire", "next", "cargo", "--wait", "0"),
                             "acquired cargo (reclaimed from holder)")
        self.assertIn(f"token cargo reclaimed from holder: holder pid {holder.pid} is gone",
                      (self.root / "board.log").read_text())
        self.assert_guard_file()

    def test_a_waiter_whose_pid_ends_stops_waiting(self) -> None:
        holder = self.holder_process()
        self.assert_acquired(self.board("acquire", "holder", "cargo", "--pid", str(holder.pid)),
                             "acquired cargo")
        run = self.holder_process()
        waiter = subprocess.Popen(
            ["bash", str(BOARD), "acquire", str(self.root), "next", "cargo",
             "--pid", str(run.pid), "--wait", "600"],
            env={**os.environ, "UNIT_NOW_EPOCH": "1000"},
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        run.kill()
        _ = run.wait(timeout=TEST_HANG_GUARD_SECS)

        _, errors = waiter.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(waiter.returncode, 4, errors)
        self.assertIn(f"gone cargo: pid {run.pid} ended while it waited", errors)
        self.assertEqual((self.lock / "holder_pid").read_text(), str(holder.pid))

    def test_reclaims_zombie_holder(self) -> None:
        holder = self.holder_process()
        self.assert_acquired(self.board("acquire", "holder", "cargo", "--pid", str(holder.pid)),
                             "acquired cargo")
        holder.kill()
        deadline = time.monotonic() + TEST_HANG_GUARD_SECS
        state = ""
        while time.monotonic() < deadline:
            state = subprocess.run(["ps", "-p", str(holder.pid), "-o", "stat="],
                                   capture_output=True, text=True, check=False).stdout.strip()
            if state.startswith("Z"):
                break
            time.sleep(0.01)
        self.assertTrue(state.startswith("Z"), f"holder state was {state!r}")
        self.assert_acquired(self.board("acquire", "next", "cargo", "--wait", "0"),
                             "acquired cargo (reclaimed from holder)")

    def test_gone_holder_reason_precedes_expiry(self) -> None:
        holder = self.holder_process()
        self.assert_acquired(self.board("acquire", "holder", "cargo", "--pid", str(holder.pid),
                                        "--hold", "10"), "acquired cargo")
        holder.kill()
        _ = holder.wait(timeout=TEST_HANG_GUARD_SECS)

        self.assert_acquired(self.board("acquire", "next", "cargo", "--wait", "0", epoch=1011),
                             "acquired cargo (reclaimed from holder)")
        log = (self.root / "board.log").read_text()
        self.assertIn(f"token cargo reclaimed from holder: holder pid {holder.pid} is gone", log)
        self.assertNotIn("after its hold expired", log)

    def test_live_holder_stays_held(self) -> None:
        holder = self.holder_process()
        self.assert_acquired(self.board("acquire", "holder", "cargo", "--pid", str(holder.pid)),
                             "acquired cargo")
        original = {name: (self.lock / name).read_text()
                    for name in ("holder", "expires", "holder_pid")}

        self.assert_busy(self.board("acquire", "next", "cargo", "--wait", "0"))
        self.assertEqual(original, {name: (self.lock / name).read_text() for name in original})

    def test_without_pid_waits_for_expiry(self) -> None:
        self.assert_acquired(self.board("acquire", "holder", "cargo", "--hold", "10"),
                             "acquired cargo")
        self.assertFalse((self.lock / "holder_pid").exists())
        self.assertFalse((self.lock / "pid").exists())
        self.assert_busy(self.board("acquire", "next", "cargo", "--wait", "0"))

        self.assert_acquired(self.board("acquire", "next", "cargo", "--wait", "0", epoch=1011),
                             "acquired cargo (reclaimed from holder)")
        self.assertIn("token cargo reclaimed from holder after its hold expired",
                      (self.root / "board.log").read_text())

    def test_old_pid_file_does_not_trigger_reclaim(self) -> None:
        holder = self.holder_process()
        holder.kill()
        _ = holder.wait(timeout=TEST_HANG_GUARD_SECS)
        self.lock.mkdir(parents=True)
        _ = (self.lock / "holder").write_text("holder")
        _ = (self.lock / "expires").write_text("4600")
        _ = (self.lock / "pid").write_text(str(holder.pid))

        self.assert_busy(self.board("acquire", "next", "cargo", "--wait", "0"))

    def test_invalid_holder_pid_is_rejected(self) -> None:
        for pid in ("0", "-3", "abc"):
            with self.subTest(pid=pid):
                result = self.board("acquire", "holder", "cargo", "--pid", pid)
                self.assertEqual(result.returncode, 2)
                self.assertIn("--pid must be a positive integer", result.stderr)
                self.assertFalse(self.lock.exists())

    def test_malformed_recorded_pid_waits_for_expiry(self) -> None:
        self.lock.mkdir(parents=True)
        _ = (self.lock / "holder").write_text("holder")
        _ = (self.lock / "expires").write_text("4600")
        _ = (self.lock / "holder_pid").write_text("not-a-pid")

        self.assert_busy(self.board("acquire", "next", "cargo", "--wait", "0"))
        self.assert_acquired(self.board("acquire", "next", "cargo", "--wait", "0", epoch=4601),
                             "acquired cargo (reclaimed from holder)")

    def test_concurrent_reclaim_has_one_winner(self) -> None:
        holder = self.holder_process()
        self.assert_acquired(self.board("acquire", "holder", "cargo", "--pid", str(holder.pid)),
                             "acquired cargo")
        holder.kill()
        _ = holder.wait(timeout=TEST_HANG_GUARD_SECS)
        start = Barrier(8)

        def contender(index: int) -> subprocess.CompletedProcess[str]:
            _ = start.wait(timeout=TEST_HANG_GUARD_SECS)
            return self.board("acquire", f"next{index}", "cargo", "--wait", "0")

        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(workers.map(contender, range(8)))
        self.assertEqual([result.returncode for result in results].count(0), 1, results)
        claims = (self.root / "board.log").read_text().splitlines()
        self.assertEqual(sum(bool(re.search(
            r"\[next[0-7]\] claim: token cargo (?:acquired|reclaimed)", line,
        )) for line in claims), 1)
        self.assert_guard_file()

    def test_live_guard_blocks_reclaim_until_holder_exits(self) -> None:
        holder = self.holder_process()
        self.assert_acquired(self.board("acquire", "holder", "cargo", "--pid", str(holder.pid)),
                             "acquired cargo")
        holder.kill()
        _ = holder.wait(timeout=TEST_HANG_GUARD_SECS)
        original = {name: (self.lock / name).read_text()
                    for name in ("holder", "expires", "holder_pid")}
        guard_holder = self.guard_process()

        self.assert_busy(self.board("acquire", "next", "cargo", "--wait", "0"))
        self.assertEqual(original, {name: (self.lock / name).read_text() for name in original})
        self.assert_guard_file()
        self.stop_guard(guard_holder)
        self.assert_acquired(self.board("acquire", "next", "cargo", "--wait", "0"),
                             "acquired cargo (reclaimed from holder)")
        self.assertIn(f"token cargo reclaimed from holder: holder pid {holder.pid} is gone",
                      (self.root / "board.log").read_text())
        self.assert_guard_file()

    def test_release_waits_for_guard_before_removing_lock(self) -> None:
        self.assert_acquired(self.board("acquire", "holder", "cargo"), "acquired cargo")
        guard_holder = self.guard_process()
        # Observe the retry after a real failed lock attempt, then hold that
        # retry until the parent has checked the lock and released the guard.
        retry_read, retry_write = os.pipe()
        continue_read, continue_write = os.pipe()
        for descriptor in (retry_read, retry_write, continue_read, continue_write):
            self.addCleanup(os.close, descriptor)
        tools = self.root / "bin"
        tools.mkdir()
        sleeper = tools / "sleep"
        _ = sleeper.write_text(
            f"#!{sys.executable}\nimport os\n"
            + f"os.write({retry_write}, b'R')\n"
            + f"os.read({continue_read}, 1)\n"
        )
        sleeper.chmod(0o755)
        release = subprocess.Popen(
            ["bash", str(BOARD), "release", str(self.root), "holder", "cargo"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"]},
            pass_fds=(retry_write, continue_read),
        )
        self.addCleanup(self.stop_guard, release)
        ready, _, _ = select.select([retry_read], [], [], TEST_HANG_GUARD_SECS)
        self.assertEqual(ready, [retry_read], "release did not retry its held guard")
        self.assertEqual(os.read(retry_read, 1), b"R")
        self.assertTrue(self.lock.is_dir())
        self.stop_guard(guard_holder)
        _ = os.write(continue_write, b"C")
        stdout, stderr = release.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(release.returncode, 0, stderr)
        self.assertEqual(stdout.strip(), "released cargo")
        self.assertFalse(self.lock.exists())
        self.assert_guard_file()

    def test_release_without_pid_keeps_a_live_holder_token(self) -> None:
        holder = self.holder_process()
        self.assert_acquired(self.board("acquire", "impl", "cargo", "--pid", str(holder.pid)),
                             "acquired cargo")
        release = self.board("release", "impl", "cargo")
        self.assertEqual(release.returncode, 1, release.stdout + release.stderr)
        self.assertIn(f"impl gave no pid and cargo is held by live pid {holder.pid}", release.stderr)
        self.assertEqual((self.lock / "holder_pid").read_text(), str(holder.pid))
        self.assertNotIn("token cargo released", (self.root / "board.log").read_text())

    def test_release_without_pid_frees_a_gone_holder_token(self) -> None:
        holder = self.holder_process()
        self.assert_acquired(self.board("acquire", "impl", "cargo", "--pid", str(holder.pid)),
                             "acquired cargo")
        holder.kill()
        _ = holder.wait(timeout=TEST_HANG_GUARD_SECS)
        release = self.board("release", "impl", "cargo")
        self.assertEqual(release.returncode, 0, release.stderr)
        self.assertEqual(release.stdout.strip(), "released cargo")
        self.assertFalse(self.lock.exists())

    def test_reclaimed_same_slot_release_keeps_new_holder_lock(self) -> None:
        first = self.holder_process()
        second = self.holder_process()
        self.assert_acquired(self.board("acquire", "holder", "cargo", "--pid", str(first.pid),
                                        "--hold", "10"), "acquired cargo")
        self.assert_acquired(self.board("acquire", "holder", "cargo", "--pid", str(second.pid),
                                        "--wait", "0", epoch=1011),
                             "acquired cargo (reclaimed from holder)")

        old_release = self.board("release", "holder", "cargo", "--pid", str(first.pid))
        self.assertEqual(old_release.returncode, 3, old_release.stdout + old_release.stderr)
        self.assertEqual((self.lock / "holder_pid").read_text(), str(second.pid))
        self.assertTrue(self.lock.exists())

        unrelated_release = self.board("release", "holder", "cargo", "--pid", "1")
        self.assertEqual(unrelated_release.returncode, 1,
                         unrelated_release.stdout + unrelated_release.stderr)
        self.assertEqual((self.lock / "holder_pid").read_text(), str(second.pid))

    def test_a_reclaimed_holder_record_outlives_a_release_and_acquire(self) -> None:
        # verify.sh gives the token back for each memory wait, so the token can
        # change hands again before the reclaimed holder releases.
        first = self.holder_process()
        second = self.holder_process()
        self.assert_acquired(self.board("acquire", "holder", "cargo", "--pid", str(first.pid),
                                        "--hold", "10"), "acquired cargo")
        self.assert_acquired(self.board("acquire", "holder", "cargo", "--pid", str(second.pid),
                                        "--wait", "0", epoch=1011),
                             "acquired cargo (reclaimed from holder)")
        self.assertEqual(self.board("release", "holder", "cargo", "--pid", str(second.pid)).returncode, 0)
        self.assert_acquired(self.board("acquire", "holder", "cargo", "--pid", str(second.pid)),
                             "acquired cargo")

        old_release = self.board("release", "holder", "cargo", "--pid", str(first.pid))
        self.assertEqual(old_release.returncode, 3, old_release.stdout + old_release.stderr)
        self.assertEqual((self.lock / "holder_pid").read_text(), str(second.pid))

        # A record whose holder is gone is dropped at the next release.
        first.kill()
        _ = first.wait(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(self.board("release", "holder", "cargo", "--pid", str(second.pid)).returncode, 0)
        self.assertFalse((self.root / "locks" / "cargo.reclaimed").exists())


if __name__ == "__main__":
    _ = unittest.main()
