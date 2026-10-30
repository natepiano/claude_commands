#!/usr/bin/env python3
"""Persist per-session shutdown requests and account launch holds."""

from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import import_module
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, NotRequired, Protocol, TypedDict, cast

from session_types import Session, parse_session
from session_types import (
    TimerRestore as TimerRestore,
    CodexSessionId as CodexSessionId,
    CodexResumePicker as CodexResumePicker,
    CodexLaunch as CodexLaunch,
    SnapshotClaudeSession as SnapshotClaudeSession,
    SnapshotCodexSession as SnapshotCodexSession,
    SnapshotSession as SnapshotSession,
    CompleteInventory,
    PartialInventory,
    KnownBoot,
)
from launch_permission import launch_barrier, shutdown_state_root

for dependency in ("lib", "whoami"):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / dependency))
from user_zone import user_zone  # noqa: E402
from note_file import canonical_label, parse_account_label, NumberedAccountLabel
from account import ResolvedHistoryLabel, HistoricalAccountUnresolved

CLAIM_STALE_SECONDS = 120
CONDUCTOR_LOCK_RETRY_DELAYS = (0.05,) * 5
_ROOT_METADATA = frozenset({"launch.lock", "snapshot_attempt.json", "snapshot_attempt.lock"})


def _is_root_metadata(name: str) -> bool:
    return name in _ROOT_METADATA or (name.startswith(".record.") and name.endswith(".tmp"))


class SnapshotTaken(TypedDict):
    kind: Literal["taken"]
    at: str
    accounts: int


class SnapshotFailed(TypedDict):
    kind: Literal["failed"]
    at: str
    reasons: list[str]


class LastSuccessfulSnapshot(TypedDict):
    kind: Literal["taken"]
    at: str


class NoSuccessfulSnapshotRecorded(TypedDict):
    kind: Literal["not recorded"]


class SnapshotAttempts(TypedDict):
    machine: str
    latest: SnapshotTaken | SnapshotFailed
    last_success: LastSuccessfulSnapshot | NoSuccessfulSnapshotRecorded


@dataclass(frozen=True)
class NoSnapshotAttempt:
    pass


class HistoricalAccountResolver(Protocol):
    def resolve_history_label(self, label: str, at: str, *, login: str = "") -> ResolvedHistoryLabel | HistoricalAccountUnresolved: ...


class ReadinessClear(TypedDict):
    kind: Literal["clear"]


class ReadinessBlocked(TypedDict):
    kind: Literal["blocked"]
    reason: str


ReadinessCheck = ReadinessClear | ReadinessBlocked


class _LegacyAllAccountSessions(TypedDict):
    kind: Literal["all account sessions"]


class _LegacySelectedSessions(TypedDict):
    kind: Literal["selected"]
    session_ids: list[str]


_LegacyScope = _LegacyAllAccountSessions | _LegacySelectedSessions


class FromSession(TypedDict):
    kind: Literal["session"]
    session_id: str


class FromTerminal(TypedDict):
    kind: Literal["terminal"]


class RebootRecovery(TypedDict):
    kind: Literal["reboot"]
    last_seen: str


class LostHostRecovery(TypedDict):
    kind: Literal["host gone"]
    last_seen: str


class AgentExitRecovery(TypedDict):
    kind: Literal["agent exited"]
    last_seen: str


class RecoveryEvidenceNotRecorded(TypedDict):
    kind: Literal["not recorded"]


RecoveryContext = RebootRecovery | LostHostRecovery | AgentExitRecovery | RecoveryEvidenceNotRecorded


class FromSnapshot(TypedDict):
    kind: Literal["snapshot"]
    taken_at: str
    recovery: RecoveryContext


RequestOrigin = FromSession | FromTerminal | FromSnapshot


class _LegacySystemdConductor(TypedDict):
    kind: Literal["systemd"]
    unit: str


class _LegacyLaunchdConductor(TypedDict):
    kind: Literal["launchd"]
    label: str


class _LegacyConductorNotStarted(TypedDict):
    kind: Literal["not started"]


_LegacyConductor = _LegacySystemdConductor | _LegacyLaunchdConductor | _LegacyConductorNotStarted
StopTiming = Literal["wait for ready", "now"]
StopLeftRunningCause = Literal["stop not confirmed", "owner identity lost"]


class NotReadyToStop(TypedDict):
    kind: Literal["not ready to stop"]
    at: str
    status: str
    progress: Literal["waiting", "ready", "held by usage limit", "passive worker ready"]


class StillRunningAfterStop(TypedDict):
    kind: Literal["still running"]
    at: str
    reason: str


class AccountUnreadableAtStop(TypedDict):
    kind: Literal["account unreadable"]
    at: str


class _LegacyWorkerStillLive(TypedDict):
    kind: Literal["worker still live"]
    at: str


class CodexServerLeftRunning(TypedDict):
    kind: Literal["codex server left running"]
    at: str
    run_dir: str
    cause: StopLeftRunningCause


class LegacyTmuxSessionLeftRunning(TypedDict):
    kind: Literal["legacy tmux session left running"]
    at: str
    session_name: str
    cause: StopLeftRunningCause


class UnitSessionLeftRunning(TypedDict):
    kind: Literal["unit session left running"]
    at: str
    host: Literal["herdr"]
    session_name: str
    cause: StopLeftRunningCause


SessionStopIssue = (
    NotReadyToStop
    | StillRunningAfterStop
    | AccountUnreadableAtStop
    | CodexServerLeftRunning
    | UnitSessionLeftRunning
    | LegacyTmuxSessionLeftRunning
)


def legacy_tmux_leftovers(record: SessionShutdown) -> list[LegacyTmuxSessionLeftRunning]:
    """Every legacy leftover requires explicit manual cleanup, whatever its cause."""
    return [issue for issue in record["stop_issues"] if issue["kind"] == "legacy tmux session left running"]


class _LegacyStopClaimFailed(TypedDict):
    kind: Literal["stop claim failed"]
    at: str
    machine: str
    reason: str


class _LegacyMachineStopFailed(TypedDict):
    kind: Literal["machine stop failed"]
    at: str
    machine: str
    reason: str


_LegacyOrchestrationIssue = _LegacyStopClaimFailed | _LegacyMachineStopFailed
_LegacyStopIssue = SessionStopIssue | _LegacyWorkerStillLive | _LegacyOrchestrationIssue




class RecoverySnapshot(TypedDict):
    machine: str
    login: str
    label: str
    taken_at: str
    boot_id: NotRequired[str]
    sessions: list[SnapshotSession]


@dataclass(frozen=True)
class NoRecoverySnapshot:
    """No recovery snapshot has been written for this login."""


def _codex_launch(value: object, place: str) -> CodexLaunch:
    values = _mapping(value, place)
    home = _string(_required(values, "home", place), f"{place}.home")
    if not Path(home).is_absolute():
        raise InvalidRecord(f"{place}.home must be absolute")
    resume = _mapping(_required(values, "resume", place), f"{place}.resume")
    kind = _kind(resume, f"{place}.resume", frozenset({"session id", "picker"}))
    if kind == "session id":
        session_id = _string(_required(resume, "session_id", place), f"{place}.resume.session_id")
        if not session_id:
            raise InvalidRecord(f"{place}.resume.session_id must not be empty")
    return cast(CodexLaunch, cast(object, values))


class SettleMessageNotSent(TypedDict):
    kind: Literal["not sent"]


class SettleMessageSent(TypedDict):
    kind: Literal["sent"]
    at: str


class SettleMessageQueued(TypedDict):
    kind: Literal["queued"]
    at: str
    reason: str


SettleMessage = SettleMessageNotSent | SettleMessageSent | SettleMessageQueued


class WhereSaid(TypedDict):
    kind: Literal["said"]
    text: str
    at: str


class WhereNotSaid(TypedDict):
    kind: Literal["not said"]


Where = WhereSaid | WhereNotSaid


class SessionWaiting(TypedDict):
    kind: Literal["waiting"]


class SessionReaping(TypedDict):
    kind: Literal["reaping"]
    at: str
    because: Literal["refused in its transcript", "account out of usage"]
    detail: str


class SessionReadyToStop(TypedDict):
    kind: Literal["ready"]
    at: str


class SessionHeldByUsageLimit(TypedDict):
    kind: Literal["held by usage limit"]
    at: str


class _LegacyPassiveWorkerReady(TypedDict):
    kind: Literal["passive worker ready"]
    at: str


class SessionStopped(TypedDict):
    kind: Literal["stopped"]
    at: str


class SessionStopping(TypedDict):
    kind: Literal["stopping"]
    at: str


class SessionAlreadyGone(TypedDict):
    kind: Literal["already gone"]
    at: str


class ProcessIdentityLost(TypedDict):
    kind: Literal["process identity lost"]
    at: str


class SessionStopFailed(TypedDict):
    kind: Literal["stop failed"]
    at: str
    reason: str


class SessionRestarted(TypedDict):
    kind: Literal["restarted"]
    at: str


class SessionRestarting(TypedDict):
    kind: Literal["restarting"]
    at: str
    prior: SessionRestartPrior


class PendingTimer(TypedDict):
    instance: str
    reason: str


class SessionRestoredTimersPending(TypedDict):
    kind: Literal["timers pending"]
    at: str
    timers: list[PendingTimer]


class _LegacyWorkerAvailable(TypedDict):
    kind: Literal["worker available on demand"]
    at: str


class SessionRestartFailed(TypedDict):
    kind: Literal["restart failed"]
    at: str
    reason: str
    trust_command: NotRequired[str]  # the user's fix when no account on the machine trusts the folder


class SessionNeedsManualRestart(TypedDict):
    kind: Literal["manual restart"]
    command: str


SessionRestartPrior = (
    SessionWaiting
    | SessionReaping
    | SessionReadyToStop
    | SessionHeldByUsageLimit
    | SessionStopped
    | SessionStopping
    | SessionAlreadyGone
    | ProcessIdentityLost
    | SessionStopFailed
    | SessionRestarted
    | SessionRestoredTimersPending
    | SessionRestartFailed
    | SessionNeedsManualRestart
)

SessionProgress = SessionRestartPrior | SessionRestarting


class _LegacySessionEntry(TypedDict):
    session: Session
    timers: list[TimerRestore]
    settle_message: SettleMessage
    where: Where
    progress: SessionProgress | _LegacyPassiveWorkerReady | _LegacyWorkerAvailable
    stop_issues: list[SessionStopIssue | _LegacyWorkerStillLive]


_LegacyState = Literal[
    "settling",
    "stopping",
    "down",
    "stop partial",
    "restarting",
    "restart partial",
    "cancelled",
    "up",
]


ShutdownContinuation = Literal["stop", "restart"]


class _LegacyShutdownRecord(TypedDict):
    login: str
    label: str
    machine: str
    state: _LegacyState
    requested_at: str
    requested_by: RequestOrigin
    scope: _LegacyScope
    conductor: _LegacyConductor
    force: StopTiming
    then: NotRequired[ShutdownContinuation]
    entries: list[_LegacySessionEntry]
    stop_issues: list[_LegacyOrchestrationIssue]


class ProgressStep(TypedDict):
    at: str
    kind: str
    detail: str


class PausedSeat(TypedDict):
    """A director's worker thread and the launcher needed to continue its task."""
    run_dir: str
    seat: str
    role: str
    thread_id: str
    turn_live: bool
    launcher_command: list[str]


class CheckoutMove(TypedDict):
    old: str
    new: str


class SessionShutdown(TypedDict):
    # A machine-wide bounded stop owns this request instead of the account conductor.
    stop_deadline: NotRequired[str]
    force_stop_note: NotRequired[str]
    move_id: NotRequired[str]
    login: str
    label: str
    machine: str
    requested_at: str
    request_id: str
    requested_by: RequestOrigin
    hold: Literal["account", "session"]
    then: ShutdownContinuation
    force: StopTiming
    alerts: Literal["user", "none"]
    probe: bool
    session: Session
    workers: list[Session]
    codex_seats: NotRequired[list[PausedSeat]]
    timers: list[TimerRestore]
    settle_message: SettleMessage
    where: Where
    progress: SessionProgress
    trail: list[ProgressStep]
    stop_issues: list[SessionStopIssue]
    # Presentation can fail after Claude starts; these issues do not warrant another launch.
    restart_issues: NotRequired[list[str]]
    alerted_at: NotRequired[str]
    # The phone push for a form open in the session's pane; the user is told once.
    form_alerted_at: NotRequired[str]
    # The one repeat of the pause message when no ready mark came back.
    reasked_at: NotRequired[str]
    # The not-back failure last pushed to the user, so an unchanged one is not pushed again.
    restore_alerted: NotRequired[str]
    # The last line a unit's showrunner was sent about this stop: "stopping", "back", or the not-back failure.
    showrunner_told: NotRequired[str]
    # Absent means resume on session.config_dir, never on a changed role.
    target: NotRequired[str]
    source_label: NotRequired[str]
    why: NotRequired[str]
    # Set by rename_unit while the unit is stopped: the restart note tells the session its new names.
    renamed: NotRequired[str]
    # A move stopped the session mid-turn; its last tool call, or "" when the transcript showed none.
    cut_off: NotRequired[str]
    # Set by `rename.py checkout` for a stopped showrunner: the launch moves its checkout first (restart.move_checkout).
    checkout_move: NotRequired[CheckoutMove]


class AccountHold(TypedDict):
    login: str
    label: str
    machine: str
    requested_at: str
    request_id: str
    requested_by: RequestOrigin
    then: ShutdownContinuation
    # Legacy holds name only request_id; legacy incomplete registration stays held.
    registration: NotRequired[Literal["in progress", "complete"]]
    # New holds retain every covered request, including after its registration finishes.
    request_ids: NotRequired[list[str]]
    # A request stays here until its owner loop completes; exceptions leave it held.
    registering_request_ids: NotRequired[list[str]]


def stop_incomplete(saved: SessionShutdown) -> bool:
    """Report a failed stop or owner resources left after a terminal stop."""
    progress = saved["progress"]["kind"]
    return progress == "stop failed" or bool(saved["stop_issues"] and progress in {
        "stopped", "already gone", "process identity lost",
    })


# Progress after which a session's shutdown takes no further step by itself.
_FINISHED_PROGRESS = frozenset({"restarted", "stop failed", "restart failed", "manual restart", "timers pending"})
_STOPPED_PROGRESS = frozenset({"stopped", "already gone", "process identity lost"})


def bounded_attempt_finished(saved: SessionShutdown) -> bool:
    """A bounded failure is terminal until explicitly retried, never a legacy stop."""
    return "stop_deadline" in saved and (saved["progress"]["kind"] in _FINISHED_PROGRESS
        or saved["progress"]["kind"] in _STOPPED_PROGRESS and saved["then"] == "stop")


def shutdown_finished(saved: SessionShutdown) -> bool:
    """Bounded failures end their attempt; legacy force retries and restart continuations remain."""
    if bounded_attempt_finished(saved):
        return True
    if saved["force"] == "now" and stop_incomplete(saved):
        return False
    progress = saved["progress"]["kind"]
    return progress in _FINISHED_PROGRESS or (progress in _STOPPED_PROGRESS and saved["then"] == "stop")


def _legacy_continuation(record: _LegacyShutdownRecord) -> ShutdownContinuation:
    """Legacy records stop; an explicit restart survives conductor launches."""
    return record.get("then", "stop")


class NoLiveRecord(LookupError):
    """Raised when an operation needs a live record but none exists."""


class InvalidRecord(ValueError):
    """Raised when shutdown record JSON does not match the wire format."""


_RECORD_STATES = frozenset(
    {
        "settling",
        "stopping",
        "down",
        "stop partial",
        "restarting",
        "restart partial",
        "cancelled",
        "up",
    }
)
_PROGRESS_WITH_TIME = frozenset(
    {
        "ready",
        "reaping",
        "held by usage limit",
        "passive worker ready",
        "stopped",
        "stopping",
        "already gone",
        "process identity lost",
        "stop failed",
        "restarted",
        "restarting",
        "timers pending",
        "worker available on demand",
        "restart failed",
    }
)


def _state_root() -> Path:
    return shutdown_state_root()


def _safe_login(login: str) -> str:
    if not login or "/" in login or "\0" in login or login.startswith("."):
        raise ValueError("login is unsafe for record storage")
    return login


def _account_directory(login: str) -> Path:
    return _state_root() / _safe_login(login).casefold()


@contextmanager
def _directory_lock(directory: Path) -> Generator[None, None, None]:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


@contextmanager
def _account_lock(login: str, *, create_directory: bool) -> Generator[Path, None, None]:
    directory = _account_directory(login)
    if create_directory:
        directory.mkdir(parents=True, exist_ok=True)
    with _directory_lock(directory):
        lock_path = directory / "lock"
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield directory
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _mapping(value: object, place: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise InvalidRecord(f"{place} must be an object")
    return cast(dict[str, object], value)


def _items(value: object, place: str) -> list[object]:
    if not isinstance(value, list):
        raise InvalidRecord(f"{place} must be a list")
    return cast(list[object], value)


def _required(values: dict[str, object], name: str, place: str) -> object:
    if name not in values:
        raise InvalidRecord(f"{place}.{name} is missing")
    return values[name]


def _string(value: object, place: str) -> str:
    if not isinstance(value, str):
        raise InvalidRecord(f"{place} must be a string")
    return value


def _boolean(value: object, place: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidRecord(f"{place} must be a boolean")
    return value


def _kind(
    values: dict[str, object], place: str, expected: frozenset[str]
) -> str:
    kind = _string(_required(values, "kind", place), f"{place}.kind")
    if kind not in expected:
        raise InvalidRecord(f"{place}.kind is invalid")
    return kind


def _strings(value: object, place: str) -> list[str]:
    items = _items(value, place)
    return [_string(item, f"{place}[{index}]") for index, item in enumerate(items)]


def _utc_time(value: object, place: str) -> str:
    text = _string(value, place)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise InvalidRecord(f"{place} must be an ISO-8601 UTC time") from error
    if (
        parsed.microsecond != 0
        or parsed.isoformat(timespec="seconds") != text
        or parsed.utcoffset() != timedelta(0)
    ):
        raise InvalidRecord(f"{place} must be an ISO-8601 UTC time")
    return text


def _scope(value: object, place: str) -> _LegacyScope:
    values = _mapping(value, place)
    kind = _kind(values, place, frozenset({"all account sessions", "selected"}))
    if kind == "selected":
        _ = _strings(
            _required(values, "session_ids", place), f"{place}.session_ids"
        )
    return cast(_LegacyScope, cast(object, values))


def _request_origin(value: object, place: str) -> RequestOrigin:
    values = _mapping(value, place)
    kind = _kind(values, place, frozenset({"session", "terminal", "snapshot"}))
    if kind == "session":
        _ = _string(
            _required(values, "session_id", place), f"{place}.session_id"
        )
    elif kind == "snapshot":
        _ = _utc_time(_required(values, "taken_at", place), f"{place}.taken_at")
        if "recovery" not in values:
            values["recovery"] = RecoveryEvidenceNotRecorded(kind="not recorded")
        context = _mapping(values["recovery"], f"{place}.recovery")
        signal = _kind(context, f"{place}.recovery", frozenset({"reboot", "host gone", "agent exited", "not recorded"}))
        if signal != "not recorded":
            _ = _utc_time(_required(context, "last_seen", place), f"{place}.recovery.last_seen")
    return cast(RequestOrigin, cast(object, values))


def _conductor(value: object, place: str) -> _LegacyConductor:
    values = _mapping(value, place)
    kind = _kind(values, place, frozenset({"systemd", "launchd", "not started"}))
    if kind == "systemd":
        _ = _string(_required(values, "unit", place), f"{place}.unit")
    elif kind == "launchd":
        _ = _string(_required(values, "label", place), f"{place}.label")
    return cast(_LegacyConductor, cast(object, values))


def _timer(value: object, place: str) -> TimerRestore:
    values = _mapping(value, place)
    _ = _string(_required(values, "instance", place), f"{place}.instance")
    _ = _boolean(
        _required(values, "was_enabled", place), f"{place}.was_enabled"
    )
    # A record written while reply footers existed also has `footer`, which nothing restores now.
    _ = values.pop("footer", None)
    return cast(TimerRestore, cast(object, values))


def _settle_message(value: object, place: str) -> SettleMessage:
    values = _mapping(value, place)
    kind = _kind(values, place, frozenset({"not sent", "sent", "queued"}))
    if kind in {"sent", "queued"}:
        _ = _utc_time(_required(values, "at", place), f"{place}.at")
    if kind == "queued":
        _ = _string(_required(values, "reason", place), f"{place}.reason")
    return cast(SettleMessage, cast(object, values))


def _where(value: object, place: str) -> Where:
    values = _mapping(value, place)
    kind = _kind(values, place, frozenset({"said", "not said"}))
    if kind == "said":
        _ = _string(_required(values, "text", place), f"{place}.text")
        _ = _utc_time(_required(values, "at", place), f"{place}.at")
    return cast(Where, cast(object, values))


def _progress(value: object, place: str, *, legacy: bool = False) -> SessionProgress | _LegacyPassiveWorkerReady | _LegacyWorkerAvailable:
    values = _mapping(value, place)
    kind = _kind(
        values,
        place,
        frozenset({"waiting", "manual restart"}) | _PROGRESS_WITH_TIME,
    )
    if kind in _PROGRESS_WITH_TIME:
        _ = _utc_time(_required(values, "at", place), f"{place}.at")
    if kind in {"passive worker ready", "worker available on demand"} and not legacy:
        raise InvalidRecord(f"{place}.kind is invalid")
    if kind == "reaping":
        _ = _kind(values, place, frozenset({"reaping"}))
        because = _string(_required(values, "because", place), f"{place}.because")
        if because not in {"refused in its transcript", "account out of usage"}:
            raise InvalidRecord(f"{place}.because is invalid")
        _ = _string(_required(values, "detail", place), f"{place}.detail")
    if kind == "restarting":
        prior = _progress(_required(values, "prior", place), f"{place}.prior")
        if prior["kind"] == "restarting":
            raise InvalidRecord(f"{place}.prior must not be a restart claim")
        values["prior"] = prior
    if kind in {"stop failed", "restart failed"}:
        _ = _string(_required(values, "reason", place), f"{place}.reason")
        if kind == "restart failed" and "trust_command" in values:
            _ = _string(values["trust_command"], f"{place}.trust_command")
    elif kind == "timers pending":
        timer_values = _items(
            _required(values, "timers", place), f"{place}.timers"
        )
        values["timers"] = [
            _pending_timer(timer, f"{place}.timers[{index}]")
            for index, timer in enumerate(timer_values)
        ]
    elif kind == "manual restart":
        _ = _string(_required(values, "command", place), f"{place}.command")
    return cast(SessionProgress, cast(object, values))


def _pending_timer(value: object, place: str) -> PendingTimer:
    values = _mapping(value, place)
    _ = _string(_required(values, "instance", place), f"{place}.instance")
    _ = _string(_required(values, "reason", place), f"{place}.reason")
    return cast(PendingTimer, cast(object, values))


def _stop_issue_fields(
    value: object, place: str, expected: frozenset[str]
) -> tuple[dict[str, object], str]:
    values = _mapping(value, place)
    kind = _kind(values, place, expected)
    _ = _utc_time(_required(values, "at", place), f"{place}.at")
    return values, kind


def _session_stop_issue(value: object, place: str, *, legacy: bool = False) -> SessionStopIssue | _LegacyWorkerStillLive:
    values, kind = _stop_issue_fields(
        value,
        place,
        frozenset(
            {
                "not ready to stop",
                "still running",
                "account unreadable",
                "worker still live",
                "codex server left running",
                "unit tmux session left running",
                "legacy tmux session left running",
                "unit session left running",
            }
        ),
    )
    if kind == "worker still live" and not legacy:
        raise InvalidRecord(f"{place}.kind is invalid")
    if kind == "not ready to stop":
        _ = _string(_required(values, "status", place), f"{place}.status")
        progress = _string(
            _required(values, "progress", place), f"{place}.progress"
        )
        if progress not in {"waiting", "ready", "held by usage limit", "passive worker ready"}:
            raise InvalidRecord(f"{place}.progress is invalid")
    elif kind == "still running":
        _ = _string(_required(values, "reason", place), f"{place}.reason")
    elif kind == "codex server left running":
        _ = _string(_required(values, "run_dir", place), f"{place}.run_dir")
        cause = _string(_required(values, "cause", place), f"{place}.cause")
        if cause not in {"stop not confirmed", "owner identity lost"}:
            raise InvalidRecord(f"{place}.cause is invalid")
    elif kind in {"unit tmux session left running", "unit session left running", "legacy tmux session left running"}:
        key = "tmux_session" if kind == "unit tmux session left running" else "session_name"
        name = _string(_required(values, key, place), f"{place}.{key}")
        host = "tmux" if kind != "unit session left running" else _string(
            _required(values, "host", place), f"{place}.host")
        if host not in {"tmux", "herdr"}:
            raise InvalidRecord(f"{place}.host is invalid")
        cause = _string(_required(values, "cause", place), f"{place}.cause")
        if cause not in {"stop not confirmed", "owner identity lost"}:
            raise InvalidRecord(f"{place}.cause is invalid")
        if host == "tmux":
            values = {"kind": "legacy tmux session left running", "at": values["at"],
                      "session_name": name, "cause": cause}
    return cast(SessionStopIssue, cast(object, values))


def _orchestration_stop_issue(
    value: object, place: str
) -> _LegacyOrchestrationIssue:
    values, _ = _stop_issue_fields(
        value,
        place,
        frozenset({"stop claim failed", "machine stop failed"}),
    )
    _ = _string(_required(values, "machine", place), f"{place}.machine")
    _ = _string(_required(values, "reason", place), f"{place}.reason")
    return cast(_LegacyOrchestrationIssue, cast(object, values))


def _recover_not_ready_stop(entry: _LegacySessionEntry) -> None:
    """Recover only the old terminal outcome for a temporary readiness refusal."""
    progress = entry["progress"]
    issues = entry["stop_issues"]
    if (progress["kind"] != "stop failed"
            or progress["reason"] != "session or worker is not idle and ready"
            or len(issues) != 1):
        return
    issue = issues[0]
    if issue["kind"] != "not ready to stop":
        return
    if issue["progress"] == "waiting":
        entry["progress"] = SessionWaiting(kind="waiting")
    elif issue["progress"] == "ready":
        entry["progress"] = SessionReadyToStop(kind="ready", at=issue["at"])
    elif issue["progress"] == "held by usage limit":
        entry["progress"] = SessionHeldByUsageLimit(kind="held by usage limit", at=issue["at"])
    else:
        entry["progress"] = _LegacyPassiveWorkerReady(kind="passive worker ready", at=issue["at"])


def _entry(value: object, place: str) -> _LegacySessionEntry:
    values = _mapping(value, place)
    # Earlier records duplicated this session field on the entry.
    _ = values.pop("config_dir", None)
    try:
        values["session"] = parse_session(
            _required(values, "session", place), f"{place}.session"
        )
    except ValueError as error:
        raise InvalidRecord(str(error)) from error
    timer_values = _items(_required(values, "timers", place), f"{place}.timers")
    values["timers"] = [
        _timer(timer, f"{place}.timers[{index}]")
        for index, timer in enumerate(timer_values)
    ]
    values["settle_message"] = _settle_message(
        _required(values, "settle_message", place), f"{place}.settle_message"
    )
    values["where"] = _where(
        _required(values, "where", place), f"{place}.where"
    )
    values["progress"] = _progress(
        _required(values, "progress", place), f"{place}.progress", legacy=True
    )
    stop_issue_values = _items(values.get("stop_issues", []), f"{place}.stop_issues")
    values["stop_issues"] = [
        _session_stop_issue(issue, f"{place}.stop_issues[{index}]", legacy=True)
        for index, issue in enumerate(stop_issue_values)
    ]
    checked = cast(_LegacySessionEntry, cast(object, values))
    _recover_not_ready_stop(checked)
    return checked


def _record(value: object, place: str) -> _LegacyShutdownRecord:
    values = _mapping(value, place)
    login = _string(_required(values, "login", place), f"{place}.login")
    try:
        _ = _safe_login(login)
    except ValueError as error:
        raise InvalidRecord(f"{place}.login is unsafe for record storage") from error
    values["label"] = canonical_label(_string(_required(values, "label", place), f"{place}.label"))
    _ = _string(_required(values, "machine", place), f"{place}.machine")
    state = _string(_required(values, "state", place), f"{place}.state")
    if state not in _RECORD_STATES:
        raise InvalidRecord(f"{place}.state is invalid")
    _ = _utc_time(
        _required(values, "requested_at", place), f"{place}.requested_at"
    )
    values["requested_by"] = _request_origin(
        _required(values, "requested_by", place), f"{place}.requested_by"
    )
    values["scope"] = _scope(_required(values, "scope", place), f"{place}.scope")
    values["conductor"] = _conductor(
        _required(values, "conductor", place), f"{place}.conductor"
    )
    force = _string(_required(values, "force", place), f"{place}.force")
    if force not in {"wait for ready", "now"}:
        raise InvalidRecord(f"{place}.force is invalid")
    if "then" in values:
        next_action = _string(values["then"], f"{place}.then")
        if next_action not in {"stop", "restart"}:
            raise InvalidRecord(f"{place}.then is invalid")
    entry_values = _items(_required(values, "entries", place), f"{place}.entries")
    values["entries"] = [
        _entry(entry, f"{place}.entries[{index}]")
        for index, entry in enumerate(entry_values)
    ]
    stop_issue_values = _items(values.get("stop_issues", []), f"{place}.stop_issues")
    values["stop_issues"] = [
        _orchestration_stop_issue(issue, f"{place}.stop_issues[{index}]")
        for index, issue in enumerate(stop_issue_values)
    ]
    return cast(_LegacyShutdownRecord, cast(object, values))


def _read_record(path: Path) -> _LegacyShutdownRecord:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise InvalidRecord(f"{path} cannot be read: {error}") from error
    try:
        value = cast(object, json.loads(text))
    except (ValueError, TypeError) as error:
        raise InvalidRecord(f"{path} is not valid JSON") from error
    changed = _worker_record_names(value)
    checked = _record(value, str(path))
    if changed:
        _write_record(path, checked)
    return checked


def _entry_kind(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "a regular file"
    if stat.S_ISDIR(mode):
        return "a directory"
    if stat.S_ISLNK(mode):
        return "a symbolic link"
    if stat.S_ISFIFO(mode):
        return "a FIFO"
    if stat.S_ISSOCK(mode):
        return "a socket"
    if stat.S_ISCHR(mode):
        return "a character device"
    if stat.S_ISBLK(mode):
        return "a block device"
    return "an unknown filesystem entry"


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise InvalidRecord(f"{path} cannot be inspected: {error}") from error


def _write_record(path: Path, record: object) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".record.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(record, output, indent=2, sort_keys=True)
            _ = output.write("\n")
            output.flush()
            _ = os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _existing_account_directory(login: str) -> Path | None:
    directory = _account_directory(login)
    directory_status = _lstat(directory)
    if directory_status is None:
        return None
    if not stat.S_ISDIR(directory_status.st_mode):
        raise InvalidRecord(
            f"{directory} must be a directory; found {_entry_kind(directory_status.st_mode)}"
        )
    return directory


@contextmanager
def _reading_lock(login: str, directory: Path) -> Generator[None, None, None]:
    """Hold the account lock for a read; a lock that cannot be taken is an unreadable record."""
    del login
    account_lock = _directory_lock(directory)
    try:
        _ = account_lock.__enter__()
    except OSError as error:
        lock_path = directory / "lock"
        raise InvalidRecord(
            f"{lock_path} cannot be opened or locked: {error}"
        ) from error
    try:
        lock_path = directory / "lock"
        if not _regular_record(lock_path):
            yield
        else:
            try:
                lock_file = lock_path.open("r+", encoding="utf-8")
            except OSError as error:
                raise InvalidRecord(f"{lock_path} cannot be opened or locked: {error}") from error
            with lock_file:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                except OSError as error:
                    raise InvalidRecord(f"{lock_path} cannot be opened or locked: {error}") from error
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        _ = account_lock.__exit__(None, None, None)


def _slot_record(directory: Path) -> _LegacyShutdownRecord | None:
    path = directory / "record.json"
    record_status = _lstat(path)
    if record_status is None:
        return None
    if not stat.S_ISREG(record_status.st_mode):
        raise InvalidRecord(
            f"{path} must be a regular file; found {_entry_kind(record_status.st_mode)}"
        )
    return _read_record(path)


def _legacy_partial_records(directory: Path) -> list[_LegacyShutdownRecord]:
    partial_directory = directory / "partial"
    partial_status = _lstat(partial_directory)
    if partial_status is None:
        return []
    if not stat.S_ISDIR(partial_status.st_mode):
        raise InvalidRecord(
            f"{partial_directory} must be a directory; found {_entry_kind(partial_status.st_mode)}"
        )
    records: list[_LegacyShutdownRecord] = []
    for path in partial_directory.glob("*.json"):
        path_status = _lstat(path)
        if path_status is None:
            continue
        if not stat.S_ISREG(path_status.st_mode):
            raise InvalidRecord(
                f"{path} must be a regular file; found {_entry_kind(path_status.st_mode)}"
            )
        records.append(_read_record(path))
    return sorted(records, key=lambda record: record["requested_at"])


def _shutdown_request_fields(values: dict[str, object], place: str) -> None:
    login = _string(_required(values, "login", place), f"{place}.login")
    try:
        _ = _safe_login(login)
    except ValueError as error:
        raise InvalidRecord(f"{place}.login is unsafe for record storage") from error
    for field in ("label", "machine"):
        _ = _string(_required(values, field, place), f"{place}.{field}")
    values["label"] = canonical_label(cast(str, values["label"]))
    _ = _utc_time(_required(values, "requested_at", place), f"{place}.requested_at")
    request_id = _string(_required(values, "request_id", place), f"{place}.request_id")
    if not request_id:
        raise InvalidRecord(f"{place}.request_id must not be empty")
    if "move_id" in values:
        move_id = _string(values["move_id"], f"{place}.move_id")
        if not move_id:
            raise InvalidRecord(f"{place}.move_id must not be empty")
    values["requested_by"] = _request_origin(
        _required(values, "requested_by", place), f"{place}.requested_by"
    )
    action = _string(_required(values, "then", place), f"{place}.then")
    if action not in {"stop", "restart"}:
        raise InvalidRecord(f"{place}.then is invalid")


def _paused_seat(value: object, place: str) -> PausedSeat:
    fields = _mapping(value, place)
    return PausedSeat(
        run_dir=_string(_required(fields, "run_dir", place), f"{place}.run_dir"),
        seat=_string(_required(fields, "seat", place), f"{place}.seat"),
        role=_string(_required(fields, "role", place), f"{place}.role"),
        thread_id=_string(_required(fields, "thread_id", place), f"{place}.thread_id"),
        turn_live=_boolean(_required(fields, "turn_live", place), f"{place}.turn_live"),
        launcher_command=_strings(fields.get("launcher_command", []), f"{place}.launcher_command"),
    )


def _session_shutdown(value: object, place: str) -> SessionShutdown:
    values = _mapping(value, place)
    _shutdown_request_fields(values, place)
    for field in ("source_label",):
        if isinstance(values.get(field), str):
            values[field] = canonical_label(cast(str, values[field]))
    if "target" in values:
        target = _string(values["target"], f"{place}.target")
        if target in ("default", "~/.claude", str(Path.home() / ".claude")):
            resolver = cast(HistoricalAccountResolver, cast(object, import_module("scripts.accounts.accounts")))
            resolved = resolver.resolve_history_label(target, _string(values["requested_at"], f"{place}.requested_at"))
            target = resolved.label if isinstance(resolved, ResolvedHistoryLabel) else "~/.claude"
        parsed = parse_account_label(target)
        if target != "~/.claude" and not (isinstance(parsed, NumberedAccountLabel) and parsed.tool == "claude"):
            raise InvalidRecord(f"{place}.target must name a numbered Claude account")
        values["target"] = canonical_label(target)
    for field in ("source_label", "why", "renamed", "cut_off"):
        if field in values:
            _ = _string(values[field], f"{place}.{field}")
    if "checkout_move" in values:
        move = _mapping(values["checkout_move"], f"{place}.checkout_move")
        values["checkout_move"] = {key: _string(_required(move, key, f"{place}.checkout_move"), f"{place}.checkout_move.{key}")
                                   for key in ("old", "new")}
    for field, allowed in (
        ("hold", {"account", "session"}),
        ("force", {"wait for ready", "now"}),
        ("alerts", {"user", "none"}),
    ):
        text = _string(_required(values, field, place), f"{place}.{field}")
        if text not in allowed:
            raise InvalidRecord(f"{place}.{field} is invalid")
    probe = values.get("probe", False)
    if not isinstance(probe, bool):
        raise InvalidRecord(f"{place}.probe must be a boolean")
    values["probe"] = probe
    if "codex_seats" in values:
        values["codex_seats"] = [
            _paused_seat(seat, f"{place}.codex_seats[{index}]")
            for index, seat in enumerate(_items(values["codex_seats"], f"{place}.codex_seats"))
        ]
    _ = _required(values, "stop_issues", place)
    fields = _entry(values, place)
    values["progress"] = _progress(values["progress"], f"{place}.progress")
    trail: list[ProgressStep] = []
    for index, step in enumerate(_items(values.get("trail", []), f"{place}.trail")):
        step_place = f"{place}.trail[{index}]"
        fields_step = _mapping(step, step_place)
        at = _utc_time(_required(fields_step, "at", step_place), f"{step_place}.at")
        kind = _string(_required(fields_step, "kind", step_place), f"{step_place}.kind")
        detail = _string(_required(fields_step, "detail", step_place), f"{step_place}.detail")
        trail.append(ProgressStep(at=at, kind=kind, detail=detail))
    values["trail"] = trail
    values["stop_issues"] = [_session_stop_issue(issue, f"{place}.stop_issues") for issue in fields["stop_issues"]]
    if "restart_issues" in values:
        values["restart_issues"] = _strings(values["restart_issues"], f"{place}.restart_issues")
    if fields["session"]["kind"] == "worker":
        raise InvalidRecord(f"{place}.session must not be a worker")
    _ = _session_filename(fields["session"]["session_id"])
    workers = _items(_required(values, "workers", place), f"{place}.workers")
    parsed_workers: list[Session] = []
    for index, worker in enumerate(workers):
        worker_place = f"{place}.workers[{index}]"
        try:
            parsed = parse_session(worker, worker_place)
        except ValueError as error:
            raise InvalidRecord(str(error)) from error
        if parsed["kind"] != "worker":
            raise InvalidRecord(f"{worker_place} must be a worker")
        owner = parsed["owner"]
        if owner["kind"] != "director" or owner["session_id"] != fields["session"]["session_id"]:
            raise InvalidRecord(f"{worker_place}.owner must identify this session")
        parsed_workers.append(parsed)
    values["workers"] = parsed_workers
    for field in ("alerted_at", "form_alerted_at", "reasked_at", "stop_deadline"):
        if field in values:
            _ = _utc_time(values[field], f"{place}.{field}")
    for field in ("restore_alerted", "showrunner_told", "force_stop_note"):
        if field in values:
            _ = _string(values[field], f"{place}.{field}")
    return cast(SessionShutdown, cast(object, values))


def _account_hold(value: object, place: str) -> AccountHold:
    values = _mapping(value, place)
    _shutdown_request_fields(values, place)
    if "registration" in values and values["registration"] not in ("in progress", "complete"):
        raise InvalidRecord(f"{place}.registration must be in progress or complete")
    request_ids = {cast(str, values["request_id"])}
    if "request_ids" in values:
        ids = _strings(values["request_ids"], f"{place}.request_ids")
        if not all(ids) or len(set(ids)) != len(ids) or not request_ids.issubset(ids):
            raise InvalidRecord(f"{place}.request_ids must be unique nonempty ids including request_id")
        request_ids = set(ids)
    if "registering_request_ids" in values:
        ids = _strings(values["registering_request_ids"], f"{place}.registering_request_ids")
        if len(set(ids)) != len(ids) or not set(ids).issubset(request_ids):
            raise InvalidRecord(f"{place}.registering_request_ids must be unique ids from request_ids")
    return cast(AccountHold, cast(object, values))


def _session_filename(session_id: str) -> str:
    if not session_id:
        raise InvalidRecord("session id must not be empty")
    return re.sub(r"[^A-Za-z0-9_.-]", "-", session_id) + ".json"


def _regular_record(path: Path) -> bool:
    status = _lstat(path)
    if status is None:
        return False
    if not stat.S_ISREG(status.st_mode):
        raise InvalidRecord(f"{path} must be a regular file; found {_entry_kind(status.st_mode)}")
    return True


def _record_directory(path: Path, *, create: bool = False) -> bool:
    status = _lstat(path)
    if status is None:
        if not create:
            return False
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise InvalidRecord(f"{path} cannot be created: {error}") from error
        status = _lstat(path)
    if status is None or not stat.S_ISDIR(status.st_mode):
        raise InvalidRecord(f"{path} must be a directory")
    return True


def _worker_record_names(value: object) -> bool:
    """Convert stored worker fields and session kinds before validation."""
    changed = False
    if isinstance(value, dict):
        fields = cast(dict[str, object], value)
        for old, new in (("busy_seats", "busy_workers"), ("seats", "workers")):
            if old in fields:
                if new not in fields:
                    fields[new] = fields[old]
                del fields[old]
                changed = True
        labels = {"seat": "worker",
                  "passive seat ready": "passive worker ready",
                  "seat available on demand": "worker available on demand",
                  "seat still live": "worker still live"}
        for name in ("kind", "progress"):
            label = fields.get(name)
            if isinstance(label, str) and label in labels:
                fields[name] = labels[label]
                changed = True
        for item in fields.values():
            changed = _worker_record_names(item) or changed
    elif isinstance(value, list):
        for item in cast(list[object], value):
            changed = _worker_record_names(item) or changed
    return changed


def _read_json(path: Path) -> object:
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
        if _worker_record_names(value):
            _write_record(path, value)
        return value
    except (OSError, UnicodeError, ValueError) as error:
        raise InvalidRecord(f"{path} cannot be read: {error}") from error


def _check_account(login: str, stored_login: str, path: Path) -> None:
    if login.casefold() != stored_login.casefold():
        raise InvalidRecord(f"{path}.login does not match its account directory")


@contextmanager
def _session_store_lock(login: str, *, create: bool = False, write: bool = False) -> Generator[Path, None, None]:
    directory = _account_directory(login)
    _ = _record_directory(directory, create=create)
    _ = _regular_record(directory / "lock")
    if create or write:
        with _account_lock(login, create_directory=create):
            yield directory
    else:
        with _reading_lock(login, directory):
            yield directory


def _legacy_account_records(directory: Path, login: str) -> list[_LegacyShutdownRecord]:
    """Read the validated active file, then partial files newest first."""
    partial = directory / "partial"
    if _record_directory(partial):
        try:
            _ = list(partial.iterdir())
        except OSError as error:
            raise InvalidRecord(f"{partial} cannot be read: {error}") from error
    slot = _slot_record(directory)
    records = [*([] if slot is None else [slot]), *reversed(_legacy_partial_records(directory))]
    for record in records:
        _check_account(login, record["login"], directory)
    return records


def _convert_session(record: _LegacyShutdownRecord, entry: _LegacySessionEntry) -> SessionShutdown:
    session = entry["session"]
    progress = entry["progress"]
    if progress["kind"] == "passive worker ready" or progress["kind"] == "worker available on demand":
        progress = SessionStopped(kind="stopped", at=progress["at"])
    if record["state"] == "restarting" and progress["kind"] != "restarted":
        progress = SessionStopped(kind="stopped", at=progress.get("at", record["requested_at"]))
    workers: list[Session] = []
    for candidate in record["entries"]:
        worker = candidate["session"]
        if worker["kind"] == "worker" and worker["owner"]["kind"] == "director":
            if worker["owner"]["session_id"] == session["session_id"]:
                workers.append(worker)
    return SessionShutdown(
        login=record["login"], label=record["label"], machine=record["machine"],
        requested_at=record["requested_at"], requested_by=record["requested_by"],
        request_id=f"legacy-{record['machine']}-{record['requested_at']}",
        hold="account" if record["scope"]["kind"] == "all account sessions" else "session",
        then=_legacy_continuation(record), force=record["force"], alerts="user", probe=False,
        session=session, workers=workers, timers=entry["timers"],
        settle_message=entry["settle_message"], where=entry["where"],
        progress=cast(SessionProgress, cast(object, progress)), trail=[], stop_issues=[issue for issue in entry["stop_issues"] if issue["kind"] != "worker still live"],
    )


def _session_records_in_directory(directory: Path, login: str) -> list[SessionShutdown]:
    records: list[SessionShutdown] = []
    sessions = directory / "sessions"
    if _record_directory(sessions):
        try:
            paths = sorted(path for path in sessions.iterdir() if path.name.endswith(".json"))
        except OSError as error:
            raise InvalidRecord(f"{sessions} cannot be read: {error}") from error
        for path in paths:
            if not _regular_record(path):
                continue
            record = _session_shutdown(_read_json(path), str(path))
            _check_account(login, record["login"], path)
            if path.name != _session_filename(record["session"]["session_id"]):
                raise InvalidRecord(f"{path} does not match its session id")
            records.append(record)
    stored_ids = {record["session"]["session_id"] for record in records}
    for old in _legacy_account_records(directory, login):
        if old["state"] in {"cancelled", "up"}:
            continue
        for entry in old["entries"]:
            session = entry["session"]
            if session["kind"] != "worker" and session["session_id"] not in stored_ids:
                records.append(_convert_session(old, entry))
                stored_ids.add(session["session_id"])
    return sorted(records, key=lambda item: (item["requested_at"], item["session"]["name"]))


def session_records(login: str) -> list[SessionShutdown]:
    """Read session files and old entries without migrating them on disk."""
    if _existing_account_directory(login) is None:
        return []
    with _session_store_lock(login) as directory:
        return _session_records_in_directory(directory, login)


def all_session_records() -> list[SessionShutdown]:
    """Read all accounts, ordered by request time and session name."""
    root = _state_root()
    if not _record_directory(root):
        return []
    try:
        directories = sorted(root.iterdir())
    except OSError as error:
        raise InvalidRecord(f"{root} cannot be read: {error}") from error
    records: list[SessionShutdown] = []
    for directory in directories:
        if _is_root_metadata(directory.name):
            _ = _regular_record(directory)
            continue
        records.extend(session_records(directory.name))
    return sorted(records, key=lambda item: (item["requested_at"], item["session"]["name"]))


def find_session(login: str, session_id: str) -> SessionShutdown | None:
    """Find this session's record, including a converted old entry."""
    return next((item for item in session_records(login) if item["session"]["session_id"] == session_id), None)


def _progress_step(record: SessionShutdown, at: str) -> ProgressStep:
    progress = record["progress"]
    detail = ""
    if progress["kind"] == "reaping":
        detail = progress["because"] + (": " + progress["detail"] if progress["detail"] else "")
    elif progress["kind"] == "stop failed" or progress["kind"] == "restart failed":
        detail = progress["reason"]
    elif progress["kind"] == "manual restart":
        detail = progress["command"]
    elif progress["kind"] == "timers pending":
        detail = "; ".join(timer["instance"] + ": " + timer["reason"] for timer in progress["timers"])
    elif progress["kind"] == "ready" and record["where"]["kind"] == "said":
        detail = record["where"]["text"]
    return ProgressStep(at=at, kind=progress["kind"], detail=detail)


def create_session(record: SessionShutdown) -> bool:
    """Create one session record while excluding competing launch decisions."""
    checked = _session_shutdown(record, "record")
    if not checked["trail"]:
        checked["trail"].append(_progress_step(
            checked, checked["progress"].get("at", checked["requested_at"])))
    with launch_barrier():
        with _session_store_lock(checked["login"], create=True) as directory:
            existing = _session_records_in_directory(directory, checked["login"])
            previous = next((item for item in existing
                             if item["session"]["session_id"] == checked["session"]["session_id"]), None)
            if previous is not None:
                if not finished_stop_request(previous):
                    return False
                history = directory / "history"
                _ = _record_directory(history, create=True)
                old = directory / "sessions" / _session_filename(previous["session"]["session_id"])
                target = history / f"{previous['requested_at']}-{previous['request_id']}-{old.name}"
                # A repeated request id must not replace its earlier history either.
                while _lstat(target) is not None:
                    target = target.with_name("old-" + target.name)
                os.replace(old, target)
            sessions = directory / "sessions"
            _ = _record_directory(sessions, create=True)
            path = sessions / _session_filename(checked["session"]["session_id"])
            if _regular_record(path):
                raise InvalidRecord(f"{path} is used by a different session id")
            _write_record(path, checked)
    return True


def finished_stop_request(saved: SessionShutdown) -> bool:
    """Only completed shutdowns can be replaced by a fresh live-session snapshot."""
    return saved["progress"]["kind"] in {
        "stopped", "restarted", "restart failed", "already gone", "process identity lost",
        "manual restart",
    }


def request_matches_process(saved: SessionShutdown, live: Session) -> bool:
    stored = saved["session"]
    return (stored["pid"] == live["pid"] and bool(stored["proc_start"])
            and stored["proc_start"] == live["proc_start"])


def update_session(
    login: str, session_id: str, change: Callable[[SessionShutdown], None]
) -> SessionShutdown:
    """Update one new-format record without changing its identity."""
    if _existing_account_directory(login) is None:
        raise NoLiveRecord(f"no session shutdown for {login}/{session_id}")
    with _session_store_lock(login, write=True) as directory:
        _ = _record_directory(directory / "sessions")
        path = directory / "sessions" / _session_filename(session_id)
        if not _regular_record(path):
            raise NoLiveRecord(f"no session shutdown for {login}/{session_id}")
        record = _session_shutdown(_read_json(path), str(path))
        _check_account(login, record["login"], path)
        if record["session"]["session_id"] != session_id:
            raise InvalidRecord(f"{path} is used by a different session id")
        identity = record["login"], record["machine"], record["session"]["session_id"]
        prior_progress = copy.deepcopy(record["progress"])
        trail = copy.deepcopy(record["trail"])
        change(record)
        record["trail"] = trail
        if record["progress"] != prior_progress:
            at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            record["trail"].append(_progress_step(record, at))
        checked = _session_shutdown(record, str(path))
        if (checked["login"], checked["machine"], checked["session"]["session_id"]) != identity:
            raise ValueError("a session record update cannot change its login, machine or session id")
        _write_record(path, checked)
        return checked


def claim_restore_alert(saved: SessionShutdown, detail: str) -> bool:
    """Claim one failure alert for this request under its account lock."""
    claimed = False
    def claim(current: SessionShutdown) -> None:
        nonlocal claimed
        if (current["request_id"] == saved["request_id"]
                and current["requested_at"] == saved["requested_at"]
                and "restore_alerted" not in current):
            current["restore_alerted"] = detail
            claimed = True
    try:
        _ = update_session(saved["login"], saved["session"]["session_id"], claim)
    except NoLiveRecord:
        return False
    return claimed


def clear_restore_alert(saved: SessionShutdown) -> None:
    """Release this request's alert after failed delivery or a live observation."""
    def clear(current: SessionShutdown) -> None:
        if (current["request_id"] == saved["request_id"]
                and current["requested_at"] == saved["requested_at"]):
            _ = current.pop("restore_alerted", None)
    try:
        _ = update_session(saved["login"], saved["session"]["session_id"], clear)
    except NoLiveRecord:
        pass


def archive_session(login: str, session_id: str) -> None:
    """Move one session file into history under its request time and name."""
    if _existing_account_directory(login) is None:
        raise NoLiveRecord(f"no session shutdown for {login}/{session_id}")
    with _session_store_lock(login, write=True) as directory:
        _ = _record_directory(directory / "sessions")
        path = directory / "sessions" / _session_filename(session_id)
        if not _regular_record(path):
            raise NoLiveRecord(f"no session shutdown for {login}/{session_id}")
        record = _session_shutdown(_read_json(path), str(path))
        _check_account(login, record["login"], path)
        if record["session"]["session_id"] != session_id:
            raise InvalidRecord(f"{path} is used by a different session id")
        history = directory / "history"
        _ = _record_directory(history, create=True)
        target = history / f"{record['requested_at']}-{path.name}"
        if _lstat(target) is not None:
            raise InvalidRecord(f"{target} already exists")
        os.replace(path, target)


def taken_over(saved: SessionShutdown) -> bool:
    """Whether a later request on any account holds this session, live or archived (a stop, a forget, a new move)."""
    root = _state_root()
    if not _record_directory(root):
        return False
    since = datetime.fromisoformat(saved["requested_at"])
    session_id = saved["session"]["session_id"]
    name = _session_filename(session_id)

    def later(requested_at: str) -> bool:
        try:
            return datetime.fromisoformat(requested_at) > since
        except ValueError:
            return False

    if any(item["session"]["session_id"] == session_id and later(item["requested_at"]) for item in all_session_records()):
        return True
    for directory in root.iterdir():
        history = directory / "history"
        try:
            if _is_root_metadata(directory.name) or not _record_directory(directory) or not _record_directory(history):
                continue
            names = [path.name for path in history.iterdir()]
        except (OSError, ValueError):
            continue
        if any(entry.endswith("-" + name) and later(entry[:-len(name) - 1]) for entry in names):
            return True
    return False


def restarted_history() -> list[SessionShutdown]:
    """Read completed per-session history once, ignoring each unreadable entry.

    Archived target labels are evidence, not requests to resolve today's accounts.
    """
    root = _state_root()
    if not _record_directory(root):
        return []
    found: list[SessionShutdown] = []
    for directory in root.iterdir():
        try:
            if directory.name == "snapshots" or not _record_directory(directory):
                continue
            history = directory / "history"
            if not _record_directory(history):
                continue
            paths = list(history.iterdir())
        except (OSError, ValueError):
            continue
        for path in paths:
            try:
                if path.suffix != ".json" or not _regular_record(path):
                    continue
                values = _mapping(cast(object, json.loads(path.read_text(encoding="utf-8"))), str(path))
                _ = _worker_record_names(values)
                target = values.pop("target", None)
                saved = _session_shutdown(values, str(path))
                _check_account(directory.name, saved["login"], path)
                if target is not None:
                    saved["target"] = canonical_label(_string(target, f"{path}.target"))
                if saved["progress"]["kind"] == "restarted":
                    found.append(saved)
            except (OSError, UnicodeError, ValueError):
                continue
    return found


def account_hold(login: str) -> AccountHold | None:
    """Read an explicit hold or an old account-wide record in a held state."""
    if _existing_account_directory(login) is None:
        return None
    with _session_store_lock(login) as directory:
        path = directory / "hold.json"
        old = _legacy_account_records(directory, login)
        if _regular_record(path):
            hold = _account_hold(_read_json(path), str(path))
            _check_account(login, hold["login"], path)
            return hold
        return _legacy_hold(old)


def _legacy_hold(old: list[_LegacyShutdownRecord]) -> AccountHold | None:
    for record in sorted(old, key=lambda item: item["requested_at"]):
        if record["scope"]["kind"] == "all account sessions" and record["state"] not in {"cancelled", "up"}:
            return AccountHold(
                login=record["login"], label=record["label"], machine=record["machine"],
                requested_at=record["requested_at"], requested_by=record["requested_by"],
                request_id=f"legacy-{record['machine']}-{record['requested_at']}",
                then=_legacy_continuation(record),
            )
    return None


def all_account_holds() -> list[AccountHold]:
    """Read every account hold, ordered by request time and label."""
    root = _state_root()
    if not _record_directory(root):
        return []
    try:
        directories = sorted(root.iterdir())
    except OSError as error:
        raise InvalidRecord(f"{root} cannot be read: {error}") from error
    holds: list[AccountHold] = []
    for directory in directories:
        if _is_root_metadata(directory.name):
            _ = _regular_record(directory)
            continue
        hold = account_hold(directory.name)
        if hold is not None:
            holds.append(hold)
    return sorted(holds, key=lambda hold: (hold["requested_at"], hold["label"]))


def set_account_hold(hold: AccountHold) -> None:
    """Persist the account-wide launch hold under the launch barrier."""
    checked = _account_hold(hold, "hold")
    with launch_barrier():
        with _session_store_lock(checked["login"], create=True) as directory:
            path = directory / "hold.json"
            if _regular_record(path):
                existing = _account_hold(_read_json(path), str(path))
                _check_account(checked["login"], existing["login"], path)
            _write_record(path, checked)


def _hold_request_ids(hold: AccountHold) -> set[str]:
    return set(hold.get("request_ids", [hold["request_id"]]))


def _registering_request_ids(hold: AccountHold) -> set[str]:
    pending = set(hold.get("registering_request_ids", []))
    if hold.get("registration", "complete") == "in progress":
        pending.add(hold["request_id"])
    return pending


def register_account_hold_request(hold: AccountHold, *, account_wide: bool) -> None:
    """Join an existing hold, or create an account-wide hold, before adding records."""
    checked = _account_hold(copy.deepcopy(hold), "hold")
    login, request_id = checked["login"], checked["request_id"]
    with launch_barrier():
        with _session_store_lock(login, create=True) as directory:
            path = directory / "hold.json"
            if _regular_record(path):
                checked = _account_hold(_read_json(path), str(path))
                _check_account(login, checked["login"], path)
            elif not account_wide:
                return
            requests = _hold_request_ids(checked)
            requests.update(saved["request_id"] for saved in _session_records_in_directory(directory, login))
            requests.add(request_id)
            pending = _registering_request_ids(checked)
            pending.add(request_id)
            checked["request_ids"] = sorted(requests)
            checked["registering_request_ids"] = sorted(pending)
            _write_record(path, checked)


def complete_account_hold_registration(login: str, request_id: str) -> None:
    """Finish only this request's registration, leaving overlapping requests held."""
    if _existing_account_directory(login) is None:
        return
    with _session_store_lock(login, write=True) as directory:
        path = directory / "hold.json"
        if not _regular_record(path):
            return
        held = _account_hold(_read_json(path), str(path))
        _check_account(login, held["login"], path)
        pending = _registering_request_ids(held)
        if request_id in pending:
            pending.remove(request_id)
            held["registering_request_ids"] = sorted(pending)
            if held["request_id"] == request_id and "registration" in held:
                held["registration"] = "complete"
            _write_record(path, held)


def clear_account_hold(login: str) -> None:
    """Clear the explicit hold; old slot records are unchanged."""
    if _existing_account_directory(login) is None:
        return
    with _session_store_lock(login, write=True) as directory:
        path = directory / "hold.json"
        if _regular_record(path):
            hold = _account_hold(_read_json(path), str(path))
            _check_account(login, hold["login"], path)
            path.unlink()


def parse_session_records(text: str) -> list[SessionShutdown]:
    """Decode and validate a JSON list of per-session shutdown records."""
    try:
        value = cast(object, json.loads(text))
    except (ValueError, TypeError) as error:
        raise InvalidRecord("session records is not valid JSON") from error
    _ = _worker_record_names(value)
    return [
        _session_shutdown(item, f"records[{index}]")
        for index, item in enumerate(_items(value, "records"))
    ]


def parse_account_holds(text: str) -> list[AccountHold]:
    """Decode and validate a JSON list of account holds."""
    try:
        value = cast(object, json.loads(text))
    except (ValueError, TypeError) as error:
        raise InvalidRecord("account holds is not valid JSON") from error
    return [
        _account_hold(item, f"holds[{index}]")
        for index, item in enumerate(_items(value, "holds"))
    ]


def migrate(login: str) -> list[str]:
    """Convert validated old files into session records and preserve their hold."""
    if _existing_account_directory(login) is None:
        return []
    with _session_store_lock(login, write=True) as directory:
        converted = _session_records_in_directory(directory, login)
        old = _legacy_account_records(directory, login)
        paths = [directory / "record.json"]
        partial = directory / "partial"
        if _record_directory(partial):
            paths.extend(sorted(partial.glob("*.json")))
        sources = [(path, _read_record(path)) for path in paths if _regular_record(path)]
        if not sources:
            return []
        hold_path = directory / "hold.json"
        if _regular_record(hold_path):
            hold = _account_hold(_read_json(hold_path), str(hold_path))
            _check_account(login, hold["login"], hold_path)
        history = directory / "history"
        _ = _record_directory(history)
        targets = [history / f"{saved['requested_at']}-{path.name}" for path, saved in sources]
        if len(set(targets)) != len(targets):
            raise InvalidRecord(f"{history} has conflicting legacy history names")
        for target in targets:
            if _lstat(target) is not None:
                raise InvalidRecord(f"{target} already exists")
        sessions = directory / "sessions"
        new_records: list[SessionShutdown] = []
        for saved in converted:
            session_id = saved["session"]["session_id"]
            path = sessions / _session_filename(session_id)
            if not _regular_record(path):
                new_records.append(saved)
                continue
            existing = _session_shutdown(_read_json(path), str(path))
            existing_id = existing["session"]["session_id"]
            if existing_id != session_id:
                raise InvalidRecord(f"{path} has conflicting session ids {existing_id!r} and {session_id!r}")
        names = [_session_filename(saved["session"]["session_id"]) for saved in new_records]
        if len(set(names)) != len(names):
            raise InvalidRecord(f"{sessions} has conflicting session names")
        _ = _record_directory(history, create=True)
        if new_records:
            _ = _record_directory(sessions, create=True)
            for saved in new_records:
                _write_record(sessions / _session_filename(saved["session"]["session_id"]), saved)
        if not _regular_record(hold_path):
            held = next((saved for saved in sorted(old, key=lambda item: item["requested_at"])
                         if saved["scope"]["kind"] == "all account sessions"
                         and saved["state"] not in {"cancelled", "up"}), None)
            if held is not None:
                _write_record(hold_path, AccountHold(
                    login=held["login"], label=held["label"], machine=held["machine"],
                    requested_at=held["requested_at"], requested_by=held["requested_by"],
                    request_id=f"legacy-{held['machine']}-{held['requested_at']}",
                    then=_legacy_continuation(held),
                ))
        lines: list[str] = []
        for (source, saved), target in zip(sources, targets):
            os.replace(source, target)
            when = datetime.fromisoformat(saved["requested_at"]).astimezone(
                user_zone()
            ).strftime("%Y-%m-%d %H:%M %Z")
            lines.append(f"shutdown: converted the {saved['state']} record of {saved['label']} from {when}")
        return lines


@dataclass(frozen=True)
class ShutdownFound:
    record: SessionShutdown


@dataclass(frozen=True)
class NoShutdown:
    """This session has no shutdown request."""


@dataclass(frozen=True)
class AccountHeld:
    hold: AccountHold


@dataclass(frozen=True)
class AccountOpen:
    """No account-wide launch hold is present."""


def lookup_session(login: str, session_id: str) -> ShutdownFound | NoShutdown:
    saved = find_session(login, session_id)
    return NoShutdown() if saved is None else ShutdownFound(saved)


def lookup_account_hold(login: str) -> AccountHeld | AccountOpen:
    held = account_hold(login)
    return AccountOpen() if held is None else AccountHeld(held)


@dataclass(frozen=True)
class SessionWithdrawn:
    record: SessionShutdown


@dataclass(frozen=True)
class SessionRetained:
    record: SessionShutdown


@dataclass(frozen=True)
class SessionForgotten:
    record: SessionShutdown


def forget_session(login: str, session_id: str, machine: str) -> SessionForgotten | SessionRetained | NoShutdown:
    """Archive an abandoned restore, rechecking its progress under the account lock."""
    if _existing_account_directory(login) is None:
        return NoShutdown()
    # Preserve legacy holds and retire old copies so they cannot reappear after archive.
    _ = migrate(login)
    with _session_store_lock(login, write=True) as directory:
        path = directory / "sessions" / _session_filename(session_id)
        if not _regular_record(path):
            return NoShutdown()
        saved = _session_shutdown(_read_json(path), str(path))
        _check_account(login, saved["login"], path)
        if saved["session"]["session_id"] != session_id:
            raise InvalidRecord(f"{path} is used by a different session id")
        if saved["machine"].casefold() != machine.casefold() or saved["progress"]["kind"] not in {
            "stopped", "already gone", "process identity lost", "restart failed", "manual restart",
        }:
            return SessionRetained(saved)
        history = directory / "history"
        _ = _record_directory(history, create=True)
        target = history / f"{saved['requested_at']}-{path.name}"
        if _lstat(target) is not None:
            raise InvalidRecord(f"{target} already exists")
        os.replace(path, target)
        return SessionForgotten(saved)


def forget_failed_restart(
    login: str, session_id: str, machine: str, expected_request_id: str,
) -> SessionForgotten | SessionRetained | NoShutdown:
    """Archive only the expected request's failed restart under the account lock."""
    if _existing_account_directory(login) is None:
        return NoShutdown()
    _ = migrate(login)
    with _session_store_lock(login, write=True) as directory:
        path = directory / "sessions" / _session_filename(session_id)
        if not _regular_record(path):
            return NoShutdown()
        saved = _session_shutdown(_read_json(path), str(path))
        _check_account(login, saved["login"], path)
        if saved["session"]["session_id"] != session_id:
            raise InvalidRecord(f"{path} is used by a different session id")
        if (saved["machine"].casefold() != machine.casefold()
                or saved["request_id"] != expected_request_id
                or saved["progress"]["kind"] != "restart failed"):
            return SessionRetained(saved)
        history = directory / "history"
        _ = _record_directory(history, create=True)
        target = history / f"{saved['requested_at']}-{path.name}"
        if _lstat(target) is not None:
            raise InvalidRecord(f"{target} already exists")
        os.replace(path, target)
        return SessionForgotten(saved)


def withdraw_session(
    login: str, session_id: str, restore_and_notify: Callable[[SessionShutdown], None]
) -> SessionWithdrawn | SessionRetained:
    """Restore and notify under the lock before another request can send."""
    return _withdraw_session(login, session_id, lambda saved: False, restore_and_notify)


def withdraw_failed_stop(
    login: str, session_id: str, identity_check: Callable[[SessionShutdown], bool],
    restore_and_notify: Callable[[SessionShutdown], None],
) -> SessionWithdrawn | SessionRetained:
    """Withdraw a failed stop only when its original session is still live.

    The identity check, resource restore, notification and archive share the
    account lock, so a competing stop claim cannot advance between them.
    """
    return _withdraw_session(login, session_id, identity_check, restore_and_notify)


def _withdraw_session(
    login: str, session_id: str, identity_check: Callable[[SessionShutdown], bool],
    restore_and_notify: Callable[[SessionShutdown], None],
) -> SessionWithdrawn | SessionRetained:
    with _session_store_lock(login, write=True) as directory:
        path = directory / "sessions" / _session_filename(session_id)
        if not _regular_record(path):
            raise NoLiveRecord(f"no session shutdown for {login}/{session_id}")
        saved = _session_shutdown(_read_json(path), str(path))
        _check_account(login, saved["login"], path)
        if saved["session"]["session_id"] != session_id:
            raise InvalidRecord(f"{path} is used by a different session id")
        progress = saved["progress"]["kind"]
        if progress not in {"waiting", "ready", "held by usage limit", "reaping"} and not (
            progress == "stop failed" and identity_check(saved)
        ):
            return SessionRetained(saved)
        history = directory / "history"
        _ = _record_directory(history, create=True)
        target = history / f"{saved['requested_at']}-{path.name}"
        if _lstat(target) is not None:
            raise InvalidRecord(f"{target} already exists")
        restore_and_notify(saved)
        os.replace(path, target)
        return SessionWithdrawn(saved)


@dataclass(frozen=True)
class ConductorRunning:
    """A process currently owns the machine's conductor lock."""


@dataclass(frozen=True)
class ConductorNotRunning:
    """The lock is absent or available."""


@dataclass(frozen=True)
class ConductorUnavailable:
    reason: str


def clear_finished_account_hold(login: str, *, request_ids: tuple[str, ...] = ()) -> None:
    """Clear the owning request's hold once every covered request is finished.

    Filtered cleanup must name the owner. Joined requests add protection while
    registering or holding records; finishing one never authorizes release.
    """
    if _existing_account_directory(login) is None:
        return
    with _session_store_lock(login, write=True) as directory:
        path = directory / "hold.json"
        if not _regular_record(path):
            return
        held = _account_hold(_read_json(path), str(path))
        _check_account(login, held["login"], path)
        if _registering_request_ids(held):
            return
        covered = _hold_request_ids(held)
        if request_ids and held["request_id"] not in request_ids:
            return
        if not any(saved["request_id"] in covered
                   for saved in _session_records_in_directory(directory, login)):
            path.unlink()


@dataclass(frozen=True)
class HoldReleased:
    """A finished account hold was removed; `hold` is what was on disk."""

    hold: AccountHold


@dataclass(frozen=True)
class HoldInUse:
    """The account hold still covers work in progress, or is an old record's hold."""

    hold: AccountHold


def release_finished_account_hold(login: str, *, conducting: bool = False) -> HoldReleased | HoldInUse | AccountOpen:
    """Remove the account hold once its request has finished, so no hold outlives its shutdown.

    A hold is finished when its registration is complete and no record of a
    covered request is still live, waiting or stopping. A running conductor
    keeps the hold until each covered record meets `shutdown_finished`;
    `conducting` says the caller is that conductor. An old record's hold is
    never removed here.
    """
    if _existing_account_directory(login) is None:
        return AccountOpen()
    with _session_store_lock(login, write=True) as directory:
        path = directory / "hold.json"
        if not _regular_record(path):
            legacy = _legacy_hold(_legacy_account_records(directory, login))
            return AccountOpen() if legacy is None else HoldInUse(legacy)
        held = _account_hold(_read_json(path), str(path))
        _check_account(login, held["login"], path)
        if _registering_request_ids(held):
            return HoldInUse(held)
        covered = _hold_request_ids(held)
        records = [saved for saved in _session_records_in_directory(directory, login) if saved["request_id"] in covered]
        if conducting:
            finished = all(shutdown_finished(saved) for saved in records)
        else:
            finished = isinstance(conductor_status(login), ConductorNotRunning) and all(
                saved["progress"]["kind"] in _FINISHED_PROGRESS | _STOPPED_PROGRESS for saved in records)
        if not finished:
            return HoldInUse(held)
        path.unlink()
        return HoldReleased(held)


def conductor_status(login: str) -> ConductorRunning | ConductorNotRunning | ConductorUnavailable:
    """Observe ownership without creating a file or waiting for its owner."""
    try:
        directory = _existing_account_directory(login)
        if directory is None:
            return ConductorNotRunning()
        path = directory / "conduct.lock"
        if not _regular_record(path):
            return ConductorNotRunning()
        with path.open("r", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return ConductorRunning()
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return ConductorNotRunning()
    except (InvalidRecord, OSError, ValueError) as error:
        return ConductorUnavailable(str(error))


@dataclass(frozen=True)
class ConductorAcquired:
    """This process owns the account's conducting loop."""


@dataclass(frozen=True)
class ConductorAlreadyRunning:
    """Another process owns the account's conducting loop."""


@contextmanager
def conductor_lock(login: str) -> Generator[ConductorAcquired | ConductorAlreadyRunning, None, None]:
    """Allow brief ownership probes to finish before refusing a duplicate."""
    directory = _account_directory(login)
    _ = _record_directory(directory, create=True)
    path = directory / "conduct.lock"
    _ = _regular_record(path)
    with path.open("a+", encoding="utf-8") as lock:
        for delay in (*CONDUCTOR_LOCK_RETRY_DELAYS, 0.0):
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not delay:
                    yield ConductorAlreadyRunning()
                    return
                time.sleep(delay)
        try:
            yield ConductorAcquired()
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _snapshot_session(value: object, place: str) -> SnapshotSession:
    values = _mapping(value, place)
    agent = _string(_required(values, "agent", place), f"{place}.agent")
    if agent not in {"claude", "codex"}:
        raise InvalidRecord(f"{place}.agent is invalid")
    try:
        owner = parse_session(_required(values, "session", place), f"{place}.session")
        workers = [parse_session(worker, f"{place}.workers[{index}]") for index, worker in enumerate(
            _items(_required(values, "workers", place), f"{place}.workers"))]
    except ValueError as error:
        raise InvalidRecord(str(error)) from error
    if owner["kind"] == "worker":
        raise InvalidRecord(f"{place}.session must be an owner")
    for worker in workers:
        if (worker["kind"] != "worker" or worker["owner"]["kind"] != "director"
                or worker["owner"]["session_id"] != owner["session_id"]):
            raise InvalidRecord(f"{place}.workers must belong to this owner")
    values["session"], values["workers"] = owner, workers
    values["timers"] = [_timer(timer, f"{place}.timers") for timer in
                       _items(_required(values, "timers", place), f"{place}.timers")]
    _ = _utc_time(_required(values, "last_seen", place), f"{place}.last_seen")
    if "crash" in values and values["crash"] not in {"reboot", "host gone", "agent exited"}:
        raise InvalidRecord(f"{place}.crash is invalid")
    if "boot_id" in values:
        if not _string(values["boot_id"], f"{place}.boot_id"):
            raise InvalidRecord(f"{place}.boot_id must not be empty")
    if "host_process" in values:
        identity = _mapping(values["host_process"], f"{place}.host_process")
        if identity.get("kind") == "process":
            if (not isinstance(identity.get("pid"), int) or isinstance(identity.get("pid"), bool)
                    or cast(int, identity["pid"]) <= 0):
                raise InvalidRecord(f"{place}.host_process.pid must be positive")
            if not _string(_required(identity, "proc_start", place), f"{place}.host_process.proc_start"):
                raise InvalidRecord(f"{place}.host_process.proc_start must not be empty")
        elif identity.get("kind") != "not recorded":
            raise InvalidRecord(f"{place}.host_process.kind is invalid")
    if _string(_required(values, "state", place), f"{place}.state") not in {"running", "unresolved"}:
        raise InvalidRecord(f"{place}.state is invalid")
    if agent == "codex":
        if owner["kind"] != "top-level" or workers:
            raise InvalidRecord(f"{place}: Codex standalones must be top-level without workers")
        values["codex"] = _codex_launch(_required(values, "codex", place), f"{place}.codex")
    elif "codex" in values:
        raise InvalidRecord(f"{place}: a Claude snapshot must not carry Codex launch metadata")
    return cast(SnapshotSession, cast(object, values))


def parse_snapshot(value: object) -> RecoverySnapshot:
    """Validate a recovery snapshot, including the agent tag at its wire boundary."""
    values = _mapping(copy.deepcopy(value), "snapshot")
    login = _string(_required(values, "login", "snapshot"), "snapshot.login")
    values["login"] = _safe_login(login).casefold()
    for field in ("machine", "label"):
        _ = _string(_required(values, field, "snapshot"), f"snapshot.{field}")
    values["label"] = canonical_label(cast(str, values["label"]))
    _ = _utc_time(_required(values, "taken_at", "snapshot"), "snapshot.taken_at")
    if "boot_id" in values and not _string(values["boot_id"], "snapshot.boot_id"):
        raise InvalidRecord("snapshot.boot_id must not be empty")
    entries = [_snapshot_session(item, f"snapshot.sessions[{index}]") for index, item in enumerate(
        _items(_required(values, "sessions", "snapshot"), "snapshot.sessions"))]
    ids = [item["session"]["session_id"] for item in entries]
    if not all(ids) or len(set(ids)) != len(ids):
        raise InvalidRecord("snapshot.sessions must have unique nonempty session ids")
    values["sessions"] = entries
    return cast(RecoverySnapshot, cast(object, values))


def _snapshot_outcome(value: object, place: str) -> SnapshotTaken | SnapshotFailed:
    values = _mapping(value, place)
    kind = _kind(values, place, frozenset({"taken", "failed"}))
    at = _utc_time(_required(values, "at", place), f"{place}.at")
    if kind == "failed":
        return SnapshotFailed(kind="failed", at=at,
            reasons=_strings(_required(values, "reasons", place), f"{place}.reasons"))
    accounts = _required(values, "accounts", place)
    if not isinstance(accounts, int) or isinstance(accounts, bool) or accounts < 0:
        raise InvalidRecord(f"{place}.accounts must be a nonnegative integer")
    return SnapshotTaken(kind="taken", at=at, accounts=accounts)


def read_snapshot_attempts() -> SnapshotAttempts | NoSnapshotAttempt:
    """Read the machine-local outcome without inferring whether snapshots exist."""
    root = _state_root()
    if not _record_directory(root):
        return NoSnapshotAttempt()
    path = root / "snapshot_attempt.json"
    if not _regular_record(path):
        return NoSnapshotAttempt()
    values = _mapping(_read_json(path), str(path))
    latest = _snapshot_outcome(_required(values, "latest", str(path)), "snapshot_attempt.latest")
    success = _mapping(_required(values, "last_success", str(path)), "snapshot_attempt.last_success")
    kind = _kind(success, "snapshot_attempt.last_success", frozenset({"taken", "not recorded"}))
    last_success: LastSuccessfulSnapshot | NoSuccessfulSnapshotRecorded = NoSuccessfulSnapshotRecorded(kind="not recorded")
    if kind == "taken":
        last_success = LastSuccessfulSnapshot(kind="taken", at=_utc_time(
            _required(success, "at", "snapshot_attempt.last_success"), "snapshot_attempt.last_success.at"))
    return SnapshotAttempts(machine=_string(_required(values, "machine", str(path)), "snapshot_attempt.machine"),
        latest=latest, last_success=last_success)


def record_snapshot_attempt(outcome: SnapshotTaken | SnapshotFailed, machine: str) -> None:
    """Replace the latest outcome under its own lock, retaining the last success."""
    checked = _snapshot_outcome(outcome, "snapshot_attempt.latest")
    root = _state_root()
    _ = _record_directory(root, create=True)
    lock = root / "snapshot_attempt.lock"
    _ = _regular_record(lock)
    with lock.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            previous = read_snapshot_attempts()
            last_success: LastSuccessfulSnapshot | NoSuccessfulSnapshotRecorded = (
                NoSuccessfulSnapshotRecorded(kind="not recorded") if isinstance(previous, NoSnapshotAttempt)
                else previous["last_success"])
            if checked["kind"] == "taken":
                last_success = LastSuccessfulSnapshot(kind="taken", at=checked["at"])
            _write_record(root / "snapshot_attempt.json", SnapshotAttempts(
                machine=machine, latest=checked, last_success=last_success))
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _snapshot_path(login: str) -> Path:
    return _state_root() / "snapshots" / (_safe_login(login).casefold() + ".json")


def _read_snapshot(login: str) -> RecoverySnapshot | NoRecoverySnapshot:
    path = _snapshot_path(login)
    _ = _record_directory(path.parent)
    if not _regular_record(path):
        return NoRecoverySnapshot()
    saved = parse_snapshot(_read_json(path))
    _check_account(login, saved["login"], path)
    return saved


def read_snapshot(login: str) -> RecoverySnapshot | NoRecoverySnapshot:
    """Read one snapshot under the same login lock as its shutdown records."""
    if not _regular_record(_snapshot_path(login)):
        return NoRecoverySnapshot()
    with _session_store_lock(login, create=True):
        return _read_snapshot(login)


def _write_snapshot(saved: RecoverySnapshot) -> None:
    path = _snapshot_path(saved["login"])
    _ = _record_directory(path.parent, create=True)
    _ = _regular_record(path)
    _write_record(path, saved)


def write_snapshot(snapshot: RecoverySnapshot) -> None:
    """Atomically replace one validated snapshot under its login's lock."""
    checked = parse_snapshot(snapshot)
    with _session_store_lock(checked["login"], create=True):
        _write_snapshot(checked)


def mark_snapshot_agent_exited(login: str, session_id: str, last_seen: str) -> None:
    """Retain a confirmed agent exit unless a newer observation replaced it."""
    with _session_store_lock(login, create=True):
        saved = _read_snapshot(login)
        if isinstance(saved, NoRecoverySnapshot):
            return
        for item in saved["sessions"]:
            if (item["agent"] == "claude" and item["session"]["session_id"] == session_id
                    and item["last_seen"] == last_seen):
                item["crash"] = "agent exited"
                item["state"] = "unresolved"
                _write_snapshot(parse_snapshot(saved))
                return


def restore_snapshot_agent_exited(snapshot: RecoverySnapshot, entry: SnapshotClaudeSession) -> bool:
    """Restore confirmed evidence retained by the watchdog, unless a newer observation owns it."""
    checked = parse_snapshot(snapshot)
    candidate = copy.deepcopy(entry)
    identity = candidate["session"]["session_id"]
    with _session_store_lock(checked["login"], create=True) as directory:
        saved = _read_snapshot(checked["login"])
        if isinstance(saved, NoRecoverySnapshot):
            saved = checked
            saved["sessions"] = []
        for current in saved["sessions"]:
            if current["session"]["session_id"] == identity:
                if (current["session"] != candidate["session"]
                        or datetime.fromisoformat(current["last_seen"]) > datetime.fromisoformat(candidate["last_seen"])):
                    return False
        for current_record in _session_records_in_directory(directory, checked["login"]):
            if (current_record["session"]["session_id"] == identity
                    and datetime.fromisoformat(current_record["requested_at"]) > datetime.fromisoformat(candidate["last_seen"])):
                return False
        candidate["crash"] = "agent exited"
        candidate["state"] = "unresolved"
        saved["sessions"] = [item for item in saved["sessions"] if item["session"]["session_id"] != identity] + [candidate]
        _write_snapshot(parse_snapshot(saved))
        return True


def all_snapshots() -> list[RecoverySnapshot]:
    root = _state_root() / "snapshots"
    if not _record_directory(root):
        return []
    try:
        paths = sorted(root.iterdir())
    except OSError as error:
        raise InvalidRecord(f"{root} cannot be read: {error}") from error
    found: list[RecoverySnapshot] = []
    for path in paths:
        if path.suffix != ".json":
            continue
        saved = read_snapshot(path.stem)
        if not isinstance(saved, NoRecoverySnapshot):
            found.append(saved)
    return found


def merge_snapshots(observed: CompleteInventory | PartialInventory, taken_at: str) -> list[RecoverySnapshot]:
    """Retain missing sessions only with a confirmed crash signal."""
    if isinstance(observed, PartialInventory):
        return []
    _ = _utc_time(taken_at, "snapshot.taken_at")
    current: dict[str, list[SnapshotSession]] = {}
    for login, entries in observed.sessions.items():
        current.setdefault(_safe_login(login).casefold(), []).extend(entries)
    current_codex = {(item["session"]["pid"], item["session"]["proc_start"])
                     for entries in current.values() for item in entries if item["agent"] == "codex"}
    labels = {login.casefold(): label for login, label in observed.labels.items()}
    logins = {saved["login"] for saved in all_snapshots()} | set(current)
    owned_ids = {saved["session"]["session_id"] for saved in all_session_records()}
    merged: list[RecoverySnapshot] = []
    for login in sorted(logins):
        with _session_store_lock(login, create=True):
            previous = _read_snapshot(login)
            seen = {item["session"]["session_id"]: copy.deepcopy(item) for item in current.get(login, [])
                    if item["session"]["session_id"] not in owned_ids}
            for item in seen.values():
                item["last_seen"], item["state"] = taken_at, "running"
                if "crash" in item:
                    del item["crash"]
                if isinstance(observed.boot, KnownBoot):
                    item["boot_id"] = observed.boot.boot_id
            if not isinstance(previous, NoRecoverySnapshot):
                for item in previous["sessions"]:
                    identity = item["session"]["session_id"]
                    same_process = (item["agent"] == "codex"
                                    and isinstance(observed.boot, KnownBoot)
                                    and item.get("boot_id") == observed.boot.boot_id
                                    and (item["session"]["pid"], item["session"]["proc_start"]) in current_codex)
                    rebooted = isinstance(observed.boot, KnownBoot) and "boot_id" in item and item["boot_id"] != observed.boot.boot_id
                    if (identity not in seen and identity not in owned_ids and not same_process
                            and ("crash" in item or rebooted or (login, identity) in observed.gone_hosts)):
                        item["state"] = "unresolved"
                        if rebooted:
                            item["crash"] = "reboot"
                        elif (login, identity) in observed.gone_hosts:
                            item["crash"] = "host gone"
                        seen[identity] = item
            label = labels.get(login, login if isinstance(previous, NoRecoverySnapshot) else previous["label"])
            saved = parse_snapshot(RecoverySnapshot(machine=observed.machine, login=login, label=label,
                                                     taken_at=taken_at, sessions=list(seen.values())))
            if isinstance(observed.boot, KnownBoot):
                saved["boot_id"] = observed.boot.boot_id
            _write_snapshot(saved)
            merged.append(saved)
    return merged


@dataclass(frozen=True)
class SnapshotAcknowledged:
    """The matching recovery entry was removed."""


@dataclass(frozen=True)
class SnapshotAlreadyAbsent:
    """There was no matching recovery entry to remove."""


def acknowledge_snapshot(login: str, session_id: str) -> SnapshotAcknowledged | SnapshotAlreadyAbsent:
    """Remove exactly one restored or forgotten session, under its login's lock."""
    with _session_store_lock(login, create=True):
        saved = _read_snapshot(login)
        if isinstance(saved, NoRecoverySnapshot):
            return SnapshotAlreadyAbsent()
        retained = [item for item in saved["sessions"] if item["session"]["session_id"] != session_id]
        if len(retained) == len(saved["sessions"]):
            return SnapshotAlreadyAbsent()
        saved["sessions"] = retained
        _write_snapshot(saved)
        return SnapshotAcknowledged()


def snapshot_restore_record(snapshot: RecoverySnapshot, entry: SnapshotClaudeSession) -> SessionShutdown:
    """Build a validated recovery request, also usable for a dry run."""
    checked = parse_snapshot(snapshot)
    item = _snapshot_session(copy.deepcopy(entry), "snapshot session")
    if item["agent"] != "claude":
        raise InvalidRecord("Codex snapshots require the Codex restore path, not a Claude shutdown record")
    at = checked["taken_at"]
    context: RecoveryContext = RecoveryEvidenceNotRecorded(kind="not recorded")
    if item.get("crash") == "reboot":
        context = RebootRecovery(kind="reboot", last_seen=item["last_seen"])
    elif item.get("crash") == "host gone":
        context = LostHostRecovery(kind="host gone", last_seen=item["last_seen"])
    elif item.get("crash") == "agent exited":
        context = AgentExitRecovery(kind="agent exited", last_seen=item["last_seen"])
    saved = SessionShutdown(
        login=checked["login"], label=checked["label"], machine=checked["machine"],
        requested_at=at, request_id=f"recovery-{at}", requested_by=FromSnapshot(kind="snapshot", taken_at=at, recovery=context),
        hold="session", then="restart", force="now", alerts="user", probe=item["session"]["probe"],
        session=item["session"], workers=item["workers"], timers=item["timers"],
        settle_message=SettleMessageNotSent(kind="not sent"), where=WhereNotSaid(kind="not said"),
        progress=SessionAlreadyGone(kind="already gone", at=at), trail=[], stop_issues=[],
    )
    return _session_shutdown(saved, "recovery record")


def snapshot_supersedes_record(snapshot: RecoverySnapshot, existing: SessionShutdown) -> bool:
    """A later snapshot replaces older restore intent, including an earlier recovery."""
    origin = existing["requested_by"]
    requested_at = origin["taken_at"] if origin["kind"] == "snapshot" else existing["requested_at"]
    return datetime.fromisoformat(snapshot["taken_at"]) > datetime.fromisoformat(requested_at)


def materialize_snapshot(snapshot: RecoverySnapshot, entry: SnapshotClaudeSession) -> SessionShutdown:
    """Persist recovery before launch, retaining an equally recent or newer request."""
    saved = snapshot_restore_record(snapshot, entry)
    login, identity = saved["login"], saved["session"]["session_id"]
    with launch_barrier():
        with _session_store_lock(login, create=True) as directory:
            for existing in _session_records_in_directory(directory, login):
                if existing["session"]["session_id"] == identity:
                    if not snapshot_supersedes_record(snapshot, existing):
                        return existing
                    _ = _record_directory(directory / "sessions", create=True)
                    _write_record(directory / "sessions" / _session_filename(identity), saved)
                    return saved
            target = directory / "sessions" / _session_filename(identity)
            _ = _record_directory(target.parent, create=True)
            if _regular_record(target):
                raise InvalidRecord(f"{target} is used by a different session id")
            _write_record(target, saved)
    return saved
