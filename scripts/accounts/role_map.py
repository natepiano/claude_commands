"""The authoritative account-role map, with a read-only cache on other machines."""
from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Literal, NotRequired, ParamSpec, TypeVar, TypedDict, cast

from scripts.accounts import worker_names
from scripts.lib.user_zone import user_zone

ROLE_MAP_HOST = "natedev"
Role = Literal["showrunners", "units", "workers"]


class StoredGroupSelection(TypedDict):
    label: str
    move_id: str
    cause: Literal["by hand", "refusal"]
    window: Literal["weekly", "five_hour", "unknown"]
    reset: NotRequired[str]


class StoredGroupSelections(TypedDict, total=False):
    units: str | StoredGroupSelection
    showrunners: str | StoredGroupSelection


@dataclass(frozen=True)
class RoleMap:
    groups: StoredGroupSelections
    workers: str


@dataclass(frozen=True)
class RoleMapChange:
    role: Role
    label: str
    why: str
    at: str
    move_id: str = ""
    cause: Literal["by hand", "refusal"] = "by hand"
    window: Literal["weekly", "five_hour", "unknown"] = "unknown"
    reset: str = ""


class RoleMapUnavailable(ValueError):
    """Neither the authority nor a valid saved map can be read."""


class RoleAccountUnavailable(ValueError):
    """A role has no account that can launch on this machine."""


class RoleMapWriteRefused(ValueError):
    """The authoritative write did not complete."""


class RoleMapDocument(TypedDict):
    groups: StoredGroupSelections
    workers: str


class RoleChangeDocument(TypedDict):
    role: Role
    label: str
    why: str
    at: str
    move_id: str
    cause: Literal["by hand", "refusal"]
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
    cause: NotRequired[Literal["by hand", "refusal"]]
    window: NotRequired[Literal["weekly", "five_hour", "unknown"]]
    reset: NotRequired[str]


def _change_document(change: RoleMapChange) -> RoleChangeDocument:
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


_command_map: ContextVar[CommandRoleMap | MapNotRead] = ContextVar("role_map", default=MapNotRead())
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


def groups_file() -> Path:
    return Path.home() / ".local/state/accounts/groups.json"


def role_map_cache_file() -> Path:
    return groups_file().with_name("role_map_cache.json")


def is_role_map_host() -> bool:
    return socket.gethostname().split(".", 1)[0] == ROLE_MAP_HOST


def ssh_role_map(arguments: tuple[str, ...]) -> str:
    """Only transport boundary; callers never invoke remote account launches."""
    command = "python3 ~/.claude/scripts/accounts/accounts.py " + shlex.join(arguments)
    result = subprocess.run(("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                             ROLE_MAP_HOST, command), capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise OSError("remote role map command failed")
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
                or not fields["move_id"] or fields.get("cause") not in ("by hand", "refusal")
                or fields.get("window") not in ("weekly", "five_hour", "unknown")):
            raise ValueError(f"invalid {group} transition")
        if re.fullmatch(r"claude ?[1-9][0-9]*", str(fields["label"])) is None:
            raise ValueError(f"invalid {group} account")
        if "reset" in fields:
            reset = fields["reset"]
            if not isinstance(reset, str) or datetime.fromisoformat(reset).tzinfo is None:
                raise ValueError(f"invalid {group} reset")
        selected = StoredGroupSelection(label=cast(str, fields["label"]), move_id=cast(str, fields["move_id"]),
            cause=cast(Literal["by hand", "refusal"], fields["cause"]),
            window=cast(Literal["weekly", "five_hour", "unknown"], fields["window"]))
        if "reset" in fields:
            selected["reset"] = cast(str, fields["reset"])
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
    return RoleMap(_groups(cast(dict[str, object], groups)), workers)


def encode_role_map(value: RoleMap) -> str:
    return json.dumps(RoleMapDocument(groups=value.groups, workers=value.workers), sort_keys=True)


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
    return RoleMap(groups, workers)


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


def read_role_map() -> RoleMap:
    scope = _command_map.get()
    if isinstance(scope, CommandRoleMap) and isinstance(scope.value, RoleMap):
        return scope.value
    if isinstance(scope, CommandRoleMap) and isinstance(scope.value, MapReadRefused):
        raise RoleMapUnavailable(scope.value.reason)
    try:
        if is_role_map_host():
            result = _local_role_map()
        else:
            try:
                result = decode_role_map(ssh_role_map(("role-map-read",)))
            except (OSError, ValueError, subprocess.SubprocessError):
                saved = _object(role_map_cache_file())
                at = saved["at"]
                if not isinstance(at, str):
                    raise ValueError("invalid cache time")
                stamp = datetime.fromisoformat(at)
                if stamp.tzinfo is None:
                    raise ValueError("invalid cache time")
                result = decode_role_map(json.dumps(saved["map"]))
                print(f"role map from {stamp.astimezone(user_zone()).strftime('%Y-%m-%d %H:%M %Z')}; "
                      + f"{ROLE_MAP_HOST} unreachable", file=sys.stderr)
            else:
                saved_map = CachedRoleMap(at=datetime.now(timezone.utc).isoformat(),
                    map=RoleMapDocument(groups=result.groups, workers=result.workers))
                _atomic_write(role_map_cache_file(), json.dumps(saved_map) + "\n")
    except (OSError, ValueError, KeyError) as error:
        reason = f"accounts: cannot read role map from {ROLE_MAP_HOST}; no usable map"
        if isinstance(scope, CommandRoleMap):
            scope.value = MapReadRefused(reason)
        raise RoleMapUnavailable(reason) from error
    if isinstance(scope, CommandRoleMap):
        scope.value = result
    return result


def decode_role_map_change(text: str) -> RoleMapChange:
    value = cast(object, json.loads(text))
    if not isinstance(value, dict):
        raise ValueError("role change must contain an object")
    fields = cast(dict[str, object], value)
    role, label, why, at = (fields.get(key) for key in ("role", "label", "why", "at"))
    if role not in ("units", "showrunners", "workers") or not all(isinstance(item, str) for item in (label, why, at)):
        raise ValueError("invalid role change")
    move_id, reset = fields.get("move_id", ""), fields.get("reset", "")
    cause, window = fields.get("cause", "by hand"), fields.get("window", "unknown")
    if (not isinstance(move_id, str) or not isinstance(reset, str)
            or cause not in ("by hand", "refusal") or window not in ("weekly", "five_hour", "unknown")):
        raise ValueError("invalid role change")
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


def write_role_map(change: RoleMapChange) -> None:
    change = decode_role_map_change(json.dumps(_change_document(change)))
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
                    size = history.stat().st_size if history.exists() else -1
                    if change.role == "workers":
                        values = worker_names.configuration(values)
                        values["workers"] = change.label
                    else:
                        selection = StoredGroupSelection(label=change.label, move_id=change.move_id,
                            cause=change.cause, window=change.window)
                        if change.reset:
                            selection["reset"] = change.reset
                        values[change.role] = selection
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
                    try:
                        _atomic_write(path, json.dumps(values, sort_keys=True) + "\n")
                        history.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        descriptor = os.open(history, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                        with os.fdopen(descriptor, "a", encoding="utf-8") as output:
                            fcntl.flock(output.fileno(), fcntl.LOCK_EX)
                            _ = output.write(json.dumps(row, sort_keys=True) + "\n")
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
            selected = StoredGroupSelection(label=change.label, move_id=change.move_id,
                cause=change.cause, window=change.window)
            if change.reset:
                selected["reset"] = change.reset
            groups[change.role] = selected
        scope.value = RoleMap(groups, workers)
