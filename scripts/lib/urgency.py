"""The order every queue the user works through shows its items in: the most useful to know now first.

Levels, first to last:

  user    the user called it important; the most recently marked first
  blocks  work has stopped until the user acts: a unit, merge, production or running process waits
  soon    it matters by a known time, its `due`; the soonest due first
  normal  an item with no level
  later   only useful eventually

Inside a level a higher value goes first where the queue has one (a proposal's score, an
efficiency gain), then the oldest. An item may name `after`, the key of its prerequisite: while
that item is open, it moves up to just before the item that needs it.

The item /next showed stays current until it leaves the queue (its verdict or answer), even when a
more urgent item arrives; a user mark takes the slot after it. `top` holds that pin, one small JSON
file per queue under NEXT_STATE_DIR (default ~/.local/state/next).
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Generic, TypeVar, cast

USER = "user"
BLOCKS = "blocks"
SOON = "soon"
NORMAL = "normal"
LATER = "later"
LEVELS = (USER, BLOCKS, SOON, NORMAL, LATER)
# The stored form of a minute: the form every queue already writes its times in.
MINUTE = "%Y-%m-%dT%H:%M"
_CELL_DUE = re.compile(r"^due (?P<due>\S+)$")
_CELL_MARKED = re.compile(r"^marked (?P<marked>\S+)$")
_CELL_AFTER = re.compile(r"^after (?P<after>\S+)$")
_UNSAFE = re.compile(r"[^\w.-]+")
Item = TypeVar("Item")


class UrgencyError(ValueError):
    """A level, due time or mark that cannot be read."""


@dataclass(frozen=True)
class Urgency:
    level: str = NORMAL
    # Set only for `soon`.
    due: datetime | None = None
    # When the user called it important; set only for `user`.
    marked: datetime | None = None


def _time(name: str, text: str) -> datetime:
    try:
        return datetime.fromisoformat(text.strip())
    except ValueError:
        raise UrgencyError(f"{name} {text!r} is not an ISO date or time") from None


def read(level: str = "", due: str = "", marked: str = "") -> Urgency:
    """The urgency these words give; no level is `normal`. `soon` needs `due`, and only `soon` takes it."""
    name = level.strip() or NORMAL
    if name not in LEVELS:
        raise UrgencyError(f"urgency {name!r} is not one of {', '.join(LEVELS)}")
    if name == SOON and not due.strip():
        raise UrgencyError("urgency soon needs a due time")
    if name != SOON and due.strip():
        raise UrgencyError(f"a due time goes only with urgency soon, not {name}")
    return Urgency(name, _time("due", due) if due.strip() else None,
                   _time("marked", marked) if name == USER and marked.strip() else None)


def mark(now: datetime) -> Urgency:
    """The urgency of an item the user calls important now."""
    return Urgency(USER, marked=now.replace(second=0, microsecond=0))


def keep_mark(new: Urgency, old: Urgency, now: datetime) -> Urgency:
    """A rewrite keeps the user's mark: the user's word outranks the writer's level."""
    if old.level == USER:
        return old
    return mark(now) if new.level == USER and new.marked is None else new


def _text(when: datetime) -> str:
    return when.strftime(MINUTE) if when.hour or when.minute or when.tzinfo else when.date().isoformat()


def fields(urgency: Urgency, prefix: str = "") -> dict[str, str]:
    """What to store: nothing for `normal`, else the level and its time."""
    if urgency.level == NORMAL:
        return {}
    stored = {f"{prefix}urgency": urgency.level}
    if urgency.due is not None:
        stored[f"{prefix}due"] = _text(urgency.due)
    if urgency.marked is not None:
        stored[f"{prefix}marked"] = _text(urgency.marked)
    return stored


def from_fields(values: Mapping[str, object], prefix: str = "") -> Urgency:
    """The urgency stored by `fields`; a value that is not text is refused."""
    words: list[str] = []
    for key in ("urgency", "due", "marked"):
        value = values.get(f"{prefix}{key}", "")
        if not isinstance(value, str):
            raise UrgencyError(f"{prefix}{key} must be text")
        words.append(value)
    return read(*words)


def cell(urgency: Urgency, after: str = "") -> str:
    """A table cell: empty for a `normal` item with no prerequisite, such as `soon, due 2026-10-22`."""
    parts = [] if urgency.level == NORMAL else [urgency.level]
    if urgency.due is not None:
        parts.append(f"due {_text(urgency.due)}")
    if urgency.marked is not None:
        parts.append(f"marked {_text(urgency.marked)}")
    if after:
        parts.append(f"after {after}")
    return ", ".join(parts)


def parse_cell(text: str) -> tuple[Urgency, str]:
    """The urgency and prerequisite a `cell` holds."""
    parts = [part.strip() for part in text.split(",") if part.strip()]
    level = parts.pop(0) if parts and parts[0] in LEVELS else ""
    due = marked = after = ""
    for part in parts:
        if found := _CELL_DUE.match(part):
            due = found["due"]
        elif found := _CELL_MARKED.match(part):
            marked = found["marked"]
        elif found := _CELL_AFTER.match(part):
            after = found["after"]
        else:
            raise UrgencyError(f"urgency cell part {part!r} is not `due`, `marked` or `after`")
    return read(level, due, marked), after


@dataclass(frozen=True)
class Entry(Generic[Item]):
    """One queue item and what orders it. `key` names it in the pin and in another item's `after`."""

    key: str
    item: Item
    urgency: Urgency = Urgency()
    since: datetime | None = None
    # Larger first; every item of one queue has the same length.
    value: tuple[float, ...] = ()
    after: str = ""


def _stamp(when: datetime | None, missing: float) -> float:
    return missing if when is None else when.timestamp()


def _key(entry: Entry[Item]) -> tuple[int, float, tuple[float, ...], float]:
    level = entry.urgency.level
    inside = (-_stamp(entry.urgency.marked, 0.0) if level == USER
              else _stamp(entry.urgency.due, float("inf")) if level == SOON else 0.0)
    return LEVELS.index(level), inside, tuple(-value for value in entry.value), _stamp(entry.since, float("inf"))


def order(entries: Iterable[Entry[Item]], current: str = "") -> list[Entry[Item]]:
    """Most useful first; the item named `current` leads while it is still in the queue."""
    ranked = sorted(entries, key=_key)
    index = {entry.key: at for at, entry in reversed(list(enumerate(ranked)))}
    placed: set[int] = set()
    out: list[Entry[Item]] = []

    def place(at: int, path: frozenset[int]) -> None:
        if at in placed or at in path:
            return
        before = index.get(ranked[at].after)
        if before is not None:
            place(before, path | {at})
        if at not in placed:
            placed.add(at)
            out.append(ranked[at])

    if current in index:
        placed.add(index[current])
        out.append(ranked[index[current]])
    for at in range(len(ranked)):
        place(at, frozenset())
    return out


def state_dir() -> Path:
    return Path(os.environ.get("NEXT_STATE_DIR") or Path.home() / ".local/state/next")


def _pin_file(queue: str) -> Path:
    return state_dir() / f"{_UNSAFE.sub('_', queue)}.json"


def pinned(queue: str) -> str:
    """The key of the item /next last showed from this queue, or the empty string."""
    try:
        value = cast(object, json.loads(_pin_file(queue).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return ""
    current = cast(dict[str, object], value).get("current") if isinstance(value, dict) else None
    return current if isinstance(current, str) else ""


def pin(queue: str, key: str) -> None:
    path = _pin_file(queue)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    _ = temporary.write_text(json.dumps({"current": key}) + "\n", encoding="utf-8")
    _ = temporary.replace(path)


def top(entries: Iterable[Entry[Item]], queue: str) -> Entry[Item] | None:
    """The item /next shows: the pinned one while it is in the queue, else the first; it is then pinned."""
    current = pinned(queue)
    ranked = order(entries, current)
    if not ranked:
        return None
    if ranked[0].key != current:
        pin(queue, ranked[0].key)
    return ranked[0]
