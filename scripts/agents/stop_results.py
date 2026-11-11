"""The outcomes known after a stop, resume, or detached move hand-off.

Kept independent of launch machinery so account callers can inspect results
without importing an account consumer back into their own module.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from scripts.whoami import note_file

if TYPE_CHECKING:
    from scripts.production.staff import StaffRow, ClaudeBackend, CodexBackend, BackendUnavailable


def shutdown_state_root() -> Path:
    """Return the machine-local folder for shutdown records."""
    return Path(os.environ.get("SHUTDOWN_STATE_DIR", str(Path.home() / ".local/state/shutdown")))


@dataclass(frozen=True)
class SessionWithoutStaffRow:
    """The session has no row in the staff record."""


SESSION_WITHOUT_STAFF_ROW = SessionWithoutStaffRow()


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
class StopUnproved(StopFailed):
    """The read cannot prove absence or identify one process to signal."""


class ClaudeStartState(StrEnum):
    STARTED = "started"
    ALREADY_RUNNING = "already running"


@dataclass(frozen=True)
class UnknownProcessId:
    """The resume result has no proved process id."""


@dataclass(frozen=True)
class ClaudeUp:
    session_id: str
    pane_id: str
    account: str
    elapsed_seconds: float = field(default=0.0, compare=False)
    trust_copied_from: str = field(default="", compare=False)
    start_state: ClaudeStartState = field(default=ClaudeStartState.STARTED, compare=False)
    pid: int | UnknownProcessId = field(default_factory=UnknownProcessId, compare=False)


@dataclass(frozen=True)
class MoveHandedOff:
    session_id: str


@dataclass(frozen=True)
class ResumeFailed:
    session_id: str
    reason: str


@dataclass(frozen=True)
class SessionNotFound:
    name: str


@dataclass(frozen=True)
class ResumePending:
    """The move has stopped its Claude; the saved launch can finish on rerun."""
    session_id: str
    account: str
    reason: str


ResumeResult = ClaudeUp | ResumeFailed | SessionNotFound
MoveResult = ResumeResult | MoveHandedOff | StopFailed | ResumePending


def claude_up_line(result: ClaudeUp) -> str:
    label = note_file.parse_account_label(result.account)
    agent = f"{label.tool.capitalize()} " if isinstance(label, note_file.NumberedAccountLabel) else ""
    if result.start_state is ClaudeStartState.ALREADY_RUNNING:
        return f"{result.session_id}: unchanged; {agent}runs on {result.account} in {result.pane_id}"
    return (f"{result.session_id}: {agent + 'is ' if agent else ''}up on {result.account} in {result.pane_id} "
            + f"({result.elapsed_seconds:.1f} s)"
            + (f"; copied folder trust from {result.trust_copied_from}" if result.trust_copied_from else ""))


StopResult = Stopped | NotRunning | StopFailed | SessionNotFound


@dataclass(frozen=True)
class SourceReleased:
    """The native source conversation is proved stopped."""


@dataclass(frozen=True)
class SourceUnresolved:
    reason: str


@dataclass(frozen=True)
class SourceReleaseUnknown:
    reason: str


SourceRelease = SourceReleased | SourceUnresolved | SourceReleaseUnknown
ReleaseSource = Callable[["StaffRow", "ClaudeBackend | CodexBackend | BackendUnavailable"], SourceRelease]
