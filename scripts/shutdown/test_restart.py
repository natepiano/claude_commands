#!/usr/bin/env python3
"""Behavioral tests for restoring every session in a shutdown record."""

from __future__ import annotations

import copy
import io
import json
from datetime import datetime, timezone, tzinfo
import os
import shlex
import subprocess
import sys
import syslog
import tempfile
import threading
import time
import unittest
from collections.abc import Callable, Iterable, Sequence
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import Literal, TypedDict, cast, final, override
from unittest.mock import patch

from test_support import registry_confirmation, ShutdownSendLeaf, ShutdownTestCase
import inventory
import record
import sessions
import remote_control
from user_action import ActionRequired
from push_texts import RenderedPush
import restart
import launch_permission as restore_permission
import stop as stop_work
import relaunch
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
LABEL = "claude2"
STOPPED = "2026-10-09T21:49:10+00:00"
RESTARTED = "2026-10-10T01:02:03+00:00"
EMPTY_SESSION_IDS: frozenset[str] = frozenset()


class RestartAttemptTime(datetime):
    @classmethod
    @override
    def now(cls, tz: tzinfo | None = None) -> "RestartAttemptTime":
        return cls.fromisoformat(RESTARTED).astimezone(tz)


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
        "updated_at": 0,
        "probe": False,
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


def worker_session(
    session_id: str,
    *,
    owner: str | None,
    run_dirs: list[str] | None = None,
) -> inventory.WorkerSession:
    worker_owner: inventory.WorkerOwner = (
        inventory.DirectorOwner(kind="director", session_id=owner)
        if owner is not None
        else inventory.NoDirector(kind="no director")
    )
    return inventory.WorkerSession(
        **common_session(session_id, run_dirs=run_dirs),
        kind="worker",
        host=inventory.UnknownHost(kind="unknown"),
        owner=worker_owner,
    )


def entry(
    session: inventory.Session, *, state: SessionProgress | None = None,
    where: Where | None = None, timers: list[TimerRestore] | None = None,
) -> SessionShutdown:
    return SessionShutdown(
        login=LOGIN, label=LABEL, machine="natedev", requested_at=STOPPED,
        request_id="request", requested_by=record.FromTerminal(kind="terminal"),
        hold="session", then="restart", force="wait for ready", alerts="user", probe=False,
        session=session, workers=[], timers=list(timers or []),
        settle_message=record.SettleMessageNotSent(kind="not sent"),
        where=where or record.WhereSaid(kind="said", text=f"working on {session['name']}", at=STOPPED),
        progress=state or record.SessionStopped(kind="stopped", at=STOPPED), stop_issues=[], trail=[],
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


class LegacyPeerRemoteControl(TypedDict):
    session_id: str
    name: str
    alerts: Literal["user", "none"]


def peer_json(*, outcome: str = "done", lines: Sequence[str] = (), missing: Sequence[LegacyPeerRemoteControl] = (), refused: Sequence[restart.RestartRefusal] = ()) -> str:
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
        _ = (self.root / "notes/claude2.md").write_text(f"---\nlogin: {LOGIN}\n---\n")
        _ = (self.root / "notes/claude3.md").write_text("---\nlogin: third@example.invalid\n---\n")
        self.assertEqual(accounts.setup("claude3"), [])
        _ = (self.root / ".claude3/.claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "third@example.invalid"}}))
        self.configure_groups()
        _ = self.enterContext(patch.object(settle, "record_time", return_value=RESTARTED))
        _ = self.enterContext(patch.object(settle, "inventory", return_value=inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL, sessions=[], unattributed=[])))

    def configure_groups(self, *, units: str = "claude2", showrunners: str = "claude2") -> None:
        path = accounts.groups_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({"units": units, "showrunners": showrunners}))

    def create(self, records: list[SessionShutdown]) -> None:
        for item in records:
            self.assertTrue(record.create_session(item))

    def recovery(self, owners: Sequence[inventory.Session], *, login: str = LOGIN,
                 at: str = STOPPED) -> record.RecoverySnapshot:
        saved = record.RecoverySnapshot(machine="natedev", login=login,
            label=LABEL if login == LOGIN else "claude3", taken_at=at,
            sessions=[record.SnapshotClaudeSession(agent="claude", session=owner, workers=[], timers=[],
                last_seen=at, state="unresolved", crash="host gone") for owner in owners])
        record.write_snapshot(saved)
        return saved

    def stored_record(self) -> SessionShutdown:
        return record.session_records(LOGIN)[0]

    def moved_record(self, identity: str = "moved-owner") -> SessionShutdown:
        saved = entry(top_level(identity, host=tmux_host(identity)))
        saved["target"] = "claude3"
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
        self.assertEqual(failed["reason"], "still on claude2")
        self.assertNotEqual(self.stored_record()["progress"]["kind"], "restarted")

    def test_move_proof_names_absent_environment_and_mismatch_and_confirms_only_the_target(self) -> None:
        for observation in ("not live", "environment unreadable", "source", "target"):
            with self.subTest(observation=observation):
                saved = self.moved_record("proof-" + observation.replace(" ", "-"))
                transition = "group-" + observation
                saved["move_id"] = transition
                _ = record.update_session(LOGIN, saved["session"]["session_id"],
                    lambda current: current.update(move_id=transition))
                owner = saved["session"]
                live = [] if observation == "not live" else [sessions.SessionRecord(
                    pid=owner["pid"], sessionId=owner["session_id"], name=owner["name"],
                    messagingSocketPath="", updatedAt=1, tmux="", formerNames=[])]
                directory = (None if observation == "environment unreadable" else
                             self.root / (".claude3" if observation == "target" else ".claude2"))
                with (patch.object(sessions, "live_sessions", return_value=live),
                      patch.object(account_reader, "process_config_dir", return_value=directory)):
                    result = restart._prove_move(saved)  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(result, observation == "target")
                entries = [entry for entry in moves.read_moves() if entry["move_id"] == saved["move_id"]]
                self.assertEqual(len(entries), 1)
                outcome = entries[0]
                if observation == "target":
                    self.assertEqual(outcome["kind"], "confirmed")
                    self.assertEqual(outcome.get("config_dir"), str(self.root / ".claude3"))
                else:
                    expected = "still on claude2" if observation == "source" else "resumed account unreadable"
                    self.assertEqual(outcome.get("reason"), expected)
                    stored = record.find_session(LOGIN, owner["session_id"])
                    assert stored is not None
                    self.assertEqual(stored["progress"]["kind"], "restart failed")

    def test_target_cleanup_keeps_records_when_live_session_or_environment_cannot_be_observed(self) -> None:
        for absent in (True, False):
            with self.subTest(absent=absent):
                saved = self.hand_restart_record("absent" if absent else "unreadable")
                owner = saved["session"]
                live = [] if absent else [sessions.SessionRecord(pid=owner["pid"],
                    sessionId=owner["session_id"], name=owner["name"], messagingSocketPath="",
                    updatedAt=1, tmux="", formerNames=[])]
                with (patch.object(sessions, "live_sessions", return_value=live),
                      patch.object(account_reader, "process_config_dir", return_value=None)):
                    checked = restart.clear_running_on_target([saved])
                self.assertEqual((checked.remaining, checked.finished), ([saved], []))
                self.assertEqual(record.find_session(LOGIN, owner["session_id"]), saved)

    def test_failed_attempt_and_successful_retry_journal_one_transition_with_separate_request_claims(self) -> None:
        first = self.moved_record("retry-owner")
        first["move_id"] = "durable-group-transition"
        _ = record.update_session(LOGIN, "retry-owner", lambda current: current.update(move_id=first["move_id"]))
        restart.mark_restart_failed(first, "first launch failed")
        record.archive_session(LOGIN, "retry-owner")
        retry: SessionShutdown = {**first, "request_id": "second-attempt", "progress": record.SessionWaiting(kind="waiting")}
        self.create([retry])
        live = self.running_on(retry, self.root / ".claude3")
        with patch.object(sessions, "live_sessions", return_value=live):
            self.assertTrue(restart._prove_move(retry))  # pyright: ignore[reportPrivateUsage]
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["failed", "confirmed"])
        self.assertEqual({entry["move_id"] for entry in entries}, {"durable-group-transition"})
        # A delayed result for the first attempt cannot modify its replacement.
        restart.mark_restart_failed(first, "stale failure")
        loaded = record.find_session(LOGIN, "retry-owner")
        assert loaded is not None
        self.assertEqual(loaded["request_id"], "second-attempt")
        self.assertEqual(loaded["progress"]["kind"], "waiting")
        self.assertEqual(len(moves.read_moves()), 2)

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
              patch.object(account_reader, "account_of", return_value=Account("claude", "target@example.com", "claude3")),
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
                        self.assertIn("restart-owner is already running on claude3", output.getvalue())
                relaunch.assert_not_called()
                self.assertEqual(record.session_records(LOGIN), [])

    def test_cancel_finishes_a_session_relaunched_by_hand_on_its_target(self) -> None:
        saved = self.hand_restart_record("cancel-owner")
        live = self.running_on(saved, self.root / ".claude3")
        output = io.StringIO()
        with (patch.object(sessions, "live_sessions", return_value=live),
              patch.object(settle, "run_remote", return_value=(0, "")), redirect_stdout(output)):
            _ = settle.cancel(self.account, True, only=[saved["session"]["session_id"]])
        self.assertIn("cancel-owner is already running on claude3; its shutdown is finished", output.getvalue())
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
        push = cast(RenderedPush, alert.call_args.args[0])
        self.assertEqual(push.id, "shutdown.restart_failed")
        self.assertEqual(push.values["stopped"], "One session")

    def test_legacy_default_record_restarts_on_the_numbered_home_without_rewriting_it_on_read(self) -> None:
        saved = entry(top_level("legacy-home-move", host=tmux_host("legacy-home-move")))
        saved["target"] = "claude2"
        self.create([saved])
        path = next((self.state_root / LOGIN / "sessions").glob("*.json"))
        original = path.read_bytes().replace(b'"target": "claude2"', b'"target": "default"')
        self.assertIn(b'"target": "default"', original)
        _ = path.write_bytes(original)

        loaded = record.session_records(LOGIN)[0]

        self.assertEqual(loaded.get("target"), "claude2")
        self.assertEqual(path.read_bytes(), original)
        recorder = CommandRecorder()
        with patch.object(restart, "_run", recorder):
            result = restart._launch_session(loaded, "resume the saved checkpoint", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(result, restart.SessionLaunched)
        commands = [argv[-1] for argv in recorder.calls if "-ic" in argv]
        self.assertEqual(len(commands), 1)
        self.assertIn("accounts.py exec claude2", commands[0])
        self.assertNotIn("CLAUDE_CONFIG_DIR=", commands[0])

    def test_resume_parts_for_both_groups_on_the_home_account_use_its_number(self) -> None:
        self.configure_groups(units="claude2", showrunners="claude2")
        for owner in (unit("home-unit"), showrunner("home-runner")):
            with self.subTest(kind=owner["kind"]):
                owner["config_dir"] = str(self.root / ".claude3")
                parts = restart._resume_parts(owner, "continue the checkpoint")  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(parts.account, restart.MoveResumeAccount("claude2"))

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
        saved["target"] = "claude3"
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
            self.configure_groups(showrunners="claude3")
        elif session["kind"] == "unit":
            self.configure_groups(units="claude3")
        saved = entry(session)
        saved["target"] = "claude3"
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
        self.assertEqual(arguments[start + 1:start + 3], ["exec", "claude3"])
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
        for target, directory in (("claude3", self.root / ".claude3"), ("claude2", self.root / ".claude")):
            with self.subTest(target=target):
                owner = top_level("moved-session", host=recorded_tmux_host("kept", "%move", 420))
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
        self.assertEqual(launch[launch.index("--account") + 1], "claude3")
        self.assertEqual(launch[launch.index("--resume") + 1], "move-unit")
        self.assertEqual(launch[launch.index("--session-name") + 1], "move-unit")

    def test_ordinary_unit_restart_defers_to_configured_units_instead_of_recorded_account(self) -> None:
        """F005: an untargeted stopped unit carries no obsolete account override."""
        self.configure_groups(units="claude3")
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
        self.assertEqual(argv[argv.index("--account") + 1], "claude3")

    def test_unit_restart_forwards_the_numbered_home_account(self) -> None:
        self.configure_groups(units="claude2")
        with patch.object(accounts, "account_for_group_launch", wraps=accounts.account_for_group_launch) as check:
            argv = restart._unit_argv(entry(unit("alias-unit")))  # pyright: ignore[reportPrivateUsage]
        check.assert_called_once_with("unit", resume=True, target="")
        self.assertEqual(argv[argv.index("--account") + 1], "claude2")

    def test_unit_restart_migrates_legacy_default_group_to_its_number(self) -> None:
        self.configure_groups(units="default")
        argv = restart._unit_argv(entry(unit("default-unit")))  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(argv[argv.index("--account") + 1], "claude2")

    def test_group_resume_parts_migrate_legacy_default_groups_to_their_number(self) -> None:
        self.configure_groups(units="default", showrunners="default")
        for owner in (unit("default-unit"), showrunner("default-runner")):
            with self.subTest(kind=owner["kind"]):
                parts = restart._resume_parts(owner, "restart")  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(parts.account, restart.MoveResumeAccount("claude2"))

    def test_group_restarts_still_refuse_an_explicit_target_outside_the_configured_group(self) -> None:
        self.configure_groups(units="claude2", showrunners="claude2")
        saved = entry(unit("alias-unit"))
        saved["target"] = "claude3"
        with self.assertRaisesRegex(accounts.AccountsError, "must name claude2"):
            _ = restart._unit_argv(saved)  # pyright: ignore[reportPrivateUsage]
        for owner in (unit("alias-unit"), showrunner("alias-runner")):
            with self.subTest(kind=owner["kind"]), self.assertRaisesRegex(
                accounts.AccountsError, "must name claude2"
            ):
                _ = restart._resume_parts(owner, "restart", "claude3")  # pyright: ignore[reportPrivateUsage]

    def test_stopped_showrunner_restart_uses_prepared_configured_showrunners_account(self) -> None:
        """F005: showrunner restarts use their group's selection instead of the stop directory."""
        self.configure_groups(showrunners="claude3")
        owner = showrunner("stopped-runner")
        owner["config_dir"] = str(self.root / ".claude2")
        saved = entry(owner)
        commands = CommandRecorder()
        with (patch.object(accounts, "prepared_account", wraps=accounts.prepared_account) as prepare,
              patch.object(restart, "_run", commands), patch.object(restart, "_retire"),
              patch.object(restart, "_live_session_ids", return_value=frozenset()),
              patch.object(restart, "_herdr_launch", wraps=restart._herdr_launch) as launch,  # pyright: ignore[reportPrivateUsage]
              redirect_stdout(io.StringIO())):
            _ = restart.relaunch_session(saved, dry_run=True, desktop_state=self.desktop_state)
        prepare.assert_any_call("claude3", resume=True)
        launch.assert_called_once()
        parts = cast(restart.ResumeParts, launch.call_args.args[0])
        self.assertEqual(parts.account, restart.MoveResumeAccount("claude3"))
        self.assertEqual(parts.cwd, owner["cwd"])
        self.assertEqual(parts.claude_arguments, (
            "--resume", "stopped-runner", "-n", "stopped-runner", "--remote-control", "stopped-runner",
            "--settings", '{"disableAgentView": true}',
        ))
        self.assertEqual(parts.opening_prompt, "/showrunner:produce /tmp/demo production.md resume")
        self.assertEqual(launch.call_args.args[1], restart.NewHerdrSession("tmux-stopped-runner"))
        self.assertTrue(launch.call_args.kwargs["dry_run"])
        self.assertFalse(any("-ic" in argv for argv in commands.calls))

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

    def test_move_target_reopens_ghostty_attached_to_herdr(self) -> None:
        owner = top_level("move-ghostty", host=inventory.WindowHost(kind="ghostty", window_shell=10,
            desktop=inventory.DesktopNotInSnapshot(kind="not in snapshot")))
        with patch.object(restart, "_herdr_launch") as launch:
            commands, _ = self.moved_restart(owner)
        parts = cast(restart.ResumeParts, launch.call_args.args[0])
        self.assertEqual(parts.account, restart.MoveResumeAccount("claude3"))
        self.assertEqual(parts.claude_arguments[:2], ("--resume", "move-ghostty"))
        self.assertEqual(launch.call_args.args[1], restart.NewHerdrSession("move-ghostty"))
        command = next(argv[-1] for argv in commands if "ghostty" in argv)
        self.assertEqual(shlex.split(command.removesuffix("; exec zsh"))[-4:],
                         ["herdr", "session", "attach", "move-ghostty"])

    def test_supervised_ghostty_move_publishes_complete_atomic_account_request(self) -> None:
        owner = top_level("supervised-move", name="kept window", cwd=str(self.root / "workspace"),
            model=inventory.ModelName(kind="model", name="opus"),
            host=inventory.WindowHost(kind="ghostty", window_shell=901,
                desktop=inventory.NamedDesktop(kind="named", name="Production"),
                supervisor=inventory.SupervisorIdentity(kind="supervisor", pid=902,
                    proc_start="supervisor-start")))
        saved = entry(owner)
        saved["target"] = "claude3"
        destination = self.state_root / "relaunch/902.json"
        published: list[dict[str, object]] = []
        replace = os.replace
        def publish(source: str | Path, target: str | Path) -> None:
            if Path(target) != destination:
                replace(source, target)
                return
            self.assertEqual(Path(source).parent, destination.parent)
            self.assertNotEqual(Path(source), destination)
            self.assertFalse(destination.exists(), "reader can see only the complete request")
            value = cast(dict[str, object], json.loads(Path(source).read_text()))
            self.assertEqual(value["cwd"], owner["cwd"])
            published.append(value)
            replace(source, target)
        with (patch.object(inventory, "_process_start", return_value="supervisor-start"),
              patch.object(os, "replace", side_effect=publish),
              patch.object(restart, "_herdr_launch") as launch,
              patch.object(restart, "_run", side_effect=AssertionError("in-place restart launches a window"))):
            launched = restart._launch_session(saved, "resume this work", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(launched, restart.SessionLaunched)
        self.assertEqual(len(published), 1)
        request = cast(dict[str, object], json.loads(destination.read_text()))
        self.assertEqual(request, published[0])
        argv = cast(list[str], request["argv"])
        self.assertEqual(argv, ["herdr", "session", "attach", "kept-window"])
        parts = cast(restart.ResumeParts, launch.call_args.args[0])
        self.assertEqual(parts.account, restart.MoveResumeAccount("claude3"))
        self.assertEqual(parts.claude_arguments[parts.claude_arguments.index("--model") + 1], "opus")
        self.assertEqual(parts.opening_prompt, "resume this work")
        self.assertEqual(request["environment"], {})
        self.assertEqual(request["unset"], [])
        self.assertEqual(request["note"], "resume this work")
        timestamp = datetime.fromisoformat(cast(str, request["written_at"]))
        self.assertEqual(timestamp.utcoffset(), datetime.fromisoformat(STOPPED).utcoffset())
        self.assertEqual(timestamp.microsecond, 0)
        self.assertEqual(list(destination.parent.iterdir()), [destination])

    def test_mac_supervised_terminal_restart_requests_in_place_and_lost_supervisor_opens_window(self) -> None:
        owner = top_level("mac-supervised", host=inventory.TerminalHost(
            kind="terminal", supervisor=inventory.SupervisorIdentity(
                kind="supervisor", pid=902, proc_start="supervisor-start")))
        destination = self.state_root / "relaunch/902.json"
        with (patch.object(sys, "platform", "darwin"),
              patch.object(inventory, "_process_start", return_value="supervisor-start"),
              patch.object(restart, "_run", side_effect=AssertionError("in-place restart opens a window"))):
            launched = restart._launch_session(entry(owner), "resume on Mac", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(launched, restart.SessionLaunched)
        request = cast(dict[str, object], json.loads(destination.read_text()))
        argv = cast(list[str], request["argv"])
        self.assertEqual(argv[argv.index("--resume") + 1], owner["session_id"])
        self.assertEqual(argv[-1], "resume on Mac")
        self.assertEqual(self.desktop_state.started_windows, 0)
        destination.unlink()
        commands = CommandRecorder()
        with (patch.object(sys, "platform", "darwin"),
              patch.object(inventory, "_process_start", return_value="reused-pid"),
              patch.object(restart, "_run", commands)):
            launched = restart._launch_session(entry(owner), "resume on Mac", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(launched, restart.SessionLaunched)
        self.assertEqual(commands.calls[0][:3], ["open", "-na", "/Applications/Nix Apps/Ghostty.app"])
        self.assertEqual(self.desktop_state.started_windows, 1)
        self.assertFalse(destination.exists())

    def test_missing_or_reused_supervisor_opens_window_on_recorded_desktop(self) -> None:
        for actual_start in ("", "reused-pid"):
            with self.subTest(actual_start=actual_start):
                state = restart.DesktopLaunchState(starting_desktop="2")
                owner = top_level("lost-supervisor", host=inventory.WindowHost(kind="ghostty", window_shell=901,
                    desktop=inventory.NamedDesktop(kind="named", name="Production"),
                    supervisor=inventory.SupervisorIdentity(kind="supervisor", pid=902,
                        proc_start="supervisor-start")))
                commands = CommandRecorder()
                with (patch.object(inventory, "_process_start", return_value=actual_start),
                      patch.object(restart, "_desktop_numbers", return_value={"Production": 4}),
                      patch.object(restart, "_run", commands),
                      patch.object(restart, "_herdr_launch")):
                    launched = restart._launch_session(entry(owner), "resume", state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
                    state.restore(dry_run=False)
                self.assertIsInstance(launched, restart.SessionLaunched)
                self.assertEqual(commands.calls[0], ["kdotool", "set_desktop", "4"])
                self.assertIn("ghostty", commands.calls[1])
                self.assertEqual(commands.calls[-1], ["kdotool", "set_desktop", "2"])
                self.assertFalse((self.state_root / "relaunch/902.json").exists())

    def test_supervised_attach_request_keeps_account_variables_in_the_agent_launch(self) -> None:
        owner = top_level("default-supervisor", host=inventory.WindowHost(kind="ghostty", window_shell=901,
            desktop=inventory.DesktopNotInSnapshot(kind="not in snapshot"),
            supervisor=inventory.SupervisorIdentity(kind="supervisor", pid=902,
                proc_start="supervisor-start")))
        with (patch.object(inventory, "_process_start", return_value="supervisor-start"),
              patch.object(restart, "_herdr_launch") as launch,
              patch.object(restart, "_run", side_effect=AssertionError("in-place restart launches a window"))):
            _ = restart._launch_session(entry(owner), "resume default", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        request = cast(relaunch.RelaunchRequest, json.loads((self.state_root / "relaunch/902.json").read_text()))
        self.assertEqual(request["argv"], ["herdr", "session", "attach", "default-supervisor"])
        parts = cast(restart.ResumeParts, launch.call_args.args[0])
        self.assertEqual(parts.account, restart.RetainResumeAccount(self.root / ".claude"))
        self.assertNotIn("CLAUDE_CONFIG_DIR", parts.environment)

    def test_supervised_restart_preview_creates_no_request_or_window(self) -> None:
        owner = top_level("preview-supervisor", host=inventory.WindowHost(kind="ghostty", window_shell=901,
            desktop=inventory.DesktopNotInSnapshot(kind="not in snapshot"),
            supervisor=inventory.SupervisorIdentity(kind="supervisor", pid=902,
                proc_start="supervisor-start")))
        output = io.StringIO()
        with (patch.object(inventory, "_process_start", return_value="supervisor-start"),
              patch.object(restart, "_run", side_effect=AssertionError("preview launches a window")),
              redirect_stdout(output)):
            _ = restart._launch_session(entry(owner), "preview resume", self.desktop_state, dry_run=True)  # pyright: ignore[reportPrivateUsage]
        self.assertFalse((self.state_root / "relaunch").exists())
        self.assertIn("preview-supervisor", output.getvalue())
        self.assertIn("902", output.getvalue())

    def test_new_ghostty_window_runs_herdr_attach_through_supervisor(self) -> None:
        owner = top_level("new-supervisor", name="quoted window", cwd="/tmp/quoted workspace",
            host=inventory.WindowHost(kind="ghostty", window_shell=901,
                desktop=inventory.DesktopNotInSnapshot(kind="not in snapshot")))
        commands, _ = self.moved_restart(owner)
        launch = next(argv for argv in commands if "ghostty" in argv)
        arguments = shlex.split(launch[-1].removesuffix("; exec zsh"))
        runner = str(Path(__file__).parents[1] / "lib/relaunch.py")
        index = arguments.index(runner)
        self.assertEqual(arguments[index - 1], str(Path(runner).with_name("py")))
        self.assertEqual(arguments[index:], [runner, "run", "--", "herdr", "session", "attach", "quoted-window"])
        self.assertEqual(arguments[:3], ["cd", owner["cwd"], "&&"])
        self.assertTrue(launch[-1].endswith("; exec zsh"))

    def test_zed_restart_always_opens_a_window_attached_to_herdr(self) -> None:
        owner = top_level("move-zed", host=inventory.WindowHost(kind="zed", window_shell=10,
            desktop=inventory.DesktopNotInSnapshot(kind="not in snapshot")))
        with (patch.object(restart, "_herdr_launch") as launch,
              patch.object(restart, "live_supervisor", side_effect=AssertionError("zed has no supervisor"))):
            commands, _ = self.moved_restart(owner)
        self.assertEqual(launch.call_args.args[1], restart.NewHerdrSession("move-zed"))
        windows = [argv for argv in commands if "ghostty" in argv]
        self.assertEqual(len(windows), 1)
        self.assertEqual(shlex.split(windows[0][-1].removesuffix("; exec zsh"))[-4:],
                         ["herdr", "session", "attach", "move-zed"])

    def test_linux_terminal_move_requires_manual_resume_and_journals_failure(self) -> None:
        owner = top_level("linux-terminal", host=inventory.TerminalHost(kind="terminal"))
        saved = entry(owner)
        saved["target"] = "claude3"
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
        with patch.object(restart, "_herdr_launch") as herdr_launch:
            commands, _ = self.moved_restart(owner)
        herdr_launch.assert_not_called()
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
            host=inventory.HerdrHost(kind="herdr", herdr_session="writing", pane_id="w1:p1", presentation="detached"))
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
        _ = cast(object, self.enterContext(patch.object(herdr, "server_environment", return_value={})))
        _ = cast(object, self.enterContext(patch.object(herdr.Herdr, "pane_activity", return_value=herdr.PaneAtShell())))
        return owner, responses, log

    def write_herdr_responses(self, path: Path, agents: list[herdr.AgentOccupant],
                              *, not_ready: bool = False, pane_present: bool = False) -> None:
        responses: dict[str, CommandResponse | list[CommandResponse]] = {
            "--session writing status server": {"stdout": "status: running\n", "code": 0},
            "--session writing agent list": {"stdout": json.dumps({"result": {"agents": agents}}), "code": 0},
            "--session writing pane list": {"stdout": json.dumps({"result": {
                "panes": [{"pane_id": "w1:p1"}] if pane_present else [],
            }}), "code": 0},
            "--session writing workspace list": {"stdout": json.dumps({"result": {"workspaces": []}}), "code": 0},
            "default": {"stdout": json.dumps({"result": {"root_pane": {"pane_id": "w2:p1"}}}), "code": 0},
        }
        if not_ready:
            start_argv = ["--session", "writing", "agent", "start", "kept-name", "--kind", "claude",
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

    def allow_new_herdr_server(self, responses: Path, name: str) -> None:
        replies = cast(dict[str, object], json.loads(responses.read_text()))
        replies[f"--session {name} status server"] = [
            {"stdout": "status: stopped\n", "code": 0},
            {"stdout": "status: running\n", "code": 0},
        ]
        replies[f"--session {name} agent list"] = {"stdout": json.dumps({"result": {"agents": []}}), "code": 0}
        replies[f"--session {name} workspace list"] = {"stdout": json.dumps({"result": {"workspaces": []}}), "code": 0}
        _ = responses.write_text(json.dumps(replies))

    def codex_recovery_fixture(self, *, picker: bool = False, attached: bool = False
                               ) -> tuple[record.RecoverySnapshot, record.SnapshotCodexSession, Path, Path]:
        owner, responses, log = self.herdr_restart_fixture()
        owner["name"], owner["session_id"] = "Codex writing", "codex-resume-id"
        owner["host"] = inventory.HerdrHost(kind="herdr", herdr_session="old-code",
            pane_id="w1:p1", presentation="attached window" if attached else "detached")
        home = self.root / "codex-home"
        home.mkdir()
        saved_entry = record.SnapshotCodexSession(agent="codex", session=owner,
            codex=record.CodexLaunch(home=str(home), resume=record.CodexResumePicker(kind="picker") if picker
                else record.CodexSessionId(kind="session id", session_id="codex-resume-id")),
            workers=[], timers=[], last_seen=STOPPED, state="unresolved", crash="host gone")
        snapshot = record.RecoverySnapshot(machine="natedev", login="codex-codex-home", label="codex-codex-home",
            taken_at=STOPPED, sessions=[saved_entry])
        record.write_snapshot(snapshot)
        self.allow_new_herdr_server(responses, "trusted-folder-codex-01f232c067f3d73a")
        replies = cast(dict[str, object], json.loads(responses.read_text()))
        replies["--session trusted-folder-codex-01f232c067f3d73a pane list"] = {"stdout": json.dumps({"result": {"panes": [
            {"pane_id": "w2:p1", "label": "trusted-folder-codex-01f232c067f3d73a"},
        ]}}), "code": 0}
        _ = responses.write_text(json.dumps(replies))
        proc = self.root / "codex-proc"
        proc.mkdir()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HERDR_PROC": str(proc)})))
        return snapshot, saved_entry, responses, log

    def test_codex_snapshot_resumes_id_with_recorded_home_and_no_prompt_or_claude_record(self) -> None:
        snapshot, saved, _responses, log = self.codex_recovery_fixture()
        with patch.object(restart, "_open_window") as window, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(restart.up(snapshot["login"]), 0, output.getvalue())
        calls = self.herdr_calls(log)
        start = next(row for row in calls if row[2:4] == ["agent", "start"])
        self.assertEqual(start[start.index("--kind") + 1], "codex")
        self.assertEqual(start[start.index("--") + 1:], ["resume", "codex-resume-id"])
        opened = next(row for row in calls if row[2:4] == ["workspace", "create"])
        self.assertIn("CODEX_HOME=" + saved["codex"]["home"], opened)
        self.assertIn(saved["session"]["cwd"], opened)
        self.assertFalse(any(row[2:4] == ["agent", "prompt"] or "send-keys" in row for row in calls))
        window.assert_not_called()
        self.assertIn("herdr session attach trusted-folder-codex-01f232c067f3d73a", output.getvalue())
        self.assertEqual(record.session_records(snapshot["login"]), [])
        self.assertIsInstance(record.lookup_account_hold(snapshot["login"]), record.AccountOpen)
        remaining = record.read_snapshot(snapshot["login"])
        assert not isinstance(remaining, record.NoRecoverySnapshot)
        self.assertEqual(remaining["sessions"], [])

    def test_codex_snapshot_without_id_opens_picker_and_reattaches_in_one_window(self) -> None:
        snapshot, _saved, _responses, log = self.codex_recovery_fixture(picker=True, attached=True)
        with (patch.object(herdr, "attached_clients", return_value=herdr.NoClientAttached()),
              patch.object(restart, "_open_window") as window, redirect_stdout(io.StringIO()) as output):
            self.assertEqual(restart.up(snapshot["login"]), 0, output.getvalue())
        start = next(row for row in self.herdr_calls(log) if row[2:4] == ["agent", "start"])
        self.assertEqual(start[start.index("--") + 1:], ["resume"])
        self.assertIn("Codex resume picker opens", output.getvalue())
        window.assert_called_once()
        self.assertIn("herdr session attach trusted-folder-codex-01f232c067f3d73a", cast(str, cast(object, window.call_args.args[0])))

    def test_codex_snapshot_running_now_is_skipped_without_server_or_acknowledgement(self) -> None:
        snapshot, _saved, _responses, log = self.codex_recovery_fixture()
        with (patch.object(inventory, "codex_liveness", return_value=inventory.CodexLive(99001)),
              patch.object(herdr, "start_new_server") as start, redirect_stdout(io.StringIO()) as output):
            self.assertEqual(restart.up(snapshot["login"]), 0)
        start.assert_not_called()
        self.assertFalse(log.exists())
        self.assertEqual(record.read_snapshot(snapshot["login"]), snapshot)
        self.assertIn("already running (pid 99001); skipped", output.getvalue())

    def test_codex_snapshot_occupied_name_refuses_before_pane_and_keeps_recovery_entry(self) -> None:
        snapshot, _saved, responses, log = self.codex_recovery_fixture()
        replies = cast(dict[str, object], json.loads(responses.read_text()))
        replies["--session trusted-folder-codex-01f232c067f3d73a status server"] = {"stdout": "status: running\n", "code": 0}
        _ = responses.write_text(json.dumps(replies))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(restart.up(snapshot["login"]), 1)
        self.assertFalse(any(row[2:4] in (["workspace", "create"], ["agent", "start"])
                             for row in self.herdr_calls(log)))
        self.assertEqual(record.read_snapshot(snapshot["login"]), snapshot)

    def test_two_codex_sessions_in_one_folder_restore_into_different_servers(self) -> None:
        snapshot, first, responses, log = self.codex_recovery_fixture()
        second = copy.deepcopy(first)
        second["session"]["session_id"] = "second-codex-id"
        second["codex"]["resume"] = record.CodexSessionId(kind="session id", session_id="second-codex-id")
        snapshot["sessions"].append(second)
        record.write_snapshot(snapshot)
        second_name = "trusted-folder-codex-69efd00c66abd993"
        self.allow_new_herdr_server(responses, second_name)
        replies = cast(dict[str, object], json.loads(responses.read_text()))
        replies[f"--session {second_name} pane list"] = {"stdout": json.dumps({"result": {"panes": [
            {"pane_id": "w2:p1", "label": second_name},
        ]}}), "code": 0}
        _ = responses.write_text(json.dumps(replies))
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(restart.up(snapshot["login"]), 0, output.getvalue())
        starts = [row for row in self.herdr_calls(log) if row[2:4] == ["agent", "start"]]
        self.assertEqual(len(starts), 2)
        self.assertEqual(len({row[1] for row in starts}), 2)
        self.assertCountEqual([row[row.index("--") + 1:] for row in starts],
                              [["resume", "codex-resume-id"], ["resume", "second-codex-id"]])
        remaining = record.read_snapshot(snapshot["login"])
        assert not isinstance(remaining, record.NoRecoverySnapshot)
        self.assertEqual(remaining["sessions"], [])

    def test_codex_agent_start_failure_stops_only_its_new_server_and_keeps_snapshot(self) -> None:
        snapshot, saved, _responses, log = self.codex_recovery_fixture()
        with (patch.object(herdr, "start_agent", side_effect=herdr.HerdrFailure("codex refused")),
              patch.object(herdr, "stop_session") as stop, redirect_stdout(io.StringIO()) as output):
            self.assertEqual(restart.up(snapshot["login"]), 1, output.getvalue())
        stop.assert_called_once_with("trusted-folder-codex-01f232c067f3d73a")
        self.assertTrue(any(row[2:4] == ["workspace", "create"] for row in self.herdr_calls(log)))
        self.assertEqual(record.read_snapshot(snapshot["login"]), snapshot)
        self.assertIn(saved["session"]["name"], output.getvalue())

    def test_window_restart_sends_recorded_arguments_and_multiline_prompt_once_in_herdr(self) -> None:
        owner, responses, log = self.herdr_restart_fixture()
        owner["host"] = inventory.WindowHost(kind="ghostty", window_shell=10,
            desktop=inventory.NamedDesktop(kind="named", name="Production"))
        name = "kept-name"
        self.allow_new_herdr_server(responses, name)
        prompt = "Continue this work.\nKeep 'quotes', ; and $(literal) intact."
        commands = CommandRecorder()
        with (patch.object(restart, "_run", commands),
              patch.object(restart, "_desktop_numbers", return_value={"Production": 4}),
              patch.object(conversation_pause, "record_scheduled_prompts", wraps=conversation_pause.record_scheduled_prompts) as scheduled):
            result = restart._launch_session(entry(owner), prompt, self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(result, restart.SessionLaunched)
        scheduled.assert_called_once_with("resume-id", (prompt,))
        calls = self.herdr_calls(log)
        opened = next(row for row in calls if row[2:4] == ["workspace", "create"])
        self.assertEqual(opened[opened.index("--cwd") + 1], owner["cwd"])
        self.assertIn("CLAUDE_CONFIG_DIR=" + owner.get("config_dir", ""), opened)
        started = [row for row in calls if row[2:4] == ["agent", "start"]]
        self.assertEqual(len(started), 1)
        argv = started[0][started[0].index("--") + 1:]
        self.assertEqual(argv, ["--resume", "resume-id", "-n", "kept name", "--remote-control", "kept name",
            "--model", "opus", "--settings", '{"disableAgentView": true}'])
        self.assertNotIn(prompt, argv)
        self.assertEqual([row for row in calls if row[2:4] == ["agent", "prompt"]],
            [["--session", name, "agent", "prompt", "w2:p1", prompt]])
        self.assertTrue(any("--unit=herdr-kept-name.service" in row for row in calls))
        windows = [row for row in commands.calls if "ghostty" in row]
        self.assertEqual(len(windows), 1)
        self.assertEqual(shlex.split(windows[0][-1].removesuffix("; exec zsh"))[-4:],
            ["herdr", "session", "attach", name])
        self.assertEqual(commands.calls[commands.calls.index(windows[0]) - 1], ["kdotool", "set_desktop", "4"])

    def test_recorded_herdr_presentation_reopens_one_window_or_prints_attach(self) -> None:
        owner, _responses, log = self.herdr_restart_fixture()
        for presentation in ("attached window", "detached", "old record"):
            with self.subTest(presentation=presentation):
                host = inventory.HerdrHost(kind="herdr", herdr_session="writing", pane_id="w1:p1")
                if presentation != "old record":
                    host["presentation"] = presentation
                owner["host"] = host
                commands, output = CommandRecorder(), io.StringIO()
                with (patch.object(restart, "_run", commands),
                      patch.object(herdr, "attached_clients", return_value=herdr.NoClientAttached()),
                      redirect_stdout(output)):
                    _ = restart._launch_session(entry(owner), "restart", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
                windows = [row for row in commands.calls if "ghostty" in row]
                self.assertEqual(len(windows), 1 if presentation == "attached window" else 0)
                if windows:
                    self.assertEqual(shlex.split(windows[0][-1].removesuffix("; exec zsh"))[-4:],
                        ["herdr", "session", "attach", "writing"])
                else:
                    self.assertIn("herdr session attach writing", output.getvalue())
        calls = self.herdr_calls(log)
        self.assertEqual(len([row for row in calls if row[2:4] == ["agent", "start"]]), 3)

    def test_tmux_showrunner_restart_maps_dot_and_long_names_with_the_shared_rule(self) -> None:
        for old_name, expected in ((".", "-"), ("..", "-"), ("a" * 65, "a" * 64)):
            with self.subTest(old_name=old_name), patch.object(restart, "_herdr_launch") as launch:
                owner = showrunner("runner", host=tmux_host(old_name))
                _ = restart._launch_session(entry(owner), "restart", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(launch.call_args.args[1], restart.NewHerdrSession(expected))

    def test_attached_herdr_restart_reuses_clients_and_prints_attach_when_unreadable(self) -> None:
        owner = top_level("attached", host=inventory.HerdrHost(kind="herdr",
            herdr_session="writing", pane_id="w1:p1", presentation="attached window"))
        for clients in (herdr.ClientsAttached(1), herdr.ClientsUnreadable("proc unreadable")):
            with (self.subTest(clients=clients),
                  patch.object(restart, "_herdr_launch") as launch,
                  patch.object(herdr, "attached_clients", return_value=clients),
                  patch.object(restart, "_open_window") as window,
                  redirect_stdout(io.StringIO()) as output):
                _ = restart._launch_session(entry(owner), "restart", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
            launch.assert_called_once()
            window.assert_not_called()
            if isinstance(clients, herdr.ClientsUnreadable):
                self.assertIn("herdr session attach writing", output.getvalue())

    def test_window_failure_records_a_restart_issue_without_relaunching_the_live_session(self) -> None:
        hosts: tuple[inventory.SessionHost, ...] = (
            inventory.WindowHost(kind="ghostty", window_shell=10,
                desktop=inventory.DesktopNotInSnapshot(kind="not in snapshot")),
            inventory.HerdrHost(kind="herdr", herdr_session="writing", pane_id="w1:p1",
                presentation="attached window"),
        )
        for host in hosts:
            with self.subTest(host=host["kind"]):
                owner = top_level("window-failure-" + host["kind"], name="failed-window", host=host)
                self.create([entry(owner)])
                with (patch.object(restart, "_herdr_launch") as launch,
                      patch.object(herdr, "attached_clients", return_value=herdr.NoClientAttached()),
                      patch.object(restart, "live_supervisor", return_value=inventory.NoSupervisor(kind="no supervisor")),
                      patch.object(restart, "_open_window", side_effect=OSError("Ghostty unavailable")),
                      patch.object(restart, "_retire"),
                      patch.object(restart, "_live_session_ids", return_value=frozenset()),
                      patch.object(restart, "_session_liveness", return_value=restart.SessionLive()),
                      patch.object(restart, "_prove_move", return_value=True),
                      patch.object(restart, "wait_for_confirmation", side_effect=self.all_sessions_live),
                      redirect_stdout(io.StringIO()) as output):
                    self.assertEqual(restart.up(LOGIN), 0)
                launch.assert_called_once()
                history = list((self.state_root / LOGIN / "history").glob("*" + owner["session_id"] + ".json"))
                self.assertEqual(len(history), 1)
                saved = record.parse_session_records("[" + history[0].read_text() + "]")[0]
                self.assertEqual(saved["progress"]["kind"], "restarted")
                self.assertTrue(any("Ghostty unavailable" in issue for issue in saved.get("restart_issues", [])))
                name = "writing" if host["kind"] == "herdr" else "failed-window"
                self.assertIn(f"herdr session attach {name}", output.getvalue())
                self.assertTrue(any(f"herdr session attach {name}" in issue
                                    for issue in saved.get("restart_issues", [])))

    def test_herdr_restart_passes_only_claude_arguments_and_prompts_once(self) -> None:
        owner, _, log = self.herdr_restart_fixture()
        prompt = "Continue here.\nKeep 'quotes', ; and $(literal) intact."
        with (patch.object(restart, "_run") as other_host,
              patch.object(herdr, "server_environment", return_value={"PATH": "/usr/bin"}) as server_environment):
            launch = restart._launch_session(entry(owner), prompt, self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(launch, restart.SessionLaunched)
        other_host.assert_not_called()
        server_environment.assert_called_once_with("writing")
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
        }], pane_present=True)
        with self.assertRaisesRegex(OSError, "pane still holds a live agent"):
            _ = restart._launch_session(entry(owner), "restart", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        calls = self.herdr_calls(log)
        self.assertFalse(any(row[2:4] in (["workspace", "create"], ["agent", "start"],
                                        ["agent", "prompt"], ["pane", "close"]) for row in calls))

    def test_herdr_restart_replaces_a_pane_at_its_shell_with_no_live_agent(self) -> None:
        owner, responses, log = self.herdr_restart_fixture()
        occupants: list[list[herdr.AgentOccupant]] = [[], [
            {"pane_id": "w1:p1", "agent": None, "agent_status": "none"},
        ], *[[{"pane_id": "w1:p1", "agent": "claude", "agent_status": state}]
             for state in ("exited", "stopped", "dead")]]
        for agents in occupants:
            with self.subTest(agents=agents):
                log.unlink(missing_ok=True)
                self.write_herdr_responses(responses, agents, pane_present=True)
                restart._herdr_launch(restart._resume_parts(owner, "restart"), restart.RecordedHerdrPane("writing", "w1:p1"), owner, dry_run=False)  # pyright: ignore[reportPrivateUsage]
                calls = self.herdr_calls(log)
                close = ["--session", "writing", "pane", "close", "w1:p1"]
                opened = next(row for row in calls if row[2:4] == ["workspace", "create"])
                self.assertEqual([row for row in calls if row[2:4] == ["pane", "close"]], [close])
                self.assertLess(calls.index(close), calls.index(opened))

    def test_herdr_restart_refuses_running_commands_in_a_recorded_pane(self) -> None:
        owner, responses, log = self.herdr_restart_fixture()
        self.write_herdr_responses(responses, [], pane_present=True)
        with (patch.object(herdr.Herdr, "pane_activity",
                           return_value=herdr.PaneRunning(("python build.py", "sleep 900"))) as activity,
              self.assertRaisesRegex(OSError, "writing:w1:p1.*python build.py.*sleep 900")):
            _ = restart._launch_session(entry(owner), "restart", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        activity.assert_called_once_with("w1:p1")
        self.assertFalse(any(row[2:4] in (["pane", "close"], ["workspace", "create"],
                                        ["agent", "start"], ["agent", "prompt"])
                             for row in self.herdr_calls(log)))

    def test_herdr_restart_refuses_variables_inherited_from_the_server(self) -> None:
        from scripts.accounts import accounts

        owner, responses, log = self.herdr_restart_fixture()
        _ = (self.root / ".claude.json").write_text(json.dumps({
            "projects": {owner["cwd"]: {"hasTrustDialogAccepted": True}},
        }))
        for variable in ("REMOVE_ME", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN"):
            for fresh in (False, True):
                with self.subTest(variable=variable, fresh=fresh):
                    log.unlink(missing_ok=True)
                    self.write_herdr_responses(responses, [])
                    if fresh:
                        replies = cast(dict[str, object], json.loads(responses.read_text()))
                        replies["--session writing status server"] = [
                            {"stdout": "status: stopped\n", "code": 0},
                            {"stdout": "status: running\n", "code": 0},
                        ]
                        _ = responses.write_text(json.dumps(replies))
                    saved = entry(owner)
                    saved["session"]["config_dir"] = str(self.root / ".claude")
                    if variable == "REMOVE_ME":
                        saved["target"] = "claude3"
                    prepared = accounts.PreparedAccount({}, frozenset({variable}))
                    with (patch.object(accounts, "prepared_account", return_value=prepared),
                          patch.object(herdr, "server_environment", return_value={variable: ""}),
                          self.assertRaises(OSError) as error):
                        _ = restart._launch_session(saved, "restart", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
                    self.assertEqual(str(error.exception),
                        f"herdr server writing carries {variable} from where it started; "
                        + "stop it with herdr --session writing server stop and restart again")
                    self.assertFalse(any(row[2:4] in (["workspace", "create"], ["pane", "close"],
                                                     ["agent", "start"], ["agent", "prompt"])
                                         for row in self.herdr_calls(log)))

    def test_herdr_restart_refuses_an_unreadable_server_environment(self) -> None:
        owner, _, log = self.herdr_restart_fixture()
        with (patch.object(herdr, "server_environment", side_effect=herdr.HerdrFailure("cannot read server environment")),
              self.assertRaisesRegex(OSError, "cannot read server environment")):
            _ = restart._launch_session(entry(owner), "restart", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        self.assertFalse(any(row[2:4] in (["workspace", "create"], ["pane", "close"],
                                        ["agent", "start"], ["agent", "prompt"])
                             for row in self.herdr_calls(log)))

    def test_herdr_main_account_launches_in_a_clean_server_without_named_variables(self) -> None:
        owner, _, log = self.herdr_restart_fixture()
        owner["config_dir"] = str(self.root / ".claude")
        _ = (self.root / ".claude.json").write_text(json.dumps({
            "projects": {owner["cwd"]: {"hasTrustDialogAccepted": True}},
        }))
        _ = restart._launch_session(entry(owner), "restart", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        calls = self.herdr_calls(log)
        opened = next(row for row in calls if row[2:4] == ["workspace", "create"])
        self.assertFalse(any(part.startswith(("CLAUDE_CONFIG_DIR=", "CLAUDE_CODE_REMOTE_MEMORY_DIR=",
                                              "CLAUDE_CODE_OAUTH_TOKEN=")) for part in opened))
        self.assertEqual([row[-1] for row in calls if row[2:4] == ["agent", "prompt"]], ["restart"])

    def test_herdr_restart_opens_a_new_pane_when_the_recorded_pane_is_missing(self) -> None:
        owner, _, log = self.herdr_restart_fixture()
        restart._herdr_launch(restart._resume_parts(owner, "restart"), restart.RecordedHerdrPane("writing", "w1:p1"), owner, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        calls = self.herdr_calls(log)
        self.assertFalse(any(row[2:4] == ["pane", "close"] for row in calls))
        self.assertEqual(len([row for row in calls if row[2:4] == ["workspace", "create"]]), 1)

    def test_herdr_restart_does_not_open_a_replacement_when_the_stale_pane_will_not_close(self) -> None:
        owner, responses, log = self.herdr_restart_fixture()
        self.write_herdr_responses(responses, [], pane_present=True)
        replies = cast(dict[str, CommandResponse | list[CommandResponse]], json.loads(responses.read_text()))
        replies["--session writing pane close w1:p1"] = {
            "stdout": "", "code": 1, "stderr": "fixture pane close refused",
        }
        _ = responses.write_text(json.dumps(replies))
        with self.assertRaisesRegex(OSError, "fixture pane close refused"):
            restart._herdr_launch(restart._resume_parts(owner, "restart"), restart.RecordedHerdrPane("writing", "w1:p1"), owner, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        calls = self.herdr_calls(log)
        self.assertEqual(calls[-1], ["--session", "writing", "pane", "close", "w1:p1"])
        self.assertFalse(any(row[2:4] == ["workspace", "create"] for row in calls))

    def test_herdr_restart_starts_a_stopped_server_under_its_named_unit(self) -> None:
        owner, responses, log = self.herdr_restart_fixture()
        replies = cast(dict[str, CommandResponse | list[CommandResponse]], json.loads(responses.read_text()))
        replies["--session writing status server"] = [
            {"stdout": "status: stopped\n", "code": 0},
            {"stdout": "status: running\n", "code": 0},
        ]
        replies["--session writing workspace list"] = {
            "stdout": json.dumps({"result": {"workspaces": [{"workspace_id": "w1"}]}}), "code": 0,
        }
        _ = responses.write_text(json.dumps(replies))
        restart._herdr_launch(restart._resume_parts(owner, "restart"), restart.RecordedHerdrPane("writing", "w1:p1"), owner, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        calls = self.herdr_calls(log)
        self.assertEqual([row for row in calls if "--unit=herdr-writing.service" in row], [[
            "--user", "--unit=herdr-writing.service", "--collect", str(self.root / "fake-herdr"),
            "--session", "writing", "server",
        ]])
        close = ["--session", "writing", "workspace", "close", "w1"]
        opened = next(row for row in calls if row[2:4] == ["workspace", "create"])
        self.assertLess(calls.index(close), calls.index(opened))
        self.assertFalse(any(row[2:4] == ["pane", "close"] for row in calls))

    def test_herdr_restart_not_ready_reports_the_screen_without_sending_input(self) -> None:
        owner, responses, log = self.herdr_restart_fixture()
        self.write_herdr_responses(responses, [], not_ready=True)
        with self.assertRaisesRegex(OSError, "Do you trust this folder"):
            _ = restart._launch_session(entry(owner), "restart", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        calls = self.herdr_calls(log)
        self.assertIn(["--session", "writing", "pane", "read", "w2:p1", "--source", "visible"], calls)
        self.assertFalse(any("send-keys" in row or "send-text" in row or "prompt" in row for row in calls))

    def test_herdr_new_session_starts_server_pane_and_agent(self) -> None:
        owner, responses, log = self.herdr_restart_fixture()
        replies = cast(dict[str, object], json.loads(responses.read_text()))
        replies["--session writing status server"] = [
            {"stdout": "status: stopped\n", "code": 0},
            {"stdout": "status: running\n", "code": 0},
        ]
        _ = responses.write_text(json.dumps(replies))
        restart._herdr_launch(restart._resume_parts(owner, "restart"),  # pyright: ignore[reportPrivateUsage]
                              restart.NewHerdrSession("writing"), owner, dry_run=False)
        calls = self.herdr_calls(log)
        self.assertTrue(any("--unit=herdr-writing.service" in row for row in calls))
        self.assertEqual(len([row for row in calls if row[2:4] == ["workspace", "create"]]), 1)
        self.assertEqual(len([row for row in calls if row[2:4] == ["agent", "start"]]), 1)
        self.assertEqual([row[-1] for row in calls if row[2:4] == ["agent", "prompt"]], ["restart"])

    def test_window_restart_refuses_an_occupied_name_without_opening_a_pane_or_window(self) -> None:
        owner, _responses, log = self.herdr_restart_fixture()
        owner["host"] = inventory.WindowHost(kind="ghostty", window_shell=10,
            desktop=inventory.DesktopNotInSnapshot(kind="not in snapshot"))
        owner["name"] = "writing"
        with (patch.object(restart, "_run") as window,
              self.assertRaisesRegex(OSError, "writing")):
            _ = restart._launch_session(entry(owner), "restart", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        window.assert_not_called()
        self.assertEqual(self.herdr_calls(log), [["--session", "writing", "status", "server"]])

    def test_tmux_showrunner_migrates_to_herdr_without_changing_its_recorded_host(self) -> None:
        fixture, responses, log = self.herdr_restart_fixture()
        self.allow_new_herdr_server(responses, "writing")
        config = self.root / ".claude.json"
        _ = config.write_text(json.dumps({"oauthAccount": {"emailAddress": LOGIN},
            "projects": {fixture["cwd"]: {"hasTrustDialogAccepted": True}}}))
        owner = showrunner("resume-id", host=recorded_tmux_host("writing", "%old", 420))
        owner["cwd"] = fixture["cwd"]
        owner["config_dir"] = fixture.get("config_dir", "")
        saved = entry(owner)
        self.create([saved])
        with patch.object(restart, "_run") as tmux:
            result = restart._launch_session(saved, "restart", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(result, restart.SessionLaunched)
        tmux.assert_not_called()
        self.assertEqual(self.stored_record()["session"]["host"], owner["host"])
        calls = self.herdr_calls(log)
        self.assertEqual([row[-1] for row in calls if row[2:4] == ["agent", "prompt"]],
                         ["/showrunner:produce /tmp/demo production.md resume"])
        self.assertTrue(any(row[2:4] == ["workspace", "create"] for row in calls))

    def test_tmux_showrunner_replaces_each_invalid_session_name_character(self) -> None:
        owner = showrunner("runner", host=tmux_host("build /[x]:é"))
        with patch.object(restart, "_herdr_launch") as launch:
            _ = restart._launch_session(entry(owner), "restart", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(launch.call_args.args[1], restart.NewHerdrSession("build---x---"))
        self.assertEqual(owner["host"].get("tmux_session"), "build /[x]:é")

    def test_herdr_account_move_sets_prepared_variables_and_omits_removed_names(self) -> None:
        from scripts.accounts import accounts

        owner, _, log = self.herdr_restart_fixture()
        target = self.root / "target account"
        target.mkdir()
        _ = (target / ".claude.json").write_text(json.dumps({
            "projects": {owner["cwd"]: {"hasTrustDialogAccepted": True}},
        }))
        saved = entry(owner)
        saved["target"] = "claude3"
        prepared = accounts.PreparedAccount(
            {"CLAUDE_CONFIG_DIR": str(target), "CLAUDE_CODE_REMOTE_MEMORY_DIR": "shared-memory",
             "CLAUDE_CODE_OAUTH_TOKEN": "removed-fixture"},
            frozenset({"CLAUDE_CODE_OAUTH_TOKEN", "REMOVE_ME"}),
        )
        with (patch.object(accounts, "prepared_account", return_value=prepared) as prepare,
              patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "caller-fixture", "REMOVE_ME": "caller"}),
              patch.object(restart, "_resume_shell", side_effect=AssertionError("shell wrapper"))):
            _ = restart._launch_session(saved, "account move", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        prepare.assert_called_once_with("claude3", resume=True)
        calls = self.herdr_calls(log)
        opened = next(row for row in calls if row[2:4] == ["workspace", "create"])
        self.assertEqual(opened[opened.index("--cwd") + 1], owner["cwd"])
        self.assertIn("CLAUDE_CONFIG_DIR=" + str(target), opened)
        self.assertIn("CLAUDE_CODE_REMOTE_MEMORY_DIR=shared-memory", opened)
        self.assertFalse(any("CLAUDE_CODE_OAUTH_TOKEN=" in part or "REMOVE_ME=" in part for row in calls for part in row))
        started = next(row for row in calls if row[2:4] == ["agent", "start"])
        self.assertEqual(started[started.index("--") + 1:], [
            "--resume", "resume-id", "-n", "kept name", "--remote-control", "kept name",
            "--model", "opus", "--settings", '{"disableAgentView": true}',
        ])
        self.assertEqual([row[-1] for row in calls if row[2:4] == ["agent", "prompt"]], ["account move"])

    def test_herdr_numbered_home_account_omits_named_variables_and_checks_home_trust(self) -> None:
        from scripts.accounts import accounts

        owner, _, log = self.herdr_restart_fixture()
        _ = (self.root / ".claude.json").write_text(json.dumps({
            "projects": {owner["cwd"]: {"hasTrustDialogAccepted": True}},
        }))
        parts = restart._resume_parts(owner, "account move", "claude2")  # pyright: ignore[reportPrivateUsage]
        parts = replace(parts, environment={**parts.environment,
            "CLAUDE_CONFIG_DIR": "old-account", "CLAUDE_CODE_REMOTE_MEMORY_DIR": "old-memory"})
        prepared = accounts.PreparedAccount({}, frozenset({
            "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_REMOTE_MEMORY_DIR",
        }))
        with patch.object(accounts, "prepared_account", return_value=prepared) as prepare:
            restart._herdr_launch(parts, restart.RecordedHerdrPane("writing", "w1:p1"),  # pyright: ignore[reportPrivateUsage]
                                  owner, dry_run=False)
        prepare.assert_called_once_with("claude2", resume=True)
        opened = next(row for row in self.herdr_calls(log) if row[2:4] == ["workspace", "create"])
        self.assertIn("ENABLE_TOOL_SEARCH=true", opened)
        self.assertFalse(any(part.startswith(("CLAUDE_CONFIG_DIR=", "CLAUDE_CODE_REMOTE_MEMORY_DIR=")) for part in opened))

    def test_herdr_account_refusal_precedes_all_host_calls_and_prompt_recording(self) -> None:
        from scripts.accounts import accounts

        owner, _, log = self.herdr_restart_fixture()
        saved = entry(owner)
        saved["target"] = "claude3"
        for reason in ("target account is not set up", "target account is logged out"):
            with self.subTest(reason=reason), patch.object(accounts, "prepared_account", return_value=accounts.AccountRefused(reason)):
                with self.assertRaisesRegex(OSError, reason):
                    _ = restart._launch_session(saved, "account move", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
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

    def test_herdr_unit_with_a_plan_restarts_through_add_unit_in_its_recorded_session(self) -> None:
        self.configure_groups(units="claude3")
        owner = unit("native", session_host="herdr", session_name="native session")
        owner["config_dir"] = str(self.root / "retained account")
        saved = entry(owner)
        with (patch.object(restart, "_run") as launch,
              patch.object(restart, "_probe") as probe,
              patch.object(restart, "_herdr_launch") as herdr_launch,
              patch.object(restart, "_tmux_launch") as tmux_launch):
            result = restart._launch_session(saved, "restart", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(result, restart.SessionLaunched)
        launch.assert_called_once()
        argv = cast(list[str], launch.call_args.args[0])
        self.assertTrue(any(part.endswith("/add_unit.py") for part in argv))
        self.assertEqual(argv[argv.index("--plan") + 1], "/tmp/native plan.md")
        self.assertEqual(argv[argv.index("--host-session") + 1], "native session")
        self.assertEqual(argv[argv.index("--resume") + 1], "native")
        self.assertNotIn("--config-dir", argv)
        self.assertEqual(argv[argv.index("--account") + 1], "claude3")
        self.assertFalse(launch.call_args.kwargs["dry_run"])
        probe.assert_not_called()
        herdr_launch.assert_not_called()
        tmux_launch.assert_not_called()
        self.assertFalse(restart._note_path(saved, "native").exists())  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(conversation_pause.read_scheduled_prompts("native"), ())

    def test_herdr_unit_without_a_run_record_restarts_with_its_units_row_plan(self) -> None:
        doc = self.root / "production.md"
        _ = doc.write_text("\n".join([
            "# Demo", "", "## Units", "",
            "| Unit | Plan | Worktree | Branch | Port | Owns |",
            "| --- | --- | --- | --- | --- | --- |",
            "| native-unit | docs/plans/demo-native.md | /w/native | demo-native | — | — |",
            "",
        ]), encoding="utf-8")
        owner = unit("native", session_host="herdr", session_name="native session",
                     doc=str(doc), plan=inventory.NoRunRecord(kind="no run record"))
        saved = entry(owner)
        with patch.object(restart, "_run") as launch:
            result = restart._launch_session(saved, "restart", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(result, restart.SessionLaunched)
        launch.assert_called_once()
        argv = cast(list[str], launch.call_args.args[0])
        self.assertTrue(any(part.endswith("/add_unit.py") for part in argv))
        self.assertEqual(argv[argv.index("--plan") + 1], "docs/plans/demo-native.md")
        self.assertEqual(argv[argv.index("--host-session") + 1], "native session")
        self.assertEqual(argv[argv.index("--resume") + 1], "native")
        self.assertFalse(restart._note_path(saved, "native").exists())  # pyright: ignore[reportPrivateUsage]

    def test_herdr_unit_without_a_row_plan_or_run_record_refuses_before_launch(self) -> None:
        doc = self.root / "production.md"
        _ = doc.write_text("# Demo\n\n## Units\n", encoding="utf-8")
        owner = unit("native", session_host="herdr", session_name="native session",
                     doc=str(doc), plan=inventory.NoRunRecord(kind="no run record"))
        saved = entry(owner)
        with patch.object(restart, "_run") as launch:
            with self.assertRaisesRegex(restart.NoUnitPlan, "no Plan in the Units table"):
                _ = restart._launch_session(saved, "restart", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        launch.assert_not_called()
        self.assertFalse(restart._note_path(saved, "native").exists())  # pyright: ignore[reportPrivateUsage]

    def test_herdr_unit_account_move_passes_the_target_to_add_unit(self) -> None:
        self.configure_groups(units="claude3")
        saved = entry(unit("native", session_host="herdr", session_name="native session"))
        saved["target"] = "claude3"
        with patch.object(restart, "_run") as launch:
            result = restart._launch_session(saved, "account move", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(result, restart.SessionLaunched)
        argv = cast(list[str], launch.call_args.args[0])
        self.assertEqual(argv[argv.index("--plan") + 1], "/tmp/native plan.md")
        self.assertEqual(argv[argv.index("--host-session") + 1], "native session")
        self.assertEqual(argv[argv.index("--account") + 1], "claude3")
        self.assertNotIn("--config-dir", argv)

    def test_herdr_showrunner_on_old_account_resumes_automatically_on_configured_group(self) -> None:
        template, _, log = self.herdr_restart_fixture()
        directory = accounts.account_dir("claude3")
        _ = (directory / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "third@example.invalid"},
            "projects": {template["cwd"]: {"hasTrustDialogAccepted": True}},
        }))
        self.configure_groups(showrunners="claude3")
        owner = showrunner("runner-resume", host=template["host"])
        owner["name"] = "production-review"
        owner["cwd"] = template["cwd"]
        owner["config_dir"] = str(accounts.account_dir("claude1"))
        saved = entry(owner, where=record.WhereSaid(kind="said", text="saved checkpoint", at=STOPPED))
        with (patch.object(accounts, "prepared_account", wraps=accounts.prepared_account) as prepared,
              patch.object(restart, "_run") as other_host):
            result = restart._launch_session(  # pyright: ignore[reportPrivateUsage]
                saved, restart.restart_note(saved, RESTARTED), self.desktop_state, dry_run=False)
        self.assertIsInstance(result, restart.SessionLaunched)
        prepared.assert_any_call("claude3", resume=True)
        other_host.assert_not_called()
        calls = self.herdr_calls(log)
        opened = next(row for row in calls if row[2:4] == ["workspace", "create"])
        self.assertIn("CLAUDE_CONFIG_DIR=" + str(directory), opened)
        self.assertFalse(any("CLAUDE_CODE_OAUTH_TOKEN=" in value for value in opened))
        started = next(row for row in calls if row[2:4] == ["agent", "start"])
        self.assertEqual(started[started.index("--") + 1:started.index("--") + 3],
                         ["--resume", "runner-resume"])
        self.assertNotIn("claude", started[started.index("--") + 1:])
        self.assertNotIn("accounts.py", " ".join(started))
        self.assertEqual(len([row for row in calls if row[2:4] == ["agent", "prompt"]]), 1)

    def test_herdr_showrunner_already_on_configured_account_retains_native_resume(self) -> None:
        template, _, log = self.herdr_restart_fixture()
        directory = accounts.account_dir("claude3")
        _ = (directory / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "third@example.invalid"},
            "projects": {template["cwd"]: {"hasTrustDialogAccepted": True}},
        }))
        self.configure_groups(showrunners="claude3")
        owner = showrunner("runner-resume", host=template["host"])
        owner["name"] = "production-review"
        owner["cwd"] = template["cwd"]
        owner["config_dir"] = str(directory)
        note = restart.restart_note(entry(owner), RESTARTED)
        parts = restart._resume_parts(owner, note)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(parts.account, restart.MoveResumeAccount("claude3"))
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
        self.configure_groups(units="claude3", showrunners="claude3")
        for owner in (unit("checkpoint-unit"), showrunner("checkpoint-runner")):
            with self.subTest(kind=owner["kind"]):
                saved = entry(owner, where=record.WhereSaid(kind="said", text=checkpoint, at=STOPPED))
                saved["target"] = "claude3"
                saved["source_label"] = LABEL
                saved["why"] = "weekly refusal"
                note = restart.restart_note(saved, RESTARTED)
                self.assertIn(checkpoint, note)
                self.assertIn("moved from claude2 to claude3", note)
                captured: list[str] = []

                def launched(argv: list[str], *, dry_run: bool) -> subprocess.CompletedProcess[str]:
                    _ = dry_run
                    if owner["kind"] == "unit":
                        captured.append(Path(argv[argv.index("--restart-note") + 1]).read_text())
                    elif "-ic" in argv:
                        captured.append(shlex.split(argv[-1].removesuffix("; exec zsh"))[-1])
                    return subprocess.CompletedProcess(argv, 0, "", "")

                def launched_herdr(parts: restart.ResumeParts, destination: restart.HerdrDestination,
                                   session: inventory.Session, *, dry_run: bool) -> None:
                    _ = destination, session, dry_run
                    captured.append(parts.opening_prompt)

                with (patch.object(restart, "_run", side_effect=launched),
                      patch.object(restart, "_herdr_launch", side_effect=launched_herdr)):
                    result = restart._launch_session(saved, note, self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
                self.assertIsInstance(result, restart.SessionLaunched)
                self.assertEqual(len(captured), 1)
                self.assertIn(checkpoint, captured[0])
                if owner["kind"] == "showrunner":
                    self.assertTrue(captured[0].startswith("/showrunner:produce "))

    def test_move_target_tmux_showrunner_passes_its_production_prompt_to_herdr(self) -> None:
        self.configure_groups(showrunners="claude3")
        owner = showrunner("move-showrunner")
        saved = entry(owner)
        saved["target"] = "claude3"
        with patch.object(restart, "_herdr_launch") as launch:
            _ = restart._launch_session(saved, "moved from claude2 to claude3", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        parts = cast(restart.ResumeParts, launch.call_args.args[0])
        self.assertEqual(parts.account, restart.MoveResumeAccount("claude3"))
        self.assertEqual(parts.claude_arguments[:2], ("--resume", "move-showrunner"))
        self.assertTrue(parts.opening_prompt.startswith("/showrunner:produce /tmp/demo production.md resume "))
        self.assertIn("moved from claude2 to claude3", parts.opening_prompt)

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
                with patch.object(restart, "_herdr_launch") as launch:
                    commands, _ = self.moved_restart(owner)
                command = next(argv[-1] for argv in commands if "ghostty" in argv)
                arguments = shlex.split(command.removesuffix("; exec zsh"))
                self.assertEqual(arguments[:3], ["cd", owner["cwd"], "&&"])
                parts = cast(restart.ResumeParts, launch.call_args.args[0])
                argv = parts.claude_arguments
                self.assertEqual(argv[argv.index("--resume") + 1], identity)
                self.assertEqual(argv[argv.index("-n") + 1], name)
                self.assertEqual(argv[argv.index("--remote-control") + 1], name)
                self.assertEqual(argv[argv.index("--model") + 1], model)
                self.assertTrue(parts.opening_prompt.startswith(f"/showrunner:produce {doc} resume "))
                self.assertIn("moved from claude2 to claude3", parts.opening_prompt)
                self.assertEqual(arguments[-4:], ["herdr", "session", "attach", name])

    def test_moved_ghostty_showrunner_reopens_on_its_recorded_desktop_without_window_title_tracking(self) -> None:
        self.configure_groups(showrunners="claude3")
        owner = showrunner("hana-id", host=inventory.WindowHost(kind="ghostty", window_shell=123,
            desktop=inventory.NamedDesktop(kind="named", name="Production")))
        saved = entry(owner)
        saved["target"] = "claude3"
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
        saved["target"] = "claude3"
        self.create([saved])
        output = io.StringIO()
        command = restart.resume_command(saved["session"], "claude3", LABEL)
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
        saved["target"] = "claude3"
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
        self.assertIn(restart.resume_command(saved["session"], "claude3", LABEL), lines[0])

    def test_move_restart_note_names_source_target_reason_and_continuation_for_every_session_kind(self) -> None:
        for owner in (top_level("top"), unit("unit"), showrunner("runner")):
            with self.subTest(kind=owner["kind"]):
                saved = entry(owner)
                saved["target"] = "claude3"
                note = restart.restart_note(saved, RESTARTED)
                self.assertIn("moved from claude2 to claude3", note.casefold())
                self.assertIn("claude2 is out of usage", note)
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
                saved["target"] = "claude2"
                saved["source_label"] = "claude3"
                saved["why"] = "weekly limit reached"
                note = restart.restart_note(saved, RESTARTED)
                self.assertIn("moved from claude3 to claude2 because weekly limit reached", note)
                self.assertIn("account move, not a stop", note)
                self.assertIn("Continue where you stopped", note)
                command = restart.resume_command(owner, "claude2", "claude3", "weekly limit reached")
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

        def start_agent(_client: herdr.Herdr, _name: str, _pane: str, *, kind: str,
                        agent_arguments: list[str], opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt, timeout_ms: int) -> None:
            self.assertEqual(kind, "claude")
            self.assertEqual(timeout_ms, 90_000)
            session_id = agent_arguments[agent_arguments.index("--resume") + 1]
            self.assertIsInstance(opening, herdr.OpeningPrompt)
            assert isinstance(opening, herdr.OpeningPrompt)
            self.assertEqual(opening.text, expected_prompts[session_id])
            self.assertEqual(conversation_pause.read_scheduled_prompts(session_id),
                             (f"wakeup for {session_id}", opening.text))
            observed_launches.append(session_id)

        def probe(argv: list[str], *, capture_output: bool, text: bool, check: bool) -> subprocess.CompletedProcess[str]:
            del capture_output, text, check
            if argv[:2] == ['tmux', 'display']:
                return subprocess.CompletedProcess(argv, 0, 'scheduled-tmux-host\t410\t0\n', '')
            if argv[:1] == ['pgrep']:
                return subprocess.CompletedProcess(argv, 1, '', '')
            return subprocess.CompletedProcess(argv, 0, '', '')
        with patch.object(restart, '_run', run), patch.object(restart, '_live_session_ids', return_value=frozenset()), patch.object(restart, 'wait_for_confirmation', side_effect=self.all_sessions_live), patch.object(settle, 'send_message', return_value=settle.MessageSent(kind='sent')), patch.object(settle, 'record_time', return_value=RESTARTED), patch('subprocess.run', probe), patch.object(herdr, 'claude_trust', return_value=herdr.Trusted(Path('/tmp/scheduled-showrunner'), Path('/unused'), Path('/tmp/scheduled-showrunner'))), patch.object(herdr, 'start_new_server'), patch.object(herdr, 'server_environment', return_value={}), patch.object(herdr.Herdr, 'agents', return_value=[]), patch.object(herdr, 'open_pane', return_value='new-pane'), patch.object(herdr.Herdr, 'start_agent', autospec=True, side_effect=start_agent):
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

    def test_permanent_unit_without_a_run_record_resumes_as_permanent(self) -> None:
        doc = self.root / 'production.md'
        _ = doc.write_text('\n'.join(['# Demo', '', '## Units', '', '| Unit | Plan | Worktree | Branch | Port | Owns |', '| --- | --- | --- | --- | --- | --- |', '| nightly-review-unit | permanent | /w/nightly | demo-nightly-review | — | — |', '']), encoding='utf-8')
        moved = entry(unit('nightly-review', doc=str(doc), plan=inventory.NoRunRecord(kind='no run record')))
        self.create(records_for_test([moved]))
        output = io.StringIO()
        with patch.object(restart, '_live_session_ids', return_value=frozenset()), patch.object(settle, 'record_time', return_value=RESTARTED), redirect_stdout(output):
            self.assertEqual(restart.up(LOGIN, dry_run=True), 0)
        argv = next((shlex.split(line) for line in output.getvalue().splitlines() if 'add_unit.py' in line))
        self.assertIn('--permanent', argv)
        self.assertNotIn('--standby', argv)

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
        self.assertEqual(len(resume_commands), 2 if sys.platform == "linux" else 3)
        self.assertIn("herdr tmux-named-showrunner", output.getvalue())
        for command in resume_commands:
            if "--resume named-showrunner " in command:
                self.assertIn("accounts.py exec claude2", command)
                self.assertNotIn(config_dir, command)
            elif "herdr session attach" in command:
                self.assertNotIn("CLAUDE_CONFIG_DIR", command)
                self.assertTrue(command.endswith("herdr session attach named-window; exec zsh"))
            else:
                self.assertIn(f'&& {named_prefix(config_dir)}ENABLE_TOOL_SEARCH=true command claude', command)
        self.assertIn(f'{named_prefix(config_dir)}ENABLE_TOOL_SEARCH=true command claude', output.getvalue().split('manual restart named-manual:', 1)[1])
        unit_argv = next((argv for argv in commands.calls if any((part.endswith('add_unit.py') for part in argv))))
        self.assertNotIn("--config-dir", unit_argv)
        self.assertEqual(unit_argv[unit_argv.index("--account") + 1], "claude2")
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
            restart._resume_command(owner, "line one\nline two", "claude3"),  # pyright: ignore[reportPrivateUsage]
            "cd '/tmp/with space' && ENABLE_TOOL_SEARCH=true command "
            + shlex.quote(sys.executable) + " "
            + shlex.quote(str(restart.SCRIPTS / "accounts" / "accounts.py"))
            + " exec claude3 " + suffix,
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
        restored = entry(top_level('quoted-id', name=name, cwd=str(cwd), model=inventory.ModelName(kind='model', name=model), host=inventory.TerminalHost(kind='terminal')), where=record.WhereSaid(kind='said', text=where, at=STOPPED))
        config_dir = str(self.root / "account with 'quotes' and $()")
        restored['session']['config_dir'] = config_dir
        saved = records_for_test([restored])
        self.create(saved)
        output = io.StringIO()
        with patch.object(sys, 'platform', 'darwin'), patch.object(restart, '_live_session_ids', return_value=frozenset()), patch.object(settle, 'record_time', return_value=RESTARTED), patch('subprocess.run', return_value=subprocess.CompletedProcess(['kdotool', 'get_desktop'], 0, '1\n', '')), redirect_stdout(output):
            self.assertEqual(restart.up(LOGIN, dry_run=True), 0)
        memory_base = str(Path.home() / '.claude')
        launch_line = next((line for line in output.getvalue().splitlines() if 'Ghostty.app' in line))
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
        self.assertEqual(args[args.index("--text-id") + 1], "shutdown.restart_incomplete")
        self.assertIn('account="claude2"', args)
        self.assertIn("machine=Mac", args)
        self.assertEqual(body, "")
        self.assertNotIn("--action", args)

    def test_restart_failure_push_uses_only_account_count_and_machine_facts(self) -> None:
        push = restart.restart_push("claude2", ["Beta"], machine="natedev")
        self.assertEqual(push.id, "shutdown.restart_failed")
        self.assertEqual(push.values, {"account": '"claude2"', "stopped": "One session", "machine": "natedev"})
        self.assertIn('/shutdown restart "claude2"', cast(ActionRequired, push.action).text)
        unknown = restart.restart_push("claude2", [], machine="Mac")
        self.assertEqual(unknown.id, "shutdown.restart_incomplete")
        self.assertEqual(unknown.values, {"account": '"claude2"', "machine": "Mac"})

    def test_large_restart_failure_group_keeps_count_without_exceeding_phone_limit(self) -> None:
        names = ["Session" + str(index) + "x" * 80 for index in range(100)]
        push = restart.restart_push(LABEL, names, machine="Mac")
        self.assertNotIn("sessions", push.values)
        self.assertEqual(push.values["stopped"], "100 sessions")
        self.assertLess(len("Action: " + cast(ActionRequired, push.action).text + "\n" + push.body), 1024)

    def test_restart_keeps_pending_job_status_off_phone_and_names_manual_restore(self) -> None:
        pending = restart.MachineRestartResult("natedev", LABEL, "partial", restarted=1,
            timers_not_started=(restart.TimerNotStarted("Owner", "job", "unavailable"),))
        manual = restart.MachineRestartResult("natedev", LABEL, "partial", manual=1,
            manual_sessions=(restart.ManualSessionRestart("Owner", "private resume command"),))
        for result in (pending, manual):
            with (self.subTest(manual=result.manual),
                  patch.object(restart, "named_claude_account", return_value=self.account),
                  patch.object(restart, "_up", return_value=result),
                  patch.object(restart, "_restart_alerts_enabled", return_value=True),
                  patch.object(settle, "alert_user") as alert, redirect_stdout(io.StringIO())):
                self.assertEqual(restart.restart(restart.AccountNamed(LABEL), dry_run=False,
                    alerts=restart.AlertWhenNotBack(), here=True), 1)
            self.assertEqual(alert.call_count, result.manual)
            if alert.called:
                push = cast(RenderedPush, alert.call_args.args[0])
                self.assertEqual(push.id, "shutdown.manual_restart")
                self.assertEqual(push.values, {"session": "Owner", "machine": "natedev"})

    def test_restart_with_no_failed_sessions_returns_success_without_a_retry_push(self) -> None:
        completed = restart.MachineRestartResult("natedev", LABEL, "done", restarted=1)
        with (patch.object(restart, "named_claude_account", return_value=self.account),
              patch.object(restart, "_up", return_value=completed),
              patch.object(settle, "alert_user") as alert, redirect_stdout(io.StringIO())):
            self.assertEqual(restart.restart(restart.AccountNamed(LABEL), dry_run=False,
                alerts=restart.AlertWhenNotBack(), here=True), 0)
        alert.assert_not_called()

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

    def test_dead_owner_in_stopping_finishes_cleanup_before_restore_and_keeps_intent(self) -> None:
        saved = entry(top_level("crashed", host=tmux_host("crashed")),
                      state=record.SessionStopping(kind="stopping", at=STOPPED))
        saved["target"] = "claude3"
        saved["where"] = record.WhereSaid(kind="said", at=STOPPED, text="resume the repair")
        saved["timers"] = [TimerRestore(instance="timer", was_enabled=True,
                                       footer=record.NoFooter(kind="no footer"))]
        self.create([saved])
        def cleanup(current: SessionShutdown) -> SessionShutdown:
            self.assertEqual(current["progress"]["kind"], "stopping")
            return record.update_session(LOGIN, "crashed", lambda changing: changing.update(
                progress=record.SessionAlreadyGone(kind="already gone", at=RESTARTED)))
        launched: list[SessionShutdown] = []
        def launch(current: SessionShutdown, _note: str, _desktop: restart.DesktopLaunchState, *,
                   dry_run: bool) -> restart.SessionLaunched:
            self.assertFalse(dry_run)
            launched.append(current)
            return restart.SessionLaunched()
        with (patch.object(restart, "_session_liveness", return_value=restart.SessionNotLive()),
              patch.object(stop_work, "stop_session", side_effect=cleanup) as stop,
              patch.object(restart, "_retire"),
              patch.object(restart, "_live_session_ids", return_value=frozenset()),
              patch.object(restart, "_launch_session", side_effect=launch),
              patch.object(restart, "wait_for_confirmation", return_value=restart.RegistryConfirmation()),
              redirect_stdout(io.StringIO())):
            _ = restart.up(LOGIN)
        stop.assert_called_once()
        self.assertEqual(len(launched), 1)
        self.assertEqual((launched[0].get("target"), launched[0]["timers"], launched[0]["where"],
                          launched[0]["request_id"]),
                         ("claude3", saved["timers"], saved["where"], saved["request_id"]))

    def test_unknown_incomplete_owner_liveness_refuses_without_cleanup_or_launch(self) -> None:
        saved = entry(top_level("uncertain"), state=record.SessionWaiting(kind="waiting"))
        self.create([saved])
        output = io.StringIO()
        with (patch.object(restart, "_session_liveness", return_value=restart.SessionLivenessUnknown("registry unreadable")),
              patch.object(stop_work, "stop_session") as stop,
              patch.object(restart, "_launch_session") as launch, redirect_stdout(output)):
            self.assertEqual(restart.up(LOGIN), 1)
        stop.assert_not_called()
        launch.assert_not_called()
        self.assertIn("registry unreadable", output.getvalue())
        self.assertEqual(record.find_session(LOGIN, "uncertain"), saved)

    def test_conflicting_shutdown_accounts_refuse_before_any_launch(self) -> None:
        first = entry(top_level("same-id", host=tmux_host("same-id")))
        second = copy.deepcopy(first)
        second["login"], second["label"] = "third@example.invalid", "claude3"
        self.create([first, second])
        with patch.object(restart, "_launch_session") as launch, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(restart.up(LOGIN), 1)
        launch.assert_not_called()
        self.assertIn(LOGIN, output.getvalue())
        self.assertIn("third@example.invalid", output.getvalue())
        self.assertIsNotNone(record.find_session(LOGIN, "same-id"))

    def test_materialized_snapshot_retry_keeps_a_saved_live_owner_without_registry_row(self) -> None:
        for progress in (record.SessionAlreadyGone(kind="already gone", at=STOPPED),
                         record.SessionRestartFailed(kind="restart failed", at=STOPPED, reason="window failed")):
            with self.subTest(progress=progress["kind"]):
                owner = top_level("live-retry-" + progress["kind"].replace(" ", "-"), host=tmux_host("retry"))
                snapshot = self.recovery([owner])
                candidate = snapshot["sessions"][0]
                assert candidate["agent"] == "claude"
                saved = record.materialize_snapshot(snapshot, candidate)
                saved = record.update_session(saved["login"], saved["session"]["session_id"], lambda current: current.update(progress=progress))
                registry = self.root / "sessions"
                registry.mkdir(exist_ok=True)
                with (patch.object(os, "kill"),
                      patch.object(sessions, "process_start", return_value=owner["proc_start"]),
                      patch.object(restart, "_launch_session") as launch,
                      redirect_stdout(io.StringIO()) as output):
                    self.assertEqual(restart.up(LOGIN, only=(owner["session_id"],)), 0)
                launch.assert_not_called()
                self.assertIn("already running; skipped", output.getvalue())
                self.assertEqual(record.find_session(LOGIN, owner["session_id"]), saved)

    def test_materialized_snapshot_retry_refuses_an_unreadable_registry(self) -> None:
        snapshot = self.recovery([top_level("uncertain-retry", host=tmux_host("retry"))])
        candidate = snapshot["sessions"][0]
        assert candidate["agent"] == "claude"
        saved = record.materialize_snapshot(snapshot, candidate)
        registry = self.root / "sessions"
        registry.mkdir()
        _ = (registry / "broken.json").write_text("{broken")
        with patch.object(restart, "_launch_session") as launch, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(restart.up(LOGIN), 1)
        launch.assert_not_called()
        self.assertIn("unreadable session registry record", output.getvalue())
        self.assertEqual(record.find_session(LOGIN, "uncertain-retry"), saved)

    def test_snapshot_retry_allows_a_reused_pid_even_when_signalling_it_would_be_denied(self) -> None:
        saved = entry(top_level("reused", host=tmux_host("reused")))
        saved["requested_by"] = record.FromSnapshot(kind="snapshot", taken_at=STOPPED)
        with (patch.object(restart, "_session_liveness", return_value=restart.SessionNotLive()),
              patch.object(os, "kill", side_effect=PermissionError("different owner")),
              patch.object(sessions, "process_start", return_value="new-process-start"),
              patch.object(restart, "_live_session_ids", return_value=frozenset()),
              patch.object(restart, "_retire"),
              patch.object(restart, "_launch_session", return_value=restart.SessionLaunched()) as launch,
              redirect_stdout(io.StringIO())):
            _ = restart.launch_entry(saved, RESTARTED, self.desktop_state, dry_run=True)
        launch.assert_called_once()

    def test_snapshot_retry_without_process_start_keeps_the_registry_absence_answer(self) -> None:
        owner = top_level("legacy-owner", host=tmux_host("legacy-owner"))
        owner["proc_start"] = ""
        saved = entry(owner)
        saved["requested_by"] = record.FromSnapshot(kind="snapshot", taken_at=STOPPED)
        with (patch.object(restart, "_session_liveness", return_value=restart.SessionNotLive()),
              patch.object(os, "kill"), patch.object(sessions, "process_start", return_value="present"),
              patch.object(restart, "_live_session_ids", return_value=frozenset()),
              patch.object(restart, "_retire"),
              patch.object(restart, "_launch_session", return_value=restart.SessionLaunched()) as launch,
              redirect_stdout(io.StringIO())):
            _ = restart.launch_entry(saved, RESTARTED, self.desktop_state, dry_run=True)
        launch.assert_called_once()

    def test_showrunner_snapshot_admission_checks_the_configured_launch_account_hold(self) -> None:
        self.configure_groups(showrunners="claude3")
        _ = self.recovery([showrunner("held-group")])
        record.set_account_hold(record.AccountHold(login="third@example.invalid", label="claude3", machine="natedev",
            requested_at=STOPPED, request_id="target-hold", requested_by=record.FromTerminal(kind="terminal"), then="stop"))
        with (patch.object(restart, "_owner_liveness", return_value=restart.SessionNotLive()),
              patch.object(restart, "_session_liveness", return_value=restart.SessionNotLive()),
              patch.object(restart, "_live_session_ids", return_value=frozenset()),
              patch.object(restart, "_retire"), patch.object(restart, "_launch_session") as launch,
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(restart.up(LOGIN), 1)
        launch.assert_not_called()
        self.assertIn("claude3 is held by a shutdown", output.getvalue())
        current = record.find_session(LOGIN, "held-group")
        assert current is not None
        self.assertEqual(current["progress"]["kind"], "restart failed")

    def test_confirmed_move_uses_the_only_remaining_snapshot_copy_at_its_target(self) -> None:
        moved = entry(top_level("only-old-copy"), state=record.SessionRestarted(kind="restarted", at=RESTARTED))
        moved["target"] = "claude3"
        moved["requested_at"] = "2026-10-09T22:00:00+00:00"
        self.create([moved])
        record.archive_session(LOGIN, moved["session"]["session_id"])
        _ = self.recovery([top_level("only-old-copy", host=tmux_host("old-host"))])
        with (patch.object(restart, "_owner_liveness", return_value=restart.SessionNotLive()),
              patch.object(restart, "_live_session_ids", return_value=frozenset()),
              patch.object(restart, "_retire"), patch.object(restart, "_launch_session", return_value=restart.SessionLaunched()) as launch,
              patch.object(restore_permission, "launch_permission", return_value=restore_permission.AllowedByShutdownState()),
              patch.object(restart, "wait_for_confirmation", side_effect=self.all_sessions_live),
              patch.object(restart, "_prove_move", return_value=True),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(restart.up(LOGIN), 0, output.getvalue())
        launch.assert_called_once()
        restored = cast(SessionShutdown, cast(object, launch.call_args.args[0]))
        self.assertEqual(restored.get("target"), "claude3")
        self.assertEqual(restored["session"]["session_id"], "only-old-copy")

    def test_snapshot_source_uses_the_entry_last_seen_instead_of_the_latest_pass_time(self) -> None:
        snapshot = self.recovery([top_level("old-observation", host=tmux_host("old-observation"))])
        snapshot["taken_at"] = RESTARTED
        record.write_snapshot(snapshot)
        with (patch.object(restart, "_owner_liveness", return_value=restart.SessionLive()),
              patch.object(restart, "datetime", RestartAttemptTime),
              patch.object(restart, "user_zone", return_value=timezone.utc),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(restart.up(LOGIN), 0)
        self.assertIn("from snapshot taken 2026-10-09 21:49:10 UTC (3h ago)", output.getvalue())
        self.assertNotIn("from snapshot taken 2026-10-10", output.getvalue())

    def test_restart_requires_a_boot_change_or_lost_host_for_an_unconfirmed_snapshot(self) -> None:
        for signal in ("reboot", "host gone", "clean exit", "unreadable"):
            with self.subTest(signal=signal):
                identity = "crash-proof-" + signal.replace(" ", "-")
                snapshot = self.recovery([top_level(identity, host=tmux_host(identity))])
                candidate = snapshot["sessions"][0]
                _ = candidate.pop("crash", None)
                candidate["boot_id"] = "old-boot"
                record.write_snapshot(snapshot)
                observation = (inventory.PartialInventory(("host lookup unreadable",)) if signal == "unreadable" else
                    inventory.CompleteInventory("natedev", {}, {},
                        inventory.KnownBoot("new-boot" if signal == "reboot" else "old-boot"),
                        frozenset({(LOGIN, identity)}) if signal == "host gone" else frozenset()))
                with (patch.object(inventory, "snapshot_inventory", return_value=observation) as observe,
                      patch.object(restart, "_owner_liveness", return_value=restart.SessionNotLive()),
                      patch.object(restart, "_live_session_ids", return_value=frozenset()),
                      patch.object(restart, "_retire"),
                      patch.object(restore_permission, "launch_permission", return_value=restore_permission.AllowedByShutdownState()),
                      patch.object(restart, "_launch_session", return_value=restart.SessionLaunched()) as launch,
                      patch.object(restart, "wait_for_confirmation", side_effect=self.all_sessions_live),
                      redirect_stdout(io.StringIO()) as output):
                    self.assertEqual(restart.up(LOGIN, only=(identity,)), 1 if signal == "unreadable" else 0)
                observe.assert_called_once()
                if signal in {"reboot", "host gone"}:
                    launch.assert_called_once()
                    self.assertIsNone(record.find_session(LOGIN, identity))
                else:
                    launch.assert_not_called()
                    self.assertIsNone(record.find_session(LOGIN, identity))
                    self.assertIn("no crash signal" if signal == "clean exit" else "host lookup unreadable", output.getvalue())

    def test_terminal_restart_reads_completed_history_once_for_all_accounts(self) -> None:
        first = entry(top_level("first-account", host=tmux_host("first-account")))
        second = entry(top_level("second-account", host=tmux_host("second-account")))
        second["login"], second["label"] = "third@example.invalid", "claude3"
        self.create([first, second])
        environment = {key: value for key, value in os.environ.items() if key != "CLAUDE_CODE_SESSION_ID"}
        with (patch.dict(os.environ, environment, clear=True),
              patch.object(record, "restarted_history", wraps=record.restarted_history) as read,
              patch.object(restart, "_launch_session", return_value=restart.SessionLaunched()) as launch,
              patch.object(restart, "_live_session_ids", return_value=frozenset()),
              patch.object(restart, "_retire"), redirect_stdout(io.StringIO())):
            self.assertEqual(shutdown.main(["restart", "--here", "--dry-run"]), 0)
        read.assert_called_once()
        self.assertCountEqual([cast(SessionShutdown, cast(object, call.args[0]))["login"]
                               for call in launch.call_args_list], [LOGIN, "third@example.invalid"])

    def test_retired_move_target_refuses_its_old_snapshot_while_unrelated_restore_continues(self) -> None:
        archived = entry(top_level("retired-move"), state=record.SessionRestarted(kind="restarted", at=RESTARTED))
        archived["target"] = "claude3"
        self.create([archived])
        record.archive_session(LOGIN, "retired-move")
        history = self.state_root / LOGIN / "history"
        archive = next(history.glob("*.json"))
        (self.root / "notes/claude3.md").unlink()
        _ = (history / "bad.json").write_text("{broken")
        snapshot = self.recovery([top_level("retired-move", host=tmux_host("old-host"))])
        self.create([entry(top_level("unrelated", host=tmux_host("unrelated")))])
        with (patch.object(restart, "_live_session_ids", return_value=frozenset()),
              patch.object(restart, "_retire"),
              patch.object(record, "restarted_history", wraps=record.restarted_history) as read,
              patch.object(restart, "_launch_session", return_value=restart.SessionLaunched()) as launch,
              patch.object(restart, "wait_for_confirmation", side_effect=self.all_sessions_live),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(restart.up(LOGIN), 1)
        read.assert_called_once()
        launch.assert_called_once()
        launched = cast(SessionShutdown, cast(object, launch.call_args.args[0]))
        self.assertEqual(launched["session"]["session_id"], "unrelated")
        self.assertIn(f"retired-move: conflicting account evidence: {LABEL} ({LOGIN}), claude3 (unavailable)", output.getvalue())
        self.assertIsNone(record.find_session(LOGIN, "retired-move"))
        self.assertEqual(record.read_snapshot(LOGIN), snapshot)
        self.assertIsNone(record.find_session(LOGIN, "unrelated"))
        self.assertTrue(archive.exists())
        self.assertEqual((history / "bad.json").read_text(), "{broken")

    def test_record_and_snapshot_restore_together_with_record_precedence_and_recorded_admission(self) -> None:
        owner = top_level("recorded", host=tmux_host("recorded"))
        self.create([entry(owner)])
        stale = copy.deepcopy(owner)
        stale["cwd"] = "/obsolete/directory"
        _ = self.recovery([stale, top_level("crashed", host=tmux_host("crashed"))])
        launched: list[SessionShutdown] = []
        purposes: list[str] = []
        def admission(_environment: object, purpose: restore_permission.LaunchPurpose) -> restore_permission.AllowedByShutdownState:
            self.assertIsInstance(purpose, restore_permission.RecordedSessionRestoreLaunchPurpose)
            assert isinstance(purpose, restore_permission.RecordedSessionRestoreLaunchPurpose)
            current = record.find_session(LOGIN, purpose.session_id)
            assert current is not None
            self.assertEqual(current["progress"]["kind"], "restarting")
            purposes.append(purpose.session_id)
            return restore_permission.AllowedByShutdownState()
        def launch(current: SessionShutdown, _note: str, _desktop: restart.DesktopLaunchState, *,
                   dry_run: bool) -> restart.SessionLaunched:
            self.assertFalse(dry_run)
            launched.append(current)
            return restart.SessionLaunched()
        with (patch.object(restart, "_retire"),
              patch.object(restart, "_session_liveness", return_value=restart.SessionNotLive()),
              patch.object(restart, "_live_session_ids", return_value=frozenset()),
              patch.object(restart, "_launch_session", side_effect=launch),
              patch.object(restore_permission, "launch_permission", side_effect=admission),
              patch.object(restart, "wait_for_confirmation", side_effect=self.all_sessions_live),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(restart.up(LOGIN), 0)
        self.assertEqual({saved["session"]["session_id"] for saved in launched}, {"recorded", "crashed"})
        self.assertEqual(next(saved["session"]["cwd"] for saved in launched if saved["session"]["session_id"] == "recorded"), owner["cwd"])
        self.assertEqual(purposes, ["crashed"])
        self.assertIn("recorded: from shutdown record", output.getvalue())
        self.assertIn("crashed: from snapshot taken", output.getvalue())
        retained = record.read_snapshot(LOGIN)
        assert not isinstance(retained, record.NoRecoverySnapshot)
        self.assertEqual(retained["sessions"], [])

    def test_failed_snapshot_restart_keeps_materialized_request_and_snapshot_for_retry(self) -> None:
        original = self.recovery([top_level("crashed", host=tmux_host("crashed"))])
        with (patch.object(restart, "_retire"),
              patch.object(restart, "_session_liveness", return_value=restart.SessionNotLive()),
              patch.object(restart, "_live_session_ids", return_value=frozenset()),
              patch.object(restart, "_launch_session", side_effect=OSError("launcher unavailable")),
              patch.object(restore_permission, "launch_permission", return_value=restore_permission.AllowedByShutdownState()),
              redirect_stdout(io.StringIO())):
            self.assertEqual(restart.up(LOGIN), 1)
        self.assertEqual(record.read_snapshot(LOGIN), original)
        current = record.find_session(LOGIN, "crashed")
        assert current is not None
        self.assertEqual(current["requested_by"]["kind"], "snapshot")
        self.assertEqual(current["progress"], {"kind": "restart failed", "at": RESTARTED, "reason": "launcher unavailable"})

    def test_snapshot_dry_run_previews_without_materializing_or_acknowledging(self) -> None:
        _ = self.recovery([top_level("crashed", host=tmux_host("crashed"))])
        before = self.files()
        with (patch.object(restart, "_session_liveness", return_value=restart.SessionNotLive()),
              patch.object(restart, "_live_session_ids", return_value=frozenset()),
              patch.object(restart, "_retire"),
              patch.object(restart, "_launch_session", return_value=restart.SessionLaunched()) as launch,
              patch.object(restart, "materialize_snapshot") as materialize,
              patch.object(restore_permission, "launch_permission") as admit,
              redirect_stdout(io.StringIO())):
            self.assertEqual(restart.up(LOGIN, dry_run=True), 0)
        launch.assert_called_once()
        self.assertTrue(launch.call_args.kwargs["dry_run"])
        materialize.assert_not_called()
        admit.assert_not_called()
        self.assertEqual(self.files(), before)

    def test_snapshot_restore_admission_refusal_keeps_record_and_never_launches(self) -> None:
        snapshot = self.recovery([top_level("blocked", host=tmux_host("blocked"))])
        with (patch.object(restart, "_session_liveness", return_value=restart.SessionNotLive()),
              patch.object(restart, "_live_session_ids", return_value=frozenset()),
              patch.object(restart, "_retire"),
              patch.object(restore_permission, "launch_permission", return_value=restore_permission.BlockedByShutdown("account held")),
              patch.object(restart, "_launch_session") as launch, redirect_stdout(io.StringIO()) as output):
            self.assertEqual(restart.up(LOGIN), 1)
        launch.assert_not_called()
        self.assertIn("account held", output.getvalue())
        self.assertIsNotNone(record.find_session(LOGIN, "blocked"))
        self.assertEqual(record.read_snapshot(LOGIN), snapshot)

    def test_snapshot_session_running_on_another_account_is_skipped_without_materialization(self) -> None:
        original = self.recovery([top_level("already-running")])
        live = sessions.SessionRecord(pid=99000, sessionId="already-running", name="Now on claude3",
            messagingSocketPath="", updatedAt=1, tmux="", formerNames=[])
        with (patch.object(sessions, "live_sessions", return_value=[live]),
              patch.object(restart, "_launch_session") as launch, redirect_stdout(io.StringIO()) as output):
            self.assertEqual(restart.up(LOGIN), 0)
        launch.assert_not_called()
        self.assertIsNone(record.find_session(LOGIN, "already-running"))
        self.assertEqual(record.read_snapshot(LOGIN), original)
        self.assertIn("already-running: already running; skipped", output.getvalue())

    def test_snapshot_process_without_messaging_socket_is_skipped_without_materialization(self) -> None:
        original = self.recovery([top_level("socket-gone")])
        running = sessions.SessionRecord(pid=99000, sessionId="socket-gone", name="Running without socket",
            messagingSocketPath="", updatedAt=1, tmux="", formerNames=[])
        with (patch.object(sessions, "live_sessions", return_value=[]),
              patch.object(sessions, "running_sessions", return_value=[running]),
              patch.object(restart, "_launch_session") as launch, redirect_stdout(io.StringIO()) as output):
            self.assertEqual(restart.up(LOGIN), 0)
        launch.assert_not_called()
        self.assertIsNone(record.find_session(LOGIN, "socket-gone"))
        self.assertEqual(record.read_snapshot(LOGIN), original)
        self.assertIn("socket-gone: already running; skipped", output.getvalue())

    def test_snapshot_saved_process_still_running_without_registry_row_is_not_relaunched(self) -> None:
        owner = top_level("registry-row-gone")
        original = self.recovery([owner])
        with (patch.object(restart, "_session_liveness", return_value=restart.SessionNotLive()),
              patch.object(restart, "_live_session_ids", return_value=frozenset()),
              patch.object(os, "kill"),
              patch.object(sessions, "process_start", return_value=owner["proc_start"]),
              patch.object(restart, "_launch_session") as launch, redirect_stdout(io.StringIO()) as output):
            self.assertEqual(restart.up(LOGIN), 0)
        launch.assert_not_called()
        self.assertIn("already running; skipped", output.getvalue())
        self.assertIsNone(record.find_session(LOGIN, owner["session_id"]))
        self.assertEqual(record.read_snapshot(LOGIN), original)

    def test_confirmed_move_and_newer_snapshot_prevent_old_account_restore(self) -> None:
        moved = entry(top_level("moved"), state=record.SessionRestarted(kind="restarted", at=RESTARTED))
        moved["target"] = "claude3"
        self.create([moved])
        _ = self.recovery([top_level("moved"), top_level("observed-later")])
        _ = self.recovery([top_level("moved"), top_level("observed-later")],
                          login="third@example.invalid", at=RESTARTED)
        with patch.object(restart, "_launch_session") as launch, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(restart.up(LOGIN), 0)
        launch.assert_not_called()
        self.assertIn("current account third@example.invalid", output.getvalue())
        self.assertIsNone(record.find_session(LOGIN, "observed-later"))

    def test_running_newer_account_does_not_reactivate_an_older_record_after_acknowledgement(self) -> None:
        older = entry(top_level("moved-again", host=tmux_host("old")))
        self.create([older])
        _ = self.recovery([top_level("moved-again", host=tmux_host("new"))],
                          login="third@example.invalid", at=RESTARTED)
        with (patch.object(restart, "_session_liveness", return_value=restart.SessionNotLive()),
              patch.object(restart, "_retire"),
              patch.object(restore_permission, "launch_permission", return_value=restore_permission.AllowedByShutdownState()),
              patch.object(restart, "_launch_session", return_value=restart.SessionLaunched()) as launch,
              patch.object(restart, "_live_session_ids", side_effect=lambda: frozenset({"moved-again"}) if launch.called else EMPTY_SESSION_IDS),
              patch.object(restart, "wait_for_confirmation", side_effect=self.all_sessions_live),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(restart.up("third@example.invalid"), 0)
            self.assertEqual(restart.up(LOGIN), 0)
        launch.assert_called_once()
        self.assertEqual(launch.call_args.args[0]["login"], "third@example.invalid")
        self.assertIn("already running on third@example.invalid; skipped", output.getvalue())
        self.assertIsNotNone(record.find_session(LOGIN, "moved-again"))

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
        with patch.object(restart, "_session_liveness", return_value=restart.SessionLive()):
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
        saved["trail"] = [record.ProgressStep(at=STOPPED, kind="manual restart", detail="resume")]
        with patch.object(record, "datetime", RestartAttemptTime):
            self.assertEqual(self.run_restarts(), (1, []))
        self.assertEqual(record.find_session(LOGIN, "manual"), {
            **saved, "trail": [
                *saved["trail"],
                record.ProgressStep(at=RESTARTED, kind="restarting", detail=""),
                record.ProgressStep(at=RESTARTED, kind="manual restart", detail="resume"),
            ],
        })

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

    def test_snapshot_restore_with_live_owner_retries_pending_timers_without_relaunch(self) -> None:
        saved = entry(top_level("snapshot-owner"), state=record.SessionRestoredTimersPending(
            kind="timers pending", at=STOPPED, timers=[record.PendingTimer(instance="pending", reason="failed")],
        ), timers=[
            TimerRestore(instance=name, was_enabled=True, footer=record.NoFooter(kind="no footer"))
            for name in ("done", "pending")
        ])
        saved["requested_by"] = record.FromSnapshot(kind="snapshot", taken_at=STOPPED)
        self.create([saved])
        with (patch.object(restart, "_session_liveness", return_value=restart.SessionLive()),
              patch.object(settle, "run_notifier") as timers):
            self.assertEqual(self.run_restarts(live=frozenset({"snapshot-owner"})), (0, []))
        timers.assert_called_once_with("start", "pending")
        self.assertIsNone(record.find_session(LOGIN, "snapshot-owner"))
        history = record.restarted_history()
        self.assertEqual([item["session"]["session_id"] for item in history], ["snapshot-owner"])
        self.assertEqual(history[0]["progress"]["kind"], "restarted")

    def test_unreadable_registry_preserves_pending_timer_progress(self) -> None:
        saved = entry(top_level("owner"), state=record.SessionRestoredTimersPending(kind="timers pending", at=STOPPED, timers=[record.PendingTimer(instance="pending", reason="failed")]))
        self.create([saved])
        saved["trail"] = [record.ProgressStep(at=STOPPED, kind="timers pending", detail="pending: failed")]
        _ = self.enterContext(patch.object(record, "datetime", RestartAttemptTime))
        with patch.object(restart, "_session_liveness", return_value=restart.SessionLivenessUnknown("unreadable")), patch.object(settle, "run_notifier") as timers:
            self.assertEqual(self.run_restarts(), (1, []))
        timers.assert_not_called()
        self.assertEqual(record.find_session(LOGIN, "owner"), {
            **saved, "trail": [
                *saved["trail"],
                record.ProgressStep(at=RESTARTED, kind="restarting", detail=""),
                record.ProgressStep(at=RESTARTED, kind="timers pending", detail="pending: failed"),
            ],
        })

    def test_failed_launcher_with_unknown_registry_preserves_prior_progress(self) -> None:
        saved = entry(top_level("owner", host=tmux_host("owner")))
        self.create([saved])
        saved["trail"] = [record.ProgressStep(at=STOPPED, kind="stopped", detail="")]
        _ = self.enterContext(patch.object(record, "datetime", RestartAttemptTime))
        with patch.object(restart, "_retire"), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "_launch_session", side_effect=OSError("failed")), patch.object(restart, "_session_liveness", return_value=restart.SessionLivenessUnknown("unreadable")), redirect_stdout(io.StringIO()):
            self.assertEqual(restart.relaunch_session(saved, dry_run=False, desktop_state=self.desktop_state), {
                **saved, "trail": [
                    *saved["trail"],
                    record.ProgressStep(at=RESTARTED, kind="restarting", detail=""),
                ],
            })
        self.assertEqual(record.find_session(LOGIN, "owner"), {
            **saved, "trail": [
                *saved["trail"],
                record.ProgressStep(at=RESTARTED, kind="restarting", detail=""),
                record.ProgressStep(at=RESTARTED, kind="stopped", detail=""),
            ],
        })

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
        with patch.object(restart, "_session_liveness", return_value=restart.SessionLive()):
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
            "alpha: from shutdown record", "manual: from shutdown record", "zeta: from shutdown record",
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
            entry(top_level("c-native", host=inventory.HerdrHost(kind="herdr", herdr_session="c-native", pane_id="w1:p1", presentation="attached window"))),
            entry(top_level("d-terminal", host=inventory.TerminalHost(kind="terminal"))),
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

        with patch.object(restart, "_herdr_launch"), patch.object(restart, "_retire"), patch.object(restart, "_record_scheduled_restart_prompt"), patch.object(restart, "_run", run), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "wait_for_confirmation", side_effect=self.all_sessions_live), patch.object(time, "sleep", sleep), redirect_stdout(io.StringIO()):
            self.assertEqual(restart.up(LOGIN), 1 if sys.platform == "linux" else 0)
        self.assertEqual(events, ["a-ghostty", "delay", "b-zed", "delay", "c-native"] if sys.platform == "linux" else
            ["a-ghostty", "delay", "b-zed", "delay", "c-native", "delay", "d-terminal"])

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
        stored = self.stored_record()
        progress = stored["progress"]
        assert progress["kind"] == "restart failed"
        expected = (f"in a terminal on natedev: ~/.claude/scripts/lib/py "
            f"~/.claude/scripts/shutdown/shutdown.py restart {LOGIN} --here --only owner")
        self.assertIn(expected, progress["reason"])
        status_lines = settle.status_record_lines(stored)
        output = io.StringIO()
        with (
            patch.object(settle, "run_remote", return_value=(0, "[]")),
            patch.object(restart, "_live_session_ids", return_value=frozenset()),
            patch.object(settle, "alert_user"),
            redirect_stdout(output),
        ):
            settle.report_stop([stored])
        command_lines = [line for line in [*status_lines, *output.getvalue().splitlines()]
            if "shutdown.py" in line or "/shutdown" in line]
        self.assertTrue(command_lines)
        for line in command_lines:
            self.assertIn("in a terminal on natedev", line)
            self.assertIn(LOGIN, line)
            self.assertIn("--only owner", line)
            self.assertIn("~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py", line)

    def test_unreachable_peer_restart_prints_scoped_retry_in_peer_terminal(self) -> None:
        cases = (
            ((), ["restart", LOGIN, "--here", "--no-alert"]),
            (("local", "peer-one", "peer-two"),
                ["restart", LOGIN, "--here", "--only", "peer-one,peer-two", "--no-alert"]),
        )
        local = restart.MachineRestartResult("natedev", LABEL, "nothing",
            local_session_ids=frozenset({"local"}))
        for ids, expected in cases:
            with self.subTest(ids=ids):
                output = io.StringIO()
                with (
                    patch.object(restart, "named_claude_account", return_value=self.account),
                    patch.object(restart, "_up", return_value=local),
                    patch.object(restart, "run_remote", return_value=(255, "")),
                    patch.object(restart, "other_machine", return_value="Mac"),
                    redirect_stdout(output),
                ):
                    _ = restart.restart(restart.AccountNamed(LOGIN), dry_run=False,
                        alerts=restart.NeverAlert(), only=ids)
                line = next(line for line in output.getvalue().splitlines() if "restart incomplete" in line)
                command = line.split("in a terminal on Mac: ", 1)[1]
                self.assertEqual(shlex.split(command), ["~/.claude/scripts/lib/py",
                    "~/.claude/scripts/shutdown/shutdown.py", *expected])

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
        peer.assert_called_once_with(["up", LOGIN, "--here", "--json", "--no-alert", "--dry-run", "--only", "remote-id"], limit={"kind": "while link alive"})
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
        with patch.object(restart, "_session_liveness", return_value=restart.SessionLive()), patch.object(restart, "_print_result") as summary, redirect_stdout(output):
            result = restart._up(LOGIN, False, ["waiting"])  # pyright: ignore[reportPrivateUsage]
        self.assertEqual((result.restarted, result.workers, result.manual, result.failed), (0, 0, 0, 0))
        self.assertEqual(result.failed_sessions, ())
        self.assertEqual(output.getvalue(), "Waiting: from shutdown record\nWaiting: already running; skipped\nWaiting is waiting, not stopped; nothing to restart\n")
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
        with patch.object(restart, "_session_liveness", return_value=restart.SessionLive()), patch.object(restart, "_print_result") as summary, patch.object(restart, "_launch_session") as launch, redirect_stdout(output):
            result = restart._up(LOGIN, False, ["held"])  # pyright: ignore[reportPrivateUsage]
        self.assertEqual((result.restarted, result.workers, result.manual, result.failed), (0, 0, 0, 0))
        self.assertEqual(result.failed_sessions, ())
        self.assertEqual(output.getvalue(), "Held: from shutdown record\nHeld: already running; skipped\nHeld is held by usage limit, not stopped; nothing to restart\n")
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

    def test_selected_reaping_session_is_refused_without_launch_or_record_change(self) -> None:
        saved = entry(top_level("reaping", name="Reaping owner"), state=record.SessionReaping(
            kind="reaping", at=RESTARTED, because="account out of usage", detail="weekly limit"))
        self.create([saved])
        before = {path.relative_to(self.state_root): path.read_bytes()
                  for path in self.state_root.rglob("*") if path.is_file()}
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run), patch.object(restart, "_session_liveness", return_value=restart.SessionLive()), patch.object(restart, "_launch_session") as launch, redirect_stdout(io.StringIO()) as output:
                result = restart._up(LOGIN, dry_run, ["reaping"])  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(result.exit_status, 1)
            self.assertEqual(result.refused, [
                {"kind": "not stopped", "session_id": "reaping", "name": "Reaping owner", "progress": "reaping"},
            ])
            self.assertEqual((result.restarted, result.failed), (0, 0))
            self.assertEqual(output.getvalue(), "Reaping owner: from shutdown record\nReaping owner: already running; skipped\nReaping owner is reaping, not stopped; nothing to restart\n")
            launch.assert_not_called()
            self.assertEqual({path.relative_to(self.state_root): path.read_bytes()
                              for path in self.state_root.rglob("*") if path.is_file()}, before)

    def test_peer_reaping_refusal_parses_and_exits_one_without_alert(self) -> None:
        refusal = restart.RestartNotStopped(kind="not stopped", session_id="peer-id", name="Peer", progress="reaping")
        line = "Peer is reaping, not stopped; nothing to restart"
        response = peer_json(outcome="nothing", lines=[line], refused=[refusal])
        self.assertEqual(restart.parse_peer_result(response)["refused"], [refusal])
        with patch.object(restart, "named_claude_account", return_value=self.account), patch.object(restart, "run_remote", return_value=(1, response)) as peer, patch.object(settle, "alert_user") as alert, patch.object(restart, "_print_result") as summary, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(restart.restart(restart.AccountNamed(LABEL), only=["peer-id"], dry_run=False, alerts=restart.AlertWhenNotBack()), 1)
        self.assertEqual(output.getvalue(), line + "\n")
        peer.assert_called_once_with(["up", LOGIN, "--here", "--json", "--only", "peer-id"], limit={"kind": "while link alive"})
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

    def test_resume_parts_preserve_probe_marker_only_for_probe_records(self) -> None:
        for probe in (False, True):
            with self.subTest(probe=probe):
                saved = entry(top_level("probe-owner", host=tmux_host("probe-owner")))
                saved["probe"] = probe
                parts = restart._resume_parts(saved["session"], "continue", probe=saved["probe"])  # pyright: ignore[reportPrivateUsage]
                if probe:
                    self.assertEqual(parts.environment["CLAUDE_PROBE_SESSION"], "1")
                else:
                    self.assertNotIn("CLAUDE_PROBE_SESSION", parts.environment)

    def test_tmux_unit_restore_carries_probe_policy_through_the_clean_subprocess(self) -> None:
        for probe in (False, True):
            for target in ("", "claude3"):
                with self.subTest(probe=probe, target=target):
                    self.configure_groups(units=target or "claude2")
                    saved = entry(unit("probe-unit"))
                    saved["probe"] = probe
                    if target:
                        saved["target"] = target
                    with (patch.dict(os.environ, {"CLAUDE_PROBE_SESSION": "1"}),
                          patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as launch):
                        result = restart._launch_session(saved, "continue", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
                    self.assertIsInstance(result, restart.SessionLaunched)
                    launch.assert_called_once()
                    argv = cast(list[str], launch.call_args.args[0])
                    self.assertEqual(argv[argv.index("--resume") + 1], saved["session"]["session_id"])
                    self.assertEqual("--probe" in argv, probe)
                    environment = cast(dict[str, str], launch.call_args.kwargs["env"])
                    self.assertNotIn("CLAUDE_PROBE_SESSION", environment)
                    if target:
                        self.assertEqual(argv[argv.index("--account") + 1], target)

    def test_probe_record_reaches_manual_restart_and_move_commands(self) -> None:
        for target in ("", "claude3"):
            with self.subTest(target=target):
                saved = entry(top_level("probe-manual"))
                saved["probe"] = True
                if target:
                    saved["target"] = target
                result = restart._launch_session(saved, "continue", self.desktop_state, dry_run=False)  # pyright: ignore[reportPrivateUsage]
                self.assertIsInstance(result, restart.ManualRestartRequired)
                assert isinstance(result, restart.ManualRestartRequired)
                self.assertIn("CLAUDE_PROBE_SESSION=1", result.command)
                if target:
                    self.assertIn("exec claude3", result.command)

    def test_old_shutdown_record_without_probe_loads_as_non_probe(self) -> None:
        saved = cast(dict[str, object], json.loads(json.dumps(entry(top_level("old-owner")))))
        _ = saved.pop("probe", None)
        restored = record.parse_session_records(json.dumps([saved]))
        self.assertEqual(len(restored), 1)
        self.assertFalse(restored[0]["probe"])

    def test_remote_control_missing_archives_restart_and_launches_fixer_without_push(self) -> None:
        for policy in ("user", "none"):
            for command_alerts in (restart.AlertWhenNotBack(), restart.NeverAlert()):
                with self.subTest(policy=policy, command_alerts=type(command_alerts).__name__):
                    saved = entry(top_level("owner", name="Owner", host=tmux_host("owner")))
                    saved["alerts"] = policy
                    saved["requested_at"] = "2026-10-09T21:49:" + str(10 + (2 if policy == "none" else 0) + int(isinstance(command_alerts, restart.NeverAlert))) + "+00:00"
                    self.create([saved])
                    output = io.StringIO()
                    with patch.object(restart, "named_claude_account", return_value=self.account), patch.object(restart, "_retire"), patch.object(restart, "_launch_session", return_value=restart.SessionLaunched()), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "wait_for_confirmation", return_value=restart.RegistryConfirmation((sessions.SessionRecord(pid=912, sessionId="owner", name="Owner", procStart="resumed-start", messagingSocketPath="", updatedAt=1, tmux="", formerNames=[]),))), patch.object(remote_control, "launch_fixer") as fixer, patch.object(restart, "wait_for_remote_control", return_value=frozenset()) as check, patch.object(restart, "_send_restart_note", return_value=settle.MessageSent(kind="sent")), patch.object(settle, "alert_user") as alert, redirect_stdout(output):
                        self.assertEqual(restart.restart(restart.AccountNamed(LOGIN), dry_run=False, alerts=command_alerts, here=True), 0)
                    self.assertNotIn("Remote Control", output.getvalue())
                    check.assert_called_once()
                    self.assertIsNone(record.find_session(LOGIN, "owner"))
                    history = list((self.state_root / LOGIN / "history").glob("*.json"))
                    self.assertEqual(cast(dict[str, object], json.loads(history[-1].read_text()))["progress"], {"kind": "restarted", "at": RESTARTED})
                    alert.assert_not_called()
                    fixer.assert_called_once_with("owner", 912, "resumed-start", "Owner", "Owner")

    def test_remote_control_fixer_launch_has_no_alert_option_under_any_policy(self) -> None:
        self.allow_leaf(ShutdownSendLeaf.LAUNCH_REMOTE_CONTROL_FIXER)
        saved = entry(unit("owner"))
        saved["session"]["name"] = "Owner"
        confirmed = restart.RegistryConfirmation((sessions.SessionRecord(pid=912,
            sessionId="owner", name="Owner", procStart="resumed-start",
            messagingSocketPath="", updatedAt=1, tmux="", formerNames=[]),))
        for policy in ("user", "none"):
            with self.subTest(policy=policy):
                saved["alerts"] = policy
                with (patch.object(restart, "wait_for_remote_control", return_value=frozenset()),
                      patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as launch,
                      patch.object(settle, "alert_user") as alert, redirect_stdout(io.StringIO())):
                    _ = restart.remote_control_missing([saved], confirmed)
                launch.assert_called_once()
                argv = cast(list[str], launch.call_args.args[0])
                self.assertNotIn("--alerts", argv)
                self.assertEqual(argv[argv.index("--pid") + 1], "912")
                self.assertEqual(argv[argv.index("--who") + 1], "owner unit")
                self.assertEqual(argv[argv.index("--session") + 1], "Owner")
                self.assertEqual(argv[argv.index("--proc-start") + 1], "resumed-start")
                alert.assert_not_called()

    def test_remote_control_launch_failure_is_quiet_under_every_alert_policy(self) -> None:
        saved = entry(unit("owner"))
        saved["session"]["name"] = "Owner"
        confirmed = restart.RegistryConfirmation((sessions.SessionRecord(pid=912,
            sessionId="owner", name="Owner", procStart="resumed-start",
            messagingSocketPath="", updatedAt=1, tmux="", formerNames=[]),))
        for policy in ("user", "none"):
            with self.subTest(policy=policy):
                saved["alerts"] = policy
                output = io.StringIO()
                with (patch.object(restart, "wait_for_remote_control", return_value=frozenset()),
                      patch.object(remote_control, "launch_fixer", side_effect=OSError("service unavailable")) as launch,
                      patch.object(syslog, "syslog") as journal,
                      patch.object(settle, "alert_user") as alert, redirect_stdout(output)):
                    _ = restart.remote_control_missing([saved], confirmed)
                launch.assert_called_once_with("owner", 912, "resumed-start", "owner unit", "Owner")
                journal.assert_called_once()
                self.assertIn("service unavailable", cast(str, journal.call_args.args[1]))
                self.assertEqual(output.getvalue(), "")
                alert.assert_not_called()

    def test_remote_control_active_restart_sends_no_alert(self) -> None:
        saved = entry(top_level("owner", name="Owner", host=tmux_host("owner")))
        self.create([saved])
        with patch.object(restart, "named_claude_account", return_value=self.account), patch.object(restart, "_retire"), patch.object(restart, "_launch_session", return_value=restart.SessionLaunched()), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "wait_for_confirmation", side_effect=self.all_sessions_live), patch.object(restart, "_send_restart_note", return_value=settle.MessageSent(kind="sent")), patch.object(settle, "alert_user") as alert, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(restart.restart(restart.AccountNamed(LOGIN), dry_run=False, alerts=restart.AlertWhenNotBack(), here=True), 0)
        self.assertNotIn("no Remote Control", output.getvalue())
        self.assertIsNone(record.find_session(LOGIN, "owner"))
        alert.assert_not_called()

    def test_legacy_peer_remote_control_entries_start_no_local_fixer_or_push(self) -> None:
        for policy in ("user", "none"):
            with self.subTest(policy=policy):
                missing = LegacyPeerRemoteControl(session_id="peer", name="Remote", alerts=policy)
                with patch.object(restart, "named_claude_account", return_value=self.account), patch.object(restart, "run_remote", return_value=(0, peer_json(missing=[missing]))), patch.object(settle, "alert_user") as alert, patch.object(remote_control, "launch_fixer") as fixer, redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(restart.restart(restart.AccountNamed(LOGIN), dry_run=False, alerts=restart.AlertWhenNotBack()), 0)
                self.assertNotIn("Remote Control", output.getvalue())
                alert.assert_not_called()
                fixer.assert_not_called()

    def test_peer_parser_ignores_missing_empty_and_legacy_remote_control_entries(self) -> None:
        baseline = cast(dict[str, object], json.loads(peer_json()))
        del baseline["no_remote_control"]
        expected = restart.parse_peer_result(json.dumps(baseline))
        for legacy in ([], [{"session_id": "peer", "name": "Remote", "alerts": "user"}],
                       [{"session_id": "peer", "name": "Remote", "alerts": "none"}], "unread old extension"):
            with self.subTest(legacy=legacy):
                parsed = restart.parse_peer_result(json.dumps({**baseline, "no_remote_control": legacy}))
                self.assertEqual(parsed, expected)

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
        self.assertEqual(cast(RenderedPush, alert.call_args.args[0]).values["stopped"], "One session")
        self.assertNotIn("Waiting", cast(RenderedPush, alert.call_args.args[0]).body)

    def test_peer_result_refuses_old_output_and_invalid_nested_fields(self) -> None:
        valid = cast(dict[str, object], json.loads(peer_json()))
        invalid: list[str] = ["Mac: restarted", "[]", "{}"]
        for key, value in (("outcome", []), ("lines", [3]), ("refused", [{"kind": "not stopped", "session_id": "x", "name": "X", "progress": "stopped"}])):
            invalid.append(json.dumps({**valid, key: value}))
        for response in invalid:
            with self.subTest(response=response), self.assertRaises(ValueError):
                _ = restart.parse_peer_result(response)



if __name__ == "__main__":
    _ = unittest.main()
