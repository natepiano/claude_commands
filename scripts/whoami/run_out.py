"""How fast an account's week is being used, from the readings agent_notes.py logs.

`weighted_rate` is the pace the dailies footer extends to 100% used. Each rise in
used percent counts half as much per `HALF_LIFE` of age, and a refill only skips
the drop, so the night before a morning refill still counts toward the next day.
Time an account sat logged out is skipped too, so it does not read as quiet use.
Replayed hourly over 2026-10-05 to 10-07 (claude 1 and codex 2, three run-outs),
it cut the footer's run-out error from a 3.4 h median and 8.2 h p90, 2.9 h early
on average, to 1.7 h and 4.0 h, 0.3 h early. The footer it replaced took a plain
rate over the day since the last refill and scaled the time left by how much
sooner past run-outs came. Half-lives from 6 to 24 h scored within the noise of
one another; 12 h is their middle.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

STATE = Path.home() / ".local/state/agent-notes"
READINGS_LOG = STATE / "readings.jsonl"
HALF_LIFE = timedelta(hours=12)
# agent_notes.py logs the logged-in account every two minutes. A longer gap is an account that sat logged out, which says nothing about its pace in use.
MAX_GAP = timedelta(minutes=15)
# Weighted time the readings must cover before their pace is trusted: one point on the meter is a large step over a few minutes.
MIN_SPAN = timedelta(hours=1)
# Until the readings give a pace, the footer's is the week's use over at most this long since its refill.
WINDOW = timedelta(hours=24)


@dataclass(frozen=True, order=True)
class Reading:
    at: datetime
    used_percent: float


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


def weighted_rate(readings: list[Reading], end: float) -> float | None:
    """Percent used per second by `end`, each rise weighted by half per HALF_LIFE of age.

    Only time the log watched counts. A drop in used percent (a weekly refill or a
    redeemed reset), time at 100% and a gap longer than `MAX_GAP` are skipped; the
    readings on either side of one still count, weighted by age. None until the
    counted time, weighted, reaches `MIN_SPAN`.
    """
    used = seconds = 0.0
    for before, after in zip(readings, readings[1:]):
        at = after.at.timestamp()
        if at > end:
            break
        span = at - before.at.timestamp()
        if after.used_percent < before.used_percent or before.used_percent >= 100 or span > MAX_GAP.total_seconds():
            continue
        weight = math.pow(0.5, (end - at) / HALF_LIFE.total_seconds())
        used += weight * (after.used_percent - before.used_percent)
        seconds += weight * span
    return used / seconds if seconds >= MIN_SPAN.total_seconds() else None
