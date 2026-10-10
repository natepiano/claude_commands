#!/usr/bin/env python3
"""Persistent records for account-scoped shutdown and restart work."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, TypedDict, cast

from inventory import Session, parse_session


class AllAccountSessions(TypedDict):
    kind: Literal["all account sessions"]


class SelectedSessions(TypedDict):
    kind: Literal["selected"]
    session_ids: list[str]


Scope = AllAccountSessions | SelectedSessions


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
Force = Literal["wait for ready", "now"]


class ShowrunnerFooter(TypedDict):
    kind: Literal["footer"]
    slug: str


class NoFooter(TypedDict):
    kind: Literal["no footer"]


class TimerRestore(TypedDict):
    instance: str
    was_enabled: bool
    footer: ShowrunnerFooter | NoFooter


class NotSent(TypedDict):
    kind: Literal["not sent"]


class Sent(TypedDict):
    kind: Literal["sent"]
    at: str


class Queued(TypedDict):
    kind: Literal["queued"]
    at: str
    reason: str


SettleMessage = NotSent | Sent | Queued


class Said(TypedDict):
    kind: Literal["said"]
    text: str
    at: str


class NotSaid(TypedDict):
    kind: Literal["not said"]


Where = Said | NotSaid


class Waiting(TypedDict):
    kind: Literal["waiting"]


class Ready(TypedDict):
    kind: Literal["ready"]
    at: str


class PassiveSeatReady(TypedDict):
    kind: Literal["passive seat ready"]
    at: str


class Stopped(TypedDict):
    kind: Literal["stopped"]
    at: str


class AlreadyGone(TypedDict):
    kind: Literal["already gone"]
    at: str


class ProcessIdentityLost(TypedDict):
    kind: Literal["process identity lost"]
    at: str


class StopFailed(TypedDict):
    kind: Literal["stop failed"]
    at: str
    reason: str


class Restarted(TypedDict):
    kind: Literal["restarted"]
    at: str


class RestartFailed(TypedDict):
    kind: Literal["restart failed"]
    at: str
    reason: str


class ManualRestart(TypedDict):
    kind: Literal["manual restart"]
    command: str


Progress = (
    Waiting
    | Ready
    | PassiveSeatReady
    | Stopped
    | AlreadyGone
    | ProcessIdentityLost
    | StopFailed
    | Restarted
    | RestartFailed
    | ManualRestart
)


class Entry(TypedDict):
    session: Session
    timers: list[TimerRestore]
    settle_message: SettleMessage
    where: Where
    progress: Progress


RecordState = Literal[
    "settling",
    "stopping",
    "down",
    "stop partial",
    "restarting",
    "restart partial",
    "cancelled",
    "up",
]


class Record(TypedDict):
    login: str
    label: str
    machine: str
    state: RecordState
    requested_at: str
    requested_by: RequestOrigin
    scope: Scope
    conductor: Conductor
    force: Force
    entries: list[Entry]


class LiveRecord(TypedDict):
    kind: Literal["live"]
    record: Record


class NoShutdown(TypedDict):
    kind: Literal["no shutdown"]


class ShutdownInProgress(Exception):
    """Raised when an account already has a live shutdown record."""

    live: Record

    def __init__(self, live: Record) -> None:
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
        "restart failed",
    }
)


def _state_root() -> Path:
    return Path(
        os.environ.get(
            "SHUTDOWN_STATE_DIR", str(Path.home() / ".local/state/shutdown")
        )
    )


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
    if parsed.isoformat() != text or parsed.utcoffset() != timedelta(0):
        raise InvalidRecord(f"{place} must be an ISO-8601 UTC time")
    return text


def _scope(value: object, place: str) -> Scope:
    values = _mapping(value, place)
    kind = _kind(values, place, frozenset({"all account sessions", "selected"}))
    if kind == "selected":
        _ = _strings(
            _required(values, "session_ids", place), f"{place}.session_ids"
        )
    return cast(Scope, cast(object, values))


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


def _progress(value: object, place: str) -> Progress:
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
    elif kind == "manual restart":
        _ = _string(_required(values, "command", place), f"{place}.command")
    return cast(Progress, cast(object, values))


def _entry(value: object, place: str) -> Entry:
    values = _mapping(value, place)
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
    return cast(Entry, cast(object, values))


def _record(value: object, place: str) -> Record:
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
    entry_values = _items(_required(values, "entries", place), f"{place}.entries")
    values["entries"] = [
        _entry(entry, f"{place}.entries[{index}]")
        for index, entry in enumerate(entry_values)
    ]
    return cast(Record, cast(object, values))


def parse_records(text: str) -> list[Record]:
    """Decode and validate a JSON list of shutdown records."""
    try:
        value = cast(object, json.loads(text))
    except (ValueError, TypeError) as error:
        raise InvalidRecord("records is not valid JSON") from error
    values = _items(value, "records")
    return [_record(item, f"records[{index}]") for index, item in enumerate(values)]


def _read_record(path: Path) -> Record:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise InvalidRecord(f"{path} cannot be read: {error}") from error
    try:
        value = cast(object, json.loads(text))
    except (ValueError, TypeError) as error:
        raise InvalidRecord(f"{path} is not valid JSON") from error
    return _record(value, str(path))


def _write_record(path: Path, record: Record) -> None:
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


def find_live(login: str) -> LiveRecord | NoShutdown:
    """Return the live shutdown for an account, if one exists."""
    directory = _account_directory(login)
    if not directory.is_dir():
        return NoShutdown(kind="no shutdown")
    with _account_lock(login, create_directory=False):
        path = directory / "record.json"
        if not path.exists():
            return NoShutdown(kind="no shutdown")
        return LiveRecord(kind="live", record=_read_record(path))


def create(record: Record) -> None:
    """Create the account's only live record."""
    checked = _record(record, "record")
    with _account_lock(checked["login"], create_directory=True) as directory:
        path = directory / "record.json"
        if path.exists():
            raise ShutdownInProgress(_read_record(path))
        _write_record(path, checked)


def update(login: str, change: Callable[[Record], None]) -> Record:
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


def live_records() -> list[Record]:
    """Return all live records on this machine, ordered by account login."""
    root = _state_root()
    if not root.is_dir():
        return []
    records: list[Record] = []
    for directory in root.iterdir():
        if not directory.is_dir():
            continue
        path = directory / "record.json"
        with _account_lock(directory.name, create_directory=False):
            if path.exists():
                records.append(_read_record(path))
    return sorted(records, key=lambda record: record["login"])
