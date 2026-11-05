"""Worker publication uses the current staff row."""
from __future__ import annotations

import io
import json
import os
import sys
import subprocess
from pathlib import Path
from contextlib import redirect_stderr
from unittest import mock

from scripts.production import staff, worker_admission
from scripts.production.staff_test_support import CHECKOUT, fixture_runtime_id, StaffTestCase, production_membership, unit_membership, showrunner_relationship, transcript_fixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class WorkerFillTests(StaffTestCase):
    def test_uncertain_claude_dispatch_requires_readable_evidence_before_resending(self) -> None:
        _, unit, row = self.tree()
        for case, torn, matching, resend in (("matched", True, True, False),
                                             ("unknown", True, False, False),
                                             ("absent", False, False, True)):
            with self.subTest(case=case):
                run = self.root / case
                run.mkdir()
                prompt = run / "prompt.md"
                _ = prompt.write_text("Complete the private assignment.")
                source = transcript_fixture(row, staff.ClaudeBackend())[0]
                text = prompt.read_text() if matching else "An unrelated assignment."
                _ = source.write_text(('{"torn":\n' if torn else "") + json.dumps({
                    "type": "user", "message": {"role": "user", "content": text}}) + "\n")
                _ = (run / f"{row.id}.handoff.md").write_text(f"Full source transcript: {source}\n")
                allowed = worker_admission.WorkerStartAllowed(row.name, unit.id, run,
                    worker_admission.ExistingWorkerSlot(row))
                worker_admission.record_dispatch(allowed, worker_admission.assignment_id(prompt), worker_admission.SwitchState.DISPATCH_DEFERRED)
                before = self.staff_file.read_bytes(), source.read_bytes()
                result = subprocess.run([sys.executable, str(CHECKOUT / "scripts/production/worker_fill.py"),
                    "coordinate-claude", "--name", row.name, "--run", str(run), "--role", "impl",
                    "--prompt", str(prompt), "--staff-id", row.id],
                    input=row.current_session + "\ndispatched\n", capture_output=True, text=True,
                    timeout=TEST_HANG_GUARD_SECS, check=False)
                self.assertEqual(result.returncode, 2 if torn and not matching else 0, result.stderr)
                if torn and not matching:
                    self.assertIn("delivery is unknown", result.stderr)
                self.assertEqual(result.stdout.splitlines(),
                                 ["attach", str(run / f"{row.id}.opening.md"), "published"]
                                 if resend else [] if torn and not matching else ["unchanged"])
                if resend:
                    opening = (run / f"{row.id}.opening.md").read_text()
                    self.assertIn(prompt.read_text(), opening)
                    self.assertLess(opening.index(str(run / f"{row.id}.handoff.md")), opening.index(str(source)))
                self.assertEqual((self.staff_file.read_bytes(), source.read_bytes()), before)
                self.assertEqual(worker_admission.dispatch_record(allowed).state,
                                 worker_admission.SwitchState.DISPATCH_DEFERRED if torn and not matching
                                 else worker_admission.SwitchState.ASSIGNMENT_ACCEPTED)

    def test_staff_id_admits_without_reading_unrelated_malformed_evidence(self) -> None:
        production_membership("demo", "models-unit")
        parent = unit_membership("demo", "models-unit")
        run = self.worker_run("demo", "models-unit", "director")
        for evidence in (Path(os.environ["HERDR_PANE_RECORD_DIR"]) / "agents.json",
                         run / "progress_history_state.json"):
            with self.subTest(evidence=evidence.name):
                evidence.parent.mkdir(parents=True, exist_ok=True)
                _ = evidence.write_text("{malformed unrelated evidence")
                self.assertEqual(staff.find(kind="worker", parent=parent.id), [])
                with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": parent.id}):
                    allowed = worker_admission.admit("impl", run, role="impl")
                    self.assertEqual(staff.find(kind="worker", parent=parent.id), [])
                    admitted = worker_admission.publish(allowed, fixture_runtime_id("published-test-worker"))
                self.assertEqual(admitted.row.parent, staff.ReportsTo(parent.id))
                self.assertEqual(staff.find(kind="worker", parent=parent.id), [admitted.row])
                self.assertEqual(evidence.read_text(), "{malformed unrelated evidence")
                evidence.unlink()
                _ = staff.retire(admitted.row.id)

    def test_retired_unit_cannot_admit_a_worker(self) -> None:
        doc = self.production("demo", "models-unit")
        _ = staff.set_doc(showrunner_relationship("demo").id, staff.LiveDoc("natedev", doc))
        production_membership("demo", "models-unit")
        parent = unit_membership("demo", "models-unit")
        _ = staff.set_worktree(parent.id, staff.RemovedWorktree("natedev", self.root / "gone"), expected=parent.worktree)
        run = self.worker_run("demo", "models-unit", "director")
        with redirect_stderr(io.StringIO()) as errors:
            allowed = worker_admission.admit("impl", run, role="impl")
        self.assertEqual(allowed.parent, parent.id)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn("retired", errors.getvalue())
        self.assertEqual(staff.find(kind="worker"), [])
