#!/usr/bin/env python3
"""Resume the sessions recorded by an account shutdown."""

from __future__ import annotations

import copy
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, TypedDict, cast
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parents[1]
for dependency in ("whoami", "message", "notify", "production", "hooks"):
    sys.path.insert(0, str(SCRIPTS / dependency))

from account import (  # noqa: E402
    Account,
    UnknownAccountName,
    UnreadableAccount,
    named_claude_account,
    own_claude_account,
)
import conversation_pause  # noqa: E402
from inventory import (  # noqa: E402
    PaneNotRecorded,
    Session,
    TmuxHost,
    TmuxPane,
    WindowHost,
)
from record import (  # noqa: E402
    AllAccountSessions,
    InvalidRecord,
    NoLiveRecord,
    PendingTimer,
    ShutdownRecord,
    ShutdownScope,
    ShutdownSessionEntry,
    SessionRestoredTimersPending,
    SessionNeedsManualRestart,
    SessionProgress,
    SessionRestarted,
    SessionRestartFailed,
    SeatAvailableOnDemand,
    TimerRestore,
    archive,
    find_live,
    live_records,
    parse_records,
    update,
)
from remote import other_machine, run_remote  # noqa: E402
import sessions  # noqa: E402
import settle  # noqa: E402
import showrunner_footer  # noqa: E402
from user_action import ActionRequired, NoActionRequired  # noqa: E402


ADD_UNIT = SCRIPTS / "production" / "add_unit.py"
SEND = SCRIPTS / "message" / "send.py"
SESSION_WAIT_SECONDS = 90.0
SESSION_POLL_SECONDS = 0.5
WINDOW_DELAY_SECONDS = 1.2
PACIFIC = ZoneInfo("America/Los_Angeles")
RESTARTABLE_STATES = frozenset({"down", "stop partial", "restart partial"})
COMPLETE_PROGRESS = frozenset({"restarted", "seat available on demand"})


class RestartClaimRejected(Exception):
    """The record exists, but its current state cannot be claimed."""

    state: str

    def __init__(self, state: str) -> None:
        self.state = state
        super().__init__(state)


class RestartAccountNotDetermined(Exception):
    """No single account can be chosen safely from restart records."""


class ScheduledRestartPromptNotRecorded(Exception):
    """The launched restart prompt could not be marked as scheduled."""


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
class RestartClaimed:
    """The live shutdown record is exclusively claimed for restart."""

    record: ShutdownRecord


@dataclass(frozen=True)
class NothingToRestart:
    """No live shutdown record remains to claim."""


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
class RestartScopeKnown:
    """The shutdown scope was preserved before restart changed the record."""

    scope: ShutdownScope


@dataclass(frozen=True)
class RestartScopeNotFoundLocally:
    """No readable local scope was available; the peer has not been checked."""


@dataclass(frozen=True)
class RestartScopeUnavailable:
    """Neither machine had a readable live shutdown scope."""


@dataclass(frozen=True)
class SessionLaunched:
    """The recorded session launch was started."""


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


@dataclass(frozen=True)
class MachineRestartResult:
    machine: str
    label: str
    status: Literal["done", "partial", "nothing", "refused"]
    restarted: int = 0
    seats: int = 0
    manual: int = 0
    failed: int = 0
    failed_sessions: tuple[FailedSessionRestart, ...] = ()
    manual_sessions: tuple[ManualSessionRestart, ...] = ()
    timers_not_started: tuple[TimerNotStarted, ...] = ()
    refusal_line: str = ""

    @property
    def exit_status(self) -> int:
        return 0 if self.status in {"done", "nothing"} else 1


@dataclass
class DesktopLaunchState:
    starting_desktop: str = ""
    started_windows: int = 0


class RemoteRecordsRead(TypedDict):
    kind: Literal["records read"]
    records: list[ShutdownRecord]


class RemoteMachineNotReached(TypedDict):
    kind: Literal["machine not reached"]
    machine: str


RemoteRecordLookup = RemoteRecordsRead | RemoteMachineNotReached


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


def wait_for_session(session_id: str, timeout: float) -> bool:
    """Wait until the resumed session has a live registry record."""
    deadline = time.monotonic() + timeout
    while True:
        if session_id in _live_session_ids():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(SESSION_POLL_SECONDS, remaining))


def _pacific(value: str) -> str:
    return datetime.fromisoformat(value).astimezone(PACIFIC).strftime(
        "%Y-%m-%d %H:%M %Z"
    )


def restart_note(
    record: ShutdownRecord, entry: ShutdownSessionEntry, restarted_at: str
) -> str:
    """Build the exact first prompt given to a resumed session."""
    opening = (
        f"Restarted after /shutdown of {record['label']} "
        f"(stopped {_pacific(record['requested_at'])}, "
        f"restarted {_pacific(restarted_at)}). "
    )
    where = entry["where"]
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


def _resume_command(session: Session, prompt: str) -> str:
    argv = [
        "claude",
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
    argv.extend(("--settings", '{"disableAgentView": true}', prompt))
    return (
        f"cd {shlex.quote(session['cwd'])} && ENABLE_TOOL_SEARCH=true command "
        f"{shlex.join(argv)}; exec zsh"
    )


def _showrunner_prompt(session: Session) -> str:
    if session["kind"] != "showrunner":
        raise ValueError("only a showrunner has a production resume prompt")
    return f"/showrunner:produce {session['doc']} resume"


def _note_path(record: ShutdownRecord, session_id: str) -> Path:
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


def _unit_argv(
    record: ShutdownRecord,
    entry: ShutdownSessionEntry,
    *,
    missing_plan: bool = False,
) -> list[str]:
    session = entry["session"]
    host = session["host"]
    if session["kind"] != "unit" or host["kind"] != "unit":
        raise ValueError("unit launch needs a unit host")
    recorded_plan = host["plan"]
    if missing_plan:
        plan = "<plan>"
    elif recorded_plan["kind"] == "plan":
        plan = recorded_plan["path"]
    else:
        raise ValueError("unit launch has no run record")
    note_path = _note_path(record, session["session_id"])
    return [
        *_add_unit_command(),
        "--production",
        host["doc"],
        host["unit"].removesuffix("-unit"),
        "--plan",
        plan,
        "--resume",
        session["session_id"],
        "--cwd",
        session["cwd"],
        "--session-name",
        session["name"],
        "--tmux-session",
        host["tmux_session"],
        "--restart-note",
        str(note_path),
    ]


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


def _restore_desktop(state: DesktopLaunchState, *, dry_run: bool) -> None:
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


def _pane_is_reusable(
    tmux: str,
    host: TmuxHost,
    pane: TmuxPane,
    *,
    dry_run: bool,
) -> bool:
    pane_probe = _probe(
        [
            tmux,
            "display",
            "-p",
            "-t",
            pane["pane_id"],
            "#{session_name}\t#{pane_pid}\t#{pane_dead}",
        ],
        dry_run=dry_run,
    )
    if pane_probe.returncode != 0:
        return False
    fields = pane_probe.stdout.rstrip("\r\n").split("\t")
    if len(fields) != 3 or fields[0] != host["tmux_session"]:
        return False
    if fields[2] == "1":
        return True
    if fields[2] != "0":
        return False
    try:
        pane_pid = int(fields[1])
    except ValueError:
        return False
    if pane_pid != pane["pane_pid"]:
        return False

    children = _probe(["pgrep", "-P", str(pane_pid)], dry_run=dry_run)
    return children.returncode == 1


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


def _tmux_launch(command: str, session: Session, *, dry_run: bool) -> None:
    host = session["host"]
    if host["kind"] != "tmux":
        raise ValueError("tmux launch needs a tmux host")
    tmux = os.environ.get("SHUTDOWN_TMUX", "tmux")
    pane = _recorded_pane(host)
    reusable_pane = pane["kind"] == "pane" and _pane_is_reusable(
        tmux, host, pane, dry_run=dry_run
    )
    if pane["kind"] == "pane" and reusable_pane and not dry_run:
        _ = _run(
            _respawn_pane_argv(tmux, pane, session, command),
            dry_run=False,
        )
        return

    probe_argv = [tmux, "has-session", "-t", f"={host['tmux_session']}"]
    probe = _probe(probe_argv, dry_run=dry_run)
    if pane["kind"] == "pane" and reusable_pane:
        argv = _respawn_pane_argv(tmux, pane, session, command)
    elif probe.returncode == 0:
        argv = [
            tmux,
            "new-window",
            "-t",
            f"={host['tmux_session']}:",
            "-c",
            session["cwd"],
            "zsh",
            "-ic",
            command,
        ]
    else:
        argv = [
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
        ]
    _ = _run(argv, dry_run=dry_run)


def _launch_session(
    record: ShutdownRecord,
    entry: ShutdownSessionEntry,
    note: str,
    desktop_state: DesktopLaunchState,
    *,
    dry_run: bool,
) -> SessionLaunched | ManualRestartRequired:
    """Launch an entry or return its explicit manual-restart requirement."""
    session = entry["session"]
    host = session["host"]
    if session["kind"] == "unit":
        if host["kind"] != "unit":
            raise ValueError("unit session has no unit host")
        if host["plan"]["kind"] == "no run record":
            _write_note(_note_path(record, session["session_id"]), note, dry_run)
            return ManualRestartRequired(
                shlex.join(_unit_argv(record, entry, missing_plan=True))
            )
        note_path = _note_path(record, session["session_id"])
        _write_note(note_path, note, dry_run)
        try:
            _ = _run(_unit_argv(record, entry), dry_run=dry_run)
        finally:
            if not dry_run:
                note_path.unlink(missing_ok=True)
        return SessionLaunched()

    prompt = _showrunner_prompt(session) if session["kind"] == "showrunner" else note
    command = _resume_command(session, prompt)
    if host["kind"] == "unknown":
        return ManualRestartRequired(command)
    _record_scheduled_restart_prompt(
        session["session_id"], prompt, dry_run=dry_run
    )
    match host["kind"]:
        case "ghostty" | "zed":
            _window_launch(command, session, desktop_state, dry_run=dry_run)
        case "terminal":
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
        case "tmux":
            _tmux_launch(command, session, dry_run=dry_run)
        case "unit":
            raise ValueError("non-unit session has a unit host")
    return SessionLaunched()


def _message_key(record: ShutdownRecord, session_id: str) -> str:
    return f"shutdown-{record['login']}-{session_id}"


def _retire(record: ShutdownRecord, entry: ShutdownSessionEntry, *, dry_run: bool) -> None:
    session_id = entry["session"]["session_id"]
    _ = _run(
        settle.retire_command(record["login"], session_id),
        dry_run=dry_run,
    )


def _send_restart_note(
    record: ShutdownRecord,
    entry: ShutdownSessionEntry,
    note: str,
    *,
    dry_run: bool,
) -> settle.MessageDelivery:
    session_id = entry["session"]["session_id"]
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
        return settle.MessageSent(kind="sent")
    return settle.send_message(
        f"session:{session_id}",
        f"{record['label']} restarted",
        note,
        action=NoActionRequired(),
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
    entry: ShutdownSessionEntry, pending: list[PendingTimer]
) -> list[TimerRestore]:
    timers_by_instance = {timer["instance"]: timer for timer in entry["timers"]}
    try:
        return [timers_by_instance[timer["instance"]] for timer in pending]
    except KeyError as error:
        raise ValueError(
            f"pending timer {error.args[0]} is missing from the restart record"
        ) from error


def _set_entry_progress(
    login: str, session_id: str, progress: SessionProgress
) -> ShutdownRecord:
    def change(record: ShutdownRecord) -> None:
        for entry in record["entries"]:
            if entry["session"]["session_id"] == session_id:
                entry["progress"] = progress
                return
        raise ValueError(f"shutdown entry {session_id} disappeared")

    return update(login, change)


def _record_timer_restore_progress(
    record: ShutdownRecord,
    entry: ShutdownSessionEntry,
    failures: tuple[PendingTimer, ...],
) -> None:
    if failures:
        _record_pending_timers(record, entry, failures)
        return
    progress: SessionProgress = SessionRestarted(
        kind="restarted", at=settle.record_time()
    )
    entry["progress"] = progress
    _ = _set_entry_progress(
        record["login"], entry["session"]["session_id"], progress
    )


def _record_pending_timers(
    record: ShutdownRecord,
    entry: ShutdownSessionEntry,
    pending: tuple[PendingTimer, ...],
) -> None:
    progress: SessionProgress = SessionRestoredTimersPending(
        kind="timers pending",
        at=settle.record_time(),
        timers=list(pending),
    )
    entry["progress"] = progress
    _ = _set_entry_progress(
        record["login"], entry["session"]["session_id"], progress
    )


def _record_relaunched_timers_pending(
    record: ShutdownRecord, entry: ShutdownSessionEntry
) -> None:
    _record_pending_timers(
        record,
        entry,
        tuple(
            PendingTimer(
                instance=timer["instance"],
                reason="not yet restored after relaunch",
            )
            for timer in entry["timers"]
        ),
    )


def _entry_order(record: ShutdownRecord, entry: ShutdownSessionEntry) -> tuple[int, str]:
    session = entry["session"]
    requester = record["requested_by"]
    if (
        requester["kind"] == "session"
        and requester["session_id"] == session["session_id"]
    ):
        return 3, session["name"]
    return (
        {"unit": 0, "showrunner": 1, "top-level": 2, "seat": 4}[session["kind"]],
        session["name"],
    )


def _claim(login: str) -> RestartClaimed | NothingToRestart:
    found = find_live(login)
    if found["kind"] == "no shutdown":
        return NothingToRestart()

    def change(record: ShutdownRecord) -> None:
        if record["state"] not in RESTARTABLE_STATES:
            raise RestartClaimRejected(record["state"])
        record["state"] = "restarting"

    try:
        return RestartClaimed(update(login, change))
    except NoLiveRecord:
        return NothingToRestart()


def _move_to_partial(login: str) -> None:
    try:
        def change(record: ShutdownRecord) -> None:
            record["state"] = "restart partial"

        _ = update(login, change)
    except (InvalidRecord, NoLiveRecord, OSError, ValueError):
        pass


def _mark_restart_failed(
    record: ShutdownRecord,
    entry: ShutdownSessionEntry,
    reason: str,
) -> None:
    entry["progress"] = SessionRestartFailed(
        kind="restart failed", at=settle.record_time(), reason=reason
    )
    _ = _set_entry_progress(
        record["login"], entry["session"]["session_id"], entry["progress"]
    )


def _restart_note_delivered(
    record: ShutdownRecord,
    entry: ShutdownSessionEntry,
    note: str,
    *,
    dry_run: bool,
) -> bool:
    try:
        delivery = _send_restart_note(record, entry, note, dry_run=dry_run)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        if not dry_run:
            _mark_restart_failed(
                record,
                entry,
                f"restart note not delivered: {_error_reason(error)}",
            )
        return False
    if delivery["kind"] == "sent":
        return True
    if not dry_run:
        _mark_restart_failed(
            record,
            entry,
            "restart note not delivered: "
            + _first_line(delivery["reason"], "queued"),
        )
    return False


def _process_session_entry(
    record: ShutdownRecord,
    entry: ShutdownSessionEntry,
    restarted_at: str,
    desktop_state: DesktopLaunchState,
    *,
    dry_run: bool,
) -> None:
    progress = entry["progress"]
    if progress["kind"] in COMPLETE_PROGRESS:
        return
    session = entry["session"]
    session_id = session["session_id"]
    relaunching_pending_timers = False
    if progress["kind"] == "timers pending":
        relaunching_pending_timers = True
        match _session_liveness(session_id):
            case SessionLive():
                timers = _pending_timer_restores(entry, progress["timers"])
                failures = _restore_timers(timers, dry_run=dry_run)
                if not dry_run:
                    _record_timer_restore_progress(record, entry, failures)
                return
            case SessionLivenessUnknown():
                return
            case SessionNotLive():
                pass
    try:
        _retire(record, entry, dry_run=dry_run)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        if not dry_run:
            _mark_restart_failed(
                record,
                entry,
                f"queued shutdown messages not retired: {_error_reason(error)}",
            )
        return
    was_live = session_id in _live_session_ids()
    note = restart_note(record, entry, restarted_at)
    if was_live:
        if progress["kind"] != "manual restart" and not _restart_note_delivered(
            record, entry, note, dry_run=dry_run
        ):
            return
        timer_failures = _restore_timers(entry["timers"], dry_run=dry_run)
        if not dry_run:
            _record_timer_restore_progress(record, entry, timer_failures)
        return

    if progress["kind"] == "manual restart":
        if dry_run:
            print(f"manual restart {session['name']}: {progress['command']}")
        return

    try:
        launch = _launch_session(
            record, entry, note, desktop_state, dry_run=dry_run
        )
    except ScheduledRestartPromptNotRecorded as error:
        if not dry_run:
            _mark_restart_failed(
                record,
                entry,
                f"restart prompt not recorded: {_error_reason(error)}",
            )
        return
    except (OSError, subprocess.CalledProcessError, subprocess.SubprocessError) as error:
        if not dry_run:
            _mark_restart_failed(record, entry, _error_reason(error))
        return
    if isinstance(launch, ManualRestartRequired):
        if dry_run:
            print(f"manual restart {session['name']}: {launch.command}")
        if not dry_run:
            entry["progress"] = SessionNeedsManualRestart(
                kind="manual restart", command=launch.command
            )
            _ = _set_entry_progress(record["login"], session_id, entry["progress"])
        return
    if dry_run:
        return
    if relaunching_pending_timers:
        _record_relaunched_timers_pending(record, entry)
    if not wait_for_session(session_id, SESSION_WAIT_SECONDS):
        entry["progress"] = SessionRestartFailed(
            kind="restart failed",
            at=settle.record_time(),
            reason=f"session not live after {SESSION_WAIT_SECONDS:g} seconds",
        )
        _ = _set_entry_progress(record["login"], session_id, entry["progress"])
        return
    if session["kind"] == "showrunner":
        if not _restart_note_delivered(record, entry, note, dry_run=False):
            return
    timer_failures = _restore_timers(entry["timers"], dry_run=False)
    _record_timer_restore_progress(record, entry, timer_failures)


def _process_seats(
    record: ShutdownRecord, *, dry_run: bool
) -> None:
    entries = {
        entry["session"]["session_id"]: entry for entry in record["entries"]
    }
    live = _live_session_ids()
    for entry in sorted(
        (
            item
            for item in record["entries"]
            if item["session"]["kind"] == "seat"
            and item["progress"]["kind"] not in COMPLETE_PROGRESS
        ),
        key=lambda item: item["session"]["name"],
    ):
        try:
            _retire(record, entry, dry_run=dry_run)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            if not dry_run:
                _mark_restart_failed(
                    record,
                    entry,
                    f"queued shutdown messages not retired: {_error_reason(error)}",
                )
            continue
        session = entry["session"]
        if session["kind"] != "seat":
            raise ValueError("seat restart processing received a non-seat entry")
        owner = session["owner"]
        if owner["kind"] == "director":
            owner_session_id = owner["session_id"]
            owner_entry = entries.get(owner_session_id)
            owner_is_live = owner_session_id in live
        else:
            owner_entry = None
            owner_is_live = False
        available = owner_entry is None or owner_is_live
        if owner_entry is not None and owner_entry["progress"]["kind"] in {
            "restarted",
            "manual restart",
        }:
            available = True
        if dry_run:
            continue
        if available:
            entry["progress"] = SeatAvailableOnDemand(
                kind="seat available on demand", at=settle.record_time()
            )
        else:
            if owner_entry is None:
                raise RuntimeError("a seat without a director must be available")
            owner_name = owner_entry["session"]["name"]
            entry["progress"] = SessionRestartFailed(
                kind="restart failed",
                at=settle.record_time(),
                reason=f"owner {owner_name} not back",
            )
        _ = _set_entry_progress(
            record["login"], session["session_id"], entry["progress"]
        )


def _finish_record(record: ShutdownRecord) -> MachineRestartResult:
    latest = find_live(record["login"])
    if latest["kind"] == "no shutdown":
        return MachineRestartResult(record["machine"], record["label"], "nothing")
    current = latest["record"]
    restarted = sum(
        entry["progress"]["kind"] in {"restarted", "timers pending"}
        for entry in current["entries"]
    )
    seats = sum(
        entry["progress"]["kind"] == "seat available on demand"
        for entry in current["entries"]
    )
    manual = sum(
        entry["progress"]["kind"] == "manual restart"
        for entry in current["entries"]
    )
    failed = sum(
        entry["progress"]["kind"] == "restart failed"
        for entry in current["entries"]
    )
    complete = all(
        entry["progress"]["kind"] in COMPLETE_PROGRESS
        for entry in current["entries"]
    )

    def change(changing: ShutdownRecord) -> None:
        changing["state"] = "up" if complete else "restart partial"

    _ = update(record["login"], change)
    if complete:
        archive(record["login"])
    failed_sessions: list[FailedSessionRestart] = []
    manual_sessions: list[ManualSessionRestart] = []
    timers_not_started: list[TimerNotStarted] = []
    for entry in current["entries"]:
        progress = entry["progress"]
        if progress["kind"] == "restart failed":
            failed_sessions.append(
                FailedSessionRestart(entry["session"]["name"], progress["reason"])
            )
        elif progress["kind"] == "manual restart":
            manual_sessions.append(
                ManualSessionRestart(entry["session"]["name"], progress["command"])
            )
        elif progress["kind"] == "timers pending":
            timers_not_started.extend(
                TimerNotStarted(
                    entry["session"]["name"], timer["instance"], timer["reason"]
                )
                for timer in progress["timers"]
            )
    return MachineRestartResult(
        current["machine"],
        current["label"],
        "done" if complete else "partial",
        restarted,
        seats,
        manual,
        failed,
        tuple(failed_sessions),
        tuple(manual_sessions),
        tuple(timers_not_started),
    )


def _result_lines(result: MachineRestartResult) -> list[str]:
    if result.status == "nothing":
        return []
    if result.status == "refused":
        return [result.refusal_line] if result.refusal_line else []
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
    return lines


def _print_result(result: MachineRestartResult) -> None:
    for line in _result_lines(result):
        print(line)


def _up(login: str, dry_run: bool) -> MachineRestartResult:
    found = find_live(login)
    machine = settle.local_machine()
    if found["kind"] == "no shutdown":
        try:
            account = named_claude_account(login)
            label = account.label
        except UnknownAccountName:
            label = login
        print(f"{machine}: no shutdown of {label} to restart")
        return MachineRestartResult(machine, label, "nothing")
    existing = found["record"]
    if dry_run:
        if existing["state"] == "restarting":
            print(
                "{}: restart of {} already running".format(
                    existing["machine"], existing["label"]
                )
            )
            return MachineRestartResult(
                existing["machine"], existing["label"], "nothing"
            )
        if existing["state"] not in RESTARTABLE_STATES:
            refusal = "{}: shutdown of {} is {}; nothing restarted".format(
                existing["machine"], existing["label"], existing["state"]
            )
            result = MachineRestartResult(
                existing["machine"], existing["label"], "refused",
                refusal_line=refusal,
            )
            _print_result(result)
            return result
        record = copy.deepcopy(existing)
    else:
        try:
            claimed = _claim(login)
        except RestartClaimRejected as rejected:
            if rejected.state == "restarting":
                print(
                    "{}: restart of {} already running".format(
                        existing["machine"], existing["label"]
                    )
                )
                return MachineRestartResult(
                    existing["machine"], existing["label"], "nothing"
                )
            refusal = "{}: shutdown of {} is {}; nothing restarted".format(
                existing["machine"], existing["label"], rejected.state
            )
            result = MachineRestartResult(
                existing["machine"], existing["label"], "refused",
                refusal_line=refusal,
            )
            _print_result(result)
            return result
        match claimed:
            case NothingToRestart():
                print(
                    f"{machine}: no shutdown of {existing['label']} to restart"
                )
                return MachineRestartResult(
                    machine, existing["label"], "nothing"
                )
            case RestartClaimed(record=claimed_record):
                record = claimed_record

    desktop_state = DesktopLaunchState()
    restarted_at = settle.record_time()
    try:
        entries = sorted(record["entries"], key=lambda entry: _entry_order(record, entry))
        for entry in entries:
            if entry["session"]["kind"] != "seat":
                _process_session_entry(
                    record,
                    entry,
                    restarted_at,
                    desktop_state,
                    dry_run=dry_run,
                )
        _process_seats(record, dry_run=dry_run)
        if dry_run:
            return MachineRestartResult(
                record["machine"], record["label"], "done"
            )
        result = _finish_record(record)
        _print_result(result)
        return result
    except BaseException:
        if not dry_run:
            _move_to_partial(login)
        raise
    finally:
        _restore_desktop(desktop_state, dry_run=dry_run)


def up(login: str, dry_run: bool = False) -> int:
    """Resume one machine's live record for `login`."""
    try:
        return _up(login, dry_run).exit_status
    except (
        InvalidRecord,
        OSError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        print(f"{settle.local_machine()}: restart failed: {_error_reason(error)}")
        return 1


def _eligible_records(records: list[ShutdownRecord]) -> list[ShutdownRecord]:
    return [record for record in records if record["state"] in RESTARTABLE_STATES]


def _remote_records() -> RemoteRecordLookup:
    status, output = run_remote(["records", "--json", "--here"])
    if status != 0:
        return {"kind": "machine not reached", "machine": other_machine()}
    return {"kind": "records read", "records": parse_records(output)}


def _account_from_records() -> Account:
    local = _eligible_records(live_records())
    remote = _remote_records()
    if remote["kind"] == "machine not reached":
        for record in local:
            print(f"{record['machine']}: {record['label']} {record['state']}")
        print(f"{remote['machine']} not reached; name the account to restart")
        raise RestartAccountNotDetermined("name the account to restart")
    candidates = [*local, *_eligible_records(remote["records"])]
    logins = {record["login"].casefold(): record for record in candidates}
    if len(logins) == 1:
        only = next(iter(logins.values()))
        return Account("claude", only["login"], only["label"])
    if candidates:
        for record in candidates:
            print(f"{record['machine']}: {record['label']} {record['state']}")
    else:
        print("no shutdown record is ready to restart")
    raise RestartAccountNotDetermined("choose the account to restart")


def _selected_account(requested: AccountNamed | AccountNotNamed) -> Account:
    match requested:
        case AccountNamed(text):
            return named_claude_account(text)
        case AccountNotNamed():
            try:
                return own_claude_account()
            except UnreadableAccount:
                return _account_from_records()


def _local_restart_scope(
    login: str,
) -> RestartScopeKnown | RestartScopeNotFoundLocally:
    try:
        found = find_live(login)
    except (InvalidRecord, OSError, ValueError):
        return RestartScopeNotFoundLocally()
    if found["kind"] == "live":
        return RestartScopeKnown(found["record"]["scope"])
    return RestartScopeNotFoundLocally()


def _remote_restart_scope(
    login: str,
) -> RestartScopeKnown | RestartScopeUnavailable:
    try:
        remote = _remote_records()
    except (
        InvalidRecord,
        OSError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
    ):
        remote = RemoteMachineNotReached(
            kind="machine not reached", machine=other_machine()
        )
    if remote["kind"] == "records read":
        for remote_record in remote["records"]:
            if remote_record["login"].casefold() == login.casefold():
                return RestartScopeKnown(remote_record["scope"])
    return RestartScopeUnavailable()


def _alert_scope(
    found: RestartScopeKnown | RestartScopeUnavailable,
) -> ShutdownScope:
    if isinstance(found, RestartScopeKnown):
        return found.scope
    return AllAccountSessions(kind="all account sessions")


def restart(
    account: AccountNamed | AccountNotNamed,
    *,
    dry_run: bool,
    alerts: AlertWhenNotBack | NeverAlert,
) -> int:
    """Resume an account's shutdown records on this and the other machine."""
    try:
        selected = _selected_account(account)
    except UnknownAccountName as error:
        print(f"shutdown: {error}", file=sys.stderr)
        return 2
    except RestartAccountNotDetermined as error:
        print(f"shutdown: {error}", file=sys.stderr)
        return 1
    except (UnreadableAccount, InvalidRecord, OSError, ValueError) as error:
        print(f"shutdown: {error}", file=sys.stderr)
        return 1

    local_scope: RestartScopeKnown | RestartScopeNotFoundLocally = (
        RestartScopeNotFoundLocally()
    )
    if isinstance(alerts, AlertWhenNotBack) and not dry_run:
        local_scope = _local_restart_scope(selected.login)

    try:
        local = _up(selected.login, dry_run)
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
    alert_scope: (
        RestartScopeKnown
        | RestartScopeNotFoundLocally
        | RestartScopeUnavailable
    ) = local_scope
    if (
        isinstance(alerts, AlertWhenNotBack)
        and not dry_run
        and local.status not in {"done", "nothing"}
        and isinstance(alert_scope, RestartScopeNotFoundLocally)
    ):
        alert_scope = _remote_restart_scope(selected.login)
    remote_args = ["up", selected.login]
    if dry_run:
        remote_args.append("--dry-run")
    status, output = run_remote(
        remote_args, limit={"kind": "while link alive"}
    )
    if output:
        print(output)
    remote_failure = ""
    if status not in {0, 255} and not output.strip():
        remote_failure = (
            f"{other_machine()}: restart failed (exit {status})"
        )
        print(remote_failure)
    incomplete = ""
    if status == 255:
        incomplete = (
            f"restart incomplete: {other_machine()} not reached — run /shutdown "
            "restart again when it is back"
        )
        print(incomplete)

    if not dry_run:
        lines = _result_lines(local)
        if output:
            lines.extend(output.splitlines())
        if remote_failure:
            lines.append(remote_failure)
        if incomplete:
            lines.append(incomplete)
        fully_back = (
            local.status in {"done", "nothing"}
            and status == 0
            and not remote_failure
            and not incomplete
        )
        if isinstance(alerts, AlertWhenNotBack) and not fully_back:
            if isinstance(alert_scope, RestartScopeNotFoundLocally):
                alert_scope = _remote_restart_scope(selected.login)
            lines.append(
                "Fix what is named above, then run /shutdown restart again; "
                + "it brings back only what is left."
            )
            try:
                settle.alert_user(
                    _alert_scope(alert_scope),
                    f"{selected.label} is not fully back",
                    "\n".join(lines),
                    ActionRequired(
                        "Fix what is named, then run /shutdown restart again."
                    ),
                )
            except (OSError, RuntimeError, ValueError):
                pass
    return 0 if local.exit_status == 0 and status in {0, 255} else 1
