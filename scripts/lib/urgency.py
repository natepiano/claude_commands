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

The order is `urgent` (the levels above), `oldest` or `newest` (by age; the levels break a tie).
In each order an item the user moved to the end goes after every other item, the first moved
first, until the user calls it important again.

The item /next showed in the urgent order stays current until it leaves the queue (its verdict or
answer), even when a more urgent item arrives; a user mark takes the slot after it. The oldest and
newest orders always show the oldest or newest item, and pin it. `top` holds the pin and the moved
items, one small JSON file per queue under NEXT_STATE_DIR (default ~/.local/state/next).
`top_across` gives bare /next its one item from several queues.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Generic, TypeVar, cast

USER = "user"
BLOCKS = "blocks"
SOON = "soon"
NORMAL = "normal"
LATER = "later"
LEVELS = (USER, BLOCKS, SOON, NORMAL, LATER)
URGENT = "urgent"
OLDEST = "oldest"
NEWEST = "newest"
ORDERS = (URGENT, OLDEST, NEWEST)
# The stored form of a minute: the form every queue already writes its times in.
MINUTE = "%Y-%m-%dT%H:%M"
_CELL_DUE = re.compile(r"^due (?P<due>\S+)$")
_CELL_MARKED = re.compile(r"^marked (?P<marked>\S+)$")
_CELL_AFTER = re.compile(r"^after (?P<after>\S+)$")
_UNSAFE = re.compile(r"[^\w.-]+")
Item = TypeVar("Item")


# The /next word that adds every production below the showrunner's own.
ALL_BELOW = "all-below"


def scope_queue(name: str, below: bool) -> str:
    """The pin name of a list: an all-below show keeps its own pin, so it never moves the default's."""
    return f"{name}-{ALL_BELOW}" if below else name


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


def _level(entry: Entry[Item]) -> tuple[int, float]:
    level = entry.urgency.level
    inside = (-_stamp(entry.urgency.marked, 0.0) if level == USER
              else _stamp(entry.urgency.due, float("inf")) if level == SOON else 0.0)
    return LEVELS.index(level), inside


def _key(entry: Entry[Item]) -> tuple[int, float, tuple[float, ...], float]:
    return *_level(entry), tuple(-value for value in entry.value), _stamp(entry.since, float("inf"))


def _checked(by: str) -> str:
    if by not in ORDERS:
        raise UrgencyError(f"order {by!r} is not one of {', '.join(ORDERS)}")
    return by


def _moved(entry: Entry[Item], ended: Mapping[str, datetime]) -> datetime | None:
    """When the user moved this item to the end, unless they called it important since then."""
    moved = ended.get(entry.key)
    marked = entry.urgency.marked
    if moved is None or (marked is not None and marked >= moved.replace(second=0, microsecond=0)):
        return None
    return moved


def _age(entry: Entry[Item], by: str) -> tuple[bool, float]:
    """The age part of the key: nothing for `urgent`; an item with no age goes last."""
    if by == URGENT:
        return False, 0.0
    if entry.since is None:
        return True, 0.0
    return False, -entry.since.timestamp() if by == NEWEST else entry.since.timestamp()


def order(entries: Iterable[Entry[Item]], current: str = "", by: str = URGENT,
          ended: Mapping[str, datetime] | None = None) -> list[Entry[Item]]:
    """In the order `by`, an item named in `ended` last; the item named `current` leads while it is in the queue."""
    _ = _checked(by)
    ends = ended or {}

    def key(entry: Entry[Item]) -> tuple[bool, float, tuple[bool, float], tuple[int, float, tuple[float, ...], float]]:
        moved = _moved(entry, ends)
        return moved is not None, _stamp(moved, 0.0), _age(entry, by), _key(entry)

    ranked = sorted(entries, key=key)
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


@dataclass(frozen=True)
class QueueState:
    """A queue's file: the item /next showed, the queue it came from (bare /next only) and the moved items."""

    current: str = ""
    source: str = ""
    ended: Mapping[str, datetime] = field(default_factory=dict[str, datetime])


def _read_state(queue: str) -> QueueState:
    try:
        value = cast(object, json.loads(_pin_file(queue).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return QueueState()
    fields = cast(dict[str, object], value) if isinstance(value, dict) else {}
    current, source, stored = fields.get("current"), fields.get("source"), fields.get("ended")
    ended: dict[str, datetime] = {}
    for key, when in (cast(dict[str, object], stored) if isinstance(stored, dict) else {}).items():
        if isinstance(when, str):
            try:
                ended[key] = datetime.fromisoformat(when)
            except ValueError:
                continue
    return QueueState(current if isinstance(current, str) else "", source if isinstance(source, str) else "", ended)


def _write_state(queue: str, state: QueueState) -> None:
    path = _pin_file(queue)
    path.parent.mkdir(parents=True, exist_ok=True)
    stored: dict[str, object] = {"current": state.current}
    if state.source:
        stored["source"] = state.source
    if state.ended:
        stored["ended"] = {key: when.isoformat(timespec="seconds") for key, when in state.ended.items()}
    temporary = path.with_suffix(".tmp")
    _ = temporary.write_text(json.dumps(stored) + "\n", encoding="utf-8")
    _ = temporary.replace(path)


def pinned(queue: str) -> str:
    """The key of the item /next last showed from this queue, or the empty string."""
    return _read_state(queue).current


def ended(queue: str) -> dict[str, datetime]:
    """The items the user moved to the end of this queue, with when."""
    return dict(_read_state(queue).ended)


def pin(queue: str, key: str, source: str = "") -> None:
    state = _read_state(queue)
    _write_state(queue, QueueState(key, source, state.ended))


def queue_order(entries: Iterable[Entry[Item]], queue: str) -> list[Entry[Item]]:
    """The queue in the urgent order, with its pin and its moved items: the order its table shows."""
    state = _read_state(queue)
    return order(entries, state.current, URGENT, state.ended)


def _pin_first(queue: str, ranked: Sequence[Entry[Item]], source: str = "") -> None:
    """Pin the first item and forget a moved item that left the queue; write only on a change."""
    state = _read_state(queue)
    present = {entry.key for entry in ranked}
    kept = {key: when for key, when in state.ended.items() if key in present}
    if ranked[0].key != state.current or source != state.source or kept != dict(state.ended):
        _write_state(queue, QueueState(ranked[0].key, source, kept))


def top(entries: Iterable[Entry[Item]], queue: str, by: str = URGENT) -> Entry[Item] | None:
    """The item /next shows, which is then pinned: in the urgent order the pinned one while it is
    in the queue, else the first; in the oldest or newest order the first."""
    state = _read_state(queue)
    ranked = order(entries, state.current if _checked(by) == URGENT else "", by, state.ended)
    if not ranked:
        return None
    _pin_first(queue, ranked)
    return ranked[0]


def top_across(queues: Mapping[str, Sequence[Entry[Item]]], name: str, by: str = URGENT) -> tuple[str, Entry[Item]] | None:
    """The one item bare /next shows from several queues, and its queue; it is pinned in both.

    In the urgent order the item it showed last, under `name`, leads while it waits and is not moved.
    Else each queue's first item in the order `by` competes: one moved to the end last, then by age
    for oldest or newest, then by level, then the oldest. A value never compares across queues.
    """
    state = _read_state(name)
    firsts: list[tuple[tuple[bool, float, tuple[bool, float], tuple[int, float], float], str, Entry[Item]]] = []
    for queue, entries in queues.items():
        ends = ended(queue)
        ranked = order(entries, "", by, ends)
        if not ranked:
            continue
        if _checked(by) == URGENT and queue == state.source:
            kept = next((entry for entry in ranked if entry.key == state.current and _moved(entry, ends) is None), None)
            if kept is not None:
                _pin_first(queue, [kept, *ranked])
                return queue, kept
        first = ranked[0]
        moved = _moved(first, ends)
        firsts.append(((moved is not None, _stamp(moved, 0.0), _age(first, by), _level(first),
                        _stamp(first.since, float("inf"))), queue, first))
    if not firsts:
        return None
    _, queue, shown = min(firsts, key=lambda found: found[0])
    _pin_first(queue, [shown, *queues[queue]])
    _write_state(name, QueueState(shown.key, queue))
    return queue, shown


def move_to_end(queue: str, key: str, now: datetime) -> None:
    """Move the item `key` to the end of its queue; it is no longer current there."""
    state = _read_state(queue)
    ends = dict(state.ended)
    ends[key] = now.replace(microsecond=0)
    _write_state(queue, QueueState("" if state.current == key else state.current, state.source, ends))
