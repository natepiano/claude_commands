from __future__ import annotations

import io
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from contextlib import redirect_stderr, redirect_stdout
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, cast, final, override
from unittest import mock

from scripts.production.rename_test_support import FolderMoveBackend, FolderMoveFixture
from scripts.production.cli_fixture import invoke_cli
from scripts.accounts import accounts
from scripts.whoami import agent_accounts
from scripts.shutdown import restart
from scripts.production import staff, session_staff
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib import unit_runs
from scripts.shutdown import pane_record as restart_records
from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_runtime_id
from scripts.message import sessions as runtime_sessions
from scripts.production import unit_move

from scripts.shutdown import pane_restore
from scripts.herdr import herdr  # noqa: E402
from scripts.shutdown import pane_record  # noqa: E402
from scripts.herdr import pane_launch
from scripts.message import sessions  # noqa: E402

END_HOOK = Path(__file__).resolve().parent.parent / "hooks/session-end-pane-record.py"
SETTINGS = '{"disableAgentView": true}'
EXACT_RUNTIME_ID = staff.runtime_id
NATIVE_SESSION_READER = herdr.Herdr.native_session


@contextmanager
def mac_process_commands(root: Path, responses: Mapping[str, object]) -> Generator[None]:
    """Read private Mac process replies through the existing command fake."""
    executable = root / "mac-process-tools"
    source = Path(__file__).resolve().parents[1] / "herdr/fake_herdr.py"
    _ = executable.write_text(f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(source)!r}, run_name='__main__')\n")
    executable.chmod(0o755)
    response_path = root / "mac-process-responses.json"
    _ = response_path.write_text(json.dumps(responses))
    with (mock.patch.object(herdr, "_PLATFORM", "darwin"),
          mock.patch.dict(os.environ, {"HERDR_LSOF": str(executable), "HERDR_PS": str(executable),
              "HERDR_PLATFORM": "darwin", "FAKE_HERDR_RESPONSES": str(response_path),
              "FAKE_HERDR_LOG": str(root / "mac-process-log")})):
        yield


@final
class FakeClient(herdr.Herdr):
    """The herdr calls a restore makes, on a layout the test sets."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        with mock.patch.object(herdr, "herdr_binary", return_value="fixture-herdr"):
            super().__init__("agents")
        self.rows = rows
        for row in self.rows:
            _ = row.setdefault("terminal_id", str(row["pane_id"]) + "-terminal")
        self.process_info: dict[str, object] = {"shell_pid": 998042, "foreground_process_group_id": 998042,
            "foreground_processes": [{"argv": ["zsh"], "name": "zsh", "pid": 998042}]}
        self.started: list[tuple[str, str, list[str]]] = []
        self.closed: list[str] = []
        self.start_error: herdr.HerdrFailure | None = None
        self.native_ids: dict[str, str] = {}
        self.prompts: list[tuple[str, herdr.OpeningPrompt]] = []
        self.opened: list[tuple[str, str, str, str, dict[str, str]]] = []
        self.publish_claude: bool = True

    @override
    def result(self, *arguments: str, deadline: float = math.inf) -> dict[str, object]:
        del deadline
        if arguments[:2] == ("workspace", "list"):
            return {"workspaces": [{"workspace_id": "w1", "label": "demo"}]}
        if arguments[:2] == ("pane", "list"):
            return {"panes": [{"tab_id": str(row["pane_id"]) + ":tab", **row} for row in self.rows]}
        if arguments[:2] == ("tab", "list"):
            return {"tabs": [{"tab_id": row.get("tab_id", str(row["pane_id"]) + ":tab"),
                              "label": row["label"], "pane_count": 1} for row in self.rows]}
        if arguments[:2] == ("pane", "process-info"):
            return {"process_info": self.process_info}
        raise AssertionError(arguments)

    @contextmanager
    def empty_shell(self, pid: int, cwd: Path) -> Generator[None]:
        """Supply the private shell at both process observation boundaries."""
        from scripts.production.fake_unit_host import process

        process(os.environ, pid, 1, ["zsh"], {}, cwd=cwd)
        result = self.result
        program = herdr.run_program
        reply: dict[str, object] = {"process_info": {"shell_pid": pid,
            "foreground_process_group_id": pid,
            "foreground_processes": [{"pid": pid, "name": "zsh", "argv": ["zsh"]}]}}

        def observe(*arguments: str, deadline: float = math.inf) -> dict[str, object]:
            if arguments[:2] == ("pane", "process-info"):
                return reply
            return result(*arguments, deadline=deadline)

        def run(argv: list[str], timeout: float = herdr.PROGRAM_TIMEOUT_SECONDS, *,
                environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            if argv[:5] == self.argv("pane", "process-info"):
                return subprocess.CompletedProcess(argv, 0, json.dumps({"result": reply}), "")
            return program(argv, timeout, environment=environment)

        with (mock.patch.object(self, "result", side_effect=observe),
              mock.patch.object(herdr, "run_program", side_effect=run),
              mock.patch.object(herdr, "_PLATFORM", "linux")):
            yield

    @override
    def pane_activity(self, pane: str) -> herdr.PaneAtShell | herdr.PaneRunning:
        _ = pane
        return herdr.PaneAtShell()

    @override
    def start_agent(self, name: str, pane: str, kind: str, agent_arguments: list[str],
                    opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt, timeout_ms: int) -> herdr.PromptSubmissionNotRequired:
        _ = (kind, opening, timeout_ms)
        if not any(row["pane_id"] == pane for row in self.rows):
            self.rows.append({"pane_id": pane, "label": name, "workspace_id": "w1",
                              "terminal_id": pane + "-terminal"})
        if self.start_error is not None:
            raise self.start_error
        self.started.append((name, pane, agent_arguments))
        if "--resume" in agent_arguments:
            self.native_ids[pane] = agent_arguments[agent_arguments.index("--resume") + 1]
            if self.publish_claude:
                entries = pane_record.read(self.session)
                owner = staff.initial_snapshot().by_current_session(self.native_ids[pane])
                reference = owner.id if owner is not None else self.native_ids[pane]
                original = next(entry for entry in entries.values() if entry["session_id"] == reference)
                environment = {**original["environment"], "CLAUDE_CONFIG_DIR": original["config_dir"]}
                replacement = pane_record.launch(pane, self.native_ids[pane], original["cwd"],
                    environment, ["claude", *agent_arguments])
                pane_record.write(self.session, replacement)
        return herdr.PromptSubmissionNotRequired()

    @override
    def run_in_pane(self, pane: str, command: list[str], *, terminal: str = "") -> None:
        _ = pane, command, terminal

    @override
    def native_session(self, pane: str, kind: str, timeout: float, *,
                       pane_row: Mapping[str, object] | herdr.PaneNotRead = herdr.PANE_NOT_READ) -> str:
        del kind, timeout, pane_row
        if self.start_error is not None:
            raise self.start_error
        if pane not in self.native_ids:
            raise herdr.HerdrFailure(f"{pane}: fixture has no native process observation")
        return self.native_ids[pane]

    @override
    def submit_opening_prompt(self, pane: str, opening: herdr.OpeningPrompt, launched_at: datetime,
                              timeout_ms: int) -> herdr.PromptSubmitted:
        del timeout_ms
        self.prompts.append((pane, opening))
        return herdr.PromptSubmitted(launched_at)

    @override
    def _close_pane(self, pane: str) -> bool:
        self.close_tab_of(pane)
        return True

    @override
    def close_tab_of(self, pane: str) -> None:
        self.closed.append(pane)
        self.rows[:] = [row for row in self.rows if row["pane_id"] != pane]


@final
class PaneRestoreTests(unittest.TestCase):
    row = staff.StaffRow(staff.StaffId("restore-row"), "agent", "alpha", staff.StaffRoot(),
        current_session=staff.StaffId("019b3a40-4250-7000-8000-000000000102"),
        worktree=staff.PresentWorktree("natedev", Path("/work/alpha")))

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.config = Path()
        self.opened: list[tuple[str, str, str, str, dict[str, str]]] = []
        self.live: list[str] = []

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / ".claude3"
        def codex_home(label: str) -> agent_accounts.CodexHomeHoldingLogin:
            return agent_accounts.CodexHomeHoldingLogin(label, self.root / f".{label}", "private@example.test")
        _ = self.enterContext(mock.patch.object(agent_accounts, "codex_account_home", side_effect=codex_home))
        _ = self.enterContext(mock.patch.object(agent_accounts, "codex_home_role_label", return_value="codex2"))
        select_role = accounts.role_account
        def role_account(role: str) -> accounts.AccountSelection:
            return (accounts.CodexWorkersAccount("codex1", self.root / ".codex1")
                    if role == "workers" else select_role(role))
        _ = self.enterContext(mock.patch.object(accounts, "role_account", side_effect=role_account))
        configuration = self.root / "configuration"
        configuration.mkdir()
        for source in (Path(__file__).resolve().parents[2] / "config").glob("*.conf"):
            write_text_atomic(configuration / source.name, source.read_text())

        _ = self.enterContext(mock.patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((self.row,))))
        _ = self.enterContext(mock.patch("scripts.shutdown.remote.local_machine", return_value="natedev"))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(configuration)})))
        _ = self.enterContext(mock.patch.object(herdr.Herdr, "saved_row_tab", return_value=herdr.NewTab("demo", "alpha")))
        _ = self.enterContext(mock.patch.object(herdr.Herdr, "remember_row_tab"))
        from scripts.production.fake_unit_host import process
        process({"HERDR_PROC": str(self.root / "proc")}, 998042, 1, ["zsh"], {})
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"HERDR_PROC": str(self.root / "proc")})))
        _ = self.enterContext(mock.patch.object(herdr, "_PLATFORM", "linux"))
        (self.config / "projects/work").mkdir(parents=True)
        _ = (self.config / "projects/work/019b3a40-4250-7000-8000-000000000102.jsonl").write_text("{}\n")
        def no_envrc(directory: Path, environment: Mapping[str, str]) -> herdr.nix_env.NoEnvrc:
            _ = directory
            return herdr.nix_env.NoEnvrc(dict(environment))

        def session_liveness(identity: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
            return sessions.LiveSessionProcess(123) if identity in self.live else sessions.SessionNotLive()

        for patcher in (mock.patch.object(herdr.nix_env, "ensure", side_effect=no_envrc),
                        mock.patch.object(pane_record, "RECORD_DIR", self.root / "herdr-panes"),
                        mock.patch.object(restart_records, "RECORD_DIR", self.root / "herdr-panes"),
                        mock.patch.object(pane_record, "PROC", self.root / "proc"),
                        mock.patch.dict(os.environ, {"HOME": str(self.root),
                            "HERDR_PANE_RECORD_DIR": str(self.root / "herdr-panes")}),
                        mock.patch.object(sessions, "session_liveness", side_effect=session_liveness),
                        mock.patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({}, ())),
                        mock.patch.object(sessions, "running_sessions", list),
                        mock.patch.object(session_staff, "member_sources", return_value=session_staff.MemberSources(())),
                        mock.patch.object(herdr, "open_tab", self.open_tab)):
            _ = cast(object, self.enterContext(patcher))

    def live_sessions(self) -> list[dict[str, str]]:
        return [{"sessionId": session_id} for session_id in self.live]

    def open_tab(self, name: str, space: str, title: str, cwd: str, environment: dict[str, str]) -> str:
        self.opened.append((name, space, title, cwd, environment))
        return "w1:p3"

    def entry(self, **changes: object) -> None:
        if changes.get("kind") == "codex":
            changes = {"config_dir": str(self.root / ".codex2"),
                       "environment": {"CODEX_HOME": str(self.root / ".codex2")}, **changes}
        launch = pane_record.launch("w1:p2", "019b3a40-4250-7000-8000-000000000102", "/work/alpha",
                                    {"CLAUDE_CONFIG_DIR": str(self.config), "SHOWRUNNER_UNIT": "demo",
                                     "CLAUDE_CODE_OAUTH_TOKEN": "secret"},
                                    ["claude", "-n", "alpha", "--remote-control", "alpha", "--model", "opus"])
        pane_record.write("agents", cast(pane_record.PaneLaunch, cast(object, {**launch, **changes})))

    def run_restore(self, client: FakeClient) -> tuple[int, str]:
        output = io.StringIO()
        with mock.patch.object(herdr, "Herdr", return_value=client), redirect_stdout(output), redirect_stderr(output):
            code = pane_restore.restore("agents", dry_run=False)
        return code, output.getvalue()

    @staticmethod
    def restored(session_id: str = "019b3a40-4250-7000-8000-000000000102") -> FakeClient:
        return FakeClient([{"pane_id": "w1:p2", "label": "alpha", "workspace_id": "w1",
                            "agent_session": {"kind": "id", "value": session_id}}])


    def test_restore_uses_each_reader_outcome_for_both_runtimes(self) -> None:
        identity = "019b3a40-4250-7000-8000-000000000102"
        for kind in ("claude", "codex"):
            for state in ("live", "stopped", "unknown"):
                with self.subTest(kind=kind, state=state):
                    self.entry(kind=kind)
                    observed = (herdr.LiveConversation(kind, (123,)) if state == "live" else
                                herdr.UnknownConversation("test-tab (conversation unknown)") if state == "unknown" else
                                herdr.StoppedConversation())
                    client = self.restored()
                    with (mock.patch.object(herdr, "read_conversation", return_value=observed) as read,
                          mock.patch.object(client, "run_in_pane"),
                          mock.patch.object(client, "start_agent", wraps=client.start_agent) as start):
                        code, output = self.run_restore(client)
                    self.assertEqual(code, 0, output)
                    self.assertEqual(len(client.started), 0 if state == "live" else 1)
                    self.assertEqual(output.count("anomaly: test-tab"), 1 if state == "unknown" else 0)
                    read.assert_any_call(identity)
                    if state != "live":
                        self.assertEqual(start.call_args.kwargs["kind"], kind)


    def test_restore_reports_unknown_once_across_the_real_launch_boundary(self) -> None:
        for kind in ("claude", "codex"):
            for changed_reason in (False, True):
                with self.subTest(kind=kind, changed_reason=changed_reason):
                    self.entry(kind=kind)
                    client = self.restored()
                    first = herdr.UnknownConversation("test-tab (conversation unreadable)")
                    last = herdr.UnknownConversation("test-tab (new read failure)") if changed_reason else first
                    with (mock.patch.object(herdr, "read_conversation", side_effect=[first, first, last]) as read,
                          mock.patch.object(herdr, "prepare_session_environment", return_value=
                              herdr.PreparedAgentEnvironment(self.root, {})),
                          mock.patch.object(pane_launch, "finish_restore") as finish):
                        code, output = self.run_restore(client)
                    self.assertEqual(code, 0, output)
                    self.assertEqual(read.call_count, 3)
                    self.assertEqual(len(client.started), 1)
                    self.assertEqual(output.count("anomaly: test-tab (conversation unreadable)"), 1)
                    self.assertEqual(output.count("anomaly: test-tab (new read failure)"), 0)
                    self.assertEqual(output.count("anomaly: test-tab"), 1)
                    finish.assert_not_called()

    def test_restore_keeps_the_original_tab_when_it_gains_other_work(self) -> None:
        for other_conversation in (False, True):
            with self.subTest(other_conversation=other_conversation):
                self.opened.clear()
                self.entry(cwd="/old/alpha")
                client = FakeClient([{ "pane_id": "w1:p2", "label": "alpha", "workspace_id": "w1"}])
                activity = herdr.PaneRunning(("other work",)) if not other_conversation else herdr.PaneAtShell()

                def fresh_activity(pane: str) -> herdr.PaneAtShell | herdr.PaneRunning:
                    self.assertEqual(pane, "w1:p2")
                    if not client.started:
                        return herdr.PaneAtShell()
                    return activity

                original_start = client.start_agent

                def start(name: str, pane: str, kind: str, agent_arguments: list[str],
                          opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt,
                          timeout_ms: int) -> herdr.PromptSubmissionNotRequired:
                    result = original_start(name, pane, kind, agent_arguments, opening, timeout_ms)
                    if other_conversation:
                        client.rows[0]["agent_session"] = {"kind": "id", "value": "another-conversation"}
                    return result

                with (mock.patch.object(client, "pane_activity", side_effect=fresh_activity),
                      mock.patch.object(client, "start_agent", side_effect=start)):
                    code, output = self.run_restore(client)
                self.assertEqual(code, 0, output)
                self.assertEqual(len(client.started), 1)
                self.assertEqual(client.closed, [])
                self.assertIn("pane close anomaly:", output)
                self.assertEqual(output.count("pane close anomaly:"), 1)
                self.assertIn("w1:p2", pane_record.read("agents"))

    def test_server_restore_keeps_numbered_and_unnamed_codex_homes_through_start_row(self) -> None:
        for name, label in ((".codex2", "codex2"), ("saved-home", "~/.codex")):
            with self.subTest(home=name):
                saved = self.root / name
                self.entry(kind="codex", config_dir=str(saved), environment={"CODEX_HOME": str(saved)})
                client = self.restored()
                with (mock.patch.object(agent_accounts, "codex_home_role_label", return_value=label),
                      mock.patch.object(agent_accounts, "codex_account_home",
                          return_value=agent_accounts.CodexHomeHoldingLogin("codex2", saved, "private@example.test"),
                          side_effect=ValueError("no numbered note") if name == "saved-home" else None) as numbered,
                      mock.patch.object(accounts, "role_account", side_effect=AssertionError("keep the saved home")),
                      mock.patch.object(restart, "start_row", wraps=restart.start_row) as start,
                      mock.patch.object(herdr, "prepare_session_environment", wraps=herdr.prepare_session_environment) as prepare,
                      mock.patch.object(client, "run_in_pane")):
                    code, output = self.run_restore(client)
                self.assertEqual(code, 0, output)
                self.assertEqual(prepare.call_args.args[2]["CODEX_HOME"], str(saved))
                self.assertEqual(start.call_args.kwargs["target_account"], accounts.CodexWorkersAccount(label, saved))
                numbered.assert_not_called()

    def test_restore_of_a_unit_uses_the_saved_account(self) -> None:
        self.row = replace(self.row, kind="unit")
        record = staff.StaffSnapshot((self.row,))
        with mock.patch.object(staff, "initial_snapshot", return_value=record):
            self.entry()
            client = self.restored()
            with (mock.patch.object(accounts, "accounts_home", return_value=self.root),
                  mock.patch.object(accounts, "role_account",
                      return_value=accounts.NamedAccount("claude4", self.root / ".claude4")) as role,
                  mock.patch.object(herdr, "prepare_session_environment", wraps=herdr.prepare_session_environment) as prepare,
                  mock.patch.object(client, "run_in_pane")):
                code, output = self.run_restore(client)
        self.assertEqual(code, 0, output)
        role.assert_not_called()
        self.assertEqual(len(client.started), 1)
        self.assertEqual(prepare.call_args.args[2]["CLAUDE_CONFIG_DIR"], str(self.config))

    def test_restore_resumes_a_saved_entry_without_a_staff_row(self) -> None:
        with mock.patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot(())):
            self.entry()
            client = self.restored()
            with client.empty_shell(998042, self.root):
                code, output = self.run_restore(client)
        self.assertEqual(code, 0, output)
        self.assertEqual(len(client.started), 1)
        self.assertEqual(client.closed, ["w1:p2"], output)

    def test_restore_reuses_worker_shell(self) -> None:
        from scripts.direct import progress_history
        from scripts.herdr import test_worker_panes, worker_panes
        from scripts.production import worker

        fixture = test_worker_panes.WorkerPaneTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        _ = self.enterContext(mock.patch.object(staff, "initial_snapshot", side_effect=staff.snapshot))
        row = fixture.worker(family="codex")
        fixture.world.add(row, "w1:p1", live=False, family="codex")
        run = fixture.run_state()
        prompt = run / "prompt.md"
        write_text_atomic(prompt, "Preserve the source behavior.\n")
        _ = progress_history.begin_dispatch(run, slot=row.name, phase="earlier", role="impl",
            worker_name=row.name, worker_id=row.id, launcher_command=["bash", "assign.sh", "impl", str(prompt)],
            prompt=prompt, kind="impl", lens="")
        pane_record.write("agents", pane_record.launch("w1:p1", str(row.current_session), str(fixture.root),
            {"CODEX_HOME": str(fixture.root / "codex")}, ["codex", "resume", str(row.current_session)]))
        before = fixture.world.panes()
        with (mock.patch.object(herdr, "read_conversation", return_value=herdr.LiveConversation("claude", (123,))),
              mock.patch.object(worker, "_resume_launcher", return_value=worker.ExitCode.OK),
              redirect_stdout(io.StringIO()) as output, redirect_stderr(output)):
            self.assertEqual(pane_restore.restore("agents", dry_run=False), 0, output.getvalue())
            self.assertEqual(pane_restore.restore("agents", dry_run=False), 0, output.getvalue())
        self.assertEqual(fixture.world.launches, [herdr.ExistingPane("w1:p1", "terminal-w1:p1")])
        self.assertEqual([(pane["pane_id"], pane.get("terminal_id")) for pane in before],
                         [(pane["pane_id"], pane.get("terminal_id")) for pane in fixture.world.panes()])
        self.assertIsInstance(fixture.observation(row), worker_panes.OpenWorker)
        self.assertEqual(staff.snapshot().by_id(row.id), row)


    def test_seed_writes_the_entry_of_each_running_claude(self) -> None:
        (self.root / "proc/4242").mkdir(parents=True)
        _ = (self.root / "proc/4242/environ").write_bytes(
            f"CLAUDE_CONFIG_DIR={self.config}\0SHOWRUNNER_UNIT=demo\0CLAUDE_CODE_OAUTH_TOKEN=secret\0".encode())
        (self.config / "sessions").mkdir()
        _ = (self.config / "sessions/4242.json").write_text(json.dumps({"pid": 4242, "sessionId": "019b3a40-4250-7000-8000-000000000102"}))
        client = FakeClient([{"pane_id": "w1:p2", "label": "alpha", "workspace_id": "w1"}])
        client.process_info = {"shell_pid": 998042, "foreground_process_group_id": 4242,
            "foreground_processes": [{"pid": 4242, "name": "claude",
                "argv": ["claude", "-n", "alpha", "--model", "opus"], "cwd": "/work/alpha"}]}
        with (mock.patch.object(herdr, "Herdr", return_value=client),
              mock.patch.object(sessions, "process_start", return_value="fixture-generation"),
              redirect_stdout(io.StringIO())):
            self.assertEqual(pane_record.seed("agents"), 0)
        entry = pane_record.read("agents")["w1:p2"]
        self.assertEqual((entry["session_id"], entry["config_dir"], entry["name"], entry["model"], entry["cwd"]),
                         (str(self.row.id), str(self.config), "alpha", "opus", "/work/alpha"))
        self.assertEqual(entry["environment"], {})


@final
class RestoreStaffNamesTests(StaffTestCase):
    def saved_batch(self, *, machine: Literal["natedev", "mac"] = "natedev") -> tuple[Path, FakeClient]:
        parent, unit, worker = self.tree()
        unit = staff.set_worktree(unit.id, staff.PresentWorktree(machine, self.root), expected=unit.worktree)
        configuration = self.root / "configuration"
        for config_source in (Path(__file__).resolve().parents[2] / "config").glob("*.conf"):
            write_text_atomic(configuration / config_source.name, config_source.read_text())
        write_text_atomic(self.root / "run/worker_transport", "mesh\n")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "CONF_DIR": str(configuration), "UNIT_RUN_DIR": str(self.root / "run"),
            "HERDR_PROC": str(self.root / "proc"), "XDG_STATE_HOME": str(self.root / "state"),
        })))
        _ = self.enterContext(mock.patch.object(unit_runs, "RUN_ROOT", self.root / "unit"))
        _ = self.enterContext(mock.patch.object(unit_runs, "FORMER_RUN_ROOT", self.root / "delegate"))
        def no_envrc(directory: Path, environment: Mapping[str, str]) -> herdr.nix_env.NoEnvrc:
            _ = directory
            return herdr.nix_env.NoEnvrc(dict(environment))

        def saved_tab(identity: str, name: str, fallback_workspace: str) -> herdr.NewTab:
            _ = identity, fallback_workspace
            return herdr.NewTab("demo", name)

        _ = self.enterContext(mock.patch.object(herdr.nix_env, "ensure", side_effect=no_envrc))
        _ = self.enterContext(mock.patch.object(restart_records, "RECORD_DIR", self.root / "saved-panes"))
        _ = self.enterContext(mock.patch.object(accounts, "accounts_home", return_value=self.root))
        _ = self.enterContext(mock.patch("scripts.shutdown.remote.local_machine", return_value=machine))
        _ = self.enterContext(mock.patch.object(herdr.Herdr, "saved_row_tab",
            side_effect=saved_tab))
        _ = self.enterContext(mock.patch.object(herdr.Herdr, "remember_row_tab"))
        _ = self.enterContext(mock.patch.object(FakeClient, "run_in_pane"))
        unit = staff.update_current_session(unit.current_session, staff.native_id(fixture_runtime_id("unit-after-clear")))
        unit = staff.rename(unit.id, "renamed")
        worker = staff.update_current_session(worker.current_session, staff.native_id(fixture_runtime_id("worker-after-clear")))
        _ = fixture_member("unit", name="models", parent=parent.id, runtime_id=fixture_runtime_id("decoy"))
        missing = fixture_runtime_id("missing-row")
        directory = self.root / "saved-panes"
        config = self.root / ".claude3"
        (config / "projects/work").mkdir(parents=True)
        entries: dict[str, pane_record.PaneLaunch] = {}
        rows: list[dict[str, object]] = []
        for index, (reference, current, name) in enumerate((
            (unit.id, unit.current_session, "models"),
            (worker.id, worker.current_session, "old-worker-label"),
            (missing, missing, "models"),
        ), start=1):
            pane = f"w1:p{index}"
            entries[pane] = pane_record.launch(pane, reference, str(self.root),
                {"CLAUDE_CONFIG_DIR": str(config)}, ["claude", "-n", name])
            _ = (config / "projects/work" / f"{current}.jsonl").write_text("{}\n")
            rows.append({"pane_id": pane, "label": name, "workspace_id": "w1",
                         "agent_session": {"kind": "id", "value": current}})
        with mock.patch.object(pane_record, "RECORD_DIR", directory):
            pane_record.save("agents", entries)
        return directory, FakeClient(rows)

    def test_pane_restore_locks_the_row_before_it_reads_the_current_session(self) -> None:
        directory, client = self.saved_batch()
        for pane in client.rows:
            _ = pane.pop("agent_session", None)
        active: list[str] = []
        launches: list[str] = []
        record = staff.snapshot()
        row_operation = staff.row_operation
        expected: dict[str, str] = {}

        @contextmanager
        def lock(reference: str) -> Generator[None]:
            owner = record.by_member_reference(reference)
            if owner is not None:
                self.assertEqual(reference, owner.id)
                changed = staff.native_id(fixture_runtime_id(f"locked-{owner.id}"))
                with mock.patch.object(staff, "row_operation", row_operation):
                    _ = staff.update_current_session(owner.current_session, changed)
                expected[reference] = str(changed)
                write_text_atomic(self.root / ".claude3/projects/work" / f"{changed}.jsonl", "{}\n")
            else:
                expected[reference] = reference
            active.append(reference)
            try:
                yield
            finally:
                active.remove(reference)

        original_start = client.start_agent

        def start(name: str, pane: str, kind: str, agent_arguments: list[str],
                  opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt,
                  timeout_ms: int) -> herdr.PromptSubmissionNotRequired:
            identity = agent_arguments[agent_arguments.index("--resume") + 1]
            self.assertEqual(len(active), 1)
            self.assertEqual(identity, expected[active[0]])
            launches.append(identity)
            return original_start(name, pane, kind, agent_arguments, opening, timeout_ms)

        with (mock.patch.object(pane_record, "RECORD_DIR", directory),
              mock.patch.object(herdr, "Herdr", return_value=client),
              client.empty_shell(998042, self.root),
              mock.patch.object(staff, "row_operation", side_effect=lock),
              mock.patch.object(herdr, "open_tab", side_effect=(f"w1:p{n}" for n in range(4, 10))),
              mock.patch.object(client, "start_agent", side_effect=start),
              mock.patch.object(runtime_sessions, "session_liveness", return_value=runtime_sessions.SessionNotLive()),
              mock.patch.object(sessions, "running_sessions", return_value=[]),
              redirect_stdout(io.StringIO()) as output, redirect_stderr(output)):
            self.assertEqual(pane_restore.restore("agents", dry_run=False), 0, output.getvalue())
        self.assertEqual(len(launches), 3)
        self.assertEqual(active, [])


    def test_mac_restore_reads_the_owner_once_and_never_selects_a_row_by_saved_name(self) -> None:
        directory, client = self.saved_batch()
        from scripts.production import session_staff

        output = io.StringIO()
        with (self.machine("mac"), mock.patch.object(pane_record, "RECORD_DIR", directory),
              mock.patch.object(herdr, "Herdr", return_value=client),
              mock.patch.object(runtime_sessions, "session_liveness", return_value=runtime_sessions.SessionNotLive()),
              mock.patch.object(sessions, "running_sessions", return_value=[]),
              mock.patch.object(session_staff, "member_sources", return_value=session_staff.MemberSources(())),
              redirect_stdout(output), redirect_stderr(output)):
            self.assertEqual(pane_restore.restore("agents", dry_run=True), 0)
        commands = self.ssh_commands()
        self.assertEqual(len(commands), 1)
        self.assertIn("--snapshot", commands[0])
        self.assertEqual(output.getvalue().count("no staff row"), 1)
        self.assertIn(fixture_runtime_id("missing-row"), output.getvalue())
        self.assertEqual(output.getvalue().count(": resume "), 3)
        self.assertIn("w1:p1 renamed: resume", output.getvalue())
        self.assertIn("w1:p2 impl1: resume", output.getvalue())
        self.assertIn("w1:p3 models: resume", output.getvalue())
        self.assertEqual(client.started, [])

    def test_unreadable_mac_owner_keeps_each_saved_name_and_reports_one_problem(self) -> None:
        directory, client = self.saved_batch()
        output = io.StringIO()
        before = (directory / "agents.json").read_bytes()
        with (self.machine("mac"), mock.patch.dict(os.environ, {"STAFF_TEST_SSH_FAIL": "1"}),
              mock.patch.object(pane_record, "RECORD_DIR", directory),
              mock.patch.object(herdr, "Herdr", return_value=client),
              mock.patch.object(runtime_sessions, "session_liveness", return_value=runtime_sessions.SessionNotLive()),
              mock.patch.object(sessions, "running_sessions", return_value=[]),
              redirect_stdout(output), redirect_stderr(output)):
            self.assertEqual(pane_restore.restore("agents", dry_run=True), 0)
        self.assertEqual(len(self.ssh_commands()), 1)
        self.assertEqual(output.getvalue().count("staff is unavailable"), 1)
        self.assertNotIn("no staff row", output.getvalue())
        self.assertEqual((directory / "agents.json").read_bytes(), before)
        self.assertEqual(client.started, [])

    def test_mac_restore_reads_each_row_under_its_lock_before_launch(self) -> None:
        from scripts.herdr.test_herdr import TitledLayoutHerdr
        from scripts.production import session_staff

        directory, saved_client = self.saved_batch(machine="mac")
        client = TitledLayoutHerdr()
        client.session = "agents"
        client.spaces = [("w1", "demo")]
        client.tabs = [{"tab_id": f"saved-t{index}", "pane_id": str(row["pane_id"]),
                        "workspace_id": "w1", "label": str(row["label"]),
                        "terminal_id": str(row["terminal_id"])}
                       for index, row in enumerate(saved_client.rows)]
        client.count = 20
        record = staff.snapshot()
        path = directory / "agents.json"
        entries = pane_launch.read_strict(path)
        for entry in entries.values():
            if record.by_member_reference(entry["session_id"]) is not None:
                entry["cwd"] = str(self.root / "previous-folder")
        pane_launch.save(path, entries)
        identities: dict[str, str] = {}
        started: list[str] = []
        config = self.root / ".claude3"

        def start(name: str, pane: str, kind: str, agent_arguments: list[str],
                  opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt,
                  timeout_ms: int) -> herdr.PromptSubmissionNotRequired:
            _ = kind, opening, timeout_ms
            identity = agent_arguments[agent_arguments.index("--resume") + 1]
            identities[pane] = identity
            started.append(name)
            replacement = pane_record.launch(pane, identity, str(self.root),
                {"CLAUDE_CONFIG_DIR": str(config)}, ["claude", *agent_arguments])
            owner = record.by_current_session(identity)
            if owner is not None:
                replacement["session_id"] = owner.id
            entries = pane_record.read("agents")
            entries[pane] = replacement
            pane_record.save("agents", entries)
            return herdr.PromptSubmissionNotRequired()

        def native(pane: str, kind: str, timeout: float) -> str:
            _ = kind, timeout
            return identities[pane]

        def no_envrc(directory: Path, environment: Mapping[str, str]) -> herdr.nix_env.NoEnvrc:
            _ = directory
            return herdr.nix_env.NoEnvrc(dict(environment))

        assign_session = client.assign_session

        def assign_local_session(tab: dict[str, str], name: str) -> None:
            with mock.patch.dict(os.environ, {"STAFF_FILE": str(self.staff_file)}):
                assign_session(tab, name)

        run_command = client.run

        def run(*arguments: str, deadline: float = math.inf) -> str:
            if arguments[:2] == ("pane", "close"):
                client.tabs[:] = [tab for tab in client.tabs if tab["pane_id"] != arguments[2]]
                return "{}"
            return run_command(*arguments, deadline=deadline)

        output = io.StringIO()
        uid = os.getuid()
        process_responses = {
            "-axo pid=,ppid=,state=,lstart=": {"stdout": "42 1 S Mon Nov 2 10:00:00 2026", "code": 0},
            f"-U {uid} -o pid=,ppid=,uid=,comm=": {"stdout": f"42 1 {uid} zsh", "code": 0},
            f"-U {uid} -o pid=,command=": {"stdout": "42 zsh", "code": 0},
            "-o tpgid= -p 42": {"stdout": "42", "code": 0},
        }
        with (self.machine("mac"), mock.patch.object(pane_record, "RECORD_DIR", directory),
              mock.patch.object(restart_records, "RECORD_DIR", directory),
              mock.patch.dict(os.environ, {"HERDR_PANE_RECORD_DIR": str(directory)}),
              mac_process_commands(self.root, process_responses),
              mock.patch.object(herdr, "Herdr", return_value=client),
              mock.patch.object(herdr, "herdr_processes", return_value=[]),
              mock.patch.object(client, "order_tabs", wraps=client.order_tabs) as order,
              mock.patch.object(client, "assign_session", side_effect=assign_local_session),
              mock.patch.object(client, "run", side_effect=run),
              mock.patch.object(herdr.nix_env, "ensure", side_effect=no_envrc),
              mock.patch.object(client, "start_agent", side_effect=start),
              mock.patch.object(client, "native_session", side_effect=native),
              mock.patch.object(runtime_sessions, "session_liveness", return_value=runtime_sessions.SessionNotLive()),
              mock.patch.object(sessions, "running_sessions", return_value=[]),
              mock.patch.object(session_staff, "member_sources", return_value=session_staff.MemberSources(())),
              redirect_stdout(output), redirect_stderr(output)):
            self.assertEqual(pane_restore.restore("agents", dry_run=False), 0, output.getvalue())
            self.assertEqual(len(pane_record.read("agents")), 3)
        self.assertEqual(len(self.ssh_commands()), 6)
        self.assertTrue(all("--snapshot" in command for command in self.ssh_commands()))
        self.assertEqual(order.call_count, 3)
        self.assertEqual(started, ["renamed", "impl1", "models"])
        self.assertEqual(sorted(title for _, titles in client.layout() for title in titles),
                         ["impl1", "models", "renamed"])


class CodexPaneRestoreTests(FolderMoveFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        def codex_home(label: str) -> agent_accounts.CodexHomeHoldingLogin:
            return agent_accounts.CodexHomeHoldingLogin(label, self.root / "home" / f".{label}", "private@example.test")
        _ = self.enterContext(mock.patch.object(agent_accounts, "codex_account_home", side_effect=codex_home))
        _ = self.enterContext(mock.patch.object(agent_accounts, "codex_home_role_label", return_value="codex2"))
        select_role = accounts.role_account
        def role_account(role: str) -> accounts.AccountSelection:
            return (accounts.CodexWorkersAccount("codex2", self.root / "home/.codex2")
                    if role == "workers" else select_role(role))
        _ = self.enterContext(mock.patch.object(accounts, "role_account", side_effect=role_account))
        configuration = self.root / "configuration"
        configuration.mkdir()
        for source in (Path(__file__).resolve().parents[2] / "config").glob("*.conf"):
            write_text_atomic(configuration / source.name, source.read_text())
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(configuration)})))
        _ = self.enterContext(mock.patch.object(herdr.Herdr, "remember_row_tab"))



    @contextmanager
    def replaced_codex_process(self, backend: FolderMoveBackend, *,
                               rollout: Literal["current", "unreadable"] = "current") -> Generator[None]:
        """Keep the old environment identity while the open rollout proves the current thread."""
        proc = self.root / "replaced-codex-proc"
        process = proc / str(backend.pid)
        descriptors = process / "fd"
        descriptors.mkdir(parents=True)
        transcript = backend.config / "sessions" / f"rollout-{self.owner.current_session}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        _ = transcript.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": self.owner.current_session}}) + "\n")
        (descriptors / "7").symlink_to(transcript)
        _ = (process / "cmdline").write_bytes(f"codex\0resume\0{self.owner.current_session}\0".encode())
        _ = (process / "environ").write_bytes(
            f"CODEX_THREAD_ID={self.owner.id}\0CODEX_HOME={backend.config}\0".encode())
        if rollout == "unreadable":
            (descriptors / "7").unlink()
            descriptors.rmdir()
            _ = descriptors.write_text("descriptor observation is unavailable\n")

        def environment(pid: int) -> dict[str, str]:
            self.assertEqual(pid, backend.pid)
            return {"CODEX_HOME": str(backend.config), "CODEX_THREAD_ID":
                    self.owner.current_session if backend.starts else self.owner.id}

        publish = backend.publish_process_state

        def publish_process_state() -> None:
            publish()
            _ = (process / "cmdline").write_bytes(
                f"codex\0resume\0{self.owner.current_session}\0".encode() if backend.live else b"")
            if rollout == "current":
                descriptor = descriptors / "7"
                descriptor.unlink(missing_ok=True)
                if backend.live:
                    descriptor.symlink_to(transcript)

        with (mock.patch.dict(os.environ, {"HERDR_PROC": str(proc)}),
              mock.patch.object(backend, "publish_process_state", side_effect=publish_process_state),
              mock.patch.object(backend, "environment", side_effect=environment),
              mock.patch.object(herdr, "process_environment", side_effect=environment)):
            yield

    @contextmanager
    def retirement_evidence(self, backend: FolderMoveBackend, client: FakeClient, *,
                            current_pane: str = "w1:p3", stale_agent: bool = False,
                            shared_tab: bool = False, stopped: bool = False,
                            rollout: bool = True, exported_claude_id: bool = True) -> Generator[None]:
        """Supply fresh process evidence for a replacement and its old shell."""
        for row in client.rows:
            pane = cast(str, row["pane_id"])
            row.update(tab_id=pane.replace(":p", ":t"), terminal_id=pane + "-terminal")
        if shared_tab:
            client.rows.append({"pane_id": "w1:p9", "tab_id": "w1:t1", "terminal_id": "other-terminal",
                                "workspace_id": "w1", "label": "unrelated"})
        proc = self.root / "retirement-proc"
        descriptors = proc / str(backend.pid) / "fd"
        descriptors.mkdir(parents=True, exist_ok=True)
        arguments = ["zsh"] if stopped else [backend.kind]
        if arguments == ["codex"]:
            arguments.extend(["resume", self.owner.current_session])
        _ = (descriptors.parent / "cmdline").write_bytes(("\0".join(arguments) + "\0").encode())
        shell_task = proc / str(backend.shell_pid) / "task" / str(backend.shell_pid)
        shell_task.mkdir(parents=True, exist_ok=True)
        _ = (shell_task / "children").write_text("")
        _ = (shell_task.parent.parent / "cmdline").write_bytes(b"zsh\0")
        shell_fields = ["S", "1", str(backend.shell_pid), str(backend.shell_pid), "0", str(backend.shell_pid),
                        *(["0"] * 13), "100"]
        _ = (shell_task.parent.parent / "stat").write_text(
            f"{backend.shell_pid} (zsh) {' '.join(shell_fields)}\n")
        transcript = backend.config / f"rollout-{self.owner.current_session}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        _ = transcript.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": self.owner.current_session}}) + "\n")
        if rollout:
            (descriptors / "7").symlink_to(transcript)
        registry = runtime_sessions.sessions_dir() / f"{backend.pid}.json"
        _ = registry.write_text(json.dumps({"pid": backend.pid, "procStart": "retirement-start",
            "sessionId": self.owner.current_session, "name": "kept-name", "updatedAt": 1,
            "messagingSocketPath": str(self.root / "registry.sock")}))
        original_result = client.result
        original_run = herdr.run_program

        def result(*arguments: str, deadline: float = math.inf) -> dict[str, object]:
            if arguments[:2] != ("pane", "process-info"):
                return original_result(*arguments, deadline=deadline)
            pane = arguments[-1]
            kind = backend.kind if pane == current_pane and not stopped else (
                "claude" if stale_agent and pane == "w1:p1" else "zsh")
            pid = backend.shell_pid if kind == "zsh" else backend.pid if pane == current_pane else backend.pid + 1
            return {"process_info": {"shell_pid": backend.shell_pid, "foreground_process_group_id": pid,
                "foreground_processes": [{"pid": pid, "name": kind, "argv": [kind], "cwd": str(self.new_folder)}]}}

        def activity(pane: str) -> herdr.PaneAtShell | herdr.PaneRunning:
            if (pane == current_pane and not stopped) or (pane == "w1:p1" and stale_agent):
                return herdr.PaneRunning((backend.kind,))
            return herdr.PaneAtShell()

        def environment(pid: int) -> dict[str, str]:
            values = {"CODEX_THREAD_ID": self.owner.id, "CODEX_HOME": str(backend.config)}
            if exported_claude_id:
                values["CLAUDE_CODE_SESSION_ID"] = self.owner.current_session if pid == backend.pid else self.owner.id
            return values

        def alive(pid: int, signal: int) -> None:
            self.assertEqual((pid, signal), (backend.pid, 0))

        def native_session(pane: str, kind: str, timeout: float) -> str:
            return NATIVE_SESSION_READER(client, pane, kind, timeout)

        def run(argv: list[str], timeout: float = 10, *,
                environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            if argv[:5] == [client.binary, "--session", "agents", "pane", "process-info"]:
                payload = client.result(*argv[3:])
                return subprocess.CompletedProcess(argv, 0, json.dumps({"result": payload}), "")
            return original_run(argv, timeout, environment=environment)

        with (mock.patch.object(herdr, "Herdr", return_value=client),
              mock.patch.object(client, "result", side_effect=result),
              mock.patch.object(client, "pane_activity", side_effect=activity),
              mock.patch.object(client, "native_session", side_effect=native_session),
              mock.patch.object(client, "startup_processes", return_value=herdr.PaneStartupObservation(
                  herdr.PaneShell(backend.shell_pid), herdr.PaneAtShell())),
              mock.patch.object(herdr, "process_environment", side_effect=environment),
              mock.patch.object(herdr, "run_program", side_effect=run),
              mock.patch.object(staff, "runtime_id", side_effect=EXACT_RUNTIME_ID),
              mock.patch.object(os, "kill", side_effect=alive),
              mock.patch.object(runtime_sessions, "process_start", return_value="retirement-start"),
              mock.patch.dict(os.environ, {"HERDR_PROC": str(proc)})):
            yield

    def finished_pair(self, kind: Literal["claude", "codex"]) -> tuple[FolderMoveBackend, FakeClient]:
        backend = self.folder_backend(kind, live=False)
        arguments = ["move-folder", "--unit", self.owner.id, "--to", str(self.new_folder)]
        moved = invoke_cli(unit_move, lambda: unit_move.main(arguments), arguments, dict(os.environ), self.checkout)
        self.assertEqual(moved.returncode, 0, moved.stderr)
        original = pane_record.read("agents")["w1:p1"]
        original["ended"] = False
        pane_record.write("agents", original)
        replacement = original.copy()
        replacement["pane_id"] = "w1:p3"
        pane_record.write("agents", replacement)
        client = FakeClient([{"pane_id": pane, "workspace_id": "w1", "label": "kept-name"}
                             for pane in ("w1:p1", "w1:p3")])
        path = pane_record.record_path("agents")
        attempt = pane_launch.resume_attempt(path, "w1:p3")
        proof = pane_launch.VerifiedResume(replacement, frozenset({self.owner.id, self.owner.current_session}))
        with self.assertRaisesRegex(herdr.HerdrFailure, "forced close failure"):
            pane_launch.finish_restore(path, original, attempt, proof,
                close=mock.Mock(side_effect=herdr.HerdrFailure("forced close failure")))
        return backend, client

    def finished_move_rerun(self) -> subprocess.CompletedProcess[str]:
        arguments = ["move-folder", "--unit", self.owner.id, "--to", str(self.new_folder)]
        return invoke_cli(unit_move, lambda: unit_move.main(arguments), arguments, dict(os.environ), self.checkout)


    def test_restore_repair_proves_claude_from_its_exact_registry_without_an_exported_id(self) -> None:
        backend, client = self.finished_pair("claude")
        with (self.retirement_evidence(backend, client, exported_claude_id=False),
              mock.patch.object(runtime_sessions, "session_liveness", return_value=runtime_sessions.LiveSessionProcess(backend.pid)),
              mock.patch.object(sessions, "running_sessions", return_value=[]),
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            result = pane_restore.restore("agents", dry_run=False)
        self.assertEqual(result, 0)
        self.assertEqual(client.closed, ["w1:p1"])
        self.assertEqual(client.started, [])
        entries = [entry for entry in pane_record.read("agents").values() if entry["session_id"] == self.owner.id]
        self.assertEqual([entry["pane_id"] for entry in entries], ["w1:p3"])


    def test_restore_liveness_uses_the_replaced_codex_thread_running_outside_herdr(self) -> None:
        backend = self.folder_backend("codex", live=True)
        client = FakeClient([{"pane_id": "w1:p1", "label": "kept-name", "workspace_id": "w1",
                              "agent_session": {"kind": "id", "value": self.owner.current_session}}])
        output = io.StringIO()
        with (self.replaced_codex_process(backend),
              mock.patch.object(herdr, "Herdr", return_value=client),
              mock.patch.object(runtime_sessions, "session_liveness", return_value=runtime_sessions.SessionNotLive()),
              mock.patch.object(sessions, "running_sessions", return_value=[]),
              redirect_stdout(output), redirect_stderr(output)):
            self.assertEqual(herdr.running_codex_threads(thread_host=lambda _argv: False),
                             {self.owner.current_session})
            result = pane_restore.restore("agents", dry_run=False)
        self.assertEqual(result, 0, output.getvalue())
        self.assertIn("unchanged", output.getvalue())
        self.assertEqual(client.started, [])
        self.assertEqual(client.closed, [])


    def test_restore_rerun_repairs_a_closed_tab_when_retirement_cannot_be_saved(self) -> None:
        backend, _transcript = self.moved_codex()
        save = pane_launch.save

        def refuse_retirement(path: Path, entries: dict[str, pane_record.PaneLaunch]) -> None:
            if "w1:p1" not in entries:
                raise OSError("forced retirement write failure")
            save(path, entries)

        with self.codex_restore(backend) as (client, _proof):
            with (mock.patch.object(pane_launch, "save", side_effect=refuse_retirement),
                  redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
                self.assertEqual(pane_restore.restore("agents", dry_run=False), 0)
        self.assertEqual(client.closed, ["w1:p1"])
        self.assertIn("w1:p1", pane_record.read("agents"))
        with (self.retirement_evidence(backend, client),
              mock.patch.object(runtime_sessions, "session_liveness", return_value=runtime_sessions.LiveSessionProcess(backend.pid)),
              mock.patch.object(sessions, "running_sessions", return_value=[]),
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            result = pane_restore.restore("agents", dry_run=False)
        self.assertEqual(result, 0)
        self.assertEqual(len(client.started), 1)
        self.assertNotIn("w1:p1", pane_record.read("agents"))

    def test_restore_rerun_closes_the_extra_shell_after_a_failed_close(self) -> None:
        backend, _transcript = self.moved_codex()
        with self.codex_restore(backend) as (client, _proof):
            with (mock.patch.object(client, "close_tab_of", side_effect=herdr.HerdrFailure("forced close failure")),
                  redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
                self.assertEqual(pane_restore.restore("agents", dry_run=False), 0)
        self.assertIn("w1:p1", pane_record.read("agents"))
        with (self.retirement_evidence(backend, client),
              mock.patch.object(runtime_sessions, "session_liveness", return_value=runtime_sessions.LiveSessionProcess(backend.pid)),
              mock.patch.object(sessions, "running_sessions", return_value=[]),
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            result = pane_restore.restore("agents", dry_run=False)
        self.assertEqual(result, 0)
        self.assertEqual(client.closed, ["w1:p1"])
        self.assertEqual(len(client.started), 1)
        self.assertNotIn("w1:p1", pane_record.read("agents"))

    def test_missing_pane_recovery_retires_its_source_before_the_next_folder_move(self) -> None:
        from scripts.accounts import accounts
        from scripts.shutdown import stop_resume

        backend = self.folder_backend("claude", live=False)
        original = pane_record.read("agents")["w1:p1"]
        original["ended"] = False
        pane_record.write("agents", original)
        client = FakeClient([])

        def open_tab(_space: str, name: str, cwd: str, _environment: dict[str, str]) -> str:
            self.assertEqual(cwd, str(self.old_folder))
            client.rows.append({"pane_id": "w1:p3", "workspace_id": "w1", "label": name})
            return "w1:p3"

        def start(tab: herdr.TabSession, entry: pane_record.PaneLaunch,
                  _account: str, _message: str) -> runtime_sessions.LiveSessionProcess:
            replacement = pane_record.launch(tab.pane_id, self.owner.current_session, entry["cwd"],
                {"CLAUDE_CONFIG_DIR": entry["config_dir"]}, ["claude", "--resume", self.owner.current_session])
            pane_record.write("agents", replacement)
            return runtime_sessions.LiveSessionProcess(backend.pid)

        with (mock.patch.object(accounts, "group_accounts", return_value=accounts.GroupAccounts(units="claude4")),
              mock.patch.object(accounts, "claude_account_dir", return_value=backend.config),
              mock.patch.object(herdr, "Herdr", return_value=client),
              mock.patch.object(client, "open_tab", side_effect=open_tab),
              mock.patch.object(stop_resume, "_tab_of", return_value=stop_resume.SessionNotFound(self.owner.current_session)),
              mock.patch.object(stop_resume, "_start_session", side_effect=start),
              mock.patch.object(stop_resume, "_detach"),
              mock.patch.object(sessions, "process_start", return_value="fixture-start"),
              mock.patch.object(herdr, "heal_claude_trust", return_value=herdr.Trusted(
                  self.old_folder, backend.config / ".claude.json", self.old_folder))):
            result = stop_resume.resume(self.owner.id, why="fixture recovery")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        with (mock.patch.object(backend, "panes", return_value=[])):
            moved = self.finished_move_rerun()
        self.assertEqual(moved.returncode, 0, moved.stderr)
        owners = [entry for entry in pane_record.read("agents").values() if entry["session_id"] == self.owner.id]
        self.assertEqual([(entry["pane_id"], pane_record.resume_launch(entry)["cwd"]) for entry in owners], [("w1:p3", str(self.new_folder))])


    def moved_codex(self) -> tuple[FolderMoveBackend, Path]:
        backend = self.folder_backend("codex", live=True)
        entry = pane_record.launch("w1:p1", self.owner.current_session, str(self.old_folder),
            {"CODEX_HOME": str(backend.config)}, ["codex", "--model", "fixture-model"], kind="codex")
        entry["name"] = "kept-name"
        pane_record.write("agents", entry)
        arguments = ["move-folder", "--unit", self.owner.id, "--to", str(self.new_folder)]
        moved = invoke_cli(unit_move, lambda: unit_move.main(arguments), arguments,
                           dict(os.environ), self.checkout)
        self.assertEqual(moved.returncode, 0, moved.stderr)
        self.assertEqual(backend.stops, [self.owner.current_session])
        self.assertEqual(len(backend.starts), 1)
        self.assertEqual(pane_record.resume_launch(pane_record.read("agents")["w1:p1"])["cwd"], str(self.new_folder))
        backend.live = False
        backend.publish_process_state()
        transcript = backend.config / "sessions/2026/11/02" / f"rollout-{self.owner.current_session}.jsonl"
        transcript.parent.mkdir(parents=True)
        _ = transcript.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": self.owner.current_session, "cwd": str(self.old_folder)}}) + "\n")
        retained = pane_record.read("agents")["w1:p1"]
        retained["cwd"] = str(self.old_folder)
        pane_record.write("agents", retained)
        return backend, transcript

    @contextmanager
    def codex_restore(self, backend: FolderMoveBackend, *, wrong_identity: bool = False,
                      reused_pid: bool = False, publish: bool = True) -> Generator[tuple[FakeClient, list[str]]]:
        client = FakeClient([{"pane_id": "w1:p1", "label": "kept-name", "workspace_id": "w1"}])
        proc = self.root / "restore-proc"
        proc.mkdir(exist_ok=True)
        from scripts.production.fake_unit_host import process
        process({"HERDR_PROC": str(proc)}, 998042, 1, ["zsh"], {})
        proof: list[str] = []
        held_id = "019b3a40-4250-7000-8000-000000000199" if wrong_identity else self.owner.current_session
        published = False
        publishing = False
        read_rollout = herdr.codex_rollout_thread

        def open_tab(session: str, space: str, name: str, cwd: str, environment: dict[str, str]) -> str:
            client.opened.append((session, space, name, cwd, environment.copy()))
            client.rows.append({"pane_id": "w1:p3", "label": name, "workspace_id": "w1"})
            return "w1:p3"

        def start(name: str, pane: str, kind: str, agent_arguments: list[str],
                  opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt,
                  timeout_ms: int) -> herdr.PromptSubmissionNotRequired:
            nonlocal published, publishing
            self.assertEqual(kind, "codex")
            self.assertEqual(agent_arguments, ["-C", str(self.new_folder), "-c", 'projects={' + json.dumps(str(self.new_folder))
                + '={trust_level="trusted"}}', "resume", self.owner.current_session, "--model", "fixture-model"])
            self.assertIsInstance(opening, herdr.NoOpeningPrompt)
            self.assertGreater(timeout_ms, 0)
            (proc / str(backend.pid) / "fd").mkdir(parents=True, exist_ok=True)
            _ = (proc / str(backend.pid) / "cmdline").write_bytes(f"codex\0resume\0{held_id}\0".encode())
            client.started.append((name, pane, agent_arguments.copy()))
            if publish:
                info: dict[str, object] = {"process_info": {"foreground_processes": [{
                    "pid": backend.pid, "argv": ["codex", *agent_arguments], "cwd": str(self.new_folder)}]}}

                def publication_result(*arguments: str) -> dict[str, object]:
                    if arguments[:2] == ("pane", "process-info"):
                        return info
                    return {"panes": [{"pane_id": "w1:p3", "terminal_id": "w1:p3-terminal",
                        "workspace_id": "w1", "agent_session": {"kind": "id", "value": held_id}}]}

                publishing = True
                try:
                    with mock.patch.object(client, "result", side_effect=publication_result):
                        launch = client.codex_launch(pane, agent_arguments)
                        launch["name"] = name
                        pane_launch.write(pane_record.record_path(client.session), launch)
                finally:
                    publishing = False
                published = True
            return herdr.PromptSubmissionNotRequired()

        def process_info(arguments: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
            self.assertEqual(arguments[-4:], ["pane", "process-info", "--pane", "w1:p3"])
            self.assertGreater(timeout, 0)
            self.assertEqual(client.closed, [])
            self.assertEqual(len(client.started), 1)
            proof.append("foreground")
            return subprocess.CompletedProcess(arguments, 0, json.dumps({"result": {"process_info": {
                "foreground_processes": [{"pid": backend.pid, "argv": ["codex", "resume", held_id],
                                           "cwd": str(self.new_folder)}]}}}), "")

        def process_start(pid: int) -> str:
            self.assertEqual(pid, backend.pid)
            if client.started and not publishing:
                self.assertEqual(client.closed, [])
                proof.append("start")
            return "changed-start" if reused_pid and proof.count("start") > 1 else "restore-start"

        def rollout_process_birth(pid: int) -> str:
            self.assertEqual(pid, backend.pid)
            return "restore-start"

        def rollout_thread(pid: int) -> str:
            with mock.patch.object(herdr, "_process_birth", side_effect=rollout_process_birth):
                return read_rollout(pid)

        def close(pane: str) -> None:
            FakeClient.close_tab_of(client, pane)
            if pane == "w1:p3":
                _ = (proc / str(backend.pid) / "cmdline").write_bytes(b"")

        def environment(pid: int) -> dict[str, str]:
            values = backend.environment(pid)
            if wrong_identity and (not publish or published):
                values["CODEX_THREAD_ID"] = "019b3a40-4250-7000-8000-000000000199"
            if client.started and not publishing:
                self.assertEqual(client.closed, [])
                proof.append("environment")
            return values

        def observe(pane: str, kind: str, timeout: float) -> str:
            self.assertEqual((pane, kind), ("w1:p3", "codex"))
            return super(FakeClient, client).native_session(pane, kind, timeout)

        with (mock.patch.dict(os.environ, {"HERDR_PROC": str(proc)}),
              mock.patch.object(herdr, "Herdr", return_value=client),
              mock.patch.object(herdr, "open_tab", side_effect=open_tab),
              mock.patch.object(herdr, "run_program", side_effect=process_info),
              mock.patch.object(herdr, "process_environment", side_effect=environment),
              mock.patch.object(herdr, "codex_rollout_thread", side_effect=rollout_thread),
              mock.patch.object(runtime_sessions, "process_start", side_effect=process_start),
              mock.patch.object(runtime_sessions, "session_liveness", return_value=runtime_sessions.SessionNotLive()),
              mock.patch.object(sessions, "running_sessions", return_value=[]),
              mock.patch.object(staff, "runtime_id", side_effect=AssertionError("Codex needs process evidence")),
              mock.patch.object(client, "start_agent", side_effect=start),
              mock.patch.object(client, "close_tab_of", side_effect=close),
              mock.patch.object(client, "native_session", side_effect=observe)):
            yield client, proof

    def second_move(self, backend: FolderMoveBackend) -> None:
        destination = self.root / "third-folder"
        arguments = ["move-folder", "--unit", self.owner.id, "--to", str(destination)]
        with (mock.patch.object(backend, "panes", return_value=[])):
            result = invoke_cli(unit_move, lambda: unit_move.main(arguments), arguments,
                                dict(os.environ), self.checkout)
        self.assertEqual(result.returncode, 0, result.stderr)
        launches = [entry for entry in pane_record.read("agents").values()
                    if entry["session_id"] in (self.owner.id, self.owner.current_session)]
        self.assertEqual(len(launches), 1)
        self.assertEqual((launches[0]["pane_id"], pane_record.resume_launch(launches[0])["cwd"]), ("w1:p3", str(destination)))
        self.assertTrue((destination / ".git").is_file())



    def test_codex_restore_publication_allows_a_second_folder_move(self) -> None:
        backend, _transcript = self.moved_codex()
        with self.codex_restore(backend, publish=True) as (client, _proof), redirect_stdout(io.StringIO()):
            self.assertEqual(pane_restore.restore("agents", dry_run=False), 0)
        self.assertEqual(client.closed, ["w1:p1"])
        self.second_move(backend)

    def test_claude_restore_requires_publication_and_then_allows_a_second_folder_move(self) -> None:
        backend = self.folder_backend("claude", live=False)
        arguments = ["move-folder", "--unit", self.owner.id, "--to", str(self.new_folder)]
        moved = invoke_cli(unit_move, lambda: unit_move.main(arguments), arguments, dict(os.environ), self.checkout)
        self.assertEqual(moved.returncode, 0, moved.stderr)
        backend.live = False
        backend.publish_process_state()
        entry = pane_record.read("agents")["w1:p1"]
        entry["ended"] = False
        entry["cwd"] = str(self.old_folder)
        pane_record.write("agents", entry)
        transcript = backend.config / "projects/work" / f"{self.owner.current_session}.jsonl"
        transcript.parent.mkdir(parents=True)
        _ = transcript.write_text("{}\n")
        before = pane_record.read("agents")
        for publish in (False, True):
            with self.subTest(publish=publish):
                client = FakeClient([{"pane_id": "w1:p1", "label": "kept-name", "workspace_id": "w1"}])
                client.publish_claude = False

                def start(name: str, pane: str, kind: str, agent_arguments: list[str],
                          opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt,
                          timeout_ms: int) -> herdr.PromptSubmissionNotRequired:
                    outcome = FakeClient.start_agent(client, name, pane, kind, agent_arguments, opening, timeout_ms)
                    if publish:
                        replacement = pane_record.launch(pane, self.owner.current_session, str(self.new_folder),
                            {"CLAUDE_CONFIG_DIR": str(backend.config)}, ["claude", *agent_arguments])
                        pane_record.write("agents", replacement)
                    return outcome

                with (mock.patch.object(herdr, "Herdr", return_value=client),
                      mock.patch.object(herdr, "open_tab", return_value="w1:p3"),
                      mock.patch.object(runtime_sessions, "session_liveness", return_value=runtime_sessions.SessionNotLive()),
                      mock.patch.object(sessions, "running_sessions", return_value=[]),
                      mock.patch.object(client, "start_agent", side_effect=start),
                      client.empty_shell(backend.shell_pid, self.root),
                      mock.patch.object(accounts, "role_account", return_value=accounts.NamedAccount("claude1", backend.config)),
                      mock.patch.object(accounts, "account_dir", return_value=backend.config),
                      redirect_stdout(io.StringIO()) as output, redirect_stderr(output)):
                    code = pane_restore.restore("agents", dry_run=False)
                if not publish:
                    self.assertEqual(code, 0)
                    self.assertNotIn("w1:p1", client.closed)
                    self.assertEqual(pane_record.read("agents"), before)
                else:
                    self.assertEqual(code, 0, output.getvalue())
                    self.assertEqual(client.closed, ["w1:p1"])
                    self.second_move(backend)


if __name__ == "__main__":
    _ = unittest.main()
