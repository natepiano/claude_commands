"""Hold history and current coordination records shared by the build reports."""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Literal, TypedDict, cast

import store

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "build_targets"))
import targets as targets  # Adds the shared script directories to sys.path.
import build_hold
import mac_test
from user_zone import user_zone

HoldTarget = Literal["mac", "natedev"]


@dataclass(frozen=True)
class CurrentHold:
    key: str
    holder: str
    reason: str
    since: datetime


@dataclass(frozen=True)
class NatedevHoldsNow:
    in_force: tuple[CurrentHold, ...]


@dataclass(frozen=True)
class MacHoldsNow:
    in_force: tuple[CurrentHold, ...]


@dataclass(frozen=True)
class HoldStateUnreadable:
    detail: str


@dataclass(frozen=True)
class CurrentHolds:
    host: str
    natedev: NatedevHoldsNow | HoldStateUnreadable
    mac: MacHoldsNow | HoldStateUnreadable


def read_current_holds() -> CurrentHolds:
    natedev: NatedevHoldsNow | HoldStateUnreadable
    mac: MacHoldsNow | HoldStateUnreadable
    try:
        holders = build_hold.natedev_holds(build_hold.holder_directory())
        natedev = NatedevHoldsNow(
            tuple(CurrentHold(item.key, item.name, item.purpose, item.since) for item in holders.in_force),
        )
    except (OSError, UnicodeError, mac_test.StateReadError) as error:
        natedev = HoldStateUnreadable(str(error))
    try:
        block = mac_test.read_claim_block(mac_test.state_paths())
        mac = MacHoldsNow(()) if isinstance(block, mac_test.NoMacBlock) else MacHoldsNow((
            CurrentHold(block.since.isoformat(), block.holder, block.reason, block.since),
        ))
    except (OSError, UnicodeError, mac_test.StateReadError) as error:
        mac = HoldStateUnreadable(str(error))
    return CurrentHolds(store.host_name(), natedev, mac)


def shown_time(instant: datetime) -> str:
    return instant.astimezone(user_zone()).strftime("%Y-%m-%d %H:%M %Z")


def now_lines(current: CurrentHolds) -> list[str]:
    lines: list[str] = []
    if isinstance(current.mac, HoldStateUnreadable):
        lines.append(f"Now: Mac state unreadable: {current.mac.detail}")
    else:
        for hold in current.mac.in_force:
            lines.append(f"Now: Mac blocked by {hold.holder} for {hold.reason} since {shown_time(hold.since)}")
    if isinstance(current.natedev, HoldStateUnreadable):
        lines.append(f"Now: natedev state unreadable: {current.natedev.detail}")
    else:
        for hold in current.natedev.in_force:
            lines.append(f"Now: natedev held by {hold.holder} for {hold.reason} since {shown_time(hold.since)}")
    return lines


@dataclass(frozen=True)
class HoldBeginningNotRecorded:
    earliest: datetime


class NoLaterHoldEvent(Enum):
    VALUE = "no later event"


@dataclass(frozen=True)
class NextHoldEvent:
    at: datetime


@dataclass(frozen=True)
class HoldEndNotRecorded:
    next_event: NextHoldEvent | NoLaterHoldEvent


class NoPlannedEnd(Enum):
    VALUE = "no planned end"


@dataclass(frozen=True)
class PlannedEnd:
    at: datetime


def planned_end(value: str) -> PlannedEnd | NoPlannedEnd:
    try:
        instant = datetime.fromisoformat(value)
    except ValueError:
        return NoPlannedEnd.VALUE
    return PlannedEnd(instant) if instant.utcoffset() is not None else NoPlannedEnd.VALUE


class NoHoldEventRecorded(Enum):
    VALUE = "no event recorded"


@dataclass(frozen=True)
class RecordedSince:
    at: datetime


class HoldRecordingStarts(TypedDict):
    mac: RecordedSince | NoHoldEventRecorded
    natedev: RecordedSince | NoHoldEventRecorded


@dataclass(frozen=True)
class OpenHold:
    through: datetime


@dataclass(frozen=True)
class HoldInterval:
    host: str
    target: HoldTarget
    key: str
    holder: str
    reason: str
    planned_end: PlannedEnd | NoPlannedEnd
    began: datetime | HoldBeginningNotRecorded
    ended: datetime | HoldEndNotRecorded | OpenHold


@dataclass(frozen=True)
class HoldHistory:
    intervals: tuple[HoldInterval, ...]
    recorded_since: HoldRecordingStarts


def key_is_present(current: CurrentHolds, host: str, target: HoldTarget, key: str) -> bool:
    if host != current.host:
        return False
    if target == "natedev":
        return isinstance(current.natedev, NatedevHoldsNow) and any(
            item.key == key for item in current.natedev.in_force
        )
    if not isinstance(current.mac, MacHoldsNow):
        return False
    try:
        since = datetime.fromisoformat(key)
    except ValueError:
        return False
    return any(item.since == since for item in current.mac.in_force)


def read_hold_history(connection: sqlite3.Connection, current: CurrentHolds, now: datetime) -> HoldHistory:
    """Read intervals once, including beginnings before every requested window."""
    intervals: list[HoldInterval] = []
    rows = cast(list[tuple[str, str, str, str, str, str, str | None, str | None, int, str | None, str | None]], connection.execute(
        "SELECT host, target, hold_key, holder, reason, until, began_at, ended_at, later_row, previous_at, next_at FROM holds",
    ).fetchall())
    first_rows = cast(list[tuple[str, str]], connection.execute(
        "SELECT target, min(at) FROM hold_events GROUP BY target",
    ).fetchall())
    recorded_since: HoldRecordingStarts = {"mac": NoHoldEventRecorded.VALUE, "natedev": NoHoldEventRecorded.VALUE}
    for target, at in first_rows:
        if target in ("mac", "natedev"):
            recorded_since[target] = RecordedSince(datetime.fromisoformat(at))
    for host, target, key, holder, reason, until, began_at, ended_at, later_row, previous_at, next_at in rows:
        if target not in ("mac", "natedev"):
            continue
        recorded = recorded_since[target]
        if not isinstance(recorded, RecordedSince):
            continue
        earliest = datetime.fromisoformat(previous_at) if previous_at is not None else recorded.at
        began = datetime.fromisoformat(began_at) if began_at is not None else HoldBeginningNotRecorded(earliest)
        ended: datetime | HoldEndNotRecorded | OpenHold = (
            datetime.fromisoformat(ended_at) if ended_at is not None else HoldEndNotRecorded(
                NextHoldEvent(datetime.fromisoformat(next_at)) if next_at is not None else NoLaterHoldEvent.VALUE,
            )
        )
        if (isinstance(ended, HoldEndNotRecorded) and isinstance(began, datetime) and began <= now
                and not later_row and key_is_present(current, host, target, key)):
            ended = OpenHold(now)
        intervals.append(HoldInterval(host, target, key, holder, reason, planned_end(until), began, ended))
    return HoldHistory(tuple(intervals), recorded_since)


@dataclass(frozen=True)
class HoldDuration:
    seconds: float
    incomplete: bool


def union_seconds(spans: list[tuple[datetime, datetime]]) -> float:
    if not spans:
        return 0.0
    ordered = sorted(spans)
    first, last = ordered[0]
    total = 0.0
    for began, ended in ordered[1:]:
        if began <= last:
            last = max(last, ended)
        else:
            total += (last - first).total_seconds()
            first, last = began, ended
    return total + (last - first).total_seconds()


def hold_duration(history: HoldHistory, target: HoldTarget, start: str, end: str) -> HoldDuration:
    first, last = datetime.fromisoformat(start), datetime.fromisoformat(end)
    spans: list[tuple[datetime, datetime]] = []
    recorded = history.recorded_since[target]
    incomplete = isinstance(recorded, NoHoldEventRecorded) or recorded.at > first
    for interval in history.intervals:
        if interval.target != target or not interval_overlaps(interval, first, last):
            continue
        began, ended = interval.began, interval.ended
        if isinstance(began, HoldBeginningNotRecorded) or isinstance(ended, HoldEndNotRecorded):
            incomplete = True
        else:
            through = ended.through if isinstance(ended, OpenHold) else ended
            low, high = max(first, began), min(last, through)
            if high > low:
                spans.append((low, high))
    return HoldDuration(union_seconds(spans), incomplete)


def duration_text(duration: HoldDuration) -> str:
    minutes = int(duration.seconds // 60)
    return ("at least " if duration.incomplete else "") + f"{minutes // 60}:{minutes % 60:02d}"


def coverage_lines(history: HoldHistory) -> list[str]:
    lines: list[str] = []
    for target in ("mac", "natedev"):
        recorded = history.recorded_since[target]
        if isinstance(recorded, NoHoldEventRecorded):
            lines.append(f"{target} holds: no event recorded yet")
            continue
        intervals = [interval for interval in history.intervals if interval.target == target]
        missing_end = sum(isinstance(interval.ended, HoldEndNotRecorded) for interval in intervals)
        missing_beginning = sum(isinstance(interval.began, HoldBeginningNotRecorded) for interval in intervals)
        lines.append(f"{target} holds recorded since {shown_time(recorded.at)}: {len(intervals)} intervals,"
                     + f" {missing_end} end not recorded, {missing_beginning} began not recorded")
    return lines


def interval_overlaps(interval: HoldInterval, first: datetime, last: datetime) -> bool:
    began, ended = interval.began, interval.ended
    earliest = began if isinstance(began, datetime) else began.earliest
    if earliest >= last:
        return False
    if isinstance(ended, HoldEndNotRecorded):
        return isinstance(ended.next_event, NoLaterHoldEvent) or ended.next_event.at > first
    latest = ended.through if isinstance(ended, OpenHold) else ended
    return latest >= first if isinstance(began, HoldBeginningNotRecorded) else latest > first


def interval_lines(history: HoldHistory, start: str, end: str) -> list[str]:
    first, last = datetime.fromisoformat(start), datetime.fromisoformat(end)
    lines: list[str] = []
    for interval in history.intervals:
        began, ended = interval.began, interval.ended
        if not interval_overlaps(interval, first, last):
            continue
        beginning = shown_time(began) if isinstance(began, datetime) else "began not recorded"
        ending = shown_time(ended) if isinstance(ended, datetime) else "open" if isinstance(ended, OpenHold) else "end not recorded"
        planned = f"; planned end {shown_time(interval.planned_end.at)}" if isinstance(interval.planned_end, PlannedEnd) else ""
        lines.append(f"- {interval.target} ({interval.host}), {interval.holder} for {interval.reason}: {beginning} to {ending}{planned}")
    return lines
