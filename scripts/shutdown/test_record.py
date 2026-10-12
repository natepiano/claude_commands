"""Tests for the account-scoped shutdown record store."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Literal, TypedDict, NotRequired, cast, final, override
from collections.abc import Callable
from unittest.mock import Mock, patch

from test_support import ShutdownTestCase
import inventory
import record
from launch_permission import launch_barrier


NOW = "2026-10-09T21:49:10+00:00"
LATER = "2026-10-09T21:50:10+00:00"


class LegacyEntryFixture(TypedDict):
    session: inventory.Session
    timers: list[record.TimerRestore]
    settle_message: record.SettleMessage
    where: record.Where
    progress: object
    stop_issues: list[object]


class LegacyRecordFixture(TypedDict):
    login: str
    label: str
    machine: str
    state: str
    requested_at: str
    requested_by: record.RequestOrigin
    scope: dict[str, object]
    conductor: dict[str, object]
    force: record.StopTiming
    then: NotRequired[record.ShutdownContinuation]
    entries: list[LegacyEntryFixture]
    stop_issues: list[object]


def write_old(saved: LegacyRecordFixture) -> None:
    path = Path(os.environ["SHUTDOWN_STATE_DIR"]) / saved["login"].casefold() / "record.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(json.dumps(saved))


def move_old(login: str) -> None:
    directory = Path(os.environ["SHUTDOWN_STATE_DIR"]) / login.casefold()
    saved = cast(LegacyRecordFixture, json.loads((directory / "record.json").read_text()))
    partial = directory / "partial"
    partial.mkdir(exist_ok=True)
    _ = (directory / "record.json").replace(partial / (saved["requested_at"] + ".json"))


def change_old(login: str, change: Callable[[LegacyRecordFixture], None]) -> None:
    path = Path(os.environ["SHUTDOWN_STATE_DIR"]) / login.casefold() / "record.json"
    saved = cast(LegacyRecordFixture, json.loads(path.read_text()))
    change(saved)
    write_old(saved)


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
    progress: object = None,
    *,
    number: int = 1,
    timer_footer: record.ShowrunnerFooter | record.NoFooter | None = None,
    settle_message: record.SettleMessage | None = None,
    where: record.Where | None = None,
) -> LegacyEntryFixture:
    return LegacyEntryFixture(
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
    state: str = "settling",
    progress: record.SessionProgress | None = None,
    requested_at: str = NOW,
) -> LegacyRecordFixture:
    return LegacyRecordFixture(
        login=login,
        label=login.split("@", 1)[0],
        machine="natedev",
        state=state,
        requested_at=requested_at,
        requested_by=record.FromTerminal(kind="terminal"),
        scope={"kind": "all account sessions"},
        conductor={"kind": "not started"},
        force="wait for ready",
        entries=[entry(progress)],
        stop_issues=[],
    )


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
        request_id=f"legacy-natedev-{NOW}",
        hold=hold, then="stop", force="wait for ready", alerts="user",
        session=saved_entry["session"], seats=[], timers=saved_entry["timers"],
        settle_message=saved_entry["settle_message"], where=saved_entry["where"],
        progress=cast(record.SessionProgress, saved_entry["progress"]), stop_issues=[],
    )


def account_hold(login: str = "owner@example.com") -> record.AccountHold:
    return record.AccountHold(
        login=login, label=login.split("@", 1)[0], machine="natedev",
        requested_at=NOW, requested_by=record.FromTerminal(kind="terminal"), then="stop",
        request_id=f"legacy-natedev-{NOW}",
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
            current["progress"] = record.SessionRestarting(kind="restarting", at=LATER, prior=record.SessionStopped(kind="stopped", at=NOW))

        changed = record.update_session(first["login"], "session-1", change)
        self.assertEqual(changed["progress"], {"kind": "restarting", "at": LATER, "prior": {"kind": "stopped", "at": NOW}})
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
        write_old(old)
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

    def test_session_records_unreadable_partial_directory_refuses_reads(self) -> None:
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
        saved = session_shutdown(progress=record.SessionRestarting(kind="restarting", at=LATER, prior=record.SessionStopped(kind="stopped", at=NOW)))
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
                old = shutdown_record(login, state=state)
                old["then"] = "restart"
                old["force"] = "now"
                old["entries"][0]["progress"] = record.SessionStopped(kind="stopped", at=LATER) if state == "down" else record.SessionWaiting(kind="waiting")
                worker = entry({"kind": "passive seat ready", "at": NOW}, number=9)
                worker["session"] = owned_seat()
                old["entries"].append(worker)
                write_old(old)
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
        write_old(old)
        converted = record.session_records(old["login"])
        self.assertEqual(converted[0]["progress"], old["entries"][0]["progress"])
        self.assertEqual(converted[1]["progress"], {"kind": "stopped", "at": NOW})

    def test_session_records_read_partial_and_selected_legacy_requests_together(self) -> None:
        old = shutdown_record(state="restart partial", progress=record.SessionRestartFailed(kind="restart failed", at=NOW, reason="not live"))
        write_old(old)
        _ = move_old(old["login"])
        selected = shutdown_record(requested_at=LATER)
        selected["scope"] = {"kind": "selected", "session_ids": ["session-2"]}
        selected["entries"] = [entry(number=2)]
        write_old(selected)
        records = record.session_records(old["login"])
        self.assertEqual([item["session"]["session_id"] for item in records], ["session-1", "session-2"])
        self.assertEqual([item["hold"] for item in records], ["account", "session"])
        self.assertEqual(record.all_session_records(), records)
        self.assertEqual(record.account_hold(old["login"]), account_hold())

    def test_session_records_slot_wins_over_partial_entry_with_the_same_request_time(self) -> None:
        partial = shutdown_record(
            state="restart partial",
            progress=record.SessionNeedsManualRestart(kind="manual restart", command="resume old"),
        )
        partial_worker = entry(number=8)
        partial_worker["session"] = owned_seat()
        partial["entries"].append(partial_worker)
        write_old(partial)
        _ = move_old(partial["login"])
        slot = shutdown_record(progress=record.SessionWaiting(kind="waiting"))
        slot_worker = entry(number=9)
        seat = owned_seat()
        seat["session_id"] = "slot-seat"
        slot_worker["session"] = seat
        slot["entries"].append(slot_worker)
        write_old(slot)

        found = record.find_session(slot["login"], "session-1")
        self.assertIsNotNone(found)
        if found is None:
            self.fail("expected the slot's session record")
        self.assertEqual(found["progress"], slot["entries"][0]["progress"])
        self.assertEqual(found["seats"], [seat])
        self.assertEqual(record.session_records(slot["login"]), [found])
        self.assertEqual(record.all_session_records(), [found])

    def test_session_records_newest_partial_entry_wins_for_one_session(self) -> None:
        older = shutdown_record(
            state="restart partial",
            progress=record.SessionNeedsManualRestart(kind="manual restart", command="resume old"),
        )
        write_old(older)
        _ = move_old(older["login"])
        newer = shutdown_record(
            state="restart partial", requested_at=LATER,
            progress=record.SessionRestartFailed(kind="restart failed", at=LATER, reason="new failure"),
        )
        worker = entry(number=9)
        worker["session"] = owned_seat()
        newer["entries"].append(worker)
        write_old(newer)
        _ = move_old(newer["login"])

        found = record.find_session(newer["login"], "session-1")
        self.assertIsNotNone(found)
        if found is None:
            self.fail("expected the newest partial session record")
        self.assertEqual(found["requested_at"], LATER)
        self.assertEqual(found["progress"], newer["entries"][0]["progress"])
        self.assertEqual(found["seats"], [worker["session"]])
        self.assertEqual(record.session_records(newer["login"]), [found])
        self.assertEqual(record.all_session_records(), [found])

    def test_session_records_new_files_suppress_matching_old_entries_only(self) -> None:
        old = shutdown_record()
        old["entries"].append(entry(number=2))
        write_old(old)
        saved = session_shutdown(requested_at=LATER, progress=record.SessionStopped(kind="stopped", at=LATER))
        sessions = self.directory / "sessions"
        sessions.mkdir()
        _ = (sessions / "session-1.json").write_text(json.dumps(saved))
        converted = record.session_records(old["login"])
        self.assertEqual([item["session"]["session_id"] for item in converted], ["session-2", "session-1"])
        self.assertEqual(record.find_session(old["login"], "session-1"), saved)
        self.assertFalse(record.create_session(session_shutdown(2)))
        self.assertFalse((sessions / "session-2.json").exists())

    def test_session_records_invalid_old_slot_and_partial_records_name_the_file(self) -> None:
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
        write_old(saved)
        path = self.directory / "record.json"
        before = path.read_bytes()

        self.assertEqual(record.session_records(saved["login"]), [])
        self.assertEqual(record.all_session_records(), [])
        self.assertIsNone(record.find_session(saved["login"], "session-1"))
        self.assertIsNone(record.account_hold(saved["login"]))
        self.assertEqual(path.read_bytes(), before)

    def test_finished_old_up_record_has_no_session_records_or_hold(self) -> None:
        saved = shutdown_record(state="up")
        write_old(saved)
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
                    state=state,
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
        write_old(old)
        self.assertEqual(record.account_hold(old["login"]), account_hold())
        self.assertEqual(record.session_records(old["login"]), [])
        for state in ("cancelled", "up"):
            def change(current: LegacyRecordFixture) -> None:
                current["state"] = state
            _ = change_old(old["login"], change)
            self.assertIsNone(record.account_hold(old["login"]))
        def select(current: LegacyRecordFixture) -> None:
            current["state"] = "settling"
            current["scope"] = {"kind": "selected", "session_ids": []}
        _ = change_old(old["login"], select)
        self.assertIsNone(record.account_hold(old["login"]))


    def test_migrate_each_legacy_state_and_keep_its_inferred_hold(self) -> None:
        for state in ("settling", "stopping", "down", "stop partial", "restarting", "restart partial", "cancelled", "up"):
            with self.subTest(state=state):
                saved = shutdown_record(f"{state}@example.com", state=state)
                saved["conductor"] = {"kind": "systemd", "unit": "old-service"}
                saved["stop_issues"] = [{"kind": "machine stop failed", "at": NOW, "machine": "Mac", "reason": "not reached"}]
                worker = entry({"kind": "passive seat ready", "at": NOW}, number=9)
                worker["session"] = owned_seat()
                saved["entries"].append(worker)
                write_old(saved)
                before = record.session_records(saved["login"])
                hold = record.account_hold(saved["login"])
                lines = record.migrate(saved["login"])
                self.assertEqual(len(lines), 1)
                self.assertIn(f"converted the {state} record", lines[0])
                self.assertEqual(record.session_records(saved["login"]), before)
                self.assertEqual(record.account_hold(saved["login"]), hold)
                directory = self.directory.parent / saved["login"]
                self.assertFalse((directory / "record.json").exists())
                self.assertTrue((directory / "history" / f"{NOW}-record.json").is_file())
                self.assertEqual(record.migrate(saved["login"]), [])

    def test_session_reads_without_existing_lock_do_not_change_the_files(self) -> None:
        write_old(shutdown_record(state="down"))
        before = {path.relative_to(self.directory): path.read_bytes() for path in self.directory.rglob("*") if path.is_file()}
        self.assertEqual(len(record.session_records("owner@example.com")), 1)
        self.assertIsNotNone(record.find_session("owner@example.com", "session-1"))
        self.assertIsNotNone(record.account_hold("owner@example.com"))
        after = {path.relative_to(self.directory): path.read_bytes() for path in self.directory.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

    def test_migrate_invalid_finished_file_moves_and_writes_nothing(self) -> None:
        write_old(shutdown_record())
        partial = self.directory / "partial"
        partial.mkdir()
        broken = partial / "finished.json"
        _ = broken.write_text(json.dumps({"state": "up"}))
        before = {path.relative_to(self.directory): path.read_bytes() for path in self.directory.rglob("*.json")}
        with self.assertRaises(record.InvalidRecord) as raised:
            _ = record.migrate("owner@example.com")
        self.assertIn(str(broken), str(raised.exception))
        after = {path.relative_to(self.directory): path.read_bytes() for path in self.directory.rglob("*.json")}
        self.assertEqual(after, before)

    def test_migrate_preserves_explicit_hold_and_new_session_precedence(self) -> None:
        explicit = account_hold()
        explicit["request_id"] = "current-hold"
        record.set_account_hold(explicit)
        fresh = session_shutdown()
        self.assertTrue(record.create_session(fresh))
        write_old(shutdown_record(progress=record.SessionNeedsManualRestart(kind="manual restart", command="old")))
        _ = record.migrate(fresh["login"])
        self.assertEqual(record.find_session(fresh["login"], "session-1"), fresh)
        self.assertEqual(record.account_hold(fresh["login"]), explicit)

    def test_migrate_refuses_a_legacy_id_that_collides_with_a_current_filename(self) -> None:
        current = session_shutdown()
        current["session"]["session_id"] = "id-one"
        self.assertTrue(record.create_session(current))
        old = shutdown_record()
        old["entries"][0]["session"]["session_id"] = "id/one"
        write_old(old)
        before = {path.relative_to(self.directory): path.read_bytes()
                  for path in self.directory.rglob("*") if path.is_file()}
        with self.assertRaises(record.InvalidRecord) as raised:
            _ = record.migrate(current["login"])
        self.assertIn("id-one", str(raised.exception))
        self.assertIn("id/one", str(raised.exception))
        after = {path.relative_to(self.directory): path.read_bytes()
                 for path in self.directory.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

    def test_stop_incomplete_includes_failed_stops_and_terminal_resource_issues(self) -> None:
        for kind in ("stopped", "already gone", "process identity lost", "stop failed", "waiting", "restarted"):
            for issues in (False, True):
                with self.subTest(kind=kind, issues=issues):
                    saved = session_shutdown()
                    progress: dict[str, object] = {"kind": kind, "at": NOW}
                    if kind == "stop failed":
                        progress["reason"] = "still running"
                    saved["progress"] = cast(record.SessionProgress, cast(object, progress))
                    if issues:
                        saved["stop_issues"] = [record.UnitTmuxLeftRunning(
                            kind="unit tmux session left running", at=NOW,
                            tmux_session="unit", cause="stop not confirmed")]
                    self.assertEqual(record.stop_incomplete(saved), kind == "stop failed" or (
                        issues and kind in {"stopped", "already gone", "process identity lost"}))

    def test_withdraw_session_keeps_a_stop_claim_and_does_not_restore_timers(self) -> None:
        saved = session_shutdown(progress=record.SessionStopping(kind="stopping", at=NOW))
        self.assertTrue(record.create_session(saved))
        with patch.object(record, "archive_session") as archive:
            restore = Mock()
            result = record.withdraw_session(saved["login"], "session-1", restore)
        self.assertIsInstance(result, record.SessionRetained)
        restore.assert_not_called()
        archive.assert_not_called()
        self.assertEqual(record.find_session(saved["login"], "session-1"), saved)

    def test_withdraw_session_restores_and_archives_under_the_account_lock(self) -> None:
        saved = session_shutdown()
        self.assertTrue(record.create_session(saved))
        def restore(_saved: record.SessionShutdown) -> None:
            with (self.directory / "lock").open("a+") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = record.withdraw_session(saved["login"], "session-1", restore)
        self.assertIsInstance(result, record.SessionWithdrawn)
        self.assertIsNone(record.find_session(saved["login"], "session-1"))

    def test_conductor_lock_allows_only_one_local_loop(self) -> None:
        with record.conductor_lock("owner@example.com") as first:
            self.assertIsInstance(first, record.ConductorAcquired)
            with record.conductor_lock("owner@example.com") as second:
                self.assertIsInstance(second, record.ConductorAlreadyRunning)
        with record.conductor_lock("owner@example.com") as third:
            self.assertIsInstance(third, record.ConductorAcquired)


    def test_failed_stop_withdrawal_checks_identity_and_restores_under_account_lock(self) -> None:
        saved = session_shutdown(progress={"kind": "stop failed", "at": NOW, "reason": "still live"})
        self.assertTrue(record.create_session(saved))
        events: list[str] = []
        def check_lock() -> None:
            with (self.directory / "lock").open("r") as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        def identity(current: record.SessionShutdown) -> bool:
            check_lock()
            self.assertEqual(current, saved)
            events.append("identity")
            return True
        def restore(current: record.SessionShutdown) -> None:
            check_lock()
            self.assertEqual(current, saved)
            events.append("restore")
        result = record.withdraw_failed_stop(saved["login"], "session-1", identity, restore)
        self.assertIsInstance(result, record.SessionWithdrawn)
        self.assertEqual(events, ["identity", "restore"])
        self.assertIsNone(record.find_session(saved["login"], "session-1"))
        self.assertEqual(len(list((self.directory / "history").glob("*.json"))), 1)

    def test_failed_stop_withdrawal_keeps_unmatched_identity_without_resource_changes(self) -> None:
        saved = session_shutdown(progress={"kind": "stop failed", "at": NOW, "reason": "still live"})
        self.assertTrue(record.create_session(saved))
        restore = Mock()
        result = record.withdraw_failed_stop(saved["login"], "session-1", lambda _saved: False, restore)
        self.assertIsInstance(result, record.SessionRetained)
        restore.assert_not_called()
        self.assertEqual(record.find_session(saved["login"], "session-1"), saved)

    def test_failed_stop_withdrawal_loses_to_a_competing_stop_claim(self) -> None:
        saved = session_shutdown(progress={"kind": "stop failed", "at": NOW, "reason": "still live"})
        self.assertTrue(record.create_session(saved))
        ready, finished = threading.Event(), threading.Event()
        outcomes: list[record.SessionWithdrawn | record.SessionRetained] = []
        failures: list[BaseException] = []
        identity, restore = Mock(return_value=True), Mock()
        def cancel() -> None:
            try:
                ready.set()
                outcomes.append(record.withdraw_failed_stop(saved["login"], "session-1", identity, restore))
            except BaseException as error:
                failures.append(error)
            finally:
                finished.set()
        def claim(current: record.SessionShutdown) -> None:
            worker.start()
            self.assertTrue(ready.wait(2))
            self.assertFalse(finished.wait(0.05))
            current["progress"] = {"kind": "stopping", "at": LATER}
        worker = threading.Thread(target=cancel)
        try:
            claimed = record.update_session(saved["login"], "session-1", claim)
        finally:
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], record.SessionRetained)
        identity.assert_not_called()
        restore.assert_not_called()
        self.assertEqual(record.find_session(saved["login"], "session-1"), claimed)

    def test_conductor_status_observes_held_released_absent_and_unreadable_lock(self) -> None:
        login = "owner@example.com"
        self.assertIsInstance(record.conductor_status(login), record.ConductorNotRunning)
        self.assertFalse(self.directory.exists())
        with record.conductor_lock(login):
            before = {path: path.read_bytes() for path in self.directory.iterdir()}
            self.assertIsInstance(record.conductor_status(login), record.ConductorRunning)
            self.assertEqual({path: path.read_bytes() for path in self.directory.iterdir()}, before)
        self.assertIsInstance(record.conductor_status(login), record.ConductorNotRunning)
        (self.directory / "conduct.lock").unlink()
        self.assertIsInstance(record.conductor_status(login), record.ConductorNotRunning)
        self.assertFalse((self.directory / "conduct.lock").exists())
        (self.directory / "conduct.lock").mkdir()
        self.assertIsInstance(record.conductor_status(login), record.ConductorUnavailable)

    def test_named_shutdown_and_account_lookup_outcomes_preserve_domain_meaning(self) -> None:
        login = "owner@example.com"
        self.assertIsInstance(record.lookup_session(login, "session-1"), record.NoShutdown)
        self.assertIsInstance(record.lookup_account_hold(login), record.AccountOpen)
        saved, held = session_shutdown(), account_hold()
        self.assertTrue(record.create_session(saved))
        record.set_account_hold(held)
        self.assertEqual(record.lookup_session(login, "session-1"), record.ShutdownFound(saved))
        self.assertEqual(record.lookup_account_hold(login), record.AccountHeld(held))

    def test_conductor_start_retries_a_brief_observation_lock(self) -> None:
        login = "owner@example.com"
        with record.conductor_lock(login):
            pass
        path = self.directory / "conduct.lock"
        started = threading.Event()
        outcomes: list[record.ConductorAcquired | record.ConductorAlreadyRunning] = []
        failures: list[BaseException] = []
        def start() -> None:
            try:
                started.set()
                with record.conductor_lock(login) as outcome:
                    outcomes.append(outcome)
            except BaseException as error:
                failures.append(error)
        with path.open("r") as observation:
            fcntl.flock(observation.fileno(), fcntl.LOCK_EX)
            worker = threading.Thread(target=start)
            worker.start()
            self.assertTrue(started.wait(2))
            time.sleep(0.05)
            fcntl.flock(observation.fileno(), fcntl.LOCK_UN)
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], record.ConductorAcquired)

    def test_conductor_start_rejects_a_persistent_owner_within_bounded_time(self) -> None:
        login = "owner@example.com"
        with record.conductor_lock(login):
            started = time.monotonic()
            with record.conductor_lock(login) as duplicate:
                elapsed = time.monotonic() - started
                self.assertIsInstance(duplicate, record.ConductorAlreadyRunning)
        self.assertGreaterEqual(elapsed, 0.15)
        self.assertLess(elapsed, 1.0)


if __name__ == "__main__":
    _ = unittest.main()
