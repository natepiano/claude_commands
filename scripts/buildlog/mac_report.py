"""Mac test outcomes from the build log, with no contact with the Mac."""

from __future__ import annotations

import math
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from enum import Enum, auto
from typing import Literal, TypeVar, cast

import index
import holds
import store
from offload import KnownMacJob, LostRunLeftovers, MacHolder, UnreadableMacHolder
from mac_outcome import (
    COUNTED_AGAINST_SHARE, NOT_USABLE_REASONS, CacheReuse, LocalReasonNotRecorded,
    LocalRunRequested, MacRunCompleted, MacRunLost, MacRunMatchedNoTest,
    NoOffloadRecord, OffloadDeclined, OffloadDeclineReason, OffloadRecord,
    RunnerResultUnavailable, ShareMissReason, UnrecognizedOffloadRecord,
    UsableShare, is_mac_candidate, is_mac_step, offload_record, share_of_records,
)

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from user_zone import user_zone  # noqa: E402

LOCAL_MAC_UNAVAILABLE_PATTERNS = (
    r"\bmac\b.*\b(?:unavailable|busy|blocked|unreachable|offline|not available|not reachable)\b",
    r"\b(?:unavailable|busy|blocked|unreachable|offline)\b.*\bmac\b",
    r"\bbackoff\b",
    r"\bbattery\b",
    r"\b(?:ssh|connection)\b.*\b(?:failed|refused|timed out)\b",
    r"\b(?:cannot|could not)\s+(?:reach|connect to)\s+(?:the\s+)?mac\b",
)
TEST_TREE_LOOKBACK = timedelta(hours=24)


class NotRecorded(Enum):
    VALUE = "not recorded"


@dataclass(frozen=True)
class RecordedTarget:
    name: str


class MacHolderNotRecorded(Enum):
    VALUE = "holder not recorded"


RecordedBusyHolder = MacHolder | MacHolderNotRecorded


def recorded_busy_holder(value: object) -> RecordedBusyHolder:
    if not isinstance(value, str) or not value:
        return MacHolderNotRecorded.VALUE
    if value == "leftovers":
        return LostRunLeftovers()
    if value == "unreadable":
        return UnreadableMacHolder()
    return KnownMacJob(value)


def holder_text(holder: RecordedBusyHolder) -> str:
    if isinstance(holder, KnownMacJob):
        return holder.what
    if isinstance(holder, LostRunLeftovers):
        return "lost-run leftovers"
    if isinstance(holder, UnreadableMacHolder):
        return "holder unreadable"
    return holder.value


@dataclass(frozen=True)
class MacTestCall:
    call_id: str
    started_at: str
    decision: OffloadRecord
    target: RecordedTarget | NotRecorded
    hold_wait_s: float | NotRecorded
    holder: RecordedBusyHolder


def nonnegative_seconds(value: object) -> float | NotRecorded:
    if isinstance(value, int | float) and not isinstance(value, bool):
        seconds = float(value)
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    return NotRecorded.VALUE


def read_test_calls(connection: sqlite3.Connection, start: str, end: str) -> list[MacTestCall]:
    rows = cast(list[tuple[object, ...]], connection.execute(
        "SELECT id, started_at, outcome, mac, mac_reason, local_reason, mac_s, target, hold_wait_s, mac_holder"
        + " FROM calls WHERE started_at >= ? AND started_at < ?"
        + " AND tool = 'verify.sh' AND verb = 'test' ORDER BY started_at, id",
        (start, end),
    ).fetchall())
    return [
        MacTestCall(
            str(call_id), str(started_at),
            offload_record(mac, reason, local_reason, mac_s, outcome),
            RecordedTarget(target) if isinstance(target, str) else NotRecorded.VALUE,
            nonnegative_seconds(hold_wait_s),
            recorded_busy_holder(holder),
        )
        for call_id, started_at, outcome, mac, reason, local_reason, mac_s, target, hold_wait_s, holder in rows
    ]


@dataclass(frozen=True)
class ReportWindow:
    label: str
    start: str
    end: str


@dataclass(frozen=True)
class TestSelectionTree:
    package: str
    command: str
    tree_key: str


@dataclass(frozen=True)
class TestTreeEvidence:
    keyed_mac_calls: dict[str, set[TestSelectionTree]]
    passing_natedev_trees: set[TestSelectionTree]


@dataclass(frozen=True)
class MacFailureComparison:
    recorded: int
    without_tree_key: int


def read_test_tree_evidence(connection: sqlite3.Connection, start: str, end: str) -> TestTreeEvidence:
    """Read keyed nextest steps once in the range [start, end)."""
    rows = cast(list[tuple[object, ...]], connection.execute(
        "SELECT s.call_id, s.caller, s.status, s.tree_key, c.package, c.command"
        + " FROM steps s JOIN calls c ON c.id = s.call_id"
        + " WHERE s.step = 'nextest' AND s.tree_key IS NOT NULL"
        + " AND s.started_at >= ? AND s.started_at < ?"
        + " AND c.tool = 'verify.sh' AND c.verb = 'test'"
        + " AND (c.outcome IS NULL OR c.outcome NOT IN ('reused', 'replayed'))",
        (start, end),
    ).fetchall())
    mac_keys: dict[str, set[TestSelectionTree]] = {}
    local_passes: set[TestSelectionTree] = set()
    for call_id, caller, status, tree_key, package, command in rows:
        if not isinstance(tree_key, str) or not tree_key or not isinstance(package, str) or not isinstance(command, str):
            continue
        key = TestSelectionTree(package, command, tree_key)
        if is_mac_step(caller):
            mac_keys.setdefault(str(call_id), set()).add(key)
        elif status == 0:
            local_passes.add(key)
    return TestTreeEvidence(mac_keys, local_passes)


def compare_mac_failures(calls: list[MacTestCall], evidence: TestTreeEvidence) -> MacFailureComparison:
    recorded = without_tree_key = 0
    for call in calls:
        if not isinstance(call.decision, MacRunCompleted) or call.decision.outcome != "failed":
            continue
        keys = evidence.keyed_mac_calls.get(call.call_id, set())
        if not keys:
            without_tree_key += 1
        elif keys & evidence.passing_natedev_trees:
            recorded += 1
    return MacFailureComparison(recorded, without_tree_key)


@dataclass(frozen=True)
class ExcludedCalls:
    cache_reuses: int
    local_flag: int
    repo: int
    linux_only: int
    no_mac_entry: int
    unreadable: int


@dataclass(frozen=True)
class WindowSummary:
    candidates: int
    passed: int
    failed: int
    lost: int
    matched_no_tests: int
    declined: Counter[OffloadDeclineReason | Literal["runner"]]
    excluded: ExcludedCalls
    local_reasons: Counter[str | LocalReasonNotRecorded]
    local_mac_unavailable: int
    share: UsableShare
    causes: Counter[ShareMissReason]
    busy_holders: Counter[RecordedBusyHolder]
    target_not_recorded: int
    hold_wait_not_recorded: int


def local_reason_says_mac_unavailable(reason: str) -> bool:
    return any(re.search(pattern, reason, re.IGNORECASE) for pattern in LOCAL_MAC_UNAVAILABLE_PATTERNS)


def summarize_window(calls: list[MacTestCall]) -> WindowSummary:
    candidates = passed = failed = lost = no_tests = unavailable = 0
    cache_reuses = local_flag = repo = linux_only = no_mac_entry = unreadable = 0
    declined: Counter[OffloadDeclineReason | Literal["runner"]] = Counter()
    local_reasons: Counter[str | LocalReasonNotRecorded] = Counter()
    for call in calls:
        result = call.decision
        candidates += int(is_mac_candidate(result))
        if isinstance(result, CacheReuse):
            cache_reuses += 1
        elif isinstance(result, NoOffloadRecord):
            no_mac_entry += 1
        elif isinstance(result, UnrecognizedOffloadRecord):
            unreadable += 1
        elif isinstance(result, LocalRunRequested):
            local_flag += 1
            local_reasons[result.reason] += 1
            if isinstance(result.reason, str):
                unavailable += int(local_reason_says_mac_unavailable(result.reason))
        elif isinstance(result, MacRunCompleted):
            passed += int(result.outcome in ("passed", "passed_filter", "passed_full"))
            failed += int(result.outcome == "failed")
        elif isinstance(result, MacRunMatchedNoTest):
            no_tests += 1
        elif isinstance(result, MacRunLost):
            lost += 1
        elif isinstance(result, RunnerResultUnavailable):
            declined["runner"] += 1
        elif result.reason == "repo":
            repo += 1
        elif result.reason == "linux_only":
            linux_only += 1
        else:
            declined[result.reason] += 1
    busy_holders: Counter[RecordedBusyHolder] = Counter()
    for call in calls:
        if isinstance(call.decision, OffloadDeclined) and call.decision.reason == "busy":
            busy_holders[call.holder] += 1
    causes: Counter[ShareMissReason] = Counter()
    for reason in COUNTED_AGAINST_SHARE:
        if reason in NOT_USABLE_REASONS:
            continue
        count = lost if reason == "lost" else declined[reason]
        if count:
            causes[reason] = count
    return WindowSummary(
        candidates, passed, failed, lost, no_tests, declined,
        ExcludedCalls(cache_reuses, local_flag, repo, linux_only, no_mac_entry, unreadable),
        local_reasons, unavailable, share_of_records(call.decision for call in calls), causes, busy_holders,
        sum(isinstance(call.target, NotRecorded) for call in calls),
        sum(isinstance(call.hold_wait_s, NotRecorded) for call in calls),
    )


@dataclass(frozen=True)
class DeclineStretches:
    blocked_s: float
    unreachable_s: float


@dataclass(frozen=True)
class DeclineCluster:
    cause: Literal["blocked", "unreachable"]
    first: datetime
    last: datetime


class NoDeclineCluster(Enum):
    VALUE = auto()


def decline_clusters(calls: list[MacTestCall]) -> list[DeclineCluster]:
    clusters: list[DeclineCluster] = []
    cluster: DeclineCluster | NoDeclineCluster = NoDeclineCluster.VALUE
    for call in calls:
        if not is_mac_candidate(call.decision):
            continue
        answer = call.decision
        cause: Literal["blocked", "unreachable"] | NoDeclineCluster = NoDeclineCluster.VALUE
        if isinstance(answer, OffloadDeclined):
            if answer.reason == "blocked":
                cause = "blocked"
            elif answer.reason in ("unreachable", "backoff"):
                cause = "unreachable"
        at = datetime.fromisoformat(call.started_at)
        if isinstance(cluster, DeclineCluster) and cluster.cause == cause:
            cluster = DeclineCluster(cluster.cause, cluster.first, at)
            continue
        if isinstance(cluster, DeclineCluster):
            clusters.append(cluster)
        cluster = NoDeclineCluster.VALUE if isinstance(cause, NoDeclineCluster) else DeclineCluster(cause, at, at)
    if isinstance(cluster, DeclineCluster):
        clusters.append(cluster)
    return clusters


def decline_stretches(clusters: list[DeclineCluster], start: str, end: str) -> DeclineStretches:
    first = datetime.fromisoformat(start)
    last = datetime.fromisoformat(end)

    def overlap_seconds(cluster: DeclineCluster) -> float:
        return max(0.0, (min(cluster.last, last) - max(cluster.first, first)).total_seconds())

    return DeclineStretches(
        sum(overlap_seconds(item) for item in clusters if item.cause == "blocked"),
        sum(overlap_seconds(item) for item in clusters if item.cause == "unreachable"),
    )


def report_windows(now: datetime, hours: float) -> list[ReportWindow]:
    end = store.utc_iso(now.timestamp())
    windows = [ReportWindow(
        f"Last {hours:g} hours", store.utc_iso(now.timestamp() - hours * 3600), end,
    )]
    today = now.astimezone(user_zone()).date()
    for days_ago in range(3):
        day = today - timedelta(days=days_ago)
        midnight = datetime.combine(day, time(), user_zone())
        following = datetime.combine(day + timedelta(days=1), time(), user_zone())
        windows.append(ReportWindow(
            day.isoformat(), store.utc_iso(midnight.timestamp()), min(store.utc_iso(following.timestamp()), end),
        ))
    return windows


def calls_in_window(calls: list[MacTestCall], window: ReportWindow) -> list[MacTestCall]:
    return [call for call in calls if window.start <= call.started_at < window.end]


CountKey = TypeVar("CountKey", bound=str)


def sorted_counts(counts: Counter[CountKey]) -> list[tuple[CountKey, int]]:
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def count_text(counts: Counter[CountKey]) -> str:
    return "; ".join(f"{reason}: {count}" for reason, count in sorted_counts(counts)) or "none"


def share_line(share: UsableShare) -> str:
    percentage = f"{100 * share.finished / share.candidates:.1f}%" if share.candidates else "no usable candidates"
    return (
        f"Usable share: {share.finished} of {share.candidates} finished on the Mac ({percentage})."
        f" Set aside as not usable: {', '.join(NOT_USABLE_REASONS)}."
        f" Counted against the share: {', '.join(COUNTED_AGAINST_SHARE)}."
    )


def window_lines(window: ReportWindow, calls: list[MacTestCall], evidence: TestTreeEvidence) -> list[str]:
    summary = summarize_window(calls)
    failures = compare_mac_failures(calls, evidence)
    excluded = summary.excluded
    lines = [
        f"## {window.label}", "",
        f"Candidates: {summary.candidates}. Finished on the Mac: {summary.passed + summary.failed + summary.matched_no_tests}"
        + f" (passed: {summary.passed}; failed: {summary.failed}"
        + (f"; matched no tests: {summary.matched_no_tests}" if summary.matched_no_tests else "")
        + f"). Lost mid-run: {summary.lost}.",
        f"Declined: {count_text(summary.declined)}.",
        f"Not candidates: cache reuses: {excluded.cache_reuses}; local flag: {excluded.local_flag};"
        + f" ineligible: {excluded.repo + excluded.linux_only}"
        + f" (repo: {excluded.repo}; linux_only: {excluded.linux_only}).",
        f"No Mac entry: {excluded.no_mac_entry}; Mac answer unreadable: {excluded.unreadable}.",
        share_line(summary.share),
        f"Largest causes of a usable Mac going unused: {count_text(summary.causes)}.",
        f"Environment failures: lost runs: {summary.lost}; runner: {summary.declined['runner']}.",
        f"Mac failures with a natedev pass of the same tree: {failures.recorded} recorded"
        + (f", {failures.without_tree_key} without a tree key" if failures.without_tree_key else "") + ".",
        f"Local-flag runs: {excluded.local_flag}; text says the Mac was unavailable: {summary.local_mac_unavailable}.",
        "Most repeated local reasons:",
    ]
    if summary.busy_holders:
        position = lines.index(f"Largest causes of a usable Mac going unused: {count_text(summary.causes)}.") + 1
        holders = sorted(summary.busy_holders.items(), key=lambda item: (-item[1], holder_text(item[0])))
        lines[position:position] = ["busy:", *(f"- {holder_text(holder)}: {count}" for holder, count in holders)]
    reasons = sorted(summary.local_reasons.items(), key=lambda item: (-item[1], item[0] if isinstance(item[0], str) else "(not recorded)"))
    for reason, count in reasons[:10]:
        text = " ".join(reason.split()) if isinstance(reason, str) else "(not recorded)"
        lines.append(f"- {count}: {text}")
    if len(reasons) > 10:
        remaining_reasons = len(reasons) - 10
        remaining_runs = sum(count for _, count in reasons[10:])
        reason_word = "reason" if remaining_reasons == 1 else "reasons"
        run_word = "run" if remaining_runs == 1 else "runs"
        lines.append(f"Other local reasons: {remaining_reasons} {reason_word}, {remaining_runs} {run_word}.")
    if not summary.local_reasons:
        lines.append("none")
    lines += [
        f"Call fields: target not recorded: {summary.target_not_recorded};"
        + f" held-target wait not recorded: {summary.hold_wait_not_recorded}.", "",
    ]
    return lines


def lower_bound(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"at least {minutes // 60}:{minutes % 60:02d} (lower bound, from declines)"


def mac_report(connection: sqlite3.Connection, now: datetime, hours: float = 24) -> str:
    windows = report_windows(now, hours)
    earliest_start = min(window.start for window in windows)
    end = max(window.end for window in windows)
    calls = read_test_calls(connection, earliest_start, end)
    steps_start = store.utc_iso((datetime.fromisoformat(earliest_start) - TEST_TREE_LOOKBACK).timestamp())
    evidence = read_test_tree_evidence(connection, steps_start, end)
    clusters = decline_clusters(calls)
    current = holds.read_current_holds()
    history = holds.read_hold_history(connection, current, now)
    lines = ["# Mac test report", "", f"Times in the user's zone; through {now.astimezone(user_zone()):%Y-%m-%d %H:%M %Z}.", ""]
    lines += holds.now_lines(current)
    lines.append("")
    lines.extend(window_lines(windows[0], calls_in_window(calls, windows[0]), evidence))
    lines += [
        "## Last three local days", "",
        "| Day | Candidates | Finished (passed, failed, matched no tests) | Lost | Declined by reason | Usable share | Cache reuses | Local flag | Ineligible (repo, linux_only) | Blocked | natedev held | Unreachable and backoff |",
        "|---|---:|---:|---:|---|---:|---:|---:|---:|---|---|---|",
    ]
    for window in windows[1:]:
        daily_calls = calls_in_window(calls, window)
        summary = summarize_window(daily_calls)
        stretches = decline_stretches(clusters, window.start, window.end)
        excluded = summary.excluded
        mac_since = history.recorded_since["mac"]
        blocked = lower_bound(stretches.blocked_s) if isinstance(mac_since, holds.NoHoldEventRecorded) or datetime.fromisoformat(window.end) <= mac_since.at else holds.duration_text(holds.hold_duration(history, "mac", window.start, window.end))
        natedev_since = history.recorded_since["natedev"]
        held = "not recorded" if isinstance(natedev_since, holds.NoHoldEventRecorded) or datetime.fromisoformat(window.end) <= natedev_since.at else holds.duration_text(holds.hold_duration(history, "natedev", window.start, window.end))
        cells = [
            window.label, str(summary.candidates), f"{summary.passed}, {summary.failed}, {summary.matched_no_tests}", str(summary.lost),
            count_text(summary.declined), f"{summary.share.finished} of {summary.share.candidates}",
            str(excluded.cache_reuses), str(excluded.local_flag),
            f"{excluded.repo}, {excluded.linux_only}",
            blocked, held, lower_bound(stretches.unreachable_s),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    intervals = holds.interval_lines(history, earliest_start, end)
    lines += ["", "## Hold intervals", "", *(intervals or ["none"]), "", *holds.coverage_lines(history)]
    return "\n".join(lines)


def main(args: list[str]) -> int:
    hours = 24.0
    try:
        if args:
            if len(args) != 2 or args[0] != "--hours":
                raise ValueError
            hours = float(args[1])
            if not math.isfinite(hours) or hours <= 0:
                raise ValueError
        now = datetime.now(UTC)
        # Validate the requested range before reading the index.
        _ = report_windows(now, hours)
    except (ValueError, OverflowError):
        print("usage: buildlog mac-report [--hours N] (N must be a positive number)", file=sys.stderr)
        return 2
    _ = index.update()
    connection = index.read_only()
    try:
        print(mac_report(connection, now, hours))
    finally:
        connection.close()
    return 0
