"""Check process ownership without the deleted rename shutdown step."""
from __future__ import annotations

from scripts.production.staff_test_support import foreign_hostname

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import NamedTuple, cast, final, override
from unittest import mock
from scripts.production import staff, worker_admission
from scripts.production.staff_test_support import fixture_worker


from scripts.agents import remote, shutdown
from scripts.production import unit_lookup
from scripts.production import unit_move
from scripts.production import staff_sources
from scripts.production.staff_test_support import (
    StaffTestCase, fixture_unit_location, recorded_showrunner,
)
from scripts.lib.conf import UseConfiguredValue
from scripts.lib import conf, unit_runs
from scripts.lint.memory_test_support import config_environment
from scripts.agents.unit_staff_test_support import PARENTS, isolated_parents

SLUG = "demo"


def setUpModule() -> None:
    unittest.enterModuleContext(foreign_hostname())


class FakeProcess(NamedTuple):
    cwd: Path
    session: int
    arguments: list[str]


@final
class RunProcessTests(StaffTestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.unit_folder = Path()
        self.processes: dict[int, FakeProcess] = {}
        self.signalled: list[int] = []

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(isolated_parents())
        PARENTS["alpha"] = staff_sources.UnitProduction(SLUG, "/fixture/demo-production.md")
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.dict(os.environ, config_environment(self.root / "config")))
        self.unit_folder = self.root / "claude-demo-alpha"
        other = self.root / "claude-demo-beta"
        mine = self.run_folder("run-a", self.unit_folder)
        theirs = self.run_folder("run-b", other)
        lint = ["python3", "detached_runs.py", "supervise", str(self.run_record(mine, "lint", 101))]
        _ = self.run_record(mine, "test", 104)
        self.processes = {
            # The unit's detached lint run: the supervisor leads its session, verify.sh and tee are in it.
            101: FakeProcess(self.unit_folder, 101, lint),
            102: FakeProcess(self.unit_folder, 101, ["bash", "verify.sh", "lint", "pkg"]),
            103: FakeProcess(self.unit_folder, 101, ["tee", "-a", "run.log"]),
            104: FakeProcess(self.unit_folder, 104, ["cargo", "build"]),
            900: FakeProcess(self.unit_folder, 900, ["zsh"]),
            # Another unit's detached run, in its own worktree.
            301: FakeProcess(other, 301, ["python3", "detached_runs.py", "supervise",
                                          str(self.run_record(theirs, "lint", 301))]),
            302: FakeProcess(other, 301, ["bash", "verify.sh", "lint", "pkg"]),
        }
        self.enterContext(mock.patch.dict(os.environ, {"RETIRE_UNIT_RUN_ROOT": str(self.root / "runs")}))
        _ = self.enterContext(mock.patch.object(unit_move, "processes_inside", self.inside))
        _ = self.enterContext(mock.patch.object(shutdown, "retirement_command_line", self.arguments))
        from scripts.agents import detached_run_process
        _ = self.enterContext(mock.patch.object(detached_run_process, "process_args", self.arguments))
        _ = self.enterContext(mock.patch.object(os, "getsid", self.session))
        _ = self.enterContext(mock.patch.object(os, "kill", self.kill))
        _ = self.enterContext(mock.patch.object(unit_lookup, "labeled_session", return_value=None))
        _ = self.enterContext(mock.patch.object(unit_lookup, "labeled_sessions", self.no_units))
        _ = self.enterContext(mock.patch.object(unit_lookup, "observed_session_labels", return_value=()))

    def run_folder(self, name: str, worktree: Path) -> Path:
        worktree.mkdir(parents=True, exist_ok=True)
        folder = self.root / "runs" / name
        (folder / shutdown.DETACHED_RUNS).mkdir(parents=True)
        parent = recorded_showrunner("demo", self.root / "demo-production.md")
        member = fixture_unit_location(parent, worktree.name, worktree)
        _ = (folder / "progress_history_state.json").write_text(json.dumps({"owner_staff_id": member.id}))
        return folder

    def run_record(self, folder: Path, label: str, pid: int) -> Path:
        path = folder / shutdown.DETACHED_RUNS / f"{label}.json"
        parent = worker_admission.parent(staff.local_snapshot(), run=folder)
        member = fixture_worker(folder.name + "-" + label, parent.id, backend=staff.CodexBackend(self.root / "codex"))
        _ = path.write_text(json.dumps({"status": "running", "pid": pid, "label": label, "staff_id": member.id}))
        return path


    def no_units(self, production_name: str) -> dict[str, object]:
        del production_name
        return {}

    def inside(self, worktree: Path) -> list[int]:
        return sorted(pid for pid, item in self.processes.items() if item.cwd == worktree)

    def arguments(self, pid: int, *, settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> list[str]:
        del settings
        return self.processes[pid].arguments if pid in self.processes else []

    def session(self, pid: int) -> int:
        if pid not in self.processes:
            raise ProcessLookupError(pid)
        return self.processes[pid].session

    def kill(self, pid: int, signal_number: int) -> None:
        if pid not in self.processes:
            raise ProcessLookupError(pid)
        if signal_number:
            self.signalled.append(pid)
            del self.processes[pid]

    def test_run_ownership_passes_one_settings_read_to_process_arguments(self) -> None:
        record = staff.snapshot()
        row = record.by_worktree(remote.local_machine(), self.unit_folder)
        assert row is not None and isinstance(row.worktree, staff.PresentWorktree)
        for machine, hostname in ((row.worktree.machine, remote.local_machine()), ("mac", "Nates-MacBook-Pro.local")):
            with self.subTest(machine=machine, hostname=hostname):
                owner = replace(row, worktree=staff.PresentWorktree(machine, row.worktree.path))
                supplied = staff.StaffSnapshot(tuple(owner if member.id == owner.id else member for member in record.rows))
                with (mock.patch.object(conf, "read", wraps=conf.read) as reads,
                      mock.patch.object(shutdown, "retirement_command_line", wraps=self.arguments) as arguments,
                      mock.patch.object(shutdown, "local_machine", return_value=hostname),
                      mock.patch.object(staff, "snapshot", side_effect=AssertionError("local staff is unavailable")),
                      mock.patch.object(staff, "local_snapshot", side_effect=AssertionError("local staff is unavailable"))):
                    found = shutdown.retirement_run_processes(owner, [101, 102], record=supplied)
                self.assertEqual(set(found), {101, 102})
                reads.assert_called_once_with("production")
                settings = [cast(object, call.kwargs["settings"]) for call in arguments.call_args_list]
                self.assertTrue(settings)
                self.assertTrue(all(value is settings[0] for value in settings))





class RunRecordLookupTests(StaffTestCase):
    def test_move_lookups_build_once_and_keep_exact_worktree_spelling(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        runs = root / "history/runs"
        runs.mkdir(parents=True)
        tree = root / "tree"
        tree.mkdir()
        alias = root / "alias"
        alias.symlink_to(tree, target_is_directory=True)
        parent = recorded_showrunner("lookup", root / "lookup-production.md")
        _ = self.enterContext(mock.patch.object(unit_runs, "RUN_ROOT", root / "unit"))
        for worktree, at in ((tree, 2), (alias, 1)):
            member = fixture_unit_location(parent, worktree.name, worktree)
            folder = root / "unit" / worktree.name
            folder.mkdir(parents=True)
            _ = (folder / unit_runs.STATE_FILE).write_text(json.dumps({unit_runs.OWNER_FIELD: member.id}))
            _ = staff.set_run(member.id, staff.OpenRun(folder))
            events = [{"event_type": "run_started", "main_agent": {"session_id": member.current_session},
                       "run_started_at": at}]
            _ = (runs / f"{worktree.name}.jsonl").write_text(
                "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        with (mock.patch.dict(os.environ, {"UNIT_HISTORY_DIR": str(root / "history")}),
              mock.patch.object(unit_lookup, "read_run_states", wraps=unit_lookup.read_run_states) as reads):
            for lookup in (unit_move.newest_run, unit_move.open_run):
                for worktree in (tree, alias, root / "absent"):
                    with self.subTest(lookup=lookup.__name__, worktree=worktree):
                        reads.reset_mock()
                        expected = runs / f"{worktree.name}.jsonl" if worktree != root / "absent" else None
                        self.assertEqual(lookup(worktree), expected)
                        reads.assert_called_once_with()
            with (runs / "alias.jsonl").open("a", encoding="utf-8") as handle:
                _ = handle.write('{"event_type": "run_finished"}\n')
            self.assertEqual(unit_move.open_run(alias), runs / "alias.jsonl")
            _ = staff.clear_run(member.id, folder)
            self.assertIsNone(unit_move.open_run(alias))
            self.assertEqual(unit_move.newest_run(alias), runs / "alias.jsonl")
