"""Real verify.sh token reservation behavior."""
from __future__ import annotations

from scripts.build_queue import admission_records

from scripts.build_queue.admission_test_support import AdmissionFixture, caller_admission
from scripts.direct.wait_for_test_state import wait_until
from scripts.build_queue import admission_protocol as wire

import os
import shutil
import signal
import subprocess
import time
from typing import cast, override
import unittest
from pathlib import Path
from scripts.direct.verify_token_wait_test_support import VerifyTokenWaitFixture, VERIFY, BOARD
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class VerifyTokenReservationTests(VerifyTokenWaitFixture):
    admission: AdmissionFixture = cast(AdmissionFixture, cast(object, None))
    @override
    def setUp(self) -> None:
        super().setUp()
        self.admission = caller_admission(self, self.root, self.environment)

    def test_a_step_waiting_for_a_busy_token_keeps_no_memory_reservation(self) -> None:
        board = self.root / "board"
        environment = {**self.environment, "UNIT_RUN_DIR": str(board), "UNIT_TEAM_ROLE": "impl"}
        output = self.root / "waiter.out"
        holder = subprocess.Popen(["bash", str(VERIFY), "check", "sample", "--no-cache"], cwd=self.root,
                                  env={**environment, "TEST_BLOCK": "1"},
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.started_groups.add(holder.pid)
        waiter: subprocess.Popen[bytes] | None = None
        try:
            self.wait_for_running(self.root / "running", holder)
            held = set(self.admission.server.reservations)
            self.assertEqual(len(held), 1, "the running step holds one reservation")
            with output.open("wb") as stream:
                waiter = subprocess.Popen(["bash", str(VERIFY), "check", "sample", "--no-cache"], cwd=self.root,
                                          env=environment, stdout=stream, stderr=subprocess.STDOUT,
                                          start_new_session=True)
            self.started_groups.add(waiter.pid)
            self.wait_for_output(output, "waiting for the cargo token since", waiter)
            with self.admission.loop_turn():
                self.assertEqual(self.admission.server.waiters, {})
                self.assertEqual(set(self.admission.server.reservations), held)
            _ = (self.root / "release").touch()
            self.assertEqual(holder.wait(timeout=TEST_HANG_GUARD_SECS), 0)
            self.assertEqual(waiter.wait(timeout=TEST_HANG_GUARD_SECS), 0, output.read_text())
            self.assertEqual((self.root / "cargo-calls").read_text().count("check"), 2)
            self.assertEqual(self.admission.server.reservations, {})
            text = output.read_text(errors="replace")
            self.assertIn("waiting for the cargo token since", text)
            self.assertIn("took the cargo token after 0 min", text)
        finally:
            _ = (self.root / "release").touch()
            for process in (waiter, holder):
                if process is not None:
                    if process.poll() is None:
                        self.end_group(process.pid)
                    _ = process.wait(timeout=TEST_HANG_GUARD_SECS)

    def test_a_step_whose_call_is_gone_stops_waiting_for_the_token(self) -> None:
        board = self.root / "board"
        waiting = self.root / "waiting-for-token"
        real_sleep = shutil.which("sleep")
        if real_sleep is None:
            self.fail("the token wait test needs sleep on PATH")
        peer = subprocess.Popen([real_sleep, "600"], start_new_session=True)
        self.started_groups.add(peer.pid)
        self.addCleanup(peer.wait, TEST_HANG_GUARD_SECS)
        taken = subprocess.run(["bash", str(BOARD), "acquire", str(board), "peer", "cargo", "--pid", str(peer.pid),
                                "--hold", "3600", "--wait", "0"],
                               capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(taken.returncode, 0, taken.stderr)
        shell = self.root / "local-attempt.sh"
        _ = shell.write_text('''set -T
probe_local_attempt() {
    if [[ "$BASH_COMMAND" == '[[ "$STEP_DEFER_LOCAL" == 0 ]]' ]]; then STEP_DEFER_LOCAL=1; fi
    if [[ "$BASH_COMMAND" == 'sleep "$pause"' ]]; then : > "$TEST_WAITING"; fi
}
trap probe_local_attempt DEBUG
''')
        environment = {**self.environment, "UNIT_RUN_DIR": str(board), "UNIT_TEAM_ROLE": "impl",
                       "BASH_ENV": str(shell), "TEST_WAITING": str(waiting)}
        output = self.root / "waiter.out"
        with output.open("wb") as stream:
            call = subprocess.Popen(["bash", str(VERIFY), "check", "sample", "--no-cache"], cwd=self.root,
                                    env=environment, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        self.started_groups.add(call.pid)
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
            + 'if [[ "$1" == acquire ]]; then printf "token\\n" >> "$TEST_SLEEP_LOG"; fi\n'
            + 'exec bash "$TEST_REAL_BOARD" "$@"\n')
        self.write_wait_sleep('printf "sleep %s\\n" "$*" >> "$TEST_SLEEP_LOG"\n'
                             + 'printf "MemAvailable: 67108864 kB\\n" > "$BUILDLOG_MEMINFO"\n'
                             + 'exit 0\n')
        _ = self.admission.occupy_host()
        _ = Path(self.environment["BUILDLOG_MEMINFO"]).write_text("MemAvailable: 0 kB\n")
        log = self.root / "sleep-log"
        environment = {**self.environment, "HOME": str(home),
                       "UNIT_RUN_DIR": str(self.root / "board"),
                       "UNIT_TEAM_ROLE": "impl", "TEST_REAL_BOARD": str(BOARD),
                       "TEST_SLEEP_LOG": str(log),
                       "BUILDLOG_MEM_POLL_S": "1", "BUILDLOG_MEM_WAIT_LIMIT_S": "2"}
        def free_after_wait(reply: wire.Reply) -> wire.Reply:
            if isinstance(reply, wire.Wait):
                with log.open("a") as stream:
                    _ = stream.write("wait\n")
                self.admission.free(60 * admission_records.GIB)
            return reply

        self.admission.server.reply_filter = free_after_wait
        result = self.verify(environment)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertNotIn("memory wait limit reached", output)
        self.assertNotIn("starting anyway", output)
        lines = log.read_text().splitlines()
        self.assertIn("token", lines)
        # All token acquisitions precede the memory wait.
        last_token = len(lines) - 1 - lines[::-1].index("token")
        self.assertNotIn("wait", lines[:last_token], "token acquisition must precede the memory request")
        self.assertIn("wait", lines[last_token:], "the token holder waits for memory")

    def test_old_two_pass_client_does_not_block_new_token_holder(self) -> None:
        board = self.root / "board"
        old_shell = self.root / "old-client.sh"
        _ = old_shell.write_text('''set -e
source "$TEST_MEMORY_GATE"
buildlog_wait_for_memory cargo check -p sample
: > "$TEST_OLD_READY"
python3 "$TEST_WAIT_HELPER" "$TEST_OLD_RELEASE"
buildlog_memory_python "$BUILDLOG_MEM_ADMIT_SCRIPT" token-wait "$BUILDLOG_MEM_RESERVATION"
BUILDLOG_MEM_RESERVATION=""
: > "$TEST_OLD_GAP"
bash "$TEST_BOARD" acquire "$TEST_BOARD_DIR" old cargo --pid $$ --hold 3600 --wait 30
trap 'bash "$TEST_BOARD" release "$TEST_BOARD_DIR" old cargo --pid $$' EXIT
BUILDLOG_MEM_TOKEN_BACK_AT=$(python3 -c 'import time; print(time.time())')
export BUILDLOG_MEM_TOKEN_BACK_AT
BUILDLOG_MEM_WAIT_SINCE=$BUILDLOG_MEM_WAIT_FROM buildlog_wait_for_memory cargo check -p sample
buildlog_release_memory 0
''')
        ready = self.root / "old-ready"
        gap = self.root / "old-gap"
        release = self.root / "old-release"
        old_errors = self.root / "old.err"
        with old_errors.open("wb") as stream:
            old = subprocess.Popen(["bash", str(old_shell)], cwd=self.root, env={**self.environment,
                "TEST_MEMORY_GATE": str(VERIFY.parents[1] / "lint/memory_gate.sh"),
                "TEST_BOARD": str(BOARD), "TEST_BOARD_DIR": str(board),
                "TEST_OLD_READY": str(ready), "TEST_OLD_GAP": str(gap), "TEST_OLD_RELEASE": str(release)},
                stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        self.started_groups.add(old.pid)
        self.wait_for_running(ready, old)
        with self.admission.loop_turn():
            self.assertEqual(len(self.admission.server.reservations), 1)
            self.admission.free(20 * admission_records.GIB)
        new_errors = self.root / "new.err"
        new = self.start_memory_waiter({**self.environment, "UNIT_RUN_DIR": str(board),
            "UNIT_TEAM_ROLE": "new", "TEST_BLOCK": "1"}, new_errors)
        _ = release.touch()
        self.wait_for_running(gap, old)
        self.wait_for_running(self.root / "running", new)
        self.assertEqual((board / "locks/cargo.d/holder_pid").read_text(), str(new.pid))
        _ = (self.root / "release").touch()
        self.assertEqual(new.wait(timeout=TEST_HANG_GUARD_SECS), 0, new_errors.read_text())
        self.assertEqual(old.wait(timeout=TEST_HANG_GUARD_SECS), 0, old_errors.read_text())
        wait_until(self.admission.callers_released, self.admission.state_dir)
        self.assertEqual(self.admission.server.waiters, {})
        rows = [line for line in (self.admission.state_dir / "queue_times.jsonl").read_text().splitlines()
                if '"token_gap"' in line]
        self.assertEqual(len(rows), 1)



if __name__ == "__main__":
    _ = unittest.main()
