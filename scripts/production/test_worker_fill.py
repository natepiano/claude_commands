"""Worker membership comes from producer records, with no worker side effects."""
from __future__ import annotations

import ast
import io
import os
import json
import shutil
import subprocess
import sys
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock
from types import FunctionType
from typing import cast, get_type_hints

from scripts.agents import codex_mesh
from scripts.herdr.herdr import HerdrPlace
from scripts.production import staff, staff_sources, worker_fill, worker_admission
import unit_lookup
from scripts.production.staff_test_support import fixture_runtime_id, StaffTestCase, production_membership, unit_membership, showrunner_relationship


class WorkerFillTests(StaffTestCase):
    def recorded_production(self, slug: str, *units: str) -> Path:
        doc = self.production(slug, *units)
        owner = showrunner_relationship(slug)
        _ = staff.set_doc(owner.id, staff.LiveDoc("natedev", doc))
        return doc

    def result(self, *, dry_run: bool = False) -> tuple[int, str, str]:
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            code = worker_fill.fill(dry_run=dry_run)
        return code, output.getvalue(), errors.getvalue()

    def pause_ledger(self, path: Path, run: Path) -> None:
        data = cast(dict[str, object], json.loads(path.read_text()))
        entries = cast(dict[str, dict[str, object]], data["workers"])
        for entry in entries.values():
            entry["ownership"] = {"kind": "Paused"}
            entry["events"] = [{"kind": "opened", "cause": "fixture launch", "run": str(run), "at": 1.0}]
        self.write_json(path, data)

    def test_non_staff_diagnostic_is_a_note_and_clean_reruns_are_unchanged(self) -> None:
        _ = self.tree()
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        note = "Codex process 817898: pane holds no staff member; not staff"

        def collect() -> tuple[staff_sources.WorkerObservation, ...]:
            print(f"staff sources: {note}", file=sys.stderr)
            return ()

        with mock.patch.object(worker_fill.FillSources, "collect", side_effect=collect):
            for _ in range(2):
                self.assertEqual(self.result(), (0, f"note: {note}\nunchanged\n", ""))
                self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_non_staff_note_does_not_hide_a_source_problem(self) -> None:
        _ = self.tree()
        note = "Codex process 817898: pane holds no staff member; not staff"

        def collect() -> tuple[staff_sources.WorkerObservation, ...]:
            print(f"staff sources: {note}", file=sys.stderr)
            print("staff sources: damaged source", file=sys.stderr)
            return ()

        with mock.patch.object(worker_fill.FillSources, "collect", side_effect=collect):
            self.assertEqual(self.result(), (
                1, f"note: {note}\nplaced 0; listed 1\n", "damaged source:record ?: source unavailable\n"))

    def test_paused_backends_follow_the_live_parent_without_a_run_and_rerun_unchanged(self) -> None:
        _ = self.recorded_production("demo", "models-unit")
        parent = unit_membership("demo", "models-unit")
        _ = self.session_record(parent.id, "models-unit")
        self.pane("w1:models", session_id=parent.id, environment={})
        run = self.worker_run("demo", "models-unit", "director")
        path = self.ledger_worker(run, "demo", "models-unit", "impl", "impl", fixture_runtime_id("thread-impl"))
        _ = self.ledger_worker(run, "demo", "models-unit", "helper", "test", fixture_runtime_id("stopped-helper"), family="claude")
        self.pause_ledger(path, run)
        shutil.rmtree(run)
        with mock.patch.object(unit_lookup, "marked_unit", side_effect=AssertionError("live identity reread")):
            code, output, errors = self.result()
        self.assertEqual((code, errors), (0, ""), output + errors)
        rows = staff.find(kind="worker", parent=parent.id)
        self.assertEqual({row.name for row in rows}, {"impl", "helper"})
        # Its filename still reflects former membership; durable worker ids take precedence.
        data = cast(dict[str, object], json.loads(path.read_text()))
        entries = cast(dict[str, dict[str, object]], data["workers"])
        for row in rows:
            entries[row.name]["staff_id"] = row.id
        stale = path.parent.parent / "former" / "former-unit.json"
        self.write_json(stale, data)
        path.unlink()
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        self.assertEqual(self.result(), (0, "unchanged\n", ""))
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_paused_workers_with_surviving_activity_override_unknown_director_evidence(self) -> None:
        _ = self.recorded_production("demo", "models-unit")
        parent = unit_membership("demo", "models-unit")
        run = self.worker_run("demo", "models-unit", "gone-director")
        path = self.ledger_worker(run, "demo", "models-unit", "impl", "impl", fixture_runtime_id("loaded-thread"))
        _ = self.ledger_worker(run, "demo", "models-unit", "helper", "test", fixture_runtime_id("live-helper"), family="claude")
        self.pause_ledger(path, run)
        _ = self.session_record(fixture_runtime_id("live-helper"), "helper")
        roster = cast(dict[str, dict[str, object]], json.loads((run / "mesh_roster.json").read_text()))
        roster["impl"]["launcher_pid"] = 0
        self.write_json(run / "mesh_roster.json", roster)
        loaded = mock.MagicMock(return_value={fixture_runtime_id("loaded-thread")})
        with mock.patch.object(unit_lookup, "marked_unit", side_effect=OSError("unreadable marks")):
            with mock.patch.object(codex_mesh, "loaded_threads", loaded):
                code, output, errors = self.result()
                self.assertEqual(loaded.call_count, 1)
        self.assertEqual((code, errors), (0, ""), output + errors)
        self.assertEqual({row.name for row in staff.find(kind="worker", parent=parent.id)}, {"impl", "helper"})
        self.assertNotIn("skipped", output)

    def test_paused_inactive_ledger_counts_once_even_without_a_staff_parent(self) -> None:
        production_membership("demo")
        run = self.worker_run("demo", "gone-unit", "gone-director")
        path = self.ledger_worker(run, "demo", "gone-unit", "impl", "impl", fixture_runtime_id("dead-thread"))
        _ = self.ledger_worker(run, "demo", "gone-unit", "helper", "test", fixture_runtime_id("dead-helper"), family="claude")
        self.pause_ledger(path, run)
        shutil.rmtree(run)
        with mock.patch.object(unit_lookup, "marked_unit", return_value=None):
            self.assertEqual(self.result(), (0, "skipped 1 not current\nunchanged\n", ""))
            self.assertEqual(self.result(), (0, "skipped 1 not current\nunchanged\n", ""))
        self.assertEqual(staff.find(kind="worker"), [])

    def test_paused_unreadable_or_incomplete_evidence_is_listed_and_never_counted(self) -> None:
        doc = self.recorded_production("demo", "models-unit")
        _ = staff.set_doc(showrunner_relationship("demo").id, staff.LiveDoc("natedev", doc))
        _ = unit_membership("demo", "models-unit")
        run = self.worker_run("demo", "models-unit", "gone-director")
        path = self.ledger_worker(run, "demo", "models-unit", "impl", "impl", fixture_runtime_id("dead-thread"))
        _ = self.ledger_worker(run, "demo", "models-unit", "helper", "test", fixture_runtime_id("dead-helper"), family="claude")
        self.pause_ledger(path, run)
        shutil.rmtree(run)
        with mock.patch.object(unit_lookup, "marked_unit", side_effect=OSError("unreadable marks")):
            code, output, errors = self.result()
        self.assertEqual(code, 1)
        self.assertIn("unreadable marks", errors)
        self.assertNotIn("skipped", output)
        registry = self.source_root / "sessions" / "broken.json"
        _ = registry.write_text("{unreadable")
        with mock.patch.object(unit_lookup, "marked_unit", return_value=None):
            code, output, errors = self.result()
        self.assertEqual(code, 1)
        self.assertIn("registry is incomplete", errors)
        self.assertNotIn("skipped", output)
        self.assertEqual(staff.find(kind="worker"), [])
        registry.unlink()
        server = run / "mesh_server.json"
        self.write_json(server, {"pid": "unreadable", "port": 12345})
        with mock.patch.object(unit_lookup, "marked_unit", return_value=None):
            code, output, errors = self.result()
        self.assertEqual(code, 1)
        self.assertIn(str(server), errors)
        self.assertNotIn("skipped", output)

    def test_paused_director_mark_keeps_live_and_unknown_outcomes_distinct(self) -> None:
        doc = self.recorded_production("demo", "models-unit")
        _ = staff.set_doc(showrunner_relationship("demo").id, staff.LiveDoc("natedev", doc))
        parent = unit_membership("demo", "models-unit")
        run = self.worker_run("demo", "models-unit", "former-director")
        path = self.ledger_worker(run, "demo", "models-unit", "impl", "impl", fixture_runtime_id("dead-thread"))
        self.pause_ledger(path, run)
        shutil.rmtree(run)
        place = HerdrPlace("agents", "w1:models")
        unknown = unit_lookup.MarkedUnit("models-unit", unit_lookup.ClaudeUnknown("unreadable director session"), place)
        with mock.patch.object(unit_lookup, "marked_unit", return_value=unknown):
            code, output, errors = self.result()
        self.assertEqual(code, 1)
        self.assertIn("unreadable director session", errors)
        self.assertNotIn("skipped", output)
        live = unit_lookup.MarkedUnit("models-unit", unit_lookup.LiveClaude("models", "new-director", "", os.getpid()), place)
        with mock.patch.object(unit_lookup, "marked_unit", return_value=live):
            code, output, errors = self.result()
        self.assertEqual((code, errors), (0, ""), output + errors)
        self.assertEqual([row.name for row in staff.find(kind="worker", parent=parent.id)], ["impl"])

    def test_missing_director_file_uses_run_started_history_for_active_ownership(self) -> None:
        _ = self.recorded_production("demo", "models-unit")
        parent = unit_membership("demo", "models-unit")
        _ = self.session_record(parent.id, "models-unit")
        self.pane("w1:models", session_id=parent.id, environment={})
        run = self.worker_run("", "", "director")
        (run / "director").unlink()
        history = run / "history.jsonl"
        # The reader intentionally reads the first history line, as the producer writes it.
        _ = history.write_text(json.dumps({"event_type": "run_started", "main_agent": {"session_id": parent.id}}) + "\n")
        self.write_json(run / "progress_history_state.json", {"history_file": str(history)})
        _ = self.roster_worker(run, "impl", "impl", fixture_runtime_id("thread-impl"), status="paused")
        code, output, errors = self.result()
        self.assertEqual((code, errors), (0, ""), output + errors)
        self.assertEqual([row.name for row in staff.find(kind="worker", parent=parent.id)], ["impl"])
        self.assertEqual(self.result(), (0, "unchanged\n", ""))

    def test_fill_places_ledger_roster_and_claude_workers_under_their_unit_once(self) -> None:
        _ = self.recorded_production("demo", "models-unit")
        production_membership("demo", "models-unit")
        parent = unit_membership("demo", "models-unit")
        run = self.worker_run("demo", "models-unit", "session-models")
        _ = self.ledger_worker(run, "demo", "models-unit", "impl", "impl", fixture_runtime_id("thread-impl"))
        _ = self.roster_worker(run, "test", "test", fixture_runtime_id("thread-test"), status="paused")
        _ = self.ledger_worker(run, "demo", "models-unit", "helper", "fix", fixture_runtime_id("session-helper"), family="claude")
        _ = self.session_record(fixture_runtime_id("session-helper"), "helper")
        _ = self.ledger_worker(run, "demo", "models-unit", "ended", "test", fixture_runtime_id("thread-ended"), retired=True)
        code, output, errors = self.result()
        self.assertEqual((code, errors), (0, ""), output + errors)
        self.assertEqual({row.name for row in staff.find(kind="worker", parent=parent.id)}, {"impl", "test", "helper"})
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        self.assertEqual(self.result(), (0, "unchanged\n", ""))
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_stale_owner_id_and_production_follow_the_unique_surviving_unit(self) -> None:
        _ = self.recorded_production("demo", "models-unit")
        production_membership("demo", "models-unit")
        parent = unit_membership("demo", "models-unit")
        run = self.worker_run("old-production", "models-unit", "director")
        _ = self.session_record(parent.id, "models-unit")
        self.pane("w1:models", session_id=parent.id, environment={
            "STAFF_ID": "sdeleted", "SHOWRUNNER_UNIT": "old-production", "SHOWRUNNER_UNIT_ID": "models-unit"})
        _ = self.roster_worker(run, "review2", "review", fixture_runtime_id("idle-review"), status="paused")
        claude_name = self.legacy_claude_worker(run, fixture_runtime_id("claude-helper"), prefix="demo", role="test")
        _ = self.session_record(fixture_runtime_id("claude-helper"), claude_name)
        with mock.patch.dict(os.environ, {"STAFF_ID": "sdeleted"}):
            allowed = worker_fill.admit("impl", run, role='impl')
            self.assertEqual(allowed.parent, parent.id)
        _ = worker_admission.publish(allowed, fixture_runtime_id("impl-native-backend"))
        self.assertEqual(self.result()[0], 0)
        self.assertEqual({row.name for row in staff.find(kind="worker", parent=parent.id)}, {"impl", "review2", claude_name})
        production_membership("other", "models-unit")
        with mock.patch.dict(os.environ, {"STAFF_ID": "sdeleted"}):
            with self.assertRaisesRegex(worker_fill.AdmissionRefused, "no unique staff entry"):
                _ = worker_fill.admit("test", run, role='test')
        code, _, errors = self.result()
        self.assertEqual(code, 1)
        self.assertIn("no unique staff entry", errors)

    def test_fill_ignores_finished_rosters_and_closed_ledger_workers(self) -> None:
        _ = self.recorded_production("demo", "models-unit")
        production_membership("demo", "models-unit")
        parent = unit_membership("demo", "models-unit")
        run = self.worker_run("demo", "models-unit", "director")
        path = self.ledger_worker(run, "demo", "models-unit", "retired", "impl", fixture_runtime_id("old-thread"), retired=True)
        data = cast(dict[str, object], json.loads(path.read_text()))
        entries = cast(dict[str, dict[str, object]], data["workers"])
        entries["adopted"] = {**entries["retired"], "name": "adopted",
                              "ownership": {"kind": "Adopted", "to_run": str(run)}}
        self.write_json(path, data)
        for linked in (False, True):
            for status in ("done", "failed", "ended", "adopted"):
                name = f"{status}-{linked}"
                _ = self.roster_worker(run, name, "review", fixture_runtime_id(f"thread-{name}"), status=status)
            for name in ("retired", "adopted"):
                _ = self.roster_worker(run, name, "impl", fixture_runtime_id("old-thread"), status="paused")
            roster = cast(dict[str, dict[str, object]], json.loads((run / "mesh_roster.json").read_text()))
            if linked:
                for entry in roster.values():
                    entry["unit_ledger"] = str(path)
                self.write_json(run / "mesh_roster.json", roster)
            with self.subTest(linked=linked):
                self.assertEqual(self.result(), (0, "unchanged\n", ""))
                self.assertEqual(staff.find(kind="worker", parent=parent.id), [])

    def test_closed_ledger_for_a_gone_unit_needs_no_parent(self) -> None:
        production_membership("demo")
        run = self.worker_run("demo", "gone-unit", "gone-director")
        path = self.ledger_worker(run, "demo", "gone-unit", "impl", "impl", fixture_runtime_id("old-thread"), retired=True)
        for linked in (False, True):
            _ = self.roster_worker(run, "impl", "impl", fixture_runtime_id("old-thread"), status="done")
            if linked:
                roster = cast(dict[str, dict[str, object]], json.loads((run / "mesh_roster.json").read_text()))
                roster["impl"]["unit_ledger"] = str(path)
                self.write_json(run / "mesh_roster.json", roster)
            with self.subTest(linked=linked):
                self.assertEqual(self.result(), (0, "skipped 1 not current\nunchanged\n", ""))
                self.assertEqual(staff.find(kind="worker"), [])

    def test_dry_run_lists_placements_without_writing_any_staff_bytes(self) -> None:
        _ = self.recorded_production("demo", "models-unit")
        production_membership("demo", "models-unit")
        run = self.worker_run("demo", "models-unit", "session-models")
        _ = self.ledger_worker(run, "demo", "models-unit", "impl", "impl", fixture_runtime_id("thread-impl"))
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        with mock.patch.object(staff, "ensure_worker", side_effect=AssertionError("dry-run wrote staff")):
            code, output, errors = self.result(dry_run=True)
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("would place impl under ", output)
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_unplaceable_ledger_is_named_while_complete_workers_still_fill(self) -> None:
        _ = self.recorded_production("demo", "models-unit")
        production_membership("demo", "models-unit")
        run = self.worker_run("demo", "models-unit", "session-models")
        _ = self.ledger_worker(run, "demo", "models-unit", "impl", "impl", fixture_runtime_id("thread-impl"))
        missing = self.worker_run("unknown", "missing-unit", "unknown-director")
        path = self.ledger_worker(missing, "unknown", "missing-unit", "test", "test", fixture_runtime_id("thread-missing"))
        code, _, errors = self.result()
        self.assertEqual(code, 1)
        self.assertIn(f"{path}:workers test:", errors)
        self.assertEqual({row.name for row in staff.find(kind="worker")}, {"impl"})

    def test_showrunner_held_astra_is_placed_under_its_showrunner_once(self) -> None:
        _ = self.recorded_production("demo")
        parent = showrunner_relationship("demo")
        _ = self.worker_design("demo", "demo-design-astra-designer", fixture_runtime_id("thread-designer"))
        code, output, errors = self.result()
        self.assertEqual((code, errors), (0, ""), output + errors)
        self.assertEqual([row.name for row in staff.find(kind="worker", parent=parent.id)],
                         ["demo-design-astra-designer"])
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        self.assertEqual(self.result(), (0, "unchanged\n", ""))
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_renamed_showrunner_keeps_the_design_worker_parent_and_legacy_admission(self) -> None:
        _ = self.recorded_production("natedev")
        parent = showrunner_relationship("natedev")
        self.write_json(Path(os.environ["SHOWRUNNER_STATE_DIR"]) / "natedev/rename.json",
                        {"old": "build-followups", "new": "natedev"})
        design = self.worker_design("natedev", "held-designer", fixture_runtime_id("thread-held"))
        before = design.read_bytes(), design.stat().st_mtime_ns
        worker = staff.find(kind="worker", parent=parent.id)[0]
        _ = staff.rename(parent.id, "renamed-owner")
        code, output, errors = self.result()
        self.assertEqual((code, errors), (0, ""), output + errors)
        self.assertEqual([row.name for row in staff.find(kind="worker", parent=parent.id)], ["held-designer"])
        self.assertEqual(staff.find(kind="worker", parent=parent.id)[0].id, worker.id)
        self.assertEqual((design.read_bytes(), design.stat().st_mtime_ns), before)
        with mock.patch.dict(os.environ, {"STAFF_ID": "sunrelated"}):
            admitted = worker_fill.admit("review", self.root, production="build-followups", role='review')
        self.assertEqual(admitted.parent, parent.id)
        self.assertEqual(self.result(), (0, "unchanged\n", ""))

    def test_design_parent_ignores_ambiguous_or_unreadable_rename_journals(self) -> None:
        _ = self.recorded_production("first")
        _ = self.recorded_production("second")
        production_membership("first")
        production_membership("second")
        root = Path(os.environ["SHOWRUNNER_STATE_DIR"])
        first, second = root / "first/rename.json", root / "second/rename.json"
        self.write_json(first, {"old": "former", "new": "first"})
        self.write_json(second, {"old": "former", "new": "second"})
        parent = showrunner_relationship("first")
        design = self.worker_design("first", "held-designer", fixture_runtime_id("thread-held"))
        before = design.read_bytes(), design.stat().st_mtime_ns
        self.assertEqual(self.result(), (0, "unchanged\n", ""))
        with self.assertRaisesRegex(worker_fill.AdmissionRefused, "no unique renamed showrunner"):
            _ = worker_fill.admit("review", self.root, production="former", role="review")
        self.assertEqual([row.name for row in staff.find(kind="worker", parent=parent.id)], ["held-designer"])
        _ = second.write_text("{unreadable journal")
        code, output, errors = self.result()
        self.assertEqual((code, errors), (0, ""), output + errors)
        with self.assertRaisesRegex(worker_fill.AdmissionRefused, str(second)):
            _ = worker_fill.admit("review", self.root, production="former", role="review")
        self.assertNotIn("skipped", output)
        self.assertEqual([row.name for row in staff.find(kind="worker", parent=parent.id)], ["held-designer"])
        self.assertEqual((design.read_bytes(), design.stat().st_mtime_ns), before)

    def test_confirmed_inactive_sources_are_counted_once_and_rerun_unchanged(self) -> None:
        production_membership("demo")
        run = self.worker_run("demo", "gone-unit", "gone-director")
        path = self.ledger_worker(run, "demo", "gone-unit", "impl", "impl", fixture_runtime_id("dead-thread"))
        (run / "mesh_server.json").unlink()
        self.write_json(run / "mesh_roster.json", {"impl": {
            "role": "impl", "status": "paused", "thread_id": fixture_runtime_id("dead-thread"), "launcher_pid": 0,
            "unit_ledger": str(path)}})
        _ = (run / "workers").write_text("gone-session\tdead-claude\n")
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                self.assertEqual(self.result(), (0, "skipped 2 not current\nunchanged\n", ""))
                self.assertEqual(staff.find(kind="worker"), [])

    def test_mixed_ledger_keeps_the_live_worker_without_importing_dead_entries(self) -> None:
        _ = self.recorded_production("demo", "models-unit")
        production_membership("demo", "models-unit")
        parent = unit_membership("demo", "models-unit")
        run = self.worker_run("demo", "models-unit", "gone-director")
        (run / "mesh_server.json").unlink()
        _ = self.ledger_worker(run, "demo", "models-unit", "live-helper", "test", fixture_runtime_id("live-session"), family="claude")
        _ = self.ledger_worker(run, "demo", "models-unit", "dead-helper", "test", fixture_runtime_id("dead-session"), family="claude")
        _ = self.session_record(fixture_runtime_id("live-session"), "live-helper")
        code, output, errors = self.result()
        self.assertEqual((code, errors), (0, ""), output + errors)
        self.assertEqual([row.name for row in staff.find(kind="worker", parent=parent.id)], ["live-helper"])
        self.assertNotIn("skipped", output)
        self.assertEqual(self.result(), (0, "unchanged\n", ""))

    def test_unreadable_or_unavailable_activity_is_listed_instead_of_counted(self) -> None:
        production_membership("demo")
        run = self.worker_run("demo", "gone-unit", "gone-director")
        path = self.ledger_worker(run, "demo", "gone-unit", "impl", "impl", fixture_runtime_id("thread-impl"))
        self.write_json(run / "mesh_roster.json", {"impl": {
            "role": "impl", "status": "running", "thread_id": fixture_runtime_id("thread-impl"), "launcher_pid": 0}})
        # A live server whose port is missing cannot prove the thread stopped.
        code, output, errors = self.result()
        self.assertEqual(code, 1)
        self.assertIn("unavailable", errors)
        self.assertNotIn("skipped", output)
        _ = path.write_text("{unreadable source")
        code, output, errors = self.result()
        self.assertEqual(code, 1)
        self.assertIn(str(path), errors)
        self.assertNotIn("skipped", output)
        self.assertEqual(staff.find(kind="worker"), [])

    def test_display_and_seed_use_server_pid_without_opening_a_codex_connection(self) -> None:
        _ = self.recorded_production("demo")
        production_membership("demo")
        parent = showrunner_relationship("demo")
        _ = self.session_record(parent.id, "demo")
        run = self.worker_run("demo", "", "gone-director")
        _ = self.roster_worker(run, "impl", "impl", "3b08f4bb-fc3c-54e2-8bdc-71bce6417158")
        roster = cast(dict[str, dict[str, object]], json.loads((run / "mesh_roster.json").read_text()))
        roster["impl"]["launcher_pid"] = 0
        self.write_json(run / "mesh_roster.json", roster)
        with mock.patch.object(codex_mesh, "Client", side_effect=AssertionError("display/seed opened RPC")):
            with redirect_stdout(io.StringIO()):
                staff_sources.seed()
            value = staff_sources.live()
        row = staff.find(kind="worker", parent=parent.id)[0]
        self.assertEqual(row.name, "impl")
        self.assertIn("3b08f4bb-fc3c-54e2-8bdc-71bce6417158", str(value["members"]))

    def test_fill_checks_loaded_threads_once_and_counts_an_empty_live_server(self) -> None:
        _ = self.recorded_production("demo", "models-unit")
        production_membership("demo", "models-unit")
        parent = unit_membership("demo", "models-unit")
        run = self.worker_run("demo", "models-unit", "gone-director")
        for name in ("impl", "test"):
            _ = self.roster_worker(run, name, name, fixture_runtime_id(f"thread-{name}"))
        roster = cast(dict[str, dict[str, object]], json.loads((run / "mesh_roster.json").read_text()))
        for entry in roster.values():
            entry["launcher_pid"] = 0
        self.write_json(run / "mesh_roster.json", roster)
        self.write_json(run / "mesh_server.json", {"pid": os.getpid(), "port": 12345})
        rpc = mock.MagicMock(return_value={"result": {"data": [], "nextCursor": None}})
        client = mock.MagicMock(call=rpc)
        with mock.patch.object(codex_mesh, "Client", return_value=client) as clients:
            self.assertEqual(self.result(), (0, "skipped 1 not current\nunchanged\n", ""))
            self.assertEqual(clients.call_count, 1)
        self.write_json(run / "mesh_retired.json", {"servers": [{"pid": os.getpid(), "port": 12346}]})
        rpc.side_effect = [{"result": {"data": []}}, {"result": {"data": [fixture_runtime_id("thread-test")]}}]
        with mock.patch.object(codex_mesh, "Client", return_value=client) as clients:
            code, output, errors = self.result()
            self.assertEqual((code, errors), (0, ""), output + errors)
            self.assertEqual(clients.call_count, 2)
        self.assertEqual([row.name for row in staff.find(kind="worker", parent=parent.id)], ["test"])

    def test_failed_probe_is_cached_and_malformed_retired_records_are_listed(self) -> None:
        production_membership("demo", "models-unit")
        run = self.worker_run("demo", "models-unit", "gone-director")
        for name in ("impl", "test"):
            _ = self.ledger_worker(run, "demo", "models-unit", name, name, fixture_runtime_id(f"thread-{name}"))
        roster = cast(dict[str, dict[str, object]], json.loads((run / "mesh_roster.json").read_text()))
        for entry in roster.values():
            entry["launcher_pid"] = 0
        self.write_json(run / "mesh_roster.json", roster)
        self.write_json(run / "mesh_server.json", {"pid": os.getpid(), "port": 12345})
        with mock.patch.object(codex_mesh, "Client", side_effect=OSError("probe failed")) as clients:
            code, output, errors = self.result()
        self.assertEqual(code, 1)
        self.assertIn("probe failed", errors)
        self.assertNotIn("skipped", output)
        self.assertEqual(clients.call_count, 1)
        retired = run / "mesh_retired.json"
        invalid_records: tuple[dict[str, object], ...] = (
            {"servers": None}, {"servers": [{"pid": "bad", "port": 12346}]},
            {"servers": [{"pid": os.getpid(), "port": "bad"}]})
        for invalid in invalid_records:
            self.write_json(retired, invalid)
            with mock.patch.object(codex_mesh, "Client") as clients:
                code, output, errors = self.result()
            self.assertEqual(code, 1)
            self.assertIn(str(retired), errors)
            self.assertNotIn("skipped", output)
            clients.assert_not_called()
        self.assertEqual(staff.find(kind="worker"), [])

    def test_inactive_run_cannot_add_a_mesh_astra_worker_back(self) -> None:
        _ = self.recorded_production("demo")
        parent = showrunner_relationship("demo")
        _ = self.session_record(parent.id, "demo")
        self.pane("w1:demo", session_id=parent.id, environment={})
        run = self.worker_run("demo", "gone-unit", "gone-director")
        _ = self.astra_worker(run, "astra-review", fixture_runtime_id("thread-astra"), parent.id)
        self.write_json(run / "mesh_server.json", {"pid": os.getpid(), "port": 12345})
        roster = cast(dict[str, dict[str, object]], json.loads((run / "mesh_roster.json").read_text()))
        roster["astra-review"]["launcher_pid"] = 0
        self.write_json(run / "mesh_roster.json", roster)
        rpc = mock.MagicMock(return_value={"result": {"data": []}})
        client = mock.MagicMock(call=rpc)
        with mock.patch.object(codex_mesh, "Client", return_value=client):
            self.assertEqual(self.result(), (0, "skipped 1 not current\nunchanged\n", ""))
        self.assertEqual(staff.find(kind="worker"), [])

    def test_live_launcher_wins_over_uncertain_director_evidence(self) -> None:
        production_membership("demo", "models-unit")
        parent = unit_membership("demo", "models-unit")
        run = self.worker_run("demo", "models-unit", "gone-director")
        _ = self.roster_worker(run, "impl", "impl", fixture_runtime_id("thread-impl"))
        with (mock.patch.object(staff_sources._WorkerSources, "director_live",  # pyright: ignore[reportPrivateUsage]
                                side_effect=ValueError("registry unavailable")),
              mock.patch.object(codex_mesh, "Client", side_effect=AssertionError("live launcher opened RPC"))):
            code, output, errors = self.result()
        self.assertEqual((code, errors), (0, ""), output + errors)
        self.assertEqual([row.name for row in staff.find(kind="worker", parent=parent.id)], ["impl"])

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
        self.assertEqual(admitted.row.parent, parent.id)
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
        self.assertEqual(admitted.row.parent, parent.id)
        self.assertEqual(staff.find(kind="worker", parent=parent.id), [admitted.row])
        self.assertEqual(state.read_text(), "{malformed fallback state")

    def test_admission_refuses_an_unknown_parent_without_writing_a_row(self) -> None:
        production_membership("demo")
        run = self.worker_run("demo", "missing-unit", "director")
        before = self.staff_file.read_bytes()
        with self.assertRaisesRegex(worker_fill.AdmissionRefused, "staff: impl: not opened: no unit staff row"):
            _ = worker_fill.admit("impl", run, role='impl')
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_unreadable_staff_refuses_admission_and_keeps_the_record(self) -> None:
        self.staff_file.parent.mkdir(parents=True, exist_ok=True)
        _ = self.staff_file.write_text("{bad record")
        with self.assertRaisesRegex(worker_fill.AdmissionRefused, "staff: impl: not opened:"):
            _ = worker_fill.admit("impl", self.root, role='impl')
        self.assertEqual(self.staff_file.read_text(), "{bad record")

    def test_refused_staff_write_does_not_leave_a_worker_row(self) -> None:
        _ = self.recorded_production("demo", "models-unit")
        production_membership("demo", "models-unit")
        run = self.worker_run("demo", "models-unit", "director")
        before = self.staff_file.read_bytes()
        allowed = worker_fill.admit("impl", run, role='impl')
        with mock.patch.object(staff, "ensure_worker", side_effect=staff.StaffRefused("record is locked")):
            with self.assertRaisesRegex(worker_fill.AdmissionRefused, "publication refused: record is locked"):
                _ = worker_fill.publish(allowed, fixture_runtime_id("refused-worker"))
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_retired_unit_cannot_admit_a_worker(self) -> None:
        _ = self.recorded_production("demo", "models-unit")
        production_membership("demo", "models-unit")
        parent = unit_membership("demo", "models-unit")
        _ = staff.set_worktree(parent.id, staff.RemovedWorktree("natedev", self.root / "gone"), expected=parent.worktree)
        run = self.worker_run("demo", "models-unit", "director")
        with redirect_stderr(io.StringIO()) as errors:
            allowed = worker_fill.admit("impl", run, role='impl')
        self.assertEqual(allowed.parent, parent.id)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn("retired", errors.getvalue())
        self.assertEqual(staff.find(kind="worker"), [])

    def test_new_codex_threads_are_created_only_inside_admitted_starters(self) -> None:
        """Guard raw codex_mesh thread creation and its nominal admission boundary, including replacements."""
        source = Path(worker_fill.__file__).parents[1] / "agents" / "codex_mesh.py"
        tree = ast.parse(source.read_text())
        starters = {"_attach_and_run", "_open_admitted_worker", "_prime_admitted_worker", "_fork_admitted_worker", "_resume_worker_thread"}
        found: set[str] = set()
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef):
                continue
            requests = [node for node in ast.walk(function) if isinstance(node, ast.Call)
                        and any(isinstance(argument, ast.Constant) and argument.value in ("thread/start", "thread/fork")
                                for argument in node.args)]
            if not requests:
                continue
            with self.subTest(starter=function.name):
                self.assertIn(function.name, starters)
                found.add(function.name)
                for request in requests:
                    self.assertIsInstance(request.func, ast.Name)
                    assert isinstance(request.func, ast.Name)
                    self.assertEqual(request.func.id, "_create_published_worker")
                arguments = [*function.args.args, *function.args.kwonlyargs]
                self.assertTrue(any(isinstance(argument.annotation, ast.Name)
                                    and argument.annotation.id == "WorkerStartAllowed" for argument in arguments))
                resolved = cast(dict[str, object], get_type_hints(cast(FunctionType, getattr(codex_mesh, function.name))))
                admitted = [argument for argument in arguments if isinstance(argument.annotation, ast.Name)
                            and argument.annotation.id == "WorkerStartAllowed"]
                for argument in admitted:
                    self.assertIs(resolved[argument.arg], worker_admission.WorkerStartAllowed)
        self.assertEqual(found, starters)
        helper = next(function for function in tree.body if isinstance(function, ast.FunctionDef)
                      and function.name == "_create_published_worker")
        permission = next(argument for argument in helper.args.args if argument.arg == "allowed")
        self.assertIsInstance(permission.annotation, ast.Name)
        assert isinstance(permission.annotation, ast.Name)
        self.assertEqual(permission.annotation.id, "WorkerStartAllowed")
        resolved_helper = cast(dict[str, object], get_type_hints(codex_mesh._create_published_worker))  # pyright: ignore[reportPrivateUsage]
        self.assertIs(resolved_helper["allowed"], worker_admission.WorkerStartAllowed)
        self.assertIsInstance(worker_admission.WorkerStartAllowed, type)
        self.assertFalse(getattr(worker_admission.WorkerStartAllowed, "_is_protocol", False))

    def test_admission_type_rejects_a_structural_row_stand_in(self) -> None:
        probe = self.root / "admission_probe.py"
        _ = probe.write_text("\n".join([
            "from pathlib import Path",
            "from scripts.production import staff",
            "from scripts.production.worker_admission import WorkerStartAllowed",
            "class StandIn:",
            "    def __init__(self, row: staff.StaffRow) -> None: self.row = row",
            "def needs_admission(value: WorkerStartAllowed) -> None: pass",
            "def caller(row: staff.StaffRow) -> None:",
            '    needs_admission(WorkerStartAllowed("worker", row.id, Path("."), "impl"))',
            "    needs_admission(StandIn(row))",
            "",
        ]))
        config = self.root / "pyrightconfig.json"
        self.write_json(config, {"include": [probe.name], "extraPaths": [str(Path(worker_fill.__file__).parents[2])],
                                 "typeCheckingMode": "standard"})
        result = subprocess.run(["basedpyright", "--outputjson", "--project", str(config)],
                                capture_output=True, text=True, check=False)
        report = cast(dict[str, object], json.loads(result.stdout))
        diagnostics = cast(list[dict[str, object]], report["generalDiagnostics"])
        errors = [entry for entry in diagnostics if entry["severity"] == "error"]
        self.assertEqual(len(errors), 1, result.stdout + result.stderr)
        self.assertEqual(errors[0].get("rule"), "reportArgumentType", result.stdout)
        self.assertIn('StandIn', str(errors[0]["message"]))
        self.assertIn('WorkerStartAllowed', str(errors[0]["message"]))
