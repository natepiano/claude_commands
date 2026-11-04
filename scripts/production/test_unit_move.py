"""Move a unit folder and resume its director with the worker commands."""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import shutil
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr
from dataclasses import replace
from pathlib import Path
from typing import cast, final
from unittest import mock

from scripts.accounts import accounts
from scripts.agents import codex_mesh
from scripts.production import staff
from scripts.direct import progress_history
from scripts.production.cli_fixture import invoke_cli
from scripts.production.rename_test_support import FolderMoveBackend, FolderMoveFixture
from scripts.production.add_unit_test_support import AddUnitTestCase
from scripts.production.staff_test_support import fixture_runtime_id, fixture_worker
from scripts.shutdown import pane_record, restart, stop_resume
from scripts.message import sessions
import herdr
import unit_move
from rename_state import RenameRefused


class AddUnitResumeFolderTests(AddUnitTestCase):
    def test_resuming_a_stopped_unit_tab_retires_the_old_record_before_a_folder_move(self) -> None:
        import add_unit
        import unit_lookup
        from scripts.shutdown.test_pane_restore import FakeClient
        from datetime import datetime, timezone

        old = self.root / "project-alpha"
        owner = self.prepare_restored_worktree("alpha", old)
        records = self.root / "move-launches"
        self.env["HERDR_PANE_RECORD_DIR"] = str(records)
        config = Path(self.env["HOME"]) / ".claude2"
        original = pane_record.launch("w7:p1", owner.current_session, str(old),
            {"CLAUDE_CONFIG_DIR": str(config)}, ["claude", "-n", "alpha", "--model", "opus"])
        client = FakeClient([{"pane_id": "w7:p1", "tab_id": "w7:t1", "workspace_id": "w7", "label": "alpha"}])
        marked = unit_lookup.MarkedUnit("alpha-unit", unit_lookup.ClaudeNotRunning(),
                                      herdr.HerdrPlace("agents", "w7:p1"))

        def launch(request: add_unit.UnitLaunch, _director: add_unit.DirectorAgent, _prompt: str,
                   _account: accounts.ClaudeLaunchAccount, *, target: str = "", model: str = "",
                   autocompact_window: str = "") -> herdr.PromptSubmitted:
            _ = (target, model, autocompact_window)
            replacement = pane_record.launch("w7:p3", owner.current_session, str(request.worktree),
                {"CLAUDE_CONFIG_DIR": str(config)}, ["claude", "--resume", owner.current_session])
            pane_record.write("agents", replacement)
            return herdr.PromptSubmitted(datetime.now(timezone.utc))

        with (mock.patch.object(pane_record, "RECORD_DIR", records),
              mock.patch("pane_record.RECORD_DIR", records),
              mock.patch.dict(os.environ, self.env)):
            pane_record.write("agents", original)
            with (mock.patch.object(add_unit, "launched_unit", return_value=add_unit.MarkedUnitFound(marked)),
                  mock.patch.object(add_unit, "launch_session", side_effect=launch),
                  mock.patch.object(add_unit, "wait_for_remote_control", return_value=marked),
                  mock.patch.object(herdr, "Herdr", return_value=client)):
                resumed = self.cli("alpha", "--plan", "docs/plans/given.md", "--resume", owner.current_session,
                                   "--cwd", str(old), "--model", "opus")
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            destination = self.root / "moved-alpha"
            arguments = ["move-folder", "--unit", owner.id, "--to", str(destination)]
            with (mock.patch.object(herdr.Herdr, "panes", return_value=[]),
                  mock.patch.object(herdr, "herdr_processes", return_value=[]),
                  mock.patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive())):
                moved = invoke_cli(unit_move, lambda: unit_move.main(arguments), arguments, self.env, self.checkout)
            self.assertEqual(moved.returncode, 0, moved.stderr)
            publications = [entry for entry in pane_record.read("agents").values()
                            if entry["session_id"] in (owner.id, owner.current_session)]
            self.assertEqual([(entry["pane_id"], pane_record.resume_launch(entry)["cwd"]) for entry in publications],
                             [("w7:p3", str(destination))])


@final
class UnitFolderMoveTests(FolderMoveFixture):
    def move_folder(self) -> subprocess.CompletedProcess[str]:
        arguments = ["move-folder", "--unit", self.owner.id, "--to", str(self.new_folder)]
        return invoke_cli(unit_move, lambda: unit_move.main(arguments), arguments, dict(os.environ), self.checkout)

    def assert_moved(self, backend: FolderMoveBackend) -> None:
        self.assertFalse(self.old_folder.exists())
        self.assertTrue((self.new_folder / ".git").is_file())
        self.assertEqual(self.folder_git(self.new_folder, "branch", "--show-current"), "unit-branch")
        self.assertEqual(self.folder_git(self.new_folder, "rev-parse", "--show-toplevel"), str(self.new_folder))
        self.assertEqual(staff.by_id(self.owner.id),
                         replace(self.owner, worktree=staff.PresentWorktree("natedev", self.new_folder)))
        launch = pane_record.read("agents")["w1:p1"]
        self.assertEqual((pane_record.resume_launch(launch)["cwd"], launch["session_id"], launch["name"]),
                         (str(self.new_folder), self.owner.id, "kept-name"))
        state = cast(dict[str, object], json.loads((self.run_dir / "progress_history_state.json").read_text()))
        event = cast(dict[str, object], json.loads(self.history.read_text().splitlines()[0]))
        for fields in (state, event):
            self.assertEqual((fields["working_dir"], fields["branch"], fields["plan_doc"], fields["run_id"]),
                             (str(self.old_folder), "unit-branch", str(self.old_folder / "plan.md"), "move-run"))
        current = cast(Callable[[Path], dict[str, object]], getattr(progress_history, "_read_state"))(self.run_dir)
        self.assertEqual(current["working_dir"], str(self.new_folder))
        self.assertEqual((Path(str(current["working_dir"])) / str(current["plan_doc"])), self.new_folder / "plan.md")
        self.assertEqual(tuple((pane["pane_id"], pane.get("terminal_id")) for pane in backend.panes()),
                         (("w1:p1", "kept-terminal"), ("w1:p2", "neighbour-terminal")))

    def move_files(self, *extra: Path) -> tuple[Path, ...]:
        return (self.staff_file, self.records / "agents.json", self.history,
                self.run_dir / "progress_history_state.json", *extra)

    def read_rows(self) -> dict[str, dict[str, object]]:
        return cast(dict[str, dict[str, object]], json.loads((self.run_dir / "mesh_roster.json").read_text()))

    def assert_finished_rerun(self, backend: FolderMoveBackend) -> None:
        paths = self.move_files(*sorted(path for path in self.run_dir.rglob("*")
                                       if path.is_file() and path.suffix != ".lock"))
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
        starts, stops = backend.starts.copy(), backend.stops.copy()
        with mock.patch.object(unit_move, "pause_folder_workers",
                               side_effect=AssertionError("A completed move must not pause workers")):
            result = self.move_folder()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "unchanged\n", ""))
        self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in paths], before)
        self.assertEqual((backend.starts, backend.stops), (starts, stops))

    def test_running_unit_stops_pauses_moves_and_resumes_with_its_worker_command(self) -> None:
        backend = self.folder_backend(live=True)
        thread = fixture_runtime_id("folder-worker")
        self.write_json(self.run_dir / "mesh_roster.json", {"impl1": {
            "thread_id": thread, "role": "impl", "status": "running",
            "cwd": str(self.old_folder), "paused_turn": True,
        }, "review1": {
            "thread_id": fixture_runtime_id("folder-reviewer"), "role": "review", "status": "paused",
            "cwd": str(self.old_folder), "paused_turn": True,
        }})
        worker_prompt = self.run_dir / "impl1.resume-prompt.md"
        task = "Keep the existing worker task.\n"
        _ = worker_prompt.write_text(task)
        reviewer_prompt = self.run_dir / "review1.resume-prompt.md"
        self.assertFalse(reviewer_prompt.exists())
        queued = self.run_dir / "queued-message.md"
        _ = queued.write_text("Continue the existing task.\n")
        events: list[str] = []
        stop = unit_move.stop_folder_session
        move_git = unit_move.git
        set_worktree = staff.set_worktree
        resume = backend.run_in_pane

        def stopped(row: staff.StaffRow, launch: unit_move.FolderLaunch) -> bool:
            result = stop(row, launch)
            self.assertTrue(result)
            events.append("stop")
            return result

        def pause(arguments: argparse.Namespace) -> int:
            self.assertEqual(events, ["stop"])
            self.assertFalse(backend.live)
            self.assertTrue(self.old_folder.is_dir())
            self.assertEqual(vars(arguments), {"session_dir": str(self.run_dir), "pause": True})
            rows = self.read_rows()
            rows["impl1"]["status"] = "paused"
            self.write_json(self.run_dir / "mesh_roster.json", rows)
            events.append("pause")
            return 0

        def git(checkout: Path, *arguments: str) -> str:
            result = move_git(checkout, *arguments)
            if arguments[:2] == ("worktree", "move"):
                self.assertEqual(events, ["stop", "pause"])
                events.append("move")
            return result

        def publish(unit: staff.StaffId, worktree: staff.PresentWorktree,
                    *, expected: staff.PresentWorktree | staff.RemovedWorktree | staff.WorktreeUnfilled) -> staff.StaffRow:
            self.assertEqual(events, ["stop", "pause", "move"])
            self.assertFalse(self.old_folder.exists())
            self.assertEqual(pane_record.read("agents")["w1:p1"]["cwd"], str(self.old_folder))
            self.assertEqual((self.run_dir / "working_dir").read_text().strip(), str(self.old_folder))
            result = set_worktree(unit, worktree, expected=expected)
            events.append("staff")
            return result

        def resumed(pane: str, command: list[str]) -> None:
            self.assertEqual(events, ["stop", "pause", "move", "staff"])
            selected = staff.by_id(self.owner.id)
            assert selected is not None
            self.assertEqual(selected.worktree,
                             staff.PresentWorktree("natedev", self.new_folder))
            resume(pane, command)
            events.append("resume")

        with (mock.patch.object(unit_move, "stop_folder_session", side_effect=stopped),
              mock.patch.object(codex_mesh, "command_stop", side_effect=pause) as paused,
              mock.patch.object(codex_mesh, "open_unit_worker",
                                side_effect=AssertionError("The director resumes the worker")),
              mock.patch.object(unit_move, "git", side_effect=git),
              mock.patch.object(staff, "set_worktree", side_effect=publish),
              mock.patch.object(backend, "run_in_pane", side_effect=resumed),
              mock.patch("unit_move.Popen") as workers):
            result = self.move_folder()
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(events, ["stop", "pause", "move", "staff", "resume"])
        self.assert_moved(backend)
        self.assertEqual(backend.stops, [self.owner.current_session])
        (pane, command), = backend.starts
        self.assertEqual(pane, "w1:p1")
        self.assertEqual(command[command.index("--resume") + 1], self.owner.current_session)
        prompt = command[-1]
        self.assertIn(f"moved from {self.old_folder} to {self.new_folder}", prompt)
        self.assertEqual(workers.call_count, 2)
        self.assertEqual([command[command.index("--to") + 1]
                          for call in workers.call_args_list for command in [cast(list[str], call.args[0])]], ["impl1", "review1"])
        self.assertTrue(all(call.kwargs["cwd"] == self.new_folder for call in workers.call_args_list))
        note = (f"Your unit folder moved from {self.old_folder} to {self.new_folder}. "
                f"Use {self.new_folder} for commands. Continue your current task.\n")
        self.assertEqual(worker_prompt.read_text(), task + note)
        self.assertEqual(reviewer_prompt.read_text(), note)
        self.assertIn(str(self.new_folder), prompt)
        self.assertEqual(self.read_rows()["impl1"]["thread_id"], thread)
        self.assertEqual(self.read_rows()["impl1"]["status"], "paused")
        self.assertEqual(queued.read_text(), "Continue the existing task.\n")
        paused.assert_called_once()
        prompt_files = (worker_prompt, reviewer_prompt)
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in prompt_files]
        launch = unit_move.folder_launch(self.owner)
        self.assertIsInstance(launch, unit_move.FolderLaunch)
        assert isinstance(launch, unit_move.FolderLaunch)
        with redirect_stderr(io.StringIO()):
            unit_move.resume_folder_session(self.owner, launch, self.old_folder, self.new_folder, (self.run_dir,))
        self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in prompt_files], before)
        self.assertEqual(len(backend.starts), 1)
        self.assert_finished_rerun(backend)

    def test_held_move_lock_refuses_without_stopping_or_moving(self) -> None:
        backend = self.folder_backend(live=True)
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in self.move_files()]
        with unit_move.folder_move_lock(self.owner.id):
            result = self.move_folder()
        self.assertEqual(result.returncode, 1)
        self.assertIn("another folder move is running", result.stderr)
        self.assertTrue(self.old_folder.is_dir())
        self.assertFalse(self.new_folder.exists())
        self.assertEqual((backend.starts, backend.stops), ([], []))
        self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in self.move_files()], before)

    def test_existing_destination_reports_git_error_and_resumes_the_stopped_session(self) -> None:
        backend = self.folder_backend(live=True)
        self.new_folder.parent.mkdir(parents=True)
        marker = self.new_folder
        _ = marker.write_text("Keep the destination.\n")
        move_git = unit_move.git
        attempts: list[tuple[str, ...]] = []
        failures: list[str] = []

        def git(checkout: Path, *arguments: str) -> str:
            if arguments[:2] == ("worktree", "move"):
                attempts.append(arguments)
            try:
                return move_git(checkout, *arguments)
            except RenameRefused as error:
                failures.append(str(error))
                raise

        with mock.patch.object(unit_move, "git", side_effect=git):
            result = self.move_folder()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(attempts, [("worktree", "move", str(self.old_folder), str(self.new_folder))])
        failure, = failures
        self.assertIn(failure, result.stderr)
        self.assertIn("already exists", failure)
        self.assertTrue(self.old_folder.is_dir())
        self.assertEqual(staff.by_id(self.owner.id), self.owner)
        self.assertEqual(marker.read_text(), "Keep the destination.\n")
        self.assertEqual(backend.stops, [self.owner.current_session])
        (pane, command), = backend.starts
        self.assertEqual(pane, "w1:p1")
        self.assertEqual(backend.cwd, self.old_folder)
        self.assertEqual(command[command.index("--resume") + 1], self.owner.current_session)

    def test_missing_launch_record_prints_one_line_and_finishes_the_move(self) -> None:
        backend = self.folder_backend(live=True)
        entries = pane_record.read("agents")
        del entries["w1:p1"]
        pane_record.save("agents", entries)
        stopped: list[str] = []
        started: list[str] = []

        def stop(session: str) -> stop_resume.Stopped:
            self.assertEqual(session, self.owner.current_session)
            self.assertTrue(self.old_folder.is_dir())
            self.assertTrue(backend.live)
            backend.live = False
            stopped.append(session)
            return stop_resume.Stopped(session, backend.pid)

        def start(row: staff.StaffRow, record: staff.StaffSnapshot, client: herdr.Herdr) -> None:
            self.assertEqual(row.current_session, self.owner.current_session)
            self.assertEqual(record.by_id(row.id), row)
            self.assertEqual(row.worktree, staff.PresentWorktree("natedev", self.new_folder))
            self.assertTrue(self.new_folder.is_dir())
            self.assertFalse(backend.live)
            self.assertIs(client, backend)
            backend.live = True
            started.append(row.current_session)

        with (mock.patch.object(stop_resume, "stop", side_effect=stop),
              mock.patch.object(restart, "start_row", side_effect=start),
              mock.patch.object(herdr, "shared_session", return_value="agents")):
            result = self.move_folder()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn("no retained launch", result.stderr)
        self.assertFalse(self.old_folder.exists())
        self.assertTrue(self.new_folder.is_dir())
        selected = staff.by_id(self.owner.id)
        assert selected is not None
        self.assertEqual(selected.worktree,
                         staff.PresentWorktree("natedev", self.new_folder))
        self.assertEqual(pane_record.read("agents"), entries)
        self.assertEqual(stopped, [self.owner.current_session])
        self.assertEqual(started, [self.owner.current_session])

    def test_failed_worker_pause_prints_one_line_and_finishes_the_move(self) -> None:
        backend = self.folder_backend(live=True)
        self.write_json(self.run_dir / "mesh_roster.json", {})
        with mock.patch.object(codex_mesh, "command_stop", return_value=1) as pause:
            result = self.move_folder()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn("cannot pause run", result.stderr)
        pause.assert_called_once()
        self.assert_moved(backend)
        self.assertEqual((len(backend.starts), len(backend.stops)), (1, 1))

    def test_crash_after_git_move_finishes_staff_paths_and_starts_the_unit(self) -> None:
        backend = self.folder_backend()
        assert self.owner.parent is not None
        _ = staff.set_doc(self.owner.parent, staff.LiveDoc("natedev", self.checkout / "plan.md"))
        self.new_folder.parent.mkdir(parents=True)
        _ = self.folder_git(self.old_folder, "worktree", "move", str(self.old_folder), str(self.new_folder))
        self.assertEqual(staff.by_id(self.owner.id), self.owner)
        with mock.patch.object(unit_move, "git", wraps=unit_move.git) as calls:
            result = self.move_folder()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertFalse(any(call.args[1:3] == ("worktree", "move") for call in calls.call_args_list))
        self.assert_moved(backend)
        self.assertEqual(backend.stops, [])
        self.assertEqual(len(backend.starts), 1)
        self.assertTrue(backend.live)
        self.assertEqual(backend.cwd, self.new_folder)
        self.assert_finished_rerun(backend)

    def test_unknown_pane_identity_does_not_stop_a_different_conversation(self) -> None:
        backend = self.folder_backend(live=True)
        panes = backend.panes()
        panes[0]["session_id"] = None
        foreground = {"process_info": {"foreground_processes": [
            {"pid": backend.pid, "argv": ["claude", "--resume", fixture_runtime_id("foreign-conversation")]}]}}
        with (mock.patch.object(backend, "panes", return_value=panes),
              mock.patch.object(backend, "result", return_value=foreground),
              mock.patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()),
              mock.patch.object(os, "kill") as kill):
            result = self.move_folder()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        kill.assert_not_called()
        self.assert_moved(backend)
        self.assertEqual((backend.stops, backend.starts, backend.live), ([], [], True))

    def test_unsafe_resume_leaves_the_pane_untouched_and_prints_the_command(self) -> None:
        backend = self.folder_backend()
        launch = unit_move.folder_launch(self.owner)
        assert isinstance(launch, unit_move.FolderLaunch)
        before = (self.records / "agents.json").read_bytes()
        for reason in ("occupied-pane", "session-elsewhere"):
            with self.subTest(reason=reason):
                output = io.StringIO()
                activity = herdr.PaneRunning(("editor",)) if reason == "occupied-pane" else herdr.PaneAtShell()
                live = sessions.SessionNotLive() if reason == "occupied-pane" else sessions.LiveSessionProcess(998099)
                with (mock.patch.object(backend, "pane_activity", return_value=activity),
                      mock.patch.object(sessions, "session_liveness", return_value=live),
                      mock.patch.object(backend, "run_in_pane") as typed,
                      redirect_stderr(output)):
                    unit_move.resume_folder_session(self.owner, launch, self.old_folder, self.old_folder, ())
                typed.assert_not_called()
                self.assertEqual(len(output.getvalue().splitlines()), 1)
                self.assertIn("--resume " + self.owner.current_session, output.getvalue())
                self.assertIn(str(self.old_folder), output.getvalue())
                self.assertEqual((self.records / "agents.json").read_bytes(), before)

    def test_missing_source_does_not_adopt_a_repository_with_another_common_directory(self) -> None:
        backend = self.folder_backend()
        assert self.owner.parent is not None
        _ = staff.set_doc(self.owner.parent, staff.LiveDoc("natedev", self.checkout / "plan.md"))
        shutil.rmtree(self.old_folder)
        self.new_folder.mkdir(parents=True)
        _ = self.folder_git(self.new_folder, "init", "-b", "unrelated")
        self.assertNotEqual(self.folder_git(self.new_folder, "rev-parse", "--path-format=absolute", "--git-common-dir"),
                            self.folder_git(self.checkout, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        before = self.staff_file.read_bytes()
        result = self.move_folder()
        self.assertEqual(result.returncode, 1)
        self.assertIn("git worktree move", result.stderr)
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertEqual(staff.by_id(self.owner.id), self.owner)
        self.assertEqual(self.folder_git(self.new_folder, "branch", "--show-current"), "unrelated")
        self.assertEqual((backend.stops, backend.starts), ([], []))

    def test_each_run_mesh_is_paused_when_the_unit_has_two_histories(self) -> None:
        backend = self.folder_backend()
        second = self.run_dir.parent / "older-run"
        history = self.history.with_name("older-run.jsonl")
        state = cast(dict[str, object], json.loads((self.run_dir / "progress_history_state.json").read_text()))
        state.update(run_id="older-run", history_file=str(history))
        self.write_json(second / "progress_history_state.json", state)
        _ = (second / "director").write_text(self.owner.current_session + "\n")
        _ = history.write_text(json.dumps({**state, "event_type": "run_started", "run_started_at": 0}) + "\n")
        for run in (self.run_dir, second):
            self.write_json(run / "mesh_roster.json", {})
        paused: list[dict[str, object]] = []

        def pause(arguments: argparse.Namespace) -> int:
            paused.append(vars(arguments))
            return 0

        with mock.patch.object(codex_mesh, "command_stop", side_effect=pause):
            result = self.move_folder()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertCountEqual(paused,
                              [{"session_dir": str(run), "pause": True} for run in (self.run_dir, second)])
        self.assert_moved(backend)
        self.assertEqual(backend.stops, [])
        self.assertEqual(len(backend.starts), 1)
        self.assertEqual(backend.cwd, self.new_folder)

    def test_stopped_unit_with_a_closed_pane_moves_records_without_starting(self) -> None:
        backend = self.folder_backend()
        entries = pane_record.read("agents")
        with (mock.patch.object(backend, "panes", return_value=[backend.panes()[1]]),
              mock.patch.object(backend, "pane_activity", side_effect=herdr.HerdrFailure("pane is closed"))):
            result = self.move_folder()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_moved(backend)
        self.assertEqual(pane_record.read("agents"),
                         entries)
        self.assertEqual((backend.starts, backend.stops), ([], []))

    def test_normal_codex_launch_moves_without_a_claude_launch_record(self) -> None:
        client_type = herdr.Herdr
        backend = self.folder_backend("codex")
        entries = pane_record.read("agents")
        del entries["w1:p1"]
        pane_record.save("agents", entries)
        commands: list[list[str]] = []

        def start(command: list[str], kind: str, pane: str, _timeout: float) -> subprocess.CompletedProcess[str]:
            self.assertEqual((kind, pane), ("codex", "w1:p1"))
            commands.append(command.copy())
            backend.live = True
            return subprocess.CompletedProcess(command, 0, "", "")

        with (mock.patch.object(herdr, "Herdr", client_type),
              mock.patch.object(herdr, "open_tab", return_value="w1:p1") as opened,
              mock.patch.object(client_type, "start_command", side_effect=start),
              mock.patch.object(client_type, "result", side_effect=backend.result)):
            launched = herdr.launch_prepared_agent("agents", herdr.NewTab("production", "kept-name"),
                herdr.PreparedAgentEnvironment(self.old_folder, {"CODEX_HOME": str(backend.config)}),
                "codex", ["resume", self.owner.current_session], herdr.NoOpeningPrompt(), 1000,
                agent="kept-name")
        self.assertEqual(len(commands), 1)
        opened.assert_called_once()
        self.assertEqual(launched.pane, "w1:p1")
        entry = pane_record.read("agents")[launched.pane]
        self.assertEqual((entry["cwd"], entry["session_id"], entry["config_dir"]),
                         (str(self.old_folder), self.owner.id, str(backend.config)))
        with mock.patch.object(os, "kill") as killed:
            result = self.move_folder()
        killed.assert_not_called()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_moved(backend)
        self.assertEqual((backend.stops, backend.starts), ([], []))
        self.assertTrue(backend.live)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn(f"codex resume {self.owner.current_session}", result.stderr)
        self.assertEqual(pane_record.read("agents")[launched.pane]["config_dir"], str(backend.config))
        self.assert_finished_rerun(backend)

    def test_open_phase_running_unit_resumes_in_the_moved_folder(self) -> None:
        with self.history.open("a") as history:
            _ = history.write(json.dumps({"event_type": "phase_started",
                "phase_instance_id": "phase-one", "phase_id": "1"}) + "\n")
        backend = self.folder_backend(live=True)
        result = self.move_folder()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_moved(backend)
        self.assertTrue(backend.live)
        self.assertEqual(backend.cwd, self.new_folder)
        self.assertEqual(backend.stops, [self.owner.current_session])
        (pane, command), = backend.starts
        self.assertEqual(pane, "w1:p1")
        self.assertEqual(command[command.index("--resume") + 1], self.owner.current_session)
        self.assertIn(str(self.new_folder), command)
        self.assert_finished_rerun(backend)

    def test_open_phase_paused_unit_starts_after_the_move(self) -> None:
        with self.history.open("a") as history:
            _ = history.write(json.dumps({"event_type": "phase_started",
                "phase_instance_id": "phase-one", "phase_id": "1"}) + "\n")
        backend = self.folder_backend()
        result = self.move_folder()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_moved(backend)
        self.assertEqual(backend.stops, [])
        self.assertEqual(len(backend.starts), 1)
        self.assertTrue(backend.live)
        self.assertEqual(backend.cwd, self.new_folder)
        self.assertIs(pane_record.read("agents")["w1:p1"].get("ended"), False)
        self.assert_finished_rerun(backend)

    def test_stopped_move_starts_the_unit_without_renaming(self) -> None:
        backend = self.folder_backend()
        before = pane_record.read("agents")
        plan = (self.old_folder / "plan.md").read_bytes()
        result = self.move_folder()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_moved(backend)
        before["w1:p1"]["ended"] = False
        self.assertEqual(pane_record.read("agents"), before)
        self.assertEqual((self.new_folder / "plan.md").read_bytes(), plan)
        self.assertEqual(backend.stops, [])
        self.assertEqual(len(backend.starts), 1)
        self.assertTrue(backend.live)
        self.assertEqual(backend.cwd, self.new_folder)
        self.assertFalse(staff.folder_move_path(self.owner.id).exists())
        found = self.cli("get", str(self.new_folder), "--json")
        self.assertEqual(json.loads(found.stdout)["id"], self.owner.id)
        self.assert_finished_rerun(backend)

    def test_rerun_starts_a_director_that_is_down_after_staff_publication(self) -> None:
        backend = self.folder_backend()
        self.new_folder.parent.mkdir(parents=True, exist_ok=True)
        _ = self.folder_git(self.checkout, "worktree", "move", str(self.old_folder), str(self.new_folder))
        _ = staff.set_worktree(self.owner.id, staff.PresentWorktree("natedev", self.new_folder),
                               expected=self.owner.worktree)
        with (mock.patch.object(unit_move, "stop_folder_session",
                                side_effect=AssertionError("A rerun must not stop a session")),
              mock.patch.object(unit_move, "pause_folder_workers",
                                side_effect=AssertionError("A rerun must not pause workers")),
              mock.patch.object(unit_move, "git",
                                side_effect=AssertionError("A rerun must not move the folder"))):
            result = self.move_folder()
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertNotEqual(result.stdout, "unchanged\n")
        self.assertEqual(backend.stops, [])
        self.assertEqual(len(backend.starts), 1)
        self.assertTrue(backend.live)
        self.assertEqual(backend.cwd, self.new_folder)
        self.assert_finished_rerun(backend)

    def test_incomplete_stop_report_still_starts_a_director_that_has_exited(self) -> None:
        backend = self.folder_backend(live=True)
        stop = unit_move.stop_folder_session

        def incomplete(row: staff.StaffRow, launch: unit_move.FolderLaunch) -> bool:
            _ = stop(row, launch)
            raise ValueError("session stop is incomplete")

        with mock.patch.object(unit_move, "stop_folder_session", side_effect=incomplete):
            result = self.move_folder()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("session stop is incomplete", result.stderr)
        self.assertEqual(backend.stops, [self.owner.current_session])
        self.assertEqual(len(backend.starts), 1)
        self.assertTrue(backend.live)
        self.assertEqual(backend.cwd, self.new_folder)

    def test_live_director_with_several_retained_launches_restarts_its_current_session(self) -> None:
        backend = self.folder_backend(live=True)
        entries = pane_record.read("agents")
        current = entries["w1:p1"].copy()
        current["session_id"] = self.owner.current_session
        entries["w1:p1"] = current
        previous = current.copy()
        previous.update(pane_id="w1:p9", session_id=self.owner.id, ended=True)
        entries["w1:p9"] = previous
        pane_record.save("agents", entries)
        result = self.move_folder()
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(backend.stops, [self.owner.current_session])
        self.assertEqual(len(backend.starts), 1)
        self.assertEqual(backend.starts[0][0], "w1:p1")
        self.assertEqual(backend.cwd, self.new_folder)

    def test_folder_move_resumes_the_same_worker_thread_with_the_staff_folder(self) -> None:
        backend = self.folder_backend(live=True)
        worker = fixture_worker("impl1", self.owner.id)
        self.write_json(self.run_dir / "mesh_roster.json", {"impl1": {
            "thread_id": worker.current_session, "role": "impl", "status": "paused", "paused_turn": True}})
        with (mock.patch.object(codex_mesh, "command_stop", return_value=0) as stop,
              mock.patch("unit_move.Popen") as start):
            result = self.move_folder()
        self.assertEqual(result.returncode, 0, result.stderr)
        stop.assert_called_once()
        start.assert_called_once()
        command = cast(list[str], start.call_args.args[0])
        self.assertEqual(command[command.index("--to") + 1], "impl1")
        self.assertEqual(start.call_args.kwargs["cwd"], self.new_folder)
        self.assertIn(str(self.new_folder), command)
        resume = cast(Callable[[str, str, str], dict[str, object]], getattr(codex_mesh, "_resume_params"))
        params = resume(str(self.run_dir), "impl1", worker.current_session)
        self.assertEqual((params["threadId"], params["cwd"]), (worker.current_session, str(self.new_folder)))
        self.assertEqual(self.read_rows()["impl1"]["thread_id"], worker.current_session)
        self.assertTrue(backend.live)

    def test_next_worker_dispatch_uses_the_moved_run_folder(self) -> None:
        from scripts.agents import codex_mesh

        _ = self.folder_backend()
        result = self.move_folder()
        self.assertEqual(result.returncode, 0, result.stderr)
        native = fixture_runtime_id("worker-after-folder-move")
        requests: list[tuple[str, dict[str, object]]] = []

        def request(_client: object, method: str, params: dict[str, object], **_options: object) -> dict[str, object]:
            requests.append((method, params.copy()))
            return {"thread": {"id": native}} if method == "thread/start" else {"turn": {"id": "first-turn"}}

        client = mock.Mock()
        client.configure_mock(**{"call.return_value": {"result": {}}})
        def model(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            del options
            self.assertEqual(command[:2], ["bash", "-c"])
            self.assertIn("agents_resolve", command[2])
            output = "gpt-fixture\nhigh\n" if "mesh-turn" in command else "codex gpt-fixture high default\n"
            return subprocess.CompletedProcess(command, 0, output, "")

        with (mock.patch.object(subprocess, "run", side_effect=model),
              mock.patch.object(codex_mesh, "workers_home", return_value=self.root / "home/.codex2"),
              mock.patch.object(codex_mesh, "ensure_server", return_value=(12345, False)) as server,
              mock.patch.object(codex_mesh, "Client", return_value=client),
              mock.patch.object(codex_mesh, "_bind_serving_server"),
              mock.patch.object(codex_mesh, "_serving_request", side_effect=request)):
            opened = codex_mesh.open_unit_worker(str(self.run_dir), "impl1", "impl")
            self.assertEqual(opened["thread_id"], native)
            self.assertEqual(server.call_args.kwargs["cwd"], str(self.new_folder))
            self.assertEqual(requests[0][1]["cwd"], str(self.new_folder))
            state = cast(Callable[[Path], dict[str, object]], getattr(progress_history, "_read_state"))(self.run_dir)
            arguments = argparse.Namespace(turn_cwd=state["working_dir"], session_dir=str(self.run_dir), name="impl1",
                                           model="gpt-fixture", effort="high", service_tier="default")
            start = cast(Callable[..., str], getattr(codex_mesh, "_start_turn"))
            with mock.patch.object(codex_mesh, "_turn_service_tier", return_value="default"):
                self.assertEqual(start(client, arguments, native, codex_mesh.WORKER_SANDBOX, "Continue the task."), "first-turn")
        self.assertEqual(requests[-1][0], "turn/start")
        self.assertEqual(requests[-1][1]["cwd"], str(self.new_folder))
        worker = staff.snapshot().by_current_session(native)
        self.assertIsInstance(worker, staff.StaffRow)
        assert isinstance(worker, staff.StaffRow)
        self.assertEqual(worker.parent, self.owner.id)


if __name__ == "__main__":
    _ = unittest.main()
