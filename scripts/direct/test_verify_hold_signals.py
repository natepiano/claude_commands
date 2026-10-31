"""Real verification hold waits: signals."""

from __future__ import annotations

import json
import os
import signal
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast
from unittest import mock

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
