"""Real verification hold waits: wait."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

from scripts.direct import progress_history
# Stop this private publication boundary without a production fault switch.
from scripts.direct.progress_history import _publish_wait_file  # pyright: ignore[reportPrivateUsage]
from scripts.lib import process_start
from scripts.lib.process_start_test_support import write_stat
from scripts.direct.recorder_fixture import invoke_recorder
from scripts.direct.verify_hold_wait_test_support import (
    TEST_HANG_GUARD_SECS,
    VerifyHoldWaitFixture,
    WaitObservation,
    grant_call,
    read_cycle_file as read_cycle_file,
    release_registered_call as release_registered_call,
)


class VerifyHoldWaitTests(VerifyHoldWaitFixture):
    def test_overlapping_turn_declarations_leave_one_recoverable_wait(self) -> None:
        history = self.record_phase()
        control = self.pause_publication("before-file")
        session = Path(self.fixture.environment["UNIT_RUN_DIR"])
        callers: list[subprocess.Popen[str]] = []
        for mode in ("--record-turn-wait", "--observe-turn-wait"):
            caller = subprocess.Popen(
                [str(control.interpreter), mode, str(session), "fixture-director", "decision: approve trial"],
                cwd=self.fixture.root, env=self.fixture.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, start_new_session=True,
            )
            self.addCleanup(self.stop_process, caller)
            callers.append(caller)
            self.wait_for_call(caller, "wait publication paused: before-file" if len(callers) == 1 else "turn wait lock:")
        first, second = callers
        admitted = "turn wait lock: acquired" in self.wait_diagnostics[second.pid]
        try:
            if admitted:
                code, output = self.finish(second)
                self.assertEqual(code, 0, output)
        finally:
            control.continue_child()
        code, output = self.finish(first)
        self.assertEqual(code, 0, output)
        if not admitted:
            code, output = self.finish(second)
            self.assertEqual(code, 0, output)
        with mock.patch.dict(os.environ, self.fixture.environment):
            progress_history.finish_turn_wait(session, "fixture-director", source="TYPED", prompt="Approved.")
        self.assertEqual([event["event_type"] for event in self.wait_events(history)
                          if str(event["event_type"]).startswith("wait_")], ["wait_started", "wait_finished"])
        self.assertFalse((session / "turn_wait.json").exists())

    def test_recovery_abandons_a_wait_published_after_its_file_scan_and_removes_the_folder(self) -> None:
        history = self.record_phase()
        session = Path(self.fixture.environment["UNIT_RUN_DIR"])
        directory = self.recovery_root() / f"verify-hold.{self.stopped_owner_pid()}.late-publication"
        directory.mkdir(parents=True)
        _ = (directory / "seconds").write_text("0\n")
        marker = directory / "phase-wait.late.hold"
        ids: list[str] = []

        def recover_before_publication(publication: progress_history.WaitIdFile | progress_history.TurnWaitFile,
                                       wait_id: str) -> None:
            self.assertEqual(publication.path, marker)
            ids.append(wait_id)
            self.assertEqual(self.publish_marker_during_cleanup(directory, wait_id), marker)
            result = self.fixture.verify("test", "sample", "--no-cache", "--local", "late publication", "--filter", "smoke")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        args = argparse.Namespace(session_dir=str(session), kind="WaitingForMemoryOrSlot", reason="build hold",
                                  worker_thread_id="thread-impl", owner_session="thread-impl", unit="")
        with mock.patch.dict(os.environ, self.fixture.environment), mock.patch.object(
            progress_history, "_publish_wait_file", recover_before_publication
        ), self.assertRaises(SystemExit) as abandoned:
            _ = progress_history.publish_wait(session, args, progress_history.WaitIdFile(marker))
        self.assertEqual(abandoned.exception.code, 5)
        self.assertFalse(directory.exists())
        wait_id, = ids
        self.assertEqual([row["event_type"] for row in self.wait_events(history) if row.get("wait_id") == wait_id],
                         ["wait_abandoned"])
        self.assertFalse(any(row["wait_id"] == wait_id for row in cast(list[dict[str, object]], self.waits_report()["waits"])))

    def test_recovery_removes_a_dead_writers_atomic_temp_file_before_recording_later_waits(self) -> None:
        history = self.record_phase()
        directory = self.recovery_root() / f"verify-hold.{self.stopped_owner_pid()}.temporary"
        directory.mkdir(parents=True)
        temporary = directory / ".phase-wait.stopped.hold.partial"
        _ = temporary.write_text("incomplete")
        self.hold()
        caller = self.start("test", "sample", "--no-cache", "--local", "stale publication", "--filter", "smoke")
        self.wait_for_call(caller)
        grant_call(self.cycle_file)
        code, output = self.finish(caller)
        self.assertEqual(code, 0, output)
        self.assertFalse(directory.exists())
        starts = [row for row in self.wait_events(history) if row["event_type"] == "wait_started"]
        wait, = starts
        self.assertEqual(wait["wait_reason"], "build hold")
        self.assertEqual(len([row for row in self.wait_events(history)
                              if row["event_type"] == "wait_finished" and row["wait_id"] == wait["wait_id"]]), 1)
        self.assertNotIn("NotRecorded", output)

    def test_publication_after_deletion_snapshot_cannot_restore_a_retired_wait_folder(self) -> None:
        history = self.record_phase()
        session = Path(self.fixture.environment["UNIT_RUN_DIR"])
        directory = self.recovery_root() / f"verify-hold.{self.stopped_owner_pid()}.snapshot"
        directory.mkdir(parents=True)
        _ = (directory / "owner.json").write_text("{}")
        marker = directory / "phase-wait.late.hold"
        publish = _publish_wait_file

        def publish_during_delete(publication: progress_history.WaitIdFile | progress_history.TurnWaitFile,
                                  wait_id: str) -> None:
            failures: list[OSError] = []

            def late_publication() -> None:
                try:
                    publish(publication, wait_id)
                except OSError as error:
                    failures.append(error)

            with self.after_wait_owner_deleted(directory, late_publication):
                result = invoke_recorder(["recover-wait-folder", "--session-dir", str(session),
                                          "--directory", str(directory)], self.fixture.environment, self.fixture.root)
            self.assertEqual(result.returncode, 0, result.stderr)
            if failures:
                raise failures[0]

        args = argparse.Namespace(session_dir=str(session), kind="WaitingForMemoryOrSlot", reason="build hold", unit="",
                                  worker_thread_id="thread-impl", owner_session="thread-impl")
        with mock.patch.dict(os.environ, self.fixture.environment), mock.patch.object(
            progress_history, "_publish_wait_file", publish_during_delete
        ), self.assertRaises(OSError):
            _ = progress_history.publish_wait(session, args, progress_history.WaitIdFile(marker))
        self.assertFalse(directory.exists())
        self.assertFalse(any(row["event_type"] == "wait_started" for row in self.wait_events(history)))
        self.assertEqual(self.waits_report()["waits"], [])

    def test_partial_retired_folder_deletion_cannot_leave_an_open_wait_without_an_owner(self) -> None:
        history = self.record_phase()
        session = Path(self.fixture.environment["UNIT_RUN_DIR"])
        directory = self.recovery_root() / f"verify-hold.{self.stopped_owner_pid()}.partial"
        directory.mkdir(parents=True)
        _ = (directory / "owner.json").write_text("{}")
        marker = directory / "phase-wait.late.hold"
        publish = _publish_wait_file
        ids: list[str] = []

        def publish_before_partial_delete(publication: progress_history.WaitIdFile | progress_history.TurnWaitFile,
                                          wait_id: str) -> None:
            ids.append(wait_id)
            rename = os.rename
            remove = shutil.rmtree

            def publish_before_rename(source: Path, target: Path) -> None:
                if source == directory:
                    publish(publication, wait_id)
                rename(source, target)

            def delete_after_publication(path: Path) -> None:
                # On the old path no rename occurs; publish after the history
                # scan at its corresponding boundary, before recursive delete.
                if path == directory:
                    publish(publication, wait_id)
                remove(path)

            def stop_after_owner() -> None:
                raise OSError("interrupted after deleting owner.json")

            with self.after_wait_owner_deleted(directory, stop_after_owner), mock.patch.object(
                os, "rename", publish_before_rename
            ), mock.patch.object(shutil, "rmtree", delete_after_publication):
                result = invoke_recorder(["recover-wait-folder", "--session-dir", str(session),
                                          "--directory", str(directory)], self.fixture.environment, self.fixture.root)
            self.assertNotEqual(result.returncode, 0)

        args = argparse.Namespace(session_dir=str(session), kind="WaitingForMemoryOrSlot", reason="build hold", unit="",
                                  worker_thread_id="thread-impl", owner_session="thread-impl")
        with mock.patch.dict(os.environ, self.fixture.environment), mock.patch.object(
            progress_history, "_publish_wait_file", publish_before_partial_delete
        ), self.assertRaises(SystemExit) as abandoned:
            _ = progress_history.publish_wait(session, args, progress_history.WaitIdFile(marker))
        self.assertEqual(abandoned.exception.code, 5)
        self.assertFalse(directory.exists())
        retired, = self.recovery_root().glob(".verify-hold-removing.*")
        self.assertFalse((retired / "owner.json").exists())
        wait_id, = ids
        self.assertEqual([row["event_type"] for row in self.wait_events(history) if row.get("wait_id") == wait_id],
                         ["wait_abandoned"])
        with self.unavailable_wait_history(history):
            result = self.fixture.verify("test", "sample", "--no-cache", "--local", "partial delete", "--filter", "smoke")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(retired.exists())
        self.assertEqual(self.waits_report()["waits"], [])

    def test_rerun_finishes_deletion_interrupted_after_retirement_without_reading_history(self) -> None:
        history = self.record_phase()
        session = Path(self.fixture.environment["UNIT_RUN_DIR"])
        wait_id = self.start_saved_wait()
        directory = self.recovery_root() / f"verify-hold.{self.stopped_owner_pid()}.retirement"
        directory.mkdir(parents=True)
        _ = (directory / "owner.json").write_text("{}")
        _ = (directory / "phase-wait.stopped.hold").write_text(wait_id + "\n")
        with mock.patch.object(shutil, "rmtree", side_effect=OSError("stopped before deletion")):
            result = invoke_recorder(["recover-wait-folder", "--session-dir", str(session), "--directory", str(directory)],
                                     self.fixture.environment, self.fixture.root)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(directory.exists())
        retired, = self.recovery_root().glob(".verify-hold-removing.*")
        self.assertEqual((retired / "phase-wait.stopped.hold").read_text().strip(), wait_id)
        self.assertEqual([row["event_type"] for row in self.wait_events(history) if row.get("wait_id") == wait_id],
                         ["wait_started", "wait_finished"])
        # The retired folder is no longer an owner record or a history input.
        _ = (retired / "owner.json").write_text(json.dumps({"pid": os.getpid(), "start_time": "unknown"}))
        with self.unavailable_wait_history(history):
            result = self.fixture.verify("test", "sample", "--no-cache", "--local", "retired delete", "--filter", "smoke")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(retired.exists())

    def test_failed_folder_recovery_keeps_owner_identity_for_a_replaced_pid_retry(self) -> None:
        history = self.record_phase()
        start = process_start.process_start(os.getpid())
        if not isinstance(start, process_start.ProcessStartTime):
            self.skipTest("fake proc checks need Linux ticks")
        wait_id = self.start_saved_wait()
        directory = self.recovery_root() / f"verify-hold.{os.getpid()}.failed-close"
        directory.mkdir(parents=True)
        owner = directory / "owner.json"
        owner_text = json.dumps({"pid": os.getpid(), "start_time": start.text})
        _ = owner.write_text(owner_text)
        marker = directory / "phase-wait.stopped.hold"
        _ = (directory / "seconds").write_text("0\n")
        proc_root = self.fixture.root / "replaced-owner-proc"
        proc_root.mkdir()
        observer = self.fixture.root / "replaced-owner-reader"
        self.fixture.write_executable(observer, f'''import os, sys
if len(sys.argv) > 2 and sys.argv[1] == "-c" and 'owner.json' in sys.argv[2]:
    os.environ["VERIFY_PROC_ROOT"] = {str(proc_root)!r}
else:
    os.environ.pop("VERIFY_PROC_ROOT", None)
os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])
''')
        self.fixture.environment["CLAUDE_PYTHON"] = str(observer)
        # An empty marker cannot be closed. Recovery must keep the whole folder.
        _ = marker.write_text("")
        first = self.fixture.verify("test", "sample", "--no-cache", "--local", "failed close", "--filter", "smoke")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertTrue(marker.exists())
        self.assertTrue(owner.is_file(), "Failed recovery removed the recorded owner identity")
        self.assertEqual(owner.read_text(), owner_text)
        self.assertIn("NotRecorded", first.stderr)
        _ = marker.write_text(wait_id + "\n")
        _ = write_stat(proc_root, os.getpid(), start_ticks=start.ticks + 1)
        self.recover_saved_wait_after_failed_read(marker, history, wait_id)
        self.assertFalse(directory.exists())

    def test_rerun_abandons_a_wait_published_before_its_start_event(self) -> None:
        self.assert_stopped_publication_recovers("before-event")

    def test_rerun_closes_a_published_start_event_once(self) -> None:
        self.assert_stopped_publication_recovers("after-event")

    def test_rerun_keeps_a_wait_file_when_its_history_cannot_be_read(self) -> None:
        history = self.record_phase()
        wait_id = self.start_saved_wait()
        directory = self.recovery_root() / f"verify-hold.{self.stopped_owner_pid()}.history-unavailable"
        directory.mkdir(parents=True)
        marker = directory / "phase-wait.stopped.hold"
        _ = marker.write_text(wait_id + "\n")
        with self.unavailable_wait_history(history):
            result = self.fixture.verify("test", "sample", "--no-cache", "--local", "unreadable history",
                                         "--filter", "smoke")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(marker.read_text().strip(), wait_id)
            self.assertIn("unavailable", result.stderr.lower())
            self.assertIn(wait_id, result.stderr)
        interval, = [row for row in cast(list[dict[str, object]], self.waits_report()["waits"])
                     if row["wait_id"] == wait_id]
        self.assertEqual(cast(dict[str, object], interval["wait_end"])["state"], "Open")
        self.recover_saved_wait_after_failed_read(marker, history, wait_id)

    def test_a_recorder_child_cannot_start_an_id_abandoned_after_its_shell_dies(self) -> None:
        history = self.record_phase()
        control = self.pause_publication("before-lock")
        self.hold()
        first = self.start("test", "sample", "--no-cache", "--local", "late recorder", "--filter", "smoke")
        self.wait_for_call(first, "wait publication paused: before-lock")
        marker = self.saved_publication_file()
        wait_id = marker.read_text().strip()
        os.kill(first.pid, signal.SIGKILL)
        _ = first.wait(timeout=TEST_HANG_GUARD_SECS)
        self.recover_publication()
        control.continue_child()
        code, output = self.finish(first)
        self.assertEqual(code, -signal.SIGKILL, output)
        self.assertIn("abandoned", output.lower())
        self.assertFalse(marker.exists())
        events = [row for row in self.wait_events(history) if row.get("wait_id") == wait_id]
        self.assertFalse(any(row["event_type"] == "wait_started" for row in events), events)
        self.assertFalse(any(row["wait_id"] == wait_id for row in cast(list[dict[str, object]], self.waits_report()["waits"])))

    def test_the_next_turn_closes_a_wait_interrupted_after_its_event(self) -> None:
        history = self.record_phase()
        control = self.pause_publication("after-event")
        session = Path(self.fixture.environment["UNIT_RUN_DIR"])
        process = subprocess.Popen(
            [str(control.interpreter), "--record-turn-wait", str(session), "fixture-director", "decision: approve trial"],
            cwd=self.fixture.root, env=self.fixture.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
        self.addCleanup(self.stop_process, process)
        self.wait_for_call(process, "wait publication paused: after-event")
        marker = session / "turn_wait.json"
        self.assertTrue(marker.exists(), "A declared wait needs its recovery marker before the start event")
        started, = [row for row in self.wait_events(history) if row["event_type"] == "wait_started"]
        self.assertEqual(cast(dict[str, object], json.loads(marker.read_text()))["wait_id"], started["wait_id"])
        self.stop_process(process)
        with mock.patch.dict(os.environ, self.fixture.environment):
            progress_history.finish_turn_wait(session, "fixture-director", source="TYPED", prompt="Approved.")
            progress_history.finish_turn_wait(session, "fixture-director", source="TYPED", prompt="Approved again.")
        events = [row for row in self.wait_events(history) if row.get("wait_id") == started["wait_id"]]
        self.assertEqual([row["event_type"] for row in events], ["wait_started", "wait_finished"])
        self.assertFalse(marker.exists())
        intervals = cast(list[dict[str, object]], self.waits_report()["waits"])
        self.assertEqual(len(intervals), 1)
        self.assertEqual(cast(dict[str, object], intervals[0]["wait_end"])["state"], "Finished")

    def recovery_root(self) -> Path:
        session = Path(self.fixture.environment["UNIT_RUN_DIR"])
        tree_key = hashlib.sha256(str(self.fixture.root.resolve()).encode()).hexdigest()
        return session / "verify_waits" / tree_key

    def stopped_owner_pid(self) -> int:
        with subprocess.Popen([sys.executable, "-c", "pass"]) as owner:
            _ = owner.wait(timeout=TEST_HANG_GUARD_SECS)
            return owner.pid

    def start_saved_wait(self) -> str:
        session = Path(self.fixture.environment["UNIT_RUN_DIR"])
        result = invoke_recorder(
            ["start-wait", "--session-dir", str(session), "--worker-thread-id", "thread-impl",
             "--kind", "WaitingForMemoryOrSlot", "--reason", "build hold"],
            self.fixture.environment, self.fixture.root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_wait_recovery_keeps_unknown_owners_and_reclaims_only_proven_stale_ones(self) -> None:
        history = self.record_phase()
        start = process_start.process_start(os.getpid())
        if not isinstance(start, process_start.ProcessStartTime):
            self.skipTest("fake proc checks need Linux ticks")
        for outcome in ("unknown", "replaced", "gone"):
            with self.subTest(outcome=outcome):
                wait_id = self.start_saved_wait()
                directory = self.recovery_root() / f"verify-hold.{os.getpid()}.{outcome}"
                directory.mkdir(parents=True)
                saved = start.ticks + 1 if outcome == "unknown" else start.ticks
                _ = (directory / "owner.json").write_text(json.dumps({
                    "pid": os.getpid(), "start_time": str(saved),
                }))
                _ = (directory / "phase-wait.stopped.hold").write_text(wait_id + "\n")
                proc_root = self.fixture.root / f"proc-{outcome}"
                proc_root.mkdir()
                if outcome == "unknown":
                    (proc_root / str(os.getpid()) / "stat").mkdir(parents=True)
                elif outcome == "replaced":
                    _ = write_stat(proc_root, os.getpid(), start_ticks=start.ticks + 1)
                observer = self.fixture.root / "wait-owner-proc-root"
                self.fixture.write_executable(observer, f'''import os, sys
if len(sys.argv) > 2 and sys.argv[1] == "-c" and 'owner.json' in sys.argv[2]:
    os.environ["VERIFY_PROC_ROOT"] = {str(proc_root)!r}
else:
    os.environ.pop("VERIFY_PROC_ROOT", None)
os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])
''')
                result = self.fixture.verify("test", "sample", "--no-cache", "--local", "owner recovery",
                                             "--filter", "smoke", extra={"CLAUDE_PYTHON": str(observer)})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
                finishes = [row for row in rows if row["event_type"] == "wait_finished" and row["wait_id"] == wait_id]
                if outcome == "unknown":
                    self.assertTrue(directory.is_dir())
                    self.assertEqual(finishes, [])
                else:
                    self.assertFalse(directory.exists())
                    self.assertEqual(len(finishes), 1)

    def test_a_scan_leaves_an_unpublished_calls_folder_alone(self) -> None:
        history = self.record_phase()
        ready = self.fixture.root / "unpublished-folder"
        resume = self.fixture.root / "resume-publication"
        os.mkfifo(resume)
        observer = self.fixture.root / "pause-owner-publication"
        self.fixture.write_executable(observer, f'''import os, sys
from pathlib import Path
if len(sys.argv) > 4 and sys.argv[1] == "-c" and sys.argv[3] == "record":
    Path({str(ready)!r}).write_text(sys.argv[4])
    print("owner publication paused", file=sys.stderr, flush=True)
    with open({str(resume)!r}) as release:
        release.read(1)
os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])
''')
        self.fixture.environment["CLAUDE_PYTHON"] = str(observer)
        arguments = ("test", "sample", "--no-cache", "--local", "publication", "--filter", "smoke")
        first = self.start(*arguments)
        self.wait_for_call(first, "owner publication paused")
        unpublished = Path(ready.read_text())
        self.fixture.environment["CLAUDE_PYTHON"] = sys.executable
        try:
            scanner = self.fixture.verify(*arguments)
            kept = unpublished.is_dir()
            published_early = list(self.recovery_root().glob(f"verify-hold.{first.pid}.*"))
            before = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
            self.hold()
        finally:
            with resume.open("w") as release:
                _ = release.write("resume")
        if kept:
            self.wait_for_call(first)
            grant_call(self.cycle_file)
        code, output = self.finish(first)
        self.assertEqual(scanner.returncode, 0, scanner.stdout + scanner.stderr)
        self.assertTrue(kept, "the concurrent scan removed an unpublished live call")
        self.assertEqual(published_early, [], "the call published its folder before its owner")
        self.assertFalse(any(row["event_type"] == "wait_started" for row in before))
        self.assertEqual(code, 0, output)
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        started, = [row for row in rows if row["event_type"] == "wait_started" and row["wait_reason"] == "build hold"]
        self.assertEqual(len([row for row in rows if row["event_type"] == "wait_finished"
                              and row["wait_id"] == started["wait_id"]]), 1, rows)
        self.assertFalse(unpublished.exists())

    def test_rerun_recovers_legacy_waits_only_after_the_owner_exits(self) -> None:
        history = self.record_phase()
        arguments = ("test", "sample", "--no-cache", "--local", "legacy owner", "--filter", "smoke")
        for owner_state in ("missing", "empty", "unreadable"):
            for retry in (False, True):
                with self.subTest(owner_state=owner_state, retry=retry):
                    wait_id = self.start_saved_wait()
                    owner = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                             stdin=subprocess.PIPE, text=True)
                    try:
                        directory = self.recovery_root() / f"verify-hold.{owner.pid}.legacy"
                        directory.mkdir(parents=True)
                        wait_file = directory / "phase-wait.stopped.hold"
                        _ = wait_file.write_text(wait_id + "\n")
                        if owner_state == "empty":
                            _ = (directory / "owner.json").write_text("")
                        elif owner_state == "unreadable":
                            (directory / "owner.json").mkdir()
                        if retry:
                            (directory / "retry").touch()
                        live_result = self.fixture.verify(*arguments)
                        kept = wait_file.exists()
                        live_rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
                    finally:
                        _ = owner.communicate(input="stop", timeout=TEST_HANG_GUARD_SECS)
                    dead_result = self.fixture.verify(*arguments)
                    rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
                    self.assertEqual(live_result.returncode, 0, live_result.stdout + live_result.stderr)
                    self.assertTrue(kept, "an incomplete owner record does not prove that its pid is gone")
                    self.assertFalse(any(row["event_type"] == "wait_finished" and row["wait_id"] == wait_id
                                         for row in live_rows))
                    self.assertEqual(dead_result.returncode, 0, dead_result.stdout + dead_result.stderr)
                    self.assertFalse(directory.exists())
                    self.assertEqual(len([row for row in rows if row["event_type"] == "wait_finished"
                                          and row["wait_id"] == wait_id]), 1, rows)

    def test_a_scan_removes_only_staging_folders_with_a_known_dead_owner(self) -> None:
        history = self.record_phase()
        root = self.recovery_root()
        root.mkdir(parents=True)
        dead_pid = self.stopped_owner_pid()
        start = process_start.process_start(os.getpid())
        self.assertIsInstance(start, process_start.ProcessStartTime)
        if not isinstance(start, process_start.ProcessStartTime):
            self.fail("this staging test needs Linux ticks")
        start_time = start.text
        dead = root / f".verify-hold-new.{dead_pid}.dead"
        reused = root / f".verify-hold-new.{os.getpid()}.reused"
        live = root / f".verify-hold-new.{os.getpid()}.live"
        ownerless = root / f".verify-hold-new.{dead_pid}.ownerless"
        for directory, pid, identity in ((dead, dead_pid, "stopped"),
                                         (reused, os.getpid(), str(start.ticks + 1)),
                                         (live, os.getpid(), start_time)):
            directory.mkdir()
            _ = (directory / "owner.json").write_text(json.dumps({"pid": pid, "start_time": identity}))
        ownerless.mkdir()
        result = self.fixture.verify("test", "sample", "--no-cache", "--local", "staging recovery", "--filter", "smoke")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(dead.exists())
        self.assertFalse(reused.exists())
        self.assertTrue(live.is_dir())
        self.assertTrue(ownerless.is_dir())
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        self.assertFalse(any(row["event_type"] == "wait_started" for row in rows))

    def test_a_wait_file_removed_by_another_scanner_is_already_recovered(self) -> None:
        history = self.record_phase()
        wait_id = self.start_saved_wait()
        directory = self.recovery_root() / f"verify-hold.{self.stopped_owner_pid()}.stopped"
        directory.mkdir(parents=True)
        wait_file = directory / "phase-wait.stopped.hold"
        _ = wait_file.write_text(wait_id + "\n")
        removed = self.remove_wait_after_recovery_scan(wait_file, wait_id)
        result = self.fixture.verify("test", "sample", "--no-cache", "--local", "concurrent recovery", "--filter", "smoke")
        self.assertEqual(removed.read_text(), "removed")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("No such file", result.stdout + result.stderr)
        self.assertNotIn("NotRecorded", result.stdout + result.stderr)
        self.assertFalse(directory.exists())
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        self.assertEqual(len([row for row in rows if row["event_type"] == "wait_finished"
                              and row["wait_id"] == wait_id]), 1, rows)

    def test_rerun_removes_incomplete_owner_folders_without_wait_ids(self) -> None:
        history = self.record_phase()
        session = Path(self.fixture.environment["UNIT_RUN_DIR"])
        tree_key = hashlib.sha256(str(self.fixture.root.resolve()).encode()).hexdigest()
        dead_pid = self.stopped_owner_pid()
        arguments = ("test", "sample", "--no-cache", "--local", "owner recovery", "--filter", "smoke")
        for owner_state in ("empty", "missing", "unreadable"):
            with self.subTest(owner_state=owner_state):
                abandoned = session / "verify_waits" / tree_key / f"verify-hold.{dead_pid}.stopped-{owner_state}"
                abandoned.mkdir(parents=True)
                if owner_state == "empty":
                    _ = (abandoned / "owner.json").write_text("")
                elif owner_state == "unreadable":
                    (abandoned / "owner.json").mkdir()
                before = history.read_text().splitlines()
                self.hold()
                process = self.start(*arguments)
                self.wait_for_call(process)
                grant_call(self.cycle_file)
                code, output = self.finish(process)
                self.assertEqual(code, 0, output)
                self.assertFalse(abandoned.exists())
                rows = [cast(dict[str, object], json.loads(line))
                        for line in history.read_text().splitlines()[len(before):]]
                started, = [row for row in rows if row["event_type"] == "wait_started"
                            and row["wait_reason"] == "build hold"]
                self.assertEqual(len([row for row in rows if row["event_type"] == "wait_finished"
                                      and row["wait_id"] == started["wait_id"]]), 1, rows)
                self.assertNotIn("an earlier wait still needs to close", output)

    def test_rerun_closes_saved_waits_with_incomplete_owner_records(self) -> None:
        history = self.record_phase()
        session = Path(self.fixture.environment["UNIT_RUN_DIR"])
        tree_key = hashlib.sha256(str(self.fixture.root.resolve()).encode()).hexdigest()
        dead_pid = self.stopped_owner_pid()
        arguments = ("test", "sample", "--no-cache", "--local", "owner recovery", "--filter", "smoke")
        for owner_state in ("empty", "missing", "unreadable"):
            with self.subTest(owner_state=owner_state):
                result = invoke_recorder(
                    ["start-wait", "--session-dir", str(session), "--worker-thread-id", "thread-impl",
                     "--kind", "WaitingForMemoryOrSlot", "--reason", "build hold"],
                    self.fixture.environment, self.fixture.root,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                old_wait_id = result.stdout.strip()
                abandoned = session / "verify_waits" / tree_key / f"verify-hold.{dead_pid}.stopped-{owner_state}"
                abandoned.mkdir(parents=True)
                _ = (abandoned / "phase-wait.stopped.hold").write_text(old_wait_id + "\n")
                if owner_state == "empty":
                    _ = (abandoned / "owner.json").write_text("")
                elif owner_state == "unreadable":
                    (abandoned / "owner.json").mkdir()
                before = history.read_text().splitlines()
                self.hold()
                process = self.start(*arguments)
                self.wait_for_call(process)
                grant_call(self.cycle_file)
                code, output = self.finish(process)
                self.assertEqual(code, 0, output)
                self.assertFalse(abandoned.exists())
                rows = [cast(dict[str, object], json.loads(line))
                        for line in history.read_text().splitlines()[len(before):]]
                old_finish, = [index for index, row in enumerate(rows)
                               if row["event_type"] == "wait_finished" and row["wait_id"] == old_wait_id]
                new_start, = [index for index, row in enumerate(rows)
                              if row["event_type"] == "wait_started" and row["wait_reason"] == "build hold"]
                self.assertLess(old_finish, new_start)
                self.assertNotIn("an earlier wait still needs to close", output)

    def test_rerun_closes_the_failed_resource_wait_before_starting_its_own(self) -> None:
        history = self.record_phase()
        fault = self.fixture.root / "fail-wait-close"
        _ = fault.write_text("fail")
        observer = self.fixture.root / "wait-close-observer"
        self.fixture.write_executable(observer, f'''import os, sys
from pathlib import Path
if len(sys.argv) > 2 and sys.argv[1].endswith("/progress_history.py") and sys.argv[2] in ("finish-wait", "recover-wait-folder") and Path({str(fault)!r}).exists():
    raise SystemExit(17)
os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])
''')
        self.fixture.environment["CLAUDE_PYTHON"] = str(observer)
        arguments = ("test", "sample", "--no-cache", "--local", "wait recovery", "--filter", "smoke")
        self.hold()
        first = self.start(*arguments)
        self.wait_for_call(first)
        grant_call(self.cycle_file)
        code, output = self.finish(first)
        self.assertEqual(code, 0, output)
        self.assertIn("cannot close", output)
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        old_wait, = [row for row in rows if row["event_type"] == "wait_started"]
        self.assertFalse(any(row["event_type"] == "wait_finished" for row in rows))

        fault.unlink()
        self.hold()
        second = self.start(*arguments)
        self.wait_for_call(second)
        grant_call(self.cycle_file)
        code, output = self.finish(second)
        self.assertEqual(code, 0, output)
        self.assertNotIn("cannot close", output)
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        old_finishes = [index for index, row in enumerate(rows)
                        if row["event_type"] == "wait_finished" and row["wait_id"] == old_wait["wait_id"]]
        self.assertEqual(len(old_finishes), 1, rows)
        new_start, = [index for index, row in enumerate(rows)
                      if row["event_type"] == "wait_started" and row["wait_id"] != old_wait["wait_id"]]
        self.assertLess(old_finishes[0], new_start)
        new_wait = rows[new_start]
        self.assertEqual(len([row for row in rows if row["event_type"] == "wait_finished"
                              and row["wait_id"] == new_wait["wait_id"]]), 1)

    def test_rerun_recovers_a_wait_when_its_owner_dies_before_cleanup(self) -> None:
        history = self.record_phase()
        victim = self.fixture.root / "wait-owner-to-kill"
        observer = self.fixture.root / "stop-during-wait-close"
        self.fixture.write_executable(observer, f'''import os, signal, sys
from pathlib import Path
victim = Path({str(victim)!r})
if len(sys.argv) > 2 and sys.argv[1].endswith("/progress_history.py") and sys.argv[2] == "finish-wait" and victim.exists():
    os.kill(int(victim.read_text()), signal.SIGKILL)
    raise SystemExit(17)
os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])
''')
        self.fixture.environment["CLAUDE_PYTHON"] = str(observer)
        arguments = ("test", "sample", "--no-cache", "--local", "wait recovery", "--filter", "smoke")
        self.hold()
        first = self.start(*arguments)
        self.wait_for_call(first)
        _ = victim.write_text(str(first.pid))
        grant_call(self.cycle_file)
        code, output = self.finish(first)
        self.assertEqual(code, -9, output)
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        old_wait, = [row for row in rows if row["event_type"] == "wait_started"]
        self.assertFalse(any(row["event_type"] == "wait_finished" for row in rows))
        session = Path(self.fixture.environment["UNIT_RUN_DIR"])
        old_file, = list((session / "verify_waits").glob("*/verify-hold.*/phase-wait.*"))
        self.assertEqual(old_file.read_text().strip(), old_wait["wait_id"])
        self.assertFalse((old_file.parent / "retry").exists())

        victim.unlink()
        self.hold()
        second = self.start(*arguments)
        self.wait_for_call(second)
        grant_call(self.cycle_file)
        code, output = self.finish(second)
        self.assertEqual(code, 0, output)
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        old_finishes = [index for index, row in enumerate(rows)
                        if row["event_type"] == "wait_finished" and row["wait_id"] == old_wait["wait_id"]]
        self.assertEqual(len(old_finishes), 1, rows)
        new_start, = [index for index, row in enumerate(rows)
                      if row["event_type"] == "wait_started" and row["wait_id"] != old_wait["wait_id"]]
        self.assertLess(old_finishes[0], new_start)
        self.assertFalse(old_file.parent.exists())

    def test_a_second_call_leaves_a_live_owners_wait_open(self) -> None:
        history = self.record_phase()
        arguments = ("test", "sample", "--no-cache", "--local", "live wait owner", "--filter", "smoke")
        self.hold()
        first = self.start(*arguments)
        self.wait_for_call(first)
        session = Path(self.fixture.environment["UNIT_RUN_DIR"])
        live_file, = list((session / "verify_waits").glob("*/verify-hold.*/phase-wait.*"))
        # Cleanup can mark a retry before its owner exits. A concurrent call
        # must still leave that live owner's wait alone.
        (live_file.parent / "retry").touch()
        self.fixture.environment.update({"UNIT_TEAM_ROLE": "test", "CODEX_THREAD_ID": "thread-test"})
        result = invoke_recorder(
            ["start-pass", "--session-dir", str(session), "--phase-id", "6", "--pass-kind", "test",
             "--activity", "verify", "--called-task", "direct.test", "--called-family", "codex",
             "--called-model", "fixture", "--worker-name", "fixture-test"],
            self.fixture.environment, self.fixture.root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        _ = (session / "mesh_roster.json").write_text(json.dumps({
            "fixture-impl": {"thread_id": "thread-impl"}, "fixture-test": {"thread_id": "thread-test"}}))
        second = self.start(*arguments)
        self.wait_for_call(second)
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        hold_waits = [row["wait_id"] for row in rows if row["event_type"] == "wait_started"]
        self.assertEqual(len(hold_waits), 2)
        self.assertFalse(any(row["event_type"] == "wait_finished" for row in rows))
        grant_call(self.cycle_file)
        for process in (first, second):
            code, output = self.finish(process)
            self.assertEqual(code, 0, output)
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        for wait_id in hold_waits:
            self.assertEqual(len([row for row in rows if row["event_type"] == "wait_finished"
                                  and row["wait_id"] == wait_id]), 1, rows)

    def test_unheld_local_call_needs_no_release(self) -> None:
        result = self.fixture.verify("test", "sample", "--local", "local admission test", "--filter", "smoke")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("natedev is held", result.stdout + result.stderr)
        self.assertEqual(len(self.fixture.local_runs()), 1)
        self.assertFalse(self.cycle_file.exists())

    def test_call_with_an_empty_holder_directory_starts_no_hold_check(self) -> None:
        Path(self.fixture.environment["BUILD_HOLD_DIR"]).mkdir()
        starts = self.count_python_starts()
        for arguments in (("check", "sample"), ("test", "sample", "--local", "local admission test", "--filter", "smoke")):
            with self.subTest(arguments=arguments):
                starts.unlink(missing_ok=True)
                result = self.fixture.verify(*arguments)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.hold_commands(starts), [])
                self.assertEqual(self.recorded_hold_wait(), 0)

    def test_held_call_starts_the_same_hold_checks_as_before(self) -> None:
        self.hold()
        starts = self.count_python_starts()
        observation = self.observe_waits()
        process = self.start("test", "sample", "--local", "local admission test", "--filter", "smoke")
        _ = self.wait_for_call(process)
        self.assertEqual(self.fixture.local_runs(), [])
        grant_call(self.cycle_file)
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        self.assertIn("held by hana for render checks; waiting", output)
        self.assertEqual(output.count("; waiting (tests that can run on the Mac"), 1)
        self.assertEqual(len(self.fixture.local_runs()), 1)
        observed = cast(WaitObservation, json.loads(observation.read_text()))
        self.assertGreater(float(observed["total"]), 0)
        self.assertAlmostEqual(float(observed["total"]), sum(float(value) for value in observed["waits"]), places=5)
        self.assertAlmostEqual(self.recorded_hold_wait(), float(observed["total"]), places=5)
        commands = self.hold_commands(starts)
        self.assertTrue(any(command[1] == "wait-local" for command in commands))
        self.assertFalse(any(command[1] in {"mark", "check-admission", "finish-local"} for command in commands))

    def test_held_call_waits_before_compiling_then_runs_on_release(self) -> None:
        history = self.record_phase()
        self.hold()
        self.fixture.environment["BUILD_TARGETS_POLL_S"] = "3600"
        process = self.start("test", "sample", "--local", "local admission test", "--filter", "smoke")
        _ = self.wait_for_call(process)
        self.assertEqual(self.fixture.local_runs(), [])
        grant_call(self.cycle_file)
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        self.assertIn("held by hana for render checks; waiting", output)
        self.assertEqual(len(self.fixture.local_runs()), 1)
        self.assertFalse((Path(self.fixture.environment["BUILD_HOLD_DIR"]) / "hana").exists())
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        started, = [row for row in rows if row["event_type"] == "wait_started"]
        finished, = [row for row in rows if row["event_type"] == "wait_finished"]
        self.assertEqual((started["wait_kind"], started["wait_reason"], started["owner_session"]),
                         ("WaitingForMemoryOrSlot", "build hold", "thread-impl"))
        self.assertEqual(started["wait_id"], finished["wait_id"])
        self.assertEqual(cast(dict[str, object], finished["wait_end"])["state"], "Finished")

    def test_unreadable_hold_state_starts_no_local_step(self) -> None:
        _ = Path(self.fixture.environment["BUILD_HOLD_DIR"]).write_text("not a directory")
        result = self.fixture.verify("test", "sample", "--local", "local admission test", "--filter", "smoke")
        self.assertEqual(result.returncode, 12, result.stdout + result.stderr)
        self.assertIn("hold state cannot be read", result.stdout + result.stderr)
        self.assertEqual(self.fixture.local_runs(), [])


if __name__ == "__main__":
    _ = unittest.main()
