"""Real verification hold waits: signals."""

from __future__ import annotations

import json
import fcntl
import os
import signal
import subprocess
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast
from unittest import mock

from scripts.direct.shell_function_test_support import shell_functions
from scripts.direct.verify_token_wait_test_support import VERIFY
from scripts.direct.wait_for_test_state import wait_for_file
from scripts.direct.verify_hold_wait_test_support import (
    VerifyHoldWaitFixture,
    WaitObservation,
    read_cycle_file,
)


class VerifyHoldWaitTests(VerifyHoldWaitFixture):
    def assert_group_stop_clears_memory_wait(self, signum: signal.Signals) -> None:
        process = self.start_memory_queued_lint()
        queued = self.memory_queue_pids()
        self.assertTrue(queued, "real admission must register the waiting step")
        self.assertTrue(all(os.getpgid(pid) != process.pid for pid in queued),
                        "the step must have its own process group")
        before = self.owned_process_snapshot()
        started = time.monotonic()
        os.killpg(process.pid, signum)
        survivors = self.wait_for_owned_exit(5)
        code = process.poll()
        remaining = self.memory_queue_pids()
        after = self.owned_process_snapshot()
        output = ""
        if not survivors:
            _, output = self.finish(process)
        diagnostic = (f"{signum.name}: exit={code}, elapsed={time.monotonic() - started:.3f}s, "
                      + f"queued={sorted(queued)}, remaining={sorted(remaining)}\n"
                      + f"before (PID PPID PGID SID STAT IGNORED ARGS):\n{before}\nafter:\n{after}")
        if code != 128 + signum:
            diagnostic += "\nlast output:\n" + "\n".join(output.splitlines()[-80:])
        print(diagnostic, flush=True)
        self.assertEqual(survivors, set(), diagnostic)
        self.assertTrue(queued.isdisjoint(remaining), diagnostic)
        self.assertEqual(code, 128 + signum, diagnostic)
        self.assertEqual(self.fixture.last_call()["outcome"], "interrupted", output)
        self.assertEqual(self.fixture.saved_results(), [], output)

    def test_term_to_run_group_ends_memory_wait_and_all_children(self) -> None:
        self.assert_group_stop_clears_memory_wait(signal.SIGTERM)

    def test_int_to_run_group_ends_memory_wait_and_all_children(self) -> None:
        self.assert_group_stop_clears_memory_wait(signal.SIGINT)

    def test_hup_to_run_group_ends_memory_wait_and_all_children(self) -> None:
        self.assert_group_stop_clears_memory_wait(signal.SIGHUP)

    def test_hup_reaps_log_readers_when_the_output_pipe_is_full(self) -> None:
        # Fill the consumer's pipe before starting verify's real log helpers.
        # No consumer drains it until after shutdown, so EOF on tee's input
        # cannot finish a pending output write.
        read_fd, write_fd = os.pipe()
        try:
            capacity = fcntl.fcntl(write_fd, fcntl.F_GETPIPE_SZ)
            _ = os.write(write_fd, b"x" * capacity)
            log = self.fixture.root / "blocked-run.log"
            harness = shell_functions(VERIFY, "run_log_tee", "start_run_log", "stop_interrupted_run_log") + r'''
RUN_LOG=$1 RUN_LOG_PIDS=()
trap 'stop_interrupted_run_log; exit 129' HUP
exec 3>&2
start_run_log
printf '%8192s' x
printf '%s\n' "${RUN_LOG_PIDS[*]}" >&3
read -r command
'''
            with subprocess.Popen(["bash", "-c", harness, "test", str(log)],
                                  stdin=subprocess.PIPE, stdout=write_fd,
                                  stderr=subprocess.PIPE, start_new_session=True, text=True) as process:
                try:
                    assert process.stderr is not None
                    reader_pids = cast(str, process.stderr.readline())
                    readers = [int(pid) for pid in reader_pids.split()]
                    self.assertEqual(len(readers), 2)
                    wait_for_file(log)
                    os.killpg(process.pid, signal.SIGHUP)
                    self.assertEqual(process.wait(timeout=5), 129)
                    self.assertTrue(all(not Path(f"/proc/{pid}").exists() for pid in readers))
                finally:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    _ = process.communicate(timeout=5)
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_interrupted_verification_removes_its_registered_call(self) -> None:
        self.hold()
        observation = self.observe_waits()
        process = self.start("test", "sample", "--local", "local admission test", "--filter", "smoke")
        _ = self.wait_for_call(process)
        process.send_signal(signal.SIGTERM)
        code, output = self.finish(process)
        self.assertEqual(code, 143, output)
        self.assertEqual(self.fixture.local_runs(), [])
        self.assertEqual(read_cycle_file(self.cycle_file)["entries"], [])
        observed = cast(WaitObservation, json.loads(observation.read_text()))
        self.assertGreater(float(observed["total"]), 0)
        self.assertAlmostEqual(float(observed["total"]), sum(float(value) for value in observed["waits"]), places=5)
        self.assertFalse(Path(observed["directory"]).exists())

    def test_signal_at_waiting_announcement_records_wait_and_removes_the_call(self) -> None:
        from scripts.build_targets import build_targets

        self.hold()
        wait_file = self.fixture.root / "announced-wait"
        started = datetime.fromisoformat("2026-10-14T00:00:00+00:00")

        def interrupt_on_announcement(*messages: object, **_options: object) -> None:
            if "; waiting (tests that can run on the Mac" in " ".join(str(message) for message in messages):
                handler = signal.getsignal(signal.SIGTERM)
                assert callable(handler)
                handler(signal.SIGTERM, None)

        with mock.patch.dict(os.environ, {**self.fixture.environment,
                                          "BUILDLOG_HOLD_WAIT_FILE": str(wait_file)}), \
             mock.patch.object(build_targets, "wait_clock", side_effect=[started, started + timedelta(seconds=7)]), \
             mock.patch("builtins.print", side_effect=interrupt_on_announcement), \
             self.assertRaises(SystemExit) as interrupted:
            _ = build_targets.command_wait_local("announcement-call")
        self.assertEqual(interrupted.exception.code, 143)
        self.assertEqual(wait_file.read_text(), "7.000000\n")
        self.assertEqual(read_cycle_file(self.cycle_file)["entries"], [])

    def test_interrupt_during_a_step_wait_records_seconds_and_removes_the_call(self) -> None:
        history = self.record_phase()
        self.hold_after_first_step()
        observation = self.observe_waits()
        process = self.start("final")
        _ = self.wait_for_call(process)
        process.send_signal(signal.SIGTERM)
        code, output = self.finish(process)
        self.assertEqual(code, 143, output)
        self.assertEqual(self.fixture.local_runs(), [])
        observed = cast(WaitObservation, json.loads(observation.read_text()))
        self.assertGreater(float(observed["total"]), 0)
        self.assertAlmostEqual(float(observed["total"]), sum(float(value) for value in observed["waits"]), places=5)
        self.assertFalse(Path(observed["directory"]).exists())
        self.assertEqual(read_cycle_file(self.cycle_file)["entries"], [])
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        started = [row for row in rows if row["event_type"] == "wait_started"]
        finished = [row for row in rows if row["event_type"] == "wait_finished"]
        self.assertTrue(started)
        self.assertEqual({row["wait_id"] for row in started}, {row["wait_id"] for row in finished})
        self.assertTrue(all(cast(dict[str, object], row["wait_end"])["state"] == "Finished" for row in finished))


if __name__ == "__main__":
    _ = unittest.main()
