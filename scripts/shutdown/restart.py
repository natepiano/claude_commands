#!/usr/bin/env python3
"""Resume the sessions recorded by an account shutdown."""

from __future__ import annotations

import copy
from collections.abc import Iterable
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol, TypedDict, cast
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parents[1]
for dependency in ("whoami", "message", "notify", "production", "hooks"):
    sys.path.insert(0, str(SCRIPTS / dependency))

from account import (  # noqa: E402
    Account,
    UnknownAccountName,
    UnreadableAccount,
    named_account_prefix,
    named_claude_account,
    own_claude_account,
)
import add_unit  # noqa: E402
import conversation_pause  # noqa: E402
from inventory import (  # noqa: E402
    PaneNotRecorded,
    Session,
    TmuxHost,
    TmuxPane,
    WindowHost,
    inventory,
)
from record import (  # noqa: E402
    CLAIM_STALE_SECONDS,
    AccountHold,
    InvalidRecord,
    NoLiveRecord,
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
    def alert_user(
        self, summary: str, text: str, action: ActionRequired, *, machine: str = "",
    ) -> None: ...


settle = cast(ShutdownServices, cast(object, import_module("settle")))


ADD_UNIT = SCRIPTS / "production" / "add_unit.py"
SEND = SCRIPTS / "message" / "send.py"
SESSION_WAIT_SECONDS = 90.0
SESSION_POLL_SECONDS = 0.5
RESTART_LAUNCH_WORKERS = 6
REFUSED_ONLY_EXIT = 4
WINDOW_DELAY_SECONDS = 1.2
PACIFIC = ZoneInfo("America/Los_Angeles")
RESTARTABLE_PROGRESS = frozenset({"stopped", "already gone", "process identity lost", "manual restart", "restart failed", "timers pending"})
COMPLETE_PROGRESS = frozenset({"restarted"})
# Worst wins when one machine's `up` attempts several records.
STATUS_SEVERITY = {"nothing": 0, "done": 1, "partial": 2, "refused": 3}


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
    elapsed_seconds: float = 0.0
    refused: bool = False
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


def wait_for_sessions(session_ids: frozenset[str], timeout: float) -> frozenset[str]:
    """Wait once for all launched sessions and return those live at the last poll."""
    if not session_ids:
        return frozenset()
    deadline = time.monotonic() + timeout
    while True:
        live = session_ids & _live_session_ids()
        if live == session_ids:
            return live
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return live
        time.sleep(min(SESSION_POLL_SECONDS, remaining))


def _pacific(value: str) -> str:
    return datetime.fromisoformat(value).astimezone(PACIFIC).strftime(
        "%Y-%m-%d %H:%M %Z"
    )


def restart_note(
    record: SessionShutdown, restarted_at: str
) -> str:
    """Build the exact first prompt given to a resumed session."""
    opening = (
        f"Restarted after /shutdown of {record['label']} "
        f"(stopped {_pacific(record['requested_at'])}, "
        f"restarted {_pacific(restarted_at)}). "
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


def _resume_command(session: Session, prompt: str) -> str:
    default_dir = str(Path.home() / ".claude")
    config_dir = session.get("config_dir", default_dir)
    prefix = (
        named_account_prefix(Path(config_dir))
        if config_dir != default_dir
        else ""
    )
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
        f"cd {shlex.quote(session['cwd'])} && {prefix}ENABLE_TOOL_SEARCH=true command "
        f"{shlex.join(argv)}; exec zsh"
    )


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


def _unit_argv(
    record: SessionShutdown,
    *,
    missing_plan: bool = False,
) -> list[str]:
    session = record["session"]
    host = session["host"]
    if session["kind"] != "unit" or host["kind"] != "unit":
        raise ValueError("unit launch needs a unit host")
    recorded_plan = host["plan"]
    if missing_plan:
        plan = "<plan>"
    elif recorded_plan["kind"] == "plan":
        plan = _units_row_plan(host["doc"], host["unit"]) or recorded_plan["path"]
    else:
        raise ValueError("unit launch has no run record")
    note_path = _note_path(record, session["session_id"])
    return [
        *_add_unit_command(),
        "--production",
        host["doc"],
        # The recorded id, exactly: add_unit strips one "-unit" before adding its own.
        host["unit"],
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


def _new_window_argv(tmux: str, tmux_session: str, cwd: str, command: str) -> list[str]:
    return [
        tmux,
        "new-window",
        "-t",
        f"={tmux_session}:",
        "-c",
        cwd,
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
        _ = _run(_respawn_pane_argv(tmux, pane, session, command), dry_run=dry_run)
        return
    if probe.returncode == 0:
        _ = _run(
            _new_window_argv(tmux, host["tmux_session"], session["cwd"], command),
            dry_run=dry_run,
        )
        return
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
            _new_window_argv(tmux, host["tmux_session"], session["cwd"], command),
            dry_run=False,
        )


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
        if host["plan"]["kind"] == "no run record":
            _write_note(_note_path(record, session["session_id"]), note, dry_run)
            return ManualRestartRequired(
                shlex.join(_unit_argv(record, missing_plan=True))
            )
        note_path = _note_path(record, session["session_id"])
        _write_note(note_path, note, dry_run)
        try:
            _ = _run(_unit_argv(record), dry_run=dry_run)
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
            _tmux_launch(command, session, dry_run=dry_run)
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
    return written


def _record_timer_restore_progress(
    record: SessionShutdown,
    failures: tuple[PendingTimer, ...],
) -> None:
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


def _mark_restart_failed(
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
            _mark_restart_failed(record, reason)
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
            _mark_restart_failed(
                record,
                f"restart note not delivered: {_error_reason(error)}",
            )
        return False
    if delivery["kind"] == "sent":
        return True
    if not dry_run:
        _mark_restart_failed(
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
            _mark_restart_failed(
                record,
                f"queued shutdown messages not retired: {_error_reason(error)}",
            )
        return EntrySettled()
    was_live = session_id in _live_session_ids()
    note = restart_note(record, restarted_at)
    if was_live:
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
            _mark_restart_failed(
                record,
                f"restart prompt not recorded: {_error_reason(error)}",
            )
        return EntrySettled()
    except (OSError, subprocess.CalledProcessError, subprocess.SubprocessError) as error:
        if dry_run or not _launch_failure_left_session_live(record, error):
            return EntrySettled()
        launch = SessionLaunched()
    if isinstance(launch, ManualRestartRequired):
        if dry_run:
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
        _mark_restart_failed(
            record,
            f"session not live after {SESSION_WAIT_SECONDS:g} seconds",
        )
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
    lines.append(_timing_line(result))
    return lines


def _print_result(result: MachineRestartResult) -> None:
    for line in _result_lines(result):
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
        _mark_restart_failed(record, _error_reason(error))
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
            _mark_restart_failed(record, _error_reason(error))
            return record
        if cleaned["request_id"] != record["request_id"]:
            return record
        record = cleaned
        if record["progress"]["kind"] == "stopping":
            return record
        if _unfinished_cleanup(record):
            _mark_restart_failed(record, "owner cleanup not confirmed; run /shutdown restart again")
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
        (issue["kind"] == "codex server left running" or issue["kind"] == "unit tmux session left running")
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


def _confirm_entries(records: list[SessionShutdown]) -> None:
    launched = [record for record in records if record["progress"]["kind"] == "restarting"]
    live: frozenset[str] = wait_for_sessions(
        frozenset(record["session"]["session_id"] for record in launched),
        SESSION_WAIT_SECONDS,
    ) if launched else frozenset()
    for record in launched:
        progress = record["progress"]
        if progress["kind"] == "restarting":
            confirm_entry(record, progress["at"], record["session"]["session_id"] in live)
    for record in records:
        if record["progress"]["kind"] == "restarted":
            archive_session(record["login"], record["session"]["session_id"])


def _up(login: str, dry_run: bool, only: Iterable[str] = ()) -> MachineRestartResult:
    if not dry_run:
        for line in migrate(login):
            print(line)
    ids = tuple(only)
    all_records = session_records(login)
    hold_outcome = lookup_account_hold(login)
    records = [saved for saved in all_records if not ids or saved["session"]["session_id"] in ids]
    refused = False
    local_ids = {saved["session"]["session_id"] for saved in all_records}
    missing = frozenset(ids) - local_ids
    if missing:
        report = inventory(login, missing)
        for owner in report["sessions"]:
            if owner["session_id"] in missing and owner["kind"] != "seat":
                local_ids.add(owner["session_id"])
                print(f"{owner['name']}: no shutdown in progress")
                refused = True
    at = datetime.fromisoformat(settle.record_time())
    for saved in records:
        if ids and saved["progress"]["kind"] in {"waiting", "ready", "stopping"}:
            print(f"{saved['session']['name']} is {saved['progress']['kind']}, not stopped; nothing to restart")
            refused = True
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
        _confirm_entries(attempted)
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
            _confirm_entries([current])
            attempted.append(current)
        remaining = session_records(login)
        if isinstance(hold_outcome, AccountHeld) and not any(item["request_id"] == hold_outcome.hold["request_id"] for item in remaining):
            finished_requests = tuple(item["request_id"] for item in attempted if item["progress"]["kind"] == "restarted")
            if not ids:
                clear_finished_account_hold(login)
            elif finished_requests:
                clear_finished_account_hold(login, request_ids=finished_requests)
        result = replace(_machine_result(attempted, machine, label), elapsed_seconds=time.monotonic() - started_at)
        result = replace(result, refused=refused, local_session_ids=frozenset(local_ids))
        _print_result(result)
        return result
    finally:
        state.restore(dry_run=dry_run)


def up(login: str, dry_run: bool = False, only: Iterable[str] = ()) -> int:
    """Resume each eligible local session without waiting on another machine."""
    try:
        result = _up(login, dry_run, only)
        if result.refused and result.status in {"done", "nothing"}:
            return REFUSED_ONLY_EXIT
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
            since = datetime.fromisoformat(hold["requested_at"]).astimezone(PACIFIC).strftime(
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


def _restart_alerts_enabled(login: str, only: Iterable[str] = (), *, here: bool = False) -> bool:
    ids = tuple(only)
    try:
        records = [saved for saved in session_records(login)
                   if not ids or saved["session"]["session_id"] in ids]
    except (InvalidRecord, OSError, ValueError):
        return True
    if here:
        return (not records and not ids) or any(saved["alerts"] == "user" for saved in _eligible_records(records))
    try:
        peer = _remote_records()
    except (InvalidRecord, OSError, ValueError):
        return (not records and not ids) or any(saved["alerts"] == "user" for saved in records)
    if peer["kind"] == "machine not reached":
        return (not records and not ids) or any(saved["alerts"] == "user" for saved in records)
    records.extend(
        saved for saved in peer["records"]
        if saved["login"].casefold() == login.casefold()
        and (not ids or saved["session"]["session_id"] in ids)
    )
    return (not records and not ids) or any(saved["alerts"] == "user" for saved in _eligible_records(records))


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
    remote_args = ["up", selected.login]
    if dry_run:
        remote_args.append("--dry-run")
    peer_ids = [identity for identity in ids if identity not in local_ids]
    if ids:
        remote_args.extend(("--only", ",".join(peer_ids)))
    status, output = ((0, "") if here or (ids and not peer_ids) else run_remote(
        remote_args, limit={"kind": "while link alive"}
    ))
    if output:
        print(output)
    remote_failure = ""
    if status not in {0, 255, REFUSED_ONLY_EXIT} and not output.strip():
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
        needs_alert = (local.status == "partial" or status not in {0, REFUSED_ONLY_EXIT}) if ids else not fully_back
        if notify and not fully_back and needs_alert and _restart_alerts_enabled(selected.login, ids, here=here):
            lines.append(
                "Fix what is named above, then run /shutdown restart again; "
                + "it brings back only what is left."
            )
            try:
                settle.alert_user(
                    f"{selected.label} is not fully back",
                    "\n".join(lines),
                    ActionRequired(
                        "Fix what is named, then run /shutdown restart again."
                    ),
                )
            except (OSError, RuntimeError, ValueError):
                pass
    return 0 if local.exit_status == 0 and status in {0, 255} else 1
