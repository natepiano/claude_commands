#!/usr/bin/env python3
"""Refuse a showrunner's hand paths for stopping or moving a Claude session; name /showrunner:move.

A session is a showrunner's when an update timer is addressed to its session id. In one, a Bash
call is refused when it sends `/exit` into a pane through tmux or herdr, runs `shutdown.py now`,
kills a Claude process, or resumes `claude` with a CLAUDE_CONFIG_DIR of its own. Every other call,
and every call in any other session, passes. A broken hook never blocks a call.
"""
from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from typing import TypedDict, cast

from scripts.production.showrunners import production_of

SKILL = ("Move or stop a session with /showrunner:move: it pauses the session first, proves the account it "
         "comes back on and tells its showrunner. ")
EXIT_REFUSAL = SKILL + "Sending /exit into another session's pane stops it in the middle of its turn."
NOW_REFUSAL = SKILL + "shutdown.py now stops a working session without asking it to pause."
KILL_REFUSAL = SKILL + "Killing a Claude process ends its turn and leaves no record to bring it back."
RESUME_REFUSAL = SKILL + "A hand-launched claude --resume with CLAUDE_CONFIG_DIR is never checked for its account."
SEGMENT_BREAK = re.compile(r"&&|\|\||[;|&\n]")
KILLERS = frozenset({"kill", "pkill", "killall"})
RESUME_FLAGS = frozenset({"--resume", "-r", "--continue", "-c"})
PROC = Path("/proc")


class HookPayload(TypedDict):
    session_id: str
    tool_name: str
    tool_input: dict[str, object]


class HookDecision(TypedDict):
    hookEventName: str
    permissionDecision: str
    permissionDecisionReason: str


class HookResponse(TypedDict):
    hookSpecificOutput: HookDecision


def _words(segment: str) -> list[str]:
    try:
        return shlex.split(segment, comments=True)
    except ValueError:
        return segment.split()


def _command_words(words: list[str]) -> list[str]:
    """The words from the program on, past leading `VAR=value`, `env`, `sudo` and `exec`."""
    index = 0
    while index < len(words) and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[index])
                                  or words[index] in {"env", "sudo", "exec", "command", "nohup"}):
        index += 1
    return words[index:]


def _is_claude(pid: int, proc: Path) -> bool:
    try:
        if (proc / str(pid) / "comm").read_text(encoding="utf-8").strip() == "claude":
            return True
        argv = (proc / str(pid) / "cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    return bool(argv) and Path(argv[0].decode(errors="replace")).name == "claude"


def _kills_claude(words: list[str], segment: str, proc: Path) -> bool:
    if not words or Path(words[0]).name not in KILLERS:
        return False
    if "claude" in segment:
        return True
    pids = (word.lstrip("-") for word in words[1:])
    return any(_is_claude(int(pid), proc) for pid in pids if pid.isdigit())


def refusal(command: str, proc: Path | None = None) -> str:
    """The reason a showrunner's Bash command is refused, or empty when it passes."""
    proc = proc or PROC
    setting_dir = "CLAUDE_CONFIG_DIR=" in command
    for segment in SEGMENT_BREAK.split(command):
        everything = _words(segment)
        words = _command_words(everything)
        if not words:
            continue
        program = Path(words[0]).name
        if program in {"tmux", "herdr"} and any(word.strip().startswith("/exit") for word in words[1:]):
            return EXIT_REFUSAL
        if any(Path(word).name == "shutdown.py" and words[index + 1:index + 2] == ["now"]
               for index, word in enumerate(words)):
            return NOW_REFUSAL
        if _kills_claude(words, segment, proc):
            return KILL_REFUSAL
        if program == "claude" and setting_dir and RESUME_FLAGS.intersection(words[1:]):
            return RESUME_REFUSAL
    return ""


def decide(raw: str, state_dir: Path | None = None, proc: Path | None = None) -> str:
    payload = cast(HookPayload, json.loads(raw))
    if payload.get("tool_name") != "Bash":
        return ""
    command = payload.get("tool_input", {}).get("command")
    if not isinstance(command, str):
        return ""
    reason = refusal(command, proc)
    if not reason or production_of(payload.get("session_id", ""), state_dir) is None:
        return ""
    return reason


def main() -> None:
    try:
        reason = decide(sys.stdin.read())
    except Exception as error:
        print(f"showrunner move guard: {type(error).__name__}: {error}", file=sys.stderr)
        return
    if reason:
        response: HookResponse = {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}
        print(json.dumps(response))


if __name__ == "__main__":
    main()
