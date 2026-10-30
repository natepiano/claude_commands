"""Run-history lookups and ownership of a stopped unit's processes."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, final, override
from unittest import mock
from scripts.direct import workers

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rename_unit
import retire_unit
import unit_lookup
import unit_move
import unit_runs
from rename_state import RenameRefused

if TYPE_CHECKING:
    from scripts.shutdown import shutdown_record
else:
    # rename_unit puts the shutdown folder on the path.
    import shutdown_record

from scripts.production.rename_unit_test_support import SLUG, TEST_HANG_GUARD_SECS, UnitRenameFixture


# An app-server's idle-stop watcher, as codex_mesh starts it: the run folder is its fourth argument.
WATCHER = 'while kill -0 "$1"; do sleep "$5"; "$4" "$0" idle-stop --session-dir "$3" --pid "$1" --port "$2"; done'
STOPPED_RECORD = {"login": "owner@example.com", "then": "stop", "progress": {"kind": "stopped"},
                  "session": {"session_id": "sid-alpha",
                              "host": {"kind": "unit", "production": SLUG, "unit": "alpha-unit"}}}


class RunRecordLookupTests(unittest.TestCase):
    def test_move_lookups_build_once_and_keep_exact_worktree_spelling(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        runs = root / "history/runs"
        runs.mkdir(parents=True)
        tree = root / "tree"
        tree.mkdir()
        alias = root / "alias"
        alias.symlink_to(tree, target_is_directory=True)
        for worktree, at in ((tree, 2), (alias, 1)):
            events = [{"event_type": "run_started", "working_dir": str(worktree), "run_started_at": at}]
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
            self.assertIsNone(unit_move.open_run(alias))
            self.assertEqual(unit_move.newest_run(alias), runs / "alias.jsonl")


class FakeProcess(NamedTuple):
    cwd: Path
    session: int
    arguments: list[str]


@final
class RunProcessTests(unittest.TestCase):
    """What a stopped unit's runs leave in its worktree. The processes are records: nothing real is signalled."""

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.worktree = Path()
        self.processes: dict[int, FakeProcess] = {}
        self.signalled: list[int] = []

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.worktree = self.root / "claude-demo-alpha"
        other = self.root / "claude-demo-beta"
        mine = self.run_folder("run-a", self.worktree)
        theirs = self.run_folder("run-b", other)
        lint = ["python3", "mesh_runs.py", "supervise", str(self.run_record(mine, "lint", 101))]
        _ = self.run_record(mine, "test", 104)
        self.processes = {
            # The unit's detached lint run: the supervisor leads its session, verify.sh and tee are in it.
            101: FakeProcess(self.worktree, 101, lint),
            102: FakeProcess(self.worktree, 101, ["bash", "verify.sh", "lint", "pkg"]),
            103: FakeProcess(self.worktree, 101, ["tee", "-a", "run.log"]),
            # The idle-stop watcher of the run's stopped app-server, and its sleep.
            201: FakeProcess(self.worktree, 201, self.watcher(mine)),
            202: FakeProcess(self.worktree, 201, ["sleep", "300"]),
            # A record whose pid now names another process; another unit's watcher; the user's shell.
            104: FakeProcess(self.worktree, 104, ["cargo", "build"]),
            203: FakeProcess(self.worktree, 203, self.watcher(theirs)),
            900: FakeProcess(self.worktree, 900, ["zsh"]),
            # Another unit's detached run, in its own worktree.
            301: FakeProcess(other, 301, ["python3", "mesh_runs.py", "supervise",
                                          str(self.run_record(theirs, "lint", 301))]),
            302: FakeProcess(other, 301, ["bash", "verify.sh", "lint", "pkg"]),
        }
        self.enterContext(mock.patch.dict(os.environ, {"RETIRE_UNIT_RUN_ROOT": str(self.root / "runs"),
            "UNIT_WORKERS_STATE": str(self.root / "unit-workers")}))
        _ = self.enterContext(mock.patch.object(unit_move, "processes_inside", self.inside))
        _ = self.enterContext(mock.patch.object(retire_unit, "command_line", self.arguments))
        from scripts.agents import mesh_runs
        _ = self.enterContext(mock.patch.object(mesh_runs, "_process_args", self.arguments))
        _ = self.enterContext(mock.patch.object(os, "getsid", self.session))
        _ = self.enterContext(mock.patch.object(os, "kill", self.kill))
        _ = self.enterContext(mock.patch.object(shutdown_record, "all_session_records", lambda: [STOPPED_RECORD]))
        _ = self.enterContext(mock.patch.object(unit_lookup, "marked_units", self.no_units))

    def run_folder(self, name: str, worktree: Path) -> Path:
        worktree.mkdir(parents=True, exist_ok=True)
        folder = self.root / "runs" / name
        (folder / retire_unit.DETACHED_RUNS).mkdir(parents=True)
        _ = (folder / "progress_history_state.json").write_text(json.dumps({"working_dir": str(worktree)}))
        return folder

    def run_record(self, folder: Path, label: str, pid: int) -> Path:
        path = folder / retire_unit.DETACHED_RUNS / f"{label}.json"
        _ = path.write_text(json.dumps({"status": "running", "pid": pid, "label": label, "cwd": str(self.worktree)}))
        return path

    def watcher(self, folder: Path) -> list[str]:
        return ["sh", "-c", WATCHER, str(retire_unit.MESH.resolve()), "900", "51101", str(folder.resolve()),
                "python3", "300"]

    def no_units(self, _slug: str) -> dict[str, object]:
        return {}

    def survive(self, _pid: int, _signal: int) -> None:
        pass

    def inside(self, worktree: Path) -> list[int]:
        return sorted(pid for pid, item in self.processes.items() if item.cwd == worktree)

    def arguments(self, pid: int) -> list[str]:
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

    def rename(self) -> rename_unit.UnitRename:
        return rename_unit.UnitRename(SLUG, "alpha-unit", "gamma-unit", "", "", SLUG, "demo-alpha", "demo-gamma",
                                      str(self.worktree), str(self.root / "claude-demo-gamma"), "", "")

    def stop(self) -> str:
        output = io.StringIO()
        with redirect_stdout(output):
            rename_unit.stop_unit(self.rename(), lambda: None)
        return output.getvalue()

    def test_rename_refuses_a_paused_workers_live_command_inside_the_worktree_without_signalling(self) -> None:
        run = self.root / "runs" / "run-a"
        record = run / retire_unit.DETACHED_RUNS / "lint.json"
        _ = record.write_text(json.dumps({"status": "running", "pid": 101, "worker": "w7", "cwd": str(self.worktree)}))
        _ = (run / "progress_history_state.json").write_text(json.dumps({"working_dir": str(self.worktree),
            "production": SLUG, "unit": "alpha-unit"}))
        with mock.patch.dict(os.environ, {"UNIT_WORKERS_STATE": str(self.root / "unit-workers")}):
            store = workers.UnitWorkerLedger(workers.ledger_path(SLUG, "alpha-unit"))
            with store.locked() as ledger:
                ledger.workers["w7"] = workers.UnitWorker("w7", "impl", "codex", "sol", "high", {},
                    workers.CodexThread("kept-thread", str(self.root / "codex")), workers.Paused(),
                    [workers.WorkerEvent("opened", "fixture", str(run), 1_793_188_800.0, {})])
            with self.assertRaisesRegex(RenameRefused, r"detached run lint of w7 works inside"):
                _ = self.stop()
        self.assertEqual(self.signalled, [])
        self.assertIn(101, self.processes)
        self.assertIn(102, self.processes)
        self.assertTrue(record.exists())

    def test_dead_server_watchers_and_their_children_need_no_matching_run_folder(self) -> None:
        for folder in (self.root / "runs/run-b", self.root / "elsewhere/run-a", self.root / "gone"):
            for shell in ("sh", "/bin/sh"):
                with self.subTest(folder=folder, shell=shell):
                    arguments = self.watcher(folder)
                    arguments[0], arguments[4] = shell, "555"
                    self.processes[203] = FakeProcess(self.worktree, 203, arguments)
                    self.processes[204] = FakeProcess(self.worktree, 203, ["sleep", "300"])
                    found = retire_unit.run_processes(self.worktree, [203, 204])
                    self.assertEqual(set(found), {203, 204})
                    for name in found.values():
                        self.assertIn("app-server watcher 203 of dead server 555", name)
        self.assertEqual(self.signalled, [])

    def test_a_live_servers_watcher_still_needs_matching_run_history(self) -> None:
        cases: tuple[tuple[Path, set[int]], ...] = (
            (self.root / "runs/run-a", {203}), (self.root / "runs/run-b", set()),
            (self.root / "elsewhere/run-a", set()), (self.root / "gone", set()))
        for folder, expected in cases:
            with self.subTest(folder=folder):
                self.processes[203] = FakeProcess(self.worktree, 203, self.watcher(folder))
                self.assertEqual(set(retire_unit.run_processes(self.worktree, [203])), expected)
        self.assertEqual(self.signalled, [])

    def test_dead_server_watcher_requires_the_mesh_entry_point_and_valid_server_pid(self) -> None:
        for index, value in ((0, "bash"), (1, "-x"), (2, "echo idle"),
                             (3, str(self.root / "codex_mesh.py")), (4, "bad"), (4, "0"), (4, "-1")):
            with self.subTest(index=index, value=value):
                arguments = self.watcher(self.root / "gone")
                arguments[4] = "555"
                arguments[index] = value
                self.processes[203] = FakeProcess(self.worktree, 203, arguments)
                self.assertEqual(retire_unit.run_processes(self.worktree, [203]), {})
        self.assertEqual(self.signalled, [])

    def test_the_stop_ends_the_runs_supervisor_and_watcher_and_leaves_the_rest(self) -> None:
        printed = self.stop()
        self.assertEqual(sorted(self.signalled), [101, 102, 103, 201, 202])
        self.assertIn("stopped: process 101 (", printed)
        self.assertIn("of the run supervisor 101 of lint in run run-a", printed)
        self.assertIn("of the app-server watcher 201 of run run-a", printed)
        # What the unit's runs do not own still blocks the move.
        with self.assertRaisesRegex(RenameRefused, r"^processes 104, 203, 900 work inside "):
            rename_unit._check_stopped(self.rename())  # pyright: ignore[reportPrivateUsage]

    def test_with_only_its_runs_left_the_stopped_unit_can_move(self) -> None:
        for pid in (104, 203, 900):
            del self.processes[pid]
        _ = self.stop()
        rename_unit._check_stopped(self.rename())  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(sorted(self.processes), [301, 302])

    def test_a_process_that_outlives_its_sigterm_refuses_the_rename(self) -> None:
        self.processes = {pid: item for pid, item in self.processes.items() if pid in (101, 102)}
        _ = self.enterContext(mock.patch.object(os, "kill", self.survive))
        _ = self.enterContext(mock.patch.object(rename_unit, "RUN_STOP_WAIT_SECONDS", 0.0))
        with self.assertRaisesRegex(RenameRefused, r"of lint in run run-a: still runs after SIGTERM"):
            _ = self.stop()

    def serve(self, code: int) -> list[list[str]]:
        """Run run-a's app-server 501 with its code-mode host 502; `codex_mesh.py stop` exits `code`
        and, when it is 0, ends them. The calls come back."""
        folder = self.root / "runs" / "run-a"
        _ = (folder / "mesh_server.json").write_text(json.dumps({"pid": 501, "port": 51101}))
        self.processes = {501: FakeProcess(self.worktree, 501, ["codex", "app-server", "--listen", "ws://x"]),
                          502: FakeProcess(self.worktree, 501, ["codex-code-mode-host"])}
        calls: list[list[str]] = []

        def mesh(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
            del cwd
            calls.append(argv)
            if not code:
                self.processes.clear()
            return subprocess.CompletedProcess(argv, code, "", "" if not code else "no server")

        _ = self.enterContext(mock.patch.object(retire_unit, "run", mesh))
        return calls

    def test_the_stop_ends_the_runs_app_server_and_its_seats(self) -> None:
        """The real case: modules-unit's 8-hour app-server and its code-mode host refused step 1."""
        calls = self.serve(0)
        rename_unit.check_stoppable(self.rename())
        printed = self.stop()
        self.assertEqual([call[2:] for call in calls], [["stop", "--pause", "--session-dir", str(self.root / "runs" / "run-a")]])
        self.assertIn("stopped: app-server pid 501 of run run-a", printed)
        rename_unit._check_stopped(self.rename())  # pyright: ignore[reportPrivateUsage]

    def test_an_app_server_its_stop_misses_gets_sigterm_with_its_children(self) -> None:
        _ = self.serve(1)
        printed = self.stop()
        self.assertEqual(sorted(self.signalled), [501, 502])
        self.assertIn("no server; SIGTERM instead", printed)
        self.assertIn("of the app-server 501 of run run-a", printed)

    def test_a_resumed_rename_ends_a_run_left_under_the_former_root(self) -> None:
        """The real case: the journal was past its stop step before the stop ended runs, and the
        run started under /tmp/claude/delegate, before the rename to /tmp/claude/unit."""
        former = self.root / "delegate"
        folder = former / "run-old"
        (folder / retire_unit.DETACHED_RUNS).mkdir(parents=True)
        _ = (folder / unit_runs.STATE_FILE).write_text(json.dumps({"working_dir": str(self.worktree)}))
        record = self.run_record(folder, "lint-old", 401)
        self.processes = {401: FakeProcess(self.worktree, 401, ["python3", "mesh_runs.py", "supervise", str(record)]),
                          402: FakeProcess(self.worktree, 401, ["bash", "verify.sh", "lint", "pkg"])}
        self.enterContext(mock.patch.dict(os.environ, {"RETIRE_UNIT_RUN_ROOT": ""}))
        _ = self.enterContext(mock.patch.object(unit_runs, "RUN_ROOT", self.root / "unit"))
        _ = self.enterContext(mock.patch.object(unit_runs, "FORMER_RUN_ROOT", former))
        output = io.StringIO()
        with redirect_stdout(output):
            rename_unit._check_stopped(self.rename())  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(sorted(self.signalled), [401, 402])
        self.assertIn("of the run supervisor 401 of lint-old in run run-old", output.getvalue())


@final
class DeadServerWatcherTests(UnitRenameFixture):
    def test_apply_ends_the_dead_servers_watcher_without_matching_run_history_and_moves_the_worktree(self) -> None:
        old = self.rust / "claude-demo-alpha"
        watcher = self.dead_server_watcher(old, self.root / "runs/run-a")
        self.assertIn(watcher.pid, unit_move.processes_inside(old))
        with mock.patch.dict(os.environ, self.env):
            self.assertNotIn(self.root / "runs/run-a", retire_unit.runs_of(old))
        server = retire_unit.command_line(watcher.pid)[4]
        # Reap the actual child on exit: kill(pid, 0) otherwise still sees its zombie.
        reaper = threading.Thread(target=lambda: watcher.wait(timeout=TEST_HANG_GUARD_SECS), daemon=True)
        reaper.start()
        def stop_watcher() -> None:
            watcher.kill()
            reaper.join(TEST_HANG_GUARD_SECS)
        self.addCleanup(stop_watcher)
        result = self.rename("apply")
        self.assert_ok(result)
        reaper.join(TEST_HANG_GUARD_SECS)
        self.assertFalse(reaper.is_alive())
        self.assertEqual(watcher.returncode, -15)
        self.assertIn(f"app-server watcher {watcher.pid} of dead server {server}", result.stdout)
        self.assertFalse(old.exists())
        self.assertTrue((self.rust / "claude-demo-gamma").is_dir())
        self.assertTrue(self.journal()["done"])


if __name__ == "__main__":
    _ = unittest.main()
