"""Observe worker identities without importing staff rows."""
from __future__ import annotations

from scripts.production.staff_test_support import fixture_member, fixture_runtime_id

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import cast, override
from unittest import mock


import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.lib.atomic_file import write_text_atomic
from scripts.lib import unit_runs
from scripts.production import staff, staff_sources, worker_fill, session_staff
from scripts.production.staff_test_support import StaffTestCase, production_membership, unit_membership
from scripts.shutdown import pane_record, remote
import herdr
from scripts.production import showrunners
from scripts.production.session_staff import MemberSources


class StaffSourcesTests(StaffTestCase):

    @override
    def production(self, slug: str, *units: str, workspace: str = "", parent: str = "") -> Path:
        doc = super().production(slug, *units, workspace=workspace)
        owner = fixture_member("showrunner", name=workspace or slug)
        _ = staff.set_doc(owner.id, staff.LiveDoc(remote.local_machine(), doc))
        if parent:
            ancestor = fixture_member("showrunner", name=parent)
            _ = staff.move(owner.id, ancestor.id)
        return doc

    @override
    def tree(self) -> tuple[staff.StaffRow, staff.StaffRow, staff.StaffRow]:
        owner, unit, worker = super().tree()
        owner = staff.set_doc(owner.id, staff.LiveDoc("natedev", self.source_root / "sdlc-production.md"))
        return owner, unit, worker

    def test_live_keeps_the_unit_and_reports_unreadable_branch_or_missing_configuration(self) -> None:
        fixture = self.row_production()
        _ = self.session_record(fixture.runner.current_session, fixture.runner.name)
        _ = self.session_record(fixture.unit.current_session, fixture.unit.name)
        before = self.staff_file.read_bytes()
        unavailable = fixture.worktree.with_name("unavailable-member")
        for failure in ("branch", "configuration"):
            with self.subTest(failure=failure):
                if failure == "branch":
                    _ = fixture.worktree.rename(unavailable)
                else:
                    _ = unavailable.rename(fixture.worktree)
                    (fixture.worktree / ".unit.toml").unlink()
                with redirect_stderr(io.StringIO()) as errors:
                    document = staff_sources.live()
                self.assertFalse(document["complete"])
                members = cast(list[dict[str, object]], document["members"])
                self.assertIn(fixture.unit.id, [member["id"] for member in members])
                lines = [line for line in errors.getvalue().splitlines() if fixture.unit.name in line]
                self.assertEqual(len(lines), 1, errors.getvalue())
                self.assertIn(str(fixture.worktree), lines[0])
                if failure == "configuration":
                    self.assertIn(".unit.toml", lines[0])
                self.assertEqual(self.staff_file.read_bytes(), before)


    @override
    def setUp(self) -> None:
        super().setUp()
        # Keep the host's scratch policy, with a private root for this fixture.
        _ = self.enterContext(mock.patch.object(showrunners, "SCRATCH_ROOT", self.root / "scratch-checkout"))

    @override
    def fill_production(self, slug: str, *units: str) -> Path:
        doc = super().fill_production(slug, *units)
        write_text_atomic(doc, f"- **Showrunner checkout:** `{doc.parents[1]}`\n"
                          + "- **Merge branch:** `main`\n" + doc.read_text())
        return doc


    def director_pane(self, pane_id: str, row: staff.StaffRow, folder: Path, environment: dict[str, str]) -> None:
        self.pane(pane_id, session_id=row.id, environment=environment)
        entries = pane_record.read("agents")
        entries[pane_id]["cwd"] = str(folder)
        pane_record.save("agents", entries)


    def test_live_staff_roots_a_surviving_style_child_and_keeps_its_unit_after_parent_wrap(self) -> None:
        parent_doc = self.production("parent")
        style_doc = self.production("style", "names-unit", parent="parent")
        write_text_atomic(style_doc, "- **Kind:** style\n" + style_doc.read_text())
        self.pane("w1:style", session_id="ffa0f240-6489-5d0c-a907-083d633c124a", environment={})
        self.pane("w1:names", session_id="7f89b939-c570-5fd3-9802-e62dfb8aeea6", environment={
            "SHOWRUNNER_UNIT": "style", "SHOWRUNNER_UNIT_ID": "names-unit"})
        _ = self.session_record("ffa0f240-6489-5d0c-a907-083d633c124a", "style")
        _ = self.session_record("7f89b939-c570-5fd3-9802-e62dfb8aeea6", "names-unit")
        _ = self.publish_workers()
        parent = next(row for row in staff.find(kind="showrunner") if row.name == "parent")
        child = next(row for row in staff.find(kind="showrunner") if row.name == "style")
        unit = unit_membership("style", "names-unit")
        _ = staff.set_doc(parent.id, staff.WrappedDoc("natedev", parent_doc))
        before = self.staff_file.read_bytes()
        members = cast(list[dict[str, object]], staff_sources.live()["members"])
        by_id = {entry["id"]: entry for entry in members}
        self.assertNotIn(parent.id, by_id)
        self.assertIn(child.id, by_id)
        self.assertIsNone(by_id[child.id]["parent"])
        self.assertEqual(by_id[unit.id]["parent"], child.id)
        self.assertEqual(staff.by_id(child.id), child)
        self.assertEqual(self.staff_file.read_bytes(), before)


    def test_serverless_delegate_skips_its_roster_and_counts_once_in_worker_fill(self) -> None:
        production_membership("sdlc", "models-unit")
        doc = self.production("sdlc", "models-unit")
        owner = next(row for row in staff.find(kind="showrunner") if row.name == "sdlc")
        _ = staff.set_doc(owner.id, staff.LiveDoc("natedev", doc))
        unit = unit_membership("sdlc", "models-unit")
        _ = self.session_record("session-models", "models-unit")
        self.pane("w1:models", session_id="session-models", environment={"STAFF_ID": unit.id})
        delegate_root = self.source_root / "delegate"
        with (mock.patch.object(unit_runs, "FORMER_RUN_ROOT", delegate_root),
              mock.patch.dict(os.environ, {"ROSTER_RUN_ROOT": str(delegate_root)})):
            run = self.worker_run("sdlc", "models-unit", "session-models")
            (run / "mesh_server.json").unlink()
            roster = self.roster_worker(run, "delegate", "", "thread-paused", status="paused")
            entry = cast(dict[str, dict[str, object]], json.loads(roster.read_text()))
            missing_role = {"delegate": {key: value for key, value in entry["delegate"].items() if key != "role"}}
            before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
            for contents in (roster.read_text(), json.dumps(missing_role), "{broken roster"):
                with self.subTest(roster=contents):
                    write_text_atomic(roster, contents)
                    with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                        code = worker_fill.fill()
                    self.assertEqual((code, output.getvalue(), errors.getvalue()),
                                     (0, "skipped 1 not current\nunchanged\n", ""))
                    self.assertEqual(staff.find(kind="worker"), [])
                    self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_serverless_delegate_held_by_ledger_adds_no_worker_and_counts_once(self) -> None:
        production_membership("sdlc", "models-unit")
        doc = self.production("sdlc", "models-unit")
        owner = next(row for row in staff.find(kind="showrunner") if row.name == "sdlc")
        _ = staff.set_doc(owner.id, staff.LiveDoc("natedev", doc))
        unit = unit_membership("sdlc", "models-unit")
        _ = self.session_record("session-models", "models-unit")
        self.pane("w1:models", session_id="session-models", environment={"STAFF_ID": unit.id})
        delegate_root = self.source_root / "delegate"
        with (mock.patch.object(unit_runs, "FORMER_RUN_ROOT", delegate_root),
              mock.patch.dict(os.environ, {"ROSTER_RUN_ROOT": str(delegate_root)})):
            run = self.worker_run("sdlc", "models-unit", "session-models")
            _ = self.ledger_worker(run, "sdlc", "models-unit", "test", "test", "thread-held")
            _ = self.roster_worker(run, "test", "test", "thread-held", status="paused")
            (run / "mesh_server.json").unlink()
            before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
            with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                code = worker_fill.fill()
            self.assertEqual((code, output.getvalue(), errors.getvalue()),
                             (0, "skipped 1 not current\nunchanged\n", ""))
            self.assertEqual(staff.find(kind="worker"), [])
            self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_serverless_unit_keeps_uncertain_worker_evidence_out_of_the_skip_count(self) -> None:
        _ = self.production("sdlc", "models-unit")
        production_membership("sdlc", "models-unit")
        run = self.worker_run("sdlc", "models-unit", "gone-director")
        (run / "mesh_server.json").unlink()
        self.write_json(run / "mesh_roster.json", {
            "test": {"role": "test", "status": "paused", "family": "codex", "launcher_pid": 0},
        })
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            code = worker_fill.fill()
        self.assertEqual(code, 1, output.getvalue() + errors.getvalue())
        self.assertIn(str(run), errors.getvalue())
        self.assertIn("source unavailable", errors.getvalue())
        self.assertNotIn("skipped", output.getvalue())
        self.assertEqual(staff.find(kind="worker"), [])

    def test_paused_roster_workers_survive_a_director_restart_or_a_missing_messaging_socket(self) -> None:
        _ = self.production("sdlc", "models-unit")
        production_membership("sdlc", "models-unit")
        unit = unit_membership("sdlc", "models-unit")
        _ = self.session_record("cdc4420c-ae5d-5a9a-8135-75e8c0875088", "sdlc")
        registry = self.session_record(unit.id, "models-unit")
        self.assertEqual(json.loads(registry.read_text())["messagingSocketPath"], "")
        self.pane("w1:models", session_id=unit.id, environment={"STAFF_ID": unit.id})
        run = self.worker_run("sdlc", "models-unit", unit.id)
        self.write_json(run / "mesh_server.json", {"pid": 0, "codex_home": str(self.root / "codex")})
        roster = self.roster_worker(run, "test", "test", "71280e34-5ba9-5e39-b979-05acf27f3f6a", status="paused")
        with mock.patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": str(self.root / "shutdown")}):
            for director in (unit.id, fixture_runtime_id("restarted-director")):
                with self.subTest(director=director):
                    _ = staff.update_current_session(unit.id, staff.native_id(director))
                    _ = self.session_record(director, "models-unit")
                    write_text_atomic(run / "director", unit.id + "\n")
                    with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as reads:
                        _ = self.publish_workers()
                    self.assertEqual(reads.call_count, 1)
                    rows = staff.find(kind="worker", parent=unit.id)
                    self.assertEqual([row.name for row in rows], ["test"])
                    with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as reads:
                        value = staff_sources.live()
                    self.assertEqual(reads.call_count, 1)
                    members = cast(list[dict[str, object]], value["members"])
                    worker = next(member for member in members if member["id"] == rows[0].id)
                    self.assertEqual(worker["parent"], unit.id)
                    self.assertEqual(worker["identity"], {"codex": "71280e34-5ba9-5e39-b979-05acf27f3f6a"})
                    self.assertEqual(worker["role"], "test")
                    self.assertIn(str(roster), cast(list[str], value["inputs"]))

    def test_legacy_claude_workers_join_board_slots_by_registered_mesh_name(self) -> None:
        _ = self.production("sdlc", "models-unit")
        _ = self.session_record("cdc4420c-ae5d-5a9a-8135-75e8c0875088", "sdlc")
        _ = self.session_record("bae0b3d7-3d25-5a1f-9630-b7991a2f9721", "models-unit")
        self.pane("w1:models", session_id="bae0b3d7-3d25-5a1f-9630-b7991a2f9721", environment={
            "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit"})
        for index, (prefix, kind, role) in enumerate((
            ("hana_catalyst", "impl", "test"), ("3D.Project-explicit", "fix", "fix"),
            ("unassigned", "impl", ""),
        )):
            with self.subTest(prefix=prefix, kind=kind, role=role):
                run = self.worker_run("sdlc", f"legacy-{index}", "bae0b3d7-3d25-5a1f-9630-b7991a2f9721")
                session_id = fixture_runtime_id(f"legacy-worker:{index}")
                _ = self.session_record(session_id, "Claude worker")
                name = self.legacy_claude_worker(run, session_id, prefix=prefix, kind=kind, role=role)
                self.assertEqual(list(run.glob("impl_worker_*.json")), [])
                _, problems = self.publish_workers()
                row = next((row for row in staff.find(kind="worker") if row.name == name), None)
                with redirect_stderr(io.StringIO()):
                    members = cast(list[dict[str, object]], staff_sources.live()["members"])
                if role:
                    self.assertIsNotNone(row, problems)
                    assert row is not None
                    member = next(member for member in members if member["id"] == row.id)
                    self.assertEqual(member["name"], name)
                    self.assertEqual(member["role"], role)
                    self.assertEqual(member["identity"], {"claude": session_id})
                else:
                    self.assertIsNone(row)
                    self.assertIn(str(run), problems)
                    self.assertFalse(any(member["name"] == name for member in members))

    def test_live_inputs_keep_rejected_production_documents_and_read_them_after_correction(self) -> None:
        _ = self.production("sdlc", "models-unit")
        _ = self.session_record("cdc4420c-ae5d-5a9a-8135-75e8c0875088", "sdlc")
        rejected = self.production("recovering", "later-unit")
        _ = self.session_record("8e2a7bad-d715-5c2d-9ec5-2a8bf9a81289", "recovering")
        _ = self.publish_workers()
        owner = staff.by_path("recovering")
        assert owner is not None
        _ = staff.set_doc(owner.id, staff.LiveDoc("natedev", rejected))
        original = rejected.read_bytes()
        _ = rejected.write_bytes(b"\xff invalid production\n")
        with redirect_stderr(io.StringIO()) as problems:
            value = staff_sources.live()
        self.assertIn("production doc could not be read", problems.getvalue())
        self.assertIn(str(rejected), cast(list[str], value["inputs"]))
        self.assertNotIn("recovering", [member["name"] for member in cast(list[dict[str, object]], value["members"])])
        _ = rejected.write_bytes(original)
        with redirect_stderr(io.StringIO()) as problems:
            recovered = staff_sources.live()
        self.assertNotIn(str(rejected), problems.getvalue())
        self.assertIn(str(rejected), cast(list[str], recovered["inputs"]))
        members = cast(list[dict[str, object]], recovered["members"])
        self.assertEqual(next(member["identity"] for member in members if member["name"] == "recovering"),
                         {"claude": "8e2a7bad-d715-5c2d-9ec5-2a8bf9a81289"})

    def test_live_inputs_include_present_and_missing_shared_session_configuration(self) -> None:
        _ = self.production("sdlc", "models-unit")
        _ = self.session_record("cdc4420c-ae5d-5a9a-8135-75e8c0875088", "sdlc")
        _ = self.publish_workers()
        config = Path(os.environ["XDG_CONFIG_HOME"]) / "herdr/shared-session"
        for present in (False, True):
            with self.subTest(present=present):
                if present:
                    config.parent.mkdir(parents=True, exist_ok=True)
                    write_text_atomic(config, "agents\n")
                with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as reads:
                    value = staff_sources.live()
                self.assertEqual(reads.call_count, 1)
                inputs = cast(list[str], value["inputs"])
                self.assertIn(str(config), inputs)
                self.assert_no_system_inputs(inputs)

    def test_finished_and_adopted_rosters_cannot_override_the_live_ledger_worker(self) -> None:
        _ = self.production("sdlc", "models-unit")
        _ = self.session_record("cdc4420c-ae5d-5a9a-8135-75e8c0875088", "sdlc")
        _ = self.session_record("bae0b3d7-3d25-5a1f-9630-b7991a2f9721", "models-unit")
        self.pane("w1:models", session_id="bae0b3d7-3d25-5a1f-9630-b7991a2f9721", environment={
            "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit"})
        run = self.worker_run("sdlc", "models-unit", "bae0b3d7-3d25-5a1f-9630-b7991a2f9721")
        ledger = self.ledger_worker(run, "sdlc", "models-unit", "review", "review", "e63a4770-62e8-5270-91f0-a163996415b9")
        _ = self.roster_worker(run, "review", "review", "e63a4770-62e8-5270-91f0-a163996415b9")
        for number, status in enumerate(("done", "adopted"), start=1):
            old_run = self.worker_run("sdlc", f"history-{status}", "bae0b3d7-3d25-5a1f-9630-b7991a2f9721")
            _ = self.roster_worker(old_run, "review", "review", f"thread-{status}", status=status)
            _ = self.roster_worker(old_run, f"fix{number}", "fix", f"thread-only-{status}", status=status)
        gone = self.worker_run("sdlc", "history-gone", "3790170d-f8c8-5dbd-ba9b-38eb5179177f")
        self.write_json(gone / "progress_history_state.json", {
            "production": "sdlc", "unit": "models-unit",
            "main_agent": {"family": "claude", "session_id": "3790170d-f8c8-5dbd-ba9b-38eb5179177f"},
        })
        self.write_json(gone / "mesh_server.json", {"pid": 0, "codex_home": str(self.root / "codex")})
        _ = self.roster_worker(gone, "fix3", "fix", "e4481fcb-8cb1-560b-b68f-82663434e61a")
        dead_roster = cast(dict[str, dict[str, object]], json.loads((gone / "mesh_roster.json").read_text()))
        dead_roster["fix3"]["launcher_pid"] = 0
        self.write_json(gone / "mesh_roster.json", dead_roster)
        _, errors = self.publish_workers()
        self.assertNotIn("conflicting worker identities", errors)
        workers = staff.find(kind="worker")
        self.assertEqual([row.name for row in workers], ["review"])
        value = staff_sources.live()
        members = cast(list[dict[str, object]], value["members"])
        reviewer = next(member for member in members if member["id"] == workers[0].id)
        self.assertEqual(reviewer["identity"], {"codex": "e63a4770-62e8-5270-91f0-a163996415b9"})
        self.assertIn(str(ledger), cast(list[str], value["inputs"]))

    def test_adhoc_ledger_of_a_dot_named_worktree_takes_its_holder_run_without_a_name_error(self) -> None:
        _ = self.production("sdlc", "models-unit")
        _ = self.session_record("cdc4420c-ae5d-5a9a-8135-75e8c0875088", "sdlc")
        _ = self.session_record("bae0b3d7-3d25-5a1f-9630-b7991a2f9721", "models-unit")
        self.pane("w1:models", session_id="bae0b3d7-3d25-5a1f-9630-b7991a2f9721", environment={
            "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit"})
        run = self.worker_run("sdlc", "models-unit", "bae0b3d7-3d25-5a1f-9630-b7991a2f9721")
        # A worktree named `.claude` gives the adhoc ledger stem `.claude-<digest>`.
        _ = self.ledger_worker(run, "adhoc", ".claude-3314ec0bf589", "review", "review", "0783462b-323d-5d81-b61c-ddee74b529d6")
        _, errors = self.publish_workers()
        self.assertNotIn("name must contain", errors)
        with redirect_stderr(io.StringIO()) as live_errors:
            members = cast(list[dict[str, object]], staff_sources.live()["members"])
        self.assertNotIn("name must contain", live_errors.getvalue())
        unit = next(member for member in members if member["kind"] == "unit")
        reviewer = next(member for member in members if member["kind"] == "worker")
        self.assertEqual((reviewer["name"], reviewer["parent"]), ("review", unit["id"]))
        self.assertEqual(reviewer["identity"], {"codex": "0783462b-323d-5d81-b61c-ddee74b529d6"})

    def test_live_owner_ignores_a_newer_launch_whose_session_has_ended(self) -> None:
        _ = self.production("sdlc", "models-unit")
        _ = self.publish_workers()
        unit = staff.by_path("sdlc/models")
        assert unit is not None
        _ = self.session_record("cdc4420c-ae5d-5a9a-8135-75e8c0875088", "sdlc")
        _ = self.session_record(unit.id, "models-unit")
        self.pane("w1:running", session_id=unit.id, environment={"STAFF_ID": unit.id},
                  written_at="2026-10-30T09:00:00+00:00")
        self.pane("w1:ended", session_id="3790170d-f8c8-5dbd-ba9b-38eb5179177f", environment={"STAFF_ID": unit.id},
                  written_at="2026-10-31T09:00:00+00:00")
        members = cast(list[dict[str, object]], staff_sources.live()["members"])
        member = next(member for member in members if member["id"] == unit.id)
        self.assertEqual(member["identity"], {"claude": unit.id})

    def test_live_workers_use_their_producers_without_invented_unit_ledger_entries(self) -> None:
        _ = fixture_member("showrunner", name="sdlc", doc=staff.LiveDoc(
            "natedev", self.source_root / "sdlc-production.md"))
        _ = self.production("sdlc", "models-unit")
        _ = self.session_record("cdc4420c-ae5d-5a9a-8135-75e8c0875088", "sdlc")
        _ = self.session_record("bae0b3d7-3d25-5a1f-9630-b7991a2f9721", "models-unit")
        self.pane("w1:models", session_id="bae0b3d7-3d25-5a1f-9630-b7991a2f9721", environment={
            "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit"})
        run = self.worker_run("sdlc", "models-unit", "bae0b3d7-3d25-5a1f-9630-b7991a2f9721")
        roster = self.roster_worker(run, "impl", "fix", "936663e1-4213-5af9-b170-de47f5a9e149")
        astra = self.astra_worker(run, "sdlc-handler-astra-reviewer", "1e49a002-64a8-51fb-8291-2828649967bd", "cdc4420c-ae5d-5a9a-8135-75e8c0875088")
        design = self.worker_design("sdlc", "sdlc-handler-astra-designer", "3d1d0e20-5129-55bd-9516-9b9e8d633777")
        self.assertEqual(list(Path(os.environ["UNIT_WORKERS_STATE"]).rglob("*.json")), [])
        _ = self.publish_workers()
        members = cast(list[dict[str, object]], staff_sources.live()["members"])
        workers = {cast(str, member["name"]): member for member in members if member["kind"] == "worker"}
        self.assertEqual(set(workers), {"impl", "sdlc-handler-astra-reviewer", "sdlc-handler-astra-designer"})
        self.assertEqual(workers["impl"]["role"], "fix")
        for name, thread in (("impl", "936663e1-4213-5af9-b170-de47f5a9e149"),
                             ("sdlc-handler-astra-reviewer", "1e49a002-64a8-51fb-8291-2828649967bd"),
                             ("sdlc-handler-astra-designer", "3d1d0e20-5129-55bd-9516-9b9e8d633777")):
            self.assertEqual(workers[name]["identity"], {"codex": thread})
        root = next(member for member in members if member["name"] == "sdlc")
        self.assertEqual(workers["sdlc-handler-astra-reviewer"]["parent"], root["id"])
        inputs = cast(list[str], staff_sources.live()["inputs"])
        for path in (roster, astra, design):
            self.assertIn(str(path), inputs)

    def test_unreadable_astra_owner_does_not_publish_a_permanent_unit_owned_duplicate(self) -> None:
        _ = fixture_member("showrunner", name="sdlc", doc=staff.LiveDoc(
            "natedev", self.source_root / "sdlc-production.md"))
        _ = self.production("sdlc", "models-unit")
        _ = self.session_record("cdc4420c-ae5d-5a9a-8135-75e8c0875088", "sdlc")
        run = self.worker_run("sdlc", "models-unit", "bae0b3d7-3d25-5a1f-9630-b7991a2f9721")
        name = "sdlc-handler-astra-reviewer"
        _ = self.ledger_worker(run, "sdlc", "models-unit", name, "review", "1e49a002-64a8-51fb-8291-2828649967bd")
        path = self.astra_worker(run, name, "1e49a002-64a8-51fb-8291-2828649967bd", "cdc4420c-ae5d-5a9a-8135-75e8c0875088")
        original = path.read_text()
        write_text_atomic(path, "{broken")
        _, errors = self.publish_workers()
        self.assertIn(str(path), errors)
        self.assertEqual(staff.find(kind="worker"), [])
        write_text_atomic(path, original)
        _ = self.publish_workers()
        root = staff.by_path("sdlc")
        assert root is not None
        rows = staff.find(kind="worker")
        self.assertEqual([(row.name, row.parent) for row in rows], [(name, root.id)])
        before = self.staff_file.read_bytes()
        _ = self.publish_workers()
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_live_inputs_include_the_remote_owner_record_and_exclude_process_and_system_reads(self) -> None:
        owner = self.tree()[0]
        for machine in ("natedev", "mac"):
            _ = staff.set_doc(owner.id, staff.LiveDoc(
                machine, self.root / f"{machine}-sources" / "sdlc-production.md"))
            with self.subTest(machine=machine), self.machine(machine):
                if machine == "mac":
                    self.assertNotIn("STAFF_FILE", os.environ)
                _ = self.production("sdlc", "models-unit")
                _ = self.session_record("cdc4420c-ae5d-5a9a-8135-75e8c0875088", "sdlc")
                remote_reads = self.ssh_log.read_text().count("production/staff.py") if machine == "mac" else 0
                with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as reads:
                    value = staff_sources.live()
                self.assertEqual(reads.call_count, 1)
                if machine == "mac":
                    self.assertEqual(self.ssh_log.read_text().count("production/staff.py") - remote_reads, 1)
                inputs = cast(list[str], value["inputs"])
                self.assertIn(str(self.staff_file), inputs)
                self.assert_no_system_inputs(inputs)

    def publish_workers(self) -> tuple[str, str]:
        """Publish each fixture backend identity through the admission writer."""
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            record = staff.snapshot()
            evidence = staff_sources.observe_workers(record, session_staff.member_sources(),
                                                    unavailable=staff_sources.UnavailableWorkerPolicy.LIST)
            for diagnostic in evidence.diagnostics:
                print(diagnostic, file=sys.stderr)
            for observation in evidence.workers:
                if isinstance(observation.identity, staff_sources.BackendUnavailable):
                    continue
                identity = next(iter(observation.identity.values()))
                _ = staff.ensure_worker(observation.name, observation.owner.match.id, runtime_id=identity)
        return output.getvalue(), errors.getvalue()

    def assert_no_system_inputs(self, inputs: list[str]) -> None:
        # The test's own temp root may sit under /dev/shm (the gate sets TMPDIR there).
        root = f"{self.root}/"
        for path in inputs:
            if path.startswith(root):
                continue
            self.assertFalse(path.startswith(("/proc/", "/dev/", "/usr/share/zoneinfo/")), path)


    def test_empty_pending_files_have_no_publication_evidence_or_diagnostics(self) -> None:
        from scripts.agents import worker_lifecycle

        run = self.root / "pending-run"
        run.mkdir()
        _ = (run / "empty.pending.json").write_bytes(b"")
        _ = (run / "object.pending.json").write_text("{}")
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in run.iterdir()}
        with mock.patch.object(worker_lifecycle, "locked_state", side_effect=AssertionError("reader took a lock")):
            evidence = staff_sources.pending_publication_evidence(run)
        self.assertEqual(evidence.workers, ())
        self.assertEqual(evidence.diagnostics, ())
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in run.iterdir()}, before)

    def test_pending_evidence_reads_every_damaged_file_without_recovery_or_locks(self) -> None:
        from scripts.agents import worker_lifecycle

        run = self.root / "pending-run"
        run.mkdir()
        empty = run / "z.pending.json"
        invalid = run / "a.pending.json"
        valid = run / "zz.pending.json"
        _ = empty.write_text("")
        self.write_json(invalid, {"messages": []})
        native = fixture_runtime_id("pending-native")
        parent = fixture_runtime_id("pending-parent")
        self.write_json(valid, {"thread_id": "", "messages": [],
                                "unpublished_thread": {"id": native, "parent": parent}})
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in run.iterdir()}
        with mock.patch.object(worker_lifecycle, "locked_state", side_effect=AssertionError("reader took a lock")):
            first = staff_sources.pending_publication_evidence(run)
            second = staff_sources.pending_publication_evidence(run)
        self.assertEqual(first, second)
        self.assertEqual(first.diagnostics, (f"{invalid}: worker state unreadable: invalid queue fields",))
        self.assertEqual([(entry.inbox, entry.identity, entry.parent) for entry in first.workers],
                         [(valid, native, parent)])
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in run.iterdir()}, before)

    def test_codex_scan_continues_after_unparsable_process_arguments(self) -> None:
        from scripts.message import process_table

        doc = self.fill_production("scan-demo")
        owner = staff.by_path("scan-demo")
        assert owner is not None
        _ = staff.set_doc(owner.id, staff.LiveDoc("natedev", doc))
        table = {
            42401: process_table.Process(42401, 1, "codex", 'codex --config "unfinished'),
            42402: process_table.Process(42402, 1, "codex", "codex --model gpt-6"),
        }
        with (mock.patch.object(process_table, "snapshot", return_value=table),
              mock.patch.object(herdr, "process_environment", return_value={"CODEX_THREAD_ID": owner.current_session}) as environment,
              redirect_stderr(io.StringIO()) as errors):
            document = staff_sources.live()
        environment.assert_called_once_with(42402)
        members = cast(list[dict[str, object]], document["members"])
        found = next(member for member in members if member["id"] == owner.id)
        self.assertEqual(found["identity"], {"codex": owner.current_session})
        self.assertFalse(document["complete"])
        self.assertEqual(errors.getvalue().splitlines(), [
            "staff sources: Codex process scan: pid 42401: No closing quotation"])

    def test_missing_codex_thread_reports_one_codex_problem(self) -> None:
        from scripts.message import process_table

        _ = self.fill_production("pane-demo", "models-unit")
        unit = staff.by_path("pane-demo/models")
        assert unit is not None
        launch = pane_record.launch("test:pane", unit.id, str(self.root), {}, [])
        _ = staff.update_current_session(unit.current_session, staff.native_id(fixture_runtime_id("pane-unit-current")))
        table = {42401: process_table.Process(42401, 1, "codex", "codex resume")}
        record = staff.snapshot()
        sources = MemberSources({}, (launch,))
        with (mock.patch.object(process_table, "snapshot", return_value=table),
              mock.patch.object(herdr, "process_environment", return_value={"HERDR_PANE_ID": "test:pane"})):
            evidence = staff_sources.observe_workers(
                record, sources, unavailable=staff_sources.UnavailableWorkerPolicy.LIST)
            with self.assertRaises(staff.StaffUnreadable) as refused:
                _ = staff_sources.observe_workers(record, sources)
        expected = "staff sources: Codex process 42401: native thread identity is unavailable"
        self.assertEqual(evidence.diagnostics, (expected,))
        self.assertEqual(str(refused.exception), expected)
        self.assertNotIn("Claude", str(refused.exception))

    def test_codex_without_a_thread_in_an_unowned_pane_reports_a_diagnostic_without_refusal(self) -> None:
        from scripts.message import process_table

        _ = self.fill_production("other-pane-demo", "models-unit")
        unit = staff.by_path("other-pane-demo/models")
        assert unit is not None
        held = pane_record.launch("other:pane", unit.id, str(self.root), {}, [])
        unowned = pane_record.launch("test:pane", fixture_runtime_id("unowned-pane"), str(self.root), {}, [])
        table = {42401: process_table.Process(42401, 1, "codex", "codex resume")}
        for launches in ((held,), (held, unowned)):
            with (self.subTest(recorded=unowned in launches),
                  mock.patch.object(process_table, "snapshot", return_value=table),
                  mock.patch.object(herdr, "process_environment", return_value={"HERDR_PANE_ID": "test:pane"})):
                evidence = staff_sources.observe_workers(staff.snapshot(), MemberSources({}, launches))
            self.assertEqual(evidence.diagnostics, (
                "staff sources: Codex process 42401: pane holds no staff member; not staff",))

    def test_missing_codex_thread_in_a_pane_with_staff_unit_labels_keeps_the_scan_incomplete(self) -> None:
        from scripts.message import process_table

        _ = self.fill_production("label-pane-demo", "models-unit")
        launch = pane_record.launch("test:pane", "", str(self.root), {
            "SHOWRUNNER_UNIT": "label-pane-demo", "SHOWRUNNER_UNIT_ID": "models-unit"}, [])
        table = {42401: process_table.Process(42401, 1, "codex", "codex resume")}
        with (mock.patch.object(process_table, "snapshot", return_value=table),
              mock.patch.object(herdr, "process_environment", return_value={"HERDR_PANE_ID": "test:pane"})):
            with self.assertRaisesRegex(staff.StaffUnreadable, "native thread identity is unavailable"):
                _ = staff_sources.observe_workers(staff.snapshot(), MemberSources({}, (launch,)))

    def test_unparsable_claude_settings_do_not_make_the_codex_scan_incomplete(self) -> None:
        from scripts.message import process_table

        doc = self.fill_production("claude-scan-demo")
        owner = next(row for row in staff.find(kind="showrunner") if row.name == "claude-scan-demo")
        _ = staff.set_doc(owner.id, staff.LiveDoc("natedev", doc))
        table = {
            42401: process_table.Process(42401, 1, "python3",
                                        'relaunch.py run -- claude --resume session --settings {"name":"unfinished}'),
            42402: process_table.Process(42402, 1, "claude",
                                        'claude --resume session --settings {"name":"unfinished}'),
        }
        with (mock.patch.object(process_table, "snapshot", return_value=table),
              mock.patch.object(herdr, "process_environment") as environment,
              redirect_stderr(io.StringIO()) as errors):
            document = staff_sources.live()
        environment.assert_not_called()
        self.assertTrue(document["complete"])
        self.assertEqual(errors.getvalue(), "")

    def test_codex_without_a_thread_or_pane_reports_a_diagnostic_without_refusal(self) -> None:
        from scripts.message import process_table

        table = {42401: process_table.Process(42401, 1, "codex", "/local/bin/codex resume")}
        with (mock.patch.object(process_table, "snapshot", return_value=table),
              mock.patch.object(herdr, "process_environment", return_value={})):
            evidence = staff_sources.observe_workers(staff.StaffSnapshot(()), MemberSources({}, ()))
        self.assertEqual(evidence.diagnostics, (
            "staff sources: Codex process 42401: not in a herdr pane; not staff",))

    def test_unparsable_node_codex_arguments_keep_the_scan_incomplete(self) -> None:
        from scripts.message import process_table

        table = {42401: process_table.Process(
            42401, 1, "node", 'node /modules/@openai/codex/bin/codex.js --config "unfinished')}
        with (mock.patch.object(process_table, "snapshot", return_value=table),
              mock.patch.object(herdr, "process_environment") as environment):
            with self.assertRaises(staff.StaffUnreadable) as refused:
                _ = staff_sources.observe_workers(staff.StaffSnapshot(()), MemberSources({}, ()))
        environment.assert_not_called()
        self.assertEqual(str(refused.exception),
                         "staff sources: Codex process scan: pid 42401: No closing quotation")

    def test_failed_codex_process_snapshot_does_not_report_a_claude_registry_failure(self) -> None:
        from scripts.message import process_table

        with mock.patch.object(process_table, "snapshot", side_effect=OSError("process table unavailable")):
            with self.assertRaisesRegex(staff.StaffUnreadable, "Codex processes: process table unavailable") as refused:
                _ = staff_sources.observe_workers(staff.StaffSnapshot(()), MemberSources({}, ()))
        self.assertNotIn("Claude", str(refused.exception))

    def test_live_staff_includes_codex_showrunner_and_unit_by_current_thread_without_role_inference(self) -> None:
        from scripts.message import process_table

        _ = self.fill_production("codex-demo", "models-unit")
        owner = staff.by_path("codex-demo")
        unit = staff.by_path("codex-demo/models")
        assert owner is not None and unit is not None
        owner_current = staff.native_id(fixture_runtime_id("codex-showrunner-current"))
        unit_current = staff.native_id(fixture_runtime_id("codex-unit-current"))
        _ = staff.update_current_session(owner.current_session, owner_current)
        _ = staff.update_current_session(unit.current_session, unit_current)
        table = {
            42401: process_table.Process(42401, 1, "codex", "codex --model gpt-6"),
            42402: process_table.Process(42402, 1, "codex", "codex --config model=gpt-6"),
            42403: process_table.Process(42403, 1, "codex", "codex --model gpt-6 app-server"),
        }
        environments = {42401: {"CODEX_THREAD_ID": owner_current},
                        42402: {"CODEX_THREAD_ID": unit_current},
                        42403: {"CODEX_THREAD_ID": fixture_runtime_id("app-server-host-only")}}
        before = self.staff_file.read_bytes()
        with (mock.patch.object(process_table, "snapshot", return_value=table),
              mock.patch.object(herdr, "process_environment", side_effect=environments.__getitem__),
              redirect_stderr(io.StringIO()) as errors):
            document = staff_sources.live()
        members = cast(list[dict[str, object]], document["members"])
        by_id = {member["id"]: member for member in members}
        self.assertEqual(by_id[owner.id]["identity"], {"codex": owner_current})
        self.assertEqual(by_id[unit.id]["identity"], {"codex": unit_current})
        self.assertEqual((by_id[owner.id]["kind"], by_id[unit.id]["kind"]), ("showrunner", "unit"))
        self.assertEqual(by_id[unit.id]["parent"], owner.id)
        self.assertNotIn(environments[42403]["CODEX_THREAD_ID"], errors.getvalue())
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_live_staff_command_keeps_worker_identity_and_all_input_paths(self) -> None:
        doc = self.production("sdlc", "models-unit")
        self.pane("w1:runner", session_id="cdc4420c-ae5d-5a9a-8135-75e8c0875088", environment={})
        self.pane("w1:models", session_id="bae0b3d7-3d25-5a1f-9630-b7991a2f9721", environment={
            "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        registry = [self.session_record("cdc4420c-ae5d-5a9a-8135-75e8c0875088", "sdlc"),
                    self.session_record("bae0b3d7-3d25-5a1f-9630-b7991a2f9721", "models-unit"),
                    self.session_record("0fc60dc3-b54e-55fd-97eb-84c6a4a4e3bf", "test")]
        run = self.worker_run("sdlc", "models-unit", "bae0b3d7-3d25-5a1f-9630-b7991a2f9721")
        ledger = self.ledger_worker(run, "sdlc", "models-unit", "impl", "impl", "3e104a36-0fc6-5eb7-b30b-7f3aa8ecb79b")
        _ = self.ledger_worker(run, "sdlc", "models-unit", "test", "test", "0fc60dc3-b54e-55fd-97eb-84c6a4a4e3bf", family="claude")
        astra = self.astra_worker(run, "sdlc-handler-astra-reviewer", "1e49a002-64a8-51fb-8291-2828649967bd", "cdc4420c-ae5d-5a9a-8135-75e8c0875088")
        _ = self.publish_workers()
        owner = staff.by_path("sdlc")
        assert owner is not None
        _ = staff.set_doc(owner.id, staff.LiveDoc("natedev", doc))
        unit = staff.by_path("sdlc/models")
        assert unit is not None
        folder = self.source_root / "worktrees" / "models-unit"
        _ = staff.set_worktree(unit.id, staff.PresentWorktree("natedev", folder), expected=unit.worktree)
        (folder / ".git").unlink()
        _ = self.fixture_git(folder, "init", "-b", "models")
        _ = self.fixture_git(folder, "config", "user.email", "fixture@example.invalid")
        _ = self.fixture_git(folder, "config", "user.name", "Fixture")
        _ = self.fixture_git(folder, "commit", "--allow-empty", "-m", "fixture")
        write_text_atomic(folder / ".unit.toml", 'plan = "active"\nresponsibilities = []\n')
        before = self.staff_file.read_bytes()
        with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as reads, \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(staff.main(["show", "--json", "--live"]), 0)
        self.assertEqual(reads.call_count, 1)
        document = cast(dict[str, object], json.loads(output.getvalue()))
        self.assertEqual(set(document), {"schema", "inputs", "members", "complete", "unregistered"})
        self.assertEqual(document["complete"], True)
        self.assertEqual(document["unregistered"], [])
        self.assertEqual(document["schema"], 3)
        members = cast(list[dict[str, object]], document["members"])
        by_name = {cast(str, member["name"]): member for member in members}
        by_identity = {tuple(cast(dict[str, str], member["identity"]).items()): member for member in members}
        root = by_name["sdlc"]
        unit = by_name["models"]
        self.assertEqual(root["identity"], {"claude": "cdc4420c-ae5d-5a9a-8135-75e8c0875088"})
        self.assertEqual(unit["parent"], root["id"])
        self.assertEqual(unit["identity"], {"claude": "bae0b3d7-3d25-5a1f-9630-b7991a2f9721"})
        for name, identity in (("impl", {"codex": "3e104a36-0fc6-5eb7-b30b-7f3aa8ecb79b"}), ("test", {"claude": "0fc60dc3-b54e-55fd-97eb-84c6a4a4e3bf"})):
            self.assertEqual(by_name[name]["parent"], unit["id"])
            self.assertEqual(by_name[name]["identity"], identity)
        designer = by_identity[(("codex", "1e49a002-64a8-51fb-8291-2828649967bd"),)]
        self.assertEqual(designer["parent"], root["id"])
        self.assertEqual(designer["role"], "astra-reviewer")
        ids = {member["id"] for member in members}
        self.assertEqual(len(ids), len(members))
        for member in members:
            self.assertEqual(set(member), {"id", "kind", "name", "parent", "current_session", "role", "identity"})
            self.assertIn(member["role"], {"showrunner", "unit", "impl", "test", "review", "fix", "astra-designer", "astra-reviewer"})
            self.assertTrue(member["parent"] in ids or member is root)
            self.assertEqual(member["parent"] is None, member is root)
            self.assertEqual(len(cast(dict[str, str], member["identity"])), 1)
        inputs = cast(list[str], document["inputs"])
        self.assertTrue(all(Path(path).is_absolute() for path in inputs))
        self.assertEqual(len(inputs), len(set(inputs)))
        for path in (self.staff_file, doc, ledger, astra, run / "mesh_roster.json", *registry,
                     Path(os.environ["NOTIFIER_STATE_DIR"]) / "showrunner-sdlc" / "conf",
                     pane_record.record_path(herdr.shared_session())):
            self.assertIn(str(path), inputs)
        self.assertEqual(self.staff_file.read_bytes(), before)
        ledger_value = cast(dict[str, object], json.loads(ledger.read_text()))
        workers = cast(dict[str, dict[str, object]], ledger_value["workers"])
        cast(dict[str, object], workers["impl"]["backend"])["thread_id"] = "4362efed-e9f3-50e0-b02e-8c33b50288b9"
        self.write_json(ledger, ledger_value)
        replacement_id = staff.native_id("4362efed-e9f3-50e0-b02e-8c33b50288b9")
        _ = staff.update_current_session(staff.StaffId(cast(str, by_name["impl"]["id"])), replacement_id)
        published = self.staff_file.read_bytes()
        replacement = cast(dict[str, object], json.loads(self.cli("show", "--json", "--live").stdout))
        replacement_members = cast(list[dict[str, object]], replacement["members"])
        replaced = next(member for member in replacement_members if member["id"] == by_name["impl"]["id"])
        self.assertEqual(replaced["identity"], {"codex": replacement_id})
        self.assertEqual(replaced["current_session"], replacement_id)
        self.assertNotIn(replacement_id, {member["id"] for member in replacement_members})
        self.assertEqual({member["id"]: member["parent"] for member in replacement_members},
                         {member["id"]: member["parent"] for member in members})
        self.assertEqual(self.staff_file.read_bytes(), published)


    def test_persisted_member_reference_survives_two_clears_rename_and_name_reuse(self) -> None:
        parent, unit, _ = self.tree()
        _ = self.production("sdlc", "models-unit")
        launch = pane_record.read_strict("agents")["fixture:sdlc/models-unit"]
        pane_record.write("agents", launch)
        second = staff.StaffId(fixture_runtime_id("member-clear-B"))
        third = staff.StaffId(fixture_runtime_id("member-clear-C"))
        _ = staff.update_current_session(unit.current_session, second)
        current = staff.update_current_session(second, third)
        current = staff.rename(current.id, "renamed")
        replacement = fixture_member("unit", name="models", parent=parent.id,
                                     runtime_id=fixture_runtime_id("replacement-models"))
        path = pane_record.record_path("agents")
        selector = staff_sources.UnitMember("sdlc", "models-unit")
        for reference in (unit.id, third):
            with self.subTest(reference=reference):
                # The first case is the writer's retained A; the second models a
                # retained observation already carrying the live conversation C.
                if reference == third:
                    launch["session_id"] = reference
                    pane_record.save("agents", {launch["pane_id"]: launch})
                before = path.read_bytes(), path.stat().st_mtime_ns
                record = staff.snapshot()
                with mock.patch.object(staff, "snapshot", side_effect=AssertionError("owner reread")):
                    found = staff_sources.member(selector, record)
                self.assertEqual(found, staff_sources.MemberFound(current))
                self.assertNotEqual(current.id, replacement.id)
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_persisted_member_continues_past_a_cleared_launch_under_another_parent(self) -> None:
        parent, moved, _ = self.tree()
        _ = self.production("sdlc", "models-unit")
        other = fixture_member("showrunner", name="elsewhere")
        moved = staff.move(moved.id, other.id)
        moved = staff.update_current_session(moved.current_session, staff.StaffId(fixture_runtime_id("moved-B")))
        target = fixture_member("unit", name="actual", parent=parent.id)
        self.pane("newest-local", session_id=moved.id, written_at="2026-11-01T12:00:00Z",
                  environment={"SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit"})
        self.pane("older-transferred", session_id=target.id, written_at="2026-10-31T12:00:00Z",
                  environment={"SHOWRUNNER_UNIT": "former", "SHOWRUNNER_UNIT_ID": "models-unit"})
        selector = staff_sources.UnitMember("sdlc", "models-unit")
        self.assertEqual(staff_sources.member(selector, staff.snapshot()), staff_sources.MemberFound(target))
        # With two eligible identities, continuation must refuse ambiguity.
        competing = fixture_member("unit", name="competing", parent=parent.id)
        self.pane("competing", session_id=competing.id,
                  environment={"SHOWRUNNER_UNIT": "former", "SHOWRUNNER_UNIT_ID": "models-unit"})
        with self.assertRaisesRegex(staff.StaffUnreadable, "conflicting staff identities"):
            _ = staff_sources.member(selector, staff.snapshot())

    def test_persisted_cleared_references_preserve_kind_and_parent_safeguards(self) -> None:
        parent, unit, worker = self.tree()
        _ = self.production("sdlc", "models-unit")
        other = fixture_member("showrunner", name="elsewhere")
        moved = staff.move(unit.id, other.id)
        _ = staff.update_current_session(moved.current_session, staff.StaffId(fixture_runtime_id("wrong-parent-B")))
        _ = fixture_member("unit", name="models", parent=parent.id, runtime_id=fixture_runtime_id("same-name"))
        selector = staff_sources.UnitMember("sdlc", "models-unit")
        for reference in (fixture_runtime_id("missing"), parent.id, worker.id, unit.id):
            with self.subTest(reference=reference):
                self.pane("latest", session_id=reference, written_at="2026-11-01T12:00:00Z",
                          environment={"SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit"})
                self.assertEqual(staff_sources.member(selector, staff.snapshot()), staff_sources.NoSuchMember(selector))

    def test_runtime_grouping_rejects_fixed_and_superseded_sessions_after_two_clears(self) -> None:
        from scripts.production import session_staff

        _, unit, _ = self.tree()
        _ = self.production("sdlc", "models-unit")
        second = staff.StaffId(fixture_runtime_id("group-B"))
        third = staff.StaffId(fixture_runtime_id("group-C"))
        _ = staff.update_current_session(unit.current_session, second)
        _ = staff.update_current_session(second, third)
        record = staff.snapshot()
        sources = session_staff.member_sources()
        for runtime in (third, unit.id, second):
            with self.subTest(runtime=runtime):
                marked = session_staff.MarkedUnit("models-unit",
                    session_staff.LiveClaude("models", runtime, "", 123),
                    herdr.HerdrPlace("agents", "fixture:sdlc/models-unit"), staff_id=runtime)
                with mock.patch.object(staff, "snapshot", side_effect=AssertionError("owner reread")):
                    grouped = session_staff.group_marked_units((("sdlc", marked),), record=record, sources=sources)
                self.assertEqual(grouped.units.get("sdlc", {}), {"models-unit": marked} if runtime == third else {})
                self.assertEqual(grouped.faults, {})

    def test_current_session_parent_and_showrunner_follow_the_retained_launch_after_rename(self) -> None:
        import unit_lookup
        from scripts.production import session_staff

        parent, unit, _ = self.tree()
        doc = self.production("sdlc", "models-unit")
        parent = staff.set_doc(parent.id, staff.LiveDoc("natedev", doc))
        current = staff.update_current_session(unit.current_session, staff.StaffId(fixture_runtime_id("lookup-B")))
        current = staff.rename(current.id, "renamed")
        other = fixture_member("showrunner", name="other")
        _ = fixture_member("unit", name="models", parent=other.id)
        sources, record = session_staff.member_sources(), staff.snapshot()
        path = pane_record.record_path("agents")
        before = path.read_bytes(), path.stat().st_mtime_ns
        with mock.patch.object(staff, "snapshot", side_effect=AssertionError("owner reread")):
            self.assertEqual(unit_lookup.session_showrunner(current.current_session, record=record, sources=sources),
                             session_staff.MemberFound(parent))
            self.assertEqual(unit_lookup.unit_parent("models-unit", session_staff.LiveSession(current.current_session), record=record, sources=sources),
                             unit_lookup.UnitProduction("sdlc", str(doc)))
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)


    def test_member_result_is_explicit_and_uses_only_the_supplied_snapshot(self) -> None:
        showrunner, unit, _ = self.tree()
        _ = self.production("sdlc", "models-unit")
        record = staff.snapshot()
        missing = staff_sources.UnitMember("sdlc", "missing")
        with mock.patch.object(staff, "snapshot", side_effect=AssertionError("member reread the staff file")):
            self.assertEqual(staff_sources.member(missing, record), staff_sources.NoSuchMember(missing))
            self.assertEqual(staff_sources.member(staff_sources.ShowrunnerMember("sdlc"), record), staff_sources.MemberFound(showrunner))
            self.assertEqual(staff_sources.member(staff_sources.UnitMember("sdlc", "models-unit"), record), staff_sources.MemberFound(unit))

    def test_member_uses_newest_matching_launch_and_id_before_name(self) -> None:
        showrunner, unit, _ = self.tree()
        _ = self.production("sdlc", "models-unit")
        renamed = staff.rename(unit.id, "panes")
        fallback = fixture_member("unit", name="models", parent=showrunner.id,
                                  runtime_id=fixture_runtime_id("new-models-backend"))
        self.pane("w1:old", session_id=fallback.id, written_at="2026-10-29T09:00:00+00:00", environment={
            "STAFF_ID": fallback.id, "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        self.pane("w1:new", session_id=renamed.id, environment={
            "STAFF_ID": renamed.id, "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        self.pane("w1:other", session_id=fallback.id, written_at="2026-10-31T09:00:00+00:00", environment={
            "STAFF_ID": fallback.id, "SHOWRUNNER_UNIT": "other", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        result = staff_sources.member(staff_sources.UnitMember("sdlc", "models-unit"), staff.snapshot())
        self.assertEqual(result, staff_sources.MemberFound(renamed))

    def test_member_with_no_staff_id_matches_unit_by_short_name(self) -> None:
        showrunner, unit, _ = self.tree()
        _ = self.production("sdlc", "models-unit")
        self.pane("w1:p2", session_id="bae0b3d7-3d25-5a1f-9630-b7991a2f9721", environment={"SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit"})
        record = staff.snapshot()
        self.assertEqual(staff_sources.member(staff_sources.UnitMember("sdlc", "models-unit"), record), staff_sources.MemberFound(unit))
        self.assertEqual(staff_sources.member(staff_sources.ShowrunnerMember("sdlc"), record), staff_sources.MemberFound(showrunner))

    def test_legacy_member_follows_a_transfer_without_using_the_production_mark(self) -> None:
        _original, unit, _worker = self.tree()
        _ = self.production("sdlc", "models-unit")
        _ = self.production("receiving")
        receiving = fixture_member("showrunner", name="receiving",
                                   doc=staff.LiveDoc("natedev", self.source_root / "receiving-production.md"))
        moved = staff.move(unit.id, receiving.id)
        self.pane("w1:unit", session_id="bae0b3d7-3d25-5a1f-9630-b7991a2f9721", environment={
            "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        record = staff.snapshot()
        for slug in ("sdlc", "missing-source"):
            with self.subTest(slug=slug):
                self.assertEqual(staff_sources.member(staff_sources.UnitMember(slug, "models-unit"), record),
                                 staff_sources.NoSuchMember(staff_sources.UnitMember(slug, "models-unit")))
        self.assertEqual(staff_sources.member(staff_sources.UnitMember("receiving", "models-unit"), record),
                         staff_sources.MemberFound(moved))
        self.assertEqual(staff_sources.unit_parent("models-unit", "bae0b3d7-3d25-5a1f-9630-b7991a2f9721"),
                         staff_sources.UnitProduction("receiving", str(self.source_root / "receiving-production.md")))

    def test_production_member_rejects_a_moved_row_despite_its_old_id_bearing_launch(self) -> None:
        _original, unit, _worker = self.tree()
        _ = self.production("sdlc", "models-unit")
        _ = self.production("receiving", workspace="control")
        receiving = fixture_member("showrunner", name="control",
                                   doc=staff.LiveDoc("natedev", self.source_root / "receiving-production.md"))
        moved = staff.move(unit.id, receiving.id)
        self.pane("w1:unit", session_id=unit.id, environment={
            "STAFF_ID": unit.id, "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        selector = staff_sources.UnitMember("sdlc", "models-unit")
        self.assertEqual(staff_sources.member(selector, staff.snapshot()), staff_sources.NoSuchMember(selector))
        self.assertEqual(staff_sources.unit_parent("models-unit", unit.id),
                         staff_sources.UnitProduction("receiving", str(self.source_root / "receiving-production.md")))
        self.assertEqual(staff.by_id(unit.id), moved)

    def test_observed_id_bearing_pane_follows_its_current_parent(self) -> None:
        _original, unit, _worker = self.tree()
        _ = self.production("sdlc", "models-unit")
        new_doc = self.production("receiving")
        receiving = fixture_member("showrunner", name="receiving",
                                   doc=staff.LiveDoc("natedev", self.source_root / "receiving-production.md"))
        moved = staff.move(unit.id, receiving.id)
        self.pane("w1:unit", session_id=unit.id, environment={
            "STAFF_ID": unit.id, "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        other = fixture_member("showrunner", name="other")
        _ = fixture_member("unit", name="models", parent=other.id)
        self.assertEqual(staff_sources.member(staff_sources.ObservedUnit("models-unit", session_staff.LiveSession(unit.current_session)), staff.snapshot()),
                         staff_sources.MemberFound(moved))
        self.assertEqual(staff_sources.unit_parent("models-unit", unit.id),
                         staff_sources.UnitProduction("receiving", str(new_doc)))

    def test_ambiguous_legacy_member_never_uses_the_mark_to_choose_a_row(self) -> None:
        _ = self.production("sdlc", "models-unit")
        _original, _unit, _worker = self.tree()
        receiving = fixture_member("showrunner", name="receiving")
        _ = fixture_member("unit", name="models", parent=receiving.id)
        self.pane("w1:unit", session_id="bae0b3d7-3d25-5a1f-9630-b7991a2f9721", environment={
            "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        selector = staff_sources.ObservedUnit("models-unit")
        with self.assertRaisesRegex(staff.StaffUnreadable, "models.*no unique staff entry"):
            _ = staff_sources.member(selector, staff.snapshot())

    def test_mac_unit_parent_uses_one_owner_read_for_the_whole_resolution(self) -> None:
        _original, unit, _worker = self.tree()
        with self.machine("mac"):
            _ = self.production("sdlc", "models-unit")
            _ = self.production("receiving")
            receiving = fixture_member("showrunner", name="receiving",
                                       doc=staff.LiveDoc(remote.local_machine(), self.source_root / "receiving-production.md"))
            _ = staff.move(unit.id, receiving.id)
            self.pane("w1:unit", session_id=unit.id, environment={
                "STAFF_ID": unit.id, "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
            })
            before = len(self.ssh_commands())
            parent = staff_sources.unit_parent("models-unit", unit.id)
            self.assertEqual(parent.slug, "receiving")
            self.assertEqual(len(self.ssh_commands()) - before, 1)

    def test_unit_parent_agrees_with_showrunner_members_after_transfer(self) -> None:
        original, unit, _worker = self.tree()
        old_doc = self.production("sdlc", "models-unit", workspace="sdlc")
        new_doc = self.production("receiving", workspace="new-tools")
        destination = fixture_member("showrunner", name="new-tools")
        original = staff.set_doc(original.id, staff.LiveDoc("natedev", old_doc))
        destination = staff.set_doc(destination.id, staff.LiveDoc("natedev", new_doc))
        destination = staff.rename(destination.id, "control")
        self.pane("w1:showrunner", session_id=destination.id, environment={"STAFF_ID": destination.id})
        self.pane("w1:unit", session_id=unit.id, environment={
            "STAFF_ID": unit.id, "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        for slug, expected_parent, doc in (("sdlc", original, old_doc), ("receiving", destination, new_doc)):
            with self.subTest(slug=slug):
                _ = staff.move(unit.id, expected_parent.id)
                record = staff.snapshot()
                members = staff_sources.showrunner_members(record)
                with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as snapshot, \
                        mock.patch.object(pane_record, "read_strict", wraps=pane_record.read_strict) as launches, \
                        mock.patch.object(showrunners, "read_sources", wraps=showrunners.read_sources) as productions:
                    parent = staff_sources.unit_parent("models-unit", unit.id)
                self.assertEqual(parent, staff_sources.UnitProduction(slug, str(doc)))
                self.assertEqual(members[parent.slug], expected_parent)
                recorded_unit = record.by_id(unit.id)
                assert recorded_unit is not None
                self.assertEqual(members[parent.slug].id, recorded_unit.parent)
                self.assertEqual(snapshot.call_count, 1)
                self.assertEqual(launches.call_count, 1)
                self.assertEqual(productions.call_count, 1)

    def test_member_with_unresolved_or_wrong_kind_id_does_not_take_an_unrelated_name(self) -> None:
        showrunner, _unit, worker = self.tree()
        _ = self.production("sdlc", "models-unit")
        selector = staff_sources.UnitMember("sdlc", "models-unit")
        for staff_id in (fixture_runtime_id("missing-member"), showrunner.id, worker.id):
            with self.subTest(staff_id=staff_id):
                self.pane("w1:p2", session_id=staff_id, environment={ "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
                })
                self.assertEqual(staff_sources.member(selector, staff.snapshot()), staff_sources.NoSuchMember(selector))


    def test_apply_seed_keeps_current_names_and_refuses_an_invalid_transaction_atomically(self) -> None:
        first = fixture_member("showrunner", name="first")
        second = fixture_member("showrunner", name="second")
        seed = staff.SeedInput((
            staff.SeedMember("first", "showrunner", "first", first.id, staff.RootSeedParent()),
            staff.SeedMember("second", "showrunner", "second", second.id, staff.SeedParent("first")),
        ))
        renamed = staff.rename(second.id, "renamed")
        record = staff.apply_seed(seed)
        self.assertEqual(record.by_id(second.id), replace(renamed, parent=first.id))
        invalid = staff.SeedInput((
            staff.SeedMember("first", "showrunner", "first", first.id, staff.SeedParent("second")),
            staff.SeedMember("second", "showrunner", "second", second.id, staff.SeedParent("first")),
        ))
        before = self.staff_file.read_bytes()
        with self.assertRaises(staff.StaffRefused):
            _ = staff.apply_seed(invalid)
        self.assertEqual(self.staff_file.read_bytes(), before)


    def test_showrunner_discovery_still_raises_unreadable_session_records(self) -> None:
        _ = self.production("sdlc")
        failure = OSError("session records are unreadable")
        errors = io.StringIO()
        with mock.patch.object(showrunners, "socket_for", side_effect=failure), redirect_stderr(errors):
            self.assertEqual(showrunners.registered_showrunners(), [])
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn("showrunner-sdlc", errors.getvalue())
        self.assertIn("session records are unreadable", errors.getvalue())

    def test_a_stale_mac_retirement_keeps_a_unit_transferred_since_the_snapshot(self) -> None:
        old = fixture_member("showrunner", name="old")
        new = fixture_member("showrunner", name="new")
        unit = fixture_member("unit", name="models", parent=old.id)
        evidence = staff.SeedInput((), (staff.SeedRetirement(unit.id, old.id),))
        moved = staff.move(unit.id, new.id)
        before = self.staff_file.read_bytes()
        with self.machine("mac"), redirect_stderr(io.StringIO()) as errors:
            record = staff.apply_seed(evidence)
        self.assertEqual(record.by_id(unit.id), moved)
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertIn(unit.name, errors.getvalue())
        self.assertIn("parent changed", errors.getvalue())
        self.assertEqual(self.ssh_commands(), [["apply-seed", "--json"]])

    def test_retirement_transport_requires_the_observed_parent(self) -> None:
        parent = fixture_member("showrunner", name="sdlc")
        unit = fixture_member("unit", name="models", parent=parent.id)
        before = self.staff_file.read_bytes()
        for retirement in (unit.id, {"id": unit.id}, {"id": unit.id, "parent": None}):
            with self.subTest(retirement=retirement), self.assertRaises(staff.StaffRefused):
                _ = staff.apply_seed(staff.SeedInput.from_json({"members": [], "retire": [retirement]}))
        self.assertEqual(self.staff_file.read_bytes(), before)


class NonLocalMemberReferenceTests(unittest.TestCase):

    def test_unresolved_nonlocal_launch_allows_name_lookup_but_resolved_evidence_does_not(self) -> None:
        from scripts.production import session_staff

        parent_id = staff.StaffId(fixture_runtime_id("nonlocal-parent"))
        unit_id = staff.StaffId(fixture_runtime_id("nonlocal-unit"))
        worker_id = staff.StaffId(fixture_runtime_id("nonlocal-worker"))
        other_id = staff.StaffId(fixture_runtime_id("nonlocal-other-parent"))
        moved_id = staff.StaffId(fixture_runtime_id("nonlocal-moved-unit"))
        parent = staff.StaffRow(parent_id, "showrunner", "demo", None, current_session=parent_id)
        unit = staff.StaffRow(unit_id, "unit", "alpha", parent_id, current_session=unit_id)
        worker = staff.StaffRow(worker_id, "worker", "impl", unit_id, current_session=worker_id)
        other = staff.StaffRow(other_id, "showrunner", "other", None, current_session=other_id)
        moved = staff.StaffRow(moved_id, "unit", "alpha", other_id, current_session=moved_id)
        record = staff.StaffSnapshot((parent, unit, worker, other, moved))
        selector = session_staff.UnitMember("demo", "alpha-unit")
        missing = fixture_runtime_id("nonlocal-unregistered-session")
        for reference in (missing, worker.id, parent.id, moved.id):
            with self.subTest(reference=reference):
                # No production mark: this must reach the non-local launch loop.
                launch: pane_record.PaneLaunch = {
                    "pane_id": "w1:p1", "session_id": reference,
                    "config_dir": "/fixture/claude", "cwd": "/fixture/unit",
                    "name": "alpha-unit", "model": "", "remote_control": False,
                    "environment": {"SHOWRUNNER_UNIT_ID": "alpha-unit"},
                    "flags_read": True, "written_at": "2026-11-01T12:00:00Z",
                }
                sources = session_staff.MemberSources({}, (launch,))
                with mock.patch.object(staff, "snapshot", side_effect=AssertionError("unexpected staff read")):
                    found = sources.member(selector, record)
                expected = (session_staff.MemberFound(unit) if reference == missing
                            else session_staff.NoSuchMember(selector))
                self.assertEqual(found, expected)


class LegacyWorkerSourceTests(StaffTestCase):
    run_dir: Path = Path()

    @property
    def parent(self) -> staff.StaffRow:
        row = staff.by_path("sdlc/models")
        assert row is not None
        return row

    @override
    def setUp(self) -> None:
        super().setUp()
        doc = self.production("sdlc", "models-unit")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            production_membership("sdlc", "models-unit")
        parent = staff.by_path("sdlc/models")
        owner = staff.by_path("sdlc")
        assert parent is not None and owner is not None
        _ = staff.set_doc(owner.id, staff.LiveDoc("natedev", doc))
        folder = self.source_root / "worktrees" / "models-unit"
        _ = staff.set_worktree(parent.id, staff.PresentWorktree("natedev", folder), expected=parent.worktree)
        (folder / ".git").unlink()
        _ = self.fixture_git(folder, "init", "-b", "models")
        _ = self.fixture_git(folder, "config", "user.email", "fixture@example.invalid")
        _ = self.fixture_git(folder, "config", "user.name", "Fixture")
        _ = self.fixture_git(folder, "commit", "--allow-empty", "-m", "fixture")
        write_text_atomic(folder / ".unit.toml", 'plan = "active"\nresponsibilities = []\n')
        _ = self.session_record(owner.current_session, owner.name)
        _ = self.session_record(parent.current_session, "models-unit")
        self.run_dir = self.worker_run("sdlc", "models-unit", parent.current_session)

    def read_live(self) -> tuple[list[dict[str, object]], str]:
        with redirect_stderr(io.StringIO()) as errors:
            value = staff_sources.live()
        return cast(list[dict[str, object]], value["members"]), errors.getvalue()

    def test_roleless_roster_uses_exact_worker_membership_without_inventing_a_role(self) -> None:
        worker = fixture_member("worker", name="review", parent=self.parent.id)
        worker = staff.update_current_session(worker.id, staff.native_id(fixture_runtime_id("legacy-review-current")))
        path = self.roster_worker(self.run_dir, worker.name, "", worker.current_session)
        for omitted in (True, False):
            with self.subTest(omitted=omitted):
                data = cast(dict[str, dict[str, object]], json.loads(path.read_text()))
                if omitted:
                    _ = data[worker.name].pop("role", None)
                else:
                    data[worker.name]["role"] = ""
                self.write_json(path, data)
                before = self.staff_file.read_bytes(), path.read_bytes()
                members, errors = self.read_live()
                self.assertEqual(errors, "")
                found = next(member for member in members if member["id"] == worker.id)
                self.assertEqual(found["kind"], "worker")
                self.assertEqual(found["role"], "worker")
                self.assertEqual(found["name"], worker.name)
                self.assertEqual(found["parent"], self.parent.id)
                self.assertEqual(found["identity"], {"codex": worker.current_session})
                self.assertEqual((self.staff_file.read_bytes(), path.read_bytes()), before)

    def test_roleless_roster_rejects_missing_or_conflicting_membership_and_invalid_roles(self) -> None:
        worker = fixture_member("worker", name="review", parent=self.parent.id)
        other = fixture_member("unit", name="other", parent=self.parent.parent)
        elsewhere = fixture_member("worker", name="elsewhere", parent=other.id)
        cases = (
            ("missing", "", fixture_runtime_id("missing-worker")),
            (worker.name, "invalid-role", worker.current_session),
            (worker.name, "", fixture_runtime_id("conflicting-worker")),
            ("wrong-name", "", worker.current_session),
            ("elsewhere", "", elsewhere.current_session),
        )
        for name, role, identity in cases:
            with self.subTest(name=name, role=role, identity=identity):
                self.write_json(self.run_dir / "mesh_roster.json", {})
                path = self.roster_worker(self.run_dir, name, role, identity)
                before = self.staff_file.read_bytes(), path.read_bytes()
                members, errors = self.read_live()
                self.assertFalse(any(member["kind"] == "worker" for member in members), members)
                self.assertEqual(len(errors.splitlines()), 1, errors)
                self.assertIn(f"{path} worker {name}:", errors)
                self.assertIn("missing or invalid recorded worker role", errors)
                self.assertEqual((self.staff_file.read_bytes(), path.read_bytes()), before)

    def test_malformed_roster_entry_preserves_its_valid_sibling(self) -> None:
        worker = fixture_member("worker", name="review", parent=self.parent.id)
        path = self.roster_worker(self.run_dir, worker.name, "review", worker.current_session)
        data = cast(dict[str, object], json.loads(path.read_text()))
        write_text_atomic(path, json.dumps({"broken": [], **data}) + "\n")
        before = self.staff_file.read_bytes(), path.read_bytes()
        members, errors = self.read_live()
        self.assertEqual(len(errors.splitlines()), 1, errors)
        self.assertIn(f"{path} worker broken: expected a JSON object", errors)
        found = next(member for member in members if member["id"] == worker.id)
        self.assertEqual(found["role"], "review")
        self.assertEqual(found["identity"], {"codex": worker.current_session})
        self.assertEqual((self.staff_file.read_bytes(), path.read_bytes()), before)

    def paused_legacy_ledger(self, identity: str) -> Path:
        path = self.ledger_worker(self.run_dir, "sdlc", "former-models-unit", "review", "review",
                                  identity, family="claude")
        data = cast(dict[str, object], json.loads(path.read_text()))
        entries = cast(dict[str, dict[str, object]], data["workers"])
        entries["review"]["ownership"] = {"kind": "Paused"}
        self.write_json(path, data)
        self.write_json(self.run_dir / "mesh_roster.json", {})
        return path

    def test_paused_legacy_ledger_finds_renamed_parent_through_backend_current_session(self) -> None:
        worker = fixture_member("worker", name="review", parent=self.parent.id)
        worker = staff.update_current_session(worker.id, staff.native_id(fixture_runtime_id("legacy-ledger-current")))
        path = self.paused_legacy_ledger(worker.current_session)
        parent = staff.rename(self.parent.id, "renamed-models")
        _ = self.session_record(worker.current_session, "review")
        before = self.staff_file.read_bytes(), path.read_bytes()
        members, errors = self.read_live()
        self.assertEqual(errors, "")
        found = next(member for member in members if member["id"] == worker.id)
        self.assertEqual(found["parent"], parent.id)
        self.assertEqual(found["identity"], {"claude": worker.current_session})
        self.assertEqual(found["role"], "review")
        self.assertEqual(next(member["name"] for member in members if member["id"] == parent.id), parent.name)
        self.assertNotIn("staff_id", path.read_text())
        self.assertEqual((self.staff_file.read_bytes(), path.read_bytes()), before)

    def test_paused_legacy_ledger_without_native_membership_has_no_current_staff_parent(self) -> None:
        identity = fixture_runtime_id("unregistered-legacy-ledger")
        path = self.paused_legacy_ledger(identity)
        _ = self.session_record(identity, "review")
        before = self.staff_file.read_bytes(), path.read_bytes()
        members, errors = self.read_live()
        self.assertFalse(any(member["kind"] == "worker" for member in members), members)
        self.assertIn(f"{path} worker review: worker ledger has no current staff parent", errors)
        self.assertEqual((self.staff_file.read_bytes(), path.read_bytes()), before)


if __name__ == "__main__":
    _ = unittest.main()
