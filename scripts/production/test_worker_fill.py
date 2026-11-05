"""Worker publication uses the current staff row."""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from scripts.production import staff, worker_admission, worker_fill
from scripts.production.staff_test_support import fixture_runtime_id, StaffTestCase, production_membership, unit_membership, showrunner_relationship, transcript_fixture


class WorkerFillTests(StaffTestCase):
    def test_uncertain_claude_dispatch_requires_readable_evidence_before_resending(self) -> None:
        _, unit, row = self.tree()
        launcher = self.root / "fixture_launcher.py"
        _ = launcher.write_text('''import os, socket, sys
from pathlib import Path
with socket.socket(fileno=int(os.environ["WORKER_SWITCH_FD"])) as channel:
    channel.sendall((sys.argv[-2] + "\\n").encode())
    with channel.makefile("rb") as control:
        assert control.readline() == b"published\\n"
    with Path(sys.argv[-1]).open("a") as sent:
        sent.write("assignment dispatched\\n")
    channel.sendall(b"dispatched\\n")
''')
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
                worker_admission.record_dispatch(allowed, worker_admission.assignment_id(prompt), "sending")
                sent = run / "dispatched.txt"
                _ = sent.write_text("")
                before = self.staff_file.read_bytes(), source.read_bytes()
                command = [sys.executable, str(launcher), row.current_session, str(sent)]
                if torn and not matching:
                    with self.assertRaisesRegex(worker_admission.AdmissionRefused, "delivery is unknown"):
                        _ = worker_fill.launch_claude(row.name, run, "impl", command, prompt, row.id)
                else:
                    self.assertEqual(worker_fill.launch_claude(row.name, run, "impl", command, prompt, row.id), 0)
                self.assertEqual(sent.read_text().splitlines(), ["assignment dispatched"] if resend else [])
                self.assertEqual((self.staff_file.read_bytes(), source.read_bytes()), before)
                self.assertEqual(worker_admission.dispatch_record(allowed)["dispatch_state"],
                                 "sending" if torn and not matching else "accepted")

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
