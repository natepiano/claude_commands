"""Parse the unit-status command output."""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple


class SessionGone(NamedTuple):
    pass


class ClaudeNotRunning(NamedTuple):
    pass


class Running(NamedTuple):
    pass


class FormWaiting(NamedTuple):
    text: str


class Decision(NamedTuple):
    text: str


class StillWaiting(NamedTuple):
    text: str


class Block(NamedTuple):
    age: str
    text: str


class TicksFailing(NamedTuple):
    text: str


class Activity(NamedTuple):
    text: str


Flag = FormWaiting | Decision | StillWaiting | Block | TicksFailing
SessionState = SessionGone | ClaudeNotRunning | Running


class StatusBlock(NamedTuple):
    session: str
    state: SessionState
    flags: tuple[Flag, ...]
    activity: tuple[Activity, ...]


class StatusFailure(Exception):
    """The unit-status output is incomplete or invalid."""

    def __init__(self, step: str, detail: str) -> None:
        super().__init__(detail)
        self.step: str = step
        self.detail: str = detail


def status_blocks(path: Path, units: tuple[str, ...]) -> tuple[StatusBlock, ...]:
    names = set(units)
    blocks: list[StatusBlock] = []
    session = ""
    state: SessionState = Running()
    flags: list[Flag] = []
    activity: list[Activity] = []
    decision: list[str] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("== "):
            if session:
                if decision is not None:
                    raise StatusFailure("status", f"{session}: unclosed DECISION block")
                blocks.append(StatusBlock(session, state, tuple(flags), tuple(activity)))
            session = line[3:].strip()
            if session not in names:
                raise StatusFailure("status", f"unknown unit {session}")
            if any(block.session == session for block in blocks):
                raise StatusFailure("status", f"duplicate unit {session}")
            state, flags, activity, decision = Running(), [], [], None
            continue
        if not session:
            if not re.fullmatch(r"at .+ / \d\d:\d\d UTC", line):
                raise StatusFailure("status", f"before first unit: {line}")
            continue
        if decision is not None:
            if line == f"=== end {session} ===":
                flags.append(Decision("\n".join(decision)))
                decision = None
            else:
                decision.append(line)
            continue
        if line == f"=== DECISION for you from {session} ===":
            decision = []
        elif line == "SESSION GONE":
            state = SessionGone()
        elif line == "CLAUDE NOT RUNNING":
            state = ClaudeNotRunning()
        elif line.startswith("TICKS FAILING (") and line.endswith(")"):
            flags.append(TicksFailing(line[len("TICKS FAILING ("):-1]))
        elif line.startswith(f"FORM WAITING on you in {session}: "):
            flags.append(FormWaiting(line.partition(": ")[2]))
        elif line.startswith(f"STILL WAITING on you, {session}:"):
            flags.append(StillWaiting(line.partition(":")[2].strip()))
        elif (match := re.fullmatch(rf"BLOCK in {re.escape(session)}, open ([^:]+):\s*(.*)", line)):
            flags.append(Block(match.group(1), match.group(2)))
        else:
            activity.append(Activity(line))
    if session:
        if decision is not None:
            raise StatusFailure("status", f"{session}: unclosed DECISION block")
        blocks.append(StatusBlock(session, state, tuple(flags), tuple(activity)))
    missing = sorted(names - {block.session for block in blocks})
    if missing:
        raise StatusFailure("status", f"missing unit blocks: {', '.join(missing)}")
    return tuple(blocks)


