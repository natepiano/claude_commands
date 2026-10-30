"""Tests for one session's stop claim, signals and resources."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from unit_staff_test_support import unit_host
import copy
import io
import json
import os
import signal
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from collections.abc import Callable
from typing import Literal, cast, final, override
from unittest.mock import call, patch

from test_support import linux_process_fixture, ShutdownTestCase, empty_session_registry
from scripts.whoami.account import Account
import inventory
import herdr
from fake_herdr import CommandResponse
import shutdown_record
import settle
import sessions
import stop
import relaunch
import unit_lookup

# Hang guard: natedev reached load 136 on 2026-10-29; scheduling delays are not failures.
HANG_GUARD_SECONDS = 300

LOGIN = "owner@example.com"
LABEL = "claude2"
NOW = "2026-10-09T21:49:10+00:00"
NOW_UTC = datetime.fromisoformat(NOW)

@final
class FreshInventorySequence:
    reports: list[inventory.Inventory]
    calls: int

    def __init__(self, reports: list[inventory.Inventory]) -> None:
        if not reports:
            raise ValueError("an inventory sequence needs at least one report")
        self.reports = reports
        self.calls = 0

    def __call__(
        self, _login: str, _only: frozenset[str]
    ) -> inventory.Inventory:
        index = min(self.calls, len(self.reports) - 1)
        self.calls += 1
        return self.reports[index]


def checkout() -> inventory.GitCheckout:
    return inventory.GitCheckout(
        kind="git",
        path="/tmp/checkout",
        head=inventory.OnBranch(kind="branch", name="work"),
        upstream=inventory.Tracking(kind="tracking", ahead=0),
        dirty=[],
    )


def common_session(
    session_id: str,
    name: str,
    pid: int,
    *,
    status: str = "idle",
    proc_start: str | None = None,
    run_dirs: list[str] | None = None,
    codex_servers: list[inventory.CodexServer] | None = None,
) -> inventory.SessionFields:
    return {
        "session_id": session_id,
        "pid": pid,
        "proc_start": proc_start or f"start-{session_id}",
        "probe": False,
        "name": name,
        "cwd": f"/tmp/{session_id}",
        "status": status,
        "updated_at": 0,
        "model": inventory.NoReplyYet(kind="no reply yet"),
        "checkout": checkout(),
        "run_dirs": list(run_dirs or []),
        "codex_servers": list(codex_servers or []),
        "timers": [],
    }


def top_level(
    session_id: str,
    pid: int,
    *,
    status: str = "idle",
    host: inventory.SessionHost | None = None,
    run_dirs: list[str] | None = None,
    codex_servers: list[inventory.CodexServer] | None = None,
) -> inventory.TopLevelSession:
    return inventory.TopLevelSession(
        **common_session(
            session_id,
            session_id,
            pid,
            status=status,
            run_dirs=run_dirs,
            codex_servers=codex_servers,
        ),
        kind="top-level",
        host=host or inventory.UnknownHost(kind="unknown"),
    )


def showrunner(
    session_id: str,
    pid: int,
    *,
    run_dirs: list[str] | None = None,
    codex_servers: list[inventory.CodexServer] | None = None,
) -> inventory.ShowrunnerSession:
    return inventory.ShowrunnerSession(
        **common_session(
            session_id,
            session_id,
            pid,
            run_dirs=run_dirs,
            codex_servers=codex_servers,
        ),
        kind="showrunner",
        host=inventory.HerdrHost(kind="herdr", herdr_session=f"herdr-{session_id}", pane_id="w1:p1", presentation="detached"),
        production="demo",
        doc="/tmp/demo.md",
    )


def unit(
    session_id: str,
    pid: int,
    *,
    session_name: str | None = None,
    session_host: Literal["herdr"] = "herdr",
    run_dirs: list[str] | None = None,
    codex_servers: list[inventory.CodexServer] | None = None,
) -> inventory.UnitSession:
    return inventory.UnitSession(
        **common_session(
            session_id,
            session_id,
            pid,
            run_dirs=run_dirs,
            codex_servers=codex_servers,
        ),
        kind="unit",
        host=unit_host(
            kind="unit",
            production="demo",
            unit=session_id,
            doc="/tmp/demo.md",
            session_name=session_name or f"herdr-{session_id}",
            session_host=session_host,
            plan=inventory.NoRunRecord(kind="no run record"),
        ),
    )


def worker_session(
    session_id: str,
    pid: int,
    *,
    owner: str | None,
) -> inventory.WorkerSession:
    worker_owner: inventory.WorkerOwner = (
        inventory.DirectorOwner(kind="director", session_id=owner)
        if owner is not None
        else inventory.NoDirector(kind="no director")
    )
    return inventory.WorkerSession(
        **common_session(session_id, session_id, pid),
        kind="worker",
        host=inventory.UnknownHost(kind="unknown"),
        owner=worker_owner,
    )


def server(
    run_dir: str, pid: int, *, busy_workers: list[str] | None = None
) -> inventory.CodexServer:
    return inventory.CodexServer(
        run_dir=run_dir, pid=pid, busy_workers=list(busy_workers or [])
    )


def entry(session: inventory.Session, progress: str = "ready") -> shutdown_record.SessionShutdown:
    state: shutdown_record.SessionProgress = (
        shutdown_record.SessionWaiting(kind="waiting") if progress == "waiting"
        else shutdown_record.SessionReadyToStop(kind="ready", at=NOW)
    )
    return shutdown_record.SessionShutdown(
        login=LOGIN, label=LABEL, machine="natedev", requested_at=NOW,
        request_id="request", requested_by=shutdown_record.FromTerminal(kind="terminal"),
        hold="session", then="stop", force="wait for ready", alerts="none", probe=False,
        session=session, workers=[], timers=[], settle_message=shutdown_record.SettleMessageNotSent(kind="not sent"),
        where=shutdown_record.WhereNotSaid(kind="not said"), progress=state, trail=[], stop_issues=[],
    )


def records_for_test(
    entries: list[shutdown_record.SessionShutdown], *, force: shutdown_record.StopTiming = "wait for ready", state: str = "",
) -> list[shutdown_record.SessionShutdown]:
    del state
    for item in entries:
        item["force"] = force
    return entries


def machine_inventory(
    sessions: list[inventory.Session], *, unattributed: list[inventory.UnattributedSession] | None = None,
) -> inventory.Inventory:
    return inventory.Inventory(
        machine="natedev", login=LOGIN, label=LABEL,
        sessions=sessions, unattributed=list(unattributed or []),
    )


@final
@final
class _HeldClaim:
    def check(self) -> None:
        pass

    check_effect = check


class SharedHerdrStopTests(ShutdownTestCase):
    def test_a_unit_in_the_shared_session_closes_only_the_tab_of_its_marked_pane(self) -> None:
        session = cast(inventory.Session, cast(object, {
            "kind": "unit", "name": "review", "session_id": "review-session",
            "host": unit_host(kind="unit", production="prod", unit="review-unit", doc="/prod.md",
                              session_name="agents", session_host="herdr", plan=inventory.NoRunRecord(kind="no run record"))}))
        claim = cast(stop.StopClaim, cast(object, _HeldClaim()))
        marked = unit_lookup.MarkedUnit("review-unit", unit_lookup.ClaudeNotRunning(), herdr.HerdrPlace("agents", "w3:pE"))
        cases: list[tuple[unit_lookup.MarkedUnit | None, list[herdr.PaneIdentity], int, int]] = [
            (marked, [], 1, 0),
            (marked, [{"pane_id": "w3:pE", "label": "prod-review", "workspace_id": "w3", "session_id": None}], 1, 1),
            (None, [], 0, 0),
        ]
        for found, panes, closes, issues in cases:
            with (self.subTest(found=found, panes=panes), patch.object(herdr, "shared_session", return_value="agents"),
                  patch.object(unit_lookup, "marked_unit", return_value=found) as lookup,
                  patch.object(herdr.Herdr, "close_tab_of") as closed,
                  patch.object(herdr.Herdr, "panes", return_value=panes)):
                result = stop._stop_unit_session(session, lambda: NOW_UTC, claim)  # pyright: ignore[reportPrivateUsage]
            lookup.assert_called_once_with("prod", "review-unit")
            self.assertEqual(closed.call_count, closes)
            if closes:
                closed.assert_called_once_with("w3:pE")
            self.assertEqual(len(result), issues)


class StopTests(ShutdownTestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        linux_process_fixture(self)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        environment = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.root), "SHUTDOWN_STATE_DIR": str(self.root / "shutdown"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "HERDR_BIN": str(Path(__file__).parents[1] / "herdr/fake_herdr.py"),
            "SYSTEMCTL": str(Path(__file__).parents[1] / "herdr/fake_herdr.py"),
            "HERDR_PROC": str(self.root / "proc"),
            "FAKE_HERDR_LOG": str(self.root / "herdr.jsonl"),
            "FAKE_HERDR_RESPONSES": str(self.root / "herdr-responses.json"),
        })))
        del environment
        empty_session_registry(self, self.root / "sessions")
        self.herdr_replies([])
        _ = self.enterContext(patch.object(settle, "record_time", return_value=NOW))

    def create(self, records: list[shutdown_record.SessionShutdown]) -> None:
        for item in records:
            self.assertTrue(shutdown_record.create_session(item))

    def stored_record(self) -> shutdown_record.SessionShutdown:
        return shutdown_record.session_records(LOGIN)[0]

    def perform(self, saved: shutdown_record.SessionShutdown) -> shutdown_record.SessionShutdown:
        return stop.stop_session(
            saved, kill=lambda _pid, _signal: None, is_alive=lambda _pid: False,
            session_record_exists=lambda _pid: False,
            fresh_inventory=lambda _login, _only: machine_inventory([saved['session'], *saved['workers']]),
            clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0,

            sleep=lambda _seconds: None,
        )

    def test_busy_fresh_owner_defers_to_prior_readiness_instead_of_failing(self) -> None:
        priors: tuple[shutdown_record.SessionProgress, ...] = (
            {"kind": "waiting"}, {"kind": "ready", "at": NOW},
            {"kind": "held by usage limit", "at": NOW},
            {"kind": "stopping", "at": (NOW_UTC - timedelta(seconds=120)).isoformat()},
        )
        for index, prior in enumerate(priors):
            with self.subTest(prior=prior["kind"]):
                owner = top_level(f"deferred-{index}", 101 + index)
                saved = entry(owner)
                saved["progress"] = prior
                self.create([saved])
                fresh = copy.deepcopy(owner)
                fresh["status"] = "busy"
                finished = stop.stop_session(saved, idle_reason="idle; nothing in progress",
                    kill=lambda _pid, _sent: self.fail("busy owner was signalled"),
                    fresh_inventory=lambda _login, _only: machine_inventory([fresh]),
                    clock=lambda: NOW_UTC, codex_mesh=lambda _args: self.fail("busy owner resources stopped"))
                expected = "waiting" if prior["kind"] == "stopping" else prior["kind"]
                self.assertEqual(finished["progress"]["kind"], expected)
                self.assertEqual(finished["stop_issues"], [{"kind": "not ready to stop",
                    "at": NOW, "status": "busy", "progress": expected}])
                self.assertFalse(shutdown_record.stop_incomplete(finished))

    def test_shell_owner_passes_stop_guard_and_preserves_its_ready_checkpoint(self) -> None:
        owner = top_level("shell-owner", 101, status="shell")
        saved = entry(owner)
        saved["where"] = {"kind": "said", "text": "continue the next check", "at": NOW}
        self.create([saved])
        signals: list[int] = []
        finished = stop.stop_session(saved, kill=lambda _pid, sent: signals.append(sent),
            is_alive=lambda _pid: False, session_record_exists=lambda _pid: False,
            fresh_inventory=lambda _login, _only: machine_inventory([owner]),
            clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0,
             sleep=lambda _seconds: None)
        self.assertEqual(signals, [signal.SIGTERM])
        self.assertEqual(finished["progress"], {"kind": "stopped", "at": NOW})
        self.assertEqual(finished["where"], saved["where"])

    def test_move_stops_an_owner_with_unpushed_commits_and_a_full_shutdown_waits(self) -> None:
        for moving in (True, False):
            with self.subTest(moving=moving):
                owner = top_level(f"unpushed-{moving}", 101)
                owner["checkout"] = checkout()
                owner["checkout"]["upstream"] = inventory.Tracking(kind="tracking", ahead=2)
                saved = entry(owner)
                saved["progress"] = {"kind": "ready", "at": NOW}
                if moving:
                    saved["target"] = "claude3"
                self.create([saved])
                signals: list[int] = []
                finished = stop.stop_session(saved, kill=lambda _pid, sent: signals.append(sent),
                    is_alive=lambda _pid: False, session_record_exists=lambda _pid: False,
                    fresh_inventory=lambda _login, _only: machine_inventory([owner]),
                    clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0,
                    sleep=lambda _seconds: None)
                self.assertEqual(signals, [signal.SIGTERM] if moving else [])
                self.assertEqual(finished["progress"]["kind"], "stopped" if moving else "ready")

    def test_idle_decision_preserves_ready_checkpoint_arriving_before_stop_claim(self) -> None:
        owner = top_level("became-ready", 101, status="shell")
        saved = entry(owner, progress="waiting")
        self.create([saved])
        checkpoint = shutdown_record.WhereSaid(kind="said", text="keep this user checkpoint", at=NOW)

        def ready(current: shutdown_record.SessionShutdown) -> None:
            current["progress"] = {"kind": "ready", "at": NOW}
            current["where"] = checkpoint

        _ = shutdown_record.update_session(LOGIN, "became-ready", ready)
        signals: list[int] = []
        finished = stop.stop_session(saved,
            idle_reason="idle; nothing in progress; a background shell was running",
            kill=lambda _pid, sent: signals.append(sent), is_alive=lambda _pid: False,
            session_record_exists=lambda _pid: False,
            fresh_inventory=lambda _login, _only: machine_inventory([owner]),
            clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0,
             sleep=lambda _seconds: None)
        self.assertEqual(signals, [signal.SIGTERM])
        self.assertEqual(finished["progress"]["kind"], "stopped")
        self.assertEqual(finished["where"], checkpoint)

    def test_stop_claim_is_persisted_before_the_first_signal(self) -> None:
        saved = entry(top_level('owner', 101))
        self.create([saved])
        sent: list[int] = []

        def kill(pid: int, sent_signal: int) -> None:
            self.assertEqual(self.stored_record()['progress'], {'kind': 'stopping', 'at': NOW})
            self.assertEqual(pid, 101)
            sent.append(sent_signal)

        finished = stop.stop_session(
            saved, kill=kill, is_alive=lambda _pid: False,
            session_record_exists=lambda _pid: False,
            fresh_inventory=lambda _login, _only: machine_inventory([saved['session']]),
            clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0,
             sleep=lambda _seconds: None,
        )
        self.assertEqual(sent, [signal.SIGTERM])
        self.assertEqual(finished['progress']['kind'], 'stopped')

    def test_idle_authorization_stops_waiting_owner_and_records_where_before_sigterm(self) -> None:
        reasons: tuple[stop.IdleStopReason, ...] = (
            "idle; nothing in progress", "idle after the pause message; no ready line",
        )
        for index, reason in enumerate(reasons):
            with self.subTest(reason=reason):
                owner = top_level(f"idle-owner-{index}", 101 + index)
                saved = entry(owner, progress="waiting")
                if reason == "idle after the pause message; no ready line":
                    saved["settle_message"] = {"kind": "sent", "at": NOW}
                    owner["updated_at"] = int(NOW_UTC.timestamp() * 1000) + 1
                self.create([saved])
                signals: list[tuple[int, int]] = []

                def kill(pid: int, sent_signal: int) -> None:
                    current = shutdown_record.find_session(LOGIN, owner["session_id"])
                    assert current is not None
                    self.assertEqual(current["progress"], {"kind": "stopping", "at": NOW})
                    self.assertEqual(current["where"], {"kind": "said", "text": reason, "at": NOW})
                    signals.append((pid, sent_signal))

                finished = stop.stop_session(saved, idle_reason=reason,
                    kill=kill, is_alive=lambda _pid: False, session_record_exists=lambda _pid: False,
                    fresh_inventory=lambda _login, _only: machine_inventory([owner]),
                    clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0,
                     sleep=lambda _seconds: None)
                self.assertEqual(signals, [(owner["pid"], signal.SIGTERM)])
                self.assertEqual(finished["progress"], {"kind": "stopped", "at": NOW})
                self.assertEqual(finished["where"], {"kind": "said", "text": reason, "at": NOW})
                self.assertEqual(finished["stop_issues"], [])

    def test_waiting_owner_without_idle_authorization_is_not_signalled(self) -> None:
        owner = top_level("unapproved-owner", 101)
        saved = entry(owner, progress="waiting")
        saved["where"] = {"kind": "said", "text": "existing checkpoint", "at": NOW}
        self.create([saved])
        finished = stop.stop_session(saved, idle_reason="not an idle stop",
            kill=lambda _pid, _sent: self.fail("waiting owner was signalled without idle authorization"),
            is_alive=lambda _pid: False, session_record_exists=lambda _pid: False,
            fresh_inventory=lambda _login, _only: machine_inventory([owner]),
            clock=lambda: NOW_UTC, codex_mesh=lambda _args: self.fail("resources were stopped"),
             sleep=lambda _seconds: None)
        self.assertEqual(finished["progress"]["kind"], "waiting")
        self.assertEqual(finished["stop_issues"], [shutdown_record.NotReadyToStop(
            kind="not ready to stop", at=NOW, status="idle", progress="waiting")])
        self.assertEqual(finished["where"], saved["where"])

    def test_idle_authorization_rechecks_fresh_owner_and_workers_before_signalling(self) -> None:
        reasons: tuple[stop.IdleStopReason, ...] = (
            "idle; nothing in progress", "idle after the pause message; no ready line",
        )
        for index, reason in enumerate(reasons):
            for obstruction in ("busy owner", "blocked checkout", "busy worker"):
                with self.subTest(reason=reason, obstruction=obstruction):
                    owner = top_level(f"changed-owner-{index}-{obstruction}", 101)
                    saved = entry(owner, progress="waiting")
                    fresh = copy.deepcopy(owner)
                    workers: list[inventory.Session] = []
                    if obstruction == "busy owner":
                        fresh["status"] = "busy"
                    elif obstruction == "blocked checkout":
                        blocked = checkout()
                        blocked["upstream"] = inventory.Tracking(kind="tracking", ahead=1)
                        fresh["checkout"] = blocked
                    else:
                        worker = worker_session("busy-worker", 102, owner=owner["session_id"])
                        worker["status"] = "busy"
                        workers.append(worker)
                    self.create([saved])
                    finished = stop.stop_session(saved, idle_reason=reason,
                        kill=lambda _pid, _sent: self.fail("changed owner was signalled"),
                        is_alive=lambda _pid: False, session_record_exists=lambda _pid: False,
                        fresh_inventory=lambda _login, _only: machine_inventory([fresh, *workers]),
                        clock=lambda: NOW_UTC, codex_mesh=lambda _args: self.fail("resources were stopped"),
                         sleep=lambda _seconds: None)
                    self.assertEqual(finished["progress"]["kind"], "waiting")
                    self.assertEqual(finished["stop_issues"], [shutdown_record.NotReadyToStop(
                        kind="not ready to stop", at=NOW, status=fresh["status"], progress="waiting")])

    def test_reaping_busy_owner_stops_without_ready_but_checks_identity_before_signal(self) -> None:
        owner = top_level("reap-owner", 101, status="busy")
        saved = entry(owner, progress="waiting")
        saved["progress"] = shutdown_record.SessionReaping(kind="reaping", at=NOW,
            because="account out of usage", detail="weekly limit")
        self.create([saved])
        signals: list[tuple[int, int]] = []
        def kill(pid: int, sent_signal: int) -> None:
            self.assertEqual(self.stored_record()["progress"]["kind"], "stopping")
            signals.append((pid, sent_signal))
        finished = stop.stop_session(saved, kill=kill, is_alive=lambda _pid: False,
            session_record_exists=lambda _pid: False,
            fresh_inventory=lambda _login, _only: machine_inventory([owner]),
            clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0,
            sleep=lambda _seconds: None)
        self.assertEqual(signals, [(101, signal.SIGTERM)])
        self.assertEqual(finished["progress"]["kind"], "stopped")
        self.assertEqual(saved["force"], "wait for ready")

    def test_reaping_owner_with_reused_pid_is_never_signalled(self) -> None:
        owner = top_level("reap-reused", 101, status="busy")
        saved = entry(owner, progress="waiting")
        saved["progress"] = shutdown_record.SessionReaping(kind="reaping", at=NOW,
            because="refused in its transcript", detail="weekly limit")
        self.create([saved])
        changed = copy.deepcopy(owner)
        changed["proc_start"] = "different-process-start"
        finished = stop.stop_session(saved,
            kill=lambda _pid, _signal: self.fail("reused process was signalled"),
            is_alive=lambda _pid: True, session_record_exists=lambda _pid: True,
            fresh_inventory=lambda _login, _only: machine_inventory([changed]),
            clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0,
            sleep=lambda _seconds: None)
        self.assertEqual(finished["progress"]["kind"], "process identity lost")

    def test_stop_identity_reread_filters_owner_and_disables_desktop_refresh(self) -> None:
        owner = top_level("identity-owner", 101)
        saved = entry(owner)
        self.create([saved])
        signals: list[tuple[int, int]] = []
        with patch.object(inventory, "inventory", return_value=machine_inventory([owner])) as read:
            finished = stop.stop_session(saved,
                kill=lambda pid, sent: signals.append((pid, sent)),
                is_alive=lambda _pid: False, session_record_exists=lambda _pid: False,
                clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0,
                 sleep=lambda _seconds: None)
        self.assertEqual(read.call_args_list, [call(LOGIN, only=frozenset({owner["session_id"]}),
            refresh_desktops=False)] * 5)
        self.assertEqual(signals, [(101, signal.SIGTERM)])
        self.assertEqual(finished["progress"]["kind"], "stopped")

    def test_filtered_identity_reread_preserves_absent_and_unattributed_outcomes(self) -> None:
        owner = top_level("missing-owner", 101)
        registered = sessions.SessionRecord(pid=owner["pid"], sessionId=owner["session_id"],
            name=owner["name"], procStart=owner["proc_start"], messagingSocketPath="",
            updatedAt=1, tmux="", formerNames=[])
        scenarios: tuple[tuple[Literal["gone", "process start mismatch", "account unreadable"], str], ...] = (("gone", "session absent"),
            ("process start mismatch", "session identity lost"),
            ("account unreadable", "session account unreadable"))
        for reason, expected in scenarios:
            with (
                self.subTest(reason=reason),
                patch.object(sessions, "live_sessions", return_value=[] if reason == "gone" else [registered]),
                patch.object(inventory, "showrunner_registrations", return_value=inventory.ShowrunnerRegistrations(())),
                patch.object(inventory, "_unit_sessions", return_value={}),
                patch.object(inventory, "_active_run_directories", return_value={}),
                patch.object(inventory, "_record_matches_process", return_value=reason != "process start mismatch"),
                patch.object(inventory, "_configuration_for_session", return_value=inventory.UnreadableSessionConfiguration()),
                patch.object(inventory, "_snapshot_desktops") as snapshot,
                patch.object(inventory, "label_for", return_value=LABEL),
                patch.object(inventory, "inventory", wraps=inventory.inventory) as read,
            ):
                report = stop._fresh_inventory(LOGIN, frozenset({owner["session_id"]}))  # pyright: ignore[reportPrivateUsage]
                observed = stop.fresh_session(owner, report)
            read.assert_called_once_with(LOGIN, only=frozenset({owner["session_id"]}), refresh_desktops=False)
            self.assertEqual(report["sessions"], [])
            self.assertEqual(observed["kind"], expected)
            self.assertEqual(report["unattributed"], [] if reason == "gone" else [
                inventory.UnattributedSession(pid=owner["pid"], name=owner["name"], reason=reason)])
            snapshot.assert_not_called()

    def test_recent_stop_claim_has_no_signal_or_cleanup_effect(self) -> None:
        saved = entry(unit('owner', 101))
        saved['progress'] = shutdown_record.SessionStopping(kind='stopping', at=NOW)
        self.create([saved])
        finished = stop.stop_session(
            saved, kill=lambda _pid, _sent: self.fail('a claimed owner was signalled'),
            fresh_inventory=lambda _login, _only: self.fail('a claimed owner was inspected'),
            clock=lambda: NOW_UTC + timedelta(seconds=119),
            codex_mesh=lambda _args: self.fail('claimed cleanup was repeated'),

        )
        self.assertEqual(finished, saved)

    def test_usage_limit_held_owner_is_claimed_and_stopped_without_now(self) -> None:
        saved = entry(top_level("owner", 101))
        saved["progress"] = shutdown_record.SessionHeldByUsageLimit(kind="held by usage limit", at=NOW)
        self.create([saved])
        signals: list[int] = []
        def kill(pid: int, sent_signal: int) -> None:
            self.assertEqual(pid, 101)
            self.assertEqual(self.stored_record()["progress"], {"kind": "stopping", "at": NOW})
            signals.append(sent_signal)
        finished = stop.stop_session(saved, kill=kill, is_alive=lambda _pid: False,
            session_record_exists=lambda _pid: False,
            fresh_inventory=lambda _login, _only: machine_inventory([saved["session"]]),
            clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0,
            sleep=lambda _seconds: None)
        self.assertEqual(signals, [signal.SIGTERM])
        self.assertEqual(finished["progress"]["kind"], "stopped")
        self.assertEqual(finished["force"], "wait for ready")

    def test_usage_limit_held_owner_with_busy_worker_keeps_held_progress_in_issue(self) -> None:
        saved = entry(top_level("owner", 101))
        saved["progress"] = shutdown_record.SessionHeldByUsageLimit(kind="held by usage limit", at=NOW)
        worker = worker_session("worker", 102, owner="owner")
        worker["status"] = "busy"
        saved["workers"] = [worker]
        self.create([saved])
        finished = stop.stop_session(saved,
            kill=lambda _pid, _sent: self.fail("busy worker owner received a signal"),
            fresh_inventory=lambda _login, _only: machine_inventory([saved["session"], worker]),
            clock=lambda: NOW_UTC, codex_mesh=lambda _args: self.fail("busy worker was ended"))
        self.assertEqual(finished["progress"]["kind"], "held by usage limit")
        self.assertEqual(finished["stop_issues"], [shutdown_record.NotReadyToStop(kind="not ready to stop",
            at=NOW, status="idle", progress="held by usage limit")])

    def test_stale_stop_claim_retries_an_idle_owner(self) -> None:
        saved = entry(top_level('owner', 101))
        saved['progress'] = shutdown_record.SessionStopping(kind='stopping', at=(NOW_UTC - timedelta(seconds=120)).isoformat())
        self.create([saved])
        self.assertEqual(self.perform(saved)['progress']['kind'], 'stopped')

    def test_cleanup_retry_only_repeats_unconfirmed_resources_without_a_signal(self) -> None:
        pending = str(self.root / "pending")
        saved = entry(unit('owner', 101, run_dirs=[str(self.root / "clean"), pending]))
        saved['progress'] = shutdown_record.SessionStopped(kind='stopped', at=NOW)
        saved['stop_issues'] = [shutdown_record.CodexServerLeftRunning(
            kind='codex server left running', at=NOW, run_dir=pending, cause='stop not confirmed',
        ), shutdown_record.UnitSessionLeftRunning(
            kind='unit session left running', at=NOW, host="herdr", session_name='herdr-owner', cause='stop not confirmed',
        )]
        self.create([saved])
        codex: list[list[str]] = []
        finished = stop.stop_session(
            saved, kill=lambda _pid, _sent: self.fail('a stopped owner was signalled'),
            is_alive=lambda _pid: False, clock=lambda: NOW_UTC,
            codex_mesh=lambda args: codex.append(args) or 0,

        )
        self.assertEqual(codex, [['stop', '--pause', '--session-dir', pending]])
        self.assertEqual(finished['stop_issues'], [])
        self.assertEqual(finished['progress'], saved['progress'])

    def test_fresh_session_reports_owned_workers_and_preserves_the_recorded_identity(self) -> None:
        owner = top_level('owner', 101)
        worker = worker_session('worker', 102, owner='owner')
        unrelated = worker_session('other', 103, owner='elsewhere')
        observed = stop.fresh_session(owner, machine_inventory([owner, worker, unrelated]))
        self.assertEqual(observed, {'kind': 'matching session', 'session': owner, 'workers': [worker]})
        changed = copy.deepcopy(owner)
        changed['pid'] = 201
        self.assertEqual(stop.fresh_session(owner, machine_inventory([changed])), {'kind': 'session identity lost'})
        self.assertEqual(owner['pid'], 101)

    def test_fresh_session_rejects_any_resumed_occurrence_even_beside_the_recorded_process(self) -> None:
        owner = top_level("owner", 101)
        resumed = copy.deepcopy(owner)
        resumed.update(pid=501, proc_start="200")
        reports: tuple[list[inventory.Session], ...] = ([owner, resumed], [resumed, owner])
        for rows in reports:
            with self.subTest(pids=[row["pid"] for row in rows]):
                self.assertEqual(stop.fresh_session(owner, machine_inventory(rows)),
                                 {"kind": "session identity lost"})

    def test_busy_worker_holds_its_owner_until_it_is_idle_and_is_never_signalled(self) -> None:
        saved = entry(top_level('owner', 101))
        worker = worker_session('worker', 102, owner='owner')
        worker['status'] = 'busy'
        saved['workers'] = [worker]
        self.create([saved])
        self.assertEqual(self.perform(saved)['progress']['kind'], 'ready')
        worker['status'] = 'idle'
        saved['workers'] = [worker]
        self.assertEqual(self.perform(saved)['progress']['kind'], 'stopped')

    def test_cancel_during_a_stop_claim_keeps_its_record_and_timers(self) -> None:
        saved = entry(top_level('owner', 101))
        saved['timers'] = [shutdown_record.TimerRestore(instance='owner-timer', was_enabled=True)]
        self.create([saved])
        entered = threading.Event()
        release = threading.Event()
        failures: list[BaseException] = []

        def kill(_pid: int, _sent: int) -> None:
            entered.set()
            self.assertTrue(release.wait(HANG_GUARD_SECONDS))

        def worker() -> None:
            try:
                _ = stop.stop_session(
                    saved, kill=kill, is_alive=lambda _pid: False,
                    session_record_exists=lambda _pid: False,
                    fresh_inventory=lambda _login, _only: machine_inventory([saved['session']]),
                    clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0,
                     sleep=lambda _seconds: None,
                )
            except BaseException as error:
                failures.append(error)

        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(entered.wait(HANG_GUARD_SECONDS))
            with patch.object(settle, 'run_notifier') as timers, patch.object(settle, 'send_message') as send, redirect_stdout(io.StringIO()):
                _ = settle.cancel(Account('claude', LOGIN, LABEL), here=True)
            timers.assert_not_called()
            send.assert_not_called()
            self.assertEqual(self.stored_record()['progress']['kind'], 'stopping')
            self.assertEqual(self.stored_record()['timers'], saved['timers'])
        finally:
            release.set()
            thread.join(HANG_GUARD_SECONDS)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])


    def test_busy_entry_is_refused_without_now(self) -> None:
        stored = top_level('busy', 101)
        current = top_level('busy', 101, status='busy')
        self.create(records_for_test([entry(stored)]))
        killed: list[int] = []
        report = stop.stop_session(self.stored_record(), kill=lambda pid, _signal: killed.append(pid), is_alive=lambda _pid: False, fresh_inventory=lambda _login, _scope: machine_inventory([current]), clock=lambda: NOW_UTC, codex_mesh=lambda _arguments: 0,  sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
        self.assertEqual(killed, [])
        self.assertNotEqual(report['progress']['kind'], 'stopping')
        self.assertEqual(report['stop_issues'], [shutdown_record.NotReadyToStop(kind='not ready to stop', at=NOW, status='busy', progress='ready')])

    def test_account_unreadable_entry_is_not_signalled_and_keeps_an_issue(self) -> None:
        stored = top_level('unreadable', 101)
        self.create(records_for_test([entry(stored)]))
        killed: list[int] = []
        unattributed = inventory.UnattributedSession(pid=101, name='unreadable', reason='account unreadable')
        report = stop.stop_session(self.stored_record(), kill=lambda pid, _signal: killed.append(pid), is_alive=lambda _pid: True, fresh_inventory=lambda _login, _scope: machine_inventory([], unattributed=[unattributed]), clock=lambda: NOW_UTC, codex_mesh=lambda _arguments: 0,  sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
        self.assertEqual(killed, [])
        self.assertEqual(report['stop_issues'], [shutdown_record.AccountUnreadableAtStop(kind='account unreadable', at=NOW)])
        self.assertNotEqual(report['progress']['kind'], 'stopping')

    def test_forced_stop_signals_a_busy_entry(self) -> None:
        stored = top_level('busy', 101)
        current = top_level('busy', 101, status='busy')
        self.create(records_for_test([entry(stored)]))
        killed: list[int] = []
        report = stop.stop_session(self.stored_record(), forced=True, kill=lambda pid, _signal: killed.append(pid), is_alive=lambda _pid: False, fresh_inventory=lambda _login, _scope: machine_inventory([current]), clock=lambda: NOW_UTC, codex_mesh=lambda _arguments: 0,  sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
        self.assertEqual(killed, [101])
        self.assertEqual(report['stop_issues'], [])

    def test_forced_stop_signals_an_account_unreadable_entry_only_when_its_process_is_unchanged(self) -> None:
        for session_id, pid, running_start, expected in (('same', 101, 'start-same', [101]), ('replaced', 102, 'a later process', [])):
            with self.subTest(session_id=session_id):
                self.create(records_for_test([entry(top_level(session_id, pid))]))
                unattributed = inventory.UnattributedSession(pid=pid, name=session_id, reason='account unreadable')
                saved = shutdown_record.find_session(LOGIN, session_id)
                assert saved is not None
                killed: list[int] = []
                with patch.object(sessions, 'process_start', return_value=running_start):
                    _ = stop.stop_session(saved, forced=True, kill=lambda pid, _signal: killed.append(pid), is_alive=lambda _pid: False, fresh_inventory=lambda _login, _scope: machine_inventory([], unattributed=[unattributed]), clock=lambda: NOW_UTC, codex_mesh=lambda _arguments: 0,  sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
                self.assertEqual(killed, expected)

    def test_now_pauses_codex_run_without_ending_busy_workers(self) -> None:
        run_dir = str(self.root / 'run')
        owned_server = server(run_dir, 900, busy_workers=['impl', 'test'])
        stored = top_level('busy', 101, status='busy', run_dirs=[run_dir], codex_servers=[owned_server])
        self.create(records_for_test([entry(stored)], force='now'))
        events: list[str] = []

        def codex(arguments: list[str]) -> int:
            events.append('codex ' + ' '.join(arguments))
            return 0
        report = stop.stop_session(self.stored_record(), kill=lambda pid, sent: events.append(f'kill {pid} {sent}'), is_alive=lambda _pid: False, fresh_inventory=lambda _login, _scope: machine_inventory([stored]), clock=lambda: NOW_UTC, codex_mesh=codex, sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
        self.assertIn(f'codex stop --pause --session-dir {run_dir}', events)
        self.assertEqual([event for event in events if event.startswith('codex ')], [f'codex stop --pause --session-dir {run_dir}'])
        self.assertEqual([event for event in events if event.startswith('kill ')], [f'kill 101 {signal.SIGTERM}'])
        self.assertNotEqual(report['progress']['kind'], 'stopping')

    def test_ready_move_pauses_running_codex_workers_but_full_stop_waits(self) -> None:
        for moving in (True, False):
            with self.subTest(moving=moving):
                run = str(self.root / f"run-{moving}")
                owner = top_level(f"owner-{moving}", 101, run_dirs=[run],
                    codex_servers=[server(run, 900, busy_workers=["Writer"])])
                saved = entry(owner)
                if moving:
                    saved["target"] = "claude3"
                self.create([saved])
                events: list[str] = []

                def pause(arguments: list[str]) -> int:
                    events.append("codex " + " ".join(arguments))
                    return 0

                finished = stop.stop_session(saved,
                    kill=lambda pid, _sent: events.append(f"kill {pid}"),
                    is_alive=lambda _pid: False, session_record_exists=lambda _pid: False,
                    fresh_inventory=lambda _login, _only: machine_inventory([owner]),
                    clock=lambda: NOW_UTC, codex_mesh=pause, sleep=lambda _seconds: None)
                self.assertEqual(events, ["kill 101", f"codex stop --pause --session-dir {run}"] if moving else [])
                self.assertEqual(finished["progress"]["kind"], "stopped" if moving else "ready")

    def test_stop_persists_owned_busy_and_idle_seats_before_pause_removes_launcher_evidence(self) -> None:
        run = self.root / "owned run"
        other = self.root / "another directors run"
        run.mkdir()
        other.mkdir()
        launcher = ["bash", "/config/scripts/direct/implement.sh", "--phase", "1", str(run), "/checkout with spaces", "/old/prompt.md", "task", "worker instructions", "impl", "resume task", "0", "impl", "prefix"]
        proc = self.root / "proc"
        for pid, parent, argv in ((800, 801, ["python3", "codex_mesh.py", "start"]), (801, 1, launcher)):
            path = proc / str(pid)
            path.mkdir(parents=True)
            _ = (path / "stat").write_text(f"{pid} (fixture) S {parent} 0 0\n")
            _ = (path / "cmdline").write_bytes(("\0".join(argv) + "\0").encode())
        roster = run / "mesh_roster.json"
        _ = roster.write_text(json.dumps({
            "busy": {"thread_id": "busy-thread", "role": "impl", "status": "running", "turn_id": "turn-1", "launcher_pid": 800},
            "review": {"thread_id": "review-thread", "role": "review", "status": "done"},
            "ended": {"thread_id": "ended-thread", "role": "impl", "status": "ended"},
        }))
        foreign = other / "mesh_roster.json"
        _ = foreign.write_text(json.dumps({"foreign": {"thread_id": "foreign-thread", "status": "running"}}))
        stored = unit("director", 101, run_dirs=[str(run)], codex_servers=[server(str(run), 900, busy_workers=["busy"])])
        self.create(records_for_test([entry(stored)], force="now"))
        calls: list[list[str]] = []

        def pause(arguments: list[str]) -> int:
            calls.append(arguments)
            # Persistence must precede cleanup, which can destroy process evidence.
            seats = self.stored_record().get("codex_seats", [])
            self.assertEqual({seat["seat"] for seat in seats}, {"busy", "review"})
            roster.unlink()
            return 0

        def launcher_alive(pid: int) -> bool:
            return pid in {800, 801}

        with patch.object(inventory, "_pid_alive", side_effect=launcher_alive):
            finished = stop.stop_session(self.stored_record(), kill=lambda _pid, _sent: None,
                is_alive=lambda _pid: False, session_record_exists=lambda _pid: False,
                fresh_inventory=lambda _login, _only: machine_inventory([stored]),
                clock=lambda: NOW_UTC, codex_mesh=pause,
                sleep=lambda _seconds: None)
        self.assertEqual(calls, [["stop", "--pause", "--session-dir", str(run)]])
        seats = {seat["seat"]: seat for seat in finished.get("codex_seats", [])}
        self.assertEqual(seats["busy"], shutdown_record.PausedSeat(run_dir=str(run), seat="busy", role="impl", thread_id="busy-thread", turn_live=True, launcher_command=launcher))
        self.assertEqual(seats["review"], shutdown_record.PausedSeat(run_dir=str(run), seat="review", role="review", thread_id="review-thread", turn_live=False, launcher_command=[]))
        prompt = run / "busy.resume-prompt.md"
        self.assertEqual(prompt.read_text().strip(), "You were paused when your unit director stopped for an account move or restart. Continue the task you were on from where you stopped; your earlier instructions still hold.")
        self.assertEqual((run / "review.resume-prompt.md").read_text().strip(), "You were paused while idle when your unit director stopped for an account move or restart. Nothing was in progress: reply ready and wait for your next assignment; your earlier instructions still hold.")
        self.assertTrue(foreign.exists())
        self.assertEqual(self.stored_record().get("codex_seats"), finished.get("codex_seats"))

    def test_absent_entry_is_not_signalled_or_refused_for_readiness(self) -> None:
        stored = top_level('gone', 101)
        self.create(records_for_test([entry(stored, 'waiting')]))
        killed: list[int] = []
        report = stop.stop_session(self.stored_record(), kill=lambda pid, _signal: killed.append(pid), is_alive=lambda _pid: False, fresh_inventory=lambda _login, _scope: machine_inventory([]), clock=lambda: NOW_UTC, codex_mesh=lambda _arguments: 0,  sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
        self.assertEqual(killed, [])
        self.assertEqual(report['progress']['kind'], 'already gone')
        self.assertNotEqual(report['progress']['kind'], 'stopping')

    def test_absent_unit_stops_each_run_once_and_its_exact_session_name(self) -> None:
        run_dir = str(self.root / 'run')
        stored = unit('gone-unit', 101, session_name='exact-unit-pane', run_dirs=[run_dir], codex_servers=[server(run_dir, 900)])
        self.create(records_for_test([entry(stored, 'waiting')]))
        codex_calls: list[list[str]] = []

        def codex(arguments: list[str]) -> int:
            codex_calls.append(arguments)
            return 0

        report = stop.stop_session(self.stored_record(), kill=lambda _pid, _signal: self.fail('an absent owner was signalled'), is_alive=lambda _pid: False, fresh_inventory=lambda _login, _scope: machine_inventory([]), clock=lambda: NOW_UTC, codex_mesh=codex,  sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
        self.assertEqual(codex_calls, [['stop', '--pause', '--session-dir', run_dir]])
        self.assertEqual(self.herdr_commands()[0], ["--session", "exact-unit-pane", "server", "stop"])
        self.assertNotEqual(report['progress']['kind'], 'stopping')

    def herdr_replies(self, running: list[str], *, stop_error: bool = False) -> None:
        entries = [herdr.SessionEntry(name=name, running=True, default=False,
                    session_dir="/fixture", socket_path="/fixture/socket") for name in running]
        responses: dict[str, CommandResponse] = {
            "default": {"code": 0, "stdout": ""},
            "--session default session list --json": {
                "code": 0, "stdout": json.dumps(herdr.SessionListResult(sessions=entries)),
            },
        }
        if stop_error:
            responses["--session unit-host server stop"] = CommandResponse(code=1, stdout="", stderr="fixture stop failed")
        _ = (self.root / "herdr-responses.json").write_text(json.dumps(responses))

    def herdr_commands(self) -> list[list[str]]:
        path = self.root / "herdr.jsonl"
        return [cast(list[str], json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def test_herdr_unit_stop_runs_after_claude_exits(self) -> None:
        for state in ("present", "absent"):
            with self.subTest(state=state):
                log = self.root / "herdr.jsonl"
                log.unlink(missing_ok=True)
                owner = unit(f"herdr-{state}", 101, session_name="unit-host", session_host="herdr")
                saved = entry(owner)
                self.create([saved])
                signals: list[int] = []

                def kill(_pid: int, sent: int) -> None:
                    self.assertEqual(self.herdr_commands(), [])
                    signals.append(sent)

                report = stop.stop_session(saved, kill=kill, is_alive=lambda _pid: False,
                    fresh_inventory=lambda _login, _scope: machine_inventory([] if state == "absent" else [owner]),
                    clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0,

                    sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
                self.assertEqual(signals, [signal.SIGTERM] if state == "present" else [])
                self.assertEqual(self.herdr_commands(), [
                    ["--session", "unit-host", "server", "stop"],
                    ["--user", "is-active", "herdr-unit-host.service"],
                    ["--user", "stop", "herdr-unit-host.service"],
                    ["--session", "default", "session", "list", "--json"],
                ])
                self.assertEqual(report["stop_issues"], [])
                self.assertEqual(report["progress"]["kind"], "stopped" if state == "present" else "already gone")

    def test_other_running_herdr_sessions_do_not_make_the_stopped_unit_a_leftover(self) -> None:
        self.herdr_replies(["unrelated"])
        saved = entry(unit("named-only", 101, session_name="unit-host", session_host="herdr"))
        self.create([saved])
        report = self.perform(saved)
        self.assertEqual(report["stop_issues"], [])
        self.assertEqual(self.herdr_commands()[0], ["--session", "unit-host", "server", "stop"])

    def test_herdr_unit_leftover_or_unreadable_stop_records_its_host(self) -> None:
        for failure in ("still running", "stop failed", "listing failed"):
            with self.subTest(failure=failure):
                self.herdr_replies(["unit-host", "unrelated"], stop_error=failure == "stop failed")
                if failure == "listing failed":
                    path = self.root / "herdr-responses.json"
                    replies = cast(dict[str, object], json.loads(path.read_text()))
                    replies["--session default session list --json"] = {"code": 1, "stderr": "fixture unreadable"}
                    _ = path.write_text(json.dumps(replies))
                owner = unit(failure, 101, session_name="unit-host", session_host="herdr")
                saved = entry(owner)
                self.create([saved])
                report = self.perform(saved)
                self.assertEqual(report["stop_issues"], [shutdown_record.UnitSessionLeftRunning(
                    kind="unit session left running", at=NOW, host="herdr",
                    session_name="unit-host", cause="stop not confirmed")])
                self.assertEqual(report["progress"]["kind"], "stopped")

    def test_herdr_unit_cleanup_retry_does_not_signal_the_stopped_owner(self) -> None:
        saved = entry(unit("retry", 101, session_name="unit-host", session_host="herdr"))
        saved["progress"] = shutdown_record.SessionStopped(kind="stopped", at=NOW)
        saved["stop_issues"] = [shutdown_record.UnitSessionLeftRunning(kind="unit session left running", at=NOW,
            host="herdr", session_name="unit-host", cause="stop not confirmed")]
        self.create([saved])
        report = stop.stop_session(saved,
            kill=lambda _pid, _sent: self.fail("stopped owner signalled"),
            is_alive=lambda _pid: False, clock=lambda: NOW_UTC,
            fresh_inventory=lambda _login, _only: machine_inventory([]),
            codex_mesh=lambda _args: self.fail("unexpected Codex cleanup"))
        self.assertEqual(report["stop_issues"], [])
        self.assertEqual(self.herdr_commands()[0], ["--session", "unit-host", "server", "stop"])

    def test_failed_herdr_stop_with_no_running_session_clears_cleanup(self) -> None:
        for error in (herdr.HerdrFailure("stop failed"), OSError("stop failed"),
                      subprocess.TimeoutExpired("herdr", 10)):
            with self.subTest(error=type(error).__name__):
                saved = entry(unit(type(error).__name__, 101,
                    session_name="unit-host", session_host="herdr"))
                saved["progress"] = shutdown_record.SessionStopped(kind="stopped", at=NOW)
                saved["stop_issues"] = [shutdown_record.UnitSessionLeftRunning(
                    kind="unit session left running", at=NOW, host="herdr",
                    session_name="unit-host", cause="stop not confirmed")]
                self.create([saved])
                with patch.object(herdr, "stop_session", side_effect=error) as stop_server, \
                        patch.object(herdr, "live_sessions", return_value=[]) as list_sessions:
                    report = self.perform(saved)
                stop_server.assert_called_once_with("unit-host")
                list_sessions.assert_called_once_with()
                self.assertEqual(report["stop_issues"], [])
                self.assertEqual(report["progress"]["kind"], "stopped")

    def test_failed_herdr_stop_with_a_running_session_keeps_cleanup_pending(self) -> None:
        saved = entry(unit("still-running", 101,
            session_name="unit-host", session_host="herdr"))
        self.create([saved])
        running = [herdr.SessionEntry(name="unit-host", running=True,
            default=False, session_dir="/fixture", socket_path="/fixture/socket")]
        with patch.object(herdr, "stop_session", side_effect=herdr.HerdrFailure("stop failed")), \
                patch.object(herdr, "live_sessions", return_value=running) as list_sessions:
            report = self.perform(saved)
        list_sessions.assert_called_once_with()
        self.assertEqual(report["stop_issues"], [shutdown_record.UnitSessionLeftRunning(
            kind="unit session left running", at=NOW, host="herdr",
            session_name="unit-host", cause="stop not confirmed")])

    def test_failed_herdr_stop_with_unreadable_liveness_keeps_cleanup_pending(self) -> None:
        for error in (herdr.HerdrFailure("list failed"), OSError("list failed"),
                      subprocess.TimeoutExpired("herdr", 10)):
            with self.subTest(error=type(error).__name__):
                saved = entry(unit(type(error).__name__, 101,
                    session_name="unit-host", session_host="herdr"))
                self.create([saved])
                with patch.object(herdr, "stop_session", side_effect=herdr.HerdrFailure("stop failed")), \
                        patch.object(herdr, "live_sessions", side_effect=error) as list_sessions:
                    report = self.perform(saved)
                list_sessions.assert_called_once_with()
                self.assertEqual(report["stop_issues"], [shutdown_record.UnitSessionLeftRunning(
                    kind="unit session left running", at=NOW, host="herdr",
                    session_name="unit-host", cause="stop not confirmed")])

    def test_herdr_unit_with_lost_identity_is_left_running_without_cleanup(self) -> None:
        owner = unit("identity", 101, session_name="unit-host", session_host="herdr")
        saved = entry(owner)
        self.create([saved])
        changed = unit("identity", 202, session_name="unit-host", session_host="herdr")
        report = stop.stop_session(saved,
            kill=lambda _pid, _sent: self.fail("changed owner signalled"),
            fresh_inventory=lambda _login, _scope: machine_inventory([changed]), clock=lambda: NOW_UTC)
        self.assertEqual(self.herdr_commands(), [])
        self.assertEqual(report["progress"]["kind"], "process identity lost")
        self.assertEqual(report["stop_issues"], [shutdown_record.UnitSessionLeftRunning(
            kind="unit session left running", at=NOW, host="herdr",
            session_name="unit-host", cause="owner identity lost")])

    def test_herdr_unit_cleanup_stops_when_another_request_takes_the_claim(self) -> None:
        owner = unit("claim", 101, session_name="unit-host", session_host="herdr")
        saved = entry(owner)
        self.create([saved])

        def replace_request(_name: str) -> None:
            def change(current: shutdown_record.SessionShutdown) -> None:
                current["request_id"] = "new-request"
            _ = shutdown_record.update_session(LOGIN, owner["session_id"], change)

        with patch.object(herdr, "stop_session", side_effect=replace_request) as stop_server, \
                patch.object(herdr, "live_sessions") as list_sessions:
            report = self.perform(saved)
        stop_server.assert_called_once_with("unit-host")
        list_sessions.assert_not_called()
        self.assertEqual(report["request_id"], "new-request")
        self.assertEqual(report["progress"]["kind"], "stopping")

    def test_herdr_unit_with_an_unexited_claude_never_stops_its_server(self) -> None:
        owner = unit("busy-owner", 101, session_name="unit-host", session_host="herdr")
        saved = entry(owner)
        self.create([saved])
        report = stop.stop_session(saved, kill=lambda _pid, _sent: None,
            is_alive=lambda _pid: True, fresh_inventory=lambda _login, _scope: machine_inventory([owner]),
            clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0,

            sleep=lambda _seconds: None, session_record_exists=lambda _pid: True)
        self.assertEqual(self.herdr_commands(), [])
        self.assertEqual(report["progress"]["kind"], "stop failed")

    def test_process_identity_changed_before_first_signal_is_left_running(self) -> None:
        run_dir = str(self.root / 'run')
        stored = unit('reused', 101, session_name='reused-unit', run_dirs=[run_dir])
        changed = unit('reused', 202, session_name='reused-unit', run_dirs=[run_dir])
        self.create(records_for_test([entry(stored)]))
        killed: list[int] = []
        codex_calls: list[list[str]] = []
        report = stop.stop_session(self.stored_record(), kill=lambda pid, _signal: killed.append(pid), is_alive=lambda _pid: True, fresh_inventory=lambda _login, _scope: machine_inventory([changed]), clock=lambda: NOW_UTC, codex_mesh=lambda arguments: codex_calls.append(arguments) or 0,  sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
        self.assertEqual(killed, [])
        self.assertEqual(codex_calls, [])
        self.assertEqual(report['progress']['kind'], 'process identity lost')
        self.assertEqual(report['stop_issues'], [shutdown_record.CodexServerLeftRunning(kind='codex server left running', at=NOW, run_dir=run_dir, cause='owner identity lost'), shutdown_record.UnitSessionLeftRunning(kind='unit session left running', at=NOW, host="herdr", session_name='reused-unit', cause='owner identity lost')])
        self.assertNotEqual(report['progress']['kind'], 'stopping')

    def test_process_identity_changed_before_second_signal_is_not_signalled_again(self) -> None:
        stored = top_level('reused', 101)
        changed = top_level('reused', 202)
        self.create(records_for_test([entry(stored)]))
        killed: list[int] = []
        # Resume after the first effect, rather than after a particular number
        # of safety reads: intermediate capture checks must still see the owner.
        report = stop.stop_session(self.stored_record(), kill=lambda pid, _signal: killed.append(pid), is_alive=lambda _pid: True, fresh_inventory=lambda _login, _scope: machine_inventory([changed if killed else stored]), clock=lambda: NOW_UTC, codex_mesh=lambda _arguments: 0,  sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
        self.assertEqual(killed, [101])
        self.assertEqual(report['progress']['kind'], 'process identity lost')
        self.assertNotEqual(report['progress']['kind'], 'stopping')

    def test_second_sigterm_uses_identity_without_rechecking_readiness(self) -> None:
        stored = top_level('became-busy', 101)
        busy = top_level('became-busy', 101, status='busy')
        inventories = FreshInventorySequence([machine_inventory([stored]), machine_inventory([busy])])
        self.create(records_for_test([entry(stored)]))
        sent: list[int] = []
        alive = True

        def kill(_pid: int, sent_signal: int) -> None:
            nonlocal alive
            sent.append(sent_signal)
            if len(sent) == 2:
                alive = False
        report = stop.stop_session(self.stored_record(), kill=kill, is_alive=lambda _pid: alive, fresh_inventory=inventories, clock=lambda: NOW_UTC, codex_mesh=lambda _arguments: 0,  sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
        self.assertEqual(sent, [signal.SIGTERM, signal.SIGTERM])
        self.assertEqual(report['progress']['kind'], 'stopped')

    def test_pid_surviving_two_sigterms_is_failed_without_sigkill(self) -> None:
        stored = top_level('stuck', 101)
        self.create(records_for_test([entry(stored)]))
        sent: list[int] = []
        report = stop.stop_session(self.stored_record(), kill=lambda _pid, sent_signal: sent.append(sent_signal), is_alive=lambda _pid: True, fresh_inventory=lambda _login, _scope: machine_inventory([stored]), clock=lambda: NOW_UTC, codex_mesh=lambda _arguments: 0,  sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
        self.assertEqual(sent, [signal.SIGTERM, signal.SIGTERM])
        self.assertNotIn(signal.SIGKILL, sent)
        progress = report['progress']
        self.assertEqual(progress['kind'], 'stop failed')
        if progress['kind'] != 'stop failed':
            self.fail('expected stop failure')
        self.assertEqual(progress['reason'], 'alive after two SIGTERMs')
        self.assertEqual(report['stop_issues'], [shutdown_record.StillRunningAfterStop(kind='still running', at=NOW, reason='alive after two SIGTERMs')])
        self.assertNotEqual(report['progress']['kind'], 'stopping')

    def test_a_new_stop_pass_replaces_an_entrys_previous_issues(self) -> None:
        stored = top_level('stuck', 101)
        stored_entry = entry(stored)
        stored_entry['stop_issues'] = [shutdown_record.AccountUnreadableAtStop(kind='account unreadable', at=NOW)]
        self.create(records_for_test([stored_entry], state='stopping'))
        report = stop.stop_session(self.stored_record(), kill=lambda _pid, _signal: None, is_alive=lambda _pid: True, fresh_inventory=lambda _login, _scope: machine_inventory([stored]), clock=lambda: NOW_UTC, codex_mesh=lambda _arguments: 0,  sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
        self.assertEqual(report['stop_issues'], [shutdown_record.StillRunningAfterStop(kind='still running', at=NOW, reason='alive after two SIGTERMs')])

    def test_showrunner_codex_server_is_stopped_once_after_exit(self) -> None:
        run_dir = str(self.root / 'showrunner-run')
        stored = showrunner('runner', 101, run_dirs=[run_dir], codex_servers=[server(run_dir, 900)])
        self.create(records_for_test([entry(stored)]))
        codex_calls: list[list[str]] = []
        report = stop.stop_session(self.stored_record(), kill=lambda _pid, _signal: None, is_alive=lambda _pid: False, fresh_inventory=lambda _login, _scope: machine_inventory([stored]), clock=lambda: NOW_UTC, codex_mesh=lambda arguments: codex_calls.append(arguments) or 0, sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
        self.assertEqual(codex_calls, [['stop', '--pause', '--session-dir', run_dir]])
        self.assertEqual(report['progress'], shutdown_record.SessionStopped(kind='stopped', at=NOW))
        self.assertEqual(report['stop_issues'], [])

    def test_unconfirmed_codex_server_and_unit_herdr_leave_stop_partial(self) -> None:
        self.herdr_replies(["unit-exact"])
        run_dir = str(self.root / 'unit-run')
        stored = unit('unit', 101, session_name='unit-exact', run_dirs=[run_dir])
        self.create(records_for_test([entry(stored)]))
        report = stop.stop_session(self.stored_record(), kill=lambda _pid, _signal: None, is_alive=lambda _pid: False, fresh_inventory=lambda _login, _scope: machine_inventory([stored]), clock=lambda: NOW_UTC, codex_mesh=lambda _arguments: 1,  sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
        self.assertEqual(report['progress'], shutdown_record.SessionStopped(kind='stopped', at=NOW))
        self.assertEqual(report['stop_issues'], [shutdown_record.CodexServerLeftRunning(kind='codex server left running', at=NOW, run_dir=run_dir, cause='stop not confirmed'), shutdown_record.UnitSessionLeftRunning(kind='unit session left running', at=NOW, host="herdr", session_name='unit-exact', cause='stop not confirmed')])

    def test_second_stop_retries_only_the_cleanup_left_by_the_first_stop(self) -> None:
        runs = [str(self.root / name) for name in ("resolved", "pending")]
        self.herdr_replies(["herdr-owner"])
        saved = entry(unit("owner", 101, run_dirs=runs))
        self.create([saved])
        first_codex: list[list[str]] = []

        def codex(args: list[str]) -> int:
            first_codex.append(args)
            return 1 if args[-1] == runs[1] else 0

        first = stop.stop_session(
            saved, kill=lambda _pid, _sent: None, is_alive=lambda _pid: False,
            session_record_exists=lambda _pid: False,
            fresh_inventory=lambda _login, _only: machine_inventory([saved["session"]]),
            clock=lambda: NOW_UTC, codex_mesh=codex,
        )
        expected_issues = [
            shutdown_record.CodexServerLeftRunning(kind="codex server left running", at=NOW, run_dir=runs[1], cause="stop not confirmed"),
            shutdown_record.UnitSessionLeftRunning(kind="unit session left running", at=NOW, host="herdr", session_name="herdr-owner", cause="stop not confirmed"),
        ]
        self.assertEqual(first["progress"], shutdown_record.SessionStopped(kind="stopped", at=NOW))
        self.assertEqual(first["stop_issues"], expected_issues)
        self.assertEqual(first_codex, [["stop", "--pause", "--session-dir", run] for run in runs])
        calls: list[list[str]] = []
        self.herdr_replies([])
        second_inventory = FreshInventorySequence([machine_inventory([])])
        second = stop.stop_session(
            first, kill=lambda _pid, _sent: self.fail("a stopped owner was signalled"),
            is_alive=lambda _pid: False,
            fresh_inventory=second_inventory,
            clock=lambda: NOW_UTC + timedelta(seconds=1),
            codex_mesh=lambda args: calls.append(args) or 0,

        )
        # Admission, both sides of capture, pause, host cleanup and completion.
        self.assertEqual(second_inventory.calls, 6)
        self.assertEqual(calls, [["stop", "--pause", "--session-dir", runs[1]]])
        self.assertEqual(second["progress"], first["progress"])
        self.assertEqual(second["stop_issues"], [])

    def test_cleanup_retry_does_not_adopt_a_resumed_owner(self) -> None:
        for prior in (shutdown_record.SessionStopped(kind="stopped", at=NOW),
                      shutdown_record.SessionStopFailed(kind="stop failed", at=NOW, reason="cleanup refused")):
            with self.subTest(progress=prior["kind"]):
                saved = entry(unit("retry", 101, session_name="unit-host", session_host="herdr",
                    run_dirs=[str(self.root / "pending")]))
                saved["progress"] = prior
                saved["stop_issues"] = [shutdown_record.UnitSessionLeftRunning(kind="unit session left running",
                    at=NOW, host="herdr", session_name="unit-host", cause="stop not confirmed")]
                self.create([saved])
                resumed = copy.deepcopy(saved["session"])
                resumed.update(pid=202, proc_start="new process")
                with patch.object(herdr, "stop_session") as stop_host:
                    report = stop.stop_session(saved,
                        kill=lambda _pid, _sent: self.fail("resumed owner signalled"),
                        fresh_inventory=lambda _login, _only: machine_inventory([resumed]),
                        clock=lambda: NOW_UTC,
                        codex_mesh=lambda _args: self.fail("resumed worker cleanup attempted"))
                stop_host.assert_not_called()
                self.assertEqual(report["progress"]["kind"], "process identity lost")
                self.assertEqual(report["session"], saved["session"])

    def test_resume_during_worker_capture_prevents_legacy_pause_cleanup_and_signals(self) -> None:
        saved = entry(unit("owner", 101, session_name="unit-host", run_dirs=[str(self.root / "run")]))
        self.create([saved])
        resumed = copy.deepcopy(saved["session"])
        resumed.update(pid=202, proc_start="resumed start")
        captured = False
        def capture(_session: inventory.Session) -> list[shutdown_record.PausedSeat]:
            nonlocal captured
            captured = True
            return []
        with (patch.object(stop, "_capture_codex_seats", side_effect=capture),
              patch.object(herdr, "stop_session") as stop_host):
            result = stop.stop_session(saved,
                kill=lambda _pid, _sent: self.fail("resumed generation reached a signal"),
                is_alive=lambda _pid: False,
                fresh_inventory=lambda _login, _only: machine_inventory([resumed if captured else saved["session"]]),
                clock=lambda: NOW_UTC,
                codex_mesh=lambda _args: self.fail("resumed generation reached worker pause"))
        stop_host.assert_not_called()
        self.assertTrue(captured)
        self.assertEqual(result["progress"]["kind"], "process identity lost")
        self.assertEqual(result["session"], saved["session"])

    def test_deleted_server_record_does_not_hide_a_surviving_server_pid(self) -> None:
        run_dir = self.root / 'live-server'
        run_dir.mkdir()
        server_file = run_dir / 'mesh_server.json'
        _ = server_file.write_text(json.dumps({'pid': 900}), encoding='utf-8')
        stored = showrunner('runner', 101, run_dirs=[str(run_dir)])
        self.create(records_for_test([entry(stored)]))

        def codex(arguments: list[str]) -> int:
            self.assertEqual(arguments, ['stop', '--pause', '--session-dir', str(run_dir)])
            server_file.unlink()
            return 0
        report = stop.stop_session(self.stored_record(), kill=lambda _pid, _signal: None, is_alive=lambda pid: pid == 900, fresh_inventory=lambda _login, _scope: machine_inventory([stored]), clock=lambda: NOW_UTC, codex_mesh=codex,  sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
        self.assertNotEqual(report['progress']['kind'], 'stopping')
        self.assertEqual(report['stop_issues'], [shutdown_record.CodexServerLeftRunning(kind='codex server left running', at=NOW, run_dir=str(run_dir), cause='stop not confirmed')])


    def test_ghostty_shell_with_a_different_parent_gets_no_sighup(self) -> None:
        host = inventory.WindowHost(kind='ghostty', window_shell=901, desktop=inventory.DesktopNotInSnapshot(kind='not in snapshot'))
        stored = top_level('window', 101, host=host)
        self.create(records_for_test([entry(stored)]))
        signals: list[tuple[int, int]] = []
        ps_rows: list[subprocess.CompletedProcess[str]] = [subprocess.CompletedProcess(args=['ps'], returncode=0, stdout='801 zsh\n', stderr=''), subprocess.CompletedProcess(args=['ps'], returncode=0, stdout='1 bash\n', stderr='')]
        with patch('stop.subprocess.run', side_effect=ps_rows) as ps:
            report = stop.stop_session(self.stored_record(), kill=lambda pid, sent_signal: signals.append((pid, sent_signal)), is_alive=lambda pid: pid == 901, fresh_inventory=lambda _login, _scope: machine_inventory([stored]), clock=lambda: NOW_UTC, codex_mesh=lambda _arguments: 0,  sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
        self.assertEqual(signals, [(101, signal.SIGTERM)])
        self.assertEqual(ps.call_count, 2)
        self.assertNotEqual(report['progress']['kind'], 'stopping')

    def test_stop_failed_record_can_be_retried_after_the_owner_becomes_idle(self) -> None:
        saved = entry(top_level("owner", 101))
        saved["progress"] = shutdown_record.SessionStopFailed(kind="stop failed", at=NOW, reason="busy")
        self.create([saved])
        self.assertEqual(self.perform(saved)["progress"]["kind"], "stopped")

    def test_interrupted_cleanup_is_retried_when_the_claim_is_stale(self) -> None:
        pending = str(self.root / "pending")
        saved = entry(unit("owner", 101, run_dirs=[pending]))
        self.create([saved])
        with patch.object(herdr, "stop_session", side_effect=RuntimeError("interrupted")), self.assertRaisesRegex(RuntimeError, "interrupted"):
            _ = stop.stop_session(
                saved, kill=lambda _pid, _sent: None, is_alive=lambda _pid: False,
                session_record_exists=lambda _pid: False,
                fresh_inventory=lambda _login, _only: machine_inventory([saved["session"]]),
                clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0,

            )
        interrupted = self.stored_record()
        self.assertEqual(interrupted["progress"]["kind"], "stopping")
        calls: list[list[str]] = []
        finished = stop.stop_session(
            interrupted, kill=lambda _pid, _sent: self.fail("an absent owner was signalled"),
            is_alive=lambda _pid: False, session_record_exists=lambda _pid: False,
            fresh_inventory=lambda _login, _only: machine_inventory([]),
            clock=lambda: NOW_UTC + timedelta(seconds=120),
            codex_mesh=lambda args: calls.append(args) or 0,

        )
        self.assertEqual(calls, [["stop", "--pause", "--session-dir", pending]])
        self.assertEqual(finished["progress"]["kind"], "already gone")

    def test_process_disappearing_at_first_signal_is_already_gone(self) -> None:
        saved = entry(top_level("owner", 101))
        self.create([saved])
        def kill(_pid: int, _sent: int) -> None:
            raise ProcessLookupError()
        finished = stop.stop_session(
            saved, kill=kill, is_alive=lambda _pid: False,
            fresh_inventory=lambda _login, _only: machine_inventory([saved["session"]]),
            clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0,
        )
        self.assertEqual(finished["progress"]["kind"], "already gone")

    def test_signal_permission_failure_keeps_a_named_failure_and_no_cleanup(self) -> None:
        saved = entry(unit("owner", 101))
        self.create([saved])
        def kill(_pid: int, _sent: int) -> None:
            raise PermissionError("signal refused")
        finished = stop.stop_session(
            saved, kill=kill, fresh_inventory=lambda _login, _only: machine_inventory([saved["session"]]),
            clock=lambda: NOW_UTC,
            codex_mesh=lambda _args: self.fail("cleanup followed a refused signal"),

        )
        self.assertEqual(finished["progress"], {"kind": "stop failed", "at": NOW, "reason": "signal refused"})

    def test_account_becoming_unreadable_before_second_signal_is_not_signalled_again(self) -> None:
        saved = entry(top_level("owner", 101))
        self.create([saved])
        report = FreshInventorySequence([
            machine_inventory([saved["session"]]),
            machine_inventory([], unattributed=[inventory.UnattributedSession(pid=101, name="owner", reason="account unreadable")]),
        ])
        signals: list[int] = []
        finished = stop.stop_session(
            saved, kill=lambda _pid, sent: signals.append(sent), is_alive=lambda _pid: True,
            session_record_exists=lambda _pid: False, fresh_inventory=report,
            clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0,
            sleep=lambda _seconds: None,
        )
        self.assertEqual(signals, [signal.SIGTERM])
        self.assertEqual(finished["progress"]["kind"], "stop failed")
        self.assertEqual(finished["stop_issues"], [{"kind": "account unreadable", "at": NOW}])

    def test_a_matching_ghostty_parent_closes_only_the_recorded_shell(self) -> None:
        saved = entry(top_level("owner", 101, host=inventory.WindowHost(
            kind="ghostty", window_shell=901, desktop=inventory.DesktopNotInSnapshot(kind="not in snapshot"),
        )))
        self.create([saved])
        signals: list[tuple[int, int]] = []
        with patch.object(stop, "_ghostty_still_owns_shell", return_value=True):
            finished = stop.stop_session(
                saved, kill=lambda pid, sent: signals.append((pid, sent)), is_alive=lambda pid: pid == 901,
                session_record_exists=lambda _pid: False,
                fresh_inventory=lambda _login, _only: machine_inventory([saved["session"]]),
                clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0,
                sleep=lambda _seconds: None,
            )
        self.assertEqual(signals, [(101, signal.SIGTERM), (901, signal.SIGHUP)])
        self.assertEqual(finished["progress"]["kind"], "stopped")

    def test_supervised_ghostty_restart_holds_window_before_claude_sigterm(self) -> None:
        owner = top_level("supervised-owner", 101, host=inventory.WindowHost(
            kind="ghostty", window_shell=901,
            desktop=inventory.NamedDesktop(kind="named", name="Production"),
            supervisor=inventory.SupervisorIdentity(kind="supervisor", pid=902, proc_start="supervisor-start")))
        saved = entry(owner)
        saved["then"] = "restart"
        self.create([saved])
        hold = self.root / "shutdown/relaunch/902.hold"
        signals: list[tuple[int, int]] = []
        def kill(pid: int, sent: int) -> None:
            self.assertTrue(hold.is_file(), "supervisor could exit before the hold is written")
            signals.append((pid, sent))
        with (patch.object(inventory, "_process_start", return_value="supervisor-start"),
              patch.object(stop, "_ghostty_still_owns_shell", return_value=True)):
            finished = stop.stop_session(saved, kill=kill, is_alive=lambda pid: pid in {901, 902},
                session_record_exists=lambda _pid: False,
                fresh_inventory=lambda _login, _only: machine_inventory([owner]),
                clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0,
                sleep=lambda _seconds: None)
        self.assertEqual(signals, [(101, signal.SIGTERM)])
        self.assertEqual(finished["progress"]["kind"], "stopped")
        self.assertTrue(hold.exists())

    def test_mac_supervised_terminal_restart_writes_hold_before_claude_sigterm(self) -> None:
        owner = top_level("mac-supervised-owner", 101, host=inventory.TerminalHost(
            kind="terminal", supervisor=inventory.SupervisorIdentity(
                kind="supervisor", pid=902, proc_start="supervisor-start")))
        saved = entry(owner)
        saved["then"] = "restart"
        self.create([saved])
        hold = self.root / "shutdown/relaunch/902.hold"
        signals: list[tuple[int, int]] = []
        def kill(pid: int, sent: int) -> None:
            self.assertTrue(hold.is_file(), "hold must precede the Claude signal")
            signals.append((pid, sent))
        with (patch.object(sys, "platform", "darwin"),
              patch.object(inventory, "_process_start", return_value="supervisor-start")):
            finished = stop.stop_session(saved, kill=kill, is_alive=lambda pid: pid == 902,
                session_record_exists=lambda _pid: False,
                fresh_inventory=lambda _login, _only: machine_inventory([owner]),
                clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0,
                sleep=lambda _seconds: None)
        self.assertEqual(signals, [(101, signal.SIGTERM)])
        self.assertEqual(finished["progress"]["kind"], "stopped")
        self.assertTrue(hold.exists())

    def test_unwritable_supervisor_hold_fails_the_stop_without_signalling_claude(self) -> None:
        owner = top_level("unwritable-hold-owner", 101, host=inventory.WindowHost(
            kind="ghostty", window_shell=901,
            desktop=inventory.NamedDesktop(kind="named", name="Production"),
            supervisor=inventory.SupervisorIdentity(kind="supervisor", pid=902, proc_start="supervisor-start")))
        saved = entry(owner)
        saved["then"] = "restart"
        self.create([saved])
        signals: list[tuple[int, int]] = []
        with (patch.object(inventory, "_process_start", return_value="supervisor-start"),
              patch.object(relaunch, "write_hold", side_effect=PermissionError("state is read only"))):
            finished = stop.stop_session(saved, kill=lambda pid, sent: signals.append((pid, sent)),
                is_alive=lambda pid: pid in {101, 901, 902},
                session_record_exists=lambda _pid: True,
                fresh_inventory=lambda _login, _only: machine_inventory([owner]),
                clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0,
                sleep=lambda _seconds: None)
        self.assertEqual(signals, [])
        self.assertEqual(finished["progress"]["kind"], "stop failed")

    def test_stop_only_or_unavailable_supervisor_closes_owned_ghostty_shell(self) -> None:
        for then, actual_start, supervised in (("stop", "supervisor-start", True),
                ("restart", "reused-pid", True), ("restart", "", False)):
            with self.subTest(then=then, actual_start=actual_start, supervised=supervised):
                owner = top_level(f"close-{then}-{supervised}", 101, host=inventory.WindowHost(
                    kind="ghostty", window_shell=901,
                    desktop=inventory.DesktopNotInSnapshot(kind="not in snapshot"),
                    supervisor=inventory.SupervisorIdentity(kind="supervisor", pid=902,
                        proc_start="supervisor-start")))
                saved = entry(owner)
                if not supervised:
                    host = owner["host"]
                    assert host["kind"] == "ghostty"
                    _ = host.pop("supervisor", None)
                saved["then"] = "restart" if then == "restart" else "stop"
                self.create([saved])
                signals: list[tuple[int, int]] = []
                with (patch.object(inventory, "_process_start", return_value=actual_start),
                      patch.object(stop, "_ghostty_still_owns_shell", return_value=True)):
                    finished = stop.stop_session(saved,
                        kill=lambda pid, sent: signals.append((pid, sent)),
                        is_alive=lambda pid: pid in {901, 902}, session_record_exists=lambda _pid: False,
                        fresh_inventory=lambda _login, _only: machine_inventory([owner]),
                        clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0,
                        sleep=lambda _seconds: None)
                self.assertEqual(signals, [(101, signal.SIGTERM), (901, signal.SIGHUP)])
                self.assertEqual(finished["progress"]["kind"], "stopped")
                self.assertFalse((self.root / "shutdown/relaunch/902.hold").exists())


    def test_moved_ghostty_showrunner_closes_its_recorded_shell_when_stopped(self) -> None:
        owner = showrunner("hana-id", 101)
        owner["host"] = inventory.WindowHost(kind="ghostty", window_shell=901,
            desktop=inventory.NamedDesktop(kind="named", name="Production"))
        saved = entry(owner)
        saved["target"] = "claude3"
        self.create([saved])
        signals: list[tuple[int, int]] = []
        with patch.object(stop, "_ghostty_still_owns_shell", return_value=True):
            finished = stop.stop_session(
                saved, kill=lambda pid, sent: signals.append((pid, sent)), is_alive=lambda pid: pid == 901,
                session_record_exists=lambda _pid: False,
                fresh_inventory=lambda _login, _only: machine_inventory([saved["session"]]),
                clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0,
                sleep=lambda _seconds: None,
            )
        self.assertEqual(signals, [(101, signal.SIGTERM), (901, signal.SIGHUP)])
        self.assertEqual(finished["progress"]["kind"], "stopped")

    def test_stop_snapshot_cannot_claim_a_new_request_for_the_same_session(self) -> None:
        old = entry(top_level("owner", 101))
        self.create([old])
        shutdown_record.archive_session(LOGIN, "owner")
        current = entry(top_level("owner", 101))
        current["request_id"] = "new-request"
        self.create([current])
        signals: list[int] = []
        result = stop.stop_session(
            old, kill=lambda _pid, sent: signals.append(sent),
            is_alive=lambda _pid: False, session_record_exists=lambda _pid: False,
            fresh_inventory=lambda _login, _only: machine_inventory([current["session"]]),
            clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0,
             sleep=lambda _seconds: None,
        )
        self.assertEqual(signals, [])
        self.assertEqual(result, current)
        self.assertEqual(self.stored_record(), current)

    def test_late_stop_outcome_leaves_a_replacement_relaunch_claim_unchanged(self) -> None:
        saved = entry(top_level("owner", 101))
        self.create([saved])
        replacement = shutdown_record.SessionRestarting(
            kind="restarting", at=(NOW_UTC + timedelta(seconds=121)).isoformat(),
            prior=shutdown_record.SessionStopped(kind="stopped", at=NOW),
        )
        real_update = shutdown_record.update_session
        updates = 0

        def update(login: str, session_id: str, change: Callable[[shutdown_record.SessionShutdown], None]) -> shutdown_record.SessionShutdown:
            nonlocal updates
            updates += 1
            if updates == 2:
                def replace(current: shutdown_record.SessionShutdown) -> None:
                    current["progress"] = replacement
                _ = real_update(login, session_id, replace)
            return real_update(login, session_id, change)

        changed = top_level("owner", 202)
        with patch.object(stop, "update_session", update):
            finished = stop.stop_session(
                saved, kill=lambda _pid, _sent: self.fail("a replaced owner was signalled"),
                fresh_inventory=lambda _login, _only: machine_inventory([changed]),
                clock=lambda: NOW_UTC,
                codex_mesh=lambda _args: self.fail("a replaced owner was cleaned"),

            )
        self.assertEqual(finished["progress"], replacement)
        self.assertEqual(finished["stop_issues"], [])
        self.assertEqual(self.stored_record(), finished)

    def test_late_stop_cleanup_leaves_a_replacement_request_untouched(self) -> None:
        saved = entry(unit("owner", 101, run_dirs=[str(self.root / "run")]))
        self.create([saved])

        def report(_login: str, _only: frozenset[str]) -> inventory.Inventory:
            def replace(current: shutdown_record.SessionShutdown) -> None:
                current["request_id"] = "replacement"
                current["progress"] = shutdown_record.SessionReadyToStop(kind="ready", at=NOW)
            _ = shutdown_record.update_session(LOGIN, "owner", replace)
            return machine_inventory([])

        finished = stop.stop_session(
            saved, kill=lambda _pid, _sent: self.fail("a replaced owner was signalled"),
            fresh_inventory=report, clock=lambda: NOW_UTC,
            codex_mesh=lambda _args: self.fail("stale Codex cleanup ran"),

        )
        self.assertEqual(finished["request_id"], "replacement")
        self.assertEqual(finished["progress"], {"kind": "ready", "at": NOW})
        self.assertEqual(finished["stop_issues"], [])

    def test_expired_stop_worker_returns_the_replacement_workers_relaunch(self) -> None:
        saved = entry(unit("owner", 101, run_dirs=[str(self.root / "run")]))
        self.create([saved])
        paused = threading.Event()
        release = threading.Event()
        returned: list[shutdown_record.SessionShutdown] = []
        errors: list[BaseException] = []

        def report(_login: str, _only: frozenset[str]) -> inventory.Inventory:
            paused.set()
            if not release.wait(HANG_GUARD_SECONDS):
                raise RuntimeError("stop worker was not released")
            return machine_inventory([])

        def first_worker() -> None:
            try:
                returned.append(stop.stop_session(
                    saved, kill=lambda _pid, _sent: self.fail("expired worker sent a signal"),
                    fresh_inventory=report, clock=lambda: NOW_UTC,
                    codex_mesh=lambda _args: self.fail("expired worker cleaned a server"),

                ))
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=first_worker)
        worker.start()
        try:
            self.assertTrue(paused.wait(HANG_GUARD_SECONDS))
            replacement_time = NOW_UTC + timedelta(seconds=shutdown_record.CLAIM_STALE_SECONDS)
            stopped = stop.stop_session(
                self.stored_record(), kill=lambda _pid, _sent: None,
                is_alive=lambda _pid: False, session_record_exists=lambda _pid: False,
                fresh_inventory=lambda _login, _only: machine_inventory([saved["session"]]),
                clock=lambda: replacement_time, codex_mesh=lambda _args: 0,

            )
            self.assertEqual(stopped["progress"]["kind"], "stopped")
            prior = shutdown_record.SessionStopped(kind="stopped", at=replacement_time.isoformat())

            def relaunch(current: shutdown_record.SessionShutdown) -> None:
                current["progress"] = shutdown_record.SessionRestarting(kind="restarting", at=replacement_time.isoformat(), prior=prior)

            replacement = shutdown_record.update_session(LOGIN, "owner", relaunch)
        finally:
            release.set()
            worker.join(HANG_GUARD_SECONDS)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(returned, [replacement])
        self.assertEqual(self.stored_record(), replacement)

    def test_stop_cleanup_rechecks_ownership_between_codex_servers_and_herdr(self) -> None:
        runs = [str(self.root / name) for name in ("first", "second")]
        self.herdr_replies(["herdr-owner"])
        saved = entry(unit("owner", 101, run_dirs=runs))
        saved["progress"] = shutdown_record.SessionStopped(kind="stopped", at=NOW)
        issues: list[shutdown_record.SessionStopIssue] = [
            shutdown_record.CodexServerLeftRunning(kind="codex server left running", at=NOW, run_dir=run, cause="stop not confirmed")
            for run in runs
        ]
        issues.append(shutdown_record.UnitSessionLeftRunning(kind="unit session left running", at=NOW, host="herdr", session_name="herdr-owner", cause="stop not confirmed"))
        saved["stop_issues"] = issues
        self.create([saved])
        calls: list[list[str]] = []
        replacement = shutdown_record.SessionRestarting(kind="restarting", at=NOW, prior=shutdown_record.SessionStopped(kind="stopped", at=NOW))

        def codex(args: list[str]) -> int:
            calls.append(args)
            def replace(current: shutdown_record.SessionShutdown) -> None:
                current["progress"] = replacement
            _ = shutdown_record.update_session(LOGIN, "owner", replace)
            return 0

        finished = stop.stop_session(
            saved, kill=lambda _pid, _sent: self.fail("a stopped owner was signalled"),
            is_alive=lambda _pid: False, clock=lambda: NOW_UTC, codex_mesh=codex,

        )
        self.assertEqual(calls, [["stop", "--pause", "--session-dir", runs[0]]])
        self.assertEqual(finished["progress"], replacement)
        self.assertEqual(finished["stop_issues"], saved["stop_issues"])

    def test_stop_rechecks_ownership_after_readiness_before_the_first_signal(self) -> None:
        saved = entry(top_level("owner", 101))
        self.create([saved])
        replacement = shutdown_record.SessionRestarting(kind="restarting", at=NOW, prior=shutdown_record.SessionStopped(kind="stopped", at=NOW))

        def blocker(_session: inventory.Session, *, moving: bool = False) -> shutdown_record.ReadinessCheck:
            _ = moving
            def replace(current: shutdown_record.SessionShutdown) -> None:
                current["progress"] = replacement
            _ = shutdown_record.update_session(LOGIN, "owner", replace)
            return shutdown_record.ReadinessClear(kind="clear")

        with patch.object(settle, "blocker", blocker):
            finished = stop.stop_session(
                saved, kill=lambda _pid, _sent: self.fail("stale first signal ran"),
                fresh_inventory=lambda _login, _only: machine_inventory([saved["session"]]),
                clock=lambda: NOW_UTC,
                codex_mesh=lambda _args: self.fail("stale cleanup ran"),

            )
        self.assertEqual(finished["progress"], replacement)

    def test_stop_rechecks_ownership_before_the_second_signal(self) -> None:
        saved = entry(top_level("owner", 101))
        self.create([saved])
        replacement = shutdown_record.SessionRestarting(kind="restarting", at=NOW, prior=shutdown_record.SessionStopped(kind="stopped", at=NOW))
        replaced = False
        signals: list[int] = []

        def report(_login: str, _only: frozenset[str]) -> inventory.Inventory:
            nonlocal replaced
            if signals and not replaced:
                replaced = True
                def replace(current: shutdown_record.SessionShutdown) -> None:
                    current["progress"] = replacement
                _ = shutdown_record.update_session(LOGIN, "owner", replace)
            return machine_inventory([saved["session"]])

        finished = stop.stop_session(
            saved, kill=lambda _pid, sent: signals.append(sent), is_alive=lambda _pid: True,
            session_record_exists=lambda _pid: False, fresh_inventory=report,
            clock=lambda: NOW_UTC, sleep=lambda _seconds: None,
            codex_mesh=lambda _args: self.fail("stale cleanup ran"),

        )
        self.assertEqual(signals, [signal.SIGTERM])
        self.assertEqual(finished["progress"], replacement)

    def test_stop_rechecks_ownership_after_pausing_codex_run(self) -> None:
        run = str(self.root / "run")
        saved = entry(unit("owner", 101, codex_servers=[server(run, 900, busy_workers=["one", "two"])]))
        saved["force"] = "now"
        self.create([saved])
        replacement = shutdown_record.SessionRestarting(kind="restarting", at=NOW, prior=shutdown_record.SessionStopped(kind="stopped", at=NOW))
        calls: list[list[str]] = []

        def codex(args: list[str]) -> int:
            calls.append(args)
            def replace(current: shutdown_record.SessionShutdown) -> None:
                current["progress"] = replacement
            _ = shutdown_record.update_session(LOGIN, "owner", replace)
            return 0

        finished = stop.stop_session(
            saved, kill=lambda _pid, _sent: None, is_alive=lambda _pid: False,
            session_record_exists=lambda _pid: False, fresh_inventory=lambda _login, _only: machine_inventory([saved["session"]]),
            clock=lambda: NOW_UTC, codex_mesh=codex,
        )
        self.assertEqual(calls, [["stop", "--pause", "--session-dir", run]])
        self.assertEqual(finished["progress"], replacement)

    def test_stop_cleanup_rechecks_ownership_before_the_ghostty_signal(self) -> None:
        host = inventory.WindowHost(kind="ghostty", window_shell=901, desktop=inventory.DesktopNotInSnapshot(kind="not in snapshot"))
        saved = entry(top_level("owner", 101, host=host))
        self.create([saved])
        replacement = shutdown_record.SessionRestarting(kind="restarting", at=NOW, prior=shutdown_record.SessionStopped(kind="stopped", at=NOW))

        def shell_owner(_pid: int) -> bool:
            def replace(current: shutdown_record.SessionShutdown) -> None:
                current["progress"] = replacement
            _ = shutdown_record.update_session(LOGIN, "owner", replace)
            return True

        signals: list[tuple[int, int]] = []
        with patch.object(stop, "_ghostty_still_owns_shell", shell_owner):
            finished = stop.stop_session(
                saved, kill=lambda pid, sent: signals.append((pid, sent)), is_alive=lambda pid: pid == 901,
                session_record_exists=lambda _pid: False,
                fresh_inventory=lambda _login, _only: machine_inventory([saved["session"]]),
                clock=lambda: NOW_UTC, codex_mesh=lambda _args: self.fail("stale cleanup ran"),
            )
        self.assertEqual(signals, [(101, signal.SIGTERM)])
        self.assertEqual(finished["progress"], replacement)


    def test_stop_timestamps_and_claim_age_use_the_injected_clock(self) -> None:
        saved = entry(unit("owner", 101, run_dirs=[str(self.root / "run")]))
        self.create([saved])
        instant = datetime(2030, 2, 3, 4, 5, 6, 789, tzinfo=timezone(timedelta(hours=2)))
        expected = "2030-02-03T02:05:06+00:00"

        def kill(_pid: int, _sent: int) -> None:
            self.assertEqual(self.stored_record()["progress"], {"kind": "stopping", "at": expected})

        finished = stop.stop_session(
            saved, kill=kill, is_alive=lambda _pid: False,
            session_record_exists=lambda _pid: False,
            fresh_inventory=lambda _login, _only: machine_inventory([saved["session"]]),
            clock=lambda: instant, codex_mesh=lambda _args: 1,
        )
        self.assertEqual(finished["progress"], {"kind": "stopped", "at": expected})
        self.assertEqual([issue["at"] for issue in finished["stop_issues"]], [expected])
        self.assertEqual(finished["stop_issues"][0]["kind"], "codex server left running")


if __name__ == "__main__":
    _ = unittest.main()
