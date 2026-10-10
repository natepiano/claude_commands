"""Tests for the account-scoped shutdown record store."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import cast, final, override
from unittest.mock import patch

import inventory
import record


NOW = "2026-10-09T21:49:10+00:00"
LATER = "2026-10-09T21:50:10+00:00"


def session(number: int = 1) -> inventory.TopLevelSession:
    return inventory.TopLevelSession(
        kind="top-level",
        session_id=f"session-{number}",
        pid=10_000 + number,
        proc_start=f"start-{number}",
        name=f"Claude {number}",
        cwd=f"/tmp/checkout-{number}",
        status="idle",
        model=inventory.NoReplyYet(kind="no reply yet"),
        checkout=inventory.NotACheckout(kind="not a checkout"),
        run_dirs=[],
        codex_servers=[],
        timers=[],
        host=inventory.UnknownHost(kind="unknown"),
    )


def entry(
    progress: record.SessionProgress | None = None,
    *,
    number: int = 1,
    timer_footer: record.ShowrunnerFooter | record.NoFooter | None = None,
    settle_message: record.SettleMessage | None = None,
    where: record.Where | None = None,
) -> record.ShutdownSessionEntry:
    return record.ShutdownSessionEntry(
        session=session(number),
        timers=[
            record.TimerRestore(
                instance=f"timer-{number}",
                was_enabled=True,
                footer=timer_footer or record.NoFooter(kind="no footer"),
            )
        ],
        settle_message=settle_message or record.SettleMessageNotSent(kind="not sent"),
        where=where or record.WhereNotSaid(kind="not said"),
        progress=progress or record.SessionWaiting(kind="waiting"),
        stop_issues=[],
    )


def shutdown_record(
    login: str = "owner@example.com",
    *,
    state: record.ShutdownState = "settling",
    progress: record.SessionProgress | None = None,
    requested_at: str = NOW,
) -> record.ShutdownRecord:
    return record.ShutdownRecord(
        login=login,
        label=login.split("@", 1)[0],
        machine="natedev",
        state=state,
        requested_at=requested_at,
        requested_by=record.FromTerminal(kind="terminal"),
        scope=record.AllAccountSessions(kind="all account sessions"),
        conductor=record.ConductorNotStarted(kind="not started"),
        force="wait for ready",
        entries=[entry(progress)],
        stop_issues=[],
    )


@final
class RecordTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        environment_context: object = cast(
            object,
            self.enterContext(
                patch.dict(
                    os.environ,
                    {"SHUTDOWN_STATE_DIR": str(self.root / "shutdown")},
                )
            ),
        )
        del environment_context

    def test_only_one_live_record_per_case_folded_account(self) -> None:
        first = shutdown_record("Owner@Example.com")
        second = shutdown_record("owner@example.com", requested_at=LATER)

        record.create(first)

        self.assertEqual(
            record.find_live("OWNER@example.COM"),
            record.LiveShutdownRecord(kind="live", record=first),
        )
        with self.assertRaises(record.ShutdownInProgress) as raised:
            record.create(second)
        self.assertEqual(raised.exception.live, first)

    def test_update_holds_the_account_lock_while_changing_record(self) -> None:
        live = shutdown_record()
        record.create(live)
        lock_path = self.root / "shutdown" / live["login"] / "lock"

        def change(current: record.ShutdownRecord) -> None:
            with lock_path.open("a+", encoding="utf-8") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(
                        contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                    )
            current["state"] = "stopping"

        changed = record.update(live["login"], change)

        self.assertEqual(changed["state"], "stopping")
        found = record.find_live(live["login"])
        self.assertEqual(found["kind"], "live")
        if found["kind"] == "live":
            self.assertEqual(found["record"]["state"], "stopping")

    def test_unsafe_login_is_refused_before_any_record_path_is_created(self) -> None:
        outside_directory = self.root / "outside@example.com"
        unsafe_logins = [
            str(outside_directory),
            "nested/owner@example.com",
            "",
            "nul\0owner@example.com",
            ".hidden@example.com",
        ]

        def leave_unchanged(current: record.ShutdownRecord) -> None:
            del current

        for login in unsafe_logins:
            with self.subTest(login=login):
                live = shutdown_record(login)
                with self.assertRaises(record.InvalidRecord):
                    record.create(live)
                with self.assertRaises(ValueError):
                    _ = record.find_live(login)
                with self.assertRaises(ValueError):
                    _ = record.update(login, leave_unchanged)
                with self.assertRaises(ValueError):
                    record.archive(login)
                with self.assertRaises(record.InvalidRecord):
                    _ = record.parse_records(json.dumps([live]))

        self.assertFalse(outside_directory.exists())
        self.assertFalse((self.root / "shutdown" / "nested").exists())

    def test_update_refuses_identity_changes_without_modifying_stored_record(
        self,
    ) -> None:
        original = shutdown_record("alpha@example.com")
        record.create(original)

        def change_login(current: record.ShutdownRecord) -> None:
            current["login"] = "beta@example.com"

        with self.assertRaises(ValueError):
            _ = record.update(original["login"], change_login)
        self.assertEqual(
            record.find_live(original["login"]),
            record.LiveShutdownRecord(kind="live", record=original),
        )
        self.assertEqual(
            record.find_live("beta@example.com"),
            record.NoShutdown(kind="no shutdown"),
        )

        def change_machine(current: record.ShutdownRecord) -> None:
            current["machine"] = "Mac"

        with self.assertRaises(ValueError):
            _ = record.update(original["login"], change_machine)
        self.assertEqual(
            record.find_live(original["login"]),
            record.LiveShutdownRecord(kind="live", record=original),
        )

    def test_archive_requires_terminal_state_and_moves_the_record(self) -> None:
        live = shutdown_record()
        record.create(live)

        with self.assertRaises(ValueError):
            record.archive(live["login"])
        self.assertEqual(record.find_live(live["login"])["kind"], "live")

        def cancel(current: record.ShutdownRecord) -> None:
            current["state"] = "cancelled"

        _ = record.update(live["login"], cancel)
        record.archive(live["login"])

        self.assertEqual(
            record.find_live(live["login"]), record.NoShutdown(kind="no shutdown")
        )
        history = self.root / "shutdown" / live["login"] / "history" / f"{NOW}.json"
        archived = cast(object, json.loads(history.read_text()))
        self.assertIsInstance(archived, dict)
        self.assertEqual(cast(dict[str, object], archived)["state"], "cancelled")

    def test_archive_also_accepts_up(self) -> None:
        live = shutdown_record(state="up")
        record.create(live)

        record.archive(live["login"])

        self.assertEqual(record.find_live(live["login"])["kind"], "no shutdown")

    def test_live_records_reads_two_accounts_in_login_order(self) -> None:
        second = shutdown_record("zeta@example.com")
        first = shutdown_record("alpha@example.com", requested_at=LATER)
        record.create(second)
        record.create(first)

        self.assertEqual(record.live_records(), [first, second])

    def test_every_record_variant_round_trips(self) -> None:
        progresses: list[record.SessionProgress] = [
            record.SessionWaiting(kind="waiting"),
            record.SessionReadyToStop(kind="ready", at=NOW),
            record.PassiveSeatReadyToStop(kind="passive seat ready", at=NOW),
            record.SessionStopped(kind="stopped", at=NOW),
            record.SessionAlreadyGone(kind="already gone", at=NOW),
            record.ProcessIdentityLost(kind="process identity lost", at=NOW),
            record.SessionStopFailed(kind="stop failed", at=NOW, reason="permission denied"),
            record.SessionRestarted(kind="restarted", at=NOW),
            record.SessionRestoredTimersPending(
                kind="timers pending",
                at=NOW,
                timers=[
                    record.PendingTimer(
                        instance="showrunner-demo", reason="notifier unavailable"
                    )
                ],
            ),
            record.SessionRestartFailed(
                kind="restart failed", at=NOW, reason="window did not open"
            ),
            record.SessionNeedsManualRestart(kind="manual restart", command="claude --resume id"),
        ]
        states: list[record.ShutdownState] = [
            "settling",
            "stopping",
            "down",
            "stop partial",
            "restarting",
            "restart partial",
            "cancelled",
            "up",
        ]
        records = [
            shutdown_record(
                f"account-{index}@example.com",
                state=states[index % len(states)],
                progress=progress,
            )
            for index, progress in enumerate(progresses)
        ]
        records[0]["requested_by"] = record.FromSession(
            kind="session", session_id="requester"
        )
        records[0]["scope"] = record.SelectedSessions(
            kind="selected", session_ids=["session-1"]
        )
        records[0]["conductor"] = record.SystemdConductor(
            kind="systemd", unit="shutdown-owner"
        )
        records[0]["force"] = "now"
        records[0]["entries"][0] = entry(
            progresses[0],
            timer_footer=record.ShowrunnerFooter(kind="footer", slug="production"),
            settle_message=record.SettleMessageSent(kind="sent", at=NOW),
            where=record.WhereSaid(kind="said", text="ready here", at=NOW),
        )
        records[1]["conductor"] = record.LaunchdConductor(
            kind="launchd", label="shutdown-owner"
        )

        encoded = json.dumps(records)

        self.assertEqual(record.parse_records(encoded), records)

    def test_every_stop_issue_variant_round_trips(self) -> None:
        live = shutdown_record()
        session_issues: list[record.SessionStopIssue] = [
            record.NotReadyToStop(
                kind="not ready to stop",
                at=NOW,
                status="busy",
                progress="waiting",
            ),
            record.StillRunningAfterStop(
                kind="still running",
                at=NOW,
                reason="alive after two SIGTERMs",
            ),
            record.AccountUnreadableAtStop(kind="account unreadable", at=NOW),
            record.SeatStillLive(kind="seat still live", at=NOW),
            record.CodexServerLeftRunning(
                kind="codex server left running",
                at=NOW,
                run_dir="/tmp/run",
                cause="stop not confirmed",
            ),
            record.UnitTmuxLeftRunning(
                kind="unit tmux session left running",
                at=NOW,
                tmux_session="demo-unit",
                cause="owner identity lost",
            ),
        ]
        orchestration_issues: list[record.OrchestrationStopIssue] = [
            record.StopClaimFailed(
                kind="stop claim failed",
                at=NOW,
                machine="Mac",
                reason="claim-stop failed (rc 3)",
            ),
            record.MachineStopFailed(
                kind="machine stop failed",
                at=NOW,
                machine="natedev",
                reason="inventory failed",
            ),
        ]
        live["entries"][0]["stop_issues"] = session_issues
        live["stop_issues"] = orchestration_issues

        self.assertEqual(record.parse_records(json.dumps([live])), [live])

    def test_stop_issue_parser_refuses_unknown_tags_and_noncanonical_time(
        self,
    ) -> None:
        malformed_issues: list[dict[str, object]] = [
            {"kind": "not a stop issue", "at": NOW},
            {
                "kind": "codex server left running",
                "at": NOW,
                "run_dir": "/tmp/run",
                "cause": "maybe stopped",
            },
            {
                "kind": "not ready to stop",
                "at": NOW,
                "status": "busy",
                "progress": "refused",
            },
            {
                "kind": "still running",
                "at": "2026-10-09T21:49:10Z",
                "reason": "alive after two SIGTERMs",
            },
        ]

        for issue in malformed_issues:
            with self.subTest(issue=issue):
                live = shutdown_record()
                entry_values = cast(
                    dict[str, object], cast(object, live["entries"][0])
                )
                entry_values["stop_issues"] = [issue]
                with self.assertRaises(record.InvalidRecord):
                    _ = record.parse_records(json.dumps([live]))

    def test_missing_stop_issue_fields_read_as_empty_lists(self) -> None:
        live = shutdown_record()
        entry_values = cast(
            dict[str, object], cast(object, live["entries"][0])
        )
        record_values = cast(dict[str, object], cast(object, live))
        del entry_values["stop_issues"]
        del record_values["stop_issues"]

        parsed = record.parse_records(json.dumps([live]))[0]

        self.assertEqual(parsed["entries"][0]["stop_issues"], [])
        self.assertEqual(parsed["stop_issues"], [])

    def test_queued_settle_message_round_trips(self) -> None:
        live = shutdown_record()
        live["entries"][0]["settle_message"] = record.SettleMessageQueued(
            kind="queued", at=NOW, reason="no live session"
        )

        self.assertEqual(record.parse_records(json.dumps([live])), [live])

    def test_queued_settle_message_requires_a_reason(self) -> None:
        live = shutdown_record()
        malformed = cast(
            dict[str, object], cast(object, live["entries"][0])
        )
        malformed["settle_message"] = {"kind": "queued", "at": NOW}

        with self.assertRaisesRegex(
            record.InvalidRecord,
            r"records\[0\]\.entries\[0\]\.settle_message\.reason is missing",
        ):
            _ = record.parse_records(json.dumps([live]))

    def test_malformed_tag_names_its_field_path(self) -> None:
        malformed = cast(dict[str, object], cast(object, shutdown_record()))
        malformed["scope"] = {"kind": "some sessions"}

        with self.assertRaisesRegex(
            record.InvalidRecord, r"records\[0\]\.scope\.kind is invalid"
        ):
            _ = record.parse_records(json.dumps([malformed]))

    def test_parse_records_refuses_noncanonical_utc_timestamp(self) -> None:
        malformed = shutdown_record(requested_at="2026-10-09/21:49:10+00:00")

        with self.assertRaises(record.InvalidRecord):
            _ = record.parse_records(json.dumps([malformed]))


if __name__ == "__main__":
    _ = unittest.main()
