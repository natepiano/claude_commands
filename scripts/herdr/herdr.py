#!/usr/bin/env python3
"""Run named herdr sessions without choosing accounts or answering dialogs.

Process discovery reads HERDR_PANE_ID only in descendants of listening herdr servers
and members of those servers' dedicated systemd units, including handoff survivors.
In shared scopes, the server's preserved pane shell PIDs recover orphaned trees.
Live-checked with herdr 0.9.0 in throwaway session herdr-libcheck: panes set
HERDR_BIN_PATH, HERDR_ENV=1, HERDR_PANE_ID, HERDR_SESSION, HERDR_SOCKET_PATH,
HERDR_TAB_ID, and HERDR_WORKSPACE_ID. Listening socket ownership identifies the
session; ancestry and dedicated unit membership associate pane processes with it.
All caller-supplied timeouts are integer milliseconds.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import NotRequired, TypedDict, cast, override


# Herdr requires agent-start timeouts strictly above this boundary.
_AGENT_START_TIMEOUT_MINIMUM_MS = 3000


class HerdrFailure(Exception):
    """An external command failure or malformed herdr response."""

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code: str = code


class HerdrMissing(HerdrFailure):
    """No herdr binary is available."""


class SessionTaken(HerdrFailure):
    """A new session cannot own a server or systemd unit already in use."""

    def __init__(self, name: str) -> None:
        super().__init__(f"herdr session {name} is already taken", "session_taken")
        self.name: str = name


class AgentBlocked(HerdrFailure):
    """An agent requires user input; nothing is sent in response."""


class AgentNotReady(HerdrFailure):
    """Startup stopped before interactive readiness, with its visible screen."""

    def __init__(self, screen: str) -> None:
        super().__init__(screen, "agent_not_ready")
        self.screen: str = screen


@dataclass(frozen=True)
class OpeningPrompt:
    text: str


@dataclass(frozen=True)
class NoOpeningPrompt:
    """Leave the ready agent waiting for the user to type."""


@dataclass(frozen=True)
class ClientsAttached:
    count: int


@dataclass(frozen=True)
class NoClientAttached:
    """No interactive herdr client is attached to this session."""


@dataclass(frozen=True)
class ClientsUnreadable:
    detail: str


@dataclass(frozen=True)
class ObservedAgentState:
    status: str


@dataclass(frozen=True)
class WaitTimedOut:
    """The CLI or its subprocess exhausted the wait deadline."""


@dataclass(frozen=True)
class HerdrPlace:
    session: str
    pane: str


@dataclass(frozen=True)
class NotInHerdr:
    """The process has no identifiable herdr pane and listening server."""


@dataclass(frozen=True)
class HerdrProcess:
    pid: int
    place: HerdrPlace
    marks: dict[str, str]
    command: str
    cwd: str


@dataclass(frozen=True)
class Trusted:
    """This account accepted one of the folder's exact repository trust keys."""

    folder: Path
    account_file: Path
    project_key: Path


@dataclass(frozen=True)
class Untrusted:
    """None of the folder's own, repository-root or main-checkout keys is accepted."""

    folder: Path
    account_file: Path
    keys_tried: tuple[Path, ...]


@dataclass(frozen=True)
class UnreadableTrustFile:
    """Trust cannot be established from this account file; its contents stay private."""

    folder: Path
    account_file: Path
    error_class: str

    @override
    def __str__(self) -> str:
        return f"cannot read {self.account_file} to check Claude trust ({self.error_class})"


class ClaudeTrustEntry(TypedDict):
    hasTrustDialogAccepted: bool


class ClaudeTrustConfiguration(TypedDict):
    projects: dict[str, ClaudeTrustEntry]


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
class PaneAtShell:
    """The pane's foreground process group is its original shell."""


@dataclass(frozen=True)
class PaneRunning:
    commands: tuple[str, ...]


class SessionEntry(TypedDict):
    name: str
    running: bool
    default: bool
    session_dir: str
    socket_path: str


class SessionListResult(TypedDict):
    sessions: list[SessionEntry]


class PaneIdentity(TypedDict):
    pane_id: str
    label: NotRequired[str | None]


class PaneListResult(TypedDict):
    panes: list[PaneIdentity]


class PaneProcessInfo(TypedDict):
    shell_pid: NotRequired[int]


class PaneProcessInfoResult(TypedDict):
    process_info: PaneProcessInfo


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


class AgentLifecycle(TypedDict):
    agent_status: str


class AgentWaitResult(TypedDict):
    agent: AgentLifecycle


class WorkerReadiness(TypedDict):
    agent_status: str
    interactive_ready: bool


class AgentGetResult(TypedDict):
    agent: WorkerReadiness


class HerdrError(TypedDict):
    code: str
    message: str


class HerdrRefusal(TypedDict):
    error: HerdrError


def json_object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise HerdrFailure(f"{context}: expected a JSON object")
    return cast(dict[str, object], value)


def decode_object(text: str, context: str) -> dict[str, object]:
    try:
        value = cast(object, json.loads(text))
    except ValueError as error:
        raise HerdrFailure(f"{context}: invalid JSON") from error
    return json_object(value, context)


def json_text(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise HerdrFailure(f"{context}: expected text")
    return value


def json_items(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise HerdrFailure(f"{context}: expected a JSON list")
    return cast(list[object], value)


def run_program(argv: list[str], timeout: float = 10.0, *,
                environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=timeout, env=environment)
    except OSError as error:
        raise HerdrFailure(f"{argv[0]}: {error}") from error


def require_success(result: subprocess.CompletedProcess[str], context: str) -> None:
    if result.returncode == 0:
        return
    code = ""
    detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
    for text in (result.stderr, result.stdout):
        try:
            error = json_object(decode_object(text, context).get("error"), context)
            code = json_text(error.get("code"), context)
            detail = json_text(error.get("message"), context)
            break
        except HerdrFailure:
            continue
    message = f"{context}: {code + ': ' if code else ''}{' '.join(detail.splitlines())}"
    if code == "agent_blocked":
        raise AgentBlocked(message, code)
    raise HerdrFailure(message, code)


def herdr_binary() -> str:
    binary = os.environ.get("HERDR_BIN") or shutil.which("herdr")
    if not binary:
        raise HerdrMissing("herdr is not installed")
    return binary


class Herdr:
    def __init__(self, session: str) -> None:
        self.binary: str = herdr_binary()
        self.session: str = session

    def argv(self, *arguments: str) -> list[str]:
        """The outside-pane targeting form verified with herdr 0.9.0."""
        return [self.binary, "--session", self.session, *arguments]

    def run(self, *arguments: str) -> str:
        context = f"herdr {' '.join(arguments[:2])}"
        try:
            result = run_program(self.argv(*arguments))
        except subprocess.TimeoutExpired as error:
            raise HerdrFailure(f"{context}: no answer after {error.timeout:g} s") from error
        require_success(result, context)
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
                raise HerdrFailure("herdr pane label: expected text or null")
            panes.append({"pane_id": json_text(pane.get("pane_id"), "herdr pane id"), "label": label})
        return panes

    def lookup(self, label: str) -> PaneFound | NoPane | SeveralPanes:
        matches = [pane["pane_id"] for pane in self.panes() if pane.get("label") == label]
        if not matches:
            return NoPane(label)
        if len(matches) > 1:
            return SeveralPanes(label, len(matches))
        return PaneFound(matches[0])

    def pane_activity(self, pane: str) -> PaneAtShell | PaneRunning:
        """Describe whether the pane is at its shell or running foreground work."""
        context = f"herdr session {self.session} pane {pane} process info"
        info = json_object(self.result("pane", "process-info", "--pane", pane).get("process_info"), context)
        foreground = info.get("foreground_process_group_id")
        shell = info.get("shell_pid")
        for pid in (foreground, shell):
            if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
                raise HerdrFailure(f"{context}: expected positive process ids")
        commands = tuple(
            json_text(json_object(process, context).get("name"), context)
            for process in json_items(info.get("foreground_processes"), context)
        )
        return PaneAtShell() if foreground == shell else PaneRunning(commands)

    def pane_id(self, label: str) -> str:
        found = self.lookup(label)
        if isinstance(found, NoPane):
            raise HerdrFailure(f"no pane for label {label!r}")
        if isinstance(found, SeveralPanes):
            raise HerdrFailure(f"several panes ({found.count}) for label {label!r}")
        return found.pane_id

    def open_pane(self, label: str, cwd: str, environment: dict[str, str]) -> str:
        arguments = ["workspace", "create", "--label", label, "--cwd", cwd]
        for key, value in {**environment, "HERDR_AGENT_PANE": "1"}.items():
            arguments.extend(["--env", f"{key}={value}"])
        result = self.result(*arguments, "--no-focus")
        root_pane = json_object(result.get("root_pane"), "herdr workspace root pane")
        pane = json_text(root_pane.get("pane_id"), "herdr workspace root pane id")
        _ = self.run("pane", "rename", pane, label)
        return pane

    def start_agent(self, name: str, pane: str, kind: str, agent_arguments: list[str],
                    opening: OpeningPrompt | NoOpeningPrompt, timeout_ms: int) -> None:
        """Wait through busy-pane refusals, start the executable, then prompt once."""
        _check_agent_arguments(kind, agent_arguments)
        _timeout(timeout_ms)
        deadline = time.monotonic() + timeout_ms / 1000
        attempt_timeout_ms = timeout_ms
        try:
            while True:
                result = run_program(self.argv("agent", "start", _agent_name(name), "--kind", kind, "--pane", pane,
                                               "--timeout", str(attempt_timeout_ms), "--", *agent_arguments),
                                     timeout=attempt_timeout_ms / 1000 + 10)
                try:
                    require_success(result, f"start agent {name}")
                except HerdrFailure as error:
                    if error.code != "agent_pane_busy":
                        raise
                    remaining_ms = int((deadline - time.monotonic()) * 1000)
                    if remaining_ms <= _AGENT_START_TIMEOUT_MINIMUM_MS:
                        raise
                    time.sleep(0.1)
                    attempt_timeout_ms = int((deadline - time.monotonic()) * 1000)
                    if attempt_timeout_ms <= _AGENT_START_TIMEOUT_MINIMUM_MS:
                        raise
                    continue
                break
        except HerdrFailure as error:
            if error.code != "agent_not_ready":
                raise
            try:
                screen = self.run("pane", "read", pane, "--source", "visible")
            except (OSError, subprocess.SubprocessError, HerdrFailure) as read_error:
                screen = f"agent_not_ready; startup screen could not be read: {read_error}"
            raise AgentNotReady(screen) from error
        if isinstance(opening, OpeningPrompt):
            self.prompt(pane, opening.text)

    def readiness(self, pane: str) -> WorkerReadiness:
        agent = json_object(self.result("agent", "get", pane).get("agent"), "herdr agent get")
        status = json_text(agent.get("agent_status"), "herdr agent readiness status")
        ready = agent.get("interactive_ready")
        if not isinstance(ready, bool):
            raise HerdrFailure("herdr agent readiness: expected interactive_ready boolean")
        return {"agent_status": status, "interactive_ready": ready}

    def prompt(self, pane: str, text: str) -> None:
        _ = self.run("agent", "prompt", pane, text)

    def wait_until(self, pane: str, state: str, timeout_ms: int) -> ObservedAgentState | WaitTimedOut:
        _timeout(timeout_ms)
        try:
            result = run_program(self.argv("agent", "wait", pane, "--until", state,
                                           "--timeout", str(timeout_ms)), timeout=timeout_ms / 1000 + 10)
        except subprocess.TimeoutExpired:
            return WaitTimedOut()
        try:
            require_success(result, "herdr agent wait")
        except HerdrFailure as error:
            if error.code == "timeout":
                return WaitTimedOut()
            raise
        payload = json_object(decode_object(result.stdout, "herdr agent wait").get("result"), "herdr wait result")
        agent = json_object(payload.get("agent"), "herdr agent state")
        status = json_text(agent.get("agent_status"), "herdr agent lifecycle status")
        if status == "blocked":
            raise AgentBlocked("agent blocked at a trust, permission or question prompt", "agent_blocked")
        return ObservedAgentState(status)

    def read(self, pane: str, lines: int, *, joined: bool, ansi: bool) -> str:
        arguments = ["pane", "read", pane, "--source", "recent-unwrapped" if joined else "recent",
                     "--lines", str(lines)]
        if ansi:
            arguments.extend(["--format", "ansi"])
        return self.run(*arguments)

    def send_keys(self, pane: str, *keys: str) -> None:
        _ = self.run("pane", "send-keys", pane, *keys)

    def close_pane(self, pane: str) -> None:
        _ = self.run("pane", "close", pane)

    def agents(self) -> list[AgentOccupant]:
        result = self.result("agent", "list")
        agents: list[AgentOccupant] = []
        for item in json_items(result.get("agents"), "herdr agent list"):
            agent = json_object(item, "herdr agent")
            kind = agent.get("agent")
            if kind is not None and not isinstance(kind, str):
                raise HerdrFailure("herdr agent kind: expected text or null")
            agents.append({
                "pane_id": json_text(agent.get("pane_id"), "herdr agent pane id"),
                "agent": kind,
                "agent_status": json_text(agent.get("agent_status"), "herdr agent state"),
            })
        return agents


def claude_trust(folder: Path, claude_json: Path) -> Trusted | Untrusted | UnreadableTrustFile:
    """Check exact folder, repository-root and main-checkout keys, never ancestors.

    Before creating a worktree, callers check repository_trust_key(checkout)
    instead of the missing folder, so only the main checkout can authorize it.
    """
    folder = folder.resolve()
    try:
        raw = cast(object, json.loads(claude_json.read_text(encoding="utf-8")))
        if not isinstance(raw, dict):
            raise ValueError()
        projects = cast(dict[str, object], raw).get("projects", {})
        if not isinstance(projects, dict):
            raise ValueError()
        configuration: ClaudeTrustConfiguration = {"projects": {
            key: {"hasTrustDialogAccepted": isinstance(entry, dict)
                  and cast(dict[str, object], entry).get("hasTrustDialogAccepted") is True}
            for key, entry in cast(dict[str, object], projects).items()
        }}
    except (OSError, UnicodeError, ValueError) as error:
        return UnreadableTrustFile(folder, claude_json, type(error).__name__)
    keys = tuple(dict.fromkeys((folder, repository_root(folder), repository_trust_key(folder))))
    for key in keys:
        if configuration["projects"].get(str(key), {"hasTrustDialogAccepted": False})["hasTrustDialogAccepted"]:
            return Trusted(folder, claude_json, key)
    return Untrusted(folder, claude_json, keys)


def repository_root(directory: Path) -> Path:
    return _repository_key(directory, "--show-toplevel")


def repository_trust_key(directory: Path) -> Path:
    return _repository_key(directory, "--git-common-dir")


def _repository_key(directory: Path, flag: str) -> Path:
    # Trust belongs to the requested directory, independent of inherited Git
    # repository overrides and the caller's global or system configuration.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    result = run_program(["git", "-C", str(directory), "rev-parse", "--path-format=absolute", flag],
                         environment=environment)
    if result.returncode != 0:
        return directory
    found = result.stdout.strip()
    if not found or not Path(found).is_absolute():
        raise HerdrFailure(f"{directory}: git returned no absolute {flag} path for project trust")
    path = Path(found)
    return path.parent if flag == "--git-common-dir" else path



def live_sessions() -> list[SessionEntry]:
    response = decode_object(Herdr("default").run("session", "list", "--json"), "herdr session list")
    payload = json_object(response["result"], "herdr session result") if "result" in response else response
    sessions: list[SessionEntry] = []
    for item in json_items(payload.get("sessions"), "herdr sessions"):
        entry = json_object(item, "herdr session")
        running, default = entry.get("running"), entry.get("default")
        if not isinstance(running, bool) or not isinstance(default, bool):
            raise HerdrFailure("herdr session: running and default must be booleans")
        session: SessionEntry = {
            "name": json_text(entry.get("name"), "herdr session name"),
            "running": running,
            "default": default,
            "session_dir": json_text(entry.get("session_dir"), "herdr session directory"),
            "socket_path": json_text(entry.get("socket_path"), "herdr session socket"),
        }
        if running:
            sessions.append(session)
    return sessions


def server_unit(name: str) -> str:
    _validate_session_name(name)
    return "herdr-" + name + ".service"


def _validate_session_name(name: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name) or name in (".", ".."):
        raise ValueError("herdr session name must be 1–64 ASCII characters [A-Za-z0-9_.-], never . or ..")


def generated_session_name(text: str, suffix: str = "") -> str:
    tail = "-" + suffix if suffix else ""
    if tail and (not re.fullmatch(r"[A-Za-z0-9_.-]+", suffix) or len(tail) >= 64):
        raise ValueError("herdr session suffix must use [A-Za-z0-9_.-] and leave room for a name")
    base = re.sub(r"[^A-Za-z0-9_.-]", "-", text)[:64 - len(tail)] or "-"
    if base in (".", ".."):
        base = "-"
    return base + tail


def _timeout(timeout_ms: object) -> None:
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms < 0:
        raise HerdrFailure("timeout must be a nonnegative integer in milliseconds")


def server_answering(status: subprocess.CompletedProcess[str]) -> bool:
    return status.returncode == 0 and "status: running" in status.stdout.splitlines()


def _clear_restored_workspaces(client: Herdr) -> None:
    """A server we just spawned holds restored layout, never live work."""
    result = client.result("workspace", "list")
    workspaces = [json_text(json_object(item, "herdr workspace").get("workspace_id"),
                            "herdr workspace id")
                  for item in json_items(result.get("workspaces"), "herdr workspace list")]
    for workspace in workspaces:
        try:
            _ = client.run("workspace", "close", workspace)
        except HerdrFailure as error:
            raise HerdrFailure(
                f"herdr session {client.session}: cannot close restored workspace {workspace}: {error}",
                error.code,
            ) from error


def _server_unit_active(name: str) -> bool:
    return run_program([
        os.environ.get("SYSTEMCTL", "systemctl"), "--user", "is-active", server_unit(name),
    ]).returncode == 0


def start_server(name: str, timeout_ms: int) -> None:
    _validate_session_name(name)
    _timeout(timeout_ms)
    herdr = Herdr(name)
    deadline = time.monotonic() + timeout_ms / 1000
    try:
        if server_answering(run_program(herdr.argv("status", "server"), timeout=max(0.001, timeout_ms / 1000))):
            return
    except subprocess.TimeoutExpired:
        pass
    require_success(run_program([
        os.environ.get("SYSTEMD_RUN", "systemd-run"), "--user", f"--unit={server_unit(name)}",
        "--collect", *herdr.argv("server"),
    ]), f"start {server_unit(name)}")
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HerdrFailure(f"server {name} not answering after {timeout_ms} ms")
        try:
            if server_answering(run_program(herdr.argv("status", "server"), timeout=max(0.001, remaining))):
                if _server_unit_active(name):
                    _clear_restored_workspaces(herdr)
                return
        except subprocess.TimeoutExpired:
            pass
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.1, remaining))


def start_new_server(name: str, timeout_ms: int) -> None:
    """Create an exclusive server; never adopt a server another launch started."""
    _validate_session_name(name)
    _timeout(timeout_ms)
    herdr = Herdr(name)
    deadline = time.monotonic() + timeout_ms / 1000
    try:
        if server_answering(run_program(herdr.argv("status", "server"), timeout=max(0.001, timeout_ms / 1000))):
            raise SessionTaken(name)
    except subprocess.TimeoutExpired:
        pass
    created = run_program([
        os.environ.get("SYSTEMD_RUN", "systemd-run"), "--user", f"--unit={server_unit(name)}",
        "--collect", *herdr.argv("server"),
    ])
    if created.returncode:
        detail = (created.stdout + created.stderr).lower()
        if "already exists" in detail or "already loaded" in detail:
            raise SessionTaken(name)
        require_success(created, f"start {server_unit(name)}")
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HerdrFailure(f"server {name} not answering after {timeout_ms} ms")
        try:
            if server_answering(run_program(herdr.argv("status", "server"), timeout=max(0.001, remaining))):
                if not _server_unit_active(name):
                    raise SessionTaken(name)
                _clear_restored_workspaces(herdr)
                return
        except subprocess.TimeoutExpired:
            pass
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.1, remaining))


def stop_session(name: str) -> None:
    _validate_session_name(name)
    herdr = Herdr(name)
    try:
        result = run_program(herdr.argv("server", "stop"))
    finally:
        systemctl = os.environ.get("SYSTEMCTL", "systemctl")
        if run_program([systemctl, "--user", "is-active", server_unit(name)]).returncode == 0:
            require_success(run_program([systemctl, "--user", "stop", server_unit(name)]), f"stop {server_unit(name)}")
    require_success(result, f"stop herdr session {name}")


def open_pane(name: str, label: str, cwd: str, environment: dict[str, str]) -> str:
    return Herdr(name).open_pane(label, cwd, environment)


def _proc_root() -> Path:
    return Path(os.environ.get("HERDR_PROC", "/proc"))


def _process_environment(process: Path) -> dict[str, str]:
    return dict(entry.split("=", 1) for entry in process.joinpath("environ").read_bytes().decode(errors="replace").split("\0") if "=" in entry)


def _process_arguments(process: Path) -> list[str]:
    return [value for value in process.joinpath("cmdline").read_bytes().decode(errors="replace").split("\0") if value]


def _herdr_command(arguments: list[str], default_session: str) -> tuple[str, list[str]]:
    if not arguments or Path(arguments[0]).name != "herdr":
        return "", []
    session = default_session
    index = 1
    while index < len(arguments) and arguments[index].startswith("-"):
        option = arguments[index]
        if option == "--":
            index += 1
            break
        if option in ("--session", "--remote", "--remote-keybindings"):
            if index + 1 >= len(arguments):
                return "", []
            if option == "--session":
                session = arguments[index + 1]
            index += 2
        elif option.startswith("--session="):
            session = option.split("=", 1)[1]
            index += 1
        elif option in ("--help", "-h", "--version", "-V"):
            return "", []
        else:
            index += 1
    return session, arguments[index:]


def attached_clients(name: str) -> ClientsAttached | NoClientAttached | ClientsUnreadable:
    try:
        processes = list(_proc_root().iterdir())
    except OSError as error:
        return ClientsUnreadable(str(error))
    count = 0
    for process in processes:
        if not process.name.isdecimal():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            arguments = _process_arguments(process)
            session, command = _herdr_command(arguments, "default")
            if session and not command:
                environment = _process_environment(process)
                session, command = _herdr_command(arguments, environment.get("HERDR_SESSION") or "default")
        except FileNotFoundError:
            continue
        except OSError as error:
            return ClientsUnreadable(str(error))
        if (session == name and not command) or command == ["session", "attach", name]:
            count += 1
    return ClientsAttached(count) if count else NoClientAttached()


def server_environment(name: str) -> dict[str, str]:
    """Read the environment of the process holding this session's listener."""
    context = f"herdr session {name} server environment"
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "herdr"
    session_directory = config if name == "default" else config / "sessions" / name
    socket_path = str(session_directory / "herdr.sock")
    try:
        sockets = _proc_root().joinpath("net", "unix").read_text().splitlines()
    except (OSError, UnicodeError) as error:
        raise HerdrFailure(f"{context}: cannot read listening sockets") from error
    listening_inodes: list[str] = []
    for line in sockets:
        fields = line.split(maxsplit=7)
        if (len(fields) == 8 and fields[7] == socket_path
                and fields[3] == "00010000" and fields[6].isdecimal()):
            listening_inodes.append(fields[6])
    if len(listening_inodes) != 1:
        raise HerdrFailure(f"{context}: expected one listening socket, found {len(listening_inodes)}")
    listener = f"socket:[{listening_inodes[0]}]"
    try:
        candidates = list(_proc_root().iterdir())
    except OSError as error:
        raise HerdrFailure(f"{context}: cannot read process directory") from error
    matches: list[Path] = []
    for process in candidates:
        if not process.name.isdecimal():
            continue
        try:
            descriptors = list(process.joinpath("fd").iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                if os.readlink(descriptor) == listener:
                    matches.append(process)
                    break
            except OSError:
                continue
    if len(matches) != 1:
        raise HerdrFailure(f"{context}: expected one listening socket holder, found {len(matches)}")
    try:
        return _process_environment(matches[0])
    except OSError as error:
        raise HerdrFailure(f"{context}: cannot read process environment") from error


def herdr_place_of(pid: int) -> HerdrPlace | NotInHerdr:
    try:
        pane = _process_environment(_proc_root() / str(pid)).get("HERDR_PANE_ID", "")
        if not pane:
            return NotInHerdr()
        candidates = [path for path in _proc_root().iterdir() if path.name.isdecimal()]
    except OSError:
        return NotInHerdr()
    session = _process_sessions(candidates, _live_server_processes(candidates)).get(pid, "")
    return HerdrPlace(session, pane) if session else NotInHerdr()


def _live_server_processes(candidates: list[Path]) -> dict[int, str]:
    """Identify servers by listening socket ownership, independent of argv."""
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "herdr"
    try:
        lines = _proc_root().joinpath("net", "unix").read_text().splitlines()
    except OSError:
        return {}
    listeners: dict[str, str] = {}
    for line in lines:
        fields = line.split(maxsplit=7)
        if len(fields) != 8 or fields[3] != "00010000":
            continue
        path = Path(fields[7])
        if path == config / "herdr.sock":
            listeners[fields[6]] = "default"
        elif path.name == "herdr.sock" and path.parent.parent == config / "sessions":
            listeners[fields[6]] = path.parent.name
    if not listeners:
        return {}
    holders: dict[str, set[int]] = {}
    for process in candidates:
        try:
            if process.stat().st_uid != os.getuid():
                continue
            for descriptor in process.joinpath("fd").iterdir():
                try:
                    link = os.readlink(descriptor)
                except OSError:
                    continue
                if link.startswith("socket:[") and link.endswith("]"):
                    inode = link[8:-1]
                    if inode in listeners:
                        holders.setdefault(inode, set()).add(int(process.name))
        except OSError:
            continue
    return {next(iter(pids)): listeners[inode] for inode, pids in holders.items() if len(pids) == 1}


def _systemd_cgroups(process: Path) -> list[tuple[str, Path]]:
    """Read unified or legacy systemd membership, never a process environment."""
    try:
        lines = process.joinpath("cgroup").read_text().splitlines()
    except OSError:
        return []
    groups: list[tuple[str, Path]] = []
    for line in lines:
        fields = line.split(":", 2)
        if len(fields) == 3 and fields[1] in ("", "name=systemd") and fields[2].startswith("/"):
            groups.append((fields[0], Path(fields[2])))
    return groups


def _pane_shell_pids(session: str) -> list[int]:
    """Read the shell identities retained in the live server's PTY runtimes."""
    try:
        client = Herdr(session)
        panes = client.panes()
    except HerdrFailure:
        return []
    shells: list[int] = []
    for pane in panes:
        try:
            info = json_object(client.result("pane", "process-info", "--pane", pane["pane_id"])
                               .get("process_info"), "herdr pane process info")
        except HerdrFailure:
            continue
        shell = info.get("shell_pid")
        if isinstance(shell, int) and not isinstance(shell, bool) and shell > 0:
            shells.append(shell)
    return shells


def _process_sessions(candidates: list[Path], servers: dict[int, str]) -> dict[int, str]:
    """Associate descendants and handoff survivors with socket-owned sessions."""
    if not servers:
        return {}
    unit_groups: dict[tuple[str, Path], str] = {}
    shared_groups: dict[tuple[str, Path], set[str]] = {}
    for pid, session in servers.items():
        try:
            unit = server_unit(session)
        except ValueError:
            continue
        for hierarchy, path in _systemd_cgroups(_proc_root() / str(pid)):
            if unit in path.parts:
                root = Path(*path.parts[:path.parts.index(unit) + 1])
                unit_groups[hierarchy, root] = session
            else:
                shared_groups.setdefault((hierarchy, path), set()).add(session)
    children: dict[int, list[Path]] = {}
    for process in candidates:
        try:
            stat = process.joinpath("stat").read_text()
            parent = int(stat[stat.rindex(")") + 1:].split()[1])
            children.setdefault(parent, []).append(process)
        except (OSError, ValueError, IndexError):
            continue
    descendants = [(process, session) for pid, session in servers.items() for process in children.get(pid, [])]
    sessions = dict(servers)
    while descendants:
        process, session = descendants.pop()
        pid = int(process.name)
        if pid in sessions:
            continue
        sessions[pid] = session
        descendants.extend((child, session) for child in children.get(pid, []))
    if unit_groups or shared_groups:
        # Handoff transfers PTYs without reparenting their processes to the
        # replacement. setsid leaves their dedicated service cgroup unchanged.
        shared_sessions: set[str] = set()
        for process in candidates:
            pid = int(process.name)
            if pid in sessions:
                continue
            try:
                if process.stat().st_uid != os.getuid():
                    continue
            except OSError:
                continue
            for hierarchy, path in _systemd_cgroups(process):
                shared_sessions.update(shared_groups.get((hierarchy, path), set()))
                for ancestor in (path, *path.parents):
                    session = unit_groups.get((hierarchy, ancestor), "")
                    if session:
                        sessions[pid] = session
                        break
                if pid in sessions:
                    break
        # Handoff retains child_pid in each imported PTY runtime. Unlike a
        # shared cgroup or HERDR_SESSION, this identifies the actual pane shell.
        for session in shared_sessions:
            for shell in _pane_shell_pids(session):
                descendants = [(_proc_root() / str(shell), session)]
                while descendants:
                    process, owner = descendants.pop()
                    pid = int(process.name)
                    if pid in sessions:
                        continue
                    sessions[pid] = owner
                    descendants.extend((child, owner) for child in children.get(pid, []))
    return {pid: session for pid, session in sessions.items() if pid not in servers}


def herdr_processes() -> list[HerdrProcess]:
    processes: list[HerdrProcess] = []
    try:
        candidates = [path for path in _proc_root().iterdir() if path.name.isdecimal()]
    except OSError:
        return processes
    sessions = _process_sessions(candidates, _live_server_processes(candidates))
    for pid, session in sessions.items():
        process = _proc_root() / str(pid)
        try:
            if process.stat().st_uid != os.getuid():
                continue
            environment = _process_environment(process)
            pane = environment.get("HERDR_PANE_ID", "")
            if not pane:
                continue
            arguments = _process_arguments(process)
            if not arguments:
                continue
            cwd = str(process.joinpath("cwd").resolve(strict=True))
            marks = {key: environment[key] for key in ("SHOWRUNNER_UNIT", "SHOWRUNNER_UNIT_ID") if key in environment}
            processes.append(HerdrProcess(pid, HerdrPlace(session, pane), marks, Path(arguments[0]).name, cwd))
        except (OSError, ValueError, RuntimeError):
            continue
    return processes


def _check_agent_arguments(kind: str, agent_arguments: list[str]) -> None:
    if agent_arguments and agent_arguments[0] == kind:
        raise ValueError(f"agent_arguments must omit the {kind!r} executable selected by --kind")


def _agent_name(name: str) -> str:
    mapped = re.sub(r"[^a-z0-9_-]", "-", name.lower())
    if not mapped or not "a" <= mapped[0] <= "z":
        mapped = "a-" + mapped
    return mapped[:32]


def start_agent(name: str, pane: str, kind: str, agent_arguments: list[str],
                opening: OpeningPrompt | NoOpeningPrompt, timeout_ms: int) -> None:
    """Start in the named session, using the pane's label as the agent label."""
    _check_agent_arguments(kind, agent_arguments)
    herdr = Herdr(name)
    labels = [row.get("label") for row in herdr.panes() if row["pane_id"] == pane]
    label = labels[0] if labels else pane
    herdr.start_agent(label or pane, pane, kind, agent_arguments, opening, timeout_ms)
