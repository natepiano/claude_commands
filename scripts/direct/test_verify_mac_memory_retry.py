"""Real verify.sh Mac routing through memory admission, with disposable tools."""
from __future__ import annotations

import fcntl
import shlex
import subprocess
import unittest
from typing import final
from scripts.direct.verify_mac_memory_retry_test_support import VerifyMacMemoryRetryFixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@final
class VerifyMacMemoryRetryTests(VerifyMacMemoryRetryFixture):
    def test_token_guard_retry_finishes_while_memory_stays_held(self) -> None:
        meminfo = self.hold_memory()
        board = self.home / ".claude/scripts/direct/board.sh"
        command = ["bash", str(board)]
        acquired = subprocess.run([*command, "acquire", str(self.board), "test", "cargo"],
                                  env=self.environment, capture_output=True, text=True,
                                  timeout=TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(acquired.returncode, 0, acquired.stdout + acquired.stderr)
        # Complete one delay before the guard collision.
        _ = subprocess.run(["sleep", "0"], env=self.environment, check=True,
                           timeout=TEST_HANG_GUARD_SECS)
        delegate = self.bin_directory / "retry-sleep"
        _ = (self.bin_directory / "sleep").rename(delegate)
        entered = self.runner_state / "guard-retry-entered"
        released = self.runner_state / "guard-retry-released"
        self.write_executable(self.bin_directory / "sleep", '#!/bin/sh\n'
            + f'touch {shlex.quote(str(entered))}\n'
            + f'"$TEST_REAL_PYTHON" "$TEST_WAIT_HELPER" {shlex.quote(str(released))}\n'
            + f'exec {shlex.quote(str(delegate))} "$@"\n')
        with (self.board / "locks/cargo.guard").open("a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            process = subprocess.Popen([*command, "renew", str(self.board), "test", "cargo", "--hold", "3600"],
                                       env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.addCleanup(self.stop_verify, process)
            self.wait_for_path(entered, (process,))
            fcntl.flock(guard, fcntl.LOCK_UN)
            released.touch()
            output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(meminfo.read_text(), "MemAvailable: 0 kB\n")

    def test_mac_busy_again_after_leaving_returns_to_the_local_wait(self) -> None:
        meminfo = self.hold_memory()
        process = self.start_verify("test", "sample", "--no-cache", extra={
            "TEST_RUNNER_BUSY_UNTIL": str(self.runner_state / "mac-free"), **self.leave_once()})
        self.addCleanup(self.stop_verify, process)
        # The Mac claim declined again; the local run waits with the hook still set.
        self.wait_for_path(self.runner_state / "free-now-after-leave", (process,))
        self.assertEqual(len(self.runner_events()), 2)
        self.assertEqual(self.local_runs(), [])
        self.release_memory(meminfo)
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(len(self.local_runs()), 1)
        self.assertEqual(len(self.runner_events()), 2)
        self.assertEqual(output.count("verify.sh: running on natedev"), 2)
        self.assertEqual(self.routing_record()["target"], "natedev")
        record = self.last_call()
        self.assertEqual((record["mac"], record["mac_reason"]), ("declined", "busy"))


if __name__ == "__main__":
    _ = unittest.main()
