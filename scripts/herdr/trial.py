#!/usr/bin/env python3
"""Control the isolated herdr trial and check its existing agent trust."""

from __future__ import annotations

import argparse
import json
import math
import os
import shlex
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, NoReturn, NotRequired, TypedDict, cast, override

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.herdr import scaffold_files

HERDR_BINARY = "/nix/store/pyqm5522kvk30b9vgb1ab976pjnjwn51-herdr-0.9.0/bin/herdr"
SESSION = "herdr-trial"
SERVER_UNIT = "herdr-trial-server"
CLIENT_UNIT = "herdr-trial-client"
SCRATCH_ROOT = "/home/natepiano/rust/herdr-trial-scratch"


class PaneIdentity(TypedDict):
    pane_id: str
    label: NotRequired[str | None]


class PaneListResult(TypedDict):
    panes: list[PaneIdentity]


class AgentOccupant(TypedDict):
    pane_id: str
    agent: NotRequired[str | None]
    agent_status: str


class AgentListResult(TypedDict):
    agents: list[AgentOccupant]


class CreatedWorkspace(TypedDict):
    root_pane: PaneIdentity
    workspace: NotRequired[WorkspaceIdentity]
    tab: NotRequired[TabIdentity]


class WorkspaceIdentity(TypedDict):
    workspace_id: str
    label: NotRequired[str | None]


class TabIdentity(TypedDict):
    tab_id: str


class WorkspaceListResult(TypedDict):
    workspaces: list[WorkspaceIdentity]


class CreatedTab(TypedDict):
    tab: TabIdentity
    root_pane: PaneIdentity


class SplitPane(TypedDict):
    pane: PaneIdentity


class AgentLifecycle(TypedDict):
    agent_status: str


class AgentWaitResult(TypedDict):
    agent: AgentLifecycle


class HerdrReply(TypedDict):
    result: PaneListResult | AgentListResult | CreatedWorkspace | AgentWaitResult | WorkspaceListResult | CreatedTab | SplitPane


class HerdrError(TypedDict):
    code: str
    message: str


class HerdrRefusal(TypedDict):
    error: HerdrError


class CodexDaemon(TypedDict):
    pid: int
    start_time: int


class StartedResources(TypedDict):
    systemd_units: list[str]
    codex_daemons_before: NotRequired[list[CodexDaemon]]
    codex_daemons_started: NotRequired[list[CodexDaemon]]


class TrialIssue(TypedDict):
    time: str
    session: str
    expected: str
    happened: str


type WorkerLabel = Literal[
    "slug-unit-impl", "slug-unit-review", "clamp-unit-impl", "clamp-unit-review",
    "count-unit-impl", "count-unit-review",
]


class WorkerWorkOrder(TypedDict):
    file: str


@dataclass(frozen=True)
class AgentConfiguration:
    family: Literal["claude", "codex"]
    model: str
    effort: str


@dataclass(frozen=True)
class LaunchAccounts:
    claude_config: Path
    codex_home: Path


@dataclass(frozen=True)
class ProjectTrust:
    project_key: Path
    accepted: bool


@dataclass(frozen=True)
class KeepCurrentDesktop:
    """Leave the visible client on its current desktop."""


@dataclass(frozen=True)
class RequestedDesktop:
    number: int


@dataclass(frozen=True)
class ClientWindow:
    window_id: str


@dataclass(frozen=True)
class NoClientWindow:
    """No visible window has the trial client's exact title."""


@dataclass(frozen=True)
class ShowrunnerPane:
    """The unmarked production coordinator pane."""


@dataclass(frozen=True)
class UnitPane:
    unit: str


@dataclass(frozen=True)
class AgentLaunch:
    label: str
    pane_id: str
    configuration: AgentConfiguration
    arguments: tuple[str, ...]
    opening_prompt: str


@dataclass(frozen=True)
class PaneFound:
    pane_id: str


@dataclass(frozen=True)
class NoPane:
    label: str


@dataclass(frozen=True)
class SeveralPanes:
    label: str
    count: int


@dataclass(frozen=True)
class ShellOnly:
    """Leave the created pane at its shell prompt."""


@dataclass(frozen=True)
class CommandToRun:
    text: str


@dataclass(frozen=True)
class AssignedWork:
    file: Path

    @property
    def report(self) -> Path:
        return Path(f"{self.file}.done")


@dataclass(frozen=True)
class NoAssignedWork:
    label: str


@dataclass(frozen=True)
class ObservedAgentState:
    status: str


@dataclass(frozen=True)
class WaitTimedOut:
    """The CLI or its subprocess exhausted the remaining wait."""


@dataclass(frozen=True)
class WorkDeadline:
    expires_at: float

    def remaining_milliseconds(self) -> int:
        return max(0, math.ceil((self.expires_at - time.monotonic()) * 1000))


class BlockedWorker(Exception):
    """The worker needs the user; no prompt response is sent."""


class TrialFailure(Exception):
    """A named refusal or external command failure."""


class UsageFailure(Exception):
    """Invalid command-line arguments or trial environment settings."""


class TrialParser(argparse.ArgumentParser):
    @override
    def error(self, message: str) -> NoReturn:
        raise UsageFailure(message)


class TrialArguments(argparse.Namespace):
    command: str = ""
    action: str = ""
    label: str = ""
    text: str = ""
    session: str = ""
    expected: str = ""
    happened: str = ""
    directory: str = ""
    start_command: ShellOnly | CommandToRun = ShellOnly()
    unit: str = ""
    role: str = ""
    file: str = ""
    desktop: KeepCurrentDesktop | RequestedDesktop = KeepCurrentDesktop()


def json_object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TrialFailure(f"{context}: expected a JSON object")
    return cast(dict[str, object], value)


def decode_object(text: str, context: str) -> dict[str, object]:
    try:
        value = cast(object, json.loads(text))
    except ValueError as error:
        raise TrialFailure(f"{context}: invalid JSON") from error
    return json_object(value, context)


def json_text(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise TrialFailure(f"{context}: expected text")
    return value


def json_items(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise TrialFailure(f"{context}: expected a JSON list")
    return cast(list[object], value)


def run_program(argv: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=timeout)


def require_success(result: subprocess.CompletedProcess[str], context: str) -> None:
    if result.returncode == 0:
        return
    detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
    try:
        body = decode_object(detail, context)
        error = json_object(body.get("error"), context)
        detail = json_text(error.get("message"), context)
    except TrialFailure:
        pass
    raise TrialFailure(f"{context}: {' '.join(detail.splitlines())}")


class Herdr:
    def __init__(self) -> None:
        self.binary: str = os.environ.get("HERDR_BIN", HERDR_BINARY)

    def argv(self, *arguments: str) -> list[str]:
        """The outside-pane targeting form verified with herdr 0.9.0."""
        return [self.binary, "--session", SESSION, *arguments]

    def run(self, *arguments: str) -> str:
        result = run_program(self.argv(*arguments))
        require_success(result, f"herdr {' '.join(arguments[:2])}")
        return result.stdout

    def result(self, *arguments: str) -> dict[str, object]:
        reply = decode_object(self.run(*arguments), "herdr response")
        return json_object(reply.get("result"), "herdr result")

    def panes(self) -> list[PaneIdentity]:
        result = self.result("pane", "list")
        panes: list[PaneIdentity] = []
        for item in json_items(result.get("panes"), "herdr pane list"):
            pane = json_object(item, "herdr pane")
            label = pane.get("label")
            if label is not None and not isinstance(label, str):
                raise TrialFailure("herdr pane label: expected text or null")
            panes.append({"pane_id": json_text(pane.get("pane_id"), "herdr pane id"), "label": label})
        return panes

    def lookup(self, label: str) -> PaneFound | NoPane | SeveralPanes:
        matches = [pane["pane_id"] for pane in self.panes() if pane.get("label") == label]
        if not matches:
            return NoPane(label)
        if len(matches) > 1:
            return SeveralPanes(label, len(matches))
        return PaneFound(matches[0])

    def pane_id(self, label: str) -> str:
        found = self.lookup(label)
        if isinstance(found, NoPane):
            raise TrialFailure(f"no pane for label {label!r}")
        if isinstance(found, SeveralPanes):
            raise TrialFailure(f"several panes ({found.count}) for label {label!r}")
        return found.pane_id

    def read(self, label: str) -> str:
        # Unlike the other commands, herdr 0.9.0 prints pane text, not JSON.
        return self.run("pane", "read", self.pane_id(label), "--source", "visible")

    def type_text(self, label: str, text: str) -> None:
        pane_id = self.pane_id(label)
        _ = self.run("pane", "send-text", pane_id, text)
        _ = self.run("pane", "send-keys", pane_id, "enter")

    def open(self, label: str, directory: str, command: ShellOnly | CommandToRun) -> str:
        if not isinstance(self.lookup(label), NoPane):
            raise TrialFailure(f"pane already exists for label {label!r}")
        result = self.result("workspace", "create", "--label", label, "--cwd", directory, "--no-focus")
        root_pane = json_object(result.get("root_pane"), "herdr workspace root pane")
        pane_id = json_text(root_pane.get("pane_id"), "herdr workspace root pane id")
        if root_pane.get("label") != label:
            _ = self.run("pane", "rename", pane_id, label)
        if isinstance(command, CommandToRun):
            _ = self.run("pane", "run", pane_id, command.text)
        return pane_id

    def close(self, label: str) -> None:
        # In herdr 0.9.0, closing the last pane also removes its tab and workspace.
        _ = self.run("pane", "close", self.pane_id(label))

    def agents(self) -> list[AgentOccupant]:
        result = self.result("agent", "list")
        agents: list[AgentOccupant] = []
        for item in json_items(result.get("agents"), "herdr agent list"):
            agent = json_object(item, "herdr agent")
            kind = agent.get("agent")
            if kind is not None and not isinstance(kind, str):
                raise TrialFailure("herdr agent kind: expected text or null")
            agents.append({
                "pane_id": json_text(agent.get("pane_id"), "herdr agent pane id"),
                "agent": kind,
                "agent_status": json_text(agent.get("agent_status"), "herdr agent state"),
            })
        return agents


def scratch_root() -> Path:
    return Path(os.environ.get("HERDR_TRIAL_ROOT", SCRATCH_ROOT)).expanduser()


def started_resources() -> StartedResources:
    path = scratch_root() / "started.json"
    resources: StartedResources = {"systemd_units": []}
    try:
        text = path.read_text()
    except FileNotFoundError:
        return resources
    data = decode_object(text, str(path))
    resources["systemd_units"] = [
        json_text(item, str(path)) for item in json_items(data.get("systemd_units"), str(path))
    ]
    for key in ("codex_daemons_before", "codex_daemons_started"):
        if key in data:
            daemons: list[CodexDaemon] = []
            for value in json_items(data[key], f"{path} {key}"):
                daemon = json_object(value, f"{path} daemon")
                pid, start = daemon.get("pid"), daemon.get("start_time")
                if (not isinstance(pid, int) or isinstance(pid, bool)
                        or not isinstance(start, int) or isinstance(start, bool)):
                    raise TrialFailure(f"{path}: daemon pid and start_time must be integers")
                daemons.append({"pid": pid, "start_time": start})
            if key == "codex_daemons_before":
                resources["codex_daemons_before"] = daemons
            else:
                resources["codex_daemons_started"] = daemons
    return resources


def write_started_resources(resources: StartedResources) -> None:
    root = scratch_root()
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=root, prefix=".started-", delete=False) as output:
        temporary = Path(output.name)
        try:
            _ = output.write(json.dumps(resources) + "\n")
            output.close()
            os.replace(temporary, root / "started.json")
        finally:
            temporary.unlink(missing_ok=True)


def record_unit(unit: str, resources: StartedResources) -> None:
    if unit in resources["systemd_units"]:
        return
    resources["systemd_units"].append(unit)
    write_started_resources(resources)


def systemctl(*arguments: str) -> subprocess.CompletedProcess[str]:
    return run_program([os.environ.get("HERDR_TRIAL_SYSTEMCTL", "systemctl"), "--user", *arguments])


def launch_unit(unit: str, command: list[str]) -> None:
    resources = started_resources()
    result = run_program([
        os.environ.get("HERDR_TRIAL_SYSTEMD_RUN", "systemd-run"),
        "--user", f"--unit={unit}", *command,
    ])
    require_success(result, f"start {unit}")
    record_unit(unit, resources)


def client_window(timeout: float = 10.0) -> ClientWindow | NoClientWindow:
    try:
        result = run_program([
            os.environ.get("HERDR_TRIAL_KDOTOOL", "kdotool"),
            "search", "--name", "^herdr-trial$",
        ], timeout=timeout)
    except subprocess.TimeoutExpired:
        return NoClientWindow()
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            if line.strip():
                return ClientWindow(line.strip())
    return NoClientWindow()


def move_client_window(window: ClientWindow, desktop: KeepCurrentDesktop | RequestedDesktop) -> None:
    if isinstance(desktop, RequestedDesktop):
        require_success(run_program([
            os.environ.get("HERDR_TRIAL_KDOTOOL", "kdotool"),
            "set_desktop_for_window", window.window_id, str(desktop.number),
        ]), f"move herdr-trial window to desktop {desktop.number}")


def client_open(herdr: Herdr, desktop: KeepCurrentDesktop | RequestedDesktop) -> None:
    timeout = start_timeout()
    resources = started_resources()
    if CLIENT_UNIT in resources["systemd_units"] and systemctl("is-active", CLIENT_UNIT).returncode == 0:
        window = client_window(timeout=min(10.0, timeout) if timeout > 0 else 10.0)
        if isinstance(window, ClientWindow):
            move_client_window(window, desktop)
            return
        require_success(systemctl("stop", CLIENT_UNIT), f"stop {CLIENT_UNIT}")
    launch_unit(CLIENT_UNIT, [
        os.environ.get("HERDR_TRIAL_TERMINAL", "ghostty"),
        "--gtk-single-instance=false", "--title=herdr-trial", "-e", *herdr.argv(),
    ])
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            # A zero polling deadline still permits one window inspection.
            probe_timeout = max(0.001, min(10.0, remaining)) if timeout > 0 else 10.0
            window = client_window(timeout=probe_timeout)
            if isinstance(window, ClientWindow):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TrialFailure("herdr-trial window missing after client startup")
            time.sleep(min(0.1, remaining))
    except (OSError, subprocess.SubprocessError, TrialFailure):
        require_success(systemctl("stop", CLIENT_UNIT), f"stop {CLIENT_UNIT} after missing window")
        raise
    move_client_window(window, desktop)


def start_timeout() -> float:
    raw = os.environ.get("HERDR_TRIAL_START_TIMEOUT", "10")
    try:
        timeout = float(raw)
    except ValueError as error:
        raise UsageFailure("HERDR_TRIAL_START_TIMEOUT must be a nonnegative number") from error
    if not math.isfinite(timeout) or timeout < 0:
        raise UsageFailure("HERDR_TRIAL_START_TIMEOUT must be a nonnegative finite number")
    return timeout


def server_answering(status: subprocess.CompletedProcess[str]) -> bool:
    return status.returncode == 0 and "status: running" in status.stdout.splitlines()


def server_start(herdr: Herdr) -> None:
    timeout = start_timeout()
    if server_answering(run_program(herdr.argv("status", "server"))):
        return
    launch_unit(SERVER_UNIT, herdr.argv("server"))
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        try:
            status = run_program(herdr.argv("status", "server"), timeout=max(0.001, remaining))
            if server_answering(status):
                return
        except subprocess.TimeoutExpired:
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TrialFailure(f"server {SESSION} not answering after {timeout:g} seconds")
        time.sleep(min(0.1, remaining))


def server_stop(herdr: Herdr) -> None:
    try:
        result = run_program(herdr.argv("server", "stop"))
    finally:
        if systemctl("is-active", SERVER_UNIT).returncode == 0:
            require_success(systemctl("stop", SERVER_UNIT), f"stop {SERVER_UNIT}")
    require_success(result, f"stop server {SESSION}")


def write_issue(session: str, expected: str, happened: str) -> None:
    root = scratch_root()
    root.mkdir(parents=True, exist_ok=True)
    issue: TrialIssue = {
        "time": datetime.now(UTC).isoformat(), "session": session,
        "expected": expected, "happened": happened,
    }
    with (root / "issues.jsonl").open("a") as output:
        _ = output.write(json.dumps(issue) + "\n")


def git_result(*arguments: str) -> subprocess.CompletedProcess[str]:
    # Keep inherited git overrides and user hooks out of the scratch repository.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    return subprocess.run([
        os.environ.get("HERDR_TRIAL_GIT", "git"), "-c", "core.hooksPath=/dev/null",
        "-c", "init.templateDir=", *arguments,
    ], capture_output=True, text=True, check=False, timeout=30.0, env=environment)


def git_command(*arguments: str) -> None:
    require_success(git_result(*arguments), "scaffold git")


def scaffold() -> None:
    root = scratch_root().resolve()
    repo = root / "repo"
    if repo.exists() or repo.is_symlink():
        raise TrialFailure(f"scaffold refuses existing repository {repo}")
    for path in (root / "origin.git", *(root / "worktrees" / unit.name for unit in scaffold_files.UNITS)):
        if path.exists() or path.is_symlink():
            raise TrialFailure(f"scaffold refuses existing path {path}")
    root.mkdir(parents=True, exist_ok=True)
    git_command("init", "--bare", "--initial-branch=main", str(root / "origin.git"))
    git_command("clone", str(root / "origin.git"), str(repo))
    for name, text in scaffold_files.file_texts(root).items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(text)
    git_command("-C", str(repo), "add", ".")
    git_command("-C", str(repo), "-c", "user.name=Herdr Trial", "-c", "user.email=herdr-trial@localhost",
                "-c", "commit.gpgSign=false", "commit", "-m", "Seed the herdr fake production")
    git_command("-C", str(repo), "push", "-u", "origin", "main")
    git_command("-C", str(repo), "switch", "-c", "herdr-fake")
    git_command("-C", str(repo), "push", "-u", "origin", "herdr-fake")
    for unit in scaffold_files.UNITS:
        git_command("-C", str(repo), "worktree", "add", "-b", f"herdr-fake-{unit.slug}",
                    str(root / "worktrees" / unit.name), "herdr-fake")


def worker_label(unit: str, role: str) -> WorkerLabel:
    if unit not in tuple(item.name for item in scaffold_files.UNITS) or role not in ("impl", "review"):
        raise UsageFailure(f"invalid worker {unit}-{role}")
    return cast(WorkerLabel, f"{unit}-{role}")


def assigned_work(label: WorkerLabel) -> AssignedWork | NoAssignedWork:
    path = scratch_root() / "work" / f"{label}.json"
    try:
        text = path.read_text()
    except FileNotFoundError:
        return NoAssignedWork(label)
    order = decode_object(text, str(path))
    return AssignedWork(Path(json_text(order.get("file"), f"{path} work order file")))


def record_work(label: WorkerLabel, assignment: AssignedWork) -> None:
    directory = scratch_root() / "work"
    directory.mkdir(parents=True, exist_ok=True)
    order: WorkerWorkOrder = {"file": str(assignment.file)}
    with tempfile.NamedTemporaryFile(mode="w", dir=directory, prefix=f".{label}-", delete=False) as output:
        temporary_path = Path(output.name)
        try:
            _ = output.write(json.dumps(order) + "\n")
            output.close()
            os.replace(temporary_path, directory / f"{label}.json")
        finally:
            temporary_path.unlink(missing_ok=True)


def check_worker_result(result: subprocess.CompletedProcess[str], label: str, operation: str) -> None:
    if result.returncode != 0:
        try:
            error = json_object(decode_object(result.stderr, "herdr refusal").get("error"), "herdr error")
        except TrialFailure:
            error = {}
        if error.get("code") == "agent_blocked":
            worker_blocked(label, operation, json_text(error.get("message"), "herdr blocked message"))
    require_success(result, f"{operation} {label}")


def worker_blocked(label: str, expected: str, happened: str) -> NoReturn:
    write_issue(label, expected, happened)
    raise BlockedWorker(f"{label}: {happened}")


def work(herdr: Herdr, unit: str, role: str, file: str) -> None:
    label = worker_label(unit, role)
    assignment = AssignedWork(Path(file).expanduser().resolve())
    if not assignment.file.is_file():
        raise TrialFailure(f"work order missing: {assignment.file}")
    pane = herdr.pane_id(label)
    assignment.report.unlink(missing_ok=True)
    record_work(label, assignment)
    prompt = f"Read and carry out {assignment.file}. When done, write your report to {assignment.report}."
    result = run_program(herdr.argv("agent", "prompt", pane, prompt))
    check_worker_result(result, label, "carry out the assigned work")


def wait_timeout() -> int:
    try:
        milliseconds = int(os.environ.get("HERDR_TRIAL_WAIT_TIMEOUT", "1800000"))
    except ValueError as error:
        raise UsageFailure("HERDR_TRIAL_WAIT_TIMEOUT must be a nonnegative integer in milliseconds") from error
    if milliseconds < 0:
        raise UsageFailure("HERDR_TRIAL_WAIT_TIMEOUT must be a nonnegative integer in milliseconds")
    return milliseconds


def wait_for_agent(herdr: Herdr, label: WorkerLabel, pane: str, milliseconds: int,
                   *state_arguments: str) -> ObservedAgentState | WaitTimedOut:
    try:
        result = run_program(herdr.argv("agent", "wait", pane, *state_arguments,
                                       "--timeout", str(milliseconds)), timeout=milliseconds / 1000 + 10)
    except subprocess.TimeoutExpired:
        return WaitTimedOut()
    if result.returncode != 0:
        try:
            error = json_object(decode_object(result.stderr, "herdr refusal").get("error"), "herdr error")
        except TrialFailure:
            error = {}
        if error.get("code") == "timeout":
            return WaitTimedOut()
    check_worker_result(result, label, "wait for assigned work to finish")
    response = decode_object(result.stdout, "herdr agent wait")
    payload = json_object(response.get("result"), "herdr agent wait result")
    agent = json_object(payload.get("agent"), "herdr agent state")
    status = json_text(agent.get("agent_status"), "herdr agent lifecycle status")
    if status == "blocked":
        worker_blocked(label, "assigned work completes", "worker blocked at a trust, permission or question prompt")
    return ObservedAgentState(status)


def wait(herdr: Herdr, unit: str, role: str) -> Path:
    label = worker_label(unit, role)
    assignment = assigned_work(label)
    if isinstance(assignment, NoAssignedWork):
        raise TrialFailure(f"no assigned work for {label}")
    milliseconds = wait_timeout()
    pane = herdr.pane_id(label)
    deadline = WorkDeadline(time.monotonic() + milliseconds / 1000)
    while True:
        remaining = deadline.remaining_milliseconds()
        if remaining == 0 and not assignment.report.is_file():
            raise TrialFailure(f"{label}: timed out waiting for report: {assignment.report}")
        outcome = wait_for_agent(herdr, label, pane, remaining)
        if isinstance(outcome, ObservedAgentState) and outcome.status not in ("idle", "done"):
            raise TrialFailure(f"{label}: agent wait returned unsettled state {outcome.status!r}")
        if assignment.report.is_file():
            return assignment.report
        if isinstance(outcome, WaitTimedOut) or deadline.remaining_milliseconds() == 0:
            raise TrialFailure(f"{label}: timed out waiting for report: {assignment.report}")
        # A just-submitted prompt may still look idle. Observe activity before
        # asking for settlement again, without sending the prompt a second time.
        outcome = wait_for_agent(herdr, label, pane, deadline.remaining_milliseconds(), "--until", "working")
        if isinstance(outcome, WaitTimedOut):
            # A fast turn can finish and write its report between state observations.
            if assignment.report.is_file():
                return assignment.report
            raise TrialFailure(f"{label}: timed out waiting for report: {assignment.report}")


def trusted_by_claude(directory: Path, config: Path) -> ProjectTrust:
    project_key = repository_trust_key(directory)
    try:
        data = decode_object(config.read_text(), str(config))
        projects = json_object(data.get("projects", {}), f"{config} projects")
        entry = json_object(projects.get(str(project_key), {}), f"{config} project {project_key}")
    except (OSError, ValueError, TrialFailure) as error:
        raise TrialFailure(f"Claude project {project_key}: {error}") from error
    return ProjectTrust(project_key, entry.get("hasTrustDialogAccepted") is True)


def repository_trust_key(directory: Path) -> Path:
    result = git_result("-C", str(directory), "rev-parse", "--path-format=absolute", "--git-common-dir")
    if result.returncode != 0:
        return directory
    common_directory = result.stdout.strip()
    if not common_directory or not Path(common_directory).is_absolute():
        raise TrialFailure(f"{directory}: git returned no absolute common directory for project trust")
    return Path(common_directory).parent


def trusted_by_codex(directory: Path, config: Path) -> ProjectTrust:
    project_key = repository_trust_key(directory)
    try:
        with config.open("rb") as source:
            data = cast(dict[str, object], tomllib.load(source))
        projects = json_object(data.get("projects", {}), f"{config} projects")
        entry = json_object(projects.get(str(project_key), {}), f"{config} project {project_key}")
    except (OSError, ValueError, TrialFailure) as error:
        raise TrialFailure(f"Codex project {project_key}: {error}") from error
    return ProjectTrust(project_key, entry.get("trust_level") == "trusted")


def require_trust(directory: Path, claude: Path, codex: Path) -> None:
    missing: list[str] = []
    try:
        claude_trust = trusted_by_claude(directory, claude)
        if not claude_trust.accepted:
            missing.append(f"Claude trust missing for project {claude_trust.project_key} in {claude}")
    except (OSError, ValueError, TrialFailure) as error:
        missing.append(f"Claude trust missing: {claude}: {error}")
    try:
        codex_trust = trusted_by_codex(directory, codex)
        if not codex_trust.accepted:
            missing.append(f"Codex trust missing for project {codex_trust.project_key} in {codex}")
    except (OSError, ValueError, TrialFailure) as error:
        missing.append(f"Codex trust missing: {codex}: {error}")
    if missing:
        raise TrialFailure(f"{directory}: {'; '.join(missing)}")


def trust_check(path: str) -> None:
    directory = Path(path).expanduser().resolve()
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude1")).expanduser() / ".claude.json"
    codex = Path(os.environ.get("CODEX_HOME", "~/.codex1")).expanduser() / "config.toml"
    require_trust(directory, claude, codex)


def launch_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith("CLAUDE_")}


def launch_program(argv: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False,
                          timeout=timeout, env=launch_environment())


def launch_result(herdr: Herdr, *arguments: str) -> dict[str, object]:
    result = launch_program(herdr.argv(*arguments))
    require_success(result, f"herdr {' '.join(arguments[:2])}")
    return json_object(decode_object(result.stdout, "herdr response").get("result"), "herdr result")


def launch_control(herdr: Herdr, *arguments: str) -> None:
    require_success(launch_program(herdr.argv(*arguments)), f"herdr {' '.join(arguments[:2])}")


def launch_accounts() -> LaunchAccounts:
    claude = Path(os.environ.get("HERDR_TRIAL_CLAUDE_CONFIG_DIR", "/home/natepiano/.claude1")).expanduser()
    if "HERDR_TRIAL_CODEX_HOME" in os.environ:
        codex = Path(os.environ["HERDR_TRIAL_CODEX_HOME"]).expanduser()
    else:
        from scripts.accounts.accounts import role_account
        try:
            codex = role_account("seats").home
        except ValueError as error:
            raise TrialFailure(f"seats account: {error}") from error
    return LaunchAccounts(claude.resolve(), codex.resolve())


def resolve_agent(task: str) -> AgentConfiguration:
    if "HERDR_TRIAL_RESOLVE" in os.environ:
        argv = [os.environ["HERDR_TRIAL_RESOLVE"], task]
    else:
        registry = Path(__file__).resolve().parents[1] / "agents" / "agents_config.sh"
        argv = ["bash", "-c", f'source {shlex.quote(str(registry))} && agents_resolve_print "$1"', "_", task]
    result = launch_program(argv)
    require_success(result, f"resolve {task}")
    fields = dict(item.split("=", 1) for item in result.stdout.split() if "=" in item)
    family = fields.get("family", "")
    if family not in ("claude", "codex") or not fields.get("agent") or not fields.get("effort"):
        raise TrialFailure(f"resolve {task}: expected claude or codex family, agent and effort")
    return AgentConfiguration(family, fields["agent"], fields["effort"])


def agent_start_timeout() -> int:
    try:
        milliseconds = int(os.environ.get("HERDR_TRIAL_START_TIMEOUT", "90000"))
    except ValueError as error:
        raise UsageFailure("HERDR_TRIAL_START_TIMEOUT for launch must be nonnegative integer milliseconds") from error
    if milliseconds < 0:
        raise UsageFailure("HERDR_TRIAL_START_TIMEOUT for launch must be nonnegative integer milliseconds")
    return milliseconds


def codex_daemons(home: Path) -> list[CodexDaemon]:
    proc = Path(os.environ.get("HERDR_TRIAL_PROC", "/proc"))
    daemons: list[CodexDaemon] = []
    for directory in sorted(proc.iterdir(), key=lambda path: path.name):
        if not directory.name.isdecimal():
            continue
        try:
            arguments = directory.joinpath("cmdline").read_bytes().decode(errors="replace").split("\0")
            if "app-server" not in arguments or not any("codex" in argument for argument in arguments):
                continue
            try:
                environment = directory.joinpath("environ").read_bytes().decode(errors="replace").split("\0")
            except OSError:
                environment = []
            if f"CODEX_HOME={home}" not in environment:
                executable = Path(os.readlink(directory / "exe"))
                if not executable.is_relative_to(home):
                    continue
            # comm (field 2) may contain spaces and parentheses; fields after
            # its final closing parenthesis start at state (field 3).
            fields = directory.joinpath("stat").read_text().rpartition(")")[2].split()
            daemons.append({"pid": int(directory.name), "start_time": int(fields[19])})
        except (OSError, ValueError, IndexError):
            # Processes may exit or be unreadable between directory reads.
            continue
    return daemons


def pane_environment(configuration: AgentConfiguration, accounts: LaunchAccounts,
                     scope: ShowrunnerPane | UnitPane) -> list[str]:
    values: dict[str, str] = {}
    if isinstance(scope, UnitPane):
        values.update({"SHOWRUNNER_UNIT": "herdr-fake", "SHOWRUNNER_UNIT_ID": scope.unit})
    if configuration.family == "claude":
        values.update({"CLAUDE_CONFIG_DIR": str(accounts.claude_config), "ENABLE_TOOL_SEARCH": "true"})
    else:
        values["CODEX_HOME"] = str(accounts.codex_home)
    return [argument for key, value in values.items() for argument in ("--env", f"{key}={value}")]


def native_arguments(configuration: AgentConfiguration, name: str, *, worker: bool) -> tuple[str, ...]:
    if configuration.family == "codex":
        return ("-m", configuration.model, "-c", f"model_reasoning_effort={configuration.effort}",
                "-s", "danger-full-access", "-a", "never")
    arguments = ["--model", configuration.model, "--effort", configuration.effort]
    if not worker:
        arguments.extend(["--remote-control", name])
    arguments.extend(["-n", name])
    return tuple(arguments)


def created_pane(result: dict[str, object], field: str) -> str:
    return json_text(json_object(result.get(field), f"herdr {field}").get("pane_id"), "herdr pane id")


def launch_layout(herdr: Herdr, root: Path, accounts: LaunchAccounts, director: AgentConfiguration,
                  impl: AgentConfiguration, review: AgentConfiguration) -> list[AgentLaunch]:
    result = launch_result(herdr, "workspace", "create", "--label", "herdr-fake", "--cwd", str(root / "repo"),
                           *pane_environment(director, accounts, ShowrunnerPane()), "--no-focus")
    workspace = json_text(json_object(result.get("workspace"), "herdr workspace").get("workspace_id"),
                          "herdr workspace id")
    tab = json_text(json_object(result.get("tab"), "herdr tab").get("tab_id"), "herdr tab id")
    showrunner = created_pane(result, "root_pane")
    launch_control(herdr, "pane", "rename", showrunner, "showrunner")
    launch_control(herdr, "tab", "rename", tab, "showrunner")
    prompt = f"/showrunner:produce {root / 'repo/docs/plans/herdr-fake-production.md'}\n\n{scaffold_files.TRIAL_RULE}"
    showrunner_launch = AgentLaunch("showrunner", showrunner, director,
                                   native_arguments(director, "herdr-fake", worker=False), prompt)
    workers: list[AgentLaunch] = []
    directors: list[AgentLaunch] = []
    for unit in scaffold_files.UNITS:
        cwd = root / "worktrees" / unit.name
        result = launch_result(herdr, "tab", "create", "--workspace", workspace, "--label", unit.name,
                               "--cwd", str(cwd), *pane_environment(director, accounts, UnitPane(unit.name)), "--no-focus")
        director_pane = created_pane(result, "root_pane")
        launch_control(herdr, "pane", "rename", director_pane, unit.name)
        prompt = f"/unit:direct {cwd / f'docs/plans/herdr-fake-{unit.slug}.md'}\n\n{scaffold_files.TRIAL_RULE}"
        directors.append(AgentLaunch(unit.name, director_pane, director,
                         native_arguments(director, f"herdr-fake-{unit.name}", worker=False), prompt))
        previous_pane = director_pane
        for role, configuration, direction in (("impl", impl, "right"), ("review", review, "down")):
            label = f"{unit.name}-{role}"
            result = launch_result(herdr, "pane", "split", previous_pane, "--direction", direction,
                                   "--cwd", str(cwd), *pane_environment(configuration, accounts, UnitPane(unit.name)), "--no-focus")
            pane = created_pane(result, "pane")
            launch_control(herdr, "pane", "rename", pane, label)
            prompt = (f"You are the {role} worker of {unit.name} in the herdr trial. "
                      "Wait; your unit director will send you Work Orders.")
            workers.append(AgentLaunch(label, pane, configuration,
                           native_arguments(configuration, f"herdr-fake-{label}", worker=True), prompt))
            previous_pane = pane
    return [*workers, *directors, showrunner_launch]


def start_agent(herdr: Herdr, agent: AgentLaunch, milliseconds: int) -> None:
    try:
        result = launch_program(herdr.argv("agent", "start", agent.label, "--kind", agent.configuration.family,
                                "--pane", agent.pane_id, "--timeout", str(milliseconds), "--", *agent.arguments),
                                timeout=milliseconds / 1000 + 10)
    except (OSError, subprocess.SubprocessError) as error:
        write_issue(agent.label, "agent starts ready within its startup deadline", str(error))
        raise TrialFailure(f"{agent.label}: startup failed: {error}") from error
    if result.returncode != 0:
        try:
            error = json_object(decode_object(result.stderr, "herdr startup refusal").get("error"), "herdr error")
        except TrialFailure:
            error = {}
        if error.get("code") == "agent_not_ready":
            try:
                screen = launch_program(herdr.argv("agent", "read", agent.label, "--source", "visible"))
                happened = screen.stdout if screen.returncode == 0 else screen.stderr or screen.stdout
            except (OSError, subprocess.SubprocessError) as read_error:
                happened = f"agent_not_ready; startup screen could not be read: {read_error}"
            write_issue(agent.label, "agent starts ready without a trust or permission prompt",
                        happened)
            raise TrialFailure(f"{agent.label}: agent not ready; startup screen logged, prompt left untouched")
    try:
        require_success(result, f"start agent {agent.label}")
    except TrialFailure as error:
        write_issue(agent.label, "agent accepts its configured model, effort, sandbox and approval arguments", str(error))
        raise
    try:
        prompt_result = launch_program(herdr.argv("agent", "prompt", agent.label, agent.opening_prompt))
        require_success(prompt_result, f"prompt agent {agent.label}")
    except (OSError, subprocess.SubprocessError, TrialFailure) as error:
        write_issue(agent.label, "ready agent accepts its opening prompt", str(error))
        raise TrialFailure(f"{agent.label}: opening prompt failed: {error}") from error
    print(f"{agent.label}\t{agent.configuration.family}\t{agent.pane_id}")


def launch(herdr: Herdr) -> None:
    try:
        answering = server_answering(launch_program(herdr.argv("status", "server")))
    except subprocess.TimeoutExpired:
        answering = False
    if not answering:
        raise TrialFailure(f"server {SESSION} not answering: run trial.py up")
    workspaces = launch_result(herdr, "workspace", "list")
    if any(json_object(item, "herdr workspace").get("label") == "herdr-fake"
           for item in json_items(workspaces.get("workspaces"), "herdr workspace list")):
        raise TrialFailure("workspace herdr-fake already exists")
    accounts = launch_accounts()
    root = scratch_root().resolve()
    for directory in (root / "repo", *(root / "worktrees" / unit.name for unit in scaffold_files.UNITS)):
        require_trust(directory, accounts.claude_config / ".claude.json", accounts.codex_home / "config.toml")
    director = resolve_agent("production.director")
    impl, review = resolve_agent("delegate.impl"), resolve_agent("delegate.review")
    if director.family != "claude":
        raise TrialFailure("production.director must use claude for the herdr trial")
    milliseconds = agent_start_timeout()
    resources = started_resources()
    if "codex_daemons_before" not in resources:
        resources["codex_daemons_before"] = codex_daemons(accounts.codex_home)
    before = resources["codex_daemons_before"]
    started = resources.get("codex_daemons_started", [])
    resources["codex_daemons_started"] = started
    write_started_resources(resources)
    agents = launch_layout(herdr, root, accounts, director, impl, review)
    try:
        for agent in agents[:6]:
            if agent.configuration.family == "claude":
                write_issue(agent.label, "Codex worker on the seats account",
                            "worker assignment resolves to claude; launching the configured Claude fallback")
            start_agent(herdr, agent, milliseconds)
    finally:
        tracked_pids = {daemon["pid"] for daemon in [*before, *started]}
        for daemon in codex_daemons(accounts.codex_home):
            if daemon["pid"] not in tracked_pids:
                started.append(daemon)
                tracked_pids.add(daemon["pid"])
        write_started_resources(resources)
    for agent in agents[6:]:
        start_agent(herdr, agent, milliseconds)


def requested_desktop(text: str) -> RequestedDesktop:
    try:
        number = int(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError("desktop must be a positive integer") from error
    if number <= 0:
        raise argparse.ArgumentTypeError("desktop must be a positive integer")
    return RequestedDesktop(number)


def parser() -> TrialParser:
    result = TrialParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    for group, actions in (("server", ("start", "stop")), ("client", ("open", "close"))):
        group_parser = commands.add_parser(group)
        action_parser = group_parser.add_subparsers(dest="action", required=True)
        for action in actions:
            action_command = action_parser.add_parser(action)
            if group == "client" and action == "open":
                _ = action_command.add_argument("--desktop", type=requested_desktop,
                                                 default=KeepCurrentDesktop())
    up_parser = commands.add_parser("up")
    _ = up_parser.add_argument("--desktop", type=requested_desktop, default=KeepCurrentDesktop())
    for command in ("pane-id", "read", "type", "open", "close"):
        pane_parser = commands.add_parser(command)
        _ = pane_parser.add_argument("label")
        if command == "type":
            _ = pane_parser.add_argument("text")
        elif command == "open":
            _ = pane_parser.add_argument("--cwd", dest="directory", default=os.getcwd())
            _ = pane_parser.add_argument("--command", dest="start_command", type=CommandToRun,
                                         default=ShellOnly())
    _ = commands.add_parser("agents")
    _ = commands.add_parser("scaffold")
    _ = commands.add_parser("launch")
    for command in ("work", "wait"):
        worker_parser = commands.add_parser(command)
        _ = worker_parser.add_argument("unit", choices=tuple(unit.name for unit in scaffold_files.UNITS))
        _ = worker_parser.add_argument("role", choices=("impl", "review"))
        if command == "work":
            _ = worker_parser.add_argument("file")
    issue_parser = commands.add_parser("issue")
    for field in ("session", "expected", "happened"):
        _ = issue_parser.add_argument(field)
    trust_parser = commands.add_parser("trust-check")
    _ = trust_parser.add_argument("directory")
    return result


def main(argv: list[str]) -> int:
    try:
        args = parser().parse_args(argv, namespace=TrialArguments())
        herdr = Herdr()
        if args.command == "up":
            # Validate tracking before either part of the combined launch can start.
            _ = started_resources()
            server_start(herdr)
            client_open(herdr, args.desktop)
        elif args.command == "server":
            if args.action == "start":
                server_start(herdr)
            else:
                server_stop(herdr)
        elif args.command == "client":
            if args.action == "open":
                client_open(herdr, args.desktop)
            else:
                require_success(systemctl("stop", CLIENT_UNIT), f"stop {CLIENT_UNIT}")
        elif args.command == "pane-id":
            print(herdr.pane_id(args.label))
        elif args.command == "open":
            print(herdr.open(args.label, args.directory, args.start_command))
        elif args.command == "close":
            herdr.close(args.label)
        elif args.command == "read":
            output = herdr.read(args.label)
            print(output, end="" if output.endswith("\n") else "\n")
        elif args.command == "type":
            herdr.type_text(args.label, args.text)
        elif args.command == "agents":
            labels = {pane["pane_id"]: pane.get("label") or pane["pane_id"] for pane in herdr.panes()}
            for agent in herdr.agents():
                label = labels.get(agent["pane_id"], agent["pane_id"])
                print(f"{label}\t{agent.get('agent') or 'unknown'}\t{agent['agent_status']}")
        elif args.command == "issue":
            write_issue(args.session, args.expected, args.happened)
        elif args.command == "scaffold":
            scaffold()
        elif args.command == "launch":
            launch(herdr)
        elif args.command == "work":
            work(herdr, args.unit, args.role, args.file)
        elif args.command == "wait":
            print(wait(herdr, args.unit, args.role))
        elif args.command == "trust-check":
            trust_check(args.directory)
        return 0
    except (UsageFailure, BlockedWorker) as error:
        print(f"trial: {' '.join(str(error).splitlines())}", file=sys.stderr)
        return 2
    except (TrialFailure, OSError, subprocess.SubprocessError) as error:
        print(f"trial: {' '.join(str(error).splitlines())}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
