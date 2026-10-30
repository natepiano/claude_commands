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
PHONE_WIDTH = 50


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


def wrap_text(text: str, *, initial_indent: str = "", subsequent_indent: str = "") -> str:
    """Wrap at spaces, keeping each word whole on its line."""
    lines: list[str] = []
    line = initial_indent
    for word in text.split():
        separator = " " if line.strip() else ""
        if line.strip() and len(line) + len(separator) + len(word) > PHONE_WIDTH:
            lines.append(line)
            line = subsequent_indent
            separator = ""
        if not line.strip() and len(line) + len(word) > PHONE_WIDTH:
            lines.append(word)
            line = subsequent_indent
        else:
            line += separator + word
    if line.strip():
        lines.append(line)
    return "\n".join(lines)


def blocks(title: str, rows: Sequence[MeasureRow]) -> str:
    heading = [wrap_text(title)]
    if not rows:
        return "\n".join([*heading, "none"])
    shared_window = all(row.window == rows[0].window for row in rows)
    shared_source = all(row.source == rows[0].source for row in rows)
    if shared_window:
        heading.append(wrap_text("Window: " + rows[0].window, subsequent_indent="    "))
    if shared_source:
        heading.append(wrap_text("Source: " + rows[0].source, subsequent_indent="    "))
    output = ["\n".join(heading)]
    for row in rows:
        fields = [("value", row.value), ("n", str(row.n))]
        if not shared_window:
            fields.append(("window", row.window))
        if not shared_source:
            fields.append(("source", row.source))
        output.append("\n".join([
            wrap_text(row.measure),
            *(wrap_text(f"{label}: {value}", initial_indent="  ", subsequent_indent="    ")
              for label, value in fields),
        ]))
    return "\n\n".join(output)


def window_bounds(since_text: str, until_text: str, now: datetime) -> Window:
    until = required_time(until_text) if until_text else now
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", until_text):
        until += timedelta(days=1)
    since = required_time(since_text) if since_text else until - timedelta(days=7)
    if since >= until:
        raise ValueError("--since must precede the end of --until")
    return Window(since, until)
