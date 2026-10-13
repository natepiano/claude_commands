"""Specification tests isolated from herdr, systemd and real account files."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, NotRequired, TypedDict, cast, override
from unittest.mock import patch

# The work order requires a bare sibling import for unittest discovery.
import trial  # pyright: ignore[reportImplicitRelativeImport]


class CommandResponse(TypedDict):
    stdout: str
    code: int
    stderr: NotRequired[str]
    write_files: NotRequired[dict[str, str]]
    require_paths: NotRequired[list[str]]
    remove_paths: NotRequired[list[str]]


class PaneDetails(TypedDict):
    pane_id: str
    label: str | None
    workspace_id: NotRequired[str]


class NewRootPane(TypedDict):
    pane_id: str
    label: NotRequired[str | None]


class CreatedWorkspace(TypedDict):
    workspace_id: str
    label: str


class CreatedTab(TypedDict):
    tab_id: str


class WorkspaceCreationResult(TypedDict):
    workspace: CreatedWorkspace
    tab: CreatedTab
    root_pane: NewRootPane


class WorkspaceCreationResponse(TypedDict):
    ok: bool
    result: WorkspaceCreationResult


class PaneCloseResult(TypedDict):
    type: str


class PaneCloseResponse(TypedDict):
    ok: bool
    result: PaneCloseResult


class PaneListResult(TypedDict):
    panes: list[PaneDetails]


class PaneListResponse(TypedDict):
    ok: bool
    result: PaneListResult


class AgentDetails(TypedDict):
    pane_id: str
    agent: str
    agent_status: str
    interactive_ready: NotRequired[bool]


class WorkerReadinessDetails(TypedDict):
    pane_id: str
    agent: str
    agent_status: str
    interactive_ready: bool


class WorkerReadinessResult(TypedDict):
    agent: WorkerReadinessDetails


class WorkerReadinessResponse(TypedDict):
    ok: bool
    result: WorkerReadinessResult


class AgentListResult(TypedDict):
    agents: list[AgentDetails]


class AgentListResponse(TypedDict):
    ok: bool
    result: AgentListResult


class SettledAgentResult(TypedDict):
    agent: AgentDetails


class SettledAgentResponse(TypedDict):
    ok: bool
    result: SettledAgentResult


class AgentErrorDetail(TypedDict):
    code: str
    message: str


class AgentErrorResponse(TypedDict):
    ok: bool
    error: AgentErrorDetail


class CodexDaemonIdentity(TypedDict):
    pid: int
    start_time: int


class CodexDaemonBaselineResources(TypedDict):
    systemd_units: list[str]
    codex_daemons_before: list[CodexDaemonIdentity]


class LaunchStartedResources(CodexDaemonBaselineResources):
    codex_daemons_started: list[CodexDaemonIdentity]


class WorkspaceListResult(TypedDict):
    workspaces: list[CreatedWorkspace]


class WorkspaceListResponse(TypedDict):
    ok: bool
    result: WorkspaceListResult


class TabCreationResult(TypedDict):
    tab: CreatedTab
    root_pane: NewRootPane


class TabCreationResponse(TypedDict):
    ok: bool
    result: TabCreationResult


class PaneSplitResult(TypedDict):
    pane: NewRootPane


class PaneSplitResponse(TypedDict):
    ok: bool
    result: PaneSplitResult


class StartedUnits(TypedDict):
    systemd_units: list[str]


class IssueRecord(TypedDict):
    time: str
    session: str
    expected: str
    happened: str


class ClaudeProjectTrust(TypedDict):
    hasTrustDialogAccepted: bool


class ClaudeTrustConfiguration(TypedDict):
    projects: dict[str, ClaudeProjectTrust]


class RecordedWorkOrder(TypedDict):
    file: str


WorkerAssignments = TypedDict("WorkerAssignments", {
    "slug-unit-impl": str,
    "slug-unit-review": str,
    "clamp-unit-impl": str,
    "clamp-unit-review": str,
    "count-unit-impl": str,
    "count-unit-review": str,
}, total=False)


@dataclass(frozen=True)
class InvocationResult:
    code: int
    stdout: str
    stderr: str


def herdr_args(*arguments: str) -> list[str]:
    """Use targeting confirmed by the impl slot against herdr 0.9.0."""
    return ["--session", "herdr-trial", *arguments]


def installed_git() -> str:
    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("scaffold fixture tests require Git")
    return executable


GIT_BINARY = installed_git()


class TrialTests(unittest.TestCase):
    root: Path = Path()
    scratch: Path = Path()
    log: Path = Path()
    response_file: Path = Path()
    fake: Path = Path()
    claude: Path = Path()
    codex: Path = Path()
    project: Path = Path()
    responses: dict[str, CommandResponse | list[CommandResponse]] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.scratch = self.root / "scratch"
        self.log = self.root / "argv.jsonl"
        self.response_file = self.root / "responses.json"
        self.fake = self.root / "fake-herdr"
        source = Path(__file__).with_name("fake_herdr.py").read_text()
        _ = self.fake.write_text("#!" + sys.executable + "\n" + source.split("\n", 1)[1])
        self.fake.chmod(0o755)
        self.claude = self.root / "claude-account"
        self.codex = self.root / "codex-account"
        self.claude.mkdir()
        self.codex.mkdir()
        self.project = self.root / "project" / "child"
        self.project.mkdir(parents=True)
        self.responses = {"default": {"stdout": "", "code": 0}}
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HERDR_BIN": str(self.fake),
            "HERDR_TRIAL_SYSTEMD_RUN": str(self.fake),
            "HERDR_TRIAL_SYSTEMCTL": str(self.fake),
            "HERDR_TRIAL_KDOTOOL": str(self.fake),
            "HERDR_TRIAL_TERMINAL": "/fixture/ghostty",
            "HERDR_TRIAL_GIT": GIT_BINARY,
            "HERDR_TRIAL_ROOT": str(self.scratch),
            "HERDR_TRIAL_PROC": str(self.root / "proc-fixture"),
            "HERDR_TRIAL_STOP_PROCESS": str(self.fake),
            "HERDR_TRIAL_MANIFEST": str(self.root / "cleanup/cleanup.json"),
            "HERDR_TRIAL_NOTIFIER_ROOT": str(self.root / "notifier"),
            "HERDR_TRIAL_SHOWRUNNER_STATE": str(self.root / "showrunner"),
            "HERDR_TRIAL_HERDR_CONFIG": str(self.root / "herdr-config"),
            "HERDR_TRIAL_START_TIMEOUT": "0.5",
            "FAKE_HERDR_LOG": str(self.log),
            "FAKE_HERDR_RESPONSES": str(self.response_file),
            "CLAUDE_CONFIG_DIR": str(self.claude),
            "CODEX_HOME": str(self.codex),
            "HOME": str(self.root),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Trial fixture",
            "GIT_AUTHOR_EMAIL": "trial@example.invalid",
            "GIT_COMMITTER_NAME": "Trial fixture",
            "GIT_COMMITTER_EMAIL": "trial@example.invalid",
            # An omitted tool override must fail instead of reaching a real tool.
            "PATH": str(self.root / "no-external-programs"),
        }, clear=True)))
        self.configure(["search", "--name", "^herdr-trial$"], "window-17\n")

    def configure(self, arguments: list[str], stdout: str = "", code: int = 0,
                  stderr: str = "") -> None:
        self.responses[" ".join(arguments)] = {
            "stdout": stdout, "code": code, "stderr": stderr,
        }

    def invoke(self, *arguments: str) -> InvocationResult:
        _ = self.response_file.write_text(json.dumps(self.responses))
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = trial.main(list(arguments))
        return InvocationResult(code, stdout.getvalue(), stderr.getvalue())

    def invoke_cli(self, *arguments: str) -> InvocationResult:
        _ = self.response_file.write_text(json.dumps(self.responses))
        result = subprocess.run([
            sys.executable, str(Path(__file__).with_name("trial.py").resolve()), *arguments,
        ], cwd=self.project, capture_output=True, text=True, check=False, timeout=10)
        return InvocationResult(result.returncode, result.stdout, result.stderr)

    def fixture_git(self, directory: Path, *arguments: str) -> str:
        result = subprocess.run([GIT_BINARY, "-C", str(directory), *arguments],
                                capture_output=True, text=True, check=False, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def production_parser(self, document: Path, expression: str) -> InvocationResult:
        program = (
            "import sys\nfrom pathlib import Path\n" +
            "sys.path.insert(0, '/home/natepiano/.claude/scripts/production')\n" +
            "from add_unit import cell_value\n" +
            "from merge_checkpoint import optional_field, FieldPresent\n" +
            "from production_lifecycle import production_name\n" +
            "lines = Path(sys.argv[1]).read_text().splitlines()\n" + expression
        )
        result = subprocess.run([sys.executable, "-B", "-c", program, str(document)],
                                capture_output=True, text=True, check=False, timeout=10)
        return InvocationResult(result.returncode, result.stdout, result.stderr)

    def assignment(self, label: str, work_order: Path) -> None:
        directory = self.scratch / "work"
        directory.mkdir(parents=True, exist_ok=True)
        order: RecordedWorkOrder = {"file": str(work_order)}
        _ = (directory / f"{label}.json").write_text(json.dumps(order))

    def assigned_files(self) -> WorkerAssignments:
        files: dict[str, str] = {}
        for path in (self.scratch / "work").glob("*.json"):
            order = cast(RecordedWorkOrder, json.loads(path.read_text()))
            files[path.stem] = order["file"]
        return cast(WorkerAssignments, cast(object, files))

    def settled_response(self, pane_id: str, state: str, timeout: int = 1800000) -> None:
        response: SettledAgentResponse = {"ok": True, "result": {"agent": {
            "pane_id": pane_id, "agent": "codex", "agent_status": state,
        }}}
        self.configure(herdr_args("agent", "wait", pane_id, "--timeout", str(timeout)),
                       json.dumps(response))

    def worker_state(self, pane_id: str, state: str = "idle", ready: bool = True) -> None:
        response: WorkerReadinessResponse = {"ok": True, "result": {"agent": {
            "pane_id": pane_id, "agent": "codex", "agent_status": state,
            "interactive_ready": ready,
        }}}
        self.configure(herdr_args("agent", "get", pane_id), json.dumps(response))

    def working_response(self, pane_id: str, timeout: int = 60000) -> None:
        response: SettledAgentResponse = {"ok": True, "result": {"agent": {
            "pane_id": pane_id, "agent": "codex", "agent_status": "working",
        }}}
        self.configure(herdr_args("agent", "wait", pane_id, "--until", "working",
                                  "--timeout", str(timeout)), json.dumps(response))

    def work_fixture(self) -> tuple[str, Path, Path]:
        pane = "w38:p71"
        work_order = self.root / "turn-order.md"
        _ = work_order.write_text("tiny Work Order\n")
        self.pane_list([{"pane_id": pane, "label": "slug-unit-impl"}])
        return pane, work_order, Path(str(work_order) + ".done")

    def turn_start_timeout_response(self, pane: str, milliseconds: int) -> list[str]:
        response: AgentErrorResponse = {"ok": False, "error": {
            "code": "timeout", "message": "agent wait timed out before working",
        }}
        arguments = herdr_args("agent", "wait", pane, "--until", "working",
                               "--timeout", str(milliseconds))
        self.configure(arguments, code=1, stderr=json.dumps(response))
        return arguments

    def single_issue(self, label: str) -> IssueRecord:
        lines = (self.scratch / "issues.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        issue = cast(IssueRecord, json.loads(lines[0]))
        self.assertEqual(set(issue), {"time", "session", "expected", "happened"})
        self.assertEqual(issue["session"], label)
        self.assertEqual(datetime.fromisoformat(issue["time"]).utcoffset(), timedelta(0))
        return issue

    def assert_blocked_issue(self, result: InvocationResult, label: str) -> None:
        self.assertEqual(result.code, 2, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn("blocked", result.stderr.casefold())
        lines = (self.scratch / "issues.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        issue = cast(IssueRecord, json.loads(lines[0]))
        self.assertEqual(set(issue), {"time", "session", "expected", "happened"})
        self.assertEqual(issue["session"], label)
        self.assertTrue(issue["expected"])
        self.assertIn("blocked", issue["happened"].casefold())
        self.assertEqual(datetime.fromisoformat(issue["time"]).utcoffset(), timedelta(0))

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [cast(list[str], json.loads(line))
                for line in self.log.read_text().splitlines()]

    def started_units(self) -> StartedUnits:
        value = cast(StartedUnits, json.loads((self.scratch / "started.json").read_text()))
        self.assertEqual(set(value), {"systemd_units"})
        return value

    def assert_success(self, result: InvocationResult) -> None:
        self.assertEqual(result.code, 0, result.stderr)
        self.assertEqual(result.stderr, "")

    def assert_refusal(self, result: InvocationResult, *names: str) -> None:
        self.assertEqual(result.code, 1, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        for name in names:
            self.assertIn(name, result.stderr)

    def trust_configs(self, claude_projects: dict[str, bool],
                      codex_projects: dict[str, str]) -> None:
        configuration: ClaudeTrustConfiguration = {
            "projects": {path: {"hasTrustDialogAccepted": accepted}
                         for path, accepted in claude_projects.items()},
        }
        _ = (self.claude / ".claude.json").write_text(json.dumps(configuration))
        _ = (self.codex / "config.toml").write_text("\n".join(
            f"[projects.{json.dumps(path)}]\ntrust_level = {json.dumps(level)}\n"
            for path, level in codex_projects.items()
        ))

    def pane_list(self, panes: list[PaneDetails]) -> None:
        response: PaneListResponse = {"ok": True, "result": {"panes": panes}}
        self.configure(herdr_args("pane", "list"), json.dumps(response))
        for pane in panes:
            if pane["label"] in {
                "slug-unit-impl", "slug-unit-review", "clamp-unit-impl",
                "clamp-unit-review", "count-unit-impl", "count-unit-review",
            }:
                self.worker_state(pane["pane_id"])
                self.working_response(pane["pane_id"])

    def launch_fixture(self, impl_family: str = "codex", review_family: str = "codex") -> None:
        self.assert_success(self.invoke("scaffold"))
        self.log.unlink(missing_ok=True)
        proc = self.root / "proc-fixture"
        proc.mkdir()
        os.environ.update({
            "HERDR_TRIAL_PROC": str(proc),
            "HERDR_TRIAL_RESOLVE": str(self.fake),
            "HERDR_TRIAL_CLAUDE_CONFIG_DIR": str(self.claude),
            "HERDR_TRIAL_CODEX_HOME": str(self.codex),
            "HERDR_TRIAL_START_TIMEOUT": "1200",
            "CLAUDE_CALLER_SECRET": "must not reach a pane or herdr",
            "CLAUDE_CODE_SESSION_ID": "unrelated-caller-session",
        })
        self.trust_configs({str(self.scratch / "repo"): True},
                           {str(self.scratch / "repo"): "trusted"})
        self.configure(herdr_args("status", "server"), "status: running\n")
        response: WorkspaceListResponse = {"ok": True, "result": {"workspaces": []}}
        self.configure(herdr_args("workspace", "list"), json.dumps(response))
        for task, family, model, effort in (
            ("production.director", "claude", "fixture-director", "high"),
            ("delegate.impl", impl_family, "fixture-implementer", "medium"),
            ("delegate.review", review_family, "fixture-reviewer", "low"),
        ):
            self.configure([task], f"task={task} family={family} agent={model} effort={effort} tier=priority\n")
        _ = (self.scratch / "started.json").write_text(
            '{"systemd_units": ["herdr-trial-server", "herdr-trial-client"]}\n')

    def launch_panes(self) -> dict[str, str]:
        panes = {"showrunner": "w79:p401"}
        for index, unit in enumerate(("slug-unit", "clamp-unit", "count-unit")):
            for offset, suffix in enumerate(("", "-impl", "-review")):
                panes[unit + suffix] = f"w79:p{510 + index * 13 + offset}"
        return panes

    def trial_rule(self) -> str:
        text = (self.scratch / "repo/docs/plans/herdr-fake-production.md").read_text()
        return next(paragraph.strip() for paragraph in text.split("\n\n")
                    if paragraph.startswith("Trial production:"))

    def opening_text(self, label: str) -> str:
        if label.endswith(("-impl", "-review")):
            unit, role = label.rsplit("-", 1)
            return (f"You are the {role} worker of {unit} in the herdr trial. "
                    "Wait; your unit director will send you Work Orders.")
        if label == "showrunner":
            plan = self.scratch / "repo/docs/plans/herdr-fake-production.md"
            command = "/showrunner:produce"
        else:
            plan = self.scratch / "worktrees" / label / "docs/plans" / f"herdr-fake-{label.removesuffix('-unit')}.md"
            command = "/unit:direct"
        return f"{command} {plan}\n\n{self.trial_rule()}"

    def proc_process(self, pid: int, start_time: int, arguments: list[str],
                     home: str, executable: Path) -> None:
        directory = self.root / "proc-fixture" / str(pid)
        directory.mkdir()
        _ = (directory / "cmdline").write_bytes("\0".join(arguments).encode() + b"\0")
        _ = (directory / "environ").write_bytes(f"CODEX_HOME={home}\0OTHER=fixture\0".encode())
        # comm may contain spaces and parentheses; starttime is stat field 22.
        fields = ["S", *["0"] * 18, str(start_time), "0"]
        _ = (directory / "stat").write_text(f"{pid} (codex worker (fixture)) " + " ".join(fields))
        (directory / "exe").symlink_to(executable)

    def invoke_launch(self, observe: Callable[[list[str]], None] = lambda _argv: None,
                      failed_label: str = "", screen: str = "",
                      blocked_prompt_label: str = "") -> InvocationResult:
        original_run = subprocess.run
        panes = self.launch_panes()
        success: PaneCloseResponse = {"ok": True, "result": {"type": "ok"}}

        def record_launch(argv: list[str], *, capture_output: bool, text: bool,
                          check: bool, timeout: float,
                          env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            if argv[1:3] == ["--session", "herdr-trial"]:
                self.assertIsNotNone(env, "all launch herdr calls need an explicit clean environment")
                if env is not None:
                    self.assertFalse(any(key.startswith("CLAUDE_") for key in env), env)
                command = argv[3:]
                observe(command)
                if command[:2] == ["workspace", "create"]:
                    response: WorkspaceCreationResponse = {"ok": True, "result": {
                        "workspace": {"workspace_id": "w79", "label": "herdr-fake"},
                        "tab": {"tab_id": "w79:t88"},
                        "root_pane": {"pane_id": panes["showrunner"]},
                    }}
                    self.configure(argv[1:], json.dumps(response))
                elif command[:2] == ["tab", "create"]:
                    unit = command[command.index("--label") + 1]
                    tab: TabCreationResponse = {"ok": True, "result": {
                        "tab": {"tab_id": f"w79:t{89 + ('slug-unit', 'clamp-unit', 'count-unit').index(unit)}"},
                        "root_pane": {"pane_id": panes[unit]},
                    }}
                    self.configure(argv[1:], json.dumps(tab))
                elif command[:2] == ["pane", "split"]:
                    parent = command[2]
                    unit = next(label for label, pane in panes.items() if pane == parent)
                    child = unit + "-impl" if not unit.endswith("-impl") else unit.removesuffix("-impl") + "-review"
                    split: PaneSplitResponse = {"ok": True, "result": {"pane": {"pane_id": panes[child]}}}
                    self.configure(argv[1:], json.dumps(split))
                elif command[:2] in (["pane", "rename"], ["tab", "rename"], ["agent", "start"]):
                    if command[:2] == ["agent", "start"] and command[2] == failed_label:
                        error: AgentErrorResponse = {"ok": False, "error": {
                            "code": "agent_not_ready", "message": "fixture startup stopped at dialog",
                        }}
                        self.configure(argv[1:], code=1, stderr=json.dumps(error))
                    elif command[:2] == ["agent", "start"]:
                        ready: SettledAgentResponse = {"ok": True, "result": {"agent": {
                            "pane_id": command[command.index("--pane") + 1],
                            "agent": command[command.index("--kind") + 1], "agent_status": "idle",
                        }}}
                        self.configure(argv[1:], json.dumps(ready))
                    else:
                        self.configure(argv[1:], json.dumps(success))
                elif command[:2] == ["agent", "read"]:
                    self.configure(argv[1:], screen)
                elif command[:2] == ["agent", "prompt"]:
                    if command[2] == blocked_prompt_label:
                        blocked: AgentErrorResponse = {"ok": False, "error": {
                            "code": "agent_blocked", "message": "fixture opening prompt refused by dialog",
                        }}
                        self.configure(argv[1:], code=1, stderr=json.dumps(blocked))
                    else:
                        self.configure(argv[1:], json.dumps(success))
                _ = self.response_file.write_text(json.dumps(self.responses))
            return original_run(argv, capture_output=capture_output, text=text,
                                check=check, timeout=timeout, env=env)

        original_text = Path.read_text
        original_bytes = Path.read_bytes
        original_iterdir = Path.iterdir

        def require_fixture(path: Path) -> None:
            for forbidden in (Path("/proc"), Path("/home/natepiano/.claude1"), Path("/home/natepiano/.codex1")):
                self.assertFalse(path == forbidden or forbidden in path.parents,
                                 f"launch test attempted to read real process/account data: {path}")

        def fixture_text(path: Path, encoding: str | None = None,
                         errors: str | None = None) -> str:
            require_fixture(path)
            return original_text(path, encoding=encoding, errors=errors)

        def fixture_bytes(path: Path) -> bytes:
            require_fixture(path)
            return original_bytes(path)

        def fixture_entries(path: Path) -> Iterator[Path]:
            require_fixture(path)
            return original_iterdir(path)

        with (patch.object(subprocess, "run", side_effect=record_launch),
              patch.object(Path, "read_text", new=fixture_text),
              patch.object(Path, "read_bytes", new=fixture_bytes),
              patch.object(Path, "iterdir", new=fixture_entries)):
            return self.invoke("launch")

    def launch_calls(self, group: str, action: str) -> list[list[str]]:
        return [argv[2:] for argv in self.calls()
                if argv[:4] == ["--session", "herdr-trial", group, action]]

    def pane_environment(self, command: list[str]) -> dict[str, str]:
        assignments = [command[index + 1] for index, item in enumerate(command) if item == "--env"]
        self.assertEqual(len(assignments), len(set(item.split("=", 1)[0] for item in assignments)))
        return dict(item.split("=", 1) for item in assignments)

    def test_launch_refuses_stopped_server_before_creating_any_pane(self) -> None:
        self.launch_fixture()
        self.configure(herdr_args("status", "server"), "status: not running\n")
        before = (self.scratch / "started.json").read_bytes()
        result = self.invoke_launch()
        self.assert_refusal(result, "server herdr-trial not answering: run trial.py up")
        self.assertEqual(self.calls(), [herdr_args("status", "server")])
        self.assertEqual((self.scratch / "started.json").read_bytes(), before)

    def test_launch_refuses_existing_workspace_before_trust_or_mutation(self) -> None:
        self.launch_fixture()
        response: WorkspaceListResponse = {"ok": True, "result": {"workspaces": [
            {"workspace_id": "w5", "label": "other"},
            {"workspace_id": "w19", "label": "herdr-fake"},
        ]}}
        self.configure(herdr_args("workspace", "list"), json.dumps(response))
        (self.claude / ".claude.json").unlink()
        before = (self.scratch / "started.json").read_bytes()
        self.assert_refusal(self.invoke_launch(), "herdr-fake")
        self.assertEqual(self.calls(), [herdr_args("status", "server"), herdr_args("workspace", "list")])
        self.assertEqual((self.scratch / "started.json").read_bytes(), before)

    def test_launch_checks_both_accounts_for_every_folder_before_first_pane(self) -> None:
        self.launch_fixture()
        folders = [self.scratch / "repo", *[self.scratch / "worktrees" / unit
                    for unit in ("slug-unit", "clamp-unit", "count-unit")]]
        # Independent repository keys allow each start-folder check to fail on
        # its own; linked worktrees would all share the already-checked main key.
        for folder in folders[1:]:
            (folder / ".git").unlink()
            _ = self.fixture_git(folder, "init", "--initial-branch=main")
        before = (self.scratch / "started.json").read_bytes()
        for account in ("claude", "codex"):
            for missing in folders:
                with self.subTest(folder=missing, account=account):
                    self.log.unlink(missing_ok=True)
                    self.trust_configs(
                        {str(folder): not (folder == missing and account == "claude") for folder in folders},
                        {str(folder): "untrusted" if folder == missing and account == "codex"
                         else "trusted" for folder in folders},
                    )
                    self.assert_refusal(self.invoke_launch(), str(missing), account.title())
                    self.assertEqual(self.calls(), [herdr_args("status", "server"), herdr_args("workspace", "list")])
                    self.assertEqual((self.scratch / "started.json").read_bytes(), before)

    def test_launch_creates_ten_labelled_panes_with_cwd_and_account_environment(self) -> None:
        self.launch_fixture()
        self.assert_success(self.invoke_launch())
        workspace = self.launch_calls("workspace", "create")
        self.assertEqual(len(workspace), 1)
        self.assertEqual(workspace[0][2:6], ["--label", "herdr-fake", "--cwd", str(self.scratch / "repo")])
        self.assertIn("--no-focus", workspace[0])
        self.assertEqual(self.pane_environment(workspace[0]), {
            "CLAUDE_CONFIG_DIR": str(self.claude), "ENABLE_TOOL_SEARCH": "true",
        })
        self.assertEqual(self.launch_calls("tab", "rename"), [["tab", "rename", "w79:t88", "showrunner"]])
        tabs = self.launch_calls("tab", "create")
        splits = self.launch_calls("pane", "split")
        self.assertEqual(len(tabs), 3)
        self.assertEqual(len(splits), 6)
        panes = self.launch_panes()
        for index, unit in enumerate(("slug-unit", "clamp-unit", "count-unit")):
            directory = str(self.scratch / "worktrees" / unit)
            tab = tabs[index]
            self.assertEqual(tab[2:8], ["--workspace", "w79", "--label", unit, "--cwd", directory])
            self.assertIn("--no-focus", tab)
            self.assertEqual(self.pane_environment(tab), {
                "SHOWRUNNER_UNIT": "herdr-fake", "SHOWRUNNER_UNIT_ID": unit,
                "CLAUDE_CONFIG_DIR": str(self.claude), "ENABLE_TOOL_SEARCH": "true",
            })
            for split, parent, direction in (
                (splits[index * 2], panes[unit], "right"),
                (splits[index * 2 + 1], panes[unit + "-impl"], "down"),
            ):
                self.assertEqual(split[2:7], [parent, "--direction", direction, "--cwd", directory])
                self.assertIn("--no-focus", split)
                self.assertEqual(self.pane_environment(split), {
                    "SHOWRUNNER_UNIT": "herdr-fake", "SHOWRUNNER_UNIT_ID": unit, "CODEX_HOME": str(self.codex),
                })
        self.assertEqual(self.launch_calls("pane", "rename"), [
            ["pane", "rename", pane, label] for label, pane in panes.items()
        ])
        calls = self.calls()
        first_start = next(index for index, call in enumerate(calls) if call[2:4] == ["agent", "start"])
        self.assertEqual(sum(call[2:4] == ["pane", "rename"] for call in calls[:first_start]), 10)
        self.assertFalse(any(call[2:4] == ["pane", "run"] for call in calls))

    def test_launch_resolves_distinct_models_and_starts_workers_before_directors_and_showrunner(self) -> None:
        self.launch_fixture()
        result = self.invoke_launch()
        self.assert_success(result)
        self.assertEqual([call for call in self.calls() if call == ["production.director"] or (call and call[0].startswith("delegate."))],
                         [["production.director"], ["delegate.impl"], ["delegate.review"]])
        units = ("slug-unit", "clamp-unit", "count-unit")
        labels = [f"{unit}-{role}" for unit in units for role in ("impl", "review")] + [*units, "showrunner"]
        starts = self.launch_calls("agent", "start")
        self.assertEqual([call[2] for call in starts], labels)
        panes = self.launch_panes()
        for label, call in zip(labels, starts, strict=True):
            worker = label.endswith(("-impl", "-review"))
            kind = "codex" if worker else "claude"
            self.assertEqual(call[:10], ["agent", "start", label, "--kind", kind, "--pane", panes[label], "--timeout", "1200", "--"])
            if worker:
                role = label.rsplit("-", 1)[1]
                model, effort = ("fixture-implementer", "medium") if role == "impl" else ("fixture-reviewer", "low")
                self.assertEqual(call[10:], ["-m", model, "-c", f"model_reasoning_effort={effort}", "-s", "danger-full-access", "-a", "never"])
            else:
                name = "herdr-fake" if label == "showrunner" else "herdr-fake-" + label
                self.assertEqual(call[10:], ["--model", "fixture-director", "--effort", "high", "--remote-control", name, "-n", name])
        self.assertEqual(result.stdout.splitlines(), [
            f"{label}\t{'codex' if label.endswith(('-impl', '-review')) else 'claude'}\t{panes[label]}" for label in labels
        ])

    def test_launch_starts_every_agent_without_an_opening_prompt_argument(self) -> None:
        self.launch_fixture()
        self.assert_success(self.invoke_launch())
        starts = self.launch_calls("agent", "start")
        self.assertEqual(len(starts), 10)
        for call in starts:
            with self.subTest(label=call[2]):
                self.assertNotIn(self.opening_text(call[2]), call)
                self.assertFalse(any("\n" in argument or "`" in argument for argument in call))
                self.assertEqual(len(call[call.index("--") + 1:]), 8)

    def test_launch_prompts_each_ready_agent_once_with_its_complete_opening_text(self) -> None:
        self.launch_fixture()
        self.assert_success(self.invoke_launch())
        starts = self.launch_calls("agent", "start")
        prompts = self.launch_calls("agent", "prompt")
        self.assertEqual(prompts, [["agent", "prompt", call[2], self.opening_text(call[2])] for call in starts])
        calls = self.calls()
        for start, prompt in zip(starts, prompts, strict=True):
            with self.subTest(label=start[2]):
                self.assertLess(calls.index(herdr_args(*start)), calls.index(herdr_args(*prompt)))
                self.assertEqual(calls.count(herdr_args(*prompt)), 1)
                if start[2] == "showrunner" or start[2].endswith("-unit"):
                    self.assertIn("\n\nTrial production:", prompt[3])
                    self.assertIn("`trial.py issue`", prompt[3])

    def test_launch_blocked_opening_prompt_logs_one_issue_and_stops_with_refusal(self) -> None:
        self.launch_fixture()
        labels = [f"{unit}-{role}" for unit in ("slug-unit", "clamp-unit", "count-unit")
                  for role in ("impl", "review")] + ["slug-unit", "clamp-unit", "count-unit", "showrunner"]
        for label in ("slug-unit-impl", "slug-unit", "showrunner"):
            with self.subTest(label=label):
                self.log.unlink(missing_ok=True)
                issues = self.scratch / "issues.jsonl"
                issues.unlink(missing_ok=True)
                self.assert_refusal(self.invoke_launch(blocked_prompt_label=label), label)
                expected = labels[:labels.index(label) + 1]
                self.assertEqual([call[2] for call in self.launch_calls("agent", "start")], expected)
                self.assertEqual(self.launch_calls("agent", "prompt"), [
                    ["agent", "prompt", target, self.opening_text(target)] for target in expected
                ])
                lines = issues.read_text().splitlines()
                self.assertEqual(len(lines), 1)
                issue = cast(IssueRecord, json.loads(lines[0]))
                self.assertEqual(issue["session"], label)
                self.assertIn("fixture opening prompt refused by dialog", issue["happened"])
                self.assertTrue(issue["expected"])
                self.assertEqual(datetime.fromisoformat(issue["time"]).utcoffset(), timedelta(0))
                self.assertFalse(any(call[2:4] in (["pane", "send-keys"], ["pane", "send-text"], ["agent", "send-keys"])
                                     for call in self.calls()))

    def test_launch_default_agent_timeout_allows_ninety_seconds_for_readiness(self) -> None:
        self.launch_fixture()
        _ = os.environ.pop("HERDR_TRIAL_START_TIMEOUT")
        timeouts: list[float] = []
        original_run = subprocess.run

        def record_timeout(argv: list[str], *, capture_output: bool, text: bool,
                           check: bool, timeout: float,
                           env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            if argv[3:5] == ["agent", "start"]:
                self.assertEqual(argv[argv.index("--timeout") + 1], "90000")
                self.assertGreater(timeout, 90)
                timeouts.append(timeout)
            return original_run(argv, capture_output=capture_output, text=text,
                                check=check, timeout=timeout, env=env)

        with patch.object(subprocess, "run", side_effect=record_timeout):
            self.assert_success(self.invoke_launch())
        self.assertEqual(len(timeouts), 10)

    def test_launch_claude_fallback_workers_use_role_models_and_names_without_remote_control(self) -> None:
        self.launch_fixture(impl_family="claude", review_family="claude")
        for impl_family, review_family in (("claude", "claude"), ("claude", "codex"), ("codex", "claude")):
            with self.subTest(impl=impl_family, review=review_family):
                self.log.unlink(missing_ok=True)
                for task, family, model, effort in (
                    ("delegate.impl", impl_family, "fixture-implementer", "medium"),
                    ("delegate.review", review_family, "fixture-reviewer", "low"),
                ):
                    self.configure([task], f"task={task} family={family} agent={model} effort={effort} tier=priority\n")
                self.assert_success(self.invoke_launch())
                self.assertEqual(self.launch_calls("agent", "prompt"), [
                    ["agent", "prompt", call[2], self.opening_text(call[2])]
                    for call in self.launch_calls("agent", "start")
                ])
                for index, call in enumerate(self.launch_calls("pane", "split")):
                    family = impl_family if index % 2 == 0 else review_family
                    environment = self.pane_environment(call)
                    if family == "claude":
                        self.assertEqual(environment["CLAUDE_CONFIG_DIR"], str(self.claude))
                        self.assertEqual(environment["ENABLE_TOOL_SEARCH"], "true")
                        self.assertNotIn("CODEX_HOME", environment)
                    else:
                        self.assertEqual(environment["CODEX_HOME"], str(self.codex))
                        self.assertNotIn("CLAUDE_CONFIG_DIR", environment)
                for call in self.launch_calls("agent", "start")[:6]:
                    label = call[2]
                    role = label.rsplit("-", 1)[1]
                    family = impl_family if role == "impl" else review_family
                    self.assertEqual(call[4], family)
                    if family == "claude":
                        model, effort = ("fixture-implementer", "medium") if role == "impl" else ("fixture-reviewer", "low")
                        self.assertEqual(call[10:], ["--model", model, "--effort", effort, "-n", "herdr-fake-" + label])

    def test_launch_records_matching_daemon_baseline_before_panes_and_new_daemons_before_directors(self) -> None:
        self.launch_fixture()
        self.proc_process(31401, 9981, ["/outside/bin/codex", "app-server", "--managed-daemon"], str(self.codex), Path("/outside/bin/codex"))
        self.proc_process(31402, 9982, ["codex", "app-server", "--managed-daemon"], "", self.codex / "bin/codex")
        self.proc_process(31403, 9983, ["codex", "app-server", "--managed-daemon"], str(self.root / "other-account"), Path("/outside/bin/codex"))
        self.proc_process(31404, 9984, ["codex", "exec"], str(self.codex), self.codex / "bin/codex")
        (self.root / "proc-fixture/31405").mkdir()  # process vanished mid-scan
        baseline: list[CodexDaemonIdentity] = [{"pid": 31401, "start_time": 9981}, {"pid": 31402, "start_time": 9982}]
        seen_before = False
        seen_after = False

        def observe(command: list[str]) -> None:
            nonlocal seen_before, seen_after
            if command[:2] == ["workspace", "create"]:
                record = cast(CodexDaemonBaselineResources, json.loads((self.scratch / "started.json").read_text()))
                self.assertCountEqual(record["codex_daemons_before"], baseline)
                self.assertEqual(record["systemd_units"], ["herdr-trial-server", "herdr-trial-client"])
                seen_before = True
            if command[:3] == ["agent", "start", "count-unit-review"]:
                self.proc_process(31409, 9999, ["codex", "app-server", "--managed-daemon"], str(self.codex), Path("/outside/bin/codex"))
            if command[:3] == ["agent", "start", "slug-unit"]:
                record = cast(LaunchStartedResources, json.loads((self.scratch / "started.json").read_text()))
                self.assertEqual(record["codex_daemons_started"], [{"pid": 31409, "start_time": 9999}])
                seen_after = True

        self.assert_success(self.invoke_launch(observe))
        self.assertTrue(seen_before)
        self.assertTrue(seen_after)
        record = cast(LaunchStartedResources, json.loads((self.scratch / "started.json").read_text()))
        self.assertCountEqual(record["codex_daemons_before"], baseline)
        self.assertEqual(record["codex_daemons_started"], [{"pid": 31409, "start_time": 9999}])

    def test_launch_records_managed_daemons_without_recording_delegate_mesh_servers(self) -> None:
        self.launch_fixture()
        self.proc_process(31501, 10001, ["codex", "app-server", "--managed-daemon"],
                          str(self.codex), self.codex / "bin/codex")
        self.proc_process(31502, 10002, ["codex", "-c", "thread_unload_delay_secs=0",
                                      "app-server", "--listen", "ws://127.0.0.1:41001"],
                          str(self.codex), self.codex / "bin/codex")

        def observe(command: list[str]) -> None:
            if command[:3] == ["agent", "start", "count-unit-review"]:
                self.proc_process(31503, 10003, ["codex", "app-server", "--listen",
                                              "ws://127.0.0.1:41002"],
                                  str(self.codex), self.codex / "bin/codex")
                self.proc_process(31504, 10004, ["codex", "app-server", "--listen", "unix://",
                                              "--analytics-default-enabled", "--managed-daemon"],
                                  str(self.codex), self.codex / "bin/codex")

        self.assert_success(self.invoke_launch(observe))
        record = cast(LaunchStartedResources, json.loads((self.scratch / "started.json").read_text()))
        self.assertEqual(record["codex_daemons_started"], [{"pid": 31504, "start_time": 10004}])
        self.assertEqual(record["codex_daemons_before"], [{"pid": 31501, "start_time": 10001}])
        for pid in (31502, 31503):
            self.assertTrue((self.root / "proc-fixture" / str(pid)).is_dir())

    def test_launch_retry_preserves_original_daemon_baseline_and_accumulates_owned_daemons(self) -> None:
        self.launch_fixture()
        baseline: CodexDaemonIdentity = {"pid": 31401, "start_time": 9981}
        previously_started: CodexDaemonIdentity = {"pid": 31402, "start_time": 9982}
        newly_seen: CodexDaemonIdentity = {"pid": 31403, "start_time": 9983}
        resources: LaunchStartedResources = {
            "systemd_units": ["herdr-trial-server", "herdr-trial-client"],
            "codex_daemons_before": [baseline],
            "codex_daemons_started": [previously_started],
        }
        tracking = self.scratch / "started.json"
        _ = tracking.write_text(json.dumps(resources))
        for daemon in (baseline, previously_started, newly_seen):
            self.proc_process(daemon["pid"], daemon["start_time"], ["codex", "app-server", "--managed-daemon"],
                              str(self.codex), Path("/outside/bin/codex"))

        self.assert_success(self.invoke_launch())

        recorded = cast(LaunchStartedResources, json.loads(tracking.read_text()))
        self.assertEqual(recorded["systemd_units"], resources["systemd_units"])
        self.assertEqual(recorded["codex_daemons_before"], [baseline])
        self.assertCountEqual(recorded["codex_daemons_started"], [previously_started, newly_seen])

    def test_launch_not_ready_dialog_is_logged_and_left_untouched_without_further_starts(self) -> None:
        self.launch_fixture()
        for failed_label, screen in (
            ("slug-unit-review", "Do you trust the files in this folder?\n1. Yes\n2. No"),
            ("slug-unit", "Allow this command to run?\nPermission required"),
        ):
            with self.subTest(label=failed_label, screen=screen):
                self.log.unlink(missing_ok=True)
                (self.scratch / "issues.jsonl").unlink(missing_ok=True)
                result = self.invoke_launch(failed_label=failed_label, screen=screen)
                self.assert_refusal(result, failed_label)
                expected_labels = ["slug-unit-impl", "slug-unit-review"] if failed_label.endswith("-review") else [
                    f"{unit}-{role}" for unit in ("slug-unit", "clamp-unit", "count-unit")
                    for role in ("impl", "review")
                ] + ["slug-unit"]
                self.assertEqual([call[2] for call in self.launch_calls("agent", "start")], expected_labels)
                self.assertEqual(self.launch_calls("agent", "read"), [["agent", "read", failed_label, "--source", "visible"]])
                lines = (self.scratch / "issues.jsonl").read_text().splitlines()
                self.assertEqual(len(lines), 1)
                issue = cast(IssueRecord, json.loads(lines[0]))
                self.assertEqual(issue["session"], failed_label)
                self.assertIn(screen, issue["happened"])
                self.assertEqual(datetime.fromisoformat(issue["time"]).utcoffset(), timedelta(0))
                self.assertTrue(issue["expected"])
                self.assertEqual(self.launch_calls("agent", "prompt"), [
                    ["agent", "prompt", label, self.opening_text(label)] for label in expected_labels[:-1]
                ])
                self.assertFalse(any(call[2:4] in (["pane", "send-keys"], ["pane", "send-text"], ["agent", "send-keys"]) for call in self.calls()))

    def test_scaffold_builds_bare_origin_committed_branches_and_three_worktrees(self) -> None:
        self.scratch.mkdir()
        started = self.scratch / "started.json"
        issues = self.scratch / "issues.jsonl"
        _ = started.write_text('{"systemd_units": ["herdr-trial-server"]}\n')
        _ = issues.write_text('existing phase one evidence\n')
        before = (started.read_bytes(), issues.read_bytes())
        self.assert_success(self.invoke("scaffold"))
        repo = self.scratch / "repo"
        origin = self.scratch / "origin.git"
        self.assertEqual(self.fixture_git(origin, "rev-parse", "--is-bare-repository"), "true")
        initial = self.fixture_git(repo, "rev-parse", "main")
        for reference in ("origin/main", "herdr-fake", "origin/herdr-fake"):
            self.assertEqual(self.fixture_git(repo, "rev-parse", reference), initial)
        self.assertEqual(self.fixture_git(repo, "status", "--porcelain"), "")
        self.assertTrue((repo / "README.md").is_file())
        self.assertTrue((repo / "textkit" / "__init__.py").is_file())
        self.assertTrue((repo / "textkit" / "test_textkit.py").is_file())
        for name in ("slug", "clamp", "count"):
            checkout = self.scratch / "worktrees" / f"{name}-unit"
            self.assertEqual(self.fixture_git(checkout, "branch", "--show-current"),
                             f"herdr-fake-{name}")
            self.assertEqual(self.fixture_git(checkout, "rev-parse", "HEAD"), initial)
            self.assertEqual(self.fixture_git(checkout, "status", "--porcelain"), "")
            self.assertTrue((checkout / "textkit" / "__init__.py").is_file())
        with patch.object(subprocess, "run", side_effect=AssertionError("second scaffold starts nothing")):
            self.assert_refusal(self.invoke("scaffold"), str(repo))
        self.assertEqual(self.fixture_git(repo, "rev-parse", "main"), initial)
        baseline = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "textkit"],
                                  cwd=repo, capture_output=True, text=True, check=False, timeout=10)
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        self.assertIn("Ran 1 test", baseline.stderr)
        self.assertEqual((started.read_bytes(), issues.read_bytes()), before)
        self.assertEqual(self.calls(), [])

    def test_scaffold_writes_production_rules_and_delegate_ready_unit_plans(self) -> None:
        self.assert_success(self.invoke("scaffold"))
        repo = self.scratch / "repo"
        plans = repo / "docs" / "plans"
        production = (plans / "herdr-fake-production.md").read_text()
        for text in (
            "> **Status: PRODUCTION — running.**", "## Production Context", "## Units",
            "**Repository:**", str(repo), "**Merge branch:**", "herdr-fake",
            "**Showrunner checkout:**", "**Showrunner session:**", "natedev",
            "**Log:**", "docs/plans/herdr-fake-log.md", "**User zone:**", "America/New_York",
            "**Updates:**", "on demand", "**Merge tests:**", "python3 -m unittest discover -s textkit",
            "**Push:**", "git", "**Promote:**", "none", "herdr session herdr-trial",
            "/home/natepiano/worktrees/claude-build-followups-herdr-trial/scripts/herdr/trial.py",
            "tmux", "add_unit.py", "trial.py issue", "never promote or wrap",
            "never answer a trust or permission prompt",
        ):
            self.assertIn(text, production)
        functions = {"slug": "slugify", "clamp": "clamp", "count": "word_count"}
        for name, function in functions.items():
            unit = f"{name}-unit"
            filename = f"herdr-fake-{name}.md"
            self.assertIn(filename, production)
            self.assertIn(unit, production)
            self.assertIn(str(self.scratch / "worktrees" / unit), production)
            self.assertIn(f"herdr-fake-{name}", production)
            plan = (plans / filename).read_text()
            for text in ("Delegation Context", "Phase 1", "Work Order", function,
                         "textkit/__init__.py", "test", f"{unit}-impl", f"{unit}-review",
                         f"trial.py work {unit}", f"trial.py wait {unit}"):
                self.assertIn(text, plan)
            self.assertNotIn("Phase 2", plan)
            self.assertIn(filename, self.fixture_git(repo, "ls-tree", "-r", "--name-only", "HEAD"))
        self.trust_configs({str(repo): True}, {str(repo): "trusted"})
        for checkout in (repo, *(self.scratch / "worktrees" / f"{name}-unit"
                                  for name in functions)):
            self.assert_success(self.invoke("trust-check", str(checkout)))

    def test_scaffold_refuses_existing_repo_before_running_git_or_replacing_files(self) -> None:
        repo = self.scratch / "repo"
        repo.mkdir(parents=True)
        marker = repo / "keep.txt"
        _ = marker.write_text("do not replace\n")
        with patch.object(subprocess, "run", side_effect=AssertionError("refusal must start nothing")):
            self.assert_refusal(self.invoke("scaffold"), str(repo))
        self.assertEqual(marker.read_text(), "do not replace\n")
        self.assertFalse((self.scratch / "origin.git").exists())
        self.assertEqual(self.calls(), [])

    def test_rendered_production_push_parses_as_git_with_the_real_merge_parser(self) -> None:
        self.assert_success(self.invoke("scaffold"))
        document = self.scratch / "repo" / "docs" / "plans" / "herdr-fake-production.md"
        result = self.production_parser(document,
                                        "field = optional_field(lines, 'Push')\n" +
                                        "assert isinstance(field, FieldPresent)\n" +
                                        "print(cell_value(field.text))\n")
        self.assert_success(result)
        self.assertEqual(result.stdout, "git\n")

    def test_rendered_production_name_is_accepted_by_the_real_lifecycle_parser(self) -> None:
        self.assert_success(self.invoke("scaffold"))
        document = self.scratch / "repo" / "docs" / "plans" / "herdr-fake-production.md"
        result = self.production_parser(document, "print(production_name(lines))\n")
        self.assert_success(result)
        self.assertEqual(result.stdout, "herdr-fake\n")

    def test_work_refuses_a_worker_not_interactively_ready_without_submitting_or_recording(self) -> None:
        pane, work_order, report = self.work_fixture()
        self.worker_state(pane, state="idle", ready=False)
        _ = report.write_text("preserve the previous report\n")
        result = self.invoke("work", "slug-unit", "impl", str(work_order))
        self.assertEqual(result.code, 2, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertFalse((self.scratch / "work" / "slug-unit-impl.json").exists())
        self.assertEqual(report.read_text(), "preserve the previous report\n")
        issue = self.single_issue("slug-unit-impl")
        self.assertEqual(issue["expected"], "worker is ready for its Work Order")
        self.assertIn("idle", issue["happened"])
        self.assertIn("false", issue["happened"].casefold())
        self.assertEqual(self.calls(), [
            herdr_args("pane", "list"), herdr_args("agent", "get", pane),
        ])

    def test_work_refuses_a_blocked_worker_even_when_interactive_readiness_is_true(self) -> None:
        pane, work_order, _ = self.work_fixture()
        self.worker_state(pane, state="blocked", ready=True)
        result = self.invoke("work", "slug-unit", "impl", str(work_order))
        self.assertEqual(result.code, 2, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertFalse((self.scratch / "work" / "slug-unit-impl.json").exists())
        issue = self.single_issue("slug-unit-impl")
        self.assertEqual(issue["expected"], "worker is ready for its Work Order")
        self.assertIn("blocked", issue["happened"])
        self.assertIn("true", issue["happened"].casefold())
        self.assertEqual(self.calls(), [
            herdr_args("pane", "list"), herdr_args("agent", "get", pane),
        ])

    def test_work_removes_assignment_and_logs_when_one_prompt_never_starts_a_turn(self) -> None:
        pane, work_order, report = self.work_fixture()
        milliseconds = 23
        wait = self.turn_start_timeout_response(pane, milliseconds)
        prompt = herdr_args("agent", "prompt", pane,
                           f"Read and carry out {work_order}. When done, write your report to {report}.")
        with patch.dict(os.environ, {"HERDR_TRIAL_TURN_START_TIMEOUT": str(milliseconds)}):
            result = self.invoke("work", "slug-unit", "impl", str(work_order))
        self.assert_refusal(result, "slug-unit-impl")
        self.assertFalse(report.exists())
        self.assertFalse((self.scratch / "work" / "slug-unit-impl.json").exists())
        issue = self.single_issue("slug-unit-impl")
        self.assertEqual(issue["expected"], "worker starts a turn on its Work Order")
        self.assertEqual(issue["happened"], "prompt submitted, no turn started within 23 ms")
        self.assertEqual(self.calls(), [
            herdr_args("pane", "list"), herdr_args("agent", "get", pane), prompt, wait,
        ])

    def test_work_accepts_a_fast_report_when_working_was_missed_by_the_start_wait(self) -> None:
        pane, work_order, report = self.work_fixture()
        milliseconds = 31
        wait = self.turn_start_timeout_response(pane, milliseconds)
        prompt = herdr_args("agent", "prompt", pane,
                           f"Read and carry out {work_order}. When done, write your report to {report}.")
        # The recorder creates the report only after work removed any stale report.
        response = cast(CommandResponse, self.responses[" ".join(wait)])
        response["write_files"] = {str(report): "fast turn finished\n"}
        with patch.dict(os.environ, {"HERDR_TRIAL_TURN_START_TIMEOUT": str(milliseconds)}):
            result = self.invoke("work", "slug-unit", "impl", str(work_order))
        self.assert_success(result)
        self.assertTrue(report.is_file(), "working timeout must accept the newly written report")
        self.assertEqual(report.read_text(), "fast turn finished\n")
        self.assertEqual(self.assigned_files(), {"slug-unit-impl": str(work_order)})
        self.assertFalse((self.scratch / "issues.jsonl").exists())
        self.assertEqual(self.calls(), [
            herdr_args("pane", "list"), herdr_args("agent", "get", pane), prompt, wait,
        ])

    def test_work_keeps_assignment_after_a_ready_worker_starts_working_with_one_prompt(self) -> None:
        pane, work_order, _ = self.work_fixture()
        for state in ("idle", "done"):
            with self.subTest(state=state):
                self.log.unlink(missing_ok=True)
                self.worker_state(pane, state=state)
                result = self.invoke("work", "slug-unit", "impl", str(work_order))
                self.assert_success(result)
                self.assertEqual(self.assigned_files(), {"slug-unit-impl": str(work_order)})
                self.assertFalse((self.scratch / "issues.jsonl").exists())
                calls = self.calls()
                self.assertEqual(calls[:2], [
                    herdr_args("pane", "list"), herdr_args("agent", "get", pane),
                ])
                self.assertEqual(sum(call[2:4] == ["agent", "prompt"] for call in calls), 1)
                self.assertEqual(calls[-1], herdr_args("agent", "wait", pane, "--until", "working",
                                                     "--timeout", "60000"))
                self.assertEqual(len(calls), 4)

    def test_work_rejects_an_invalid_turn_start_deadline_before_submitting_input(self) -> None:
        _, work_order, _ = self.work_fixture()
        for value in ("-1", "1.5", "not-a-number"):
            with self.subTest(value=value):
                self.log.unlink(missing_ok=True)
                with patch.dict(os.environ, {"HERDR_TRIAL_TURN_START_TIMEOUT": value}):
                    result = self.invoke("work", "slug-unit", "impl", str(work_order))
                self.assertEqual(result.code, 2, result.stderr)
                self.assertEqual(len(result.stderr.splitlines()), 1)
                self.assertIn("HERDR_TRIAL_TURN_START_TIMEOUT", result.stderr)
                self.assertFalse((self.scratch / "work" / "slug-unit-impl.json").exists())
                self.assertFalse(any(call[2:4] == ["agent", "prompt"] for call in self.calls()))

    def test_work_logs_a_blocked_turn_start_without_sending_additional_input(self) -> None:
        pane, work_order, _ = self.work_fixture()
        response: SettledAgentResponse = {"ok": True, "result": {"agent": {
            "pane_id": pane, "agent": "codex", "agent_status": "blocked",
        }}}
        wait = herdr_args("agent", "wait", pane, "--until", "working", "--timeout", "60000")
        self.configure(wait, json.dumps(response))
        result = self.invoke("work", "slug-unit", "impl", str(work_order))
        self.assert_blocked_issue(result, "slug-unit-impl")
        calls = self.calls()
        self.assertEqual(sum(call[2:4] == ["agent", "prompt"] for call in calls), 1)
        self.assertEqual(calls[-1], wait)
        self.assertFalse(any(call[3] in {"send-text", "send-keys", "run"} for call in calls))

    def test_concurrent_worker_dispatches_preserve_both_assignments_for_separate_waits(self) -> None:
        units = {"slug-unit": "w38:p71", "clamp-unit": "w38:p81"}
        self.pane_list([{"pane_id": pane, "label": f"{unit}-impl"}
                        for unit, pane in units.items()])
        orders: dict[str, Path] = {}
        for unit in units:
            order = self.root / f"{unit}.md"
            _ = order.write_text("tiny simultaneous work order\n")
            orders[unit] = order
        _ = self.response_file.write_text(json.dumps(self.responses))
        launcher = [sys.executable, "-B", str(Path(__file__).with_name("trial.py").resolve()), "work"]
        with subprocess.Popen([*launcher, "slug-unit", "impl", str(orders["slug-unit"])],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as first:
            with subprocess.Popen([*launcher, "clamp-unit", "impl", str(orders["clamp-unit"])],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as second:
                try:
                    first_stdout, first_stderr = first.communicate(timeout=10)
                    second_stdout, second_stderr = second.communicate(timeout=10)
                    self.assert_success(InvocationResult(first.returncode, first_stdout, first_stderr))
                    self.assert_success(InvocationResult(second.returncode, second_stdout, second_stderr))
                finally:
                    for process in (first, second):
                        if process.poll() is None:
                            process.kill()
        for unit, pane in units.items():
            with self.subTest(unit=unit):
                self.assertTrue((self.scratch / "work" / f"{unit}-impl.json").is_file())
                report = Path(str(orders[unit]) + ".done")
                _ = report.write_text(f"{unit} completed\n")
                self.settled_response(pane, "done")
                result = self.invoke("wait", unit, "impl")
                self.assert_success(result)
                self.assertEqual(result.stdout, str(report) + "\n")
        self.assertFalse((self.scratch / "work.json").exists())
        prompts = [call for call in self.calls() if call[2:4] == ["agent", "prompt"]]
        self.assertEqual({call[4] for call in prompts}, set(units.values()))
        self.assertEqual(len(prompts), 2)

    def test_work_records_file_removes_stale_report_and_submits_literal_prompt_once(self) -> None:
        label = "slug-unit-impl"
        work_order = self.root / "work order ' café.md"
        report = Path(str(work_order) + ".done")
        _ = work_order.write_text("tiny work order\n")
        _ = report.write_text("stale report\n")
        other_file = self.root / "other.md"
        self.assignment("count-unit-review", other_file)
        self.pane_list([{"pane_id": "w38:p71", "label": label}])
        prompt = f"Read and carry out {work_order}. When done, write your report to {report}."
        expected = herdr_args("agent", "prompt", "w38:p71", prompt)
        original_run = subprocess.run

        def check_dispatch_order(argv: list[str], *, capture_output: bool, text: bool,
                                 check: bool, timeout: float) -> subprocess.CompletedProcess[str]:
            if argv == [str(self.fake), *expected]:
                self.assertFalse(report.exists())
                self.assertEqual(self.assigned_files().get(label), str(work_order))
            return original_run(argv, capture_output=capture_output, text=text,
                                check=check, timeout=timeout)

        with patch.object(subprocess, "run", side_effect=check_dispatch_order):
            self.assert_success(self.invoke("work", "slug-unit", "impl", str(work_order)))
        self.assertFalse(report.exists())
        self.assertEqual(self.assigned_files(), {
            "count-unit-review": str(other_file), label: str(work_order),
        })
        self.assertEqual(self.calls(), [
            herdr_args("pane", "list"), herdr_args("agent", "get", "w38:p71"), expected,
            herdr_args("agent", "wait", "w38:p71", "--until", "working", "--timeout", "60000"),
        ])

    def test_work_and_wait_use_role_labels_and_refuse_invalid_roles_without_input(self) -> None:
        for action in ("work", "wait"):
            arguments = [action, "slug-unit", "director"]
            if action == "work":
                arguments.append(str(self.root / "order.md"))
            with self.subTest(action=action):
                result = self.invoke(*arguments)
                self.assertEqual(result.code, 2, result.stderr)
                self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.scratch.exists())

    def test_wait_in_separate_process_uses_recorded_assignment_and_prints_settled_report_path(self) -> None:
        label = "clamp-unit-review"
        work_order = self.root / "review.md"
        report = Path(str(work_order) + ".done")
        _ = work_order.write_text("review the tiny change\n")
        self.pane_list([{"pane_id": "w38:p82", "label": label}])
        self.assert_success(self.invoke("work", "clamp-unit", "review", str(work_order)))
        _ = report.write_text("Review complete: the change passes.\n")
        for state in ("idle", "done"):
            with self.subTest(state=state):
                self.settled_response("w38:p82", state)
                result = self.invoke_cli("wait", "clamp-unit", "review")
                self.assert_success(result)
                self.assertEqual(result.stdout, str(report) + "\n")
        prompt_calls = [call for call in self.calls() if call[2:4] == ["agent", "prompt"]]
        self.assertEqual(len(prompt_calls), 1)
        self.assertEqual(self.calls()[-2:], [
            herdr_args("pane", "list"),
            herdr_args("agent", "wait", "w38:p82", "--timeout", "1800000"),
        ])

    def test_wait_without_assignment_refuses_before_contacting_herdr(self) -> None:
        for has_map in (False, True):
            with self.subTest(has_map=has_map):
                if has_map:
                    self.assignment("count-unit-impl", self.root / "other.md")
                self.assert_refusal(self.invoke("wait", "slug-unit", "impl"), "slug-unit-impl")
                self.assertEqual(self.calls(), [])

    def test_relative_work_file_remains_resolvable_when_wait_runs_from_another_directory(self) -> None:
        work_order = self.project / "relative.md"
        report = Path(str(work_order) + ".done")
        _ = work_order.write_text("work from the worker checkout\n")
        self.pane_list([{"pane_id": "w38:p93", "label": "count-unit-impl"}])
        self.assert_success(self.invoke_cli("work", "count-unit", "impl", "relative.md"))
        self.assertEqual(self.assigned_files().get("count-unit-impl"), str(work_order))
        _ = report.write_text("finished\n")
        self.settled_response("w38:p93", "done")
        result = self.invoke("wait", "count-unit", "impl")
        self.assert_success(result)
        self.assertEqual(result.stdout, str(report) + "\n")
        self.assertEqual(sum(call[2:4] == ["agent", "prompt"] for call in self.calls()), 1)

    def test_wait_keeps_waiting_after_initial_idle_until_report_or_deadline_without_resubmitting(self) -> None:
        work_order = self.root / "order.md"
        report = Path(str(work_order) + ".done")
        _ = work_order.write_text("work that has not started its turn yet\n")
        self.pane_list([{"pane_id": "w38:p93", "label": "count-unit-impl"}])
        self.assert_success(self.invoke("work", "count-unit", "impl", str(work_order)))
        states = iter(("idle", "working", "done"))
        waits: list[list[str]] = []
        original_run = subprocess.run

        def finish_after_start(argv: list[str], *, capture_output: bool, text: bool,
                               check: bool, timeout: float) -> subprocess.CompletedProcess[str]:
            if argv[3:5] == ["agent", "wait"]:
                state = next(states)
                waits.append(argv[1:])
                self.assertEqual("--until" in argv, state == "working")
                if state == "working":
                    self.assertEqual(argv[argv.index("--until") + 1], "working")
                self.assertFalse(report.exists())
                response: SettledAgentResponse = {"ok": True, "result": {"agent": {
                    "pane_id": "w38:p93", "agent": "codex", "agent_status": state,
                }}}
                self.configure(argv[1:], json.dumps(response))
                _ = self.response_file.write_text(json.dumps(self.responses))
                result = original_run(argv, capture_output=capture_output, text=text,
                                      check=check, timeout=timeout)
                if state == "done":
                    _ = report.write_text("report arrives after the turn starts\n")
                return result
            return original_run(argv, capture_output=capture_output, text=text,
                                check=check, timeout=timeout)

        with patch.object(subprocess, "run", side_effect=finish_after_start):
            result = self.invoke("wait", "count-unit", "impl")
        self.assert_success(result)
        self.assertEqual(result.stdout, str(report) + "\n")
        self.assertEqual(len(waits), 3)
        remaining = [int(call[call.index("--timeout") + 1]) for call in waits]
        self.assertTrue(all(0 < milliseconds <= 1800000 for milliseconds in remaining))
        self.assertEqual(remaining, sorted(remaining, reverse=True))
        report.unlink()
        self.settled_response("w38:p93", "idle", timeout=0)
        with patch.dict(os.environ, {"HERDR_TRIAL_WAIT_TIMEOUT": "0"}):
            expired = self.invoke("wait", "count-unit", "impl")
        self.assert_refusal(expired)
        self.assertRegex(expired.stderr.casefold(), "deadline|timed out|timeout|report missing")
        self.assertEqual(sum(call[2:4] == ["agent", "prompt"] for call in self.calls()), 1)

    def test_wait_deadline_exceeds_ten_seconds_and_prompt_is_not_resubmitted(self) -> None:
        work_order = self.root / "long-order.md"
        _ = work_order.write_text("takes longer than ten seconds\n")
        self.pane_list([{"pane_id": "w38:p71", "label": "slug-unit-impl"}])
        self.assert_success(self.invoke("work", "slug-unit", "impl", str(work_order)))
        _ = Path(str(work_order) + ".done").write_text("long work finished\n")
        dispatch_call_count = len(self.calls())
        original_run = subprocess.run
        deadlines: list[float] = []

        def emulate_long_wait(argv: list[str], *, capture_output: bool, text: bool,
                              check: bool, timeout: float) -> subprocess.CompletedProcess[str]:
            if argv[3:5] == ["agent", "wait"]:
                milliseconds = int(argv[-1])
                deadlines.append(timeout)
                # Model the long wait at its process boundary without sleeping.
                if timeout <= milliseconds / 1000:
                    raise subprocess.TimeoutExpired(argv, timeout)
            return original_run(argv, capture_output=capture_output, text=text,
                                check=check, timeout=timeout)

        for milliseconds in (1800000, 25000):
            with self.subTest(milliseconds=milliseconds):
                self.settled_response("w38:p71", "done", milliseconds)
                with patch.dict(os.environ, {"HERDR_TRIAL_WAIT_TIMEOUT": str(milliseconds)}):
                    with patch.object(subprocess, "run", side_effect=emulate_long_wait):
                        self.assert_success(self.invoke("wait", "slug-unit", "impl"))
        self.assertEqual(len(deadlines), 2)
        self.assertGreater(deadlines[0], 1800)
        self.assertGreater(deadlines[1], 25)
        self.assertEqual(sum(call[2:4] == ["agent", "prompt"] for call in self.calls()), 1)
        self.assertFalse(any("--until" in call for call in self.calls()[dispatch_call_count:]))

    def test_work_agent_blocked_refusal_logs_one_issue_without_raw_input(self) -> None:
        label = "slug-unit-review"
        work_order = self.root / "review.md"
        _ = work_order.write_text("review\n")
        self.pane_list([{"pane_id": "w38:p72", "label": label}])
        response: AgentErrorResponse = {"ok": False, "error": {
            "code": "agent_blocked", "message": "agent is blocked by a permission dialog",
        }}
        prompt = (f"Read and carry out {work_order}. When done, write your report to "
                  f"{work_order}.done.")
        arguments = herdr_args("agent", "prompt", "w38:p72", prompt)
        self.configure(arguments, code=1, stderr=json.dumps(response))
        self.assert_blocked_issue(self.invoke("work", "slug-unit", "review", str(work_order)), label)
        self.assertEqual(self.calls(), [
            herdr_args("pane", "list"), herdr_args("agent", "get", "w38:p72"), arguments,
        ])

    def test_wait_settled_blocked_logs_issue_even_when_a_report_exists(self) -> None:
        label = "count-unit-review"
        work_order = self.root / "review.md"
        self.assignment(label, work_order)
        _ = Path(str(work_order) + ".done").write_text("do not accept while blocked\n")
        self.pane_list([{"pane_id": "w38:p94", "label": label}])
        self.settled_response("w38:p94", "blocked")
        result = self.invoke("wait", "count-unit", "review")
        self.assert_blocked_issue(result, label)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.calls(), [
            herdr_args("pane", "list"),
            herdr_args("agent", "wait", "w38:p94", "--timeout", "1800000"),
        ])

    def test_wait_agent_blocked_error_logs_one_issue_and_never_submits_input(self) -> None:
        label = "clamp-unit-impl"
        self.assignment(label, self.root / "order.md")
        self.pane_list([{"pane_id": "w38:p81", "label": label}])
        response: AgentErrorResponse = {"ok": False, "error": {
            "code": "agent_blocked", "message": "blocked while waiting",
        }}
        arguments = herdr_args("agent", "wait", "w38:p81", "--timeout", "1800000")
        self.configure(arguments, code=1, stderr=json.dumps(response))
        self.assert_blocked_issue(self.invoke("wait", "clamp-unit", "impl"), label)
        self.assertEqual(self.calls(), [herdr_args("pane", "list"), arguments])

    def test_wait_timeout_refuses_without_resending_the_prompt(self) -> None:
        label = "clamp-unit-review"
        work_order = self.root / "review.md"
        self.assignment(label, work_order)
        self.pane_list([{"pane_id": "w38:p82", "label": label}])
        response: AgentErrorResponse = {"ok": False, "error": {
            "code": "timeout", "message": "agent wait timed out",
        }}
        arguments = herdr_args("agent", "wait", "w38:p82", "--timeout", "1800000")
        self.configure(arguments, code=1, stderr=json.dumps(response))
        result = self.invoke("wait", "clamp-unit", "review")
        self.assert_refusal(result)
        self.assertIn("timed out", result.stderr)
        self.assertEqual(self.assigned_files(), {label: str(work_order)})
        self.assertEqual(self.calls(), [herdr_args("pane", "list"), arguments])

    def test_pane_id_finds_exact_label_and_uses_opaque_id(self) -> None:
        self.pane_list([
            {"pane_id": "w4:p9", "label": "smoke-other"},
            {"pane_id": "w27:p61", "label": "smoke"},
            {"pane_id": "w4:p10", "label": None},
        ])
        result = self.invoke("pane-id", "smoke")
        self.assert_success(result)
        self.assertEqual(result.stdout, "w27:p61\n")
        self.assertEqual(self.calls(), [herdr_args("pane", "list")])

    def test_pane_id_zero_matches_names_label(self) -> None:
        self.pane_list([{"pane_id": "w4:p9", "label": "another"}])
        result = self.invoke("pane-id", "missing-label")
        self.assert_refusal(result, "missing-label")
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.calls(), [herdr_args("pane", "list")])

    def test_pane_id_two_matches_refuses_ambiguity(self) -> None:
        self.pane_list([
            {"pane_id": "w1:p1", "label": "smoke"},
            {"pane_id": "w9:p8", "label": "smoke"},
        ])
        result = self.invoke("pane-id", "smoke")
        self.assert_refusal(result, "smoke")
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.calls(), [herdr_args("pane", "list")])

    def test_open_refuses_an_existing_label_without_creating_or_running_anything(self) -> None:
        cases: tuple[list[PaneDetails], ...] = (
            [{"pane_id": "w27:p61", "label": "smoke"}],
            [{"pane_id": "w27:p61", "label": "smoke"},
             {"pane_id": "w8:p9", "label": "smoke"}],
        )
        for panes in cases:
            with self.subTest(matches=len(panes)):
                if self.log.exists():
                    self.log.unlink()
                self.pane_list(panes)
                result = self.invoke("open", "smoke", "--command", "never run")
                self.assert_refusal(result, "smoke")
                self.assertEqual(result.stdout, "")
                self.assertEqual(self.calls(), [herdr_args("pane", "list")])

    def test_open_labels_returned_root_pane_and_runs_only_requested_command(self) -> None:
        cases: tuple[tuple[NewRootPane, bool], ...] = (
            ({"pane_id": "w27:p61"}, True),
            ({"pane_id": "w27:p61", "label": None}, True),
            ({"pane_id": "w27:p61", "label": "smoke"}, False),
        )
        for root_pane, explicit_options in cases:
            with self.subTest(root_pane=root_pane, explicit_options=explicit_options):
                if self.log.exists():
                    self.log.unlink()
                self.pane_list([{"pane_id": "w4:p9", "label": "smoke-other"}])
                cwd = str(self.project) if explicit_options else os.getcwd()
                create = herdr_args("workspace", "create", "--label", "smoke",
                                    "--cwd", cwd, "--no-focus")
                response: WorkspaceCreationResponse = {"ok": True, "result": {
                    "workspace": {"workspace_id": "w27", "label": "smoke"},
                    "tab": {"tab_id": "w27:t42"}, "root_pane": root_pane,
                }}
                self.configure(create, json.dumps(response))
                arguments = ["open", "smoke"]
                expected = [herdr_args("pane", "list"), create]
                if root_pane.get("label") != "smoke":
                    expected.append(herdr_args("pane", "rename", "w27:p61", "smoke"))
                if explicit_options:
                    command = "zsh -ic 'echo $HOME ; café'"
                    arguments.extend(["--cwd", cwd, "--command", command])
                    expected.append(herdr_args("pane", "run", "w27:p61", command))
                result = self.invoke(*arguments)
                self.assert_success(result)
                self.assertEqual(result.stdout, "w27:p61\n")
                self.assertEqual(self.calls(), expected)

    def test_close_targets_labelled_pane_and_preserves_workspace_with_other_panes(self) -> None:
        target: PaneDetails = {
            "pane_id": "w27:p61", "label": "smoke", "workspace_id": "w27",
        }
        sibling: PaneDetails = {
            "pane_id": "w27:p84", "label": "keep", "workspace_id": "w27",
        }
        unrelated: PaneDetails = {
            "pane_id": "w9:p2", "label": "elsewhere", "workspace_id": "w9",
        }
        for remaining in ([], [sibling]):
            with self.subTest(other_panes=len(remaining)):
                if self.log.exists():
                    self.log.unlink()
                self.pane_list([unrelated, target, *remaining])
                response: PaneCloseResponse = {"ok": True, "result": {"type": "ok"}}
                self.configure(herdr_args("pane", "close", "w27:p61"), json.dumps(response))
                result = self.invoke("close", "smoke")
                self.assert_success(result)
                self.assertEqual(result.stdout, "")
                # Live 0.9.0 removes the workspace when its last pane closes;
                # an explicit workspace close would also destroy a surviving sibling.
                self.assertEqual(self.calls(), [
                    herdr_args("pane", "list"), herdr_args("pane", "close", "w27:p61"),
                ])

    def test_read_prints_plain_pane_text_from_resolved_id_and_visible_source(self) -> None:
        self.pane_list([{"pane_id": "w27:p61", "label": "smoke"}])
        self.configure(herdr_args("pane", "read", "w27:p61", "--source", "visible"),
                       "echo $HOME ; ok\n/home/example\n")
        result = self.invoke("read", "smoke")
        self.assert_success(result)
        self.assertEqual(result.stdout.rstrip("\n"), "echo $HOME ; ok\n/home/example")
        self.assertEqual(self.calls(), [
            herdr_args("pane", "list"),
            herdr_args("pane", "read", "w27:p61", "--source", "visible"),
        ])

    def test_type_preserves_literal_text_and_sends_enter_after_text(self) -> None:
        self.pane_list([{"pane_id": "w27:p61", "label": "smoke"}])
        text = "echo $HOME ; ok\n'quoted' café $(never-run)"
        self.assert_success(self.invoke("type", "smoke", text))
        self.assertEqual(self.calls(), [
            herdr_args("pane", "list"),
            herdr_args("pane", "send-text", "w27:p61", text),
            herdr_args("pane", "send-keys", "w27:p61", "enter"),
        ])

    def test_read_and_type_refuse_missing_or_ambiguous_labels_before_input(self) -> None:
        pane_cases: list[list[PaneDetails]] = [[], [
            {"pane_id": "w1:p1", "label": "smoke"},
            {"pane_id": "w9:p8", "label": "smoke"},
        ]]
        for panes in pane_cases:
            for arguments in (("read", "smoke"), ("type", "smoke", "do not submit")):
                with self.subTest(panes=panes, arguments=arguments):
                    if self.log.exists():
                        self.log.unlink()
                    self.pane_list(panes)
                    self.assert_refusal(self.invoke(*arguments), "smoke")
                    self.assertEqual(self.calls(), [herdr_args("pane", "list")])

    def test_type_does_not_press_enter_if_sending_text_fails(self) -> None:
        self.pane_list([{"pane_id": "w27:p61", "label": "smoke"}])
        self.configure(herdr_args("pane", "send-text", "w27:p61", "hello"), code=1,
                       stderr='{"error":{"code":"pane_closed","message":"pane closed"}}\n')
        result = self.invoke("type", "smoke", "hello")
        self.assert_refusal(result)
        self.assertEqual(self.calls(), [
            herdr_args("pane", "list"),
            herdr_args("pane", "send-text", "w27:p61", "hello"),
        ])

    def test_agents_joins_labels_by_id_and_prints_kind_and_state(self) -> None:
        self.pane_list([
            {"pane_id": "w8:p7", "label": "director"},
            {"pane_id": "w8:p9", "label": "worker"},
        ])
        response: AgentListResponse = {"ok": True, "result": {"agents": [
            {"pane_id": "w8:p9", "agent": "codex", "agent_status": "blocked"},
            {"pane_id": "w8:p7", "agent": "claude", "agent_status": "idle"},
        ]}}
        self.configure(herdr_args("agent", "list"), json.dumps(response))
        result = self.invoke("agents")
        self.assert_success(result)
        self.assertCountEqual(result.stdout.splitlines(), [
            "worker\tcodex\tblocked", "director\tclaude\tidle",
        ])
        self.assertCountEqual(self.calls(), [herdr_args("pane", "list"),
                                            herdr_args("agent", "list")])

    def test_agents_empty_list_prints_no_lines(self) -> None:
        self.pane_list([])
        response: AgentListResponse = {"ok": True, "result": {"agents": []}}
        self.configure(herdr_args("agent", "list"), json.dumps(response))
        result = self.invoke("agents")
        self.assert_success(result)
        self.assertEqual(result.stdout, "")

    def test_fake_recorder_exact_argv_default_and_exit_code(self) -> None:
        arguments = ["pane", "send-text", "w9:p4", "echo '$HOME'; café\nsecond line"]
        self.configure(arguments, "configured output\n", 7)
        _ = self.response_file.write_text(json.dumps(self.responses))
        result = subprocess.run([str(self.fake), *arguments], capture_output=True,
                                text=True, check=False)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, "configured output\n")
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.calls(), [arguments])
        self.responses["default"] = {"stdout": "default output", "code": 3}
        _ = self.response_file.write_text(json.dumps(self.responses))
        result = subprocess.run([str(self.fake), "unconfigured"], capture_output=True,
                                text=True, check=False)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, "default output")
        self.assertEqual(self.calls(), [arguments, ["unconfigured"]])

    def test_server_start_launches_named_unit_then_records_it(self) -> None:
        status = herdr_args("status", "server")
        self.responses[" ".join(status)] = [
            {"stdout": "", "code": 1},
            {"stdout": "session: herdr-trial\nstatus: running\n", "code": 0},
        ]
        result = self.invoke("server", "start")
        self.assert_success(result)
        launch = ["--user", "--unit=herdr-trial-server", str(self.fake),
                  *herdr_args("server")]
        calls = self.calls()
        self.assertEqual(calls[0], status)
        self.assertEqual(calls.count(launch), 1)
        self.assertGreaterEqual(calls.count(status), 2)
        self.assertGreater(calls.index(launch), calls.index(status))
        self.assertEqual(self.started_units(), {"systemd_units": ["herdr-trial-server"]})

    def test_unreadable_started_record_refuses_before_launching_any_unit(self) -> None:
        self.scratch.mkdir()
        path = self.scratch / "started.json"
        self.configure(herdr_args("status", "server"), "status: not running\n")
        for unreadable in (False, True):
            if unreadable:
                path.unlink()
                path.mkdir()
            else:
                _ = path.write_text("{")
            for arguments in (("client", "open"), ("server", "start")):
                with self.subTest(unreadable=unreadable, arguments=arguments):
                    if self.log.exists():
                        self.log.unlink()
                    result = self.invoke(*arguments)
                    self.assert_refusal(result, "started.json")
                    self.assertFalse(any("--unit=herdr-trial-client" in call
                                         or "--unit=herdr-trial-server" in call
                                         for call in self.calls()), self.calls())
                    if not unreadable:
                        self.assertEqual(path.read_text(), "{")
                    else:
                        self.assertTrue(path.is_dir())

    def test_up_starts_server_before_client_and_reuses_recorded_active_client(self) -> None:
        status = herdr_args("status", "server")
        self.responses[" ".join(status)] = [
            {"stdout": "status: not running\n", "code": 0},
            {"stdout": "status: running\n", "code": 0},
        ]
        self.configure(["--user", "is-active", "herdr-trial-client"], "active\n")
        server_launch = ["--user", "--unit=herdr-trial-server", str(self.fake),
                         *herdr_args("server")]
        client_launch = ["--user", "--unit=herdr-trial-client", "/fixture/ghostty",
                         "--gtk-single-instance=false", "--title=herdr-trial", "-e",
                         str(self.fake), *herdr_args()]
        self.assert_success(self.invoke("up", "--desktop", "6"))
        first_calls = self.calls()
        self.assertEqual(first_calls.count(server_launch), 1)
        self.assertEqual(first_calls.count(client_launch), 1)
        self.assertLess(first_calls.index(server_launch), first_calls.index(client_launch))
        self.assertGreaterEqual(first_calls[:first_calls.index(client_launch)].count(status), 2)
        self.assertIn(["set_desktop_for_window", "window-17", "6"], first_calls)
        self.assertEqual(self.started_units(), {
            "systemd_units": ["herdr-trial-server", "herdr-trial-client"],
        })
        before = (self.scratch / "started.json").read_bytes()
        self.assert_success(self.invoke("up", "--desktop", "6"))
        repeated_calls = self.calls()[len(first_calls):]
        self.assertNotIn(server_launch, repeated_calls)
        self.assertNotIn(client_launch, repeated_calls)
        self.assertIn(["--user", "is-active", "herdr-trial-client"], repeated_calls)
        self.assertIn(["search", "--name", "^herdr-trial$"], repeated_calls)
        self.assertIn(["set_desktop_for_window", "window-17", "6"], repeated_calls)
        self.assertEqual((self.scratch / "started.json").read_bytes(), before)

    def test_server_stop_timeout_still_stops_active_unit_and_reports_failure(self) -> None:
        stop = [str(self.fake), *herdr_args("server", "stop")]
        original_run = subprocess.run

        def run_with_stop_timeout(argv: list[str], *, capture_output: bool,
                                  text: bool, check: bool,
                                  timeout: float) -> subprocess.CompletedProcess[str]:
            if argv == stop:
                with self.log.open("a") as output:
                    _ = output.write(json.dumps(argv[1:]) + "\n")
                raise subprocess.TimeoutExpired(argv, timeout)
            return original_run(argv, capture_output=capture_output, text=text,
                                check=check, timeout=timeout)

        self.configure(["--user", "is-active", "herdr-trial-server"], "active\n")
        with patch.object(subprocess, "run", side_effect=run_with_stop_timeout):
            result = self.invoke("server", "stop")
        self.assert_refusal(result, "herdr-trial")
        self.assertIn("timed out", result.stderr.casefold())
        self.assertEqual(self.calls(), [
            herdr_args("server", "stop"),
            ["--user", "is-active", "herdr-trial-server"],
            ["--user", "stop", "herdr-trial-server"],
        ])

    def test_server_start_when_answering_changes_nothing(self) -> None:
        self.configure(herdr_args("status", "server"), "session: herdr-trial\nstatus: running\n")
        result = self.invoke("server", "start")
        self.assert_success(result)
        self.assertEqual(self.calls(), [herdr_args("status", "server")])
        self.assertFalse(self.scratch.exists())

    def test_server_status_exit_zero_without_running_still_launches_and_waits(self) -> None:
        # Live 0.9.0 reports "not running" with a successful process exit.
        status = herdr_args("status", "server")
        self.responses[" ".join(status)] = [
            {"stdout": "session: herdr-trial\nstatus: not running\n", "code": 0},
            {"stdout": "session: herdr-trial\nstatus: not running\n", "code": 0},
            {"stdout": "session: herdr-trial\nstatus: running\n", "code": 0},
        ]
        self.assert_success(self.invoke("server", "start"))
        self.assertEqual(self.calls(), [
            status,
            ["--user", "--unit=herdr-trial-server", str(self.fake), *herdr_args("server")],
            status,
            status,
        ])
        self.assertEqual(self.started_units(), {"systemd_units": ["herdr-trial-server"]})

    def test_repeated_server_start_preserves_started_file(self) -> None:
        self.scratch.mkdir()
        before = '{"systemd_units": ["herdr-trial-server", "herdr-trial-client"]}\n'
        _ = (self.scratch / "started.json").write_text(before)
        self.configure(herdr_args("status", "server"), "session: herdr-trial\nstatus: running\n")
        self.assert_success(self.invoke("server", "start"))
        self.assertEqual((self.scratch / "started.json").read_text(), before)
        self.assertEqual(self.calls(), [herdr_args("status", "server")])

    def test_server_start_timeout_names_server_and_records_started_unit(self) -> None:
        self.configure(herdr_args("status", "server"), "status: not running\n")
        with patch.dict(os.environ, {"HERDR_TRIAL_START_TIMEOUT": "0"}):
            result = self.invoke("server", "start")
        self.assert_refusal(result, "server")
        self.assertIn(["--user", "--unit=herdr-trial-server", str(self.fake),
                       *herdr_args("server")], self.calls())
        self.assertEqual(self.started_units(), {"systemd_units": ["herdr-trial-server"]})

    def test_server_stop_stops_only_the_named_active_unit(self) -> None:
        self.configure(["--user", "is-active", "herdr-trial-server"], "active\n")
        self.assert_success(self.invoke("server", "stop"))
        self.assertEqual(self.calls(), [
            herdr_args("server", "stop"),
            ["--user", "is-active", "herdr-trial-server"],
            ["--user", "stop", "herdr-trial-server"],
        ])

    def test_server_stop_leaves_inactive_unit_alone(self) -> None:
        self.configure(["--user", "is-active", "herdr-trial-server"], "inactive\n", 3)
        self.assert_success(self.invoke("server", "stop"))
        self.assertEqual(self.calls(), [
            herdr_args("server", "stop"),
            ["--user", "is-active", "herdr-trial-server"],
        ])

    def test_client_open_launches_ghostty_with_named_session_and_records_visible_unit(self) -> None:
        self.assert_success(self.invoke("client", "open"))
        self.assertEqual(self.calls(), [[
            "--user", "--unit=herdr-trial-client", "/fixture/ghostty",
            "--gtk-single-instance=false", "--title=herdr-trial", "-e", str(self.fake),
            *herdr_args(),
        ], ["search", "--name", "^herdr-trial$"]])
        self.assertEqual(self.started_units(), {"systemd_units": ["herdr-trial-client"]})

    def test_client_open_adds_unit_once_and_preserves_existing_units(self) -> None:
        self.scratch.mkdir()
        _ = (self.scratch / "started.json").write_text(json.dumps({
            "systemd_units": ["herdr-trial-server", "herdr-trial-client"],
        }))
        self.assert_success(self.invoke("client", "open"))
        self.assertEqual(self.started_units(), {
            "systemd_units": ["herdr-trial-server", "herdr-trial-client"],
        })

    def test_client_open_waits_for_ghostty_window_and_moves_it_to_requested_desktop(self) -> None:
        search = ["search", "--name", "^herdr-trial$"]
        self.responses[" ".join(search)] = [
            {"stdout": "", "code": 1},
            {"stdout": "window-63\n", "code": 0},
        ]
        launch = ["--user", "--unit=herdr-trial-client", "/fixture/ghostty",
                  "--gtk-single-instance=false", "--title=herdr-trial", "-e",
                  str(self.fake), *herdr_args()]
        move = ["set_desktop_for_window", "window-63", "6"]

        self.assert_success(self.invoke("client", "open", "--desktop", "6"))

        self.assertEqual(self.calls(), [launch, search, search, move])
        self.assertEqual(self.started_units(), {"systemd_units": ["herdr-trial-client"]})

    def test_client_open_defaults_to_ghostty_when_no_terminal_override_is_set(self) -> None:
        del os.environ["HERDR_TRIAL_TERMINAL"]

        self.assert_success(self.invoke("client", "open"))

        self.assertEqual(self.calls()[0], [
            "--user", "--unit=herdr-trial-client", "ghostty",
            "--gtk-single-instance=false", "--title=herdr-trial", "-e",
            str(self.fake), *herdr_args(),
        ])

    def test_client_open_refuses_when_window_cannot_be_moved_to_requested_desktop(self) -> None:
        move = ["set_desktop_for_window", "window-17", "6"]
        self.configure(move, "", 1, "desktop fixture refused")

        self.assert_refusal(self.invoke("client", "open", "--desktop", "6"), "desktop")

        self.assertEqual(self.calls().count(move), 1)

    def test_client_open_stops_its_unit_when_no_window_appears_before_deadline(self) -> None:
        os.environ["HERDR_TRIAL_START_TIMEOUT"] = "0.2"
        search = ["search", "--name", "^herdr-trial$"]
        self.configure(search, "", 1)
        stop = ["--user", "stop", "herdr-trial-client"]

        result = self.invoke("client", "open", "--desktop", "6")

        self.assert_refusal(result, "herdr-trial", "window")
        calls = self.calls()
        launches = [call for call in calls if "--unit=herdr-trial-client" in call]
        self.assertEqual(len(launches), 1, calls)
        self.assertIn(search, calls)
        self.assertEqual(calls.count(stop), 1, calls)
        self.assertLess(calls.index(launches[0]), calls.index(stop))
        self.assertFalse(any(call[0] == "set_desktop_for_window" for call in calls), calls)

    def test_client_open_replaces_recorded_active_unit_that_has_no_window(self) -> None:
        self.scratch.mkdir()
        resources: StartedUnits = {"systemd_units": ["herdr-trial-server", "herdr-trial-client"]}
        _ = (self.scratch / "started.json").write_text(json.dumps(resources))
        active = ["--user", "is-active", "herdr-trial-client"]
        search = ["search", "--name", "^herdr-trial$"]
        stop = ["--user", "stop", "herdr-trial-client"]
        launch = ["--user", "--unit=herdr-trial-client", "/fixture/ghostty",
                  "--gtk-single-instance=false", "--title=herdr-trial", "-e",
                  str(self.fake), *herdr_args()]
        self.configure(active, "active\n")
        self.responses[" ".join(search)] = [
            {"stdout": "", "code": 1},
            {"stdout": "replacement-42\n", "code": 0},
        ]

        self.assert_success(self.invoke("client", "open", "--desktop", "6"))

        self.assertEqual(self.calls(), [active, search, stop, launch, search,
                                       ["set_desktop_for_window", "replacement-42", "6"]])
        self.assertEqual(self.started_units(), resources)

    def test_client_close_stops_only_client_unit(self) -> None:
        self.assert_success(self.invoke("client", "close"))
        self.assertEqual(self.calls(), [["--user", "stop", "herdr-trial-client"]])

    def test_issue_appends_utc_records_preserving_arguments(self) -> None:
        before = datetime.now(timezone.utc)
        self.assert_success(self.invoke("issue", "smoke", "one line\nnext", "quote: ' café"))
        self.assert_success(self.invoke("issue", "probe", "trusted", "blocked"))
        after = datetime.now(timezone.utc)
        lines = (self.scratch / "issues.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 2)
        records = [cast(IssueRecord, json.loads(line)) for line in lines]
        for record in records:
            self.assertEqual(set(record), {"time", "session", "expected", "happened"})
            instant = datetime.fromisoformat(record["time"].replace("Z", "+00:00"))
            self.assertEqual(instant.utcoffset(), timedelta(0))
            self.assertGreaterEqual(instant, before - timedelta(seconds=1))
            self.assertLessEqual(instant, after)
        self.assertEqual(records[0]["session"], "smoke")
        self.assertEqual(records[0]["expected"], "one line\nnext")
        self.assertEqual(records[0]["happened"], "quote: ' café")
        self.assertEqual(records[1]["session"], "probe")
        self.assertEqual(records[1]["expected"], "trusted")
        self.assertEqual(records[1]["happened"], "blocked")
        self.assertEqual(self.calls(), [])

    def test_trust_check_accepts_directory_itself_and_reads_only(self) -> None:
        self.trust_configs({str(self.project): True}, {str(self.project): "trusted"})
        paths = [self.claude / ".claude.json", self.codex / "config.toml"]
        before = [path.read_bytes() for path in paths]
        self.assert_success(self.invoke("trust-check", str(self.project)))
        self.assertEqual([path.read_bytes() for path in paths], before)
        self.assertFalse(self.scratch.exists())
        self.assertEqual(self.calls(), [])

    def test_trust_check_outside_git_rejects_claude_ancestor_trust(self) -> None:
        self.trust_configs({str(self.project.parent): True}, {str(self.project): "trusted"})
        self.assert_refusal(self.invoke("trust-check", str(self.project)), "Claude", str(self.project))
        self.assertEqual(self.calls(), [])

    def test_trust_check_rejects_linked_worktree_when_only_codex_ancestor_is_trusted(self) -> None:
        self.assert_success(self.invoke("scaffold"))
        repo = self.scratch / "repo"
        checkout = self.scratch / "worktrees/slug-unit"
        self.assertTrue((checkout / ".git").is_file())
        self.trust_configs({str(repo): True}, {str(self.root): "trusted"})

        self.assert_refusal(self.invoke("trust-check", str(checkout)), "Codex", str(repo))

    def test_trust_check_accepts_linked_worktree_with_exact_codex_main_repository_entry(self) -> None:
        self.assert_success(self.invoke("scaffold"))
        repo = self.scratch / "repo"
        checkout = self.scratch / "worktrees/slug-unit"
        self.assertTrue((checkout / ".git").is_file())
        self.trust_configs({str(repo): True}, {str(repo): "trusted"})
        paths = [self.claude / ".claude.json", self.codex / "config.toml"]
        before = [path.read_bytes() for path in paths]

        self.assert_success(self.invoke("trust-check", str(checkout)))
        self.assertEqual([path.read_bytes() for path in paths], before)

    def test_trust_check_outside_git_requires_exact_codex_directory_entry(self) -> None:
        probe = subprocess.run([GIT_BINARY, "-C", str(self.project), "rev-parse", "--git-common-dir"],
                               capture_output=True, text=True, check=False, timeout=10)
        self.assertNotEqual(probe.returncode, 0, "fixture must be outside any git repository")
        self.trust_configs({str(self.project): True}, {str(self.root): "trusted"})
        self.assert_refusal(self.invoke("trust-check", str(self.project)), "Codex", str(self.project))

        self.trust_configs({str(self.project): True}, {str(self.project): "trusted"})
        self.assert_success(self.invoke("trust-check", str(self.project)))

    def test_trust_check_rejects_linked_worktree_with_only_claude_ancestor_trust(self) -> None:
        self.assert_success(self.invoke("scaffold"))
        repo = self.scratch / "repo"
        checkout = self.scratch / "worktrees/slug-unit"
        self.assertTrue((checkout / ".git").is_file())
        common_directory = self.fixture_git(checkout, "rev-parse", "--path-format=absolute",
                                            "--git-common-dir")
        self.assertEqual(Path(common_directory).parent, repo)
        self.trust_configs({str(self.scratch): True}, {str(repo): "trusted"})
        paths = [self.claude / ".claude.json", self.codex / "config.toml"]
        before = [path.read_bytes() for path in paths]

        self.assert_refusal(self.invoke("trust-check", str(checkout)), "Claude", str(repo))

        self.assertEqual([path.read_bytes() for path in paths], before)

    def test_trust_check_accepts_linked_worktree_with_exact_claude_main_repository_trust(self) -> None:
        self.assert_success(self.invoke("scaffold"))
        repo = self.scratch / "repo"
        checkout = self.scratch / "worktrees/slug-unit"
        self.assertTrue((checkout / ".git").is_file())
        self.trust_configs({str(repo): True}, {str(repo): "trusted"})
        paths = [self.claude / ".claude.json", self.codex / "config.toml"]
        before = [path.read_bytes() for path in paths]

        self.assert_success(self.invoke("trust-check", str(checkout)))

        self.assertEqual([path.read_bytes() for path in paths], before)

    def test_trust_check_requires_both_accounts(self) -> None:
        cases: tuple[tuple[dict[str, bool], dict[str, str], str], ...] = (
            ({str(self.project): False}, {str(self.project): "trusted"}, "Claude"),
            ({str(self.project): True}, {str(self.project): "untrusted"}, "Codex"),
            ({}, {}, ""),
        )
        for claude_projects, codex_projects, name in cases:
            with self.subTest(missing=name):
                self.trust_configs(claude_projects, codex_projects)
                result = self.invoke("trust-check", str(self.project))
                self.assert_refusal(result, str(self.project))
                if name:
                    self.assertIn(name.casefold(), result.stderr.casefold())
        self.assertEqual(self.calls(), [])

    def test_trust_check_does_not_accept_a_sibling_with_same_prefix(self) -> None:
        sibling = self.project.parent.with_name(self.project.parent.name + "-other")
        self.trust_configs({str(sibling): True}, {str(sibling): "trusted"})
        self.assert_refusal(self.invoke("trust-check", str(self.project)), str(self.project))

    def test_trust_check_missing_files_is_a_named_refusal(self) -> None:
        self.assert_refusal(self.invoke("trust-check", str(self.project)), str(self.project))
        self.assertFalse((self.claude / ".claude.json").exists())
        self.assertFalse((self.codex / "config.toml").exists())
        self.assertEqual(self.calls(), [])

    def cleanup_fixture(self, before: tuple[CodexDaemonIdentity, ...] = (),
                        started: tuple[CodexDaemonIdentity, ...] = ()) -> Path:
        self.scratch.mkdir()
        proc = self.root / "proc-fixture"
        proc.mkdir()
        os.environ.update({
            "HERDR_TRIAL_CLAUDE_CONFIG_DIR": str(self.claude),
            "HERDR_TRIAL_CODEX_HOME": str(self.codex),
        })
        record: LaunchStartedResources = {
            "systemd_units": ["herdr-trial-server", "herdr-trial-client", "fixture-extra.timer"],
            "codex_daemons_before": list(before), "codex_daemons_started": list(started),
        }
        _ = (self.scratch / "started.json").write_text(json.dumps(record))
        _ = (self.scratch / "issues.jsonl").write_text(
            '{"time":"2026-10-13T18:15:00+00:00","session":"showrunner",' +
            '"expected":"lookup finds unit","happened":"tmux lookup missed herdr"}\n')
        for resource in (
            self.root / "notifier/showrunner-herdr-fake",
            self.root / "showrunner/herdr-fake",
            self.root / "herdr-config/sessions/herdr-trial",
            self.root / "herdr-config/sessions/herdr-devcheck",
        ):
            resource.mkdir(parents=True)
            _ = (resource / "fixture-state").write_text("trial owned\n")
        for name in (".plugins.lock", "release-notes.json"):
            _ = (self.root / "herdr-config" / name).write_text("trial state\n")
        workspace: WorkspaceListResponse = {"ok": True, "result": {"workspaces": [
            {"workspace_id": "w9", "label": "herdr-fake"},
        ]}}
        self.configure(herdr_args("workspace", "list"), json.dumps(workspace))
        self.pane_list([
            {"pane_id": "w9:p71", "label": "slug-unit-impl", "workspace_id": "w9"},
            {"pane_id": "w9:p82", "label": "clamp-unit-review", "workspace_id": "w9"},
        ])
        self.worker_state("w9:p71", "blocked", True)
        self.worker_state("w9:p82", "idle", False)
        self.responses[" ".join(herdr_args("status", "server"))] = [
            {"stdout": "status: running\n", "code": 0},
            {"stdout": "status: not running\n", "code": 0},
        ]
        for unit in record["systemd_units"]:
            active = ["--user", "is-active", unit]
            self.responses[" ".join(active)] = [
                {"stdout": "active\n", "code": 0},
                {"stdout": "inactive\n", "code": 3},
            ]
            self.configure(["--user", "stop", unit])
        return self.root / "cleanup/cleanup.json"

    def assert_no_shutdown_input(self) -> None:
        for call in self.calls():
            self.assertFalse(any(word in call for word in (
                "send-text", "send-keys", "prompt", "run", "start",
            )), call)

    def test_teardown_stops_every_recorded_unit_before_removing_scratch(self) -> None:
        manifest = self.cleanup_fixture()
        issue_text = (self.scratch / "issues.jsonl").read_text()
        for unit in ("herdr-trial-server", "herdr-trial-client", "fixture-extra.timer"):
            self.responses[" ".join(["--user", "stop", unit])] = {
                "stdout": "", "code": 0,
                "require_paths": [str(self.scratch / "started.json"), str(manifest)],
            }
        result = self.invoke("teardown")
        self.assert_success(result)
        for unit in ("herdr-trial-server", "herdr-trial-client", "fixture-extra.timer"):
            self.assertIn(["--user", "stop", unit], self.calls())
        self.assertFalse(self.scratch.exists())
        self.assertTrue(manifest.is_file())
        archived = [p for p in manifest.parent.iterdir()
                    if p.is_file() and p != manifest and issue_text in p.read_text()]
        self.assertTrue(archived, "issues must survive scratch deletion outside scratch")
        self.assert_no_shutdown_input()

    def test_teardown_closes_blocked_and_unready_panes_without_any_input(self) -> None:
        _ = self.cleanup_fixture()
        self.assert_success(self.invoke("teardown"))
        calls = self.calls()
        workspace_close = herdr_args("workspace", "close", "w9")
        pane_closes = [herdr_args("pane", "close", pane) for pane in ("w9:p71", "w9:p82")]
        self.assertTrue(workspace_close in calls or all(call in calls for call in pane_closes), calls)
        self.assert_no_shutdown_input()

    def test_teardown_removes_exact_trial_state_and_preserves_other_sessions(self) -> None:
        _ = self.cleanup_fixture()
        other_paths = [
            self.root / "herdr-config/sessions/keep-me/state",
            self.root / "notifier/showrunner-unrelated/state",
            self.root / "showrunner/unrelated/state",
            self.root / "scratch-other/state",
        ]
        for path in other_paths:
            path.parent.mkdir(parents=True)
            _ = path.write_text("unrelated survives\n")
        self.assert_success(self.invoke("teardown"))
        for path in other_paths:
            self.assertEqual(path.read_text(), "unrelated survives\n")
        for path in (
            self.root / "herdr-config/sessions/herdr-trial",
            self.root / "herdr-config/sessions/herdr-devcheck",
            self.root / "herdr-config/.plugins.lock",
            self.root / "herdr-config/release-notes.json",
            self.root / "notifier/showrunner-herdr-fake",
            self.root / "showrunner/herdr-fake",
        ):
            self.assertFalse(path.exists(), str(path))

    def daemon_fixture(self, identity: CodexDaemonIdentity) -> None:
        self.proc_process(identity["pid"], identity["start_time"], ["codex", "app-server", "--managed-daemon"],
                          str(self.codex), self.codex / "bin/codex")
        self.responses[str(identity["pid"])] = {
            "stdout": "", "code": 0,
            "remove_paths": [str(self.root / "proc-fixture" / str(identity["pid"]))],
        }

    def test_teardown_never_stops_a_daemon_from_the_original_baseline(self) -> None:
        baseline: CodexDaemonIdentity = {"pid": 27101, "start_time": 4100}
        _ = self.cleanup_fixture(before=(baseline,), started=(baseline,))
        self.daemon_fixture(baseline)
        result = self.invoke("teardown")
        self.assert_success(result)
        self.assertFalse(any("daemon" in line for line in result.stdout.splitlines()), result.stdout)
        self.assertNotIn(["27101"], self.calls())
        self.assertTrue((self.root / "proc-fixture/27101").is_dir())
        self.assertFalse(self.scratch.exists())

    def test_teardown_stops_only_a_matching_trial_started_daemon_without_outside_clients(self) -> None:
        daemon: CodexDaemonIdentity = {"pid": 27102, "start_time": 4200}
        _ = self.cleanup_fixture(started=(daemon,))
        self.daemon_fixture(daemon)
        self.assert_success(self.invoke("teardown"))
        self.assertEqual(self.calls().count(["27102"]), 1)
        self.assertFalse((self.root / "proc-fixture/27102").exists())
        self.assertLess(self.calls().index(herdr_args("workspace", "close", "w9")),
                        self.calls().index(["27102"]))
        self.assertLess(self.calls().index(["--user", "stop", "herdr-trial-server"]),
                        self.calls().index(["27102"]))
        self.assert_no_shutdown_input()

    def test_teardown_preserves_a_trial_started_daemon_shared_by_an_outside_account_client(self) -> None:
        daemon: CodexDaemonIdentity = {"pid": 27103, "start_time": 4300}
        _ = self.cleanup_fixture(started=(daemon,))
        self.daemon_fixture(daemon)
        self.proc_process(28103, 9300, ["codex", "-m", "outside-model"], str(self.codex),
                          self.codex / "bin/codex")
        result = self.invoke("teardown")
        self.assertEqual(result.code, 1, result.stderr)
        self.assertIn("27103", result.stdout)
        self.assertIn("shared", result.stdout)
        self.assertNotIn(["27103"], self.calls())
        self.assertNotIn(["28103"], self.calls())
        self.assertTrue((self.root / "proc-fixture/27103").exists())
        self.assertTrue((self.root / "proc-fixture/28103").exists())
        self.assertFalse(self.scratch.exists())
        result = self.invoke_cli("leftovers")
        self.assertEqual(result.code, 1, result.stderr)
        daemon_lines = [line for line in result.stdout.splitlines()
                        if "daemon" in line and "27103" in line]
        self.assertEqual(len(daemon_lines), 1, result.stdout)
        self.assertIn("shared", daemon_lines[0])
        self.assertNotIn("trial-started", daemon_lines[0])
        self.assertNotIn(["27103"], self.calls())

    def test_teardown_does_not_stop_a_reused_pid_with_a_different_start_time(self) -> None:
        recorded: CodexDaemonIdentity = {"pid": 27104, "start_time": 4400}
        _ = self.cleanup_fixture(started=(recorded,))
        self.daemon_fixture({"pid": 27104, "start_time": 9900})
        result = self.invoke("teardown")
        self.assert_success(result)
        self.assertNotIn(["27104"], self.calls())
        self.assertTrue((self.root / "proc-fixture/27104").is_dir())
        self.assertIn("27104", result.stdout)

    def test_teardown_does_not_stop_a_recorded_pid_with_unreadable_identity(self) -> None:
        daemon: CodexDaemonIdentity = {"pid": 27105, "start_time": 4500}
        _ = self.cleanup_fixture(started=(daemon,))
        self.daemon_fixture(daemon)
        (self.root / "proc-fixture/27105/stat").unlink()
        result = self.invoke("teardown")
        self.assertEqual(result.code, 1, result.stderr)
        self.assertNotIn(["27105"], self.calls())
        self.assertTrue((self.root / "proc-fixture/27105").is_dir())
        self.assertIn("27105", result.stdout)
        self.assertIn("unreadable", result.stdout.casefold())

    def test_teardown_does_not_stop_a_recorded_pid_when_its_command_or_account_changed(self) -> None:
        first: CodexDaemonIdentity = {"pid": 27106, "start_time": 4600}
        second: CodexDaemonIdentity = {"pid": 27107, "start_time": 4700}
        missing: CodexDaemonIdentity = {"pid": 27108, "start_time": 4800}
        _ = self.cleanup_fixture(started=(first, second, missing))
        self.proc_process(27106, 4600, ["sleep", "3600"], str(self.codex), self.root / "bin/sleep")
        self.proc_process(27107, 4700, ["codex", "app-server", "--managed-daemon"], str(self.root / "other-account"),
                          self.root / "other-account/bin/codex")
        self.assert_success(self.invoke("teardown"))
        for pid in (27106, 27107, 27108):
            self.assertNotIn([str(pid)], self.calls())
        self.assertTrue((self.root / "proc-fixture/27106").is_dir())
        self.assertTrue((self.root / "proc-fixture/27107").is_dir())

    def test_leftovers_in_a_separate_invocation_uses_manifest_after_scratch_deletion(self) -> None:
        daemon: CodexDaemonIdentity = {"pid": 27109, "start_time": 4900}
        manifest = self.cleanup_fixture(started=(daemon,))
        self.daemon_fixture(daemon)
        self.proc_process(28109, 9400, ["codex", "-m", "outside-model"], str(self.codex),
                          self.codex / "bin/codex")
        result = self.invoke("teardown")
        self.assertEqual(result.code, 1, result.stderr)
        self.assertFalse(self.scratch.exists())
        original_manifest = manifest.read_bytes()
        self.configure(herdr_args("status", "server"), "status: not running\n")
        for unit in ("herdr-trial-server", "herdr-trial-client", "fixture-extra.timer"):
            self.configure(["--user", "is-active", unit], "inactive\n", code=3)
        before = len(self.calls())
        result = self.invoke_cli("leftovers")
        self.assertEqual(result.code, 1, result.stderr)
        self.assertIn("27109", result.stdout)
        self.assertIn("shared", result.stdout)
        self.assertNotIn("scratch " + str(self.scratch), result.stdout)
        self.assertEqual(manifest.read_bytes(), original_manifest)
        for call in self.calls()[before:]:
            self.assertNotIn("stop", call)
        self.assertFalse(self.scratch.exists())

    def test_leftovers_names_each_surviving_runtime_resource(self) -> None:
        shared: CodexDaemonIdentity = {"pid": 27110, "start_time": 5000}
        owned: CodexDaemonIdentity = {"pid": 27111, "start_time": 5100}
        _ = self.cleanup_fixture(before=(shared,), started=(owned,))
        self.daemon_fixture(shared)
        self.daemon_fixture(owned)
        self.proc_process(28111, 9500, ["claude"], str(self.codex), self.root / "bin/claude")
        (self.root / "proc-fixture/28111/cwd").symlink_to(self.scratch / "repo")
        result = self.invoke("leftovers")
        self.assertEqual(result.code, 1, result.stderr)
        for resource in (
            str(self.scratch), "herdr-trial-server", "herdr-trial-client", "fixture-extra.timer",
            str(self.root / "notifier/showrunner-herdr-fake"),
            str(self.root / "showrunner/herdr-fake"),
            str(self.root / "herdr-config/sessions/herdr-trial"),
            str(self.root / "herdr-config/sessions/herdr-devcheck"),
            str(self.root / "herdr-config/.plugins.lock"),
            str(self.root / "herdr-config/release-notes.json"), "w9", "28111",
        ):
            self.assertIn(resource, result.stdout)
        lines = result.stdout.splitlines()
        self.assertFalse(any("daemon" in line and "27110" in line for line in lines), result.stdout)
        self.assertTrue(any("27111" in line and "trial-started" in line for line in lines))
        self.assertTrue(self.scratch.exists())
        self.assert_no_shutdown_input()
        self.assertFalse(any("stop" in call or "close" in call for call in self.calls()))

    def test_unreadable_process_shares_a_trial_daemon_only_when_owned_by_the_current_user(self) -> None:
        daemon: CodexDaemonIdentity = {"pid": 27201, "start_time": 6201}
        _ = self.cleanup_fixture(started=(daemon,))
        self.daemon_fixture(daemon)
        self.proc_process(28201, 9201, ["python", "outside-session"], "", self.root / "bin/python")
        unreadable = self.root / "proc-fixture/28201"
        (unreadable / "environ").unlink()
        original_stat = Path.stat
        owner_uid = os.getuid() + 1

        def fixture_stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
            actual = original_stat(path, follow_symlinks=follow_symlinks)
            if path != unreadable:
                return actual
            return os.stat_result((actual.st_mode, actual.st_ino, actual.st_dev,
                                   actual.st_nlink, owner_uid, actual.st_gid,
                                   actual.st_size, actual.st_atime, actual.st_mtime, actual.st_ctime))

        with patch.object(Path, "stat", fixture_stat):
            result = self.invoke("leftovers")
            self.assertEqual(result.code, 1, result.stderr)
            daemon_lines = [line for line in result.stdout.splitlines()
                            if line.startswith("codex daemon 27201 ")]
            self.assertEqual(daemon_lines, ["codex daemon 27201 trial-started"], result.stdout)
            self.assert_success(self.invoke("teardown"))
            self.assertEqual(self.calls().count(["27201"]), 1)
            self.assertFalse((self.root / "proc-fixture/27201").exists())
            self.assertTrue(unreadable.is_dir())

            # Replay the recorded daemon with a current-user inspection failure.
            # It must be shared and retained, including on a cleanup retry.
            self.daemon_fixture(daemon)
            owner_uid = os.getuid()
            result = self.invoke("leftovers")
            self.assertEqual(result.code, 1, result.stderr)
            daemon_lines = [line for line in result.stdout.splitlines()
                            if line.startswith("codex daemon 27201 ")]
            self.assertEqual(daemon_lines, ["codex daemon 27201 shared"], result.stdout)
            result = self.invoke("teardown")
            self.assertEqual(result.code, 1, result.stderr)
            self.assertEqual(self.calls().count(["27201"]), 1,
                             "current-user uncertainty must not cross the stop boundary")
            self.assertTrue((self.root / "proc-fixture/27201").is_dir())
        self.assert_no_shutdown_input()

    def test_teardown_retains_a_recorded_delegate_mesh_server_without_signalling_it(self) -> None:
        mesh: CodexDaemonIdentity = {"pid": 27203, "start_time": 6203}
        _ = self.cleanup_fixture(started=(mesh,))
        self.proc_process(mesh["pid"], mesh["start_time"],
                          ["codex", "-c", "thread_unload_delay_secs=0", "app-server",
                           "--listen", "ws://127.0.0.1:41003"],
                          str(self.codex), self.codex / "bin/codex")
        self.responses[str(mesh["pid"])] = {
            "stdout": "", "code": 0,
            "remove_paths": [str(self.root / "proc-fixture" / str(mesh["pid"]))],
        }
        result = self.invoke("teardown")
        self.assert_success(result)
        self.assertNotIn([str(mesh["pid"])], self.calls())
        self.assertTrue((self.root / "proc-fixture" / str(mesh["pid"])).is_dir())
        self.assertEqual(result.stdout.splitlines().count(
            "process 27203: not a managed Codex daemon; retained"), 1, result.stdout)
        self.assert_no_shutdown_input()

    def test_leftovers_ignores_baseline_and_unrecorded_daemons_and_delegate_mesh_servers(self) -> None:
        baseline: CodexDaemonIdentity = {"pid": 27204, "start_time": 6204}
        unrecorded: CodexDaemonIdentity = {"pid": 27205, "start_time": 6205}
        manifest = self.cleanup_fixture(before=(baseline,))
        self.daemon_fixture(baseline)
        self.daemon_fixture(unrecorded)
        self.proc_process(27206, 6206, ["codex", "app-server", "--listen", "ws://127.0.0.1:41004"],
                          str(self.codex), self.codex / "bin/codex")
        # Publish the manifest and remove fixture trial resources; all remaining
        # processes belong to sessions outside the trial.
        _ = self.invoke("teardown")
        self.assertTrue(manifest.is_file())
        self.assertFalse(self.scratch.exists())
        result = self.invoke_cli("leftovers")
        self.assert_success(result)
        self.assertFalse(any("daemon" in line for line in result.stdout.splitlines()), result.stdout)
        for pid in (27204, 27205, 27206):
            self.assertTrue((self.root / "proc-fixture" / str(pid)).is_dir())
            self.assertNotIn([str(pid)], self.calls())
        self.assert_no_shutdown_input()

    def test_leftovers_succeeds_after_complete_cleanup_and_teardown_retry_is_safe(self) -> None:
        manifest = self.cleanup_fixture()
        self.assert_success(self.invoke("teardown"))
        before = manifest.read_bytes()
        self.assert_success(self.invoke_cli("leftovers"))
        self.assert_success(self.invoke("teardown"))
        self.assert_success(self.invoke_cli("leftovers"))
        self.assertEqual(manifest.read_bytes(), before)
        self.assertFalse(self.scratch.exists())
        self.assertFalse((self.root / "herdr-config").exists())
        self.assert_no_shutdown_input()

    def test_teardown_and_leftovers_report_retained_trust_grants_without_editing_configs(self) -> None:
        _ = self.cleanup_fixture()
        repo = self.scratch / "repo"
        self.trust_configs({str(repo): True}, {str(repo): "trusted"})
        with (self.codex / "config.toml").open("a") as output:
            _ = output.write('[hooks]\ntrust = "all"\n')
        files = [self.claude / ".claude.json", self.codex / "config.toml"]
        original = [file.read_bytes() for file in files]
        for command in ("teardown", "leftovers"):
            result = self.invoke(command)
            self.assert_success(result)
            for text in ("Claude", "Codex", "hooks", str(repo), str(self.claude), str(self.codex)):
                self.assertIn(text, result.stdout)
            self.assertEqual([file.read_bytes() for file in files], original)

    def test_teardown_retains_scratch_and_manifest_when_a_recorded_unit_cannot_stop(self) -> None:
        manifest = self.cleanup_fixture()
        self.configure(["--user", "stop", "herdr-trial-client"], code=1,
                       stderr="fixture stop denied\n")
        result = self.invoke("teardown")
        self.assertEqual(result.code, 1, result.stderr)
        self.assertTrue(self.scratch.exists(), "a failed stop must keep cleanup evidence")
        self.assertTrue((self.scratch / "started.json").exists())
        self.assertTrue(manifest.is_file())
        self.assertIn("herdr-trial-client", result.stderr + result.stdout)
        self.assert_no_shutdown_input()

    def test_usage_errors_are_single_lines_and_have_no_effects(self) -> None:
        for arguments in ([], ["unknown"], ["server"], ["client", "bad"],
                          ["client", "open", "--desktop", "0"],
                          ["client", "open", "--desktop", "-1"],
                          ["up", "--desktop", "six"],
                          ["pane-id"], ["read"], ["type", "smoke"],
                          ["issue", "smoke", "expected"], ["trust-check"]):
            with self.subTest(arguments=arguments):
                result = self.invoke(*arguments)
                self.assertEqual(result.code, 2, result.stderr)
                self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
                self.assertTrue(result.stderr.strip())
                self.assertEqual(self.calls(), [])
                self.assertFalse(self.scratch.exists())


if __name__ == "__main__":
    _ = unittest.main()
