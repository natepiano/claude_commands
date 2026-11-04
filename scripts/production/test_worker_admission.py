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

from scripts.lib import unit_runs
from scripts.production import session_staff, staff, worker_admission, showrunners
from scripts.production.staff_test_support import (
    CHECKOUT, StaffTestCase, fixture_runtime_id, fixture_session, fixture_worker, transcript_fixture,
)
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.shutdown import stop_resume


class RecordedDirectorAdmissionTests(StaffTestCase):
    def test_run_without_owner_staff_id_resolves_its_known_unit(self) -> None:
        production, unit, _ = self.tree()
        run = self.root / "run-without-owner"
        run.mkdir()
        _ = (run / unit_runs.STATE_FILE).write_text(json.dumps({
            "production": production.name, "unit": unit.name,
        }))
        self.assertEqual(worker_admission.parent(staff.snapshot(), run=run), unit)

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
            found = worker_admission.showrunner_member(
                staff.snapshot(), session_staff.MemberSources({}, ()), "old",
            )
        self.assertIsInstance(found, session_staff.NoSuchMember)

    def test_valid_worker_roles_are_retained_by_read_only_start_permission(self) -> None:
        _, unit, _ = self.tree()
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        for supplied in staff.WORKER_ROLES:
            with self.subTest(role=supplied), mock.patch.object(staff, "runtime_id", return_value=unit.current_session):
                allowed = worker_admission.admit("new-worker", self.root, role=supplied)
                self.assertEqual((allowed.name, allowed.parent, allowed.slot),
                                 ("new-worker", unit.id, worker_admission.NewWorkerSlot()))
                self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_start_permission_creates_no_row_and_publication_uses_the_returned_thread(self) -> None:
        _, unit, _ = self.tree()
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        with mock.patch.object(staff, "runtime_id", return_value=unit.id):
            allowed = worker_admission.admit("test", self.root, role="test")
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
        self.assert_only_current_session_changed(before, old, thread)
        self.assertIsNone(staff.by_current_session(old.current_session))
        self.assertEqual(staff.by_current_session(thread), result.row)
        self.assertEqual(staff.by_id(sibling.id), sibling)
        self.assertEqual(staff.by_id(unit.id), unit)


class FamilyHandoffTests(StaffTestCase):
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
        for family in ("claude", "codex"):
            with self.subTest(family=family):
                allowed, source, evidence = self.source(family)
                assert isinstance(allowed.slot, worker_admission.ExistingWorkerSlot)
                old = allowed.slot.row
                destination = "codex" if family == "claude" else "claude"
                identity = fixture_runtime_id("new-" + destination)
                inbox = session_staff.inbox_path(old.id)
                inbox.parent.mkdir(parents=True, exist_ok=True)
                _ = inbox.write_text(json.dumps({"messages": ["held work"], "accepting_messages": True}))
                previous_run = self.root / ("previous-director-" + family)
                previous_run.mkdir()
                reference = previous_run / "mesh_roster.json"
                _ = reference.write_text(json.dumps({old.name: {"staff_id": old.id, "status": "done"}}))
                parent = staff.by_id(allowed.parent)
                assert parent is not None
                _ = staff.update_current_session(parent.current_session, staff.native_id(
                    fixture_runtime_id("next-director-" + family)))
                before = self.staff_file.read_bytes(), inbox.read_bytes(), reference.read_bytes()
                tree = session_staff.worker_directory(staff.snapshot(), old.id)
                events: list[str] = []

                def locked() -> None:
                    with (self.staff_file.parent / "rows" / f"{old.id}.lock").open("a") as probe:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

                def stop(reference: str) -> stop_resume.Stopped:
                    self.assertEqual(reference, old.id)
                    locked()
                    events.append("stop")
                    return stop_resume.Stopped(old.current_session, 987654)

                def start(handoff: worker_admission.FamilyHandoff) -> str:
                    locked()
                    self.assertEqual(events, ["stop"])
                    self.assertEqual(staff.by_id(old.id), old)
                    assert isinstance(handoff, worker_admission.TranscriptHandoff)
                    self.assertEqual(handoff.source, source)
                    self.assertIn(evidence[0][0], handoff.extract.read_text())
                    fixture_session(identity, staff.CodexBackend(self.root / "destination-codex")
                                    if destination == "codex" else staff.ClaudeBackend())
                    events.append("start")
                    return identity

                def dispatch(published: worker_admission.PublishedWorker) -> None:
                    locked()
                    self.assertEqual(events, ["stop", "start"])
                    self.assertEqual(published.row, replace(old, current_session=staff.StaffId(identity)))
                    self.assertEqual(staff.by_id(old.id), published.row)
                    self.assertIsInstance(published.handoff, worker_admission.TranscriptHandoff)
                    events.append("dispatch")

                cleaned: list[str] = []
                with mock.patch.object(stop_resume, "stop", side_effect=stop):
                    result = worker_admission.switch_worker(allowed, destination,
                        start=start, dispatch=dispatch, cleanup=cleaned.append)
                self.assertEqual(result.row, replace(old, current_session=staff.StaffId(identity)))
                self.assertEqual(events, ["stop", "start", "dispatch"])
                self.assertEqual(cleaned, [])
                self.assert_only_current_session_changed(before[0], old, identity)
                self.assertEqual((inbox.read_bytes(), reference.read_bytes()), before[1:])
                self.assertEqual(session_staff.worker_directory(staff.snapshot(), old.id), tree)
                saved = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
                with redirect_stdout(io.StringIO()) as output:
                    repeated = worker_admission.switch_worker(allowed, destination,
                        start=start, dispatch=dispatch, cleanup=cleaned.append)
                self.assertEqual(repeated.row, result.row)
                self.assertEqual(output.getvalue().strip(), "unchanged")
                self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), saved)
                self.assertEqual(events, ["stop", "start", "dispatch"])

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

        def dispatch(published: worker_admission.PublishedWorker) -> None:
            self.assertEqual(staff.by_id(old.id), published.row)
            attempts.append(published.handoff)
            if len(attempts) == 1:
                raise OSError("dispatch interrupted")

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

                def dispatch(published: worker_admission.PublishedWorker) -> None:
                    events.append("dispatch:" + published.row.current_session + str(type(published.handoff)))

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
except staff.StaffRefused as error:
    print(error)
    raise SystemExit(2)
'''
            environment = {key: value for key, value in os.environ.items() if not key.startswith("STAFF_ROW_FD_")}
            contender = subprocess.run([sys.executable, "-c", program, old.id, str(allowed.run)],
                env=environment, cwd=CHECKOUT, capture_output=True, text=True,
                check=False, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(contender.returncode, 2, contender.stdout + contender.stderr)
            self.assertIn("another operation", contender.stdout + contender.stderr)
            fixture_session(identity, staff.CodexBackend(self.root / "destination-codex"))
            return identity

        def dispatch(published: worker_admission.PublishedWorker) -> None:
            self.assertEqual(staff.by_id(old.id), published.row)
            self.assertIsInstance(published.handoff, worker_admission.TranscriptHandoff)
            events.append(published.row.current_session)

        with mock.patch.object(stop_resume, "stop", return_value=stop_resume.Stopped(old.current_session, 987654)):
            _ = worker_admission.switch_worker(allowed, "codex", start=start,
                dispatch=dispatch, cleanup=events.append)
        self.assertEqual(events, [identity])

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

                def dispatch(published: worker_admission.PublishedWorker) -> None:
                    self.assertEqual(staff.by_id(old.id), published.row)
                    self.assertIsInstance(published.handoff, worker_admission.TranscriptUnavailable)
                    events.append("dispatch")

                stopped = (stop_resume.StopFailed(old.current_session, "source identity changed")
                           if failure == "stop" else stop_resume.SessionNotFound(old.id) if failure == "unresolved"
                           else stop_resume.NotRunning(old.current_session))
                with (mock.patch.object(stop_resume, "stop", return_value=stopped),
                      redirect_stderr(io.StringIO()) as errors):
                    if failure != "transcript":
                        reason = "source identity changed" if failure == "stop" else "could not resolve the staff reference"
                        with self.assertRaisesRegex(worker_admission.AdmissionRefused, reason):
                            _ = worker_admission.switch_worker(allowed, "codex", start=start,
                                dispatch=dispatch, cleanup=events.append)
                    else:
                        _ = worker_admission.switch_worker(allowed, "codex", start=start,
                            dispatch=dispatch, cleanup=events.append)
                if failure != "transcript":
                    self.assertEqual(events, [])
                    self.assertEqual(self.staff_file.read_bytes(), before)
                    if failure == "stop":
                        self.assertIn("source identity changed", errors.getvalue())
                else:
                    self.assertEqual(events, ["start", "dispatch"])
                    self.assertEqual(staff.by_id(old.id), replace(old, current_session=staff.StaffId(identity)))
                self.assertEqual(errors.getvalue().count("transcript extract:"), 1)
