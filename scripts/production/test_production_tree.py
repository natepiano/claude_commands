"""The manager tree cargo-handler polls: every edge from fixture records, none from live state."""
from __future__ import annotations

import contextlib
import io
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import cast, override
from unittest import mock

import production_tree
import showrunners
import unit_lookup
import unit_runs

RUNNER, CHILD_RUNNER, GONE_RUNNER = "sid-runner", "sid-child-runner", "sid-gone-runner"
DIRECTOR, OLD_DIRECTOR, ORPHAN = "sid-director", "sid-old-director", "sid-orphan"
SCRIPT = Path(__file__).with_name("production_tree.py")


def record(session_id: str) -> dict[str, object]:
    return {"sessionId": session_id}


def unit(session_id: str, name: str) -> unit_lookup.MarkedUnit:
    claude = unit_lookup.LiveClaude(name, session_id, f"/run/{name}.sock", 1)
    return unit_lookup.MarkedUnit(name, name, claude, unit_lookup.TmuxPlace(f"%{name}"))


def server_recorded(run: Path) -> bool:
    """A fixture run's server counts as running while its record exists; no pid is checked."""
    return (run / "mesh_server.json").is_file()


class ProductionTreeTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        notifier, state = self.root / "notifier", self.root / "showrunner"
        for slug, session_id in (("main", RUNNER), ("child", CHILD_RUNNER), ("gone", GONE_RUNNER)):
            (notifier / f"showrunner-{slug}").mkdir(parents=True)
            _ = (notifier / f"showrunner-{slug}" / "conf").write_text(f"TARGET=session:{session_id}\nEVERY=30\n")
        (state / "child").mkdir(parents=True)
        _ = (state / "child" / "parent").write_text("main\n")
        for patch in (
            mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", notifier),
            mock.patch.object(showrunners, "SHOWRUNNER_STATE_DIR", state),
            mock.patch.object(unit_runs, "RUN_ROOT", self.root / "unit"),
            mock.patch.object(unit_runs, "FORMER_RUN_ROOT", self.root / "delegate"),
            mock.patch.object(production_tree, "_server_running", side_effect=server_recorded),
            mock.patch.object(production_tree, "pool_root", return_value=self.root / "review-pool"),
        ):
            _ = self.enterContext(patch)

    def run_folder(self, root: str, director: str, roster: dict[str, dict[str, object]], server: bool = True) -> Path:
        run = self.root / root / f"run-{director}"
        (self.root / root / "active").mkdir(parents=True, exist_ok=True)
        run.mkdir()
        _ = (self.root / root / "active" / director).write_text(f"{run}\n")
        _ = (run / "mesh_roster.json").write_text(json.dumps(roster))
        if server:
            _ = (run / "mesh_server.json").write_text(json.dumps({"pid": 1}))
        return run

    def tree(self, running: set[str], units: dict[str, dict[str, unit_lookup.MarkedUnit]]) -> list[production_tree.Edge]:
        marked = unit_lookup.MarkedProductions(units, {})
        with mock.patch.object(production_tree, "running_sessions", return_value=[record(session_id) for session_id in running]), \
                mock.patch.object(unit_lookup, "every_marked_unit", return_value=marked):
            return production_tree.tree()

    def test_every_kind_of_edge_between_running_sessions(self) -> None:
        _ = self.run_folder("unit", DIRECTOR, {
            "a-impl": {"thread_id": "thread-impl", "role": "impl", "status": "running"},
            "a-test": {"thread_id": "thread-test", "status": "idle"},
            "a-review": {"thread_id": "thread-review", "role": "review", "status": "ended"},
        })
        _ = self.run_folder("delegate", OLD_DIRECTOR, {
            "b-4": {"thread_id": "thread-fix", "role": "fix", "status": "done"},
        })
        edges = self.tree({RUNNER, CHILD_RUNNER, DIRECTOR, OLD_DIRECTOR}, {
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
        edges = self.tree({RUNNER, DIRECTOR}, {"main": {"one": unit(DIRECTOR, "one"), "two": unit(OLD_DIRECTOR, "two")}})
        self.assertEqual([edge for edge in edges if edge["role"] == "review"], [
            {"manager": DIRECTOR, "member": "thread-busy", "role": "review", "unit": "one",
             "thread_id": "thread-busy", "state": "busy", "type": "code"},
            {"manager": DIRECTOR, "member": "thread-idle", "role": "review", "unit": "one",
             "thread_id": "thread-idle", "state": "idle", "type": "design"},
        ])

    def test_a_manager_on_another_machine_keeps_its_edges(self) -> None:
        _ = self.run_folder("unit", DIRECTOR, {"a-impl": {"thread_id": "thread-impl", "role": "impl", "status": "running"}}, server=False)
        _ = self.run_folder("unit", ORPHAN, {"c-impl": {"thread_id": "thread-orphan", "role": "impl", "status": "running"}})
        edges = self.tree({CHILD_RUNNER, DIRECTOR, ORPHAN}, {
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
        edges = self.tree({RUNNER}, {"main": {"one": unit(DIRECTOR, "one")}})
        self.assertEqual(edges, [])

    def test_unreadable_unit_marks_keep_the_other_edges(self) -> None:
        with mock.patch.object(production_tree, "running_sessions", return_value=[record(RUNNER), record(CHILD_RUNNER)]), \
                mock.patch.object(unit_lookup, "every_marked_unit", side_effect=OSError("tmux: gone")), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            edges = production_tree.tree()
        self.assertEqual(edges, [{"manager": RUNNER, "member": CHILD_RUNNER, "role": "showrunner"}])
        self.assertIn("unit marks unreadable: tmux: gone", errors.getvalue())

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
        edges = self.tree({DIRECTOR, "sid-claude-impl", "sid-claude-test", "sid-claude-stray", "thread-codex-fix"}, {})
        self.assertEqual(edges, [
            {"manager": DIRECTOR, "member": "sid-claude-impl", "role": "impl"},
            {"manager": DIRECTOR, "member": "sid-claude-test", "role": "impl"},
        ])

    def test_a_run_with_both_families_lists_codex_and_claude_workers(self) -> None:
        run = self.run_folder("unit", DIRECTOR, {"proj-impl": {"thread_id": "thread-impl", "role": "impl", "status": "running"}})
        _ = (run / "board.log").write_text("2026-10-16T09:00:00-0400 [fix] register: fix launcher up; role=fix\n")
        _ = (run / "impl_worker_fix.json").write_text(json.dumps(
            {"name": "proj-fix", "slot": "fix", "family": "claude", "model": "m", "worker_id": "sid-claude-fix"}))
        edges = self.tree({DIRECTOR, "sid-claude-fix"}, {})
        self.assertEqual(edges, [
            {"manager": DIRECTOR, "member": "thread-impl", "role": "impl"},
            {"manager": DIRECTOR, "member": "sid-claude-fix", "role": "fix"},
        ])

    def test_a_large_tree_answers_well_under_a_second(self) -> None:
        directors = [f"sid-director-{number}" for number in range(40)]
        for director in directors:
            _ = self.run_folder("unit", director, {
                f"w{seat}-impl": {"thread_id": f"{director}-{seat}", "role": "impl", "status": "running"} for seat in range(6)
            })
        started = time.process_time()
        edges = self.tree({RUNNER, *directors}, {"main": {director: unit(director, director) for director in directors}})
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
