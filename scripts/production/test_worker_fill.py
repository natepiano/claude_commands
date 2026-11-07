"""Worker publication uses the current staff row."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from scripts.production import staff, worker_admission
from scripts.direct import claude_worker
from scripts.production.staff_test_support import fixture_member, fixture_runtime_id, StaffTestCase, production_membership, unit_membership, recorded_showrunner, transcript_fixture
from scripts.message import sessions


class WorkerFillTests(StaffTestCase):
    def turn(self, row: staff.StaffRow, run: Path, prompt: Path) -> claude_worker.ClaudeTurn:
        sender = self.root / "send.py"
        _ = sender.write_text("print('SENT: fixture')\n")
        environment = {**os.environ, "CLAUDE_BIN": "/bin/true", "CLAUDE_WORKER_SEND_SCRIPT": str(sender)}
        return claude_worker.ClaudeTurn(row.name, run, self.root, prompt, run / "summary", run / "log",
                                        "", "", "", row.id, False, environment)

    def test_claude_retry_without_a_transcript_keeps_the_session_and_delivers_the_saved_handoff(self) -> None:
        runner = fixture_member("showrunner", name="sdlc")
        unit = fixture_member("unit", name="models", parent=runner.id)
        native = staff.native_id(fixture_runtime_id("published-claude-without-transcript"))
        row = staff.ensure_worker(native, "impl", unit.id)
        registry = Path(os.environ["NOTIFIER_SESSIONS_DIR"]) / f"{os.getpid()}.json"
        _ = registry.write_text(json.dumps({
            "pid": os.getpid(), "sessionId": native, "name": row.name,
            "messagingSocketPath": "", "procStart": sessions.process_start(os.getpid()), "updatedAt": 1,
        }))
        run = self.root / "published-retry"
        run.mkdir()
        prompt = run / "prompt.md"
        _ = prompt.write_text("Complete the assignment after the family switch.")
        source = run / "source.jsonl"
        _ = source.write_text("Source conversation before the switch.\n")
        extract = worker_admission.handoff_path(run, row)
        _ = extract.write_text(f"{worker_admission.HANDOFF_SOURCE_PREFIX}{source}\nSaved source conversation.\n")
        allowed = worker_admission.WorkerStartAllowed(row.name, unit.id, run, worker_admission.ExistingWorkerSlot(row))
        worker_admission.record_dispatch(allowed, worker_admission.assignment_id(prompt),
                                         worker_admission.SwitchState.DISPATCH_IN_FLIGHT)
        before = self.staff_file.read_bytes()
        self.assertEqual(sessions.transcript_locations(native), [])
        result = claude_worker.open_worker(self.turn(row, run, prompt))
        self.assertIsInstance(result, claude_worker.Delivered)
        assert isinstance(result, claude_worker.Delivered)
        self.assertEqual(result.native_id, row.current_session)
        prepared = (run / f"{row.id}.opening.md").read_text()
        self.assertIn(prompt.read_text(), prepared)
        self.assertLess(prepared.index(str(extract)), prepared.index(prompt.read_text()))
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertEqual(staff.by_id(row.id), row)
        self.assertEqual(worker_admission.dispatch_record(allowed.run, allowed.name).state, worker_admission.SwitchState.ASSIGNMENT_ACCEPTED)

    def test_claude_retry_reports_one_unknown_dispatch_state_and_completes_dispatch(self) -> None:
        _, unit, row = self.tree()
        run = self.root / "retry"
        run.mkdir()
        prompt = run / "prompt.md"
        _ = prompt.write_text("Complete the retry assignment.")
        source = transcript_fixture(row, staff.ClaudeBackend())[0]
        _ = worker_admission.handoff_path(run, row).write_text(f"{worker_admission.HANDOFF_SOURCE_PREFIX}{source}\n")
        _ = (run / "mesh_roster.json").write_text(json.dumps({row.name: {
            "staff_id": row.id, "assignment_id": worker_admission.assignment_id(prompt), "dispatch_state": "invalid"}}))
        with redirect_stderr(io.StringIO()) as errors:
            result = claude_worker.open_worker(self.turn(row, run, prompt))
        self.assertIsInstance(result, claude_worker.Delivered)
        assert isinstance(result, claude_worker.Delivered)
        self.assertEqual(result.native_id, row.current_session)
        self.assertEqual(errors.getvalue().splitlines(),
                         [f"anomaly: {row.name}: unknown dispatch_state 'invalid'; using NO_RECORD"])
        allowed = worker_admission.WorkerStartAllowed(row.name, unit.id, run, worker_admission.ExistingWorkerSlot(row))
        self.assertEqual(worker_admission.dispatch_record(allowed.run, allowed.name).state, worker_admission.SwitchState.ASSIGNMENT_ACCEPTED)

    def test_uncertain_claude_dispatch_requires_readable_evidence_before_resending(self) -> None:
        _, unit, row = self.tree()
        for case, torn, matching in (("matched", True, True), ("unknown", True, False), ("absent", False, False)):
            with self.subTest(case=case):
                run = self.root / case
                run.mkdir()
                prompt = run / "prompt.md"
                _ = prompt.write_text("Complete the private assignment.")
                source = transcript_fixture(row, staff.ClaudeBackend())[0]
                text = prompt.read_text() if matching else "An unrelated assignment."
                _ = source.write_text(('{"torn":\n' if torn else "") + json.dumps({
                    "type": "user", "message": {"role": "user", "content": text}}) + "\n")
                _ = worker_admission.handoff_path(run, row).write_text(f"{worker_admission.HANDOFF_SOURCE_PREFIX}{source}\n")
                allowed = worker_admission.WorkerStartAllowed(row.name, unit.id, run,
                    worker_admission.ExistingWorkerSlot(row))
                worker_admission.record_dispatch(allowed, worker_admission.assignment_id(prompt), worker_admission.SwitchState.DISPATCH_IN_FLIGHT)
                before = self.staff_file.read_bytes(), source.read_bytes()
                refused = case == "unknown"
                with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                    result = claude_worker.open_worker(self.turn(row, run, prompt))
                output, errors = out.getvalue(), err.getvalue()
                self.assertIsInstance(result, claude_worker.NotDelivered if refused else
                                      claude_worker.Delivered if case == "absent" else claude_worker.Unchanged)
                if isinstance(result, claude_worker.NotDelivered):
                    self.assertEqual(result.code, 2)
                if refused:
                    self.assertIn("delivery is unknown", errors)
                self.assertEqual(output.splitlines(), ["unchanged"] if case == "matched" else [])
                if case == "absent":
                    assert isinstance(result, claude_worker.Delivered)
                    self.assertEqual(result.native_id, row.current_session)
                    opening = (run / f"{row.id}.opening.md").read_text()
                    self.assertIn(prompt.read_text(), opening)
                    self.assertLess(opening.index(str(worker_admission.handoff_path(run, row))), opening.index(str(source)))
                self.assertEqual((self.staff_file.read_bytes(), source.read_bytes()), before)
                self.assertEqual(worker_admission.dispatch_record(allowed.run, allowed.name).state,
                                 worker_admission.SwitchState.DISPATCH_IN_FLIGHT if refused
                                 else worker_admission.SwitchState.ASSIGNMENT_ACCEPTED)

    def test_staff_id_admits_without_reading_unrelated_malformed_evidence(self) -> None:
        production_membership("demo", "models")
        parent = unit_membership("demo", "models")
        run = self.worker_run("demo", "models", "director")
        for evidence in (Path(os.environ["HERDR_PANE_RECORD_DIR"]) / "agents.json",
                         run / "progress_history_state.json"):
            with self.subTest(evidence=evidence.name):
                evidence.parent.mkdir(parents=True, exist_ok=True)
                _ = evidence.write_text("{malformed unrelated evidence")
                self.assertEqual(staff.find(kind="worker", parent=parent.id), [])
                with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": parent.id}):
                    allowed = worker_admission.admit("impl", run)
                    self.assertEqual(staff.find(kind="worker", parent=parent.id), [])
                    admitted = worker_admission.publish(allowed, fixture_runtime_id("published-test-worker"))
                self.assertEqual(admitted.row.parent, staff.ReportsTo(parent.id))
                self.assertEqual(staff.find(kind="worker", parent=parent.id), [admitted.row])
                self.assertEqual(evidence.read_text(), "{malformed unrelated evidence")
                evidence.unlink()
                _ = staff.retire(admitted.row.id)

    def test_retired_unit_cannot_admit_a_worker(self) -> None:
        doc = self.production("demo", "models")
        _ = recorded_showrunner("demo", doc)
        production_membership("demo", "models")
        parent = unit_membership("demo", "models")
        _ = staff.set_worktree(parent.id, staff.RemovedWorktree("natedev", self.root / "gone"), expected=parent.worktree)
        run = self.worker_run("demo", "models", "director")
        self.write_json(run / "progress_history_state.json", {
            "owner_staff_id": parent.id, "main_agent": {"family": "claude", "session_id": "director"},
        })
        with redirect_stderr(io.StringIO()) as errors:
            allowed = worker_admission.admit("impl", run)
        self.assertEqual(allowed.parent, parent.id)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn("retired", errors.getvalue())
        self.assertEqual(staff.find(kind="worker"), [])
