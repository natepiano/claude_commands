"""Read typed log fields and format measurement rows for hierarchy reports."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import math
import re
from typing import cast

from scripts.lib.user_zone import user_zone

JsonObject = dict[str, object]


class TimeNotRecorded(Enum):
    MISSING = "time not recorded"


RecordedTime = datetime | TimeNotRecorded


@dataclass(frozen=True)
class Window:
    since: datetime
    until: datetime

    def contains(self, at: RecordedTime) -> bool:
        return isinstance(at, datetime) and self.since <= at < self.until

    @property
    def label(self) -> str:
        start = self.since.astimezone(user_zone()).strftime("%Y-%m-%d %H:%M %Z")
        end = self.until.astimezone(user_zone()).strftime("%Y-%m-%d %H:%M %Z")
        return f"{start} to {end} (end excluded)"


@dataclass(frozen=True)
class MeasureRow:
    measure: str
    value: str
    n: int
    window: str
    source: str


@dataclass(frozen=True)
class SourceProblem:
    source: str
    reason: str


def field_text(record: JsonObject, key: str) -> str:
    if key not in record:
        return ""
    value = record[key]
    if not isinstance(value, str):
        raise ValueError(f"{key}: expected text")
    return value


def field_number(record: JsonObject, key: str) -> float:
    value = record.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError(f"{key}: expected a finite number")
    return float(value)


def field_flag(record: JsonObject, key: str) -> bool:
    value = record.get(key, False)
    if not isinstance(value, bool):
        raise ValueError(f"{key}: expected a boolean")
    return value


def moment(value: object) -> RecordedTime:
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value, timezone.utc)
        if isinstance(value, str) and value:
            at = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return at.replace(tzinfo=user_zone()) if at.tzinfo is None else at
    except (ValueError, OverflowError, OSError):
        pass
    return TimeNotRecorded.MISSING


def required_time(value: object) -> datetime:
    at = moment(value)
    if not isinstance(at, datetime):
        raise ValueError(f"invalid or missing timestamp: {value!r}")
    return at


def record_time(record: JsonObject) -> datetime:
    for key in ("timestamp_epoch", "time", "at", "timestamp"):
        if key in record:
            return required_time(record[key])
    raise ValueError("timestamp not recorded")


def object_record(value: object) -> JsonObject:
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return cast(JsonObject, value)


def source_problem_rows(problems: Sequence[SourceProblem], window: Window) -> list[MeasureRow]:
    return [MeasureRow("source unavailable", problem.reason, 0, window.label, problem.source) for problem in problems]


def table(title: str, rows: Sequence[MeasureRow]) -> str:
    headings = ("Measure", "Value", "n", "Window", "Source")
    cells = [headings, *((row.measure, row.value, str(row.n), row.window, row.source) for row in rows)]
    widths = [max(len(cell[index]) for cell in cells) for index in range(5)]
    lines = ["  ".join(value.ljust(widths[index]) for index, value in enumerate(row)).rstrip() for row in cells]
    return title + "\n" + "\n".join(lines)


def window_bounds(since_text: str, until_text: str, now: datetime) -> Window:
    until = required_time(until_text) if until_text else now
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", until_text):
        until += timedelta(days=1)
    since = required_time(since_text) if since_text else until - timedelta(days=7)
    if since >= until:
        raise ValueError("--since must precede the end of --until")
    return Window(since, until)
