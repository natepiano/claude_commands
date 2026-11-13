"""Verify that restart and stop share one monotonic deadline."""
from __future__ import annotations

import math
import os
import signal
import subprocess
import tempfile
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from typing import cast, override
from unittest.mock import Mock, patch

from scripts.herdr import herdr
from scripts.agents import restart
from scripts.lib import atomic_file, conf, operation_deadline
from scripts.production import staff
from scripts.agents import stop_resume


class DeadlineTests(unittest.TestCase):
    now: float = 100.0

    @override
    def setUp(self) -> None:
        _ = self.enterContext(patch.object(herdr, "herdr_binary", return_value=str(Path(herdr.__file__).with_name("fake_herdr.py"))))
        self.now = 100.0
        config = Path(self.enterContext(tempfile.TemporaryDirectory()))
        atomic_file.write_text_atomic(config / "shutdown.conf",
            "# whole: 1..300\nSTOP_GRACE_SECONDS=2\n# whole: 1..300\nKILL_GRACE_SECONDS=5\n")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(config)})))
        _ = self.enterContext(patch.object(time, "monotonic", lambda: self.now))
        _ = self.enterContext(patch.object(stop_resume, "monotonic", lambda: self.now))

    def block(self, argv: list[str], *, timeout: float, **_arguments: object) -> subprocess.CompletedProcess[str]:
        self.now += timeout
        raise subprocess.TimeoutExpired(argv, timeout)

    def row(self) -> staff.StaffRow:
        return staff.StaffRow(staff.StaffId("row"), "unit", "alpha", staff.StaffRoot(),
                              current_session=staff.StaffId("session"), unit_type=staff.UnitDetails("direct"))

    def test_stop_row_limits_a_blocking_pane_read(self) -> None:
        with (patch.object(subprocess, "run", side_effect=self.block),
              patch.object(stop_resume, "stop") as stop):
            self.assertFalse(restart.stop_row(self.row(), herdr.Herdr("agents"), deadline=100.25))
        self.assertLessEqual(self.now, 100.26)
        stop.assert_not_called()

    def test_start_row_limits_a_blocking_live_read(self) -> None:
        def read(_identity: str, _kind: str = "") -> herdr.StoppedConversation:
            _ = herdr.run_program(["fake"], timeout=10)
            return herdr.StoppedConversation()

        with (patch.object(herdr, "read_conversation", side_effect=read),
              patch.object(subprocess, "run", side_effect=self.block),
              patch.object(herdr, "launch_prepared_agent") as launch):
            with self.assertRaises(subprocess.TimeoutExpired):
                _ = restart.start_row(self.row(), staff.StaffSnapshot(()), herdr.Herdr("agents"), deadline=100.25)
        self.assertLessEqual(self.now, 100.26)
        launch.assert_not_called()

    def test_stop_limits_a_blocking_live_read(self) -> None:
        def read(_identity: str, _kind: str = "") -> herdr.StoppedConversation:
            _ = herdr.Herdr("agents").panes()
            return herdr.StoppedConversation()

        with (patch.object(staff, "row_operation", return_value=nullcontext()),
              patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((self.row(),))),
              patch.object(stop_resume, "_record_launches"),
              patch.object(herdr, "read_conversation", side_effect=read),
              patch.object(subprocess, "run", side_effect=self.block)):
            result = stop_resume.stop("session", deadline=100.25)
        self.assertIsInstance(result, stop_resume.StopFailed)
        self.assertLessEqual(self.now, 100.26)

    def test_expired_deadline_sends_no_signal(self) -> None:
        with (operation_deadline.within(99),
              patch.dict(os.environ, {"CONF_DIR": os.environ["CONF_DIR"]}, clear=True),
              patch.object(os, "kill") as send):
            for number in (signal.SIGTERM, signal.SIGKILL):
                with self.subTest(number=number):
                    with self.assertRaisesRegex(TimeoutError, "deadline passed"):
                        stop_resume._signal_process(123, number)  # pyright: ignore[reportPrivateUsage]
        send.assert_not_called()

    def test_stop_returns_failure_without_a_signal_after_the_outer_deadline(self) -> None:
        with (patch.object(staff, "row_operation", return_value=nullcontext()),
              patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((self.row(),))),
              patch.object(stop_resume, "_record_launches"),
              patch.object(herdr, "read_conversation", return_value=herdr.LiveConversation("claude", (123,))),
              patch.object(herdr, "native_process", return_value=herdr.NativeAgentProcess(
                  herdr.EnvironmentThread("session"), "started", {})),
              patch.object(stop_resume, "_same_process", return_value=True),
              patch.object(herdr, "check_native_target", return_value=True),
              patch.dict(os.environ, {"CONF_DIR": os.environ["CONF_DIR"]}, clear=True), patch.object(os, "kill") as send):
            result = stop_resume.stop("session", deadline=99)
        self.assertIsInstance(result, stop_resume.StopFailed)
        self.assertIn("deadline passed", str(result))
        send.assert_not_called()

    def test_term_wait_ends_at_deadline_without_kill(self) -> None:
        row = self.row()
        client = staff.StaffSnapshot((row,))
        proved = herdr.NativeAgentProcess(herdr.EnvironmentThread("session"), "started", {})
        def advance(seconds: float) -> bool:
            self.now += seconds
            return False
        event = Mock(wait=Mock(side_effect=advance))
        with (patch.object(staff, "row_operation", return_value=nullcontext()),
              patch.object(staff, "initial_snapshot", return_value=client),
              patch.object(stop_resume, "_record_launches"),
              patch.object(herdr, "read_conversation", return_value=herdr.LiveConversation("claude", (123,))),
              patch.object(herdr, "native_process", return_value=proved),
              patch.object(herdr, "check_native_target", return_value=True),
              patch.object(stop_resume, "_same_process", return_value=True),
              patch.object(stop_resume, "_signal_process") as send,
              patch.object(stop_resume, "Event", return_value=event)):
            result = stop_resume.stop("session", deadline=100.25)
        self.assertIsInstance(result, stop_resume.StopFailed)
        self.assertIn("received SIGTERM and still runs", str(result))
        self.assertIn("deadline passed", str(result))
        self.assertLessEqual(self.now, 100.26)
        send.assert_called_once_with(123, signal.SIGTERM)

    def test_default_keeps_both_signal_waits(self) -> None:
        row = self.row()
        client = staff.StaffSnapshot((row,))
        proved = herdr.NativeAgentProcess(herdr.EnvironmentThread("session"), "started", {})
        def advance(seconds: float) -> bool:
            self.now += seconds
            return False
        event = Mock(wait=Mock(side_effect=advance))
        with (patch.object(staff, "row_operation", return_value=nullcontext()),
              patch.object(staff, "initial_snapshot", return_value=client),
              patch.object(stop_resume, "_record_launches"),
              patch.object(herdr, "read_conversation", return_value=herdr.LiveConversation("claude", (123,))),
              patch.object(herdr, "native_process", return_value=proved),
              patch.object(herdr, "check_native_target", return_value=True),
              patch.object(stop_resume, "_same_process", return_value=True),
              patch.object(stop_resume, "_signal_process") as send,
              patch.object(stop_resume, "Event", return_value=event)):
            result = stop_resume.stop("session")
        self.assertIsInstance(result, stop_resume.StopFailed)
        self.assertNotIn("deadline passed", str(result))
        self.assertAlmostEqual(self.now, 107)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args_list[0].args, (123, signal.SIGTERM))
        self.assertEqual(send.call_args_list[1].args, (123, signal.SIGKILL))

    def test_default_keeps_the_program_cap(self) -> None:
        with patch.object(subprocess, "run", side_effect=self.block):
            with self.assertRaises(subprocess.TimeoutExpired):
                _ = herdr.run_program(["fake"])
        self.assertEqual(self.now, 110)
        with operation_deadline.within(math.inf):
            self.assertEqual(operation_deadline.remaining(float(conf.read("shutdown").text("STOP_GRACE_SECONDS"))), 2)
            self.assertEqual(operation_deadline.remaining(float(conf.read("shutdown").text("KILL_GRACE_SECONDS"))), 5)


if __name__ == "__main__":
    _ = unittest.main()
