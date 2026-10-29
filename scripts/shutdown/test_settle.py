"""Behavioral tests for the account shutdown settle phase."""

from __future__ import annotations

import copy
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait as wait_futures
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from collections.abc import Callable, Generator, Sequence
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict, cast, final, override
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_support import registry_confirmation, ShutdownSendLeaf, ShutdownTestCase
from launch_permission import NewWorkLaunchPurpose
import inventory
import herdr
from fake_herdr import CommandResponse
import record
import sessions
import remote_control
from user_action import ActionRequired
from push_texts import RenderedPush, render, catalog, render_entry
import restart as restart_work
import settle
import detached
import shutdown
if TYPE_CHECKING:
    from ..notify import review
else:
    import review
import stop as stop_work
# The module settle imports by its own path entry: a separate object from the package import below.
import showrunners as settle_showrunners
from scripts.accounts import accounts, moves, outage_alerts
import usage_store
from scripts.production import showrunners
from account import Account


LOGIN = "owner@example.com"
LABEL = "claude2"
NOW = "2026-10-09T21:49:10+00:00"
NOW_UTC = datetime(2026, 10, 9, 21, 49, 10, tzinfo=timezone.utc)


def setUpModule() -> None:
    _ = unittest.enterModuleContext(patch.object(herdr, "security_session", return_value=herdr.NotMacSession()))
    root = Path(unittest.enterModuleContext(tempfile.TemporaryDirectory()))
    _ = unittest.enterModuleContext(patch.object(showrunners, "NOTIFIER_STATE_DIR", root))
    _ = unittest.enterModuleContext(patch.object(settle_showrunners, "NOTIFIER_STATE_DIR", root))


class SentCall(TypedDict):
    args: list[str]
    text: str


def checkout(*, ahead: int = 0, path: str = "/tmp/checkout") -> inventory.GitCheckout:
    return inventory.GitCheckout(
        kind="git",
        path=path,
        head=inventory.OnBranch(kind="branch", name="work"),
        upstream=inventory.Tracking(kind="tracking", ahead=ahead),
        dirty=[],
    )


def common_session(
    session_id: str,
    name: str,
    *,
    status: str = "idle",
    ahead: int = 0,
    timers: list[str] | None = None,
    busy_workers: list[str] | None = None,
) -> inventory.SessionFields:
    return {
        "session_id": session_id,
        "pid": 10_000 + sum(ord(character) for character in session_id),
        "proc_start": f"start-{session_id}",
        "name": name,
        "cwd": f"/tmp/{session_id}",
        "status": status,
        "updated_at": 0,
        "probe": False,
        "model": inventory.NoReplyYet(kind="no reply yet"),
        "checkout": checkout(ahead=ahead, path=f"/tmp/{session_id}"),
        "run_dirs": [],
        "codex_servers": (
            [
                inventory.CodexServer(
                    run_dir=f"/tmp/run-{session_id}",
                    pid=20_000,
                    busy_workers=list(busy_workers or []),
                )
            ]
            if busy_workers
            else []
        ),
        "timers": list(timers or []),
    }


def top_level(
    session_id: str,
    name: str,
    *,
    status: str = "idle",
    ahead: int = 0,
    timers: list[str] | None = None,
    busy_workers: list[str] | None = None,
) -> inventory.TopLevelSession:
    return inventory.TopLevelSession(
        **common_session(
            session_id,
            name,
            status=status,
            ahead=ahead,
            timers=timers,
            busy_workers=busy_workers,
        ),
        kind="top-level",
        host=inventory.UnknownHost(kind="unknown"),
    )


def unit(
    session_id: str,
    name: str,
    *,
    production: str = "demo",
    status: str = "idle",
    ahead: int = 0,
) -> inventory.UnitSession:
    return inventory.UnitSession(
        **common_session(session_id, name, status=status, ahead=ahead),
        kind="unit",
        host=inventory.UnitHost(
            kind="unit",
            production=production,
            unit=name,
            doc=f"/tmp/{production}.md",
            session_name=name,
            session_host="herdr",
            plan=inventory.NoRunRecord(kind="no run record"),
        ),
    )


def showrunner(
    session_id: str,
    name: str,
    *,
    production: str = "demo",
) -> inventory.ShowrunnerSession:
    return inventory.ShowrunnerSession(
        **common_session(session_id, name),
        kind="showrunner",
        host=inventory.HerdrHost(kind="herdr", herdr_session=name, pane_id="w1:p1", presentation="detached"),
        production=production,
        doc=f"/tmp/{production}.md",
    )


def worker_session(
    session_id: str,
    name: str,
    *,
    owner: str | None,
    status: str = "idle",
) -> inventory.WorkerSession:
    worker_owner: inventory.WorkerOwner = (
        inventory.DirectorOwner(kind="director", session_id=owner)
        if owner is not None
        else inventory.NoDirector(kind="no director")
    )
    return inventory.WorkerSession(
        **common_session(session_id, name, status=status),
        kind="worker",
        host=inventory.UnknownHost(kind="unknown"),
        owner=worker_owner,
    )


def machine_inventory(
    sessions: list[inventory.Session],
    *,
    machine: str = "natedev",
    unattributed: list[inventory.UnattributedSession] | None = None,
) -> inventory.Inventory:
    return inventory.Inventory(
        machine=machine,
        login=LOGIN,
        label=LABEL,
        sessions=sessions,
        unattributed=list(unattributed or []),
    )



def session_shutdown(session: inventory.Session, *, progress: record.SessionProgress | None = None,
                     request_id: str = "request-one", requester: str = "", then: record.ShutdownContinuation = "stop") -> record.SessionShutdown:
    return record.SessionShutdown(
        login=LOGIN, label=LABEL, machine="natedev", requested_at=NOW, request_id=request_id,
        requested_by={"kind": "session", "session_id": requester} if requester else {"kind": "terminal"},
        hold="session", then=then, force="wait for ready", alerts="user", probe=False, session=session,
        workers=[], timers=[], settle_message={"kind": "not sent"}, where={"kind": "not said"},
        progress=progress or {"kind": "waiting"}, trail=[], stop_issues=[],
    )


class StopFake(Protocol):
    def __call__(self, saved: record.SessionShutdown, *,
                 idle_reason: stop_work.IdleStopReason = "not an idle stop",
                 forced: bool = False) -> record.SessionShutdown: ...


def ignoring_idle_reason(action: Callable[[record.SessionShutdown], record.SessionShutdown]) -> StopFake:
    def perform(saved: record.SessionShutdown, *,
                idle_reason: stop_work.IdleStopReason = "not an idle stop",
                forced: bool = False) -> record.SessionShutdown:
        del idle_reason, forced
        return action(saved)
    return perform


def await_ready_mark(saved: record.SessionShutdown) -> None:
    """Keep an idle fixture waiting until the test supplies its ready mark."""
    saved["settle_message"] = {"kind": "sent", "at": NOW}
    saved["session"]["updated_at"] = int(NOW_UTC.timestamp() * 1000) - 1


def unchanged(saved: record.SessionShutdown) -> record.SessionShutdown:
    return saved


class PassObserved(Exception):
    """The test has observed one complete conducting pass."""


class ConsumptionObservedFuture[T](Future[T]):
    """Expose whether the conductor consumed a completed action's result."""

    consumed: bool = False

    @override
    def result(self, timeout: float | None = None) -> T:
        try:
            return super().result(timeout)
        finally:
            self.consumed = True


@final
class SettleTests(ShutdownTestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(patch.object(sessions, "remote_control", return_value="active"))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        context = cast(object, self.enterContext(patch.dict(os.environ, {"HOME": str(self.root), "SHUTDOWN_STATE_DIR": str(self.root / "state"), "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions")})))
        del context
        _ = self.enterContext(patch.object(settle, "now_utc", return_value=NOW_UTC))
        _ = self.enterContext(patch.object(settle, "merge_in_progress", return_value=False))
        _ = self.enterContext(patch.object(settle, "_showing_form", return_value=False))
        # Fixture folders do not exist; a restore whose folder is gone is dropped before launch.
        _ = self.enterContext(patch.object(restart_work, "_folder_exists", return_value=True))

    def save(self, saved: record.SessionShutdown) -> None:
        self.assertTrue(record.create_session(saved))

    def test_terminal_down_follows_started_request_and_returns_follow_outcome(self) -> None:
        owner = top_level("follow-owner", "Owner")
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}),
            patch.object(settle, "_warm", return_value=True),
            patch.object(settle, "_launch_conductor") as launch,
            patch.object(shutdown, "follow", return_value=130) as follow,
            patch.object(output, "isatty", return_value=True),
            redirect_stdout(output),
        ):
            status = settle.down(Account("claude", LOGIN, LABEL), here=True, only=[owner["session_id"]],
                request_id="terminal-request", report=machine_inventory([owner]))
        self.assertEqual(status, 130)
        launch.assert_called_once_with(LOGIN, LABEL)
        follow.assert_called_once_with(LOGIN, "terminal-request")
        self.assertNotIn("To follow it", output.getvalue())
        self.assertEqual(record.session_records(LOGIN)[0]["request_id"], "terminal-request")

    def test_non_tty_or_session_down_prints_scoped_follow_and_cancel_commands(self) -> None:
        for number, (origin, tty, target) in enumerate((("", False, ""), ("requester", True, ""), ("", False, "claude 3"))):
            with self.subTest(origin=origin, tty=tty, target=target):
                owner = top_level(f"closing-owner-{number}", "Owner")
                output = io.StringIO()
                with (
                    patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}),
                    patch.object(settle, "_warm", return_value=True),
                    patch.object(settle, "_launch_conductor"),
                    patch.object(shutdown, "follow", side_effect=AssertionError("closing line starts follow")),
                    patch.object(output, "isatty", return_value=tty),
                    redirect_stdout(output),
                ):
                    status = settle.down(Account("claude", LOGIN, LABEL), here=True, only=[owner["session_id"]],
                        requested_by=origin, request_id="closing-request", target=target,
                        then="restart" if target else "stop", report=machine_inventory([owner]))
                self.assertEqual(status, 0)
                text = output.getvalue()
                self.assertIn("started for 1 sessions", text)
                self.assertIn(f"follow {LOGIN} --request closing-request", text)
                self.assertIn(f"cancel {LOGIN} --here --only {owner["session_id"]}", text)
                self.assertIn("in a terminal on natedev", text)
                self.assertIn("~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py", text)
                self.assertNotIn("undo", text.casefold())
                if target:
                    self.assertIn("stop the move of the ones not yet stopped", text)
                record.archive_session(LOGIN, owner["session_id"])

    def test_ghostty_holdouts_push_once_for_each_requester_without_a_form_but_not_reaping(self) -> None:
        owners: list[inventory.Session] = []
        identities: dict[str, str] = {}
        for identity, requester in (("terminal-wait", ""), ("session-wait", "requester"),
                                    ("reaping-owner", ""), ("ready-owner", "")):
            owner = top_level(identity, f"Display {identity}", status="busy" if identity == "ready-owner" else "idle")
            if identity.endswith("-wait"):
                identities[owner["name"]] = identity
            owner["host"] = inventory.WindowHost(kind="ghostty", window_shell=123,
                desktop=inventory.NamedDesktop(kind="named", name="Writing"))
            owners.append(owner)
            progress: record.SessionProgress = record.SessionWaiting(kind="waiting")
            if identity == "reaping-owner":
                progress = record.SessionReaping(kind="reaping", at=NOW,
                    because="account out of usage", detail="weekly limit")
            elif identity == "ready-owner":
                progress = record.SessionReadyToStop(kind="ready", at=NOW)
            saved = session_shutdown(owner, requester=requester, progress=progress,
                then="restart" if identity == "session-wait" else "stop")
            saved["settle_message"] = record.SettleMessageSent(kind="sent", at=NOW)
            self.save(saved)
        def end(_seconds: float) -> None:
            raise PassObserved()
        with (
            patch.object(settle, "now_utc", return_value=NOW_UTC + timedelta(minutes=5)),
            patch.object(settle, "run_inventory", return_value=machine_inventory(owners)),
            patch.object(settle, "_showing_form", return_value=False),
            patch.object(settle, "_send_settle_message"),
            patch.object(stop_work, "stop_session", side_effect=ignoring_idle_reason(self.fake_stop)),
            patch.object(settle, "report_stop"),
            patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")),
            patch.object(settle, "alert_user", return_value=True) as alert,
            patch.object(time, "sleep", side_effect=end),
        ):
            for _ in range(2):
                with self.assertRaises(PassObserved):
                    _ = settle.conduct(LOGIN)
        self.assertEqual(alert.call_count, 2)
        ready_owner = record.find_session(LOGIN, "ready-owner")
        assert ready_owner is not None
        self.assertNotIn("alerted_at", ready_owner)
        pushes = [cast(RenderedPush, call.args[0]) for call in alert.call_args_list]
        self.assertEqual({push.values["session"] for push in pushes}, set(identities))
        for push in pushes:
            self.assertEqual(push.id, "shutdown.holdout_unanswered")
            self.assertIn(f"Session: {push.values['session']}\n", push.message)
            self.assertIn("is stuck", push.title)
            self.assertIn(push.values["command"], push.body)
            combined = push.body + " " + cast(ActionRequired, push.action).text
            self.assertIn("in a terminal on natedev,", combined.casefold())
            self.assertIn(f"now {LABEL} --here --only {identities[push.values['session']]}", combined)
            self.assertTrue(push.body.startswith(f"Your Claude {push.values['operation']} waits for each session to pause. The session {push.values['session']} has not paused, so the {push.values['operation']} is stuck.\n\n"))
            self.assertNotIn("form", push.title + "\n" + push.message)
            if identities[push.values["session"]] == "session-wait":
                self.assertIn("restart", push.title)
            else:
                self.assertIn("shutdown", push.title)

    def test_shutdown_closing_cold_key_peer_and_holdout_lines_name_command_places(self) -> None:
        owner = top_level("place-owner", "Owner")
        outputs: list[str] = []
        for warm in (False, True):
            output, errors = io.StringIO(), io.StringIO()
            with (
                patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}),
                patch.object(settle, "_warm", return_value=warm),
                patch.object(settle, "_machine", return_value="natedev"),
                patch.object(settle, "_launch_conductor"),
                patch.object(settle, "other_machine", return_value="Mac"),
                patch.object(settle, "run_remote", return_value=(255, "unreachable")),
                redirect_stdout(output), redirect_stderr(errors),
            ):
                _ = settle.down(Account("claude", LOGIN, LABEL), only=["place-owner", "peer-owner"],
                    request_id="place-request", report=machine_inventory([owner]))
            outputs.extend((output.getvalue(), errors.getvalue()))
        outputs.append(settle.holdout_line(session_shutdown(owner)))
        command_lines = [line for text in outputs for line in text.splitlines()
            if any(token in line for token in ("/shutdown", "shutdown.py", "accounts.py", "github-warm"))]
        self.assertGreaterEqual(len(command_lines), 4)
        for line in command_lines:
            with self.subTest(line=line):
                self.assertTrue(any(phrase in line.casefold() for phrase in (
                    "in this terminal", "in a terminal on ", "in any claude session on ")), line)

    def test_cold_key_retry_round_trips_original_scope_move_and_alert_policy(self) -> None:
        cases: list[tuple[bool, Literal["user", "none"], tuple[str, ...], record.ShutdownContinuation, str, str, list[str]]] = [
            (False, "user", (), "stop", "", "", ["down", LOGIN, "--also", "mac"]),
            (False, "none", (), "stop", "", "", ["down", LOGIN, "--also", "mac", "--no-alert"]),
            (False, "none", ("peer-owner",), "restart", "", "",
                ["down", LOGIN, "--only", "peer-owner", "--restart", "--no-alert"]),
            (True, "user", ("local-one", "local-two"), "restart", "", "",
                ["down", LOGIN, "--here", "--only", "local-one,local-two", "--restart"]),
        ]
        account = Account("claude", LOGIN, LABEL)
        report = machine_inventory([])
        for here, alerts, ids, then, target, why, expected in cases:
            with self.subTest(arguments=expected):
                errors = io.StringIO()
                with patch.object(settle, "_warm", return_value=False), redirect_stderr(errors):
                    self.assertEqual(settle.down(account, here, ids, then=then, alerts=alerts,
                        target=target, why=why, report=report, origin=accounts.HandOrigin(), move_id=""), 1)
                command = errors.getvalue().split("Then in a terminal on natedev: ", 1)[1].strip()
                argv = shlex.split(command)
                self.assertEqual(argv, ["~/.claude/scripts/lib/py", "~/.claude/scripts/shutdown/shutdown.py", *expected])
                with (
                    patch.object(shutdown, "named_claude_account", return_value=account),
                    patch.object(shutdown, "inventory", return_value=report),
                    patch.object(shutdown, "resolve_sessions", return_value=shutdown.ResolvedSessions(ids, report)) as resolve,
                    patch.object(settle, "down", return_value=0) as request,
                ):
                    self.assertEqual(shutdown.main(argv[2:]), 0)
                request.assert_called_once_with(account, here, ids, then=then, alerts=alerts,
                    requested_by=None, request_id="", target=target, why=why, report=report, origin=accounts.HandOrigin(), move_id="", checkout_move=None)
                if ids:
                    resolve.assert_called_once_with(LOGIN, ids, here=here)
                else:
                    resolve.assert_not_called()

    def write_refusal_transcript(self, identity: str) -> None:
        path = self.root / ".claude/projects/refusal-fixture" / f"{identity}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({"type": "assistant", "isApiErrorMessage": True,
            "error": "rate_limit", "timestamp": NOW, "uuid": "refusal-entry",
            "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "weekly limit"}]}}) + "\n")

    def write_usage(self, weekly: float, *, five: float = 50,
                    at: datetime = NOW_UTC, login: str = LOGIN) -> None:
        saved = usage_store.load()
        saved[usage_store.key("Claude", login)] = usage_store.StoredUsage(
            weekly=usage_store.StoredWindow(left=weekly, at=at.isoformat(),
                resets=(NOW_UTC + timedelta(days=2)).isoformat()),
            five_hour=usage_store.StoredWindow(left=five, at=at.isoformat(),
                resets=(NOW_UTC + timedelta(hours=3)).isoformat()))
        usage_store.write(saved)

    def test_prompt_state_refusal_outweighs_room_and_uses_own_config(self) -> None:
        self.write_usage(80)
        self.write_refusal_transcript("own-refusal")
        own_config = self.root / "own-config"
        _ = (self.root / ".claude").rename(own_config)
        subject = settle.PromptSubject("own-refusal", LOGIN, own_config)
        state = settle.prompt_state(subject, NOW_UTC)
        self.assertIsInstance(state, settle.StoppedTakingPrompts)
        if isinstance(state, settle.StoppedTakingPrompts):
            self.assertEqual(state.because, "refused in its transcript")
            self.assertIn("weekly", state.detail)
        other = settle.PromptSubject("own-refusal", LOGIN, self.root / ".claude")
        self.assertEqual(settle.prompt_state(other, NOW_UTC), settle.TakesPrompts("room"))

    def test_prompt_state_room_spent_and_stale_readings_are_account_scoped(self) -> None:
        subject = settle.PromptSubject("usage-owner", LOGIN, self.root / ".claude")
        self.assertEqual(settle.prompt_state(subject, NOW_UTC), settle.TakesPrompts("no fresh reading"))
        self.write_usage(90, login="other@example.com")
        for weekly, five in ((0, 70), (80, 0)):
            with self.subTest(weekly=weekly, five=five):
                self.write_usage(weekly, five=five)
                state = settle.prompt_state(subject, NOW_UTC)
                self.assertIsInstance(state, settle.StoppedTakingPrompts)
                if isinstance(state, settle.StoppedTakingPrompts):
                    self.assertEqual(state.because, "account out of usage")
                    self.assertIn("weekly" if weekly == 0 else "5", state.detail)
        self.write_usage(80, five=1)
        self.assertEqual(settle.prompt_state(subject, NOW_UTC), settle.TakesPrompts("room"))
        self.write_usage(0, five=0, at=NOW_UTC - timedelta(minutes=11))
        self.assertEqual(settle.prompt_state(subject, NOW_UTC), settle.TakesPrompts("no fresh reading"))

    def test_spent_account_reaps_without_pause_on_first_tick_then_restarts(self) -> None:
        owner = top_level("spent-owner", "Spent owner", status="busy")
        owner["config_dir"] = str(self.root / ".claude")
        self.write_usage(0)
        report = machine_inventory([owner])
        with patch.object(settle, "run_inventory", return_value=report):
            added = settle.begin(LOGIN, only=[owner["session_id"]], request_id="spent-request", then="restart")
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["progress"]["kind"], "reaping")
        self.assertEqual(added[0]["settle_message"], {"kind": "not sent"})
        self.assertIsInstance(settle.next_step(added[0], self.matching(added[0]), [], NOW_UTC), settle.StopSession)
        stops: list[str] = []
        def stop_once(saved: record.SessionShutdown) -> record.SessionShutdown:
            self.assertEqual(saved["progress"]["kind"], "reaping")
            stops.append(saved["session"]["session_id"])
            return self.fake_stop(saved)
        with (patch.object(settle, "run_inventory", return_value=report),
              patch.object(stop_work, "stop_session", side_effect=ignoring_idle_reason(stop_once)),
              patch.object(restart_work, "relaunch_session", side_effect=self.fake_launch) as launch,
              patch.object(restart_work, "wait_for_confirmation", side_effect=self.all_live),
              patch.object(restart_work, "confirm_entry", side_effect=self.fake_confirm),
              patch.object(settle, "send_message") as send,
              patch.object(settle, "report_stop")):
            self.assertEqual(settle.conduct(LOGIN), 0)
        self.assertEqual(stops, [owner["session_id"]])
        launch.assert_called_once()
        send.assert_not_called()
        history = list((self.root / "state" / LOGIN / "history").glob("*.json"))
        self.assertEqual(len(history), 1)
        archived = record.parse_session_records("[" + history[0].read_text() + "]")[0]
        self.assertEqual(archived["progress"]["kind"], "restarted")

    def test_account_with_room_sends_pause_and_waits_until_ready(self) -> None:
        owner = top_level("room-owner", "Room owner")
        owner["config_dir"] = str(self.root / ".claude")
        self.write_usage(70)
        report = machine_inventory([owner])
        with patch.object(settle, "run_inventory", return_value=report):
            saved = settle.begin(LOGIN, only=[owner["session_id"]], request_id="room-request")[0]
        self.assertEqual(saved["force"], "wait for ready")
        self.assertEqual(saved["progress"]["kind"], "waiting")
        with patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")) as send:
            settle._send_settle_message(saved)  # pyright: ignore[reportPrivateUsage]
        send.assert_called_once()
        current = record.find_session(LOGIN, owner["session_id"])
        assert current is not None
        self.assertEqual(current["settle_message"]["kind"], "sent")
        self.assertIsInstance(settle.next_step(current, self.matching(current), [], NOW_UTC), settle.Hold)
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": owner["session_id"]}), patch.object(settle, "run_inventory", return_value=report), patch.object(settle, "_launch_conductor"):
            self.assertEqual(settle.ready("saved and pushed"), 0)
        current = record.find_session(LOGIN, owner["session_id"])
        assert current is not None
        self.assertIsInstance(settle.next_step(current, self.matching(current), [], NOW_UTC), settle.StopSession)

    def test_ghostty_refusal_after_pause_reaps_in_one_tick_without_reask(self) -> None:
        owner = top_level("late-refusal", "Late refusal", status="busy")
        owner["config_dir"] = str(self.root / ".claude")
        owner["host"] = inventory.WindowHost(kind="ghostty", window_shell=123,
            desktop=inventory.DesktopNotInSnapshot(kind="not in snapshot"))
        self.write_refusal_transcript(owner["session_id"])
        saved = session_shutdown(owner)
        saved["settle_message"] = record.SettleMessageSent(kind="sent",
            at=(NOW_UTC - timedelta(minutes=3)).isoformat())
        self.save(saved)
        observed: list[str] = []
        def stop_refused(current: record.SessionShutdown) -> record.SessionShutdown:
            self.assertEqual(current["progress"]["kind"], "reaping")
            observed.append(current["session"]["session_id"])
            return self.fake_stop(current)
        with (patch.object(settle, "run_inventory", return_value=machine_inventory([owner])) as read,
              patch.object(stop_work, "stop_session", side_effect=ignoring_idle_reason(stop_refused)),
              patch.object(settle, "send_message") as send,
              patch.object(settle, "report_stop")):
            self.assertEqual(settle.conduct(LOGIN), 0)
        read.assert_called_once()
        self.assertEqual(observed, [owner["session_id"]])
        send.assert_not_called()

    def test_conductor_skips_owner_withdrawn_during_refusal_scan_and_stops_other_owner(self) -> None:
        from scripts.accounts import refusal_watch

        withdrawn = session_shutdown(top_level("withdrawn-owner", "A withdrawn owner", status="busy"))
        withdrawn["session"]["config_dir"] = str(self.root / ".claude")
        withdrawn["settle_message"] = record.SettleMessageSent(kind="sent",
            at=(NOW_UTC - timedelta(seconds=1)).isoformat())
        ready = session_shutdown(top_level("ready-owner", "Z ready owner"),
            progress={"kind": "ready", "at": NOW})
        self.save(withdrawn)
        self.save(ready)
        self.write_refusal_transcript(withdrawn["session"]["session_id"])
        read_refusal = refusal_watch.transcript_refusal

        def withdraw_after_read(identity: str, *, config_dir: Path | None = None) -> refusal_watch.TranscriptRefusal | refusal_watch.NoSessionRefusal:
            refusal = read_refusal(identity, config_dir=config_dir)
            self.assertIsInstance(refusal, refusal_watch.TranscriptRefusal)
            _ = record.withdraw_session(LOGIN, identity, lambda _saved: None)
            return refusal

        with (patch.object(refusal_watch, "transcript_refusal", side_effect=withdraw_after_read) as scan,
              patch.object(settle, "_usage_limit_shown", return_value=False),
              patch.object(settle, "run_inventory", return_value=machine_inventory([withdrawn["session"], ready["session"]])),
              patch.object(stop_work, "stop_session", side_effect=ignoring_idle_reason(self.fake_stop)) as stop,
              patch.object(settle, "send_message") as send,
              patch.object(settle, "report_stop") as report):
            self.assertEqual(settle.conduct(LOGIN), 0)
        scan.assert_called_once()
        stop.assert_called_once()
        self.assertEqual(stop.call_args.args[0]["session"]["session_id"], "ready-owner")
        self.assertIsNone(record.find_session(LOGIN, "withdrawn-owner"))
        current = record.find_session(LOGIN, "ready-owner")
        assert current is not None
        self.assertEqual(current["progress"]["kind"], "stopped")
        reported = cast(list[record.SessionShutdown], report.call_args.args[0])
        self.assertEqual([saved["session"]["session_id"] for saved in reported], ["ready-owner"])
        send.assert_not_called()

    def test_now_advances_reaping_owner_with_and_without_explicit_selection(self) -> None:
        for ids in ((), ("reaping-owner",)):
            with self.subTest(ids=ids):
                identity = ids[0] if ids else "unfiltered-reaping-owner"
                owner = top_level(identity, "Reaping owner", status="busy")
                saved = session_shutdown(owner, progress=record.SessionReaping(kind="reaping", at=NOW,
                    because="account out of usage", detail="weekly limit"))
                self.save(saved)
                with (patch.object(settle, "_launch_conductor", side_effect=self.run_conductor) as launch,
                      patch.object(settle, "run_inventory", return_value=machine_inventory([owner])),
                      patch.object(stop_work, "stop_session", side_effect=ignoring_idle_reason(self.fake_stop)) as stop,
                      patch.object(settle, "send_message") as send,
                      patch.object(settle, "report_stop"), redirect_stdout(io.StringIO())):
                    self.assertEqual(settle.now(Account("claude", LOGIN, LABEL), here=True, only=ids), 0)
                launch.assert_called_once_with(LOGIN, LABEL)
                stop.assert_called_once()
                self.assertEqual(stop.call_args.args[0]["progress"]["kind"], "reaping")
                current = record.find_session(LOGIN, owner["session_id"])
                assert current is not None
                self.assertEqual(current["progress"]["kind"], "stopped")
                send.assert_not_called()
                record.archive_session(LOGIN, owner["session_id"])

    def test_status_trail_prints_steps_in_user_zone_in_order(self) -> None:
        saved = session_shutdown(top_level("trail-owner", "Trail owner"))
        saved["trail"] = [record.ProgressStep(at=NOW, kind="waiting", detail="pause sent"),
            record.ProgressStep(at="2026-10-09T21:50:10+00:00", kind="reaping", detail="weekly limit")]
        with patch.dict(os.environ, {"USER_ZONE": "America/New_York"}):
            lines = settle.status_record_lines(saved)
        self.assertEqual([line.strip() for line in lines[1:]],
            ["17:49:10 waiting pause sent", "17:50:10 reaping weekly limit"])

    def test_conductor_tick_inventories_only_live_record_ids_without_desktop_refresh(self) -> None:
        first = top_level("first-owner", "First owner", status="busy")
        second = top_level("second-owner", "Second owner", status="busy")
        worker = worker_session("owned-worker", "Owned worker", owner=first["session_id"])
        for owner in (first, second):
            saved = session_shutdown(owner)
            saved["settle_message"] = record.SettleMessageSent(kind="sent", at=NOW)
            if owner is first:
                saved["workers"] = [worker]
            self.save(saved)
        with (
            patch.object(settle, "inventory", return_value=machine_inventory([first, second, worker])) as read,
            patch.object(stop_work, "stop_session") as stop,
            patch.object(settle, "send_message") as send,
            patch.object(time, "sleep", side_effect=PassObserved),
            self.assertRaises(PassObserved),
        ):
            _ = settle.conduct(LOGIN)
        read.assert_called_once_with(LOGIN,
            only=frozenset({first["session_id"], second["session_id"]}), refresh_desktops=False)
        stop.assert_not_called()
        send.assert_not_called()

    def test_conductor_tick_reads_cached_desktops_without_running_snapshot_updater(self) -> None:
        owner = top_level("cached-owner", "Cached owner", status="busy")
        saved = session_shutdown(owner)
        saved["settle_message"] = record.SettleMessageSent(kind="sent", at=NOW)
        self.save(saved)
        registered = sessions.SessionRecord(pid=owner["pid"], sessionId=owner["session_id"],
            name=owner["name"], procStart=owner["proc_start"], messagingSocketPath="",
            updatedAt=1, tmux="", formerNames=[])
        registry = self.root / "sessions"
        registry.mkdir(parents=True, exist_ok=True)
        _ = (registry / f"{owner['pid']}.json").write_text(json.dumps(
            {**registered, "status": "busy", "cwd": str(self.root)}))
        _ = self.enterContext(patch.object(inventory, "showrunner_registrations", return_value=inventory.ShowrunnerRegistrations(())))
        _ = self.enterContext(patch.object(inventory, "_unit_sessions", return_value={}))
        _ = self.enterContext(patch.object(inventory, "_active_run_directories", return_value={}))
        _ = self.enterContext(patch.object(inventory, "_newest_unit_plans", return_value={}))
        with (
            patch.object(Path, "home", return_value=self.root),
            patch.object(sys, "platform", "linux"),
            patch.dict(os.environ, {"REFUSAL_WATCH_INVENTORY": "refresh"}),
            patch.object(sessions, "live_sessions", return_value=[registered]),
            patch.object(inventory, "_record_matches_process", return_value=True),
            patch.object(inventory, "_configuration_for_session", return_value=
                inventory.AttributedSessionConfiguration(Account("claude", LOGIN, LABEL), self.root / ".claude")),
            patch.object(inventory, "_checkout", return_value=owner["checkout"]),
            patch.object(inventory, "_host", return_value=owner["host"]),
            patch.object(inventory, "_timers", return_value=[]),
            patch.object(inventory, "label_for", return_value=LABEL),
            patch.object(inventory, "_snapshot_desktops", wraps=inventory._snapshot_desktops) as snapshot,  # pyright: ignore[reportPrivateUsage]
            patch.object(shutil, "which", return_value="/fixture/agent-sessions-snapshot"),
            patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as command,
            patch.object(settle, "send_message") as send,
            patch.object(stop_work, "stop_session") as stop,
            patch.object(time, "sleep", side_effect=PassObserved),
            self.assertRaises(PassObserved),
        ):
            _ = settle.conduct(LOGIN)
        snapshot.assert_called_once_with(refresh=False)
        command.assert_not_called()
        send.assert_not_called()
        stop.assert_not_called()

    def test_refusal_before_pause_does_not_reap_a_waiting_ghostty_owner(self) -> None:
        owner = top_level("older-refusal", "Older refusal", status="busy")
        owner["config_dir"] = str(self.root / ".claude")
        owner["host"] = inventory.WindowHost(kind="ghostty", window_shell=123,
            desktop=inventory.DesktopNotInSnapshot(kind="not in snapshot"))
        self.write_refusal_transcript(owner["session_id"])
        saved = session_shutdown(owner)
        saved["settle_message"] = record.SettleMessageSent(kind="sent",
            at=(NOW_UTC + timedelta(seconds=1)).isoformat())
        self.save(saved)
        with (patch.object(settle, "run_inventory", return_value=machine_inventory([owner])),
              patch.object(stop_work, "stop_session") as stop,
              patch.object(settle, "send_message") as send,
              patch.object(settle, "report_stop"),
              patch.object(time, "sleep", side_effect=PassObserved),
              self.assertRaises(PassObserved)):
            _ = settle.conduct(LOGIN)
        current = record.find_session(LOGIN, owner["session_id"])
        assert current is not None
        self.assertEqual(current["progress"]["kind"], "waiting")
        stop.assert_not_called()
        send.assert_not_called()

    def test_conductor_service_preserves_read_existing_snapshot_policy(self) -> None:
        self.allow_leaf(ShutdownSendLeaf.LAUNCH_CONDUCTOR)
        for snapshot_policy in ("", "read existing"):
            with (self.subTest(policy=snapshot_policy), patch.object(sys, "platform", "linux"),
                  patch.dict(os.environ, {"REFUSAL_WATCH_INVENTORY": snapshot_policy}),
                  patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as launch):
                settle._launch_conductor(LOGIN, LABEL)  # pyright: ignore[reportPrivateUsage]
            launch.assert_called_once()
            arguments = cast(list[str], launch.call_args.args[0])
            self.assertEqual(arguments[0], "systemd-run")
            python_index = arguments.index(sys.executable)
            self.assertEqual(arguments[python_index - 2:], ["env", "PYTHONUNBUFFERED=1",
                sys.executable, "-u", str(settle.SHUTDOWN), "conduct", LOGIN])
            if snapshot_policy:
                self.assertIn("REFUSAL_WATCH_INVENTORY=read existing", arguments[:python_index])

    def test_darwin_conductor_launch_waits_for_its_start_line_in_a_fresh_state_log(self) -> None:
        self.allow_leaf(ShutdownSendLeaf.LAUNCH_CONDUCTOR)
        paths: list[Path] = []

        def start(argv: Sequence[str], *, ready: Callable[[], bool], within: float,
                  output: detached.AppendLog, environment: detached.ExplicitEnvironment) -> int:
            log = output.path
            self.assertEqual(environment.mapping[detached.STARTUP_SIGNAL_ENV], "1")
            self.assertEqual(list(argv), ["env", "REFUSAL_WATCH_INVENTORY=read existing", "env",
                "PYTHONUNBUFFERED=1", sys.executable, "-u", str(settle.SHUTDOWN), "conduct", LOGIN])
            self.assertEqual(within, 15)
            self.assertEqual(log.parent, self.root / "state" / "logs")
            self.assertNotIn(log, paths)
            paths.append(log)
            self.assertFalse(ready())
            _ = log.write_text("unrelated output\n")
            self.assertFalse(ready())
            @contextmanager
            def already_running(login: str) -> Generator[record.ConductorAlreadyRunning, None, None]:
                self.assertEqual(login, LOGIN)
                self.assertNotIn(detached.STARTUP_SIGNAL_ENV, os.environ)
                yield record.ConductorAlreadyRunning()

            with (log.open("a") as stream, redirect_stderr(stream),
                  patch.dict(os.environ, environment.mapping, clear=True),
                  patch.object(record, "conductor_lock", side_effect=already_running)):
                self.assertEqual(settle.conduct(LOGIN), 0)
            self.assertTrue(ready(), "the child entry point must publish its startup line")
            return 4321

        with (patch.object(sys, "platform", "darwin"), patch.object(herdr, "_PLATFORM", "darwin"),
              patch.dict(os.environ, {"REFUSAL_WATCH_INVENTORY": "read existing"}),
              patch.object(detached, "start_detached", side_effect=start),
              patch.object(subprocess, "run", side_effect=AssertionError("service manager used"))):
            for _ in range(2):
                settle._launch_conductor(LOGIN, LABEL)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(len(paths), 2)
        self.assertEqual(record.all_session_records(), [])
        self.assertEqual(record.all_account_holds(), [])

    def test_darwin_conductor_failed_child_reports_service_and_keeps_its_log(self) -> None:
        self.allow_leaf(ShutdownSendLeaf.LAUNCH_CONDUCTOR)
        paths: list[Path] = []

        def fail(argv: Sequence[str], *, ready: Callable[[], bool], within: float,
                  output: detached.AppendLog, environment: detached.ExplicitEnvironment) -> int:
            log = output.path
            self.assertEqual(environment.mapping[detached.STARTUP_SIGNAL_ENV], "1")
            del argv, within
            self.assertFalse(ready())
            paths.append(log)
            _ = log.write_text("child import failed\n")
            raise detached.DetachedStartFailure("child exited with status 1")

        with (patch.object(sys, "platform", "darwin"), patch.object(herdr, "_PLATFORM", "darwin"),
              patch.object(detached, "start_detached", side_effect=fail),
              patch.object(subprocess, "run", side_effect=AssertionError("service manager used")),
              self.assertRaisesRegex(RuntimeError,
                  r"could not start shutdown conductor shutdown-claude2-.*: child exited with status 1") as failure):
            settle._launch_conductor(LOGIN, LABEL)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(paths[0].read_text(), "child import failed\n")
        self.assertIn(str(paths[0]), str(failure.exception))

    def test_ghostty_transcript_refusal_enters_usage_hold_without_a_screen(self) -> None:
        owner = top_level("ghostty-refusal", "Ghostty owner")
        owner["host"] = inventory.WindowHost(kind="ghostty", window_shell=123,
            desktop=inventory.DesktopNotInSnapshot(kind="not in snapshot"))
        self.write_refusal_transcript(owner["session_id"])
        saved = session_shutdown(owner)
        with patch.object(settle, "_screen", return_value=settle.ScreenUnreadable("no pane host")):
            self.assertIsInstance(settle._usage_limit_change(saved, self.matching(saved)),  # pyright: ignore[reportPrivateUsage]
                settle.EnterUsageLimitHold)
            owner["status"] = "busy"
            self.assertIsInstance(settle._usage_limit_change(saved, self.matching(saved)),  # pyright: ignore[reportPrivateUsage]
                settle.NoUsageLimitChange)

    def test_ghostty_assumed_refusal_enters_usage_hold_for_only_that_session(self) -> None:
        owner = top_level("assumed-owner", "Assumed owner")
        owner["host"] = inventory.WindowHost(kind="ghostty", window_shell=123,
            desktop=inventory.DesktopNotInSnapshot(kind="not in snapshot"))
        path = self.root / ".local/state/accounts/assumed_refusals/assumed-owner.json"
        path.parent.mkdir(parents=True)
        _ = path.write_text(json.dumps({"uuid": "assumed-refusal", "timestamp": NOW,
            "window": "weekly", "reset": ""}))
        saved = session_shutdown(owner)
        other = session_shutdown(top_level("unrelated-owner", "Unrelated owner"))
        with patch.object(settle, "_screen", return_value=settle.ScreenUnreadable("no pane host")):
            self.assertIsInstance(settle._usage_limit_change(saved, self.matching(saved)),  # pyright: ignore[reportPrivateUsage]
                settle.EnterUsageLimitHold)
            self.assertIsInstance(settle._usage_limit_change(other, self.matching(other)),  # pyright: ignore[reportPrivateUsage]
                settle.NoUsageLimitChange)

    def test_a_move_stops_a_busy_session_two_minutes_after_its_pause_message(self) -> None:
        owner = top_level("busy-owner", "Busy owner", status="busy", busy_workers=["Busy worker"])
        saved = session_shutdown(owner)
        saved["settle_message"] = record.SettleMessageSent(kind="sent", at=NOW)
        saved["target"], saved["source_label"], saved["why"] = "claude3", LABEL, "weekly limit reached"
        self.assertIsInstance(settle.next_step(saved, self.matching(saved), [], NOW_UTC + timedelta(seconds=119)), settle.Hold)
        overdue = NOW_UTC + settle.MOVE_ANSWER_AFTER
        self.assertEqual(settle.next_step(saved, self.matching(saved), [], overdue),
                         settle.StopSession(forced=True, mid_turn=True))
        unreadable = stop_work.SessionAccountUnreadable(kind="session account unreadable")
        self.assertEqual(settle.next_step(saved, unreadable, [], overdue), settle.StopSession(forced=True))
        # A full shutdown never stops a busy session on time alone.
        full = cast(record.SessionShutdown, cast(object, {key: value for key, value in saved.items()
                                                           if key not in {"target", "source_label", "why"}}))
        self.assertNotIsInstance(settle.next_step(full, self.matching(full), [], overdue), settle.StopSession)
        self.assertIsInstance(settle.next_step(full, unreadable, [], overdue), settle.Hold)

    def test_a_move_counts_its_answer_time_from_the_request_when_no_message_was_sent(self) -> None:
        owner = top_level("busy-owner", "Busy owner", status="busy")
        saved = session_shutdown(owner, progress=record.SessionReadyToStop(kind="ready", at=NOW))
        saved["target"] = "claude3"
        owner["status"] = "idle"
        self.assertEqual(settle.next_step(saved, self.matching(saved), [], NOW_UTC + settle.MOVE_ANSWER_AFTER),
                         settle.StopSession(forced=True))

    def test_a_mid_turn_move_stop_saves_the_last_tool_call_before_the_forced_stop(self) -> None:
        owner = top_level("busy-owner", "Busy owner", status="busy")
        owner["config_dir"] = str(self.root / ".claude")
        saved = session_shutdown(owner, then="restart")
        saved["target"] = "claude3"
        self.save(saved)
        path = self.root / ".claude/projects/cut-off-fixture" / "busy-owner.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        def assistant(*content: dict[str, object]) -> str:
            return json.dumps({"type": "assistant", "message": {"content": list(content)}})
        _ = path.write_text("\n".join((
            assistant({"type": "tool_use", "name": "Read", "input": {"file_path": "/early"}}),
            assistant({"type": "text", "text": "running"},
                      {"type": "tool_use", "name": "Bash", "input": {"command": "x", "description": "Run   the\n tests"}}),
            json.dumps({"type": "user", "message": {"content": "tool result"}}),
            "not json",
        )) + "\n")
        self.assertEqual(inventory.last_tool_call(self.root / ".claude", "busy-owner"), "Bash: Run the tests")
        self.assertEqual(inventory.last_tool_call(self.root / ".claude", "no-transcript"), "")
        def stopped(current: record.SessionShutdown, *, idle_reason: stop_work.IdleStopReason = "not an idle stop",
                    forced: bool = False) -> record.SessionShutdown:
            del idle_reason
            self.assertTrue(forced)
            return current
        with (patch.object(stop_work, "stop_session", side_effect=stopped),
              patch.object(settle, "send_message", return_value={"kind": "sent"})):
            _ = settle._stop_after_telling(saved, settle.StopSession(forced=True, mid_turn=True))  # pyright: ignore[reportPrivateUsage]
        stored = record.find_session(LOGIN, "busy-owner")
        assert stored is not None
        self.assertEqual(stored.get("cut_off"), "Bash: Run the tests")

    def test_transcript_refusal_hold_keeps_busy_worker_safety_and_a_move_skips_upstream(self) -> None:
        owner = top_level("refused-owner", "Refused owner", busy_workers=["Busy worker"])
        self.write_refusal_transcript(owner["session_id"])
        saved = session_shutdown(owner, progress=record.SessionHeldByUsageLimit(kind="held by usage limit", at=NOW))
        saved["target"] = "claude3"
        saved["source_label"] = LABEL
        saved["why"] = "claude2 refused: weekly limit until tomorrow"
        self.assertIsInstance(settle.next_step(saved, self.matching(saved), [], NOW_UTC), settle.Hold)
        line = settle.status_record_lines(saved, self.matching(saved))[0]
        self.assertIn("claude3", line)
        self.assertIn(saved["why"], line)
        owner["codex_servers"] = []
        owner["checkout"] = checkout(ahead=1)
        self.assertIsInstance(settle.next_step(saved, self.matching(saved), [], NOW_UTC), settle.StopSession)
        # The same owner under a full shutdown: it shares the session, so the checkout changes reach both.
        full = cast(record.SessionShutdown, cast(object, {key: value for key, value in saved.items()
                                                           if key not in {"target", "source_label", "why"}}))
        self.assertIsInstance(settle.next_step(full, self.matching(full), [], NOW_UTC), settle.Hold)
        owner["checkout"] = checkout()
        self.assertIsInstance(settle.next_step(full, self.matching(full), [], NOW_UTC), settle.StopSession)

    def test_cancelled_move_records_failure_and_removes_only_its_assumed_refusal(self) -> None:
        saved = session_shutdown(top_level("cancelled-owner", "Cancelled owner"), then="restart")
        saved["target"] = "claude3"
        self.save(saved)
        assumed = self.root / ".local/state/accounts/assumed_refusals"
        assumed.mkdir(parents=True)
        own = assumed / "cancelled-owner.json"
        other = assumed / "unrelated-owner.json"
        _ = own.write_text("{}")
        _ = other.write_text("{}")
        with (patch.object(settle, "_retire_shutdown_messages"),
              patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")),
              redirect_stdout(io.StringIO())):
            self.assertEqual(settle.cancel(Account("claude", LOGIN, LABEL), True), 0)
        self.assertFalse(own.exists())
        self.assertTrue(other.exists())
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["failed"])
        failed = entries[0]
        assert failed["kind"] == "failed"
        self.assertEqual(failed["move_id"], saved["request_id"])
        self.assertEqual(failed["reason"], "cancelled")

    def test_cancelling_a_group_member_journals_the_transition_instead_of_its_attempt_id(self) -> None:
        saved = session_shutdown(top_level("cancel-transition", "Cancelled group member"), then="restart")
        saved.update(target="claude3", move_id="whole-group-transition")
        self.save(saved)
        with (patch.object(settle, "_retire_shutdown_messages"),
              patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")),
              redirect_stdout(io.StringIO())):
            self.assertEqual(settle.cancel(Account("claude", LOGIN, LABEL), True), 0)
        entries = moves.read_moves()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["move_id"], "whole-group-transition")
        self.assertNotEqual(entries[0]["move_id"], saved["request_id"])

    def test_cancel_suppresses_refusal_before_a_watcher_tick_after_withdrawal(self) -> None:
        from scripts.accounts import accounts, refusal_watch
        import agent_accounts
        owner = top_level("cancel-race", "Cancelled owner")
        saved = session_shutdown(owner, then="restart")
        saved["target"] = "claude3"
        self.save(saved)
        self.write_refusal_transcript(owner["session_id"])
        live = sessions.SessionRecord(pid=owner["pid"], sessionId=owner["session_id"],
            name=owner["name"], messagingSocketPath="", updatedAt=1, tmux="", formerNames=[])
        withdraw = record.withdraw_session
        ticks: list[list[str]] = []
        def withdraw_then_tick(login: str, session_id: str,
                               restore: Callable[[record.SessionShutdown], None]) -> record.SessionWithdrawn | record.SessionRetained:
            outcome = withdraw(login, session_id, restore)
            ticks.append(refusal_watch.run(NOW_UTC))
            return outcome
        with (patch.object(record, "withdraw_session", side_effect=withdraw_then_tick),
              patch.object(sessions, "live_sessions", return_value=[live]),
              patch.object(refusal_watch, "live_owners", return_value=[owner]),
              patch.object(accounts, "account_identity", return_value=agent_accounts.NumberedAccount(LABEL, Path.home() / ".claude", LOGIN)),
              patch.object(accounts, "pick", return_value=accounts.PickedAccount("claude3", self.root / ".claude3",
                  80, accounts.NoResetKnown())),
              patch.object(accounts, "move_sessions", return_value=(
                  accounts.SessionMoveRequested(owner["session_id"], "replacement", "claude3", source_login=LOGIN),)) as move,
              patch.object(settle, "_restore_record"), redirect_stdout(io.StringIO())):
            self.assertEqual(settle.cancel(Account("claude", LOGIN, LABEL), True), 0)
        self.assertEqual(len(ticks), 1)
        move.assert_not_called()
        self.assertEqual(record.session_records(LOGIN), [])

    def run_conductor(self, login: str, _label: str) -> None:
        self.assertEqual(login, LOGIN)
        self.assertEqual(settle.conduct(login), 0)

    def matching(self, saved: record.SessionShutdown) -> stop_work.FreshSession:
        return stop_work.MatchingSession(kind="matching session", session=saved["session"], workers=saved["workers"])

    def conduct_idle_stop(self, saved: record.SessionShutdown, owner: inventory.Session,
                          expected_where: str) -> None:
        self.save(saved)
        claims: list[record.Where] = []
        signals: list[tuple[int, int]] = []

        def observe_update(login: str, identity: str,
                           change: Callable[[record.SessionShutdown], None]) -> record.SessionShutdown:
            def observe(current: record.SessionShutdown) -> None:
                before = current["progress"]["kind"]
                where_before = current["where"].copy()
                change(current)
                progress = current["progress"]
                if before == "waiting" and progress["kind"] == "stopping":
                    self.assertEqual(where_before, {"kind": "not said"})
                    self.assertEqual(current["where"], {
                        "kind": "said", "text": expected_where, "at": progress["at"],
                    })
                    self.assertEqual(datetime.fromisoformat(progress["at"]).microsecond, 0)
                    claims.append(current["where"].copy())
            return record.update_session(login, identity, observe)

        def kill(pid: int, sent_signal: int) -> None:
            current = record.find_session(LOGIN, owner["session_id"])
            assert current is not None
            self.assertEqual(current["progress"]["kind"], "stopping")
            self.assertEqual(current["where"], claims[0])
            signals.append((pid, sent_signal))

        perform_stop = partial(stop_work.stop_session, kill=kill, is_alive=lambda _pid: False,
            session_record_exists=lambda _pid: False,
            fresh_inventory=lambda _login, _only: machine_inventory([owner]),
            clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0,
            sleep=lambda _seconds: None)
        with (
            patch.object(settle, "run_inventory", return_value=machine_inventory([owner])),
            patch.object(stop_work, "update_session", side_effect=observe_update),
            patch.object(stop_work, "stop_session", new=perform_stop),
            patch.object(settle, "send_message") as sender,
            patch.object(settle, "alert_user", return_value=True) as alert,
            patch.object(settle, "report_stop"),
        ):
            self.assertEqual(settle.conduct(LOGIN), 0)
        self.assertEqual(signals, [(owner["pid"], signal.SIGTERM)])
        sender.assert_not_called()
        alert.assert_not_called()
        self.assertEqual(len(claims), 1)
        latest = record.find_session(LOGIN, owner["session_id"])
        assert latest is not None
        self.assertEqual(latest["progress"]["kind"], "stopped")
        self.assertEqual(latest["where"], claims[0])
        self.assertEqual(latest["settle_message"], saved["settle_message"])

    def test_not_ready_stop_waits_then_retries_and_restarts_without_reporting_early(self) -> None:
        owner = top_level("retry-owner", "Retry owner")
        saved = session_shutdown(owner, progress={"kind": "ready", "at": NOW}, then="restart")
        saved["where"] = {"kind": "said", "text": "resume the check", "at": NOW}
        self.save(saved)
        fresh_busy = top_level("retry-owner", "Retry owner", status="busy")
        fresh_calls = 0
        saw_refusal = False
        signals: list[int] = []

        def fresh(_login: str, _only: frozenset[str]) -> inventory.Inventory:
            nonlocal fresh_calls
            fresh_calls += 1
            return machine_inventory([fresh_busy if fresh_calls == 1 else owner])

        def observe(_login: str, _only: object = (), *, refresh_desktops: bool = True) -> inventory.Inventory:
            nonlocal saw_refusal
            del refresh_desktops
            current = record.find_session(LOGIN, owner["session_id"])
            if current is not None and current["stop_issues"] and current["progress"]["kind"] == "ready":
                saw_refusal = True
                self.assertFalse(settle._done(current))  # pyright: ignore[reportPrivateUsage]
                report.assert_not_called()
            return machine_inventory([owner])

        perform_stop = partial(stop_work.stop_session, kill=lambda _pid, sent: signals.append(sent),
            is_alive=lambda _pid: False, session_record_exists=lambda _pid: False,
            fresh_inventory=fresh, clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0,
            sleep=lambda _seconds: None)
        with (
            patch.object(settle, "SETTLE_INTERVAL_SECONDS", 0.001),
            patch.object(settle, "run_inventory", side_effect=observe),
            patch.object(stop_work, "stop_session", new=perform_stop),
            patch.object(restart_work, "relaunch_session", side_effect=self.fake_launch),
            patch.object(restart_work, "wait_for_confirmation", side_effect=self.all_live),
            patch.object(restart_work, "confirm_entry", side_effect=self.fake_confirm),
            patch.object(settle, "report_stop") as report,
        ):
            self.assertEqual(settle.conduct(LOGIN), 0)
        self.assertTrue(saw_refusal)
        self.assertEqual(fresh_calls, 2)
        self.assertEqual(signals, [signal.SIGTERM])
        report.assert_called_once()
        outcomes = cast(list[record.SessionShutdown], report.call_args.args[0])
        self.assertEqual(outcomes[0]["progress"]["kind"], "restarted")
        self.assertEqual(outcomes[0]["where"], saved["where"])
        self.assertEqual(outcomes[0]["stop_issues"], [])

    def test_real_stop_exception_replaces_an_earlier_not_ready_issue(self) -> None:
        saved = session_shutdown(top_level("failed-owner", "Failed owner"), progress={"kind": "ready", "at": NOW})
        saved["stop_issues"] = [record.NotReadyToStop(kind="not ready to stop", at=NOW,
            status="busy", progress="ready")]
        self.save(saved)
        settle._action_failed(saved, RuntimeError("signal refused"), stopping=True)  # pyright: ignore[reportPrivateUsage]
        current = record.find_session(LOGIN, "failed-owner")
        assert current is not None
        self.assertEqual(current["progress"], {"kind": "stop failed", "at": NOW, "reason": "signal refused"})
        self.assertEqual(current["stop_issues"], [])
        self.assertTrue(settle._done(current))  # pyright: ignore[reportPrivateUsage]

    def test_ready_recovers_old_not_ready_failure_and_launches_conductor(self) -> None:
        saved = session_shutdown(top_level("recovered-owner", "Recovered owner"), progress={
            "kind": "stop failed", "at": NOW, "reason": "session or worker is not idle and ready"})
        saved["stop_issues"] = [record.NotReadyToStop(kind="not ready to stop", at=NOW,
            status="busy", progress="waiting")]
        directory = self.root / "state" / LOGIN / "sessions"
        directory.mkdir(parents=True)
        _ = (directory / "recovered-owner.json").write_text(json.dumps(saved))
        with (
            patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "recovered-owner"}),
            patch.object(settle, "run_inventory", return_value=machine_inventory([saved["session"]])),
            patch.object(settle, "_launch_conductor") as launch,
        ):
            self.assertEqual(settle.ready("finish next check"), 0)
        launch.assert_called_once_with(LOGIN, LABEL)
        current = record.find_session(LOGIN, "recovered-owner")
        assert current is not None
        self.assertEqual(current["progress"], {"kind": "ready", "at": NOW})
        self.assertEqual(current["where"], {"kind": "said", "at": NOW, "text": "finish next check"})

    def test_ready_launch_failure_keeps_checkpoint_and_names_recovery_terminal_command(self) -> None:
        saved = session_shutdown(top_level("orphan-owner", "Orphan owner"))
        self.save(saved)
        output, error = io.StringIO(), io.StringIO()
        with (
            patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "orphan-owner"}),
            patch.object(settle, "run_inventory", return_value=machine_inventory([saved["session"]])),
            patch.object(settle, "_launch_conductor", side_effect=RuntimeError("launch unavailable")),
            redirect_stdout(output), redirect_stderr(error),
        ):
            self.assertEqual(settle.ready("resume here"), 2)
        text = output.getvalue() + error.getvalue()
        self.assertIn("in a terminal on natedev", text)
        self.assertIn("~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py", text)
        self.assertIn(f"conduct {LOGIN}", text)
        current = record.find_session(LOGIN, "orphan-owner")
        assert current is not None
        self.assertEqual(current["progress"]["kind"], "ready")
        self.assertEqual(current["where"], {"kind": "said", "at": NOW, "text": "resume here"})

    def test_shell_automatic_stops_record_background_shell_for_both_pause_states(self) -> None:
        for message in ("not sent", "sent"):
            with self.subTest(message=message):
                owner = top_level("shell-" + message, "Shell owner", status="shell")
                owner["updated_at"] = int(NOW_UTC.timestamp() * 1000) + 1
                saved = session_shutdown(owner)
                saved["settle_message"] = {"kind": "not sent"} if message == "not sent" else {"kind": "sent", "at": NOW}
                saved["workers"] = [worker_session("idle-worker", "Idle worker", owner=owner["session_id"])]
                step = settle.next_step(saved, self.matching(saved), [], NOW_UTC)
                self.assertIsInstance(step, settle.StopSession)
                assert isinstance(step, settle.StopSession)
                saved["workers"] = []
                reason = "idle; nothing in progress" if message == "not sent" else "idle after the pause message; no ready line"
                self.assertEqual(step.idle_reason, reason + "; a background shell was running")
                self.conduct_idle_stop(saved, owner, reason + "; a background shell was running")

    def test_ready_shell_keeps_checkpoint_but_shell_worker_and_busy_owner_still_wait(self) -> None:
        owner = top_level("shell-ready", "Shell owner", status="shell")
        saved = session_shutdown(owner, progress={"kind": "ready", "at": NOW})
        saved["where"] = {"kind": "said", "text": "my own checkpoint", "at": NOW}
        step = settle.next_step(saved, self.matching(saved), [], NOW_UTC)
        self.assertIsInstance(step, settle.StopSession)
        assert isinstance(step, settle.StopSession)
        self.assertEqual(step.idle_reason, "not an idle stop")
        self.assertNotIn("shell", settle.hold_reason(saved, self.matching(saved)))
        saved["workers"] = [worker_session("worker-shell", "Shell worker", owner="shell-ready", status="shell")]
        self.assertIsInstance(settle.next_step(saved, self.matching(saved), [], NOW_UTC), settle.Hold)
        busy = session_shutdown(top_level("busy-owner", "Busy owner", status="busy"))
        self.assertIsInstance(settle.next_step(busy, self.matching(busy), [], NOW_UTC), settle.SendSettleMessage)

    def test_idle_before_any_message_stops_without_sending_and_records_where(self) -> None:
        saved = session_shutdown(top_level("idle-before", "Idle before", status="busy"))
        fresh = top_level("idle-before", "Idle before")
        self.conduct_idle_stop(saved, fresh, "idle; nothing in progress")

    def test_idle_after_pause_with_fresh_update_stops_and_records_where(self) -> None:
        saved = session_shutdown(top_level("idle-after", "Idle after", status="busy"))
        saved["settle_message"] = {"kind": "sent", "at": NOW}
        fresh = top_level("idle-after", "Idle after")
        fresh["updated_at"] = int(NOW_UTC.timestamp() * 1000) + 1
        self.conduct_idle_stop(saved, fresh, "idle after the pause message; no ready line")

    def test_idle_with_queued_pause_stops_without_retrying_delivery(self) -> None:
        saved = session_shutdown(top_level("queued-idle", "Queued idle"))
        saved["settle_message"] = {"kind": "queued", "at": NOW, "reason": "delivery failed"}
        self.conduct_idle_stop(saved, saved["session"], "idle; nothing in progress")

    def test_busy_after_pause_reasks_holds_and_alerts_only_when_requested(self) -> None:
        for alerts in ("user", "none"):
            with self.subTest(alerts=alerts):
                saved = session_shutdown(unit("busy-" + alerts, "Busy", status="busy"))
                saved["alerts"] = alerts
                saved["settle_message"] = {"kind": "sent", "at": NOW}
                owner = self.matching(saved)
                self.assertIsInstance(settle.next_step(saved, owner, [],
                    NOW_UTC + settle.REASK_AFTER - timedelta(seconds=1)), settle.Hold)
                self.assertIsInstance(settle.next_step(saved, owner, [],
                    NOW_UTC + settle.REASK_AFTER), settle.SendSettleMessage)
                saved["reasked_at"] = (NOW_UTC + settle.REASK_AFTER).isoformat()
                self.assertIsInstance(settle.next_step(saved, owner, [],
                    NOW_UTC + settle.NOT_ANSWERING_AFTER), settle.Hold)
                self.assertFalse(settle._holdout_due(saved, NOW_UTC + settle.NOT_ANSWERING_AFTER  # pyright: ignore[reportPrivateUsage]
                    - timedelta(seconds=1)))
                self.save(saved)

                def end_pass(_seconds: float) -> None:
                    raise PassObserved()

                with (
                    patch.object(settle, "now_utc", return_value=NOW_UTC + settle.NOT_ANSWERING_AFTER),
                    patch.object(settle, "run_inventory", return_value=machine_inventory([saved["session"]])),
                    patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")),
                    patch.object(settle, "alert_user", return_value=True) as alert,
                    patch.object(stop_work, "stop_session") as stop,
                    patch.object(time, "sleep", side_effect=end_pass),
                    self.assertRaises(PassObserved),
                ):
                    _ = settle.conduct(LOGIN)
                self.assertEqual(alert.call_count, 1 if alerts == "user" else 0)
                stop.assert_not_called()
                record.archive_session(LOGIN, saved["session"]["session_id"])

    def test_idle_update_at_or_before_pause_is_not_an_answer(self) -> None:
        saved = session_shutdown(top_level("stale-idle", "Stale idle"))
        saved["session"]["updated_at"] = int(NOW_UTC.timestamp() * 1000) + 1000
        saved["settle_message"] = {"kind": "sent", "at": NOW}
        for offset in (-1, 0):
            with self.subTest(milliseconds=offset):
                fresh = top_level("stale-idle", "Stale idle")
                fresh["updated_at"] = int(NOW_UTC.timestamp() * 1000) + offset
                owner = stop_work.MatchingSession(kind="matching session", session=fresh, workers=[])
                self.assertIsInstance(settle.next_step(saved, owner, [], NOW_UTC), settle.Hold)
                self.assertIsInstance(settle.next_step(saved, owner, [], NOW_UTC + settle.REASK_AFTER),
                    settle.SendSettleMessage)

    def test_idle_with_form_blocker_or_busy_worker_still_holds(self) -> None:
        saved = session_shutdown(top_level("blocked-idle", "Blocked idle"))
        saved["settle_message"] = {"kind": "sent", "at": NOW}
        saved["session"]["updated_at"] = int(NOW_UTC.timestamp() * 1000) + 1
        with patch.object(settle, "_showing_form", return_value=True):
            self.assertIsInstance(settle.next_step(saved, self.matching(saved), [], NOW_UTC), settle.Hold)
        saved["session"]["checkout"] = checkout(ahead=1)
        self.assertIsInstance(settle.next_step(saved, self.matching(saved), [], NOW_UTC), settle.Hold)
        saved["session"]["checkout"] = checkout()
        saved["workers"] = [worker_session("busy-worker", "Busy worker", owner="blocked-idle", status="busy")]
        self.assertIsInstance(settle.next_step(saved, self.matching(saved), [], NOW_UTC), settle.Hold)


    def test_usage_limit_patterns_match_only_the_last_fifteen_nonempty_lines(self) -> None:
        patterns = ("hit your session limit", "hit your usage limit", "hit your weekly limit",
                    "usage limit reached", "wait for limit to reset")
        self.assertEqual(settle.USAGE_LIMIT_PATTERNS, patterns)
        for pattern in patterns:
            with self.subTest(pattern=pattern):
                shown = settle.ScreenText(pattern.upper() + "\n\n" + "\n\n".join(["menu"] * 14))
                old = settle.ScreenText(pattern + "\n" + "\n\n".join(["conversation"] * 15))
                self.assertTrue(settle._held_by_usage_limit(shown))  # pyright: ignore[reportPrivateUsage]
                self.assertFalse(settle._held_by_usage_limit(old))  # pyright: ignore[reportPrivateUsage]
        self.assertFalse(settle._held_by_usage_limit(settle.ScreenText("ordinary prompt")))  # pyright: ignore[reportPrivateUsage]
        self.assertFalse(settle._held_by_usage_limit(settle.ScreenUnreadable("capture failed")))  # pyright: ignore[reportPrivateUsage]


    def test_unit_screen_refuses_a_registry_record_with_a_reused_pid(self) -> None:
        owner = unit("original", "Original")
        registered = sessions.SessionRecord(pid=owner["pid"], sessionId="different", name="Different",
            messagingSocketPath="", updatedAt=1, tmux="", formerNames=[])
        with patch.object(sessions, "read_session", return_value=registered), \
             patch.object(herdr, "herdr_place_of", return_value=herdr.HerdrPlace("other", "w1:p1")) as place, \
             patch("session_pane.read_place", return_value="other conversation") as read:
            screen = settle._screen(owner)  # pyright: ignore[reportPrivateUsage]
            self.assertIsInstance(screen, settle.ScreenUnreadable)
            place.assert_not_called()
            read.assert_not_called()
            registered["sessionId"] = owner["session_id"]
            self.assertEqual(settle._screen(owner), settle.ScreenText("other conversation"))  # pyright: ignore[reportPrivateUsage]
            read.assert_called_once_with(herdr.HerdrPlace("other", "w1:p1"), 60)

    def test_herdr_owner_screen_reads_its_named_pane_and_usage_limit(self) -> None:
        owner = top_level("native", "Native")
        owner["host"] = inventory.HerdrHost(kind="herdr", herdr_session="native-host", pane_id="w1:p2", presentation="detached")
        responses = self.root / "herdr-responses.json"
        log = self.root / "herdr.jsonl"
        command = ["--session", "native-host", "pane", "read", "w1:p2", "--source", "recent", "--lines", "60"]
        response = CommandResponse(code=0, stdout="hit your usage limit\nEsc to cancel\n")
        _ = responses.write_text(json.dumps({" ".join(command): response}))
        with patch.dict(os.environ, {
            "HERDR_BIN": str(Path(__file__).resolve().parents[1] / "herdr" / "fake_herdr.py"),
            "FAKE_HERDR_LOG": str(log), "FAKE_HERDR_RESPONSES": str(responses),
        }):
            screen = settle._screen(owner)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(screen, settle.ScreenText("hit your usage limit\nEsc to cancel\n"))
        self.assertTrue(settle._held_by_usage_limit(screen))  # pyright: ignore[reportPrivateUsage]
        self.assertEqual([json.loads(line) for line in log.read_text().splitlines()], [command])

    def test_herdr_screen_failure_never_creates_a_usage_limit_hold(self) -> None:
        owner = top_level("native", "Native")
        owner["host"] = inventory.HerdrHost(kind="herdr", herdr_session="native-host", pane_id="w1:p2", presentation="detached")
        responses = self.root / "herdr-responses.json"
        log = self.root / "herdr.jsonl"
        response = CommandResponse(code=1, stdout="hit your weekly limit", stderr="pane unavailable")
        _ = responses.write_text(json.dumps({"default": response}))
        for binary in (Path(__file__).resolve().parents[1] / "herdr" / "fake_herdr.py", self.root / "absent-herdr"):
            with self.subTest(binary=binary), patch.dict(os.environ, {
                "HERDR_BIN": str(binary), "FAKE_HERDR_LOG": str(log),
                "FAKE_HERDR_RESPONSES": str(responses),
            }):
                screen = settle._screen(owner)  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(screen, settle.ScreenUnreadable("capture failed"))
                self.assertFalse(settle._held_by_usage_limit(screen))  # pyright: ignore[reportPrivateUsage]


    def test_usage_limit_change_requires_matching_wait_for_ready_owner(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"))
        with patch.object(settle, "_screen", return_value=settle.ScreenText("hit your usage limit")) as screen:
            for progress in (record.SessionWaiting(kind="waiting"), record.SessionReadyToStop(kind="ready", at=NOW)):
                saved["progress"] = progress
                self.assertIsInstance(settle._usage_limit_change(saved, self.matching(saved)), settle.EnterUsageLimitHold)  # pyright: ignore[reportPrivateUsage]
            saved["session"]["status"] = "busy"
            screen.reset_mock()
            self.assertIsInstance(settle._usage_limit_change(saved, self.matching(saved)), settle.NoUsageLimitChange)  # pyright: ignore[reportPrivateUsage]
            screen.assert_not_called()
            saved["session"]["status"] = "idle"
            saved["force"] = "now"
            self.assertIsInstance(settle._usage_limit_change(saved, self.matching(saved)), settle.NoUsageLimitChange)  # pyright: ignore[reportPrivateUsage]
            saved["force"] = "wait for ready"
            for owner in (stop_work.SessionAbsent(kind="session absent"), stop_work.SessionIdentityLost(kind="session identity lost"), stop_work.SessionAccountUnreadable(kind="session account unreadable")):
                self.assertIsInstance(settle._usage_limit_change(saved, owner), settle.NoUsageLimitChange)  # pyright: ignore[reportPrivateUsage]
            saved["progress"] = {"kind": "stopping", "at": NOW}
            self.assertIsInstance(settle._usage_limit_change(saved, self.matching(saved)), settle.NoUsageLimitChange)  # pyright: ignore[reportPrivateUsage]
        saved["progress"] = record.SessionHeldByUsageLimit(kind="held by usage limit", at=NOW)
        for shown in (settle.ScreenText("ordinary prompt"), settle.ScreenUnreadable("capture failed")):
            with self.subTest(screen=shown), patch.object(settle, "_screen", return_value=shown):
                self.assertIsInstance(settle._usage_limit_change(saved, self.matching(saved)), settle.LeaveUsageLimitHold)  # pyright: ignore[reportPrivateUsage]
        saved["session"]["status"] = "busy"
        with patch.object(settle, "_screen", return_value=settle.ScreenText("hit your session limit")):
            self.assertIsInstance(settle._usage_limit_change(saved, self.matching(saved)), settle.LeaveUsageLimitHold)  # pyright: ignore[reportPrivateUsage]

    def test_held_next_step_uses_idle_workers_and_readiness_without_the_limit_form(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"), progress=record.SessionHeldByUsageLimit(kind="held by usage limit", at=NOW))
        with patch.object(settle, "_showing_form", return_value=True) as form:
            self.assertIsInstance(settle.next_step(saved, self.matching(saved), [], NOW_UTC), settle.StopSession)
            saved["workers"] = [worker_session("worker", "Worker", owner="owner", status="busy")]
            self.assertEqual(settle.next_step(saved, self.matching(saved), [], NOW_UTC), settle.Hold("Codex worker Worker is still running"))
            saved["workers"] = []
            saved["session"]["status"] = "busy"
            self.assertIsInstance(settle.next_step(saved, self.matching(saved), [], NOW_UTC), settle.Hold)
            saved["session"]["status"] = "idle"
            saved["session"]["checkout"] = checkout(ahead=1)
            self.assertIsInstance(settle.next_step(saved, self.matching(saved), [], NOW_UTC), settle.Hold)
            form.assert_not_called()
        for owner in (stop_work.SessionAbsent(kind="session absent"), stop_work.SessionIdentityLost(kind="session identity lost")):
            with self.subTest(owner=owner):
                self.assertIsInstance(settle.next_step(saved, owner, [], NOW_UTC), settle.StopSession)
        self.assertEqual(settle.next_step(saved, stop_work.SessionAccountUnreadable(kind="session account unreadable"), [], NOW_UTC), settle.Hold("account unreadable"))
        saved["force"] = "now"
        saved["session"]["status"] = "busy"
        self.assertIsInstance(settle.next_step(saved, self.matching(saved), [], NOW_UTC), settle.StopSession)

    def test_one_conduct_pass_stops_limit_waiting_owner_and_ordinary_ready_owner(self) -> None:
        held = session_shutdown(top_level("held", "Held"))
        ordinary = session_shutdown(top_level("ordinary", "Ordinary"), progress={"kind": "ready", "at": NOW})
        held["workers"] = [worker_session("held-worker", "Held worker", owner="held")]
        ordinary["workers"] = [worker_session("ordinary-worker", "Ordinary worker", owner="ordinary")]
        self.save(held)
        self.save(ordinary)
        seen: list[str] = []
        def screen(owner: inventory.Session) -> settle.ScreenText:
            return settle.ScreenText("hit your session limit" if owner["session_id"] == "held" else "ordinary prompt")
        def stop(saved: record.SessionShutdown) -> record.SessionShutdown:
            stored = record.find_session(LOGIN, saved["session"]["session_id"])
            assert stored is not None
            self.assertEqual(stored["progress"], saved["progress"])
            self.assertEqual(saved["progress"]["kind"], "held by usage limit" if saved["session"]["session_id"] == "held" else "ready")
            seen.append(saved["session"]["session_id"])
            return self.fake_stop(saved)
        report = machine_inventory([held["session"], ordinary["session"], *held["workers"], *ordinary["workers"]])
        with patch.object(settle, "run_inventory", return_value=report) as observe, patch.object(settle, "_screen", side_effect=screen), patch.object(stop_work, "stop_session", side_effect=ignoring_idle_reason(stop)), patch.object(settle, "send_message") as sender, patch.object(settle, "report_stop"):
            self.assertEqual(settle.conduct(LOGIN), 0)
        self.assertCountEqual(seen, ["held", "ordinary"])
        observe.assert_called_once()
        sender.assert_not_called()

    def test_conductor_returns_held_owner_to_waiting_and_sends_settle_message(self) -> None:
        deliveries: list[record.SettleMessage] = [
            {"kind": "not sent"},
            {"kind": "queued", "at": NOW, "reason": "pending"},
            {"kind": "sent", "at": NOW},
        ]
        for delivery in deliveries:
            with self.subTest(delivery=delivery["kind"]):
                owner = top_level("owner-" + delivery["kind"], "Owner", status="busy")
                saved = session_shutdown(owner, progress=record.SessionHeldByUsageLimit(kind="held by usage limit", at=NOW))
                saved["settle_message"] = delivery
                saved["alerts"] = "none"
                self.save(saved)
                def end_pass(_seconds: float) -> None:
                    raise PassObserved()
                with patch.object(settle, "now_utc", return_value=NOW_UTC), patch.object(settle, "run_inventory", return_value=machine_inventory([owner])), patch.object(settle, "_screen", return_value=settle.ScreenText("ordinary prompt")), patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")) as sender, patch.object(stop_work, "stop_session") as stop, patch.object(time, "sleep", side_effect=end_pass), self.assertRaises(PassObserved):
                    _ = settle.conduct(LOGIN)
                sender.assert_called_once()
                stop.assert_not_called()
                latest = record.find_session(LOGIN, owner["session_id"])
                assert latest is not None
                self.assertEqual(latest["progress"], {"kind": "waiting"})
                record.archive_session(LOGIN, owner["session_id"])

    def test_usage_limit_write_refuses_cancel_replacement_and_stop_claim_in_conductor(self) -> None:
        for index, mutation in enumerate(("cancel", "replace", "claim")):
            with self.subTest(mutation=mutation):
                saved = session_shutdown(top_level("owner", "Owner"))
                sentinel = session_shutdown(top_level("sentinel", "Sentinel", status="busy"))
                saved["requested_at"] = sentinel["requested_at"] = (NOW_UTC + timedelta(seconds=index)).isoformat()
                self.save(saved)
                self.save(sentinel)
                apply = settle._apply_usage_limit_change  # pyright: ignore[reportPrivateUsage]
                def interleave(current: record.SessionShutdown, change: settle.EnterUsageLimitHold | settle.LeaveUsageLimitHold | settle.NoUsageLimitChange) -> record.SessionShutdown | settle.UsageLimitChangeRefused:
                    if current["session"]["session_id"] != "owner":
                        return apply(current, change)
                    if mutation == "cancel":
                        _ = record.withdraw_session(LOGIN, "owner", lambda _saved: None)
                    else:
                        def replace(latest: record.SessionShutdown) -> None:
                            if mutation == "replace":
                                latest["request_id"] = "replacement"
                            else:
                                latest["progress"] = {"kind": "stopping", "at": NOW}
                        _ = record.update_session(LOGIN, "owner", replace)
                    before = record.find_session(LOGIN, "owner")
                    result = apply(current, change)
                    self.assertIsInstance(result, settle.UsageLimitChangeRefused)
                    self.assertEqual(record.find_session(LOGIN, "owner"), before)
                    return result
                def screen(owner: inventory.Session) -> settle.ScreenText:
                    return settle.ScreenText("usage limit reached" if owner["session_id"] == "owner" else "ordinary prompt")
                def end_pass(_seconds: float) -> None:
                    raise PassObserved()
                with patch.object(settle, "run_inventory", return_value=machine_inventory([saved["session"], sentinel["session"]])), patch.object(settle, "_screen", side_effect=screen), patch.object(settle, "_apply_usage_limit_change", side_effect=interleave), patch.object(settle, "_send_settle_message"), patch.object(stop_work, "stop_session") as stop, patch.object(time, "sleep", side_effect=end_pass), self.assertRaises(PassObserved):
                    _ = settle.conduct(LOGIN)
                stop.assert_not_called()
                for latest in record.session_records(LOGIN):
                    record.archive_session(LOGIN, latest["session"]["session_id"])

    def test_held_owner_has_no_holdout_alert_and_done_requires_stop_or_restart(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"), progress=record.SessionHeldByUsageLimit(kind="held by usage limit", at=NOW))
        saved["workers"] = [worker_session("worker", "Worker", owner="owner", status="busy")]
        self.save(saved)
        self.assertFalse(settle._done(saved))  # pyright: ignore[reportPrivateUsage]
        at = NOW_UTC + settle.NOT_ANSWERING_AFTER + timedelta(seconds=1)
        self.assertFalse(settle._reserve_holdout_alert(saved, at))  # pyright: ignore[reportPrivateUsage]
        def end_pass(_seconds: float) -> None:
            raise PassObserved()
        with patch.object(settle, "now_utc", return_value=at), patch.object(settle, "_screen", return_value=settle.ScreenText("wait for limit to reset")), patch.object(settle, "run_inventory", return_value=machine_inventory([saved["session"], *saved["workers"]])), patch.object(settle, "alert_user", return_value=True) as alert, patch.object(settle, "send_message") as sender, patch.object(time, "sleep", side_effect=end_pass), self.assertRaises(PassObserved):
            _ = settle.conduct(LOGIN)
        alert.assert_not_called()
        sender.assert_not_called()
        for progress in (record.SessionStopped(kind="stopped", at=NOW), record.SessionRestarted(kind="restarted", at=NOW)):
            saved["progress"] = progress
            self.assertTrue(settle._done(saved))  # pyright: ignore[reportPrivateUsage]

    def test_ready_accepts_owner_held_by_usage_limit(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"), progress=record.SessionHeldByUsageLimit(kind="held by usage limit", at=NOW))
        self.save(saved)
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "owner"}), patch.object(settle, "run_inventory", return_value=machine_inventory([saved["session"]])), patch.object(settle, "_launch_conductor"):
            self.assertEqual(settle.ready("limit reset"), 0)
        latest = record.find_session(LOGIN, "owner")
        assert latest is not None
        self.assertEqual(latest["progress"], {"kind": "ready", "at": NOW})

    def test_next_step_covers_request_delivery_stop_restore_and_terminal_states(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"))
        cases: list[tuple[record.SessionProgress, str, record.ShutdownContinuation, type[settle.Step]]] = [
            ({"kind": "waiting"}, "idle", "stop", settle.StopSession),
            ({"kind": "ready", "at": NOW}, "idle", "stop", settle.StopSession),
            ({"kind": "ready", "at": NOW}, "busy", "stop", settle.Hold),
            ({"kind": "stopped", "at": NOW}, "idle", "restart", settle.RelaunchSession),
            ({"kind": "already gone", "at": NOW}, "idle", "restart", settle.RelaunchSession),
            ({"kind": "process identity lost", "at": NOW}, "idle", "restart", settle.RelaunchSession),
            ({"kind": "stopped", "at": NOW}, "idle", "stop", settle.Finished),
            ({"kind": "already gone", "at": NOW}, "idle", "stop", settle.Finished),
            ({"kind": "process identity lost", "at": NOW}, "idle", "stop", settle.Finished),
            ({"kind": "restarted", "at": NOW}, "idle", "restart", settle.Finished),
            ({"kind": "restart failed", "at": NOW, "reason": "launch"}, "idle", "restart", settle.Hold),
            ({"kind": "stop failed", "at": NOW, "reason": "busy"}, "idle", "stop", settle.Hold),
            ({"kind": "manual restart", "command": "resume"}, "idle", "restart", settle.Hold),
            ({"kind": "timers pending", "at": NOW, "timers": []}, "idle", "restart", settle.Hold),
            ({"kind": "stopping", "at": NOW}, "idle", "stop", settle.Hold),
            ({"kind": "restarting", "at": NOW, "prior": {"kind": "stopped", "at": NOW}}, "idle", "restart", settle.Hold),
        ]
        for progress, status, then, expected in cases:
            with self.subTest(progress=progress["kind"], status=status, then=then):
                saved["progress"], saved["then"], saved["session"]["status"] = progress, then, status
                self.assertIsInstance(settle.next_step(saved, self.matching(saved), [saved], NOW_UTC), expected)

    def test_next_step_keeps_unreadable_owner_held_and_missing_identity_unsignalled(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"), progress={"kind": "ready", "at": NOW})
        self.assertEqual(settle.next_step(saved, stop_work.SessionAccountUnreadable(kind="session account unreadable"), [], NOW_UTC), settle.Hold("account unreadable"))
        for owner in (stop_work.SessionAbsent(kind="session absent"), stop_work.SessionIdentityLost(kind="session identity lost")):
            self.assertIsInstance(settle.next_step(saved, owner, [], NOW_UTC), settle.StopSession)

    def test_worker_becoming_idle_allows_the_owner_to_stop(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"), progress={"kind": "ready", "at": NOW})
        worker = worker_session("worker", "Worker", owner="owner", status="busy")
        observed = stop_work.fresh_session(saved["session"], machine_inventory([saved["session"], worker]))
        self.assertEqual(settle.next_step(saved, observed, [], NOW_UTC), settle.Hold("Codex worker Worker is still running"))
        worker["status"] = "idle"
        self.assertIsInstance(settle.next_step(saved, stop_work.fresh_session(saved["session"], machine_inventory([saved["session"], worker])), [], NOW_UTC), settle.StopSession)

    def test_showrunner_ready_does_not_wait_for_a_waiting_unit(self) -> None:
        runner = session_shutdown(showrunner("runner", "Runner"), progress={"kind": "ready", "at": NOW})
        director = session_shutdown(unit("unit", "Unit"))
        self.assertIsInstance(settle.next_step(runner, self.matching(runner), [runner, director], NOW_UTC), settle.StopSession)

    def test_pause_message_starts_with_a_fixed_line_and_asks_for_a_stop_at_once(self) -> None:
        moving = session_shutdown(unit("unit", "Unit"))
        moving["target"], moving["why"] = "claude1", "out of usage"
        first, body = settle._settle_text(moving, moving["session"]).split("\n", 1)  # pyright: ignore[reportPrivateUsage]
        self.assertTrue(first.startswith("Pause for move: claude2 → claude1 (out of usage, "), first)
        self.assertIn("Stop now, without finishing the step you are in", body)
        self.assertIn("shutdown.py ready --where", body)
        self.assertIn("You will be resumed on claude1 with that line", body)
        self.assertNotIn("let any worker turn", body)
        stopping = session_shutdown(top_level("owner", "Owner"))
        first = settle._settle_text(stopping, stopping["session"]).splitlines()[0]  # pyright: ignore[reportPrivateUsage]
        self.assertTrue(first.startswith("Pause for stop: claude2 (requested by the user, "), first)

    def assert_pause_confirmation(self, saved: record.SessionShutdown, first_line: str,
                                  citation: str, operation: str) -> None:
        with patch.object(settle, "_user_time", return_value="Fri 2:49 PM"):
            text = settle._settle_text(saved, saved["session"])  # pyright: ignore[reportPrivateUsage]
        lines = text.splitlines()
        self.assertEqual(lines[0], first_line)
        confirmation = lines[1]
        self.assertEqual(confirmation, settle.pause_confirmation_line(saved))
        self.assertIn("the shutdown the user started", confirmation)
        self.assertIn(citation, confirmation)
        self.assertIn("not from another session's own choice", confirmation)
        self.assertIn("run in this terminal:", confirmation)
        self.assertIn("~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py status claude2 --here",
                      confirmation)
        self.assertTrue(confirmation.endswith(f"It lists this session under the {operation}."), confirmation)
        self.assertNotIn(saved["login"], text)
        self.assertEqual(lines[2], "")
        self.assertTrue(lines[3].startswith("Stop now, without finishing the step you are in"), lines[3])
        self.assertIn("shutdown.py ready --where", lines[3])

    def test_move_pause_confirms_user_request_by_canonical_account_before_stop_instructions(self) -> None:
        for owner in (top_level("owner", "Owner", status="busy"),
                      unit("unit", "Unit", status="busy"), showrunner("runner", "Runner")):
            with self.subTest(kind=owner["kind"]):
                owner["status"] = "busy"
                saved = session_shutdown(owner, then="restart")
                saved["label"] = "claude 2"
                saved["target"], saved["why"] = "claude1", "out of usage"
                self.assert_pause_confirmation(saved,
                    "Pause for move: claude 2 → claude1 (out of usage, Fri 2:49 PM)",
                    'docs/accounts.md, "Move a session"', "move")

    def test_stop_pause_confirms_user_request_by_canonical_account_before_stop_instructions(self) -> None:
        for owner in (top_level("owner", "Owner", status="busy"),
                      unit("unit", "Unit", status="busy"), showrunner("runner", "Runner")):
            with self.subTest(kind=owner["kind"]):
                owner["status"] = "busy"
                saved = session_shutdown(owner)
                saved["label"] = "claude 2"
                self.assert_pause_confirmation(saved,
                    "Pause for stop: claude 2 (requested by the user, Fri 2:49 PM)",
                    "docs/as-built/shutdown-and-restart.md", "stop")

    def unit_for_showrunner(self, then: record.ShutdownContinuation = "restart") -> record.SessionShutdown:
        owner = unit("unit", "Unit")
        owner["host"]["doc"] = "/tmp/demo-production.md"
        saved = session_shutdown(owner, progress={"kind": "ready", "at": NOW}, then=then)
        saved["target"] = "claude3"
        saved["why"] = "claude2 is out of usage"
        self.save(saved)
        return saved

    @staticmethod
    def sent(send: Mock) -> tuple[str, str, str]:
        return cast(tuple[str, str, str], send.call_args.args)

    def test_unit_showrunner_is_told_once_before_the_stop_and_once_when_back(self) -> None:
        saved = self.unit_for_showrunner()
        with (
            patch.object(settle_showrunners, "current_name", return_value="runner") as named,
            patch.object(settle, "send_message", return_value={"kind": "sent"}) as send,
            patch.object(stop_work, "stop_session", side_effect=ignoring_idle_reason(unchanged)),
        ):
            _ = settle._stop_after_telling(saved, settle.StopSession())  # pyright: ignore[reportPrivateUsage]
            _ = settle._stop_after_telling(record.session_records(LOGIN)[0], settle.StopSession())  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(send.call_count, 1)
            self.assertEqual(cast(tuple[str], named.call_args.args), ("demo",))
            self.assertEqual(self.sent(send)[:2], ("runner", "Unit is stopping: claude2 is out of usage"))
            self.assertIn("it comes back on claude3 within minutes", self.sent(send)[2])
            back = record.session_records(LOGIN)[0]
            back["progress"] = {"kind": "restarted", "at": NOW}
            settle.tell_showrunners_outcome([back])
            self.assertEqual(send.call_count, 2)
            self.assertEqual(self.sent(send)[:2], ("runner", "Unit is back on claude3"))

    def test_a_move_reported_after_a_later_stop_and_forget_does_not_say_the_unit_is_back(self) -> None:
        saved = self.unit_for_showrunner()
        def restarted(current: record.SessionShutdown) -> None:
            current["progress"] = {"kind": "restarted", "at": NOW}
            current["showrunner_told"] = settle.SHOWRUNNER_TOLD_STOPPING
        back = record.update_session(LOGIN, "unit", restarted)
        record.archive_session(LOGIN, "unit")
        stop = session_shutdown(saved["session"], progress={"kind": "stopped", "at": NOW}, request_id="request-two")
        stop["requested_at"] = (NOW_UTC + timedelta(minutes=10)).isoformat()
        self.save(stop)
        self.assertIsInstance(record.forget_session(LOGIN, "unit", "natedev"), record.SessionForgotten)
        with (
            patch.object(settle_showrunners, "current_name", return_value="runner"),
            patch.object(settle, "send_message", return_value={"kind": "sent"}) as send,
        ):
            settle.tell_showrunners_outcome([back])
        send.assert_not_called()

    def test_unit_showrunner_is_told_once_per_new_failure_and_a_stop_says_it_stays_stopped(self) -> None:
        saved = self.unit_for_showrunner(then="stop")
        with (
            patch.object(restart_work, "restart_failed_liveness", return_value=restart_work.SessionNotLive()),
            patch.object(settle_showrunners, "current_name", return_value="runner"),
            patch.object(settle, "send_message", return_value={"kind": "sent"}) as send,
        ):
            settle.tell_showrunner_stopping(saved)
            self.assertIn("it stays stopped until the user restarts it", self.sent(send)[2])
            def fail(current: record.SessionShutdown) -> None:
                current["progress"] = {"kind": "restart failed", "at": NOW, "reason": "launch timed out"}
            settle.tell_showrunners_outcome([record.update_session(LOGIN, "unit", fail)])
            settle.tell_showrunners_outcome([record.session_records(LOGIN)[0]])
            self.assertEqual(send.call_count, 2)
            self.assertEqual(self.sent(send)[1], "Unit did not come back: launch timed out")

    def test_retire_reason_is_saved_and_sent_to_the_session_and_showrunner(self) -> None:
        owner = unit("retiring", "Retiring")
        owner["host"]["doc"] = "/tmp/demo-production.md"
        added = settle.begin(LOGIN, only=["retiring"], request_id="retire-request",
                             then="stop", why="retired; /showrunner:resume_unit brings it back.",
                             report=machine_inventory([owner]))
        self.assertEqual(len(added), 1)
        saved = record.session_records(LOGIN)[0]
        self.assertIn("retired; /showrunner:resume_unit brings it back.",
                      settle._settle_text(saved, owner))  # pyright: ignore[reportPrivateUsage]
        self.assertIn("retired; /showrunner:resume_unit brings it back.", settle.holdout_line(saved))
        with (patch.object(settle_showrunners, "current_name", return_value="runner"),
              patch.object(settle, "send_message", return_value={"kind": "sent"}) as send):
            settle.tell_showrunner_stopping(saved)
        self.assertEqual(self.sent(send), ("runner",
            "Retiring is stopping: retired; /showrunner:resume_unit brings it back.",
            "Retiring is stopping: retired; /showrunner:resume_unit brings it back."))

    def test_normal_user_stop_keeps_the_showrunner_text(self) -> None:
        saved = self.unit_for_showrunner(then="stop")
        def user_stop(current: record.SessionShutdown) -> None:
            _ = current.pop("why", None)
            _ = current.pop("target", None)
        saved = record.update_session(LOGIN, "unit", user_stop)
        with (patch.object(settle_showrunners, "current_name", return_value="runner"),
              patch.object(settle, "send_message", return_value={"kind": "sent"}) as send):
            settle.tell_showrunner_stopping(saved)
        self.assertEqual(self.sent(send), ("runner", "Unit is stopping: requested by the user",
            "Unit is stopping: requested by the user; it stays stopped until the user restarts it. " +
            "One more line follows when it is back or has not come back."))

    def test_only_a_unit_with_a_running_showrunner_is_announced(self) -> None:
        other = session_shutdown(top_level("other", "Other"))
        self.save(other)
        saved = self.unit_for_showrunner()
        with (
            patch.object(settle_showrunners, "current_name", return_value=""),
            patch.object(settle, "send_message", return_value={"kind": "sent"}) as send,
        ):
            settle.tell_showrunner_stopping(other)
            settle.tell_showrunner_stopping(saved)
        send.assert_not_called()
        self.assertFalse(any("showrunner_told" in current for current in record.session_records(LOGIN)))

    def test_failed_restart_showrunner_outcome_checks_liveness_and_sends_once(self) -> None:
        saved = self.unit_for_showrunner()
        cases = (restart_work.SessionLive(), restart_work.SessionNotLive(),
                 restart_work.SessionLivenessUnknown("scratch registry unreadable"))
        for observed in cases:
            with self.subTest(liveness=observed):
                def failed(current: record.SessionShutdown) -> None:
                    current["progress"] = {"kind": "restart failed", "at": NOW,
                                           "reason": "restart prompt not submitted"}
                    current["showrunner_told"] = settle.SHOWRUNNER_TOLD_STOPPING
                saved = record.update_session(LOGIN, "unit", failed)
                with (patch.object(restart_work, "restart_failed_liveness", return_value=observed) as liveness,
                      patch.object(settle_showrunners, "current_name", return_value="runner"),
                      patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")) as send):
                    settle.tell_showrunners_outcome([saved])
                    liveness.assert_called_once_with(saved)
                    current = record.find_session(LOGIN, "unit")
                    assert current is not None
                    settle.tell_showrunners_outcome([current])
                    self.assertEqual(liveness.call_count, 2)
                send.assert_called_once()
                recipient, summary, text = self.sent(send)
                self.assertEqual(recipient, "runner")
                if isinstance(observed, restart_work.SessionLive):
                    self.assertEqual(summary,
                        "Unit is back on claude3; its restart prompt was not confirmed — check it, " +
                        "then close its record in a terminal on natedev: " + settle.shutdown_command("forget", "unit", "--account", LOGIN))
                    self.assertEqual(text, summary)
                    self.assertNotIn("shutdown.py restart", text)
                    self.assertEqual(current.get("showrunner_told"), settle.SHOWRUNNER_TOLD_BACK)
                else:
                    self.assertIn("Unit did not come back: restart prompt not submitted", summary)
                    self.assertIn(settle.shutdown_command("restart", LOGIN, "--here", "--only", "unit"), text)
                    self.assertEqual(current.get("showrunner_told"), settle.not_back_text(saved))
                    if isinstance(observed, restart_work.SessionLivenessUnknown):
                        self.assertIn("liveness unknown: scratch registry unreadable", summary)

    def test_stop_report_passes_live_failed_unit_to_showrunner_with_one_liveness_read(self) -> None:
        _ = self.unit_for_showrunner()
        def failed(current: record.SessionShutdown) -> None:
            current["progress"] = {"kind": "restart failed", "at": NOW,
                                   "reason": "restart prompt not submitted"}
            current["showrunner_told"] = settle.SHOWRUNNER_TOLD_STOPPING
        saved = record.update_session(LOGIN, "unit", failed)
        with (patch.object(restart_work, "clear_running_on_target",
                           return_value=restart_work.TargetCheck([saved], [])),
              patch.object(restart_work, "restart_failed_liveness", return_value=restart_work.SessionLive()) as liveness,
              patch.object(settle_showrunners, "current_name", return_value="runner"),
              patch.object(settle, "run_remote", return_value=(0, "[]")),
              patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")) as send,
              patch.object(settle, "alert_user", return_value=True) as alert):
            settle.report_stop([saved])
        liveness.assert_called_once_with(saved)
        back = [call for call in send.call_args_list if call.args[0] == "runner"]
        self.assertEqual(len(back), 1)
        self.assertEqual(back[0].args[1],
                         "Unit is back on claude3; its restart prompt was not confirmed — check it, " +
                         "then close its record in a terminal on natedev: " + settle.shutdown_command("forget", "unit", "--account", LOGIN))
        self.assertNotIn("shutdown.py restart", cast(str, back[0].args[2]))
        current = record.find_session(LOGIN, "unit")
        assert current is not None
        self.assertEqual(current.get("showrunner_told"), settle.SHOWRUNNER_TOLD_BACK)
        alert.assert_not_called()

    def test_now_stops_a_busy_requester_without_waiting_for_its_request(self) -> None:
        requester = session_shutdown(top_level("requester", "Requester"), requester="requester", then="restart")
        requester["force"] = "now"
        other = session_shutdown(top_level("other", "Other"), requester="requester", then="restart")
        self.assertIsInstance(settle.next_step(requester, self.matching(requester), [other], NOW_UTC), settle.StopSession)

    def test_requester_waits_for_its_own_request_through_restore_confirmation(self) -> None:
        requester = session_shutdown(top_level("requester", "Requester"), progress={"kind": "ready", "at": NOW}, requester="requester", then="restart")
        other = session_shutdown(top_level("other", "Other"), progress={"kind": "stopped", "at": NOW}, requester="requester", then="restart")
        self.assertEqual(settle.next_step(requester, self.matching(requester), [other], NOW_UTC), settle.Hold("requester goes last"))
        other["progress"] = {"kind": "restarted", "at": NOW}
        self.assertIsInstance(settle.next_step(requester, self.matching(requester), [other], NOW_UTC), settle.StopSession)

    def test_two_selected_requests_in_one_second_do_not_wait_on_each_other(self) -> None:
        first = session_shutdown(top_level("first", "First"), progress={"kind": "ready", "at": NOW}, requester="first", request_id="one")
        second = session_shutdown(top_level("second", "Second"), progress={"kind": "ready", "at": NOW}, requester="second", request_id="two")
        for current in (first, second):
            self.assertIsInstance(settle.next_step(current, self.matching(current), [first, second], NOW_UTC), settle.StopSession)

    def test_queued_message_retries_only_after_five_minutes_and_showrunner_is_immediate(self) -> None:
        saved = session_shutdown(showrunner("runner", "Runner"))
        saved["session"]["status"] = "busy"
        self.assertIsInstance(settle.next_step(saved, self.matching(saved), [], NOW_UTC), settle.SendSettleMessage)
        saved["settle_message"] = {"kind": "queued", "at": NOW, "reason": "no live session"}
        self.assertIsInstance(settle.next_step(saved, self.matching(saved), [], NOW_UTC + timedelta(seconds=299)), settle.Hold)
        self.assertIsInstance(settle.next_step(saved, self.matching(saved), [], NOW_UTC + timedelta(seconds=300)), settle.SendSettleMessage)

    def test_moved_ghostty_showrunner_records_its_existing_desktop_without_old_window_tracking(self) -> None:
        owner = showrunner("hana-id", "hana")
        owner["host"] = inventory.WindowHost(kind="ghostty", window_shell=123,
            desktop=inventory.NamedDesktop(kind="named", name="Production"),
            supervisor=inventory.NoSupervisor(kind="no supervisor"))
        with (
            patch.object(settle, "run_inventory", return_value=machine_inventory([owner])),
            patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "")) as windows_probe,
            patch.object(sys, "platform", "linux"),
            redirect_stdout(io.StringIO()),
        ):
            saved = settle.begin(LOGIN, only=["hana-id"], request_id="move", target="claude3", then="restart")
        self.assertNotIn("old_window", saved[0])
        self.assertNotIn("old_window", record.session_records(LOGIN)[0])
        self.assertEqual(saved[0]["session"]["host"], owner["host"])
        windows_probe.assert_not_called()

    def test_down_to_stores_target_on_each_new_session_record(self) -> None:
        owners: list[inventory.Session] = [top_level("first", "First"), top_level("second", "Second")]
        with (
            patch.object(settle, "_warm", return_value=True),
            patch.object(settle, "inventory", return_value=machine_inventory(owners)),
            patch.object(settle, "run_inventory", return_value=machine_inventory(owners)),
            patch.object(settle, "_launch_conductor"),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(settle.down(Account("claude", LOGIN, LABEL), True,
                ["first", "second"], then="restart", target="claude3"), 0)
        saved = record.session_records(LOGIN)
        self.assertEqual(len(saved), 2)
        self.assertTrue(all(item.get("target") == "claude3" for item in saved))

    def test_down_journals_the_supplied_origin_without_inheriting_a_stored_account_refusal(self) -> None:
        from scripts.accounts import accounts, refusals
        reset = NOW_UTC + timedelta(days=2)
        refusals.write_refusal(refusals.AccountRefusal("claude", LOGIN,
            refusals.KnownWindow("weekly"), refusals.KnownReset(reset), NOW_UTC))
        for refused in (False, True):
            identity = "origin-" + str(refused)
            owner = top_level(identity, identity)
            origin: accounts.MoveOrigin = accounts.RefusalOrigin("weekly", accounts.KnownReset(reset)) if refused else accounts.HandOrigin()
            with (self.subTest(refused=refused), patch.object(settle, "_warm", return_value=True),
                  patch.object(settle, "inventory", return_value=machine_inventory([owner])),
                  patch.object(settle, "run_inventory", return_value=machine_inventory([owner])),
                  patch.object(settle, "_launch_conductor"),
                  patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")),
                  redirect_stdout(io.StringIO())):
                self.assertEqual(settle.down(Account("claude", LOGIN, LABEL), True, [identity], then="restart",
                    target="claude3", origin=origin, move_id="group-" + identity), 0)
            saved = record.find_session(LOGIN, identity)
            assert saved is not None
            self.assertEqual(saved.get("move_id"), "group-" + identity)
            requests = [entry for entry in moves.read_moves() if entry["kind"] == "requested" and entry["subject"].startswith(identity + ":")]
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["move_id"], saved.get("move_id"))
            self.assertEqual(requests[0].get("cause"), "refusal" if refused else "by hand")
            self.assertEqual(requests[0]["window"], "weekly" if refused else "unknown")
            if refused:
                self.assertEqual(requests[0].get("reset"), reset.isoformat())
            else:
                self.assertNotIn("reset", requests[0])

    def test_down_to_persists_source_label_and_explicit_reason_beside_each_new_target(self) -> None:
        with (
            patch.object(settle, "run_inventory", return_value=machine_inventory([top_level("owner", "Owner")])),
            redirect_stdout(io.StringIO()),
        ):
            saved = settle.begin(LOGIN, only=["owner"], request_id="move", then="restart",
                target="claude3", why="weekly limit reached")
        self.assertEqual(saved[0].get("source_label"), LABEL)
        self.assertEqual(saved[0].get("why"), "weekly limit reached")
        self.assertEqual(record.session_records(LOGIN)[0].get("source_label"), LABEL)
        self.assertEqual(record.session_records(LOGIN)[0].get("why"), "weekly limit reached")

    def test_begin_saves_a_checkout_move_on_the_record(self) -> None:
        with (
            patch.object(settle, "run_inventory", return_value=machine_inventory([top_level("owner", "Owner")])),
            redirect_stdout(io.StringIO()),
        ):
            _ = settle.begin(LOGIN, only=["owner"], request_id="checkout", then="restart",
                checkout_move={"old": "/w/old", "new": "/w/new"})
        self.assertEqual(record.session_records(LOGIN)[0].get("checkout_move"), {"old": "/w/old", "new": "/w/new"})

    def test_down_to_defaults_its_saved_reason_to_source_label_out_of_usage(self) -> None:
        with (
            patch.object(settle, "run_inventory", return_value=machine_inventory([top_level("owner", "Owner")])),
            redirect_stdout(io.StringIO()),
        ):
            saved = settle.begin(LOGIN, only=["owner"], request_id="move", then="restart", target="claude2")
        self.assertEqual(saved[0].get("why"), f"{LABEL} is out of usage")
        self.assertEqual(saved[0].get("source_label"), LABEL)

    def test_down_without_to_records_no_move_target(self) -> None:
        with patch.object(settle, "run_inventory", return_value=machine_inventory([top_level("owner", "Owner")])):
            added = settle.begin(LOGIN, only=["owner"], request_id="ordinary", then="restart")
        self.assertEqual(len(added), 1)
        self.assertNotIn("target", added[0])
        self.assertNotIn("target", record.session_records(LOGIN)[0])

    def request_move_over(self, existing: record.SessionShutdown) -> str:
        self.save(existing)
        with (
            patch.object(settle, "_warm", return_value=True),
            patch.object(settle, "inventory", return_value=machine_inventory([existing["session"]])),
            patch.object(settle, "run_inventory", return_value=machine_inventory([existing["session"]])),
            patch.object(settle, "_launch_conductor"),
            patch.object(settle, "_carry_trust"),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(settle.down(Account("claude", LOGIN, LABEL), True, ["owner"],
                then="restart", target="claude3", why="different reason", requested_by="another", request_id="second-request"), 0)
        return output.getvalue()

    def test_down_to_replaces_a_waiting_stop_and_keeps_its_request_fields(self) -> None:
        original = session_shutdown(top_level("owner", "Owner"), requester="requester",
            request_id="first-request", then="stop")
        original["alerts"] = "none"
        output = self.request_move_over(original)
        self.assertIn("Owner: the move replaces its waiting stop", output)
        expected = copy.deepcopy(original)
        expected["then"], expected["target"], expected["why"] = "restart", "claude3", "different reason"
        expected["source_label"] = LABEL
        self.assertEqual(record.find_session(LOGIN, "owner"), expected)

    def test_down_to_never_changes_an_existing_move(self) -> None:
        moving = session_shutdown(top_level("owner", "Owner"), requester="requester",
            request_id="first-request", then="restart")
        moving["target"], moving["source_label"], moving["why"] = "claude2", LABEL, "first reason"
        moving["alerts"] = "none"
        _ = self.request_move_over(moving)
        self.assertEqual(record.find_session(LOGIN, "owner"), moving)

    def test_down_to_never_changes_a_stop_in_progress(self) -> None:
        stopping = session_shutdown(top_level("owner", "Owner"), requester="requester",
            request_id="first-request", then="stop",
            progress=record.SessionReaping(kind="reaping", at=NOW, because="account out of usage", detail="weekly limit"))
        stopping["alerts"] = "none"
        _ = self.request_move_over(stopping)
        self.assertEqual(record.find_session(LOGIN, "owner"), stopping)

    def test_down_to_forwards_the_target_to_peer_for_remote_selected_sessions(self) -> None:
        with (
            patch.object(settle, "_warm", return_value=True),
            patch.object(settle, "inventory", return_value=machine_inventory([])),
            patch.object(settle, "run_inventory", return_value=machine_inventory([])),
            patch.object(settle, "_launch_conductor"),
            patch.object(settle, "run_remote", return_value=(0, "")) as peer,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(settle.down(Account("claude", LOGIN, LABEL), False, ["remote"],
                then="restart", target="claude2", why="weekly limit reached", requested_by="requester", request_id="move-request"), 0)
        arguments = cast(list[str], peer.call_args.args[0])
        self.assertEqual(arguments[:3], ["down", LOGIN, "--here"])
        self.assertEqual(arguments[arguments.index("--to") + 1], "claude2")
        self.assertEqual(arguments[arguments.index("--why") + 1], "weekly limit reached")
        self.assertEqual(arguments[arguments.index("--only") + 1], "remote")
        self.assertEqual(arguments[arguments.index("--requested-by") + 1], "requester")
        self.assertIn("--restart", arguments)

    def test_automatic_move_peer_retry_preserves_the_flag_and_journal_cause(self) -> None:
        account = Account("claude", LOGIN, LABEL)
        empty = machine_inventory([])
        for failure in ("peer unreachable",):
            identity = "auto-" + failure.replace(" ", "-")
            owner = top_level(identity, identity)
            errors = io.StringIO()
            with (self.subTest(failure=failure),
                  patch.object(settle, "_warm", return_value=True),
                  patch.object(settle, "_launch_conductor"),
                  patch.object(settle, "run_remote", return_value=(255, "unreachable")) as peer,
                  redirect_stdout(io.StringIO()), redirect_stderr(errors)):
                result = settle.down(account, False, [identity], then="restart", alerts="none",
                    target="claude3", why="auto: units-low", origin=accounts.AutoOrigin(),
                    move_id=identity, report=empty)
            self.assertEqual(result, 0)
            retry = shlex.split(errors.getvalue().split("~/.claude/scripts/lib/py ", 1)[1])
            self.assertTrue(retry[0].endswith("/shutdown.py"))
            arguments = retry[1:]
            self.assertIn("--auto", arguments)
            self.assertIn("--no-alert", arguments)
            peer.assert_called_once_with(arguments)
            report = machine_inventory([owner])
            with (patch.object(shutdown, "named_claude_account", return_value=account),
                  patch.object(shutdown, "resolve_sessions", return_value=shutdown.ResolvedSessions((identity,), report)),
                  patch.object(settle, "_warm", return_value=True),
                  patch.object(settle, "_launch_conductor"),
                  patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")),
                  redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
                self.assertEqual(shutdown.main(arguments), 0)
            requested = [entry for entry in moves.read_moves()
                         if entry["kind"] == "requested" and entry["move_id"] == identity]
            self.assertEqual(len(requested), 1)
            self.assertEqual((requested[0].get("cause"), requested[0]["why"]), ("auto", "auto: units-low"))
            saved = record.find_session(LOGIN, identity)
            assert saved is not None
            self.assertEqual(saved["alerts"], "none")

    def test_self_move_records_the_session_as_its_requester(self) -> None:
        owner = top_level("self-id", "Self")
        with (
            patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "self-id"}),
            patch.object(settle, "_warm", return_value=True),
            patch.object(settle, "inventory", return_value=machine_inventory([owner])),
            patch.object(settle, "run_inventory", return_value=machine_inventory([owner])),
            patch.object(settle, "_launch_conductor"),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(settle.down(Account("claude", LOGIN, LABEL), True, ["self-id"],
                then="restart", target="claude3"), 0)
        self.assertEqual(record.session_records(LOGIN)[0]["requested_by"],
            record.FromSession(kind="session", session_id="self-id"))

    def test_move_starts_with_cold_keys_and_a_full_shutdown_still_refuses(self) -> None:
        account = Account("claude", LOGIN, LABEL)
        owner = top_level("cold-move", "Cold move", ahead=2)
        report = machine_inventory([owner])
        with (patch.object(settle, "_warm", return_value=False) as warm,
              patch.object(settle, "_launch_conductor"),
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            self.assertEqual(settle.down(account, True, ["cold-move"], then="restart",
                target="claude3", why="weekly limit", report=report), 0)
            warm.assert_not_called()
            saved = record.find_session(LOGIN, "cold-move")
            assert saved is not None
            self.assertEqual(saved.get("target"), "claude3")
            self.assertEqual(settle.down(account, True, ["cold-move"], report=report), 1)
            warm.assert_called_once_with()

    def test_blocker_skips_unpushed_commits_only_for_a_move(self) -> None:
        owner = top_level("unpushed", "Unpushed", ahead=1)
        self.assertEqual(settle.blocker(owner), {"kind": "blocked", "reason": "push work first"})
        self.assertEqual(settle.blocker(owner, moving=True), {"kind": "clear"})
        owner = top_level("unpushed", "Unpushed", ahead=1, busy_workers=["Writer"])
        self.assertEqual(settle.blocker(owner, moving=True),
                         {"kind": "blocked", "reason": "Codex worker Writer is still running"})

    def test_move_hold_text_names_no_unpushed_commits(self) -> None:
        saved = session_shutdown(top_level("moving-ahead", "Moving ahead", status="busy", ahead=3))
        self.assertIn("ahead 3", settle.hold_reason(saved, self.matching(saved), local_observation=False))
        saved["target"] = "claude3"
        reason = settle.hold_reason(saved, self.matching(saved), local_observation=False)
        self.assertNotIn("ahead", reason)
        self.assertIn("busy", reason)

    def test_probe_begin_overrides_user_alerts_and_due_holdout_never_pushes(self) -> None:
        owner = top_level("probe-owner", "Probe", status="busy")
        owner["probe"] = True
        with patch.object(settle, "run_inventory", return_value=machine_inventory([owner])):
            added = settle.begin(LOGIN, only=[owner["session_id"]], request_id="probe-request",
                                 then="restart", alerts="user")
        self.assertEqual(len(added), 1)
        stored = record.find_session(LOGIN, owner["session_id"])
        assert stored is not None
        self.assertTrue(stored["probe"])
        self.assertEqual(stored["alerts"], "none")

        def mark_pause_sent(current: record.SessionShutdown) -> None:
            current["settle_message"] = {"kind": "sent", "at": NOW}
            current["reasked_at"] = NOW
        _ = record.update_session(LOGIN, owner["session_id"], mark_pause_sent)
        with (patch.object(settle, "now_utc", return_value=NOW_UTC + timedelta(hours=3)),
              patch.object(settle, "run_inventory", return_value=machine_inventory([owner])),
              patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")),
              patch.object(settle, "alert_user", return_value=True) as alert,
              patch.object(stop_work, "stop_session") as stop,
              patch.object(time, "sleep", side_effect=PassObserved),
              self.assertRaises(PassObserved)):
            _ = settle.conduct(LOGIN)
        alert.assert_not_called()
        stop.assert_not_called()
        current = record.find_session(LOGIN, owner["session_id"])
        assert current is not None
        self.assertNotIn("alerted_at", current)

    def test_begin_attaches_owned_workers_and_second_request_adds_only_new_owner(self) -> None:
        first, second = top_level("first", "First"), top_level("second", "Second")
        worker = worker_session("worker", "Worker", owner="first")
        orphan = worker_session("orphan", "Orphan", owner=None)
        with patch.object(settle, "run_inventory", return_value=machine_inventory([first, worker, orphan])):
            added = settle.begin(LOGIN, only=["first"], request_id="one")
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["workers"], [worker])
        original = record.find_session(LOGIN, "first")
        with patch.object(settle, "run_inventory", return_value=machine_inventory([first, second])):
            added = settle.begin(LOGIN, only=["first", "second"], request_id="two", then="restart")
        self.assertEqual([saved["session"]["session_id"] for saved in added], ["second"])
        self.assertEqual(record.find_session(LOGIN, "first"), original)
        self.assertIsNone(record.find_session(LOGIN, "orphan"))

    def test_begin_timer_stop_failure_takes_only_that_owner_out(self) -> None:
        first, second = top_level("first", "First", timers=["timer-first"]), top_level("second", "Second")
        def snapshot(owner: inventory.Session) -> list[record.TimerRestore]:
            return [record.TimerRestore(instance="timer-first", was_enabled=True)] if owner["session_id"] == "first" else []
        with (
            patch.object(settle, "run_inventory", return_value=machine_inventory([first, second])),
            patch.object(settle, "_notifier_enabled", return_value=True),
            patch.object(settle, "_session_timer_snapshot", side_effect=snapshot),
            patch.object(settle, "run_notifier", side_effect=[RuntimeError("stop failed"), None]) as notifier,
            patch.object(settle, "_retire_shutdown_messages"),
            redirect_stderr(io.StringIO()),
        ):
            added = settle.begin(LOGIN, only=["first", "second"], request_id="one")
        self.assertEqual([saved["session"]["session_id"] for saved in added], ["second"])
        self.assertIsNone(record.find_session(LOGIN, "first"))
        self.assertEqual(notifier.call_args_list, [call("stop", "timer-first"), call("start", "timer-first")])

    def test_down_starts_local_conductor_before_unreachable_peer_and_keeps_local_records(self) -> None:
        events: list[str] = []
        def launch(_login: str, _label: str) -> None:
            events.append("local conductor")
        def remote_call(arguments: list[str]) -> tuple[int, str]:
            events.append("peer down")
            self.assertEqual(arguments[0], "down")
            self.assertIn("--request-id", arguments)
            return 255, ""
        with (
            patch.object(settle, "_warm", return_value=True),
            patch.object(settle, "run_inventory", return_value=machine_inventory([top_level("owner", "Owner")])),
            patch.object(settle, "_launch_conductor", side_effect=launch),
            patch.object(settle, "run_remote", side_effect=remote_call),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(settle.down(Account("claude", LOGIN, LABEL)), 0)
        self.assertEqual(events, ["local conductor", "peer down"])
        self.assertIsNotNone(record.find_session(LOGIN, "owner"))

    def test_down_resumes_conductor_when_all_requested_sessions_already_have_records(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"))
        self.save(saved)
        before = record.find_session(LOGIN, "owner")
        for tty in (False, True):
            output = io.StringIO()
            with (
                self.subTest(tty=tty),
                patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}),
                patch.object(settle, "_warm", return_value=True),
                patch.object(settle, "run_inventory", return_value=machine_inventory([saved["session"]])),
                patch.object(settle, "_launch_conductor") as launch,
                patch.object(shutdown, "follow", return_value=0) as follow,
                patch.object(output, "isatty", return_value=tty),
                redirect_stdout(output),
            ):
                self.assertEqual(settle.down(Account("claude", LOGIN, LABEL), True, ["owner"],
                    then="restart", request_id="unused-new-request"), 0)
            launch.assert_called_once_with(LOGIN, LABEL)
            self.assertEqual(record.find_session(LOGIN, "owner"), before)
            if tty:
                follow.assert_called_once_with(LOGIN, saved["request_id"])
            else:
                follow.assert_not_called()
                self.assertIn(f"follow {LOGIN} --request {saved['request_id']}", output.getvalue())
                self.assertNotIn("unused-new-request", output.getvalue())
                self.assertIn(f"cancel {LOGIN} --here --only owner", output.getvalue())

    def test_cancel_keeps_stopped_and_stopping_records_and_their_timers(self) -> None:
        stopped = session_shutdown(top_level("stopped", "Stopped"), progress={"kind": "stopped", "at": NOW})
        stopping = session_shutdown(top_level("stopping", "Stopping"), progress={"kind": "stopping", "at": NOW})
        waiting = session_shutdown(top_level("waiting", "Waiting"))
        for saved in (stopped, stopping, waiting):
            saved["timers"] = [record.TimerRestore(instance=saved["session"]["session_id"], was_enabled=True)]
            self.save(saved)
        with patch.object(settle, "run_notifier") as notifier, patch.object(settle, "_retire_shutdown_messages"):
            self.assertEqual(settle.cancel(Account("claude", LOGIN, LABEL), True), 1)
        notifier.assert_called_once_with("start", "waiting")
        self.assertEqual(record.find_session(LOGIN, "stopped"), stopped)
        self.assertEqual(record.find_session(LOGIN, "stopping"), stopping)
        self.assertIsNone(record.find_session(LOGIN, "waiting"))

    def test_cancel_after_stop_claim_leaves_paused_worker_and_timers_intact(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"), progress={"kind": "ready", "at": NOW})
        saved["timers"] = [record.TimerRestore(instance="timer", was_enabled=True)]
        self.save(saved)
        entered, release = threading.Event(), threading.Event()
        def kill(_pid: int, _signal: int) -> None:
            entered.set()
            self.assertTrue(release.wait(1))
        errors: list[Exception] = []
        def worker() -> None:
            try:
                _ = stop_work.stop_session(saved, kill=kill, is_alive=lambda _pid: False,
                    session_record_exists=lambda _pid: False,
                    fresh_inventory=lambda _login, _only: machine_inventory([saved["session"]]),
                    clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0,  sleep=lambda _seconds: None)
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            with patch.object(settle, "run_notifier") as notifier:
                self.assertEqual(settle.cancel(Account("claude", LOGIN, LABEL), True), 1)
                notifier.assert_not_called()
            current = record.find_session(LOGIN, "owner")
            self.assertIsNotNone(current)
            if current is not None:
                self.assertEqual(current["progress"]["kind"], "stopping")
        finally:
            release.set()
            thread.join(1)
        self.assertEqual(errors, [])

    def test_now_marks_a_failed_stop_for_its_conductor(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"), progress={"kind": "stop failed", "at": NOW, "reason": "still running"})
        self.save(saved)
        with patch.object(stop_work, "stop_session") as stop, patch.object(settle, "_launch_conductor") as launch:
            self.assertEqual(settle.now(Account("claude", LOGIN, LABEL), True), 0)
        stop.assert_not_called()
        current = record.find_session(LOGIN, "owner")
        self.assertIsNotNone(current)
        if current is not None:
            self.assertEqual(current["force"], "now")
        launch.assert_called_once_with(LOGIN, LABEL)

    def test_now_conductor_retries_failed_stops_together_before_reporting(self) -> None:
        for session_id in ("first", "second"):
            self.save(session_shutdown(top_level(session_id, session_id), progress={"kind": "stop failed", "at": NOW, "reason": "still running"}))
        entered = threading.Barrier(2)
        completed: list[str] = []
        def stop(saved: record.SessionShutdown) -> record.SessionShutdown:
            self.assertEqual(saved["force"], "now")
            _ = entered.wait(2)
            result = self.fake_stop(saved)
            completed.append(saved["session"]["session_id"])
            return result
        def report(_records: list[record.SessionShutdown]) -> None:
            self.assertCountEqual(completed, ["first", "second"])
        owners = [saved["session"] for saved in record.session_records(LOGIN)]
        with patch.object(stop_work, "stop_session", side_effect=ignoring_idle_reason(stop)), patch.object(settle, "_launch_conductor", side_effect=self.run_conductor), patch.object(settle, "run_inventory", return_value=machine_inventory(owners)), patch.object(settle, "report_stop", side_effect=report):
            self.assertEqual(settle.now(Account("claude", LOGIN, LABEL), True), 0)
        self.assertEqual([saved["progress"]["kind"] for saved in record.session_records(LOGIN)], ["stopped", "stopped"])

    @staticmethod
    def all_live(ids: frozenset[str], _timeout: float) -> restart_work.RegistryConfirmation:
        return registry_confirmation(ids)

    def fake_stop(self, saved: record.SessionShutdown) -> record.SessionShutdown:
        def stop(current: record.SessionShutdown) -> None:
            current["progress"] = {"kind": "stopped", "at": NOW}
        return record.update_session(LOGIN, saved["session"]["session_id"], stop)

    def fake_launch(self, saved: record.SessionShutdown, *, dry_run: bool, desktop_state: restart_work.DesktopLaunchState) -> record.SessionShutdown:
        del desktop_state
        self.assertFalse(dry_run)
        def launch(current: record.SessionShutdown) -> None:
            prior = current["progress"]
            if prior["kind"] == "restarting":
                prior = prior["prior"]
            current["progress"] = {"kind": "restarting", "at": NOW, "prior": prior}
        return record.update_session(LOGIN, saved["session"]["session_id"], launch)

    def fake_confirm(self, saved: record.SessionShutdown, _at: str, live: bool, *, observed_at: float) -> None:
        del observed_at
        self.assertTrue(live)
        saved["progress"] = {"kind": "restarted", "at": NOW}
        def confirm(current: record.SessionShutdown) -> None:
            current["progress"] = saved["progress"]
        _ = record.update_session(LOGIN, saved["session"]["session_id"], confirm)

    def test_form_blocked_owner_holds_while_two_others_stop_and_return_in_one_pass(self) -> None:
        owners: list[inventory.Session] = [top_level(name, name) for name in ("blocked", "first", "second")]
        for owner in owners:
            self.save(session_shutdown(owner, progress={"kind": "ready", "at": NOW}, then="restart"))
        def end_pass(_seconds: float) -> None:
            self.assertIsNone(record.find_session(LOGIN, "first"))
            self.assertIsNone(record.find_session(LOGIN, "second"))
            blocked = record.find_session(LOGIN, "blocked")
            self.assertIsNotNone(blocked)
            if blocked is not None:
                self.assertEqual(blocked["progress"]["kind"], "ready")
            raise PassObserved()
        def form(owner: inventory.Session) -> bool:
            return owner["session_id"] == "blocked"
        with (
            patch.object(settle, "run_inventory", return_value=machine_inventory(owners)),
            patch.object(settle, "_showing_form", side_effect=form),
            patch.object(stop_work, "stop_session", side_effect=ignoring_idle_reason(self.fake_stop)),
            patch.object(restart_work, "relaunch_session", side_effect=self.fake_launch),
            patch.object(restart_work, "wait_for_confirmation", side_effect=self.all_live) as wait,
            patch.object(restart_work, "confirm_entry", side_effect=self.fake_confirm),
            patch.object(time, "sleep", side_effect=end_pass),
            self.assertRaises(PassObserved),
        ):
            _ = settle.conduct(LOGIN)
        confirmed: list[str] = []
        for call in wait.call_args_list:
            ids = cast(frozenset[str], call.args[0])
            self.assertEqual(call.args[1], restart_work.SESSION_WAIT_SECONDS)
            confirmed.extend(ids)
        self.assertCountEqual(confirmed, ["first", "second"])

    def test_conductor_archives_restarted_without_launching_and_clears_only_its_hold(self) -> None:
        done = session_shutdown(top_level("done", "Done"), progress={"kind": "restarted", "at": NOW})
        self.save(done)
        hold = record.AccountHold(login=LOGIN, label=LABEL, machine="natedev", requested_at=NOW,
                                  requested_by={"kind": "terminal"}, request_id="request-one", then="restart")
        record.set_account_hold(hold)
        with patch.object(settle, "report_stop"), patch.object(restart_work, "relaunch_session") as launch:
            self.assertEqual(settle.conduct(LOGIN), 0)
        launch.assert_not_called()
        self.assertEqual(record.session_records(LOGIN), [])
        self.assertIsNone(record.account_hold(LOGIN))

    def test_conductor_confirmation_error_leaves_other_session_restarted_and_archived(self) -> None:
        owners: list[inventory.Session] = []
        for name in ("first", "second"):
            owner = top_level(name, name)
            owner["host"] = inventory.TerminalHost(kind="terminal")
            owners.append(owner)
            self.save(session_shutdown(owner, progress={"kind": "stopped", "at": NOW}, then="restart"))
        def confirm(saved: record.SessionShutdown, at: str, live: bool, *, observed_at: float) -> None:
            if saved["session"]["session_id"] == "second":
                raise OSError("confirm failed")
            self.fake_confirm(saved, at, live, observed_at=observed_at)
        with (
            patch.object(settle, "run_inventory", return_value=machine_inventory(owners)),
            patch.object(restart_work, "relaunch_session", side_effect=self.fake_launch),
            patch.object(restart_work, "wait_for_confirmation", side_effect=self.all_live) as registry,
            patch.object(restart_work, "confirm_entry", side_effect=confirm),
            patch.object(restart_work, "wait_for_remote_control", return_value=frozenset({"first"})) as control,
            patch.object(settle, "report_stop") as report,
        ):
            self.assertEqual(settle.conduct(LOGIN), 0)
        registry.assert_called_once_with(frozenset({"first", "second"}), restart_work.SESSION_WAIT_SECONDS)
        control.assert_called_once()
        checked = cast(tuple[sessions.SessionRecord, ...], cast(object, control.call_args.args[0]))
        self.assertEqual([item["sessionId"] for item in checked], ["first"])
        self.assertIsNone(record.find_session(LOGIN, "first"))
        failed = record.find_session(LOGIN, "second")
        assert failed is not None
        self.assertEqual(failed["progress"], {"kind": "restart failed", "at": NOW, "reason": "confirm failed"})
        outcomes = cast(list[record.SessionShutdown], cast(object, report.call_args.args[0]))
        self.assertEqual(next(item for item in outcomes if item["session"]["session_id"] == "first")["progress"]["kind"], "restarted")

    def test_queued_settle_send_skips_cancelled_request_and_sends_replacement_instruction(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"))
        self.save(saved)
        entered, release = threading.Event(), threading.Event()
        def pause() -> None:
            entered.set()
            self.assertTrue(release.wait(2))
        with patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")) as sender:
            with ThreadPoolExecutor(max_workers=1) as pool:
                blocker = pool.submit(pause)
                self.assertTrue(entered.wait(2))
                queued = pool.submit(settle._send_settle_message, saved)  # pyright: ignore[reportPrivateUsage]
                try:
                    _ = record.withdraw_session(LOGIN, "owner", lambda _saved: None)
                    replacement = session_shutdown(saved["session"], request_id="replacement")
                    self.save(replacement)
                finally:
                    release.set()
                blocker.result()
                queued.result()
                sender.assert_not_called()
                self.assertEqual(record.find_session(LOGIN, "owner"), replacement)
                pool.submit(settle._send_settle_message, replacement).result()  # pyright: ignore[reportPrivateUsage]
            sender.assert_called_once()
        latest = record.find_session(LOGIN, "owner")
        assert latest is not None
        self.assertEqual(latest["settle_message"], {"kind": "sent", "at": NOW})

    def test_confirmation_skips_missing_record_and_checks_other_session_remote_control(self) -> None:
        batch: list[record.SessionShutdown] = []
        for name in ("missing", "present"):
            saved = session_shutdown(top_level(name, name), progress={"kind": "restarting", "at": NOW,
                "prior": {"kind": "stopped", "at": NOW}}, then="restart")
            self.save(saved)
            batch.append(saved)
        def confirm(saved: record.SessionShutdown, at: str, live: bool, *, observed_at: float) -> None:
            if saved["session"]["session_id"] == "missing":
                record.archive_session(LOGIN, "missing")
                raise record.NoLiveRecord("gone")
            self.fake_confirm(saved, at, live, observed_at=observed_at)
        with (
            patch.object(restart_work, "wait_for_confirmation", side_effect=self.all_live),
            patch.object(restart_work, "confirm_entry", side_effect=confirm),
            patch.object(restart_work, "wait_for_remote_control", return_value=frozenset({"present"})) as control,
        ):
            _ = settle._confirm_relaunch_pass(batch)  # pyright: ignore[reportPrivateUsage]
        checked = cast(tuple[sessions.SessionRecord, ...], cast(object, control.call_args.args[0]))
        self.assertEqual([item["sessionId"] for item in checked], ["present"])
        latest = record.find_session(LOGIN, "present")
        assert latest is not None
        self.assertEqual(latest["progress"]["kind"], "restarted")

    def test_conductor_liveness_wait_error_journals_each_target_move_failure(self) -> None:
        owners: list[inventory.Session] = []
        for name in ("first", "second"):
            owner = top_level(name, name)
            owner["host"] = inventory.TerminalHost(kind="terminal")
            owners.append(owner)
            saved = session_shutdown(owner, progress={"kind": "stopped", "at": NOW}, then="restart")
            saved["target"] = "claude3"
            self.save(saved)
            requested: moves.RequestedMove = {"kind": "requested", "at": NOW, "tool": "claude",
                "subject": name + ": " + name, "move_id": saved["request_id"], "from": LABEL, "to": "claude3",
                "why": "weekly refusal", "window": "weekly"}
            moves.append_move(requested)
        with (
            patch.object(settle, "run_inventory", return_value=machine_inventory(owners)),
            patch.object(restart_work, "relaunch_session", side_effect=self.fake_launch),
            patch.object(restart_work, "wait_for_confirmation", side_effect=OSError("registry unreadable")),
            patch.object(restart_work, "confirm_entry") as confirm,
            patch.object(restart_work, "wait_for_remote_control") as control,
            patch.object(settle, "report_stop"),
        ):
            self.assertEqual(settle.conduct(LOGIN), 0)
        confirm.assert_not_called()
        control.assert_not_called()
        self.assertEqual([saved["progress"] for saved in record.session_records(LOGIN)],
            [{"kind": "restart failed", "at": NOW, "reason": "registry unreadable"}] * 2)

        from scripts.accounts import accounts
        entries = moves.read_moves()
        failed = [entry for entry in entries if entry["kind"] == "failed"]
        self.assertEqual({entry["subject"] for entry in failed}, {"first: first", "second: second"})
        self.assertTrue(all(entry["move_id"] == "request-one" and entry["reason"] == "registry unreadable" for entry in failed))
        readout = "\n".join(accounts._move_status_lines(entries, timezone.utc))  # pyright: ignore[reportPrivateUsage]
        self.assertIn("failed: registry unreadable", readout)
        self.assertNotIn("pending", readout)

    def test_settle_delivery_does_not_update_request_replaced_during_send(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"))
        replacement = session_shutdown(saved["session"], request_id="replacement")
        self.save(saved)
        def deliver(*_args: object, **_kwargs: object) -> settle.MessageSent:
            _ = record.withdraw_session(LOGIN, "owner", lambda _saved: None)
            self.save(replacement)
            return settle.MessageSent(kind="sent")
        with patch.object(settle, "send_message", side_effect=deliver):
            settle._send_settle_message(saved)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(record.find_session(LOGIN, "owner"), replacement)

    def test_settle_send_skips_absent_or_no_longer_waiting_record(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"))
        with patch.object(settle, "send_message") as sender:
            settle._send_settle_message(saved)  # pyright: ignore[reportPrivateUsage]
            ready = session_shutdown(saved["session"], progress={"kind": "ready", "at": NOW})
            self.save(ready)
            settle._send_settle_message(saved)  # pyright: ignore[reportPrivateUsage]
        sender.assert_not_called()
        self.assertEqual(record.find_session(LOGIN, "owner"), ready)

    def test_conductor_does_not_confirm_a_stale_claim_lost_to_another_caller(self) -> None:
        old = (NOW_UTC - timedelta(seconds=121)).isoformat(timespec="seconds")
        saved = session_shutdown(top_level("owner", "Owner"), progress={
            "kind": "restarting", "at": old, "prior": {"kind": "stopped", "at": old},
        }, then="restart")
        self.save(saved)
        def lose(current: record.SessionShutdown, *, dry_run: bool, desktop_state: restart_work.DesktopLaunchState) -> record.SessionShutdown:
            del dry_run, desktop_state
            def other_claim(changing: record.SessionShutdown) -> None:
                progress = changing["progress"]
                if progress["kind"] == "restarting":
                    changing["progress"] = {"kind": "restarting", "at": NOW, "prior": progress["prior"]}
            _ = record.update_session(LOGIN, "owner", other_claim)
            return current
        with (
            patch.object(settle, "run_inventory", return_value=machine_inventory([saved["session"]])),
            patch.object(restart_work, "relaunch_session", side_effect=lose),
            patch.object(restart_work, "wait_for_confirmation") as wait,
            patch.object(restart_work, "confirm_entry") as confirm,
            patch.object(time, "sleep", side_effect=PassObserved()),
            self.assertRaises(PassObserved),
        ):
            _ = settle.conduct(LOGIN)
        wait.assert_not_called()
        confirm.assert_not_called()
        latest = record.find_session(LOGIN, "owner")
        self.assertIsNotNone(latest)
        if latest is not None:
            self.assertEqual(latest["progress"], {"kind": "restarting", "at": NOW, "prior": {"kind": "stopped", "at": old}})

    def test_conductor_launches_eighth_before_waiting_for_missing_registry_entries(self) -> None:
        owners: list[inventory.Session] = [top_level(str(index), str(index)) for index in range(8)]
        for owner in owners:
            self.save(session_shutdown(owner, progress={"kind": "stopped", "at": NOW}, then="restart"))
        launches: list[str] = []
        def launch(saved: record.SessionShutdown, *, dry_run: bool, desktop_state: restart_work.DesktopLaunchState) -> record.SessionShutdown:
            launches.append(saved["session"]["session_id"])
            return self.fake_launch(saved, dry_run=dry_run, desktop_state=desktop_state)
        def wait(ids: frozenset[str], _timeout: float) -> restart_work.RegistryConfirmation:
            self.assertCountEqual(launches, [str(index) for index in range(8)])
            self.assertEqual(ids, frozenset(launches))
            return registry_confirmation(frozenset({"7"}))
        def confirm(saved: record.SessionShutdown, _at: str, live: bool, *, observed_at: float) -> None:
            del observed_at
            saved["progress"] = ({"kind": "restarted", "at": NOW} if live
                                 else {"kind": "restart failed", "at": NOW, "reason": "not live"})
            def save(current: record.SessionShutdown) -> None:
                current["progress"] = saved["progress"]
            _ = record.update_session(LOGIN, saved["session"]["session_id"], save)
        with (
            patch.object(settle, "run_inventory", return_value=machine_inventory(owners)),
            patch.object(restart_work, "relaunch_session", side_effect=launch),
            patch.object(restart_work, "wait_for_confirmation", side_effect=wait) as registry,
            patch.object(restart_work, "confirm_entry", side_effect=confirm),
            patch.object(settle, "report_stop"),
        ):
            self.assertEqual(settle.conduct(LOGIN), 0)
        registry.assert_called_once()
        self.assertIsNone(record.find_session(LOGIN, "7"))
        self.assertEqual(len(record.session_records(LOGIN)), 7)
        self.assertTrue(all(saved["progress"]["kind"] == "restart failed" for saved in record.session_records(LOGIN)))

    def test_unreadable_account_keeps_record_held_without_stopping_or_sending(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"), progress={"kind": "ready", "at": NOW})
        self.save(saved)
        report = machine_inventory([], unattributed=[inventory.UnattributedSession(pid=saved["session"]["pid"], name=saved["session"]["name"], reason="account unreadable")])
        with (
            patch.object(settle, "run_inventory", return_value=report),
            patch.object(stop_work, "stop_session") as stop,
            patch.object(settle, "send_message") as send,
            patch.object(time, "sleep", side_effect=PassObserved()),
            self.assertRaises(PassObserved),
        ):
            _ = settle.conduct(LOGIN)
        stop.assert_not_called()
        send.assert_not_called()
        self.assertEqual(record.find_session(LOGIN, "owner"), saved)

    def test_second_conductor_returns_without_observing_or_advancing_records(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"))
        self.save(saved)
        with record.conductor_lock(LOGIN), patch.object(settle, "run_inventory") as observe:
            self.assertEqual(settle.conduct(LOGIN), 0)
        observe.assert_not_called()
        self.assertEqual(record.find_session(LOGIN, "owner"), saved)

    @contextmanager
    def conductor_modules(self) -> Generator[dict[str, Path], None, None]:
        directory = self.root / "conductor-modules"
        directory.mkdir(exist_ok=True)
        modules = {name: directory / name for name in (
            "shutdown.py", "settle.py", "stop.py", "record.py", "inventory.py", "restart.py")}
        for path in modules.values():
            _ = path.write_text("# original module\n")
        with patch.object(settle, "__file__", str(modules["settle.py"])):
            yield modules

    def test_conductor_changed_module_reexecs_service_argv_after_unlock_without_new_steps(self) -> None:
        self.allow_leaf(ShutdownSendLeaf.LAUNCH_CONDUCTOR)
        with (patch.object(sys, "platform", "linux"),
              patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as service):
            settle._launch_conductor(LOGIN, LABEL)  # pyright: ignore[reportPrivateUsage]
        service_command = cast(list[str], service.call_args.args[0])
        python_index = service_command.index(sys.executable)
        argv = service_command[python_index + 2:]
        self.assertEqual(service_command[python_index + 1], "-u")
        self.assertEqual(argv, [str(settle.SHUTDOWN), "conduct", LOGIN])
        choose_step = settle.next_step
        changes = ((name, field) for name in (
            "shutdown.py", "settle.py", "stop.py", "record.py", "inventory.py", "restart.py")
            for field in ("mtime", "size"))
        for name, field in changes:
            with self.subTest(module=name, field=field), self.conductor_modules() as modules:
                identity = f"reload-{Path(name).stem}-{field}"
                owner = top_level(identity, "Waiting", status="busy")
                saved = session_shutdown(owner, progress={"kind": "ready", "at": NOW})
                self.save(saved)
                fresh_owner = top_level(identity, "Waiting", status="busy")
                changed = False
                output = io.StringIO()

                def install(_seconds: float) -> None:
                    nonlocal changed
                    self.assertFalse(changed, "conductor did not notice the changed module on its next pass")
                    path = modules[name]
                    metadata = path.stat()
                    if field == "mtime":
                        os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000_000))
                    else:
                        _ = path.write_text("# replacement module with a different size\n")
                        os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
                    changed = True
                    fresh_owner["status"] = "idle"

                def step(current: record.SessionShutdown, fresh: stop_work.FreshSession,
                         others: list[record.SessionShutdown], at: datetime) -> settle.Step:
                    self.assertFalse(changed, "a new step was selected with obsolete code")
                    return choose_step(current, fresh, others, at)

                def reexec(executable: str, arguments: list[str]) -> None:
                    self.assertEqual((executable, arguments), (sys.executable, [sys.executable, "-u", *argv]))
                    with record.conductor_lock(LOGIN) as ownership:
                        self.assertIsInstance(ownership, record.ConductorAcquired)
                    restore.assert_called_once_with(dry_run=False)
                    raise PassObserved()

                with (
                    patch.object(sys, "argv", argv),
                    patch.object(settle, "run_inventory", return_value=machine_inventory([fresh_owner])),
                    patch.object(settle, "next_step", side_effect=step) as selected,
                    patch.object(time, "sleep", side_effect=install),
                    patch.object(restart_work.DesktopLaunchState, "restore") as restore,
                    patch.object(os, "execv", side_effect=reexec) as execute,
                    patch.object(stop_work, "stop_session") as stop,
                    patch.object(settle, "report_stop") as report,
                    redirect_stdout(output),
                    self.assertRaises(PassObserved),
                ):
                    _ = settle.conduct(LOGIN)
                execute.assert_called_once()
                selected.assert_called_once()
                stop.assert_not_called()
                report.assert_not_called()
                self.assertEqual(output.getvalue().count("shutdown modules changed"), 1)
                self.assertIn(name, output.getvalue())
                self.assertEqual(record.find_session(LOGIN, owner["session_id"]), saved)
                record.archive_session(LOGIN, owner["session_id"])

    def test_conductor_reload_drains_and_consumes_launch_stop_message_alert_and_registry_work(self) -> None:
        for stage in ("launch", "stop", "message", "alert", "confirmation"):
            with self.subTest(stage=stage), self.conductor_modules() as modules:
                owner = top_level("draining-" + stage, "Draining", status="busy" if stage in {"message", "alert"} else "idle")
                progress: record.SessionProgress = ({"kind": "stopped", "at": NOW} if stage in {"launch", "confirmation"}
                    else {"kind": "ready", "at": NOW} if stage == "stop" else {"kind": "waiting"})
                saved = session_shutdown(owner, progress=progress,
                    then="restart" if stage in {"launch", "confirmation"} else "stop")
                if stage == "confirmation":
                    owner["host"] = inventory.TerminalHost(kind="terminal")
                if stage == "alert":
                    saved["settle_message"] = {"kind": "sent", "at": NOW}
                    saved["reasked_at"] = NOW
                self.save(saved)
                jobs: list[tuple[ConsumptionObservedFuture[object], Callable[[], None]]] = []
                ticks = 0
                changed = False
                choose_step = settle.next_step

                def submit[**P, R](executor: ThreadPoolExecutor, function: Callable[P, R], /,
                                   *args: P.args, **kwargs: P.kwargs) -> Future[R]:
                    del executor
                    future = ConsumptionObservedFuture[R]()
                    _ = future.set_running_or_notify_cancel()

                    def finish() -> None:
                        try:
                            future.set_result(function(*args, **kwargs))
                        except BaseException as error:
                            future.set_exception(error)

                    jobs.append((cast(ConsumptionObservedFuture[object], cast(object, future)), finish))
                    return future

                def advance() -> None:
                    nonlocal ticks, changed
                    ticks += 1
                    self.assertLess(ticks, 8, "reload failed to finish draining")
                    execute.assert_not_called()
                    self.assertTrue(jobs, "test never put an action in flight")
                    if ticks == 1:
                        _ = modules["restart.py"].write_text("# replacement restart module\n")
                        changed = True
                    else:
                        if ticks == 2:
                            self.assertTrue(all(not future.done() for future, _finish in jobs))
                        for future, finish in list(jobs):
                            if not future.done():
                                finish()

                def sleep(_seconds: float) -> None:
                    advance()

                def wait(futures: list[Future[object]], *, timeout: float,
                         return_when: str) -> tuple[set[Future[object]], set[Future[object]]]:
                    del timeout, return_when
                    advance()
                    return ({future for future in futures if future.done()},
                            {future for future in futures if not future.done()})

                def step(current: record.SessionShutdown, fresh: stop_work.FreshSession,
                         others: list[record.SessionShutdown], at: datetime) -> settle.Step:
                    self.assertFalse(changed, "reload started another record step")
                    return choose_step(current, fresh, others, at)

                def reexec(_executable: str, _arguments: list[str]) -> None:
                    self.assertGreaterEqual(ticks, 2)
                    self.assertTrue(all(future.done() and future.consumed for future, _finish in jobs),
                                    "exec lost an owned action's result")
                    latest = record.find_session(LOGIN, owner["session_id"])
                    if stage in {"launch", "confirmation"}:
                        self.assertIsNone(latest, "registry result was not archived before reload")
                    else:
                        assert latest is not None
                        if stage == "stop":
                            self.assertEqual(latest["progress"]["kind"], "stopped")
                        elif stage == "message":
                            self.assertEqual(latest["settle_message"]["kind"], "sent")
                        else:
                            self.assertIn("alerted_at", latest)
                            alert.assert_called_once()
                    raise PassObserved()

                with (
                    patch.object(ThreadPoolExecutor, "submit", new=submit),
                    patch.object(settle, "run_inventory", return_value=machine_inventory([owner])),
                    patch.object(settle, "now_utc", return_value=NOW_UTC + (settle.NOT_ANSWERING_AFTER if stage == "alert" else timedelta())),
                    patch.object(settle, "next_step", side_effect=step),
                    patch.object(settle, "wait", side_effect=wait),
                    patch.object(time, "sleep", side_effect=sleep),
                    patch.object(stop_work, "stop_session", side_effect=ignoring_idle_reason(self.fake_stop)),
                    patch.object(restart_work, "relaunch_session", side_effect=self.fake_launch),
                    patch.object(restart_work, "wait_for_confirmation", side_effect=self.all_live),
                    patch.object(restart_work, "confirm_entry", side_effect=self.fake_confirm),
                    patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")),
                    patch.object(settle, "alert_user", return_value=True) as alert,
                    patch.object(settle, "report_stop"),
                    patch.object(os, "execv", side_effect=reexec) as execute,
                    self.assertRaises(PassObserved),
                ):
                    _ = settle.conduct(LOGIN)
                execute.assert_called_once()
                if record.find_session(LOGIN, owner["session_id"]) is not None:
                    record.archive_session(LOGIN, owner["session_id"])
                (self.root / "state" / LOGIN / "conduct_archive.json").unlink(missing_ok=True)

    def test_conductor_unchanged_modules_finish_without_reexec(self) -> None:
        owner = top_level("same-code", "Same code")
        self.save(session_shutdown(owner, progress={"kind": "ready", "at": NOW}, then="restart"))
        with (
            self.conductor_modules(),
            patch.object(settle, "run_inventory", return_value=machine_inventory([owner])),
            patch.object(stop_work, "stop_session", side_effect=ignoring_idle_reason(self.fake_stop)) as stop,
            patch.object(restart_work, "relaunch_session", side_effect=self.fake_launch) as launch,
            patch.object(restart_work, "wait_for_confirmation", side_effect=self.all_live),
            patch.object(restart_work, "confirm_entry", side_effect=self.fake_confirm),
            patch.object(settle, "report_stop") as report,
            patch.object(os, "execv", side_effect=AssertionError("unchanged code re-executed")) as execute,
        ):
            self.assertEqual(settle.conduct(LOGIN), 0)
        stop.assert_called_once()
        launch.assert_called_once()
        execute.assert_not_called()
        outcomes = cast(list[record.SessionShutdown], report.call_args.args[0])
        self.assertEqual([(saved["session"]["session_id"], saved["progress"]["kind"]) for saved in outcomes],
                         [("same-code", "restarted")])

    def test_conductor_reload_keeps_archived_outcomes_and_never_launches_an_owner_twice(self) -> None:
        first, second = top_level("before-reload", "Before"), top_level("after-reload", "After")
        self.save(session_shutdown(first, progress={"kind": "stopped", "at": NOW}, then="restart"))
        self.save(session_shutdown(second, progress={"kind": "ready", "at": NOW}, then="restart"))
        archive_path = self.root / "state" / LOGIN / "conduct_archive.json"
        reloaded = False

        def form(owner: inventory.Session) -> bool:
            return owner["session_id"] == "after-reload" and not reloaded

        with self.conductor_modules() as modules:
            def install(_seconds: float) -> None:
                self.assertIsNone(record.find_session(LOGIN, "before-reload"))
                _ = modules["record.py"].write_text("# replacement record module\n")

            def reexec(_executable: str, _arguments: list[str]) -> None:
                self.assertTrue(archive_path.is_file())
                self.assertIn("before-reload", archive_path.read_text())
                self.assertIsNone(record.find_session(LOGIN, "before-reload"))
                report.assert_not_called()
                raise PassObserved()

            with (
                patch.object(settle, "run_inventory", return_value=machine_inventory([first, second])),
                patch.object(settle, "_showing_form", side_effect=form),
                patch.object(stop_work, "stop_session", side_effect=ignoring_idle_reason(self.fake_stop)) as stop,
                patch.object(restart_work, "relaunch_session", side_effect=self.fake_launch) as launch,
                patch.object(restart_work, "wait_for_confirmation", side_effect=self.all_live),
                patch.object(restart_work, "confirm_entry", side_effect=self.fake_confirm),
                patch.object(settle, "report_stop") as report,
                patch.object(os, "execv", side_effect=reexec) as execute,
            ):
                with patch.object(time, "sleep", side_effect=install), self.assertRaises(PassObserved):
                    _ = settle.conduct(LOGIN)
                execute.assert_called_once()
                reloaded = True
                self.assertEqual(settle.conduct(LOGIN), 0)
        execute.assert_called_once()
        report.assert_called_once()
        outcomes = cast(list[record.SessionShutdown], report.call_args.args[0])
        self.assertCountEqual([(saved["session"]["session_id"], saved["progress"]["kind"]) for saved in outcomes],
                              [("before-reload", "restarted"), ("after-reload", "restarted")])
        self.assertCountEqual([cast(record.SessionShutdown, item.args[0])["session"]["session_id"]
                               for item in launch.call_args_list], ["before-reload", "after-reload"])
        stop.assert_called_once()
        self.assertFalse(archive_path.exists(), "the new conductor did not consume its saved outcomes")

    def test_conductor_unreadable_module_pass_keeps_outcomes_until_complete_reload(self) -> None:
        first = top_level("before-install-gap", "Before install gap")
        second = top_level("after-install-gap", "After install gap")
        self.save(session_shutdown(first, progress={"kind": "stopped", "at": NOW}, then="restart"))
        self.save(session_shutdown(second, progress={"kind": "ready", "at": NOW}, then="restart"))
        archive_path = self.root / "state" / LOGIN / "conduct_archive.json"
        reloaded = False
        install_passes = 0

        def form(owner: inventory.Session) -> bool:
            return owner["session_id"] == "after-install-gap" and not reloaded

        with self.conductor_modules() as modules:
            def install(_seconds: float) -> None:
                nonlocal install_passes
                install_passes += 1
                self.assertLessEqual(install_passes, 2, "complete changed fingerprint did not request reload")
                self.assertIsNone(record.find_session(LOGIN, "before-install-gap"))
                execute.assert_not_called()
                report.assert_not_called()
                if install_passes == 1:
                    modules["stop.py"].unlink()
                else:
                    self.assertFalse(modules["stop.py"].exists())
                    self.assertFalse(archive_path.exists(), "unreadable module prematurely requested reload")
                    _ = modules["stop.py"].write_text("# replacement stop module after install gap\n")

            def reexec(_executable: str, _arguments: list[str]) -> None:
                self.assertEqual(install_passes, 2)
                self.assertTrue(modules["stop.py"].is_file())
                handed_off = record.parse_session_records(archive_path.read_text())
                self.assertEqual([saved["session"]["session_id"] for saved in handed_off], ["before-install-gap"])
                stop.assert_not_called()
                report.assert_not_called()
                raise PassObserved()

            with (
                patch.object(settle, "run_inventory", return_value=machine_inventory([first, second])),
                patch.object(settle, "_showing_form", side_effect=form),
                patch.object(stop_work, "stop_session", side_effect=ignoring_idle_reason(self.fake_stop)) as stop,
                patch.object(restart_work, "relaunch_session", side_effect=self.fake_launch) as launch,
                patch.object(restart_work, "wait_for_confirmation", side_effect=self.all_live),
                patch.object(restart_work, "confirm_entry", side_effect=self.fake_confirm),
                patch.object(settle, "report_stop") as report,
                patch.object(os, "execv", side_effect=reexec) as execute,
            ):
                with patch.object(time, "sleep", side_effect=install), self.assertRaises(PassObserved):
                    _ = settle.conduct(LOGIN)
                execute.assert_called_once()
                reloaded = True
                self.assertEqual(settle.conduct(LOGIN), 0)
        execute.assert_called_once()
        report.assert_called_once()
        outcomes = cast(list[record.SessionShutdown], report.call_args.args[0])
        self.assertCountEqual([(saved["session"]["session_id"], saved["progress"]["kind"]) for saved in outcomes],
                             [("before-install-gap", "restarted"), ("after-install-gap", "restarted")])
        self.assertCountEqual([cast(record.SessionShutdown, call.args[0])["session"]["session_id"]
                              for call in launch.call_args_list], ["before-install-gap", "after-install-gap"])
        stop.assert_called_once()
        self.assertFalse(archive_path.exists())

    def test_running_stop_does_not_prevent_a_new_inventory_or_another_ready_owner(self) -> None:
        slow = session_shutdown(top_level("slow", "Slow"), progress={"kind": "ready", "at": NOW})
        fast = session_shutdown(top_level("fast", "Fast"))
        await_ready_mark(fast)
        self.save(slow)
        self.save(fast)
        entered, release, fast_done = threading.Event(), threading.Event(), threading.Event()
        elapsed = [0.0]
        calls = [0]
        def stop(saved: record.SessionShutdown) -> record.SessionShutdown:
            if saved["session"]["session_id"] == "slow":
                entered.set()
                self.assertTrue(release.wait(1))
            result = self.fake_stop(saved)
            if saved["session"]["session_id"] == "fast":
                self.assertEqual(saved["progress"]["kind"], "ready")
                self.assertFalse(release.is_set())
                fast_done.set()
            return result
        def tick(futures: list[Future[object]], *, timeout: float, return_when: str) -> tuple[set[Future[object]], set[Future[object]]]:
            del timeout
            calls[0] += 1
            if calls[0] == 1:
                self.assertTrue(entered.wait(1))
                def ready(current: record.SessionShutdown) -> None:
                    current["progress"] = {"kind": "ready", "at": NOW}
                _ = record.update_session(LOGIN, "fast", ready)
                elapsed[0] = 16.0
            else:
                self.assertTrue(fast_done.wait(1))
                release.set()
            return wait_futures(futures, timeout=0.01, return_when=return_when)
        with (
            patch.object(settle, "run_inventory", return_value=machine_inventory([slow["session"], fast["session"]])) as observe,
            patch.object(stop_work, "stop_session", side_effect=ignoring_idle_reason(stop)),
            patch.object(settle, "_send_settle_message"),
            patch.object(settle, "report_stop"),
            patch.object(time, "monotonic", side_effect=lambda: elapsed[0]),
            patch.object(settle, "wait", side_effect=tick),
        ):
            try:
                self.assertEqual(settle.conduct(LOGIN), 0)
            finally:
                release.set()
        self.assertEqual(observe.call_count, 2)

    def test_desktop_restores_when_conducting_raises(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"))
        self.save(saved)
        with patch.object(settle, "run_inventory", side_effect=RuntimeError("inventory failed")), patch.object(restart_work.DesktopLaunchState, "restore") as restore, self.assertRaisesRegex(RuntimeError, "inventory failed"):
            _ = settle.conduct(LOGIN)
        restore.assert_called_once_with(dry_run=False)

    def test_registry_wait_does_not_prevent_another_owner_becoming_ready(self) -> None:
        slow = session_shutdown(top_level("slow", "Slow"), progress={"kind": "stopped", "at": NOW}, then="restart")
        fast = session_shutdown(top_level("fast", "Fast"), then="restart")
        await_ready_mark(fast)
        self.save(slow)
        self.save(fast)
        registry_entered, release, fast_confirmed = threading.Event(), threading.Event(), threading.Event()
        elapsed = [0.0]
        advanced = [False]
        def stop_ready(saved: record.SessionShutdown) -> record.SessionShutdown:
            self.assertEqual(saved["progress"]["kind"], "ready")
            self.assertTrue(registry_entered.is_set())
            return self.fake_stop(saved)
        def registry(ids: frozenset[str], _timeout: float) -> restart_work.RegistryConfirmation:
            if ids == frozenset({"slow"}):
                registry_entered.set()
                self.assertTrue(release.wait(1))
            else:
                self.assertEqual(ids, frozenset({"fast"}))
            return registry_confirmation(ids)
        def confirm(saved: record.SessionShutdown, at: str, live: bool, *, observed_at: float) -> None:
            if saved["session"]["session_id"] == "fast":
                self.assertFalse(release.is_set())
                fast_confirmed.set()
                release.set()
            self.fake_confirm(saved, at, live, observed_at=observed_at)
        def tick(futures: list[Future[object]], *, timeout: float, return_when: str) -> tuple[set[Future[object]], set[Future[object]]]:
            del timeout
            if registry_entered.is_set() and not advanced[0]:
                def ready(current: record.SessionShutdown) -> None:
                    current["progress"] = {"kind": "ready", "at": NOW}
                _ = record.update_session(LOGIN, "fast", ready)
                elapsed[0], advanced[0] = 16.0, True
            return wait_futures(futures, timeout=0.01, return_when=return_when)
        with (
            patch.object(settle, "run_inventory", return_value=machine_inventory([slow["session"], fast["session"]])) as observe,
            patch.object(stop_work, "stop_session", side_effect=ignoring_idle_reason(stop_ready)),
            patch.object(restart_work, "relaunch_session", side_effect=self.fake_launch),
            patch.object(restart_work, "wait_for_confirmation", side_effect=registry),
            patch.object(restart_work, "confirm_entry", side_effect=confirm),
            patch.object(settle, "_send_settle_message"),
            patch.object(settle, "report_stop"),
            patch.object(time, "monotonic", side_effect=lambda: elapsed[0]),
            patch.object(settle, "wait", side_effect=tick),
        ):
            try:
                self.assertEqual(settle.conduct(LOGIN), 0)
            finally:
                release.set()
        self.assertTrue(fast_confirmed.is_set())
        self.assertEqual(observe.call_count, 2)

    def test_conductor_window_launches_share_desktop_state_on_the_calling_thread(self) -> None:
        first, second, third = top_level("a", "a"), top_level("b", "b"), top_level("c", "c")
        first["host"] = inventory.WindowHost(kind="ghostty", window_shell=1, desktop=inventory.NamedDesktop(kind="named", name="Work"))
        second["host"] = inventory.WindowHost(kind="zed", window_shell=2, desktop=inventory.NamedDesktop(kind="named", name="Work"))
        third["host"] = inventory.TerminalHost(kind="terminal")
        owners: list[inventory.Session] = [third, second, first]
        for owner in owners:
            self.save(session_shutdown(owner, progress={"kind": "stopped", "at": NOW}, then="restart"))
        caller = threading.get_ident()
        launches: list[str] = []
        states: list[restart_work.DesktopLaunchState] = []
        def launch(saved: record.SessionShutdown, *, dry_run: bool, desktop_state: restart_work.DesktopLaunchState) -> record.SessionShutdown:
            self.assertEqual(threading.get_ident(), caller)
            launches.append(saved["session"]["session_id"])
            states.append(desktop_state)
            return self.fake_launch(saved, dry_run=dry_run, desktop_state=desktop_state)
        session_kind = herdr.NonDesktopSession()
        with (
            patch.object(herdr, "security_session", return_value=session_kind) as session_query,
            patch.object(settle, "run_inventory", return_value=machine_inventory(owners)),
            patch.object(restart_work, "relaunch_session", side_effect=launch),
            patch.object(restart_work, "wait_for_confirmation", side_effect=self.all_live),
            patch.object(restart_work, "confirm_entry", side_effect=self.fake_confirm),
            patch.object(settle, "report_stop"),
        ):
            self.assertEqual(settle.conduct(LOGIN), 0)
        self.assertEqual(launches, ["a", "b", "c"])
        self.assertTrue(all(state is states[0] for state in states))
        self.assertTrue(all(state.session_kind is session_kind for state in states))
        session_query.assert_called_once_with()

    def test_conductor_serializes_attached_herdr_windows_and_pools_detached_hosts(self) -> None:
        owners: list[inventory.Session] = []
        for presentation in ("attached window", "detached"):
            owner = top_level(presentation, presentation)
            owner["host"] = inventory.HerdrHost(kind="herdr", herdr_session=presentation.replace(" ", "-"),
                pane_id="w1:p1", presentation=presentation)
            owners.append(owner)
            self.save(session_shutdown(owner, progress={"kind": "stopped", "at": NOW}, then="restart"))
        caller = threading.get_ident()
        launch_threads: dict[str, int] = {}

        def launch(saved: record.SessionShutdown, *, dry_run: bool,
                   desktop_state: restart_work.DesktopLaunchState) -> record.SessionShutdown:
            launch_threads[saved["session"]["session_id"]] = threading.get_ident()
            return self.fake_launch(saved, dry_run=dry_run, desktop_state=desktop_state)

        with (
            patch.object(settle, "run_inventory", return_value=machine_inventory(owners)),
            patch.object(restart_work, "relaunch_session", side_effect=launch),
            patch.object(restart_work, "wait_for_confirmation", side_effect=self.all_live),
            patch.object(restart_work, "confirm_entry", side_effect=self.fake_confirm),
            patch.object(settle, "report_stop"),
        ):
            self.assertEqual(settle.conduct(LOGIN), 0)
        self.assertEqual(launch_threads["attached window"], caller)
        self.assertNotEqual(launch_threads["detached"], caller)
        self.assertEqual(record.session_records(LOGIN), [])

    def test_cancel_restores_enabled_timers_and_retires_queued_messages(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"))
        saved["settle_message"] = {"kind": "queued", "at": NOW, "reason": "busy"}
        saved["timers"] = [record.TimerRestore(instance="enabled", was_enabled=True),
                           record.TimerRestore(instance="off", was_enabled=False)]
        self.save(saved)
        with patch.object(settle, "run_notifier") as timers, patch.object(settle, "_retire_shutdown_messages") as retire, patch.object(settle, "send_message", return_value={"kind": "sent"}) as send:
            self.assertEqual(settle.cancel(Account("claude", LOGIN, LABEL), True), 0)
        timers.assert_called_once_with("start", "enabled")
        retire.assert_called_once_with(LOGIN, "owner")
        self.assertIn("cancelled by the user: continue where you were", cast(str, send.call_args.args[2]))
        self.assertIsNone(record.find_session(LOGIN, "owner"))

    def test_ready_preserves_the_recorded_process_identity_and_where_contract(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"))
        self.save(saved)
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "owner"}), patch.object(settle, "run_inventory", return_value=machine_inventory([saved["session"]])), patch.object(settle, "_launch_conductor"):
            self.assertEqual(settle.ready("next test"), 0)
        changed = record.find_session(LOGIN, "owner")
        self.assertIsNotNone(changed)
        if changed is not None:
            self.assertEqual(changed["session"], saved["session"])
            self.assertEqual(changed["where"], {"kind": "said", "at": NOW, "text": "next test"})
            self.assertEqual(changed["progress"], {"kind": "ready", "at": NOW})

    def test_stop_only_account_with_no_owner_releases_its_hold_when_its_conductor_ends(self) -> None:
        hold = record.AccountHold(login=LOGIN, label=LABEL, machine="natedev", requested_at=NOW,
                                  requested_by={"kind": "terminal"}, request_id="empty", then="stop")
        record.set_account_hold(hold)
        with patch.object(settle, "report_stop"):
            self.assertEqual(settle.conduct(LOGIN), 0)
        self.assertIsNone(record.account_hold(LOGIN))

    def test_conductor_releases_empty_accounts_with_restart_continuations(self) -> None:
        for login, label in ((LOGIN, LABEL), ("third@example.com", "claude3")):
            report = inventory.Inventory(machine="natedev", login=login, label=label,
                                         sessions=[], unattributed=[])
            self.assertEqual(settle.begin(login, then="restart", request_id="empty", report=report), [])
            self.assertIsNotNone(record.account_hold(login))
            with patch.object(settle, "report_stop"):
                self.assertEqual(settle.conduct(login), 0)
            self.assertIsNone(record.account_hold(login))

    def test_conductor_keeps_hold_while_begin_has_not_inserted_its_records(self) -> None:
        owners: list[inventory.Session] = [top_level(name, name, status="busy") for name in ("first", "second")]
        create_session = record.create_session
        observed_holds: list[record.AccountHeld | record.AccountOpen] = []

        def conduct_before_record(saved: record.SessionShutdown) -> bool:
            if not observed_holds:
                self.assertEqual(record.session_records(LOGIN), [])
                with patch.object(settle, "report_stop"):
                    self.assertEqual(settle.conduct(LOGIN), 0)
                observed_holds.append(record.lookup_account_hold(LOGIN))
            return create_session(saved)

        with patch.object(record, "create_session", side_effect=conduct_before_record):
            added = settle.begin(LOGIN, then="restart", request_id="being-added",
                                 report=machine_inventory(owners))
        self.assertEqual(len(observed_holds), 1)
        self.assertIsInstance(observed_holds[0], record.AccountHeld)
        self.assertEqual([saved["progress"]["kind"] for saved in added], ["waiting", "waiting"])
        self.assertIsInstance(record.lookup_account_hold(LOGIN), record.AccountHeld)

    def test_failed_begin_keeps_its_hold_until_registration_can_finish(self) -> None:
        with patch.object(settle, "prompt_state", side_effect=OSError("session unreadable")):
            with self.assertRaisesRegex(OSError, "session unreadable"):
                _ = settle.begin(LOGIN, then="restart", request_id="interrupted",
                                 report=machine_inventory([top_level("owner", "Owner")]))
        with patch.object(settle, "report_stop"):
            self.assertEqual(settle.conduct(LOGIN), 0)
        record.clear_finished_account_hold(LOGIN)
        self.assertIsInstance(record.lookup_account_hold(LOGIN), record.AccountHeld)

    def test_second_request_keeps_completed_hold_and_blocks_launch_during_registration(self) -> None:
        for selected in (False, True):
            with self.subTest(selected=selected):
                identity = f"owner-{selected}"
                self.assertEqual(settle.begin(LOGIN, then="restart", request_id=f"first-{selected}",
                                             report=machine_inventory([])), [])
                create_session = record.create_session

                def conduct_before_record(saved: record.SessionShutdown) -> bool:
                    self.assertEqual(record.session_records(LOGIN), [])
                    with patch.object(settle, "report_stop"):
                        self.assertEqual(settle.conduct(LOGIN), 0)
                    self.assertIsInstance(record.lookup_account_hold(LOGIN), record.AccountHeld)
                    self.assertEqual(shutdown.launch_blocked(Account("claude", LOGIN, LABEL),
                                                            NewWorkLaunchPurpose()), 0)
                    return create_session(saved)

                with patch.object(record, "create_session", side_effect=conduct_before_record):
                    added = settle.begin(LOGIN, then="restart", request_id=f"second-{selected}",
                        only=[identity] if selected else (),
                        report=machine_inventory([top_level(identity, "Owner", status="busy")]))
                self.assertEqual([saved["progress"]["kind"] for saved in added], ["waiting"])
                record.clear_finished_account_hold(LOGIN)
                self.assertIsInstance(record.lookup_account_hold(LOGIN), record.AccountHeld)
                self.assertEqual(shutdown.launch_blocked(Account("claude", LOGIN, LABEL),
                                                        NewWorkLaunchPurpose()), 0)
                record.archive_session(LOGIN, identity)
                record.clear_finished_account_hold(LOGIN)
                self.assertIsInstance(record.lookup_account_hold(LOGIN), record.AccountOpen)

    def test_failed_second_begin_keeps_the_existing_hold_closed_to_new_work(self) -> None:
        self.assertEqual(settle.begin(LOGIN, then="restart", request_id="complete",
                                     report=machine_inventory([])), [])
        with patch.object(settle, "prompt_state", side_effect=OSError("session unreadable")):
            with self.assertRaisesRegex(OSError, "session unreadable"):
                _ = settle.begin(LOGIN, then="restart", request_id="interrupted",
                                 report=machine_inventory([top_level("owner", "Owner")]))
        with patch.object(settle, "report_stop"):
            self.assertEqual(settle.conduct(LOGIN), 0)
        self.assertEqual(shutdown.launch_blocked(Account("claude", LOGIN, LABEL),
                                                NewWorkLaunchPurpose()), 0)

    def test_one_request_finishing_does_not_clear_a_different_requests_hold(self) -> None:
        done = session_shutdown(top_level("done", "Done"), progress={"kind": "restarted", "at": NOW}, request_id="one")
        stopped = session_shutdown(top_level("stopped", "Stopped"), progress={"kind": "stopped", "at": NOW}, request_id="two")
        for saved in (done, stopped):
            self.save(saved)
        hold = record.AccountHold(login=LOGIN, label=LABEL, machine="natedev", requested_at=NOW,
                                  requested_by={"kind": "terminal"}, request_id="two", then="stop",
                                  registration="in progress")
        record.set_account_hold(hold)
        with patch.object(settle, "report_stop"):
            self.assertEqual(settle.conduct(LOGIN), 0)
        self.assertEqual(record.account_hold(LOGIN), hold)

    def test_conductor_confirms_other_owners_before_stopping_the_requester(self) -> None:
        owners: list[inventory.Session] = [top_level("other", "Other"), top_level("requester", "Requester")]
        for owner in owners:
            self.save(session_shutdown(owner, progress={"kind": "ready", "at": NOW}, requester="requester", then="restart"))
        def stop(saved: record.SessionShutdown) -> record.SessionShutdown:
            if saved["session"]["session_id"] == "requester":
                self.assertIsNone(record.find_session(LOGIN, "other"))
            return self.fake_stop(saved)
        with (
            patch.object(settle, "run_inventory", return_value=machine_inventory(owners)),
            patch.object(stop_work, "stop_session", side_effect=ignoring_idle_reason(stop)),
            patch.object(restart_work, "relaunch_session", side_effect=self.fake_launch),
            patch.object(restart_work, "wait_for_confirmation", side_effect=self.all_live),
            patch.object(restart_work, "confirm_entry", side_effect=self.fake_confirm),
            patch.object(time, "sleep"),
            patch.object(settle, "report_stop"),
        ):
            self.assertEqual(settle.conduct(LOGIN), 0)
        self.assertEqual(record.session_records(LOGIN), [])

    def test_fake_sender_reads_message_input_but_retire_and_pending_leave_stdin_open(self) -> None:
        sender = self.root / "sender.py"
        _ = sender.write_text('import sys\nverb = sys.argv[1]\nif verb not in {"retire", "pending"}:\n    print(sys.stdin.read())\n')
        for verb in ("retire", "pending"):
            with subprocess.Popen([sys.executable, str(sender), verb], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
                self.assertEqual(process.wait(timeout=0.8), 0)
        result = subprocess.run([sys.executable, str(sender), "send"], input="body", capture_output=True, text=True, check=False, timeout=0.8)
        self.assertEqual(result.stdout, "body\n")

    def test_conductor_report_includes_current_local_and_peer_requests_but_keeps_older_stopped_records(self) -> None:
        old = session_shutdown(top_level("old-local", "Older local"),
            progress={"kind": "stopped", "at": NOW}, request_id="earlier-request")
        current = session_shutdown(top_level("current-local", "Current local"),
            progress={"kind": "ready", "at": NOW}, request_id="current-request")
        another = session_shutdown(top_level("another-local", "Another local"),
            progress={"kind": "ready", "at": NOW}, request_id="another-request")
        peer = session_shutdown(top_level("current-peer", "Current peer"), request_id="current-request")
        older_peer = session_shutdown(top_level("old-peer", "Older peer"),
            progress={"kind": "stopped", "at": NOW}, request_id="earlier-request")
        peer["machine"] = older_peer["machine"] = "Mac"
        for saved in (old, current, another):
            saved["alerts"] = "none"
        for saved in (old, current):
            self.save(saved)
        def stop_and_accept_late_request(saved: record.SessionShutdown) -> record.SessionShutdown:
            if saved["session"]["session_id"] == "current-local":
                self.save(another)
            return self.fake_stop(saved)
        with (patch.object(settle, "run_inventory", return_value=machine_inventory([current["session"], another["session"]])),
              patch.object(stop_work, "stop_session", side_effect=ignoring_idle_reason(stop_and_accept_late_request)),
              patch.object(settle, "SETTLE_INTERVAL_SECONDS", 0.001),
              patch.object(settle, "run_remote", return_value=(0, json.dumps([peer, older_peer]))),
              patch.object(settle, "send_message", return_value={"kind": "sent"}),
              patch.object(settle, "alert_user", return_value=True) as alert,
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(settle.conduct(LOGIN), 0)
        self.assertIn("natedev Current local: stopped", output.getvalue())
        self.assertIn("natedev Another local: stopped", output.getvalue())
        self.assertIn("Mac Current peer: waiting", output.getvalue())
        self.assertNotIn("Older local", output.getvalue())
        self.assertNotIn("Older peer", output.getvalue())
        self.assertEqual(record.find_session(LOGIN, "old-local"), old)
        self.assertIsNotNone(record.find_session(LOGIN, "current-local"))
        self.assertIsNotNone(record.find_session(LOGIN, "another-local"))
        alert.assert_not_called()

    def test_report_stop_lists_peer_waiting_and_alerts_only_requested_failures(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"), progress={"kind": "stop failed", "at": NOW, "reason": "busy"}, requester="requester")
        saved["hold"] = "account"
        peer = session_shutdown(top_level("peer", "Peer"))
        peer["machine"] = "Mac"
        with patch.object(settle, "run_remote", return_value=(0, json.dumps([peer]))), patch.object(settle, "send_message", return_value={"kind": "sent"}) as send, patch.object(settle, "alert_user", return_value=True) as alert:
            settle.report_stop([saved])
        self.assertIn("Mac Peer: waiting", cast(str, send.call_args.args[2]))
        self.assertEqual(send.call_args.kwargs["key"], f"shutdown-result-{LOGIN}")
        alert.assert_called_once()
        saved["alerts"] = "none"
        with patch.object(settle, "run_remote", return_value=(255, "")), patch.object(settle, "send_message", return_value={"kind": "sent"}), patch.object(settle, "alert_user", return_value=True) as alert:
            settle.report_stop([saved])
        alert.assert_not_called()

    def test_conductor_rechecks_ready_work_added_after_its_final_snapshot(self) -> None:
        saved = session_shutdown(top_level("late", "Late"), progress={"kind": "ready", "at": NOW})
        read = record.session_records
        added = False
        def snapshot(login: str) -> list[record.SessionShutdown]:
            nonlocal added
            records = read(login)
            if not added:
                added = True
                self.save(saved)
                self.assertEqual(settle.conduct(login), 0)
            return records
        with patch.object(record, "session_records", side_effect=snapshot), patch.object(settle, "run_inventory", return_value=machine_inventory([saved["session"]])), patch.object(stop_work, "stop_session", side_effect=ignoring_idle_reason(self.fake_stop)) as stop, patch.object(settle, "report_stop"):
            self.assertEqual(settle.conduct(LOGIN), 0)
        stop.assert_called_once()
        current = record.find_session(LOGIN, "late")
        self.assertIsNotNone(current)
        if current is not None:
            self.assertEqual(current["progress"]["kind"], "stopped")

    def test_conductor_handoff_keeps_work_owned_across_release_interleavings(self) -> None:
        for moment in ("before release", "after release", "after recheck"):
            with self.subTest(moment=moment):
                saved = session_shutdown(top_level(moment.replace(" ", "-"), moment), progress={"kind": "ready", "at": NOW})
                saved["request_id"] = moment
                real_lock, read = record.conductor_lock, record.session_records
                inserted, released, deferred = False, False, False
                def add() -> None:
                    nonlocal inserted
                    inserted = True
                    self.save(saved)
                @contextmanager
                def handoff(login: str) -> Generator[record.ConductorAcquired | record.ConductorAlreadyRunning, None, None]:
                    nonlocal released
                    with real_lock(login) as ownership:
                        yield ownership
                        if isinstance(ownership, record.ConductorAcquired) and not inserted and moment == "before release":
                            add()
                            self.assertEqual(settle.conduct(login), 0)
                    if isinstance(ownership, record.ConductorAcquired):
                        released = True
                        if not inserted and moment == "after release":
                            add()
                            self.assertEqual(settle.conduct(login), 0)
                def snapshot(login: str) -> list[record.SessionShutdown]:
                    nonlocal deferred
                    records = read(login)
                    if released and not inserted and moment == "after recheck":
                        add()
                        deferred = True
                    return records
                with patch.object(record, "conductor_lock", side_effect=handoff), patch.object(record, "session_records", side_effect=snapshot), patch.object(settle, "run_inventory", return_value=machine_inventory([saved["session"]])), patch.object(stop_work, "stop_session", side_effect=ignoring_idle_reason(self.fake_stop)) as stop, patch.object(settle, "report_stop"):
                    self.assertEqual(settle.conduct(LOGIN), 0)
                    if deferred:
                        self.assertEqual(settle.conduct(LOGIN), 0)
                stop.assert_called_once()
                current = record.find_session(LOGIN, saved["session"]["session_id"])
                self.assertIsNotNone(current)
                if current is not None:
                    self.assertEqual(current["progress"]["kind"], "stopped")
                record.archive_session(LOGIN, saved["session"]["session_id"])

    def test_begin_stops_timers_before_a_waiting_cancel_can_restore_them(self) -> None:
        owner = top_level("owner", "Owner", timers=["timer"])
        saved_snapshot = threading.Event()
        cancelled = threading.Event()
        events: list[str] = []
        failures: list[Exception] = []
        update = record.update_session
        def save_snapshot(login: str, session_id: str, change: object) -> record.SessionShutdown:
            result = update(login, session_id, cast('Callable[[record.SessionShutdown], None]', change))
            saved_snapshot.set()
            self.assertTrue(cancelled.wait(2))
            return result
        def cancel_waiter() -> None:
            try:
                self.assertTrue(saved_snapshot.wait(2))
                _ = settle.cancel(Account("claude", LOGIN, LABEL), True)
            except Exception as error:
                failures.append(error)
            finally:
                cancelled.set()
        def notifier(verb: str, _instance: str) -> None:
            events.append(verb)
        with patch.object(settle, "run_inventory", return_value=machine_inventory([owner])), patch.object(settle, "_notifier_enabled", return_value=True), patch.object(record, "update_session", side_effect=save_snapshot), patch.object(settle, "run_notifier", side_effect=notifier), patch.object(settle, "_retire_shutdown_messages"):
            worker = threading.Thread(target=cancel_waiter)
            worker.start()
            try:
                _ = settle.begin(LOGIN, only=["owner"], request_id="timer-request")
            finally:
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(events, ["stop", "start"])
        self.assertIsNone(record.find_session(LOGIN, "owner"))

    def test_cancel_clears_account_hold_while_retaining_stopped_and_stopping_sessions(self) -> None:
        for kind in ("stopped", "stopping"):
            self.save(session_shutdown(top_level(kind, kind), progress=cast(record.SessionProgress, cast(object, {"kind": kind, "at": NOW}))))
        record.set_account_hold(record.AccountHold(login=LOGIN, label=LABEL, machine="natedev", requested_at=NOW,
            requested_by={"kind": "terminal"}, request_id="request-one", then="stop"))
        self.assertEqual(settle.cancel(Account("claude", LOGIN, LABEL), True), 1)
        self.assertIsNone(record.account_hold(LOGIN))
        self.assertEqual(len(record.session_records(LOGIN)), 2)

    def test_cancel_names_each_retained_records_actual_progress(self) -> None:
        progress: list[record.SessionProgress] = [
            {"kind": "stopped", "at": NOW}, {"kind": "stopping", "at": NOW},
            {"kind": "restarting", "at": NOW, "prior": {"kind": "stopped", "at": NOW}},
            {"kind": "restarted", "at": NOW}, {"kind": "restart failed", "at": NOW, "reason": "not live"},
            {"kind": "manual restart", "command": "resume owner"},
            {"kind": "timers pending", "at": NOW, "timers": [{"instance": "timer", "reason": "not started"}]},
            {"kind": "stop failed", "at": NOW, "reason": "busy"},
        ]
        for index, item in enumerate(progress):
            self.save(session_shutdown(top_level(str(index), str(index)), progress=item))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(settle.cancel(Account("claude", LOGIN, LABEL), True), 1)
        for index, item in enumerate(progress):
            line = output.getvalue().splitlines()[index]
            self.assertTrue(line.startswith(f"{index} was not cancelled: {item['kind']}"))
            self.assertIn("in a terminal on natedev:", line)
            self.assertIn(f"restart {LOGIN} --here --only", line)
        self.assertIn("restart failed — not live", output.getvalue())
        self.assertIn("manual restart — in a terminal on natedev: resume owner", output.getvalue())

    def test_request_added_just_after_cancel_keeps_its_settle_instruction(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"))
        saved["settle_message"] = {"kind": "sent", "at": NOW}
        self.save(saved)
        key = f"shutdown-{LOGIN}-owner"
        queue: dict[str, str] = {key: "old instruction"}
        delivered: list[str] = []
        withdraw = record.withdraw_session
        def send(_recipient: str, _summary: str, text: str, *, key: str) -> settle.MessageDelivery:
            queue[key] = text
            delivered.append(text)
            return {"kind": "sent"}
        def retire(_login: str, _session_id: str) -> None:
            queue.clear()
        def withdraw_then_request(login: str, session_id: str, restore: Callable[[record.SessionShutdown], None]) -> record.SessionWithdrawn | record.SessionRetained:
            outcome = withdraw(login, session_id, restore)
            added = settle.begin(LOGIN, only=["owner"], request_id="new-request")
            self.assertEqual(len(added), 1)
            settle._send_settle_message(added[0])  # pyright: ignore[reportPrivateUsage]
            return outcome
        with patch.object(record, "withdraw_session", side_effect=withdraw_then_request), patch.object(settle, "run_inventory", return_value=machine_inventory([saved["session"]])), patch.object(settle, "_retire_shutdown_messages", side_effect=retire), patch.object(settle, "send_message", side_effect=send):
            self.assertEqual(settle.cancel(Account("claude", LOGIN, LABEL), True), 0)
        current = record.find_session(LOGIN, "owner")
        assert current is not None
        self.assertEqual(current["request_id"], "new-request")
        self.assertEqual(current["progress"]["kind"], "waiting")
        self.assertIn("cancelled by the user", delivered[0])
        self.assertEqual(queue[key], delivered[-1])
        self.assertIn("ready", queue[key])
        self.assertNotIn("cancelled by the user", queue[key])

    def test_status_and_hold_reason_keep_delivery_text_separate_from_observed_details(self) -> None:
        for status in ("idle", "busy"):
            with self.subTest(status=status):
                saved = session_shutdown(top_level("owner", "Owner", status=status))
                saved["settle_message"] = {"kind": "queued", "at": NOW, "reason": "delivery pending, retry later"}
                owner = self.matching(saved)
                queued = "message queued: delivery pending, retry later"
                expected_reason = queued if status == "idle" else f"busy, {queued}"
                self.assertEqual(settle.hold_reason(saved, owner), expected_reason)
                detail = "" if status == "idle" else " — busy"
                self.assertEqual(settle.status_record_lines(saved, owner), [f"  Owner: waiting{detail} · {queued}"])

    def test_conductor_drops_records_cancelled_before_its_result(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"), requester="requester")
        self.save(saved)
        def cancelled(_login: str, _only: object = (), *, refresh_desktops: bool = True) -> inventory.Inventory:
            del refresh_desktops
            self.assertEqual(settle.cancel(Account("claude", LOGIN, LABEL), True), 0)
            return machine_inventory([])
        with patch.object(settle, "run_inventory", side_effect=cancelled), patch.object(settle, "_retire_shutdown_messages"), patch.object(stop_work, "stop_session", side_effect=record.NoLiveRecord("cancelled")), patch.object(settle, "report_stop") as report:
            self.assertEqual(settle.conduct(LOGIN), 0)
        self.assertEqual(report.call_args.args[0], [])

    def test_conductor_drops_records_archived_by_another_restore_caller(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"), progress={"kind": "stopped", "at": NOW}, then="restart")
        self.save(saved)
        def resumed(_login: str, _only: object = (), *, refresh_desktops: bool = True) -> inventory.Inventory:
            del refresh_desktops
            def complete(current: record.SessionShutdown) -> None:
                current["progress"] = {"kind": "restarted", "at": NOW}
            _ = record.update_session(LOGIN, "owner", complete)
            record.archive_session(LOGIN, "owner")
            return machine_inventory([])
        with patch.object(settle, "run_inventory", side_effect=resumed), patch.object(restart_work, "relaunch_session", side_effect=record.NoLiveRecord("finished elsewhere")), patch.object(settle, "report_stop") as report:
            self.assertEqual(settle.conduct(LOGIN), 0)
        self.assertEqual(report.call_args.args[0], [])

    def test_session_count_text_names_one_and_many_sessions(self) -> None:
        self.assertEqual(settle.stopped_sessions_text(1), "One session")
        self.assertEqual(settle.stopped_sessions_text(14), "14 sessions")

    def test_shutdown_failure_pushes_keep_causes_in_session_reports(self) -> None:
        cases: list[record.SessionProgress] = [
            {"kind": "stop failed", "at": NOW, "reason": "process did not exit"},
            {"kind": "restart failed", "at": NOW, "reason": "window failed to open"},
        ]
        for progress in cases:
            with self.subTest(kind=progress["kind"]):
                saved = session_shutdown(top_level("owner", "Owner"), progress=progress, requester="requester")
                saved["hold"] = "account"
                with (patch.object(settle, "run_remote", return_value=(0, "[]")),
                      patch.object(settle, "send_message") as sender, patch.object(settle, "alert_user", return_value=True) as alert):
                    settle.report_stop([saved])
                push = cast(RenderedPush, alert.call_args.args[0])
                self.assertEqual(push.id, "shutdown.stop_failed" if progress["kind"] == "stop failed" else "shutdown.restart_failed")
                assert progress["kind"] == "stop failed" or progress["kind"] == "restart failed"
                if progress["kind"] == "stop failed":
                    self.assertEqual(push.values, {"account": '"claude2"', "machine": "natedev", "stopped": "One session"})
                    self.assertNotIn(progress["reason"], push.body)
                else:
                    self.assertEqual(push.values, {"account": '"claude2"', "machine": "natedev",
                        "stopped": "One session (Owner)", "cause": progress["reason"]})
                    self.assertIn(f"The restart gave this error: {progress['reason']}", push.body)
                self.assertIn(progress["reason"], cast(str, sender.call_args.args[2]))
                self.assertIsInstance(push.action, ActionRequired)
                self.assertNotIn("status", cast(ActionRequired, push.action).text)

    def test_failed_move_pushes_its_target_account_and_no_restart_onto_its_source(self) -> None:
        saved = session_shutdown(top_level("ups-id", "ups"), then="restart",
            progress={"kind": "restart failed", "at": NOW, "reason": "herdr agents:w1:p1: pane still holds a live agent"})
        saved["target"], saved["source_label"] = "claude3", saved["label"]
        self.save(saved)
        with (patch.object(sessions, "live_sessions", return_value=[]),
              patch.object(restart_work, "_target_config_dir",
                           return_value=restart_work.RecordedConfigDir(self.root / ".claude3")),
              patch.object(settle, "run_remote", return_value=(0, "[]")),
              patch.object(settle, "send_message"),
              patch.object(settle, "alert_user", return_value=True) as alert,
              redirect_stdout(io.StringIO())):
            settle.report_stop([saved])
        alert.assert_called_once()
        push = cast(RenderedPush, alert.call_args.args[0])
        self.assertEqual(push.id, "shutdown.move_failed")
        self.assertEqual((push.values["session"], push.values["target"]), ("ups", "claude3"))
        self.assertNotIn("restart", cast(ActionRequired, push.action).text)

    def test_failed_move_into_an_untrusted_folder_pushes_the_move_trust_step_not_a_move_retry(self) -> None:
        command = "cd /home/natepiano/worktrees/organon && CLAUDE_CONFIG_DIR=/home/natepiano/.claude3 claude"
        saved = session_shutdown(top_level("ups-id", "ups"), then="restart",
            progress={"kind": "restart failed", "at": NOW, "trust_command": command,
                      "reason": f"/home/natepiano/worktrees/organon: Claude has not trusted this folder. Run: {command}"})
        saved["target"], saved["source_label"] = "claude3", saved["label"]
        self.save(saved)
        with (patch.object(sessions, "live_sessions", return_value=[]),
              patch.object(restart_work, "_target_config_dir",
                           return_value=restart_work.RecordedConfigDir(self.root / ".claude3")),
              patch.object(settle, "run_remote", return_value=(0, "[]")),
              patch.object(settle, "send_message"),
              patch.object(settle, "alert_user", return_value=True) as alert,
              redirect_stdout(io.StringIO())):
            settle.report_stop([saved])
        alert.assert_called_once()
        push = cast(RenderedPush, alert.call_args.args[0])
        self.assertEqual(push.id, "shutdown.move_untrusted")
        self.assertEqual(push.values["target"], "claude3")
        self.assertIn("/shutdown restart \"claude2\" in the natedev session. That finishes the move to claude3.",
                      cast(ActionRequired, push.action).text)
        # The record lives under the source account; restarting it resumes the move on its target.
        self.assertEqual((push.values["session"], push.values["account"], push.values["command"]),
                         ("ups", '"claude2"', command))
        current = record.find_session(LOGIN, "ups-id")
        assert current is not None
        self.assertEqual(current.get("restore_alerted"), settle.not_back_text(current))

    def test_automatic_restart_with_an_unsent_prompt_pushes_the_failed_session_name(self) -> None:
        saved = session_shutdown(top_level("unsent-owner", "Unsent owner"), then="restart",
            progress={"kind": "restart failed", "at": NOW, "reason": "restart prompt not submitted"})
        self.save(saved)
        with (patch.object(sessions, "live_sessions", return_value=[]),
              patch.object(settle, "run_remote", return_value=(0, "[]")),
              patch.object(settle, "send_message") as sender,
              patch.object(settle, "alert_user", return_value=True) as alert,
              redirect_stdout(io.StringIO()) as output):
            settle.report_stop([saved])
        sender.assert_not_called()
        alert.assert_called_once()
        push = cast(RenderedPush, alert.call_args.args[0])
        self.assertEqual(push.id, "shutdown.restart_failed")
        self.assertIn("Unsent owner", push.title + "\n" + push.body)
        self.assertIn("Unsent owner: restart prompt not submitted", output.getvalue())
        current = record.find_session(LOGIN, "unsent-owner")
        assert current is not None
        self.assertEqual(current["progress"], saved["progress"])

    def test_report_claims_each_move_push_just_before_sending(self) -> None:
        records = [session_shutdown(top_level(f"slow-{index}", f"Owner {index}"), then="restart",
                   progress={"kind": "restart failed", "at": NOW, "reason": "both failed"}) for index in range(6)]
        for saved in records:
            saved["target"] = "claude3"
            self.save(saved)
        clock = [1000.0]
        sent: list[str] = []
        def send(push: RenderedPush, **_kwargs: object) -> bool:
            for saved in records[len(sent) + 1:]:
                claim = outage_alerts.claim(saved["session"]["session_id"])
                self.assertIsInstance(claim, outage_alerts.AlertClaim)
                assert isinstance(claim, outage_alerts.AlertClaim)
                outage_alerts.release(claim)
            sent.append(push.values["session"])
            clock[0] += 190
            return True
        with (patch.object(sessions, "live_sessions", return_value=[]),
              patch.object(accounts, "claude_account_dir", return_value=self.root / ".claude3"),
              patch.object(settle, "run_remote", return_value=(0, "[]")),
              patch.object(settle, "send_message"),
              patch.object(time, "time", side_effect=lambda: clock[0]),
              patch.object(settle, "alert_user", side_effect=send), redirect_stdout(io.StringIO())):
            settle.report_stop(records)
        self.assertEqual(len(sent), 6)

    def test_untrusted_folder_restart_pushes_the_trust_step_and_never_a_retry(self) -> None:
        command = "cd /home/natepiano/worktrees/organon && CLAUDE_CONFIG_DIR=/home/natepiano/.claude2 claude"
        untrusted = session_shutdown(top_level("untrusted-owner", "Untrusted owner"), then="restart",
            progress={"kind": "restart failed", "at": NOW, "trust_command": command,
                      "reason": f"/home/natepiano/worktrees/organon: Claude has not trusted this folder. Run: {command}"})
        broken = session_shutdown(top_level("broken-owner", "Broken owner"), then="restart",
            progress={"kind": "restart failed", "at": NOW, "reason": "window failed to open"})
        records = [untrusted, broken]
        for saved in records:
            self.save(saved)
        for attempt in ("first", "unchanged"):
            with (self.subTest(attempt=attempt),
                  patch.object(sessions, "live_sessions", return_value=[]),
                  patch.object(settle, "run_remote", return_value=(0, "[]")),
                  patch.object(settle, "send_message"),
                  patch.object(settle, "alert_user", return_value=True) as alert,
                  redirect_stdout(io.StringIO())):
                settle.report_stop([current for saved in records
                                    if (current := record.find_session(LOGIN, saved["session"]["session_id"]))])
            pushes = {cast(RenderedPush, call.args[0]).id: cast(RenderedPush, call.args[0]) for call in alert.call_args_list}
            if attempt == "unchanged":
                self.assertEqual(pushes, {})
                continue
            self.assertEqual(set(pushes), {"shutdown.restart_untrusted", "shutdown.restart_failed"})
            trust = pushes["shutdown.restart_untrusted"]
            self.assertEqual(trust.values, {"session": "Untrusted owner", "machine": "natedev",
                                            "account": '"claude2"', "command": command})
            retry = pushes["shutdown.restart_failed"]
            self.assertEqual((retry.values["stopped"], retry.values["cause"]),
                             ("One session (Broken owner)", "window failed to open"))
            self.assertNotIn("Untrusted owner", retry.message)

    def test_stop_report_observes_failed_restart_liveness_once_before_commands_and_pushes(self) -> None:
        cases = (
            ("live", restart_work.SessionLive()),
            ("not live", restart_work.SessionNotLive()),
            ("unknown", restart_work.SessionLivenessUnknown("scratch registry unreadable")),
        )
        for state, observed in cases:
            with self.subTest(liveness=state):
                failed = session_shutdown(top_level(f"failed-{state}", "Slow owner"), then="restart",
                    requester="requester",
                    progress={"kind": "restart failed", "at": NOW, "reason": "restart prompt not submitted"})
                sibling = session_shutdown(top_level(f"restarted-{state}", "Restarted sibling"), then="restart",
                    requester="requester", progress={"kind": "restarted", "at": NOW})
                self.save(failed)
                self.save(sibling)
                records = [failed, sibling]
                with (patch.object(restart_work, "clear_running_on_target",
                                   return_value=restart_work.TargetCheck(records, [])),
                      patch.object(restart_work, "restart_failed_liveness", return_value=observed) as liveness,
                      patch.object(settle, "run_remote", return_value=(0, "[]")),
                      patch.object(settle, "send_message") as sender,
                      patch.object(settle, "alert_user", return_value=True) as alert,
                      patch.object(restart_work, "restart_outage_pushes", wraps=restart_work.restart_outage_pushes) as pushes,
                      patch.object(settle, "mark_not_back_pushed", wraps=settle.mark_not_back_pushed) as mark):
                    settle.report_stop(records)
                liveness.assert_called_once_with(failed)
                sender.assert_called_once()
                summary = cast(str, sender.call_args.args[1])
                text = cast(str, sender.call_args.args[2])
                current = record.find_session(LOGIN, failed["session"]["session_id"])
                assert current is not None
                if isinstance(observed, restart_work.SessionLive):
                    self.assertEqual(summary, f"{LABEL} is back")
                    self.assertIn("  Slow owner: restart prompt not confirmed; session is running — check it, " +
                                  "then close its record in a terminal on natedev: " + settle.shutdown_command("forget",
                                      failed["session"]["session_id"], "--account", LOGIN), text)
                    self.assertNotIn("  Slow owner: restart prompt not submitted", text)
                    self.assertNotIn("shutdown.py restart", text)
                    self.assertEqual(current, failed)
                    alert.assert_not_called()
                    pushes.assert_not_called()
                    mark.assert_not_called()
                else:
                    self.assertEqual(summary, f"{LABEL} is not fully back")
                    self.assertIn("  Slow owner: restart prompt not submitted", text)
                    command = settle.shutdown_command("restart", LOGIN, "--here", "--only",
                                                      failed["session"]["session_id"])
                    self.assertIn(f"in a terminal on natedev: {command}", text)
                    self.assertNotIn("session is running", text)
                    if isinstance(observed, restart_work.SessionLivenessUnknown):
                        self.assertIn(f"{command}; liveness unknown: {observed.reason}", text)
                    alert.assert_called_once()
                    pushes.assert_called_with(
                        LABEL, [restart_work.FailedSessionRestart("Slow owner", "restart prompt not submitted")],
                        machine="natedev")
                    mark.assert_called_once_with(failed)
                    self.assertEqual(current.get("restore_alerted"), settle.not_back_text(failed))

    def test_automatic_restart_sends_all_split_pushes_and_marks_every_named_failure(self) -> None:
        batches = [[f"Owner{index}-" + "x" * suffix_length for index in range(6)]
                   for suffix_length in (80, 40)]
        batches.append(["Short owner", "Oversize-" + "x" * 201, "Another owner"])
        for names in batches:
            with self.subTest(names=names):
                records = [session_shutdown(top_level(f"split-{len(names[0])}-{index}", name), then="restart",
                    progress={"kind": "restart failed", "at": NOW, "reason": "restart prompt not submitted"})
                    for index, name in enumerate(names)]
                for saved in records:
                    self.save(saved)
                with (patch.object(sessions, "live_sessions", return_value=[]),
                      patch.object(settle, "run_remote", return_value=(0, "[]")),
                      patch.object(settle, "send_message"), patch.object(settle, "alert_user", return_value=True) as alert,
                      redirect_stdout(io.StringIO())):
                    settle.report_stop(records)
                    pushes = [cast(RenderedPush, call.args[0]) for call in alert.call_args_list]
                    self.assertGreater(len(pushes), 1)
                    for push in pushes:
                        self.assertEqual(push.id, "shutdown.restart_failed")
                        self.assertLessEqual(len(push.values["stopped"]), 400)
                        self.assertLess(len(push.message), 500)
                        self.assertLess(len(push.title), 45)
                        self.assertTrue(any(name in push.message for name in names)
                                        or "…" in push.values["stopped"])
                    for name in names:
                        if len(name) == 210:
                            shortened, = [push for push in pushes if "…" in push.values["stopped"]]
                            displayed = shortened.values["stopped"].removeprefix("One session (").removesuffix(")")
                            self.assertTrue(displayed.endswith("…"))
                            self.assertGreater(len(displayed), 1)
                            self.assertTrue(name.startswith(displayed[:-1]))
                            self.assertLess(len(displayed[:-1]), len(name))
                            self.assertTrue(len(shortened.message) == 499
                                            or len(shortened.values["stopped"]) == 400)
                            self.assertFalse(any(other in shortened.message for other in names if other != name))
                        else:
                            self.assertEqual(sum(push.message.count(name) for push in pushes), 1)
                    for saved in records:
                        current = record.find_session(LOGIN, saved["session"]["session_id"])
                        assert current is not None
                        self.assertEqual(current.get("restore_alerted"), settle.not_back_text(current))
                    alert.reset_mock()
                    settle.report_stop(record.session_records(LOGIN))
                    alert.assert_not_called()

    def test_stop_failure_push_keeps_the_count_without_session_names(self) -> None:
        saved = session_shutdown(top_level("stop-owner", "Stop owner"),
            progress={"kind": "stop failed", "at": NOW, "reason": "process did not exit"})
        saved["hold"] = "account"
        with (patch.object(sessions, "live_sessions", return_value=[]),
              patch.object(settle, "run_remote", return_value=(0, "[]")),
              patch.object(settle, "send_message"), patch.object(settle, "alert_user", return_value=True) as alert,
              redirect_stdout(io.StringIO())):
            settle.report_stop([saved])
        alert.assert_called_once()
        push = cast(RenderedPush, alert.call_args.args[0])
        self.assertEqual(push.id, "shutdown.stop_failed")
        self.assertEqual(push.values["stopped"], "One session")
        self.assertNotIn("Stop owner", push.title + "\n" + push.body)

    def test_large_shutdown_failure_group_keeps_total_count_and_full_session_report(self) -> None:
        records = [session_shutdown(top_level(str(index), "Session" + str(index) + "x" * 80),
            progress={"kind": "stop failed", "at": NOW, "reason": "still running"}, requester="requester")
            for index in range(100)]
        for saved in records:
            saved["hold"] = "account"
        with patch.object(settle, "run_remote", return_value=(0, "[]")), patch.object(settle, "send_message") as sender, patch.object(settle, "alert_user", return_value=True) as alert:
            settle.report_stop(records)
        push = cast(RenderedPush, alert.call_args.args[0])
        self.assertEqual(push.values["stopped"], "100 sessions")
        self.assertNotIn("sessions", push.values)
        self.assertLess(len("Action: " + cast(ActionRequired, push.action).text + "\n" + push.body), 1024)
        self.assertIn(records[-1]["session"]["name"], cast(str, sender.call_args.args[2]))

    def test_holdout_alert_names_each_terminal_and_pushes_for_busy_sessions(self) -> None:
        form = session_shutdown(top_level("form", "Owner"), requester="requester")
        busy = session_shutdown(top_level("busy", "Busy"), requester="requester")
        with patch.object(settle, "send_message") as sender, patch.object(settle, "alert_user", return_value=True) as alert:
            settle._send_holdout_alert([(form, "showing a form"), (busy, "busy")])  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(alert.call_count, 2)
        pushes = [cast(RenderedPush, call.args[0]) for call in alert.call_args_list]
        self.assertEqual({push.values["session"] for push in pushes}, {"Owner", "Busy"})
        self.assertEqual([push.id for push in pushes], ["shutdown.holdout", "shutdown.holdout_unanswered"])
        self.assertIn("claude session is showing a form and it can't proceed.", pushes[0].body)
        self.assertEqual(cast(ActionRequired, pushes[0].action).text, "answer the open form")
        self.assertEqual(cast(ActionRequired, pushes[1].action).text, "In a terminal on natedev, stop Busy with the command below")
        self.assertIn("The session Busy has not paused, so the shutdown is stuck.", pushes[1].body)
        self.assertNotIn("form", pushes[1].title + "\n" + pushes[1].message)
        self.assertIn("Busy", cast(str, sender.call_args.args[2]))

    def test_holdout_with_a_form_among_other_details_asks_to_answer_the_form(self) -> None:
        saved = session_shutdown(top_level("held", "pm-move-ghostty"), requester="requester")
        with patch.object(settle, "send_message"), patch.object(settle, "alert_user", return_value=True) as alert:
            settle._send_holdout_alert([(saved, "busy, ahead 3 on main, showing a form, merge in progress")])  # pyright: ignore[reportPrivateUsage]
        push = cast(RenderedPush, alert.call_args.args[0])
        self.assertEqual(push.id, "shutdown.holdout")
        self.assertEqual(cast(ActionRequired, push.action).text, "answer the open form")
        self.assertTrue(push.body.startswith("Reason: shutdown is blocked because pm-move-ghostty's claude session is showing a form and it can't proceed.\n\n"))
        self.assertNotIn("ahead 3", push.body)
        self.assertNotIn("merge in progress", push.body)

    def test_holdout_alert_names_the_session_and_machine_for_the_command(self) -> None:
        owner = top_level("form", "Display name")
        owner["host"] = inventory.HerdrHost(kind="herdr", herdr_session="Actual terminal", pane_id="w1:p1", presentation="detached")
        saved = session_shutdown(owner)
        with patch.object(settle, "send_message"), patch.object(settle, "alert_user", return_value=True) as alert:
            settle._send_holdout_alert([(saved, "showing a form")])  # pyright: ignore[reportPrivateUsage]
        push = cast(RenderedPush, alert.call_args.args[0])
        self.assertEqual(push.values["session"], "Display name")
        self.assertTrue(push.message.startswith("Session: Display name\nAction: "))
        self.assertIn("in a terminal on natedev, run:", push.body.casefold())

    def test_holdout_push_uses_the_canonical_label_without_the_login_email(self) -> None:
        for reason, expected_id in (("showing a form", "shutdown.holdout"), ("waiting", "shutdown.holdout_unanswered")):
            with self.subTest(reason=reason):
                saved = session_shutdown(top_level("held", "pm-move-ghostty"), then="restart")
                saved["login"] = "held-account@example.com"
                saved["label"] = "claude 4"
                with patch.object(settle, "send_message"), patch.object(settle, "alert_user", return_value=True) as alert:
                    settle._send_holdout_alert([(saved, reason)])  # pyright: ignore[reportPrivateUsage]
                push = cast(RenderedPush, alert.call_args.args[0])
                self.assertEqual(push.id, expected_id)
                self.assertNotIn("@", push.title + "\n" + push.message)
                self.assertEqual(push.title, "pm-move-ghostty restart is blocked" if expected_id == "shutdown.holdout" else "Your Claude restart is stuck")
                self.assertIn("shutdown.py now claude4 --here --only held", push.body)

    def test_pending_scheduled_jobs_remain_in_session_reports_without_a_phone_alert(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"), progress={"kind": "timers pending", "at": NOW, "timers": []}, requester="requester")
        with patch.object(settle, "run_remote", return_value=(0, "[]")), patch.object(settle, "send_message") as sender, patch.object(settle, "alert_user", return_value=True) as alert:
            settle.report_stop([saved])
        alert.assert_not_called()
        self.assertIn("timers pending", cast(str, sender.call_args.args[2]))

    def test_holdout_without_a_requesting_session_is_logged_and_pushed(self) -> None:
        busy = session_shutdown(top_level("busy", "Busy"))
        pending = session_shutdown(top_level("pending", "Pending"), progress={"kind": "timers pending", "at": NOW, "timers": []})
        with (patch.object(settle, "run_remote", return_value=(0, "[]")),
              patch.object(settle, "send_message") as sender, patch.object(settle, "alert_user", return_value=True) as alert,
              redirect_stdout(io.StringIO()) as output):
            settle._send_holdout_alert([(busy, "busy")])  # pyright: ignore[reportPrivateUsage]
            settle.report_stop([pending])
        sender.assert_not_called()
        alert.assert_called_once()
        self.assertIn("Busy has not answered the pause", output.getvalue())
        self.assertIn("Pending: timers pending", output.getvalue())

    def test_manual_restart_asks_for_manual_restore_instead_of_retrying_the_automatic_path(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"), progress={"kind": "manual restart", "command": "private resume command"}, requester="requester")
        with patch.object(settle, "run_remote", return_value=(0, "[]")), patch.object(settle, "send_message") as sender, patch.object(settle, "alert_user", return_value=True) as alert:
            settle.report_stop([saved])
        push = cast(RenderedPush, alert.call_args.args[0])
        self.assertEqual(push.id, "shutdown.manual_restart")
        self.assertNotIn("private resume command", push.body)
        self.assertIn('send: "Resume the stopped Claude session named Owner on natedev from its shutdown record."', cast(ActionRequired, push.action).text)
        self.assertIn("on the natedev computer", cast(ActionRequired, push.action).text)
        self.assertIn("Its terminal cannot restart on its own", push.body)
        self.assertIn("manual restart", cast(str, sender.call_args.args[2]))

    def test_reports_with_no_failed_sessions_never_ask_the_user_to_retry(self) -> None:
        for records in ([], [session_shutdown(top_level("owner", "Owner"),
                progress={"kind": "stopped", "at": NOW}, requester="requester")]):
            with (self.subTest(records=records),
                  patch.object(settle, "run_remote", return_value=(0, "[]")),
                  patch.object(settle, "send_message") as sender,
                  patch.object(settle, "alert_user", return_value=True) as alert):
                settle.report_stop(records)
            alert.assert_not_called()
            if records:
                self.assertIn("Owner: stopped", cast(str, sender.call_args.args[2]))
        for entry in catalog():
            if entry.id in ("shutdown.stop_failed", "shutdown.restart_failed"):
                self.assertTrue(all(example.values["stopped"] != "0 sessions" for example in entry.examples))

    def test_shutdown_catalog_examples_render_all_named_cases(self) -> None:
        for entry in catalog():
            if not entry.id.startswith("shutdown."):
                continue
            for example in entry.examples:
                with self.subTest(id=entry.id, example=example.name):
                    push = render_entry(entry, example.values)
                    self.assertIsInstance(push.action, ActionRequired)
                    self.assertLessEqual(len(push.title), 250)
                    self.assertLessEqual(len("Action: " + cast(ActionRequired, push.action).text + "\n" + push.body), 1024)

    def test_report_stop_treats_owner_resource_issues_as_a_partial_stop(self) -> None:
        for kind in ("stopped", "already gone", "process identity lost"):
            for alerts in ("user", "none"):
                saved = session_shutdown(top_level("owner", "Owner"), progress=cast(record.SessionProgress, cast(object, {"kind": kind, "at": NOW})), requester="requester")
                saved["hold"], saved["alerts"] = "account", alerts
                saved["stop_issues"] = [record.UnitSessionLeftRunning(kind="unit session left running", at=NOW, host="herdr", session_name="unit", cause="stop not confirmed")]
                with patch.object(settle, "run_remote", return_value=(0, "[]")), patch.object(settle, "send_message", return_value={"kind": "sent"}) as send, patch.object(settle, "alert_user", return_value=True) as alert:
                    settle.report_stop([saved])
                self.assertEqual(send.call_args.args[1], f"{LABEL}: stop partial")
                self.assertIn(f"now {LOGIN} --here --only owner", cast(str, send.call_args.args[2]))
                self.assertEqual(alert.call_count, int(alerts == "user"))

    def test_now_leaves_failed_stop_retries_to_the_conductor(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"), progress={"kind": "stop failed", "at": NOW, "reason": "still running"})
        self.save(saved)
        with patch.object(stop_work, "stop_session") as stop, patch.object(settle, "_launch_conductor") as launch:
            self.assertEqual(settle.now(Account("claude", LOGIN, LABEL), True), 0)
        stop.assert_not_called()
        launch.assert_called_once_with(LOGIN, LABEL)

    def test_now_retries_stopped_owner_resource_cleanup_in_its_conductor(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"), progress={"kind": "stopped", "at": NOW})
        saved["stop_issues"] = [record.CodexServerLeftRunning(kind="codex server left running", at=NOW, run_dir="/tmp/run", cause="stop not confirmed")]
        self.save(saved)
        def cleaned(current: record.SessionShutdown) -> None:
            current["stop_issues"] = []
        def cleanup(current: record.SessionShutdown) -> record.SessionShutdown:
            self.assertEqual(current["force"], "now")
            return record.update_session(LOGIN, "owner", cleaned)
        with patch.object(settle, "_launch_conductor", side_effect=self.run_conductor), patch.object(settle, "run_inventory", return_value=machine_inventory([saved["session"]])), patch.object(stop_work, "stop_session", side_effect=ignoring_idle_reason(cleanup)) as stop, patch.object(settle, "report_stop"):
            self.assertEqual(settle.now(Account("claude", LOGIN, LABEL), True), 0)
        stop.assert_called_once()
        current = record.find_session(LOGIN, "owner")
        self.assertIsNotNone(current)
        if current is not None:
            self.assertEqual(current["stop_issues"], [])

    def test_now_reports_a_persistent_cleanup_failure_after_one_owned_retry(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"), progress={"kind": "already gone", "at": NOW})
        saved["stop_issues"] = [record.UnitSessionLeftRunning(kind="unit session left running", at=NOW, host="herdr", session_name="unit", cause="stop not confirmed")]
        self.save(saved)
        def failed_cleanup(current: record.SessionShutdown) -> record.SessionShutdown:
            return current
        with patch.object(settle, "_launch_conductor", side_effect=self.run_conductor), patch.object(settle, "run_inventory", return_value=machine_inventory([])), patch.object(stop_work, "stop_session", side_effect=ignoring_idle_reason(failed_cleanup)) as stop, patch.object(settle, "report_stop") as report:
            self.assertEqual(settle.now(Account("claude", LOGIN, LABEL), True), 0)
        stop.assert_called_once()
        outcome = cast(list[record.SessionShutdown], report.call_args.args[0])[0]
        self.assertTrue(record.stop_incomplete(outcome))
        self.assertEqual(outcome["force"], "wait for ready")


    def test_cancel_only_keeps_hold_until_last_record_of_its_request_leaves(self) -> None:
        first, second, separate = [session_shutdown(top_level(identity, identity)) for identity in ("first", "second", "separate")]
        separate["request_id"] = "another-request"
        for saved in (first, second, separate):
            self.save(saved)
        held = record.AccountHold(login=LOGIN, label=LABEL, machine="natedev", requested_at=NOW,
            requested_by={"kind": "terminal"}, request_id="request-one", then="stop")
        record.set_account_hold(held)
        with patch.object(settle, "_retire_shutdown_messages"):
            self.assertEqual(settle.cancel(Account("claude", LOGIN, LABEL), True, only=["first"]), 0)
            self.assertEqual(record.account_hold(LOGIN), held)
            self.assertEqual(settle.cancel(Account("claude", LOGIN, LABEL), True, only=["second"]), 0)
        self.assertIsNone(record.account_hold(LOGIN))
        self.assertEqual(record.session_records(LOGIN), [separate])

    def test_cancel_only_keeps_hold_when_its_stopped_sibling_remains(self) -> None:
        waiting = session_shutdown(top_level("waiting", "Waiting"))
        stopped = session_shutdown(top_level("stopped", "Stopped"), progress={"kind": "stopped", "at": NOW})
        self.save(waiting)
        self.save(stopped)
        held = record.AccountHold(login=LOGIN, label=LABEL, machine="natedev", requested_at=NOW,
            requested_by={"kind": "terminal"}, request_id="request-one", then="stop")
        record.set_account_hold(held)
        with patch.object(settle, "_retire_shutdown_messages"):
            self.assertEqual(settle.cancel(Account("claude", LOGIN, LABEL), True, only=["waiting"]), 0)
        self.assertEqual(record.account_hold(LOGIN), held)
        self.assertEqual(record.session_records(LOGIN), [stopped])

    def test_cancel_only_live_failed_stop_restores_resources_and_names_running_session(self) -> None:
        saved = session_shutdown(top_level("failed", "Failed"), progress={"kind": "stop failed", "at": NOW, "reason": "still live"})
        saved["timers"] = [record.TimerRestore(instance="timer", was_enabled=True)]
        saved["settle_message"] = {"kind": "sent", "at": NOW}
        sibling = session_shutdown(top_level("other", "Other"))
        self.save(saved)
        self.save(sibling)
        output = io.StringIO()
        with patch.object(settle, "inventory", return_value=machine_inventory([saved["session"]])), patch.object(settle, "run_notifier") as notifier, patch.object(settle, "_retire_shutdown_messages") as retire, patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")) as send, redirect_stdout(output):
            self.assertEqual(settle.cancel(Account("claude", LOGIN, LABEL), True, only=["failed"]), 0)
        self.assertEqual(output.getvalue(), "Failed: taken out, still running\n")
        notifier.assert_called_once_with("start", "timer")
        retire.assert_called_once_with(LOGIN, "failed")
        self.assertIn("cancelled by the user: continue where you were", cast(str, cast(object, send.call_args.args[2])))
        self.assertIsNone(record.find_session(LOGIN, "failed"))
        self.assertEqual(record.find_session(LOGIN, "other"), sibling)

    def test_cancel_only_failed_stop_with_changed_process_identity_is_retained(self) -> None:
        saved = session_shutdown(top_level("failed", "Failed"), progress={"kind": "stop failed", "at": NOW, "reason": "still live"})
        self.save(saved)
        reused = top_level("failed", "Failed")
        reused["proc_start"] = "new-start"
        with patch.object(settle, "inventory", return_value=machine_inventory([reused])), patch.object(settle, "_restore_record") as restore, redirect_stdout(io.StringIO()):
            self.assertEqual(settle.cancel(Account("claude", LOGIN, LABEL), True, only=["failed"]), 1)
        restore.assert_not_called()
        self.assertEqual(record.find_session(LOGIN, "failed"), saved)

    def test_now_only_failed_stop_marks_force_and_workers_retry_to_conductor(self) -> None:
        saved = session_shutdown(top_level("failed", "Failed"), progress={"kind": "stop failed", "at": NOW, "reason": "still live"})
        other = session_shutdown(top_level("other", "Other"))
        self.save(saved)
        self.save(other)
        output = io.StringIO()
        with patch.object(settle, "_launch_conductor") as conductor, patch.object(stop_work, "stop_session") as stop, redirect_stdout(output):
            self.assertEqual(settle.now(Account("claude", LOGIN, LABEL), True, only=["failed"]), 0)
        self.assertEqual(output.getvalue(), "Failed: stopping now\n")
        conductor.assert_called_once_with(LOGIN, LABEL)
        stop.assert_not_called()
        current = record.find_session(LOGIN, "failed")
        assert current is not None
        self.assertEqual(current["force"], "now")
        self.assertIsInstance(settle.next_step(current, self.matching(current), [other], NOW_UTC), settle.StopSession)
        self.assertEqual(record.find_session(LOGIN, "other"), other)

    def test_cancel_only_failed_stop_lost_to_stop_claim_keeps_timers_and_names_stopping(self) -> None:
        saved = session_shutdown(top_level("failed", "Failed"), progress={"kind": "stop failed", "at": NOW, "reason": "still live"})
        saved["timers"] = [record.TimerRestore(instance="timer", was_enabled=True)]
        self.save(saved)
        withdraw = record.withdraw_failed_stop
        def stop_first(login: str, identity: str, check: Callable[[record.SessionShutdown], bool], restore: Callable[[record.SessionShutdown], None]) -> record.SessionWithdrawn | record.SessionRetained:
            def claim(current: record.SessionShutdown) -> None:
                current["progress"] = {"kind": "stopping", "at": NOW}
            _ = record.update_session(login, identity, claim)
            return withdraw(login, identity, check, restore)
        output = io.StringIO()
        with patch.object(record, "withdraw_failed_stop", side_effect=stop_first), patch.object(settle, "inventory") as observe, patch.object(settle, "run_notifier") as notifier, patch.object(settle, "_retire_shutdown_messages") as retire, redirect_stdout(output):
            self.assertEqual(settle.cancel(Account("claude", LOGIN, LABEL), True, only=["failed"]), 1)
        self.assertEqual(output.getvalue(), f"Failed was not cancelled: stopping; in a terminal on natedev: ~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py restart {LOGIN} --here --only failed brings it back\n")
        observe.assert_not_called()
        notifier.assert_not_called()
        retire.assert_not_called()
        current = record.find_session(LOGIN, "failed")
        assert current is not None
        self.assertEqual(current["progress"], {"kind": "stopping", "at": NOW})
        self.assertEqual(current["timers"], saved["timers"])

    def test_now_only_stopped_owner_with_leftover_cleanup_is_retried_by_conductor(self) -> None:
        saved = session_shutdown(top_level("partial", "Partial"), progress={"kind": "stopped", "at": NOW})
        saved["stop_issues"] = [record.CodexServerLeftRunning(kind="codex server left running", at=NOW, run_dir="/tmp/run", cause="stop not confirmed")]
        self.save(saved)
        with patch.object(settle, "_launch_conductor") as conductor, patch.object(stop_work, "stop_session") as stop, redirect_stdout(io.StringIO()):
            self.assertEqual(settle.now(Account("claude", LOGIN, LABEL), True, only=["partial"]), 0)
        conductor.assert_called_once_with(LOGIN, LABEL)
        stop.assert_not_called()
        current = record.find_session(LOGIN, "partial")
        assert current is not None
        self.assertEqual(current["force"], "now")
        self.assertIsInstance(settle.next_step(current, self.matching(current), [], NOW_UTC), settle.StopSession)

    def test_selected_cancel_of_peer_keeps_local_hold_even_when_peer_retains_record(self) -> None:
        held = record.AccountHold(login=LOGIN, label=LABEL, machine="natedev", requested_at=NOW,
            requested_by={"kind": "terminal"}, request_id="held-request", then="stop")
        record.set_account_hold(held)
        with patch.object(settle, "inventory", return_value=machine_inventory([])), patch.object(settle, "run_remote", return_value=(1, "Peer was not cancelled: stopped")) as peer, redirect_stdout(io.StringIO()):
            self.assertEqual(settle.cancel(Account("claude", LOGIN, LABEL), only=["peer-id"]), 1)
        peer.assert_called_once_with(["cancel", LOGIN, "--here", "--only", "peer-id"])
        self.assertEqual(record.account_hold(LOGIN), held)

    def test_selected_cancel_of_another_request_keeps_local_account_hold(self) -> None:
        held = record.AccountHold(login=LOGIN, label=LABEL, machine="natedev", requested_at=NOW,
            requested_by={"kind": "terminal"}, request_id="held-request", then="stop", registration="in progress")
        record.set_account_hold(held)
        with patch.object(settle, "_retire_shutdown_messages"), redirect_stdout(io.StringIO()):
            added = settle.begin(LOGIN, only=["selected"], request_id="selected-request",
                report=machine_inventory([top_level("selected", "Selected", status="busy")]))
            self.assertEqual(len(added), 1)
            self.assertEqual(settle.cancel(Account("claude", LOGIN, LABEL), True, only=["selected"]), 0)
        self.assertIsNone(record.find_session(LOGIN, "selected"))
        outcome = record.lookup_account_hold(LOGIN)
        self.assertIsInstance(outcome, record.AccountHeld)
        assert isinstance(outcome, record.AccountHeld)
        self.assertEqual((outcome.hold["request_id"], outcome.hold["then"]), ("held-request", "stop"))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(shutdown.launch_blocked(Account("claude", LOGIN, LABEL),
                                                    NewWorkLaunchPurpose()), 0)

    def test_cancelling_joined_restart_lets_the_finished_stop_release_its_hold(self) -> None:
        with patch.object(settle, "_retire_shutdown_messages"), redirect_stdout(io.StringIO()):
            self.assertEqual(settle.begin(LOGIN, then="stop", request_id="stop-account",
                                         report=machine_inventory([])), [])
            added = settle.begin(LOGIN, then="restart", only=["selected"], request_id="restart-selected",
                report=machine_inventory([top_level("selected", "Selected", status="busy")]))
            self.assertEqual(len(added), 1)
            self.assertEqual(settle.cancel(Account("claude", LOGIN, LABEL), True, only=["selected"]), 0)
            with patch.object(settle, "report_stop"):
                self.assertEqual(settle.conduct(LOGIN), 0)
            self.assertIsInstance(record.lookup_account_hold(LOGIN), record.AccountOpen)
            self.assertEqual(shutdown.launch_blocked(Account("claude", LOGIN, LABEL),
                                                    NewWorkLaunchPurpose()), 1)
            _ = restart_work._up(LOGIN, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(record.session_records(LOGIN), [])
        self.assertIsInstance(record.lookup_account_hold(LOGIN), record.AccountOpen)

    def test_selected_now_prints_peer_refusal_as_answer_without_not_reached_warning(self) -> None:
        line = "Peer is stopping; nothing to stop"
        output, error = io.StringIO(), io.StringIO()
        with patch.object(settle, "inventory", return_value=machine_inventory([])), patch.object(settle, "run_remote", return_value=(1, line)) as peer, redirect_stdout(output), redirect_stderr(error):
            self.assertEqual(settle.now(Account("claude", LOGIN, LABEL), only=["peer-id"]), 1)
        self.assertEqual((output.getvalue(), error.getvalue()), (line + "\n", ""))
        peer.assert_called_once_with(["now", LOGIN, "--here", "--only", "peer-id"])
    def test_holdout_alert_uses_delivery_time_or_request_time_and_is_told_once(self) -> None:
        deliveries: list[record.SettleMessage] = [{"kind": "not sent"}, {"kind": "queued", "at": NOW, "reason": "pending"}, {"kind": "sent", "at": (NOW_UTC + timedelta(minutes=2)).isoformat()}]
        for delivery in deliveries:
            saved = session_shutdown(top_level("owner", "Owner"))
            saved["settle_message"] = delivery
            baseline = NOW_UTC + (timedelta(minutes=2) if delivery["kind"] == "sent" else timedelta())
            self.assertFalse(settle._holdout_due(saved, baseline + settle.NOT_ANSWERING_AFTER - timedelta(seconds=1)))  # pyright: ignore[reportPrivateUsage]
            self.assertTrue(settle._holdout_due(saved, baseline + settle.NOT_ANSWERING_AFTER))  # pyright: ignore[reportPrivateUsage]
            saved["alerted_at"] = (baseline + settle.NOT_ANSWERING_AFTER).isoformat()
            self.assertFalse(settle._holdout_due(saved, baseline + timedelta(hours=3)))  # pyright: ignore[reportPrivateUsage]
            del saved["alerted_at"]
            saved["alerts"] = "none"
            self.assertFalse(settle._holdout_due(saved, baseline + timedelta(hours=3)))  # pyright: ignore[reportPrivateUsage]

    def test_plain_wait_reaches_the_requester_and_the_phone(self) -> None:
        for reason in ("waiting", "busy", "ahead 2 on main", "merge in progress", "Codex worker writer is still running",
                       "busy, ahead 3 on main, merge in progress"):
            with self.subTest(reason=reason):
                saved = session_shutdown(top_level("owner", "Owner"), requester="requester")
                with patch.object(settle, "alert_user", return_value=True) as alert, patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")) as sender:
                    settle._send_holdout_alert([(saved, reason)])  # pyright: ignore[reportPrivateUsage]
                alert.assert_called_once()
                push = cast(RenderedPush, alert.call_args.args[0])
                self.assertEqual(push.id, "shutdown.holdout_unanswered")
                self.assertEqual(cast(ActionRequired, push.action).text, "In a terminal on natedev, stop Owner with the command below")
                self.assertNotIn("form", push.title + "\n" + push.message)
                self.assertTrue(push.body.startswith("Your Claude shutdown waits for each session to pause. The session Owner has not paused, so the shutdown is stuck.\n\n"))
                self.assertIn("Owner has not answered the pause", cast(str, sender.call_args.args[2]))
                self.assertIn(f"now {LOGIN} --here --only owner", cast(str, sender.call_args.args[2]))

    def test_holdout_does_not_push_again_when_a_form_opens_after_wait_alert(self) -> None:
        owner = top_level("owner", "Owner", status="busy")
        self.save(session_shutdown(owner, requester="requester"))
        at = NOW_UTC + timedelta(minutes=5)
        shown = [False]
        def form(_owner: inventory.Session) -> bool:
            return shown[0]
        def end(_seconds: float) -> None:
            raise PassObserved()
        for showing, pushes in ((False, 1), (True, 0), (True, 0)):
            shown[0] = showing
            with patch.object(settle, "now_utc", return_value=at), patch.object(settle, "run_inventory", return_value=machine_inventory([owner])), patch.object(settle, "_showing_form", side_effect=form), patch.object(settle, "_send_settle_message"), patch.object(settle, "alert_user", return_value=True) as alert, patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")), patch.object(time, "sleep", side_effect=end), self.assertRaises(PassObserved):
                _ = settle.conduct(LOGIN)
            self.assertEqual(alert.call_count, pushes)

    def test_holdout_alert_groups_form_and_waiting_requester_while_two_sessions_return(self) -> None:
        owners: list[inventory.Session] = [top_level(name, name) for name in ("form", "first", "second", "requester")]
        for owner in owners:
            saved = session_shutdown(owner, requester="requester", then="restart",
                progress={"kind": "waiting"} if owner["session_id"] in {"requester", "form"} else {"kind": "ready", "at": NOW})
            if owner["session_id"] == "form":
                saved["settle_message"] = record.SettleMessageSent(kind="sent", at=NOW)
            self.save(saved)
        observed = threading.Event()
        def phone(_push: RenderedPush, *, machine: str = "") -> None:
            del machine
            observed.set()
        def end(_seconds: float) -> None:
            if observed.is_set() and record.find_session(LOGIN, "first") is None and record.find_session(LOGIN, "second") is None:
                raise PassObserved()
        at = NOW_UTC + timedelta(minutes=5)
        def form(owner: inventory.Session) -> bool:
            return owner["session_id"] == "form"
        with patch.object(settle, "now_utc", return_value=at), patch.object(settle, "run_inventory", return_value=machine_inventory(owners)), patch.object(settle, "SETTLE_INTERVAL_SECONDS", 0.001), patch.object(settle, "_showing_form", side_effect=form), patch.object(stop_work, "stop_session", side_effect=ignoring_idle_reason(self.fake_stop)), patch.object(restart_work, "relaunch_session", side_effect=self.fake_launch), patch.object(restart_work, "wait_for_confirmation", side_effect=self.all_live), patch.object(restart_work, "confirm_entry", side_effect=self.fake_confirm), patch.object(settle, "alert_user", side_effect=phone) as alert, patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")) as sender, patch.object(time, "sleep", side_effect=end), self.assertRaises(PassObserved):
            _ = settle.conduct(LOGIN)
        self.assertEqual(alert.call_count, 2)
        pushes = [cast(RenderedPush, call.args[0]) for call in alert.call_args_list]
        self.assertEqual({push.values["session"] for push in pushes}, {"form", "requester"})
        self.assertEqual({push.values["session"]: push.id for push in pushes},
            {"form": "shutdown.holdout", "requester": "shutdown.holdout_unanswered"})
        requester_texts = [cast(str, sent.args[2]) for sent in sender.call_args_list
            if sent.args[0] == "session:requester"]
        self.assertEqual(len(requester_texts), 1)
        self.assertIn(f"now {LOGIN} --here --only form", requester_texts[0])
        self.assertIn(f"now {LOGIN} --here --only requester", requester_texts[0])
        for identity in ("form", "requester"):
            saved = record.find_session(LOGIN, identity)
            assert saved is not None and "alerted_at" in saved
            self.assertEqual(saved["alerted_at"], at.isoformat())
        requester = record.find_session(LOGIN, "requester")
        assert requester is not None
        self.assertEqual(requester["settle_message"], {"kind": "not sent"})

    def test_holdout_paused_phone_and_requester_sends_do_not_delay_ready_stop(self) -> None:
        for paused in ("phone", "requester"):
            with self.subTest(paused=paused):
                owner = top_level("held-" + paused, "Held")
                ready = top_level("ready-" + paused, "Ready")
                held = session_shutdown(owner, requester=owner["session_id"])
                moving = session_shutdown(ready, progress={"kind": "ready", "at": NOW})
                self.save(held)
                self.save(moving)
                entered, stopped = threading.Event(), threading.Event()
                main_thread = threading.get_ident()
                def pause(*_args: object, **_kwargs: object) -> settle.MessageSent:
                    self.assertNotEqual(threading.get_ident(), main_thread)
                    entered.set()
                    self.assertTrue(stopped.wait(2))
                    return settle.MessageSent(kind="sent")
                def stop(saved: record.SessionShutdown) -> record.SessionShutdown:
                    self.assertTrue(entered.wait(2))
                    result = self.fake_stop(saved)
                    stopped.set()
                    return result
                def end(_seconds: float) -> None:
                    if stopped.is_set():
                        raise PassObserved()
                def form(shown: inventory.Session) -> bool:
                    return shown["session_id"] == owner["session_id"]
                with patch.object(settle, "now_utc", return_value=NOW_UTC + timedelta(minutes=5)), patch.object(settle, "run_inventory", return_value=machine_inventory([owner, ready])), patch.object(settle, "SETTLE_INTERVAL_SECONDS", 0.001), patch.object(settle, "_showing_form", side_effect=form), patch.object(stop_work, "stop_session", side_effect=ignoring_idle_reason(stop)), patch.object(settle, "alert_user", side_effect=pause if paused == "phone" else None), patch.object(settle, "send_message", side_effect=pause if paused == "requester" else None, return_value=settle.MessageSent(kind="sent")), patch.object(time, "sleep", side_effect=end), self.assertRaises(PassObserved):
                    _ = settle.conduct(LOGIN)
                record.archive_session(LOGIN, owner["session_id"])
                record.archive_session(LOGIN, ready["session_id"])

    def test_remote_control_conductor_check_and_fixer_leave_other_records_moving(self) -> None:
        for policy in ("user", "none"):
            with self.subTest(policy=policy):
                restored = session_shutdown(top_level("restored-" + policy, "Restored"), progress={"kind": "stopped", "at": NOW}, then="restart")
                restored["alerts"] = policy
                other = session_shutdown(top_level("other-" + policy, "Other"), progress={"kind": "waiting"})
                await_ready_mark(other)
                other["alerts"] = "none"
                self.save(restored)
                self.save(other)
                checking, stopped = threading.Event(), threading.Event()
                main_thread = threading.get_ident()
                def confirmed(ids: frozenset[str], _timeout: float) -> restart_work.RegistryConfirmation:
                    return restart_work.RegistryConfirmation(tuple(sessions.SessionRecord(
                        pid=912, sessionId=identity, name="Restored", procStart="resumed-start",
                        messagingSocketPath="", updatedAt=1, tmux="", formerNames=[])
                        for identity in sorted(ids)))
                def check(_records: object, _timeout: float) -> frozenset[str]:
                    self.assertNotEqual(threading.get_ident(), main_thread)
                    checking.set()
                    self.assertTrue(stopped.wait(2))
                    return frozenset()
                def stop(saved: record.SessionShutdown) -> record.SessionShutdown:
                    self.assertEqual(saved["progress"]["kind"], "ready")
                    result = self.fake_stop(saved)
                    stopped.set()
                    return result
                def observe(_login: str, _only: object = (), *, refresh_desktops: bool = True) -> inventory.Inventory:
                    del refresh_desktops
                    if checking.is_set():
                        def ready(current: record.SessionShutdown) -> None:
                            if current["progress"]["kind"] == "waiting":
                                current["progress"] = {"kind": "ready", "at": NOW}
                        _ = record.update_session(LOGIN, other["session"]["session_id"], ready)
                    return machine_inventory([restored["session"], other["session"]])
                with patch.object(settle, "run_inventory", side_effect=observe), patch.object(settle, "SETTLE_INTERVAL_SECONDS", 0.001), patch.object(restart_work, "relaunch_session", side_effect=self.fake_launch), patch.object(restart_work, "wait_for_confirmation", side_effect=confirmed), patch.object(restart_work, "confirm_entry", side_effect=self.fake_confirm), patch.object(restart_work, "wait_for_remote_control", side_effect=check), patch.object(remote_control, "launch_fixer") as fixer, patch.object(stop_work, "stop_session", side_effect=ignoring_idle_reason(stop)), patch.object(settle, "_send_settle_message"), patch.object(settle, "alert_user", return_value=True) as alert, patch.object(settle, "report_stop"), redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(settle.conduct(LOGIN), 0)
                self.assertNotIn("Remote Control", output.getvalue())
                self.assertIsNone(record.find_session(LOGIN, restored["session"]["session_id"]))
                alert.assert_not_called()
                fixer.assert_called_once_with("restored-" + policy, 912, "resumed-start", "Restored", "Restored")
                record.archive_session(LOGIN, other["session"]["session_id"])

    def test_user_alert_wait_covers_configured_review_and_remote_transport(self) -> None:
        self.allow_leaf(ShutdownSendLeaf.DELIVER_MESSAGE)
        config = self.root / "notify.conf"
        _ = config.write_text("REVIEW_TIMEOUT_SECONDS=90\n")
        with patch.object(review, "NOTIFY_CONFIG", config):
            for machine, expected in (("", 220), ("natedev", 260)):
                with (self.subTest(machine=machine),
                      patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as sender):
                    _ = settle.alert_user(render("nixos.ups_battery", charge="30", minutes="5"), machine=machine)
                self.assertEqual(sender.call_args.kwargs["timeout"], expected)
            with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as sender:
                self.assertEqual(settle.send_message("session:owner", "Shutdown", "Stop work"), {"kind": "sent"})
            self.assertEqual(sender.call_args.kwargs["timeout"], 60)

    def test_user_alert_timeout_reports_failed_delivery_without_interrupting_shutdown(self) -> None:
        self.allow_leaf(ShutdownSendLeaf.DELIVER_MESSAGE)
        with (patch.object(review, "user_send_seconds", return_value=115),
              patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("fake send", 130)),
              redirect_stderr(io.StringIO()) as errors):
            _ = settle.alert_user(render("nixos.ups_battery", charge="30", minutes="5"))
        self.assertIn("shutdown: user alert failed: send to user timed out after 130 seconds", errors.getvalue())

    def test_ups_alert_ignores_account_hold_and_every_session_progress(self) -> None:
        progresses: list[record.SessionProgress] = [{"kind": "waiting"}, {"kind": "ready", "at": NOW}, {"kind": "stopping", "at": NOW}, {"kind": "stopped", "at": NOW}, {"kind": "already gone", "at": NOW}, {"kind": "process identity lost", "at": NOW}, {"kind": "stop failed", "at": NOW, "reason": "failed"}, {"kind": "restarting", "at": NOW, "prior": {"kind": "stopped", "at": NOW}}, {"kind": "restarted", "at": NOW}, {"kind": "restart failed", "at": NOW, "reason": "failed"}, {"kind": "manual restart", "command": "resume"}, {"kind": "timers pending", "at": NOW, "timers": []}]
        held = record.AccountHold(login=LOGIN, label=LABEL, machine="natedev", requested_at=NOW, request_id="held", requested_by={"kind": "terminal"}, then="stop")
        record.set_account_hold(held)
        self.allow_leaf(ShutdownSendLeaf.DELIVER_MESSAGE)
        for index, progress in enumerate(progresses):
            saved = session_shutdown(top_level(str(index), str(index)), progress=progress)
            self.save(saved)
            with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as sender, patch.object(record, "session_records", side_effect=AssertionError("alert consulted shutdown state")), patch.object(review, "user_send_seconds", return_value=85):
                _ = settle.alert_user(render("nixos.ups_battery", charge="30", minutes="5"))
            self.assertEqual(sender.call_args.kwargs["input"], "")
            self.assertIn("nixos.ups_battery", cast(list[str], sender.call_args.args[0]))
            self.assertEqual(sender.call_args.kwargs["timeout"], 100)
            self.assertIn("--to", cast(list[str], cast(object, sender.call_args.args[0])))

    def test_remote_control_paused_fixer_launch_does_not_delay_later_ready_session(self) -> None:
        restored = session_shutdown(top_level("restored", "Restored"), progress={"kind": "stopped", "at": NOW}, then="restart")
        other = session_shutdown(top_level("other", "Other"))
        await_ready_mark(other)
        self.save(restored)
        self.save(other)
        entered, stopped = threading.Event(), threading.Event()
        main_thread = threading.get_ident()
        def launch_fixer(*_args: object, **_kwargs: object) -> None:
            self.assertNotEqual(threading.get_ident(), main_thread)
            entered.set()
            self.assertTrue(stopped.wait(2))
        def observe(_login: str, _only: object = (), *, refresh_desktops: bool = True) -> inventory.Inventory:
            del refresh_desktops
            if entered.is_set():
                def ready(current: record.SessionShutdown) -> None:
                    if current["progress"]["kind"] == "waiting":
                        current["progress"] = {"kind": "ready", "at": NOW}
                _ = record.update_session(LOGIN, "other", ready)
            return machine_inventory([restored["session"], other["session"]])
        def stop(saved: record.SessionShutdown) -> record.SessionShutdown:
            self.assertEqual(saved["progress"]["kind"], "ready")
            result = self.fake_stop(saved)
            stopped.set()
            return result
        with patch.object(settle, "run_inventory", side_effect=observe), patch.object(settle, "SETTLE_INTERVAL_SECONDS", 0.001), patch.object(restart_work, "relaunch_session", side_effect=self.fake_launch), patch.object(restart_work, "wait_for_confirmation", side_effect=self.all_live), patch.object(restart_work, "confirm_entry", side_effect=self.fake_confirm), patch.object(restart_work, "wait_for_remote_control", return_value=frozenset()), patch.object(stop_work, "stop_session", side_effect=ignoring_idle_reason(stop)), patch.object(settle, "_send_settle_message"), patch.object(remote_control, "launch_fixer", side_effect=launch_fixer) as fixer, patch.object(settle, "alert_user", return_value=True) as alert, patch.object(settle, "report_stop"), redirect_stdout(io.StringIO()):
            self.assertEqual(settle.conduct(LOGIN), 0)
        fixer.assert_called_once()
        alert.assert_not_called()
        self.assertTrue(stopped.is_set())

    def test_holdout_alert_from_mac_routes_phone_to_natedev_and_same_text_to_requester(self) -> None:
        saved = session_shutdown(top_level("held", "Held"), requester="requester")
        saved["machine"] = "Mac"
        with patch.object(settle, "local_machine", return_value="Mac"), patch.object(settle, "alert_user", return_value=True) as alert, patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")) as sender:
            settle._send_holdout_alert([(saved, "showing a form")])  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(alert.call_args.kwargs, {"machine": "natedev"})
        push = cast(RenderedPush, alert.call_args.args[0])
        self.assertIn("Held", push.title)
        self.assertIn("shutdown", push.title)
        self.assertIn("is blocked", push.title)
        self.assertIn("in a terminal on mac, run:", push.body.casefold())
        self.assertIn(f"now {LABEL} --here --only held", push.body)
        self.assertIn(f"now {LOGIN} --here --only held", cast(str, sender.call_args.args[2]))



class PromptDetectionTests(unittest.TestCase):
    RULE: str = "─" * 40
    PERMISSION: str = "Bash command\n  rm -rf build\nDo you want to proceed?\n❯ 1. Yes\n  2. No\nEsc to cancel"

    def test_words_that_contain_form_never_report_a_form(self) -> None:
        transcript = ("The information is in the right format.\nWe perform the platform check.\n"
                      "Fill in the form, or press esc to cancel the edit.\n")
        for bottom in ("", f"{self.RULE}\n❯ \n{self.RULE}\n  ? for shortcuts"):
            with self.subTest(input_box=bool(bottom)):
                self.assertFalse(settle.waits_on_prompt(transcript + bottom))

    def test_a_permission_prompt_waits_for_the_user(self) -> None:
        self.assertTrue(settle.waits_on_prompt(f"Earlier output\n{self.RULE}\n{self.PERMISSION}\n"))
        self.assertTrue(settle.waits_on_prompt(f"{self.RULE}\n❯ 1. Yes\n  2. No\nEnter to select"))

    def test_a_prompt_scrolled_above_the_input_box_no_longer_waits(self) -> None:
        self.assertFalse(settle.waits_on_prompt(f"{self.PERMISSION}\nDone.\n{self.RULE}\n❯ \n{self.RULE}"))

    def test_a_busy_session_never_reports_a_form(self) -> None:
        for status, expected in (("busy", False), ("idle", True)):
            with (self.subTest(status=status),
                  patch.object(settle, "_screen", return_value=settle.ScreenText(self.PERMISSION)) as screen):
                self.assertEqual(settle._showing_form(top_level("one", "one", status=status)), expected)  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(screen.called, status != "busy")


if __name__ == "__main__":
    _ = unittest.main()
