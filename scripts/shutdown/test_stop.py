#!/usr/bin/env python3
"""Behavioral tests for stopping an account's sessions and resources."""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypedDict, cast, final, override
from unittest.mock import patch

import inventory
import record
import remote as remote_transport
import settle
import shutdown
import stop
from account import Account
from record import (
    LiveShutdownRecord,
    PassiveSeatReadyToStop,
    SessionAlreadyGone,
    SessionNeedsManualRestart,
    SessionProgress,
    SessionReadyToStop,
    SessionRestarted,
    SessionRestartFailed,
    SessionStopFailed,
    SessionStopped,
    SessionWaiting,
    SettleMessageNotSent,
    SettleMessageQueued,
    SettleMessageSent,
    ShutdownRecord,
    ShutdownScope,
    ShutdownSessionEntry,
    ShutdownState,
    StopTiming,
    WhereNotSaid,
    WhereSaid,
)


LOGIN = "owner@example.com"
LABEL = "claude 2"
NOW = "2026-10-09T21:49:10+00:00"
NOW_UTC = datetime(2026, 10, 9, 21, 49, 10, tzinfo=timezone.utc)


class AlertCall(TypedDict):
    recipient: str
    summary: str
    text: str
    need: str
    machine: str


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
        self, _login: str, _scope: ShutdownScope
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
        "name": name,
        "cwd": f"/tmp/{session_id}",
        "status": status,
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
        host=inventory.TmuxHost(kind="tmux", tmux_session=f"tmux-{session_id}"),
        production="demo",
        doc="/tmp/demo.md",
    )


def unit(
    session_id: str,
    pid: int,
    *,
    tmux_session: str | None = None,
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
        host=inventory.UnitHost(
            kind="unit",
            production="demo",
            unit=session_id,
            doc="/tmp/demo.md",
            tmux_session=tmux_session or f"tmux-{session_id}",
            plan=inventory.NoRunRecord(kind="no run record"),
        ),
    )


def seat(
    session_id: str,
    pid: int,
    *,
    owner: str | None,
) -> inventory.SeatSession:
    seat_owner: inventory.SeatOwner = (
        inventory.DirectorOwner(kind="director", session_id=owner)
        if owner is not None
        else inventory.NoDirector(kind="no director")
    )
    return inventory.SeatSession(
        **common_session(session_id, session_id, pid),
        kind="seat",
        host=inventory.UnknownHost(kind="unknown"),
        owner=seat_owner,
    )


def server(
    run_dir: str, pid: int, *, busy_seats: list[str] | None = None
) -> inventory.CodexServer:
    return inventory.CodexServer(
        run_dir=run_dir, pid=pid, busy_seats=list(busy_seats or [])
    )


def entry(
    session: inventory.Session,
    progress: Literal["waiting", "ready", "passive seat ready"] = "ready",
) -> ShutdownSessionEntry:
    if progress == "waiting":
        session_progress: SessionProgress = SessionWaiting(kind="waiting")
    elif progress == "passive seat ready":
        session_progress = PassiveSeatReadyToStop(
            kind="passive seat ready", at=NOW
        )
    else:
        session_progress = SessionReadyToStop(kind="ready", at=NOW)
    return ShutdownSessionEntry(
        session=session,
        timers=[],
        settle_message=SettleMessageNotSent(kind="not sent"),
        where=WhereNotSaid(kind="not said"),
        progress=session_progress,
        stop_issues=[],
    )


def shutdown_record(
    entries: list[ShutdownSessionEntry],
    *,
    machine: str = "natedev",
    state: ShutdownState = "stopping",
    force: StopTiming = "wait for ready",
    requester: str | None = None,
) -> ShutdownRecord:
    requested_by: record.RequestOrigin = (
        record.FromSession(kind="session", session_id=requester)
        if requester is not None
        else record.FromTerminal(kind="terminal")
    )
    return ShutdownRecord(
        login=LOGIN,
        label=LABEL,
        machine=machine,
        state=state,
        requested_at=NOW,
        requested_by=requested_by,
        scope=record.AllAccountSessions(kind="all account sessions"),
        conductor=record.ConductorNotStarted(kind="not started"),
        force=force,
        entries=entries,
        stop_issues=[],
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


def refresh_report(current: ShutdownRecord) -> settle.RefreshReport:
    return settle.RefreshReport(record=current, verdicts=[])


def stop_report(
    current: ShutdownRecord,
    *,
    unattributed: list[inventory.UnattributedSession] | None = None,
) -> stop.StopReport:
    counts: dict[str, int] = {}
    for entry in current["entries"]:
        kind = entry["session"]["kind"]
        counts[kind] = counts.get(kind, 0) + 1
    return stop.StopReport(
        record=current,
        unattributed=list(unattributed or []),
        counts=counts,
    )


@final
class StopTests(unittest.TestCase):
    root: Path = Path()
    state_root: Path = Path()
    account = Account("claude", LOGIN, LABEL)

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state_root = self.root / "shutdown"
        environment_context: object = cast(
            object,
            self.enterContext(
                patch.dict(
                    os.environ,
                    {
                        **os.environ,
                        "SHUTDOWN_STATE_DIR": str(self.state_root),
                        "NOTIFIER_STATE_DIR": str(self.root / "notifier"),
                        "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
                        "CONVERSATION_PAUSE_STATE_DIR": str(self.root / "pause"),
                        "PLAN_DELEGATE_HISTORY_DIR": str(self.root / "delegate"),
                        "SHUTDOWN_DELEGATE_ROOT": str(self.root / "delegate"),
                        "AGENT_NOTES_DIR": str(self.root / "notes"),
                    },
                    clear=True,
                )
            ),
        )
        del environment_context

    def stored_record(self) -> ShutdownRecord:
        found = record.find_live(LOGIN)
        self.assertEqual(found["kind"], "live")
        if found["kind"] != "live":
            self.fail("expected a live shutdown record")
        return found["record"]

    def test_phase_six_record_type_names_keep_the_wire_kinds(self) -> None:
        progress: list[SessionProgress] = [
            SessionWaiting(kind="waiting"),
            SessionReadyToStop(kind="ready", at=NOW),
            PassiveSeatReadyToStop(kind="passive seat ready", at=NOW),
            SessionStopped(kind="stopped", at=NOW),
            SessionAlreadyGone(kind="already gone", at=NOW),
            SessionStopFailed(kind="stop failed", at=NOW, reason="still alive"),
            SessionRestarted(kind="restarted", at=NOW),
            SessionRestartFailed(
                kind="restart failed", at=NOW, reason="could not launch"
            ),
            SessionNeedsManualRestart(kind="manual restart", command="resume"),
        ]
        messages = [
            SettleMessageNotSent(kind="not sent"),
            SettleMessageSent(kind="sent", at=NOW),
            SettleMessageQueued(kind="queued", at=NOW, reason="offline"),
        ]
        where = [
            WhereNotSaid(kind="not said"),
            WhereSaid(kind="said", text="tmux", at=NOW),
        ]
        states: list[ShutdownState] = ["settling", "stopping", "down"]
        scope: ShutdownScope = record.AllAccountSessions(
            kind="all account sessions"
        )
        timing: StopTiming = "wait for ready"
        shutdown = shutdown_record([])
        live = LiveShutdownRecord(kind="live", record=shutdown)

        self.assertEqual(
            [item["kind"] for item in progress],
            [
                "waiting",
                "ready",
                "passive seat ready",
                "stopped",
                "already gone",
                "stop failed",
                "restarted",
                "restart failed",
                "manual restart",
            ],
        )
        self.assertEqual([item["kind"] for item in messages], ["not sent", "sent", "queued"])
        self.assertEqual([item["kind"] for item in where], ["not said", "said"])
        self.assertEqual(states, ["settling", "stopping", "down"])
        self.assertEqual(scope["kind"], "all account sessions")
        self.assertEqual(timing, "wait for ready")
        self.assertIs(live["record"], shutdown)

    def test_public_stop_report_parser_round_trips_the_wire_shape(self) -> None:
        expected = stop_report(shutdown_record([], state="down"))

        parsed = stop.parse_stop_report(json.dumps(expected))

        self.assertEqual(parsed, expected)

    def test_second_stop_waits_for_the_first_then_returns_without_effects(
        self,
    ) -> None:
        stored = top_level("owner", 101)
        record.create(shutdown_record([entry(stored)]))
        first_signalled = threading.Event()
        release_first = threading.Event()
        second_finished = threading.Event()
        first_reports: list[stop.StopReport] = []
        second_reports: list[stop.StopReport] = []
        thread_errors: list[BaseException] = []

        def blocking_kill(_pid: int, _signal: int) -> None:
            first_signalled.set()
            if not release_first.wait(5):
                raise AssertionError("first stop was not released")

        def run_first() -> None:
            try:
                first_reports.append(
                    stop.stop(
                        LOGIN,
                        kill=blocking_kill,
                        is_alive=lambda _pid: False,
                        fresh_inventory=lambda _login, _scope: machine_inventory(
                            [stored]
                        ),
                        clock=lambda: NOW_UTC,
                        codex_mesh=lambda _arguments: 0,
                        tmux=lambda _arguments: 1,
                        sleep=lambda _seconds: None,
                    )
                )
            except BaseException as error:
                thread_errors.append(error)

        def unexpected_effect(_value: object) -> int:
            raise AssertionError("the completed stop must not run another effect")

        def unexpected_kill(_pid: int, _signal: int) -> None:
            raise AssertionError("the completed stop must not signal a process")

        def run_second() -> None:
            try:
                second_reports.append(
                    stop.stop(
                        LOGIN,
                        kill=unexpected_kill,
                        is_alive=lambda _pid: False,
                        fresh_inventory=lambda _login, _scope: machine_inventory(
                            []
                        ),
                        clock=lambda: NOW_UTC,
                        codex_mesh=unexpected_effect,
                        tmux=unexpected_effect,
                        sleep=lambda _seconds: None,
                    )
                )
            except BaseException as error:
                thread_errors.append(error)
            finally:
                second_finished.set()

        first = threading.Thread(target=run_first)
        second = threading.Thread(target=run_second)
        first.start()
        self.assertTrue(first_signalled.wait(2))
        second.start()
        self.assertFalse(second_finished.wait(0.1))
        release_first.set()
        first.join(2)
        second.join(2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(thread_errors, [])
        self.assertEqual(len(first_reports), 1)
        self.assertEqual(len(second_reports), 1)
        self.assertEqual(first_reports[0]["record"]["state"], "down")
        self.assertEqual(second_reports[0]["record"]["state"], "down")

    def test_stop_partial_returns_its_report_without_running_effects(self) -> None:
        current = shutdown_record(
            [entry(top_level("left", 101))], state="stop partial"
        )
        current["entries"][0]["stop_issues"] = [
            record.NotReadyToStop(
                kind="not ready to stop",
                at=NOW,
                status="busy",
                progress="ready",
            )
        ]
        record.create(current)

        def unexpected_effect(_value: object) -> int:
            raise AssertionError("a finished stop must not signal or run a command")

        def unexpected_kill(_pid: int, _signal: int) -> None:
            raise AssertionError("a finished stop must not signal a process")

        report = stop.stop(
            LOGIN,
            kill=unexpected_kill,
            is_alive=lambda _pid: False,
            fresh_inventory=lambda _login, _scope: machine_inventory([]),
            clock=lambda: NOW_UTC,
            codex_mesh=unexpected_effect,
            tmux=unexpected_effect,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(report, stop_report(current))

    def test_conductor_claim_only_moves_settling_and_keeps_force(self) -> None:
        states: list[ShutdownState] = [
            "settling",
            "stopping",
            "down",
            "stop partial",
            "restarting",
            "restart partial",
            "cancelled",
            "up",
        ]

        for index, state in enumerate(states):
            with self.subTest(state=state), patch.dict(
                os.environ,
                {"SHUTDOWN_STATE_DIR": str(self.root / f"claim-{index}")},
            ):
                record.create(shutdown_record([], state=state, force="now"))

                claimed = stop.claim_stop_as_conductor(LOGIN)

                self.assertEqual(claimed, state == "settling")
                current = self.stored_record()
                self.assertEqual(
                    current["state"], "stopping" if state == "settling" else state
                )
                self.assertEqual(current["force"], "now")

    def test_peer_claim_replays_stopping_and_only_upgrades_timing(self) -> None:
        record.create(
            shutdown_record([], state="settling", force="wait for ready")
        )

        self.assertTrue(stop.claim_stop_as_peer(LOGIN, "wait for ready"))
        self.assertTrue(stop.claim_stop_as_peer(LOGIN, "now"))
        self.assertTrue(stop.claim_stop_as_peer(LOGIN, "wait for ready"))
        current = self.stored_record()
        self.assertEqual(current["state"], "stopping")
        self.assertEqual(current["force"], "now")

    def test_wait_peer_claim_replays_after_now_upgrade_without_downgrading(
        self,
    ) -> None:
        record.create(
            shutdown_record([], state="settling", force="wait for ready")
        )
        self.assertTrue(stop.claim_stop_as_peer(LOGIN, "wait for ready"))

        def upgrade_to_now(current: ShutdownRecord) -> None:
            current["force"] = "now"

        _ = record.update(LOGIN, upgrade_to_now)

        self.assertTrue(stop.claim_stop_as_peer(LOGIN, "wait for ready"))
        self.assertEqual(self.stored_record()["force"], "now")

    def test_close_failed_stop_closes_only_claimed_or_finished_stops(self) -> None:
        states: list[ShutdownState] = [
            "settling",
            "stopping",
            "down",
            "stop partial",
            "cancelled",
        ]
        issue = record.MachineStopFailed(
            kind="machine stop failed",
            at=NOW,
            machine="natedev",
            reason="inventory failed",
        )

        for index, state in enumerate(states):
            with self.subTest(state=state), patch.dict(
                os.environ,
                {"SHUTDOWN_STATE_DIR": str(self.root / f"close-{index}")},
            ):
                record.create(shutdown_record([], state=state))

                stop.close_failed_stop(LOGIN, issue)

                current = self.stored_record()
                changed = state in {"stopping", "down", "stop partial"}
                self.assertEqual(
                    current["state"], "stop partial" if changed else state
                )
                self.assertEqual(current["stop_issues"], [issue] if changed else [])

    def test_close_failed_stop_deduplicates_a_retried_issue(self) -> None:
        record.create(shutdown_record([], state="stopping"))
        first = record.MachineStopFailed(
            kind="machine stop failed",
            at=NOW,
            machine="Mac",
            reason="ssh reply was lost",
        )
        retried = record.MachineStopFailed(
            kind="machine stop failed",
            at="2026-10-09T21:49:25+00:00",
            machine="Mac",
            reason="ssh reply was lost",
        )

        stop.close_failed_stop(LOGIN, first)
        stop.close_failed_stop(LOGIN, retried)

        current = self.stored_record()
        self.assertEqual(current["state"], "stop partial")
        self.assertEqual(current["stop_issues"], [first])

    def test_conductor_runners_receive_arguments_without_the_executable(
        self,
    ) -> None:
        systemctl_calls: list[list[str]] = []
        launchctl_calls: list[list[str]] = []

        stop.stop_conductor(
            record.SystemdConductor(kind="systemd", unit="shutdown-owner"),
            systemctl=lambda arguments: systemctl_calls.append(arguments) or 5,
            launchctl=lambda arguments: launchctl_calls.append(arguments) or 5,
        )
        stop.stop_conductor(
            record.LaunchdConductor(kind="launchd", label="shutdown.owner"),
            systemctl=lambda arguments: systemctl_calls.append(arguments) or 5,
            launchctl=lambda arguments: launchctl_calls.append(arguments) or 5,
        )

        self.assertEqual(
            systemctl_calls, [["--user", "stop", "shutdown-owner"]]
        )
        self.assertEqual(launchctl_calls, [["remove", "shutdown.owner"]])

    def test_stops_units_then_showrunners_then_top_level_and_requester_last(
        self,
    ) -> None:
        requester = top_level("requester", 104)
        ordinary = top_level("ordinary", 103)
        runner = showrunner("runner", 102)
        director = unit("director", 101)
        sessions: list[inventory.Session] = [requester, ordinary, runner, director]
        record.create(
            shutdown_record(
                [entry(item) for item in sessions], requester="requester"
            )
        )
        killed: list[int] = []

        report = stop.stop(
            LOGIN,
            kill=lambda pid, _signal: killed.append(pid),
            is_alive=lambda _pid: False,
            fresh_inventory=lambda _login, _scope: machine_inventory(sessions),
            clock=lambda: NOW_UTC,
            codex_mesh=lambda _arguments: 0,
            tmux=lambda arguments: 1 if arguments[0] == "has-session" else 0,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(killed, [101, 102, 103, 104])
        self.assertEqual(report["record"]["state"], "down")

    def test_busy_entry_is_refused_without_now(self) -> None:
        stored = top_level("busy", 101)
        current = top_level("busy", 101, status="busy")
        record.create(shutdown_record([entry(stored)]))
        killed: list[int] = []

        report = stop.stop(
            LOGIN,
            kill=lambda pid, _signal: killed.append(pid),
            is_alive=lambda _pid: False,
            fresh_inventory=lambda _login, _scope: machine_inventory([current]),
            clock=lambda: NOW_UTC,
            codex_mesh=lambda _arguments: 0,
            tmux=lambda _arguments: 1,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(killed, [])
        self.assertEqual(report["record"]["state"], "stop partial")
        self.assertEqual(
            report["record"]["entries"][0]["stop_issues"],
            [
                record.NotReadyToStop(
                    kind="not ready to stop",
                    at=NOW,
                    status="busy",
                    progress="ready",
                )
            ],
        )

    def test_account_unreadable_entry_is_not_signalled_and_keeps_an_issue(
        self,
    ) -> None:
        stored = top_level("unreadable", 101)
        record.create(shutdown_record([entry(stored)]))
        killed: list[int] = []
        unattributed = inventory.UnattributedSession(
            pid=101,
            name="unreadable",
            reason="account unreadable",
        )

        report = stop.stop(
            LOGIN,
            kill=lambda pid, _signal: killed.append(pid),
            is_alive=lambda _pid: True,
            fresh_inventory=lambda _login, _scope: machine_inventory(
                [], unattributed=[unattributed]
            ),
            clock=lambda: NOW_UTC,
            codex_mesh=lambda _arguments: 0,
            tmux=lambda _arguments: 1,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(killed, [])
        self.assertEqual(
            report["record"]["entries"][0]["stop_issues"],
            [
                record.AccountUnreadableAtStop(
                    kind="account unreadable", at=NOW
                )
            ],
        )
        self.assertEqual(report["record"]["state"], "stop partial")

    def test_now_ends_busy_codex_seats_before_stopping_their_owner(self) -> None:
        run_dir = str(self.root / "run")
        owned_server = server(run_dir, 900, busy_seats=["impl", "test"])
        stored = top_level(
            "busy",
            101,
            status="busy",
            run_dirs=[run_dir],
            codex_servers=[owned_server],
        )
        record.create(shutdown_record([entry(stored)], force="now"))
        events: list[str] = []

        def codex(arguments: list[str]) -> int:
            events.append("codex " + " ".join(arguments))
            return 0

        report = stop.stop(
            LOGIN,
            kill=lambda pid, sent: events.append(f"kill {pid} {sent}"),
            is_alive=lambda _pid: False,
            fresh_inventory=lambda _login, _scope: machine_inventory([stored]),
            clock=lambda: NOW_UTC,
            codex_mesh=codex,
            tmux=lambda _arguments: 1,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(
            events[:3],
            [
                f"codex end --session-dir {run_dir} --to impl",
                f"codex end --session-dir {run_dir} --to test",
                f"kill 101 {signal.SIGTERM}",
            ],
        )
        self.assertEqual(report["record"]["state"], "down")

    def test_now_accepted_mid_stop_applies_to_the_next_busy_entry(self) -> None:
        first = top_level("first", 101)
        later = top_level("later", 102, status="busy")
        record.create(
            shutdown_record([entry(first), entry(later, "waiting")])
        )
        killed: list[int] = []

        def kill(pid: int, _sent_signal: int) -> None:
            killed.append(pid)
            if pid != 101:
                return

            def force_now(current: ShutdownRecord) -> None:
                current["force"] = "now"

            _ = record.update(LOGIN, force_now)

        report = stop.stop(
            LOGIN,
            kill=kill,
            is_alive=lambda _pid: False,
            fresh_inventory=lambda _login, _scope: machine_inventory(
                [first, later]
            ),
            clock=lambda: NOW_UTC,
            codex_mesh=lambda _arguments: 0,
            tmux=lambda _arguments: 1,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(killed, [101, 102])
        self.assertEqual(report["record"]["state"], "down")

    def test_absent_entry_is_not_signalled_or_refused_for_readiness(self) -> None:
        stored = top_level("gone", 101)
        record.create(shutdown_record([entry(stored, "waiting")]))
        killed: list[int] = []

        report = stop.stop(
            LOGIN,
            kill=lambda pid, _signal: killed.append(pid),
            is_alive=lambda _pid: False,
            fresh_inventory=lambda _login, _scope: machine_inventory([]),
            clock=lambda: NOW_UTC,
            codex_mesh=lambda _arguments: 0,
            tmux=lambda _arguments: 1,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(killed, [])
        self.assertEqual(
            report["record"]["entries"][0]["progress"]["kind"],
            "already gone",
        )
        self.assertEqual(report["record"]["state"], "down")

    def test_absent_unit_stops_each_run_once_and_its_exact_tmux_session(
        self,
    ) -> None:
        run_dir = str(self.root / "run")
        stored = unit(
            "gone-unit",
            101,
            tmux_session="exact-unit-pane",
            run_dirs=[run_dir],
            codex_servers=[server(run_dir, 900)],
        )
        record.create(shutdown_record([entry(stored, "waiting")]))
        codex_calls: list[list[str]] = []
        tmux_calls: list[list[str]] = []

        def codex(arguments: list[str]) -> int:
            codex_calls.append(arguments)
            return 0

        def tmux(arguments: list[str]) -> int:
            tmux_calls.append(arguments)
            return 1 if arguments[0] == "has-session" else 0

        report = stop.stop(
            LOGIN,
            kill=lambda _pid, _signal: self.fail("an absent owner was signalled"),
            is_alive=lambda _pid: False,
            fresh_inventory=lambda _login, _scope: machine_inventory([]),
            clock=lambda: NOW_UTC,
            codex_mesh=codex,
            tmux=tmux,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(
            codex_calls, [["stop", "--session-dir", run_dir]]
        )
        self.assertEqual(
            tmux_calls,
            [
                ["kill-session", "-t", "=exact-unit-pane"],
                ["has-session", "-t", "=exact-unit-pane"],
            ],
        )
        self.assertEqual(report["record"]["state"], "down")

    def test_process_identity_changed_before_first_signal_is_left_running(
        self,
    ) -> None:
        run_dir = str(self.root / "run")
        stored = unit(
            "reused",
            101,
            tmux_session="reused-unit",
            run_dirs=[run_dir],
        )
        changed = unit(
            "reused",
            202,
            tmux_session="reused-unit",
            run_dirs=[run_dir],
        )
        record.create(shutdown_record([entry(stored)]))
        killed: list[int] = []
        codex_calls: list[list[str]] = []

        report = stop.stop(
            LOGIN,
            kill=lambda pid, _signal: killed.append(pid),
            is_alive=lambda _pid: True,
            fresh_inventory=lambda _login, _scope: machine_inventory([changed]),
            clock=lambda: NOW_UTC,
            codex_mesh=lambda arguments: codex_calls.append(arguments) or 0,
            tmux=lambda _arguments: 1,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(killed, [])
        self.assertEqual(codex_calls, [])
        self.assertEqual(
            report["record"]["entries"][0]["progress"]["kind"],
            "process identity lost",
        )
        self.assertEqual(
            report["record"]["entries"][0]["stop_issues"],
            [
                record.CodexServerLeftRunning(
                    kind="codex server left running",
                    at=NOW,
                    run_dir=run_dir,
                    cause="owner identity lost",
                ),
                record.UnitTmuxLeftRunning(
                    kind="unit tmux session left running",
                    at=NOW,
                    tmux_session="reused-unit",
                    cause="owner identity lost",
                ),
            ],
        )
        self.assertEqual(report["record"]["state"], "down")

    def test_process_identity_changed_before_second_signal_is_not_signalled_again(
        self,
    ) -> None:
        stored = top_level("reused", 101)
        changed = top_level("reused", 202)
        inventories = FreshInventorySequence(
            [machine_inventory([stored]), machine_inventory([changed]), machine_inventory([])]
        )
        record.create(shutdown_record([entry(stored)]))
        killed: list[int] = []

        report = stop.stop(
            LOGIN,
            kill=lambda pid, _signal: killed.append(pid),
            is_alive=lambda _pid: True,
            fresh_inventory=inventories,
            clock=lambda: NOW_UTC,
            codex_mesh=lambda _arguments: 0,
            tmux=lambda _arguments: 1,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(killed, [101])
        self.assertEqual(
            report["record"]["entries"][0]["progress"]["kind"],
            "process identity lost",
        )
        self.assertEqual(report["record"]["state"], "down")

    def test_second_sigterm_uses_identity_without_rechecking_readiness(self) -> None:
        stored = top_level("became-busy", 101)
        busy = top_level("became-busy", 101, status="busy")
        inventories = FreshInventorySequence(
            [machine_inventory([stored]), machine_inventory([busy])]
        )
        record.create(shutdown_record([entry(stored)]))
        sent: list[int] = []
        alive = True

        def kill(_pid: int, sent_signal: int) -> None:
            nonlocal alive
            sent.append(sent_signal)
            if len(sent) == 2:
                alive = False

        report = stop.stop(
            LOGIN,
            kill=kill,
            is_alive=lambda _pid: alive,
            fresh_inventory=inventories,
            clock=lambda: NOW_UTC,
            codex_mesh=lambda _arguments: 0,
            tmux=lambda _arguments: 1,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(sent, [signal.SIGTERM, signal.SIGTERM])
        self.assertEqual(
            report["record"]["entries"][0]["progress"]["kind"], "stopped"
        )

    def test_pid_surviving_two_sigterms_is_failed_without_sigkill(self) -> None:
        stored = top_level("stuck", 101)
        record.create(shutdown_record([entry(stored)]))
        sent: list[int] = []

        report = stop.stop(
            LOGIN,
            kill=lambda _pid, sent_signal: sent.append(sent_signal),
            is_alive=lambda _pid: True,
            fresh_inventory=lambda _login, _scope: machine_inventory([stored]),
            clock=lambda: NOW_UTC,
            codex_mesh=lambda _arguments: 0,
            tmux=lambda _arguments: 1,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(sent, [signal.SIGTERM, signal.SIGTERM])
        self.assertNotIn(signal.SIGKILL, sent)
        progress = report["record"]["entries"][0]["progress"]
        self.assertEqual(progress["kind"], "stop failed")
        if progress["kind"] != "stop failed":
            self.fail("expected stop failure")
        self.assertEqual(progress["reason"], "alive after two SIGTERMs")
        self.assertEqual(
            report["record"]["entries"][0]["stop_issues"],
            [
                record.StillRunningAfterStop(
                    kind="still running",
                    at=NOW,
                    reason="alive after two SIGTERMs",
                )
            ],
        )
        self.assertEqual(report["record"]["state"], "stop partial")

    def test_a_new_stop_pass_replaces_an_entrys_previous_issues(self) -> None:
        stored = top_level("stuck", 101)
        stored_entry = entry(stored)
        stored_entry["stop_issues"] = [
            record.AccountUnreadableAtStop(kind="account unreadable", at=NOW)
        ]
        record.create(shutdown_record([stored_entry], state="stopping"))

        report = stop.stop(
            LOGIN,
            kill=lambda _pid, _signal: None,
            is_alive=lambda _pid: True,
            fresh_inventory=lambda _login, _scope: machine_inventory([stored]),
            clock=lambda: NOW_UTC,
            codex_mesh=lambda _arguments: 0,
            tmux=lambda _arguments: 1,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(
            report["record"]["entries"][0]["stop_issues"],
            [
                record.StillRunningAfterStop(
                    kind="still running",
                    at=NOW,
                    reason="alive after two SIGTERMs",
                )
            ],
        )

    def test_rerun_leaves_a_stop_failed_entry_and_its_reason_untouched(self) -> None:
        stored = top_level("stuck", 101, run_dirs=[str(self.root / "run")])
        failed = entry(stored)
        failed["progress"] = SessionStopFailed(
            kind="stop failed", at=NOW, reason="alive after two SIGTERMs"
        )
        failed["stop_issues"] = [
            record.StillRunningAfterStop(
                kind="still running",
                at=NOW,
                reason="alive after two SIGTERMs",
            )
        ]
        record.create(shutdown_record([failed], state="stopping"))
        codex_calls: list[list[str]] = []

        report = stop.stop(
            LOGIN,
            kill=lambda _pid, _signal: self.fail("a terminal entry was signalled"),
            is_alive=lambda _pid: True,
            fresh_inventory=lambda _login, _scope: machine_inventory([]),
            clock=lambda: NOW_UTC,
            codex_mesh=lambda arguments: codex_calls.append(arguments) or 0,
            tmux=lambda _arguments: 1,
            sleep=lambda _seconds: None,
        )

        progress = report["record"]["entries"][0]["progress"]
        self.assertEqual(progress, failed["progress"])
        self.assertEqual(codex_calls, [])
        self.assertEqual(report["record"]["state"], "stop partial")

    def test_showrunner_codex_server_is_stopped_once_after_exit(self) -> None:
        run_dir = str(self.root / "showrunner-run")
        stored = showrunner(
            "runner",
            101,
            run_dirs=[run_dir],
            codex_servers=[server(run_dir, 900)],
        )
        record.create(shutdown_record([entry(stored)]))
        codex_calls: list[list[str]] = []

        report = stop.stop(
            LOGIN,
            kill=lambda _pid, _signal: None,
            is_alive=lambda _pid: False,
            fresh_inventory=lambda _login, _scope: machine_inventory([stored]),
            clock=lambda: NOW_UTC,
            codex_mesh=lambda arguments: codex_calls.append(arguments) or 0,
            tmux=lambda _arguments: 1,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(
            codex_calls, [["stop", "--session-dir", run_dir]]
        )
        self.assertEqual(report["record"]["state"], "down")

    def test_unconfirmed_codex_server_and_unit_tmux_leave_stop_partial(self) -> None:
        run_dir = str(self.root / "unit-run")
        stored = unit(
            "unit",
            101,
            tmux_session="unit-exact",
            run_dirs=[run_dir],
        )
        record.create(shutdown_record([entry(stored)]))

        report = stop.stop(
            LOGIN,
            kill=lambda _pid, _signal: None,
            is_alive=lambda _pid: False,
            fresh_inventory=lambda _login, _scope: machine_inventory([stored]),
            clock=lambda: NOW_UTC,
            codex_mesh=lambda _arguments: 1,
            tmux=lambda _arguments: 0,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(report["record"]["state"], "stop partial")
        self.assertEqual(
            report["record"]["entries"][0]["stop_issues"],
            [
                record.CodexServerLeftRunning(
                    kind="codex server left running",
                    at=NOW,
                    run_dir=run_dir,
                    cause="stop not confirmed",
                ),
                record.UnitTmuxLeftRunning(
                    kind="unit tmux session left running",
                    at=NOW,
                    tmux_session="unit-exact",
                    cause="stop not confirmed",
                ),
            ],
        )

    def test_retry_repeats_cleanup_when_first_cleanup_dies_before_completion(
        self,
    ) -> None:
        run_dir = str(self.root / "unit-run")
        stored = unit(
            "unit",
            101,
            tmux_session="unit-exact",
            run_dirs=[run_dir],
        )
        record.create(shutdown_record([entry(stored)]))
        inventories = FreshInventorySequence(
            [machine_inventory([stored]), machine_inventory([])]
        )
        codex_calls: list[list[str]] = []
        tmux_calls: list[list[str]] = []

        def codex(arguments: list[str]) -> int:
            codex_calls.append(arguments)
            return 1

        def tmux(arguments: list[str]) -> int:
            tmux_calls.append(arguments)
            if len(tmux_calls) == 1:
                raise RuntimeError("stop process died")
            return 1 if arguments[0] == "has-session" else 0

        with self.assertRaisesRegex(RuntimeError, "stop process died"):
            _ = stop.stop(
                LOGIN,
                kill=lambda _pid, _signal: None,
                is_alive=lambda _pid: False,
                session_record_exists=lambda _pid: False,
                fresh_inventory=inventories,
                clock=lambda: NOW_UTC,
                codex_mesh=codex,
                tmux=tmux,
                sleep=lambda _seconds: None,
            )

        interrupted = self.stored_record()["entries"][0]
        self.assertEqual(interrupted["progress"]["kind"], "ready")
        self.assertEqual(interrupted["stop_issues"], [])

        report = stop.stop(
            LOGIN,
            kill=lambda _pid, _signal: self.fail("an absent owner was signalled"),
            is_alive=lambda _pid: False,
            session_record_exists=lambda _pid: False,
            fresh_inventory=inventories,
            clock=lambda: NOW_UTC,
            codex_mesh=codex,
            tmux=tmux,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(
            codex_calls,
            [
                ["stop", "--session-dir", run_dir],
                ["stop", "--session-dir", run_dir],
            ],
        )
        self.assertEqual(report["record"]["state"], "stop partial")
        self.assertEqual(
            report["record"]["entries"][0]["stop_issues"],
            [
                record.CodexServerLeftRunning(
                    kind="codex server left running",
                    at=NOW,
                    run_dir=run_dir,
                    cause="stop not confirmed",
                )
            ],
        )

    def test_deleted_server_record_does_not_hide_a_surviving_server_pid(self) -> None:
        run_dir = self.root / "live-server"
        run_dir.mkdir()
        server_file = run_dir / "mesh_server.json"
        _ = server_file.write_text(json.dumps({"pid": 900}), encoding="utf-8")
        stored = showrunner("runner", 101, run_dirs=[str(run_dir)])
        record.create(shutdown_record([entry(stored)]))

        def codex(arguments: list[str]) -> int:
            self.assertEqual(arguments, ["stop", "--session-dir", str(run_dir)])
            server_file.unlink()
            return 0

        report = stop.stop(
            LOGIN,
            kill=lambda _pid, _signal: None,
            is_alive=lambda pid: pid == 900,
            fresh_inventory=lambda _login, _scope: machine_inventory([stored]),
            clock=lambda: NOW_UTC,
            codex_mesh=codex,
            tmux=lambda _arguments: 1,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(report["record"]["state"], "stop partial")
        self.assertEqual(
            report["record"]["entries"][0]["stop_issues"],
            [
                record.CodexServerLeftRunning(
                    kind="codex server left running",
                    at=NOW,
                    run_dir=str(run_dir),
                    cause="stop not confirmed",
                )
            ],
        )

    def test_tmux_runner_error_leaves_the_unit_session_unconfirmed(self) -> None:
        stored = unit("gone-unit", 101, tmux_session="unit-on-nix")
        record.create(shutdown_record([entry(stored, "waiting")]))

        def unavailable_tmux(_arguments: list[str]) -> int:
            raise OSError("tmux is not on PATH")

        report = stop.stop(
            LOGIN,
            kill=lambda _pid, _signal: self.fail("an absent unit was signalled"),
            is_alive=lambda _pid: False,
            fresh_inventory=lambda _login, _scope: machine_inventory([]),
            clock=lambda: NOW_UTC,
            codex_mesh=lambda _arguments: 0,
            tmux=unavailable_tmux,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(report["record"]["state"], "stop partial")
        self.assertEqual(
            report["record"]["entries"][0]["stop_issues"],
            [
                record.UnitTmuxLeftRunning(
                    kind="unit tmux session left running",
                    at=NOW,
                    tmux_session="unit-on-nix",
                    cause="stop not confirmed",
                )
            ],
        )

    def test_ghostty_shell_with_a_different_parent_gets_no_sighup(self) -> None:
        host = inventory.WindowHost(
            kind="ghostty",
            window_shell=901,
            desktop=inventory.DesktopNotInSnapshot(kind="not in snapshot"),
        )
        stored = top_level("window", 101, host=host)
        record.create(shutdown_record([entry(stored)]))
        signals: list[tuple[int, int]] = []
        ps_rows: list[subprocess.CompletedProcess[str]] = [
            subprocess.CompletedProcess(
                args=["ps"], returncode=0, stdout="801 zsh\n", stderr=""
            ),
            subprocess.CompletedProcess(
                args=["ps"], returncode=0, stdout="1 bash\n", stderr=""
            ),
        ]

        with patch("stop.subprocess.run", side_effect=ps_rows) as ps:
            report = stop.stop(
                LOGIN,
                kill=lambda pid, sent_signal: signals.append((pid, sent_signal)),
                is_alive=lambda pid: pid == 901,
                fresh_inventory=lambda _login, _scope: machine_inventory([stored]),
                clock=lambda: NOW_UTC,
                codex_mesh=lambda _arguments: 0,
                tmux=lambda _arguments: 1,
                sleep=lambda _seconds: None,
            )

        self.assertEqual(signals, [(101, signal.SIGTERM)])
        self.assertEqual(ps.call_count, 2)
        self.assertEqual(report["record"]["state"], "down")

    def test_seat_is_stopped_when_its_owner_stops(self) -> None:
        owner = unit("owner", 101)
        owned_seat = seat("seat", 201, owner="owner")
        sessions: list[inventory.Session] = [owner, owned_seat]
        record.create(
            shutdown_record(
                [entry(owner), entry(owned_seat, "passive seat ready")]
            )
        )

        report = stop.stop(
            LOGIN,
            kill=lambda _pid, _signal: None,
            is_alive=lambda _pid: False,
            fresh_inventory=lambda _login, _scope: machine_inventory(sessions),
            clock=lambda: NOW_UTC,
            codex_mesh=lambda _arguments: 0,
            tmux=lambda arguments: 1 if arguments[0] == "has-session" else 0,
            sleep=lambda _seconds: None,
        )

        seat_progress = next(
            item["progress"]["kind"]
            for item in report["record"]["entries"]
            if item["session"]["kind"] == "seat"
        )
        self.assertEqual(seat_progress, "stopped")
        self.assertEqual(report["record"]["state"], "down")

    def test_identity_lost_owner_live_under_new_pid_keeps_seat_live(self) -> None:
        stored_owner = top_level("owner", 101)
        current_owner = top_level("owner", 102)
        owned_seat = seat("seat", 201, owner="owner")
        record.create(
            shutdown_record(
                [entry(stored_owner), entry(owned_seat, "passive seat ready")]
            )
        )
        inventories = FreshInventorySequence(
            [
                machine_inventory([current_owner]),
                machine_inventory([current_owner]),
            ]
        )

        report = stop.stop(
            LOGIN,
            kill=lambda _pid, _signal: self.fail(
                "an identity-lost owner was signalled"
            ),
            is_alive=lambda _pid: True,
            fresh_inventory=inventories,
            clock=lambda: NOW_UTC,
            codex_mesh=lambda _arguments: 0,
            tmux=lambda _arguments: 1,
            sleep=lambda _seconds: None,
        )

        owner_entry, seat_entry = report["record"]["entries"]
        self.assertEqual(owner_entry["progress"]["kind"], "process identity lost")
        self.assertEqual(seat_entry["progress"]["kind"], "passive seat ready")
        self.assertEqual(
            seat_entry["stop_issues"],
            [record.SeatStillLive(kind="seat still live", at=NOW)],
        )
        self.assertEqual(report["record"]["state"], "stop partial")

    def test_identity_lost_owner_gone_releases_its_absent_seat(self) -> None:
        stored_owner = top_level("owner", 101)
        owned_seat = seat("seat", 201, owner="owner")
        record.create(
            shutdown_record(
                [entry(stored_owner), entry(owned_seat, "passive seat ready")]
            )
        )
        mismatch = inventory.UnattributedSession(
            pid=101,
            name="unrelated",
            reason="process start mismatch",
        )
        inventories = FreshInventorySequence(
            [machine_inventory([], unattributed=[mismatch]), machine_inventory([])]
        )

        report = stop.stop(
            LOGIN,
            kill=lambda _pid, _signal: self.fail(
                "an identity-lost owner was signalled"
            ),
            is_alive=lambda _pid: True,
            fresh_inventory=inventories,
            clock=lambda: NOW_UTC,
            codex_mesh=lambda _arguments: 0,
            tmux=lambda _arguments: 1,
            sleep=lambda _seconds: None,
        )

        owner_entry, seat_entry = report["record"]["entries"]
        self.assertEqual(owner_entry["progress"]["kind"], "process identity lost")
        self.assertEqual(seat_entry["progress"]["kind"], "stopped")
        self.assertEqual(seat_entry["stop_issues"], [])
        self.assertEqual(report["record"]["state"], "down")

    def test_orphan_seat_stops_only_after_fresh_inventory_shows_no_process(
        self,
    ) -> None:
        orphan = seat("orphan", 201, owner=None)
        record.create(
            shutdown_record([entry(orphan, "passive seat ready")])
        )

        report = stop.stop(
            LOGIN,
            kill=lambda _pid, _signal: self.fail("a passive seat was signalled"),
            is_alive=lambda _pid: False,
            fresh_inventory=lambda _login, _scope: machine_inventory([]),
            clock=lambda: NOW_UTC,
            codex_mesh=lambda _arguments: 0,
            tmux=lambda _arguments: 1,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(
            report["record"]["entries"][0]["progress"]["kind"], "stopped"
        )
        self.assertEqual(report["record"]["state"], "down")

    def test_orphan_seat_stays_unstopped_while_fresh_inventory_lists_it(
        self,
    ) -> None:
        owner = top_level("live-owner", 101)
        orphan = seat("orphan", 201, owner="live-owner")
        record.create(
            shutdown_record([entry(orphan, "passive seat ready")])
        )

        report = stop.stop(
            LOGIN,
            kill=lambda _pid, _signal: self.fail("a passive seat was signalled"),
            is_alive=lambda _pid: True,
            fresh_inventory=lambda _login, _scope: machine_inventory(
                [owner, orphan]
            ),
            clock=lambda: NOW_UTC,
            codex_mesh=lambda _arguments: 0,
            tmux=lambda _arguments: 1,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(
            report["record"]["entries"][0]["progress"]["kind"],
            "passive seat ready",
        )
        self.assertEqual(
            report["record"]["entries"][0]["stop_issues"],
            [record.SeatStillLive(kind="seat still live", at=NOW)],
        )
        self.assertEqual(report["record"]["state"], "stop partial")

    def test_empty_record_goes_down(self) -> None:
        record.create(shutdown_record([]))

        report = stop.stop(
            LOGIN,
            kill=lambda _pid, _signal: self.fail("nothing should be signalled"),
            is_alive=lambda _pid: False,
            fresh_inventory=lambda _login, _scope: machine_inventory([]),
            clock=lambda: NOW_UTC,
            codex_mesh=lambda _arguments: 0,
            tmux=lambda _arguments: 1,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(report["record"]["state"], "down")

    def test_conduct_stops_other_machine_then_requesters_machine_and_alerts(
        self,
    ) -> None:
        requester = top_level("requester", 101)
        local = shutdown_record(
            [entry(requester)], machine="natedev", state="settling", requester="requester"
        )
        remote = shutdown_record([], machine="Mac", state="settling")
        local_down = shutdown_record(
            [entry(requester)], machine="natedev", state="down", requester="requester"
        )
        remote_down = shutdown_record([], machine="Mac", state="down")
        record.create(local)
        events: list[str] = []
        alerts: list[AlertCall] = []

        def remote_stop(_login: str) -> settle.RemoteStopOutcome:
            events.append("Mac")
            return settle.RemoteStopped(
                kind="stopped", report=stop_report(remote_down)
            )

        def local_stop(_login: str) -> stop.StopReport:
            events.append("natedev")
            return stop_report(local_down)

        def send_alert(
            recipient: str,
            summary: str,
            text: str,
            *,
            need: str = "",
            machine: str = "",
        ) -> settle.MessageDelivery:
            alerts.append(
                AlertCall(
                    recipient=recipient,
                    summary=summary,
                    text=text,
                    need=need,
                    machine=machine,
                )
            )
            return settle.MessageSent(kind="sent")

        with (
            patch.object(
                settle,
                "conduct_cycle",
                return_value=settle.ReadyToStop(
                    kind="ready to stop",
                    reports=[refresh_report(local), refresh_report(remote)]
                ),
            ),
            patch.object(stop, "claim_stop_as_conductor", return_value=True),
            patch.object(
                settle,
                "claim_remote_stop",
                return_value=settle.RemoteStopClaimed(kind="claimed"),
            ),
            patch.object(settle, "stop_remote", side_effect=remote_stop),
            patch.object(stop, "stop", side_effect=local_stop),
            patch.object(settle, "send_message", side_effect=send_alert),
        ):
            result = settle.conduct(LOGIN)

        self.assertEqual(result, 0)
        self.assertEqual(events, ["Mac", "natedev"])
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["recipient"], "user")
        self.assertEqual(alerts[0]["summary"], f"{LABEL} is down")
        self.assertEqual(alerts[0]["need"], "note")
        self.assertIn("/shutdown restart", alerts[0]["text"])

    def test_conduct_claims_peer_with_conductor_force_before_any_stop(
        self,
    ) -> None:
        local = shutdown_record([], state="settling", force="now")
        remote_busy = top_level("remote-busy", 202, status="busy")
        remote = shutdown_record(
            [entry(remote_busy, "waiting")],
            machine="Mac",
            state="settling",
        )
        remote_down = shutdown_record(
            [entry(remote_busy, "waiting")],
            machine="Mac",
            state="down",
            force="now",
        )
        remote_down["entries"][0]["progress"] = SessionStopped(
            kind="stopped", at=NOW
        )
        record.create(local)
        events: list[str] = []

        def claim_remote(_login: str, force: str) -> settle.RemoteStopClaimOutcome:
            events.append(f"claim Mac {force}")
            remote["state"] = "stopping"
            remote["force"] = cast(StopTiming, force)
            return settle.RemoteStopClaimed(kind="claimed")

        def remote_stop(_login: str) -> settle.RemoteStopOutcome:
            self.assertEqual(remote["state"], "stopping")
            self.assertEqual(remote["force"], "now")
            events.append("stop Mac busy entry")
            return settle.RemoteStopped(
                kind="stopped", report=stop_report(remote_down)
            )

        def local_stop(_login: str) -> stop.StopReport:
            current = self.stored_record()
            self.assertEqual(current["state"], "stopping")
            self.assertEqual(current["force"], "now")
            events.append("stop natedev")
            local_down = shutdown_record([], state="down", force="now")
            return stop_report(local_down)

        with (
            patch.object(
                settle,
                "conduct_cycle",
                return_value=settle.ReadyToStop(
                    kind="ready to stop",
                    reports=[refresh_report(local), refresh_report(remote)],
                ),
            ),
            patch.object(settle, "claim_remote_stop", side_effect=claim_remote),
            patch.object(settle, "stop_remote", side_effect=remote_stop),
            patch.object(stop, "stop", side_effect=local_stop),
            patch.object(settle, "send_stop_alert"),
        ):
            result = settle.conduct(LOGIN)

        self.assertEqual(result, 0)
        self.assertEqual(
            events,
            ["claim Mac now", "stop Mac busy entry", "stop natedev"],
        )
        self.assertEqual(
            remote_down["entries"][0]["progress"]["kind"], "stopped"
        )

    def test_claim_stop_command_copies_force_while_claiming(self) -> None:
        record.create(shutdown_record([], state="settling"))

        result = shutdown.main(
            ["claim-stop", LOGIN, "--force", "now"]
        )

        self.assertEqual(result, 0)
        current = self.stored_record()
        self.assertEqual(current["state"], "stopping")
        self.assertEqual(current["force"], "now")

    def test_conduct_retries_a_peer_claim_after_its_success_response_is_lost(
        self,
    ) -> None:
        local = shutdown_record([], state="settling", force="now")
        remote = shutdown_record(
            [], machine="Mac", state="settling", force="wait for ready"
        )
        remote_root = self.root / "remote-shutdown"
        record.create(local)
        with patch.dict(
            os.environ, {"SHUTDOWN_STATE_DIR": str(remote_root)}
        ):
            record.create(remote)

        real_stop = stop.stop
        claim_attempts = 0

        def remote_command(
            arguments: list[str],
            stdin: str = "",
            limit: remote_transport.RemoteCallLimit = remote_transport.STANDARD_TIME_LIMIT,
        ) -> tuple[int, str]:
            nonlocal claim_attempts
            del stdin, limit
            with patch.dict(
                os.environ, {"SHUTDOWN_STATE_DIR": str(remote_root)}
            ):
                if arguments[0] == "claim-stop":
                    result = shutdown.main(arguments)
                    claim_attempts += 1
                    return (255, "") if claim_attempts == 1 else (result, "")
                self.assertEqual(arguments, ["stop", LOGIN])
                report = real_stop(
                    LOGIN,
                    fresh_inventory=lambda _login, _scope: machine_inventory(
                        [], machine="Mac"
                    ),
                )
                return 0, json.dumps(report)

        def local_stop(login: str) -> stop.StopReport:
            return real_stop(
                login,
                fresh_inventory=lambda _login, _scope: machine_inventory([]),
            )

        with (
            patch.object(
                settle,
                "conduct_cycle",
                return_value=settle.ReadyToStop(
                    kind="ready to stop",
                    reports=[refresh_report(local), refresh_report(remote)],
                ),
            ),
            patch.object(settle, "run_remote", side_effect=remote_command),
            patch.object(time, "sleep"),
            patch.object(stop, "stop", side_effect=local_stop),
            patch.object(settle, "send_stop_alert") as alert,
        ):
            result = settle.conduct(LOGIN)

        with patch.dict(
            os.environ, {"SHUTDOWN_STATE_DIR": str(remote_root)}
        ):
            remote_record = record.find_live(LOGIN)
        self.assertEqual(result, 0)
        self.assertEqual(claim_attempts, 2)
        self.assertEqual(remote_record["kind"], "live")
        if remote_record["kind"] != "live":
            self.fail("expected the peer shutdown record to remain live")
        self.assertEqual(remote_record["record"]["state"], "down")
        self.assertEqual(remote_record["record"]["force"], "now")
        self.assertEqual(self.stored_record()["state"], "down")
        alert.assert_called_once()

    def test_cancelled_record_ends_conduct_without_stopping(self) -> None:
        record.create(shutdown_record([], state="cancelled"))

        with (
            patch.object(stop, "claim_stop_as_conductor") as claim,
            patch.object(stop, "stop") as stop_local,
        ):
            result = settle.conduct(LOGIN, here=True)

        self.assertEqual(result, 0)
        claim.assert_not_called()
        stop_local.assert_not_called()

    def test_stop_wins_claim_and_cancel_changes_nothing(self) -> None:
        record.create(shutdown_record([], state="settling"))
        self.assertTrue(stop.claim_stop_as_conductor(LOGIN))
        output = io.StringIO()

        with redirect_stdout(output):
            result = settle.cancel(self.account, here=True)

        self.assertEqual(result, 1)
        self.assertEqual(self.stored_record()["state"], "stopping")
        self.assertEqual(
            output.getvalue(),
            f"shutdown of {LABEL} is already stopping; "
            + "/shutdown restart brings it back once it is down\n",
        )

    def test_cancel_wins_claim_and_later_stop_changes_nothing(self) -> None:
        record.create(shutdown_record([], state="settling"))

        with (
            patch.object(settle, "_stop_conductor"),
            patch.object(settle, "_restore_record"),
        ):
            cancelled = settle.cancel(self.account, here=True)

        self.assertEqual(cancelled, 0)
        self.assertFalse(stop.claim_stop_as_conductor(LOGIN))
        self.assertEqual(
            record.find_live(LOGIN), record.NoShutdown(kind="no shutdown")
        )

    def test_cancel_with_an_already_gone_conductor_prints_no_service_noise(
        self,
    ) -> None:
        current = shutdown_record([], state="settling")
        current["conductor"] = record.SystemdConductor(
            kind="systemd", unit="shutdown-owner"
        )
        record.create(current)
        stdout = io.StringIO()
        stderr = io.StringIO()
        service_calls: list[list[str]] = []
        real_stop_conductor = stop.stop_conductor

        def already_gone(arguments: list[str]) -> int:
            service_calls.append(arguments)
            return 5

        def stop_without_noise(conductor: record.Conductor) -> None:
            real_stop_conductor(conductor, systemctl=already_gone)

        with (
            patch.object(
                stop, "stop_conductor", side_effect=stop_without_noise
            ) as stopped,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = settle.cancel(self.account, here=True)

        self.assertEqual(result, 0)
        stopped.assert_called_once_with(current["conductor"])
        self.assertEqual(
            service_calls, [["--user", "stop", "shutdown-owner"]]
        )
        self.assertNotIn("Failed to stop", stdout.getvalue() + stderr.getvalue())

    def test_alert_counts_unattributed_sessions_as_left_running(self) -> None:
        current = shutdown_record([], state="down")
        record.create(shutdown_record([], state="settling"))
        report = stop_report(
            current,
            unattributed=[
                inventory.UnattributedSession(
                    pid=801, name="unknown", reason="account unreadable"
                ),
                inventory.UnattributedSession(
                    pid=802, name="reused", reason="process start mismatch"
                ),
            ],
        )
        alerts: list[AlertCall] = []

        def send_alert(
            recipient: str,
            summary: str,
            text: str,
            *,
            need: str = "",
            machine: str = "",
        ) -> settle.MessageDelivery:
            alerts.append(
                AlertCall(
                    recipient=recipient,
                    summary=summary,
                    text=text,
                    need=need,
                    machine=machine,
                )
            )
            return settle.MessageSent(kind="sent")

        with (
            patch.object(
                settle,
                "conduct_cycle",
                return_value=settle.ReadyToStop(
                    kind="ready to stop",
                    reports=[refresh_report(current)]
                ),
            ),
            patch.object(stop, "claim_stop_as_conductor", return_value=True),
            patch.object(
                settle, "_stop_machines", return_value=([report], [])
            ),
            patch.object(settle, "send_message", side_effect=send_alert),
        ):
            self.assertEqual(settle.conduct(LOGIN, here=True), 0)

        self.assertEqual(len(alerts), 1)
        self.assertIn(
            f"left running, not attributed to {LABEL}: 2 ", alerts[0]["text"]
        )
        self.assertIn("1 account unreadable", alerts[0]["text"])
        self.assertIn("1 process start mismatch", alerts[0]["text"])
        self.assertNotIn("stopped unknown", alerts[0]["text"])

    def test_stop_alert_route_comes_from_the_conductor_machine(self) -> None:
        natedev = stop_report(
            shutdown_record([], machine="natedev", state="down")
        )
        mac = stop_report(shutdown_record([], machine="Mac", state="down"))
        alerts: list[AlertCall] = []

        def send_alert(
            recipient: str,
            summary: str,
            text: str,
            *,
            need: str = "",
            machine: str = "",
        ) -> settle.MessageDelivery:
            alerts.append(
                AlertCall(
                    recipient=recipient,
                    summary=summary,
                    text=text,
                    need=need,
                    machine=machine,
                )
            )
            return settle.MessageSent(kind="sent")

        with patch.object(settle, "send_message", side_effect=send_alert):
            with patch.object(settle, "_machine", return_value="Mac"):
                settle.send_stop_alert([natedev, mac], [])
            with patch.object(settle, "_machine", return_value="natedev"):
                settle.send_stop_alert([mac, natedev], [])

        self.assertEqual(
            [alert["machine"] for alert in alerts], ["natedev", ""]
        )

    def test_local_stop_exception_sends_a_partial_alert(self) -> None:
        current = shutdown_record([], state="settling")
        record.create(current)
        alerts: list[AlertCall] = []

        def send_alert(
            recipient: str,
            summary: str,
            text: str,
            *,
            need: str = "",
            machine: str = "",
        ) -> settle.MessageDelivery:
            alerts.append(
                AlertCall(
                    recipient=recipient,
                    summary=summary,
                    text=text,
                    need=need,
                    machine=machine,
                )
            )
            return settle.MessageSent(kind="sent")

        with (
            patch.object(
                settle,
                "conduct_cycle",
                return_value=settle.ReadyToStop(
                    kind="ready to stop", reports=[refresh_report(current)]
                ),
            ),
            patch.object(stop, "stop", side_effect=OSError("inventory failed")),
            patch.object(settle, "send_message", side_effect=send_alert),
            patch.object(settle, "_machine", return_value="natedev"),
        ):
            result = settle.conduct(LOGIN, here=True)

        self.assertEqual(result, 0)
        self.assertEqual(alerts[0]["summary"], f"{LABEL}: stop partial")
        self.assertIn(
            "natedev: stop failed: inventory failed", alerts[0]["text"]
        )


if __name__ == "__main__":
    _ = unittest.main()
