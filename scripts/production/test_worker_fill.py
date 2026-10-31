"""Worker membership comes from producer records, with no worker side effects."""
from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from scripts.production import staff, worker_fill
from scripts.production.staff_test_support import StaffTestCase, production_membership, unit_membership


class WorkerFillTests(StaffTestCase):
    def result(self, *, dry_run: bool = False) -> tuple[int, str, str]:
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            code = worker_fill.fill(dry_run=dry_run)
        return code, output.getvalue(), errors.getvalue()

    def test_fill_places_ledger_roster_and_claude_workers_under_their_unit_once(self) -> None:
        _ = self.production("demo", "models-unit")
        production_membership("demo", "models-unit")
        parent = unit_membership("demo", "models-unit")
        run = self.worker_run("demo", "models-unit", "session-models")
        _ = self.ledger_worker(run, "demo", "models-unit", "impl", "impl", "thread-impl")
        _ = self.roster_worker(run, "test", "test", "thread-test", status="paused")
        _ = self.ledger_worker(run, "demo", "models-unit", "helper", "fix", "session-helper", family="claude")
        _ = self.ledger_worker(run, "demo", "models-unit", "ended", "test", "thread-ended", retired=True)
        code, output, errors = self.result()
        self.assertEqual((code, errors), (0, ""), output + errors)
        self.assertEqual({row.name for row in staff.find(kind="worker", parent=parent.id)}, {"impl", "test", "helper"})
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        self.assertEqual(self.result(), (0, "unchanged\n", ""))
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_dry_run_lists_placements_without_writing_any_staff_bytes(self) -> None:
        _ = self.production("demo", "models-unit")
        production_membership("demo", "models-unit")
        run = self.worker_run("demo", "models-unit", "session-models")
        _ = self.ledger_worker(run, "demo", "models-unit", "impl", "impl", "thread-impl")
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        with mock.patch.object(staff, "ensure_worker", side_effect=AssertionError("dry-run wrote staff")):
            code, output, errors = self.result(dry_run=True)
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("would place impl under ", output)
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_unplaceable_ledger_is_named_while_complete_workers_still_fill(self) -> None:
        _ = self.production("demo", "models-unit")
        production_membership("demo", "models-unit")
        run = self.worker_run("demo", "models-unit", "session-models")
        _ = self.ledger_worker(run, "demo", "models-unit", "impl", "impl", "thread-impl")
        missing = self.worker_run("unknown", "missing-unit", "unknown-director")
        path = self.ledger_worker(missing, "unknown", "missing-unit", "test", "test", "thread-missing")
        code, _, errors = self.result()
        self.assertEqual(code, 1)
        self.assertIn(f"{path}:test test:", errors)
        self.assertEqual({row.name for row in staff.find(kind="worker")}, {"impl"})

    def test_showrunner_held_astra_is_listed_without_a_unit_row(self) -> None:
        _ = self.production("demo")
        production_membership("demo")
        path = self.worker_design("demo", "demo-design-astra-designer", "thread-designer")
        code, _, errors = self.result()
        self.assertEqual(code, 1)
        self.assertIn(str(path), errors)
        self.assertIn("demo-design-astra-designer", errors)
        self.assertEqual(staff.find(kind="worker"), [])

    def test_admission_uses_current_unit_membership_and_is_unchanged_on_retry(self) -> None:
        _ = self.production("demo", "models-unit")
        production_membership("demo", "models-unit")
        parent = unit_membership("demo", "models-unit")
        run = self.worker_run("demo", "models-unit", "director")
        row = worker_fill.admit("hana_catalyst-impl", run)
        self.assertEqual(row.parent, parent.id)
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        self.assertEqual(worker_fill.admit("hana_catalyst-impl", run), row)
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_admission_refuses_an_unknown_parent_without_writing_a_row(self) -> None:
        production_membership("demo")
        run = self.worker_run("demo", "missing-unit", "director")
        before = self.staff_file.read_bytes()
        with self.assertRaisesRegex(worker_fill.AdmissionRefused, "staff: impl: not opened: no unit staff row"):
            _ = worker_fill.admit("impl", run)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_unreadable_staff_refuses_admission_and_keeps_the_record(self) -> None:
        self.staff_file.parent.mkdir(parents=True, exist_ok=True)
        _ = self.staff_file.write_text("{bad record")
        with self.assertRaisesRegex(worker_fill.AdmissionRefused, "staff: impl: not opened:"):
            _ = worker_fill.admit("impl", self.root)
        self.assertEqual(self.staff_file.read_text(), "{bad record")

    def test_refused_staff_write_does_not_leave_a_worker_row(self) -> None:
        _ = self.production("demo", "models-unit")
        production_membership("demo", "models-unit")
        run = self.worker_run("demo", "models-unit", "director")
        before = self.staff_file.read_bytes()
        with mock.patch.object(staff, "ensure_worker", side_effect=staff.StaffRefused("record is locked")):
            with self.assertRaisesRegex(worker_fill.AdmissionRefused, "not opened: record is locked"):
                _ = worker_fill.admit("impl", run)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_retired_unit_cannot_admit_a_worker(self) -> None:
        _ = self.production("demo", "models-unit")
        production_membership("demo", "models-unit")
        parent = unit_membership("demo", "models-unit")
        _ = staff.set_worktree(parent.id, staff.RemovedWorktree("natedev", self.root / "gone"))
        run = self.worker_run("demo", "models-unit", "director")
        with self.assertRaises(worker_fill.AdmissionRefused):
            _ = worker_fill.admit("impl", run)
        self.assertEqual(staff.find(kind="worker"), [])
