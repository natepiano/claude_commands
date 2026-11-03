"""Move a unit folder while retaining its identity, records, and terminal."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import unittest
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast, final
from unittest import mock

from scripts.agents import worker_lifecycle
from scripts.accounts import accounts
from scripts.lib.atomic_file import write_bytes_atomic, write_text_atomic
from scripts.lib import relaunch, conf
from scripts.message import sessions
from scripts.production import staff
from scripts.production.cli_fixture import invoke_cli
from scripts.production.rename_test_support import FolderMoveBackend, FolderMoveFixture
from scripts.production.add_unit_test_support import AddUnitTestCase
from scripts.production.staff_test_support import fixture_runtime_id
import unit_move
from scripts.shutdown import pane_record
import herdr


def folder_move_config(stop: float, resume: float, poll: float) -> str:
    return (f"# range: 5e-324..\nFOLDER_MOVE_STOP_SECONDS={stop}\n"
            + f"# range: 5e-324..\nFOLDER_MOVE_RESUME_SECONDS={resume}\n"
            + f"# range: 5e-324..\nFOLDER_MOVE_POLL_SECONDS={poll}\n")


UNREADABLE_ARGUMENTS: tuple[dict[str, object], ...] = (
    {}, {"argv": None}, {"argv": "starting"}, {"argv": ["starting", 17]},
)


@final
class WorkerMoveRuntime:
    """Private launchers expose worker progress and process death separately."""

    def __init__(self, case: UnitFolderMoveTests, backend: FolderMoveBackend) -> None:
        from scripts.agents import codex_mesh
        from scripts.shutdown import inventory

        self.case: UnitFolderMoveTests = case
        self.backend: FolderMoveBackend = backend
        self.starts: list[tuple[list[str], Path]] = []
        self.live: dict[int, bool] = {}
        self.launch_status: Literal["starting", "running"] = "running"
        self.vanish_pid: int = 0
        self.vanished: list[int] = []
        self.pauses: list[str] = []
        self.original_popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen
        self.original_liveness: Callable[[int, str], sessions.LiveSessionProcess | sessions.SessionNotLive
                                        | sessions.SessionLivenessUnknown] = sessions.process_liveness
        self.original_arguments: Callable[[int], list[str]] = pane_record.process_arguments
        self.original_start: Callable[[int], str | None] = sessions.process_start
        for context in (
            mock.patch.object(codex_mesh, "command_stop", side_effect=self.pause),
            mock.patch.object(codex_mesh, "open_unit_worker",
                              side_effect=AssertionError("A move must reuse the worker thread")),
            mock.patch.object(inventory, "codex_seats", return_value=[]),
            mock.patch.object(pane_record, "PROC", case.root / "folder-processes"),
            mock.patch("pane_record.PROC", case.root / "folder-processes"),
            mock.patch.object(pane_record, "process_arguments", side_effect=self.arguments),
            mock.patch("pane_record.process_arguments", side_effect=self.arguments),
            mock.patch.object(sessions, "process_liveness", side_effect=self.liveness),
            mock.patch.object(sessions, "process_start", side_effect=self.process_start),
            mock.patch.object(subprocess, "Popen", side_effect=self.launch),
        ):
            _ = case.enterContext(context)

    def publish_process(self, pid: int, arguments: list[str]) -> None:
        process = self.case.root / "folder-processes" / str(pid)
        write_bytes_atomic(process / "cmdline", b"\0".join(os.fsencode(word) for word in arguments) + b"\0")
        write_text_atomic(process / "stat", f"{pid} (fixture) S 1 " + "0 " * 18 + "1\n")
        write_bytes_atomic(process / "environ", b"")
        self.live[pid] = True

    def pause(self, arguments: argparse.Namespace) -> int:
        self.case.assertTrue(cast(bool, arguments.pause))
        self.pauses.append(cast(str, arguments.session_dir))
        rows = self.case.read_rows()
        rows["impl1"]["status"] = "paused"
        self.case.write_json(self.case.run_dir / "mesh_roster.json", rows)
        for pid in self.live:
            self.live[pid] = False
        return 0

    def launch(self, command: list[str], **options: object) -> subprocess.Popen[str]:
        if not any(Path(word).name in ("implement.sh", "review.sh") for word in command):
            return self.original_popen(command, **options)
        self.starts.append((command.copy(), Path(cast(str | Path, options["cwd"]))))
        pid = 998050 + len(self.starts)
        self.publish_process(pid, command)
        rows = self.case.read_rows()
        rows["impl1"].update(status=self.launch_status, launcher_pid=pid, launcher_start="fixture-start")
        self.case.write_json(self.case.run_dir / "mesh_roster.json", rows)
        return cast(subprocess.Popen[str], mock.Mock(pid=pid))

    def liveness(self, pid: int, started: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive | sessions.SessionLivenessUnknown:
        if pid in self.live:
            return sessions.LiveSessionProcess(pid) if self.live[pid] else sessions.SessionNotLive()
        return self.original_liveness(pid, started)

    def process_start(self, pid: int) -> str | None:
        if pid in self.live:
            return "fixture-start" if self.live[pid] else None
        return self.original_start(pid)

    def arguments(self, pid: int) -> list[str]:
        if pid == self.vanish_pid:
            self.vanish_pid = 0
            self.live[pid] = False
            self.vanished.append(pid)
            raise FileNotFoundError(f"/private-proc/{pid}/cmdline")
        return self.original_arguments(pid)


@final
class AddUnitResumeFolderTests(AddUnitTestCase):
    def test_resuming_a_stopped_unit_tab_retires_the_old_record_before_a_folder_move(self) -> None:
        import add_unit
        import unit_lookup
        from scripts.shutdown.test_pane_restore import FakeClient
        from datetime import datetime, timezone

        old = self.root / "project-alpha"
        self.commit_prepared_row(self.unit_row())
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
                  mock.patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()),
                  mock.patch.object(unit_move, "processes_inside", return_value=[])):
                moved = invoke_cli(unit_move, lambda: unit_move.main(arguments), arguments, self.env, self.checkout)
            self.assertEqual(moved.returncode, 0, moved.stderr)
            publications = [entry for entry in pane_record.read("agents").values()
                            if entry["session_id"] in (owner.id, owner.current_session)]
            self.assertEqual([(entry["pane_id"], entry["cwd"]) for entry in publications],
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
        self.assertEqual((launch["cwd"], launch["session_id"], launch["name"]),
                         (str(self.new_folder), self.owner.id, "kept-name"))
        state = cast(dict[str, object], json.loads((self.run_dir / "progress_history_state.json").read_text()))
        event = cast(dict[str, object], json.loads(self.history.read_text().splitlines()[0]))
        for fields in (state, event):
            self.assertEqual((fields["working_dir"], fields["branch"], fields["plan_doc"], fields["run_id"]),
                             (str(self.new_folder), "unit-branch", str(self.new_folder / "plan.md"), "move-run"))
        self.assertEqual(tuple((pane["pane_id"], pane.get("terminal_id")) for pane in backend.panes()),
                         (("w1:p1", "kept-terminal"), ("w1:p2", "neighbour-terminal")))

    def move_files(self, *extra: Path) -> tuple[Path, ...]:
        return (self.staff_file, self.records / "agents.json", self.history,
                self.run_dir / "progress_history_state.json", *extra)

    def read_rows(self) -> dict[str, dict[str, object]]:
        return cast(dict[str, dict[str, object]], json.loads((self.run_dir / "mesh_roster.json").read_text()))

    def assert_finished_rerun(self, backend: FolderMoveBackend) -> None:
        paths = self.move_files(staff.folder_move_path(self.owner.id))
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
        starts, stops = backend.starts.copy(), backend.stops.copy()
        result = self.move_folder()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "unchanged\n", ""))
        self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in paths], before)
        self.assertEqual((backend.starts, backend.stops), (starts, stops))

    def test_one_folder_move_shares_settings_through_stop_and_resume(self) -> None:
        backend = self.folder_backend(live=True)
        configuration = self.root / "move-config"
        configuration.mkdir()
        path = configuration / "production.conf"
        _ = path.write_text(folder_move_config(3, 3, 0.01))
        with (mock.patch.dict(os.environ, {"CONF_DIR": str(configuration), "PRODUCTION_CONFIG": str(path)}),
              mock.patch.object(conf, "read", wraps=conf.read) as reads):
            result = self.move_folder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call.args[0] for call in reads.call_args_list if call.args[0] == path], [path])
        self.assert_moved(backend)
        self.assertTrue(backend.starts)
        self.assertTrue(backend.stops)

    def test_worker_wait_restart_uses_the_same_settings_as_its_first_deadline(self) -> None:
        path = self.root / "production.conf"
        _ = path.write_text(folder_move_config(3, 4, 0.01))
        with (mock.patch.dict(os.environ, {"CONF_DIR": str(self.root), "PRODUCTION_CONFIG": str(path)}),
              mock.patch.object(conf, "read", wraps=conf.read) as reads,
              mock.patch.object(time, "monotonic", return_value=0.0)):
            wait = unit_move.FolderMoveWait("FOLDER_MOVE_RESUME_SECONDS", settings=conf.read(Path(os.environ["PRODUCTION_CONFIG"])))
            _ = path.write_text(folder_move_config(3, 9, 0.01))
            wait.restart()
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(wait.deadline, 4.0)

    def test_stopped_move_updates_every_path_without_starting_or_renaming(self) -> None:
        backend = self.folder_backend()
        before = pane_record.read("agents")
        plan = (self.old_folder / "plan.md").read_bytes()
        result = self.move_folder()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_moved(backend)
        self.assertEqual(pane_record.read("agents"),
                         {**before, "w1:p1": {**before["w1:p1"], "cwd": str(self.new_folder)}})
        self.assertEqual((self.new_folder / "plan.md").read_bytes(), plan)
        self.assertEqual((backend.starts, backend.stops, backend.live), ([], [], False))
        record = cast(dict[str, object], json.loads(staff.folder_move_path(self.owner.id).read_text()))
        self.assertEqual(set(record), {"old", "new", "steps"})
        found = self.cli("get", str(self.new_folder), "--json")
        self.assertEqual(json.loads(found.stdout)["id"], self.owner.id)
        self.assert_finished_rerun(backend)

    def outside_codex_evidence(self, backend: FolderMoveBackend, *, readable: bool = True) -> None:
        process = self.root / "folder-processes" / str(backend.pid)
        write_bytes_atomic(process / "cmdline", b"codex\0resume\0" + self.owner.current_session.encode() + b"\0")
        rollout = self.root / "outside/sessions/rollout-current.jsonl"
        write_text_atomic(rollout, json.dumps({"type": "session_meta",
            "payload": {"id": self.owner.current_session}}) + "\n" if readable else "unreadable metadata\n")
        (process / "fd/7").symlink_to(rollout)
        self.assertFalse(backend.live)
        self.assertEqual(backend.observed_processes(), [])

    def assert_idle_move_refused(self, backend: FolderMoveBackend) -> None:
        paths = self.move_files(self.run_dir / "working_dir")
        before = [path.read_bytes() for path in paths]
        result = self.move_folder()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Codex", result.stderr)
        self.assertEqual([path.read_bytes() for path in paths], before)
        self.assertTrue((self.old_folder / ".git").is_file())
        self.assertFalse(self.new_folder.exists())
        self.assertFalse(staff.folder_move_path(self.owner.id).exists())
        self.assertEqual((backend.starts, backend.stops), ([], []))

    def test_idle_retained_pane_refuses_a_move_when_its_codex_thread_runs_outside_herdr(self) -> None:
        backend = self.folder_backend("codex")
        self.outside_codex_evidence(backend)
        self.assert_idle_move_refused(backend)

    def test_idle_retained_pane_moves_when_codex_evidence_is_unreadable(self) -> None:
        backend = self.folder_backend("codex")
        self.outside_codex_evidence(backend, readable=False)
        with (mock.patch.object(herdr, "herdr_place_of", return_value=herdr.NotInHerdr()),
              mock.patch.object(sessions, "running_sessions", return_value=[])):
            result = self.move_folder()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"anomaly: pid {backend.pid} (Codex thread unknown); not blocking", result.stderr)
        self.assert_moved(backend)
        self.assertEqual((backend.starts, backend.stops, backend.live), ([], [], False))
        self.assert_finished_rerun(backend)

    def test_idle_retained_pane_refuses_a_pending_resume_when_its_codex_thread_runs_elsewhere(self) -> None:
        backend = self.folder_backend("codex", live=True)
        with mock.patch.object(unit_move, "resume_folder_session", side_effect=OSError("interrupted before resume")):
            interrupted = self.move_folder()
        self.assertEqual(interrupted.returncode, 1, interrupted.stdout + interrupted.stderr)
        self.assertIn("interrupted before resume", interrupted.stderr)
        progress = staff.folder_move_path(self.owner.id)
        move = unit_move.read_folder_move(progress)
        self.assertIs(move.steps["resume"], unit_move.FolderStepState.PENDING)
        self.assertEqual((backend.starts, backend.stops), ([], [self.owner.current_session]))
        self.outside_codex_evidence(backend)
        paths = self.move_files(progress)
        before = [path.read_bytes() for path in paths]
        result = self.move_folder()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Codex", result.stderr)
        self.assertEqual([path.read_bytes() for path in paths], before)
        self.assertFalse(self.old_folder.exists())
        self.assertTrue((self.new_folder / ".git").is_file())
        self.assertEqual((backend.starts, backend.stops), ([], [self.owner.current_session]))

    def test_idle_retained_pane_moves_without_any_live_claude_or_codex_process(self) -> None:
        for kind in ("claude", "codex"):
            with self.subTest(kind=kind):
                case = UnitFolderMoveTests()
                case.setUp()
                try:
                    backend = case.folder_backend(kind)
                    self.assertEqual(herdr.running_codex_threads(thread_host=lambda _argv: False), set())
                    moved = case.move_folder()
                    case.assertEqual(moved.returncode, 0, moved.stderr)
                    case.assert_moved(backend)
                    case.assertEqual((backend.starts, backend.stops, backend.live), ([], [], False))
                    case.assert_finished_rerun(backend)
                finally:
                    case.doCleanups()

    def assert_move_with_unidentifiable_codex(self, kind: Literal["claude", "codex"],
                                             *, target_live: bool = False) -> None:
        for pending_resume in (False, True):
            with self.subTest(kind=kind, pending_resume=pending_resume, target_live=target_live):
                case = UnitFolderMoveTests()
                case.setUp()
                try:
                    read_environment = herdr.process_environment
                    backend = case.folder_backend(kind, live=pending_resume)
                    progress = staff.folder_move_path(case.owner.id)
                    if pending_resume:
                        with mock.patch.object(unit_move, "resume_folder_session",
                                               side_effect=OSError("interrupted before resume")):
                            interrupted = case.move_folder()
                        case.assertEqual(interrupted.returncode, 1, interrupted.stderr)
                        case.assertIn("interrupted before resume", interrupted.stderr)
                        case.assertIs(unit_move.read_folder_move(progress).steps["resume"],
                                      unit_move.FolderStepState.PENDING)
                    pid = backend.pid + 1
                    process = case.root / "folder-processes" / str(pid)
                    (process / "fd").mkdir(parents=True)
                    write_bytes_atomic(process / "cmdline", b"codex\0")
                    write_bytes_atomic(process / "environ", b"")
                    write_text_atomic(process / "stat", f"{pid} (codex) S 1 0\n")

                    def environment(candidate: int) -> dict[str, str]:
                        return (read_environment(candidate) if candidate == pid
                                else backend.environment(candidate))

                    _ = case.enterContext(mock.patch.object(herdr, "process_environment",
                                                           side_effect=environment))
                    _ = case.enterContext(mock.patch.object(herdr, "herdr_place_of",
                                                           return_value=herdr.NotInHerdr()))
                    _ = case.enterContext(mock.patch.object(sessions, "running_sessions", return_value=[]))
                    with case.assertRaisesRegex(ValueError, "Codex launch identity is unavailable"):
                        _ = herdr.native_process(pid, "codex")
                    if target_live and kind == "claude":
                        _ = case.enterContext(mock.patch.object(sessions, "session_liveness",
                            return_value=sessions.LiveSessionProcess(pid + 1)))
                    elif target_live:
                        case.outside_codex_evidence(backend)
                    elif kind == "codex":
                        _ = case.enterContext(mock.patch.object(sessions, "session_liveness",
                            return_value=sessions.SessionLivenessUnknown("Claude registry is unavailable")))
                    paths = case.move_files(case.run_dir / "working_dir")
                    if pending_resume:
                        paths += (progress,)
                    before = [path.read_bytes() for path in paths]
                    starts, stops = backend.starts.copy(), backend.stops.copy()
                    result = case.move_folder()
                    if target_live:
                        case.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                        case.assertIn("live Codex thread" if kind == "codex" else "live session", result.stderr)
                        case.assertEqual([path.read_bytes() for path in paths], before)
                        case.assertEqual((backend.starts, backend.stops), (starts, stops))
                        case.assertEqual(case.old_folder.exists(), not pending_resume)
                        case.assertEqual(case.new_folder.exists(), pending_resume)
                        if not pending_resume:
                            case.assertFalse(progress.exists())
                    else:
                        case.assertEqual(result.returncode, 0, result.stderr)
                        case.assertIn(f"anomaly: pid {pid} (Codex thread unknown); not blocking", result.stderr)
                        if kind == "codex":
                            case.assertIn("anomaly: Claude registry is unavailable; not blocking", result.stderr)
                        case.assert_moved(backend)
                        case.assertEqual(backend.stops, stops)
                        case.assertEqual(len(backend.starts), int(pending_resume))
                        case.assertEqual(backend.live, pending_resume)
                        case.assert_finished_rerun(backend)
                finally:
                    case.doCleanups()

    def test_stopped_claude_target_moves_with_an_unidentifiable_codex_process(self) -> None:
        self.assert_move_with_unidentifiable_codex("claude")

    def test_stopped_codex_target_moves_with_an_unidentifiable_codex_process(self) -> None:
        self.assert_move_with_unidentifiable_codex("codex")

    def test_idle_retained_pane_refuses_when_its_target_session_is_proven_live(self) -> None:
        for kind in ("claude", "codex"):
            self.assert_move_with_unidentifiable_codex(kind, target_live=True)

    def assert_live_move(self, kind: Literal["claude", "codex"], *, legacy: bool = False,
                         startup_delay_polls: int = 0) -> None:
        backend = self.folder_backend(kind, live=True)
        backend.startup_delay_polls = startup_delay_polls
        if legacy:
            entries = pane_record.read("agents")
            del entries["w1:p1"]["kind"]
            pane_record.save("agents", entries)
        neighbour = pane_record.read("agents")["w1:p2"]
        clock = [0.0]

        def sleep(seconds: float) -> None:
            clock[0] += seconds

        with (mock.patch.object(time, "monotonic", side_effect=lambda: clock[0]),
              mock.patch.object(time, "sleep", side_effect=sleep)):
            result = self.move_folder()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_moved(backend)
        self.assertTrue(backend.live)
        self.assertEqual(len(backend.stops), 1)
        (pane, command), = backend.starts
        self.assertEqual(pane, "w1:p1")
        self.assertIn(str(self.new_folder), command)
        resume = command.index("--resume" if kind == "claude" else "resume", command.index("exec") + 1)
        self.assertEqual(command[resume + 1], self.owner.current_session)
        self.assertNotEqual(command[resume + 1], self.owner.id)
        self.assertEqual(command[command.index("exec") + 1], backend.config.name.removeprefix("."))
        if kind == "claude":
            self.assertIn("the session folder moved", command[-1])
            self.assertNotIn("the herdr server restarted", command[-1])
        self.assertEqual(backend.cwd, self.new_folder)
        self.assertEqual(backend.startup_observations, startup_delay_polls + 1)
        self.assertFalse(backend.startup_pending)
        self.assertEqual(backend.agents(), [{"pane_id": "w1:p1", "agent": kind, "agent_status": "running"}])
        self.assertEqual(clock[0] > 0, startup_delay_polls > 0)
        self.assertIs(unit_move.read_folder_move(staff.folder_move_path(self.owner.id)).steps["resume"],
                      unit_move.FolderStepState.DONE)
        self.assertEqual(pane_record.read("agents")["w1:p2"], neighbour)
        self.assert_finished_rerun(backend)

    def test_live_claude_resumes_current_session_in_the_same_terminal(self) -> None:
        self.assert_live_move("claude")

    def test_live_codex_resumes_current_thread_in_the_same_terminal(self) -> None:
        self.assert_live_move("codex")

    def test_live_claude_without_a_kind_field_resumes_from_its_retained_launch(self) -> None:
        self.assert_live_move("claude", legacy=True)

    def test_resume_waits_for_starting_processes_before_proving_the_session(self) -> None:
        for kind in ("claude", "codex"):
            with self.subTest(kind=kind):
                case = UnitFolderMoveTests()
                case.setUp()
                try:
                    case.assert_live_move(kind, startup_delay_polls=3)
                finally:
                    case.doCleanups()

    def test_resume_refuses_a_proven_foreign_conversation_or_account_without_retrying(self) -> None:
        for kind in ("claude", "codex"):
            for changed in ("conversation", "account"):
                with self.subTest(kind=kind, changed=changed):
                    case = UnitFolderMoveTests()
                    case.setUp()
                    try:
                        backend = case.folder_backend(kind, live=True)
                        native_process = herdr.native_process

                        def observe(pid: int, runtime: str) -> herdr.NativeAgentProcess:
                            observed = native_process(pid, runtime)
                            if not backend.starts:
                                return observed
                            if changed == "conversation":
                                return replace(observed, identity=fixture_runtime_id("foreign-resumed-session"))
                            key = "CODEX_HOME" if kind == "codex" else "CLAUDE_CONFIG_DIR"
                            return replace(observed, environment={**observed.environment,
                                key: str(case.root / "foreign-account")})

                        with (mock.patch.object(herdr, "native_process", side_effect=observe),
                              mock.patch.object(time, "sleep") as sleep):
                            result = case.move_folder()
                        case.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                        case.assertIn("foreground runtime identity is unproved" if changed == "conversation"
                                      else "retained pane account changed", result.stderr)
                        sleep.assert_not_called()
                        case.assertEqual(backend.startup_observations, 1)
                        case.assertEqual((len(backend.starts), len(backend.stops)), (1, 1))
                        case.assertEqual(backend.starts[0][0], "w1:p1")
                        case.assertEqual(backend.cwd, case.new_folder)
                        move = unit_move.read_folder_move(staff.folder_move_path(case.owner.id))
                        case.assertIs(move.steps["resume"], unit_move.FolderStepState.PENDING)
                        case.assertIs(pane_record.read("agents")["w1:p1"].get("ended"), True)
                    finally:
                        case.doCleanups()

    def test_resume_waits_for_the_native_identity_after_recognizing_the_process(self) -> None:
        for kind in ("claude", "codex"):
            with self.subTest(kind=kind):
                case = UnitFolderMoveTests()
                case.setUp()
                try:
                    backend = case.folder_backend(kind, live=True)
                    native_process = herdr.native_process
                    observations: list[int] = []
                    clock = [0.0]

                    def observe(pid: int, runtime: str) -> herdr.NativeAgentProcess:
                        if backend.starts:
                            case.assertEqual((pid, runtime), (backend.pid, kind))
                            case.assertTrue(backend.live)
                            observations.append(pid)
                            if len(observations) <= 3:
                                raise ValueError("native identity is not published yet")
                        return native_process(pid, runtime)

                    def sleep(seconds: float) -> None:
                        clock[0] += seconds

                    with (mock.patch.object(herdr, "native_process", side_effect=observe),
                          mock.patch.object(time, "monotonic", side_effect=lambda: clock[0]),
                          mock.patch.object(time, "sleep", side_effect=sleep)):
                        moved = case.move_folder()
                    case.assertEqual(moved.returncode, 0, moved.stderr)
                    case.assert_moved(backend)
                    case.assertEqual(observations, [backend.pid] * 4)
                    case.assertGreater(clock[0], 0)
                    case.assertEqual((len(backend.starts), len(backend.stops)), (1, 1))
                    pane, command = backend.starts[0]
                    case.assertEqual(pane, "w1:p1")
                    resume = command.index("--resume" if kind == "claude" else "resume", command.index("exec") + 1)
                    case.assertEqual(command[resume + 1], case.owner.current_session)
                    case.assertEqual(backend.cwd, case.new_folder)
                    case.assertIs(unit_move.read_folder_move(staff.folder_move_path(case.owner.id)).steps["resume"],
                                  unit_move.FolderStepState.DONE)
                    case.assert_finished_rerun(backend)
                finally:
                    case.doCleanups()

    def assert_unreadable_resume(self, kind: Literal["claude", "codex"], arguments: dict[str, object], *,
                                 verified: bool = False, expires: bool = False) -> None:
        backend = self.folder_backend(kind, live=True)
        unreadable: dict[str, object] = {"pid": backend.pid + 1, "cwd": str(self.new_folder), **arguments}
        foreground = [unreadable]
        if verified:
            foreground.insert(0, {"pid": backend.pid, "argv": [kind], "cwd": str(self.new_folder)})
        polls = 32 if expires else 1 if verified else 2
        backend.startup_foreground_polls = [foreground.copy() for _ in range(polls)]
        clock = [0.0]
        sleeps: list[float] = []
        proved: list[int] = []
        native_process = herdr.native_process

        def observe(pid: int, runtime: str) -> herdr.NativeAgentProcess:
            observed = native_process(pid, runtime)
            if backend.starts:
                proved.append(pid)
            return observed

        def sleep(seconds: float) -> None:
            self.assertEqual(len(backend.starts), 1)
            move = unit_move.read_folder_move(staff.folder_move_path(self.owner.id))
            self.assertIs(move.steps["resume"], unit_move.FolderStepState.PENDING)
            self.assertIs(pane_record.read("agents")["w1:p1"].get("ended"), True)
            sleeps.append(seconds)
            clock[0] += seconds

        with (mock.patch.object(herdr, "native_process", side_effect=observe),
              mock.patch.object(time, "monotonic", side_effect=lambda: clock[0]),
              mock.patch.object(time, "sleep", side_effect=sleep),
              mock.patch.object(unit_move, "folder_move_deadline", return_value=1.0)):
            result = self.move_folder()
        self.assert_moved(backend)
        self.assertEqual((len(backend.starts), len(backend.stops)), (1, 1))
        pane, command = backend.starts[0]
        self.assertEqual(pane, "w1:p1")
        self.assertEqual(backend.cwd, self.new_folder)
        resume = command.index("--resume" if kind == "claude" else "resume", command.index("exec") + 1)
        self.assertEqual(command[resume + 1], self.owner.current_session)
        self.assertGreater(len(sleeps), 0)
        move = unit_move.read_folder_move(staff.folder_move_path(self.owner.id))
        if expires:
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("resumed session is not yet proved; rerun move-folder", result.stderr)
            self.assertGreaterEqual(clock[0], 1.0)
            self.assertTrue(backend.startup_foreground_polls)
            self.assertEqual(proved, [])
            self.assertIs(move.steps["resume"], unit_move.FolderStepState.PENDING)
            self.assertTrue(all(move.steps[step] is unit_move.FolderStepState.DONE
                                for step in unit_move.FOLDER_MOVE_STEPS if step != "resume"))
            self.assertIs(pane_record.read("agents")["w1:p1"].get("ended"), True)
        else:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(sleeps), polls)
            self.assertEqual(backend.startup_foreground_polls, [])
            self.assertEqual(proved, [backend.pid] * (polls + 1 if verified else 1))
            self.assertIs(move.steps["resume"], unit_move.FolderStepState.DONE)
            self.assertIs(pane_record.read("agents")["w1:p1"].get("ended"), False)
            self.assert_finished_rerun(backend)

    def test_resume_retries_unreadable_arguments_until_the_process_is_readable(self) -> None:
        for kind in ("claude", "codex"):
            for arguments in UNREADABLE_ARGUMENTS:
                with self.subTest(kind=kind, arguments=arguments):
                    case = UnitFolderMoveTests()
                    case.setUp()
                    try:
                        case.assert_unreadable_resume(kind, arguments)
                    finally:
                        case.doCleanups()

    def test_resume_keeps_a_verified_agent_pending_beside_unreadable_arguments(self) -> None:
        for kind in ("claude", "codex"):
            for arguments in UNREADABLE_ARGUMENTS:
                with self.subTest(kind=kind, arguments=arguments):
                    case = UnitFolderMoveTests()
                    case.setUp()
                    try:
                        case.assert_unreadable_resume(kind, arguments, verified=True)
                    finally:
                        case.doCleanups()

    def test_resume_keeps_its_step_pending_when_arguments_stay_unreadable_until_the_deadline(self) -> None:
        for kind in ("claude", "codex"):
            for arguments in UNREADABLE_ARGUMENTS:
                with self.subTest(kind=kind, arguments=arguments):
                    case = UnitFolderMoveTests()
                    case.setUp()
                    try:
                        case.assert_unreadable_resume(kind, arguments, expires=True)
                    finally:
                        case.doCleanups()

    def test_unreadable_arguments_before_resume_refuse_without_sending_a_command(self) -> None:
        for kind in ("claude", "codex"):
            for arguments in UNREADABLE_ARGUMENTS:
                with self.subTest(kind=kind, arguments=arguments):
                    case = UnitFolderMoveTests()
                    case.setUp()
                    try:
                        backend = case.folder_backend(kind, live=True)
                        backend.foreground_before_start = [
                            {"pid": backend.pid, "argv": [kind], "cwd": str(case.old_folder)},
                            {"pid": backend.pid + 1, "cwd": str(case.old_folder), **arguments},
                        ]
                        paths = case.move_files()
                        before = [path.read_bytes() for path in paths]
                        with mock.patch.object(time, "sleep") as sleep:
                            result = case.move_folder()
                        case.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                        case.assertIn("foreground arguments are unreadable", result.stderr)
                        sleep.assert_not_called()
                        case.assertEqual((backend.starts, backend.stops), ([], []))
                        case.assertTrue(backend.live)
                        case.assertEqual(backend.cwd, case.old_folder)
                        case.assertTrue((case.old_folder / ".git").is_file())
                        case.assertFalse(case.new_folder.exists())
                        case.assertFalse(staff.folder_move_path(case.owner.id).exists())
                        case.assertEqual([path.read_bytes() for path in paths], before)
                    finally:
                        case.doCleanups()

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
              mock.patch.object(client_type, "result", side_effect=backend.result),
              mock.patch("scripts.shutdown.launch_permission.claim_launch_pane")):
            launched = herdr.launch_prepared_agent("agents", herdr.NewTab("production", "kept-name"),
                herdr.PreparedAgentEnvironment(self.old_folder, {"CODEX_HOME": str(backend.config)}),
                "codex", ["resume", self.owner.current_session], herdr.NoOpeningPrompt(), 1000,
                agent="kept-name")
        self.assertEqual(launched.pane, "w1:p1")
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(len(commands), 1)
        entry = pane_record.read("agents")[launched.pane]
        self.assertEqual(entry["config_dir"], str(backend.config))
        self.assertEqual((entry["cwd"], entry["session_id"]), (str(self.old_folder), self.owner.id))

        with mock.patch.object(herdr, "herdr_processes", side_effect=backend.observed_processes):
            result = self.move_folder()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(staff.by_id(self.owner.id),
                             replace(self.owner, worktree=staff.PresentWorktree("natedev", self.new_folder)))
            retained = pane_record.read("agents")["w1:p1"]
            self.assertEqual(retained["cwd"], str(self.new_folder))
            self.assertEqual(retained["config_dir"], str(backend.config))
            owner = staff.snapshot().by_member_reference(retained["session_id"])
            assert isinstance(owner, staff.StaffRow)
            self.assertEqual(owner.id, self.owner.id)
            (pane, resumed), = backend.starts
            self.assertEqual(pane, launched.pane)
            self.assertEqual(resumed[resumed.index("resume", resumed.index("exec")) + 1], self.owner.current_session)
            self.assertEqual(backend.cwd, self.new_folder)
            self.assert_finished_rerun(backend)

    def test_interactive_claude_move_allows_its_supervisor_and_refuses_an_unrelated_process(self) -> None:
        backend = self.folder_backend(live=True)
        supervisor = 998039
        commands: list[list[str]] = []

        def execute(_program: str, arguments: list[str], _environment: dict[str, str]) -> None:
            commands.append(arguments.copy())

        with (mock.patch.object(accounts, "prepared_account", return_value=accounts.PreparedAccount(
                  {"CLAUDE_CONFIG_DIR": str(backend.config)}, frozenset())),
              mock.patch.object(accounts, "launch_shell_environment", return_value={}),
              mock.patch.object(accounts, "resolve_program", return_value="/fixture/claude"),
              mock.patch.object(sys.stdin, "isatty", return_value=True),
              mock.patch.object(os, "execvpe", side_effect=execute)):
            _ = accounts.exec_account("claude4", ["--resume", self.owner.current_session, "-n", "kept-name"])
        command, = commands
        self.assertEqual(Path(command[1]).name, "relaunch.py")
        self.assertEqual(command[2:4], ["run", "--"])
        proc = self.root / "proc"
        for pid, parent, arguments in ((backend.pid, supervisor, command[4:]),
                                       (supervisor, backend.shell_pid, command),
                                       (backend.shell_pid, 1, ["bash"])):
            directory = proc / str(pid)
            directory.mkdir(parents=True)
            write_text_atomic(directory / "stat", f"{pid} (fixture) S {parent} " + "0 " * 18 + "1\n")
            write_bytes_atomic(directory / "cmdline", os.fsencode("\0".join(arguments) + "\0"))
            write_bytes_atomic(directory / "environ", os.fsencode(
                f"CLAUDE_CONFIG_DIR={backend.config}\0HERDR_PANE_ID=w1:p1\0HERDR_SESSION=agents\0"))
        unrelated = [998042]

        def inside(folder: Path) -> list[int]:
            return backend.processes_inside(folder) + ([supervisor, *unrelated] if backend.live else [])

        def liveness(pid: int, started: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
            self.assertIn(pid, (backend.pid, supervisor))
            self.assertEqual(started, "fixture-start")
            return sessions.LiveSessionProcess(pid) if backend.live else sessions.SessionNotLive()

        def environment(pid: int) -> dict[str, str]:
            if pid == supervisor:
                return {"CLAUDE_CONFIG_DIR": str(backend.config), "HERDR_PANE_ID": "w1:p1",
                        "HERDR_SESSION": "agents"}
            return backend.environment(pid)

        with (mock.patch.dict(os.environ, {"HERDR_PROC": str(proc)}),
              mock.patch("pane_record.PROC", proc), mock.patch.object(pane_record, "PROC", proc),
              mock.patch.object(herdr, "process_environment", side_effect=environment),
              mock.patch.object(sessions, "process_liveness", side_effect=liveness),
              mock.patch.object(unit_move, "processes_inside", side_effect=inside)):
            refused = self.move_folder()
            self.assertEqual(refused.returncode, 1)
            self.assertIn("998042", refused.stderr)
            self.assertEqual((backend.starts, backend.stops), ([], []))
            self.assertFalse(staff.folder_move_path(self.owner.id).exists())
            unrelated.clear()
            moved = self.move_folder()
        self.assertEqual(moved.returncode, 0, moved.stderr)
        self.assert_moved(backend)
        self.assertEqual((len(backend.starts), len(backend.stops)), (1, 1))
        self.assert_finished_rerun(backend)

    def test_interactive_supervisor_accepts_restored_and_explicit_prompts_but_not_a_namesake(self) -> None:
        for explicit_prompt in (False, True):
            with self.subTest(explicit_prompt=explicit_prompt):
                case = UnitFolderMoveTests()
                case.setUp()
                try:
                    backend = case.folder_backend(live=True)
                    supervisor, unrelated = 998039, 998042
                    environment = {"HERDR_ENV": "1", "HERDR_SESSION": "agents", "HERDR_PANE_ID": "w1:p1",
                                   "CLAUDE_CONFIG_DIR": str(backend.config)}
                    commands: list[list[str]] = []

                    def execute(_program: str, arguments: list[str], _environment: dict[str, str]) -> None:
                        commands.append(arguments.copy())

                    arguments = ["--resume", case.owner.current_session, "-n", "kept-name"]
                    if explicit_prompt:
                        arguments.append("Continue the assigned work.")
                    with (mock.patch.object(accounts, "prepared_account", return_value=accounts.PreparedAccount(
                              environment, frozenset())),
                          mock.patch.object(accounts, "launch_shell_environment", return_value=environment),
                          mock.patch.object(accounts, "resolve_program", return_value="/fixture/claude"),
                          mock.patch.object(sys.stdin, "isatty", return_value=True),
                          mock.patch.object(os, "execvpe", side_effect=execute)):
                        _ = accounts.exec_account("claude4", arguments)
                    command, = commands
                    child, child_environment = relaunch.restored_launch(command[4:], environment)
                    if explicit_prompt:
                        case.assertEqual(child, command[4:])
                    else:
                        case.assertEqual(child[:-1], command[4:])
                        case.assertEqual(len(child), len(command[4:]) + 1)
                    proc = case.root / "restored-proc"
                    for pid, parent, argv in ((backend.pid, supervisor, child),
                                              (supervisor, backend.shell_pid, command),
                                              (unrelated, backend.shell_pid, command),
                                              (backend.shell_pid, 1, ["bash"])):
                        directory = proc / str(pid)
                        directory.mkdir(parents=True)
                        write_text_atomic(directory / "stat", f"{pid} (fixture) S {parent} " + "0 " * 18 + "1\n")
                        write_bytes_atomic(directory / "cmdline", os.fsencode("\0".join(argv) + "\0"))
                        write_bytes_atomic(directory / "environ", os.fsencode("\0".join(
                            f"{key}={value}" for key, value in child_environment.items()) + "\0"))
                    extra = [unrelated]

                    def inside(folder: Path) -> list[int]:
                        return backend.processes_inside(folder) + ([supervisor, *extra] if backend.live else [])

                    def liveness(pid: int, started: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
                        case.assertIn(pid, (backend.pid, supervisor))
                        case.assertEqual(started, "fixture-start")
                        return sessions.LiveSessionProcess(pid) if backend.live else sessions.SessionNotLive()

                    def process_environment(pid: int) -> dict[str, str]:
                        return environment.copy() if pid == supervisor else backend.environment(pid)

                    with (mock.patch.dict(os.environ, {"HERDR_PROC": str(proc)}),
                          mock.patch("pane_record.PROC", proc), mock.patch.object(pane_record, "PROC", proc),
                          mock.patch.object(herdr, "process_environment", side_effect=process_environment),
                          mock.patch.object(sessions, "process_liveness", side_effect=liveness),
                          mock.patch.object(unit_move, "processes_inside", side_effect=inside)):
                        refused = case.move_folder()
                        case.assertEqual(refused.returncode, 1)
                        case.assertIn(str(unrelated), refused.stderr)
                        case.assertEqual((backend.starts, backend.stops), ([], []))
                        case.assertFalse(staff.folder_move_path(case.owner.id).exists())
                        extra.clear()
                        moved = case.move_folder()
                    case.assertEqual(moved.returncode, 0, moved.stderr)
                    case.assert_moved(backend)
                    case.assertEqual((len(backend.starts), len(backend.stops)), (1, 1))
                    case.assert_finished_rerun(backend)
                finally:
                    case.doCleanups()

    def test_stopped_unit_with_a_closed_pane_moves_records_without_starting(self) -> None:
        backend = self.folder_backend()
        entries = pane_record.read("agents")
        with (mock.patch.object(backend, "panes", return_value=[backend.panes()[1]]),
              mock.patch.object(unit_move, "processes_inside", return_value=[])):
            result = self.move_folder()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(pane_record.read("agents"),
                             {**entries, "w1:p1": {**entries["w1:p1"], "cwd": str(self.new_folder)}})
            self.assertFalse(self.old_folder.exists())
            self.assertEqual(staff.by_id(self.owner.id),
                             replace(self.owner, worktree=staff.PresentWorktree("natedev", self.new_folder)))
            state = cast(dict[str, object], json.loads((self.run_dir / "progress_history_state.json").read_text()))
            self.assertEqual(state["working_dir"], str(self.new_folder))
            self.assertEqual((backend.starts, backend.stops), ([], []))
            self.assert_finished_rerun(backend)

    def test_resumed_launch_is_active_and_a_finished_rerun_repairs_an_ended_record(self) -> None:
        for kind in ("claude", "codex"):
            with self.subTest(kind=kind):
                case = UnitFolderMoveTests()
                case.setUp()
                try:
                    backend = case.folder_backend(kind, live=True)
                    moved = case.move_folder()
                    self.assertEqual(moved.returncode, 0, moved.stderr)
                    launch = pane_record.read("agents")["w1:p1"]
                    self.assertIs(launch.get("ended"), False)
                    launch["ended"] = True
                    pane_record.write("agents", launch)
                    repaired = case.move_folder()
                    self.assertEqual((repaired.returncode, repaired.stderr), (0, ""))
                    self.assertIs(pane_record.read("agents")["w1:p1"].get("ended"), False)
                    self.assertEqual((len(backend.starts), len(backend.stops)), (1, 1))
                    case.assert_finished_rerun(backend)
                finally:
                    case.doCleanups()

    def test_stop_reads_changed_wait_limits_and_obeys_the_operation_deadline(self) -> None:
        backend = self.folder_backend(live=True)
        configuration = self.root / "production.conf"
        clock: list[float] = [0.0]
        signals: list[int] = []

        def sleep(seconds: float) -> None:
            clock[0] += seconds

        def signal_process(pid: int, _signal: int) -> None:
            self.assertEqual(pid, backend.pid)
            self.assertIs(pane_record.read("agents")["w1:p1"].get("ended"), True)
            signals.append(pid)

        launch = unit_move.folder_launch(self.owner)
        assert isinstance(launch, unit_move.FolderLaunch)
        with (mock.patch.dict(os.environ, {"PRODUCTION_CONFIG": str(configuration)}),
              mock.patch.object(time, "monotonic", side_effect=lambda: clock[0]),
              mock.patch.object(time, "sleep", side_effect=sleep),
              mock.patch.object(os, "kill", side_effect=signal_process),
              mock.patch.object(worker_lifecycle, "effective_deadline", return_value=1.0)):
            for configured, expected in ((0.35, 0.35), (0.65, 0.65), (2.0, 1.0)):
                with self.subTest(configured=configured):
                    _ = configuration.write_text(folder_move_config(configured, 3, 0.1))
                    clock[0] = 0.0
                    with self.assertRaisesRegex(ValueError, "session did not leave"):
                        unit_move.stop_folder_session(self.owner, launch, settings=conf.read(Path(os.environ["PRODUCTION_CONFIG"])))
                    self.assertAlmostEqual(clock[0], expected)
        self.assertEqual(signals, [backend.pid] * 3)
        self.assertEqual(backend.starts, [])

    def assert_interrupted_steps(self, *, after: bool) -> None:
        for step in ("stop", "worktree", "staff", "launch", "run", "resume"):
            with self.subTest(step=step, after=after):
                case = UnitFolderMoveTests()
                case.setUp()
                try:
                    backend = case.folder_backend(live=True)
                    save = unit_move.save_folder_move
                    reached: list[str] = []

                    def interrupt(path: Path, move: unit_move.FolderMove) -> None:
                        boundary = move.steps[cast(unit_move.FolderMoveStep, step)] is unit_move.FolderStepState.DONE
                        if boundary and not after:
                            reached.append(step)
                            raise OSError("interrupted " + step)
                        save(path, move)
                        if boundary and after:
                            reached.append(step)
                            raise OSError("interrupted " + step)

                    with mock.patch.object(unit_move, "save_folder_move", side_effect=interrupt):
                        failed = case.move_folder()
                    self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
                    self.assertIn("interrupted " + step, failed.stderr)
                    self.assertEqual(reached, [step])
                    resumed = case.move_folder()
                    self.assertEqual(resumed.returncode, 0, resumed.stderr)
                    case.assert_moved(backend)
                    self.assertEqual((len(backend.starts), len(backend.stops)), (1, 1))
                    case.assert_finished_rerun(backend)
                finally:
                    case.doCleanups()

    def test_rerun_finishes_after_each_step_before_progress_is_saved(self) -> None:
        self.assert_interrupted_steps(after=False)

    def test_rerun_finishes_after_each_saved_step(self) -> None:
        self.assert_interrupted_steps(after=True)

    def open_move_phase(self) -> None:
        configuration = self.root / "config"
        configuration.mkdir()
        write_text_atomic(configuration / "production.conf", folder_move_config(3, 3, 0.01))
        _ = self.enterContext(cast(AbstractContextManager[object], mock.patch.dict(os.environ, {
            "CONF_DIR": str(configuration), "PRODUCTION_CONFIG": str(configuration / "production.conf"),
        })))
        with self.history.open("a") as history:
            _ = history.write(json.dumps({"event_type": "phase_started",
                "phase_instance_id": "phase-one", "phase_id": "1"}) + "\n")

    def test_open_phase_running_unit_resumes_in_the_moved_folder(self) -> None:
        self.open_move_phase()
        backend = self.folder_backend(live=True)
        result = self.move_folder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_moved(backend)
        self.assertTrue(backend.live)
        self.assertEqual(backend.cwd, self.new_folder)
        self.assertEqual(backend.stops, [self.owner.current_session])
        (pane, command), = backend.starts
        self.assertEqual(pane, "w1:p1")
        self.assertEqual(command[command.index("--resume") + 1], self.owner.current_session)
        self.assertIn(str(self.new_folder), command)
        self.assert_finished_rerun(backend)

    def test_open_phase_paused_unit_stays_paused_after_the_move(self) -> None:
        self.open_move_phase()
        backend = self.folder_backend()
        result = self.move_folder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_moved(backend)
        self.assertEqual((backend.starts, backend.stops, backend.live), ([], [], False))
        self.assertIs(pane_record.read("agents")["w1:p1"].get("ended"), True)

    def assert_open_phase_worker_move(self, prior: Literal["running", "paused"]) -> None:
        from scripts.agents import codex_mesh

        self.open_move_phase()
        backend = self.folder_backend(live=True)
        thread = fixture_runtime_id("open-phase-worker")
        roster = self.run_dir / "mesh_roster.json"
        self.write_json(roster, {"impl1": {
            "thread_id": thread, "role": "impl", "status": prior,
            "cwd": str(self.old_folder), "codex_home": str(self.root / "home/.codex2"),
        }})
        pauses: list[str] = []
        starts: list[list[str]] = []
        launch_process = cast(Callable[..., subprocess.Popen[str]], subprocess.Popen)
        process_liveness = sessions.process_liveness

        def liveness(pid: int, started: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive | sessions.SessionLivenessUnknown:
            if pid == 998043:
                self.assertEqual(started, "fixture-start")
                return sessions.LiveSessionProcess(pid) if starts else sessions.SessionNotLive()
            return process_liveness(pid, started)

        def pause(arguments: argparse.Namespace) -> int:
            self.assertTrue(cast(bool, arguments.pause))
            self.assertEqual(cast(str, arguments.session_dir), str(self.run_dir))
            self.assertTrue(self.old_folder.exists())
            pauses.append(cast(str, arguments.session_dir))
            rows = self.read_rows()
            rows["impl1"]["status"] = "paused"
            self.write_json(roster, rows)
            return 0

        def launch(command: list[str], **options: object) -> subprocess.Popen[str]:
            if not any(Path(argument).name in ("implement.sh", "review.sh") for argument in command):
                return launch_process(command, **options)
            self.assertFalse(self.old_folder.exists())
            self.assertTrue(self.new_folder.is_dir())
            self.assertEqual(command[command.index("--to") + 1], "impl1")
            self.assertIn(str(self.new_folder), command)
            rows = self.read_rows()
            self.assertEqual(rows["impl1"]["thread_id"], thread)
            self.assertEqual(rows["impl1"]["status"], "paused")
            starts.append(command.copy())
            write_bytes_atomic(self.root / "folder-processes/998043/cmdline",
                               b"\0".join(os.fsencode(word) for word in command) + b"\0")
            rows["impl1"]["status"] = "running"
            self.write_json(roster, rows)
            return cast(subprocess.Popen[str], mock.Mock(pid=998043))

        with (mock.patch.object(codex_mesh, "command_stop", side_effect=pause),
              mock.patch.object(pane_record, "PROC", self.root / "folder-processes"),
              mock.patch("pane_record.PROC", self.root / "folder-processes"),
              mock.patch.object(codex_mesh, "open_unit_worker",
                                side_effect=AssertionError("A move must reuse the worker thread")),
              mock.patch.object(sessions, "process_liveness", side_effect=liveness),
              mock.patch.object(subprocess, "Popen", side_effect=launch)):
            result = self.move_folder()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assert_moved(backend)
            self.assertEqual(pauses, [str(self.run_dir)])
            self.assertEqual(len(starts), 1 if prior == "running" else 0)
            row = self.read_rows()["impl1"]
            self.assertEqual((row["thread_id"], row["status"]), (thread, prior))
            self.assert_finished_rerun(backend)
            self.assertEqual(pauses, [str(self.run_dir)])
            self.assertEqual(len(starts), 1 if prior == "running" else 0)

    def test_open_phase_running_worker_resumes_on_its_same_thread(self) -> None:
        self.assert_open_phase_worker_move("running")

    def test_open_phase_paused_worker_stays_paused(self) -> None:
        self.assert_open_phase_worker_move("paused")

    def prepare_worker_resume(self, cwd: Path) -> tuple[WorkerMoveRuntime, str]:
        self.open_move_phase()
        write_text_atomic(self.root / "config/production.conf", folder_move_config(3, 0.03, 0.001))
        backend = self.folder_backend(live=True)
        thread = fixture_runtime_id("recovering-folder-worker")
        cwd.mkdir(parents=True, exist_ok=True)
        self.write_json(self.run_dir / "mesh_roster.json", {"impl1": {
            "thread_id": thread, "role": "impl", "status": "running", "cwd": str(cwd),
            "codex_home": str(self.root / "home/.codex2"),
        }})
        return WorkerMoveRuntime(self, backend), thread

    def test_worker_without_a_saved_launcher_resumes_in_its_own_outside_folder(self) -> None:
        cwd = self.root / "worker-checkout"
        runtime, thread = self.prepare_worker_resume(cwd)
        moved = self.move_folder()
        self.assertEqual(moved.returncode, 0, moved.stdout + moved.stderr)
        self.assert_moved(runtime.backend)
        (command, launch_cwd), = runtime.starts
        self.assertEqual(launch_cwd, cwd)
        self.assertIn(str(cwd), command)
        self.assertEqual(command[command.index("--to") + 1], "impl1")
        row = self.read_rows()["impl1"]
        self.assertEqual((row["thread_id"], row["cwd"], row["status"]), (thread, str(cwd), "running"))
        self.assert_finished_rerun(runtime.backend)
        self.assertEqual(len(runtime.starts), 1)

    def test_rerun_relaunches_a_worker_whose_starting_launcher_died(self) -> None:
        runtime, thread = self.prepare_worker_resume(self.old_folder)
        runtime.launch_status = "starting"
        failed = self.move_folder()
        self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
        self.assertEqual(self.read_rows()["impl1"]["status"], "starting")
        self.assertEqual(len(runtime.starts), 1)
        runtime.live[998051] = False
        runtime.launch_status = "running"
        resumed = self.move_folder()
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual(len(runtime.starts), 2)
        self.assertEqual((self.read_rows()["impl1"]["thread_id"], self.read_rows()["impl1"]["status"]),
                         (thread, "running"))
        self.assert_moved(runtime.backend)
        self.assert_finished_rerun(runtime.backend)
        self.assertEqual(len(runtime.starts), 2)

    def test_resumed_worker_that_finishes_before_process_capture_counts_as_restored(self) -> None:
        runtime, thread = self.prepare_worker_resume(self.old_folder)

        def finish(command: list[str], **options: object) -> subprocess.Popen[str]:
            process = runtime.launch(command, **options)
            if process.pid in runtime.live:
                rows = self.read_rows()
                rows["impl1"]["status"] = "done"
                self.write_json(self.run_dir / "mesh_roster.json", rows)
                runtime.live[process.pid] = False
            return process

        with mock.patch.object(subprocess, "Popen", side_effect=finish):
            moved = self.move_folder()
        self.assertEqual(moved.returncode, 0, moved.stdout + moved.stderr)
        self.assert_moved(runtime.backend)
        self.assertEqual((self.read_rows()["impl1"]["thread_id"], self.read_rows()["impl1"]["status"]),
                         (thread, "done"))
        move = unit_move.read_folder_move(staff.folder_move_path(self.owner.id))
        self.assertTrue(move.steps["resume"])
        self.assertIsInstance(move.sessions, unit_move.FolderSessions)
        assert isinstance(move.sessions, unit_move.FolderSessions)
        worker, = move.sessions.workers
        self.assertTrue(worker.restored)
        self.assertIsInstance(worker.resume_process, unit_move.UnstartedWorkerResume)
        self.assert_finished_rerun(runtime.backend)
        self.assertEqual(len(runtime.starts), 1)

    def test_rerun_relaunches_a_dead_worker_even_when_its_roster_says_running(self) -> None:
        runtime, thread = self.prepare_worker_resume(self.old_folder)
        save = unit_move.save_folder_move

        def interrupt(path: Path, move: unit_move.FolderMove) -> None:
            if (isinstance(move.sessions, unit_move.FolderSessions)
                    and any(worker.restored for worker in move.sessions.workers)):
                raise OSError("interrupted before worker restore was saved")
            save(path, move)

        with mock.patch.object(unit_move, "save_folder_move", side_effect=interrupt):
            failed = self.move_folder()
        self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
        self.assertIn("interrupted before worker restore was saved", failed.stderr)
        self.assertEqual(self.read_rows()["impl1"]["status"], "running")
        self.assertEqual(len(runtime.starts), 1)
        runtime.live[998051] = False
        resumed = self.move_folder()
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual(len(runtime.starts), 2)
        self.assertEqual(self.read_rows()["impl1"]["thread_id"], thread)
        self.assert_moved(runtime.backend)
        self.assert_finished_rerun(runtime.backend)
        self.assertEqual(len(runtime.starts), 2)

    def test_worker_launcher_with_a_parent_path_component_is_owned_by_the_move(self) -> None:
        from scripts.agents import codex_mesh

        runtime, thread = self.prepare_worker_resume(self.old_folder)
        pid = 998045
        mesh = Path(codex_mesh.__file__).resolve().parents[1] / "direct/../agents/codex_mesh.py"
        runtime.publish_process(pid, [sys.executable, str(mesh), "send", "--session-dir", str(self.run_dir),
                                      "--to", "impl1"])
        rows = self.read_rows()
        rows["impl1"].update(launcher_pid=pid, launcher_start="fixture-start")
        self.write_json(self.run_dir / "mesh_roster.json", rows)

        def inside(folder: Path) -> list[int]:
            return runtime.backend.processes_inside(folder) + ([pid] if runtime.live[pid] else [])

        with mock.patch.object(unit_move, "processes_inside", side_effect=inside):
            moved = self.move_folder()
        self.assertEqual(moved.returncode, 0, moved.stdout + moved.stderr)
        self.assert_moved(runtime.backend)
        self.assertEqual(runtime.pauses, [str(self.run_dir)])
        self.assertEqual(len(runtime.starts), 1)
        self.assertEqual(self.read_rows()["impl1"]["thread_id"], thread)

    def assert_process_exit_during_pause(self) -> None:
        from scripts.agents import codex_mesh

        pid = 998045
        self.write_json(self.run_dir / "mesh_roster.json", {})
        selected = unit_move.read_folder_sessions({"unit": self.owner.current_session, "prior": "paused",
            "runs": [str(self.run_dir)], "workers": [],
            "processes": [{"pid": pid, "started": "fixture-start", "arguments": ["worker-launcher"]}]})
        self.open_move_phase()
        observations: list[int] = []

        def liveness(candidate: int, started: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
            self.assertEqual((candidate, started), (pid, "fixture-start"))
            observations.append(candidate)
            return sessions.LiveSessionProcess(pid) if len(observations) == 1 else sessions.SessionNotLive()

        with (mock.patch.object(codex_mesh, "command_stop", return_value=0),
              mock.patch.object(sessions, "process_liveness", side_effect=liveness),
              mock.patch.object(pane_record, "PROC", self.root / "folder-processes"),
              mock.patch("pane_record.PROC", self.root / "folder-processes"),
              mock.patch.object(os, "kill") as kill):
            unit_move.pause_folder_workers(selected, settings=conf.read(Path(os.environ["PRODUCTION_CONFIG"])))
        self.assertGreaterEqual(len(observations), 2)
        kill.assert_not_called()

    def assert_process_exit_during_resume(self) -> None:
        runtime, thread = self.prepare_worker_resume(self.old_folder)
        runtime.launch_status = "starting"
        failed = self.move_folder()
        self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
        self.assertEqual(len(runtime.starts), 1)
        rows = self.read_rows()
        rows["impl1"]["status"] = "paused"
        self.write_json(self.run_dir / "mesh_roster.json", rows)
        runtime.live[998051] = True
        runtime.vanish_pid = 998051
        runtime.launch_status = "running"
        resumed = self.move_folder()
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual(runtime.vanished, [998051])
        self.assertEqual(len(runtime.starts), 2)
        self.assertEqual(self.read_rows()["impl1"]["thread_id"], thread)
        self.assert_moved(runtime.backend)

    def test_worker_process_exit_during_argument_read_does_not_stop_pause_or_resume(self) -> None:
        for stage in ("pause", "resume"):
            with self.subTest(stage=stage):
                case = UnitFolderMoveTests()
                case.setUp()
                try:
                    if stage == "pause":
                        case.assert_process_exit_during_pause()
                    else:
                        case.assert_process_exit_during_resume()
                finally:
                    case.doCleanups()

    def assert_foreign_process_refused(self, *, open_phase: bool) -> None:
        if open_phase:
            self.open_move_phase()
        backend = self.folder_backend(live=True)
        paths = self.move_files()
        before = [path.read_bytes() for path in paths]
        with mock.patch.object(unit_move, "processes_inside", return_value=[backend.pid, 998042]):
            result = self.move_folder()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("998042", result.stderr)
        self.assertEqual([path.read_bytes() for path in paths], before)
        self.assertTrue(self.old_folder.exists())
        self.assertFalse(self.new_folder.exists())
        self.assertFalse(staff.folder_move_path(self.owner.id).exists())
        self.assertEqual((backend.starts, backend.stops), ([], []))

    def test_foreign_process_refuses_before_any_session_stops_with_or_without_an_open_phase(self) -> None:
        for open_phase in (False, True):
            with self.subTest(open_phase=open_phase):
                case = UnitFolderMoveTests()
                case.setUp()
                try:
                    case.assert_foreign_process_refused(open_phase=open_phase)
                finally:
                    case.doCleanups()

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
            state = cast(dict[str, object], json.loads((self.run_dir / "progress_history_state.json").read_text()))
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
