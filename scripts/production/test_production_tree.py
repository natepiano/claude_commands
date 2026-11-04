"""The manager tree cargo-handler polls: every edge from fixture records, none from live state."""
from __future__ import annotations

from scripts.production.staff_test_support import fixture_member, fixture_runtime_id, fixture_worker, fixture_roster

import contextlib
import io
import json
import os
import resource
import subprocess
import sys
import time
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.lib.atomic_file import write_text_atomic
from scripts.production import staff, staff_sources
from scripts.usage import agents_log
from scripts.production.staff_test_support import StaffTestCase

sys.path.insert(0, str(Path(__file__).resolve().parent))
import production_tree  # noqa: E402
from scripts.production import showrunners
import showrunner_tree
import unit_lookup
from herdr import HerdrPlace
import unit_runs

RUNNER, CHILD_RUNNER, GONE_RUNNER = "17058649-2f0a-5cbf-b652-118ad9afdcf5", "25f9ce3d-b694-5281-a407-3e1369686426", "da46e5c2-e05e-5af0-b745-29e7288c841f"
DIRECTOR, OLD_DIRECTOR, ORPHAN = "db159174-782d-5cca-87c3-df4eadc6ee0f", "c748073f-6db4-5c9a-ab26-b7a847ba2e00", "06d7f3bf-8f51-5582-a8c7-d9fa5176a3ed"
SCRIPT = Path(__file__).with_name("production_tree.py")


def record(session_id: str) -> dict[str, object]:
    return {"sessionId": session_id}


def unit(session_id: str, name: str) -> unit_lookup.MarkedUnit:
    claude = unit_lookup.LiveClaude(name, session_id, f"/run/{name}.sock", 1)
    return unit_lookup.MarkedUnit(name, claude, HerdrPlace(name, "w1:p1"))


def server_recorded(run: Path) -> bool:
    """A fixture run's server counts as running while its record exists; no pid is checked."""
    return (run / "mesh_server.json").is_file()


class ProductionTreeTests(StaffTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        notifier, state = self.source_root / "notifier", self.source_root / "showrunners"
        rows: dict[str, staff.StaffRow] = {}
        for slug, session_id in (("main", RUNNER), ("child", CHILD_RUNNER), ("gone", GONE_RUNNER)):
            doc = self.production(slug, parent="main" if slug == "child" else "")
            conf = notifier / f"showrunner-{slug}" / "conf"
            target = showrunners.read_bound_source(conf.parent, self.source_root / f"{slug}-production.md").timer.target
            write_text_atomic(conf, conf.read_text().replace("TARGET=" + target, "TARGET=session:" + session_id))
            rows[slug] = fixture_member("showrunner", name=slug, parent=rows["main"].id if slug == "child" else None,
                                        runtime_id=session_id, doc=staff.LiveDoc("natedev", doc))
        _ = cast(object, self.enterContext(mock.patch.dict(
            os.environ, {"ASTRA_ROLES_STATE": str(self.root / "astra-roles"),
                         "USAGE_STATE_DIR": str(self.root / "usage"),
                         "DESIGN_ROLES_STATE": str(self.root / "design-roles"),
                         "DESIGN_OWNERSHIP_STATE": str(self.root / "design-ownership")})))
        for patch in (
            mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", notifier),
            mock.patch.object(showrunners, "SHOWRUNNER_STATE_DIR", state),
            mock.patch.object(unit_runs, "RUN_ROOT", self.root / "unit"),
            mock.patch.object(unit_runs, "FORMER_RUN_ROOT", self.root / "delegate"),
            mock.patch.object(production_tree, "_server_running", side_effect=server_recorded),
        ):
            _ = self.enterContext(patch)

    def director_row(self, runtime: str) -> staff.StaffRow:
        existing = staff.initial_snapshot().by_current_session(runtime)
        if existing is not None:
            return existing
        parent = staff.initial_snapshot().by_current_session(GONE_RUNNER if runtime == ORPHAN else RUNNER)
        assert parent is not None
        name = {DIRECTOR: "one", OLD_DIRECTOR: "two", ORPHAN: "orphan"}.get(runtime, "unit-" + runtime)
        return fixture_member("unit", name=name, parent=parent.id, runtime_id=runtime)

    def run_folder(self, root: str, director: str, roster: dict[str, dict[str, object]], server: bool = True,
                   name: str = "") -> Path:
        """A run `start-run` opened for `director`: its state names a history whose first event says so."""
        run = self.root / root / (name or f"run-{director}")
        run.mkdir(parents=True)
        history = self.root / "history" / f"{run.name}.jsonl"
        history.parent.mkdir(exist_ok=True)
        started = {"event_type": "run_started", "main_agent": {"family": "claude", "session_id": director}}
        _ = history.write_text(json.dumps(started) + "\n" + json.dumps({"event_type": "phase_started"}) + "\n")
        parent = self.director_row(director)
        _ = (run / "progress_history_state.json").write_text(json.dumps(
            {"history_file": str(history), "main_agent": {"family": "claude", "session_id": director},
             "production": "gone" if director == ORPHAN else "main", "unit": parent.name}))
        valid = {name: {**entry, "launcher_pid": os.getpid() if entry.get("status") == "running" else 0}
                 for name, entry in roster.items()
                 if entry.get("role") in (*agents_log.ROLES, "astra-designer", "astra-reviewer")}
        for entry in valid.values():
            role = entry.get("role")
            role = "review" if role == "astra-reviewer" else "impl" if role == "astra-designer" else role
            for task in agents_log.ROLES:
                if role == task:
                    agents_log.append(str(entry["thread_id"]), task, None, datetime.now(UTC))
                    break
        published = fixture_roster(cast(dict[str, object], valid), parent.id, self.root / "codex-home")
        published.update({name: entry for name, entry in roster.items() if name not in valid})
        _ = (run / "mesh_roster.json").write_text(json.dumps(published))
        if server:
            _ = (run / "mesh_server.json").write_text(json.dumps({"pid": 1}))
        return run

    def session_tree(self, running: set[str], units: dict[str, dict[str, unit_lookup.MarkedUnit]]) -> list[production_tree.Edge]:
        snapshot = staff.snapshot()
        for slug, members in units.items():
            parent = staff_sources.member(staff_sources.ShowrunnerMember(slug), snapshot)
            assert isinstance(parent, staff_sources.MemberFound)
            for name, member in members.items():
                assert isinstance(member.claude, unit_lookup.LiveClaude)
                existing = staff.initial_snapshot().by_current_session(member.claude.session_id)
                if existing is not None and existing.name != name:
                    _ = staff.rename(existing.id, name)
                _ = fixture_member("unit", name=name, parent=parent.row.id, runtime_id=member.claude.session_id)
        marked = unit_lookup.MarkedProductions(units, {})
        with mock.patch.object(production_tree, "running_sessions", return_value=[record(session_id) for session_id in running]), \
                mock.patch.object(unit_lookup, "every_marked_unit", return_value=marked):
            return production_tree.tree()

    def astra_role(self, role: str, thread: str, manager: str, session_dir: str = "", worker: str = "") -> None:
        parent = self.director_row(manager)
        row = fixture_worker(worker or "held-" + role, parent.id, runtime_id=thread,
                             backend=staff.CodexBackend(self.root / "codex-home"))
        roles = self.root / "astra-roles"
        roles.mkdir(exist_ok=True)
        _ = (roles / f"{row.id}.json").write_text(json.dumps({
            "staff_id": row.id, "held_at": "2026-10-20T20:00:00+00:00",
            "session_dir": session_dir or None, "worker": worker or None,
            "pid": None if worker else os.getpid(),
        }))
        with mock.patch.dict(os.environ, {"USAGE_STATE_DIR": str(self.root / "usage")}):
            agents_log.append(thread, "review" if role == "reviewer" else "impl", None, datetime.now(UTC))

    def test_transferred_live_unit_edge_uses_its_staff_parent_despite_the_old_launch_mark(self) -> None:
        for slug in ("natedev", "sdlc"):
            doc = self.production(slug)
            _ = fixture_member("showrunner", name=slug, doc=staff.LiveDoc("natedev", doc))
        parent = staff_sources.member(staff_sources.ShowrunnerMember("sdlc"), staff.snapshot())
        assert isinstance(parent, staff_sources.MemberFound)
        row = fixture_member("unit", name="one", parent=parent.row.id, runtime_id=DIRECTOR)
        other_parent = fixture_member("showrunner", name="other")
        _ = fixture_member("unit", name="one", parent=other_parent.id)
        self.pane("w1:p1", session_id=DIRECTOR, environment={
            "STAFF_ID": row.id, "SHOWRUNNER_UNIT": "natedev", "SHOWRUNNER_UNIT_ID": "one",
        })
        with (mock.patch.object(production_tree, "running_sessions", return_value=[record(DIRECTOR)]),
              mock.patch.object(unit_lookup, "observed_staff_marks", return_value=(
                  ("natedev", replace(unit(DIRECTOR, "one"), staff_id=row.id)),
              ))):
            edges = production_tree.tree()
        self.assertEqual(edges, [{"manager": parent.row.id, "member": DIRECTOR, "role": "unit"}])

    def test_complete_tree_with_child_productions_reads_one_staff_snapshot(self) -> None:
        with (mock.patch.object(staff, "local_snapshot", wraps=staff.local_snapshot) as reads,
              mock.patch.object(unit_lookup, "observed_staff_marks", return_value=()),
              mock.patch.object(production_tree, "running_sessions", return_value=[record(RUNNER), record(CHILD_RUNNER)])):
            edges = production_tree.tree()
        self.assertIn({"manager": RUNNER, "member": CHILD_RUNNER, "role": "showrunner"}, edges)
        self.assertEqual(reads.call_count, 1)

    def test_seed_and_json_follow_staff_moves_despite_stale_documents_and_parent_files(self) -> None:
        main, child, gone = (staff_sources.member(staff_sources.ShowrunnerMember(slug), staff.snapshot())
                             for slug in ("main", "child", "gone"))
        assert isinstance(main, staff_sources.MemberFound)
        assert isinstance(child, staff_sources.MemberFound)
        assert isinstance(gone, staff_sources.MemberFound)
        _ = staff.rename(main.row.id, "tools")
        document = self.source_root / "main-production.md"
        write_text_atomic(document, "# Fixture production\n- **Showrunner session:** `tools`\n")
        _ = self.worktree("one")
        director = fixture_member("unit", name="one", parent=main.row.id, runtime_id=DIRECTOR)
        self.pane("w1:p1", session_id=DIRECTOR, environment={
            "STAFF_ID": director.id, "SHOWRUNNER_UNIT": "main", "SHOWRUNNER_UNIT_ID": "one",
        })
        marked = unit_lookup.MarkedProductions({"main": {"one": unit(DIRECTOR, "one")}}, {})
        with (mock.patch.object(production_tree, "running_sessions", return_value=[record(CHILD_RUNNER), record(DIRECTOR)]),
              mock.patch.object(unit_lookup, "every_marked_unit", return_value=marked)):
            with contextlib.redirect_stdout(io.StringIO()) as before:
                self.assertEqual(production_tree.main(["--json"]), 0)
            expected = ('{"schema":1,"edges":[{"manager":"17058649-2f0a-5cbf-b652-118ad9afdcf5","member":"25f9ce3d-b694-5281-a407-3e1369686426","role":"showrunner"},'
                        '{"manager":"17058649-2f0a-5cbf-b652-118ad9afdcf5","member":"db159174-782d-5cca-87c3-df4eadc6ee0f","role":"unit"}]}\n')
            self.assertEqual(before.getvalue(), expected)
            parent_file = self.source_root / "showrunners/child/parent"
            write_text_atomic(parent_file, "obsolete-parent\n")
            with contextlib.redirect_stdout(io.StringIO()) as same:
                self.assertEqual(production_tree.main(["--json"]), 0)
            self.assertEqual(same.getvalue().encode(), before.getvalue().encode())
            write_text_atomic(parent_file, "main\n")
            _ = staff.move(child.row.id, staff.ReportsTo(gone.row.id))
            _ = staff.move(director.id, staff.ReportsTo(gone.row.id))
            with contextlib.redirect_stdout(io.StringIO()):
                staff_sources.seed()
            self.assertEqual(showrunner_tree.parent_of("child"), "gone")
            self.assertEqual(showrunner_tree.children_of("gone"), ["child"])
            with contextlib.redirect_stdout(io.StringIO()) as after:
                self.assertEqual(production_tree.main(["--json"]), 0)
            self.assertEqual(after.getvalue(), expected.replace('"manager":"17058649-2f0a-5cbf-b652-118ad9afdcf5"', '"manager":"da46e5c2-e05e-5af0-b745-29e7288c841f"'))
        self.assertEqual(staff.by_id(director.id), staff.StaffRow(director.id, "unit", "one", staff.ReportsTo(gone.row.id), current_session=director.id))
        self.assertEqual(parent_file.read_text(), "main\n")

    def test_astra_designer_and_reviewer_edges_carry_their_account_and_live_state(self) -> None:
        run = self.run_folder("unit", DIRECTOR, {
            "project-design": {"thread_id": "3d1d0e20-5129-55bd-9516-9b9e8d633777", "role": "astra-designer", "status": "idle"},
        })
        self.astra_role("designer", "3d1d0e20-5129-55bd-9516-9b9e8d633777", DIRECTOR, str(run), "project-design")
        self.astra_role("reviewer", "4f832112-25a1-5946-bf2c-52640f8daba4", RUNNER)
        edges = self.session_tree({RUNNER, DIRECTOR}, {"main": {"one": unit(DIRECTOR, "one")}})
        self.assertCountEqual(edges, [
            {"manager": RUNNER, "member": DIRECTOR, "role": "unit"},
            {"manager": DIRECTOR, "member": "3d1d0e20-5129-55bd-9516-9b9e8d633777", "role": "astra-designer",
             "codex_home": str(self.root / "codex-home"), "state": "idle"},
            {"manager": RUNNER, "member": "4f832112-25a1-5946-bf2c-52640f8daba4", "role": "astra-reviewer",
             "codex_home": str(self.root / "codex-home"), "state": "busy"},
        ])

    def test_an_astra_thread_has_one_edge_and_other_mesh_workers_keep_their_edges(self) -> None:
        run = self.run_folder("unit", DIRECTOR, {
            "project-impl": {"thread_id": "3d1d0e20-5129-55bd-9516-9b9e8d633777", "role": "astra-designer", "status": "running"},
            "project-test": {"thread_id": "6b21868b-7de5-5be5-840d-d5b38b69fe7e", "role": "test", "status": "running"},
        })
        self.astra_role("designer", "3d1d0e20-5129-55bd-9516-9b9e8d633777", DIRECTOR, str(run), "project-impl")
        self.assertEqual(production_tree.worker_edges({DIRECTOR}), [
            {"manager": DIRECTOR, "member": "6b21868b-7de5-5be5-840d-d5b38b69fe7e", "role": "test"},
        ])
        self.assertCountEqual(self.session_tree({DIRECTOR}, {}), [
            {"manager": DIRECTOR, "member": "3d1d0e20-5129-55bd-9516-9b9e8d633777", "role": "astra-designer",
             "codex_home": str(self.root / "codex-home"), "state": "busy"},
            {"manager": DIRECTOR, "member": "6b21868b-7de5-5be5-840d-d5b38b69fe7e", "role": "test"},
        ])

    def test_a_local_astra_thread_keeps_its_manager_on_another_machine(self) -> None:
        self.astra_role("reviewer", "59ce7961-14cb-5d77-9ac6-487205243a97", ORPHAN)
        self.assertEqual(self.session_tree(set(), {}), [
            {"manager": ORPHAN, "member": "59ce7961-14cb-5d77-9ac6-487205243a97", "role": "astra-reviewer",
             "codex_home": str(self.root / "codex-home"), "state": "busy"},
        ])

    def test_an_unreadable_astra_record_keeps_other_astra_and_claude_edges(self) -> None:
        self.astra_role("reviewer", "39bdc62f-55c5-5da7-a8ea-c7d94495798c", RUNNER)
        broken = self.root / "astra-roles" / "thread-broken.json"
        _ = broken.write_text("{not json")
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            edges = self.session_tree({RUNNER, CHILD_RUNNER}, {})
        self.assertCountEqual(edges, [
            {"manager": RUNNER, "member": CHILD_RUNNER, "role": "showrunner"},
            {"manager": RUNNER, "member": "39bdc62f-55c5-5da7-a8ea-c7d94495798c", "role": "astra-reviewer",
             "codex_home": str(self.root / "codex-home"), "state": "busy"},
        ])
        self.assertIn("thread-broken", errors.getvalue())

    def test_malformed_astra_records_keep_other_edges_and_json_output(self) -> None:
        self.astra_role("reviewer", "39bdc62f-55c5-5da7-a8ea-c7d94495798c", RUNNER)
        valid = cast(dict[str, object], json.loads(
            (self.root / "astra-roles" / "39bdc62f-55c5-5da7-a8ea-c7d94495798c.json").read_text()))
        for shape in ("missing-lifetime", "extra-state", "wrong-pid"):
            thread = f"thread-{shape}"
            record = {**valid, "thread_id": thread}
            if shape == "missing-lifetime":
                del record["session_dir"]
                del record["worker"]
            elif shape == "extra-state":
                record["state"] = "busy"
            else:
                record["pid"] = True
            _ = (self.root / "astra-roles" / f"{thread}.json").write_text(json.dumps(record))
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            edges = self.session_tree({RUNNER, CHILD_RUNNER}, {})
        self.assertCountEqual(edges, [
            {"manager": RUNNER, "member": CHILD_RUNNER, "role": "showrunner"},
            {"manager": RUNNER, "member": "39bdc62f-55c5-5da7-a8ea-c7d94495798c", "role": "astra-reviewer",
             "codex_home": str(self.root / "codex-home"), "state": "busy"},
        ])
        self.assertEqual(len(errors.getvalue().splitlines()), 3)
        for shape in ("missing-lifetime", "extra-state", "wrong-pid"):
            self.assertIn(f"thread-{shape}.json", errors.getvalue())
        with mock.patch.object(production_tree, "tree", return_value=edges), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(production_tree.main(["--json"]), 0)
        self.assertEqual(cast(object, json.loads(output.getvalue())), {"schema": 1, "edges": edges})

    def test_every_kind_of_edge_between_running_sessions(self) -> None:
        _ = self.run_folder("unit", DIRECTOR, {
            "a-impl": {"thread_id": "3e104a36-0fc6-5eb7-b30b-7f3aa8ecb79b", "role": "impl", "status": "running"},
            "a-test": {"thread_id": "6b21868b-7de5-5be5-840d-d5b38b69fe7e", "role": "test", "status": "idle"},
            "a-review": {"thread_id": "4f3dae8d-56f4-52d5-bf07-3722056834a4", "role": "review", "status": "ended"},
        })
        _ = self.run_folder("delegate", OLD_DIRECTOR, {
            "b-4": {"thread_id": "7ad49826-81fd-5789-96fa-32ce613c9deb", "role": "fix", "status": "done"},
        })
        edges = self.session_tree({RUNNER, CHILD_RUNNER, DIRECTOR, OLD_DIRECTOR}, {
            "main": {"one": unit(DIRECTOR, "one"), "two": unit(OLD_DIRECTOR, "two")},
        })
        self.assertEqual(edges, [
            {"manager": RUNNER, "member": CHILD_RUNNER, "role": "showrunner"},
            {"manager": RUNNER, "member": DIRECTOR, "role": "unit"},
            {"manager": RUNNER, "member": OLD_DIRECTOR, "role": "unit"},
            {"manager": DIRECTOR, "member": "3e104a36-0fc6-5eb7-b30b-7f3aa8ecb79b", "role": "impl"},
            {"manager": DIRECTOR, "member": "6b21868b-7de5-5be5-840d-d5b38b69fe7e", "role": "test"},
            {"manager": OLD_DIRECTOR, "member": "7ad49826-81fd-5789-96fa-32ce613c9deb", "role": "fix"},
        ])

    def test_record_assignment_wins_over_a_conflicting_name_suffix(self) -> None:
        _ = self.run_folder("unit", DIRECTOR, {
            "project-impl": {"thread_id": "4f3dae8d-56f4-52d5-bf07-3722056834a4", "role": "review", "status": "done"},
            "no-job-in-name": {"thread_id": "6b21868b-7de5-5be5-840d-d5b38b69fe7e", "role": "test", "status": "idle"},
        })
        self.assertCountEqual(production_tree.worker_edges({DIRECTOR}), [
            {"manager": DIRECTOR, "member": "4f3dae8d-56f4-52d5-bf07-3722056834a4", "role": "review"},
            {"manager": DIRECTOR, "member": "6b21868b-7de5-5be5-840d-d5b38b69fe7e", "role": "test"},
        ])

    def test_legacy_and_bad_assignments_never_gain_a_role_from_the_name(self) -> None:
        _ = self.run_folder("unit", DIRECTOR, {
            "legacy-impl": {"thread_id": "f00b18f5-6e1c-5015-ad12-7540857e0935", "status": "idle"},
            "invalid-review": {"thread_id": "f5f7271b-1bd1-516c-9ef9-ac8d0d8b066a", "status": "idle", "role": "designer"},
        })
        self.assertEqual(production_tree.worker_edges({DIRECTOR}), [])
        self.assertEqual(staff.find(kind="worker"), [])


    def test_a_manager_on_another_machine_keeps_its_edges(self) -> None:
        _ = self.run_folder("unit", DIRECTOR, {"a-impl": {"thread_id": "3e104a36-0fc6-5eb7-b30b-7f3aa8ecb79b", "role": "impl", "status": "running"}}, server=False)
        _ = self.run_folder("unit", ORPHAN, {"c-impl": {"thread_id": "ecd6bfc9-3836-550d-94d8-f5fb6cfb0c72", "role": "impl", "status": "running"}})
        edges = self.session_tree({CHILD_RUNNER, DIRECTOR, ORPHAN}, {
            "main": {"one": unit(DIRECTOR, "one")},
            "gone": {"orphan": unit(ORPHAN, "orphan")},
        })
        # Neither showrunner runs here, yet each timer names its session, so its child and its
        # units keep their manager; the director's server has stopped, so its worker is gone.
        self.assertEqual(edges, [
            {"manager": RUNNER, "member": CHILD_RUNNER, "role": "showrunner"},
            {"manager": GONE_RUNNER, "member": ORPHAN, "role": "unit"},
            {"manager": RUNNER, "member": DIRECTOR, "role": "unit"},
            {"manager": ORPHAN, "member": "ecd6bfc9-3836-550d-94d8-f5fb6cfb0c72", "role": "impl"},
        ])

    def test_a_member_that_is_not_running_here_leaves_its_edges_out(self) -> None:
        edges = self.session_tree({RUNNER}, {"main": {"one": unit(DIRECTOR, "one")}})
        self.assertEqual(edges, [])

    def test_unreadable_staff_marks_keep_the_other_edges(self) -> None:
        with mock.patch.object(production_tree, "running_sessions", return_value=[record(RUNNER), record(CHILD_RUNNER)]), \
                mock.patch.object(unit_lookup, "every_marked_unit", side_effect=OSError("herdr: gone")), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            edges = production_tree.tree()
        self.assertEqual(edges, [{"manager": RUNNER, "member": CHILD_RUNNER, "role": "showrunner"}])
        self.assertIn("unit marks unreadable: herdr: gone", errors.getvalue())

    def test_unreadable_staff_reports_the_error_and_omits_edges(self) -> None:
        _ = self.run_folder("unit", DIRECTOR, {
            "unit-impl": {"thread_id": "3e104a36-0fc6-5eb7-b30b-7f3aa8ecb79b", "role": "impl", "status": "running"},
        })
        _ = self.write_document({"schema": 99, "rows": []})
        marked = unit_lookup.MarkedProductions({"main": {"one": unit(DIRECTOR, "one")}}, {})
        with (mock.patch.object(production_tree, "running_sessions",
                                return_value=[record(CHILD_RUNNER), record(DIRECTOR)]),
              mock.patch.object(unit_lookup, "every_marked_unit", return_value=marked),
              contextlib.redirect_stderr(io.StringIO()) as errors):
            edges = production_tree.tree()
        self.assertEqual(edges, [])
        self.assertIn("staff unreadable", errors.getvalue())

    def test_claude_workers_take_their_recorded_role_without_a_server(self) -> None:
        run = self.run_folder("unit", DIRECTOR, {}, server=False)
        _ = (run / "board.log").write_text("".join(f"2026-10-16T09:00:00-0400 {line}\n" for line in (
            "[impl] register: impl launcher up (claude/opus:high); mesh=proj-impl; role=impl",
            "[test] register: test launcher up (claude/opus:high); mesh=proj-test; role=test",
            "[review] register: review launcher up (claude/opus:high); mesh=proj-review; role=review",
            "[test] handoff: role=impl writing the second half",
            "[stray] register: hello, no role yet",
        )))
        for slot, worker, family in (
            ("impl", "f6613ac7-5256-526f-8325-c8fb23406331", "claude"),
            ("review", "cc792405-70ac-5b76-b106-e7f699a1e22e", "claude"),
            ("fix", "eb095a46-56c5-5f8f-81ca-5f13dbb75aed", "codex"),
        ):
            _ = (run / f"impl_worker_{slot}.json").write_text(json.dumps(
                {"name": f"proj-{slot}", "slot": slot, "family": family, "model": "m", "worker_id": worker}))
        # The test worker has no launch record yet: its ledger short id prefixes its session id, and
        # its name ends with its slot, so its role is the one its slot took over on the board.
        _ = (run / "workers").write_text("sid-claude-t\tproj-test\nsid-claude-s\tproj-stray\nsid-nobody\tproj-impl\n")
        parent = self.director_row(DIRECTOR)
        for name, identity in (("proj-impl", "f6613ac7-5256-526f-8325-c8fb23406331"),
                               ("proj-test", "d8d82cfe-b022-5df1-bb36-a4188171be51")):
            _ = fixture_worker(name, parent.id, runtime_id=identity)
        edges = self.session_tree({DIRECTOR, "f6613ac7-5256-526f-8325-c8fb23406331", "d8d82cfe-b022-5df1-bb36-a4188171be51", "1e3dd2f3-a20c-5098-a8a1-8a15e3e0c901", "eb095a46-56c5-5f8f-81ca-5f13dbb75aed"}, {})
        self.assertCountEqual(edges, [
            {"manager": DIRECTOR, "member": "f6613ac7-5256-526f-8325-c8fb23406331", "role": "impl"},
            {"manager": DIRECTOR, "member": "d8d82cfe-b022-5df1-bb36-a4188171be51", "role": "impl"},
        ])

    def test_a_run_with_both_families_lists_codex_and_claude_workers(self) -> None:
        run = self.run_folder("unit", DIRECTOR, {"proj-impl": {"thread_id": "3e104a36-0fc6-5eb7-b30b-7f3aa8ecb79b", "role": "impl", "status": "running"}})
        _ = (run / "board.log").write_text("2026-10-16T09:00:00-0400 [fix] register: fix launcher up; role=fix\n")
        _ = (run / "impl_worker_fix.json").write_text(json.dumps(
            {"name": "proj-fix", "slot": "fix", "family": "claude", "model": "m", "worker_id": "a4c70871-c34e-5bd2-a0b9-c090bdda3993"}))
        _ = (run / "workers").write_text("sid-clau\tproj-fix\n")
        _ = fixture_worker("proj-fix", self.director_row(DIRECTOR).id,
                           runtime_id="a4c70871-c34e-5bd2-a0b9-c090bdda3993")
        edges = self.session_tree({DIRECTOR, "a4c70871-c34e-5bd2-a0b9-c090bdda3993"}, {})
        self.assertEqual(edges, [
            {"manager": DIRECTOR, "member": "3e104a36-0fc6-5eb7-b30b-7f3aa8ecb79b", "role": "impl"},
            {"manager": DIRECTOR, "member": "a4c70871-c34e-5bd2-a0b9-c090bdda3993", "role": "fix"},
        ])

    def test_a_run_without_a_worker_ledger_has_no_claude_workers(self) -> None:
        run = self.run_folder("unit", DIRECTOR, {}, server=False)
        _ = (run / "board.log").write_text("2026-10-16T09:00:00-0400 [impl] register: impl launcher up; role=impl\n")
        _ = (run / "impl_worker_impl.json").write_text(json.dumps(
            {"name": "proj-impl", "slot": "impl", "family": "claude", "model": "m", "worker_id": "f6613ac7-5256-526f-8325-c8fb23406331"}))
        self.assertEqual(self.session_tree({DIRECTOR, "f6613ac7-5256-526f-8325-c8fb23406331"}, {}), [])

    def test_runs_side_by_side_under_one_director_each_list_their_workers(self) -> None:
        # A director's marker names one run; the phase it runs beside that one has its own folder.
        main = self.run_folder("delegate", DIRECTOR, {"p1-impl": {"thread_id": "401389bd-5153-584d-a320-e805d51cd742", "role": "impl", "status": "running"}},
                               name="run-phase-1")
        _ = self.run_folder("delegate", DIRECTOR, {"p2-fix": {"thread_id": "2f462ef2-a6e8-5e0e-99b2-89a49dcf81d9", "role": "fix", "status": "running"}},
                            name="run-phase-2")
        (self.root / "delegate" / "active").mkdir()
        _ = (self.root / "delegate" / "active" / DIRECTOR).write_text(f"{main}\n")
        edges = self.session_tree({DIRECTOR}, {})
        self.assertCountEqual(edges, [
            {"manager": DIRECTOR, "member": "401389bd-5153-584d-a320-e805d51cd742", "role": "impl"},
            {"manager": DIRECTOR, "member": "2f462ef2-a6e8-5e0e-99b2-89a49dcf81d9", "role": "fix"},
        ])

    def test_the_staff_parent_keeps_ownership_after_a_run_history_change(self) -> None:
        run = self.run_folder("unit", DIRECTOR, {"a-impl": {"thread_id": "3e104a36-0fc6-5eb7-b30b-7f3aa8ecb79b", "role": "impl", "status": "running"}})
        # A pass opened inside the worker rewrote the state's identity with the worker's own thread.
        _ = (run / "progress_history_state.json").write_text(json.dumps({
            "history_file": str(self.root / "history" / f"{run.name}.jsonl"),
            "main_agent": {"family": "codex", "session_id": "3e104a36-0fc6-5eb7-b30b-7f3aa8ecb79b"},
        }))
        edges = self.session_tree({DIRECTOR, "3e104a36-0fc6-5eb7-b30b-7f3aa8ecb79b"}, {})
        self.assertEqual(edges, [{"manager": DIRECTOR, "member": "3e104a36-0fc6-5eb7-b30b-7f3aa8ecb79b", "role": "impl"}])

    def test_the_recorded_director_names_a_run_opened_without_start_run(self) -> None:
        run = self.run_folder("unit", DIRECTOR, {"a-impl": {"thread_id": "07538736-7b35-5555-9dee-95eaad4b8b17", "role": "impl", "status": "running"}},
                              name="run-by-hand")
        (run / "progress_history_state.json").unlink()
        expected = [{"manager": DIRECTOR, "member": "07538736-7b35-5555-9dee-95eaad4b8b17", "role": "impl"}]
        self.assertEqual(self.session_tree({DIRECTOR}, {}), expected)
        self.assertEqual(self.session_tree({DIRECTOR}, {}), [{"manager": DIRECTOR, "member": "07538736-7b35-5555-9dee-95eaad4b8b17", "role": "impl"}])

    def test_staff_ownership_keeps_edges_without_a_started_history(self) -> None:
        no_state = self.run_folder("unit", DIRECTOR, {"a-impl": {"thread_id": "07538736-7b35-5555-9dee-95eaad4b8b17", "role": "impl", "status": "running"}},
                                   name="run-no-state")
        (no_state / "progress_history_state.json").unlink()
        gone = self.run_folder("unit", DIRECTOR, {"b-impl": {"thread_id": "63408ac4-90a9-5afc-96b7-e8f68a09a012", "role": "impl", "status": "running"}},
                               name="run-history-gone")
        (self.root / "history" / f"{gone.name}.jsonl").unlink()
        unstarted = self.run_folder("unit", DIRECTOR, {"c-impl": {"thread_id": "1cdbe0ba-3440-5e08-baf9-59d316123f26", "role": "impl", "status": "running"}},
                                    name="run-unstarted")
        _ = (self.root / "history" / f"{unstarted.name}.jsonl").write_text(json.dumps({"event_type": "phase_started"}) + "\n")
        _ = (self.root / "unit" / "stray-file").write_text("not a run\n")
        self.assertCountEqual(self.session_tree({DIRECTOR}, {}), [
            {"manager": DIRECTOR, "member": identity, "role": "impl"}
            for identity in ("07538736-7b35-5555-9dee-95eaad4b8b17", "63408ac4-90a9-5afc-96b7-e8f68a09a012", "1cdbe0ba-3440-5e08-baf9-59d316123f26")
        ])

    def test_a_large_tree_answers_well_under_a_second(self) -> None:
        directors = [fixture_runtime_id(f"large-tree-director:{number}") for number in range(40)]
        for director in directors:
            _ = self.run_folder("unit", director, {
                f"w{seat}-impl": {"thread_id": fixture_runtime_id(f"large-tree-worker:{director}:{seat}"), "role": "impl", "status": "running"} for seat in range(6)
            })
        running = {RUNNER, *directors}
        units = {"main": {f"unit{number}": unit(director, f"unit{number}")
                          for number, director in enumerate(directors)}}
        _ = self.session_tree(running, units)
        started = time.process_time()
        with mock.patch.object(production_tree, "running_sessions", return_value=[record(identity) for identity in running]), \
                mock.patch.object(unit_lookup, "every_marked_unit", return_value=unit_lookup.MarkedProductions(units, {})):
            edges = production_tree.tree()
        self.assertLess(time.process_time() - started, 0.25)
        self.assertEqual(len(edges), 40 + 40 * 6)

    def test_the_command_prints_schema_one_and_starts_fast(self) -> None:
        with mock.patch.object(production_tree, "tree", return_value=[]), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(production_tree.main(["--json"]), 0)
        self.assertEqual(cast(object, json.loads(out.getvalue())), {"schema": 1, "edges": []})
        # The child's CPU time, not the wall clock: on a loaded machine waiting for a CPU adds only to the clock.
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        shown = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, check=False,
                               env={**os.environ, "NOTIFIER_STATE_DIR": str(self.root / "notifier")}, stdin=subprocess.DEVNULL)
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertLess((after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime), 0.75)


if __name__ == "__main__":
    _ = unittest.main()
