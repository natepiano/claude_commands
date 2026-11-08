"""Permission is read-only; backend identities are published before work is sent."""
from __future__ import annotations

import os
import json
import fcntl
import io
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import Literal
from unittest import mock

from scripts.agents import worker_lifecycle
from scripts.lib import unit_runs
from scripts.production import fake_unit_host, session_staff, staff, worker_admission, showrunners
from scripts.production.staff_test_support import (
    CHECKOUT, StaffTestCase, fixture_runtime_id, fixture_session, fixture_worker, transcript_fixture,
)
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.shutdown import stop_resume


class RecordedDirectorAdmissionTests(StaffTestCase):
    def test_missing_run_owner_and_caller_report_what_is_missing(self) -> None:
        _ = self.tree()
        run = self.root / "ownerless-run"
        run.mkdir()
        record = staff.snapshot()
        with self.assertRaises(ValueError) as missing_run:
            _ = worker_admission.parent(record, run=run)
        self.assertEqual(str(missing_run.exception),
                         f"this run has no owner because {unit_runs.STATE_FILE} and mesh_roster.json name no unit, production or staff row")
        with self.assertRaises(ValueError) as missing_caller:
            _ = worker_admission.parent(record)
        self.assertEqual(str(missing_caller.exception), "the caller has no staff row and names no unit or production")

    def test_parent_prints_a_state_read_anomaly_once(self) -> None:
        unit_runs.report_anomaly.cache_clear()
        self.addCleanup(unit_runs.report_anomaly.cache_clear)
        _, unit, _ = self.tree()
        run = self.root / "bad-state"
        run.mkdir()
        _ = (run / unit_runs.STATE_FILE).write_text("[]")
        record = staff.snapshot()
        with redirect_stderr(io.StringIO()) as errors:
            for _ in range(2):
                self.assertEqual(worker_admission.parent(record, staff_id=unit.current_session, run=run), unit)
        self.assertEqual(errors.getvalue().splitlines(),
                         [f"anomaly: {run / unit_runs.STATE_FILE}: worker state unreadable: expected a JSON object"])

    def test_run_with_owner_staff_id_resolves_its_known_unit(self) -> None:
        production, unit, _ = self.tree()
        run = self.root / "run-with-owner"
        run.mkdir()
        _ = (run / unit_runs.STATE_FILE).write_text(json.dumps({
            "production": production.name, "unit": unit.name,
            "owner_staff_id": unit.id,
        }))
        self.assertEqual(worker_admission.parent(staff.snapshot(), run=run), unit)

    def test_run_owner_resolves_the_renamed_unit_despite_old_state_names(self) -> None:
        production, unit, _ = self.tree()
        run = self.root / "renamed-owner"
        run.mkdir()
        _ = (run / unit_runs.STATE_FILE).write_text(json.dumps({
            "production": production.name, "unit": unit.name, "owner_staff_id": unit.id,
        }))
        renamed = staff.rename(unit.id, "renamed-unit")
        self.assertEqual(worker_admission.parent(staff.snapshot(), run=run), renamed)

    def test_worker_caller_resolves_its_fixed_parent_after_session_replacement(self) -> None:
        _, unit, worker = self.tree()
        current = staff.native_id(fixture_runtime_id("dispatching-worker-current-session"))
        _ = staff.update_current_session(worker.current_session, current)
        before = self.staff_file.read_bytes()
        found = worker_admission.parent(staff.snapshot(), staff_id=current)
        self.assertEqual(found, unit)
        self.assertEqual(self.staff_file.read_bytes(), before)


class WorkerAdmissionTests(StaffTestCase):
    def test_rename_journal_cannot_select_a_holder_by_name_alone(self) -> None:
        owner, _, _ = self.tree()
        state = self.root / "showrunner-state"
        journal = state / "new" / "rename.json"
        journal.parent.mkdir(parents=True)
        _ = journal.write_text(json.dumps({"old": "old", "new": owner.name}))
        with mock.patch.object(showrunners, "SHOWRUNNER_STATE_DIR", state):
            found = session_staff.MemberSources(()).member(
                session_staff.ShowrunnerMember("old"), staff.snapshot(),
            )
        self.assertIsInstance(found, session_staff.NoSuchMember)

    def test_valid_worker_roles_are_retained_by_read_only_start_permission(self) -> None:
        _, unit, _ = self.tree()
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        for supplied in staff.WORKER_ROLES:
            with (self.subTest(role=supplied),
                  mock.patch.dict(os.environ, {"UNIT_TEAM_ROLE": supplied}),
                  mock.patch.object(staff, "runtime_id", return_value=unit.current_session)):
                allowed = worker_admission.admit("new-worker", self.root)
                self.assertEqual((allowed.name, allowed.parent, allowed.slot),
                                 ("new-worker", unit.id, worker_admission.NewWorkerSlot()))
                self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_start_permission_creates_no_row_and_publication_uses_the_returned_thread(self) -> None:
        _, unit, _ = self.tree()
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        with mock.patch.object(staff, "runtime_id", return_value=unit.id):
            allowed = worker_admission.admit("test", self.root)
        self.assertIsInstance(allowed, worker_admission.WorkerStartAllowed)
        self.assertEqual((allowed.name, allowed.parent, allowed.slot),
                         ("test", unit.id, worker_admission.NewWorkerSlot()))
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
            allowed = worker_admission.admit("test", self.root)
        self.assertEqual(allowed.parent, unit.id)
        self.assertEqual(staff.by_id(unit.id), changed)
        self.assertEqual(self.staff_file.read_bytes(), before)
        worker = worker_admission.publish(allowed, fixture_runtime_id("worker-of-codex"))
        self.assertEqual(worker.row.parent, staff.ReportsTo(unit.id))
        self.assertEqual(staff.by_current_session(current), changed)
        self.assertIsNone(staff.by_current_session(unit.id))

    def test_permission_is_read_only_and_publication_uses_the_fixed_parent(self) -> None:
        _, unit, existing = self.tree()
        for replaced in (False, True):
            if replaced:
                previous = unit.current_session
                unit = staff.update_current_session(previous, staff.native_id(fixture_runtime_id("replaced-director")))
                self.assertIsNone(staff.by_current_session(previous))
            with self.subTest(replaced=replaced), mock.patch.dict(os.environ, {"CODEX_THREAD_ID": unit.current_session}):
                before = self.staff_state()
                allowed = worker_admission.admit("new-worker", self.root)
                with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                    repeated = worker_admission.admit("new-worker", self.root)
                self.assertEqual(repeated, allowed)
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(errors.getvalue(), "")
                self.assertEqual((allowed.name, allowed.parent, allowed.slot),
                                 ("new-worker", unit.id, worker_admission.NewWorkerSlot()))
                self.assertEqual(self.staff_state(), before)
                self.assertEqual(staff.find(kind="worker", parent=unit.id), [existing])
            thread = fixture_runtime_id(f"returned-{replaced}")
            published = worker_admission.publish(allowed, thread)
            self.assertEqual((published.row.id, published.row.name, published.row.parent),
                             (thread, "new-worker", staff.ReportsTo(unit.id)))
            self.assertEqual(staff.by_id(staff.StaffId(thread)), published.row)
            self.assertEqual(staff.by_id(unit.id), unit)
            self.assertEqual(staff.by_current_session(unit.current_session), unit)
            after = self.staff_state()
            self.assertEqual(worker_admission.publish(allowed, thread), published)
            self.assertEqual(self.staff_state(), after)
            _ = staff.retire(published.row.id)

    def test_replacement_changes_only_current_session_and_leaves_the_fixed_graph_unchanged(self) -> None:
        _, unit, old = self.tree()
        sibling = staff.ensure_worker(staff.native_id(fixture_runtime_id("sibling-thread")), "test", unit.id)
        with mock.patch.object(staff, "runtime_id", return_value=unit.id):
            allowed = worker_admission.admit(old.name, self.root)
        thread = fixture_runtime_id("replacement-thread")
        before = self.staff_file.read_bytes()
        result = worker_admission.publish(allowed, thread)
        self.assertEqual(result.row, replace(old, current_session=staff.StaffId(thread)))
        self.assertEqual(staff.by_id(old.id), result.row)
        self.assert_only_current_session_changed(before, old, thread)
        self.assertIsNone(staff.by_current_session(old.current_session))
        self.assertEqual(staff.by_current_session(thread), result.row)
        self.assertEqual(staff.by_id(sibling.id), sibling)
        self.assertEqual(staff.by_id(unit.id), unit)


class FamilyHandoffTests(StaffTestCase):
    def test_claude_transcript_entries_ignore_api_request_blobs(self) -> None:
        for kind, expected in (("api-request-blob", ""), ("user", "user:\nThe request"),
                               ("assistant", "assistant:\nThe request")):
            with self.subTest(kind=kind):
                self.assertEqual(worker_admission.transcript_entry({
                    "type": kind,
                    "message": {"role": "assistant" if kind == "assistant" else "user",
                                "content": [{"type": "text", "text": "The request"}]},
                }, set()), expected)

    def source(self, family: Literal["claude", "codex"], *, turns: int = 1,
               malformed: bool = False) -> tuple[worker_admission.WorkerStartAllowed, Path,
                                                tuple[list[str], list[str], list[str]]]:
        _, unit, _ = self.tree()
        tree = self.root / "worktree"
        tree.mkdir(exist_ok=True)
        _ = staff.set_worktree(unit.id, staff.PresentWorktree("natedev", tree), expected=unit.worktree)
        backend = staff.ClaudeBackend() if family == "claude" else staff.CodexBackend(self.root / "codex")
        row = fixture_worker("switch-" + family, unit.id, backend=backend)
        path, evidence = transcript_fixture(row, backend, turns=turns, malformed=malformed)
        run = self.root / ("run-" + family)
        run.mkdir(exist_ok=True)
        return worker_admission.WorkerStartAllowed(row.name, unit.id, run,
            worker_admission.ExistingWorkerSlot(row)), path, evidence

    def test_dispatch_update_preserves_a_request_replaced_before_the_exclusive_lock(self) -> None:
        allowed, _, _ = self.source("claude")
        worker_admission.record_dispatch(allowed, "old-request", worker_admission.SwitchState.DISPATCH_IN_FLIGHT)
        original = worker_lifecycle.acquire_flock
        saved: list[bytes] = []
        path = allowed.run / "mesh_roster.json"

        def supersede(handle: int | worker_lifecycle.LockFileDescriptor, *, mode: int = fcntl.LOCK_EX,
                      deadline: float = worker_lifecycle.NO_LOCK_DEADLINE) -> None:
            if mode == fcntl.LOCK_EX and not saved:
                with mock.patch.object(worker_lifecycle, "acquire_flock", original):
                    worker_admission.record_dispatch(allowed, "new-request",
                                                     worker_admission.SwitchState.CREATION_INTENT_RECORDED)
                saved.append(path.read_bytes())
            original(handle, mode=mode, deadline=deadline)

        with mock.patch.object(worker_lifecycle, "acquire_flock", side_effect=supersede):
            worker_admission.record_dispatch(allowed, "old-request",
                worker_admission.SwitchState.ASSIGNMENT_ACCEPTED, expected_request="old-request")
        self.assertEqual(path.read_bytes(), saved[0])
        self.assertEqual(worker_admission.dispatch_record(allowed.run, allowed.name), worker_admission.DispatchRecord(
            "new-request", worker_admission.SwitchState.CREATION_INTENT_RECORDED))
        worker_admission.record_dispatch(allowed, "new-request",
            worker_admission.SwitchState.ASSIGNMENT_ACCEPTED, expected_request="new-request")
        self.assertEqual(worker_admission.dispatch_record(allowed.run, allowed.name), worker_admission.DispatchRecord(
            "new-request", worker_admission.SwitchState.ASSIGNMENT_ACCEPTED))

    def test_pending_dispatch_accepts_an_unset_state_and_reports_an_unreadable_roster(self) -> None:
        allowed, _, _ = self.source("claude")
        prompt = allowed.run / "assignment.txt"
        _ = prompt.write_text("Complete this assignment.\n")
        worker_admission.record_dispatch(allowed, worker_admission.assignment_id(prompt), worker_admission.SwitchState.NO_RECORD)
        self.assertTrue(worker_admission.dispatch_pending(allowed.run, allowed.name, prompt))
        path = allowed.run / "mesh_roster.json"
        _ = path.write_text("{torn")
        with redirect_stderr(io.StringIO()) as errors:
            pending = worker_admission.dispatch_pending(allowed.run, allowed.name, prompt)
        self.assertFalse(pending)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn(f"anomaly: {allowed.name}:", errors.getvalue())
        self.assertIn(str(path), errors.getvalue())

    def test_unknown_dispatch_state_reports_an_anomaly_and_allows_admission(self) -> None:
        allowed, _, _ = self.source("claude")
        _ = (allowed.run / "mesh_roster.json").write_text(json.dumps({allowed.name: {"dispatch_state": "invalid"}}))
        before = self.staff_state()
        with redirect_stderr(io.StringIO()) as errors:
            record = worker_admission.dispatch_record(allowed.run, allowed.name)
            repeated = worker_admission.admit(allowed.name, allowed.run, reference=allowed.lock_reference)
        self.assertEqual(errors.getvalue().splitlines(),
                         [f"anomaly: {allowed.name}: unknown dispatch_state 'invalid'; using NO_RECORD"])
        self.assertEqual(record.state, worker_admission.SwitchState.NO_RECORD)
        self.assertEqual(repeated, allowed)
        self.assertEqual(self.staff_state(), before)

    def test_source_run_discovery_only_follows_an_idle_codex_source(self) -> None:
        cases: tuple[tuple[Literal["claude", "codex"], bool], ...] = (("claude", False), ("codex", True))
        for family, stopped in cases:
            allowed, _, _ = self.source(family)
            assert isinstance(allowed.slot, worker_admission.ExistingWorkerSlot)
            row = allowed.slot.row
            result = stop_resume.Stopped(row.current_session, 123) if stopped else stop_resume.NotRunning(row.current_session)
            identity = fixture_runtime_id("lazy-" + family)
            fixture_session(identity, staff.ClaudeBackend() if family == "codex" else staff.CodexBackend(self.root / "destination"))
            with (mock.patch.object(stop_resume, "stop", return_value=result),
                  mock.patch.object(session_staff, "worker_run_index", side_effect=AssertionError("unnecessary run discovery"))):
                _ = worker_admission.switch_worker(allowed, "claude" if family == "codex" else "codex",
                    start=lambda handoff: identity, dispatch=lambda published: worker_admission.SwitchState.ASSIGNMENT_ACCEPTED, cleanup=lambda identity: None)

    def test_sibling_slots_can_launch_together_and_equal_slots_refuse_contention(self) -> None:
        _, unit, _ = self.tree()
        for equal in (False, True):
            name = "equal" if equal else "first"
            sibling = name if equal else "second"
            allowed = worker_admission.WorkerStartAllowed(name, unit.id, self.root, worker_admission.NewWorkerSlot())
            identity = fixture_runtime_id("slot-" + name)
            fixture_session(identity, staff.ClaudeBackend())
            program = 'import sys\nfrom pathlib import Path\nfrom scripts.production import staff, worker_admission as admission\nfrom scripts.production.staff_test_support import fixture_session, fixture_runtime_id\nallowed = admission.WorkerStartAllowed(sys.argv[2], staff.StaffId(sys.argv[1]), Path(sys.argv[3]), admission.NewWorkerSlot())\nidentity = fixture_runtime_id("slot-child-" + sys.argv[2])\nfixture_session(identity, staff.ClaudeBackend())\ntry:\n    admission.switch_worker(allowed, "claude", start=lambda handoff: identity, dispatch=admission.DispatchOwner.TURN_SUBMISSION, cleanup=lambda identity: None)\nexcept admission.AdmissionRefused:\n    raise SystemExit(2)\n'
            def start(_handoff: worker_admission.FamilyHandoff) -> str:
                child = subprocess.run([sys.executable, "-c", program, unit.id, sibling, str(self.root)],
                    env=staff.child_environment(os.environ), cwd=CHECKOUT, capture_output=True, text=True,
                    timeout=TEST_HANG_GUARD_SECS, check=False)
                self.assertEqual(child.returncode, 2 if equal else 0, child.stdout + child.stderr)
                return identity

            _ = worker_admission.switch_worker(allowed, "claude", start=start, dispatch=worker_admission.DispatchOwner.TURN_SUBMISSION, cleanup=lambda identity: None)
            self.assertEqual(len([row for row in staff.local_snapshot().find(kind="worker", parent=unit.id) if row.name == name]), 1)

    def test_both_formats_keep_complete_histories_and_report_malformed_records_once(self) -> None:
        for family in ("claude", "codex"):
            for malformed in (False, True):
                with self.subTest(family=family, malformed=malformed):
                    allowed, source, evidence = self.source(family, turns=90 if not malformed else 3,
                                                            malformed=malformed)
                    assert isinstance(allowed.slot, worker_admission.ExistingWorkerSlot)
                    patch = "*** Begin Patch\n*** Update File: old.py\n*** Move to: moved.py\n@@\n-old\n+new\n*** End Patch"
                    calls = ["await tools.apply_patch(" + json.dumps(patch) + ");",
                             "const example = " + json.dumps("tools.apply_patch(" + json.dumps(patch.replace("old.py", "quoted.py")) + ")") + ";"]
                    with source.open("a") as transcript:
                        for call in calls:
                            _ = transcript.write(json.dumps({"type": "response_item", "payload": {
                                "type": "custom_tool_call", "name": "exec", "input": call}}) + "\n")
                    before = source.read_bytes(), self.staff_file.read_bytes()
                    with redirect_stderr(io.StringIO()) as errors:
                        handoff = worker_admission.extract_transcript(allowed.slot.row, allowed.run,
                            staff.ClaudeBackend() if family == "claude" else staff.CodexBackend(self.root / "codex"))
                    assert isinstance(handoff, worker_admission.TranscriptHandoff)
                    text = handoff.extract.read_text()
                    for expected in (*evidence[0], *evidence[1], *evidence[2]):
                        self.assertIn(expected, text)
                    self.assertIn("old.py", text)
                    self.assertIn("moved.py", text)
                    self.assertNotIn("quoted.py", text)
                    self.assertNotIn("await tools", text)
                    self.assertNotIn("PRIVATE_TOOL_REQUEST", text)
                    self.assertNotIn("PRIVATE_TOOL_OUTPUT", text)
                    self.assertEqual(handoff.source, source)
                    self.assertTrue(handoff.extract.is_relative_to(allowed.run))
                    prompt = handoff.opening()
                    self.assertLess(prompt.index(str(handoff.extract)), prompt.index(str(source)))
                    self.assertIn("only when the extract does not answer", prompt)
                    self.assertEqual((source.read_bytes(), self.staff_file.read_bytes()), before)
                    self.assertEqual(len(errors.getvalue().splitlines()), int(malformed), errors.getvalue())
                    if malformed:
                        self.assertIn(allowed.name, errors.getvalue())

    def test_unavailable_transcript_reports_one_anomaly(self) -> None:
        allowed, source, _ = self.source("claude")
        assert isinstance(allowed.slot, worker_admission.ExistingWorkerSlot)
        for unavailable in ("unreadable", "missing"):
            with self.subTest(unavailable=unavailable):
                if unavailable == "unreadable":
                    _ = source.write_bytes(b"\xff")
                else:
                    source.unlink()
                with redirect_stderr(io.StringIO()) as errors:
                    handoff = worker_admission.extract_transcript(allowed.slot.row, allowed.run, staff.ClaudeBackend())
                assert isinstance(handoff, worker_admission.TranscriptUnavailable)
                self.assertTrue(handoff.reason)
                self.assertEqual(len(errors.getvalue().splitlines()), 1, errors.getvalue())

    def test_switch_preserves_staff_graph_slot_folder_and_held_messages_before_dispatch(self) -> None:
        families: tuple[Literal["claude", "codex"], ...] = ("claude", "codex")
        for family in families:
            interrupted = family == "claude"
            with self.subTest(family=family, interrupted=interrupted):
                allowed, source, evidence = self.source(family)
                assert isinstance(allowed.slot, worker_admission.ExistingWorkerSlot)
                old = allowed.slot.row
                destination = "codex" if family == "claude" else "claude"
                identity = fixture_runtime_id("new-" + destination)
                inbox = session_staff.inbox_path(old.id)
                inbox.parent.mkdir(parents=True, exist_ok=True)
                _ = inbox.write_text(json.dumps({"messages": ["held work"], "accepting_messages": True}))
                previous_run = self.root / (f"previous-director-{family}-{interrupted}")
                previous_run.mkdir()
                reference = previous_run / "mesh_roster.json"
                _ = reference.write_text(json.dumps({old.name: {"staff_id": old.id, "status": "done"}}))
                parent = staff.by_id(allowed.parent)
                assert parent is not None
                _ = staff.update_current_session(parent.current_session, staff.native_id(
                    fixture_runtime_id(f"next-director-{family}-{interrupted}")))
                before = self.staff_file.read_bytes(), inbox.read_bytes(), reference.read_bytes()
                tree = session_staff.worker_directory(staff.snapshot(), old.id)
                events: list[str] = []
                attempts: list[worker_admission.FamilyHandoff] = []

                def locked() -> None:
                    with (self.staff_file.parent / "rows" / f"{old.id}.lock").open("a") as probe:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

                def stop(reference: str, kind: str) -> stop_resume.Stopped:
                    self.assertEqual(reference, old.id)
                    self.assertEqual(kind, family)
                    locked()
                    events.append("stop")
                    return stop_resume.Stopped(old.current_session, 987654)

                def start(handoff: worker_admission.FamilyHandoff) -> str:
                    locked()
                    self.assertTrue(worker_admission.dispatch_record(allowed.run, allowed.name).assignment_id)
                    self.assertEqual(staff.by_id(old.id), old)
                    assert isinstance(handoff, worker_admission.TranscriptHandoff)
                    self.assertEqual(handoff.source, source)
                    self.assertIn(evidence[0][0], handoff.extract.read_text())
                    fixture_session(identity, staff.CodexBackend(self.root / "destination-codex")
                                    if destination == "codex" else staff.ClaudeBackend())
                    events.append("start")
                    return identity

                def dispatch(published: worker_admission.PublishedWorker) -> worker_admission.SwitchState:
                    locked()
                    self.assertEqual(events, ["stop", "start"] if not attempts else ["stop", "start", "dispatch"])
                    self.assertEqual(published.row, replace(old, current_session=staff.StaffId(identity)))
                    self.assertEqual(staff.by_id(old.id), published.row)
                    self.assertIsInstance(published.handoff, worker_admission.TranscriptHandoff)
                    events.append("dispatch")
                    attempts.append(published.handoff)
                    if interrupted and len(attempts) == 1:
                        worker_admission.record_dispatch(allowed, "stable-assignment", worker_admission.SwitchState.DISPATCH_IN_FLIGHT)
                        raise OSError("dispatch interrupted")
                    if interrupted:
                        self.assertEqual(worker_admission.dispatch_record(allowed.run, allowed.name).state, worker_admission.SwitchState.DISPATCH_IN_FLIGHT)
                    return worker_admission.SwitchState.ASSIGNMENT_ACCEPTED

                cleaned: list[str] = []
                with mock.patch.object(stop_resume, "stop", side_effect=stop):
                    if interrupted:
                        with self.assertRaisesRegex(OSError, "dispatch interrupted"):
                            _ = worker_admission.switch_worker(allowed, destination,
                                start=start, dispatch=dispatch, cleanup=cleaned.append, request="stable-assignment")
                    allowed = worker_admission.admit(old.name, allowed.run, reference=old.id)
                    result = worker_admission.switch_worker(allowed, destination,
                        start=start, dispatch=dispatch, cleanup=cleaned.append, request="stable-assignment")
                self.assertEqual(result.row, replace(old, current_session=staff.StaffId(identity)))
                self.assertEqual(events, ["stop", "start", *(["dispatch"] * (2 if interrupted else 1))])
                self.assertEqual(cleaned, [])
                self.assertEqual(len(attempts), 2 if interrupted else 1)
                self.assertEqual(attempts[0], attempts[-1])
                assert isinstance(attempts[-1], worker_admission.TranscriptHandoff)
                self.assertEqual(attempts[-1].source, source)
                self.assert_only_current_session_changed(before[0], old, identity)
                self.assertEqual((inbox.read_bytes(), reference.read_bytes()), before[1:])
                self.assertEqual(session_staff.worker_directory(staff.snapshot(), old.id), tree)
                saved = self.staff_state()
                with redirect_stdout(io.StringIO()) as output:
                    allowed = worker_admission.admit(old.name, allowed.run, reference=old.id)
                    repeated = worker_admission.switch_worker(allowed, destination,
                        start=start, dispatch=dispatch, cleanup=cleaned.append, request="stable-assignment")
                self.assertEqual(repeated.row, result.row)
                self.assertEqual(output.getvalue().strip(), "unchanged")
                self.assertEqual(self.staff_state(), saved)
                self.assertEqual(events, ["stop", "start", *(["dispatch"] * (2 if interrupted else 1))])

    def test_default_switch_request_reuses_the_published_destination_after_fresh_admission(self) -> None:
        host = fake_unit_host.environment(self.root)
        Path(host["HERDR_PROC"]).mkdir(exist_ok=True)
        _ = Path(host["FAKE_HERDR_RESPONSES"]).write_text(json.dumps({
            "default": {"code": 0, "stdout": json.dumps({"result": {"panes": []}})},
            "--session default session list --json": {"code": 0, "stdout": json.dumps({"sessions": []})},
        }))
        cases: tuple[tuple[Literal["claude", "codex"], Literal["claude", "codex"], bool], ...] = (
            ("claude", "codex", True), ("claude", "codex", False),
            ("codex", "claude", True), ("codex", "claude", False),
        )
        for family, destination, deferred in cases:
            with self.subTest(family=family, deferred=deferred), mock.patch.dict(os.environ, host):
                source, _, _ = self.source(family)
                assert isinstance(source.slot, worker_admission.ExistingWorkerSlot)
                old = source.slot.row
                run = source.run / ("prime" if deferred else "dispatch")
                run.mkdir()
                _ = (run / "mesh_roster.json").write_text(json.dumps({old.name: {"staff_id": old.id, "status": "done"}}))
                _ = (run / "mesh_server.json").write_text(json.dumps({"previous_servers": [{"pid": 987654321, "port": 9}]}))
                allowed = worker_admission.admit(old.name, run, reference=old.id)
                created: list[str] = []
                dispatched: list[str] = []
                cleaned: list[str] = []

                def start(handoff: worker_admission.FamilyHandoff) -> str:
                    if not created:
                        self.assertIsInstance(handoff, worker_admission.TranscriptHandoff)
                    intent = worker_admission.dispatch_record(allowed.run, allowed.name)
                    self.assertTrue(intent.assignment_id)
                    self.assertEqual(intent.state, worker_admission.SwitchState.CREATION_INTENT_RECORDED)
                    identity = fixture_runtime_id(f"retry-{family}-{deferred}-{len(created)}")
                    fixture_session(identity, staff.CodexBackend(self.root / "retry-codex")
                                    if destination == "codex" else staff.ClaudeBackend())
                    created.append(identity)
                    return identity

                def dispatch(published: worker_admission.PublishedWorker) -> worker_admission.SwitchState:
                    self.assertEqual(staff.by_id(old.id), published.row)
                    dispatched.append(published.row.current_session)
                    if len(dispatched) == 1:
                        raise OSError("interrupted after publication")
                    return worker_admission.SwitchState.ASSIGNMENT_ACCEPTED

                with self.assertRaisesRegex(OSError, "interrupted after publication"):
                    _ = worker_admission.switch_worker(allowed, destination, start=start,
                        dispatch=worker_admission.DispatchOwner.TURN_SUBMISSION if deferred else dispatch, cleanup=cleaned.append)
                    raise OSError("interrupted after publication")
                published = staff.by_id(old.id)
                assert published is not None
                self.assertEqual(published.current_session, created[0])
                self.assertEqual(session_staff.worker_family(staff.snapshot(), old.id), destination)
                intent = worker_admission.dispatch_record(allowed.run, allowed.name)
                allowed = worker_admission.admit(old.name, run, reference=old.id)
                result = worker_admission.switch_worker(allowed, destination, start=start,
                    dispatch=worker_admission.DispatchOwner.TURN_SUBMISSION if deferred else dispatch, cleanup=cleaned.append)
                self.assertEqual(created, [published.current_session])
                self.assertEqual(result.row, published)
                self.assertEqual(worker_admission.dispatch_record(allowed.run, allowed.name).assignment_id, intent.assignment_id)
                self.assertEqual(worker_admission.dispatch_record(allowed.run, allowed.name).state, worker_admission.SwitchState.CREATION_INTENT_RECORDED if deferred
                                 else worker_admission.SwitchState.ASSIGNMENT_ACCEPTED)
                self.assertEqual(dispatched, [] if deferred else [published.current_session] * 2)
                self.assertEqual(cleaned, [])

    def test_a_published_switch_retries_dispatch_with_the_original_extract(self) -> None:
        allowed, source, _ = self.source("claude")
        assert isinstance(allowed.slot, worker_admission.ExistingWorkerSlot)
        old = allowed.slot.row
        identity = fixture_runtime_id("dispatch-retry")
        starts: list[str] = []
        attempts: list[worker_admission.FamilyHandoff] = []

        def start(handoff: worker_admission.FamilyHandoff) -> str:
            self.assertIsInstance(handoff, worker_admission.TranscriptHandoff)
            fixture_session(identity, staff.CodexBackend(self.root / "destination"))
            starts.append(identity)
            return identity

        def dispatch(published: worker_admission.PublishedWorker) -> worker_admission.SwitchState:
            self.assertEqual(staff.by_id(old.id), published.row)
            attempts.append(published.handoff)
            if len(attempts) == 1:
                raise OSError("dispatch interrupted")
            return worker_admission.SwitchState.ASSIGNMENT_ACCEPTED

        with mock.patch.object(stop_resume, "stop", return_value=stop_resume.NotRunning(old.current_session)):
            with self.assertRaisesRegex(OSError, "dispatch interrupted"):
                _ = worker_admission.switch_worker(allowed, "codex", start=start, dispatch=dispatch, cleanup=starts.append)
            _ = worker_admission.switch_worker(allowed, "codex", start=start, dispatch=dispatch, cleanup=starts.append)
        self.assertEqual(starts, [identity])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])
        assert isinstance(attempts[1], worker_admission.TranscriptHandoff)
        self.assertEqual(attempts[1].source, source)

    def test_failed_creation_or_publication_dispatches_nothing_and_preserves_current_session(self) -> None:
        for failure in ("creation", "publication"):
            with self.subTest(failure=failure):
                allowed, _, _ = self.source("claude")
                assert isinstance(allowed.slot, worker_admission.ExistingWorkerSlot)
                old = allowed.slot.row
                identity = fixture_runtime_id("failed-" + failure)
                before = self.staff_file.read_bytes()
                events: list[str] = []

                def start(handoff: worker_admission.FamilyHandoff) -> str:
                    self.assertIsInstance(handoff, worker_admission.TranscriptHandoff)
                    if failure == "creation":
                        raise OSError("destination creation failed")
                    return identity

                def dispatch(published: worker_admission.PublishedWorker) -> worker_admission.SwitchState:
                    events.append("dispatch:" + published.row.current_session + str(type(published.handoff)))
                    return worker_admission.SwitchState.ASSIGNMENT_ACCEPTED

                def publish(old_session: str, new_session: str) -> staff.StaffRow:
                    self.assertEqual((old_session, new_session), (old.current_session, identity))
                    raise staff.StaffUnreadable("publication failed")

                cleaned: list[str] = []
                with (mock.patch.object(stop_resume, "stop", return_value=stop_resume.Stopped(old.current_session, 987654)),
                      mock.patch.object(staff, "update_current_session", side_effect=publish)):
                    with self.assertRaises((OSError, worker_admission.AdmissionRefused)):
                        _ = worker_admission.switch_worker(allowed, "codex", start=start,
                            dispatch=dispatch, cleanup=cleaned.append)
                self.assertEqual(events, [])
                self.assertEqual(cleaned, [identity] if failure == "publication" else [])
                self.assertEqual(staff.by_id(old.id), old)
                self.assertEqual(self.staff_file.read_bytes(), before)

    def test_competing_switch_cannot_start_or_dispatch_a_second_destination(self) -> None:
        _ = self.staff_config(lock_seconds=0.1)
        allowed, _, _ = self.source("claude")
        assert isinstance(allowed.slot, worker_admission.ExistingWorkerSlot)
        old = allowed.slot.row
        identity = fixture_runtime_id("winner")
        events: list[str] = []

        def start(handoff: worker_admission.FamilyHandoff) -> str:
            self.assertIsInstance(handoff, worker_admission.TranscriptHandoff)
            program = '''import sys
from pathlib import Path
from scripts.production import staff, worker_admission as admission
row = staff.local_snapshot().by_id(staff.StaffId(sys.argv[1]))
assert row is not None
allowed = admission.WorkerStartAllowed(row.name, row.parent.id, Path(sys.argv[2]), admission.ExistingWorkerSlot(row))
def unexpected(*args):
    print("second destination received work")
    raise SystemExit(9)
try:
    admission.switch_worker(allowed, "codex", start=unexpected, dispatch=unexpected, cleanup=unexpected)
except admission.AdmissionRefused as error:
    print(error)
    raise SystemExit(2)
'''
            environment = staff.child_environment(os.environ)
            contender = subprocess.run([str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-c", program, old.id, str(allowed.run)],
                env=environment, cwd=CHECKOUT, capture_output=True, text=True,
                check=False, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(contender.returncode, 2, contender.stdout + contender.stderr)
            self.assertIn("another operation", contender.stdout + contender.stderr)
            fixture_session(identity, staff.CodexBackend(self.root / "destination-codex"))
            return identity

        def dispatch(published: worker_admission.PublishedWorker) -> worker_admission.SwitchState:
            self.assertEqual(staff.by_id(old.id), published.row)
            self.assertIsInstance(published.handoff, worker_admission.TranscriptHandoff)
            events.append(published.row.current_session)
            return worker_admission.SwitchState.ASSIGNMENT_ACCEPTED

        with mock.patch.object(stop_resume, "stop", return_value=stop_resume.Stopped(old.current_session, 987654)):
            _ = worker_admission.switch_worker(allowed, "codex", start=start,
                dispatch=dispatch, cleanup=events.append)
        self.assertEqual(events, [identity])

    def test_missing_claude_transcript_is_empty_but_missing_codex_transcript_is_unavailable(self) -> None:
        for family in ("claude", "codex"):
            with self.subTest(family=family):
                allowed, source, _ = self.source(family)
                assert isinstance(allowed.slot, worker_admission.ExistingWorkerSlot)
                row = allowed.slot.row
                backend = staff.backend(row)
                assert isinstance(backend, (staff.ClaudeBackend, staff.CodexBackend))
                source.unlink()
                entries = worker_admission.conversation_entries(row, backend, set())
                if family == "claude":
                    self.assertEqual(list(entries), [])
                else:
                    with self.assertRaisesRegex(ValueError, "no source transcript found"):
                        _ = list(entries)
                self.assertFalse(source.exists())

    def test_missing_transcript_allows_dispatch_but_failed_stop_preserves_the_source(self) -> None:
        for failure in ("stop", "unresolved", "transcript"):
            with self.subTest(failure=failure):
                allowed, source, _ = self.source("claude")
                assert isinstance(allowed.slot, worker_admission.ExistingWorkerSlot)
                old = allowed.slot.row
                source.unlink()
                before = self.staff_file.read_bytes()
                identity = fixture_runtime_id("missing-evidence-destination")
                events: list[str] = []

                def start(handoff: worker_admission.FamilyHandoff) -> str:
                    self.assertIsInstance(handoff, worker_admission.TranscriptUnavailable)
                    fixture_session(identity, staff.CodexBackend(self.root / "destination"))
                    events.append("start")
                    return identity

                def dispatch(published: worker_admission.PublishedWorker) -> worker_admission.SwitchState:
                    self.assertEqual(staff.by_id(old.id), published.row)
                    self.assertIsInstance(published.handoff, worker_admission.TranscriptUnavailable)
                    events.append("dispatch")
                    return worker_admission.SwitchState.ASSIGNMENT_ACCEPTED

                stopped = (stop_resume.StopFailed(old.current_session, "source identity changed")
                           if failure == "stop" else stop_resume.SessionNotFound(old.id) if failure == "unresolved"
                           else stop_resume.NotRunning(old.current_session))
                with (mock.patch.object(stop_resume, "stop", return_value=stopped),
                      redirect_stderr(io.StringIO()) as errors):
                    if failure != "transcript":
                        reason = "source identity changed" if failure == "stop" else "could not resolve the staff reference"
                        with self.assertRaisesRegex(worker_admission.AdmissionRefused, reason) as refusal:
                            _ = worker_admission.switch_worker(allowed, "codex", start=start,
                                dispatch=dispatch, cleanup=events.append)
                        if failure == "stop":
                            self.assertIn("source identity changed", str(refusal.exception))
                    else:
                        _ = worker_admission.switch_worker(allowed, "codex", start=start,
                            dispatch=dispatch, cleanup=events.append)
                if failure != "transcript":
                    self.assertEqual(events, [])
                    self.assertEqual(self.staff_file.read_bytes(), before)
                else:
                    self.assertEqual(events, ["start", "dispatch"])
                    self.assertEqual(staff.by_id(old.id), replace(old, current_session=staff.StaffId(identity)))
                self.assertEqual(errors.getvalue().count("transcript extract:"), 1)
