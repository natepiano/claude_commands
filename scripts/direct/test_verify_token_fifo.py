"""Real verify.sh token fifo behavior."""
from __future__ import annotations

import subprocess
import unittest
from scripts.direct.verify_token_wait_test_support import VerifyTokenWaitFixture, VERIFY, BOARD
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class VerifyTokenFifoTests(VerifyTokenWaitFixture):
    def test_the_token_goes_to_the_call_that_waited_longest(self) -> None:
        # On 2026-10-22 about 25 calls polled one board's token, any of them
        # took it when it came free, and two calls waited 3 and 3.5 h. A call
        # now waits while an older call that still runs verify.sh waits.
        board = self.root / "board"
        waiters = board / "token_waiters"
        waiters.mkdir(parents=True)
        older = subprocess.Popen(["bash", "-c", 'read -r -t "$1" _; :', "verify.sh",
                                  str(TEST_HANG_GUARD_SECS)], stdin=subprocess.PIPE,
                                 start_new_session=True)
        self.started_groups.add(older.pid)
        older_entry = waiters / f"peer.0123456789abcdef.1000000.{older.pid}.again"
        older_entry.touch()
        output = self.root / "queued.out"
        with output.open("wb") as stream:
            call = subprocess.Popen(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                                    env={**self.environment, "UNIT_RUN_DIR": str(board),
                                         "UNIT_TEAM_ROLE": "impl"},
                                    stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        self.started_groups.add(call.pid)
        try:
            self.wait_for_output(output, "calls ahead of this one: 1", call)
            self.assertIn("it is free and goes to an earlier call", output.read_text())
            self.assertFalse((board / "locks" / "cargo.d").exists(), "the newer call took the token")
            self.end_group(older.pid)
            _ = older.wait(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(call.wait(timeout=TEST_HANG_GUARD_SECS), 0, output.read_text())
        finally:
            if older.stdin is not None:
                older.stdin.close()
            for process in (older, call):
                if process.poll() is None:
                    self.end_group(process.pid)
                _ = process.wait(timeout=TEST_HANG_GUARD_SECS)
        self.assertIn("took the cargo token after 0 min", output.read_text())
        self.assertEqual(list(waiters.iterdir()), [], "an ended waiter's file stays in the queue")
        self.assertEqual((self.root / "cargo-calls").read_text().count("check"), 1)

    def test_a_long_token_wait_never_gives_up(self) -> None:
        # Each refused acquire moves the clock past the old 1800 s limit.
        home = self.root / "busy-home"
        direct_dir = home / ".claude/scripts/direct"
        direct_dir.mkdir(parents=True)
        for source in VERIFY.parent.parent.iterdir():
            if source.name != "direct":
                (direct_dir.parent / source.name).symlink_to(source, target_is_directory=source.is_dir())
        for source in VERIFY.parent.iterdir():
            if source.name != "board.sh":
                (direct_dir / source.name).symlink_to(source, target_is_directory=source.is_dir())
        _ = (direct_dir / "board.sh").write_text('''#!/bin/bash
if [[ "$1" == acquire ]]; then
    count=$(( $(cat "$TEST_ACQUIRES" 2>/dev/null || echo 0) + 1 ))
    printf '%s\\n' "$count" > "$TEST_ACQUIRES"
    if (( count <= 3 )); then
        printf '%s\\n' $(( $(cat "$UNIT_TOKEN_CLOCK_FILE") + 1800 )) > "$UNIT_TOKEN_CLOCK_FILE"
        echo "busy cargo held by peer" >&2
        exit 1
    fi
fi
exec bash "$TEST_REAL_BOARD" "$@"
''')
        clock = self.root / "busy-clock"
        _ = clock.write_text("100\n")
        acquires = self.root / "acquires"
        environment = {**self.environment, "HOME": str(home),
                       "UNIT_RUN_DIR": str(self.root / "busy-board"),
                       "UNIT_TEAM_ROLE": "impl", "TEST_REAL_BOARD": str(BOARD),
                       "TEST_ACQUIRES": str(acquires), "UNIT_TOKEN_CLOCK_FILE": str(clock)}
        result = self.verify(environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("did not get it", result.stderr)
        self.assertGreaterEqual(int(acquires.read_text()), 4)
        self.assertEqual(self.records()[0]["token_wait_s"], 5400)
        self.assertFalse((self.root / "busy-board" / "locks" / "cargo.d").exists())



if __name__ == "__main__":
    _ = unittest.main()
