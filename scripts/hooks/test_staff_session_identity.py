"""A clear proves one exact process and changes only its current native session."""
from __future__ import annotations

from functools import cached_property
from dataclasses import dataclass

from contextlib import redirect_stderr
from dataclasses import replace
import io
import json
import os
import runpy
from pathlib import Path
from typing import Callable, cast, override
from unittest import mock

from scripts.hooks import staff_session_identity
from scripts.message import sessions
from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase, fixture_runtime_id
from scripts.shutdown import pane_record


@dataclass
class ClearWorkerFixture:
    runner: staff.StaffRow
    unit: staff.StaffRow
    worker: staff.StaffRow
    sibling: staff.StaffRow
    new_id: str
    environment: dict[str, str]
    payload: dict[str, object]
    launch: dict[str, object]
    panes: Path
    parked: Path


class StaffSessionIdentityTests(StaffTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.fixture

    @cached_property
    def fixture(self) -> ClearWorkerFixture:
        runner, unit, worker = self.tree()
        sibling = staff.ensure_worker(staff.native_id(fixture_runtime_id("sibling")), "test", unit.id)
        new_id = fixture_runtime_id("after-clear")
        environment = {"HERDR_ENV": "1", "HERDR_SESSION": "agents", "HERDR_PANE_ID": "w1:p1",
                       "CLAUDE_CONFIG_DIR": os.environ["CLAUDE_CONFIG_DIR"]}
        payload: dict[str, object] = {
            "hook_event_name": "SessionStart", "source": "clear", "session_id": new_id,
            "cwd": str(self.root),
        }
        launch: dict[str, object] = dict(pane_record.launch(
            "w1:p1", worker.id, str(self.root), environment, ["claude"],
        ))
        worker = replace(worker, home=staff.SessionHome(Path(environment["CLAUDE_CONFIG_DIR"])))
        launch.update({"pid": os.getpid(), "proc_start": sessions.process_start(os.getpid())})
        panes = pane_record.record_path("agents")
        self.write_json(panes, {"w1:p1": launch})
        parked = self.root / "parked.toml"
        _ = parked.write_text('plan = "plan.md"\n')

        return ClearWorkerFixture(runner, unit, worker, sibling, new_id, environment, payload, launch, panes, parked)

    def handle(self) -> None:
        with (mock.patch.object(os, "getppid", return_value=os.getpid()),
              mock.patch.object(staff, "runtime_id", return_value=self.fixture.new_id)):
            _ = staff_session_identity.handle(self.fixture.payload, self.fixture.environment)

    def record_start(self) -> None:
        hook = Path(staff_session_identity.__file__).with_name("session-start-pane-record.py")
        namespace = runpy.run_path(str(hook))
        record_start = cast(Callable[[], None], namespace["main"])
        with (mock.patch.dict(os.environ, self.fixture.environment),
              mock.patch("sys.stdin", io.StringIO(json.dumps(self.fixture.payload))),
              mock.patch.object(pane_record, "claude_arguments", return_value=["claude"]),
              mock.patch.object(os, "getppid", return_value=os.getpid()),
              mock.patch.object(staff, "runtime_id", return_value=self.fixture.new_id)):
            record_start()

    def test_outer_staffed_clear_changes_only_staff_and_keeps_retained_launch_bytes(self) -> None:
        before = self.staff_file.read_bytes()
        launch = self.fixture.panes.read_bytes(), self.fixture.panes.stat().st_mtime_ns
        parked = self.fixture.parked.read_bytes()
        with mock.patch.object(pane_record, "write", wraps=pane_record.write) as write:
            self.record_start()
        write.assert_not_called()
        self.assertEqual(self.staff_file.read_bytes(), before.replace(
            f'"current_session": "{self.fixture.worker.current_session}"'.encode(),
            f'"current_session": "{self.fixture.new_id}"'.encode(), 1))
        self.assertEqual((self.fixture.panes.read_bytes(), self.fixture.panes.stat().st_mtime_ns), launch)
        self.assertEqual(self.fixture.parked.read_bytes(), parked)
        self.assertEqual(staff.by_id(self.fixture.worker.id), replace(
            self.fixture.worker, current_session=staff.native_id(self.fixture.new_id)))

    def test_outer_clear_without_staff_row_records_new_native_launch_for_restart(self) -> None:
        self.forget_rows(self.fixture.worker.id)
        before = self.staff_file.read_bytes()
        self.record_start()
        launch = pane_record.read("agents")["w1:p1"]
        self.assertEqual(launch["session_id"], self.fixture.new_id)
        self.assertEqual(launch.get("pid"), os.getpid())
        self.assertEqual(launch.get("proc_start"), sessions.process_start(os.getpid()))
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertIsNone(staff.by_current_session(self.fixture.new_id))
        self.assertEqual(pane_record.resume_launch(launch)["session_id"], self.fixture.new_id)

    def test_outer_owned_clear_io_failure_preserves_staff_and_old_launch(self) -> None:
        before = self.staff_file.read_bytes(), self.fixture.panes.read_bytes()
        with (mock.patch.object(staff, "update_current_session", side_effect=OSError("owner unreachable")),
              mock.patch.object(pane_record, "write", wraps=pane_record.write) as write):
            with self.assertRaisesRegex(OSError, "owner unreachable"):
                self.record_start()
        write.assert_not_called()
        self.assertEqual((self.staff_file.read_bytes(), self.fixture.panes.read_bytes()), before)

    def test_clear_row_removed_during_publication_records_new_launch_without_adopting_another_row(self) -> None:
        remaining: list[bytes] = []

        def removed(old: staff.StaffId, new: staff.StaffId) -> staff.StaffRow:
            self.assertEqual((old, new), (self.fixture.worker.current_session, self.fixture.new_id))
            self.forget_rows(self.fixture.worker.id)
            remaining.append(self.staff_file.read_bytes())
            raise staff.StaffNotFound(old)

        with mock.patch.object(staff, "update_current_session", side_effect=removed):
            self.record_start()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(self.staff_file.read_bytes(), remaining[0])
        self.assertEqual(staff.by_id(self.fixture.unit.id), self.fixture.unit)
        self.assertEqual(staff.by_id(self.fixture.sibling.id), self.fixture.sibling)
        self.assertIsNone(staff.by_current_session(self.fixture.new_id))
        launch = pane_record.read("agents")["w1:p1"]
        self.assertEqual(launch["session_id"], self.fixture.new_id)
        self.assertEqual(launch.get("pid"), os.getpid())
        self.assertEqual(launch.get("proc_start"), sessions.process_start(os.getpid()))
        self.assertEqual(pane_record.resume_launch(launch)["session_id"], self.fixture.new_id)

    def test_not_found_with_an_existing_owner_preserves_retry_evidence_instead_of_recording_a_new_launch(self) -> None:
        before = self.staff_file.read_bytes(), self.fixture.panes.read_bytes()
        with (mock.patch.object(staff, "update_current_session", side_effect=staff.StaffNotFound(self.fixture.worker.current_session)),
              mock.patch.object(pane_record, "write", wraps=pane_record.write) as write):
            with self.assertRaisesRegex(staff.StaffNotFound, "staff member not found: " + self.fixture.worker.current_session):
                self.record_start()
        write.assert_not_called()
        self.assertEqual((self.staff_file.read_bytes(), self.fixture.panes.read_bytes()), before)
        self.record_start()
        self.assertEqual(staff.by_id(self.fixture.worker.id), replace(
            self.fixture.worker, current_session=staff.native_id(self.fixture.new_id)))
        self.assertEqual(self.fixture.panes.read_bytes(), before[1])

    def test_session_end_clear_outside_herdr_or_without_row_is_silent_success(self) -> None:
        ending: dict[str, object] = {"hook_event_name": "SessionEnd", "reason": "clear",
                                     "session_id": self.fixture.worker.current_session}
        before = self.staff_file.read_bytes(), self.fixture.panes.read_bytes()
        for staffed in (True, False):
            if not staffed:
                self.forget_rows(self.fixture.worker.id)
                before = self.staff_file.read_bytes(), self.fixture.panes.read_bytes()
            environment = {**self.fixture.environment, "HERDR_ENV": "0"} if staffed else self.fixture.environment
            with (self.subTest(staffed=staffed), mock.patch.dict(os.environ, environment),
                  mock.patch("sys.stdin", io.StringIO(json.dumps(ending))),
                  mock.patch.object(staff, "runtime_id", side_effect=AssertionError("unassigned clear read process")),
                  redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(staff_session_identity.main(), 0)
            self.assertEqual(errors.getvalue(), "")
            self.assertEqual((self.staff_file.read_bytes(), self.fixture.panes.read_bytes()), before)

    def test_clearing_a_worker_keeps_its_unit_and_same_folder_siblings_and_other_stores(self) -> None:
        staff_before = self.staff_file.read_bytes()
        before = self.fixture.panes.read_bytes(), self.fixture.parked.read_bytes()
        self.handle()
        self.assertIsNone(staff.by_current_session(self.fixture.worker.current_session))
        self.assertEqual(staff.by_id(self.fixture.worker.id), replace(self.fixture.worker, current_session=staff.StaffId(self.fixture.new_id)))
        self.assertEqual(staff.by_id(self.fixture.unit.id), self.fixture.unit)
        self.assertEqual(staff.by_id(self.fixture.runner.id), self.fixture.runner)
        self.assertEqual(staff.by_id(self.fixture.sibling.id), self.fixture.sibling)
        self.assertEqual((self.fixture.panes.read_bytes(), self.fixture.parked.read_bytes()), before)
        self.assertEqual(self.staff_file.read_bytes(), staff_before.replace(
            f'"current_session": "{self.fixture.worker.current_session}"'.encode(),
            f'"current_session": "{self.fixture.new_id}"'.encode(), 1))


    def test_repeated_clear_publication_is_a_noop_and_does_not_rewrite_the_launch(self) -> None:
        self.handle()
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns, self.fixture.panes.read_bytes()
        self.handle()
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns, self.fixture.panes.read_bytes()), before)

    def test_ordinary_clear_recording_runs_only_after_the_staff_handoff(self) -> None:
        before = self.fixture.panes.read_bytes(), self.fixture.panes.stat().st_mtime_ns
        with mock.patch.object(pane_record, "write", wraps=pane_record.write) as write:
            self.record_start()
        expected = replace(self.fixture.worker, current_session=staff.StaffId(self.fixture.new_id))
        write.assert_not_called()
        self.assertEqual(staff.by_id(self.fixture.worker.id), expected)
        self.assertEqual((self.fixture.panes.read_bytes(), self.fixture.panes.stat().st_mtime_ns), before)
        recorded = pane_record.read("agents")["w1:p1"]
        self.assertEqual(recorded["session_id"], self.fixture.worker.id)
        self.assertEqual(recorded.get("pid"), os.getpid())
        self.assertEqual(recorded.get("proc_start"), sessions.process_start(os.getpid()))

    def test_refused_clear_handoff_keeps_the_old_launch_observation_and_staff_graph(self) -> None:
        self.fixture.payload["session_id"] = fixture_runtime_id("unproved-hook-id")
        before = self.staff_file.read_bytes(), self.fixture.panes.read_bytes()
        with mock.patch.object(pane_record, "write", wraps=pane_record.write) as write:
            with self.assertRaisesRegex(staff.StaffRefused, "registry identities differ"):
                self.record_start()
        write.assert_not_called()
        self.assertEqual((self.staff_file.read_bytes(), self.fixture.panes.read_bytes()), before)
        self.assertEqual(staff.by_id(self.fixture.worker.id), self.fixture.worker)

    def test_a_second_clear_uses_the_current_row_instead_of_reusing_the_original_launch_id(self) -> None:
        before = self.fixture.panes.read_bytes(), self.fixture.panes.stat().st_mtime_ns
        self.handle()
        previous = self.fixture.new_id
        self.record_start()
        recorded = pane_record.read("agents")["w1:p1"]
        self.assertEqual(recorded["session_id"], self.fixture.worker.id)
        self.assertEqual(recorded.get("pid"), os.getpid())
        self.fixture.new_id = fixture_runtime_id("second-clear")
        # The SessionEnd observation identifies the actual old id of this process.
        ending: dict[str, object] = {
            "hook_event_name": "SessionEnd", "reason": "clear", "session_id": previous,
        }
        with (mock.patch.object(os, "getppid", return_value=os.getpid()),
              mock.patch.object(staff, "runtime_id", return_value=previous)):
            _ = staff_session_identity.handle(ending, self.fixture.environment)
        self.fixture.payload["session_id"] = self.fixture.new_id
        self.handle()
        self.assertIsNone(staff.by_current_session(previous))
        self.assertEqual(staff.by_id(self.fixture.unit.id), self.fixture.unit)
        self.assertEqual(staff.by_id(self.fixture.runner.id), self.fixture.runner)
        self.assertEqual(staff.by_id(self.fixture.worker.id), replace(self.fixture.worker, current_session=staff.StaffId(self.fixture.new_id)))
        self.assertEqual(staff.by_id(self.fixture.sibling.id), self.fixture.sibling)
        self.assertEqual((self.fixture.panes.read_bytes(), self.fixture.panes.stat().st_mtime_ns), before)

    def test_clear_refuses_a_different_process_start_without_touching_staff_or_panes(self) -> None:
        self.fixture.launch["proc_start"] = "a previous process with the same pid"
        self.write_json(self.fixture.panes, {"w1:p1": self.fixture.launch})
        before = self.staff_file.read_bytes(), self.fixture.panes.read_bytes()
        with self.assertRaisesRegex(staff.StaffRefused, "process start"):
            self.handle()
        self.assertEqual((self.staff_file.read_bytes(), self.fixture.panes.read_bytes()), before)

    def test_clear_refuses_an_unproved_registry_id_and_keeps_the_old_row(self) -> None:
        self.fixture.payload["session_id"] = fixture_runtime_id("unrelated-session")
        before = self.staff_file.read_bytes(), self.fixture.panes.read_bytes()
        with self.assertRaisesRegex(staff.StaffRefused, "registry identities differ"):
            self.handle()
        self.assertEqual((self.staff_file.read_bytes(), self.fixture.panes.read_bytes()), before)
        self.assertEqual(staff.by_id(self.fixture.worker.id), self.fixture.worker)

    def test_failed_clear_hook_reports_one_reason_and_keeps_the_old_graph(self) -> None:
        before = self.staff_file.read_bytes(), self.fixture.panes.read_bytes()
        with (mock.patch("sys.stdin", io.StringIO(json.dumps(self.fixture.payload))),
              mock.patch.dict(os.environ, self.fixture.environment),
              mock.patch.object(os, "getppid", return_value=os.getpid()),
              mock.patch.object(staff, "runtime_id", return_value=self.fixture.new_id),
              mock.patch.object(staff, "update_current_session", side_effect=staff.StaffRefused("write denied")),
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(staff_session_identity.main(), 2)
        self.assertEqual(errors.getvalue().splitlines(), ["staff identity: write denied"])
        self.assertEqual((self.staff_file.read_bytes(), self.fixture.panes.read_bytes()), before)

    def test_compaction_between_clears_keeps_fixed_launch_owner_and_allows_the_next_handoff(self) -> None:
        self.record_start()
        previous = self.fixture.new_id
        after_first = self.staff_file.read_bytes()
        self.fixture.payload["source"] = "compact"
        self.record_start()
        self.assertEqual(self.staff_file.read_bytes(), after_first)
        launch = pane_record.read("agents")["w1:p1"]
        self.assertEqual(launch["session_id"], self.fixture.worker.id)
        self.assertEqual(launch.get("pid"), os.getpid())
        self.assertEqual(launch.get("proc_start"), sessions.process_start(os.getpid()))
        retained = self.fixture.panes.read_bytes(), self.fixture.panes.stat().st_mtime_ns
        ending: dict[str, object] = {"hook_event_name": "SessionEnd", "reason": "clear", "session_id": previous}
        with (mock.patch.object(os, "getppid", return_value=os.getpid()),
              mock.patch.object(staff, "runtime_id", return_value=previous)):
            self.assertEqual(staff_session_identity.handle(ending, self.fixture.environment),
                             staff_session_identity.ClearIdentityResult.STAFFED)
        self.fixture.new_id = fixture_runtime_id("clear-after-compaction")
        self.fixture.payload.update(source="clear", session_id=self.fixture.new_id)
        self.record_start()
        self.assertEqual(staff.by_id(self.fixture.worker.id), replace(
            self.fixture.worker, current_session=staff.native_id(self.fixture.new_id)))
        self.assertEqual(staff.by_id(self.fixture.unit.id), self.fixture.unit)
        self.assertEqual(staff.by_id(self.fixture.sibling.id), self.fixture.sibling)
        self.assertEqual((self.fixture.panes.read_bytes(), self.fixture.panes.stat().st_mtime_ns), retained)
        self.assertEqual(self.staff_file.read_bytes(), after_first.replace(
            f'"current_session": "{previous}"'.encode(),
            f'"current_session": "{self.fixture.new_id}"'.encode(), 1))

    def test_compaction_does_not_change_any_staff_or_launch_record(self) -> None:
        self.fixture.payload["source"] = "compact"
        before = self.staff_file.read_bytes(), self.fixture.panes.read_bytes()
        self.handle()
        self.assertEqual((self.staff_file.read_bytes(), self.fixture.panes.read_bytes()), before)
