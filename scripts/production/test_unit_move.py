"""Move a private unit worktree with native session evidence and row lifecycle calls."""
from __future__ import annotations

from scripts.shutdown import remote

import argparse
import io
import json
import os
import subprocess
import time
import unittest
from collections.abc import Callable, Iterable
from contextlib import ExitStack, redirect_stderr
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast
from unittest import mock

from scripts.accounts import accounts
from scripts.agents import codex_mesh
from scripts.direct import progress_history
from scripts.lib import models_registry
from scripts.lib.test_models_registry import registry_text
from scripts.message import sessions
from scripts.production import session_staff, staff, unit_move
from scripts.production.add_unit_test_support import AddUnitTestCase
from scripts.production.cli_fixture import invoke_cli
from scripts.production.rename_unit_test_support import FolderRenameFixture, FolderSessionFixture
from scripts.production.rename_rerun_test_support import file_versions, saved_fixture
from scripts.production.staff_test_support import fixture_member, fixture_runtime_id, fixture_worker
from scripts.shutdown import pane_record, restart, shutdown_record
from scripts.herdr import herdr


def setUpModule() -> None:
    # Session fixtures have no panes unless a test supplies pane evidence.
    _ = unittest.enterModuleContext(mock.patch.object(herdr.Herdr, "panes", return_value=[]))


class AddUnitResumeFolderTests(AddUnitTestCase):
    def test_resuming_a_stopped_unit_tab_retires_the_old_record_before_a_folder_move(self) -> None:
        from scripts.production import add_unit
        from scripts.production import unit_lookup
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
              mock.patch("scripts.shutdown.pane_record.RECORD_DIR", records),
              mock.patch.dict(os.environ, self.env)):
            pane_record.write("agents", original)
            with (mock.patch.object(add_unit, "launched_unit", return_value=add_unit.LabeledSessionFound(labeled)),
                  mock.patch.object(add_unit, "launch_session", side_effect=launch),
                  mock.patch.object(add_unit, "wait_for_remote_control", return_value=labeled),
                  client.empty_shell(shell_pid, old),
                  mock.patch.object(client, "run", side_effect=close),
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
            replace(self.owner, worktree=staff.PresentWorktree(remote.local_machine(), self.destination)))
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

    def test_running_unit_stops_moves_and_resumes_current_session(self) -> None:
        backend = FolderSessionFixture(self.owner, live=True)
        with backend.installed():
            result = self.move_folder()
            self.assertEqual((result.returncode, result.stderr), (0, ""))
            self.assert_moved(backend)
            self.assertEqual(backend.stops, [self.owner.current_session])
            self.assertEqual(len(backend.starts), 1)
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
                           reported_unknown: str = "", codex: herdr.CodexConversations | herdr.CodexLaunchRead = herdr.CodexLaunchRead.CURRENT,
                           kind: Literal["claude", "codex"] = kind) -> herdr.AgentLaunched:
                    _ = (session, opening, timeout, agent, codex)
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
        from scripts.herdr import herdr
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

    def test_move_reads_only_members_and_resumes_their_current_sessions(self) -> None:
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
        from scripts.herdr import herdr
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


    def test_running_unit_stops_pauses_moves_and_resumes_with_its_worker_command(self) -> None:
        self.test_move_reads_only_members_and_resumes_their_current_sessions()

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

    def test_failed_worker_pause_prints_one_line_and_finishes_the_move(self) -> None:
        with mock.patch.object(codex_mesh, "command_stop", return_value=1) as pause:
            self.test_running_unit_stops_moves_and_resumes_current_session()
        pause.assert_not_called()

    def test_crash_after_git_move_finishes_staff_paths_and_starts_the_unit(self) -> None:
        backend = FolderSessionFixture(self.owner, live=False)
        unit_move.prepare_move(self.owner, self.destination, unit_move.KeepBranch.CURRENT)
        _ = self.git("worktree", "move", str(self.source), str(self.destination))
        self.assertEqual(staff.by_id(self.owner.id), self.owner)
        with backend.installed(), mock.patch.object(unit_move, "git", wraps=unit_move.git) as command:
            result = self.move_folder()
            self.assertEqual((result.returncode, result.stderr), (0, ""))
            self.assertFalse(any(call.args[1:3] == ("worktree", "move") for call in command.call_args_list))
            self.assert_moved(backend)
            self.assertEqual((backend.stops, backend.starts, backend.live), ([], [], False))
            self.assert_finished_rerun(backend)

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
                    self.assertEqual(staff.by_id(self.owner.id), replace(self.owner,
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
              mock.patch.object(herdr, "launch_prepared_agent", side_effect=herdr.HerdrFailure("target retained")) as guard,
              redirect_stderr(io.StringIO()) as output):
            with self.assertRaisesRegex(herdr.HerdrFailure, "target retained"):
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

    def test_each_run_mesh_is_paused_when_the_unit_has_two_histories(self) -> None:
        paths = tuple(self.root / "runs" / name / "mesh_roster.json" for name in ("first", "second"))
        for path in paths:
            path.parent.mkdir(parents=True)
            _ = path.write_text('{"queued_message": "Keep this task"}\n')
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
        with mock.patch.object(codex_mesh, "command_stop") as pause:
            self.test_move_reads_only_members_and_resumes_their_current_sessions()
        pause.assert_not_called()
        self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in paths], before)

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
        _ = self.git("worktree", "move", str(self.source), str(self.destination))
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

    def test_folder_move_resumes_the_same_worker_thread_with_the_staff_folder(self) -> None:
        self.test_move_reads_only_members_and_resumes_their_current_sessions()


    def test_next_worker_dispatch_uses_the_moved_run_folder(self) -> None:
        from scripts.agents import codex_mesh

        run = self.root / "runs/move-run"
        run.mkdir(parents=True)
        _ = (run / "progress_history_state.json").write_text(json.dumps({
            "owner_staff_id": str(self.owner.id), "status": "active", "run_id": "move-run",
            "plan_doc": "kept.txt", "branch": "cargo-liner-source"}))
        backend = FolderSessionFixture(self.owner, live=False)
        with backend.installed():
            result = self.move_folder()
        self.assertEqual(result.returncode, 0, result.stderr)
        native = fixture_runtime_id("worker-after-folder-move")
        state = cast(Callable[[Path], dict[str, object]], getattr(progress_history, "_read_state"))(run)
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

        inherited = models_registry.LaunchModelConfiguration(self.root / "models.conf", registry_text(), ())
        configuration = models_registry.launch_configuration(inherited.environment(os.environ))
        with (mock.patch.dict(os.environ, configuration.environment(os.environ)),
              mock.patch.object(subprocess, "run", side_effect=model),
              mock.patch.object(codex_mesh, "workers_home", return_value=self.root / "home/.codex2"),
              mock.patch.object(codex_mesh, "ensure_server", return_value=(12345, False)) as server,
              mock.patch.object(codex_mesh, "Client", return_value=client),
              mock.patch.object(codex_mesh, "_bind_serving_server"),
              mock.patch.object(codex_mesh, "_serving_request", side_effect=request)):
            opened = codex_mesh.open_unit_worker(str(run), "impl1", "impl", configuration=configuration)
            self.assertEqual(session_staff.worker_session(opened, staff.local_snapshot()), native)
            self.assertEqual(server.call_args.kwargs["cwd"], str(self.destination))
            self.assertEqual(requests[0][1]["cwd"], str(self.destination))
            arguments = argparse.Namespace(turn_cwd=state["working_dir"], session_dir=str(run), name="impl1",
                                           model="gpt-fixture", effort="high", service_tier="default")
            start = cast(Callable[..., str], getattr(codex_mesh, "_start_turn"))
            with mock.patch.object(codex_mesh, "_turn_service_tier", return_value="default"):
                self.assertEqual(start(client, arguments, native, codex_mesh.WORKER_SANDBOX, "Continue the task."), "first-turn")
        self.assertEqual(requests[-1][0], "turn/start")
        self.assertEqual(requests[-1][1]["cwd"], str(self.destination))
        worker = staff.snapshot().by_current_session(native)
        self.assertIsInstance(worker, staff.StaffRow)
        assert isinstance(worker, staff.StaffRow)
        self.assertEqual(worker.parent, staff.ReportsTo(self.owner.id))


class FolderBranchRecoveryTests(FolderRenameFixture):
    def assert_recovery(self, step: str) -> None:
        from contextlib import ExitStack
        from scripts.production import unit_move as moving
        from scripts.shutdown import shutdown
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

        with backend.installed(), mock.patch.object(shutdown, "rename_repair_displays", return_value=False):
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
                worktree=staff.PresentWorktree(remote.local_machine(), self.destination)))
            self.assertTrue(backend.starts)
            self.assertTrue(backend.live)
            self.assertTrue(all(row.current_session == self.owner.current_session for row in backend.starts))
            self.assertEqual(backend.starts[-1].worktree, staff.PresentWorktree(remote.local_machine(), self.destination))
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
        from scripts.shutdown import shutdown

        for kind in ("claude", "codex"):
            with self.subTest(kind=kind), saved_fixture(self.root) as restore:
                self.transcript(kind)
                self.assertEqual(list(self.records.iterdir()), [])
                backend = FolderSessionFixture(self.owner, live=True)
                with backend.installed(), mock.patch.object(shutdown, "rename_repair_displays", return_value=False):
                    result = self.command()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(backend.resumes, [(kind,
                    ["--resume", self.owner.current_session, "--name", backend.starts[0].name]
                    if kind == "claude" else ["resume", self.owner.current_session])])
                self.assertEqual(backend.stops, [self.owner.current_session])
                self.assertEqual(backend.starts[0].worktree, staff.PresentWorktree(remote.local_machine(), self.destination))
                self.assertEqual(backend.starts[0].id, self.owner.id)
                self.assertEqual(list(self.records.iterdir()), [])
                restore()

    def test_dirty_work_does_not_refuse_a_nondestructive_move(self) -> None:
        from scripts.shutdown import shutdown

        _ = (self.source / "kept.txt").write_text("Keep the uncommitted edit.\n")
        _ = (self.source / "draft.md").write_text("Keep the untracked draft.\n")
        tip = self.git("rev-parse", "HEAD", cwd=self.source)
        backend = FolderSessionFixture(self.owner, live=True)
        with backend.installed(), mock.patch.object(shutdown, "rename_repair_displays", return_value=False):
            result = self.command()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.destination / "kept.txt").read_text(), "Keep the uncommitted edit.\n")
        self.assertEqual((self.destination / "draft.md").read_text(), "Keep the untracked draft.\n")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.destination), tip)
        self.assertEqual(len(backend.starts), 1)

    def test_existing_target_refuses_without_overwriting_any_file(self) -> None:
        from scripts.shutdown import shutdown

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
        from scripts.shutdown import shutdown

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
        from scripts.shutdown import shutdown

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
        from scripts.production import unit_move
        from scripts.shutdown import shutdown

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
                    _ = staff.set_worktree(self.owner.id, staff.PresentWorktree(remote.local_machine(), self.destination),
                                           expected=self.owner.worktree)
                backend = FolderSessionFixture(self.owner, live=False)
                with backend.installed(), mock.patch.object(shutdown, "rename_repair_displays", return_value=False):
                    result = self.command()
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(self.source.exists())
                    self.assertEqual(self.git("branch", "--show-current", cwd=self.destination), "cargo-liner-destination")
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
                    rerun = self.command()
                    self.assertEqual(rerun.returncode, 0, rerun.stderr)
                    self.assertEqual(rerun.stdout, "unchanged\n")
                    self.assertEqual(rerun.stderr, "")
                    self.assertNotIn("agents restart", rerun.stdout + rerun.stderr)
                    self.assertEqual(file_versions(self.root), before)
                    self.assertEqual((backend.starts, backend.stops), ([], []))

    def test_f006_failed_start_reports_the_restart_command_immediately(self) -> None:
        from scripts.lint.memory_test_support import write_config_fixture
        from scripts.shutdown import shutdown

        _ = write_config_fixture(self.configuration, "RENAME_LIVE_WAIT_SECONDS=0.01\nRENAME_POLL_SECONDS=0.001\n")
        backend = FolderSessionFixture(self.owner, live=True)
        with (backend.installed(), mock.patch.object(shutdown, "rename_repair_displays", return_value=False),
              mock.patch.object(restart, "start_row", side_effect=OSError("fixture launch failure")) as start):
            result = self.command()
        self.assertEqual(result.returncode, 0, result.stderr)
        start.assert_called_once()
        command = f"agents restart --only {self.owner.id}"
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
                        worktree=staff.PresentWorktree(remote.local_machine(), self.destination)))
                    self.assertEqual(self.git("branch", "--show-current", cwd=self.destination), f"cargo-liner-{name}")
                    self.assertEqual((displays.pane, displays.tab), (name, name))
                    self.assertEqual((self.destination / "kept.txt").read_text(), "Keep this work.\n")
                    self.assertIsInstance(unit_move.pending_move(self.owner.id), unit_move.NoPendingMove)
                    self.assertEqual((backend.starts, backend.stops), ([], []))
            self.assertNotEqual(pending_types[0], pending_types[1])

    def test_git_timeout_reports_a_rename_error_and_a_move_anomaly(self) -> None:
        from scripts.shutdown import shutdown

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
