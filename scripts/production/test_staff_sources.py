"""Test staff membership and native worker identity sources."""
from __future__ import annotations

from scripts.production.staff_test_support import fixture_member, fixture_runtime_id, fixture_worker
from scripts.herdr import worker_panes
from scripts.herdr.test_worker_panes import PaneWorld

import io
import json
import os
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from pathlib import Path
from typing import cast, override
from unittest import mock
from zoneinfo import ZoneInfo




from scripts.lib.atomic_file import write_text_atomic
from scripts.production import staff, staff_sources, session_staff
from scripts.production.production_doc import ProductionDocument
from scripts.production.staff_test_support import StaffTestCase, unit_membership
from scripts.shutdown import pane_record, remote
from scripts.herdr import herdr
from scripts.production import showrunners
from scripts.production.session_staff import MemberSources


class StaffSourcesTests(StaffTestCase):
    @override
    def production(self, production_name: str, *units: str, workspace: str = "", parent: str = "") -> Path:
        doc = super().production(production_name, *units, workspace=workspace)
        owner = fixture_member("showrunner", name=workspace or production_name)
        _ = staff.set_doc(owner.id, staff.LiveDoc(remote.local_machine(), doc))
        if parent:
            ancestor = fixture_member("showrunner", name=parent)
            _ = staff.move(owner.id, staff.ReportsTo(ancestor.id))
        return doc


    @override
    def tree(self) -> tuple[staff.StaffRow, staff.StaffRow, staff.StaffRow]:
        owner, unit, worker = super().tree()
        owner = staff.set_doc(owner.id, staff.LiveDoc("natedev", self.source_root / "sdlc-production.md"))
        return owner, unit, worker


    def test_showrunner_admission_completes_the_started_pane_home(self) -> None:
        identity = staff.native_id(fixture_runtime_id("TEST-showrunner-admission"))
        home = self.root / "TEST-showrunner-home"
        entry = pane_record.launch("TEST-pane", identity, str(self.root), {"CLAUDE_CONFIG_DIR": str(home)}, ["claude"])
        pane_record.write("TEST-showrunner", entry)
        self.assertIsNone(staff.initial_snapshot().by_current_session(identity))
        self.assertIn("config_dir", pane_record.read("TEST-showrunner")["TEST-pane"])
        production = ProductionDocument(self.root / "TEST-production.md", "TEST-showrunner", "main",
                                                     self.root, self.root / "TEST-log.md", ZoneInfo("UTC"))
        row = staff_sources.ensure_staff_showrunner(production, runtime_id=identity)
        self.assertEqual(row.home, staff.SessionHome(home))
        self.assertEqual(staff.by_id(row.id), row)
        saved = pane_record.read("TEST-showrunner")["TEST-pane"]
        self.assertNotIn("config_dir", saved)
        self.assertEqual(saved["session_id"], row.id)

    def test_live_pane_workers_keep_all_five_states_and_report_unknown_evidence(self) -> None:
        fixture = self.row_production()
        _ = self.session_record(fixture.runner.current_session, fixture.runner.name)
        _ = self.session_record(fixture.unit.current_session, fixture.unit.name)
        rows = [fixture_worker(name, fixture.unit.id) for name in ("open", "stopped", "closed", "unknown")]
        remote_parent = fixture_member("unit", name="remote-parent", parent=fixture.runner.id,
                                       worktree=staff.PresentWorktree("mac", self.root / "remote-tree"))
        remote_worker = fixture_worker("remote-worker", remote_parent.id)
        world = PaneWorld()
        world.add(fixture.unit, "w1:p0")
        world.add(rows[0], "w1:p1")
        world.add(rows[1], "w1:p2", live=False)
        world.add(rows[3], "w1:p4")
        config = self.root / "config"
        config.mkdir()
        write_text_atomic(config / "direct.conf", "# one of: mesh, herdr\nWORKER_TRANSPORT=herdr\n")
        before = self.staff_file.read_bytes()
        with (mock.patch.dict(os.environ, {"CONF_DIR": str(config), "ROSTER_RUN_ROOT": str(self.root / "runs")}),
              mock.patch.object(worker_panes, "_client", return_value=world),
              mock.patch.object(herdr, "native_process", side_effect=world.native_process),
              mock.patch.object(herdr, "_startup_processes", side_effect=world.descendants)):
            with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as reads:
                document = staff_sources.live()
            self.assertEqual(reads.call_count, 1)
            members = {value["name"]: value for value in cast(list[dict[str, object]], document["members"])}
            self.assertEqual([members[name]["pane_state"] for name in ("open", "stopped", "closed")],
                             ["open", "stopped", "closed"])
            self.assertNotIn("pane_state", members[fixture.unit.name])
            self.assertNotIn("pane_state", members[remote_worker.name])
            self.assertEqual(document["schema"], 3)
            world.unknown["w1:p4"] = "process read denied"
            with redirect_stderr(io.StringIO()) as errors:
                document = staff_sources.live()
            members = {value["name"]: value for value in cast(list[dict[str, object]], document["members"])}
            self.assertEqual(members["unknown"]["pane_state"], "unknown")
            self.assertEqual(members["closed"]["pane_state"], "incomplete")
            self.assertIn("unknown: process read denied", errors.getvalue())
            self.assertIn("unknown: process read denied", cast(list[str], document["problems"]))
            self.assertFalse(document["complete"])
            self.assertEqual(self.staff_file.read_bytes(), before)
            self.assertEqual(list((self.root / "runs").glob("**/worker_transport*")), [])

    def test_live_reports_a_bad_run_pin_and_keeps_mesh_members(self) -> None:
        fixture = self.row_production()
        _ = self.session_record(fixture.unit.current_session, fixture.unit.name)
        worker = fixture_worker("mesh-worker", fixture.unit.id)
        run = self.root / "runs/pinned"
        run.mkdir(parents=True)
        write_text_atomic(run / "mesh_roster.json", json.dumps({"mesh-worker": {"staff_id": worker.id}}))
        pin = run / "worker_transport"
        for failure in ("invalid", "unreadable"):
            with self.subTest(failure=failure):
                if failure == "invalid":
                    write_text_atomic(pin, "junk\n")
                else:
                    pin.unlink()
                    pin.mkdir()
                with (mock.patch.dict(os.environ, {"ROSTER_RUN_ROOT": str(run.parent)}),
                      redirect_stderr(io.StringIO()) as errors):
                    document = staff_sources.live()
                self.assertFalse(document["complete"])
                self.assertIn(str(pin), errors.getvalue())
                problems = cast(list[str], document["problems"])
                self.assertTrue(any(str(pin) in problem for problem in problems), problems)
                if failure == "invalid":
                    self.assertTrue(any("junk" in problem for problem in problems), problems)
                members = cast(list[dict[str, object]], document["members"])
                self.assertIn(worker.id, [member["id"] for member in members])

    def test_live_reports_unreadable_owner_and_record_without_an_empty_member_list(self) -> None:
        _ = self.row_production()
        _ = self.write_document({"schema": 2, "rows": ["invalid member"]})
        with redirect_stderr(io.StringIO()) as errors:
            document = staff_sources.live()
        self.assertFalse(document["complete"])
        self.assertNotIn("members", document)
        self.assertIn("staff membership", errors.getvalue())
        self.assertTrue(document["problems"])
        with (self.machine("mac"), mock.patch.dict(os.environ, {"STAFF_TEST_SSH_FAIL": "1"}),
              redirect_stderr(io.StringIO()) as errors):
            document = staff_sources.live()
        self.assertFalse(document["complete"])
        self.assertNotIn("members", document)
        self.assertIn("not reached", errors.getvalue())
        self.assertTrue(document["problems"])

    def test_live_names_an_unreadable_production_document_and_keeps_its_members(self) -> None:
        fixture = self.row_production()
        _ = self.session_record(fixture.unit.current_session, fixture.unit.name)
        _ = staff.set_doc(fixture.runner.id, staff.LiveDoc("natedev", self.root / "other-production.md"))
        with redirect_stderr(io.StringIO()) as errors:
            document = staff_sources.live()
        self.assertFalse(document["complete"])
        self.assertIn("production doc could not be read", errors.getvalue())
        self.assertIn(fixture.unit.id, [value["id"] for value in cast(list[dict[str, object]], document["members"])])

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
        # Synthetic processes have no open rollout unless the test supplies one.
        _ = self.enterContext(mock.patch.object(herdr, "codex_rollout_thread",
                                                side_effect=herdr.NoOpenCodexRollout("no open rollout")))
        # Keep the host's scratch policy, with a private root for this fixture.
        _ = self.enterContext(mock.patch.object(showrunners, "SCRATCH_ROOT", self.root / "scratch-checkout"))


    def director_pane(self, pane_id: str, row: staff.StaffRow, folder: Path, environment: dict[str, str]) -> None:
        self.pane(pane_id, session_id=row.id, environment=environment)
        entries = pane_record.read("agents")
        entries[pane_id]["cwd"] = str(folder)
        pane_record.save("agents", entries)


    def test_live_staff_roots_a_surviving_style_child_and_keeps_its_unit_after_parent_wrap(self) -> None:
        parent_doc = self.production("parent")
        style_doc = self.production("style", "names", parent="parent")
        write_text_atomic(style_doc, "- **Kind:** style\n" + style_doc.read_text())
        self.pane("w1:style", session_id="ffa0f240-6489-5d0c-a907-083d633c124a", environment={})
        self.pane("w1:names", session_id="7f89b939-c570-5fd3-9802-e62dfb8aeea6", environment={
            ('' + 'SHOWRUNNER_' + 'UNIT'): "style", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "names"})
        _ = self.session_record("ffa0f240-6489-5d0c-a907-083d633c124a", "style")
        _ = self.session_record("7f89b939-c570-5fd3-9802-e62dfb8aeea6", "names")
        parent = next(row for row in staff.find(kind="showrunner") if row.name == "parent")
        child = next(row for row in staff.find(kind="showrunner") if row.name == "style")
        unit = unit_membership("style", "names")
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



    def test_live_inputs_keep_rejected_production_documents_and_read_them_after_correction(self) -> None:
        _ = self.production("sdlc", "models")
        _ = self.session_record("cdc4420c-ae5d-5a9a-8135-75e8c0875088", "sdlc")
        rejected = self.production("recovering", "later")
        _ = self.session_record("8e2a7bad-d715-5c2d-9ec5-2a8bf9a81289", "recovering")
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
        _ = self.production("sdlc", "models")
        _ = self.session_record("cdc4420c-ae5d-5a9a-8135-75e8c0875088", "sdlc")
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



    def test_live_owner_ignores_a_newer_launch_whose_session_has_ended(self) -> None:
        _ = self.production("sdlc", "models")
        unit = staff.by_path("sdlc/models")
        assert unit is not None
        _ = self.session_record("cdc4420c-ae5d-5a9a-8135-75e8c0875088", "sdlc")
        _ = self.session_record(unit.id, "models")
        self.pane("w1:running", session_id=unit.id, environment={"STAFF_ID": unit.id},
                  written_at="2026-10-30T09:00:00+00:00")
        self.pane("w1:ended", session_id="3790170d-f8c8-5dbd-ba9b-38eb5179177f", environment={"STAFF_ID": unit.id},
                  written_at="2026-10-31T09:00:00+00:00")
        members = cast(list[dict[str, object]], staff_sources.live()["members"])
        member = next(member for member in members if member["id"] == unit.id)
        self.assertEqual(member["identity"], {"claude": unit.id})



    def test_live_inputs_include_the_remote_owner_record_and_exclude_process_and_system_reads(self) -> None:
        owner = self.tree()[0]
        for machine in ("natedev", "mac"):
            _ = staff.set_doc(owner.id, staff.LiveDoc(
                machine, self.root / f"{machine}-sources" / "sdlc-production.md"))
            with self.subTest(machine=machine), self.machine(machine):
                if machine == "mac":
                    self.assertNotIn("STAFF_FILE", os.environ)
                _ = self.production("sdlc", "models")
                _ = self.session_record("cdc4420c-ae5d-5a9a-8135-75e8c0875088", "sdlc")
                remote_reads = len(self.ssh_commands()) if machine == "mac" else 0
                with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as reads:
                    value = staff_sources.live()
                self.assertEqual(reads.call_count, 1)
                if machine == "mac":
                    self.assertEqual(len(self.ssh_commands()) - remote_reads, 1)
                inputs = cast(list[str], value["inputs"])
                self.assertIn(str(self.staff_file), inputs)
                self.assert_no_system_inputs(inputs)


    def assert_no_system_inputs(self, inputs: list[str]) -> None:
        # The test's own temp root may sit under /dev/shm (the gate sets TMPDIR there).
        root = f"{self.root}/"
        for path in inputs:
            if path.startswith(root):
                continue
            self.assertFalse(path.startswith(("/proc/", "/dev/", "/usr/share/zoneinfo/")), path)


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

    def test_codex_scan_continues_after_a_process_environment_disappears(self) -> None:
        from scripts.message import process_table

        doc = self.fill_production("environment-demo")
        owner = staff.by_path("environment-demo")
        assert owner is not None
        _ = staff.set_doc(owner.id, staff.LiveDoc("natedev", doc))
        table = {pid: process_table.Process(pid, 1, "codex", "codex") for pid in (42401, 42402)}
        with (mock.patch.object(process_table, "snapshot", return_value=table),
              mock.patch.object(herdr, "process_environment", side_effect=[
                  FileNotFoundError("process disappeared"), {"CODEX_THREAD_ID": owner.current_session}]),
              redirect_stderr(io.StringIO()) as errors):
            document = staff_sources.live()
        members = cast(list[dict[str, object]], document["members"])
        found = next(member for member in members if member["id"] == owner.id)
        self.assertEqual(found["identity"], {"codex": owner.current_session})
        self.assertFalse(document["complete"])
        self.assertEqual(errors.getvalue().splitlines(), [
            "staff sources: Codex process 42401: process disappeared"])

    def test_codex_worker_without_a_launch_record_is_staff(self) -> None:
        from scripts.message import codex_daemon, process_table

        owner = unit_membership("show", "unit")
        worker = fixture_worker("impl", owner.id, backend=staff.CodexBackend(self.root / "codex"))
        process = self.root / "proc/42401"
        process.mkdir(parents=True)
        for source in ("environment", "resume", "terminal", "pane"):
            with self.subTest(source=source):
                arguments = (["codex", "resume", worker.current_session] if source == "resume"
                             else ["codex", "--no-daemon"])
                _ = (process / "cmdline").write_bytes("\0".join(arguments).encode() + b"\0")
                table = {42401: process_table.Process(42401, 1, "codex", " ".join(arguments))}
                environment = {"HERDR_PANE_ID": "test:pane"}
                if source == "environment":
                    environment["CODEX_THREAD_ID"] = worker.current_session
                pane: dict[str, object] = {"terminal_title_stripped": "impl | idle"}
                if source == "pane":
                    pane["agent_session"] = {"value": worker.current_session}
                    pane["terminal_title_stripped"] = "Reply ready | herdr"
                with (mock.patch.dict(os.environ, {"HERDR_PROC": str(process.parent), "HERDR_PLATFORM": "linux"}),
                      mock.patch.object(process_table, "snapshot", return_value=table),
                      mock.patch.object(herdr, "process_environment", return_value=environment),
                      mock.patch.object(herdr, "herdr_place_of", return_value=herdr.HerdrPlace("agents", "test:pane")),
                      mock.patch.object(herdr.Herdr, "result", return_value={
                          "pane": pane, "process_info": {"foreground_processes": [{"pid": 42401, "argv": arguments}]}}),
                      mock.patch.object(codex_daemon, "loaded_sessions", return_value=[
                          codex_daemon.DaemonSession(worker.current_session, "impl", str(self.root), "idle")])):
                    evidence = staff_sources.observe_workers(staff.snapshot(), MemberSources(()), current_only=True)
                self.assertEqual(evidence.diagnostics, ())
                self.assertEqual(evidence.identities[worker.id], {"codex": worker.current_session})
                self.assertEqual([entry.row for entry in evidence.workers], [worker])

    def test_only_the_panes_foreground_codex_can_use_its_metadata(self) -> None:
        from scripts.message import process_table

        owner = unit_membership("show", "unit")
        worker = fixture_worker("impl", owner.id, backend=staff.CodexBackend(self.root / "codex"))
        table = {42401: process_table.Process(42401, 1, "codex", "codex task")}
        payload: dict[str, object] = {
            "pane": {"agent_session": {"value": worker.current_session}},
            "process_info": {"foreground_processes": [{"pid": 42402, "argv": ["codex", "--no-daemon"]}]},
        }
        with (mock.patch.object(herdr, "codex_resume_thread", side_effect=ValueError("no resume argument")),
              mock.patch.object(herdr, "herdr_place_of", return_value=herdr.HerdrPlace("agents", "test:pane")),
              mock.patch.object(herdr.Herdr, "result", return_value=payload) as reads,
              mock.patch.object(herdr, "process_environment", return_value={}),
              mock.patch.object(process_table, "snapshot", return_value=table),
              mock.patch.object(session_staff, "member_sources", return_value=MemberSources(()))):
            reader = staff_sources.CodexProcessThreads()
            self.assertEqual(reader.thread(42401, {}), "")
            self.assertEqual(reader.check(42403, "").native_id, "")
            self.assertEqual(reader.thread(42402, {}), worker.current_session)
            self.assertEqual(reads.call_args_list, [
                mock.call("pane", "process-info", "--pane", "test:pane"),
                mock.call("pane", "get", "test:pane"),
            ])
            staff_sources.require_worker_stopped(staff.snapshot(), worker.id)

    def test_current_rollout_overrides_resume_and_does_not_block_the_old_thread(self) -> None:
        from scripts.message import process_table

        owner = unit_membership("show", "unit")
        old = fixture_worker("old", owner.id, backend=staff.CodexBackend(self.root / "codex"))
        current = fixture_worker("current", owner.id, backend=staff.CodexBackend(self.root / "codex"))
        table = {42401: process_table.Process(42401, 1, "codex", f"codex resume {old.current_session}")}
        record = staff.snapshot()
        with (mock.patch.object(herdr, "codex_rollout_thread", return_value=current.current_session),
              mock.patch.object(herdr, "codex_resume_thread", return_value=old.current_session) as resume,
              mock.patch.object(herdr, "process_environment", return_value={"CODEX_THREAD_ID": old.current_session}),
              mock.patch.object(process_table, "snapshot", return_value=table),
              mock.patch.object(session_staff, "member_sources", return_value=MemberSources(()))):
            reader = staff_sources.CodexProcessThreads()
            self.assertEqual(reader.thread(42401, {"CODEX_THREAD_ID": old.current_session}), current.current_session)
            staff_sources.require_worker_stopped(record, old.id)
            with self.assertRaises(staff.StaffRefused):
                staff_sources.require_worker_stopped(record, current.id)
        resume.assert_not_called()

    def test_unreadable_open_rollout_never_uses_a_lower_identity_source(self) -> None:
        for failure in (PermissionError("rollout denied"), ValueError("rollout ambiguous"),
                        herdr.HerdrFailure("rollout read failed")):
            with (self.subTest(failure=type(failure).__name__),
                  mock.patch.object(herdr, "codex_rollout_thread", side_effect=failure),
                  mock.patch.object(herdr, "codex_resume_thread") as resume,
                  mock.patch.object(herdr, "herdr_place_of") as place):
                self.assertEqual(staff_sources.CodexProcessThreads().thread(42401, {"CODEX_THREAD_ID": "old"}), "")
            resume.assert_not_called()
            place.assert_not_called()

    def test_resumed_codex_session_without_a_staff_row_is_quiet(self) -> None:
        from scripts.message import process_table

        owner = unit_membership("show", "unit")
        worker = fixture_worker("impl", owner.id, backend=staff.CodexBackend(self.root / "codex"))
        thread = fixture_runtime_id("freestanding-codex")
        table = {42401: process_table.Process(42401, 1, "codex", f"codex resume {thread}")}
        process = self.root / "proc/42401"
        process.mkdir(parents=True)
        _ = (process / "cmdline").write_bytes(f"codex\0resume\0{thread}\0".encode())
        record = staff.snapshot()
        with (mock.patch.dict(os.environ, {"HERDR_PROC": str(process.parent), "HERDR_PLATFORM": "linux"}),
              mock.patch.object(process_table, "snapshot", return_value=table),
              mock.patch.object(herdr, "process_environment", return_value={"HERDR_PANE_ID": "test:pane"}),
              redirect_stderr(io.StringIO()) as errors):
            evidence = staff_sources.observe_workers(record, MemberSources(()), current_only=True)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(evidence.diagnostics, ())
        self.assertNotIn(worker.id, evidence.identities)
        self.assertEqual(evidence.workers, ())

    def test_missing_codex_thread_reports_one_codex_problem(self) -> None:
        from scripts.message import process_table

        _ = self.fill_production("pane-demo", "models")
        unit = staff.by_path("pane-demo/models")
        assert unit is not None
        launch = pane_record.launch("test:pane", unit.id, str(self.root), {}, [])
        _ = staff.update_current_session(unit.current_session, staff.native_id(fixture_runtime_id("pane-unit-current")))
        table = {42401: process_table.Process(42401, 1, "codex", "codex resume")}
        record = staff.snapshot()
        sources = MemberSources((launch,))
        with (mock.patch.object(process_table, "snapshot", return_value=table),
              mock.patch.object(herdr, "process_environment", return_value={"HERDR_PANE_ID": "test:pane"}),
              mock.patch.object(staff_sources.CodexProcessThreads, "thread", return_value="") as recover):
            evidence = staff_sources.observe_workers(record, sources)
        recover.assert_called_once_with(42401, {"HERDR_PANE_ID": "test:pane"})
        expected = ("staff sources: Codex process 42401 in pane test:pane: "
                    "cannot read its thread; staff status may be incomplete")
        self.assertEqual(evidence.diagnostics, (expected,))
        self.assertNotIn("Claude", "\n".join(evidence.diagnostics))


    def test_codex_process_that_exits_during_environment_read_leaves_no_diagnostic(self) -> None:
        from scripts.message import process_table

        table = {42401: process_table.Process(42401, 1, "codex", "codex resume")}
        for failure in (FileNotFoundError("process exited"), PermissionError("permission denied"),
                        herdr.HerdrFailure("process 42401: exited")):
            with (self.subTest(failure=type(failure).__name__),
                  mock.patch.object(process_table, "snapshot", side_effect=[table, {}]),
                  mock.patch.object(herdr, "process_environment", side_effect=failure),
                  redirect_stderr(io.StringIO()) as errors):
                evidence = staff_sources.observe_workers(staff.StaffSnapshot(()), MemberSources(()))
            self.assertEqual(evidence.diagnostics, ())
            self.assertEqual(errors.getvalue(), "")


    def test_codex_process_with_unreadable_environment_reports_a_diagnostic(self) -> None:
        from scripts.message import process_table

        table = {42401: process_table.Process(42401, 1, "codex", "codex resume")}
        with (mock.patch.object(process_table, "snapshot", side_effect=[table, table]),
              mock.patch.object(herdr, "process_environment", side_effect=PermissionError("permission denied"))):
            evidence = staff_sources.observe_workers(staff.StaffSnapshot(()), MemberSources(()))
        self.assertIn("staff sources: Codex process 42401: permission denied", evidence.diagnostics)


    def test_codex_without_a_thread_in_an_unowned_pane_is_not_staff(self) -> None:
        from scripts.message import process_table

        _ = self.fill_production("other-pane-demo", "models")
        unit = staff.by_path("other-pane-demo/models")
        assert unit is not None
        held = pane_record.launch("other:pane", unit.id, str(self.root), {}, [])
        unowned = pane_record.launch("test:pane", fixture_runtime_id("unowned-pane"), str(self.root), {}, [])
        table = {42401: process_table.Process(42401, 1, "codex", "codex resume")}
        for launches in ((held,), (held, unowned)):
            with (self.subTest(recorded=unowned in launches),
                  mock.patch.object(process_table, "snapshot", return_value=table),
                  mock.patch.object(herdr, "process_environment", return_value={"HERDR_PANE_ID": "test:pane"})):
                evidence = staff_sources.observe_workers(staff.snapshot(), MemberSources(launches))
            self.assertEqual(evidence.diagnostics, ())
            self.assertEqual(evidence.identities, {})


    def test_missing_codex_thread_in_a_pane_with_staff_unit_labels_keeps_the_scan_incomplete(self) -> None:
        from scripts.message import process_table

        _ = self.fill_production("label-pane-demo", "models")
        row = staff.by_path("label-pane-demo/models")
        assert row is not None
        launch = pane_record.launch("test:pane", row.current_session, str(self.root), {
            ('' + 'SHOWRUNNER_' + 'UNIT'): "label-pane-demo", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "models"}, [])
        table = {42401: process_table.Process(42401, 1, "codex", "codex resume")}
        with (mock.patch.object(process_table, "snapshot", return_value=table),
              mock.patch.object(herdr, "process_environment", return_value={"HERDR_PANE_ID": "test:pane"})):
            evidence = staff_sources.observe_workers(staff.snapshot(), MemberSources((launch,)))
        self.assertEqual(evidence.diagnostics, (
            "staff sources: Codex process 42401 in pane test:pane: cannot read its thread; staff status may be incomplete",))


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


    def test_codex_without_a_thread_or_pane_is_not_staff(self) -> None:
        from scripts.message import process_table

        table = {42401: process_table.Process(42401, 1, "codex", "/local/bin/codex resume")}
        with (mock.patch.object(process_table, "snapshot", return_value=table),
              mock.patch.object(herdr, "process_environment", return_value={})):
            evidence = staff_sources.observe_workers(staff.StaffSnapshot(()), MemberSources(()))
        self.assertEqual(evidence.diagnostics, ())
        self.assertEqual(evidence.workers, ())


    def test_unparsable_node_codex_arguments_keep_the_scan_incomplete(self) -> None:
        from scripts.message import process_table

        table = {42401: process_table.Process(
            42401, 1, "node", 'node /modules/@openai/codex/bin/codex.js --config "unfinished')}
        with (mock.patch.object(process_table, "snapshot", return_value=table),
              mock.patch.object(herdr, "process_environment") as environment):
            evidence = staff_sources.observe_workers(staff.StaffSnapshot(()), MemberSources(()))
        environment.assert_not_called()
        self.assertEqual(evidence.diagnostics,
                         ("staff sources: Codex process scan: pid 42401: No closing quotation",))


    def test_failed_codex_process_snapshot_does_not_report_a_claude_registry_failure(self) -> None:
        from scripts.message import process_table

        with mock.patch.object(process_table, "snapshot", side_effect=OSError("process table unavailable")):
            evidence = staff_sources.observe_workers(staff.StaffSnapshot(()), MemberSources(()))
        self.assertEqual(evidence.diagnostics, ("staff sources: Codex processes: process table unavailable",))
        self.assertNotIn("Claude", "\n".join(evidence.diagnostics))


    def test_live_staff_includes_codex_showrunner_and_unit_by_current_thread_without_role_inference(self) -> None:
        from scripts.message import process_table

        _ = self.fill_production("codex-demo", "models")
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


    def test_persisted_member_reference_survives_two_clears_rename_and_name_reuse(self) -> None:
        parent, unit, _ = self.tree()
        _ = self.production("sdlc", "models")
        launch = pane_record.read_strict("agents")["fixture:sdlc/models"]
        pane_record.write("agents", launch)
        second = staff.StaffId(fixture_runtime_id("member-clear-B"))
        third = staff.StaffId(fixture_runtime_id("member-clear-C"))
        _ = staff.update_current_session(unit.current_session, second)
        current = staff.update_current_session(second, third)
        current = staff.rename(current.id, "renamed")
        replacement = fixture_member("unit", name="models", parent=parent.id,
                                     runtime_id=fixture_runtime_id("replacement-models"))
        path = pane_record.record_path("agents")
        selector = staff_sources.UnitMember("sdlc", "models")
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
                    found = session_staff.unit_match(selector, record, session_staff.StoredReference(reference),
                        parent=session_staff.RequiredParent(parent))
                self.assertEqual(found, staff_sources.MemberFound(current))
                self.assertNotEqual(current.id, replacement.id)
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)


    def test_persisted_member_continues_past_a_cleared_launch_under_another_parent(self) -> None:
        parent, moved, _ = self.tree()
        _ = self.production("sdlc", "models")
        other = fixture_member("showrunner", name="elsewhere")
        moved = staff.move(moved.id, staff.ReportsTo(other.id))
        moved = staff.update_current_session(moved.current_session, staff.StaffId(fixture_runtime_id("moved-B")))
        target = fixture_member("unit", name="actual", parent=parent.id)
        self.pane("newest-local", session_id=moved.id, written_at="2026-11-01T12:00:00Z",
                  environment={('' + 'SHOWRUNNER_' + 'UNIT'): "sdlc", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "models"})
        self.pane("older-transferred", session_id=target.id, written_at="2026-10-31T12:00:00Z",
                  environment={('' + 'SHOWRUNNER_' + 'UNIT'): "former", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "models"})
        selector = staff_sources.UnitMember("sdlc", "models")
        self.assertEqual(session_staff.unit_match(selector, staff.snapshot(), session_staff.StoredReference(target.id),
            parent=session_staff.RequiredParent(parent)), staff_sources.MemberFound(target))
        # Each stored reference selects one row without copied labels.
        competing = fixture_member("unit", name="competing", parent=parent.id)
        self.pane("competing", session_id=competing.id,
                  environment={('' + 'SHOWRUNNER_' + 'UNIT'): "former", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "models"})
        self.assertEqual(session_staff.unit_match(selector, staff.snapshot(),
            session_staff.StoredReference(competing.id), parent=session_staff.RequiredParent(parent)),
            staff_sources.MemberFound(competing))
        self.assertEqual(session_staff.unit_match(selector, staff.snapshot(),
            session_staff.StoredReference(moved.id), parent=session_staff.RequiredParent(parent)),
            staff_sources.NoSuchMember(selector))


    def test_persisted_cleared_references_preserve_kind_and_parent_safeguards(self) -> None:
        parent, unit, worker = self.tree()
        _ = self.production("sdlc", "models")
        other = fixture_member("showrunner", name="elsewhere")
        moved = staff.move(unit.id, staff.ReportsTo(other.id))
        _ = staff.update_current_session(moved.current_session, staff.StaffId(fixture_runtime_id("wrong-parent-B")))
        _ = fixture_member("unit", name="models", parent=parent.id, runtime_id=fixture_runtime_id("same-name"))
        selector = staff_sources.UnitMember("sdlc", "models")
        for reference in (fixture_runtime_id("missing"), parent.id, worker.id, unit.id):
            with self.subTest(reference=reference):
                self.pane("latest", session_id=reference, written_at="2026-11-01T12:00:00Z",
                          environment={('' + 'SHOWRUNNER_' + 'UNIT'): "sdlc", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "models"})
                self.assertEqual(session_staff.unit_match(selector, staff.snapshot(), session_staff.StoredReference(reference),
                    parent=session_staff.RequiredParent(parent)), staff_sources.NoSuchMember(selector))


    def test_runtime_grouping_rejects_fixed_and_superseded_sessions_after_two_clears(self) -> None:
        from scripts.production import session_staff

        _, unit, _ = self.tree()
        _ = self.production("sdlc", "models")
        second = staff.StaffId(fixture_runtime_id("group-B"))
        third = staff.StaffId(fixture_runtime_id("group-C"))
        _ = staff.update_current_session(unit.current_session, second)
        _ = staff.update_current_session(second, third)
        record = staff.snapshot()
        sources = session_staff.member_sources()
        for runtime in (third, unit.id, second):
            with self.subTest(runtime=runtime):
                marked = session_staff.LabeledSession("models",
                    session_staff.LiveClaude("models", runtime, "", 123),
                    herdr.HerdrPlace("agents", "fixture:sdlc/models"), identity=session_staff.LiveSession(runtime))
                with mock.patch.object(staff, "snapshot", side_effect=AssertionError("owner reread")):
                    grouped = session_staff.group_labeled_sessions((marked,), record=record, sources=sources)
                    if runtime != third:
                        self.assertEqual(grouped.units, {})
                    else:
                        self.assertEqual(grouped.units.get("sdlc", {}), {"models": marked})

    def test_current_session_parent_and_showrunner_follow_the_retained_launch_after_rename(self) -> None:
        from scripts.production import unit_lookup
        from scripts.production import session_staff

        parent, unit, _ = self.tree()
        doc = self.production("sdlc", "models")
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
            self.assertEqual(unit_lookup.unit_parent("models", session_staff.LiveSession(current.current_session), record=record),
                             unit_lookup.UnitProduction("sdlc", str(doc)))
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)


    def test_member_result_is_explicit_and_uses_only_the_supplied_snapshot(self) -> None:
        showrunner, unit, _ = self.tree()
        _ = self.production("sdlc", "models")
        record = staff.snapshot()
        missing = staff_sources.UnitMember("sdlc", "missing")
        with mock.patch.object(staff, "snapshot", side_effect=AssertionError("member reread the staff file")):
            self.assertEqual(staff_sources.member(missing, record), staff_sources.NoSuchMember(missing))
            self.assertEqual(staff_sources.member(staff_sources.ShowrunnerMember("sdlc"), record), staff_sources.MemberFound(showrunner))
            self.assertEqual(staff_sources.member(staff_sources.UnitMember("sdlc", "models"), record), staff_sources.MemberFound(unit))


    def test_member_uses_newest_matching_launch_and_id_before_name(self) -> None:
        showrunner, unit, _ = self.tree()
        _ = self.production("sdlc", "models")
        renamed = staff.rename(unit.id, "panes")
        fallback = fixture_member("unit", name="models", parent=showrunner.id,
                                  runtime_id=fixture_runtime_id("new-models-backend"))
        self.pane("w1:old", session_id=fallback.id, written_at="2026-10-29T09:00:00+00:00", environment={
            "STAFF_ID": fallback.id, ('' + 'SHOWRUNNER_' + 'UNIT'): "sdlc", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "models",
        })
        self.pane("w1:new", session_id=renamed.id, environment={
            "STAFF_ID": renamed.id, ('' + 'SHOWRUNNER_' + 'UNIT'): "sdlc", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "models",
        })
        self.pane("w1:other", session_id=fallback.id, written_at="2026-10-31T09:00:00+00:00", environment={
            "STAFF_ID": fallback.id, ('' + 'SHOWRUNNER_' + 'UNIT'): "other", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "models",
        })
        result = session_staff.unit_match(staff_sources.UnitMember("sdlc", "models"), staff.snapshot(),
            session_staff.StoredReference(renamed.id), parent=session_staff.RequiredParent(showrunner))
        self.assertEqual(result, staff_sources.MemberFound(renamed))


    def test_member_with_no_staff_id_matches_unit_by_short_name(self) -> None:
        showrunner, unit, _ = self.tree()
        _ = self.production("sdlc", "models")
        self.pane("w1:p2", session_id="bae0b3d7-3d25-5a1f-9630-b7991a2f9721", environment={('' + 'SHOWRUNNER_' + 'UNIT'): "sdlc", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "models"})
        record = staff.snapshot()
        self.assertEqual(staff_sources.member(staff_sources.UnitMember("sdlc", "models"), record), staff_sources.MemberFound(unit))
        self.assertEqual(staff_sources.member(staff_sources.ShowrunnerMember("sdlc"), record), staff_sources.MemberFound(showrunner))


    def test_legacy_member_follows_a_transfer_without_using_the_production_mark(self) -> None:
        _original, unit, _worker = self.tree()
        _ = self.production("sdlc", "models")
        _ = self.production("receiving")
        receiving = fixture_member("showrunner", name="receiving",
                                   doc=staff.LiveDoc("natedev", self.source_root / "receiving-production.md"))
        moved = staff.move(unit.id, staff.ReportsTo(receiving.id))
        self.pane("w1:unit", session_id="bae0b3d7-3d25-5a1f-9630-b7991a2f9721", environment={
            ('' + 'SHOWRUNNER_' + 'UNIT'): "sdlc", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "models",
        })
        record = staff.snapshot()
        for production_name in ("sdlc", "missing-source"):
            with self.subTest(name=production_name):
                self.assertEqual(staff_sources.member(staff_sources.UnitMember(production_name, "models"), record),
                                 staff_sources.NoSuchMember(staff_sources.UnitMember(production_name, "models")))
        self.assertEqual(staff_sources.member(staff_sources.UnitMember("receiving", "models"), record),
                         staff_sources.MemberFound(moved))
        self.assertEqual(staff_sources.unit_parent("models", "bae0b3d7-3d25-5a1f-9630-b7991a2f9721"),
                         staff_sources.UnitProduction("receiving", str(self.source_root / "receiving-production.md")))


    def test_production_member_rejects_a_moved_row_despite_its_old_id_bearing_launch(self) -> None:
        _original, unit, _worker = self.tree()
        _ = self.production("sdlc", "models")
        _ = self.production("receiving", workspace="control")
        receiving = fixture_member("showrunner", name="control",
                                   doc=staff.LiveDoc("natedev", self.source_root / "receiving-production.md"))
        moved = staff.move(unit.id, staff.ReportsTo(receiving.id))
        self.pane("w1:unit", session_id=unit.id, environment={
            "STAFF_ID": unit.id, ('' + 'SHOWRUNNER_' + 'UNIT'): "sdlc", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "models",
        })
        selector = staff_sources.UnitMember("sdlc", "models")
        self.assertEqual(staff_sources.member(selector, staff.snapshot()), staff_sources.NoSuchMember(selector))
        self.assertEqual(staff_sources.unit_parent("models", unit.id),
                         staff_sources.UnitProduction("control", str(self.source_root / "receiving-production.md")))
        self.assertEqual(staff.by_id(unit.id), moved)


    def test_observed_id_bearing_pane_follows_its_current_parent(self) -> None:
        _original, unit, _worker = self.tree()
        _ = self.production("sdlc", "models")
        new_doc = self.production("receiving")
        receiving = fixture_member("showrunner", name="receiving",
                                   doc=staff.LiveDoc("natedev", self.source_root / "receiving-production.md"))
        moved = staff.move(unit.id, staff.ReportsTo(receiving.id))
        self.pane("w1:unit", session_id=unit.id, environment={
            "STAFF_ID": unit.id, ('' + 'SHOWRUNNER_' + 'UNIT'): "sdlc", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "models",
        })
        other = fixture_member("showrunner", name="other")
        _ = fixture_member("unit", name="models", parent=other.id)
        self.assertEqual(staff_sources.member(staff_sources.ObservedUnit("models", session_staff.LiveSession(unit.current_session)), staff.snapshot()),
                         staff_sources.MemberFound(moved))
        self.assertEqual(staff_sources.unit_parent("models", unit.id),
                         staff_sources.UnitProduction("receiving", str(new_doc)))


    def test_ambiguous_legacy_member_never_uses_the_mark_to_choose_a_row(self) -> None:
        _ = self.production("sdlc", "models")
        _original, _unit, _worker = self.tree()
        receiving = fixture_member("showrunner", name="receiving")
        _ = fixture_member("unit", name="models", parent=receiving.id)
        self.pane("w1:unit", session_id="bae0b3d7-3d25-5a1f-9630-b7991a2f9721", environment={
            ('' + 'SHOWRUNNER_' + 'UNIT'): "sdlc", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "models",
        })
        selector = staff_sources.ObservedUnit("models")
        with self.assertRaisesRegex(staff.StaffUnreadable, "no native runtime identity"):
            _ = staff_sources.member(selector, staff.snapshot())


    def test_mac_unit_parent_uses_one_owner_read_for_the_whole_resolution(self) -> None:
        _original, unit, _worker = self.tree()
        with self.machine("mac"):
            _ = self.production("sdlc", "models")
            _ = self.production("receiving")
            receiving = fixture_member("showrunner", name="receiving",
                                       doc=staff.LiveDoc(remote.local_machine(), self.source_root / "receiving-production.md"))
            _ = staff.move(unit.id, staff.ReportsTo(receiving.id))
            self.pane("w1:unit", session_id=unit.id, environment={
                "STAFF_ID": unit.id, ('' + 'SHOWRUNNER_' + 'UNIT'): "sdlc", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "models",
            })
            before = len(self.ssh_commands())
            parent = staff_sources.unit_parent("models", unit.id)
            self.assertEqual(parent.name, "receiving")
            self.assertEqual(len(self.ssh_commands()) - before, 1)


    def test_unit_parent_agrees_with_showrunner_members_after_transfer(self) -> None:
        original, unit, _worker = self.tree()
        old_doc = self.production("sdlc", "models", workspace="sdlc")
        new_doc = self.production("receiving", workspace="new-tools")
        destination = fixture_member("showrunner", name="new-tools")
        original = staff.set_doc(original.id, staff.LiveDoc("natedev", old_doc))
        destination = staff.set_doc(destination.id, staff.LiveDoc("natedev", new_doc))
        destination = staff.rename(destination.id, "control")
        self.pane("w1:showrunner", session_id=destination.id, environment={"STAFF_ID": destination.id})
        self.pane("w1:unit", session_id=unit.id, environment={
            "STAFF_ID": unit.id, ('' + 'SHOWRUNNER_' + 'UNIT'): "sdlc", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "models",
        })
        for production_name, expected_parent, doc in (("sdlc", original, old_doc), ("control", destination, new_doc)):
            with self.subTest(name=production_name):
                _ = staff.move(unit.id, staff.ReportsTo(expected_parent.id))
                record = staff.snapshot()
                members = staff_sources.showrunner_members(record)
                with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as snapshot, \
                        mock.patch.object(pane_record, "read_strict", wraps=pane_record.read_strict) as launches, \
                        mock.patch.object(showrunners, "read_sources", wraps=showrunners.read_sources) as productions:
                    parent = staff_sources.unit_parent("models", unit.id)
                self.assertEqual(parent, staff_sources.UnitProduction(production_name, str(doc)))
                self.assertEqual(members[parent.name], expected_parent)
                recorded_unit = record.by_id(unit.id)
                assert recorded_unit is not None
                self.assertEqual(staff.ReportsTo(members[parent.name].id), recorded_unit.parent)
                self.assertEqual(snapshot.call_count, 1)
                self.assertEqual(launches.call_count, 0)
                self.assertEqual(productions.call_count, 0)


    def test_member_with_unresolved_or_wrong_kind_id_does_not_take_an_unrelated_name(self) -> None:
        showrunner, _unit, worker = self.tree()
        _ = self.production("sdlc", "models")
        selector = staff_sources.UnitMember("sdlc", "models")
        for staff_id in (fixture_runtime_id("missing-member"), showrunner.id, worker.id):
            with self.subTest(staff_id=staff_id):
                self.pane("w1:p2", session_id=staff_id, environment={ ('' + 'SHOWRUNNER_' + 'UNIT'): "sdlc", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "models",
                })
                self.assertEqual(session_staff.unit_match(selector, staff.snapshot(), session_staff.StoredReference(staff_id),
                    parent=session_staff.RequiredParent(showrunner)), staff_sources.NoSuchMember(selector))


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
        moved = staff.move(unit.id, staff.ReportsTo(new.id))
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
        self.write_json(invalid, {"messages": [42]})
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


class NonLocalMemberReferenceTests(unittest.TestCase):
    def test_named_member_ignores_nonlocal_launch_labels(self) -> None:
        from scripts.production import session_staff

        parent_id = staff.StaffId(fixture_runtime_id("nonlocal-parent"))
        unit_id = staff.StaffId(fixture_runtime_id("nonlocal"))
        worker_id = staff.StaffId(fixture_runtime_id("nonlocal-worker"))
        other_id = staff.StaffId(fixture_runtime_id("nonlocal-other-parent"))
        moved_id = staff.StaffId(fixture_runtime_id("nonlocal-moved"))
        parent = staff.StaffRow(parent_id, "showrunner", "demo", staff.StaffRoot(), current_session=parent_id)
        unit = staff.StaffRow(unit_id, "unit", "alpha", staff.ReportsTo(parent_id), current_session=unit_id)
        worker = staff.StaffRow(worker_id, "worker", "impl", staff.ReportsTo(unit_id), current_session=worker_id)
        other = staff.StaffRow(other_id, "showrunner", "other", staff.StaffRoot(), current_session=other_id)
        moved = staff.StaffRow(moved_id, "unit", "alpha", staff.ReportsTo(other_id), current_session=moved_id)
        record = staff.StaffSnapshot((parent, unit, worker, other, moved))
        selector = session_staff.UnitMember("demo", "alpha")
        missing = fixture_runtime_id("nonlocal-unregistered-session")
        for reference in (missing, worker.id, parent.id, moved.id):
            with self.subTest(reference=reference):
                # A named request uses the recorded parent and name.
                launch: pane_record.PaneLaunch = {
                    "pane_id": "w1:p1", "session_id": reference,
                    "config_dir": "/fixture/claude", "cwd": "/fixture/unit",
                    "name": "alpha", "model": "", "remote_control": False,
                    "environment": {('' + 'SHOWRUNNER_' + 'UNIT_ID'): "alpha"},
                    "flags_read": True, "written_at": "2026-11-01T12:00:00Z",
                }
                sources = session_staff.MemberSources((launch,))
                with mock.patch.object(staff, "snapshot", side_effect=AssertionError("unexpected staff read")):
                    found = sources.member(selector, record)
                expected = session_staff.MemberFound(unit)
                self.assertEqual(found, expected)



class StaffWorkerIdentityTests(StaffTestCase):
    def test_observation_uses_current_staff_session_after_rename(self) -> None:
        runner = fixture_member("showrunner", name="runner")
        owner = fixture_member("unit", name="unit", parent=runner.id)
        worker = fixture_member("worker", name="impl", parent=owner.id)
        worker = staff.rename(worker.id, "review")
        before = self.staff_file.read_bytes()
        evidence = staff_sources.observe_workers(staff.snapshot(), MemberSources(()))
        self.assertEqual(len(evidence.workers), 1)
        observed = evidence.workers[0]
        self.assertEqual(observed.row, worker)
        self.assertEqual(observed.owner.match, owner)
        self.assertEqual(observed.identity, staff_sources.ClaudeSession(worker.current_session))
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_stopped_worker_has_no_current_observation(self) -> None:
        runner = fixture_member("showrunner", name="runner")
        owner = fixture_member("unit", name="unit", parent=runner.id)
        worker = fixture_member("worker", name="impl", parent=owner.id)
        evidence = staff_sources.observe_workers(staff.snapshot(), MemberSources(()), current_only=True)
        self.assertEqual(evidence.workers, ())
        staff_sources.require_worker_stopped(staff.snapshot(), worker.id)

    def test_retire_stopped_worker_prints_an_anomaly_for_another_codex_process(self) -> None:
        from scripts.message import process_table

        runner, owner, worker = self.tree()
        launch = pane_record.launch("test:pane", owner.current_session, str(self.root), {}, [])
        owner = replace(owner, home=staff.SessionHome(Path.home() / ".claude"))
        table = {42401: process_table.Process(42401, 1, "codex", "codex resume")}
        with (mock.patch.object(process_table, "snapshot", return_value=table),
              mock.patch.object(herdr, "process_environment", return_value={"HERDR_PANE_ID": "test:pane"}),
              mock.patch.object(session_staff, "member_sources", return_value=MemberSources((launch,))),
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(staff.retire(worker.id), worker)
        self.assertIsNone(staff.by_id(worker.id))
        self.assertEqual(staff.snapshot().rows, tuple(sorted((runner, owner), key=lambda row: row.id)))
        self.assertEqual(errors.getvalue().splitlines(), [
            "anomaly: staff sources: Codex process 42401 in pane test:pane: cannot read its thread; staff status may be incomplete"])
