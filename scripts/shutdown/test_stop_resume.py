from __future__ import annotations

import io
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from threading import Barrier, Lock, local
from typing import cast, final, override
from unittest.mock import Mock, call, patch

from scripts.shutdown import stop_resume
import herdr
import pane_record
import sessions
import shutdown
from test_pane_restore import FakeClient


class ResumeClock(local):
    elapsed: float = 0.0


@final
class StopResumeTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.entry = pane_record.launch("w1:p2", "sid-alpha", str(self.root),
            {"CLAUDE_CONFIG_DIR": str(self.root / ".claude3"), "SHOWRUNNER_UNIT": "alpha"},
            ["claude", "-n", "alpha", "--remote-control", "alpha", "--model", "sonnet"])
        self.client = FakeClient([{"pane_id": "w1:p2", "tab_id": "w1:t9", "label": "alpha", "workspace_id": "w1",
                                   "agent_session": {"kind": "id", "value": "sid-alpha"}}])
        self.live = Mock(return_value=sessions.SessionNotLive())
        self.process = Mock(return_value=sessions.LiveSessionProcess(123))
        self.kill = Mock()
        self.wait = Mock()
        self.start = Mock(return_value="")
        self.seed = Mock(return_value=0)
        self.clock = ResumeClock()
        self.launched: set[str] = set()

    @override
    def setUp(self) -> None:
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "", "SHUTDOWN_STATE_DIR": str(self.root / "shutdown")})))
        _ = self.enterContext(patch.object(herdr, "shared_session", return_value="agents"))
        _ = self.enterContext(patch.object(herdr, "Herdr", return_value=self.client))
        _ = self.enterContext(patch.object(pane_record, "RECORD_DIR", self.root / "panes"))
        pane_record.write("agents", self.entry)
        _ = self.enterContext(patch.object(sessions, "session_liveness", self.observe))
        _ = self.enterContext(patch.object(sessions, "process_start", return_value="generation-1"))
        _ = self.enterContext(patch.object(sessions, "process_liveness", self.process))
        _ = self.enterContext(patch.object(os, "kill", self.kill))
        _ = self.enterContext(patch.object(stop_resume, "Event", self.wait))
        cast(Mock, cast(Mock, self.wait.return_value).wait).side_effect = self.advance
        _ = self.enterContext(patch.object(self.client, "run", self.pane_run))
        _ = self.enterContext(patch.object(stop_resume, "monotonic", lambda: self.clock.elapsed))
        _ = self.enterContext(patch.object(time, "sleep", self.advance))
        _ = self.enterContext(patch("unit_runs.held", return_value=[]))
        _ = self.enterContext(patch.object(pane_record, "seed", self.seed))

    def advance(self, seconds: float) -> None:
        self.clock.elapsed += seconds

    def observe(self, session: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive | sessions.SessionLivenessUnknown:
        return sessions.LiveSessionProcess(456) if session in self.launched else cast(
            sessions.LiveSessionProcess | sessions.SessionNotLive | sessions.SessionLivenessUnknown, self.live(session))

    def pane_run(self, *arguments: str) -> str:
        result = cast(str, self.start(*arguments))
        command = self._typed(arguments)
        self.launched.add(command[command.index("--resume") + 1])
        return result

    @staticmethod
    def _typed(arguments: tuple[str, ...]) -> tuple[str, ...]:
        """What the pane's shell parses from the one line herdr types."""
        return (*arguments[:3], *shlex.split(arguments[3]))

    def add_beta(self) -> None:
        second = pane_record.launch("w1:p4", "sid-beta", str(self.root),
            {"CLAUDE_CONFIG_DIR": self.entry["config_dir"]}, ["claude", "-n", "beta", "--model", "opus"])
        self.client.rows.append({"pane_id": "w1:p4", "tab_id": "w1:t7", "label": "beta", "workspace_id": "w1",
                                 "agent_session": {"kind": "id", "value": "sid-beta"}})
        pane_record.write("agents", second)

    def test_a_stopped_tab_resumes_by_id_or_label_from_its_launch_entry(self) -> None:
        self.client.rows[0]["agent_session"] = None
        for session in ("sid-alpha", "alpha"):
            self.launched.clear()
            self.assertEqual(stop_resume.resume(session, why="maintenance"), stop_resume.Resumed("sid-alpha", "w1:p2", "claude3"))
        self.assertEqual(stop_resume.resume("sid-other", why="maintenance"), stop_resume.SessionNotFound("sid-other"))

    def test_a_new_session_herdr_has_no_id_for_is_found_through_the_launches_read_first(self) -> None:
        self.client.rows[0]["agent_session"] = None
        pane_record.record_path("agents").unlink()
        def seed(_session: str) -> int:
            pane_record.write("agents", self.entry)
            return 0
        self.seed.side_effect = seed
        self.assertEqual(stop_resume.move_sessions(["alpha"], "claude4", why="limit"),
                         [stop_resume.Resumed("sid-alpha", "w1:p2", "claude4")])

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
        self.launched.clear()
        self.seed.side_effect = herdr.HerdrFailure("no reply")
        self.assertEqual(stop_resume.stop("sid-alpha"), stop_resume.NotRunning("sid-alpha"))

    def test_stop_sends_term_then_kill_to_the_same_claude_and_writes_nothing(self) -> None:
        self.live.return_value = sessions.LiveSessionProcess(123)
        def signal_delivered(_pid: int, sig: int) -> None:
            if sig == signal.SIGKILL:
                self.live.return_value = sessions.SessionNotLive()
                self.process.return_value = sessions.SessionNotLive()
        self.kill.side_effect = signal_delivered
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.Stopped("sid-alpha", 123))
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM), call(123, signal.SIGKILL)])
        cast(Mock, cast(Mock, self.wait.return_value).wait).assert_called_once_with(2.0)
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)
        self.start.assert_not_called()

    def test_stop_does_not_kill_an_exited_or_replaced_process(self) -> None:
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        self.assertEqual(stop_resume.stop("sid-alpha"), stop_resume.Stopped("sid-alpha", 123))
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM)])

    def test_registry_removal_does_not_resume_until_the_saved_process_exits(self) -> None:
        self.live.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive(), sessions.SessionNotLive()]
        self.process.side_effect = [sessions.LiveSessionProcess(123)] * 3 + [sessions.SessionNotLive()]
        def start(*_command: str) -> str:
            self.assertEqual(self.process.call_count, 4, "registry deletion is not process exit")
            self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM), call(123, signal.SIGKILL)])
            return ""
        self.start.side_effect = start
        self.assertEqual(stop_resume.move("alpha", "claude4", why="limit"),
                         stop_resume.Resumed("sid-alpha", "w1:p2", "claude4"))

    def test_stop_confirms_exit_after_kill(self) -> None:
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.process.side_effect = [sessions.LiveSessionProcess(123)] * 3 + [sessions.SessionNotLive()]
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.Stopped("sid-alpha", 123))
        self.assertEqual(self.process.call_count, 4)
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM), call(123, signal.SIGKILL)])

    def test_a_process_still_alive_after_kill_fails_and_never_resumes(self) -> None:
        self.live.return_value = sessions.LiveSessionProcess(123)
        with patch.object(stop_resume, "monotonic", side_effect=[10.0, 10.0, 15.0]):
            result = stop_resume.move("alpha", "claude4", why="limit")
        self.assertIsInstance(result, stop_resume.StopFailed)
        assert isinstance(result, stop_resume.StopFailed)
        self.assertIn("123", result.reason)
        self.assertIn("SIGKILL", result.reason)
        self.start.assert_not_called()

    def test_self_move_hands_off_the_whole_batch_without_signalling(self) -> None:
        self.add_beta()
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform), patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sid-alpha"}), patch.object(sys, "platform", platform), patch.object(subprocess, "run", return_value=subprocess.CompletedProcess[str]([], 0, "", "")) as run, patch.object(subprocess, "Popen") as popen:
                self.client.rows[0]["agent_session"] = None
                result = stop_resume.move_sessions(["beta", "alpha"], "claude4", why="limit", warn_seconds=3)
                self.assertEqual(result, [stop_resume.MoveHandedOff("sid-beta"), stop_resume.MoveHandedOff("sid-alpha")])
                invocation = run.call_args if platform == "linux" else popen.call_args
                self.assertIsNotNone(invocation)
                assert invocation is not None
                argv = cast(list[str], invocation.args[0])
                if platform == "linux":
                    self.assertEqual(argv[:5], ["systemd-run", "--user", "--collect", "--quiet", "--no-block"])
                    self.assertIn("CLAUDE_CODE_SESSION_ID", argv)
                else:
                    self.assertTrue(cast(bool, invocation.kwargs["start_new_session"]))
                self.assertNotIn("CLAUDE_CODE_SESSION_ID", cast(dict[str, str], invocation.kwargs["env"]))
                self.assertEqual(cast(object, invocation.kwargs["stdin"]), subprocess.DEVNULL)
                self.assertIn("batch", argv)
                self.assertEqual(argv[argv.index("--target") + 1], "claude4")
                self.assertEqual(argv[-2:], ["beta", "alpha"])
        self.kill.assert_not_called()
        self.start.assert_not_called()

    def test_detached_batch_uses_the_same_moves_and_logs_each_member(self) -> None:
        log = self.root / "batch.log"
        self.live.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive(), sessions.SessionNotLive()]
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sid-alpha"}), patch.object(subprocess, "run") as run, patch.object(subprocess, "Popen") as popen:
            result = stop_resume.main(["batch", "--target", "claude4", "--why", "limit", "--log", str(log), "alpha", "missing"])
        self.assertEqual(result, 1)
        self.assertEqual(len(log.read_text().splitlines()), 2)
        self.assertIn('"result": "Resumed"', log.read_text())
        self.assertIn('"result": "SessionNotFound"', log.read_text())
        self.start.assert_called_once()
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM)])
        self.seed.assert_called_once()
        run.assert_not_called()
        popen.assert_not_called()

    def test_failed_handoff_reports_failure_without_signalling(self) -> None:
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sid-alpha"}), patch.object(sys, "platform", "linux"), patch.object(subprocess, "run", side_effect=OSError("service manager unavailable")):
            result = stop_resume.move_sessions(["alpha"], "claude4", why="limit")
        self.assertEqual(result, [stop_resume.StopFailed("alpha", "cannot hand off move: service manager unavailable")])
        self.kill.assert_not_called()
        self.start.assert_not_called()

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
        self.entry["environment"].update(HERDR_PANE_ID="w9:p9", HERDR_WORKSPACE_ID="w9")
        pane_record.write("agents", self.entry)
        result = stop_resume.resume("alpha", why="maintenance")
        self.assertEqual(result, stop_resume.Resumed("sid-alpha", "w1:p2", "claude3"))
        self.start.assert_called_once()
        command = self._typed(cast(tuple[str, ...], self.start.call_args.args))
        self.assertEqual(command[:4], ("pane", "run", "w1:p2", "env"))
        self.assertIn("SHOWRUNNER_UNIT=alpha", command)
        self.assertIn("HERDR_PANE_ID=w1:p2", command)
        self.assertEqual(command[4:6], ("-u", "HERDR_TAB_ID"))
        self.assertIn("HERDR_WORKSPACE_ID=w1", command)
        self.assertEqual(self.clock.elapsed, stop_resume.STAY_SECONDS)
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

    def test_admitted_pane_run_with_no_start_retries_three_times_then_fails(self) -> None:
        with patch.object(self.client, "run", self.start):
            result = stop_resume.resume("alpha", why="maintenance")
        self.assertEqual(result, stop_resume.ResumeFailed("sid-alpha", "never started"))
        self.assertEqual(self.start.call_count, 3)
        self.assertEqual(self.clock.elapsed, 60.0)
        self.assertEqual({invocation.args[2] for invocation in self.start.call_args_list}, {"w1:p2"})

    def test_a_start_that_exits_retries_in_the_same_pane_and_stays_up(self) -> None:
        def observed(_session: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
            if self.start.call_count == 0 or (self.start.call_count == 1 and self.clock.elapsed >= 1.0):
                return sessions.SessionNotLive()
            return sessions.LiveSessionProcess(456)
        with patch.object(sessions, "session_liveness", side_effect=observed):
            result = stop_resume.resume("alpha", why="maintenance")
        self.assertEqual(result, stop_resume.Resumed("sid-alpha", "w1:p2", "claude3"))
        self.assertEqual(self.start.call_count, 2)
        self.assertEqual(self.clock.elapsed, 11.0)
        self.assertEqual([invocation.args[2] for invocation in self.start.call_args_list], ["w1:p2", "w1:p2"])

    def test_three_early_exits_report_failure_instead_of_resumed(self) -> None:
        starts: list[float] = []
        def start(*_command: str) -> str:
            starts.append(self.clock.elapsed)
            return ""
        def observed(_session: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
            return (sessions.LiveSessionProcess(456) if starts and self.clock.elapsed - starts[-1] < 1.0
                    else sessions.SessionNotLive())
        self.start.side_effect = start
        with patch.object(sessions, "session_liveness", side_effect=observed):
            result = stop_resume.resume("alpha", why="maintenance")
        self.assertEqual(result, stop_resume.ResumeFailed("sid-alpha", "ended after 1 s"))
        self.assertEqual(starts, [0.0, 1.0, 2.0])

    def test_saved_entry_finds_an_unlabelled_pane_with_empty_or_stale_herdr_id(self) -> None:
        self.client.rows[0]["label"] = None
        for agent in (None, {"kind": "id", "value": "stale-session"}):
            with self.subTest(agent=agent):
                self.launched.clear()
                self.client.rows[0]["agent_session"] = agent
                self.assertEqual(stop_resume.resume("sid-alpha", why="maintenance"),
                                 stop_resume.Resumed("sid-alpha", "w1:p2", "claude3"))

    def test_saved_entry_never_selects_a_different_live_conversation(self) -> None:
        self.client.rows[0]["agent_session"] = {"kind": "id", "value": "another-live-session"}
        self.live.return_value = sessions.LiveSessionProcess(789)
        self.assertEqual(stop_resume.resume("sid-alpha", why="maintenance"), stop_resume.SessionNotFound("sid-alpha"))
        self.assertEqual(stop_resume.stop("sid-alpha"), stop_resume.SessionNotFound("sid-alpha"))
        self.start.assert_not_called()
        self.kill.assert_not_called()

    def test_saved_entries_for_closed_panes_do_not_create_ambiguous_matches(self) -> None:
        missing = self.entry.copy()
        missing["pane_id"] = "w1:p-missing"
        pane_record.write("agents", missing)
        self.assertEqual(stop_resume.resume("sid-alpha", why="maintenance"),
                         stop_resume.Resumed("sid-alpha", "w1:p2", "claude3"))

    def test_resume_refuses_a_mismatched_entry_but_does_not_wait_for_idle(self) -> None:
        self.entry["session_id"] = "sid-other"
        pane_record.write("agents", self.entry)
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.assertEqual(stop_resume.resume("alpha", why="maintenance"), stop_resume.SessionNotFound("alpha"))
        self.start.assert_not_called()
        self.entry["session_id"] = "sid-alpha"
        self.live.return_value = sessions.SessionNotLive()
        pane_record.write("agents", self.entry)
        with patch.object(self.client, "pane_activity", side_effect=AssertionError("no idle gate")):
            self.assertEqual(stop_resume.resume("alpha", why="maintenance"), stop_resume.Resumed("sid-alpha", "w1:p2", "claude3"))

    def test_move_stops_then_runs_the_target_account_in_the_same_pane(self) -> None:
        self.live.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive(), sessions.SessionNotLive()]
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        result = stop_resume.move("alpha", "claude4", why="weekly limit")
        self.assertEqual(result, stop_resume.Resumed("sid-alpha", "w1:p2", "claude4"))
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM)])
        command = self._typed(cast(tuple[str, ...], self.start.call_args.args))
        self.assertEqual(command[command.index("exec") + 1], "claude4")
        self.assertIn("moved from claude3 to claude4", command[-1])

    def test_move_of_a_stopped_session_resumes_but_a_failed_stop_does_not(self) -> None:
        self.assertEqual(stop_resume.move("alpha", "claude4", why="limit"), stop_resume.Resumed("sid-alpha", "w1:p2", "claude4"))
        self.start.reset_mock()
        self.launched.clear()
        self.live.return_value = sessions.SessionLivenessUnknown("identity unreadable")
        self.assertEqual(stop_resume.move("alpha", "claude4", why="limit"), stop_resume.StopFailed("sid-alpha", "identity unreadable"))
        self.start.assert_not_called()
        self.kill.assert_not_called()

    def test_a_failed_pane_run_never_reports_resume_success(self) -> None:
        self.start.side_effect = herdr.HerdrFailure("pane run refused")
        self.assertEqual(stop_resume.resume("alpha", why="maintenance"), stop_resume.ResumeFailed("sid-alpha", "pane run refused"))
        self.assertEqual(self.client.closed, [])

    def test_batch_stops_all_before_any_resume_and_uses_each_own_pane(self) -> None:
        self.add_beta()
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
        self.launched.clear()
        self.live.return_value = sessions.LiveSessionProcess(123)
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(shutdown.main(["resume", "alpha", "--why", "maintenance"]), 1)
        self.assertEqual(len(output.getvalue().splitlines()), 1)


if __name__ == "__main__":
    _ = unittest.main()
