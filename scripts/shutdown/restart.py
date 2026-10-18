#!/usr/bin/env python3
"""Resume the sessions recorded by an account shutdown."""

from __future__ import annotations

import copy
import hashlib
import io
import json
from contextlib import contextmanager, redirect_stdout
from contextvars import ContextVar
from collections.abc import Generator, Iterable, Sequence
import os
import re
import shlex
import subprocess
import sys
import syslog
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Literal, Never, Protocol, TypedDict, cast

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
    NAMED_ACCOUNT_VARIABLES,
)
import production_units  # noqa: E402
import herdr  # noqa: E402
import inventory as inventory_work  # noqa: E402
import conversation_pause  # noqa: E402
import relaunch  # noqa: E402
from probe_session import PROBE_ENV  # noqa: E402
from inventory import (  # noqa: E402
    Session,
    Desktop,
    Host,
    DesktopNotInSnapshot,
    herdr_presentation,
    live_supervisor,
)
from record import (  # noqa: E402
    CLAIM_STALE_SECONDS,
    AccountHold,
    InvalidRecord,
    NoLiveRecord,
    PendingTimer,
    SessionShutdown,
    PausedSeat,
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
    RecoverySnapshot,
    SnapshotCodexSession,
    SnapshotSession,
    all_snapshots,
    acknowledge_snapshot,
    materialize_snapshot,
)
import record as recovery_store  # noqa: E402
import launch_permission as restore_permission  # noqa: E402
from remote import other_machine, run_remote  # noqa: E402
import sessions  # noqa: E402
import remote_control  # noqa: E402
import showrunner_footer  # noqa: E402
import stop as stop_work  # noqa: E402
from push_texts import RenderedPush, render  # noqa: E402
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
    def elapsed_text(self, seconds: int) -> str: ...
    def stopped_sessions_text(self, count: int) -> str: ...
    def shutdown_command(self, *arguments: str) -> str: ...
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
        self, push: RenderedPush, *, machine: str = "",
    ) -> None: ...
    def tell_showrunners_outcome(self, records: list[SessionShutdown]) -> None: ...
    def not_back_text(self, saved: SessionShutdown) -> str: ...
    def mark_not_back_pushed(self, saved: SessionShutdown) -> None: ...


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
    """The registry or saved process identity positively identifies a live owner."""


@dataclass(frozen=True)
class SessionNotLive:
    """A readable session registry contains no live record for the session."""


@dataclass(frozen=True)
class SessionLivenessUnknown:
    """The session registry cannot safely establish whether the session is live."""

    reason: str


@dataclass(frozen=True)
class AccountObservation:
    """Evidence of which account last owned one session."""

    login: str
    label: str
    at: datetime


@dataclass(frozen=True)
class RestoreAccountChosen:
    login: str
    source_login: str


@dataclass(frozen=True)
class RestoreAccountConflict:
    accounts: tuple[str, ...]


def _restore_account(observations: Sequence[AccountObservation | RestoreAccountConflict]) -> RestoreAccountChosen | RestoreAccountConflict:
    conflicts = [item for item in observations if isinstance(item, RestoreAccountConflict)]
    known = [item for item in observations if isinstance(item, AccountObservation)]
    if conflicts:
        accounts = {account for conflict in conflicts for account in conflict.accounts}
        accounts.update(f"{item.label} ({item.login})" for item in known)
        return RestoreAccountConflict(tuple(sorted(accounts)))
    latest = max(item.at for item in known)
    current = {item.login.casefold(): item for item in known if item.at == latest}
    if len(current) == 1:
        login = next(iter(current))
        return RestoreAccountChosen(login, login)
    return RestoreAccountConflict(tuple(sorted(f"{item.label} ({item.login})" for item in current.values())))


def _record_observation(saved: SessionShutdown) -> AccountObservation | RestoreAccountConflict:
    progress = saved["progress"]
    target_name = saved.get("target", "")
    if target_name and progress["kind"] == "restarted":
        try:
            from scripts.accounts import accounts

            resolved = accounts.resolve_history_label(target_name, saved["requested_at"])
            if not isinstance(resolved, accounts.ResolvedHistoryLabel):
                return RestoreAccountConflict((f"{saved['label']} ({saved['login']})", f"{target_name} (unavailable)"))
            target = named_claude_account(resolved.label)
        except (UnknownAccountName, UnreadableAccount):
            return RestoreAccountConflict((f"{saved['label']} ({saved['login']})", f"{target_name} (unavailable)"))
        return AccountObservation(target.login, target.label, datetime.fromisoformat(progress["at"]))
    at = progress["at"] if progress["kind"] == "restarted" else saved["requested_at"]
    return AccountObservation(saved["login"], saved["label"], datetime.fromisoformat(at))


@dataclass(frozen=True)
class IncompleteShutdownRefused:
    reason: str


@dataclass(frozen=True)
class IncompleteShutdownStillLive:
    """The owner is running, so its unfinished shutdown remains unchanged."""


INCOMPLETE_PROGRESS = frozenset({"waiting", "ready", "held by usage limit", "reaping", "stopping"})


def _recover_incomplete_shutdown(
    saved: SessionShutdown, *, dry_run: bool,
) -> SessionShutdown | IncompleteShutdownRefused | IncompleteShutdownStillLive:
    if saved["progress"]["kind"] not in INCOMPLETE_PROGRESS:
        return saved
    match _owner_liveness(saved["session"]):
        case SessionLive():
            return IncompleteShutdownStillLive()
        case SessionLivenessUnknown(reason):
            return IncompleteShutdownRefused(reason)
        case SessionNotLive():
            pass
    if dry_run:
        return {**saved, "progress": {"kind": "already gone", "at": settle.record_time()}}
    # The stop path proves worker and host cleanup too, and owns its stopping claim.
    cleaned = stop_work.stop_session(saved)
    if cleaned["request_id"] != saved["request_id"]:
        return IncompleteShutdownRefused("shutdown request changed during recovery")
    if cleaned["progress"]["kind"] not in RESTARTABLE_PROGRESS or _unfinished_cleanup(cleaned):
        return IncompleteShutdownRefused("owner cleanup not confirmed: " + cleaned["progress"]["kind"])
    return cleaned


@dataclass(frozen=True)
class SessionLaunched:
    """A started process plus the evidence for its first prompt."""

    prompt_outcome: herdr.PromptSubmitOutcome | herdr.PromptSubmissionNotRequired = herdr.PromptNotSubmitted("no launch submit proof")


@dataclass(frozen=True)
class HerdrLaunch:
    server: herdr.ServerCreated | herdr.ServerAdopted | herdr.WindowServerCreated
    prompt_outcome: herdr.PromptSubmitOutcome


@dataclass(frozen=True)
class LaunchPromptProofNotRead:
    """Read the proof for the current process launch before confirming it."""


_LAUNCH_PROMPT_PROOF_NOT_READ = LaunchPromptProofNotRead()


@dataclass(frozen=True)
class MoveNoteTimes:
    paused_at: str
    resumed_at: str


@dataclass(frozen=True)
class MoveNoteTimesUnrecorded:
    """A preview has no completed pause and resume yet."""


_MOVE_NOTE_TIMES_UNRECORDED = MoveNoteTimesUnrecorded()


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
    progress: Literal["waiting", "ready", "held by usage limit", "reaping", "stopping"]


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
    no_remote_control: list[Never]
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
    workers: int = 0
    manual: int = 0
    failed: int = 0
    failed_sessions: tuple[FailedSessionRestart, ...] = ()
    manual_sessions: tuple[ManualSessionRestart, ...] = ()
    timers_not_started: tuple[TimerNotStarted, ...] = ()
    elapsed_seconds: float = 0.0
    refused: list[RestartRefusal] = field(default_factory=list)
    local_session_ids: frozenset[str] = frozenset()

    @property
    def exit_status(self) -> int:
        return 0 if self.status in {"done", "nothing"} and not self.failed and not self.refused else 1


_SESSION_KIND_NOT_READ = herdr.SessionKindNotRead()
_COMMAND_SECURITY_SESSION: ContextVar[herdr.SecuritySession | herdr.SessionKindNotRead] = ContextVar(
    "restart_security_session", default=_SESSION_KIND_NOT_READ)


def _command_security_session() -> herdr.SecuritySession:
    known = _COMMAND_SECURITY_SESSION.get()
    return herdr.security_session() if isinstance(known, herdr.SessionKindNotRead) else known


@dataclass
class DesktopLaunchState:
    session_kind: herdr.SecuritySession = field(default_factory=lambda: herdr.SessionKindUnreadable("command session not supplied"))
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
    return frozenset(item["sessionId"] for item in [*sessions.live_sessions(), *sessions.running_sessions()])


def _process_liveness(pid: int, recorded_start: str) -> SessionLive | SessionNotLive | SessionLivenessUnknown:
    if not recorded_start:
        return SessionNotLive()
    current = sessions.process_start(pid)
    if current is not None and current != recorded_start:
        return SessionNotLive()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return SessionNotLive()
    except PermissionError:
        if current == recorded_start:
            return SessionLive()
    except (OSError, OverflowError) as error:
        return SessionLivenessUnknown(f"process identity unreadable: {error}")
    if current is None:
        return SessionLivenessUnknown(f"process identity unreadable for pid {pid}")
    return SessionLive()


def _session_liveness(
    session_id: str,
) -> SessionLive | SessionNotLive | SessionLivenessUnknown:
    try:
        paths = [path for path in sessions.sessions_dir().iterdir() if path.suffix == ".json"]
    except OSError as error:
        return SessionLivenessUnknown(f"session registry unreadable: {_error_reason(error)}")
    unreadable_records: list[str] = []
    for path in paths:
        registered = sessions.read_session(path)
        if isinstance(registered, sessions.UnreadableSessionRecord):
            unreadable_records.append(path.name)
            continue
        if registered["sessionId"] != session_id:
            continue
        if sessions.running_session(registered):
            return SessionLive()
        observed = _process_liveness(registered["pid"], registered.get("procStart", ""))
        if not isinstance(observed, SessionNotLive):
            return observed
    if unreadable_records:
        return SessionLivenessUnknown("unreadable session registry record: " + unreadable_records[0])
    return SessionNotLive()


def _owner_liveness(owner: Session) -> SessionLive | SessionNotLive | SessionLivenessUnknown:
    """Registry absence is insufficient while the saved process identity still runs."""
    observed = _session_liveness(owner["session_id"])
    if not isinstance(observed, SessionNotLive):
        return observed
    return _process_liveness(owner["pid"], owner["proc_start"])


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


def remote_control_missing(records: list[SessionShutdown], confirmation: RegistryConfirmation) -> None:
    """Launch quiet recovery for each confirmed local session still missing it."""
    active = wait_for_remote_control(confirmation.records, REMOTE_CONTROL_WAIT_SECONDS)
    live_by_id = {item["sessionId"]: item for item in confirmation.records}
    for saved in records:
        session = saved["session"]
        identity = session["session_id"]
        if identity not in confirmation.live_ids - active:
            continue
        live = live_by_id[identity]
        try:
            remote_control.launch_fixer(identity, live["pid"], live.get("procStart", ""),
                                        remote_control.remote_control_who(saved), session["name"])
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            syslog.syslog(syslog.LOG_WARNING, f"{session['name']}: could not start Remote Control fixer: {error}")


def _user_time(value: str) -> str:
    return datetime.fromisoformat(value).astimezone(user_zone()).strftime(
        "%Y-%m-%d %H:%M %Z"
    )


def restart_note(
    record: SessionShutdown, restarted_at: str
) -> str:
    """Build the exact first prompt given to a resumed session."""
    note = _stop_note(record, restarted_at)
    # A unit renamed while it was stopped learns its new names before anything else it reads.
    return f"{record['renamed']} {note}" if "renamed" in record else note


def _stop_note(record: SessionShutdown, restarted_at: str) -> str:
    if "target" in record and record["requested_by"]["kind"] != "snapshot":
        note = account_move_note(record["session"], record["target"],
                                 record.get("source_label", record["label"]), record.get("why", ""),
                                 codex_seats=record.get("codex_seats", []),
                                 times=MoveNoteTimes(record["requested_at"], restarted_at))
        where = record["where"]
        if where["kind"] == "said":
            punctuation = "" if where["text"].endswith((".", "!", "?")) else "."
            note += f" Before it you wrote: {where['text']}{punctuation}"
        return note
    origin = record["requested_by"]
    if origin["kind"] == "snapshot":
        context = origin["recovery"]
        if context["kind"] == "not recorded":
            opening = (
                "Restored after a crash (crash evidence and last-seen time were not recorded; "
                f"restored {_user_time(restarted_at)}). "
            )
        else:
            signal = "the machine rebooted" if context["kind"] == "reboot" else "its host was lost"
            opening = (
                f"Restored after a crash: {signal} "
                f"(last seen {_user_time(context['last_seen'])}, "
                f"restored {_user_time(restarted_at)}). "
            )
    else:
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
class ResumeConversation:
    """The launch account has a transcript for this session."""


@dataclass(frozen=True)
class StartFreshConversation:
    """Keep the session identity when its launch account has no transcript."""


def _conversation_launch(session_id: str, config_dir: Path) -> ResumeConversation | StartFreshConversation:
    if any(path.is_file() for path in (config_dir / "projects").glob("*/" + session_id + ".jsonl")):
        return ResumeConversation()
    return StartFreshConversation()


@dataclass(frozen=True)
class ResumeParts:
    """A conversation launch before any host-specific command rendering."""

    cwd: str
    environment: dict[str, str]
    claude_arguments: tuple[str, ...]
    opening_prompt: str
    account: RetainResumeAccount | MoveResumeAccount
    conversation: ResumeConversation | StartFreshConversation


@dataclass(frozen=True)
class NewHerdrSession:
    name: str


@dataclass(frozen=True)
class RecordedHerdrPane:
    name: str
    pane_id: str


@dataclass(frozen=True)
class SharedHerdrTab:
    """A new tab in the shared session, in the workspace named for the session's production."""
    name: str


HerdrDestination = NewHerdrSession | RecordedHerdrPane | SharedHerdrTab


def herdr_space(session: Session) -> str:
    """The shared session's workspace for a Claude session: its production, else `sessions`."""
    if session["kind"] == "showrunner":
        from showrunners import production_slug
        return production_slug(session["doc"])
    return "sessions"


class HerdrDestinationTaken(OSError):
    """An exclusive launch found an occupied destination, before opening a pane."""


def _resume_parts(session: Session, prompt: str, target: str = "", *, login: str = "", at: str = "", probe: bool = False) -> ResumeParts:
    from scripts.accounts import accounts

    config_dir = Path(session.get("config_dir", str(accounts.main_config_dir()))).expanduser()
    if not target and config_dir == accounts.main_config_dir() and session["kind"] not in ("unit", "showrunner"):
        resolved = accounts.resolve_history_label("~/.claude", at, login=login)
        if not isinstance(resolved, accounts.ResolvedHistoryLabel):
            raise accounts.AccountsError("home account unresolved in shutdown record")
        config_dir = accounts.claude_account_dir(resolved.label)
    if session["kind"] in ("unit", "showrunner"):
        role = "unit" if session["kind"] == "unit" else "showrunner"
        selected = accounts.account_for_group_launch(role, resume=True, target=target)
        target = selected.label
    if not target:
        number = re.fullmatch(r"\.claude([1-9][0-9]*)", config_dir.name)
        if number is None or config_dir != accounts.account_dir("claude" + number.group(1)):
            raise accounts.AccountsError("shutdown record must name a numbered Claude account folder")
    account: RetainResumeAccount | MoveResumeAccount = (
        MoveResumeAccount(target) if target else RetainResumeAccount(config_dir)
    )
    environment = named_account_environment(config_dir) if not target else {}
    environment["ENABLE_TOOL_SEARCH"] = "true"
    if probe or session.get("probe", False):
        environment[PROBE_ENV] = "1"
    launch_config_dir = accounts.account_dir(target) if target else config_dir
    conversation = _conversation_launch(session["session_id"], launch_config_dir)
    argv = [
        "--resume" if isinstance(conversation, ResumeConversation) else "--session-id",
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
    return ResumeParts(session["cwd"], environment, tuple(argv), prompt, account, conversation)


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


def _resume_command(session: Session, prompt: str, target: str = "", *, login: str = "", at: str = "", probe: bool = False) -> str:
    return _resume_shell(_resume_parts(session, prompt, target, login=login, at=at, probe=probe))


def account_move_note(session: Session, target: str, source_label: str, why: str = "", *,
                      codex_seats: Sequence[PausedSeat] = (),
                      times: MoveNoteTimes | MoveNoteTimesUnrecorded = _MOVE_NOTE_TIMES_UNRECORDED) -> str:
    """One stable explanation for every moved conversation and host."""
    reason = why or f"{source_label} is out of usage"
    continuation = (" Continue your /unit:direct run." if session["kind"] == "unit" else "")
    workers = (" Your Codex workers are paused, not ended; a message lists the command that resumes each."
               if codex_seats else "")
    timing = (f"Paused {_user_time(times.paused_at)}; resumed {_user_time(times.resumed_at)}. "
              if isinstance(times, MoveNoteTimes) else "Pause and resume times are not yet recorded. ")
    return (f"You were moved from {source_label} to {target} because {reason}. " + timing
            + "This was an account move, not a stop. Continue where you stopped."
            + continuation + workers + " First run `~/.claude/scripts/lib/py "
            + "~/.claude/scripts/message/send.py pending` for messages kept while you "
            + "were down; if you were waiting on the user, keep waiting.")


def resume_command(session: Session, target: str, source_label: str = "", why: str = "", *,
                   times: MoveNoteTimes | MoveNoteTimesUnrecorded = _MOVE_NOTE_TIMES_UNRECORDED) -> str:
    """The stable manual move command printed before shutdown and at restart."""
    if not source_label:
        directory = Path(session.get("config_dir", str(Path.home() / ".claude")))
        from account import claude_account
        identity = claude_account(directory)
        source_label = identity.label if identity is not None else str(directory)
    note = account_move_note(session, target, source_label, why, times=times)
    prompt = f"{_showrunner_prompt(session)} {note}" if session["kind"] == "showrunner" else note
    return _resume_command(session, prompt, target)


def legacy_resume_command(record: SessionShutdown) -> str:
    """Keep obsolete host names out of destinations and preserve unresolved cleanup."""
    session = record["session"]
    prompt = _showrunner_prompt(session) if session["kind"] == "showrunner" else "Continue where you stopped."
    resume = _resume_command(session, prompt, record.get("target", ""), login=record["login"],
        at=record["requested_at"], probe=record.get("probe", False))
    cleanup = [shlex.join(["tmux", "kill-session", "-t", "=" + issue["session_name"]])
               for issue in recovery_store.legacy_tmux_leftovers(record)]
    return " && ".join([*cleanup, resume])


def legacy_codex_command(entry: SnapshotCodexSession) -> str:
    launch = entry["codex"]
    argv = ["codex", "resume"]
    if launch["resume"]["kind"] == "session id":
        argv.append(launch["resume"]["session_id"])
    return (f"cd {shlex.quote(entry['session']['cwd'])} && "
            f"CODEX_HOME={shlex.quote(launch['home'])} {shlex.join(argv)}")


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


def _prompt_outcome_path(record: SessionShutdown) -> Path:
    return _note_path(record, record["session"]["session_id"]).with_suffix(".prompt-outcome.json")


def _save_prompt_outcome(record: SessionShutdown, outcome: herdr.PromptSubmitOutcome) -> None:
    progress = record["progress"]
    attempt = progress["at"] if progress["kind"] == "restarting" else record["requested_at"]
    _write_note(_prompt_outcome_path(record), json.dumps({
        "request_id": record["request_id"], "session_id": record["session"]["session_id"],
        "attempt": attempt, "target": record.get("target", record["session"].get("config_dir", "")),
        "outcome": herdr.encode_prompt_outcome(outcome),
    }), False)


def _prompt_outcome(record: SessionShutdown) -> herdr.PromptSubmitOutcome:
    try:
        value = cast(object, json.loads(_prompt_outcome_path(record).read_text()))
        if not isinstance(value, dict):
            return herdr.PromptNotSubmitted("no launch submit proof")
        row = cast(dict[str, object], value)
        progress = record["progress"]
        if (row.get("request_id") != record["request_id"]
                or row.get("session_id") != record["session"]["session_id"]
                or row.get("target") != record.get("target", record["session"].get("config_dir", ""))
                or progress["kind"] == "restarting" and row.get("attempt") != progress["at"]):
            return herdr.PromptNotSubmitted("submit proof belongs to another relaunch attempt")
        encoded = row.get("outcome")
        outcome = herdr.decode_prompt_outcome(encoded) if isinstance(encoded, str) else herdr.PromptNotSubmitted("no launch submit proof")
        if isinstance(outcome, herdr.PromptSubmitted):
            process = herdr.prompt_recipient_process(record["session"]["session_id"])
            # A progress transition may change the claim time without launching
            # a process. The process identity survives those transitions; a
            # receipt without it cannot prove any later process's prompt.
            if (not isinstance(process, herdr.PromptRecipientProcess)
                    or outcome.process != process):
                return herdr.PromptNotSubmitted("submit proof belongs to another launch process")
        return outcome
    except (OSError, UnicodeError, ValueError):
        return herdr.PromptNotSubmitted("no launch submit proof")


def _require_submitted_prompt(record: SessionShutdown, outcome: herdr.PromptSubmitOutcome | LaunchPromptProofNotRead = _LAUNCH_PROMPT_PROOF_NOT_READ) -> bool:
    if isinstance(outcome, LaunchPromptProofNotRead):
        outcome = _prompt_outcome(record)
    if isinstance(outcome, herdr.PromptSubmitted):
        return True
    mark_restart_failed(record, "restart prompt not submitted")
    return False


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
        table = production_units.unit_table(Path(doc).read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeError, production_units.Refusal):
        return None
    rows = [row for row in table if production_units.cell_value(row.get("Unit", "")) == unit]
    if len(rows) != 1 or "Plan" not in rows[0]:
        return None
    return production_units.cell_value(rows[0]["Plan"])


def _unit_argv(record: SessionShutdown) -> list[str]:
    session = record["session"]
    host = session["host"]
    if session["kind"] != "unit" or host["kind"] != "unit":
        raise ValueError("unit launch needs a unit host")
    recorded_plan = host["plan"]
    # The Units row first: a brief unit's row names its stub plan before any run record exists.
    row_plan = _units_row_plan(host["doc"], host["unit"])
    plan_arguments: list[str]
    if row_plan in ("standby", "permanent"):
        plan_arguments = ["--" + row_plan]
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
    from scripts.accounts import accounts

    selected = accounts.account_for_group_launch("unit", resume=True, target=record.get("target", ""))
    argv.extend(("--account", selected.label))
    if record["probe"]:
        argv.append("--probe")
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


def _open_window(command: str, desktop: Desktop, desktop_state: DesktopLaunchState, *, dry_run: bool) -> None:
    """Open one Ghostty window, selecting a recorded desktop when available."""
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


def _herdr_launch(
    parts: ResumeParts, destination: HerdrDestination, session: Session, *, dry_run: bool,
    session_kind: herdr.SecuritySession | herdr.SessionKindNotRead = _SESSION_KIND_NOT_READ,
) -> HerdrLaunch:
    """Resume Claude with its own arguments and one separate opening prompt."""
    unset_variables: set[str] = {"CLAUDE_CODE_OAUTH_TOKEN"}
    if isinstance(parts.account, MoveResumeAccount):
        from scripts.accounts import accounts

        prepared = accounts.prepared_account(parts.account.target, resume=True)
        if isinstance(prepared, accounts.AccountRefused):
            raise OSError(prepared.reason)
        unset_variables.update(prepared.remove)
        environment = {**parts.environment, **prepared.variables}
        for key in prepared.remove:
            _ = environment.pop(key, None)
    else:
        environment = dict(parts.environment)
        environment = {**named_account_environment(parts.account.config_dir), **environment}
    name = destination.name
    if dry_run:
        location = (destination.pane_id if isinstance(destination, RecordedHerdrPane)
                    else f"a new tab in {herdr_space(session)}" if isinstance(destination, SharedHerdrTab)
                    else "a new pane")
        print(f"herdr {name}: resume {session['session_id']} in {location}")
        return HerdrLaunch(herdr.ServerAdopted(), herdr.PromptNotSubmitted("dry run"))
    try:
        from scripts.accounts.accounts import NamedAccount, trust_command

        config_dir = Path(environment["CLAUDE_CONFIG_DIR"])
        claude_json = config_dir / ".claude.json"
        trust = herdr.claude_trust(Path(parts.cwd), claude_json)
        if isinstance(trust, (herdr.Untrusted, herdr.UnreadableTrustFile)):
            account = NamedAccount(config_dir.name.removeprefix("."), config_dir)
            reason = (str(trust) if isinstance(trust, herdr.UnreadableTrustFile)
                      else f"{parts.cwd}: Claude has not trusted this folder")
            raise OSError(f"{reason}. Run: {trust_command(Path(parts.cwd), account)}")
        server = herdr.restore_server(name, 90_000,
            session_kind=herdr.security_session() if isinstance(session_kind, herdr.SessionKindNotRead) else session_kind,
            exclusive=isinstance(destination, NewHerdrSession))
        server_environment = herdr.server_environment(name)
        for variable in sorted(unset_variables):
            if variable in server_environment:
                raise OSError(
                    f"herdr server {name} carries {variable} from where it started; "
                    + f"stop it with herdr --session {name} server stop and restart again"
                )
        client = herdr.Herdr(name)
        agents = client.agents()
        if any((isinstance(destination, NewHerdrSession)
                or (isinstance(destination, RecordedHerdrPane) and row["pane_id"] == destination.pane_id))
               and row.get("agent")
               and row["agent_status"] not in {"exited", "stopped", "dead"} for row in agents):
            location = (f"{name}:{destination.pane_id}" if isinstance(destination, RecordedHerdrPane)
                        else name)
            raise OSError(f"herdr {location}: pane still holds a live agent")
        if isinstance(destination, RecordedHerdrPane) and any(
            row["pane_id"] == destination.pane_id for row in client.panes()
        ):
            activity = client.pane_activity(destination.pane_id)
            if isinstance(activity, herdr.PaneAtShell):
                client.close_pane(destination.pane_id)
            else:
                raise OSError(f"herdr {name}:{destination.pane_id}: pane runs {', '.join(activity.commands)}")
        _record_scheduled_restart_prompt(session["session_id"], parts.opening_prompt, dry_run=False)
        if name == herdr.shared_session():
            pane = herdr.open_tab(name, herdr_space(session), session["name"], parts.cwd, environment)
        else:
            pane = herdr.open_pane(name, name, parts.cwd, environment)
        try:
            outcome = client.start_agent(session["name"], pane, kind="claude", agent_arguments=list(parts.claude_arguments),
                opening=herdr.OpeningPrompt(parts.opening_prompt,
                    herdr.SessionTranscript(config_dir, session["session_id"])), timeout_ms=90_000)
        except herdr.AgentExited:
            if name == herdr.shared_session():
                herdr.close_agent(name, pane)
            else:
                herdr.stop_created_server(name, server)
            raise
        if isinstance(server, (herdr.ServerAdopted, herdr.WindowServerCreated)):
            focused = _focus_restored_pane(client, pane, session["name"])
            if focused and isinstance(server, herdr.WindowServerCreated):
                _close_startup_workspaces(client, pane)
        if isinstance(parts.conversation, StartFreshConversation):
            print(f"{session['name']}: no conversation to resume; started a new one")
        return HerdrLaunch(server, outcome if isinstance(outcome, (herdr.PromptSubmitted, herdr.PromptNotSubmitted))
                           else herdr.PromptNotSubmitted("launcher returned no submit proof"))
    except herdr.MacDesktopUnavailable:
        raise
    except herdr.SessionTaken as error:
        raise HerdrDestinationTaken(str(error)) from error
    except herdr.HerdrFailure as error:
        raise OSError(str(error)) from error


def _leave_recorded_session(name: str, recorded: str, pane: str) -> None:
    """Close the pane a session left behind when it moved into the shared session; stop the server it emptied.

    A server whose other panes are idle shells, such as the one its attach window opened, counts as emptied.
    """
    client = herdr.Herdr(recorded)
    try:
        rows = client.panes()
        left = [row for row in rows if row["pane_id"] == pane]
        if left and not isinstance(client.pane_activity(pane), herdr.PaneAtShell):
            print(f"{name} restart issue: herdr {recorded}:{pane} still runs a program; left open")
            return
        if all(isinstance(client.pane_activity(row["pane_id"]), herdr.PaneAtShell) for row in rows):
            herdr.stop_session(recorded)
        elif left:
            client.close_pane(pane)
    except herdr.HerdrFailure as error:
        print(f"{name} restart issue: herdr session {recorded} left open: {error}")


def _focus_restored_pane(client: herdr.Herdr, pane: str, name: str) -> bool:
    try:
        client.focus_workspace(pane.split(":", 1)[0])
        return True
    except herdr.HerdrFailure as error:
        print(f"{name} restart issue: restored workspace not focused: {error}")
        return False


def _close_startup_workspaces(client: herdr.Herdr, pane: str) -> None:
    try:
        client.close_startup_workspaces(pane.split(":", 1)[0])
    except herdr.HerdrFailure as error:
        print(f"{client.session} restart issue: startup workspace left open: {error}")


def launches_window(host: Host) -> bool:
    """Window hosts and attached herdr hosts share serialized presentation."""
    return host["kind"] in {"ghostty", "zed", "terminal"} or (
        host["kind"] == "herdr" and herdr_presentation(host) == "attached window")


def _herdr_attach_command(name: str, cwd: str) -> str:
    argv = [str(SCRIPTS / "lib" / "py"), str(SCRIPTS / "lib" / "relaunch.py"),
            "run", "--", "herdr", "session", "attach", name]
    return f"cd {shlex.quote(cwd)} && {shlex.join(argv)}; exec zsh"


@dataclass(frozen=True)
class CodexWindowRestore:
    """A restored Codex session has no Claude shutdown record for window issues."""

    machine: str
    session_name: str


def _herdr_window(
    record: SessionShutdown | CodexWindowRestore, name: str, cwd: str, desktop: Desktop,
    desktop_state: DesktopLaunchState, *, dry_run: bool,
) -> None:
    """Report a failed attachment without turning a launched agent into another launch."""
    try:
        if sys.platform == "darwin":
            if not dry_run and desktop_state.started_windows:
                time.sleep(WINDOW_DELAY_SECONDS)
            _ = _run([
                os.environ.get("SHUTDOWN_OPEN", "open"), "-na",
                "/Applications/Nix Apps/Ghostty.app", "--args", "-e",
                herdr.herdr_binary(), "session", "attach", name,
            ], dry_run=dry_run)
            desktop_state.started_windows += 1
        else:
            _open_window(_herdr_attach_command(name, cwd), desktop, desktop_state, dry_run=dry_run)
    except (OSError, subprocess.SubprocessError) as error:
        attach = shlex.join(["herdr", "session", "attach", name])
        issue = f"window not opened: {_error_reason(error)}; {attach}"
        if isinstance(record, CodexWindowRestore):
            print(f"{record.machine}: {record.session_name}: {issue}")
            return
        print(f"{record['machine']}: {record['session']['name']} restart issue: {issue}")
        if not dry_run:
            def remember(current: SessionShutdown) -> None:
                if _same_relaunch_claim(record, current):
                    current.setdefault("restart_issues", []).append(issue)
            _ = update_session(record["login"], record["session"]["session_id"], remember)


def _launch_session(
    record: SessionShutdown,
    note: str,
    desktop_state: DesktopLaunchState,
    *,
    dry_run: bool,
) -> SessionLaunched | ManualRestartRequired:
    """Launch an entry or return its explicit manual-restart requirement."""
    from scripts.accounts import accounts

    if "target" in record:
        resolved = accounts.resolve_history_label(record["target"], record["requested_at"])
        if not isinstance(resolved, accounts.ResolvedHistoryLabel):
            raise accounts.AccountsError("home account unresolved in shutdown record")
        record = {**record, "target": resolved.label}
    session = record["session"]
    host = session["host"]
    if host["kind"] in {"legacy tmux", "legacy tmux unit"}:
        return ManualRestartRequired(legacy_resume_command(record))
    if session["kind"] == "unit":
        if host["kind"] != "unit":
            raise ValueError("unit session has no unit host")
        argv = _unit_argv(record)
        note_path = _note_path(record, session["session_id"])
        _write_note(note_path, note, dry_run)
        try:
            result = _run(argv, dry_run=dry_run, check=False)
            encoded = next((line.removeprefix("restart-prompt-outcome: ") for line in
                            (result.stdout.splitlines() if result is not None else [])
                            if line.startswith("restart-prompt-outcome: ")), "")
            outcome = herdr.decode_prompt_outcome(encoded)
            if not encoded and result is not None and result.returncode:
                raise subprocess.CalledProcessError(result.returncode, argv, result.stdout, result.stderr)
        finally:
            if not dry_run:
                note_path.unlink(missing_ok=True)
        return SessionLaunched(outcome)

    prompt = _showrunner_prompt(session) if session["kind"] == "showrunner" else note
    if session["kind"] == "showrunner" and ("target" in record or record["requested_by"]["kind"] == "snapshot"):
        prompt += " " + note
    parts = _resume_parts(session, prompt, record.get("target", ""), login=record["login"],
                          at=record["requested_at"], probe=record.get("probe", False))
    if host["kind"] == "herdr":
        shared = herdr.shared_session()
        moving = bool(shared) and host["herdr_session"] != shared
        destination = SharedHerdrTab(shared) if moving else RecordedHerdrPane(host["herdr_session"], host["pane_id"])
        launch = _herdr_launch(parts, destination, session, dry_run=dry_run, session_kind=desktop_state.session_kind)
        if moving and not dry_run:
            _leave_recorded_session(session["name"], host["herdr_session"], host["pane_id"])
        name = destination.name
        if isinstance(launch.server, herdr.WindowServerCreated):
            return SessionLaunched(launch.prompt_outcome)
        if herdr_presentation(host) == "attached window":
            match herdr.attached_clients(name):
                case herdr.NoClientAttached():
                    _herdr_window(record, name, parts.cwd, DesktopNotInSnapshot(kind="not in snapshot"),
                                  desktop_state, dry_run=dry_run)
                case herdr.ClientsUnreadable():
                    print(shlex.join(["herdr", "session", "attach", name]))
                case herdr.ClientsAttached():
                    pass
        else:
            print(shlex.join(["herdr", "session", "attach", name]))
        return SessionLaunched(launch.prompt_outcome)
    if host["kind"] == "ghostty" or host["kind"] == "zed" or (host["kind"] == "terminal" and sys.platform == "darwin"):
        shared = herdr.shared_session()
        name = shared or herdr.generated_session_name(session["name"])
        launch = _herdr_launch(parts, SharedHerdrTab(name) if shared else NewHerdrSession(name), session,
                    dry_run=dry_run, session_kind=desktop_state.session_kind)
        if isinstance(launch.server, herdr.WindowServerCreated):
            return SessionLaunched(launch.prompt_outcome)
        if host["kind"] == "ghostty" or host["kind"] == "terminal":
            supervisor = live_supervisor(host)
            if supervisor["kind"] == "supervisor":
                request = relaunch.RelaunchRequest(
                    argv=[herdr.herdr_binary() if sys.platform == "darwin" else "herdr",
                          "session", "attach", name], cwd=parts.cwd,
                    environment={}, unset=[],
                    written_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), note=note)
                if dry_run:
                    print(f"restart in window via supervisor {supervisor['pid']}: {shlex.join(request['argv'])}")
                else:
                    relaunch.write_request(supervisor["pid"], request)
                return SessionLaunched(launch.prompt_outcome)
        if shared and not dry_run and isinstance(herdr.attached_clients(name), herdr.ClientsAttached):
            return SessionLaunched(launch.prompt_outcome)
        desktop = (DesktopNotInSnapshot(kind="not in snapshot") if host["kind"] == "terminal"
                   else host["desktop"])
        _herdr_window(record, name, parts.cwd, desktop, desktop_state, dry_run=dry_run)
        return SessionLaunched(launch.prompt_outcome)
    if host["kind"] == "unit":
        raise ValueError("non-unit session has a unit host")
    return ManualRestartRequired(_resume_shell(parts))


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
        subject=session["session_id"] + ": " + session["name"], move_id=record.get("move_id", record["request_id"]), request_id=record["request_id"], reason=reason))


@dataclass(frozen=True)
class RecordedConfigDir:
    path: Path


@dataclass(frozen=True)
class NoRecordedConfigDir:
    """Neither a move target nor the original directory was recorded."""


@dataclass(frozen=True)
class ObservedConfigDir:
    path: Path


@dataclass(frozen=True)
class LiveObservationUnavailable:
    reason: Literal["not live", "environment unreadable"]


def _target_config_dir(record: SessionShutdown) -> RecordedConfigDir | NoRecordedConfigDir:
    from scripts.accounts import accounts

    target_label = record.get("target", "")
    if not target_label:
        session = record["session"]
        directory = Path(session.get("config_dir", str(accounts.main_config_dir()))).expanduser()
        if directory != accounts.main_config_dir():
            return RecordedConfigDir(directory)
        target_label = "~/.claude"
    resolved = accounts.resolve_history_label(target_label, record["requested_at"],
                                             login=record["login"] if not record.get("target") else "")
    if not isinstance(resolved, accounts.ResolvedHistoryLabel):
        return NoRecordedConfigDir()
    target_label = resolved.label
    return RecordedConfigDir(accounts.claude_account_dir(target_label))


def _live_config_dir(record: SessionShutdown) -> ObservedConfigDir | LiveObservationUnavailable:
    import account
    live = next((item for item in sessions.live_sessions() if item["sessionId"] == record["session"]["session_id"]), None)
    if live is None:
        return LiveObservationUnavailable("not live")
    setting = account.process_account_directory(account.process_config_dir(live["pid"]))
    match setting:
        case account.ConfiguredClaudeFolder(path):
            return ObservedConfigDir(path)
        case account.NoAccountSetting() | account.ProcessEnvironmentUnread():
            return LiveObservationUnavailable("environment unreadable")


def _confirm_move(record: SessionShutdown, outcome: herdr.PromptSubmitOutcome | LaunchPromptProofNotRead = _LAUNCH_PROMPT_PROOF_NOT_READ) -> None:
    from scripts.accounts import moves
    from scripts.accounts.refusal_watch import clear_assumed
    if not _require_submitted_prompt(record, outcome):
        return
    session = record["session"]
    target = _target_config_dir(record)
    if isinstance(target, NoRecordedConfigDir):
        return
    moves.append_move(moves.ConfirmedMove(kind="confirmed", at=settle.record_time(), tool="claude",
        subject=session["session_id"] + ": " + session["name"], move_id=record.get("move_id", record["request_id"]), request_id=record["request_id"],
        config_dir=str(target.path)))
    clear_assumed(session["session_id"])


def _prove_move(record: SessionShutdown, outcome: herdr.PromptSubmitOutcome | LaunchPromptProofNotRead = _LAUNCH_PROMPT_PROOF_NOT_READ) -> bool:
    """A registry entry alone does not prove that the session adopted the target."""
    if not _require_submitted_prompt(record, outcome):
        return False
    if not record.get("target", ""):
        return True
    directory, target = _live_config_dir(record), _target_config_dir(record)
    if isinstance(directory, LiveObservationUnavailable) or isinstance(target, NoRecordedConfigDir):
        mark_restart_failed(record, "resumed account unreadable")
        return False
    if directory.path.resolve() != target.path.resolve():
        reason = "still on " + record.get("source_label", record["label"])
        mark_restart_failed(record, reason)
        return False
    _confirm_move(record, outcome)
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
        if (recovery_store.legacy_tmux_leftovers(record)
                or record["progress"]["kind"] not in NOT_BACK_PROGRESS
                or record["machine"].casefold() != settle.local_machine().casefold()):
            remaining.append(record)
            continue
        directory, target = _live_config_dir(record), _target_config_dir(record)
        if (isinstance(directory, LiveObservationUnavailable) or isinstance(target, NoRecordedConfigDir)
                or directory.path.resolve() != target.path.resolve()):
            remaining.append(record)
            continue
        if not _require_submitted_prompt(record):
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
            return _require_submitted_prompt(record)
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


def _launch_recovery_record(
    saved: SessionShutdown, note: str, desktop_state: DesktopLaunchState, *, dry_run: bool,
) -> SessionLaunched | ManualRestartRequired:
    # Units perform the same admission in add_unit, in its own process/barrier.
    if dry_run or saved["requested_by"]["kind"] != "snapshot" or saved["session"]["kind"] == "unit":
        return _launch_session(saved, note, desktop_state, dry_run=dry_run)
    with restore_permission.launch_barrier():
        found = lookup_session(saved["login"], saved["session"]["session_id"])
        if not isinstance(found, ShutdownFound) or not _same_relaunch_claim(saved, found.record):
            raise OSError("recovery record is absent or its restart claim changed")
        parts = _resume_parts(saved["session"], note, saved.get("target", ""), login=saved["login"],
                              at=saved["requested_at"], probe=saved.get("probe", False))
        launch_record: SessionShutdown = saved
        environment = {key: value for key, value in os.environ.items()
                       if key not in {*NAMED_ACCOUNT_VARIABLES, "CLAUDE_CODE_OAUTH_TOKEN"}}
        if isinstance(parts.account, MoveResumeAccount):
            from scripts.accounts import accounts

            prepared = accounts.prepared_account(parts.account.target, resume=True)
            if isinstance(prepared, accounts.AccountRefused):
                raise OSError(prepared.reason)
            environment.update(prepared.variables)
            for key in prepared.remove:
                _ = environment.pop(key, None)
            # Pin the selected group account; a concurrent group change must refuse.
            launch_record = {**saved, "target": parts.account.target}
        else:
            environment.update(parts.environment)
        permission = restore_permission.launch_permission(environment,
            restore_permission.RecordedSessionRestoreLaunchPurpose(saved["session"]["session_id"]))
        if isinstance(permission, restore_permission.BlockedByShutdown):
            raise OSError(permission.reason)
        if isinstance(permission, restore_permission.ShutdownStateUnreadable):
            raise OSError("shutdown state unreadable: " + permission.detail)
        return _launch_session(launch_record, note, desktop_state, dry_run=False)


def launch_entry(
    record: SessionShutdown,
    restarted_at: str,
    desktop_state: DesktopLaunchState,
    *,
    dry_run: bool,
) -> EntryLaunched | EntrySettled:
    from scripts.accounts import accounts

    progress = _prior_progress(record)
    if progress["kind"] in COMPLETE_PROGRESS:
        return EntrySettled()
    session = record["session"]
    session_id = session["session_id"]
    if session["host"]["kind"] in {"legacy tmux", "legacy tmux unit"} or recovery_store.legacy_tmux_leftovers(record):
        command = legacy_resume_command(record)
        print(f"manual restart {session['name']}: {command}")
        if not dry_run:
            _ = _set_entry_progress(record, SessionNeedsManualRestart(kind="manual restart", command=command))
        return EntrySettled()
    relaunching_pending_timers = False
    if progress["kind"] == "timers pending":
        relaunching_pending_timers = True
        match _session_liveness(session_id):
            case SessionLive():
                if not dry_run:
                    # This claim owns timer restoration, not a new process launch.
                    # Read the existing launch's receipt against its pending state;
                    # keep the fresh claim on record for guarded progress writes.
                    outcome = _prompt_outcome({**record, "progress": progress})
                    if not _prove_move(record, outcome):
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
    if progress["kind"] == "restart failed" or (record["requested_by"]["kind"] == "snapshot" and progress["kind"] == "already gone"):
        observed = _owner_liveness(session)
        if isinstance(observed, SessionLive):
            print(f"{session['name']}: already running; skipped")
            if not dry_run:
                _restore_prior(record)
            return EntrySettled()
        if isinstance(observed, SessionLivenessUnknown):
            print(f"{session['name']}: recovery refused: {observed.reason}")
            if not dry_run:
                mark_restart_failed(record, observed.reason)
            return EntrySettled()
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

    if not dry_run:
        _save_prompt_outcome(record, herdr.PromptNotSubmitted("launch has no submit proof"))
    try:
        launch = _launch_recovery_record(
            record, note, desktop_state, dry_run=dry_run
        )
    except herdr.MacDesktopUnavailable as error:
        if dry_run:
            print(str(error))
        else:
            mark_restart_failed(record, str(error))
        return EntrySettled()
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
    except accounts.AccountsError as error:
        if dry_run:
            print(f"{session['name']} restart failed: {_error_reason(error)}")
        else:
            mark_restart_failed(record, _error_reason(error))
        return EntrySettled()
    except (OSError, subprocess.CalledProcessError, subprocess.SubprocessError) as error:
        if dry_run or not _launch_failure_left_session_live(record, error):
            return EntrySettled()
        launch = SessionLaunched(_prompt_outcome(record))
    if isinstance(launch, ManualRestartRequired):
        print(f"manual restart {session['name']}: {launch.command}")
        if not dry_run:
            _ = _set_entry_progress(record, SessionNeedsManualRestart(
                kind="manual restart", command=launch.command,
            ))
        return EntrySettled()
    if dry_run:
        return EntrySettled()
    outcome = launch.prompt_outcome
    if not isinstance(outcome, (herdr.PromptSubmitted, herdr.PromptNotSubmitted)):
        outcome = herdr.PromptNotSubmitted("restart launcher supplied no submit proof")
    _save_prompt_outcome(record, outcome)
    if isinstance(outcome, herdr.PromptNotSubmitted):
        mark_restart_failed(record, "restart prompt not submitted")
        return EntrySettled()
    return EntryLaunched(relaunching_pending_timers)


def _resume_worker_command(seat: PausedSeat, cwd: str) -> str:
    """Build a complete launcher invocation that continues this seat's thread."""
    launcher = "review.sh" if seat["role"] == "review" else "implement.sh"
    prompt = str(Path(seat["run_dir"]) / f"{seat['seat']}.resume-prompt.md")
    resumed = _resumed_launcher_command(seat["launcher_command"], seat["seat"], prompt) if seat["turn_live"] else None
    if resumed is not None:
        return resumed
    start = [str(SCRIPTS / "delegate" / launcher), "--to", seat["seat"], seat["run_dir"], cwd, prompt]
    if launcher == "review.sh":
        return shlex.join([*start, "review", "resume after a move", "resuming after a move"])
    from scripts.delegate.worker_state import worker_slot

    # The roster role is the last pass kind; implement.sh claims the worker by the slot its identity names.
    team_role = worker_slot(Path(seat["run_dir"]), seat["seat"]) or seat["role"]
    kind = team_role if team_role in {"impl", "test", "fix"} else "impl"
    return shlex.join([*start, kind, "resume after a move", kind, "resuming after a move", "0", team_role])


def _resumed_launcher_command(command: list[str], seat: str, prompt: str) -> str | None:
    """Keep the recorded task arguments, following the seat with its resume prompt; None when incomplete."""
    index = next((i for i, arg in enumerate(command) if Path(arg).name in {"implement.sh", "review.sh"}), -1)
    if index < 0:
        return None
    # Launchers accept option/value pairs followed by session_dir, cwd, prompt_file.
    position = index + 1
    options: list[str] = []
    while position < len(command) and command[position].startswith("--"):
        if position + 1 >= len(command):
            return None
        option, value = command[position], command[position + 1]
        if option not in {"--slot", "--pool", "--lease", "--to"}:
            options.extend((option, value))
        position += 2
    arguments = command[position:]
    # implement.sh requires its team role as the ninth argument; review.sh has defaults after three.
    if len(arguments) < (9 if Path(command[index]).name == "implement.sh" else 3):
        return None
    arguments[2] = prompt
    return shlex.join([*command[:index + 1], *options, "--to", seat, *arguments])


def _send_worker_resume(record: SessionShutdown) -> None:
    seats = record.get("codex_seats", [])
    if not seats:
        return
    lines = ["Resume your Codex workers; do not open new ones."]
    cwd = record["session"]["cwd"]
    for seat in seats:
        state = "was working" if seat["turn_live"] else "idle"
        lines.append(f"{seat['seat']} ({seat['role']}, {state}): {_resume_worker_command(seat, cwd)}")
    lines.append("Each seat continues its own thread; after resuming, send later work with --to as usual.")
    identity = record["session"]["session_id"]
    try:
        result = _run([*_send_command(), "--to", f"session:{identity}", "--from", "shutdown",
                       "--key", f"resume-workers-{identity}", "--text", "\n".join(lines)],
                      dry_run=False, check=False)
        if result is not None and result.returncode != 0:
            print(f"shutdown: worker resume message: {_first_line(result.stderr, 'send failed')}", file=sys.stderr)
    except (OSError, subprocess.SubprocessError) as error:
        print(f"shutdown: worker resume message: {_error_reason(error)}", file=sys.stderr)


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
    if record["session"]["kind"] == "showrunner" and record["requested_by"]["kind"] != "snapshot":
        note = restart_note(record, restarted_at)
        if not _restart_note_delivered(record, note, dry_run=False):
            return
    _send_worker_resume(current)
    timer_failures = _restore_timers(record["timers"], dry_run=False)
    _record_timer_restore_progress(record, timer_failures)


def _machine_result(records: list[SessionShutdown], machine: str, label: str) -> MachineRestartResult:
    incomplete = [item for item in records if item["progress"]["kind"] != "restarted"
                  and not restart_failed_session_running(item)]
    failed = tuple(
        FailedSessionRestart(item["session"]["name"], item["progress"]["reason"])
        for item in incomplete if item["progress"]["kind"] == "restart failed"
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
        machine, label, "partial" if incomplete else "done",
        restarted=restarted, workers=sum(len(item["workers"]) for item in records if item["progress"]["kind"] == "restarted"),
        failed=len(failed), manual=len(manual), failed_sessions=failed,
        manual_sessions=manual, timers_not_started=pending,
    )


def _timing_line(result: MachineRestartResult) -> str:
    return f"{result.machine}: restarted {result.restarted} in {result.elapsed_seconds:.1f} s"


def _result_lines(result: MachineRestartResult) -> list[str]:
    if result.status == "nothing":
        return []
    lines = [
        "{}: {} restarted, {} workers available on demand".format(
            result.machine, result.restarted, result.workers
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
        message = next((message for message in (herdr.MAC_DESKTOP_REQUIRED, herdr.MAC_DESKTOP_TERMINAL_REQUIRED)
                        if message in line), line)
        print(message)


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
    if record["progress"]["kind"] == "restart failed":
        observed = _owner_liveness(record["session"])
        if isinstance(observed, SessionLive):
            print(f"{record['session']['name']}: already running; skipped")
            return record
        if isinstance(observed, SessionLivenessUnknown):
            print(f"{record['session']['name']}: recovery refused: {observed.reason}")
            return record
    if dry_run:
        _ = launch_entry(record, settle.record_time(), desktop_state, dry_run=True)
        return record
    if record["progress"]["kind"] != "restarting" and _unfinished_cleanup(record) and not recovery_store.legacy_tmux_leftovers(record) and record["session"]["session_id"] not in _live_session_ids():
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
            command = settle.shutdown_command("restart", record["login"], "--here", "--only", record["session"]["session_id"])
            mark_restart_failed(record, f"owner cleanup not confirmed; in a terminal on {record['machine']}: {command}")
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
    return bool(recovery_store.legacy_tmux_leftovers(record)) or any(
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


def _confirm_entries(records: list[SessionShutdown]) -> None:
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
            _acknowledge_restored_snapshots(record["session"]["session_id"])

    remote_control_missing(launched, confirmation)


def _acknowledge_restored_snapshots(identity: str) -> None:
    """A successful restore retires stale account copies of the same session too."""
    for snapshot in all_snapshots():
        if any(entry["session"]["session_id"] == identity for entry in snapshot["sessions"]):
            _ = acknowledge_snapshot(snapshot["login"], identity)


def snapshot_source(last_seen: str) -> str:
    taken = datetime.fromisoformat(last_seen)
    age = int((datetime.now(timezone.utc) - taken).total_seconds())
    return "from snapshot taken " + taken.astimezone(user_zone()).strftime("%Y-%m-%d %H:%M:%S %Z") + f" ({settle.elapsed_text(age)} ago)"


@dataclass(frozen=True)
class UnsharedRestoreHistory:
    """A direct restart reads completed history for its own selection pass."""


@dataclass(frozen=True)
class CompletedRestoreHistory:
    records: tuple[SessionShutdown, ...]


_RESTORE_HISTORY: ContextVar[UnsharedRestoreHistory | CompletedRestoreHistory] = ContextVar(
    "restore_history", default=UnsharedRestoreHistory())


@contextmanager
def restore_history_pass() -> Generator[None, None, None]:
    """Share archive evidence and the security session across an account-wide command."""
    history = CompletedRestoreHistory(tuple(recovery_store.restarted_history()))
    token = _RESTORE_HISTORY.set(history)
    security_token = _COMMAND_SECURITY_SESSION.set(_command_security_session())
    try:
        yield
    finally:
        _COMMAND_SECURITY_SESSION.reset(security_token)
        _RESTORE_HISTORY.reset(token)


def _account_observations(
    records: list[SessionShutdown], snapshots: list[RecoverySnapshot],
) -> dict[str, list[AccountObservation | RestoreAccountConflict]]:
    observations: dict[str, list[AccountObservation | RestoreAccountConflict]] = {}
    for saved in records:
        observations.setdefault(saved["session"]["session_id"], []).append(_record_observation(saved))
    history = _RESTORE_HISTORY.get()
    archived = history.records if isinstance(history, CompletedRestoreHistory) else recovery_store.restarted_history()
    for saved in archived:
        observations.setdefault(saved["session"]["session_id"], []).append(_record_observation(saved))
    for snapshot in snapshots:
        for entry in snapshot["sessions"]:
            observations.setdefault(entry["session"]["session_id"], []).append(AccountObservation(
                snapshot["login"], snapshot["label"], datetime.fromisoformat(entry["last_seen"])))
    return observations


def _restore_choices(
    records: list[SessionShutdown], snapshots: list[RecoverySnapshot],
) -> dict[str, RestoreAccountChosen | RestoreAccountConflict]:
    observations = _account_observations(records, snapshots)
    copies: dict[str, list[AccountObservation]] = {}
    for saved in records:
        copies.setdefault(saved["session"]["session_id"], []).append(AccountObservation(
            saved["login"], saved["label"], datetime.fromisoformat(saved["requested_at"])))
    for snapshot in snapshots:
        for entry in snapshot["sessions"]:
            copies.setdefault(entry["session"]["session_id"], []).append(AccountObservation(
                snapshot["login"], snapshot["label"], datetime.fromisoformat(entry["last_seen"])))
    choices: dict[str, RestoreAccountChosen | RestoreAccountConflict] = {}
    for identity, evidence in observations.items():
        decision = _restore_account(evidence)
        if isinstance(decision, RestoreAccountChosen) and identity in copies:
            available = copies[identity]
            if not any(item.login.casefold() == decision.login for item in available):
                source = _restore_account(available)
                decision = (RestoreAccountChosen(decision.login, source.login)
                            if isinstance(source, RestoreAccountChosen) else source)
        choices[identity] = decision
    return choices


def restore_account_choices() -> dict[str, RestoreAccountChosen | RestoreAccountConflict]:
    """Choose the source copy and destination across all local recovery sources."""
    return _restore_choices(all_session_records(), all_snapshots())


def _restore_target(saved: SessionShutdown, decision: RestoreAccountChosen, *, dry_run: bool) -> SessionShutdown:
    if decision.login == saved["login"].casefold():
        return saved
    target = named_claude_account(decision.login).label
    if dry_run:
        return {**saved, "target": target}
    def retarget(current: SessionShutdown) -> None:
        if current["request_id"] == saved["request_id"]:
            current["target"] = target
    return update_session(saved["login"], saved["session"]["session_id"], retarget)


def _launch_codex_snapshot(
    snapshot: RecoverySnapshot, entry: SnapshotCodexSession, state: DesktopLaunchState, *, dry_run: bool,
) -> SessionLaunched | ManualRestartRequired:
    owner, launch = entry["session"], entry["codex"]
    if owner["host"]["kind"] == "legacy tmux":
        command = legacy_codex_command(entry)
        print(f"manual restart {owner['name']}: {command}")
        return ManualRestartRequired(command)
    suffix = hashlib.sha256(owner["session_id"].encode()).hexdigest()[:16]
    name = herdr.generated_session_name(Path(owner["cwd"]).name + "-codex", suffix)
    arguments = ["resume"]
    if launch["resume"]["kind"] == "session id":
        arguments.append(launch["resume"]["session_id"])
    else:
        print(f"{owner['name']}: Codex resume picker opens")
    if dry_run:
        print(f"herdr {name}: CODEX_HOME={launch['home']} codex {shlex.join(arguments)} in {owner['cwd']}")
        return SessionLaunched()
    server = herdr.restore_server(name, 90_000, session_kind=state.session_kind,
                                  exclusive=True)
    try:
        pane = herdr.open_pane(name, name, owner["cwd"], {"CODEX_HOME": launch["home"]})
        _ = herdr.start_agent(name, pane, kind="codex", agent_arguments=arguments,
                          opening=herdr.NoOpeningPrompt(), timeout_ms=90_000)
    except (herdr.HerdrFailure, OSError, subprocess.SubprocessError):
        herdr.stop_created_server(name, server)
        raise
    focused = _focus_restored_pane(herdr.Herdr(name), pane, owner["name"])
    if focused and isinstance(server, herdr.WindowServerCreated):
        _close_startup_workspaces(herdr.Herdr(name), pane)
    _acknowledge_restored_snapshots(owner["session_id"])
    if isinstance(server, herdr.WindowServerCreated):
        return SessionLaunched()
    attach = shlex.join(["herdr", "session", "attach", name])
    if launches_window(owner["host"]):
        clients = herdr.attached_clients(name)
        if isinstance(clients, herdr.ClientsAttached):
            return SessionLaunched()
        if isinstance(clients, herdr.NoClientAttached):
            if sys.platform == "darwin":
                _herdr_window(CodexWindowRestore(snapshot["machine"], owner["name"]), name,
                    owner["cwd"], DesktopNotInSnapshot(kind="not in snapshot"), state, dry_run=False)
                return SessionLaunched()
            try:
                _open_window(_herdr_attach_command(name, owner["cwd"]),
                    DesktopNotInSnapshot(kind="not in snapshot"), state, dry_run=False)
                return SessionLaunched()
            except (OSError, subprocess.SubprocessError) as error:
                print(f"{snapshot['machine']}: {owner['name']}: window not opened: {_error_reason(error)}")
    print(attach)
    return SessionLaunched()


@dataclass(frozen=True)
class CrashNotChecked:
    """No current boot or host observation was needed yet."""


@dataclass(frozen=True)
class CrashConfirmed:
    """A recorded or current boot/host observation proves a crash signal."""

    signal: Literal["reboot", "host gone"]


@dataclass(frozen=True)
class NoCrashSignal:
    """The observations do not authorize snapshot recovery."""


@dataclass(frozen=True)
class CrashEvidenceUnreadable:
    reason: str


@dataclass
class RecoveryCrashCheck:
    observation: inventory_work.CompleteInventory | inventory_work.PartialInventory | CrashNotChecked = field(default_factory=CrashNotChecked)

    def check(self, snapshot: RecoverySnapshot, entry: SnapshotSession) -> CrashConfirmed | NoCrashSignal | CrashEvidenceUnreadable:
        if "crash" in entry:
            return CrashConfirmed(entry["crash"])
        if isinstance(self.observation, CrashNotChecked):
            self.observation = inventory_work.snapshot_inventory()
        observed = self.observation
        if isinstance(observed, inventory_work.PartialInventory):
            return CrashEvidenceUnreadable("; ".join(observed.failures))
        previous_boot = entry.get("boot_id", snapshot.get("boot_id", ""))
        if (isinstance(observed.boot, inventory_work.KnownBoot) and previous_boot
                and previous_boot != observed.boot.boot_id):
            return CrashConfirmed("reboot")
        if (snapshot["login"], entry["session"]["session_id"]) in observed.gone_hosts:
            return CrashConfirmed("host gone")
        return NoCrashSignal()


def _up(login: str, dry_run: bool, only: Iterable[str] = (), *,
        session_kind: herdr.SecuritySession | herdr.SessionKindNotRead = _SESSION_KIND_NOT_READ) -> MachineRestartResult:
    if isinstance(session_kind, herdr.SessionKindNotRead):
        session_kind = _command_security_session()
    if not dry_run:
        for line in migrate(login):
            print(line)
    ids = tuple(only)
    all_records = session_records(login)
    snapshots = all_snapshots()
    choices = _restore_choices(all_session_records(), snapshots)
    recovery_failures: list[FailedSessionRestart] = []
    sources: dict[str, RecoverySnapshot] = {}
    recorded = {saved["session"]["session_id"]: saved for saved in all_records}
    live_ids = _live_session_ids()
    codex_entries: list[tuple[RecoverySnapshot, SnapshotCodexSession]] = []
    snapshot_ids: set[str] = set()
    crash_check = RecoveryCrashCheck()
    for snapshot in snapshots:
        if snapshot["login"].casefold() != login.casefold():
            continue
        for entry in snapshot["sessions"]:
            identity, name = entry["session"]["session_id"], entry["session"]["name"]
            snapshot_ids.add(identity)
            if (ids and identity not in ids) or (identity in recorded
                    and not recovery_store.snapshot_supersedes_record(snapshot, recorded[identity])):
                continue
            print(f"{name}: {snapshot_source(entry['last_seen'])}")
            decision = choices[identity]
            if isinstance(decision, RestoreAccountConflict):
                reason = "conflicting account evidence: " + ", ".join(decision.accounts)
                print(f"{name}: {reason}")
                recovery_failures.append(FailedSessionRestart(name, reason))
                continue
            if decision.source_login != login.casefold():
                print(f"{name}: skipped obsolete copy on {snapshot['label']}; current account {decision.login}")
                continue
            if entry["agent"] == "codex":
                liveness = inventory_work.codex_liveness(entry)
                if isinstance(liveness, inventory_work.CodexLive):
                    print(f"{name}: already running (pid {liveness.pid}); skipped")
                elif isinstance(liveness, inventory_work.CodexLivenessUnknown):
                    print(f"{name}: recovery refused: {liveness.reason}")
                    recovery_failures.append(FailedSessionRestart(name, liveness.reason))
                else:
                    evidence = crash_check.check(snapshot, entry)
                    if isinstance(evidence, CrashConfirmed):
                        codex_entries.append((snapshot, entry))
                    elif isinstance(evidence, CrashEvidenceUnreadable):
                        print(f"{name}: recovery refused: {evidence.reason}")
                        recovery_failures.append(FailedSessionRestart(name, evidence.reason))
                    else:
                        print(f"{name}: no crash signal; skipped")
                continue
            if identity in live_ids:
                print(f"{name}: already running; skipped")
                continue
            liveness = _owner_liveness(entry["session"])
            if isinstance(liveness, SessionLive):
                print(f"{name}: already running; skipped")
                continue
            if isinstance(liveness, SessionLivenessUnknown):
                print(f"{name}: recovery refused: {liveness.reason}")
                recovery_failures.append(FailedSessionRestart(name, liveness.reason))
                continue
            evidence = crash_check.check(snapshot, entry)
            if isinstance(evidence, CrashEvidenceUnreadable):
                print(f"{name}: recovery refused: {evidence.reason}")
                recovery_failures.append(FailedSessionRestart(name, evidence.reason))
                continue
            if isinstance(evidence, NoCrashSignal):
                print(f"{name}: no crash signal; skipped")
                continue
            verified_entry: recovery_store.SnapshotClaudeSession = {**entry, "crash": evidence.signal}
            saved = (recovery_store.snapshot_restore_record(snapshot, verified_entry) if dry_run
                     else materialize_snapshot(snapshot, verified_entry))
            saved = _restore_target(saved, decision, dry_run=dry_run)
            all_records = [current for current in all_records if current["session"]["session_id"] != identity]
            all_records.append(saved)
            recorded[identity] = saved
            sources[identity] = snapshot
    hold_outcome = lookup_account_hold(login)
    records = [saved for saved in all_records if not ids or saved["session"]["session_id"] in ids]
    selected_records: list[SessionShutdown] = []
    for saved in records:
        name, identity = saved["session"]["name"], saved["session"]["session_id"]
        if identity not in sources:
            print(f"{name}: from shutdown record")
        decision = choices[identity]
        if isinstance(decision, RestoreAccountConflict):
            reason = "conflicting account evidence: " + ", ".join(decision.accounts)
            print(f"{name}: {reason}")
            recovery_failures.append(FailedSessionRestart(name, reason))
            continue
        if decision.source_login != login.casefold():
            print(f"{name}: skipped obsolete copy on {saved['label']}; current account {decision.login}")
            continue
        if decision.login != login.casefold() and identity in live_ids:
            print(f"{name}: already running on {decision.login}; skipped")
            continue
        saved = _restore_target(saved, decision, dry_run=dry_run)
        if saved["requested_by"]["kind"] == "snapshot" and saved["progress"]["kind"] in {"already gone", "restart failed"}:
            observed = _owner_liveness(saved["session"])
            if isinstance(observed, SessionLive):
                print(f"{name}: already running; skipped")
                continue
            if isinstance(observed, SessionLivenessUnknown):
                print(f"{name}: recovery refused: {observed.reason}")
                recovery_failures.append(FailedSessionRestart(name, observed.reason))
                continue
        recovered = _recover_incomplete_shutdown(saved, dry_run=dry_run)
        if isinstance(recovered, IncompleteShutdownStillLive):
            print(f"{name}: already running; skipped")
            selected_records.append(saved)
        elif isinstance(recovered, IncompleteShutdownRefused):
            print(f"{name}: recovery refused: {recovered.reason}")
            recovery_failures.append(FailedSessionRestart(name, recovered.reason))
            selected_records.append(saved)
        else:
            if identity in live_ids:
                print(f"{name}: already running; launch skipped")
            selected_records.append(recovered)
    records = selected_records
    finished: list[SessionShutdown] = []
    if not dry_run:
        checked = clear_running_on_target(records)
        records, finished = checked.remaining, checked.finished
        for record in finished:
            print(f"{record['machine']}: {record['session']['name']} is already running on {record.get('target') or record['label']}")
    refused: list[RestartRefusal] = []
    local_ids = {saved["session"]["session_id"] for saved in all_records} | snapshot_ids
    for identity, name in settle.unrecorded_local_sessions(login, tuple(item for item in ids if item not in snapshot_ids), all_records).items():
        local_ids.add(identity)
        refusal = RestartNoShutdown(kind="no shutdown", session_id=identity, name=name)
        refused.append(refusal)
        print(refusal_line(refusal))
    at = datetime.fromisoformat(settle.record_time())
    for saved in records:
        if ids and saved["progress"]["kind"] in {"waiting", "ready", "held by usage limit", "reaping", "stopping"}:
            refusal = RestartNotStopped(kind="not stopped", session_id=saved["session"]["session_id"],
                name=saved["session"]["name"], progress=cast(Literal["waiting", "ready", "held by usage limit", "reaping", "stopping"], saved["progress"]["kind"]))
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
    if not eligible and not codex_entries:
        if not ids and not dry_run and isinstance(hold_outcome, AccountHeld) and not any(
            item["request_id"] == hold_outcome.hold["request_id"] for item in all_records
        ):
            clear_finished_account_hold(login)
        if not records and not recovery_failures and not ids:
            print(f"nothing to restore for {label}")
        return MachineRestartResult(machine, label, "partial" if recovery_failures else "nothing",
            failed=len(recovery_failures), failed_sessions=tuple(recovery_failures),
            refused=refused, local_session_ids=frozenset(local_ids))
    state = DesktopLaunchState(session_kind=session_kind)
    started_at = time.monotonic()
    attempted: list[SessionShutdown] = []
    codex_restarted = 0
    manual_sessions: list[ManualSessionRestart] = []
    try:
        if dry_run:
            for record in sorted(eligible, key=_dry_run_entry_order):
                if record["progress"]["kind"] != "restarted":
                    _ = relaunch_session(record, dry_run=True, desktop_state=state)
                    if record["session"]["host"]["kind"] in {"legacy tmux", "legacy tmux unit"} or recovery_store.legacy_tmux_leftovers(record):
                        manual_sessions.append(ManualSessionRestart(record["session"]["name"], legacy_resume_command(record)))
            for snapshot, entry in codex_entries:
                launch = _launch_codex_snapshot(snapshot, entry, state, dry_run=True)
                if isinstance(launch, ManualRestartRequired):
                    manual_sessions.append(ManualSessionRestart(entry["session"]["name"], launch.command))
            return MachineRestartResult(machine, label, "partial" if recovery_failures or manual_sessions else "done",
                manual=len(manual_sessions), manual_sessions=tuple(manual_sessions), failed=len(recovery_failures), failed_sessions=tuple(recovery_failures),
                refused=refused, local_session_ids=frozenset(local_ids))
        for snapshot, entry in codex_entries:
            try:
                launch = _launch_codex_snapshot(snapshot, entry, state, dry_run=False)
                if isinstance(launch, ManualRestartRequired):
                    manual_sessions.append(ManualSessionRestart(entry["session"]["name"], launch.command))
                else:
                    codex_restarted += 1
            except (OSError, ValueError, herdr.HerdrFailure, subprocess.SubprocessError) as error:
                recovery_failures.append(FailedSessionRestart(entry["session"]["name"], _error_reason(error)))
        previous_manual_ids = {record["session"]["session_id"] for record in eligible
                               if _prior_progress(record)["kind"] == "manual restart"}
        ordered = sorted(eligible, key=_entry_order)
        others = [record for record in ordered if ids or not _entry_order(record)[0]]
        requesters = [record for record in ordered if not ids and _entry_order(record)[0]]
        with ThreadPoolExecutor(max_workers=RESTART_LAUNCH_WORKERS) as executor:
            workers = [
                (record, executor.submit(relaunch_session, record, dry_run=False, desktop_state=state))
                for record in others if not launches_window(record["session"]["host"])
            ]
            for record in others:
                if launches_window(record["session"]["host"]):
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
        settle.tell_showrunners_outcome([*finished, *attempted])
        remaining = session_records(login)
        if isinstance(hold_outcome, AccountHeld) and not any(item["request_id"] == hold_outcome.hold["request_id"] for item in remaining):
            finished_requests = tuple(item["request_id"] for item in attempted if item["progress"]["kind"] == "restarted")
            if not ids:
                clear_finished_account_hold(login)
            elif finished_requests:
                clear_finished_account_hold(login, request_ids=finished_requests)
        result = replace(_machine_result(attempted, machine, label), elapsed_seconds=time.monotonic() - started_at)
        result = replace(result, restarted=result.restarted + codex_restarted)
        if manual_sessions:
            result = replace(result, status="partial", manual=result.manual + len(manual_sessions), manual_sessions=(*result.manual_sessions, *manual_sessions))
        if recovery_failures:
            result = replace(result, status="partial", failed=result.failed + len(recovery_failures),
                             failed_sessions=(*result.failed_sessions, *recovery_failures))
        result = replace(result, refused=refused, local_session_ids=frozenset(local_ids))
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
    return PeerRestartResult(machine=result.machine, label=result.label,
        outcome="partial" if result.failed else result.status,
        lines=output.getvalue().splitlines(), no_remote_control=[], refused=list(result.refused))


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
    refusals: list[RestartRefusal] = []
    for key in ("refused",):
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
            if item.get("kind") == "no shutdown":
                refusals.append(RestartNoShutdown(kind="no shutdown", session_id=identity, name=name))
            elif item.get("kind") == "not stopped" and item.get("progress") in ("waiting", "ready", "held by usage limit", "reaping", "stopping"):
                refusals.append(RestartNotStopped(kind="not stopped", session_id=identity, name=name,
                    progress=cast(Literal["waiting", "ready", "held by usage limit", "reaping", "stopping"], item["progress"])))
            else:
                raise ValueError("invalid peer restart refusal")
    return PeerRestartResult(machine=cast(str, fields["machine"]), label=cast(str, fields["label"]),
        outcome=cast(Literal["done", "partial", "nothing"], fields["outcome"]), lines=cast(list[str], raw_lines),
        no_remote_control=[], refused=refusals)


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


def restart_failed_liveness(saved: SessionShutdown) -> SessionLive | SessionNotLive | SessionLivenessUnknown:
    """Observe the session behind an unconfirmed restart without changing its record."""
    return _owner_liveness(saved["session"])


def restart_failed_session_running(saved: SessionShutdown) -> bool:
    return (saved["progress"]["kind"] == "restart failed"
            and isinstance(restart_failed_liveness(saved), SessionLive))


def _not_back_records(login: str, ids: tuple[str, ...]) -> list[SessionShutdown]:
    try:
        records = session_records(login)
    except (InvalidRecord, OSError, ValueError):
        return []
    return [saved for saved in records
            if saved["machine"] == settle.local_machine()
            and saved["progress"]["kind"] in {"restart failed", "manual restart"}
            and (not ids or saved["session"]["session_id"] in ids)
            and not restart_failed_session_running(saved)]


def _pushed_not_back(login: str, ids: tuple[str, ...]) -> set[str]:
    """Names of this machine's sessions whose unchanged not-back state was already pushed."""
    return {saved["session"]["name"] for saved in _not_back_records(login, ids)
            if saved.get("restore_alerted") == settle.not_back_text(saved)}


def _mark_pushed_not_back(login: str, ids: tuple[str, ...], names: set[str]) -> None:
    for saved in _not_back_records(login, ids):
        if saved["session"]["name"] in names:
            settle.mark_not_back_pushed(saved)


def restart_push(label: str, names: list[str], *, machine: str) -> tuple[RenderedPush, ...]:
    """Render known failed sessions, or the unconfirmed machine when names are unavailable."""
    if not names:
        return (render("shutdown.restart_incomplete", account=json.dumps(label), machine=machine),)

    def named_push(group: list[str]) -> RenderedPush:
        stopped = settle.stopped_sessions_text(len(group)) + " (" + ", ".join(group) + ")"
        return render("shutdown.restart_failed", account=json.dumps(label), stopped=stopped, machine=machine)

    def fits(push: RenderedPush) -> bool:
        return len(push.values["stopped"]) <= 400 and len(push.message) < 500 and len(push.title) < 45

    pushes: list[RenderedPush] = []
    group: list[str] = []
    for name in names:
        candidate = named_push([*group, name])
        if group and not fits(candidate):
            pushes.append(named_push(group))
            group = []
            candidate = named_push([name])
        if not fits(candidate):
            excess = max(len(candidate.values["stopped"]) - 400, len(candidate.message) - 499)
            shortened = name[:max(0, len(name) - excess - 1)] + "…"
            pushes.append(named_push([shortened]))
            continue
        group.append(name)
    if group:
        pushes.append(named_push(group))
    return tuple(pushes)


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

    session_kind = _command_security_session()
    notify = isinstance(alerts, AlertWhenNotBack) and not dry_run
    ids = tuple(only)
    local_ids: set[str] = set()
    try:
        token = _COMMAND_SECURITY_SESSION.set(session_kind)
        try:
            local = _up(selected.login, dry_run, ids)
        finally:
            _COMMAND_SECURITY_SESSION.reset(token)
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
    if isinstance(alerts, NeverAlert):
        remote_args.append("--no-alert")
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
    if not_reached:
        retry = ["restart", selected.login, "--here"]
        if peer_ids:
            retry.extend(("--only", ",".join(peer_ids)))
        if dry_run:
            retry.append("--dry-run")
        if isinstance(alerts, NeverAlert):
            retry.append("--no-alert")
        print(f"restart incomplete: {other_machine()} not reached; when it is back, "
            + f"in a terminal on {other_machine()}: {settle.shutdown_command(*retry)}")
    bring_back = local.status == "partial" or peer["outcome"] == "partial" or not_reached
    if notify:
        failure_alert = bring_back and _restart_alerts_enabled(selected.login, ids, peer=peer_availability)
        try:
            if failure_alert:
                # A retry that fails the same way as a pushed failure tells the user nothing new.
                pushed = _pushed_not_back(selected.login, ids)
                running = {saved["session"]["name"] for saved in session_records(selected.login)
                           if (not ids or saved["session"]["session_id"] in ids)
                           and restart_failed_session_running(saved)}
                failed = [item for item in local.failed_sessions if item.name not in pushed and item.name not in running]
                manual = [item for item in local.manual_sessions if item.name not in pushed]
                names = [item.name for item in failed]
                names.extend(match.group(1) for line in peer["lines"]
                             if (match := re.match(r"[^:]+: (.+) restart failed:", line)) is not None)
                local_partial = local.status == "partial" and (failed or manual or not (
                    local.failed_sessions or local.manual_sessions))
                machines = [local.machine] if local_partial else []
                if peer["outcome"] == "partial" or not_reached:
                    machines.append(peer["machine"])
                # Pending scheduled jobs are reported in the command output; they need no user step.
                peer_requires_retry = peer["outcome"] == "partial" and not (peer["lines"] and all(
                    re.match(r"[^:]+: (timer .+ not started:|manual restart |[0-9]+ restarted|restarted [0-9]+|already running; skipped$)", line)
                    for line in peer["lines"]))
                if names or not_reached or failed or peer_requires_retry:
                    for push in restart_push(selected.label, names, machine=" and ".join(machines)):
                        settle.alert_user(push)
                for item in manual:
                    settle.alert_user(render("shutdown.manual_restart", session=item.name, machine=local.machine))
                _mark_pushed_not_back(selected.login, ids, {item.name for item in [*failed, *manual]})
                for line in peer["lines"]:
                    if match := re.match(r"[^:]+: manual restart (.+?):", line):
                        settle.alert_user(render("shutdown.manual_restart", session=match.group(1), machine=peer["machine"]))
        except (OSError, RuntimeError, ValueError):
            pass
    return 0 if local.exit_status == 0 and peer["outcome"] != "partial" and not peer["refused"] else 1
