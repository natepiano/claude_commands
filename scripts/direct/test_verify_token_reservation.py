"""Real verify.sh token reservation behavior."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
import unittest
from pathlib import Path
from scripts.direct.verify_token_wait_test_support import VerifyTokenWaitFixture, VERIFY, BOARD
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class VerifyTokenReservationTests(VerifyTokenWaitFixture):
    def test_a_step_waiting_for_a_busy_token_keeps_no_memory_reservation(self) -> None:
        # A reservation kept through the token wait promises memory no build
        # uses; with many such steps the gate held every step back. The waiter
        # gives the token back for its memory wait, a peer takes it, then the
        # waiter's gate admits it while the token is busy.
        board = self.root / "board"
        admission = self.root / "buildlog" / "admission"
        waiting = self.root / "waiting-for-token"
        meminfo = Path(self.environment["BUILDLOG_MEMINFO"])
        _ = meminfo.write_text("MemAvailable: 0 kB\n")
        peer_meminfo = self.root / "peer-meminfo"
        _ = peer_meminfo.write_text("MemAvailable: 50331648 kB\n")
        sleep = self.root / "stubs" / "sleep"
        _ = sleep.write_text("#!/bin/sh\n"
                             + 'if [ "$1" = 3 ] && [ -n "${TEST_WAITING:-}" ]; then : > "$TEST_WAITING"; fi\n'
                             + 'exit 0\n')
        sleep.chmod(0o755)
        environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                       "UNIT_TEAM_ROLE": "impl", "BUILDLOG_MEM_POLL_S": "1"}
        output = self.root / "waiter.out"
        waiter = self.start_memory_waiter({**environment, "TEST_WAITING": str(waiting)}, output)
        holder: subprocess.Popen[bytes] | None = None
        try:
            holder = subprocess.Popen(["bash", str(VERIFY), "check", "sample", "--no-cache"], cwd=self.root,
                                      env={**environment, "BUILDLOG_MEMINFO": str(peer_meminfo),
                                           "TEST_BLOCK": "1"},
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      start_new_session=True)
            self.started_groups.add(holder.pid)
            self.wait_for_running(self.root / "running", holder)
            held = sorted(admission.glob("*.reservation"))
            self.assertEqual(len(held), 1, "the running step holds one reservation")
            _ = meminfo.write_text("MemAvailable: 50331648 kB\n")
            self.wait_for_running(waiting, waiter)
            self.assertEqual(sorted(admission.glob("*.reservation")), held)
            _ = (self.root / "release").touch()
            self.assertEqual(holder.wait(timeout=TEST_HANG_GUARD_SECS), 0)
            self.assertEqual(waiter.wait(timeout=TEST_HANG_GUARD_SECS), 0, output.read_text())
            self.assertEqual((self.root / "cargo-calls").read_text().count("check"), 2)
            self.assertEqual(list(admission.glob("*.reservation")), [])
            # The wait after the memory wait shows in the log; on 2026-10-22 its
            # log ended on "starting anyway" for hours.
            text = output.read_text(errors="replace")
            self.assertIn("waiting for the cargo token since", text)
            self.assertIn("took the cargo token after 0 min", text)
        finally:
            _ = meminfo.write_text("MemAvailable: 50331648 kB\n")
            _ = (self.root / "release").touch()
            for process in (waiter, holder):
                if process is not None:
                    if process.poll() is None:
                        self.end_group(process.pid)
                    _ = process.wait(timeout=TEST_HANG_GUARD_SECS)

    def test_a_step_whose_call_is_gone_stops_waiting_for_the_token(self) -> None:
        # On 2026-10-20 a test killed a call's group and left its step, in a
        # group of its own, trying for the token for 45 h. The step gives the
        # token back for its memory wait, a peer takes it, and the call is
        # killed while its step waits for the token again.
        board = self.root / "board"
        waiting = self.root / "waiting-for-token"
        meminfo = Path(self.environment["BUILDLOG_MEMINFO"])
        _ = meminfo.write_text("MemAvailable: 0 kB\n")
        real_sleep = shutil.which("sleep")
        if real_sleep is None:
            self.fail("the token wait test needs sleep on PATH")
        sleep = self.root / "stubs" / "sleep"
        _ = sleep.write_text("#!/bin/sh\n"
                             + 'if [ "$1" = 3 ] && [ -n "${TEST_WAITING:-}" ]; then : > "$TEST_WAITING"; fi\n'
                             + 'exit 0\n')
        sleep.chmod(0o755)
        environment = {**self.environment, "PLAN_DELEGATE_BOARD_DIR": str(board),
                       "PLAN_DELEGATE_TEAM_ROLE": "impl", "BUILDLOG_MEM_POLL_S": "1",
                       "TEST_REAL_SLEEP": real_sleep, "TEST_WAITING": str(waiting)}
        output = self.root / "waiter.out"
        call = self.start_memory_waiter(environment, output)
        peer = subprocess.Popen([real_sleep, "600"], start_new_session=True)
        self.started_groups.add(peer.pid)
        self.addCleanup(peer.wait, TEST_HANG_GUARD_SECS)
        taken = subprocess.run(["bash", str(BOARD), "acquire", str(board), "peer", "cargo", "--pid", str(peer.pid),
                                "--hold", "3600", "--wait", "0"],
                               capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(taken.returncode, 0, taken.stderr)
        _ = meminfo.write_text("MemAvailable: 50331648 kB\n")
        self.wait_for_running(waiting, call)
        os.kill(call.pid, signal.SIGKILL)
        _ = call.wait(timeout=TEST_HANG_GUARD_SECS)
        deadline = time.monotonic() + TEST_HANG_GUARD_SECS
        while (left := self.live_group_processes(call.pid)) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(left, [], output.read_text(errors="replace"))
        self.assertIn("the call that started this step is gone", output.read_text(errors="replace"))

    def test_a_token_wait_keeps_the_steps_place_and_never_starts_without_memory(self) -> None:
        # On 2026-10-21 a step waited 15 min for memory, 24 min for the token,
        # then 15 min for memory again. No pass starts a step without memory now:
        # the first poll sleep frees memory, the step's token try fails, and the
        # pass after the token wait admits at once, in the first pass's place.
        # The board wrapper makes the step's one try for the token fail, and
        # logs each other acquire next to the sleep calls.
        home = self.root / "busy-home"
        delegate = home / ".claude/scripts/direct"
        delegate.mkdir(parents=True)
        for source in VERIFY.parent.parent.iterdir():
            if source.name != "direct":
                (delegate.parent / source.name).symlink_to(source, target_is_directory=source.is_dir())
        for source in VERIFY.parent.iterdir():
            if source.name != "board.sh":
                (delegate / source.name).symlink_to(source, target_is_directory=source.is_dir())
        _ = (delegate / "board.sh").write_text(
            '#!/bin/bash\n'
            + 'if [[ "$1" == acquire && " $* " == *" --wait 0 "* ]]; then exit 1; fi\n'
            + 'if [[ "$1" == acquire ]]; then printf "token\\n" >> "$TEST_SLEEP_LOG"; fi\n'
            + 'exec bash "$TEST_REAL_BOARD" "$@"\n')
        sleep = self.root / "stubs" / "sleep"
        _ = sleep.write_text('#!/bin/sh\nprintf "sleep %s\\n" "$*" >> "$TEST_SLEEP_LOG"\n'
                             + 'printf "MemAvailable: 67108864 kB\\n" > "$BUILDLOG_MEMINFO"\n'
                             + 'exit 0\n')
        sleep.chmod(0o755)
        _ = Path(self.environment["BUILDLOG_MEMINFO"]).write_text("MemAvailable: 0 kB\n")
        log = self.root / "sleep-log"
        environment = {**self.environment, "HOME": str(home),
                       "UNIT_RUN_DIR": str(self.root / "board"),
                       "UNIT_TEAM_ROLE": "impl", "TEST_REAL_BOARD": str(BOARD),
                       "TEST_SLEEP_LOG": str(log),
                       "BUILDLOG_MEM_POLL_S": "1", "BUILDLOG_MEM_WAIT_LIMIT_S": "2"}
        result = self.verify(environment)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertNotIn("memory wait limit reached", output)
        self.assertNotIn("starting anyway", output)
        lines = log.read_text().splitlines()
        self.assertIn("token", lines)
        # The step's acquire after its failed try is the last one in the log.
        last_token = len(lines) - 1 - lines[::-1].index("token")
        self.assertIn("sleep 1", lines[:last_token], "the first memory wait polls until memory is free")
        self.assertNotIn("sleep 1", lines[last_token:], "the pass after the token wait waited again")



if __name__ == "__main__":
    _ = unittest.main()
