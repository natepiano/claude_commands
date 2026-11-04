"""Permission is read-only; backend identities are published before work is sent."""
from __future__ import annotations

import os
from dataclasses import replace
from unittest import mock

from scripts.production import staff, worker_admission
from scripts.production.staff_test_support import StaffTestCase, fixture_runtime_id


class RecordedDirectorAdmissionTests(StaffTestCase):
    def test_worker_caller_resolves_its_fixed_parent_after_session_replacement(self) -> None:
        _, unit, worker = self.tree()
        current = staff.native_id(fixture_runtime_id("dispatching-worker-current-session"))
        _ = staff.update_current_session(worker.current_session, current)
        before = self.staff_file.read_bytes()
        found = worker_admission.parent(staff.snapshot(), staff_id=current)
        self.assertEqual(found, unit)
        self.assertEqual(self.staff_file.read_bytes(), before)

class WorkerAdmissionTests(StaffTestCase):
    def test_valid_worker_roles_are_retained_by_read_only_start_permission(self) -> None:
        _, unit, _ = self.tree()
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        for supplied in staff.WORKER_ROLES:
            with self.subTest(role=supplied), mock.patch.object(staff, "runtime_id", return_value=unit.current_session):
                allowed = worker_admission.admit("new-worker", self.root, role=supplied)
                self.assertEqual((allowed.name, allowed.parent, allowed.member), ("new-worker", unit.id, None))
                self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_start_permission_creates_no_row_and_publication_uses_the_returned_thread(self) -> None:
        _, unit, _ = self.tree()
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        with mock.patch.object(staff, "runtime_id", return_value=unit.id):
            allowed = worker_admission.admit("test", self.root, role="test")
        self.assertIsInstance(allowed, worker_admission.WorkerStartAllowed)
        self.assertEqual((allowed.name, allowed.parent, allowed.member), ("test", unit.id, None))
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)
        self.assertEqual(staff.find(kind="worker", parent=unit.id), [self.tree()[2]])
        thread = fixture_runtime_id("returned-test-thread")
        published = worker_admission.publish(allowed, thread)
        self.assertIsInstance(published, worker_admission.PublishedWorker)
        self.assertEqual((published.row.id, published.row.name, published.row.parent), (thread, "test", staff.ReportsTo(unit.id)))
        self.assertEqual(staff.by_id(staff.StaffId(thread)), published.row)
        after = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        self.assertEqual(worker_admission.publish(allowed, thread), published)
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), after)

    def test_codex_unit_admits_under_its_fixed_parent_id_after_thread_replacement(self) -> None:
        _, unit, _ = self.tree()
        current = staff.native_id(fixture_runtime_id("codex-unit-replaced-thread"))
        changed = staff.update_current_session(unit.current_session, current)
        before = self.staff_file.read_bytes()
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": current}):
            allowed = worker_admission.admit("test", self.root, role="test")
        self.assertEqual(allowed.parent, unit.id)
        self.assertEqual(staff.by_id(unit.id), changed)
        self.assertEqual(self.staff_file.read_bytes(), before)
        worker = worker_admission.publish(allowed, fixture_runtime_id("worker-of-codex-unit"))
        self.assertEqual(worker.row.parent, staff.ReportsTo(unit.id))
        self.assertEqual(staff.by_current_session(current), changed)
        self.assertIsNone(staff.by_current_session(unit.id))

    def test_replacement_changes_only_current_session_and_leaves_the_fixed_graph_unchanged(self) -> None:
        _, unit, old = self.tree()
        sibling = staff.ensure_worker(staff.native_id(fixture_runtime_id("sibling-thread")), "test", unit.id)
        with mock.patch.object(staff, "runtime_id", return_value=unit.id):
            allowed = worker_admission.admit(old.name, self.root, role="impl")
        thread = fixture_runtime_id("replacement-thread")
        before = self.staff_file.read_bytes()
        result = worker_admission.publish(allowed, thread)
        self.assertEqual(result.row, replace(old, current_session=staff.StaffId(thread)))
        self.assertEqual(staff.by_id(old.id), result.row)
        self.assertEqual(self.staff_file.read_bytes(), before.replace(
            f'"current_session": "{old.current_session}"'.encode(),
            f'"current_session": "{thread}"'.encode(), 1))
        self.assertIsNone(staff.by_current_session(old.current_session))
        self.assertEqual(staff.by_current_session(thread), result.row)
        self.assertEqual(staff.by_id(sibling.id), sibling)
        self.assertEqual(staff.by_id(unit.id), unit)
