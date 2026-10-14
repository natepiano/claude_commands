#!/usr/bin/env python3
"""Resume the sessions recorded by an account shutdown."""

from __future__ import annotations

import copy
import io
import json
from contextlib import redirect_stdout
from collections.abc import Iterable, Sequence
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Literal, Protocol, TypedDict, cast

SCRIPTS = Path(__file__).resolve().parents[1]
for dependency in ("whoami", "message", "notify", "production", "hooks", "lib", "herdr"):
    sys.path.insert(0, str(SCRIPTS / dependency))

from account import (  # noqa: E402
    Account,
    UnknownAccountName,
    UnreadableAccount,
    named_account_environment,
    named_claude_account,
    own_claude_account,
)
import add_unit  # noqa: E402
import herdr  # noqa: E402
import conversation_pause  # noqa: E402
from inventory import (  # noqa: E402
    PaneNotRecorded,
    Session,
    TmuxHost,
    TmuxPane,
    TmuxServer,
    TmuxServerNotRecorded,
    TmuxSessionId,
    TmuxSessionIdNotRecorded,
    WindowHost,
)
from record import (  # noqa: E402
    CLAIM_STALE_SECONDS,
    AccountHold,
    InvalidRecord,
    NoLiveRecord,
    NoRemoteControl,
    PendingTimer,
    SessionShutdown,
    SessionRestoredTimersPending,
    SessionNeedsManualRestart,
    SessionProgress,
    SessionRestarted,
    SessionRestartFailed,
    TimerRestore,
    lookup_account_hold,
    AccountHeld,
    all_account_holds,
    all_session_records,
    archive_session,
    clear_finished_account_hold,
    lookup_session,
    ShutdownFound,
    migrate,
    parse_account_holds,
    parse_session_records,
    session_records,
    update_session,
)
from remote import other_machine, run_remote  # noqa: E402
import sessions  # noqa: E402
import showrunner_footer  # noqa: E402
import stop as stop_work  # noqa: E402
from user_action import ActionRequired  # noqa: E402
from user_zone import user_zone  # noqa: E402


class RestartMessageSent(TypedDict):
    kind: Literal["sent"]


class RestartMessageQueued(TypedDict):
    kind: Literal["queued"]
    reason: str


RestartMessageDelivery = RestartMessageSent | RestartMessageQueued


class ShutdownServices(Protocol):
    NOTIFIER: Path

    def record_time(self) -> str: ...
    def local_machine(self) -> str: ...
    def retire_command(self, login: str, session_id: str) -> list[str]: ...
    def run_notifier(self, verb: str, instance: str) -> None: ...
    def send_message(
        self, recipient: str, summary: str, text: str, *,
        machine: str = "", key: str = "",
    ) -> RestartMessageDelivery: ...
    def unrecorded_local_sessions(
        self, login: str, ids: tuple[str, ...], records: list[SessionShutdown],
    ) -> dict[str, str]: ...
    def alert_user(
        self, summary: str, text: str, action: ActionRequired, *, machine: str = "",
    ) -> None: ...
    def tell_showrunners_outcome(self, records: list[SessionShutdown]) -> None: ...


settle = cast(ShutdownServices, cast(object, import_module("settle")))


ADD_UNIT = SCRIPTS / "production" / "add_unit.py"
SEND = SCRIPTS / "message" / "send.py"
SESSION_WAIT_SECONDS = 90.0
SESSION_POLL_SECONDS = 0.5
RESTART_LAUNCH_WORKERS = 6
REMOTE_CONTROL_WAIT_SECONDS = 10.0
WINDOW_DELAY_SECONDS = 1.2
RESTARTABLE_PROGRESS = frozenset({"stopped", "already gone", "process identity lost", "manual restart", "restart failed", "timers pending"})
COMPLETE_PROGRESS = frozenset({"restarted"})
# Worst wins when one machine's `up` attempts several records.
STATUS_SEVERITY = {"nothing": 0, "done": 1, "partial": 2}


class RestartAccountNotDetermined(Exception):
    """No single account can be chosen safely from restart records."""


class ScheduledRestartPromptNotRecorded(Exception):
    """The launched restart prompt could not be marked as scheduled."""


class NoUnitPlan(Exception):
    """Neither the unit's Units row nor a run record says what it resumes with."""


@dataclass(frozen=True)
class AccountNamed:
    """An account name was given at the command line."""

    text: str


@dataclass(frozen=True)
class AccountNotNamed:
    """The account must be inferred from the caller or restart records."""


@dataclass(frozen=True)
class AlertWhenNotBack:
    """A failed restart should ask the user to act."""


@dataclass(frozen=True)
class NeverAlert:
    """The restart is a silent live check."""


@dataclass(frozen=True)
class SessionLive:
    """The session registry positively identifies the session as live."""


@dataclass(frozen=True)
class SessionNotLive:
    """A readable session registry contains no live record for the session."""


@dataclass(frozen=True)
class SessionLivenessUnknown:
    """The session registry cannot safely establish whether the session is live."""

    reason: str


@dataclass(frozen=True)
class SessionLaunched:
    """The recorded session launch was started."""


@dataclass(frozen=True)
class EntryLaunched:
    """A launched entry needs confirmation from the session registry."""

    relaunching_pending_timers: bool


@dataclass(frozen=True)
class EntrySettled:
    """The entry needs no launch confirmation in this attempt."""


@dataclass(frozen=True)
class ManualRestartRequired:
    """The recorded session needs a person to run its resume command."""

    command: str


@dataclass(frozen=True)
class FailedSessionRestart:
    name: str
    reason: str


@dataclass(frozen=True)
class ManualSessionRestart:
    name: str
    command: str


@dataclass(frozen=True)
class TimerNotStarted:
    session_name: str
    instance: str
    reason: str


class RestartNotStopped(TypedDict):
    kind: Literal["not stopped"]
    session_id: str
    name: str
    progress: Literal["waiting", "ready", "held by usage limit", "stopping"]


class RestartNoShutdown(TypedDict):
    kind: Literal["no shutdown"]
    session_id: str
    name: str


RestartRefusal = RestartNotStopped | RestartNoShutdown


class PeerRestartResult(TypedDict):
    machine: str
    label: str
    outcome: Literal["done", "partial", "nothing"]
    lines: list[str]
    no_remote_control: list[NoRemoteControl]
    refused: list[RestartRefusal]


@dataclass(frozen=True)
class RegistryConfirmation:
    """Live registry records retained from the final confirmation poll."""
    records: tuple[sessions.SessionRecord, ...] = ()

    @property
    def live_ids(self) -> frozenset[str]:
        return frozenset(record["sessionId"] for record in self.records)


@dataclass(frozen=True)
class MachineRestartResult:
    machine: str
    label: str
    status: Literal["done", "partial", "nothing"]
    restarted: int = 0
    seats: int = 0
    manual: int = 0
    failed: int = 0
    failed_sessions: tuple[FailedSessionRestart, ...] = ()
    manual_sessions: tuple[ManualSessionRestart, ...] = ()
    timers_not_started: tuple[TimerNotStarted, ...] = ()
    elapsed_seconds: float = 0.0
    refused: list[RestartRefusal] = field(default_factory=list)
    no_remote_control: list[NoRemoteControl] = field(default_factory=list)
    local_session_ids: frozenset[str] = frozenset()

    @property
    def exit_status(self) -> int:
        return 0 if self.status in {"done", "nothing"} and not self.refused else 1


@dataclass
class DesktopLaunchState:
    starting_desktop: str = ""
    started_windows: int = 0

    def restore(self, *, dry_run: bool = False) -> None:
        """Return to the desktop where this sequence of window launches began."""
        restore_desktop(self, dry_run=dry_run)


class RemoteRecordsRead(TypedDict):
    kind: Literal["records read"]
    records: list[SessionShutdown]


class RemoteMachineNotReached(TypedDict):
    kind: Literal["machine not reached"]
    machine: str


RemoteRecordLookup = RemoteRecordsRead | RemoteMachineNotReached


class RemoteHoldsRead(TypedDict):
    kind: Literal["holds read"]
    holds: list[AccountHold]


RemoteHoldLookup = RemoteHoldsRead | RemoteMachineNotReached


def _send_command() -> list[str]:
    override = os.environ.get("SHUTDOWN_SEND")
    return [override] if override else [sys.executable, str(SEND)]


def _add_unit_command() -> list[str]:
    override = os.environ.get("SHUTDOWN_ADD_UNIT")
    return [override] if override else [sys.executable, str(ADD_UNIT)]


def _display(argv: list[str]) -> None:
    print(shlex.join(argv))


def _first_line(text: str, fallback: str) -> str:
    lines = text.splitlines()
    return lines[0] if lines else fallback


def _error_reason(error: BaseException) -> str:
    if isinstance(error, subprocess.CalledProcessError):
        attributes = cast(dict[str, object], cast(object, vars(error)))
        stderr = attributes.get("stderr")
        if isinstance(stderr, str) and stderr:
            return _first_line(stderr, type(error).__name__)
    return _first_line(str(error), type(error).__name__)


def _run(argv: list[str], *, dry_run: bool, check: bool = True) -> subprocess.CompletedProcess[str] | None:
    if dry_run:
        _display(argv)
        return None
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=check,
        env={
            name: value
            for name, value in os.environ.items()
            if not name.startswith("CLAUDE_")
        },
    )


def _live_session_ids() -> frozenset[str]:
    return frozenset(item["sessionId"] for item in sessions.live_sessions())


def _session_liveness(
    session_id: str,
) -> SessionLive | SessionNotLive | SessionLivenessUnknown:
    try:
        paths = [
            path
            for path in sessions.sessions_dir().iterdir()
            if path.suffix == ".json"
        ]
    except OSError as error:
        return SessionLivenessUnknown(
            f"session registry unreadable: {_error_reason(error)}"
        )

    unreadable_records: list[str] = []
    for path in paths:
        registered = sessions.read_session(path)
        if isinstance(registered, sessions.UnreadableSessionRecord):
            unreadable_records.append(path.name)
            continue
        if registered["sessionId"] == session_id and sessions.live_session(
            registered
        ):
            return SessionLive()
    if unreadable_records:
        return SessionLivenessUnknown(
            "unreadable session registry record: " + unreadable_records[0]
        )
    return SessionNotLive()


def wait_for_confirmation(session_ids: frozenset[str], timeout: float) -> RegistryConfirmation:
    """Keep the final liveness poll's records for the subsequent field check."""
    if not session_ids:
        return RegistryConfirmation()
    deadline = time.monotonic() + timeout
    confirmed = RegistryConfirmation()
    while time.monotonic() < deadline:
        live = sessions.live_sessions()
        confirmed = RegistryConfirmation(tuple(item for item in live if item["sessionId"] in session_ids))
        if confirmed.live_ids == session_ids:
            return confirmed
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(SESSION_POLL_SECONDS, remaining))
    return confirmed


def wait_for_remote_control(records: Sequence[sessions.SessionRecord], timeout: float) -> frozenset[str]:
    """Poll small registry files, starting no poll after the deadline."""
    ids = frozenset(item["sessionId"] for item in records)
    active: frozenset[str] = frozenset()
    deadline = time.monotonic() + timeout
    while ids and time.monotonic() < deadline:
        active = frozenset(item["sessionId"] for item in records if sessions.remote_control(item) == "active")
        if active == ids:
            return active
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(SESSION_POLL_SECONDS, remaining))
    return active


def remote_control_missing(records: list[SessionShutdown], confirmation: RegistryConfirmation) -> tuple[NoRemoteControl, ...]:
    """Print the diagnostic while retaining each archived session's alert policy."""
    active = wait_for_remote_control(confirmation.records, REMOTE_CONTROL_WAIT_SECONDS)
    missing = tuple(NoRemoteControl(session_id=saved["session"]["session_id"],
        name=saved["session"]["name"], alerts=saved["alerts"]) for saved in records
        if saved["session"]["session_id"] in confirmation.live_ids - active)
    for item in missing:
        print(f"{item['name']}: running, no Remote Control")
    return missing


def _user_time(value: str) -> str:
    return datetime.fromisoformat(value).astimezone(user_zone()).strftime(
        "%Y-%m-%d %H:%M %Z"
    )


def restart_note(
    record: SessionShutdown, restarted_at: str
) -> str:
    """Build the exact first prompt given to a resumed session."""
    if "target" in record:
        return account_move_note(record["session"], record["target"],
                                 record.get("source_label", record["label"]), record.get("why", ""))
    opening = (
        f"Restarted after /shutdown of {record['label']} "
        f"(stopped {_user_time(record['requested_at'])}, "
        f"restarted {_user_time(restarted_at)}). "
    )
    where = record["where"]
    if where["kind"] == "said":
        punctuation = "" if where["text"].endswith((".", "!", "?")) else "."
        opening += f"Before it you wrote: {where['text']}{punctuation} "
    else:
        opening += (
            "It left no note of where it was; read your branch and plan before "
            "you continue. "
        )
    return opening + (
        "First run `~/.claude/scripts/lib/py "
        "~/.claude/scripts/message/send.py pending` for messages kept while you "
        "were down, then continue from there; if you were waiting on the user, "
        "keep waiting."
    )


@dataclass(frozen=True)
class RetainResumeAccount:
    """Resume with the conversation's recorded Claude config directory."""

    config_dir: Path


@dataclass(frozen=True)
class MoveResumeAccount:
    """The accounts launcher must validate and select this target account."""

    target: str


@dataclass(frozen=True)
class ResumeParts:
    """A conversation launch before any host-specific command rendering."""

    cwd: str
    environment: dict[str, str]
    claude_arguments: tuple[str, ...]
    opening_prompt: str
    account: RetainResumeAccount | MoveResumeAccount


def _resume_parts(session: Session, prompt: str, target: str = "") -> ResumeParts:
    default_dir = str(Path.home() / ".claude")
    config_dir = session.get("config_dir", default_dir)
    account: RetainResumeAccount | MoveResumeAccount = (
        MoveResumeAccount(target) if target else RetainResumeAccount(Path(config_dir))
    )
    environment = (
        named_account_environment(Path(config_dir))
        if config_dir != default_dir and not target else {}
    )
    environment["ENABLE_TOOL_SEARCH"] = "true"
    argv = [
        "--resume",
        session["session_id"],
        "-n",
        session["name"],
        "--remote-control",
        session["name"],
    ]
    model = session["model"]
    if model["kind"] == "model":
        argv.extend(("--model", model["name"]))
    argv.extend(("--settings", '{"disableAgentView": true}'))
    return ResumeParts(session["cwd"], environment, tuple(argv), prompt, account)


def _resume_shell(parts: ResumeParts) -> str:
    argv = ["claude", *parts.claude_arguments, parts.opening_prompt]
    if isinstance(parts.account, MoveResumeAccount):
        argv = [sys.executable, str(SCRIPTS / "accounts" / "accounts.py"),
                "exec", parts.account.target, *argv[1:]]
    prefix = "".join(f"{key}={shlex.quote(value)} " for key, value in parts.environment.items())
    return (
        f"cd {shlex.quote(parts.cwd)} && {prefix}command "
        f"{shlex.join(argv)}; exec zsh"
    )


def _resume_command(session: Session, prompt: str, target: str = "") -> str:
    return _resume_shell(_resume_parts(session, prompt, target))


def account_move_note(session: Session, target: str, source_label: str, why: str = "") -> str:
    """One stable explanation for every moved conversation and host."""
    reason = why or f"{source_label} is out of usage"
    continuation = (" Continue your /unit:direct run." if session["kind"] == "unit" else "")
    return (f"You were moved from {source_label} to {target} because {reason}. "
            + "This was an account move, not a stop. Continue where you stopped."
            + continuation + " First run `~/.claude/scripts/lib/py "
            + "~/.claude/scripts/message/send.py pending` for messages kept while you "
            + "were down; if you were waiting on the user, keep waiting.")


def resume_command(session: Session, target: str, source_label: str = "", why: str = "") -> str:
    """The stable manual move command printed before shutdown and at restart."""
    if not source_label:
        directory = Path(session.get("config_dir", str(Path.home() / ".claude")))
        from account import claude_account
        identity = claude_account(directory)
        source_label = identity.label if identity is not None else str(directory)
    note = account_move_note(session, target, source_label, why)
    prompt = f"{_showrunner_prompt(session)} {note}" if session["kind"] == "showrunner" else note
    return _resume_command(session, prompt, target)


def _showrunner_prompt(session: Session) -> str:
    if session["kind"] != "showrunner":
        raise ValueError("only a showrunner has a production resume prompt")
    return f"/showrunner:produce {session['doc']} resume"


def _note_path(record: SessionShutdown, session_id: str) -> Path:
    root = Path(
        os.environ.get(
            "SHUTDOWN_STATE_DIR", str(Path.home() / ".local/state/shutdown")
        )
    )
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "-", session_id)
    return root / record["login"].casefold() / f"restart-note-{safe_id}.txt"


def _write_note(path: Path, note: str, dry_run: bool) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            _ = output.write(note + "\n")
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _record_scheduled_restart_prompt(
    session_id: str, prompt: str, *, dry_run: bool
) -> None:
    if dry_run:
        return
    try:
        recorded = conversation_pause.read_scheduled_prompts(session_id)
        conversation_pause.record_scheduled_prompts(session_id, (*recorded, prompt))
    except (OSError, RuntimeError, ValueError) as error:
        raise ScheduledRestartPromptNotRecorded(_error_reason(error)) from error


def _units_row_plan(doc: str, unit: str) -> str | None:
    """The Plan cell add_unit checks a resume against; None when `doc` has no single row for `unit`.

    A run record's plan can differ from it: the plan moves to as-built, or a later run uses another doc.
    """
    try:
        table = add_unit.unit_table(Path(doc).read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeError, add_unit.Refusal):
        return None
    rows = [row for row in table if add_unit.cell_value(row.get("Unit", "")) == unit]
    if len(rows) != 1 or "Plan" not in rows[0]:
        return None
    return add_unit.cell_value(rows[0]["Plan"])


def _unit_argv(record: SessionShutdown) -> list[str]:
    session = record["session"]
    host = session["host"]
    if session["kind"] != "unit" or host["kind"] != "unit":
        raise ValueError("unit launch needs a unit host")
    recorded_plan = host["plan"]
    # The Units row first: a brief unit's row names its stub plan before any run record exists.
    row_plan = _units_row_plan(host["doc"], host["unit"])
    plan_arguments: list[str]
    if row_plan == "standby":
        plan_arguments = ["--standby"]
    elif row_plan:
        plan_arguments = ["--plan", row_plan]
    elif recorded_plan["kind"] == "plan":
        plan_arguments = ["--plan", recorded_plan["path"]]
    else:
        raise NoUnitPlan(f"{host['unit']} has no Plan in the Units table of {host['doc']} and no run record")
    note_path = _note_path(record, session["session_id"])
    argv = [
        *_add_unit_command(),
        "--production",
        host["doc"],
        # The recorded id, exactly: add_unit strips one "-unit" before adding its own.
        host["unit"],
        *plan_arguments,
        "--resume",
        session["session_id"],
        "--cwd",
        session["cwd"],
        "--session-name",
        session["name"],
        "--host-session",
        host["session_name"],
        "--restart-note",
        str(note_path),
    ]
    model = session["model"]
    if model["kind"] == "model":
        argv.extend(("--model", model["name"]))
    if "target" in record:
        argv.extend(("--account", record["target"]))
    else:
        argv.extend(("--config-dir", session.get("config_dir", str(Path.home() / ".claude"))))
    return argv


def _desktop_numbers() -> dict[str, int]:
    path = Path(
        os.environ.get("SHUTDOWN_KWINRC", str(Path.home() / ".config/kwinrc"))
    )
    found: dict[str, int] = {}
    try:
        in_desktops = False
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if line.startswith("["):
                in_desktops = line == "[Desktops]"
                continue
            if in_desktops and line.startswith("Name_"):
                key, separator, value = line.partition("=")
                if separator:
                    found[value] = int(key.removeprefix("Name_"))
    except (OSError, ValueError):
        return {}
    return found


def _window_launch(
    command: str,
    session: Session,
    desktop_state: DesktopLaunchState,
    *,
    dry_run: bool,
) -> None:
    host = session["host"]
    if host["kind"] not in {"ghostty", "zed"}:
        raise ValueError("window launch needs a window host")
    window_host = cast(WindowHost, cast(object, host))
    kdotool = os.environ.get("SHUTDOWN_KDOTOOL", "kdotool")
    if not desktop_state.starting_desktop:
        get_desktop = [kdotool, "get_desktop"]
        if dry_run:
            _display(get_desktop)
        result = _run(get_desktop, dry_run=False, check=False)
        if result is not None:
            desktop_state.starting_desktop = result.stdout.strip()
    if not dry_run and desktop_state.started_windows:
        time.sleep(WINDOW_DELAY_SECONDS)
    desktop = window_host["desktop"]
    number = (
        _desktop_numbers().get(desktop["name"])
        if desktop["kind"] == "named"
        else None
    )
    if number is not None:
        _ = _run([kdotool, "set_desktop", str(number)], dry_run=dry_run)
    systemd_run = os.environ.get("SHUTDOWN_SYSTEMD_RUN", "systemd-run")
    ghostty = os.environ.get("SHUTDOWN_GHOSTTY", "ghostty")
    _ = _run(
        [
            systemd_run,
            "--user",
            "--collect",
            "--quiet",
            "--",
            ghostty,
            "-e",
            "zsh",
            "-ic",
            command,
        ],
        dry_run=dry_run,
    )
    desktop_state.started_windows += 1


def restore_desktop(state: DesktopLaunchState, *, dry_run: bool = False) -> None:
    if not state.starting_desktop.isdigit() or not state.started_windows:
        return
    kdotool = os.environ.get("SHUTDOWN_KDOTOOL", "kdotool")
    _ = _run(
        [kdotool, "set_desktop", state.starting_desktop],
        dry_run=dry_run,
        check=False,
    )


def _probe(argv: list[str], *, dry_run: bool) -> subprocess.CompletedProcess[str]:
    if dry_run:
        _display(argv)
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
    )


def _recorded_pane(host: TmuxHost) -> TmuxPane | PaneNotRecorded:
    return host["pane"]


@dataclass(frozen=True)
class ReusableTmuxPane:
    pass


@dataclass(frozen=True)
class OccupiedTmuxPane:
    session_ref: TmuxSessionId | TmuxSessionIdNotRecorded


@dataclass(frozen=True)
class TmuxPaneUnavailable:
    pass


TmuxPaneState = ReusableTmuxPane | OccupiedTmuxPane | TmuxPaneUnavailable


def _pane_is_reusable(
    pane: TmuxPane,
    pane_pid_text: str,
    pane_dead: str,
    *,
    dry_run: bool,
) -> bool:
    if pane_dead == "1":
        return True
    if pane_dead != "0":
        return False
    try:
        pane_pid = int(pane_pid_text)
    except ValueError:
        return False
    if pane_pid != pane["pane_pid"]:
        return False

    children = _probe(["pgrep", "-P", str(pane_pid)], dry_run=dry_run)
    return children.returncode == 1


def _tmux_server_matches(
    server: TmuxServer | TmuxServerNotRecorded, started_at_text: str, pid_text: str,
) -> bool:
    if server["kind"] == "not recorded":
        return False
    try:
        return int(started_at_text) == server["started_at"] and int(pid_text) == server["pid"]
    except ValueError:
        return False


def _tmux_pane_state(
    tmux: str, pane: TmuxPane | PaneNotRecorded,
    server: TmuxServer | TmuxServerNotRecorded, *, dry_run: bool,
) -> TmuxPaneState:
    if pane["kind"] == "not recorded" or server["kind"] == "not recorded":
        return TmuxPaneUnavailable()
    pane_probe = _probe(
        [
            tmux,
            "display",
            "-p",
            "-t",
            pane["pane_id"],
            "#{session_id}\t#{pane_pid}\t#{pane_dead}\t#{start_time}\t#{pid}",
        ],
        dry_run=dry_run,
    )
    if pane_probe.returncode != 0:
        return TmuxPaneUnavailable()
    fields = pane_probe.stdout.rstrip("\r\n").split("\t")
    if len(fields) != 5 or not _tmux_server_matches(server, fields[3], fields[4]):
        return TmuxPaneUnavailable()
    if _pane_is_reusable(pane, fields[1], fields[2], dry_run=dry_run):
        return ReusableTmuxPane()
    session_ref: TmuxSessionId | TmuxSessionIdNotRecorded = {"kind": "not recorded"}
    if re.fullmatch(r"\$[0-9]+", fields[0]) is not None:
        session_ref = {"kind": "session id", "session_id": fields[0]}
    return OccupiedTmuxPane(session_ref)


def _respawn_pane_argv(
    tmux: str, pane: TmuxPane, session: Session, command: str
) -> list[str]:
    return [
        tmux,
        "respawn-pane",
        "-k",
        "-t",
        pane["pane_id"],
        "-c",
        session["cwd"],
        "zsh",
        "-ic",
        command,
    ]


def _new_window_argv(tmux: str, target: str, cwd: str, command: str) -> list[str]:
    return [
        tmux,
        "new-window",
        "-t",
        target,
        "-c",
        cwd,
        "zsh",
        "-ic",
        command,
    ]


def _tmux_launch(command: str, session: Session, *, dry_run: bool) -> str:
    host = session["host"]
    if host["kind"] != "tmux":
        raise ValueError("tmux launch needs a tmux host")
    tmux = os.environ.get("SHUTDOWN_TMUX", "tmux")
    pane = _recorded_pane(host)
    server = host["server"]
    pane_state = _tmux_pane_state(tmux, pane, server, dry_run=dry_run)
    if pane["kind"] == "pane" and isinstance(pane_state, ReusableTmuxPane):
        _ = _run(
            _respawn_pane_argv(tmux, pane, session, command),
            dry_run=dry_run,
        )
        return pane["pane_id"]

    if isinstance(pane_state, OccupiedTmuxPane) and pane_state.session_ref["kind"] == "session id":
        _ = _run(
            _new_window_argv(
                tmux, f"{pane_state.session_ref['session_id']}:", session["cwd"], command,
            ),
            dry_run=dry_run,
        )
        return pane_state.session_ref["session_id"]
    session_ref = host["session_ref"]
    if server["kind"] == "server" and session_ref["kind"] == "session id":
        session_probe = _probe(
            [tmux, "display", "-p", "-t", session_ref["session_id"], "#{start_time}\t#{pid}"],
            dry_run=dry_run,
        )
        server_fields = session_probe.stdout.rstrip("\r\n").split("\t")
        if (
            session_probe.returncode == 0
            and len(server_fields) == 2
            and _tmux_server_matches(server, server_fields[0], server_fields[1])
        ):
            _ = _run(
                _new_window_argv(tmux, f"{session_ref['session_id']}:", session["cwd"], command),
                dry_run=dry_run,
            )
            return session_ref["session_id"]
    probe_argv = [tmux, "has-session", "-t", f"={host['tmux_session']}"]
    probe = _probe(probe_argv, dry_run=dry_run)
    if probe.returncode == 0:
        _ = _run(
            _new_window_argv(tmux, f"={host['tmux_session']}:", session["cwd"], command),
            dry_run=dry_run,
        )
        return f"={host['tmux_session']}"
    try:
        _ = _run(
            [
                tmux,
                "new-session",
                "-d",
                "-s",
                host["tmux_session"],
                "-c",
                session["cwd"],
                "zsh",
                "-ic",
                command,
            ],
            dry_run=dry_run,
        )
    except subprocess.CalledProcessError:
        if dry_run or _probe(probe_argv, dry_run=False).returncode != 0:
            raise
        _ = _run(
            _new_window_argv(tmux, f"={host['tmux_session']}:", session["cwd"], command),
            dry_run=False,
        )

    return f"={host['tmux_session']}"


def _herdr_launch(parts: ResumeParts, session: Session, *, dry_run: bool) -> None:
    """Resume Claude with its own arguments and one separate opening prompt."""
    host = session["host"]
    if host["kind"] != "herdr":
        raise ValueError("herdr launch needs a herdr host")
    if isinstance(parts.account, MoveResumeAccount):
        raise OSError(
            f"herdr account move to {parts.account.target} is not supported: "
            + "pane environment assignments cannot perform accounts exec credential "
            + "removal and readiness checks; resume manually with accounts.py exec"
        )
    environment = {**named_account_environment(parts.account.config_dir), **parts.environment}
    name = host["herdr_session"]
    pane = host["pane_id"]
    if dry_run:
        print(f"herdr {name}: resume {session['session_id']} in {pane}")
        return
    _record_scheduled_restart_prompt(session["session_id"], parts.opening_prompt, dry_run=False)
    try:
        from scripts.accounts.accounts import ClaudeLaunchAccount, DefaultAccount, NamedAccount

        config_dir = Path(environment["CLAUDE_CONFIG_DIR"])
        claude_json = (Path.home() / ".claude.json" if config_dir == Path.home() / ".claude"
                       else config_dir / ".claude.json")
        trust = herdr.claude_trust(Path(parts.cwd), claude_json)
        if isinstance(trust, (herdr.Untrusted, herdr.UnreadableTrustFile)):
            account: ClaudeLaunchAccount = (
                DefaultAccount() if config_dir == Path.home() / ".claude"
                else NamedAccount(str(config_dir), config_dir)
            )
            reason = (str(trust) if isinstance(trust, herdr.UnreadableTrustFile)
                      else f"{parts.cwd}: Claude has not trusted this folder")
            raise OSError(f"{reason}. Run: {herdr.trust_command(Path(parts.cwd), account)}")
        herdr.start_server(name, 90_000)
        client = herdr.Herdr(name)
        agents = client.agents()
        if any(row["pane_id"] == pane and row.get("agent")
               and row["agent_status"] not in {"exited", "stopped", "dead"} for row in agents):
            raise OSError(f"herdr {name}:{pane}: pane still holds a live agent")
        pane = herdr.open_pane(name, name, parts.cwd, environment)
        client.start_agent(session["name"], pane, kind="claude", agent_arguments=list(parts.claude_arguments),
                           opening_prompt=parts.opening_prompt, timeout_ms=90_000)
    except herdr.HerdrFailure as error:
        raise OSError(str(error)) from error


def _launch_session(
    record: SessionShutdown,
    note: str,
    desktop_state: DesktopLaunchState,
    *,
    dry_run: bool,
) -> SessionLaunched | ManualRestartRequired:
    """Launch an entry or return its explicit manual-restart requirement."""
    session = record["session"]
    host = session["host"]
    if session["kind"] == "unit":
        if host["kind"] != "unit":
            raise ValueError("unit session has no unit host")
        if host["session_host"] == "herdr":
            return ManualRestartRequired(_resume_command(session, note, record.get("target", "")))
        argv = _unit_argv(record)
        note_path = _note_path(record, session["session_id"])
        _write_note(note_path, note, dry_run)
        try:
            _ = _run(argv, dry_run=dry_run)
        finally:
            if not dry_run:
                note_path.unlink(missing_ok=True)
        return SessionLaunched()

    prompt = _showrunner_prompt(session) if session["kind"] == "showrunner" else note
    if session["kind"] == "showrunner" and "target" in record:
        prompt += " " + note
    parts = _resume_parts(session, prompt, record.get("target", ""))
    if host["kind"] == "herdr":
        _herdr_launch(parts, session, dry_run=dry_run)
        return SessionLaunched()
    command = _resume_shell(parts)
    if host["kind"] == "unknown" or (host["kind"] == "terminal" and sys.platform == "linux"):
        if "target" in record:
            command = resume_command(session, record["target"], record.get("source_label", record["label"]), record.get("why", ""))
        return ManualRestartRequired(command)
    _record_scheduled_restart_prompt(
        session["session_id"], prompt, dry_run=dry_run
    )
    match host["kind"]:
        case "ghostty" | "zed":
            _window_launch(command, session, desktop_state, dry_run=dry_run)
        case "terminal":
            if not dry_run and desktop_state.started_windows:
                time.sleep(WINDOW_DELAY_SECONDS)
            opener = os.environ.get("SHUTDOWN_OPEN", "open")
            _ = _run(
                [
                    opener,
                    "-na",
                    "/Applications/Nix Apps/Ghostty.app",
                    "--args",
                    "-e",
                    "zsh",
                    "-ic",
                    command,
                ],
                dry_run=dry_run,
            )
            desktop_state.started_windows += 1
        case "tmux":
            retained_session = _tmux_launch(command, session, dry_run=dry_run)
            if "target" in record:
                from scripts.accounts import accounts

                directory = (accounts.main_config_dir() if record["target"] == "default"
                             else accounts.account_dir(record["target"]))
                _ = _run([os.environ.get("SHUTDOWN_TMUX", "tmux"), "set-option", "-t",
                          retained_session, "@claude_config_dir", str(directory)], dry_run=dry_run)
        case "unit":
            raise ValueError("non-unit session has a unit host")
    return SessionLaunched()


def _message_key(record: SessionShutdown, session_id: str) -> str:
    return f"shutdown-{record['login']}-{session_id}"


def _retire(record: SessionShutdown, *, dry_run: bool) -> None:
    session_id = record["session"]["session_id"]
    _ = _run(
        settle.retire_command(record["login"], session_id),
        dry_run=dry_run,
    )


def _send_restart_note(
    record: SessionShutdown,
    note: str,
    *,
    dry_run: bool,
) -> RestartMessageDelivery:
    session_id = record["session"]["session_id"]
    if dry_run:
        argv = [
            *_send_command(),
            "--to",
            f"session:{session_id}",
            "--from",
            "shutdown",
            "--summary",
            f"{record['label']} restarted",
            "--key",
            _message_key(record, session_id),
        ]
        _display(argv)
        return RestartMessageSent(kind="sent")
    return settle.send_message(
        f"session:{session_id}",
        f"{record['label']} restarted",
        note,
        key=_message_key(record, session_id),
    )


def _restore_timers(
    timers: list[TimerRestore], *, dry_run: bool
) -> tuple[PendingTimer, ...]:
    failures: list[PendingTimer] = []
    for timer in timers:
        if timer["was_enabled"]:
            if dry_run:
                override = os.environ.get("SHUTDOWN_NOTIFIER")
                notifier = [override] if override else ["zsh", str(settle.NOTIFIER)]
                _display([*notifier, "start", timer["instance"]])
            else:
                try:
                    settle.run_notifier("start", timer["instance"])
                except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                    failures.append(
                        PendingTimer(
                            instance=timer["instance"],
                            reason=_error_reason(error),
                        )
                    )
        footer = timer["footer"]
        if footer["kind"] == "footer" and not dry_run:
            showrunner_footer.set_footer_state(
                footer["slug"], showrunner_footer.FooterState.ON
            )
    return tuple(failures)


def _pending_timer_restores(
    record: SessionShutdown, pending: list[PendingTimer]
) -> list[TimerRestore]:
    timers_by_instance = {timer["instance"]: timer for timer in record["timers"]}
    try:
        return [timers_by_instance[timer["instance"]] for timer in pending]
    except KeyError as error:
        raise ValueError(
            f"pending timer {error.args[0]} is missing from the restart record"
        ) from error


def _same_relaunch_claim(record: SessionShutdown, current: SessionShutdown) -> bool:
    expected = record["progress"]
    observed = current["progress"]
    return (
        record["request_id"] == current["request_id"]
        and expected["kind"] == "restarting"
        and observed["kind"] == "restarting"
        and expected["at"] == observed["at"]
    )


def _set_entry_progress(record: SessionShutdown, progress: SessionProgress) -> bool:
    expected = record["progress"]
    written = False

    def change(current: SessionShutdown) -> None:
        nonlocal written
        observed = current["progress"]
        if current["request_id"] != record["request_id"]:
            return
        if expected["kind"] == "restarting":
            if not _same_relaunch_claim(record, current):
                return
        elif expected["kind"] == "stopping":
            if observed["kind"] != "stopping" or expected["at"] != observed["at"]:
                return
        elif observed["kind"] in {"restarting", "stopping"}:
            return
        current["progress"] = progress
        written = True

    try:
        _ = update_session(record["login"], record["session"]["session_id"], change)
    except NoLiveRecord:
        return False
    if written:
        record["progress"] = progress
        if record.get("target") and progress["kind"] in {"restart failed", "manual restart"}:
            _journal_move_failure(record, progress["reason"] if progress["kind"] == "restart failed" else "manual restart")
    return written


def _journal_move_failure(record: SessionShutdown, reason: str) -> None:
    from scripts.accounts import moves
    session = record["session"]
    moves.append_move(moves.FailedMove(kind="failed", at=settle.record_time(), tool="claude",
        subject=session["session_id"] + ": " + session["name"], move_id=record["request_id"], reason=reason))


def _target_config_dir(record: SessionShutdown) -> Path | None:
    """The config directory a restored session belongs on: its move target, else the one it left, if recorded."""
    target_label = record.get("target", "")
    if not target_label:
        session = record["session"]
        return Path(session["config_dir"]) if "config_dir" in session else None
    from scripts.accounts import accounts
    return accounts.main_config_dir() if target_label == "default" else accounts.account_dir(target_label)


def _live_config_dir(record: SessionShutdown) -> Path | None:
    import account
    live = next((item for item in sessions.live_sessions() if item["sessionId"] == record["session"]["session_id"]), None)
    return account.process_config_dir(live["pid"]) if live is not None else None


def _confirm_move(record: SessionShutdown) -> None:
    from scripts.accounts import moves
    from scripts.accounts.refusal_watch import clear_assumed
    session = record["session"]
    moves.append_move(moves.ConfirmedMove(kind="confirmed", at=settle.record_time(), tool="claude",
        subject=session["session_id"] + ": " + session["name"], move_id=record["request_id"],
        config_dir=str(_target_config_dir(record) or "")))
    clear_assumed(session["session_id"])


def _prove_move(record: SessionShutdown) -> bool:
    """A registry entry alone does not prove that the session adopted the target."""
    if not record.get("target", ""):
        return True
    directory, target = _live_config_dir(record), _target_config_dir(record)
    if directory is None or target is None or directory.resolve() != target.resolve():
        reason = "still on " + record.get("source_label", record["label"]) if directory is not None else "resumed account unreadable"
        mark_restart_failed(record, reason)
        return False
    _confirm_move(record)
    return True


NOT_BACK_PROGRESS = frozenset({"manual restart", "restart failed"})


@dataclass(frozen=True)
class TargetCheck:
    remaining: list[SessionShutdown]
    finished: list[SessionShutdown]


def clear_running_on_target(records: list[SessionShutdown]) -> TargetCheck:
    """Finish the records of sessions already running on their target, as a relaunch by hand leaves them.

    `remaining` holds every other record as last written, and `finished` the archived ones as
    restarted: a session that is back is never reported as needing a restart, and `restart`
    never starts a second process on it.
    """
    remaining: list[SessionShutdown] = []
    finished: list[SessionShutdown] = []
    for record in records:
        if record["progress"]["kind"] not in NOT_BACK_PROGRESS or record["machine"].casefold() != settle.local_machine().casefold():
            remaining.append(record)
            continue
        directory, target = _live_config_dir(record), _target_config_dir(record)
        if directory is None or target is None or directory.resolve() != target.resolve():
            remaining.append(record)
            continue
        if record.get("target", ""):
            _confirm_move(record)
        failures = _restore_timers(record["timers"], dry_run=False)
        progress: SessionProgress = (SessionRestoredTimersPending(kind="timers pending", at=settle.record_time(), timers=list(failures))
            if failures else SessionRestarted(kind="restarted", at=settle.record_time()))
        if not _set_entry_progress(record, progress):
            found = lookup_session(record["login"], record["session"]["session_id"])
            if isinstance(found, ShutdownFound):
                remaining.append(found.record)
            continue
        if failures:
            remaining.append({**record, "progress": progress})
            continue
        archive_session(record["login"], record["session"]["session_id"])
        finished.append({**record, "progress": progress})
    return TargetCheck(remaining, finished)


def _record_timer_restore_progress(
    record: SessionShutdown,
    failures: tuple[PendingTimer, ...],
) -> None:
    from scripts.accounts.refusal_watch import clear_assumed
    clear_assumed(record["session"]["session_id"])
    if failures:
        _record_pending_timers(record, failures)
        return
    progress: SessionProgress = SessionRestarted(
        kind="restarted", at=settle.record_time()
    )
    _ = _set_entry_progress(record, progress)


def _record_pending_timers(
    record: SessionShutdown,
    pending: tuple[PendingTimer, ...],
) -> None:
    progress: SessionProgress = SessionRestoredTimersPending(
        kind="timers pending",
        at=settle.record_time(),
        timers=list(pending),
    )
    _ = _set_entry_progress(record, progress)


def _entry_order(record: SessionShutdown) -> tuple[int, str]:
    session = record["session"]
    requester = record["requested_by"]
    return (
        int(requester["kind"] == "session" and requester["session_id"] == session["session_id"]),
        session["name"],
    )


def _dry_run_entry_order(record: SessionShutdown) -> tuple[int, str]:
    return _entry_order(record)


def mark_restart_failed(
    record: SessionShutdown,
    reason: str,
) -> None:
    _ = _set_entry_progress(record, SessionRestartFailed(
        kind="restart failed", at=settle.record_time(), reason=reason,
    ))


def _launch_failure_left_session_live(
    record: SessionShutdown,
    error: BaseException,
) -> bool:
    """Settle a failed launch command by the session registry; True when its session is live.

    A launcher can fail after it started the session, as add_unit does when remote control never
    reports active. That session is launched: recording it failed invites a second process on it.
    """
    name = record["session"]["name"]
    reason = _error_reason(error)
    match _session_liveness(record["session"]["session_id"]):
        case SessionLive():
            print(f"{record['machine']}: {name} is live; its launch reported: {reason}")
            return True
        case SessionNotLive():
            mark_restart_failed(record, reason)
        case SessionLivenessUnknown(reason=unknown):
            print(f"{record['machine']}: {name} restart unfinished: {reason}; {unknown}")
            _restore_prior(record)
    return False


def _restart_note_delivered(
    record: SessionShutdown,
    note: str,
    *,
    dry_run: bool,
) -> bool:
    try:
        delivery = _send_restart_note(record, note, dry_run=dry_run)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        if not dry_run:
            mark_restart_failed(
                record,
                f"restart note not delivered: {_error_reason(error)}",
            )
        return False
    if delivery["kind"] == "sent":
        return True
    if not dry_run:
        mark_restart_failed(
            record,
            "restart note not delivered: "
            + _first_line(delivery["reason"], "queued"),
        )
    return False


def launch_entry(
    record: SessionShutdown,
    restarted_at: str,
    desktop_state: DesktopLaunchState,
    *,
    dry_run: bool,
) -> EntryLaunched | EntrySettled:
    progress = _prior_progress(record)
    if progress["kind"] in COMPLETE_PROGRESS:
        return EntrySettled()
    session = record["session"]
    session_id = session["session_id"]
    relaunching_pending_timers = False
    if progress["kind"] == "timers pending":
        relaunching_pending_timers = True
        match _session_liveness(session_id):
            case SessionLive():
                if not dry_run and not _prove_move(record):
                    return EntrySettled()
                timers = _pending_timer_restores(record, progress["timers"])
                failures = _restore_timers(timers, dry_run=dry_run)
                if not dry_run:
                    _record_timer_restore_progress(record, failures)
                return EntrySettled()
            case SessionLivenessUnknown():
                if not dry_run:
                    _restore_prior(record)
                return EntrySettled()
            case SessionNotLive():
                pass
    try:
        _retire(record, dry_run=dry_run)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        if not dry_run:
            mark_restart_failed(
                record,
                f"queued shutdown messages not retired: {_error_reason(error)}",
            )
        return EntrySettled()
    was_live = session_id in _live_session_ids()
    note = restart_note(record, restarted_at)
    if was_live:
        if not dry_run and not _prove_move(record):
            return EntrySettled()
        if progress["kind"] != "manual restart" and not _restart_note_delivered(
            record, note, dry_run=dry_run
        ):
            return EntrySettled()
        timer_failures = _restore_timers(record["timers"], dry_run=dry_run)
        if not dry_run:
            _record_timer_restore_progress(record, timer_failures)
        return EntrySettled()

    if progress["kind"] == "manual restart":
        if dry_run:
            print(f"manual restart {session['name']}: {progress['command']}")
        elif not dry_run:
            _ = _set_entry_progress(record, progress)
        return EntrySettled()

    try:
        launch = _launch_session(
            record, note, desktop_state, dry_run=dry_run
        )
    except ScheduledRestartPromptNotRecorded as error:
        if not dry_run:
            mark_restart_failed(
                record,
                f"restart prompt not recorded: {_error_reason(error)}",
            )
        return EntrySettled()
    except NoUnitPlan as error:
        if dry_run:
            print(f"cannot restart {session['name']}: {error}")
        else:
            mark_restart_failed(record, str(error))
        return EntrySettled()
    except (OSError, subprocess.CalledProcessError, subprocess.SubprocessError) as error:
        if dry_run or not _launch_failure_left_session_live(record, error):
            return EntrySettled()
        launch = SessionLaunched()
    if isinstance(launch, ManualRestartRequired):
        print(f"manual restart {session['name']}: {launch.command}")
        if not dry_run:
            _ = _set_entry_progress(record, SessionNeedsManualRestart(
                kind="manual restart", command=launch.command,
            ))
        return EntrySettled()
    if dry_run:
        return EntrySettled()
    return EntryLaunched(relaunching_pending_timers)


def confirm_entry(
    record: SessionShutdown,
    restarted_at: str,
    live: bool,
) -> None:
    """Finish an entry after its shared registry wait."""
    found = lookup_session(record["login"], record["session"]["session_id"])
    if not isinstance(found, ShutdownFound):
        return
    current = found.record
    if current["request_id"] != record["request_id"]:
        return
    if record["progress"]["kind"] == "restarting":
        if not _same_relaunch_claim(record, current):
            return
    elif current["progress"]["kind"] in {"restarting", "stopping"}:
        return
    if not live:
        mark_restart_failed(
            record,
            f"session not live after {SESSION_WAIT_SECONDS:g} seconds",
        )
        return
    if not _prove_move(record):
        return
    if record["session"]["kind"] == "showrunner":
        note = restart_note(record, restarted_at)
        if not _restart_note_delivered(record, note, dry_run=False):
            return
    timer_failures = _restore_timers(record["timers"], dry_run=False)
    _record_timer_restore_progress(record, timer_failures)


def _machine_result(records: list[SessionShutdown], machine: str, label: str) -> MachineRestartResult:
    failed = tuple(
        FailedSessionRestart(item["session"]["name"], item["progress"]["reason"])
        for item in records if item["progress"]["kind"] == "restart failed"
    )
    manual = tuple(
        ManualSessionRestart(item["session"]["name"], item["progress"]["command"])
        for item in records if item["progress"]["kind"] == "manual restart"
    )
    pending = tuple(
        TimerNotStarted(item["session"]["name"], timer["instance"], timer["reason"])
        for item in records if item["progress"]["kind"] == "timers pending"
        for timer in item["progress"]["timers"]
    )
    restarted = sum(item["progress"]["kind"] in {"restarted", "timers pending"} for item in records)
    return MachineRestartResult(
        machine, label, "partial" if failed or manual or pending or any(
            item["progress"]["kind"] != "restarted" for item in records
        ) else "done",
        restarted=restarted, seats=sum(len(item["seats"]) for item in records if item["progress"]["kind"] == "restarted"),
        failed=len(failed), manual=len(manual), failed_sessions=failed,
        manual_sessions=manual, timers_not_started=pending,
    )


def _timing_line(result: MachineRestartResult) -> str:
    return f"{result.machine}: restarted {result.restarted} in {result.elapsed_seconds:.1f} s"


def _result_lines(result: MachineRestartResult) -> list[str]:
    if result.status == "nothing":
        return []
    lines = [
        "{}: {} restarted, {} seats available on demand".format(
            result.machine, result.restarted, result.seats
        )
    ]
    lines.extend(
        f"{result.machine}: {item.name} restart failed: {item.reason}"
        for item in result.failed_sessions
    )
    lines.extend(
        f"{result.machine}: manual restart {item.name}: {item.command}"
        for item in result.manual_sessions
    )
    lines.extend(
        f"{result.machine}: timer {item.instance} of {item.session_name} not started: {item.reason}"
        for item in result.timers_not_started
    )
    lines.append(_timing_line(result))
    return lines


def _print_result(result: MachineRestartResult, *,
                  already_reported: tuple[ManualSessionRestart, ...] = ()) -> None:
    displayed = replace(result, manual_sessions=tuple(item for item in result.manual_sessions
                                                     if item not in already_reported))
    for line in _result_lines(displayed):
        print(line)


def _prior_progress(record: SessionShutdown) -> SessionProgress:
    progress = record["progress"]
    return progress["prior"] if progress["kind"] == "restarting" else progress


def _restore_prior(record: SessionShutdown) -> None:
    progress = copy.deepcopy(_prior_progress(record))
    _ = _set_entry_progress(record, progress)


def _claim_relaunch(record: SessionShutdown) -> tuple[SessionShutdown, bool]:
    claimed = False
    at = settle.record_time()

    def claim(current: SessionShutdown) -> None:
        nonlocal claimed
        progress = current["progress"]
        if progress["kind"] == "restarting":
            age = (datetime.fromisoformat(at) - datetime.fromisoformat(progress["at"])).total_seconds()
            if age < CLAIM_STALE_SECONDS:
                return
            progress = progress["prior"]
        if current["request_id"] != record["request_id"] or progress["kind"] not in RESTARTABLE_PROGRESS:
            return
        current["progress"] = {"kind": "restarting", "at": at, "prior": copy.deepcopy(progress)}
        claimed = True

    return update_session(record["login"], record["session"]["session_id"], claim), claimed


def _launch_entry_safely(
    record: SessionShutdown,
    restarted_at: str,
    desktop_state: DesktopLaunchState,
) -> EntryLaunched | EntrySettled:
    try:
        return launch_entry(record, restarted_at, desktop_state, dry_run=False)
    except Exception as error:
        mark_restart_failed(record, _error_reason(error))
        return EntrySettled()


def relaunch_session(
    record: SessionShutdown,
    *,
    dry_run: bool,
    desktop_state: DesktopLaunchState,
) -> SessionShutdown:
    """Claim and launch one session; leave registry confirmation to the caller."""
    if dry_run:
        _ = launch_entry(record, settle.record_time(), desktop_state, dry_run=True)
        return record
    if record["progress"]["kind"] != "restarting" and _unfinished_cleanup(record) and record["session"]["session_id"] not in _live_session_ids():
        try:
            cleaned = stop_work.stop_session(record)
        except Exception as error:
            mark_restart_failed(record, _error_reason(error))
            return record
        if cleaned["request_id"] != record["request_id"]:
            return record
        record = cleaned
        if record["progress"]["kind"] == "stopping":
            return record
        if _unfinished_cleanup(record):
            mark_restart_failed(record, "owner cleanup not confirmed; run /shutdown restart again")
            return record
    try:
        current, claimed = _claim_relaunch(record)
    except NoLiveRecord:
        return record
    if not claimed:
        # A competing owner keeps its claim; this caller has no launch to confirm.
        return record
    claim = current["progress"]
    if claim["kind"] != "restarting":
        return current
    outcome = _launch_entry_safely(current, claim["at"], desktop_state)
    if isinstance(outcome, EntryLaunched) and outcome.relaunching_pending_timers:
        def reset_pending(changing: SessionShutdown) -> None:
            progress = changing["progress"]
            if changing["request_id"] == current["request_id"] and progress["kind"] == "restarting" and progress["at"] == claim["at"]:
                progress["prior"] = SessionRestoredTimersPending(
                    kind="timers pending", at=settle.record_time(),
                    timers=[PendingTimer(instance=timer["instance"], reason="not yet restored after relaunch") for timer in changing["timers"]],
                )
        current = update_session(current["login"], current["session"]["session_id"], reset_pending)
    found = lookup_session(current["login"], current["session"]["session_id"])
    if not isinstance(found, ShutdownFound):
        return current if current["progress"]["kind"] != "restarting" else record
    latest = found.record
    if latest["request_id"] != record["request_id"]:
        return record
    progress = latest["progress"]
    if progress["kind"] == "restarting" and progress["at"] != claim["at"]:
        return record
    if isinstance(outcome, EntrySettled):
        return current if current["progress"]["kind"] != "restarting" else record
    return latest


def _unfinished_cleanup(record: SessionShutdown) -> bool:
    return any(
        (issue["kind"] == "codex server left running" or issue["kind"] == "unit session left running")
        and issue["cause"] == "stop not confirmed"
        for issue in record["stop_issues"]
    )


def owns_relaunch_claim(original: SessionShutdown, result: SessionShutdown) -> bool:
    """A new claim belongs to this call; an unchanged claim belongs to its owner."""
    claim = result["progress"]
    previous = original["progress"]
    return original["request_id"] == result["request_id"] and claim["kind"] == "restarting" and (
        previous["kind"] != "restarting" or claim["at"] != previous["at"]
    )


def _confirm_entries(records: list[SessionShutdown]) -> tuple[NoRemoteControl, ...]:
    launched = [record for record in records if record["progress"]["kind"] == "restarting"]
    confirmation = wait_for_confirmation(
        frozenset(record["session"]["session_id"] for record in launched),
        SESSION_WAIT_SECONDS,
    ) if launched else RegistryConfirmation()
    for record in launched:
        progress = record["progress"]
        if progress["kind"] == "restarting":
            confirm_entry(record, progress["at"], record["session"]["session_id"] in confirmation.live_ids)
    for record in records:
        if record["progress"]["kind"] == "restarted":
            archive_session(record["login"], record["session"]["session_id"])

    return remote_control_missing(launched, confirmation)


def _up(login: str, dry_run: bool, only: Iterable[str] = ()) -> MachineRestartResult:
    if not dry_run:
        for line in migrate(login):
            print(line)
    ids = tuple(only)
    all_records = session_records(login)
    hold_outcome = lookup_account_hold(login)
    records = [saved for saved in all_records if not ids or saved["session"]["session_id"] in ids]
    finished: list[SessionShutdown] = []
    if not dry_run:
        checked = clear_running_on_target(records)
        records, finished = checked.remaining, checked.finished
        for record in finished:
            print(f"{record['machine']}: {record['session']['name']} is already running on {record.get('target') or record['label']}")
    refused: list[RestartRefusal] = []
    local_ids = {saved["session"]["session_id"] for saved in all_records}
    for identity, name in settle.unrecorded_local_sessions(login, ids, all_records).items():
        local_ids.add(identity)
        refusal = RestartNoShutdown(kind="no shutdown", session_id=identity, name=name)
        refused.append(refusal)
        print(refusal_line(refusal))
    at = datetime.fromisoformat(settle.record_time())
    for saved in records:
        if ids and saved["progress"]["kind"] in {"waiting", "ready", "held by usage limit", "stopping"}:
            refusal = RestartNotStopped(kind="not stopped", session_id=saved["session"]["session_id"],
                name=saved["session"]["name"], progress=cast(Literal["waiting", "ready", "held by usage limit", "stopping"], saved["progress"]["kind"]))
            refused.append(refusal)
            print(refusal_line(refusal))
        elif ids and saved["progress"]["kind"] == "restarting" and (
            at - datetime.fromisoformat(saved["progress"]["at"])
        ).total_seconds() < CLAIM_STALE_SECONDS:
            print(f"{saved['session']['name']} is already restarting")
    eligible = [
        record for record in records
        if record["progress"]["kind"] in RESTARTABLE_PROGRESS | {"restarted"}
        or (record["progress"]["kind"] == "restarting" and
            (at - datetime.fromisoformat(record["progress"]["at"])).total_seconds() >= CLAIM_STALE_SECONDS)
    ]
    label = eligible[0]["label"] if eligible else hold_outcome.hold["label"] if isinstance(hold_outcome, AccountHeld) else login
    machine = eligible[0]["machine"] if eligible else hold_outcome.hold["machine"] if isinstance(hold_outcome, AccountHeld) else settle.local_machine()
    if not eligible:
        if not ids and not dry_run and isinstance(hold_outcome, AccountHeld) and not any(
            item["request_id"] == hold_outcome.hold["request_id"] for item in all_records
        ):
            clear_finished_account_hold(login)
        return MachineRestartResult(machine, label, "nothing", refused=refused, local_session_ids=frozenset(local_ids))
    state = DesktopLaunchState()
    started_at = time.monotonic()
    attempted: list[SessionShutdown] = []
    try:
        if dry_run:
            for record in sorted(eligible, key=_dry_run_entry_order):
                if record["progress"]["kind"] != "restarted":
                    _ = relaunch_session(record, dry_run=True, desktop_state=state)
            return MachineRestartResult(machine, label, "done", refused=refused, local_session_ids=frozenset(local_ids))
        previous_manual_ids = {record["session"]["session_id"] for record in eligible
                               if _prior_progress(record)["kind"] == "manual restart"}
        ordered = sorted(eligible, key=_entry_order)
        others = [record for record in ordered if ids or not _entry_order(record)[0]]
        requesters = [record for record in ordered if not ids and _entry_order(record)[0]]
        with ThreadPoolExecutor(max_workers=RESTART_LAUNCH_WORKERS) as executor:
            workers = [
                (record, executor.submit(relaunch_session, record, dry_run=False, desktop_state=state))
                for record in others if record["session"]["host"]["kind"] not in {"ghostty", "zed", "terminal"}
            ]
            for record in others:
                if record["session"]["host"]["kind"] in {"ghostty", "zed", "terminal"}:
                    current = relaunch_session(record, dry_run=False, desktop_state=state)
                    if current["progress"]["kind"] == "restarting" and not owns_relaunch_claim(record, current):
                        continue
                    attempted.append(current)
            for original, future in workers:
                current = future.result()
                if current["progress"]["kind"] == "restarting" and not owns_relaunch_claim(original, current):
                    continue
                attempted.append(current)
        missing_control = list(_confirm_entries(attempted))
        for record in requesters:
            other_unfinished = [
                item for item in session_records(login)
                if item["request_id"] == record["request_id"]
                and item["session"]["session_id"] != record["session"]["session_id"]
                and item["progress"]["kind"] not in {"restarted", "stop failed", "restart failed", "manual restart", "timers pending"}
            ]
            if other_unfinished:
                continue
            current = relaunch_session(record, dry_run=False, desktop_state=state)
            if current["progress"]["kind"] == "restarting" and not owns_relaunch_claim(record, current):
                continue
            missing_control.extend(_confirm_entries([current]))
            attempted.append(current)
        settle.tell_showrunners_outcome([*finished, *attempted])
        remaining = session_records(login)
        if isinstance(hold_outcome, AccountHeld) and not any(item["request_id"] == hold_outcome.hold["request_id"] for item in remaining):
            finished_requests = tuple(item["request_id"] for item in attempted if item["progress"]["kind"] == "restarted")
            if not ids:
                clear_finished_account_hold(login)
            elif finished_requests:
                clear_finished_account_hold(login, request_ids=finished_requests)
        result = replace(_machine_result(attempted, machine, label), elapsed_seconds=time.monotonic() - started_at)
        result = replace(result, refused=refused, no_remote_control=missing_control, local_session_ids=frozenset(local_ids))
        reported = tuple(ManualSessionRestart(record["session"]["name"], record["progress"]["command"])
                             for record in attempted if record["progress"]["kind"] == "manual restart"
                             and record["session"]["session_id"] not in previous_manual_ids)
        if reported:
            _print_result(result, already_reported=reported)
        else:
            _print_result(result)
        return result
    finally:
        state.restore(dry_run=dry_run)


def refusal_line(refusal: RestartRefusal) -> str:
    if refusal["kind"] == "no shutdown":
        return f"{refusal['name']}: no shutdown in progress"
    return f"{refusal['name']} is {refusal['progress']}, not stopped; nothing to restart"


def up_result(login: str, dry_run: bool = False, only: Iterable[str] = ()) -> PeerRestartResult:
    """Run the local verb and retain its printed lines in a validated peer object."""
    output = io.StringIO()
    with redirect_stdout(output):
        try:
            result = _up(login, dry_run, only)
        except (InvalidRecord, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            print(f"{settle.local_machine()}: restart failed: {error}")
            result = MachineRestartResult(settle.local_machine(), login, "partial", failed=1)
    return PeerRestartResult(machine=result.machine, label=result.label, outcome=result.status,
        lines=output.getvalue().splitlines(), no_remote_control=list(result.no_remote_control), refused=list(result.refused))


def parse_peer_result(text: str) -> PeerRestartResult:
    """Refuse older or malformed peer replies at the transport boundary."""
    value = cast(object, json.loads(text))
    if not isinstance(value, dict):
        raise ValueError("invalid peer restart result")
    fields = cast(dict[str, object], value)
    for key in ("machine", "label"):
        if not isinstance(fields.get(key), str) or not fields[key]:
            raise ValueError(f"invalid peer restart {key}")
    if fields.get("outcome") not in ("done", "partial", "nothing"):
        raise ValueError("invalid peer restart outcome")
    raw_lines = fields.get("lines")
    if not isinstance(raw_lines, list) or any(not isinstance(line, str) for line in cast(list[object], raw_lines)):
        raise ValueError("invalid peer restart lines")
    missing: list[NoRemoteControl] = []
    refusals: list[RestartRefusal] = []
    for key in ("no_remote_control", "refused"):
        raw = fields.get(key)
        if not isinstance(raw, list):
            raise ValueError(f"invalid peer restart {key}")
        for entry in cast(list[object], raw):
            if not isinstance(entry, dict):
                raise ValueError(f"invalid peer restart {key} entry")
            item = cast(dict[str, object], entry)
            if any(not isinstance(item.get(field), str) or not item[field] for field in ("session_id", "name")):
                raise ValueError(f"invalid peer restart {key} identity")
            identity, name = cast(str, item["session_id"]), cast(str, item["name"])
            if key == "no_remote_control":
                if item.get("alerts") not in ("user", "none"):
                    raise ValueError("invalid peer alert policy")
                missing.append(NoRemoteControl(session_id=identity, name=name, alerts=cast(Literal["user", "none"], item["alerts"])))
            elif item.get("kind") == "no shutdown":
                refusals.append(RestartNoShutdown(kind="no shutdown", session_id=identity, name=name))
            elif item.get("kind") == "not stopped" and item.get("progress") in ("waiting", "ready", "held by usage limit", "stopping"):
                refusals.append(RestartNotStopped(kind="not stopped", session_id=identity, name=name,
                    progress=cast(Literal["waiting", "ready", "held by usage limit", "stopping"], item["progress"])))
            else:
                raise ValueError("invalid peer restart refusal")
    return PeerRestartResult(machine=cast(str, fields["machine"]), label=cast(str, fields["label"]),
        outcome=cast(Literal["done", "partial", "nothing"], fields["outcome"]), lines=cast(list[str], raw_lines),
        no_remote_control=missing, refused=refusals)


def up(login: str, dry_run: bool = False, only: Iterable[str] = ()) -> int:
    """Resume each eligible local session without waiting on another machine."""
    try:
        result = _up(login, dry_run, only)
        return result.exit_status
    except (InvalidRecord, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"{settle.local_machine()}: restart failed: {error}")
        return 1


def _eligible_records(records: list[SessionShutdown]) -> list[SessionShutdown]:
    return [record for record in records if record["progress"]["kind"] in {
        "stopped", "already gone", "process identity lost", "restarting",
        "restart failed", "manual restart", "timers pending", "stop failed",
    }]


def _remote_records() -> RemoteRecordLookup:
    status, output = run_remote(["records", "--json", "--here"])
    if status != 0:
        return {"kind": "machine not reached", "machine": other_machine()}
    return {"kind": "records read", "records": parse_session_records(output)}


def _remote_holds() -> RemoteHoldLookup:
    status, output = run_remote(["holds", "--json", "--here"])
    if status != 0:
        return {"kind": "machine not reached", "machine": other_machine()}
    return {"kind": "holds read", "holds": parse_account_holds(output)}


def _account_candidate_lines(
    records: list[SessionShutdown], holds: list[AccountHold],
) -> list[str]:
    lines = [
        f"{record['machine']}: {record['label']} {record['session']['name']}: {record['progress']['kind']}"
        for record in records
    ]
    session_logins = {record["login"].casefold() for record in records}
    for hold in holds:
        if hold["login"].casefold() not in session_logins:
            since = datetime.fromisoformat(hold["requested_at"]).astimezone(user_zone()).strftime(
                "%Y-%m-%d %H:%M %Z"
            )
            lines.append(f"{hold['machine']}: {hold['label']} is held by a shutdown (since {since})")
    return lines


def _account_from_records() -> Account:
    local = _eligible_records(all_session_records())
    local_holds = all_account_holds()
    remote = _remote_records()
    if remote["kind"] == "machine not reached":
        for line in _account_candidate_lines(local, local_holds):
            print(line)
        print(f"{remote['machine']} not reached; name the account to restart")
        raise RestartAccountNotDetermined("name the account to restart")
    remote_holds = _remote_holds()
    if remote_holds["kind"] == "machine not reached":
        for line in _account_candidate_lines(local, local_holds):
            print(line)
        print(f"{remote_holds['machine']} not reached; name the account to restart")
        raise RestartAccountNotDetermined("name the account to restart")
    candidates = [*local, *_eligible_records(remote["records"])]
    holds = [*local_holds, *remote_holds["holds"]]
    logins = {
        record["login"].casefold(): Account("claude", record["login"], record["label"])
        for record in [*holds, *candidates]
    }
    if len(logins) == 1:
        return next(iter(logins.values()))
    if logins:
        for line in _account_candidate_lines(candidates, holds):
            print(line)
    else:
        print("no shutdown record is ready to restart")
    raise RestartAccountNotDetermined("choose the account to restart")


def select_account(requested: AccountNamed | AccountNotNamed) -> Account:
    match requested:
        case AccountNamed(text):
            return named_claude_account(text)
        case AccountNotNamed():
            try:
                return own_claude_account()
            except UnreadableAccount:
                return _account_from_records()


class PeerRestartAvailability(Enum):
    NOT_ASKED = "not asked"
    NOT_REACHED = "not reached"
    REACHED_TO_BE_READ = "reached and to be read"


def _restart_alerts_enabled(login: str, only: Iterable[str] = (), *, peer: PeerRestartAvailability) -> bool:
    ids = tuple(only)
    try:
        records = [saved for saved in session_records(login)
                   if not ids or saved["session"]["session_id"] in ids]
    except (InvalidRecord, OSError, ValueError):
        return True
    if peer is PeerRestartAvailability.NOT_ASKED:
        return (not records and not ids) or any(saved["alerts"] == "user" for saved in _eligible_records(records))
    if peer is PeerRestartAvailability.NOT_REACHED:
        return (not records and not ids) or any(saved["alerts"] == "user" for saved in records)
    try:
        peer_records = _remote_records()
    except (InvalidRecord, OSError, ValueError):
        return (not records and not ids) or any(saved["alerts"] == "user" for saved in records)
    if peer_records["kind"] == "machine not reached":
        return (not records and not ids) or any(saved["alerts"] == "user" for saved in records)
    records.extend(
        saved for saved in peer_records["records"]
        if saved["login"].casefold() == login.casefold()
        and (not ids or saved["session"]["session_id"] in ids)
    )
    return (not records and not ids) or any(saved["alerts"] == "user" for saved in _eligible_records(records))


def restart_push(label: str, lines: list[str], *, failed: bool, phone_access: bool) -> tuple[str, str, ActionRequired]:
    account = json.dumps(label)
    diagnostics = sorted(lines, key=lambda line: not any(word in line.lower() for word in ("failed", "not reached", "no remote control")))
    details = "\n".join(diagnostics)
    details = details if len(details) <= 130 else details[:127].rsplit(" ", 1)[0] + "…"
    if failed:
        title = "Some Claude sessions did not restart"
        body = f"Sessions on Claude account {account} did not restart. Failure details: {details}\n\n"
        body += f"The status command shows what failed. Fix the cause. Then run /shutdown restart {account} in the same Claude session on natedev to try again."
        failures = [match for line in diagnostics if (match := re.match(r"[^:]+: ([^:]+) restart failed: (.*)", line)) is not None]
        if failures:
            name, reason = failures[0].groups()
            reason = reason if len(reason) <= 130 else reason[:127].rsplit(" ", 1)[0] + "…"
            title = f"Session {name} did not restart" if len(name) <= 15 else title
            body = f"Session {name} on Claude account {account} did not restart: {reason}.\n\n" + body.split("\n\n", 1)[1]
    else:
        title = "Restarted sessions lack phone access"
        names = ", ".join(line.partition(":")[0] for line in diagnostics)
        body = f"Sessions on Claude account {account} restarted. Remote Control is off, so you cannot reach them from your phone. Affected sessions: {names}.\n\n"
    if phone_access:
        body += "The status command shows each session's machine. On that machine, run /remote-control in its terminal to turn phone access back on."
    return title, body, ActionRequired(f"In a Claude session on natedev, run /shutdown status {account}.")


def restart(
    account: AccountNamed | AccountNotNamed,
    *,
    dry_run: bool,
    alerts: AlertWhenNotBack | NeverAlert,
    only: Iterable[str] = (),
    here: bool = False,
) -> int:
    """Resume an account's shutdown records on this and the other machine."""
    try:
        selected = select_account(account)
    except UnknownAccountName as error:
        print(f"shutdown: {error}", file=sys.stderr)
        return 2
    except RestartAccountNotDetermined as error:
        print(f"shutdown: {error}", file=sys.stderr)
        return 1
    except (UnreadableAccount, InvalidRecord, OSError, ValueError) as error:
        print(f"shutdown: {error}", file=sys.stderr)
        return 1

    notify = isinstance(alerts, AlertWhenNotBack) and not dry_run
    ids = tuple(only)
    local_ids: set[str] = set()
    try:
        local = _up(selected.login, dry_run, ids)
        local_ids = set(local.local_session_ids)
    except (
        InvalidRecord,
        OSError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        print(f"shutdown: restart failed: {error}", file=sys.stderr)
        local = MachineRestartResult(
            settle.local_machine(),
            selected.label,
            "partial",
            failed=1,
            failed_sessions=(
                FailedSessionRestart(selected.label, _error_reason(error)),
            ),
        )
    remote_args = ["up", selected.login, "--here", "--json"]
    if dry_run:
        remote_args.append("--dry-run")
    peer_ids = [identity for identity in ids if identity not in local_ids]
    if ids:
        remote_args.extend(("--only", ",".join(peer_ids)))
    peer = PeerRestartResult(machine=other_machine(), label=selected.label, outcome="nothing",
        lines=[], no_remote_control=[], refused=[])
    peer_availability = PeerRestartAvailability.NOT_ASKED
    if not here and (not ids or peer_ids):
        status, output = run_remote(remote_args, limit={"kind": "while link alive"})
        try:
            if status not in {0, 1}:
                raise ValueError("peer not reached")
            peer = parse_peer_result(output)
            peer_availability = PeerRestartAvailability.REACHED_TO_BE_READ
        except (ValueError, TypeError):
            peer_availability = PeerRestartAvailability.NOT_REACHED
    not_reached = peer_availability is PeerRestartAvailability.NOT_REACHED
    for line in peer["lines"]:
        print(line)
    incomplete = ""
    if not_reached:
        incomplete = (f"restart incomplete: {other_machine()} not reached — run /shutdown "
            "restart again when it is back")
        print(incomplete)
    missing = [item for item in (*local.no_remote_control, *peer["no_remote_control"]) if item["alerts"] == "user"]
    bring_back = local.status == "partial" or peer["outcome"] == "partial" or not_reached
    if notify:
        failure_alert = bring_back and _restart_alerts_enabled(selected.login, ids, peer=peer_availability)
        if failure_alert or missing:
            lines = _result_lines(local) if failure_alert else []
            if failure_alert:
                excluded = {refusal_line(item) for item in peer["refused"]}
                excluded.update(f"{item['name']}: running, no Remote Control" for item in peer["no_remote_control"])
                lines.extend(line for line in peer["lines"] if line not in excluded)
                if incomplete:
                    lines.append(incomplete)
            lines.extend(f"{item['name']}: running, no Remote Control" for item in missing)
            try:
                settle.alert_user(*restart_push(selected.label, lines, failed=failure_alert, phone_access=bool(missing)))
            except (OSError, RuntimeError, ValueError):
                pass
    return 0 if local.exit_status == 0 and peer["outcome"] != "partial" and not peer["refused"] else 1
