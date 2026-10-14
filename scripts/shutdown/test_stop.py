"""Tests for one session's stop claim, signals and resources."""

from __future__ import annotations

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
from pathlib import Path
from collections.abc import Callable
from typing import Literal, cast, final, override
from unittest.mock import patch

from test_support import ShutdownTestCase
from account import Account
import inventory
import herdr
from fake_herdr import CommandResponse
import record
import settle
import stop

LOGIN = "owner@example.com"
LABEL = "claude 2"
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
        host=inventory.TmuxHost(
            kind="tmux",
            server=inventory.TmuxServerNotRecorded(kind="not recorded"),
            session_ref=inventory.TmuxSessionIdNotRecorded(kind="not recorded"),
            tmux_session=f"tmux-{session_id}",
            pane=inventory.PaneNotRecorded(kind="not recorded"),
        ),
        production="demo",
        doc="/tmp/demo.md",
    )


def unit(
    session_id: str,
    pid: int,
    *,
    tmux_session: str | None = None,
    session_host: Literal["tmux", "herdr"] = "tmux",
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
            session_name=tmux_session or f"tmux-{session_id}",
            session_host=session_host,
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


def entry(session: inventory.Session, progress: str = "ready") -> record.SessionShutdown:
    state: record.SessionProgress = (
        record.SessionWaiting(kind="waiting") if progress == "waiting"
        else record.SessionReadyToStop(kind="ready", at=NOW)
    )
    return record.SessionShutdown(
        login=LOGIN, label=LABEL, machine="natedev", requested_at=NOW,
        request_id="request", requested_by=record.FromTerminal(kind="terminal"),
        hold="session", then="stop", force="wait for ready", alerts="none",
        session=session, seats=[], timers=[], settle_message=record.SettleMessageNotSent(kind="not sent"),
        where=record.WhereNotSaid(kind="not said"), progress=state, stop_issues=[],
    )


def records_for_test(
    entries: list[record.SessionShutdown], *, force: record.StopTiming = "wait for ready", state: str = "",
) -> list[record.SessionShutdown]:
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
class StopTests(ShutdownTestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
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
        self.herdr_replies([])
        _ = self.enterContext(patch.object(settle, "record_time", return_value=NOW))

    def create(self, records: list[record.SessionShutdown]) -> None:
        for item in records:
            self.assertTrue(record.create_session(item))

    def stored_record(self) -> record.SessionShutdown:
        return record.session_records(LOGIN)[0]

    def perform(self, saved: record.SessionShutdown) -> record.SessionShutdown:
        return stop.stop_session(
            saved, kill=lambda _pid, _signal: None, is_alive=lambda _pid: False,
            session_record_exists=lambda _pid: False,
            fresh_inventory=lambda _login, _only: machine_inventory([saved['session'], *saved['seats']]),
            clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0,
            tmux=lambda args: 1 if args[0] == 'has-session' else 0,
            sleep=lambda _seconds: None,
        )

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
            tmux=lambda _args: 1, sleep=lambda _seconds: None,
        )
        self.assertEqual(sent, [signal.SIGTERM])
        self.assertEqual(finished['progress']['kind'], 'stopped')

    def test_recent_stop_claim_has_no_signal_or_cleanup_effect(self) -> None:
        saved = entry(unit('owner', 101))
        saved['progress'] = record.SessionStopping(kind='stopping', at=NOW)
        self.create([saved])
        finished = stop.stop_session(
            saved, kill=lambda _pid, _sent: self.fail('a claimed owner was signalled'),
            fresh_inventory=lambda _login, _only: self.fail('a claimed owner was inspected'),
            clock=lambda: NOW_UTC + timedelta(seconds=119),
            codex_mesh=lambda _args: self.fail('claimed cleanup was repeated'),
            tmux=lambda _args: self.fail('claimed cleanup was repeated'),
        )
        self.assertEqual(finished, saved)

    def test_usage_limit_held_owner_is_claimed_and_stopped_without_now(self) -> None:
        saved = entry(top_level("owner", 101))
        saved["progress"] = record.SessionHeldByUsageLimit(kind="held by usage limit", at=NOW)
        self.create([saved])
        signals: list[int] = []
        def kill(pid: int, sent_signal: int) -> None:
            self.assertEqual(pid, 101)
            self.assertEqual(self.stored_record()["progress"], {"kind": "stopping", "at": NOW})
            signals.append(sent_signal)
        finished = stop.stop_session(saved, kill=kill, is_alive=lambda _pid: False,
            session_record_exists=lambda _pid: False,
            fresh_inventory=lambda _login, _only: machine_inventory([saved["session"]]),
            clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0, tmux=lambda _args: 1,
            sleep=lambda _seconds: None)
        self.assertEqual(signals, [signal.SIGTERM])
        self.assertEqual(finished["progress"]["kind"], "stopped")
        self.assertEqual(finished["force"], "wait for ready")

    def test_usage_limit_held_owner_with_busy_seat_keeps_held_progress_in_issue(self) -> None:
        saved = entry(top_level("owner", 101))
        saved["progress"] = record.SessionHeldByUsageLimit(kind="held by usage limit", at=NOW)
        worker = seat("worker", 102, owner="owner")
        worker["status"] = "busy"
        saved["seats"] = [worker]
        self.create([saved])
        finished = stop.stop_session(saved,
            kill=lambda _pid, _sent: self.fail("busy seat owner received a signal"),
            fresh_inventory=lambda _login, _only: machine_inventory([saved["session"], worker]),
            clock=lambda: NOW_UTC, codex_mesh=lambda _args: self.fail("busy seat was ended"),
            tmux=lambda _args: self.fail("busy seat host was stopped"))
        self.assertEqual(finished["progress"]["kind"], "stop failed")
        self.assertEqual(finished["stop_issues"], [record.NotReadyToStop(kind="not ready to stop",
            at=NOW, status="idle", progress="held by usage limit")])

    def test_stale_stop_claim_retries_an_idle_owner(self) -> None:
        saved = entry(top_level('owner', 101))
        saved['progress'] = record.SessionStopping(kind='stopping', at=(NOW_UTC - timedelta(seconds=120)).isoformat())
        self.create([saved])
        self.assertEqual(self.perform(saved)['progress']['kind'], 'stopped')

    def test_cleanup_retry_only_repeats_unconfirmed_resources_without_a_signal(self) -> None:
        pending = str(self.root / "pending")
        saved = entry(unit('owner', 101, run_dirs=[str(self.root / "clean"), pending]))
        saved['progress'] = record.SessionStopped(kind='stopped', at=NOW)
        saved['stop_issues'] = [record.CodexServerLeftRunning(
            kind='codex server left running', at=NOW, run_dir=pending, cause='stop not confirmed',
        ), record.UnitSessionLeftRunning(
            kind='unit session left running', at=NOW, host="tmux", session_name='tmux-owner', cause='stop not confirmed',
        )]
        self.create([saved])
        codex: list[list[str]] = []
        tmux: list[list[str]] = []
        finished = stop.stop_session(
            saved, kill=lambda _pid, _sent: self.fail('a stopped owner was signalled'),
            is_alive=lambda _pid: False, clock=lambda: NOW_UTC,
            codex_mesh=lambda args: codex.append(args) or 0,
            tmux=lambda args: tmux.append(args) or (1 if args[0] == 'has-session' else 0),
        )
        self.assertEqual(codex, [['stop', '--session-dir', pending]])
        self.assertEqual(tmux, [['kill-session', '-t', '=tmux-owner'], ['has-session', '-t', '=tmux-owner']])
        self.assertEqual(finished['stop_issues'], [])
        self.assertEqual(finished['progress'], saved['progress'])

    def test_fresh_session_reports_owned_seats_and_preserves_the_recorded_identity(self) -> None:
        owner = top_level('owner', 101)
        worker = seat('worker', 102, owner='owner')
        unrelated = seat('other', 103, owner='elsewhere')
        observed = stop.fresh_session(owner, machine_inventory([owner, worker, unrelated]))
        self.assertEqual(observed, {'kind': 'matching session', 'session': owner, 'seats': [worker]})
        changed = copy.deepcopy(owner)
        changed['pid'] = 201
        self.assertEqual(stop.fresh_session(owner, machine_inventory([changed])), {'kind': 'session identity lost'})
        self.assertEqual(owner['pid'], 101)

    def test_busy_seat_holds_its_owner_until_it_is_idle_and_is_never_signalled(self) -> None:
        saved = entry(top_level('owner', 101))
        worker = seat('worker', 102, owner='owner')
        worker['status'] = 'busy'
        saved['seats'] = [worker]
        self.create([saved])
        self.assertEqual(self.perform(saved)['progress']['kind'], 'stop failed')
        worker['status'] = 'idle'
        saved['seats'] = [worker]
        self.assertEqual(self.perform(saved)['progress']['kind'], 'stopped')

    def test_cancel_during_a_stop_claim_keeps_its_record_and_timers(self) -> None:
        saved = entry(top_level('owner', 101))
        saved['timers'] = [record.TimerRestore(instance='owner-timer', was_enabled=True, footer=record.NoFooter(kind='no footer'))]
        self.create([saved])
        entered = threading.Event()
        release = threading.Event()
        failures: list[BaseException] = []

        def kill(_pid: int, _sent: int) -> None:
            entered.set()
            self.assertTrue(release.wait(0.8))

        def worker() -> None:
            try:
                _ = stop.stop_session(
                    saved, kill=kill, is_alive=lambda _pid: False,
                    session_record_exists=lambda _pid: False,
                    fresh_inventory=lambda _login, _only: machine_inventory([saved['session']]),
                    clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0,
                    tmux=lambda _args: 1, sleep=lambda _seconds: None,
                )
            except BaseException as error:
                failures.append(error)

        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(entered.wait(0.8))
            with patch.object(settle, 'run_notifier') as timers, patch.object(settle, 'send_message') as send, redirect_stdout(io.StringIO()):
                _ = settle.cancel(Account('claude', LOGIN, LABEL), here=True)
            timers.assert_not_called()
            send.assert_not_called()
            self.assertEqual(self.stored_record()['progress']['kind'], 'stopping')
            self.assertEqual(self.stored_record()['timers'], saved['timers'])
        finally:
            release.set()
            thread.join(0.8)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])


    def test_busy_entry_is_refused_without_now(self) -> None:
        stored = top_level('busy', 101)
        current = top_level('busy', 101, status='busy')
        self.create(records_for_test([entry(stored)]))
        killed: list[int] = []
        report = stop.stop_session(self.stored_record(), kill=lambda pid, _signal: killed.append(pid), is_alive=lambda _pid: False, fresh_inventory=lambda _login, _scope: machine_inventory([current]), clock=lambda: NOW_UTC, codex_mesh=lambda _arguments: 0, tmux=lambda _arguments: 1, sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
        self.assertEqual(killed, [])
        self.assertNotEqual(report['progress']['kind'], 'stopping')
        self.assertEqual(report['stop_issues'], [record.NotReadyToStop(kind='not ready to stop', at=NOW, status='busy', progress='ready')])

    def test_account_unreadable_entry_is_not_signalled_and_keeps_an_issue(self) -> None:
        stored = top_level('unreadable', 101)
        self.create(records_for_test([entry(stored)]))
        killed: list[int] = []
        unattributed = inventory.UnattributedSession(pid=101, name='unreadable', reason='account unreadable')
        report = stop.stop_session(self.stored_record(), kill=lambda pid, _signal: killed.append(pid), is_alive=lambda _pid: True, fresh_inventory=lambda _login, _scope: machine_inventory([], unattributed=[unattributed]), clock=lambda: NOW_UTC, codex_mesh=lambda _arguments: 0, tmux=lambda _arguments: 1, sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
        self.assertEqual(killed, [])
        self.assertEqual(report['stop_issues'], [record.AccountUnreadableAtStop(kind='account unreadable', at=NOW)])
        self.assertNotEqual(report['progress']['kind'], 'stopping')

    def test_now_ends_busy_codex_seats_before_stopping_their_owner(self) -> None:
        run_dir = str(self.root / 'run')
        owned_server = server(run_dir, 900, busy_seats=['impl', 'test'])
        stored = top_level('busy', 101, status='busy', run_dirs=[run_dir], codex_servers=[owned_server])
        self.create(records_for_test([entry(stored)], force='now'))
        events: list[str] = []

        def codex(arguments: list[str]) -> int:
            events.append('codex ' + ' '.join(arguments))
            return 0
        report = stop.stop_session(self.stored_record(), kill=lambda pid, sent: events.append(f'kill {pid} {sent}'), is_alive=lambda _pid: False, fresh_inventory=lambda _login, _scope: machine_inventory([stored]), clock=lambda: NOW_UTC, codex_mesh=codex, tmux=lambda _arguments: 1, sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
        self.assertEqual(events[:3], [f'codex end --session-dir {run_dir} --to impl', f'codex end --session-dir {run_dir} --to test', f'kill 101 {signal.SIGTERM}'])
        self.assertNotEqual(report['progress']['kind'], 'stopping')

    def test_absent_entry_is_not_signalled_or_refused_for_readiness(self) -> None:
        stored = top_level('gone', 101)
        self.create(records_for_test([entry(stored, 'waiting')]))
        killed: list[int] = []
        report = stop.stop_session(self.stored_record(), kill=lambda pid, _signal: killed.append(pid), is_alive=lambda _pid: False, fresh_inventory=lambda _login, _scope: machine_inventory([]), clock=lambda: NOW_UTC, codex_mesh=lambda _arguments: 0, tmux=lambda _arguments: 1, sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
        self.assertEqual(killed, [])
        self.assertEqual(report['progress']['kind'], 'already gone')
        self.assertNotEqual(report['progress']['kind'], 'stopping')

    def test_absent_unit_stops_each_run_once_and_its_exact_tmux_session(self) -> None:
        run_dir = str(self.root / 'run')
        stored = unit('gone-unit', 101, tmux_session='exact-unit-pane', run_dirs=[run_dir], codex_servers=[server(run_dir, 900)])
        self.create(records_for_test([entry(stored, 'waiting')]))
        codex_calls: list[list[str]] = []
        tmux_calls: list[list[str]] = []

        def codex(arguments: list[str]) -> int:
            codex_calls.append(arguments)
            return 0

        def tmux(arguments: list[str]) -> int:
            tmux_calls.append(arguments)
            return 1 if arguments[0] == 'has-session' else 0
        report = stop.stop_session(self.stored_record(), kill=lambda _pid, _signal: self.fail('an absent owner was signalled'), is_alive=lambda _pid: False, fresh_inventory=lambda _login, _scope: machine_inventory([]), clock=lambda: NOW_UTC, codex_mesh=codex, tmux=tmux, sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
        self.assertEqual(codex_calls, [['stop', '--session-dir', run_dir]])
        self.assertEqual(tmux_calls, [['kill-session', '-t', '=exact-unit-pane'], ['has-session', '-t', '=exact-unit-pane']])
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
            responses["--session same-name-as-tmux server stop"] = CommandResponse(code=1, stdout="", stderr="fixture stop failed")
        _ = (self.root / "herdr-responses.json").write_text(json.dumps(responses))

    def herdr_commands(self) -> list[list[str]]:
        path = self.root / "herdr.jsonl"
        return [cast(list[str], json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def test_herdr_unit_stop_runs_after_claude_exits_and_never_touches_tmux(self) -> None:
        for state in ("present", "absent"):
            with self.subTest(state=state):
                log = self.root / "herdr.jsonl"
                log.unlink(missing_ok=True)
                owner = unit(f"herdr-{state}", 101, tmux_session="same-name-as-tmux", session_host="herdr")
                saved = entry(owner)
                self.create([saved])
                signals: list[int] = []

                def kill(_pid: int, sent: int) -> None:
                    self.assertEqual(self.herdr_commands(), [])
                    signals.append(sent)

                report = stop.stop_session(saved, kill=kill, is_alive=lambda _pid: False,
                    fresh_inventory=lambda _login, _scope: machine_inventory([] if state == "absent" else [owner]),
                    clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0,
                    tmux=lambda _args: self.fail("herdr cleanup touched tmux"),
                    sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
                self.assertEqual(signals, [signal.SIGTERM] if state == "present" else [])
                self.assertEqual(self.herdr_commands(), [
                    ["--session", "same-name-as-tmux", "server", "stop"],
                    ["--user", "is-active", "herdr-same-name-as-tmux"],
                    ["--user", "stop", "herdr-same-name-as-tmux"],
                    ["--session", "default", "session", "list", "--json"],
                ])
                self.assertEqual(report["stop_issues"], [])
                self.assertEqual(report["progress"]["kind"], "stopped" if state == "present" else "already gone")

    def test_other_running_herdr_sessions_do_not_make_the_stopped_unit_a_leftover(self) -> None:
        self.herdr_replies(["unrelated"])
        saved = entry(unit("named-only", 101, tmux_session="same-name-as-tmux", session_host="herdr"))
        self.create([saved])
        report = self.perform(saved)
        self.assertEqual(report["stop_issues"], [])
        self.assertEqual(self.herdr_commands()[0], ["--session", "same-name-as-tmux", "server", "stop"])

    def test_herdr_unit_leftover_or_unreadable_stop_records_its_host(self) -> None:
        for failure in ("still running", "stop failed", "listing failed"):
            with self.subTest(failure=failure):
                self.herdr_replies(["same-name-as-tmux", "unrelated"], stop_error=failure == "stop failed")
                if failure == "listing failed":
                    path = self.root / "herdr-responses.json"
                    replies = cast(dict[str, object], json.loads(path.read_text()))
                    replies["--session default session list --json"] = {"code": 1, "stderr": "fixture unreadable"}
                    _ = path.write_text(json.dumps(replies))
                owner = unit(failure, 101, tmux_session="same-name-as-tmux", session_host="herdr")
                saved = entry(owner)
                self.create([saved])
                report = self.perform(saved)
                self.assertEqual(report["stop_issues"], [record.UnitSessionLeftRunning(
                    kind="unit session left running", at=NOW, host="herdr",
                    session_name="same-name-as-tmux", cause="stop not confirmed")])
                self.assertEqual(report["progress"]["kind"], "stopped")

    def test_herdr_unit_cleanup_retry_does_not_signal_the_stopped_owner(self) -> None:
        saved = entry(unit("retry", 101, tmux_session="same-name-as-tmux", session_host="herdr"))
        saved["progress"] = record.SessionStopped(kind="stopped", at=NOW)
        saved["stop_issues"] = [record.UnitSessionLeftRunning(kind="unit session left running", at=NOW,
            host="herdr", session_name="same-name-as-tmux", cause="stop not confirmed")]
        self.create([saved])
        report = stop.stop_session(saved,
            kill=lambda _pid, _sent: self.fail("stopped owner signalled"),
            is_alive=lambda _pid: False, clock=lambda: NOW_UTC,
            codex_mesh=lambda _args: self.fail("unexpected Codex cleanup"),
            tmux=lambda _args: self.fail("herdr retry touched tmux"))
        self.assertEqual(report["stop_issues"], [])
        self.assertEqual(self.herdr_commands()[0], ["--session", "same-name-as-tmux", "server", "stop"])

    def test_failed_herdr_stop_with_no_running_session_clears_cleanup(self) -> None:
        for error in (herdr.HerdrFailure("stop failed"), OSError("stop failed"),
                      subprocess.TimeoutExpired("herdr", 10)):
            with self.subTest(error=type(error).__name__):
                saved = entry(unit(type(error).__name__, 101,
                    tmux_session="same-name-as-tmux", session_host="herdr"))
                saved["progress"] = record.SessionStopped(kind="stopped", at=NOW)
                saved["stop_issues"] = [record.UnitSessionLeftRunning(
                    kind="unit session left running", at=NOW, host="herdr",
                    session_name="same-name-as-tmux", cause="stop not confirmed")]
                self.create([saved])
                with patch.object(herdr, "stop_session", side_effect=error) as stop_server, \
                        patch.object(herdr, "live_sessions", return_value=[]) as list_sessions:
                    report = self.perform(saved)
                stop_server.assert_called_once_with("same-name-as-tmux")
                list_sessions.assert_called_once_with()
                self.assertEqual(report["stop_issues"], [])
                self.assertEqual(report["progress"]["kind"], "stopped")

    def test_failed_herdr_stop_with_a_running_session_keeps_cleanup_pending(self) -> None:
        saved = entry(unit("still-running", 101,
            tmux_session="same-name-as-tmux", session_host="herdr"))
        self.create([saved])
        running = [herdr.SessionEntry(name="same-name-as-tmux", running=True,
            default=False, session_dir="/fixture", socket_path="/fixture/socket")]
        with patch.object(herdr, "stop_session", side_effect=herdr.HerdrFailure("stop failed")), \
                patch.object(herdr, "live_sessions", return_value=running) as list_sessions:
            report = self.perform(saved)
        list_sessions.assert_called_once_with()
        self.assertEqual(report["stop_issues"], [record.UnitSessionLeftRunning(
            kind="unit session left running", at=NOW, host="herdr",
            session_name="same-name-as-tmux", cause="stop not confirmed")])

    def test_failed_herdr_stop_with_unreadable_liveness_keeps_cleanup_pending(self) -> None:
        for error in (herdr.HerdrFailure("list failed"), OSError("list failed"),
                      subprocess.TimeoutExpired("herdr", 10)):
            with self.subTest(error=type(error).__name__):
                saved = entry(unit(type(error).__name__, 101,
                    tmux_session="same-name-as-tmux", session_host="herdr"))
                self.create([saved])
                with patch.object(herdr, "stop_session", side_effect=herdr.HerdrFailure("stop failed")), \
                        patch.object(herdr, "live_sessions", side_effect=error) as list_sessions:
                    report = self.perform(saved)
                list_sessions.assert_called_once_with()
                self.assertEqual(report["stop_issues"], [record.UnitSessionLeftRunning(
                    kind="unit session left running", at=NOW, host="herdr",
                    session_name="same-name-as-tmux", cause="stop not confirmed")])

    def test_herdr_unit_with_lost_identity_is_left_running_without_cleanup(self) -> None:
        owner = unit("identity", 101, tmux_session="same-name-as-tmux", session_host="herdr")
        saved = entry(owner)
        self.create([saved])
        changed = unit("identity", 202, tmux_session="same-name-as-tmux", session_host="herdr")
        report = stop.stop_session(saved,
            kill=lambda _pid, _sent: self.fail("changed owner signalled"),
            fresh_inventory=lambda _login, _scope: machine_inventory([changed]), clock=lambda: NOW_UTC,
            tmux=lambda _args: self.fail("changed owner tmux cleanup"))
        self.assertEqual(self.herdr_commands(), [])
        self.assertEqual(report["progress"]["kind"], "process identity lost")
        self.assertEqual(report["stop_issues"], [record.UnitSessionLeftRunning(
            kind="unit session left running", at=NOW, host="herdr",
            session_name="same-name-as-tmux", cause="owner identity lost")])

    def test_herdr_unit_cleanup_stops_when_another_request_takes_the_claim(self) -> None:
        owner = unit("claim", 101, tmux_session="same-name-as-tmux", session_host="herdr")
        saved = entry(owner)
        self.create([saved])

        def replace_request(_name: str) -> None:
            def change(current: record.SessionShutdown) -> None:
                current["request_id"] = "new-request"
            _ = record.update_session(LOGIN, owner["session_id"], change)

        with patch.object(herdr, "stop_session", side_effect=replace_request) as stop_server, \
                patch.object(herdr, "live_sessions") as list_sessions:
            report = self.perform(saved)
        stop_server.assert_called_once_with("same-name-as-tmux")
        list_sessions.assert_not_called()
        self.assertEqual(report["request_id"], "new-request")
        self.assertEqual(report["progress"]["kind"], "stopping")

    def test_herdr_unit_with_an_unexited_claude_never_stops_its_server(self) -> None:
        owner = unit("busy-owner", 101, tmux_session="same-name-as-tmux", session_host="herdr")
        saved = entry(owner)
        self.create([saved])
        report = stop.stop_session(saved, kill=lambda _pid, _sent: None,
            is_alive=lambda _pid: True, fresh_inventory=lambda _login, _scope: machine_inventory([owner]),
            clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0,
            tmux=lambda _args: self.fail("herdr owner touched tmux"),
            sleep=lambda _seconds: None, session_record_exists=lambda _pid: True)
        self.assertEqual(self.herdr_commands(), [])
        self.assertEqual(report["progress"]["kind"], "stop failed")

    def test_process_identity_changed_before_first_signal_is_left_running(self) -> None:
        run_dir = str(self.root / 'run')
        stored = unit('reused', 101, tmux_session='reused-unit', run_dirs=[run_dir])
        changed = unit('reused', 202, tmux_session='reused-unit', run_dirs=[run_dir])
        self.create(records_for_test([entry(stored)]))
        killed: list[int] = []
        codex_calls: list[list[str]] = []
        report = stop.stop_session(self.stored_record(), kill=lambda pid, _signal: killed.append(pid), is_alive=lambda _pid: True, fresh_inventory=lambda _login, _scope: machine_inventory([changed]), clock=lambda: NOW_UTC, codex_mesh=lambda arguments: codex_calls.append(arguments) or 0, tmux=lambda _arguments: 1, sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
        self.assertEqual(killed, [])
        self.assertEqual(codex_calls, [])
        self.assertEqual(report['progress']['kind'], 'process identity lost')
        self.assertEqual(report['stop_issues'], [record.CodexServerLeftRunning(kind='codex server left running', at=NOW, run_dir=run_dir, cause='owner identity lost'), record.UnitSessionLeftRunning(kind='unit session left running', at=NOW, host="tmux", session_name='reused-unit', cause='owner identity lost')])
        self.assertNotEqual(report['progress']['kind'], 'stopping')

    def test_process_identity_changed_before_second_signal_is_not_signalled_again(self) -> None:
        stored = top_level('reused', 101)
        changed = top_level('reused', 202)
        inventories = FreshInventorySequence([machine_inventory([stored]), machine_inventory([changed]), machine_inventory([])])
        self.create(records_for_test([entry(stored)]))
        killed: list[int] = []
        report = stop.stop_session(self.stored_record(), kill=lambda pid, _signal: killed.append(pid), is_alive=lambda _pid: True, fresh_inventory=inventories, clock=lambda: NOW_UTC, codex_mesh=lambda _arguments: 0, tmux=lambda _arguments: 1, sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
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
        report = stop.stop_session(self.stored_record(), kill=kill, is_alive=lambda _pid: alive, fresh_inventory=inventories, clock=lambda: NOW_UTC, codex_mesh=lambda _arguments: 0, tmux=lambda _arguments: 1, sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
        self.assertEqual(sent, [signal.SIGTERM, signal.SIGTERM])
        self.assertEqual(report['progress']['kind'], 'stopped')

    def test_pid_surviving_two_sigterms_is_failed_without_sigkill(self) -> None:
        stored = top_level('stuck', 101)
        self.create(records_for_test([entry(stored)]))
        sent: list[int] = []
        report = stop.stop_session(self.stored_record(), kill=lambda _pid, sent_signal: sent.append(sent_signal), is_alive=lambda _pid: True, fresh_inventory=lambda _login, _scope: machine_inventory([stored]), clock=lambda: NOW_UTC, codex_mesh=lambda _arguments: 0, tmux=lambda _arguments: 1, sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
        self.assertEqual(sent, [signal.SIGTERM, signal.SIGTERM])
        self.assertNotIn(signal.SIGKILL, sent)
        progress = report['progress']
        self.assertEqual(progress['kind'], 'stop failed')
        if progress['kind'] != 'stop failed':
            self.fail('expected stop failure')
        self.assertEqual(progress['reason'], 'alive after two SIGTERMs')
        self.assertEqual(report['stop_issues'], [record.StillRunningAfterStop(kind='still running', at=NOW, reason='alive after two SIGTERMs')])
        self.assertNotEqual(report['progress']['kind'], 'stopping')

    def test_a_new_stop_pass_replaces_an_entrys_previous_issues(self) -> None:
        stored = top_level('stuck', 101)
        stored_entry = entry(stored)
        stored_entry['stop_issues'] = [record.AccountUnreadableAtStop(kind='account unreadable', at=NOW)]
        self.create(records_for_test([stored_entry], state='stopping'))
        report = stop.stop_session(self.stored_record(), kill=lambda _pid, _signal: None, is_alive=lambda _pid: True, fresh_inventory=lambda _login, _scope: machine_inventory([stored]), clock=lambda: NOW_UTC, codex_mesh=lambda _arguments: 0, tmux=lambda _arguments: 1, sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
        self.assertEqual(report['stop_issues'], [record.StillRunningAfterStop(kind='still running', at=NOW, reason='alive after two SIGTERMs')])

    def test_showrunner_codex_server_is_stopped_once_after_exit(self) -> None:
        run_dir = str(self.root / 'showrunner-run')
        stored = showrunner('runner', 101, run_dirs=[run_dir], codex_servers=[server(run_dir, 900)])
        self.create(records_for_test([entry(stored)]))
        codex_calls: list[list[str]] = []
        report = stop.stop_session(self.stored_record(), kill=lambda _pid, _signal: None, is_alive=lambda _pid: False, fresh_inventory=lambda _login, _scope: machine_inventory([stored]), clock=lambda: NOW_UTC, codex_mesh=lambda arguments: codex_calls.append(arguments) or 0, tmux=lambda _arguments: 1, sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
        self.assertEqual(codex_calls, [['stop', '--session-dir', run_dir]])
        self.assertEqual(report['progress'], record.SessionStopped(kind='stopped', at=NOW))
        self.assertEqual(report['stop_issues'], [])

    def test_unconfirmed_codex_server_and_unit_tmux_leave_stop_partial(self) -> None:
        run_dir = str(self.root / 'unit-run')
        stored = unit('unit', 101, tmux_session='unit-exact', run_dirs=[run_dir])
        self.create(records_for_test([entry(stored)]))
        report = stop.stop_session(self.stored_record(), kill=lambda _pid, _signal: None, is_alive=lambda _pid: False, fresh_inventory=lambda _login, _scope: machine_inventory([stored]), clock=lambda: NOW_UTC, codex_mesh=lambda _arguments: 1, tmux=lambda _arguments: 0, sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
        self.assertEqual(report['progress'], record.SessionStopped(kind='stopped', at=NOW))
        self.assertEqual(report['stop_issues'], [record.CodexServerLeftRunning(kind='codex server left running', at=NOW, run_dir=run_dir, cause='stop not confirmed'), record.UnitSessionLeftRunning(kind='unit session left running', at=NOW, host="tmux", session_name='unit-exact', cause='stop not confirmed')])

    def test_second_stop_retries_only_the_cleanup_left_by_the_first_stop(self) -> None:
        runs = [str(self.root / name) for name in ("resolved", "pending")]
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
            clock=lambda: NOW_UTC, codex_mesh=codex, tmux=lambda _args: 0,
        )
        expected_issues = [
            record.CodexServerLeftRunning(kind="codex server left running", at=NOW, run_dir=runs[1], cause="stop not confirmed"),
            record.UnitSessionLeftRunning(kind="unit session left running", at=NOW, host="tmux", session_name="tmux-owner", cause="stop not confirmed"),
        ]
        self.assertEqual(first["progress"], record.SessionStopped(kind="stopped", at=NOW))
        self.assertEqual(first["stop_issues"], expected_issues)
        self.assertEqual(first_codex, [["stop", "--session-dir", run] for run in runs])
        calls: list[list[str]] = []
        tmux_calls: list[list[str]] = []
        second = stop.stop_session(
            first, kill=lambda _pid, _sent: self.fail("a stopped owner was signalled"),
            is_alive=lambda _pid: False,
            fresh_inventory=lambda _login, _only: self.fail("cleanup retry inspected the owner"),
            clock=lambda: NOW_UTC + timedelta(seconds=1),
            codex_mesh=lambda args: calls.append(args) or 0,
            tmux=lambda args: tmux_calls.append(args) or (1 if args[0] == "has-session" else 0),
        )
        self.assertEqual(calls, [["stop", "--session-dir", runs[1]]])
        self.assertEqual(tmux_calls, [["kill-session", "-t", "=tmux-owner"], ["has-session", "-t", "=tmux-owner"]])
        self.assertEqual(second["progress"], first["progress"])
        self.assertEqual(second["stop_issues"], [])

    def test_deleted_server_record_does_not_hide_a_surviving_server_pid(self) -> None:
        run_dir = self.root / 'live-server'
        run_dir.mkdir()
        server_file = run_dir / 'mesh_server.json'
        _ = server_file.write_text(json.dumps({'pid': 900}), encoding='utf-8')
        stored = showrunner('runner', 101, run_dirs=[str(run_dir)])
        self.create(records_for_test([entry(stored)]))

        def codex(arguments: list[str]) -> int:
            self.assertEqual(arguments, ['stop', '--session-dir', str(run_dir)])
            server_file.unlink()
            return 0
        report = stop.stop_session(self.stored_record(), kill=lambda _pid, _signal: None, is_alive=lambda pid: pid == 900, fresh_inventory=lambda _login, _scope: machine_inventory([stored]), clock=lambda: NOW_UTC, codex_mesh=codex, tmux=lambda _arguments: 1, sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
        self.assertNotEqual(report['progress']['kind'], 'stopping')
        self.assertEqual(report['stop_issues'], [record.CodexServerLeftRunning(kind='codex server left running', at=NOW, run_dir=str(run_dir), cause='stop not confirmed')])

    def test_tmux_runner_error_leaves_the_unit_session_unconfirmed(self) -> None:
        stored = unit('gone-unit', 101, tmux_session='unit-on-nix')
        self.create(records_for_test([entry(stored, 'waiting')]))

        def unavailable_tmux(_arguments: list[str]) -> int:
            raise OSError('tmux is not on PATH')
        report = stop.stop_session(self.stored_record(), kill=lambda _pid, _signal: self.fail('an absent unit was signalled'), is_alive=lambda _pid: False, fresh_inventory=lambda _login, _scope: machine_inventory([]), clock=lambda: NOW_UTC, codex_mesh=lambda _arguments: 0, tmux=unavailable_tmux, sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
        self.assertNotEqual(report['progress']['kind'], 'stopping')
        self.assertEqual(report['stop_issues'], [record.UnitSessionLeftRunning(kind='unit session left running', at=NOW, host="tmux", session_name='unit-on-nix', cause='stop not confirmed')])

    def test_ghostty_shell_with_a_different_parent_gets_no_sighup(self) -> None:
        host = inventory.WindowHost(kind='ghostty', window_shell=901, desktop=inventory.DesktopNotInSnapshot(kind='not in snapshot'))
        stored = top_level('window', 101, host=host)
        self.create(records_for_test([entry(stored)]))
        signals: list[tuple[int, int]] = []
        ps_rows: list[subprocess.CompletedProcess[str]] = [subprocess.CompletedProcess(args=['ps'], returncode=0, stdout='801 zsh\n', stderr=''), subprocess.CompletedProcess(args=['ps'], returncode=0, stdout='1 bash\n', stderr='')]
        with patch('stop.subprocess.run', side_effect=ps_rows) as ps:
            report = stop.stop_session(self.stored_record(), kill=lambda pid, sent_signal: signals.append((pid, sent_signal)), is_alive=lambda pid: pid == 901, fresh_inventory=lambda _login, _scope: machine_inventory([stored]), clock=lambda: NOW_UTC, codex_mesh=lambda _arguments: 0, tmux=lambda _arguments: 1, sleep=lambda _seconds: None, session_record_exists=lambda _pid: False)
        self.assertEqual(signals, [(101, signal.SIGTERM)])
        self.assertEqual(ps.call_count, 2)
        self.assertNotEqual(report['progress']['kind'], 'stopping')

    def test_stop_failed_record_can_be_retried_after_the_owner_becomes_idle(self) -> None:
        saved = entry(top_level("owner", 101))
        saved["progress"] = record.SessionStopFailed(kind="stop failed", at=NOW, reason="busy")
        self.create([saved])
        self.assertEqual(self.perform(saved)["progress"]["kind"], "stopped")

    def test_interrupted_cleanup_is_retried_when_the_claim_is_stale(self) -> None:
        pending = str(self.root / "pending")
        saved = entry(unit("owner", 101, run_dirs=[pending]))
        self.create([saved])
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            _ = stop.stop_session(
                saved, kill=lambda _pid, _sent: None, is_alive=lambda _pid: False,
                session_record_exists=lambda _pid: False,
                fresh_inventory=lambda _login, _only: machine_inventory([saved["session"]]),
                clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0,
                tmux=lambda _args: (_ for _ in ()).throw(RuntimeError("interrupted")),
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
            tmux=lambda args: 1 if args[0] == "has-session" else 0,
        )
        self.assertEqual(calls, [["stop", "--session-dir", pending]])
        self.assertEqual(finished["progress"]["kind"], "already gone")

    def test_process_disappearing_at_first_signal_is_already_gone(self) -> None:
        saved = entry(top_level("owner", 101))
        self.create([saved])
        def kill(_pid: int, _sent: int) -> None:
            raise ProcessLookupError()
        finished = stop.stop_session(
            saved, kill=kill, is_alive=lambda _pid: False,
            fresh_inventory=lambda _login, _only: machine_inventory([saved["session"]]),
            clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0, tmux=lambda _args: 1,
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
            tmux=lambda _args: self.fail("cleanup followed a refused signal"),
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
            clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0, tmux=lambda _args: 1,
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
                clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0, tmux=lambda _args: 1,
                sleep=lambda _seconds: None,
            )
        self.assertEqual(signals, [(101, signal.SIGTERM), (901, signal.SIGHUP)])
        self.assertEqual(finished["progress"]["kind"], "stopped")


    def test_moved_ghostty_showrunner_closes_its_recorded_shell_when_stopped(self) -> None:
        owner = showrunner("hana-id", 101)
        owner["host"] = inventory.WindowHost(kind="ghostty", window_shell=901,
            desktop=inventory.NamedDesktop(kind="named", name="Production"))
        saved = entry(owner)
        saved["target"] = "claude 3"
        self.create([saved])
        signals: list[tuple[int, int]] = []
        with patch.object(stop, "_ghostty_still_owns_shell", return_value=True):
            finished = stop.stop_session(
                saved, kill=lambda pid, sent: signals.append((pid, sent)), is_alive=lambda pid: pid == 901,
                session_record_exists=lambda _pid: False,
                fresh_inventory=lambda _login, _only: machine_inventory([saved["session"]]),
                clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0, tmux=lambda _args: 1,
                sleep=lambda _seconds: None,
            )
        self.assertEqual(signals, [(101, signal.SIGTERM), (901, signal.SIGHUP)])
        self.assertEqual(finished["progress"]["kind"], "stopped")

    def test_stop_snapshot_cannot_claim_a_new_request_for_the_same_session(self) -> None:
        old = entry(top_level("owner", 101))
        self.create([old])
        record.archive_session(LOGIN, "owner")
        current = entry(top_level("owner", 101))
        current["request_id"] = "new-request"
        self.create([current])
        signals: list[int] = []
        result = stop.stop_session(
            old, kill=lambda _pid, sent: signals.append(sent),
            is_alive=lambda _pid: False, session_record_exists=lambda _pid: False,
            fresh_inventory=lambda _login, _only: machine_inventory([current["session"]]),
            clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0,
            tmux=lambda _args: 1, sleep=lambda _seconds: None,
        )
        self.assertEqual(signals, [])
        self.assertEqual(result, current)
        self.assertEqual(self.stored_record(), current)

    def test_late_stop_outcome_leaves_a_replacement_relaunch_claim_unchanged(self) -> None:
        saved = entry(top_level("owner", 101))
        self.create([saved])
        replacement = record.SessionRestarting(
            kind="restarting", at=(NOW_UTC + timedelta(seconds=121)).isoformat(),
            prior=record.SessionStopped(kind="stopped", at=NOW),
        )
        real_update = record.update_session
        updates = 0

        def update(login: str, session_id: str, change: Callable[[record.SessionShutdown], None]) -> record.SessionShutdown:
            nonlocal updates
            updates += 1
            if updates == 2:
                def replace(current: record.SessionShutdown) -> None:
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
                tmux=lambda _args: self.fail("a replaced owner was cleaned"),
            )
        self.assertEqual(finished["progress"], replacement)
        self.assertEqual(finished["stop_issues"], [])
        self.assertEqual(self.stored_record(), finished)

    def test_late_stop_cleanup_leaves_a_replacement_request_untouched(self) -> None:
        saved = entry(unit("owner", 101, run_dirs=[str(self.root / "run")]))
        self.create([saved])

        def report(_login: str, _only: frozenset[str]) -> inventory.Inventory:
            def replace(current: record.SessionShutdown) -> None:
                current["request_id"] = "replacement"
                current["progress"] = record.SessionReadyToStop(kind="ready", at=NOW)
            _ = record.update_session(LOGIN, "owner", replace)
            return machine_inventory([])

        finished = stop.stop_session(
            saved, kill=lambda _pid, _sent: self.fail("a replaced owner was signalled"),
            fresh_inventory=report, clock=lambda: NOW_UTC,
            codex_mesh=lambda _args: self.fail("stale Codex cleanup ran"),
            tmux=lambda _args: self.fail("stale tmux cleanup ran"),
        )
        self.assertEqual(finished["request_id"], "replacement")
        self.assertEqual(finished["progress"], {"kind": "ready", "at": NOW})
        self.assertEqual(finished["stop_issues"], [])

    def test_expired_stop_worker_returns_the_replacement_workers_relaunch(self) -> None:
        saved = entry(unit("owner", 101, run_dirs=[str(self.root / "run")]))
        self.create([saved])
        paused = threading.Event()
        release = threading.Event()
        returned: list[record.SessionShutdown] = []
        errors: list[BaseException] = []

        def report(_login: str, _only: frozenset[str]) -> inventory.Inventory:
            paused.set()
            if not release.wait(2):
                raise RuntimeError("stop worker was not released")
            return machine_inventory([])

        def first_worker() -> None:
            try:
                returned.append(stop.stop_session(
                    saved, kill=lambda _pid, _sent: self.fail("expired worker sent a signal"),
                    fresh_inventory=report, clock=lambda: NOW_UTC,
                    codex_mesh=lambda _args: self.fail("expired worker cleaned a server"),
                    tmux=lambda _args: self.fail("expired worker cleaned tmux"),
                ))
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=first_worker)
        worker.start()
        try:
            self.assertTrue(paused.wait(2))
            replacement_time = NOW_UTC + timedelta(seconds=record.CLAIM_STALE_SECONDS)
            stopped = stop.stop_session(
                self.stored_record(), kill=lambda _pid, _sent: None,
                is_alive=lambda _pid: False, session_record_exists=lambda _pid: False,
                fresh_inventory=lambda _login, _only: machine_inventory([saved["session"]]),
                clock=lambda: replacement_time, codex_mesh=lambda _args: 0,
                tmux=lambda args: 1 if args[0] == "has-session" else 0,
            )
            self.assertEqual(stopped["progress"]["kind"], "stopped")
            prior = record.SessionStopped(kind="stopped", at=replacement_time.isoformat())

            def relaunch(current: record.SessionShutdown) -> None:
                current["progress"] = record.SessionRestarting(kind="restarting", at=replacement_time.isoformat(), prior=prior)

            replacement = record.update_session(LOGIN, "owner", relaunch)
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(returned, [replacement])
        self.assertEqual(self.stored_record(), replacement)

    def test_stop_cleanup_rechecks_ownership_between_codex_servers_and_tmux(self) -> None:
        runs = [str(self.root / name) for name in ("first", "second")]
        saved = entry(unit("owner", 101, run_dirs=runs))
        saved["progress"] = record.SessionStopped(kind="stopped", at=NOW)
        issues: list[record.SessionStopIssue] = [
            record.CodexServerLeftRunning(kind="codex server left running", at=NOW, run_dir=run, cause="stop not confirmed")
            for run in runs
        ]
        issues.append(record.UnitSessionLeftRunning(kind="unit session left running", at=NOW, host="tmux", session_name="tmux-owner", cause="stop not confirmed"))
        saved["stop_issues"] = issues
        self.create([saved])
        calls: list[list[str]] = []
        replacement = record.SessionRestarting(kind="restarting", at=NOW, prior=record.SessionStopped(kind="stopped", at=NOW))

        def codex(args: list[str]) -> int:
            calls.append(args)
            def replace(current: record.SessionShutdown) -> None:
                current["progress"] = replacement
            _ = record.update_session(LOGIN, "owner", replace)
            return 0

        finished = stop.stop_session(
            saved, kill=lambda _pid, _sent: self.fail("a stopped owner was signalled"),
            is_alive=lambda _pid: False, clock=lambda: NOW_UTC, codex_mesh=codex,
            tmux=lambda _args: self.fail("stale tmux cleanup ran"),
        )
        self.assertEqual(calls, [["stop", "--session-dir", runs[0]]])
        self.assertEqual(finished["progress"], replacement)
        self.assertEqual(finished["stop_issues"], saved["stop_issues"])

    def test_stop_rechecks_ownership_after_readiness_before_the_first_signal(self) -> None:
        saved = entry(top_level("owner", 101))
        self.create([saved])
        replacement = record.SessionRestarting(kind="restarting", at=NOW, prior=record.SessionStopped(kind="stopped", at=NOW))

        def blocker(_session: inventory.Session) -> record.ReadinessCheck:
            def replace(current: record.SessionShutdown) -> None:
                current["progress"] = replacement
            _ = record.update_session(LOGIN, "owner", replace)
            return record.ReadinessClear(kind="clear")

        with patch.object(settle, "blocker", blocker):
            finished = stop.stop_session(
                saved, kill=lambda _pid, _sent: self.fail("stale first signal ran"),
                fresh_inventory=lambda _login, _only: machine_inventory([saved["session"]]),
                clock=lambda: NOW_UTC,
                codex_mesh=lambda _args: self.fail("stale cleanup ran"),
                tmux=lambda _args: self.fail("stale cleanup ran"),
            )
        self.assertEqual(finished["progress"], replacement)

    def test_stop_rechecks_ownership_before_the_second_signal(self) -> None:
        saved = entry(top_level("owner", 101))
        self.create([saved])
        replacement = record.SessionRestarting(kind="restarting", at=NOW, prior=record.SessionStopped(kind="stopped", at=NOW))
        inventories = 0
        signals: list[int] = []

        def report(_login: str, _only: frozenset[str]) -> inventory.Inventory:
            nonlocal inventories
            inventories += 1
            if inventories == 2:
                def replace(current: record.SessionShutdown) -> None:
                    current["progress"] = replacement
                _ = record.update_session(LOGIN, "owner", replace)
            return machine_inventory([saved["session"]])

        finished = stop.stop_session(
            saved, kill=lambda _pid, sent: signals.append(sent), is_alive=lambda _pid: True,
            session_record_exists=lambda _pid: False, fresh_inventory=report,
            clock=lambda: NOW_UTC, sleep=lambda _seconds: None,
            codex_mesh=lambda _args: self.fail("stale cleanup ran"),
            tmux=lambda _args: self.fail("stale cleanup ran"),
        )
        self.assertEqual(signals, [signal.SIGTERM])
        self.assertEqual(finished["progress"], replacement)

    def test_stop_rechecks_ownership_between_forced_codex_seats(self) -> None:
        run = str(self.root / "run")
        saved = entry(unit("owner", 101, codex_servers=[server(run, 900, busy_seats=["one", "two"])]))
        saved["force"] = "now"
        self.create([saved])
        replacement = record.SessionRestarting(kind="restarting", at=NOW, prior=record.SessionStopped(kind="stopped", at=NOW))
        calls: list[list[str]] = []

        def codex(args: list[str]) -> int:
            calls.append(args)
            def replace(current: record.SessionShutdown) -> None:
                current["progress"] = replacement
            _ = record.update_session(LOGIN, "owner", replace)
            return 0

        finished = stop.stop_session(
            saved, kill=lambda _pid, _sent: self.fail("a replaced owner was signalled"),
            fresh_inventory=lambda _login, _only: machine_inventory([saved["session"]]),
            clock=lambda: NOW_UTC, codex_mesh=codex, tmux=lambda _args: self.fail("stale cleanup ran"),
        )
        self.assertEqual(calls, [["end", "--session-dir", run, "--to", "one"]])
        self.assertEqual(finished["progress"], replacement)

    def test_stop_cleanup_rechecks_ownership_before_the_ghostty_signal(self) -> None:
        host = inventory.WindowHost(kind="ghostty", window_shell=901, desktop=inventory.DesktopNotInSnapshot(kind="not in snapshot"))
        saved = entry(top_level("owner", 101, host=host))
        self.create([saved])
        replacement = record.SessionRestarting(kind="restarting", at=NOW, prior=record.SessionStopped(kind="stopped", at=NOW))

        def shell_owner(_pid: int) -> bool:
            def replace(current: record.SessionShutdown) -> None:
                current["progress"] = replacement
            _ = record.update_session(LOGIN, "owner", replace)
            return True

        signals: list[tuple[int, int]] = []
        with patch.object(stop, "_ghostty_still_owns_shell", shell_owner):
            finished = stop.stop_session(
                saved, kill=lambda pid, sent: signals.append((pid, sent)), is_alive=lambda pid: pid == 901,
                session_record_exists=lambda _pid: False,
                fresh_inventory=lambda _login, _only: machine_inventory([saved["session"]]),
                clock=lambda: NOW_UTC, codex_mesh=lambda _args: self.fail("stale cleanup ran"), tmux=lambda _args: 1,
            )
        self.assertEqual(signals, [(101, signal.SIGTERM)])
        self.assertEqual(finished["progress"], replacement)

    def test_stop_cleanup_rechecks_ownership_after_the_tmux_command(self) -> None:
        saved = entry(unit("owner", 101))
        saved["progress"] = record.SessionStopped(kind="stopped", at=NOW)
        saved["stop_issues"] = [record.UnitSessionLeftRunning(kind="unit session left running", at=NOW, host="tmux", session_name="tmux-owner", cause="stop not confirmed")]
        self.create([saved])
        replacement = record.SessionRestarting(kind="restarting", at=NOW, prior=record.SessionStopped(kind="stopped", at=NOW))
        calls: list[list[str]] = []

        def tmux(args: list[str]) -> int:
            calls.append(args)
            def replace(current: record.SessionShutdown) -> None:
                current["progress"] = replacement
            _ = record.update_session(LOGIN, "owner", replace)
            return 0

        finished = stop.stop_session(
            saved, kill=lambda _pid, _sent: self.fail("a stopped owner was signalled"),
            clock=lambda: NOW_UTC, codex_mesh=lambda _args: 0, tmux=tmux,
        )
        self.assertEqual(calls, [["kill-session", "-t", "=tmux-owner"]])
        self.assertEqual(finished["progress"], replacement)
        self.assertEqual(finished["stop_issues"], saved["stop_issues"])

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
            clock=lambda: instant, codex_mesh=lambda _args: 1, tmux=lambda _args: 0,
        )
        self.assertEqual(finished["progress"], {"kind": "stopped", "at": expected})
        self.assertEqual([issue["at"] for issue in finished["stop_issues"]], [expected, expected])


if __name__ == "__main__":
    _ = unittest.main()
