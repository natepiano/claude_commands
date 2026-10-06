#!/usr/bin/env python3
"""Cargo token recovery keeps live holders and admits one replacement."""

from __future__ import annotations

import os
import re
import select
import subprocess
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import override


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
            env={**os.environ, "PLAN_DELEGATE_NOW_EPOCH": str(epoch)},
            capture_output=True, text=True, check=False, timeout=10,
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
        ready, _, _ = select.select([helper.stdout], [], [], 5)
        self.assertTrue(ready, "guard helper did not start")
        self.assertEqual(os.read(helper.stdout.fileno(), 64).decode().strip(), "locked")
        helper.stdout.close()
        return helper

    @staticmethod
    def stop_holder(holder: subprocess.Popen[bytes]) -> None:
        if holder.poll() is None:
            holder.kill()
        _ = holder.wait(timeout=5)

    @staticmethod
    def stop_guard(helper: subprocess.Popen[str]) -> None:
        if helper.poll() is None:
            helper.kill()
        _ = helper.wait(timeout=5)

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
        _ = holder.wait(timeout=5)

        self.assert_acquired(self.board("acquire", "next", "cargo", "--wait", "0"),
                             "acquired cargo (reclaimed from holder)")
        self.assertIn(f"token cargo reclaimed from holder: holder pid {holder.pid} is gone",
                      (self.root / "board.log").read_text())
        self.assert_guard_file()

    def test_reclaims_zombie_holder(self) -> None:
        holder = self.holder_process()
        self.assert_acquired(self.board("acquire", "holder", "cargo", "--pid", str(holder.pid)),
                             "acquired cargo")
        holder.kill()
        deadline = time.monotonic() + 5
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
        _ = holder.wait(timeout=5)

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
        _ = holder.wait(timeout=5)
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
        _ = holder.wait(timeout=5)
        start = Barrier(8)

        def contender(index: int) -> subprocess.CompletedProcess[str]:
            _ = start.wait(timeout=10)
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
        _ = holder.wait(timeout=5)
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
        release = subprocess.Popen(
            ["bash", str(BOARD), "release", str(self.root), "holder", "cargo"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(self.stop_guard, release)
        with self.assertRaises(subprocess.TimeoutExpired):
            _ = release.wait(timeout=0.5)
        self.assertTrue(self.lock.is_dir())
        self.stop_guard(guard_holder)
        stdout, stderr = release.communicate(timeout=2)
        self.assertEqual(release.returncode, 0, stderr)
        self.assertEqual(stdout.strip(), "released cargo")
        self.assertFalse(self.lock.exists())
        self.assert_guard_file()


if __name__ == "__main__":
    _ = unittest.main()
