"""The authoritative account-role map, with a read-only cache on other machines."""
from __future__ import annotations

import fcntl
import json
import os
import pwd
import re
import shlex
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Literal, NotRequired, ParamSpec, TypeVar, TypedDict, cast

from scripts.accounts import worker_names
from scripts.lib.user_zone import user_zone

ROLE_MAP_HOST = "natedev"
Role = Literal["showrunners", "units", "workers"]


@dataclass(frozen=True)
class RecordedTransitionReason:
    text: str


@dataclass(frozen=True)
class UnavailableTransitionReason:
    """A historical selection did not record its transition reason."""


TransitionReason = RecordedTransitionReason | UnavailableTransitionReason


class StoredGroupSelection(TypedDict):
    label: str
    move_id: str
    cause: Literal["by hand", "refusal", "auto"]
    window: Literal["weekly", "five_hour", "unknown"]
    reset: NotRequired[str]
    why: NotRequired[str]


def selection_reason(selection: StoredGroupSelection) -> TransitionReason:
    if "why" in selection:
        return RecordedTransitionReason(selection["why"])
    return UnavailableTransitionReason()


class StoredGroupSelections(TypedDict, total=False):
    units: str | StoredGroupSelection
    showrunners: str | StoredGroupSelection


@dataclass(frozen=True)
class DefaultWorkersClaudeAccount:
    """Use the caller's or showrunners' account for Claude workers."""


@dataclass(frozen=True)
class NamedWorkersClaudeAccount:
    """Use this workers account until the source weekly reset."""

    label: str
    until: datetime

    def __post_init__(self) -> None:
        if re.fullmatch(r"claude[1-9][0-9]*", self.label) is None:
            raise ValueError("invalid Claude workers account")
        if self.until.tzinfo is None:
            raise ValueError("Claude workers route needs an end time with a zone")


WorkersClaudeAccount = NamedWorkersClaudeAccount | DefaultWorkersClaudeAccount
DEFAULT_WORKERS_CLAUDE_ACCOUNT = DefaultWorkersClaudeAccount()


def _workers_claude_account(fields: dict[str, object]) -> WorkersClaudeAccount:
    if "workers_claude" not in fields:
        return DEFAULT_WORKERS_CLAUDE_ACCOUNT
    route = fields["workers_claude"]
    # Older maps had only a label. They cannot keep a route open without an end.
    if isinstance(route, str):
        return DEFAULT_WORKERS_CLAUDE_ACCOUNT
    if not isinstance(route, dict):
        raise ValueError("invalid Claude workers route")
    fields = cast(dict[str, object], route)
    if "until" not in fields:
        return DEFAULT_WORKERS_CLAUDE_ACCOUNT
    label, until = fields.get("label"), fields["until"]
    if not isinstance(label, str) or not isinstance(until, str):
        raise ValueError("invalid Claude workers route")
    return NamedWorkersClaudeAccount(label, datetime.fromisoformat(until))


@dataclass(frozen=True)
class WorkersClaudeRouteChange:
    account: WorkersClaudeAccount
    why: str
    at: str


@dataclass(frozen=True)
class CurrentRoleMap:
    """The authority supplied this map during this command."""


@dataclass(frozen=True)
class CachedAfterFetchFailure:
    at: datetime
    failure: str


@dataclass(frozen=True)
class ExplicitStoredRoleMap:
    at: datetime


RoleMapProvenance = CurrentRoleMap | CachedAfterFetchFailure | ExplicitStoredRoleMap


@dataclass(frozen=True)
class RoleMap:
    groups: StoredGroupSelections
    workers: str
    workers_claude: WorkersClaudeAccount = DEFAULT_WORKERS_CLAUDE_ACCOUNT
    provenance: RoleMapProvenance = field(default_factory=CurrentRoleMap, compare=False)


@dataclass(frozen=True)
class RoleMapChange:
    role: Role
    label: str
    why: str
    at: str
    move_id: str = ""
    cause: Literal["by hand", "refusal", "auto", "credits off"] = "by hand"
    window: Literal["weekly", "five_hour", "unknown"] = "unknown"
    reset: str = ""


class RoleMapUnavailable(ValueError):
    """Neither the authority nor a valid saved map can be read."""


class RoleAccountUnavailable(ValueError):
    """A role has no account that can launch on this machine."""


class RoleMapWriteRefused(ValueError):
    """The authoritative write did not complete."""


class WorkersClaudeRouteDocument(TypedDict):
    label: str
    until: str


class RoleMapDocument(TypedDict):
    groups: StoredGroupSelections
    workers: str
    workers_claude: NotRequired[WorkersClaudeRouteDocument]


class RoleChangeDocument(TypedDict):
    role: Role
    label: str
    why: str
    at: str
    move_id: str
    cause: Literal["by hand", "refusal", "auto", "credits off"]
    window: Literal["weekly", "five_hour", "unknown"]
    reset: str


class RoleHistoryEntry(TypedDict):
    at: str
    label: str
    why: str
    role: NotRequired[Role]
    group: NotRequired[Role]
    account_label: NotRequired[str]
    move_id: NotRequired[str]
    cause: NotRequired[Literal["by hand", "refusal", "auto", "credits off"]]
    window: NotRequired[Literal["weekly", "five_hour", "unknown"]]
    reset: NotRequired[str]


def _change_document(change: RoleMapChange | WorkersClaudeRouteChange) -> RoleChangeDocument | dict[str, str]:
    if isinstance(change, WorkersClaudeRouteChange):
        return {"role": "workers_claude", "label": (change.account.label
            if isinstance(change.account, NamedWorkersClaudeAccount) else ""), "until": (change.account.until.isoformat()
            if isinstance(change.account, NamedWorkersClaudeAccount) else ""), "why": change.why, "at": change.at}
    return RoleChangeDocument(role=change.role, label=change.label, why=change.why, at=change.at,
        move_id=change.move_id, cause=change.cause, window=change.window, reset=change.reset)


class CachedRoleMap(TypedDict):
    at: str
    map: RoleMapDocument


@dataclass(frozen=True)
class MapNotRead:
    """The current command has not read the map yet."""


@dataclass(frozen=True)
class MapReadRefused:
    reason: str


@dataclass
class CommandRoleMap:
    value: RoleMap | MapNotRead | MapReadRefused


@dataclass(frozen=True)
class FetchRoleMap:
    """Read the authority, falling back to its last usable cache."""


@dataclass(frozen=True)
class StoredRoleMap:
    """Read local authority or saved cache without contacting a remote host."""


RoleMapSource = FetchRoleMap | StoredRoleMap
_command_map: ContextVar[CommandRoleMap | MapNotRead] = ContextVar("role_map", default=MapNotRead())
_role_map_source: ContextVar[RoleMapSource] = ContextVar("role_map_source", default=FetchRoleMap())
P = ParamSpec("P")
R = TypeVar("R")


def role_map_command(function: Callable[P, R]) -> Callable[P, R]:
    """Reuse one remote observation within a command, never across commands."""
    @wraps(function)
    def command(*args: P.args, **kwargs: P.kwargs) -> R:
        if isinstance(_command_map.get(), CommandRoleMap):
            return function(*args, **kwargs)
        token = _command_map.set(CommandRoleMap(MapNotRead()))
        try:
            return function(*args, **kwargs)
        finally:
            _command_map.reset(token)
    return command


@contextmanager
def stored_role_map() -> Generator[None]:
    """Read local authority or saved cache only, within an independent scope."""
    source_token = _role_map_source.set(StoredRoleMap())
    map_token = _command_map.set(CommandRoleMap(MapNotRead()))
    try:
        yield
    finally:
        _command_map.reset(map_token)
        _role_map_source.reset(source_token)


def groups_file() -> Path:
    return Path.home() / ".local/state/accounts/groups.json"


def role_map_cache_file() -> Path:
    return groups_file().with_name("role_map_cache.json")


@dataclass(frozen=True)
class LoginHome:
    path: Path


@dataclass(frozen=True)
class LoginHomeUnread:
    """The password database did not supply the current uid's home."""


def _login_home() -> LoginHome | LoginHomeUnread:
    """Read the current uid's home independently of the process's HOME."""
    try:
        return LoginHome(Path(pwd.getpwuid(os.getuid()).pw_dir))
    except (KeyError, OSError):
        return LoginHomeUnread()


def is_role_map_host() -> bool:
    if socket.gethostname().split(".", 1)[0] == ROLE_MAP_HOST:
        return True
    login_home = _login_home()
    if isinstance(login_home, LoginHomeUnread):
        return True
    try:
        return Path.home().resolve() != login_home.path.resolve()
    except (OSError, RuntimeError):
        # An unconfirmed home must never reach the real remote map.
        return True


class RemoteCommandRefused(OSError):
    """The host ran the command and supplied its refusal for the caller to report."""


def ssh_role_map(arguments: tuple[str, ...]) -> str:
    """Only transport boundary; callers never invoke remote account launches."""
    command = "python3 ~/.claude/scripts/accounts/accounts.py " + shlex.join(arguments)
    result = subprocess.run(("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                             ROLE_MAP_HOST, command), capture_output=True, text=True, timeout=20)
    if result.returncode:
        message = next((line.strip() for line in reversed(result.stderr.splitlines()) if line.strip()),
                       "remote role map command failed")
        if result.returncode == 255:
            raise OSError(message)
        raise RemoteCommandRefused(message)
    return result.stdout


def _object(path: Path) -> dict[str, object]:
    value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return cast(dict[str, object], value)


def _groups(raw: dict[str, object]) -> StoredGroupSelections:
    groups = StoredGroupSelections()
    for group in ("units", "showrunners"):
        if group not in raw:
            continue
        value = raw[group]
        if isinstance(value, str):
            if value not in ("default", "~/.claude") and re.fullmatch(r"claude ?[1-9][0-9]*", value) is None:
                raise ValueError(f"invalid {group} account")
            groups[group] = value
            continue
        if not isinstance(value, dict):
            raise ValueError(f"invalid {group} account")
        fields = cast(dict[str, object], value)
        if (not isinstance(fields.get("label"), str) or not isinstance(fields.get("move_id"), str)
                or not fields["move_id"] or fields.get("cause") not in ("by hand", "refusal", "auto")
                or fields.get("window") not in ("weekly", "five_hour", "unknown")):
            raise ValueError(f"invalid {group} transition")
        if re.fullmatch(r"claude ?[1-9][0-9]*", str(fields["label"])) is None:
            raise ValueError(f"invalid {group} account")
        if "reset" in fields:
            reset = fields["reset"]
            if not isinstance(reset, str) or datetime.fromisoformat(reset).tzinfo is None:
                raise ValueError(f"invalid {group} reset")
        if "why" in fields and not isinstance(fields["why"], str):
            raise ValueError(f"invalid {group} reason")
        selected = StoredGroupSelection(label=cast(str, fields["label"]), move_id=cast(str, fields["move_id"]),
            cause=cast(Literal["by hand", "refusal", "auto"], fields["cause"]),
            window=cast(Literal["weekly", "five_hour", "unknown"], fields["window"]))
        if "reset" in fields:
            selected["reset"] = cast(str, fields["reset"])
        if "why" in fields:
            selected["why"] = cast(str, fields["why"])
        groups[group] = selected
    return groups


def decode_role_map(text: str) -> RoleMap:
    value = cast(object, json.loads(text))
    if not isinstance(value, dict):
        raise ValueError("role map must contain an object")
    fields = cast(dict[str, object], value)
    groups, workers = fields.get("groups"), fields.get("workers")
    if not isinstance(groups, dict) or not isinstance(workers, str):
        raise ValueError("invalid role map")
    return RoleMap(_groups(cast(dict[str, object], groups)), workers, _workers_claude_account(fields))


def _map_document(value: RoleMap) -> RoleMapDocument:
    document = RoleMapDocument(groups=value.groups, workers=value.workers)
    if isinstance(value.workers_claude, NamedWorkersClaudeAccount):
        document["workers_claude"] = WorkersClaudeRouteDocument(
            label=value.workers_claude.label, until=value.workers_claude.until.isoformat())
    return document


def encode_role_map(value: RoleMap) -> str:
    return json.dumps(_map_document(value), sort_keys=True)


def _local_role_map() -> RoleMap:
    try:
        groups = _groups(_object(groups_file()))
    except FileNotFoundError:
        groups = StoredGroupSelections()
    try:
        values = worker_names.configuration(_object(worker_names.accounts_config_file()))
    except FileNotFoundError:
        values = {}
    workers = values.get("workers", "")
    if not isinstance(workers, str):
        raise ValueError("invalid workers account")
    return RoleMap(groups, workers, _workers_claude_account(values))


def _atomic_write(path: Path, contents: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            _ = output.write(contents)
        _ = temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _cached_role_map() -> tuple[RoleMap, datetime]:
    saved = _object(role_map_cache_file())
    at = saved["at"]
    if not isinstance(at, str):
        raise ValueError("invalid cache time")
    stamp = datetime.fromisoformat(at)
    if stamp.tzinfo is None:
        raise ValueError("invalid cache time")
    return decode_role_map(json.dumps(saved["map"])), stamp


def read_role_map() -> RoleMap:
    scope = _command_map.get()
    if isinstance(scope, CommandRoleMap) and isinstance(scope.value, RoleMap):
        return scope.value
    if isinstance(scope, CommandRoleMap) and isinstance(scope.value, MapReadRefused):
        raise RoleMapUnavailable(scope.value.reason)
    try:
        if is_role_map_host():
            result = _local_role_map()
        elif isinstance(_role_map_source.get(), StoredRoleMap):
            result, stamp = _cached_role_map()
            result = replace(result, provenance=ExplicitStoredRoleMap(stamp))
            _map_observer.get()(result.provenance)
        else:
            try:
                result = decode_role_map(ssh_role_map(("role-map-read",)))
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                result, stamp = _cached_role_map()
                provenance = CachedAfterFetchFailure(stamp, str(error))
                result = replace(result, provenance=provenance)
                _map_observer.get()(provenance)
                print(cached_map_warning(provenance), file=sys.stderr)
            else:
                saved_map = CachedRoleMap(at=datetime.now(timezone.utc).isoformat(),
                    map=_map_document(result))
                _atomic_write(role_map_cache_file(), json.dumps(saved_map) + "\n")
    except (OSError, ValueError, KeyError) as error:
        reason = f"accounts: cannot read role map from {ROLE_MAP_HOST}; no usable map"
        if isinstance(scope, CommandRoleMap):
            scope.value = MapReadRefused(reason)
        raise RoleMapUnavailable(reason) from error
    if isinstance(scope, CommandRoleMap):
        scope.value = result
    return result


def observed_map_provenance() -> RoleMapProvenance:
    """Return the provenance of the same memoized read used by this command."""
    scope = _command_map.get()
    return scope.value.provenance if isinstance(scope, CommandRoleMap) and isinstance(scope.value, RoleMap) else CurrentRoleMap()


@contextmanager
def observe_map_reads(observer: Callable[[RoleMapProvenance], None]) -> Generator[None]:
    token = _map_observer.set(observer)
    try:
        yield
    finally:
        _map_observer.reset(token)


def _ignore_map_read(_provenance: RoleMapProvenance) -> None:
    pass


_map_observer: ContextVar[Callable[[RoleMapProvenance], None]] = ContextVar(
    "map_observer", default=_ignore_map_read)


def cached_map_warning(provenance: CachedAfterFetchFailure) -> str:
    stamp = provenance.at.astimezone(user_zone()).strftime('%Y-%m-%d %H:%M %Z')
    return f"role map from {stamp}; {ROLE_MAP_HOST} fetch failed: {provenance.failure}"


def decode_role_map_change(text: str) -> RoleMapChange | WorkersClaudeRouteChange:
    value = cast(object, json.loads(text))
    if not isinstance(value, dict):
        raise ValueError("role change must contain an object")
    fields = cast(dict[str, object], value)
    role, label, why, at = (fields.get(key) for key in ("role", "label", "why", "at"))
    if role == "workers_claude":
        if not all(isinstance(item, str) for item in (label, why, at)):
            raise ValueError("invalid Claude workers route change")
        if datetime.fromisoformat(cast(str, at)).tzinfo is None:
            raise ValueError("invalid route change time")
        until = fields.get("until")
        if label and not isinstance(until, str):
            raise ValueError("Claude workers route needs an end time")
        return WorkersClaudeRouteChange(NamedWorkersClaudeAccount(cast(str, label), datetime.fromisoformat(cast(str, until))) if label
            else DEFAULT_WORKERS_CLAUDE_ACCOUNT, cast(str, why), cast(str, at))
    if role not in ("units", "showrunners", "workers") or not all(isinstance(item, str) for item in (label, why, at)):
        raise ValueError("invalid role change")
    move_id, reset = fields.get("move_id", ""), fields.get("reset", "")
    cause, window = fields.get("cause", "by hand"), fields.get("window", "unknown")
    if (not isinstance(move_id, str) or not isinstance(reset, str)
            or cause not in ("by hand", "refusal", "auto", "credits off") or window not in ("weekly", "five_hour", "unknown")):
        raise ValueError("invalid role change")
    if role != "workers" and cause == "credits off":
        raise ValueError("credits off is a workers transition cause")
    change = RoleMapChange(role, cast(str, label), cast(str, why), cast(str, at), move_id, cause, window, reset)
    if datetime.fromisoformat(change.at).tzinfo is None:
        raise ValueError("invalid role change")
    prefix = "codex" if change.role == "workers" else "claude"
    if not change.label.startswith(prefix) or not change.label[len(prefix):].isdigit() or int(change.label[len(prefix):]) < 1:
        raise ValueError("invalid role account")
    return change


@contextmanager
def _groups_lock() -> Generator[None]:
    path = groups_file().with_suffix(".lock")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _role_history_entry(change: RoleMapChange) -> RoleHistoryEntry:
    row = RoleHistoryEntry(at=change.at, label=change.label, why=change.why)
    if change.role == "workers":
        row["role"] = "workers"
        row["account_label"] = change.label
    else:
        row["group"] = change.role
    if change.move_id:
        row["move_id"] = change.move_id
    row["cause"] = change.cause
    row["window"] = change.window
    if change.reset:
        row["reset"] = change.reset
    return row


def _recover_locked_role_history(history: Path) -> None:
    if history.with_name(f".{history.name}.workers").exists():
        with history.open("r+", encoding="utf-8") as source:
            fcntl.flock(source.fileno(), fcntl.LOCK_EX)
            _ = worker_names.migrate_locked_journal(source, history)


def write_role_map(change: RoleMapChange | WorkersClaudeRouteChange) -> None:
    change = decode_role_map_change(json.dumps(_change_document(change)))
    if isinstance(change, WorkersClaudeRouteChange):
        _write_workers_claude_route(change)
        return
    if not is_role_map_host():
        try:
            _ = ssh_role_map(("role-map-write", json.dumps(_change_document(change), sort_keys=True)))
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise RoleMapWriteRefused(f"accounts: {ROLE_MAP_HOST} refused {change.role} → {change.label}") from error
    else:
        path = worker_names.accounts_config_file() if change.role == "workers" else groups_file()
        history = groups_file().with_name("roles.jsonl")
        try:
            with _groups_lock(), worker_names.roles_lock():
                try:
                    previous = path.read_text(encoding="utf-8") if path.exists() else ""
                    values = _object(path) if previous else {}
                    _recover_locked_role_history(history)
                    size = history.stat().st_size if history.exists() else -1
                    if change.role == "workers":
                        values = worker_names.configuration(values)
                        values["workers"] = change.label
                    else:
                        assert change.cause != "credits off"
                        selection = StoredGroupSelection(label=change.label, move_id=change.move_id,
                            cause=change.cause, window=change.window, why=change.why)
                        if change.reset:
                            selection["reset"] = change.reset
                        values[change.role] = selection
                    row = _role_history_entry(change)
                    try:
                        _atomic_write(path, json.dumps(values, sort_keys=True) + "\n")
                        history.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        descriptor = os.open(history, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
                        with os.fdopen(descriptor, "a", encoding="utf-8") as output:
                            fcntl.flock(output.fileno(), fcntl.LOCK_EX)
                            append_journal_line(output.fileno(), json.dumps(row, sort_keys=True), valid_row=lambda _row: True)
                    except OSError:
                        if previous:
                            _atomic_write(path, previous)
                        else:
                            path.unlink(missing_ok=True)
                        if size < 0:
                            history.unlink(missing_ok=True)
                        elif history.exists():
                            with history.open("r+b") as output:
                                _ = output.truncate(size)
                        raise
                except (OSError, ValueError) as error:
                    raise RoleMapWriteRefused(f"accounts: could not write {change.role} → {change.label} on {ROLE_MAP_HOST}") from error
        except OSError as error:
            raise RoleMapWriteRefused(f"accounts: could not write {change.role} → {change.label} on {ROLE_MAP_HOST}") from error
    scope = _command_map.get()
    if isinstance(scope, CommandRoleMap) and isinstance(scope.value, RoleMap):
        groups = scope.value.groups.copy()
        workers = scope.value.workers
        if change.role == "workers":
            workers = change.label
        else:
            assert change.cause != "credits off"
            selected = StoredGroupSelection(label=change.label, move_id=change.move_id,
                cause=change.cause, window=change.window, why=change.why)
            if change.reset:
                selected["reset"] = change.reset
            groups[change.role] = selected
        scope.value = RoleMap(groups, workers, scope.value.workers_claude)


def _write_workers_claude_route(change: WorkersClaudeRouteChange) -> None:
    """Write the route through the same authority and locks as the workers role."""
    try:
        if not is_role_map_host():
            _ = ssh_role_map(("role-map-write", json.dumps(_change_document(change), sort_keys=True)))
        else:
            with _groups_lock(), worker_names.roles_lock():
                path = worker_names.accounts_config_file()
                values = worker_names.configuration(_object(path)) if path.exists() else {}
                if isinstance(change.account, NamedWorkersClaudeAccount):
                    values["workers_claude"] = {"label": change.account.label, "until": change.account.until.isoformat()}
                else:
                    _ = values.pop("workers_claude", None)
                _atomic_write(path, json.dumps(values, sort_keys=True) + "\n")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise RoleMapWriteRefused(f"accounts: could not write Claude workers route on {ROLE_MAP_HOST}") from error
    scope = _command_map.get()
    if isinstance(scope, CommandRoleMap) and isinstance(scope.value, RoleMap):
        scope.value = replace(scope.value, workers_claude=change.account)


def append_journal_line(descriptor: int, document: str, *, valid_row: Callable[[object], bool]) -> None:
    """Write a complete encoded row on an O_APPEND descriptor under its flock."""
    flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
    _ = fcntl.fcntl(descriptor, fcntl.F_SETFL, flags | os.O_APPEND)
    data = (document + "\n").encode("utf-8")
    # Remove an interrupted final append before adding a new independent row.
    size = os.lseek(descriptor, 0, os.SEEK_END)
    if size:
        tail = os.pread(descriptor, size, 0)
        if not tail.endswith(b"\n"):
            final = tail.rsplit(b"\n", 1)[-1]
            try:
                if not valid_row(cast(object, json.loads(final))):
                    raise ValueError("invalid journal row")
            except (ValueError, UnicodeError):
                os.ftruncate(descriptor, size - len(final))
            else:
                _ = os.write(descriptor, b"\n")
    while data:
        count = os.write(descriptor, data)
        if count <= 0:
            raise OSError("journal append made no progress")
        data = data[count:]


@dataclass(frozen=True)
class UnreadableAutoMoveHistory:
    """Replay protection is unavailable, so no log entry may run this pass."""


@dataclass(frozen=True)
class RecordedAutoMoves:
    entry_ids: frozenset[str]


def recorded_auto_moves(path: Path) -> RecordedAutoMoves | UnreadableAutoMoveHistory:
    """Recorded choices consume their entries even if log removal was interrupted."""
    try:
        worker_names.recover_pending_journal(path, role_history=True)
    except (OSError, ValueError) as error:
        print(f"account moves: cannot read recorded auto moves: {error}")
        return UnreadableAutoMoveHistory()
    return _read_recorded_auto_moves(path)


def _read_recorded_auto_moves(path: Path) -> RecordedAutoMoves | UnreadableAutoMoveHistory:
    try:
        text = path.read_bytes()
        lines = text.splitlines(keepends=True)
    except FileNotFoundError:
        return RecordedAutoMoves(frozenset())
    except (OSError, ValueError) as error:
        print(f"account moves: cannot read recorded auto moves: {error}")
        return UnreadableAutoMoveHistory()
    consumed: set[str] = set()
    try:
        for index, line in enumerate(lines):
            try:
                raw = cast(object, json.loads(line))
            except (ValueError, UnicodeError):
                if index == len(lines) - 1 and not line.endswith(b"\n"):
                    print(f"account moves: skipping torn final line in {path}")
                    continue
                raise
            if not isinstance(raw, dict):
                continue
            fields = cast(dict[str, object], raw)
            why = fields.get("why")
            if (fields.get("cause") == "auto" and fields.get("group") in ("units", "showrunners")
                    and isinstance(why, str) and why.startswith("auto: ")):
                consumed.add(why.removeprefix("auto: "))
    except ValueError as error:
        print(f"account moves: cannot read recorded auto moves: {error}")
        return UnreadableAutoMoveHistory()
    return RecordedAutoMoves(frozenset(consumed))


@dataclass(frozen=True)
class AutoMoveTarget:
    entry_id: str
    group: Literal["units", "showrunners"]
    label: str


def repair_selected_auto_moves(entries: tuple[AutoMoveTarget, ...], now: datetime, *,
                               dry_run: bool = False) -> RecordedAutoMoves | UnreadableAutoMoveHistory:
    """Keep replay evidence for choices saved before their history append."""
    history = groups_file().with_name("roles.jsonl")
    try:
        with (nullcontext() if dry_run else _groups_lock()), (nullcontext() if dry_run else worker_names.roles_lock()):
            if dry_run:
                recorded = recorded_auto_moves(history)
            else:
                _recover_locked_role_history(history)
                recorded = _read_recorded_auto_moves(history)
            if isinstance(recorded, UnreadableAutoMoveHistory):
                return recorded
            path = groups_file()
            selections = _groups(_object(path)) if path.exists() else StoredGroupSelections()
            consumed = set(recorded.entry_ids)
            missing: list[RoleHistoryEntry] = []
            for entry in entries:
                selected = selections.get("units") if entry.group == "units" else selections.get("showrunners")
                if (selected is None or isinstance(selected, str) or selected["cause"] != "auto"
                        or selected.get("why") != "auto: " + entry.entry_id
                        or selected["label"] != entry.label):
                    continue
                if entry.entry_id not in consumed:
                    missing.append(_role_history_entry(RoleMapChange(entry.group, selected["label"],
                        "auto: " + entry.entry_id, now.isoformat(), selected["move_id"], "auto",
                        selected["window"], selected.get("reset", ""))))
                consumed.add(entry.entry_id)
            if missing and not dry_run:
                descriptor = os.open(history, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(descriptor, "a", encoding="utf-8") as output:
                    fcntl.flock(output.fileno(), fcntl.LOCK_EX)
                    for row in missing:
                        append_journal_line(output.fileno(), json.dumps(row, sort_keys=True), valid_row=lambda _row: True)
            return RecordedAutoMoves(frozenset(consumed))
    except (OSError, ValueError) as error:
        print(f"account moves: cannot read recorded auto moves: {error}")
        return UnreadableAutoMoveHistory()
