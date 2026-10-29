#!/usr/bin/env python3
"""Refuse build calls from sessions subject to natedev's hold."""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tool_call_text import scan_text

CANDIDATE = re.compile(r"\b(?:cargo|merge_gate\.py)\b")
CARGO_BUILDS = frozenset({"build", "b", "check", "c", "clippy", "test", "t", "nextest", "run", "r",
                          "bench", "doc", "d", "rustc", "rustdoc", "fix", "install", "mend", "miri"})
PREFIXES = frozenset({"env", "sudo", "exec", "command", "nohup", "time", "timeout", "nice",
                      "!", "{", "then", "else", "do", "if", "while", "until"})
PREFIX_ARGUMENTS = frozenset({"-u", "--unset", "--user", "-g", "--group", "-n", "--adjustment",
                             "-s", "--signal", "-k", "--kill-after"})
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*=")
HEREDOC = re.compile(r"<<(-?)[ \t]*(['\"]?)([A-Za-z_][A-Za-z_0-9]*)\2[^\n]*\n")
SHELL_HEREDOC = re.compile(r"\b(?:bash|sh|zsh)(?:[ \t]+[^<\n]*)?[ \t]*$")


class BuildHoldCommandInput(TypedDict, total=False):
    command: str | list[str]
    cmd: str | list[str]


class BuildHoldHookPayload(TypedDict, total=False):
    session_id: str
    tool_name: str
    tool_input: BuildHoldCommandInput


def without_literal_heredocs(command: str) -> str:
    """Keep written command examples out of the build check."""
    for found in reversed(list(HEREDOC.finditer(command))):
        end = re.search(rf"^{('[\t]*' if found.group(1) else '')}{found.group(3)}$",
                        command[found.end():], re.MULTILINE)
        if end:
            start, stop = found.end(), found.end() + end.end()
            body = command[start:stop]
            if SHELL_HEREDOC.search(command[:found.start()]):
                continue
            if not found.group(2) and ("$(" in body or "`" in body):
                continue
            command = command[:start] + "\n" * command[start:stop].count("\n") + command[stop:]
    return command


def builds(words: list[str], *, merge_gate_only: bool = False) -> bool:
    """Test one shell command after its environment and command prefixes."""
    while words:
        if ASSIGNMENT.match(words[0]):
            words = words[1:]
            continue
        prefix = Path(words[0]).name
        if prefix not in PREFIXES:
            break
        words = words[1:]
        while words and words[0].startswith("-"):
            words = words[2:] if words[0] in PREFIX_ARGUMENTS else words[1:]
        if prefix == "timeout":
            words = words[1:]
    if not words:
        return False
    program = Path(words[0]).name
    arguments = words[1:]
    if program in {"bash", "sh", "zsh"}:
        return any("c" in option and building_command(script, merge_gate_only=merge_gate_only)
                   for option, script in zip(arguments, arguments[1:]) if option.startswith("-"))
    if program == "merge_gate.py":
        return True
    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", program) or (program == "py" and Path(words[0]).parent.name == "lib"):
        while arguments and arguments[0].startswith("-"):
            option = arguments[0]
            if option == "--":
                arguments = arguments[1:]
                break
            if option.startswith(("-c", "-m")):
                return False
            arguments = arguments[2:] if option in {"-W", "-X", "--check-hash-based-pycs"} else arguments[1:]
        return bool(arguments and Path(arguments[0]).name == "merge_gate.py")
    if merge_gate_only:
        return False
    if program != "cargo":
        return False
    if arguments and arguments[0].startswith("+"):
        arguments = arguments[1:]
    while arguments and arguments[0].startswith("-"):
        option = arguments[0]
        arguments = arguments[2:] if option in {"--config", "--color", "--manifest-path", "-C", "-Z"} else arguments[1:]
    return bool(arguments and arguments[0] in CARGO_BUILDS)


def building_command(command: str, *, merge_gate_only: bool = False) -> bool:
    if not CANDIDATE.search(command):
        return False
    command = scan_text("Bash", {"command": command}, {}).replace("\\\n", "")
    lexer = shlex.shlex(without_literal_heredocs(command), posix=True, punctuation_chars=";&|()\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    words: list[str] = []
    for word in lexer:
        if word and all(char in ";&|()\n" for char in word):
            if builds(words, merge_gate_only=merge_gate_only):
                return True
            words = []
        else:
            words.append(word)
    return builds(words, merge_gate_only=merge_gate_only)


def decide(raw: str) -> str:
    payload = cast(BuildHoldHookPayload, json.loads(raw))
    if payload.get("tool_name") != "Bash":
        return ""
    tool_input = payload.get("tool_input", {})
    command = tool_input.get("command") or tool_input.get("cmd") or ""
    if isinstance(command, list):
        command = shlex.join(command)
    if not building_command(command):
        return ""
    directory = Path(os.environ.get("BUILD_HOLD_DIR", str(Path.home() / ".local/state/build-hold")))
    try:
        if not any(directory.iterdir()):
            return ""
    except FileNotFoundError:
        return ""
    # Load the hold reader only for a build call with a possible hold.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "build_hold"))
    if TYPE_CHECKING:
        from ..build_hold.hold_applies import HoldStateReadError, hold_applies
    else:
        from hold_applies import HoldStateReadError, hold_applies

    if not os.environ.get("CLAUDE_CODE_SESSION_ID"):
        os.environ["CLAUDE_CODE_SESSION_ID"] = payload.get("session_id", "")
    try:
        reason = hold_applies()
    except HoldStateReadError as error:
        return f"natedev hold state cannot be read: {' '.join(str(error).split())}"
    if reason and building_command(command, merge_gate_only=True):
        return reason + " The merge gate waits for the hold to end. Run it after the release."
    return reason


def main() -> None:
    try:
        reason = decide(sys.stdin.read())
    except Exception as error:
        reason = f"natedev build hold decision cannot be read: {type(error).__name__}: {' '.join(str(error).split())}"
    if reason:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}))


if __name__ == "__main__":
    main()
