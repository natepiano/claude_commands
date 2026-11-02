"""Audit drift without repairing the fixture or suppressing uncertain evidence."""
from __future__ import annotations

from scripts.production.staff_test_support import fixture_member

from contextlib import redirect_stdout
from dataclasses import dataclass, replace
from functools import cached_property
import io
import os
from pathlib import Path
import shutil
from typing import override
from unittest import mock
import unittest

from scripts.lib import unit_runs
from scripts.production import staff, staff_check, staff_sources, unit_lookup, showrunners
from scripts.production.staff_test_support import StaffTestCase
from scripts.message import process_table, roster
from scripts.message.relay_test_support import write_relay_record
import herdr
import sessions


@dataclass(frozen=True)
class StaffCheckFixture:
    runner: staff.StaffRow
    unit: staff.StaffRow
    worker: staff.StaffRow
    doc: Path
    folder: Path
    live: tuple[staff_check.LiveStaffSession, ...]
    evidence: staff_check.CheckEvidence


class StaffCheckTests(StaffTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(mock.patch.object(roster, "peer_snapshot", return_value=roster.Roster([], [])))
        _ = self.fixture

    @cached_property
    def fixture(self) -> StaffCheckFixture:
        runner, unit, worker = self.tree()
        doc = self.production("sdlc", "models-unit")
        folder = self.worktree("models-unit")
        runner = staff.set_doc(runner.id, staff.LiveDoc("natedev", doc))
        unit = staff.set_worktree(unit.id, staff.PresentWorktree("natedev", folder))
        live = (
            staff_check.LiveStaffSession(101, "claude:" + runner.id, runner.id),
            staff_check.LiveStaffSession(102, "claude:" + unit.id, unit.id, str(folder)),
            staff_check.LiveStaffSession(103, "claude:" + worker.id, worker.id),
        )
        owner = staff_sources.WorkerOwner("unit", unit)
        observation = staff_sources.WorkerObservation("impl1", owner, "impl", {"claude": worker.id})
        evidence = staff_check.CheckEvidence(
            unit_lookup.member_sources(), live, staff_sources.WorkerEvidence((observation,), {}, ()))
        return StaffCheckFixture(runner, unit, worker, doc, folder, live, evidence)

    def check(self, evidence: staff_check.CheckEvidence | None = None) -> tuple[int, str]:
        output = io.StringIO()
        with (mock.patch.object(staff_check, "read_evidence", return_value=evidence or self.fixture.evidence),
              redirect_stdout(output)):
            code = staff_check.main()
        return code, output.getvalue()

    def assert_wrong(self, row: staff.StaffRow, text: str,
                     evidence: staff_check.CheckEvidence | None = None) -> str:
        code, output = self.check(evidence)
        self.assertEqual(code, 1, output)
        self.assertIn(f"{row.id} {row.name}:", output)
        self.assertIn(text, output)
        return output

    def process_evidence(self, process: process_table.Process, registry: sessions.RegistryRead,
                         environment: dict[str, str]) -> staff_check.CheckEvidence:
        with (mock.patch.object(sessions, "read_registry", return_value=registry),
              mock.patch.object(sessions, "running_session", return_value=True),
              mock.patch.object(process_table, "snapshot", return_value={process.pid: process}),
              mock.patch.object(herdr, "process_environment", return_value=environment),
              mock.patch.object(staff_sources, "observe_workers", return_value=staff_sources.WorkerEvidence((), {}, ()))):
            return staff_check.read_evidence(staff.snapshot())

    def test_codex_without_a_thread_in_an_unowned_pane_is_one_note_and_no_wrong_row(self) -> None:
        process = process_table.Process(104, 1, "codex", "codex resume")
        evidence = self.process_evidence(process, sessions.RegistryRead((), ()), {"HERDR_PANE_ID": "unowned:pane"})
        self.assertEqual(evidence.sessions, ())
        self.assertEqual(evidence.uncertainties, ())
        note = "staff sources: Codex process 104: pane holds no staff member; not staff"
        self.assertEqual(evidence.notes, (note,))
        evidence = replace(evidence, sessions=self.fixture.live,
                           workers=replace(self.fixture.evidence.workers, diagnostics=(note,)))
        self.assertEqual(self.check(evidence), (0, f"note: {note}\nchecked 3 rows; 0 wrong\n"))

    def test_codex_without_a_thread_in_a_staff_pane_stays_uncertain(self) -> None:
        self.pane("staff:pane", session_id=self.fixture.unit.id, environment={})
        process = process_table.Process(104, 1, "codex", "codex resume")
        evidence = self.process_evidence(process, sessions.RegistryRead((), ()), {"HERDR_PANE_ID": "staff:pane"})
        self.assertEqual(evidence.notes, ())
        self.assertEqual(evidence.uncertainties, ("pid 104 uncertain: Codex runtime identity is unavailable",))

    def test_damaged_pending_files_have_identical_sorted_reports_without_file_changes(self) -> None:
        run = Path(os.environ["ROSTER_RUN_ROOT"]) / "damaged-inboxes"
        run.mkdir(parents=True)
        for name, text in (("z.pending.json", '{"messages": []}'), ("a.pending.json", '{"messages": []}')):
            _ = (run / name).write_text(text)
        other = run.with_name("other-damaged-inboxes")
        other.mkdir()
        _ = (other / "bad.pending.json").write_text('{"messages": []}')
        def disk() -> dict[Path, tuple[bytes, int]]:
            return {path: (path.read_bytes(), path.stat().st_mtime_ns)
                    for folder in (run, other) for path in folder.iterdir()}

        before = disk()
        outputs: list[str] = []
        with mock.patch.object(process_table, "snapshot", return_value={}):
            for _ in range(2):
                output = io.StringIO()
                with redirect_stdout(output):
                    code = staff_check.main()
                self.assertEqual(code, 1, output.getvalue())
                outputs.append(output.getvalue())
        self.assertEqual(outputs[0], outputs[1])
        self.assertIn("checked 3 rows; 2 wrong", outputs[0])
        self.assertLess(outputs[0].index("a.pending.json"), outputs[0].index("z.pending.json"))
        self.assertEqual(disk(), before)

    def test_codex_app_server_with_a_director_staff_mark_is_not_a_second_session(self) -> None:
        process = process_table.Process(104, 1, "codex", "codex app-server --listen stdio://")
        evidence = self.process_evidence(process, sessions.RegistryRead((), ()),
                                         {"CODEX_THREAD_ID": self.fixture.unit.id})
        evidence = replace(evidence, sessions=(*self.fixture.live, *evidence.sessions),
                           workers=self.fixture.evidence.workers)
        self.assertEqual(self.check(evidence), (0, "checked 3 rows; 0 wrong\n"))

    def test_codex_director_with_a_claude_director_staff_mark_is_a_second_session(self) -> None:
        process = process_table.Process(104, 1, "codex", "codex --resume director-thread")
        evidence = self.process_evidence(process, sessions.RegistryRead((), ()),
                                         {"CODEX_THREAD_ID": self.fixture.unit.id})
        evidence = replace(evidence, sessions=(*self.fixture.live, *evidence.sessions),
                           workers=self.fixture.evidence.workers)
        _ = self.assert_wrong(self.fixture.unit,
                              f"multiple live sessions: claude:{self.fixture.unit.id}, codex:{self.fixture.unit.id}", evidence)

    def test_codex_app_server_with_top_level_value_options_is_not_a_second_session(self) -> None:
        for options in ("--local-provider ollama", "--local-provider=ollama",
                        "-c model=example", "--config=model=example",
                        "--remote unix:///tmp/example.sock", "--remote=unix:///tmp/example.sock",
                        "--remote-auth-token-env EXAMPLE_TOKEN", "--remote-auth-token-env=EXAMPLE_TOKEN"):
            with self.subTest(options=options):
                process = process_table.Process(
                    104, 1, "codex", f"codex {options} app-server --listen stdio://")
                evidence = self.process_evidence(process, sessions.RegistryRead((), ()),
                                                 {"CODEX_THREAD_ID": self.fixture.unit.id})
                evidence = replace(evidence, sessions=(*self.fixture.live, *evidence.sessions),
                                   workers=self.fixture.evidence.workers)
                self.assertEqual(self.check(evidence), (0, "checked 3 rows; 0 wrong\n"))

    def test_claude_registry_identity_is_read_without_an_exported_staff_mark(self) -> None:
        record: sessions.SessionRecord = {
            "pid": 104, "sessionId": self.fixture.unit.id, "name": "unrelated-display",
            "messagingSocketPath": "", "updatedAt": 1, "tmux": "", "formerNames": [],
            "entrypoint": "cli", "cwd": str(self.fixture.folder),
        }
        registry = sessions.RegistryRead((sessions.RegistryObservation(record, {}),), ())
        evidence = self.process_evidence(process_table.Process(104, 1, "claude", "claude"), registry, {})
        self.assertEqual([(session.pid, session.identity, session.staff_id) for session in evidence.sessions],
                         [(104, "claude:" + self.fixture.unit.id, self.fixture.unit.id)])

    def test_codex_thread_identity_overrides_a_stale_inherited_staff_mark(self) -> None:
        thread = "019b3a40-4250-7000-8000-000000000104"
        evidence = self.process_evidence(
            process_table.Process(104, 1, "codex", "codex exec task"), sessions.RegistryRead((), ()),
            {"CODEX_THREAD_ID": thread, "STAFF_ID": self.fixture.unit.id},
        )
        self.assertEqual([(session.identity, session.staff_id) for session in evidence.sessions],
                         [("codex:" + thread, thread)])

    def test_new_registry_identity_is_listed_while_staff_publication_is_held(self) -> None:
        new_id = "12c4a16a-68ac-49b0-88fe-000000000104"
        record: sessions.SessionRecord = {
            "pid": 104, "sessionId": new_id, "name": self.fixture.worker.name,
            "messagingSocketPath": "", "updatedAt": 1, "tmux": "", "formerNames": [],
            "entrypoint": "cli", "cwd": str(self.fixture.folder),
        }
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        evidence = self.process_evidence(
            process_table.Process(104, 1, "claude", "claude"),
            sessions.RegistryRead((sessions.RegistryObservation(record, {}),), ()), {},
        )
        code, output = self.check(replace(evidence, workers=self.fixture.evidence.workers))
        self.assertEqual(code, 1, output)
        self.assertIn(new_id, output)
        self.assertIn("missing staff row", output)
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)
        self.assertEqual(staff.by_id(self.fixture.worker.id), self.fixture.worker)
        self.assertIsNone(staff.by_id(staff.StaffId(new_id)))

    def test_peer_snapshot_supplies_a_remote_members_native_runtime_identity(self) -> None:
        peer = roster.RosterEntry(
            machine="mac", kind="claude", role="unit director", name="different-display-name",
            status="running", reach=roster.Messageable("session:" + self.fixture.unit.id),
            cwd="/remote/models", showrunner=roster.NoShowrunner(), director=roster.NoDirector(),
            assignment=roster.NotAWorker(), session=roster.ClaudeSession(self.fixture.unit.id),
        )
        before = self.staff_file.read_bytes()
        with mock.patch.object(roster, "peer_snapshot", return_value=roster.Roster([peer], [])):
            evidence = self.process_evidence(
                process_table.Process(104, 1, "bash", "bash"), sessions.RegistryRead((), ()), {},
            )
        self.assertEqual([(session.identity, session.staff_id) for session in evidence.sessions],
                         [("claude:" + self.fixture.unit.id, self.fixture.unit.id)])
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_peer_freestanding_agents_are_one_note_without_missing_staff_rows(self) -> None:
        identity = "746a8678-798b-4e84-9973-52e5de973302"
        peer = roster.RosterEntry(
            machine="mac", kind="claude", role="freestanding", name="same-display",
            status="running", reach=roster.Messageable("session:" + identity), cwd="/remote/home",
            showrunner=roster.NoShowrunner(), director=roster.NoDirector(), assignment=roster.NotAWorker(),
            session=roster.ClaudeSession(identity),
        )
        codex = replace(peer, kind="codex", session=roster.NoClaudeSession(), thread=roster.CodexThread(identity))
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        for entry in (peer, codex):
            with self.subTest(kind=entry.kind), mock.patch.object(
                roster, "peer_snapshot", return_value=roster.Roster([entry, entry], [])
            ):
                evidence = self.process_evidence(
                    process_table.Process(104, 1, "bash", "bash"), sessions.RegistryRead((), ()), {},
                )
                self.assertEqual(evidence.sessions, ())
                self.assertEqual(evidence.uncertainties, ())
                evidence = replace(evidence, sessions=self.fixture.live, workers=self.fixture.evidence.workers)
                self.assertEqual(self.check(evidence), (
                    0, "note: mac freestanding same-display: not staff\nchecked 3 rows; 0 wrong\n"))
                self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_peer_staff_roles_without_rows_remain_problems(self) -> None:
        identity = "746a8678-798b-4e84-9973-52e5de973302"
        roles: tuple[roster.Role, ...] = ("showrunner", "unit director", "worker", "astra-designer", "astra-reviewer")
        for role in roles:
            peer = roster.RosterEntry(
                machine="mac", kind="claude", role=role, name="same-display", status="running",
                reach=roster.Messageable("session:" + identity), cwd="/remote/home",
                showrunner=roster.NoShowrunner(), director=roster.NoDirector(), assignment=roster.NotAWorker(),
                session=roster.ClaudeSession(identity),
            )
            codex = replace(peer, kind="codex", session=roster.NoClaudeSession(), thread=roster.CodexThread(identity))
            for entry in (peer, codex):
                with self.subTest(role=role, kind=entry.kind), mock.patch.object(
                    roster, "peer_snapshot", return_value=roster.Roster([entry], [])
                ):
                    evidence = self.process_evidence(
                        process_table.Process(104, 1, "bash", "bash"), sessions.RegistryRead((), ()), {},
                    )
                    self.assertEqual(evidence.notes, ())
                    self.assertEqual(len(evidence.sessions), 1)
                    self.assertEqual(evidence.sessions[0].staff_id, identity)
                    evidence = replace(evidence, sessions=(*self.fixture.live, *evidence.sessions),
                                       workers=self.fixture.evidence.workers)
                    self.assertEqual(self.check(evidence), (
                        1, f"{identity} pid 0: live session names a removed or missing staff row\n"
                        + "checked 3 rows; 1 wrong\n"))

    def test_unavailable_peer_snapshot_lists_uncertainty_without_confirming_remote_absence(self) -> None:
        before = self.staff_file.read_bytes()
        with mock.patch.object(roster, "peer_snapshot", return_value=roster.PeerNotReached("mac", "offline")):
            evidence = self.process_evidence(
                process_table.Process(104, 1, "bash", "bash"), sessions.RegistryRead((), ()), {},
            )
        self.assertTrue(any("mac" in reason and "offline" in reason for reason in evidence.uncertainties),
                        evidence.uncertainties)
        code, output = self.check(evidence)
        self.assertEqual(code, 1, output)
        self.assertIn("uncertain", output)
        self.assertIn("offline", output)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_clean_staff_exits_zero_with_only_the_count_line(self) -> None:
        self.assertEqual(self.check(), (0, "checked 3 rows; 0 wrong\n"))

    def test_uncertain_current_session_is_reported_under_its_fixed_row_without_writing_staff(self) -> None:
        current = staff.native_id("019b3a40-4250-7000-8000-000000000209")
        worker = staff.update_current_session(self.fixture.worker.current_session, current)
        live = replace(self.fixture.live[2], identity="claude:" + current, staff_id=current)
        observation = replace(self.fixture.evidence.workers.workers[0], identity={"claude": current})
        evidence = replace(
            self.fixture.evidence,
            sessions=(*self.fixture.live[:2], live),
            workers=replace(self.fixture.evidence.workers, workers=(observation,)),
            uncertainties=(f"{current} uncertain: runtime probe unavailable",),
        )
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns

        self.assertEqual(self.check(evidence), (
            1, f"{worker.id} {worker.name}: uncertain: runtime probe unavailable\nchecked 3 rows; 1 wrong\n",
        ))
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_codex_worker_observed_by_the_scanner_under_its_live_unit_is_right(self) -> None:
        observation = replace(self.fixture.evidence.workers.workers[0], identity={"codex": self.fixture.worker.id})
        evidence = replace(
            self.fixture.evidence,
            sessions=self.fixture.live[:2],
            workers=replace(self.fixture.evidence.workers, workers=(observation,)),
        )
        self.assertEqual(self.check(evidence), (0, "checked 3 rows; 0 wrong\n"))

    def test_scanner_worker_and_process_staff_id_disagreement_lists_both_rows(self) -> None:
        second = fixture_member("worker", name="impl2", parent=self.fixture.unit.id)
        evidence = replace(self.fixture.evidence, sessions=(
            *self.fixture.live[:2], replace(self.fixture.live[2], staff_id=second.id),
        ))
        code, output = self.check(evidence)
        self.assertEqual(code, 1, output)
        for row, other in ((self.fixture.worker, second), (second, self.fixture.worker)):
            self.assertIn(f"{row.id} {row.name}: uncertain: session also names staff row {other.id}: claude:{self.fixture.worker.id}", output)

    def test_scanner_and_process_identity_conflict_is_uncertain_and_lists_both_identities(self) -> None:
        for source in ("owner", "worker"):
            with self.subTest(source=source):
                workers = self.fixture.evidence.workers
                if source == "owner":
                    row = self.fixture.unit
                    identities = {row.id: {"claude": "other-director"}}
                    workers = replace(workers, identities=identities)
                    expected = "other-director"
                else:
                    row = self.fixture.worker
                    observation = replace(workers.workers[0], identity={"claude": "other-worker"})
                    workers = replace(workers, workers=(observation,))
                    expected = "other-worker"
                evidence = replace(self.fixture.evidence, workers=workers)
                output = self.assert_wrong(row, "runtime identity differs from current session: claude:" + expected, evidence)
                self.assertIn(row.id, output)
                self.assertIn(expected, output)

    def test_plain_folder_in_place_of_a_unit_worktree_is_listed(self) -> None:
        (self.fixture.folder / ".git").unlink()
        _ = self.assert_wrong(self.fixture.unit, f"not a linked git worktree: {self.fixture.folder}")

    def test_headless_claude_is_not_reintroduced_by_its_executable_name(self) -> None:
        record: sessions.SessionRecord = {
            "pid": 104, "sessionId": "query", "name": "query", "messagingSocketPath": "",
            "updatedAt": 1, "tmux": "", "formerNames": [], "entrypoint": "sdk-cli",
        }
        registry = sessions.RegistryRead((sessions.RegistryObservation(record, {}),), ())
        process = process_table.Process(104, 1, "claude", "claude -p query")
        evidence = self.process_evidence(process, registry, {"CODEX_THREAD_ID": self.fixture.unit.id})
        self.assertEqual(evidence.sessions, ())
        self.assertEqual(evidence.uncertainties, ())

    def test_unregistered_interactive_claude_lists_its_pid_without_using_stale_staff_marks(self) -> None:
        process = process_table.Process(104, 1, "claude", "claude --resume missing-registry")
        for identity in (self.fixture.unit.id, staff.StaffId("s00000000000")):
            with self.subTest(staff_id=identity):
                evidence = self.process_evidence(process, sessions.RegistryRead((), ()), {"STAFF_ID": identity})
                self.assertEqual(evidence.sessions, ())
                self.assertEqual(self.check(evidence), (1, "\n".join((
                    "pid session: 104 uncertain: Claude process has no live registry identity",
                    "checked 3 rows; 1 wrong", "",
                ))))

    def test_unregistered_relay_is_excluded_by_the_shared_session_rule(self) -> None:
        state = self.root / "state"
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(state)}):
            write_relay_record(state / "message", os.getpid(), "director")
            process = process_table.Process(os.getpid(), 1, "claude", "claude -p relay-message")
            evidence = self.process_evidence(process, sessions.RegistryRead((), ()), {"CODEX_THREAD_ID": self.fixture.unit.id})
        self.assertEqual(self.check(evidence), (0, "checked 3 rows; 0 wrong\n"))

    def test_incomplete_timer_is_skipped_by_registry_discovery_but_refuses_the_audit(self) -> None:
        conf = Path(os.environ["NOTIFIER_STATE_DIR"]) / "showrunner-sdlc" / "conf"
        _ = conf.write_text("TARGET=session:session-sdlc\n")
        with self.subTest(reader="registered showrunners"):
            self.assertEqual(showrunners.registered_showrunners(known_sessions={}), [])
        with self.subTest(reader="audit"):
            output = io.StringIO()
            with redirect_stdout(output):
                code = staff_check.main()
            self.assertEqual(code, 2, output.getvalue())
            self.assertIn(str(conf), output.getvalue())
            self.assertIn("CHECK", output.getvalue())

    def test_live_session_with_removed_staff_id_is_listed_without_name_fallback(self) -> None:
        missing = staff.StaffId("s00000000000")
        evidence = replace(self.fixture.evidence, sessions=(*self.fixture.live, staff_check.LiveStaffSession(104, "codex:lost", missing)))
        code, output = self.check(evidence)
        self.assertEqual(code, 1, output)
        self.assertIn(f"{missing} pid 104: live session names a removed or missing staff row", output)

    def test_two_live_sessions_cannot_claim_one_row(self) -> None:
        evidence = replace(self.fixture.evidence, sessions=(*self.fixture.live, staff_check.LiveStaffSession(104, "codex:second", self.fixture.worker.id)))
        _ = self.assert_wrong(self.fixture.worker, f"multiple live sessions: claude:{self.fixture.worker.id}, codex:second", evidence)

    def test_one_session_cannot_claim_two_existing_rows(self) -> None:
        evidence = replace(self.fixture.evidence, sessions=(*self.fixture.live, staff_check.LiveStaffSession(104, "claude:" + self.fixture.worker.id, self.fixture.unit.id)))
        code, output = self.check(evidence)
        self.assertEqual(code, 1, output)
        self.assertIn("session also names staff row", output)

    def test_refused_parent_graph_exits_two_with_the_staff_readers_reason(self) -> None:
        for parent in (staff.StaffId("s00000000000"), self.fixture.worker.id):
            with self.subTest(parent=parent):
                unit = replace(self.fixture.unit, parent=parent)
                self.write_json(self.staff_file, {"schema": 2, "rows": [
                    self.fixture.runner.to_json(), unit.to_json(), self.fixture.worker.to_json(),
                ]})
                code, output = self.check()
                self.assertEqual(code, 2, output)
                self.assertEqual(len(output.splitlines()), 1, output)
                self.assertIn("cannot read source: staff record:", output)
                self.assertIn(f"unit {unit.name} requires a showrunner parent: {parent}", output)

    def test_live_child_whose_parent_is_not_live_is_listed(self) -> None:
        evidence = replace(self.fixture.evidence, sessions=self.fixture.live[1:])
        _ = self.assert_wrong(self.fixture.unit, f"live row has no live parent {self.fixture.runner.id}", evidence)

    def test_worker_under_a_different_parent_than_its_live_owner_is_listed(self) -> None:
        owner = staff_sources.WorkerOwner("runner", self.fixture.runner)
        worker = replace(self.fixture.evidence.workers.workers[0], owner=owner)
        evidence = replace(self.fixture.evidence, workers=replace(self.fixture.evidence.workers, workers=(worker,)))
        _ = self.assert_wrong(self.fixture.worker, f"live worker belongs under {self.fixture.runner.id}", evidence)

    def test_live_worker_without_a_staff_row_is_listed(self) -> None:
        self.assertEqual(staff.retire(self.fixture.worker.id), self.fixture.worker)
        code, output = self.check()
        self.assertEqual(code, 1, output)
        self.assertIn("impl1: live worker has no row under", output)
        self.assertIn(f"{self.fixture.worker.id} pid 103: live session names a removed or missing staff row", output)

    def test_missing_worktree_directory_is_listed(self) -> None:
        shutil.rmtree(self.fixture.folder)
        _ = self.assert_wrong(self.fixture.unit, f"worktree does not exist: {self.fixture.folder}")

    def test_worktree_that_disagrees_with_its_bound_document_is_listed(self) -> None:
        elsewhere = self.root / "other-checkout"
        elsewhere.mkdir()
        _ = staff.set_worktree(self.fixture.unit.id, staff.PresentWorktree("natedev", elsewhere))
        _ = self.assert_wrong(self.fixture.unit, "worktree belongs elsewhere: document names")

    def test_resumed_session_in_an_external_folder_keeps_its_valid_ownership(self) -> None:
        evidence = replace(self.fixture.evidence, sessions=(
            self.fixture.live[0], replace(self.fixture.live[1], cwd=str(self.root)), self.fixture.live[2],
        ))
        self.assertEqual(self.check(evidence), (0, "checked 3 rows; 0 wrong\n"))

    def test_removed_worktree_is_valid_for_an_offline_retained_unit(self) -> None:
        _ = staff.set_worktree(self.fixture.unit.id, staff.RemovedWorktree("natedev", self.fixture.folder))
        shutil.rmtree(self.fixture.folder)
        evidence = replace(self.fixture.evidence, sessions=self.fixture.live[:1], workers=staff_sources.WorkerEvidence((), {}, ()))
        self.assertEqual(self.check(evidence), (0, "checked 3 rows; 0 wrong\n"))

    def test_live_session_on_a_removed_worktree_is_listed(self) -> None:
        _ = staff.set_worktree(self.fixture.unit.id, staff.RemovedWorktree("natedev", self.fixture.folder))
        _ = self.assert_wrong(self.fixture.unit, "live session is attached to a retired row")

    def test_missing_showrunner_document_is_listed(self) -> None:
        self.fixture.doc.unlink()
        _ = self.assert_wrong(self.fixture.runner, f"production doc does not exist: {self.fixture.doc}")

    def test_showrunner_document_that_disagrees_with_its_timer_is_listed(self) -> None:
        other = self.root / "other-production.md"
        _ = other.write_text(self.fixture.doc.read_text())
        _ = staff.set_doc(self.fixture.runner.id, staff.LiveDoc("natedev", other))
        _ = self.assert_wrong(self.fixture.runner, "production doc belongs elsewhere: timer names")

    def test_remote_worktree_is_reported_as_uncertain_instead_of_inferred(self) -> None:
        _ = staff.set_worktree(self.fixture.unit.id, staff.PresentWorktree("mac", self.fixture.folder))
        _ = self.assert_wrong(self.fixture.unit, "uncertain: worktree is on mac")

    def test_unfilled_worktree_is_listed(self) -> None:
        row = fixture_member("unit", name="unfilled", parent=self.fixture.runner.id)
        _ = self.assert_wrong(row, "worktree is unfilled")

    def test_an_unbound_row_cannot_borrow_another_units_marked_session_or_document(self) -> None:
        stale = fixture_member("unit", name="former-name", parent=self.fixture.runner.id)
        folder = self.worktree("former-name-unit")
        stale = staff.set_worktree(stale.id, staff.PresentWorktree("natedev", folder))
        output = self.assert_wrong(stale, "uncertain: no document worktree bound to this unit")
        self.assertNotIn(f"{self.fixture.unit.id} {self.fixture.unit.name}:", output)

    def test_delegate_without_a_server_is_silent_but_a_live_server_keeps_the_role_problem(self) -> None:
        _ = self.session_record("director", "models-unit")
        self.pane("w1:models", session_id="director", environment={"STAFF_ID": self.fixture.unit.id})
        delegate_root = self.source_root / "delegate"
        with (mock.patch.object(unit_runs, "FORMER_RUN_ROOT", delegate_root),
              mock.patch.dict(os.environ, {"ROSTER_RUN_ROOT": str(delegate_root)})):
            run = self.worker_run("sdlc", "models-unit", "director")
            roster = self.roster_worker(run, "delegate", "", "thread-paused", status="paused")
            server = run / "mesh_server.json"
            server_record = server.read_text()
            server.unlink()
            before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
            for server_present in (False, True):
                with self.subTest(server_present=server_present):
                    if server_present:
                        _ = server.write_text(server_record)
                    workers = staff_sources.observe_workers(staff.snapshot(), unit_lookup.member_sources())
                    evidence = replace(self.fixture.evidence, workers=workers)
                    code, output = self.check(evidence)
                    if server_present:
                        self.assertEqual(code, 1, output)
                        self.assertIn(str(roster), output)
                        self.assertIn("missing or invalid recorded worker role", output)
                    else:
                        self.assertEqual((code, output), (0, "checked 3 rows; 0 wrong\n"))
                        self.assertEqual(workers.workers, ())
                        self.assertEqual(workers.diagnostics, ())
                    self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)
                    self.assertNotIn("delegate", [row.name for row in staff.find(kind="worker")])

    def test_a_paused_worker_without_a_recorded_role_remains_an_audit_problem(self) -> None:
        _ = self.session_record("session-models", "models-unit")
        self.pane("w1:models", session_id="session-models", environment={"CODEX_THREAD_ID": self.fixture.unit.id})
        run = self.worker_run("sdlc", "models-unit", "session-models")
        roster = self.roster_worker(run, "delegate", "", "thread-paused", status="paused")
        workers = staff_sources.observe_workers(staff.snapshot(), unit_lookup.member_sources())
        evidence = replace(self.fixture.evidence, workers=workers)
        code, output = self.check(evidence)
        self.assertEqual(code, 1, output)
        self.assertIn(str(roster), output)
        self.assertIn("missing or invalid recorded worker role", output)
        self.assertNotIn("delegate", [row.name for row in staff.find(kind="worker")])

    def test_unreadable_staff_record_exits_two_and_names_the_source(self) -> None:
        _ = self.staff_file.write_text("broken record")
        code, output = self.check()
        self.assertEqual(code, 2, output)
        self.assertIn("cannot read source", output)
        self.assertIn("staff", output)

    def test_unreadable_session_registry_exits_two_and_names_the_path(self) -> None:
        unreadable = self.root / "unreadable-registry"
        registry = sessions.RegistryRead((), (unreadable,))
        output = io.StringIO()
        with (mock.patch.object(sessions, "read_registry", return_value=registry), redirect_stdout(output)):
            code = staff_check.main()
        self.assertEqual(code, 2, output.getvalue())
        self.assertIn(str(unreadable), output.getvalue())

    def test_process_outside_herdr_lists_its_missing_native_row(self) -> None:
        process = process_table.Process(104, 1, "codex", "codex exec task")
        identity = "5e2cecb0-32f9-438d-91ee-673f9df69697"
        evidence = self.process_evidence(process, sessions.read_registry(), {"CODEX_THREAD_ID": identity})
        self.assertEqual(evidence.sessions[0].staff_id, identity)
        code, output = self.check(evidence)
        self.assertEqual(code, 1, output)
        self.assertIn(f"{identity} pid 104: live session names a removed or missing staff row", output)

    def test_unreadable_process_environment_exits_two_instead_of_claiming_absence(self) -> None:
        process = process_table.Process(104, 1, "codex", "codex exec task")
        output = io.StringIO()
        with (mock.patch.object(process_table, "snapshot", return_value={104: process}),
              mock.patch.object(herdr, "process_environment", side_effect=PermissionError("denied")),
              redirect_stdout(output)):
            code = staff_check.main()
        self.assertEqual(code, 2, output.getvalue())
        self.assertIn("process 104 environment", output.getvalue())

    def test_unknown_worker_backend_is_listed_as_uncertain(self) -> None:
        worker = replace(self.fixture.evidence.workers.workers[0], identity=staff_sources.BackendUnavailable())
        evidence = replace(self.fixture.evidence, workers=replace(self.fixture.evidence.workers, workers=(worker,)))
        _ = self.assert_wrong(self.fixture.unit, "uncertain: worker impl1 backend is unavailable", evidence)

    def test_unreadable_worker_ledger_exits_two_and_names_the_source(self) -> None:
        path = Path(os.environ["UNIT_WORKERS_STATE"]) / "sdlc" / "models.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text("broken ledger")
        output = io.StringIO()
        with (mock.patch.object(process_table, "snapshot", return_value={}), redirect_stdout(output)):
            code = staff_check.main()
        self.assertEqual(code, 2, output.getvalue())
        self.assertIn(str(path), output.getvalue())

    def test_two_runs_print_identical_bytes_without_changing_files_or_taking_locks(self) -> None:
        def disk() -> dict[Path, tuple[bytes, int]]:
            return {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.root.rglob("*") if path.is_file()}

        before = disk()
        with (mock.patch.object(staff, "operation", side_effect=AssertionError("checker took a lock")),
              mock.patch.object(process_table, "snapshot", return_value={})):
            output = io.StringIO()
            with redirect_stdout(output):
                first = staff_check.main()
            first_bytes = output.getvalue()
            output = io.StringIO()
            with redirect_stdout(output):
                second = staff_check.main()
        self.assertEqual((first, second), (0, 0), output.getvalue())
        self.assertEqual(first_bytes, output.getvalue())
        self.assertEqual(disk(), before)


if __name__ == "__main__":
    _ = unittest.main()
