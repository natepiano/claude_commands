#!/usr/bin/env python3
"""An interrupted recorder keeps its original step until publication finishes."""

from __future__ import annotations

import fcntl
import gzip
import json
import os
import subprocess
import sys
import time
import unittest
from collections.abc import Callable
from contextlib import ExitStack, chdir, closing
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, cast, override
from unittest import mock

import index
import launches
import record
import report
import store
import test_launch_recording as launch_tests
import test_record as record_tests
from test_index import point_root_at, step as indexed_step
from test_launches import CLI

if TYPE_CHECKING:
    from ..direct.test_verify_untested_examples import FIXED_RUSTC
else:
    from scripts.direct.test_verify_untested_examples import FIXED_RUSTC


class RecorderStopped(BaseException):
    """The recorder stopped at a persistence or source-cleanup boundary."""


class StepRecordingFault(Enum):
    AFTER_LOG_SAVE = "after the failed log was saved"
    AFTER_ROW_PUBLICATION = "after the row was published"
    BEFORE_HELD_DELETE = "before deleting the held output"
    BEFORE_PEAK_DELETE = "before deleting the peak reading"
    BEFORE_COMPLETION_MARKER = "before marking the pending step complete"
    LOG_SAVE_FAILURE = "saving the failed log failed"
    PUBLICATION_FAILURE = "publishing the failed step failed"


class PendingRecordKind(Enum):
    STEP = "step"
    LAUNCH = "launch"


class RecordRerunTests(unittest.TestCase):
    fixture: record_tests.RecordTests  # pyright: ignore[reportUninitializedInstanceVariable]
    held: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    peak: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    output: bytes = b""
    peak_contents: bytes = b""

    @override
    def setUp(self) -> None:
        # Reuse the real recorder fixture's Git, tool and isolated-root setup.
        self.fixture = record_tests.RecordTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        environment = self.fixture.environment(
            BUILDLOG_TRANSCRIPTS=str(self.fixture.base / "transcripts"),
            BUILDLOG_CALLER="verify",
            BUILDLOG_CALL_ID=record_tests.CALL_ID,
            BUILDLOG_MEM_WAIT_S="17",
            BUILDLOG_TREE_START="tree-before-original-step",
            CODEX_THREAD_ID="original-step-worker",
            CLAUDE_CODE_SESSION_ID="original-parent-session",
            UNIT_TEAM_ROLE="test2",
            UNIT_RUN_DIR=str(self.fixture.base / "original-unit-run"),
            USER_ZONE="UTC",
            TZ="UTC",
        )
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, environment, clear=True)))
        self.held = self.fixture.handoff()
        self.peak = self.fixture.peak_file()
        self.output = self.held.read_bytes()
        self.peak_contents = (
            b"123456\n"
            b"some avg10=0.00 avg60=0.01 avg300=0.02 total=1250000\n"
            b"full avg10=0.00 avg60=0.00 avg300=0.00 total=250000\n"
        )
        _ = self.peak.write_bytes(self.peak_contents)

    def run_step(self, status: int = 1, *, with_sources: bool = True, with_held_log: bool = True) -> int:
        with chdir(self.fixture.repo), mock.patch.object(record, "detach"), \
             mock.patch.object(store, "host_name", return_value="original-host"):
            return record.main([
                "step", str(status), str(record_tests.START), str(record_tests.START + 12.5),
                "0", str(self.held) if with_sources and with_held_log else "",
                str(self.peak) if with_sources else "",
                *record_tests.NEXTEST,
            ])

    def fresh_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = self.fixture.environment(
            BUILDLOG_TRANSCRIPTS=str(self.fixture.base / "transcripts"),
            BUILDLOG_CALLER="different-caller",
            BUILDLOG_CALL_ID="different-call",
            BUILDLOG_MEM_WAIT_S="999",
            BUILDLOG_TREE_START="different-tree",
            CODEX_THREAD_ID="different-step-worker",
            CLAUDE_CODE_SESSION_ID="different-parent-session",
            UNIT_TEAM_ROLE="different-role",
            UNIT_RUN_DIR=str(self.fixture.base / "different-unit-run"),
            USER_ZONE="UTC",
            TZ="UTC",
        )
        result = subprocess.run(
            [sys.executable, str(CLI), *arguments], cwd=self.fixture.base,
            env=environment, capture_output=True, text=True, check=False, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def report_text(self) -> str:
        _ = index.update()
        with closing(index.read_only()) as connection:
            return report.report(connection, "2026-10-02")

    def saved_logs(self) -> list[Path]:
        return sorted(self.fixture.root.glob("*/logs/**/*.log.gz"))

    def step_records(self) -> list[record_tests.Record]:
        month = store.host_file("original-host", record_tests.START)
        rows = self.fixture.records()
        self.assertEqual(len(month.read_bytes().splitlines()) if month.exists() else 0, len(rows))
        self.assertEqual(sorted(self.fixture.root.glob("*/*.jsonl")), [month] if month.exists() else [])
        return rows

    def assert_original_step(self, row: record_tests.Record, original_id: str) -> None:
        self.assertEqual(row["id"], original_id)
        self.assertEqual(row["host"], "original-host")
        self.assertEqual((row["kind"], row["step"], row["status"]), ("step", "nextest", 1))
        self.assertEqual(row["argv"], record_tests.NEXTEST)
        self.assertEqual(row["cwd"], str(self.fixture.repo))
        self.assertEqual(row["worktree"], str(self.fixture.repo))
        self.assertEqual(row["repo_path"], str(self.fixture.repo))
        self.assertEqual(row["branch"], "main")
        self.assertEqual(row["sha"], self.fixture.git("rev-parse", "--short", "HEAD", cwd=self.fixture.repo))
        self.assertIsNone(row["tree_key"])
        self.assertIs(row["tree_changed"], True)
        self.assertEqual(row["started_at"], "2026-10-02T12:00:00.000Z")
        self.assertEqual(row["ended_at"], "2026-10-02T12:00:12.500Z")
        self.assertEqual(row["duration_s"], 12.5)
        self.assertEqual(row["session"], "original-step-worker")
        self.assertEqual(row["worker"], "test2")
        self.assertEqual(row["unit_session"], "original-unit-run")
        self.assertEqual(row["caller"], "verify")
        self.assertEqual(row["call_id"], record_tests.CALL_ID)
        self.assertEqual(row["mem_wait_s"], 17)
        self.assertEqual(row["slice"], "builds")
        self.assertEqual(row["peak_mem_bytes"], 123456)
        self.assertEqual(row["mem_stall_some_s"], 1.25)
        self.assertEqual(row["mem_stall_full_s"], 0.25)
        self.assertEqual((row["tests_run"], row["tests_failed"], row["tests_flaky"]), (5, 1, 1))
        self.assertEqual(row["rustc"], FIXED_RUSTC)
        self.assertEqual(len(self.saved_logs()), 1)
        self.assertEqual(self.fixture.root / str(row["log"]), self.saved_logs()[0])
        self.assertEqual(gzip.decompress(self.saved_logs()[0].read_bytes()), self.output)

    def launch_fixture(self) -> launch_tests.LaunchRecordingTests:
        fixture = launch_tests.LaunchRecordingTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        # Keep one build-log root while reusing the launch fixture's source writer.
        point_root_at(self, self.fixture.root)
        return fixture

    def assert_step_recovers(self, fault: StepRecordingFault) -> None:
        pending = record.pending_steps_root()
        original_ids: list[str] = []
        published_context: dict[str, object] = {}
        new_id, keep_log, publish = record.new_id, record.keep_log, record.write_step_line
        unlink, replace = os.unlink, os.replace

        def remember_original_id(host: str, epoch: float) -> str:
            identifier = new_id(host, epoch)
            original_ids.append(identifier)
            return identifier

        def stop_after_log(data: bytes, host: str, epoch: float, identifier: str) -> str:
            _ = keep_log(data, host, epoch, identifier)
            raise RecorderStopped(fault.value)

        def publish_and_capture(pending_step: record.PendingStep) -> bool:
            added = publish(pending_step)
            rows = self.step_records()
            self.assertEqual(len(rows), 1)
            published_context.update(rows[0])
            if fault is StepRecordingFault.AFTER_ROW_PUBLICATION:
                raise RecorderStopped(fault.value)
            return added

        def stop_before_delete(path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                               *, dir_fd: int | None = None) -> None:
            candidate = Path(os.fsdecode(path))
            target = self.held if fault is StepRecordingFault.BEFORE_HELD_DELETE else self.peak
            if candidate == target:
                raise RecorderStopped(fault.value)
            unlink(path, dir_fd=dir_fd)

        def stop_before_completion(old: str | os.PathLike[str], new: str | os.PathLike[str]) -> None:
            source, destination = Path(old), Path(new)
            if source.parent == pending and destination.suffix == ".done":
                raise RecorderStopped(fault.value)
            replace(old, new)

        with ExitStack() as cuts:
            _ = cast(object, cuts.enter_context(mock.patch.object(record, "new_id", side_effect=remember_original_id)))
            _ = cast(object, cuts.enter_context(mock.patch.object(record, "write_step_line", side_effect=publish_and_capture)))
            match fault:
                case StepRecordingFault.AFTER_LOG_SAVE:
                    _ = cast(object, cuts.enter_context(mock.patch.object(record, "keep_log", side_effect=stop_after_log)))
                case StepRecordingFault.BEFORE_HELD_DELETE | StepRecordingFault.BEFORE_PEAK_DELETE:
                    _ = cast(object, cuts.enter_context(mock.patch.object(os, "unlink", side_effect=stop_before_delete)))
                case StepRecordingFault.BEFORE_COMPLETION_MARKER:
                    _ = cast(object, cuts.enter_context(mock.patch.object(os, "replace", side_effect=stop_before_completion)))
                case StepRecordingFault.LOG_SAVE_FAILURE:
                    _ = cast(object, cuts.enter_context(mock.patch.object(record, "keep_log", side_effect=OSError(fault.value))))
                case StepRecordingFault.PUBLICATION_FAILURE:
                    _ = cast(object, cuts.enter_context(mock.patch.object(record, "write_step_line", side_effect=OSError(fault.value))))
                case StepRecordingFault.AFTER_ROW_PUBLICATION:
                    pass
            if fault in {StepRecordingFault.LOG_SAVE_FAILURE, StepRecordingFault.PUBLICATION_FAILURE}:
                self.assertEqual(self.run_step(), 0)
            else:
                with self.assertRaises(RecorderStopped):
                    _ = self.run_step()

        self.assertEqual(len(original_ids), 1)
        pending_paths = list(pending.glob("*.json"))
        self.assertEqual(len(pending_paths), 1)
        self.assertTrue(pending.is_relative_to(self.fixture.root))
        already_published = fault in {
            StepRecordingFault.AFTER_ROW_PUBLICATION,
            StepRecordingFault.BEFORE_HELD_DELETE,
            StepRecordingFault.BEFORE_PEAK_DELETE,
            StepRecordingFault.BEFORE_COMPLETION_MARKER,
        }
        self.assertEqual(len(self.step_records()), 1 if already_published else 0)
        self.assertEqual(len(self.saved_logs()), 0 if fault is StepRecordingFault.LOG_SAVE_FAILURE else 1)
        if fault in {StepRecordingFault.BEFORE_PEAK_DELETE, StepRecordingFault.BEFORE_COMPLETION_MARKER}:
            self.assertFalse(self.held.exists())
        else:
            self.assertEqual(self.held.read_bytes(), self.output)
        if fault is StepRecordingFault.BEFORE_COMPLETION_MARKER:
            self.assertFalse(self.peak.exists())
        else:
            self.assertEqual(self.peak.read_bytes(), self.peak_contents)

        # A fresh process has a different cwd, host, caller and session environment.
        _ = self.fresh_cli("launches")
        rows = self.step_records()
        self.assertEqual(len(rows), 1)
        self.assert_original_step(rows[0], original_ids[0])
        if published_context:
            self.assertEqual(rows[0], published_context)
        self.assertFalse(self.held.exists())
        self.assertFalse(self.peak.exists())
        self.assertEqual(list(pending.glob("*.json")), [])
        self.assertTrue(pending_paths[0].with_suffix(".done").is_file())
        self.assertEqual(list(self.fixture.root.rglob("*.tmp")), [])
        self.assertEqual(index.update(), 1)
        saved_log_bytes = self.saved_logs()[0].read_bytes()
        month = store.host_file("original-host", record_tests.START)
        source_lines = month.read_bytes()

        _ = self.fresh_cli("launches")
        repeated = launches.collect()
        self.assertEqual((repeated.added, repeated.steps_added), (0, 0))
        self.assertEqual(self.run_step(), 0)
        self.assertEqual(self.step_records(), rows)
        self.assertEqual(month.read_bytes(), source_lines)
        self.assertEqual(self.saved_logs()[0].read_bytes(), saved_log_bytes)
        self.assertEqual(index.update(), 0)
        self.assertEqual(list(pending.glob("*.json")), [])

    def test_stop_after_failed_log_save_finishes_the_original_step_once(self) -> None:
        self.assert_step_recovers(StepRecordingFault.AFTER_LOG_SAVE)

    def test_stop_after_row_publication_does_not_append_a_second_row(self) -> None:
        self.assert_step_recovers(StepRecordingFault.AFTER_ROW_PUBLICATION)

    def test_stop_before_held_log_delete_finishes_cleanup_once(self) -> None:
        self.assert_step_recovers(StepRecordingFault.BEFORE_HELD_DELETE)

    def test_stop_before_peak_delete_finishes_cleanup_once(self) -> None:
        self.assert_step_recovers(StepRecordingFault.BEFORE_PEAK_DELETE)

    def test_stop_before_completion_marker_finishes_with_both_sources_already_gone(self) -> None:
        self.assert_step_recovers(StepRecordingFault.BEFORE_COMPLETION_MARKER)

    def test_failed_log_save_keeps_both_sources_for_the_next_collect(self) -> None:
        self.assert_step_recovers(StepRecordingFault.LOG_SAVE_FAILURE)

    def test_failed_row_publication_keeps_sources_and_the_original_id(self) -> None:
        self.assert_step_recovers(StepRecordingFault.PUBLICATION_FAILURE)

    def test_recovery_starts_at_the_saved_month_offset_and_keeps_other_appends(self) -> None:
        month = store.host_file("original-host", record_tests.START)
        store.append_line(month, indexed_step("old-month-prefix", host="original-host"))
        prefix = month.read_bytes()
        with mock.patch.object(record, "write_step_line", side_effect=OSError("fixture append failed")):
            self.assertEqual(self.run_step(), 0)
        pending_paths = list(record.pending_steps_root().glob("*.json"))
        self.assertEqual(len(pending_paths), 1)
        saved = cast(dict[str, object], json.loads(pending_paths[0].read_text()))
        self.assertEqual(saved["month_offset"], len(prefix))
        original_row = cast(dict[str, object], saved["row"])
        store.append_line(month, indexed_step("intervening-append", host="original-host"))
        before_recovery = month.read_bytes()
        loads = json.loads

        def refuse_old_history(data: str | bytes | bytearray) -> object:
            self.assertNotIn("old-month-prefix", str(data), "recovery parsed history before month_offset")
            return cast(object, loads(data))

        with mock.patch.object(json, "loads", side_effect=refuse_old_history):
            finished = record.finish_pending_step(pending_paths[0])
        self.assertTrue(finished.added)
        rows = self.step_records()
        self.assertEqual(len(rows), 3)
        self.assertEqual([row["id"] for row in rows[:2]], ["old-month-prefix", "intervening-append"])
        self.assertTrue(month.read_bytes().startswith(before_recovery))
        self.assert_original_step(rows[2], str(original_row["id"]))
        self.assertFalse(self.held.exists())
        self.assertFalse(self.peak.exists())
        self.assertEqual(list(record.pending_steps_root().glob("*.json")), [])
        final_bytes = month.read_bytes()
        _ = self.fresh_cli("launches")
        self.assertEqual(self.run_step(), 0)
        self.assertEqual(month.read_bytes(), final_bytes)

    def test_normal_step_appends_without_opening_month_history_for_reading(self) -> None:
        month = store.host_file("original-host", record_tests.START)
        store.append_line(month, indexed_step("earlier-step", host="original-host"))
        open_path = Path.open

        def refuse_history_read(path: Path, mode: str = "r", buffering: int = -1,
                                encoding: str | None = None, errors: str | None = None,
                                newline: str | None = None) -> object:
            if path == month and ("r" in mode or "+" in mode):
                raise AssertionError("a normal step read the monthly history")
            return cast(object, open_path(path, mode, buffering, encoding, errors, newline))

        with mock.patch.object(Path, "open", new=refuse_history_read):
            self.assertEqual(self.run_step(), 0)
        rows = self.step_records()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["id"], "earlier-step")
        self.assertEqual(rows[1]["status"], 1)
        self.assertFalse(self.held.exists())
        self.assertFalse(self.peak.exists())

    def interrupt_month_append(self, complete_object: bool) -> tuple[Path, dict[str, object]]:
        write = os.write
        started = False

        def cut_write(fd: int, data: bytes | memoryview) -> int:
            nonlocal started
            if started:
                raise RecorderStopped("after a short monthly append")
            payload = bytes(data)
            self.assertTrue(payload.startswith(b'{"kind":"step"'))
            started = True
            cut = len(payload) - 1 if complete_object else payload.index(b',"host"')
            return write(fd, payload[:cut])

        with mock.patch.object(os, "write", side_effect=cut_write):
            with self.assertRaises(RecorderStopped):
                _ = self.run_step()
        paths = list(record.pending_steps_root().glob("*.json"))
        self.assertEqual(len(paths), 1)
        fields = cast(dict[str, object], json.loads(paths[0].read_text()))
        row = cast(dict[str, object], fields["row"])
        month = store.host_file("original-host", record_tests.START)
        self.assertFalse(month.read_bytes().endswith(b"\n"))
        self.assertTrue(self.held.is_file())
        self.assertTrue(self.peak.is_file())
        return paths[0], row

    def complete_month_rows(self) -> list[dict[str, object]]:
        month = store.host_file("original-host", record_tests.START)
        rows: list[dict[str, object]] = []
        for line in month.read_bytes().splitlines(keepends=True):
            if not line.endswith(b"\n"):
                continue
            try:
                row = cast(dict[str, object], json.loads(line))
            except ValueError:
                continue
            rows.append(row)
        return rows

    def assert_short_append_recovers(self, *, complete_object: bool, stop_after_repair: bool) -> None:
        pending, original = self.interrupt_month_append(complete_object)
        month = store.host_file("original-host", record_tests.START)
        if stop_after_repair:
            write = os.write

            def stop_after_newline(fd: int, data: bytes | memoryview) -> int:
                self.assertEqual(bytes(data), b"\n")
                _ = write(fd, data)
                raise RecorderStopped("immediately after tail repair")

            with mock.patch.object(os, "write", side_effect=stop_after_newline):
                with self.assertRaises(RecorderStopped):
                    _ = record.finish_pending_step(pending)
            self.assertTrue(month.read_bytes().endswith(b"\n"))
            self.assertTrue(pending.is_file())
            self.assertEqual(self.held.read_bytes(), self.output)
            self.assertEqual(self.peak.read_bytes(), self.peak_contents)

        # Both normal writers must be safe before the original step recovers.
        self.assertEqual(self.run_step(status=0, with_sources=False), 0)
        with chdir(self.fixture.repo), mock.patch.object(store, "host_name", return_value="original-host"):
            record.write_call(record_tests.START, {"tool": "fixture", "outcome": "ran"}, "intervening-call")
        before = self.complete_month_rows()
        self.assertEqual(sum(row["id"] == "intervening-call" for row in before), 1)
        normal_ids = [row["id"] for row in before if row["id"] != original["id"] and row["kind"] == "step"]
        self.assertEqual(len(normal_ids), 1)
        loads = json.loads
        scanned: list[bytes] = []

        def scan_under_lock(data: str | bytes | bytearray) -> object:
            if isinstance(data, bytes):
                with month.open("rb") as contender:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                scanned.append(data)
            return cast(object, loads(data))

        with mock.patch.object(json, "loads", side_effect=scan_under_lock):
            finished = record.finish_pending_step(pending)
        self.assertEqual(finished.added, not complete_object)
        self.assertTrue(scanned)
        rows = self.complete_month_rows()
        self.assertEqual(len(rows), 3)
        for identifier in (original["id"], *normal_ids, "intervening-call"):
            self.assertEqual(sum(row["id"] == identifier for row in rows), 1,
                             "each id must occupy exactly one physical JSON line")
        self.assertEqual(next(row for row in rows if row["id"] == original["id"]), original)
        self.assert_original_step(original, str(original["id"]))
        self.assertFalse(self.held.exists())
        self.assertFalse(self.peak.exists())
        self.assertFalse(pending.exists())
        self.assertTrue(pending.with_suffix(".done").is_file())
        self.assertEqual(index.update(), 3)
        contents = month.read_bytes()
        _ = launches.collect()
        self.assertEqual(self.run_step(), 0)
        self.assertEqual(month.read_bytes(), contents)
        self.assertEqual(index.update(), 0)

    def test_fragment_then_normal_step_and_call_recovers_one_physical_row_per_id(self) -> None:
        self.assert_short_append_recovers(complete_object=False, stop_after_repair=False)

    def test_complete_object_without_newline_then_other_writers_recovers_without_duplicate(self) -> None:
        self.assert_short_append_recovers(complete_object=True, stop_after_repair=False)

    def test_stop_after_fragment_repair_newline_recovers_without_losing_later_rows(self) -> None:
        self.assert_short_append_recovers(complete_object=False, stop_after_repair=True)

    def test_stop_after_complete_object_repair_newline_recovers_without_duplicate(self) -> None:
        self.assert_short_append_recovers(complete_object=True, stop_after_repair=True)

    def test_late_finish_refreshes_completion_before_rename_and_survives_a_stop(self) -> None:
        with mock.patch.object(record, "write_step_line", side_effect=OSError("fixture delayed publication")):
            self.assertEqual(self.run_step(), 0)
        paths = list(record.pending_steps_root().glob("*.json"))
        self.assertEqual(len(paths), 1)
        pending = paths[0]
        now = time.time()
        old = now - 3 * 24 * 60 * 60
        os.utime(pending, (old, old))
        replace = os.replace
        fsync = os.fsync
        synced_pending: list[int] = []

        def sync_before_completion(fd: int) -> None:
            if os.fstat(fd).st_ino == pending.stat().st_ino:
                self.assertGreaterEqual(pending.stat().st_mtime, now)
                synced_pending.append(fd)
            fsync(fd)

        def stop_after_completion(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
            replace(source, target)
            if Path(source) == pending and Path(target).suffix == ".done":
                self.assertTrue(synced_pending)
                raise RecorderStopped("after completion rename")

        with mock.patch.object(os, "replace", side_effect=stop_after_completion), \
             mock.patch.object(os, "fsync", side_effect=sync_before_completion):
            with self.assertRaises(RecorderStopped):
                _ = record.finish_pending_step(pending)

        completed = pending.with_suffix(".done")
        self.assertFalse(pending.exists())
        self.assertGreaterEqual(completed.stat().st_mtime, now)
        self.assertFalse(self.held.exists())
        self.assertFalse(self.peak.exists())
        rows = self.step_records()
        self.assertEqual(len(rows), 1)
        self.assert_original_step(rows[0], str(rows[0]["id"]))
        recovered = launches.collect()
        self.assertEqual((recovered.added, recovered.steps_added), (0, 0))
        self.assertTrue(completed.is_file())
        self.assertEqual(self.run_step(), 0)
        self.assertEqual(self.step_records(), rows)

    def test_identical_arguments_without_completion_proof_record_the_new_environment(self) -> None:
        self.assertEqual(self.run_step(), 0)
        first = self.step_records()[0]
        completed = list(record.pending_steps_root().glob("*.done"))
        self.assertEqual(len(completed), 1)
        self.assertEqual(self.fixture.handoff(), self.held)
        self.assertEqual(self.fixture.peak_file(), self.peak)
        # Recreated sources do not override durable completion proof.
        self.assertEqual(self.run_step(), 0)
        self.assertEqual(self.step_records(), [first])
        self.assertTrue(self.held.is_file())
        self.assertTrue(self.peak.is_file())
        completed[0].unlink()
        with mock.patch.dict(os.environ, {
            "CODEX_THREAD_ID": "second-step-worker", "BUILDLOG_CALL_ID": "second-call",
            "BUILDLOG_CALLER": "agent", "BUILDLOG_MEM_WAIT_S": "29",
        }):
            self.assertEqual(self.run_step(), 0)
        rows = self.step_records()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], first)
        self.assertNotEqual(rows[0]["id"], rows[1]["id"])
        self.assertEqual((rows[1]["session"], rows[1]["call_id"], rows[1]["caller"], rows[1]["mem_wait_s"]),
                         ("second-step-worker", "second-call", "agent", 29))
        self.assertEqual(len(self.saved_logs()), 2)
        self.assertFalse(self.held.exists())
        self.assertFalse(self.peak.exists())

    def test_absent_peak_records_fallback_once_then_replays_from_completion_marker(self) -> None:
        self.peak.unlink()
        self.assertEqual(self.run_step(status=0, with_held_log=False), 0)
        rows = self.step_records()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["slice"], "fallback")
        self.assertIsNone(rows[0]["peak_mem_bytes"])
        self.assertIsNone(rows[0]["log"])
        completed = list(record.pending_steps_root().glob("*.done"))
        self.assertEqual(len(completed), 1)
        self.assertEqual(list(record.pending_steps_root().glob("*.json")), [])
        self.assertEqual(report.recorder_health_lines("2026-10-02"), [])
        month = store.host_file("original-host", record_tests.START)
        original_bytes = month.read_bytes()
        self.assertEqual(self.run_step(status=0, with_held_log=False), 0)
        self.assertEqual(self.step_records(), rows)
        self.assertEqual(month.read_bytes(), original_bytes)
        self.assertTrue(completed[0].is_file())
        self.assertEqual(self.held.read_bytes(), self.output)
        self.assertFalse(self.peak.exists())

    def test_collect_expires_only_step_completion_markers_older_than_two_days(self) -> None:
        self.assertEqual(self.run_step(status=0), 0)
        completed = set(record.pending_steps_root().glob("*.done"))
        self.assertEqual(len(completed), 1)
        old = next(iter(completed))
        self.assertEqual(self.run_step(status=0, with_sources=False), 0)
        newer = set(record.pending_steps_root().glob("*.done")) - completed
        self.assertEqual(len(newer), 1)
        recent = next(iter(newer))
        now = record_tests.START
        old_time, recent_time = now - 3 * 24 * 60 * 60, now - 24 * 60 * 60
        os.utime(old, (old_time, old_time))
        os.utime(recent, (recent_time, recent_time))
        rows = self.step_records()
        self.assertEqual(len(rows), 2)
        self.assertEqual(report.recorder_health_lines("2026-10-02"), [])

        with mock.patch.object(time, "time", return_value=now):
            for attempt in range(2):
                with self.subTest(attempt=attempt):
                    recovered = launches.collect()
                    self.assertEqual((recovered.added, recovered.steps_added), (0, 0))
                    self.assertFalse(old.exists())
                    self.assertTrue(recent.is_file())
                    self.assertEqual(self.step_records(), rows)
                    self.assertEqual(report.recorder_health_lines("2026-10-02"), [])

    def test_steps_without_named_sources_are_new_events_despite_identical_arguments(self) -> None:
        self.assertEqual(self.run_step(status=0, with_sources=False), 0)
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "second-step-worker"}):
            self.assertEqual(self.run_step(status=0, with_sources=False), 0)
        rows = self.step_records()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["id"], rows[1]["id"])
        self.assertEqual([row["session"] for row in rows], ["original-step-worker", "second-step-worker"])
        self.assertEqual(self.held.read_bytes(), self.output)
        self.assertEqual(self.peak.read_bytes(), self.peak_contents)
        self.assertEqual(self.saved_logs(), [])

    def test_invalid_status_start_or_end_is_logged_and_removes_unfinishable_sources(self) -> None:
        for field in range(3):
            with self.subTest(field=("status", "start", "end")[field]):
                held, peak = self.fixture.handoff(), self.fixture.peak_file()
                arguments = ["1", str(record_tests.START), str(record_tests.START + 12.5)]
                arguments[field] = "invalid-recorder-number"
                result = self.fixture.record("step", *arguments, "0", str(held), str(peak), *record_tests.NEXTEST)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.step_records(), [])
                self.assertFalse(held.exists())
                self.assertFalse(peak.exists())
                self.assertEqual(list(record.pending_steps_root().glob("*.json")), [])
        errors = (self.fixture.root / store.ERRORS_NAME).read_text()
        self.assertEqual(sum(" record.py step " in line for line in errors.splitlines()), 3)
        self.assertIn("invalid-recorder-number", errors)

    def assert_health_line(self, pending: int, errors_pattern: str = "[1-9][0-9]*") -> str:
        lines = [line for line in report.recorder_health_lines("2026-10-02") if line]
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0],
                         rf"^{pending} records not published yet; {errors_pattern} recorder errors since .+$")
        rendered = self.report_text()
        self.assertEqual([line for line in rendered.splitlines() if "records not published yet" in line], lines)
        return lines[0]

    def test_launch_pending_save_failure_reaches_the_report_without_losing_its_source(self) -> None:
        fixture = self.launch_fixture()
        source = fixture.saved_launch(15803, "2026-10-02T12:00:00+00:00")
        original_source = source.read_bytes()
        replace = os.replace

        def refuse_pending_save(old: str | os.PathLike[str], new: str | os.PathLike[str]) -> None:
            if Path(new).parent == record.pending_launches_root():
                raise OSError("fixture pending save was refused")
            replace(old, new)

        with mock.patch.object(os, "replace", side_effect=refuse_pending_save):
            self.assertFalse(record.launch(source, fixture.worktree))
        self.assertEqual(source.read_bytes(), original_source)
        self.assertEqual(self.fixture.records(), [])
        self.assertEqual(list(record.pending_launches_root().glob("*.json")), [])
        errors = self.fixture.root / store.ERRORS_NAME
        self.assertIn("fixture pending save was refused", errors.read_text())
        first_line = self.assert_health_line(0, "1")
        self.assertEqual(self.assert_health_line(0, "1"), first_line)
        self.assertEqual(source.read_bytes(), original_source)

    def assert_persistent_publish_error_recovers(self, kind: PendingRecordKind) -> None:
        action: Callable[[], int | bool]
        if kind is PendingRecordKind.STEP:
            pending = record.pending_steps_root()
            publisher = store
            method = "append_locked"
            action = self.run_step
        else:
            fixture = self.launch_fixture()
            source = fixture.saved_launch(15803, "2026-10-02T12:00:00+00:00")
            pending = record.pending_launches_root()
            publisher = record
            method = "write_launch_line"
            action = lambda: record.launch(source, fixture.worktree)

        with mock.patch.object(publisher, method, side_effect=OSError(f"fixture {kind.value} publication failed")):
            self.assertFalse(action())
            for attempt in range(2):
                with self.subTest(attempt=attempt):
                    still_failing = launches.collect()
                    self.assertEqual((still_failing.added, still_failing.steps_added), (0, 0))
                    self.assertEqual(self.step_records() if kind is PendingRecordKind.STEP else self.fixture.records(), [])
                    self.assertEqual(len(list(pending.glob("*.json"))), 1)
                    _ = self.assert_health_line(1)
            if kind is PendingRecordKind.STEP:
                self.assertEqual(self.held.read_bytes(), self.output)
                self.assertEqual(self.peak.read_bytes(), self.peak_contents)

        errors = self.fixture.root / store.ERRORS_NAME
        error_history = errors.read_bytes()
        self.assertIn(f"fixture {kind.value} publication failed", error_history.decode())
        recovered = launches.collect()
        self.assertEqual((recovered.added, recovered.steps_added),
                         (0, 1) if kind is PendingRecordKind.STEP else (1, 0))
        rows = self.step_records() if kind is PendingRecordKind.STEP else self.fixture.records()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["step"], "nextest" if kind is PendingRecordKind.STEP else "launch")
        self.assertEqual(list(pending.glob("*.json")), [])
        if kind is PendingRecordKind.STEP:
            self.assertFalse(self.held.exists())
            self.assertFalse(self.peak.exists())
            self.assertEqual(len(self.saved_logs()), 1)
            self.assertEqual(gzip.decompress(self.saved_logs()[0].read_bytes()), self.output)
        # Resolving a pending record must not erase the recorder's error history.
        self.assertEqual(errors.read_bytes(), error_history)
        self.assertEqual(report.recorder_health_lines("2026-10-02"), [])
        self.assertNotIn("records not published yet", self.report_text())
        _ = self.fresh_cli("launches")
        self.assertEqual(self.fixture.records(), rows)
        self.assertEqual(report.recorder_health_lines("2026-10-02"), [])

    def test_step_publication_stays_visible_until_collect_recovers(self) -> None:
        self.assert_persistent_publish_error_recovers(PendingRecordKind.STEP)

    def test_launch_publication_stays_visible_until_collect_recovers(self) -> None:
        self.assert_persistent_publish_error_recovers(PendingRecordKind.LAUNCH)

    def test_clean_log_adds_no_recorder_health_line(self) -> None:
        self.assertEqual(report.recorder_health_lines("2026-10-02"), [])
        self.assertNotIn("records not published yet", self.report_text())
        rendered = self.fresh_cli("report", "2026-10-02")
        self.assertNotIn("records not published yet", rendered.stdout)
        self.assertFalse((self.fixture.root / store.ERRORS_NAME).exists())
        self.assertEqual(self.fixture.records(), [])

    def test_health_uses_the_users_local_midnight_and_existing_local_time_format(self) -> None:
        self.fixture.root.mkdir(parents=True, exist_ok=True)
        _ = (self.fixture.root / store.ERRORS_NAME).write_text("\n".join([
            "2026-10-02T03:59:59.999Z before the local report day",
            "2026-10-02T04:00:00.000Z within the local report day", "",
        ]))
        with mock.patch.dict(os.environ, {"USER_ZONE": "America/New_York"}):
            line = self.assert_health_line(0, "1")
        self.assertEqual(line,
                         "0 records not published yet; 1 recorder errors since 2026-10-02 00:00:00 EDT")

    def test_successful_step_removes_its_sources_without_saving_a_failure_log(self) -> None:
        self.assertEqual(self.run_step(status=0), 0)
        rows = self.step_records()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], 0)
        self.assertIsNone(rows[0]["log"])
        self.assertEqual(rows[0]["peak_mem_bytes"], 123456)
        self.assertFalse(self.held.exists())
        self.assertFalse(self.peak.exists())
        self.assertEqual(self.saved_logs(), [])
        self.assertEqual(list(record.pending_steps_root().glob("*.json")), [])
        _ = self.fresh_cli("launches")
        self.assertEqual(self.step_records(), rows)

    def test_repeating_completed_failed_step_keeps_one_row_and_one_saved_log(self) -> None:
        self.assertEqual(self.run_step(), 0)
        rows = self.step_records()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], 1)
        self.assertEqual(len(self.saved_logs()), 1)
        saved = self.saved_logs()[0].read_bytes()
        self.assertEqual(gzip.decompress(saved), self.output)

        self.assertEqual(self.run_step(), 0)
        self.assertEqual(self.step_records(), rows)
        self.assertEqual(len(self.saved_logs()), 1)
        self.assertEqual(self.saved_logs()[0].read_bytes(), saved)
        self.assertFalse(self.held.exists())
        self.assertFalse(self.peak.exists())
        self.assertEqual(list(record.pending_steps_root().glob("*.json")), [])
        _ = self.fresh_cli("launches")
        self.assertEqual(self.step_records(), rows)
