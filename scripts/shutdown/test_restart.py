"""Restart staff rows without a session journal."""
from __future__ import annotations

from scripts.shutdown import remote

import io
import math
import json
import os
import signal
import shlex
import shutil
import socketserver
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from threading import Event, RLock, Thread
from pathlib import Path
from typing import Literal, cast, override
from dataclasses import replace
from unittest.mock import call, patch

from scripts.shutdown import inventory
from scripts.herdr import herdr, pane_launch, worker_panes
from scripts.message import sessions
from scripts.shutdown import restart, shutdown
from scripts.shutdown.unit_staff_test_support import unit_host
from scripts.production import fake_unit_host, session_staff, staff, worker
from scripts.production import test_worker
from scripts.accounts import accounts
from scripts.whoami import agent_accounts, note_file
from scripts.whoami.account import Account, UnknownAccountName
from scripts.message import codex_daemon
from scripts.shutdown import shutdown_record, stop_resume, pane_record
from scripts.lib import file_lock, unit_runs
from scripts.lib.atomic_file import write_text_atomic
from scripts.production.staff_test_support import fixture_member
from scripts.herdr.fake_herdr import herdr_listener
from scripts.direct import progress_history
from scripts.shutdown.test_support import ShutdownTestCase, registry_confirmation

LAUNCH_PREPARED_AGENT = herdr.launch_prepared_agent
STOP_SESSION = stop_resume.stop


class RestartTestCase(ShutdownTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (home / "proc").mkdir()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(home), "CODEX_HOME": str(home / ".codex"),
            "HERDR_PROC": str(home / "proc"), "ROSTER_RUN_ROOT": str(home / "runs"),
            "UNIT_RUN_DIR": str(home / "run"), "PLAN_DELEGATE_BOARD_DIR": "",
            "XDG_STATE_HOME": str(home / "state"), "SHUTDOWN_STATE_DIR": str(home / "shutdown"),
        })))
        _ = self.enterContext(patch.object(herdr, "_PLATFORM", "linux"))
        _ = cast(object, self.enterContext(patch.multiple(unit_runs, RUN_ROOT=home / "runs",
                                                        FORMER_RUN_ROOT=home / "former-runs",
                                                        ASTRA_RUN_ROOT=home / "astra")))
        _ = self.enterContext(patch.object(codex_daemon, "read_daemons", return_value=[]))
        _ = self.enterContext(patch.object(sessions, "process_start", return_value="fixture-start"))
        def role_account(role: str) -> accounts.AccountSelection:
            return (accounts.CodexWorkersAccount("codex1", Path.home() / ".codex1") if role == "workers" else
                    accounts.NamedAccount("claude2", Path(os.environ["CLAUDE_CONFIG_DIR"])))
        _ = self.enterContext(patch.object(accounts, "role_account", side_effect=role_account))
        _ = self.enterContext(patch.object(herdr.Herdr, "result", return_value={
            "panes": [], "tabs": [], "workspaces": []}))


class ClaudeLaunchTrustTests(RestartTestCase):
    def test_concurrent_claude_launches_keep_both_repository_trust_keys(self) -> None:
        account = Path.home() / "account"
        account.mkdir()
        target = account / ".claude.json"
        write_text_atomic(target, "{}")
        folders = (Path.home() / "first-folder", Path.home() / "second-folder")
        for folder in folders:
            folder.mkdir()
        first_at_write, second_attempt, first_written = Event(), Event(), Event()
        read_text = Path.read_text
        lock_config = file_lock.claude_config_lock

        def read(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
            text = read_text(path, encoding=encoding, errors=errors)
            if path == target and first_at_write.is_set():
                second_attempt.set()
            return text

        @contextmanager
        def lock(path: Path) -> Generator[None]:
            if first_at_write.is_set():
                second_attempt.set()
            with lock_config(path):
                yield

        def publish(path: Path, text: str, *, new_file_mode: int | None = None) -> None:
            if not first_at_write.is_set():
                first_at_write.set()
                self.assertTrue(second_attempt.wait(5), "second writer did not attempt the account update")
                write_text_atomic(path, text, new_file_mode=new_file_mode)
                first_written.set()
            else:
                self.assertTrue(first_written.wait(5), "first writer did not publish its account update")
                write_text_atomic(path, text, new_file_mode=new_file_mode)

        with (patch.object(Path, "read_text", read),
              patch.object(file_lock, "claude_config_lock", lock),
              patch.object(herdr, "write_text_atomic", publish),
              ThreadPoolExecutor(max_workers=2) as writers):
            first = writers.submit(herdr.trust_claude_launch, folders[0], account)
            self.assertTrue(first_at_write.wait(5), "first writer did not reach publication")
            second = writers.submit(herdr.trust_claude_launch, folders[1], account)
            first.result(timeout=10)
            second.result(timeout=10)
        settings = cast(dict[str, dict[str, dict[str, bool]]], json.loads(target.read_text()))
        self.assertEqual(settings["projects"], {
            str(folder.resolve()): {"hasTrustDialogAccepted": True} for folder in folders})

    def test_claude_launch_without_config_dir_writes_the_home_account_file(self) -> None:
        folder = Path.home() / "launch-folder"
        folder.mkdir()
        target = Path.home() / ".claude.json"
        prepared = herdr.PreparedAgentEnvironment(folder, {})

        def start(*_args: object, **_kwargs: object) -> herdr.PromptSubmissionNotRequired:
            self.assertTrue(target.is_file(), "legacy account trust must exist before the start")
            settings = cast(dict[str, dict[str, dict[str, bool]]], json.loads(target.read_text()))
            self.assertTrue(settings["projects"][str(folder.resolve())]["hasTrustDialogAccepted"])
            return herdr.PromptSubmissionNotRequired()

        with (patch.object(herdr, "open_tab", return_value="w1:p1"),
              patch.object(herdr.Herdr, "run_in_pane"),
              patch.object(herdr, "start_agent", side_effect=start) as started):
            result = herdr.launch_prepared_agent("agents", herdr.NewTab("workers", "worker"),
                prepared, "claude", [], herdr.NoOpeningPrompt(), 5000, agent="worker")
        self.assertIsInstance(result.outcome, herdr.PromptSubmissionNotRequired)
        started.assert_called_once()
        self.assertFalse((Path.home() / ".claude/.claude.json").exists())


class CodexSnapshotTests(RestartTestCase):
    root: Path = Path()
    proc: Path = Path()
    rows: tuple[staff.StaffRow, ...] = ()
    claude: set[str] = set()
    codex: dict[int, str] = {}

    @override
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.rows = tuple(
            staff.StaffRow(staff.StaffId(f"row-{index}"), "agent", identity, staff.STAFF_ROOT,
                current_session=staff.StaffId(identity), worktree=staff.PresentWorktree(remote.local_machine(), self.root))
            for index, identity in enumerate(("claude-a", "codex-a", "claude-b", "codex-b")))
        self.claude = {"claude-a", "claude-b", "claude-orphan"}
        self.codex = {998081: "codex-a", 998082: "codex-b", 998083: "codex-orphan"}
        for pid in self.codex:
            path = self.proc / str(pid)
            path.mkdir()
            _ = (path / "cmdline").write_bytes(b"codex\0")
        _ = self.enterContext(patch.object(herdr, "process_environment", return_value={}))
        _ = self.enterContext(patch.object(herdr, "native_process", side_effect=self.native))
        _ = self.enterContext(patch.object(sessions, "session_liveness", side_effect=self.liveness))
        records = registry_confirmation(frozenset(self.claude)).records
        _ = self.enterContext(patch.object(sessions, "running_sessions", return_value=records))

    def native(self, pid: int, _kind: str) -> herdr.NativeAgentProcess:
        return herdr.NativeAgentProcess(herdr.EnvironmentThread(self.codex[pid]), "1", {})

    def liveness(self, identity: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
        return sessions.LiveSessionProcess(998080) if identity in self.claude else sessions.SessionNotLive()

    def test_live_rows_scans_codex_once_for_members_and_unassigned_candidates(self) -> None:
        with (patch.object(herdr, "_proc_root", return_value=self.proc) as scans,
              patch.object(herdr.Herdr, "save_layout") as save,
              redirect_stderr(io.StringIO()) as errors):
            selected = restart.live_rows(staff.StaffSnapshot(self.rows), herdr.Herdr("agents"), save_tabs=False)
        self.assertEqual(selected, list(self.rows))
        self.assertEqual(errors.getvalue().splitlines(), [
            "claude-orphan: restart anomaly: unassigned live session",
            "codex-orphan: restart anomaly: unassigned live session"])
        save.assert_not_called()
        scans.assert_called_once()

    def test_live_members_with_only_claude_rows_does_not_scan_codex(self) -> None:
        rows = self.rows[::2]
        with patch.object(herdr, "_proc_root", return_value=self.proc) as scans:
            self.assertEqual(restart.live_members(rows, staff.StaffSnapshot(self.rows)), list(rows))
        scans.assert_not_called()

    def test_live_members_shares_one_scan_and_reads_fresh_evidence_each_pass(self) -> None:
        with patch.object(herdr, "_proc_root", return_value=self.proc) as scans:
            self.assertEqual(restart.live_members(iter(self.rows), staff.StaffSnapshot(self.rows)), list(self.rows))
            scans.assert_called_once()
            self.codex[998081] = "changed-thread"
            self.assertEqual(restart.live_members(iter(self.rows), staff.StaffSnapshot(self.rows)), list(self.rows[::2]) + [self.rows[3]])
            self.assertEqual(scans.call_count, 2)

    def test_live_members_uses_a_supplied_snapshot_without_a_scan(self) -> None:
        snapshot = herdr.CodexConversations({"codex-a": (998081,)}, ())
        with patch.object(herdr, "_proc_root", return_value=self.proc) as scans:
            self.assertEqual(restart.live_members(self.rows, staff.StaffSnapshot(self.rows), codex=snapshot), list(self.rows[:3]))
        scans.assert_not_called()

    def test_live_rows_reports_one_shared_cause_and_one_unproved_row_count(self) -> None:
        snapshot = herdr.CodexConversations({}, ("test-account (Codex daemon unknown: unreadable)",))
        with (patch.object(herdr, "codex_conversations", return_value=snapshot) as scan,
              patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()),
              patch.object(sessions, "running_sessions", return_value=[]),
              patch.dict(os.environ, {"SHUTDOWN_DETAIL": "0"}),
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.live_rows(staff.StaffSnapshot(self.rows), herdr.Herdr("agents")), [])
        scan.assert_called_once()
        self.assertEqual(errors.getvalue().splitlines(), [
            "anomaly: test-account (Codex daemon unknown: unreadable); not blocking",
            "anomaly: 4 rows not proved live or stopped (shared cause above); not blocking"])

    def test_live_rows_detail_reports_each_row_with_shared_uncertainty(self) -> None:
        snapshot = herdr.CodexConversations({}, ("test-account (Codex daemon unknown: unreadable)",))
        with (patch.object(herdr, "codex_conversations", return_value=snapshot),
              patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()),
              patch.object(sessions, "running_sessions", return_value=[]),
              patch.dict(os.environ, {"SHUTDOWN_DETAIL": "1"}),
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.live_rows(staff.StaffSnapshot(self.rows), herdr.Herdr("agents")), [])
        lines = errors.getvalue().splitlines()
        self.assertEqual(len(lines), 6)
        for row, line in zip(self.rows, lines[1:-1], strict=True):
            self.assertIn(f"{row.current_session} is not proved live or stopped", line)
        self.assertEqual(lines[-1], "anomaly: 4 rows not proved live or stopped (shared cause above); not blocking")

    def test_live_members_reports_shared_evidence_once_when_it_reads_the_snapshot(self) -> None:
        snapshot = herdr.CodexConversations({}, ("test-account (Codex daemon unknown: unreadable)",))
        with (patch.object(herdr, "codex_conversations", return_value=snapshot) as scan,
              patch.object(herdr, "running_codex_threads") as threads,
              patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()),
              patch.dict(os.environ, {"SHUTDOWN_DETAIL": "0"}),
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.live_members(self.rows, staff.StaffSnapshot(self.rows)), [])
        scan.assert_called_once()
        threads.assert_not_called()
        self.assertEqual(errors.getvalue().splitlines(), [
            "anomaly: test-account (Codex daemon unknown: unreadable); not blocking",
            "anomaly: 4 rows not proved live or stopped (shared cause above); not blocking"])


def checkout() -> inventory.GitCheckout:
    return inventory.GitCheckout(
        kind="git", path="/tmp/checkout", head=inventory.OnBranch(kind="branch", name="work"),
        upstream=inventory.Tracking(kind="tracking", ahead=0), dirty=[])


def common_session(session_id: str, name: str | None = None, *, cwd: str | None = None,
    model: inventory.LastModel | None = None, run_dirs: list[str] | None = None,
) -> inventory.SessionFields:
    return {
        "session_id": session_id, "config_dir": str(Path.home() / ".claude2"),
        "pid": 1000 + len(session_id), "proc_start": f"start-{session_id}",
        "name": name or session_id, "cwd": cwd or f"/tmp/{session_id}",
        "status": "idle", "updated_at": 0, "probe": False,
        "model": model or inventory.NoReplyYet(kind="no reply yet"),
        "checkout": checkout(), "run_dirs": list(run_dirs or []), "codex_servers": [], "timers": [],
    }


def top_level(session_id: str, *, name: str | None = None, cwd: str | None = None,
    model: inventory.LastModel | None = None, host: inventory.SessionHost | None = None,
    run_dirs: list[str] | None = None, status: str = "idle",
) -> inventory.TopLevelSession:
    result = inventory.TopLevelSession(
        **common_session(session_id, name, cwd=cwd, model=model, run_dirs=run_dirs),
        kind="top-level", host=host or inventory.UnknownHost(kind="unknown"))
    result["status"] = status
    return result


def showrunner(session_id: str, *, host: inventory.SessionHost | None = None) -> inventory.ShowrunnerSession:
    return inventory.ShowrunnerSession(
        **common_session(session_id), kind="showrunner",
        host=host or inventory.HerdrHost(kind="herdr", herdr_session=f"herdr-{session_id}", pane_id="w1:p1", presentation="detached"),
        production="demo", doc="/tmp/demo-production.md")


def unit(session_id: str, *, plan: inventory.UnitPlan | None = None, session_name: str | None = None,
    session_host: Literal["herdr"] = "herdr", run_dirs: list[str] | None = None, doc: str = "/tmp/demo production.md",
) -> inventory.UnitSession:
    return inventory.UnitSession(
        **common_session(session_id, run_dirs=run_dirs), kind="unit",
        host=unit_host(kind="unit", production="demo", unit=f"{session_id}-unit", doc=doc,
            session_name=session_name or f"herdr-{session_id}", session_host=session_host,
            plan=plan or inventory.PlanFromRunRecord(kind="plan", path=f"/tmp/{session_id} plan.md")))


def worker_session(session_id: str, *, owner: str | None, run_dirs: list[str] | None = None) -> inventory.WorkerSession:
    worker_owner: inventory.WorkerOwner = (
        inventory.DirectorOwner(kind="director", session_id=owner)
        if owner is not None
        else inventory.NoDirector(kind="no director")
    )
    return inventory.WorkerSession(
        **common_session(session_id, run_dirs=run_dirs), kind="worker",
        host=inventory.UnknownHost(kind="unknown"), owner=worker_owner)


class SessionLaunchTests(RestartTestCase):
    def test_restart_reads_a_claude_transcript_without_a_pane_launch_record(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        identity = "saved"
        records = root / "panes"
        records.mkdir()
        config = root / "config"
        project = config / "projects" / "project"
        project.mkdir(parents=True)
        (project / f"{identity}.jsonl").touch()
        output = io.StringIO()
        with (patch.object(pane_record, "RECORD_DIR", records),
              patch("scripts.shutdown.restart.sessions.config_dirs", return_value=[config]),
              patch.object(restart, "named_account_environment", return_value={"CLAUDE_CONFIG_DIR": "/account"}),
              redirect_stderr(output)):
            kind, environment, arguments = restart.session_launch(identity, "current-name")
        self.assertEqual(kind, "claude")
        self.assertEqual(environment, {"CLAUDE_CONFIG_DIR": "/account"})
        self.assertEqual(arguments, ["--resume", identity, "--name", "current-name"])
        self.assertEqual(output.getvalue(), "")

    def test_restart_reads_plugin_directories_from_the_shutdown_pane_record(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        directories = [str(root / "postmaster"), str(root / "second plugin")]
        entry = pane_record.launch("w1:p2", "saved", str(root), {},
            ["claude", "--plugin-dir", directories[0], "--plugin-dir=" + directories[1]])
        with (patch.object(pane_record, "RECORD_DIR", root / "panes"),
              patch.object(restart, "named_account_environment", return_value={"CLAUDE_CONFIG_DIR": "/account"})):
            pane_record.write("agents", entry)
            kind, environment, arguments = restart.session_launch("saved", "current-name")
        self.assertEqual(kind, "claude")
        self.assertEqual(environment, {"CLAUDE_CONFIG_DIR": "/account"})
        parsed = pane_record.parse_claude_arguments(["claude", *arguments])
        self.assertEqual(parsed.plugin_directories, tuple(directories))
        self.assertEqual(parsed.values["--resume"], "saved")
        self.assertEqual(arguments.count("--plugin-dir"), 2)


class RestartFixtureTests(RestartTestCase):
    def test_fixture_excludes_host_sessions_from_orphan_candidates(self) -> None:
        host = Path(self.enterContext(tempfile.TemporaryDirectory()))
        process = host / "proc/998099"
        process.mkdir(parents=True)
        write_text_atomic(process / "cmdline", "codex\0")
        transcript = host / ".claude/projects/host/host-session.jsonl"
        write_text_atomic(transcript, "{}\n")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HOME": str(host), "HERDR_PROC": str(host / "proc")})))
        _ = self.enterContext(patch.object(herdr, "_PLATFORM", "linux"))
        _ = self.enterContext(patch.object(herdr, "process_environment", return_value={}))
        _ = self.enterContext(patch.object(herdr, "native_process", return_value=herdr.NativeAgentProcess(
            herdr.EnvironmentThread("host-session"), "fixture-start", {})))
        _ = self.enterContext(patch.object(unit_runs, "run_roots", return_value=()))
        _ = self.enterContext(patch.object(unit_runs, "ASTRA_RUN_ROOT", host / "astra"))
        _ = self.enterContext(patch.object(codex_daemon, "read_daemons", return_value=[]))
        _ = self.enterContext(patch.object(herdr.Herdr, "result", return_value={"panes": [{
            "pane_id": "w9:p9", "workspace_id": "w9", "session_id": "host-session", "label": "host"}]}))
        fixture = SessionLaunchTests("test_restart_reads_a_claude_transcript_without_a_pane_launch_record")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(restart.live_rows(staff.StaffSnapshot(()), herdr.Herdr("agents")), [])
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(herdr.Herdr("agents").panes(), [])
        self.assertNotIn(host / ".claude", sessions.config_dirs())
        self.assertEqual(pane_record.launches(), [])
        self.assertTrue(Path(os.environ["STAFF_FILE"]).is_relative_to(Path(os.environ["NOTIFIER_SESSIONS_DIR"]).parent))


class RecordedResumeTests(RestartTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        configuration = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for source in (Path(__file__).resolve().parents[2] / "config").glob("*.conf"):
            write_text_atomic(configuration / source.name, source.read_text())
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(configuration)})))

    folder: Path = Path("/tmp/recorded-resume")

    def entry(self, kind: Literal["claude", "codex"], model: str) -> pane_record.PaneLaunch:
        return {"kind": kind, "pane_id": "w1:p1", "session_id": "recorded-id",
                "config_dir": "/account", "cwd": str(self.folder), "name": "recorded-name",
                "model": model, "remote_control": True, "environment": {}, "flags_read": True,
                "plugin_directories": ["/plugins/first", "/plugins/second plugin"], "written_at": "fixture"}

    def resumed_arguments(self, entry: pane_record.PaneLaunch) -> list[str]:
        row = staff.StaffRow(staff.StaffId("row"), "agent", "current-name", staff.StaffRoot(),
                            current_session=staff.StaffId("current-id"),
                            worktree=staff.PresentWorktree(remote.local_machine(), self.folder))
        with (patch.object(pane_record, "launch_for_session", return_value=entry),
              patch.object(restart, "named_account_environment", return_value={"CLAUDE_CONFIG_DIR": "/account"}),
              patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(herdr.Herdr, "saved_row_tab", return_value=herdr.NewTab("saved", row.name)),
              patch.object(herdr.Herdr, "panes", return_value=[]),
              patch.object(herdr.Herdr, "remember_row_tab"),
              patch.object(herdr, "prepare_session_environment", return_value=herdr.PreparedAgentEnvironment(self.folder, {})),
              patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                  "w1:p1", herdr.PromptSubmissionNotRequired())) as launch):
            self.assertEqual(restart.start_row(row, staff.StaffSnapshot((row,)), herdr.Herdr("agents")), "w1:p1")
        return cast(list[str], launch.call_args.args[4])

    def test_codex_resume_keeps_the_recorded_model_and_omits_an_empty_model(self) -> None:
        folder = str(self.folder.resolve())
        for model in ("", "codex-model"):
            with self.subTest(model=model):
                arguments = self.resumed_arguments(self.entry("codex", model))
                expected = ["-C", folder, "-c",
                            f'projects={{"{folder}"={{trust_level="trusted"}}}}', "resume", "current-id"]
                if model:
                    expected.extend(("--model", model))
                self.assertEqual(arguments, expected)

    def test_claude_resume_keeps_model_plugins_and_one_current_name(self) -> None:
        for recorded_name in ("recorded-name", ""):
            with self.subTest(recorded_name=recorded_name):
                entry = self.entry("claude", "claude-model")
                entry["name"] = recorded_name
                arguments = self.resumed_arguments(entry)
                expected = ["--resume", "current-id", "-n", "current-name", "--remote-control", "current-name"]
                expected.extend(("--model", "claude-model", "--plugin-dir", "/plugins/first",
                                 "--plugin-dir", "/plugins/second plugin", "--settings", '{"disableAgentView": true}'))
                self.assertEqual(arguments, expected)
                names = [arguments[index + 1] for index, value in enumerate(arguments) if value in ("-n", "--name")]
                self.assertEqual(names, ["current-name"])

    def test_resume_uses_requested_identity_without_changing_the_record(self) -> None:
        for kind in ("claude", "codex"):
            with (self.subTest(kind=kind),
                  patch.object(pane_record, "launch_for_session", return_value=(entry := self.entry(kind, "saved-model"))),
                  patch.object(restart, "named_account_environment", return_value={"CLAUDE_CONFIG_DIR": "/account"})):
                _kind, _environment, arguments = restart.session_launch("requested-id", "requested-name")
                self.assertEqual(arguments[:2], ["resume" if kind == "codex" else "--resume", "requested-id"])
                self.assertIn("--model", arguments)
                self.assertEqual(arguments[arguments.index("--model") + 1], "saved-model")
                self.assertEqual(entry["session_id"], "recorded-id")
                self.assertEqual(entry["name"], "recorded-name")
                if kind == "claude":
                    self.assertEqual(arguments[arguments.index("-n") + 1], "requested-name")
                    self.assertEqual(arguments[arguments.index("--remote-control") + 1], "requested-name")
                else:
                    self.assertNotIn("-n", arguments)
                    self.assertNotIn("--name", arguments)


class MixedRestartTests(ShutdownTestCase):
    maxDiff: int | None = None

    def test_mixed_restart_and_stop_share_all_full_reads(self) -> None:
        self.run_commands(("restart", "stop"), frozenset())

    def test_failed_worker_stop_keeps_the_live_process_pending(self) -> None:
        self.run_commands(("restart",), frozenset({991104}))

    def test_count_fixture_selects_linux_readers_on_macos(self) -> None:
        inherited = (sys.platform, herdr._PLATFORM)  # pyright: ignore[reportPrivateUsage]
        self.addCleanup(lambda: self.assertEqual((sys.platform, herdr._PLATFORM), inherited))  # pyright: ignore[reportPrivateUsage]
        with patch.object(herdr, "_PLATFORM", "darwin"), patch.object(sys, "platform", "darwin"):
            self.run_commands(("stop",), frozenset())

    def run_commands(self, commands: tuple[str, ...], surviving: frozenset[int]) -> None:
        with patch.object(herdr, "_PLATFORM", "linux"), patch.object(sys, "platform", "linux"):
            self.linux_commands(commands, surviving)

    def linux_commands(self, commands: tuple[str, ...], surviving: frozenset[int]) -> None:
        self.assertEqual(herdr._PLATFORM, "linux")  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(sys.platform, "linux")
        from scripts.agents import codex_mesh

        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="mixed-", dir="/tmp")))
        write_text_atomic(root / "staff.json", '{"schema": 2, "rows": []}\n')
        environment = {"HOME": str(root), "STAFF_FILE": str(root / "staff.json"), "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_STATE_HOME": str(root / "state"), "SHUTDOWN_STATE_DIR": str(root / "shutdown"),
            "HERDR_PROC": str(root / "proc"), "HERDR_PANE_RECORD_DIR": str(root / "panes"),
            "CLAUDE_CONFIG_DIR": str(root / ".claude2"), "CODEX_HOME": str(root / ".codex1"),
            "ROSTER_RUN_ROOT": str(root / "runs"), "UNIT_RUN_DIR": str(root / "runs/pane"),
            "CONF_DIR": str(root / "conf"), "SHUTDOWN_PROOF_WORKSPACE": "w1",
            "MODELS_CONFIG_FILE": str(root / "conf/models.conf"), "MODELS_CONFIG_PUBLISH": "0",
            "AGENTS_CALLER_FAMILY": "", "CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": ""}
        _ = cast(object, self.enterContext(patch.dict(os.environ, environment)))
        for source in (Path(__file__).resolve().parents[2] / "config").glob("*.conf"):
            write_text_atomic(root / "conf" / source.name, source.read_text())
        write_text_atomic(root / "conf/models.conf",
            Path(__file__).resolve().parents[1].joinpath("lib/test_models_registry.conf").read_text())
        directory = staff.PresentWorktree(remote.local_machine(), root)
        doc = root / "production.md"
        write_text_atomic(doc, "# Fixture\n")
        show = fixture_member("showrunner", name="mixed-show", doc=staff.LiveDoc(remote.local_machine(), doc))
        (root / "mesh").mkdir()
        mesh = fixture_member("unit", name="mixed-mesh", parent=show.id,
            worktree=staff.PresentWorktree(remote.local_machine(), root / "mesh"))
        seat = fixture_member("worker", name="mixed-seat", parent=mesh.id)
        owner = fixture_member("unit", name="mixed-pane", parent=show.id, worktree=directory)
        workers = [fixture_member("worker", name=name, parent=owner.id,
            backend=staff.CodexBackend(root / ".codex1")) for name in ("impl", "review")]
        rows = [show, mesh, seat, owner, *workers]
        run = root / "runs/pane"
        mesh_run = root / "runs/mesh"
        write_text_atomic(mesh_run / "worker_transport", "mesh\n")
        write_text_atomic(mesh_run / "progress_history_state.json", json.dumps({"owner_staff_id": mesh.id,
            "main_agent": {"session_id": mesh.current_session}, "pass": {}, "events": []}))
        write_text_atomic(run / "worker_transport", "herdr\n")
        write_text_atomic(run / "progress_history_state.json", json.dumps({"owner_staff_id": owner.id,
            "phase": {"id": "work", "instance_id": "active", "status": "active", "started_at": 1},
            "main_agent": {"session_id": owner.current_session, "family": "claude"},
            "history_file": str(run / "history.jsonl"), "pass": {}, "events": []}))
        prompt = run / "prompt.md"
        write_text_atomic(prompt, "Preserve behavior.\n")
        for row in workers:
            _ = progress_history.begin_dispatch(run, slot=row.name, phase="work", role="impl", worker_name=row.name,
                worker_id=row.id, launcher_command=["assign.sh"], prompt=prompt, kind="impl", lens="")
        panes: dict[str, dict[str, object]] = {f"w1:p{n}": {"pane_id": f"w1:p{n}", "terminal_id": f"term-{n}", "workspace_id": "w1",
            "tab_id": "w1:t3" if n >= 3 else f"w1:t{n}", "label": row.name, "agent_status": "idle"}
            for n, row in enumerate(rows) if n != 2}
        live: dict[str, int] = {}
        unconfirmed: dict[str, int] = {}
        shell_env: dict[str, dict[str, str]] = {}
        requests: list[str] = []
        signals: list[int] = []
        launches: list[str] = []
        lock = RLock()
        proc = root / "proc"

        def process(pid: int, parent: int, argv: list[str], marks: dict[str, str]) -> None:
            folder = proc / str(pid)
            write_text_atomic(folder / "stat", f"{pid} (fixture) S {parent} {pid} 0 0 {pid} " + "0 " * 13 + "1\n")
            write_text_atomic(folder / "cmdline", "\0".join(argv) + "\0")
            write_text_atomic(folder / "environ", "\0".join(f"{key}={value}" for key, value in marks.items()) + "\0")
            write_text_atomic(folder / "task" / str(pid) / "children", "")
            if not (folder / "cwd").exists():
                (folder / "cwd").symlink_to(root)

        def start(pane: str, argv: list[str]) -> None:
            index = int(pane.removeprefix("w1:p"))
            row = rows[index]
            pid, shell = 991100 + index, 991000 + index
            if (proc / str(pid)).exists():
                pid += 100
            marks = {**environment, **shell_env.get(pane, {}), "HERDR_SESSION": "agents", "HERDR_PANE_ID": pane}
            process(pid, shell, argv, marks)
            write_text_atomic(proc / str(shell) / "task" / str(shell) / "children", str(pid))
            record: sessions.SessionRecord = {"pid": pid, "procStart": "1", "sessionId": row.current_session,
                "name": row.name, "updatedAt": int(time.time()), "messagingSocketPath": "", "tmux": "", "formerNames": []}
            if argv[0] == "claude":
                for folder in (sessions.sessions_dir(), Path(marks["CLAUDE_CONFIG_DIR"]) / "sessions"):
                    write_text_atomic(folder / f"{pid}.json", json.dumps(record))
            else:
                rollout = root / ".codex1/sessions" / f"rollout-{row.current_session}.jsonl"
                write_text_atomic(rollout, json.dumps({"type": "session_meta", "payload": {"id": row.current_session}}) + "\n")
            live[pane] = pid
            panes[pane]["agent_session"] = {"kind": "id", "value": row.current_session}

        def info(pane: str) -> dict[str, object]:
            shell = 991000 + int(pane.removeprefix("w1:p"))
            pid = live.get(pane, unconfirmed.get(pane, shell))
            argv = (proc / str(pid) / "cmdline").read_text().strip("\0").split("\0")
            return {"process_info": {"shell_pid": shell, "foreground_process_group_id": pid,
                "foreground_processes": [{"pid": pid, "name": argv[0], "argv": argv, "cwd": str(root / "mesh" if pane == "w1:p1" else root)}]}}

        def reply(request: dict[str, object]) -> dict[str, object]:
            method = str(request.get("method", ""))
            requests.append(method)
            params = cast(dict[str, object], request.get("params", {}))
            if method == "pane.process_info":
                return info(str(params["pane_id"]))
            if method == "layout.export":
                return {"layout": {"root": {"type": "pane", "pane_id": "w1:p3"}}}
            arguments = cast(list[str], request.get("argv", []))[2:]
            action = arguments[:2]
            if action == ["status", "server"]:
                return {"running": True}
            if action == ["pane", "list"]:
                return {"panes": list(panes.values())}
            if action == ["pane", "process-info"]:
                return info(arguments[-1])
            if action == ["agent", "list"]:
                return {"agents": [{"pane_id": pane, "agent_status": "idle", "agent": "fixture"} for pane in live]}
            if action == ["workspace", "list"]:
                return {"workspaces": [{"workspace_id": "w1", "label": show.name}]}
            if action == ["tab", "list"]:
                return {"tabs": [{"tab_id": f"w1:t{n}", "workspace_id": "w1", "label": row.name}
                    for n, row in enumerate(rows[:4]) if n != 2]}
            if action == ["pane", "run"]:
                tokens = shlex.split(shlex.split(arguments[3])[1])
                shell_env[arguments[2]] = dict(value.split("=", 1) for value in tokens[tokens.index("export") + 1:])
            elif action == ["agent", "start"]:
                pane = arguments[arguments.index("--pane") + 1]
                kind = arguments[arguments.index("--kind") + 1]
                launches.append(pane)
                start(pane, [kind, *arguments[arguments.index("--") + 1:]])
            elif action == ["pane", "read"]:
                return {"text": ""}
            return {}

        class Handler(socketserver.StreamRequestHandler):
            @override
            def handle(self) -> None:
                request = cast(dict[str, object], json.loads(self.rfile.readline()))
                with lock:
                    response = reply(request)
                _ = self.wfile.write((json.dumps({"result": response}) + "\n").encode())

        endpoint = root / "config/herdr/sessions/agents/herdr.sock"
        endpoint.parent.mkdir(parents=True)
        server = socketserver.ThreadingUnixStreamServer(str(endpoint), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        cli = root / "herdr"
        write_text_atomic(cli, f"#!{sys.executable}\n" +
            "import json, socket, sys\n" + f"with socket.socket(socket.AF_UNIX) as stream:\n    stream.connect({str(endpoint)!r})\n" +
            "    stream.sendall((json.dumps({'argv': sys.argv[1:]}) + '\\n').encode())\n" +
            "    response = stream.makefile().readline()\n" +
            "    print('status: running' if sys.argv[-2:] == ['status', 'server'] else response, end='\\n')\n", new_file_mode=0o700)
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HERDR_BIN": str(cli)})))
        _ = cast(object, self.enterContext(patch.multiple(unit_runs, RUN_ROOT=root / "runs", FORMER_RUN_ROOT=root / "former", ASTRA_RUN_ROOT=root / "astra")))
        def role_account(role: str) -> accounts.AccountSelection:
            return (accounts.CodexWorkersAccount("codex1", root / ".codex1") if role == "workers" else
                    accounts.NamedAccount("claude2", root / ".claude2"))
        _ = self.enterContext(patch.object(accounts, "role_account", side_effect=role_account))
        process(990999, 1, ["herdr", "server"], {})
        herdr_listener(proc, 990999, "agents", os.environ)
        for pane in panes:
            process(991000 + int(pane.removeprefix("w1:p")), 990999, ["zsh"], {})
        read_bytes, kill = Path.read_bytes, os.kill
        def proc_bytes(path: Path) -> bytes:
            return read_bytes(proc / path.relative_to("/proc")) if str(path).startswith("/proc/991") else read_bytes(path)
        def signal_process(pid: int, number: int) -> None:
            if not 990999 <= pid < 991300:
                return kill(pid, number)
            if not (proc / str(pid)).exists():
                raise ProcessLookupError(pid)
            if number:
                signals.append(pid)
                pane = next(pane for pane, current in live.items() if current == pid)
                del live[pane]
                if pid in surviving:
                    unconfirmed[pane] = pid
                    raise PermissionError("TEST worker stop is unconfirmed")
                _ = panes[pane].pop("agent_session", None)
                write_text_atomic(proc / str(991000 + int(pane.removeprefix("w1:p"))) / "task" /
                    str(991000 + int(pane.removeprefix("w1:p"))) / "children", "")
                shutil.rmtree(proc / str(pid))
        _ = self.enterContext(patch.object(Path, "read_bytes", proc_bytes))
        _ = self.enterContext(patch.object(os, "kill", side_effect=signal_process))
        for command in commands:
            for pid in tuple(live.values()):
                signal_process(pid, signal.SIGTERM)
            requests.clear()
            signals.clear()
            launches.clear()
            for pane in panes:
                kind = "codex" if int(pane[-1]) >= 4 else "claude"
                start(pane, [kind, "resume" if kind == "codex" else "--resume", str(rows[int(pane[-1])].current_session)])
            with (patch.object(unit_runs, "held_by_session", wraps=unit_runs.held_by_session) as searches,
                  patch.object(herdr, "codex_conversations", wraps=herdr.codex_conversations) as scans,
                  patch.object(herdr, "_scan_herdr_processes", wraps=herdr._scan_herdr_processes) as processes,  # pyright: ignore[reportPrivateUsage]
                  patch.object(codex_mesh, "_pause_roster") as pause,
                  patch.object(worker_panes, "start_or_resume", wraps=worker_panes.start_or_resume) as starts,
                  patch.object(pane_record, "seed", wraps=pane_record.seed) as seeds,
                  patch.object(herdr.Herdr, "order_tabs", autospec=True, side_effect=herdr.Herdr.order_tabs) as orders,
                  redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(shutdown.main([command, "--all"]), 0, errors.getvalue())
            if surviving:
                self.assertNotIn("w1:p4", launches, errors.getvalue())
                self.assertTrue((proc / "991104/stat").exists())
                self.assertNotIn("w1:p4", live)
                self.assertEqual(shutdown_record.read(), [str(workers[0].id)])
                self.assertEqual(sum("impl:" in line and "target process still runs" in line
                    for line in errors.getvalue().splitlines()), 1, errors.getvalue())
                self.assertIn("w1:p5", launches)
                continue
            self.assertEqual(errors.getvalue(), "", output.getvalue())
            # Stop uses direct pane reads. Restart also classifies tabs for ordering.
            self.assertEqual((searches.call_count, scans.call_count, processes.call_count),
                             (1, 1, int(command == "restart")), command)
            pause.assert_called_once()
            self.assertCountEqual(signals, [991100 + index for index in (0, 1, 3, 4, 5)])
            self.assertEqual(len(launches), 5 if command == "restart" else 0)
            self.assertEqual(len(live), 5 if command == "restart" else 0)
            self.assertGreater(seeds.call_count, 0)
            self.assertIn("pane.process_info", requests)
            self.assertEqual(starts.call_count, 2 if command == "restart" else 0)
            self.assertEqual(bool(orders.call_count), command == "restart")


class WorkerRestartTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.case: test_worker.WorkerCommandTests = test_worker.WorkerCommandTests("runTest")

    @override
    def setUp(self) -> None:
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        world = self.case.fixture.world
        world.locations["w1:p0"]["agent_session"] = {"value": self.case.fixture.parent.current_session}

    def test_start_row_keeps_an_idle_worker_closed(self) -> None:
        opened = self.case.opened()
        world = self.case.fixture.world
        _ = world.stop(opened.row, world)
        before = len(world.launches)
        with (patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(restart, "_saved_launch", side_effect=AssertionError("workers require worker restore")) as saved,
              patch.object(restart, "session_launch", wraps=restart.session_launch) as generic,
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(restart.start_row(opened.row, staff.snapshot(), world), "")
        self.assertIn(f"{opened.row.name}: closed; its next assign resumes it\n", output.getvalue())
        self.assertEqual(len(world.launches), before)
        self.assertNotIn(opened.pane, world.foreground)
        saved.assert_not_called()
        generic.assert_not_called()

    def test_start_row_keeps_a_closed_selected_worker_with_an_unreadable_sibling_closed(self) -> None:
        selected, sibling = self.case.opened(), self.case.opened("review", "review")
        world = self.case.fixture.world
        _ = world.stop(selected.row, world)
        self.assertIsInstance(worker_panes.close(self.case.fixture.parent.id,
            staff.WorkerName.parse(selected.row.name)), worker_panes.Closed)
        world.unknown[sibling.pane] = "sibling process is unreadable"
        before = len(world.launches)
        with (patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.start_row(selected.row, staff.snapshot(), world,
                worker_scope=restart.WorkerRestartScope.SELECTED_ROWS), "")
        self.assertIn(f"{selected.row.name}: closed; its next assign resumes it\n", output.getvalue())
        self.assertEqual(errors.getvalue(), f"herdr agents:{sibling.pane} tab w1:t1: pane evidence is unknown: "
                         + "sibling process is unreadable; row placement retained\n")
        self.assertEqual(len(world.launches), before)
        self.assertNotIn(selected.pane, world.locations)
        self.assertIn(sibling.pane, world.foreground)

    def test_start_row_resumes_one_worker_without_starting_its_owner(self) -> None:
        opened = self.case.opened("review", "review")
        initial = self.case.fixture.world.requests[-1][1]
        _ = progress_history.begin_dispatch(self.case.run_dir, slot="review", phase="8", role="review",
            worker_name=opened.row.name, worker_id=opened.row.id, launcher_command=["assign.sh"],
            prompt=self.case.prompt, kind="review", lens="")
        world = self.case.fixture.world
        _ = world.stop(opened.row, world)
        before = len(world.launches)
        with (patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(worker, "restore", wraps=worker.restore) as restore,
              patch.object(restart, "_saved_launch", side_effect=AssertionError("workers require worker restore")) as saved,
              patch.object(restart, "session_launch", wraps=restart.session_launch) as generic,
              redirect_stdout(io.StringIO())):
            self.assertEqual(restart.start_row(opened.row, staff.snapshot(), world), opened.pane)
        restore.assert_called_once()
        self.assertEqual(restore.call_args.args, (self.case.fixture.parent.id,))
        self.assertEqual(restore.call_args.kwargs["only"], frozenset({opened.row.id}))
        saved.assert_not_called()
        generic.assert_not_called()
        self.assertEqual(len(world.launches), before + 1)
        self.assertIn("w1:p0", world.foreground)
        self.assertIn(opened.pane, world.foreground)
        kind, arguments, environment = world.requests[-1]
        self.assertEqual(kind, "codex")
        self.assertEqual(arguments[arguments.index("--model") + 1], "gpt-test")
        self.assertEqual(arguments[arguments.index("--sandbox") + 1], "read-only")
        self.assertEqual(arguments[arguments.index("--ask-for-approval") + 1], "never")
        self.assertTrue(any(argument.startswith('model_reasoning_effort="') for argument in arguments))
        self.assertEqual(arguments[arguments.index("--model"):], initial[initial.index("--model"):])
        self.assertEqual(environment["UNIT_TEAM_ROLE"], "review")
        self.assertIn(opened.row.current_session, arguments)
        with (patch.object(herdr, "read_conversation", return_value=herdr.LiveConversation("codex", ())),
              patch.object(worker, "restore", wraps=worker.restore) as restore,
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(restart.start_row(opened.row, staff.snapshot(), world), "")
        restore.assert_not_called()
        self.assertEqual(output.getvalue(), f"{opened.row.name}: unchanged\n")

    def test_start_row_resumes_the_selected_worker_on_the_requested_account_without_its_sibling(self) -> None:
        selected, sibling = self.case.opened(), self.case.opened("review", "review")
        world = self.case.fixture.world
        for opened in (selected, sibling):
            _ = progress_history.begin_dispatch(self.case.run_dir, slot=opened.row.name, phase="8", role="impl",
                worker_name=opened.row.name, worker_id=opened.row.id, launcher_command=["assign.sh"],
                prompt=self.case.prompt, kind="impl", lens="")
            _ = world.stop(opened.row, world)
        destination = self.case.fixture.root / "codex2"
        before = len(world.launches)
        with (patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(agent_accounts, "codex_account_home", return_value=agent_accounts.CodexHomeHoldingLogin(
                  "codex2", destination, "fixture-login")),
              redirect_stdout(io.StringIO())):
            self.assertEqual(restart.start_row(selected.row, staff.snapshot(), world,
                target_account=accounts.CodexWorkersAccount("codex2", destination),
                worker_scope=restart.WorkerRestartScope.SELECTED_ROWS), selected.pane)
        self.assertEqual(len(world.launches), before + 1)
        self.assertEqual(world.requests[-1][2]["CODEX_HOME"], str(destination))
        self.assertIn(selected.pane, world.foreground)
        self.assertNotIn(sibling.pane, world.foreground)
        self.assertIsInstance(progress_history.active_dispatch(sibling.row.id, self.case.run_dir),
                              progress_history.ActiveDispatch)

    def test_start_row_keeps_the_selected_unnumbered_codex_home(self) -> None:
        opened = self.case.opened()
        _ = progress_history.begin_dispatch(self.case.run_dir, slot=opened.row.name, phase="8", role="impl",
            worker_name=opened.row.name, worker_id=opened.row.id, launcher_command=["assign.sh"],
            prompt=self.case.prompt, kind="impl", lens="")
        world = self.case.fixture.world
        _ = world.stop(opened.row, world)
        destination = self.case.fixture.root / ".codex"
        selected = accounts.CodexWorkersAccount("codex", destination)
        with (patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(agent_accounts, "codex_account_home", wraps=agent_accounts.codex_account_home) as resolve,
              patch.object(worker, "restore", wraps=worker.restore) as restore,
              redirect_stdout(io.StringIO())):
            self.assertEqual(restart.start_row(opened.row, staff.snapshot(), world,
                target_account=selected, worker_scope=restart.WorkerRestartScope.SELECTED_ROWS), opened.pane)
        self.assertIs(cast(object, restore.call_args.kwargs["account"]), selected)
        resolve.assert_not_called()
        self.assertEqual(world.requests[-1][2]["CODEX_HOME"], str(destination))
        self.assertIn(opened.row.current_session, world.requests[-1][1])


class RestartTests(RestartTestCase):
    root: Path = Path()
    rows: tuple[staff.StaffRow, ...] = ()
    running: set[str] = set()
    launches: list[tuple[str, herdr.NewTab | herdr.SplitPane | herdr.ExistingPane]] = []
    stops: list[str] = []
    workspace_label: str | None = "live"

    @override
    def setUp(self) -> None:
        super().setUp()
        configuration = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for source in (Path(__file__).resolve().parents[2] / "config").glob("*.conf"):
            write_text_atomic(configuration / source.name, source.read_text())
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(configuration)})))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = self.root / ".claude2"
        config.mkdir()
        _ = (config / ".claude.json").write_text(json.dumps({
            "projects": {str(self.root): {"hasTrustDialogAccepted": True}},
        }))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": str(self.root),
                                                     "XDG_STATE_HOME": str(self.root), "HOME": str(self.root),
                                                     "CODEX_HOME": str(self.root / ".codex"),
                                                     "CLAUDE_CONFIG_DIR": str(config)})))
        _ = self.enterContext(patch.object(herdr.Herdr, "run_in_pane"))
        self.rows = tuple(staff.StaffRow(staff.StaffId(f"row-{n}"), "unit", f"unit-{n}", staff.StaffRoot(),
            current_session=staff.StaffId(f"conversation-{n}"),
            worktree=staff.PresentWorktree(remote.local_machine(), self.root)) for n in range(3))
        self.running = {str(row.current_session) for row in self.rows}
        self.launches = []
        self.stops = []
        record = staff.StaffSnapshot(self.rows)
        _ = self.enterContext(patch.object(staff, "initial_snapshot", return_value=record))
        _ = self.enterContext(patch.object(sessions, "_readable_records", side_effect=self.records))
        _ = self.enterContext(patch.object(sessions, "session_liveness", side_effect=self.liveness))
        _ = self.enterContext(patch.object(stop_resume, "stop", side_effect=self.stop))
        _ = self.enterContext(patch.object(stop_resume, "stop_unit_workers", return_value=[], create=True))
        _ = self.enterContext(patch.object(restart, "session_launch", side_effect=self.session_launch))
        _ = self.enterContext(patch.object(accounts, "accounts_home", return_value=self.root))
        _ = self.enterContext(patch.object(accounts, "trust_sources", return_value=[]))
        _ = self.enterContext(patch.object(herdr, "prepare_session_environment",
            return_value=herdr.PreparedAgentEnvironment(self.root, {})))
        _ = self.enterContext(patch.object(herdr, "launch_prepared_agent", side_effect=self.launch))
        _ = self.enterContext(patch.object(herdr, "restore_server"))
        _ = self.enterContext(patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({}, ())))
        _ = self.enterContext(patch.object(herdr.Herdr, "sort_tabs"))
        _ = self.enterContext(patch.object(herdr.Herdr, "save_layout"))
        _ = self.enterContext(patch.object(herdr.Herdr, "remember_row_tab"))
        _ = self.enterContext(patch.object(herdr.Herdr, "tab_sessions", return_value=[]))
        _ = self.enterContext(patch.object(herdr.Herdr, "result", side_effect=self.layout_result))
        _ = self.enterContext(patch.object(herdr.Herdr, "panes", side_effect=self.panes))
        _ = self.enterContext(patch.object(herdr.Herdr, "native_session", side_effect=self.native))
        def native_process(pid: int, _kind: str) -> herdr.NativeAgentProcess:
            return herdr.NativeAgentProcess(herdr.EnvironmentThread(
                str(self.rows[pid - 990100].current_session)), "fixture-start", {})
        _ = self.enterContext(patch.object(herdr, "native_process", side_effect=native_process))
        _ = self.enterContext(redirect_stderr(io.StringIO()))
        layout: herdr.SavedLayout = {"at": 1, "workspaces": [{"name": "saved", "seen": 1,
            "tabs": [{"name": row.name, "seen": 1, "row_id": str(row.id)} for row in reversed(self.rows)]}]}
        _ = self.enterContext(patch.object(herdr, "saved_layout", return_value=layout))

    @staticmethod
    def records_for(identities: set[str]) -> list[sessions.SessionRecord]:
        records = list(registry_confirmation(frozenset(identities)).records)
        for record in records:
            record.update(pid=os.getpid(), procStart=sessions.process_start(os.getpid()) or "")
        return records

    def records(self) -> list[sessions.SessionRecord]:
        return self.records_for(self.running)

    @staticmethod
    def session_launch(identity: str, name: str) -> tuple[Literal["claude"], dict[str, str], list[str]]:
        return "claude", {}, ["--resume", identity, "--name", name]

    def layout_result(self, *arguments: str) -> dict[str, object]:
        if arguments == ("workspace", "list"):
            return {"workspaces": [{"workspace_id": "saved", "label": "saved"}, {"workspace_id": "w1", "label": self.workspace_label}]}
        if arguments == ("tab", "list"):
            return {"tabs": [{"tab_id": f"w1:t{n}", "workspace_id": "w1", "label": row.name}
                             for n, row in enumerate(self.rows)]}
        if arguments == ("pane", "list"):
            return {"panes": [{**pane, "tab_id": f"w1:t{pane['pane_id'].rsplit('p', 1)[1]}"} for pane in self.panes()]}
        if arguments[:3] == ("pane", "process-info", "--pane"):
            index = int(arguments[3].rsplit("p", 1)[1])
            return {"process_info": {"shell_pid": 990090, "foreground_process_group_id": 990100 + index,
                "foreground_processes": [{"pid": 990100 + index, "argv": ["claude"]}]}}
        raise AssertionError(arguments)

    def liveness(self, identity: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
        return sessions.LiveSessionProcess(123) if identity in self.running else sessions.SessionNotLive()

    def panes(self) -> list[herdr.PaneIdentity]:
        return [{"pane_id": f"w1:p{n}", "workspace_id": "w1", "session_id": row.current_session,
                 "terminal_id": f"terminal-{n}"} for n, row in enumerate(self.rows)
                if row.current_session in self.running]

    def native(self, pane: str, _kind: str, _timeout: float) -> str:
        return str(self.rows[int(pane.rsplit("p", 1)[1])].current_session)

    def stop(self, identity: str, *, deadline: float = math.inf,
             codex: herdr.CodexConversations | None = None,
             runs: dict[str, list[str]] | None = None) -> stop_resume.Stopped:
        _ = deadline, codex, runs
        row = next(row for row in self.rows if row.current_session == identity)
        self.stops.append(str(row.id))
        self.running.discard(str(row.current_session))
        return stop_resume.Stopped(str(row.current_session), 123)

    def launch(self, _name: str, destination: herdr.NewTab | herdr.SplitPane | herdr.ExistingPane,
               _prepared: herdr.PreparedAgentEnvironment, _kind: str, arguments: list[str],
               _opening: herdr.NoOpeningPrompt, _timeout: int, *, agent: str = "", reported_unknown: str = "",
               codex: herdr.CodexConversations | herdr.CodexLaunchRead = herdr.CodexLaunchRead.CURRENT) -> herdr.AgentLaunched:
        _ = reported_unknown, codex
        row = next(row for row in self.rows if row.name == agent)
        self.assertEqual(arguments, ["--resume", str(row.current_session), "--name", row.name])
        self.assertIsInstance(_opening, herdr.NoOpeningPrompt)
        self.assertEqual(_prepared.directory, self.root)
        self.launches.append((agent, destination))
        self.running.add(str(row.current_session))
        return herdr.AgentLaunched(f"w1:p{self.rows.index(row)}", herdr.PromptSubmissionNotRequired())

    def test_unconfirmed_stop_of_live_codex_does_not_launch(self) -> None:
        self.running.clear()
        row = self.rows[0]
        with (patch.object(restart, "live_rows", return_value=[row]),
              patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations(
                  {str(row.current_session): (990100,)}, ())) as scans,
              patch.object(restart, "stop_row", return_value=False),
              patch.object(restart, "session_launch", return_value=("codex", {}, ["resume", str(row.current_session)])),
              patch.object(herdr, "native_process", return_value=herdr.NativeAgentProcess(
                  herdr.PaneSessionReport(str(row.current_session)), "fresh", {})),
              patch.object(sessions, "process_liveness", return_value=sessions.LiveSessionProcess(990100)),
              patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                  "w1:p0", herdr.PromptSubmissionNotRequired())) as launch,
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(restart.restart(stop_all=True), 0)
        launch.assert_not_called()
        self.assertEqual(scans.call_count, 1)
        self.assertIn(f"{row.name}: unchanged", output.getvalue())

    def test_unconfirmed_stop_of_exited_codex_launches(self) -> None:
        self.running.clear()
        row = self.rows[0]
        with (patch.object(restart, "live_rows", return_value=[row]),
              patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations(
                  {str(row.current_session): (990100,)}, ())) as scans,
              patch.object(restart, "stop_row", return_value=False),
              patch.object(restart, "session_launch", return_value=("codex", {}, ["resume", str(row.current_session)])),
              patch.object(herdr, "native_process", side_effect=ProcessLookupError("target exited")),
              patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                  "w1:p0", herdr.PromptSubmissionNotRequired())) as launch,
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(restart.restart(stop_all=True), 0)
        launch.assert_called_once()
        self.assertEqual(scans.call_count, 1)
        self.assertIn(f"{row.name}: restarted", output.getvalue())

    def test_unclear_identity_after_stop_keeps_the_running_target_pending(self) -> None:
        row = self.rows[0]
        for reason in ("rollout is missing", "generation changed"):
            with self.subTest(reason=reason):
                self.running.clear()
                shutdown_record.save([])
                with (patch.object(restart, "live_rows", return_value=[row]),
                      patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations(
                          {str(row.current_session): (990100,)}, ())),
                      patch.object(restart, "stop_row", return_value=False),
                      patch.object(restart, "session_launch", return_value=("codex", {}, ["resume", str(row.current_session)])),
                      patch.object(herdr, "native_process", side_effect=FileNotFoundError(reason) if reason.startswith("rollout") else None,
                          return_value=herdr.NativeAgentProcess(herdr.PaneSessionReport(str(row.current_session)), "fresh", {})),
                      patch.object(sessions, "process_liveness", return_value=sessions.SessionNotLive()),
                      patch.object(os, "kill"),
                      patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                          "w1:p0", herdr.PromptSubmissionNotRequired())) as launch,
                      redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors):
                    self.assertEqual(restart.restart(stop_all=True), 0)
                launch.assert_not_called()
                self.assertEqual(shutdown_record.read(), [str(row.id)])
                self.assertEqual(len(errors.getvalue().splitlines()), 1)
                self.assertTrue(errors.getvalue().startswith(f"{row.name}: restart anomaly:"))
                self.assertIn(reason, errors.getvalue())

    def test_restart_retains_dead_row_after_unconfirmed_stop_and_failed_launch(self) -> None:
        self.running.clear()
        row = self.rows[0]
        with (patch.object(restart, "live_rows", return_value=[row]),
              patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations(
                  {str(row.current_session): (990100,)}, ())),
              patch.object(restart, "stop_row", return_value=False),
              patch.object(restart, "session_launch", return_value=("codex", {}, ["resume", str(row.current_session)])),
              patch.object(herdr, "launch_prepared_agent", side_effect=OSError("launch failed")) as launch,
              redirect_stdout(io.StringIO())):
            self.assertEqual(restart.restart(stop_all=True), 0)
        self.assertEqual(shutdown_record.read(), [str(row.id)])
        launch.assert_called_once()

    def test_worker_restore_output_names_its_owner(self) -> None:
        from scripts.production import worker

        owner = self.rows[0]
        child = replace(self.rows[1], kind="worker", parent=staff.ReportsTo(owner.id))
        record = staff.StaffSnapshot((owner, child))
        with (patch.object(staff, "snapshot", return_value=record),
              patch.object(restart, "pane_workers", return_value=True),
              patch.object(worker, "_read_workers", return_value=worker.MeshWorkerRows([])),
              redirect_stdout(io.StringIO()) as output):
            _ = restart._restore_workers(owner, record)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(output.getvalue().splitlines(), [f"{owner.name}: workers unchanged"])

    def account_processes(self) -> None:
        def observe(identity: str, _kind: str = "", *,
                    codex: herdr.CodexConversations | None = None) -> herdr.LiveConversation:
            _ = codex
            index = next(index for index, row in enumerate(self.rows) if row.current_session == identity)
            return herdr.LiveConversation("claude", (990100 + index,))

        _ = self.enterContext(patch.object(herdr, "read_conversation", side_effect=observe))
        def environment(pid: int) -> dict[str, str]:
            return {"CLAUDE_CONFIG_DIR": str(self.root / (".claude3" if pid == 990100 else ".claude2"))}

        _ = self.enterContext(patch.object(herdr, "process_environment", side_effect=environment))
        _ = self.enterContext(patch.object(shutdown, "named_claude_account",
            return_value=Account("claude", "test@example.invalid", "claude3")))

    def test_restart_account_all_dry_run_lists_only_matching_live_rows(self) -> None:
        self.account_processes()
        shutdown_record.update(started=[], pending=[str(self.rows[2].id)])
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(shutdown.main(["restart", "claude3", "--all", "--dry-run"]), 0)
        self.assertIn(self.rows[0].name + ":", output.getvalue())
        self.assertNotIn(self.rows[1].name + ":", output.getvalue())
        self.assertNotIn(self.rows[2].name + ":", output.getvalue())
        self.assertEqual(self.stops, [])

    def test_restart_account_all_stops_and_starts_only_matching_rows_on_that_account(self) -> None:
        self.account_processes()
        with (patch.object(restart, "stop_row") as stop,
              patch.object(restart, "start_row", return_value="w1:p0") as start,
              patch.object(staff, "row_operation"),
              redirect_stdout(io.StringIO())):
            self.assertEqual(shutdown.main(["restart", "claude3", "--all"]), 0)
        self.assertEqual([item.args[0] for item in stop.call_args_list], [self.rows[0]])
        self.assertEqual([item.args[0] for item in start.call_args_list], [self.rows[0]])
        self.assertEqual(start.call_args.kwargs["target_account"],
                         accounts.NamedAccount("claude3", self.root / ".claude3"))

    def test_restart_codex_account_filters_live_process_homes(self) -> None:
        def observe(identity: str, _kind: str = "", *,
                    codex: herdr.CodexConversations | None = None) -> herdr.LiveConversation:
            _ = codex
            index = next(index for index, row in enumerate(self.rows) if row.current_session == identity)
            return herdr.LiveConversation("codex", (990100 + index,))

        def environment(pid: int) -> dict[str, str]:
            return {"CODEX_HOME": str(self.root / (".codex1" if pid == 990100 else ".codex2"))}

        with (patch.object(herdr, "read_conversation", side_effect=observe),
              patch.object(herdr, "process_environment", side_effect=environment),
              patch.object(agent_accounts, "codex_account_home",
                  return_value=agent_accounts.CodexSetupHome("codex1", self.root / ".codex1")),
              patch.object(note_file, "read_note",
                  return_value=note_file.Note(self.root / "codex1.md", "codex", "", 0, 0, "codex1")),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(shutdown.main(["restart", "codex1", "--all", "--dry-run"]), 0)
        self.assertIn(self.rows[0].name + ":", output.getvalue())
        self.assertNotIn(self.rows[1].name + ":", output.getvalue())
        self.assertNotIn(self.rows[2].name + ":", output.getvalue())

    def test_restart_account_without_all_filters_saved_session_accounts(self) -> None:
        self.account_processes()
        self.running.clear()
        shutdown_record.update(started=[], pending=[str(row.id) for row in self.rows])

        def saved(identity: str, name: str) -> tuple[Literal["claude"], dict[str, str], list[str]]:
            return "claude", {"CLAUDE_CONFIG_DIR": str(self.root / (
                ".claude3" if identity == self.rows[0].current_session else ".claude2"))}, [
                    "--resume", identity, "--name", name]

        with (patch.object(restart, "session_launch", side_effect=saved),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(shutdown.main(["restart", "claude3", "--dry-run"]), 0)
        self.assertIn(self.rows[0].name + ":", output.getvalue())
        self.assertNotIn(self.rows[1].name + ":", output.getvalue())
        self.assertNotIn(self.rows[2].name + ":", output.getvalue())
        self.assertEqual(shutdown_record.read(), [str(row.id) for row in self.rows])

    def test_restart_all_only_dry_run_lists_only_the_requested_live_row(self) -> None:
        self.account_processes()
        for account, requested in (([], self.rows[0]), (["claude3"], self.rows[1])):
            with self.subTest(account=account), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(shutdown.main(["restart", *account, "--all", "--only",
                    str(requested.id), "--dry-run"]), 0)
                self.assertEqual([line.split(":", 1)[0] for line in output.getvalue().splitlines()],
                                 [self.rows[0].name] if not account else [])

    def test_restart_all_only_stops_and_starts_only_the_requested_live_row(self) -> None:
        with (patch.object(restart, "live_rows", return_value=list(self.rows)),
              patch.object(restart, "stop_row") as stop,
              patch.object(restart, "start_row", return_value="w1:p0") as start,
              patch.object(staff, "row_operation")):
            self.assertEqual(restart.restart(stop_all=True, only=(str(self.rows[0].id),)), 0)
        self.assertEqual([item.args[0] for item in stop.call_args_list], [self.rows[0]])
        self.assertEqual([item.args[0] for item in start.call_args_list], [self.rows[0]])

    def test_restart_selected_caller_detaches_before_any_stop(self) -> None:
        for variable in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
            with (self.subTest(variable=variable),
                  patch.dict(os.environ, {variable: str(self.rows[0].current_session)}),
                  patch.object(restart, "live_rows", return_value=list(self.rows)),
                  patch.object(stop_resume, "detach", create=True, return_value=self.root / "restart.log") as detach,
                  patch.object(restart, "stop_row") as stop,
                  patch.object(restart, "start_row") as start,
                  patch.object(staff, "row_operation"),
                  redirect_stdout(io.StringIO()) as output):
                self.assertEqual(restart.restart(stop_all=True, only=(str(self.rows[0].id),)), 0)
                detach.assert_called_once()
                stop.assert_not_called()
                start.assert_not_called()
                self.assertIn(str(self.root / "restart.log"), output.getvalue())
                self.assertEqual(len(output.getvalue().splitlines()), 1)

    def test_restart_only_keeps_worker_and_owner_operations_within_selected_rows(self) -> None:
        owner = self.rows[0]
        children = tuple(replace(row, kind="worker", parent=staff.ReportsTo(owner.id)) for row in self.rows[1:])
        record = staff.StaffSnapshot((owner, *children))
        for selected in (owner, children[0]):
            with (self.subTest(selected=selected.id),
                  patch.object(staff, "initial_snapshot", return_value=record),
                  patch.object(restart, "live_rows", return_value=list(record.rows)),
                  patch.object(unit_runs, "worker_transport", return_value="herdr"),
                  patch.object(restart.stop_resume, "stop_unit_workers") as stop_workers,
                  patch.object(restart, "stop_row") as stop,
                  patch.object(restart, "start_row", return_value="w1:p1") as start,
                  patch.object(staff, "row_operation")):
                self.assertEqual(restart.restart(stop_all=True, only=(str(selected.id),)), 0)
                self.assertEqual([item.args[0] for item in stop.call_args_list], [selected])
                self.assertEqual([item.args[0] for item in start.call_args_list], [selected])
                stop_workers.assert_not_called()

    def test_account_filter_selects_a_hosted_codex_thread_by_its_daemon_home(self) -> None:
        row = self.rows[0]
        home = self.root / ".codex1"
        observed = codex_daemon.DaemonRead(codex_daemon.CodexDaemon(home, home / "control.sock"), [
            codex_daemon.DaemonSession(str(row.current_session), row.name, str(self.root), "idle")])
        with (patch.object(herdr, "read_conversation", return_value=herdr.LiveConversation("codex", ())),
              patch.object(codex_daemon, "read_daemons", return_value=[observed])):
            self.assertEqual(restart.live_members([row], staff.StaffSnapshot(self.rows),
                codex=herdr.CodexConversations({str(row.current_session): ()}, ()),
                account=accounts.CodexWorkersAccount("codex1", home)), [row])

    def test_restart_rejects_a_codex_label_without_an_account_note(self) -> None:
        with (patch.object(note_file, "read_note", return_value=note_file.NotAccountNote(self.root / "missing.md")),
              patch.object(agent_accounts, "codex_account_home",
                  return_value=agent_accounts.CodexSetupHome("codex987654321", self.root / ".codex987654321")),
              patch.object(restart, "restart") as run,
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(shutdown.main(["restart", "codex987654321", "--all"]), 2)
        run.assert_not_called()
        self.assertEqual(len(errors.getvalue().splitlines()), 1)

    def test_account_filters_partition_an_owner_and_its_workers_by_their_own_accounts(self) -> None:

        self.account_processes()
        owner = self.rows[0]
        children = tuple(replace(row, kind="worker", parent=staff.ReportsTo(owner.id)) for row in self.rows[1:])
        record = staff.StaffSnapshot((owner, *children))
        states = [worker_panes.OpenWorker(row, f"w1:p{index}", f"terminal-{index}", herdr.AgentStatus.IDLE)
                  for index, row in enumerate(children, 1)]
        with (patch.object(unit_runs, "worker_transport", return_value="herdr"),
              patch.object(worker_panes, "observe_parents", return_value={owner.id: states})):
            for label, expected in (("claude3", [owner]), ("claude2", list(children))):
                with self.subTest(account=label):
                    self.assertEqual(restart.live_rows(record, herdr.Herdr("agents"),
                        account=accounts.NamedAccount(label, self.root / ("." + label))), expected)

    def test_restart_names_unclassified_claude_and_codex_workers_once_without_acting(self) -> None:
        row = replace(self.rows[0], kind="worker")
        for kind in ("claude", "codex"):
            account = (accounts.NamedAccount("claude3", self.root / ".claude3") if kind == "claude" else
                       accounts.CodexWorkersAccount("codex1", self.root / ".codex1"))
            for dry_run in (False, True):
                with (self.subTest(kind=kind, dry_run=dry_run),
                      patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
                      patch.object(sessions, "running_sessions", return_value=[]),
                      patch.object(herdr, "read_conversation",
                          return_value=herdr.LiveConversation(kind, ())),
                      patch.object(restart, "stop_row") as stop,
                      patch.object(restart, "start_row") as start,
                      redirect_stdout(io.StringIO()) as output):
                    self.assertEqual(restart.restart(stop_all=True, dry_run=dry_run, account=account), 0)
                    self.assertEqual(output.getvalue().splitlines(),
                                     [f"{row.name}: left alone; account unknown"])
                    stop.assert_not_called()
                    start.assert_not_called()

    def test_restart_unknown_account_returns_two_before_any_stop(self) -> None:
        with (patch.object(shutdown, "named_claude_account",
                  side_effect=UnknownAccountName("unknown account: missing")),
              patch.object(restart, "stop_row") as stop,
              patch.object(restart, "start_row") as start,
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(shutdown.main(["restart", "missing", "--all"]), 2)
        stop.assert_not_called()
        start.assert_not_called()
        self.assertEqual(len(errors.getvalue().splitlines()), 1)

    def test_restart_uses_each_reader_outcome_for_both_runtimes(self) -> None:
        self.running.clear()
        row = self.rows[0]
        for kind in ("claude", "codex"):
            for state in ("live", "stopped", "unknown"):
                with self.subTest(kind=kind, state=state):
                    observed = (herdr.LiveConversation(kind, (123,)) if state == "live" else
                                herdr.UnknownConversation("test-tab (conversation unknown)") if state == "unknown" else
                                herdr.StoppedConversation())
                    with (patch.object(herdr, "read_conversation", return_value=observed),
                          patch.object(restart, "session_launch", return_value=(kind, {},
                              ["--resume", str(row.current_session)] if kind == "claude" else ["resume", str(row.current_session)])),
                          patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                              "w1:p0", herdr.PromptSubmissionNotRequired())) as launch,
                          redirect_stderr(io.StringIO()) as errors,
                          redirect_stdout(io.StringIO()) as output):
                        pane = restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents"))
                    self.assertEqual(pane, "" if state == "live" else "w1:p0")
                    self.assertEqual(launch.call_count, 0 if state == "live" else 1)
                    self.assertIn("unchanged" if state == "live" else "restarted", output.getvalue())
                    self.assertEqual(errors.getvalue().count("anomaly: test-tab"), 1 if state == "unknown" else 0)

    def test_restart_reports_unknown_once_across_the_real_launch_boundary(self) -> None:
        self.running.clear()
        row = self.rows[0]
        for kind in ("claude", "codex"):
            with (self.subTest(kind=kind),
                  patch.object(herdr, "read_conversation", return_value=herdr.UnknownConversation(
                      "test-tab (conversation unreadable)")) as read,
                  patch.object(restart, "session_launch", return_value=(kind, {},
                      ["--resume", str(row.current_session)] if kind == "claude" else ["resume", str(row.current_session)])),
                  patch.object(herdr, "launch_prepared_agent", LAUNCH_PREPARED_AGENT),
                  patch.object(herdr, "open_tab", return_value="w1:p9"),
                  patch.object(herdr, "start_agent", return_value=herdr.PromptSubmissionNotRequired()) as start,
                  redirect_stderr(io.StringIO()) as errors, redirect_stdout(io.StringIO())):
                self.assertEqual(restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents")), "w1:p9")
            self.assertEqual(read.call_count, 3)
            start.assert_called_once()
            self.assertEqual(errors.getvalue().splitlines(), [
                "anomaly: test-tab (conversation unreadable); not blocking"])

    def test_restart_retains_unproved_panes_and_starts_independent_work(self) -> None:
        self.running.clear()
        row = self.rows[0]
        pane: herdr.PaneIdentity = {"pane_id": "w1:p0", "workspace_id": "w1", "session_id": row.current_session,
                                     "terminal_id": "kept-terminal"}
        for fault in ("duplicate-panes", "unreadable-panes"):
            with self.subTest(fault=fault):
                selected = {**pane}
                with (patch.object(herdr.Herdr, "panes", return_value=[selected, selected] if fault == "duplicate-panes" else [selected],
                          side_effect=herdr.HerdrFailure("pane list unavailable") if fault == "unreadable-panes" else None),
                      patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                          "w1:p9", herdr.PromptSubmissionNotRequired())) as launch,
                      redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors):
                    self.assertEqual(restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents")), "w1:p9")
                self.assertIsInstance(cast(object, launch.call_args.args[1]), herdr.NewTab)
                self.assertEqual(len(errors.getvalue().splitlines()), 1)

    def test_start_row_ignores_a_managed_daemon_at_both_live_checks(self) -> None:
        self.running.clear()
        row = self.rows[0]
        with (patch.object(restart, "session_launch", return_value=("codex", {}, ["resume", row.current_session])),
              patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({}, ())) as scan,
              patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                  "w1:p0", herdr.PromptSubmissionNotRequired())) as launch,
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents")), "w1:p0")
        self.assertEqual(scan.call_count, 2)
        launch.assert_called_once()
        self.assertEqual(output.getvalue(), f"{row.name}: restarted\n")

    def test_live_members_reads_only_selected_local_rows(self) -> None:
        remote_row = replace(self.rows[0], id=staff.StaffId("remote-row"),
            current_session=staff.StaffId("remote-conversation"),
            worktree=staff.PresentWorktree("mac", self.root))
        record = staff.StaffSnapshot((*self.rows, remote_row))
        resolver = session_staff.RowResolver(record)
        snapshot = herdr.CodexConversations({}, ())
        with (patch.object(herdr, "read_conversation", side_effect=[
                  herdr.LiveConversation("claude", (123,)), herdr.StoppedConversation()]) as read,
              patch.object(sessions, "running_sessions") as registry,
              patch.object(herdr, "running_codex_threads") as codex,
              redirect_stderr(io.StringIO()) as errors):
            selected = restart.live_members(iter((*self.rows[:2], remote_row)), record, resolver=resolver, codex=snapshot)
        self.assertEqual(selected, [self.rows[0]])
        self.assertEqual(read.call_args_list, [
            call(str(self.rows[0].current_session), codex=snapshot), call(str(self.rows[1].current_session), codex=snapshot)])
        registry.assert_not_called()
        codex.assert_not_called()
        self.assertEqual(errors.getvalue(), "")

    def test_live_members_reports_unknown_and_continues_after_row_errors(self) -> None:
        for error in (OSError("read failed"), ValueError("invalid conversation")):
            with (self.subTest(error=error),
                  patch.object(herdr, "read_conversation", side_effect=[
                      herdr.UnknownConversation("test-tab (conversation unknown)"), error,
                      herdr.LiveConversation("codex", (123,))]),
                  redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(restart.live_members(self.rows, staff.StaffSnapshot(self.rows),
                    codex=herdr.CodexConversations({}, ())), [self.rows[2]])
            self.assertEqual(errors.getvalue().splitlines(), [
                "anomaly: test-tab (conversation unknown); not blocking",
                f"{self.rows[1].name}: restart anomaly: {error}"])

    def test_codex_clients_stay_live_and_only_daemon_rows_are_omitted(self) -> None:
        self.running.clear()
        row = self.rows[0]
        for arguments, live in ((["codex", "app-server", "--managed-daemon"], False),
                                (["codex", "resume", str(row.current_session)], True),
                                (["codex", "app-server", "--listen", "ws://127.0.0.1:1234"], True)):
            with (self.subTest(arguments=arguments),
                  patch.object(restart, "session_launch", return_value=("codex", {}, ["resume", row.current_session])),
                  patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({str(row.current_session): (123,)} if live else {}, ())),
                  patch.object(herdr, "launch_prepared_agent") as launch,
                  redirect_stdout(io.StringIO()) as output):
                self.assertEqual(restart.live_rows(staff.StaffSnapshot((row,)), herdr.Herdr("agents")), [row] if live else [])
                if live:
                    self.assertEqual(restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents")), "")
                    self.assertEqual(output.getvalue(), f"{row.name}: unchanged\n")
                launch.assert_not_called()

    def test_bulk_restart_starts_every_selected_row_after_sigterm_zombies(self) -> None:
        kinds: tuple[Literal["claude", "codex"], ...] = ("claude", "codex")
        for kind in kinds:
            with self.subTest(kind=kind):
                self.running = {str(row.current_session) for row in self.rows}
                self.launches.clear()
                elapsed = 0.0
                signalled: list[int] = []
                environment = {"HERDR_PROC": str(self.root / (kind + "-proc"))}
                for index in range(len(self.rows)):
                    fake_unit_host.process(environment, 990100 + index, 1, [kind], {}, cwd=self.root)

                def observe(identity: str, _kind: str = "", *,
                            runtime: Literal["claude", "codex"] = kind,
                            codex: herdr.CodexConversations | None = None) -> herdr.LiveConversation | herdr.StoppedConversation:
                    _ = codex
                    row = next(row for row in self.rows if row.current_session == identity)
                    return (herdr.LiveConversation(runtime, (990100 + self.rows.index(row),))
                            if identity in self.running else herdr.StoppedConversation())

                def process(pid: int, _started: str) -> sessions.LiveSessionProcess:
                    return sessions.LiveSessionProcess(pid)

                def native(pid: int, _kind: str) -> herdr.NativeAgentProcess:
                    identity = str(self.rows[pid - 990100].current_session)
                    evidence = herdr.PaneSessionReport(identity) if kind == "codex" else herdr.EnvironmentThread(identity)
                    return herdr.NativeAgentProcess(evidence, "fixture-start", {})

                def signal_process(pid: int, number: int) -> None:
                    self.assertEqual(number, signal.SIGTERM)
                    stat = Path(environment["HERDR_PROC"]) / str(pid) / "stat"
                    content = stat.read_text()
                    self.assertIn(") S ", content)
                    _ = stat.write_text(content.replace(") S ", ") Z "))
                    self.running.discard(str(self.rows[pid - 990100].current_session))
                    signalled.append(pid)

                def launch_arguments(identity: str, name: str) -> tuple[str, dict[str, str], list[str]]:
                    arguments = ["resume", identity] if kind == "codex" else ["--resume", identity, "--name", name]
                    return kind, {}, arguments

                def launched(_session: str, destination: herdr.NewTab | herdr.SplitPane | herdr.ExistingPane,
                             _prepared: herdr.PreparedAgentEnvironment, launch_kind: str, arguments: list[str],
                             _opening: herdr.NoOpeningPrompt, _timeout: int, *, agent: str = "",
                             reported_unknown: str = "", codex: herdr.CodexConversations | herdr.CodexLaunchRead = herdr.CodexLaunchRead.CURRENT) -> herdr.AgentLaunched:
                    _ = codex
                    self.assertEqual((launch_kind, reported_unknown), (kind, ""))
                    row = next(row for row in self.rows if row.name == agent)
                    self.assertIn(row.current_session, arguments)
                    self.launches.append((agent, destination))
                    self.running.add(str(row.current_session))
                    return herdr.AgentLaunched(f"w1:p{self.rows.index(row)}", herdr.PromptSubmissionNotRequired())

                def advance(seconds: float) -> None:
                    nonlocal elapsed
                    elapsed += seconds

                def now() -> float:
                    return elapsed

                class ClockEvent:
                    def wait(self, seconds: float) -> None:
                        advance(seconds)

                with (patch.dict(os.environ, environment),
                      patch.object(herdr, "_PLATFORM", "linux"),
                      patch.object(herdr, "read_conversation", side_effect=observe),
                      patch.object(herdr, "native_process", side_effect=native),
                      patch.object(restart, "session_launch", side_effect=launch_arguments),
                      patch.object(herdr, "launch_prepared_agent", side_effect=launched),
                      patch.object(stop_resume, "stop", STOP_SESSION),
                      patch.object(sessions, "process_liveness", side_effect=process),
                      patch.object(stop_resume, "_signal_process", side_effect=signal_process),
                      patch.object(time, "monotonic", side_effect=now),
                      patch.object(stop_resume, "monotonic", side_effect=now),
                      patch.object(stop_resume, "Event", return_value=ClockEvent()),
                      patch.object(pane_record, "seed", return_value=0),
                      patch.object(stop_resume, "_retire_prompt"),
                      redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors):
                    self.assertEqual(restart.restart(stop_all=True), 0, errors.getvalue())
                self.assertCountEqual(signalled, [990100, 990101, 990102])
                self.assertCountEqual([name for name, _ in self.launches], [row.name for row in self.rows])
                self.assertEqual(errors.getvalue(), "")

    def test_start_row_uses_the_target_account_environment(self) -> None:
        source, target = self.root / ".claude3", self.root / ".claude4"
        environment = {"CLAUDE_CONFIG_DIR": str(self.root / ".claude4"), "ACCOUNT_SETTING": "target"}
        for requested in ("claude4", ""):
            with (self.subTest(target_account=requested),
                  patch.object(accounts, "role_account", return_value=accounts.NamedAccount("claude4", target)) as selection,
                  patch.object(restart, "named_account_environment", return_value=environment) as account,
                  patch.object(restart, "session_launch", return_value=("claude", {"CLAUDE_CONFIG_DIR": str(source)},
                      ["--resume", str(self.rows[0].current_session), "--name", self.rows[0].name])),
                  patch.object(herdr, "prepare_session_environment",
                      return_value=herdr.PreparedAgentEnvironment(self.root, environment)) as prepare,
                  patch("scripts.accounts.accounts.account_dir", return_value=target)):
                self.running.clear()
                pane = restart.start_row(self.rows[0], staff.StaffSnapshot(self.rows),
                                         herdr.Herdr("agents"), target_account=requested)
            if requested:
                selection.assert_not_called()
            else:
                selection.assert_called_once_with("unit")
            account.assert_called_once_with(target)
            prepare.assert_called_once_with(str(self.rows[0].current_session), str(self.root),
                                            environment, herdr.NO_ENVIRONMENT_REMOVALS)
            self.assertEqual(pane, "w1:p0")

    def test_start_row_selects_the_workers_account_unless_the_target_is_explicit(self) -> None:
        self.running.clear()
        row = self.rows[0]
        for requested in ("", "codex3"):
            expected = requested or "codex1"
            with (self.subTest(target=requested),
                  patch.object(accounts, "role_account", return_value=accounts.CodexWorkersAccount(
                      "codex1", self.root / ".codex1")) as role,
                  patch.object(restart, "session_launch", return_value=("codex", {"CODEX_HOME": str(self.root / ".codex2")},
                      ["resume", str(row.current_session)])),
                  patch.object(herdr, "prepare_session_environment", return_value=herdr.PreparedAgentEnvironment(
                      self.root, {})) as prepare,
                  patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                      "w1:p0", herdr.PromptSubmissionNotRequired()))):
                _ = restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents"), target_account=requested)
            self.assertEqual(prepare.call_args.args[2]["CODEX_HOME"], str(self.root / f".{expected}"))
            if requested:
                role.assert_not_called()
            else:
                role.assert_called_once_with("workers")

    def test_start_row_with_a_saved_entry_uses_the_role_account(self) -> None:
        self.running.clear()
        row = self.rows[0]
        source, target = self.root / ".claude3", self.root / ".claude2"
        entry = pane_record.launch("w1:p0", str(row.id), str(self.root),
            {"CLAUDE_CONFIG_DIR": str(source)}, ["claude", "--resume", str(row.current_session)])
        with (patch.object(pane_record, "publication_for_session",
                  return_value=pane_launch.LaunchPublication(self.root / "agents.json", entry)),
              patch.object(herdr, "prepare_session_environment",
                  return_value=herdr.PreparedAgentEnvironment(self.root, {})) as prepare,
              patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                  "w1:p0", herdr.PromptSubmissionNotRequired()))):
            self.assertEqual(restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents")), "w1:p0")
        self.assertEqual(prepare.call_args.args[2]["CLAUDE_CONFIG_DIR"], str(target))

    def test_start_row_without_a_role_uses_units_unless_the_target_is_explicit(self) -> None:
        self.running.clear()
        row = replace(self.rows[0], kind="agent")
        source, target = self.root / ".claude3", self.root / ".claude2"
        entry = pane_record.launch("w1:p0", str(row.current_session), str(self.root),
            {"CLAUDE_CONFIG_DIR": str(source)}, ["claude", "--resume", str(row.current_session)])
        for requested, expected in (("", target), ("claude3", source)):
            with (self.subTest(target=requested),
                  patch.object(pane_record, "publication_for_session",
                      return_value=pane_launch.LaunchPublication(self.root / "agents.json", entry)),
                  patch.object(accounts, "role_account", return_value=accounts.NamedAccount("claude2", target)) as role,
                  patch.object(herdr, "prepare_session_environment",
                      return_value=herdr.PreparedAgentEnvironment(self.root, {})) as prepare,
                  patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                      "w1:p0", herdr.PromptSubmissionNotRequired()))):
                self.assertEqual(restart.start_row(row, staff.StaffSnapshot((row,)), herdr.Herdr("agents"),
                                                  target_account=requested), "w1:p0")
                self.assertEqual(prepare.call_args.args[2]["CLAUDE_CONFIG_DIR"], str(expected))
                if requested:
                    role.assert_not_called()
                else:
                    role.assert_called_once_with("unit")

    def test_account_trust_with_the_same_account_keeps_the_file(self) -> None:
        target = self.root / ".claude2"
        before = (target / ".claude.json").read_text()
        self.assertIsInstance(stop_resume.heal_account_trust(self.root, target, target, "test"), herdr.Trusted)
        self.assertEqual((target / ".claude.json").read_text(), before)

    def test_start_row_opens_and_trusts_the_codex_folder(self) -> None:
        self.running.clear()
        row = self.rows[0]
        with (patch.object(restart, "session_launch", return_value=("codex", {}, ["resume", row.current_session])),
              patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                  "w1:p0", herdr.PromptSubmissionNotRequired())) as launch):
            _ = restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents"))
        self.assertEqual(launch.call_args.args[4], ["-C", str(self.root), "-c",
            'projects={' + json.dumps(str(self.root)) + '={trust_level="trusted"}}', "resume", row.current_session])

    def test_account_trust_reports_anomalies_and_returns_the_result(self) -> None:
        target = self.root / ".claude4"
        for result in (herdr.Untrusted(self.root, target / ".claude.json", (self.root,)),
                       herdr.UnreadableTrustFile(self.root, target / ".claude.json", "ValueError")):
            with (patch.object(herdr, "heal_claude_trust", return_value=result),
                  patch("scripts.accounts.accounts.trust_sources", return_value=[]),
                  redirect_stderr(io.StringIO()) as output):
                self.assertIs(stop_resume.heal_account_trust(self.root, target, self.root, "test"), result)
            reason = str(result) if isinstance(result, herdr.UnreadableTrustFile) else f"Claude has not trusted {self.root}"
            self.assertEqual(output.getvalue(), f"test: trust anomaly: {reason}\n")

    def test_saved_codex_record_uses_workers_home_without_claude_trust(self) -> None:
        row = self.rows[0]
        home = self.root / ".codex2"
        workers = self.root / ".codex1"
        entry = pane_record.launch("w1:p0", str(row.current_session), str(self.root),
            {"CLAUDE_CONFIG_DIR": str(home)}, ["codex"], kind="codex")
        entry["config_dir"] = str(home)
        pane_record.write("agents", entry)
        with (patch.object(accounts, "role_account", return_value=accounts.CodexWorkersAccount("codex1", workers)) as role,
              patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(stop_resume, "heal_account_trust") as trust,
              patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                  "w1:p0", herdr.PromptSubmissionNotRequired())),
              patch.object(herdr, "prepare_session_environment", return_value=herdr.PreparedAgentEnvironment(
                  self.root, {"CODEX_HOME": str(workers)})) as prepare):
            _ = restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents"))
        trust.assert_not_called()
        role.assert_called_once_with("workers")
        self.assertEqual(prepare.call_args.args[2]["CODEX_HOME"], str(workers))

    def test_saved_codex_restart_uses_explicit_account(self) -> None:
        row = self.rows[0]
        home = self.root / ".codex2"
        target = self.root / ".codex3"
        entry = pane_record.launch("w1:p0", str(row.current_session), str(self.root),
            {"CLAUDE_CONFIG_DIR": str(home)}, ["codex"], kind="codex")
        entry["config_dir"] = str(home)
        pane_record.write("agents", entry)
        with (patch.object(accounts, "role_account") as role,
              patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(stop_resume, "heal_account_trust") as trust,
              patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                  "w1:p0", herdr.PromptSubmissionNotRequired())),
              patch.object(herdr, "prepare_session_environment", return_value=herdr.PreparedAgentEnvironment(
                  self.root, {"CODEX_HOME": str(target)})) as prepare):
            _ = restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents"), target_account="codex3")
        role.assert_not_called()
        trust.assert_not_called()
        self.assertEqual(prepare.call_args.args[2]["CODEX_HOME"], str(target))

    def test_dry_run_names_a_morty_agent_with_an_unproved_native_identity(self) -> None:
        row = replace(self.rows[0], kind="agent", name="morty", worktree=staff.WORKTREE_UNFILLED)
        self.rows = (row,)
        record = staff.StaffSnapshot(self.rows)
        entry = pane_record.launch("w1:p0", str(row.current_session), str(self.root),
            {"CODEX_HOME": str(self.root / ".codex2")}, ["codex"], kind="codex")
        pane_record.write("agents", entry)
        with (patch.object(staff, "initial_snapshot", return_value=record),
              patch.object(herdr, "read_conversation", return_value=herdr.UnknownConversation("native identity unavailable")),
              patch.object(herdr.Herdr, "panes", return_value=[{
                  "pane_id": "w1:p0", "workspace_id": "w1", "session_id": None, "label": "morty"}]),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(restart.restart(stop_all=True, dry_run=True), 0)
        self.assertEqual(output.getvalue().splitlines(), [
            "morty: left alone; no proved local restart target"])
        self.assertEqual((self.stops, self.launches), ([], []))

    def test_restart_all_dry_run_lists_every_live_row_with_its_tab_position(self) -> None:
        self.rows = (replace(self.rows[0], name="unit"),
            replace(self.rows[1], kind="worker", name="codex-worker", parent=staff.ReportsTo(self.rows[0].id),
                    worktree=staff.WORKTREE_UNFILLED),
            replace(self.rows[2], kind="agent", name="morty", parent=staff.StaffRoot(),
                    worktree=staff.WORKTREE_UNFILLED, doc=staff.LiveDoc(remote.local_machine(), self.root / "morty.md")))
        layout: herdr.SavedLayout = {"at": 1, "workspaces": [{"name": "saved", "seen": 1,
            "tabs": [{"name": "old", "seen": 1, "row_id": str(self.rows[1].id)}]}]}
        shutdown_record.save([str(self.rows[2].id)])
        before = shutdown_record.record_path().read_bytes()
        for identity, label, unavailable in ((str(self.rows[0].current_session), "live", False), (None, "live", False),
                                           (str(self.rows[0].current_session), None, False), (None, "live", True)):
            with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot(self.rows)),
                  patch.object(unit_runs, "worker_transport", return_value="mesh"),
                  patch.object(herdr.Herdr, "panes", return_value=[{**self.panes()[0], "session_id": identity}],
                               side_effect=herdr.HerdrFailure("socket unavailable") if unavailable else None),
                  patch.object(herdr, "saved_layout", return_value=layout),
                  patch.object(self, "workspace_label", label),
                  redirect_stderr(io.StringIO()) as errors,
                  patch.object(staff, "row_operation") as lock, patch.object(herdr, "_write_layout") as write,
                  patch.object(herdr.Herdr, "save_layout") as save, redirect_stdout(io.StringIO()) as output):
                self.assertEqual(restart.restart(stop_all=True, dry_run=True), 0)
            self.assertEqual(output.getvalue().splitlines(), [
                f"{row.name}: {row.id}, {row.kind}, {position}" for row, position in zip(self.rows,
                    ("workspace live, tab 1 (unit)" if identity and label else "no tab", "workspace saved, tab 1 (old)", "no tab"))])
            self.assertEqual(len(errors.getvalue().splitlines()), int(unavailable or (identity is not None and label is None)))
            if unavailable:
                self.assertEqual(errors.getvalue(), "restart: layout anomaly: socket unavailable\n")
            for unused in (lock, save, write):
                unused.assert_not_called()
            self.assertEqual(shutdown_record.record_path().read_bytes(), before)
            self.assertEqual((self.stops, self.launches), ([], []))
            self.assertEqual(shutdown_record.row_folder(self.rows[2], staff.StaffSnapshot(self.rows)), (remote.local_machine(), self.root))
            self.assertEqual(shutdown_record.row_folder(replace(self.rows[2], worktree=staff.PresentWorktree(remote.local_machine(), self.root / "own")),
                staff.StaffSnapshot(self.rows)), (remote.local_machine(), self.root / "own"))

    def test_codex_dry_run_uses_the_newest_pane_record_and_reports_rows_without_a_tab(self) -> None:
        self.rows = (replace(self.rows[0], kind="agent", name="morty"), *self.rows[1:])
        row = self.rows[0]
        older: pane_record.PaneLaunch = {"kind": "codex", "pane_id": "w1:p2", "session_id": str(row.id),
            "config_dir": "/account", "cwd": str(self.root), "name": row.name, "model": "",
            "remote_control": False, "environment": {}, "flags_read": True, "written_at": "1"}
        for reference in (str(row.id), str(row.current_session)):
            newest = older.copy()
            newest.update(session_id=reference, pane_id="w1:p0", written_at="2")
            with (self.subTest(reference=reference),
                  patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot(self.rows)),
                  patch.object(restart, "live_rows", return_value=list(self.rows)),
                  patch.object(herdr.Herdr, "panes", return_value=[{**pane, "session_id": None} for pane in self.panes()]),
                  patch.object(herdr, "saved_layout", return_value={"at": 1, "workspaces": []}),
                  patch.object(pane_record, "launches", return_value=[newest, older]) as launches,
                  redirect_stdout(io.StringIO()) as output):
                self.assertEqual(restart.restart(stop_all=True, dry_run=True), 0)
            launches.assert_called_once_with()
            self.assertEqual(output.getvalue().splitlines(), [
                f"{row.name}: {row.id}, agent, workspace live, tab 1 ({row.name})",
                *[f"{other.name}: {other.id}, {other.kind}, no tab" for other in self.rows[1:]]])

    def test_row_panes_keeps_session_matches_and_ignores_an_absent_recorded_pane(self) -> None:
        row = self.rows[0]
        entry: pane_record.PaneLaunch = {"kind": "codex", "pane_id": "w1:p9", "session_id": str(row.id),
            "config_dir": "/account", "cwd": str(self.root), "name": row.name, "model": "",
            "remote_control": False, "environment": {}, "flags_read": True, "written_at": "2"}
        older = entry.copy()
        older.update(pane_id="w1:p1", written_at="1")
        with patch.object(pane_record, "launches", return_value=[entry, older]):
            self.assertEqual(restart.row_panes([row], herdr.Herdr("agents")), [(row, "w1:p0")])
            with patch.object(herdr.Herdr, "panes", return_value=[{**pane, "session_id": None} for pane in self.panes()]):
                self.assertEqual(restart.row_panes([row], herdr.Herdr("agents")), [])

    def test_restart_lock_refusal_leaves_the_record_unchanged(self) -> None:
        reference = str(self.rows[0].id)
        for stop_all in (False, True):
            for saved in ([], [reference, "another-pending-row"]):
                with self.subTest(stop_all=stop_all, saved=saved):
                    shutdown_record.record_path().unlink(missing_ok=True)
                    if saved:
                        shutdown_record.save(saved)
                    path = shutdown_record.record_path()
                    before = path.read_bytes() if path.exists() else None
                    with (patch.object(staff, "row_operation", side_effect=staff.StaffRefused("row is locked")),
                          patch.object(restart, "start_row") as start,
                          redirect_stderr(io.StringIO()) as errors):
                        self.assertEqual(restart.restart(stop_all=stop_all, only=(reference,)), 0)
                    self.assertEqual(path.read_bytes() if path.exists() else None, before)
                    start.assert_not_called()
                    self.assertEqual(errors.getvalue().splitlines(),
                        [f"{self.rows[0].name}: restart anomaly: row is locked"])

    def test_restart_record_delta_keeps_another_runs_failed_row(self) -> None:
        self.running.clear()
        other = "another-failed-row"

        def start(_row: staff.StaffRow, _record: staff.StaffSnapshot, _client: herdr.Herdr, *,
                  present: bool, resolver: session_staff.RowResolver,
                  target_account: str | accounts.AccountSelection,
                  worker_scope: restart.WorkerRestartScope,
                  observed: restart.RestartObservations) -> None:
            _ = present, resolver, target_account, worker_scope, observed
            shutdown_record.update(started=[], pending=[other])

        shutdown_record.save([str(self.rows[0].id), str(self.rows[1].id)])
        with patch.object(restart, "start_row", side_effect=start):
            self.assertEqual(restart.restart(only=(str(self.rows[0].id),)), 0)
        self.assertEqual(shutdown_record.read(), [str(self.rows[1].id), other])

    def test_failed_trust_write_keeps_the_pending_restart_and_reports_once(self) -> None:
        self.running.clear()
        row = self.rows[0]
        shutdown_record.save([str(row.id)])
        reason = f"{self.root}: trust anomaly: {self.root / '.claude2/.claude.json'}: PermissionError"

        def launch(*_args: object, **_kwargs: object) -> herdr.AgentLaunched:
            print(reason, file=sys.stderr)
            return herdr.AgentLaunched("", herdr.ClaudeTrustNotWritten(reason))

        with (patch.object(herdr, "launch_prepared_agent", side_effect=launch),
              redirect_stderr(io.StringIO()) as errors):
            result = restart.restart()
        self.assertEqual(result, 1)
        self.assertEqual(shutdown_record.read(), [str(row.id)])
        self.assertEqual(errors.getvalue().splitlines(), [reason])
        self.assertEqual(self.launches, [])

    def test_started_row_leaves_pending_when_layout_save_fails(self) -> None:
        self.running.clear()
        shutdown_record.save([str(self.rows[0].id)])
        with (patch.object(herdr.Herdr, "remember_row_tab", side_effect=herdr.HerdrFailure("layout unavailable")),
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.restart(), 0)
        self.assertEqual(shutdown_record.read(), [])
        self.assertIn(str(self.rows[0].current_session), self.running)
        self.assertEqual(errors.getvalue(), f"{self.rows[0].name}: restart anomaly: layout unavailable\n")

    def test_unit_without_a_saved_tab_opens_in_its_parent_workspace(self) -> None:
        self.running.clear()
        parent = staff.StaffRow(staff.StaffId("parent"), "showrunner", "production", staff.STAFF_ROOT,
                                current_session=staff.StaffId("parent-session"))
        owner = staff.StaffRow(staff.StaffId("owner"), "agent", "owner", staff.ReportsTo(parent.id),
                               current_session=staff.StaffId("owner-session"))
        row = replace(self.rows[0], parent=staff.ReportsTo(owner.id))
        record = staff.StaffSnapshot((parent, owner, row))
        with (patch.object(pane_record, "publication_for_session", return_value=None),
              patch.object(herdr, "saved_layout", return_value=herdr.SavedLayout(at=1, workspaces=[]))):
            self.assertEqual(restart.start_row(row, record, herdr.Herdr("agents")), "w1:p0")
        self.assertEqual(self.launches, [(row.name, herdr.NewTab(parent.name, row.name))])

    def test_unit_with_a_saved_tab_keeps_its_saved_workspace(self) -> None:
        self.running.clear()
        parent = staff.StaffRow(staff.StaffId("parent"), "showrunner", "production", staff.STAFF_ROOT,
                                current_session=staff.StaffId("parent-session"))
        row = replace(self.rows[0], parent=staff.ReportsTo(parent.id))
        with patch.object(pane_record, "publication_for_session", return_value=None):
            self.assertEqual(restart.start_row(row, staff.StaffSnapshot((parent, row)),
                                             herdr.Herdr("agents")), "w1:p0")
        self.assertEqual(self.launches, [(row.name, herdr.NewTab("saved", row.name))])

    def test_saved_tab_reuses_only_the_targets_shell_and_splits_beside_a_sibling(self) -> None:
        layout: herdr.SavedLayout = {"at": 1, "workspaces": [{"name": "saved", "seen": 1,
            "tabs": [{"name": "unit-0", "seen": 1, "row_id": "row-0", "terminal_id": "own-terminal"}]}]}

        def result(*arguments: str) -> dict[str, object]:
            if arguments == ("workspace", "list"):
                return {"workspaces": [{"workspace_id": "w1", "label": "saved"}]}
            if arguments == ("tab", "list"):
                return {"tabs": [{"workspace_id": "w1", "tab_id": "t1", "label": "unit-0"}]}
            if arguments == ("pane", "list"):
                return {"panes": [{"pane_id": "w1:sibling", "workspace_id": "w1", "tab_id": "t1", "terminal_id": "sibling-terminal"},
                                  {"pane_id": "w1:own", "workspace_id": "w1", "tab_id": "t1", "terminal_id": "own-terminal"}]}
            raise AssertionError(arguments)

        client = herdr.Herdr("fixture")
        with (patch.object(herdr, "saved_layout", return_value=layout),
              patch.object(client, "result", side_effect=result),
              patch.object(client, "pane_activity", return_value=herdr.PaneAtShell()) as activity):
            self.assertEqual(client.saved_row_tab("row-0", "unit-0", "fallback"), herdr.ExistingPane("w1:own", "own-terminal"))
            activity.assert_called_once_with("w1:own")
            activity.return_value = herdr.PaneRunning(("codex",))
            self.assertEqual(client.saved_row_tab("row-0", "unit-0", "fallback"), herdr.SplitPane("w1:sibling", "right", 0.5, "unit-0"))
            layout["workspaces"][0]["tabs"][0]["terminal_id"] = "missing-terminal"
            activity.reset_mock()
            self.assertEqual(client.saved_row_tab("row-0", "unit-0", "fallback"), herdr.SplitPane("w1:sibling", "right", 0.5, "unit-0"))
            activity.assert_not_called()

    def test_held_sessions_saves_live_rows_and_tabs_and_keeps_pending_rows(self) -> None:
        shutdown_record.save(["earlier"])
        self.running.remove(str(self.rows[2].current_session))
        with (patch.object(shutdown, "restart", restart), patch.object(shutdown, "wait_for_workers") as wait,
              patch.object(herdr.Herdr, "save_layout") as save,
              patch.object(herdr.Herdr, "remember_row_tab") as remember,
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(shutdown.main(["held-sessions"]), 0)
        self.assertEqual(shutdown_record.read(), ["earlier", "row-0", "row-1"])
        save.assert_called_once()
        self.assertEqual([call.args for call in remember.call_args_list], [("row-0", "w1:p0"), ("row-1", "w1:p1")])
        wait.assert_called_once()
        self.assertRegex(output.getvalue(), r"^saved 2 rows for restart at \d{2}:\d{2}\n$")
        self.assertEqual(self.stops, [])

    def test_staff_read_error_keeps_pending_rows_and_reports_one_anomaly(self) -> None:
        shutdown_record.save(["earlier"])
        with (patch.object(shutdown, "restart", restart), patch.object(shutdown, "wait_for_workers") as wait,
              patch.object(staff, "initial_snapshot", side_effect=OSError("staff unavailable")),
              redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(shutdown.main(["held-sessions"]), 0)
        self.assertEqual(shutdown_record.read(), ["earlier"])
        wait.assert_called_once_with(staff.StaffSnapshot(()))
        self.assertTrue(output.getvalue().startswith("saved 0 rows for restart at "))
        self.assertEqual(errors.getvalue(), "restart: save anomaly: staff unavailable\n")

    def test_restart_fixed_reference_uses_current_native_id_name_and_folder(self) -> None:
        native_id = str(self.rows[0].current_session)
        shutdown_record.save([str(self.rows[0].id)])
        folder = self.root / "renamed-folder"
        folder.mkdir()
        self.rows = (replace(self.rows[0], name="renamed",
            worktree=staff.PresentWorktree(remote.local_machine(), folder)), *self.rows[1:])
        record = staff.StaffSnapshot(self.rows)
        self.running.clear()
        prepared = herdr.PreparedAgentEnvironment(folder, {})
        with (patch.object(staff, "initial_snapshot", return_value=record),
              patch.object(herdr, "prepare_session_environment", return_value=prepared) as prepare,
              patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                  "w1:p0", herdr.PromptSubmissionNotRequired())) as launch):
            self.assertEqual(restart.restart(), 0)
        prepare.assert_called_once()
        self.assertEqual(prepare.call_args.args[:2], (native_id, str(folder)))
        self.assertEqual(prepare.call_args.args[3], herdr.NO_ENVIRONMENT_REMOVALS)
        self.assertEqual(launch.call_args.args[3:5],
                         ("claude", ["--resume", native_id, "--name", "renamed"]))
        self.assertEqual(launch.call_args.kwargs["agent"], "renamed")
        self.assertEqual(shutdown_record.read(), [])

    def test_restart_failure_keeps_only_the_failed_fixed_reference(self) -> None:
        identities = [str(row.id) for row in self.rows]
        shutdown_record.save(identities)
        self.running.clear()
        launch = self.launch

        def fail_first(name: str, destination: herdr.NewTab | herdr.SplitPane | herdr.ExistingPane,
                       prepared: herdr.PreparedAgentEnvironment, kind: str, arguments: list[str],
                       opening: herdr.NoOpeningPrompt, timeout: int, *, agent: str = "",
                       reported_unknown: str = "", codex: herdr.CodexConversations | herdr.CodexLaunchRead = herdr.CodexLaunchRead.CURRENT) -> herdr.AgentLaunched:
            if agent == self.rows[0].name:
                raise herdr.HerdrFailure("fixture launch failed")
            return launch(name, destination, prepared, kind, arguments, opening, timeout, agent=agent,
                          reported_unknown=reported_unknown, codex=codex)

        with (patch.object(herdr, "launch_prepared_agent", side_effect=fail_first),
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.restart(), 0)
        self.assertEqual(shutdown_record.read(), [identities[0]])
        self.assertEqual(errors.getvalue(), "unit-0: restart anomaly: fixture launch failed\n")
        self.assertCountEqual([name for name, _ in self.launches], [row.name for row in self.rows[1:]])

    def test_restart_staff_read_error_preserves_fixed_references(self) -> None:
        shutdown_record.save([str(self.rows[0].id)])
        before = shutdown_record.record_path().read_bytes()
        with (patch.object(staff, "initial_snapshot", side_effect=OSError("staff unavailable")),
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.restart(), 0)
        self.assertEqual(shutdown_record.record_path().read_bytes(), before)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn("staff unavailable", errors.getvalue())
        self.assertEqual(self.launches, [])

    def test_restart_skips_a_saved_name_after_another_row_reuses_it(self) -> None:
        shutdown_record.save([str(self.rows[0].id), "missing"])
        self.running.clear()
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.assertEqual(restart.restart(), 0)
        self.assertEqual(shutdown_record.read(), ["missing"])
        self.assertEqual([name for name, _ in self.launches], [self.rows[0].name])
        path = shutdown_record.record_path()
        before = (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ctime_ns)
        self.rows = (self.rows[0], replace(self.rows[1], name="missing"), *self.rows[2:])
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot(self.rows)),
              redirect_stderr(errors)):
            self.assertEqual(restart.restart(), 0)
            self.assertEqual(restart.restart(), 0)
        self.assertEqual([name for name, _ in self.launches], [self.rows[0].name])
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ctime_ns), before)
        self.assertEqual(errors.getvalue().splitlines(),
            ["missing: restart anomaly: in the restart record, no fixed staff id"] * 3)
        self.assertEqual(self.stops, [])

    def test_restart_saved_conversation_reference_does_not_select_a_member(self) -> None:
        reference = str(self.rows[0].current_session)
        shutdown_record.save([reference])
        path = shutdown_record.record_path()
        before = (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ctime_ns)
        self.running.clear()
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(restart.restart(), 0)
        self.assertEqual(self.launches, [])
        self.assertEqual(errors.getvalue(),
            f"{reference}: restart anomaly: in the restart record, no fixed staff id\n")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ctime_ns), before)

    def test_restart_command_still_resolves_a_name_without_selecting_saved_entries(self) -> None:
        shutdown_record.save([str(self.rows[1].id), "missing"])
        self.running.clear()
        self.assertEqual(restart.restart(only=(self.rows[0].name,)), 0)
        self.assertEqual([name for name, _ in self.launches], [self.rows[0].name])
        self.assertEqual(shutdown_record.read(), [str(self.rows[1].id), "missing"])

    def test_bare_dry_run_reports_record_time_and_missing_rows(self) -> None:
        shutdown_record.save(["row-0", "gone"])
        saved_at = time.strftime("%H:%M", time.localtime(shutdown_record.record_path().stat().st_mtime))
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(restart.restart(dry_run=True), 0)
        self.assertEqual(output.getvalue().splitlines(), [f"restart record: 2 rows saved at {saved_at}",
            "unit-1: live, not in the restart record", "unit-2: live, not in the restart record",
            "unit-0: row-0, unit, workspace live, tab 1 (unit-0)"])
        self.assertEqual(errors.getvalue(), "gone: restart anomaly: in the restart record, no fixed staff id\n")
        self.assertEqual(shutdown_record.read(), ["row-0", "gone"])

    def test_layout_error_keeps_the_live_rows_and_other_tab_saves(self) -> None:
        with (patch.object(herdr.Herdr, "save_layout", side_effect=herdr.HerdrFailure("layout unavailable")),
              patch.object(herdr.Herdr, "remember_row_tab") as remember,
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.live_rows(staff.StaffSnapshot(self.rows), herdr.Herdr("fixture"), save_tabs=True),
                             list(self.rows))
        self.assertEqual(remember.call_count, 3)
        self.assertEqual(errors.getvalue(), "restart: layout anomaly: layout unavailable\n")

    def test_restart_all_resumes_a_pending_holder_after_an_interruption(self) -> None:
        pending = str(self.rows[0].id)
        shutdown_record.save([pending, "missing-row"])
        self.running.clear()
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(restart.restart(stop_all=True), 0)
        self.assertEqual([name for name, _ in self.launches], [self.rows[0].name])
        self.assertEqual(shutdown_record.read(), ["missing-row"])
        self.assertIn("missing-row: restart anomaly: in the restart record, no fixed staff id", errors.getvalue())

    def test_stop_all_saves_each_row_before_stop_and_starts_nothing(self) -> None:
        def stop(identity: str, *, deadline: float = math.inf,
                 codex: herdr.CodexConversations | None = None,
                 runs: dict[str, list[str]] | None = None) -> stop_resume.Stopped:
            row = next(row for row in self.rows if row.current_session == identity)
            self.assertIn(str(row.id), shutdown_record.read())
            return self.stop(identity, deadline=deadline, codex=codex, runs=runs)

        with patch.object(stop_resume, "stop", side_effect=stop):
            self.assertEqual(shutdown.main(["stop", "--all"]), 0)
        self.assertCountEqual(shutdown_record.read(), [str(row.id) for row in self.rows])
        self.assertCountEqual(self.stops, [str(row.id) for row in self.rows])
        self.assertEqual(self.launches, [])

    def test_stop_all_keeps_a_freestanding_agent_for_its_saved_tab(self) -> None:
        row = replace(self.rows[0], kind="agent", name="TEST-agent", worktree=staff.WORKTREE_UNFILLED)
        self.rows = (row,)
        record = staff.StaffSnapshot(self.rows)
        with patch.object(staff, "initial_snapshot", return_value=record):
            self.assertEqual(shutdown.main(["stop", "--all"]), 0)
            self.assertEqual(shutdown_record.read(), [str(row.id)])
            self.assertEqual(self.launches, [])
            self.assertEqual(restart.restart(), 0)
        self.assertEqual(self.stops, [str(row.id)])
        self.assertEqual(self.launches, [(row.name, herdr.NewTab("saved", row.name))])
        self.assertEqual(shutdown_record.read(), [])

    def test_stop_all_dry_run_saves_nothing_and_names_each_row_once(self) -> None:
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(shutdown.main(["stop", "--all", "--dry-run"]), 0)
        self.assertEqual(output.getvalue().splitlines(), [
            f"{row.name}: {row.id}, {row.kind}, workspace live, tab {n + 1} ({row.name}); would stop and save"
            for n, row in enumerate(self.rows)])
        self.assertFalse(shutdown_record.record_path().exists())
        self.assertEqual((self.stops, self.launches), ([], []))

    def test_live_rows_proves_a_codex_resume_without_an_open_rollout_from_its_pane(self) -> None:
        row = replace(self.rows[0], kind="agent", name="TEST-brain", worktree=staff.WORKTREE_UNFILLED)
        self.rows = (row,)
        self.running.clear()
        pane = {"pane_id": "w1:p0", "workspace_id": "w1", "tab_id": "w1:t0",
                "terminal_id": "terminal-0", "agent": "codex"}
        result = self.layout_result

        def layout(*arguments: str) -> dict[str, object]:
            if arguments == ("pane", "list"):
                return {"panes": [pane]}
            if arguments[:2] == ("pane", "process-info"):
                return {"process_info": {"shell_pid": 990090, "foreground_process_group_id": 990100,
                    "foreground_processes": [{"pid": 990100, "argv": ["codex", "resume", str(row.current_session)]}]}}
            return result(*arguments)

        with (patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations(
                    {}, ("TEST-brain (Codex thread unknown)",))),
              patch.object(herdr.Herdr, "result", side_effect=layout),
              patch.object(herdr, "native_process", return_value=herdr.NativeAgentProcess(
                    herdr.LaunchArgument(str(row.current_session)), "fixture-start", {})),
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.live_rows(staff.StaffSnapshot(self.rows), herdr.Herdr("agents")), [row])
        self.assertEqual(errors.getvalue(), "")

    def test_restart_all_starts_each_row_in_its_saved_tab(self) -> None:
        with (patch.object(herdr.Herdr, "save_layout") as save,
              patch.object(herdr.Herdr, "remember_row_tab") as remember):
            self.assertEqual(restart.restart(stop_all=True), 0)
        save.assert_called_once()
        self.assertEqual([call.args for call in remember.call_args_list[:3]],
                         [(str(row.id), f"w1:p{n}") for n, row in enumerate(self.rows)])
        self.assertCountEqual(self.stops, [str(row.id) for row in self.rows])
        self.assertCountEqual(self.launches, [(row.name, herdr.NewTab("saved", row.name)) for row in self.rows])
        self.assertEqual(self.running, {str(row.current_session) for row in self.rows})

    def test_inventory_keeps_sessions_with_unknown_staff_folders(self) -> None:
        for session in (unit("unknown"), worker_session("unknown-worker", owner=None)):
            for record in (staff.StaffSnapshot(()), staff.StaffSnapshot((
                    replace(self.rows[0], current_session=staff.StaffId(session["session_id"]),
                            worktree=staff.WORKTREE_UNFILLED),))):
                with self.subTest(kind=session["kind"], rows=len(record.rows)):
                    with patch.object(staff, "snapshot", return_value=record):
                        parsed = inventory.parse_session(session, "fixture")
                    self.assertEqual(parsed["session_id"], session["session_id"])
                    self.assertEqual(parsed["cwd"], {"kind": "unknown folder"})
                    self.assertEqual(parsed["checkout"], {"kind": "not a checkout"})

    def test_inventory_uses_the_current_staff_folder_without_rewriting_the_record(self) -> None:
        session = unit(str(self.rows[0].current_session))
        saved = json.dumps(session)
        for folder in (self.root / "before", self.root / "after"):
            record = staff.StaffSnapshot((replace(self.rows[0],
                worktree=staff.PresentWorktree(remote.local_machine(), folder)),))
            with patch.object(staff, "snapshot", return_value=record):
                parsed = inventory.parse_session(session, "fixture")
            self.assertEqual(parsed["cwd"], str(folder))
            self.assertEqual(json.dumps(session), saved)

    def test_restart_skips_an_unknown_unit_folder_and_starts_the_other_rows(self) -> None:
        parent = staff.StaffRow(staff.StaffId("parent"), "showrunner", "parent", staff.StaffRoot(),
                               current_session=staff.StaffId("parent-session"),
                               doc=staff.LiveDoc(remote.local_machine(), self.root / "production.md"))
        self.rows = (replace(self.rows[0], parent=staff.ReportsTo(parent.id), worktree=staff.WORKTREE_UNFILLED), *self.rows[1:])
        record = staff.StaffSnapshot((*self.rows, parent))
        self.running.clear()
        shutdown_record.save([str(row.id) for row in self.rows])
        with (patch.object(staff, "initial_snapshot", return_value=record),
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.restart(), 0)
        self.assertCountEqual([name for name, _ in self.launches], [row.name for row in self.rows[1:]])
        self.assertEqual(errors.getvalue().splitlines(), [
            "unit-0: restart anomaly: unit-0: the staff worktree is unavailable"])
        self.assertEqual(shutdown_record.read(), [str(self.rows[0].id)])

    def test_socketless_running_sessions_restart_or_print_and_unassigned_sessions_print_once(self) -> None:
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                records = self.records_for(self.running | {"orphan"})
                records = [record for record in records if record["sessionId"] != self.rows[0].current_session]
                orphan = next(record for record in records if record["sessionId"] == "orphan")
                orphan.update(name="unassigned", cwd="/tmp/orphan")
                def codex_conversations(*, wanted: str = "") -> herdr.CodexConversations:
                    _ = wanted
                    identities: dict[str, tuple[int, ...]] = {"orphan": (456,), "codex-orphan": (789,)}
                    if self.rows[0].current_session in self.running:
                        identities[str(self.rows[0].current_session)] = (123,)
                    return herdr.CodexConversations(identities, ())
                with (patch.object(sessions, "_readable_records", return_value=records),
                      patch.object(herdr, "codex_conversations", side_effect=codex_conversations),
                      patch.object(herdr, "running_codex_threads",
                                   return_value={str(self.rows[0].current_session), "orphan", "codex-orphan"}) as codex,
                      redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
                    self.assertEqual(sessions.live_sessions(), [])
                    self.assertEqual(restart.restart(stop_all=True, dry_run=dry_run), 0)
                self.assertEqual(errors.getvalue().splitlines(), [
                    "codex-orphan: restart anomaly: unassigned live session",
                    "unassigned in /tmp/orphan: restart anomaly: unassigned live session"])
                codex.assert_called_once()
                expected = [f"{row.name}: {row.id}, {row.kind}, workspace live, tab {index} ({row.name})"
                            if dry_run else f"{row.name}: restarted" for index, row in enumerate(self.rows, 1)]
                if dry_run:
                    self.assertEqual(output.getvalue().splitlines(), expected)
                else:
                    self.assertCountEqual(output.getvalue().splitlines(), expected)
                self.assertCountEqual(self.stops, [] if dry_run else [str(row.id) for row in self.rows])
                self.assertCountEqual([name for name, _place in self.launches], [] if dry_run else [row.name for row in self.rows])

    def test_restart_all_rerun_starts_only_the_rows_that_failed(self) -> None:
        launch = self.launch
        def fail_one(name: str, destination: herdr.NewTab | herdr.SplitPane | herdr.ExistingPane,
                     prepared: herdr.PreparedAgentEnvironment, kind: str, arguments: list[str],
                     opening: herdr.NoOpeningPrompt, timeout: int, *, agent: str = "", reported_unknown: str = "", codex: herdr.CodexConversations | herdr.CodexLaunchRead = herdr.CodexLaunchRead.CURRENT) -> herdr.AgentLaunched:
            if agent == self.rows[1].name:
                raise OSError("fixture launch failed")
            return launch(name, destination, prepared, kind, arguments, opening, timeout, agent=agent, reported_unknown=reported_unknown, codex=codex)
        with patch.object(herdr, "launch_prepared_agent", side_effect=fail_one):
            self.assertEqual(restart.restart(stop_all=True), 0)
        self.launches.clear()
        self.assertEqual(restart.restart(), 0)
        self.assertEqual(self.launches, [(self.rows[1].name, herdr.NewTab("saved", self.rows[1].name))])
        self.launches.clear()
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(restart.restart(), 0)
        self.assertEqual(self.launches, [])
        self.assertEqual(output.getvalue(), "")

    def test_restart_all_record_keeps_only_rows_that_failed_to_start(self) -> None:
        self.running.remove(str(self.rows[2].current_session))
        launch = self.launch

        def fail_one(name: str, destination: herdr.NewTab | herdr.SplitPane | herdr.ExistingPane,
                     prepared: herdr.PreparedAgentEnvironment, kind: str, arguments: list[str],
                     opening: herdr.NoOpeningPrompt, timeout: int, *, agent: str = "", reported_unknown: str = "", codex: herdr.CodexConversations | herdr.CodexLaunchRead = herdr.CodexLaunchRead.CURRENT) -> herdr.AgentLaunched:
            if agent == self.rows[1].name:
                raise OSError("fixture launch failed")
            return launch(name, destination, prepared, kind, arguments, opening, timeout, agent=agent, reported_unknown=reported_unknown, codex=codex)

        with patch.object(herdr, "launch_prepared_agent", side_effect=fail_one):
            self.assertEqual(restart.restart(stop_all=True), 0)
        self.assertEqual(shutdown_record.read(), [str(self.rows[1].id)])
        self.assertCountEqual(self.stops, [str(row.id) for row in self.rows[:2]])
        self.assertEqual(restart.restart(), 0)
        self.assertEqual(shutdown_record.read(), [])
        self.assertEqual(self.running, {str(row.current_session) for row in self.rows[:2]})

    def test_restart_all_writes_only_live_row_ids(self) -> None:
        self.running.remove(str(self.rows[1].current_session))
        self.assertEqual(restart.restart(stop_all=True, dry_run=True), 0)
        self.assertFalse(shutdown_record.record_path().exists())
        self.assertEqual(self.stops, [])
        self.assertEqual(self.launches, [])
        with patch.object(restart, "start_row", side_effect=OSError("fixture launch failed")):
            self.assertEqual(restart.restart(stop_all=True), 0)
        self.assertEqual(json.loads(shutdown_record.record_path().read_text()), [self.rows[0].id, self.rows[2].id])

    def test_stop_row_reports_one_reason_when_the_target_terminal_is_unknown(self) -> None:
        pane: herdr.PaneIdentity = {"pane_id": "w1:p0", "workspace_id": "w1", "session_id": str(self.rows[0].current_session)}
        with (patch.object(herdr.Herdr, "panes", return_value=[pane]),
              patch.object(stop_resume, "stop") as stopped, redirect_stderr(io.StringIO()) as errors):
            self.assertFalse(restart.stop_row(self.rows[0], herdr.Herdr("agents")))
        stopped.assert_not_called()
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn("terminal is unknown", errors.getvalue())

    def test_restart_all_skips_a_stop_when_the_pane_runs_another_conversation(self) -> None:
        with (patch.object(herdr, "native_process", return_value=herdr.NativeAgentProcess(
                  herdr.EnvironmentThread("another-conversation"), "fixture-start", {})),
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.restart(stop_all=True), 0)
        self.assertEqual(self.stops, [])
        self.assertEqual(self.launches, [])
        self.assertIn("would signal a process that does not run session", errors.getvalue())

    def retained_director(self, session: str = "agents") -> pane_launch.LaunchPublication:
        row = self.rows[0]
        entry: pane_record.PaneLaunch = {"kind": "claude", "pane_id": "w1:p0", "session_id": str(row.id),
            "config_dir": str(self.root / ".claude2"), "cwd": str(self.root), "name": row.name,
            "model": "", "remote_control": False, "environment": {}, "flags_read": True, "written_at": "fixture"}
        return pane_launch.LaunchPublication(pane_record.record_path(session), entry)

    def test_stopped_director_reuses_its_retained_shell_without_a_saved_binding(self) -> None:
        row = self.rows[0]
        self.running.clear()
        pane: herdr.PaneIdentity = {"pane_id": "w1:p0", "workspace_id": "w1", "session_id": None,
                                     "terminal_id": "original-terminal"}
        with (patch.object(pane_record, "publication_for_session", return_value=self.retained_director()),
              patch.object(herdr.Herdr, "saved_row_tab", return_value=herdr.NewTab("saved", row.name)),
              patch.object(herdr.Herdr, "panes", return_value=[pane]),
              patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                  "w1:p0", herdr.PromptSubmissionNotRequired())) as launch,
              patch.object(herdr.Herdr, "pane_activity", return_value=herdr.PaneAtShell()),
              patch.object(herdr, "open_tab") as opened,
              patch.object(herdr.Herdr, "close_tab_confirmed") as closed):
            self.assertEqual(restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents")), "w1:p0")
        self.assertEqual(launch.call_args.args[1], herdr.ExistingPane("w1:p0", "original-terminal"))
        opened.assert_not_called()
        closed.assert_not_called()

    def test_retained_shell_from_another_session_keeps_the_saved_destination(self) -> None:
        row = self.rows[0]
        self.running.clear()
        destination = herdr.SplitPane("w1:sibling", "right", 0.5, row.name)
        pane: herdr.PaneIdentity = {"pane_id": "w1:p0", "workspace_id": "w1", "session_id": None,
                                     "terminal_id": "other-terminal"}
        with (patch.object(pane_record, "publication_for_session", return_value=self.retained_director("other")),
              patch.object(herdr.Herdr, "saved_row_tab", return_value=destination),
              patch.object(herdr.Herdr, "panes", return_value=[pane]),
              patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                  "w1:p0", herdr.PromptSubmissionNotRequired())) as launch,
              patch.object(herdr.Herdr, "pane_activity", return_value=herdr.PaneAtShell())):
            _ = restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents"))
        self.assertEqual(launch.call_args.args[1], destination)

    def test_row_without_a_retained_launch_keeps_the_saved_destination(self) -> None:
        row = self.rows[0]
        self.running.clear()
        destination = herdr.SplitPane("w1:sibling", "down", 0.5, row.name)
        with (patch.object(pane_record, "publication_for_session", return_value=None),
              patch.object(herdr.Herdr, "saved_row_tab", return_value=destination)):
            _ = restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents"))
        self.assertEqual(self.launches, [(row.name, destination)])

    def test_retained_pane_requires_a_terminal_and_a_shell_for_reuse(self) -> None:
        row = self.rows[0]
        destination = herdr.NewTab("saved", row.name)
        for terminal, activity in (("original-terminal", herdr.PaneRunning(("vim",))), ("", herdr.PaneAtShell())):
            with self.subTest(terminal=terminal, activity=activity):
                self.running.clear()
                self.launches.clear()
                pane: herdr.PaneIdentity = {"pane_id": "w1:p0", "workspace_id": "w1", "session_id": None,
                                             "terminal_id": terminal}
                with (patch.object(pane_record, "publication_for_session", return_value=self.retained_director()),
                      patch.object(herdr.Herdr, "saved_row_tab", return_value=destination),
                      patch.object(herdr.Herdr, "panes", return_value=[pane]),
                      patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                          "w1:p0", herdr.PromptSubmissionNotRequired())) as launch,
                      patch.object(herdr.Herdr, "pane_activity", return_value=activity)):
                    _ = restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents"))
                self.assertEqual(launch.call_args.args[1], destination)

    def test_unlabelled_old_pane_with_a_program_stays_open_and_workers_restore(self) -> None:
        row = self.rows[0]
        self.running.clear()
        pane: herdr.PaneIdentity = {"pane_id": "w1:p0", "workspace_id": "w1", "session_id": None,
                                     "terminal_id": "old-terminal", "label": None}
        with (patch.object(pane_record, "publication_for_session", return_value=self.retained_director()),
              patch.object(herdr.Herdr, "panes", return_value=[pane]),
              patch.object(herdr.Herdr, "pane_activity", return_value=herdr.PaneRunning(("vim",))) as activity,
              patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                  "w1:p9", herdr.PromptSubmissionNotRequired())),
              patch.object(pane_launch, "finish_restore") as finish,
              patch.object(herdr.Herdr, "close_tab_confirmed") as closed,
              patch.object(restart, "_restore_workers") as workers,
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents")), "w1:p9")
        self.assertIn(call("w1:p0"), activity.call_args_list)
        self.assertEqual(errors.getvalue().splitlines(), [
            "w1:p0: pane close anomaly: pane holds other work; kept open"])
        finish.assert_not_called()
        closed.assert_not_called()
        workers.assert_called_once_with(row, staff.StaffSnapshot(self.rows))

    def test_failed_restore_cleanup_reports_once_and_workers_restore(self) -> None:
        row = self.rows[0]
        self.running.clear()
        with (patch.object(pane_record, "publication_for_session", return_value=self.retained_director()),
              patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                  "w1:p9", herdr.PromptSubmissionNotRequired())),
              patch.object(pane_launch, "finish_restore", side_effect=herdr.HerdrFailure("wrong_target", code="wrong_target")),
              patch.object(herdr.Herdr, "remember_row_tab") as remember,
              patch.object(restart, "_restore_workers") as workers,
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents")), "w1:p9")
        self.assertEqual(errors.getvalue().splitlines(), [f"{row.name}: restart anomaly: wrong_target"])
        remember.assert_called_once_with(str(row.id), "w1:p9")
        workers.assert_called_once_with(row, staff.StaffSnapshot(self.rows))

    def test_restart_rerun_after_director_tab(self) -> None:
        from scripts.production import worker

        holder = self.rows[0]
        child = replace(self.rows[1], kind="worker", parent=staff.ReportsTo(holder.id))
        record = staff.StaffSnapshot((holder, child))
        self.running.clear()
        shutdown_record.save([str(holder.id), str(child.id)])
        with (patch.object(staff, "initial_snapshot", return_value=record),
              patch.object(unit_runs, "worker_transport", return_value="herdr"),
              patch.object(worker, "restore", side_effect=[worker.ExitCode.UNCERTAIN, worker.ExitCode.OK]) as restore):
            self.assertEqual(restart.restart(), 0)
            self.assertEqual(shutdown_record.read(), [str(holder.id), str(child.id)])
            self.assertEqual(restart.restart(), 0)
        self.assertEqual([item.args[0] for item in restore.call_args_list], [holder.id, holder.id])
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(shutdown_record.read(), [])

    def test_restart_rerun_after_first_worker(self) -> None:
        from scripts.production import worker

        holder = self.rows[0]
        children = tuple(replace(row, kind="worker", parent=staff.ReportsTo(holder.id)) for row in self.rows[1:])
        record = staff.StaffSnapshot((holder, *children))
        resumed: set[staff.StaffId] = set()

        def restore(_parent: staff.StaffId, *, record: staff.StaffSnapshot,
                    runs: dict[str, list[str]],
                    codex: herdr.CodexConversations | herdr.CodexLaunchRead = herdr.CodexLaunchRead.CURRENT) -> worker.ExitCode:
            _ = record, runs
            self.assertIsInstance(codex, herdr.CodexConversations)
            if not resumed:
                resumed.add(children[0].id)
                raise OSError("interrupted after first worker")
            resumed.update(child.id for child in children)
            return worker.ExitCode.OK

        shutdown_record.save([str(row.id) for row in record.rows])
        with (patch.object(staff, "initial_snapshot", return_value=record),
              patch.object(unit_runs, "worker_transport", return_value="herdr"),
              patch.object(worker, "restore", side_effect=restore)):
            self.assertEqual(restart.restart(), 0)
            self.assertEqual(len(shutdown_record.read()), 3)
            self.assertEqual(restart.restart(), 0)
            self.assertEqual(restart.restart(only=(str(holder.id),)), 0)
        self.assertEqual(resumed, {child.id for child in children})
        self.assertEqual(self.launches, [])
        self.assertEqual(shutdown_record.read(), [])

        child = children[0]
        self.running.add(str(child.current_session))

        with (patch.object(staff, "initial_snapshot", return_value=record),
              patch.object(unit_runs, "worker_transport", return_value="herdr"),
              patch.object(worker_panes, "observe") as observe):
            self.assertTrue(restart.stop_row(child, herdr.Herdr("fixture")))
        observe.assert_not_called()


class RetainedSplitRestartTests(RestartTestCase):
    def test_interrupted_restart_keeps_every_original_pane_and_terminal(self) -> None:
        from scripts.direct import progress_history
        from scripts.herdr.test_worker_panes import PaneWorld
        from scripts.lib.test_models_registry import registry_text
        from scripts.production import worker
        from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_worker

        fixture = StaffTestCase()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        root = fixture.root
        config = root / "conf"
        for source in (Path(__file__).resolve().parents[2] / "config").glob("*.conf"):
            write_text_atomic(config / source.name, source.read_text())
        write_text_atomic(config / "models.conf", registry_text())
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(config),
            "MODELS_CONFIG_FILE": str(config / "models.conf"), "MODELS_CONFIG_PUBLISH": "0",
            "SHUTDOWN_STATE_DIR": str(root / "shutdown"), "XDG_STATE_HOME": str(root / "state"),
            "UNIT_RUN_DIR": str(root / "run"), "ROSTER_RUN_ROOT": str(root)})))
        showrunner = fixture_member("showrunner", name="showrunner")
        holder = fixture_member("unit", name="unit", parent=showrunner.id,
            worktree=staff.PresentWorktree("natedev", root))
        children = tuple(fixture_worker(name, holder.id, backend=staff.CodexBackend(root / "codex"))
                         for name in ("impl1", "impl2"))
        world = PaneWorld()
        for index, row in enumerate((holder, *children)):
            world.add(row, f"w1:p{index}", live=False)
        original = {pane: value["terminal_id"] for pane, value in world.locations.items()}
        entry: pane_record.PaneLaunch = {"kind": "claude", "pane_id": "w1:p0", "session_id": str(holder.id),
            "config_dir": os.environ["CLAUDE_CONFIG_DIR"], "cwd": str(root), "name": holder.name,
            "model": "", "remote_control": False, "environment": {}, "flags_read": True,
            "written_at": "2026-11-06T00:00:00+00:00"}
        pane_record.write("agents", entry)
        run = root / "run"
        write_text_atomic(run / "progress_history_state.json", json.dumps({
            "main_agent": {"session_id": str(holder.current_session)},
            "phase": {"id": "earlier", "title": "Earlier", "instance_id": "earlier-phase",
                      "started_at": 1, "status": "active"},
            "pass": {}, "history_file": str(run / "history.jsonl")}))
        write_text_atomic(run / "worker_transport", "herdr\n")
        prompt = run / "prompt.md"
        write_text_atomic(prompt, "Preserve the source behavior.\n")
        for row in children:
            _ = progress_history.begin_dispatch(run, slot=row.name, phase="earlier", role="impl",
                worker_name=row.name, worker_id=row.id, launcher_command=["bash", "assign.sh", "impl", str(prompt)],
                prompt=prompt, kind="impl", lens="")
        shutdown_record.save([str(row.id) for row in (holder, *children)])
        world.interrupt = "agent"

        def conversation(identity: str, kind: str = "", *,
                         codex: herdr.CodexConversations | None = None) -> herdr.LiveConversation | herdr.StoppedConversation:
            _ = kind, codex
            for pid, family, _arguments in world.foreground.values():
                if world.processes[pid].current_identity == identity:
                    return herdr.LiveConversation(cast(Literal["claude", "codex"], family), (pid,))
            return herdr.StoppedConversation()

        def prepare(identity: str, directory: str, environment: dict[str, str],
                    removals: frozenset[str]) -> herdr.PreparedAgentEnvironment:
            _ = identity, removals
            return herdr.PreparedAgentEnvironment(Path(directory), environment)

        def role_account(role: str) -> accounts.NamedAccount | accounts.CodexWorkersAccount:
            return (accounts.CodexWorkersAccount("codex1", root / "codex") if role == "workers" else
                    accounts.NamedAccount("claude2", Path(os.environ["CLAUDE_CONFIG_DIR"])))

        def launch(name: str, destination: herdr.NewTab | herdr.SplitPane | herdr.ExistingPane,
                   prepared: herdr.PreparedAgentEnvironment, kind: str, arguments: list[str],
                   opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt, timeout: int, *, agent: str,
                   reported_unknown: str = "", agent_name: str | herdr.AgentNameSource = herdr.AgentNameSource.MEMBER_REFERENCE,
                   codex: herdr.CodexConversations | herdr.CodexLaunchRead = herdr.CodexLaunchRead.CURRENT) -> herdr.AgentLaunched:
            _ = codex
            return world.launch(name, destination, prepared, kind, arguments, opening, timeout,
                                agent=agent, reported_unknown=reported_unknown, agent_name=agent_name)

        with (patch.object(herdr, "Herdr", return_value=world),
              patch.object(herdr, "read_conversation", side_effect=conversation),
              patch.object(herdr, "native_process", side_effect=world.native_process),
              patch.object(herdr, "_startup_processes", side_effect=world.descendants),
              patch.object(herdr, "launch_prepared_agent", side_effect=launch),
              patch.object(herdr, "prepare_session_environment", side_effect=prepare),
              patch.object(herdr, "saved_layout", return_value={"at": 1, "workspaces": []}),
              patch.object(herdr, "restore_server"),
              patch.object(world, "remember_row_tab"), patch.object(world, "sort_tabs"),
              patch.object(accounts, "role_account", side_effect=role_account),
              patch.object(accounts, "account_dir", return_value=Path(os.environ["CLAUDE_CONFIG_DIR"])),
              patch.object(restart.stop_resume, "heal_account_trust"),
              patch.object(worker, "_resume_launcher", return_value=worker.ExitCode.OK),
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.restart(), 0)
            self.assertEqual(len(shutdown_record.read()), 3)
            self.assertIn("interrupted after agent start", errors.getvalue())
            self.assertEqual(restart.restart(), 0)
        self.assertEqual(shutdown_record.read(), [])
        self.assertEqual({pane: value["terminal_id"] for pane, value in world.locations.items()}, original)
        self.assertEqual(world.launches[0], herdr.ExistingPane("w1:p0", "terminal-w1:p0"))
        self.assertCountEqual(world.launches, [herdr.ExistingPane(pane, cast(str, terminal))
                                              for pane, terminal in original.items()])
        self.assertEqual(world.closed, [])
        self.assertEqual({world.processes[pid].current_identity for pid, _family, _arguments in world.foreground.values()},
                         {row.current_session for row in (holder, *children)})
