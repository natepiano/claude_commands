"""Restart staff rows without a session journal."""
from __future__ import annotations

from scripts.shutdown import remote

import io
import math
import json
import os
import signal
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Literal, cast, override
from dataclasses import replace
from unittest.mock import call, patch

from scripts.shutdown import inventory
from scripts.herdr import herdr, pane_launch
from scripts.message import sessions
from scripts.shutdown import restart, shutdown
from scripts.shutdown.unit_staff_test_support import unit_host
from scripts.production import fake_unit_host, session_staff, staff
from scripts.accounts import accounts
from scripts.message import codex_daemon
from scripts.shutdown import shutdown_record, stop_resume, pane_record
from scripts.lib import unit_runs
from scripts.lib.atomic_file import write_text_atomic
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

    def stop(self, identity: str, *, deadline: float = math.inf) -> stop_resume.Stopped:
        _ = deadline
        row = next(row for row in self.rows if row.current_session == identity)
        self.stops.append(str(row.id))
        self.running.discard(str(row.current_session))
        return stop_resume.Stopped(str(row.current_session), 123)

    def launch(self, _name: str, destination: herdr.NewTab | herdr.SplitPane | herdr.ExistingPane,
               _prepared: herdr.PreparedAgentEnvironment, _kind: str, arguments: list[str],
               _opening: herdr.NoOpeningPrompt, _timeout: int, *, agent: str = "", reported_unknown: str = "") -> herdr.AgentLaunched:
        _ = reported_unknown
        row = next(row for row in self.rows if row.name == agent)
        self.assertEqual(arguments, ["--resume", str(row.current_session), "--name", row.name])
        self.assertIsInstance(_opening, herdr.NoOpeningPrompt)
        self.assertEqual(_prepared.directory, self.root)
        self.launches.append((agent, destination))
        self.running.add(str(row.current_session))
        return herdr.AgentLaunched(f"w1:p{self.rows.index(row)}", herdr.PromptSubmissionNotRequired())

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
                    evidence = herdr.RolloutThread(identity) if kind == "codex" else herdr.EnvironmentThread(identity)
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
                             reported_unknown: str = "") -> herdr.AgentLaunched:
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
                self.assertEqual(signalled, [990100, 990101, 990102])
                self.assertEqual([name for name, _ in self.launches], [row.name for row in self.rows])
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
                  patch("scripts.accounts.accounts.trust_sources", return_value=[("claude3", source / ".claude.json")]) as sources,
                  patch.object(herdr, "heal_claude_trust", return_value=herdr.TrustCopied(self.root,
                      target / ".claude.json", "claude3")) as trust,
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
            sources.assert_called_once_with(target, source)
            trust.assert_called_once_with(self.root, target / ".claude.json",
                                          [("claude3", source / ".claude.json")], dry_run=False)
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
                  patch.object(restart, "pane_workers", return_value=False),
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
                    expected = self.rows if stop_all else self.rows[:1]
                    self.assertEqual(errors.getvalue().splitlines(),
                        (["another-pending-row: restart anomaly: in the restart record, no fixed staff id"]
                         if stop_all and saved else []) +
                        [f"{row.name}: restart anomaly: row is locked" for row in expected])

    def test_restart_record_delta_keeps_another_runs_failed_row(self) -> None:
        self.running.clear()
        other = "another-failed-row"

        def start(_row: staff.StaffRow, _record: staff.StaffSnapshot, _client: herdr.Herdr, *,
                  present: bool, resolver: session_staff.RowResolver) -> None:
            _ = present, resolver
            shutdown_record.update(started=[], pending=[other])

        shutdown_record.save([str(self.rows[0].id), str(self.rows[1].id)])
        with patch.object(restart, "start_row", side_effect=start):
            self.assertEqual(restart.restart(only=(str(self.rows[0].id),)), 0)
        self.assertEqual(shutdown_record.read(), [str(self.rows[1].id), other])

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
                       reported_unknown: str = "") -> herdr.AgentLaunched:
            if agent == self.rows[0].name:
                raise herdr.HerdrFailure("fixture launch failed")
            return launch(name, destination, prepared, kind, arguments, opening, timeout, agent=agent,
                          reported_unknown=reported_unknown)

        with (patch.object(herdr, "launch_prepared_agent", side_effect=fail_first),
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.restart(), 0)
        self.assertEqual(shutdown_record.read(), [identities[0]])
        self.assertEqual(errors.getvalue(), "unit-0: restart anomaly: fixture launch failed\n")
        self.assertEqual([name for name, _ in self.launches], [row.name for row in self.rows[1:]])

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

    def test_restart_all_starts_each_row_in_its_saved_tab(self) -> None:
        with (patch.object(herdr.Herdr, "save_layout") as save,
              patch.object(herdr.Herdr, "remember_row_tab") as remember):
            self.assertEqual(restart.restart(stop_all=True), 0)
        save.assert_called_once()
        self.assertEqual([call.args for call in remember.call_args_list[:3]],
                         [(str(row.id), f"w1:p{n}") for n, row in enumerate(self.rows)])
        self.assertEqual(self.stops, [str(row.id) for row in self.rows])
        self.assertEqual(self.launches, [(row.name, herdr.NewTab("saved", row.name)) for row in self.rows])
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
        self.assertEqual([name for name, _ in self.launches], [row.name for row in self.rows[1:]])
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
                self.assertEqual(output.getvalue().splitlines(), [
                    f"{row.name}: {row.id}, {row.kind}, workspace live, tab {index} ({row.name})"
                    if dry_run else f"{row.name}: restarted" for index, row in enumerate(self.rows, 1)])
                self.assertEqual(self.stops, [] if dry_run else [str(row.id) for row in self.rows])
                self.assertEqual([name for name, _place in self.launches], [] if dry_run else [row.name for row in self.rows])

    def test_restart_all_rerun_starts_only_the_rows_that_failed(self) -> None:
        launch = self.launch
        def fail_one(name: str, destination: herdr.NewTab | herdr.SplitPane | herdr.ExistingPane,
                     prepared: herdr.PreparedAgentEnvironment, kind: str, arguments: list[str],
                     opening: herdr.NoOpeningPrompt, timeout: int, *, agent: str = "", reported_unknown: str = "") -> herdr.AgentLaunched:
            if agent == self.rows[1].name:
                raise OSError("fixture launch failed")
            return launch(name, destination, prepared, kind, arguments, opening, timeout, agent=agent, reported_unknown=reported_unknown)
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
                     opening: herdr.NoOpeningPrompt, timeout: int, *, agent: str = "", reported_unknown: str = "") -> herdr.AgentLaunched:
            if agent == self.rows[1].name:
                raise OSError("fixture launch failed")
            return launch(name, destination, prepared, kind, arguments, opening, timeout, agent=agent, reported_unknown=reported_unknown)

        with patch.object(herdr, "launch_prepared_agent", side_effect=fail_one):
            self.assertEqual(restart.restart(stop_all=True), 0)
        self.assertEqual(shutdown_record.read(), [str(self.rows[1].id)])
        self.assertEqual(self.stops, [str(row.id) for row in self.rows[:2]])
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
        self.assertEqual(restore.call_args_list, [call(holder.id), call(holder.id)])
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(shutdown_record.read(), [])

    def test_restart_rerun_after_first_worker(self) -> None:
        from scripts.production import worker

        holder = self.rows[0]
        children = tuple(replace(row, kind="worker", parent=staff.ReportsTo(holder.id)) for row in self.rows[1:])
        record = staff.StaffSnapshot((holder, *children))
        resumed: set[staff.StaffId] = set()

        def restore(_parent: staff.StaffId) -> worker.ExitCode:
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
        from scripts.herdr import worker_panes

        with (patch.object(staff, "initial_snapshot", return_value=record),
              patch.object(unit_runs, "worker_transport", return_value="herdr"),
              patch.object(worker_panes, "observe") as observe):
            self.assertTrue(restart.stop_row(child, herdr.Herdr("fixture")))
        observe.assert_not_called()


class RetainedSplitRestartTests(RestartTestCase):
    def test_interrupted_restart_keeps_every_original_pane_and_terminal(self) -> None:
        from scripts.herdr.test_worker_panes import PaneWorld
        from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_worker
        from scripts.lib.test_models_registry import registry_text

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
        write_text_atomic(root / "run/progress_history_state.json", json.dumps({"main_agent": {"session_id": str(holder.current_session)}, "pass": {
            row.name: {"instance_id": "dispatch-" + row.name, "worker_name": row.name,
                       "team_slot": row.name, "kind": "impl", "status": "completed"} for row in children}}))
        write_text_atomic(root / "run/worker_transport", "herdr\n")
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

        with (patch.object(herdr, "Herdr", return_value=world),
              patch.object(herdr, "read_conversation", side_effect=conversation),
              patch.object(herdr, "native_process", side_effect=world.native_process),
              patch.object(herdr, "_startup_processes", side_effect=world.descendants),
              patch.object(herdr, "launch_prepared_agent", side_effect=world.launch),
              patch.object(herdr, "prepare_session_environment", side_effect=prepare),
              patch.object(herdr, "saved_layout", return_value={"at": 1, "workspaces": []}),
              patch.object(herdr, "restore_server"),
              patch.object(world, "remember_row_tab"), patch.object(world, "sort_tabs"),
              patch.object(accounts, "role_account", side_effect=role_account),
              patch.object(accounts, "account_dir", return_value=Path(os.environ["CLAUDE_CONFIG_DIR"])),
              patch.object(restart.stop_resume, "heal_account_trust"),
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
