"""Behavioral tests for the account shutdown settle phase."""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, TypedDict, cast, final, override
from unittest.mock import ANY, call, patch

import inventory
import record
import remote
import settle
import shutdown
import stop as stop_work
import showrunner_footer
import conversation_pause
from account import Account
from user_action import ActionRequired, NoActionRequired, UserAction


LOGIN = "owner@example.com"
LABEL = "claude 2"
NOW = "2026-10-09T21:49:10+00:00"
NOW_UTC = datetime(2026, 10, 9, 21, 49, 10, tzinfo=timezone.utc)
NO_ACTION_REQUIRED = NoActionRequired()


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
            tmux_session=name,
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


def empty_shutdown_record(
    machine: str,
    scope: record.ShutdownScope,
) -> record.ShutdownRecord:
    return record.ShutdownRecord(
        login=LOGIN,
        label=LABEL,
        machine=machine,
        state="settling",
        requested_at=NOW,
        requested_by=record.FromTerminal(kind="terminal"),
        scope=scope,
        conductor=record.ConductorNotStarted(kind="not started"),
        force="wait for ready",
        entries=[],
        stop_issues=[],
    )


def empty_refresh_report(machine: str, scope: record.ShutdownScope) -> settle.RefreshReport:
    return settle.RefreshReport(
        record=empty_shutdown_record(machine, scope),
        verdicts=[],
    )


def completed_stop_report(current: record.ShutdownRecord) -> stop_work.StopReport:
    counts: dict[str, int] = {}
    for entry in current["entries"]:
        kind = entry["session"]["kind"]
        counts[kind] = counts.get(kind, 0) + 1
    return stop_work.StopReport(
        record=current,
        unattributed=[],
        counts=counts,
    )


@final
class SettleTests(unittest.TestCase):
    root: Path = Path()
    state_root: Path = Path()
    notifier_root: Path = Path()
    pause_root: Path = Path()
    send_log: Path = Path()
    notifier_log: Path = Path()
    notifier_stub: Path = Path()
    send_stub: Path = Path()
    ssh_stub: Path = Path()
    account = Account("claude", LOGIN, LABEL)

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state_root = self.root / "shutdown"
        self.notifier_root = self.root / "notifier"
        self.pause_root = self.root / "conversation-pause"
        self.send_log = self.root / "send.jsonl"
        self.notifier_log = self.root / "notifier.jsonl"
        self.notifier_root.mkdir()
        binary_root = self.root / "bin"
        binary_root.mkdir()
        self.notifier_stub = binary_root / "notifier"
        _ = self.notifier_stub.write_text(
            """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
with Path(os.environ["SHUTDOWN_NOTIFIER_LOG"]).open("a") as output:
    output.write(json.dumps(args) + "\\n")
verb, instance = args
state = Path(os.environ["NOTIFIER_STATE_DIR"]) / instance / "state"
if state.exists() and verb in {"start", "stop"}:
    enabled = "1" if verb == "start" else "0"
    lines = state.read_text().splitlines()
    state.write_text("\\n".join(
        f"ENABLED={enabled}" if line.startswith("ENABLED=") else line
        for line in lines
    ) + "\\n")
""",
            encoding="utf-8",
        )
        self.notifier_stub.chmod(0o755)
        self.send_stub = binary_root / "send"
        _ = self.send_stub.write_text(
            """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

call = {"args": sys.argv[1:], "text": sys.stdin.read()}
with Path(os.environ["SHUTDOWN_SEND_LOG"]).open("a") as output:
    output.write(json.dumps(call) + "\\n")
printed = os.environ.get("SHUTDOWN_SEND_OUTPUT", "")
if printed:
    print(printed)
sys.exit(int(os.environ.get("SHUTDOWN_SEND_EXIT", "0")))
""",
            encoding="utf-8",
        )
        self.send_stub.chmod(0o755)
        self.ssh_stub = binary_root / "ssh"
        _ = self.ssh_stub.write_text(
            """#!/usr/bin/env python3
import os
import sys

mode = os.environ.get("FAKE_SSH_MODE", "unreachable")
if mode == "unreachable":
    sys.exit(255)
if mode == "unavailable":
    print("remote refused")
    print("rc=7")
    sys.exit(0)
print(os.environ.get("FAKE_SSH_OUTPUT", ""))
print("rc=0")
""",
            encoding="utf-8",
        )
        self.ssh_stub.chmod(0o755)
        environment = {
            **os.environ,
            "HOME": str(self.root),
            "PATH": f"{binary_root}{os.pathsep}{os.environ.get('PATH', '')}",
            "SHUTDOWN_STATE_DIR": str(self.state_root),
            "NOTIFIER_STATE_DIR": str(self.notifier_root),
            "CONVERSATION_PAUSE_STATE_DIR": str(self.pause_root),
            "SHOWRUNNER_STATE_DIR": str(self.root / "showrunner"),
            "PLAN_DELEGATE_HISTORY_DIR": str(self.root / "runs"),
            "SHUTDOWN_DELEGATE_ROOT": str(self.root / "delegate"),
            "AGENT_NOTES_DIR": str(self.root / "notes"),
            "SHUTDOWN_NOTIFIER": str(self.notifier_stub),
            "SHUTDOWN_NOTIFIER_LOG": str(self.notifier_log),
            "SHUTDOWN_SEND": str(self.send_stub),
            "SHUTDOWN_SEND_LOG": str(self.send_log),
        }
        environment_context: object = cast(
            object,
            self.enterContext(patch.dict(os.environ, environment, clear=True)),
        )
        del environment_context

    def write_instance(
        self,
        name: str,
        session_id: str,
        *,
        enabled: bool,
    ) -> Path:
        instance = self.notifier_root / name
        instance.mkdir()
        _ = (instance / "conf").write_text(
            f"TARGET=session:{session_id}\nEVERY=5\nCOMMAND=report\n",
            encoding="utf-8",
        )
        _ = (instance / "state").write_text(
            f"ENABLED={int(enabled)}\nNEXT_DUE=100\n",
            encoding="utf-8",
        )
        return instance

    def found_record(self) -> record.ShutdownRecord:
        found = record.find_live(LOGIN)
        self.assertEqual(found["kind"], "live")
        if found["kind"] != "live":
            self.fail("expected a live shutdown record")
        return found["record"]

    def logged_calls(self, path: Path) -> list[list[str]]:
        if not path.exists():
            return []
        calls: list[list[str]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            value = cast(object, json.loads(line))
            if not isinstance(value, list):
                raise AssertionError("notifier log entry is not a list")
            calls.append(cast(list[str], value))
        return calls

    def sent_calls(self) -> list[SentCall]:
        if not self.send_log.exists():
            return []
        calls: list[SentCall] = []
        for line in self.send_log.read_text(encoding="utf-8").splitlines():
            value = cast(object, json.loads(line))
            if not isinstance(value, dict):
                raise AssertionError("send log entry is not an object")
            fields = cast(dict[str, object], value)
            args = fields.get("args")
            text = fields.get("text")
            if not isinstance(args, list) or not isinstance(text, str):
                raise AssertionError("send log entry is invalid")
            raw_args = cast(list[object], args)
            if not all(isinstance(item, str) for item in raw_args):
                raise AssertionError("send arguments are invalid")
            calls.append(
                SentCall(args=cast(list[str], cast(object, raw_args)), text=text)
            )
        return calls

    def pause_record(
        self,
        session_id: str,
        *,
        instance: str | None = None,
        footer: str | None = None,
        kept_off: bool,
    ) -> Path:
        self.pause_root.mkdir(parents=True, exist_ok=True)
        path = self.pause_root / f"{session_id}.json"
        phase: dict[str, object] = (
            {"kind": "kept_off"}
            if kept_off
            else {"kind": "replying", "user_wrote_at": 100}
        )
        _ = path.write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "instances": [instance] if instance is not None else [],
                    "footers": [footer] if footer is not None else [],
                    "phase": phase,
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_alert_user_forwards_the_callers_action_for_account_wide_scope(
        self,
    ) -> None:
        action = ActionRequired("Follow the alert text.")
        with patch.object(
            settle,
            "send_message",
            return_value=settle.MessageSent(kind="sent"),
        ) as send:
            settle.alert_user(
                record.AllAccountSessions(kind="all account sessions"),
                "action needed",
                "fix it",
                action,
                machine="natedev",
            )
            settle.alert_user(
                record.SelectedSessions(
                    kind="selected", session_ids=["scratch"]
                ),
                "scoped action needed",
                "fix it",
                action,
            )

        send.assert_called_once_with(
            "user",
            "action needed",
            "fix it",
            action=action,
            need="decision",
            machine="natedev",
        )

    def test_clean_stop_reports_to_requesting_session_without_paging_user(
        self,
    ) -> None:
        claimed = empty_shutdown_record(
            "natedev", record.AllAccountSessions(kind="all account sessions")
        )
        claimed["requested_by"] = record.FromSession(
            kind="session", session_id="requester"
        )
        stopped = empty_shutdown_record(
            "natedev", record.AllAccountSessions(kind="all account sessions")
        )
        stopped["state"] = "down"

        settle.report_stop(claimed, [completed_stop_report(stopped)], [])

        calls = self.sent_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0]["args"],
            [
                "--to",
                "session:requester",
                "--from",
                "shutdown",
                "--summary",
                f"{LABEL} is down",
                "--key",
                f"shutdown-result-{LOGIN}",
            ],
        )
        self.assertIn("/shutdown restart", calls[0]["text"])

    def test_clean_stop_requested_from_terminal_sends_nothing(self) -> None:
        claimed = empty_shutdown_record(
            "natedev", record.AllAccountSessions(kind="all account sessions")
        )
        stopped = empty_shutdown_record(
            "natedev", record.AllAccountSessions(kind="all account sessions")
        )
        stopped["state"] = "down"

        settle.report_stop(claimed, [completed_stop_report(stopped)], [])

        self.assertEqual(self.sent_calls(), [])

    def test_partial_stop_alert_names_the_account_restart_action(
        self,
    ) -> None:
        claimed = empty_shutdown_record(
            "natedev", record.AllAccountSessions(kind="all account sessions")
        )
        claimed["requested_by"] = record.FromSession(
            kind="session", session_id="requester"
        )
        stopped = empty_shutdown_record(
            "natedev", record.AllAccountSessions(kind="all account sessions")
        )
        stopped["state"] = "stop partial"

        settle.report_stop(claimed, [completed_stop_report(stopped)], [])

        calls = self.sent_calls()
        self.assertEqual(
            [call["args"][1] for call in calls],
            ["user", "session:requester"],
        )
        self.assertIn("--need", calls[0]["args"])
        self.assertIn("decision", calls[0]["args"])
        action_index = calls[0]["args"].index("--action")
        self.assertEqual(
            calls[0]["args"][action_index + 1],
            f"Run /shutdown restart on {LABEL}.",
        )
        self.assertIn(f"shutdown-result-{LOGIN}", calls[1]["args"])
        self.assertIn(f"{LABEL}: stop partial", calls[1]["args"])

    def test_partial_stop_pages_when_requester_delivery_fails(self) -> None:
        claimed = empty_shutdown_record(
            "natedev", record.AllAccountSessions(kind="all account sessions")
        )
        claimed["requested_by"] = record.FromSession(
            kind="session", session_id="requester"
        )
        stopped = empty_shutdown_record(
            "natedev", record.AllAccountSessions(kind="all account sessions")
        )
        stopped["state"] = "stop partial"
        events: list[str] = []

        def record_alert(
            _scope: record.ShutdownScope,
            _summary: str,
            _text: str,
            action: ActionRequired,
            *,
            machine: str = "",
        ) -> None:
            del action, machine
            events.append("user alert")

        def fail_requester_delivery(
            _recipient: str,
            _summary: str,
            _text: str,
            *,
            action: UserAction,
            need: Literal["note", "decision", "blocked"] = "note",
            machine: str = "",
            key: str = "",
        ) -> settle.MessageDelivery:
            del need, machine, key, action
            events.append("requester result")
            raise RuntimeError("requester delivery failed")

        with (
            patch.object(
                settle, "alert_user", side_effect=record_alert
            ) as alert,
            patch.object(
                settle,
                "send_message",
                side_effect=fail_requester_delivery,
            ) as send,
            self.assertRaisesRegex(RuntimeError, "requester delivery failed"),
        ):
            settle.report_stop(claimed, [completed_stop_report(stopped)], [])

        self.assertEqual(events, ["user alert", "requester result"])
        alert.assert_called_once()
        send.assert_called_once_with(
            "session:requester",
            f"{LABEL}: stop partial",
            ANY,
            action=NO_ACTION_REQUIRED,
            key=f"shutdown-result-{LOGIN}",
        )

    def test_partial_stop_reports_to_requester_when_user_alert_fails(
        self,
    ) -> None:
        claimed = empty_shutdown_record(
            "natedev", record.AllAccountSessions(kind="all account sessions")
        )
        claimed["requested_by"] = record.FromSession(
            kind="session", session_id="requester"
        )
        stopped = empty_shutdown_record(
            "natedev", record.AllAccountSessions(kind="all account sessions")
        )
        stopped["state"] = "stop partial"
        events: list[str] = []

        def fail_user_alert(
            _scope: record.ShutdownScope,
            _summary: str,
            _text: str,
            action: ActionRequired,
            *,
            machine: str = "",
        ) -> None:
            del action, machine
            events.append("user alert")
            raise RuntimeError("user alert failed")

        def record_requester_delivery(
            _recipient: str,
            _summary: str,
            _text: str,
            *,
            action: UserAction,
            need: Literal["note", "decision", "blocked"] = "note",
            machine: str = "",
            key: str = "",
        ) -> settle.MessageDelivery:
            del need, machine, key, action
            events.append("requester result")
            return settle.MessageSent(kind="sent")

        with (
            patch.object(
                settle,
                "alert_user",
                side_effect=fail_user_alert,
            ) as alert,
            patch.object(
                settle,
                "send_message",
                side_effect=record_requester_delivery,
            ) as send,
            self.assertRaisesRegex(RuntimeError, "user alert failed"),
        ):
            settle.report_stop(claimed, [completed_stop_report(stopped)], [])

        self.assertEqual(events, ["user alert", "requester result"])
        alert.assert_called_once()
        send.assert_called_once_with(
            "session:requester",
            f"{LABEL}: stop partial",
            ANY,
            action=NO_ACTION_REQUIRED,
            key=f"shutdown-result-{LOGIN}",
        )

    def test_partial_scoped_stop_reports_only_to_requester(self) -> None:
        scope = record.SelectedSessions(
            kind="selected", session_ids=["requester"]
        )
        claimed = empty_shutdown_record("natedev", scope)
        claimed["requested_by"] = record.FromSession(
            kind="session", session_id="requester"
        )
        stopped = empty_shutdown_record("natedev", scope)
        stopped["state"] = "stop partial"

        settle.report_stop(claimed, [completed_stop_report(stopped)], [])

        calls = self.sent_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["args"][1], "session:requester")

    def test_scoped_holdout_reports_only_to_requester(self) -> None:
        current = empty_shutdown_record(
            "natedev",
            record.SelectedSessions(kind="selected", session_ids=["requester"]),
        )
        current["requested_by"] = record.FromSession(
            kind="session", session_id="requester"
        )
        report = settle.RefreshReport(
            record=current,
            verdicts=[
                settle.EntryVerdict(
                    session_id="requester",
                    verdict=settle.Holdout(
                        kind="holdout",
                        line="natedev top-level requester: busy",
                    ),
                )
            ],
        )

        settle._send_holdout_alert([report], {})  # pyright: ignore[reportPrivateUsage]

        calls = self.sent_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["args"][1], "session:requester")

    def test_begin_stops_only_enabled_timers_in_the_account_set(self) -> None:
        selected = self.write_instance(
            "selected-report", "selected", enabled=True
        )
        other_account = self.write_instance(
            "other-account-report", "other-account", enabled=True
        )
        report = machine_inventory(
            [top_level("selected", "Selected", timers=[selected.name])]
        )

        with (
            patch.object(settle, "run_inventory", return_value=report),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
        ):
            created = settle.begin(LOGIN)

        self.assertEqual(created["state"], "settling")
        self.assertTrue((selected / "state").read_text().startswith("ENABLED=0\n"))
        self.assertTrue(
            (other_account / "state").read_text().startswith("ENABLED=1\n")
        )
        self.assertEqual(
            self.logged_calls(self.notifier_log),
            [["stop", selected.name]],
        )
        self.assertEqual(
            created["entries"][0]["timers"],
            [
                record.TimerRestore(
                    instance=selected.name,
                    was_enabled=True,
                    footer=record.NoFooter(kind="no footer"),
                )
            ],
        )

    def test_cancel_keeps_a_released_kept_off_pause_off(self) -> None:
        paused = self.write_instance("delegate-paused", "session", enabled=False)
        pause_path = self.pause_record(
            "session", instance=paused.name, kept_off=True
        )
        report = machine_inventory(
            [top_level("session", "Paused", timers=[paused.name])]
        )

        with (
            patch.object(settle, "run_inventory", return_value=report),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
        ):
            created = settle.begin(LOGIN)
            cancel_result = settle.cancel(self.account, here=True)

        self.assertEqual(cancel_result, 0)
        self.assertFalse(pause_path.exists())
        self.assertEqual(created["entries"][0]["timers"][0]["was_enabled"], False)
        self.assertTrue((paused / "state").read_text().startswith("ENABLED=0\n"))
        self.assertEqual(self.logged_calls(self.notifier_log), [])

    def test_cancel_asks_the_conductor_machine_before_touching_local_state(
        self,
    ) -> None:
        local = empty_shutdown_record(
            "natedev", record.AllAccountSessions(kind="all account sessions")
        )
        record.create(local)
        stopping_line = (
            f"shutdown of {LABEL} is already stopping; "
            + "/shutdown restart brings it back once it is down"
        )
        output = io.StringIO()

        with (
            patch.object(
                settle, "run_remote", return_value=(1, stopping_line)
            ) as remote,
            patch.object(settle, "_cancel_local") as cancel_local,
            redirect_stdout(output),
        ):
            result = settle.cancel(self.account)

        self.assertEqual(result, 1)
        remote.assert_called_once_with(["cancel", LOGIN, "--here"])
        cancel_local.assert_not_called()
        self.assertEqual(self.found_record()["state"], "settling")
        self.assertEqual(output.getvalue(), stopping_line + "\n")

    def test_showrunner_message_waits_until_every_unit_is_ready(self) -> None:
        runner = showrunner("runner", "Showrunner")
        director = unit("unit", "Unit")
        report = machine_inventory([runner, director])
        remote = empty_refresh_report(
            "Mac", record.AllAccountSessions(kind="all account sessions")
        )
        with (
            patch.object(settle, "run_inventory", return_value=report),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
            patch.object(
                settle,
                "_remote_record",
                return_value=settle.RemoteRefresh(kind="refresh", report=remote),
            ),
        ):
            _ = settle.begin(LOGIN)
            first_outcome = settle.conduct_cycle(LOGIN, {})

            def mark_unit_ready(current: record.ShutdownRecord) -> None:
                entry = next(
                    item
                    for item in current["entries"]
                    if item["session"]["session_id"] == "unit"
                )
                entry["progress"] = record.SessionReadyToStop(kind="ready", at=NOW)

            _ = record.update(LOGIN, mark_unit_ready)
            second_outcome = settle.conduct_cycle(LOGIN, {})

        self.assertEqual(first_outcome["kind"], "settlement pending")
        self.assertEqual(second_outcome["kind"], "settlement pending")
        recipients = [
            call["args"][call["args"].index("--to") + 1]
            for call in self.sent_calls()
        ]
        self.assertEqual(recipients, ["session:unit", "session:runner"])

    def test_queued_settle_message_records_reason_and_status(self) -> None:
        report = machine_inventory([top_level("session", "Work")])
        with (
            patch.object(settle, "run_inventory", return_value=report),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
            patch.dict(
                os.environ,
                {
                    "SHUTDOWN_SEND_EXIT": "1",
                    "SHUTDOWN_SEND_OUTPUT": "QUEUED: no live session",
                },
            ),
        ):
            _ = settle.begin(LOGIN)
            refreshed = settle.refresh(LOGIN, ["session"])

        entry = refreshed["record"]["entries"][0]
        self.assertEqual(
            entry["settle_message"],
            record.SettleMessageQueued(
                kind="queued", at=NOW, reason="no live session"
            ),
        )
        self.assertIn(
            "Work: waiting · message queued: no live session",
            "\n".join(settle.status_record_lines(refreshed["record"])),
        )

    def test_queued_settle_message_retries_only_after_five_minutes(self) -> None:
        report = machine_inventory([top_level("session", "Work")])
        with (
            patch.object(settle, "run_inventory", return_value=report),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
        ):
            _ = settle.begin(LOGIN)
        with (
            patch.object(settle, "run_inventory", return_value=report),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
            patch.dict(
                os.environ,
                {
                    "SHUTDOWN_SEND_EXIT": "1",
                    "SHUTDOWN_SEND_OUTPUT": "QUEUED: no live session",
                },
            ),
        ):
            _ = settle.refresh(LOGIN, ["session"])
        with (
            patch.object(settle, "run_inventory", return_value=report),
            patch.object(
                settle,
                "now_utc",
                return_value=NOW_UTC + timedelta(seconds=299),
            ),
            patch.dict(
                os.environ,
                {"SHUTDOWN_SEND_EXIT": "0", "SHUTDOWN_SEND_OUTPUT": "SENT: Work"},
            ),
        ):
            too_soon = settle.refresh(LOGIN, ["session"])
        with (
            patch.object(settle, "run_inventory", return_value=report),
            patch.object(
                settle,
                "now_utc",
                return_value=NOW_UTC + timedelta(seconds=300),
            ),
            patch.dict(
                os.environ,
                {"SHUTDOWN_SEND_EXIT": "0", "SHUTDOWN_SEND_OUTPUT": "SENT: Work"},
            ),
        ):
            delivered = settle.refresh(LOGIN, ["session"])

        self.assertEqual(
            too_soon["record"]["entries"][0]["settle_message"]["kind"],
            "queued",
        )
        self.assertEqual(
            delivered["record"]["entries"][0]["settle_message"],
            record.SettleMessageSent(kind="sent", at="2026-10-09T21:54:10+00:00"),
        )
        self.assertEqual(len(self.sent_calls()), 2)

    def test_conductor_reconsiders_a_queued_message_for_delivery(self) -> None:
        report = machine_inventory([top_level("session", "Work")])
        with (
            patch.object(settle, "run_inventory", return_value=report),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
        ):
            _ = settle.begin(LOGIN)

        def mark_queued(current: record.ShutdownRecord) -> None:
            current["entries"][0]["settle_message"] = record.SettleMessageQueued(
                kind="queued", at=NOW, reason="no live session"
            )

        _ = record.update(LOGIN, mark_queued)
        with (
            patch.object(settle, "run_inventory", return_value=report) as inventories,
            patch.object(settle, "now_utc", return_value=NOW_UTC),
        ):
            outcome = settle.conduct_cycle(LOGIN, {}, here=True)

        self.assertEqual(outcome["kind"], "settlement pending")
        self.assertEqual(inventories.call_count, 2)
        self.assertEqual(self.sent_calls(), [])

    def test_remote_stop_waits_past_the_old_owner_timeout_in_one_call(self) -> None:
        remote_record = empty_shutdown_record(
            "Mac", record.AllAccountSessions(kind="all account sessions")
        )
        remote_record["state"] = "down"
        elapsed_seconds = 0
        calls: list[remote.RemoteCallLimit] = []

        def delayed_remote(
            arguments: list[str],
            stdin: str = "",
            limit: remote.RemoteCallLimit = remote.STANDARD_TIME_LIMIT,
        ) -> tuple[int, str]:
            nonlocal elapsed_seconds
            self.assertEqual(arguments, ["stop", LOGIN])
            self.assertEqual(stdin, "")
            calls.append(limit)
            elapsed_seconds = 201
            return (
                0,
                json.dumps(
                    {
                        "record": remote_record,
                        "unattributed": [],
                        "counts": {},
                    }
                ),
            )

        with patch.object(settle, "run_remote", side_effect=delayed_remote):
            outcome = settle.stop_remote(LOGIN)

        self.assertGreater(elapsed_seconds, max(120, 60 + 20 * 7))
        self.assertEqual(
            calls, [remote.WhileLinkAlive(kind="while link alive")]
        )
        self.assertEqual(outcome["kind"], "stopped")

    def test_conductor_decisions_drive_poll_stop_and_exit_actions(self) -> None:
        scope = record.AllAccountSessions(kind="all account sessions")
        session = top_level("work", "Work")
        with (
            patch.dict(
                os.environ,
                {"SHUTDOWN_STATE_DIR": str(self.root / "decision-pending")},
            ),
            patch.object(
                settle, "run_inventory", return_value=machine_inventory([session])
            ),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
        ):
            _ = settle.begin(LOGIN)
            pending = settle.conduct_cycle(LOGIN, {}, here=True)

        with (
            patch.dict(
                os.environ,
                {"SHUTDOWN_STATE_DIR": str(self.root / "decision-ready")},
            ),
            patch.object(
                settle, "run_inventory", return_value=machine_inventory([])
            ),
        ):
            record.create(empty_shutdown_record("natedev", scope))
            ready = settle.conduct_cycle(LOGIN, {}, here=True)

        with patch.dict(
            os.environ,
            {"SHUTDOWN_STATE_DIR": str(self.root / "decision-ended")},
        ):
            ended = settle.conduct_cycle(LOGIN, {}, here=True)

        self.assertEqual(
            [pending["kind"], ready["kind"], ended["kind"]],
            ["settlement pending", "ready to stop", "settlement ended"],
        )

        action_root = self.root / "decision-actions"
        action_record = empty_shutdown_record("natedev", scope)
        action_report = settle.RefreshReport(record=action_record, verdicts=[])
        decisions: list[settle.ConductorDecision] = [
            settle.SettlementPending(
                kind="settlement pending", reports=[action_report]
            ),
            settle.ReadyToStop(kind="ready to stop", reports=[action_report]),
        ]
        with patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": str(action_root)}):
            record.create(action_record)
            with (
                patch.object(settle, "conduct_cycle", side_effect=decisions) as cycle,
                patch.object(time, "sleep") as sleep,
                patch.object(
                    stop_work, "claim_stop_as_conductor", return_value=True
                ) as claim,
                patch.object(settle, "_stop_machines", return_value=([], [])) as stop,
                patch.object(settle, "report_stop") as alert,
            ):
                self.assertEqual(settle.conduct(LOGIN, here=True), 0)
        self.assertEqual(cycle.call_count, 2)
        # `time.sleep` is patched process-wide, so a timed subprocess wait under load
        # adds its own short polls; only the settle-interval sleeps belong to `conduct`.
        settle_sleeps = [
            sleep_call
            for sleep_call in sleep.call_args_list
            if sleep_call == call(settle.SETTLE_INTERVAL_SECONDS)
        ]
        self.assertEqual(settle_sleeps, [call(settle.SETTLE_INTERVAL_SECONDS)])
        claim.assert_called_once_with(LOGIN)
        stop.assert_called_once_with(LOGIN, [action_report])
        alert.assert_called_once()

        with (
            patch.object(
                settle,
                "conduct_cycle",
                return_value=settle.SettlementEnded(
                    kind="settlement ended", reason="cancelled"
                ),
            ),
            patch.object(stop_work, "claim_stop_as_conductor") as ended_claim,
        ):
            self.assertEqual(settle.conduct(LOGIN, here=True), 0)
        ended_claim.assert_not_called()

    def test_peer_claim_failure_closes_local_before_alert_without_stopping(
        self,
    ) -> None:
        scope = record.AllAccountSessions(kind="all account sessions")
        local = empty_shutdown_record("natedev", scope)
        remote_record = empty_shutdown_record("Mac", scope)
        remote_root = self.root / "peer-claim-remote"
        record.create(local)
        with patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": str(remote_root)}):
            record.create(remote_record)
        reports = [
            settle.RefreshReport(record=local, verdicts=[]),
            settle.RefreshReport(record=remote_record, verdicts=[]),
        ]
        alerted = False

        def check_alert(
            claimed: record.ShutdownRecord,
            _reports: list[stop_work.StopReport],
            issues: list[record.OrchestrationStopIssue],
        ) -> None:
            nonlocal alerted
            alerted = True
            self.assertEqual(claimed["label"], LABEL)
            local_closed = self.found_record()
            self.assertEqual(local_closed["state"], "stop partial")
            self.assertEqual(local_closed["stop_issues"], issues)
            with patch.dict(
                os.environ, {"SHUTDOWN_STATE_DIR": str(remote_root)}
            ):
                peer = record.find_live(LOGIN)
            self.assertEqual(peer["kind"], "live")
            if peer["kind"] == "live":
                self.assertEqual(peer["record"]["state"], "settling")

        with (
            patch.object(
                settle,
                "conduct_cycle",
                return_value=settle.ReadyToStop(
                    kind="ready to stop", reports=reports
                ),
            ),
            patch.object(
                settle,
                "claim_remote_stop",
                return_value=settle.RemoteStopClaimFailed(
                    kind="failed", reason="rc 4"
                ),
            ),
            patch.object(settle, "other_machine", return_value="Mac"),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
            patch.object(settle, "report_stop", side_effect=check_alert),
            patch.object(settle, "stop_remote") as remote_stop,
            patch.object(stop_work, "stop") as local_stop,
        ):
            result = settle.conduct(LOGIN)

        self.assertEqual(result, 0)
        self.assertTrue(alerted)
        remote_stop.assert_not_called()
        local_stop.assert_not_called()
        self.assertEqual(
            self.found_record()["stop_issues"],
            [
                record.StopClaimFailed(
                    kind="stop claim failed",
                    at=NOW,
                    machine="Mac",
                    reason="rc 4",
                )
            ],
        )

    def test_local_stop_exception_closes_local_after_peer_finishes_before_alert(
        self,
    ) -> None:
        scope = record.AllAccountSessions(kind="all account sessions")
        local = empty_shutdown_record("natedev", scope)
        remote_record = empty_shutdown_record("Mac", scope)
        remote_root = self.root / "local-failure-remote"
        record.create(local)
        with patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": str(remote_root)}):
            record.create(remote_record)
        reports = [
            settle.RefreshReport(record=local, verdicts=[]),
            settle.RefreshReport(record=remote_record, verdicts=[]),
        ]

        def claim_peer(
            _login: str, timing: record.StopTiming
        ) -> settle.RemoteStopClaimOutcome:
            with patch.dict(
                os.environ, {"SHUTDOWN_STATE_DIR": str(remote_root)}
            ):
                self.assertTrue(stop_work.claim_stop_as_peer(LOGIN, timing))
            return settle.RemoteStopClaimed(kind="claimed")

        def stop_peer(_login: str) -> settle.RemoteStopOutcome:
            def finish(current: record.ShutdownRecord) -> None:
                current["state"] = "down"

            with patch.dict(
                os.environ, {"SHUTDOWN_STATE_DIR": str(remote_root)}
            ):
                finished = record.update(LOGIN, finish)
            return settle.RemoteStopped(
                kind="stopped", report=completed_stop_report(finished)
            )

        def check_alert(
            claimed: record.ShutdownRecord,
            _reports: list[stop_work.StopReport],
            issues: list[record.OrchestrationStopIssue],
            *,
            failed_records: list[record.ShutdownRecord],
        ) -> None:
            self.assertEqual(claimed["label"], LABEL)
            self.assertEqual(
                [failed["machine"] for failed in failed_records], ["natedev"]
            )
            self.assertEqual(self.found_record()["stop_issues"], issues)
            self.assertEqual(self.found_record()["state"], "stop partial")
            with patch.dict(
                os.environ, {"SHUTDOWN_STATE_DIR": str(remote_root)}
            ):
                peer = record.find_live(LOGIN)
            self.assertEqual(peer["kind"], "live")
            if peer["kind"] == "live":
                self.assertEqual(peer["record"]["state"], "down")

        with (
            patch.object(
                settle,
                "conduct_cycle",
                return_value=settle.ReadyToStop(
                    kind="ready to stop", reports=reports
                ),
            ),
            patch.object(settle, "claim_remote_stop", side_effect=claim_peer),
            patch.object(settle, "stop_remote", side_effect=stop_peer),
            patch.object(stop_work, "stop", side_effect=OSError("inventory failed")),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
            patch.object(settle, "report_stop", side_effect=check_alert),
        ):
            result = settle.conduct(LOGIN)

        self.assertEqual(result, 0)
        self.assertEqual(
            self.found_record()["stop_issues"],
            [
                record.MachineStopFailed(
                    kind="machine stop failed",
                    at=NOW,
                    machine="natedev",
                    reason="inventory failed",
                )
            ],
        )

    def test_local_stop_exception_alert_includes_persisted_session_issue(
        self,
    ) -> None:
        scope = record.AllAccountSessions(kind="all account sessions")
        local = empty_shutdown_record("natedev", scope)
        local["entries"] = [
            record.ShutdownSessionEntry(
                session=top_level("work", "Work"),
                timers=[],
                settle_message=record.SettleMessageNotSent(kind="not sent"),
                where=record.WhereNotSaid(kind="not said"),
                progress=record.SessionWaiting(kind="waiting"),
                stop_issues=[],
            )
        ]
        record.create(local)
        report = settle.RefreshReport(record=local, verdicts=[])

        def fail_after_persisting_issue(_login: str) -> stop_work.StopReport:
            def save_issue(current: record.ShutdownRecord) -> None:
                current["entries"][0]["stop_issues"] = [
                    record.NotReadyToStop(
                        kind="not ready to stop",
                        at=NOW,
                        status="idle",
                        progress="waiting",
                    )
                ]

            _ = record.update(LOGIN, save_issue)
            raise OSError("inventory failed")

        with (
            patch.object(
                settle,
                "conduct_cycle",
                return_value=settle.ReadyToStop(
                    kind="ready to stop", reports=[report]
                ),
            ),
            patch.object(stop_work, "stop", side_effect=fail_after_persisting_issue),
            patch.object(settle, "record_time", return_value=NOW),
        ):
            self.assertEqual(settle.conduct(LOGIN, here=True), 0)

        alert = self.sent_calls()[0]["text"]
        self.assertIn("natedev:\n  Work: not stopped: idle, waiting", alert)
        self.assertIn("natedev: stop failed: inventory failed", alert)

    def test_close_failed_stop_uses_shared_record_time(self) -> None:
        current = empty_shutdown_record(
            "natedev", record.AllAccountSessions(kind="all account sessions")
        )
        current["state"] = "stopping"
        record.create(current)
        timestamp = "2026-10-10T01:02:03+00:00"

        with (
            patch.object(settle, "record_time", return_value=timestamp) as clock,
            patch.object(settle, "local_machine", return_value="natedev"),
        ):
            self.assertEqual(
                shutdown.main(
                    ["close-failed-stop", LOGIN, "--reason", "inventory failed"]
                ),
                0,
            )

        clock.assert_called_once_with()
        self.assertEqual(self.found_record()["stop_issues"][0]["at"], timestamp)

    def test_remote_stop_failure_closes_both_and_records_failed_close_status(
        self,
    ) -> None:
        scope = record.AllAccountSessions(kind="all account sessions")
        local = empty_shutdown_record("natedev", scope)
        remote_record = empty_shutdown_record("Mac", scope)
        remote_root = self.root / "remote-failure-peer"
        record.create(local)
        with patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": str(remote_root)}):
            record.create(remote_record)
        reports = [
            settle.RefreshReport(record=local, verdicts=[]),
            settle.RefreshReport(record=remote_record, verdicts=[]),
        ]

        def claim_peer(_login: str, timing: record.StopTiming) -> settle.RemoteStopClaimOutcome:
            with patch.dict(
                os.environ, {"SHUTDOWN_STATE_DIR": str(remote_root)}
            ):
                self.assertTrue(stop_work.claim_stop_as_peer(LOGIN, timing))
            return settle.RemoteStopClaimed(kind="claimed")

        def stop_local(_login: str) -> stop_work.StopReport:
            def finish(current: record.ShutdownRecord) -> None:
                current["state"] = "down"

            return completed_stop_report(record.update(LOGIN, finish))

        def close_peer(
            arguments: list[str],
            stdin: str = "",
            limit: remote.RemoteCallLimit = remote.STANDARD_TIME_LIMIT,
        ) -> tuple[int, str]:
            self.assertEqual(stdin, "")
            self.assertEqual(limit, remote.STANDARD_TIME_LIMIT)
            if arguments == ["records", "--json", "--here"]:
                with patch.dict(
                    os.environ, {"SHUTDOWN_STATE_DIR": str(remote_root)}
                ):
                    peer = record.find_live(LOGIN)
                self.assertEqual(peer["kind"], "live")
                if peer["kind"] != "live":
                    self.fail("expected peer shutdown record")
                return 0, json.dumps([peer["record"]])
            self.assertEqual(
                arguments,
                ["close-failed-stop", LOGIN, "--reason", "rc 9"],
            )
            with (
                patch.dict(
                    os.environ, {"SHUTDOWN_STATE_DIR": str(remote_root)}
                ),
                patch.object(settle, "local_machine", return_value="Mac"),
                patch.object(settle, "now_utc", return_value=NOW_UTC),
            ):
                self.assertEqual(shutdown.main(arguments), 0)
            return 6, ""

        def check_alert(
            claimed: record.ShutdownRecord,
            _reports: list[stop_work.StopReport],
            issues: list[record.OrchestrationStopIssue],
            *,
            failed_records: list[record.ShutdownRecord],
        ) -> None:
            self.assertEqual(claimed["label"], LABEL)
            self.assertEqual(
                [failed["machine"] for failed in failed_records], ["Mac"]
            )
            local_closed = self.found_record()
            self.assertEqual(local_closed["state"], "stop partial")
            self.assertEqual(local_closed["stop_issues"], issues)
            self.assertEqual(
                issues[0]["reason"],
                "rc 9; its record could not be closed (rc 6)",
            )
            with patch.dict(
                os.environ, {"SHUTDOWN_STATE_DIR": str(remote_root)}
            ):
                peer = record.find_live(LOGIN)
            self.assertEqual(peer["kind"], "live")
            if peer["kind"] == "live":
                self.assertEqual(peer["record"]["state"], "stop partial")
                self.assertEqual(peer["record"]["stop_issues"][0]["reason"], "rc 9")

        with (
            patch.object(
                settle,
                "conduct_cycle",
                return_value=settle.ReadyToStop(
                    kind="ready to stop", reports=reports
                ),
            ),
            patch.object(settle, "claim_remote_stop", side_effect=claim_peer),
            patch.object(
                settle,
                "stop_remote",
                return_value=settle.RemoteStopFailed(kind="failed", reason="rc 9"),
            ),
            patch.object(stop_work, "stop", side_effect=stop_local),
            patch.object(settle, "run_remote", side_effect=close_peer),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
            patch.object(settle, "report_stop", side_effect=check_alert),
        ):
            result = settle.conduct(LOGIN)

        self.assertEqual(result, 0)

    def test_cancel_notifies_an_entry_with_a_queued_settle_message(self) -> None:
        report = machine_inventory([top_level("session", "Work")])
        with (
            patch.object(settle, "run_inventory", return_value=report),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
        ):
            _ = settle.begin(LOGIN)

        def mark_queued(current: record.ShutdownRecord) -> None:
            current["entries"][0]["settle_message"] = record.SettleMessageQueued(
                kind="queued", at=NOW, reason="no live session"
            )

        _ = record.update(LOGIN, mark_queued)
        result = settle.cancel(self.account, here=True)

        self.assertEqual(result, 0)
        calls = self.sent_calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["args"][0], "retire")
        self.assertIn("cancelled by the user", calls[1]["text"])

    def test_ready_rechecks_an_ahead_checkout_and_accepts_after_push(self) -> None:
        ahead = machine_inventory([top_level("session", "Work", ahead=2)])
        pushed = machine_inventory([top_level("session", "Work", ahead=0)])
        errors = io.StringIO()
        output = io.StringIO()
        with (
            patch.object(
                settle,
                "run_inventory",
                side_effect=[ahead, ahead, pushed],
            ),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
            patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "session"}),
        ):
            _ = settle.begin(LOGIN)
            with redirect_stderr(errors):
                refused = settle.ready("phase 5: push next")
            with redirect_stdout(output):
                accepted = settle.ready("phase 5: push next")

        self.assertEqual(refused, 2)
        self.assertEqual(accepted, 0)
        self.assertEqual(errors.getvalue(), "push work first\n")
        self.assertIn("ready for shutdown", output.getvalue())
        current = self.found_record()
        self.assertEqual(current["entries"][0]["progress"]["kind"], "ready")
        self.assertEqual(
            current["entries"][0]["where"],
            record.WhereSaid(kind="said", text="phase 5: push next", at=NOW),
        )

    def test_showrunner_with_merge_in_progress_does_not_count_ready(self) -> None:
        runner = showrunner("runner", "Showrunner")
        report = machine_inventory([runner])
        with (
            patch.object(settle, "run_inventory", return_value=report),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
        ):
            _ = settle.begin(LOGIN)

        def mark_ready(current: record.ShutdownRecord) -> None:
            current["entries"][0]["progress"] = record.SessionReadyToStop(
                kind="ready", at=NOW
            )

        current = record.update(LOGIN, mark_ready)
        remote = empty_refresh_report(
            "Mac", record.AllAccountSessions(kind="all account sessions")
        )
        with (
            patch.object(settle, "merge_in_progress", return_value=True),
            patch.object(settle, "run_inventory", return_value=report),
            patch.object(
                settle,
                "_remote_record",
                return_value=settle.RemoteRefresh(kind="refresh", report=remote),
            ),
        ):
            outcome = settle.conduct_cycle(LOGIN, {})

        self.assertEqual(outcome["kind"], "settlement pending")
        self.assertEqual(current["entries"][0]["progress"]["kind"], "ready")

    def test_remote_verdict_keeps_its_merge_and_form_holdout_without_local_tmux(
        self,
    ) -> None:
        runner = showrunner("remote-runner", "Remote Showrunner")
        remote_record = empty_shutdown_record(
            "Mac", record.AllAccountSessions(kind="all account sessions")
        )
        remote_record["requested_by"] = record.FromSession(
            kind="session", session_id="remote-runner"
        )
        remote_record["entries"] = [
            record.ShutdownSessionEntry(
                session=runner,
                timers=[],
                settle_message=record.SettleMessageNotSent(kind="not sent"),
                where=record.WhereNotSaid(kind="not said"),
                progress=record.SessionReadyToStop(kind="ready", at=NOW),
                stop_issues=[],
            )
        ]
        line = "Mac showrunner Remote Showrunner: idle, showing a form, merge in progress"
        remote_report = settle.RefreshReport(
            record=remote_record,
            verdicts=[
                settle.EntryVerdict(
                    session_id="remote-runner",
                    verdict=settle.Holdout(kind="holdout", line=line),
                )
            ],
        )
        tmux_log = self.root / "tmux-called"
        tmux = self.root / "tmux"
        _ = tmux.write_text(
            f"#!/bin/sh\ntouch {tmux_log}\nprintf 'form\\n'\n",
            encoding="utf-8",
        )
        tmux.chmod(0o755)
        local = machine_inventory([])
        with (
            patch.object(settle, "run_inventory", return_value=local),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
            patch.object(
                settle,
                "_remote_record",
                return_value=settle.RemoteRefresh(
                    kind="refresh", report=remote_report
                ),
            ),
            patch.dict(os.environ, {"SHUTDOWN_TMUX": str(tmux)}),
        ):
            _ = settle.begin(LOGIN)
            outcome = settle.conduct_cycle(LOGIN, {})

        self.assertEqual(outcome["kind"], "settlement pending")
        pending = cast(settle.SettlementPending, outcome)
        holdout_lines: list[str] = []
        for report in pending["reports"]:
            for result in report["verdicts"]:
                verdict = result["verdict"]
                if verdict["kind"] == "holdout":
                    holdout_lines.append(verdict["line"])
        self.assertEqual(
            holdout_lines,
            [line],
        )
        self.assertFalse(tmux_log.exists())

    def test_busy_seat_holds_then_becomes_passive_without_a_message(self) -> None:
        owner = top_level("owner", "Owner")
        busy = seat("seat", "Seat", owner="owner", status="busy")
        idle = seat("seat", "Seat", owner="owner", status="idle")
        busy_report = machine_inventory([owner, busy])
        idle_report = machine_inventory([owner, idle])
        with (
            patch.object(
                settle,
                "run_inventory",
                side_effect=[busy_report, busy_report, idle_report],
            ),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
        ):
            _ = settle.begin(LOGIN)

            def mark_owner_ready(current: record.ShutdownRecord) -> None:
                current["entries"][0]["progress"] = record.SessionReadyToStop(
                    kind="ready", at=NOW
                )

            _ = record.update(LOGIN, mark_owner_ready)
            held = settle.refresh(LOGIN)
            ready = settle.refresh(LOGIN, ["owner", "seat"])

        held_seat = next(
            entry
            for entry in held["record"]["entries"]
            if entry["session"]["session_id"] == "seat"
        )
        ready_seat = next(
            entry
            for entry in ready["record"]["entries"]
            if entry["session"]["session_id"] == "seat"
        )
        self.assertEqual(held_seat["progress"]["kind"], "waiting")
        self.assertEqual(ready_seat["progress"]["kind"], "passive seat ready")
        recipients = [
            call["args"][call["args"].index("--to") + 1]
            for call in self.sent_calls()
        ]
        self.assertEqual(recipients, ["session:owner"])

    def test_unattributed_session_is_status_only(self) -> None:
        timer = self.write_instance("unknown-report", "unknown", enabled=True)
        report = machine_inventory(
            [],
            unattributed=[
                inventory.UnattributedSession(
                    pid=999,
                    name="Unknown",
                    reason="account unreadable",
                )
            ],
        )
        errors = io.StringIO()
        with (
            patch.object(settle, "run_inventory", return_value=report),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
            patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "unknown"}),
        ):
            created = settle.begin(LOGIN)
            with redirect_stderr(errors):
                ready_result = settle.ready("not attributable")

        self.assertEqual(created["entries"], [])
        self.assertEqual(ready_result, 1)
        self.assertIn("no shutdown in progress", errors.getvalue())
        self.assertTrue((timer / "state").read_text().startswith("ENABLED=1\n"))
        self.assertEqual(self.logged_calls(self.notifier_log), [])
        self.assertEqual(self.sent_calls(), [])

    def test_ready_refuses_a_session_missing_from_the_fresh_inventory(self) -> None:
        original = top_level("session", "Work")
        errors = io.StringIO()
        with (
            patch.object(
                settle,
                "run_inventory",
                side_effect=[machine_inventory([original]), machine_inventory([])],
            ),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
            patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "session"}),
        ):
            _ = settle.begin(LOGIN)
            with redirect_stderr(errors):
                result = settle.ready("done")

        self.assertEqual(result, 2)
        self.assertEqual(
            errors.getvalue(),
            "this session is not in a fresh inventory of claude 2: not found\n",
        )
        self.assertEqual(
            self.found_record()["entries"][0]["progress"]["kind"], "waiting"
        )

    def test_ready_refuses_an_unattributed_copy_of_the_session(self) -> None:
        original = top_level("session", "Work")
        unknown = machine_inventory(
            [],
            unattributed=[
                inventory.UnattributedSession(
                    pid=original["pid"],
                    name="Work",
                    reason="process start mismatch",
                )
            ],
        )
        errors = io.StringIO()
        with (
            patch.object(
                settle,
                "run_inventory",
                side_effect=[machine_inventory([original]), unknown],
            ),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
            patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "session"}),
        ):
            _ = settle.begin(LOGIN)
            with redirect_stderr(errors):
                result = settle.ready("done")

        self.assertEqual(result, 2)
        self.assertIn("process start mismatch", errors.getvalue())
        self.assertEqual(
            self.found_record()["entries"][0]["progress"]["kind"], "waiting"
        )

    def test_refresh_keeps_an_unattributed_entry_as_a_reasoned_holdout(self) -> None:
        original = top_level("session", "Work")
        unknown = machine_inventory(
            [],
            unattributed=[
                inventory.UnattributedSession(
                    pid=original["pid"],
                    name="Work",
                    reason="account unreadable",
                )
            ],
        )
        with (
            patch.object(
                settle,
                "run_inventory",
                side_effect=[machine_inventory([original]), unknown],
            ),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
        ):
            _ = settle.begin(LOGIN)
            refreshed = settle.refresh(LOGIN, ["session"])

        entry = refreshed["record"]["entries"][0]
        self.assertEqual(entry["progress"]["kind"], "waiting")
        self.assertEqual(entry["settle_message"]["kind"], "not sent")
        self.assertEqual(
            refreshed["verdicts"][0]["verdict"],
            settle.Holdout(
                kind="holdout",
                line="natedev top-level Work: account unreadable",
            ),
        )

    def test_refresh_marks_a_gone_entry_settled_and_conduct_completes(self) -> None:
        original = top_level("session", "Work")
        gone = machine_inventory([])
        with (
            patch.object(
                settle,
                "run_inventory",
                side_effect=[machine_inventory([original]), gone, gone],
            ),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
        ):
            _ = settle.begin(LOGIN)
            refreshed = settle.refresh(LOGIN)
            outcome = settle.conduct_cycle(LOGIN, {}, here=True)

        self.assertEqual(
            refreshed["record"]["entries"][0]["progress"]["kind"],
            "already gone",
        )
        self.assertEqual(
            refreshed["verdicts"][0]["verdict"]["kind"], "counts ready"
        )
        self.assertEqual(outcome["kind"], "ready to stop")

    def test_refresh_returns_a_reappearing_entry_to_waiting(self) -> None:
        original = top_level("session", "Work")
        present = machine_inventory([original])
        with (
            patch.object(
                settle,
                "run_inventory",
                side_effect=[present, machine_inventory([]), present],
            ),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
        ):
            _ = settle.begin(LOGIN)
            _ = settle.refresh(LOGIN)
            returned = settle.refresh(LOGIN)

        self.assertEqual(
            returned["record"]["entries"][0]["progress"]["kind"], "waiting"
        )

    def test_seat_cannot_mark_itself_ready(self) -> None:
        worker = seat("seat", "Seat", owner=None)
        errors = io.StringIO()
        with (
            patch.object(settle, "run_inventory", return_value=machine_inventory([worker])),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
            patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "seat"}),
        ):
            _ = settle.begin(LOGIN)
            with redirect_stderr(errors):
                result = settle.ready("done")

        self.assertEqual(result, 2)
        self.assertIn("a seat is ready on its own", errors.getvalue())
        self.assertEqual(
            self.found_record()["entries"][0]["progress"]["kind"], "waiting"
        )

    def test_ordinary_ready_progress_never_settles_a_seat(self) -> None:
        worker = seat("seat", "Seat", owner=None, status="busy")
        report = machine_inventory([worker])
        with (
            patch.object(settle, "run_inventory", return_value=report),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
        ):
            _ = settle.begin(LOGIN)

            def mark_ordinary_ready(current: record.ShutdownRecord) -> None:
                current["entries"][0]["progress"] = record.SessionReadyToStop(
                    kind="ready", at=NOW
                )

            _ = record.update(LOGIN, mark_ordinary_ready)
            refreshed = settle.refresh(LOGIN)

        self.assertEqual(refreshed["verdicts"][0]["verdict"]["kind"], "holdout")

    def test_begin_with_no_sessions_still_creates_settling_record(self) -> None:
        report = machine_inventory([])
        with (
            patch.object(settle, "run_inventory", return_value=report),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
        ):
            created = settle.begin(LOGIN)

        self.assertEqual(created["state"], "settling")
        self.assertEqual(created["entries"], [])
        self.assertEqual(self.found_record(), created)

    def test_now_reports_forced_and_peer_with_nothing_to_force(self) -> None:
        current = empty_shutdown_record(
            "natedev", record.AllAccountSessions(kind="all account sessions")
        )
        record.create(current)
        output = io.StringIO()

        with (
            patch.object(settle, "_machine", return_value="natedev"),
            patch.object(
                settle,
                "run_remote",
                return_value=(0, "Mac: no shutdown in progress"),
            ),
            redirect_stdout(output),
        ):
            result = settle.now(self.account)

        self.assertEqual(result, 0)
        self.assertEqual(
            output.getvalue(),
            "natedev: stopping now\nMac: no shutdown in progress\n",
        )

    def test_now_with_nothing_to_force_on_either_machine_exits_one(self) -> None:
        output = io.StringIO()

        with (
            patch.object(settle, "_machine", return_value="natedev"),
            patch.object(
                settle,
                "run_remote",
                return_value=(0, "Mac: no shutdown in progress"),
            ),
            redirect_stdout(output),
        ):
            result = settle.now(self.account)

        self.assertEqual(result, 1)
        self.assertEqual(
            output.getvalue(),
            "natedev: no shutdown in progress\n"
            + "Mac: no shutdown in progress\n",
        )

    def test_now_reports_an_unreached_peer_and_exits_one(self) -> None:
        current = empty_shutdown_record(
            "natedev", record.AllAccountSessions(kind="all account sessions")
        )
        record.create(current)
        output = io.StringIO()
        errors = io.StringIO()

        with (
            patch.object(settle, "_machine", return_value="natedev"),
            patch.object(settle, "other_machine", return_value="Mac"),
            patch.object(settle, "run_remote", return_value=(255, "")),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            result = settle.now(self.account)

        self.assertEqual(result, 1)
        self.assertEqual(output.getvalue(), "natedev: stopping now\n")
        self.assertEqual(
            errors.getvalue(),
            "Mac not reached: run /shutdown now there when it is back\n",
        )
    def test_begin_releases_a_pause_through_the_python_api(self) -> None:
        report = machine_inventory([top_level("session", "Work")])
        with (
            patch.object(settle, "run_inventory", return_value=report),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
            patch.object(
                conversation_pause,
                "release",
                return_value=conversation_pause.NoPauseRecord.ABSENT,
            ) as release,
        ):
            _ = settle.begin(LOGIN)

        release.assert_called_once_with("session")

    def test_footer_only_pause_restores_footer_without_enabling_disabled_timer(
        self,
    ) -> None:
        timer = self.write_instance("showrunner-demo", "session", enabled=False)
        pause_path = self.pause_record(
            "session", footer="demo", kept_off=False
        )
        showrunner_footer.set_footer_state("demo", showrunner_footer.FooterState.OFF)
        report = machine_inventory([top_level("session", "Work")])
        with (
            patch.object(settle, "run_inventory", return_value=report),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
        ):
            created = settle.begin(LOGIN)
            result = settle.cancel(self.account, here=True)

        restore = created["entries"][0]["timers"][0]
        self.assertEqual(restore["instance"], timer.name)
        self.assertFalse(restore["was_enabled"])
        self.assertEqual(restore["footer"]["kind"], "footer")
        self.assertEqual(result, 0)
        self.assertFalse(pause_path.exists())
        self.assertFalse((timer / "state").read_text().startswith("ENABLED=1\n"))
        self.assertIs(
            showrunner_footer.footer_state("demo"), showrunner_footer.FooterState.ON
        )

    def test_kept_off_footer_only_pause_stays_off_after_cancel(self) -> None:
        timer = self.write_instance("showrunner-demo", "session", enabled=False)
        _ = self.pause_record("session", footer="demo", kept_off=True)
        showrunner_footer.set_footer_state("demo", showrunner_footer.FooterState.OFF)
        report = machine_inventory([top_level("session", "Work")])
        with (
            patch.object(settle, "run_inventory", return_value=report),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
        ):
            created = settle.begin(LOGIN)
            _ = settle.cancel(self.account, here=True)

        restore = created["entries"][0]["timers"][0]
        self.assertEqual(restore["instance"], timer.name)
        self.assertEqual(restore["footer"]["kind"], "no footer")
        self.assertIs(
            showrunner_footer.footer_state("demo"), showrunner_footer.FooterState.OFF
        )

    def test_begin_records_all_timer_states_before_a_stop_failure_and_rolls_back(
        self,
    ) -> None:
        first = self.write_instance("first", "session", enabled=True)
        second = self.write_instance("second", "session", enabled=True)
        report = machine_inventory(
            [top_level("session", "Work", timers=[first.name, second.name])]
        )
        real_run_notifier = settle.run_notifier
        stops = 0

        def fail_second_stop(verb: str, instance: str) -> None:
            nonlocal stops
            if verb == "stop":
                stops += 1
                if stops == 2:
                    raise RuntimeError("stop failed")
            real_run_notifier(verb, instance)

        with (
            patch.object(settle, "run_inventory", return_value=report),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
            patch.object(settle, "run_notifier", side_effect=fail_second_stop),
            self.assertRaisesRegex(RuntimeError, "stop failed"),
        ):
            _ = settle.begin(LOGIN)

        self.assertTrue((first / "state").read_text().startswith("ENABLED=1\n"))
        self.assertTrue((second / "state").read_text().startswith("ENABLED=1\n"))
        self.assertEqual(
            record.find_live(LOGIN), record.NoShutdown(kind="no shutdown")
        )

    def test_refresh_commits_timer_stops_before_cancel_can_restore_them(self) -> None:
        original = top_level("session", "Work")
        timer = self.write_instance("new-report", "new", enabled=True)
        newcomer = top_level("new", "New", timers=[timer.name])
        with (
            patch.object(
                settle,
                "run_inventory",
                return_value=machine_inventory([original]),
            ),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
        ):
            _ = settle.begin(LOGIN)

        real_update = record.update
        cancelled = False

        def update_then_cancel(
            login: str, change: Callable[[record.ShutdownRecord], None]
        ) -> record.ShutdownRecord:
            nonlocal cancelled
            current = real_update(login, change)
            if not cancelled:
                cancelled = True
                _ = settle.cancel(self.account, here=True)
            return current

        with (
            patch.object(
                settle,
                "run_inventory",
                return_value=machine_inventory([original, newcomer]),
            ),
            patch.object(settle, "update", side_effect=update_then_cancel),
        ):
            _ = settle.refresh(LOGIN)

        self.assertTrue((timer / "state").read_text().startswith("ENABLED=1\n"))
        self.assertEqual(
            record.find_live(LOGIN), record.NoShutdown(kind="no shutdown")
        )

    def test_refresh_persists_every_timer_before_raising_a_stop_failure(self) -> None:
        original = top_level("session", "Work")
        first = self.write_instance("first-new", "new", enabled=True)
        second = self.write_instance("second-new", "new", enabled=True)
        newcomer = top_level(
            "new", "New", timers=[first.name, second.name]
        )
        with (
            patch.object(
                settle,
                "run_inventory",
                return_value=machine_inventory([original]),
            ),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
        ):
            _ = settle.begin(LOGIN)

        real_run_notifier = settle.run_notifier
        stops = 0

        def fail_second_stop(verb: str, instance: str) -> None:
            nonlocal stops
            if verb == "stop":
                stops += 1
                if stops == 2:
                    raise RuntimeError("stop failed")
            real_run_notifier(verb, instance)

        with (
            patch.object(
                settle,
                "run_inventory",
                return_value=machine_inventory([original, newcomer]),
            ),
            patch.object(settle, "run_notifier", side_effect=fail_second_stop),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop failed"):
                _ = settle.refresh(LOGIN)
            new_entry = next(
                entry
                for entry in self.found_record()["entries"]
                if entry["session"]["session_id"] == "new"
            )
            self.assertEqual(
                [timer["instance"] for timer in new_entry["timers"]],
                [first.name, second.name],
            )
            self.assertTrue(
                (first / "state").read_text().startswith("ENABLED=0\n")
            )
            _ = settle.cancel(self.account, here=True)

        self.assertTrue((first / "state").read_text().startswith("ENABLED=1\n"))

    def test_cancel_stops_conductor_before_reading_the_restoration_snapshot(
        self,
    ) -> None:
        report = machine_inventory([top_level("session", "Work")])
        with (
            patch.object(settle, "run_inventory", return_value=report),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
        ):
            _ = settle.begin(LOGIN)
        order: list[str] = []
        real_update = record.update

        def ordered_update(
            login: str, change: Callable[[record.ShutdownRecord], None]
        ) -> record.ShutdownRecord:
            order.append("snapshot")
            return real_update(login, change)

        def stopped(_conductor: record.Conductor) -> None:
            order.append("stop")

        def restored(_record: record.ShutdownRecord) -> None:
            order.append("restore")

        with (
            patch.object(
                settle, "_stop_conductor", side_effect=stopped
            ),
            patch.object(settle, "update", side_effect=ordered_update),
            patch.object(
                settle, "_restore_record", side_effect=restored
            ),
        ):
            _ = settle.cancel(self.account, here=True)

        self.assertEqual(order, ["stop", "snapshot", "restore"])

    def test_refresh_after_cancel_flip_refuses_without_stopping_a_timer(self) -> None:
        original = top_level("session", "Work")
        newcomer = top_level("new", "New", timers=["new-timer"])
        with (
            patch.object(settle, "run_inventory", return_value=machine_inventory([original])),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
        ):
            _ = settle.begin(LOGIN)

        def flip(current: record.ShutdownRecord) -> None:
            current["state"] = "cancelled"

        _ = record.update(LOGIN, flip)
        with (
            patch.object(settle, "run_inventory", return_value=machine_inventory([newcomer])),
            patch.object(settle, "run_notifier") as notifier,
            self.assertRaises(record.NoLiveRecord),
        ):
            _ = settle.refresh(LOGIN)

        notifier.assert_not_called()

    def test_conduct_cycle_ends_cleanly_when_local_record_is_cancelled(self) -> None:
        report = machine_inventory([top_level("session", "Work")])
        with (
            patch.object(settle, "run_inventory", return_value=report),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
        ):
            _ = settle.begin(LOGIN)

        def flip(current: record.ShutdownRecord) -> None:
            current["state"] = "cancelled"

        _ = record.update(LOGIN, flip)
        outcome = settle.conduct_cycle(LOGIN, {}, here=True)

        self.assertEqual(outcome["kind"], "settlement ended")

    def test_selected_scope_excludes_new_unselected_sessions_on_refresh(self) -> None:
        selected = top_level("selected", "Selected")
        unselected = top_level("unselected", "Unselected")
        seen_scopes: list[record.ShutdownScope] = []

        def scoped_inventory(login: str, scope: record.ShutdownScope) -> inventory.Inventory:
            self.assertEqual(login, LOGIN)
            seen_scopes.append(scope)
            wanted: frozenset[str] = (
                frozenset(scope["session_ids"])
                if scope["kind"] == "selected"
                else frozenset[str]()
            )
            sessions: list[inventory.Session] = [selected, unselected]
            if wanted:
                sessions = [
                    session
                    for session in sessions
                    if session["session_id"] in wanted
                ]
            return machine_inventory(sessions)

        with (
            patch.object(settle, "run_inventory", side_effect=scoped_inventory),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
        ):
            result = shutdown.main(["begin", LOGIN, "--only", "selected"])
            created = self.found_record()
            refreshed = settle.refresh(LOGIN)

        self.assertEqual(result, 0)
        expected_scope = record.SelectedSessions(
            kind="selected", session_ids=["selected"]
        )
        self.assertEqual(created["scope"], expected_scope)
        self.assertEqual(seen_scopes, [expected_scope, expected_scope])
        self.assertEqual(
            [
                entry["session"]["session_id"]
                for entry in refreshed["record"]["entries"]
            ],
            ["selected"],
        )

    def test_down_refuses_unreachable_or_unavailable_peer_without_changes(
        self,
    ) -> None:
        local = machine_inventory([])
        for mode, expected in (
            ("unreachable", "Mac is unreachable"),
            ("unavailable", "Mac is unavailable (rc 7)"),
        ):
            with self.subTest(mode=mode):
                errors = io.StringIO()
                with (
                    patch.object(settle, "_warm", return_value=True),
                    patch.object(settle, "run_inventory", return_value=local),
                    patch.object(settle, "other_machine", return_value="Mac"),
                    patch.dict(os.environ, {"FAKE_SSH_MODE": mode}),
                    redirect_stderr(errors),
                ):
                    result = settle.down(self.account)

                self.assertEqual(result, 1)
                self.assertIn(expected, errors.getvalue())
                self.assertIn("nothing was shut down", errors.getvalue())
                self.assertEqual(
                    record.find_live(LOGIN), record.NoShutdown(kind="no shutdown")
                )
                self.assertEqual(self.logged_calls(self.notifier_log), [])

    def test_down_names_the_account_on_its_first_output_line(self) -> None:
        output = io.StringIO()
        report = machine_inventory([])
        with (
            patch.object(settle, "_warm", return_value=True),
            patch.object(settle, "run_inventory", return_value=report),
            patch.object(settle, "_launch_conductor"),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
            redirect_stdout(output),
        ):
            result = settle.down(self.account, here=True)

        self.assertEqual(result, 0)
        self.assertEqual(
            output.getvalue().splitlines()[0],
            "Account: claude 2 (owner@example.com)",
        )

    def test_failed_conductor_launch_cancels_local_and_remote_records(self) -> None:
        local = machine_inventory([], machine="natedev")
        remote = machine_inventory([], machine="Mac")
        remote_commands: list[list[str]] = []

        def remote_command(arguments: list[str]) -> tuple[int, str]:
            remote_commands.append(arguments)
            return 0, ""

        with (
            patch.object(settle, "_warm", return_value=True),
            patch.object(
                settle,
                "_preflight_inventories",
                return_value=settle.PreflightReady(
                    kind="ready", reports=[local, remote]
                ),
            ),
            patch.object(
                settle,
                "_live_on_other",
                return_value=settle.NoRemoteRecord(kind="no shutdown"),
            ),
            patch.object(settle, "run_inventory", return_value=local),
            patch.object(settle, "run_remote", side_effect=remote_command),
            patch.object(
                settle,
                "_launch_conductor",
                side_effect=RuntimeError("launch failed"),
            ),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
        ):
            result = settle.down(self.account)

        self.assertEqual(result, 1)
        self.assertEqual(
            record.find_live(LOGIN), record.NoShutdown(kind="no shutdown")
        )
        self.assertIn(["cancel", LOGIN, "--here"], remote_commands)

    def test_unreached_machine_keeps_conduct_settling_and_is_alerted(self) -> None:
        report = machine_inventory([top_level("session", "Work")])
        unreached: dict[str, datetime] = {}
        failure = settle.RemoteUnreachable(kind="unreachable", machine="Mac")
        with (
            patch.object(settle, "run_inventory", return_value=report),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
            patch.object(settle, "_remote_record", return_value=failure),
            patch.object(settle, "merge_in_progress", return_value=False),
        ):
            _ = settle.begin(LOGIN)
            outcome = settle.conduct_cycle(LOGIN, unreached)

        self.assertEqual(outcome["kind"], "settlement pending")
        pending = cast(settle.SettlementPending, outcome)
        self.assertEqual(len(pending["reports"]), 1)
        self.assertEqual(self.found_record()["state"], "settling")
        self.assertEqual(unreached, {"Mac": NOW_UTC})

        with (
            patch.object(settle, "run_inventory", return_value=report),
            patch.object(
                settle,
                "now_utc",
                return_value=NOW_UTC + timedelta(minutes=20),
            ),
            patch.object(settle, "_remote_record", return_value=failure),
            patch.object(settle, "merge_in_progress", return_value=False),
            patch.object(time, "sleep", side_effect=StopIteration),
            self.assertRaises(StopIteration),
        ):
            _ = settle.conduct(LOGIN)

        alert = self.sent_calls()[-1]
        self.assertIn("Mac: not reached since", alert["text"])
        self.assertIn("/shutdown now", alert["text"])
        self.assertIn("/shutdown cancel", alert["text"])

    def test_holdout_alert_names_the_now_or_cancel_action_at_twenty_minutes(
        self,
    ) -> None:
        report = machine_inventory([top_level("session", "Work")])
        with (
            patch.object(settle, "run_inventory", return_value=report),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
        ):
            live = settle.begin(LOGIN)
        twenty_minutes_later = NOW_UTC + timedelta(minutes=20)

        with (
            patch.object(
                settle,
                "conduct_cycle",
                return_value=settle.SettlementPending(
                    kind="settlement pending",
                    reports=[
                        settle.RefreshReport(
                            record=live,
                            verdicts=[
                                settle.EntryVerdict(
                                    session_id="session",
                                    verdict=settle.Holdout(
                                        kind="holdout",
                                        line="natedev top-level Work: idle",
                                    ),
                                )
                            ],
                        )
                    ],
                ),
            ),
            patch.object(settle, "run_inventory", return_value=report),
            patch.object(settle, "now_utc", return_value=twenty_minutes_later),
            patch.object(settle, "merge_in_progress", return_value=False),
            patch.object(time, "sleep", side_effect=StopIteration),
            self.assertRaises(StopIteration),
        ):
            _ = settle.conduct(LOGIN)

        alert = self.sent_calls()[-1]
        self.assertIn("Shutdown of claude 2: 1 not ready", alert["args"])
        self.assertIn("decision", alert["args"])
        action_index = alert["args"].index("--action")
        self.assertEqual(
            alert["args"][action_index + 1],
            "Run /shutdown now to stop them anyway, or /shutdown cancel.",
        )
        self.assertIn("natedev top-level Work: idle", alert["text"])

    def test_cancel_restores_only_recorded_enabled_states_and_names_peer(
        self,
    ) -> None:
        enabled = self.write_instance("enabled", "session", enabled=True)
        disabled = self.write_instance("disabled", "session", enabled=False)
        report = machine_inventory(
            [
                top_level(
                    "session",
                    "Work",
                    timers=[enabled.name, disabled.name],
                )
            ]
        )
        with (
            patch.object(settle, "run_inventory", return_value=report),
            patch.object(settle, "now_utc", return_value=NOW_UTC),
        ):
            _ = settle.begin(LOGIN)

        def message_was_sent(current: record.ShutdownRecord) -> None:
            current["entries"][0]["settle_message"] = record.SettleMessageSent(
                kind="sent", at=NOW
            )

        _ = record.update(LOGIN, message_was_sent)

        def start_conductor(current: record.ShutdownRecord) -> None:
            current["conductor"] = record.SystemdConductor(
                kind="systemd", unit="shutdown-test"
            )

        _ = record.update(LOGIN, start_conductor)
        errors = io.StringIO()
        with (
            patch.object(settle, "run_remote", return_value=(255, "")),
            patch.object(settle, "other_machine", return_value="Mac"),
            patch.object(settle, "_stop_conductor"),
            redirect_stderr(errors),
        ):
            result = settle.cancel(self.account)

        self.assertEqual(result, 1)
        self.assertIn(
            "Mac not reached: run /shutdown cancel there when it is back",
            errors.getvalue(),
        )
        self.assertTrue((enabled / "state").read_text().startswith("ENABLED=1\n"))
        self.assertTrue((disabled / "state").read_text().startswith("ENABLED=0\n"))
        self.assertEqual(
            self.logged_calls(self.notifier_log),
            [["stop", enabled.name], ["start", enabled.name]],
        )
        self.assertEqual(
            record.find_live(LOGIN), record.NoShutdown(kind="no shutdown")
        )
        sent = self.sent_calls()
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0]["args"][0], "retire")
        self.assertIn("cancelled by the user", sent[1]["text"])


if __name__ == "__main__":
    _ = unittest.main()
