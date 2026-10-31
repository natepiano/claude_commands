#!/usr/bin/env python3
"""Record argv, validate agent names and workspace ids, and replay responses without external tools.

FAKE_HERDR_RESPONSES maps space-joined argv (excluding the executable) to
{stdout, code} entries, optionally with stderr and write_files (path-to-text
report fixtures), require_paths and remove_paths (fixture lifecycle checks). The "default" entry is used for unmatched argv. A list of entries replays successive responses, keeping
the final response for subsequent calls.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import NotRequired, TypedDict, cast
from uuid import NAMESPACE_URL, uuid5


class CommandResponse(TypedDict):
    stdout: str
    code: int
    stderr: NotRequired[str]
    write_files: NotRequired[dict[str, str]]
    require_paths: NotRequired[list[str]]
    remove_paths: NotRequired[list[str]]


class PaneCommandOutcome(TypedDict):
    arguments: list[str]
    command_index: int
    exit_code: int
    moved_pane: NotRequired[str]


def command_refusal(argv: list[str]) -> CommandResponse | None:
    if argv[2:4] == ["agent", "start"] and (len(argv) < 5 or re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", argv[4]) is None):
        return {"stdout": "", "code": 1, "stderr": json.dumps({"error": {
            "code": "invalid_agent_name", "message": "invalid agent name",
        }}) + "\n"}
    for name, workspace in zip(argv, argv[1:]):
        if name == "--workspace" and re.fullmatch(r"w[0-9A-Z]+", workspace) is None:
            return {"stdout": "", "code": 1, "stderr": json.dumps({"error": {
                "code": "workspace_not_found", "message": f"workspace {workspace} not found",
            }, "id": "cli:tab:list"}) + "\n"}
    return None


def command_response(argv: list[str], responses: dict[str, CommandResponse | list[CommandResponse]],
                     previous: list[list[str]]) -> CommandResponse:
    response = responses.get(" ".join(argv), responses.get("default", {"stdout": "", "code": 0}))
    if isinstance(response, list):
        seen = previous.count(argv)
        return response[min(seen, len(response) - 1)]
    return response


def option(arguments: list[str], name: str) -> str | None:
    return arguments[arguments.index(name) + 1] if name in arguments[:-1] else None


def pane_agents(session: str) -> dict[str, bool]:
    """Read the fixture's surviving shells and agents, without consulting the real host."""
    root = os.environ.get("HERDR_PROC")
    if sys.platform == "darwin" or os.environ.get("HERDR_PLATFORM") == "darwin" or not root or not Path(root).is_dir():
        return {}
    running: dict[str, bool] = {}
    for directory in Path(root).glob("[0-9]*"):
        try:
            environment = dict(item.split("=", 1) for item in (directory / "environ").read_text().split("\0") if "=" in item)
            command = (directory / "cmdline").read_text().split("\0")[0]
        except OSError:
            continue
        pane = environment.get("HERDR_PANE_ID", "")
        if pane and environment.get("HERDR_SESSION") == session:
            running[pane] = running.get(pane, False) or Path(command).name in {"claude", "codex"}
    return running


def pane_list(stdout: str, session: list[str], outcomes: list[PaneCommandOutcome]) -> str:
    """Supply stable terminals and live conversations, including pane moves and agent exits.

    Explicit response fields win, including malformed values used to test the reader.
    Launch outcomes are recorded when commands finish, including lifecycle check failures.
    """
    try:
        decoded = cast(object, json.loads(stdout))
    except ValueError:
        return stdout
    if not isinstance(decoded, dict):
        return stdout
    reply = cast(dict[str, object], decoded)
    if not isinstance(reply.get("result"), dict):
        return stdout
    result = cast(dict[str, object], reply["result"])
    if not isinstance(result.get("panes"), list):
        return stdout
    sessions: dict[str, dict[str, str]] = {}
    terminals: dict[str, str] = {}
    moves: list[tuple[str, str]] = []
    for outcome in outcomes:
        command = outcome["arguments"]
        if command[:2] != session or outcome["exit_code"] != 0:
            continue
        if command[2:4] == ["pane", "move"] and (moved := outcome.get("moved_pane")):
            original = command[4]
            moves.append((original, moved))
            terminals[moved] = terminals.get(original, original)
            if original in sessions:
                sessions[moved] = sessions.pop(original)
            continue
        if command[2:4] != ["agent", "start"]:
            continue
        pane = option(command, "--pane")
        if pane is None:
            continue
        kind = option(command, "--kind") or "claude"
        value = option(command, "--session-id") or option(command, "--resume")
        sessions[pane] = {"agent": kind, "kind": "id", "source": f"herdr:{kind}",
                          "value": value or str(uuid5(NAMESPACE_URL, json.dumps([command, outcome["command_index"]])))}
    running = pane_agents(session[1]) if sessions else {}
    # Processes keep their inherited pane id when herdr moves the terminal.
    for original, moved in moves:
        if original in running:
            running[moved] = running.pop(original) or running.get(moved, False)
    for item in cast(list[object], result["panes"]):
        if not isinstance(item, dict):
            continue
        pane = cast(dict[str, object], item)
        ident = pane.get("pane_id")
        if isinstance(ident, str):
            _ = pane.setdefault("workspace_id", ident.split(":", 1)[0])
            _ = pane.setdefault("terminal_id", "term_" + uuid5(NAMESPACE_URL, json.dumps([session, terminals.get(ident, ident)])).hex)
            _ = pane.setdefault("agent_session", sessions.get(ident) if running.get(ident, True) else None)
    return json.dumps(reply)


def herdr_listener(proc: Path, pid: int, name: str, environment: Mapping[str, str]) -> None:
    """Give a fake server ownership of its session's listening socket."""
    fixture_home = Path(environment.get("HOME", str(Path.home())))
    config = Path(environment.get("XDG_CONFIG_HOME") or fixture_home / ".config") / "herdr"
    directory = config if name == "default" else config / "sessions" / name
    table = proc / "net/unix"
    table.parent.mkdir(parents=True, exist_ok=True)
    line = f"0: 2 0 00010000 0001 01 {pid} {directory}/herdr.sock\n"
    existing = table.read_text() if table.exists() else ""
    if line not in existing.splitlines(keepends=True):
        _ = table.write_text(existing + line)
    descriptors = proc / str(pid) / "fd"
    descriptors.mkdir(parents=True, exist_ok=True)
    listener = descriptors / "3"
    listener.unlink(missing_ok=True)
    listener.symlink_to(f"socket:[{pid}]")


def execute(argv: list[str], previous: list[list[str]], outcomes: Path) -> int:
    refusal = command_refusal(argv)
    if refusal is not None:
        _ = sys.stderr.write(refusal.get("stderr", ""))
        return refusal["code"]
    responses = cast(
        dict[str, CommandResponse | list[CommandResponse]],
        json.loads(Path(os.environ["FAKE_HERDR_RESPONSES"]).read_text()),
    )
    response = command_response(argv, responses, previous)
    for required in response.get("require_paths", []):
        if not Path(required).exists():
            _ = sys.stderr.write(f"fixture required path missing: {required}\n")
            return 98
    for removed in response.get("remove_paths", []):
        path = Path(removed)
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    for file, text in response.get("write_files", {}).items():
        _ = Path(file).write_text(text)
    stdout = response["stdout"]
    if argv[2:4] == ["pane", "list"] and response["code"] == 0:
        history = [cast(PaneCommandOutcome, json.loads(line)) for line in outcomes.read_text().splitlines()] if outcomes.exists() else []
        stdout = pane_list(stdout, argv[:2], history)
    _ = sys.stdout.write(stdout)
    _ = sys.stderr.write(response.get("stderr", ""))
    return response["code"]


def main(argv: list[str]) -> int:
    log = Path(os.environ["FAKE_HERDR_LOG"])
    previous = [cast(list[str], json.loads(line)) for line in log.read_text().splitlines()] if log.exists() else []
    with log.open("a") as output:
        _ = output.write(json.dumps(argv) + "\n")
    outcomes = log.with_name(log.name + ".agent-starts")
    code = execute(argv, previous, outcomes)
    if argv[2:4] in (["agent", "start"], ["pane", "move"]):
        outcome = PaneCommandOutcome(arguments=argv, command_index=len(previous), exit_code=code)
        if argv[2:4] == ["pane", "move"] and code == 0:
            responses = cast(dict[str, CommandResponse | list[CommandResponse]],
                             json.loads(Path(os.environ["FAKE_HERDR_RESPONSES"]).read_text()))
            try:
                reply = cast(dict[str, object], json.loads(command_response(argv, responses, previous)["stdout"]))
                result = cast(dict[str, object], reply["result"])
                move = cast(dict[str, object], result["move_result"])
                moved_pane = cast(dict[str, object], move["pane"])["pane_id"]
                if isinstance(moved_pane, str):
                    outcome["moved_pane"] = moved_pane
            except (ValueError, KeyError, TypeError):
                pass
        with outcomes.open("a") as output:
            _ = output.write(json.dumps(outcome) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
