#!/usr/bin/env python3
"""Persistent records for account-scoped shutdown and restart work."""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast

from inventory import Session, parse_session
from launch_permission import launch_barrier, shutdown_state_root


class AllAccountSessions(TypedDict):
    kind: Literal["all account sessions"]


class SelectedSessions(TypedDict):
    kind: Literal["selected"]
    session_ids: list[str]


ShutdownScope = AllAccountSessions | SelectedSessions


class FromSession(TypedDict):
    kind: Literal["session"]
    session_id: str


class FromTerminal(TypedDict):
    kind: Literal["terminal"]


RequestOrigin = FromSession | FromTerminal


class SystemdConductor(TypedDict):
    kind: Literal["systemd"]
    unit: str


class LaunchdConductor(TypedDict):
    kind: Literal["launchd"]
    label: str


class ConductorNotStarted(TypedDict):
    kind: Literal["not started"]


Conductor = SystemdConductor | LaunchdConductor | ConductorNotStarted
StopTiming = Literal["wait for ready", "now"]
StopLeftRunningCause = Literal["stop not confirmed", "owner identity lost"]


class NotReadyToStop(TypedDict):
    kind: Literal["not ready to stop"]
    at: str
    status: str
    progress: Literal["waiting", "ready", "passive seat ready"]


class StillRunningAfterStop(TypedDict):
    kind: Literal["still running"]
    at: str
    reason: str


class AccountUnreadableAtStop(TypedDict):
    kind: Literal["account unreadable"]
    at: str


class SeatStillLive(TypedDict):
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
    | SeatStillLive
    | CodexServerLeftRunning
    | UnitTmuxLeftRunning
)


class StopClaimFailed(TypedDict):
    kind: Literal["stop claim failed"]
    at: str
    machine: str
    reason: str


class MachineStopFailed(TypedDict):
    kind: Literal["machine stop failed"]
    at: str
    machine: str
    reason: str


OrchestrationStopIssue = StopClaimFailed | MachineStopFailed
StopIssue = SessionStopIssue | OrchestrationStopIssue


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


class PassiveSeatReadyToStop(TypedDict):
    kind: Literal["passive seat ready"]
    at: str


class SessionStopped(TypedDict):
    kind: Literal["stopped"]
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


class PendingTimer(TypedDict):
    instance: str
    reason: str


class SessionRestoredTimersPending(TypedDict):
    kind: Literal["timers pending"]
    at: str
    timers: list[PendingTimer]


class SeatAvailableOnDemand(TypedDict):
    kind: Literal["seat available on demand"]
    at: str


class SessionRestartFailed(TypedDict):
    kind: Literal["restart failed"]
    at: str
    reason: str


class SessionNeedsManualRestart(TypedDict):
    kind: Literal["manual restart"]
    command: str


SessionProgress = (
    SessionWaiting
    | SessionReadyToStop
    | PassiveSeatReadyToStop
    | SessionStopped
    | SessionAlreadyGone
    | ProcessIdentityLost
    | SessionStopFailed
    | SessionRestarted
    | SessionRestarting
    | SessionRestoredTimersPending
    | SeatAvailableOnDemand
    | SessionRestartFailed
    | SessionNeedsManualRestart
)


class ShutdownSessionEntry(TypedDict):
    session: Session
    timers: list[TimerRestore]
    settle_message: SettleMessage
    where: Where
    progress: SessionProgress
    stop_issues: list[SessionStopIssue]


ShutdownState = Literal[
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


class ShutdownRecord(TypedDict):
    login: str
    label: str
    machine: str
    state: ShutdownState
    requested_at: str
    requested_by: RequestOrigin
    scope: ShutdownScope
    conductor: Conductor
    force: StopTiming
    then: NotRequired[ShutdownContinuation]
    entries: list[ShutdownSessionEntry]
    stop_issues: list[OrchestrationStopIssue]


class SessionShutdown(TypedDict):
    login: str
    label: str
    machine: str
    requested_at: str
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


class AccountHold(TypedDict):
    login: str
    label: str
    machine: str
    requested_at: str
    requested_by: RequestOrigin
    then: ShutdownContinuation


def continuation(record: ShutdownRecord) -> ShutdownContinuation:
    """Legacy records stop; an explicit restart survives conductor launches."""
    return record.get("then", "stop")


class LiveShutdownRecord(TypedDict):
    kind: Literal["live"]
    record: ShutdownRecord


class NoShutdown(TypedDict):
    kind: Literal["no shutdown"]


class ShutdownInProgress(Exception):
    """Raised when an account already has a live shutdown record."""

    live: ShutdownRecord

    def __init__(self, live: ShutdownRecord) -> None:
        self.live = live
        super().__init__(f"shutdown already in progress for {live['label']}")


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
        "passive seat ready",
        "stopped",
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
def _account_lock(login: str, *, create_directory: bool) -> Generator[Path, None, None]:
    directory = _account_directory(login)
    if create_directory:
        directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / "lock"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield directory
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def stop_lock(login: str) -> Generator[None, None, None]:
    """Serialize complete stop attempts without blocking ordinary record reads."""
    directory = _account_directory(login)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "stop.lock").open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
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


def _scope(value: object, place: str) -> ShutdownScope:
    values = _mapping(value, place)
    kind = _kind(values, place, frozenset({"all account sessions", "selected"}))
    if kind == "selected":
        _ = _strings(
            _required(values, "session_ids", place), f"{place}.session_ids"
        )
    return cast(ShutdownScope, cast(object, values))


def _request_origin(value: object, place: str) -> RequestOrigin:
    values = _mapping(value, place)
    kind = _kind(values, place, frozenset({"session", "terminal"}))
    if kind == "session":
        _ = _string(
            _required(values, "session_id", place), f"{place}.session_id"
        )
    return cast(RequestOrigin, cast(object, values))


def _conductor(value: object, place: str) -> Conductor:
    values = _mapping(value, place)
    kind = _kind(values, place, frozenset({"systemd", "launchd", "not started"}))
    if kind == "systemd":
        _ = _string(_required(values, "unit", place), f"{place}.unit")
    elif kind == "launchd":
        _ = _string(_required(values, "label", place), f"{place}.label")
    return cast(Conductor, cast(object, values))


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


def _progress(value: object, place: str) -> SessionProgress:
    values = _mapping(value, place)
    kind = _kind(
        values,
        place,
        frozenset({"waiting", "manual restart"}) | _PROGRESS_WITH_TIME,
    )
    if kind in _PROGRESS_WITH_TIME:
        _ = _utc_time(_required(values, "at", place), f"{place}.at")
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


def _session_stop_issue(value: object, place: str) -> SessionStopIssue:
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
    if kind == "not ready to stop":
        _ = _string(_required(values, "status", place), f"{place}.status")
        progress = _string(
            _required(values, "progress", place), f"{place}.progress"
        )
        if progress not in {"waiting", "ready", "passive seat ready"}:
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
) -> OrchestrationStopIssue:
    values, _ = _stop_issue_fields(
        value,
        place,
        frozenset({"stop claim failed", "machine stop failed"}),
    )
    _ = _string(_required(values, "machine", place), f"{place}.machine")
    _ = _string(_required(values, "reason", place), f"{place}.reason")
    return cast(OrchestrationStopIssue, cast(object, values))


def _entry(value: object, place: str) -> ShutdownSessionEntry:
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
        _required(values, "progress", place), f"{place}.progress"
    )
    stop_issue_values = _items(values.get("stop_issues", []), f"{place}.stop_issues")
    values["stop_issues"] = [
        _session_stop_issue(issue, f"{place}.stop_issues[{index}]")
        for index, issue in enumerate(stop_issue_values)
    ]
    return cast(ShutdownSessionEntry, cast(object, values))


def _record(value: object, place: str) -> ShutdownRecord:
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
    return cast(ShutdownRecord, cast(object, values))


def parse_records(text: str) -> list[ShutdownRecord]:
    """Decode and validate a JSON list of shutdown records."""
    try:
        value = cast(object, json.loads(text))
    except (ValueError, TypeError) as error:
        raise InvalidRecord("records is not valid JSON") from error
    values = _items(value, "records")
    return [_record(item, f"records[{index}]") for index, item in enumerate(values)]


def _read_record(path: Path) -> ShutdownRecord:
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


def _write_record(path: Path, record: ShutdownRecord | SessionShutdown | AccountHold) -> None:
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
    account_lock = _account_lock(login, create_directory=False)
    try:
        _ = account_lock.__enter__()
    except OSError as error:
        lock_path = directory / "lock"
        raise InvalidRecord(
            f"{lock_path} cannot be opened or locked: {error}"
        ) from error
    try:
        yield
    finally:
        _ = account_lock.__exit__(None, None, None)


def _slot_record(directory: Path) -> ShutdownRecord | None:
    path = directory / "record.json"
    record_status = _lstat(path)
    if record_status is None:
        return None
    if not stat.S_ISREG(record_status.st_mode):
        raise InvalidRecord(
            f"{path} must be a regular file; found {_entry_kind(record_status.st_mode)}"
        )
    return _read_record(path)


def _parked_records(directory: Path) -> list[ShutdownRecord]:
    parked_directory = directory / "partial"
    parked_status = _lstat(parked_directory)
    if parked_status is None:
        return []
    if not stat.S_ISDIR(parked_status.st_mode):
        raise InvalidRecord(
            f"{parked_directory} must be a directory; found {_entry_kind(parked_status.st_mode)}"
        )
    records: list[ShutdownRecord] = []
    for path in parked_directory.glob("*.json"):
        path_status = _lstat(path)
        if path_status is None:
            continue
        if not stat.S_ISREG(path_status.st_mode):
            raise InvalidRecord(
                f"{path} must be a regular file; found {_entry_kind(path_status.st_mode)}"
            )
        records.append(_read_record(path))
    return sorted(records, key=lambda record: record["requested_at"])


def find_live(login: str) -> LiveShutdownRecord | NoShutdown:
    """Return the account's slot record, if one exists; parked records are not in the slot."""
    directory = _existing_account_directory(login)
    if directory is None:
        return NoShutdown(kind="no shutdown")
    with _reading_lock(login, directory):
        found = _slot_record(directory)
    if found is None:
        return NoShutdown(kind="no shutdown")
    return LiveShutdownRecord(kind="live", record=found)


def _slot_and_parked(directory: Path) -> list[ShutdownRecord]:
    slot = _slot_record(directory)
    return [*([] if slot is None else [slot]), *_parked_records(directory)]


def parked(login: str) -> list[ShutdownRecord]:
    """Return the account's parked `restart partial` records, oldest first."""
    directory = _existing_account_directory(login)
    if directory is None:
        return []
    with _reading_lock(login, directory):
        return _parked_records(directory)


def account_records(login: str) -> list[ShutdownRecord]:
    """Return the account's slot record, then its parked records oldest first."""
    directory = _existing_account_directory(login)
    if directory is None:
        return []
    with _reading_lock(login, directory):
        return _slot_and_parked(directory)


def create(record: ShutdownRecord) -> None:
    """Create the account's slot record; parked records never block it."""
    checked = _record(record, "record")
    with launch_barrier():
        with _account_lock(checked["login"], create_directory=True) as directory:
            path = directory / "record.json"
            if path.exists():
                raise ShutdownInProgress(_read_record(path))
            _write_record(path, checked)


def update(login: str, change: Callable[[ShutdownRecord], None]) -> ShutdownRecord:
    """Change and atomically replace one live record while holding its lock."""
    directory = _account_directory(login)
    if not directory.is_dir():
        raise NoLiveRecord(f"no live shutdown for {login}")
    with _account_lock(login, create_directory=False):
        path = directory / "record.json"
        if not path.exists():
            raise NoLiveRecord(f"no live shutdown for {login}")
        record = _read_record(path)
        original_login = record["login"]
        original_machine = record["machine"]
        change(record)
        if (
            record.get("login") != original_login
            or record.get("machine") != original_machine
        ):
            raise ValueError("a record update cannot change its login or machine")
        checked = _record(record, "record")
        _write_record(path, checked)
        return checked


def archive(login: str) -> None:
    """Move a completed or cancelled live record into account history."""
    directory = _account_directory(login)
    if not directory.is_dir():
        raise NoLiveRecord(f"no live shutdown for {login}")
    with _account_lock(login, create_directory=False):
        path = directory / "record.json"
        if not path.exists():
            raise NoLiveRecord(f"no live shutdown for {login}")
        record = _read_record(path)
        if record["state"] not in {"cancelled", "up"}:
            raise ValueError("a shutdown can be archived only after it is cancelled or up")
        history = directory / "history"
        history.mkdir(exist_ok=True)
        os.replace(path, history / f"{record['requested_at']}.json")


def park(login: str) -> ShutdownRecord:
    """Move a `restart partial` slot record to `partial/<requested_at>.json`, freeing the slot."""
    directory = _account_directory(login)
    if not directory.is_dir():
        raise NoLiveRecord(f"no live shutdown for {login}")
    with _account_lock(login, create_directory=False):
        found = _slot_record(directory)
        if found is None:
            raise NoLiveRecord(f"no live shutdown for {login}")
        if found["state"] != "restart partial":
            raise ValueError(
                f"only a restart partial record can be parked; this one is {found['state']}"
            )
        parked_directory = directory / "partial"
        parked_directory.mkdir(exist_ok=True)
        target = parked_directory / f"{found['requested_at']}.json"
        if _lstat(target) is not None:
            raise ValueError(f"{target} is already parked")
        os.replace(directory / "record.json", target)
        return found


def unpark(login: str, requested_at: str) -> None:
    """Return one parked record to the empty slot."""
    _ = _utc_time(requested_at, "requested_at")
    directory = _account_directory(login)
    if not directory.is_dir():
        raise NoLiveRecord(f"no parked shutdown for {login}")
    with _account_lock(login, create_directory=False):
        occupant = _slot_record(directory)
        if occupant is not None:
            raise ShutdownInProgress(occupant)
        source = directory / "partial" / f"{requested_at}.json"
        source_status = _lstat(source)
        if source_status is None:
            raise NoLiveRecord(f"no parked shutdown of {login} from {requested_at}")
        if not stat.S_ISREG(source_status.st_mode):
            raise InvalidRecord(
                f"{source} must be a regular file; found {_entry_kind(source_status.st_mode)}"
            )
        os.replace(source, directory / "record.json")


def live_records() -> list[ShutdownRecord]:
    """Return every account's records on this machine in login order.

    Each account lists its slot record, then its parked ones oldest first.
    """
    root = _state_root()
    if not root.is_dir():
        return []
    records: list[ShutdownRecord] = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        with _account_lock(directory.name, create_directory=False):
            records.extend(_slot_and_parked(directory))
    return records


def _shutdown_request_fields(values: dict[str, object], place: str) -> None:
    login = _string(_required(values, "login", place), f"{place}.login")
    try:
        _ = _safe_login(login)
    except ValueError as error:
        raise InvalidRecord(f"{place}.login is unsafe for record storage") from error
    for field in ("label", "machine"):
        _ = _string(_required(values, field, place), f"{place}.{field}")
    _ = _utc_time(_required(values, "requested_at", place), f"{place}.requested_at")
    values["requested_by"] = _request_origin(
        _required(values, "requested_by", place), f"{place}.requested_by"
    )
    action = _string(_required(values, "then", place), f"{place}.then")
    if action not in {"stop", "restart"}:
        raise InvalidRecord(f"{place}.then is invalid")


def _session_shutdown(value: object, place: str) -> SessionShutdown:
    values = _mapping(value, place)
    _shutdown_request_fields(values, place)
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
    if "alerted_at" in values:
        _ = _utc_time(values["alerted_at"], f"{place}.alerted_at")
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
def _session_store_lock(login: str, *, create: bool = False) -> Generator[Path, None, None]:
    directory = _account_directory(login)
    _ = _record_directory(directory, create=create)
    _ = _regular_record(directory / "lock")
    with _reading_lock(login, directory):
        yield directory


def _legacy_account_records(directory: Path, login: str) -> list[ShutdownRecord]:
    """Read the validated slot, then parked records newest first."""
    partial = directory / "partial"
    if _record_directory(partial):
        try:
            _ = list(partial.iterdir())
        except OSError as error:
            raise InvalidRecord(f"{partial} cannot be read: {error}") from error
    slot = _slot_record(directory)
    records = [*([] if slot is None else [slot]), *reversed(_parked_records(directory))]
    for record in records:
        _check_account(login, record["login"], directory)
    return records


def _convert_session(record: ShutdownRecord, entry: ShutdownSessionEntry) -> SessionShutdown:
    session = entry["session"]
    progress = entry["progress"]
    if record["state"] == "restarting" and progress["kind"] not in {"restarted", "seat available on demand"}:
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
        hold="account" if record["scope"]["kind"] == "all account sessions" else "session",
        then=continuation(record), force=record["force"], alerts="user",
        session=session, seats=seats, timers=entry["timers"],
        settle_message=entry["settle_message"], where=entry["where"],
        progress=progress, stop_issues=entry["stop_issues"],
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
    with _session_store_lock(login) as directory:
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
    with _session_store_lock(login) as directory:
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
                    then=continuation(record),
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
    with _session_store_lock(login) as directory:
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
