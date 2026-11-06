"""Status discovery retains healthy units when process evidence is incomplete."""
from __future__ import annotations

from contextlib import redirect_stderr
from dataclasses import replace
from io import StringIO
import json
import os
from pathlib import Path
from unittest import mock

from scripts.lib import unit_runs
from scripts.lib.atomic_file import write_text_atomic
from scripts.production import session_staff as labels, staff
from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_runtime_id, fixture_worker, unit_membership
import herdr
from pane_launch import PaneLaunch


class StatusDiscoveryTests(StaffTestCase):
    def test_pane_parents_selects_only_local_pane_workers_from_the_record(self) -> None:
        config, runs = self.root / "config", self.root / "runs"
        config.mkdir()
        write_text_atomic(config / "direct.conf", "# one of: mesh, herdr\nWORKER_TRANSPORT=herdr\n")
        runner = fixture_member("showrunner", name="pane-show", doc=staff.LiveDoc("natedev", self.root / "show.md"))
        selected = fixture_member("unit", name="pane", parent=runner.id,
                                  worktree=staff.PresentWorktree("natedev", self.root / "pane"))
        mesh = fixture_member("unit", name="mesh", parent=runner.id,
                              worktree=staff.PresentWorktree("natedev", self.root / "mesh"))
        other = fixture_member("unit", name="other-machine", parent=runner.id,
                               worktree=staff.PresentWorktree("mac", self.root / "other"))
        _ = fixture_member("unit", name="no-workers", parent=runner.id,
                           worktree=staff.PresentWorktree("natedev", self.root / "empty"))
        for parent in (runner, selected, mesh, other):
            _ = fixture_worker("impl", parent.id)
            if parent == runner:
                continue
            run = runs / parent.name
            run.mkdir(parents=True)
            write_text_atomic(run / unit_runs.STATE_FILE, json.dumps({unit_runs.OWNER_FIELD: parent.id}))
            write_text_atomic(run / "worker_transport", "mesh\n" if parent == mesh else "herdr\n")
        _ = fixture_worker("test", selected.id)
        record = staff.snapshot()
        before = self.staff_state()
        with (mock.patch.object(unit_runs, "RUN_ROOT", runs),
              mock.patch.object(unit_runs, "FORMER_RUN_ROOT", self.root / "former"),
              mock.patch.dict(os.environ, {"CONF_DIR": str(config), "ROSTER_RUN_ROOT": str(runs)}),
              mock.patch.object(unit_runs, "held_by_session", wraps=unit_runs.held_by_session) as scans):
            self.assertEqual({row.id for row in labels.pane_parents(record)}, {runner.id, selected.id})
            self.assertEqual(scans.call_count, 1)
        self.assertEqual(self.staff_state(), before)

    def test_unknown_process_identity_preserves_healthy_unit_status(self) -> None:
        import unit_lookup

        broken = unit_membership("show", "broken")
        healthy = unit_membership("show", "healthy")
        place = labels.Place("agents", "w1:p1")
        launch = PaneLaunch(pane_id=place.pane, session_id=broken.current_session, config_dir="", cwd="",
                            name="", model="", remote_control=False, environment={}, flags_read=True, written_at="")
        for marks in ({}, {"CLAUDE_CODE_SESSION_ID": "unregistered"}):
            with self.subTest(marks=marks):
                processes = [herdr.HerdrProcess(910001, place, marks, "claude", ""),
                             herdr.HerdrProcess(910002, labels.Place("agents", "w2:p1"),
                                               {"CLAUDE_CODE_SESSION_ID": healthy.current_session}, "zsh", "")]
                errors = StringIO()
                with mock.patch.object(herdr, "live_sessions", return_value=[{"name": "agents"}]), redirect_stderr(errors):
                    observed = tuple(unit_lookup._herdr_labeled({}, unit_lookup.UnreadAt({}, unit_lookup.RecordsRead()), processes))  # pyright: ignore[reportPrivateUsage]
                    grouped = labels.group_labeled_sessions(observed, record=staff.snapshot(),
                                                            sources=labels.MemberSources((launch,)))
                self.assertEqual(len(errors.getvalue().splitlines()), 1)
                self.assertIn("anomaly:", errors.getvalue())
                states = grouped.status_units("show")
                self.assertIsInstance(states["broken"].claude, labels.ClaudeUnknown)
                self.assertIsInstance(states["healthy"].claude, labels.ClaudeNotRunning)

    def test_agent_children_select_the_agent_without_an_anomaly(self) -> None:
        row = unit_membership("show", "unit")
        row = staff.update_current_session(row.current_session, staff.native_id(fixture_runtime_id("claim-after-clear")))
        place = labels.Place("agents", "w1:p1")
        children = [labels.LabeledSession("", labels.ClaudeUnknown("child process"), place,
                    labels.ObservedProcess(910001 + index, command), labels.LiveSession(row.current_session))
                    for index, command in enumerate(("bevy_brp_mcp", "zsh", "python3", "sleep", "sh", "codex-code-mode-host"))]
        for command in ("claude", "codex"):
            with self.subTest(agent=command):
                agent = replace(children[0], process=labels.ObservedProcess(910099, command))
                errors = StringIO()
                with redirect_stderr(errors):
                    grouped = labels.group_labeled_sessions([*children, agent], record=staff.snapshot(),
                                                            sources=labels.MemberSources(()))
                self.assertEqual(errors.getvalue(), "")
                self.assertEqual(grouped.units, {"show": {row.name: replace(agent, unit=row.name)}})


class WorkerRunDiscoveryTests(StaffTestCase):
    def test_run_index_keeps_the_owner_marker_without_a_worker_record(self) -> None:
        from scripts.lib import unit_runs

        owner = unit_membership("show", "owner")
        worker = fixture_worker("worker", owner.id)
        runs = self.root / "runs"
        run = runs / "marker-run"
        run.mkdir(parents=True)
        with (mock.patch.object(unit_runs, "run_roots", return_value=(runs,)),
              mock.patch.dict(os.environ, {"UNIT_RUN_DIR": "", "PLAN_DELEGATE_BOARD_DIR": ""})):
            unit_runs.activate(run, owner.current_session)
            record = staff.local_snapshot()
            found = labels.worker_run_index(record)
            entries = labels.worker_run_entries(record, (worker,))
        self.assertEqual(found[worker.id], (run,))
        self.assertNotIn(worker.id, entries)

    def test_release_finds_a_loaded_source_thread_through_the_owner_marker(self) -> None:
        from scripts.agents.test_codex_mesh import StubAppServer, select_fixture_workers
        from scripts.lib import unit_runs
        from scripts.shutdown import stop_resume
        from scripts.shutdown.stop_results import SourceReleased

        owner = unit_membership("show", "owner")
        home = Path.home()
        home.mkdir(parents=True, exist_ok=True)
        notes = self.root / "notes"
        select_fixture_workers(home, notes=notes)
        backend = staff.CodexBackend(home / ".codex")
        worker = fixture_worker("worker", owner.id, backend=backend)
        loaded = {worker.current_session}

        def reply(request: dict[str, object]) -> list[dict[str, object]]:
            result: dict[str, object] = {}
            match request.get("method"):
                case "initialize":
                    pass
                case "thread/loaded/list":
                    result = {"data": sorted(loaded), "nextCursor": None}
                case "thread/queue/list":
                    result = {"data": []}
                case "thread/read":
                    result = {"thread": {"id": worker.current_session, "status": {"type": "idle"}}}
                case "thread/unsubscribe":
                    self.assertEqual(request["params"], {"threadId": worker.current_session})
                    loaded.remove(worker.current_session)
                    result = {"status": "notLoaded"}
                case _:
                    self.fail(f"Unexpected server request: {request!r}")
            return [{"id": request["id"], "result": result}]

        server = StubAppServer(reply)
        self.addCleanup(server.close)
        runs = self.root / "runs"
        run = runs / "marker-run"
        self.write_json(run / "mesh_server.json", {"pid": os.getpid(), "port": server.port})
        with (mock.patch.object(unit_runs, "run_roots", return_value=(runs,)),
              mock.patch.dict(os.environ, {"UNIT_RUN_DIR": "", "PLAN_DELEGATE_BOARD_DIR": "", "AGENT_NOTES_DIR": str(notes)}),
              mock.patch.object(stop_resume, "stop", return_value=stop_resume.NotRunning(worker.current_session))):
            unit_runs.activate(run, owner.current_session)
            record = staff.local_snapshot()
            result = stop_resume.release_source(worker, lambda: labels.worker_run_index(record).get(worker.id, ()), backend)
        self.assertIsInstance(result, SourceReleased)
        self.assertFalse(loaded)
        self.assertEqual([request["method"] for request in server.requests].count("thread/unsubscribe"), 1)

    def test_run_index_reads_workers_without_writing_to_the_marker_folder(self) -> None:
        from scripts.lib import unit_runs
        import os

        owner = unit_membership("show", "owner")
        worker = fixture_worker("worker", owner.id)
        runs = self.root / "runs"
        run = runs / "worker-run"
        run.mkdir(parents=True)
        self.write_json(run / "mesh_roster.json", {worker.name: {"staff_id": worker.id, "status": "running"}})
        with (mock.patch.object(unit_runs, "run_roots", return_value=(runs,)),
              mock.patch.dict(os.environ, {"UNIT_RUN_DIR": "", "PLAN_DELEGATE_BOARD_DIR": ""})):
            unit_runs.activate(run, owner.current_session)
            markers = runs / "active"
            before = {path.name: path.read_bytes() for path in markers.iterdir()}
            found = labels.worker_run_index(staff.local_snapshot())
            after = {path.name: path.read_bytes() for path in markers.iterdir()}
        self.assertEqual(found[worker.id], (run,))
        self.assertEqual(after, before)

    def test_unreadable_owner_marker_reports_one_anomaly_and_keeps_other_workers_runs(self) -> None:
        from scripts.lib import unit_runs
        import os

        broken = unit_membership("show", "broken")
        healthy = unit_membership("show", "healthy")
        first = fixture_worker("broken-worker", broken.id)
        second = fixture_worker("healthy-worker", healthy.id)
        runs = self.root / "runs"
        run = runs / "healthy-run"
        run.mkdir(parents=True)
        self.write_json(run / "mesh_roster.json", {second.name: {"staff_id": second.id, "status": "running"}})
        errors = StringIO()
        with (mock.patch.object(unit_runs, "run_roots", return_value=(runs,)),
              mock.patch.dict(os.environ, {"UNIT_RUN_DIR": "", "PLAN_DELEGATE_BOARD_DIR": ""})):
            unit_runs.activate(run, healthy.current_session)
            _ = unit_runs.marker(broken.current_session).write_bytes(b"\xff")
            with redirect_stderr(errors):
                found = labels.worker_run_index(staff.local_snapshot())
        self.assertEqual(found[second.id], (run,))
        self.assertNotIn(first.id, found)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn(f"anomaly: {broken.name}:", errors.getvalue())
