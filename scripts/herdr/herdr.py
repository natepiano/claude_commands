#!/usr/bin/env python3
"""Run named herdr sessions without choosing accounts or answering dialogs.

Process discovery resolves launch pane keys against the live panes of listening herdr servers
and members of those servers' dedicated systemd units, including handoff survivors.
In shared scopes, the server's preserved pane shell PIDs recover orphaned trees.
Live-checked with herdr 0.9.0 in throwaway session herdr-libcheck: panes set
HERDR_BIN_PATH, HERDR_ENV=1, HERDR_PANE_ID, HERDR_SESSION, HERDR_SOCKET_PATH,
HERDR_TAB_ID, and HERDR_WORKSPACE_ID. Listening socket ownership identifies the
session; ancestry and dedicated unit membership associate pane processes with it.
On Darwin, ps supplies user processes and their environments; lsof identifies
the bound session socket and process working directories. Servers detach directly.
All caller-supplied timeouts are integer milliseconds.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import base64
import secrets
import struct
import json
import hashlib
import ctypes
import os
import re
import signal
import socket
import shlex
import shutil
import subprocess
import math
import sys
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from threading import RLock
from functools import cache, cached_property
from importlib import import_module
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence, Set
from typing import BinaryIO, TYPE_CHECKING, Literal, NamedTuple, Never, NotRequired, TypedDict, Protocol, cast, final, override


from scripts.herdr import pane_launch
from scripts.agents import worker_lifecycle
from scripts.lib import conf, models_registry, operation_deadline
from scripts.lib.agent_scope import agents_slice, agent_scope_prefix
from scripts.lib import legacy_identity
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.script_envelope import script_prompt
from scripts.message import sessions as runtime_sessions
from scripts.production import staff

from scripts.lib.process_failure import failure_detail
from scripts.lib import nix_env as nix_env
from scripts.herdr.place import HerdrPlace as HerdrPlace
from scripts.herdr.pane_launch import RecordedLabels, StaffLabelsRead
from scripts.herdr.herdr_types import NATIVE_ID_MARKS, HerdrFailure, HerdrMissing

if TYPE_CHECKING:
    from scripts.production.session_labels import StaffLabels

# herdr refuses a longer `agent start --timeout`.
_AGENT_START_TIMEOUT_MAXIMUM_MS = 300_000
PROMPT_SETTLE_SECONDS = 10
_PLATFORM = sys.platform
NO_ENVIRONMENT_REMOVALS: frozenset[str] = frozenset()
SHARED_SESSION = "agents"


@dataclass(frozen=True)
class ReadStaffAtUse:
    """A standalone tab operation reads the current staff record."""


@dataclass(frozen=True)
class ScanAtUse:
    """The layout operation reads current pane evidence."""


@dataclass(frozen=True)
class RestoreStaff:
    """The restore operation shares its staff record and session labels."""

    record: staff.StaffSnapshot
    labels: StaffLabels | StaffLabelsRead


_READ_STAFF_AT_USE = ReadStaffAtUse()
_SCAN_AT_USE = ScanAtUse()
_RESTORE_STAFF: ContextVar[RestoreStaff | ReadStaffAtUse] = ContextVar(
    "restore_staff", default=_READ_STAFF_AT_USE)


@contextmanager
def restore_staff(record: staff.StaffSnapshot, *, labels: StaffLabels | StaffLabelsRead = StaffLabelsRead.UNAVAILABLE) -> Generator[None]:
    """Share the restore snapshot with every tab operation in this context."""
    token = _RESTORE_STAFF.set(RestoreStaff(record, labels))
    try:
        yield
    finally:
        _RESTORE_STAFF.reset(token)


def _tab_staff() -> staff.StaffSnapshot:
    record = _RESTORE_STAFF.get()
    try:
        return staff.initial_snapshot() if isinstance(record, ReadStaffAtUse) else record.record
    except (OSError, ValueError) as error:
        print(f"herdr: staff layout data is unknown: {error}", file=sys.stderr)
        return staff.StaffSnapshot(())


# The transient server unit gets CPU first and keeps its memory out of swap under the pane load.
_SERVER_PROPERTIES = ("-p", "CPUWeight=1000", "-p", "MemoryLow=1G", "-p", "MemorySwapMax=0")
# The pane shell wrapper. herdr runs $SHELL for a pane; this puts the real shell in its own scope.
# The scope runs in place of the wrapper, so the pane's shell pid does not change (the scope is run-p<pid>).
# SHELL is the real shell again, so a tool that reads SHELL does not start a scope on each command.
_PANE_SHELL_SCRIPT = """#!/bin/sh
real=${HERDR_PANE_SHELL:-/bin/sh}
unset HERDR_PANE_SHELL
SHELL=$real; export SHELL
exec @SCOPE_PREFIX@ "$real" "$@"
"""


def _platform() -> str:
    """The platform herdr reads processes as: HERDR_PLATFORM names the one a test's fake process tree was built for."""
    return os.environ.get("HERDR_PLATFORM", _PLATFORM)


class NativeRegistryUnavailable(ValueError):
    """The process has no native registry identity."""


class _DarwinProcessGone(HerdrFailure):
    """An empty failed ps read was confirmed by the pid no longer existing."""


class SessionTaken(HerdrFailure):
    """A new session cannot own a server or systemd unit already in use."""

    def __init__(self, name: str) -> None:
        super().__init__(f"herdr session {name} is already taken", "session_taken")
        self.name: str = name


class AgentBlocked(HerdrFailure):
    """An agent requires user input; nothing is sent in response."""


class AgentExited(HerdrFailure):
    """The submitted agent returned to its shell before startup completed."""

    def __init__(self, session: str, pane: str) -> None:
        super().__init__(f"herdr {session}:{pane}: agent process exited before startup completed", "agent_exited")


@dataclass(frozen=True)
class ServerCreated:
    """This attempt started the server and may clean up its failed agent."""


@dataclass(frozen=True)
class ServerAdopted:
    """The server was already running and belongs to an earlier launch."""


@dataclass(frozen=True)
class DesktopSession:
    """The caller belongs to the Mac desktop's Aqua security session."""


@dataclass(frozen=True)
class NonDesktopSession:
    """The caller cannot start a server with desktop keychain access."""


@dataclass(frozen=True)
class SessionKindUnreadable:
    reason: str


@dataclass(frozen=True)
class NotMacSession:
    """Security-session routing is unnecessary on this platform."""


SecuritySession = DesktopSession | NonDesktopSession | SessionKindUnreadable | NotMacSession


@dataclass(frozen=True)
class SessionKindNotRead:
    """A command boundary still needs to determine its security session."""


_SESSION_KIND_NOT_READ = SessionKindNotRead()
MAC_DESKTOP_REQUIRED = "the Mac desktop is locked or signed out: unlock it and restart"
MAC_DESKTOP_TERMINAL_REQUIRED = "herdr must start from the Mac desktop: run this from a Mac terminal"


class MacDesktopUnavailable(HerdrFailure):
    def __init__(self) -> None:
        super().__init__(MAC_DESKTOP_REQUIRED, "mac_desktop_unavailable")


@dataclass(frozen=True)
class WindowServerCreated:
    """A desktop window started this server; the user owns its cleanup."""


def security_session() -> SecuritySession:
    """Read the command's security context once; a failed query grants no access."""
    if _platform() != "darwin":
        return NotMacSession()
    try:
        result = run_program([os.environ.get("LAUNCHCTL", "launchctl"), "managername"])
    except (OSError, subprocess.SubprocessError, HerdrFailure) as error:
        return SessionKindUnreadable(str(error))
    if result.returncode:
        return SessionKindUnreadable(result.stderr.strip() or "launchctl managername failed")
    return DesktopSession() if result.stdout.strip() == "Aqua" else NonDesktopSession()


class AgentNotReady(HerdrFailure):
    """Startup stopped before interactive readiness, with its visible screen."""

    def __init__(self, screen: str) -> None:
        super().__init__(screen, "agent_not_ready")
        self.screen: str = screen


@dataclass(frozen=True)
class SessionTranscript:
    config_dir: Path
    session_id: str


@dataclass(frozen=True)
class UnspecifiedPromptTranscript:
    """The caller has not identified the conversation that must receive the prompt."""


@dataclass(frozen=True)
class PromptScreenNotRead:
    """No visible screen has been read for this submission."""


@dataclass(frozen=True)
class PromptInputReady:
    """The destination session can accept its opening prompt."""

    screen: str | PromptScreenNotRead = PromptScreenNotRead()
    idle: bool = False


@dataclass(frozen=True)
class PromptInputNotReady:
    reason: str
    screen: str | PromptScreenNotRead = PromptScreenNotRead()


@dataclass(frozen=True)
class PromptInputTimedOut:
    """The destination did not become ready before the launch deadline."""

    screen: str | PromptScreenNotRead = PromptScreenNotRead()


@dataclass(frozen=True)
class PromptRecipientProcess:
    pid: int
    started_at: str


@dataclass(frozen=True)
class PromptRecipientUnavailable:
    reason: str


@dataclass(frozen=True)
class PromptSubmitted:
    at: datetime
    process: PromptRecipientProcess | PromptRecipientUnavailable = PromptRecipientUnavailable("prompt recipient was not identified")


@dataclass(frozen=True)
class PromptNotSubmitted:
    reason: str
    screen: str | PromptScreenNotRead = PromptScreenNotRead()
    # True only when the launcher never typed the prompt: the session then holds no work of it.
    never_typed: bool = False


@dataclass(frozen=True)
class AgentNotStarted(PromptNotSubmitted):
    """The start command failed, and no agent runs in the pane."""


@dataclass(frozen=True)
class ClaudeTrustNotWritten(AgentNotStarted):
    """The launch cannot start because the target account trust write failed."""


@dataclass(frozen=True)
class PromptSubmissionNotRequired:
    """The launch intentionally leaves the session without an opening prompt."""


PromptSubmitOutcome = PromptSubmitted | PromptNotSubmitted


def _no_staff_publication(transcript: SessionTranscript) -> None:
    del transcript


@dataclass(frozen=True)
class OpeningPrompt:
    text: str
    transcript: SessionTranscript | UnspecifiedPromptTranscript = UnspecifiedPromptTranscript()
    before_prompt: Callable[[SessionTranscript], object] = _no_staff_publication
    cursor: TranscriptCursor | UnspecifiedPromptTranscript = UnspecifiedPromptTranscript()


@dataclass(frozen=True)
class NoOpeningPrompt:
    """Leave the ready agent waiting for the user to type."""


@dataclass(frozen=True)
class TranscriptReadable:
    """Every matching transcript file was read successfully."""


@dataclass(frozen=True)
class TranscriptUnreadable:
    """A matching transcript file could not be read; absence of a turn is unproven."""


def complete_transcript_offset(stream: BinaryIO) -> int:
    """Find the byte after the last complete transcript record."""
    position = stream.seek(0, os.SEEK_END)
    while position:
        start = max(0, position - 4096)
        _ = stream.seek(start)
        chunk = stream.read(position - start)
        newline = chunk.rfind(b"\n")
        if newline >= 0:
            return start + newline + 1
        position = start
    return 0


@dataclass
class TranscriptCursor:
    """The complete new turns seen by one submission, with per-file byte positions."""

    transcript: SessionTranscript
    since: datetime
    offsets: dict[Path, int] = field(default_factory=dict)
    sizes: dict[Path, int] = field(default_factory=dict)
    current_paths: tuple[Path, ...] = field(default=(), init=False)
    turns: dict[Path, list[tuple[datetime, dict[str, object]]]] = field(default_factory=dict)
    readability: TranscriptReadable | TranscriptUnreadable = field(default_factory=TranscriptReadable)

    def skip_existing_turns(self) -> None:
        """Skip complete rows before launch without parsing historical JSON."""
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.transcript.session_id):
            return
        try:
            paths = tuple((self.transcript.config_dir / "projects").glob(f"*/{self.transcript.session_id}.jsonl"))
            for path in paths:
                with path.open("rb") as stream:
                    size = stream.seek(0, os.SEEK_END)
                    self.offsets[path] = complete_transcript_offset(stream)
                    self.sizes[path] = size
        except OSError:
            self.readability = TranscriptUnreadable()

    def read_turns(self) -> list[tuple[datetime, dict[str, object]]]:
        self.readability = TranscriptReadable()
        self.current_paths = ()
        # A session id is a file component, never a glob supplied by a caller.
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.transcript.session_id):
            return []
        try:
            paths = tuple((self.transcript.config_dir / "projects").glob(f"*/{self.transcript.session_id}.jsonl"))
        except OSError:
            self.readability = TranscriptUnreadable()
            paths = ()
        self.current_paths = paths
        for path in paths:
            try:
                with path.open("rb") as stream:
                    offset = self.offsets.get(path, 0)
                    size = os.fstat(stream.fileno()).st_size
                    if size < self.sizes.get(path, 0):
                        offset = 0
                        self.turns[path] = []
                    self.sizes[path] = size
                    self.offsets[path] = offset
                    _ = stream.seek(offset)
                    while line := stream.readline():
                        # Leave partial UTF-8 and partial JSON unread until the writer
                        # completes this row. Offsets always fall after a newline.
                        if not line.endswith(b"\n"):
                            break
                        self.offsets[path] = stream.tell()
                        try:
                            value = cast(object, json.loads(line))
                            if not isinstance(value, dict):
                                continue
                            row = cast(dict[str, object], value)
                            stamp = row.get("timestamp")
                            if not isinstance(stamp, str):
                                continue
                            at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                            if at.tzinfo is not None and at > self.since:
                                self.turns.setdefault(path, []).append((at, row))
                        except (ValueError, TypeError, UnicodeError):
                            continue
            except OSError:
                self.readability = TranscriptUnreadable()
                continue
        return [turn for turns in self.turns.values() for turn in turns]

    def submitted_prompt(self, text: str) -> PromptSubmitOutcome:
        for at, row in self.read_turns():
            if row.get("type") == "user" and not row.get("isMeta", False) and _prompt_matches(text, _turn_text(row)):
                return PromptSubmitted(at)
        return PromptNotSubmitted("no matching non-meta user turn after launch")

    def command_rejections(self, text: str) -> int:
        """How many times Claude answered this prompt's slash command with `Unknown command` since launch."""
        command = _slash_command(text)
        if not command:
            return 0
        rejected = re.compile(rf"Unknown command: {re.escape(command)}(?![\w:-])")
        return sum(1 for _, row in self.read_turns()
                   if row.get("type") == "system" and rejected.match(_turn_text(row)))


def _slash_command(text: str) -> str:
    """The `/name` a prompt starts with, or "" for a plain prompt."""
    return text.split(maxsplit=1)[0] if text.startswith("/") and len(text) > 1 else ""


def _turn_text(row: dict[str, object]) -> str:
    message = row.get("message", row)
    if not isinstance(message, dict):
        return ""
    content: object = cast(dict[str, object], message).get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for value in cast(list[object], content):
            if isinstance(value, dict):
                block = cast(dict[str, object], value)
                text = block.get("text")
                if block.get("type") == "text" and isinstance(text, str):
                    texts.append(text)
        return "\n".join(texts)
    return ""


def _prompt_matches(text: str, recorded_text: str) -> bool:
    if text in recorded_text:
        return True
    if text.startswith("/"):
        command, *arguments = text.split(maxsplit=1)
        return (f"<command-name>{command}</command-name>" in recorded_text
                and (not arguments or f"<command-args>{arguments[0].strip()}</command-args>" in recorded_text))
    return False


def submitted_prompt(transcript: SessionTranscript, text: str, launched_at: datetime) -> PromptSubmitOutcome:
    return TranscriptCursor(transcript, launched_at).submitted_prompt(text)


@dataclass(frozen=True)
class ComposerInput:
    text: str

    def contains(self, text: str) -> bool:
        # Physical wraps can split words, but spaces in the prompt must still
        # match whitespace. This is the same match used for Enter-only retries.
        pattern = r"\s+".join(r"\n?".join(re.escape(char) for char in word) for word in text.split())
        return bool(pattern) and re.search(pattern, self.text) is not None


@dataclass(frozen=True)
class NoComposer:
    """The visible screen has no current input box."""


_SGR = re.compile(r"\x1b\[([0-9;:]*)m")
_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _prompt_screen(screen: str) -> str:
    return _ESCAPE.sub("", screen)


def _faint_after(parameters: str, faint: bool) -> bool:
    """The faint state after one SGR sequence; colour arguments (38/48/58 ;5;n or ;2;r;g;b) are skipped."""
    values = parameters.split(";")
    index = 0
    while index < len(values):
        value = values[index]
        if value in {"", "0", "22"}:
            faint = False
        elif value == "2":
            faint = True
        elif value in {"38", "48", "58"} and index + 1 < len(values):
            index += {"5": 2, "2": 4}.get(values[index + 1], 0)
        index += 1
    return faint


def _typed_text(screen: str) -> str:
    """The screen without escapes and without faint text, which is Claude's placeholder hint, never input."""
    kept: list[str] = []
    faint, position = False, 0
    for match in _SGR.finditer(screen):
        segment = _ESCAPE.sub("", screen[position:match.start()])
        kept.append("".join(char for char in segment if char in "\r\n") if faint else segment)
        faint = _faint_after(match.group(1), faint)
        position = match.end()
    segment = _ESCAPE.sub("", screen[position:])
    kept.append("".join(char for char in segment if char in "\r\n") if faint else segment)
    return "".join(kept)


def _composer_input(screen: str) -> ComposerInput | NoComposer:
    """Parse the final live composer, excluding old input and its footer.

    An ANSI screen (`--format ansi`) lets faint placeholder text such as `Try "fix lint errors"` read as empty.
    """
    lines = _prompt_screen(screen).splitlines()
    typed = _typed_text(screen).splitlines()
    closing_borders = [i for i, line in enumerate(lines) if re.fullmatch(r"\s*[─━-]{3,}\s*", line)]
    if not closing_borders:
        return NoComposer()
    closing = closing_borders[-1]
    # A rule-like input row is not a top border. The actual marker is at column 0.
    top_borders = [i for i, line in enumerate(lines[:closing])
                   if re.fullmatch(r"\s*[─━-]{3,}(?:\s+.+\s+[─━-]+)?\s*", line)
                   and re.match(r"^[❯>]", lines[i + 1])]
    if not top_borders:
        return NoComposer()
    # Column-zero output below the last box makes that box scrollback.
    if any(line.strip() and not line[0].isspace() for line in lines[closing + 1:]):
        return NoComposer()
    region = typed[top_borders[-1] + 1:closing]
    region[0] = re.sub(r"^[❯>]\s*", "", region[0])
    return ComposerInput("\n".join(line.strip() for line in region))


def prompt_recipient_process(session_id: str) -> PromptRecipientProcess | PromptRecipientUnavailable:
    """Identify the process receiving input, including before its socket opens."""
    from scripts.message import sessions

    try:
        identities = {PromptRecipientProcess(row["pid"], row.get("procStart", ""))
                      for row in sessions.running_sessions() if row["sessionId"] == session_id}
    except (OSError, ValueError) as error:
        return PromptRecipientUnavailable(str(error))
    if len(identities) != 1:
        return PromptRecipientUnavailable("prompt recipient is missing or ambiguous")
    process = identities.pop()
    if not process.started_at:
        return PromptRecipientUnavailable("prompt recipient start is unrecorded")
    return process


def encode_prompt_outcome(outcome: PromptSubmitOutcome) -> str:
    if isinstance(outcome, PromptNotSubmitted):
        return json.dumps({"kind": "not submitted", "reason": outcome.reason,
                           **({"screen": outcome.screen} if isinstance(outcome.screen, str) else {})})
    process = outcome.process
    return json.dumps({"kind": "submitted", "at": outcome.at.isoformat(),
                       "process": {"pid": process.pid, "started_at": process.started_at}
                       if isinstance(process, PromptRecipientProcess) else {}})


def decode_prompt_outcome(text: str) -> PromptSubmitOutcome:
    try:
        value = cast(object, json.loads(text))
        if isinstance(value, dict):
            row = cast(dict[str, object], value)
            if row.get("kind") == "submitted" and isinstance(row.get("at"), str):
                at = datetime.fromisoformat(cast(str, row["at"]))
                if at.tzinfo is not None:
                    process = row.get("process")
                    if isinstance(process, dict):
                        fields = cast(dict[str, object], process)
                        pid, started_at = fields.get("pid"), fields.get("started_at")
                        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 and isinstance(started_at, str) and started_at:
                            return PromptSubmitted(at, PromptRecipientProcess(pid, started_at))
                    return PromptSubmitted(at)
            if row.get("kind") == "not submitted" and isinstance(row.get("reason"), str):
                screen = row.get("screen")
                return PromptNotSubmitted(cast(str, row["reason"]),
                                          screen if isinstance(screen, str) else PromptScreenNotRead())
    except (ValueError, TypeError):
        pass
    return PromptNotSubmitted("launcher supplied no valid submit proof")


@dataclass(frozen=True)
class ClientsAttached:
    count: int


@dataclass(frozen=True)
class NoClientAttached:
    """No interactive herdr client is attached to this session."""


@dataclass(frozen=True)
class ClientsUnreadable:
    detail: str


class AgentStatus(StrEnum):
    WORKING = "working"
    IDLE = "idle"
    DONE = "done"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"
    # Older servers can report these lifecycle states.
    STARTING = "starting"
    RUNNING = "running"
    EXITED = "exited"
    STOPPED = "stopped"
    DEAD = "dead"
    NONE = "none"

    @property
    def idle(self) -> bool:
        return self in {AgentStatus.IDLE, AgentStatus.DONE}

    @classmethod
    def decode(cls, value: object) -> AgentStatus:
        text = json_text(value, "herdr agent status")
        try:
            return cls(text)
        except ValueError:
            return cls.UNKNOWN


@dataclass(frozen=True)
class ObservedAgentState:
    status: AgentStatus


@dataclass(frozen=True)
class WaitTimedOut:
    """The CLI or its subprocess exhausted the wait deadline."""


@dataclass(frozen=True)
class NotInHerdr:
    """The process has no identifiable herdr pane and listening server."""


@dataclass(frozen=True)
class UnreadableCwd:
    detail: str


@dataclass(frozen=True)
class HerdrProcess:
    pid: int
    place: HerdrPlace
    marks: dict[str, str]
    command: str
    working_directory: str | UnreadableCwd

    @property
    def cwd(self) -> str:
        """Display a known directory; placement uses the independent place."""
        return self.working_directory if isinstance(self.working_directory, str) else ""


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
class NoPane:
    label: str


@dataclass(frozen=True)
class PaneNotRead:
    """The caller has not read the pane row."""


PANE_NOT_READ = PaneNotRead()


@dataclass(frozen=True)
class SeveralPanes:
    label: str
    count: int


@dataclass(frozen=True)
class UnknownPane:
    pane: str
    tab: str
    reason: str


@dataclass(frozen=True)
class RowPaneScan:
    found: dict[staff.StaffId, list[dict[str, object]]]
    unknown: list[UnknownPane]


@dataclass(frozen=True)
class TabSession:
    """A labelled pane and its last known conversation, including after the agent exits."""

    pane_id: str
    label: str
    workspace: str
    session_id: str | None


@dataclass(frozen=True)
class PaneAtShell:
    """The pane's foreground process group is its original shell."""


@dataclass(frozen=True)
class PaneRunning:
    commands: tuple[str, ...]


@dataclass(frozen=True)
class ProcessGenerationUnavailable:
    """The process start time cannot prove its generation."""

    reason: str


@dataclass(frozen=True)
class AgentProcessIdentity:
    pid: int
    # Linux uses clock ticks. Darwin uses UTC epoch microseconds from libproc.
    start_ticks: int | ProcessGenerationUnavailable


@dataclass(frozen=True)
class AgentProcessPresent:
    """An identified agent process is still alive, including after exec."""

    identities: tuple[AgentProcessIdentity, ...] = ()


@dataclass(frozen=True)
class PaneProcessesUnreadable:
    """The pane's current processes cannot establish startup or exit."""


@dataclass(frozen=True)
class PaneShell:
    pid: int


@dataclass(frozen=True)
class PaneShellUnknown:
    """No process observation has located the pane's shell yet."""


@dataclass(frozen=True)
class PaneStartupObservation:
    shell: PaneShell
    activity: AgentProcessPresent | PaneAtShell | PaneRunning


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
    terminal_id: NotRequired[str]
    tab_id: NotRequired[str]
    focused: NotRequired[bool]
    label: NotRequired[str | None]
    workspace_id: str
    session_id: str | None


class PaneListResult(TypedDict):
    panes: list[PaneIdentity]


class PaneForegroundProcess(TypedDict):
    pid: int
    name: str
    argv: list[str]


class PaneProcessInfo(TypedDict):
    shell_pid: NotRequired[int]
    foreground_process_group_id: NotRequired[int]
    foreground_processes: NotRequired[list[PaneForegroundProcess]]


class PaneProcessInfoResult(TypedDict):
    process_info: PaneProcessInfo


@dataclass(frozen=True)
class ReportedAgentSession:
    """The agent reports its current native session id."""

    session_id: str


@dataclass(frozen=True)
class NoReportedAgentSession:
    """The agent has no reported native session id."""


class AgentOccupant(TypedDict):
    pane_id: str
    agent: NotRequired[str | None]
    agent_status: AgentStatus
    agent_session: ReportedAgentSession | NoReportedAgentSession


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


class _StartupShellEvidence(Enum):
    UNTOUCHED = "untouched startup shell"
    PRESERVE = "activity or incomplete startup evidence"


@dataclass(frozen=True)
class _WorkspaceStartupState:
    workspace_id: str
    evidence: _StartupShellEvidence


@dataclass(frozen=True)
class _PaneStartupState:
    pane_id: str
    evidence: _StartupShellEvidence


class AgentLifecycle(TypedDict):
    agent_status: AgentStatus


class AgentWaitResult(TypedDict):
    agent: AgentLifecycle


class WorkerReadiness(TypedDict):
    agent_status: AgentStatus
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


def child_environment(environment: dict[str, str]) -> dict[str, str]:
    """Remove launch proofs, retired identity hints, and NO_COLOR from child processes."""
    return {key: value for key, value in legacy_identity.without_legacy_identity(staff.child_environment(environment)).items()
            if key not in (nix_env.HEAL_SESSION_ENV, nix_env.HEAL_DIRECTORY_ENV, "NO_COLOR")}


def _pane_environment(environment: dict[str, str]) -> dict[str, str]:
    """The pane's terminal owns TERM and COLORTERM."""
    return {**{key: value for key, value in child_environment(environment).items()
               if key not in ("TERM", "COLORTERM")}, "HERDR_AGENT_PANE": "1"}


def unmarked_environment() -> dict[str, str]:
    """Remove legacy labels and launch proofs from inherited child environments."""
    return child_environment(dict(os.environ))


PROGRAM_TIMEOUT_SECONDS = 10.0


def run_program(argv: list[str], timeout: float = PROGRAM_TIMEOUT_SECONDS, *,
                environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=operation_deadline.remaining(timeout),
                              env=unmarked_environment() if environment is None else child_environment(environment))
    except OSError as error:
        raise HerdrFailure(f"{argv[0]}: {error}") from error


def require_success(result: subprocess.CompletedProcess[str], context: str) -> None:
    if result.returncode == 0:
        return
    code = ""
    detail = failure_detail(result)
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


# A tab or workspace gone from the shared session keeps its saved place this long, so a restart
# puts it back where it was. A production retired for longer than this loses its place.
LAYOUT_KEEP_SECONDS = 14 * 24 * 3600
# A fresh server gets back each workspace seen this long before the last save: a restart cut short
# by a second reboot still has the places of the sessions it did not reach.
LAYOUT_RESTORE_SECONDS = 24 * 3600
# The title of the shell tab a restart opens to hold a saved workspace's place until its first
# session's tab arrives. herdr 0.9.5 cannot move a workspace, so creation order is the only order.
LAYOUT_PLACEHOLDER = "restoring"
_NO_LAYOUT_EXCLUSIONS: frozenset[str] = frozenset()


class SavedName(TypedDict):
    name: str
    seen: float


class SavedMemberTab(TypedDict):
    staff_id: str
    seen: float
    sort_after_rename: NotRequired[bool]


@dataclass(frozen=True)
class UnownedTab:
    """The tab has a display title and no native staff owner."""

    title: str


class SavedSpace(TypedDict):
    name: str
    seen: float
    tabs: list[SavedMemberTab]


class SavedLayout(TypedDict):
    at: float
    workspaces: list[SavedSpace]


class WorkspacesKeptForSplitTabs(NamedTuple):
    labels: list[str]


class _LiveSpace(NamedTuple):
    workspace_id: str
    label: str
    tabs: list[str]


def _tab_members(record: staff.StaffSnapshot, scan: RowPaneScan) -> dict[str, staff.StaffRow]:
    owners: dict[str, staff.StaffRow] = {}
    for row_id, matches in scan.found.items():
        member = record.rows_by_id[row_id]
        for pane in matches:
            tab = json_text(pane.get("tab_id"), "herdr pane tab id")
            if tab not in owners or owners[tab].kind == "worker":
                owners[tab] = member
    return owners


def layout_path(session: str) -> Path:
    """The shared session's saved workspace and tab order, under the state directory."""
    state = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    from urllib.parse import quote

    return Path(state) / "herdr" / f"layout-{quote(session, safe='')}.json"


def saved_layout(session: str) -> SavedLayout:
    """The last saved layout; an absent or unreadable file is an empty one."""
    try:
        data = cast(object, json.loads(layout_path(session).read_text(encoding="utf-8")))
        root = json_object(data, "herdr layout")
        at = root.get("at")
        if isinstance(at, bool) or not isinstance(at, (int, float)):
            raise HerdrFailure("herdr layout at: expected a number")
        spaces: list[SavedSpace] = []
        for item in json_items(root.get("workspaces"), "herdr layout workspaces"):
            space = json_object(item, "herdr layout workspace")
            spaces.append(SavedSpace(name=json_text(space.get("name"), "herdr layout workspace name"),
                                     seen=_seen(space.get("seen")),
                                     tabs=[_saved_member_tab(tab) for tab in json_items(space.get("tabs"), "herdr layout tabs")]))
        return SavedLayout(at=float(at), workspaces=spaces)
    except (OSError, ValueError, HerdrFailure):
        return SavedLayout(at=0.0, workspaces=[])


def _seen(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HerdrFailure("herdr layout seen: expected a number")
    return float(value)


def _saved_member_tab(value: object) -> SavedMemberTab:
    item = json_object(value, "herdr layout tab")
    result = SavedMemberTab(staff_id=json_text(item.get("staff_id"), "herdr layout staff row"), seen=_seen(item.get("seen")))
    if item.get("sort_after_rename") is True:
        result["sort_after_rename"] = True
    return result


def merged_order(saved: list[SavedName], live: list[str], now: float, *, expire: bool = True) -> list[SavedName]:
    """Live names in live order, seen now; a saved name not live stays after the saved name before it.

    With expire, a saved name unseen for longer than LAYOUT_KEEP_SECONDS is dropped.
    """
    known = {entry["name"]: entry for entry in saved}
    result: list[SavedName] = []
    for name in live:
        entry = known[name].copy() if name in known else SavedName(name=name, seen=now)
        entry["seen"] = now
        result.append(entry)
    placed = set(live)
    for index, entry in enumerate(saved):
        if entry["name"] in placed or (expire and now - entry["seen"] > LAYOUT_KEEP_SECONDS):
            continue
        before = next((earlier["name"] for earlier in reversed(saved[:index]) if earlier["name"] in placed), None)
        at = 0 if before is None else next(place for place, item in enumerate(result) if item["name"] == before) + 1
        result.insert(at, entry.copy())
        placed.add(entry["name"])
    return result


def _write_layout(session: str, layout: SavedLayout) -> None:
    """The one writer of a session's saved layout: a staged file renamed over the old one."""
    path = layout_path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    _ = staged.write_text(json.dumps(layout, indent=1) + "\n", encoding="utf-8")
    _ = staged.replace(path)


def _bind_row_tab(saved: SavedLayout, workspace: str, row_id: str,
                  target: SavedMemberTab | None = None, index: int = 0, *,
                  record: staff.StaffSnapshot | ReadStaffAtUse = _READ_STAFF_AT_USE) -> bool:
    membership = _tab_staff() if isinstance(record, ReadStaffAtUse) else record
    member = membership.by_member_reference(row_id)
    if member is not None:
        workspace = row_workspace(member, membership, workspace)
    space = next((space for space in saved["workspaces"] if space["name"] == workspace),
                 SavedSpace(name=workspace, seen=time.time(), tabs=[]))
    bindings = [(previous, position, tab) for previous in saved["workspaces"]
                for position, tab in enumerate(previous["tabs"])
                if tab["staff_id"] == row_id]
    if target is None:
        if len(bindings) == 1 and bindings[0][0] is space:
            return False
        target = bindings[0][2].copy() if bindings else SavedMemberTab(staff_id=row_id, seen=time.time())
        entries = [*space["tabs"], target]
        keys = [entry["staff_id"] for entry in entries]
        units = {row.id for row in staff.active(membership, "unit")}
        owner = membership.parent_of(member) if member is not None else None
        if owner is not None and owner.kind == "unit":
            owner = membership.parent_of(owner)
        first = owner.id if owner is not None else ""
        index = tab_order(first, keys, space["tabs"], units, record=membership).index(len(keys) - 1)
    index = next((position for previous, position, _ in bindings if previous is space), index)
    for previous, _, tab in bindings:
        previous["tabs"].remove(tab)
    target["staff_id"] = row_id
    space["tabs"].insert(index, target)
    if space not in saved["workspaces"]:
        saved["workspaces"].append(space)
    return True


def _member_layout(saved: SavedLayout, record: staff.StaffSnapshot) -> SavedLayout:
    """Group saved member positions through their current staff parents."""
    spaces: dict[str, SavedSpace] = {}
    for space in saved["workspaces"]:
        for tab in space["tabs"]:
            member = record.by_id(staff.StaffId(tab["staff_id"]))
            name = row_workspace(member, record, space["name"]) if member is not None else space["name"]
            group = spaces.setdefault(name, SavedSpace(name=name, seen=space["seen"], tabs=[]))
            group["seen"] = max(group["seen"], space["seen"])
            group["tabs"].append(tab.copy())
    return SavedLayout(at=saved["at"], workspaces=list(spaces.values()))


def _merged_member_tabs(saved: list[SavedMemberTab], live: list[str], now: float, *, expire: bool) -> list[SavedMemberTab]:
    known = {tab["staff_id"]: tab for tab in saved}
    ordered = merged_order([SavedName(name=tab["staff_id"], seen=tab["seen"]) for tab in saved], live, now, expire=expire)
    tabs: list[SavedMemberTab] = []
    for entry in ordered:
        tab = known[entry["name"]].copy() if entry["name"] in known else SavedMemberTab(staff_id=entry["name"], seen=entry["seen"])
        tab["seen"] = entry["seen"]
        tabs.append(tab)
    return tabs


def merged_layout(saved: SavedLayout, live: list[_LiveSpace], now: float, *, expire: bool = True,
                  record: staff.StaffSnapshot | ReadStaffAtUse = _READ_STAFF_AT_USE) -> SavedLayout:
    """Merge the live layout into the saved layout and preserve absent entries until they expire."""
    if expire:
        saved = _member_layout(saved, _tab_staff() if isinstance(record, ReadStaffAtUse) else record)
    tabs = {space["name"]: space["tabs"] for space in saved["workspaces"]}
    live_tabs = {space.label: space.tabs for space in live}
    order = merged_order([SavedName(name=space["name"], seen=space["seen"]) for space in saved["workspaces"]],
                         [space.label for space in live], now, expire=expire)
    return SavedLayout(at=now, workspaces=[
        SavedSpace(name=entry["name"], seen=entry["seen"],
                   tabs=_merged_member_tabs(tabs.get(entry["name"], []), live_tabs.get(entry["name"], []), now, expire=expire))
        for entry in order])


def tab_order(first_member: str, tab_references: list[str], saved: list[SavedMemberTab], unit_ids: Set[str],
              *, record: staff.StaffSnapshot | ReadStaffAtUse = _READ_STAFF_AT_USE) -> list[int]:
    """Order fixed member references and native tab references without display text.

    Saved references keep their positions. Current staff names set the alphabetical order.
    The selected showrunner stays first. Unowned tabs retain their native references.
    """
    membership = _tab_staff() if isinstance(record, ReadStaffAtUse) else record
    first_reference = first_member
    if tab_references and not saved and first_member not in tab_references:
        first_reference = tab_references[0]

    def rule(reference: str) -> tuple[bool, bool, str]:
        member = membership.by_member_reference(reference)
        return (reference != first_reference, reference not in unit_ids, member.name if member is not None else reference)

    places = {entry["staff_id"]: place for place, entry in enumerate(saved)}
    order = sorted((index for index, reference in enumerate(tab_references) if reference in places), key=lambda index: places[tab_references[index]])
    for index in sorted((index for index, reference in enumerate(tab_references) if reference not in places), key=lambda index: rule(tab_references[index])):
        # Keep the selected first tab before new members.
        start = 0 if tab_references[index] == first_member or not order else 1
        at = next((place + 1 for place in range(len(order) - 1, start - 1, -1)
                   if rule(tab_references[order[place]]) <= rule(tab_references[index])), start)
        order.insert(at, index)
    return order


class _LayoutLock:
    """One launcher at a time changes the shared session's workspaces and tab order."""

    def __init__(self, session: str) -> None:
        self.path: Path = layout_path(session).with_suffix(".lock")
        self.handle: int = -1

    def __enter__(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            worker_lifecycle.acquire_flock(self.handle)
        except TimeoutError:
            if self.handle >= 0:
                os.close(self.handle)
                self.handle = -1
            raise
        except OSError as error:
            print(f"herdr: layout lock is unavailable: {self.path}: {error}", file=sys.stderr)

    def __exit__(self, *_: object) -> None:
        if self.handle >= 0:
            os.close(self.handle)


@dataclass(frozen=True)
class SubmitUnproved:
    """Submit without waiting for a turn state."""


@dataclass(frozen=True)
class SubmitStarts:
    timeout_ms: int


SUBMIT_UNPROVED = SubmitUnproved()


@dataclass(frozen=True)
class PaneSessionsStarting:
    """The Codex pane starts without a current session identity."""


@dataclass(frozen=True)
class PaneSessionsUnknown:
    """The process evidence cannot establish the pane's current sessions."""

    reason: str


@dataclass
class PaneEvidenceRun:
    """Share decoded pane evidence and printed reasons within one run."""

    sessions: dict[tuple[str, str, str], frozenset[str] | PaneSessionsStarting | PaneSessionsUnknown] = field(default_factory=dict)
    shell_processes: dict[tuple[str, str, str], int] = field(default_factory=dict)
    reported: set[tuple[str, str, str]] = field(default_factory=set)
    lock: RLock = field(default_factory=RLock)


_pane_evidence_run: ContextVar[PaneEvidenceRun | ScanAtUse] = ContextVar(
    "pane_evidence_run", default=_SCAN_AT_USE)


@contextmanager
def pane_evidence_run() -> Generator[None]:
    """Retain the first pane evidence until the run ends or its target changes."""
    if isinstance(_pane_evidence_run.get(), PaneEvidenceRun):
        yield
        return
    token = _pane_evidence_run.set(PaneEvidenceRun())
    try:
        yield
    finally:
        _pane_evidence_run.reset(token)


@dataclass
class PaneObservations:
    """Share read-only pane evidence until the report scope closes."""
    replies: dict[tuple[str, tuple[str, ...]], dict[str, object]] = field(default_factory=dict)
    process_batches: set[str] = field(default_factory=set)

    @cached_property
    def process_places(self) -> dict[int, HerdrPlace]:
        return herdr_places()


_pane_observations: ContextVar[PaneObservations | None] = ContextVar("pane_observations", default=None)


@contextmanager
def observation_snapshot() -> Generator[None]:
    """Read each pane, process, and layout once for a status pass."""
    if _pane_observations.get() is not None:
        yield
        return
    token = _pane_observations.set(PaneObservations())
    try:
        with pane_evidence_run(), process_snapshot():
            yield
    finally:
        _pane_observations.reset(token)


def forget_pane_evidence(session: str, pane: str) -> None:
    """Require fresh scan evidence after a target read or process change."""
    run = _pane_evidence_run.get()
    if isinstance(run, PaneEvidenceRun):
        with run.lock:
            for key in tuple(run.sessions):
                if key[:2] == (session, pane):
                    del run.sessions[key]
                    _ = run.shell_processes.pop(key, None)
    observed = _pane_observations.get()
    if observed is not None:
        _ = observed.replies.pop((session, ("pane", "process-info", "--pane", pane)), None)


class CodexArgumentsSource(Enum):
    FOREGROUND = "arguments from the selected foreground process"


class Herdr:
    def __init__(self, session: str) -> None:
        self.binary: str = herdr_binary()
        self.session: str = session
        self.restore_placeholders: set[str] = set()
        self.created_workspaces: dict[str, str] = {}

    def argv(self, *arguments: str) -> list[str]:
        """The outside-pane targeting form verified with herdr 0.9.0."""
        return [self.binary, "--session", self.session, *arguments]

    def run(self, *arguments: str, deadline: float = math.inf) -> str:
        """Run within the caller's monotonic deadline, retaining the usual 10 s cap."""
        deadline = worker_lifecycle.effective_deadline(deadline)
        if deadline <= time.monotonic():
            raise HerdrFailure("deadline passed")
        context = f"herdr {' '.join(arguments[:2])}"
        try:
            result = run_program(self.argv(*arguments), timeout=min(PROGRAM_TIMEOUT_SECONDS, max(0.0, deadline - time.monotonic())))
        except subprocess.TimeoutExpired as error:
            raise HerdrFailure(f"{context}: no answer after {error.timeout:g} s") from error
        require_success(result, context)
        return result.stdout

    def label_pane(self, pane: str, text: str) -> None:
        """Keep the pane label independent of its agent lifecycle."""
        _ = self.run("pane", "rename", pane, text)

    def run_in_pane(self, pane: str, command: list[str], *, terminal: str = "") -> None:
        """Run a command only when the selected pane contains its empty shell."""
        deadline = time.monotonic() + operation_deadline.remaining(PROGRAM_TIMEOUT_SECONDS)
        while True:
            try:
                if check_target("", action="keys", place=HerdrPlace(self.session, pane), terminal=terminal,
                                pane_info=self.pane_row(pane), client=self):
                    break
                failure = HerdrFailure(f"{pane}: would send keys to an unproved target instead of the selected shell")
            except HerdrFailure as error:
                if error.code != "wrong_target":
                    raise
                failure = error
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise failure
            time.sleep(min(0.1, remaining))
        _ = self.run("pane", "run", pane, shlex.join(command))

    def result(self, *arguments: str, deadline: float = math.inf) -> dict[str, object]:
        observed = _pane_observations.get()
        key = (self.session, arguments)
        shared = observed is not None and arguments[:2] in (
            ("pane", "list"), ("tab", "list"), ("workspace", "list"),
            ("agent", "list"), ("pane", "process-info"))
        if observed is not None and arguments[:2] == ("pane", "process-info"):
            self._process_info_batch(observed)
        if shared and observed is not None and key in observed.replies:
            return observed.replies[key]
        output = self.run(*arguments) if deadline == math.inf else self.run(*arguments, deadline=deadline)
        reply = json_object(decode_object(output, "herdr response").get("result"), "herdr result")
        if shared and observed is not None:
            observed.replies[key] = reply
        return reply

    def _process_info_batch(self, observed: PaneObservations) -> None:
        """Read all pane processes in one report batch without CLI subprocesses."""
        if self.session in observed.process_batches:
            return
        observed.process_batches.add(self.session)
        panes = self.listed("pane")
        run = _pane_evidence_run.get()
        if isinstance(run, PaneEvidenceRun):
            with run.lock:
                panes = [pane for pane in panes if
                         (self.session, str(pane.get("pane_id", "")), str(pane.get("terminal_id", ""))) not in run.sessions]
        def read(pane: str) -> tuple[str, dict[str, object]]:
            try:
                return pane, self.socket_request("pane.process_info", pane_id=pane)
            except (OSError, HerdrFailure):
                return pane, {}
        with ThreadPoolExecutor(max_workers=16) as pool:
            for pane, reply in pool.map(read, [json_text(pane.get("pane_id"), "pane id") for pane in panes]):
                if reply:
                    observed.replies[self.session, ("pane", "process-info", "--pane", pane)] = reply

    def socket_request(self, method: str, **params: object) -> dict[str, object]:
        """Send one request to this session's socket."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
            stream.settimeout(PROGRAM_TIMEOUT_SECONDS)
            stream.connect(str(_session_socket(self.session)))
            stream.sendall((json.dumps({"id": "herdr-tabs", "method": method, "params": params}) + "\n").encode())
            with stream.makefile("r", encoding="utf-8") as reply:
                response = decode_object(reply.readline(), f"herdr {method}")
        if "error" in response:
            error = json_object(response["error"], f"herdr {method} error")
            raise HerdrFailure(json_text(error.get("message"), "socket error"))
        return json_object(response.get("result"), f"herdr {method} result")

    def listed(self, kind: str, *arguments: str, deadline: float = math.inf) -> list[dict[str, object]]:
        return [json_object(item, f"herdr {kind}").copy() for item in json_items(
            self.result(kind, "list", *arguments, **({"deadline": deadline} if deadline != math.inf else {})).get(f"{kind}s"), f"herdr {kind} list")]

    def pane_row(self, pane: str) -> PaneIdentity | NoPane:
        return next((row for row in self.panes() if row["pane_id"] == pane), NoPane(pane))

    def panes(self) -> list[PaneIdentity]:
        panes: list[PaneIdentity] = []
        for pane in self.listed("pane"):
            label = pane.get("label")
            if label is not None and not isinstance(label, str):
                print(f"herdr {self.session}: pane label is unreadable; pane id used", file=sys.stderr)
                label = None
            session_id = pane_session(pane) or None
            entry: PaneIdentity = {"pane_id": json_text(pane.get("pane_id"), "herdr pane id"), "label": label,
                                  "terminal_id": json_text(pane.get("terminal_id", ""), "herdr terminal id"),
                                  "workspace_id": json_text(pane.get("workspace_id"), "herdr pane workspace id"),
                                  "session_id": session_id}
            if "tab_id" in pane:
                entry["tab_id"] = json_text(pane["tab_id"], "herdr pane tab id")
            if isinstance(focused := pane.get("focused"), bool):
                entry["focused"] = focused
            panes.append(entry)
        return panes

    def tab_sessions(self) -> list[TabSession]:
        """Read pane conversations and retain their display names."""
        panes = self.panes()
        spaces = {json_text(space.get("workspace_id"), "herdr workspace id"):
                  "" if space.get("label") is None else json_text(space["label"], "herdr workspace label")
                  for space in self.listed("workspace")}
        return [TabSession(pane["pane_id"], pane.get("label") or "", spaces.get(pane["workspace_id"], ""), pane["session_id"])
                for pane in panes]

    def pane_activity(self, pane: str) -> PaneAtShell | PaneRunning:
        """Describe whether the pane is at its shell or running foreground work."""
        forget_pane_evidence(self.session, pane)
        context = f"herdr session {self.session} pane {pane} process info"
        info = json_object(self.result("pane", "process-info", "--pane", pane).get("process_info"), context)
        shell, foreground = _pane_process_ids(info, context)
        commands = tuple(
            json_text(json_object(process, context).get("name"), context)
            for process in json_items(info.get("foreground_processes"), context)
        )
        return PaneAtShell() if foreground == shell else PaneRunning(commands)

    def pane_id(self, reference: str) -> str:
        """Resolve a staff member through its current session, or keep an explicit pane id."""
        if re.fullmatch(r"w[0-9]+:p[0-9]+", reference):
            return reference
        membership = _tab_staff()
        owner = membership.by_member_reference(reference) or membership.by_path(reference)
        target = owner.current_session if owner is not None else reference
        rows = self.panes()
        if any(row["pane_id"] == reference for row in rows):
            return reference
        matches = [row["pane_id"] for row in rows if row["session_id"] == target]
        if len(matches) != 1:
            raise HerdrFailure(f"session {target}: pane is unavailable or ambiguous")
        return matches[0]

    @pane_evidence_run()
    def open_tab(self, space: str, title: str, cwd: str, environment: dict[str, str], *, member: str = "") -> str:
        """Open a named tab and pane in an existing or new workspace."""
        options = ["--label", title, "--cwd", cwd]
        for key, value in _pane_environment(environment).items():
            options.extend(["--env", f"{key}={value}"])
        with _LayoutLock(self.session):
            saved = saved_layout(self.session)
            workspace = self._workspace_named(space)
            if workspace is None and self._place_saved_workspaces(saved, frozenset({space})):
                workspace = self._workspace_named(space)
            if workspace is not None:
                result = self.result("tab", "create", "--workspace", workspace, *options, "--no-focus")
            else:
                options[1] = space
                result = self.result("workspace", "create", *options, "--no-focus")
                created = json_object(result.get("workspace"), "created workspace")
                self.created_workspaces[space] = json_text(created.get("workspace_id"), "workspace id")
                tab = json_text(json_object(result.get("tab"), "herdr workspace tab").get("tab_id"), "herdr tab id")
                _ = self.run("tab", "rename", tab, title)
            root_pane = json_object(result.get("root_pane"), "herdr tab root pane")
            pane = json_text(root_pane.get("pane_id"), "herdr tab root pane id")
            _ = self.run("pane", "rename", pane, title)
            if workspace is not None:
                _ = self._close_placeholders(workspace)
                try:
                    pane = self.order_tabs(workspace, saved, opening_members=((pane, member),) if member else ()).get(pane, pane)
                except (HerdrFailure, OSError, ValueError) as error:
                    print(f"herdr {self.session}: tab order unchanged: {error}", file=sys.stderr)
            self.save_layout()
            return pane

    def _workspace_named(self, space: str, *, record: staff.StaffSnapshot | ReadStaffAtUse = _READ_STAFF_AT_USE,
                         exclude: frozenset[str] = _NO_LAYOUT_EXCLUSIONS,
                         evidence: pane_launch.NativePaneEvidence | pane_launch.ReadNativePanes = pane_launch.ReadNativePanes.CURRENT) -> str | None:
        membership = _tab_staff() if isinstance(record, ReadStaffAtUse) else record
        candidates = self._workspace_candidates(membership, exclude=exclude, evidence=evidence)
        if space in candidates:
            return candidates[space]
        created = self.created_workspaces.get(space)
        if created is not None and any(item.get("workspace_id") == created for item in self.listed("workspace")):
            return created
        producer_space = conf.read_or_repository("staff").text("PRODUCER_WORKSPACE")
        if space == producer_space:
            return next((json_text(item.get("workspace_id"), "herdr workspace id")
                         for item in self.listed("workspace") if item.get("label") == space), None)
        return None

    @pane_evidence_run()
    def _workspace_candidates(self, membership: staff.StaffSnapshot, *,
                              exclude: frozenset[str] = _NO_LAYOUT_EXCLUSIONS,
                              evidence: pane_launch.NativePaneEvidence | pane_launch.ReadNativePanes = pane_launch.ReadNativePanes.CURRENT) -> dict[str, str]:
        """Read workspace placement once from native owners, retained shells, and marked shells."""
        if isinstance(evidence, pane_launch.ReadNativePanes):
            panes = self.listed("pane")
            scan = self.scan_row_panes(membership, panes)
        else:
            panes = [dict(pane) for pane in evidence.panes]
            scan = evidence.scan
        candidates: dict[str, str] = {}
        for row_id, matches in scan.found.items():
            if row_id in exclude:
                continue
            member = membership.rows_by_id[row_id]
            _ = candidates.setdefault(row_workspace(member, membership, member.name),
                                  json_text(matches[0].get("workspace_id"), "herdr workspace id"))
        run = cast(PaneEvidenceRun, _pane_evidence_run.get())
        for pane in panes:
            key = (self.session, str(pane.get("pane_id", "")), str(pane.get("terminal_id", "")))
            with run.lock:
                pid = run.shell_processes.get(key)
            if pid is None:
                continue
            try:
                environment = process_environment(pid)
            except (OSError, ValueError, subprocess.SubprocessError):
                continue
            member = membership.by_member_reference(environment.get("CLAUDE_CODE_SESSION_ID", ""))
            member = member or membership.by_member_reference(environment.get("CODEX_THREAD_ID", ""))
            if member is not None and member.id not in exclude:
                _ = candidates.setdefault(row_workspace(member, membership, member.name),
                                      json_text(pane.get("workspace_id"), "herdr workspace id"))
        if panes:
            entries = pane_launch.read(pane_launch.record_directory() / f"{self.session}.json")
            references = {reference.id for entry in entries.values()
                          if isinstance(reference := pane_launch.launch_reference(entry, membership), pane_launch.StaffLaunchReference)}
            for identity in references - exclude:
                member = membership.by_id(identity)
                if member is None:
                    continue
                space = row_workspace(member, membership, member.name)
                matches = pane_launch.reference_panes(pane_launch.StaffLaunchReference(identity), membership, self, entries,
                    evidence=pane_launch.NativePaneEvidence(panes, scan))
                for pane in matches:
                    if isinstance(self.pane_activity(json_text(pane.get("pane_id"), "pane id")), PaneAtShell):
                        _ = candidates.setdefault(space, json_text(pane.get("workspace_id"), "herdr workspace id"))
        return candidates

    def _place_saved_workspaces(self, saved: SavedLayout, selected: frozenset[str]) -> bool:
        """Create only workspaces this restore uses, in their saved order."""
        membership = _tab_staff()
        saved = _member_layout(saved, membership)
        wanted = [space["name"] for space in saved["workspaces"]
                  if space["name"] in selected and space["seen"] >= saved["at"] - LAYOUT_RESTORE_SECONDS]
        candidates = self._workspace_candidates(membership) if wanted else {}
        wanted = [name for name in wanted if name not in candidates and name not in self.created_workspaces]
        if not wanted:
            return False
        for name in wanted:
            _ = self._create_workspace(name)
        return True

    def _create_workspace(self, name: str) -> str:
        result = self.result("workspace", "create", "--label", name, "--cwd", str(Path.home()), "--no-focus")
        tab = json_text(json_object(result.get("tab"), "workspace tab").get("tab_id"), "tab id")
        pane = json_text(json_object(result.get("root_pane"), "workspace pane").get("pane_id"), "pane id")
        _ = self.run("tab", "rename", tab, LAYOUT_PLACEHOLDER)
        _ = self.run("pane", "rename", pane, LAYOUT_PLACEHOLDER)
        self.restore_placeholders.add(pane)
        workspace = json_text(json_object(result.get("workspace"), "created workspace").get("workspace_id"), "workspace id")
        self.created_workspaces[name] = workspace
        return workspace

    def prepare_restore_workspaces(self, selected: frozenset[str]) -> None:
        """Reserve this run's saved workspace positions before any launch."""
        with _LayoutLock(self.session):
            _ = self._place_saved_workspaces(saved_layout(self.session), selected)

    def _close_placeholders(self, workspace: str) -> bool:
        if not self.restore_placeholders:
            return False
        tabs = self.listed("tab", "--workspace", workspace)
        changed = False
        for tab in tabs:
            if tab.get("pane_count") != 1:
                continue
            ident = json_text(tab.get("tab_id"), "herdr tab id")
            owned = [pane for pane in self.listed("pane") if pane.get("tab_id") == ident]
            if len(owned) != 1 or owned[0].get("pane_id") not in self.restore_placeholders:
                continue
            try:
                safe = check_target("", action="close", place=HerdrPlace(
                    self.session, json_text(owned[0].get("pane_id"), "herdr pane id")), client=self)
            except HerdrFailure as error:
                print(f"herdr {self.session}: placeholder retained: {error}", file=sys.stderr)
                continue
            if not safe:
                continue
            # Keep the current tab membership before the close.
            current = self.listed("tab", "--workspace", workspace)
            if not any(tab.get("tab_id") == ident and tab.get("pane_count") == 1 for tab in current):
                continue
            if len(current) == 1:
                _ = self.run("workspace", "close", workspace)
            else:
                _ = self.run("tab", "close", ident)
            self.restore_placeholders.discard(json_text(owned[0].get("pane_id"), "herdr pane id"))
            changed = True
        return changed

    def close_restore_placeholders(self) -> None:
        """Remove unfilled shell placeholders even when their launches failed."""
        with _LayoutLock(self.session):
            for space in self.live_layout():
                _ = self._close_placeholders(space.workspace_id)

    def live_layout(self) -> list[_LiveSpace]:
        """The session's workspaces in order, each with its tab titles in order, placeholders left out."""
        workspaces, tabs = self.listed("workspace"), self.listed("tab")
        spaces: list[_LiveSpace] = []
        for workspace in workspaces:
            ident = json_text(workspace.get("workspace_id"), "herdr workspace id")
            label = workspace.get("label")
            titles = [json_text(tab.get("label"), "herdr tab label") for tab in tabs if tab.get("workspace_id") == ident]
            if isinstance(label, str) and label:
                spaces.append(_LiveSpace(ident, label, titles))
        return spaces

    def save_layout(self, *, record: staff.StaffSnapshot | ReadStaffAtUse = _READ_STAFF_AT_USE,
                    scan: RowPaneScan | ScanAtUse = _SCAN_AT_USE) -> None:
        """Save the layout and report a read or write failure on stderr."""
        try:
            self.record_layout(record=record, scan=scan)
        except (HerdrFailure, OSError) as error:
            print(f"herdr {self.session}: layout not saved: {error}", file=sys.stderr)

    @pane_evidence_run()
    def record_layout(self, *, record: staff.StaffSnapshot | ReadStaffAtUse = _READ_STAFF_AT_USE,
                      scan: RowPaneScan | ScanAtUse = _SCAN_AT_USE) -> None:
        """Save staff row ids in the native tab order."""
        record = _tab_staff() if isinstance(record, ReadStaffAtUse) else record
        spaces = self.listed("workspace")
        tabs = self.listed("tab")
        scan = self.scan_row_panes(record, self.listed("pane")) if isinstance(scan, ScanAtUse) else scan
        owners = _tab_members(record, scan)
        saved = saved_layout(self.session)
        retained = {tab["staff_id"]: space["name"] for space in saved["workspaces"] for tab in space["tabs"]}
        groups: dict[str, _LiveSpace] = {}
        for space in spaces:
            workspace = json_text(space.get("workspace_id"), "herdr workspace id")
            for tab in tabs:
                member = owners.get(json_text(tab.get("tab_id"), "herdr tab id"))
                if tab.get("workspace_id") != workspace or member is None:
                    continue
                name = (retained[member.id] if scan.unknown and member.id in retained else
                        row_workspace(member, record, str(space.get("label", member.name))))
                group = groups.setdefault(name, _LiveSpace(workspace, name, []))
                if member.id not in group.tabs:
                    group.tabs.append(member.id)
        live = list(groups.values())
        if live:
            if not scan.unknown:
                for space in saved["workspaces"]:
                    space["tabs"] = [item for item in space["tabs"] if record.by_id(staff.StaffId(item["staff_id"])) is not None]
            _write_layout(self.session, merged_layout(saved, live, time.time(), expire=not scan.unknown, record=record))

    def pane_tab(self, pane: str) -> tuple[str, int, SavedMemberTab | UnownedTab]:
        """Read the native member and the current tab position."""
        panes = self.listed("pane")
        selected = next((item for item in panes if item.get("pane_id") == pane), dict[str, object]())
        tabs = [item for item in self.listed("tab") if item.get("workspace_id") == selected.get("workspace_id")]
        index, target = next(((index, item) for index, item in enumerate(tabs, 1)
                              if item.get("tab_id") == selected.get("tab_id")), (0, dict[str, object]()))
        workspace = next((space.get("label") for space in self.listed("workspace")
                          if space.get("workspace_id") == selected.get("workspace_id")), None)
        record = _tab_staff()
        owner = (_tab_members(record, self.scan_row_panes(record, panes)).get(str(target.get("tab_id", "")))
                 if record.rows else None)
        member = (SavedMemberTab(staff_id=owner.id, seen=time.time()) if owner is not None
                  else UnownedTab(json_text(target.get("label"), "herdr tab label")))
        return json_text(workspace, "herdr workspace label"), index, member

    @pane_evidence_run()
    def remember_row_tab(self, row_id: str, pane: str) -> None:
        """Save the position of a tab whose native process proves the member."""
        with _LayoutLock(self.session):
            workspace, index, target = self.pane_tab(pane)
            if isinstance(target, UnownedTab) or target["staff_id"] != row_id:
                return
            self.record_layout()
            saved = saved_layout(self.session)
            _ = _bind_row_tab(saved, workspace, row_id, target, max(0, index - 1))
            _write_layout(self.session, saved)

    def saved_row_tab(self, row_id: str, title: str, fallback_workspace: str) -> NewTab | SplitPane | ExistingPane:
        """Select current native placement and leave an unowned shell unowned."""
        record = _tab_staff()
        member = record.by_id(staff.StaffId(row_id))
        if member is None:
            return NewTab(fallback_workspace, title)
        if not any(tab["staff_id"] == row_id for space in saved_layout(self.session)["workspaces"] for tab in space["tabs"]):
            return NewTab(row_workspace(member, record, fallback_workspace), title)
        panes = self.listed("pane")
        scan = self.scan_row_panes(record, panes)
        entries = pane_launch.read(pane_launch.record_directory() / f"{self.session}.json")
        matches = pane_launch.reference_panes(pane_launch.StaffLaunchReference(member.id), record, self, entries,
                                             evidence=pane_launch.NativePaneEvidence(panes, scan))
        for pane in matches:
            identity = json_text(pane.get("pane_id"), "pane id")
            if isinstance(self.pane_activity(identity), PaneAtShell):
                return ExistingPane(identity, json_text(pane.get("terminal_id"), "terminal id"))
        tabs = {pane.get("tab_id") for pane in matches}
        children = {row.id for row in record.find(parent=member.id)}
        siblings = [pane for identity, found in scan.found.items() for pane in found
                    if (pane.get("tab_id") in tabs and pane not in matches) or identity in children]
        if siblings:
            return SplitPane(json_text(siblings[0].get("pane_id"), "pane id"), "right", 0.5, title)
        return NewTab(row_workspace(member, record, fallback_workspace), title)

    def producer_row_tab(self, row_id: str, title: str, workspace: str,
                         *, record: staff.StaffSnapshot | ReadStaffAtUse = _READ_STAFF_AT_USE) -> NewTab | ExistingPane:
        """Reuse a producer's shell only in its own tab in the configured workspace."""
        destination = self.saved_row_tab(row_id, title, workspace)
        if isinstance(destination, ExistingPane) and self.producer_pane(destination.pane, row_id, workspace, record=record):
            return destination
        return NewTab(workspace, title)

    def producer_pane(self, pane_id: str, row_id: str, workspace: str,
                      *, record: staff.StaffSnapshot | ReadStaffAtUse = _READ_STAFF_AT_USE) -> bool:
        """Accept a producer's recorded shell in its actual tab and configured workspace."""
        record = _tab_staff() if isinstance(record, ReadStaffAtUse) else record
        owner = record.by_id(staff.StaffId(row_id))
        if owner is None or owner.kind != "producer":
            return False
        panes = self.listed("pane")
        own = next((pane for pane in panes if pane.get("pane_id") == pane_id), None)
        if own is None or not isinstance(self.pane_activity(pane_id), PaneAtShell):
            return False
        tab = next((tab for tab in self.listed("tab") if tab.get("tab_id") == own.get("tab_id")), None)
        if (tab is None or sum(pane.get("tab_id") == tab.get("tab_id") for pane in panes) != 1
                or not any(space.get("label") == workspace and space.get("workspace_id") == tab.get("workspace_id")
                           for space in self.listed("workspace"))):
            return False
        entries = pane_launch.read(pane_launch.record_directory() / f"{self.session}.json")
        scan = self.scan_row_panes(record, panes)
        matches = pane_launch.reference_panes(pane_launch.StaffLaunchReference(owner.id), record, self, entries,
                                             evidence=pane_launch.NativePaneEvidence(panes, scan))
        if not any(pane.get("pane_id") == pane_id for pane in matches):
            return False
        launch = entries.get(pane_id)
        if launch is not None and record.by_member_reference(pane_launch.reference_id(launch)) != owner:
            return False
        return True

    def _retitle(self, row: dict[str, object], title: str, *, tab: bool) -> bool:
        changed = row.get("label") != title
        if changed:
            _ = self.run("pane", "rename", json_text(row.get("pane_id"), "pane id"), title)
        if tab:
            ident = json_text(row.get("tab_id"), "tab id")
            shown = next((item.get("label") for item in self.listed("tab") if item.get("tab_id") == ident), None)
            if shown != title:
                _ = self.run("tab", "rename", ident, title)
                changed = True
        return changed

    def mark_tab_rename(self, identity: staff.StaffId) -> None:
        """Retain a rename request before the staff name or display labels change."""
        with _LayoutLock(self.session):
            saved = saved_layout(self.session)
            record = _tab_staff()
            changed = False
            for space in saved["workspaces"]:
                for entry in space["tabs"]:
                    member = record.by_member_reference(entry["staff_id"])
                    if member is not None and member.id == identity and not entry.get("sort_after_rename"):
                        entry["sort_after_rename"] = True
                        changed = True
            if changed:
                _write_layout(self.session, saved)

    @pane_evidence_run()
    def retitle(self, pane: str, title: str, *, tab: bool = True) -> bool:
        """Set the pane title and, when requested, its tab title."""
        with _LayoutLock(self.session):
            row = next((row for row in self.listed("pane") if row.get("pane_id") == pane), None)
            if row is None:
                return False
            saved = saved_layout(self.session)
            reorder = False
            record = _tab_staff()
            owner: staff.StaffRow | None = None
            if tab and saved["workspaces"]:
                scan = self.scan_row_panes(record, self.listed("pane"))
                owner = _tab_members(record, scan).get(str(row.get("tab_id", "")))
                reorder = any(entry.get("sort_after_rename")
                    and owner is not None and entry["staff_id"] == owner.id
                    for space in saved["workspaces"] for entry in space["tabs"])
            changed = self._retitle(row, title, tab=tab)
            if not changed and not reorder:
                return False
            if tab:
                _ = self.order_tabs(json_text(row.get("workspace_id"), "workspace id"), saved)
            if reorder:
                self.record_layout()
                saved = saved_layout(self.session)
                for space in saved["workspaces"]:
                    for entry in space["tabs"]:
                        if owner is not None and entry["staff_id"] == owner.id:
                            _ = entry.pop("sort_after_rename", False)
                _write_layout(self.session, saved)
            else:
                self.save_layout()
            return True

    def _row_pane_sessions(self, pane: dict[str, object], run: PaneEvidenceRun) -> frozenset[str] | PaneSessionsStarting | PaneSessionsUnknown:
        key = (self.session, str(pane.get("pane_id", "")), str(pane.get("terminal_id", "")))
        with run.lock:
            if key in run.sessions:
                return run.sessions[key]
            try:
                info = json_object(self.result("pane", "process-info", "--pane",
                    json_text(pane.get("pane_id"), "pane id")).get("process_info"), "native pane processes")
                shell, foreground = _pane_process_ids(info, "native pane processes")
                sessions: frozenset[str] | PaneSessionsStarting | PaneSessionsUnknown = frozenset(_pane_sessions(info, pane))
                if not sessions and foreground == shell:
                    run.shell_processes[key] = shell
            except CodexPaneStarting:
                sessions = PaneSessionsStarting()
            except (HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
                sessions = PaneSessionsUnknown(str(error))
            run.sessions[key] = sessions
            return sessions

    @pane_evidence_run()
    def scan_row_panes(self, record: staff.StaffSnapshot,
                       panes: list[dict[str, object]]) -> RowPaneScan:
        """Match native sessions and report each incomplete observation once."""
        found: dict[staff.StaffId, list[dict[str, object]]] = {}
        unknown: list[UnknownPane] = []
        launches: dict[str, pane_launch.PaneLaunch] | None = None
        run = cast(PaneEvidenceRun, _pane_evidence_run.get())
        for pane in panes:
            sessions = self._row_pane_sessions(pane, run)
            if isinstance(sessions, (PaneSessionsStarting, PaneSessionsUnknown)):
                reason = "starting" if isinstance(sessions, PaneSessionsStarting) else sessions.reason
                unread = UnknownPane(str(pane.get("pane_id", "")), str(pane.get("tab_id", "")), reason)
                unknown.append(unread)
                if isinstance(sessions, PaneSessionsUnknown):
                    if launches is None:
                        try:
                            launches = pane_launch.read_strict(pane_launch.record_directory() / f"{self.session}.json")
                        except (OSError, ValueError):
                            launches = {}
                    launch = launches.get(unread.pane)
                    member = launch.get("staff_id", "") if launch is not None else ""
                    if member and member not in record.rows_by_id:
                        continue
                notice = (self.session, unread.pane, unread.reason)
                with run.lock:
                    if notice in run.reported:
                        continue
                    if isinstance(sessions, PaneSessionsStarting):
                        print(f"herdr {self.session}:{unread.pane}: pane is starting", file=sys.stderr)
                    else:
                        print(f"herdr {self.session}:{unread.pane} tab {unread.tab}: pane evidence is unknown: "
                              + f"{' '.join(unread.reason.splitlines())}; row placement retained", file=sys.stderr)
                    run.reported.add(notice)
            else:
                for identity in sessions:
                    if (row := record.by_current_session(identity)) is not None:
                        found.setdefault(row.id, []).append(pane)
        return RowPaneScan(found, unknown)

    @pane_evidence_run()
    def retitle_row(self, row_id: str) -> bool:
        """Set each proved pane's title from its row."""
        record = _tab_staff()
        row = record.by_id(staff.StaffId(row_id))
        if row is None:
            raise HerdrFailure(f"no staff row {row_id!r}")
        title = row.name
        if row.kind == "worker":
            agent_name = cast(Callable[[staff.StaffRow, str], str], import_module("scripts.herdr.worker_panes").agent_name)
            title = agent_name(record.rows_by_id[cast(staff.ReportsTo, row.parent).id], row.name)
        with _LayoutLock(self.session):
            scan = self.scan_row_panes(record, self.listed("pane"))
            matches = scan.found.get(row.id, [])
            changed = any([self._retitle(pane, title, tab=row.kind != "worker") for pane in matches])
            if changed:
                if row.kind != "worker":
                    for workspace in dict.fromkeys(json_text(pane.get("workspace_id"), "workspace id") for pane in matches):
                        _ = self.order_tabs(workspace, saved_layout(self.session), record=record)
                self.save_layout(record=record, scan=scan)
            return changed

    @pane_evidence_run()
    def move_row_tabs(self, row_id: str, *, record: staff.StaffSnapshot | ReadStaffAtUse = _READ_STAFF_AT_USE) -> bool:
        """Move member tabs and saved bindings into the parent's workspace."""
        record = _tab_staff() if isinstance(record, ReadStaffAtUse) else record
        row = record.by_id(staff.StaffId(row_id))
        parent = record.parent_of(row) if row is not None else None
        if row is None or parent is None:
            raise HerdrFailure(f"staff row {row_id!r} has no parent workspace")
        with _LayoutLock(self.session):
            saved = saved_layout(self.session)
            panes = self.listed("pane")
            scan = self.scan_row_panes(record, panes)
            spaces = self.listed("workspace") if panes else []
            moving = frozenset(member.id for member in (row, *record.find(parent=row.id)))
            workspace = (self._workspace_named(parent.name, record=record, exclude=moving,
                evidence=pane_launch.NativePaneEvidence(panes, scan)) or "") if panes else ""
            focused_space = next((item.get("workspace_id") for item in spaces if item.get("focused") is True), "")
            focused = next((json_text(pane.get("pane_id"), "pane id") for pane in panes
                            if pane.get("focused") is True and pane.get("workspace_id") == focused_space), "")
            changed, bound, placed = bool(scan.unknown), False, False
            visited: set[str] = set()
            try:
                for member in (row, *record.find(parent=row.id)):
                    matches = scan.found.get(member.id, [])
                    for pane in matches:
                        tab = json_text(pane.get("tab_id"), "tab id")
                        if tab in visited:
                            continue
                        visited.add(tab)
                        if pane.get("workspace_id") != workspace:
                            changed = True
                            if not workspace:
                                workspace = self._create_workspace(parent.name)
                            try:
                                moved = self._move_row_tab(pane, workspace, member.name, panes)
                                focused = moved.get(focused, focused)
                                placed = bool(moved) or placed
                            except (HerdrFailure, OSError, ValueError) as error:
                                print(f"herdr {member.id}: tab move is pending: {error}", file=sys.stderr)
                        else:
                            placed = True
                            changed = self._retitle(pane, member.name, tab=True) or changed
                    bound = _bind_row_tab(saved, parent.name, member.id, record=record) or bound
                if bound:
                    _write_layout(self.session, saved)
                if placed:
                    ordered = self.order_tabs(workspace, saved, record=record)
                    focused = ordered.get(focused, focused)
                    changed = bool(ordered) or changed
                    changed = self._close_placeholders(workspace) or changed
            finally:
                if focused:
                    try:
                        current = next((pane for pane in self.listed("pane") if pane.get("pane_id") == focused), None)
                        if current is not None:
                            destination = json_text(current.get("workspace_id"), "workspace id")
                            active = any(space.get("workspace_id") == destination and space.get("focused") is True
                                         for space in self.listed("workspace"))
                            if not active:
                                self.focus_workspace(destination)
                            if not active or current.get("focused") is not True:
                                _ = self.run("tab", "focus", json_text(current.get("tab_id"), "tab id"))
                    except (HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
                        detail = str(error).replace("\n", " ")
                        print(f"herdr {self.session}: focus restore failed: {detail}; tab move result retained", file=sys.stderr)
            return changed or bound

    def _move_row_tab(self, selected: dict[str, object], workspace: str, title: str,
                      panes: list[dict[str, object]]) -> dict[str, str]:
        """Replay the exported split tree without creating terminals."""
        tab = json_text(selected.get("tab_id"), "tab id")
        layout = json_object(self.socket_request("layout.export", tab_id=tab).get("layout"), "exported layout")
        if layout.get("zoomed") is True:
            print(f"herdr {tab}: tab is zoomed; unzoom it and run again", file=sys.stderr)
            return {}
        plan: list[tuple[str, str, str, str]] = []
        leaves: list[str] = []
        def visit(value: object) -> str:
            node = json_object(value, "layout node")
            if node.get("type") == "pane":
                pane = json_text(node.get("pane_id"), "layout pane id")
                leaves.append(pane)
                return pane
            direction, ratio = node.get("direction"), node.get("ratio")
            if (node.get("type") != "split" or direction not in ("right", "down")
                    or isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 < ratio < 1):
                raise HerdrFailure("tab split tree is invalid")
            position = len(plan)
            first, second = visit(node.get("first")), visit(node.get("second"))
            plan.insert(position, (second, first, str(direction), str(ratio)))
            return first
        first = visit(layout.get("root"))
        members = {json_text(pane.get("pane_id"), "pane id") for pane in panes if pane.get("tab_id") == tab}
        if len(leaves) != len(set(leaves)) or set(leaves) != members:
            raise HerdrFailure("tab split tree does not match its panes")
        moved: dict[str, str] = {}
        try:
            result = self._move_pane(first, "--new-tab", "--workspace", workspace)
            new_tab = json_text(json_object(result.get("created_tab"), "moved tab").get("tab_id"), "tab id")
            moved[first] = json_text(json_object(result.get("pane"), "moved pane").get("pane_id"), "pane id")
            for pane, anchor, direction, ratio in plan:
                result = self._move_pane(pane, "--tab", new_tab, "--split", direction,
                    "--target-pane", moved[anchor], "--ratio", ratio, "--no-focus")
                moved[pane] = json_text(json_object(result.get("pane"), "moved pane").get("pane_id"), "pane id")
            _ = self.run("tab", "rename", new_tab, title)
        except (HerdrFailure, OSError, ValueError) as error:
            if not moved:
                raise
            left = ", ".join(pane for pane in leaves if pane not in moved) or "none"
            print(f"herdr {tab}: split was lost; panes left behind: {left}; {error}", file=sys.stderr)
        return moved

    def _move_pane(self, pane: str, *arguments: str) -> dict[str, object]:
        result = json_object(self.result("pane", "move", pane, *arguments).get("move_result"), "pane move")
        changed = result.get("changed")
        if changed is None:
            key, flag = ("workspace_id", "--workspace") if "--workspace" in arguments else ("tab_id", "--tab")
            destination = arguments[arguments.index(flag) + 1]
            moved = json_object(result.get("pane"), "moved pane").get("pane_id")
            changed = any(row.get("pane_id") == moved and row.get(key) == destination for row in self.listed("pane"))
        if changed is not True:
            raise HerdrFailure(f"pane {pane} did not move: {result.get('reason', 'unknown reason')}")
        return result

    def order_tabs(self, workspace: str, saved: SavedLayout,
                   *, record: staff.StaffSnapshot | ReadStaffAtUse = _READ_STAFF_AT_USE,
                   opening_members: Sequence[tuple[str, str]] = ()) -> dict[str, str]:
        """Order existing tabs and return the unchanged pane ids of moved tabs."""
        tabs = self.listed("tab", "--workspace", workspace)
        if len(tabs) < 2:
            return {}
        panes = self.listed("pane")
        panes_of: dict[str, list[str]] = {}
        for pane in panes:
            tab = json_text(pane.get("tab_id"), "herdr pane tab id")
            panes_of.setdefault(tab, []).append(json_text(pane.get("pane_id"), "herdr pane id"))
        membership = _tab_staff() if isinstance(record, ReadStaffAtUse) else record
        scan = self.scan_row_panes(membership, panes)
        owners = _tab_members(membership, scan)
        for pane_id, member_id in opening_members:
            member = membership.by_id(staff.StaffId(member_id))
            pane = next((pane for pane in panes if pane.get("pane_id") == pane_id), None)
            if member is not None and pane is not None:
                owners[json_text(pane.get("tab_id"), "herdr pane tab id")] = member
        keys = [owners[tab_id].id if tab_id in owners else tab_id
                for tab in tabs if (tab_id := json_text(tab.get("tab_id"), "herdr tab id"))]
        units = {row.id for row in staff.active(membership, "unit")}
        parents = {row.parent.id for row in owners.values() if row.id in units and isinstance(row.parent, staff.ReportsTo)}
        first = next((key for key in keys if key in parents), "")
        if not first:
            first = next((key for key in keys if (member := membership.by_id(staff.StaffId(key))) is not None
                          and member.kind == "showrunner"), "")
        stored = [entry for space in saved["workspaces"] for entry in space["tabs"] if entry["staff_id"] in keys
                  and not entry.get("sort_after_rename")]
        order = tab_order(first, keys, stored, units, record=membership)
        current = list(range(len(tabs)))
        moved: dict[str, str] = {}
        for position, index in enumerate(order):
            previous = current.index(index)
            if previous == position:
                continue
            tab = json_text(tabs[index].get("tab_id"), "tab id")
            _ = self.socket_request("tab.move", tab_id=tab, insert_index=position)
            current.insert(position, current.pop(previous))
            moved.update((pane, pane) for pane in panes_of.get(tab, []))
        return moved

    @pane_evidence_run()
    def sort_tabs(self) -> WorkspacesKeptForSplitTabs:
        """Sort every workspace and preserve its panes and splits."""
        with _LayoutLock(self.session):
            membership = _tab_staff()
            for space in self.live_layout():
                _ = self.order_tabs(space.workspace_id, SavedLayout(at=0.0, workspaces=[]), record=membership)
            self.save_layout(record=membership)
        return WorkspacesKeptForSplitTabs([])

    def close_tab_of(self, pane: str) -> None:
        """Close only the selected shell for outside callers."""
        _ = self.close_pane(pane, target="")

    def close_tab_confirmed(self, pane: str, *, target: str = "", terminal: str = "") -> bool:
        """Return true only after the selected pane and terminal are absent."""
        row = self.pane_row(pane)
        if isinstance(row, NoPane):
            return self._pane_closed(pane, terminal)
        terminal = terminal or row.get("terminal_id", "")
        if not check_target(target, action="close", place=HerdrPlace(self.session, pane),
                            terminal=terminal, pane_info=row, client=self):
            return False
        return self._close_pane(pane) and self._pane_closed(pane, terminal)

    def close_terminal_pane(self, pane: str, terminal: str) -> bool:
        """Close the matching terminal and confirm its removal without a process check."""
        if not terminal:
            return False
        row = self.pane_row(pane)
        if isinstance(row, NoPane):
            return self._pane_closed(pane, terminal)
        if row.get("terminal_id") != terminal:
            return False
        return self._close_pane(pane) and self._pane_closed(pane, terminal)

    def _pane_closed(self, pane: str, terminal: str) -> bool:
        if any(row["pane_id"] == pane or terminal and row.get("terminal_id") == terminal for row in self.panes()):
            print(f"herdr {self.session}:{pane}: closure is pending; record retained", file=sys.stderr)
            return False
        return True

    def close_retained_pane(self, pane: str, *, terminal: str = "") -> bool:
        """Retire an outside caller's record only after confirmed terminal closure."""
        path = pane_launch.record_directory() / f"{self.session}.json"
        try:
            original = pane_launch.read_valid_entries(path).get(pane)
        except (OSError, ValueError) as error:
            print(f"herdr {self.session}:{pane}: legacy record is unknown: {error}", file=sys.stderr)
            original = None
        rows = self.listed("pane")
        selected = next((row for row in rows if row.get("pane_id") == pane), None)
        if selected is not None:
            observed = json_text(selected.get("terminal_id", ""), "herdr terminal id")
            if not observed or terminal and terminal != observed:
                raise HerdrFailure(f"{pane}: terminal identity changed; would close another terminal", "wrong_target")
            terminal = observed
        target = ""
        if original is not None:
            if "staff_id" in original:
                owner = staff.initial_snapshot().by_id(staff.StaffId(original["staff_id"]))
                if owner is not None:
                    target = str(owner.current_session)
            else:
                target = original["session_id"]
        if not self.close_tab_confirmed(pane, target=target, terminal=terminal):
            return False
        if original is not None:
            try:
                pane_launch.retire_launch(path, original["pane_id"], original)
            except (OSError, ValueError) as error:
                print(f"herdr {self.session}:{pane}: legacy record retained: {error}", file=sys.stderr)
        return True

    def focus_tab_of(self, pane: str) -> None:
        """Show the tab holding `pane`, and its workspace, in the session's attached clients."""
        for row in self.listed("pane"):
            if row.get("pane_id") == pane:
                self.focus_workspace(json_text(row.get("workspace_id"), "herdr pane workspace id"))
                _ = self.run("tab", "focus", json_text(row.get("tab_id"), "herdr pane tab id"))
                return
        raise HerdrFailure(f"herdr pane {pane} is not open in session {self.session}")

    def focus_workspace(self, workspace_id: str) -> None:
        """Show a workspace in the session's attached clients."""
        _ = self.run("workspace", "focus", workspace_id)

    def close_startup_workspaces(self, restored_workspace: str) -> None:
        """Remove only untouched home shells left by this restore's attach window."""
        candidates = [_workspace_startup_state(item) for item in self.listed("workspace")]
        for candidate in candidates:
            if candidate.workspace_id == restored_workspace:
                continue
            # A previous close can take long enough for another shell to receive input.
            workspaces = [_workspace_startup_state(item) for item in self.listed("workspace")]
            raw_panes = self.listed("pane")
            panes = [_pane_startup_state(item) for item in raw_panes]
            if any(workspace.workspace_id == candidate.workspace_id and _untouched_startup_shell(workspace, panes)
                   for workspace in workspaces):
                members = [json_object(item, "herdr pane") for item in raw_panes
                           if json_object(item, "herdr pane").get("workspace_id") == candidate.workspace_id]
                if not members:
                    print(f"herdr {self.session}: workspace {candidate.workspace_id} has unknown panes; workspace retained", file=sys.stderr)
                    continue
                try:
                    if all(check_target("", action="close",
                                        place=HerdrPlace(self.session, json_text(pane.get("pane_id"), "herdr pane id")),
                                        pane_info=cast(PaneIdentity, cast(object, pane)), client=self) for pane in members):
                        _ = self.run("workspace", "close", candidate.workspace_id)
                except HerdrFailure as error:
                    print(f"herdr {self.session}: workspace {candidate.workspace_id} retained: {error}", file=sys.stderr)

    def producer_admission_launch(self, pane: str, kind: Literal["claude", "codex"]) -> pane_launch.CurrentLaunch:
        """Read the native identity and launch evidence before producer admission."""
        info = json_object(self.result("pane", "process-info", "--pane", pane).get("process_info"),
                           "producer process info")
        row = self.pane_row(pane)
        if isinstance(row, NoPane):
            raise HerdrFailure("producer pane is unavailable")
        observed = pane_native_process(info, kind, row)
        processes = [json_object(value, "producer process")
                     for value in json_items(info.get("foreground_processes"), "producer processes")
                     if _is_agent_process(_foreground_arguments(json_object(value, "producer process")), kind)]
        if len(processes) != 1:
            raise HerdrFailure("producer has no unique foreground process")
        process = processes[0]
        pid = process.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise HerdrFailure("producer has no process id")
        folder = json_text(process.get("cwd"), "producer folder")
        if (not Path(folder).is_absolute() or observed.environment.get("HERDR_PANE_ID") != pane
                or observed.environment.get("HERDR_SESSION", "default") != self.session):
            raise HerdrFailure("producer process does not match its pane")
        entry = pane_launch.launch(pane, observed.current_identity, folder, observed.environment,
                                   _foreground_arguments(process), kind=kind)
        entry["config_dir"] = observed.environment.get("CODEX_HOME" if kind == "codex" else "CLAUDE_CONFIG_DIR") or str(
            Path.home() / (".codex" if kind == "codex" else ".claude"))
        entry.update(pid=pid, proc_start=observed.started)
        current = self.native_pane_process(pane, kind, PROGRAM_TIMEOUT_SECONDS, pane_row=row)
        if current != observed:
            raise HerdrFailure("producer process changed before admission")
        return entry

    def codex_launch(self, pane: str,
                     arguments: Sequence[str] | CodexArgumentsSource = CodexArgumentsSource.FOREGROUND) -> pane_launch.CurrentLaunch:
        """Read one Codex launch from its exact foreground process."""
        forget_pane_evidence(self.session, pane)
        info = json_object(self.result("pane", "process-info", "--pane", pane).get("process_info"),
                           "Codex launch process info")
        return self._codex_launch(pane, info, arguments)

    def _codex_launch(self, pane: str, info: dict[str, object],
                      arguments: Sequence[str] | CodexArgumentsSource) -> pane_launch.CurrentLaunch:
        matches: list[tuple[dict[str, object], list[str]]] = []
        for value in json_items(info.get("foreground_processes"), "Codex launch processes"):
            process = json_object(value, "Codex launch process")
            argv = _foreground_arguments(process)
            if is_codex_process(argv):
                matches.append((process, argv))
        if len(matches) != 1:
            raise ValueError("Codex launch has no unique foreground process")
        process, argv = matches[0]
        if isinstance(arguments, CodexArgumentsSource):
            arguments = argv[2:] if Path(argv[0]).name in {"node", "nodejs"} else argv[1:]
        pid = process.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValueError("Codex launch has no process id")
        observed = native_process(pid, "codex")
        session_id, started, environment = observed.identity.id, observed.started, observed.environment
        cwd = json_text(process.get("cwd"), "Codex launch cwd")
        if not Path(cwd).is_absolute():
            raise ValueError("Codex launch working folder is unavailable")
        entry = pane_launch.launch(pane, session_id, cwd, legacy_identity.without_legacy_identity(environment),
                                   ["codex", *arguments], kind="codex")
        entry["pid"], entry["proc_start"] = pid, started
        return entry

    def start_agent(self, name: str, pane: str, kind: str, agent_arguments: list[str],
                    opening: OpeningPrompt | NoOpeningPrompt, timeout_ms: int) -> PromptSubmitOutcome | PromptSubmissionNotRequired:
        """Start once, then prove the opening prompt arrived as this session's user turn."""
        forget_pane_evidence(self.session, pane)
        _check_agent_arguments(kind, agent_arguments)
        _timeout(timeout_ms)
        launched_at = datetime.now(timezone.utc)
        deadline = time.monotonic() + operation_deadline.remaining(timeout_ms / 1000)
        attempt_timeout_ms = min(timeout_ms, _AGENT_START_TIMEOUT_MAXIMUM_MS)
        try:
            while True:
                result = self.start_command(
                    self.argv("agent", "start", _agent_name(name), "--kind", kind, "--pane", pane,
                              "--timeout", str(attempt_timeout_ms), "--", *agent_arguments),
                    kind, pane, max(0, deadline - time.monotonic()),
                )
                try:
                    require_success(result, f"start agent {name}")
                except HerdrFailure as error:
                    if (error.code != "agent_pane_busy"
                            or deadline <= time.monotonic()):
                        raise
                    time.sleep(operation_deadline.remaining(min(0.1, deadline - time.monotonic())))
                    attempt_timeout_ms = min(int((deadline - time.monotonic()) * 1000),
                                             _AGENT_START_TIMEOUT_MAXIMUM_MS)
                    continue
                break
        except HerdrFailure as error:
            screen = ""
            if error.code == "agent_not_ready":
                try:
                    screen = self.run("pane", "read", pane, "--source", "visible")
                except (HerdrFailure, OSError, subprocess.SubprocessError) as screen_error:
                    screen = f"visible screen is unavailable: {screen_error}"
            print(f"herdr {name}:{pane}: {error}; session retained", file=sys.stderr)
            if screen:
                print(screen, file=sys.stderr)
            return AgentNotStarted(str(error))
        if kind == "codex":
            try:
                entry = self.codex_launch(pane, agent_arguments)
                entry["name"] = name
                directory = pane_launch.record_directory()
                pane_launch.write(directory / f"{self.session}.json", entry, sources=tuple(directory.glob("*.json")))
            except (CodexPaneStarting, CodexSessionPending) as error:
                if isinstance(error, CodexPaneStarting) or isinstance(opening, OpeningPrompt):
                    print(f"herdr {name}:{pane}: pane is starting; pane retained", file=sys.stderr)
                return (PromptNotSubmitted("pane is starting; prompt retained", never_typed=True)
                        if isinstance(opening, OpeningPrompt) else PromptSubmissionNotRequired())
            except (HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
                print(f"anomaly: herdr {name}:{pane}: legacy launch publication is unavailable: {error}; launch continues", file=sys.stderr)
        if isinstance(opening, OpeningPrompt):
            if isinstance(opening.transcript, SessionTranscript):
                transcript = opening.transcript
                if transcript.session_id and not check_target(
                        transcript.session_id, action="keys", place=HerdrPlace(self.session, pane)):
                    return PromptNotSubmitted("prompt target is unknown; session retained")
                try:
                    identity = self.native_session(pane, kind, max(0.1, deadline - time.monotonic()))
                except (HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
                    print(f"herdr {name}:{pane}: native session is unavailable: {error}; pane retained", file=sys.stderr)
                    return PromptNotSubmitted("native session is unavailable; pane retained")
                transcript = SessionTranscript(transcript.config_dir, identity)
                try:
                    _ = opening.before_prompt(transcript)
                except Exception as error:
                    print(f"herdr {transcript.session_id}: staff publication failed: {error}", file=sys.stderr)
                    return PromptNotSubmitted(f"staff publication failed: {error}")
                opening = OpeningPrompt(opening.text, transcript)
            return self.submit_opening_prompt(pane, opening, launched_at,
                                              max(0, int((deadline - time.monotonic()) * 1000)))
        return PromptSubmissionNotRequired()

    def start_command(self, argv: list[str], kind: str, pane: str,
                      timeout: float) -> subprocess.CompletedProcess[str]:
        """Observe a submitted agent while its CLI waits for interactive readiness."""
        deadline = time.monotonic() + operation_deadline.remaining(timeout)
        shell: PaneShell | PaneShellUnknown = PaneShellUnknown()
        agent_seen = False
        tracked: set[AgentProcessIdentity] = set()
        # Locate the shell before submitting the command, so a short-lived agent
        # cannot run entirely during the first herdr observation.
        try:
            shell = self.startup_processes(pane, kind, min(0.5, timeout)).shell
        except (HerdrFailure, subprocess.TimeoutExpired):
            pass
        next_observation = time.monotonic() + 0.5
        operation_deadline.check()
        try:
            process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                       env=unmarked_environment())
        except OSError as error:
            raise HerdrFailure(f"{argv[0]}: {error}") from error
        def stopped(reason: BaseException) -> Never:
            process.kill()
            _ = process.communicate()
            raise reason

        with process:
            try:
                while True:
                    # A completed CLI result wins over a deadline or concurrent exit.
                    if process.poll() is not None:
                        stdout, stderr = process.communicate()
                        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(argv, timeout)
                    try:
                        stdout, stderr = process.communicate(timeout=min(0.05, remaining))
                        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
                    except subprocess.TimeoutExpired:
                        pass
                    activity: AgentProcessPresent | PaneAtShell | PaneRunning | PaneProcessesUnreadable = PaneProcessesUnreadable()
                    if isinstance(shell, PaneShell):
                        activity = _startup_processes(shell, kind, tracked, _ProcessReadDeadline(deadline))
                    remaining = deadline - time.monotonic()
                    if (isinstance(activity, PaneProcessesUnreadable)
                            and time.monotonic() >= next_observation and remaining > 0):
                        try:
                            observation = self.startup_processes(pane, kind, min(0.5, remaining))
                            shell, activity = observation.shell, observation.activity
                        except (HerdrFailure, subprocess.TimeoutExpired):
                            pass
                        next_observation = time.monotonic() + 0.5
                    if isinstance(activity, AgentProcessPresent):
                        for identity in activity.identities:
                            _track_startup_identity(tracked, identity)
                    agent_seen = agent_seen or isinstance(activity, AgentProcessPresent)
                    if agent_seen and not tracked and isinstance(activity, PaneAtShell):
                        if process.poll() is not None:
                            stdout, stderr = process.communicate()
                            return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
                        raise AgentExited(self.session, pane)
            except BaseException as error:
                # Only the waiting CLI is ours; this never signals the pane's process.
                stopped(error)

    def startup_processes(self, pane: str, kind: str, timeout: float) -> PaneStartupObservation:
        deadline = _ProcessReadDeadline(time.monotonic() + timeout)
        forget_pane_evidence(self.session, pane)
        result = run_program(self.argv("pane", "process-info", "--pane", pane), timeout=timeout)
        require_success(result, "startup pane processes")
        payload = json_object(decode_object(result.stdout, "startup pane processes").get("result"), "startup pane result")
        info = json_object(payload.get("process_info"), "startup pane process info")
        shell, foreground = _pane_process_ids(info, "startup pane process info")
        commands: list[str] = []
        agent_present = False
        identities: list[AgentProcessIdentity] = []
        agent_pids: list[int] = []
        for value in json_items(info.get("foreground_processes"), "startup foreground processes"):
            process = json_object(value, "startup foreground process")
            arguments = _foreground_arguments(process)
            if not arguments:
                continue
            commands.append(json_text(process.get("name"), "startup process name"))
            if _is_agent_process(arguments, kind):
                agent_present = True
                pid = process.get("pid")
                if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
                    agent_pids.append(pid)
        if agent_pids:
            try:
                reader = _startup_process_reader(deadline)
                for pid in agent_pids:
                    try:
                        identities.append(reader.identity(pid))
                    except (_StartupProcessGone, HerdrFailure, OSError, ValueError, IndexError):
                        pass
            except subprocess.TimeoutExpired:
                raise
            except (HerdrFailure, OSError, ValueError, subprocess.SubprocessError):
                pass
        activity = AgentProcessPresent(tuple(identities)) if agent_present else PaneAtShell() if foreground == shell else PaneRunning(tuple(commands))
        return PaneStartupObservation(PaneShell(shell), activity)

    def native_session(self, pane: str, kind: str, timeout: float, *,
                       pane_row: Mapping[str, object] | PaneNotRead = PANE_NOT_READ) -> str:
        """Prove the backend identity from the exact foreground process."""
        try:
            return self.native_pane_process(pane, kind, timeout, pane_row=pane_row).current_identity
        except ValueError as error:
            raise HerdrFailure(str(error)) from error

    def pane_codex_pid(self, pane: str) -> int:
        """Return the sole foreground Codex PID, or zero for other foreground work."""
        info = json_object(self.result("pane", "process-info", "--pane", pane).get("process_info"),
                           "pane process info")
        foreground = _codex_foreground(info)
        if len(foreground) != 1:
            return 0
        pid = foreground[0].get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise HerdrFailure("native process pid is unavailable")
        return pid

    def native_pane_process(self, pane: str, kind: str, timeout: float, *,
                            pane_row: Mapping[str, object] | PaneNotRead = PANE_NOT_READ) -> NativeAgentProcess:
        """Read the identity and launch environment from one proved foreground process."""
        forget_pane_evidence(self.session, pane)
        result = run_program(self.argv("pane", "process-info", "--pane", pane), timeout=timeout)
        require_success(result, "native pane processes")
        payload = json_object(decode_object(result.stdout, "native pane processes").get("result"), "native pane result")
        info = json_object(payload.get("process_info"), "native pane process info")
        if isinstance(pane_row, PaneNotRead) and kind == "codex" and codex_without_session(info):
            try:
                row = self.pane_row(pane)
                if not isinstance(row, NoPane):
                    pane_row = row
            except HerdrFailure:
                pass
        return pane_native_process(info, kind, {} if isinstance(pane_row, PaneNotRead) else pane_row)

    def pane_agent_process(self, pane: str, terminal: str) -> AgentProcessIdentity:
        """Read the foreground agent identity, then confirm its terminal."""
        forget_pane_evidence(self.session, pane)
        info = json_object(self.result("pane", "process-info", "--pane", pane).get("process_info"),
                           "pane process info")
        processes: list[AgentProcessIdentity] = []
        reader = _startup_process_reader(PROGRAM_TIMEOUT_SECONDS)
        for value in json_items(info.get("foreground_processes"), "foreground processes"):
            process = json_object(value, "foreground process")
            arguments = _foreground_arguments(process)
            if any(_is_agent_process(arguments, kind) for kind in ("claude", "codex")):
                pid = cast(int, process["pid"])
                try:
                    processes.append(reader.identity(pid))
                except _StartupProcessGone as error:
                    raise HerdrFailure(f"{pane}: {error}") from error
        if len(processes) != 1:
            raise HerdrFailure(f"{pane}: expected one foreground agent process")
        row = self.pane_row(pane)
        if isinstance(row, NoPane) or row.get("terminal_id") != terminal:
            raise HerdrFailure(f"{pane}: terminal changed before process selection", "wrong_target")
        return processes[0]

    def readiness(self, pane: str) -> WorkerReadiness:
        agent = json_object(self.result("agent", "get", pane).get("agent"), "herdr agent get")
        status = AgentStatus.decode(agent.get("agent_status"))
        ready = agent.get("interactive_ready", True)
        if not isinstance(ready, bool):
            print(f"herdr {self.session}:{pane}: readiness is unknown", file=sys.stderr)
            ready = False
        return {"agent_status": status, "interactive_ready": ready}

    def prompt_input_state(self, pane: str) -> PromptInputReady | PromptInputNotReady:
        ready, idle = False, False
        try:
            observation = self.readiness(pane)
            status = observation["agent_status"]
            idle = status.idle
            ready = observation["interactive_ready"] and (status == AgentStatus.WORKING or idle)
        except (OSError, subprocess.SubprocessError, HerdrFailure):
            pass
        if not ready:
            return PromptInputNotReady("destination session is still loading")
        raw = self.run("pane", "read", pane, "--source", "visible", "--format", "ansi")
        screen = _prompt_screen(raw)
        composer = _composer_input(raw)
        if isinstance(composer, ComposerInput):
            if composer.text.strip():
                return PromptInputNotReady("destination composer holds a draft", screen)
            return PromptInputReady(screen, idle)
        return PromptInputNotReady("destination has no empty composer", screen)

    def wait_for_prompt_input(self, pane: str, timeout_ms: int) -> PromptInputReady | PromptInputTimedOut:
        deadline = time.monotonic() + operation_deadline.remaining(timeout_ms / 1000)
        screen: str | PromptScreenNotRead = PromptScreenNotRead()
        while time.monotonic() < deadline:
            state = self.prompt_input_state(pane)
            if isinstance(state.screen, str):
                screen = state.screen
            if isinstance(state, PromptInputReady):
                return state
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.1, remaining))
        return PromptInputTimedOut(screen)

    def composer(self, pane: str) -> ComposerInput | NoComposer:
        """Read the visible input region."""
        return _composer_input(self.run("pane", "read", pane, "--source", "visible", "--format", "ansi"))

    def prompt_in_input(self, pane: str, text: str) -> bool:
        """Read only the visible composer's last input region, never transcript scrollback."""
        composer = self.composer(pane)
        return isinstance(composer, ComposerInput) and composer.contains(text)

    def prompt_input_empty(self, pane: str) -> bool:
        composer = self.composer(pane)
        return isinstance(composer, ComposerInput) and not composer.text.strip()

    def _wait_for_submit(self, cursor: TranscriptCursor, text: str, deadline: float) -> PromptSubmitOutcome:
        while True:
            result = cursor.submitted_prompt(text)
            if isinstance(result, PromptSubmitted):
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return result
            time.sleep(min(0.1, remaining))

    def submit_opening_prompt(self, pane: str, opening: OpeningPrompt, launched_at: datetime,
                              timeout_ms: int) -> PromptSubmitOutcome:
        if timeout_ms <= 0 or not opening.text.strip():
            return PromptNotSubmitted("opening prompt deadline elapsed or text is empty")
        opening = OpeningPrompt(script_prompt(opening.text), opening.transcript, opening.before_prompt, opening.cursor)
        deadline = time.monotonic() + operation_deadline.remaining(timeout_ms / 1000)
        cursor = opening.cursor
        if not isinstance(cursor, TranscriptCursor) and isinstance(opening.transcript, SessionTranscript):
            cursor = TranscriptCursor(opening.transcript, launched_at)
        screen: str | PromptScreenNotRead = PromptScreenNotRead()
        typings, enters, rejections = 0, 0, 0
        recipient: PromptRecipientProcess | PromptRecipientUnavailable = PromptRecipientUnavailable("transcript identity not supplied")
        current = opening.text

        def failure(reason: str) -> PromptSubmitOutcome:
            if typings and isinstance(cursor, TranscriptCursor):
                proof = cursor.submitted_prompt(current)
                if not isinstance(proof, PromptSubmitted) and current != opening.text:
                    proof = cursor.submitted_prompt(opening.text)
                if isinstance(proof, PromptSubmitted):
                    if (isinstance(recipient, PromptRecipientProcess)
                            and prompt_recipient_process(cursor.transcript.session_id) != recipient):
                        reason = "prompt recipient changed during submission"
                    else:
                        return PromptSubmitted(proof.at, recipient)
            detail = f"{reason}; prompt typed {typings} times" if typings else reason
            visible = "\n".join(screen.splitlines()[-40:]) if isinstance(screen, str) else screen
            return PromptNotSubmitted(detail, visible, never_typed=typings == 0)

        def deadline_failure() -> PromptSubmitOutcome:
            reason = "opening prompt deadline elapsed"
            if isinstance(cursor, TranscriptCursor) and isinstance(cursor.readability, TranscriptUnreadable):
                reason += ": transcript unreadable"
            return failure(reason)

        try:
            ready = self.wait_for_prompt_input(pane, timeout_ms)
            screen = ready.screen
            if isinstance(ready, PromptInputTimedOut):
                if isinstance(cursor, TranscriptCursor):
                    _ = cursor.read_turns()
                if isinstance(cursor, TranscriptCursor) and isinstance(cursor.readability, TranscriptUnreadable):
                    return deadline_failure()
                return failure("destination never became ready for input")
            recipient = (prompt_recipient_process(opening.transcript.session_id)
                         if isinstance(opening.transcript, SessionTranscript)
                         else PromptRecipientUnavailable("transcript identity not supplied"))

            def bound_outcome(outcome: PromptSubmitted) -> PromptSubmitOutcome:
                if (isinstance(recipient, PromptRecipientProcess)
                        and isinstance(opening.transcript, SessionTranscript)
                        and prompt_recipient_process(opening.transcript.session_id) != recipient):
                    return PromptNotSubmitted("prompt recipient changed during submission")
                return PromptSubmitted(outcome.at, recipient)

            # Readiness can take time: a matching turn may have arrived meanwhile.
            if isinstance(cursor, TranscriptCursor):
                existing = cursor.submitted_prompt(opening.text)
                if isinstance(existing, PromptSubmitted):
                    return bound_outcome(existing)
                while isinstance(cursor.readability, TranscriptUnreadable):
                    result = self._wait_for_submit(cursor, opening.text, min(deadline, time.monotonic() + 5))
                    if isinstance(result, PromptSubmitted):
                        return bound_outcome(result)
                    screen = _prompt_screen(self.run("pane", "read", pane, "--source", "visible"))
                    if time.monotonic() >= deadline:
                        return deadline_failure()
                    if isinstance(cursor.readability, TranscriptReadable):
                        ready = self.wait_for_prompt_input(pane,
                            max(0, int((deadline - time.monotonic()) * 1000)))
                        if isinstance(ready.screen, str):
                            screen = ready.screen
                        if isinstance(ready, PromptInputTimedOut):
                            return deadline_failure()
                        existing = cursor.submitted_prompt(opening.text)
                        if isinstance(existing, PromptSubmitted):
                            return bound_outcome(existing)
            while time.monotonic() < deadline:
                typings += 1
                try:
                    if not isinstance(cursor, TranscriptCursor):
                        return failure("caller did not identify the transcript; submit is unproven")
                    if not check_target(cursor.transcript.session_id, action="keys",
                                        place=HerdrPlace(self.session, pane), text=current,
                                        recipient=recipient if typings > 1 else _TARGET_RECIPIENT_UNKNOWN,
                                        pane_info=self.pane_row(pane), client=self):
                        return failure("prompt target is unknown; session retained")
                    proof = cursor.submitted_prompt(current)
                    if isinstance(proof, PromptSubmitted):
                        return bound_outcome(proof)
                    if isinstance(cursor.readability, TranscriptUnreadable):
                        result = self._wait_for_submit(cursor, current, deadline)
                        return bound_outcome(result) if isinstance(result, PromptSubmitted) else deadline_failure()
                    self._prompt(pane, current)
                except HerdrFailure as error:
                    if error.code != "agent_prompt_stalled":
                        if isinstance(cursor, TranscriptCursor):
                            proof = cursor.submitted_prompt(current)
                            if isinstance(proof, PromptSubmitted):
                                return bound_outcome(proof)
                        return failure(str(error))
                last_input = time.monotonic()
                if not isinstance(cursor, TranscriptCursor):
                    return failure("caller did not identify the transcript; submit is unproven")
                while True:
                    result = self._wait_for_submit(cursor, current, min(deadline, time.monotonic() + 5))
                    if isinstance(result, PromptSubmitted):
                        return bound_outcome(result)
                    rejected = cursor.command_rejections(current) if current == opening.text else rejections
                    if rejected > rejections:
                        # Retry the command once while Claude loads its command list.
                        rejections = rejected
                        if rejections > 1:
                            return failure(f"Claude answered Unknown command: {_slash_command(current)}")
                        if (remaining := deadline - time.monotonic()) > 0:
                            time.sleep(min(PROMPT_SETTLE_SECONDS, remaining))
                        if time.monotonic() >= deadline:
                            return deadline_failure()
                        if not self.prompt_input_empty(pane):
                            return failure("destination input contains other text")
                        break
                    raw = self.run("pane", "read", pane, "--source", "visible", "--format", "ansi")
                    screen = _prompt_screen(raw)
                    composer = _composer_input(raw)
                    if time.monotonic() >= deadline:
                        return deadline_failure()
                    if isinstance(cursor.readability, TranscriptUnreadable):
                        continue
                    if isinstance(composer, NoComposer):
                        continue
                    if composer.contains(current):
                        if enters == 3:
                            return failure("no user turn after three Enter retries")
                        if not check_target(cursor.transcript.session_id, action="keys",
                                            place=HerdrPlace(self.session, pane), text=current, recipient=recipient,
                                            pane_info=self.pane_row(pane), client=self):
                            return failure("prompt target is unknown; session retained")
                        # A turn can land during the screen/process reads too.
                        proof = cursor.submitted_prompt(current)
                        if isinstance(proof, PromptSubmitted):
                            return bound_outcome(proof)
                        if isinstance(cursor.readability, TranscriptUnreadable):
                            continue
                        if time.monotonic() >= deadline:
                            return deadline_failure()
                        self._send_keys(pane, "Enter")
                        enters += 1
                        last_input = time.monotonic()
                        continue
                    if composer.text.strip():
                        return failure("destination input contains other text")
                    # Working can mean queued input, even with an empty composer.
                    # Retype only when the recipient is idle after the settle time.
                    observation = self.readiness(pane)
                    if (not observation["agent_status"].idle
                            or time.monotonic() - last_input < PROMPT_SETTLE_SECONDS):
                        continue
                    if typings == 3:
                        return failure("no user turn after three typings")
                    ready = self.wait_for_prompt_input(pane,
                        max(0, int((deadline - time.monotonic()) * 1000)))
                    if isinstance(ready.screen, str):
                        screen = ready.screen
                    if isinstance(ready, PromptInputTimedOut):
                        if isinstance(cursor.readability, TranscriptUnreadable):
                            return deadline_failure()
                        return failure("destination never became ready for re-type")
                    if not ready.idle:
                        continue
                    break
            return deadline_failure()
        except (OSError, subprocess.SubprocessError, HerdrFailure) as error:
            return failure(str(error))

    def prompt(self, pane: str, text: str, *, target: str,
               proof: SubmitUnproved | SubmitStarts = SUBMIT_UNPROVED,
               on_submission: Callable[[], object] = lambda: None) -> None:
        """Send input only to the selected session."""
        if not target:
            raise HerdrFailure(f"{pane}: would send keys without a named target session")
        row = self.pane_row(pane)
        if not check_target(target, action="keys", place=HerdrPlace(self.session, pane), text=text,
                            pane_info=row, client=self):
            raise HerdrFailure(f"{pane}: would send keys to an unproved target instead of session {target}")
        _ = on_submission()
        self._prompt(pane, text, proof=proof)

    def prompt_terminal(self, pane: str, terminal: str, text: str) -> None:
        """Send input to the selected pane terminal."""
        row = self.pane_row(pane)
        if isinstance(row, NoPane) or row.get("terminal_id") != terminal:
            raise HerdrFailure(f"{pane}: terminal changed before prompt")
        self._prompt(pane, text)

    def _prompt(self, pane: str, text: str, *,
                proof: SubmitUnproved | SubmitStarts = SUBMIT_UNPROVED) -> None:
        from scripts.lib import script_prompts

        text = script_prompt(text)
        if text.lstrip().startswith("/"):
            script_prompts.record(pane, text)
        arguments = ["agent", "prompt", pane, text]
        if isinstance(proof, SubmitStarts):
            arguments += ["--wait", "--until", "working", "--until", "blocked", "--timeout", str(proof.timeout_ms)]
        _ = self.run(*arguments)

    def quit_agent(self, pane: str, terminal: str, target: str, timeout: float) -> None:
        """Quit the selected conversation and wait for its shell or pane closure."""
        deadline = time.monotonic() + timeout
        row = self.pane_row(pane)
        if isinstance(row, NoPane) or row.get("terminal_id") != terminal:
            raise HerdrFailure(f"{pane}: terminal changed before quit")
        self.prompt(pane, "/quit", target=target)
        while True:
            if isinstance(self.pane_row(pane), NoPane):
                return
            try:
                if isinstance(self.pane_activity(pane), PaneAtShell):
                    return
            except HerdrFailure as error:
                if error.code == "pane_not_found":
                    return
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HerdrFailure(f"{pane}: agent did not return to its shell")
            time.sleep(min(0.1, remaining))

    def wait_turn_end(self, pane: str, timeout_ms: int) -> ObservedAgentState | WaitTimedOut:
        _timeout(timeout_ms)
        try:
            result = run_program(self.argv("agent", "wait", pane, "--until", "done", "--until", "idle",
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
        status = AgentStatus.decode(agent.get("agent_status"))
        if status == AgentStatus.BLOCKED:
            raise AgentBlocked(f"herdr {self.session}:{pane}: agent needs input")
        return ObservedAgentState(status)

    def read(self, pane: str, lines: int, *, joined: bool, ansi: bool, deadline: float = math.inf) -> str:
        arguments = ["pane", "read", pane, "--source", "recent-unwrapped" if joined else "recent",
                     "--lines", str(lines)]
        if ansi:
            arguments.extend(["--format", "ansi"])
        return self.run(*arguments, deadline=deadline)

    def send_keys(self, pane: str, *keys: str, target: str, expected_text: str = "") -> None:
        if not target:
            raise HerdrFailure(f"{pane}: would send keys without a named target session")
        row = self.pane_row(pane)
        if not check_target(target, action="keys", place=HerdrPlace(self.session, pane),
                            pane_info=row, client=self, text=expected_text):
            raise HerdrFailure(f"{pane}: would send keys to an unproved target instead of session {target}")
        self._send_keys(pane, *keys)

    def answer_question(self, pane: str, text: str, *, target: str) -> None:
        """Answer a Codex question through the selected session's input box."""
        if not target:
            raise HerdrFailure(f"{pane}: would send keys without a named target session")
        for index, key in enumerate(("shift+left", "enter", "enter")):
            expected = "" if index == 0 else text
            if not check_target(target, action="keys", place=HerdrPlace(self.session, pane), text=expected,
                                pane_info=self.pane_row(pane), client=self):
                raise HerdrFailure(f"{pane}: would send keys to an unproved target instead of session {target}")
            self._send_keys(pane, key)
            if index == 0:
                _ = self.run("pane", "send-text", pane, text)

    def _send_keys(self, pane: str, *keys: str) -> None:
        _ = self.run("pane", "send-keys", pane, *keys)

    def close_pane(self, pane: str, *, target: str) -> bool:
        """Close only the pane that runs the selected session, or its empty shell."""
        if not check_target(target, action="close", place=HerdrPlace(self.session, pane),
                            pane_info=self.pane_row(pane), client=self):
            return False
        return self._close_pane(pane)

    def _close_pane(self, pane: str) -> bool:
        try:
            _ = self.run("pane", "close", pane)
        except HerdrFailure as error:
            if error.code != "pane_not_found":
                print(f"herdr {self.session}:{pane}: closure is incomplete: {error}", file=sys.stderr)
                return False
        return True

    def agents(self, *, deadline: float = math.inf) -> list[AgentOccupant]:
        agents: list[AgentOccupant] = []
        for agent in self.listed("agent", deadline=deadline):
            kind = agent.get("agent")
            if kind is not None and not isinstance(kind, str):
                raise HerdrFailure("herdr agent kind: expected text or null")
            session: ReportedAgentSession | NoReportedAgentSession = NoReportedAgentSession()
            if "agent_session" in agent:
                report = json_object(agent["agent_session"], "herdr agent session")
                if json_text(report.get("kind"), "herdr agent session kind") == "id":
                    session = ReportedAgentSession(json_text(report.get("value"), "herdr agent session id"))
            agents.append({
                "pane_id": json_text(agent.get("pane_id"), "herdr agent pane id"),
                "agent": kind,
                "agent_status": AgentStatus.decode(agent.get("agent_status")),
                "agent_session": session,
            })
        return agents


def _pane_process_ids(info: dict[str, object], context: str) -> tuple[int, int]:
    values = (info.get("shell_pid"), info.get("foreground_process_group_id"))
    if any(isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 for pid in values):
        raise HerdrFailure(f"{context}: expected positive process ids")
    return cast(tuple[int, int], values)


def pane_session(row: Mapping[str, object]) -> str:
    """Read the native session recorded in a pane's metadata."""
    report = row.get("agent_session")
    session = cast(dict[str, object], report).get("value") if isinstance(report, dict) else row.get("session_id")
    if session is not None and not isinstance(session, str):
        print("herdr: pane session is unreadable", file=sys.stderr)
    return session if isinstance(session, str) else ""


def _codex_foreground(info: dict[str, object]) -> list[dict[str, object]]:
    """Select a sole Codex process after excluding the turn-end hook."""
    foreground: list[dict[str, object]] = []
    for value in json_items(info.get("foreground_processes"), "native foreground processes"):
        process = json_object(value, "native foreground process")
        arguments = _foreground_arguments(process)
        program = (arguments[1] if len(arguments) > 1 and Path(arguments[0]).name.startswith("python")
                   else arguments[0] if arguments else "")
        if Path(program).parts[-3:] != ("scripts", "herdr", "turn_end.py"):
            foreground.append(process)
    return foreground if len(foreground) == 1 and is_codex_process(_foreground_arguments(foreground[0])) else []


def codex_without_session(info: dict[str, object]) -> bool:
    """Identify one foreground Codex that can supply a session report."""
    return len(_codex_foreground(info)) == 1


def pane_native_process(info: dict[str, object], kind: str,
                         pane: Mapping[str, object]) -> NativeAgentProcess:
    foreground = json_items(info.get("foreground_processes"), "native foreground processes")
    reported = pane_session(pane) if kind == "codex" and codex_without_session(info) else ""
    processes: list[NativeAgentProcess] = []
    for value in foreground:
        process = json_object(value, "native foreground process")
        arguments = _foreground_arguments(process)
        if not _is_agent_process(arguments, kind):
            continue
        pid = process.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise HerdrFailure("native process pid is unavailable")
        try:
            observed = native_process(pid, kind, reported) if kind == "codex" else native_process(pid, kind)
        except CodexPaneStarting as error:
            report = pane.get("agent_session")
            value = cast(dict[str, object], report).get("value") if isinstance(report, dict) else pane.get("session_id")
            if (not codex_without_session(info) or report is not None and not isinstance(report, dict)
                    or value is not None and value != ""):
                raise HerdrFailure("Codex launch identity is unavailable") from error
            raise
        except (OSError, ValueError) as error:
            raise HerdrFailure(str(error)) from error
        if (kind == "codex" and codex_without_session(info) and "pane_id" in pane
                and not reported and isinstance(observed.identity, LaunchArgument)):
            observed = NativeAgentProcess(UnreportedResume(observed.identity.id), observed.started, observed.environment)
        processes.append(observed)
    if len(processes) != 1:
        raise HerdrFailure(f"expected one foreground {kind} process, found {len(processes)}")
    return processes[0]


def _pane_sessions(info: dict[str, object], pane: Mapping[str, object]) -> set[str]:
    kinds: set[str] = set()
    for value in json_items(info.get("foreground_processes"), "foreground processes"):
        process = json_object(value, "foreground process")
        arguments = _foreground_arguments(process)
        if not arguments and Path(str(process.get("name", ""))).name in ("claude", "codex"):
            raise HerdrFailure("agent argv is unavailable")
        kinds.update(kind for kind in ("claude", "codex") if _is_agent_process(arguments, kind))
    return {pane_native_process(info, kind, pane).current_identity for kind in kinds}


_TARGET_PLACE_UNKNOWN = NotInHerdr()
_TARGET_SHELL_UNKNOWN = PaneShellUnknown()
_TARGET_PANE_UNKNOWN = NoPane("")
_TARGET_RECIPIENT_UNKNOWN = PromptRecipientUnavailable("recipient was not recorded")


def check_target(target: str | AgentProcessIdentity, *, action: Literal["signal", "keys", "close"],
                 place: HerdrPlace | NotInHerdr = _TARGET_PLACE_UNKNOWN,
                 shell: PaneShell | PaneShellUnknown = _TARGET_SHELL_UNKNOWN,
                 terminal: str = "", text: str = "",
                 pane_info: PaneIdentity | NoPane = _TARGET_PANE_UNKNOWN,
                 recipient: PromptRecipientProcess | PromptRecipientUnavailable = _TARGET_RECIPIENT_UNKNOWN,
                 client: Herdr | NotInHerdr = _TARGET_PLACE_UNKNOWN) -> bool:
    """Protect the selected session before a signal, input, or pane closure.

    Unknown evidence preserves the resource without stopping independent actions.
    """
    verb = {"signal": "signal", "keys": "send keys to", "close": "close"}[action]
    name = f"pid {target.pid}" if isinstance(target, AgentProcessIdentity) else target or "shell"
    if isinstance(target, AgentProcessIdentity):
        if isinstance(shell, PaneShell) and target.pid == shell.pid:
            raise HerdrFailure(f"would {verb} the shell: agent identity matches the shell for session {name}", "wrong_target")
        if isinstance(target.start_ticks, ProcessGenerationUnavailable):
            print(f"herdr {name}: process generation is unknown; process retained", file=sys.stderr)
            return False
        try:
            current = _startup_process_reader(PROGRAM_TIMEOUT_SECONDS).identity(target.pid)
        except (_StartupProcessGone, FileNotFoundError, ProcessLookupError):
            return False
        except (HerdrFailure, OSError, ValueError, IndexError, subprocess.SubprocessError) as error:
            print(f"herdr {name}: process identity is unknown: {error}; process retained", file=sys.stderr)
            return False
        if isinstance(current.start_ticks, ProcessGenerationUnavailable):
            print(f"herdr {name}: process generation is unknown; process retained", file=sys.stderr)
            return False
        if current != target:
            raise HerdrFailure(f"would {verb} a reused process that does not run session {name}", "wrong_target")
        return True
    if isinstance(place, NotInHerdr):
        print(f"herdr {name}: pane is unknown; target retained", file=sys.stderr)
        return False
    client = Herdr(place.session) if isinstance(client, NotInHerdr) else client
    try:
        row = client.pane_row(place.pane) if pane_info is _TARGET_PANE_UNKNOWN else pane_info
        if isinstance(row, NoPane):
            print(f"herdr {name}: pane {place.pane} is absent", file=sys.stderr)
            return False
        actual_terminal = row.get("terminal_id", "")
        if not actual_terminal:
            print(f"herdr {name}: terminal is unknown; target retained", file=sys.stderr)
            return False
        if terminal and actual_terminal != terminal:
            raise HerdrFailure(f"terminal identity changed; would {verb} another terminal instead of session {name}", "wrong_target")
        forget_pane_evidence(place.session, place.pane)
        info = json_object(client.result("pane", "process-info", "--pane", place.pane).get("process_info"),
                           "target process info")
        shell_pid, foreground = _pane_process_ids(info, "target process info")
        if foreground == shell_pid:
            descendants = _startup_processes(PaneShell(shell_pid), "claude", set())
            if isinstance(descendants, PaneRunning) and not descendants.commands:
                print(f"herdr {name}: foreground process is unknown; pane retained", file=sys.stderr)
                return False
            if isinstance(descendants, (AgentProcessPresent, PaneRunning)):
                commands = ", ".join(descendants.commands) if isinstance(descendants, PaneRunning) else "claude"
                raise HerdrFailure(f"pane runs other work: {commands}; would {verb} other live work instead of session {name}", "wrong_target")
            if isinstance(descendants, PaneProcessesUnreadable):
                print(f"herdr {name}: descendants are unknown; pane retained", file=sys.stderr)
                return False
            return action != "keys" or not target
        if not target:
            raise HerdrFailure(f"would {verb} a pane with other live work instead of the selected shell", "wrong_target")
        observed = _pane_sessions(info, row)
        if not observed:
            print(f"herdr {name}: native session is unknown; target retained", file=sys.stderr)
            return False
        if observed != {target}:
            raise HerdrFailure(f"would {verb} a process that does not run session {target}", "wrong_target")
        if action == "keys":
            if isinstance(recipient, PromptRecipientProcess):
                current_recipient = prompt_recipient_process(target)
                if isinstance(current_recipient, PromptRecipientUnavailable):
                    print(f"herdr {name}: recipient is unknown; input retained", file=sys.stderr)
                    return False
                if current_recipient != recipient:
                    raise HerdrFailure(f"recipient changed; would {verb} another process in session {target}", "wrong_target")
            composer = client.composer(place.pane)
            if isinstance(composer, ComposerInput) and composer.text.strip() and not composer.contains(text):
                raise HerdrFailure(f"would overwrite unsaved input in session {target}", "wrong_target")
        return True
    except CodexPaneStarting:
        print(f"herdr {name}: pane is starting; target retained", file=sys.stderr)
        return False
    except (HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
        if isinstance(error, HerdrFailure) and error.code == "wrong_target":
            raise
        print(f"herdr {name}: target evidence is unknown: {error}; target retained", file=sys.stderr)
        return False


def claude_trust(folder: Path, claude_json: Path) -> Trusted | Untrusted | UnreadableTrustFile:
    """Check exact folder, repository-root and main-checkout keys, never ancestors."""
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
    keys = claude_trust_keys(folder)
    for key in keys:
        if configuration["projects"].get(str(key), {"hasTrustDialogAccepted": False})["hasTrustDialogAccepted"]:
            return Trusted(folder, claude_json, key)
    return Untrusted(folder, claude_json, keys)


def claude_trust_keys(folder: Path) -> tuple[Path, ...]:
    """The exact folder, repository root, and main checkout that can grant trust."""
    folder = folder.resolve()
    return tuple(dict.fromkeys((folder, repository_root(folder), repository_trust_key(folder))))


def copy_claude_trust(folder: Path, source: Path, target: Path) -> Trusted | Untrusted | UnreadableTrustFile:
    """Copy only the source account's trust for this folder into the private target file."""
    present = claude_trust(folder, target)
    if not (isinstance(present, Untrusted)
            or isinstance(present, UnreadableTrustFile) and present.error_class == "FileNotFoundError"):
        return present
    granted = claude_trust(folder, source)
    if not isinstance(granted, Trusted):
        return granted
    return _write_claude_trust(folder, target, granted.project_key)


def _write_claude_trust(folder: Path, target: Path, project_key: Path) -> Trusted | UnreadableTrustFile:
    """Lock the account file through its read and atomic trust update."""
    from scripts.lib import file_lock

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with file_lock.claude_config_lock(target):
            try:
                raw = cast(object, json.loads(target.read_text(encoding="utf-8")))
            except FileNotFoundError:
                raw = {}
            if not isinstance(raw, dict):
                raise ValueError()
            settings = cast(dict[str, object], raw)
            projects = settings.setdefault("projects", {})
            if not isinstance(projects, dict):
                raise ValueError()
            entry = cast(dict[str, object], projects).setdefault(str(project_key), {})
            if not isinstance(entry, dict):
                raise ValueError()
            cast(dict[str, object], entry)["hasTrustDialogAccepted"] = True
            if target.exists():
                target.chmod(0o600)
            write_text_atomic(target, json.dumps(settings, indent=2), new_file_mode=0o600)
    except (OSError, UnicodeError, ValueError) as error:
        return UnreadableTrustFile(folder, target, type(error).__name__)
    return Trusted(folder, target, project_key)


def trust_claude_launch(folder: Path, config_dir: Path) -> None:
    """Write trust before either Claude launch path starts a process."""
    from scripts.whoami.agent_accounts import claude_config_file

    target = claude_config_file(config_dir)
    try:
        folder = folder.resolve()
        result = _write_claude_trust(folder, target, repository_trust_key(folder))
    except (OSError, subprocess.SubprocessError, HerdrFailure) as error:
        reason = f"{folder}: trust anomaly: {target}: {error}"
    else:
        if isinstance(result, Trusted):
            return
        reason = f"{folder}: trust anomaly: {target}: {result.error_class}"
    reason = " ".join(reason.splitlines())
    raise HerdrFailure(reason)


@dataclass(frozen=True)
class TrustCopied:
    """The target account lacked trust for the folder, and the account `source` had it."""

    folder: Path
    account_file: Path
    source: str


def heal_claude_trust(
    folder: Path, target: Path, sources: Sequence[tuple[str, Path]], *, dry_run: bool,
) -> Trusted | TrustCopied | Untrusted | UnreadableTrustFile:
    """Copy trust from the first source that trusts the folder.

    A dry run reports the source without a write.
    """
    present = claude_trust(folder, target)
    if not isinstance(present, Untrusted):
        return present
    for label, source in sources:
        if not isinstance(claude_trust(folder, source), Trusted):
            continue
        if dry_run:
            return TrustCopied(present.folder, target, label)
        copied = copy_claude_trust(folder, source, target)
        return TrustCopied(present.folder, target, label) if isinstance(copied, Trusted) else copied
    return present


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
        print(f"herdr {directory}: repository path is unknown", file=sys.stderr)
        return directory
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
    return "herdr-" + name + ".service"


def generated_session_name(text: str, suffix: str = "") -> str:
    tail = "-" + suffix if suffix else ""
    tail = re.sub(r"[^A-Za-z0-9_.-]", "-", tail)[:63]
    base = re.sub(r"[^A-Za-z0-9_.-]", "-", text)[:64 - len(tail)] or "-"
    if base in (".", ".."):
        base = "-"
    return base + tail


def _timeout(timeout_ms: object) -> None:
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms < 0:
        raise HerdrFailure("timeout must be a nonnegative integer in milliseconds")


def server_answering(status: subprocess.CompletedProcess[str]) -> bool:
    return status.returncode == 0 and "status: running" in status.stdout.splitlines()


def _workspace_startup_state(value: object) -> _WorkspaceStartupState:
    workspace = json_object(value, "herdr workspace")
    workspace_id = json_text(workspace.get("workspace_id"), "herdr workspace id")
    tab_count, pane_count = workspace.get("tab_count"), workspace.get("pane_count")
    untouched = (workspace.get("label") == "~" and type(tab_count) is int and tab_count == 1
                 and type(pane_count) is int and pane_count == 1 and workspace.get("agent_status") == "unknown")
    return _WorkspaceStartupState(workspace_id,
        _StartupShellEvidence.UNTOUCHED if untouched else _StartupShellEvidence.PRESERVE)


def _pane_startup_state(value: object) -> _PaneStartupState:
    pane = json_object(value, "herdr pane")
    pane_id = json_text(pane.get("pane_id"), "herdr pane id")
    revision, scroll = pane.get("revision"), pane.get("scroll")
    scrollback = (json_object(cast(object, scroll), "herdr pane scroll").get("max_offset_from_bottom")
                  if isinstance(scroll, dict) else None)
    untouched = (pane.get("agent") is None and pane.get("agent_status") == "unknown"
                 and pane.get("cwd") == str(Path.home()) and pane.get("foreground_cwd") == str(Path.home())
                 and type(revision) is int and revision == 0 and type(scrollback) is int and scrollback == 0)
    return _PaneStartupState(pane_id,
        _StartupShellEvidence.UNTOUCHED if untouched else _StartupShellEvidence.PRESERVE)


def _untouched_startup_shell(workspace: _WorkspaceStartupState, panes: list[_PaneStartupState]) -> bool:
    """Missing evidence preserves the workspace rather than risking live work."""
    members = [pane for pane in panes if pane.pane_id.split(":", 1)[0] == workspace.workspace_id]
    return (workspace.evidence is _StartupShellEvidence.UNTOUCHED and len(members) == 1
            and members[0].evidence is _StartupShellEvidence.UNTOUCHED)


def keeps_restored_layout(session: str) -> bool:
    """Keep restored panes regardless of the server name."""
    del session
    return True


def _server_unit_active(name: str) -> bool:
    return run_program([
        os.environ.get("SYSTEMCTL", "systemctl"), "--user", "is-active", server_unit(name),
    ]).returncode == 0


def _installed_server_unit(name: str) -> bool:
    """True when a unit file defines the server's unit (the boot unit); a transient unit does not count."""
    shown = run_program([
        os.environ.get("SYSTEMCTL", "systemctl"), "--user", "show", "-p", "FragmentPath", "--value", server_unit(name),
    ])
    path = shown.stdout.strip()
    return shown.returncode == 0 and bool(path) and "/transient/" not in path


def _pane_shell_path() -> Path:
    """The pane shell wrapper. One path for every worktree, because a running server keeps it in SHELL."""
    state = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(state) / "herdr" / "pane-shell"


def _pane_shell() -> str:
    """The shell that panes run: the caller's SHELL when it is an executable file, else /bin/sh.

    A systemd-run service gets the user manager's environment, not the caller's, so the value goes to it explicitly.
    """
    shell = os.environ.get("SHELL", "")
    if (shell and Path(shell).is_absolute() and os.path.isfile(shell) and os.access(shell, os.X_OK)
            and Path(shell).resolve() != _pane_shell_path().resolve()):
        return shell
    return "/bin/sh"


def _pane_scopes_unavailable() -> str:
    """Probe a scope and write the wrapper. Return the failure reason, or an empty string."""
    prefix, unavailable = agent_scope_prefix()
    if unavailable:
        return unavailable
    path = _pane_shell_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        staged = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        _ = staged.write_text(_PANE_SHELL_SCRIPT.replace("@SCOPE_PREFIX@", shlex.join(prefix)), encoding="utf-8")
        # The mode is set before the replace: a server that runs now never finds a wrapper it cannot run.
        staged.chmod(0o755)
        _ = staged.replace(path)
    except OSError as error:
        return f"cannot write {path}: {error}"
    return ""


def _submit_server(client: Herdr) -> subprocess.CompletedProcess[str]:
    # The shared session's server has a boot unit; a transient unit of its name cannot start beside it.
    if client.session == shared_session() and _installed_server_unit(client.session):
        return run_program([os.environ.get("SYSTEMCTL", "systemctl"), "--user", "start", server_unit(client.session)])
    # Each pane shell uses the configured agent slice. The unit holds only the server.
    unavailable = " ".join(_pane_scopes_unavailable().splitlines())
    if unavailable:
        print(f"herdr {client.session}: pane scopes unavailable, panes stay in the server unit: {unavailable}",
              file=sys.stderr)
    shell = [] if unavailable else [f"--setenv=SHELL={_pane_shell_path()}", f"--setenv=HERDR_PANE_SHELL={_pane_shell()}"]
    return run_program([
        os.environ.get("SYSTEMD_RUN", "systemd-run"), "--user", f"--unit={server_unit(client.session)}",
        "--collect", *_SERVER_PROPERTIES, *shell, *client.argv("server"),
    ])


def _server_start_failure(name: str) -> HerdrFailure:
    message = f"herdr server for {name} did not start"
    log = _session_socket(name).with_name("herdr-server.log")
    if log.exists():
        message += f"; see {log}"
    return HerdrFailure(message)


def _start_darwin_server(client: Herdr, timeout_ms: int, *, session_kind: SecuritySession) -> ServerCreated | ServerAdopted:
    from scripts.lib.detached import DiscardOutput, ExplicitEnvironment, DetachedChildExited, DetachedStartFailure, start_detached

    deadline = time.monotonic() + operation_deadline.remaining(min(10.0, timeout_ms / 1000))
    try:
        if server_answering(run_program(client.argv("status", "server"),
                                        timeout=max(0.001, deadline - time.monotonic()))):
            return ServerAdopted()
    except subprocess.TimeoutExpired:
        pass
    if time.monotonic() >= deadline:
        print(f"herdr {client.session}: server start is pending", file=sys.stderr)
        return ServerAdopted()
    if not isinstance(session_kind, DesktopSession):
        print(f"herdr {client.session}: desktop context is unknown; opening a desktop window", file=sys.stderr)
        _ = restore_server(client.session, timeout_ms, session_kind=session_kind)
        return ServerAdopted()
    holders: set[int] = set()

    def ready() -> bool:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            if server_answering(run_program(client.argv("status", "server"), timeout=remaining)):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                holders.update(listener.pid for listener in _darwin_bound_sockets(timeout=remaining)
                               if listener.path == _session_socket(client.session))
                return bool(holders)
        except subprocess.TimeoutExpired:
            pass
        return False

    operation_deadline.check()
    try:
        pid = start_detached(client.argv("server"), ready=ready, within=deadline - time.monotonic(),
                             output=DiscardOutput(), environment=ExplicitEnvironment(unmarked_environment()))
    except DetachedChildExited as error:
        if holders and holders != {error.pid}:
            return ServerAdopted()
        raise _server_start_failure(client.session) from error
    except (DetachedStartFailure, HerdrFailure) as error:
        raise _server_start_failure(client.session) from error
    if holders != {pid}:
        try:
            identity = _startup_process_reader(PROGRAM_TIMEOUT_SECONDS).identity(pid)
            if check_target(identity, action="signal"):
                operation_deadline.check()
                os.kill(pid, signal.SIGTERM)
        except (_StartupProcessGone, ProcessLookupError, FileNotFoundError):
            pass
        except (HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
            print(f"herdr {client.session}: child {pid} retained: {error}", file=sys.stderr)
        return ServerAdopted()
    return ServerCreated()


def start_server(name: str, timeout_ms: int, *,
                 session_kind: SecuritySession | SessionKindNotRead = _SESSION_KIND_NOT_READ) -> ServerCreated | ServerAdopted:
    _timeout(timeout_ms)
    herdr = Herdr(name)
    if _platform() == "darwin":
        return _start_darwin_server(herdr, timeout_ms,
            session_kind=security_session() if isinstance(session_kind, SessionKindNotRead) else session_kind)
    deadline = time.monotonic() + operation_deadline.remaining(timeout_ms / 1000)
    try:
        if server_answering(run_program(herdr.argv("status", "server"), timeout=max(0.001, timeout_ms / 1000))):
            return ServerAdopted()
    except subprocess.TimeoutExpired:
        pass
    require_success(_submit_server(herdr), f"start {server_unit(name)}")
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(f"herdr {name}: server is pending after {timeout_ms} ms", file=sys.stderr)
            return ServerCreated()
        try:
            if server_answering(run_program(herdr.argv("status", "server"), timeout=max(0.001, remaining))):
                if _server_unit_active(name):
                    return ServerCreated()
                return ServerAdopted()
        except subprocess.TimeoutExpired:
            pass
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.1, remaining))


def restore_server(name: str, timeout_ms: int, *,
                   session_kind: SecuritySession) -> ServerCreated | ServerAdopted | WindowServerCreated:
    """Start in Aqua directly, or let a desktop attach window bootstrap its server."""
    _timeout(timeout_ms)
    if _platform() != "darwin" or isinstance(session_kind, DesktopSession):
        if _platform() == "darwin":
            return start_server(name, timeout_ms, session_kind=session_kind)
        return start_server(name, timeout_ms)
    client = Herdr(name)
    deadline = time.monotonic() + min(15.0, timeout_ms / 1000)
    try:
        if server_answering(run_program(client.argv("status", "server"),
                                        timeout=max(0.001, deadline - time.monotonic()))):
            return ServerAdopted()
        # An existing listener that has not answered yet is also another launch's.
        if any(item.path == _session_socket(name) for item in _darwin_bound_sockets(
                timeout=max(0.001, deadline - time.monotonic()))):
            print(f"herdr {name}: existing listener is pending; listener retained", file=sys.stderr)
            return ServerAdopted()
    except (OSError, subprocess.SubprocessError, HerdrFailure) as error:
        print(f"herdr {name}: listener evidence is unknown: {error}", file=sys.stderr)
    try:
        require_success(run_program([
            os.environ.get("SHUTDOWN_OPEN", "open"), "-na", "/Applications/Nix Apps/Ghostty.app",
            "--args", "-e", herdr_binary(), "session", "attach", name,
        ], timeout=max(0.001, deadline - time.monotonic())), "open Mac desktop window")
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if server_answering(run_program(client.argv("status", "server"), timeout=max(0.001, remaining))):
                return WindowServerCreated()
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.1, remaining))
    except (OSError, subprocess.SubprocessError, HerdrFailure) as error:
        print(f"herdr {name}: desktop start is incomplete: {error}", file=sys.stderr)
    print(f"herdr {name}: desktop server is pending", file=sys.stderr)
    return WindowServerCreated()


def shared_session(*, dependencies: Callable[[Path], None] | None = None) -> str:
    """The one herdr session every Claude session lives in, one workspace per production and one tab
    per Claude session: `agents`, or the name in `herdr/shared-session` under the config directory.

    """
    path = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "herdr" / "shared-session"
    if dependencies is not None:
        dependencies(path)
    try:
        name = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return SHARED_SESSION
    return name


def row_workspace(row: staff.StaffRow | staff.StaffRoot, record: staff.StaffSnapshot, name: str) -> str:
    """Select the configured producer workspace or the owner's workspace."""
    if isinstance(row, staff.StaffRow) and row.kind == "producer":
        return conf.read_or_repository("staff").text("PRODUCER_WORKSPACE")
    while isinstance(row, staff.StaffRow):
        if row.kind == "showrunner":
            return row.name
        row = record.parent_of(row) or staff.STAFF_ROOT
    return name or shared_session()


def attach_hint(space: str, tab: str) -> str:
    """Where the user finds one Claude session: its tab in a workspace of the shared session."""
    return f"herdr session attach {shared_session()}, workspace {space}, tab {tab}"


def open_tab(name: str, space: str, title: str, cwd: str, environment: dict[str, str], *, member: str = "") -> str:
    return Herdr(name).open_tab(space, title, cwd, environment, **({"member": member} if member else {}))


def save_shared_layout(running: set[str]) -> None:
    """Save the shared session's workspace and tab order, when that session is among the `running` ones.

    A tab the user renamed in herdr reaches the saved layout here; launchers save after each change.
    """
    name = shared_session()
    if name in running:
        client = Herdr(name)
        with _LayoutLock(name):
            client.save_layout()


def reload_agent_rules(name: str) -> bool:
    """Return whether session `name` reloaded its detection rules; report a failure on stderr."""
    try:
        _ = Herdr(name).run("server", "reload-agent-manifests")
    except HerdrFailure as error:
        print(f"herdr {name}: detection rules not reloaded: {error}", file=sys.stderr)
        return False
    return True


def close_agent(session: str, pane: str) -> None:
    """Close the selected tab, or stop its separate server."""
    if session != shared_session():
        stop_session(session)
        return
    client = Herdr(session)
    path = pane_launch.record_directory() / f"{session}.json"
    if pane in pane_launch.read_valid_entries(path):
        _ = client.close_retained_pane(pane)
        return
    rows = client.listed("pane")
    selected = next((row for row in rows if row.get("pane_id") == pane), None)
    if selected is None:
        return
    tab = json_text(selected.get("tab_id"), "herdr pane tab id")
    if any(row.get("tab_id") == tab and row.get("pane_id") != pane for row in rows):
        raise HerdrFailure(f"{pane}: would close another pane in tab {tab}", "wrong_target")
    target = ""
    terminal = json_text(selected.get("terminal_id", ""), "herdr terminal id")
    pane_info: PaneIdentity = {
        "pane_id": pane, "terminal_id": terminal, "session_id": target or None,
        "workspace_id": json_text(selected.get("workspace_id", ""), "herdr workspace id"),
    }
    if not check_target(target, action="close", place=HerdrPlace(session, pane),
                        terminal=terminal, pane_info=pane_info, client=client):
        return
    _ = client.run("tab", "close", tab)
    remaining = json_items(client.result("pane", "list").get("panes"), "herdr pane list")
    if any((row := json_object(item, "herdr pane")).get("pane_id") == pane
           or terminal and row.get("terminal_id") == terminal for item in remaining):
        raise HerdrFailure(f"{pane}: tab closure is not confirmed")


def repair_retained_launches(record: staff.StaffSnapshot, directory: Path, *,
                             member: staff.StaffId | None = None, dry_run: bool = False) -> list[str]:
    """Repair legacy records for outside callers using current native sessions."""
    groups: dict[staff.StaffId, list[pane_launch.LaunchPublication]] = {}
    notes: list[str] = []
    for path in sorted(directory.glob("*.json")):
        try:
            entries = pane_launch.read_valid_entries(path)
        except (OSError, ValueError) as error:
            notes.append(f"{path}: legacy records are unknown: {error}")
            continue
        for entry in entries.values():
            owner = record.by_member_reference(pane_launch.reference_id(entry))
            if owner is not None and (member is None or owner.id == member):
                groups.setdefault(owner.id, []).append(pane_launch.LaunchPublication(path, entry))
    for identity, publications in groups.items():
        owner = record.by_id(identity)
        if owner is None or len(publications) < 2:
            continue
        proved: list[pane_launch.LaunchPublication] = []
        for publication in publications:
            try:
                client = Herdr(publication.path.stem)
                if not any(row["pane_id"] == publication.entry["pane_id"] for row in client.panes()):
                    continue
                if client.native_session(publication.entry["pane_id"],
                                         pane_launch.agent_kind(publication.entry), PROGRAM_TIMEOUT_SECONDS) == owner.current_session:
                    proved.append(publication)
            except (HerdrFailure, OSError, ValueError):
                continue
        if len(proved) != 1:
            notes.append(f"{owner.name}: current pane is unknown or duplicated; legacy records retained")
            continue
        winner = proved[0]
        for publication in publications:
            if publication == winner:
                continue
            pane = publication.entry["pane_id"]
            client = Herdr(publication.path.stem)
            try:
                selected = next((row for row in client.panes() if row["pane_id"] == pane), None)
                if Herdr(winner.path.stem).native_session(
                        winner.entry["pane_id"], pane_launch.agent_kind(winner.entry),
                        PROGRAM_TIMEOUT_SECONDS) != owner.current_session:
                    notes.append(f"{owner.name}: current session changed; legacy pane {pane} retained")
                    continue
                if not dry_run:
                    if selected is not None and not client.close_tab_confirmed(
                            pane, terminal=selected.get("terminal_id", "")):
                        notes.append(f"{owner.name}: legacy pane {pane} closure is pending; record retained")
                        continue
                    pane_launch.retire_launch(publication.path, pane, publication.entry)
                notes.append(f"{client.session}:{pane}: stale launch retired" if not dry_run
                             else f"{client.session}:{pane}: would retire stale launch")
            except (HerdrFailure, OSError, ValueError) as error:
                if isinstance(error, HerdrFailure) and error.code == "wrong_target":
                    raise
                notes.append(f"{owner.name}: legacy pane {pane} retained: {error}")
    return notes


def stop_session(name: str) -> None:
    """Stop the selected server and its service, while preserving other sessions."""
    if name == shared_session():
        raise HerdrFailure(f"would stop other sessions in herdr session {name}; close one tab instead", "wrong_target")
    client = Herdr(name)
    try:
        result = run_program(client.argv("server", "stop"))
        require_success(result, f"stop herdr session {name}")
    finally:
        if _platform() != "darwin":
            if _server_unit_active(name):
                require_success(run_program([os.environ.get("SYSTEMCTL", "systemctl"), "--user", "stop", server_unit(name)]),
                                f"stop {server_unit(name)}")


def proc_root() -> Path:
    return Path(os.environ.get("HERDR_PROC", "/proc"))


def _process_environment(process: Path) -> dict[str, str]:
    return dict(entry.split("=", 1) for entry in process.joinpath("environ").read_bytes().decode(errors="replace").split("\0") if "=" in entry)


@dataclass(frozen=True)
class EnvironmentThread:
    """The native Claude runtime identifies its current session."""

    id: str


@dataclass(frozen=True)
class LaunchArgument:
    """A resume argument identifies a launch, without current conversation proof."""

    id: str


@dataclass(frozen=True)
class PaneSessionReport:
    """One foreground Codex process owns the reported pane session."""

    id: str


@dataclass(frozen=True)
class UnreportedResume:
    """A resumed Codex with no report yet is named by its resume argument."""

    id: str


@dataclass(frozen=True)
class StartingCodexPane:
    """A fresh interactive Codex launch has no conversation report yet."""

    pid: int


@dataclass(frozen=True)
class CodexLaunchIdentityUnknown:
    """The launch evidence cannot identify a conversation or prove a fresh launch."""

    reason: str


@dataclass(frozen=True)
class CodexWrapperPending(CodexLaunchIdentityUnknown):
    """The Codex pane still exposes a wrapper with no conversation identity."""


class CodexPaneStarting(BlockingIOError):
    """A caller needs a conversation while a proved fresh Codex pane starts."""

    def __init__(self, state: StartingCodexPane) -> None:
        super().__init__(f"pid {state.pid}: Codex launch identity is unavailable; pane is starting")
        self.state: StartingCodexPane = state


@dataclass(frozen=True)
class NativeAgentProcess:
    """Conversation evidence from one process with a stable start time."""

    identity: EnvironmentThread | LaunchArgument | PaneSessionReport | UnreportedResume
    started: str
    environment: dict[str, str]

    @property
    def current_identity(self) -> str:
        if isinstance(self.identity, LaunchArgument):
            raise ValueError("launch argument does not prove the current conversation")
        return self.identity.id


def _process_birth(pid: int) -> str:
    started = (darwin_process_identity(pid).started if _platform() == "darwin"
               else runtime_sessions.process_start(pid))
    if not started:
        raise ValueError(f"pid {pid}: process start is unavailable")
    return started


class CodexSessionPending(ValueError):
    """A fresh Codex process or its wrapper has no session report yet."""


def codex_resume_thread(pid: int) -> str:
    """Read the Codex thread from its resume arguments."""
    launch = _codex_launch_argument(pid)
    if isinstance(launch, LaunchArgument):
        return launch.id
    if isinstance(launch, (StartingCodexPane, CodexWrapperPending)):
        raise CodexSessionPending(f"pid {pid}: Codex launch identity is unavailable")
    raise ValueError(launch.reason)


def _codex_launch_argument(pid: int) -> LaunchArgument | StartingCodexPane | CodexLaunchIdentityUnknown:
    """Classify the launch arguments without requiring a conversation identity."""
    unavailable = f"pid {pid}: Codex launch identity is unavailable"
    unknown = CodexLaunchIdentityUnknown(f"{unavailable}: arguments unreadable")
    if _platform() == "darwin":
        process = next((process for process in _darwin_processes() if process.pid == pid), None)
        if process is None or isinstance(process.arguments, UnreadableProcessArguments):
            return unknown
        arguments = list(process.arguments.argv)
    else:
        arguments = _process_arguments(proc_root() / str(pid))
    if is_codex_process(arguments):
        options = iter(arguments[2:] if Path(arguments[0]).name in {"node", "nodejs"} else arguments[1:])
        fresh = True
        value_options = {"-c", "--config", "--enable", "--disable", "-m", "--model",
                         "-C", "--cd", "-p", "--profile", "-s", "--sandbox", "-a", "--ask-for-approval",
                         "--local-provider", "-i", "--image", "--add-dir", "--remote", "--remote-auth-token-env"}
        for option in options:
            option_name, separator, option_value = option.partition("=")
            if separator and option_name in value_options:
                option = option_name
            elif len(option) > 2 and option[:2] in {"-c", "-m", "-C", "-p", "-s", "-a", "-i"}:
                option, option_value = option[:2], option[2:].removeprefix("=")
            elif option in value_options:
                option_value = next(options, "")
            if option in value_options:
                if not option_value or option_value.startswith("-"):
                    fresh = False
                    unknown = CodexLaunchIdentityUnknown(f"{unavailable}: option {option} has a missing or dash value")
                if option in {"-c", "--config"} and "=" not in option_value:
                    fresh = False
                    unknown = CodexLaunchIdentityUnknown(f"{unavailable}: option {option} has no =")
            elif option == "--":
                return StartingCodexPane(pid) if fresh else unknown
            elif not option.startswith("-"):
                if option == "resume":
                    try:
                        return LaunchArgument(staff.native_id(next(options, "")))
                    except staff.StaffRefused:
                        return CodexLaunchIdentityUnknown(f"{unavailable}: bad resume id")
                if option in {"exec", "e", "review", "fork", "login", "logout", "mcp", "mcp-server",
                              "app-server", "app", "completion", "sandbox", "debug", "apply", "a",
                              "cloud", "features", "help"}:
                    return CodexLaunchIdentityUnknown(f"{unavailable}: subcommand {option}")
                return StartingCodexPane(pid) if fresh else unknown
            elif option in {"--no-daemon", "--no-alt-screen", "--full-auto", "--search", "--oss",
                            "--dangerously-bypass-approvals-and-sandbox"}:
                continue
            else:
                fresh = False
                name = option_name if option_name.startswith("--") else option_name[:2]
                unknown = CodexLaunchIdentityUnknown(f"{unavailable}: unknown option {name}")
        else:
            if fresh:
                return StartingCodexPane(pid)
    else:
        return CodexWrapperPending(unavailable)
    return unknown


def codex_launch_identity(pid: int, reported: str) -> LaunchArgument | PaneSessionReport | StartingCodexPane | CodexLaunchIdentityUnknown:
    """Distinguish a known identity, a fresh launch, and incomplete launch evidence."""
    launch = _codex_launch_argument(pid)
    if isinstance(launch, LaunchArgument):
        return PaneSessionReport(reported) if reported == launch.id else launch
    return PaneSessionReport(reported) if reported else launch


def codex_pane_process(pid: int, place: HerdrPlace) -> NativeAgentProcess:
    """Read the report owned by this pane's sole foreground Codex process."""
    client = Herdr(place.session)
    info = json_object(client.result("pane", "process-info", "--pane", place.pane).get("process_info"),
                       "Codex pane process info")
    foreground = _codex_foreground(info)
    if len(foreground) == 1 and foreground[0].get("pid") == pid:
        pane = client.pane_row(place.pane)
        if not isinstance(pane, NoPane):
            try:
                return pane_native_process(info, "codex", pane)
            except CodexPaneStarting:
                raise
            except HerdrFailure as error:
                if isinstance(error.__cause__, CodexSessionPending):
                    raise error.__cause__
                raise ValueError(str(error)) from error
    return native_process(pid, "codex", "")


def native_process(pid: int, kind: str, reported: str | PaneNotRead = PANE_NOT_READ) -> NativeAgentProcess:
    """Bind the current identity and account to one unchanged process."""
    if kind == "codex" and isinstance(reported, PaneNotRead):
        observed = _pane_observations.get()
        place = observed.process_places.get(pid, NotInHerdr()) if observed is not None else herdr_place_of(pid)
        if isinstance(place, HerdrPlace):
            return codex_pane_process(pid, place)
        reported = ""
    started = _process_birth(pid)
    environment = process_environment(pid)
    if kind == "codex":
        identity = codex_launch_identity(pid, reported if isinstance(reported, str) else "")
    elif kind == "claude":
        try:
            current = staff.runtime_id(pid)
        except staff.StaffRefused as error:
            raise ValueError(f"pid {pid}: native registry identity is invalid") from error
        if not current:
            raise NativeRegistryUnavailable(f"pid {pid}: native registry identity is unavailable")
        identity = EnvironmentThread(current)
    else:
        raise ValueError(f"pid {pid}: unknown agent kind {kind}")
    if _process_birth(pid) != started:
        raise ValueError(f"pid {pid}: native process start changed")
    if isinstance(identity, StartingCodexPane):
        raise CodexPaneStarting(identity)
    if isinstance(identity, CodexWrapperPending):
        raise CodexSessionPending(identity.reason)
    if isinstance(identity, CodexLaunchIdentityUnknown):
        raise ValueError(identity.reason)
    return NativeAgentProcess(identity, started, environment)


def check_native_target(pid: int, kind: str, expected: NativeAgentProcess) -> bool:
    """Check the current conversation and process generation before a signal."""
    try:
        target = _startup_process_reader(PROGRAM_TIMEOUT_SECONDS).identity(pid)
    except (_StartupProcessGone, FileNotFoundError) as error:
        raise ProcessLookupError(f"process {pid}: exited") from error
    place = herdr_place_of(pid)
    if isinstance(place, HerdrPlace):
        forget_pane_evidence(place.session, place.pane)
    try:
        observed = native_process(pid, kind)
    except CodexPaneStarting:
        return False
    if observed != expected:
        return False
    return check_target(target, action="signal")


def _process_display_name(pid: int) -> str:
    """Use the tab or registry name when available; otherwise use the pid."""
    try:
        place = herdr_place_of(pid)
        if isinstance(place, HerdrPlace):
            tab = Herdr(place.session).pane_tab(place.pane)[2]
            record = _tab_staff()
            owner = record.by_id(staff.StaffId(tab["staff_id"])) if not isinstance(tab, UnownedTab) else None
            name = (owner.name if owner is not None else tab.title if isinstance(tab, UnownedTab) else "").strip()
            if name:
                return name
    except (HerdrFailure, OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        pass
    try:
        for record in runtime_sessions.running_sessions():
            if record["pid"] == pid and record["name"].strip():
                return record["name"]
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        pass
    return f"pid {pid}"


def codex_managed_daemon(arguments: list[str]) -> bool:
    """Identify the shared daemon and its helpers, which hold no thread."""
    if not is_codex_process(arguments):
        return False
    index = 2 if Path(arguments[0]).name in {"node", "nodejs"} else 1
    commands = arguments[index:]
    if "app-server" not in commands:
        return False
    server = commands.index("app-server")
    return "--managed-daemon" in commands or commands[server + 1:server + 2] == ["daemon"]


@dataclass(frozen=True)
class LiveConversation:
    """The target conversation runs in these processes or a hosted thread."""

    kind: Literal["claude", "codex"]
    pids: tuple[int, ...]


@dataclass(frozen=True)
class StoppedConversation:
    """A complete read found no live target conversation."""


@dataclass(frozen=True)
class UnknownConversation:
    """Incomplete evidence cannot prove that the target conversation stopped."""

    reason: str
    shared_codex: bool = False


def conversation_note(observed: UnknownConversation) -> None:
    print(f"anomaly: {' '.join(observed.reason.splitlines())}; not blocking", file=sys.stderr)


@dataclass(frozen=True)
class CodexConversations:
    """One pass shares these live Codex threads and incomplete process reads."""

    live: Mapping[str, tuple[int, ...]]
    unknown: tuple[str, ...]


@final
class LoadedThreadRpc:
    """Read native loaded-thread replies within one deadline."""

    def __init__(self, port: int, deadline: float) -> None:
        self.deadline: float = deadline
        self.connection: socket.socket = socket.create_connection(("127.0.0.1", port), timeout=self.remaining())
        self.buffer: bytearray = bytearray()
        self.counter: int = 0
        try:
            key = base64.b64encode(secrets.token_bytes(16)).decode()
            self.connection.sendall((f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n").encode())
            while b"\r\n\r\n" not in self.buffer:
                self.receive()
            header, _, rest = self.buffer.partition(b"\r\n\r\n")
            if b" 101 " not in header.split(b"\r\n")[0]:
                raise ValueError("server refused the websocket handshake")
            self.buffer = bytearray(rest)
            _ = self.call("initialize", {"clientInfo": {"name": "staff-observe", "version": "1.0"},
                                         "capabilities": {"experimentalApi": True}})
        except BaseException:
            self.connection.close()
            raise

    def remaining(self) -> float:
        if time.monotonic() >= worker_lifecycle.effective_deadline():
            raise worker_lifecycle.OperationDeadlineExpired("shutdown incomplete: RPC operation deadline expired")
        remaining = min(self.deadline, worker_lifecycle.effective_deadline()) - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("loaded-thread observation timed out")
        return remaining

    def receive(self) -> None:
        self.connection.settimeout(self.remaining())
        data = self.connection.recv(262144)
        if not data:
            raise ConnectionError("server closed the connection")
        self.buffer.extend(data)

    def take(self, size: int) -> bytes:
        while len(self.buffer) < size:
            self.receive()
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data

    def call(self, method: str, params: dict[str, object], timeout: float = math.inf) -> dict[str, object]:
        deadline = self.deadline
        self.deadline = min(deadline, time.monotonic() + timeout)
        try:
            self.counter += 1
            data = json.dumps({"jsonrpc": "2.0", "id": self.counter, "method": method, "params": params}).encode()
            mask = secrets.token_bytes(4)
            length = len(data)
            header = bytes((0x81, 0x80 | length)) if length < 126 else (
                bytes((0x81, 0xFE)) + struct.pack(">H", length) if length < 65536
                else bytes((0x81, 0xFF)) + struct.pack(">Q", length))
            self.connection.settimeout(self.remaining())
            self.connection.sendall(header + mask + bytes(byte ^ mask[index % 4] for index, byte in enumerate(data)))
            while True:
                header = self.take(2)
                length = header[1] & 0x7F
                if length in (126, 127):
                    length = int.from_bytes(self.take(2 if length == 126 else 8), "big")
                payload = self.take(length)
                if header[0] & 0x0F == 8:
                    raise ConnectionError("server sent close")
                if header[0] & 0x0F not in (1, 2):
                    continue
                message = cast(object, json.loads(payload))
                if not isinstance(message, dict):
                    continue
                reply = cast(dict[str, object], message)
                if reply.get("id") != self.counter:
                    continue
                return reply
        finally:
            self.deadline = deadline


class LoadedThreadClient(Protocol):
    def call(self, method: str, params: dict[str, object], timeout: float = ...) -> Mapping[str, object]: ...


def loaded_threads(client: LoadedThreadClient, deadline: float) -> set[str]:
    """Read every loaded-thread page before the supplied deadline."""
    threads: set[str] = set()
    cursor = ""
    seen: set[str] = set()
    while time.monotonic() < deadline:
        response = client.call("thread/loaded/list", {"cursor": cursor} if cursor else {},
                               timeout=max(0.01, deadline - time.monotonic()))
        if response.get("error"):
            raise ValueError(f"thread/loaded/list failed: {response['error']}")
        result = response.get("result")
        listed = cast(dict[str, object], result) if isinstance(result, dict) else {}
        data = listed.get("data")
        if not isinstance(data, list):
            raise ValueError("server returned no loaded-thread list")
        for item in cast(list[object], data):
            if not isinstance(item, str) or not item:
                raise ValueError("server returned an invalid loaded-thread id")
            threads.add(item)
        next_cursor = listed.get("nextCursor")
        if next_cursor is None or next_cursor == "":
            return threads
        if not isinstance(next_cursor, str) or next_cursor in seen:
            raise ValueError("server returned an invalid loaded-thread cursor")
        cursor = next_cursor
        seen.add(cursor)
    if time.monotonic() >= worker_lifecycle.effective_deadline():
        raise worker_lifecycle.OperationDeadlineExpired("shutdown incomplete: RPC operation deadline expired")
    raise ValueError("loaded-thread list timed out")


@observation_snapshot()
def codex_conversations(*, wanted: str = "") -> CodexConversations:
    from scripts.message import codex_daemon

    candidates: list[tuple[int, list[str]]] = []
    unknown: list[tuple[int, str] | str] = []
    process_root = proc_root()
    process_paths: list[Path] = []

    @cache
    def pane_servers() -> dict[int, str]:
        return _live_server_processes(process_paths)

    def add_unknown(pid: int, reason: str) -> None:
        """Count terminal foreground groups; failed process reads retain uncertainty."""
        try:
            if _platform() == "darwin":
                result = run_program([os.environ.get("HERDR_PS", "ps"), "-o", "pgid=,tpgid=", "-p", str(pid)],
                                     timeout=PROGRAM_TIMEOUT_SECONDS)
                require_success(result, "Codex terminal process group")
                group, foreground = map(int, result.stdout.split())
                if foreground <= 0 or group != foreground:
                    return
            else:
                stat = (process_root / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
                group, foreground = int(stat[2]), int(stat[5])
                if foreground <= 0 or group != foreground:
                    return
                leader = (process_root / stat[3] / "stat").read_text().rsplit(")", 1)[1].split()
                if int(leader[1]) not in pane_servers():
                    return
        except (OSError, ValueError, IndexError, HerdrFailure, subprocess.SubprocessError):
            pass
        unknown.append((pid, reason))

    try:
        if _platform() == "darwin":
            for process in _darwin_processes(complete=True):
                if isinstance(process.arguments, UnreadableProcessArguments):
                    add_unknown(process.pid, "Codex argv unknown")
                else:
                    candidates.append((process.pid, list(process.arguments.argv)))
        else:
            process_paths = list(process_root.iterdir())
            for path in process_paths:
                if not path.name.isdecimal():
                    continue
                try:
                    if path.stat().st_uid == os.getuid():
                        candidates.append((int(path.name), _process_arguments(path)))
                except FileNotFoundError:
                    if path.exists():
                        add_unknown(int(path.name), "process argv unknown")
                except OSError:
                    add_unknown(int(path.name), "process argv unknown")
    except (HerdrFailure, OSError, ValueError) as error:
        unknown.append(f"Codex process list unknown: {error}")
    live: dict[str, tuple[int, ...]] = {}
    daemon_reads = {read.owner.home: read.sessions for read in codex_daemon.read_daemons(os.environ)}
    for home, observed in daemon_reads.items():
        if isinstance(observed, codex_daemon.DaemonUnreadable):
            unknown.append(f"{home} (Codex daemon unknown: {observed.reason})")
        else:
            for session in observed:
                _ = live.setdefault(session.thread_id, ())
    for pid, arguments in candidates:
        if not is_codex_process(arguments) or codex_managed_daemon(arguments):
            continue
        environment: dict[str, str] | None = None
        try:
            environment = process_environment(pid)
            observed = native_process(pid, "codex")
            if isinstance(observed.identity, LaunchArgument):
                add_unknown(pid, "Codex thread unknown")
            else:
                identity = observed.current_identity
                live[identity] = (*live.get(identity, ()), pid)
        except ProcessLookupError:
            continue
        except (HerdrFailure, OSError, ValueError) as error:
            observations = _pane_observations.get()
            if (isinstance(error, CodexPaneStarting) and observations is not None
                    and pid in observations.process_places):
                print(f"herdr pid {pid}: pane is starting", file=sys.stderr)
                continue
            if environment is not None:
                if "--no-daemon" not in arguments:
                    home = Path(environment.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
                    if home in daemon_reads:
                        # A failed daemon read already has one note for its account.
                        continue
            add_unknown(pid, "Codex thread unknown")
    if wanted in live:
        return CodexConversations(live, ())
    return CodexConversations(live, tuple(
        entry if isinstance(entry, str) else f"{_process_display_name(entry[0])} ({entry[1]})"
        for entry in unknown))


def read_conversation(identity: str, kind: str = "", *,
                      codex: CodexConversations | None = None) -> LiveConversation | StoppedConversation | UnknownConversation:
    """Read one current conversation across the native runtimes and hosted threads."""
    unknown: list[str] = []
    shared_codex = False
    if kind != "codex":
        try:
            observed = runtime_sessions.session_liveness(identity)
            if isinstance(observed, runtime_sessions.LiveSessionProcess):
                return LiveConversation("claude", (observed.pid,))
            if isinstance(observed, runtime_sessions.SessionLivenessUnknown):
                match = re.search(r"(?:pid )?(\d+)(?:\.json|$)", observed.reason)
                name = _process_display_name(int(match[1])) if match else identity
                unknown.append(f"{name} (Claude session unknown: {observed.reason})")
        except (HerdrFailure, OSError, ValueError) as error:
            unknown.append(f"{identity} (Claude session unknown: {error})")
    if kind != "claude":
        codex = codex_conversations(wanted=identity) if codex is None else codex
        if identity in codex.live:
            return LiveConversation("codex", codex.live[identity])
        shared_codex = not unknown and bool(codex.unknown)
        unknown.extend(codex.unknown)
    if unknown:
        first = " ".join(unknown[0].splitlines())
        return UnknownConversation(f"{identity} is not proved live or stopped: {len(unknown)} processes unknown, first: {first}",
                                   shared_codex=shared_codex)
    return StoppedConversation()


def codex_notes(codex: CodexConversations) -> None:
    """Print each shared reason from the snapshot once."""
    for reason in dict.fromkeys(codex.unknown):
        conversation_note(UnknownConversation(reason))


def running_codex_threads(*, thread_host: Callable[[list[str]], bool],
                          codex: CodexConversations | None = None) -> set[str]:
    """Read proved current threads, including app-server threads and excluding the shared daemon."""
    # Keep the keyword for external callers; host processes no longer exclude their threads.
    _ = thread_host
    codex = codex_conversations() if codex is None else codex
    codex_notes(codex)
    return set(codex.live)


def process_environment(pid: int) -> dict[str, str]:
    """Read a process even outside herdr; a failed observation remains an error."""
    if _platform() == "darwin":
        return darwin_process_environment(pid)
    return _process_environment(proc_root() / str(pid))


def _process_arguments(process: Path) -> list[str]:
    return [value for value in process.joinpath("cmdline").read_bytes().decode(errors="replace").split("\0") if value]


def _foreground_arguments(process: dict[str, object]) -> list[str]:
    """Treat a process without an argv list as an exited process."""
    arguments = process.get("argv")
    if not isinstance(arguments, list):
        return []
    return [json_text(value, "foreground process argument") for value in cast(list[object], arguments)]


def _is_agent_process(arguments: list[str], kind: str) -> bool:
    if not arguments:
        return False
    executable = Path(arguments[0]).name
    if executable == kind:
        return True
    # The npm installations run a specific script through node, not a bare executable.
    script = arguments[1] if len(arguments) > 1 else ""
    return executable in {"node", "nodejs"} and (
        kind == "claude" and script.endswith("/@anthropic-ai/claude-code/cli.js")
        or kind == "codex" and script.endswith("/@openai/codex/bin/codex.js")
    )


def is_claude_process(arguments: list[str]) -> bool:
    """The argv is a Claude Code process, native or through node."""
    return _is_agent_process(arguments, "claude")


def is_codex_process(arguments: list[str]) -> bool:
    """The argv is a Codex process, native or through node."""
    return _is_agent_process(arguments, "codex")


class _StartupProcessGone(Exception):
    """The process has exited or is absent from the process snapshot."""


@final
class _DarwinProcessBSDInfo(ctypes.Structure):
    # Mac probe, 2026-11-03: PROC_PIDTBSDINFO returns 136 bytes.
    # The pid is at offset 12. Start seconds and microseconds are at offsets 120 and 128.
    # The spawned process matched ps lstart and the probe's wall-clock interval.
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32), ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32), ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32), ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32), ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32), ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32), ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32), ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32), ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32), ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64), ("pbi_start_tvusec", ctypes.c_uint64),
    ]
    pbi_pid: int = 0
    pbi_start_tvsec: int = 0
    pbi_start_tvusec: int = 0


def _darwin_process_generation(pid: int) -> int | ProcessGenerationUnavailable:
    """Read the kernel start time without reading process environment values."""
    try:
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        read = cast(Callable[[int, int, int, object, int], int], ctypes.CFUNCTYPE(
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
            ctypes.c_void_p, ctypes.c_int)(("proc_pidinfo", library)))
        info = _DarwinProcessBSDInfo()
        size = ctypes.sizeof(info)
        # PROC_PIDTBSDINFO is flavor 3 in libproc's public API.
        count = read(pid, 3, 0, ctypes.byref(info), size)
    except (OSError, AttributeError) as error:
        return ProcessGenerationUnavailable(f"process {pid}: libproc start time: {error}")
    if count != size or info.pbi_pid != pid or not 0 <= info.pbi_start_tvusec < 1_000_000:
        return ProcessGenerationUnavailable(f"process {pid}: libproc start time is unavailable")
    return info.pbi_start_tvsec * 1_000_000 + info.pbi_start_tvusec


@dataclass(frozen=True)
class _ProcessReadDeadline:
    at: float

    def remaining(self) -> float:
        remaining = self.at - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired("process observation", 0)
        return remaining


class _LinuxStartupProcesses:
    """Read startup facts from Linux process files."""

    def __init__(self) -> None:
        self.root: Path = proc_root()

    def identity(self, pid: int) -> AgentProcessIdentity:
        stat = (self.root / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] == "Z":
            raise _StartupProcessGone(f"process {pid}: exited")
        return AgentProcessIdentity(pid, int(stat[19]))

    def arguments(self, pid: int) -> list[str]:
        return _process_arguments(self.root / str(pid))

    def children(self, pid: int) -> Iterable[int]:
        # Each thread has its own child list.
        for task in sorted((self.root / str(pid) / "task").iterdir(), key=lambda task: int(task.name)):
            for child in (task / "children").read_text().split():
                yield int(child)

    def foreground(self, pid: int) -> int:
        stat = (self.root / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
        return int(stat[5])


class _DarwinStartupProcesses:
    """Share process tables within one startup observation."""

    def __init__(self, timeout: float | _ProcessReadDeadline) -> None:
        self.deadline: _ProcessReadDeadline = (timeout if isinstance(timeout, _ProcessReadDeadline)
                                               else _ProcessReadDeadline(time.monotonic() + timeout))
        result = run_program([os.environ.get("HERDR_PS", "ps"), "-axo", "pid=,ppid=,state=,lstart="],
                             timeout=self.deadline.remaining(),
                             environment={**os.environ, "LC_ALL": "C", "TZ": "UTC"})
        require_success(result, "startup process identities")
        self.states: dict[int, str] = {}
        self.children_by_parent: dict[int, list[int]] = {}
        for line in result.stdout.splitlines():
            fields = line.split(maxsplit=3)
            if len(fields) != 4 or not all(field.isdecimal() for field in fields[:2]):
                raise HerdrFailure("startup process identities are unreadable")
            pid = int(fields[0])
            if pid <= 0 or pid in self.states:
                raise HerdrFailure("startup process identities are ambiguous")
            self.states[pid] = fields[2]
            self.children_by_parent.setdefault(int(fields[1]), []).append(pid)
        if not self.states:
            raise HerdrFailure("startup process identities are empty")

    def identity(self, pid: int) -> AgentProcessIdentity:
        _ = self.deadline.remaining()
        if pid not in self.states or self.states[pid].startswith("Z"):
            raise _StartupProcessGone(f"process {pid}: exited")
        generation = _darwin_process_generation(pid)
        _ = self.deadline.remaining()
        return AgentProcessIdentity(pid, generation)

    @cached_property
    def processes(self) -> dict[int, _UserProcess]:
        return {process.pid: process for process in _darwin_processes(timeout=self.deadline)}

    def arguments(self, pid: int) -> list[str]:
        if pid not in self.processes:
            result = run_program([os.environ.get("HERDR_PS", "ps"), "-o", "command=", "-p", str(pid)],
                                 timeout=self.deadline.remaining())
            try:
                _require_darwin_process_read(result, pid, f"startup process {pid} command")
            except _DarwinProcessGone as error:
                raise _StartupProcessGone(str(error)) from error
            return result.stdout.split()
        arguments = self.processes[pid].arguments
        if isinstance(arguments, UnreadableProcessArguments):
            raise HerdrFailure(arguments.reason)
        return list(arguments.argv)

    def children(self, pid: int) -> Iterable[int]:
        return self.children_by_parent.get(pid, [])

    def foreground(self, pid: int) -> int:
        result = run_program([os.environ.get("HERDR_PS", "ps"), "-o", "tpgid=", "-p", str(pid)],
                             timeout=self.deadline.remaining())
        _require_darwin_process_read(result, pid, f"startup shell {pid} foreground")
        return int(result.stdout.strip())


def _startup_process_reader(timeout: float | _ProcessReadDeadline = PROGRAM_TIMEOUT_SECONDS) -> _LinuxStartupProcesses | _DarwinStartupProcesses:
    return _DarwinStartupProcesses(timeout) if _platform() == "darwin" else _LinuxStartupProcesses()


def _track_startup_identity(tracked: set[AgentProcessIdentity], identity: AgentProcessIdentity) -> None:
    """Keep strong generation evidence when a later read is weak."""
    if (isinstance(identity.start_ticks, ProcessGenerationUnavailable)
            and any(value.pid == identity.pid and isinstance(value.start_ticks, int) for value in tracked)):
        return
    tracked.difference_update(value for value in tuple(tracked) if value.pid == identity.pid)
    tracked.add(identity)


def _startup_processes(shell: PaneShell, kind: str, tracked: set[AgentProcessIdentity],
                       timeout: float | _ProcessReadDeadline = PROGRAM_TIMEOUT_SECONDS) -> AgentProcessPresent | PaneAtShell | PaneRunning | PaneProcessesUnreadable:
    """Keep identified processes across exec and parent changes; inspect every thread."""
    try:
        reader = _startup_process_reader(timeout)
    except (HerdrFailure, OSError, ValueError, subprocess.SubprocessError):
        return PaneProcessesUnreadable()
    commands: list[str] = []
    pending: list[tuple[int, bool]] = [(shell.pid, False)]
    live: dict[int, AgentProcessIdentity] = {}
    unreadable = False
    for identity in tuple(tracked):
        try:
            current = reader.identity(identity.pid)
            if isinstance(current.start_ticks, ProcessGenerationUnavailable):
                live[identity.pid] = current
                pending.append((identity.pid, True))
            elif current != identity:
                tracked.remove(identity)
            else:
                live[identity.pid] = identity
                pending.append((identity.pid, True))
        except (_StartupProcessGone, FileNotFoundError):
            tracked.remove(identity)
        except subprocess.TimeoutExpired:
            return PaneProcessesUnreadable()
        except (HerdrFailure, OSError, ValueError, IndexError, subprocess.SubprocessError):
            unreadable = True
    visited: set[int] = set()
    while pending:
        pid, belongs_to_agent = pending.pop()
        if pid in visited:
            continue
        visited.add(pid)
        try:
            if pid != shell.pid:
                identity = reader.identity(pid)
                previous = live.pop(pid, None)
                if previous is not None and previous != identity:
                    if isinstance(previous.start_ticks, ProcessGenerationUnavailable):
                        # A later read cannot remove this observation's weak evidence.
                        identity = previous
                    elif not isinstance(identity.start_ticks, ProcessGenerationUnavailable):
                        tracked.discard(previous)
                        # A new generation must prove that it is an agent.
                        belongs_to_agent = False
                if not belongs_to_agent:
                    arguments = reader.arguments(pid)
                    belongs_to_agent = _is_agent_process(arguments, kind)
                    if not belongs_to_agent:
                        commands.append(arguments[0] if arguments else str(pid))
                if belongs_to_agent:
                    _track_startup_identity(tracked, identity)
                    live[pid] = identity
            for child in reader.children(pid):
                pending.append((child, belongs_to_agent))
        except _StartupProcessGone:
            continue
        except subprocess.TimeoutExpired:
            return PaneProcessesUnreadable()
        except (HerdrFailure, OSError, ValueError, IndexError, subprocess.SubprocessError):
            unreadable = True
    if live:
        return AgentProcessPresent(tuple(live.values()))
    if unreadable:
        return PaneProcessesUnreadable()
    try:
        # Read foreground ownership after walking the tree, not before launch.
        foreground = reader.foreground(shell.pid)
    except (HerdrFailure, OSError, ValueError, IndexError, subprocess.SubprocessError):
        return PaneProcessesUnreadable()
    if not commands and foreground == shell.pid:
        return PaneAtShell()
    return PaneRunning(tuple(commands))


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
    if _platform() == "darwin":
        return _darwin_attached_clients(name)
    try:
        processes = list(proc_root().iterdir())
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
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError as error:
            return ClientsUnreadable(str(error))
        if (session == name and not command) or command == ["session", "attach", name]:
            count += 1
    return ClientsAttached(count) if count else NoClientAttached()


def _session_socket(name: str) -> Path:
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "herdr"
    session_directory = config if name == "default" else config / "sessions" / name
    return session_directory / "herdr.sock"


class DarwinProcessIdentity(NamedTuple):
    """Registry-compatible birth time and controlling-terminal presence (0 or 1)."""

    started: str
    terminal: int


def darwin_process_identity(pid: int) -> DarwinProcessIdentity:
    result = run_program([os.environ.get("HERDR_PS", "ps"), "-o", "lstart=,tty=", "-p", str(pid)],
                         timeout=5, environment={**os.environ, "LC_ALL": "C", "TZ": "UTC"})
    require_success(result, f"process {pid} identity")
    fields = result.stdout.strip().rsplit(maxsplit=1)
    if len(fields) != 2 or not re.fullmatch(
            r"[A-Z][a-z]{2} [A-Z][a-z]{2} +[0-9]{1,2} [0-9]{2}:[0-9]{2}:[0-9]{2} [0-9]{4}", fields[0]):
        raise HerdrFailure(f"process {pid}: cannot read start time and controlling terminal")
    return DarwinProcessIdentity(fields[0], 0 if fields[1] in {"??", "?", "-"} else 1)


def darwin_open_files(pid: int) -> list[Path]:
    result = run_program([os.environ.get("HERDR_LSOF", "lsof"), "-a", "-p", str(pid), "-Fn"])
    if result.returncode == 1 and not result.stdout and not result.stderr:
        return []
    require_success(result, f"process {pid} open files")
    return [Path(field[1:]) for field in result.stdout.splitlines() if field.startswith("n/")]


def _require_darwin_process_read(result: subprocess.CompletedProcess[str], pid: int, context: str) -> None:
    if result.returncode != 0 and not result.stdout and not result.stderr:
        try:
            os.kill(pid, 0)
        except ProcessLookupError as error:
            raise _DarwinProcessGone(f"process {pid}: exited") from error
        except OSError:
            pass
    require_success(result, context)


def darwin_process_environment(pid: int) -> dict[str, str]:
    """Read only the environment following a stable, separately read argv."""
    ps = os.environ.get("HERDR_PS", "ps")
    command = run_program([ps, "-o", "command=", "-p", str(pid)], timeout=5)
    _require_darwin_process_read(command, pid, f"process {pid} command")
    arguments = command.stdout.rstrip("\r\n").lstrip()
    if arguments.startswith("(") and arguments.endswith(")"):
        raise HerdrFailure(f"process {pid}: arguments are unreadable")
    expanded = run_program([ps, "eww", "-o", "command=", "-p", str(pid)], timeout=5)
    _require_darwin_process_read(expanded, pid, f"process {pid} environment")
    confirmed = run_program([ps, "-o", "command=", "-p", str(pid)], timeout=5)
    _require_darwin_process_read(confirmed, pid, f"process {pid} command confirmation")
    text = expanded.stdout.rstrip("\r\n").lstrip()
    if not arguments or confirmed.stdout != command.stdout or not text.startswith(arguments):
        raise HerdrFailure(f"process {pid}: environment does not follow stable command")
    environment_text = text[len(arguments):]
    if environment_text and not environment_text[0].isspace():
        raise HerdrFailure(f"process {pid}: environment does not follow command boundary")
    # ps cannot distinguish " KEY=" inside a value; splitting it errs toward refusing a server.
    entries = list(re.finditer(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=", environment_text))
    return {entry[1]: environment_text[entry.end():entries[index + 1].start() if index + 1 < len(entries)
                                     else len(environment_text)].rstrip()
            for index, entry in enumerate(entries)}


@dataclass(frozen=True)
class _BoundUnixSocket:
    pid: int
    path: Path


def _darwin_bound_sockets(*, timeout: float = 10.0) -> list[_BoundUnixSocket]:
    # Darwin lsof reports a bound Unix sun_path; unbound client endpoints have
    # arrow names. TCP state fields are not emitted for these Unix sockets.
    result = run_program([os.environ.get("HERDR_LSOF", "lsof"), "-n", "-P", "-a", "-U",
                          "-u", str(os.getuid()), "-F", "pfn"], timeout=timeout)
    # lsof exits 1 with no output when no files match.
    if result.returncode == 1 and not result.stdout and not result.stderr:
        return []
    require_success(result, "herdr listening sockets")
    sockets: list[_BoundUnixSocket] = []
    pid = 0
    path = ""
    for field in [*result.stdout.splitlines(), "f"]:
        if field.startswith(("p", "f")):
            if pid > 0 and path.startswith("/"):
                sockets.append(_BoundUnixSocket(pid, Path(path)))
            path = ""
        if field.startswith("p"):
            pid = int(field[1:]) if field[1:].isdecimal() else 0
        elif field.startswith("n"):
            path = field[1:]
    return sockets


def server_environment(name: str) -> dict[str, str]:
    """Read the environment of the process holding this session's listener."""
    context = f"herdr session {name} server environment"
    socket_path = str(_session_socket(name))
    if _platform() == "darwin":
        try:
            holders = {listener.pid for listener in _darwin_bound_sockets() if str(listener.path) == socket_path}
            if len(holders) != 1:
                raise HerdrFailure(f"expected one listening socket holder, found {len(holders)}")
            return darwin_process_environment(holders.pop())
        except (HerdrFailure, subprocess.TimeoutExpired) as error:
            raise HerdrFailure(f"{context}: {error}") from error
    try:
        sockets = proc_root().joinpath("net", "unix").read_text().splitlines()
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
        candidates = list(proc_root().iterdir())
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


class _PaneListStatus(Enum):
    READABLE = "readable"
    UNREADABLE = "unreadable"


class _LivePanePlaces:
    """One server read, with retained shell identities for panes whose ids changed."""

    def __init__(self, session: str) -> None:
        self.session: str = session
        self.panes: dict[str, PaneIdentity] = {}
        self.pane_list_status: _PaneListStatus = _PaneListStatus.UNREADABLE
        try:
            self.panes = {pane["pane_id"]: pane for pane in Herdr(session).panes()}
        except (HerdrFailure, OSError, ValueError):
            pass
        else:
            self.pane_list_status = _PaneListStatus.READABLE
        self.shell_places: dict[int, HerdrPlace] = {}
        self.shells_read: bool = False

    def shells(self) -> dict[int, HerdrPlace]:
        if self.pane_list_status is _PaneListStatus.UNREADABLE:
            return {}
        if not self.shells_read:
            self.shells_read = True
            for pane in self.panes:
                try:
                    info = json_object(Herdr(self.session).result("pane", "process-info", "--pane", pane)
                                       .get("process_info"), "herdr pane process info")
                except HerdrFailure:
                    continue
                shell = info.get("shell_pid")
                if isinstance(shell, int) and not isinstance(shell, bool) and shell > 0:
                    self.shell_places[shell] = HerdrPlace(self.session, pane)
        return self.shell_places

    def place(self, pid: int, environment: dict[str, str], parent: Callable[[int], int]) -> HerdrPlace | NotInHerdr:
        launch_pane = environment.get("HERDR_PANE_ID", "")
        if not launch_pane or environment.get("TMUX_PANE"):
            return NotInHerdr()
        if self.pane_list_status is _PaneListStatus.UNREADABLE:
            return HerdrPlace(self.session, launch_pane)
        if launch_pane in self.panes:
            return HerdrPlace(self.session, self.panes[launch_pane]["pane_id"])
        shells = self.shells()
        seen: set[int] = set()
        while pid > 1 and pid not in seen:
            if pid in shells:
                return shells[pid]
            seen.add(pid)
            try:
                pid = parent(pid)
            except (OSError, ValueError, IndexError):
                break
        return NotInHerdr()


_live_pane_reader: ContextVar[Callable[[str], _LivePanePlaces]] = ContextVar(
    "herdr_live_pane_reader", default=_LivePanePlaces)


@contextmanager
def pane_snapshot() -> Generator[Callable[[str], list[PaneIdentity]]]:
    """Share live panes and retained shells; yield the pane reader for report display."""
    reader = cache(_live_pane_reader.get())
    token = _live_pane_reader.set(reader)
    try:
        yield lambda session: [pane.copy() for pane in reader(session).panes.values()]
    finally:
        _live_pane_reader.reset(token)


def linux_process_parent(pid: int) -> int:
    """Read the parent pid from the Linux process root."""
    stat = (proc_root() / str(pid) / "stat").read_text()
    return int(stat[stat.rindex(")") + 1:].split()[1])


@pane_snapshot()
def herdr_place_of(pid: int) -> HerdrPlace | NotInHerdr:
    if _platform() == "darwin":
        return next((process.place for process in herdr_processes() if process.pid == pid), NotInHerdr())
    try:
        environment = _process_environment(proc_root() / str(pid))
        if environment.get("TMUX_PANE"):
            return NotInHerdr()
        pane = environment.get("HERDR_PANE_ID", "")
        if not pane:
            return NotInHerdr()
        candidates = [path for path in proc_root().iterdir() if path.name.isdecimal()]
    except OSError:
        return NotInHerdr()
    session = _process_sessions(candidates, _live_server_processes(candidates)).get(pid, "")
    return (_live_pane_reader.get()(session).place(pid, environment, linux_process_parent)
            if session else NotInHerdr())


@pane_snapshot()
def herdr_places() -> dict[int, HerdrPlace]:
    """Every herdr process's place from one scan, by pid: herdr_place_of for many pids at once."""
    if _platform() == "darwin":
        return {process.pid: process.place for process in herdr_processes()}
    try:
        candidates = [path for path in proc_root().iterdir() if path.name.isdecimal()]
    except OSError:
        return {}
    places: dict[int, HerdrPlace] = {}
    for pid, session in _process_sessions(candidates, _live_server_processes(candidates)).items():
        try:
            environment = _process_environment(proc_root() / str(pid))
        except OSError:
            continue
        place = _live_pane_reader.get()(session).place(pid, environment, linux_process_parent)
        if isinstance(place, HerdrPlace):
            places[pid] = place
    return places


def _live_server_processes(candidates: list[Path]) -> dict[int, str]:
    """Identify servers by listening socket ownership, independent of argv."""
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "herdr"
    try:
        lines = proc_root().joinpath("net", "unix").read_text().splitlines()
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


def systemd_cgroups(process: Path) -> list[tuple[str, Path]]:
    """Read systemd memberships; callers decide whether a read error is uncertainty."""
    lines = process.joinpath("cgroup").read_text().splitlines()
    groups: list[tuple[str, Path]] = []
    for line in lines:
        fields = line.split(":", 2)
        if len(fields) == 3 and fields[1] in ("", "name=systemd") and fields[2].startswith("/"):
            groups.append((fields[0], Path(fields[2])))
    return groups


def _systemd_cgroups(process: Path) -> list[tuple[str, Path]]:
    """Discovery outside a stop tolerates processes disappearing during a scan."""
    try:
        return systemd_cgroups(process)
    except OSError:
        return []


def _pane_shell_pids(session: str) -> list[int]:
    """Read the shell identities retained in the live server's PTY runtimes."""
    return list(_live_pane_reader.get()(session).shells())


def pane_scopes(groups: list[tuple[str, Path]], slice_name: str) -> list[tuple[str, Path]]:
    """Return the populated pane scopes in the configured agent slice."""
    return [(hierarchy, scope) for hierarchy, path in groups
            for scope in (path, *path.parents) if scope.parent.name == slice_name]


def _add_scope_owners(owners: dict[tuple[str, Path], set[str]], pids: Sequence[int],
                      sessions: dict[int, str], slice_name: str) -> None:
    """Record the session of each matched process in its pane scope."""
    for pid in pids:
        for scope in pane_scopes(_systemd_cgroups(proc_root() / str(pid)), slice_name):
            owners.setdefault(scope, set()).add(sessions[pid])


def _process_sessions(candidates: list[Path], servers: dict[int, str]) -> dict[int, str]:
    """Associate descendants and handoff survivors with socket-owned sessions."""
    if not servers:
        return {}
    read_slice = cache(agents_slice)
    unit_groups: dict[tuple[str, Path], str] = {}
    shared_groups: dict[tuple[str, Path], set[str]] = {}
    for pid, session in servers.items():
        try:
            unit = server_unit(session)
        except ValueError:
            continue
        for hierarchy, path in _systemd_cgroups(proc_root() / str(pid)):
            if unit in path.parts:
                root = Path(*path.parts[:path.parts.index(unit) + 1])
                unit_groups[hierarchy, root] = session
            else:
                shared_groups.setdefault((hierarchy, path), set()).add(session)
    children: dict[int, list[Path]] = {}
    for process in candidates:
        try:
            parent = linux_process_parent(int(process.name))
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
        # A pane shell in its own scope is not in the unit's cgroup. After a
        # handoff, its scope is the only membership that it keeps.
        loose: dict[int, list[tuple[str, Path]]] = {}
        for process in candidates:
            pid = int(process.name)
            if pid in sessions:
                continue
            try:
                if process.stat().st_uid != os.getuid():
                    continue
            except OSError:
                continue
            groups = _systemd_cgroups(process)
            for hierarchy, path in groups:
                shared_sessions.update(shared_groups.get((hierarchy, path), set()))
                for ancestor in (path, *path.parents):
                    session = unit_groups.get((hierarchy, ancestor), "")
                    if session:
                        sessions[pid] = session
                        break
                if pid in sessions:
                    break
            if pid not in sessions and (scopes := pane_scopes(groups, read_slice())):
                loose[pid] = scopes
        # A pane scope holds one pane's processes, as the unit's cgroup held
        # them before. Only a scope that no matched process explains needs the
        # unit servers' pane shells.
        owners: dict[tuple[str, Path], set[str]] = {}
        if loose:
            _add_scope_owners(owners, [pid for pid in sessions if pid not in servers], sessions, read_slice())
            if any(not any(scope in owners for scope in scopes) for scopes in loose.values()):
                shared_sessions.update(unit_groups.values())
        walked = set(sessions)
        # Handoff retains child_pid in each imported PTY runtime. Unlike a
        # shared cgroup or HERDR_SESSION, this identifies the actual pane shell.
        for session in shared_sessions:
            for shell in _pane_shell_pids(session):
                descendants = [(proc_root() / str(shell), session)]
                while descendants:
                    process, owner = descendants.pop()
                    pid = int(process.name)
                    if pid in sessions:
                        continue
                    sessions[pid] = owner
                    descendants.extend((child, owner) for child in children.get(pid, []))
        if loose:
            _add_scope_owners(owners, [pid for pid in sessions if pid not in walked], sessions, read_slice())
            for pid, scopes in loose.items():
                found = {session for scope in scopes for session in owners.get(scope, set())}
                # A scope with two owners holds a server started inside a pane; it names neither.
                if pid not in sessions and len(found) == 1:
                    sessions[pid] = found.pop()
    return {pid: session for pid, session in sessions.items() if pid not in servers}


@dataclass(frozen=True)
class ReadProcessArguments:
    """The executable and parsed argument remainder from a readable command line."""

    argv: tuple[str, ...]


@dataclass(frozen=True)
class UnreadableProcessArguments:
    reason: str


@dataclass(frozen=True)
class _UserProcess:
    pid: int
    parent: int
    executable: str
    arguments: ReadProcessArguments | UnreadableProcessArguments


def _darwin_processes(*, complete: bool = False, timeout: float | _ProcessReadDeadline = PROGRAM_TIMEOUT_SECONDS) -> list[_UserProcess]:
    result = run_program([os.environ.get("HERDR_PS", "ps"), "-U", str(os.getuid()),
                          "-o", "pid=,ppid=,uid=,comm="],
                         timeout=timeout.remaining() if isinstance(timeout, _ProcessReadDeadline) else timeout)
    require_success(result, "user processes")
    # Both fields can contain spaces. Separate batched columns retain an unambiguous
    # executable boundary and keep unreadable or changed argv as an explicit state.
    command_result = run_program([os.environ.get("HERDR_PS", "ps"), "-U", str(os.getuid()),
                                  "-o", "pid=,command="],
                                 timeout=timeout.remaining() if isinstance(timeout, _ProcessReadDeadline) else timeout)
    require_success(command_result, "user process arguments")
    commands: dict[int, str] = {}
    for line in command_result.stdout.splitlines():
        fields = line.split(maxsplit=1)
        if len(fields) == 2 and fields[0].isdecimal():
            commands[int(fields[0])] = fields[1]
        elif complete and line.strip():
            raise HerdrFailure("user process arguments are incomplete")
    processes: list[_UserProcess] = []
    for line in result.stdout.splitlines():
        fields = line.split(maxsplit=3)
        if len(fields) != 4 or not all(field.isdecimal() for field in fields[:3]):
            if complete and line.strip():
                raise HerdrFailure("user process descendants are incomplete")
            continue
        pid, parent, uid = map(int, fields[:3])
        if uid != os.getuid() or pid <= 0:
            continue
        executable = fields[3].rstrip()
        if pid not in commands:
            try:
                current = run_program([os.environ.get("HERDR_PS", "ps"), "-o", "command=", "-p", str(pid)],
                                      timeout=timeout.remaining() if isinstance(timeout, _ProcessReadDeadline) else timeout)
                _require_darwin_process_read(current, pid, f"process {pid} command")
            except _DarwinProcessGone:
                continue
            except subprocess.TimeoutExpired:
                if isinstance(timeout, _ProcessReadDeadline):
                    raise
                continue
            except HerdrFailure:
                pass
        command = commands.get(pid, "")
        arguments: ReadProcessArguments | UnreadableProcessArguments = UnreadableProcessArguments(
            f"process {pid}: arguments are unreadable or changed")
        if command == executable or command.startswith(executable + " "):
            try:
                arguments = ReadProcessArguments((executable, *shlex.split(command[len(executable):])))
            except ValueError:
                pass
        processes.append(_UserProcess(pid, parent, executable, arguments))
    if complete and set(commands) != {process.pid for process in processes}:
        raise HerdrFailure("user process descendants changed or are incomplete")
    return processes


def _darwin_attached_clients(name: str) -> ClientsAttached | NoClientAttached | ClientsUnreadable:
    try:
        processes = _darwin_processes()
    except (HerdrFailure, subprocess.TimeoutExpired, OSError) as error:
        return ClientsUnreadable(str(error))
    count = 0
    for process in processes:
        if isinstance(process.arguments, UnreadableProcessArguments):
            if Path(process.executable).name == "herdr":
                return ClientsUnreadable(process.arguments.reason)
            continue
        arguments = list(process.arguments.argv)
        session, command = _herdr_command(arguments, "default")
        if session and not command:
            try:
                environment = darwin_process_environment(process.pid)
            except _DarwinProcessGone:
                continue
            except (HerdrFailure, subprocess.TimeoutExpired, OSError) as error:
                return ClientsUnreadable(str(error))
            session, command = _herdr_command(arguments, environment.get("HERDR_SESSION") or "default")
        if (session == name and not command) or command == ["session", "attach", name]:
            count += 1
    return ClientsAttached(count) if count else NoClientAttached()


def _darwin_process_cwd(pid: int) -> str | UnreadableCwd:
    try:
        result = run_program([os.environ.get("HERDR_LSOF", "lsof"), "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
        require_success(result, f"process {pid} cwd")
    except (HerdrFailure, subprocess.TimeoutExpired) as error:
        return UnreadableCwd(str(error))
    directories = [field[1:] for field in result.stdout.splitlines() if field.startswith("n/")]
    if len(directories) != 1:
        return UnreadableCwd(f"process {pid}: expected one cwd, found {len(directories)}")
    return directories[0]


def _darwin_herdr_processes() -> list[HerdrProcess]:
    try:
        candidates = _darwin_processes()
        listeners = _darwin_bound_sockets()
    except (HerdrFailure, subprocess.TimeoutExpired):
        return []
    config = _session_socket("default").parent
    holders: dict[str, set[int]] = {}
    owned = {process.pid for process in candidates}
    for listener in listeners:
        if listener.pid not in owned:
            continue
        if listener.path == config / "herdr.sock":
            session = "default"
        elif listener.path.name == "herdr.sock" and listener.path.parent.parent == config / "sessions":
            session = listener.path.parent.name
        else:
            continue
        holders.setdefault(session, set()).add(listener.pid)
    servers = {next(iter(pids)): session for session, pids in holders.items() if len(pids) == 1}
    children: dict[int, list[int]] = {}
    by_pid = {process.pid: process for process in candidates}
    for process in candidates:
        children.setdefault(process.parent, []).append(process.pid)
    sessions: dict[int, str] = {}
    for server, session in servers.items():
        # A live handoff preserves pane shell PIDs even after reparenting.
        pending = [*children.get(server, []), *_pane_shell_pids(session)]
        while pending:
            pid = pending.pop()
            if pid in sessions or pid not in by_pid or pid in servers:
                continue
            sessions[pid] = session
            pending.extend(children.get(pid, []))
    processes: list[HerdrProcess] = []
    for pid, session in sessions.items():
        try:
            arguments = by_pid[pid].arguments
            if isinstance(arguments, UnreadableProcessArguments):
                continue
            environment = darwin_process_environment(pid)
            place = _live_pane_reader.get()(session).place(
                pid, environment, lambda ident: by_pid[ident].parent if ident in by_pid else 0)
            if isinstance(place, NotInHerdr):
                continue
            marks = _process_marks(pid, environment, list(arguments.argv), place)
            processes.append(HerdrProcess(pid, place, marks,
                                          Path(by_pid[pid].executable).name, _darwin_process_cwd(pid)))
        except (HerdrFailure, subprocess.TimeoutExpired):
            continue
    return processes


def _process_marks(pid: int, environment: dict[str, str], arguments: list[str], place: HerdrPlace) -> dict[str, str]:
    """Project proved agent identities for reports; retain membership when proof is unavailable."""
    marks = {key: environment[key] for key in NATIVE_ID_MARKS if key in environment}
    kind = "codex" if is_codex_process(arguments) else "claude" if is_claude_process(arguments) else ""
    if kind:
        try:
            observed = codex_pane_process(pid, place) if kind == "codex" else native_process(pid, kind)
            marks["CODEX_THREAD_ID" if kind == "codex" else "CLAUDE_CODE_SESSION_ID"] = observed.current_identity
        except CodexPaneStarting:
            _ = marks.pop("CODEX_THREAD_ID", None)
            return marks
        except (HerdrFailure, OSError, ValueError):
            return marks
    return marks


@dataclass(frozen=True)
class UnitTab:
    """A tab's unit identity and its current staff parent."""

    row: staff.StaffRow
    parent: staff.StaffRow


@dataclass(frozen=True)
class OtherTab:
    """The native session does not identify a current unit."""


class UnitTabClassifier:
    """Classify units through their current native sessions and staff parents."""

    by_pane: dict[str, UnitTab]

    def __init__(self, session: str, processes: Sequence[HerdrProcess], record: staff.StaffSnapshot) -> None:
        from scripts.production import staff

        parents = {row.id: row for row in staff.active(record, "showrunner")}
        units: dict[str, UnitTab] = {row.current_session: UnitTab(row, parents[row.parent.id]) for row in staff.active(record, "unit")
                                     if isinstance(row.parent, staff.ReportsTo) and row.parent.id in parents}
        self.by_pane = {process.place.pane: units[ident] for process in processes
                        if process.place.session == session
                        and (ident := process.marks.get("CODEX_THREAD_ID")
                             or process.marks.get("CLAUDE_CODE_SESSION_ID")) in units}

    def classify(self, panes: Sequence[str]) -> UnitTab | OtherTab:
        for pane in panes:
            if pane in self.by_pane:
                return self.by_pane[pane]
        return OtherTab()


@pane_snapshot()
def _scan_herdr_processes() -> list[HerdrProcess]:
    if _platform() == "darwin":
        return _darwin_herdr_processes()
    processes: list[HerdrProcess] = []
    try:
        candidates = [path for path in proc_root().iterdir() if path.name.isdecimal()]
    except OSError:
        return processes
    sessions = _process_sessions(candidates, _live_server_processes(candidates))
    for pid, session in sessions.items():
        process = proc_root() / str(pid)
        try:
            if process.stat().st_uid != os.getuid():
                continue
            environment = _process_environment(process)
            place = _live_pane_reader.get()(session).place(pid, environment, linux_process_parent)
            if isinstance(place, NotInHerdr):
                continue
            arguments = _process_arguments(process)
            if not arguments:
                continue
            # Placement and marks remain evidence when a moved/deleted worktree
            # makes cwd unreadable, just as on Darwin.
            cwd: str | UnreadableCwd
            try:
                cwd = str(process.joinpath("cwd").resolve(strict=True))
            except (OSError, ValueError, RuntimeError) as error:
                cwd = UnreadableCwd(str(error))
            marks = _process_marks(pid, environment, arguments, place)
            processes.append(HerdrProcess(pid, place, marks, Path(arguments[0]).name, cwd))
        except (OSError, ValueError, RuntimeError):
            continue
    return processes


@dataclass
class _ProcessSnapshot:
    """A report-only reader whose lifetime ends for all copied contexts at once."""
    parent: Callable[[], list[HerdrProcess]]
    active: bool = True
    saved: Callable[[], list[HerdrProcess]] = field(init=False)

    def __post_init__(self) -> None:
        self.saved = cache(self.parent)

    def __call__(self) -> list[HerdrProcess]:
        reader: Callable[[], list[HerdrProcess]] = self
        while isinstance(reader, _ProcessSnapshot):
            if not reader.active:
                return _scan_herdr_processes()
            reader = reader.parent
        return self.saved()


# Only report reads may share a snapshot. Action decisions always scan afresh.
_process_reader: ContextVar[Callable[[], list[HerdrProcess]]] = ContextVar(
    "herdr_process_reader", default=lambda: _scan_herdr_processes())
_process_action: ContextVar[bool] = ContextVar("herdr_process_action", default=False)


@contextmanager
def process_snapshot() -> Generator[None]:
    """Share one successful report scan while this scope is open.

    Errors propagate and are retried. Closed ancestors also close descendants.
    Nested report scopes share their parent's scan; action scopes never cache.
    """
    if _process_action.get():
        yield
        return
    reader = _ProcessSnapshot(_process_reader.get())
    token = _process_reader.set(reader)
    try:
        yield
    finally:
        reader.active = False
        _process_reader.reset(token)


@contextmanager
def process_action() -> Generator[None]:
    """Read fresh for an action decision and retire reports from before it.

    Enter before classifying owners or members for placement, moves or recovery.
    Callers that only report can keep their shared scan until an action is needed.
    """
    reader = _process_reader.get()
    while isinstance(reader, _ProcessSnapshot):
        reader.active = False
        reader = reader.parent
    token = _process_reader.set(lambda: _scan_herdr_processes())
    action = _process_action.set(True)
    try:
        yield
    finally:
        _process_action.reset(action)
        _process_reader.reset(token)


def herdr_processes(*, membership: StaffLabels | StaffLabelsRead = StaffLabelsRead.PENDING) -> list[HerdrProcess]:
    """Read current processes, or the snapshot held by an explicit scope."""
    labels = RecordedLabels(membership)
    return [HerdrProcess(row.pid, row.place, labels.resolve(row.pid, row.marks),
                         row.command, row.working_directory)
            for row in _process_reader.get()()]


def _check_agent_arguments(kind: str, agent_arguments: list[str]) -> None:
    if agent_arguments and agent_arguments[0] == kind:
        del agent_arguments[0]


def _agent_name(name: str) -> str:
    mapped = re.sub(r"[^a-z0-9_-]", "-", name.lower())
    if not mapped or not "a" <= mapped[0] <= "z":
        mapped = "a-" + mapped
    if len(mapped) <= 32:
        return mapped
    return mapped[:23] + "-" + hashlib.sha256(mapped.encode()).hexdigest()[:8]


@dataclass(frozen=True)
class AgentLaunched:
    pane: str
    outcome: PromptSubmitOutcome | PromptSubmissionNotRequired


@dataclass(frozen=True)
class PreparedAgentEnvironment:
    """An environment already proved for the exact directory to be opened."""

    directory: Path
    env: dict[str, str]
    unset_environment: frozenset[str] = NO_ENVIRONMENT_REMOVALS


@dataclass(frozen=True)
class NewTab:
    space: str
    title: str


@dataclass(frozen=True)
class SplitPane:
    of_pane: str
    direction: Literal["right", "down"]
    ratio: float
    label: str

@dataclass(frozen=True)
class ExistingPane:
    pane: str
    terminal: str


def launch_agent(name: str, space: str, title: str, cwd: str, environment: dict[str, str],
                 kind: str, agent_arguments: list[str], opening: OpeningPrompt | NoOpeningPrompt,
                 timeout_ms: int, *, agent: str, unset_environment: frozenset[str] = NO_ENVIRONMENT_REMOVALS,
                 reported_unknown: str = "") -> AgentLaunched | nix_env.Refused:
    """Prove the start environment before creating a pane or starting an agent.

    herdr owns the executable selected by --kind. The pane carries the loaded
    environment, so its canonical executable inherits it, including on resume.
    Caller account choices and session labels take precedence over inherited ones.
    """
    _check_agent_arguments(kind, agent_arguments)
    session_id = next((agent_arguments[index + 1] for index, argument in enumerate(agent_arguments[:-1])
                       if argument in {"--resume", "--session-id", "resume"}), "")
    prepared = prepare_session_environment(session_id, cwd, environment, unset_environment)
    if isinstance(prepared, nix_env.Refused):
        return prepared
    return launch_prepared_agent(name, NewTab(space, title), prepared, kind, agent_arguments, opening, timeout_ms,
                                 agent=agent, reported_unknown=reported_unknown)


def agent_base_environment(environment: dict[str, str], unset_environment: frozenset[str]) -> dict[str, str]:
    """Keep the target account's choices while dropping the launcher's identity."""
    base_env = dict(os.environ)
    for key in tuple(base_env):
        # scripts/lib/unit_env.sh and unit_runs.environ map every PLAN_DELEGATE_ alias to UNIT_.
        if (key.startswith(("HERDR_", "CODEX_", "UNIT_", "PLAN_DELEGATE_", "SHOWRUNNER_", "CLAUDE_", "STAFF_", "NIX_HEAL_"))
                or key in {"ANTHROPIC_API_KEY", "SHELL", "TMUX", "TMUX_PANE"}):
            del base_env[key]
    base_env.update(environment)
    for identity in ("STAFF_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
        _ = base_env.pop(identity, None)
    for key in unset_environment:
        _ = base_env.pop(key, None)
    return child_environment(base_env)


def prepare_agent_environment(cwd: str, environment: dict[str, str],
                              unset_environment: frozenset[str] = NO_ENVIRONMENT_REMOVALS) -> PreparedAgentEnvironment | nix_env.Refused:
    """Load the environment once before the launch."""
    loaded = nix_env.ensure(Path(cwd), agent_base_environment(environment, unset_environment))
    if isinstance(loaded, nix_env.Refused):
        print(f"herdr {cwd}: environment is unavailable: {loaded}", file=sys.stderr)
        launch_environment = agent_base_environment(environment, unset_environment)
    else:
        launch_environment = {key: value for key, value in loaded.env.items()
                              if key not in unset_environment
                              and key not in {"STAFF_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"}}
    return PreparedAgentEnvironment(Path(cwd).resolve(), launch_environment,
                                    unset_environment | (os.environ.keys() - launch_environment.keys()))


def prepare_session_environment(session_id: str, cwd: str, environment: dict[str, str],
                                unset_variables: frozenset[str]) -> PreparedAgentEnvironment | nix_env.Refused:
    """Use the healer's environment only for its selected session and folder."""
    if (os.environ.get(nix_env.HEAL_SESSION_ENV) == session_id
            and os.environ.get(nix_env.HEAL_DIRECTORY_ENV) == str(Path(cwd).resolve())):
        launch_environment = agent_base_environment(environment, unset_variables)
        return PreparedAgentEnvironment(Path(cwd).resolve(), launch_environment,
                                        unset_variables | (os.environ.keys() - launch_environment.keys()))
    return prepare_agent_environment(cwd, environment, unset_variables)


class AgentNameSource(Enum):
    MEMBER_REFERENCE = "member_reference"


class CodexLaunchRead(Enum):
    CURRENT = "read Codex at launch"


class LaunchClientSource(Enum):
    NEW = "create a client for this launch"


def agent_alias_script(kind: str) -> str:
    """Clear the agent alias before the shell starts the agent."""
    return f"unalias {kind} 2>/dev/null; "


def launch_prepared_agent(name: str, destination: NewTab | SplitPane | ExistingPane, prepared: PreparedAgentEnvironment,
                          kind: str, agent_arguments: list[str], opening: OpeningPrompt | NoOpeningPrompt,
                          timeout_ms: int, *, agent: str, reported_unknown: str = "",
                          agent_name: str | AgentNameSource = AgentNameSource.MEMBER_REFERENCE,
                          codex: CodexConversations | CodexLaunchRead = CodexLaunchRead.CURRENT,
                          client: Herdr | LaunchClientSource = LaunchClientSource.NEW) -> AgentLaunched:
    """Launch the prepared environment. Use agent for staff lookup and agent_name for the server name."""
    _check_agent_arguments(kind, agent_arguments)
    if kind == "claude":
        agent_arguments = models_registry.claude_arguments(agent_arguments, prepared.env)
        try:
            trust_claude_launch(prepared.directory, Path(prepared.env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"))
        except HerdrFailure as error:
            print(str(error), file=sys.stderr)
            return AgentLaunched("", ClaudeTrustNotWritten(str(error)))
    session_id = next((agent_arguments[index + 1] for index, argument in enumerate(agent_arguments[:-1])
                       if argument in {"--resume", "--session-id", "resume"}), "")
    reuse_client = not isinstance(client, LaunchClientSource)
    client = Herdr(name) if isinstance(client, LaunchClientSource) else client
    member = ""
    try:
        membership = _tab_staff()
        owner = membership.by_member_reference(session_id or agent) or membership.by_path(agent)
        if owner is not None:
            session_id = owner.current_session
            member = owner.id
        observed = (read_conversation(session_id, kind,
                    **({"codex": codex} if isinstance(codex, CodexConversations) else {}))
                    if session_id else StoppedConversation())
    except (HerdrFailure, OSError, ValueError) as error:
        print(f"herdr {session_id}: live session evidence is unknown: {error}", file=sys.stderr)
    else:
        if isinstance(observed, LiveConversation):
            raise HerdrFailure(f"would start a second copy of running session {session_id}", "already_running")
        if isinstance(observed, UnknownConversation) and not reported_unknown:
            conversation_note(observed)
    if isinstance(destination, NewTab):
        pane = (client.open_tab(destination.space, destination.title, str(prepared.directory), prepared.env,
                               **({"member": member} if member else {})) if reuse_client else
                open_tab(name, destination.space, destination.title, str(prepared.directory), prepared.env,
                         **({"member": member} if member else {})))
    elif isinstance(destination, SplitPane):
        if not 0 < destination.ratio < 1 or destination.direction not in {"right", "down"}:
            print(f"herdr {name}: split direction or ratio adjusted to backend limits", file=sys.stderr)
        options = ["pane", "split", destination.of_pane, "--direction",
                   destination.direction if destination.direction in {"right", "down"} else "right",
                   "--ratio", str(min(0.99, max(0.01, destination.ratio))), "--cwd", str(prepared.directory), "--no-focus"]
        for key, value in _pane_environment(prepared.env).items():
            options.extend(["--env", f"{key}={value}"])
        result = client.result(*options)
        pane = json_text(json_object(result.get("pane"), "herdr split pane").get("pane_id"), "herdr split pane id")
    else:
        panes = client.panes()
        moved = next((row for row in panes if destination.terminal
                      and row.get("terminal_id") == destination.terminal), None)
        pane = moved["pane_id"] if moved is not None else destination.pane
    try:
        if isinstance(destination, SplitPane):
            client.label_pane(pane, destination.label)
        script = agent_alias_script(kind)
        if isinstance(destination, ExistingPane):
            # Set the environment in the existing shell before the agent starts.
            environment = _pane_environment(prepared.env)
            exports = shlex.join([f"{key}={value}" for key, value in environment.items()
                                  if not key.startswith("HERDR_") or key == "HERDR_AGENT_PANE"])
            removals = (prepared.unset_environment | {
                "STAFF_ID", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
                "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY",
            }) - {"TERM", "COLORTERM"}
            script += ("for legacy_label in $(env); do case $legacy_label in SHOWRUNNER_*=*) "
                      'unset "${legacy_label%%=*}";; esac; done; '
                      f"cd -- {shlex.quote(str(prepared.directory))} && "
                      f"unset {shlex.join(sorted(key for key in removals if not key.startswith('HERDR_') and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key)))} && export {exports}")
        client.run_in_pane(pane, ["eval", script],
                           terminal=destination.terminal if isinstance(destination, ExistingPane) else "")
        server_name = agent_name if isinstance(agent_name, str) else agent
        forget_pane_evidence(name, pane)
        try:
            outcome = start_agent(name, pane, kind, agent_arguments, opening, timeout_ms, agent=server_name)
        finally:
            forget_pane_evidence(name, pane)
    except AgentExited:
        if isinstance(destination, NewTab):
            close_agent(name, pane)
        elif isinstance(destination, SplitPane):
            _ = client.close_retained_pane(pane)
        raise
    except (HerdrFailure, OSError, subprocess.SubprocessError) as error:
        if isinstance(error, HerdrFailure) and error.code == "wrong_target":
            raise
        print(f"herdr {name}:{pane}: start incomplete: {error}; pane retained", file=sys.stderr)
        return AgentLaunched(pane, PromptNotSubmitted(str(error)))
    if isinstance(outcome, AgentNotStarted) and not isinstance(destination, ExistingPane):
        if isinstance(destination, NewTab):
            close_agent(name, pane)
        else:
            _ = client.close_retained_pane(pane)
        print(f"herdr {name}:{pane}: agent start failed: {outcome.reason}; pane closed", file=sys.stderr)
    return AgentLaunched(pane, outcome)


def start_agent(name: str, pane: str, kind: str, agent_arguments: list[str],
                opening: OpeningPrompt | NoOpeningPrompt, timeout_ms: int, *, agent: str) -> PromptSubmitOutcome | PromptSubmissionNotRequired:
    """Start the agent named `agent` in the named session.

    `agent` is the full agent name, which can include the production prefix.
    """
    _check_agent_arguments(kind, agent_arguments)
    return Herdr(name).start_agent(agent, pane, kind=kind, agent_arguments=agent_arguments,
                                  opening=opening, timeout_ms=timeout_ms)
