"""How fast an account's week is being used, from the readings agent_notes.py logs.

`weighted_rate` is the pace the dailies footer extends to 100% used. Each rise in
used percent counts half as much per `HALF_LIFE` of age, and a refill only skips
the drop, so the night before a morning refill still counts toward the next day.
Time an account sat logged out is skipped too, so it does not read as quiet use.
Replayed hourly over 2026-10-05 to 10-07 (claude1 and codex2, three run-outs),
it cut the footer's run-out error from a 3.4 h median and 8.2 h p90, 2.9 h early
on average, to 1.7 h and 4.0 h, 0.3 h early. The footer it replaced took a plain
rate over the day since the last refill and scaled the time left by how much
sooner past run-outs came. Half-lives from 6 to 24 h scored within the noise of
one another; 12 h is their middle.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import NotRequired, TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.whoami.note_file import canonical_label

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


class StoredReading(TypedDict):
    account: str
    at: str
    remaining: NotRequired[float]
    credits: NotRequired[float]


@dataclass(frozen=True, order=True)
class CreditReading:
    at: datetime
    credits: float


@dataclass(frozen=True)
class CreditsUsed:
    used: float
    observed_from: datetime
    observed_to: datetime


@dataclass(frozen=True)
class NoCreditReading:
    """No numeric credit observation exists for this account by the cutoff."""


@dataclass(frozen=True)
class UsageUnknown:
    reason: str


def _reading_records(path: Path) -> list[StoredReading]:
    """Parse the shared observations once for either allowance or credits."""
    records: list[StoredReading] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return records
    for line in lines:
        try:
            item = cast(object, json.loads(line))
            if not isinstance(item, dict):
                continue
            value = cast(dict[str, object], item)
            if not isinstance(value.get("account"), str) or not isinstance(value.get("at"), str):
                continue
            at = datetime.fromisoformat(cast(str, value["at"]))
            if at.tzinfo is not None:
                record = StoredReading(account=canonical_label(cast(str, value["account"])), at=cast(str, value["at"]))
                if _numeric(value.get("remaining")):
                    record["remaining"] = cast(float, value["remaining"])
                if _numeric(value.get("credits")):
                    record["credits"] = cast(float, value["credits"])
                records.append(record)
        except (ValueError, TypeError):
            continue
    return records


def _numeric(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def read_readings(path: Path) -> dict[str, list[Reading]]:
    """Each account's readings in time order; a line that does not parse is skipped."""
    readings: dict[str, list[Reading]] = {}
    for record in _reading_records(path):
        remaining = record.get("remaining")
        if _numeric(remaining):
            readings.setdefault(record["account"], []).append(
                Reading(datetime.fromisoformat(record["at"]), 100 - cast(float, remaining)))
    return {account: sorted(found) for account, found in readings.items()}


def read_credit_readings(path: Path) -> dict[str, list[CreditReading]]:
    readings: dict[str, list[CreditReading]] = {}
    for record in _reading_records(path):
        credits = record.get("credits")
        if _numeric(credits) and cast(float, credits) >= 0:
            readings.setdefault(record["account"], []).append(
                CreditReading(datetime.fromisoformat(record["at"]), cast(float, credits)))
    return {account: sorted(found) for account, found in readings.items()}


def credits_used(readings: dict[str, list[CreditReading]], label: str, *,
                 since: datetime, until: datetime) -> CreditsUsed | NoCreditReading | UsageUnknown:
    available = sorted(item for item in readings.get(canonical_label(label), []) if item.at <= until)
    if not available:
        return NoCreditReading()
    before = [item for item in available if item.at <= since]
    start = before[-1] if before else available[0]
    observed = [item for item in available if item.at >= start.at]
    newest = observed[-1]
    if newest.at < until - timedelta(minutes=10):
        return UsageUnknown("the newest credit reading is older than 10 minutes")
    if newest.at - max(start.at, since) < timedelta(minutes=30):
        return UsageUnknown("credit observations cover less than 30 minutes of the hour")
    used = sum(max(0, old.credits - new.credits) for old, new in zip(observed, observed[1:]))
    return CreditsUsed(used, start.at, newest.at)


def weighted_rate(readings: list[Reading], end: float) -> float | None:
    """Percent used per second by `end`, each rise weighted by half per HALF_LIFE of age.

    Only time the log watched counts. A drop in used percent (a weekly refill or a
    redeemed reset), time at 100% and a gap longer than `MAX_GAP` are skipped; the
    readings on either side of one still count, weighted by age. Time before an
    account's first rise after a refill, or after the log began, is skipped too: an
    account untouched since then is waiting its turn, not quietly in use, and one
    still waiting after `MIN_SPAN` of watched time has a pace of 0. Otherwise None
    until the counted time, weighted, reaches `MIN_SPAN`.
    """
    used = seconds = waited = 0.0
    waiting = True
    for before, after in zip(readings, readings[1:]):
        at = after.at.timestamp()
        if at > end:
            break
        span = at - before.at.timestamp()
        if after.used_percent < before.used_percent:
            waiting, waited = True, 0.0
            continue
        if after.used_percent > before.used_percent:
            waiting = False
        if before.used_percent >= 100 or span > MAX_GAP.total_seconds():
            continue
        if waiting:
            waited += span
            continue
        weight = math.pow(0.5, (end - at) / HALF_LIFE.total_seconds())
        used += weight * (after.used_percent - before.used_percent)
        seconds += weight * span
    if waiting and waited >= MIN_SPAN.total_seconds():
        return 0.0
    return used / seconds if seconds >= MIN_SPAN.total_seconds() else None
