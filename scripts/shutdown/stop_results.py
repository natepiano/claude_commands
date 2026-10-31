"""The outcomes known after a stop, resume, or detached move hand-off.

Kept independent of launch machinery so account callers can inspect results
without importing an account consumer back into their own module.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Stopped:
    session_id: str
    pid: int


@dataclass(frozen=True)
class NotRunning:
    session_id: str


@dataclass(frozen=True)
class StopFailed:
    session_id: str
    reason: str


@dataclass(frozen=True)
class ClaudeUp:
    session_id: str
    pane_id: str
    account: str
    elapsed_seconds: float = field(default=0.0, compare=False)
    trust_copied_from: str = field(default="", compare=False)


@dataclass(frozen=True)
class MoveHandedOff:
    session_id: str


@dataclass(frozen=True)
class DownHandedOff:
    session_id: str


@dataclass(frozen=True)
class ResumeFailed:
    session_id: str
    reason: str


@dataclass(frozen=True)
class SessionNotFound:
    name: str


MoveResult = ClaudeUp | MoveHandedOff | StopFailed | ResumeFailed | SessionNotFound
DownResult = Stopped | NotRunning | DownHandedOff | StopFailed | SessionNotFound


def down_result_line(result: DownResult) -> str:
    if isinstance(result, Stopped):
        return f"{result.session_id}: stopped"
    if isinstance(result, NotRunning):
        return f"{result.session_id}: already down"
    if isinstance(result, DownHandedOff):
        return f"{result.session_id}: stopping in detached batch"
    if isinstance(result, SessionNotFound):
        return f"{result.name}: failed: no local tab"
    return f"{result.session_id}: failed: {result.reason}"


def claude_up_line(result: ClaudeUp) -> str:
    return (f"{result.session_id}: Claude is up on {result.account} in {result.pane_id} "
            + f"({result.elapsed_seconds:.1f} s)"
            + (f"; copied folder trust from {result.trust_copied_from}" if result.trust_copied_from else ""))
