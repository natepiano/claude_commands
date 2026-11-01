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
from scripts.production import staff, staff_sources, worker_fill, worker_admission
from scripts.production.staff_test_support import StaffTestCase, production_membership, unit_membership, showrunner_relationship


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
        _ = self.session_record("session-helper", "helper")
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
                self.assertEqual(self.result(), (0, "skipped 1 not current\nunchanged\n", ""))
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

    def test_showrunner_held_astra_is_placed_under_its_showrunner_once(self) -> None:
        _ = self.production("demo")
        parent = showrunner_relationship("demo")
        _ = self.worker_design("demo", "demo-design-astra-designer", "thread-designer")
        code, output, errors = self.result()
        self.assertEqual((code, errors), (0, ""), output + errors)
        self.assertEqual([row.name for row in staff.find(kind="worker", parent=parent.id)],
                         ["demo-design-astra-designer"])
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        self.assertEqual(self.result(), (0, "unchanged\n", ""))
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_renamed_showrunner_holder_uses_its_journal_for_fill_and_admission(self) -> None:
        _ = self.production("natedev")
        parent = showrunner_relationship("natedev")
        self.write_json(Path(os.environ["SHOWRUNNER_STATE_DIR"]) / "natedev/rename.json",
                        {"old": "build-followups", "new": "natedev"})
        _ = self.worker_design("build-followups", "held-designer", "thread-held")
        code, output, errors = self.result()
        self.assertEqual((code, errors), (0, ""), output + errors)
        self.assertEqual([row.name for row in staff.find(kind="worker", parent=parent.id)], ["held-designer"])
        with mock.patch.dict(os.environ, {"STAFF_ID": "sunrelated"}):
            admitted = worker_fill.admit("review", self.root, production="build-followups")
        self.assertEqual(admitted.row.parent, parent.id)
        self.assertEqual(self.result(), (0, "unchanged\n", ""))

    def test_renamed_holder_with_ambiguous_or_unreadable_journals_stays_listed(self) -> None:
        _ = self.production("first")
        _ = self.production("second")
        production_membership("first")
        production_membership("second")
        root = Path(os.environ["SHOWRUNNER_STATE_DIR"])
        first, second = root / "first/rename.json", root / "second/rename.json"
        self.write_json(first, {"old": "former", "new": "first"})
        self.write_json(second, {"old": "former", "new": "second"})
        _ = self.worker_design("former", "held-designer", "thread-held")
        code, _, errors = self.result()
        self.assertEqual(code, 1)
        self.assertIn("no unique renamed showrunner", errors)
        self.assertEqual(staff.find(kind="worker"), [])
        _ = second.write_text("{unreadable journal")
        code, output, errors = self.result()
        self.assertEqual(code, 1)
        self.assertIn(str(second), errors)
        self.assertNotIn("skipped", output)
        self.assertEqual(staff.find(kind="worker"), [])

    def test_confirmed_inactive_sources_are_counted_once_and_rerun_unchanged(self) -> None:
        production_membership("demo")
        run = self.worker_run("demo", "gone-unit", "gone-director")
        path = self.ledger_worker(run, "demo", "gone-unit", "impl", "impl", "dead-thread")
        (run / "mesh_server.json").unlink()
        self.write_json(run / "mesh_roster.json", {"impl": {
            "role": "impl", "status": "paused", "thread_id": "dead-thread", "launcher_pid": 0,
            "unit_ledger": str(path)}})
        _ = (run / "workers").write_text("gone-session\tdead-claude\n")
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                self.assertEqual(self.result(), (0, "skipped 2 not current\nunchanged\n", ""))
                self.assertEqual(staff.find(kind="worker"), [])

    def test_mixed_ledger_keeps_the_live_worker_without_importing_dead_entries(self) -> None:
        _ = self.production("demo", "models-unit")
        production_membership("demo", "models-unit")
        parent = unit_membership("demo", "models-unit")
        run = self.worker_run("demo", "models-unit", "gone-director")
        (run / "mesh_server.json").unlink()
        _ = self.ledger_worker(run, "demo", "models-unit", "live-helper", "test", "live-session", family="claude")
        _ = self.ledger_worker(run, "demo", "models-unit", "dead-helper", "test", "dead-session", family="claude")
        _ = self.session_record("live-session", "live-helper")
        code, output, errors = self.result()
        self.assertEqual((code, errors), (0, ""), output + errors)
        self.assertEqual([row.name for row in staff.find(kind="worker", parent=parent.id)], ["live-helper"])
        self.assertNotIn("skipped", output)
        self.assertEqual(self.result(), (0, "unchanged\n", ""))

    def test_unreadable_or_unavailable_activity_is_listed_instead_of_counted(self) -> None:
        production_membership("demo")
        run = self.worker_run("demo", "gone-unit", "gone-director")
        path = self.ledger_worker(run, "demo", "gone-unit", "impl", "impl", "thread-impl")
        self.write_json(run / "mesh_roster.json", {"impl": {
            "role": "impl", "status": "running", "thread_id": "thread-impl", "launcher_pid": 0}})
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
        _ = self.production("demo")
        production_membership("demo")
        parent = showrunner_relationship("demo")
        _ = self.session_record("session-demo", "demo")
        run = self.worker_run("demo", "", "gone-director")
        _ = self.roster_worker(run, "impl", "impl", "not-loaded")
        roster = cast(dict[str, dict[str, object]], json.loads((run / "mesh_roster.json").read_text()))
        roster["impl"]["launcher_pid"] = 0
        self.write_json(run / "mesh_roster.json", roster)
        with mock.patch.object(codex_mesh, "Client", side_effect=AssertionError("display/seed opened RPC")):
            with redirect_stdout(io.StringIO()):
                staff_sources.seed()
            value = staff_sources.live()
        row = staff.find(kind="worker", parent=parent.id)[0]
        self.assertEqual(row.name, "impl")
        self.assertIn("not-loaded", str(value["members"]))

    def test_fill_checks_loaded_threads_once_and_counts_an_empty_live_server(self) -> None:
        _ = self.production("demo", "models-unit")
        production_membership("demo", "models-unit")
        parent = unit_membership("demo", "models-unit")
        run = self.worker_run("demo", "models-unit", "gone-director")
        for name in ("impl", "test"):
            _ = self.roster_worker(run, name, name, f"thread-{name}")
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
        rpc.side_effect = [{"result": {"data": []}}, {"result": {"data": ["thread-test"]}}]
        with mock.patch.object(codex_mesh, "Client", return_value=client) as clients:
            code, output, errors = self.result()
            self.assertEqual((code, errors), (0, ""), output + errors)
            self.assertEqual(clients.call_count, 2)
        self.assertEqual([row.name for row in staff.find(kind="worker", parent=parent.id)], ["test"])

    def test_failed_probe_is_cached_and_malformed_retired_records_are_listed(self) -> None:
        production_membership("demo", "models-unit")
        run = self.worker_run("demo", "models-unit", "gone-director")
        for name in ("impl", "test"):
            _ = self.ledger_worker(run, "demo", "models-unit", name, name, f"thread-{name}")
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
        _ = self.production("demo")
        parent = showrunner_relationship("demo")
        _ = self.session_record("manager", "demo")
        self.pane("w1:demo", session_id="manager", environment={"STAFF_ID": parent.id})
        run = self.worker_run("demo", "gone-unit", "gone-director")
        _ = self.astra_worker(run, "astra-review", "thread-astra", "manager")
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
        _ = self.roster_worker(run, "impl", "impl", "thread-impl")
        with (mock.patch.object(staff_sources._WorkerSources, "director_live",  # pyright: ignore[reportPrivateUsage]
                                side_effect=ValueError("registry unavailable")),
              mock.patch.object(codex_mesh, "Client", side_effect=AssertionError("live launcher opened RPC"))):
            code, output, errors = self.result()
        self.assertEqual((code, errors), (0, ""), output + errors)
        self.assertEqual([row.name for row in staff.find(kind="worker", parent=parent.id)], ["impl"])

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
