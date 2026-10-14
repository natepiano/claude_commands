"""Behavioral tests for the account shutdown settle phase."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait as wait_futures
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from collections.abc import Callable, Generator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast, final, override
from unittest.mock import call, patch

from test_support import registry_confirmation, ShutdownSendLeaf, ShutdownTestCase
import inventory
import record
import sessions
from user_action import ActionRequired
import restart as restart_work
import settle
if TYPE_CHECKING:
    from ..notify import review
else:
    import review
import stop as stop_work
from scripts.accounts import moves
from scripts.production import showrunners
from account import Account


LOGIN = "owner@example.com"
LABEL = "claude 2"
NOW = "2026-10-09T21:49:10+00:00"
NOW_UTC = datetime(2026, 10, 9, 21, 49, 10, tzinfo=timezone.utc)


def setUpModule() -> None:
    root = Path(unittest.enterModuleContext(tempfile.TemporaryDirectory()))
    _ = unittest.enterModuleContext(patch.object(showrunners, "NOTIFIER_STATE_DIR", root))


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
    busy_seats: list[str] | None = None,
) -> inventory.SessionFields:
    return {
        "session_id": session_id,
        "pid": 10_000 + sum(ord(character) for character in session_id),
        "proc_start": f"start-{session_id}",
        "name": name,
        "cwd": f"/tmp/{session_id}",
        "status": status,
        "model": inventory.NoReplyYet(kind="no reply yet"),
        "checkout": checkout(ahead=ahead, path=f"/tmp/{session_id}"),
        "run_dirs": [],
        "codex_servers": (
            [
                inventory.CodexServer(
                    run_dir=f"/tmp/run-{session_id}",
                    pid=20_000,
                    busy_seats=list(busy_seats or []),
                )
            ]
            if busy_seats
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
    busy_seats: list[str] | None = None,
) -> inventory.TopLevelSession:
    return inventory.TopLevelSession(
        **common_session(
            session_id,
            name,
            status=status,
            ahead=ahead,
            timers=timers,
            busy_seats=busy_seats,
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
            session_host="tmux",
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
        host=inventory.TmuxHost(
            kind="tmux",
            server=inventory.TmuxServerNotRecorded(kind="not recorded"),
            session_ref=inventory.TmuxSessionIdNotRecorded(kind="not recorded"),
            tmux_session=name,
            pane=inventory.PaneNotRecorded(kind="not recorded"),
        ),
        production=production,
        doc=f"/tmp/{production}.md",
    )


def seat(
    session_id: str,
    name: str,
    *,
    owner: str | None,
    status: str = "idle",
) -> inventory.SeatSession:
    seat_owner: inventory.SeatOwner = (
        inventory.DirectorOwner(kind="director", session_id=owner)
        if owner is not None
        else inventory.NoDirector(kind="no director")
    )
    return inventory.SeatSession(
        **common_session(session_id, name, status=status),
        kind="seat",
        host=inventory.UnknownHost(kind="unknown"),
        owner=seat_owner,
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
        hold="session", then=then, force="wait for ready", alerts="user", session=session,
        seats=[], timers=[], settle_message={"kind": "not sent"}, where={"kind": "not said"},
        progress=progress or {"kind": "waiting"}, stop_issues=[],
    )


class PassObserved(Exception):
    """The test has observed one complete conducting pass."""


@final
class SettleTests(ShutdownTestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(patch.object(sessions, "remote_control", return_value="active"))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        context = cast(object, self.enterContext(patch.dict(os.environ, {"HOME": str(self.root), "SHUTDOWN_STATE_DIR": str(self.root / "state"), "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions")})))
        del context
        _ = self.enterContext(patch.object(settle, "now_utc", return_value=NOW_UTC))
        _ = self.enterContext(patch.object(settle, "merge_in_progress", return_value=False))
        _ = self.enterContext(patch.object(settle, "_showing_form", return_value=False))

    def save(self, saved: record.SessionShutdown) -> None:
        self.assertTrue(record.create_session(saved))

    def write_refusal_transcript(self, identity: str) -> None:
        path = self.root / ".claude/projects/refusal-fixture" / f"{identity}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({"type": "assistant", "isApiErrorMessage": True,
            "error": "rate_limit", "timestamp": NOW, "uuid": "refusal-entry",
            "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "weekly limit"}]}}) + "\n")

    def test_conductor_service_preserves_read_existing_snapshot_policy(self) -> None:
        self.allow_leaf(ShutdownSendLeaf.LAUNCH_CONDUCTOR)
        for platform in ("linux", "darwin"):
            with (self.subTest(platform=platform), patch.object(sys, "platform", platform),
                  patch.dict(os.environ, {"REFUSAL_WATCH_INVENTORY": "read existing"}),
                  patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as launch):
                settle._launch_conductor(LOGIN, LABEL)  # pyright: ignore[reportPrivateUsage]
            launch.assert_called_once()
            arguments = cast(list[str], launch.call_args.args[0])
            prefix = arguments.index("env")
            self.assertEqual(arguments[prefix:], ["env", "REFUSAL_WATCH_INVENTORY=read existing",
                sys.executable, str(settle.SHUTDOWN), "conduct", LOGIN])

    def test_ghostty_transcript_refusal_enters_usage_hold_without_a_screen(self) -> None:
        owner = top_level("ghostty-refusal", "Ghostty owner")
        owner["host"] = inventory.WindowHost(kind="ghostty", window_shell=123,
            desktop=inventory.DesktopNotInSnapshot(kind="not in snapshot"))
        self.write_refusal_transcript(owner["session_id"])
        saved = session_shutdown(owner)
        with patch.object(settle, "_screen", return_value=settle.ScreenUnreadable("no tmux host")):
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
        with patch.object(settle, "_screen", return_value=settle.ScreenUnreadable("no tmux host")):
            self.assertIsInstance(settle._usage_limit_change(saved, self.matching(saved)),  # pyright: ignore[reportPrivateUsage]
                settle.EnterUsageLimitHold)
            self.assertIsInstance(settle._usage_limit_change(other, self.matching(other)),  # pyright: ignore[reportPrivateUsage]
                settle.NoUsageLimitChange)

    def test_transcript_refusal_hold_preserves_busy_seat_and_upstream_safety(self) -> None:
        owner = top_level("refused-owner", "Refused owner", busy_seats=["Busy worker"])
        self.write_refusal_transcript(owner["session_id"])
        saved = session_shutdown(owner, progress=record.SessionHeldByUsageLimit(kind="held by usage limit", at=NOW))
        saved["target"] = "claude 3"
        saved["source_label"] = LABEL
        saved["why"] = "claude 2 refused: weekly limit until tomorrow"
        self.assertIsInstance(settle.next_step(saved, self.matching(saved), [], NOW_UTC), settle.Hold)
        line = settle.status_record_lines(saved, self.matching(saved))[0]
        self.assertIn("claude 3", line)
        self.assertIn(saved["why"], line)
        owner["codex_servers"] = []
        owner["checkout"] = checkout(ahead=1)
        self.assertIsInstance(settle.next_step(saved, self.matching(saved), [], NOW_UTC), settle.Hold)
        owner["checkout"] = checkout()
        self.assertIsInstance(settle.next_step(saved, self.matching(saved), [], NOW_UTC), settle.StopSession)

    def test_cancelled_move_records_failure_and_removes_only_its_assumed_refusal(self) -> None:
        saved = session_shutdown(top_level("cancelled-owner", "Cancelled owner"), then="restart")
        saved["target"] = "claude 3"
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

    def test_cancel_suppresses_refusal_before_a_watcher_tick_after_withdrawal(self) -> None:
        from scripts.accounts import accounts, refusal_watch
        import agent_accounts
        owner = top_level("cancel-race", "Cancelled owner")
        saved = session_shutdown(owner, then="restart")
        saved["target"] = "claude 3"
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
              patch.object(accounts, "account_identity", return_value=agent_accounts.KnownClaudeAccount(LOGIN, LABEL)),
              patch.object(accounts, "pick", return_value=accounts.PickedAccount("claude 3", self.root / ".claude3",
                  80, accounts.NoResetKnown())),
              patch.object(accounts, "move_sessions", return_value=(
                  accounts.SessionMoveRequested(owner["session_id"], "replacement", "claude 3"),)) as move,
              patch.object(settle, "_restore_record"), redirect_stdout(io.StringIO())):
            self.assertEqual(settle.cancel(Account("claude", LOGIN, LABEL), True), 0)
        self.assertEqual(len(ticks), 1)
        move.assert_not_called()
        self.assertEqual(record.session_records(LOGIN), [])

    def run_conductor(self, login: str, _label: str) -> None:
        self.assertEqual(login, LOGIN)
        self.assertEqual(settle.conduct(login), 0)

    def matching(self, saved: record.SessionShutdown) -> stop_work.FreshSession:
        return stop_work.MatchingSession(kind="matching session", session=saved["session"], seats=saved["seats"])

    def write_owner_pane(self, owner: inventory.Session, tmux: str, *, identity: str = "") -> None:
        directory = self.root / "sessions"
        directory.mkdir(exist_ok=True)
        saved = sessions.SessionRecord(pid=owner["pid"], sessionId=identity or owner["session_id"],
            name=owner["name"], messagingSocketPath="", updatedAt=1, tmux=tmux, formerNames=[])
        _ = (directory / f"{owner['pid']}.json").write_text(json.dumps(saved))

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

    def test_screen_targets_owner_pane_when_other_pane_shows_usage_limit(self) -> None:
        owner = showrunner("owner", "Owner")
        owner["host"] = inventory.TmuxHost(kind="tmux", tmux_session="shared",
            server=inventory.TmuxServerNotRecorded(kind="not recorded"),
            session_ref=inventory.TmuxSessionIdNotRecorded(kind="not recorded"),
            pane=inventory.PaneNotRecorded(kind="not recorded"))
        self.write_owner_pane(owner, "shared:@2.%5")
        other = showrunner("other", "Other")
        other["host"] = owner["host"]
        self.write_owner_pane(other, "shared:@2.%6")
        saved = session_shutdown(owner)
        self.save(saved)
        captured: list[str] = []
        def capture(arguments: list[str], **_keywords: object) -> subprocess.CompletedProcess[str]:
            self.assertEqual(arguments[1:4], ["capture-pane", "-p", "-t"])
            self.assertEqual(_keywords["timeout"], 5)
            target = arguments[arguments.index("-t") + 1]
            captured.append(target)
            return subprocess.CompletedProcess(arguments, 0, "ordinary prompt" if target == "%5" else "hit your session limit", "")
        def end_pass(_seconds: float) -> None:
            raise PassObserved()
        with patch.object(subprocess, "run", side_effect=capture):
            self.assertEqual(settle._screen(other), settle.ScreenText("hit your session limit"))  # pyright: ignore[reportPrivateUsage]
            captured.clear()
            self.assertEqual(settle._screen(owner), settle.ScreenText("ordinary prompt"))  # pyright: ignore[reportPrivateUsage]
            with patch.object(settle, "run_inventory", return_value=machine_inventory([owner])), patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")) as send, patch.object(time, "sleep", side_effect=end_pass), self.assertRaises(PassObserved):
                _ = settle.conduct(LOGIN)
        self.assertTrue(captured)
        self.assertEqual(set(captured), {"%5"})
        send.assert_called_once()
        latest = record.find_session(LOGIN, "owner")
        assert latest is not None
        self.assertEqual(latest["progress"], {"kind": "waiting"})

    def test_unreadable_owner_screens_never_create_a_usage_limit_hold(self) -> None:
        owner = showrunner("owner", "Owner")
        for tmux, identity in (("", "owner"), ("shared:@2.%5", "another"), ("shared:@2", "owner"), ("shared:@2.%bad", "owner")):
            with self.subTest(tmux=tmux, identity=identity):
                self.write_owner_pane(owner, tmux, identity=identity)
                with patch.object(subprocess, "run") as capture:
                    screen = settle._screen(owner)  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(screen, settle.ScreenUnreadable("no pane id"))
                self.assertFalse(settle._held_by_usage_limit(screen))  # pyright: ignore[reportPrivateUsage]
                capture.assert_not_called()
        (self.root / "sessions" / f"{owner['pid']}.json").unlink()
        self.assertEqual(settle._screen(owner), settle.ScreenUnreadable("no pane id"))  # pyright: ignore[reportPrivateUsage]
        self.write_owner_pane(owner, "shared:@2.%5")
        for failure in (OSError("capture unavailable"), subprocess.TimeoutExpired("tmux", 5)):
            with self.subTest(failure=type(failure).__name__), patch.object(subprocess, "run", side_effect=failure):
                screen = settle._screen(owner)  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(screen, settle.ScreenUnreadable("capture failed"))
                self.assertFalse(settle._held_by_usage_limit(screen))  # pyright: ignore[reportPrivateUsage]
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "hit your weekly limit", "")):
            self.assertEqual(settle._screen(owner), settle.ScreenUnreadable("capture failed"))  # pyright: ignore[reportPrivateUsage]
        owner["host"] = inventory.WindowHost(kind="ghostty", window_shell=123,
            desktop=inventory.DesktopNotInSnapshot(kind="not in snapshot"))
        with patch.object(subprocess, "run") as capture:
            self.assertEqual(settle._screen(owner), settle.ScreenUnreadable("no tmux host"))  # pyright: ignore[reportPrivateUsage]
            self.assertFalse(settle._usage_limit_shown(owner))  # pyright: ignore[reportPrivateUsage]
        capture.assert_not_called()

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

    def test_held_next_step_uses_idle_seats_and_readiness_without_the_limit_form(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"), progress=record.SessionHeldByUsageLimit(kind="held by usage limit", at=NOW))
        with patch.object(settle, "_showing_form", return_value=True) as form:
            self.assertIsInstance(settle.next_step(saved, self.matching(saved), [], NOW_UTC), settle.StopSession)
            saved["seats"] = [seat("worker", "Worker", owner="owner", status="busy")]
            self.assertEqual(settle.next_step(saved, self.matching(saved), [], NOW_UTC), settle.Hold("Codex seat Worker is still running"))
            saved["seats"] = []
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
        held["seats"] = [seat("held-seat", "Held seat", owner="held")]
        ordinary["seats"] = [seat("ordinary-seat", "Ordinary seat", owner="ordinary")]
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
        report = machine_inventory([held["session"], ordinary["session"], *held["seats"], *ordinary["seats"]])
        with patch.object(settle, "run_inventory", return_value=report) as observe, patch.object(settle, "_screen", side_effect=screen), patch.object(stop_work, "stop_session", side_effect=stop), patch.object(settle, "send_message") as sender, patch.object(settle, "report_stop"):
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
                owner = top_level("owner-" + delivery["kind"], "Owner")
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
        saved["seats"] = [seat("worker", "Worker", owner="owner", status="busy")]
        self.save(saved)
        self.assertFalse(settle._done(saved))  # pyright: ignore[reportPrivateUsage]
        at = NOW_UTC + settle.NOT_ANSWERING_AFTER + timedelta(seconds=1)
        self.assertFalse(settle._reserve_holdout_alert(saved, at))  # pyright: ignore[reportPrivateUsage]
        def end_pass(_seconds: float) -> None:
            raise PassObserved()
        with patch.object(settle, "now_utc", return_value=at), patch.object(settle, "_screen", return_value=settle.ScreenText("wait for limit to reset")), patch.object(settle, "run_inventory", return_value=machine_inventory([saved["session"], *saved["seats"]])), patch.object(settle, "alert_user") as alert, patch.object(settle, "send_message") as sender, patch.object(time, "sleep", side_effect=end_pass), self.assertRaises(PassObserved):
            _ = settle.conduct(LOGIN)
        alert.assert_not_called()
        sender.assert_not_called()
        for progress in (record.SessionStopped(kind="stopped", at=NOW), record.SessionRestarted(kind="restarted", at=NOW)):
            saved["progress"] = progress
            self.assertTrue(settle._done(saved))  # pyright: ignore[reportPrivateUsage]

    def test_ready_accepts_owner_held_by_usage_limit(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"), progress=record.SessionHeldByUsageLimit(kind="held by usage limit", at=NOW))
        self.save(saved)
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "owner"}), patch.object(settle, "run_inventory", return_value=machine_inventory([saved["session"]])):
            self.assertEqual(settle.ready("limit reset"), 0)
        latest = record.find_session(LOGIN, "owner")
        assert latest is not None
        self.assertEqual(latest["progress"], {"kind": "ready", "at": NOW})

    def test_next_step_covers_request_delivery_stop_restore_and_terminal_states(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"))
        cases: list[tuple[record.SessionProgress, str, record.ShutdownContinuation, type[settle.Step]]] = [
            ({"kind": "waiting"}, "idle", "stop", settle.SendSettleMessage),
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

    def test_seat_becoming_idle_allows_the_owner_to_stop(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"), progress={"kind": "ready", "at": NOW})
        worker = seat("seat", "Seat", owner="owner", status="busy")
        observed = stop_work.fresh_session(saved["session"], machine_inventory([saved["session"], worker]))
        self.assertEqual(settle.next_step(saved, observed, [], NOW_UTC), settle.Hold("Codex seat Seat is still running"))
        worker["status"] = "idle"
        self.assertIsInstance(settle.next_step(saved, stop_work.fresh_session(saved["session"], machine_inventory([saved["session"], worker])), [], NOW_UTC), settle.StopSession)

    def test_showrunner_ready_does_not_wait_for_a_waiting_unit(self) -> None:
        runner = session_shutdown(showrunner("runner", "Runner"), progress={"kind": "ready", "at": NOW})
        director = session_shutdown(unit("unit", "Unit"))
        self.assertIsInstance(settle.next_step(runner, self.matching(runner), [runner, director], NOW_UTC), settle.StopSession)

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
        self.assertIsInstance(settle.next_step(saved, self.matching(saved), [], NOW_UTC), settle.SendSettleMessage)
        saved["settle_message"] = {"kind": "queued", "at": NOW, "reason": "no live session"}
        self.assertIsInstance(settle.next_step(saved, self.matching(saved), [], NOW_UTC + timedelta(seconds=299)), settle.Hold)
        self.assertIsInstance(settle.next_step(saved, self.matching(saved), [], NOW_UTC + timedelta(seconds=300)), settle.SendSettleMessage)

    def test_moved_ghostty_showrunner_records_its_existing_desktop_without_old_window_tracking(self) -> None:
        owner = showrunner("hana-id", "hana")
        owner["host"] = inventory.WindowHost(kind="ghostty", window_shell=123,
            desktop=inventory.NamedDesktop(kind="named", name="Production"))
        with (
            patch.object(settle, "run_inventory", return_value=machine_inventory([owner])),
            patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "")) as windows_probe,
            patch.object(sys, "platform", "linux"),
            redirect_stdout(io.StringIO()),
        ):
            saved = settle.begin(LOGIN, only=["hana-id"], request_id="move", target="claude 3", then="restart")
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
                ["first", "second"], then="restart", target="claude 3"), 0)
        saved = record.session_records(LOGIN)
        self.assertEqual(len(saved), 2)
        self.assertTrue(all(item.get("target") == "claude 3" for item in saved))

    def test_down_to_persists_source_label_and_explicit_reason_beside_each_new_target(self) -> None:
        with (
            patch.object(settle, "run_inventory", return_value=machine_inventory([top_level("owner", "Owner")])),
            redirect_stdout(io.StringIO()),
        ):
            saved = settle.begin(LOGIN, only=["owner"], request_id="move", then="restart",
                target="claude 3", why="weekly limit reached")
        self.assertEqual(saved[0].get("source_label"), LABEL)
        self.assertEqual(saved[0].get("why"), "weekly limit reached")
        self.assertEqual(record.session_records(LOGIN)[0].get("source_label"), LABEL)
        self.assertEqual(record.session_records(LOGIN)[0].get("why"), "weekly limit reached")

    def test_down_to_defaults_its_saved_reason_to_source_label_out_of_usage(self) -> None:
        with (
            patch.object(settle, "run_inventory", return_value=machine_inventory([top_level("owner", "Owner")])),
            redirect_stdout(io.StringIO()),
        ):
            saved = settle.begin(LOGIN, only=["owner"], request_id="move", then="restart", target="default")
        self.assertEqual(saved[0].get("why"), f"{LABEL} is out of usage")
        self.assertEqual(saved[0].get("source_label"), LABEL)

    def test_down_without_to_records_no_move_target(self) -> None:
        with patch.object(settle, "run_inventory", return_value=machine_inventory([top_level("owner", "Owner")])):
            added = settle.begin(LOGIN, only=["owner"], request_id="ordinary", then="restart")
        self.assertEqual(len(added), 1)
        self.assertNotIn("target", added[0])
        self.assertNotIn("target", record.session_records(LOGIN)[0])

    def test_down_to_never_changes_an_existing_record_or_its_request_fields(self) -> None:
        original = session_shutdown(top_level("owner", "Owner"), requester="requester",
            request_id="first-request", then="stop")
        original["alerts"] = "none"
        self.save(original)
        before = {str(path): path.read_bytes() for path in (self.root / "state").rglob("*.json")}
        with (
            patch.object(settle, "_warm", return_value=True),
            patch.object(settle, "inventory", return_value=machine_inventory([original["session"]])),
            patch.object(settle, "run_inventory", return_value=machine_inventory([original["session"]])),
            patch.object(settle, "_launch_conductor"),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(settle.down(Account("claude", LOGIN, LABEL), True, ["owner"],
                then="restart", target="claude 3", why="different reason", requested_by="another", request_id="second-request"), 0)
        self.assertEqual(record.find_session(LOGIN, "owner"), original)
        self.assertEqual({str(path): path.read_bytes() for path in (self.root / "state").rglob("*.json")}, before)

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
                then="restart", target="default", why="weekly limit reached", requested_by="requester", request_id="move-request"), 0)
        arguments = cast(list[str], peer.call_args.args[0])
        self.assertEqual(arguments[:3], ["down", LOGIN, "--here"])
        self.assertEqual(arguments[arguments.index("--to") + 1], "default")
        self.assertEqual(arguments[arguments.index("--why") + 1], "weekly limit reached")
        self.assertEqual(arguments[arguments.index("--only") + 1], "remote")
        self.assertEqual(arguments[arguments.index("--requested-by") + 1], "requester")
        self.assertIn("--restart", arguments)

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
                then="restart", target="claude 3"), 0)
        self.assertEqual(record.session_records(LOGIN)[0]["requested_by"],
            record.FromSession(kind="session", session_id="self-id"))

    def test_begin_attaches_owned_seats_and_second_request_adds_only_new_owner(self) -> None:
        first, second = top_level("first", "First"), top_level("second", "Second")
        worker = seat("worker", "Worker", owner="first")
        orphan = seat("orphan", "Orphan", owner=None)
        with patch.object(settle, "run_inventory", return_value=machine_inventory([first, worker, orphan])):
            added = settle.begin(LOGIN, only=["first"], request_id="one")
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["seats"], [worker])
        original = record.find_session(LOGIN, "first")
        with patch.object(settle, "run_inventory", return_value=machine_inventory([first, second])):
            added = settle.begin(LOGIN, only=["first", "second"], request_id="two", then="restart")
        self.assertEqual([saved["session"]["session_id"] for saved in added], ["second"])
        self.assertEqual(record.find_session(LOGIN, "first"), original)
        self.assertIsNone(record.find_session(LOGIN, "orphan"))

    def test_begin_timer_stop_failure_takes_only_that_owner_out(self) -> None:
        first, second = top_level("first", "First", timers=["timer-first"]), top_level("second", "Second")
        def snapshot(owner: inventory.Session) -> list[record.TimerRestore]:
            return [record.TimerRestore(instance="timer-first", was_enabled=True, footer={"kind": "no footer"})] if owner["session_id"] == "first" else []
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
        with (
            patch.object(settle, "_warm", return_value=True),
            patch.object(settle, "run_inventory", return_value=machine_inventory([saved["session"]])),
            patch.object(settle, "_launch_conductor") as launch,
        ):
            self.assertEqual(settle.down(Account("claude", LOGIN, LABEL), True, ["owner"], then="restart"), 0)
        launch.assert_called_once_with(LOGIN, LABEL)
        self.assertEqual(record.find_session(LOGIN, "owner"), before)

    def test_cancel_keeps_stopped_and_stopping_records_and_their_timers(self) -> None:
        stopped = session_shutdown(top_level("stopped", "Stopped"), progress={"kind": "stopped", "at": NOW})
        stopping = session_shutdown(top_level("stopping", "Stopping"), progress={"kind": "stopping", "at": NOW})
        waiting = session_shutdown(top_level("waiting", "Waiting"))
        for saved in (stopped, stopping, waiting):
            saved["timers"] = [record.TimerRestore(instance=saved["session"]["session_id"], was_enabled=True, footer={"kind": "no footer"})]
            self.save(saved)
        with patch.object(settle, "run_notifier") as notifier, patch.object(settle, "_retire_shutdown_messages"):
            self.assertEqual(settle.cancel(Account("claude", LOGIN, LABEL), True), 1)
        notifier.assert_called_once_with("start", "waiting")
        self.assertEqual(record.find_session(LOGIN, "stopped"), stopped)
        self.assertEqual(record.find_session(LOGIN, "stopping"), stopping)
        self.assertIsNone(record.find_session(LOGIN, "waiting"))

    def test_cancel_after_stop_claim_leaves_paused_worker_and_timers_intact(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"), progress={"kind": "ready", "at": NOW})
        saved["timers"] = [record.TimerRestore(instance="timer", was_enabled=True, footer={"kind": "no footer"})]
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
                    clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0, tmux=lambda _args: 0, sleep=lambda _seconds: None)
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
        with patch.object(stop_work, "stop_session", side_effect=stop), patch.object(settle, "_launch_conductor", side_effect=self.run_conductor), patch.object(settle, "run_inventory", return_value=machine_inventory(owners)), patch.object(settle, "report_stop", side_effect=report):
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

    def fake_confirm(self, saved: record.SessionShutdown, _at: str, live: bool) -> None:
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
            patch.object(stop_work, "stop_session", side_effect=self.fake_stop),
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
        def confirm(saved: record.SessionShutdown, at: str, live: bool) -> None:
            if saved["session"]["session_id"] == "second":
                raise OSError("footer restore failed")
            self.fake_confirm(saved, at, live)
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
        self.assertEqual(failed["progress"], {"kind": "restart failed", "at": NOW, "reason": "footer restore failed"})
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
        def confirm(saved: record.SessionShutdown, at: str, live: bool) -> None:
            if saved["session"]["session_id"] == "missing":
                record.archive_session(LOGIN, "missing")
                raise record.NoLiveRecord("gone")
            self.fake_confirm(saved, at, live)
        with (
            patch.object(restart_work, "wait_for_confirmation", side_effect=self.all_live),
            patch.object(restart_work, "confirm_entry", side_effect=confirm),
            patch.object(restart_work, "wait_for_remote_control", return_value=frozenset({"present"})) as control,
        ):
            completed = settle._confirm_relaunch_pass(batch)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(completed.no_remote_control, ())
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
            saved["target"] = "claude 3"
            self.save(saved)
            requested: moves.RequestedMove = {"kind": "requested", "at": NOW, "tool": "claude",
                "subject": name + ": " + name, "move_id": saved["request_id"], "from": LABEL, "to": "claude 3",
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
        def confirm(saved: record.SessionShutdown, _at: str, live: bool) -> None:
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

    def test_running_stop_does_not_prevent_a_new_inventory_or_another_ready_owner(self) -> None:
        slow = session_shutdown(top_level("slow", "Slow"), progress={"kind": "ready", "at": NOW})
        fast = session_shutdown(top_level("fast", "Fast"))
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
            patch.object(stop_work, "stop_session", side_effect=stop),
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
        self.save(slow)
        self.save(fast)
        registry_entered, release, fast_confirmed = threading.Event(), threading.Event(), threading.Event()
        elapsed = [0.0]
        advanced = [False]
        def registry(ids: frozenset[str], _timeout: float) -> restart_work.RegistryConfirmation:
            if ids == frozenset({"slow"}):
                registry_entered.set()
                self.assertTrue(release.wait(1))
            else:
                self.assertEqual(ids, frozenset({"fast"}))
            return registry_confirmation(ids)
        def confirm(saved: record.SessionShutdown, at: str, live: bool) -> None:
            if saved["session"]["session_id"] == "fast":
                self.assertFalse(release.is_set())
                fast_confirmed.set()
                release.set()
            self.fake_confirm(saved, at, live)
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
            patch.object(stop_work, "stop_session", side_effect=self.fake_stop),
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
        with (
            patch.object(settle, "run_inventory", return_value=machine_inventory(owners)),
            patch.object(restart_work, "relaunch_session", side_effect=launch),
            patch.object(restart_work, "wait_for_confirmation", side_effect=self.all_live),
            patch.object(restart_work, "confirm_entry", side_effect=self.fake_confirm),
            patch.object(settle, "report_stop"),
        ):
            self.assertEqual(settle.conduct(LOGIN), 0)
        self.assertEqual(launches, ["a", "b", "c"])
        self.assertTrue(all(state is states[0] for state in states))

    def test_cancel_restores_enabled_timers_footers_and_retires_queued_messages(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"))
        saved["settle_message"] = {"kind": "queued", "at": NOW, "reason": "busy"}
        saved["timers"] = [record.TimerRestore(instance="enabled", was_enabled=True, footer={"kind": "footer", "slug": "production"}),
                           record.TimerRestore(instance="off", was_enabled=False, footer={"kind": "no footer"})]
        self.save(saved)
        with patch.object(settle, "run_notifier") as timers, patch.object(settle, "_restore_footer") as footer, patch.object(settle, "_retire_shutdown_messages") as retire, patch.object(settle, "send_message", return_value={"kind": "sent"}) as send:
            self.assertEqual(settle.cancel(Account("claude", LOGIN, LABEL), True), 0)
        timers.assert_called_once_with("start", "enabled")
        footer.assert_called_once_with("production")
        retire.assert_called_once_with(LOGIN, "owner")
        self.assertIn("cancelled by the user: continue where you were", cast(str, send.call_args.args[2]))
        self.assertIsNone(record.find_session(LOGIN, "owner"))

    def test_ready_preserves_the_recorded_process_identity_and_where_contract(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"))
        self.save(saved)
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "owner"}), patch.object(settle, "run_inventory", return_value=machine_inventory([saved["session"]])):
            self.assertEqual(settle.ready("next test"), 0)
        changed = record.find_session(LOGIN, "owner")
        self.assertIsNotNone(changed)
        if changed is not None:
            self.assertEqual(changed["session"], saved["session"])
            self.assertEqual(changed["where"], {"kind": "said", "at": NOW, "text": "next test"})
            self.assertEqual(changed["progress"], {"kind": "ready", "at": NOW})

    def test_stop_only_account_with_no_owner_keeps_its_hold_until_restore(self) -> None:
        hold = record.AccountHold(login=LOGIN, label=LABEL, machine="natedev", requested_at=NOW,
                                  requested_by={"kind": "terminal"}, request_id="empty", then="stop")
        record.set_account_hold(hold)
        with patch.object(settle, "report_stop"):
            self.assertEqual(settle.conduct(LOGIN), 0)
        self.assertEqual(record.account_hold(LOGIN), hold)

    def test_one_request_finishing_does_not_clear_a_different_requests_hold(self) -> None:
        done = session_shutdown(top_level("done", "Done"), progress={"kind": "restarted", "at": NOW}, request_id="one")
        stopped = session_shutdown(top_level("stopped", "Stopped"), progress={"kind": "stopped", "at": NOW}, request_id="two")
        for saved in (done, stopped):
            self.save(saved)
        hold = record.AccountHold(login=LOGIN, label=LABEL, machine="natedev", requested_at=NOW,
                                  requested_by={"kind": "terminal"}, request_id="two", then="stop")
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
            patch.object(stop_work, "stop_session", side_effect=stop),
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

    def test_report_stop_lists_peer_waiting_and_alerts_only_requested_failures(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"), progress={"kind": "stop failed", "at": NOW, "reason": "busy"}, requester="requester")
        saved["hold"] = "account"
        peer = session_shutdown(top_level("peer", "Peer"))
        peer["machine"] = "Mac"
        with patch.object(settle, "run_remote", return_value=(0, json.dumps([peer]))), patch.object(settle, "send_message", return_value={"kind": "sent"}) as send, patch.object(settle, "alert_user") as alert:
            settle.report_stop([saved])
        self.assertIn("Mac Peer: waiting", cast(str, send.call_args.args[2]))
        self.assertEqual(send.call_args.kwargs["key"], f"shutdown-result-{LOGIN}")
        alert.assert_called_once()
        saved["alerts"] = "none"
        with patch.object(settle, "run_remote", return_value=(255, "")), patch.object(settle, "send_message", return_value={"kind": "sent"}), patch.object(settle, "alert_user") as alert:
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
        with patch.object(record, "session_records", side_effect=snapshot), patch.object(settle, "run_inventory", return_value=machine_inventory([saved["session"]])), patch.object(stop_work, "stop_session", side_effect=self.fake_stop) as stop, patch.object(settle, "report_stop"):
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
                with patch.object(record, "conductor_lock", side_effect=handoff), patch.object(record, "session_records", side_effect=snapshot), patch.object(settle, "run_inventory", return_value=machine_inventory([saved["session"]])), patch.object(stop_work, "stop_session", side_effect=self.fake_stop) as stop, patch.object(settle, "report_stop"):
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
            self.assertTrue(line.endswith("; /shutdown restart brings it back"))
        self.assertIn("restart failed — not live", output.getvalue())
        self.assertIn("manual restart — run: resume owner", output.getvalue())

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
        def cancelled(_login: str, _only: object = ()) -> inventory.Inventory:
            self.assertEqual(settle.cancel(Account("claude", LOGIN, LABEL), True), 0)
            return machine_inventory([])
        with patch.object(settle, "run_inventory", side_effect=cancelled), patch.object(settle, "_retire_shutdown_messages"), patch.object(stop_work, "stop_session", side_effect=record.NoLiveRecord("cancelled")), patch.object(settle, "report_stop") as report:
            self.assertEqual(settle.conduct(LOGIN), 0)
        self.assertEqual(report.call_args.args[0], [])

    def test_conductor_drops_records_archived_by_another_restore_caller(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"), progress={"kind": "stopped", "at": NOW}, then="restart")
        self.save(saved)
        def resumed(_login: str, _only: object = ()) -> inventory.Inventory:
            def complete(current: record.SessionShutdown) -> None:
                current["progress"] = {"kind": "restarted", "at": NOW}
            _ = record.update_session(LOGIN, "owner", complete)
            record.archive_session(LOGIN, "owner")
            return machine_inventory([])
        with patch.object(settle, "run_inventory", side_effect=resumed), patch.object(restart_work, "relaunch_session", side_effect=record.NoLiveRecord("finished elsewhere")), patch.object(settle, "report_stop") as report:
            self.assertEqual(settle.conduct(LOGIN), 0)
        self.assertEqual(report.call_args.args[0], [])

    def test_shutdown_pushes_name_the_session_cause_and_recovery_effect(self) -> None:
        """F015: failed shutdown and restore pushes carry their recorded cause."""
        cases: list[record.SessionProgress] = [
            {"kind": "stop failed", "at": NOW, "reason": "process did not exit"},
            {"kind": "restart failed", "at": NOW, "reason": "window failed to open"},
        ]
        for progress in cases:
            with self.subTest(kind=progress["kind"]):
                saved = session_shutdown(top_level("owner", "Owner"), progress=progress)
                saved["hold"] = "account"
                with (patch.object(settle, "run_remote", return_value=(0, "[]")),
                      patch.object(settle, "send_message"), patch.object(settle, "alert_user") as alert):
                    settle.report_stop([saved])
                title, body, action = cast(tuple[str, str, ActionRequired], alert.call_args.args)
                self.assertIn("session named Owner", body)
                self.assertIn("Claude account", body)
                self.assertIn("process did not exit" if progress["kind"] == "stop failed" else "window failed to open", body)
                self.assertIn("tries to", body)
                self.assertIn("natedev", action.text)
                self.assertLess(len(title), 45)

    def test_single_holdout_names_the_terminal_where_its_form_can_be_read(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"))
        with patch.object(settle, "send_message"), patch.object(settle, "alert_user") as alert:
            settle._send_holdout_alert([(saved, "showing a form")])  # pyright: ignore[reportPrivateUsage]
        body = cast(str, alert.call_args.args[1])
        action = cast(ActionRequired, alert.call_args.args[2])
        expected = (
            'Shutdown of Claude account "claude 2" is waiting for sessions to stop. '
            "Its session named Owner on natedev is showing a form, so it has not stopped."
            " Read any open form in that session's terminal before choosing.\n\n"
            'Cancel (recommended): /shutdown cancel "claude 2" --only Owner. This keeps it running so you can check its work.\n\n'
            'Stop now: /shutdown now "claude 2" --only Owner. This stops it without waiting.'
        )
        self.assertEqual(body, expected)
        self.assertEqual(action.text, "In the natedev Claude session, choose cancel or stop now.")
        posted = "Action: " + action.text + "\n" + body
        self.assertIn('Shutdown of Claude account "claude 2" is waiting for sessions to stop.', posted[:150])
        self.assertLess(len(posted), 500)
        self.assertLess(len(cast(str, alert.call_args.args[0])), 45)
        self.assertIn("session named Owner on natedev", body)
        self.assertIn("Read any open form in that session's terminal before choosing.", body)
        self.assertIn("keeps it running so you can check its work", body)
        self.assertIn("stops it without waiting", body)

    def test_grouped_holdout_notice_keeps_whole_sentences_and_counts_omitted_sessions(self) -> None:
        for names, included in ((("abcdefgh", "ijklmnop", "qrstuvwx"), 1),
                                (("CoordinatorSessionOne", "CoordinatorSessionTwo", "CoordinatorSessionThree"), 0),
                                (("A" * 80, "B" * 80, "C" * 80), 0)):
            with self.subTest(names=names):
                due = [(session_shutdown(top_level(str(index), name)), "showing a form")
                       for index, name in enumerate(names)]
                with patch.object(settle, "send_message"), patch.object(settle, "alert_user") as alert:
                    settle._send_holdout_alert(due)  # pyright: ignore[reportPrivateUsage]
                body = cast(str, alert.call_args.args[1])
                action = cast(ActionRequired, alert.call_args.args[2])
                opening = 'Shutdown of Claude account "claude 2" is waiting for sessions to stop. '
                sentences = [f"Its session named {name} on natedev is showing a form, so it has not stopped."
                             for name in names[:included]]
                expected = opening + " ".join([*sentences, f"{3 - included} more sessions are waiting."])
                if len(names[0]) == 80:
                    expected = opening + "3 sessions are waiting. One is showing a form, so it has not stopped."
                self.assertEqual(body.split("\n\n")[0], expected)
                posted = "Action: " + action.text + "\n" + body
                self.assertIn(opening.strip(), posted[:150])
                self.assertLess(len(posted), 500)
                self.assertLess(len(cast(str, alert.call_args.args[0])), 45)

    def test_holdout_notice_with_names_that_do_not_fit_offers_account_commands(self) -> None:
        due = [(session_shutdown(top_level(str(index), name * 80)), "showing a form")
               for index, name in enumerate(("A", "B", "C"))]
        with patch.object(settle, "send_message"), patch.object(settle, "alert_user") as alert:
            settle._send_holdout_alert(due)  # pyright: ignore[reportPrivateUsage]
        body = cast(str, alert.call_args.args[1])
        action = cast(ActionRequired, alert.call_args.args[2])
        expected = (
            'Shutdown of Claude account "claude 2" is waiting for sessions to stop. '
            '3 sessions are waiting. One is showing a form, so it has not stopped.\n\n'
            'Cancel (recommended): /shutdown cancel "claude 2". This keeps every waiting session running so you can check their work.\n\n'
            'Stop now: /shutdown now "claude 2". This stops every waiting session without waiting for its work to finish.'
        )
        self.assertEqual(body, expected)
        self.assertEqual(action.text, "In the natedev Claude session, choose cancel or stop now.")
        posted = "Action: " + action.text + "\n" + body
        self.assertEqual(len(posted), 438)
        self.assertIn('Shutdown of Claude account "claude 2" is waiting for sessions to stop.', posted[:150])
        self.assertLess(len(posted), 500)
        self.assertLess(len(cast(str, alert.call_args.args[0])), 45)

    def test_unit_session_issue_text_identifies_the_host_name_and_cause(self) -> None:
        for host in ("tmux", "herdr"):
            for cause in ("stop not confirmed", "owner identity lost"):
                with self.subTest(host=host, cause=cause):
                    issue = record.UnitSessionLeftRunning(kind="unit session left running", at=NOW,
                        host=host, session_name="named-unit", cause=cause)
                    self.assertEqual(settle.stop_issue_text(issue),
                        f"{host} session named-unit left running: {cause}")

    def test_report_stop_treats_owner_resource_issues_as_a_partial_stop(self) -> None:
        for kind in ("stopped", "already gone", "process identity lost"):
            for alerts in ("user", "none"):
                saved = session_shutdown(top_level("owner", "Owner"), progress=cast(record.SessionProgress, cast(object, {"kind": kind, "at": NOW})), requester="requester")
                saved["hold"], saved["alerts"] = "account", alerts
                saved["stop_issues"] = [record.UnitSessionLeftRunning(kind="unit session left running", at=NOW, host="tmux", session_name="unit", cause="stop not confirmed")]
                with patch.object(settle, "run_remote", return_value=(0, "[]")), patch.object(settle, "send_message", return_value={"kind": "sent"}) as send, patch.object(settle, "alert_user") as alert:
                    settle.report_stop([saved])
                self.assertEqual(send.call_args.args[1], f"{LABEL}: stop partial")
                self.assertIn("/shutdown now retries the failed stop", cast(str, send.call_args.args[2]))
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
        with patch.object(settle, "_launch_conductor", side_effect=self.run_conductor), patch.object(settle, "run_inventory", return_value=machine_inventory([saved["session"]])), patch.object(stop_work, "stop_session", side_effect=cleanup) as stop, patch.object(settle, "report_stop"):
            self.assertEqual(settle.now(Account("claude", LOGIN, LABEL), True), 0)
        stop.assert_called_once()
        current = record.find_session(LOGIN, "owner")
        self.assertIsNotNone(current)
        if current is not None:
            self.assertEqual(current["stop_issues"], [])

    def test_now_reports_a_persistent_cleanup_failure_after_one_owned_retry(self) -> None:
        saved = session_shutdown(top_level("owner", "Owner"), progress={"kind": "already gone", "at": NOW})
        saved["stop_issues"] = [record.UnitSessionLeftRunning(kind="unit session left running", at=NOW, host="tmux", session_name="unit", cause="stop not confirmed")]
        self.save(saved)
        def failed_cleanup(current: record.SessionShutdown) -> record.SessionShutdown:
            return current
        with patch.object(settle, "_launch_conductor", side_effect=self.run_conductor), patch.object(settle, "run_inventory", return_value=machine_inventory([])), patch.object(stop_work, "stop_session", side_effect=failed_cleanup) as stop, patch.object(settle, "report_stop") as report:
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
        saved["timers"] = [record.TimerRestore(instance="timer", was_enabled=True, footer={"kind": "no footer"})]
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

    def test_now_only_failed_stop_marks_force_and_delegates_retry_to_conductor(self) -> None:
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
        saved["timers"] = [record.TimerRestore(instance="timer", was_enabled=True, footer={"kind": "no footer"})]
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
        self.assertEqual(output.getvalue(), "Failed was not cancelled: stopping; /shutdown restart --only Failed brings it back\n")
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
        saved = session_shutdown(top_level("selected", "Selected"))
        saved["request_id"] = "selected-request"
        self.save(saved)
        held = record.AccountHold(login=LOGIN, label=LABEL, machine="natedev", requested_at=NOW,
            requested_by={"kind": "terminal"}, request_id="held-request", then="stop")
        record.set_account_hold(held)
        with patch.object(settle, "_retire_shutdown_messages"), redirect_stdout(io.StringIO()):
            self.assertEqual(settle.cancel(Account("claude", LOGIN, LABEL), True, only=["selected"]), 0)
        self.assertIsNone(record.find_session(LOGIN, "selected"))
        self.assertEqual(record.account_hold(LOGIN), held)

    def test_selected_now_prints_peer_refusal_as_answer_without_not_reached_warning(self) -> None:
        line = "Peer is stopping; nothing to stop"
        output, error = io.StringIO(), io.StringIO()
        with patch.object(settle, "inventory", return_value=machine_inventory([])), patch.object(settle, "run_remote", return_value=(1, line)) as peer, redirect_stdout(output), redirect_stderr(error):
            self.assertEqual(settle.now(Account("claude", LOGIN, LABEL), only=["peer-id"]), 1)
        self.assertEqual((output.getvalue(), error.getvalue()), (line + "\n", ""))
        peer.assert_called_once_with(["now", LOGIN, "--here", "--only", "peer-id"])
    def test_holdout_alert_uses_delivery_time_or_request_time_and_repeats_hourly(self) -> None:
        deliveries: list[record.SettleMessage] = [{"kind": "not sent"}, {"kind": "queued", "at": NOW, "reason": "pending"}, {"kind": "sent", "at": (NOW_UTC + timedelta(minutes=2)).isoformat()}]
        for delivery in deliveries:
            saved = session_shutdown(top_level("owner", "Owner"))
            saved["settle_message"] = delivery
            baseline = NOW_UTC + (timedelta(minutes=2) if delivery["kind"] == "sent" else timedelta())
            self.assertFalse(settle._holdout_due(saved, baseline + timedelta(minutes=5) - timedelta(seconds=1)))  # pyright: ignore[reportPrivateUsage]
            self.assertTrue(settle._holdout_due(saved, baseline + timedelta(minutes=5)))  # pyright: ignore[reportPrivateUsage]
            saved["alerted_at"] = (baseline + timedelta(minutes=5)).isoformat()
            self.assertFalse(settle._holdout_due(saved, baseline + timedelta(minutes=65) - timedelta(seconds=1)))  # pyright: ignore[reportPrivateUsage]
            self.assertTrue(settle._holdout_due(saved, baseline + timedelta(minutes=65)))  # pyright: ignore[reportPrivateUsage]
            saved["alerts"] = "none"
            self.assertFalse(settle._holdout_due(saved, baseline + timedelta(minutes=65)))  # pyright: ignore[reportPrivateUsage]

    def test_holdout_alert_groups_form_and_waiting_requester_while_two_sessions_return(self) -> None:
        owners: list[inventory.Session] = [top_level(name, name) for name in ("form", "first", "second", "requester")]
        for owner in owners:
            saved = session_shutdown(owner, requester="requester", then="restart",
                progress={"kind": "waiting"} if owner["session_id"] == "requester" else {"kind": "ready", "at": NOW})
            self.save(saved)
        observed = threading.Event()
        def phone(_summary: str, _text: str, _action: object, *, machine: str = "") -> None:
            del machine
            observed.set()
        def end(_seconds: float) -> None:
            if observed.is_set() and record.find_session(LOGIN, "first") is None and record.find_session(LOGIN, "second") is None:
                raise PassObserved()
        at = NOW_UTC + timedelta(minutes=5)
        def form(owner: inventory.Session) -> bool:
            return owner["session_id"] == "form"
        with patch.object(settle, "now_utc", return_value=at), patch.object(settle, "run_inventory", return_value=machine_inventory(owners)), patch.object(settle, "SETTLE_INTERVAL_SECONDS", 0.001), patch.object(settle, "_showing_form", side_effect=form), patch.object(stop_work, "stop_session", side_effect=self.fake_stop), patch.object(restart_work, "relaunch_session", side_effect=self.fake_launch), patch.object(restart_work, "wait_for_confirmation", side_effect=self.all_live), patch.object(restart_work, "confirm_entry", side_effect=self.fake_confirm), patch.object(settle, "alert_user", side_effect=phone) as alert, patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")) as sender, patch.object(time, "sleep", side_effect=end), self.assertRaises(PassObserved):
            _ = settle.conduct(LOGIN)
        alert.assert_called_once()
        summary = cast(str, cast(object, alert.call_args.args[0]))
        text = cast(str, cast(object, alert.call_args.args[1]))
        action = cast(ActionRequired, cast(object, alert.call_args.args[2]))
        self.assertEqual(summary, "Shutdown waits on a session showing a form")
        self.assertIn("showing a form", text)
        self.assertIn("choose cancel or stop now", action.text)
        self.assertIn("natedev", action.text)
        self.assertIn("recommended", text)
        self.assertIn("--only form,requester", text)
        self.assertLess(len("Action: " + action.text + "\n" + text), 500)
        sender.assert_called_once()
        self.assertIn("/shutdown cancel --only form", cast(str, sender.call_args.args[2]))
        self.assertIn("/shutdown now --only requester", cast(str, sender.call_args.args[2]))
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
                with patch.object(settle, "now_utc", return_value=NOW_UTC + timedelta(minutes=5)), patch.object(settle, "run_inventory", return_value=machine_inventory([owner, ready])), patch.object(settle, "SETTLE_INTERVAL_SECONDS", 0.001), patch.object(stop_work, "stop_session", side_effect=stop), patch.object(settle, "alert_user", side_effect=pause if paused == "phone" else None), patch.object(settle, "send_message", side_effect=pause if paused == "requester" else None, return_value=settle.MessageSent(kind="sent")), patch.object(time, "sleep", side_effect=end), self.assertRaises(PassObserved):
                    _ = settle.conduct(LOGIN)
                record.archive_session(LOGIN, owner["session_id"])
                record.archive_session(LOGIN, ready["session_id"])

    def test_remote_control_conductor_check_and_alert_leave_other_records_moving(self) -> None:
        for policy in ("user", "none"):
            with self.subTest(policy=policy):
                restored = session_shutdown(top_level("restored-" + policy, "Restored"), progress={"kind": "stopped", "at": NOW}, then="restart")
                restored["alerts"] = policy
                other = session_shutdown(top_level("other-" + policy, "Other"), progress={"kind": "waiting"})
                other["alerts"] = "none"
                self.save(restored)
                self.save(other)
                checking, stopped = threading.Event(), threading.Event()
                main_thread = threading.get_ident()
                def check(_records: object, _timeout: float) -> frozenset[str]:
                    self.assertNotEqual(threading.get_ident(), main_thread)
                    checking.set()
                    self.assertTrue(stopped.wait(2))
                    return frozenset()
                def stop(saved: record.SessionShutdown) -> record.SessionShutdown:
                    result = self.fake_stop(saved)
                    stopped.set()
                    return result
                def observe(_login: str) -> inventory.Inventory:
                    if checking.is_set():
                        def ready(current: record.SessionShutdown) -> None:
                            if current["progress"]["kind"] == "waiting":
                                current["progress"] = {"kind": "ready", "at": NOW}
                        _ = record.update_session(LOGIN, other["session"]["session_id"], ready)
                    return machine_inventory([restored["session"], other["session"]])
                with patch.object(settle, "run_inventory", side_effect=observe), patch.object(settle, "SETTLE_INTERVAL_SECONDS", 0.001), patch.object(restart_work, "relaunch_session", side_effect=self.fake_launch), patch.object(restart_work, "wait_for_confirmation", side_effect=self.all_live), patch.object(restart_work, "confirm_entry", side_effect=self.fake_confirm), patch.object(restart_work, "wait_for_remote_control", side_effect=check), patch.object(stop_work, "stop_session", side_effect=stop), patch.object(settle, "_send_settle_message"), patch.object(settle, "alert_user") as alert, patch.object(settle, "report_stop"), redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(settle.conduct(LOGIN), 0)
                self.assertIn("Restored: running, no Remote Control", output.getvalue())
                self.assertIsNone(record.find_session(LOGIN, restored["session"]["session_id"]))
                self.assertEqual(alert.call_count, int(policy == "user"))
                if alert.called:
                    self.assertIn("/remote-control", cast(ActionRequired, cast(object, alert.call_args.args[2])).text)
                record.archive_session(LOGIN, other["session"]["session_id"])

    def test_user_alert_wait_covers_configured_review_and_remote_transport(self) -> None:
        self.allow_leaf(ShutdownSendLeaf.DELIVER_MESSAGE)
        config = self.root / "notify.conf"
        _ = config.write_text("REVIEW_TIMEOUT_SECONDS=90\n")
        with patch.object(review, "NOTIFY_CONFIG", config):
            for machine, expected in (("", 130), ("natedev", 170)):
                with (self.subTest(machine=machine),
                      patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as sender):
                    settle.alert_user("UPS power warning", "Battery low", ActionRequired("Check power"), machine=machine)
                self.assertEqual(sender.call_args.kwargs["timeout"], expected)
            with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as sender:
                self.assertEqual(settle.send_message("session:owner", "Shutdown", "Stop work"), {"kind": "sent"})
            self.assertEqual(sender.call_args.kwargs["timeout"], 60)

    def test_user_alert_timeout_reports_failed_delivery_without_interrupting_shutdown(self) -> None:
        self.allow_leaf(ShutdownSendLeaf.DELIVER_MESSAGE)
        with (patch.object(review, "user_send_seconds", return_value=115),
              patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("fake send", 130)),
              redirect_stderr(io.StringIO()) as errors):
            settle.alert_user("UPS power warning", "Battery low", ActionRequired("Check power"))
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
                settle.alert_user("UPS power warning", "Battery low", ActionRequired("Check power"))
            self.assertEqual(sender.call_args.kwargs["input"], "Battery low")
            self.assertEqual(sender.call_args.kwargs["timeout"], 100)
            self.assertIn("--to", cast(list[str], cast(object, sender.call_args.args[0])))

    def test_remote_control_paused_alert_send_does_not_delay_later_ready_session(self) -> None:
        restored = session_shutdown(top_level("restored", "Restored"), progress={"kind": "stopped", "at": NOW}, then="restart")
        other = session_shutdown(top_level("other", "Other"))
        self.save(restored)
        self.save(other)
        entered, stopped = threading.Event(), threading.Event()
        main_thread = threading.get_ident()
        def phone(*_args: object, **_kwargs: object) -> None:
            self.assertNotEqual(threading.get_ident(), main_thread)
            entered.set()
            self.assertTrue(stopped.wait(2))
        def observe(_login: str) -> inventory.Inventory:
            if entered.is_set():
                def ready(current: record.SessionShutdown) -> None:
                    if current["progress"]["kind"] == "waiting":
                        current["progress"] = {"kind": "ready", "at": NOW}
                _ = record.update_session(LOGIN, "other", ready)
            return machine_inventory([restored["session"], other["session"]])
        def stop(saved: record.SessionShutdown) -> record.SessionShutdown:
            result = self.fake_stop(saved)
            stopped.set()
            return result
        with patch.object(settle, "run_inventory", side_effect=observe), patch.object(settle, "SETTLE_INTERVAL_SECONDS", 0.001), patch.object(restart_work, "relaunch_session", side_effect=self.fake_launch), patch.object(restart_work, "wait_for_confirmation", side_effect=self.all_live), patch.object(restart_work, "confirm_entry", side_effect=self.fake_confirm), patch.object(restart_work, "wait_for_remote_control", return_value=frozenset()), patch.object(stop_work, "stop_session", side_effect=stop), patch.object(settle, "_send_settle_message"), patch.object(settle, "alert_user", side_effect=phone) as alert, patch.object(settle, "report_stop"), redirect_stdout(io.StringIO()):
            self.assertEqual(settle.conduct(LOGIN), 0)
        alert.assert_called_once()
        self.assertTrue(stopped.is_set())

    def test_holdout_alert_from_mac_routes_phone_to_natedev_and_same_text_to_requester(self) -> None:
        saved = session_shutdown(top_level("held", "Held"), requester="requester")
        saved["machine"] = "Mac"
        with patch.object(settle, "local_machine", return_value="Mac"), patch.object(settle, "alert_user") as alert, patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")) as sender:
            settle._send_holdout_alert([(saved, "busy")])  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(alert.call_args.kwargs, {"machine": "natedev"})
        self.assertIn("session named Held on Mac", cast(str, cast(object, alert.call_args.args[1])))
        self.assertIn("/shutdown cancel --only Held", cast(str, sender.call_args.args[2]))



if __name__ == "__main__":
    _ = unittest.main()
