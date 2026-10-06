"""When an account's week runs out, and how much sooner the past run-outs came.

`trailing_rate` is the raw pace: percent used per second across up to a day of
readings since the last refill, extended by the dailies footer to 100% used. An
account runs out because its use ran heavier than its average, so a trailing rate
predicts late: codex 2's run-out on 2026-10-05 and claude 1's on 2026-10-06 came
sooner than 27 of the 31 hourly footer predictions before them.

`record_run_outs` replays each run-out in the readings log hour by hour over its
last day and keeps actual time left over predicted time left. `lean` is the lower
quartile of those ratios; the footer multiplies its time left by it, so the
prediction leans early. Checked by leaving one run-out out: a lean learned from
the other cut late predictions from 19 of 21 to 5 of 21 for claude 1 and from 8 of
10 to 2 of 10 for codex 2.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from statistics import quantiles
from typing import TypedDict, cast

STATE = Path.home() / ".local/state/agent-notes"
READINGS_LOG = STATE / "readings.jsonl"
RUN_OUTS_LOG = STATE / "run_outs.jsonl"
WINDOW = timedelta(hours=24)
MIN_SPAN = timedelta(hours=1)
# A reset with this many percent left still ends a run-out: the user resets once alerted.
NEARLY_OUT = 5
# The lean is learned from this many of the latest run-outs, so it follows how the accounts are used now.
KEPT = 10


@dataclass(frozen=True, order=True)
class Reading:
    at: datetime
    used_percent: float


class RunOut(TypedDict):
    account: str
    ended: str
    ratios: list[float]


def read_readings(path: Path) -> dict[str, list[Reading]]:
    """Each account's readings in time order; a line that does not parse is skipped."""
    readings: dict[str, list[Reading]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return readings
    for line in lines:
        try:
            item = cast(object, json.loads(line))
            if not isinstance(item, dict):
                continue
            record = cast(dict[str, object], item)
            account = record.get("account")
            at_text = record.get("at")
            remaining = record.get("remaining")
            if not isinstance(account, str) or not isinstance(at_text, str):
                continue
            at = datetime.fromisoformat(at_text)
            if at.tzinfo is None or isinstance(remaining, bool) or not isinstance(remaining, (int, float)) or not math.isfinite(remaining):
                continue
            readings.setdefault(account, []).append(Reading(at, 100 - remaining))
        except (ValueError, TypeError, KeyError):
            continue
    return {account: sorted(found) for account, found in readings.items()}


def trailing_rate(readings: list[Reading], start: float, end: float) -> float | None:
    """Percent used per second between the first and last readings in [start, end], once they span an hour."""
    inside = [reading for reading in readings if start <= reading.at.timestamp() <= end]
    if len(inside) < 2:
        return None
    span = inside[-1].at.timestamp() - inside[0].at.timestamp()
    if span < MIN_SPAN.total_seconds():
        return None
    return (inside[-1].used_percent - inside[0].used_percent) / span


def episodes(readings: list[Reading]) -> list[tuple[list[Reading], Reading]]:
    """Each run-out: its readings since the refill before it, up to the first reading at its last level.

    A run-out reached 100% used, or was refilled with at most `NEARLY_OUT` percent left.
    """
    found: list[tuple[list[Reading], Reading]] = []
    start = 0
    for index in range(1, len(readings) + 1):
        ongoing = index == len(readings)
        if not ongoing and readings[index].used_percent >= readings[index - 1].used_percent:
            continue
        span = readings[start:index]
        top = span[-1].used_percent
        if top >= 100 or (not ongoing and top >= 100 - NEARLY_OUT):
            reached = next(reading for reading in span if reading.used_percent >= top)
            found.append((span[:span.index(reached) + 1], reached))
        start = index
    return found


def ratios(span: list[Reading], reached: Reading) -> list[float]:
    """Actual over predicted time to the run-out's last level, from the footer's pace, hourly over its last day."""
    found: list[float] = []
    end = reached.at.timestamp()
    for hours in range(1, int(WINDOW / timedelta(hours=1)) + 1):
        before = [reading for reading in span if reading.at.timestamp() <= end - hours * 3600]
        if not before:
            break
        sample = before[-1].at.timestamp()
        rate = trailing_rate(span, max(span[0].at.timestamp(), sample - WINDOW.total_seconds()), sample)
        if rate is None or rate <= 0:
            continue
        found.append((end - sample) * rate / (reached.used_percent - before[-1].used_percent))
    return found


def read_run_outs(path: Path) -> list[RunOut]:
    """Recorded run-outs in the order they ended, each once."""
    found: dict[tuple[str, str], RunOut] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return []
    for line in lines:
        try:
            item = cast(object, json.loads(line))
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        record = cast(dict[str, object], item)
        account, ended, values = record.get("account"), record.get("ended"), record.get("ratios")
        if not isinstance(account, str) or not isinstance(ended, str) or not isinstance(values, list):
            continue
        numbers = [float(value) for value in cast(list[object], values)
                   if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0]
        found[(account, ended)] = {"account": account, "ended": ended, "ratios": numbers}
    return sorted(found.values(), key=lambda run_out: datetime.fromisoformat(run_out["ended"]).timestamp())


def lean(path: Path) -> float:
    """The lower quartile of the latest run-outs' ratios; 1 until a run-out is recorded."""
    pooled = [ratio for run_out in read_run_outs(path)[-KEPT:] for ratio in run_out["ratios"]]
    if len(pooled) < 2:
        return pooled[0] if pooled else 1.0
    # Inclusive: with few ratios, the exclusive method extrapolates below the smallest one.
    return quantiles(pooled, n=4, method="inclusive")[0]


def record_run_outs(readings_path: Path, run_outs_path: Path) -> list[str]:
    """Append each run-out in the readings that is not recorded yet; one line for each."""
    known = {(run_out["account"], run_out["ended"]) for run_out in read_run_outs(run_outs_path)}
    lines: list[str] = []
    for account, readings in read_readings(readings_path).items():
        for span, reached in episodes(readings):
            ended = reached.at.isoformat(timespec="seconds")
            found = ratios(span, reached)
            if (account, ended) in known or not found:
                continue
            record: RunOut = {"account": account, "ended": ended, "ratios": [round(ratio, 3) for ratio in found]}
            run_outs_path.parent.mkdir(parents=True, exist_ok=True)
            with run_outs_path.open("a", encoding="utf-8") as output:
                _ = output.write(json.dumps(record, separators=(",", ":")) + "\n")
            lines.append(f"{account}: run-out at {ended} recorded; footer lean now {lean(run_outs_path):.2f}")
    return lines
