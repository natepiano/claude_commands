"""Pause and exit preserve private staff rows and use the shared stop path."""
from __future__ import annotations

import os
import io
import sys
import subprocess
import time
from dataclasses import replace
from functools import cached_property
from contextlib import redirect_stdout
from typing import cast, final, override
from unittest import mock

from scripts.accounts import accounts
from scripts.herdr import herdr, pane_launch
from scripts.message import sessions
from scripts.production import live_units, staff, unit_lookup, worker
from scripts.lib.resume_message import BusyPass, NoOwnPass
from scripts.production.cli_fixture import invoke_cli
from scripts.production.staff_test_support import CHECKOUT, StaffTestCase, fixture_member, fixture_session
from scripts.agents import restart_support, shutdown, stop_resume, remote
from scripts.agents.stop_resume import stop as stop_session
from scripts.agents.stop_results import ClaudeStartState, StopResult


@final
class PauseExitTests(StaffTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.child, self.stop_workers, self.stop_director, self.start, self.send
        fixture_session(self.unit.current_session, staff.ClaudeBackend())
        _ = self.enterContext(mock.patch.object(staff, "runtime_id", return_value=str(self.owner.current_session)))
        _ = self.enterContext(mock.patch.object(stop_resume, "_pane_environment", return_value=([], {})))
        def tab(session: str) -> herdr.TabSession | stop_resume.SessionNotFound:
            return (herdr.TabSession("w1:p1", self.unit.name, "w1", self.unit.current_session)
                    if session == self.unit.name else stop_resume.SessionNotFound(session))
        _ = self.enterContext(mock.patch.object(stop_resume, "_tab_of", side_effect=tab))
        _ = self.enterContext(mock.patch.object(accounts, "launch_shell_environment", return_value=dict(os.environ)))
        _ = self.enterContext(mock.patch.object(accounts, "prepare_claude_launch",
            return_value=accounts.PreparedAccount({}, frozenset())))
        _ = self.enterContext(mock.patch.object(herdr, "trust_claude_launch"))
        _ = self.enterContext(mock.patch.object(stop_resume, "running_on_account",
            return_value=stop_resume.ClaudeUp(self.unit.current_session, "w1:p1", "claude2",
                start_state=ClaudeStartState.ALREADY_RUNNING)))
        _ = self.enterContext(mock.patch("scripts.lib.agent_program.resolve_program", return_value=str(self.root / "TEST-agent")))

    @cached_property
    def owner(self) -> staff.StaffRow:
        return fixture_member("showrunner", name="TEST-production")

    @cached_property
    def unit(self) -> staff.StaffRow:
        return fixture_member("unit", name="TEST-unit", parent=self.owner.id,
                              worktree=staff.PresentWorktree(remote.local_machine(), self.root))

    @cached_property
    def child(self) -> staff.StaffRow:
        return fixture_member("worker", name="TEST-worker", parent=self.unit.id)

    @cached_property
    def stop_workers(self) -> mock.Mock:
        return cast(mock.Mock, self.enterContext(mock.patch.object(stop_resume, "stop_unit_workers", return_value=[])))

    @cached_property
    def stop_director(self) -> mock.Mock:
        return cast(mock.Mock, self.enterContext(mock.patch.object(stop_resume, "stop", return_value=stop_resume.Stopped(self.unit.current_session, 123))))

    @cached_property
    def start(self) -> mock.Mock:
        return cast(mock.Mock, self.enterContext(mock.patch.object(stop_resume, "resume", return_value=stop_resume.ClaudeUp(self.unit.current_session, "w1:p1", "claude2"))))

    @cached_property
    def send(self) -> mock.Mock:
        return cast(mock.Mock, self.enterContext(mock.patch.object(restart_support, "send_message", return_value={"kind": "sent", "how": "fixture"})))

    def agents(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return invoke_cli(shutdown, lambda: shutdown.main(list(arguments)), list(arguments), dict(os.environ), CHECKOUT)

    def current(self) -> staff.StaffRow:
        row = staff.by_id(self.unit.id)
        assert row is not None
        return row

    def save_exit(self) -> None:
        record = staff.snapshot()
        rows = [{**row.to_json(), **({"exited": {"since": "2026-11-07T12:00:00-05:00", "by": self.owner.name, "why": "maintenance"}} if row.id == self.unit.id else {})}
                for row in record.rows]
        _ = self.write_document({"schema": record.schema, "rows": rows})

    def test_pause_stops_workers_and_records_the_caller_without_stopping_the_director(self) -> None:
        result = self.agents("pause", self.unit.name, "--why", "maintenance")
        self.assertEqual(result.returncode, 0, result.stderr)
        paused = cast(dict[str, object], self.current().to_json()["paused"])
        self.assertEqual((paused["by"], paused["why"]), (self.owner.name, "maintenance"))
        self.assertTrue(paused["since"])
        self.stop_workers.assert_called_once()
        self.assertEqual(self.stop_workers.call_args.args[0], self.unit.id)
        self.stop_director.assert_not_called()
        self.assertEqual(staff.by_id(self.child.id), self.child)
        self.send.assert_called_once()
        self.assertIn("current turn", cast(str, self.send.call_args.args[2]))

    def test_stop_by_name_dry_run_stops_nothing(self) -> None:
        before = staff.snapshot()
        result = self.agents("stop", self.unit.name, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f"{self.child.name}: would stop", f"{self.unit.name}: would stop"])
        self.stop_workers.assert_not_called()
        self.stop_director.assert_not_called()
        self.start.assert_not_called()
        self.assertEqual(staff.snapshot(), before)
        missing = "session:TEST-missing-session"
        with (mock.patch.object(stop_resume, "_tab_of", return_value=stop_resume.SessionNotFound(missing)),
              mock.patch.object(stop_resume, "_record_launches"),
              mock.patch.object(stop_resume, "stop", wraps=stop_session)):
            preview = self.agents("stop", missing, "--dry-run")
            result = self.agents("stop", missing)
        self.assertEqual((preview.returncode, preview.stdout), (result.returncode, result.stdout))
        self.assertEqual((preview.returncode, preview.stdout), (2, f"{missing}: no session on this machine\n"))

    def test_stop_by_name_stops_the_workers_listed_by_its_dry_run(self) -> None:
        stopped: list[str] = []

        def stop_workers(unit: staff.StaffId, record: staff.StaffSnapshot) -> list[str]:
            self.assertEqual(unit, self.unit.id)
            stopped.extend(row.name for row in record.find(kind="worker", parent=unit))
            return []

        self.stop_workers.side_effect = stop_workers
        preview = self.agents("stop", self.unit.name, "--dry-run")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.stop_workers.assert_not_called()
        result = self.agents("stop", self.unit.name)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([*stopped, self.unit.name],
                         [line.removesuffix(": would stop") for line in preview.stdout.splitlines()])
        self.stop_workers.assert_called_once()
        self.stop_director.assert_called_once()

    def test_failed_continuation_delivery_preserves_the_pause(self) -> None:
        for outcome in ({"kind": "queued", "reason": "fixture queue"},
                        {"kind": "unconfirmed", "reason": "fixture failure"}, RuntimeError("fixture delivery failed")):
            with self.subTest(outcome=outcome):
                _ = staff.pause(str(self.unit.id), "maintenance")
                paused = self.current()
                self.send.reset_mock()
                self.send.side_effect = outcome if isinstance(outcome, RuntimeError) else None
                if not isinstance(outcome, RuntimeError):
                    self.send.return_value = outcome
                result = self.agents("resume", self.unit.name)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.current(), paused)
                self.assertIn("continuation delivery", result.stderr)
                self.assertNotIn(": active", result.stdout)
                self.send.assert_called_once()
                self.start.assert_not_called()

    def test_missing_agent_program_preserves_the_exit_before_exec(self) -> None:
        self.save_exit()
        exited = self.current()
        tab = herdr.TabSession("w1:p1", self.unit.name, "w1", str(self.unit.current_session))
        entry: pane_launch.CurrentLaunch = {
            "kind": "claude", "pane_id": "w1:p1", "session_id": str(self.unit.id),
            "config_dir": str(self.root / ".claude2"), "cwd": str(self.root),
            "name": self.unit.name, "model": "sonnet", "remote_control": False,
            "environment": {}, "flags_read": True, "written_at": "fixture"}
        with (mock.patch.object(stop_resume, "_pane_command", return_value=["env", "missing-TEST-agent"]),
              mock.patch("scripts.lib.agent_program.resolve_program", side_effect=FileNotFoundError("TEST agent program not found")),
              mock.patch.object(os, "chdir"),
              mock.patch.object(os, "execvp", side_effect=SystemExit),
              mock.patch.object(os, "execvpe", side_effect=SystemExit)):
            with self.assertRaisesRegex(FileNotFoundError, "TEST agent program not found"):
                stop_resume._exec_in_own_pane(tab, entry, "claude2", "")  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(self.current(), exited)

    def test_exit_stops_workers_and_director_and_the_board_shows_exited(self) -> None:
        for machine in (remote.local_machine(), "mac"):
            row = self.current()
            _ = staff.set_worktree(row.id, staff.PresentWorktree(machine, self.root), expected=row.worktree)
            for already_exited in (False, True):
                for failure in ("", "stop failed", "not found", "unreachable", "unreadable"):
                    with self.subTest(machine=machine, failure=failure, already_exited=already_exited):
                        _ = staff.resume(str(self.unit.id))
                        if already_exited:
                            self.save_exit()
                        original = self.current()
                        self.stop_workers.reset_mock()
                        self.stop_director.reset_mock()
                        alive = True
                        def stop(identity: str, *, deadline: float) -> StopResult:
                            nonlocal alive
                            del deadline
                            if already_exited:
                                self.assertEqual(self.current().exited, original.exited)
                            else:
                                self.assertNotIn("exited", self.current().to_json())
                            self.assertEqual(remote.local_machine(), machine)
                            if failure == "stop failed":
                                return stop_resume.StopFailed(identity, "fixture stop failed")
                            if failure == "not found":
                                return stop_resume.SessionNotFound(identity)
                            alive = False
                            return stop_resume.Stopped(identity, 123)
                        def ssh(args: list[str], stdin: str = "", limit: remote.RemoteCallLimit = remote.STANDARD_TIME_LIMIT) -> tuple[int, str]:
                            del limit
                            if failure == "unreachable":
                                return 255, ""
                            if failure == "unreadable":
                                return 0, "not a stop result"
                            output = io.StringIO()
                            with (mock.patch.object(sys, "stdin", io.StringIO(stdin)),
                                  mock.patch.object(shutdown, "local_machine", return_value="mac"),
                                  mock.patch.object(remote, "local_machine", return_value="mac"),
                                  mock.patch.object(shutdown, "other_machine", return_value="natedev"),
                                  redirect_stdout(output),
                                  mock.patch("sys.stderr", io.StringIO())):
                                code = shutdown.main(args)
                            return code, output.getvalue()
                        self.stop_director.side_effect = stop
                        with mock.patch.object(shutdown, "run_remote", side_effect=ssh):
                            result = self.agents("exit", self.unit.name, "--why", "maintenance")
                        if failure in ("stop failed", "not found") or (machine == "mac" and failure in ("unreachable", "unreadable")):
                            self.assertNotEqual(result.returncode, 0)
                            self.assertNotIn(": exited", result.stdout)
                            if already_exited:
                                self.assertEqual(self.current().exited, original.exited)
                            else:
                                self.assertNotIn("exited", self.current().to_json())
                            if failure == "stop failed":
                                self.assertIn("fixture stop failed", result.stdout + result.stderr)
                            self.assertTrue(alive)
                            self.stop_director.reset_mock()
                            self.stop_workers.reset_mock()
                            with mock.patch.object(shutdown, "run_remote", side_effect=ssh):
                                # The rerun uses the same retained staff identity.
                                failure = ""
                                result = self.agents("exit", self.unit.name, "--why", "maintenance")
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertFalse(alive)
                        if already_exited:
                            self.assertEqual(self.current().exited, original.exited)
                        exited = cast(dict[str, object], self.current().to_json()["exited"])
                        self.assertEqual((exited["by"], exited["why"]), (self.owner.name, "maintenance"))
                        self.assertTrue(exited["since"])
                        self.stop_workers.assert_called_once()
                        self.stop_director.assert_called_once()
                        self.assertEqual(staff.by_id(self.child.id), self.child)
                        every = unit_lookup.LabeledProductions({}, {self.owner.name: (self.unit.name,)})
                        self.assertEqual(live_units.units_in(self.owner.name, every)[0].status, "exited")

    def test_finish_step_waits_for_the_current_worker_task_before_stopping(self) -> None:
        for command in ("pause", "exit"):
            with self.subTest(command=command):
                events: list[str] = []
                def task(row: staff.StaffRow, record: staff.StaffSnapshot) -> BusyPass | NoOwnPass:
                    self.assertEqual(row.id, self.child.id)
                    self.assertIsNotNone(record.by_id(row.id))
                    events.append("task")
                    return BusyPass("TEST-task") if events == ["task"] else NoOwnPass()
                def stopped(unit: staff.StaffId, record: staff.StaffSnapshot) -> list[str]:
                    self.assertEqual(unit, self.unit.id)
                    self.assertIsNotNone(record.by_id(unit))
                    events.append("stop")
                    return []
                with (mock.patch.object(worker, "own_pass", side_effect=task),
                      mock.patch.object(shutdown, "wait_for_workers"),
                      mock.patch.object(time, "sleep"),
                      mock.patch.object(stop_resume, "stop_unit_workers", side_effect=stopped)):
                    result = self.agents(command, self.unit.name, "--finish-step", "--why", "maintenance")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(events, ["task", "task", "stop"])

    def test_resume_of_a_paused_unit_removes_the_pause_and_sends_one_continuation(self) -> None:
        for original, observed in (
                (self.unit, stop_resume.ClaudeUp(self.unit.current_session, "w1:p1", "claude2",
                    start_state=ClaudeStartState.ALREADY_RUNNING)),
                (self.unit, sessions.SessionNotLive()),
                (replace(self.unit, worktree=staff.PresentWorktree(remote.other_machine(), self.root)), sessions.SessionNotLive()),
                (replace(self.owner, doc=staff.LiveDoc(remote.other_machine(), self.root / "TEST-production.md")),
                    sessions.SessionNotLive()),
                (replace(self.owner, doc=staff.WrappedDoc(remote.other_machine(), self.root / "TEST-production.md")),
                    sessions.SessionNotLive())):
            for target in ((), ("--to", "claude4")):
                with self.subTest(row=original, observed=observed, target=target):
                    record = staff.snapshot()
                    _ = self.write_document({"schema": record.schema, "rows": [
                        original.to_json() if row.id == original.id else row.to_json() for row in record.rows]})
                    _ = staff.pause(str(original.id), "maintenance")
                    self.start.reset_mock()
                    self.send.reset_mock()
                    def start(session: str, *, why: str, to: str | None) -> stop_resume.ClaudeUp:
                        self.assertEqual((session, why), (str(original.id), "resumed by name"))
                        self.assertEqual(staff.by_id(original.id), original)
                        return stop_resume.ClaudeUp(original.current_session, "w1:p1", to or "claude2")
                    self.start.side_effect = start
                    with mock.patch.object(stop_resume, "running_on_account", return_value=observed) as running:
                        result = self.agents("resume", str(original.id), *target)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(staff.by_id(original.id), original)
                    if isinstance(observed, sessions.SessionNotLive) and original == self.unit:
                        self.start.assert_called_once_with(str(original.id), why="resumed by name",
                                                           to=target[1] if target else None)
                        self.send.assert_not_called()
                    else:
                        self.start.assert_not_called()
                        self.send.assert_called_once()
                        self.assertIn("Continue", cast(str, self.send.call_args.args[2]))
                    if original != self.unit:
                        running.assert_not_called()

    def test_resume_of_an_exited_unit_uses_the_shared_resume_and_removes_the_exit(self) -> None:
        self.save_exit()
        result = self.agents("resume", self.unit.name)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.start.assert_called_once_with(str(self.unit.id), why="resumed by name", to=None)
        self.assertNotIn("exited", self.current().to_json())
        self.send.assert_not_called()

    def test_a_failed_resume_keeps_the_exit_record(self) -> None:
        self.save_exit()
        exited = self.current().exited
        assert isinstance(exited, staff.Exited)
        paused = staff.Paused(exited.since, exited.by, exited.why)
        failure = stop_resume.ResumeFailed(self.unit.current_session, "fixture failure")
        for hold, outcome, code in ((exited, failure, 1), (paused, failure, 1),
                                    (paused, staff.StaffRefused("TEST row is locked"), 2)):
            with self.subTest(hold=hold, outcome=outcome):
                held = staff.set_hold(str(self.unit.id), hold)
                self.start.reset_mock()
                self.start.side_effect = outcome if isinstance(outcome, staff.StaffRefused) else None
                self.start.return_value = outcome
                with mock.patch.object(stop_resume, "running_on_account", return_value=sessions.SessionNotLive()):
                    result = self.agents("resume", self.unit.name)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(self.current(), held)
                if isinstance(hold, staff.Exited):
                    self.assertIn("exited", self.current().to_json())
                self.start.assert_called_once_with(str(self.unit.id), why="resumed by name", to=None)
                self.send.assert_not_called()

    def test_the_old_staff_pause_and_resume_commands_are_deleted(self) -> None:
        for command in ("pause", "resume"):
            arguments = [command, str(self.unit.id), *(["--why", "maintenance"] if command == "pause" else [])]
            with self.subTest(command=command):
                result = invoke_cli(staff, lambda: staff.main(arguments), arguments, dict(os.environ), CHECKOUT)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("invalid choice", result.stderr)

    def test_no_arguments_and_help_show_the_same_pause_and_exit_usage(self) -> None:
        empty, help_result = self.agents(), self.agents("help")
        self.assertEqual(empty.stdout, help_result.stdout)
        self.assertIn("agents pause NAME", empty.stdout)
        self.assertIn("agents exit NAME", empty.stdout)
        self.assertIn("--finish-step", empty.stdout)

    def test_resuming_in_its_own_pane_clears_the_exit_before_exec(self) -> None:
        self.save_exit()
        tab = herdr.TabSession("w1:p1", self.unit.name, "w1", str(self.unit.current_session))
        def execute(program: str, arguments: list[str], environment: dict[str, str]) -> None:
            self.assertEqual(program, str(self.root / "TEST-agent"))
            self.assertEqual(arguments[0], program)
            self.assertNotIn("STAFF_OPERATION_FD", environment)
            self.assertNotIn("exited", self.current().to_json())
            raise SystemExit
        entry: pane_launch.CurrentLaunch = {
            "kind": "claude", "pane_id": "w1:p1", "session_id": str(self.unit.id),
            "config_dir": str(self.root / ".claude2"), "cwd": str(self.root),
            "name": self.unit.name, "model": "sonnet", "remote_control": False,
            "environment": {}, "flags_read": True, "written_at": "2026-11-07T12:00:00-05:00"}
        with (mock.patch.object(os, "chdir"),
              mock.patch.object(os, "execvpe", side_effect=execute)):
            with self.assertRaises(SystemExit):
                stop_resume._exec_in_own_pane(tab, entry, "claude2", "")  # pyright: ignore[reportPrivateUsage]

    def test_a_failed_exec_preserves_the_exit_record(self) -> None:
        self.save_exit()
        tab = herdr.TabSession("w1:p1", self.unit.name, "w1", str(self.unit.current_session))
        entry: pane_launch.CurrentLaunch = {
            "kind": "claude", "pane_id": "w1:p1", "session_id": str(self.unit.id),
            "config_dir": str(self.root / ".claude2"), "cwd": str(self.root),
            "name": self.unit.name, "model": "sonnet", "remote_control": False,
            "environment": {}, "flags_read": True, "written_at": "2026-11-07T12:00:00-05:00"}
        with (mock.patch.object(os, "chdir"),
              mock.patch.object(os, "execvpe", side_effect=OSError("fixture failure"))):
            with self.assertRaises(OSError):
                stop_resume._exec_in_own_pane(tab, entry, "claude2", "")  # pyright: ignore[reportPrivateUsage]
        self.assertIn("exited", self.current().to_json())
