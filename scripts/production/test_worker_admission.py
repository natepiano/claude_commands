"""Permission is read-only; backend identities are published before work is sent."""
from __future__ import annotations

import os
from dataclasses import replace
from unittest import mock

from scripts.production import staff, worker_admission
from scripts.production.staff_test_support import StaffTestCase, fixture_runtime_id


class WorkerAdmissionTests(StaffTestCase):
    def test_invalid_worker_roles_refuse_before_membership_or_backend_publication(self) -> None:
        _, unit, _ = self.tree()
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        for role in ("", "owner", "showrunner", "arbitrary"):
            with self.subTest(role=role), mock.patch.object(staff, "runtime_id", return_value=unit.current_session):
                with self.assertRaisesRegex(worker_admission.AdmissionRefused, "not opened: invalid worker role"):
                    _ = worker_admission.admit("new-worker", self.root, role=role)
                self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)
        self.assertFalse(any(row.name == "new-worker" for row in staff.snapshot().rows))

    def test_valid_worker_roles_are_retained_by_read_only_start_permission(self) -> None:
        _, unit, _ = self.tree()
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        for supplied, expected in (("impl", "impl"), ("test", "test"), ("review", "review"),
                ("fix", "fix"), ("designer", "astra-designer"), ("reviewer", "astra-reviewer"),
                ("astra-designer", "astra-designer"), ("astra-reviewer", "astra-reviewer")):
            with self.subTest(role=supplied), mock.patch.object(staff, "runtime_id", return_value=unit.current_session):
                allowed = worker_admission.admit("new-worker", self.root, role=supplied)
                self.assertEqual((allowed.name, allowed.parent, allowed.role), ("new-worker", unit.id, expected))
                self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_launch_permission_creates_no_row_and_publication_uses_the_returned_thread(self) -> None:
        _, unit, _ = self.tree()
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        with mock.patch.object(staff, "runtime_id", return_value=unit.id):
            allowed = worker_admission.admit("test", self.root, role="test")
        self.assertIsInstance(allowed, worker_admission.WorkerStartAllowed)
        self.assertEqual((allowed.name, allowed.parent, allowed.role), ("test", unit.id, "test"))
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)
        self.assertEqual(staff.find(kind="worker", parent=unit.id), [self.tree()[2]])
        thread = fixture_runtime_id("returned-test-thread")
        published = worker_admission.publish(allowed, thread)
        self.assertIsInstance(published, worker_admission.PublishedWorker)
        self.assertEqual((published.row.id, published.row.name, published.row.parent), (thread, "test", unit.id))
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
        self.assertEqual(worker.row.parent, unit.id)
        self.assertEqual(staff.by_current_session(current), changed)
        self.assertIsNone(staff.by_current_session(unit.id))

    def test_refused_permission_starts_nothing_and_changes_no_row(self) -> None:
        _, unit, _ = self.tree()
        _ = staff.set_worktree(unit.id, staff.RemovedWorktree("natedev", self.root / "gone"))
        before = self.staff_file.read_bytes()
        with mock.patch.object(staff, "runtime_id", return_value=unit.id):
            with self.assertRaisesRegex(worker_admission.AdmissionRefused, r"^staff: test: not opened:.*retired"):
                _ = worker_admission.admit("test", self.root, role="test")
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertFalse(any(row.name == "test" for row in staff.find(kind="worker")))

    def test_publication_refusal_names_the_created_backend_and_preserves_membership(self) -> None:
        _, unit, _ = self.tree()
        with mock.patch.object(staff, "runtime_id", return_value=unit.id):
            allowed = worker_admission.admit("test", self.root, role="test")
        before = self.staff_file.read_bytes()
        thread = fixture_runtime_id("refused-thread")
        with mock.patch.object(staff, "ensure_worker", side_effect=staff.StaffRefused("cannot publish")):
            with self.assertRaisesRegex(worker_admission.AdmissionRefused, thread + ": publication refused: cannot publish"):
                _ = worker_admission.publish(allowed, thread)
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertIsNone(staff.by_id(staff.StaffId(thread)))

    def test_replacement_changes_only_current_session_and_leaves_the_fixed_graph_unchanged(self) -> None:
        _, unit, old = self.tree()
        sibling = staff.ensure_worker("test", unit.id, runtime_id=fixture_runtime_id("sibling-thread"))
        with mock.patch.object(staff, "runtime_id", return_value=unit.id):
            allowed = worker_admission.admit(old.name, self.root, role="impl")
        thread = fixture_runtime_id("replacement-thread")
        before = self.staff_file.read_bytes()
        result = worker_admission.publish(allowed, thread, previous_id=old.current_session)
        self.assertEqual(result.row, replace(old, current_session=staff.StaffId(thread)))
        self.assertEqual(staff.by_id(old.id), result.row)
        self.assertEqual(self.staff_file.read_bytes(), before.replace(
            f'"current_session": "{old.current_session}"'.encode(),
            f'"current_session": "{thread}"'.encode(), 1))
        self.assertIsNone(staff.by_current_session(old.current_session))
        self.assertEqual(staff.by_current_session(thread), result.row)
        self.assertEqual(staff.by_id(sibling.id), sibling)
        self.assertEqual(staff.by_id(unit.id), unit)

    def test_replacement_refuses_an_unproved_previous_owner_without_writing(self) -> None:
        _, unit, old = self.tree()
        with mock.patch.object(staff, "runtime_id", return_value=unit.id):
            allowed = worker_admission.admit("different", self.root, role="test")
        before = self.staff_file.read_bytes()
        with self.assertRaisesRegex(worker_admission.AdmissionRefused, "replacement owner is not proved"):
            _ = worker_admission.publish(allowed, fixture_runtime_id("wrong-replacement"), previous_id=old.id)
        self.assertEqual(self.staff_file.read_bytes(), before)
