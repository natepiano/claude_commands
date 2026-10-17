"""The ordered, one-shot Claude group moves kept on the role-map host."""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
import subprocess
import tempfile
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import uuid4

from scripts.accounts import role_map


class NextMoveError(ValueError):
    """The next-move log or requested change is invalid or unavailable."""


class NextMoveHoldRemoval(Protocol):
    def removing_next_move(self, entry_id: str) -> AbstractContextManager[None]: ...


@dataclass(frozen=True)
class NextMove:
    id: str
    group: Literal["units", "showrunners"]
    at_percent: float
    hold_minutes: float
    to: str
    added_at: datetime

    def __post_init__(self) -> None:
        if not self.id or self.group not in ("units", "showrunners"):
            raise NextMoveError("next: expected an id and units or showrunners")
        if isinstance(self.at_percent, bool) or not 0 <= self.at_percent <= 100 or not math.isfinite(self.at_percent):
            raise NextMoveError("next: percent must be from 0 to 100")
        try:
            valid_hold = (not isinstance(self.hold_minutes, bool) and self.hold_minutes >= 0
                          and math.isfinite(self.hold_minutes))
        except OverflowError:
            valid_hold = False
        if not valid_hold:
            raise NextMoveError("next: hold_minutes must be finite and nonnegative")
        if not re.fullmatch(r"claude[1-9][0-9]*", self.to):
            raise NextMoveError("next: target must be a numbered Claude account")
        if self.added_at.utcoffset() is None:
            raise NextMoveError("next: added_at needs a zone")


def next_moves_file() -> Path:
    return Path.home() / ".local/state/accounts/next_moves.json"


def encode_next_moves(entries: tuple[NextMove, ...]) -> str:
    return json.dumps([dict(id=entry.id, group=entry.group, at_percent=entry.at_percent,
                            hold_minutes=entry.hold_minutes, to=entry.to,
                            added_at=entry.added_at.isoformat()) for entry in entries])


def decode_next_moves(text: str) -> tuple[NextMove, ...]:
    try:
        raw = cast(object, json.loads(text))
        if not isinstance(raw, list):
            raise NextMoveError("next: expected an entry list")
        entries: list[NextMove] = []
        ids: set[str] = set()
        for item in cast(list[object], raw):
            if not isinstance(item, dict):
                raise NextMoveError("next: expected an entry object")
            fields = cast(dict[str, object], item)
            name, group, target, added = (fields.get(key) for key in ("id", "group", "to", "added_at"))
            percent, hold = fields.get("at_percent"), fields.get("hold_minutes", 0)
            if (not isinstance(name, str) or group not in ("units", "showrunners")
                    or not isinstance(target, str) or not isinstance(added, str)
                    or not isinstance(percent, (int, float)) or isinstance(percent, bool)
                    or not isinstance(hold, (int, float)) or isinstance(hold, bool)):
                raise NextMoveError("next: invalid entry fields")
            entry = NextMove(name, group, float(percent),
                             float(hold), target, datetime.fromisoformat(added))
            if entry.id in ids:
                raise NextMoveError("next: duplicate entry id")
            ids.add(entry.id)
            entries.append(entry)
        return tuple(entries)
    except (ValueError, TypeError, OverflowError) as error:
        raise NextMoveError(f"next: invalid log: {error}") from error


def read_next_moves() -> tuple[NextMove, ...]:
    try:
        if not role_map.is_role_map_host():
            return decode_next_moves(role_map.ssh_role_map(("next-read",)))
        try:
            text = next_moves_file().read_text()
        except FileNotFoundError:
            return ()
        return decode_next_moves(text)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise NextMoveError(f"next: cannot read log: {error}") from error


@contextmanager
def next_moves_lock() -> Generator[None]:
    """Serialize local edits and the engine's selection/consumption pass."""
    if not role_map.is_role_map_host():
        raise NextMoveError("next: log writes belong on natedev")
    path = next_moves_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def write_next_moves(entries: tuple[NextMove, ...]) -> None:
    """Replace the local log; the caller holds next_moves_lock."""
    if not role_map.is_role_map_host():
        raise NextMoveError("next: log writes belong on natedev")
    path = next_moves_file()
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".next-")
    try:
        with os.fdopen(descriptor, "w") as output:
            _ = output.write(encode_next_moves(entries) + "\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def add_next_move(group: Literal["units", "showrunners"], at_percent: float, to: str,
                  hold_minutes: float = 0) -> NextMove:
    entry = NextMove(uuid4().hex, group, at_percent, hold_minutes, to, datetime.now(timezone.utc))
    if not role_map.is_role_map_host():
        try:
            response = role_map.ssh_role_map(("next-write", encode_next_moves((entry,))))
            received = decode_next_moves(response)
            if len(received) != 1:
                raise NextMoveError("next: remote add returned no entry")
            return received[0]
        except (OSError, subprocess.TimeoutExpired) as error:
            raise NextMoveError(f"next: remote add failed: {error}") from error
    try:
        with next_moves_lock():
            write_next_moves((*read_next_moves(), entry))
    except OSError as error:
        raise NextMoveError(f"next: cannot add entry: {error}") from error
    return entry


def remove_next_move(entry_id: str) -> None:
    if not role_map.is_role_map_host():
        try:
            _ = role_map.ssh_role_map(("next", "remove", entry_id))
        except (OSError, subprocess.TimeoutExpired) as error:
            raise NextMoveError(f"next: remote remove failed: {error}") from error
        return
    try:
        watcher = cast(NextMoveHoldRemoval, cast(object, import_module("scripts.accounts.refusal_watch")))
        with watcher.removing_next_move(entry_id), next_moves_lock():
            entries = read_next_moves()
            kept = tuple(entry for entry in entries if entry.id != entry_id)
            if len(kept) == len(entries):
                raise NextMoveError(f"next: no entry {entry_id}")
            write_next_moves(kept)
    except OSError as error:
        raise NextMoveError(f"next: cannot remove entry: {error}") from error


def render_next_move(entry: NextMove) -> str:
    hold = f" (hold {entry.hold_minutes:g} minutes)" if entry.hold_minutes else ""
    return f"{entry.id}: when {entry.group} reach {entry.at_percent:g}% → {entry.to}{hold}"
