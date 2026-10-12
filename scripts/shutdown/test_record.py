"""Tests for the account-scoped shutdown record store."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Literal, cast, final, override
from unittest.mock import patch

from test_support import ShutdownTestCase
import inventory
import record
from launch_permission import launch_barrier


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
class RecordTests(ShutdownTestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        environment_context: object = cast(
            object,
            self.enterContext(
                patch.dict(
                    os.environ,
                    {
                        "HOME": str(self.root),
                        "SHUTDOWN_STATE_DIR": str(self.root / "shutdown"),
                    },
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

    def test_missing_account_directory_is_no_shutdown(self) -> None:
        self.assertEqual(
            record.find_live("missing@example.com"),
            record.NoShutdown(kind="no shutdown"),
        )

    def test_missing_record_file_is_no_shutdown(self) -> None:
        account_directory = self.root / "shutdown" / "empty@example.com"
        account_directory.mkdir(parents=True)

        self.assertEqual(
            record.find_live("empty@example.com"),
            record.NoShutdown(kind="no shutdown"),
        )

    def test_non_directory_account_paths_are_invalid_records(self) -> None:
        state_root = self.root / "shutdown"
        state_root.mkdir()
        regular_file = state_root / "file@example.com"
        _ = regular_file.write_text("not a directory", encoding="utf-8")
        dangling_link = state_root / "dangling@example.com"
        dangling_link.symlink_to(state_root / "missing-target")
        looping_link = state_root / "loop@example.com"
        looping_link.symlink_to(looping_link)

        for login in (
            "file@example.com",
            "dangling@example.com",
            "loop@example.com",
        ):
            with self.subTest(login=login), self.assertRaises(record.InvalidRecord):
                _ = record.find_live(login)

    def test_record_path_must_be_a_regular_file(self) -> None:
        account_directory = self.root / "shutdown" / "owner@example.com"
        (account_directory / "record.json").mkdir(parents=True)

        with self.assertRaises(record.InvalidRecord):
            _ = record.find_live("owner@example.com")

    def test_broken_account_lock_is_an_invalid_record(self) -> None:
        account_directory = self.root / "shutdown" / "owner@example.com"
        lock_path = account_directory / "lock"
        lock_path.mkdir(parents=True)

        with self.assertRaises(record.InvalidRecord) as raised:
            _ = record.find_live("owner@example.com")

        self.assertIn(str(lock_path), str(raised.exception))
        self.assertIn("Is a directory", str(raised.exception))

    def test_create_waits_for_launch_barrier_held_by_another_process(self) -> None:
        helper = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "from launch_permission import launch_barrier\n"
                    "import sys\n"
                    "with launch_barrier():\n"
                    " print('locked', flush=True)\n"
                    " sys.stdin.readline()\n"
                ),
            ],
            cwd=Path(__file__).parent,
            env=dict(os.environ),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(helper.__exit__, None, None, None)
        self.addCleanup(helper.kill)
        self.assertIsNotNone(helper.stdout)
        if helper.stdout is None:
            self.fail("barrier helper has no stdout")
        self.assertEqual(helper.stdout.readline(), "locked\n")

        created = threading.Event()
        failure: list[BaseException] = []

        def create_record() -> None:
            try:
                record.create(shutdown_record())
            except BaseException as error:
                failure.append(error)
            finally:
                created.set()

        creator = threading.Thread(target=create_record)
        creator.start()
        self.assertFalse(created.wait(0.2), "create returned while launch barrier was held")
        self.assertIsNotNone(helper.stdin)
        if helper.stdin is None:
            self.fail("barrier helper has no stdin")
        _ = helper.stdin.write("release\n")
        helper.stdin.flush()
        self.assertEqual(helper.wait(timeout=5), 0, helper.stderr.read() if helper.stderr else "")
        self.assertTrue(created.wait(5), "create did not continue after barrier release")
        creator.join(timeout=5)
        self.assertEqual(failure, [])
        self.assertEqual(record.find_live("owner@example.com")["kind"], "live")

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

    def test_a_parked_restart_partial_frees_the_slot_and_stays_held(self) -> None:
        unfinished = shutdown_record(state="restart partial")
        record.create(unfinished)

        self.assertEqual(record.park(unfinished["login"]), unfinished)

        self.assertEqual(record.find_live(unfinished["login"])["kind"], "no shutdown")
        self.assertEqual(record.parked(unfinished["login"]), [unfinished])
        newer = shutdown_record(requested_at=LATER)
        record.create(newer)
        self.assertEqual(record.live_records(), [newer, unfinished])
        self.assertEqual(record.account_records(newer["login"]), [newer, unfinished])

    def test_park_and_unpark_refuse_without_moving_a_record(self) -> None:
        for state in ("settling", "stopping", "down", "stop partial", "restarting"):
            with self.subTest(state=state):
                live = shutdown_record(state=cast(record.ShutdownState, state))
                record.create(live)
                with self.assertRaises(ValueError):
                    _ = record.park(live["login"])
                self.assertEqual(record.account_records(live["login"]), [live])
                (self.root / "shutdown" / live["login"] / "record.json").unlink()
        unfinished = shutdown_record(state="restart partial")
        with self.assertRaises(record.NoLiveRecord):
            _ = record.park(unfinished["login"])
        with self.assertRaises(record.NoLiveRecord):
            record.unpark(unfinished["login"], unfinished["requested_at"])
        record.create(unfinished)
        _ = record.park(unfinished["login"])
        newer = shutdown_record(requested_at=LATER)
        record.create(newer)

        with self.assertRaises(record.ShutdownInProgress):
            record.unpark(unfinished["login"], unfinished["requested_at"])

        self.assertEqual(record.account_records(newer["login"]), [newer, unfinished])
        (self.root / "shutdown" / newer["login"] / "record.json").unlink()
        record.unpark(unfinished["login"], unfinished["requested_at"])
        self.assertEqual(record.account_records(unfinished["login"]), [unfinished])
        self.assertEqual(record.parked(unfinished["login"]), [])

    def test_config_dir_is_saved_only_on_the_session_and_round_trips(self) -> None:
        """F004: persistence keeps one authoritative directory on the session."""
        live = shutdown_record()
        config_dir = str(self.root / "account with spaces")
        live["entries"][0]["session"]["config_dir"] = config_dir

        record.create(live)

        found = record.find_live(live["login"])
        if found["kind"] != "live":
            self.fail("expected a live shutdown record")
        restored = found["record"]["entries"][0]
        self.assertEqual(restored["session"].get("config_dir"), config_dir)
        self.assertNotIn("config_dir", restored)
        self.assertEqual(
            record.parse_records(json.dumps([found["record"]])),
            [found["record"]],
        )

    def test_obsolete_entry_config_dir_is_ignored_when_reading_records(self) -> None:
        """F004: old entry-level directories never override the session snapshot."""
        for obsolete_dir in (str(self.root / "old account"), 123):
            with self.subTest(obsolete_dir=obsolete_dir):
                live = shutdown_record()
                config_dir = str(self.root / "current account")
                live["entries"][0]["session"]["config_dir"] = config_dir
                entry_values = cast(dict[str, object], cast(object, live["entries"][0]))
                entry_values["config_dir"] = obsolete_dir

                restored = record.parse_records(json.dumps([live]))[0]["entries"][0]

                self.assertNotIn("config_dir", restored)
                self.assertEqual(restored["session"].get("config_dir"), config_dir)

    def test_restart_continuation_round_trips_in_the_record(self) -> None:
        for action in ("stop", "restart"):
            with self.subTest(action=action):
                live = shutdown_record()
                live["then"] = action
                record.create(live)
                found = record.find_live(live["login"])
                if found["kind"] != "live":
                    self.fail("expected a live shutdown record")
                self.assertEqual(record.continuation(found["record"]), action)
                self.assertEqual(record.parse_records(json.dumps([live])), [live])
                (self.root / "shutdown" / live["login"] / "record.json").unlink()

    def test_legacy_record_stops_without_changing_its_wire_shape(self) -> None:
        live = shutdown_record()
        restored = record.parse_records(json.dumps([live]))[0]
        self.assertEqual(record.continuation(restored), "stop")
        self.assertEqual(restored, live)
        self.assertNotIn("then", restored)

    def test_record_rejects_unknown_or_non_string_continuations(self) -> None:
        for action in ("launch", "", 1, None, {"kind": "restart"}):
            with self.subTest(action=action):
                live = shutdown_record()
                values = cast(dict[str, object], cast(object, live))
                values["then"] = action
                with self.assertRaisesRegex(record.InvalidRecord, r"records\[0\]\.then"):
                    _ = record.parse_records(json.dumps([live]))

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


def session_shutdown(
    number: int = 1,
    *,
    login: str = "owner@example.com",
    requested_at: str = NOW,
    progress: record.SessionProgress | None = None,
    hold: Literal["account", "session"] = "session",
) -> record.SessionShutdown:
    saved_entry = entry(progress, number=number)
    return record.SessionShutdown(
        login=login, label=login.split("@", 1)[0], machine="natedev",
        requested_at=requested_at, requested_by=record.FromTerminal(kind="terminal"),
        hold=hold, then="stop", force="wait for ready", alerts="user",
        session=saved_entry["session"], seats=[], timers=saved_entry["timers"],
        settle_message=saved_entry["settle_message"], where=saved_entry["where"],
        progress=saved_entry["progress"], stop_issues=[],
    )


def account_hold(login: str = "owner@example.com") -> record.AccountHold:
    return record.AccountHold(
        login=login, label=login.split("@", 1)[0], machine="natedev",
        requested_at=NOW, requested_by=record.FromTerminal(kind="terminal"), then="stop",
    )


def owned_seat(owner: str = "session-1") -> inventory.SeatSession:
    return inventory.SeatSession(
        kind="seat", session_id="seat-1", pid=20001, proc_start="seat-start",
        name="worker", cwd="/tmp/seat", status="idle",
        model=inventory.NoReplyYet(kind="no reply yet"),
        checkout=inventory.NotACheckout(kind="not a checkout"),
        run_dirs=[], codex_servers=[], timers=[], host=inventory.UnknownHost(kind="unknown"),
        owner=inventory.DirectorOwner(kind="director", session_id=owner),
    )


@final
class SessionRecordTests(ShutdownTestCase):
    root: Path = Path()
    directory: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.directory = self.root / "shutdown" / "owner@example.com"
        context = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.root), "SHUTDOWN_STATE_DIR": str(self.root / "shutdown"),
        })))
        del context

    def test_session_records_create_and_find_independent_sessions_in_one_account(self) -> None:
        first = session_shutdown(login="Owner@Example.com")
        second = session_shutdown(2, requested_at=LATER)
        self.assertTrue(record.create_session(first))
        self.assertTrue(record.create_session(second))
        self.assertEqual(record.find_session("OWNER@EXAMPLE.COM", "session-1"), first)
        self.assertEqual(record.session_records(second["login"]), [first, second])
        self.assertEqual(record.parse_session_records(json.dumps([first, second])), [first, second])
        self.assertFalse((self.directory / "record.json").exists())

    def test_session_records_second_create_leaves_the_existing_file_unchanged(self) -> None:
        first = session_shutdown()
        self.assertTrue(record.create_session(first))
        path = self.directory / "sessions" / "session-1.json"
        before = path.read_bytes(), path.stat().st_mtime_ns
        replacement = session_shutdown(requested_at=LATER)
        replacement["then"] = "restart"
        self.assertFalse(record.create_session(replacement))
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(record.find_session(first["login"], "session-1"), first)

    def test_session_records_sort_by_request_time_then_name_across_accounts(self) -> None:
        later = session_shutdown(1, requested_at=LATER)
        bravo = session_shutdown(2)
        bravo["session"]["name"] = "bravo"
        alpha = session_shutdown(3, login="other@example.com")
        alpha["session"]["name"] = "alpha"
        for saved in (later, bravo, alpha):
            self.assertTrue(record.create_session(saved))
        self.assertEqual(record.session_records(later["login"]), [bravo, later])
        self.assertEqual(record.all_session_records(), [alpha, bravo, later])

    def test_session_records_update_holds_the_account_lock_and_changes_only_its_session(self) -> None:
        first, second = session_shutdown(), session_shutdown(2)
        for saved in (first, second):
            self.assertTrue(record.create_session(saved))

        def change(current: record.SessionShutdown) -> None:
            with (self.directory / "lock").open("a+") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            current["progress"] = record.SessionRestarting(kind="restarting", at=LATER)

        changed = record.update_session(first["login"], "session-1", change)
        self.assertEqual(changed["progress"], {"kind": "restarting", "at": LATER})
        self.assertEqual(record.find_session(first["login"], "session-1"), changed)
        self.assertEqual(record.find_session(first["login"], "session-2"), second)

    def test_session_records_update_rejects_identity_changes_without_writing(self) -> None:
        saved = session_shutdown()
        self.assertTrue(record.create_session(saved))
        for field, value in (("login", "other@example.com"), ("machine", "Mac"), ("session_id", "changed")):
            with self.subTest(field=field):
                def change(current: record.SessionShutdown) -> None:
                    values = cast(dict[str, object], cast(object, current))
                    if field == "session_id":
                        values = cast(dict[str, object], cast(object, current["session"]))
                    values[field] = value

                with self.assertRaises(ValueError):
                    _ = record.update_session(saved["login"], "session-1", change)
                self.assertEqual(record.find_session(saved["login"], "session-1"), saved)

    def test_session_records_archive_moves_each_file_to_its_own_history_name(self) -> None:
        for number in (1, 2):
            saved = session_shutdown(number, progress=record.SessionRestarted(kind="restarted", at=LATER))
            self.assertTrue(record.create_session(saved))
            record.archive_session(saved["login"], saved["session"]["session_id"])
            path = self.directory / "history" / f"{NOW}-session-{number}.json"
            self.assertEqual(record.parse_session_records("[" + path.read_text() + "]"), [saved])
            self.assertIsNone(record.find_session(saved["login"], saved["session"]["session_id"]))
        self.assertEqual(record.session_records("owner@example.com"), [])

    def test_session_records_missing_state_is_empty_and_mutations_require_a_record(self) -> None:
        self.assertEqual(record.session_records("owner@example.com"), [])
        self.assertEqual(record.all_session_records(), [])
        self.assertIsNone(record.find_session("owner@example.com", "missing"))
        self.assertIsNone(record.account_hold("owner@example.com"))
        record.clear_account_hold("owner@example.com")
        with self.assertRaises(record.NoLiveRecord):
            _ = record.update_session("owner@example.com", "missing", lambda current: None)
        with self.assertRaises(record.NoLiveRecord):
            record.archive_session("owner@example.com", "missing")
        self.assertFalse(self.directory.exists())

    def test_session_records_hold_file_persists_without_any_session(self) -> None:
        hold = account_hold("Owner@Example.com")
        record.set_account_hold(hold)
        self.assertEqual(record.account_hold("OWNER@EXAMPLE.COM"), hold)
        self.assertEqual(record.session_records(hold["login"]), [])
        self.assertEqual(cast(object, json.loads((self.directory / "hold.json").read_text())), hold)
        hold["then"] = "restart"
        record.set_account_hold(hold)
        self.assertEqual(record.account_hold(hold["login"]), hold)
        record.clear_account_hold(hold["login"])
        record.clear_account_hold(hold["login"])
        self.assertIsNone(record.account_hold(hold["login"]))

    def test_all_account_holds_include_explicit_and_empty_old_accounts_in_request_order(self) -> None:
        explicit = account_hold()
        explicit["label"] = "Alpha"
        record.set_account_hold(explicit)
        later = account_hold("later@example.com")
        later["requested_at"] = LATER
        later["label"] = "Earlier label"
        record.set_account_hold(later)
        old = shutdown_record("legacy@example.com", state="down")
        old["label"] = "Zulu"
        old["entries"] = []
        record.create(old)
        legacy = account_hold(old["login"])
        legacy["label"] = old["label"]
        self.assertTrue(record.create_session(session_shutdown(login="selected@example.com")))

        self.assertEqual(record.all_account_holds(), [explicit, legacy, later])
        self.assertEqual(record.session_records(old["login"]), [])
        self.assertTrue((self.directory.parent / "launch.lock").is_file())

    def test_all_account_holds_missing_root_is_empty(self) -> None:
        self.assertEqual(record.all_account_holds(), [])
        self.assertFalse(self.directory.parent.exists())

    def test_all_account_holds_reject_non_directory_roots_and_accounts(self) -> None:
        state = self.directory.parent
        _ = state.write_text("not a directory")
        with self.assertRaises(record.InvalidRecord):
            _ = record.all_account_holds()
        state.unlink()
        state.mkdir()
        _ = self.directory.write_text("not a directory")
        with self.assertRaises(record.InvalidRecord):
            _ = record.all_account_holds()
        self.directory.unlink()
        (state / "launch.lock").mkdir()
        with self.assertRaises(record.InvalidRecord):
            _ = record.all_account_holds()

    def test_all_account_holds_refuse_directory_and_lock_read_failures(self) -> None:
        record.set_account_hold(account_hold())
        with patch.object(Path, "iterdir", side_effect=PermissionError("access refused")):
            with self.assertRaisesRegex(record.InvalidRecord, "access refused"):
                _ = record.all_account_holds()
        with patch.object(fcntl, "flock", side_effect=OSError("lock unavailable")):
            with self.assertRaisesRegex(record.InvalidRecord, "lock unavailable"):
                _ = record.all_account_holds()

    def test_all_account_holds_refuse_invalid_hold_and_old_record_files(self) -> None:
        self.directory.mkdir(parents=True)
        for filename in ("hold.json", "record.json"):
            with self.subTest(filename=filename):
                path = self.directory / filename
                _ = path.write_text("{}")
                with self.assertRaises(record.InvalidRecord) as raised:
                    _ = record.all_account_holds()
                self.assertIn(str(path), str(raised.exception))
                path.unlink()

    def test_parse_account_holds_accepts_a_valid_list(self) -> None:
        holds = [account_hold(), account_hold("other@example.com")]
        self.assertEqual(record.parse_account_holds(json.dumps(holds)), holds)
        self.assertEqual(record.parse_account_holds("[]"), [])

    def test_parse_account_holds_rejects_invalid_text_and_hold_fields(self) -> None:
        for text in ("not JSON", "{}", "null", "[1]", "[{}]"):
            with self.subTest(text=text):
                with self.assertRaises(record.InvalidRecord):
                    _ = record.parse_account_holds(text)
        for field, value in (
            ("login", "../other"), ("requested_at", "not a time"),
            ("requested_by", {"kind": "unknown"}), ("then", "continue"),
        ):
            with self.subTest(field=field):
                hold = cast(dict[str, object], cast(object, account_hold()))
                hold[field] = value
                with self.assertRaises(record.InvalidRecord):
                    _ = record.parse_account_holds(json.dumps([hold]))

    def test_session_records_create_and_hold_wait_for_the_launch_barrier(self) -> None:
        for operation in ("session", "hold"):
            with self.subTest(operation=operation):
                done = threading.Event()
                failures: list[BaseException] = []

                def create() -> None:
                    try:
                        if operation == "session":
                            self.assertTrue(record.create_session(session_shutdown()))
                        else:
                            record.set_account_hold(account_hold())
                    except BaseException as error:
                        failures.append(error)
                    finally:
                        done.set()

                with launch_barrier():
                    worker = threading.Thread(target=create)
                    worker.start()
                    self.assertFalse(done.wait(0.05))
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(failures, [])
                self.assertTrue(done.is_set())

    def test_session_records_unsafe_ids_stay_inside_the_sessions_directory(self) -> None:
        for session_id, filename in (("../outside/\0:*", "..-outside----.json"), ("safe.ID_2-3", "safe.ID_2-3.json")):
            with self.subTest(session_id=session_id):
                saved = session_shutdown()
                saved["session"]["session_id"] = session_id
                self.assertTrue(record.create_session(saved))
                self.assertTrue((self.directory / "sessions" / filename).is_file())
                self.assertEqual(record.find_session(saved["login"], session_id), saved)
        self.assertFalse((self.root / "outside").exists())

    def test_session_records_colliding_safe_names_never_replace_another_session(self) -> None:
        first = session_shutdown()
        first["session"]["session_id"] = "id/one"
        self.assertTrue(record.create_session(first))
        collision = session_shutdown(2)
        collision["session"]["session_id"] = "id-one"
        with self.assertRaises(record.InvalidRecord):
            _ = record.create_session(collision)
        with self.assertRaises(record.InvalidRecord):
            _ = record.update_session(first["login"], "id-one", lambda current: None)
        with self.assertRaises(record.InvalidRecord):
            record.archive_session(first["login"], "id-one")
        self.assertEqual(record.find_session(first["login"], "id/one"), first)

    def test_session_records_reject_non_directory_accounts_and_sessions_paths(self) -> None:
        self.directory.parent.mkdir(parents=True)
        _ = self.directory.write_text("not a directory")
        with self.assertRaises(record.InvalidRecord):
            _ = record.session_records("owner@example.com")
        with self.assertRaises(record.InvalidRecord):
            _ = record.account_hold("owner@example.com")
        with self.assertRaises(record.InvalidRecord):
            _ = record.all_session_records()
        self.directory.unlink()
        self.directory.mkdir()
        _ = (self.directory / "sessions").write_text("not a directory")
        with self.assertRaises(record.InvalidRecord):
            _ = record.session_records("owner@example.com")

    def test_session_records_reject_non_regular_data_and_lock_files(self) -> None:
        self.directory.mkdir(parents=True)
        sessions = self.directory / "sessions"
        sessions.mkdir()
        for filename in ("sessions/session-1.json", "hold.json", "lock"):
            if filename == "lock":
                (self.directory / "lock").unlink(missing_ok=True)
            for kind in ("directory", "link", "fifo"):
                with self.subTest(filename=filename, kind=kind):
                    path = self.directory / filename
                    if kind == "directory":
                        path.mkdir()
                    elif kind == "link":
                        path.symlink_to(self.root / "absent")
                    else:
                        os.mkfifo(path)
                    with self.assertRaises(record.InvalidRecord):
                        if filename == "hold.json":
                            _ = record.account_hold("owner@example.com")
                        else:
                            _ = record.session_records("owner@example.com")
                    if kind == "directory":
                        path.rmdir()
                    else:
                        path.unlink()

    def test_session_records_lock_and_directory_read_failures_are_invalid_records(self) -> None:
        self.assertTrue(record.create_session(session_shutdown()))
        with patch.object(fcntl, "flock", side_effect=OSError("lock unavailable")):
            with self.assertRaisesRegex(record.InvalidRecord, "lock unavailable"):
                _ = record.session_records("owner@example.com")
        with patch.object(Path, "iterdir", side_effect=PermissionError("access refused")):
            with self.assertRaisesRegex(record.InvalidRecord, "access refused"):
                _ = record.session_records("owner@example.com")

    def test_session_records_read_errors_name_the_file(self) -> None:
        self.assertTrue(record.create_session(session_shutdown()))
        path = self.directory / "sessions" / "session-1.json"
        _ = path.write_text("invalid JSON")
        with self.assertRaises(record.InvalidRecord) as raised:
            _ = record.find_session("owner@example.com", "session-1")
        self.assertIn(str(path), str(raised.exception))
        _ = path.write_text(json.dumps(session_shutdown(login="other@example.com")))
        with self.assertRaises(record.InvalidRecord):
            _ = record.session_records("owner@example.com")
        _ = (self.directory / "hold.json").write_text(json.dumps(account_hold("other@example.com")))
        with self.assertRaises(record.InvalidRecord):
            _ = record.account_hold("owner@example.com")

    def test_session_records_atomic_update_failure_keeps_the_previous_file(self) -> None:
        saved = session_shutdown()
        self.assertTrue(record.create_session(saved))
        def change(current: record.SessionShutdown) -> None:
            current["progress"] = record.SessionStopped(kind="stopped", at=LATER)
        with patch.object(os, "replace", side_effect=OSError("replace refused")):
            with self.assertRaisesRegex(OSError, "replace refused"):
                _ = record.update_session(saved["login"], "session-1", change)
        self.assertEqual(record.find_session(saved["login"], "session-1"), saved)
        self.assertEqual([path.name for path in (self.directory / "sessions").iterdir()], ["session-1.json"])

    def test_session_records_unreadable_parked_directory_refuses_reads(self) -> None:
        self.directory.mkdir(parents=True)
        (self.directory / "partial").mkdir()
        with patch.object(Path, "iterdir", side_effect=PermissionError("partial unavailable")):
            for read in (record.session_records, record.account_hold):
                with self.assertRaisesRegex(record.InvalidRecord, "partial unavailable"):
                    _ = read("owner@example.com")

    def test_session_records_hold_write_never_replaces_unreadable_or_foreign_state(self) -> None:
        self.directory.mkdir(parents=True)
        path = self.directory / "hold.json"
        for text in ("invalid JSON", json.dumps(account_hold("other@example.com"))):
            with self.subTest(text=text):
                _ = path.write_text(text)
                with self.assertRaises(record.InvalidRecord):
                    record.set_account_hold(account_hold())
                self.assertEqual(path.read_text(), text)

    def test_session_records_validate_seats_and_claim_timestamps(self) -> None:
        saved = session_shutdown(progress=record.SessionRestarting(kind="restarting", at=LATER))
        saved["seats"] = [owned_seat()]
        saved["alerted_at"] = NOW
        self.assertEqual(record.parse_session_records(json.dumps([saved])), [saved])
        for field, value in (
            ("session", owned_seat()), ("seats", [session()]),
            ("seats", [owned_seat("another")]), ("progress", {"kind": "restarting"}),
            ("alerted_at", "2026-10-09T21:49:10Z"), ("hold", "all"), ("alerts", "phone"),
        ):
            with self.subTest(field=field):
                malformed = cast(dict[str, object], cast(object, session_shutdown()))
                malformed[field] = value
                with self.assertRaises(record.InvalidRecord):
                    _ = record.parse_session_records(json.dumps([malformed]))

    def test_session_records_convert_old_states_and_attach_seats_without_writing(self) -> None:
        for state in ("settling", "down", "restarting"):
            with self.subTest(state=state):
                login = f"{state}@example.com"
                old = shutdown_record(login, state=cast(record.ShutdownState, state))
                old["then"] = "restart"
                old["force"] = "now"
                old["entries"][0]["progress"] = record.SessionStopped(kind="stopped", at=LATER) if state == "down" else record.SessionWaiting(kind="waiting")
                worker = entry(record.PassiveSeatReadyToStop(kind="passive seat ready", at=NOW), number=9)
                worker["session"] = owned_seat()
                old["entries"].append(worker)
                record.create(old)
                directory = self.directory.parent / login
                before = (directory / "record.json").read_bytes()
                converted = record.session_records(login)
                self.assertEqual(len(converted), 1)
                self.assertEqual(converted[0]["session"], old["entries"][0]["session"])
                self.assertEqual(converted[0]["seats"], [worker["session"]])
                self.assertEqual(converted[0]["timers"], old["entries"][0]["timers"])
                self.assertEqual(converted[0]["hold"], "account")
                self.assertEqual(converted[0]["then"], "restart")
                self.assertEqual(converted[0]["alerts"], "user")
                self.assertEqual(converted[0]["force"], "now")
                expected = "stopped" if state in {"down", "restarting"} else "waiting"
                self.assertEqual(converted[0]["progress"]["kind"], expected)
                self.assertEqual((directory / "record.json").read_bytes(), before)
                self.assertFalse((directory / "sessions").exists())

    def test_session_records_old_restarting_keeps_completed_entries(self) -> None:
        old = shutdown_record(state="restarting")
        old["entries"] = [
            entry(record.SessionRestarted(kind="restarted", at=LATER)),
            entry(record.SessionNeedsManualRestart(kind="manual restart", command="resume"), number=2),
        ]
        record.create(old)
        converted = record.session_records(old["login"])
        self.assertEqual(converted[0]["progress"], old["entries"][0]["progress"])
        self.assertEqual(converted[1]["progress"], {"kind": "stopped", "at": NOW})

    def test_session_records_read_parked_and_selected_slot_records_together(self) -> None:
        old = shutdown_record(state="restart partial", progress=record.SessionRestartFailed(kind="restart failed", at=NOW, reason="not live"))
        record.create(old)
        _ = record.park(old["login"])
        selected = shutdown_record(requested_at=LATER)
        selected["scope"] = record.SelectedSessions(kind="selected", session_ids=["session-2"])
        selected["entries"] = [entry(number=2)]
        record.create(selected)
        records = record.session_records(old["login"])
        self.assertEqual([item["session"]["session_id"] for item in records], ["session-1", "session-2"])
        self.assertEqual([item["hold"] for item in records], ["account", "session"])
        self.assertEqual(record.all_session_records(), records)
        self.assertEqual(record.account_hold(old["login"]), account_hold())

    def test_session_records_slot_wins_over_parked_entry_with_the_same_request_time(self) -> None:
        parked = shutdown_record(
            state="restart partial",
            progress=record.SessionNeedsManualRestart(kind="manual restart", command="resume old"),
        )
        parked_worker = entry(number=8)
        parked_worker["session"] = owned_seat()
        parked["entries"].append(parked_worker)
        record.create(parked)
        _ = record.park(parked["login"])
        slot = shutdown_record(progress=record.SessionWaiting(kind="waiting"))
        slot_worker = entry(number=9)
        seat = owned_seat()
        seat["session_id"] = "slot-seat"
        slot_worker["session"] = seat
        slot["entries"].append(slot_worker)
        record.create(slot)

        found = record.find_session(slot["login"], "session-1")
        self.assertIsNotNone(found)
        if found is None:
            self.fail("expected the slot's session record")
        self.assertEqual(found["progress"], slot["entries"][0]["progress"])
        self.assertEqual(found["seats"], [seat])
        self.assertEqual(record.session_records(slot["login"]), [found])
        self.assertEqual(record.all_session_records(), [found])

    def test_session_records_newest_parked_entry_wins_for_one_session(self) -> None:
        older = shutdown_record(
            state="restart partial",
            progress=record.SessionNeedsManualRestart(kind="manual restart", command="resume old"),
        )
        record.create(older)
        _ = record.park(older["login"])
        newer = shutdown_record(
            state="restart partial", requested_at=LATER,
            progress=record.SessionRestartFailed(kind="restart failed", at=LATER, reason="new failure"),
        )
        worker = entry(number=9)
        worker["session"] = owned_seat()
        newer["entries"].append(worker)
        record.create(newer)
        _ = record.park(newer["login"])

        found = record.find_session(newer["login"], "session-1")
        self.assertIsNotNone(found)
        if found is None:
            self.fail("expected the newest parked session record")
        self.assertEqual(found["requested_at"], LATER)
        self.assertEqual(found["progress"], newer["entries"][0]["progress"])
        self.assertEqual(found["seats"], [worker["session"]])
        self.assertEqual(record.session_records(newer["login"]), [found])
        self.assertEqual(record.all_session_records(), [found])

    def test_session_records_new_files_suppress_matching_old_entries_only(self) -> None:
        old = shutdown_record()
        old["entries"].append(entry(number=2))
        record.create(old)
        saved = session_shutdown(requested_at=LATER, progress=record.SessionStopped(kind="stopped", at=LATER))
        sessions = self.directory / "sessions"
        sessions.mkdir()
        _ = (sessions / "session-1.json").write_text(json.dumps(saved))
        converted = record.session_records(old["login"])
        self.assertEqual([item["session"]["session_id"] for item in converted], ["session-2", "session-1"])
        self.assertEqual(record.find_session(old["login"], "session-1"), saved)
        self.assertFalse(record.create_session(session_shutdown(2)))
        self.assertFalse((sessions / "session-2.json").exists())

    def test_session_records_invalid_old_slot_and_parked_records_name_the_file(self) -> None:
        self.directory.mkdir(parents=True)
        partial = self.directory / "partial"
        partial.mkdir()
        for path in (self.directory / "record.json", partial / "old.json"):
            with self.subTest(path=path):
                _ = path.write_text("{}")
                for read in (record.session_records, record.account_hold):
                    with self.assertRaises(record.InvalidRecord) as raised:
                        _ = read("owner@example.com")
                    self.assertIn(str(path), str(raised.exception))
                path.unlink()

    def test_finished_old_cancelled_record_has_no_session_records_or_hold(self) -> None:
        saved = shutdown_record(state="cancelled")
        record.create(saved)
        path = self.directory / "record.json"
        before = path.read_bytes()

        self.assertEqual(record.session_records(saved["login"]), [])
        self.assertEqual(record.all_session_records(), [])
        self.assertIsNone(record.find_session(saved["login"], "session-1"))
        self.assertIsNone(record.account_hold(saved["login"]))
        self.assertEqual(path.read_bytes(), before)

    def test_finished_old_up_record_has_no_session_records_or_hold(self) -> None:
        saved = shutdown_record(state="up")
        record.create(saved)
        path = self.directory / "record.json"
        before = path.read_bytes()

        self.assertEqual(record.session_records(saved["login"]), [])
        self.assertEqual(record.all_session_records(), [])
        self.assertIsNone(record.find_session(saved["login"], "session-1"))
        self.assertIsNone(record.account_hold(saved["login"]))
        self.assertEqual(path.read_bytes(), before)

    def test_finished_old_records_must_still_validate(self) -> None:
        self.directory.mkdir(parents=True)
        path = self.directory / "record.json"
        for state in ("cancelled", "up"):
            with self.subTest(state=state):
                saved = cast(dict[str, object], cast(object, shutdown_record(
                    state=cast(record.ShutdownState, state),
                )))
                saved["entries"] = "invalid entries"
                _ = path.write_text(json.dumps(saved))
                for read in (record.session_records, record.account_hold):
                    with self.assertRaises(record.InvalidRecord) as raised:
                        _ = read("owner@example.com")
                    self.assertIn(str(path), str(raised.exception))

    def test_session_records_legacy_account_hold_excludes_selected_and_finished_records(self) -> None:
        old = shutdown_record()
        old["entries"] = []
        record.create(old)
        self.assertEqual(record.account_hold(old["login"]), account_hold())
        self.assertEqual(record.session_records(old["login"]), [])
        for state in ("cancelled", "up"):
            def change(current: record.ShutdownRecord) -> None:
                current["state"] = cast(record.ShutdownState, state)
            _ = record.update(old["login"], change)
            self.assertIsNone(record.account_hold(old["login"]))
        def select(current: record.ShutdownRecord) -> None:
            current["state"] = "settling"
            current["scope"] = record.SelectedSessions(kind="selected", session_ids=[])
        _ = record.update(old["login"], select)
        self.assertIsNone(record.account_hold(old["login"]))


if __name__ == "__main__":
    _ = unittest.main()
