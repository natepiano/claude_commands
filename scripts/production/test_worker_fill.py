"""Worker publication uses the current staff row."""
from __future__ import annotations

import io
import os
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from scripts.production import staff, worker_fill
from scripts.production.staff_test_support import fixture_runtime_id, StaffTestCase, production_membership, unit_membership, showrunner_relationship


class WorkerFillTests(StaffTestCase):
    def recorded_production(self, slug: str, *units: str) -> Path:
        doc = self.production(slug, *units)
        owner = showrunner_relationship(slug)
        _ = staff.set_doc(owner.id, staff.LiveDoc("natedev", doc))
        return doc


    def test_admission_uses_current_unit_membership_and_is_unchanged_on_retry(self) -> None:
        _ = self.recorded_production("demo", "models-unit")
        production_membership("demo", "models-unit")
        parent = unit_membership("demo", "models-unit")
        run = self.worker_run("demo", "models-unit", "director")
        row = worker_fill.admit("hana_catalyst-impl", run, role='impl')
        self.assertEqual(row.parent, parent.id)
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(worker_fill.admit("hana_catalyst-impl", run, role='impl'), row)
        self.assertEqual((output.getvalue(), errors.getvalue()), ("", ""))
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)


    def test_staff_id_admits_without_reading_an_unrelated_unreadable_pane_record(self) -> None:
        production_membership("demo", "models-unit")
        parent = unit_membership("demo", "models-unit")
        run = self.worker_run("demo", "models-unit", "director")
        panes = Path(os.environ["HERDR_PANE_RECORD_DIR"]) / "agents.json"
        panes.parent.mkdir(parents=True, exist_ok=True)
        _ = panes.write_text("{unreadable unrelated pane")
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": parent.id}):
            allowed = worker_fill.admit("impl", run, role='impl')
            self.assertEqual(staff.find(kind="worker", parent=parent.id), [])
            admitted = worker_fill.publish(allowed, fixture_runtime_id("published-test-worker"))
        self.assertEqual(admitted.row.parent, staff.ReportsTo(parent.id))
        self.assertEqual(staff.find(kind="worker", parent=parent.id), [admitted.row])
        self.assertEqual(panes.read_text(), "{unreadable unrelated pane")


    def test_staff_id_admits_without_reading_malformed_fallback_run_state(self) -> None:
        production_membership("demo", "models-unit")
        parent = unit_membership("demo", "models-unit")
        run = self.worker_run("demo", "models-unit", "director")
        state = run / "progress_history_state.json"
        _ = state.write_text("{malformed fallback state")
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": parent.id}):
            allowed = worker_fill.admit("impl", run, role='impl')
            self.assertEqual(staff.find(kind="worker", parent=parent.id), [])
            admitted = worker_fill.publish(allowed, fixture_runtime_id("published-test-worker"))
        self.assertEqual(admitted.row.parent, staff.ReportsTo(parent.id))
        self.assertEqual(staff.find(kind="worker", parent=parent.id), [admitted.row])
        self.assertEqual(state.read_text(), "{malformed fallback state")

    def test_retired_unit_cannot_admit_a_worker(self) -> None:
        _ = self.recorded_production("demo", "models-unit")
        production_membership("demo", "models-unit")
        parent = unit_membership("demo", "models-unit")
        _ = staff.set_worktree(parent.id, staff.RemovedWorktree("natedev", self.root / "gone"), expected=parent.worktree)
        run = self.worker_run("demo", "models-unit", "director")
        with redirect_stderr(io.StringIO()) as errors:
            allowed = worker_fill.admit("impl", run, role="impl")
        self.assertEqual(allowed.parent, parent.id)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn("retired", errors.getvalue())
        self.assertEqual(staff.find(kind="worker"), [])
