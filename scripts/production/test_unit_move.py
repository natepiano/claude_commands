"""Move a private unit worktree with native session evidence and row lifecycle calls."""
from __future__ import annotations

from scripts.agents import remote
from scripts.herdr import pane_launch, herdr_types

import io
import json
import math
import os
import shutil
import subprocess
import sys
import time
import unittest
from collections.abc import Callable, Iterable
from contextlib import ExitStack, redirect_stderr
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast, override
from unittest import mock

from scripts.accounts import accounts
from scripts.message import sessions
from scripts.production import session_staff, staff, unit_move
from scripts.production.add_unit_test_support import AddUnitTestCase
from scripts.production.cli_fixture import invoke_cli
from scripts.production.rename_unit_test_support import FolderRenameFixture, FolderSessionFixture
from scripts.production.rename_rerun_test_support import file_versions, saved_fixture
from scripts.production.staff_test_support import fixture_member, fixture_worker
from scripts.agents import pane_record, restart, shutdown_record
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.herdr import herdr
from scripts.herdr.test_worker_panes import PaneWorld
from scripts.lib.atomic_file import write_text_atomic


def setUpModule() -> None:
    # Session fixtures have no panes unless a test supplies pane evidence.
    _ = unittest.enterModuleContext(mock.patch.object(herdr.Herdr, "panes", return_value=[]))


class AddUnitResumeFolderTests(AddUnitTestCase):
    def test_resuming_a_stopped_unit_tab_retires_the_old_record_before_a_folder_move(self) -> None:
        from scripts.production import add_unit
        from scripts.production import unit_lookup
        from scripts.agents.test_pane_restore import FakeClient
        from datetime import datetime, timezone

        old = self.root / "project-alpha"
        owner = self.prepare_restored_worktree("alpha", old)
        records = self.root / "move-launches"
        self.env["HERDR_PANE_RECORD_DIR"] = str(records)
        config = Path(self.env["HOME"]) / ".claude2"
        original = pane_record.launch("w7:p1", owner.current_session, str(old),
            {"CLAUDE_CONFIG_DIR": str(config)}, ["claude", "-n", "alpha", "--model", "opus"])
        client = FakeClient([{"pane_id": "w7:p1", "tab_id": "w7:t1", "workspace_id": "w7", "label": "alpha"}])
        shell_pid = 998041
        def close(*arguments: str) -> subprocess.CompletedProcess[str]:
            self.assertEqual(arguments, ("pane", "close", "w7:p1"))
            FakeClient.close_tab_of(client, arguments[2])
            return subprocess.CompletedProcess(arguments, 0, "", "")

        labeled = unit_lookup.LabeledSession("alpha", unit_lookup.ClaudeNotRunning(),
            herdr.HerdrPlace("agents", "w7:p1"), identity=unit_lookup.LiveSession(owner.current_session))

        def launch(request: add_unit.UnitLaunch, _director: add_unit.DirectorAgent, _prompt: str,
                   _account: accounts.ClaudeLaunchAccount, *, target: str = "", model: str = "",
                   autocompact_window: str = "") -> herdr.PromptSubmitted:
            _ = (target, model, autocompact_window)
            replacement = pane_record.launch("w7:p3", owner.current_session, str(request.worktree),
                {"CLAUDE_CONFIG_DIR": str(config)}, ["claude", "--resume", owner.current_session])
            pane_record.write("agents", replacement)
            return herdr.PromptSubmitted(datetime.now(timezone.utc))

        with (mock.patch.object(pane_record, "RECORD_DIR", records),
              mock.patch("scripts.agents.pane_record.RECORD_DIR", records),
              mock.patch.dict(os.environ, self.env)):
            write_text_atomic(config / "projects/fixture" / f"{owner.current_session}.jsonl", "")
            pane_record.write("agents", original)
            with (mock.patch.object(add_unit, "launched_unit", return_value=add_unit.LabeledSessionFound(labeled)),
                  mock.patch.object(add_unit, "launch_session", side_effect=launch),
                  mock.patch.object(add_unit, "wait_for_remote_control", return_value=labeled),
                  client.empty_shell(shell_pid, old),
                  mock.patch.object(client, "run", side_effect=close),
                  mock.patch.object(herdr, "Herdr", return_value=client)):
                resumed = self.cli("alpha", "--plan", "docs/plans/given.md", "--resume", owner.current_session,
                                   "--cwd", str(old), "--model", "opus")
            shutil.rmtree(Path(self.env["HERDR_PROC"]) / str(shell_pid))
            shutil.rmtree(Path(self.env["HERDR_PROC"]) / "102")
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            destination = self.root / "moved-alpha"
            arguments = ["move-folder", "--unit", owner.id, "--to", str(destination)]
            with (mock.patch.object(herdr.Herdr, "panes", return_value=[]),
                  mock.patch.object(herdr, "herdr_processes", return_value=[]),
                  mock.patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive())):
                moved = invoke_cli(unit_move, lambda: unit_move.main(arguments), arguments, self.env, self.checkout)
            self.assertEqual(moved.returncode, 0, moved.stderr)
            publications = [entry for entry in pane_record.read("agents").values()
                            if pane_launch.reference_id(entry) in (owner.id, owner.current_session)]
            self.assertEqual([(entry["pane_id"], pane_record.resume_launch(entry)["cwd"]) for entry in publications],
                             [("w7:p3", str(destination))])


class UnitFolderMoveTests(FolderRenameFixture):
    def move_folder(self) -> subprocess.CompletedProcess[str]:
        arguments = ["move-folder", "--unit", str(self.owner.id), "--to", str(self.destination)]
        return invoke_cli(unit_move, lambda: unit_move.main(arguments), arguments, dict(os.environ), self.checkout)

    def assert_moved(self, backend: FolderSessionFixture) -> None:
        self.assertFalse(self.source.exists())
        self.assertTrue((self.destination / ".git").is_file())
        self.assertEqual(self.git("branch", "--show-current", cwd=self.destination), "cargo-liner-source")
        self.assertEqual(self.git("rev-parse", "--show-toplevel", cwd=self.destination), str(self.destination))
        current = staff.by_id(self.owner.id)
        assert current is not None
        self.assertEqual(current,
            replace(self.owner, worktree=staff.PresentWorktree(remote.local_machine(), self.destination), home=current.home))
        self.assertEqual((self.destination / "kept.txt").read_text(), "Keep this work.\n")
        for row in backend.starts:
            self.assertEqual(row.current_session, self.owner.current_session)
            self.assertEqual(row.worktree, staff.PresentWorktree(remote.local_machine(), self.destination))

    def assert_finished_rerun(self, backend: FolderSessionFixture) -> None:
        before = file_versions(self.root)
        starts, stops = backend.starts.copy(), backend.stops.copy()
        result = self.move_folder()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "unchanged\n", ""))
        self.assertEqual(file_versions(self.root), before)
        self.assertEqual((backend.starts, backend.stops), (starts, stops))

    def test_move_processes_inside_keeps_only_the_directors_pane_ancestors(self) -> None:
        shell, wrapper, director, child, other_shell, above_pane = 998030, 998040, 998041, 998042, 998050, 998020
        parents = {os.getpid(): 1, shell: above_pane, wrapper: shell, director: wrapper,
                   child: director, other_shell: shell, above_pane: 1}
        def parent(pid: int) -> int:
            return parents.get(pid, 0)
        for extra in ((), (other_shell,)):
            with (
                self.subTest(unrelated_shell=bool(extra)),
                mock.patch.object(unit_move, "processes_inside", return_value=[shell, wrapper, director, child, above_pane, *extra]),
                mock.patch.object(pane_record, "process_parent", side_effect=parent),
                mock.patch.object(herdr, "read_conversation", return_value=herdr.LiveConversation("claude", (director,))),
                mock.patch.object(herdr, "native_process", return_value=herdr.NativeAgentProcess(
                    herdr.EnvironmentThread(self.owner.current_session), "fixture-start", {})),
                mock.patch.object(herdr, "herdr_place_of", return_value=herdr.HerdrPlace("agents", "w1:p1")),
                mock.patch.object(pane_record, "pane_shell_pid", return_value=shell),
            ):
                self.assertEqual(unit_move.move_processes_inside(self.owner, self.source), [above_pane, *extra])

    def test_processes_inside_reads_the_selected_process_root(self) -> None:
        process_root = self.root / "selected-proc"
        process_root.mkdir()
        for pid, cwd in ((41, str(self.source)), (23, str(self.source / "child")),
                         (17, str(self.source) + "-other"), (31, str(self.source) + " (deleted)")):
            process = process_root / str(pid)
            process.mkdir()
            (process / "cwd").symlink_to(cwd)
        with (mock.patch.dict(os.environ, {"HERDR_PROC": str(process_root)}),
              mock.patch.object(sys, "platform", "linux")):
            self.assertEqual(unit_move.processes_inside(self.source), [23, 31, 41])

    def test_running_unit_stops_moves_and_resumes_current_session(self) -> None:
        from scripts.agents import detached_run_process
        from scripts.lib import unit_runs

        with saved_fixture(self.root) as restore:
            for pending in ("", "rename", "native director and caller", "wrapped native director", "late detached command", "late pane worker",
                            "late run path", "late run path with lock"):
                with self.subTest(pending=pending), ExitStack() as scope:
                    restore()
                    if pending == "rename" or pending == "late run path with lock":
                        _ = self.git("worktree", "lock", "--reason", f"staff:{self.owner.id}", str(self.source))
                    backend = FolderSessionFixture(self.owner, live=True)
                    _ = scope.enter_context(mock.patch.object(unit_runs, "run_roots", return_value=(self.root / "runs",)))
                    path = self.root / "runs" / "current" / "detached_runs" / "late.json"
                    if pending == "native director and caller":
                        caller = os.getpid()
                        parents = {caller: 998042, 998042: 998041, 998043: 998041, 998041: 998040, 998040: 1}
                        def parent(pid: int) -> int:
                            return parents.get(pid, 0)
                        _ = scope.enter_context(mock.patch.object(unit_move, "processes_inside",
                            return_value=[caller, 998040, 998041, 998042, 998043]))
                        _ = scope.enter_context(mock.patch.object(pane_record, "process_parent", side_effect=parent))
                        _ = scope.enter_context(mock.patch.object(herdr, "read_conversation",
                            return_value=herdr.LiveConversation("claude", (998041,))))
                        _ = scope.enter_context(mock.patch.object(herdr, "native_process",
                            return_value=herdr.NativeAgentProcess(herdr.EnvironmentThread(self.owner.current_session),
                                "fixture-start", {})))
                    if pending == "wrapped native director":
                        parents = {os.getpid(): 1, 998041: 998042, 998042: 998040, 998040: 1, 998043: 998041}
                        def director_parent(pid: int) -> int:
                            return parents.get(pid, 0)
                        def conversation(_identity: str) -> herdr.LiveConversation | herdr.StoppedConversation:
                            return herdr.LiveConversation("claude", (998041,)) if backend.live else herdr.StoppedConversation()
                        _ = scope.enter_context(mock.patch.object(unit_move, "processes_inside",
                            return_value=[998040, 998041, 998042, 998043]))
                        _ = scope.enter_context(mock.patch.object(pane_record, "process_parent", side_effect=director_parent))
                        _ = scope.enter_context(mock.patch.object(herdr, "read_conversation", side_effect=conversation))
                        _ = scope.enter_context(mock.patch.object(herdr, "native_process",
                            return_value=herdr.NativeAgentProcess(herdr.EnvironmentThread(self.owner.current_session),
                                "fixture-start", {})))
                        _ = scope.enter_context(mock.patch.object(herdr, "herdr_place_of",
                            return_value=herdr.HerdrPlace("agents", "w1:p1")))
                        _ = scope.enter_context(mock.patch.object(pane_record, "pane_shell_pid", return_value=998040))
                    workers = [fixture_worker("late-implementation", self.owner.id)] if pending == "late pane worker" else []
                    late_owners = [fixture_member("unit", name="late-run-owner", parent=self.owner.parent,
                        worktree=staff.PresentWorktree(remote.local_machine(), self.root / "other-worktree"))
                        ] if pending.startswith("late run path") else []
                    run = self.source / "late-run"
                    lock = Path(self.git("rev-parse", "--absolute-git-dir", cwd=self.source)) / "locked"
                    refusal_files: dict[str, tuple[bytes, int]] = {}
                    appeared = False
                    original_owner = staff.by_id(self.owner.id)
                    assert original_owner is not None
                    before = file_versions(self.root)

                    def stop(row: staff.StaffRow, client: object) -> bool:
                        nonlocal appeared
                        stopped = backend.stop(row, client)
                        appeared = True
                        if pending == "late detached command":
                            write_text_atomic(path, json.dumps({"status": "running", "pid": 998201,
                                "label": "late fixture gate", "command": ["cat", str(self.source / "kept.txt")]}))
                        if late_owners:
                            run.mkdir()
                            _ = staff.set_run(late_owners[0].id, staff.OpenRun(run))
                            refusal_files.update(file_versions(self.root))
                            if pending == "late run path":
                                _ = refusal_files.pop(str(lock.relative_to(self.root)), None)
                        return stopped

                    def observe(rows: Iterable[staff.StaffRow], _record: staff.StaffSnapshot) -> list[staff.StaffRow]:
                        return [row for row in rows if row.id == self.owner.id and backend.live
                                or appeared and any(row.id == worker.id for worker in workers)]

                    with backend.installed(), ExitStack() as running_scope:
                        if pending.startswith("late "):
                            _ = running_scope.enter_context(mock.patch.object(restart, "stop_row", side_effect=stop))
                            _ = running_scope.enter_context(mock.patch.object(restart, "live_members", side_effect=observe))
                            _ = running_scope.enter_context(mock.patch.object(detached_run_process, "supervises", return_value=True))
                        if pending == "native director and caller":
                            arguments = ["move-folder", "--unit", str(self.owner.id), "--to", str(self.destination)]
                            result = invoke_cli(unit_move, lambda: unit_move.main(arguments), arguments,
                                                dict(os.environ), self.source)
                        else:
                            result = self.move_folder()
                        if late_owners:
                            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                            self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
                            self.assertIn(str(run), result.stderr)
                        else:
                            self.assertEqual((result.returncode, result.stderr), (0, ""))
                        self.assertEqual(backend.stops, [self.owner.current_session])
                        self.assertEqual(len(backend.starts), 1)
                        if pending.startswith("late "):
                            self.assertTrue(self.source.is_dir())
                            self.assertFalse(self.destination.exists())
                            self.assertEqual(staff.by_id(self.owner.id), original_owner)
                            self.assertTrue(backend.live)
                            if late_owners:
                                self.assertEqual(lock.exists(), pending == "late run path with lock")
                                self.assertEqual(file_versions(self.root), refusal_files)
                                self.assertEqual(staff.by_id(late_owners[0].id), replace(late_owners[0], run=staff.OpenRun(run)))
                            else:
                                after = file_versions(self.root)
                                _ = after.pop(str(path.relative_to(self.root)), None)
                                self.assertEqual(after, before)
                                lines = [line for line in result.stdout.splitlines() if "deferred" in line]
                                self.assertEqual(len(lines), 1, result.stdout)
                                self.assertIn(workers[0].name if workers else "late fixture gate", lines[0])
                        else:
                            self.assert_moved(backend)
                            self.assert_finished_rerun(backend)

    def test_resume_in_a_new_pane_closes_the_old_terminal_and_keeps_a_reused_pane(self) -> None:
        from scripts.herdr import herdr

        restore = self.enterContext(saved_fixture(self.root))
        for resumed_pane, close_fails in (("w1:p3", False), ("w1:p1", False), ("", False), ("w1:p3", True)):
            restore()
            with self.subTest(resumed_pane=resumed_pane, close_fails=close_fails):
                backend = FolderSessionFixture(self.owner, live=True)
                panes: list[herdr.PaneIdentity] = [
                    {"pane_id": "w1:p1", "terminal_id": "old-terminal", "workspace_id": "w1",
                     "session_id": self.owner.current_session},
                    {"pane_id": "w1:p2", "terminal_id": "sibling-terminal", "workspace_id": "w1",
                     "session_id": None},
                ]

                def start(row: staff.StaffRow, record: staff.StaffSnapshot, client: object) -> str:
                    _ = backend.start(row, record, client)
                    panes[0]["session_id"] = None
                    return resumed_pane

                with (backend.installed(), mock.patch.object(restart, "start_row", side_effect=start),
                      mock.patch.object(herdr.Herdr, "panes", side_effect=lambda: panes.copy()),
                      mock.patch.object(herdr.Herdr, "close_retained_pane",
                                        side_effect=OSError("fixture close failed") if close_fails else None) as close):
                    result = self.move_folder()
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assert_moved(backend)
                    self.assertEqual(backend.stops, [self.owner.current_session])
                    self.assertEqual(len(backend.starts), 1)
                    self.assertTrue(backend.live)
                    self.assertEqual(close.call_args_list,
                        [mock.call("w1:p1", terminal="old-terminal")] if resumed_pane == "w1:p3" else [])
                    if close_fails:
                        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
                        self.assertIn("fixture close failed", result.stderr)
                    else:
                        self.assertEqual(result.stderr, "")
                    self.assert_finished_rerun(backend)

    def test_history_and_launch_files_keep_their_bytes_and_times(self) -> None:
        files = (self.records / "agents.json", self.root / "runs/mesh_roster.json",
                 self.root / "runs/progress_history_state.json",
                 self.root / "history.jsonl")
        text = json.dumps({"session_id": self.owner.current_session, "cwd": str(self.source),
                           "branch": "cargo-liner-source", "queued_message": "Keep this task"}) + "\n"
        for path in files:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(path, text)
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in files]
        backend = FolderSessionFixture(self.owner, live=True)
        with backend.installed():
            result = self.move_folder()
            self.assertEqual((result.returncode, result.stderr), (0, ""))
            self.assert_moved(backend)
            self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in files], before)
            self.assert_finished_rerun(backend)

    def test_stopped_unit_with_a_closed_pane_stays_stopped(self) -> None:
        from scripts.agents import detached_run_process
        from scripts.lib import unit_runs

        restore = self.enterContext(saved_fixture(self.root))
        for state in ("idle", "pane worker", "detached command", "detached cwd", "finished live supervisor"):
            with self.subTest(state=state), ExitStack() as scope:
                restore()
                backend = FolderSessionFixture(self.owner, live=False)
                _ = scope.enter_context(backend.installed())
                _ = scope.enter_context(mock.patch.object(unit_move, "processes_inside", return_value=[]))
                _ = scope.enter_context(mock.patch.object(unit_runs, "run_roots", return_value=(self.root / "runs",)))
                if state == "pane worker":
                    worker = fixture_worker("running-implementation", self.owner.id)
                    def live_worker_members(rows: Iterable[staff.StaffRow], _record: staff.StaffSnapshot) -> list[staff.StaffRow]:
                        return [row for row in rows if row.id == worker.id]
                    _ = scope.enter_context(mock.patch.object(restart, "live_members",
                        side_effect=live_worker_members))
                    evidence = worker.name
                else:
                    evidence = "fixture gate"
                    path = self.root / "runs" / "other-unit" / "detached_runs" / "gate.json"
                    if state != "idle":
                        write_text_atomic(path, json.dumps({"status": "finished" if state == "finished live supervisor" else "running", "pid": 998201,
                            "label": evidence, "command": ["sh", "-c", f"cat '{self.source}/kept.txt'"]
                                if state != "detached cwd" else ["echo", "fixture"],
                            "cwd": str(self.source) if state == "detached cwd" else str(self.checkout)}))
                    _ = scope.enter_context(mock.patch.object(detached_run_process, "supervises", return_value=True))
                before = file_versions(self.root)
                result = self.move_folder()
                self.assertEqual((result.returncode, result.stderr), (0, ""))
                if state in ("pane worker", "detached command", "detached cwd", "finished live supervisor"):
                    self.assertTrue(self.source.is_dir())
                    self.assertFalse(self.destination.exists())
                    self.assertEqual(staff.by_id(self.owner.id), self.owner)
                    self.assertEqual(file_versions(self.root), before)
                    lines = [line for line in result.stdout.splitlines() if "deferred" in line]
                    self.assertEqual(len(lines), 1, result.stdout)
                    self.assertIn(evidence, lines[0])
                else:
                    self.assert_moved(backend)
                    self.assert_finished_rerun(backend)
                self.assertEqual((backend.starts, backend.stops), ([], []))
                self.assertFalse(backend.live)

    def test_run_paths_inside_the_worktree_refuse_without_changes(self) -> None:
        from scripts.lib import unit_runs

        restore = self.enterContext(saved_fixture(self.root))
        for source in ("staff run", "dispatch record", "supervisor record", "other member run", "other member dispatch"):
            with self.subTest(source=source):
                restore()
                run = self.source / "run"
                run.mkdir()
                owner = self.owner
                if source.startswith("other member"):
                    owner = fixture_member("unit", name="other", parent=self.owner.parent,
                        worktree=staff.PresentWorktree(remote.local_machine(), self.root / "other-worktree"))
                if source in ("staff run", "other member run"):
                    _ = staff.set_run(owner.id, staff.OpenRun(run))
                    evidence = run
                elif source in ("dispatch record", "other member dispatch"):
                    worker = fixture_worker("implementation", owner.id)
                    evidence = run / "progress_history_state.json"
                    write_text_atomic(evidence, "{}")
                    link = self.staff_file.parent / "dispatches" / str(worker.id)
                    link.parent.mkdir(parents=True, exist_ok=True)
                    link.symlink_to(evidence)
                else:
                    evidence = run / "supervisor.json"
                    write_text_atomic(evidence, json.dumps({"status": "finished", "pid": 998201}))
                    link = self.root / "runs" / "other-unit" / "detached_runs" / "gate.json"
                    link.parent.mkdir(parents=True, exist_ok=True)
                    link.symlink_to(evidence)
                before = file_versions(self.root)
                backend = FolderSessionFixture(self.owner, live=False)
                with (backend.installed(),
                      mock.patch.object(unit_runs, "run_roots", return_value=(self.root / "runs",))):
                    result = self.move_folder()
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
                self.assertIn(str(evidence), result.stderr)
                self.assertEqual(file_versions(self.root), before)
                self.assertTrue(self.source.is_dir())
                self.assertFalse(self.destination.exists())
                self.assertEqual((backend.starts, backend.stops), ([], []))

    def test_f002_stopped_unit_at_destination_rerun_changes_nothing(self) -> None:
        _ = self.git("worktree", "move", "--force", "--force", str(self.source), str(self.destination))
        moved = staff.set_worktree(self.owner.id, staff.PresentWorktree(remote.local_machine(), self.destination),
                                  expected=self.owner.worktree)
        backend = FolderSessionFixture(moved, live=False)
        before = file_versions(self.root)
        with backend.installed():
            result = self.move_folder()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "unchanged\n", ""))
        self.assertEqual(file_versions(self.root), before)
        self.assertEqual(staff.by_id(self.owner.id), moved)
        self.assertEqual((backend.starts, backend.stops), ([], []))
        self.assertFalse(backend.live)

    def test_missing_launch_record_finishes_the_move_without_writing_one(self) -> None:
        backend = FolderSessionFixture(self.owner, live=True)
        self.assertEqual(list(self.records.iterdir()), [])
        with backend.installed():
            result = self.move_folder()
            self.assertEqual((result.returncode, result.stderr), (0, ""))
            self.assert_moved(backend)
            self.assertEqual(list(self.records.iterdir()), [])
            self.assertEqual(len(backend.starts), 1)
            self.assert_finished_rerun(backend)

    def test_normal_codex_launch_moves_without_a_claude_launch_record(self) -> None:
        self.transcript("codex")
        backend = FolderSessionFixture(self.owner, live=True)
        with backend.installed():
            result = self.move_folder()
            self.assertEqual((result.returncode, result.stderr), (0, ""))
            self.assert_moved(backend)
            self.assertEqual(backend.resumes, [("codex", ["resume", self.owner.current_session])])
            self.assert_finished_rerun(backend)

    def test_real_restart_resumes_each_family_in_the_destination_and_keeps_the_terminal(self) -> None:
        from scripts.herdr import herdr
        from scripts.message import sessions
        from scripts.accounts import accounts
        from scripts.agents import stop_resume

        kinds: tuple[Literal["claude", "codex"], ...] = ("claude", "codex")
        for kind in kinds:
            with self.subTest(kind=kind), saved_fixture(self.root) as restore:
                self.transcript(kind)
                backend = FolderSessionFixture(self.owner, live=True)
                real_start = restart.start_row
                self.publish_session_process(kind, live=True)
                panes: list[herdr.PaneIdentity] = [{"pane_id": "w1:p1", "workspace_id": "w1",
                    "terminal_id": "kept-terminal", "label": self.owner.name, "session_id": self.owner.current_session}]
                resumed: list[tuple[str, list[str]]] = []
                def liveness(identity: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
                    self.assertEqual(identity, self.owner.current_session)
                    return sessions.LiveSessionProcess(998041) if backend.live else sessions.SessionNotLive()
                def stop(row: staff.StaffRow, client: object, *, kind: Literal["claude", "codex"] = kind) -> bool:
                    stopped = backend.stop(row, client)
                    self.publish_session_process(kind, live=False)
                    return stopped
                def prepare(identity: str, folder: str, environment: dict[str, str],
                            unset: frozenset[str]) -> herdr.PreparedAgentEnvironment:
                    self.assertEqual((identity, folder), (self.owner.current_session, str(self.destination)))
                    return herdr.PreparedAgentEnvironment(Path(folder), environment, unset)
                def launch(session: str, target: herdr.NewTab | herdr.SplitPane | herdr.ExistingPane,
                           environment: herdr.PreparedAgentEnvironment, family: str, arguments: list[str],
                           opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt, timeout: int, *, agent: str,
                           reported_unknown: str = "", codex: herdr.CodexConversations | herdr.CodexLaunchRead = herdr.CodexLaunchRead.CURRENT,
                           client: herdr.Herdr | herdr.LaunchClientSource = herdr.LaunchClientSource.NEW,
                           kind: Literal["claude", "codex"] = kind) -> herdr.AgentLaunched:
                    _ = (session, opening, timeout, agent, codex, client)
                    self.assertEqual(reported_unknown, "")
                    self.assertFalse(backend.live)
                    self.assertEqual(target, herdr.ExistingPane("w1:p1", "kept-terminal"))
                    self.assertEqual(environment.directory, self.destination)
                    self.assertIn(self.owner.current_session, arguments)
                    self.assertNotIn(str(self.owner.id), arguments)
                    self.assertEqual(family, kind)
                    resumed.append((family, arguments.copy()))
                    backend.live = True
                    self.publish_session_process(kind, live=True)
                    return herdr.AgentLaunched("w1:p1", herdr.PromptSubmissionNotRequired())
                def process_info(*arguments: str, deadline: float = math.inf) -> dict[str, object]:
                    del deadline
                    self.assertEqual(arguments, ("pane", "process-info", "--pane", "w1:p1"))
                    pid = 998041 if backend.live else 998040
                    argv = [kind, "--resume" if kind == "claude" else "resume", self.owner.current_session] if backend.live else ["zsh"]
                    return {"process_info": {"shell_pid": 998040, "foreground_process_group_id": pid,
                        "foreground_processes": [{"pid": pid, "name": kind if backend.live else "zsh", "argv": argv}]}}
                with (backend.installed(), mock.patch.object(restart, "start_row", side_effect=real_start),
                      mock.patch.object(restart, "stop_row", side_effect=stop),
                      mock.patch.object(accounts, "role_account", return_value=accounts.NamedAccount("claude1", self.home / ".claude")),
                      mock.patch.object(accounts, "account_dir", return_value=self.home / ".claude"),
                      mock.patch.object(stop_resume, "heal_account_trust"),
                      mock.patch.object(sessions, "session_liveness", side_effect=liveness),
                      mock.patch.object(herdr, "prepare_session_environment", side_effect=prepare),
                      mock.patch.object(herdr, "launch_prepared_agent", side_effect=launch),
                      mock.patch.object(herdr, "running_codex_threads", return_value=set()),
                      mock.patch.object(herdr.Herdr, "saved_row_tab", return_value=herdr.ExistingPane("w1:p1", "kept-terminal")),
                      mock.patch.object(herdr.Herdr, "result", side_effect=process_info),
                      mock.patch.object(herdr.Herdr, "panes", return_value=panes),
                      mock.patch.object(herdr.Herdr, "pane_activity", return_value=herdr.PaneAtShell()),
                      mock.patch.object(herdr.Herdr, "remember_row_tab")):
                    result = self.move_folder()
                    self.assertEqual((result.returncode, result.stderr), (0, ""))
                    self.assert_moved(backend)
                    self.assertEqual(len(resumed), 1)
                    arguments = resumed[0][1]
                    flag = "--resume" if kind == "claude" else "resume"
                    self.assertEqual(arguments[arguments.index(flag) + 1], self.owner.current_session)
                    self.assertEqual(list(self.records.iterdir()), [])
                    before = file_versions(self.root)
                    rerun = self.move_folder()
                    self.assertEqual((rerun.returncode, rerun.stdout, rerun.stderr), (0, "unchanged\n", ""))
                    self.assertEqual(len(resumed), 1)
                    self.assertEqual(file_versions(self.root), before)
                restore()

    def test_a_foreign_native_session_receives_no_stop_signal(self) -> None:
        from scripts.herdr import herdr
        from scripts.agents import stop_resume

        client = herdr.Herdr("agents")
        panes: list[herdr.PaneIdentity] = [{"pane_id": "w1:p1", "workspace_id": "w1", "label": "stale",
                                          "session_id": self.owner.current_session}]
        with (mock.patch.object(client, "panes", return_value=panes),
              mock.patch.object(client, "native_session", return_value="another-native-session"),
              mock.patch.object(stop_resume, "stop") as stop,
              mock.patch.object(os, "kill") as kill):
            self.assertFalse(restart.stop_row(self.owner, client))
        stop.assert_not_called()
        kill.assert_not_called()

    def test_missing_source_does_not_adopt_a_repository_with_another_common_directory(self) -> None:
        _ = self.git("worktree", "remove", "--force", "--force", str(self.source))
        unrelated = self.root / "unrelated-repository"
        unrelated.mkdir()
        _ = self.git("init", "-b", "unrelated", cwd=unrelated)
        _ = self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     "commit", "--allow-empty", "-m", "unrelated", cwd=unrelated)
        _ = self.git("worktree", "add", "-b", "unrelated-work", str(self.destination), cwd=unrelated)
        self.assertTrue((self.destination / ".git").is_file())
        self.assertNotEqual(self.git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=self.destination),
                            self.git("rev-parse", "--path-format=absolute", "--git-common-dir"))
        before = file_versions(self.root)
        backend = FolderSessionFixture(self.owner, live=False)
        with backend.installed():
            result = self.move_folder()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(file_versions(self.root), before)
        self.assertEqual((backend.starts, backend.stops), ([], []))

    def test_move_reads_only_members_and_defers_their_current_sessions(self) -> None:
        from scripts.herdr import herdr
        from scripts.message import sessions

        worker = fixture_worker("impl1", self.owner.id)
        other = fixture_member("unit", name="other", parent=self.owner.parent,
            worktree=staff.PresentWorktree(remote.local_machine(), self.root / "other-worktree"))
        other_worker = fixture_worker("other-worker", other.id)
        rows = (self.owner, worker, other, other_worker)
        running = {row.id for row in rows}
        events: list[tuple[str, staff.StaffRow]] = []
        def read_conversation(identity: str, _kind: str = "", *,
                              codex: herdr.CodexConversations | None = None) -> herdr.LiveConversation | herdr.StoppedConversation:
            _ = codex
            row = next(row for row in rows if row.current_session == identity)
            return herdr.LiveConversation("claude", (998041,)) if row.id in running else herdr.StoppedConversation()
        def stop(row: staff.StaffRow, _client: object) -> bool:
            events.append(("stop", row))
            running.remove(row.id)
            return True
        def start(row: staff.StaffRow, record: staff.StaffSnapshot, _client: object) -> str:
            self.assertNotIn(row.id, running)
            resolver = session_staff.RowResolver(record)
            self.assertEqual(shutdown_record.row_folder(row, record, resolver=resolver), (remote.local_machine(), self.destination))
            self.assertEqual(row.current_session, self.owner.current_session if row.id == self.owner.id else worker.current_session)
            events.append(("start", row))
            running.add(row.id)
            return "w1:p1" if row.id == self.owner.id else "w1:p2"
        with (mock.patch.object(herdr, "read_conversation", side_effect=read_conversation) as read,
              mock.patch.object(sessions, "running_sessions") as registry,
              mock.patch.object(herdr, "running_codex_threads") as codex,
              mock.patch.object(restart, "stop_row", side_effect=stop),
              mock.patch.object(restart, "start_row", side_effect=start),
              mock.patch.object(os, "kill", side_effect=AssertionError("No real signal is allowed"))):
            result = self.move_folder()
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(events, [])
        self.assertIn("folder move deferred", result.stdout)
        self.assertIn(worker.name, result.stdout)
        self.assertEqual(staff.by_id(self.owner.id), self.owner)
        self.assertEqual(staff.by_id(worker.id), worker)
        self.assertEqual(running, {row.id for row in rows})
        registry.assert_not_called()
        codex.assert_not_called()
        self.assertEqual({call.args[0] for call in read.call_args_list},
                         {self.owner.current_session, worker.current_session})
        self.assertTrue(self.source.is_dir())
        self.assertFalse(self.destination.exists())

    def test_unsafe_stop_moves_no_folder_and_sends_no_signal(self) -> None:
        from scripts.agents import shutdown

        for operation in ("move", "rename"):
            with self.subTest(operation=operation), saved_fixture(self.root) as restore, ExitStack() as recovery:
                _ = recovery.callback(restore)
                backend = FolderSessionFixture(self.owner, live=True)
                command = self.move_folder if operation == "move" else self.command
                with (backend.installed(), mock.patch.object(shutdown, "rename_repair_displays", return_value=False),
                      mock.patch.object(shutdown, "rename_send_notice")):
                    with (mock.patch.object(restart, "stop_row", return_value=False) as stop,
                          mock.patch.object(restart, "start_row", return_value="") as start,
                          mock.patch.object(os, "kill") as kill):
                        result = command()
                    self.assertEqual(result.returncode, 0, result.stderr)
                    if operation == "move":
                        self.assertIn("stop is incomplete", result.stderr)
                        self.assertTrue(self.source.is_dir())
                        self.assertFalse(self.destination.exists())
                        self.assertEqual(staff.by_id(self.owner.id), self.owner)
                    else:
                        self.assertEqual(result.stderr, "")
                        self.assertFalse(self.source.exists())
                        self.assertTrue(self.destination.is_dir())
                        stop.assert_not_called()
                        start.assert_not_called()
                        self.assertTrue(backend.live)
                    kill.assert_not_called()
                    retry = command()
                    self.assertEqual(retry.returncode, 0, retry.stderr)
                    expected_name = self.new_name if operation == "rename" else self.old_name
                    self.assertEqual(staff.by_id(self.owner.id), replace(self.owner, name=expected_name,
                        worktree=staff.PresentWorktree(remote.local_machine(), self.destination)))
                    self.assertEqual(self.git("branch", "--show-current", cwd=self.destination),
                                     f"cargo-liner-{expected_name}")

    def test_unsafe_resume_sends_no_keystroke_or_second_session(self) -> None:
        from scripts.herdr import herdr
        from scripts.message import sessions as native_sessions
        from scripts.accounts import accounts
        from scripts.agents import stop_resume

        client = herdr.Herdr("agents")
        panes: list[herdr.PaneIdentity] = [{"pane_id": "w1:p1", "session_id": self.owner.current_session,
                                          "terminal_id": "kept-terminal", "workspace_id": "w1", "label": self.owner.name}]
        with (mock.patch.object(accounts, "role_account", return_value=accounts.NamedAccount("claude1", self.home / ".claude")),
              mock.patch.object(accounts, "account_dir", return_value=self.home / ".claude"),
              mock.patch.object(stop_resume, "heal_account_trust"),
              mock.patch.object(client, "saved_row_tab", return_value=herdr.NewTab("w1", self.owner.name)),
              mock.patch.object(client, "panes", return_value=panes),
              mock.patch.object(client, "pane_activity", return_value=herdr.PaneRunning(("editor",))),
              mock.patch.object(herdr, "PROGRAM_TIMEOUT_SECONDS", 0),
              mock.patch.object(herdr, "check_target", wraps=herdr.check_target) as target,
              mock.patch.object(client, "result", return_value={"process_info": {"shell_pid": 998040, "foreground_process_group_id": 998099,
                  "foreground_processes": [{"pid": 998099, "name": "claude", "argv": ["claude"]}]}}),
              mock.patch.object(herdr, "native_process", return_value=herdr.NativeAgentProcess(
                  herdr.EnvironmentThread(self.owner.current_session), "fixture-start", {})),
              mock.patch.object(herdr, "start_agent") as agent_start,
              mock.patch.object(client, "run") as typed,
              mock.patch.object(herdr, "Herdr", return_value=client),
              mock.patch.object(herdr, "open_tab") as second):
            for live in (native_sessions.SessionNotLive(), native_sessions.LiveSessionProcess(998099)):
                self.publish_session_process("claude", live=isinstance(live, native_sessions.LiveSessionProcess), pid=998099)
                with self.subTest(live=live), mock.patch.object(native_sessions, "session_liveness", return_value=live):
                    with (mock.patch.object(restart, "session_launch", return_value=("claude", {}, ["--resume", self.owner.current_session])),
                          mock.patch.object(herdr, "launch_prepared_agent", wraps=herdr.launch_prepared_agent) as launch,
                          mock.patch.object(herdr, "prepare_session_environment", return_value=herdr.PreparedAgentEnvironment(self.source, {}))):
                        if isinstance(live, native_sessions.SessionNotLive):
                            with self.assertRaisesRegex(herdr_types.HerdrFailure, "other live work"):
                                _ = restart.start_row(self.owner, staff.snapshot(), client)
                        else:
                            _ = restart.start_row(self.owner, staff.snapshot(), client)
                    self.assertEqual(launch.call_count, int(isinstance(live, native_sessions.SessionNotLive)))
            target.assert_called_once()
            typed.assert_not_called()
            second.assert_not_called()
            agent_start.assert_not_called()


    def test_running_unit_defers_with_its_worker_command(self) -> None:
        self.test_move_reads_only_members_and_defers_their_current_sessions()

    def test_held_move_lock_refuses_without_stopping_or_moving(self) -> None:
        import fcntl

        backend = FolderSessionFixture(self.owner, live=True)
        before = file_versions(self.root)
        lock = self.staff_file.parent / "rows" / f"{self.owner.id}.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open("a") as handle, backend.installed():
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.move_folder()
        self.assertEqual(result.returncode, 1)
        self.assertIn("operation", result.stderr)
        self.assertTrue(self.source.is_dir())
        self.assertFalse(self.destination.exists())
        self.assertEqual((backend.starts, backend.stops), ([], []))
        self.assertEqual(file_versions(self.root), before)

    def test_existing_destination_reports_git_error_and_resumes_the_stopped_session(self) -> None:
        backend = FolderSessionFixture(self.owner, live=True)
        _ = self.destination.write_text("Keep the destination.\n")
        before = file_versions(self.root)
        with backend.installed(), mock.patch.object(unit_move, "git", wraps=unit_move.git) as command:
            result = self.move_folder()
        self.assertEqual(result.returncode, 1)
        self.assertIn("destination already exists", result.stderr)
        command.assert_not_called()
        self.assertTrue(self.source.is_dir())
        self.assertEqual(staff.by_id(self.owner.id), self.owner)
        self.assertEqual(self.destination.read_text(), "Keep the destination.\n")
        self.assertEqual((backend.stops, backend.starts), ([], []))
        self.assertTrue(backend.live)
        self.assertEqual(file_versions(self.root), before)

    def test_missing_launch_record_prints_one_line_and_finishes_the_move(self) -> None:
        self.test_missing_launch_record_finishes_the_move_without_writing_one()


    def test_crash_after_git_move_finishes_staff_paths_and_starts_the_unit(self) -> None:
        for step in ("move", "publish"):
            with self.subTest(step=step), saved_fixture(self.root) as restore, ExitStack() as recovery:
                _ = recovery.callback(restore)
                backend = FolderSessionFixture(self.owner, live=False)
                _ = self.git("worktree", "lock", "--reason", f"staff:{self.owner.id}", str(self.source))
                _ = self.git("worktree", "move", "--force", "--force", str(self.source), str(self.destination))
                if step == "publish":
                    _ = staff.set_worktree(self.owner.id, staff.PresentWorktree(remote.local_machine(), self.destination),
                                       expected=self.owner.worktree)
                with backend.installed():
                    result = self.move_folder()
                    self.assertEqual((result.returncode, result.stderr), (0, ""))
                    self.assert_moved(backend)
                    self.assertEqual((backend.stops, backend.starts, backend.live), ([], [], False))
                    self.assert_finished_rerun(backend)
                    second = self.root / "second-destination"
                    _ = unit_move.move_folder(self.owner.id, second)
                    current = staff.by_id(self.owner.id)
                    assert current is not None
                    self.assertEqual(current.worktree, staff.PresentWorktree(remote.local_machine(), second))
                    self.assertEqual((second / "kept.txt").read_text(), "Keep this work.\n")

    def test_unknown_pane_identity_does_not_stop_a_different_conversation(self) -> None:
        self.test_a_foreign_native_session_receives_no_stop_signal()

    def test_unsafe_resume_keeps_the_pane_and_reports_the_reason(self) -> None:
        self.test_unsafe_resume_sends_no_keystroke_or_second_session()

    def test_complete_unknown_folder_move_reports_once_with_or_without_a_launch(self) -> None:
        for kind in ("claude", "codex"):
            for retained in (False, True):
                with self.subTest(kind=kind, retained=retained), saved_fixture(self.root) as restore:
                    if retained:
                        pane_record.write("agents", pane_record.launch("w1:p1", str(self.owner.current_session),
                            str(self.source), {"CLAUDE_CONFIG_DIR": str(self.home / ".claude")}, [kind]))
                    before = file_versions(self.records)
                    current = staff.by_id(self.owner.id)
                    assert current is not None
                    unknown = herdr.UnknownConversation("source (conversation unreadable)")
                    with (mock.patch.object(herdr, "read_conversation", return_value=unknown),
                          mock.patch.object(sessions, "running_sessions", return_value=[]),
                          mock.patch.object(herdr, "running_codex_threads", return_value=set()),
                          mock.patch.object(restart, "stop_row") as stop,
                          mock.patch.object(restart, "start_row") as start,
                          mock.patch.object(os, "kill") as killed):
                        result = self.move_folder()
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("anomaly: source (conversation unreadable); not blocking", result.stderr)
                    stop.assert_not_called()
                    start.assert_not_called()
                    killed.assert_not_called()
                    self.assertFalse(self.source.exists())
                    self.assertTrue(self.destination.exists())
                    self.assertEqual(staff.by_id(self.owner.id), replace(current,
                        worktree=staff.PresentWorktree(remote.local_machine(), self.destination)))
                    self.assertEqual(file_versions(self.records), before)
                    restore()

    def test_unknown_folder_reader_never_proves_stopped_or_authorizes_a_signal(self) -> None:
        client = herdr.Herdr("agents")
        unknown = herdr.UnknownConversation("source (conversation unreadable)")
        codex = herdr.CodexConversations({}, ())
        with (mock.patch.object(herdr, "codex_conversations", return_value=codex),
              mock.patch.object(herdr, "read_conversation", return_value=unknown) as read,
              mock.patch.object(sessions, "running_sessions", return_value=[]),
              mock.patch.object(herdr, "running_codex_threads", return_value=set()) as threads,
              mock.patch.object(os, "kill") as killed, redirect_stderr(io.StringIO()) as output):
            self.assertEqual(restart.live_rows(staff.StaffSnapshot((self.owner,)), client), [])
        read.assert_called_once_with(self.owner.current_session, codex=threads.call_args.kwargs["codex"])
        killed.assert_not_called()
        self.assertEqual(output.getvalue().splitlines(), [
            "anomaly: source (conversation unreadable); not blocking"])

    def test_unknown_folder_resume_continues_only_through_the_target_guard(self) -> None:
        client = herdr.Herdr("agents")
        unknown = herdr.UnknownConversation("source (conversation unreadable)")
        target = herdr.NewTab("w1", self.owner.name)
        prepared = herdr.PreparedAgentEnvironment(self.source, {})
        with (mock.patch.object(accounts, "role_account", return_value=accounts.CodexWorkersAccount(
                  "codex1", self.home / ".codex1")),
              mock.patch.object(herdr, "read_conversation", return_value=unknown),
              mock.patch.object(restart, "session_launch", return_value=("codex", {}, ["resume", self.owner.current_session])),
              mock.patch.object(client, "saved_row_tab", return_value=target),
              mock.patch.object(client, "panes", return_value=[]),
              mock.patch.object(herdr, "prepare_session_environment", return_value=prepared),
              mock.patch.object(herdr, "launch_prepared_agent", side_effect=herdr_types.HerdrFailure("target retained")) as guard,
              redirect_stderr(io.StringIO()) as output):
            with self.assertRaisesRegex(herdr_types.HerdrFailure, "target retained"):
                _ = restart.start_row(self.owner, staff.snapshot(), client)
        guard.assert_called_once()
        self.assertEqual(guard.call_args.kwargs["reported_unknown"], unknown.reason)
        self.assertEqual(guard.call_args.args[1], target)
        self.assertEqual(output.getvalue().splitlines(), [
            "anomaly: source (conversation unreadable); not blocking"])

    def test_unknown_folder_resume_retains_a_pane_without_a_terminal(self) -> None:
        client = herdr.Herdr("agents")
        panes: list[herdr.PaneIdentity] = [{"pane_id": "w1:p1", "session_id": self.owner.current_session,
                                          "workspace_id": "w1", "label": self.owner.name}]
        unknown = herdr.UnknownConversation("source (conversation unreadable)")
        before = file_versions(self.records)
        with (mock.patch.object(accounts, "role_account", return_value=accounts.CodexWorkersAccount(
                  "codex1", self.home / ".codex1")),
              mock.patch.object(herdr, "read_conversation", return_value=unknown),
              mock.patch.object(restart, "session_launch", return_value=("codex", {}, ["resume", self.owner.current_session])),
              mock.patch.object(client, "saved_row_tab", return_value=herdr.ExistingPane("w1:p1", "")),
              mock.patch.object(client, "panes", return_value=panes),
              mock.patch.object(herdr, "prepare_session_environment", return_value=herdr.PreparedAgentEnvironment(self.source, {})),
              mock.patch.object(client, "result", return_value={"process_info": {"shell_pid": 998040,
                  "foreground_process_group_id": 998041, "foreground_processes": [{"pid": 998041,
                      "name": "codex", "argv": ["codex", "resume", self.owner.current_session]}]}}),
              mock.patch.object(herdr, "native_process", return_value=herdr.NativeAgentProcess(
                  herdr.PaneSessionReport(self.owner.current_session), "fixture-start", {})),
              mock.patch.object(client, "run") as typed,
              mock.patch.object(client, "remember_row_tab"),
              mock.patch.object(herdr, "Herdr", return_value=client),
              mock.patch.object(herdr, "PROGRAM_TIMEOUT_SECONDS", 0),
              mock.patch.object(herdr, "start_agent") as start,
              mock.patch.object(herdr, "open_tab") as second,
              redirect_stderr(io.StringIO()) as output):
            self.assertEqual(restart.start_row(self.owner, staff.snapshot(), client), "w1:p1")
        typed.assert_not_called()
        start.assert_not_called()
        second.assert_not_called()
        self.assertEqual(output.getvalue().count("anomaly: source"), 1)
        self.assertIn("terminal is unknown; target retained", output.getvalue())
        self.assertEqual(file_versions(self.records), before)

    def test_stopped_folder_reader_sends_no_signal_for_either_runtime(self) -> None:
        for kind in ("claude", "codex"):
            with self.subTest(kind=kind), saved_fixture(self.root) as restore:
                self.transcript(kind)
                backend = FolderSessionFixture(self.owner, live=False)
                with backend.installed(), mock.patch.object(os, "kill") as killed:
                    result = self.move_folder()
                self.assertEqual((result.returncode, result.stderr), (0, ""))
                killed.assert_not_called()
                self.assertFalse(backend.live)
                self.assertEqual((backend.starts, backend.stops), ([], []))
                restore()


    def test_stopped_unit_with_a_closed_pane_moves_records_without_starting(self) -> None:
        self.test_stopped_unit_with_a_closed_pane_stays_stopped()

    def test_live_codex_folder_move_keeps_thread_pane_and_terminal_despite_old_launch_marks(self) -> None:
        self.test_real_restart_resumes_each_family_in_the_destination_and_keeps_the_terminal()

    def test_open_phase_running_unit_resumes_in_the_moved_folder(self) -> None:
        history = self.root / "history.jsonl"
        _ = history.write_text(json.dumps({"event_type": "phase_started",
            "phase_instance_id": "phase-one", "phase_id": "1"}) + "\n")
        before = (history.read_bytes(), history.stat().st_mtime_ns)
        self.test_running_unit_stops_moves_and_resumes_current_session()
        self.assertEqual((history.read_bytes(), history.stat().st_mtime_ns), before)

    def test_open_phase_paused_unit_starts_after_the_move(self) -> None:
        history = self.root / "history.jsonl"
        _ = history.write_text(json.dumps({"event_type": "phase_started",
            "phase_instance_id": "phase-one", "phase_id": "1"}) + "\n")
        before = (history.read_bytes(), history.stat().st_mtime_ns)
        self.test_stopped_unit_with_a_closed_pane_stays_stopped()
        self.assertEqual((history.read_bytes(), history.stat().st_mtime_ns), before)

    def test_stopped_move_starts_the_unit_without_renaming(self) -> None:
        self.test_stopped_unit_with_a_closed_pane_stays_stopped()

    def test_rerun_starts_a_director_that_is_down_after_staff_publication(self) -> None:
        backend = FolderSessionFixture(self.owner, live=False)
        _ = self.git("worktree", "move", "--force", "--force", str(self.source), str(self.destination))
        _ = staff.set_worktree(self.owner.id, staff.PresentWorktree(remote.local_machine(), self.destination),
                               expected=self.owner.worktree)
        with backend.installed(), mock.patch.object(restart, "stop_row") as stop:
            result = self.move_folder()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout.endswith("unchanged\n"))
            stop.assert_not_called()
            self.assertEqual((backend.stops, backend.starts, backend.live), ([], [], False))
            self.assert_finished_rerun(backend)

    def test_incomplete_stop_report_still_starts_a_director_that_has_exited(self) -> None:
        backend = FolderSessionFixture(self.owner, live=True)
        def incomplete(row: staff.StaffRow, client: object) -> bool:
            _ = backend.stop(row, client)
            return False
        with backend.installed(), mock.patch.object(restart, "stop_row", side_effect=incomplete):
            result = self.move_folder()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stop is incomplete", result.stderr)
        self.assertEqual(backend.stops, [self.owner.current_session])
        self.assertEqual(len(backend.starts), 1)
        self.assertTrue(backend.live)
        self.assertTrue(self.source.exists())
        self.assertFalse(self.destination.exists())
        self.assertEqual(backend.starts[0].worktree, self.owner.worktree)

    def test_live_director_with_several_retained_launches_restarts_its_current_session(self) -> None:
        current = pane_record.launch("w1:p1", str(self.owner.current_session), str(self.source),
            {"CLAUDE_CONFIG_DIR": str(self.home / ".claude")}, ["claude", "--resume", self.owner.current_session])
        previous = current.copy()
        previous.update(pane_id="w1:p9", session_id=str(self.owner.id), ended=True)
        pane_record.save("agents", {"w1:p1": current, "w1:p9": previous})
        before = file_versions(self.records)
        self.test_running_unit_stops_moves_and_resumes_current_session()
        self.assertEqual(file_versions(self.records), before)

    def test_folder_move_defers_and_keeps_a_live_idle_worker(self) -> None:
        world = PaneWorld()
        world.setup(self)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.home / ".claude")})))
        config = self.root / "conf/direct.conf"
        write_text_atomic(config.with_name("production.conf"), self.configuration.read_text())
        write_text_atomic(config, config.read_text().replace("WORKER_TRANSPORT=mesh", "WORKER_TRANSPORT=herdr"))
        idle = fixture_worker("idle", self.owner.id)
        world.add(self.owner, "w1:p0")
        world.add(idle, "w1:p1")
        backend = FolderSessionFixture(self.owner, live=True)
        real_start = restart.start_row

        def live_members(rows: Iterable[staff.StaffRow], _record: staff.StaffSnapshot) -> list[staff.StaffRow]:
            identities = {world.processes[pid].current_identity for pid, _, _ in world.foreground.values()}
            return [row for row in rows if row.current_session in identities]

        def start(row: staff.StaffRow, record: staff.StaffSnapshot, client: herdr.Herdr) -> str:
            if row.kind == "worker":
                return real_start(row, record, client)
            pane = backend.start(row, record, client)
            world.activate(row.current_session, "w1:p0", "claude")
            return pane

        def stop(row: staff.StaffRow, client: herdr.Herdr) -> bool:
            if row.id == self.owner.id:
                _ = backend.stop(row, client)
            return world.stop(row, client)

        with (mock.patch.object(restart, "live_members", side_effect=live_members),
              mock.patch.object(restart, "stop_row", side_effect=stop),
              mock.patch.object(restart, "start_row", side_effect=start),
              mock.patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              mock.patch.object(herdr.Herdr, "panes", wraps=world.panes),
              mock.patch.object(os, "kill", side_effect=world.signal)):
            result = self.move_folder()
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertTrue(self.source.is_dir())
        self.assertFalse(self.destination.exists())
        self.assertEqual(staff.by_id(self.owner.id), self.owner)
        self.assertEqual(staff.by_id(idle.id), idle)
        self.assertEqual((backend.starts, backend.stops), ([], []))
        self.assertTrue(backend.live)
        self.assertIn("folder move deferred", result.stdout)
        self.assertIn(idle.name, result.stdout)
        self.assertIn("w1:p0", world.foreground)
        self.assertIn("w1:p1", world.foreground)
        self.assertEqual(world.launches, [])

    def test_folder_move_keeps_the_same_worker_thread_and_staff_folder(self) -> None:
        self.test_move_reads_only_members_and_defers_their_current_sessions()




class FolderBranchRecoveryTests(FolderRenameFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        from scripts.agents import shutdown

        _ = self.enterContext(mock.patch.object(shutdown, "rename_send_notice"))

    def assert_recovery(self, step: str) -> None:
        from contextlib import ExitStack
        from scripts.production import unit_move as moving
        from scripts.agents import shutdown

        class InterruptedMove(BaseException):
            pass

        backend = FolderSessionFixture(self.owner, live=True)
        real_git = moving.git
        real_publish = staff.set_worktree
        real_prepare = shutdown.rename_prepare_notice
        real_notify = shutdown.rename_notify_unit

        def git(checkout: Path, *arguments: str) -> str:
            if arguments[:2] == ("branch", "-m"):
                self.assertTrue(backend.live)
            value = real_git(checkout, *arguments)
            if ((step == "move" and arguments[:2] == ("worktree", "move"))
                    or (step == "branch" and arguments[:2] == ("branch", "-m"))):
                raise InterruptedMove()
            return value

        def publish(identity: staff.StaffId, value: staff.PresentWorktree | staff.RemovedWorktree, *,
                    expected: staff.PresentWorktree | staff.RemovedWorktree | staff.WorktreeUnfilled) -> staff.StaffRow:
            result = real_publish(identity, value, expected=expected)
            if step == "publish":
                raise InterruptedMove()
            return result

        def prepare(row: staff.StaffRow, name: str) -> None:
            real_prepare(row, name)
            if step == "request":
                raise InterruptedMove()

        def notify(row: staff.StaffRow) -> bool:
            result = real_notify(row)
            if step == "notices":
                raise InterruptedMove()
            return result

        with backend.installed(), mock.patch.object(shutdown, "rename_repair_displays", return_value=False):
            with ExitStack() as interruptions:
                _ = interruptions.enter_context(mock.patch.object(moving, "git", side_effect=git))
                _ = interruptions.enter_context(mock.patch.object(staff, "set_worktree", side_effect=publish))
                _ = interruptions.enter_context(mock.patch.object(shutdown, "rename_prepare_notice", side_effect=prepare))
                _ = interruptions.enter_context(mock.patch.object(shutdown, "rename_notify_unit", side_effect=notify))
                with self.assertRaises(InterruptedMove):
                    _ = self.command()
            result = self.command()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(self.source.exists())
            self.assertEqual(self.git("branch", "--show-current", cwd=self.destination), "cargo-liner-destination")
            self.assertEqual(staff.by_id(self.owner.id), replace(self.owner, name="destination",
                worktree=staff.PresentWorktree(remote.local_machine(), self.destination)))
            self.assertEqual((backend.starts, backend.stops), ([], []))
            self.assertTrue(backend.live)
            before = file_versions(self.root)
            starts, stops = backend.starts.copy(), backend.stops.copy()
            rerun = self.command()
            self.assertEqual((rerun.returncode, rerun.stdout, rerun.stderr), (0, "unchanged\n", ""))
            self.assertEqual(file_versions(self.root), before)
            self.assertEqual((backend.starts, backend.stops), (starts, stops))

    def test_deferred_custom_branch_rename_refuses_a_second_rename(self) -> None:
        from scripts.agents import shutdown

        for origin in ("normal", "absent", "no push", "legacy absent"):
            with self.subTest(origin=origin), saved_fixture(self.root) as restore, ExitStack() as scope:
                _ = scope.callback(restore)
                pushes: list[tuple[str, ...]] = []
                if origin in {"absent", "legacy absent"}:
                    _ = self.git("remote", "remove", "origin")
                if origin == "no push":
                    git = unit_move.git

                    def refuse_push(checkout: Path, *arguments: str) -> str:
                        if arguments[:1] == ("push",):
                            pushes.append(arguments)
                            raise OSError("This branch must stay local.")
                        return git(checkout, *arguments)

                    _ = scope.enter_context(mock.patch.object(unit_move, "git", side_effect=refuse_push))
                _ = self.git("branch", "-m", "feature", cwd=self.source)
                destination = self.source.with_name("cargo-liner-omega")
                backend = FolderSessionFixture(self.owner, live=True)
                with (backend.installed(), mock.patch.object(shutdown, "rename_repair_displays", return_value=False),
                      mock.patch.object(unit_move, "worktree_busy", side_effect=[True, False, False])):
                    arguments = ["rename", str(self.owner.id), self.new_name]
                    result = invoke_cli(shutdown, lambda: shutdown.main(arguments), arguments,
                                        dict(os.environ), self.checkout)
                    self.assertEqual((result.returncode, result.stderr),
                                     (0, f"anomaly: branch feature does not end with -{self.old_name}\n"))
                    self.assertEqual(staff.by_id(self.owner.id), replace(self.owner, name=self.new_name))
                    self.assertEqual(unit_move.pending_rename(self.owner.id), unit_move.PendingRename(
                        self.source, self.destination, "feature", "feature", self.new_name))
                    movement = unit_move.json_object(unit_move.read_json(unit_move.rename_move_path(self.owner.id)))
                    self.assertIs(movement["branch_change"], False)
                    if origin == "legacy absent":
                        del movement["branch_change"]
                        unit_move.write_json(unit_move.rename_move_path(self.owner.id), movement)
                    fields = unit_move.json_object(unit_move.read_json(shutdown.rename_notice_path(self.owner.id)))
                    self.assertEqual(set(fields), {"key", "pending"})
                    self.assertTrue(self.source.is_dir())
                    other = ["rename", str(self.owner.id), "omega"]
                    before = file_versions(self.root)
                    refused = invoke_cli(shutdown, lambda: shutdown.main(other), other, dict(os.environ), self.checkout)
                    self.assertEqual(refused.returncode, 1)
                    self.assertIn(f"pending rename to {self.new_name}", refused.stderr)
                    self.assertEqual(file_versions(self.root), before)
                    result = invoke_cli(shutdown, lambda: shutdown.main(arguments), arguments,
                                        dict(os.environ), self.checkout)
                    self.assertEqual((result.returncode, result.stderr), (0, ""))
                    self.assertEqual(result.stdout.splitlines()[-1], "done")
                    result = invoke_cli(shutdown, lambda: shutdown.main(other), other, dict(os.environ), self.checkout)
                    self.assertEqual((result.returncode, result.stderr),
                                     (0, f"anomaly: branch feature does not end with -{self.new_name}\n"))
                    self.assertEqual(staff.by_id(self.owner.id), replace(self.owner, name="omega",
                        worktree=staff.PresentWorktree(remote.local_machine(), destination)))
                    self.assertEqual(self.git("branch", "--show-current", cwd=destination), "feature")
                    self.assertFalse(shutdown.rename_notice_path(self.owner.id).exists())
                    self.assertIsNone(unit_move.pending_rename(self.owner.id))
                    self.assertEqual((backend.starts, backend.stops), ([], []))
                    self.assertTrue(backend.live)
                self.assertEqual(pushes, [])

    def test_immediate_staff_rename_recovers_each_git_stage(self) -> None:
        from scripts.agents import shutdown

        cases = ((self.old_name, self.new_name, False), ("alpha", "beta-alpha", False),
                 ("alpha", "beta-alpha", True), ("alpha-beta", "beta", False),
                 ("alpha-beta", "beta", True), ("liner-alpha", "alpha", False))
        for old_name, new_name, custom_branch in cases:
            stages = ["staff", "move", "branch", "publication", "cleanup"]
            if old_name == "alpha" and new_name == "beta-alpha" and not custom_branch:
                stages.append("new rename")
            for stage in stages:
                with (self.subTest(old=old_name, new=new_name, custom=custom_branch, stage=stage),
                      saved_fixture(self.root) as restore, ExitStack() as recovery):
                    _ = recovery.callback(restore)
                    owner = self.owner
                    source = self.source
                    destination = self.destination
                    target_branch = "cargo-liner-destination"
                    if old_name != self.old_name:
                        source = self.source.with_name(f"cargo-liner-{old_name}")
                        destination = source.with_name(f"cargo-liner-{new_name}")
                        _ = self.git("worktree", "move", str(self.source), str(source))
                        owner = staff.set_worktree(owner.id, staff.PresentWorktree(remote.local_machine(), source),
                                                  expected=owner.worktree)
                        owner = staff.rename(owner.id, old_name)
                        _ = self.git("branch", "-m", "feature" if custom_branch else f"demo-{old_name}", cwd=source)
                        target_branch = "feature" if custom_branch else f"demo-{new_name}"
                    _ = unit_move.prepare_rename(owner, destination, target_branch, new_name)
                    shutdown.rename_prepare_notice(owner, new_name)
                    renamed = staff.rename(owner.id, new_name)
                    if stage != "staff":
                        _ = self.git("worktree", "lock", "--reason", f"staff:{owner.id}", str(source))
                        _ = self.git("worktree", "move", "--force", "--force", str(source), str(destination))
                    if stage in {"branch", "publication", "cleanup"} and not custom_branch:
                        _ = self.git("branch", "-m", target_branch, cwd=destination)
                    if stage in {"publication", "cleanup"}:
                        _ = staff.set_worktree(renamed.id, staff.PresentWorktree(remote.local_machine(), destination),
                                               expected=renamed.worktree)
                    if stage == "cleanup":
                        _ = self.git("worktree", "unlock", str(destination))
                        _ = shutdown.rename_notify_unit(renamed)
                    backend = FolderSessionFixture(renamed, live=True)
                    arguments = ["rename", str(owner.id), new_name]
                    with (backend.installed(), mock.patch.object(shutdown, "rename_repair_displays", return_value=False),
                          mock.patch.object(unit_move, "move_idle_folder", wraps=unit_move.move_idle_folder) as move):
                        if stage == "new rename":
                            before = file_versions(self.root)
                            other = ["rename", str(owner.id), "gamma"]
                            refused = invoke_cli(shutdown, lambda: shutdown.main(other), other,
                                                 dict(os.environ), self.checkout)
                            self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
                            self.assertIn("pending rename to beta-alpha", refused.stderr)
                            self.assertEqual(file_versions(self.root), before)
                            self.assertEqual(staff.by_id(renamed.id), renamed)
                            self.assertEqual(self.git("branch", "--show-current", cwd=destination), "demo-alpha")
                            move.assert_not_called()
                            other_move = ["move-folder", "--unit", str(owner.id), "--to", str(source.with_name("elsewhere"))]
                            refused = invoke_cli(unit_move, lambda: unit_move.main(other_move), other_move,
                                                 dict(os.environ), self.checkout)
                            self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
                            self.assertIn("pending rename to beta-alpha", refused.stderr)
                            self.assertEqual(file_versions(self.root), before)
                        result = invoke_cli(shutdown, lambda: shutdown.main(arguments), arguments,
                                            dict(os.environ), self.checkout)
                        self.assertEqual((result.returncode, result.stderr), (0, ""))
                        self.assertEqual(staff.by_id(renamed.id), replace(renamed,
                            worktree=staff.PresentWorktree(remote.local_machine(), destination)))
                        self.assertEqual(self.git("branch", "--show-current", cwd=destination), target_branch)
                        self.assertFalse(unit_move.move_locked(renamed.id, destination))
                        self.assertFalse(shutdown.rename_notice_path(renamed.id).exists())
                        self.assertIsNone(unit_move.pending_rename(renamed.id))
                        self.assertEqual((backend.starts, backend.stops), ([], []))
                        self.assertTrue(backend.live)
                        if stage == "cleanup":
                            move.assert_not_called()
                            self.assertIn(result.stdout.strip(), {"unchanged", "done"})
                        before = file_versions(self.root)
                        rerun = invoke_cli(shutdown, lambda: shutdown.main(arguments), arguments,
                                           dict(os.environ), self.checkout)
                        self.assertEqual((rerun.returncode, rerun.stdout, rerun.stderr), (0, "unchanged\n", ""))
                        self.assertEqual(file_versions(self.root), before)

    def test_pending_rename_refuses_an_unrecorded_path_or_branch(self) -> None:
        from scripts.agents import shutdown

        for changed in ("folder", "branch"):
            with self.subTest(changed=changed), saved_fixture(self.root) as restore, ExitStack() as scope:
                _ = scope.callback(restore)
                pending = unit_move.prepare_rename(self.owner, self.destination, "cargo-liner-destination", self.new_name)
                renamed = staff.rename(self.owner.id, self.new_name)
                if changed == "folder":
                    found = self.source.with_name("cargo-liner-unexpected")
                    _ = self.git("worktree", "lock", "--reason", f"staff:{renamed.id}", str(self.source))
                    _ = self.git("worktree", "move", "--force", "--force", str(self.source), str(found))
                    wanted = str(found)
                else:
                    wanted = "unexpected-source"
                    _ = self.git("branch", "-m", wanted, cwd=self.source)
                backend = FolderSessionFixture(renamed, live=True)
                before = file_versions(self.root)
                arguments = ["rename", str(renamed.id), self.new_name]
                with backend.installed(), mock.patch.object(shutdown, "rename_repair_displays") as displays:
                    result = invoke_cli(shutdown, lambda: shutdown.main(arguments), arguments,
                                        dict(os.environ), self.checkout)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(wanted, result.stderr)
                self.assertIn(f"pending rename to {self.new_name}", result.stderr)
                self.assertEqual(file_versions(self.root), before)
                self.assertEqual(unit_move.pending_rename(renamed.id), pending)
                self.assertEqual(staff.by_id(renamed.id), renamed)
                self.assertEqual((backend.starts, backend.stops), ([], []))
                displays.assert_not_called()

    def test_pending_rename_refuses_a_different_branch_before_worker_migration(self) -> None:
        from scripts.agents import shutdown

        pending = unit_move.prepare_rename(self.owner, self.destination, "cargo-liner-destination", self.new_name)
        renamed = staff.rename(self.owner.id, self.new_name)
        arguments = ["rename", str(renamed.id), self.new_name, "--to-branch", "another-branch"]
        before = file_versions(self.root)
        with mock.patch.object(shutdown, "rename_qualified_workers") as migration:
            result = invoke_cli(shutdown, lambda: shutdown.main(arguments), arguments,
                                dict(os.environ), self.checkout)
            self.assertEqual((result.returncode, result.stdout, result.stderr),
                             (1, "", f"rename: {renamed.name}: finish the pending rename to {pending.name}\n"))
            request = shutdown.RenamePlan(renamed, self.new_name,
                                          shutdown.UnitBranchMove(pending.source_branch, "another-branch"))
            with self.assertRaisesRegex(unit_move.RenameRefused, f"finish the pending rename to {pending.name}"):
                _ = shutdown.rename_apply_unit(request)
        migration.assert_not_called()
        self.assertEqual(unit_move.pending_rename(renamed.id), pending)
        self.assertEqual(staff.by_id(renamed.id), renamed)
        self.assertEqual(file_versions(self.root), before)

    def test_completed_rename_clears_after_the_checkout_is_removed(self) -> None:
        from scripts.agents import shutdown

        target_branch = "cargo-liner-destination"
        _ = unit_move.prepare_rename(self.owner, self.destination, target_branch, self.new_name)
        renamed = staff.rename(self.owner.id, self.new_name)
        _ = self.git("worktree", "move", str(self.source), str(self.destination))
        _ = self.git("branch", "-m", target_branch, cwd=self.destination)
        removed = staff.set_worktree(renamed.id, staff.RemovedWorktree(
            remote.local_machine(), self.destination, staff.ParkedBranch(target_branch)), expected=renamed.worktree)
        _ = self.git("worktree", "remove", str(self.destination))
        arguments = ["rename", str(removed.id), self.new_name]
        with (mock.patch.object(shutdown, "rename_repair_displays", return_value=False),
              mock.patch.object(unit_move, "move_idle_folder", wraps=unit_move.move_idle_folder) as move):
            result = invoke_cli(shutdown, lambda: shutdown.main(arguments), arguments,
                                dict(os.environ), self.checkout)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "done\n", ""))
        self.assertEqual(staff.by_id(removed.id), removed)
        self.assertIsNone(unit_move.pending_rename(removed.id))
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.git("--git-dir", str(self.root / "origin.git"), "for-each-ref",
                                  "--format=%(objectname)", f"refs/heads/{target_branch}"),
                         self.git("rev-parse", f"refs/heads/{target_branch}"))
        move.assert_not_called()

    def test_failed_push_recovers_after_the_checkout_is_removed(self) -> None:
        from scripts.agents import shutdown

        target_branch = "cargo-liner-destination"
        for removed_location in (False, True):
            with self.subTest(removed=removed_location), saved_fixture(self.root) as restore, ExitStack() as scope:
                _ = scope.callback(restore)
                owner = self.owner
                if removed_location:
                    owner = staff.set_worktree(owner.id, staff.RemovedWorktree(
                        remote.local_machine(), self.source, staff.ParkedBranch("cargo-liner-source")),
                        expected=owner.worktree)
                backend = FolderSessionFixture(owner, live=False)
                _ = scope.enter_context(backend.installed())
                _ = scope.enter_context(mock.patch.object(shutdown, "rename_repair_displays", return_value=False))
                git = unit_move.git

                def failed_push(checkout: Path, *arguments: str) -> str:
                    if arguments[:1] == ("push",):
                        raise OSError("The push stopped.")
                    return git(checkout, *arguments)

                with mock.patch.object(unit_move, "git", side_effect=failed_push):
                    failed = self.command()
                self.assertNotEqual(failed.returncode, 0)
                self.assertIn("The push stopped.", failed.stderr)
                pending = unit_move.pending_rename(owner.id)
                self.assertIsNotNone(pending)
                assert pending is not None
                self.assertTrue(pending.branch_change)
                current = staff.by_id(owner.id)
                assert current is not None
                self.assertEqual(current.worktree.path if not isinstance(current.worktree, staff.WorktreeUnfilled) else None,
                                 self.destination)
                if removed_location:
                    self.assertEqual(current.worktree, staff.RemovedWorktree(
                        remote.local_machine(), self.destination, staff.ParkedBranch("cargo-liner-source")))
                self.assertIn(target_branch, failed.stderr)
                self.assertEqual(len(failed.stderr.splitlines()), 1, failed.stderr)
                tip = self.git("rev-parse", f"refs/heads/{target_branch}")
                if not removed_location:
                    current = staff.set_worktree(current.id, staff.RemovedWorktree(
                        remote.local_machine(), self.destination, staff.ParkedBranch(target_branch)),
                        expected=current.worktree)
                _ = self.git("worktree", "remove", "--force", "--force", str(self.destination))
                arguments = ["rename", str(current.id), self.new_name]
                with mock.patch.object(unit_move, "git", side_effect=failed_push):
                    refused = invoke_cli(shutdown, lambda: shutdown.main(arguments), arguments,
                                         dict(os.environ), self.checkout)
                self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
                self.assertEqual(len(refused.stderr.splitlines()), 1, refused.stderr)
                self.assertIn(target_branch, refused.stderr)
                self.assertEqual(unit_move.pending_rename(current.id), pending)
                self.assertEqual(staff.by_id(current.id), current)
                result = invoke_cli(shutdown, lambda: shutdown.main(arguments), arguments,
                                    dict(os.environ), self.checkout)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "done\n", ""))
                self.assertEqual(self.git("--git-dir", str(self.root / "origin.git"), "rev-parse",
                                          f"refs/heads/{target_branch}"), tip)
                self.assertIsNone(unit_move.pending_rename(current.id))
                self.assertEqual(staff.by_id(current.id), replace(current, worktree=staff.RemovedWorktree(
                    remote.local_machine(), self.destination, staff.ParkedBranch(target_branch))))
                self.assertFalse(self.destination.exists())
                self.assertEqual((backend.starts, backend.stops), ([], []))
                before = file_versions(self.root)
                retry = invoke_cli(shutdown, lambda: shutdown.main(arguments), arguments,
                                   dict(os.environ), self.checkout)
                self.assertEqual((retry.returncode, retry.stdout, retry.stderr), (0, "unchanged\n", ""))
                self.assertEqual(file_versions(self.root), before)

    def test_missing_checkout_requires_a_matching_removed_location(self) -> None:
        pending = unit_move.prepare_rename(self.owner, self.destination, "cargo-liner-destination", self.new_name)
        _ = self.git("worktree", "remove", str(self.source))
        states = (staff.PresentWorktree(remote.local_machine(), self.destination), staff.WorktreeUnfilled(),
                  staff.RemovedWorktree(remote.local_machine(), self.destination, staff.BranchUnrecorded()),
                  staff.RemovedWorktree(remote.local_machine(), self.destination, staff.ParkedBranch("unexpected")))
        for location in states:
            with self.subTest(location=location):
                row = replace(self.owner, worktree=location)
                with self.assertRaises(unit_move.RenameRefused):
                    _ = unit_move.finish_rename(row, pending)
                self.assertEqual(unit_move.pending_rename(row.id), pending)
        row = replace(self.owner, worktree=staff.RemovedWorktree(
            remote.local_machine(), self.destination, staff.ParkedBranch(pending.source_branch)))
        self.assertFalse(unit_move.finish_rename(row, pending))
        self.assertEqual(unit_move.pending_rename(row.id), pending)

    def test_rerun_finishes_after_the_move_request(self) -> None:
        self.assert_recovery("request")

    def test_rerun_finishes_after_move(self) -> None:
        self.assert_recovery("move")

    def test_rerun_finishes_after_publish(self) -> None:
        self.assert_recovery("publish")

    def test_rerun_finishes_after_branch(self) -> None:
        self.assert_recovery("branch")

    def test_rerun_finishes_after_the_notices(self) -> None:
        self.assert_recovery("notices")

    def test_both_native_families_keep_current_session_without_a_launch_record(self) -> None:
        from scripts.agents import shutdown

        for kind in ("claude", "codex"):
            with self.subTest(kind=kind), saved_fixture(self.root) as restore:
                self.transcript(kind)
                self.assertEqual(list(self.records.iterdir()), [])
                backend = FolderSessionFixture(self.owner, live=True)
                with backend.installed(), mock.patch.object(shutdown, "rename_repair_displays", return_value=False):
                    result = self.command()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((backend.resumes, backend.stops, backend.starts), ([], [], []))
                self.assertTrue(backend.live)
                self.assertEqual(staff.by_id(self.owner.id), replace(self.owner, name="destination",
                    worktree=staff.PresentWorktree(remote.local_machine(), self.destination)))
                self.assertEqual(list(self.records.iterdir()), [])
                restore()

    def test_dirty_work_does_not_refuse_a_nondestructive_move(self) -> None:
        from scripts.agents import shutdown

        with saved_fixture(self.root) as restore:
            for state in ("idle", "live detached run", "stale detached record"):
                with self.subTest(state=state), ExitStack() as scope:
                    restore()
                    _ = (self.source / "kept.txt").write_text("Keep the uncommitted edit.\n")
                    _ = (self.source / "draft.md").write_text("Keep the untracked draft.\n")
                    tip = self.git("rev-parse", "HEAD", cwd=self.source)
                    commands: list[subprocess.Popen[bytes]] = []
                    if state != "idle":
                        run = self.root / "run"
                        records = run / "detached_runs"
                        records.mkdir(parents=True)
                        _ = (run / "progress_history_state.json").write_text(json.dumps({"owner_staff_id": self.owner.id}))
                        _ = staff.set_run(self.owner.id, staff.OpenRun(run))
                        path = records / "gate.json"
                        script = run / "detached_runs.py"
                        _ = script.write_text("import sys\nsys.stdin.buffer.read()\n")
                        process = subprocess.Popen([sys.executable, str(script), "supervise", str(path)],
                            cwd=self.checkout, stdin=subprocess.PIPE)
                        _ = scope.callback(process.communicate, timeout=TEST_HANG_GUARD_SECS)
                        commands.append(process)
                        _ = path.write_text(json.dumps({"status": "running", "pid": process.pid}))
                        if state == "stale detached record":
                            _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                    backend = FolderSessionFixture(self.owner, live=True)
                    with backend.installed(), mock.patch.object(shutdown, "rename_repair_displays", return_value=False):
                        result = self.command()
                        self.assertEqual(result.returncode, 0, result.stderr)
                        if state == "live detached run":
                            self.assertIn("folder and branch kept", result.stdout)
                            self.assertIsNone(commands[0].poll())
                            self.assertEqual(self.git("branch", "--show-current", cwd=self.source), "cargo-liner-source")
                            self.assertEqual((backend.starts, backend.stops), ([], []))
                            _ = commands[0].communicate(timeout=TEST_HANG_GUARD_SECS)
                            self.assertEqual(self.command().returncode, 0)
                    self.assertEqual((self.destination / "kept.txt").read_text(), "Keep the uncommitted edit.\n")
                    self.assertEqual((self.destination / "draft.md").read_text(), "Keep the untracked draft.\n")
                    self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.destination), tip)
                    self.assertEqual((backend.starts, backend.stops), ([], []))
                    self.assertTrue(backend.live)

    def test_existing_target_refuses_without_overwriting_any_file(self) -> None:
        from scripts.agents import shutdown

        self.destination.mkdir()
        marker = self.destination / "owned-by-another-unit.txt"
        _ = marker.write_text("Keep this destination.\n")
        before = file_versions(self.destination)
        backend = FolderSessionFixture(self.owner, live=True)
        with backend.installed(), mock.patch.object(shutdown, "rename_repair_displays", return_value=False):
            result = self.command()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(file_versions(self.destination), before)
        self.assertTrue(self.source.is_dir())
        row = staff.by_id(self.owner.id)
        assert row is not None
        self.assertEqual(row.worktree, self.owner.worktree)

    def test_same_row_lock_refuses_without_stopping_or_moving(self) -> None:
        import fcntl
        from scripts.agents import shutdown

        backend = FolderSessionFixture(self.owner, live=True)
        lock = self.staff_file.parent / "rows" / f"{self.owner.id}.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        with (backend.installed(), mock.patch.object(shutdown, "rename_repair_displays", return_value=False),
              lock.open("a") as handle):
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = file_versions(self.root)
            result = self.command()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((backend.starts, backend.stops), ([], []))
        self.assertEqual(file_versions(self.root), before)
        self.assertTrue(self.source.is_dir())
        self.assertFalse(self.destination.exists())

    def test_stopped_unit_moves_its_row_and_branch_and_stays_stopped(self) -> None:
        from scripts.agents import shutdown

        backend = FolderSessionFixture(self.owner, live=False)
        with backend.installed(), mock.patch.object(shutdown, "rename_repair_displays", return_value=False):
            result = self.command()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(backend.starts, [])
            self.assertFalse(backend.live)
            self.assertEqual(staff.by_id(self.owner.id), replace(self.owner, name="destination",
                worktree=staff.PresentWorktree(remote.local_machine(), self.destination)))
            self.assertEqual(self.git("branch", "--show-current", cwd=self.destination), "cargo-liner-destination")
            before = file_versions(self.root)
            rerun = self.command()
            self.assertEqual((rerun.returncode, rerun.stdout, rerun.stderr), (0, "unchanged\n", ""))
            self.assertEqual(file_versions(self.root), before)
            self.assertEqual((backend.starts, backend.stops), ([], []))
            self.assertFalse(backend.live)


class FolderRenameConfigTests(FolderRenameFixture):
    def test_each_git_call_reads_the_changed_command_limit_and_obeys_the_deadline(self) -> None:
        from scripts.agents import worker_lifecycle
        from scripts.lint.memory_test_support import write_config_fixture
        from scripts.production import unit_move as moving

        for configured, remaining, expected in ((37, 100, 37), (11, 100, 11), (11, 4, 4)):
            with self.subTest(configured=configured, remaining=remaining):
                _ = write_config_fixture(self.configuration, f"RENAME_COMMAND_TIMEOUT_SECONDS={configured}\n")
                response = subprocess.CompletedProcess(["git"], 0, "kept\n", "")
                with (mock.patch.object(time, "monotonic", return_value=100),
                      worker_lifecycle.operation_deadline(deadline=100 + remaining),
                      mock.patch.object(subprocess, "run", return_value=response) as command):
                    self.assertEqual(moving.git(self.source, "status", "--porcelain"), "kept")
                self.assertEqual(command.call_args.kwargs["timeout"], expected)

    def test_changed_wait_and_poll_limits_take_effect_at_each_use(self) -> None:
        from scripts.lint.memory_test_support import write_config_fixture
        from scripts.production import unit_move as moving

        wait_for_rows = cast(Callable[..., bool], getattr(moving, "_wait_folder_rows"))
        for running in (False, True):
            for limit, poll in ((3, 1), (2, 0.5)):
                with self.subTest(running=running, limit=limit, poll=poll):
                    key = "RENAME_LIVE_WAIT_SECONDS" if running else "RENAME_RUN_STOP_WAIT_SECONDS"
                    _ = write_config_fixture(self.configuration, f"{key}={limit}\nRENAME_POLL_SECONDS={poll}\n")
                    clock = [100.0]
                    delays: list[float] = []
                    def sleep(seconds: float) -> None:
                        self.assertGreater(seconds, 0)
                        self.assertLessEqual(seconds, poll)
                        delays.append(seconds)
                        clock[0] += seconds
                    def observe() -> set[staff.StaffId]:
                        return set() if running else {self.owner.id}
                    with (mock.patch.object(time, "monotonic", side_effect=lambda: clock[0]),
                          mock.patch.object(time, "sleep", side_effect=sleep)):
                        self.assertFalse(wait_for_rows(observe, {self.owner.id}, running=running))
                    self.assertEqual(sum(delays), limit)
                    self.assertEqual(delays, [poll] * int(limit / poll))

    def test_the_operation_deadline_shortens_both_waits_and_the_poll_delay(self) -> None:
        from scripts.agents import worker_lifecycle
        from scripts.lint.memory_test_support import write_config_fixture
        from scripts.production import unit_move as moving

        wait_for_rows = cast(Callable[..., bool], getattr(moving, "_wait_folder_rows"))
        _ = write_config_fixture(self.configuration,
            "RENAME_LIVE_WAIT_SECONDS=20\nRENAME_RUN_STOP_WAIT_SECONDS=20\nRENAME_POLL_SECONDS=10\n")
        for running in (False, True):
            with self.subTest(running=running):
                clock = [100.0]
                delays: list[float] = []
                def sleep(seconds: float) -> None:
                    delays.append(seconds)
                    clock[0] += seconds
                def observe() -> set[staff.StaffId]:
                    return set() if running else {self.owner.id}
                with (mock.patch.object(time, "monotonic", side_effect=lambda: clock[0]),
                      mock.patch.object(time, "sleep", side_effect=sleep),
                      worker_lifecycle.operation_deadline(deadline=101.5)):
                    self.assertFalse(wait_for_rows(observe, {self.owner.id}, running=running))
                self.assertEqual(delays, [1.5])

    def test_expired_deadline_runs_no_git_command(self) -> None:
        from scripts.agents import worker_lifecycle
        from scripts.production import unit_move as moving

        with (mock.patch.object(time, "monotonic", return_value=100),
              worker_lifecycle.operation_deadline(deadline=99),
              mock.patch.object(subprocess, "run") as command):
            with self.assertRaises(worker_lifecycle.OperationDeadlineExpired):
                _ = moving.git(self.source, "status", "--porcelain")
        command.assert_not_called()


class FolderMoveReviewTests(FolderRenameFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        from scripts.agents import shutdown

        _ = self.enterContext(mock.patch.object(shutdown, "rename_send_notice"))

    def test_f005_transfer_uses_the_same_staff_row_lock(self) -> None:
        import fcntl
        import io
        from contextlib import redirect_stdout
        from zoneinfo import ZoneInfo
        from scripts.production import staff_move, transfer_unit
        from scripts.production.production_doc import Production
        from scripts.production.staff_test_support import fixture_member

        parent = fixture_member("showrunner", name="receiving")
        production = Production(self.checkout / "receiving-production.md", "receiving", "main",
                                self.checkout, self.checkout / "production.log", ZoneInfo("UTC"), parent)
        request = transfer_unit.UnitTransfer(self.owner, parent, production, "source")
        lock = self.staff_file.parent / "rows" / f"{self.owner.id}.lock"
        def move_projection(*_arguments: object, **_options: object) -> None:
            with lock.open("a") as handle:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (mock.patch.object(transfer_unit, "build", return_value=request),
              mock.patch.object(staff, "row_operation", wraps=staff.row_operation) as row_lock,
              mock.patch.object(staff_move, "move_unit", side_effect=move_projection) as projection,
              redirect_stdout(io.StringIO())):
            self.assertEqual(transfer_unit.apply("folder-production", "source", production.name), request)
        row_lock.assert_called_once_with(str(self.owner.id))
        projection.assert_called_once()
        self.assertEqual(staff.by_id(self.owner.id), replace(self.owner, parent=staff.ReportsTo(parent.id)))

    def test_f006_hard_kill_states_finish_without_cleanup_or_automatic_starts(self) -> None:
        from scripts.agents import shutdown

        for step, explicit_branch in (("stop", ""), ("move", ""), ("publish", ""), ("move", "feature")):
            with self.subTest(step=step, explicit_branch=explicit_branch), saved_fixture(self.root) as restore, ExitStack() as recovery:
                _ = recovery.callback(restore)
                arguments = ["rename", f"folder-production/{self.old_name}", self.new_name]
                if explicit_branch:
                    arguments.extend(("--to-branch", explicit_branch))

                def rename_command() -> subprocess.CompletedProcess[str]:
                    return invoke_cli(shutdown, lambda: shutdown.main(arguments), arguments, dict(os.environ), self.checkout)

                worker = fixture_worker("stopped", self.owner.id)
                _ = self.git("worktree", "lock", "--reason", f"staff:{self.owner.id}", str(self.source))
                if step in ("move", "publish"):
                    _ = self.git("worktree", "move", "--force", "--force", str(self.source), str(self.destination))
                if step == "publish":
                    _ = self.git("branch", "-m", "cargo-liner-destination", cwd=self.destination)
                    _ = staff.set_worktree(self.owner.id, staff.PresentWorktree(remote.local_machine(), self.destination),
                                           expected=self.owner.worktree)
                backend = FolderSessionFixture(self.owner, live=False)
                with backend.installed(), mock.patch.object(shutdown, "rename_repair_displays", return_value=False):
                    result = rename_command()
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(self.source.exists())
                    self.assertEqual(self.git("branch", "--show-current", cwd=self.destination), explicit_branch or "cargo-liner-destination")
                    self.assertEqual(staff.by_id(self.owner.id), replace(self.owner, name="destination",
                        worktree=staff.PresentWorktree(remote.local_machine(), self.destination)))
                    self.assertEqual(staff.by_id(worker.id), worker)
                    self.assertEqual((backend.starts, backend.stops), ([], []))
                    self.assertFalse(backend.live)
                    for row in (self.owner, worker):
                        command = f"agents restart --only {row.id}"
                        lines = [line for line in (result.stdout + result.stderr).splitlines() if command in line]
                        self.assertEqual(len(lines), 1, result.stdout + result.stderr)
                        self.assertIn("info", lines[0].lower())
                    before = file_versions(self.root)
                    rerun = rename_command()
                    self.assertEqual(rerun.returncode, 0, rerun.stderr)
                    self.assertEqual(rerun.stdout, "unchanged\n")
                    self.assertEqual(rerun.stderr, "")
                    self.assertNotIn("agents restart", rerun.stdout + rerun.stderr)
                    self.assertEqual(file_versions(self.root), before)
                    self.assertEqual((backend.starts, backend.stops), ([], []))

    def test_rename_keeps_the_live_session_without_a_start(self) -> None:
        from scripts.lint.memory_test_support import write_config_fixture
        from scripts.agents import shutdown

        _ = write_config_fixture(self.configuration, "RENAME_LIVE_WAIT_SECONDS=0.01\nRENAME_POLL_SECONDS=0.001\n")
        restore = self.enterContext(saved_fixture(self.root))
        for state in ("idle", "native director and MCP", "native director and MCP with worker",
                      "native director and MCP with worker caller", "native director and MCP with worker identity unknown"):
            with self.subTest(state=state), ExitStack() as scope:
                restore()
                backend = FolderSessionFixture(self.owner, live=True)
                workers = [fixture_worker("active", self.owner.id)] if "with worker" in state else []
                if state.startswith("native director and MCP"):
                    caller = os.getpid()
                    parents = {caller: 998044, 998044: 998040, 998040: 1,
                               998041: 998040, 998042: 998041, 998043: 998041, 998051: 998041}
                    if state.endswith("worker caller"):
                        parents[998044] = 998051
                    def parent(pid: int) -> int:
                        return parents.get(pid, 0)
                    def conversation(identity: str) -> herdr.LiveConversation:
                        return herdr.LiveConversation("claude", (998051 if workers and identity == workers[0].current_session else 998041,))
                    def native(pid: int, _kind: str) -> herdr.NativeAgentProcess:
                        if pid == 998051 and state.endswith("identity unknown"):
                            raise ValueError("fixture worker identity is unknown")
                        identity = workers[0].current_session if workers and pid == 998051 else self.owner.current_session
                        return herdr.NativeAgentProcess(herdr.EnvironmentThread(identity), "fixture-start", {})
                    _ = scope.enter_context(mock.patch.object(unit_move, "processes_inside",
                        return_value=[caller, 998040, 998041, 998042, 998043, 998044, *([998051] if workers else [])]))
                    _ = scope.enter_context(mock.patch.object(pane_record, "process_parent", side_effect=parent))
                    _ = scope.enter_context(mock.patch.object(herdr, "read_conversation", side_effect=conversation))
                    _ = scope.enter_context(mock.patch.object(herdr, "native_process", side_effect=native))
                with (backend.installed(), mock.patch.object(shutdown, "rename_repair_displays", return_value=False),
                      mock.patch.object(restart, "start_row", side_effect=OSError("fixture launch failure")) as start):
                    result = self.command()
                self.assertEqual(result.returncode, 0, result.stderr)
                start.assert_not_called()
                command = f"agents restart --only {self.owner.id}"
                lines = [line for line in (result.stdout + result.stderr).splitlines() if command in line]
                self.assertEqual(lines, [])
                self.assertTrue(backend.live)
                self.assertEqual(backend.starts, [])
                self.assertEqual(backend.stops, [])
                if workers:
                    self.assertTrue(self.source.is_dir())
                    self.assertFalse(self.destination.exists())
                    self.assertEqual(self.git("branch", "--show-current", cwd=self.source), "cargo-liner-source")
                    self.assertEqual(staff.by_id(self.owner.id), replace(self.owner, name="destination"))
                    self.assertEqual(staff.by_id(workers[0].id), workers[0])
                    self.assertIn("folder and branch kept", result.stdout)
                else:
                    self.assertFalse(self.source.exists())
                    self.assertTrue(self.destination.is_dir())
                    self.assertEqual(self.git("branch", "--show-current", cwd=self.destination), "cargo-liner-destination")
                    self.assertEqual(staff.by_id(self.owner.id), replace(self.owner, name="destination",
                        worktree=staff.PresentWorktree(remote.local_machine(), self.destination)))

    def test_f007_no_lifecycle_call_starts_after_the_shared_deadline(self) -> None:
        from scripts.agents import worker_lifecycle

        _ = fixture_worker("idle-worker", self.owner.id)
        running = {self.owner.id}
        clock = [100.0]
        calls: list[tuple[str, staff.StaffId, float]] = []
        def observe(rows: Iterable[staff.StaffRow], _record: staff.StaffSnapshot) -> list[staff.StaffRow]:
            return [row for row in rows if row.id in running]
        def stop(row: staff.StaffRow, _client: object, **_options: object) -> bool:
            calls.append(("stop", row.id, clock[0]))
            running.remove(row.id)
            clock[0] += 1
            return True
        def start(row: staff.StaffRow, _record: staff.StaffSnapshot, _client: object, **_options: object) -> str:
            calls.append(("start", row.id, clock[0]))
            running.add(row.id)
            return "fixture-pane"
        with (mock.patch.object(time, "monotonic", side_effect=lambda: clock[0]),
              worker_lifecycle.operation_deadline(deadline=100.25),
              mock.patch.object(restart, "live_members", side_effect=observe),
              mock.patch.object(restart, "stop_row", side_effect=stop),
              mock.patch.object(restart, "start_row", side_effect=start),
              mock.patch.object(os, "kill", side_effect=AssertionError("No real signal is allowed"))):
            try:
                _ = unit_move.move_folder(self.owner.id, self.destination, branch="cargo-liner-destination")
            except worker_lifecycle.OperationDeadlineExpired:
                pass
        self.assertEqual(calls, [("stop", self.owner.id, 100.0)])
        self.assertTrue(self.source.exists())
        self.assertFalse(self.destination.exists())
        self.assertEqual(staff.by_id(self.owner.id), self.owner)

    def test_f007_late_observations_never_count_as_success(self) -> None:
        from scripts.agents import worker_lifecycle

        wait_for_rows = cast(Callable[..., bool], getattr(unit_move, "_wait_folder_rows"))
        for running in (False, True):
            with self.subTest(running=running):
                clock = [100.0]
                def observe() -> set[staff.StaffId]:
                    clock[0] = 101.0
                    return {self.owner.id} if running else set()
                with (mock.patch.object(time, "monotonic", side_effect=lambda: clock[0]),
                      worker_lifecycle.operation_deadline(deadline=100.25)):
                    self.assertFalse(wait_for_rows(observe, {self.owner.id}, running=running))
        with (mock.patch.object(time, "monotonic", return_value=100.0),
              worker_lifecycle.operation_deadline(deadline=99.0)):
            expired_observe = mock.Mock(return_value={self.owner.id})
            try:
                settled = wait_for_rows(expired_observe, {self.owner.id}, running=True)
            except worker_lifecycle.OperationDeadlineExpired:
                settled = False
            self.assertFalse(settled)
            expired_observe.assert_not_called()

    def test_f009_missing_source_refuses_another_worktree_of_the_same_repository(self) -> None:
        _ = self.git("worktree", "remove", "--force", "--force", str(self.source))
        _ = self.git("worktree", "add", "-b", "another", str(self.destination))
        self.assertEqual(self.git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=self.destination),
                         self.git("rev-parse", "--path-format=absolute", "--git-common-dir"))
        before = file_versions(self.root)
        backend = FolderSessionFixture(self.owner, live=False)
        arguments = ["move-folder", "--unit", str(self.owner.id), "--to", str(self.destination)]
        with backend.installed():
            result = invoke_cli(unit_move, lambda: unit_move.main(arguments), arguments, dict(os.environ), self.checkout)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("destination already exists", result.stderr)
        self.assertEqual(staff.by_id(self.owner.id), self.owner)
        self.assertEqual(file_versions(self.root), before)
        self.assertEqual((backend.starts, backend.stops), ([], []))

    def test_f009_pending_move_refuses_another_worktree_on_the_requested_branch(self) -> None:
        from scripts.production.rename_test_support import RenameDisplays

        _ = self.git("worktree", "lock", "--reason", f"staff:{self.owner.id}", str(self.source))
        renamed = self.owner
        _ = self.git("worktree", "remove", "--force", "--force", str(self.source))
        _ = self.git("worktree", "add", "-b", "cargo-liner-destination", str(self.destination))
        _ = (self.destination / "foreign-work.txt").write_text("Another unit owns this work.\n")
        registered = self.git("worktree", "list", "--porcelain")
        before = file_versions(self.root)
        backend = FolderSessionFixture(renamed, live=False)
        displays = RenameDisplays(renamed)
        displays.pane = displays.tab = renamed.name
        with backend.installed(), displays.installed():
            result = self.command()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("destination already exists", result.stderr)
        self.assertEqual(staff.by_id(renamed.id), renamed)
        self.assertEqual(file_versions(self.root), before)
        self.assertEqual(self.git("worktree", "list", "--porcelain"), registered)
        self.assertEqual((backend.starts, backend.stops), ([], []))
        self.assertEqual(displays.commands, [])

    def test_f019_folder_move_and_rename_recover_as_distinct_pending_operations(self) -> None:
        from scripts.production.rename_test_support import RenameDisplays

        for operation in ("folder", "rename"):
            with self.subTest(operation=operation), saved_fixture(self.root) as restore, ExitStack() as recovery:
                _ = recovery.callback(restore)
                _ = self.git("worktree", "lock", "--reason", f"staff:{self.owner.id}", str(self.source))
                _ = self.git("worktree", "move", "--force", "--force", str(self.source), str(self.destination))
                backend = FolderSessionFixture(self.owner, live=False)
                displays = RenameDisplays(self.owner)
                displays.pane = displays.tab = self.owner.name
                with backend.installed(), displays.installed():
                    if operation == "folder":
                        arguments = ["move-folder", "--unit", str(self.owner.id), "--to", str(self.destination)]
                        result = invoke_cli(unit_move, lambda: unit_move.main(arguments), arguments,
                                            dict(os.environ), self.checkout)
                    else:
                        result = self.command()
                self.assertEqual((result.returncode, result.stderr), (0, ""))
                self.assertEqual(result.stdout.splitlines()[-1], "done")
                name = "source" if operation == "folder" else "destination"
                self.assertEqual(staff.by_id(self.owner.id), replace(self.owner, name=name,
                    worktree=staff.PresentWorktree(remote.local_machine(), self.destination)))
                self.assertEqual(self.git("branch", "--show-current", cwd=self.destination), f"cargo-liner-{name}")
                self.assertEqual((displays.pane, displays.tab), (name, name))
                self.assertEqual((self.destination / "kept.txt").read_text(), "Keep this work.\n")
                self.assertEqual((backend.starts, backend.stops), ([], []))

    def test_git_timeout_reports_a_rename_error_and_a_move_anomaly(self) -> None:
        from scripts.agents import shutdown

        real_git = unit_move.git
        def git(folder: Path, *arguments: str) -> str:
            if arguments[:2] == ("worktree", "move"):
                raise subprocess.TimeoutExpired(["git", *arguments], 0.01)
            return real_git(folder, *arguments)
        for entry in ("unit_move", "rename"):
            with self.subTest(entry=entry), saved_fixture(self.root) as restore, ExitStack() as recovery:
                _ = recovery.callback(restore)
                backend = FolderSessionFixture(self.owner, live=False)
                with (backend.installed(), mock.patch.object(unit_move, "git", side_effect=git),
                      mock.patch.object(shutdown, "rename_repair_displays", return_value=False)):
                    if entry == "rename":
                        result = self.command()
                    else:
                        arguments = ["move-folder", "--unit", str(self.owner.id), "--to", str(self.destination)]
                        result = invoke_cli(unit_move, lambda: unit_move.main(arguments), arguments,
                                            dict(os.environ), self.checkout)
                if entry == "rename":
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn("rename: error:", result.stderr)
                    self.assertEqual(len(result.stderr.splitlines()), 1)
                else:
                    self.assertIn("anomaly", result.stderr.lower())
                self.assertNotIn("Traceback", result.stdout + result.stderr)
                self.assertTrue(self.source.exists())
                self.assertFalse(self.destination.exists())

    def test_git_timeout_reports_a_transfer_error_without_a_traceback(self) -> None:
        from scripts.production import transfer_unit

        arguments = ["apply", "demo", "alpha", str(self.checkout / "production.md")]
        before = file_versions(self.root)
        with mock.patch.object(transfer_unit, "apply", side_effect=subprocess.TimeoutExpired(["git", "status"], 0.01)):
            result = invoke_cli(transfer_unit, lambda: transfer_unit.main(arguments), arguments,
                                dict(os.environ), self.checkout)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("transfer_unit: error:", result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertEqual(file_versions(self.root), before)


if __name__ == "__main__":
    _ = unittest.main()
