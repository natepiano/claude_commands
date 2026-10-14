#!/usr/bin/env python3
"""Refuse a checkpoint commit that does not say what the user sees change.

A Bash call that runs `git commit` with a checkpoint(...) subject must carry a line
`Change-Review: <what you see change> | <how to check it>` or `Change-Review: none — <why nothing
visible changes>`, in its -m text, its heredoc or its -F file. A checkpoint that stages only
Markdown needs none, and every other commit passes untouched. The merge gate reads the same lines
and refuses a unit checkpoint without one, so this hook is the early half of that gate.
A broken hook never blocks a call.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

if TYPE_CHECKING:
    from ..production import change_review
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "production"))
    import change_review

COMMIT = re.compile(r"\bgit\b(?P<options>[^\n;&|]*?)\bcommit\b")
DIRECTORY = re.compile(r"-C\s+(?P<path>\"[^\"]+\"|'[^']+'|\S+)")
MESSAGE_FILE = re.compile(r"(?:-F|--file)[=\s]+(?P<path>\"[^\"]+\"|'[^']+'|\S+)")
SUBJECT = re.compile(r"(?:^|(?:-[a-zA-Z]*m|--message)[=\s]*[\"']?)(?P<subject>checkpoint\([^)\s]*\):[^\n\"']*)", re.MULTILINE)
ALL = re.compile(r"\s(?:-[a-zA-Z]*a[a-zA-Z]*|--all)\b")
LINE = re.compile(r"Change-Review:[ \t]*(?P<value>[^\n]*)")


class HookPayload(TypedDict, total=False):
    session_id: str
    cwd: str
    tool_name: str
    tool_input: dict[str, object]


class HookDecision(TypedDict):
    hookEventName: str
    permissionDecision: str
    permissionDecisionReason: str


class HookResponse(TypedDict):
    hookSpecificOutput: HookDecision


def _path(text: str, cwd: Path) -> Path:
    return (cwd / Path(text.strip("\"'")).expanduser()).resolve()


def message_text(command: str, cwd: Path) -> str:
    """The command's own text with the contents of any -F file it names."""
    parts = [command]
    for found in MESSAGE_FILE.finditer(command):
        if found.group("path") != "-":
            try:
                parts.append(_path(found.group("path"), cwd).read_text(encoding="utf-8"))
            except (OSError, UnicodeError):
                continue
    return "\n".join(parts)


def staged(directory: Path, every: bool) -> tuple[str, ...]:
    """The files the commit takes: the index, or with -a every tracked change against HEAD."""
    result = subprocess.run(["git", "-C", str(directory), "diff", "--name-only", "HEAD" if every else "--cached"],
                            capture_output=True, text=True, check=False, timeout=10)
    return tuple(result.stdout.split()) if result.returncode == 0 else ()


def refusal(command: str, cwd: Path) -> str:
    commit = COMMIT.search(command)
    if commit is None:
        return ""
    text = message_text(command, cwd)
    subject = SUBJECT.search(text)
    if subject is None:
        return ""
    directory = DIRECTORY.search(commit.group("options"))
    every = ALL.search(command[commit.end():].split("\n", 1)[0]) is not None
    files = staged(_path(directory.group("path"), cwd) if directory else cwd, every)
    values = "\n".join(f"{change_review.TRAILER}: " + found.group("value").rstrip().rstrip("\"')").rstrip()
                       for found in LINE.finditer(text))
    return change_review.problem(subject.group("subject").strip(), values, files or ("unknown",))


def decide(raw: str) -> str:
    payload = cast(HookPayload, json.loads(raw))
    if payload.get("tool_name") != "Bash":
        return ""
    command = payload.get("tool_input", {}).get("command")
    if not isinstance(command, str):
        return ""
    reason = refusal(command, Path(payload.get("cwd") or Path.cwd()))
    return f"Checkpoint refused: {reason}." if reason else ""


def main() -> None:
    try:
        reason = decide(sys.stdin.read())
    except Exception as error:
        print(f"change review gate: {type(error).__name__}: {error}", file=sys.stderr)
        return
    if reason:
        response: HookResponse = {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}
        print(json.dumps(response))


if __name__ == "__main__":
    main()
