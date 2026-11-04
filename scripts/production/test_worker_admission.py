"""Permission is read-only; backend identities are published before work is sent."""
from __future__ import annotations

import os
import json
from dataclasses import replace
from unittest import mock

from scripts.lib import unit_runs
from scripts.production import staff, worker_admission
from scripts.production import session_staff
from scripts.production.staff_test_support import StaffTestCase, fixture_runtime_id


class RecordedDirectorAdmissionTests(StaffTestCase):
    def test_old_caller_resolves_recorded_native_director_before_run_names(self) -> None:
        _, unit, _ = self.tree()
        run = self.root / "run"
        run.mkdir()
        _ = (run / unit_runs.DIRECTOR_FILE).write_text(unit.id + "\n")
        _ = (run / unit_runs.STATE_FILE).write_text(json.dumps({
            "production": "former-production", "unit": "former-unit",
        }))
        before = self.staff_file.read_bytes()
        found = worker_admission.parent(staff.snapshot(), staff_id="s0123456789ab", run=run)
        self.assertEqual(found, unit)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_old_or_missing_caller_resolves_director_with_empty_run_names(self) -> None:
        _, unit, _ = self.tree()
        run = self.root / "run"
        run.mkdir()
        _ = (run / unit_runs.DIRECTOR_FILE).write_text(unit.id + "\n")
        _ = (run / unit_runs.STATE_FILE).write_text('{"production": "", "unit": ""}')
        before = self.staff_file.read_bytes()
        for caller in ("s0123456789ab", ""):
            with self.subTest(caller=caller), mock.patch.object(staff, "runtime_id", return_value=caller):
                allowed = worker_admission.admit("new-worker", run, role="impl")
                self.assertEqual(allowed.parent, unit.id)
                self.assertEqual(self.staff_file.read_bytes(), before)

    def test_worker_caller_resolves_its_fixed_parent_after_session_replacement(self) -> None:
        _, unit, worker = self.tree()
        current = staff.native_id(fixture_runtime_id("dispatching-worker-current-session"))
        _ = staff.update_current_session(worker.current_session, current)
        before = self.staff_file.read_bytes()
        found = worker_admission.parent(staff.snapshot(), staff_id=current)
        self.assertEqual(found, unit)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_unknown_caller_without_run_or_names_keeps_the_refusal(self) -> None:
        _ = self.tree()
        before = self.staff_file.read_bytes()
        with self.assertRaises(ValueError) as refused:
            _ = worker_admission.parent(
                staff.snapshot(), session_staff.MemberSources({}, ()), staff_id="s0123456789ab",
            )
        self.assertEqual(str(refused.exception), "no showrunner staff row for ")
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_recorded_fixed_director_resolves_after_clear(self) -> None:
        _, unit, _ = self.tree()
        run = self.root / "run"
        run.mkdir()
        director = run / unit_runs.DIRECTOR_FILE
        _ = director.write_text(unit.id + "\n")
        current = staff.native_id(fixture_runtime_id("cleared-dispatch-director"))
        changed = staff.update_current_session(unit.current_session, current)
        before = self.staff_file.read_bytes(), director.read_bytes()
        found = worker_admission.parent(staff.snapshot(), staff_id="s0123456789ab", run=run)
        self.assertEqual(found, changed)
        self.assertEqual((self.staff_file.read_bytes(), director.read_bytes()), before)



class WorkerAdmissionTests(StaffTestCase):
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

    def test_start_permission_creates_no_row_and_publication_uses_the_returned_thread(self) -> None:
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
