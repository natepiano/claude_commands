"""One reading of a test call's Mac result for both build reports."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal, cast

MacRunOutcome = Literal["passed", "passed_filter", "failed"]
OffloadDeclineReason = Literal[
    "off", "repo", "linux_only", "backoff", "blocked", "busy", "state",
    "unreachable", "mac_busy", "battery", "disk", "copy",
]
ShareMissReason = Literal["busy", "mac_busy", "lost", "runner", "state", "copy"]
DECLINE_REASONS: tuple[OffloadDeclineReason, ...] = (
    "off", "repo", "linux_only", "backoff", "blocked", "busy", "state",
    "unreachable", "mac_busy", "battery", "disk", "copy",
)
NOT_USABLE_REASONS: tuple[OffloadDeclineReason, ...] = (
    "blocked", "backoff", "unreachable", "battery", "disk", "off",
)
COUNTED_AGAINST_SHARE: tuple[ShareMissReason, ...] = (
    "busy", "mac_busy", "lost", "runner", "state", "copy",
)
MAC_STEP_CALLER = "verify-mac"
OFFLOADED_STEP = "({step}.caller IS '" + MAC_STEP_CALLER + "')"


class NoOffloadRecord:
    """A test call made without an offload result."""


class CacheReuse:
    """A call that reused a recorded result instead of running tests."""


class LocalReasonNotRecorded:
    """The caller supplied no recorded words for choosing natedev."""


@dataclass(frozen=True)
class LocalRunRequested:
    reason: str | LocalReasonNotRecorded


class RunnerResultUnavailable:
    """The runner returned no usable result."""


@dataclass(frozen=True)
class OffloadDeclined:
    reason: OffloadDeclineReason
    seconds: float


@dataclass(frozen=True)
class MacRunCompleted:
    outcome: MacRunOutcome
    seconds: float


@dataclass(frozen=True)
class MacRunLost:
    seconds: float


@dataclass(frozen=True)
class MacRunMatchedNoTest:
    seconds: float


class UnrecognizedOffloadRecord:
    """Offload columns that do not encode a known result."""


class DurationNotRecorded:
    """A duration that was absent or could not be read."""


OffloadRecord = (
    NoOffloadRecord | CacheReuse | LocalRunRequested | RunnerResultUnavailable
    | OffloadDeclined | MacRunCompleted | MacRunLost | MacRunMatchedNoTest
    | UnrecognizedOffloadRecord
)
NO_OFFLOAD_RECORD = NoOffloadRecord()
CACHE_REUSE = CacheReuse()
LOCAL_REASON_NOT_RECORDED = LocalReasonNotRecorded()
RUNNER_RESULT_UNAVAILABLE = RunnerResultUnavailable()
UNRECOGNIZED_OFFLOAD_RECORD = UnrecognizedOffloadRecord()
DURATION_NOT_RECORDED = DurationNotRecorded()


def offload_seconds(value: object) -> float | DurationNotRecorded:
    if isinstance(value, int | float) and not isinstance(value, bool):
        duration = float(value)
        if math.isfinite(duration) and duration >= 0:
            return duration
    return DURATION_NOT_RECORDED


def offload_record(
    mac: object, reason: object, local_reason: object, seconds_value: object,
    outcome: object = "ran",
) -> OffloadRecord:
    """Read the Mac columns and cache outcome of a verify test call."""
    if outcome in ("reused", "replayed"):
        return CACHE_REUSE
    if mac is None or mac == "":
        return NO_OFFLOAD_RECORD
    if not isinstance(mac, str) or not isinstance(reason, str | None):
        return UNRECOGNIZED_OFFLOAD_RECORD
    if mac == "declined" and reason == "local_flag" and seconds_value is None:
        if not isinstance(local_reason, str | None):
            return UNRECOGNIZED_OFFLOAD_RECORD
        return LocalRunRequested(local_reason if local_reason else LOCAL_REASON_NOT_RECORDED)
    if mac == "declined" and reason == "runner" and seconds_value is None:
        return RUNNER_RESULT_UNAVAILABLE
    if mac == "declined" and reason in ("repo", "linux_only") and seconds_value is None:
        return OffloadDeclined(reason, 0.0)
    duration = offload_seconds(seconds_value)
    if isinstance(duration, DurationNotRecorded):
        return UNRECOGNIZED_OFFLOAD_RECORD
    if mac in ("passed", "passed_filter", "failed") and reason in (None, ""):
        return MacRunCompleted(mac, duration)
    if mac == "lost" and reason in (None, ""):
        return MacRunLost(duration)
    if mac == "declined" and reason == "no_tests":
        return MacRunMatchedNoTest(duration)
    if mac == "declined" and reason in DECLINE_REASONS:
        return OffloadDeclined(reason, duration)
    return UNRECOGNIZED_OFFLOAD_RECORD


def is_mac_candidate(record: OffloadRecord) -> bool:
    return isinstance(record, (MacRunCompleted, MacRunLost, MacRunMatchedNoTest, RunnerResultUnavailable)) or (
        isinstance(record, OffloadDeclined) and record.reason not in ("repo", "linux_only")
    )


@dataclass(frozen=True)
class UsableShare:
    finished: int
    candidates: int


def share_of_records(records: Iterable[OffloadRecord]) -> UsableShare:
    finished = candidates = 0
    for record in records:
        if not is_mac_candidate(record):
            continue
        if isinstance(record, OffloadDeclined) and record.reason in NOT_USABLE_REASONS:
            continue
        if isinstance(record, (MacRunCompleted, MacRunMatchedNoTest)):
            finished += 1
            candidates += 1
        elif isinstance(record, OffloadDeclined):
            candidates += int(record.reason in COUNTED_AGAINST_SHARE)
        elif isinstance(record, MacRunLost):
            candidates += int("lost" in COUNTED_AGAINST_SHARE)
        elif isinstance(record, RunnerResultUnavailable):
            candidates += int("runner" in COUNTED_AGAINST_SHARE)
    return UsableShare(finished, candidates)


def usable_share(connection: sqlite3.Connection, start: str, end: str) -> UsableShare:
    """Finished Mac tests among usable candidates in the range [start, end)."""
    rows = cast(list[tuple[object, ...]], connection.execute(
        "SELECT mac, mac_reason, local_reason, mac_s, outcome FROM calls"
        + " WHERE started_at >= ? AND started_at < ? AND tool = 'verify.sh' AND verb = 'test'",
        (start, end),
    ).fetchall())
    return share_of_records(
        offload_record(mac, reason, local_reason, duration, outcome)
        for mac, reason, local_reason, duration, outcome in rows
    )


def is_mac_step(caller: object) -> bool:
    return caller == MAC_STEP_CALLER


def natedev_step_predicate(step: str = "steps") -> str:
    return f"NOT {OFFLOADED_STEP.format(step=step)}"
