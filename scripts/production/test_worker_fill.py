"""Worker membership comes from producer records, with no worker side effects."""
from __future__ import annotations

import ast
import io
import os
import json
import subprocess
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock
from types import FunctionType
from typing import cast, get_type_hints

from scripts.agents import codex_mesh
from scripts.production import staff, worker_fill, worker_admission
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

    def test_stale_owner_id_and_production_follow_the_unique_surviving_unit(self) -> None:
        _ = self.production("demo", "models-unit")
        production_membership("demo", "models-unit")
        parent = unit_membership("demo", "models-unit")
        run = self.worker_run("old-production", "models-unit", "director")
        _ = self.session_record("director", "models-unit")
        self.pane("w1:models", session_id="director", environment={
            "STAFF_ID": "sdeleted", "SHOWRUNNER_UNIT": "old-production", "SHOWRUNNER_UNIT_ID": "models-unit"})
        _ = self.roster_worker(run, "review2", "review", "idle-review", status="paused")
        claude_name = self.legacy_claude_worker(run, "claude-helper", prefix="demo", role="test")
        _ = self.session_record("claude-helper", claude_name)
        with mock.patch.dict(os.environ, {"STAFF_ID": "sdeleted"}):
            self.assertEqual(worker_fill.admit("impl", run).row.parent, parent.id)
        self.assertEqual(self.result()[0], 0)
        self.assertEqual({row.name for row in staff.find(kind="worker", parent=parent.id)}, {"impl", "review2", claude_name})
        production_membership("other", "models-unit")
        with mock.patch.dict(os.environ, {"STAFF_ID": "sdeleted"}):
            with self.assertRaisesRegex(worker_fill.AdmissionRefused, "no unique staff entry"):
                _ = worker_fill.admit("test", run)
        code, _, errors = self.result()
        self.assertEqual(code, 1)
        self.assertIn("no unique staff entry", errors)

    def test_fill_ignores_finished_rosters_and_closed_ledger_workers(self) -> None:
        _ = self.production("demo", "models-unit")
        production_membership("demo", "models-unit")
        parent = unit_membership("demo", "models-unit")
        run = self.worker_run("demo", "models-unit", "director")
        path = self.ledger_worker(run, "demo", "models-unit", "retired", "impl", "old-thread", retired=True)
        data = cast(dict[str, object], json.loads(path.read_text()))
        entries = cast(dict[str, dict[str, object]], data["workers"])
        entries["adopted"] = {**entries["retired"], "name": "adopted",
                              "ownership": {"kind": "Adopted", "to_run": str(run)}}
        self.write_json(path, data)
        for linked in (False, True):
            for status in ("done", "failed", "ended", "adopted"):
                name = f"{status}-{linked}"
                _ = self.roster_worker(run, name, "review", f"thread-{name}", status=status)
            for name in ("retired", "adopted"):
                _ = self.roster_worker(run, name, "impl", "old-thread", status="paused")
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
        path = self.ledger_worker(run, "demo", "gone-unit", "impl", "impl", "old-thread", retired=True)
        for linked in (False, True):
            _ = self.roster_worker(run, "impl", "impl", "old-thread", status="done")
            if linked:
                roster = cast(dict[str, dict[str, object]], json.loads((run / "mesh_roster.json").read_text()))
                roster["impl"]["unit_ledger"] = str(path)
                self.write_json(run / "mesh_roster.json", roster)
            with self.subTest(linked=linked):
                self.assertEqual(self.result(), (0, "unchanged\n", ""))
                self.assertEqual(staff.find(kind="worker"), [])

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
        self.assertIn(f"{path}:workers test:", errors)
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
        self.assertEqual(row.row.parent, parent.id)
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(worker_fill.admit("hana_catalyst-impl", run), row)
        self.assertEqual((output.getvalue(), errors.getvalue()), ("", ""))
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_staff_id_admits_without_reading_an_unrelated_unreadable_pane_record(self) -> None:
        production_membership("demo", "models-unit")
        parent = unit_membership("demo", "models-unit")
        run = self.worker_run("demo", "models-unit", "director")
        panes = Path(os.environ["HERDR_PANE_RECORD_DIR"]) / "agents.json"
        panes.parent.mkdir(parents=True, exist_ok=True)
        _ = panes.write_text("{unreadable unrelated pane")
        with mock.patch.dict(os.environ, {"STAFF_ID": parent.id}):
            admitted = worker_fill.admit("impl", run)
        self.assertEqual(admitted.row.parent, parent.id)
        self.assertEqual(staff.find(kind="worker", parent=parent.id), [admitted.row])
        self.assertEqual(panes.read_text(), "{unreadable unrelated pane")

    def test_staff_id_admits_without_reading_malformed_fallback_run_state(self) -> None:
        production_membership("demo", "models-unit")
        parent = unit_membership("demo", "models-unit")
        run = self.worker_run("demo", "models-unit", "director")
        state = run / "progress_history_state.json"
        _ = state.write_text("{malformed fallback state")
        with mock.patch.dict(os.environ, {"STAFF_ID": parent.id}):
            admitted = worker_fill.admit("impl", run)
        self.assertEqual(admitted.row.parent, parent.id)
        self.assertEqual(staff.find(kind="worker", parent=parent.id), [admitted.row])
        self.assertEqual(state.read_text(), "{malformed fallback state")

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

    def test_new_codex_threads_are_created_only_inside_admitted_starters(self) -> None:
        """Guard raw codex_mesh thread creation and its nominal admission boundary, including replacements."""
        source = Path(worker_fill.__file__).parents[1] / "agents" / "codex_mesh.py"
        tree = ast.parse(source.read_text())
        starters = {"_attach_and_run", "_open_admitted_worker", "_prime_admitted_worker", "_fork_admitted_worker", "_resume_worker_thread"}
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
                arguments = [*function.args.args, *function.args.kwonlyargs]
                self.assertTrue(any(isinstance(argument.annotation, ast.Name)
                                    and argument.annotation.id == "WorkerAdmitted" for argument in arguments))
                resolved = cast(dict[str, object], get_type_hints(cast(FunctionType, getattr(codex_mesh, function.name))))
                admitted = [argument for argument in arguments if isinstance(argument.annotation, ast.Name)
                            and argument.annotation.id == "WorkerAdmitted"]
                for argument in admitted:
                    self.assertIs(resolved[argument.arg], worker_admission.WorkerAdmitted)
        self.assertIsInstance(worker_admission.WorkerAdmitted, type)
        self.assertFalse(getattr(worker_admission.WorkerAdmitted, "_is_protocol", False))

    def test_admission_type_rejects_a_structural_row_stand_in(self) -> None:
        probe = self.root / "admission_probe.py"
        _ = probe.write_text("\n".join([
            "from scripts.production import staff",
            "from scripts.production.worker_admission import WorkerAdmitted",
            "class StandIn:",
            "    def __init__(self, row: staff.StaffRow) -> None: self.row = row",
            "def needs_admission(value: WorkerAdmitted) -> None: pass",
            "def caller(row: staff.StaffRow) -> None:",
            "    needs_admission(WorkerAdmitted(row))",
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
        self.assertIn('WorkerAdmitted', str(errors[0]["message"]))
