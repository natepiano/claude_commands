#!/usr/bin/env python3
"""Persist per-session shutdown requests and account launch holds."""

from __future__ import annotations

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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast

from inventory import Session, parse_session
from launch_permission import launch_barrier, shutdown_state_root

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from user_zone import user_zone  # noqa: E402

CLAIM_STALE_SECONDS = 120
CONDUCTOR_LOCK_RETRY_DELAYS = (0.05,) * 5


class NoRemoteControl(TypedDict):
    session_id: str
    name: str
    alerts: Literal["user", "none"]


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


RequestOrigin = FromSession | FromTerminal


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
    progress: Literal["waiting", "ready", "held by usage limit", "passive seat ready"]


class StillRunningAfterStop(TypedDict):
    kind: Literal["still running"]
    at: str
    reason: str


class AccountUnreadableAtStop(TypedDict):
    kind: Literal["account unreadable"]
    at: str


class _LegacySeatStillLive(TypedDict):
    kind: Literal["seat still live"]
    at: str


class CodexServerLeftRunning(TypedDict):
    kind: Literal["codex server left running"]
    at: str
    run_dir: str
    cause: StopLeftRunningCause


class UnitTmuxLeftRunning(TypedDict):
    kind: Literal["unit tmux session left running"]
    at: str
    tmux_session: str
    cause: StopLeftRunningCause


SessionStopIssue = (
    NotReadyToStop
    | StillRunningAfterStop
    | AccountUnreadableAtStop
    | CodexServerLeftRunning
    | UnitTmuxLeftRunning
)


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
_LegacyStopIssue = SessionStopIssue | _LegacySeatStillLive | _LegacyOrchestrationIssue


class ShowrunnerFooter(TypedDict):
    kind: Literal["footer"]
    slug: str


class NoFooter(TypedDict):
    kind: Literal["no footer"]


class TimerRestore(TypedDict):
    instance: str
    was_enabled: bool
    footer: ShowrunnerFooter | NoFooter


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


class SessionReadyToStop(TypedDict):
    kind: Literal["ready"]
    at: str


class SessionHeldByUsageLimit(TypedDict):
    kind: Literal["held by usage limit"]
    at: str


class _LegacyPassiveSeatReady(TypedDict):
    kind: Literal["passive seat ready"]
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


class _LegacySeatAvailable(TypedDict):
    kind: Literal["seat available on demand"]
    at: str


class SessionRestartFailed(TypedDict):
    kind: Literal["restart failed"]
    at: str
    reason: str


class SessionNeedsManualRestart(TypedDict):
    kind: Literal["manual restart"]
    command: str


SessionRestartPrior = (
    SessionWaiting
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
    progress: SessionProgress | _LegacyPassiveSeatReady | _LegacySeatAvailable
    stop_issues: list[SessionStopIssue | _LegacySeatStillLive]


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


class SessionShutdown(TypedDict):
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
    session: Session
    seats: list[Session]
    timers: list[TimerRestore]
    settle_message: SettleMessage
    where: Where
    progress: SessionProgress
    stop_issues: list[SessionStopIssue]
    alerted_at: NotRequired[str]
    # The phone push for a form open in the session's pane; the user is told once.
    form_alerted_at: NotRequired[str]
    # The not-back failure last pushed to the user, so an unchanged one is not pushed again.
    restore_alerted: NotRequired[str]
    # Absent means resume on session.config_dir, never on a changed role.
    target: NotRequired[str]
    source_label: NotRequired[str]
    why: NotRequired[str]


class AccountHold(TypedDict):
    login: str
    label: str
    machine: str
    requested_at: str
    request_id: str
    requested_by: RequestOrigin
    then: ShutdownContinuation


def stop_incomplete(saved: SessionShutdown) -> bool:
    """Report a failed stop or owner resources left after a terminal stop."""
    progress = saved["progress"]["kind"]
    return progress == "stop failed" or bool(saved["stop_issues"] and progress in {
        "stopped", "already gone", "process identity lost",
    })


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
        "held by usage limit",
        "passive seat ready",
        "stopped",
        "stopping",
        "already gone",
        "process identity lost",
        "stop failed",
        "restarted",
        "restarting",
        "timers pending",
        "seat available on demand",
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
    kind = _kind(values, place, frozenset({"session", "terminal"}))
    if kind == "session":
        _ = _string(
            _required(values, "session_id", place), f"{place}.session_id"
        )
    return cast(RequestOrigin, cast(object, values))


def _conductor(value: object, place: str) -> _LegacyConductor:
    values = _mapping(value, place)
    kind = _kind(values, place, frozenset({"systemd", "launchd", "not started"}))
    if kind == "systemd":
        _ = _string(_required(values, "unit", place), f"{place}.unit")
    elif kind == "launchd":
        _ = _string(_required(values, "label", place), f"{place}.label")
    return cast(_LegacyConductor, cast(object, values))


def _footer(value: object, place: str) -> ShowrunnerFooter | NoFooter:
    values = _mapping(value, place)
    kind = _kind(values, place, frozenset({"footer", "no footer"}))
    if kind == "footer":
        _ = _string(_required(values, "slug", place), f"{place}.slug")
    return cast(ShowrunnerFooter | NoFooter, cast(object, values))


def _timer(value: object, place: str) -> TimerRestore:
    values = _mapping(value, place)
    _ = _string(_required(values, "instance", place), f"{place}.instance")
    _ = _boolean(
        _required(values, "was_enabled", place), f"{place}.was_enabled"
    )
    values["footer"] = _footer(
        _required(values, "footer", place), f"{place}.footer"
    )
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


def _progress(value: object, place: str, *, legacy: bool = False) -> SessionProgress | _LegacyPassiveSeatReady | _LegacySeatAvailable:
    values = _mapping(value, place)
    kind = _kind(
        values,
        place,
        frozenset({"waiting", "manual restart"}) | _PROGRESS_WITH_TIME,
    )
    if kind in _PROGRESS_WITH_TIME:
        _ = _utc_time(_required(values, "at", place), f"{place}.at")
    if kind in {"passive seat ready", "seat available on demand"} and not legacy:
        raise InvalidRecord(f"{place}.kind is invalid")
    if kind == "restarting":
        prior = _progress(_required(values, "prior", place), f"{place}.prior")
        if prior["kind"] == "restarting":
            raise InvalidRecord(f"{place}.prior must not be a restart claim")
        values["prior"] = prior
    if kind in {"stop failed", "restart failed"}:
        _ = _string(_required(values, "reason", place), f"{place}.reason")
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


def _session_stop_issue(value: object, place: str, *, legacy: bool = False) -> SessionStopIssue | _LegacySeatStillLive:
    values, kind = _stop_issue_fields(
        value,
        place,
        frozenset(
            {
                "not ready to stop",
                "still running",
                "account unreadable",
                "seat still live",
                "codex server left running",
                "unit tmux session left running",
            }
        ),
    )
    if kind == "seat still live" and not legacy:
        raise InvalidRecord(f"{place}.kind is invalid")
    if kind == "not ready to stop":
        _ = _string(_required(values, "status", place), f"{place}.status")
        progress = _string(
            _required(values, "progress", place), f"{place}.progress"
        )
        if progress not in {"waiting", "ready", "held by usage limit", "passive seat ready"}:
            raise InvalidRecord(f"{place}.progress is invalid")
    elif kind == "still running":
        _ = _string(_required(values, "reason", place), f"{place}.reason")
    elif kind == "codex server left running":
        _ = _string(_required(values, "run_dir", place), f"{place}.run_dir")
        cause = _string(_required(values, "cause", place), f"{place}.cause")
        if cause not in {"stop not confirmed", "owner identity lost"}:
            raise InvalidRecord(f"{place}.cause is invalid")
    elif kind == "unit tmux session left running":
        _ = _string(
            _required(values, "tmux_session", place), f"{place}.tmux_session"
        )
        cause = _string(_required(values, "cause", place), f"{place}.cause")
        if cause not in {"stop not confirmed", "owner identity lost"}:
            raise InvalidRecord(f"{place}.cause is invalid")
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
    return cast(_LegacySessionEntry, cast(object, values))


def _record(value: object, place: str) -> _LegacyShutdownRecord:
    values = _mapping(value, place)
    login = _string(_required(values, "login", place), f"{place}.login")
    try:
        _ = _safe_login(login)
    except ValueError as error:
        raise InvalidRecord(f"{place}.login is unsafe for record storage") from error
    _ = _string(_required(values, "label", place), f"{place}.label")
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
    return _record(value, str(path))


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


def _write_record(path: Path, record: _LegacyShutdownRecord | SessionShutdown | AccountHold) -> None:
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
    _ = _utc_time(_required(values, "requested_at", place), f"{place}.requested_at")
    request_id = _string(_required(values, "request_id", place), f"{place}.request_id")
    if not request_id:
        raise InvalidRecord(f"{place}.request_id must not be empty")
    values["requested_by"] = _request_origin(
        _required(values, "requested_by", place), f"{place}.requested_by"
    )
    action = _string(_required(values, "then", place), f"{place}.then")
    if action not in {"stop", "restart"}:
        raise InvalidRecord(f"{place}.then is invalid")


def _session_shutdown(value: object, place: str) -> SessionShutdown:
    values = _mapping(value, place)
    _shutdown_request_fields(values, place)
    if "target" in values:
        target = _string(values["target"], f"{place}.target")
        if target != "default" and not re.fullmatch(r"claude [1-9][0-9]*", target):
            raise InvalidRecord(f"{place}.target must be default or claude N")
    for field in ("source_label", "why"):
        if field in values:
            _ = _string(values[field], f"{place}.{field}")
    for field, allowed in (
        ("hold", {"account", "session"}),
        ("force", {"wait for ready", "now"}),
        ("alerts", {"user", "none"}),
    ):
        text = _string(_required(values, field, place), f"{place}.{field}")
        if text not in allowed:
            raise InvalidRecord(f"{place}.{field} is invalid")
    _ = _required(values, "stop_issues", place)
    fields = _entry(values, place)
    values["progress"] = _progress(values["progress"], f"{place}.progress")
    values["stop_issues"] = [_session_stop_issue(issue, f"{place}.stop_issues") for issue in fields["stop_issues"]]
    if fields["session"]["kind"] == "seat":
        raise InvalidRecord(f"{place}.session must not be a seat")
    _ = _session_filename(fields["session"]["session_id"])
    seats = _items(_required(values, "seats", place), f"{place}.seats")
    parsed_seats: list[Session] = []
    for index, seat in enumerate(seats):
        seat_place = f"{place}.seats[{index}]"
        try:
            parsed = parse_session(seat, seat_place)
        except ValueError as error:
            raise InvalidRecord(str(error)) from error
        if parsed["kind"] != "seat":
            raise InvalidRecord(f"{seat_place} must be a seat")
        owner = parsed["owner"]
        if owner["kind"] != "director" or owner["session_id"] != fields["session"]["session_id"]:
            raise InvalidRecord(f"{seat_place}.owner must identify this session")
        parsed_seats.append(parsed)
    values["seats"] = parsed_seats
    for field in ("alerted_at", "form_alerted_at"):
        if field in values:
            _ = _utc_time(values[field], f"{place}.{field}")
    if "restore_alerted" in values:
        _ = _string(values["restore_alerted"], f"{place}.restore_alerted")
    return cast(SessionShutdown, cast(object, values))


def _account_hold(value: object, place: str) -> AccountHold:
    values = _mapping(value, place)
    _shutdown_request_fields(values, place)
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


def _read_json(path: Path) -> object:
    try:
        return cast(object, json.loads(path.read_text(encoding="utf-8")))
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
    if progress["kind"] == "passive seat ready" or progress["kind"] == "seat available on demand":
        progress = SessionStopped(kind="stopped", at=progress["at"])
    if record["state"] == "restarting" and progress["kind"] != "restarted":
        progress = SessionStopped(kind="stopped", at=progress.get("at", record["requested_at"]))
    seats: list[Session] = []
    for candidate in record["entries"]:
        seat = candidate["session"]
        if seat["kind"] == "seat" and seat["owner"]["kind"] == "director":
            if seat["owner"]["session_id"] == session["session_id"]:
                seats.append(seat)
    return SessionShutdown(
        login=record["login"], label=record["label"], machine=record["machine"],
        requested_at=record["requested_at"], requested_by=record["requested_by"],
        request_id=f"legacy-{record['machine']}-{record['requested_at']}",
        hold="account" if record["scope"]["kind"] == "all account sessions" else "session",
        then=_legacy_continuation(record), force=record["force"], alerts="user",
        session=session, seats=seats, timers=entry["timers"],
        settle_message=entry["settle_message"], where=entry["where"],
        progress=cast(SessionProgress, cast(object, progress)), stop_issues=[issue for issue in entry["stop_issues"] if issue["kind"] != "seat still live"],
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
            if session["kind"] != "seat" and session["session_id"] not in stored_ids:
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
        if directory.name == "launch.lock":
            _ = _regular_record(directory)
            continue
        records.extend(session_records(directory.name))
    return sorted(records, key=lambda item: (item["requested_at"], item["session"]["name"]))


def find_session(login: str, session_id: str) -> SessionShutdown | None:
    """Find this session's record, including a converted old entry."""
    return next((item for item in session_records(login) if item["session"]["session_id"] == session_id), None)


def create_session(record: SessionShutdown) -> bool:
    """Create one session record while excluding competing launch decisions."""
    checked = _session_shutdown(record, "record")
    with launch_barrier():
        with _session_store_lock(checked["login"], create=True) as directory:
            existing = _session_records_in_directory(directory, checked["login"])
            if any(item["session"]["session_id"] == checked["session"]["session_id"] for item in existing):
                return False
            sessions = directory / "sessions"
            _ = _record_directory(sessions, create=True)
            path = sessions / _session_filename(checked["session"]["session_id"])
            if _regular_record(path):
                raise InvalidRecord(f"{path} is used by a different session id")
            _write_record(path, checked)
    return True


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
        change(record)
        checked = _session_shutdown(record, str(path))
        if (checked["login"], checked["machine"], checked["session"]["session_id"]) != identity:
            raise ValueError("a session record update cannot change its login, machine or session id")
        _write_record(path, checked)
        return checked


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
        if directory.name == "launch.lock":
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
        if progress not in {"waiting", "ready", "held by usage limit"} and not (
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
    """Clear the current hold only after its last session record leaves."""
    if _existing_account_directory(login) is None:
        return
    with _session_store_lock(login, write=True) as directory:
        path = directory / "hold.json"
        if not _regular_record(path):
            return
        held = _account_hold(_read_json(path), str(path))
        _check_account(login, held["login"], path)
        if request_ids and held["request_id"] not in request_ids:
            return
        if not any(saved["request_id"] == held["request_id"]
                   for saved in _session_records_in_directory(directory, login)):
            path.unlink()


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
