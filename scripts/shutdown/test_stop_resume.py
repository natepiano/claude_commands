from __future__ import annotations

import io
import os
import shlex
import signal
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from threading import Barrier, Lock
from typing import cast, final, override
from unittest.mock import Mock, call, patch

from scripts.shutdown import stop_resume
import herdr
import pane_record
import sessions
import shutdown
from test_pane_restore import FakeClient


@final
class StopResumeTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.entry = pane_record.launch("w1:p2", "sid-alpha", str(self.root),
            {"CLAUDE_CONFIG_DIR": str(self.root / ".claude3"), "SHOWRUNNER_UNIT": "alpha"},
            ["claude", "-n", "alpha", "--remote-control", "alpha", "--model", "sonnet"])
        self.client = FakeClient([{"pane_id": "w1:p2", "label": "alpha", "workspace_id": "w1",
                                   "agent_session": {"kind": "id", "value": "sid-alpha"}}])
        self.live = Mock(return_value=sessions.SessionNotLive())
        self.process = Mock(return_value=sessions.LiveSessionProcess(123))
        self.kill = Mock()
        self.wait = Mock()
        self.start = Mock(return_value="")
        self.seed = Mock(return_value=0)

    @override
    def setUp(self) -> None:
        _ = self.enterContext(patch.object(herdr, "shared_session", return_value="agents"))
        _ = self.enterContext(patch.object(herdr, "Herdr", return_value=self.client))
        _ = self.enterContext(patch.object(pane_record, "RECORD_DIR", self.root / "panes"))
        pane_record.write("agents", self.entry)
        _ = self.enterContext(patch.object(sessions, "session_liveness", self.live))
        _ = self.enterContext(patch.object(sessions, "process_start", return_value="generation-1"))
        _ = self.enterContext(patch.object(sessions, "process_liveness", self.process))
        _ = self.enterContext(patch.object(os, "kill", self.kill))
        _ = self.enterContext(patch.object(stop_resume, "Event", self.wait))
        _ = self.enterContext(patch.object(self.client, "run", self.start))
        _ = self.enterContext(patch("unit_runs.held", return_value=[]))
        _ = self.enterContext(patch.object(pane_record, "seed", self.seed))

    @staticmethod
    def _typed(arguments: tuple[str, ...]) -> tuple[str, ...]:
        """What the pane's shell parses from the one line herdr types."""
        return (*arguments[:3], *shlex.split(arguments[3]))

    def test_a_stopped_tab_resumes_by_id_or_label_from_its_launch_entry(self) -> None:
        self.client.rows[0]["agent_session"] = None
        for session in ("sid-alpha", "alpha"):
            self.assertEqual(stop_resume.resume(session, why="maintenance"), stop_resume.Resumed("sid-alpha", "w1:p2", "claude3"))
        self.assertEqual(stop_resume.resume("sid-other", why="maintenance"), stop_resume.SessionNotFound("sid-other"))

    def test_launches_are_read_while_claude_runs_before_any_stop(self) -> None:
        order: list[str] = []
        def seed(_session: str) -> int:
            order.append("seed")
            return 0
        self.seed.side_effect = seed
        def stop_one(session: str) -> stop_resume.NotRunning:
            order.append("stop")
            return stop_resume.NotRunning(session)
        with patch.object(stop_resume, "_stop", side_effect=stop_one):
            _ = stop_resume.move_sessions(["sid-alpha", "sid-beta"], "claude4", why="limit")
        self.assertEqual(order, ["seed", "stop", "stop"])
        self.seed.side_effect = herdr.HerdrFailure("no reply")
        self.assertEqual(stop_resume.stop("sid-alpha"), stop_resume.NotRunning("sid-alpha"))

    def test_stop_sends_term_then_kill_to_the_same_claude_and_writes_nothing(self) -> None:
        self.live.return_value = sessions.LiveSessionProcess(123)
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.Stopped("sid-alpha", 123))
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM), call(123, signal.SIGKILL)])
        cast(Mock, cast(Mock, self.wait.return_value).wait).assert_called_once_with(2.0)
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)
        self.start.assert_not_called()

    def test_stop_does_not_kill_an_exited_or_replaced_process(self) -> None:
        for final in (sessions.SessionNotLive(), sessions.LiveSessionProcess(456)):
            with self.subTest(final=final):
                self.kill.reset_mock()
                self.live.side_effect = [sessions.LiveSessionProcess(123), sessions.LiveSessionProcess(123), final]
                self.assertEqual(stop_resume.stop("sid-alpha"), stop_resume.Stopped("sid-alpha", 123))
                self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM)])

    def test_reused_pid_generation_is_checked_before_each_signal(self) -> None:
        for changed_before_term in (True, False):
            with self.subTest(before_term=changed_before_term):
                self.kill.reset_mock()
                self.live.return_value = sessions.LiveSessionProcess(123)
                self.process.side_effect = ([sessions.SessionNotLive()] if changed_before_term else
                                           [sessions.LiveSessionProcess(123), sessions.SessionNotLive()])
                result = stop_resume.stop("alpha")
                self.assertIsInstance(result, stop_resume.NotRunning if changed_before_term else stop_resume.Stopped)
                self.assertEqual(self.kill.call_args_list, [] if changed_before_term else [call(123, signal.SIGTERM)])

    def test_absent_and_unknown_sessions_are_distinct_and_unsignalled(self) -> None:
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.NotRunning("sid-alpha"))
        self.live.return_value = sessions.SessionLivenessUnknown("registry unreadable")
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.StopFailed("sid-alpha", "registry unreadable"))
        self.assertEqual(stop_resume.stop("elsewhere"), stop_resume.SessionNotFound("elsewhere"))
        self.assertEqual(stop_resume.resume("elsewhere", why="maintenance"), stop_resume.SessionNotFound("elsewhere"))
        self.kill.assert_not_called()
        self.start.assert_not_called()

    def test_tab_labels_are_exact_and_unidentified_or_ambiguous_tabs_never_signal(self) -> None:
        self.assertEqual(stop_resume.stop("alph"), stop_resume.SessionNotFound("alph"))
        self.client.rows.append({"pane_id": "w1:p4", "label": "empty", "workspace_id": "w1"})
        self.assertEqual(stop_resume.stop("empty"), stop_resume.SessionNotFound("empty"))
        self.client.rows.append({"pane_id": "w1:p5", "label": "alpha", "workspace_id": "w1",
                                 "agent_session": {"kind": "id", "value": "sid-other"}})
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.StopFailed("alpha", "alpha: several tabs match"))
        self.assertEqual(stop_resume.stop("sid-alpha"), stop_resume.NotRunning("sid-alpha"))
        self.kill.assert_not_called()
        self.start.assert_not_called()

    def test_resume_refuses_a_live_session_without_starting(self) -> None:
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.assertEqual(stop_resume.resume("alpha", why="maintenance"), stop_resume.ResumeFailed("sid-alpha", "Claude is still running"))
        self.start.assert_not_called()

    def test_resume_runs_account_exec_in_its_own_pane_with_one_message(self) -> None:
        result = stop_resume.resume("alpha", why="maintenance")
        self.assertEqual(result, stop_resume.Resumed("sid-alpha", "w1:p2", "claude3"))
        self.start.assert_called_once()
        command = self._typed(cast(tuple[str, ...], self.start.call_args.args))
        self.assertEqual(command[:4], ("pane", "run", "w1:p2", "env"))
        self.assertIn("SHOWRUNNER_UNIT=alpha", command)
        index = command.index("exec")
        self.assertTrue(command[index - 1].endswith("accounts/accounts.py"))
        self.assertEqual(command[index + 1:-1], ("claude3", "--resume", "sid-alpha", "-n", "alpha",
            "--remote-control", "alpha", "--model", "sonnet", "--settings", '{"disableAgentView": true}'))
        self.assertIn("stopped and resumed", command[-1])
        self.assertIn("maintenance", command[-1])
        self.assertIn("Continue where you stopped", command[-1])
        self.assertIn("Codex", command[-1])
        self.assertEqual(self.client.closed, [])
        self.assertEqual(self.client.started, [])

    def test_resume_refuses_a_mismatched_entry_but_does_not_wait_for_idle(self) -> None:
        self.entry["session_id"] = "sid-other"
        pane_record.write("agents", self.entry)
        self.assertIsInstance(stop_resume.resume("alpha", why="maintenance"), stop_resume.ResumeFailed)
        self.start.assert_not_called()
        self.entry["session_id"] = "sid-alpha"
        pane_record.write("agents", self.entry)
        with patch.object(self.client, "pane_activity", side_effect=AssertionError("no idle gate")):
            self.assertEqual(stop_resume.resume("alpha", why="maintenance"), stop_resume.Resumed("sid-alpha", "w1:p2", "claude3"))

    def test_move_stops_then_runs_the_target_account_in_the_same_pane(self) -> None:
        self.live.side_effect = [sessions.LiveSessionProcess(123), sessions.LiveSessionProcess(123),
                                 sessions.SessionNotLive(), sessions.SessionNotLive()]
        result = stop_resume.move("alpha", "claude4", why="weekly limit")
        self.assertEqual(result, stop_resume.Resumed("sid-alpha", "w1:p2", "claude4"))
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM)])
        command = self._typed(cast(tuple[str, ...], self.start.call_args.args))
        self.assertEqual(command[command.index("exec") + 1], "claude4")
        self.assertIn("moved from claude3 to claude4", command[-1])

    def test_move_of_a_stopped_session_resumes_but_a_failed_stop_does_not(self) -> None:
        self.assertEqual(stop_resume.move("alpha", "claude4", why="limit"), stop_resume.Resumed("sid-alpha", "w1:p2", "claude4"))
        self.start.reset_mock()
        self.live.return_value = sessions.SessionLivenessUnknown("identity unreadable")
        self.assertEqual(stop_resume.move("alpha", "claude4", why="limit"), stop_resume.StopFailed("sid-alpha", "identity unreadable"))
        self.start.assert_not_called()
        self.kill.assert_not_called()

    def test_a_failed_pane_run_never_reports_resume_success(self) -> None:
        self.start.side_effect = herdr.HerdrFailure("pane run refused")
        self.assertEqual(stop_resume.resume("alpha", why="maintenance"), stop_resume.ResumeFailed("sid-alpha", "pane run refused"))
        self.assertEqual(self.client.closed, [])

    def test_batch_stops_all_before_any_resume_and_uses_each_own_pane(self) -> None:
        second = pane_record.launch("w1:p4", "sid-beta", str(self.root),
            {"CLAUDE_CONFIG_DIR": self.entry["config_dir"]}, ["claude", "-n", "beta", "--model", "opus"])
        self.client.rows.append({"pane_id": "w1:p4", "label": "beta", "workspace_id": "w1",
                                 "agent_session": {"kind": "id", "value": "sid-beta"}})
        pane_record.write("agents", second)
        both_stopping = Barrier(2, timeout=3)
        both_resuming = Barrier(2, timeout=3)
        stopped: list[str] = []
        lock = Lock()
        def stop_one(session: str) -> stop_resume.Stopped:
            _ = both_stopping.wait()
            with lock:
                stopped.append(session)
            return stop_resume.Stopped(session, 123)
        def run(*_command: str) -> str:
            self.assertCountEqual(stopped, ["sid-alpha", "sid-beta"])
            _ = both_resuming.wait()
            return ""
        self.start.side_effect = run
        with patch.object(stop_resume, "_stop", side_effect=stop_one):
            results = stop_resume.move_sessions(["sid-alpha", "sid-beta"], "claude4", why="limit")
        self.assertEqual(results, [stop_resume.Resumed("sid-alpha", "w1:p2", "claude4"),
                                   stop_resume.Resumed("sid-beta", "w1:p4", "claude4")])
        calls = [self._typed(cast(tuple[str, ...], invocation.args)) for invocation in self.start.call_args_list]
        self.assertEqual({args[2]: args[args.index("--resume") + 1] for args in calls},
                         {"w1:p2": "sid-alpha", "w1:p4": "sid-beta"})

    def test_batch_failures_do_not_stop_other_members(self) -> None:
        def stop_one(session: str) -> stop_resume.NotRunning:
            if session == "stop-error":
                raise OSError("cannot signal")
            return stop_resume.NotRunning(session)
        def resume_one(session: str, *, why: str, to: str) -> stop_resume.Resumed | stop_resume.SessionNotFound:
            self.assertEqual(why, "limit")
            if session == "resume-error":
                raise OSError("cannot run")
            if session == "gone-tab":
                return stop_resume.SessionNotFound(session)
            return stop_resume.Resumed(session, "own-pane", to)
        with patch.object(stop_resume, "_stop", side_effect=stop_one), patch.object(stop_resume, "resume", side_effect=resume_one):
            results = stop_resume.move_sessions(["stop-error", "resume-error", "gone-tab", "good"], "claude4", why="limit")
        self.assertEqual(results, [stop_resume.StopFailed("stop-error", "cannot signal"),
            stop_resume.ResumeFailed("resume-error", "cannot run"),
            stop_resume.ResumeFailed("gone-tab", "session has no tab after stop"), stop_resume.Resumed("good", "own-pane", "claude4")])

    def test_warning_precedes_all_stops_and_is_limited_to_fifteen_seconds(self) -> None:
        events: list[str] = []
        def warn(session: str, _target: str, _why: str, _seconds: int) -> None:
            events.append("warn " + session)
        def stop_one(session: str) -> stop_resume.StopFailed:
            self.assertCountEqual(events, ["warn first", "warn second"])
            return stop_resume.StopFailed(session, "fixture")
        with patch.object(stop_resume, "_warn_session", side_effect=warn), patch.object(stop_resume, "_stop", side_effect=stop_one), patch.object(stop_resume, "monotonic", side_effect=[100.0, 102.0]):
            results = stop_resume.move_sessions(["first", "second"], "claude4", why="limit", warn_seconds=15)
            self.assertEqual(len(results), 2)
            with self.assertRaises(ValueError):
                _ = stop_resume.move_sessions(["first"], "claude4", why="limit", warn_seconds=16)
        cast(Mock, cast(Mock, self.wait.return_value).wait).assert_called_once_with(13.0)

    def test_cli_prints_one_line_and_uses_the_result_exit_status(self) -> None:
        for arguments, expected in ((["stop", "alpha"], 0), (["resume", "alpha", "--why", "maintenance"], 0),
                                    (["stop", "elsewhere"], 2)):
            with self.subTest(arguments=arguments), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(shutdown.main(arguments), expected)
                self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.live.return_value = sessions.LiveSessionProcess(123)
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(shutdown.main(["resume", "alpha", "--why", "maintenance"]), 1)
        self.assertEqual(len(output.getvalue().splitlines()), 1)


if __name__ == "__main__":
    _ = unittest.main()
