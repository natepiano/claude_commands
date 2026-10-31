"""The manager tree cargo-handler polls: every edge from fixture records, none from live state."""
from __future__ import annotations

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
from pathlib import Path
from typing import cast, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.lib.atomic_file import write_text_atomic
from scripts.production import staff, staff_sources
from scripts.production.staff_test_support import StaffTestCase

sys.path.insert(0, str(Path(__file__).resolve().parent))
import production_tree  # noqa: E402
from scripts.production import showrunners
import showrunner_tree
import unit_lookup
from herdr import HerdrPlace
import unit_runs
import roster

RUNNER, CHILD_RUNNER, GONE_RUNNER = "sid-runner", "sid-child-runner", "sid-gone-runner"
DIRECTOR, OLD_DIRECTOR, ORPHAN = "sid-director", "sid-old-director", "sid-orphan"
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
            _ = self.production(slug, parent="main" if slug == "child" else "")
            conf = notifier / f"showrunner-{slug}" / "conf"
            write_text_atomic(conf, conf.read_text().replace(f"session:session-{slug}", f"session:{session_id}"))
            rows[slug] = staff.ensure("showrunner", name=slug, parent=rows["main"].id if slug == "child" else None)
        _ = cast(object, self.enterContext(mock.patch.dict(
            os.environ, {"ASTRA_ROLES_STATE": str(self.root / "astra-roles"),
                         "DESIGN_ROLES_STATE": str(self.root / "design-roles"),
                         "DESIGN_OWNERSHIP_STATE": str(self.root / "design-ownership")})))
        for patch in (
            mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", notifier),
            mock.patch.object(showrunners, "SHOWRUNNER_STATE_DIR", state),
            mock.patch.object(unit_runs, "RUN_ROOT", self.root / "unit"),
            mock.patch.object(unit_runs, "FORMER_RUN_ROOT", self.root / "delegate"),
            mock.patch.object(production_tree, "_server_running", side_effect=server_recorded),
            mock.patch.object(production_tree, "pool_root", return_value=self.root / "review-pool"),
        ):
            _ = self.enterContext(patch)

    def run_folder(self, root: str, director: str, roster: dict[str, dict[str, object]], server: bool = True,
                   name: str = "") -> Path:
        """A run `start-run` opened for `director`: its state names a history whose first event says so."""
        run = self.root / root / (name or f"run-{director}")
        run.mkdir(parents=True)
        history = self.root / "history" / f"{run.name}.jsonl"
        history.parent.mkdir(exist_ok=True)
        started = {"event_type": "run_started", "main_agent": {"family": "claude", "session_id": director}}
        _ = history.write_text(json.dumps(started) + "\n" + json.dumps({"event_type": "phase_started"}) + "\n")
        _ = (run / "progress_history_state.json").write_text(json.dumps(
            {"history_file": str(history), "main_agent": {"family": "claude", "session_id": director}}))
        _ = (run / "mesh_roster.json").write_text(json.dumps(roster))
        if server:
            _ = (run / "mesh_server.json").write_text(json.dumps({"pid": 1}))
        return run

    def session_tree(self, running: set[str], units: dict[str, dict[str, unit_lookup.MarkedUnit]]) -> list[production_tree.Edge]:
        snapshot = staff.snapshot()
        for slug, members in units.items():
            parent = staff_sources.member(staff_sources.ShowrunnerMember(slug), snapshot)
            assert isinstance(parent, staff_sources.MemberFound)
            for name in members:
                _ = staff.ensure("unit", name=name, parent=parent.row.id)
        marked = unit_lookup.MarkedProductions(units, {})
        with mock.patch.object(production_tree, "running_sessions", return_value=[record(session_id) for session_id in running]), \
                mock.patch.object(unit_lookup, "every_marked_unit", return_value=marked):
            return production_tree.tree()

    def astra_role(self, role: str, thread: str, manager: str, session_dir: str = "", worker: str = "") -> None:
        roles = self.root / "astra-roles"
        roles.mkdir(exist_ok=True)
        _ = (roles / f"{thread}.json").write_text(json.dumps({
            "role": role, "thread_id": thread, "codex_home": str(self.root / "codex-home"),
            "manager": manager, "held_at": "2026-10-20T20:00:00+00:00",
            "session_dir": session_dir or None, "worker": worker or None,
            "pid": None if worker else os.getpid(),
        }))

    def test_transferred_live_unit_edge_uses_its_staff_parent_despite_the_old_launch_mark(self) -> None:
        for slug in ("natedev", "sdlc"):
            _ = self.production(slug)
            _ = staff.ensure("showrunner", name=slug)
        parent = staff_sources.member(staff_sources.ShowrunnerMember("sdlc"), staff.snapshot())
        assert isinstance(parent, staff_sources.MemberFound)
        row = staff.ensure("unit", name="one", parent=parent.row.id)
        other_parent = staff.ensure("showrunner", name="other")
        _ = staff.ensure("unit", name="one", parent=other_parent.id)
        self.pane("w1:p1", session_id=DIRECTOR, environment={
            "STAFF_ID": row.id, "SHOWRUNNER_UNIT": "natedev", "SHOWRUNNER_UNIT_ID": "one",
        })
        with (mock.patch.object(production_tree, "running_sessions", return_value=[record(DIRECTOR)]),
              mock.patch.object(unit_lookup, "observed_unit_marks", return_value=(
                  ("natedev", replace(unit(DIRECTOR, "one"), staff_id=row.id)),
              ))):
            edges = production_tree.tree()
        self.assertEqual(edges, [{"manager": "session-sdlc", "member": DIRECTOR, "role": "unit"}])

    def test_complete_tree_with_child_productions_reads_one_staff_snapshot(self) -> None:
        with (mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as reads,
              mock.patch.object(unit_lookup, "observed_unit_marks", return_value=()),
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
        self.units_doc(document, ("one",), workspace="tools")
        _ = self.worktree("one")
        director = staff.ensure("unit", name="one", parent=main.row.id)
        self.pane("w1:p1", session_id=DIRECTOR, environment={
            "STAFF_ID": director.id, "SHOWRUNNER_UNIT": "main", "SHOWRUNNER_UNIT_ID": "one",
        })
        marked = unit_lookup.MarkedProductions({"main": {"one": unit(DIRECTOR, "one")}}, {})
        with (mock.patch.object(production_tree, "running_sessions", return_value=[record(CHILD_RUNNER), record(DIRECTOR)]),
              mock.patch.object(unit_lookup, "every_marked_unit", return_value=marked)):
            with contextlib.redirect_stdout(io.StringIO()) as before:
                self.assertEqual(production_tree.main(["--json"]), 0)
            expected = ('{"schema":1,"edges":[{"manager":"sid-runner","member":"sid-child-runner","role":"showrunner"},'
                        '{"manager":"sid-runner","member":"sid-director","role":"unit"}]}\n')
            self.assertEqual(before.getvalue(), expected)
            parent_file = self.source_root / "showrunners/child/parent"
            write_text_atomic(parent_file, "obsolete-parent\n")
            with contextlib.redirect_stdout(io.StringIO()) as same:
                self.assertEqual(production_tree.main(["--json"]), 0)
            self.assertEqual(same.getvalue().encode(), before.getvalue().encode())
            write_text_atomic(parent_file, "main\n")
            _ = staff.move(child.row.id, gone.row.id)
            _ = staff.move(director.id, gone.row.id)
            with contextlib.redirect_stdout(io.StringIO()):
                staff_sources.seed()
            self.assertEqual(showrunner_tree.parent_of("child"), "gone")
            self.assertEqual(showrunner_tree.children_of("gone"), ["child"])
            with contextlib.redirect_stdout(io.StringIO()) as after:
                self.assertEqual(production_tree.main(["--json"]), 0)
            self.assertEqual(after.getvalue(), expected.replace('"manager":"sid-runner"', '"manager":"sid-gone-runner"'))
        self.assertEqual(staff.by_id(director.id), staff.StaffRow(director.id, "unit", "one", gone.row.id))
        self.assertEqual(parent_file.read_text(), "main\n")
        self.assertIn("| `one` |", document.read_text())

    def test_astra_designer_and_reviewer_edges_carry_their_account_and_live_state(self) -> None:
        run = self.run_folder("unit", DIRECTOR, {
            "project-design": {"thread_id": "thread-design", "role": "impl", "status": "idle"},
        })
        self.astra_role("designer", "thread-design", DIRECTOR, str(run), "project-design")
        self.astra_role("reviewer", "thread-reviewer", RUNNER)
        edges = self.session_tree({RUNNER, DIRECTOR}, {"main": {"one": unit(DIRECTOR, "one")}})
        self.assertCountEqual(edges, [
            {"manager": RUNNER, "member": DIRECTOR, "role": "unit"},
            {"manager": DIRECTOR, "member": "thread-design", "role": "astra-designer",
             "codex_home": str(self.root / "codex-home"), "state": "idle"},
            {"manager": RUNNER, "member": "thread-reviewer", "role": "astra-reviewer",
             "codex_home": str(self.root / "codex-home"), "state": "busy"},
        ])

    def test_an_astra_thread_has_one_edge_and_other_mesh_workers_keep_their_edges(self) -> None:
        run = self.run_folder("unit", DIRECTOR, {
            "project-impl": {"thread_id": "thread-design", "role": "impl", "status": "running"},
            "project-test": {"thread_id": "thread-test", "role": "test", "status": "running"},
        })
        self.astra_role("designer", "thread-design", DIRECTOR, str(run), "project-impl")
        self.assertEqual(production_tree.worker_edges({DIRECTOR}), [
            {"manager": DIRECTOR, "member": "thread-test", "role": "test"},
        ])
        self.assertCountEqual(self.session_tree({DIRECTOR}, {}), [
            {"manager": DIRECTOR, "member": "thread-design", "role": "astra-designer",
             "codex_home": str(self.root / "codex-home"), "state": "busy"},
            {"manager": DIRECTOR, "member": "thread-test", "role": "test"},
        ])

    def test_a_local_astra_thread_keeps_its_manager_on_another_machine(self) -> None:
        self.astra_role("reviewer", "thread-remote-manager", ORPHAN)
        self.assertEqual(self.session_tree(set(), {}), [
            {"manager": ORPHAN, "member": "thread-remote-manager", "role": "astra-reviewer",
             "codex_home": str(self.root / "codex-home"), "state": "busy"},
        ])

    def test_an_unreadable_astra_record_keeps_other_astra_and_claude_edges(self) -> None:
        self.astra_role("reviewer", "thread-readable", RUNNER)
        broken = self.root / "astra-roles" / "thread-broken.json"
        _ = broken.write_text("{not json")
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            edges = self.session_tree({RUNNER, CHILD_RUNNER}, {})
        self.assertCountEqual(edges, [
            {"manager": RUNNER, "member": CHILD_RUNNER, "role": "showrunner"},
            {"manager": RUNNER, "member": "thread-readable", "role": "astra-reviewer",
             "codex_home": str(self.root / "codex-home"), "state": "busy"},
        ])
        self.assertIn("thread-broken", errors.getvalue())

    def test_malformed_astra_records_keep_other_edges_and_json_output(self) -> None:
        self.astra_role("reviewer", "thread-readable", RUNNER)
        valid = cast(dict[str, object], json.loads(
            (self.root / "astra-roles" / "thread-readable.json").read_text()))
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
            {"manager": RUNNER, "member": "thread-readable", "role": "astra-reviewer",
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
            "a-impl": {"thread_id": "thread-impl", "role": "impl", "status": "running"},
            "a-test": {"thread_id": "thread-test", "role": "test", "status": "idle"},
            "a-review": {"thread_id": "thread-review", "role": "review", "status": "ended"},
        })
        _ = self.run_folder("delegate", OLD_DIRECTOR, {
            "b-4": {"thread_id": "thread-fix", "role": "fix", "status": "done"},
        })
        edges = self.session_tree({RUNNER, CHILD_RUNNER, DIRECTOR, OLD_DIRECTOR}, {
            "main": {"one": unit(DIRECTOR, "one"), "two": unit(OLD_DIRECTOR, "two")},
        })
        self.assertEqual(edges, [
            {"manager": RUNNER, "member": CHILD_RUNNER, "role": "showrunner"},
            {"manager": RUNNER, "member": DIRECTOR, "role": "unit"},
            {"manager": RUNNER, "member": OLD_DIRECTOR, "role": "unit"},
            {"manager": DIRECTOR, "member": "thread-impl", "role": "impl"},
            {"manager": DIRECTOR, "member": "thread-test", "role": "test"},
            {"manager": OLD_DIRECTOR, "member": "thread-fix", "role": "fix"},
        ])

    def test_record_assignment_wins_over_a_conflicting_name_suffix(self) -> None:
        _ = self.run_folder("unit", DIRECTOR, {
            "project-impl": {"thread_id": "thread-review", "role": "review", "status": "done"},
            "no-job-in-name": {"thread_id": "thread-test", "role": "test", "status": "idle"},
        })
        self.assertCountEqual(production_tree.worker_edges({DIRECTOR}), [
            {"manager": DIRECTOR, "member": "thread-review", "role": "review"},
            {"manager": DIRECTOR, "member": "thread-test", "role": "test"},
        ])

    def test_legacy_and_bad_assignments_never_gain_a_role_from_the_name(self) -> None:
        _ = self.run_folder("unit", DIRECTOR, {
            "legacy-impl": {"thread_id": "thread-old", "status": "idle"},
            "invalid-review": {"thread_id": "thread-bad", "status": "idle", "role": "designer"},
        })
        self.assertEqual(production_tree.worker_edges({DIRECTOR}), [])
        self.assertEqual(production_tree._worker_role({}), roster.AssignmentUnrecorded())  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(production_tree._worker_role({"role": "designer"}),  # pyright: ignore[reportPrivateUsage]
                         roster.AssignmentUnreadable("designer"))

    def test_each_leased_pool_reviewer_reports_to_its_running_unit_director(self) -> None:
        pool = self.root / "review-pool" / "main" / "pool.json"
        pool.parent.mkdir(parents=True)
        _ = pool.write_text(json.dumps({
            "generation": 1, "next_index": 5,
            "templates": {"code": {"thread_id": "thread-template"}},
            "reviewers": {
                "code1": {"type": "code", "thread_id": "thread-busy", "lease": {"unit": "one", "ordinal": 1},
                          "busy_pid": os.getpid()},
                "design2": {"type": "design", "thread_id": "thread-idle", "lease": {"unit": "one", "ordinal": 2},
                            "busy_pid": None},
                "code3": {"type": "code", "thread_id": "thread-free", "lease": None, "busy_pid": None},
                "code4": {"type": "code", "thread_id": "thread-stopped", "lease": {"unit": "two", "ordinal": 1},
                          "busy_pid": None},
            },
        }))
        edges = self.session_tree({RUNNER, DIRECTOR}, {"main": {"one": unit(DIRECTOR, "one"), "two": unit(OLD_DIRECTOR, "two")}})
        self.assertEqual([edge for edge in edges if edge["role"] == "review"], [
            {"manager": DIRECTOR, "member": "thread-busy", "role": "review", "unit": "one",
             "thread_id": "thread-busy", "state": "busy", "type": "code"},
            {"manager": DIRECTOR, "member": "thread-idle", "role": "review", "unit": "one",
             "thread_id": "thread-idle", "state": "idle", "type": "design"},
        ])

    def test_a_manager_on_another_machine_keeps_its_edges(self) -> None:
        _ = self.run_folder("unit", DIRECTOR, {"a-impl": {"thread_id": "thread-impl", "role": "impl", "status": "running"}}, server=False)
        _ = self.run_folder("unit", ORPHAN, {"c-impl": {"thread_id": "thread-orphan", "role": "impl", "status": "running"}})
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
            {"manager": ORPHAN, "member": "thread-orphan", "role": "impl"},
        ])

    def test_a_member_that_is_not_running_here_leaves_its_edges_out(self) -> None:
        edges = self.session_tree({RUNNER}, {"main": {"one": unit(DIRECTOR, "one")}})
        self.assertEqual(edges, [])

    def test_unreadable_unit_marks_keep_the_other_edges(self) -> None:
        with mock.patch.object(production_tree, "running_sessions", return_value=[record(RUNNER), record(CHILD_RUNNER)]), \
                mock.patch.object(unit_lookup, "every_marked_unit", side_effect=OSError("herdr: gone")), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            edges = production_tree.tree()
        self.assertEqual(edges, [{"manager": RUNNER, "member": CHILD_RUNNER, "role": "showrunner"}])
        self.assertIn("unit marks unreadable: herdr: gone", errors.getvalue())

    def test_unreadable_staff_omits_membership_edges_and_keeps_worker_edges(self) -> None:
        _ = self.run_folder("unit", DIRECTOR, {
            "unit-impl": {"thread_id": "thread-impl", "role": "impl", "status": "running"},
        })
        _ = self.write_document({"schema": 99, "rows": []})
        marked = unit_lookup.MarkedProductions({"main": {"one": unit(DIRECTOR, "one")}}, {})
        with (mock.patch.object(production_tree, "running_sessions",
                                return_value=[record(CHILD_RUNNER), record(DIRECTOR)]),
              mock.patch.object(unit_lookup, "every_marked_unit", return_value=marked),
              contextlib.redirect_stderr(io.StringIO()) as errors):
            edges = production_tree.tree()
        self.assertEqual(edges, [{"manager": DIRECTOR, "member": "thread-impl", "role": "impl"}])
        self.assertIn("staff unreadable", errors.getvalue())

    def test_claude_workers_take_their_slot_role_from_the_board_without_a_server(self) -> None:
        run = self.run_folder("unit", DIRECTOR, {}, server=False)
        _ = (run / "board.log").write_text("".join(f"2026-10-16T09:00:00-0400 {line}\n" for line in (
            "[impl] register: impl launcher up (claude/opus:high); mesh=proj-impl; role=impl",
            "[test] register: test launcher up (claude/opus:high); mesh=proj-test; role=test",
            "[review] register: review launcher up (claude/opus:high); mesh=proj-review; role=review",
            "[test] handoff: role=impl writing the second half",
            "[stray] register: hello, no role yet",
        )))
        for slot, worker, family in (
            ("impl", "sid-claude-impl", "claude"),
            ("review", "sid-claude-review-gone", "claude"),
            ("fix", "thread-codex-fix", "codex"),
        ):
            _ = (run / f"impl_worker_{slot}.json").write_text(json.dumps(
                {"name": f"proj-{slot}", "slot": slot, "family": family, "model": "m", "worker_id": worker}))
        # The test worker has no launch record yet: its ledger short id prefixes its session id, and
        # its name ends with its slot, so its role is the one its slot took over on the board.
        _ = (run / "workers").write_text("sid-claude-t\tproj-test\nsid-claude-s\tproj-stray\nsid-nobody\tproj-impl\n")
        edges = self.session_tree({DIRECTOR, "sid-claude-impl", "sid-claude-test", "sid-claude-stray", "thread-codex-fix"}, {})
        self.assertEqual(edges, [
            {"manager": DIRECTOR, "member": "sid-claude-impl", "role": "impl"},
            {"manager": DIRECTOR, "member": "sid-claude-test", "role": "impl"},
        ])

    def test_a_run_with_both_families_lists_codex_and_claude_workers(self) -> None:
        run = self.run_folder("unit", DIRECTOR, {"proj-impl": {"thread_id": "thread-impl", "role": "impl", "status": "running"}})
        _ = (run / "board.log").write_text("2026-10-16T09:00:00-0400 [fix] register: fix launcher up; role=fix\n")
        _ = (run / "impl_worker_fix.json").write_text(json.dumps(
            {"name": "proj-fix", "slot": "fix", "family": "claude", "model": "m", "worker_id": "sid-claude-fix"}))
        _ = (run / "workers").write_text("sid-clau\tproj-fix\n")
        edges = self.session_tree({DIRECTOR, "sid-claude-fix"}, {})
        self.assertEqual(edges, [
            {"manager": DIRECTOR, "member": "thread-impl", "role": "impl"},
            {"manager": DIRECTOR, "member": "sid-claude-fix", "role": "fix"},
        ])

    def test_a_run_without_a_worker_ledger_has_no_claude_workers(self) -> None:
        run = self.run_folder("unit", DIRECTOR, {}, server=False)
        _ = (run / "board.log").write_text("2026-10-16T09:00:00-0400 [impl] register: impl launcher up; role=impl\n")
        _ = (run / "impl_worker_impl.json").write_text(json.dumps(
            {"name": "proj-impl", "slot": "impl", "family": "claude", "model": "m", "worker_id": "sid-claude-impl"}))
        self.assertEqual(self.session_tree({DIRECTOR, "sid-claude-impl"}, {}), [])

    def test_runs_side_by_side_under_one_director_each_list_their_workers(self) -> None:
        # A director's marker names one run; the phase it runs beside that one has its own folder.
        main = self.run_folder("delegate", DIRECTOR, {"p1-impl": {"thread_id": "thread-p1", "role": "impl", "status": "running"}},
                               name="run-phase-1")
        _ = self.run_folder("delegate", DIRECTOR, {"p2-fix": {"thread_id": "thread-p2", "role": "fix", "status": "running"}},
                            name="run-phase-2")
        (self.root / "delegate" / "active").mkdir()
        _ = (self.root / "delegate" / "active" / DIRECTOR).write_text(f"{main}\n")
        edges = self.session_tree({DIRECTOR}, {})
        self.assertEqual(edges, [
            {"manager": DIRECTOR, "member": "thread-p1", "role": "impl"},
            {"manager": DIRECTOR, "member": "thread-p2", "role": "fix"},
        ])

    def test_the_director_is_the_session_that_started_the_run(self) -> None:
        run = self.run_folder("unit", DIRECTOR, {"a-impl": {"thread_id": "thread-impl", "role": "impl", "status": "running"}})
        # A pass opened inside the worker rewrote the state's identity with the worker's own thread.
        _ = (run / "progress_history_state.json").write_text(json.dumps({
            "history_file": str(self.root / "history" / f"{run.name}.jsonl"),
            "main_agent": {"family": "codex", "session_id": "thread-impl"},
        }))
        edges = self.session_tree({DIRECTOR, "thread-impl"}, {})
        self.assertEqual(edges, [{"manager": DIRECTOR, "member": "thread-impl", "role": "impl"}])

    def test_the_recorded_director_names_a_run_opened_without_start_run(self) -> None:
        run = self.run_folder("unit", DIRECTOR, {"a-impl": {"thread_id": "thread-a", "role": "impl", "status": "running"}},
                              name="run-by-hand")
        (run / "progress_history_state.json").unlink()
        self.assertEqual(self.session_tree({DIRECTOR}, {}), [])
        unit_runs.record_director(run, DIRECTOR)
        self.assertEqual(self.session_tree({DIRECTOR}, {}), [{"manager": DIRECTOR, "member": "thread-a", "role": "impl"}])

    def test_a_run_without_a_started_history_has_no_director(self) -> None:
        no_state = self.run_folder("unit", DIRECTOR, {"a-impl": {"thread_id": "thread-a", "role": "impl", "status": "running"}},
                                   name="run-no-state")
        (no_state / "progress_history_state.json").unlink()
        gone = self.run_folder("unit", DIRECTOR, {"b-impl": {"thread_id": "thread-b", "role": "impl", "status": "running"}},
                               name="run-history-gone")
        (self.root / "history" / f"{gone.name}.jsonl").unlink()
        unstarted = self.run_folder("unit", DIRECTOR, {"c-impl": {"thread_id": "thread-c", "role": "impl", "status": "running"}},
                                    name="run-unstarted")
        _ = (self.root / "history" / f"{unstarted.name}.jsonl").write_text(json.dumps({"event_type": "phase_started"}) + "\n")
        _ = (self.root / "unit" / "stray-file").write_text("not a run\n")
        self.assertEqual(self.session_tree({DIRECTOR}, {}), [])

    def test_a_large_tree_answers_well_under_a_second(self) -> None:
        directors = [f"sid-director-{number}" for number in range(40)]
        for director in directors:
            _ = self.run_folder("unit", director, {
                f"w{seat}-impl": {"thread_id": f"{director}-{seat}", "role": "impl", "status": "running"} for seat in range(6)
            })
        started = time.process_time()
        edges = self.session_tree({RUNNER, *directors}, {"main": {director: unit(director, director) for director in directors}})
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
