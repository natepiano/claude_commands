"""The stop's record operations share its deadline, including recovery."""
from __future__ import annotations

import fcntl
import json
import os
import stat
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from collections.abc import Callable
from typing import TextIO, cast, final
from unittest.mock import patch

import test_bounded_stop as bounded
from test_stop import NOW_UTC, machine_inventory, top_level
import inventory
import shutdown_record as record
import settle
import stop


@final
class RecordDeadlineTests(bounded.BoundedShutdownTests):
    def test_record_locks_held_eighty_seconds_fail_by_sixty_six_without_late_publication(self) -> None:
        for directory in (True, False):
            with self.subTest(directory=directory):
                _ = self.output.seek(0)
                _ = self.output.truncate()
                saved = self.save(top_level(f"owner-{directory}", 101))
                before = record.find_session(saved["login"], saved["session"]["session_id"])
                descriptors: set[int] = set()
                def held(descriptor: int, operation: int) -> None:
                    if operation == fcntl.LOCK_UN:
                        return
                    descriptors.add(descriptor)
                    if stat.S_ISDIR(os.fstat(descriptor).st_mode) != directory:
                        return
                    if operation & fcntl.LOCK_NB:
                        raise BlockingIOError("fake holder releases at 80 seconds")
                    self.elapsed = 80
                self.elapsed = 0
                with patch.object(fcntl, "flock", side_effect=held):
                    result = self.run_stop([saved])
                self.assertLessEqual(self.elapsed, 66)
                self.assertEqual(result[0]["progress"]["kind"], "stop failed")
                self.assertIn("record not updated", str(result[0]["progress"]))
                # Closing the waiting descriptors also releases any outer lock.
                for descriptor in descriptors:
                    with self.assertRaises(OSError):
                        _ = os.fstat(descriptor)
                self.elapsed = 80
                self.assertEqual(record.find_session(saved["login"], saved["session"]["session_id"]), before)
                self.assertNotIn("stopped  owner", self.output.getvalue())
                self.elapsed = 0
                self.assertEqual(self.run_stop([saved], grace=0)[0]["progress"]["kind"], "stopped")

    def test_directory_and_file_lock_waits_spend_one_budget_before_callback(self) -> None:
        saved = self.save(top_level("owner", 101))
        changes: list[str] = []
        original = record.update_session
        def updating(login: str, identity: str, change: Callable[[record.SessionShutdown], None]) -> record.SessionShutdown:
            def callback(current: record.SessionShutdown) -> None:
                changes.append("entered")
                change(current)
            return original(login, identity, callback)
        def held(descriptor: int, operation: int) -> None:
            if operation == fcntl.LOCK_UN:
                return
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                self.elapsed = 50
            elif operation & fcntl.LOCK_NB:
                raise BlockingIOError("account file held until 80")
            else:
                self.elapsed = 80
        with patch.object(fcntl, "flock", side_effect=held), patch.object(stop, "update_session", updating):
            result = self.run_stop([saved])
        self.assertEqual(self.elapsed, 66)
        self.assertEqual(changes, [])
        self.assertIn("record not updated", str(result[0]["progress"]))
        self.assertEqual(record.find_session(saved["login"], "owner"), saved)

    def test_record_read_locks_cannot_restart_the_stop_budget(self) -> None:
        saved = self.save(top_level("owner", 101))
        original = record.find_session
        reading = False
        def read(login: str, identity: str) -> record.SessionShutdown | None:
            nonlocal reading
            reading = True
            return original(login, identity)
        def held(_descriptor: int, operation: int) -> None:
            if not reading or operation == fcntl.LOCK_UN:
                return
            if operation & fcntl.LOCK_NB:
                raise BlockingIOError("read lock held until 80")
            self.elapsed = 80
        with patch.object(stop, "find_session", read), patch.object(fcntl, "flock", side_effect=held):
            result = self.run_stop([saved])
        self.assertLessEqual(self.elapsed, 66)
        self.assertIn("record not updated", str(result[0]["progress"]))
        current = record.find_session(saved["login"], "owner")
        self.assertIsNotNone(current)
        assert current is not None
        self.assertNotEqual(current["progress"]["kind"], "stopped")

    def test_prepared_stopped_record_crossing_deadline_is_not_published(self) -> None:
        saved = self.save(top_level("owner", 101))
        dump = json.dump
        def preparing(value: object, output: TextIO, *, indent: int, sort_keys: bool) -> None:
            progress = cast(dict[str, object], value).get("progress") if isinstance(value, dict) else None
            if isinstance(progress, dict) and cast(dict[str, object], progress).get("kind") == "stopped":
                self.elapsed = 66
            dump(value, output, indent=indent, sort_keys=sort_keys)
        with patch.object(json, "dump", side_effect=preparing):
            result = self.run_stop([saved])
        self.assertEqual(result[0]["progress"]["kind"], "stop failed")
        self.assertIn("record not updated", str(result[0]["progress"]))
        current = record.find_session(saved["login"], "owner")
        self.assertIsNotNone(current)
        assert current is not None
        self.assertNotEqual(current["progress"]["kind"], "stopped")
        self.assertNotIn("stopped  owner", self.output.getvalue())
        self.assertEqual(list(self.root.rglob(".record.*.tmp")), [])

    def test_initial_record_preparation_crossing_deadline_leaves_record_unchanged(self) -> None:
        saved = self.save(top_level("owner", 101))
        def fsync(_descriptor: int) -> None:
            self.elapsed = 66
        with patch.object(os, "fsync", side_effect=fsync):
            result = self.run_stop([saved])
        self.assertEqual(result[0]["progress"]["kind"], "stop failed")
        self.assertIn("record not updated", str(result[0]["progress"]))
        self.assertEqual(record.find_session(saved["login"], "owner"), saved)
        self.assertEqual(list(self.root.rglob(".record.*.tmp")), [])

    def test_hold_release_after_down_all_uses_the_original_deadline(self) -> None:
        saved = self.save(top_level("owner", 101))
        finished = False
        def stopped(records: list[record.SessionShutdown], **_options: object) -> list[record.SessionShutdown]:
            nonlocal finished
            results = [record.update_session(item["login"], item["session"]["session_id"],
                lambda current: current.update(progress={"kind": "stopped", "at": saved["requested_at"]})) for item in records]
            self.elapsed = 65
            finished = True
            return results
        def held(_descriptor: int, operation: int) -> None:
            if not finished or operation == fcntl.LOCK_UN:
                return
            if operation & fcntl.LOCK_NB:
                raise BlockingIOError("hold release lock held until 80")
            self.elapsed = 80
        with (patch.object(inventory, "all_live_accounts", return_value=[machine_inventory([saved["session"]])]),
              patch.object(stop, "bounded_stop", side_effect=stopped),
              patch.object(settle, "_send_settle_message", return_value=None),
              patch.object(time, "monotonic", side_effect=lambda: self.elapsed),
              patch.object(time, "sleep", side_effect=self.advance),
              patch.object(fcntl, "flock", side_effect=held), redirect_stdout(self.output), redirect_stderr(self.output)):
            self.assertEqual(settle.down_all(), 1)
        self.assertEqual(self.elapsed, 66)
        self.assertIn("record not updated", self.output.getvalue())
        self.assertIsNotNone(record.account_hold(saved["login"]))
        self.assertIsInstance(record.release_finished_account_hold(saved["login"]), record.HoldReleased)

    def test_down_all_initial_registration_lock_is_inside_the_deadline(self) -> None:
        saved = self.save(top_level("owner", 101))
        def held(descriptor: int, operation: int) -> None:
            if operation == fcntl.LOCK_UN or not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                return
            if operation & fcntl.LOCK_NB:
                raise BlockingIOError("registration directory held until 80")
            self.elapsed = 80
        with (patch.object(inventory, "all_live_accounts", return_value=[machine_inventory([saved["session"]])]),
              patch.object(settle, "_send_settle_message", return_value=None),
              patch.object(time, "monotonic", side_effect=lambda: self.elapsed),
              patch.object(time, "sleep", side_effect=self.advance),
              patch.object(fcntl, "flock", side_effect=held), redirect_stdout(self.output), redirect_stderr(self.output)):
            self.assertEqual(settle.down_all(), 1)
        self.assertEqual(self.elapsed, 66)
        self.assertEqual(record.find_session(saved["login"], "owner"), saved)

    def test_expired_attempt_is_rescheduled_without_ever_using_legacy_stop(self) -> None:
        saved = self.save(top_level("owner", 101))
        live_step = settle.next_step(saved, {"kind": "session absent"}, [saved], NOW_UTC + timedelta(seconds=65))
        self.assertIsInstance(live_step, settle.Hold)
        expired_step = settle.next_step(saved, {"kind": "session absent"}, [saved], NOW_UTC + timedelta(seconds=66))
        self.assertIsInstance(expired_step, settle.StopSession)
        with patch.object(stop, "bounded_stop", return_value=[saved]) as retry, patch.object(stop, "stop_session") as legacy:
            _ = settle._stop_after_telling(saved, settle.StopSession())  # pyright: ignore[reportPrivateUsage]
        legacy.assert_not_called()
        self.assertEqual(retry.call_count, 1)
        self.assertEqual(retry.call_args.args, ([saved],))
        self.assertEqual(retry.call_args.kwargs["grace_seconds"], 0)

    def test_expired_registration_releases_only_after_every_covered_record_finishes(self) -> None:
        saved = self.save(top_level("owner", 101))
        assert "stop_deadline" in saved
        with patch.object(record, "complete_account_hold_registration", side_effect=OSError("interrupted registration")):
            with self.assertRaisesRegex(OSError, "interrupted registration"):
                _ = settle.begin(saved["login"], request_id=saved["request_id"],
                                 report=machine_inventory([]), stop_deadline=saved["stop_deadline"])
        held = record.account_hold(saved["login"])
        self.assertIsNotNone(held)
        assert held is not None
        self.assertEqual(held.get("registration_deadlines"),
                         {saved["request_id"]: (NOW_UTC + timedelta(seconds=66)).isoformat()})
        self.assertIsInstance(record.release_finished_account_hold(saved["login"], conducting=True), record.HoldInUse)
        _ = record.update_session(saved["login"], "owner", lambda current: current.update(
            progress={"kind": "stopped", "at": saved["requested_at"]}))
        self.assertIsInstance(record.release_finished_account_hold(saved["login"], conducting=True), record.HoldReleased)
        self.assertIsNone(record.account_hold(saved["login"]))

    def test_expired_unpersisted_attempt_retries_bounded_proof_and_releases_abandoned_registration(self) -> None:
        saved = self.save(top_level("owner", 101))
        hold = record.AccountHold(login=saved["login"], label=saved["label"], machine=saved["machine"],
            requested_at=saved["requested_at"], requested_by=saved["requested_by"], request_id=saved["request_id"], then="stop")
        # A registration interrupted before its completion write, as persisted by begin().
        record.register_account_hold_request(hold, account_wide=True)
        hold_path = self.root / "state" / saved["login"].casefold() / "hold.json"
        content = cast(dict[str, object], json.loads(hold_path.read_text()))
        content["registration_deadlines"] = {saved["request_id"]: (NOW_UTC + timedelta(seconds=66)).isoformat()}
        _ = hold_path.write_text(json.dumps(content))
        def held(_descriptor: int, operation: int) -> None:
            if operation == fcntl.LOCK_UN:
                return
            if operation & fcntl.LOCK_NB:
                raise BlockingIOError("record held until 80")
            self.elapsed = 80
        with patch.object(fcntl, "flock", side_effect=held):
            failed = self.run_stop([saved])
        self.assertEqual(failed[0]["progress"]["kind"], "stop failed")
        self.assertEqual(record.find_session(saved["login"], "owner"), saved)
        step = settle.next_step(saved, {"kind": "session absent"}, [saved], NOW_UTC + timedelta(seconds=80))
        self.assertIsInstance(step, settle.StopSession)
        self.elapsed = 0
        original = stop.bounded_stop
        def retry(records: list[record.SessionShutdown], **_options: object) -> list[record.SessionShutdown]:
            return original(records, started=0, grace_seconds=0, monotonic=lambda: self.elapsed, sleep=self.advance,
                            clock=lambda: NOW_UTC, observe=self.observe, identity=self.probe, kill=self.kill)
        with patch.object(stop, "bounded_stop", side_effect=retry), patch.object(stop, "stop_session") as legacy:
            result = settle._stop_after_telling(saved, settle.StopSession())  # pyright: ignore[reportPrivateUsage]
        legacy.assert_not_called()
        self.assertEqual(result["progress"]["kind"], "stopped")
        self.assertIsInstance(record.release_finished_account_hold(saved["login"], conducting=True), record.HoldReleased)
        self.assertIsNone(record.account_hold(saved["login"]))


def load_tests(_loader: unittest.TestLoader, _tests: unittest.TestSuite, _pattern: str | None) -> unittest.TestSuite:
    # Reuse the bounded-stop fake clock/process fixture without running its inherited tests twice.
    return unittest.TestSuite(RecordDeadlineTests(name) for name in RecordDeadlineTests.__dict__ if name.startswith("test_"))
