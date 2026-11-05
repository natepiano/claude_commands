"""Move a private unit worktree with native session evidence and row lifecycle calls."""
from __future__ import annotations

import json
import os
import subprocess
import time
import unittest
from collections.abc import Callable, Iterable
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast
from unittest import mock

from scripts.production import session_staff, staff, unit_move
from scripts.production.cli_fixture import invoke_cli
from scripts.production.rename_unit_test_support import FolderRenameFixture, FolderSessionFixture
from scripts.production.rename_rerun_test_support import file_versions, saved_fixture
from scripts.production.staff_test_support import fixture_member, fixture_worker
from scripts.shutdown import restart, shutdown_record


class UnitFolderMoveTests(FolderRenameFixture):
    def move_folder(self) -> subprocess.CompletedProcess[str]:
        arguments = ["move-folder", "--unit", str(self.owner.id), "--to", str(self.destination)]
        return invoke_cli(unit_move, lambda: unit_move.main(arguments), arguments, dict(os.environ), self.checkout)

    def assert_moved(self, backend: FolderSessionFixture) -> None:
        self.assertFalse(self.source.exists())
        self.assertTrue((self.destination / ".git").is_file())
        self.assertEqual(self.git("branch", "--show-current", cwd=self.destination), "cargo-liner-source")
        self.assertEqual(self.git("rev-parse", "--show-toplevel", cwd=self.destination), str(self.destination))
        self.assertEqual(staff.by_id(self.owner.id),
            replace(self.owner, worktree=staff.PresentWorktree("natedev", self.destination)))
        self.assertEqual((self.destination / "kept.txt").read_text(), "Keep this work.\n")
        for row in backend.starts:
            self.assertEqual(row.current_session, self.owner.current_session)
            self.assertEqual(row.worktree, staff.PresentWorktree("natedev", self.destination))

    def assert_finished_rerun(self, backend: FolderSessionFixture) -> None:
        before = file_versions(self.root)
        starts, stops = backend.starts.copy(), backend.stops.copy()
        result = self.move_folder()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "unchanged\n", ""))
        self.assertEqual(file_versions(self.root), before)
        self.assertEqual((backend.starts, backend.stops), (starts, stops))

    def test_running_unit_stops_moves_and_resumes_current_session(self) -> None:
        backend = FolderSessionFixture(self.owner, live=True)
        with backend.installed():
            result = self.move_folder()
            self.assertEqual((result.returncode, result.stderr), (0, ""))
            self.assert_moved(backend)
            self.assertEqual(backend.stops, [self.owner.current_session])
            self.assertEqual(len(backend.starts), 1)
            self.assert_finished_rerun(backend)

    def test_history_mesh_and_launch_files_keep_their_bytes_and_times(self) -> None:
        files = (self.records / "agents.json", self.root / "runs/mesh_roster.json",
                 self.root / "runs/progress_history_state.json", self.root / "history.jsonl")
        text = json.dumps({"session_id": self.owner.current_session, "cwd": str(self.source),
                           "branch": "cargo-liner-source", "queued_message": "Keep this task"}) + "\n"
        for path in files:
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_text(text)
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in files]
        backend = FolderSessionFixture(self.owner, live=True)
        with backend.installed():
            result = self.move_folder()
            self.assertEqual((result.returncode, result.stderr), (0, ""))
            self.assert_moved(backend)
            self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in files], before)
            self.assert_finished_rerun(backend)

    def test_stopped_unit_with_a_closed_pane_stays_stopped(self) -> None:
        backend = FolderSessionFixture(self.owner, live=False)
        with backend.installed():
            result = self.move_folder()
            self.assertEqual((result.returncode, result.stderr), (0, ""))
            self.assert_moved(backend)
            self.assertEqual((backend.starts, backend.stops), ([], []))
            self.assertFalse(backend.live)
            self.assert_finished_rerun(backend)

    def test_f002_stopped_unit_at_destination_rerun_changes_nothing(self) -> None:
        _ = self.git("worktree", "move", str(self.source), str(self.destination))
        moved = staff.set_worktree(self.owner.id, staff.PresentWorktree("natedev", self.destination),
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
        import herdr
        from scripts.message import sessions
        from scripts.accounts import accounts
        from scripts.shutdown import stop_resume

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
                           reported_unknown: str = "", kind: Literal["claude", "codex"] = kind) -> herdr.AgentLaunched:
                    _ = (session, opening, timeout, agent)
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
                with (backend.installed(), mock.patch.object(restart, "start_row", side_effect=real_start),
                      mock.patch.object(restart, "stop_row", side_effect=stop),
                      mock.patch.object(accounts, "role_account", return_value=accounts.NamedAccount("claude1", self.home / ".claude")),
                      mock.patch.object(accounts, "account_dir", return_value=self.home / ".claude"),
                      mock.patch.object(stop_resume, "heal_account_trust"),
                      mock.patch.object(sessions, "session_liveness", side_effect=liveness),
                      mock.patch.object(herdr, "prepare_session_environment", side_effect=prepare),
                      mock.patch.object(herdr, "launch_prepared_agent", side_effect=launch),
                      mock.patch.object(herdr, "running_codex_threads", return_value=set()),
                      mock.patch.object(herdr.Herdr, "saved_row_tab", return_value=herdr.NewTab("w1", self.owner.name)),
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
        import herdr
        from scripts.shutdown import stop_resume

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
        _ = self.git("worktree", "remove", str(self.source))
        unrelated = self.root / "unrelated-repository"
        unrelated.mkdir()
        _ = self.git("init", "-b", "unrelated", cwd=unrelated)
        _ = self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     "commit", "--allow-empty", "-m", "unrelated", cwd=unrelated)
        _ = self.git("worktree", "add", "-b", "unrelated-unit", str(self.destination), cwd=unrelated)
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

    def test_move_reads_only_members_and_resumes_their_current_sessions(self) -> None:
        import herdr
        import sessions

        worker = fixture_worker("impl1", self.owner.id)
        other = fixture_member("unit", name="other-unit", parent=self.owner.parent,
            worktree=staff.PresentWorktree("natedev", self.root / "other-worktree"))
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
            self.assertEqual(shutdown_record.row_folder(row, record, resolver=resolver), ("natedev", self.destination))
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
        self.assertEqual([(action, row.id) for action, row in events],
            [("stop", self.owner.id), ("stop", worker.id), ("start", self.owner.id), ("start", worker.id)])
        self.assertEqual(staff.by_id(worker.id), worker)
        self.assertEqual(running, {row.id for row in rows})
        registry.assert_not_called()
        codex.assert_not_called()
        self.assertEqual({call.args[0] for call in read.call_args_list},
                         {self.owner.current_session, worker.current_session})
        self.assertFalse(self.source.exists())
        self.assertTrue(self.destination.is_dir())

    def test_unsafe_stop_moves_no_folder_and_sends_no_signal(self) -> None:
        backend = FolderSessionFixture(self.owner, live=True)
        with (backend.installed(), mock.patch.object(restart, "stop_row", return_value=False),
              mock.patch.object(restart, "start_row", return_value=""),
              mock.patch.object(os, "kill") as kill):
            result = self.move_folder()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stop is incomplete", result.stderr)
        self.assertTrue(self.source.is_dir())
        self.assertFalse(self.destination.exists())
        self.assertEqual(staff.by_id(self.owner.id), self.owner)
        kill.assert_not_called()

    def test_unsafe_resume_sends_no_keystroke_or_second_session(self) -> None:
        import herdr
        from scripts.message import sessions as native_sessions
        from scripts.accounts import accounts
        from scripts.shutdown import stop_resume

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
              mock.patch.object(client, "result", return_value={"process_info": {"shell_pid": 998040, "foreground_process_group_id": 998041}}),
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
                            with self.assertRaisesRegex(herdr.HerdrFailure, "other live work"):
                                _ = restart.start_row(self.owner, staff.snapshot(), client)
                        else:
                            _ = restart.start_row(self.owner, staff.snapshot(), client)
                    self.assertEqual(launch.call_count, int(isinstance(live, native_sessions.SessionNotLive)))
            target.assert_called_once()
            typed.assert_not_called()
            second.assert_not_called()
            agent_start.assert_not_called()


class FolderBranchRecoveryTests(FolderRenameFixture):
    def assert_recovery(self, step: str) -> None:
        from contextlib import ExitStack
        from scripts.production import rename_unit, unit_move as moving
        from scripts.shutdown import restart

        class InterruptedMove(BaseException):
            pass

        backend = FolderSessionFixture(self.owner, live=True)
        real_git = moving.git
        real_publish = staff.set_worktree

        def git(checkout: Path, *arguments: str) -> str:
            if arguments[:2] == ("branch", "-m"):
                self.assertFalse(backend.live)
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

        def stop(row: staff.StaffRow, client: object) -> bool:
            result = backend.stop(row, client)
            if step == "stop":
                raise InterruptedMove()
            return result

        def start(row: staff.StaffRow, record: staff.StaffSnapshot, client: object,
                  **options: object) -> str:
            result = backend.start(row, record, client, **options)
            if step == "start":
                raise InterruptedMove()
            return result

        with backend.installed(), mock.patch.object(rename_unit, "repair_displays", return_value=False):
            with ExitStack() as interruptions:
                _ = interruptions.enter_context(mock.patch.object(moving, "git", side_effect=git))
                _ = interruptions.enter_context(mock.patch.object(staff, "set_worktree", side_effect=publish))
                _ = interruptions.enter_context(mock.patch.object(restart, "stop_row", side_effect=stop))
                _ = interruptions.enter_context(mock.patch.object(restart, "start_row", side_effect=start))
                with self.assertRaises(InterruptedMove):
                    _ = self.command()
            result = self.command()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(self.source.exists())
            self.assertEqual(self.git("branch", "--show-current", cwd=self.destination), "cargo-liner-destination")
            self.assertEqual(staff.by_id(self.owner.id), replace(self.owner, name="destination",
                worktree=staff.PresentWorktree("natedev", self.destination)))
            self.assertTrue(backend.starts)
            self.assertTrue(backend.live)
            self.assertTrue(all(row.current_session == self.owner.current_session for row in backend.starts))
            self.assertEqual(backend.starts[-1].worktree, staff.PresentWorktree("natedev", self.destination))
            before = file_versions(self.root)
            starts, stops = backend.starts.copy(), backend.stops.copy()
            rerun = self.command()
            self.assertEqual((rerun.returncode, rerun.stdout, rerun.stderr), (0, "unchanged\n", ""))
            self.assertEqual(file_versions(self.root), before)
            self.assertEqual((backend.starts, backend.stops), (starts, stops))

    def test_rerun_finishes_after_stop(self) -> None:
        self.assert_recovery("stop")

    def test_rerun_finishes_after_move(self) -> None:
        self.assert_recovery("move")

    def test_rerun_finishes_after_publish(self) -> None:
        self.assert_recovery("publish")

    def test_rerun_finishes_after_branch(self) -> None:
        self.assert_recovery("branch")

    def test_rerun_finishes_after_start(self) -> None:
        self.assert_recovery("start")

    def test_both_native_families_resume_current_session_without_a_launch_record(self) -> None:
        from scripts.production import rename_unit

        for kind in ("claude", "codex"):
            with self.subTest(kind=kind), saved_fixture(self.root) as restore:
                self.transcript(kind)
                self.assertEqual(list(self.records.iterdir()), [])
                backend = FolderSessionFixture(self.owner, live=True)
                with backend.installed(), mock.patch.object(rename_unit, "repair_displays", return_value=False):
                    result = self.command()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(backend.resumes, [(kind,
                    ["--resume", self.owner.current_session, "--name", backend.starts[0].name]
                    if kind == "claude" else ["resume", self.owner.current_session])])
                self.assertEqual(backend.stops, [self.owner.current_session])
                self.assertEqual(backend.starts[0].worktree, staff.PresentWorktree("natedev", self.destination))
                self.assertEqual(backend.starts[0].id, self.owner.id)
                self.assertEqual(list(self.records.iterdir()), [])
                restore()

    def test_dirty_work_does_not_refuse_a_nondestructive_move(self) -> None:
        from scripts.production import rename_unit

        _ = (self.source / "kept.txt").write_text("Keep the uncommitted edit.\n")
        _ = (self.source / "draft.md").write_text("Keep the untracked draft.\n")
        tip = self.git("rev-parse", "HEAD", cwd=self.source)
        backend = FolderSessionFixture(self.owner, live=True)
        with backend.installed(), mock.patch.object(rename_unit, "repair_displays", return_value=False):
            result = self.command()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.destination / "kept.txt").read_text(), "Keep the uncommitted edit.\n")
        self.assertEqual((self.destination / "draft.md").read_text(), "Keep the untracked draft.\n")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.destination), tip)
        self.assertEqual(len(backend.starts), 1)

    def test_existing_target_refuses_without_overwriting_any_file(self) -> None:
        from scripts.production import rename_unit

        self.destination.mkdir()
        marker = self.destination / "owned-by-another-unit.txt"
        _ = marker.write_text("Keep this destination.\n")
        before = file_versions(self.destination)
        backend = FolderSessionFixture(self.owner, live=True)
        with backend.installed(), mock.patch.object(rename_unit, "repair_displays", return_value=False):
            result = self.command()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(file_versions(self.destination), before)
        self.assertTrue(self.source.is_dir())
        row = staff.by_id(self.owner.id)
        assert row is not None
        self.assertEqual(row.worktree, self.owner.worktree)

    def test_same_row_lock_refuses_without_stopping_or_moving(self) -> None:
        import fcntl
        from scripts.production import rename_unit

        backend = FolderSessionFixture(self.owner, live=True)
        lock = self.staff_file.parent / "rows" / f"{self.owner.id}.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        with (backend.installed(), mock.patch.object(rename_unit, "repair_displays", return_value=False),
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
        from scripts.production import rename_unit

        backend = FolderSessionFixture(self.owner, live=False)
        with backend.installed(), mock.patch.object(rename_unit, "repair_displays", return_value=False):
            result = self.command()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(backend.starts, [])
            self.assertFalse(backend.live)
            self.assertEqual(staff.by_id(self.owner.id), replace(self.owner, name="destination",
                worktree=staff.PresentWorktree("natedev", self.destination)))
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
                                self.checkout, self.checkout / "production.log", ZoneInfo("UTC"))
        request = transfer_unit.UnitTransfer(self.owner, parent, production, "source-unit")
        lock = self.staff_file.parent / "rows" / f"{self.owner.id}.lock"
        def move_projection(*_arguments: object, **_options: object) -> None:
            with lock.open("a") as handle:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (mock.patch.object(transfer_unit, "build", return_value=request),
              mock.patch.object(staff, "row_operation", wraps=staff.row_operation) as row_lock,
              mock.patch.object(staff_move, "move_unit", side_effect=move_projection) as projection,
              redirect_stdout(io.StringIO())):
            self.assertEqual(transfer_unit.apply("folder-production", "source-unit", production.doc), request)
        row_lock.assert_called_once_with(str(self.owner.id))
        projection.assert_called_once()
        self.assertEqual(staff.by_id(self.owner.id), replace(self.owner, parent=staff.ReportsTo(parent.id)))

    def test_f006_hard_kill_states_finish_without_cleanup_or_automatic_starts(self) -> None:
        from scripts.production import rename_unit, unit_move

        for step in ("stop", "move", "publish"):
            with self.subTest(step=step), saved_fixture(self.root) as restore, ExitStack() as recovery:
                _ = recovery.callback(restore)
                worker = fixture_worker("stopped-worker", self.owner.id)
                unit_move.prepare_move(self.owner, self.destination, "cargo-liner-destination", "destination")
                _ = staff.rename(self.owner.id, "destination")
                if step in ("move", "publish"):
                    _ = self.git("worktree", "move", str(self.source), str(self.destination))
                if step == "publish":
                    _ = self.git("branch", "-m", "cargo-liner-destination", cwd=self.destination)
                    _ = staff.set_worktree(self.owner.id, staff.PresentWorktree("natedev", self.destination),
                                           expected=self.owner.worktree)
                backend = FolderSessionFixture(self.owner, live=False)
                with backend.installed(), mock.patch.object(rename_unit, "repair_displays", return_value=False):
                    result = self.command()
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(self.source.exists())
                    self.assertEqual(self.git("branch", "--show-current", cwd=self.destination), "cargo-liner-destination")
                    self.assertEqual(staff.by_id(self.owner.id), replace(self.owner, name="destination",
                        worktree=staff.PresentWorktree("natedev", self.destination)))
                    self.assertEqual(staff.by_id(worker.id), worker)
                    self.assertEqual((backend.starts, backend.stops), ([], []))
                    self.assertFalse(backend.live)
                    for row in (self.owner, worker):
                        command = f"shutdown.py restart --only {row.id}"
                        lines = [line for line in (result.stdout + result.stderr).splitlines() if command in line]
                        self.assertEqual(len(lines), 1, result.stdout + result.stderr)
                        self.assertIn("info", lines[0].lower())
                    before = file_versions(self.root)
                    rerun = self.command()
                    self.assertEqual(rerun.returncode, 0, rerun.stderr)
                    self.assertEqual(rerun.stdout, "unchanged\n")
                    self.assertEqual(rerun.stderr, "")
                    self.assertNotIn("shutdown.py restart", rerun.stdout + rerun.stderr)
                    self.assertEqual(file_versions(self.root), before)
                    self.assertEqual((backend.starts, backend.stops), ([], []))

    def test_f006_failed_start_reports_the_restart_command_immediately(self) -> None:
        from scripts.lint.memory_test_support import write_config_fixture
        from scripts.production import rename_unit

        _ = write_config_fixture(self.configuration, "RENAME_LIVE_WAIT_SECONDS=0.01\nRENAME_POLL_SECONDS=0.001\n")
        backend = FolderSessionFixture(self.owner, live=True)
        with (backend.installed(), mock.patch.object(rename_unit, "repair_displays", return_value=False),
              mock.patch.object(restart, "start_row", side_effect=OSError("fixture launch failure")) as start):
            result = self.command()
        self.assertEqual(result.returncode, 0, result.stderr)
        start.assert_called_once()
        command = f"shutdown.py restart --only {self.owner.id}"
        lines = [line for line in (result.stdout + result.stderr).splitlines() if command in line]
        self.assertEqual(len(lines), 1, result.stdout + result.stderr)
        self.assertIn("anomaly", lines[0].lower())
        self.assertFalse(backend.live)
        self.assertEqual(backend.starts, [])
        self.assertEqual(backend.stops, [self.owner.current_session])

    def test_f007_no_lifecycle_call_starts_after_the_shared_deadline(self) -> None:
        from scripts.agents import worker_lifecycle

        worker = fixture_worker("live-worker", self.owner.id)
        running = {self.owner.id, worker.id}
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
        _ = self.git("worktree", "remove", str(self.source))
        _ = self.git("worktree", "add", "-b", "another-unit", str(self.destination))
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

        unit_move.prepare_move(self.owner, self.destination, "cargo-liner-destination", "destination")
        renamed = staff.rename(self.owner.id, "destination")
        _ = self.git("worktree", "remove", str(self.source))
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

        for journal_format in ("current", "legacy"):
            pending_types: list[type[object]] = []
            for operation in ("folder", "rename"):
                with self.subTest(journal_format=journal_format, operation=operation), saved_fixture(self.root) as restore, ExitStack() as recovery:
                    _ = recovery.callback(restore)
                    branch = unit_move.KeepBranch.CURRENT if operation == "folder" else "cargo-liner-destination"
                    if operation == "folder":
                        unit_move.prepare_move(self.owner, self.destination, branch)
                    else:
                        unit_move.prepare_move(self.owner, self.destination, branch, "destination")
                    if journal_format == "legacy":
                        journal = staff.folder_move_path(self.owner.id)
                        saved = cast(dict[str, object], json.loads(journal.read_text()))
                        del saved["kind"]
                        _ = journal.write_text(json.dumps(saved) + "\n")
                    _ = self.git("worktree", "move", str(self.source), str(self.destination))
                    pending_types.append(type(unit_move.pending_move(self.owner.id)))
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
                        worktree=staff.PresentWorktree("natedev", self.destination)))
                    self.assertEqual(self.git("branch", "--show-current", cwd=self.destination), f"cargo-liner-{name}")
                    self.assertEqual((displays.pane, displays.tab), (name, name))
                    self.assertEqual((self.destination / "kept.txt").read_text(), "Keep this work.\n")
                    self.assertIsInstance(unit_move.pending_move(self.owner.id), unit_move.NoPendingMove)
                    self.assertEqual((backend.starts, backend.stops), ([], []))
            self.assertNotEqual(pending_types[0], pending_types[1])

    def test_git_timeout_reports_a_rename_error_and_a_move_anomaly(self) -> None:
        from scripts.production import rename_unit

        real_git = unit_move.git
        def git(folder: Path, *arguments: str) -> str:
            if arguments[:2] == ("worktree", "move"):
                raise subprocess.TimeoutExpired(["git", *arguments], 0.01)
            return real_git(folder, *arguments)
        for entry in ("unit_move", "rename_unit"):
            with self.subTest(entry=entry), saved_fixture(self.root) as restore, ExitStack() as recovery:
                _ = recovery.callback(restore)
                backend = FolderSessionFixture(self.owner, live=False)
                with (backend.installed(), mock.patch.object(unit_move, "git", side_effect=git),
                      mock.patch.object(rename_unit, "repair_displays", return_value=False)):
                    if entry == "rename_unit":
                        result = self.command()
                    else:
                        arguments = ["move-folder", "--unit", str(self.owner.id), "--to", str(self.destination)]
                        result = invoke_cli(unit_move, lambda: unit_move.main(arguments), arguments,
                                            dict(os.environ), self.checkout)
                if entry == "rename_unit":
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn("rename_unit: error:", result.stderr)
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
