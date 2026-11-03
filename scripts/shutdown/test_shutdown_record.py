"""Tests for the account-scoped shutdown record store."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Literal, TypedDict, NotRequired, cast, final, override
from collections.abc import Callable, Generator
from contextlib import contextmanager
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from unit_staff_test_support import unit_host
from test_support import ShutdownTestCase
import inventory
import shutdown_record as shutdown_store
from scripts.production import staff
from scripts.production.staff_test_support import fixture_member, fixture_worker


# Hang guard: natedev reached load 136 on 2026-10-29; scheduling delays are not failures.
HANG_GUARD_SECONDS = 300

NOW = "2026-10-09T21:49:10+00:00"
LATER = "2026-10-09T21:50:10+00:00"


@contextmanager
def observe_lock_contention(blocked: threading.Event) -> Generator[None, None, None]:
    """Report an actually held lock before entering its normal blocking wait."""
    real_flock = fcntl.flock

    def flock(descriptor: int, operation: int) -> None:
        if operation == fcntl.LOCK_EX:
            try:
                real_flock(descriptor, operation | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                blocked.set()
        real_flock(descriptor, operation)

    with patch.object(fcntl, "flock", side_effect=flock):
        yield


class RecordModuleIdentityTests(unittest.TestCase):
    def test_shutdown_and_buildlog_record_names_load_their_own_files_in_either_order(self) -> None:
        scripts = Path(__file__).resolve().parents[1]
        probe = "\n".join([
            "import importlib, sys",
            "from pathlib import Path",
            "scripts = Path(sys.argv[1])",
            "sys.path[:0] = [str(scripts / part) for part in sys.argv[2:4]]",
            "for name in sys.argv[4:]: importlib.import_module(name)",
            "import record, shutdown_record",
            "assert Path(record.__file__).resolve() == scripts / 'buildlog' / 'record.py'",
            "assert Path(shutdown_record.__file__).resolve() == scripts / 'shutdown' / 'shutdown_record.py'",
            "assert record is not shutdown_record",
        ])
        for folders in (("shutdown", "buildlog"), ("buildlog", "shutdown")):
            for names in (("shutdown_record", "record"), ("record", "shutdown_record")):
                with self.subTest(folders=folders, imports=names):
                    result = subprocess.run(
                        [sys.executable, "-c", probe, str(scripts), *folders, *names],
                        capture_output=True, text=True, check=False, timeout=HANG_GUARD_SECONDS,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)


class ProgressWriteTime(datetime):
    at: str = NOW

    @classmethod
    @override
    def now(cls, tz: tzinfo | None = None) -> "ProgressWriteTime":
        return cls.fromisoformat(cls.at).astimezone(tz)


class LegacyEntryFixture(TypedDict):
    session: inventory.Session
    timers: list[shutdown_store.TimerRestore]
    settle_message: shutdown_store.SettleMessage
    where: shutdown_store.Where
    progress: object
    stop_issues: list[object]


class LegacyRecordFixture(TypedDict):
    login: str
    label: str
    machine: str
    state: str
    requested_at: str
    requested_by: shutdown_store.RequestOrigin
    scope: dict[str, object]
    conductor: dict[str, object]
    force: shutdown_store.StopTiming
    then: NotRequired[shutdown_store.ShutdownContinuation]
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
        probe=False,
        session_id=f"session-{number}",
        pid=10_000 + number,
        proc_start=f"start-{number}",
        name=f"Claude {number}",
        cwd=f"/tmp/checkout-{number}",
        status="idle",
        updated_at=0,
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
    settle_message: shutdown_store.SettleMessage | None = None,
    where: shutdown_store.Where | None = None,
) -> LegacyEntryFixture:
    return LegacyEntryFixture(
        session=session(number),
        timers=[
            shutdown_store.TimerRestore(
                instance=f"timer-{number}",
                was_enabled=True,
            )
        ],
        settle_message=settle_message or shutdown_store.SettleMessageNotSent(kind="not sent"),
        where=where or shutdown_store.WhereNotSaid(kind="not said"),
        progress=progress or shutdown_store.SessionWaiting(kind="waiting"),
        stop_issues=[],
    )


def shutdown_record(
    login: str = "owner@example.com",
    *,
    state: str = "settling",
    progress: shutdown_store.SessionProgress | None = None,
    requested_at: str = NOW,
) -> LegacyRecordFixture:
    return LegacyRecordFixture(
        login=login,
        label=login.split("@", 1)[0],
        machine="natedev",
        state=state,
        requested_at=requested_at,
        requested_by=shutdown_store.FromTerminal(kind="terminal"),
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
    progress: shutdown_store.SessionProgress | None = None,
    hold: Literal["account", "session"] = "session",
) -> shutdown_store.SessionShutdown:
    saved_entry = entry(progress, number=number)
    return shutdown_store.SessionShutdown(
        login=login, label=login.split("@", 1)[0], machine="natedev",
        requested_at=requested_at, requested_by=shutdown_store.FromTerminal(kind="terminal"),
        request_id=f"legacy-natedev-{NOW}",
        hold=hold, then="stop", force="wait for ready", alerts="user", probe=False,
        session=saved_entry["session"], workers=[], timers=saved_entry["timers"],
        settle_message=saved_entry["settle_message"], where=saved_entry["where"],
        progress=cast(shutdown_store.SessionProgress, saved_entry["progress"]), trail=[], stop_issues=[],
    )


def account_hold(login: str = "owner@example.com") -> shutdown_store.AccountHold:
    return shutdown_store.AccountHold(
        login=login, label=login.split("@", 1)[0], machine="natedev",
        requested_at=NOW, requested_by=shutdown_store.FromTerminal(kind="terminal"), then="stop",
        request_id=f"legacy-natedev-{NOW}",
    )


def owned_worker(owner: str = "session-1") -> inventory.WorkerSession:
    runner = fixture_member("showrunner", name="record-tests")
    parent = fixture_member("unit", name="worker-owner", parent=runner.id,
                            worktree=staff.PresentWorktree("natedev", Path(os.environ["SHUTDOWN_STATE_DIR"]) / "worker"))
    row = fixture_worker("worker", parent.id)
    return inventory.WorkerSession(
        kind="worker", probe=False, session_id=row.current_session, pid=20001, proc_start="worker-start",
        name="worker", cwd=str(parent.worktree.path) if isinstance(parent.worktree, staff.PresentWorktree) else "", status="idle", updated_at=0,
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

    def unit_row(self) -> staff.StaffRow:
        runner = fixture_member("showrunner", name="record-tests")
        return fixture_member("unit", name="one", parent=runner.id,
                              worktree=staff.PresentWorktree("natedev", Path(session()["cwd"])))

    def test_unit_and_worker_records_follow_staff_without_a_record_rewrite(self) -> None:
        row = self.unit_row()
        owner = cast(inventory.UnitSession, cast(object, {**session(), "kind": "unit",
                     "session_id": row.current_session,
                     "host": unit_host(kind="unit", production="record-tests", unit="one", doc=str(self.root / "production.md"),
                                       session_name="one", session_host="herdr",
                                       plan=inventory.NoRunRecord(kind="no run record"))}))
        worker_row = fixture_worker("reader", row.id)
        worker = owned_worker(owner["session_id"])
        worker["session_id"] = worker_row.current_session
        worker["cwd"] = owner["cwd"]
        owner["checkout"] = inventory.GitCheckout(kind="git", path=owner["cwd"], head=inventory.OnBranch(kind="branch", name="topic"),
                                                   upstream=inventory.NoUpstream(kind="no upstream"), dirty=[])
        saved = session_shutdown()
        saved["session"], saved["workers"] = owner, [worker]
        self.assertTrue(shutdown_store.create_session(saved))
        path = self.directory / "sessions" / f"{row.current_session}.json"
        fields = cast(dict[str, object], json.loads(path.read_text()))
        stored_owner = cast(dict[str, object], fields["session"])
        self.assertNotIn("cwd", stored_owner)
        self.assertNotIn("path", cast(dict[str, object], stored_owner["checkout"]))
        self.assertNotIn("cwd", cast(list[dict[str, object]], fields["workers"])[0])
        before = path.read_bytes(), path.stat().st_mtime_ns
        destination = self.root / "staff-folder"
        _ = staff.set_worktree(row.id, staff.PresentWorktree("natedev", destination), expected=row.worktree)
        loaded = shutdown_store.find_session(saved["login"], row.current_session)
        assert loaded is not None
        self.assertEqual(loaded["session"]["cwd"], str(destination))
        self.assertEqual(loaded["session"]["checkout"], inventory.GitCheckout(kind="git", path=str(destination),
                         head=inventory.OnBranch(kind="branch", name="topic"),
                         upstream=inventory.NoUpstream(kind="no upstream"), dirty=[]))
        self.assertEqual(loaded["workers"][0]["cwd"], str(destination))
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_legacy_not_ready_failure_recovers_at_every_record_read_boundary(self) -> None:
        for index, progress in enumerate(("waiting", "ready", "held by usage limit")):
            with self.subTest(progress=progress):
                saved = session_shutdown(index + 1, progress={"kind": "stop failed", "at": LATER,
                    "reason": "session or worker is not idle and ready"})
                saved["stop_issues"] = [shutdown_store.NotReadyToStop(kind="not ready to stop", at=NOW,
                    status="busy", progress=progress)]
                directory = self.directory / "sessions"
                directory.mkdir(parents=True, exist_ok=True)
                _ = (directory / f"{saved["session"]["session_id"]}.json").write_text(json.dumps(saved))
                expected: shutdown_store.SessionProgress = {"kind": "waiting"} if progress == "waiting" else (
                    shutdown_store.SessionReadyToStop(kind="ready", at=NOW) if progress == "ready" else
                    shutdown_store.SessionHeldByUsageLimit(kind="held by usage limit", at=NOW))
                identity = saved["session"]["session_id"]
                for loaded in (shutdown_store.find_session(saved["login"], identity),
                               next(item for item in shutdown_store.all_session_records() if item["session"]["session_id"] == identity),
                               shutdown_store.parse_session_records(json.dumps([saved]))[0]):
                    assert loaded is not None
                    self.assertEqual(loaded["progress"], expected)
                    self.assertEqual(loaded["stop_issues"], saved["stop_issues"])
                    self.assertFalse(shutdown_store.stop_incomplete(loaded))

    def test_old_account_record_recovers_not_ready_failure_before_conversion(self) -> None:
        old = shutdown_record(progress={"kind": "stop failed", "at": LATER,
            "reason": "session or worker is not idle and ready"})
        old["entries"][0]["stop_issues"] = [shutdown_store.NotReadyToStop(kind="not ready to stop",
            at=NOW, status="busy", progress="ready")]
        write_old(old)
        loaded = shutdown_store.find_session(old["login"], "session-1")
        assert loaded is not None
        self.assertEqual(loaded["progress"], {"kind": "ready", "at": NOW})
        self.assertEqual(loaded["stop_issues"], old["entries"][0]["stop_issues"])

    def test_real_stop_failure_with_not_ready_issue_is_not_normalized(self) -> None:
        for number, reason, extra_issue in ((1, "signal refused", False),
                (2, "session or worker is not idle and ready", True)):
            with self.subTest(reason=reason, extra_issue=extra_issue):
                saved = session_shutdown(number, progress={"kind": "stop failed", "at": LATER, "reason": reason})
                saved["stop_issues"] = [shutdown_store.NotReadyToStop(kind="not ready to stop", at=NOW,
                    status="busy", progress="ready")]
                if extra_issue:
                    saved["stop_issues"].append(shutdown_store.StillRunningAfterStop(kind="still running", at=LATER, reason="still alive"))
                directory = self.directory / "sessions"
                directory.mkdir(parents=True, exist_ok=True)
                _ = (directory / f"{saved["session"]["session_id"]}.json").write_text(json.dumps(saved))
                loaded = shutdown_store.find_session(saved["login"], saved["session"]["session_id"])
                self.assertEqual(loaded, saved)
                self.assertEqual(shutdown_store.parse_session_records(json.dumps([saved])), [saved])

    def test_transition_id_round_trips_beside_the_independent_request_id_without_migrating_old_records(self) -> None:
        current = session_shutdown()
        current["move_id"] = "whole-group-transition"
        legacy = session_shutdown(2)
        for saved in (current, legacy):
            self.assertTrue(shutdown_store.create_session(saved))
        before = {path: path.read_bytes() for path in self.directory.rglob("*.json")}
        self.assertEqual(shutdown_store.find_session(current["login"], current["session"]["session_id"]), current)
        loaded = shutdown_store.find_session(legacy["login"], legacy["session"]["session_id"])
        self.assertEqual(loaded, legacy)
        assert loaded is not None
        self.assertNotIn("move_id", loaded)
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_transition_id_rejects_nontext_values_without_creating_a_record(self) -> None:
        for value in (7, None, ["transition"]):
            with self.subTest(value=value):
                malformed = {**session_shutdown(), "move_id": value}
                with self.assertRaises(shutdown_store.InvalidRecord):
                    _ = shutdown_store.create_session(cast(shutdown_store.SessionShutdown, cast(object, malformed)))
                self.assertEqual(shutdown_store.session_records("owner@example.com"), [])

    def test_legacy_record_labels_load_canonically_without_rewriting_the_file(self) -> None:
        saved = session_shutdown()
        saved.update(label="claude2", source_label="claude2", target="claude2")
        self.assertTrue(shutdown_store.create_session(saved))
        path = next((self.directory / "sessions").glob("*.json"))
        original = path.read_bytes().replace(b"claude2", b"claude 2")
        _ = path.write_bytes(original)
        loaded = shutdown_store.find_session(saved["login"], "session-1")
        assert loaded is not None
        self.assertEqual((loaded["label"], loaded.get("source_label"), loaded.get("target")),
                         ("claude2", "claude2", "claude2"))
        self.assertEqual(path.read_bytes(), original)

    def test_legacy_home_move_target_uses_cutover_evidence_instead_of_the_source_login(self) -> None:
        notes = self.root / "notes"
        notes.mkdir()
        _ = (notes / "claude1.md").write_text("---\nlogin: owner@example.com\n---\n")
        _ = (notes / "claude2.md").write_text("---\nlogin: home@example.invalid\n---\n")
        configuration = self.root / ".claude/config/accounts.json"
        configuration.parent.mkdir(parents=True, exist_ok=True)
        _ = configuration.write_text(json.dumps({"home_cutovers": [{
            "label": "claude2", "login": "home@example.invalid", "at": LATER}]}))
        _ = (self.root / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "different@example.invalid"}}))
        saved = session_shutdown()
        saved["target"] = "claude2"
        self.assertTrue(shutdown_store.create_session(saved))
        path = next((self.directory / "sessions").glob("*.json"))
        original = path.read_bytes()
        for target in ("default", "~/.claude"):
            with self.subTest(target=target):
                legacy = original.replace(b'"target": "claude2"', f'"target": "{target}"'.encode())
                _ = path.write_bytes(legacy)
                with patch.dict(os.environ, {"AGENT_NOTES_DIR": str(notes)}):
                    loaded = shutdown_store.find_session(saved["login"], "session-1")
                assert loaded is not None
                self.assertEqual(loaded.get("target"), "claude2")
                self.assertEqual(path.read_bytes(), legacy)

    def test_first_read_converts_nested_worker_ownership_and_fields_without_rewriting_again(self) -> None:
        saved = session_shutdown(progress=shutdown_store.SessionStopping(kind="stopping", at=LATER))
        saved["stop_issues"] = [shutdown_store.NotReadyToStop(kind="not ready to stop", at=NOW,
                                                    status="idle", progress="waiting")]
        worker = owned_worker()
        worker["codex_servers"] = [inventory.CodexServer(run_dir="/tmp/run", pid=123,
                                                        busy_workers=["writer"])]
        saved["workers"] = [worker]
        self.assertTrue(shutdown_store.create_session(saved))
        expected = shutdown_store.find_session(saved["login"], "session-1")
        path = next((self.directory / "sessions").glob("*.json"))
        old = (path.read_text().replace('"busy_workers"', '"busy_seats"')
               .replace('"workers"', '"seats"').replace('"kind": "worker"', '"kind": "seat"'))
        _ = path.write_text(old)
        self.assertEqual(shutdown_store.find_session(saved["login"], "session-1"), expected)
        rewritten = path.read_text()
        self.assertIn('"workers"', rewritten)
        self.assertNotIn('"seats"', rewritten)
        self.assertNotIn('"kind": "seat"', rewritten)
        self.assertNotIn('"busy_seats"', rewritten)
        self.assertIn('"kind": "worker"', rewritten)
        modified = path.stat().st_mtime_ns
        self.assertEqual(shutdown_store.find_session(saved["login"], "session-1"), expected)
        self.assertEqual(path.stat().st_mtime_ns, modified)

    def test_first_read_rewrites_legacy_worker_progress_and_issues_once(self) -> None:
        saved = shutdown_record()
        saved["entries"][0]["progress"] = {"kind": "passive seat ready", "at": NOW}
        saved["entries"][0]["stop_issues"] = [{"kind": "seat still live", "at": NOW}]
        write_old(saved)
        _ = shutdown_store.session_records(saved["login"])
        path = self.directory / "record.json"
        rewritten = path.read_text()
        self.assertIn('"passive worker ready"', rewritten)
        self.assertIn('"worker still live"', rewritten)
        modified = path.stat().st_mtime_ns
        _ = shutdown_store.session_records(saved["login"])
        self.assertEqual(path.stat().st_mtime_ns, modified)

    def test_first_read_converts_legacy_stop_issue_progress_and_preserves_the_issue(self) -> None:
        saved = shutdown_record()
        saved["entries"][0]["stop_issues"] = [{"kind": "not ready to stop", "at": NOW,
                                               "status": "idle", "progress": "passive seat ready"}]
        write_old(saved)
        expected = shutdown_store.NotReadyToStop(kind="not ready to stop", at=NOW,
                                         status="idle", progress="passive worker ready")
        loaded = shutdown_store.session_records(saved["login"])
        self.assertEqual(loaded[0]["stop_issues"], [expected])
        path = self.directory / "record.json"
        rewritten = path.read_text()
        self.assertNotIn("passive seat ready", rewritten)
        self.assertIn("passive worker ready", rewritten)
        modified = path.stat().st_mtime_ns
        self.assertEqual(shutdown_store.session_records(saved["login"]), loaded)
        self.assertEqual(path.stat().st_mtime_ns, modified)

    def test_first_read_converts_session_stop_issue_progress_without_rewriting_it_again(self) -> None:
        saved = session_shutdown()
        saved["stop_issues"] = [shutdown_store.NotReadyToStop(kind="not ready to stop", at=NOW,
                                                    status="idle", progress="passive worker ready")]
        self.assertTrue(shutdown_store.create_session(saved))
        path = self.directory / "sessions/session-1.json"
        _ = path.write_text(path.read_text().replace("passive worker ready", "passive seat ready"))
        self.assertEqual(shutdown_store.find_session(saved["login"], "session-1"), saved)
        rewritten = path.read_text()
        self.assertNotIn("passive seat ready", rewritten)
        self.assertIn("passive worker ready", rewritten)
        modified = path.stat().st_mtime_ns
        self.assertEqual(shutdown_store.find_session(saved["login"], "session-1"), saved)
        self.assertEqual(path.stat().st_mtime_ns, modified)

    def test_session_records_create_and_find_independent_sessions_in_one_account(self) -> None:
        first = session_shutdown(login="Owner@Example.com")
        second = session_shutdown(2, requested_at=LATER)
        self.assertTrue(shutdown_store.create_session(first))
        self.assertTrue(shutdown_store.create_session(second))
        self.assertEqual(shutdown_store.find_session("OWNER@EXAMPLE.COM", "session-1"), first)
        self.assertEqual(shutdown_store.session_records(second["login"]), [first, second])
        self.assertEqual(shutdown_store.parse_session_records(json.dumps([first, second])), [first, second])
        self.assertFalse((self.directory / "record.json").exists())

    def test_herdr_unit_session_leftover_issues_round_trip(self) -> None:
        for host in ("herdr",):
            with self.subTest(host=host):
                saved = session_shutdown()
                saved["stop_issues"] = [shutdown_store.UnitSessionLeftRunning(
                    kind="unit session left running", at=NOW, host=host,
                    session_name="named-unit", cause="stop not confirmed")]
                self.assertEqual(shutdown_store.parse_session_records(json.dumps([saved])), [saved])

    def test_pre_trail_record_loads_without_inventing_history(self) -> None:
        saved = session_shutdown()
        document = cast(dict[str, object], json.loads(json.dumps(saved)))
        _ = document.pop("trail", None)
        path = self.directory / "sessions/session-1.json"
        path.parent.mkdir(parents=True)
        _ = path.write_text(json.dumps(document))
        before = path.read_bytes()
        found = shutdown_store.find_session(saved["login"], "session-1")
        assert found is not None
        self.assertEqual(found["trail"], [])
        self.assertEqual(found["progress"], {"kind": "waiting"})
        self.assertEqual(path.read_bytes(), before)

    def test_progress_trail_keeps_order_details_and_times_after_archival(self) -> None:
        saved = session_shutdown()
        self.assertTrue(shutdown_store.create_session(saved))
        baseline = list(shutdown_store.session_records(saved["login"])[0]["trail"])
        transitions: list[shutdown_store.SessionProgress] = [
            shutdown_store.SessionReaping(kind="reaping", at=NOW,
                because="account out of usage", detail="weekly limit, resets tomorrow"),
            shutdown_store.SessionStopping(kind="stopping", at=LATER),
            shutdown_store.SessionStopped(kind="stopped", at="2026-10-09T21:51:10+00:00"),
            shutdown_store.SessionRestarted(kind="restarted", at="2026-10-09T21:52:10+00:00"),
        ]
        for progress in transitions:
            def change(current: shutdown_store.SessionShutdown) -> None:
                current["progress"] = progress
            with patch.object(shutdown_store, "datetime", ProgressWriteTime):
                ProgressWriteTime.at = progress.get("at", NOW)
                saved = shutdown_store.update_session(saved["login"], "session-1", change)
        appended = saved["trail"][len(baseline):]
        self.assertEqual([step["kind"] for step in appended], ["reaping", "stopping", "stopped", "restarted"])
        self.assertEqual([step["at"] for step in appended],
            [NOW, LATER, "2026-10-09T21:51:10+00:00", "2026-10-09T21:52:10+00:00"])
        self.assertIn("weekly limit, resets tomorrow", appended[0]["detail"])
        self.assertIn("account out of usage", appended[0]["detail"])
        def delivery_only(current: shutdown_store.SessionShutdown) -> None:
            current["settle_message"] = shutdown_store.SettleMessageSent(kind="sent", at=LATER)
        saved = shutdown_store.update_session(saved["login"], "session-1", delivery_only)
        self.assertEqual(saved["trail"][len(baseline):], appended)
        shutdown_store.archive_session(saved["login"], "session-1")
        history = list((self.directory / "history").glob("*.json"))
        self.assertEqual(len(history), 1)
        archived = shutdown_store.parse_session_records("[" + history[0].read_text() + "]")[0]
        self.assertEqual(archived["trail"], saved["trail"])
        self.assertEqual(archived["progress"], transitions[-1])

    def test_progress_restoration_uses_write_time_and_preserves_original_progress(self) -> None:
        saved = session_shutdown()
        stopped = shutdown_store.SessionStopped(kind="stopped", at=NOW)
        saved["progress"] = stopped
        self.assertTrue(shutdown_store.create_session(saved))
        restarting = shutdown_store.SessionRestarting(kind="restarting", at=LATER, prior=stopped)
        rollback_at = "2026-10-09T21:51:10+00:00"
        with patch.object(shutdown_store, "datetime", ProgressWriteTime):
            ProgressWriteTime.at = LATER
            _ = shutdown_store.update_session(saved["login"], "session-1",
                lambda current: current.update(progress=restarting))
            ProgressWriteTime.at = rollback_at
            restored = shutdown_store.update_session(saved["login"], "session-1",
                lambda current: current.update(progress=stopped))
        self.assertEqual(restored["progress"], stopped)
        self.assertEqual(restored["trail"], [
            shutdown_store.ProgressStep(at=NOW, kind="stopped", detail=""),
            shutdown_store.ProgressStep(at=LATER, kind="restarting", detail=""),
            shutdown_store.ProgressStep(at=rollback_at, kind="stopped", detail=""),
        ])
        self.assertEqual(shutdown_store.find_session(saved["login"], "session-1"), restored)

    def test_old_tmux_leftover_issue_is_normalized_on_read_and_written_canonically(self) -> None:
        saved = session_shutdown()
        saved["stop_issues"] = [shutdown_store.LegacyTmuxSessionLeftRunning(
            kind="legacy tmux session left running", at=NOW,
            session_name="old-unit", cause="owner identity lost")]
        wire = cast(dict[str, object], json.loads(json.dumps(saved)))
        wire["stop_issues"] = [{"kind": "unit tmux session left running", "at": NOW,
                               "tmux_session": "old-unit", "cause": "owner identity lost"}]
        parsed = shutdown_store.parse_session_records(json.dumps([wire]))[0]
        self.assertEqual(parsed, saved)
        self.assertTrue(shutdown_store.create_session(parsed))
        stored = cast(dict[str, object], json.loads((self.directory / "sessions/session-1.json").read_text()))
        self.assertEqual(stored["stop_issues"], saved["stop_issues"])
        self.assertEqual(shutdown_store.find_session(saved["login"], "session-1"), parsed)

    def test_unit_session_leftover_issue_rejects_an_unknown_host_or_missing_name(self) -> None:
        for fields in ({"host": "other", "session_name": "unit"}, {"host": "herdr"},
                       {"host": "herdr", "session_name": 4}, {"session_name": "unit"}):
            with self.subTest(fields=fields):
                wire = cast(dict[str, object], json.loads(json.dumps(session_shutdown())))
                wire["stop_issues"] = [{"kind": "unit session left running", "at": NOW,
                                       "cause": "stop not confirmed", **fields}]
                with self.assertRaises(shutdown_store.InvalidRecord):
                    _ = shutdown_store.parse_session_records(json.dumps([wire]))

    def test_herdr_hosts_round_trip_for_owners_and_their_workers(self) -> None:
        saved = session_shutdown()
        owner = session()
        owner["host"] = inventory.HerdrHost(kind="herdr", herdr_session="director", pane_id="w1:p1", presentation="detached")
        saved["session"] = owner
        worker = owned_worker()
        worker["host"] = inventory.HerdrHost(kind="herdr", herdr_session="director", pane_id="w1:p2", presentation="detached")
        saved["workers"] = [worker]
        self.assertTrue(shutdown_store.create_session(saved))
        self.assertEqual(shutdown_store.find_session(saved["login"], "session-1"), saved)
        self.assertEqual(shutdown_store.parse_session_records(json.dumps([saved])), [saved])

    def test_legacy_unit_wire_forms_normalize_in_records(self) -> None:
        for transport in (None, "tmux"):
            for key in ("session_name", "tmux_session"):
                with self.subTest(transport=transport, key=key):
                    host: dict[str, object] = {"kind": "unit", "production": "build", "unit": "one",
                        "doc": "/tmp/build.md", key: "unit-session"}
                    if transport is not None:
                        host["session_host"] = transport
                    saved = session_shutdown()
                    document = cast(dict[str, object], json.loads(json.dumps(saved)))
                    wire_session = cast(dict[str, object], document["session"])
                    wire_session.update(kind="unit", host=host, session_id=self.unit_row().current_session)
                    expected = {"kind": "legacy tmux unit", "unit": "one", "session_name": "unit-session"}
                    parsed = shutdown_store.parse_session_records(json.dumps([document]))[0]
                    self.assertEqual(parsed["session"]["host"], expected)
                    path = self.directory / "sessions" / f"{wire_session["session_id"]}.json"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    _ = path.write_text(json.dumps(document))
                    self.assertEqual(shutdown_store.find_session(saved["login"], str(wire_session["session_id"])), parsed)

    def test_old_tmux_hosts_normalize_in_records(self) -> None:
        saved = session_shutdown()
        document = cast(dict[str, object], json.loads(json.dumps(saved)))
        owner = cast(dict[str, object], document["session"])
        owner["host"] = {"kind": "tmux", "tmux_session": "old:name", "pane": {"kind": "not recorded"}}
        parsed = shutdown_store.parse_session_records(json.dumps([document]))[0]
        self.assertEqual(parsed["session"]["host"], {"kind": "legacy tmux", "session_name": "old:name"})

    def test_both_legacy_leftover_forms_require_time_and_normalize(self) -> None:
        expected = {"kind": "legacy tmux session left running", "at": NOW,
            "session_name": "old-unit", "cause": "stop not confirmed"}
        for old in ({"kind": "unit tmux session left running", "tmux_session": "old-unit"},
                    {"kind": "unit session left running", "host": "tmux", "session_name": "old-unit"}):
            document = cast(dict[str, object], json.loads(json.dumps(session_shutdown())))
            issue = {**old, "at": NOW, "cause": "stop not confirmed"}
            document["stop_issues"] = [issue]
            self.assertEqual(shutdown_store.parse_session_records(json.dumps([document]))[0]["stop_issues"], [expected])
            del issue["at"]
            with self.assertRaises(shutdown_store.InvalidRecord):
                _ = shutdown_store.parse_session_records(json.dumps([document]))

    def test_herdr_unit_transport_round_trips_through_the_record_store(self) -> None:
        saved = session_shutdown()
        saved["session"] = cast(inventory.UnitSession, cast(object, {
            **session(), "kind": "unit", "session_id": self.unit_row().current_session, "host": unit_host(kind="unit", production="build", unit="one",
                doc="/tmp/build.md", session_name="herdr-unit", session_host="herdr",
                plan=inventory.NoRunRecord(kind="no run record")),
        }))
        self.assertTrue(shutdown_store.create_session(saved))
        self.assertEqual(shutdown_store.find_session(saved["login"], saved["session"]["session_id"]), saved)
        self.assertEqual(shutdown_store.parse_session_records(json.dumps([saved])), [saved])

    def test_unit_host_rejects_invalid_transport_instead_of_defaulting_to_tmux(self) -> None:
        for transport in ("unknown", "", None, 4, ["invalid"]):
            with self.subTest(transport=transport):
                saved = session_shutdown()
                document = cast(dict[str, object], json.loads(json.dumps(saved)))
                wire_session = cast(dict[str, object], document["session"])
                wire_session["kind"] = "unit"
                wire_session["session_id"] = self.unit_row().current_session
                wire_session["host"] = {
                    "kind": "unit", "production": "build", "unit": "one", "doc": "/tmp/build.md",
                    "session_name": "unit", "session_host": transport, "plan": {"kind": "no run record"},
                }
                with self.assertRaises(shutdown_store.InvalidRecord):
                    _ = shutdown_store.parse_session_records(json.dumps([document]))

    def test_unit_host_prefers_the_new_name_when_both_keys_are_present(self) -> None:
        saved = session_shutdown()
        document = cast(dict[str, object], json.loads(json.dumps(saved)))
        wire_session = cast(dict[str, object], document["session"])
        wire_session["kind"] = "unit"
        wire_session["session_id"] = self.unit_row().current_session
        wire_session["host"] = {
            "kind": "unit", "production": "build", "unit": "one", "doc": "/tmp/build.md",
            "session_name": "new", "tmux_session": "old", "plan": {"kind": "no run record"},
        }
        parsed = shutdown_store.parse_session_records(json.dumps([document]))[0]["session"]["host"]
        self.assertEqual(parsed.get("session_name"), "new")
        self.assertNotIn("tmux_session", parsed)

    def test_herdr_host_rejects_missing_or_nontext_identity_fields(self) -> None:
        for host in ({"kind": "herdr", "pane_id": "w1:p1"},
                     {"kind": "herdr", "herdr_session": "owner"},
                     {"kind": "herdr", "herdr_session": "owner", "pane_id": 4}):
            with self.subTest(host=host):
                saved = session_shutdown()
                document = cast(dict[str, object], json.loads(json.dumps(saved)))
                wire_session = cast(dict[str, object], document["session"])
                wire_session["host"] = host
                with self.assertRaises(shutdown_store.InvalidRecord):
                    _ = shutdown_store.parse_session_records(json.dumps([document]))

    def test_session_records_second_create_leaves_the_existing_file_unchanged(self) -> None:
        first = session_shutdown()
        self.assertTrue(shutdown_store.create_session(first))
        path = self.directory / "sessions" / "session-1.json"
        before = path.read_bytes(), path.stat().st_mtime_ns
        replacement = session_shutdown(requested_at=LATER)
        replacement["then"] = "restart"
        self.assertFalse(shutdown_store.create_session(replacement))
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(shutdown_store.find_session(first["login"], "session-1"), first)

    def test_session_records_sort_by_request_time_then_name_across_accounts(self) -> None:
        later = session_shutdown(1, requested_at=LATER)
        bravo = session_shutdown(2)
        bravo["session"]["name"] = "bravo"
        alpha = session_shutdown(3, login="other@example.com")
        alpha["session"]["name"] = "alpha"
        for saved in (later, bravo, alpha):
            self.assertTrue(shutdown_store.create_session(saved))
        self.assertEqual(shutdown_store.session_records(later["login"]), [bravo, later])
        self.assertEqual(shutdown_store.all_session_records(), [alpha, bravo, later])

    def test_session_records_update_holds_the_account_lock_and_changes_only_its_session(self) -> None:
        first, second = session_shutdown(), session_shutdown(2)
        for saved in (first, second):
            self.assertTrue(shutdown_store.create_session(saved))

        def change(current: shutdown_store.SessionShutdown) -> None:
            with (self.directory / "lock").open("a+") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            current["progress"] = shutdown_store.SessionRestarting(kind="restarting", at=LATER, prior=shutdown_store.SessionStopped(kind="stopped", at=NOW))

        changed = shutdown_store.update_session(first["login"], "session-1", change)
        self.assertEqual(changed["progress"], {"kind": "restarting", "at": LATER, "prior": {"kind": "stopped", "at": NOW}})
        self.assertEqual(shutdown_store.find_session(first["login"], "session-1"), changed)
        self.assertEqual(shutdown_store.find_session(first["login"], "session-2"), second)

    def test_restore_alert_claim_serializes_callers_and_resets_only_its_request(self) -> None:
        saved = session_shutdown()
        self.assertTrue(shutdown_store.create_session(saved))
        start = threading.Barrier(2)
        claims: list[bool] = []
        def contender() -> None:
            _ = start.wait(timeout=HANG_GUARD_SECONDS)
            claims.append(shutdown_store.claim_restore_alert(saved, "move failed"))
        threads = [threading.Thread(target=contender) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=HANG_GUARD_SECONDS)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(claims), [False, True])
        shutdown_store.clear_restore_alert(saved)
        self.assertTrue(shutdown_store.claim_restore_alert(saved, "another outage"))
        def replace_request(current: shutdown_store.SessionShutdown) -> None:
            current["request_id"] = "newer-request"
        _ = shutdown_store.update_session(saved["login"], saved["session"]["session_id"], replace_request)
        shutdown_store.clear_restore_alert(saved)
        current = shutdown_store.find_session(saved["login"], saved["session"]["session_id"])
        assert current is not None
        self.assertEqual(current.get("restore_alerted"), "another outage")
        self.assertFalse(shutdown_store.claim_restore_alert(saved, "stale request"))

    def test_session_records_update_rejects_identity_changes_without_writing(self) -> None:
        saved = session_shutdown()
        self.assertTrue(shutdown_store.create_session(saved))
        for field, value in (("login", "other@example.com"), ("machine", "Mac"), ("session_id", "changed")):
            with self.subTest(field=field):
                def change(current: shutdown_store.SessionShutdown) -> None:
                    values = cast(dict[str, object], cast(object, current))
                    if field == "session_id":
                        values = cast(dict[str, object], cast(object, current["session"]))
                    values[field] = value

                with self.assertRaises(ValueError):
                    _ = shutdown_store.update_session(saved["login"], "session-1", change)
                self.assertEqual(shutdown_store.find_session(saved["login"], "session-1"), saved)

    def test_session_records_archive_moves_each_file_to_its_own_history_name(self) -> None:
        for number in (1, 2):
            saved = session_shutdown(number, progress=shutdown_store.SessionRestarted(kind="restarted", at=LATER))
            self.assertTrue(shutdown_store.create_session(saved))
            shutdown_store.archive_session(saved["login"], saved["session"]["session_id"])
            path = self.directory / "history" / f"{NOW}-session-{number}.json"
            self.assertEqual(shutdown_store.parse_session_records("[" + path.read_text() + "]"), [saved])
            self.assertIsNone(shutdown_store.find_session(saved["login"], saved["session"]["session_id"]))
        self.assertEqual(shutdown_store.session_records("owner@example.com"), [])

    def test_session_records_missing_state_is_empty_and_mutations_require_a_record(self) -> None:
        self.assertEqual(shutdown_store.session_records("owner@example.com"), [])
        self.assertEqual(shutdown_store.all_session_records(), [])
        self.assertIsNone(shutdown_store.find_session("owner@example.com", "missing"))
        self.assertIsNone(shutdown_store.account_hold("owner@example.com"))
        shutdown_store.clear_account_hold("owner@example.com")
        with self.assertRaises(shutdown_store.NoLiveRecord):
            _ = shutdown_store.update_session("owner@example.com", "missing", lambda current: None)
        with self.assertRaises(shutdown_store.NoLiveRecord):
            shutdown_store.archive_session("owner@example.com", "missing")
        self.assertFalse(self.directory.exists())

    def test_forget_archives_abandoned_restores_under_the_account_lock_and_keeps_the_hold(self) -> None:
        held = account_hold()
        shutdown_store.set_account_hold(held)
        sibling = session_shutdown(9)
        self.assertTrue(shutdown_store.create_session(sibling))
        progress: tuple[shutdown_store.SessionProgress, ...] = (
            shutdown_store.SessionRestartFailed(kind="restart failed", at=NOW, reason="agent exited"),
            shutdown_store.SessionNeedsManualRestart(kind="manual restart", command="resume"),
            shutdown_store.SessionStopped(kind="stopped", at=NOW),
            shutdown_store.SessionAlreadyGone(kind="already gone", at=NOW),
            shutdown_store.ProcessIdentityLost(kind="process identity lost", at=NOW),
        )
        replace = os.replace

        def archive(source: Path, target: Path) -> None:
            with (self.directory / "lock").open("a+") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            replace(source, target)

        for number, state in enumerate(progress, 1):
            with self.subTest(kind=state["kind"]):
                saved = session_shutdown(number, progress=state)
                self.assertTrue(shutdown_store.create_session(saved))
                with patch.object(os, "replace", side_effect=archive):
                    result = shutdown_store.forget_session(saved["login"], saved["session"]["session_id"], "natedev")
                self.assertIsInstance(result, shutdown_store.SessionForgotten)
                self.assertIsNone(shutdown_store.find_session(saved["login"], saved["session"]["session_id"]))
                archived = self.directory / "history" / f"{NOW}-session-{number}.json"
                self.assertEqual(json.loads(archived.read_text()), saved)
        self.assertEqual(shutdown_store.session_records(held["login"]), [sibling])
        self.assertEqual(shutdown_store.account_hold(held["login"]), held)

    def test_forget_rechecks_progress_and_retains_in_progress_or_foreign_records(self) -> None:
        states: tuple[shutdown_store.SessionProgress, ...] = (
            shutdown_store.SessionWaiting(kind="waiting"),
            shutdown_store.SessionStopping(kind="stopping", at=NOW),
            shutdown_store.SessionRestarting(kind="restarting", at=NOW, prior=shutdown_store.SessionStopped(kind="stopped", at=NOW)),
            shutdown_store.SessionRestoredTimersPending(kind="timers pending", at=NOW, timers=[]),
        )
        for number, progress in enumerate(states, 1):
            saved = session_shutdown(number, progress=progress)
            self.assertTrue(shutdown_store.create_session(saved))
            with self.subTest(kind=progress["kind"]):
                self.assertIsInstance(shutdown_store.forget_session(saved["login"], saved["session"]["session_id"], "natedev"), shutdown_store.SessionRetained)
                self.assertEqual(shutdown_store.find_session(saved["login"], saved["session"]["session_id"]), saved)
        foreign = session_shutdown(9, progress=shutdown_store.SessionStopped(kind="stopped", at=NOW))
        foreign["machine"] = "Mac"
        self.assertTrue(shutdown_store.create_session(foreign))
        self.assertIsInstance(shutdown_store.forget_session(foreign["login"], "session-9", "natedev"), shutdown_store.SessionRetained)
        self.assertEqual(shutdown_store.find_session(foreign["login"], "session-9"), foreign)
        self.assertFalse((self.directory / "history").exists())

    def test_failed_restart_archive_checks_request_progress_and_machine_under_the_lock(self) -> None:
        failed = shutdown_store.SessionRestartFailed(kind="restart failed", at=NOW, reason="prompt unconfirmed")
        cases: tuple[tuple[str, shutdown_store.SessionProgress, str, bool], ...] = (
            ("expected-request", failed, "natedev", True),
            ("another-request", failed, "natedev", False),
            ("expected-request", shutdown_store.SessionNeedsManualRestart(kind="manual restart", command="resume"), "natedev", False),
            ("expected-request", failed, "Mac", False),
        )
        original_replace = os.replace

        def archive_locked(source: Path, target: Path) -> None:
            with (self.directory / "lock").open("a+") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            original_replace(source, target)

        for number, (request_id, progress, machine, closes) in enumerate(cases, 1):
            with self.subTest(request=request_id, progress=progress, machine=machine):
                saved = session_shutdown(number, progress=progress)
                saved["request_id"], saved["machine"] = request_id, machine
                self.assertTrue(shutdown_store.create_session(saved))
                with patch.object(os, "replace", side_effect=archive_locked):
                    outcome = shutdown_store.forget_failed_restart(saved["login"], saved["session"]["session_id"],
                        "natedev", "expected-request")
                archived = self.directory / "history" / f"{NOW}-session-{number}.json"
                if closes:
                    self.assertIsInstance(outcome, shutdown_store.SessionForgotten)
                    self.assertEqual(json.loads(archived.read_text()), saved)
                    self.assertIsNone(shutdown_store.find_session(saved["login"], saved["session"]["session_id"]))
                else:
                    self.assertEqual(outcome, shutdown_store.SessionRetained(saved))
                    self.assertEqual(shutdown_store.find_session(saved["login"], saved["session"]["session_id"]), saved)
                    self.assertFalse(archived.exists())

    def test_forget_legacy_restore_does_not_reappear_and_preserves_siblings_and_hold(self) -> None:
        old = shutdown_record(state="restart partial",
            progress=shutdown_store.SessionRestartFailed(kind="restart failed", at=NOW, reason="not live"))
        old["entries"].append(entry(number=2))
        write_old(old)
        held = shutdown_store.account_hold(old["login"])
        outcome = shutdown_store.forget_session(old["login"], "session-1", "natedev")
        self.assertIsInstance(outcome, shutdown_store.SessionForgotten)
        self.assertEqual([saved["session"]["session_id"] for saved in shutdown_store.session_records(old["login"])], ["session-2"])
        self.assertEqual(shutdown_store.account_hold(old["login"]), held)
        self.assertIsInstance(shutdown_store.forget_session(old["login"], "session-1", "natedev"), shutdown_store.NoShutdown)

    def test_session_records_hold_file_persists_without_any_session(self) -> None:
        hold = account_hold("Owner@Example.com")
        shutdown_store.set_account_hold(hold)
        self.assertEqual(shutdown_store.account_hold("OWNER@EXAMPLE.COM"), hold)
        self.assertEqual(shutdown_store.session_records(hold["login"]), [])
        self.assertEqual(cast(object, json.loads((self.directory / "hold.json").read_text())), hold)
        hold["then"] = "restart"
        shutdown_store.set_account_hold(hold)
        self.assertEqual(shutdown_store.account_hold(hold["login"]), hold)
        shutdown_store.clear_account_hold(hold["login"])
        shutdown_store.clear_account_hold(hold["login"])
        self.assertIsNone(shutdown_store.account_hold(hold["login"]))

    def test_finishing_registration_cannot_release_a_replacement_requests_hold(self) -> None:
        held = account_hold()
        held["registration"] = "in progress"
        held["request_id"] = "replacement"
        shutdown_store.set_account_hold(held)
        shutdown_store.complete_account_hold_registration(held["login"], "original")
        shutdown_store.clear_finished_account_hold(held["login"])
        self.assertEqual(shutdown_store.account_hold(held["login"]), held)
        shutdown_store.complete_account_hold_registration(held["login"], "replacement")
        shutdown_store.clear_finished_account_hold(held["login"])
        self.assertIsNone(shutdown_store.account_hold(held["login"]))

    def test_hold_waits_for_every_registration_and_every_covered_requests_records(self) -> None:
        first = account_hold()
        second = account_hold()
        second["request_id"] = "second"
        shutdown_store.register_account_hold_request(first, account_wide=True)
        shutdown_store.register_account_hold_request(second, account_wide=True)
        shutdown_store.complete_account_hold_registration(first["login"], first["request_id"])
        shutdown_store.clear_finished_account_hold(first["login"])
        self.assertIsInstance(shutdown_store.lookup_account_hold(first["login"]), shutdown_store.AccountHeld)

        saved = session_shutdown()
        saved["request_id"] = second["request_id"]
        self.assertTrue(shutdown_store.create_session(saved))
        shutdown_store.complete_account_hold_registration(second["login"], second["request_id"])
        shutdown_store.clear_finished_account_hold(first["login"])
        self.assertIsInstance(shutdown_store.lookup_account_hold(first["login"]), shutdown_store.AccountHeld)
        shutdown_store.archive_session(saved["login"], saved["session"]["session_id"])
        shutdown_store.clear_finished_account_hold(first["login"], request_ids=(second["request_id"],))
        self.assertIsInstance(shutdown_store.lookup_account_hold(first["login"]), shutdown_store.AccountHeld)
        shutdown_store.clear_finished_account_hold(first["login"], request_ids=(first["request_id"],))
        self.assertIsInstance(shutdown_store.lookup_account_hold(first["login"]), shutdown_store.AccountOpen)

    def test_account_hold_registration_includes_requests_that_already_have_records(self) -> None:
        saved = session_shutdown()
        saved["request_id"] = "already-waiting"
        self.assertTrue(shutdown_store.create_session(saved))
        held = account_hold()
        shutdown_store.register_account_hold_request(held, account_wide=True)
        shutdown_store.complete_account_hold_registration(held["login"], held["request_id"])
        shutdown_store.clear_finished_account_hold(held["login"])
        self.assertIsInstance(shutdown_store.lookup_account_hold(held["login"]), shutdown_store.AccountHeld)
        shutdown_store.archive_session(saved["login"], saved["session"]["session_id"])
        shutdown_store.clear_finished_account_hold(held["login"])
        self.assertIsInstance(shutdown_store.lookup_account_hold(held["login"]), shutdown_store.AccountOpen)

    def registered_stop_hold(self) -> shutdown_store.AccountHold:
        held = account_hold()
        held["requested_by"] = shutdown_store.FromSession(kind="session", session_id="b1f5ca98-45aa-4604-8e2b-33ed0f071bb1")
        held["request_id"] = "e520bca5-6b79-4c16-874e-512142adfed2"
        shutdown_store.register_account_hold_request(held, account_wide=True)
        shutdown_store.complete_account_hold_registration(held["login"], held["request_id"])
        stored = shutdown_store.account_hold(held["login"])
        assert stored is not None
        return stored

    def hold_record(self, held: shutdown_store.AccountHold, number: int, progress: shutdown_store.SessionProgress) -> shutdown_store.SessionShutdown:
        saved = session_shutdown(number, progress=progress, hold="account")
        saved["request_id"] = held["request_id"]
        self.assertTrue(shutdown_store.create_session(saved))
        return saved

    def test_a_finished_stop_hold_whose_records_another_path_archived_is_released(self) -> None:
        """The 10-18 claude2 case: a stop request's sessions ended restarted or restart failed, a later
        path archived their records, and no conductor saw them finish, so its hold outlived it."""
        held = self.registered_stop_hold()
        for number, progress in ((1, shutdown_store.SessionRestarted(kind="restarted", at=LATER)),
                                 (2, shutdown_store.SessionRestartFailed(kind="restart failed", at=LATER, reason="restart prompt not submitted")),
                                 (3, shutdown_store.SessionStopped(kind="stopped", at=LATER))):
            saved = self.hold_record(held, number, progress)
            shutdown_store.archive_session(saved["login"], saved["session"]["session_id"])
        self.assertEqual(shutdown_store.release_finished_account_hold(held["login"]), shutdown_store.HoldReleased(held))
        self.assertFalse((self.directory / "hold.json").exists())
        self.assertIsInstance(shutdown_store.lookup_account_hold(held["login"]), shutdown_store.AccountOpen)
        self.assertIsInstance(shutdown_store.release_finished_account_hold(held["login"]), shutdown_store.AccountOpen)

    def test_a_finished_hold_is_released_while_its_stopped_records_stay_for_restore(self) -> None:
        held = self.registered_stop_hold()
        saved = self.hold_record(held, 1, shutdown_store.SessionStopped(kind="stopped", at=LATER))
        self.assertEqual(shutdown_store.release_finished_account_hold(held["login"]), shutdown_store.HoldReleased(held))
        self.assertEqual(shutdown_store.session_records(held["login"]), [saved])

    def test_a_hold_stays_while_a_session_of_its_request_is_live_or_stopping_or_its_conductor_runs(self) -> None:
        held = self.registered_stop_hold()
        saved = self.hold_record(held, 1, shutdown_store.SessionWaiting(kind="waiting"))
        self.assertEqual(shutdown_store.release_finished_account_hold(held["login"]), shutdown_store.HoldInUse(held))
        stopping: shutdown_store.SessionProgress = shutdown_store.SessionStopping(kind="stopping", at=LATER)
        _ = shutdown_store.update_session(held["login"], saved["session"]["session_id"], lambda current: current.update(progress=stopping))
        self.assertEqual(shutdown_store.release_finished_account_hold(held["login"]), shutdown_store.HoldInUse(held))
        stopped: shutdown_store.SessionProgress = shutdown_store.SessionStopped(kind="stopped", at=LATER)
        _ = shutdown_store.update_session(held["login"], saved["session"]["session_id"], lambda current: current.update(progress=stopped))
        with shutdown_store.conductor_lock(held["login"]) as conductor:
            self.assertIsInstance(conductor, shutdown_store.ConductorAcquired)
            self.assertEqual(shutdown_store.release_finished_account_hold(held["login"]), shutdown_store.HoldInUse(held))
        self.assertEqual(shutdown_store.release_finished_account_hold(held["login"]), shutdown_store.HoldReleased(held))

    def test_a_conductor_keeps_a_restart_hold_until_it_restarts_its_stopped_sessions(self) -> None:
        held = self.registered_stop_hold()
        held["then"] = "restart"
        shutdown_store.set_account_hold(held)
        saved = self.hold_record(held, 1, shutdown_store.SessionStopped(kind="stopped", at=LATER))
        _ = shutdown_store.update_session(held["login"], saved["session"]["session_id"], lambda current: current.update(then="restart"))
        self.assertEqual(shutdown_store.release_finished_account_hold(held["login"], conducting=True), shutdown_store.HoldInUse(held))
        restarted: shutdown_store.SessionProgress = shutdown_store.SessionRestarted(kind="restarted", at=LATER)
        _ = shutdown_store.update_session(held["login"], saved["session"]["session_id"], lambda current: current.update(progress=restarted))
        self.assertEqual(shutdown_store.release_finished_account_hold(held["login"], conducting=True), shutdown_store.HoldReleased(held))

    def test_a_registering_or_old_record_hold_is_never_released(self) -> None:
        _ = self.enterContext(patch.object(shutdown_store, "registration_process_start", return_value="fixture-process-start"))
        held = account_hold()
        held["registration"] = "in progress"
        held["registration_owners"] = {held["request_id"]:
            shutdown_store.RegistrationOwner(pid=4242, proc_start="fixture-process-start")}
        shutdown_store.set_account_hold(held)
        self.assertEqual(shutdown_store.release_finished_account_hold(held["login"]), shutdown_store.HoldInUse(held))
        shutdown_store.clear_account_hold(held["login"])
        write_old(shutdown_record(state="down"))
        outcome = shutdown_store.release_finished_account_hold(held["login"])
        self.assertIsInstance(outcome, shutdown_store.HoldInUse)
        self.assertIsInstance(shutdown_store.lookup_account_hold(held["login"]), shutdown_store.AccountHeld)

    def test_all_account_holds_include_explicit_and_empty_old_accounts_in_request_order(self) -> None:
        explicit = account_hold()
        explicit["label"] = "Alpha"
        shutdown_store.set_account_hold(explicit)
        later = account_hold("later@example.com")
        later["requested_at"] = LATER
        later["label"] = "Earlier label"
        shutdown_store.set_account_hold(later)
        old = shutdown_record("legacy@example.com", state="down")
        old["label"] = "Zulu"
        old["entries"] = []
        write_old(old)
        legacy = account_hold(old["login"])
        legacy["label"] = old["label"]
        self.assertTrue(shutdown_store.create_session(session_shutdown(login="selected@example.com")))

        self.assertEqual(shutdown_store.all_account_holds(), [explicit, legacy, later])
        self.assertEqual(shutdown_store.session_records(old["login"]), [])

    def test_all_account_holds_missing_root_is_empty(self) -> None:
        self.assertEqual(shutdown_store.all_account_holds(), [])
        self.assertFalse(self.directory.parent.exists())

    def test_all_account_holds_reject_non_directory_roots_and_accounts(self) -> None:
        state = self.directory.parent
        _ = state.write_text("not a directory")
        with self.assertRaises(shutdown_store.InvalidRecord):
            _ = shutdown_store.all_account_holds()
        state.unlink()
        state.mkdir()
        _ = self.directory.write_text("not a directory")
        with self.assertRaises(shutdown_store.InvalidRecord):
            _ = shutdown_store.all_account_holds()
        self.directory.unlink()
        (state / "launch.lock").mkdir()
        with self.assertRaises(shutdown_store.InvalidRecord):
            _ = shutdown_store.all_account_holds()

    def test_all_account_holds_refuse_directory_and_lock_read_failures(self) -> None:
        shutdown_store.set_account_hold(account_hold())
        with patch.object(Path, "iterdir", side_effect=PermissionError("access refused")):
            with self.assertRaisesRegex(shutdown_store.InvalidRecord, "access refused"):
                _ = shutdown_store.all_account_holds()
        with patch.object(fcntl, "flock", side_effect=OSError("lock unavailable")):
            with self.assertRaisesRegex(shutdown_store.InvalidRecord, "lock unavailable"):
                _ = shutdown_store.all_account_holds()

    def test_all_account_holds_refuse_invalid_hold_and_old_record_files(self) -> None:
        self.directory.mkdir(parents=True)
        for filename in ("hold.json", "record.json"):
            with self.subTest(filename=filename):
                path = self.directory / filename
                _ = path.write_text("{}")
                with self.assertRaises(shutdown_store.InvalidRecord) as raised:
                    _ = shutdown_store.all_account_holds()
                self.assertIn(str(path), str(raised.exception))
                path.unlink()

    def test_parse_account_holds_accepts_a_valid_list(self) -> None:
        holds = [account_hold(), account_hold("other@example.com")]
        self.assertEqual(shutdown_store.parse_account_holds(json.dumps(holds)), holds)
        self.assertEqual(shutdown_store.parse_account_holds("[]"), [])

    def test_parse_account_holds_rejects_invalid_text_and_hold_fields(self) -> None:
        for text in ("not JSON", "{}", "null", "[1]", "[{}]"):
            with self.subTest(text=text):
                with self.assertRaises(shutdown_store.InvalidRecord):
                    _ = shutdown_store.parse_account_holds(text)
        for field, value in (
            ("login", "../other"), ("requested_at", "not a time"),
            ("requested_by", {"kind": "unknown"}), ("then", "continue"),
            ("registration", "unknown"), ("registration", {}),
            ("request_ids", "not a list"), ("request_ids", []),
            ("request_ids", ["unknown"]), ("registering_request_ids", ["unknown"]),
            ("registering_request_ids", [1]),
        ):
            with self.subTest(field=field):
                hold = cast(dict[str, object], cast(object, account_hold()))
                hold[field] = value
                with self.assertRaises(shutdown_store.InvalidRecord):
                    _ = shutdown_store.parse_account_holds(json.dumps([hold]))


    def test_session_records_unsafe_ids_stay_inside_the_sessions_directory(self) -> None:
        for session_id, filename in (("../outside/\0:*", "..-outside----.json"), ("safe.ID_2-3", "safe.ID_2-3.json")):
            with self.subTest(session_id=session_id):
                saved = session_shutdown()
                saved["session"]["session_id"] = session_id
                self.assertTrue(shutdown_store.create_session(saved))
                self.assertTrue((self.directory / "sessions" / filename).is_file())
                self.assertEqual(shutdown_store.find_session(saved["login"], session_id), saved)
        self.assertFalse((self.root / "outside").exists())

    def test_session_records_colliding_safe_names_never_replace_another_session(self) -> None:
        first = session_shutdown()
        first["session"]["session_id"] = "id/one"
        self.assertTrue(shutdown_store.create_session(first))
        collision = session_shutdown(2)
        collision["session"]["session_id"] = "id-one"
        with self.assertRaises(shutdown_store.InvalidRecord):
            _ = shutdown_store.create_session(collision)
        with self.assertRaises(shutdown_store.InvalidRecord):
            _ = shutdown_store.update_session(first["login"], "id-one", lambda current: None)
        with self.assertRaises(shutdown_store.InvalidRecord):
            shutdown_store.archive_session(first["login"], "id-one")
        self.assertEqual(shutdown_store.find_session(first["login"], "id/one"), first)

    def test_session_records_reject_non_directory_accounts_and_sessions_paths(self) -> None:
        self.directory.parent.mkdir(parents=True)
        _ = self.directory.write_text("not a directory")
        with self.assertRaises(shutdown_store.InvalidRecord):
            _ = shutdown_store.session_records("owner@example.com")
        with self.assertRaises(shutdown_store.InvalidRecord):
            _ = shutdown_store.account_hold("owner@example.com")
        with self.assertRaises(shutdown_store.InvalidRecord):
            _ = shutdown_store.all_session_records()
        self.directory.unlink()
        self.directory.mkdir()
        _ = (self.directory / "sessions").write_text("not a directory")
        with self.assertRaises(shutdown_store.InvalidRecord):
            _ = shutdown_store.session_records("owner@example.com")

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
                    with self.assertRaises(shutdown_store.InvalidRecord):
                        if filename == "hold.json":
                            _ = shutdown_store.account_hold("owner@example.com")
                        else:
                            _ = shutdown_store.session_records("owner@example.com")
                    if kind == "directory":
                        path.rmdir()
                    else:
                        path.unlink()

    def test_session_records_lock_and_directory_read_failures_are_invalid_records(self) -> None:
        self.assertTrue(shutdown_store.create_session(session_shutdown()))
        with patch.object(fcntl, "flock", side_effect=OSError("lock unavailable")):
            with self.assertRaisesRegex(shutdown_store.InvalidRecord, "lock unavailable"):
                _ = shutdown_store.session_records("owner@example.com")
        with patch.object(Path, "iterdir", side_effect=PermissionError("access refused")):
            with self.assertRaisesRegex(shutdown_store.InvalidRecord, "access refused"):
                _ = shutdown_store.session_records("owner@example.com")

    def test_session_records_read_errors_name_the_file(self) -> None:
        self.assertTrue(shutdown_store.create_session(session_shutdown()))
        path = self.directory / "sessions" / "session-1.json"
        _ = path.write_text("invalid JSON")
        with self.assertRaises(shutdown_store.InvalidRecord) as raised:
            _ = shutdown_store.find_session("owner@example.com", "session-1")
        self.assertIn(str(path), str(raised.exception))
        _ = path.write_text(json.dumps(session_shutdown(login="other@example.com")))
        with self.assertRaises(shutdown_store.InvalidRecord):
            _ = shutdown_store.session_records("owner@example.com")
        _ = (self.directory / "hold.json").write_text(json.dumps(account_hold("other@example.com")))
        with self.assertRaises(shutdown_store.InvalidRecord):
            _ = shutdown_store.account_hold("owner@example.com")

    def test_session_records_atomic_update_failure_keeps_the_previous_file(self) -> None:
        saved = session_shutdown()
        self.assertTrue(shutdown_store.create_session(saved))
        def change(current: shutdown_store.SessionShutdown) -> None:
            current["progress"] = shutdown_store.SessionStopped(kind="stopped", at=LATER)
        with patch.object(os, "replace", side_effect=OSError("replace refused")):
            with self.assertRaisesRegex(OSError, "replace refused"):
                _ = shutdown_store.update_session(saved["login"], "session-1", change)
        self.assertEqual(shutdown_store.find_session(saved["login"], "session-1"), saved)
        self.assertEqual([path.name for path in (self.directory / "sessions").iterdir()], ["session-1.json"])

    def test_session_records_unreadable_partial_directory_refuses_reads(self) -> None:
        self.directory.mkdir(parents=True)
        (self.directory / "partial").mkdir()
        with patch.object(Path, "iterdir", side_effect=PermissionError("partial unavailable")):
            for read in (shutdown_store.session_records, shutdown_store.account_hold):
                with self.assertRaisesRegex(shutdown_store.InvalidRecord, "partial unavailable"):
                    _ = read("owner@example.com")

    def test_session_records_hold_write_never_replaces_unreadable_or_foreign_state(self) -> None:
        self.directory.mkdir(parents=True)
        path = self.directory / "hold.json"
        for text in ("invalid JSON", json.dumps(account_hold("other@example.com"))):
            with self.subTest(text=text):
                _ = path.write_text(text)
                with self.assertRaises(shutdown_store.InvalidRecord):
                    shutdown_store.set_account_hold(account_hold())
                self.assertEqual(path.read_text(), text)

    def test_remote_records_convert_old_worker_fields_and_nested_kinds(self) -> None:
        saved = session_shutdown(progress=shutdown_store.SessionStopped(kind="stopped", at=LATER))
        saved["workers"] = [owned_worker()]
        saved["stop_issues"] = [shutdown_store.NotReadyToStop(kind="not ready to stop", at=NOW,
                                                    status="idle", progress="passive worker ready")]
        previous = (json.dumps([saved]).replace('"workers"', '"seats"')
                    .replace('"kind": "worker"', '"kind": "seat"')
                    .replace('passive worker ready', 'passive seat ready'))
        self.assertEqual(shutdown_store.parse_session_records(previous), [saved])

    def test_session_records_validate_workers_and_claim_timestamps(self) -> None:
        saved = session_shutdown(progress=shutdown_store.SessionRestarting(kind="restarting", at=LATER, prior=shutdown_store.SessionStopped(kind="stopped", at=NOW)))
        saved["workers"] = [owned_worker()]
        saved["alerted_at"] = NOW
        self.assertEqual(shutdown_store.parse_session_records(json.dumps([saved])), [saved])
        for field, value in (
            ("session", owned_worker()), ("workers", [session()]),
            ("workers", [owned_worker("another")]), ("progress", {"kind": "restarting"}),
            ("alerted_at", "2026-10-09T21:49:10Z"), ("hold", "all"), ("alerts", "phone"),
        ):
            with self.subTest(field=field):
                malformed = cast(dict[str, object], cast(object, session_shutdown()))
                malformed[field] = value
                with self.assertRaises(shutdown_store.InvalidRecord):
                    _ = shutdown_store.parse_session_records(json.dumps([malformed]))

    def test_session_records_convert_old_states_and_attach_workers_without_writing(self) -> None:
        for state in ("settling", "down", "restarting"):
            with self.subTest(state=state):
                login = f"{state}@example.com"
                old = shutdown_record(login, state=state)
                old["then"] = "restart"
                old["force"] = "now"
                old["entries"][0]["progress"] = shutdown_store.SessionStopped(kind="stopped", at=LATER) if state == "down" else shutdown_store.SessionWaiting(kind="waiting")
                worker = entry({"kind": "passive worker ready", "at": NOW}, number=9)
                worker["session"] = owned_worker()
                old["entries"].append(worker)
                write_old(old)
                directory = self.directory.parent / login
                before = (directory / "record.json").read_bytes()
                converted = shutdown_store.session_records(login)
                self.assertEqual(len(converted), 1)
                self.assertEqual(converted[0]["session"], old["entries"][0]["session"])
                self.assertEqual(converted[0]["workers"], [worker["session"]])
                self.assertEqual(converted[0]["timers"], old["entries"][0]["timers"])
                self.assertEqual(converted[0]["hold"], "account")
                self.assertEqual(converted[0]["then"], "restart")
                self.assertEqual(converted[0]["alerts"], "user")
                self.assertFalse(converted[0]["probe"])
                self.assertEqual(converted[0]["force"], "now")
                expected = "stopped" if state in {"down", "restarting"} else "waiting"
                self.assertEqual(converted[0]["progress"]["kind"], expected)
                self.assertEqual((directory / "record.json").read_bytes(), before)
                self.assertFalse((directory / "sessions").exists())

    def test_session_records_old_restarting_keeps_completed_entries(self) -> None:
        old = shutdown_record(state="restarting")
        old["entries"] = [
            entry(shutdown_store.SessionRestarted(kind="restarted", at=LATER)),
            entry(shutdown_store.SessionNeedsManualRestart(kind="manual restart", command="resume"), number=2),
        ]
        write_old(old)
        converted = shutdown_store.session_records(old["login"])
        self.assertEqual(converted[0]["progress"], old["entries"][0]["progress"])
        self.assertEqual(converted[1]["progress"], {"kind": "stopped", "at": NOW})

    def test_session_records_read_partial_and_selected_legacy_requests_together(self) -> None:
        old = shutdown_record(state="restart partial", progress=shutdown_store.SessionRestartFailed(kind="restart failed", at=NOW, reason="not live"))
        write_old(old)
        _ = move_old(old["login"])
        selected = shutdown_record(requested_at=LATER)
        selected["scope"] = {"kind": "selected", "session_ids": ["session-2"]}
        selected["entries"] = [entry(number=2)]
        write_old(selected)
        records = shutdown_store.session_records(old["login"])
        self.assertEqual([item["session"]["session_id"] for item in records], ["session-1", "session-2"])
        self.assertEqual([item["hold"] for item in records], ["account", "session"])
        self.assertEqual(shutdown_store.all_session_records(), records)
        self.assertEqual(shutdown_store.account_hold(old["login"]), account_hold())

    def test_session_records_slot_wins_over_partial_entry_with_the_same_request_time(self) -> None:
        partial = shutdown_record(
            state="restart partial",
            progress=shutdown_store.SessionNeedsManualRestart(kind="manual restart", command="resume old"),
        )
        partial_worker = entry(number=8)
        partial_worker["session"] = owned_worker()
        partial["entries"].append(partial_worker)
        write_old(partial)
        _ = move_old(partial["login"])
        slot = shutdown_record(progress=shutdown_store.SessionWaiting(kind="waiting"))
        slot_worker = entry(number=9)
        worker = owned_worker()
        registered = staff.snapshot().by_current_session(worker["session_id"])
        assert registered is not None and registered.parent is not None
        worker["session_id"] = fixture_worker("slot-worker", registered.parent).current_session
        slot_worker["session"] = worker
        slot["entries"].append(slot_worker)
        write_old(slot)

        found = shutdown_store.find_session(slot["login"], "session-1")
        self.assertIsNotNone(found)
        if found is None:
            self.fail("expected the slot's session record")
        self.assertEqual(found["progress"], slot["entries"][0]["progress"])
        self.assertEqual(found["workers"], [worker])
        self.assertEqual(shutdown_store.session_records(slot["login"]), [found])
        self.assertEqual(shutdown_store.all_session_records(), [found])

    def test_session_records_newest_partial_entry_wins_for_one_session(self) -> None:
        older = shutdown_record(
            state="restart partial",
            progress=shutdown_store.SessionNeedsManualRestart(kind="manual restart", command="resume old"),
        )
        write_old(older)
        _ = move_old(older["login"])
        newer = shutdown_record(
            state="restart partial", requested_at=LATER,
            progress=shutdown_store.SessionRestartFailed(kind="restart failed", at=LATER, reason="new failure"),
        )
        worker = entry(number=9)
        worker["session"] = owned_worker()
        newer["entries"].append(worker)
        write_old(newer)
        _ = move_old(newer["login"])

        found = shutdown_store.find_session(newer["login"], "session-1")
        self.assertIsNotNone(found)
        if found is None:
            self.fail("expected the newest partial session record")
        self.assertEqual(found["requested_at"], LATER)
        self.assertEqual(found["progress"], newer["entries"][0]["progress"])
        self.assertEqual(found["workers"], [worker["session"]])
        self.assertEqual(shutdown_store.session_records(newer["login"]), [found])
        self.assertEqual(shutdown_store.all_session_records(), [found])

    def test_session_records_new_files_suppress_matching_old_entries_only(self) -> None:
        old = shutdown_record()
        old["entries"].append(entry(number=2))
        write_old(old)
        saved = session_shutdown(requested_at=LATER, progress=shutdown_store.SessionStopped(kind="stopped", at=LATER))
        sessions = self.directory / "sessions"
        sessions.mkdir()
        _ = (sessions / "session-1.json").write_text(json.dumps(saved))
        converted = shutdown_store.session_records(old["login"])
        self.assertEqual([item["session"]["session_id"] for item in converted], ["session-2", "session-1"])
        self.assertEqual(shutdown_store.find_session(old["login"], "session-1"), saved)
        self.assertFalse(shutdown_store.create_session(session_shutdown(2)))
        self.assertFalse((sessions / "session-2.json").exists())

    def test_session_records_invalid_old_slot_and_partial_records_name_the_file(self) -> None:
        self.directory.mkdir(parents=True)
        partial = self.directory / "partial"
        partial.mkdir()
        for path in (self.directory / "record.json", partial / "old.json"):
            with self.subTest(path=path):
                _ = path.write_text("{}")
                for read in (shutdown_store.session_records, shutdown_store.account_hold):
                    with self.assertRaises(shutdown_store.InvalidRecord) as raised:
                        _ = read("owner@example.com")
                    self.assertIn(str(path), str(raised.exception))
                path.unlink()

    def test_finished_old_cancelled_record_has_no_session_records_or_hold(self) -> None:
        saved = shutdown_record(state="cancelled")
        write_old(saved)
        path = self.directory / "record.json"
        before = path.read_bytes()

        self.assertEqual(shutdown_store.session_records(saved["login"]), [])
        self.assertEqual(shutdown_store.all_session_records(), [])
        self.assertIsNone(shutdown_store.find_session(saved["login"], "session-1"))
        self.assertIsNone(shutdown_store.account_hold(saved["login"]))
        self.assertEqual(path.read_bytes(), before)

    def test_finished_old_up_record_has_no_session_records_or_hold(self) -> None:
        saved = shutdown_record(state="up")
        write_old(saved)
        path = self.directory / "record.json"
        before = path.read_bytes()

        self.assertEqual(shutdown_store.session_records(saved["login"]), [])
        self.assertEqual(shutdown_store.all_session_records(), [])
        self.assertIsNone(shutdown_store.find_session(saved["login"], "session-1"))
        self.assertIsNone(shutdown_store.account_hold(saved["login"]))
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
                for read in (shutdown_store.session_records, shutdown_store.account_hold):
                    with self.assertRaises(shutdown_store.InvalidRecord) as raised:
                        _ = read("owner@example.com")
                    self.assertIn(str(path), str(raised.exception))

    def test_session_records_legacy_account_hold_excludes_selected_and_finished_records(self) -> None:
        old = shutdown_record()
        old["entries"] = []
        write_old(old)
        self.assertEqual(shutdown_store.account_hold(old["login"]), account_hold())
        self.assertEqual(shutdown_store.session_records(old["login"]), [])
        for state in ("cancelled", "up"):
            def change(current: LegacyRecordFixture) -> None:
                current["state"] = state
            _ = change_old(old["login"], change)
            self.assertIsNone(shutdown_store.account_hold(old["login"]))
        def select(current: LegacyRecordFixture) -> None:
            current["state"] = "settling"
            current["scope"] = {"kind": "selected", "session_ids": []}
        _ = change_old(old["login"], select)
        self.assertIsNone(shutdown_store.account_hold(old["login"]))


    def test_migrate_each_legacy_state_and_keep_its_inferred_hold(self) -> None:
        for state in ("settling", "stopping", "down", "stop partial", "restarting", "restart partial", "cancelled", "up"):
            with self.subTest(state=state):
                saved = shutdown_record(f"{state}@example.com", state=state)
                saved["conductor"] = {"kind": "systemd", "unit": "old-service"}
                saved["stop_issues"] = [{"kind": "machine stop failed", "at": NOW, "machine": "Mac", "reason": "not reached"}]
                worker = entry({"kind": "passive worker ready", "at": NOW}, number=9)
                worker["session"] = owned_worker()
                saved["entries"].append(worker)
                write_old(saved)
                before = shutdown_store.session_records(saved["login"])
                hold = shutdown_store.account_hold(saved["login"])
                lines = shutdown_store.migrate(saved["login"])
                self.assertEqual(len(lines), 1)
                self.assertIn(f"converted the {state} record", lines[0])
                self.assertEqual(shutdown_store.session_records(saved["login"]), before)
                self.assertEqual(shutdown_store.account_hold(saved["login"]), hold)
                directory = self.directory.parent / saved["login"]
                self.assertFalse((directory / "record.json").exists())
                self.assertTrue((directory / "history" / f"{NOW}-record.json").is_file())
                self.assertEqual(shutdown_store.migrate(saved["login"]), [])

    def test_session_reads_without_existing_lock_do_not_change_the_files(self) -> None:
        write_old(shutdown_record(state="down"))
        before = {path.relative_to(self.directory): path.read_bytes() for path in self.directory.rglob("*") if path.is_file()}
        self.assertEqual(len(shutdown_store.session_records("owner@example.com")), 1)
        self.assertIsNotNone(shutdown_store.find_session("owner@example.com", "session-1"))
        self.assertIsNotNone(shutdown_store.account_hold("owner@example.com"))
        after = {path.relative_to(self.directory): path.read_bytes() for path in self.directory.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

    def test_migrate_invalid_finished_file_moves_and_writes_nothing(self) -> None:
        write_old(shutdown_record())
        partial = self.directory / "partial"
        partial.mkdir()
        broken = partial / "finished.json"
        _ = broken.write_text(json.dumps({"state": "up"}))
        before = {path.relative_to(self.directory): path.read_bytes() for path in self.directory.rglob("*.json")}
        with self.assertRaises(shutdown_store.InvalidRecord) as raised:
            _ = shutdown_store.migrate("owner@example.com")
        self.assertIn(str(broken), str(raised.exception))
        after = {path.relative_to(self.directory): path.read_bytes() for path in self.directory.rglob("*.json")}
        self.assertEqual(after, before)

    def test_migrate_preserves_explicit_hold_and_new_session_precedence(self) -> None:
        explicit = account_hold()
        explicit["request_id"] = "current-hold"
        shutdown_store.set_account_hold(explicit)
        fresh = session_shutdown()
        self.assertTrue(shutdown_store.create_session(fresh))
        write_old(shutdown_record(progress=shutdown_store.SessionNeedsManualRestart(kind="manual restart", command="old")))
        _ = shutdown_store.migrate(fresh["login"])
        self.assertEqual(shutdown_store.find_session(fresh["login"], "session-1"), fresh)
        self.assertEqual(shutdown_store.account_hold(fresh["login"]), explicit)

    def test_migrate_refuses_a_legacy_id_that_collides_with_a_current_filename(self) -> None:
        current = session_shutdown()
        current["session"]["session_id"] = "id-one"
        self.assertTrue(shutdown_store.create_session(current))
        old = shutdown_record()
        old["entries"][0]["session"]["session_id"] = "id/one"
        write_old(old)
        before = {path.relative_to(self.directory): path.read_bytes()
                  for path in self.directory.rglob("*") if path.is_file()}
        with self.assertRaises(shutdown_store.InvalidRecord) as raised:
            _ = shutdown_store.migrate(current["login"])
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
                    saved["progress"] = cast(shutdown_store.SessionProgress, cast(object, progress))
                    if issues:
                        saved["stop_issues"] = [shutdown_store.UnitSessionLeftRunning(
                            kind="unit session left running", at=NOW,
                            host="herdr", session_name="unit", cause="stop not confirmed")]
                    self.assertEqual(shutdown_store.stop_incomplete(saved), kind == "stop failed" or (
                        issues and kind in {"stopped", "already gone", "process identity lost"}))

    def test_usage_limit_progress_and_not_ready_issue_round_trip(self) -> None:
        held = shutdown_store.SessionHeldByUsageLimit(kind="held by usage limit", at=NOW)
        saved = session_shutdown(progress=held)
        saved["stop_issues"] = [shutdown_store.NotReadyToStop(kind="not ready to stop", at=NOW,
            status="idle", progress="held by usage limit")]
        self.assertTrue(shutdown_store.create_session(saved))
        self.assertEqual(shutdown_store.find_session(saved["login"], "session-1"), saved)
        self.assertEqual(shutdown_store.parse_session_records(json.dumps([saved])), [saved])
        restarting = session_shutdown(number=2, progress=shutdown_store.SessionRestarting(
            kind="restarting", at=LATER, prior=held))
        self.assertEqual(shutdown_store.parse_session_records(json.dumps([restarting])), [restarting])
        old = shutdown_record(progress=shutdown_store.SessionReadyToStop(kind="ready", at=NOW))
        write_old(old)
        self.assertEqual(shutdown_store.find_session(saved["login"], "session-1"), saved)
        shutdown_store.archive_session(saved["login"], "session-1")
        legacy = shutdown_store.find_session(saved["login"], "session-1")
        assert legacy is not None
        self.assertEqual(legacy["progress"], {"kind": "ready", "at": NOW})

    def test_withdraw_session_takes_usage_limit_held_owner_out(self) -> None:
        saved = session_shutdown(progress=shutdown_store.SessionHeldByUsageLimit(kind="held by usage limit", at=NOW))
        self.assertTrue(shutdown_store.create_session(saved))
        restore = Mock()
        result = shutdown_store.withdraw_session(saved["login"], "session-1", restore)
        self.assertIsInstance(result, shutdown_store.SessionWithdrawn)
        restore.assert_called_once_with(saved)
        self.assertIsNone(shutdown_store.find_session(saved["login"], "session-1"))

    def test_withdraw_session_keeps_a_stop_claim_and_does_not_restore_timers(self) -> None:
        saved = session_shutdown(progress=shutdown_store.SessionStopping(kind="stopping", at=NOW))
        self.assertTrue(shutdown_store.create_session(saved))
        with patch.object(shutdown_store, "archive_session") as archive:
            restore = Mock()
            result = shutdown_store.withdraw_session(saved["login"], "session-1", restore)
        self.assertIsInstance(result, shutdown_store.SessionRetained)
        restore.assert_not_called()
        archive.assert_not_called()
        self.assertEqual(shutdown_store.find_session(saved["login"], "session-1"), saved)

    def test_withdraw_session_restores_and_archives_under_the_account_lock(self) -> None:
        saved = session_shutdown()
        self.assertTrue(shutdown_store.create_session(saved))
        def restore(_saved: shutdown_store.SessionShutdown) -> None:
            with (self.directory / "lock").open("a+") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = shutdown_store.withdraw_session(saved["login"], "session-1", restore)
        self.assertIsInstance(result, shutdown_store.SessionWithdrawn)
        self.assertIsNone(shutdown_store.find_session(saved["login"], "session-1"))

    def test_conductor_lock_allows_only_one_local_loop(self) -> None:
        with shutdown_store.conductor_lock("owner@example.com") as first:
            self.assertIsInstance(first, shutdown_store.ConductorAcquired)
            with shutdown_store.conductor_lock("owner@example.com") as second:
                self.assertIsInstance(second, shutdown_store.ConductorAlreadyRunning)
        with shutdown_store.conductor_lock("owner@example.com") as third:
            self.assertIsInstance(third, shutdown_store.ConductorAcquired)


    def test_failed_stop_withdrawal_checks_identity_and_restores_under_account_lock(self) -> None:
        saved = session_shutdown(progress={"kind": "stop failed", "at": NOW, "reason": "still live"})
        self.assertTrue(shutdown_store.create_session(saved))
        events: list[str] = []
        def check_lock() -> None:
            with (self.directory / "lock").open("r") as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        def identity(current: shutdown_store.SessionShutdown) -> bool:
            check_lock()
            self.assertEqual(current, saved)
            events.append("identity")
            return True
        def restore(current: shutdown_store.SessionShutdown) -> None:
            check_lock()
            self.assertEqual(current, saved)
            events.append("restore")
        result = shutdown_store.withdraw_failed_stop(saved["login"], "session-1", identity, restore)
        self.assertIsInstance(result, shutdown_store.SessionWithdrawn)
        self.assertEqual(events, ["identity", "restore"])
        self.assertIsNone(shutdown_store.find_session(saved["login"], "session-1"))
        self.assertEqual(len(list((self.directory / "history").glob("*.json"))), 1)

    def test_failed_stop_withdrawal_keeps_unmatched_identity_without_resource_changes(self) -> None:
        saved = session_shutdown(progress={"kind": "stop failed", "at": NOW, "reason": "still live"})
        self.assertTrue(shutdown_store.create_session(saved))
        restore = Mock()
        result = shutdown_store.withdraw_failed_stop(saved["login"], "session-1", lambda _saved: False, restore)
        self.assertIsInstance(result, shutdown_store.SessionRetained)
        restore.assert_not_called()
        self.assertEqual(shutdown_store.find_session(saved["login"], "session-1"), saved)

    def test_failed_stop_withdrawal_loses_to_a_competing_stop_claim(self) -> None:
        saved = session_shutdown(progress={"kind": "stop failed", "at": NOW, "reason": "still live"})
        self.assertTrue(shutdown_store.create_session(saved))
        blocked, finished = threading.Event(), threading.Event()
        outcomes: list[shutdown_store.SessionWithdrawn | shutdown_store.SessionRetained] = []
        failures: list[BaseException] = []
        identity, restore = Mock(return_value=True), Mock()
        def cancel() -> None:
            try:
                outcomes.append(shutdown_store.withdraw_failed_stop(saved["login"], "session-1", identity, restore))
            except BaseException as error:
                failures.append(error)
            finally:
                finished.set()
        def claim(current: shutdown_store.SessionShutdown) -> None:
            worker.start()
            self.assertTrue(blocked.wait(HANG_GUARD_SECONDS))
            self.assertFalse(finished.is_set())
            current["progress"] = {"kind": "stopping", "at": LATER}
        worker = threading.Thread(target=cancel)
        with observe_lock_contention(blocked):
            try:
                claimed = shutdown_store.update_session(saved["login"], "session-1", claim)
            finally:
                worker.join(HANG_GUARD_SECONDS)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], shutdown_store.SessionRetained)
        identity.assert_not_called()
        restore.assert_not_called()
        self.assertEqual(shutdown_store.find_session(saved["login"], "session-1"), claimed)

    def test_conductor_status_observes_held_released_absent_and_unreadable_lock(self) -> None:
        login = "owner@example.com"
        self.assertIsInstance(shutdown_store.conductor_status(login), shutdown_store.ConductorNotRunning)
        self.assertFalse(self.directory.exists())
        with shutdown_store.conductor_lock(login):
            before = {path: path.read_bytes() for path in self.directory.iterdir()}
            self.assertIsInstance(shutdown_store.conductor_status(login), shutdown_store.ConductorRunning)
            self.assertEqual({path: path.read_bytes() for path in self.directory.iterdir()}, before)
        self.assertIsInstance(shutdown_store.conductor_status(login), shutdown_store.ConductorNotRunning)
        (self.directory / "conduct.lock").unlink()
        self.assertIsInstance(shutdown_store.conductor_status(login), shutdown_store.ConductorNotRunning)
        self.assertFalse((self.directory / "conduct.lock").exists())
        (self.directory / "conduct.lock").mkdir()
        self.assertIsInstance(shutdown_store.conductor_status(login), shutdown_store.ConductorUnavailable)

    def test_named_shutdown_and_account_lookup_outcomes_preserve_domain_meaning(self) -> None:
        login = "owner@example.com"
        self.assertIsInstance(shutdown_store.lookup_session(login, "session-1"), shutdown_store.NoShutdown)
        self.assertIsInstance(shutdown_store.lookup_account_hold(login), shutdown_store.AccountOpen)
        saved, held = session_shutdown(), account_hold()
        self.assertTrue(shutdown_store.create_session(saved))
        shutdown_store.set_account_hold(held)
        self.assertEqual(shutdown_store.lookup_session(login, "session-1"), shutdown_store.ShutdownFound(saved))
        self.assertEqual(shutdown_store.lookup_account_hold(login), shutdown_store.AccountHeld(held))

    def test_conductor_start_retries_a_brief_observation_lock(self) -> None:
        login = "owner@example.com"
        with shutdown_store.conductor_lock(login):
            pass
        path = self.directory / "conduct.lock"
        retrying, released = threading.Event(), threading.Event()
        outcomes: list[shutdown_store.ConductorAcquired | shutdown_store.ConductorAlreadyRunning] = []
        failures: list[BaseException] = []
        def retry(_seconds: float) -> None:
            retrying.set()
            self.assertTrue(released.wait(HANG_GUARD_SECONDS))
        def start() -> None:
            try:
                with shutdown_store.conductor_lock(login) as outcome:
                    outcomes.append(outcome)
            except BaseException as error:
                failures.append(error)
        with path.open("r") as observation, patch.object(time, "sleep", side_effect=retry):
            fcntl.flock(observation.fileno(), fcntl.LOCK_EX)
            worker = threading.Thread(target=start)
            worker.start()
            try:
                self.assertTrue(retrying.wait(HANG_GUARD_SECONDS))
            finally:
                fcntl.flock(observation.fileno(), fcntl.LOCK_UN)
                released.set()
                worker.join(HANG_GUARD_SECONDS)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], shutdown_store.ConductorAcquired)

    def test_conductor_start_rejects_a_persistent_owner_after_bounded_retries(self) -> None:
        login = "owner@example.com"
        with shutdown_store.conductor_lock(login):
            with patch.object(time, "sleep") as sleep, shutdown_store.conductor_lock(login) as duplicate:
                self.assertIsInstance(duplicate, shutdown_store.ConductorAlreadyRunning)
        self.assertEqual(sleep.call_count, 5)
        self.assertEqual([call.args for call in sleep.call_args_list], [(0.05,)] * 5)


if __name__ == "__main__":
    _ = unittest.main()
