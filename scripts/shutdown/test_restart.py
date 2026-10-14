#!/usr/bin/env python3
"""Behavioral tests for restoring every session in a shutdown record."""

from __future__ import annotations

import copy
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable, Iterable, Sequence
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Literal, cast, final, override
from unittest.mock import patch

from test_support import registry_confirmation, ShutdownSendLeaf, ShutdownTestCase
import inventory
import record
import sessions
from user_action import ActionRequired
import restart
import herdr
from fake_herdr import CommandResponse
import conversation_pause
import settle
import showrunner_footer
# The module settle imports by its own path entry: a separate object from the package import below.
import showrunners as settle_showrunners
import shutdown
import account as account_reader
from scripts.accounts import accounts, moves
from scripts.production import showrunners
from account import Account, UnknownAccountName, UnreadableAccount
from record import SessionShutdown, SessionProgress, TimerRestore, Where

LOGIN = "owner@example.com"
LABEL = "claude 2"
STOPPED = "2026-10-09T21:49:10+00:00"
RESTARTED = "2026-10-10T01:02:03+00:00"
EMPTY_SESSION_IDS: frozenset[str] = frozenset()

def named_prefix(config_dir: str) -> str:
    """The assignments a session resumed on a named account's directory starts with."""
    return (
        f"CLAUDE_CONFIG_DIR={shlex.quote(config_dir)} "
        f"CLAUDE_CODE_REMOTE_MEMORY_DIR={shlex.quote(str(Path.home() / '.claude'))} "
    )


def checkout() -> inventory.GitCheckout:
    return inventory.GitCheckout(
        kind="git",
        path="/tmp/checkout",
        head=inventory.OnBranch(kind="branch", name="work"),
        upstream=inventory.Tracking(kind="tracking", ahead=0),
        dirty=[],
    )


def tmux_host(tmux_session: str) -> inventory.TmuxHost:
    return inventory.TmuxHost(
        kind="tmux",
        tmux_session=tmux_session,
        server=inventory.TmuxServerNotRecorded(kind="not recorded"),
        session_ref=inventory.TmuxSessionIdNotRecorded(kind="not recorded"),
        pane=inventory.PaneNotRecorded(kind="not recorded"),
    )


def recorded_tmux_host(
    tmux_session: str, pane_id: str, pane_pid: int
) -> inventory.TmuxHost:
    return inventory.TmuxHost(
        kind="tmux",
        tmux_session=tmux_session,
        server=inventory.TmuxServer(kind="server", pid=34500, started_at=1700000000),
        session_ref=inventory.TmuxSessionIdNotRecorded(kind="not recorded"),
        pane=inventory.TmuxPane(
            kind="pane", pane_id=pane_id, pane_pid=pane_pid
        ),
    )



def pane_display(format_text: str, name: str, session_id: str, pid: str, dead: str, started_at: str = "1700000000", server_pid: str = "34500") -> str:
    values = {"#{session_name}": name, "#{session_id}": session_id,
              "#{pane_pid}": pid, "#{pane_dead}": dead, "#{start_time}": started_at, "#{pid}": server_pid}
    for field, value in values.items():
        format_text = format_text.replace(field, value)
    return format_text + "\n"


def common_session(
    session_id: str,
    name: str | None = None,
    *,
    cwd: str | None = None,
    model: inventory.LastModel | None = None,
    run_dirs: list[str] | None = None,
) -> inventory.SessionFields:
    return {
        "session_id": session_id,
        "pid": 1000 + len(session_id),
        "proc_start": f"start-{session_id}",
        "name": name or session_id,
        "cwd": cwd or f"/tmp/{session_id}",
        "status": "idle",
        "model": model or inventory.NoReplyYet(kind="no reply yet"),
        "checkout": checkout(),
        "run_dirs": list(run_dirs or []),
        "codex_servers": [],
        "timers": [],
    }


def top_level(
    session_id: str,
    *,
    name: str | None = None,
    cwd: str | None = None,
    model: inventory.LastModel | None = None,
    host: inventory.SessionHost | None = None,
    run_dirs: list[str] | None = None,
) -> inventory.TopLevelSession:
    return inventory.TopLevelSession(
        **common_session(
            session_id,
            name,
            cwd=cwd,
            model=model,
            run_dirs=run_dirs,
        ),
        kind="top-level",
        host=host or inventory.UnknownHost(kind="unknown"),
    )


def showrunner(
    session_id: str,
    *,
    host: inventory.SessionHost | None = None,
) -> inventory.ShowrunnerSession:
    return inventory.ShowrunnerSession(
        **common_session(session_id),
        kind="showrunner",
        host=host or tmux_host(f"tmux-{session_id}"),
        production="demo",
        doc="/tmp/demo production.md",
    )


def unit(
    session_id: str,
    *,
    plan: inventory.UnitPlan | None = None,
    session_name: str | None = None,
    session_host: Literal["tmux", "herdr"] = "tmux",
    run_dirs: list[str] | None = None,
    doc: str = "/tmp/demo production.md",
) -> inventory.UnitSession:
    return inventory.UnitSession(
        **common_session(session_id, run_dirs=run_dirs),
        kind="unit",
        host=inventory.UnitHost(
            kind="unit",
            production="demo",
            unit=f"{session_id}-unit",
            doc=doc,
            session_name=session_name or f"tmux-{session_id}",
            session_host=session_host,
            plan=plan or inventory.PlanFromRunRecord(kind="plan", path=f"/tmp/{session_id} plan.md"),
        ),
    )


def seat(
    session_id: str,
    *,
    owner: str | None,
    run_dirs: list[str] | None = None,
) -> inventory.SeatSession:
    seat_owner: inventory.SeatOwner = (
        inventory.DirectorOwner(kind="director", session_id=owner)
        if owner is not None
        else inventory.NoDirector(kind="no director")
    )
    return inventory.SeatSession(
        **common_session(session_id, run_dirs=run_dirs),
        kind="seat",
        host=inventory.UnknownHost(kind="unknown"),
        owner=seat_owner,
    )


def entry(
    session: inventory.Session, *, state: SessionProgress | None = None,
    where: Where | None = None, timers: list[TimerRestore] | None = None,
) -> SessionShutdown:
    return SessionShutdown(
        login=LOGIN, label=LABEL, machine="natedev", requested_at=STOPPED,
        request_id="request", requested_by=record.FromTerminal(kind="terminal"),
        hold="session", then="restart", force="wait for ready", alerts="user",
        session=session, seats=[], timers=list(timers or []),
        settle_message=record.SettleMessageNotSent(kind="not sent"),
        where=where or record.WhereSaid(kind="said", text=f"working on {session['name']}", at=STOPPED),
        progress=state or record.SessionStopped(kind="stopped", at=STOPPED), stop_issues=[],
    )


def records_for_test(
    records: list[SessionShutdown], *, machine: str = "natedev",
    requester: str | None = None, stopped_at: str = STOPPED,
) -> list[SessionShutdown]:
    for item in records:
        item["machine"] = machine
        item["requested_at"] = stopped_at
        if requester is not None:
            item["requested_by"] = record.FromSession(kind="session", session_id=requester)
    return records


def session_shutdown(
    session_id: str, *, machine: str = "natedev", hold: Literal["account", "session"] = "account",
) -> SessionShutdown:
    item = entry(top_level(session_id))
    item["machine"] = machine
    item["hold"] = hold
    return item


def peer_json(*, outcome: str = "done", lines: Sequence[str] = (), missing: Sequence[record.NoRemoteControl] = (), refused: Sequence[restart.RestartRefusal] = ()) -> str:
    return json.dumps({"machine": "Mac", "label": LABEL, "outcome": outcome, "lines": lines,
        "no_remote_control": missing, "refused": refused})


def setUpModule() -> None:
    """The times these tests expect are Pacific; the user's zone setting must not change them."""
    unittest.enterModuleContext(patch.dict(os.environ, {"USER_ZONE": "America/Los_Angeles"}))
    root = Path(unittest.enterModuleContext(tempfile.TemporaryDirectory()))
    _ = unittest.enterModuleContext(patch.object(showrunners, "NOTIFIER_STATE_DIR", root))
    _ = unittest.enterModuleContext(patch.object(settle_showrunners, "NOTIFIER_STATE_DIR", root))


@final
class CommandRecorder:
    calls: list[list[str]]

    def __init__(self) -> None:
        self.calls = []

    def __call__(
        self, argv: list[str], *, dry_run: bool, check: bool = True
    ) -> subprocess.CompletedProcess[str] | None:
        del check
        self.calls.append(argv)
        if dry_run:
            print(shlex.join(argv))
            return None
        return subprocess.CompletedProcess(argv, 0, "", "")


@final
class RestartTests(ShutdownTestCase):
    root: Path = Path()
    state_root: Path = Path()
    desktop_state: restart.DesktopLaunchState = restart.DesktopLaunchState()
    account = Account("claude", LOGIN, LABEL)

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(patch.object(sessions, "remote_control", return_value="active"))
        self.desktop_state = restart.DesktopLaunchState()
        self.addCleanup(self.desktop_state.restore, dry_run=True)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state_root = self.root / "shutdown"
        environment = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.root), "SHUTDOWN_STATE_DIR": str(self.state_root),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "CONVERSATION_PAUSE_STATE_DIR": str(self.root / "pause"),
            "AGENT_NOTES_DIR": str(self.root / "notes"),
        })))
        del environment
        (self.root / ".claude").mkdir()
        (self.root / "notes").mkdir()
        _ = (self.root / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": LOGIN}}))
        _ = (self.root / "notes/claude 2.md").write_text(f"---\nlogin: {LOGIN}\n---\n")
        _ = (self.root / "notes/claude 3.md").write_text("---\nlogin: third@example.invalid\n---\n")
        self.assertEqual(accounts.setup("claude 3"), [])
        _ = (self.root / ".claude3/.claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "third@example.invalid"}}))
        self.configure_groups()
        _ = self.enterContext(patch.object(settle, "record_time", return_value=RESTARTED))
        _ = self.enterContext(patch.object(settle, "inventory", return_value=inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL, sessions=[], unattributed=[])))

    def configure_groups(self, *, units: str = "default", showrunners: str = "default") -> None:
        path = accounts.groups_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({"units": units, "showrunners": showrunners}))

    def create(self, records: list[SessionShutdown]) -> None:
        for item in records:
            self.assertTrue(record.create_session(item))

    def stored_record(self) -> SessionShutdown:
        return record.session_records(LOGIN)[0]

    def moved_record(self, identity: str = "moved-owner") -> SessionShutdown:
        saved = entry(top_level(identity, host=tmux_host(identity)))
        saved["target"] = "claude 3"
        saved["source_label"] = LABEL
        saved["why"] = "weekly refusal"
        self.create([saved])
        return saved

    def test_live_source_account_cannot_confirm_a_move(self) -> None:
        saved = self.moved_record()
        owner = saved["session"]
        live = sessions.SessionRecord(pid=owner["pid"], sessionId=owner["session_id"],
            name=owner["name"], messagingSocketPath="", updatedAt=1, tmux="", formerNames=[])
        with (patch.object(sessions, "live_sessions", return_value=[live]),
              patch.object(account_reader, "process_config_dir", return_value=self.root / ".claude2"),
              patch.object(account_reader, "account_of", return_value=Account("claude", LOGIN, LABEL)),
              patch.object(settle, "run_notifier") as timers):
            restart.confirm_entry(saved, RESTARTED, True)
        timers.assert_not_called()
        entries = moves.read_moves()
        self.assertEqual([item["kind"] for item in entries], ["failed"])
        failed = entries[0]
        assert failed["kind"] == "failed"
        self.assertEqual(failed["move_id"], saved["request_id"])
        self.assertEqual(failed["reason"], "still on claude 2")
        self.assertNotEqual(self.stored_record()["progress"]["kind"], "restarted")

    def test_target_process_confirms_move_even_when_timer_restoration_is_pending(self) -> None:
        saved = self.moved_record()
        saved["timers"] = [TimerRestore(instance="timer", was_enabled=True,
            footer=record.NoFooter(kind="no footer"))]
        _ = record.update_session(LOGIN, saved["session"]["session_id"],
            lambda current: current.update(timers=saved["timers"]))
        owner = saved["session"]
        live = sessions.SessionRecord(pid=owner["pid"], sessionId=owner["session_id"],
            name=owner["name"], messagingSocketPath="", updatedAt=1, tmux="", formerNames=[])
        with (patch.object(sessions, "live_sessions", return_value=[live]),
              patch.object(account_reader, "process_config_dir", return_value=self.root / ".claude3"),
              patch.object(account_reader, "account_of", return_value=Account("claude", "target@example.com", "claude 3")),
              patch.object(settle, "run_notifier", side_effect=RuntimeError("timer unavailable"))):
            restart.confirm_entry(saved, RESTARTED, True)
        self.assertEqual(self.stored_record()["progress"]["kind"], "timers pending")
        confirmed = moves.read_moves()
        self.assertEqual([item["kind"] for item in confirmed], ["confirmed"])
        item = confirmed[0]
        assert item["kind"] == "confirmed" and "config_dir" in item
        self.assertEqual(item["move_id"], saved["request_id"])
        self.assertEqual(item["config_dir"], str(self.root / ".claude3"))

    def test_unconfirmed_target_restart_records_failure_with_request_id(self) -> None:
        saved = self.moved_record()
        restart.confirm_entry(saved, RESTARTED, False)
        entries = moves.read_moves()
        self.assertEqual([item["kind"] for item in entries], ["failed"])
        item = entries[0]
        assert item["kind"] == "failed"
        self.assertEqual(item["move_id"], saved["request_id"])
        self.assertIn("not live", item["reason"])

    def hand_restart_record(self, identity: str = "moved-owner") -> SessionShutdown:
        saved = self.moved_record(identity)
        progress: SessionProgress = record.SessionNeedsManualRestart(kind="manual restart", command="add_unit.py --resume moved-owner")
        _ = record.update_session(LOGIN, saved["session"]["session_id"], lambda current: current.update(progress=progress))
        return self.stored_record()

    def running_on(self, saved: SessionShutdown, directory: Path) -> list[sessions.SessionRecord]:
        owner = saved["session"]
        _ = self.enterContext(patch.object(account_reader, "process_config_dir", return_value=directory))
        return [sessions.SessionRecord(pid=owner["pid"], sessionId=owner["session_id"],
            name=owner["name"], messagingSocketPath="", updatedAt=1, tmux="", formerNames=[])]

    def test_session_relaunched_by_hand_on_its_target_is_never_reported_not_back(self) -> None:
        saved = self.hand_restart_record()
        live = self.running_on(saved, self.root / ".claude3")
        with (patch.object(sessions, "live_sessions", return_value=live),
              patch.object(settle, "run_remote", return_value=(0, "[]")),
              patch.object(settle, "send_message") as sender, patch.object(settle, "alert_user") as alert):
            settle.report_stop([saved])
        alert.assert_not_called()
        sender.assert_not_called()
        self.assertEqual(record.session_records(LOGIN), [])
        self.assertEqual([item["kind"] for item in moves.read_moves()], ["confirmed"])

    def test_status_and_restart_finish_a_session_relaunched_by_hand_on_its_target(self) -> None:
        for command in ("status", "restart"):
            with self.subTest(command=command):
                saved = self.hand_restart_record(command + "-owner")
                live = self.running_on(saved, self.root / ".claude3")
                output = io.StringIO()
                with (patch.object(sessions, "live_sessions", return_value=live),
                      patch.object(restart, "relaunch_session") as relaunch, redirect_stdout(output)):
                    if command == "status":
                        outcome = shutdown._local_shutdown_records(LOGIN, "natedev")  # pyright: ignore[reportPrivateUsage]
                        self.assertEqual(outcome, {"kind": "records", "records": []})
                    else:
                        _ = restart._up(LOGIN, False)  # pyright: ignore[reportPrivateUsage]
                        self.assertIn("restart-owner is already running on claude 3", output.getvalue())
                relaunch.assert_not_called()
                self.assertEqual(record.session_records(LOGIN), [])

    def test_cancel_finishes_a_session_relaunched_by_hand_on_its_target(self) -> None:
        saved = self.hand_restart_record("cancel-owner")
        live = self.running_on(saved, self.root / ".claude3")
        output = io.StringIO()
        with (patch.object(sessions, "live_sessions", return_value=live),
              patch.object(settle, "run_remote", return_value=(0, "")), redirect_stdout(output)):
            _ = settle.cancel(self.account, True, only=[saved["session"]["session_id"]])
        self.assertIn("cancel-owner is already running on claude 3; its shutdown is finished", output.getvalue())
        self.assertNotIn("was not cancelled", output.getvalue())
        self.assertEqual(record.session_records(LOGIN), [])

    def test_session_running_on_its_source_account_stays_not_back(self) -> None:
        saved = self.hand_restart_record()
        live = self.running_on(saved, self.root / ".claude2")
        with patch.object(sessions, "live_sessions", return_value=live):
            checked = restart.clear_running_on_target([saved])
        self.assertEqual((checked.remaining, checked.finished), ([saved], []))
        self.assertEqual(self.stored_record(), saved)

    def test_unchanged_not_back_failure_is_pushed_once(self) -> None:
        saved = self.hand_restart_record()
        with (patch.object(sessions, "live_sessions", return_value=[]),
              patch.object(settle, "run_remote", return_value=(0, "[]")),
              patch.object(settle, "send_message"), patch.object(settle, "alert_user") as alert):
            settle.report_stop([saved])
            settle.report_stop([self.stored_record()])
            alert.assert_called_once()
            progress: SessionProgress = record.SessionRestartFailed(kind="restart failed", at=RESTARTED, reason="window failed to open")
            _ = record.update_session(LOGIN, saved["session"]["session_id"], lambda current: current.update(progress=progress))
            settle.report_stop([self.stored_record()])
        self.assertEqual(alert.call_count, 2)
        self.assertIn("window failed to open", cast(str, alert.call_args.args[1]))

    def test_default_target_process_confirms_main_directory_and_removes_assumption(self) -> None:
        saved = entry(top_level("default-target", host=tmux_host("default-target")))
        saved["target"] = "default"
        self.create([saved])
        assumed = self.root / ".local/state/accounts/assumed_refusals/default-target.json"
        assumed.parent.mkdir(parents=True)
        _ = assumed.write_text("{}")
        live = sessions.SessionRecord(pid=23456, sessionId="default-target", name="default-target",
            messagingSocketPath="", updatedAt=1, tmux="", formerNames=[])
        with (patch.object(sessions, "live_sessions", return_value=[live]),
              patch.object(account_reader, "process_config_dir", return_value=self.root / ".claude")):
            restart.confirm_entry(saved, RESTARTED, True)
        self.assertFalse(assumed.exists())
        entries = moves.read_moves()
        self.assertEqual([item["kind"] for item in entries], ["confirmed"])
        confirmed = entries[0]
        assert confirmed["kind"] == "confirmed" and "config_dir" in confirmed
        self.assertEqual(confirmed["config_dir"], str(self.root / ".claude"))

    def test_manual_move_restart_records_failure_instead_of_confirmation(self) -> None:
        saved = entry(top_level("manual-owner"))
        saved["target"] = "claude 3"
        self.create([saved])
        with (patch.object(restart, "_retire"),
              patch.object(restart, "_live_session_ids", return_value=frozenset()),
              redirect_stdout(io.StringIO())):
            _ = restart.launch_entry(saved, RESTARTED, self.desktop_state, dry_run=False)
        entries = moves.read_moves()
        self.assertEqual([item["kind"] for item in entries], ["failed"])
        item = entries[0]
        assert item["kind"] == "failed"
        self.assertEqual(item["move_id"], saved["request_id"])
        self.assertIn("manual restart", item["reason"])

    @staticmethod
    def all_sessions_live(ids: frozenset[str], _timeout: float) -> restart.RegistryConfirmation:
        return registry_confirmation(ids)

    def moved_restart(self, session: inventory.Session) -> tuple[list[list[str]], str]:
        if session["kind"] == "showrunner":
            self.configure_groups(showrunners="claude 3")
        saved = entry(session)
        saved["target"] = "claude 3"
        commands = CommandRecorder()
        output = io.StringIO()

        def probe(arguments: list[str], *, dry_run: bool) -> subprocess.CompletedProcess[str]:
            del dry_run
            if arguments[:2] == ["tmux", "display"]:
                return subprocess.CompletedProcess(arguments, 0, "$8\t420\t0\t1700000000\t34500\n", "")
            return subprocess.CompletedProcess(arguments, 1, "", "")

        with (
            patch.object(restart, "_run", commands),
            patch.object(restart, "_retire"),
            patch.object(restart, "_probe", side_effect=probe),
            patch.object(restart, "_live_session_ids", return_value=frozenset()),
            patch.object(restart, "_desktop_numbers", return_value={}),
            redirect_stdout(output),
        ):
            self.assertEqual(restart.relaunch_session(saved, dry_run=True,
                desktop_state=self.desktop_state), saved)
        return commands.calls, output.getvalue()

    def assert_move_resume(self, command: str, session: inventory.Session) -> None:
        arguments = shlex.split(command.removesuffix("; exec zsh"))
        script = next(argument for argument in arguments if argument.endswith("/accounts.py"))
        start = arguments.index(script)
        self.assertEqual(arguments[start + 1:start + 3], ["exec", "claude 3"])
        self.assertEqual(arguments[arguments.index("--resume") + 1], session["session_id"])
        self.assertEqual(arguments[arguments.index("-n") + 1], session["name"])
        self.assertEqual(arguments[arguments.index("--remote-control") + 1], session["name"])
        model = session["model"]
        if model["kind"] == "model":
            self.assertEqual(arguments[arguments.index("--model") + 1], model["name"])
        self.assertIn("pending` for messages kept while you", arguments[-1])
        self.assertNotIn("CLAUDE_CONFIG_DIR=", command)

    def test_move_target_respawns_its_tmux_pane_through_accounts_exec(self) -> None:
        owner = top_level("move-id", name="kept name", model=inventory.ModelName(kind="model", name="opus"),
            host=recorded_tmux_host("kept tmux", "%move", 420))
        commands, _ = self.moved_restart(owner)
        launch = next(argv for argv in commands if argv[:2] == ["tmux", "respawn-pane"])
        self.assertEqual(launch[launch.index("-t") + 1], "%move")
        self.assert_move_resume(launch[-1], owner)

    def test_tmux_account_move_updates_retained_directory_for_named_and_default_targets(self) -> None:
        for target, directory in (("claude 3", self.root / ".claude3"), ("default", self.root / ".claude")):
            with self.subTest(target=target):
                owner = showrunner("moved-showrunner", host=recorded_tmux_host("kept", "%move", 420))
                owner["config_dir"] = str(self.root / ".claude1")
                saved = entry(owner)
                saved["target"] = target
                self.configure_groups(showrunners=target)
                commands = CommandRecorder()
                with (patch.object(restart, "_run", commands),
                      patch.object(restart, "_tmux_pane_state", return_value=restart.ReusableTmuxPane()),
                      patch.object(restart, "_record_scheduled_restart_prompt"),
                      patch.object(restart, "_retire"),
                      patch.object(restart, "_live_session_ids", return_value=frozenset())):
                    result = restart.launch_entry(saved, RESTARTED, self.desktop_state, dry_run=False)
                self.assertIsInstance(result, restart.EntryLaunched)
                options = [argv for argv in commands.calls if "@claude_config_dir" in argv]
                self.assertEqual(len(options), 1, commands.calls)
                self.assertEqual(options[0][-1], str(directory))
                self.assertIn(options[0][options[0].index("-t") + 1], ("=kept", "%move", "$8"))
                self.assertEqual(len([argv for argv in commands.calls if argv[:2] == ["tmux", "respawn-pane"]]), 1)

    def test_move_target_relaunches_unit_with_explicit_account(self) -> None:
        """F005: an account move preserves its explicit target override."""
        owner = unit("move-unit")
        commands, _ = self.moved_restart(owner)
        launch = next(argv for argv in commands if any(part.endswith("/add_unit.py") for part in argv))
        self.assertEqual(launch[launch.index("--account") + 1], "claude 3")
        self.assertEqual(launch[launch.index("--resume") + 1], "move-unit")
        self.assertEqual(launch[launch.index("--session-name") + 1], "move-unit")

    def test_ordinary_unit_restart_defers_to_configured_units_instead_of_recorded_account(self) -> None:
        """F005: an untargeted stopped unit carries no obsolete account override."""
        self.configure_groups(units="claude 1")
        owner = unit("stopped-unit")
        owner["config_dir"] = str(self.root / ".claude2")
        saved = entry(owner)
        commands = CommandRecorder()
        with (patch.object(restart, "_run", commands), patch.object(restart, "_retire"),
              patch.object(restart, "_live_session_ids", return_value=frozenset()),
              redirect_stdout(io.StringIO())):
            _ = restart.relaunch_session(saved, dry_run=True, desktop_state=self.desktop_state)
        argv = next(command for command in commands.calls if any(part.endswith("/add_unit.py") for part in command))
        self.assertEqual(argv[argv.index("--resume") + 1], "stopped-unit")
        self.assertNotIn("--config-dir", argv)
        self.assertNotIn("--account", argv)

    def test_stopped_showrunner_restart_uses_prepared_configured_showrunners_account(self) -> None:
        """F005: showrunner restarts use their group's selection instead of the stop directory."""
        self.configure_groups(showrunners="claude 3")
        owner = showrunner("stopped-runner")
        owner["config_dir"] = str(self.root / ".claude2")
        saved = entry(owner)
        commands = CommandRecorder()
        with (patch.object(accounts, "prepared_account", wraps=accounts.prepared_account) as prepare,
              patch.object(restart, "_run", commands), patch.object(restart, "_retire"),
              patch.object(restart, "_live_session_ids", return_value=frozenset()),
              redirect_stdout(io.StringIO())):
            _ = restart.relaunch_session(saved, dry_run=True, desktop_state=self.desktop_state)
        prepare.assert_any_call("claude 3", resume=True)
        command = next(argv[-1] for argv in commands.calls if "-ic" in argv)
        arguments = shlex.split(command.removesuffix("; exec zsh"))
        self.assertEqual(arguments[arguments.index("exec") + 1], "claude 3")
        self.assertEqual(arguments[arguments.index("--resume") + 1], owner["session_id"])
        self.assertNotIn(str(self.root / ".claude2"), command)

    def test_stopped_showrunner_without_configured_group_is_not_resumed_and_names_setup(self) -> None:
        """F005: missing showrunners configuration refuses before any resume."""
        _ = accounts.groups_file().write_text(json.dumps({"units": "default"}))
        saved = entry(showrunner("unconfigured-runner"))
        self.create([saved])
        commands = CommandRecorder()
        output = io.StringIO()
        with (patch.object(restart, "_run", commands), patch.object(restart, "_retire"),
              patch.object(restart, "_live_session_ids", return_value=frozenset()), redirect_stdout(output)):
            _ = restart.relaunch_session(saved, dry_run=False, desktop_state=self.desktop_state)
        current = record.find_session(LOGIN, "unconfigured-runner")
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current["progress"]["kind"], "restart failed")
        progress = current["progress"]
        assert progress["kind"] == "restart failed"
        self.assertIn("accounts.py move showrunners", progress["reason"] + output.getvalue())
        self.assertFalse(any("-ic" in command or "respawn-pane" in command for command in commands.calls))

    def test_unconfigured_showrunner_refusal_does_not_abort_later_unit_restart(self) -> None:
        """F005: a launch-account refusal settles one entry and the restart batch continues."""
        _ = accounts.groups_file().write_text(json.dumps({"units": "default"}))
        refused_id, unit_id = "unconfigured-runner", "later-unit"
        self.create(records_for_test([
            entry(showrunner(refused_id)), entry(unit(unit_id)),
        ], requester=unit_id))
        commands = CommandRecorder()
        output = io.StringIO()
        with (patch.object(restart, "_run", commands), patch.object(restart, "_retire"),
              patch.object(restart, "_live_session_ids", return_value=frozenset()),
              patch.object(restart, "wait_for_confirmation", side_effect=self.all_sessions_live) as confirm,
              redirect_stdout(output)):
            self.assertEqual(restart.up(LOGIN), 1)
        refused = record.find_session(LOGIN, refused_id)
        self.assertIsNotNone(refused)
        assert refused is not None
        progress = refused["progress"]
        self.assertEqual(progress["kind"], "restart failed")
        assert progress["kind"] == "restart failed"
        self.assertIn("accounts.py move showrunners", progress["reason"])
        self.assertIn("accounts.py move showrunners", output.getvalue())
        self.assertIsNone(record.find_session(LOGIN, unit_id))
        launch = next(command for command in commands.calls if any(part.endswith("/add_unit.py") for part in command))
        self.assertEqual(launch[launch.index("--resume") + 1], unit_id)
        self.assertFalse(any("-ic" in command or "respawn-pane" in command for command in commands.calls))
        confirm.assert_called_once_with(frozenset({unit_id}), restart.SESSION_WAIT_SECONDS)

    def test_unconfigured_showrunner_refusal_does_not_abort_later_unit_restart_preview(self) -> None:
        """F005: a refused showrunner cannot abort subsequent restart previews."""
        _ = accounts.groups_file().write_text(json.dumps({"units": "default"}))
        self.create(records_for_test([
            entry(showrunner("unconfigured-runner")), entry(unit("later-unit")),
        ], requester="later-unit"))
        before = {path: path.read_bytes() for path in self.state_root.rglob("*") if path.is_file()}
        commands = CommandRecorder()
        output = io.StringIO()
        with (patch.object(restart, "_run", commands), patch.object(restart, "_retire"),
              patch.object(restart, "_live_session_ids", return_value=frozenset()), redirect_stdout(output)):
            _ = restart.up(LOGIN, dry_run=True)
        self.assertIn("accounts.py move showrunners", output.getvalue())
        launch = next(command for command in commands.calls if any(part.endswith("/add_unit.py") for part in command))
        self.assertEqual(launch[launch.index("--resume") + 1], "later-unit")
        self.assertFalse(any("-ic" in command or "respawn-pane" in command for command in commands.calls))
        self.assertEqual({path: path.read_bytes() for path in self.state_root.rglob("*") if path.is_file()}, before)

    def test_moved_unit_passes_its_recorded_model_to_add_unit_resume(self) -> None:
        owner = unit("model-unit")
        owner["model"] = inventory.ModelName(kind="model", name="opus")
        commands, _ = self.moved_restart(owner)
        launch = next(argv for argv in commands if any(part.endswith("/add_unit.py") for part in argv))
        self.assertEqual(launch[launch.index("--model") + 1], "opus")

    def test_move_target_reopens_ghostty_through_accounts_exec(self) -> None:
        owner = top_level("move-ghostty", host=inventory.WindowHost(kind="ghostty", window_shell=10,
            desktop=inventory.DesktopNotInSnapshot(kind="not in snapshot")))
        commands, _ = self.moved_restart(owner)
        launch = next(argv for argv in commands if "ghostty" in argv)
        self.assert_move_resume(launch[-1], owner)

    def test_move_target_reopens_zed_session_through_accounts_exec(self) -> None:
        owner = top_level("move-zed", host=inventory.WindowHost(kind="zed", window_shell=10,
            desktop=inventory.DesktopNotInSnapshot(kind="not in snapshot")))
        commands, _ = self.moved_restart(owner)
        launch = next(argv for argv in commands if "ghostty" in argv)
        self.assert_move_resume(launch[-1], owner)

    def test_linux_terminal_move_requires_manual_resume_and_journals_failure(self) -> None:
        owner = top_level("linux-terminal", host=inventory.TerminalHost(kind="terminal"))
        saved = entry(owner)
        saved["target"] = "claude 3"
        saved["source_label"] = LABEL
        saved["why"] = "weekly refusal"
        self.create([saved])
        with (patch.object(sys, "platform", "linux"),
              patch.object(restart, "_retire"),
              patch.object(restart, "_live_session_ids", return_value=frozenset()),
              patch.object(restart, "_run") as launch, redirect_stdout(io.StringIO()) as output):
            result = restart.launch_entry(saved, RESTARTED, self.desktop_state, dry_run=False)
        launch.assert_not_called()
        self.assertIsInstance(result, restart.EntrySettled)
        progress = self.stored_record()["progress"]
        self.assertEqual(progress["kind"], "manual restart")
        assert progress["kind"] == "manual restart"
        self.assert_move_resume(progress["command"], owner)
        self.assertIn("manual restart linux-terminal:", output.getvalue())
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["failed"])
        failed = entries[0]
        assert failed["kind"] == "failed"
        self.assertEqual(failed["move_id"], saved["request_id"])
        self.assertEqual(failed["reason"], "manual restart")

    def test_move_target_reopens_terminal_through_accounts_exec(self) -> None:
        owner = top_level("move-terminal", host=inventory.TerminalHost(kind="terminal"))
        with patch.object(sys, "platform", "darwin"):
            commands, _ = self.moved_restart(owner)
        launch = next(argv for argv in commands if argv[:1] == ["open"])
        self.assert_move_resume(launch[-1], owner)

    def test_move_target_unknown_host_prints_one_manual_restart_line(self) -> None:
        owner = top_level("move-manual", name="kept name")
        _, output = self.moved_restart(owner)
        lines = output.splitlines()
        self.assertEqual(len(lines), 1)
        prefix = "manual restart kept name: "
        self.assertTrue(lines[0].startswith(prefix), output)
        self.assert_move_resume(lines[0].removeprefix(prefix), owner)

    def test_tmux_unit_restart_keeps_its_recorded_session_name(self) -> None:
        owner = unit("kept-unit", session_name="kept tmux session")
        commands, _ = self.moved_restart(owner)
        launch = next(argv for argv in commands if any(part.endswith("/add_unit.py") for part in argv))
        self.assertEqual(launch[launch.index("--host-session") + 1], "kept tmux session")

    def herdr_restart_fixture(self) -> tuple[inventory.TopLevelSession, Path, Path]:
        cwd = self.root / "trusted folder"
        cwd.mkdir()
        config = self.root / "account dir"
        config.mkdir()
        _ = (config / ".claude.json").write_text(json.dumps({
            "projects": {str(cwd): {"hasTrustDialogAccepted": True}},
        }), encoding="utf-8")
        self.assertIsInstance(herdr.claude_trust(cwd, config / ".claude.json"), herdr.Trusted)
        owner = top_level("resume-id", name="kept name", cwd=str(cwd),
            model=inventory.ModelName(kind="model", name="opus"),
            host=inventory.HerdrHost(kind="herdr", herdr_session="writing", pane_id="w1:p1"))
        owner["config_dir"] = str(config)
        fake = self.root / "fake-herdr"
        _ = fake.write_bytes((restart.SCRIPTS / "herdr" / "fake_herdr.py").read_bytes())
        fake.chmod(0o755)
        responses = self.root / "herdr-responses.json"
        log = self.root / "herdr-log.jsonl"
        self.write_herdr_responses(responses, [])
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HERDR_BIN": str(fake), "SYSTEMD_RUN": str(fake), "SYSTEMCTL": str(fake),
            "FAKE_HERDR_RESPONSES": str(responses), "FAKE_HERDR_LOG": str(log),
        })))
        return owner, responses, log

    def write_herdr_responses(self, path: Path, agents: list[herdr.AgentOccupant],
                              *, not_ready: bool = False) -> None:
        responses: dict[str, CommandResponse | list[CommandResponse]] = {
            "--session writing status server": {"stdout": "status: running\n", "code": 0},
            "--session writing agent list": {"stdout": json.dumps({"result": {"agents": agents}}), "code": 0},
            "default": {"stdout": json.dumps({"result": {"root_pane": {"pane_id": "w2:p1"}}}), "code": 0},
        }
        if not_ready:
            start_argv = ["--session", "writing", "agent", "start", "kept name", "--kind", "claude",
                          "--pane", "w2:p1", "--timeout", "90000", "--", "--resume", "resume-id",
                          "-n", "kept name", "--remote-control", "kept name", "--model", "opus",
                          "--settings", '{"disableAgentView": true}']
            responses[" ".join(start_argv)] = {"stdout": json.dumps({
                "error": {"code": "agent_not_ready", "message": "trust dialog"},
            }), "code": 1}
            responses["--session writing pane read w2:p1 --source visible"] = {
                "stdout": "Do you trust this folder?", "code": 0,
            }
        _ = path.write_text(json.dumps(responses), encoding="utf-8")

    @staticmethod
    def herdr_calls(log: Path) -> list[list[str]]:
        return [cast(list[str], json.loads(line)) for line in log.read_text().splitlines()]

    def test_herdr_restart_passes_only_claude_arguments_and_prompts_once(self) -> None:
        owner, _, log = self.herdr_restart_fixture()
        prompt = "Continue here.\nKeep 'quotes', ; and $(literal) intact."
        with patch.object(restart, "_run") as other_host:
            launch = restart._launch_session(entry(owner), prompt, self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(launch, restart.SessionLaunched)
        other_host.assert_not_called()
        calls = self.herdr_calls(log)
        opened = next(row for row in calls if row[2:4] == ["workspace", "create"])
        self.assertEqual(opened[opened.index("--cwd") + 1], owner["cwd"])
        self.assertIn("CLAUDE_CONFIG_DIR=" + owner.get("config_dir", ""), opened)
        self.assertIn("CLAUDE_CODE_REMOTE_MEMORY_DIR=" + str(self.root / ".claude"), opened)
        self.assertIn("ENABLE_TOOL_SEARCH=true", opened)
        started = next(row for row in calls if row[2:4] == ["agent", "start"])
        argv = started[started.index("--") + 1:]
        self.assertEqual(argv, ["--resume", "resume-id", "-n", "kept name",
                                "--remote-control", "kept name", "--model", "opus",
                                "--settings", '{"disableAgentView": true}'])
        self.assertNotIn(prompt, argv)
        self.assertEqual([row for row in calls if row[2:4] == ["agent", "prompt"]],
                         [["--session", "writing", "agent", "prompt", "w2:p1", prompt]])
        self.assertEqual(conversation_pause.read_scheduled_prompts("resume-id"), (prompt,))

    def test_herdr_restart_refuses_a_pane_with_a_live_agent(self) -> None:
        owner, responses, log = self.herdr_restart_fixture()
        self.write_herdr_responses(responses, [{
            "pane_id": "w1:p1", "agent": "claude", "agent_status": "idle",
        }])
        with self.assertRaisesRegex(OSError, "pane still holds a live agent"):
            _ = restart._launch_session(entry(owner), "restart", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        calls = self.herdr_calls(log)
        self.assertFalse(any(row[2:4] in (["workspace", "create"], ["agent", "start"],
                                        ["agent", "prompt"]) for row in calls))

    def test_herdr_restart_replaces_a_pane_with_no_agent(self) -> None:
        owner, responses, log = self.herdr_restart_fixture()
        self.write_herdr_responses(responses, [{
            "pane_id": "w1:p1", "agent": None, "agent_status": "none",
        }])
        restart._herdr_launch(restart._resume_parts(owner, "restart"), owner, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        self.assertTrue(any(row[2:4] == ["workspace", "create"] for row in self.herdr_calls(log)))

    def test_herdr_restart_starts_a_stopped_server_under_its_named_unit(self) -> None:
        owner, responses, log = self.herdr_restart_fixture()
        replies = cast(dict[str, CommandResponse | list[CommandResponse]], json.loads(responses.read_text()))
        replies["--session writing status server"] = [
            {"stdout": "status: stopped\n", "code": 0},
            {"stdout": "status: running\n", "code": 0},
        ]
        _ = responses.write_text(json.dumps(replies))
        restart._herdr_launch(restart._resume_parts(owner, "restart"), owner, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        calls = self.herdr_calls(log)
        self.assertEqual([row for row in calls if "--unit=herdr-writing" in row], [[
            "--user", "--unit=herdr-writing", "--collect", str(self.root / "fake-herdr"),
            "--session", "writing", "server",
        ]])

    def test_herdr_restart_not_ready_reports_the_screen_without_sending_input(self) -> None:
        owner, responses, log = self.herdr_restart_fixture()
        self.write_herdr_responses(responses, [], not_ready=True)
        with self.assertRaisesRegex(OSError, "Do you trust this folder"):
            _ = restart._launch_session(entry(owner), "restart", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        calls = self.herdr_calls(log)
        self.assertIn(["--session", "writing", "pane", "read", "w2:p1", "--source", "visible"], calls)
        self.assertFalse(any("send-keys" in row or "send-text" in row or "prompt" in row for row in calls))

    def test_herdr_account_move_keeps_claude_arguments_and_refuses_before_start(self) -> None:
        owner, _, log = self.herdr_restart_fixture()
        saved = entry(owner)
        saved["target"] = "claude 3"
        observed: list[restart.ResumeParts] = []
        launch = restart._herdr_launch  # pyright: ignore[reportPrivateUsage]

        def inspect(parts: restart.ResumeParts, session: inventory.Session, *, dry_run: bool) -> None:
            observed.append(parts)
            launch(parts, session, dry_run=dry_run)

        with patch.object(restart, "_herdr_launch", side_effect=inspect):
            with self.assertRaisesRegex(OSError, "credential removal and readiness checks"):
                _ = restart._launch_session(saved, "account move", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0].claude_arguments, (
            "--resume", "resume-id", "-n", "kept name", "--remote-control", "kept name",
            "--model", "opus", "--settings", '{"disableAgentView": true}',
        ))
        self.assertEqual(observed[0].account, restart.MoveResumeAccount("claude 3"))
        self.assertEqual(observed[0].opening_prompt, "account move")
        self.assertFalse(log.exists())
        self.assertEqual(conversation_pause.read_scheduled_prompts("resume-id"), ())

    def test_herdr_resume_never_renders_or_parses_a_shell_command(self) -> None:
        owner, _, log = self.herdr_restart_fixture()
        owner["name"] = "command && ; exec zsh"
        prompt = "command && literal\n'quotes' $(literal)"
        with (
            patch.object(restart, "_resume_shell", side_effect=AssertionError("shell rendered")),
            patch.object(shlex, "split", side_effect=AssertionError("shell parsed")),
        ):
            result = restart._launch_session(entry(owner), prompt, self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(result, restart.SessionLaunched)
        calls = self.herdr_calls(log)
        started = next(row for row in calls if row[2:4] == ["agent", "start"])
        self.assertEqual(started[started.index("--") + 1:], [
            "--resume", "resume-id", "-n", owner["name"], "--remote-control", owner["name"],
            "--model", "opus", "--settings", '{"disableAgentView": true}',
        ])
        self.assertEqual([row[-1] for row in calls if row[2:4] == ["agent", "prompt"]], [prompt])

    def test_herdr_restart_dry_run_starts_no_program_and_records_no_prompt(self) -> None:
        owner, _, log = self.herdr_restart_fixture()
        output = io.StringIO()
        with redirect_stdout(output):
            launch = restart._launch_session(entry(owner), "restart", self.desktop_state, dry_run=True)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(launch, restart.SessionLaunched)
        self.assertIn("herdr writing", output.getvalue())
        self.assertFalse(log.exists())
        self.assertEqual(conversation_pause.read_scheduled_prompts("resume-id"), ())

    def test_herdr_restart_refuses_an_untrusted_folder_before_server_start(self) -> None:
        owner, _, log = self.herdr_restart_fixture()
        config_dir = Path(owner.get("config_dir", ""))
        _ = (config_dir / ".claude.json").write_text('{"projects": {}}')
        self.assertIsInstance(herdr.claude_trust(Path(owner["cwd"]), config_dir / ".claude.json"), herdr.Untrusted)
        with self.assertRaisesRegex(OSError, "has not trusted this folder") as refused:
            _ = restart._launch_session(entry(owner), "restart", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        command = f"cd {shlex.quote(owner['cwd'])} && {named_prefix(str(config_dir))}claude"
        self.assertIn(f"Run: {command}", str(refused.exception))
        self.assertFalse(log.exists())

    def test_herdr_restart_refuses_an_unreadable_trust_file_with_its_account_command(self) -> None:
        owner, _, log = self.herdr_restart_fixture()
        config_dir = Path(owner.get("config_dir", ""))
        _ = (config_dir / ".claude.json").write_text('{"private": "do not disclose"')
        self.assertIsInstance(herdr.claude_trust(Path(owner["cwd"]), config_dir / ".claude.json"), herdr.UnreadableTrustFile)
        with self.assertRaisesRegex(OSError, "cannot read .* to check Claude trust") as refused:
            _ = restart._launch_session(entry(owner), "restart", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        command = f"cd {shlex.quote(owner['cwd'])} && {named_prefix(str(config_dir))}claude"
        self.assertIn(f"Run: {command}", str(refused.exception))
        self.assertNotIn("do not disclose", str(refused.exception))
        self.assertFalse(log.exists())

    def test_herdr_restart_default_account_trust_refusals_clear_named_account_variables(self) -> None:
        owner, _, log = self.herdr_restart_fixture()
        owner["config_dir"] = str(self.root / ".claude")
        for content, trust_type in (("{\"projects\": {}}", herdr.Untrusted), ("{", herdr.UnreadableTrustFile)):
            with self.subTest(trust_type=trust_type):
                _ = (self.root / ".claude.json").write_text(content)
                self.assertIsInstance(herdr.claude_trust(Path(owner["cwd"]), self.root / ".claude.json"), trust_type)
                with self.assertRaises(OSError) as refused:
                    _ = restart._launch_session(entry(owner), "restart", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
                command = (f"cd {shlex.quote(owner['cwd'])} && env -u CLAUDE_CONFIG_DIR "
                           "-u CLAUDE_CODE_REMOTE_MEMORY_DIR claude")
                self.assertIn(f"Run: {command}", str(refused.exception))
                self.assertFalse(log.exists())

    def test_herdr_restart_does_not_accept_trust_from_an_ancestor_folder(self) -> None:
        owner, _, log = self.herdr_restart_fixture()
        config_dir = Path(owner.get("config_dir", ""))
        _ = (config_dir / ".claude.json").write_text(json.dumps({
            "projects": {str(self.root): {"hasTrustDialogAccepted": True}},
        }))
        with self.assertRaisesRegex(OSError, "has not trusted this folder") as refused:
            _ = restart._launch_session(entry(owner), "restart", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        command = f"cd {shlex.quote(owner['cwd'])} && {named_prefix(str(config_dir))}claude"
        self.assertIn(f"Run: {command}", str(refused.exception))
        self.assertFalse(log.exists())

    def test_herdr_unit_restart_and_move_require_manual_account_exec_without_pane_changes(self) -> None:
        """F001/F002: unrestricted unit names never reach native herdr agent launch."""
        template, _, log = self.herdr_restart_fixture()
        checkpoint = "finish Parser Review; keep 'quotes' and $(literal)"
        for label in ("default", "claude 3"):
            self.configure_groups(units=label)
            for moved in (False, True):
                for already_on_group in (False, True):
                    for dry_run in (False, True):
                        with self.subTest(label=label, moved=moved,
                                          already_on_group=already_on_group, dry_run=dry_run):
                            owner = unit("parser-review", session_host="herdr", session_name="writing")
                            owner["name"] = "Parser Review"
                            owner["cwd"] = template["cwd"]
                            directory = accounts.main_config_dir() if label == "default" else accounts.account_dir(label)
                            old_directory = accounts.account_dir("claude 1")
                            owner["config_dir"] = str(directory if already_on_group else old_directory)
                            saved = entry(owner, where=record.WhereSaid(kind="said", text=checkpoint, at=STOPPED))
                            if moved:
                                saved["target"] = label
                                saved["source_label"] = LABEL
                                saved["why"] = "checkpoint move"
                            note = restart.restart_note(saved, RESTARTED)
                            with (patch.object(herdr, "open_pane") as opened,
                                  patch.object(herdr.Herdr, "close_pane") as closed,
                                  patch.object(herdr.Herdr, "start_agent") as started,
                                  patch.object(restart, "_run") as other_host):
                                result = restart._launch_session(saved, note, self.desktop_state, dry_run=dry_run)  # pyright: ignore[reportPrivateUsage]
                            self.assertIsInstance(result, restart.ManualRestartRequired)
                            assert isinstance(result, restart.ManualRestartRequired)
                            arguments = shlex.split(result.command.removesuffix("; exec zsh"))
                            self.assertIn(str(restart.SCRIPTS / "accounts" / "accounts.py"), arguments)
                            self.assertEqual(arguments[arguments.index("exec") + 1], label)
                            self.assertEqual(arguments[arguments.index("--resume") + 1], "parser-review")
                            self.assertEqual(arguments[arguments.index("-n") + 1], "Parser Review")
                            self.assertIn(checkpoint, arguments[-1])
                            opened.assert_not_called()
                            closed.assert_not_called()
                            started.assert_not_called()
                            other_host.assert_not_called()
                            self.assertFalse(log.exists())
                            self.assertEqual(conversation_pause.read_scheduled_prompts("parser-review"), ())

    def test_herdr_showrunner_off_account_restart_and_move_require_manual_account_exec(self) -> None:
        """F001: account changes never rely on PATH inside a herdr shell."""
        template, _, log = self.herdr_restart_fixture()
        checkpoint = "continue the saved showrunner checkpoint"
        for label in ("default", "claude 3"):
            self.configure_groups(showrunners=label)
            for moved in (False, True):
                for dry_run in (False, True):
                    with self.subTest(label=label, moved=moved, dry_run=dry_run):
                        owner = showrunner("runner-resume", host=template["host"])
                        owner["name"] = "Production Review"
                        owner["cwd"] = template["cwd"]
                        owner["config_dir"] = str(accounts.account_dir("claude 1"))
                        saved = entry(owner, where=record.WhereSaid(kind="said", text=checkpoint, at=STOPPED))
                        if moved:
                            saved["target"] = label
                            saved["source_label"] = LABEL
                            saved["why"] = "checkpoint move"
                        note = restart.restart_note(saved, RESTARTED)
                        with (patch.object(herdr, "open_pane") as opened,
                              patch.object(herdr.Herdr, "close_pane") as closed,
                              patch.object(herdr.Herdr, "start_agent") as started,
                              patch.object(restart, "_run") as other_host):
                            result = restart._launch_session(saved, note, self.desktop_state, dry_run=dry_run)  # pyright: ignore[reportPrivateUsage]
                        self.assertIsInstance(result, restart.ManualRestartRequired)
                        assert isinstance(result, restart.ManualRestartRequired)
                        arguments = shlex.split(result.command.removesuffix("; exec zsh"))
                        self.assertIn(str(restart.SCRIPTS / "accounts" / "accounts.py"), arguments)
                        self.assertEqual(arguments[arguments.index("exec") + 1], label)
                        self.assertEqual(arguments[arguments.index("--resume") + 1], "runner-resume")
                        self.assertIn(checkpoint, arguments[-1])
                        opened.assert_not_called()
                        closed.assert_not_called()
                        started.assert_not_called()
                        other_host.assert_not_called()
                        self.assertFalse(log.exists())
                        self.assertEqual(conversation_pause.read_scheduled_prompts("runner-resume"), ())

    def test_herdr_showrunner_already_on_configured_account_retains_native_resume(self) -> None:
        template, _, log = self.herdr_restart_fixture()
        directory = accounts.account_dir("claude 3")
        _ = (directory / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "third@example.invalid"},
            "projects": {template["cwd"]: {"hasTrustDialogAccepted": True}},
        }))
        self.configure_groups(showrunners="claude 3")
        owner = showrunner("runner-resume", host=template["host"])
        owner["name"] = "production-review"
        owner["cwd"] = template["cwd"]
        owner["config_dir"] = str(directory)
        note = restart.restart_note(entry(owner), RESTARTED)
        parts = restart._resume_parts(owner, note)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(parts.account, restart.RetainResumeAccount(directory))
        with patch.object(restart, "_run") as other_host:
            result = restart._launch_session(entry(owner), note, self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(result, restart.SessionLaunched)
        other_host.assert_not_called()
        calls = self.herdr_calls(log)
        opened = next(row for row in calls if row[2:4] == ["workspace", "create"])
        self.assertIn("CLAUDE_CONFIG_DIR=" + str(directory), opened)
        self.assertIn("CLAUDE_CODE_REMOTE_MEMORY_DIR=" + str(self.root / ".claude"), opened)
        self.assertFalse(any(value.startswith("SHOWRUNNER_UNIT") for value in opened))
        started = next(row for row in calls if row[2:4] == ["agent", "start"])
        self.assertEqual(started[started.index("--resume") + 1], "runner-resume")
        self.assertNotIn("accounts.py", " ".join(started))
        self.assertFalse(any(row[2:4] == ["pane", "close"] for row in calls))
        prompts = [row[-1] for row in calls if row[2:4] == ["agent", "prompt"]]
        self.assertEqual(len(prompts), 1)
        self.assertTrue(prompts[0].startswith("/showrunner:produce "))
        self.assertEqual(conversation_pause.read_scheduled_prompts("runner-resume"), tuple(prompts))

    def test_targeted_unit_and_showrunner_resume_prompts_carry_the_ready_checkpoint(self) -> None:
        checkpoint = "commit the partial parser; keep 'quotes' and $(literal)"
        self.configure_groups(units="claude 3", showrunners="claude 3")
        for owner in (unit("checkpoint-unit"), showrunner("checkpoint-runner")):
            with self.subTest(kind=owner["kind"]):
                saved = entry(owner, where=record.WhereSaid(kind="said", text=checkpoint, at=STOPPED))
                saved["target"] = "claude 3"
                saved["source_label"] = LABEL
                saved["why"] = "weekly refusal"
                note = restart.restart_note(saved, RESTARTED)
                self.assertIn(checkpoint, note)
                self.assertIn("moved from claude 2 to claude 3", note)
                captured: list[str] = []

                def launched(argv: list[str], *, dry_run: bool) -> subprocess.CompletedProcess[str]:
                    _ = dry_run
                    if owner["kind"] == "unit":
                        captured.append(Path(argv[argv.index("--restart-note") + 1]).read_text())
                    elif "-ic" in argv:
                        captured.append(shlex.split(argv[-1].removesuffix("; exec zsh"))[-1])
                    return subprocess.CompletedProcess(argv, 0, "", "")

                with patch.object(restart, "_run", side_effect=launched):
                    result = restart._launch_session(saved, note, self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
                self.assertIsInstance(result, restart.SessionLaunched)
                self.assertEqual(len(captured), 1)
                self.assertIn(checkpoint, captured[0])
                if owner["kind"] == "showrunner":
                    self.assertTrue(captured[0].startswith("/showrunner:produce "))

    def test_move_target_showrunner_resumes_its_production_conversation(self) -> None:
        owner = showrunner("move-showrunner")
        commands, _ = self.moved_restart(owner)
        launch = next(argv for argv in commands if "-ic" in argv)
        arguments = shlex.split(launch[-1].removesuffix("; exec zsh"))
        self.assertIn("accounts.py", " ".join(arguments))
        self.assertEqual(arguments[arguments.index("exec") + 1], "claude 3")
        self.assertEqual(arguments[arguments.index("--resume") + 1], "move-showrunner")
        self.assertTrue(arguments[-1].startswith("/showrunner:produce /tmp/demo production.md resume "))
        self.assertIn("moved from claude 2 to claude 3", arguments[-1])

    def test_moved_showrunners_each_keep_cwd_name_model_and_their_own_production_resume(self) -> None:
        for identity, name, model, doc in (
            ("hana-id", "hana", "opus", "/tmp/hana production.md"),
            ("brp-id", "bevy_brp", "sonnet", "/tmp/brp production.md"),
        ):
            with self.subTest(showrunner=name):
                owner = showrunner(identity, host=inventory.WindowHost(kind="ghostty", window_shell=10,
                    desktop=inventory.NamedDesktop(kind="named", name="Production")))
                owner["name"] = name
                owner["model"] = inventory.ModelName(kind="model", name=model)
                owner["doc"] = doc
                owner["cwd"] = f"/tmp/{name} workspace"
                commands, _ = self.moved_restart(owner)
                command = next(argv[-1] for argv in commands if "ghostty" in argv)
                arguments = shlex.split(command.removesuffix("; exec zsh"))
                self.assertEqual(arguments[:3], ["cd", owner["cwd"], "&&"])
                self.assertEqual(arguments[arguments.index("--resume") + 1], identity)
                self.assertEqual(arguments[arguments.index("-n") + 1], name)
                self.assertEqual(arguments[arguments.index("--remote-control") + 1], name)
                self.assertEqual(arguments[arguments.index("--model") + 1], model)
                self.assertTrue(arguments[-1].startswith(f"/showrunner:produce {doc} resume "))
                self.assertIn("moved from claude 2 to claude 3", arguments[-1])

    def test_moved_ghostty_showrunner_reopens_on_its_recorded_desktop_without_window_title_tracking(self) -> None:
        self.configure_groups(showrunners="claude 3")
        owner = showrunner("hana-id", host=inventory.WindowHost(kind="ghostty", window_shell=123,
            desktop=inventory.NamedDesktop(kind="named", name="Production")))
        saved = entry(owner)
        saved["target"] = "claude 3"
        commands = CommandRecorder()
        with (
            patch.object(restart, "_run", commands),
            patch.object(restart, "_retire"),
            patch.object(restart, "_live_session_ids", return_value=frozenset()),
            patch.object(restart, "_desktop_numbers", return_value={"Production": 4}),
            redirect_stdout(io.StringIO()),
        ):
            _ = restart.relaunch_session(saved, dry_run=True, desktop_state=self.desktop_state)
        launch_index = next(index for index, arguments in enumerate(commands.calls) if "ghostty" in arguments)
        self.assertEqual(commands.calls[launch_index - 1], ["kdotool", "set_desktop", "4"])
        self.assertFalse(any("windowclose" in arguments or "getwindowname" in arguments for arguments in commands.calls))

    def test_live_unknown_host_move_reports_its_manual_resume_line_once(self) -> None:
        saved = entry(top_level("manual-move", name="kept name"), where=record.WhereNotSaid(kind="not said"))
        saved["target"] = "claude 3"
        self.create([saved])
        output = io.StringIO()
        command = restart.resume_command(saved["session"], "claude 3", LABEL)
        with (
            patch.object(restart, "_retire"),
            patch.object(restart, "_live_session_ids", return_value=frozenset()),
            redirect_stdout(output),
        ):
            current = restart.relaunch_session(saved, dry_run=False, desktop_state=self.desktop_state)
            _ = restart.relaunch_session(current, dry_run=False, desktop_state=self.desktop_state)
        self.assertEqual(output.getvalue().splitlines(), [f"manual restart kept name: {command}"])
        self.assertEqual(record.session_records(LOGIN)[0]["progress"]["kind"], "manual restart")

    def test_live_restart_verb_reports_an_unknown_host_move_command_exactly_once(self) -> None:
        saved = entry(top_level("manual-move", name="kept name"), where=record.WhereNotSaid(kind="not said"))
        saved["target"] = "claude 3"
        self.create([saved])
        output = io.StringIO()
        with (
            patch.object(restart, "_retire"),
            patch.object(restart, "_live_session_ids", return_value=frozenset()),
            patch.object(settle, "unrecorded_local_sessions", return_value={}),
            redirect_stdout(output),
        ):
            result = restart.up_result(LOGIN, False, ["manual-move"])
        lines = [line for line in result["lines"] if "manual restart kept name:" in line]
        self.assertEqual(len(lines), 1, "\n".join(result["lines"]))
        self.assertIn(restart.resume_command(saved["session"], "claude 3", LABEL), lines[0])

    def test_move_restart_note_names_source_target_reason_and_continuation_for_every_session_kind(self) -> None:
        for owner in (top_level("top"), unit("unit"), showrunner("runner")):
            with self.subTest(kind=owner["kind"]):
                saved = entry(owner)
                saved["target"] = "claude 3"
                note = restart.restart_note(saved, RESTARTED)
                self.assertIn("moved from claude 2 to claude 3", note.casefold())
                self.assertIn("claude 2 is out of usage", note)
                self.assertIn("account move", note.casefold())
                self.assertIn("not a stop", note.casefold())
                self.assertIn("continue", note.casefold())
                if owner["kind"] == "unit":
                    self.assertIn("/unit:direct", note)

    def test_move_restart_notes_use_the_same_explicit_source_target_and_reason_for_all_hosts(self) -> None:
        owners = (
            top_level("tmux-note", host=tmux_host("tmux-note")),
            top_level("ghostty-note", host=inventory.WindowHost(kind="ghostty", window_shell=10,
                desktop=inventory.DesktopNotInSnapshot(kind="not in snapshot"))),
            top_level("zed-note", host=inventory.WindowHost(kind="zed", window_shell=10,
                desktop=inventory.DesktopNotInSnapshot(kind="not in snapshot"))),
            top_level("terminal-note", host=inventory.TerminalHost(kind="terminal")),
            top_level("unknown-note"), unit("unit-note"), showrunner("runner-note"),
        )
        for owner in owners:
            with self.subTest(host=owner["host"]["kind"], kind=owner["kind"]):
                saved = entry(owner, where=record.WhereNotSaid(kind="not said"))
                saved["target"] = "default"
                saved["source_label"] = "claude 3"
                saved["why"] = "weekly limit reached"
                note = restart.restart_note(saved, RESTARTED)
                self.assertIn("moved from claude 3 to default because weekly limit reached", note)
                self.assertIn("account move, not a stop", note)
                self.assertIn("Continue where you stopped", note)
                command = restart.resume_command(owner, "default", "claude 3", "weekly limit reached")
                arguments = shlex.split(command.removesuffix("; exec zsh"))
                self.assertIn(note, arguments[-1])
                if owner["kind"] == "showrunner":
                    self.assertTrue(arguments[-1].startswith(f"/showrunner:produce {owner['doc']} resume "))
                if owner["kind"] == "unit":
                    self.assertIn("/unit:direct", note)

    def test_notes_use_the_stop_dates_zone_and_explain_a_missing_where(self) -> None:
        october = entry(top_level('october'), where=record.WhereSaid(kind='said', text="quoted 'work' and $()", at=STOPPED))
        october_note = restart.restart_note(records_for_test([october])[0], RESTARTED)
        self.assertIn('stopped 2026-10-09 14:49 PDT', october_note)
        self.assertIn('restarted 2026-10-09 18:02 PDT', october_note)
        self.assertIn("Before it you wrote: quoted 'work' and $().", october_note)
        january_time = '2026-01-15T17:30:00+00:00'
        january = entry(top_level('january'), where=record.WhereNotSaid(kind='not said'))
        january_note = restart.restart_note(records_for_test([january], stopped_at=january_time)[0], '2026-01-15T18:00:00+00:00')
        self.assertIn('stopped 2026-01-15 09:30 PST', january_note)
        self.assertIn('restarted 2026-01-15 10:00 PST', january_note)
        self.assertIn('It left no note of where it was', january_note)
        self.assertNotIn('Before it you wrote', january_note)

    def test_restart_note_adds_only_missing_sentence_punctuation(self) -> None:
        for summary in ('finished the work.', 'finished the work'):
            with self.subTest(summary=summary):
                restored = entry(top_level('punctuation'), where=record.WhereSaid(kind='said', text=summary, at=STOPPED))
                note = restart.restart_note(records_for_test([restored])[0], RESTARTED)
                self.assertIn('Before it you wrote: finished the work. First run', note)
                self.assertNotIn('finished the work.. First run', note)

    def test_restart_prompts_are_scheduled_before_showrunner_and_window_launches(self) -> None:
        self.allow_leaf(ShutdownSendLeaf.RESTART_RETIRE)
        restored_entries = [entry(showrunner('scheduled-showrunner')), entry(top_level('scheduled-window', host=inventory.WindowHost(kind='ghostty', window_shell=10, desktop=inventory.DesktopNotInSnapshot(kind='not in snapshot')))), entry(top_level('scheduled-tmux', host=recorded_tmux_host('scheduled-tmux-host', '%scheduled', 410)))]
        saved = records_for_test(restored_entries)
        self.create(saved)
        expected_prompts: dict[str, str] = {}
        for restored in restored_entries:
            session_id = restored['session']['session_id']
            expected_prompts[session_id] = '/showrunner:produce /tmp/demo production.md resume' if restored['session']['kind'] == 'showrunner' else restart.restart_note(restored, RESTARTED)
            conversation_pause.record_scheduled_prompts(session_id, (f'wakeup for {session_id}',))
        observed_launches: list[str] = []

        def run(argv: list[str], *, dry_run: bool, check: bool=True) -> subprocess.CompletedProcess[str] | None:
            del check
            if dry_run:
                return None
            shell_command = argv[-1]
            if not shell_command.endswith('; exec zsh'):
                return subprocess.CompletedProcess(argv, 0, '', '')
            launched_argv = shlex.split(shell_command.removesuffix('; exec zsh'))
            if '--resume' not in launched_argv:
                return subprocess.CompletedProcess(argv, 0, '', '')
            resume_index = launched_argv.index('--resume')
            session_id = launched_argv[resume_index + 1]
            prompt = expected_prompts[session_id]
            launched_prompt = launched_argv[-1]
            self.assertEqual(launched_prompt, prompt)
            self.assertEqual(conversation_pause.read_scheduled_prompts(session_id), (f'wakeup for {session_id}', prompt))
            self.assertIs(conversation_pause.prompt_source(launched_prompt, lambda: frozenset(), lambda: conversation_pause.read_scheduled_prompts(session_id)), conversation_pause.PromptSource.SCHEDULED)
            observed_launches.append(session_id)
            return subprocess.CompletedProcess(argv, 0, '', '')

        def probe(argv: list[str], *, capture_output: bool, text: bool, check: bool) -> subprocess.CompletedProcess[str]:
            del capture_output, text, check
            if argv[:2] == ['tmux', 'display']:
                return subprocess.CompletedProcess(argv, 0, 'scheduled-tmux-host\t410\t0\n', '')
            if argv[:1] == ['pgrep']:
                return subprocess.CompletedProcess(argv, 1, '', '')
            return subprocess.CompletedProcess(argv, 0, '', '')
        with patch.object(restart, '_run', run), patch.object(restart, '_live_session_ids', return_value=frozenset()), patch.object(restart, 'wait_for_confirmation', side_effect=self.all_sessions_live), patch.object(settle, 'send_message', return_value=settle.MessageSent(kind='sent')), patch.object(settle, 'record_time', return_value=RESTARTED), patch('subprocess.run', probe):
            self.assertEqual(restart.up(LOGIN), 0)
        self.assertCountEqual(observed_launches, ['scheduled-showrunner', 'scheduled-window', 'scheduled-tmux'])

    def test_unit_launch_leaves_prompt_recording_to_add_unit(self) -> None:
        self.allow_leaf(ShutdownSendLeaf.RESTART_RETIRE)
        restored = entry(unit('scheduled-unit'))
        saved = records_for_test([restored])
        self.create(saved)
        conversation_pause.record_scheduled_prompts('scheduled-unit', ('existing wakeup',))
        observed_unit_launch = False

        def run(argv: list[str], *, dry_run: bool, check: bool=True) -> subprocess.CompletedProcess[str] | None:
            nonlocal observed_unit_launch
            del check
            if any((part.endswith('add_unit.py') for part in argv)):
                observed_unit_launch = True
                self.assertEqual(conversation_pause.read_scheduled_prompts('scheduled-unit'), ('existing wakeup',))
            return None if dry_run else subprocess.CompletedProcess(argv, 0, '', '')
        with patch.object(restart, '_run', run), patch.object(restart, '_live_session_ids', return_value=frozenset()), patch.object(restart, 'wait_for_confirmation', side_effect=self.all_sessions_live), patch.object(settle, 'record_time', return_value=RESTARTED):
            self.assertEqual(restart.up(LOGIN), 0)
        self.assertTrue(observed_unit_launch)

    def test_dry_run_does_not_record_restart_notes(self) -> None:
        restored = entry(top_level('dry-scheduled', host=tmux_host('dry-scheduled-host')))
        self.create(records_for_test([restored]))
        conversation_pause.record_scheduled_prompts('dry-scheduled', ('existing wakeup',))
        with patch.object(restart, '_live_session_ids', return_value=frozenset()), patch('subprocess.run', return_value=subprocess.CompletedProcess(['probe'], 1, '', '')), redirect_stdout(io.StringIO()):
            self.assertEqual(restart.up(LOGIN, dry_run=True), 0)
        self.assertEqual(conversation_pause.read_scheduled_prompts('dry-scheduled'), ('existing wakeup',))

    def test_dry_run_prints_each_host_argv_and_changes_nothing(self) -> None:
        entries = [entry(unit('unit')), entry(top_level('ghostty', host=inventory.WindowHost(kind='ghostty', window_shell=10, desktop=inventory.NamedDesktop(kind='named', name='Work Desk')))), entry(top_level('zed', host=inventory.WindowHost(kind='zed', window_shell=11, desktop=inventory.DesktopNotInSnapshot(kind='not in snapshot')))), entry(top_level('terminal', host=inventory.TerminalHost(kind='terminal'))), entry(top_level('tmux', host=recorded_tmux_host('kept tmux', '%dry', 420))), entry(top_level('unknown'))]
        original = records_for_test(entries)
        self.create(original)

        def tmux_exists(argv: list[str], *, capture_output: bool, text: bool, check: bool, env: dict[str, str] | None=None) -> subprocess.CompletedProcess[str]:
            del capture_output, text, check, env
            if argv[:2] == ['tmux', 'display']:
                return subprocess.CompletedProcess(argv, 0, pane_display(argv[-1], 'kept tmux', '$8', '420', '0'), '')
            if argv[:1] == ['pgrep']:
                return subprocess.CompletedProcess(argv, 1, '', '')
            return subprocess.CompletedProcess(argv, 0, '', '')
        output = io.StringIO()
        with patch.object(restart, '_live_session_ids', return_value=frozenset()), patch.object(restart, '_desktop_numbers', return_value={'Work Desk': 2}), patch('subprocess.run', tmux_exists), patch.object(settle, 'record_time', return_value=RESTARTED), redirect_stdout(output):
            self.assertEqual(restart.up(LOGIN, dry_run=True), 0)
        lines = output.getvalue().splitlines()
        argvs = [shlex.split(line) for line in lines if not line.startswith('manual restart')]
        unit_argv = next((argv for argv in argvs if any((part.endswith('add_unit.py') for part in argv))))
        self.assertEqual(unit_argv[unit_argv.index('--production') + 1], '/tmp/demo production.md')
        self.assertEqual(unit_argv[unit_argv.index('--plan') + 1], '/tmp/unit plan.md')
        self.assertEqual(unit_argv[unit_argv.index('--session-name') + 1], 'unit')
        self.assertEqual(unit_argv[unit_argv.index('--host-session') + 1], 'tmux-unit')
        self.assertTrue(any((argv == ['kdotool', 'set_desktop', '2'] for argv in argvs)))
        window_argvs = [argv for argv in argvs if argv[:1] == ['systemd-run']]
        self.assertEqual(len(window_argvs), 2)
        self.assertTrue(all(('ghostty' in argv for argv in window_argvs)))
        if sys.platform == "linux":
            self.assertFalse(any(argv[:1] == ['open'] for argv in argvs))
            self.assertTrue(any(line.startswith('manual restart terminal:') for line in lines))
        else:
            terminal = next(argv for argv in argvs if argv[:1] == ['open'])
            self.assertEqual(terminal[:4], ['open', '-na', '/Applications/Nix Apps/Ghostty.app', '--args'])
        tmux = next((argv for argv in argvs if argv[:2] == ['tmux', 'respawn-pane']))
        self.assertIn('%dry', tmux)
        display = next(argv for argv in argvs if argv[:2] == ['tmux', 'display'])
        self.assertEqual(display[:5], ['tmux', 'display', '-p', '-t', '%dry'])
        self.assertIn('#{session_id}', display[-1])
        self.assertIn('#{start_time}', display[-1])
        self.assertFalse(any(argv[:2] == ['tmux', 'has-session'] for argv in argvs))
        self.assertIn(['pgrep', '-P', '420'], argvs)
        self.assertTrue(any((line.startswith('manual restart unknown:') for line in lines)))
        self.assertCountEqual(record.session_records(LOGIN), original)
        self.assertEqual(list(self.state_root.rglob('restart-note-*.txt')), [])

    def test_unit_resumes_with_its_units_row_plan_when_its_run_record_names_another(self) -> None:
        doc = self.root / 'production.md'
        _ = doc.write_text('\n'.join(['# Demo', '', '## Units', '', '| Unit | Plan | Worktree | Branch | Port | Owns |', '| --- | --- | --- | --- | --- | --- |', '| tables-unit | docs/as-built/tables.md | /w/tables | demo-tables | — | `x/` |', '']), encoding='utf-8')
        moved = entry(unit('tables', doc=str(doc), plan=inventory.PlanFromRunRecord(kind='plan', path='docs/plans/demo-tables-followups.md')))
        self.create(records_for_test([moved]))
        output = io.StringIO()
        with patch.object(restart, '_live_session_ids', return_value=frozenset()), patch.object(settle, 'record_time', return_value=RESTARTED), redirect_stdout(output):
            self.assertEqual(restart.up(LOGIN, dry_run=True), 0)
        argv = next((shlex.split(line) for line in output.getvalue().splitlines() if 'add_unit.py' in line))
        self.assertEqual(argv[argv.index('--plan') + 1], 'docs/as-built/tables.md')

    def test_standby_unit_without_a_run_record_resumes_with_standby(self) -> None:
        doc = self.root / 'production.md'
        _ = doc.write_text('\n'.join(['# Demo', '', '## Units', '', '| Unit | Plan | Worktree | Branch | Port | Owns |', '| --- | --- | --- | --- | --- | --- |', '| gate-unit | standby | /w/gate | demo-gate | — | — |', '']), encoding='utf-8')
        moved = entry(unit('gate', doc=str(doc), plan=inventory.NoRunRecord(kind='no run record')))
        self.create(records_for_test([moved]))
        output = io.StringIO()
        with patch.object(restart, '_live_session_ids', return_value=frozenset()), patch.object(settle, 'record_time', return_value=RESTARTED), redirect_stdout(output):
            self.assertEqual(restart.up(LOGIN, dry_run=True), 0)
        self.assertNotIn('manual restart', output.getvalue())
        argv = next((shlex.split(line) for line in output.getvalue().splitlines() if 'add_unit.py' in line))
        self.assertIn('--standby', argv)
        self.assertNotIn('--plan', argv)

    def test_brief_unit_without_a_run_record_resumes_with_its_stub_plan(self) -> None:
        doc = self.root / 'production.md'
        _ = doc.write_text('\n'.join(['# Demo', '', '## Units', '', '| Unit | Plan | Worktree | Branch | Port | Owns |', '| --- | --- | --- | --- | --- | --- |', '| notes-unit | docs/plans/demo-notes.md | /w/notes | demo-notes | — | — |', '']), encoding='utf-8')
        moved = entry(unit('notes', doc=str(doc), plan=inventory.NoRunRecord(kind='no run record')))
        self.create(records_for_test([moved]))
        output = io.StringIO()
        with patch.object(restart, '_live_session_ids', return_value=frozenset()), patch.object(settle, 'record_time', return_value=RESTARTED), redirect_stdout(output):
            self.assertEqual(restart.up(LOGIN, dry_run=True), 0)
        self.assertNotIn('manual restart', output.getvalue())
        argv = next((shlex.split(line) for line in output.getvalue().splitlines() if 'add_unit.py' in line))
        self.assertEqual(argv[argv.index('--plan') + 1], 'docs/plans/demo-notes.md')
        self.assertNotIn('<plan>', argv)

    def test_unit_with_no_row_plan_and_no_run_record_is_refused_with_its_reason(self) -> None:
        doc = self.root / 'production.md'
        _ = doc.write_text('# Demo\n\n## Units\n', encoding='utf-8')
        moved = entry(unit('lost', doc=str(doc), plan=inventory.NoRunRecord(kind='no run record')))
        with self.assertRaisesRegex(restart.NoUnitPlan, 'no Plan in the Units table'):
            _ = restart._unit_argv(moved)  # pyright: ignore[reportPrivateUsage]
        self.create(records_for_test([moved]))
        output = io.StringIO()
        with patch.object(restart, '_live_session_ids', return_value=frozenset()), patch.object(settle, 'record_time', return_value=RESTARTED), redirect_stdout(output):
            _ = restart.up(LOGIN, dry_run=True)
        self.assertIn('cannot restart', output.getvalue())
        self.assertNotIn('add_unit.py', output.getvalue())

    def test_dry_run_preserves_ad_hoc_directories_and_defers_group_accounts_to_configuration(self) -> None:
        config_dir = str(self.root / "account with 'quotes' and $()")
        restored_entries = [entry(showrunner('named-showrunner')), entry(top_level('named-tmux', host=tmux_host('named-tmux-host'))), entry(top_level('named-terminal', host=inventory.TerminalHost(kind='terminal'))), entry(top_level('named-window', host=inventory.WindowHost(kind='ghostty', window_shell=10, desktop=inventory.DesktopNotInSnapshot(kind='not in snapshot')))), entry(top_level('named-manual')), entry(unit('named-unit'))]
        for restored in restored_entries:
            restored['session']['config_dir'] = config_dir
        saved = records_for_test(restored_entries)
        self.create(saved)
        commands = CommandRecorder()
        output = io.StringIO()
        with patch.object(restart, '_run', commands), patch.object(restart, '_live_session_ids', return_value=frozenset()), patch.object(settle, 'record_time', return_value=RESTARTED), redirect_stdout(output):
            self.assertEqual(restart.up(LOGIN, dry_run=True), 0)
        resume_commands = [argv[-1] for argv in commands.calls if '-ic' in argv]
        self.assertEqual(len(resume_commands), 3 if sys.platform == "linux" else 4)
        for command in resume_commands:
            if "--resume named-showrunner " in command:
                self.assertIn("accounts.py exec default", command)
                self.assertNotIn(config_dir, command)
            else:
                self.assertIn(f'&& {named_prefix(config_dir)}ENABLE_TOOL_SEARCH=true command claude', command)
        self.assertIn(f'{named_prefix(config_dir)}ENABLE_TOOL_SEARCH=true command claude', output.getvalue().split('manual restart named-manual:', 1)[1])
        unit_argv = next((argv for argv in commands.calls if any((part.endswith('add_unit.py') for part in argv))))
        self.assertNotIn("--config-dir", unit_argv)
        self.assertNotIn("--account", unit_argv)
        self.assertNotIn('CLAUDE_CONFIG_DIR', shlex.join(unit_argv))
        self.assertCountEqual(record.session_records(LOGIN), saved)

    def test_default_and_legacy_resume_commands_are_byte_identical(self) -> None:
        restored = entry(top_level('default-command'))
        expected = 'cd /tmp/default-command && ENABLE_TOOL_SEARCH=true command claude --resume default-command -n default-command --remote-control default-command --settings \'{"disableAgentView": true}\' prompt; exec zsh'
        resume_command = cast(Callable[[inventory.Session, str], str], getattr(restart, '_resume_command'))
        self.assertEqual(resume_command(restored['session'], 'prompt'), expected)
        restored['session']['config_dir'] = str(Path.home() / '.claude')
        self.assertEqual(resume_command(restored['session'], 'prompt'), expected)

    def test_resume_parts_preserve_named_account_and_move_shell_commands(self) -> None:
        owner = top_level("resume-id", name="kept name", cwd="/tmp/with space",
                          model=inventory.ModelName(kind="model", name="opus"))
        owner["config_dir"] = "/tmp/account dir"
        suffix = ("--resume resume-id -n 'kept name' --remote-control 'kept name' "
                  "--model opus --settings '{\"disableAgentView\": true}' 'line one\nline two'; exec zsh")
        self.assertEqual(
            restart._resume_command(owner, "line one\nline two"),  # pyright: ignore[reportPrivateUsage]
            "cd '/tmp/with space' && CLAUDE_CONFIG_DIR='/tmp/account dir' "
            + "CLAUDE_CODE_REMOTE_MEMORY_DIR=" + shlex.quote(str(Path.home() / ".claude"))
            + " ENABLE_TOOL_SEARCH=true command claude " + suffix,
        )
        self.assertEqual(
            restart._resume_command(owner, "line one\nline two", "claude 3"),  # pyright: ignore[reportPrivateUsage]
            "cd '/tmp/with space' && ENABLE_TOOL_SEARCH=true command "
            + shlex.quote(sys.executable) + " "
            + shlex.quote(str(restart.SCRIPTS / "accounts" / "accounts.py"))
            + " exec 'claude 3' " + suffix,
        )

    def test_window_dry_run_reads_and_restores_the_starting_desktop(self) -> None:
        restored = entry(top_level('window', host=inventory.WindowHost(kind='ghostty', window_shell=10, desktop=inventory.NamedDesktop(kind='named', name='Work'))))
        saved = records_for_test([restored])
        self.create(saved)
        calls: list[tuple[list[str], bool]] = []

        def run(argv: list[str], *, dry_run: bool, check: bool=True) -> subprocess.CompletedProcess[str] | None:
            del check
            calls.append((argv, dry_run))
            if dry_run:
                print(shlex.join(argv))
                return None
            return subprocess.CompletedProcess(argv, 0, '7\n', '')
        output = io.StringIO()
        with patch.object(restart, '_run', run), patch.object(restart, '_desktop_numbers', return_value={'Work': 3}), patch.object(restart, '_live_session_ids', return_value=frozenset()), patch.object(settle, 'record_time', return_value=RESTARTED), redirect_stdout(output):
            self.assertEqual(restart.up(LOGIN, dry_run=True), 0)
        displayed = [shlex.split(line) for line in output.getvalue().splitlines() if line.startswith(('kdotool', 'systemd-run'))]
        self.assertEqual(displayed[0], ['kdotool', 'get_desktop'])
        self.assertEqual(displayed[1], ['kdotool', 'set_desktop', '3'])
        self.assertEqual(displayed[2][0], 'systemd-run')
        self.assertEqual(displayed[3], ['kdotool', 'set_desktop', '7'])
        self.assertEqual([argv for argv, dry_run in calls if not dry_run], [['kdotool', 'get_desktop']])
        self.assertCountEqual(record.session_records(LOGIN), saved)

    def test_zsh_parses_recorded_values_as_the_same_claude_arguments(self) -> None:
        cwd = self.root / "cwd with 'quotes' and $()"
        cwd.mkdir()
        name = "name with 'quotes' and $()"
        model = "model with 'quotes' and $()"
        where = "where with 'quotes' and $()"
        restored = entry(top_level('quoted-id', name=name, cwd=str(cwd), model=inventory.ModelName(kind='model', name=model), host=inventory.WindowHost(kind='ghostty', window_shell=12, desktop=inventory.DesktopNotInSnapshot(kind='not in snapshot'))), where=record.WhereSaid(kind='said', text=where, at=STOPPED))
        config_dir = str(self.root / "account with 'quotes' and $()")
        restored['session']['config_dir'] = config_dir
        saved = records_for_test([restored])
        self.create(saved)
        output = io.StringIO()
        with patch.object(restart, '_live_session_ids', return_value=frozenset()), patch.object(settle, 'record_time', return_value=RESTARTED), patch('subprocess.run', return_value=subprocess.CompletedProcess(['kdotool', 'get_desktop'], 0, '1\n', '')), redirect_stdout(output):
            self.assertEqual(restart.up(LOGIN, dry_run=True), 0)
        memory_base = str(Path.home() / '.claude')
        launch_line = next((line for line in output.getvalue().splitlines() if 'ghostty' in line))
        outer = shlex.split(launch_line)
        resume_command = outer[outer.index('-ic') + 1]
        binary = self.root / 'bin'
        binary.mkdir()
        argv_log = self.root / 'claude-argv.json'
        claude = binary / 'claude'
        _ = claude.write_text('\n'.join(('#!/usr/bin/env python3', 'import json, os, sys', 'from pathlib import Path', "Path(os.environ['ARGV_LOG']).write_text(json.dumps(sys.argv[1:]))", "Path(os.environ['CONFIG_LOG']).write_text(os.environ['CLAUDE_CONFIG_DIR'])", "Path(os.environ['MEMORY_LOG']).write_text(os.environ['CLAUDE_CODE_REMOTE_MEMORY_DIR'])", '')), encoding='utf-8')
        _ = claude.chmod(493)
        final_zsh = binary / 'zsh'
        _ = final_zsh.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
        _ = final_zsh.chmod(493)
        zdotdir = self.root / 'empty-zdotdir'
        zdotdir.mkdir()
        environment = {**os.environ, 'ARGV_LOG': str(argv_log), 'CONFIG_LOG': str(self.root / 'claude-config-dir'), 'MEMORY_LOG': str(self.root / 'claude-memory-dir'), 'CLAUDE_CONFIG_DIR': '/another-account-in-the-shell', 'HOME': str(self.root), 'PATH': f'{binary}:/usr/bin:/bin', 'ZDOTDIR': str(zdotdir)}
        parsed = subprocess.run(['/bin/zsh', '-f', '-ic', resume_command], cwd=self.root, env=environment, capture_output=True, text=True, check=False, timeout=60)
        self.assertEqual(parsed.returncode, 0, (parsed.stdout, parsed.stderr))
        self.assertEqual((self.root / 'claude-config-dir').read_text(encoding='utf-8'), config_dir)
        self.assertEqual((self.root / 'claude-memory-dir').read_text(encoding='utf-8'), memory_base)
        actual = cast(list[str], cast(object, json.loads(argv_log.read_text(encoding='utf-8'))))
        note = restart.restart_note(restored, RESTARTED)
        self.assertEqual(actual, ['--resume', 'quoted-id', '-n', name, '--remote-control', name, '--model', model, '--settings', '{"disableAgentView": true}', note])

    def test_confirmation_keeps_only_final_poll_records_for_remote_control(self) -> None:
        ids = frozenset({"first", "second"})
        polls = [registry_confirmation(frozenset({"first"})).records, registry_confirmation(ids).records]
        clock = [0.0]
        def sleep(seconds: float) -> None:
            clock[0] += seconds
        with patch.object(sessions, "live_sessions", side_effect=polls) as registry, patch.object(time, "monotonic", side_effect=lambda: clock[0]), patch.object(time, "sleep", side_effect=sleep):
            result = restart.wait_for_confirmation(ids, 1.0)
            self.assertEqual(result.records, polls[-1])
            self.assertEqual(result.live_ids, ids)
            with patch.object(sessions, "live_sessions", side_effect=AssertionError("extra liveness scan")):
                self.assertEqual(restart.wait_for_remote_control(result.records, 1.0), ids)
        self.assertEqual(registry.call_count, 2)

    def test_confirmation_returns_only_confirmed_ids_at_the_shared_deadline(self) -> None:
        clock = [0.0]
        def sleep(seconds: float) -> None:
            clock[0] += seconds
        with patch.object(sessions, "live_sessions", return_value=registry_confirmation(frozenset({"live", "unrelated"})).records), patch.object(time, "monotonic", side_effect=lambda: clock[0]), patch.object(time, "sleep", side_effect=sleep) as sleeping:
            self.assertEqual(restart.wait_for_confirmation(frozenset({"live", "missing"}), 0.2).live_ids, frozenset({"live"}))
        sleeping.assert_called_once_with(0.2)

    def test_remote_control_wait_returns_immediately_when_all_are_active(self) -> None:
        records = registry_confirmation(frozenset({"one", "two"})).records
        with patch.object(time, "sleep") as sleep:
            self.assertEqual(restart.wait_for_remote_control(records, 10), frozenset({"one", "two"}))
        sleep.assert_not_called()

    def test_remote_control_wait_returns_when_last_session_activates_or_deadline_expires(self) -> None:
        records = registry_confirmation(frozenset({"one", "two"})).records
        for activates in (True, False):
            with self.subTest(activates=activates):
                clock = [0.0]
                def sleep(seconds: float) -> None:
                    clock[0] += seconds
                def probe(item: sessions.SessionRecord) -> str:
                    return "active" if item["sessionId"] == "one" or (activates and clock[0] >= 1) else "absent"
                with patch.object(sessions, "remote_control", side_effect=probe), patch.object(time, "monotonic", side_effect=lambda: clock[0]), patch.object(time, "sleep", side_effect=sleep):
                    self.assertEqual(restart.wait_for_remote_control(records, 2), frozenset({"one", "two"} if activates else {"one"}))
                    self.assertEqual(clock[0], 1 if activates else 2)

    def test_remote_control_slow_probe_starts_no_poll_after_deadline(self) -> None:
        records = registry_confirmation(frozenset({"one", "two"})).records
        clock = [0.0]
        def probe(_item: sessions.SessionRecord) -> str:
            clock[0] += 6
            return "absent"
        with patch.object(sessions, "remote_control", side_effect=probe) as read, patch.object(time, "monotonic", side_effect=lambda: clock[0]), patch.object(time, "sleep") as sleep:
            self.assertEqual(restart.wait_for_remote_control(records, 10), frozenset())
        self.assertEqual(read.call_count, 2)
        sleep.assert_not_called()
        self.assertEqual(clock[0], 12)

    def test_parallel_tmux_creation_recovers_by_opening_a_window(self) -> None:
        self.create(records_for_test([entry(top_level(name, host=tmux_host('shared'))) for name in ('alpha', 'bravo')]))
        initial_probes = threading.Barrier(2)
        created = threading.Event()
        lock = threading.Lock()
        probe_count = 0
        attempts: list[list[str]] = []
        commands: list[list[str]] = []

        def probe(argv: list[str], *, dry_run: bool) -> subprocess.CompletedProcess[str]:
            nonlocal probe_count
            self.assertFalse(dry_run)
            self.assertEqual(argv, ['tmux', 'has-session', '-t', '=shared'])
            with lock:
                initial = probe_count < 2
                probe_count += 1
            if initial:
                _ = initial_probes.wait(1)
                return subprocess.CompletedProcess(argv, 1, '', '')
            return subprocess.CompletedProcess(argv, 0 if created.is_set() else 1, '', '')

        def run(argv: list[str], *, dry_run: bool, check: bool=True) -> subprocess.CompletedProcess[str]:
            self.assertFalse(dry_run)
            self.assertTrue(check)
            with lock:
                attempts.append(argv)
                if argv[:2] == ['tmux', 'new-session']:
                    if created.is_set():
                        raise subprocess.CalledProcessError(1, argv, stderr='duplicate session: shared')
                    created.set()
                else:
                    self.assertEqual(argv[:4], ['tmux', 'new-window', '-t', '=shared:'])
                    self.assertTrue(created.is_set())
                commands.append(argv)
            return subprocess.CompletedProcess(argv, 0, '', '')

        def resume(parts: restart.ResumeParts) -> str:
            return f"claude --resume {parts.claude_arguments[1]}"
        with patch.object(restart, '_retire'), patch.object(restart, '_record_scheduled_restart_prompt'), patch.object(restart, '_resume_shell', resume), patch.object(restart, '_probe', probe), patch.object(restart, '_run', run), patch.object(restart, '_session_liveness', return_value=restart.SessionNotLive()), patch.object(restart, '_live_session_ids', return_value=frozenset()), patch.object(restart, 'wait_for_confirmation', side_effect=self.all_sessions_live) as wait, patch.object(settle, 'record_time', return_value=RESTARTED), patch.object(restart, 'archive_session'), redirect_stdout(io.StringIO()):
            self.assertEqual(restart.up(LOGIN), 0)
        wait.assert_called_once_with(frozenset({'alpha', 'bravo'}), restart.SESSION_WAIT_SECONDS)
        self.assertTrue(all((item['progress']['kind'] == 'restarted' for item in record.session_records(LOGIN))))
        self.assertEqual(probe_count, 3)
        self.assertEqual([argv[1] for argv in attempts], ['new-session', 'new-session', 'new-window'])
        self.assertEqual([argv[1] for argv in commands], ['new-session', 'new-window'])
        self.assertEqual(commands[0][2:6], ['-d', '-s', 'shared', '-c'])
        self.assertEqual(commands[1][2:5], ['-t', '=shared:', '-c'])
        self.assertCountEqual([argv[-1] for argv in commands], ['claude --resume alpha', 'claude --resume bravo'])

    def test_tmux_respawns_a_reusable_live_or_dead_pane(self) -> None:
        self.allow_leaf(ShutdownSendLeaf.RESTART_RETIRE)
        cases = [('live', '700', '0'), ('dead', '', '1')]
        for label, pane_pid, pane_dead in cases:
            with self.subTest(label=label):
                commands = CommandRecorder()
                probes: list[list[str]] = []
                restored = top_level(label, cwd='/tmp/restored', host=recorded_tmux_host('kept', f'%{label}', 700))
                self.create(records_for_test([entry(restored)], machine='Mac'))

                def probe(argv: list[str], *, capture_output: bool, text: bool, check: bool) -> subprocess.CompletedProcess[str]:
                    del capture_output, text, check
                    probes.append(argv)
                    if argv[:2] == ['tmux', 'display']:
                        return subprocess.CompletedProcess(argv, 0, pane_display(argv[-1], 'kept', '$8', pane_pid, pane_dead), '')
                    if argv[:1] == ['pgrep']:
                        return subprocess.CompletedProcess(argv, 1, '', '')
                    return subprocess.CompletedProcess(argv, 0, '', '')
                with patch.object(restart, '_run', commands), patch.object(restart, '_live_session_ids', return_value=frozenset()), patch.object(restart, 'wait_for_confirmation', side_effect=self.all_sessions_live), patch('subprocess.run', probe), patch.object(settle, 'record_time', return_value=RESTARTED):
                    self.assertEqual(restart.up(LOGIN), 0)
                launches = [argv for argv in commands.calls if argv[:2] == ['tmux', 'respawn-pane']]
                self.assertEqual(len(launches), 1)
                self.assertEqual(launches[0][:-1], ['tmux', 'respawn-pane', '-k', '-t', f'%{label}', '-c', '/tmp/restored', 'zsh', '-ic'])
                self.assertIn(f'--resume {label}', launches[0][-1])
                self.assertNotIn(['tmux', 'has-session', '-t', '=kept'], probes)
                if label == 'dead':
                    self.assertFalse(any((argv[:1] == ['pgrep'] for argv in probes)))

    def test_tmux_respawns_recorded_pane_when_session_name_has_outer_spaces(self) -> None:
        self.allow_leaf(ShutdownSendLeaf.RESTART_RETIRE)
        commands = CommandRecorder()
        restored = top_level('spaced', host=recorded_tmux_host(' work ', '%8', 700))
        self.create(records_for_test([entry(restored)], machine='Mac'))

        def probe(argv: list[str], *, capture_output: bool, text: bool, check: bool) -> subprocess.CompletedProcess[str]:
            del capture_output, text, check
            if argv[:2] == ['tmux', 'display']:
                return subprocess.CompletedProcess(argv, 0, pane_display(argv[-1], ' work ', '$8', '700', '0'), '')
            if argv[:1] == ['pgrep']:
                return subprocess.CompletedProcess(argv, 1, '', '')
            return subprocess.CompletedProcess(argv, 0, '', '')
        with patch.object(restart, '_run', commands), patch.object(restart, '_live_session_ids', return_value=frozenset()), patch.object(restart, 'wait_for_confirmation', side_effect=self.all_sessions_live), patch('subprocess.run', probe), patch.object(settle, 'record_time', return_value=RESTARTED):
            self.assertEqual(restart.up(LOGIN), 0)
        self.assertEqual(len([argv for argv in commands.calls if argv[:2] == ['tmux', 'respawn-pane']]), 1)

    def test_tmux_falls_back_when_a_recorded_pane_is_in_use_or_gone(self) -> None:
        self.allow_leaf(ShutdownSendLeaf.RESTART_RETIRE)
        recorded = inventory.TmuxPane(kind='pane', pane_id='%7', pane_pid=700)
        fallbacks: list[tuple[str, inventory.TmuxPane | inventory.PaneNotRecorded, int, str, int]] = [('child process', recorded, 0, 'kept\t700\t0\n', 0), ('different pane pid', recorded, 0, 'kept\t701\t0\n', 1), ('missing pane', recorded, 1, '', 1), ('pane not recorded', inventory.PaneNotRecorded(kind='not recorded'), 1, '', 1)]
        for label, pane, display_status, display_output, pgrep_status in fallbacks:
            for session_survives in (True, False):
                with self.subTest(label=label, session_survives=session_survives), patch.dict(os.environ, {'SHUTDOWN_STATE_DIR': str(self.root / (label + str(session_survives)))}):
                    commands = CommandRecorder()
                    probes: list[list[str]] = []
                    restored = top_level('fallback', cwd='/tmp/fallback', host=inventory.TmuxHost(kind='tmux', tmux_session='kept', server=inventory.TmuxServer(kind='server', pid=34500, started_at=1700000000), session_ref=inventory.TmuxSessionIdNotRecorded(kind='not recorded'), pane=pane))
                    self.create(records_for_test([entry(restored)], machine='Mac'))

                    def probe(argv: list[str], *, capture_output: bool, text: bool, check: bool) -> subprocess.CompletedProcess[str]:
                        del capture_output, text, check
                        probes.append(argv)
                        if argv[:2] == ['tmux', 'display']:
                            name, _, rest = display_output.rstrip('\n').partition('\t')
                            pid, _, dead = rest.partition('\t')
                            output = pane_display(argv[-1], name, '$8', pid, dead) if display_status == 0 else ''
                            return subprocess.CompletedProcess(argv, display_status, output, '')
                        if argv[:1] == ['pgrep']:
                            return subprocess.CompletedProcess(argv, pgrep_status, '', '')
                        return subprocess.CompletedProcess(argv, 0 if session_survives else 1, '', '')
                    with patch.object(restart, '_run', commands), patch.object(restart, '_live_session_ids', return_value=frozenset()), patch.object(restart, 'wait_for_confirmation', side_effect=self.all_sessions_live), patch('subprocess.run', probe), patch.object(settle, 'record_time', return_value=RESTARTED):
                        self.assertEqual(restart.up(LOGIN), 0)
                    launch = next((argv for argv in commands.calls if argv[:1] == ['tmux']))
                    if display_status == 0 and pane['kind'] == 'pane':
                        expected_prefix = ['tmux', 'new-window', '-t', '$8:', '-c', '/tmp/fallback', 'zsh', '-ic']
                    elif session_survives:
                        expected_prefix = ['tmux', 'new-window', '-t', '=kept:', '-c', '/tmp/fallback', 'zsh', '-ic']
                    else:
                        expected_prefix = ['tmux', 'new-session', '-d', '-s', 'kept', '-c', '/tmp/fallback', 'zsh', '-ic']
                    self.assertEqual(launch[:-1], expected_prefix)
                    self.assertIn('--resume fallback', launch[-1])
                    pane_probed = any((argv[:2] == ['tmux', 'display'] for argv in probes))
                    self.assertEqual(pane_probed, pane['kind'] == 'pane')

    def test_renamed_tmux_respawns_its_free_live_or_dead_pane(self) -> None:
        for pane_pid, dead in (("700", "0"), ("", "1")):
            with self.subTest(dead=dead):
                host = recorded_tmux_host("old name", "%8", 700)
                host["session_ref"] = inventory.TmuxSessionId(kind="session id", session_id="$4")
                owner = top_level("owner", host=host)
                probes: list[list[str]] = []
                commands = CommandRecorder()
                def probe(argv: list[str], *, dry_run: bool) -> subprocess.CompletedProcess[str]:
                    self.assertFalse(dry_run)
                    probes.append(argv)
                    if argv[1:2] == ["display"]:
                        return subprocess.CompletedProcess(argv, 0,
                            pane_display(argv[-1], "renamed", "$4", pane_pid, dead), "")
                    self.assertEqual(argv, ["pgrep", "-P", "700"])
                    return subprocess.CompletedProcess(argv, 1, "", "")
                with patch.dict(os.environ, {"SHUTDOWN_TMUX": "/test/tmux"}), patch.object(restart, "_probe", probe), patch.object(restart, "_run", commands):
                    _ = restart._tmux_launch("resume owner", owner, dry_run=False)  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(commands.calls, [["/test/tmux", "respawn-pane", "-k", "-t", "%8",
                    "-c", owner["cwd"], "zsh", "-ic", "resume owner"]])
                self.assertEqual(sum(argv[1:2] == ["display"] for argv in probes), 1)
                if dead == "1":
                    self.assertEqual(len(probes), 1)

    def test_new_window_uses_current_pane_session_then_recorded_id_then_name(self) -> None:
        cases: list[tuple[str, bool, str, int, str, set[str], str]] = [
            ("occupied child", True, "700", 0, "$9", {"$4", "=old name"}, "$9:"),
            ("occupied replacement", True, "701", 1, "$9", {"$4", "=old name"}, "$9:"),
            ("closed pane renamed session", False, "", 1, "", {"$4", "=old name"}, "$4:"),
            ("closed pane missing id", False, "", 1, "", {"=old name"}, "=old name:"),
            ("closed pane missing session", False, "", 1, "", set(), "new session"),
        ]
        for label, pane_exists, pid, child_status, current_id, available, target in cases:
            with self.subTest(route=label):
                host = recorded_tmux_host("old name", "%8", 700)
                host["session_ref"] = inventory.TmuxSessionId(kind="session id", session_id="$4")
                owner = top_level("owner", host=host)
                commands = CommandRecorder()
                probes: list[list[str]] = []
                def probe(argv: list[str], *, dry_run: bool) -> subprocess.CompletedProcess[str]:
                    self.assertFalse(dry_run)
                    probes.append(argv)
                    if argv[1:2] == ["display"] and argv[4] == "%8":
                        self.assertEqual(argv[:5], ["/test/tmux", "display", "-p", "-t", "%8"])
                        self.assertIn("#{session_id}", argv[-1])
                        return subprocess.CompletedProcess(argv, 0 if pane_exists else 1,
                            pane_display(argv[-1], "renamed", current_id, pid, "0") if pane_exists else "", "")
                    if argv[1:2] == ["display"]:
                        self.assertEqual(argv[:5], ["/test/tmux", "display", "-p", "-t", "$4"])
                        self.assertIn("#{start_time}", argv[-1])
                        self.assertIn("#{pid}", argv[-1])
                        return subprocess.CompletedProcess(argv, 0 if "$4" in available else 1,
                            pane_display(argv[-1], "", "$4", "", ""), "")
                    if argv[:1] == ["pgrep"]:
                        return subprocess.CompletedProcess(argv, child_status, "123\n" if child_status == 0 else "", "")
                    self.assertEqual(argv[:3], ["/test/tmux", "has-session", "-t"])
                    return subprocess.CompletedProcess(argv, 0 if argv[3] in available else 1, "", "")
                with patch.dict(os.environ, {"SHUTDOWN_TMUX": "/test/tmux"}), patch.object(restart, "_probe", probe), patch.object(restart, "_run", commands):
                    _ = restart._tmux_launch("resume owner", owner, dry_run=False)  # pyright: ignore[reportPrivateUsage]
                prefix = (["/test/tmux", "new-session", "-d", "-s", "old name"] if target == "new session"
                          else ["/test/tmux", "new-window", "-t", target])
                self.assertEqual(commands.calls, [prefix + ["-c", owner["cwd"], "zsh", "-ic", "resume owner"]])
                session_checks = [argv[4] if argv[1:2] == ["display"] else argv[3]
                    for argv in probes if argv[1:2] == ["has-session"] or (argv[1:2] == ["display"] and argv[4] == "$4")]
                expected = [] if pane_exists else (["$4"] if "$4" in available else ["$4", "=old name"])
                self.assertEqual(session_checks, expected)
                self.assertEqual(sum(argv[1:2] == ["display"] and argv[4] == "%8" for argv in probes), 1)

    def test_changed_server_rejects_free_pane_and_recorded_session_id(self) -> None:
        for pane_exists in (True, False):
            for name_exists in (True, False):
                with self.subTest(pane_exists=pane_exists, name_exists=name_exists):
                    host = recorded_tmux_host("old name", "%8", 700)
                    host["session_ref"] = inventory.TmuxSessionId(kind="session id", session_id="$4")
                    owner = top_level("owner", host=host)
                    commands = CommandRecorder()
                    probes: list[list[str]] = []
                    def probe(argv: list[str], *, dry_run: bool) -> subprocess.CompletedProcess[str]:
                        self.assertFalse(dry_run)
                        probes.append(argv)
                        if argv[1:2] == ["display"]:
                            self.assertIn("#{start_time}", argv[-1])
                            self.assertIn(argv[4], ("%8", "$4"))
                            return subprocess.CompletedProcess(argv,
                                0 if argv[4] == "$4" or pane_exists else 1,
                                pane_display(argv[-1], "unrelated", "$9", "700", "0", "1700000001"), "")
                        self.assertEqual(argv, ["/test/tmux", "has-session", "-t", "=old name"])
                        return subprocess.CompletedProcess(argv, 0 if name_exists else 1, "", "")
                    with patch.dict(os.environ, {"SHUTDOWN_TMUX": "/test/tmux"}), patch.object(restart, "_probe", probe), patch.object(restart, "_run", commands):
                        _ = restart._tmux_launch("resume owner", owner, dry_run=False)  # pyright: ignore[reportPrivateUsage]
                    prefix = (["/test/tmux", "new-window", "-t", "=old name:"] if name_exists
                              else ["/test/tmux", "new-session", "-d", "-s", "old name"])
                    self.assertEqual(commands.calls, [prefix + ["-c", owner["cwd"], "zsh", "-ic", "resume owner"]])
                    self.assertEqual([argv[4] for argv in probes if argv[1:2] == ["display"]], ["%8", "$4"])

    def test_same_second_replacement_server_uses_name_or_creation_for_every_pane_state(self) -> None:
        for pane_state in ("free", "occupied", "closed"):
            for name_exists in (True, False):
                with self.subTest(pane_state=pane_state, name_exists=name_exists):
                    host = recorded_tmux_host("old name", "%8", 700)
                    host["session_ref"] = inventory.TmuxSessionId(kind="session id", session_id="$4")
                    owner = top_level("owner", host=host)
                    commands = CommandRecorder()
                    probes: list[list[str]] = []
                    def probe(argv: list[str], *, dry_run: bool) -> subprocess.CompletedProcess[str]:
                        self.assertFalse(dry_run)
                        probes.append(argv)
                        if argv[1:2] == ["display"]:
                            self.assertIn(argv[4], ("%8", "$4"))
                            self.assertIn("#{pid}", argv[-1])
                            self.assertIn("#{start_time}", argv[-1])
                            pane_pid = "701" if pane_state == "occupied" else "700"
                            return subprocess.CompletedProcess(argv,
                                0 if argv[4] == "$4" or pane_state != "closed" else 1,
                                pane_display(argv[-1], "unrelated", "$9", pane_pid, "0",
                                    server_pid="34501"), "")
                        self.assertEqual(argv, ["/test/tmux", "has-session", "-t", "=old name"])
                        return subprocess.CompletedProcess(argv, 0 if name_exists else 1, "", "")
                    with patch.dict(os.environ, {"SHUTDOWN_TMUX": "/test/tmux"}), patch.object(restart, "_probe", probe), patch.object(restart, "_run", commands):
                        _ = restart._tmux_launch("resume owner", owner, dry_run=False)  # pyright: ignore[reportPrivateUsage]
                    prefix = (["/test/tmux", "new-window", "-t", "=old name:"] if name_exists
                              else ["/test/tmux", "new-session", "-d", "-s", "old name"])
                    self.assertEqual(commands.calls, [prefix + ["-c", owner["cwd"], "zsh", "-ic", "resume owner"]])
                    self.assertEqual([argv[4] for argv in probes if argv[1:2] == ["display"]], ["%8", "$4"])

    def test_unrecorded_server_uses_name_or_creation_without_id_routes(self) -> None:
        for server_wire in ("not recorded", "missing", "start only"):
            for name_exists in (True, False):
                with self.subTest(server_wire=server_wire, name_exists=name_exists):
                    host = recorded_tmux_host("old name", "%8", 700)
                    host["session_ref"] = inventory.TmuxSessionId(kind="session id", session_id="$4")
                    host["server"] = inventory.TmuxServerNotRecorded(kind="not recorded")
                    saved = entry(top_level("legacy", host=host))
                    value = cast(dict[str, object], cast(object, json.loads(json.dumps(saved))))
                    owner_wire = cast(dict[str, object], value["session"])
                    host_wire = cast(dict[str, object], owner_wire["host"])
                    if server_wire == "missing":
                        del host_wire["server"]
                    elif server_wire == "start only":
                        host_wire["server"] = {"kind": "server", "started_at": 1700000000}
                    parsed = record.parse_session_records(json.dumps([value]))[0]["session"]
                    self.assertEqual(parsed["host"].get("server"), {"kind": "not recorded"})
                    probes: list[list[str]] = []
                    commands = CommandRecorder()
                    def probe(argv: list[str], *, dry_run: bool) -> subprocess.CompletedProcess[str]:
                        self.assertFalse(dry_run)
                        probes.append(argv)
                        if argv[1:2] == ["display"]:
                            self.assertEqual(argv[:5], ["/test/tmux", "display", "-p", "-t", "%8"])
                            return subprocess.CompletedProcess(argv, 0,
                                pane_display(argv[-1], "unrelated", "$9", "700", "0"), "")
                        self.assertEqual(argv, ["/test/tmux", "has-session", "-t", "=old name"])
                        return subprocess.CompletedProcess(argv, 0 if name_exists else 1, "", "")
                    with patch.dict(os.environ, {"SHUTDOWN_TMUX": "/test/tmux"}), patch.object(restart, "_probe", probe), patch.object(restart, "_run", commands):
                        _ = restart._tmux_launch("resume legacy", parsed, dry_run=False)  # pyright: ignore[reportPrivateUsage]
                    prefix = (["/test/tmux", "new-window", "-t", "=old name:"] if name_exists
                              else ["/test/tmux", "new-session", "-d", "-s", "old name"])
                    self.assertEqual(commands.calls, [prefix + ["-c", parsed["cwd"], "zsh", "-ic", "resume legacy"]])
                    self.assertEqual(probes, [["/test/tmux", "has-session", "-t", "=old name"]])

    def test_legacy_record_without_session_reference_restarts_by_name_or_creates_session(self) -> None:
        for survives in (True, False):
            with self.subTest(session_survives=survives):
                saved = entry(top_level("legacy", host=tmux_host("old name")))
                value = cast(dict[str, object], cast(object, json.loads(json.dumps(saved))))
                owner_wire = cast(dict[str, object], value["session"])
                host_wire = cast(dict[str, object], owner_wire["host"])
                del host_wire["session_ref"]
                del host_wire["server"]
                parsed = record.parse_session_records(json.dumps([value]))[0]
                self.assertEqual(parsed["session"]["host"].get("session_ref"), {"kind": "not recorded"})
                probes: list[list[str]] = []
                commands = CommandRecorder()
                def probe(argv: list[str], *, dry_run: bool) -> subprocess.CompletedProcess[str]:
                    self.assertFalse(dry_run)
                    probes.append(argv)
                    return subprocess.CompletedProcess(argv, 0 if survives else 1, "", "")
                with patch.dict(os.environ, {"SHUTDOWN_TMUX": "/test/tmux"}), patch.object(restart, "_probe", probe), patch.object(restart, "_run", commands):
                    _ = restart._tmux_launch("resume legacy", parsed["session"], dry_run=False)  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(probes, [["/test/tmux", "has-session", "-t", "=old name"]])
                prefix = (["/test/tmux", "new-window", "-t", "=old name:"] if survives
                          else ["/test/tmux", "new-session", "-d", "-s", "old name"])
                self.assertEqual(commands.calls, [prefix + ["-c", parsed["session"]["cwd"], "zsh", "-ic", "resume legacy"]])

    def test_up_prints_a_machine_prefixed_failure_to_stdout(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        with patch.object(restart, '_up', side_effect=RuntimeError('restore broke')), patch.object(settle, 'local_machine', return_value='Mac'), redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(restart.up(LOGIN), 1)
        self.assertEqual(output.getvalue(), 'Mac: restart failed: restore broke\n')
        self.assertEqual(errors.getvalue(), '')

    def test_incomplete_restart_alert_emits_the_named_recovery_action(self) -> None:
        self.allow_leaf(ShutdownSendLeaf.DELIVER_MESSAGE)
        sent: list[tuple[list[str], str]] = []

        def send(argv: list[str], *, input: str, capture_output: bool, text: bool, check: bool, timeout: int) -> subprocess.CompletedProcess[str]:
            del capture_output, text, check, timeout
            sent.append((argv, input))
            return subprocess.CompletedProcess(argv, 0, '', '')
        output = io.StringIO()
        with patch.object(restart, 'named_claude_account', return_value=self.account), patch.object(restart, '_up', return_value=restart.MachineRestartResult('natedev', LABEL, 'nothing')), patch.object(restart, 'run_remote', return_value=(2, '')), patch.object(restart, 'other_machine', return_value='Mac'), patch.object(settle, '_send_command', return_value=['fake-send']), patch.object(subprocess, 'run', send), redirect_stdout(output):
            self.assertEqual(restart.restart(restart.AccountNamed(LABEL), dry_run=False, alerts=restart.AlertWhenNotBack()), 0)
        failure = 'restart incomplete: Mac not reached'
        self.assertIn(failure, output.getvalue())
        self.assertEqual(len(sent), 1)
        args, body = sent[0]
        action_index = args.index('--action')
        self.assertEqual(args[action_index + 1], f'In a Claude session on natedev, run /shutdown status {json.dumps(LABEL)}.')
        self.assertIn('Some Claude sessions did not restart', args)
        self.assertIn(failure, body)
        self.assertLess(len('Action: ' + args[action_index + 1] + '\n' + body), 500)

    def test_restart_push_names_failed_sessions_and_phone_access_effect(self) -> None:
        """F014: recovery explains the named session's failure or missing phone access."""
        result = restart.MachineRestartResult("natedev", "claude 2", "partial", failed=1,
            failed_sessions=(restart.FailedSessionRestart("Beta", "window failed to open"),))
        title, body, action = restart.restart_push("claude 2", restart._result_lines(result),  # pyright: ignore[reportPrivateUsage]
            failed=True, phone_access=False)
        self.assertIn("Beta", title)
        self.assertIn('Session Beta on Claude account "claude 2"', body)
        self.assertIn("window failed to open", body)
        self.assertIn('/shutdown restart "claude 2"', body)
        self.assertIn("Claude session on natedev", action.text)
        _, phone, _ = restart.restart_push("claude 2", ["Owner: running, no Remote Control"],
            failed=False, phone_access=True)
        self.assertIn("cannot reach them from your phone", phone)
        self.assertIn("Affected sessions: Owner.", phone)
        self.assertIn("/remote-control", phone)

    def test_restart_cli_builds_a_never_alert_policy(self) -> None:
        received: list[tuple[restart.AccountNamed | restart.AccountNotNamed, bool, restart.AlertWhenNotBack | restart.NeverAlert]] = []

        def run_restart(account: restart.AccountNamed | restart.AccountNotNamed, *, dry_run: bool, alerts: restart.AlertWhenNotBack | restart.NeverAlert, only: Iterable[str], here: bool) -> int:
            self.assertEqual(tuple(only), ())
            self.assertFalse(here)
            received.append((account, dry_run, alerts))
            return 0
        with patch.object(restart, 'restart', run_restart):
            self.assertEqual(shutdown.main(['restart', LABEL, '--no-alert']), 0)
        self.assertEqual(received, [(restart.AccountNamed(LABEL), False, restart.NeverAlert())])

    def test_restart_help_hides_no_alert(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            _ = shutdown.main(['restart', '--help'])
        self.assertEqual(raised.exception.code, 0)
        self.assertNotIn('--no-alert', output.getvalue())

    def test_unknown_named_account_is_a_usage_error(self) -> None:
        with patch.object(restart, 'named_claude_account', side_effect=UnknownAccountName('nobody')), patch.object(settle, 'alert_user') as alert_user, redirect_stderr(io.StringIO()):
            self.assertEqual(restart.restart(restart.AccountNamed('nobody'), dry_run=False, alerts=restart.AlertWhenNotBack()), 2)
        alert_user.assert_not_called()

    def run_restarts(self, *, live: frozenset[str] = EMPTY_SESSION_IDS, confirmed: frozenset[str] = EMPTY_SESSION_IDS) -> tuple[int, list[str]]:
        launches: list[str] = []

        def launch(current: SessionShutdown, _note: str, _desktop: restart.DesktopLaunchState, *, dry_run: bool) -> restart.SessionLaunched:
            self.assertFalse(dry_run)
            claim = record.find_session(LOGIN, current["session"]["session_id"])
            assert claim is not None
            self.assertEqual(claim["progress"]["kind"], "restarting")
            launches.append(current["session"]["session_id"])
            return restart.SessionLaunched()

        def wait(ids: frozenset[str], _timeout: float) -> restart.RegistryConfirmation:
            return registry_confirmation(ids & confirmed)

        with (
            patch.object(restart, "_retire"),
            patch.object(restart, "_launch_session", launch),
            patch.object(restart, "_live_session_ids", return_value=live),
            patch.object(restart, "wait_for_confirmation", side_effect=wait),
            patch.object(restart, "_send_restart_note", return_value=settle.MessageSent(kind="sent")),
            redirect_stdout(io.StringIO()),
        ):
            result = restart.up(LOGIN)
        return result, launches

    def test_six_tmux_launches_run_at_once(self) -> None:
        saved = [entry(top_level(str(index), host=tmux_host(str(index)))) for index in range(6)]
        self.create(saved)
        threads: set[int] = set()
        lock = threading.Lock()
        launches_in_flight = threading.Barrier(6, timeout=10)
        launches: list[str] = []

        def run(argv: list[str], *, dry_run: bool, check: bool = True) -> subprocess.CompletedProcess[str]:
            del check
            self.assertFalse(dry_run)
            self.assertEqual(argv[:2], ["tmux", "new-session"])
            with lock:
                threads.add(threading.get_ident())
                launches.append(argv[argv.index("-s") + 1])
            _ = launches_in_flight.wait()
            return subprocess.CompletedProcess(argv, 0, "", "")

        def confirm(ids: frozenset[str], timeout: float) -> restart.RegistryConfirmation:
            self.assertCountEqual(launches, [str(index) for index in range(6)])
            return self.all_sessions_live(ids, timeout)

        missing = subprocess.CompletedProcess(["tmux"], 1, "", "")
        with patch.object(restart, "_retire"), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "_run", run), patch.object(restart, "_probe", return_value=missing) as probing, patch.object(restart, "_record_scheduled_restart_prompt"), patch.object(restart, "wait_for_confirmation", side_effect=confirm), patch.object(time, "sleep") as sleeping, redirect_stdout(io.StringIO()):
            self.assertEqual(restart.up(LOGIN), 0)
        self.assertEqual(probing.call_count, 6)
        sleeping.assert_not_called()
        self.assertCountEqual(launches, [str(index) for index in range(6)])
        self.assertEqual(restart.RESTART_LAUNCH_WORKERS, 6)
        self.assertEqual(len(threads), 6)
        self.assertNotIn(threading.get_ident(), threads)
        self.assertEqual(record.session_records(LOGIN), [])

    def test_seven_missing_registry_entries_do_not_delay_the_eighth_launch(self) -> None:
        self.create([entry(top_level(str(index), host=tmux_host(str(index)))) for index in range(8)])
        launched: list[str] = []

        def launch(current: SessionShutdown, _note: str, _state: restart.DesktopLaunchState, *, dry_run: bool) -> restart.SessionLaunched:
            del dry_run
            launched.append(current["session"]["session_id"])
            return restart.SessionLaunched()

        def wait(ids: frozenset[str], timeout: float) -> restart.RegistryConfirmation:
            self.assertCountEqual(launched, [str(index) for index in range(8)])
            self.assertEqual(ids, frozenset(launched))
            self.assertEqual(timeout, restart.SESSION_WAIT_SECONDS)
            return registry_confirmation(frozenset({"7"}))

        with patch.object(restart, "_retire"), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "_launch_session", launch), patch.object(restart, "wait_for_confirmation", wait), redirect_stdout(io.StringIO()):
            self.assertEqual(restart.up(LOGIN), 1)
        remaining = record.session_records(LOGIN)
        self.assertEqual(len(remaining), 7)
        self.assertTrue(all(item["progress"]["kind"] == "restart failed" for item in remaining))
        self.assertIsNone(record.find_session(LOGIN, "7"))

    def test_one_launcher_exception_marks_only_its_session_failed(self) -> None:
        self.create([entry(top_level(name, host=tmux_host(name))) for name in ("alpha", "broken", "zeta")])

        def launch(current: SessionShutdown, _note: str, _state: restart.DesktopLaunchState, *, dry_run: bool) -> restart.SessionLaunched:
            del dry_run
            if current["session"]["session_id"] == "broken":
                raise RuntimeError("launch failed\nmore")
            return restart.SessionLaunched()

        with patch.object(restart, "_retire"), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "_launch_session", launch), patch.object(restart, "wait_for_confirmation", side_effect=self.all_sessions_live), redirect_stdout(io.StringIO()):
            self.assertEqual(restart.up(LOGIN), 1)
        remaining = record.session_records(LOGIN)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["session"]["session_id"], "broken")
        self.assertEqual(remaining[0]["progress"], {"kind": "restart failed", "at": RESTARTED, "reason": "launch failed"})

    def test_requester_launches_after_other_sessions_are_confirmed(self) -> None:
        saved = records_for_test([
            entry(top_level("requester", host=tmux_host("requester"))),
            entry(showrunner("showrunner")), entry(unit("unit")),
        ], requester="requester")
        self.create(saved)
        events: list[str] = []

        def launch(current: SessionShutdown, _note: str, _state: restart.DesktopLaunchState, *, dry_run: bool) -> restart.SessionLaunched:
            del dry_run
            name = current["session"]["session_id"]
            if name == "showrunner":
                self.assertNotIn("wait", events)
            if name == "requester":
                self.assertIsNone(record.find_session(LOGIN, "unit"))
                self.assertIsNone(record.find_session(LOGIN, "showrunner"))
            events.append(name)
            return restart.SessionLaunched()

        def wait(ids: frozenset[str], _timeout: float) -> restart.RegistryConfirmation:
            events.append("wait")
            return registry_confirmation(ids)

        with patch.object(restart, "_retire"), patch.object(restart, "_launch_session", launch), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "wait_for_confirmation", wait), patch.object(restart, "_send_restart_note", return_value=settle.MessageSent(kind="sent")), redirect_stdout(io.StringIO()):
            self.assertEqual(restart.up(LOGIN), 0)
        self.assertCountEqual(events[:2], ["unit", "showrunner"])
        self.assertEqual(events[2:], ["wait", "requester", "wait"])

    def test_requester_of_a_separate_request_does_not_wait_on_another_request(self) -> None:
        waiting = entry(top_level("busy"), state=record.SessionWaiting(kind="waiting"))
        waiting["request_id"] = "other"
        requester = entry(top_level("requester", host=tmux_host("requester")))
        requester["requested_by"] = record.FromSession(kind="session", session_id="requester")
        requester["request_id"] = "ready-request"
        self.create([waiting, requester])
        self.assertEqual(self.run_restarts(confirmed=frozenset({"requester"})), (0, ["requester"]))
        self.assertEqual(record.session_records(LOGIN), [waiting])

    def test_recent_relaunch_claim_is_left_to_its_owner(self) -> None:
        saved = entry(top_level("owner", host=tmux_host("owner")))
        saved["progress"] = record.SessionRestarting(kind="restarting", at=RESTARTED, prior=record.SessionStopped(kind="stopped", at=STOPPED))
        self.create([saved])
        with patch.object(restart, "launch_entry") as launch:
            self.assertEqual(restart.relaunch_session(saved, dry_run=False, desktop_state=self.desktop_state), saved)
        launch.assert_not_called()

    def test_stale_relaunch_claim_reuses_its_prior_progress(self) -> None:
        saved = entry(top_level("owner", host=tmux_host("owner")))
        prior = record.SessionStopped(kind="stopped", at=STOPPED)
        saved["progress"] = record.SessionRestarting(kind="restarting", at=STOPPED, prior=prior)
        self.create([saved])
        with patch.object(restart, "_retire"), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "_launch_session", return_value=restart.SessionLaunched()), patch.object(restart, "wait_for_confirmation") as wait:
            current = restart.relaunch_session(saved, dry_run=False, desktop_state=self.desktop_state)
        wait.assert_not_called()
        self.assertEqual(current["progress"], {"kind": "restarting", "at": RESTARTED, "prior": prior})

    def test_launch_and_confirmation_are_separate_stages(self) -> None:
        saved = entry(top_level("owner", host=tmux_host("owner")))
        self.create([saved])
        with patch.object(restart, "_retire"), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "_launch_session", return_value=restart.SessionLaunched()), patch.object(restart, "wait_for_confirmation") as wait:
            current = restart.relaunch_session(saved, dry_run=False, desktop_state=self.desktop_state)
            wait.assert_not_called()
            self.assertEqual(current["progress"]["kind"], "restarting")
            restart.confirm_entry(current, RESTARTED, True)
        finished = record.find_session(LOGIN, "owner")
        assert finished is not None
        self.assertEqual(finished["progress"]["kind"], "restarted")

    def test_restarted_record_is_archived_without_another_launch(self) -> None:
        saved = entry(top_level("done"), state=record.SessionRestarted(kind="restarted", at=RESTARTED))
        self.create([saved])
        result, launched = self.run_restarts()
        self.assertEqual((result, launched), (0, []))
        self.assertEqual(record.session_records(LOGIN), [])

    def test_up_accepts_already_gone_and_identity_lost_progress(self) -> None:
        for state in (
            record.SessionAlreadyGone(kind="already gone", at=STOPPED),
            record.ProcessIdentityLost(kind="process identity lost", at=STOPPED),
        ):
            with self.subTest(state=state["kind"]), patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": str(self.root / state["kind"])}):
                saved = entry(top_level("owner", host=tmux_host("owner")), state=state)
                self.create([saved])
                self.assertEqual(self.run_restarts(confirmed=frozenset({"owner"})), (0, ["owner"]))

    def test_manually_resumed_session_restores_timers_and_is_archived_without_launch(self) -> None:
        saved = entry(top_level("manual"), state=record.SessionNeedsManualRestart(kind="manual restart", command="resume"))
        saved["timers"] = [TimerRestore(instance="timer", was_enabled=True, footer=record.NoFooter(kind="no footer"))]
        self.create([saved])
        with patch.object(settle, "run_notifier") as timers:
            self.assertEqual(self.run_restarts(live=frozenset({"manual"})), (0, []))
        timers.assert_called_once_with("start", "timer")
        self.assertEqual(record.session_records(LOGIN), [])

    def test_manual_session_not_live_keeps_its_resume_command(self) -> None:
        saved = entry(top_level("manual"), state=record.SessionNeedsManualRestart(kind="manual restart", command="resume"))
        self.create([saved])
        self.assertEqual(self.run_restarts(), (1, []))
        self.assertEqual(record.find_session(LOGIN, "manual"), saved)

    def test_pending_timers_only_retry_the_failed_timer_when_live(self) -> None:
        saved = entry(top_level("owner"), state=record.SessionRestoredTimersPending(
            kind="timers pending", at=STOPPED, timers=[record.PendingTimer(instance="pending", reason="failed")],
        ), timers=[
            TimerRestore(instance=name, was_enabled=True, footer=record.NoFooter(kind="no footer"))
            for name in ("done", "pending")
        ])
        self.create([saved])
        with patch.object(restart, "_session_liveness", return_value=restart.SessionLive()), patch.object(settle, "run_notifier") as timers:
            self.assertEqual(self.run_restarts(), (0, []))
        timers.assert_called_once_with("start", "pending")

    def test_unreadable_registry_preserves_pending_timer_progress(self) -> None:
        saved = entry(top_level("owner"), state=record.SessionRestoredTimersPending(kind="timers pending", at=STOPPED, timers=[record.PendingTimer(instance="pending", reason="failed")]))
        self.create([saved])
        with patch.object(restart, "_session_liveness", return_value=restart.SessionLivenessUnknown("unreadable")), patch.object(settle, "run_notifier") as timers:
            self.assertEqual(self.run_restarts(), (1, []))
        timers.assert_not_called()
        self.assertEqual(record.find_session(LOGIN, "owner"), saved)

    def test_failed_launcher_with_unknown_registry_preserves_prior_progress(self) -> None:
        saved = entry(top_level("owner", host=tmux_host("owner")))
        self.create([saved])
        with patch.object(restart, "_retire"), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "_launch_session", side_effect=OSError("failed")), patch.object(restart, "_session_liveness", return_value=restart.SessionLivenessUnknown("unreadable")), redirect_stdout(io.StringIO()):
            self.assertEqual(restart.relaunch_session(saved, dry_run=False, desktop_state=self.desktop_state), saved)
        self.assertEqual(record.find_session(LOGIN, "owner"), saved)

    def test_failed_launcher_with_live_registry_is_confirmed(self) -> None:
        saved = entry(top_level("owner", host=tmux_host("owner")))
        self.create([saved])
        with patch.object(restart, "_retire"), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "_launch_session", side_effect=OSError("failed")), patch.object(restart, "_session_liveness", return_value=restart.SessionLive()), patch.object(restart, "wait_for_confirmation", side_effect=self.all_sessions_live), redirect_stdout(io.StringIO()):
            self.assertEqual(restart.up(LOGIN), 0)
        self.assertIsNone(record.find_session(LOGIN, "owner"))

    def test_queued_showrunner_note_keeps_its_timers_stopped(self) -> None:
        saved = entry(showrunner("owner"))
        saved["timers"] = [TimerRestore(instance="timer", was_enabled=True, footer=record.NoFooter(kind="no footer"))]
        self.create([saved])
        with patch.object(restart, "_send_restart_note", return_value=settle.MessageQueued(kind="queued", reason="not ready")), patch.object(settle, "run_notifier") as timers:
            restart.confirm_entry(saved, RESTARTED, True)
        timers.assert_not_called()
        self.assertEqual(self.stored_record()["progress"]["kind"], "restart failed")

    def test_real_up_clears_a_stop_only_request_hold_after_last_restore(self) -> None:
        saved = entry(top_level("owner", host=tmux_host("owner")))
        saved["then"] = "stop"
        saved["hold"] = "account"
        self.create([saved])
        record.set_account_hold(record.AccountHold(
            login=LOGIN, label=LABEL, machine="natedev", requested_at=STOPPED,
            request_id=saved["request_id"], requested_by=saved["requested_by"], then="stop",
        ))
        self.assertEqual(self.run_restarts(confirmed=frozenset({"owner"})), (0, ["owner"]))
        self.assertIsNone(record.account_hold(LOGIN))

    def test_real_up_clears_an_account_hold_without_session_records(self) -> None:
        record.set_account_hold(record.AccountHold(
            login=LOGIN, label=LABEL, machine="natedev", requested_at=STOPPED,
            request_id="empty", requested_by=record.FromTerminal(kind="terminal"), then="stop",
        ))
        self.assertEqual(restart.up(LOGIN), 0)
        self.assertIsNone(record.account_hold(LOGIN))

    def test_up_keeps_a_hold_while_its_own_request_still_has_a_record(self) -> None:
        waiting = entry(top_level("waiting"), state=record.SessionWaiting(kind="waiting"))
        self.create([waiting])
        record.set_account_hold(record.AccountHold(
            login=LOGIN, label=LABEL, machine="natedev", requested_at=STOPPED,
            request_id=waiting["request_id"], requested_by=waiting["requested_by"], then="stop",
        ))
        self.assertEqual(restart.up(LOGIN), 0)
        self.assertIsNotNone(record.account_hold(LOGIN))

    def test_dry_run_leaves_session_files_and_holds_unchanged(self) -> None:
        saved = entry(top_level("owner"))
        self.create([saved])
        record.set_account_hold(record.AccountHold(
            login=LOGIN, label=LABEL, machine="natedev", requested_at=STOPPED,
            request_id="request", requested_by=saved["requested_by"], then="restart",
        ))
        before = self.files()
        with patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "migrate") as migrate, patch.object(restart, "update_session") as update, patch.object(restart, "archive_session") as archive, patch.object(restart, "clear_finished_account_hold") as clear, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(restart.up(LOGIN, dry_run=True), 0)
        for mutation in (migrate, update, archive, clear):
            mutation.assert_not_called()
        self.assertNotIn("converted", output.getvalue())
        self.assertNotIn("restarted 0 in", output.getvalue())
        self.assertEqual(self.files(), before)

    def files(self) -> dict[str, bytes]:
        return {str(path.relative_to(self.state_root)): path.read_bytes() for path in self.state_root.rglob("*") if path.is_file()}

    def test_restart_dry_run_fixture_preserves_output_and_files(self) -> None:
        saved = [
            entry(top_level("zeta", host=inventory.TerminalHost(kind="terminal"))),
            entry(top_level("alpha", host=inventory.TerminalHost(kind="terminal"))),
            entry(top_level("manual")),
        ]
        self.create(saved)
        before = self.files()
        output = io.StringIO()
        def resume(parts: restart.ResumeParts) -> str:
            return f"claude --resume {parts.claude_arguments[1]}"
        with patch.object(restart, "_resume_shell", side_effect=resume), patch.object(restart, "_live_session_ids", return_value=frozenset()), redirect_stdout(output):
            self.assertEqual(restart.up(LOGIN, dry_run=True), 0)
        self.assertEqual(output.getvalue().replace(str(restart.SCRIPTS), "<scripts>").splitlines(), [
            f"{sys.executable} <scripts>/message/send.py retire --to session:alpha --from shutdown --key shutdown-owner@example.com-alpha",
            ("manual restart alpha: claude --resume alpha" if sys.platform == "linux" else
             "open -na '/Applications/Nix Apps/Ghostty.app' --args -e zsh -ic 'claude --resume alpha'"),
            f"{sys.executable} <scripts>/message/send.py retire --to session:manual --from shutdown --key shutdown-owner@example.com-manual",
            "manual restart manual: claude --resume manual",
            f"{sys.executable} <scripts>/message/send.py retire --to session:zeta --from shutdown --key shutdown-owner@example.com-zeta",
            ("manual restart zeta: claude --resume zeta" if sys.platform == "linux" else
             "open -na '/Applications/Nix Apps/Ghostty.app' --args -e zsh -ic 'claude --resume zeta'"),
        ])
        self.assertEqual(self.files(), before)

    def test_restart_account_choice_includes_a_hold_without_sessions(self) -> None:
        hold = record.AccountHold(
            login=LOGIN, label=LABEL, machine="natedev", requested_at=STOPPED,
            request_id="empty", requested_by=record.FromTerminal(kind="terminal"), then="stop",
        )
        record.set_account_hold(hold)
        with patch.object(restart, "own_claude_account", side_effect=UnreadableAccount("unreadable")), patch.object(restart, "run_remote", return_value=(0, "[]")), patch.object(restart, "_up", return_value=restart.MachineRestartResult("natedev", LABEL, "nothing")) as up, redirect_stdout(io.StringIO()):
            self.assertEqual(restart.restart(restart.AccountNotNamed(), dry_run=False, alerts=restart.NeverAlert()), 0)
        up.assert_called_once_with(LOGIN, False, ())

    def test_restart_account_choice_includes_a_peer_hold_without_sessions(self) -> None:
        hold = record.AccountHold(
            login=LOGIN, label=LABEL, machine="Mac", requested_at=STOPPED,
            request_id="empty", requested_by=record.FromTerminal(kind="terminal"), then="stop",
        )
        def remote(args: list[str], **_kwargs: object) -> tuple[int, str]:
            return (0, json.dumps([hold])) if args[0] == "holds" else (0, "[]") if args[0] == "records" else (0, "")
        with patch.object(restart, "own_claude_account", side_effect=UnreadableAccount("unreadable")), patch.object(restart, "run_remote", remote), patch.object(restart, "_up", return_value=restart.MachineRestartResult("natedev", LABEL, "nothing")) as up, redirect_stdout(io.StringIO()):
            self.assertEqual(restart.restart(restart.AccountNotNamed(), dry_run=False, alerts=restart.NeverAlert()), 0)
        up.assert_called_once_with(LOGIN, False, ())

    def test_window_hosts_launch_serially_on_the_calling_thread_with_spacing(self) -> None:
        saved = [
            entry(top_level("c-terminal", host=inventory.TerminalHost(kind="terminal"))),
            entry(top_level("b-zed", host=inventory.WindowHost(kind="zed", window_shell=11, desktop=inventory.DesktopNotInSnapshot(kind="not in snapshot")))),
            entry(top_level("a-ghostty", host=inventory.WindowHost(kind="ghostty", window_shell=10, desktop=inventory.DesktopNotInSnapshot(kind="not in snapshot")))),
        ]
        self.create(saved)
        caller = threading.get_ident()
        events: list[str] = []

        def run(argv: list[str], *, dry_run: bool, check: bool = True) -> subprocess.CompletedProcess[str]:
            del check
            self.assertFalse(dry_run)
            self.assertEqual(threading.get_ident(), caller)
            if argv[0] in {"systemd-run", "open"}:
                events.append(next(item["session"]["name"] for item in saved if item["session"]["session_id"] in argv[-1]))
            return subprocess.CompletedProcess(argv, 0, "0", "")

        def sleep(seconds: float) -> None:
            self.assertEqual(threading.get_ident(), caller)
            self.assertEqual(seconds, restart.WINDOW_DELAY_SECONDS)
            events.append("delay")

        with patch.object(restart, "_retire"), patch.object(restart, "_record_scheduled_restart_prompt"), patch.object(restart, "_run", run), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "wait_for_confirmation", side_effect=self.all_sessions_live), patch.object(time, "sleep", sleep), redirect_stdout(io.StringIO()):
            self.assertEqual(restart.up(LOGIN), 1 if sys.platform == "linux" else 0)
        self.assertEqual(events, ["a-ghostty", "delay", "b-zed"] if sys.platform == "linux" else
            ["a-ghostty", "delay", "b-zed", "delay", "c-terminal"])

    def test_a_competing_relaunch_claim_is_not_confirmed_by_the_losing_caller(self) -> None:
        saved = entry(top_level("owner", host=tmux_host("owner")))
        self.create([saved])
        entered = threading.Event()
        release = threading.Event()
        results: list[SessionShutdown] = []

        def launch(_record: SessionShutdown, _note: str, _state: restart.DesktopLaunchState, *, dry_run: bool) -> restart.SessionLaunched:
            del dry_run
            entered.set()
            self.assertTrue(release.wait(0.8))
            return restart.SessionLaunched()

        with patch.object(restart, "_retire"), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "_launch_session", launch), patch.object(restart, "wait_for_confirmation") as wait:
            worker = threading.Thread(target=lambda: results.append(restart.relaunch_session(saved, dry_run=False, desktop_state=self.desktop_state)))
            worker.start()
            try:
                self.assertTrue(entered.wait(0.8))
                losing = restart.relaunch_session(saved, dry_run=False, desktop_state=self.desktop_state)
                self.assertEqual(losing["progress"], saved["progress"])
                wait.assert_not_called()
            finally:
                release.set()
                worker.join(0.8)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["progress"]["kind"], "restarting")

    def test_closed_session_resets_all_pending_timers_before_confirmation(self) -> None:
        saved = entry(top_level("owner", host=tmux_host("owner")), state=record.SessionRestoredTimersPending(
            kind="timers pending", at=STOPPED, timers=[record.PendingTimer(instance="pending", reason="failed")],
        ), timers=[
            TimerRestore(instance=name, was_enabled=True, footer=record.NoFooter(kind="no footer"))
            for name in ("done", "pending")
        ])
        self.create([saved])
        with patch.object(restart, "_retire"), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "_session_liveness", return_value=restart.SessionNotLive()), patch.object(restart, "_launch_session", return_value=restart.SessionLaunched()):
            current = restart.relaunch_session(saved, dry_run=False, desktop_state=self.desktop_state)
        progress = current["progress"]
        assert progress["kind"] == "restarting"
        prior = progress["prior"]
        assert prior["kind"] == "timers pending"
        self.assertEqual([timer["instance"] for timer in prior["timers"]], ["done", "pending"])

    def write_old_file(self, saved: list[SessionShutdown]) -> Path:
        account = self.state_root / LOGIN
        account.mkdir(parents=True, exist_ok=True)
        _ = (account / "lock").write_text("")
        entries: list[dict[str, object]] = []
        for item in saved:
            fields = cast(dict[str, object], cast(object, item))
            entries.append({key: fields[key] for key in ("session", "timers", "settle_message", "where", "progress", "stop_issues")})
        wire: dict[str, object] = {
            "login": LOGIN, "label": LABEL, "machine": "natedev",
            "state": "down", "requested_at": STOPPED,
            "requested_by": {"kind": "terminal"},
            "scope": {"kind": "all account sessions"},
            "conductor": {"kind": "not started"}, "force": "wait for ready", "then": "stop",
            "entries": entries, "stop_issues": [],
        }
        path = account / "record.json"
        _ = path.write_text(json.dumps(wire))
        return path

    def test_dry_run_reads_old_files_without_migrating_or_changing_any_path(self) -> None:
        old = self.write_old_file([entry(top_level("owner"))])
        before = self.files()
        directories = sorted(str(path) for path in self.state_root.rglob("*") if path.is_dir())
        with patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "migrate") as migrate, patch.object(restart, "update_session") as update, patch.object(restart, "archive_session") as archive, patch.object(restart, "clear_finished_account_hold") as clear, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(restart.up(LOGIN, dry_run=True), 0)
        for mutation in (migrate, update, archive, clear):
            mutation.assert_not_called()
        self.assertTrue(old.is_file())
        self.assertEqual(self.files(), before)
        self.assertEqual(sorted(str(path) for path in self.state_root.rglob("*") if path.is_dir()), directories)
        self.assertNotIn("converted", output.getvalue())
        self.assertIn("manual restart owner:", output.getvalue())

    def test_real_restart_migrates_a_stop_only_record_and_clears_its_inferred_hold(self) -> None:
        old = self.write_old_file([entry(top_level("owner", host=tmux_host("owner")))])
        self.assertEqual(self.run_restarts(confirmed=frozenset({"owner"})), (0, ["owner"]))
        self.assertFalse(old.exists())
        self.assertEqual(record.session_records(LOGIN), [])
        self.assertIsNone(record.account_hold(LOGIN))

    def test_real_restart_of_an_empty_old_hold_clears_it(self) -> None:
        old = self.write_old_file([])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(restart.up(LOGIN), 0)
        self.assertFalse(old.exists())
        self.assertIsNone(record.account_hold(LOGIN))

    def test_timer_failure_records_pending_work_without_another_launch(self) -> None:
        saved = entry(top_level("owner", host=tmux_host("owner")), timers=[
            TimerRestore(instance="timer", was_enabled=True, footer=record.NoFooter(kind="no footer")),
        ])
        self.create([saved])
        with patch.object(settle, "run_notifier", side_effect=RuntimeError("notifier unavailable")):
            self.assertEqual(self.run_restarts(confirmed=frozenset({"owner"})), (1, ["owner"]))
        current = self.stored_record()
        self.assertEqual(current["progress"], {
            "kind": "timers pending", "at": RESTARTED,
            "timers": [{"instance": "timer", "reason": "notifier unavailable"}],
        })
        with patch.object(restart, "_session_liveness", return_value=restart.SessionLive()), patch.object(settle, "run_notifier") as timer:
            self.assertEqual(self.run_restarts(), (0, []))
        timer.assert_called_once_with("start", "timer")
        self.assertEqual(record.session_records(LOGIN), [])

    def test_disabled_timer_keeps_its_footer_restore_without_starting_the_timer(self) -> None:
        saved = entry(top_level("owner"), timers=[TimerRestore(
            instance="disabled", was_enabled=False, footer=record.ShowrunnerFooter(kind="footer", slug="demo"),
        )])
        self.create([saved])
        with patch.object(settle, "run_notifier") as timer, patch.object(showrunner_footer, "set_footer_state") as footer:
            restart.confirm_entry(saved, RESTARTED, True)
        timer.assert_not_called()
        footer.assert_called_once_with("demo", showrunner_footer.FooterState.ON)

    def test_restart_with_alerts_disabled_does_not_send_a_phone_alert(self) -> None:
        saved = entry(top_level("owner"))
        saved["alerts"] = "none"
        self.create([saved])
        with patch.object(restart, "named_claude_account", return_value=self.account), patch.object(restart, "_up", return_value=restart.MachineRestartResult("natedev", LABEL, "partial")), patch.object(restart, "run_remote", return_value=(1, "failed")), patch.object(settle, "alert_user") as alert, redirect_stdout(io.StringIO()):
            self.assertEqual(restart.restart(restart.AccountNamed(LABEL), dry_run=False, alerts=restart.AlertWhenNotBack()), 1)
        alert.assert_not_called()

    def test_restart_with_an_unreachable_peer_uses_all_local_alert_policies(self) -> None:
        for peer_records in ((255, ""), (0, "invalid JSON")):
            for progress in (
                record.SessionWaiting(kind="waiting"),
                record.SessionReadyToStop(kind="ready", at=STOPPED),
                record.SessionRestarted(kind="restarted", at=RESTARTED),
            ):
                for policy in ("user", "none", "absent"):
                    with self.subTest(peer_records=peer_records, progress=progress, policy=policy):
                        saved = entry(top_level("owner"), state=progress)
                        saved["alerts"] = "none" if policy == "none" else "user"
                        local_records = [] if policy == "absent" else [saved]

                        def remote(args: list[str], **_kwargs: object) -> tuple[int, str]:
                            return peer_records if args[0] == "records" else (255, "")

                        with (
                            patch.object(restart, "named_claude_account", return_value=self.account),
                            patch.object(restart, "_up", return_value=restart.MachineRestartResult("natedev", LABEL, "nothing")),
                            patch.object(restart, "session_records", return_value=local_records),
                            patch.object(restart, "run_remote", remote),
                            patch.object(settle, "alert_user") as alert,
                            redirect_stdout(io.StringIO()),
                        ):
                            self.assertEqual(restart.restart(restart.AccountNamed(LABEL), dry_run=False, alerts=restart.AlertWhenNotBack()), 0)
                        self.assertEqual(alert.call_count, 0 if policy == "none" else 1)

    def test_restart_uses_the_peer_incomplete_records_alert_policy(self) -> None:
        for policy in ("none", "user"):
            with self.subTest(policy=policy):
                saved = entry(top_level("peer"), state=record.SessionRestartFailed(kind="restart failed", at=RESTARTED, reason="not live"))
                saved["machine"] = "Mac"
                saved["alerts"] = "none" if policy == "none" else "user"
                unrelated = copy.deepcopy(saved)
                unrelated["login"] = "other@example.com"
                unrelated["alerts"] = "user"
                calls: list[list[str]] = []

                def remote(args: list[str], **_kwargs: object) -> tuple[int, str]:
                    calls.append(args)
                    if args == ["records", "--json", "--here"]:
                        return 0, json.dumps([saved, unrelated])
                    return 1, peer_json(outcome="partial", lines=["Mac: peer restart failed: not live"])

                with (
                    patch.object(restart, "named_claude_account", return_value=self.account),
                    patch.object(restart, "_up", return_value=restart.MachineRestartResult("natedev", LABEL, "nothing")),
                    patch.object(restart, "run_remote", remote),
                    patch.object(settle, "alert_user") as alert,
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(restart.restart(restart.AccountNamed(LABEL), dry_run=False, alerts=restart.AlertWhenNotBack()), 1)
                self.assertEqual(alert.call_count, 0 if policy == "none" else 1)
                self.assertEqual(calls, [["up", LOGIN, "--here", "--json"], ["records", "--json", "--here"]])

    def test_unreached_restore_uses_local_waiting_policy_without_reading_peer_records(self) -> None:
        replies = [(0, "Mac: restarted"), (0, "{}"), (255, ""), (2, peer_json())]
        for policy in ("user", "none"):
            for reply in replies:
                with self.subTest(policy=policy, reply=reply):
                    saved = entry(top_level("waiting"), state={"kind": "waiting"})
                    saved["alerts"] = policy
                    calls: list[list[str]] = []
                    def remote(args: list[str], **_kwargs: object) -> tuple[int, str]:
                        calls.append(args)
                        return (0, "[]") if args[0] == "records" else reply
                    with (
                        patch.object(restart, "named_claude_account", return_value=self.account),
                        patch.object(restart, "_up", return_value=restart.MachineRestartResult("natedev", LABEL, "nothing")),
                        patch.object(restart, "session_records", return_value=[saved]),
                        patch.object(restart, "run_remote", side_effect=remote),
                        patch.object(settle, "alert_user") as alert,
                        redirect_stdout(io.StringIO()) as output,
                    ):
                        self.assertEqual(restart.restart(restart.AccountNamed(LOGIN), dry_run=False, alerts=restart.AlertWhenNotBack()), 0)
                    self.assertIn("not reached", output.getvalue())
                    self.assertEqual(alert.call_count, int(policy == "user"))
                    self.assertEqual(calls, [["up", LOGIN, "--here", "--json"]])

    def test_unreached_selected_restore_policy_uses_only_chosen_local_records(self) -> None:
        chosen = entry(top_level("chosen"), state={"kind": "waiting"})
        chosen["alerts"] = "none"
        sibling = entry(top_level("sibling"), state={"kind": "waiting"})
        with (
            patch.object(restart, "named_claude_account", return_value=self.account),
            patch.object(restart, "_up", return_value=restart.MachineRestartResult("natedev", LABEL, "nothing")),
            patch.object(restart, "session_records", return_value=[chosen, sibling]),
            patch.object(restart, "run_remote", return_value=(0, "Mac: restarted")) as peer,
            patch.object(settle, "alert_user") as alert,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(restart.restart(restart.AccountNamed(LOGIN), dry_run=False,
                alerts=restart.AlertWhenNotBack(), only=["chosen", "remote"]), 0)
        peer.assert_called_once_with(["up", LOGIN, "--here", "--json", "--only", "chosen,remote"], limit={"kind": "while link alive"})
        alert.assert_not_called()

    def test_restart_alert_policy_ignores_successfully_restored_user_records(self) -> None:
        complete = entry(top_level("complete"))
        incomplete = entry(top_level("incomplete"), state=record.SessionRestartFailed(kind="restart failed", at=RESTARTED, reason="not live"))
        incomplete["alerts"] = "none"
        self.create([complete, incomplete])

        def up(_login: str, _dry_run: bool, _only: Iterable[str]) -> restart.MachineRestartResult:
            record.archive_session(LOGIN, "complete")
            return restart.MachineRestartResult("natedev", LABEL, "partial", failed=1)

        def remote(args: list[str], **_kwargs: object) -> tuple[int, str]:
            return (0, "[]") if args[0] == "records" else (0, peer_json())

        with (
            patch.object(restart, "named_claude_account", return_value=self.account),
            patch.object(restart, "_up", up), patch.object(restart, "run_remote", remote),
            patch.object(settle, "alert_user") as alert, redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(restart.restart(restart.AccountNamed(LABEL), dry_run=False, alerts=restart.AlertWhenNotBack()), 1)
        alert.assert_not_called()

    def test_direct_relaunch_requires_the_callers_desktop_state(self) -> None:
        saved = entry(top_level("window", host=inventory.TerminalHost(kind="terminal")))
        call = cast(Callable[..., SessionShutdown], restart.relaunch_session)
        with patch.object(restart, "launch_entry") as launch:
            with self.assertRaisesRegex(TypeError, "desktop_state"):
                _ = call(saved, dry_run=True)
        launch.assert_not_called()


    def test_dry_run_of_an_old_record_without_a_lock_adds_no_file(self) -> None:
        old = self.write_old_file([entry(top_level("owner"))])
        (old.parent / "lock").unlink()
        before = self.files()
        with patch.object(restart, "_live_session_ids", return_value=frozenset()), redirect_stdout(io.StringIO()):
            self.assertEqual(restart.up(LOGIN, dry_run=True), 0)
        self.assertEqual(self.files(), before)

    def test_relaunch_retries_unconfirmed_herdr_unit_cleanup_before_launch(self) -> None:
        saved = entry(unit("owner", session_host="herdr", session_name="herdr-owner"))
        saved["stop_issues"] = [record.UnitSessionLeftRunning(
            kind="unit session left running", at=STOPPED, host="herdr",
            session_name="herdr-owner", cause="stop not confirmed",
        )]
        self.create([saved])
        events: list[str] = []

        def cleanup(current: SessionShutdown) -> SessionShutdown:
            events.append("cleanup")
            def clean(changing: SessionShutdown) -> None:
                changing["stop_issues"] = []
            return record.update_session(current["login"], current["session"]["session_id"], clean)

        def launch(current: SessionShutdown, _note: str, _state: restart.DesktopLaunchState, *, dry_run: bool) -> restart.SessionLaunched:
            del dry_run
            events.append("launch")
            self.assertEqual(current["stop_issues"], [])
            return restart.SessionLaunched()

        import stop as stop_work
        with patch.object(stop_work, "stop_session", cleanup), patch.object(restart, "_retire"), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "_launch_session", launch):
            _ = restart.relaunch_session(saved, dry_run=False, desktop_state=self.desktop_state)
        self.assertEqual(events, ["cleanup", "launch"])


    def test_unconfirmed_cleanup_prevents_launch_and_timer_restore(self) -> None:
        saved = entry(unit("owner"), timers=[TimerRestore(instance="timer", was_enabled=True, footer=record.NoFooter(kind="no footer"))])
        saved["stop_issues"] = [record.UnitSessionLeftRunning(
            kind="unit session left running", at=STOPPED, host="tmux",
            session_name="tmux-owner", cause="stop not confirmed",
        )]
        self.create([saved])
        import stop as stop_work
        with patch.object(stop_work, "stop_session", return_value=saved), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "_launch_session") as launch, patch.object(settle, "run_notifier") as timers:
            current = restart.relaunch_session(saved, dry_run=False, desktop_state=self.desktop_state)
        launch.assert_not_called()
        timers.assert_not_called()
        self.assertEqual(current["progress"]["kind"], "restart failed")

    def test_cleanup_exception_does_not_prevent_other_sessions_from_returning(self) -> None:
        saved = entry(unit("broken"))
        saved["stop_issues"] = [record.UnitSessionLeftRunning(
            kind="unit session left running", at=STOPPED, host="tmux",
            session_name="tmux-broken", cause="stop not confirmed",
        )]
        self.create([saved, entry(top_level("good", host=tmux_host("good")))])
        import stop as stop_work
        with patch.object(stop_work, "stop_session", side_effect=RuntimeError("cleanup failed")):
            self.assertEqual(self.run_restarts(confirmed=frozenset({"good"})), (1, ["good"]))
        self.assertEqual(record.session_records(LOGIN)[0]["progress"], {
            "kind": "restart failed", "at": RESTARTED, "reason": "cleanup failed",
        })

    def test_an_already_live_owner_is_not_cleaned_up_before_reconciliation(self) -> None:
        saved = entry(unit("owner"))
        saved["stop_issues"] = [record.UnitSessionLeftRunning(
            kind="unit session left running", at=STOPPED, host="tmux",
            session_name="tmux-owner", cause="stop not confirmed",
        )]
        self.create([saved])
        import stop as stop_work
        with patch.object(stop_work, "stop_session") as cleanup:
            self.assertEqual(self.run_restarts(live=frozenset({"owner"})), (0, []))
        cleanup.assert_not_called()


    def test_up_does_not_confirm_a_stale_snapshot_after_losing_its_relaunch_claim(self) -> None:
        for host_kind in ("tmux", "terminal"):
            for requester in (False, True):
                with self.subTest(host=host_kind, requester=requester), patch.dict(os.environ, {
                    "SHUTDOWN_STATE_DIR": str(self.root / f"race-{host_kind}-{requester}"),
                }):
                    host = tmux_host("owner") if host_kind == "tmux" else inventory.TerminalHost(kind="terminal")
                    saved = entry(top_level("owner", host=host))
                    if requester:
                        saved["requested_by"] = record.FromSession(kind="session", session_id="owner")
                    saved["progress"] = record.SessionRestarting(
                        kind="restarting", at=STOPPED,
                        prior=record.SessionStopped(kind="stopped", at=STOPPED),
                    )
                    self.create([saved])
                    entered = threading.Event()
                    release = threading.Event()
                    failures: list[BaseException] = []

                    def launch(_saved: SessionShutdown, _note: str, _desktop: restart.DesktopLaunchState, *, dry_run: bool) -> restart.SessionLaunched:
                        del dry_run
                        entered.set()
                        self.assertTrue(release.wait(0.8))
                        return restart.SessionLaunched()

                    def owner() -> None:
                        try:
                            _ = restart.relaunch_session(saved, dry_run=False, desktop_state=self.desktop_state)
                        except BaseException as error:
                            failures.append(error)

                    reads = 0
                    def read_snapshot(login: str) -> list[SessionShutdown]:
                        nonlocal reads
                        reads += 1
                        return [saved] if reads == 1 else record.session_records(login)

                    with patch.object(restart, "_retire"), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "_launch_session", launch):
                        worker = threading.Thread(target=owner)
                        worker.start()
                        try:
                            self.assertTrue(entered.wait(0.8))
                            with patch.object(restart, "session_records", read_snapshot), patch.object(restart, "wait_for_confirmation", return_value=registry_confirmation(frozenset())) as wait, redirect_stdout(io.StringIO()):
                                self.assertEqual(restart.up(LOGIN), 0)
                            wait.assert_not_called()
                            current = record.find_session(LOGIN, "owner")
                            assert current is not None
                            self.assertEqual(current["progress"], {
                                "kind": "restarting", "at": RESTARTED,
                                "prior": record.SessionStopped(kind="stopped", at=STOPPED),
                            })
                        finally:
                            release.set()
                            worker.join(0.8)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(failures, [])

    def test_up_confirms_a_stale_relaunch_claim_won_by_its_own_worker(self) -> None:
        saved = entry(top_level("owner", host=tmux_host("owner")))
        saved["progress"] = record.SessionRestarting(
            kind="restarting", at=STOPPED,
            prior=record.SessionStopped(kind="stopped", at=STOPPED),
        )
        self.create([saved])
        self.assertEqual(self.run_restarts(confirmed=frozenset({"owner"})), (0, ["owner"]))
        self.assertIsNone(record.find_session(LOGIN, "owner"))


    def test_confirmation_leaves_a_replacement_claim_and_its_timers_unchanged(self) -> None:
        for live in (False, True):
            with self.subTest(live=live), patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": str(self.root / f"confirm-{live}")}):
                saved = entry(top_level("owner"), timers=[TimerRestore(
                    instance="timer", was_enabled=True, footer=record.NoFooter(kind="no footer"),
                )])
                saved["progress"] = record.SessionRestarting(
                    kind="restarting", at=STOPPED, prior=record.SessionStopped(kind="stopped", at=STOPPED),
                )
                self.create([saved])
                replacement = record.SessionRestarting(
                    kind="restarting", at=RESTARTED, prior=record.SessionStopped(kind="stopped", at=STOPPED),
                )
                def replace(current: SessionShutdown) -> None:
                    current["progress"] = replacement
                _ = record.update_session(LOGIN, "owner", replace)
                with patch.object(settle, "run_notifier") as timer:
                    restart.confirm_entry(saved, STOPPED, live)
                timer.assert_not_called()
                self.assertEqual(self.stored_record()["progress"], replacement)

    def test_timer_completion_does_not_overwrite_a_replacement_claim(self) -> None:
        saved = entry(top_level("owner"), timers=[TimerRestore(
            instance="timer", was_enabled=True, footer=record.NoFooter(kind="no footer"),
        )])
        saved["progress"] = record.SessionRestarting(
            kind="restarting", at=STOPPED, prior=record.SessionStopped(kind="stopped", at=STOPPED),
        )
        self.create([saved])
        replacement = record.SessionRestarting(
            kind="restarting", at=RESTARTED, prior=record.SessionStopped(kind="stopped", at=STOPPED),
        )
        def timer(_verb: str, _instance: str) -> None:
            def replace(current: SessionShutdown) -> None:
                current["progress"] = replacement
            _ = record.update_session(LOGIN, "owner", replace)
        with patch.object(settle, "run_notifier", timer):
            restart.confirm_entry(saved, STOPPED, True)
        self.assertEqual(self.stored_record()["progress"], replacement)

    def test_launcher_does_not_return_a_replacement_claim_for_confirmation(self) -> None:
        saved = entry(top_level("owner", host=tmux_host("owner")))
        self.create([saved])
        replacement = record.SessionRestarting(
            kind="restarting", at="2026-10-10T01:04:03+00:00",
            prior=record.SessionStopped(kind="stopped", at=STOPPED),
        )
        def launch(_saved: SessionShutdown, _note: str, _desktop: restart.DesktopLaunchState, *, dry_run: bool) -> restart.SessionLaunched:
            del dry_run
            def replace(current: SessionShutdown) -> None:
                current["progress"] = replacement
            _ = record.update_session(LOGIN, "owner", replace)
            return restart.SessionLaunched()
        with patch.object(restart, "_retire"), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "_launch_session", launch):
            result = restart.relaunch_session(saved, dry_run=False, desktop_state=self.desktop_state)
        self.assertFalse(restart.owns_relaunch_claim(saved, result))
        self.assertEqual(self.stored_record()["progress"], replacement)


    def test_selected_up_restores_requester_while_same_request_siblings_wait(self) -> None:
        requester = entry(top_level("requester", name="Requester"))
        requester["requested_by"] = record.FromSession(kind="session", session_id="requester")
        waiting = entry(top_level("waiting"), state={"kind": "waiting"})
        another_stopped = entry(top_level("another"))
        self.create([requester, waiting, another_stopped])
        launched: list[str] = []
        def launch(saved: SessionShutdown, _note: str, _desktop: restart.DesktopLaunchState, *, dry_run: bool) -> restart.SessionLaunched:
            self.assertFalse(dry_run)
            launched.append(saved["session"]["session_id"])
            return restart.SessionLaunched()
        with patch.object(restart, "_launch_session", side_effect=launch), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "wait_for_confirmation", side_effect=self.all_sessions_live), patch.object(restart, "_retire"), patch.object(restart, "_send_restart_note", return_value=settle.MessageSent(kind="sent")), redirect_stdout(io.StringIO()):
            self.assertEqual(restart.up(LOGIN, only=["requester"]), 0)
        self.assertEqual(launched, ["requester"])
        self.assertIsNone(record.find_session(LOGIN, "requester"))
        self.assertEqual(record.find_session(LOGIN, "waiting"), waiting)
        self.assertEqual(record.find_session(LOGIN, "another"), another_stopped)

    def test_selected_restart_routes_only_peer_id_and_preserves_flags(self) -> None:
        waiting = entry(top_level("waiting"), state={"kind": "waiting"})
        self.create([waiting])
        with patch.object(restart, "named_claude_account", return_value=self.account), patch.object(restart, "run_remote", return_value=(0, peer_json(lines=["Mac: restored Remote"]))) as peer, patch.object(restart, "_launch_session") as launch, patch.object(settle, "alert_user") as alert, redirect_stdout(io.StringIO()):
            self.assertEqual(restart.restart(restart.AccountNamed(LABEL), only=["remote-id"], dry_run=True, alerts=restart.NeverAlert()), 0)
        peer.assert_called_once_with(["up", LOGIN, "--here", "--json", "--dry-run", "--only", "remote-id"], limit={"kind": "while link alive"})
        launch.assert_not_called()
        alert.assert_not_called()
        self.assertEqual(record.find_session(LOGIN, "waiting"), waiting)

    def test_selected_restart_dry_run_changes_no_file_or_other_session(self) -> None:
        selected, other = entry(unit("selected")), entry(unit("other"))
        self.create([selected, other])
        before = {path.relative_to(self.state_root): path.read_bytes() for path in self.state_root.rglob("*") if path.is_file()}
        output = io.StringIO()
        with patch.object(restart, "_run", side_effect=CommandRecorder()), patch.object(restart, "_live_session_ids", return_value=frozenset()), redirect_stdout(output):
            self.assertEqual(restart.up(LOGIN, dry_run=True, only=["selected"]), 0)
        self.assertIn("--resume selected", output.getvalue())
        self.assertNotIn("--resume other", output.getvalue())
        self.assertEqual({path.relative_to(self.state_root): path.read_bytes() for path in self.state_root.rglob("*") if path.is_file()}, before)

    def test_selected_up_refusal_adds_no_failed_or_restarted_count(self) -> None:
        saved = entry(top_level("waiting", name="Waiting"), state={"kind": "waiting"})
        self.create([saved])
        output = io.StringIO()
        with patch.object(restart, "_print_result") as summary, redirect_stdout(output):
            result = restart._up(LOGIN, False, ["waiting"])  # pyright: ignore[reportPrivateUsage]
        self.assertEqual((result.restarted, result.seats, result.manual, result.failed), (0, 0, 0, 0))
        self.assertEqual(result.failed_sessions, ())
        self.assertEqual(output.getvalue(), "Waiting is waiting, not stopped; nothing to restart\n")
        summary.assert_not_called()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(restart.up(LOGIN, only=["waiting"]), 1)

    def test_selected_peer_restart_refusal_exits_one_without_phone_alert_or_summary(self) -> None:
        line = "Peer is waiting, not stopped; nothing to restart"
        output = io.StringIO()
        with patch.object(restart, "named_claude_account", return_value=self.account), patch.object(settle, "inventory", return_value=inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL, sessions=[], unattributed=[])), patch.object(restart, "run_remote", return_value=(1, peer_json(lines=[line], refused=[restart.RestartNotStopped(kind="not stopped", session_id="peer-id", name="Peer", progress="waiting")])) ) as peer, patch.object(settle, "alert_user") as alert, patch.object(restart, "_print_result") as summary, redirect_stdout(output):
            self.assertEqual(restart.restart(restart.AccountNamed(LABEL), only=["peer-id"], dry_run=False, alerts=restart.AlertWhenNotBack()), 1)
        self.assertEqual(output.getvalue(), line + "\n")
        peer.assert_called_once_with(["up", LOGIN, "--here", "--json", "--only", "peer-id"], limit={"kind": "while link alive"})
        alert.assert_not_called()
        summary.assert_not_called()

    def test_selected_usage_limit_held_refusal_has_no_restart_or_failure_count(self) -> None:
        saved = entry(top_level("held", name="Held"), state=record.SessionHeldByUsageLimit(kind="held by usage limit", at=RESTARTED))
        self.create([saved])
        output = io.StringIO()
        with patch.object(restart, "_print_result") as summary, patch.object(restart, "_launch_session") as launch, redirect_stdout(output):
            result = restart._up(LOGIN, False, ["held"])  # pyright: ignore[reportPrivateUsage]
        self.assertEqual((result.restarted, result.seats, result.manual, result.failed), (0, 0, 0, 0))
        self.assertEqual(result.failed_sessions, ())
        self.assertEqual(output.getvalue(), "Held is held by usage limit, not stopped; nothing to restart\n")
        summary.assert_not_called()
        launch.assert_not_called()
        self.assertEqual(record.find_session(LOGIN, "held"), saved)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(restart.up(LOGIN, only=["held"]), 1)

    def test_peer_usage_limit_held_refusal_parses_and_exits_one_without_alert(self) -> None:
        refusal = restart.RestartNotStopped(kind="not stopped", session_id="peer-id", name="Peer", progress="held by usage limit")
        line = "Peer is held by usage limit, not stopped; nothing to restart"
        response = peer_json(lines=[line], refused=[refusal])
        parsed = restart.parse_peer_result(response)
        self.assertEqual(parsed["refused"], [refusal])
        with patch.object(restart, "named_claude_account", return_value=self.account), patch.object(settle, "inventory", return_value=inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL, sessions=[], unattributed=[])), patch.object(restart, "run_remote", return_value=(1, response)), patch.object(settle, "alert_user") as alert, patch.object(restart, "_print_result") as summary, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(restart.restart(restart.AccountNamed(LABEL), only=["peer-id"], dry_run=False, alerts=restart.AlertWhenNotBack()), 1)
        self.assertEqual(output.getvalue(), line + "\n")
        alert.assert_not_called()
        summary.assert_not_called()

    def test_selected_peer_silent_restart_failure_ignores_untouched_local_alert_policy(self) -> None:
        sibling = entry(top_level("sibling"))
        self.create([sibling])
        chosen = entry(top_level("peer", name="Peer"), state={"kind": "restart failed", "at": RESTARTED, "reason": "not live"})
        chosen["machine"], chosen["alerts"] = "Mac", "none"
        def remote(arguments: list[str], **_keywords: object) -> tuple[int, str]:
            return (0, json.dumps([chosen])) if arguments[0] == "records" else (1, peer_json(outcome="partial", lines=["Mac: Peer restart failed: not live"]))
        with patch.object(restart, "named_claude_account", return_value=self.account), patch.object(settle, "inventory", return_value=inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL, sessions=[], unattributed=[])), patch.object(restart, "run_remote", side_effect=remote), patch.object(settle, "alert_user") as alert, redirect_stdout(io.StringIO()):
            self.assertEqual(restart.restart(restart.AccountNamed(LABEL), only=["peer"], dry_run=False, alerts=restart.AlertWhenNotBack()), 1)
        alert.assert_not_called()
        self.assertEqual(record.find_session(LOGIN, "sibling"), sibling)
    def test_remote_control_missing_prints_diagnostic_archives_restart_and_alerts_action(self) -> None:
        for policy in ("user", "none"):
            for command_alerts in (restart.AlertWhenNotBack(), restart.NeverAlert()):
                with self.subTest(policy=policy, command_alerts=type(command_alerts).__name__):
                    saved = entry(top_level("owner", name="Owner", host=tmux_host("owner")))
                    saved["alerts"] = policy
                    saved["requested_at"] = "2026-10-09T21:49:" + str(10 + (2 if policy == "none" else 0) + int(isinstance(command_alerts, restart.NeverAlert))) + "+00:00"
                    self.create([saved])
                    output = io.StringIO()
                    with patch.object(restart, "named_claude_account", return_value=self.account), patch.object(restart, "_retire"), patch.object(restart, "_launch_session", return_value=restart.SessionLaunched()), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "wait_for_confirmation", side_effect=self.all_sessions_live), patch.object(restart, "wait_for_remote_control", return_value=frozenset()) as check, patch.object(restart, "_send_restart_note", return_value=settle.MessageSent(kind="sent")), patch.object(settle, "alert_user") as alert, redirect_stdout(output):
                        self.assertEqual(restart.restart(restart.AccountNamed(LOGIN), dry_run=False, alerts=command_alerts, here=True), 0)
                    self.assertIn("Owner: running, no Remote Control\n", output.getvalue())
                    check.assert_called_once()
                    self.assertIsNone(record.find_session(LOGIN, "owner"))
                    history = list((self.state_root / LOGIN / "history").glob("*.json"))
                    self.assertEqual(cast(dict[str, object], json.loads(history[-1].read_text()))["progress"], {"kind": "restarted", "at": RESTARTED})
                    self.assertEqual(alert.call_count, int(policy == "user" and isinstance(command_alerts, restart.AlertWhenNotBack)))
                    if alert.called:
                        self.assertIn("Owner", cast(str, cast(object, alert.call_args.args[1])))
                        self.assertIn("/remote-control", cast(str, cast(object, alert.call_args.args[1])))
                        self.assertIn("Claude session on natedev", cast(ActionRequired, cast(object, alert.call_args.args[2])).text)
                        self.assertNotIn("run /shutdown restart again", cast(str, cast(object, alert.call_args.args[1])))

    def test_remote_control_active_restart_sends_no_alert(self) -> None:
        saved = entry(top_level("owner", name="Owner", host=tmux_host("owner")))
        self.create([saved])
        with patch.object(restart, "named_claude_account", return_value=self.account), patch.object(restart, "_retire"), patch.object(restart, "_launch_session", return_value=restart.SessionLaunched()), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "wait_for_confirmation", side_effect=self.all_sessions_live), patch.object(restart, "_send_restart_note", return_value=settle.MessageSent(kind="sent")), patch.object(settle, "alert_user") as alert, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(restart.restart(restart.AccountNamed(LOGIN), dry_run=False, alerts=restart.AlertWhenNotBack(), here=True), 0)
        self.assertNotIn("no Remote Control", output.getvalue())
        self.assertIsNone(record.find_session(LOGIN, "owner"))
        alert.assert_not_called()

    def test_remote_control_peer_policy_and_name_survive_archived_record(self) -> None:
        for policy in ("user", "none"):
            with self.subTest(policy=policy):
                diagnostic = "Remote: running, no Remote Control"
                missing = record.NoRemoteControl(session_id="peer", name="Remote", alerts=policy)
                with patch.object(restart, "named_claude_account", return_value=self.account), patch.object(restart, "run_remote", return_value=(0, peer_json(lines=[diagnostic], missing=[missing]))), patch.object(settle, "alert_user") as alert, redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(restart.restart(restart.AccountNamed(LOGIN), dry_run=False, alerts=restart.AlertWhenNotBack()), 0)
                self.assertIn(diagnostic, output.getvalue())
                self.assertEqual(alert.call_count, int(policy == "user"))
                if alert.called:
                    self.assertIn("Remote", cast(str, cast(object, alert.call_args.args[1])))
                    self.assertIn("/remote-control", cast(str, cast(object, alert.call_args.args[1])))
                    self.assertIn("Claude session on natedev", cast(ActionRequired, cast(object, alert.call_args.args[2])).text)

    def test_peer_no_shutdown_refusal_keeps_line_without_alert(self) -> None:
        refusal = restart.RestartNoShutdown(kind="no shutdown", session_id="peer", name="Remote")
        with patch.object(restart, "named_claude_account", return_value=self.account), patch.object(restart, "run_remote", return_value=(1, peer_json(lines=[restart.refusal_line(refusal)], refused=[refusal]))), patch.object(settle, "alert_user") as alert, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(restart.restart(restart.AccountNamed(LOGIN), dry_run=False, alerts=restart.AlertWhenNotBack(), only=["peer"]), 1)
        self.assertEqual(output.getvalue(), "Remote: no shutdown in progress\n")
        alert.assert_not_called()

    def test_peer_refusal_beside_real_failure_alerts_only_the_failure(self) -> None:
        refused = restart.RestartNotStopped(kind="not stopped", session_id="waiting", name="Waiting", progress="waiting")
        lines = [restart.refusal_line(refused), "Mac: Failed restart failed: not live"]
        def remote(args: list[str], **_kwargs: object) -> tuple[int, str]:
            return (0, "[]") if args[0] == "records" else (1, peer_json(outcome="partial", lines=lines, refused=[refused]))
        with patch.object(restart, "named_claude_account", return_value=self.account), patch.object(restart, "run_remote", side_effect=remote), patch.object(settle, "alert_user") as alert, redirect_stdout(io.StringIO()):
            self.assertEqual(restart.restart(restart.AccountNamed(LOGIN), dry_run=False, alerts=restart.AlertWhenNotBack()), 1)
        alert.assert_called_once()
        self.assertIn("Failed", cast(str, cast(object, alert.call_args.args[1])))
        self.assertNotIn("Waiting", cast(str, cast(object, alert.call_args.args[1])))

    def test_peer_result_refuses_old_output_and_invalid_nested_fields(self) -> None:
        valid = cast(dict[str, object], json.loads(peer_json()))
        invalid: list[str] = ["Mac: restarted", "[]", "{}"]
        for key, value in (("outcome", []), ("lines", [3]), ("no_remote_control", [{"session_id": "x", "name": "X", "alerts": "other"}]), ("refused", [{"kind": "not stopped", "session_id": "x", "name": "X", "progress": "stopped"}])):
            invalid.append(json.dumps({**valid, key: value}))
        for response in invalid:
            with self.subTest(response=response), self.assertRaises(ValueError):
                _ = restart.parse_peer_result(response)



if __name__ == "__main__":
    _ = unittest.main()
