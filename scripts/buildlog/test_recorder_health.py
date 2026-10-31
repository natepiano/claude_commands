#!/usr/bin/env python3
"""Recorder health crosses machines through snapshots, without sharing retry state."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from typing import cast, override
from unittest import mock

import health
import launches
import record
import report
import store
import test_record_rerun as recovery_tests


class RecorderHealthTests(unittest.TestCase):
    recovery: recovery_tests.RecordRerunTests  # pyright: ignore[reportUninitializedInstanceVariable]
    producer: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    reader: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.recovery = recovery_tests.RecordRerunTests()
        self.addCleanup(self.recovery.doCleanups)
        self.recovery.setUp()
        self.producer = self.recovery.fixture.root
        self.reader = self.recovery.fixture.base / "natedev-buildlog"
        _ = recovery_tests.refresh_machine_health(self, "natedev", self.reader, self.reader)

    def transfer_peer_snapshot(self, host: str = "Mac") -> Path:
        source = self.producer / host
        destination = self.reader / host
        _ = shutil.copytree(source, destination, dirs_exist_ok=True)
        return destination / health.SNAPSHOT_NAME

    def collect_on_mac(self) -> launches.LaunchCounts:
        with mock.patch.object(store, "host_name", return_value="Mac"):
            return launches.collect()

    def reader_lines(self) -> list[str]:
        with (
            mock.patch.dict(os.environ, {"BUILDLOG_DIR": str(self.reader)}),
            mock.patch.object(health, "calculate_snapshot", side_effect=AssertionError("reader scanned recorder state")),
        ):
            return report.recorder_health_lines("2026-10-02")

    def assert_mac_line(self, unfinished: int, errors: int) -> str:
        lines = self.reader_lines()
        self.assertEqual(len(lines), 1)
        record_word = "record" if unfinished == 1 else "records"
        error_word = "error" if errors == 1 else "errors"
        self.assertRegex(lines[0], rf"^Mac: {unfinished} {record_word} the recorder has not finished; {errors} recorder {error_word} since ")
        self.assertTrue(lines[0].endswith("run buildlog launches on the Mac"), lines[0])
        self.assertNotIn("not published yet", lines[0])
        return lines[0]

    def unfinished_steps(self, count: int) -> list[Path]:
        root = record.pending_steps_root()
        root.mkdir(parents=True, exist_ok=True)
        paths = [root / f"unfinished-{number}.json" for number in range(count)]
        for path in paths:
            _ = path.write_text("{}")
        return paths

    def assert_peer_failure_recovers(self, *, cleanup: bool) -> None:
        unlink = os.unlink

        def refuse_held_delete(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes], *, dir_fd: int | None = None,
        ) -> None:
            if Path(os.fsdecode(path)) == self.recovery.held:
                raise OSError("fixture cleanup refused")
            unlink(path, dir_fd=dir_fd)

        with ExitStack() as faults:
            if cleanup:
                _ = cast(object, faults.enter_context(mock.patch.object(os, "unlink", side_effect=refuse_held_delete)))
            else:
                _ = cast(object, faults.enter_context(mock.patch.object(store, "append_locked", side_effect=OSError("fixture publish refused"))))
            self.assertEqual(self.recovery.run_step(), 0)
            retried = self.collect_on_mac()
            self.assertEqual(retried.steps_added, 0)
            self.assertEqual(len(self.recovery.step_records()), 1 if cleanup else 0)
            self.assertEqual(len(list(record.pending_steps_root().glob("*.json"))), 1)

        _ = self.transfer_peer_snapshot()
        self.assertFalse((self.reader / store.ERRORS_NAME).exists())
        self.assertFalse((self.reader / "pending-steps").exists())
        line = self.assert_mac_line(1, 2)
        # No report has run on the producing machine: collection alone refreshed it.
        with mock.patch.dict(os.environ, {"BUILDLOG_DIR": str(self.reader)}):
            self.assertIn(line, self.recovery.report_text())
        error_history = (self.producer / store.ERRORS_NAME).read_bytes()
        finished = self.collect_on_mac()
        self.assertEqual(finished.steps_added, 0 if cleanup else 1)
        self.assertEqual(len(self.recovery.step_records()), 1)
        self.assertFalse(self.recovery.held.exists())
        self.assertFalse(self.recovery.peak.exists())
        self.assertEqual(list(record.pending_steps_root().glob("*.json")), [])
        self.assertEqual((self.producer / store.ERRORS_NAME).read_bytes(), error_history)
        _ = self.transfer_peer_snapshot()
        self.assertEqual(self.reader_lines(), [])

    def test_mac_publish_failure_reaches_natedev_without_a_mac_report_and_clears_on_recovery(self) -> None:
        self.assert_peer_failure_recovers(cleanup=False)

    def test_published_row_with_failed_cleanup_still_counts_as_unfinished(self) -> None:
        self.assert_peer_failure_recovers(cleanup=True)

    def test_failed_snapshot_replace_preserves_failure_data_until_it_becomes_stale(self) -> None:
        with mock.patch.object(store, "append_locked", side_effect=OSError("fixture publish refused")):
            self.assertEqual(self.recovery.run_step(), 0)
            _ = self.collect_on_mac()
        source = self.producer / "Mac" / health.SNAPSHOT_NAME
        original = source.read_bytes()
        generated = cast(dict[str, object], json.loads(original))["generated_at"]
        self.assertIsInstance(generated, int | float)
        pending = list(record.pending_steps_root().glob("*.json"))
        self.assertEqual(len(pending), 1)
        self.assertTrue(record.finish_pending_step(pending[0]).added)
        replace = os.replace

        def refuse_snapshot(old: str | os.PathLike[str], new: str | os.PathLike[str]) -> None:
            if Path(new) == source:
                raise OSError("fixture snapshot replacement refused")
            replace(old, new)

        with mock.patch.object(store, "host_name", return_value="Mac"), mock.patch.object(os, "replace", side_effect=refuse_snapshot):
            self.assertFalse(health.write_snapshot())
        self.assertEqual(source.read_bytes(), original)
        self.assertIn("fixture snapshot replacement refused", (self.producer / store.ERRORS_NAME).read_text())
        _ = self.transfer_peer_snapshot()
        _ = self.assert_mac_line(1, 2)
        with mock.patch.object(time, "time", return_value=float(cast(float, generated)) + 7201):
            lines = self.reader_lines()
        mac = [line for line in lines if line.startswith("Mac:")]
        self.assertEqual(len(mac), 1)
        self.assertIn("unknown", mac[0].lower())
        self.assertIn("stale", mac[0].lower())
        self.assertNotIn("0 records", mac[0])
        self.assertIn("run buildlog launches on the Mac", mac[0])

    def test_freshness_uses_generated_at_and_two_hourly_periods_not_sync_status(self) -> None:
        snapshot = recovery_tests.refresh_machine_health(self, "Mac", self.producer, self.reader)
        document = cast(dict[str, object], json.loads(snapshot.read_text()))
        generated = cast(float, document["generated_at"])
        for name in ("sync.json", "sync_paused.json"):
            _ = (self.reader / name).write_text('{"ok":false,"last_ok":"1999-01-01T00:00:00Z"}')
        self.assertEqual(self.reader_lines(), [])
        with mock.patch.dict(os.environ, {"BUILDLOG_DIR": str(self.reader)}):
            with mock.patch.object(time, "time", return_value=generated + 7200):
                fresh = health.read_snapshots()
                self.assertFalse(any(line.startswith("Mac:") for line in self.reader_lines()))
            with mock.patch.object(time, "time", return_value=generated + 7200.01):
                stale = health.read_snapshots()
        self.assertIsInstance(fresh[1], health.FreshRecorderHealth)
        self.assertIsInstance(stale[1], health.StaleRecorderHealth)
        if not isinstance(stale[1], health.StaleRecorderHealth):
            self.fail("Mac snapshot should be stale")
        self.assertEqual(stale[1].snapshot.machine, health.Machine.MAC)
        self.assertEqual(stale[1].snapshot.unfinished_records, 0)
        self.assertEqual(stale[1].snapshot.generated_at, generated)

    def test_absent_or_unreadable_mac_snapshot_is_unknown_with_its_recovery_command(self) -> None:
        for unreadable in (False, True):
            with self.subTest(unreadable=unreadable):
                if unreadable:
                    source = self.reader / "Mac" / health.SNAPSHOT_NAME
                    source.parent.mkdir(parents=True, exist_ok=True)
                    _ = source.write_text("not a JSON snapshot\n")
                with mock.patch.dict(os.environ, {"BUILDLOG_DIR": str(self.reader)}):
                    states = health.read_snapshots()
                self.assertEqual(len(states), 2)
                self.assertIsInstance(states[1], health.RecorderHealthUnavailable)
                lines = self.reader_lines()
                self.assertEqual(len(lines), 1)
                self.assertTrue(lines[0].startswith("Mac:"))
                self.assertIn("unknown", lines[0].lower())
                self.assertNotIn("0 records", lines[0])
                self.assertIn("run buildlog launches on the Mac", lines[0])

    def test_both_mac_folder_names_select_the_newest_valid_snapshot_and_count_one_machine(self) -> None:
        # The historical folder has a more recent snapshot with a retry; the
        # current folder's older clean snapshot must not hide it or add a third host.
        _ = recovery_tests.refresh_machine_health(self, "Mac", self.producer, self.reader)
        pending = record.pending_steps_root() / "waiting-for-sidecar.json"
        pending.parent.mkdir(parents=True, exist_ok=True)
        _ = pending.write_text('{"test_phase_read_error":"temporary read failure"}')
        with mock.patch.object(time, "time", return_value=time.time() + 1):
            _ = recovery_tests.refresh_machine_health(self, "Nates-MacBook-Pro-2023", self.producer, self.reader)
        with mock.patch.dict(os.environ, {"BUILDLOG_DIR": str(self.reader)}):
            states = health.read_snapshots()
        self.assertEqual(len(states), 2)
        self.assertIsInstance(states[0], health.FreshRecorderHealth)
        self.assertIsInstance(states[1], health.FreshRecorderHealth)
        if not isinstance(states[1], health.FreshRecorderHealth):
            self.fail("historical Mac snapshot should resolve to the Mac")
        self.assertEqual(states[1].snapshot.machine, health.Machine.MAC)
        self.assertEqual(states[1].snapshot.unfinished_records, 1)
        lines = self.reader_lines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("Mac: 1 record the recorder has not finished; 0 recorder errors since "))
        self.assertNotIn("Nates-MacBook-Pro-2023", lines[0])

    def test_future_mac_alias_cannot_hide_current_unfinished_records(self) -> None:
        pending = self.unfinished_steps(2)
        now = time.time()
        with mock.patch.object(time, "time", return_value=now):
            _ = recovery_tests.refresh_machine_health(self, "Mac", self.producer, self.reader)
            for path in pending:
                path.unlink()
            with mock.patch.object(time, "time", return_value=now + 3 * 60 * 60):
                _ = recovery_tests.refresh_machine_health(self, "Nates-MacBook-Pro-2023", self.producer, self.reader)
            with mock.patch.dict(os.environ, {"BUILDLOG_DIR": str(self.reader)}):
                states = health.read_snapshots()
            if not isinstance(states[1], health.FreshRecorderHealth):
                self.fail("the current Mac snapshot should remain usable")
            self.assertEqual(states[1].snapshot.unfinished_records, 2)
            self.assertEqual(states[1].snapshot.generated_at, now)
            _ = self.assert_mac_line(2, 0)

    def test_snapshot_beyond_clock_skew_is_unavailable_instead_of_clean(self) -> None:
        now = time.time()
        for advance, usable in ((300, True), (300.01, False), (3 * 60 * 60, False)):
            with self.subTest(seconds_ahead=advance):
                with mock.patch.object(time, "time", return_value=now + advance):
                    _ = recovery_tests.refresh_machine_health(self, "Mac", self.producer, self.reader)
                with mock.patch.object(time, "time", return_value=now):
                    with mock.patch.dict(os.environ, {"BUILDLOG_DIR": str(self.reader)}):
                        states = health.read_snapshots()
                    lines = self.reader_lines()
                if usable:
                    self.assertIsInstance(states[1], health.FreshRecorderHealth)
                    self.assertEqual(lines, [])
                else:
                    self.assertIsInstance(states[1], health.RecorderHealthUnavailable)
                    self.assertEqual(len(lines), 1)
                    self.assertIn("Mac: recorder health unknown (snapshot unavailable)", lines[0])
                    self.assertTrue(lines[0].endswith("run buildlog launches on the Mac"))
                    self.assertNotIn("0 records", lines[0])

    def test_overlapping_writers_calculate_and_publish_in_lock_order(self) -> None:
        pending = self.unfinished_steps(2)
        first_calculated = threading.Event()
        second_started = threading.Event()
        release_first = threading.Event()
        first_published = threading.Event()
        calculate = health.calculate_snapshot
        flock = fcntl.flock
        replace = os.replace
        calculations: list[health.RecorderHealthSnapshot] = []
        results: dict[str, bool] = {}

        def calculate_in_order() -> health.RecorderHealthSnapshot:
            if threading.current_thread().name == "second-health-writer":
                premature = not first_published.is_set()
                second_started.set()
                if premature:
                    raise RuntimeError("second calculation ran before the first publication")
            snapshot = calculate()
            calculations.append(snapshot)
            if threading.current_thread().name == "first-health-writer":
                first_calculated.set()
                if not release_first.wait(5):
                    raise RuntimeError("first writer was not released")
            return snapshot

        def observe_lock(descriptor: int, operation: int) -> None:
            if threading.current_thread().name == "second-health-writer":
                second_started.set()
            flock(descriptor, operation)

        def observe_publication(old: str | os.PathLike[str], new: str | os.PathLike[str]) -> None:
            replace(old, new)
            if threading.current_thread().name == "first-health-writer":
                first_published.set()

        def write() -> None:
            results[threading.current_thread().name] = health.write_snapshot()

        first = threading.Thread(target=write, name="first-health-writer", daemon=True)
        second = threading.Thread(target=write, name="second-health-writer", daemon=True)
        with (
            mock.patch.object(store, "host_name", return_value="Mac"),
            mock.patch.object(health, "calculate_snapshot", side_effect=calculate_in_order),
            mock.patch.object(fcntl, "flock", side_effect=observe_lock),
            mock.patch.object(os, "replace", side_effect=observe_publication),
        ):
            first.start()
            try:
                self.assertTrue(first_calculated.wait(5), "first writer did not calculate")
                for path in pending:
                    path.unlink()
                second.start()
                self.assertTrue(second_started.wait(5), "second writer did not start")
            finally:
                release_first.set()
                first.join(5)
                if second.ident is not None:
                    second.join(5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(results, {"first-health-writer": True, "second-health-writer": True})
        self.assertEqual([snapshot.unfinished_records for snapshot in calculations], [2, 0])
        states = health.read_snapshots()
        if not isinstance(states[1], health.FreshRecorderHealth):
            self.fail("expected the second writer's fresh Mac snapshot")
        self.assertEqual(states[1].snapshot, calculations[1])
        self.assertEqual(states[1].snapshot.unfinished_records, 0)

    def test_snapshot_keeps_error_times_for_the_readers_local_midnight(self) -> None:
        self.producer.mkdir(parents=True, exist_ok=True)
        _ = (self.producer / store.ERRORS_NAME).write_text("\n".join([
            "2026-10-02T03:59:59.999Z before Eastern midnight",
            "2026-10-02T04:00:00.000Z at Eastern midnight", "",
        ]))
        _ = recovery_tests.refresh_machine_health(self, "Mac", self.producer, self.reader)
        with mock.patch.dict(os.environ, {"BUILDLOG_DIR": str(self.reader)}):
            states = health.read_snapshots()
        if not isinstance(states[1], health.FreshRecorderHealth):
            self.fail("expected a fresh Mac error snapshot")
        self.assertEqual(len(states[1].snapshot.error_timestamps), 2)
        with mock.patch.dict(os.environ, {"USER_ZONE": "America/New_York"}):
            eastern = self.reader_lines()
        with mock.patch.dict(os.environ, {"USER_ZONE": "UTC"}):
            utc = self.reader_lines()
        self.assertRegex(eastern[0], r"1 recorder error since 2026-10-02 00:00:00 EDT")
        self.assertIn("2 recorder errors since 2026-10-02 00:00:00 UTC", utc[0])

    def test_health_counts_unreadable_phase_retry_without_opening_the_sidecar(self) -> None:
        pending = record.pending_steps_root() / "unreadable-phase.json"
        pending.parent.mkdir(parents=True, exist_ok=True)
        sidecar = self.recovery.fixture.base / "private-phase.json"
        _ = pending.write_text(json.dumps({"test_phase_read_error": "permission denied", "test_phase_file": str(sidecar)}))
        _ = (self.producer / store.ERRORS_NAME).write_text(
            f"2026-10-02T12:00:00.000Z pending record {pending}\nPermissionError: fixture phase source\n"
        )
        with mock.patch.object(record, "read_test_phase", side_effect=AssertionError("health opened sidecar")):
            _ = recovery_tests.refresh_machine_health(self, "Mac", self.producer, self.reader)
        _ = self.assert_mac_line(1, 1)
        self.assertFalse(sidecar.exists())

    def test_mac_report_names_a_natedev_failure_and_its_natedev_recovery_command(self) -> None:
        with mock.patch.object(store, "append_locked", side_effect=OSError("fixture natedev publish refused")):
            self.assertEqual(self.recovery.run_step(), 0)
        _ = recovery_tests.refresh_machine_health(self, "natedev", self.producer, self.reader)
        _ = recovery_tests.refresh_machine_health(self, "Mac", self.reader / "clean-local", self.reader)
        with mock.patch.object(store, "host_name", return_value="Mac"):
            lines = self.reader_lines()
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"^natedev: 1 record the recorder has not finished; 1 recorder error since ")
        self.assertTrue(lines[0].endswith("run buildlog launches on natedev"))


if __name__ == "__main__":
    _ = unittest.main()
