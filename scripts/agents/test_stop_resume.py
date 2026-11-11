from __future__ import annotations

from scripts.agents import remote
from scripts.herdr import herdr_types

import io
import fcntl
import json
import math
import os
import shlex
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable, Mapping
from contextlib import closing, nullcontext, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Lock, local
from typing import cast, final, override
from unittest.mock import Mock, call, patch

from scripts.accounts import accounts, moves
from scripts.whoami import account, agent_accounts
from scripts.agents import restart, stop_resume, stop_results
from scripts.lib import agent_program, atomic_file, conf, operation_deadline, unit_runs
from scripts.herdr import worker_panes
from scripts.agents.stop_results import ClaudeStartState, StopResult

from scripts.agents.stop_results import SourceReleased, SourceReleaseUnknown
from scripts.agents.launch_names import LaunchNames
from scripts.production import showrunners, staff, staff_sources, stall_watch, worker_admission
from scripts.lib.resume_message import ClaudeChildResume
from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_worker, isolated_staff_file
from scripts.usage import agents_log
from scripts.herdr import herdr, pane_launch
from scripts.agents import pane_record, shutdown
from scripts.message import sessions
from scripts.message import sessions as runtime_sessions
from scripts.production import unit_lookup, test_worker
from scripts.direct import progress_history
from scripts.lib.test_models_registry import write_registry
from scripts.agents.test_pane_restore import FakeClient

RUN_IN_PANE = herdr.Herdr.run_in_pane


_REAL_CONFIRM_MOVE_START = stop_resume._confirm_move_start  # pyright: ignore[reportPrivateUsage]
_REAL_TELL = showrunners.tell


def confirmed_fixture_move(result: stop_resume.ClaudeUp, _requester: str, _why: str) -> stop_resume.ClaudeUp:
    return result


class ResumeClock(local):
    elapsed: float = 0.0


class WorkerResumeTests(unittest.TestCase):
    def test_resume_without_a_worker_record_returns_the_restored_pane(self) -> None:
        case = test_worker.WorkerCommandTests("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        _ = write_registry(case.fixture.root / "conf/models.conf", {"assignments": {"direct": "claude"}})
        atomic_file.write_text_atomic(case.fixture.root / "conf/shutdown.conf",
            (Path(__file__).resolve().parents[2] / "config/shutdown.conf").read_text())
        opened = case.opened()
        _ = progress_history.begin_dispatch(case.run_dir, slot=opened.row.name, phase="8", role="impl",
            worker_name=opened.row.name, worker_id=opened.row.id, launcher_command=["assign.sh"],
            prompt=case.prompt, kind="impl", lens="")
        world = case.fixture.world
        world.locations["w1:p0"]["agent_session"] = {"value": case.fixture.parent.current_session}
        _ = world.stop(opened.row, world)
        pane_record.save(world.session, {})

        def running(identity: str, pane: str, *, kind: str = "claude"
                    ) -> stop_resume.ClaudeUp | sessions.SessionNotLive:
            self.assertEqual((identity, kind), (opened.row.current_session, "claude"))
            return (stop_resume.ClaudeUp(identity, pane, "claude") if pane in world.foreground else
                    sessions.SessionNotLive())

        with (patch.object(stop_resume, "running_on_account", side_effect=running) as proof,
              patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              redirect_stdout(io.StringIO())):
            result = stop_resume.resume(str(opened.row.id))
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        assert isinstance(result, stop_resume.ClaudeUp)
        self.assertEqual(result.session_id, opened.row.current_session)
        self.assertEqual(result.pane_id, opened.pane)
        self.assertEqual(world.locations[result.pane_id]["terminal_id"], opened.terminal)
        self.assertEqual(world.closed, [])
        self.assertEqual(result.start_state, ClaudeStartState.STARTED)
        self.assertEqual(proof.call_args.args, (opened.row.current_session, result.pane_id))


@final
class StopResumeTests(unittest.TestCase):
    def test_stop_unit_workers_rerun_after_pane_stop(self) -> None:
        parent = staff.StaffId("holder")
        rows = [staff.StaffRow(staff.StaffId(f"row-{i}"), "worker", f"worker-{i}", staff.ReportsTo(parent),
                              current_session=staff.StaffId(f"native-{i}")) for i in range(5)]
        states: list[worker_panes.WorkerPane] = [
            worker_panes.OpenWorker(rows[0], "p0", "t0", herdr.AgentStatus.WORKING),
            worker_panes.StoppedWorker(rows[1], "p1", "t1"), worker_panes.ClosedWorker(rows[2]),
            worker_panes.ProcessUnknownWorker(rows[3], "p3", "t3", "unreadable process"),
            worker_panes.UncertainWorker(rows[4], "duplicate terminal")]
        record = staff.StaffSnapshot(tuple(rows))
        def finish(row: staff.StaffRow, client: herdr.Herdr, *, deadline: float) -> bool:
            del client
            self.assertEqual(row, rows[0])
            self.assertEqual(deadline, math.inf)
            states[0] = worker_panes.StoppedWorker(rows[0], "p0", "t0")
            return True
        def observed(_parent: staff.StaffId, _record: staff.StaffSnapshot) -> list[worker_panes.WorkerPane]:
            return list(states)
        with (patch.object(worker_panes, "observe", side_effect=observed),
              patch.object(restart, "stop_row", side_effect=finish) as stop):
            expected = ["worker-3: unreadable process", "worker-4: duplicate terminal"]
            with redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(stop_resume.stop_unit_workers(parent, record), expected)
            self.assertEqual(errors.getvalue(), "")
            self.assertEqual(stop_resume.stop_unit_workers(parent, record), expected)
        self.assertEqual(stop.call_count, 1)
        self.assertEqual(record.rows, tuple(rows))

    def test_unresolved_worker_has_one_report_line_and_keeps_stop_diagnostics(self) -> None:
        parent = staff.StaffId("holder")
        row = staff.StaffRow(staff.StaffId("fixed-worker"), "worker", "impl", staff.ReportsTo(parent),
                             current_session=staff.StaffId("native-worker"))
        def stop(_row: staff.StaffRow, _client: herdr.Herdr, *, deadline: float) -> bool:
            del deadline
            print("warning: recovered shutdown configuration", file=sys.stderr)
            print("impl: restart anomaly: no shell proof", file=sys.stderr)
            return False
        with (patch.object(worker_panes, "observe", return_value=[worker_panes.OpenWorker(row, "pane", "terminal", herdr.AgentStatus.WORKING)]),
              patch.object(restart, "stop_row", side_effect=stop), redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(stop_resume.stop_unit_workers(parent, staff.StaffSnapshot((row,))), ["impl: not proved stopped"])
        self.assertEqual(errors.getvalue().splitlines(), ["warning: recovered shutdown configuration",
            "impl: restart anomaly: no shell proof"])

    def test_worker_stop_proves_the_retained_pane_without_a_director_tab(self) -> None:
        parent = staff.StaffId("holder")
        row = staff.StaffRow(staff.StaffId("fixed-worker"), "worker", "impl", staff.ReportsTo(parent),
                             current_session=staff.StaffId("native-worker"))
        before = worker_panes.OpenWorker(row, "pane", "terminal", herdr.AgentStatus.WORKING)
        for pane, terminal, activity, conversation, success in (
                ("", "", herdr.PaneAtShell(), herdr.StoppedConversation(), True),
                ("", "", herdr.PaneAtShell(), herdr.LiveConversation("claude", (123,)), False),
                ("pane", "terminal", herdr.PaneAtShell(), herdr.StoppedConversation(), True),
                ("other-pane", "terminal", herdr.PaneAtShell(), herdr.StoppedConversation(), False),
                ("pane", "other-terminal", herdr.PaneAtShell(), herdr.StoppedConversation(), False),
                ("pane", "terminal", herdr.PaneRunning(("busy",)), herdr.StoppedConversation(), False),
                ("pane", "terminal", herdr.PaneRunning(("codex", "python")), herdr.StoppedConversation(), False),
                ("pane", "terminal", herdr.PaneAtShell(), herdr.LiveConversation("claude", (123,)), False)):
            with (self.subTest(pane=pane, terminal=terminal, activity=activity),
                  patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
                  patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((row,))),
                  patch.object(worker_panes, "observe", side_effect=[[before], [worker_panes.ClosedWorker(row)]]),
                  patch.object(self.client, "panes", return_value=[{"pane_id": pane, "terminal_id": terminal}] if pane else []),
                  patch.object(self.client, "tab_sessions", return_value=[]),
                  patch.object(self.client, "pane_activity", return_value=activity),
                  patch.object(herdr, "read_conversation", return_value=conversation),
                  patch.object(stop_resume, "_stop", return_value=stop_resume.Stopped("native-worker", 123))):
                result = stop_resume.stop(row.id)
                self.assertEqual(isinstance(result, stop_resume.Stopped), success)
                if isinstance(activity, herdr.PaneRunning):
                    self.assertIsInstance(result, stop_resume.StopFailed)
                    assert isinstance(result, stop_resume.StopFailed)
                    self.assertIn("pane pane", result.reason)
                    self.assertIn(f"foreground processes: {', '.join(activity.commands)}", result.reason)

    def test_worker_stop_rejects_an_unreadable_retained_pane(self) -> None:
        row = staff.StaffRow(staff.StaffId("fixed-worker"), "worker", "impl", staff.ReportsTo(staff.StaffId("holder")),
                             current_session=staff.StaffId("native-worker"))
        before = worker_panes.OpenWorker(row, "pane", "terminal", herdr.AgentStatus.WORKING)
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(worker_panes, "observe", return_value=[before]),
              patch.object(self.client, "panes", side_effect=herdr_types.HerdrFailure("unreadable")),
              patch.object(stop_resume, "_stop", return_value=stop_resume.Stopped("native-worker", 123))):
            result = stop_resume.stop(row.id)
        self.assertIsInstance(result, stop_resume.StopFailed)

    def test_stopped_codex_worker_needs_no_launch_or_signal_on_a_repeated_stop(self) -> None:
        identity = self.entry["session_id"]
        row = staff.StaffRow(staff.StaffId("fixed-worker"), "worker", "impl", staff.ReportsTo(staff.StaffId("holder")),
                             current_session=staff.StaffId(identity))
        self.client.rows[0]["agent_session"] = None
        atomic_file.write_text_atomic(pane_record.record_path("agents"), "{}\n")
        observed = worker_panes.StoppedWorker(row, "w1:p2", "w1:p2-terminal")
        results: list[StopResult] = []
        stop = stop_resume.stop
        def finish(session: str, *, deadline: float) -> StopResult:
            result = stop(session, deadline=deadline)
            results.append(result)
            return result
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(worker_panes, "observe", return_value=[observed]),
              patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({}, ("sdlc (Codex thread unknown)",))),
              patch.object(stop_resume, "stop", side_effect=finish),
              patch.object(stop_resume, "_stop", wraps=stop_resume._stop) as native_stop,  # pyright: ignore[reportPrivateUsage]
              patch.object(stop_resume, "_worker_stopped", wraps=stop_resume._worker_stopped) as proof,  # pyright: ignore[reportPrivateUsage]
              redirect_stderr(io.StringIO()) as errors):
            self.assertIsNone(pane_record.publication_for_session(identity))
            self.assertTrue(restart.stop_row(row, self.client))
        self.assertEqual(results, [stop_resume.NotRunning(identity)])
        native_stop.assert_not_called()
        self.assertEqual(proof.call_count, 1)
        self.kill.assert_not_called()
        self.assertEqual(errors.getvalue(), "")

    def test_closed_worker_still_signals_its_live_native_conversation(self) -> None:
        identity = self.entry["session_id"]
        row = staff.StaffRow(staff.StaffId("fixed-worker"), "worker", "impl", staff.ReportsTo(staff.StaffId("holder")),
                             current_session=staff.StaffId(identity))
        self.client.rows.clear()
        self.entry["kind"] = "codex"
        pane_record.write("agents", self.entry)
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(worker_panes, "observe", return_value=[worker_panes.ClosedWorker(row)]),
              patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({identity: (123,)}, ())),
              patch.object(stop_resume, "_stop", wraps=stop_resume._stop) as native_stop):  # pyright: ignore[reportPrivateUsage]
            self.assertTrue(restart.stop_row(row, self.client))
        native_stop.assert_called_once_with(identity, "")
        self.kill.assert_called_once_with(123, signal.SIGTERM)

    def test_closed_worker_needs_no_retained_shell_proof(self) -> None:
        row = staff.StaffRow(staff.StaffId("closed-worker"), "worker", "impl", staff.ReportsTo(staff.StaffId("holder")),
                             current_session=staff.StaffId("native-worker"))
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(worker_panes, "observe", return_value=[worker_panes.ClosedWorker(row)]),
              patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning("native-worker"))):
            self.assertEqual(stop_resume.stop(row.id), stop_resume.NotRunning("native-worker"))

    def test_shutdown_uses_committed_values_and_keeps_the_recovery_warning(self) -> None:
        config = self.root / "committed-config"
        path = config / "shutdown.conf"
        atomic_file.write_text_atomic(path, "# range: 5e-324..\nSTOP_GRACE_SECONDS=.2\n# range: 5e-324..\nKILL_GRACE_SECONDS=.3\n")
        for args in (("init", "-q"), ("add", "shutdown.conf"),
                     ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", "commit", "-qm", "settings")):
            _ = subprocess.run(["git", "-C", str(config), *args], capture_output=True, check=True)
        atomic_file.write_text_atomic(path, "# range: 5e-324..\nSTOP_GRACE_SECONDS=bad\n# range: 5e-324..\nKILL_GRACE_SECONDS=.3\n")
        self.process.side_effect = self.alive_until_kill
        self.live.return_value = sessions.LiveSessionProcess(123)
        with patch.dict(os.environ, {"CONF_DIR": str(config)}), redirect_stderr(io.StringIO()) as warnings:
            result = stop_resume.stop(self.entry["session_id"])
        self.assertIsInstance(result, stop_resume.Stopped)
        self.assertIn("STOP_GRACE_SECONDS='bad'", warnings.getvalue())
        self.assertAlmostEqual(self.clock.elapsed, .2)

    def test_worker_stops_share_the_outer_deadline_concurrently(self) -> None:
        parent = staff.StaffId("holder")
        rows = tuple(staff.StaffRow(staff.StaffId(f"row-{i}"), "worker", f"worker-{i}", staff.ReportsTo(parent),
                                   current_session=staff.StaffId(f"native-{i}")) for i in range(3))
        states = [worker_panes.OpenWorker(row, row.name, row.name, herdr.AgentStatus.WORKING) for row in rows]
        config = self.root / "config"
        atomic_file.write_text_atomic(config / "shutdown.conf", "# range: 5e-324..\nSTOP_GRACE_SECONDS=.25\n# range: 5e-324..\nKILL_GRACE_SECONDS=.5\n")
        joined = Barrier(3)
        def stop(row: staff.StaffRow, client: herdr.Herdr, *, deadline: float) -> bool:
            del row, client
            self.assertEqual(deadline, outer_deadline)
            _ = joined.wait(timeout=5)
            return True
        outer_deadline = time.monotonic() + 100
        with (operation_deadline.within(outer_deadline),
              patch.dict(os.environ, {"CONF_DIR": str(config)}),
              patch.object(worker_panes, "observe", return_value=states),
              patch.object(restart, "stop_row", side_effect=stop)):
            self.assertEqual(stop_resume.stop_unit_workers(parent, staff.StaffSnapshot(rows)), [])

    def test_slow_reads_preserve_each_concurrent_signal_grace(self) -> None:
        parent = staff.StaffId("holder")
        rows = tuple(staff.StaffRow(staff.StaffId(f"row-{i}"), "worker", f"worker-{i}", staff.ReportsTo(parent),
                                   current_session=staff.StaffId(f"native-{i}")) for i in range(3))
        states = [worker_panes.OpenWorker(row, row.name, row.name, herdr.AgentStatus.WORKING) for row in rows]
        for stop_grace, kill_grace in ((.25, .5), (.5, 1)):
            joined, lock = Barrier(3), Lock()
            elapsed: list[float] = []
            self.kill.reset_mock()
            self.clock.elapsed = 0
            config = self.root / "config"
            atomic_file.write_text_atomic(config / "shutdown.conf",
                f"# range: 5e-324..\nSTOP_GRACE_SECONDS={stop_grace}\n# range: 5e-324..\nKILL_GRACE_SECONDS={kill_grace}\n")
            def read(identity: str, _kind: str = "") -> herdr.LiveConversation:
                self.advance(10)
                _ = joined.wait(timeout=5)
                return herdr.LiveConversation("claude",  (100 + int(identity.rsplit("-", 1)[1]),))
            def native(pid: int, _kind: str) -> herdr.NativeAgentProcess:
                return herdr.NativeAgentProcess(herdr.EnvironmentThread(str(rows[pid - 100].current_session)), "started", {})
            def send(pid: int, number: int) -> None:
                operation_deadline.check()
                self.kill(pid, number)
            def stopped(session: str, kind: str = "") -> StopResult:
                result = stop_resume._stop(session, kind)  # pyright: ignore[reportPrivateUsage]
                with lock:
                    elapsed.append(self.clock.elapsed)
                return result
            with (self.subTest(stop_grace=stop_grace, kill_grace=kill_grace),
                  patch.dict(os.environ, {"CONF_DIR": str(config)}),
                  patch.object(time, "monotonic", lambda: self.clock.elapsed),
                  patch.object(staff, "row_operation", return_value=nullcontext()),
                  patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot(rows)),
                  patch.object(worker_panes, "observe", return_value=states),
                  patch.object(self.client, "panes", return_value=[]),
                  patch.object(stop_resume, "_stop_recorded", side_effect=stopped),
                  patch.object(stop_resume, "_same_process", return_value=True),
                  patch.object(stop_resume, "_signal_process", side_effect=send),
                  patch.object(herdr, "read_conversation", side_effect=read),
                  patch.object(herdr, "native_process", side_effect=native),
                  patch.object(herdr, "check_native_target", return_value=True)):
                self.assertEqual(len(stop_resume.stop_unit_workers(parent, staff.StaffSnapshot(rows))), 3)
            self.assertEqual(self.kill.call_count, 6)
            self.assertEqual(len(elapsed), 3)
            for duration in elapsed:
                self.assertAlmostEqual(duration, 10 + stop_grace + kill_grace)

    def test_unknown_stop_reason_prints_once_through_each_caller(self) -> None:
        parent = staff.StaffId("holder")
        row = staff.StaffRow(staff.StaffId("fixed-worker"), "worker", "impl", staff.ReportsTo(parent),
                             current_session=staff.StaffId("native-worker"))
        for batch in (False, True):
            with (self.subTest(batch=batch),
                  patch.object(staff, "row_operation", return_value=nullcontext()),
                  patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
                  patch.object(self.client, "panes", return_value=[]),
                  patch.object(stop_resume, "_record_launches"),
                  patch.object(worker_panes, "observe", return_value=[worker_panes.OpenWorker(row, "pane", "terminal", herdr.AgentStatus.WORKING)]),
                  patch.object(herdr, "read_conversation", return_value=herdr.UnknownConversation("native evidence unavailable")),
                  redirect_stderr(io.StringIO()) as errors):
                if batch:
                    self.assertEqual(stop_resume.stop_unit_workers(parent, staff.StaffSnapshot((row,))), ["impl: not proved stopped"])
                else:
                    self.assertFalse(restart.stop_row(row, self.client))
            self.assertEqual(errors.getvalue().splitlines(), ["impl: restart anomaly: native evidence unavailable"])

    def test_stop_uses_the_runtime_from_an_ended_launch_with_a_fixed_staff_id(self) -> None:
        identity = self.entry["session_id"]
        row = staff.StaffRow(staff.StaffId("fixed-agent"), "producer", "alpha", staff.StaffRoot(),
                             current_session=staff.StaffId(identity))
        for kind in ("claude", "codex"):
            entry = self.entry.copy()
            entry.update(session_id=str(row.id), kind=kind, ended=True)
            pane_record.write("agents", entry)
            unknown = ("accounts (Codex thread unknown)",) if kind == "claude" else ()
            with (self.subTest(kind=kind),
                  patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
                  patch.object(runtime_sessions, "session_liveness", return_value=runtime_sessions.SessionNotLive()),
                  patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({}, unknown)),
                  patch.object(herdr, "read_conversation", wraps=herdr.read_conversation) as read):
                self.assertEqual(stop_resume.stop(row.id), stop_resume.NotRunning(identity))
                read.assert_called_once_with(identity, kind)
        self.kill.assert_not_called()

    def test_stop_without_a_publication_still_finds_a_live_codex_session(self) -> None:
        identity = self.entry["session_id"]
        self.process.side_effect = self.alive_until_kill
        with (patch.object(pane_record, "publication_for_session", return_value=None),
              patch.object(runtime_sessions, "session_liveness", return_value=runtime_sessions.SessionNotLive()),
              patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({identity: (123,)}, ("unrelated unknown",))),
              patch.object(herdr, "read_conversation", wraps=herdr.read_conversation) as read):
            self.assertEqual(stop_resume.stop(identity), stop_resume.Stopped(identity, 123))
        read.assert_called_once_with(identity, "")

    def test_launch_read_error_reports_once_and_reads_both_runtimes(self) -> None:
        identity = self.entry["session_id"]
        with (patch.object(pane_record, "publication_for_session", side_effect=OSError("record unreadable")),
              patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()) as read,
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(stop_resume.stop(identity), stop_resume.NotRunning(identity))
        read.assert_called_once_with(identity, "")
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn("record unreadable", errors.getvalue())

    def test_unusable_shutdown_values_send_no_signal_or_start(self) -> None:
        config = self.root / "config"
        for key in ("STOP_GRACE_SECONDS", "KILL_GRACE_SECONDS", "START_SECONDS", "STAY_SECONDS"):
            values = {name: "1" for name in ("STOP_GRACE_SECONDS", "KILL_GRACE_SECONDS", "START_SECONDS", "STAY_SECONDS")}
            values[key] = "0"
            atomic_file.write_text_atomic(config / "shutdown.conf", "\n".join(f"# range: 5e-324..\n{name}={value}" for name, value in values.items()))
            with (self.subTest(key=key), patch.dict(os.environ, {"CONF_DIR": str(config)}),
                  patch.object(stop_resume, "_signal_process") as send,
                  patch.object(stop_resume, "_start_session") as start):
                result = stop_resume.stop(self.entry["session_id"]) if "GRACE" in key else stop_resume.resume(self.entry["session_id"])
            self.assertIsInstance(result, (stop_resume.StopFailed, stop_resume.ResumeFailed))
            self.assertIn(key, str(result))
            self.assertIn(str(config / "shutdown.conf"), str(result))
            send.assert_not_called()
            start.assert_not_called()

    def test_tab_lookup_keeps_the_conversation_tab_when_staff_is_unreadable(self) -> None:
        identity = self.entry["session_id"]
        for error in (OSError("staff unreadable"), ValueError("staff invalid")):
            with (self.subTest(error=error),
                  patch.object(staff, "initial_snapshot", side_effect=error),
                  redirect_stderr(io.StringIO()) as output):
                tab = stop_resume._tab_of(identity)  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(tab, herdr.TabSession("w1:p2", "alpha", "w1", identity))
            self.assertEqual(output.getvalue().splitlines(), [f"launch names: staff is unavailable: {error}; saved names retained"])

    def test_up_lines_name_the_agent_from_the_account(self) -> None:
        for label, agent in (("codex1", "Codex"), ("claude1", "Claude")):
            with self.subTest(account=label):
                started = stop_results.ClaudeUp("worker", "w1:p2", label, elapsed_seconds=2.5)
                running = stop_results.ClaudeUp("worker", "w1:p2", label,
                                              start_state=ClaudeStartState.ALREADY_RUNNING)
                self.assertEqual(stop_results.claude_up_line(started),
                                 f"worker: {agent} is up on {label} in w1:p2 (2.5 s)")
                self.assertEqual(stop_results.claude_up_line(running),
                                 f"worker: unchanged; {agent} runs on {label} in w1:p2")


    def test_unnumbered_accounts_name_no_agent_in_either_result_state(self) -> None:
        for label in ("default", "unknown account", "~/.codex"):
            for state, text in ((ClaudeStartState.STARTED, f"up on {label} in w1:p2 (2.5 s)"),
                                (ClaudeStartState.ALREADY_RUNNING, f"unchanged; runs on {label} in w1:p2")):
                with self.subTest(account=label, state=state):
                    result = stop_results.ClaudeUp("worker", "w1:p2", label, elapsed_seconds=2.5, start_state=state)
                    self.assertEqual(stop_results.claude_up_line(result), f"worker: {text}")

    def test_resume_uses_the_group_account_unless_the_target_is_explicit(self) -> None:
        identity = staff.StaffId(self.entry["session_id"])
        for kind in ("unit", "showrunner", "producer", "missing"):
            for target in (None, "claude3"):
                with self.subTest(kind=kind, target=target):
                    pane_record.save("agents", {self.entry["pane_id"]: self.entry})
                    row = staff.StaffRow(identity, "producer" if kind == "missing" else kind, "alpha",
                                         staff.STAFF_ROOT, current_session=identity)
                    expected = target or "claude4"
                    rows = () if kind == "missing" else (row,)
                    with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot(rows)),
                          patch.object(accounts, "role_account", return_value=accounts.NamedAccount(
                              "claude4", self.root / ".claude4")) as selection,
                          patch.object(stop_resume, "_start_session", return_value=sessions.LiveSessionProcess(456)) as start):
                        result = stop_resume.resume(str(identity), to=target)
                    self.assertIsInstance(result, stop_resume.ClaudeUp)
                    assert isinstance(result, stop_resume.ClaudeUp)
                    self.assertEqual(result.account, expected)
                    self.assertEqual(start.call_args.args[2], expected)
                    entry = cast(pane_record.PaneLaunch, start.call_args.args[1])
                    self.assertEqual(entry["environment"]["CLAUDE_CONFIG_DIR"], str(self.root / f".{expected}"))
                    if kind == "missing":
                        self.assertEqual(str(cast(staff.SessionHome, pane_launch.config_home(entry)).path), str(self.root / f".{expected}"))
                    else:
                        self.assertNotIn("config_dir", entry)
                    if target is None:
                        selection.assert_called_once_with(kind if kind in ("unit", "showrunner") else "unit")
                    else:
                        selection.assert_not_called()

    def test_resume_without_a_staff_row_on_the_same_account_reports_no_move(self) -> None:
        result = stop_resume.resume(self.entry["session_id"], why="maintenance")
        self.assertEqual(result, stop_resume.ClaudeUp(self.entry["session_id"], "w1:p2", "claude3"))
        self.assertEqual(len(self.client.prompts), 1)
        text = self.client.prompts[0][1].text
        self.assertIn("You were stopped and resumed because maintenance.", text)
        self.assertNotIn("You were moved", text)
        self.start.assert_called_once()

    def saved_codex_name(self, name: str = "saved-worker") -> str:
        identity = "019b3a40-4250-7000-8000-000000000103"
        home = self.root / ".codex2"
        atomic_file.write_text_atomic(home / "session_index.jsonl", json.dumps({
            "id": identity, "thread_name": name, "updated_at": "2026-11-06T00:00:00Z"}) + "\n")
        atomic_file.write_text_atomic(home / "sessions" / f"rollout-{identity}.jsonl", json.dumps({
            "type": "session_meta", "payload": {"id": identity, "cwd": str(self.root / "project")}}) + "\n")
        for label in ("codex1", "codex2"):
            with closing(sqlite3.connect(":memory:")) as database:
                _ = database.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
                if label == "codex2":
                    _ = database.execute("INSERT INTO threads VALUES (?)", (identity,))
                database.commit()
                atomic_file.write_bytes_atomic(self.root / f".{label}" / "state_5.sqlite", database.serialize())
        pane_record.save("agents", {})
        self.client.rows.clear()
        return identity

    def test_resume_resolves_an_exact_codex_name_and_reports_distinct_candidates(self) -> None:
        identity = self.saved_codex_name()
        path = self.root / ".codex2/session_index.jsonl"
        first = path.read_text()
        with (patch.dict(os.environ, {"CODEX_HOME": str(path.parent)}),
              patch.object(stop_resume, "_resume_once", return_value=stop_resume.ClaudeUp(identity, "pane", "codex1")) as once):
            self.assertIsInstance(stop_resume.resume("saved-worker"), stop_resume.ClaudeUp)
            self.assertEqual(once.call_args.args[0], identity)
            atomic_file.write_text_atomic(path, first + first)
            self.assertIsInstance(stop_resume.resume("saved-worker"), stop_resume.ClaudeUp)
            atomic_file.write_text_atomic(path, first + json.dumps({"id": "another-id", "thread_name": "saved-worker"}) + "\n")
            once.reset_mock()
            result = stop_resume.resume("saved-worker")
            self.assertIsInstance(result, stop_resume.ResumeFailed)
            assert isinstance(result, stop_resume.ResumeFailed)
            self.assertIn("ambiguous name; candidates:", result.reason)
            self.assertIn(identity, result.reason)
            self.assertIn("another-id", result.reason)
            once.assert_not_called()
            once.return_value = stop_resume.SessionNotFound("saved")
            self.assertEqual(stop_resume.resume("SAVED-WORKER"), stop_resume.ResumeFailed("SAVED-WORKER", "unknown name"))

    def test_raw_codex_id_wins_over_another_threads_name(self) -> None:
        identity = self.saved_codex_name()
        path = self.root / ".codex2/session_index.jsonl"
        write = json.dumps({"id": "another-id", "thread_name": identity}) + "\n"
        atomic_file.write_text_atomic(path, path.read_text() + write)
        with (patch.dict(os.environ, {"CODEX_HOME": str(path.parent)}),
              patch.object(stop_resume, "_resume_once", return_value=stop_resume.SessionNotFound(identity)) as once):
            _ = stop_resume.resume(identity)
        self.assertEqual(once.call_args.args[0], identity)

    def test_claude_uuid_is_not_replaced_by_a_codex_thread_name(self) -> None:
        identity = self.entry["session_id"]
        other = self.saved_codex_name(identity)
        self.assertNotEqual(identity, other)
        with (patch.dict(os.environ, {"CODEX_HOME": str(self.root / ".codex2")}),
              patch.object(stop_resume, "_resume_once", return_value=stop_resume.SessionNotFound(identity)) as once):
            _ = stop_resume.resume(identity)
        self.assertEqual(once.call_args.args[0], identity)
        with patch.object(agent_accounts, "codex_home", side_effect=AssertionError("an id needs no index")):
            self.assertEqual(stop_resume._codex_name_ids(identity), [identity])  # pyright: ignore[reportPrivateUsage]

    def test_uuid_does_not_match_another_conversations_pane_label(self) -> None:
        identity = self.entry["session_id"]
        pane_record.save("agents", {})
        self.client.rows[0].update(label=identity, agent_session={"kind": "id", "value": "another-conversation"})
        tab = stop_resume._tab_of(identity, names=LaunchNames(staff.StaffSnapshot(())))  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(tab, stop_resume.SessionNotFound(identity))

    def resume_codex_staff(self, observed: herdr.LiveConversation | herdr.UnknownConversation) -> tuple[stop_resume.ResumeResult, Mock, Mock, Mock, str]:
        identity = self.entry["session_id"]
        row = staff.StaffRow(staff.StaffId("codex-holder"), "producer", "alpha", staff.STAFF_ROOT,
                             current_session=staff.StaffId(identity), home=staff.SessionHome(self.root / ".codex2"))
        self.entry["kind"] = "codex"
        self.entry["config_dir"] = str(self.root / ".codex2")
        self.entry["environment"] = {"CODEX_HOME": str(cast(staff.SessionHome, pane_launch.config_home(self.entry)).path)}
        pane_record.save("agents", {"w1:p2": self.entry})
        def record_home(identity: staff.StaffId, home: staff.SessionHome) -> staff.StaffRow:
            nonlocal row
            self.assertEqual(identity, row.id)
            row = replace(row, home=home)
            return row
        with (patch.object(staff, "initial_snapshot", side_effect=lambda: staff.StaffSnapshot((row,))),
              patch.object(staff, "local_snapshot", side_effect=lambda: staff.StaffSnapshot((row,))),
              patch.object(staff, "set_home", side_effect=record_home),
              patch.object(accounts, "role_account", return_value=accounts.CodexWorkersAccount(
                  "codex1", self.root / ".codex1")),
              patch.object(agent_accounts, "codex_home_role_label", return_value="codex7") as source_label,
              patch.object(herdr, "read_conversation", side_effect=[herdr.StoppedConversation(), observed]),
              patch.object(account, "process_codex_home", return_value=account.ProcessCodexHome(self.root / ".codex1")),
              patch.object(accounts, "codex_home_label", return_value="codex1"),
              patch.object(herdr, "prepare_session_environment", return_value=herdr.PreparedAgentEnvironment(
                  self.root, {"CODEX_HOME": str(self.root / ".codex1")})),
              patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                  "w1:p2", herdr.PromptSubmissionNotRequired())) as launch,
              patch.object(self.client, "prompt") as prompt,
              redirect_stderr(io.StringIO()) as errors):
            result = stop_resume.resume("alpha", to="codex1")
        return result, launch, prompt, source_label, errors.getvalue()

    def test_codex_staff_resume_uses_native_prompt_delivery(self) -> None:
        result, launch, prompt, _label, _errors = self.resume_codex_staff(herdr.LiveConversation("codex", (456,)))
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertIsInstance(cast(object, launch.call_args.args[5]), herdr.NoOpeningPrompt)
        prompt.assert_called_once()
        self.assertEqual(prompt.call_args.kwargs["target"], self.entry["session_id"])

    def test_codex_resume_message_uses_the_source_login_label(self) -> None:
        _result, _launch, prompt, label, _errors = self.resume_codex_staff(herdr.LiveConversation("codex", (456,)))
        label.assert_called_once_with(self.root / ".codex2")
        self.assertIn("codex7", cast(str, prompt.call_args.args[1]))

    def test_codex_resume_observes_the_started_process_generation(self) -> None:
        _result, _launch, _prompt, _label, errors = self.resume_codex_staff(herdr.LiveConversation("codex", (456,)))
        self.detach.assert_called_once()
        arguments = cast(list[str], self.detach.call_args.args[0])
        self.assertEqual(arguments[arguments.index("--pid") + 1], "456")
        self.assertEqual(arguments[arguments.index("--generation") + 1], "generation-1")
        self.assertEqual(errors, "")

    def test_codex_resume_reports_one_anomaly_when_the_started_pid_is_unreadable(self) -> None:
        result, _launch, _prompt, _label, errors = self.resume_codex_staff(herdr.UnknownConversation("pid unreadable"))
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.detach.assert_not_called()
        self.assertEqual(len(errors.splitlines()), 2)
        self.assertIn("anomaly: pid unreadable; not blocking", errors)
        self.assertIn("observer anomaly:", errors)

    def test_resume_codex_name_uses_the_workers_account_and_the_rollout_folder(self) -> None:
        identity = self.saved_codex_name()
        source = self.root / ".codex2"
        for target in (None, "codex3"):
            expected = target or "codex1"
            with (self.subTest(target=target), patch.dict(os.environ, {"CODEX_HOME": str(source)}),
                  patch.object(accounts, "role_account", return_value=accounts.CodexWorkersAccount(
                      "codex1", self.root / ".codex1")) as role,
                  patch.object(self.client, "open_tab", return_value="w1:p2"),
                  patch.object(self.client, "prompt") as prompt,
                  patch.object(self.client, "tab_sessions", return_value=[herdr.TabSession("w1:p2", "saved-worker", "w1", "")]),
                  patch.object(herdr, "prepare_session_environment", return_value=herdr.PreparedAgentEnvironment(
                      self.root / "project", {"CODEX_HOME": str(self.root / f".{expected}")})) as prepare,
                  patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                      "w1:p2", herdr.PromptSubmissionNotRequired())) as launch):
                result = stop_resume.resume("saved-worker", to=target)
                self.assertIsInstance(result, stop_resume.ClaudeUp)
                assert isinstance(result, stop_resume.ClaudeUp)
                self.assertEqual((result.session_id, result.account), (identity, expected))
                self.assertEqual(prepare.call_args.args[1], str(self.root / "project"))
                self.assertEqual(prepare.call_args.args[2]["CODEX_HOME"], str(self.root / f".{expected}"))
                self.assertEqual(launch.call_args.args[3], "codex")
                self.assertIsInstance(cast(object, launch.call_args.args[5]), herdr.NoOpeningPrompt)
                prompt.assert_not_called()
                self.assertEqual(launch.call_args.args[4][-2:], ["resume", identity])
                if target is None:
                    role.assert_called_once_with("workers")
                else:
                    role.assert_not_called()

    def test_unknown_codex_process_does_not_block_a_stopped_thread_without_a_pane(self) -> None:
        identity = self.saved_codex_name()
        unknown = herdr.CodexConversations({}, ("other-session (Codex thread unknown)",))
        with (patch.dict(os.environ, {"CODEX_HOME": str(self.root / ".codex2")}),
              patch.object(herdr, "codex_conversations", side_effect=[unknown, herdr.CodexConversations({identity: (456,)}, ())]),
              patch.object(accounts, "role_account", return_value=accounts.CodexWorkersAccount("codex1", self.root / ".codex1")),
              patch.object(account, "process_codex_home", return_value=account.ProcessCodexHome(self.root / ".codex1")),
              patch.object(accounts, "codex_home_label", return_value="codex1"),
              patch.object(self.client, "open_tab", return_value="w1:p2") as opened,
              patch.object(self.client, "tab_sessions", return_value=[herdr.TabSession("w1:p2", "saved-worker", "w1", "")]),
              patch.object(herdr, "prepare_session_environment", return_value=herdr.PreparedAgentEnvironment(
                  self.root / "project", {"CODEX_HOME": str(self.root / ".codex1")})),
              patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                  "w1:p2", herdr.PromptSubmissionNotRequired())) as launch,
              redirect_stderr(io.StringIO()) as errors):
            result = stop_resume.resume("saved-worker")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        assert isinstance(result, stop_resume.ClaudeUp)
        self.assertEqual(result.start_state, ClaudeStartState.STARTED)
        opened.assert_called_once()
        launch.assert_called_once()
        self.assertEqual(sum(line.startswith("anomaly:") for line in errors.getvalue().splitlines()), 1)
        self.assertIn("other-session (Codex thread unknown)", errors.getvalue())
        self.assertIn("not blocking", errors.getvalue())

    def test_unknown_codex_thread_in_a_running_pane_does_not_start_again(self) -> None:
        identity = self.saved_codex_name()
        labels: tuple[dict[str, str | None], ...] = ({"label": "saved-worker"}, {}, {"label": None})
        for label in labels:
            with self.subTest(label=label):
                self.client.rows[:] = [{"pane_id": "w1:p2", "workspace_id": "w1",
                                        "terminal_id": "worker-terminal", "session_id": identity, **label}]
                with (patch.dict(os.environ, {"CODEX_HOME": str(self.root / ".codex2")}),
                      patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations(
                          {}, ("other-session (Codex thread unknown)",))),
                      patch.object(self.client, "pane_activity", return_value=herdr.PaneRunning(("codex",))) as activity,
                      patch.object(self.client, "open_tab") as opened,
                      patch.object(herdr, "launch_prepared_agent") as launch,
                      redirect_stderr(io.StringIO()) as errors):
                    result = stop_resume.resume("saved-worker", to="codex1")
                self.assertIsInstance(result, stop_resume.ClaudeUp)
                assert isinstance(result, stop_resume.ClaudeUp)
                self.assertEqual(result.start_state, ClaudeStartState.ALREADY_RUNNING)
                self.assertEqual((result.session_id, result.pane_id), (identity, "w1:p2"))
                activity.assert_called_once_with("w1:p2")
                opened.assert_not_called()
                launch.assert_not_called()
                self.detach.assert_not_called()
                self.assertEqual(sum(line.startswith("anomaly:") for line in errors.getvalue().splitlines()), 1)
                self.assertIn("other-session (Codex thread unknown)", errors.getvalue())

    def test_resume_of_a_running_codex_name_keeps_its_current_account(self) -> None:
        identity = self.saved_codex_name()
        with (patch.dict(os.environ, {"CODEX_HOME": str(self.root / ".codex2")}),
              patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({identity: (456,)}, ())),
              patch.object(account, "process_codex_home", return_value=account.ProcessCodexHome(self.root / ".codex2")),
              patch.object(accounts, "codex_home_label", return_value="codex2"),
              patch.object(herdr, "launch_prepared_agent") as launch):
            result = stop_resume.resume("saved-worker", to="codex1")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        assert isinstance(result, stop_resume.ClaudeUp)
        self.assertEqual(result.start_state, ClaudeStartState.ALREADY_RUNNING)
        self.assertEqual(result.account, "codex2")
        launch.assert_not_called()

    def test_closed_showrunner_uses_its_role_account_before_its_first_launch(self) -> None:
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(identity, "showrunner", "alpha", staff.STAFF_ROOT, current_session=identity)
        self.client.rows.clear()
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(accounts, "role_account", return_value=accounts.NamedAccount("claude4", self.root / ".claude4")) as role,
              patch.object(self.client, "open_tab", return_value="w1:p3"),
              patch.object(self.client, "tab_sessions", return_value=[herdr.TabSession("w1:p3", "alpha", "w1", "")]),
              patch.object(stop_resume, "_start_session", return_value=sessions.LiveSessionProcess(456)) as start):
            result = stop_resume.resume("alpha")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        role.assert_called_once_with("showrunner")
        self.assertEqual(start.call_args.args[1]["environment"]["CLAUDE_CONFIG_DIR"], str(self.root / ".claude4"))

    def test_moved_showrunner_resumes_only_its_worker_children_with_native_dispatch_ids(self) -> None:
        fixture = StaffTestCase()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        pane_record.write("agents", self.entry)
        doc = fixture.root / "production.md"
        atomic_file.write_text_atomic(doc, f"# Production\n\n- **Merge branch:** `merge`\n"
            + f"- **Showrunner checkout:** `{fixture.root}`\n- **Log:** `log.md`\n\n## Units\n")
        owner = fixture_member("showrunner", name="owner", runtime_id=self.entry["session_id"],
                               doc=staff.LiveDoc(remote.local_machine(), doc))
        _ = staff.set_home(owner.id, staff.SessionHome(self.root / ".claude3"))
        unit = fixture_member("unit", name="unit", parent=owner.id)
        foreign = fixture_worker("foreign", unit.id)
        codex = fixture_worker("codex-own", owner.id, backend=staff.CodexBackend(fixture.root / "codex"))
        claude = fixture_worker("claude-own", owner.id)
        runs = fixture.root / "runs"
        run = runs / "unit-run"
        run.mkdir(parents=True)
        for child in (codex, claude):
            agents_log.append(child.current_session, "impl", None, datetime.now(timezone.utc))
        fixture.write_json(run / "progress_history_state.json", {
            "owner_staff_id": owner.id, "pass": {child.name: {
                "worker_name": child.name, "status": "active", "instance_id": f"dispatch-{child.name}",
                "launcher_command": ["assign.sh", "--to", child.name],
            } for child in (codex, claude, foreign)}})
        for child in (codex, claude):
            backend = staff.backend(child)
            assert isinstance(backend, (staff.ClaudeBackend, staff.CodexBackend))
            text = f"End your final reply with DONE dispatch-{child.name}."
            event = ({"type": "user", "message": {"role": "user", "content": text}}
                     if isinstance(backend, staff.ClaudeBackend) else
                     {"type": "response_item", "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": text}]}})
            atomic_file.write_text_atomic(worker_admission.transcript_source(child, backend), json.dumps(event) + "\n")
        with (patch("scripts.lib.unit_runs.run_roots", return_value=(runs,)),
              patch.object(unit_runs, "held", return_value=[str(run)]),
              patch.dict(os.environ, {"UNIT_RUN_DIR": str(run)}),
              patch.object(staff, "initial_snapshot", return_value=staff.local_snapshot()),
              patch.object(stop_resume, "_start_session", return_value=sessions.LiveSessionProcess(456)) as start):
            _ = staff.set_run(owner.id, staff.OpenRun(run))
            self.assertEqual(unit_runs.director(run), owner.current_session)
            result = stop_resume.resume(owner.current_session, why="account move", to="claude4")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        message = cast(str, start.call_args.args[3])
        self.assertNotIn(f"Worker {foreign.id}:", message)
        self.assertNotIn(f"dispatch-{foreign.name}", message)
        for child in (codex, claude):
            self.assertEqual(message.count(f"Worker {child.id}: busy dispatch dispatch-{child.name}."), 1)
        self.assertIn("moved from claude3 to claude4", message)
        self.assertNotIn("assign.sh --to", message)

    def test_resume_without_a_pane_record_starts_the_staff_row(self) -> None:
        identity = self.entry["session_id"]
        row = staff.StaffRow(staff.StaffId("fixed-row"), "unit", "alpha", staff.StaffRoot(),
                             current_session=staff.StaffId(identity))
        record = staff.StaffSnapshot((row,))
        pane_record.save("agents", {})
        for target in (None, "claude4"):
            for panes in (self.client.rows, []):
                with (self.subTest(target=target, panes=bool(panes)),
                      patch.object(staff, "initial_snapshot", return_value=record),
                      patch.object(self.client, "rows", panes),
                      patch.object(restart, "start_row", return_value="w1:p2") as start,
                      patch.object(stop_resume, "running_on_account", side_effect=[
                          sessions.SessionNotLive(), stop_resume.ClaudeUp(identity, "w1:p2", target or "claude3")])):
                    result = stop_resume.resume(str(row.id), to=target)
                self.assertEqual(result, stop_resume.ClaudeUp(identity, "w1:p2", target or "claude3"))
                assert isinstance(result, stop_resume.ClaudeUp)
                self.assertEqual(result.start_state, ClaudeStartState.STARTED)
                start.assert_called_once_with(row, record, self.client, target_account=target or "")

    def test_resume_without_a_staff_row_or_pane_record_finds_no_session(self) -> None:
        identity = self.entry["session_id"]
        pane_record.save("agents", {})
        result = stop_resume._resume_once(identity, why="test", names=LaunchNames(staff.StaffSnapshot(())))  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(result, stop_resume.SessionNotFound(identity))

    def test_admitted_pane_run_with_no_start_fails_after_one_attempt(self) -> None:
        with patch.object(self.client, "run", self.start):
            result = stop_resume.resume("alpha", why="maintenance")
        self.assertEqual(result, stop_resume.ResumeFailed("019b3a40-4250-7000-8000-000000000102", "never started"))
        self.assertEqual(self.start.call_count, 1)
        self.assertEqual(self.clock.elapsed, 20.0)
        self.assertEqual({invocation.args[2] for invocation in self.start.call_args_list}, {"w1:p2"})


    def test_batch_without_a_tab_reaches_the_shared_restore(self) -> None:
        up = stop_resume.ClaudeUp("saved", "new-pane", "claude4")
        with (patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning("saved")),
              patch.object(stop_resume, "resume", return_value=up) as resumed):
            results = stop_resume.move_sessions(["saved"], "claude4", why="limit")
        self.assertEqual(results, [up])
        resumed.assert_called_once_with("saved", why="limit", to="claude4")


    def test_stale_launch_metadata_does_not_prevent_resume(self) -> None:
        identity = self.entry["session_id"]
        self.entry.update(session_id="sid-other", flags_read=False, ended=True)
        pane_record.write("agents", self.entry)
        tab = herdr.TabSession("w1:p2", "alpha", "w1", identity)
        with (patch.object(stop_resume, "_tab_of", return_value=tab), redirect_stderr(io.StringIO()) as output):
            result = stop_resume.resume(identity)
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertIn("launch record anomaly:", output.getvalue())
        self.start.assert_called_once()
        self.assertIn(identity, self.launched)


    def test_failed_stop_bookkeeping_reports_an_anomaly_and_keeps_the_stopped_result(self) -> None:
        result = stop_resume.Stopped(self.entry["session_id"], 123)
        with (patch.object(stop_resume, "_stop", return_value=result),
              patch.object(pane_record, "end", side_effect=OSError("fixture write failed")),
              redirect_stderr(io.StringIO()) as output):
            self.assertEqual(stop_resume.stop("alpha"), result)
        self.assertEqual(output.getvalue().splitlines(),
                         [f"{result.session_id}: stop note anomaly: fixture write failed"])


    def test_staff_home_changes_only_after_the_target_pane_starts(self) -> None:
        row = staff.ensure("producer", name="TEST-account-home", runtime_id=self.entry["session_id"],
            worktree=staff.PresentWorktree(remote.local_machine(), self.root))
        row = pane_launch.staff_admitted(row)
        running_home = self.root / ".claude3"
        target_home = self.root / ".claude4"
        self.assertEqual(row.home, staff.SessionHome(running_home))
        starts: list[str] = []

        def started(tab: herdr.TabSession, entry: pane_record.PaneLaunch, label: str,
                    _message: str) -> sessions.LiveSessionProcess:
            current = staff.by_id(row.id)
            assert current is not None
            self.assertEqual(current.home, staff.SessionHome(running_home))
            self.assertNotIn("config_dir", entry)
            self.assertNotIn("config_dir", pane_record.read("agents")[tab.pane_id])
            self.assertEqual(entry["environment"]["CLAUDE_CONFIG_DIR"], str(target_home))
            replacement = pane_record.launch(tab.pane_id, entry["session_id"], entry["cwd"], entry["environment"],
                                            ["claude", *pane_record.resume_arguments(entry)])
            pane_record.write("agents", replacement)
            starts.append(label)
            return sessions.LiveSessionProcess(456)

        with patch.object(stop_resume, "_start_session", side_effect=started):
            result = stop_resume.resume(row.id, to="claude4")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertEqual(starts, ["claude4"])
        current = staff.by_id(row.id)
        assert current is not None
        self.assertEqual(current.home, staff.SessionHome(target_home))
        saved = pane_record.read("agents")["w1:p2"]
        self.assertNotIn("config_dir", saved)
        self.assertNotIn("CLAUDE_CONFIG_DIR", saved["environment"])

    def test_account_change_keeps_pane_data_until_the_start_publication(self) -> None:
        identity = self.entry["session_id"]
        row = staff.StaffRow(staff.StaffId("fixed-alpha"), "producer", "alpha", staff.STAFF_ROOT,
                             current_session=staff.StaffId(identity), home=staff.SessionHome(self.root / ".claude3"))
        self.entry["session_id"] = row.id
        pane_record.save("agents", {"w1:p2": self.entry})
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot((row,)))):
            result = stop_resume.resume("alpha", to="claude4")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        saved = pane_record.read("agents")["w1:p2"]
        self.assertEqual(saved, self.entry)
        self.assertEqual(saved["session_id"], row.id)
        self.assertNotIn("CLAUDE_CONFIG_DIR", saved["environment"])
        command = self._typed(cast(tuple[str, ...], self.start.call_args.args))
        self.assertIn("CLAUDE_CODE_SESSION_ID=" + identity, command)

    def test_own_pane_resume_saves_pane_data_with_the_fixed_staff_reference(self) -> None:
        identity = self.entry["session_id"]
        row = staff.StaffRow(staff.StaffId("fixed-alpha"), "producer", "alpha", staff.STAFF_ROOT,
                             current_session=staff.StaffId(identity))
        self.entry["session_id"] = row.id
        pane_record.save("agents", {"w1:p2": self.entry})
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2"}),
              patch.object(stop_resume, "_exec_in_own_pane", side_effect=SystemExit) as execute):
            with self.assertRaises(SystemExit):
                _ = stop_resume.resume("alpha")
        self.assertEqual(pane_record.read("agents")["w1:p2"], self.entry)
        launched = cast(pane_launch.PaneLaunch, execute.call_args.args[1])
        self.assertEqual(launched["session_id"], identity)
        self.assertEqual(launched["environment"]["CLAUDE_CODE_SESSION_ID"], identity)

    def test_own_pane_resume_by_old_tab_label_keeps_the_fixed_staff_reference(self) -> None:
        identity = self.entry["session_id"]
        row = staff.StaffRow(staff.StaffId("fixed-alpha"), "producer", "renamed-alpha", staff.STAFF_ROOT,
                             current_session=staff.StaffId(identity))
        self.entry["session_id"] = row.id
        pane_record.save("agents", {"w1:p2": self.entry})
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2"}),
              patch.object(stop_resume, "_exec_in_own_pane", side_effect=SystemExit) as execute):
            with self.assertRaises(SystemExit):
                _ = stop_resume.resume("alpha")
        self.assertEqual(pane_record.read("agents")["w1:p2"]["session_id"], row.id)
        execute.assert_called_once()
        launched = cast(pane_launch.PaneLaunch, execute.call_args.args[1])
        self.assertEqual(launched["session_id"], identity)
        self.assertEqual(launched["name"], row.name)

    def test_failed_account_bookkeeping_reports_an_anomaly_and_resumes(self) -> None:
        with (patch.object(pane_record, "write", side_effect=OSError("fixture")),
              redirect_stderr(io.StringIO()) as output):
            result = stop_resume.resume("alpha", to="claude4")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertIn("account record anomaly: fixture", output.getvalue())
        self.start.assert_called_once()


    def test_missing_prompt_confirmation_reports_an_anomaly_and_keeps_the_live_result(self) -> None:
        with (patch.object(self.client, "submit_opening_prompt", return_value=herdr.PromptNotSubmitted("fixture")),
              redirect_stderr(io.StringIO()) as output):
            result = stop_resume.resume("alpha")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertIn("prompt anomaly: fixture", output.getvalue())
        self.start.assert_called_once()


    def test_missing_stay_generation_reports_an_anomaly_and_keeps_the_live_result(self) -> None:
        with (patch.object(sessions, "process_start", return_value=None), redirect_stderr(io.StringIO()) as output):
            result = stop_resume.resume("alpha")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertIn("observer anomaly: process generation is unavailable", output.getvalue())
        self.start.assert_called_once()
        self.detach.assert_not_called()


    def test_failed_stay_launch_reports_an_anomaly_and_keeps_the_live_result(self) -> None:
        self.detach.side_effect = OSError("fixture")
        with redirect_stderr(io.StringIO()) as output:
            result = stop_resume.resume("alpha")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertIn("observer anomaly: fixture", output.getvalue())
        self.start.assert_called_once()


    def test_failed_title_change_reports_an_anomaly_and_resumes(self) -> None:
        with (patch.object(self.client, "retitle", side_effect=herdr_types.HerdrFailure("fixture")),
              redirect_stderr(io.StringIO()) as output):
            result = stop_resume.resume("alpha")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertIn("title anomaly: fixture", output.getvalue())
        self.start.assert_called_once()


    def test_enveloped_resume_launch_paths_need_no_source_record(self) -> None:
        from scripts.lib import script_prompts

        with patch.object(script_prompts, "record", side_effect=AssertionError("recorded resume")):
            result = stop_resume.resume("alpha")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertTrue(self.client.prompts[0][1].text.startswith('<system-reminder source="script">'))


    def test_resume_does_not_type_into_a_pane_with_other_work(self) -> None:
        self.client.process_info["foreground_process_group_id"] = 998043
        pane_result = self.client.result

        def observe(*arguments: str, deadline: float = math.inf) -> dict[str, object]:
            if arguments[:2] == ("pane", "process-info"):
                return {"process_info": self.client.process_info}
            return pane_result(*arguments, deadline=deadline)

        with (patch.object(self.client, "result", side_effect=observe),
              patch.object(time, "monotonic", lambda: self.clock.elapsed)):
            result = stop_resume.resume("alpha")
        self.assertEqual(result, stop_resume.ResumeFailed(
            self.entry["session_id"], "would send keys to a pane with other live work instead of the selected shell"))
        self.assertEqual(self.clock.elapsed, herdr.PROGRAM_TIMEOUT_SECONDS)
        self.start.assert_not_called()
        self.assertEqual(self.client.prompts, [])


    def test_resume_names_use_a_fresh_snapshot_under_the_row_lock(self) -> None:
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(staff.StaffId("accounts"), "unit", "accounts", staff.StaffRoot(),
                             current_session=identity)
        for reference in (row.name, row.id, identity):
            self.launched.clear()
            self.client.prompts.clear()
            with patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))) as read:
                result = stop_resume.resume(reference)
            self.assertIsInstance(result, stop_resume.ClaudeUp)
            self.assertEqual(read.call_count, 2)
            self.assertIn("resumed by name", self.client.prompts[0][1].text)
            self.assertTrue(self.client.prompts[0][1].text.startswith('<system-reminder source="script">'))
            self.assertNotIn("the herdr server restarted", self.client.prompts[0][1].text)

    def test_unknown_ambiguous_and_remote_names_refuse(self) -> None:
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(staff.StaffId("accounts"), "unit", "accounts", staff.StaffRoot(),
                             current_session=identity)
        other = staff.StaffRow(staff.StaffId("other"), "unit", "accounts", staff.StaffRoot(),
                               current_session=staff.StaffId("other-session"))
        with patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row, other))):
            for reference, text in (("absent", "unknown name"), ("accounts", "ambiguous name")):
                result = stop_resume.resume(reference)
                assert isinstance(result, stop_resume.ResumeFailed)
                self.assertIn(text, result.reason)
                if reference == "accounts":
                    self.assertIn("accounts", result.reason)
                    self.assertIn("other", result.reason)
                else:
                    self.assertEqual(result.reason, "unknown name")
        remote_row = staff.StaffRow(row.id, row.kind, row.name, row.parent,
            current_session=identity, worktree=staff.PresentWorktree("other-machine", self.root))
        with patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((remote_row,))):
            result = stop_resume.resume("accounts")
        self.assertEqual(result, stop_resume.ResumeFailed("accounts", "session is on other-machine"))
        self.start.assert_not_called()
        self.kill.assert_not_called()

    def test_codex_record_uses_workers_home_without_claude_trust(self) -> None:
        identity = self.entry["session_id"]
        row = staff.StaffRow(staff.StaffId("saved-codex-owner"), "producer", "alpha", staff.STAFF_ROOT,
                             current_session=staff.StaffId(identity))
        home = self.root / ".codex2"
        workers = self.root / ".codex1"
        self.entry.update(kind="codex", config_dir=str(home), model="")
        self.entry["environment"] = {"CLAUDE_CONFIG_DIR": str(home)}
        pane_record.write("agents", self.entry)
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(accounts, "role_account", return_value=accounts.CodexWorkersAccount("codex1", workers)) as role,
              patch.object(agent_accounts, "codex_home_role_label", return_value="codex2"),
              patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(stop_resume, "heal_account_trust") as trust,
              patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2"}),
              patch.object(stop_resume, "_exec_in_own_pane", side_effect=SystemExit) as execute):
            with self.assertRaises(SystemExit):
                _ = stop_resume.resume("alpha")
        trust.assert_not_called()
        role.assert_called_once_with("workers")
        entry = cast(pane_record.PaneLaunch, execute.call_args.args[1])
        self.assertNotIn("config_dir", entry)
        self.assertEqual(entry["environment"]["CODEX_HOME"], str(workers))

    def test_codex_own_pane_exec_publishes_the_target_home(self) -> None:
        source, target = self.root / ".codex1", self.root / ".codex2"
        row = fixture_member("producer", name="own-codex", runtime_id=self.entry["session_id"])
        _ = staff.set_home(row.id, staff.SessionHome(source))
        entry = self.entry.copy()
        entry.update(kind="codex", config_dir=str(source), environment={"CODEX_HOME": str(target)})
        tab = herdr.TabSession("w1:p2", row.name, "w1", row.current_session)
        with (patch.object(os, "chdir"), patch.object(os, "execvpe", side_effect=SystemExit)):
            with self.assertRaises(SystemExit):
                stop_resume._exec_in_own_pane(tab, entry, "codex2", "")  # pyright: ignore[reportPrivateUsage]
        current = staff.by_id(row.id)
        assert current is not None
        self.assertEqual(current.home, staff.SessionHome(target))

    def test_codex_own_pane_exec_has_no_prompt_and_uses_workers_home(self) -> None:
        identity = self.entry["session_id"]
        row = staff.StaffRow(staff.StaffId("saved-codex-owner"), "producer", "alpha", staff.STAFF_ROOT,
                             current_session=staff.StaffId(identity))
        home = self.root / ".codex2"
        workers = self.root / ".codex1"
        self.entry.update(kind="codex", config_dir=str(home), model="")
        self.entry["environment"] = {"CLAUDE_CONFIG_DIR": str(home)}
        pane_record.write("agents", self.entry)
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(accounts, "role_account", return_value=accounts.CodexWorkersAccount("codex1", workers)) as role,
              patch.object(agent_accounts, "codex_home_role_label", return_value="codex2"),
              patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(stop_resume, "heal_account_trust") as trust,
              patch.object(stop_resume, "resume_message", return_value="Continue this conversation.") as message,
              patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2"}),
              patch.object(os, "chdir"), patch.object(os, "execvpe", side_effect=SystemExit) as execute):
            with self.assertRaises(SystemExit):
                _ = stop_resume.resume("alpha")
        trust.assert_not_called()
        role.assert_called_once_with("workers")
        message.assert_called_once()
        arguments = cast(list[str], execute.call_args.args[1])
        self.assertEqual(arguments[-2:], ["resume", identity])
        self.assertEqual(arguments[0], str(self.root / "bin" / "codex"))
        environment = cast(dict[str, str], execute.call_args.args[2])
        self.assertEqual(environment["CODEX_HOME"], str(workers))
        self.assertNotIn("Continue this conversation.", arguments)
        self.assertNotIn(str(stop_resume.SCRIPTS / "accounts/accounts.py"), arguments)

    def test_saved_codex_resume_uses_explicit_account(self) -> None:
        identity = self.entry["session_id"]
        row = staff.StaffRow(staff.StaffId("saved-codex-owner"), "producer", "alpha", staff.STAFF_ROOT,
                             current_session=staff.StaffId(identity))
        home = self.root / ".codex2"
        target = self.root / ".codex3"
        self.entry.update(kind="codex", config_dir=str(home), model="")
        self.entry["environment"] = {"CLAUDE_CONFIG_DIR": str(home)}
        pane_record.write("agents", self.entry)
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(accounts, "role_account") as role,
              patch.object(agent_accounts, "codex_home_role_label", return_value="codex2"),
              patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(stop_resume, "heal_account_trust") as trust,
              patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2"}),
              patch.object(stop_resume, "_exec_in_own_pane", side_effect=SystemExit) as execute):
            with self.assertRaises(SystemExit):
                _ = stop_resume.resume("alpha", to="codex3")
        role.assert_not_called()
        trust.assert_not_called()
        entry = cast(pane_record.PaneLaunch, execute.call_args.args[1])
        self.assertNotIn("config_dir", entry)
        self.assertEqual(entry["environment"]["CODEX_HOME"], str(target))

    def test_own_empty_pane_executes_the_saved_launch(self) -> None:
        self.entry["ended"] = True
        pane_record.write("agents", self.entry)
        with (patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2"}),
              patch.object(os, "chdir") as directory,
              patch.object(os, "execvpe", side_effect=SystemExit(0)) as execute):
            with self.assertRaises(SystemExit):
                _ = stop_resume.resume("alpha")
        directory.assert_called_once_with(str(self.root))
        arguments = cast(list[str], execute.call_args.args[1])
        self.assertEqual(arguments[0], str(self.root / "bin" / "claude"))
        self.assertIn("resumed by name", arguments[-1])
        environment = cast(dict[str, str], execute.call_args.args[2])
        self.assertEqual(environment["CLAUDE_CONFIG_DIR"], str(cast(staff.SessionHome, pane_launch.config_home(self.entry)).path))
        self.assertTrue(pane_record.read("agents")["w1:p2"].get("ended", False))
        self.start.assert_not_called()
        observed = pane_record.launch("w1:p2", self.entry["session_id"], str(self.root),
            self.entry["environment"], ["claude", "--resume", self.entry["session_id"]])
        pane_record.write("agents", observed)
        self.assertFalse(pane_record.read("agents")["w1:p2"].get("ended", False))

    def test_unknown_name_lists_only_close_local_units_and_showrunners(self) -> None:
        from scripts.agents import remote

        machine = remote.local_machine()
        rows = (
            staff.StaffRow(staff.StaffId("accounts"), "unit", "accounts", staff.StaffRoot(),
                current_session=staff.StaffId("accounts-session"),
                worktree=staff.PresentWorktree(machine, self.root)),
            staff.StaffRow(staff.StaffId("team-accounts"), "showrunner", "team-accounts", staff.StaffRoot(),
                current_session=staff.StaffId("team-session"), doc=staff.LiveDoc(machine, self.root / "plan.md")),
            staff.StaffRow(staff.StaffId("account-worker"), "worker", "accounts-worker", staff.StaffRoot(),
                current_session=staff.StaffId("worker-session"), worktree=staff.PresentWorktree(machine, self.root)),
            staff.StaffRow(staff.StaffId("remote-accounts"), "unit", "accounts-remote", staff.StaffRoot(),
                current_session=staff.StaffId("remote-session"), worktree=staff.PresentWorktree("other-machine", self.root)),
            staff.StaffRow(staff.StaffId("unrelated"), "unit", "herdr", staff.StaffRoot(),
                current_session=staff.StaffId("herdr-session"), worktree=staff.PresentWorktree(machine, self.root)),
        )
        with patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot(rows)) as read:
            self.assertEqual(stop_resume.resume("acc"), stop_resume.ResumeFailed(
                "acc", "unknown name; candidates: accounts, team-accounts"))
        self.assertEqual(read.call_count, 2)
        with patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot(rows)):
            self.assertEqual(stop_resume.resume("absent"), stop_resume.ResumeFailed("absent", "unknown name"))
        self.start.assert_not_called()

    def test_own_pane_exec_failure_keeps_the_stop_state_and_records_failure(self) -> None:
        self.entry["ended"] = True
        pane_record.write("agents", self.entry)
        with (patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2"}),
              patch.object(os, "chdir"),
              patch.object(os, "execvpe", side_effect=OSError("exec refused")),
              patch.object(pane_launch, "finish_restore", wraps=pane_launch.finish_restore) as finish,
              patch.object(LaunchNames, "resume_proof") as proof):
            self.assertEqual(stop_resume.resume("alpha"), stop_resume.ResumeFailed(
                self.entry["session_id"], "exec refused"))
        proof.assert_not_called()
        self.assertTrue(finish.called)
        for invocation in finish.call_args_list:
            self.assertIsInstance(cast(object, invocation.args[3]), pane_launch.FailedResume)
        self.assertTrue(pane_record.read("agents")["w1:p2"].get("ended", False))
        self.start.assert_not_called()
        self.assertEqual(self.client.closed, [])

    def test_resume_of_a_live_target_is_an_idempotent_success(self) -> None:
        identity = self.entry["session_id"]
        self.launched.add(identity)
        self.client.native_ids["w1:p2"] = identity
        self.assertEqual(stop_resume.resume("alpha", to="claude4"), stop_resume.ClaudeUp(
            identity, "w1:p2", "claude4"))
        self.start.assert_not_called()
        self.kill.assert_not_called()
        self.detach.assert_not_called()


    def test_running_codex_account_uses_its_live_process_home(self) -> None:
        from scripts.accounts import accounts
        from scripts.whoami import account

        identity = self.entry["session_id"]
        home = self.root / ".codex4"
        with (patch.object(herdr, "read_conversation", return_value=herdr.LiveConversation("codex", (987654,))),
              patch.object(account, "process_codex_home", return_value=account.ProcessCodexHome(home)) as read,
              patch.object(accounts, "codex_home_label", return_value="codex4")):
            result = stop_resume.running_on_account(identity, "w1:p2", kind="codex")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        assert isinstance(result, stop_resume.ClaudeUp)
        self.assertEqual(result.account, "codex4")
        read.assert_called_once_with(987654)

    def test_resume_by_name_of_a_running_session_reports_success_without_a_launch(self) -> None:
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(staff.StaffId("accounts"), "unit", "accounts", staff.StaffRoot(),
                             current_session=identity)
        self.client.native_ids["w1:p2"] = identity
        self.live.return_value = sessions.LiveSessionProcess(123)
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(shutdown.main(["resume", "accounts"]), 0)
        self.assertEqual(output.getvalue().splitlines(), [
            f"{identity}: unchanged; Claude runs on claude3 in w1:p2"])
        self.start.assert_not_called()
        self.kill.assert_not_called()

    def test_resume_arguments_use_the_current_row_name_after_a_rename_and_clear(self) -> None:
        fixed = staff.StaffId(self.entry["session_id"])
        current = staff.StaffId("019b3a40-4250-7000-8000-000000000103")
        owner = staff.StaffRow(fixed, "unit", "row-name", staff.StaffRoot(), current_session=current)
        with patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((owner,))):
            arguments = stop_resume.resume_arguments(self.entry)
        self.assertEqual(arguments[arguments.index("--resume") + 1], current)
        self.assertEqual(arguments[arguments.index("-n") + 1], "row-name")
        self.assertEqual(arguments[arguments.index("--remote-control") + 1], "row-name")
        self.assertEqual(self.entry["name"], "alpha")

    def test_resume_reuses_the_open_shell_with_the_fixed_row_id(self) -> None:
        fixed = staff.StaffId(self.entry["session_id"])
        current = staff.StaffId("019b3a40-4250-7000-8000-000000000103")
        row = staff.StaffRow(fixed, "producer", "row-name", staff.StaffRoot(), current_session=current)
        self.client.rows[0]["agent_session"] = {"kind": "none"}
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(self.client, "open_tab") as opened):
            result = stop_resume._resume_once(str(current), why="recovery",  # pyright: ignore[reportPrivateUsage]
                                             names=LaunchNames(staff.StaffSnapshot((row,))))
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        assert isinstance(result, stop_resume.ClaudeUp)
        self.assertEqual((result.session_id, result.pane_id), (current, self.entry["pane_id"]))
        command = self._typed(self.start.call_args.args)
        self.assertEqual(command[2], self.entry["pane_id"])
        self.assertEqual(command[command.index("--resume") + 1], current)
        opened.assert_not_called()

    def test_pane_command_unsets_conductor_variables_and_removes_saved_values(self) -> None:
        conductor = {"HERDR_TAB_ID": "w1:t9", "STAFF_ID": "conductor", "STAFF_OPERATION_FD": "81",
                     "PYTHONUNBUFFERED": "1", "REFUSAL_WATCH_INVENTORY": "inventory", "STAFF_ROW_FD_x": "82"}
        entry = self.entry.copy()
        entry["environment"] = {**entry["environment"], **conductor,
                                "STAFF_ROW_FD_saved": "83", "RESUME_TEST_VALUE": "kept"}
        with patch.dict(os.environ, conductor):
            command = stop_resume._pane_command(  # pyright: ignore[reportPrivateUsage]
                herdr.TabSession("w1:p2", "alpha", "w1", entry["session_id"]), entry, "claude3")
        environment = command[1:command.index("sh")]
        for key in (*conductor, "STAFF_ROW_FD_saved"):
            with self.subTest(key=key):
                self.assertEqual(environment[environment.index(key) - 1], "-u")
                self.assertFalse(any(argument.startswith(f"{key}=") for argument in environment))
        self.assertIn("RESUME_TEST_VALUE=kept", environment)

    def test_pane_command_trusts_the_codex_folder(self) -> None:
        folder = str(self.root / 'a.b "folder"')
        entry = pane_record.launch("w1:p2", "thread", folder, {"CODEX_HOME": "/account"}, ["codex"], kind="codex")
        command = stop_resume._pane_command(  # pyright: ignore[reportPrivateUsage]
            herdr.TabSession("w1:p2", "test", "w1", "thread"), entry, "workers")
        self.assertEqual(command[-4:], ["-c", 'projects={' + json.dumps(folder)
            + '={trust_level="trusted"}}', "resume", "thread"])

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.entry = pane_record.launch("w1:p2", "019b3a40-4250-7000-8000-000000000102", str(self.root),
            {"CLAUDE_CONFIG_DIR": str(self.root / ".claude3"), "SHOWRUNNER_UNIT": "alpha"},
            ["claude", "-n", "alpha", "--remote-control", "alpha", "--model", "sonnet"])
        self.client = FakeClient([{"pane_id": "w1:p2", "tab_id": "w1:t9", "label": "alpha", "workspace_id": "w1",
                                   "agent_session": {"kind": "id", "value": "019b3a40-4250-7000-8000-000000000102"}}])
        self.client_result = self.client.result
        self.tab_titles: dict[str, str] = {"w1:t9": "alpha"}
        for label in ("claude3", "claude4"):
            config = self.root / f".{label}"
            config.mkdir()
            _ = (config / ".claude.json").write_text(json.dumps({
                "projects": {str(self.root): {"hasTrustDialogAccepted": True}},
            }))
        self.live = Mock(return_value=sessions.SessionNotLive())
        self.process = Mock(return_value=sessions.LiveSessionProcess(123))
        self.kill = Mock()
        self.signal_guard = patch.object(stop_resume, "_signal_process", self.kill)
        self.wait = Mock()
        self.start = Mock(return_value="")
        self.seed = Mock(return_value=0)
        self.detach = Mock()
        self.confirm = Mock(side_effect=confirmed_fixture_move)
        self.real_detach = stop_resume._detach  # pyright: ignore[reportPrivateUsage]
        self.clock = ResumeClock()
        self.launched: set[str] = set()

    @override
    def setUp(self) -> None:
        configuration = self.root / "configuration"
        for source in (Path(__file__).resolve().parents[2] / "config").glob("*.conf"):
            atomic_file.write_text_atomic(configuration / source.name, source.read_text())
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(configuration)})))
        _ = self.enterContext(redirect_stderr(io.StringIO()))
        _ = self.enterContext(patch.object(accounts, "accounts_home", return_value=self.root))
        def resolved_program(tool: str, *, environment: Mapping[str, str]) -> str:
            self.assertTrue(environment)
            return str(self.root / "bin" / tool)
        _ = self.enterContext(patch.object(agent_program, "resolve_program", side_effect=resolved_program))
        _ = self.enterContext(patch.object(accounts, "role_account", return_value=accounts.NamedAccount(
            "claude3", self.root / ".claude3")))
        _ = self.enterContext(patch.object(account, "process_config_dir", side_effect=self.process_account))
        _ = self.enterContext(patch.object(herdr, "repository_root", return_value=self.root))
        _ = self.enterContext(patch.object(herdr, "repository_trust_key", return_value=self.root))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.root),
            "CODEX_HOME": str(self.root / ".codex"),
            "CLAUDE_CONFIG_DIR": str(self.root / ".claude3"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "AGENT_NOTES_DIR": str(self.root / "notes"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "CLAUDE_CODE_SESSION_ID": "", "SHUTDOWN_STATE_DIR": str(self.root / "shutdown"),
        })))
        self.assertTrue(moves.store_file().is_relative_to(self.root))
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        _ = self.enterContext(patch.object(herdr, "shared_session", return_value="agents"))
        _ = self.enterContext(patch.object(herdr, "Herdr", return_value=self.client))
        _ = self.enterContext(patch.object(pane_record, "RECORD_DIR", self.root / "panes"))
        pane_record.write("agents", self.entry)
        _ = self.enterContext(patch.object(stall_watch, "STATE_DIR",
                                         Path(self.enterContext(tempfile.TemporaryDirectory()))))
        _ = self.enterContext(patch.object(sessions, "session_liveness", self.observe))
        _ = self.enterContext(patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({}, ())))
        _ = self.enterContext(patch.object(sessions, "process_start", return_value="generation-1"))
        _ = self.enterContext(patch.object(herdr, "native_process", return_value=herdr.NativeAgentProcess(
            herdr.PaneSessionReport("019b3a40-4250-7000-8000-000000000102"), "generation-1", {})))
        _ = self.enterContext(patch.object(sessions, "process_liveness", self.process))
        _ = self.enterContext(patch.object(os, "kill", self.kill))
        # Stop tests supply signals; guard tests restore the shared function.
        _ = self.signal_guard.start()
        self.addCleanup(self.signal_guard.stop)
        _ = self.enterContext(patch.object(stop_resume, "Event", self.wait))
        cast(Mock, cast(Mock, self.wait.return_value).wait).side_effect = self.advance
        _ = self.enterContext(patch.object(self.client, "run", self.pane_run))
        _ = self.enterContext(patch.object(self.client, "run_in_pane", self.run_in_pane))
        _ = self.enterContext(patch.object(self.client, "result", self.pane_result))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HERDR_PROC": str(self.root / "proc")})))
        _ = self.enterContext(self.client.empty_shell(998042, self.root))
        from scripts.production.fake_unit_host import process
        process(os.environ, 123, 1, ["claude"], {}, cwd=self.root)
        _ = self.enterContext(patch.object(stop_resume, "monotonic", lambda: self.clock.elapsed))
        _ = self.enterContext(patch.object(time, "sleep", self.advance))
        _ = self.enterContext(patch.object(unit_runs, "held", return_value=[]))
        _ = self.enterContext(patch.object(pane_record, "seed", self.seed))
        _ = self.enterContext(patch.object(stop_resume, "_detach", self.detach))
        _ = self.enterContext(patch.object(stop_resume, "_confirm_move_start", self.confirm))
        from scripts.whoami import quota_alert
        _ = self.enterContext(patch.object(quota_alert, "tell_user"))
        _ = self.enterContext(patch.object(showrunners, "tell", return_value="fixture report"))


    def run_in_pane(self, pane: str, command: list[str], *, terminal: str = "") -> None:
        RUN_IN_PANE(self.client, pane, command, terminal=terminal)

    def process_account(self, _pid: int) -> account.ConfiguredClaudeFolder:
        return account.ConfiguredClaudeFolder(self.root / (".claude4" if self.launched else ".claude3"))

    def advance(self, seconds: float) -> None:
        self.clock.elapsed += seconds

    def observe(self, session: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive | sessions.SessionLivenessUnknown:
        return sessions.LiveSessionProcess(456) if session in self.launched else cast(
            sessions.LiveSessionProcess | sessions.SessionNotLive | sessions.SessionLivenessUnknown, self.live(session))

    def pane_result(self, *arguments: str, deadline: float = math.inf) -> dict[str, object]:
        if arguments[:2] == ("tab", "list"):
            return {"tabs": [{"tab_id": row.get("tab_id", str(row["pane_id"]) + ":tab"),
                              "label": self.tab_titles.get(
                                  str(row.get("tab_id", str(row["pane_id"]) + ":tab")), row["label"]),
                              "pane_count": 1} for row in self.client.rows]}
        return self.client_result(*arguments, deadline=deadline)

    def pane_run(self, *arguments: str) -> str:
        if arguments == ("session", "list", "--json"):
            return json.dumps({"sessions": [{"name": self.client.session, "running": True, "default": False,
                "session_dir": str(self.root), "socket_path": str(self.root / "herdr.sock")}]})
        if arguments[:2] in (("pane", "rename"), ("tab", "rename")):
            for row in self.client.rows:
                tab = str(row.get("tab_id", str(row["pane_id"]) + ":tab"))
                if arguments[:2] == ("pane", "rename") and arguments[2] == row["pane_id"]:
                    if tab not in self.tab_titles:
                        self.tab_titles[tab] = str(row["label"])
                    row["label"] = arguments[3]
                elif arguments[:2] == ("tab", "rename") and arguments[2] == tab:
                    self.tab_titles[tab] = arguments[3]
            return "{}"
        result = cast(str, self.start(*arguments))
        command = self._typed(arguments)
        identity = command[command.index("--resume") + 1]
        self.launched.add(identity)
        self.client.native_ids[arguments[2]] = identity
        pane = arguments[2]
        if pane not in pane_record.read("agents"):
            original = next((entry for entry in pane_record.read("agents").values()
                             if entry["session_id"] == identity), None)
            assert original is not None
            environment = dict(original["environment"])
            for item in command[:command.index("exec")]:
                if "=" in item:
                    key, value = item.split("=", 1)
                    environment[key] = value
            replacement = pane_record.launch(pane, identity, original["cwd"], environment,
                ["claude", *command[command.index("exec") + 2:]])
            replacement["session_id"] = original["session_id"]
            entries = pane_record.read("agents")
            entries[pane] = replacement
            pane_record.save("agents", entries)
        return result

    @staticmethod
    def _typed(arguments: tuple[str, ...]) -> tuple[str, ...]:
        """What the pane's shell parses from the one line herdr types."""
        return (*arguments[:3], *shlex.split(arguments[3]))

    def add_beta(self) -> None:
        second = pane_record.launch("w1:p4", "sid-beta", str(self.root),
            {"CLAUDE_CONFIG_DIR": str(self.root / ".claude3")}, ["claude", "-n", "beta", "--model", "opus"])
        self.client.rows.append({"pane_id": "w1:p4", "terminal_id": "beta-terminal", "tab_id": "w1:t7", "label": "beta", "workspace_id": "w1",
                                 "agent_session": {"kind": "id", "value": "sid-beta"}})
        pane_record.write("agents", second)

    def alive_until_kill(self, _pid: int, _generation: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
        return (sessions.LiveSessionProcess(123) if call(123, signal.SIGKILL) not in self.kill.call_args_list
                else sessions.SessionNotLive())

    def test_review_resume_keeps_the_lens_and_task_with_the_current_folder(self) -> None:
        run = str(self.root / "worker run")
        old, current = str(self.root / "old folder"), str(self.root / "staff folder")
        command = ["bash", str(stop_resume.SCRIPTS / "direct/review.sh"), "--lens", "contract",
                   "--phase", "4w", run, old, "old prompt.md", "review", "check the contract", "reviewing", "3"]
        seat = ClaudeChildResume(run_dir=run, seat="review2", role="review", staff_id="019b3a40-4250-7000-8000-000000000104",
                          turn_live=True, launcher_command=command)
        rebuilt = shlex.split(stop_resume.resume_worker_command(seat, current))
        self.assertEqual(rebuilt, ["bash", str(stop_resume.SCRIPTS / "direct/assign.sh"),
                                  "--to", "review2", "--session-dir", run, "--cwd", current,
                                  "--prompt", str(Path(run) / "review2.resume-prompt.md"),
                                  "--lens", "contract", "--phase", "4w", "--role", "review", "--pass", "3"])
        self.assertEqual(seat["launcher_command"], command)

    def test_saved_review_resumes_without_retired_allocation_options(self) -> None:
        run, cwd = str(self.root / "worker run"), str(self.root / "working tree")
        task = [run, cwd, "old prompt.md", "review", "check this change", "reviewing", "2"]
        command = ["bash", str(stop_resume.SCRIPTS / "direct/review.sh"),
                   "--pool", "code", "--lease", "x/y/7", "--phase", "3p", "--lens", "contract",
                   "--slot", "review", *task]
        seat = ClaudeChildResume(run_dir=run, seat="review2", role="review", staff_id="019b3a40-4250-7000-8000-000000000104",
                          turn_live=True, launcher_command=command)
        expected = ["bash", str(stop_resume.SCRIPTS / "direct/assign.sh"), "--to", "review2",
                    "--session-dir", run, "--cwd", cwd, "--prompt", str(Path(run) / "review2.resume-prompt.md"),
                    "--phase", "3p", "--lens", "contract", "--role", "review", "--pass", "2"]
        rebuilt = stop_resume.resume_worker_command(seat, cwd)
        self.assertEqual(shlex.split(rebuilt), expected)
        self.assertNotIn("--pool", shlex.split(rebuilt))
        self.assertNotIn("--lease", shlex.split(rebuilt))
        seat["launcher_command"] = shlex.split(rebuilt)
        self.assertEqual(stop_resume.resume_worker_command(seat, cwd), rebuilt)

    def test_positional_review_resume_keeps_the_pass_lens_and_delivery_file(self) -> None:
        run, cwd = str(self.root / "worker run"), str(self.root / "staff folder")
        ready = str(self.root / "final diff ready")
        for requested_lens in ("", "contract"):
            with self.subTest(requested_lens=requested_lens):
                command = ["bash", str(stop_resume.SCRIPTS / "direct/review.sh")]
                if requested_lens:
                    command.extend(("--lens", requested_lens))
                command.extend((run, "/old tree", "old prompt", "review", "check this change", "reviewing",
                                "3", "adversary", ready))
                seat = ClaudeChildResume(run_dir=run, seat="review2", role="review", staff_id="worker-id",
                                  turn_live=True, launcher_command=command)
                rebuilt = shlex.split(stop_resume.resume_worker_command(seat, cwd))
                options = dict(zip(rebuilt[2::2], rebuilt[3::2]))
                self.assertEqual(options["--pass"], "3")
                self.assertEqual(options["--lens"], requested_lens or "adversary")
                self.assertEqual(options["--early-ready"], ready)
                self.assertEqual(options["--cwd"], cwd)
                self.assertEqual(options["--prompt"], str(Path(run) / "review2.resume-prompt.md"))
                seat["launcher_command"] = rebuilt
                self.assertEqual(shlex.split(stop_resume.resume_worker_command(seat, cwd)), rebuilt)

    def test_saved_assignment_resume_keeps_task_options_and_repeated_images(self) -> None:
        run, cwd = str(self.root / "worker run"), str(self.root / "staff folder")
        task = ["--role", "review", "--model", "gpt-6.1", "--effort", "high",
                "--image", "/image one.png", "--image", "/image two.png", "--operation", "follow",
                "--held-role", "reviewer", "--held-design", "design-id", "--family", "codex",
                "--phase", "3t", "--lens", "contract", "--pass", "4", "--summary", "/summary file",
                "--early-ready", "/ready file", "--style-since", "abc", "--style-until", "def",
                "--diff", "/saved diff", "--log", "/saved log", "--id-file", "/saved id",
                "--from-session-dir", "/former run", "--fix-cause", "keep this cause", "--paths", "/saved paths"]
        for launcher in ("assign.sh", "assign.py"):
            with self.subTest(launcher=launcher):
                saved = ["bash" if launcher.endswith(".sh") else "python3",
                         str(stop_resume.SCRIPTS / "direct" / launcher), "--new-worker", "former-worker",
                         "--session-dir", "/old run", "--cwd", "/old tree", "--prompt", "/old prompt", *task]
                seat = ClaudeChildResume(run_dir=run, seat="review2", role="impl", staff_id="worker-id",
                                  turn_live=True, launcher_command=saved)
                rebuilt = shlex.split(stop_resume.resume_worker_command(seat, cwd))
                self.assertEqual(rebuilt, ["bash", str(stop_resume.SCRIPTS / "direct/assign.sh"),
                                          "--to", "review2", "--session-dir", run, "--cwd", cwd,
                                          "--prompt", str(Path(run) / "review2.resume-prompt.md"), *task])
                self.assertEqual(seat["launcher_command"], saved)
                seat["launcher_command"] = rebuilt
                self.assertEqual(shlex.split(stop_resume.resume_worker_command(seat, cwd)), rebuilt)

    def test_writer_resume_uses_the_phase_folder_and_has_no_run_paths_fallback(self) -> None:
        run = str(self.root / "worker run")
        for phase in ("ad hoc", ""):
            with self.subTest(phase=phase):
                command = ["bash", str(stop_resume.SCRIPTS / "direct/assign.sh"), "--role", "implement",
                           "--session-dir", run, "--cwd", "/old tree", "--prompt", "old prompt", "--to", "writer"]
                if phase:
                    command.extend(("--phase", phase))
                seat = ClaudeChildResume(run_dir=run, seat="writer", role="impl", staff_id="worker-id",
                                  turn_live=True, launcher_command=command)
                rebuilt = shlex.split(stop_resume.resume_worker_command(seat, "/current tree"))
                options = dict(zip(rebuilt[2::2], rebuilt[3::2]))
                self.assertEqual(options["--role"], "implement")
                if phase:
                    self.assertEqual(options["--paths"], str(Path(run) / "phases/adhoc/files"))
                else:
                    self.assertNotIn("--paths", options)

    def test_resume_without_a_launcher_uses_the_shared_last_dispatch(self) -> None:
        from scripts.direct import progress_history

        run = self.root / "worker run"
        seat = ClaudeChildResume(run_dir=str(run), seat="worker", role="impl",
                          staff_id="019b3a40-4250-7000-8000-000000000104", turn_live=True, launcher_command=[])
        for role in ("review", "test"):
            with self.subTest(role=role):
                missing = progress_history.DispatchDetailNotRecorded.UNRECORDED
                dispatch = progress_history.RecordedDispatch(
                    role, "3t", "contract" if role == "review" else missing,
                    missing if role == "review" else "/saved/paths.txt")
                with patch.object(progress_history, "last_dispatch", return_value=dispatch) as read:
                    command = shlex.split(stop_resume.resume_worker_command(seat, "/current/tree"))
                read.assert_called_once_with(staff.StaffId(seat["staff_id"]))
                self.assertEqual(command[:2], ["bash", str(stop_resume.SCRIPTS / "direct/assign.sh")])
                options = dict(zip(command[2::2], command[3::2]))
                self.assertEqual(options["--role"], role)
                self.assertEqual(options["--phase"], "3t")
                self.assertEqual(options["--cwd"], "/current/tree")
                self.assertEqual(options["--prompt"], str(run / "worker.resume-prompt.md"))
                if role == "review":
                    self.assertEqual(options["--lens"], "contract")
                    self.assertNotIn("--paths", options)
                else:
                    self.assertEqual(options["--paths"], "/saved/paths.txt")

    def test_recovery_opens_a_fresh_tab_for_a_missing_pane_then_uses_the_shared_resume(self) -> None:
        self.client.rows.clear()
        def open_tab(space: str, title: str, cwd: str, environment: dict[str, str]) -> str:
            self.assertEqual((space, title, cwd), ("alpha", "alpha", str(self.root)))
            self.assertEqual(environment["CLAUDE_CONFIG_DIR"], str(self.root / ".claude3"))
            self.client.rows.append({"pane_id": "w2:p3", "terminal_id": "fresh-terminal", "label": title, "workspace_id": "w2",
                "agent_session": {"kind": "none"}})
            return "w2:p3"
        with patch.object(self.client, "open_tab", side_effect=open_tab) as opened:
            result = stop_resume.resume("019b3a40-4250-7000-8000-000000000102", why="watchdog recovery")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        assert isinstance(result, stop_resume.ClaudeUp)
        self.assertEqual((result.session_id, result.pane_id, result.account), ("019b3a40-4250-7000-8000-000000000102", "w2:p3", "claude3"))
        opened.assert_called_once()
        self.assertIn("019b3a40-4250-7000-8000-000000000102", self.launched)
        self.assertEqual(pane_record.read("agents")["w2:p3"]["session_id"], "019b3a40-4250-7000-8000-000000000102")
        self.assertEqual(self._typed(self.start.call_args.args)[2], "w2:p3")

    def test_recovery_uses_the_locked_snapshot_for_the_renamed_row_title_and_arguments(self) -> None:
        from scripts.production import session_staff

        fixed = staff.StaffId(self.entry["session_id"])
        current = staff.StaffId("019b3a40-4250-7000-8000-000000000103")
        row = staff.StaffRow(fixed, "showrunner", "row-name", staff.StaffRoot(), current_session=current,
                             home=staff.SessionHome(self.root / ".claude3"))
        record = staff.StaffSnapshot((row,))
        _ = self.enterContext(patch.object(staff, "local_snapshot", return_value=record))
        retained = self.entry.copy()
        retained["session_id"] = current
        pane_record.save("agents", {retained["pane_id"]: retained})
        self.client.rows.clear()
        opened: list[str] = []

        def open_tab(space: str, title: str, cwd: str, environment: dict[str, str]) -> str:
            _ = space, cwd, environment
            opened.append(title)
            self.client.rows.append({"pane_id": "w2:p3", "terminal_id": "fresh-terminal", "label": title, "workspace_id": "w2"})
            return "w2:p3"

        with (patch.object(staff, "initial_snapshot", return_value=record) as snapshot,
              patch.object(session_staff, "member_sources", return_value=session_staff.MemberSources(())),
              patch.object(self.client, "open_tab", side_effect=open_tab)):
            result = stop_resume.resume(str(fixed), why="recovery")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertEqual(opened, ["row-name"])
        self.assertEqual(snapshot.call_count, 3)
        command = self._typed(self.start.call_args.args)
        self.assertEqual(command[command.index("-n") + 1], "row-name")
        self.assertEqual(command[command.index("--resume") + 1], current)
        self.assertEqual(pane_record.read("agents")["w2:p3"]["name"], "row-name")

    def test_recovery_retitles_an_existing_pane_with_the_current_row_name(self) -> None:
        from scripts.production import session_staff

        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(identity, "showrunner", "row-name", staff.StaffRoot(), current_session=identity)
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))) as snapshot,
              patch.object(session_staff, "member_sources", return_value=session_staff.MemberSources(()))):
            result = stop_resume.resume(str(identity), why="recovery")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertEqual(self.client.rows[0]["label"], "row-name")
        command = self._typed(self.start.call_args.args)
        self.assertEqual(command[command.index("-n") + 1], "row-name")
        self.assertEqual(snapshot.call_count, 2)

    def test_recovery_reconciles_titles_and_preserves_unnamed_titles(self) -> None:
        from scripts.production import session_staff

        identity = staff.StaffId(self.entry["session_id"])
        cases = (
            ("stale-tab", "row-name", "row-name", "old-tab", 1),
            ("current-titles", "row-name", "row-name", "row-name", 2),
            ("unnamed", "", "manual-pane", "manual-tab", 1),
        )
        for case, name, pane_title, tab_title, attempts in cases:
            with self.subTest(case=case):
                retained = self.entry.copy()
                retained["name"] = name
                pane_record.save("agents", {retained["pane_id"]: retained})
                self.client.rows[0]["label"] = pane_title
                self.tab_titles["w1:t9"] = tab_title
                record = staff.StaffSnapshot((staff.StaffRow(
                    identity, "showrunner", name, staff.StaffRoot(), current_session=identity),)) if name else staff.StaffSnapshot(())
                state = self.root / case
                with (patch.dict(os.environ, {"XDG_STATE_HOME": str(state)}),
                      patch.object(staff, "initial_snapshot", return_value=record),
                      patch.object(session_staff, "member_sources", return_value=session_staff.MemberSources(())),
                      patch.object(self.client, "retitle", wraps=self.client.retitle) as retitle,
                      patch.object(self.client, "run", wraps=self.client.run) as commands,
                      patch.object(self.client, "order_tabs", return_value={}) as order,
                      patch.object(self.client, "save_layout", wraps=self.client.save_layout) as save):
                    for _ in range(attempts):
                        self.launched.clear()
                        result = stop_resume.resume(str(identity), why="recovery")
                        self.assertIsInstance(result, stop_resume.ClaudeUp)
                    renames = [invocation for invocation in commands.call_args_list
                               if invocation.args[:2] in (("pane", "rename"), ("tab", "rename"))]
                    if case == "stale-tab":
                        self.assertEqual(renames, [call("tab", "rename", "w1:t9", name)])
                        order.assert_called_once()
                        save.assert_called_once()
                    else:
                        self.assertEqual(renames, [])
                        order.assert_not_called()
                        save.assert_not_called()
                        self.assertFalse(herdr.layout_path("agents").exists())
                    self.assertEqual(self.client.rows[0]["label"], name or pane_title)
                    self.assertEqual(self.tab_titles["w1:t9"], name or tab_title)
                    if name:
                        self.assertEqual(retitle.call_count, attempts)
                    else:
                        retitle.assert_not_called()

    def test_recovery_fixture_rejects_an_unrecorded_resume_identity(self) -> None:
        with self.assertRaises(AssertionError):
            _ = self.pane_run("pane", "run", "w2:p3", "exec claude --resume unrecorded-session")
        self.assertNotIn("w2:p3", pane_record.read("agents"))

    def test_fixed_owner_resume_uses_current_pane_and_preserves_staff_and_retained_record(self) -> None:
        from scripts.production.staff_test_support import isolated_staff_file, isolated_source_readers, source_environment

        path = self.root / "staff.json"
        with (isolated_staff_file(path, {}),
              patch.dict(os.environ, source_environment(self.root / "sources")),
              isolated_source_readers()):
            parent = staff.ensure("showrunner", name="demo", runtime_id="019b3a40-4250-7000-8000-000000000101")
            owner = staff.ensure("unit", name="alpha", parent=staff.ReportsTo(parent.id), runtime_id=self.entry["session_id"])
            current = staff.native_id("019b3a40-4250-7000-8000-000000000103")
            _ = staff.update_current_session(owner.current_session, current)
            folder = self.root / "staff-folder"
            _ = staff.set_worktree(owner.id, staff.PresentWorktree(remote.local_machine(), folder), expected=owner.worktree)
            self.client.rows[0]["agent_session"] = {"kind": "id", "value": current}
            pane_record.write("agents", self.entry)
            record = pane_record.record_path("agents")
            before = path.read_bytes(), record.read_bytes(), record.stat().st_mtime_ns
            result = stop_resume.resume(owner.id, why="fixture changed conversation")
            self.assertIsInstance(result, stop_resume.ClaudeUp)
            assert isinstance(result, stop_resume.ClaudeUp)
            self.assertEqual((result.session_id, result.pane_id, result.account), (current, "w1:p2", "claude3"))
            arguments = self._typed(self.start.call_args.args)
            self.assertEqual(arguments[arguments.index("--resume") + 1], current)
            self.assertNotIn(owner.id, arguments)
            self.assertEqual(self.launched, {current})
            self.assertIn(str(folder), arguments)
            self.assertEqual((path.read_bytes(), record.read_bytes(), record.stat().st_mtime_ns), before)

    def test_stop_returns_as_soon_as_term_takes_effect(self) -> None:
        self.live.return_value = sessions.LiveSessionProcess(123)
        def exits_soon(_pid: int, _generation: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
            return sessions.LiveSessionProcess(123) if self.clock.elapsed < 0.1 else sessions.SessionNotLive()
        self.process.side_effect = exits_soon
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.Stopped("019b3a40-4250-7000-8000-000000000102", 123))
        self.assertLessEqual(self.clock.elapsed, 0.1)
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM)])

    def prompt_retirement_fixture(self, *, alerted: bool) -> tuple[Path, staff.StaffRow]:
        """Publish cancellation requests for the watcher with an isolated send.py recorder."""
        from scripts.production.staff_test_support import unit_membership

        row = unit_membership("production", "alpha")
        self.entry["environment"].update({"SHOWRUNNER_UNIT": "production", "SHOWRUNNER_UNIT_ID": "alpha"})
        pane_record.write("agents", self.entry)
        state = self.root / "stall-watch"
        state.mkdir(exist_ok=True)
        sender = self.root / "retire.py"
        log = self.root / "retire.json"
        _ = sender.write_text("import json, pathlib, sys\n"
                              + f"pathlib.Path({str(log)!r}).write_text(json.dumps(sys.argv[1:]))\n")
        _ = self.enterContext(patch.object(stall_watch, "STATE_DIR", state))
        _ = self.enterContext(patch.object(stall_watch, "SEND", sender))
        prompt = stall_watch.PromptWait(session_id="019b3a40-4250-7000-8000-000000000102", created=0,
            key="stall-watch:prompt:production:alpha", since=0, alerted=alerted, tool="permission", retiring=False, told=60)
        stall_watch.save_prompt(stall_watch.watched_staff_unit("production", "alpha", row).prompt_path, prompt)
        return log, row

    def assert_prompt_cancellation_requested(self, log: Path, row: staff.StaffRow) -> None:
        self.assertFalse(log.exists(), "stop and move must never send")
        path = stall_watch.watched_staff_unit("production", "alpha", row).prompt_path
        self.assertTrue(path.exists())
        requests = list(stall_watch.STATE_DIR.glob("*.prompt.cancel.json"))
        self.assertTrue(requests)
        for request in requests:
            record = cast(dict[str, object], json.loads(request.read_text()))
            self.assertEqual(set(record), {"session_id", "at"})
            self.assertEqual(record["session_id"], "019b3a40-4250-7000-8000-000000000102")
            self.assertIsInstance(record["at"], (float, int))

    def drain_prompt_cancellations(self) -> None:
        with (patch.object(showrunners, "load_settings", return_value={}),
              patch.object(showrunners, "registered_showrunners", return_value=showrunners.ShowrunnerDiscovery()),
              patch.object(stall_watch, "processes", return_value=[]),
              patch.object(stall_watch, "prompt_grace_seconds", return_value=120),
              patch.object(unit_lookup, "read_run_states", return_value=unit_lookup.RunStates({}))):
            _ = stall_watch.tick(100)

    def test_stop_requests_prompt_cancellation_before_returning_without_sending(self) -> None:
        log, row = self.prompt_retirement_fixture(alerted=True)
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.NotRunning("019b3a40-4250-7000-8000-000000000102"))
        self.assert_prompt_cancellation_requested(log, row)
        worker = staff.StaffRow(staff.StaffId("fixed-worker"), "worker", "impl", staff.ReportsTo(staff.StaffId("holder")),
                             current_session=staff.StaffId(self.entry["session_id"]))
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((worker,))),
              patch.object(worker_panes, "observe", return_value=[worker_panes.StoppedWorker(worker, "w1:p2", "w1:p2-terminal")]),
              patch.object(stop_resume, "_stop", side_effect=AssertionError("a proved shell needs no native stop")),
              patch.object(stop_resume, "_retire_prompt", wraps=stop_resume._retire_prompt) as retire):  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(stop_resume.stop(worker.id), stop_resume.NotRunning(self.entry["session_id"]))
        retire.assert_called_once_with(self.entry["session_id"])
        self.assert_prompt_cancellation_requested(log, row)

    def test_move_requests_prompt_cancellation_before_resuming_without_sending(self) -> None:
        log, row = self.prompt_retirement_fixture(alerted=False)
        def start_after_request(*arguments: str) -> str:
            self.assertIn("exec", self._typed(arguments))
            self.assert_prompt_cancellation_requested(log, row)
            return ""
        self.start.side_effect = start_after_request
        self.assertEqual(stop_resume.move("alpha", "claude4", why="limit"),
                         stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude4"))
        self.assert_prompt_cancellation_requested(log, row)

    def test_failed_prompt_retirement_stays_requested_until_the_watcher_confirms_it(self) -> None:
        log, row = self.prompt_retirement_fixture(alerted=True)
        sender = self.root / "retire.py"
        original = sender.read_text()
        _ = sender.write_text(original + "raise SystemExit(3)\n")
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.NotRunning("019b3a40-4250-7000-8000-000000000102"))
        self.assert_prompt_cancellation_requested(log, row)
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.drain_prompt_cancellations()
        self.assertIn("stall-watch: retire:", errors.getvalue())
        path = stall_watch.watched_staff_unit("production", "alpha", row).prompt_path
        self.assertTrue(list(stall_watch.STATE_DIR.glob("*.prompt.cancel.json")))
        _ = sender.write_text(original)
        self.drain_prompt_cancellations()
        self.assertFalse(path.exists())
        self.assertEqual(list(stall_watch.STATE_DIR.glob("*.prompt.cancel.json")), [])
        self.assertEqual(json.loads(log.read_text()), ["retire", "--to", "user", "--from", "stall-watch",
                                                      "--key", "stall-watch:prompt:production:alpha"])

    def test_cancellation_key_errors_cannot_prevent_stop_bookkeeping_or_move_resume(self) -> None:
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        errors = io.StringIO()
        with (patch.object(stall_watch, "retire_session_prompt", side_effect=KeyError("session_id")),
              redirect_stderr(errors)):
            self.assertEqual(stop_resume.stop("alpha"), stop_resume.Stopped("019b3a40-4250-7000-8000-000000000102", 123))
        self.assertTrue(pane_record.read("agents")["w1:p2"].get("ended"))
        self.assertIn("019b3a40-4250-7000-8000-000000000102: prompt cancellation:", errors.getvalue())
        pane_record.write("agents", self.entry)
        self.live.return_value = sessions.SessionNotLive()
        self.process.side_effect = None
        self.process.return_value = sessions.LiveSessionProcess(456)
        with (patch.object(stall_watch, "retire_session_prompt", side_effect=KeyError("session_id")),
              redirect_stderr(errors)):
            self.assertIsInstance(stop_resume.move("alpha", "claude4", why="limit"), stop_resume.ClaudeUp)
        self.assertEqual(errors.getvalue().count("prompt cancellation:"), 2)

    def test_stop_and_move_never_take_a_held_watcher_lock(self) -> None:
        log, row = self.prompt_retirement_fixture(alerted=True)
        real_flock = fcntl.flock
        forbidden_attempts: list[tuple[int, int]] = []
        with (stall_watch.STATE_DIR / "lock").open("a+") as lock:
            real_flock(lock, fcntl.LOCK_EX)
            held = os.fstat(lock.fileno())
            watcher_identity = (held.st_dev, held.st_ino)

            def forbid_watcher_lock(file: int | io.IOBase, operation: int) -> None:
                descriptor = file if isinstance(file, int) else file.fileno()
                stat = os.fstat(descriptor)
                identity = (stat.st_dev, stat.st_ino)
                if identity == watcher_identity:
                    forbidden_attempts.append(identity)
                    raise AssertionError("watcher lock must not be acquired")
                real_flock(file, operation)

            with patch.object(fcntl, "flock", side_effect=forbid_watcher_lock):
                self.assertEqual(stop_resume.stop("alpha"), stop_resume.NotRunning("019b3a40-4250-7000-8000-000000000102"))
                self.assertIsInstance(stop_resume.move("alpha", "claude4", why="limit"), stop_resume.ClaudeUp)
        self.assertEqual(forbidden_attempts, [], "stop and move must never attempt the watcher lock")
        self.assert_prompt_cancellation_requested(log, row)

    def test_a_delayed_cancellation_never_retires_a_resumed_sessions_new_prompt(self) -> None:
        log, row = self.prompt_retirement_fixture(alerted=True)
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.NotRunning("019b3a40-4250-7000-8000-000000000102"))
        path = stall_watch.watched_staff_unit("production", "alpha", row).prompt_path
        stall_watch.save_prompt(path, stall_watch.PromptWait(session_id="019b3a40-4250-7000-8000-000000000102", created=time.time() + 1,
            key="stall-watch:prompt:production:alpha", since=200, alerted=False, tool="new-request", retiring=False))
        self.drain_prompt_cancellations()
        self.assertFalse(log.exists())
        self.assertEqual(list(stall_watch.STATE_DIR.glob("*.prompt.cancel.json")), [])
        saved = stall_watch.saved_prompt(path)
        assert saved is not None
        self.assertEqual((saved["since"], saved["retiring"]), (200, False))

    def test_a_malformed_cancellation_does_not_stop_valid_requests(self) -> None:
        log, row = self.prompt_retirement_fixture(alerted=True)
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.NotRunning("019b3a40-4250-7000-8000-000000000102"))
        bad = stall_watch.STATE_DIR / "aaa.prompt.cancel.json"
        _ = bad.write_text("{")
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.drain_prompt_cancellations()
        self.assertIn(str(bad), errors.getvalue())
        self.assertTrue(bad.exists())
        self.assertTrue(log.exists())
        self.assertFalse(stall_watch.watched_staff_unit("production", "alpha", row).prompt_path.exists())

    def test_a_mac_stop_publishes_cancellation_without_any_lookup(self) -> None:
        log, row = self.prompt_retirement_fixture(alerted=True)
        with (patch("platform.system", return_value="Darwin"),
              patch.object(pane_record, "launch_for_session", side_effect=AssertionError("no launch lookup")) as launch,
              patch.object(staff_sources, "unit_parent", side_effect=AssertionError("no remote lookup")) as parent,
              patch.object(showrunners, "socket_for", side_effect=AssertionError("no socket lookup")) as socket):
            self.assertEqual(stop_resume.stop("alpha"), stop_resume.NotRunning("019b3a40-4250-7000-8000-000000000102"))
        launch.assert_not_called()
        parent.assert_not_called()
        socket.assert_not_called()
        self.assert_prompt_cancellation_requested(log, row)

    def test_launch_deletion_after_stop_cannot_lose_the_old_alerts_cancellation(self) -> None:
        log, row = self.prompt_retirement_fixture(alerted=True)
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.NotRunning("019b3a40-4250-7000-8000-000000000102"))
        # Launch deletion must preserve pending prompt cancellation.
        pane_record.record_path("agents").unlink()
        self.drain_prompt_cancellations()
        self.assertEqual(json.loads(log.read_text()), ["retire", "--to", "user", "--from", "stall-watch",
                                                      "--key", "stall-watch:prompt:production:alpha"])
        self.assertFalse(stall_watch.watched_staff_unit("production", "alpha", row).prompt_path.exists())
        self.assertEqual(list(stall_watch.STATE_DIR.glob("*.prompt.cancel.json")), [])

    def test_missing_pane_recovery_does_not_duplicate_a_live_session(self) -> None:
        self.client.rows.clear()
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.client.native_ids["w1:p2"] = self.entry["session_id"]
        with patch.object(self.client, "open_tab") as opened:
            result = stop_resume.resume("019b3a40-4250-7000-8000-000000000102", why="recovery")
        self.assertEqual(result, stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude3"))
        opened.assert_not_called()
        self.start.assert_not_called()

    def test_seed_reads_only_the_selected_session_pane(self) -> None:
        self.add_beta()
        _ = stop_resume.stop("alpha")
        self.seed.assert_called_once()
        select = cast(Callable[[herdr.PaneIdentity], bool], self.seed.call_args.kwargs["select"])
        self.assertEqual([pane["pane_id"] for pane in self.client.panes() if select(pane)], ["w1:p2"])

    def test_move_resume_failure_is_pending_and_rerun_finishes_without_restopping(self) -> None:
        self.start.side_effect = herdr_types.HerdrFailure("registry temporarily unreadable")
        with patch.object(stop_resume, "_stop", return_value=stop_resume.Stopped("019b3a40-4250-7000-8000-000000000102", 123)):
            pending = stop_resume.move("alpha", "claude4", why="limit")
        self.assertIsInstance(pending, stop_resume.ResumePending)
        assert isinstance(pending, stop_resume.ResumePending)
        self.assertEqual(accounts.move_result_line(pending),
                         "019b3a40-4250-7000-8000-000000000102: stopped, resume pending on claude4: registry temporarily unreadable")
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["requested", "failed"])
        self.assertEqual(entries[0]["move_id"], entries[1]["move_id"])
        self.start.side_effect = None
        self.assertEqual(stop_resume.move("alpha", "claude4", why="limit"),
                         stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude4"))
        self.start.reset_mock()
        self.seed.reset_mock()
        before = pane_record.record_path("agents").read_bytes()
        with patch.object(stop_resume, "_stop", side_effect=AssertionError("rerun stopped the destination")), \
                patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "019b3a40-4250-7000-8000-000000000102"}):
            self.assertEqual(stop_resume.move("alpha", "claude4", why="limit"),
                             stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude4"))
        self.start.assert_not_called()
        self.seed.assert_not_called()
        self.confirm.assert_called_once()
        self.detach.assert_not_called()
        self.assertEqual(pane_record.record_path("agents").read_bytes(), before)


    def test_shutdown_resume_with_an_explicit_target_finishes_a_pending_move_and_reruns_with_one_line(self) -> None:
        self.start.side_effect = herdr_types.HerdrFailure("cannot start yet")
        self.assertIsInstance(stop_resume.move("alpha", "claude4", why="limit"), stop_resume.ResumePending)
        self.start.side_effect = None
        for iteration in range(2):
            output = io.StringIO()
            with redirect_stdout(output):
                status = shutdown.main(["resume", "alpha", "--why", "finish move", "--to", "claude4"])
            self.assertEqual(status, 0, output.getvalue())
            self.assertEqual(len(output.getvalue().splitlines()), 1)
            self.assertIn("Claude is up on claude4" if iteration == 0 else "unchanged; Claude runs on claude4", output.getvalue())

    def test_move_writes_target_trust_before_start(self) -> None:
        target = self.root / ".claude4/.claude.json"
        atomic_file.write_text_atomic(target, json.dumps({"projects": {}, "unrelated": "keep"}))
        def start_after_trust(*arguments: str) -> str:
            self.assertIsInstance(herdr.claude_trust(self.root, target), herdr.Trusted)
            command = self._typed(arguments)
            self.assertEqual(command[command.index("exec") + 1], "claude4")
            return ""
        self.start.side_effect = start_after_trust
        results = stop_resume.move_sessions(["alpha"], "claude4", why="usage limit")
        self.start.assert_called_once()
        result = results[0]
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        assert isinstance(result, stop_resume.ClaudeUp)
        self.assertEqual(result.trust_copied_from, "")
        line = accounts.move_result_line(result)
        self.assertNotIn("copied folder trust", line)
        self.assertEqual(json.loads(target.read_text())["unrelated"], "keep")


    def test_a_new_session_herdr_has_no_id_for_is_found_through_the_launches_read_first(self) -> None:
        self.client.rows[0]["agent_session"] = None
        pane_record.record_path("agents").unlink()
        def seed(_session: str, *, select: Callable[[herdr.PaneIdentity], bool]) -> int:
            self.assertEqual({pane["pane_id"] for pane in self.client.panes() if select(pane)}, {"w1:p2"})
            pane_record.write("agents", self.entry)
            return 0
        self.seed.side_effect = seed
        self.assertEqual(stop_resume.move_sessions(["alpha"], "claude4", why="limit"),
                         [stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude4")])

    def test_launches_are_read_while_claude_runs_before_any_stop(self) -> None:
        order: list[str] = []
        def seed(_session: str, *, select: Callable[[herdr.PaneIdentity], bool]) -> int:
            self.assertEqual({pane["pane_id"] for pane in self.client.panes() if select(pane)}, {"w1:p2"})
            order.append("seed")
            return 0
        self.seed.side_effect = seed
        def stop_one(session: str) -> stop_resume.NotRunning:
            order.append("stop")
            return stop_resume.NotRunning(session)
        with patch.object(stop_resume, "_stop", side_effect=stop_one):
            _ = stop_resume.move_sessions(["019b3a40-4250-7000-8000-000000000102", "sid-beta"], "claude4", why="limit")
        self.assertEqual(order, ["seed", "stop", "stop"])
        self.launched.clear()
        self.seed.side_effect = herdr_types.HerdrFailure("no reply")
        self.assertEqual(stop_resume.stop("019b3a40-4250-7000-8000-000000000102"), stop_resume.NotRunning("019b3a40-4250-7000-8000-000000000102"))

    def test_proof_workspace_refuses_a_stop_outside_its_panes(self) -> None:
        self.signal_guard.stop()
        identity = self.entry["session_id"]
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        self.client.rows.append({"pane_id": "w2:p1", "workspace_id": "w2", "label": "proof-TEST"})
        def read(*arguments: str, deadline: float = math.inf) -> dict[str, object]:
            if arguments[:2] == ("pane", "process-info"):
                self.assertEqual(arguments[-1], "w2:p1")
                return {"process_info": {"foreground_processes": [{"pid": 456}]}}
            return self.client_result(*arguments, deadline=deadline)
        with (patch.dict(os.environ, {"SHUTDOWN_PROOF_WORKSPACE": "w2"}),
              patch.object(self.client, "result", side_effect=read)):
            result = stop_resume.stop("alpha")
        self.assertIsInstance(result, stop_resume.StopFailed)
        assert isinstance(result, stop_resume.StopFailed)
        self.assertEqual(result.session_id, identity)
        self.assertIn("proof workspace w2", result.reason)
        self.assertIn("pid 123", result.reason)
        self.kill.assert_not_called()

    def test_proof_workspace_refuses_a_pid_in_no_pane(self) -> None:
        self.signal_guard.stop()
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        self.client.process_info = {"foreground_processes": [{"pid": 456}]}
        with (patch.dict(os.environ, {"SHUTDOWN_PROOF_WORKSPACE": "w1"}),
              patch.object(self.client, "result", side_effect=self.client_result)):
            result = stop_resume.stop("alpha")
        self.assertIsInstance(result, stop_resume.StopFailed)
        assert isinstance(result, stop_resume.StopFailed)
        self.assertIn("proof workspace w1", result.reason)
        self.assertIn("pid 123", result.reason)
        self.kill.assert_not_called()

    def test_proof_workspace_allows_a_pid_in_its_pane(self) -> None:
        self.signal_guard.stop()
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        self.client.process_info = {"foreground_processes": [{"pid": 123}]}
        with (patch.dict(os.environ, {"SHUTDOWN_PROOF_WORKSPACE": "w1"}),
              patch.object(self.client, "result", side_effect=self.client_result)):
            result = stop_resume.stop("alpha")
        self.assertEqual(result, stop_resume.Stopped(self.entry["session_id"], 123))
        self.kill.assert_called_once_with(123, signal.SIGTERM)

    def test_stop_without_a_proof_workspace_reads_no_process_info(self) -> None:
        self.signal_guard.stop()
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        with patch.dict(os.environ):
            _ = os.environ.pop("SHUTDOWN_PROOF_WORKSPACE", None)
            _ = os.environ.pop("STAFF_FILE", None)
            _ = os.environ.pop("SHUTDOWN_STATE_DIR", None)
            with patch.object(self.client, "result", side_effect=self.client_result) as read:
                result = stop_resume.stop("alpha")
        self.assertEqual(result, stop_resume.Stopped(self.entry["session_id"], 123))
        self.assertFalse(any(item.args[:2] == ("pane", "process-info") for item in read.call_args_list))
        self.kill.assert_called_once_with(123, signal.SIGTERM)

    def test_proof_workspace_unset_refuses_staff_file_signals(self) -> None:
        self.signal_guard.stop()
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        with patch.dict(os.environ):
            _ = os.environ.pop("SHUTDOWN_PROOF_WORKSPACE", None)
            _ = os.environ.pop("SHUTDOWN_STATE_DIR", None)
            result = stop_resume.stop("alpha")
        self.assertIsInstance(result, stop_resume.StopFailed)
        assert isinstance(result, stop_resume.StopFailed)
        self.assertIn("SHUTDOWN_PROOF_WORKSPACE", result.reason)
        self.kill.assert_not_called()

    def test_proof_workspace_unset_refuses_shutdown_state_signals(self) -> None:
        self.signal_guard.stop()
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        with patch.dict(os.environ):
            _ = os.environ.pop("SHUTDOWN_PROOF_WORKSPACE", None)
            _ = os.environ.pop("STAFF_FILE", None)
            result = stop_resume.stop("alpha")
        self.assertIsInstance(result, stop_resume.StopFailed)
        assert isinstance(result, stop_resume.StopFailed)
        self.assertIn("SHUTDOWN_PROOF_WORKSPACE", result.reason)
        self.kill.assert_not_called()

    def test_proof_workspace_refuses_unreadable_pane_processes(self) -> None:
        self.signal_guard.stop()
        replies: tuple[dict[str, object] | Exception, ...] = (
            {}, {"process_info": {"foreground_processes": [{"pid": 123.0}]}},
            herdr_types.HerdrFailure("pane unavailable"), OSError("herdr unavailable"))
        for reply in replies:
            with self.subTest(reply=reply):
                self.live.return_value = sessions.LiveSessionProcess(123)
                self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
                def read(*arguments: str, deadline: float = math.inf) -> dict[str, object]:
                    if arguments[:2] == ("pane", "process-info"):
                        if isinstance(reply, Exception):
                            raise reply
                        return reply
                    return self.client_result(*arguments, deadline=deadline)
                with (patch.dict(os.environ, {"SHUTDOWN_PROOF_WORKSPACE": "w1"}),
                      patch.object(self.client, "result", side_effect=read)):
                    result = stop_resume.stop("alpha")
                self.assertIsInstance(result, stop_resume.StopFailed)
                assert isinstance(result, stop_resume.StopFailed)
                self.assertIn("proof workspace w1", result.reason)
                self.kill.assert_not_called()

    def test_proof_workspace_checks_membership_again_before_sigkill(self) -> None:
        self.signal_guard.stop()
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.client.process_info = {"foreground_processes": [{"pid": 123}]}
        def move_pane(_pid: int, _number: int) -> None:
            self.client.rows[0]["workspace_id"] = "w2"
        self.kill.side_effect = move_pane
        with (patch.dict(os.environ, {"SHUTDOWN_PROOF_WORKSPACE": "w1"}),
              patch.object(self.client, "result", side_effect=self.client_result)):
            result = stop_resume.stop("alpha")
        self.assertIsInstance(result, stop_resume.StopFailed)
        self.kill.assert_called_once_with(123, signal.SIGTERM)

    def test_proof_workspace_does_not_guard_signal_zero(self) -> None:
        self.signal_guard.stop()
        with (patch.dict(os.environ, {"SHUTDOWN_PROOF_WORKSPACE": "w2"}),
              patch.object(herdr, "Herdr", side_effect=AssertionError("unexpected pane read"))):
            stop_resume._signal_process(123, 0)  # pyright: ignore[reportPrivateUsage]
        self.kill.assert_called_once_with(123, 0)

    def test_proof_workspace_refuses_an_empty_workspace(self) -> None:
        self.signal_guard.stop()
        self.live.return_value = sessions.LiveSessionProcess(123)
        with patch.dict(os.environ, {"SHUTDOWN_PROOF_WORKSPACE": ""}):
            result = stop_resume.stop("alpha")
        self.assertIsInstance(result, stop_resume.StopFailed)
        self.kill.assert_not_called()

    def test_stop_sends_term_then_kill_to_the_same_claude_and_only_marks_its_launch_ended(self) -> None:
        self.live.return_value = sessions.LiveSessionProcess(123)
        def signal_delivered(_pid: int, sig: int) -> None:
            if sig == signal.SIGKILL:
                self.live.return_value = sessions.SessionNotLive()
                self.process.return_value = sessions.SessionNotLive()
        self.kill.side_effect = signal_delivered
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.Stopped("019b3a40-4250-7000-8000-000000000102", 123))
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM), call(123, signal.SIGKILL)])
        self.assertTrue(all(0 < invocation.args[0] <= 0.1
                            for invocation in cast(Mock, cast(Mock, self.wait.return_value).wait).call_args_list))
        launch_path = pane_record.record_path("agents")
        expected = cast(dict[str, dict[str, object]], json.loads(before[launch_path]))
        expected["w1:p2"]["ended"] = True
        self.assertEqual(json.loads(launch_path.read_bytes()), expected)
        after = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(set(after), set(before))
        self.assertEqual({path: content for path, content in after.items() if path != launch_path},
                         {path: content for path, content in before.items() if path != launch_path})
        self.start.assert_not_called()

    def test_stop_does_not_kill_an_exited_or_replaced_process(self) -> None:
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        self.assertEqual(stop_resume.stop("019b3a40-4250-7000-8000-000000000102"), stop_resume.Stopped("019b3a40-4250-7000-8000-000000000102", 123))
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM)])

    def test_stop_never_reports_success_for_a_live_process_in_either_runtime(self) -> None:
        identity = self.entry["session_id"]
        for kind in ("claude", "codex"):
            with (self.subTest(kind=kind),
                  patch.object(herdr, "read_conversation", return_value=herdr.LiveConversation(kind, (123,)))):
                self.kill.reset_mock()
                self.clock.elapsed = 0
                result = stop_resume.stop(identity)
                self.assertIsInstance(result, stop_resume.StopFailed)
                self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM), call(123, signal.SIGKILL)])

    def test_stop_refuses_launch_evidence_even_when_a_caller_says_live(self) -> None:
        identity = self.entry["session_id"]
        with (patch.object(herdr, "read_conversation", return_value=herdr.LiveConversation("codex", (123,))),
              patch.object(herdr, "native_process", return_value=herdr.NativeAgentProcess(
                  herdr.LaunchArgument(identity), "generation-1", {}))):
            result = stop_resume.stop(identity)
        self.assertIsInstance(result, stop_resume.StopFailed)
        assert isinstance(result, stop_resume.StopFailed)
        self.assertIn("launch argument does not prove", result.reason)
        self.kill.assert_not_called()

    def test_stop_unknown_never_reports_stopped_or_sends_a_signal(self) -> None:
        identity = self.entry["session_id"]
        for kind in ("Claude", "Codex"):
            with (self.subTest(kind=kind), redirect_stderr(io.StringIO()) as output,
                  patch.object(herdr, "read_conversation", return_value=herdr.UnknownConversation(
                      f"test-tab ({kind} conversation unknown)"))):
                result = stop_resume.stop(identity)
            self.assertIsInstance(result, stop_resume.StopFailed)
            self.kill.assert_not_called()
            self.assertEqual(output.getvalue(), "")
            assert isinstance(result, stop_resume.StopFailed)
            self.assertIn("test-tab", result.reason)

    def test_codex_release_keeps_an_unknown_native_process_unproved(self) -> None:
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(identity, "worker", "alpha", staff.STAFF_ROOT, current_session=identity)
        unknown = "pid 998071 (Codex thread unknown)"
        with (patch.object(herdr, "_scan_herdr_processes", return_value=[]),
              patch.object(runtime_sessions, "session_liveness", return_value=runtime_sessions.SessionNotLive()),
              patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({}, (unknown,))),
              redirect_stderr(io.StringIO()) as output):
            result = stop_resume.release_source(row, staff.CodexBackend(self.root / "codex"))
        self.assertIsInstance(result, SourceReleaseUnknown)
        assert isinstance(result, SourceReleaseUnknown)
        self.assertIn(unknown, result.reason)
        self.assertEqual(output.getvalue(), "")
        self.kill.assert_not_called()

    def test_source_release_confirms_the_native_process_exit(self) -> None:
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(identity, "worker", "alpha", staff.STAFF_ROOT, current_session=identity)
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        result = stop_resume.release_source(row, staff.ClaudeBackend())
        self.assertIsInstance(result, SourceReleased)
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM)])

    def test_source_release_keeps_unavailable_backend_evidence_unproved(self) -> None:
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(identity, "worker", "alpha", staff.STAFF_ROOT, current_session=identity)
        with patch.object(herdr, "read_conversation", return_value=herdr.UnknownConversation("source identity unreadable")):
            result = stop_resume.release_source(row, staff.BACKEND_UNAVAILABLE)
        self.assertIsInstance(result, SourceReleaseUnknown)
        assert isinstance(result, SourceReleaseUnknown)
        self.assertIn("source identity unreadable", result.reason)
        self.kill.assert_not_called()

    def test_codex_release_refuses_a_hosted_thread_without_a_signal_target(self) -> None:
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(identity, "worker", "alpha", staff.STAFF_ROOT, current_session=identity)
        with (patch.object(herdr, "_scan_herdr_processes", return_value=[]),
              patch.object(runtime_sessions, "session_liveness", return_value=runtime_sessions.SessionNotLive()),
              patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({identity: ()}, ()))):
            result = stop_resume.release_source(row, staff.CodexBackend(self.root / "codex"))
        self.assertIsInstance(result, SourceReleaseUnknown)
        assert isinstance(result, SourceReleaseUnknown)
        self.assertIn("no unique signal target", result.reason)
        self.kill.assert_not_called()

    def test_claude_release_ignores_unrelated_unknown_codex_processes(self) -> None:
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(identity, "worker", "alpha", staff.STAFF_ROOT, current_session=identity)
        with (patch.object(herdr, "_scan_herdr_processes", return_value=[]),
              patch.object(runtime_sessions, "session_liveness", return_value=runtime_sessions.SessionNotLive()) as claude,
              patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations(
                  {}, ("pid 998071 (Codex thread unknown)",))) as codex):
            result = stop_resume.release_source(row, staff.ClaudeBackend())
        self.assertIsInstance(result, SourceReleased)
        claude.assert_called_once_with(identity)
        codex.assert_not_called()
        self.kill.assert_not_called()

    def test_codex_release_does_not_read_an_unreadable_claude_registry(self) -> None:
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(identity, "worker", "alpha", staff.STAFF_ROOT, current_session=identity)
        with (patch.object(herdr, "_scan_herdr_processes", return_value=[]),
              patch.object(runtime_sessions, "session_liveness", side_effect=OSError("Claude registry unreadable")) as claude,
              patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({}, ()))):
            result = stop_resume.release_source(row, staff.CodexBackend(self.root / "codex"))
        self.assertIsInstance(result, SourceReleased)
        claude.assert_not_called()
        self.kill.assert_not_called()

    def test_direct_stop_keeps_shared_unknown_evidence_failed_and_preserves_the_prompt(self) -> None:
        identity = self.entry["session_id"]
        self.entry["kind"] = "codex"
        pane_record.write("agents", self.entry)
        prompt = stall_watch.STATE_DIR / "selected.prompt.json"
        with prompt.open("w") as output:
            json.dump({"session_id": identity, "key": "selected-prompt"}, output)
        before = {path: path.read_bytes() for path in stall_watch.STATE_DIR.iterdir()}
        with (patch.object(herdr, "_scan_herdr_processes", return_value=[]),
              patch.object(runtime_sessions, "session_liveness", return_value=runtime_sessions.SessionNotLive()),
              patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations(
                  {}, ("pid 998071 (Codex thread unknown)",)))):
            result = stop_resume.stop(identity)
        self.assertIsInstance(result, stop_resume.StopFailed)
        self.assertIsInstance(result, stop_resume.StopUnproved)
        self.assertEqual({path: path.read_bytes() for path in stall_watch.STATE_DIR.iterdir()}, before)
        self.kill.assert_not_called()

    def test_stop_proves_an_idle_restarted_codex_process_with_the_staff_current_session(self) -> None:
        identity = self.entry["session_id"]
        self.entry["kind"] = "codex"
        pane_record.write("agents", self.entry)
        self.client.rows.insert(0, {"pane_id": "w1:p1", "workspace_id": "w1", "label": "unnamed"})
        row = staff.StaffRow(staff.StaffId("stable-row"), "producer", "alpha", staff.StaffRoot(),
                             current_session=staff.StaffId(identity))
        self.process.side_effect = self.alive_until_kill
        self.client.process_info = {"foreground_processes": [
            {"pid": 123, "argv": ["codex", "resume", identity], "cwd": str(self.root)}]}
        def native_process(pid: int, kind: str) -> herdr.NativeAgentProcess:
            self.assertEqual((pid, kind), (123, "codex"))
            processes = herdr.json_items(self.client.process_info["foreground_processes"], "fixture foreground processes")
            if herdr.json_object(processes[0], "fixture Codex process")["argv"] != ["codex", "resume", identity]:
                raise ValueError(f"pid {pid}: Codex launch identity is unavailable")
            return herdr.NativeAgentProcess(herdr.PaneSessionReport(identity), "generation-1", {})
        def pane_activity(pane: str) -> herdr.PaneRunning:
            self.assertEqual(pane, "w1:p2")
            return herdr.PaneRunning(("codex",))
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(self.client, "pane_activity", side_effect=pane_activity),
              patch.object(self.client, "result", side_effect=self.client_result),
              patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({identity: (123,)}, ())),
              patch.object(herdr, "native_process", side_effect=native_process) as native):
            self.assertEqual(stop_resume.stop("stable-row"), stop_resume.Stopped(identity, 123))
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM), call(123, signal.SIGKILL)])
        self.assertEqual(native.call_args_list, [call(123, "codex")] * 3)

    def test_stop_finds_an_unnamed_resumed_codex_pane(self) -> None:
        identity = self.entry["session_id"]
        self.entry["kind"] = "codex"
        pane_record.write("agents", self.entry)
        row = staff.StaffRow(staff.StaffId("stable-row"), "producer", "alpha", staff.StaffRoot(),
                             current_session=staff.StaffId(identity))
        _ = self.client.rows[0].pop("agent_session")
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        self.client.process_info = {"foreground_processes": [
            {"pid": 123, "argv": ["codex", "resume", identity], "cwd": str(self.root)}]}
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(self.client, "pane_activity", return_value=herdr.PaneRunning(("codex",))),
              patch.object(self.client, "result", side_effect=self.client_result),
              patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({identity: (123,)}, ()))):
            self.assertEqual(stop_resume.stop("stable-row"), stop_resume.Stopped(identity, 123))
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM)])

    def test_stop_skips_an_unnamed_pane_without_the_selected_codex_conversation(self) -> None:
        identity = self.entry["session_id"]
        self.entry["kind"] = "codex"
        pane_record.write("agents", self.entry)
        row = staff.StaffRow(staff.StaffId("stable-row"), "producer", "alpha", staff.StaffRoot(),
                             current_session=staff.StaffId(identity))
        _ = self.client.rows[0].pop("agent_session")
        observations: list[tuple[dict[str, tuple[int, ...]], list[str]]] = [
            ({"other-thread": (987,)}, []), ({}, []), ({}, ["test-tab (Codex thread unknown)"])]
        for observed in observations:
            with (self.subTest(observed=observed),
                  patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
                  patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations(observed[0], tuple(observed[1])))):
                result = stop_resume.stop("stable-row")
                self.assertIsInstance(result, stop_resume.StopFailed if observed[1] else stop_resume.NotRunning)
                self.kill.assert_not_called()

    def test_registry_removal_does_not_resume_until_the_saved_process_exits(self) -> None:
        self.live.side_effect = [sessions.LiveSessionProcess(123), sessions.LiveSessionProcess(123),
                                 sessions.SessionNotLive(), sessions.SessionNotLive()]
        self.process.side_effect = self.alive_until_kill
        def start(*_command: str) -> str:
            self.assertGreater(self.process.call_count, 4, "registry deletion is not process exit")
            self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM), call(123, signal.SIGKILL)])
            return ""
        self.start.side_effect = start
        self.assertEqual(stop_resume.move("alpha", "claude4", why="limit"),
                         stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude4"))

    def test_stop_confirms_exit_after_kill(self) -> None:
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.process.side_effect = self.alive_until_kill
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.Stopped("019b3a40-4250-7000-8000-000000000102", 123))
        self.assertGreater(self.process.call_count, 4)
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM), call(123, signal.SIGKILL)])

    def test_move_waits_for_exit_when_term_removes_the_registry_before_cleanup_finishes(self) -> None:
        identity = self.entry["session_id"]
        staff_id = staff.StaffId(identity)
        row = staff.StaffRow(staff_id, "unit", "alpha", staff.StaffRoot(), current_session=staff_id,
                             home=staff.SessionHome(self.root / ".claude3"))
        _ = self.enterContext(patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))))
        _ = self.enterContext(patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot((row,))))
        for exits, error in ((True, herdr.NativeRegistryUnavailable("pid 123: native registry identity is unavailable")),
                             (True, PermissionError("process read denied during exit")),
                             (False, herdr.NativeRegistryUnavailable("pid 123: native registry identity is unavailable"))):
            with self.subTest(exits=exits, error=error):
                self.clock.elapsed = 0
                self.launched.clear()
                self.kill.reset_mock()
                self.start.reset_mock()
                def registry(_session: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
                    return sessions.SessionNotLive() if self.kill.called else sessions.LiveSessionProcess(123)

                self.live.side_effect = registry

                def process(_pid: int, _started: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
                    return sessions.SessionNotLive() if exits and self.clock.elapsed >= 3 else sessions.LiveSessionProcess(123)

                def native(_pid: int, _kind: str) -> herdr.NativeAgentProcess:
                    if self.kill.called:
                        raise error
                    return herdr.NativeAgentProcess(herdr.PaneSessionReport(identity), "generation-1", {})

                def start(*_command: str) -> str:
                    self.assertGreaterEqual(self.clock.elapsed, 3)
                    return ""

                self.process.side_effect = process
                self.start.side_effect = start
                with patch.object(herdr, "native_process", side_effect=native), redirect_stderr(io.StringIO()) as output:
                    result = stop_resume.move("alpha", "claude4", why="limit")
                self.kill.assert_called_once_with(123, signal.SIGTERM)
                if isinstance(error, herdr.NativeRegistryUnavailable):
                    self.assertEqual(output.getvalue(), "")
                else:
                    self.assertIn("stop identity anomaly after SIGTERM:", output.getvalue())
                if exits:
                    self.assertEqual(result, stop_resume.ClaudeUp(identity, "w1:p2", "claude4"))
                    self.start.assert_called_once()
                else:
                    self.assertEqual(result, stop_resume.StopFailed(identity, "pid 123 is still running after SIGTERM"))
                    self.start.assert_not_called()

    def test_a_process_still_alive_after_kill_fails_and_never_resumes(self) -> None:
        self.live.return_value = sessions.LiveSessionProcess(123)
        result = stop_resume.move("alpha", "claude4", why="limit")
        self.assertIsInstance(result, stop_resume.StopFailed)
        assert isinstance(result, stop_resume.StopFailed)
        self.assertIn("123", result.reason)
        self.assertIn("SIGKILL", result.reason)
        self.start.assert_not_called()

    def test_self_move_hands_off_the_whole_batch_without_signalling(self) -> None:
        from scripts.production.staff_test_support import fixture_member, isolated_staff_file

        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        _ = fixture_member("producer", name="alpha", runtime_id=self.entry["session_id"])
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.root / "sessions")})))
        self.add_beta()
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform), patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "019b3a40-4250-7000-8000-000000000102"}), patch.object(stop_resume, "_detach", wraps=self.real_detach), patch.object(sys, "platform", platform), patch.object(subprocess, "run", return_value=subprocess.CompletedProcess[str]([], 0, "", "")) as run, patch.object(subprocess, "Popen") as popen:
                self.client.rows[0]["agent_session"] = None
                result = stop_resume.move_sessions(["beta", "alpha"], "claude4", why="limit", warn_seconds=3, cause="auto")
                self.assertFalse(moves.store_file().exists())
                self.assertEqual(result, [stop_resume.MoveHandedOff("sid-beta"), stop_resume.MoveHandedOff("019b3a40-4250-7000-8000-000000000102")])
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
                self.assertEqual(argv[argv.index("--cause") + 1], "auto")
                self.assertEqual(argv[argv.index("--target") + 1], "claude4")
                self.assertEqual(argv[-2:], ["beta", "alpha"])
        self.kill.assert_not_called()
        self.start.assert_not_called()

    def test_self_move_uses_the_current_session_of_a_saved_staff_reference(self) -> None:
        current = self.entry["session_id"]
        row = staff.StaffRow(staff.StaffId("fixed-alpha"), "producer", "alpha", staff.StaffRoot(),
                             current_session=staff.StaffId(current))
        saved = self.entry.copy()
        saved["session_id"] = row.id
        pane_record.save("agents", {saved["pane_id"]: saved})
        self.client.rows[0]["agent_session"] = None
        with (patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": current}),
              patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,)))):
            result = stop_resume.move_sessions(["alpha"], "claude4", why="limit")
        self.assertEqual(result, [stop_resume.MoveHandedOff(current)])
        self.detach.assert_called_once()
        self.kill.assert_not_called()
        self.start.assert_not_called()

    def test_detached_batch_uses_the_same_moves_and_logs_each_member(self) -> None:
        log = self.root / "batch.log"
        self.live.side_effect = [sessions.LiveSessionProcess(123), sessions.LiveSessionProcess(123),
                                 sessions.SessionNotLive(), sessions.SessionNotLive(), sessions.SessionNotLive()]
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "019b3a40-4250-7000-8000-000000000102"}), patch.object(subprocess, "run") as run, patch.object(subprocess, "Popen") as popen:
            result = stop_resume.main(["batch", "--target", "claude4", "--why", "limit", "--cause", "auto", "--log", str(log), "alpha", "missing"])
        self.assertEqual(result, 1)
        self.assertEqual(len(log.read_text().splitlines()), 2)
        self.assertIn('"result": "ClaudeUp"', log.read_text())
        self.assertIn('"result": "SessionNotFound"', log.read_text())
        self.assertEqual([entry.get("cause") for entry in moves.read_moves() if entry["kind"] == "requested"], ["auto", "auto"])
        self.start.assert_called_once()
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM)])
        self.seed.assert_called_once()
        run.assert_not_called()
        popen.assert_not_called()


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
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.NotRunning("019b3a40-4250-7000-8000-000000000102"))
        self.live.return_value = sessions.SessionLivenessUnknown("registry unreadable")
        identity = "019b3a40-4250-7000-8000-000000000102"
        reason = f"{identity} is not proved live or stopped: 1 processes unknown, first: "\
            + f"{identity} (Claude session unknown: registry unreadable)"
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.StopUnproved(identity, reason))
        self.assertEqual(stop_resume.stop("elsewhere"), stop_resume.SessionNotFound("elsewhere"))
        self.assertEqual(stop_resume.resume("elsewhere", why="maintenance"), stop_resume.ResumeFailed("elsewhere", "unknown name"))
        self.kill.assert_not_called()
        self.start.assert_not_called()

    def test_tab_labels_are_exact_and_unidentified_or_ambiguous_tabs_never_signal(self) -> None:
        self.assertEqual(stop_resume.stop("alph"), stop_resume.SessionNotFound("alph"))
        self.client.rows.append({"pane_id": "w1:p4", "label": "empty", "workspace_id": "w1"})
        self.assertEqual(stop_resume.stop("empty"), stop_resume.SessionNotFound("empty"))
        self.client.rows.append({"pane_id": "w1:p5", "label": "alpha", "workspace_id": "w1",
                                 "agent_session": {"kind": "id", "value": "sid-other"}})
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.StopFailed("alpha", "alpha: several tabs match"))
        self.assertEqual(stop_resume.stop("019b3a40-4250-7000-8000-000000000102"), stop_resume.NotRunning("019b3a40-4250-7000-8000-000000000102"))
        self.kill.assert_not_called()
        self.start.assert_not_called()

    def test_resume_leaves_a_live_session_on_its_account_without_starting(self) -> None:
        self.client.native_ids["w1:p2"] = self.entry["session_id"]
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.assertEqual(stop_resume.resume("alpha", why="maintenance"), stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude3"))
        self.start.assert_not_called()

    def test_resume_proves_the_native_id_before_submitting_the_opening_work(self) -> None:
        def observe(pane: str, kind: str, timeout: float) -> str:
            self.assertEqual((pane, kind), ("w1:p2", "claude"))
            self.assertGreater(timeout, 0)
            self.assertEqual(self.client.prompts, [])
            self.start.assert_called_once()
            arguments = self._typed(cast(tuple[str, ...], self.start.call_args.args))
            self.assertNotIn("Continue where you stopped", " ".join(arguments))
            self.assertNotIn("STAFF_ID=", " ".join(arguments))
            return self.entry["session_id"]

        with patch.object(self.client, "native_session", side_effect=observe):
            result = stop_resume.resume("alpha", why="maintenance")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertEqual(len(self.client.prompts), 1)
        pane, opening = self.client.prompts[0]
        self.assertEqual(pane, "w1:p2")
        assert isinstance(opening.transcript, herdr.SessionTranscript)
        self.assertEqual(opening.transcript.session_id, self.entry["session_id"])
        self.assertIn("maintenance", opening.text)
        self.assertIn("Continue where you stopped", opening.text)


    def test_resume_identity_mismatch_or_missing_proof_dispatches_no_work(self) -> None:
        for proof in ("019b3a40-4250-7000-8000-000000000199", herdr_types.HerdrFailure("registry unavailable")):
            with self.subTest(proof=proof):
                self.launched.clear()
                self.client.prompts.clear()
                self.start.reset_mock()
                errors = io.StringIO()
                identity = staff.StaffId(self.entry["session_id"])
                row = staff.StaffRow(identity, "unit", "alpha", staff.StaffRoot(), current_session=identity,
                             home=staff.SessionHome(self.root / ".claude3"))
                with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot((row,))),
                      patch.object(self.client, "native_session", return_value=proof if isinstance(proof, str) else "",
                                   side_effect=None if isinstance(proof, str) else proof),
                      redirect_stderr(errors)):
                    result = stop_resume.resume("alpha", why="maintenance")
                self.assertIsInstance(result, stop_resume.ClaudeUp)
                self.assertEqual(self.client.prompts, [])
                self.assertEqual(len(errors.getvalue().splitlines()), 1, errors.getvalue())
                self.assertIn("prompt identity anomaly:", errors.getvalue())
                self.detach.assert_called()

    def test_resume_retries_identity_until_proved_and_submits_one_prompt(self) -> None:
        errors = io.StringIO()
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(identity, "unit", "alpha", staff.StaffRoot(), current_session=identity,
                             home=staff.SessionHome(self.root / ".claude3"))
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(self.client, "native_session", side_effect=[
                  herdr_types.HerdrFailure("registry unavailable"), self.entry["session_id"]]) as observed,
              redirect_stderr(errors)):
            result = stop_resume.resume("alpha", why="maintenance")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertEqual(observed.call_count, 2)
        self.assertEqual(self.clock.elapsed, 0.5)
        self.assertEqual(len(self.client.prompts), 1)
        self.assertEqual(errors.getvalue(), "")

    def test_resume_retries_a_timed_out_identity_read_and_submits_one_prompt(self) -> None:
        errors = io.StringIO()
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(identity, "unit", "alpha", staff.StaffRoot(), current_session=identity,
                             home=staff.SessionHome(self.root / ".claude3"))
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(self.client, "native_session", side_effect=[
                  subprocess.TimeoutExpired("herdr pane process-info", 0.1), self.entry["session_id"]]) as observed,
              redirect_stderr(errors)):
            result = stop_resume.resume("alpha", why="maintenance")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertEqual(observed.call_count, 2)
        self.assertEqual(self.clock.elapsed, 0.5)
        self.assertEqual(len(self.client.prompts), 1)
        self.assertEqual(errors.getvalue(), "")

    def test_resume_reports_identity_failure_once_at_the_deadline(self) -> None:
        errors = io.StringIO()
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(identity, "unit", "alpha", staff.StaffRoot(), current_session=identity,
                             home=staff.SessionHome(self.root / ".claude3"))
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(self.client, "native_session", side_effect=herdr_types.HerdrFailure(
                  "registry unavailable")) as observed,
              redirect_stderr(errors)):
            result = stop_resume.resume("alpha", why="maintenance")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertGreater(observed.call_count, 1)
        self.assertEqual(self.clock.elapsed, conf.read("shutdown").number("START_SECONDS"))
        self.assertEqual(self.client.prompts, [])
        self.assertEqual(errors.getvalue().splitlines(), [
            f"{self.entry['session_id']}: prompt identity anomaly: registry unavailable"])
        self.detach.assert_called()

    def test_resume_prepares_the_transcript_cursor_before_launch(self) -> None:
        path = Path(str(cast(staff.SessionHome, pane_launch.config_home(self.entry)).path)) / "projects" / "fixture" / f"{self.entry['session_id']}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text('{"type":"assistant","message":{"content":"old context"}}\n')
        before = path.stat().st_size

        def start(*_command: str) -> str:
            with path.open("a") as stream:
                _ = stream.write(json.dumps({
                    "type": "user",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "message": {"role": "user", "content": "launch-time work"},
                }) + "\n")
            return ""

        self.start.side_effect = start
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(identity, "unit", "alpha", staff.StaffRoot(), current_session=identity)
        with patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))):
            result = stop_resume.resume("alpha", why="maintenance")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertEqual(len(self.client.prompts), 1)
        cursor = self.client.prompts[0][1].cursor
        assert isinstance(cursor, herdr.TranscriptCursor)
        self.assertEqual(cursor.offsets[path], before)
        self.assertEqual(len(cursor.read_turns()), 1)

    def test_resume_identity_mismatch_dispatches_no_work_without_retry(self) -> None:
        proof = "019b3a40-4250-7000-8000-000000000199"
        errors = io.StringIO()
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(identity, "unit", "alpha", staff.StaffRoot(), current_session=identity,
                             home=staff.SessionHome(self.root / ".claude3"))
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(self.client, "native_session", return_value=proof) as observed,
              redirect_stderr(errors)):
            result = stop_resume.resume("alpha", why="maintenance")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertEqual(self.client.prompts, [])
        self.assertEqual(errors.getvalue().splitlines(), [
            f"{identity}: prompt identity anomaly: observed {proof}"])
        observed.assert_called_once()
        self.assertEqual(self.clock.elapsed, 0)
        self.detach.assert_called()

    def test_resume_worker_uses_its_own_busy_pass_and_leaves_idle_workers_without_input(self) -> None:
        from scripts.direct import progress_history
        from scripts.production import worker

        parent = staff.StaffRow(staff.StaffId("director"), "unit", "director", staff.STAFF_ROOT,
                                current_session=staff.StaffId("native-director"))
        row = staff.StaffRow(staff.StaffId("fixed-worker"), "worker", "alpha", staff.ReportsTo(parent.id),
                             current_session=staff.StaffId(self.entry["session_id"]))
        record = staff.StaffSnapshot((parent, row))
        for active in (progress_history.ActiveDispatch("own-dispatch", ("assign.sh",)), progress_history.NoActiveDispatch()):
            with (self.subTest(active=active),
                  patch.object(staff, "initial_snapshot", return_value=record),
                  patch.object(worker_panes, "worker_run", return_value=self.root) as run,
                  patch.object(progress_history, "active_dispatch", return_value=active) as read,
                  patch.object(worker, "resume_receipt", return_value=worker.DispatchSubmitted())):
                self.launched.clear()
                self.client.prompts.clear()
                result = stop_resume.resume(str(row.id), why="maintenance")
                self.assertIsInstance(result, stop_resume.ClaudeUp)
                if isinstance(active, progress_history.ActiveDispatch):
                    self.assertEqual(len(self.client.prompts), 1)
                    self.assertIn("Continue pass own-dispatch in this conversation", self.client.prompts[0][1].text)
                    self.assertNotIn("Worker ", self.client.prompts[0][1].text)
                else:
                    self.assertEqual(self.client.prompts, [])
                run.assert_called_once_with(row, record=record)
                read.assert_called_once_with(row.id, self.root)

    def test_named_resume_keeps_the_saved_assignment_when_the_worker_never_received_it(self) -> None:
        from scripts.direct import progress_history
        from scripts.production import worker

        parent = staff.StaffRow(staff.StaffId("director"), "unit", "director", staff.STAFF_ROOT,
                                current_session=staff.StaffId("native-director"))
        row = staff.StaffRow(staff.StaffId("fixed-worker"), "worker", "alpha", staff.ReportsTo(parent.id),
                             current_session=staff.StaffId(self.entry["session_id"]))
        record = staff.StaffSnapshot((parent, row))
        with (patch.object(staff, "initial_snapshot", return_value=record),
              patch.object(worker_panes, "worker_run", return_value=self.root),
              patch.object(progress_history, "active_dispatch", return_value=progress_history.ActiveDispatch(
                  "own-dispatch", ("bash", "assign.sh", "saved prompt.md"))),
              patch.object(worker, "resume_receipt", return_value=worker.DispatchNotSubmitted())):
            result = stop_resume.resume("alpha", why="maintenance")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        text = "\n".join(prompt.text for _pane, prompt in self.client.prompts)
        self.assertIn("bash assign.sh 'saved prompt.md'", text)
        self.assertNotIn("Continue pass", text)

    def test_resume_director_submits_an_opening_prompt_with_idle_pane_workers(self) -> None:
        from scripts.lib.resume_message import IdleWorker, PaneWorkerResume, ResumeWorkers
        from scripts.production import worker

        row = staff.StaffRow(staff.StaffId("director"), "unit", "alpha", staff.STAFF_ROOT,
                             current_session=staff.StaffId(self.entry["session_id"]))
        workers = ResumeWorkers(panes=[PaneWorkerResume(staff.StaffId("idle-worker"), IdleWorker())])
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(worker, "resume_workers", return_value=workers)):
            result = stop_resume.resume(str(row.id), why="maintenance")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertEqual(len(self.client.prompts), 1)
        text = self.client.prompts[0][1].text
        self.assertIn("You were stopped and resumed because maintenance.", text)
        self.assertIn("for messages kept while you were down", text)
        self.assertIn("Worker idle-worker: idle", text)

    def test_empty_resume_message_adds_no_argument_in_the_current_pane(self) -> None:
        tab = herdr.TabSession("w1:p2", "alpha", "w1", self.entry["session_id"])
        with (patch.object(os, "chdir"), patch.object(os, "execvpe", side_effect=SystemExit) as execute):
            with self.assertRaises(SystemExit):
                stop_resume._exec_in_own_pane(tab, self.entry, "claude3", "")  # pyright: ignore[reportPrivateUsage]
        self.assertNotIn("", cast(list[str], execute.call_args.args[1]))

    def test_resume_runs_account_exec_in_its_own_pane_with_one_message(self) -> None:
        self.entry["environment"].update(HERDR_PANE_ID="w9:p9", HERDR_WORKSPACE_ID="w9")
        pane_record.write("agents", self.entry)
        result = stop_resume.resume("alpha", why="maintenance")
        self.assertEqual(result, stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude3"))
        self.start.assert_called_once()
        command = self._typed(cast(tuple[str, ...], self.start.call_args.args))
        self.assertEqual(command[:4], ("pane", "run", "w1:p2", "env"))
        self.assertFalse(any(part.startswith("SHOWRUNNER_") for part in command))
        self.assertIn("HERDR_PANE_ID=w1:p2", command)
        self.assertEqual(command[4:6], ("-u", "HERDR_TAB_ID"))
        self.assertIn("HERDR_WORKSPACE_ID=w1", command)
        self.assertLess(self.clock.elapsed, 1.0)
        self.detach.assert_called_once()
        index = command.index("exec")
        self.assertTrue(command[index - 1].endswith("accounts/accounts.py"))
        self.assertEqual(command[index + 1:], ("claude3", "--resume", "019b3a40-4250-7000-8000-000000000102", "-n", "alpha",
            "--remote-control", "alpha", "--model", "sonnet", "--settings", '{"disableAgentView": true}'))
        self.assertEqual(len(self.client.prompts), 1)
        pane, opening = self.client.prompts[0]
        self.assertEqual(pane, "w1:p2")
        self.assertIn("stopped and resumed", opening.text)
        self.assertIn("maintenance", opening.text)
        self.assertIn("Continue where you stopped", opening.text)
        self.assertEqual(self.client.closed, [])
        self.assertEqual(self.client.started, [])


    def test_resume_returns_before_the_stay_window_and_detaches_its_generation(self) -> None:
        result = stop_resume.resume("alpha", why="maintenance")
        self.assertLess(self.clock.elapsed, conf.read("shutdown").number("STAY_SECONDS"))
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        arguments = cast(list[str], self.detach.call_args.args[0])
        self.assertEqual(arguments[:3], ["stay", "--session", "019b3a40-4250-7000-8000-000000000102"])
        self.assertEqual(arguments[arguments.index("--pid") + 1], "456")
        self.assertIn("generation-1", arguments)
        self.assertEqual(arguments[arguments.index("--why") + 1], "maintenance")
        self.start.assert_called_once()

    def run_stay_child(self, *, requester: str = "", why: str = "usage limit") -> tuple[int, str]:
        def exits_soon(_pid: int, _generation: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
            return sessions.LiveSessionProcess(456) if self.clock.elapsed < 0.6 else sessions.SessionNotLive()
        self.process.side_effect = exits_soon
        log = self.root / "stay.log"
        result = stop_resume.main(["stay", "--session", "019b3a40-4250-7000-8000-000000000102", "--pane", "w1:p2",
            "--account", "claude3", "--pid", "456", "--generation", "generation-1",
            "--requester", requester, "--why", why, "--log", str(log)])
        return result, log.read_text(encoding="utf-8")

    def test_detached_stay_check_alerts_on_early_exit_without_restarting(self) -> None:
        from scripts.whoami import quota_alert
        with patch.object(quota_alert, "tell_user") as push:
            result, log = self.run_stay_child()
        self.assertEqual(result, 1)
        self.assertIn("stopped 0.6 s", log)
        self.assertLess(self.clock.elapsed, conf.read("shutdown").number("STAY_SECONDS"))
        push.assert_called_once()
        self.start.assert_not_called()

    def test_early_exit_tells_showrunner_pushes_except_probe_and_enters_watchdog(self) -> None:
        from scripts.production import showrunners, staff_sources
        from scripts.accounts import unit_watchdog
        from scripts.notify.push_texts import RenderedPush
        from scripts.whoami import quota_alert
        identity = staff.StaffId(self.entry["session_id"])
        owner = staff.StaffRow(identity, "unit", "alpha", staff.StaffRoot(), current_session=identity)
        _ = self.enterContext(patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((owner,))))
        for probe in (False, True):
            for outcome in ("watchdog: alpha restarted on claude3",
                            "watchdog: alpha: folder no longer exists; restore dropped"):
                with self.subTest(probe=probe, outcome=outcome):
                    self.entry["environment"]["CLAUDE_PROBE_SESSION"] = "1" if probe else "0"
                    pane_record.write("agents", self.entry)
                    with (patch.object(staff_sources, "unit_parent", return_value=staff_sources.UnitProduction("demo", str(self.root / "production.md"))),
                          patch.object(showrunners, "tell", return_value="told demo") as tell,
                          patch.object(quota_alert, "tell_user") as push,
                          patch.object(unit_watchdog, "run", return_value=[outcome]) as recovery):
                        result, log = self.run_stay_child(requester="other")
                        self.assertEqual(result, 1)
                        tell.assert_called_once()
                        self.assertEqual(tell.call_args.args[0], "demo")
                        line = cast(str, tell.call_args.args[2])
                        self.assertEqual(line, f"alpha stopped 0.6 s after its move or resume to claude3; {outcome}")
                        self.assertIn(line, log)
                        self.assertEqual(push.call_count, 0 if probe else 1)
                        if not probe:
                            self.assertEqual(cast(RenderedPush, push.call_args.args[0]).values["cause"], line)
                        recovery.assert_called_once()
                    self.clock.elapsed = 0.0

    def test_named_probe_reports_rounded_time_account_retry_and_shutdown_sender(self) -> None:
        from scripts.production import showrunners
        from scripts.whoami import quota_alert
        self.entry["environment"]["CLAUDE_PROBE_SESSION"] = "1"
        pane_record.write("agents", self.entry)
        with (patch.object(showrunners, "tell", _REAL_TELL),
              patch.object(showrunners, "current_address", return_value="session:requester"),
              patch.object(showrunners, "send_to_address", return_value=showrunners.NoticeDelivery(("told requester",))) as send,
              patch.object(quota_alert, "tell_user") as push):
            result, log = self.run_stay_child(requester="requester")
        self.assertEqual(result, 1)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], "session:requester")
        self.assertEqual(send.call_args.kwargs, {"recipient": "requester", "sender": "shutdown"})
        line = cast(str, send.call_args.args[2])
        self.assertEqual(line, "alpha stopped 0.6 s after its move or resume to claude3; resume it: "
                         + "agents resume alpha --to claude3 --why 'usage limit'")
        self.assertIn(line, log)
        push.assert_not_called()

    def test_unnamed_session_uses_its_id_in_alert_and_retry(self) -> None:
        from scripts.production import showrunners
        from scripts.notify.push_texts import RenderedPush
        from scripts.whoami import quota_alert
        self.entry["name"] = ""
        self.client.rows[0]["label"] = None
        pane_record.write("agents", self.entry)
        with (patch.object(showrunners, "tell", return_value="told requester") as tell,
              patch.object(quota_alert, "tell_user") as push):
            result, log = self.run_stay_child(requester="requester")
        self.assertEqual(result, 1)
        line = cast(str, tell.call_args.args[2])
        self.assertEqual(line, "019b3a40-4250-7000-8000-000000000102 stopped 0.6 s after its move or resume to claude3; resume it: "
                         + "agents resume 019b3a40-4250-7000-8000-000000000102 --to claude3 --why 'usage limit'")
        self.assertIn(line, log)
        self.assertEqual(cast(RenderedPush, push.call_args.args[0]).values["cause"], line)

    def test_no_requester_or_unit_never_tells_showrunners_and_only_pushes_non_probes(self) -> None:
        from scripts.production import showrunners
        from scripts.whoami import quota_alert
        for probe in (False, True):
            with self.subTest(probe=probe):
                self.entry["environment"]["CLAUDE_PROBE_SESSION"] = "1" if probe else "0"
                pane_record.write("agents", self.entry)
                with (patch.object(showrunners, "tell") as tell,
                      patch.object(showrunners, "tell_others") as broadcast,
                      patch.object(quota_alert, "tell_user") as push):
                    result, log = self.run_stay_child()
                self.assertEqual(result, 1)
                self.assertIn("alpha stopped 0.6 s", log)
                tell.assert_not_called()
                broadcast.assert_not_called()
                self.assertEqual(push.call_count, 0 if probe else 1)
                self.clock.elapsed = 0.0

    def test_non_unit_early_exit_tells_only_the_requesting_showrunner(self) -> None:
        from scripts.production import showrunners
        from scripts.whoami import quota_alert
        requester = fixture_member("showrunner", name="requester",
            runtime_id="019b3a40-4250-7000-8000-000000000201")
        with (patch.object(staff, "runtime_id", return_value=requester.current_session),
              patch.object(stop_resume, "_detach", wraps=self.real_detach) as detach,
              patch.object(subprocess, "run"), patch.object(subprocess, "Popen"),
              patch.object(showrunners, "tell") as tell,
              patch.object(showrunners, "tell_others") as broadcast,
              patch.object(quota_alert, "tell_user") as push):
            _ = stop_resume.resume("alpha", why="maintenance")
            invocation = cast(list[str], (detach.call_args.args[0]))
            # Read the actual detached command, after _detach appends the requester.
            platform_call = cast(Mock, subprocess.Popen if sys.platform == "darwin" else subprocess.run)
            command = cast(list[str], platform_call.call_args.args[0])
            arguments = command[command.index("stay"):]
            self.assertEqual(invocation[0], "stay")
            self.assertIn("--requester", arguments)
            self.assertEqual(arguments[arguments.index("--requester") + 1], "requester")
            log = self.root / "stay.log"
            self.process.return_value = sessions.SessionNotLive()
            arguments[arguments.index("--log") + 1] = str(log)
            result = stop_resume.main(arguments)
            self.assertEqual(result, 1)
            tell.assert_called_once()
            self.assertEqual(tell.call_args.args[0], "requester")
            broadcast.assert_not_called()
            push.assert_called_once()

    def test_detached_move_keeps_the_requester_for_its_stay_observation(self) -> None:
        log = self.root / "requested-batch.log"
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sid-requester"}):
            result = stop_resume.main(["batch", "--requester", "requester", "--target", "claude4",
                                       "--why", "limit", "--log", str(log), "alpha"])
            self.assertEqual(os.environ.get("CLAUDE_CODE_SESSION_ID", ""), "")
        self.assertEqual(result, 0)
        self.confirm.assert_called_once_with(stop_resume.ClaudeUp(self.entry["session_id"], "w1:p2", "claude4"),
                                             "requester", "limit")
        self.detach.assert_not_called()

    def test_saved_entry_finds_an_unlabelled_pane_with_empty_or_stale_herdr_id(self) -> None:
        self.client.rows[0]["label"] = None
        for agent in (None, {"kind": "id", "value": "stale-session"}):
            with self.subTest(agent=agent):
                self.launched.clear()
                self.client.rows[0]["agent_session"] = agent
                self.assertEqual(stop_resume.resume("019b3a40-4250-7000-8000-000000000102", why="maintenance"),
                                 stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude3"))

    def test_saved_entry_never_selects_a_different_live_conversation(self) -> None:
        self.client.rows[0]["agent_session"] = {"kind": "id", "value": "another-live-session"}
        self.live.return_value = sessions.LiveSessionProcess(789)
        self.assertEqual(stop_resume.resume("019b3a40-4250-7000-8000-000000000102", why="maintenance"), stop_resume.ResumeFailed("019b3a40-4250-7000-8000-000000000102", "unknown name"))
        self.assertEqual(stop_resume.stop("019b3a40-4250-7000-8000-000000000102"), stop_resume.SessionNotFound("019b3a40-4250-7000-8000-000000000102"))
        self.start.assert_not_called()
        self.kill.assert_not_called()

    def test_saved_entries_for_closed_panes_do_not_create_ambiguous_matches(self) -> None:
        missing = self.entry.copy()
        missing["pane_id"] = "w1:p-missing"
        pane_record.write("agents", missing)
        self.assertEqual(stop_resume.resume("019b3a40-4250-7000-8000-000000000102", why="maintenance"),
                         stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude3"))


    def test_move_stops_then_runs_the_target_account_in_the_same_pane(self) -> None:
        identity = "019b3a40-4250-7000-8000-000000000102"
        self.entry["environment"]["STAFF_ID"] = identity
        pane_record.write("agents", self.entry)
        tab = dict(self.client.rows[0])
        self.live.side_effect = [sessions.LiveSessionProcess(123), sessions.LiveSessionProcess(123),
                                 sessions.SessionNotLive(), sessions.SessionNotLive()]
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        result = stop_resume.move("alpha", "claude4", why="weekly limit")
        self.assertEqual(result, stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude4"))
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM)])
        command = self._typed(cast(tuple[str, ...], self.start.call_args.args))
        self.assertEqual(command[command.index("exec") + 1], "claude4")
        self.assertEqual(len(self.client.prompts), 1)
        pane, opening = self.client.prompts[0]
        self.assertEqual(pane, "w1:p2")
        self.assertIn("moved from claude3 to claude4", opening.text)
        self.assertIn("weekly limit", opening.text)
        self.assertNotIn("the herdr server restarted", opening.text)
        self.assertNotIn(f"STAFF_ID={identity}", command)
        self.assertIn(identity, command)
        environment = dict(value.split("=", 1) for value in command[command.index("env") + 1:command.index("exec")] if "=" in value)
        refresh = pane_record.launch("w1:p2", "019b3a40-4250-7000-8000-000000000102", str(self.root),
                                     environment, ["claude", "--resume", "019b3a40-4250-7000-8000-000000000102"])
        pane_record.write("agents", refresh)
        self.assertEqual(pane_record.read("agents")["w1:p2"]["session_id"], identity)
        self.assertNotIn("STAFF_ID", pane_record.read("agents")["w1:p2"]["environment"])
        self.assertEqual(self.client.rows[0], tab)
        self.assertEqual(self.client.closed, [])
        self.assertEqual(self.client.started, [])

    def test_move_of_a_stopped_session_resumes_but_a_failed_stop_does_not(self) -> None:
        self.assertEqual(stop_resume.move("alpha", "claude4", why="limit"), stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude4"))
        self.start.reset_mock()
        self.launched.clear()
        self.live.return_value = sessions.SessionLivenessUnknown("identity unreadable")
        identity = "019b3a40-4250-7000-8000-000000000102"
        reason = f"{identity} is not proved live or stopped: 1 processes unknown, first: "\
            + f"{identity} (Claude session unknown: identity unreadable)"
        self.assertEqual(stop_resume.move("alpha", "claude4", why="limit"), stop_resume.StopUnproved(identity, reason))
        self.start.assert_not_called()
        self.kill.assert_not_called()

    def test_a_failed_pane_run_never_reports_resume_success(self) -> None:
        self.start.side_effect = herdr_types.HerdrFailure("pane run refused")
        self.assertEqual(stop_resume.resume("alpha", why="maintenance"), stop_resume.ResumeFailed("019b3a40-4250-7000-8000-000000000102", "pane run refused"))
        self.assertEqual(self.client.closed, [])

    def test_move_records_the_callers_process_account_instead_of_a_stale_source(self) -> None:
        identity = self.entry["session_id"]
        up = stop_resume.ClaudeUp(identity, "w1:p2", "claude4")
        with (patch.object(stop_resume, "running_on_account", return_value=stop_resume.ClaudeUp(identity, "w1:p2", "claude3")),
              patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning(identity)),
              patch.object(stop_resume, "resume", return_value=up) as resume,
              patch.object(stop_resume, "_confirm_move_start", _REAL_CONFIRM_MOVE_START),
              patch.object(sessions, "session_liveness", return_value=sessions.LiveSessionProcess(456)),
              patch.object(stop_resume, "_same_process", return_value=True)):
            self.assertEqual(stop_resume.move_sessions([identity], "claude4", why="auto: head", cause="auto",
                source_accounts={identity: "claude1"}), [up])
        request = moves.read_moves()[0]
        assert request["kind"] == "requested"
        self.assertEqual((request["from"], request["to"]), ("claude1", "claude4"))
        resume.assert_called_once_with(identity, why="auto: head", to="claude4")

    def test_move_records_and_reports_an_immediate_resume_exit(self) -> None:
        identity = self.entry["session_id"]
        up = stop_resume.ClaudeUp(identity, "w1:p2", "claude4")
        with (patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning(identity)),
              patch.object(stop_resume, "resume", return_value=up),
              patch.object(stop_resume, "_confirm_move_start", _REAL_CONFIRM_MOVE_START),
              patch.object(sessions, "session_liveness", return_value=sessions.LiveSessionProcess(456)),
              patch.object(stop_resume, "_same_process", return_value=False),
              patch.object(showrunners, "tell", return_value="told director") as tell):
            result = stop_resume.move_sessions([identity], "claude4", why="auto: head", requester="director")
        self.assertIsInstance(result[0], stop_resume.ResumePending)
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["requested", "failed"])
        failed = entries[-1]
        assert failed["kind"] == "failed"
        self.assertIn("stopped 0.0 s after its move or resume to claude4", failed["reason"])
        tell.assert_called_once()
        self.assertEqual(tell.call_args.args[0], "director")
        self.assertIn("claude4", str(cast(object, tell.call_args.args[2])))

    def test_move_records_and_reports_a_resume_failure(self) -> None:
        identity = self.entry["session_id"]
        with (patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning(identity)),
              patch.object(stop_resume, "resume", return_value=stop_resume.ResumeFailed(identity, "launch failed")),
              patch.object(showrunners, "tell", return_value="told director") as tell):
            result = stop_resume.move_sessions([identity], "claude4", why="auto: head", requester="director")
        self.assertIsInstance(result[0], stop_resume.ResumePending)
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["requested", "failed"])
        failed = entries[-1]
        assert failed["kind"] == "failed"
        self.assertEqual(failed["reason"], "launch failed")
        tell.assert_called_once()
        self.assertIn("resume on claude4 failed: launch failed", str(cast(object, tell.call_args.args[2])))

    def test_each_stopped_unit_gets_the_target_or_a_reported_failed_outcome(self) -> None:
        names = ["devtools", "fix-rounds", "style", "verify", "natest", "queue"]

        def stop_unit(identity: str) -> stop_resume.Stopped:
            return stop_resume.Stopped(identity, 123)

        def resume_unit(identity: str, *, why: str, to: str, requester: str) -> stop_resume.ResumeFailed:
            self.assertEqual((why, to, requester), ("auto: head", "claude5", "director"))
            return stop_resume.ResumeFailed(identity, "session did not start")

        with (patch.object(stop_resume, "_stop", side_effect=stop_unit),
              patch.object(stop_resume, "resume", side_effect=resume_unit) as resumed,
              patch.object(showrunners, "tell", return_value="fixture report") as tell):
            results = stop_resume.move_sessions(names, "claude5", why="auto: head", requester="director", cause="auto")
        self.assertEqual(resumed.call_count, len(names))
        self.assertEqual(tell.call_count, len(names))
        self.assertEqual(results, [stop_resume.ResumePending(name, "claude5", "session did not start") for name in names])
        entries = moves.read_moves()
        for name in names:
            trail = [entry for entry in entries if entry["subject"] == name]
            self.assertEqual([entry["kind"] for entry in trail], ["requested", "failed"])
            self.assertEqual(trail[0]["move_id"], trail[1]["move_id"])
        self.assertTrue(all("resume on claude5 failed" in str(cast(object, invocation.args[2]))
                            for invocation in tell.call_args_list))

    def test_move_logs_the_live_source_and_final_account_with_its_cause(self) -> None:
        identity = self.entry["session_id"]
        row = staff.StaffRow(staff.StaffId("fixed-alpha"), "producer", "alpha", staff.StaffRoot(),
                             current_session=staff.StaffId(identity))
        self.entry["config_dir"] = str(self.root / ".claude1")
        pane_record.write("agents", self.entry)

        def stop_after_request(_session: str) -> stop_resume.NotRunning:
            self.assertEqual([entry["kind"] for entry in moves.read_moves()], ["requested"])
            return stop_resume.NotRunning(identity)

        up = stop_resume.ClaudeUp(identity, "w1:p2", "claude4")
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(stop_resume, "running_on_account", return_value=stop_resume.ClaudeUp(identity, "w1:p2", "claude3")),
              patch.object(stop_resume, "_stop", side_effect=stop_after_request),
              patch.object(stop_resume, "resume", return_value=up)):
            self.assertEqual(stop_resume.move_sessions(["alpha"], "claude4", why="weekly limit", cause="auto"), [up])
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["requested", "confirmed"])
        request, confirmed = entries
        assert request["kind"] == "requested" and confirmed["kind"] == "confirmed"
        self.assertEqual(request["move_id"], confirmed["move_id"])
        self.assertEqual(len(request["move_id"]), 32)
        self.assertEqual((request["subject"], confirmed["subject"]), (f"{identity}: alpha", f"{identity}: alpha"))
        self.assertEqual((request["tool"], request["from"], request["to"], request.get("cause"), request["window"]),
                         ("claude", "claude3", "claude4", "auto", "unknown"))
        self.assertEqual(request["why"], "weekly limit")
        self.assertEqual(datetime.fromisoformat(request["at"]).utcoffset(), timezone.utc.utcoffset(None))
        self.assertEqual(confirmed.get("worker"), "alpha")
        self.assertNotIn("config_dir", confirmed)
        detail = accounts._move_status_lines(entries, timezone.utc)  # pyright: ignore[reportPrivateUsage]
        self.assertIn("(auto)", detail[0])
        self.assertIn("confirmed alpha", detail[0])

    def test_move_logs_failures_and_uses_the_last_launch_for_a_stopped_session(self) -> None:
        identity = self.entry["session_id"]
        for failed in (stop_resume.StopFailed(identity, "cannot stop"),
                       stop_resume.ResumeFailed(identity, "cannot start"), stop_resume.SessionNotFound(identity)):
            with self.subTest(result=failed):
                if moves.store_file().exists():
                    moves.store_file().unlink()
                stop = failed if isinstance(failed, (stop_resume.StopFailed, stop_resume.SessionNotFound)) else stop_resume.NotRunning(identity)
                with (patch.object(stop_resume, "_stop", return_value=stop),
                      patch.object(stop_resume, "resume", return_value=failed)):
                    _ = stop_resume.move_sessions(["alpha"], "claude4", why="move")
                request, outcome = moves.read_moves()
                assert request["kind"] == "requested" and outcome["kind"] == "failed"
                self.assertEqual(request["from"], "claude3")
                self.assertEqual(request["subject"], "alpha")
                self.assertEqual(request.get("cause"), "by hand")
                self.assertEqual(request["move_id"], outcome["move_id"])
                self.assertEqual(outcome["reason"], f"{identity}: session not found" if isinstance(failed, stop_resume.SessionNotFound) else failed.reason)

    def test_move_without_a_live_process_or_saved_account_logs_an_unresolved_home(self) -> None:
        pane_record.save("agents", {})
        with patch.object(stop_resume, "_stop", return_value=stop_resume.SessionNotFound("missing")):
            _ = stop_resume.move_sessions(["missing"], "claude4", why="move")
        request = moves.read_moves()[0]
        assert request["kind"] == "requested"
        self.assertEqual(request["from"], moves.HomeAccountUnresolved(self.root / ".claude"))

    def test_move_already_on_the_target_does_not_write_the_log(self) -> None:
        identity = self.entry["session_id"]
        up = stop_resume.ClaudeUp(identity, "w1:p2", "claude4")
        with (patch.object(stop_resume, "running_on_account", return_value=up),
              patch.object(stop_resume, "_stop") as stopped):
            self.assertEqual(stop_resume.move_sessions(["alpha"], "claude4", why="rerun"), [up])
        self.assertFalse(moves.store_file().exists())
        stopped.assert_not_called()

    def test_batch_logs_each_session_independently(self) -> None:
        def stop_one(identity: str) -> stop_resume.StopFailed | stop_resume.NotRunning:
            return stop_resume.StopFailed(identity, "cannot stop") if identity == "bad" else stop_resume.NotRunning(identity)

        with (patch.object(stop_resume, "_stop", side_effect=stop_one),
              patch.object(stop_resume, "resume", return_value=stop_resume.ClaudeUp("good", "own-pane", "claude4"))):
            _ = stop_resume.move_sessions(["bad", "good"], "claude4", why="move", cause="auto")
        entries = moves.read_moves()
        for subject, kind in (("bad", "failed"), ("good", "confirmed")):
            trail = [entry for entry in entries if entry["subject"] == subject]
            self.assertEqual([entry["kind"] for entry in trail], ["requested", kind])
            self.assertEqual(trail[0]["move_id"], trail[1]["move_id"])
        self.assertNotEqual(entries[0]["move_id"], entries[1]["move_id"])

    def test_move_log_write_errors_do_not_stop_the_move(self) -> None:
        identity = self.entry["session_id"]
        up = stop_resume.ClaudeUp(identity, "w1:p2", "claude4")
        with (patch.object(moves, "append_move", side_effect=OSError("disk failed")) as append,
              patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning(identity)),
              patch.object(stop_resume, "resume", return_value=up) as resumed,
              redirect_stderr(io.StringIO()) as output):
            self.assertEqual(stop_resume.move_sessions(["alpha"], "claude4", why="move"), [up])
        resumed.assert_called_once()
        self.assertEqual(append.call_count, 1)
        self.assertIn(f"{identity}: move log anomaly: disk failed", output.getvalue())

    def test_launch_names_read_failure_does_not_stop_the_move(self) -> None:
        identity = self.entry["session_id"]
        up = stop_resume.ClaudeUp(identity, "w1:p2", "claude4")
        with (patch.object(stop_resume, "_tab_of", return_value=stop_resume.SessionNotFound(identity)),
              patch.object(LaunchNames, "read", side_effect=RuntimeError("names failed")) as read,
              patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning(identity)),
              patch.object(stop_resume, "resume", return_value=up) as resumed,
              redirect_stderr(io.StringIO()) as output):
            self.assertEqual(stop_resume.move_sessions([identity], "claude4", why="move"), [up])
        read.assert_called_once()
        resumed.assert_called_once()
        self.assertFalse(moves.store_file().exists())
        self.assertEqual(output.getvalue(), f"{identity}: move log anomaly: names failed\n")

    def test_outcome_write_failure_does_not_lose_the_batch_results(self) -> None:
        results = [stop_resume.ClaudeUp("bad", "bad-pane", "claude3"),
                   stop_resume.ClaudeUp("good", "good-pane", "claude4")]

        def resume_one(identity: str, *, why: str, to: str) -> stop_resume.ClaudeUp:
            self.assertEqual((why, to), ("move", "claude4"))
            return results[0] if identity == "bad" else results[1]

        append = moves.append_move
        def fail_one(entry: moves.MoveEntry) -> None:
            if entry["kind"] == "confirmed" and entry["subject"] == "bad":
                raise OSError("TEST receipt write failed")
            append(entry)

        with (patch.object(moves, "append_move", side_effect=fail_one),
              patch.object(stop_resume, "_stop", side_effect=stop_resume.NotRunning),
              patch.object(stop_resume, "resume", side_effect=resume_one),
              redirect_stderr(io.StringIO()) as output):
            self.assertEqual(stop_resume.move_sessions(["bad", "good"], "claude4", why="move"), results)
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["requested", "requested", "confirmed"])
        self.assertEqual(entries[-1]["subject"], "good")
        self.assertIn("bad: move log anomaly:", output.getvalue())
        self.assertEqual(len(output.getvalue().splitlines()), 3)
        self.assertEqual(output.getvalue().count("session home is unknown"), 2)

    def test_batch_stops_all_before_any_resume_and_uses_each_own_pane(self) -> None:
        self.add_beta()
        third = pane_record.launch("w1:p6", "sid-gamma", str(self.root),
            {"CLAUDE_CONFIG_DIR": str(self.root / ".claude3")}, ["claude", "-n", "gamma"])
        pane_record.write("agents", third)
        self.client.rows.append({"pane_id": "w1:p6", "terminal_id": "gamma-terminal", "label": "gamma", "workspace_id": "w1",
                                 "agent_session": {"kind": "id", "value": "sid-gamma"}})
        both_stopping = Barrier(3, timeout=3)
        both_resuming = Barrier(3, timeout=3)
        stopped: list[str] = []
        lock = Lock()
        def stop_one(session: str) -> stop_resume.Stopped:
            _ = both_stopping.wait()
            with lock:
                stopped.append(session)
            return stop_resume.Stopped(session, 123)
        def run(*_command: str) -> str:
            self.assertCountEqual(stopped, ["019b3a40-4250-7000-8000-000000000102", "sid-beta", "sid-gamma"])
            _ = both_resuming.wait()
            return ""
        self.start.side_effect = run
        with patch.object(stop_resume, "_stop", side_effect=stop_one):
            results = stop_resume.move_sessions(["019b3a40-4250-7000-8000-000000000102", "sid-beta", "sid-gamma"], "claude4", why="limit")
        self.assertEqual(results, [stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude4"),
                                   stop_resume.ClaudeUp("sid-beta", "w1:p4", "claude4"),
                                   stop_resume.ClaudeUp("sid-gamma", "w1:p6", "claude4")])
        calls = [self._typed(cast(tuple[str, ...], invocation.args)) for invocation in self.start.call_args_list]
        self.assertEqual({args[2]: args[args.index("--resume") + 1] for args in calls},
                         {"w1:p2": "019b3a40-4250-7000-8000-000000000102", "w1:p4": "sid-beta", "w1:p6": "sid-gamma"})

    def test_batch_failures_do_not_stop_other_members(self) -> None:
        def stop_one(session: str) -> stop_resume.NotRunning:
            if session == "stop-error":
                raise OSError("cannot signal")
            return stop_resume.NotRunning(session)
        def resume_one(session: str, *, why: str, to: str) -> stop_resume.ClaudeUp | stop_resume.SessionNotFound:
            self.assertEqual(why, "limit")
            if session == "resume-error":
                raise OSError("cannot run")
            if session == "gone-tab":
                return stop_resume.SessionNotFound(session)
            self.launched.add(session)
            return stop_resume.ClaudeUp(session, "own-pane", to)
        with patch.object(stop_resume, "_stop", side_effect=stop_one), patch.object(stop_resume, "resume", side_effect=resume_one):
            results = stop_resume.move_sessions(["stop-error", "resume-error", "gone-tab", "good"], "claude4", why="limit")
        self.assertEqual(results, [stop_resume.StopFailed("stop-error", "cannot signal"),
            stop_resume.ResumePending("resume-error", "claude4", "cannot run"),
            stop_resume.ResumePending("gone-tab", "claude4", "session has no tab after stop"), stop_resume.ClaudeUp("good", "own-pane", "claude4")])

    def test_cli_prints_one_line_and_uses_the_result_exit_status(self) -> None:
        for arguments, expected in ((["stop", "alpha"], 0), (["resume", "alpha", "--why", "maintenance"], 0),
                                    (["stop", "elsewhere"], 2)):
            with self.subTest(arguments=arguments), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(shutdown.main(arguments), expected)
                self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.launched.clear()
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.client.native_ids["w1:p2"] = self.entry["session_id"]
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(shutdown.main(["resume", "alpha", "--why", "maintenance"]), 0)
        self.assertEqual(len(output.getvalue().splitlines()), 1)


if __name__ == "__main__":
    _ = unittest.main()
