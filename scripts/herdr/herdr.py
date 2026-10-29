#!/usr/bin/env python3
"""Run named herdr sessions without choosing accounts or answering dialogs.

Process discovery reads HERDR_PANE_ID only in descendants of listening herdr servers
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

import json
import os
import re
import signal
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Sequence, Set
from typing import NamedTuple, NotRequired, TypedDict, cast, override

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "message"))

import nix_env as nix_env

# Herdr requires agent-start timeouts strictly above this boundary.
_AGENT_START_TIMEOUT_MINIMUM_MS = 3000
# herdr refuses a longer `agent start --timeout`.
_AGENT_START_TIMEOUT_MAXIMUM_MS = 300_000
PROMPT_SETTLE_SECONDS = 10
_PLATFORM = sys.platform
# The marks that name a production unit. Only a unit's own launch sets them; a probe, or a herdr
# server started from inside a unit, drops them so that the unit's lookup never finds a second session.
UNIT_MARKS = ("SHOWRUNNER_UNIT", "SHOWRUNNER_UNIT_ID")
NO_ENVIRONMENT_REMOVALS: frozenset[str] = frozenset()
_NO_NEW_UNIT_DIRECTOR_TITLES: frozenset[str] = frozenset()
SHARED_SESSION = "agents"
# The slice of the pane scopes. A pane shell in its own scope keeps the agent load out of the server's cgroup.
PANE_SLICE = "app-agents.slice"
# The transient server unit gets CPU first and keeps its memory out of swap under the pane load.
_SERVER_PROPERTIES = ("-p", "CPUWeight=1000", "-p", "MemoryLow=1G", "-p", "MemorySwapMax=0")
_PANE_SCOPE_PROBE_SECONDS = 5.0
# The pane shell wrapper. herdr runs $SHELL for a pane; this puts the real shell in its own scope.
# The scope runs in place of the wrapper, so the pane's shell pid does not change (the scope is run-p<pid>).
# SHELL is the real shell again, so a tool that reads SHELL does not start a scope on each command.
_PANE_SHELL_SCRIPT = f"""#!/bin/sh
real=${{HERDR_PANE_SHELL:-/bin/sh}}
unset HERDR_PANE_SHELL
SHELL=$real; export SHELL
exec @SYSTEMD_RUN@ --user --scope --quiet --collect --slice={PANE_SLICE} -- "$real" "$@"
"""


def _platform() -> str:
    """The platform herdr reads processes as: HERDR_PLATFORM names the one a test's fake process tree was built for."""
    return os.environ.get("HERDR_PLATFORM", _PLATFORM)


class HerdrFailure(Exception):
    """An external command failure or malformed herdr response."""

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code: str = code


class HerdrMissing(HerdrFailure):
    """No herdr binary is available."""


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


class MacDesktopTerminalRequired(HerdrFailure):
    def __init__(self) -> None:
        super().__init__(MAC_DESKTOP_TERMINAL_REQUIRED, "mac_desktop_terminal_required")


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


UNSPECIFIED_PROMPT_TRANSCRIPT = UnspecifiedPromptTranscript()


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
class PromptSubmissionNotRequired:
    """The launch intentionally leaves the session without an opening prompt."""


PromptSubmitOutcome = PromptSubmitted | PromptNotSubmitted


@dataclass(frozen=True)
class OpeningPrompt:
    text: str
    transcript: SessionTranscript | UnspecifiedPromptTranscript = UnspecifiedPromptTranscript()


@dataclass(frozen=True)
class NoOpeningPrompt:
    """Leave the ready agent waiting for the user to type."""


@dataclass(frozen=True)
class TranscriptReadable:
    """Every matching transcript file was read successfully."""


@dataclass(frozen=True)
class TranscriptUnreadable:
    """A matching transcript file could not be read; absence of a turn is unproven."""


@dataclass
class TranscriptCursor:
    """The complete new turns seen by one submission, with per-file byte positions."""

    transcript: SessionTranscript
    since: datetime
    offsets: dict[Path, int] = field(default_factory=dict)
    sizes: dict[Path, int] = field(default_factory=dict)
    turns: dict[Path, list[tuple[datetime, dict[str, object]]]] = field(default_factory=dict)
    readability: TranscriptReadable | TranscriptUnreadable = field(default_factory=TranscriptReadable)

    def read_turns(self) -> list[tuple[datetime, dict[str, object]]]:
        self.readability = TranscriptReadable()
        # A session id is a file component, never a glob supplied by a caller.
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.transcript.session_id):
            return []
        try:
            paths = tuple((self.transcript.config_dir / "projects").glob(f"*/{self.transcript.session_id}.jsonl"))
        except OSError:
            self.readability = TranscriptUnreadable()
            paths = ()
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


def command_fallback(text: str, transcript: SessionTranscript) -> str | None:
    """A plain prompt that runs a slash command from its file, for a session whose command list lacks it.

    None when the prompt is no slash command, or the command has no file under the account's commands.
    """
    command = _slash_command(text)
    name = command.removeprefix("/")
    if not re.fullmatch(r"[A-Za-z0-9_-]+(?::[A-Za-z0-9_-]+)*", name):
        return None
    path = (transcript.config_dir / "commands").joinpath(*name.split(":")).with_suffix(".md")
    if not path.is_file():
        return None
    arguments = text[len(command):].strip()
    given = f"with the arguments: {arguments}" if arguments else "with no arguments"
    # Plain text: a prompt that starts with the command would be parsed as that command again.
    return (f"This session's command list does not have {command} yet, so read {path.resolve()} in full "
            + f"and follow it as {command} {given}")


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
    import sessions

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


@dataclass(frozen=True)
class AgentProcessIdentity:
    pid: int
    start_ticks: int


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
    label: NotRequired[str | None]


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


def child_environment(environment: dict[str, str]) -> dict[str, str]:
    """Keep the one-launch heal proof in its consuming process, never its children."""
    return {key: value for key, value in environment.items()
            if key not in (nix_env.HEAL_SESSION_ENV, nix_env.HEAL_DIRECTORY_ENV)}


def unmarked_environment() -> dict[str, str]:
    """Exclude unit identity and one-launch heal proof from inherited child environments."""
    return child_environment({key: value for key, value in os.environ.items() if key not in UNIT_MARKS})


def run_program(argv: list[str], timeout: float = 10.0, *,
                environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=timeout,
                              env=unmarked_environment() if environment is None else child_environment(environment))
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


# A tab or workspace gone from the shared session keeps its saved place this long, so a restart
# puts it back where it was. A production retired for longer than this loses its place.
LAYOUT_KEEP_SECONDS = 14 * 24 * 3600
# A fresh server gets back each workspace seen this long before the last save: a restart cut short
# by a second reboot still has the places of the sessions it did not reach.
LAYOUT_RESTORE_SECONDS = 24 * 3600
# The title of the shell tab a restart opens to hold a saved workspace's place until its first
# session's tab arrives. herdr 0.9.5 cannot move a workspace, so creation order is the only order.
LAYOUT_PLACEHOLDER = "restoring"


class SavedName(TypedDict):
    name: str
    seen: float


class SavedSpace(TypedDict):
    name: str
    seen: float
    tabs: list[SavedName]


class SavedLayout(TypedDict):
    at: float
    workspaces: list[SavedSpace]


class WorkspacesKeptForSplitTabs(NamedTuple):
    labels: list[str]


class _LiveSpace(NamedTuple):
    workspace_id: str
    label: str
    tabs: list[str]


def layout_path(session: str) -> Path:
    """The shared session's saved workspace and tab order, under the state directory."""
    _validate_session_name(session)
    state = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(state) / "herdr" / f"layout-{session}.json"


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
                                     tabs=[_saved_name(tab) for tab in json_items(space.get("tabs"), "herdr layout tabs")]))
        return SavedLayout(at=float(at), workspaces=spaces)
    except (OSError, ValueError, HerdrFailure):
        return SavedLayout(at=0.0, workspaces=[])


def _seen(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HerdrFailure("herdr layout seen: expected a number")
    return float(value)


def _saved_name(value: object) -> SavedName:
    item = json_object(value, "herdr layout tab")
    return SavedName(name=json_text(item.get("name"), "herdr layout tab name"), seen=_seen(item.get("seen")))


def merged_order(saved: list[SavedName], live: list[str], now: float) -> list[SavedName]:
    """Live names in live order, seen now; a saved name not live stays after the saved name before it.

    A saved name unseen for longer than LAYOUT_KEEP_SECONDS is dropped.
    """
    result = [SavedName(name=name, seen=now) for name in live]
    placed = set(live)
    for index, entry in enumerate(saved):
        if entry["name"] in placed or now - entry["seen"] > LAYOUT_KEEP_SECONDS:
            continue
        before = next((earlier["name"] for earlier in reversed(saved[:index]) if earlier["name"] in placed), None)
        at = 0 if before is None else next(place for place, item in enumerate(result) if item["name"] == before) + 1
        result.insert(at, SavedName(name=entry["name"], seen=entry["seen"]))
        placed.add(entry["name"])
    return result


def merged_layout(saved: SavedLayout, live: list[_LiveSpace], now: float) -> SavedLayout:
    """The saved layout with the live one laid over it: live order wins, gone entries keep their places."""
    tabs = {space["name"]: space["tabs"] for space in saved["workspaces"]}
    live_tabs = {space.label: space.tabs for space in live}
    order = merged_order([SavedName(name=space["name"], seen=space["seen"]) for space in saved["workspaces"]],
                         [space.label for space in live], now)
    return SavedLayout(at=now, workspaces=[
        SavedSpace(name=entry["name"], seen=entry["seen"],
                   tabs=merged_order(tabs.get(entry["name"], []), live_tabs.get(entry["name"], []), now))
        for entry in order])


def tab_order(space: str, titles: list[str], saved: list[SavedName], unit_director_titles: Set[str]) -> list[int]:
    """The indexes of `titles` in the order the workspace `space` shows them.

    Saved tabs keep their saved order. For other tabs, the showrunner comes first, then unit
    directors A-Z, then other tabs A-Z. The first tab stays first. `sort-tabs` ignores saved order.
    """
    showrunner_title = space
    if titles and not saved and space not in titles:
        showrunner_title = titles[0]

    def rule(title: str) -> tuple[bool, bool, str]:
        return (title != showrunner_title, title not in unit_director_titles, title)

    places = {entry["name"]: place for place, entry in enumerate(saved)}
    order = sorted((index for index, title in enumerate(titles) if title in places), key=lambda index: places[titles[index]])
    for index in sorted((index for index, title in enumerate(titles) if title not in places), key=lambda index: rule(titles[index])):
        # The first tab stays first: it is the showrunner's, also when its title is not the workspace's name.
        start = 0 if titles[index] == space or not order else 1
        at = next((place for place in range(start, len(order)) if rule(titles[order[place]]) > rule(titles[index])), len(order))
        order.insert(at, index)
    return order


class _LayoutLock:
    """One launcher at a time changes the shared session's workspaces and tab order."""

    def __init__(self, session: str) -> None:
        self.path: Path = layout_path(session).with_suffix(".lock")
        self.handle: int = -1

    def __enter__(self) -> None:
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.handle, fcntl.LOCK_EX)

    def __exit__(self, *_: object) -> None:
        os.close(self.handle)


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

    def open_tab(self, space: str, title: str, cwd: str, environment: dict[str, str]) -> str:
        """Open a tab and its pane, both named `title`, in the workspace named `space`, creating the workspace first.

        A server that holds none of the saved workspaces gets them all first, in their saved order,
        each holding a placeholder tab until its first session's tab arrives.
        """
        options = ["--label", title, "--cwd", cwd]
        for key, value in {**child_environment(environment), "HERDR_AGENT_PANE": "1"}.items():
            options.extend(["--env", f"{key}={value}"])
        with _LayoutLock(self.session):
            saved = saved_layout(self.session)
            workspace = self._workspace_named(space)
            if workspace is None and self._place_saved_workspaces(saved):
                workspace = self._workspace_named(space)
            if workspace is not None:
                result = self.result("tab", "create", "--workspace", workspace, *options, "--no-focus")
            else:
                options[1] = space
                result = self.result("workspace", "create", *options, "--no-focus")
                tab = json_text(json_object(result.get("tab"), "herdr workspace tab").get("tab_id"), "herdr tab id")
                _ = self.run("tab", "rename", tab, title)
            root_pane = json_object(result.get("root_pane"), "herdr tab root pane")
            pane = json_text(root_pane.get("pane_id"), "herdr tab root pane id")
            _ = self.run("pane", "rename", pane, title)
            if workspace is not None:
                self._close_placeholders(workspace)
                unit_director_titles = {title} if has_unit_marks(environment) else _NO_NEW_UNIT_DIRECTOR_TITLES
                pane = self.order_tabs(workspace, saved, unit_director_titles).get(pane, pane)
            self.save_layout()
            return pane

    def _workspace_named(self, space: str) -> str | None:
        workspaces = [json_object(item, "herdr workspace") for item in json_items(
            self.result("workspace", "list").get("workspaces"), "herdr workspace list")]
        found = [item for item in workspaces if item.get("label") == space]
        return json_text(found[0].get("workspace_id"), "herdr workspace id") if found else None

    def _place_saved_workspaces(self, saved: SavedLayout) -> bool:
        """Create the recently live workspaces in saved order, when the server holds none of them."""
        wanted = [space["name"] for space in saved["workspaces"] if space["seen"] >= saved["at"] - LAYOUT_RESTORE_SECONDS]
        if not wanted or {space.label for space in self.live_layout()} & set(wanted):
            return False
        home = str(Path.home())
        for name in wanted:
            result = self.result("workspace", "create", "--label", name, "--cwd", home, "--no-focus")
            tab = json_text(json_object(result.get("tab"), "herdr workspace tab").get("tab_id"), "herdr tab id")
            pane = json_text(json_object(result.get("root_pane"), "herdr workspace root pane").get("pane_id"),
                             "herdr workspace root pane id")
            _ = self.run("tab", "rename", tab, LAYOUT_PLACEHOLDER)
            _ = self.run("pane", "rename", pane, LAYOUT_PLACEHOLDER)
        return True

    def _close_placeholders(self, workspace: str) -> None:
        tabs = [json_object(item, "herdr tab") for item in json_items(
            self.result("tab", "list", "--workspace", workspace).get("tabs"), "herdr tab list")]
        for tab in tabs:
            if tab.get("label") == LAYOUT_PLACEHOLDER and tab.get("pane_count") == 1:
                _ = self.run("tab", "close", json_text(tab.get("tab_id"), "herdr tab id"))

    def live_layout(self) -> list[_LiveSpace]:
        """The session's workspaces in order, each with its tab titles in order, placeholders left out."""
        workspaces = [json_object(item, "herdr workspace") for item in json_items(
            self.result("workspace", "list").get("workspaces"), "herdr workspace list")]
        tabs = [json_object(item, "herdr tab") for item in json_items(
            self.result("tab", "list").get("tabs"), "herdr tab list")]
        spaces: list[_LiveSpace] = []
        for workspace in workspaces:
            ident = json_text(workspace.get("workspace_id"), "herdr workspace id")
            label = workspace.get("label")
            titles = [json_text(tab.get("label"), "herdr tab label") for tab in tabs if tab.get("workspace_id") == ident]
            if isinstance(label, str) and label:
                spaces.append(_LiveSpace(ident, label, titles))
        return spaces

    def save_layout(self) -> None:
        """`record_layout`, where a failure is a line on stderr: the tab is open, and only its saved place waits."""
        try:
            self.record_layout()
        except (HerdrFailure, OSError) as error:
            print(f"herdr {self.session}: layout not saved: {error}", file=sys.stderr)

    def record_layout(self) -> None:
        """Save the live workspace and tab order over the saved one. A server with no session tab saves nothing."""
        live = [space._replace(tabs=[title for title in space.tabs if title != LAYOUT_PLACEHOLDER])
                for space in self.live_layout()]
        live = [space for space in live if space.tabs]
        if not live:
            return
        path = layout_path(self.session)
        path.parent.mkdir(parents=True, exist_ok=True)
        staged = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        _ = staged.write_text(json.dumps(merged_layout(saved_layout(self.session), live, time.time()), indent=1) + "\n",
                              encoding="utf-8")
        _ = staged.replace(path)

    def retitle(self, pane: str, title: str) -> bool:
        """Give the pane and its tab the name `title`, then order the workspace's tabs again.

        False when both already carry it, after two reads and no change.
        """
        rows = [json_object(item, "herdr pane") for item in json_items(
            self.result("pane", "list").get("panes"), "herdr pane list")]
        row = next((row for row in rows if row.get("pane_id") == pane), None)
        if row is None:
            raise HerdrFailure(f"no pane {pane!r}")
        tab = json_text(row.get("tab_id"), "herdr pane tab id")
        workspace = json_text(row.get("workspace_id"), "herdr pane workspace id")
        tabs = [json_object(item, "herdr tab") for item in json_items(
            self.result("tab", "list", "--workspace", workspace).get("tabs"), "herdr tab list")]
        shown = next((item.get("label") for item in tabs if item.get("tab_id") == tab), None)
        if shown == title and row.get("label") == title:
            return False
        if row.get("label") != title:
            _ = self.run("pane", "rename", pane, title)
        if shown != title:
            _ = self.run("tab", "rename", tab, title)
            with _LayoutLock(self.session):
                _ = self.order_tabs(workspace, saved_layout(self.session))
                self.save_layout()
        return True

    def order_tabs(self, workspace: str, saved: SavedLayout,
                   new_unit_director_titles: Set[str] = _NO_NEW_UNIT_DIRECTOR_TITLES) -> dict[str, str]:
        """Keep saved order; sort other tabs by showrunner, unit directors A-Z, then other tabs A-Z.

        `sort-tabs` passes an empty saved layout to sort all tabs. The first tab stays first.
        herdr 0.9.5 cannot move a tab, but a
        pane moved into a new tab puts that tab last: so every tab from the first one out of place
        moves, in order. Each pane keeps its process, and focus goes back where it was. A tab with
        several panes stops the sort only when it must move. Returns each moved pane's new id by its old one.
        """
        spaces = [json_object(item, "herdr workspace") for item in json_items(
            self.result("workspace", "list").get("workspaces"), "herdr workspace list")]
        name = next((item.get("label") for item in spaces if item.get("workspace_id") == workspace), None)
        tabs = [json_object(item, "herdr tab") for item in json_items(
            self.result("tab", "list", "--workspace", workspace).get("tabs"), "herdr tab list")]
        if len(tabs) < 2:
            return {}
        rows = [json_object(item, "herdr pane") for item in json_items(
            self.result("pane", "list").get("panes"), "herdr pane list")]
        panes_of: dict[str, list[str]] = {}
        for row in rows:
            tab = json_text(row.get("tab_id"), "herdr pane tab id")
            panes_of.setdefault(tab, []).append(json_text(row.get("pane_id"), "herdr pane id"))
        unit_panes = unit_director_panes(self.session, herdr_processes())
        unit_director_titles = new_unit_director_titles | {
            json_text(tab.get("label"), "herdr tab label") for tab in tabs
            if any(pane in unit_panes for pane in panes_of.get(json_text(tab.get("tab_id"), "herdr tab id"), []))}
        titles = [json_text(tab.get("label"), "herdr tab label") for tab in tabs]
        unsaved: list[SavedName] = []
        kept = next((space["tabs"] for space in saved["workspaces"] if space["name"] == name), unsaved)
        order = tab_order(name if isinstance(name, str) else "", titles, kept, unit_director_titles)
        start = next((place for place, index in enumerate(order) if index != place), len(tabs))
        if start == len(tabs) or any(tabs[index].get("pane_count") != 1 for index in order[start:]):
            return {}
        focused = next((item for item in spaces if item.get("focused") is True), None)
        active = next((item.get("active_tab_id") for item in spaces if item.get("workspace_id") == workspace), None)
        moved: dict[str, str] = {}
        active_tab = active
        for index in order[start:]:
            tab = json_text(tabs[index].get("tab_id"), "herdr tab id")
            pane = panes_of[tab][0]
            result = json_object(self.result("pane", "move", pane, "--new-tab", "--workspace", workspace)
                                 .get("move_result"), "herdr pane move")
            created = json_text(json_object(result.get("created_tab"), "herdr moved tab").get("tab_id"), "herdr tab id")
            moved[pane] = json_text(json_object(result.get("pane"), "herdr moved pane").get("pane_id"), "herdr pane id")
            _ = self.run("tab", "rename", created, titles[index])
            if tab == active:
                active_tab = created
        if isinstance(active_tab, str):
            _ = self.run("tab", "focus", active_tab)
        if focused is not None and focused.get("workspace_id") != workspace:
            self.focus_workspace(json_text(focused.get("workspace_id"), "herdr workspace id"))
        return moved

    def sort_tabs(self) -> WorkspacesKeptForSplitTabs:
        """Sort each workspace by showrunner, units A-Z, then other tabs A-Z; save and return split workspaces kept."""
        kept: list[str] = []
        with _LayoutLock(self.session):
            for space in self.live_layout():
                tabs = [json_object(item, "herdr tab") for item in json_items(
                    self.result("tab", "list", "--workspace", space.workspace_id).get("tabs"), "herdr tab list")]
                if any(tab.get("pane_count") != 1 for tab in tabs):
                    kept.append(space.label)
                    continue
                _ = self.order_tabs(space.workspace_id, SavedLayout(at=0.0, workspaces=[]))
            self.save_layout()
        return WorkspacesKeptForSplitTabs(kept)

    def close_tab_of(self, pane: str) -> None:
        """Close the tab holding `pane`; a pane already gone leaves nothing to close."""
        rows = [json_object(item, "herdr pane") for item in json_items(
            self.result("pane", "list").get("panes"), "herdr pane list")]
        for row in rows:
            if row.get("pane_id") == pane:
                _ = self.run("tab", "close", json_text(row.get("tab_id"), "herdr pane tab id"))
                return

    def focus_tab_of(self, pane: str) -> None:
        """Show the tab holding `pane`, and its workspace, in the session's attached clients."""
        rows = [json_object(item, "herdr pane") for item in json_items(
            self.result("pane", "list").get("panes"), "herdr pane list")]
        for row in rows:
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
        candidates = [_workspace_startup_state(item) for item in json_items(
            self.result("workspace", "list").get("workspaces"), "herdr workspace list")]
        for candidate in candidates:
            if candidate.workspace_id == restored_workspace:
                continue
            # A previous close can take long enough for another shell to receive input.
            workspaces = [_workspace_startup_state(item) for item in json_items(
                self.result("workspace", "list").get("workspaces"), "herdr workspace list")]
            panes = [_pane_startup_state(item) for item in json_items(
                self.result("pane", "list").get("panes"), "herdr pane list")]
            if any(workspace.workspace_id == candidate.workspace_id and _untouched_startup_shell(workspace, panes)
                   for workspace in workspaces):
                _ = self.run("workspace", "close", candidate.workspace_id)

    def start_agent(self, name: str, pane: str, kind: str, agent_arguments: list[str],
                    opening: OpeningPrompt | NoOpeningPrompt, timeout_ms: int) -> PromptSubmitOutcome | PromptSubmissionNotRequired:
        """Start once, then prove the opening prompt arrived as this session's user turn."""
        _check_agent_arguments(kind, agent_arguments)
        _timeout(timeout_ms)
        launched_at = datetime.now(timezone.utc)
        # A loaded machine starts Claude slowly: the start and the opening prompt wait longer.
        budget_ms = int(timeout_ms * load_factor())
        deadline = time.monotonic() + budget_ms / 1000
        attempt_timeout_ms = min(budget_ms, _AGENT_START_TIMEOUT_MAXIMUM_MS)
        try:
            while True:
                result = self.start_command(
                    self.argv("agent", "start", _agent_name(name), "--kind", kind, "--pane", pane,
                              "--timeout", str(attempt_timeout_ms), "--", *agent_arguments),
                    kind, pane, attempt_timeout_ms / 1000 + 10,
                )
                try:
                    require_success(result, f"start agent {name}")
                except HerdrFailure as error:
                    if error.code == "agent_start_failed" and "process exited" in str(error):
                        raise AgentExited(self.session, pane) from error
                    if error.code != "agent_pane_busy":
                        raise
                    remaining_ms = int((deadline - time.monotonic()) * 1000)
                    if remaining_ms <= _AGENT_START_TIMEOUT_MINIMUM_MS:
                        raise
                    time.sleep(0.1)
                    attempt_timeout_ms = min(int((deadline - time.monotonic()) * 1000), _AGENT_START_TIMEOUT_MAXIMUM_MS)
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
            return self.submit_opening_prompt(pane, opening, launched_at,
                                              max(0, int((deadline - time.monotonic()) * 1000)))
        return PromptSubmissionNotRequired()

    def start_command(self, argv: list[str], kind: str, pane: str,
                      timeout: float) -> subprocess.CompletedProcess[str]:
        """Observe a submitted agent while its CLI waits for interactive readiness."""
        deadline = time.monotonic() + timeout
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
        try:
            process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                       env=unmarked_environment())
        except OSError as error:
            raise HerdrFailure(f"{argv[0]}: {error}") from error
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
                        activity = _startup_processes(shell, kind, tracked)
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
                        tracked.update(activity.identities)
                    agent_seen = agent_seen or isinstance(activity, AgentProcessPresent)
                    if agent_seen and not tracked and isinstance(activity, PaneAtShell):
                        if process.poll() is not None:
                            stdout, stderr = process.communicate()
                            return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
                        raise AgentExited(self.session, pane)
            except BaseException:
                # Only the waiting CLI is ours; this never signals the pane's process.
                process.kill()
                _ = process.communicate()
                raise

    def startup_processes(self, pane: str, kind: str, timeout: float) -> PaneStartupObservation:
        result = run_program(self.argv("pane", "process-info", "--pane", pane), timeout=timeout)
        require_success(result, "startup pane processes")
        payload = json_object(decode_object(result.stdout, "startup pane processes").get("result"), "startup pane result")
        info = json_object(payload.get("process_info"), "startup pane process info")
        shell = info.get("shell_pid")
        foreground = info.get("foreground_process_group_id")
        for pid in (shell, foreground):
            if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
                raise HerdrFailure("startup pane process info: expected positive process ids")
        commands: list[str] = []
        agent_present = False
        identities: list[AgentProcessIdentity] = []
        for value in json_items(info.get("foreground_processes"), "startup foreground processes"):
            process = json_object(value, "startup foreground process")
            commands.append(json_text(process.get("name"), "startup process name"))
            arguments = [json_text(arg, "startup process argument")
                         for arg in json_items(process.get("argv"), "startup process argv")]
            if _is_agent_process(arguments, kind):
                agent_present = True
                pid = process.get("pid")
                if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
                    try:
                        stat = (_proc_root() / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
                        if stat[0] != "Z":
                            identities.append(AgentProcessIdentity(pid, int(stat[19])))
                    except (OSError, ValueError, IndexError):
                        pass
        activity = AgentProcessPresent(tuple(identities)) if agent_present else PaneAtShell() if foreground == shell else PaneRunning(tuple(commands))
        return PaneStartupObservation(PaneShell(cast(int, shell)), activity)

    def readiness(self, pane: str) -> WorkerReadiness:
        agent = json_object(self.result("agent", "get", pane).get("agent"), "herdr agent get")
        status = json_text(agent.get("agent_status"), "herdr agent readiness status")
        ready = agent.get("interactive_ready")
        if not isinstance(ready, bool):
            raise HerdrFailure("herdr agent readiness: expected interactive_ready boolean")
        return {"agent_status": status, "interactive_ready": ready}

    def prompt_input_state(self, pane: str, transcript: SessionTranscript | UnspecifiedPromptTranscript,
                           launched_at: datetime, *,
                           cursor: TranscriptCursor | UnspecifiedPromptTranscript = UNSPECIFIED_PROMPT_TRANSCRIPT,
                           ) -> PromptInputReady | PromptInputNotReady:
        ready, idle = False, False
        try:
            observation = self.readiness(pane)
            idle = observation["agent_status"] == "idle"
            ready = observation["agent_status"] in {"working", "idle"} and observation["interactive_ready"]
        except (OSError, subprocess.SubprocessError, HerdrFailure):
            pass
        if not ready and isinstance(transcript, SessionTranscript):
            reader = cursor if isinstance(cursor, TranscriptCursor) else TranscriptCursor(transcript, launched_at)
            ready = any(row.get("type") == "system" and "/remote-control is active" in _turn_text(row)
                        for _, row in reader.read_turns())
        if not ready:
            return PromptInputNotReady("destination session is still loading")
        raw = self.run("pane", "read", pane, "--source", "visible", "--format", "ansi")
        screen = _prompt_screen(raw)
        composer = _composer_input(raw)
        if isinstance(composer, ComposerInput) and not composer.text.strip():
            return PromptInputReady(screen, idle)
        return PromptInputNotReady("destination has no empty composer", screen)

    def wait_for_prompt_input(self, pane: str, transcript: SessionTranscript | UnspecifiedPromptTranscript, launched_at: datetime,
                              timeout_ms: int, *,
                              cursor: TranscriptCursor | UnspecifiedPromptTranscript = UNSPECIFIED_PROMPT_TRANSCRIPT,
                              ) -> PromptInputReady | PromptInputTimedOut:
        if isinstance(cursor, UnspecifiedPromptTranscript) and isinstance(transcript, SessionTranscript):
            cursor = TranscriptCursor(transcript, launched_at)
        deadline = time.monotonic() + timeout_ms / 1000
        screen: str | PromptScreenNotRead = PromptScreenNotRead()
        while time.monotonic() < deadline:
            state = self.prompt_input_state(pane, transcript, launched_at, cursor=cursor)
            if isinstance(state.screen, str):
                screen = state.screen
            if isinstance(state, PromptInputReady):
                return state
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.1, remaining))
        return PromptInputTimedOut(screen)

    def prompt_in_input(self, pane: str, text: str) -> bool:
        """Read only the visible composer's last input region, never transcript scrollback."""
        composer = _composer_input(self.run("pane", "read", pane, "--source", "visible", "--format", "ansi"))
        return isinstance(composer, ComposerInput) and composer.contains(text)

    def prompt_input_empty(self, pane: str) -> bool:
        composer = _composer_input(self.run("pane", "read", pane, "--source", "visible", "--format", "ansi"))
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
        deadline = time.monotonic() + timeout_ms / 1000
        cursor = (TranscriptCursor(opening.transcript, launched_at)
                  if isinstance(opening.transcript, SessionTranscript) else UnspecifiedPromptTranscript())
        screen: str | PromptScreenNotRead = PromptScreenNotRead()
        typings, enters, rejections = 0, 0, 0
        recipient: PromptRecipientProcess | PromptRecipientUnavailable = PromptRecipientUnavailable("transcript identity not supplied")
        # The text typed last: the opening prompt, or the plain fallback once Claude twice called its command unknown.
        current = opening.text
        fallback = (command_fallback(opening.text, opening.transcript)
                    if isinstance(opening.transcript, SessionTranscript) else None)

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
            ready = self.wait_for_prompt_input(pane, opening.transcript, launched_at, timeout_ms, cursor=cursor)
            screen = ready.screen
            if isinstance(ready, PromptInputTimedOut):
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
                    return failure("prompt recipient changed during submission")
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
                        ready = self.wait_for_prompt_input(pane, opening.transcript, launched_at,
                            max(0, int((deadline - time.monotonic()) * 1000)), cursor=cursor)
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
                    self.prompt(pane, current)
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
                        # A resumed Claude can take a prompt before its command list is complete
                        # (cbc14765, 2026-10-20): type the command once more, then its file as a plain prompt.
                        rejections = rejected
                        if rejections > 1:
                            if fallback is None:
                                return failure(f"Claude answered Unknown command: {_slash_command(current)}")
                            current = fallback
                        elif (remaining := deadline - time.monotonic()) > 0:
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
                        if (isinstance(recipient, PromptRecipientProcess)
                                and prompt_recipient_process(cursor.transcript.session_id) != recipient):
                            return failure("prompt recipient changed before Enter retry")
                        # A turn can land during the screen/process reads too.
                        proof = cursor.submitted_prompt(current)
                        if isinstance(proof, PromptSubmitted):
                            return bound_outcome(proof)
                        if isinstance(cursor.readability, TranscriptUnreadable):
                            continue
                        if time.monotonic() >= deadline:
                            return deadline_failure()
                        self.send_keys(pane, "Enter")
                        enters += 1
                        last_input = time.monotonic()
                        continue
                    if composer.text.strip():
                        return failure("destination input contains other text")
                    # Working can mean queued input, even with an empty box. Only
                    # an idle recipient after a quiet settle window may be re-typed.
                    observation = self.readiness(pane)
                    if (observation["agent_status"] != "idle"
                            or time.monotonic() - last_input < PROMPT_SETTLE_SECONDS):
                        continue
                    if typings == 3:
                        return failure("no user turn after three typings")
                    ready = self.wait_for_prompt_input(pane, opening.transcript, launched_at,
                        max(0, int((deadline - time.monotonic()) * 1000)), cursor=cursor)
                    if isinstance(ready.screen, str):
                        screen = ready.screen
                    if isinstance(ready, PromptInputTimedOut):
                        if isinstance(cursor.readability, TranscriptUnreadable):
                            return deadline_failure()
                        return failure("destination never became ready for re-type")
                    if not ready.idle:
                        continue
                    if not isinstance(recipient, PromptRecipientProcess):
                        return failure("prompt recipient unavailable before re-type")
                    if prompt_recipient_process(cursor.transcript.session_id) != recipient:
                        return failure("prompt recipient changed before re-type")
                    # The last guard immediately before another typing: a late
                    # accepted turn wins over every readiness or idle observation.
                    proof = cursor.submitted_prompt(current)
                    if isinstance(proof, PromptSubmitted):
                        return bound_outcome(proof)
                    if isinstance(cursor.readability, TranscriptUnreadable):
                        continue
                    break
            return deadline_failure()
        except (OSError, subprocess.SubprocessError, HerdrFailure) as error:
            return failure(str(error))

    def prompt(self, pane: str, text: str) -> None:
        """Type a prompt, recorded first so that the prompt hook does not count it as the user's."""
        import script_prompts

        try:
            script_prompts.record(pane, text)
        except OSError as error:
            print(f"herdr: script prompt not recorded: {error}", file=sys.stderr)
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


def copy_claude_trust(folder: Path, source: Path, target: Path) -> Trusted | Untrusted | UnreadableTrustFile:
    """Give the target account file the source account's trust for this folder, and nothing more.

    The target gains only the one key the source already trusts. The file is replaced in one step,
    so Claude never reads half of it; every other value in it stays as it was.
    """
    present = claude_trust(folder, target)
    if not isinstance(present, Untrusted):
        return present
    granted = claude_trust(folder, source)
    if not isinstance(granted, Trusted):
        return granted
    try:
        raw = cast(object, json.loads(target.read_text(encoding="utf-8")))
        if not isinstance(raw, dict):
            raise ValueError()
        settings = cast(dict[str, object], raw)
        projects = settings.setdefault("projects", {})
        if not isinstance(projects, dict):
            raise ValueError()
        entry = cast(dict[str, object], projects).setdefault(str(granted.project_key), {})
        if not isinstance(entry, dict):
            raise ValueError()
        cast(dict[str, object], entry)["hasTrustDialogAccepted"] = True
        staged = target.with_name(f".{target.name}.trust-{os.getpid()}")
        descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(settings, output, indent=2)
        os.replace(staged, target)
    except (OSError, UnicodeError, ValueError) as error:
        return UnreadableTrustFile(folder, target, type(error).__name__)
    return Trusted(folder, target, granted.project_key)


@dataclass(frozen=True)
class TrustCopied:
    """The target account lacked trust for the folder, and the account `source` had it."""

    folder: Path
    account_file: Path
    source: str


def heal_claude_trust(
    folder: Path, target: Path, sources: Sequence[tuple[str, Path]], *, dry_run: bool,
) -> Trusted | TrustCopied | Untrusted | UnreadableTrustFile:
    """Give the target account file the trust the first trusting source already gave this folder.

    `sources` pairs each account's label with its account file, in the order to try. The copy is
    `copy_claude_trust`'s one key; a dry run copies nothing. With no trusting source, the target's
    own `Untrusted` comes back, and the user accepts trust once.
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


def load_factor() -> float:
    """How many times its limit a launch may wait: the one-minute load per CPU, from 1 to 6."""
    try:
        per_cpu = os.getloadavg()[0] / (os.cpu_count() or 1)
    except OSError:
        return 1.0
    return min(6.0, max(1.0, per_cpu))


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


def _herdr_resumes_agents() -> bool:
    """False only when herdr's config sets `[session] resume_agents_on_restore = false`."""
    path = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "herdr" / "config.toml"
    try:
        session = tomllib.loads(path.read_text(encoding="utf-8")).get("session")
    except (OSError, tomllib.TOMLDecodeError):
        return True
    return not (isinstance(session, dict) and cast(dict[str, object], session).get("resume_agents_on_restore") is False)


def keeps_restored_layout(session: str) -> bool:
    """The shared session keeps a restored layout while herdr resumes no agent itself."""
    return session == shared_session() and not _herdr_resumes_agents()


def _clear_restored_workspaces(client: Herdr) -> None:
    """A server we just spawned holds restored layout, never live work.

    The shared session keeps its layout for `pane_restore.py` only when herdr resumes no agent
    itself: else herdr would resume each pane on the server's account (herdr review, 2026-10-19).
    """
    if keeps_restored_layout(client.session):
        return
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
    """Probe a scope once per server start and write the wrapper; the reason on failure, else ""."""
    systemd_run = os.environ.get("SYSTEMD_RUN", "systemd-run")
    try:
        probe = run_program([systemd_run, "--user", "--scope", "--quiet", "--collect", "--", "true"],
                            timeout=_PANE_SCOPE_PROBE_SECONDS)
    except (HerdrFailure, subprocess.TimeoutExpired) as error:
        return str(error)
    if probe.returncode != 0:
        return probe.stderr.strip() or f"exit {probe.returncode}"
    path = _pane_shell_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        staged = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        absolute = shutil.which(systemd_run) or systemd_run
        _ = staged.write_text(_PANE_SHELL_SCRIPT.replace("@SYSTEMD_RUN@", shlex.quote(absolute)), encoding="utf-8")
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
    # Each pane shell goes into its own scope under PANE_SLICE, so the unit holds only the server.
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
    from detached import DiscardOutput, ExplicitEnvironment, DetachedChildExited, DetachedStartFailure, start_detached

    deadline = time.monotonic() + min(10.0, timeout_ms / 1000)
    try:
        if server_answering(run_program(client.argv("status", "server"),
                                        timeout=max(0.001, deadline - time.monotonic()))):
            return ServerAdopted()
    except subprocess.TimeoutExpired:
        pass
    if time.monotonic() >= deadline:
        raise _server_start_failure(client.session)
    if not isinstance(session_kind, DesktopSession):
        raise MacDesktopTerminalRequired()
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
        # Only our own child may be stopped; the other listener belongs to its launch.
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        return ServerAdopted()
    _clear_restored_workspaces(client)
    return ServerCreated()


def start_server(name: str, timeout_ms: int, *,
                 session_kind: SecuritySession | SessionKindNotRead = _SESSION_KIND_NOT_READ) -> ServerCreated | ServerAdopted:
    _validate_session_name(name)
    _require_shared(name)
    _timeout(timeout_ms)
    herdr = Herdr(name)
    if _platform() == "darwin":
        return _start_darwin_server(herdr, timeout_ms,
            session_kind=security_session() if isinstance(session_kind, SessionKindNotRead) else session_kind)
    deadline = time.monotonic() + timeout_ms / 1000
    try:
        if server_answering(run_program(herdr.argv("status", "server"), timeout=max(0.001, timeout_ms / 1000))):
            return ServerAdopted()
    except subprocess.TimeoutExpired:
        pass
    require_success(_submit_server(herdr), f"start {server_unit(name)}")
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HerdrFailure(f"server {name} not answering after {timeout_ms} ms")
        try:
            if server_answering(run_program(herdr.argv("status", "server"), timeout=max(0.001, remaining))):
                if _server_unit_active(name):
                    _clear_restored_workspaces(herdr)
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
    _validate_session_name(name)
    _require_shared(name)
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
            raise SessionTaken(name)
    except SessionTaken:
        raise
    except (OSError, subprocess.SubprocessError, HerdrFailure) as error:
        raise HerdrFailure(f"herdr session {name}: could not check existing listener: {error}") from error
    try:
        require_success(run_program([
            os.environ.get("SHUTDOWN_OPEN", "open"), "-na", "/Applications/Nix Apps/Ghostty.app",
            "--args", "-e", herdr_binary(), "session", "attach", name,
        ], timeout=max(0.001, deadline - time.monotonic())), "open Mac desktop window")
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if server_answering(run_program(client.argv("status", "server"), timeout=max(0.001, remaining))):
                _clear_restored_workspaces(client)
                return WindowServerCreated()
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.1, remaining))
    except (OSError, subprocess.SubprocessError, HerdrFailure) as error:
        raise MacDesktopUnavailable() from error
    raise MacDesktopUnavailable()


def shared_session() -> str:
    """The one herdr session every Claude session lives in, one workspace per production and one tab
    per Claude session: `agents`, or the name in `herdr/shared-session` under the config directory.

    There is no other herdr session. A launcher opens a tab here and never starts a server of its own
    (user, 2026-10-26); `_require_shared` refuses any other name.
    """
    path = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "herdr" / "shared-session"
    try:
        name = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return SHARED_SESSION
    _validate_session_name(name)
    return name


def attach_hint(space: str, tab: str) -> str:
    """Where the user finds one Claude session: its tab in a workspace of the shared session."""
    return f"herdr session attach {shared_session()}, workspace {space}, tab {tab}"


def _require_shared(name: str) -> None:
    shared = shared_session()
    if name != shared:
        raise HerdrFailure(f"herdr session {name}: every Claude session runs as a tab in herdr session "
                           + f"{shared}; open a tab there instead of starting another session")


def open_tab(name: str, space: str, title: str, cwd: str, environment: dict[str, str]) -> str:
    return Herdr(name).open_tab(space, title, cwd, environment)


def save_shared_layout(running: set[str]) -> None:
    """Save the shared session's workspace and tab order, when that session is among the `running` ones.

    A tab the user renamed in herdr reaches the saved layout here; launchers save after each change.
    """
    name = shared_session()
    if name in running:
        client = Herdr(name)
        with _LayoutLock(name):
            client.save_layout()


def reload_agent_rules(name: str) -> None:
    """Make the running session `name` read its agent detection rules again; a failure is a line on stderr."""
    try:
        _ = Herdr(name).run("server", "reload-agent-manifests")
    except HerdrFailure as error:
        print(f"herdr {name}: detection rules not reloaded: {error}", file=sys.stderr)


def close_agent(session: str, pane: str) -> None:
    """End one Claude session's place: its tab in the shared session, else its own session."""
    if session and session == shared_session():
        Herdr(session).close_tab_of(pane)
        return
    stop_session(session)


def stop_session(name: str) -> None:
    _validate_session_name(name)
    if name == shared_session():
        raise HerdrFailure(f"herdr session {name} holds every production; close one tab instead")
    herdr = Herdr(name)
    if _platform() == "darwin":
        require_success(run_program(herdr.argv("server", "stop")), f"stop herdr session {name}")
        return
    try:
        result = run_program(herdr.argv("server", "stop"))
    finally:
        if _server_unit_active(name):
            command = [os.environ.get("SYSTEMCTL", "systemctl"), "--user", "stop", server_unit(name)]
            require_success(run_program(command), f"stop {server_unit(name)}")
    require_success(result, f"stop herdr session {name}")


def _proc_root() -> Path:
    return Path(os.environ.get("HERDR_PROC", "/proc"))


def _process_environment(process: Path) -> dict[str, str]:
    return dict(entry.split("=", 1) for entry in process.joinpath("environ").read_bytes().decode(errors="replace").split("\0") if "=" in entry)


def _process_arguments(process: Path) -> list[str]:
    return [value for value in process.joinpath("cmdline").read_bytes().decode(errors="replace").split("\0") if value]


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


def _startup_processes(shell: PaneShell, kind: str, tracked: set[AgentProcessIdentity]) -> AgentProcessPresent | PaneAtShell | PaneRunning | PaneProcessesUnreadable:
    """Keep identified processes across exec and parent changes; inspect every thread."""
    root = _proc_root()
    commands: list[str] = []
    pending: list[tuple[int, bool]] = [(shell.pid, False)]
    live: set[AgentProcessIdentity] = set()
    unreadable = False
    for identity in tuple(tracked):
        try:
            stat = (root / str(identity.pid) / "stat").read_text().rsplit(")", 1)[1].split()
            if stat[0] == "Z" or int(stat[19]) != identity.start_ticks:
                tracked.remove(identity)
            else:
                live.add(identity)
                pending.append((identity.pid, True))
        except FileNotFoundError:
            tracked.remove(identity)
        except (OSError, ValueError, IndexError):
            unreadable = True
    visited: set[int] = set()
    while pending:
        pid, belongs_to_agent = pending.pop()
        if pid in visited:
            continue
        visited.add(pid)
        try:
            process = root / str(pid)
            if pid != shell.pid:
                stat = (process / "stat").read_text().rsplit(")", 1)[1].split()
                if stat[0] == "Z":
                    continue
                if not belongs_to_agent:
                    arguments = _process_arguments(process)
                    belongs_to_agent = _is_agent_process(arguments, kind)
                    if not belongs_to_agent:
                        commands.append(arguments[0] if arguments else str(pid))
                if belongs_to_agent:
                    identity = AgentProcessIdentity(pid, int(stat[19]))
                    tracked.add(identity)
                    live.add(identity)
            # children is per-thread, not the entire process's child list.
            for task in (process / "task").iterdir():
                children = (task / "children").read_text().split()
                pending.extend((int(child), belongs_to_agent) for child in children)
        except (OSError, ValueError, IndexError):
            unreadable = True
    if live:
        return AgentProcessPresent(tuple(live))
    if unreadable:
        return PaneProcessesUnreadable()
    try:
        # Read foreground ownership after walking the tree, not before launch.
        shell_stat = (root / str(shell.pid) / "stat").read_text().rsplit(")", 1)[1].split()
        foreground = int(shell_stat[5])
    except (OSError, ValueError, IndexError):
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
    if _platform() == "darwin":
        return next((process.place for process in herdr_processes() if process.pid == pid), NotInHerdr())
    try:
        environment = _process_environment(_proc_root() / str(pid))
        if environment.get("TMUX_PANE"):
            return NotInHerdr()
        pane = environment.get("HERDR_PANE_ID", "")
        if not pane:
            return NotInHerdr()
        candidates = [path for path in _proc_root().iterdir() if path.name.isdecimal()]
    except OSError:
        return NotInHerdr()
    session = _process_sessions(candidates, _live_server_processes(candidates)).get(pid, "")
    return HerdrPlace(session, pane) if session else NotInHerdr()


def herdr_places() -> dict[int, HerdrPlace]:
    """Every herdr process's place from one scan, by pid: herdr_place_of for many pids at once."""
    if _platform() == "darwin":
        return {process.pid: process.place for process in herdr_processes()}
    try:
        candidates = [path for path in _proc_root().iterdir() if path.name.isdecimal()]
    except OSError:
        return {}
    places: dict[int, HerdrPlace] = {}
    for pid, session in _process_sessions(candidates, _live_server_processes(candidates)).items():
        try:
            environment = _process_environment(_proc_root() / str(pid))
        except OSError:
            continue
        pane = environment.get("HERDR_PANE_ID", "")
        if pane and not environment.get("TMUX_PANE"):
            places[pid] = HerdrPlace(session, pane)
    return places


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


def _pane_scopes(groups: list[tuple[str, Path]]) -> list[tuple[str, Path]]:
    """The pane scopes under PANE_SLICE that hold a process, from its systemd memberships."""
    return [(hierarchy, scope) for hierarchy, path in groups
            for scope in (path, *path.parents) if scope.parent.name == PANE_SLICE]


def _add_scope_owners(owners: dict[tuple[str, Path], set[str]], pids: Sequence[int], sessions: dict[int, str]) -> None:
    """Record the session of each matched process in its pane scope."""
    for pid in pids:
        for scope in _pane_scopes(_systemd_cgroups(_proc_root() / str(pid))):
            owners.setdefault(scope, set()).add(sessions[pid])


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
            if pid not in sessions and (scopes := _pane_scopes(groups)):
                loose[pid] = scopes
        # A pane scope holds one pane's processes, as the unit's cgroup held
        # them before. Only a scope that no matched process explains needs the
        # unit servers' pane shells.
        owners: dict[tuple[str, Path], set[str]] = {}
        if loose:
            _add_scope_owners(owners, [pid for pid in sessions if pid not in servers], sessions)
            if any(not any(scope in owners for scope in scopes) for scopes in loose.values()):
                shared_sessions.update(unit_groups.values())
        walked = set(sessions)
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
        if loose:
            _add_scope_owners(owners, [pid for pid in sessions if pid not in walked], sessions)
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


def _darwin_processes() -> list[_UserProcess]:
    result = run_program([os.environ.get("HERDR_PS", "ps"), "-U", str(os.getuid()),
                          "-o", "pid=,ppid=,uid=,comm="])
    require_success(result, "user processes")
    # Both fields can contain spaces. Separate batched columns retain an unambiguous
    # executable boundary and keep unreadable or changed argv as an explicit state.
    command_result = run_program([os.environ.get("HERDR_PS", "ps"), "-U", str(os.getuid()),
                                  "-o", "pid=,command="])
    require_success(command_result, "user process arguments")
    commands: dict[int, str] = {}
    for line in command_result.stdout.splitlines():
        fields = line.split(maxsplit=1)
        if len(fields) == 2 and fields[0].isdecimal():
            commands[int(fields[0])] = fields[1]
    processes: list[_UserProcess] = []
    for line in result.stdout.splitlines():
        fields = line.split(maxsplit=3)
        if len(fields) != 4 or not all(field.isdecimal() for field in fields[:3]):
            continue
        pid, parent, uid = map(int, fields[:3])
        if uid != os.getuid() or pid <= 0:
            continue
        executable = fields[3].rstrip()
        if pid not in commands:
            try:
                current = run_program([os.environ.get("HERDR_PS", "ps"), "-o", "command=", "-p", str(pid)], timeout=5)
                _require_darwin_process_read(current, pid, f"process {pid} command")
            except _DarwinProcessGone:
                continue
            except (HerdrFailure, subprocess.TimeoutExpired):
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
            if isinstance(by_pid[pid].arguments, UnreadableProcessArguments):
                continue
            environment = darwin_process_environment(pid)
            pane = environment.get("HERDR_PANE_ID", "")
            if environment.get("TMUX_PANE") or not pane:
                continue
            marks = {key: environment[key] for key in UNIT_MARKS if key in environment}
            processes.append(HerdrProcess(pid, HerdrPlace(session, pane), marks,
                                          Path(by_pid[pid].executable).name, _darwin_process_cwd(pid)))
        except (HerdrFailure, subprocess.TimeoutExpired):
            continue
    return processes


def has_unit_marks(environment: dict[str, str]) -> bool:
    return all(environment.get(mark) for mark in UNIT_MARKS)


def unit_director_panes(session: str, processes: Sequence[HerdrProcess]) -> set[str]:
    """The session's pane ids whose processes carry both unit marks."""
    return {process.place.pane for process in processes
            if process.place.session == session and has_unit_marks(process.marks)}


def herdr_processes() -> list[HerdrProcess]:
    if _platform() == "darwin":
        return _darwin_herdr_processes()
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
            if environment.get("TMUX_PANE") or not pane:
                continue
            arguments = _process_arguments(process)
            if not arguments:
                continue
            cwd = str(process.joinpath("cwd").resolve(strict=True))
            marks = {key: environment[key] for key in UNIT_MARKS if key in environment}
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


@dataclass(frozen=True)
class AgentLaunched:
    pane: str
    outcome: PromptSubmitOutcome | PromptSubmissionNotRequired


@dataclass(frozen=True)
class PreparedAgentEnvironment:
    """An environment already proved for the exact directory to be opened."""

    directory: Path
    env: dict[str, str]


def launch_agent(name: str, space: str, title: str, cwd: str, environment: dict[str, str],
                 kind: str, agent_arguments: list[str], opening: OpeningPrompt | NoOpeningPrompt,
                 timeout_ms: int, *, agent: str, unset_environment: frozenset[str] = NO_ENVIRONMENT_REMOVALS) -> AgentLaunched | nix_env.Refused:
    """Prove the start environment before creating a pane or starting an agent.

    herdr owns the executable selected by --kind. The pane carries the loaded
    environment, so its canonical executable inherits it, including on resume.
    Caller account choices and unit marks take precedence over inherited ones.
    """
    _check_agent_arguments(kind, agent_arguments)
    if os.environ.get(nix_env.HEAL_SESSION_ENV) or os.environ.get(nix_env.HEAL_DIRECTORY_ENV):
        # Unit restores reach this choke point through add_unit in a child
        # process rather than restart's direct Claude/Codex restoration paths.
        session_id = next((agent_arguments[index + 1] for index, argument in enumerate(agent_arguments[:-1])
                           if argument in {"--resume", "--session-id", "resume"}), "")
        prepared = prepare_session_environment(session_id, cwd, environment, unset_environment)
    else:
        prepared = prepare_agent_environment(cwd, environment, unset_environment)
    if isinstance(prepared, nix_env.Refused):
        return prepared
    return launch_prepared_agent(name, space, title, prepared, kind, agent_arguments, opening, timeout_ms, agent=agent)


def agent_base_environment(environment: dict[str, str], unset_environment: frozenset[str]) -> dict[str, str]:
    """Keep the target account's choices while dropping the launcher's identity."""
    base_env = dict(os.environ)
    for key in tuple(base_env):
        # scripts/lib/unit_env.sh and unit_runs.environ map every PLAN_DELEGATE_ alias to UNIT_.
        if (key.startswith(("HERDR_", "CODEX_", "UNIT_", "PLAN_DELEGATE_", "SHOWRUNNER_", "CLAUDE_", "NIX_HEAL_"))
                or key in {"ANTHROPIC_API_KEY", "SHELL", "TMUX", "TMUX_PANE"}):
            del base_env[key]
    base_env.update(environment)
    for key in unset_environment:
        _ = base_env.pop(key, None)
    return child_environment(base_env)


def prepare_agent_environment(cwd: str, environment: dict[str, str],
                              unset_environment: frozenset[str] = NO_ENVIRONMENT_REMOVALS) -> PreparedAgentEnvironment | nix_env.Refused:
    """Load before a server restore can create a desktop window."""
    loaded = nix_env.ensure(Path(cwd), agent_base_environment(environment, unset_environment))
    if isinstance(loaded, nix_env.Refused):
        return loaded
    launch_environment = {key: value for key, value in loaded.env.items() if key not in unset_environment}
    return PreparedAgentEnvironment(Path(cwd).resolve(), launch_environment)


def prepare_session_environment(session_id: str, cwd: str, environment: dict[str, str],
                                unset_variables: frozenset[str]) -> PreparedAgentEnvironment | nix_env.Refused:
    """Consume the healer's one-session proof without loading after its hold check."""
    ready_session = os.environ.get(nix_env.HEAL_SESSION_ENV, "")
    ready_directory = os.environ.get(nix_env.HEAL_DIRECTORY_ENV, "")
    directory = Path(cwd).resolve()
    if ready_session or ready_directory:
        if ready_session != session_id or ready_directory != str(directory):
            return nix_env.Refused("nix_env: prepared restart environment belongs to another session or directory")
        if nix_env.nearest_envrc(directory) is not None and not os.environ.get("IN_NIX_SHELL"):
            return nix_env.Refused(f"nix_env: {directory}: prepared restart environment has no nix shell")
        return PreparedAgentEnvironment(directory, agent_base_environment(environment, unset_variables))
    return prepare_agent_environment(cwd, environment, unset_variables)


def launch_prepared_agent(name: str, space: str, title: str, prepared: PreparedAgentEnvironment,
                          kind: str, agent_arguments: list[str], opening: OpeningPrompt | NoOpeningPrompt,
                          timeout_ms: int, *, agent: str) -> AgentLaunched:
    """Launch an already proved environment without another potentially slow load."""
    _check_agent_arguments(kind, agent_arguments)
    pane = open_tab(name, space, title, str(prepared.directory), prepared.env)
    try:
        outcome = start_agent(name, pane, kind, agent_arguments, opening, timeout_ms, agent=agent)
    except (HerdrFailure, OSError, subprocess.SubprocessError):
        close_agent(name, pane)
        raise
    return AgentLaunched(pane, outcome)


def start_agent(name: str, pane: str, kind: str, agent_arguments: list[str],
                opening: OpeningPrompt | NoOpeningPrompt, timeout_ms: int, *, agent: str) -> PromptSubmitOutcome | PromptSubmissionNotRequired:
    """Start the agent named `agent` in the named session.

    `agent` is the full session name: a pane carries only the short tab title.
    """
    _check_agent_arguments(kind, agent_arguments)
    return Herdr(name).start_agent(agent, pane, kind=kind, agent_arguments=agent_arguments,
                                  opening=opening, timeout_ms=timeout_ms)
