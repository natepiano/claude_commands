#!/usr/bin/env python3
"""Control the isolated herdr trial and check its existing agent trust."""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn, NotRequired, TypedDict, cast, override

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


class HerdrReply(TypedDict):
    result: PaneListResult | AgentListResult | CreatedWorkspace


class HerdrError(TypedDict):
    code: str
    message: str


class HerdrRefusal(TypedDict):
    error: HerdrError


class StartedResources(TypedDict):
    systemd_units: list[str]


class TrialIssue(TypedDict):
    time: str
    session: str
    expected: str
    happened: str


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
    return resources


def record_unit(unit: str, resources: StartedResources) -> None:
    if unit in resources["systemd_units"]:
        return
    resources["systemd_units"].append(unit)
    root = scratch_root()
    root.mkdir(parents=True, exist_ok=True)
    _ = (root / "started.json").write_text(json.dumps(resources) + "\n")


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


def client_open(herdr: Herdr) -> None:
    resources = started_resources()
    if CLIENT_UNIT in resources["systemd_units"] and systemctl("is-active", CLIENT_UNIT).returncode == 0:
        return
    launch_unit(CLIENT_UNIT, ["konsole", "-e", *herdr.argv()])


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


def trusted_by_claude(directory: Path, config: Path) -> bool:
    data = decode_object(config.read_text(), str(config))
    projects = json_object(data.get("projects", {}), f"{config} projects")
    for parent in (directory, *directory.parents):
        project = projects.get(str(parent), {})
        entry = json_object(project, f"{config} project {parent}")
        if entry.get("hasTrustDialogAccepted") is True:
            return True
    return False


def trusted_by_codex(directory: Path, config: Path) -> bool:
    with config.open("rb") as source:
        data = cast(dict[str, object], tomllib.load(source))
    projects = json_object(data.get("projects", {}), f"{config} projects")
    for parent in (directory, *directory.parents):
        entry = json_object(projects.get(str(parent), {}), f"{config} project {parent}")
        if entry.get("trust_level") == "trusted":
            return True
    return False


def trust_check(path: str) -> None:
    directory = Path(path).expanduser().resolve()
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude1")).expanduser() / ".claude.json"
    codex = Path(os.environ.get("CODEX_HOME", "~/.codex1")).expanduser() / "config.toml"
    missing: list[str] = []
    for name, config, checker in (
        ("Claude", claude, trusted_by_claude), ("Codex", codex, trusted_by_codex),
    ):
        try:
            if not checker(directory, config):
                missing.append(f"{name} trust missing in {config}")
        except (OSError, ValueError, TrialFailure) as error:
            missing.append(f"{name} trust missing: {config}: {error}")
    if missing:
        raise TrialFailure(f"{directory}: {'; '.join(missing)}")


def parser() -> TrialParser:
    result = TrialParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    for group, actions in (("server", ("start", "stop")), ("client", ("open", "close"))):
        group_parser = commands.add_parser(group)
        action_parser = group_parser.add_subparsers(dest="action", required=True)
        for action in actions:
            _ = action_parser.add_parser(action)
    _ = commands.add_parser("up")
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
            client_open(herdr)
        elif args.command == "server":
            if args.action == "start":
                server_start(herdr)
            else:
                server_stop(herdr)
        elif args.command == "client":
            if args.action == "open":
                client_open(herdr)
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
        elif args.command == "trust-check":
            trust_check(args.directory)
        return 0
    except UsageFailure as error:
        print(f"trial: {' '.join(str(error).splitlines())}", file=sys.stderr)
        return 2
    except (TrialFailure, OSError, subprocess.SubprocessError) as error:
        print(f"trial: {' '.join(str(error).splitlines())}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
