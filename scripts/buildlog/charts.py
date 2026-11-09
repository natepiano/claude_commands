"""Read shared build-page data and open the shared page link."""

from __future__ import annotations

import bisect
import os
import sqlite3
import subprocess
import sys
import webbrowser
from collections import defaultdict
from collections.abc import Callable, Mapping
from contextlib import closing, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from enum import StrEnum
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

from scripts.lib import conf
from scripts.buildlog import costs, index, moments
from scripts.lib.user_zone import user_zone
from scripts.buildlog.costs import CostUnavailable as CostUnavailable

MEMORY_FEED = Path(__file__).resolve().parents[1] / "build_queue/memory_feed.py"


@dataclass(frozen=True)
class MeasuredCost:
    seconds: float


@dataclass(frozen=True)
class MacIdleUpperBound:
    known_seconds: float
    unknown_seconds: float


class NoDataYet(StrEnum):
    VALUE = "no data yet"


type ChartCost = MeasuredCost | MacIdleUpperBound | CostUnavailable | NoDataYet

@dataclass(frozen=True)
class DailyBucket:
    start: datetime
    end: datetime
    thin: bool
    costs: costs.BuildCosts
    observed_machines: frozenset[str]
    first_sources: dict[str, float]


@dataclass(frozen=True)
class ChartSet:
    start: datetime
    end: datetime
    zone: str
    buckets: tuple[DailyBucket, ...]
    machines: tuple[str, ...]
    events: tuple[moments.Moment, ...]


@dataclass(frozen=True)
class CostAction:
    text: str
    owner: str


@dataclass(frozen=True)
class WindowMatchesRecent:
    pass


@dataclass(frozen=True)
class WindowDiffers:
    value: ChartCost


type WindowValue = WindowMatchesRecent | WindowDiffers


@dataclass(frozen=True)
class TopCost:
    machine: str
    cause: costs.Cause
    recent: ChartCost
    window: WindowValue
    action: CostAction


@dataclass(frozen=True)
class TopCosts:
    start: datetime
    end: datetime
    recent_days: int
    rows: tuple[TopCost, ...] | NoDataYet


@dataclass(frozen=True)
class PageData:
    charts: ChartSet
    top: TopCosts


@dataclass(frozen=True)
class IndexUnavailable:
    reason: str


COST_KEYS: dict[costs.Cause, str] = {
    "Memory wait": "MEMORY_WAIT",
    "Cold rebuilds": "COLD_REBUILDS",
    "Reruns after a pass": "RERUNS_AFTER_A_PASS",
    "Failed runs": "FAILED_RUNS",
    "Cancelled CI": "CANCELLED_CI",
    "Mac idle while natedev waits": "MAC_IDLE_WHILE_NATEDEV_WAITS",
}

CAUSES = tuple(COST_KEYS)


def with_first_sources(reading: costs.CostReading) -> costs.CostReading:
    """Include the first observation when the source history has no earlier value."""
    first_sources = dict(reading.first_sources)
    for observation in reading.observations:
        first_sources[observation.machine] = min(first_sources.get(observation.machine, observation.at), observation.at)
    return replace(reading, first_sources=first_sources)


def build_bucket(reading: costs.CostReading, start: datetime, end: datetime, *, thin: bool = False) -> DailyBucket:
    """Use the same coverage and cost rules for each window."""
    low, high = start.timestamp(), end.timestamp()
    observed = frozenset(item.machine for item in reading.observations if low <= item.at < high)
    covered = frozenset(item.machine for item in reading.coverage if item.start <= low and item.end >= high)
    return DailyBucket(start, end, thin, costs.aggregate_costs(reading, start, end), observed | covered, reading.first_sources)


def bucket_edges(start: datetime, end: datetime, zone: ZoneInfo) -> tuple[datetime, ...]:
    """Clip local calendar days to the captured UTC window."""
    if end <= start:
        raise ValueError("chart window must have positive duration")
    edges = [start.astimezone(UTC)]
    day = start.astimezone(zone).date() + timedelta(days=1)
    while True:
        edge = datetime.combine(day, time(), zone).astimezone(UTC)
        if edge >= end:
            break
        edges.append(edge)
        day += timedelta(days=1)
    edges.append(end.astimezone(UTC))
    return tuple(edges)


def build_series(reading: costs.CostReading, start: datetime, end: datetime, zone: ZoneInfo,
                 configured: tuple[str, ...] = ()) -> ChartSet:
    return chart_series(with_first_sources(reading), start, end, zone, configured)


def chart_series(reading: costs.CostReading, start: datetime, end: datetime, zone: ZoneInfo,
                 configured: tuple[str, ...]) -> ChartSet:
    edges = bucket_edges(start, end, zone)
    stamps = [edge.timestamp() for edge in edges]
    count = len(edges)-1
    contributions: defaultdict[int, list[costs.CostContribution]] = defaultdict(list)
    observations: defaultdict[int, list[costs.SourceObservation]] = defaultdict(list)
    phases: defaultdict[int, list[moments.ProgressFinish]] = defaultdict(list)
    spans: defaultdict[int, list[costs.IdleSpan]] = defaultdict(list)
    for contribution in reading.contributions:
        position = bisect.bisect_right(stamps, contribution.at)-1
        if 0 <= position < count:
            contributions[position].append(contribution)
    for observation in reading.observations:
        position = bisect.bisect_right(stamps, observation.at)-1
        if 0 <= position < count:
            observations[position].append(observation)
    for phase in reading.phases:
        position = bisect.bisect_right(stamps, phase.timestamp_epoch)-1
        if 0 <= position < count:
            phases[position].append(phase)
    for span in reading.idle_spans:
        position = max(0, bisect.bisect_right(stamps, span.start)-1)
        while position < count and stamps[position] < span.end:
            spans[position].append(span)
            position += 1
    buckets: list[DailyBucket] = []
    for position in range(count):
        low, high = edges[position:position+2]
        day = low.astimezone(zone).date()
        full_low = datetime.combine(day, time(), zone).astimezone(UTC)
        full_high = datetime.combine(day+timedelta(days=1), time(), zone).astimezone(UTC)
        daily = costs.CostReading(tuple(contributions[position]), tuple(observations[position]),
                                  tuple(spans[position]), tuple(phases[position]), reading.coverage, reading.first_sources)
        buckets.append(build_bucket(daily, low, high, thin=low != full_low or high != full_high))
    machines = sorted(set(configured) | set(reading.first_sources))
    if reading.idle_spans and "mac" not in machines:
        machines.append("mac")
    return ChartSet(start, end, str(zone), tuple(buckets), tuple(sorted(machines)), moments.MOMENTS)


def read_snapshot(connection: sqlite3.Connection, start: datetime, end: datetime) -> costs.CostReading:
    """Read costs in one transaction before any chart calculation."""
    _ = connection.execute("BEGIN")
    try:
        return costs.read_costs(connection, start, end, boundary_context=True)
    finally:
        _ = connection.execute("ROLLBACK")


def collect_charts(connection: sqlite3.Connection, start: datetime, end: datetime, zone: ZoneInfo) -> ChartSet:
    """Keep one transaction and one shared read for the whole chart set."""
    from scripts.buildlog import record

    return build_series(read_snapshot(connection, start, end), start, end, zone, record.configured_targets())


def read_index[T](read: Callable[[sqlite3.Connection], T]) -> T | IndexUnavailable:
    """Close the index after the read and preserve an index error as data."""
    try:
        with closing(index.read_only()) as connection:
            return read(connection)
    except sqlite3.Error as error:
        return IndexUnavailable(str(error))


def cost_value(bucket: DailyBucket, machine: str, cause: costs.Cause, *, per_phase: bool = False) -> ChartCost:
    idle = bucket.costs.mac_idle
    idle_wait = cause == "Mac idle while natedev waits" and machine == "mac" and idle.candidate_wait_seconds > 0
    if bucket.first_sources.get(machine, bucket.end.timestamp()) >= bucket.end.timestamp() and not idle_wait:
        return NoDataYet.VALUE
    if per_phase and not bucket.costs.phases:
        return CostUnavailable("no finished phases")
    if cause == "Mac idle while natedev waits" and machine == "mac":
        if idle_wait:
            return MacIdleUpperBound(idle.upper_bound_seconds-idle.unknown_seconds, idle.unknown_seconds)
        if "natedev" not in bucket.observed_machines:
            return CostUnavailable("no recorded candidate coverage")
    elif machine not in bucket.observed_machines:
        return CostUnavailable("no recorded source coverage")
    missing = sum(row.missing_waits for row in bucket.costs.ranked if row.machine == machine and row.cause == cause)
    if missing:
        return CostUnavailable(f"{missing} applicable waits missing")
    return MeasuredCost(sum(row.seconds for row in bucket.costs.ranked if row.machine == machine and row.cause == cause))


def known_seconds(value: ChartCost) -> float | None:
    """Return the measured part of a cost when that part is available."""
    if isinstance(value, MeasuredCost):
        return value.seconds
    if isinstance(value, MacIdleUpperBound):
        return value.known_seconds
    return None


def window_value(recent: ChartCost, window: ChartCost, recent_days: int, days: int) -> WindowValue:
    """Compare daily costs and preserve unavailable comparison values."""
    recent_seconds = known_seconds(recent)
    if recent_seconds is None:
        return WindowMatchesRecent()
    window_seconds = known_seconds(window)
    if window_seconds is None:
        return WindowDiffers(window)
    recent_seconds /= recent_days
    window_seconds /= days
    if abs(window_seconds - recent_seconds) <= 0.5 * recent_seconds:
        return WindowMatchesRecent()
    value = (MacIdleUpperBound(window_seconds, window.unknown_seconds / days)
             if isinstance(window, MacIdleUpperBound) else MeasuredCost(window_seconds))
    return WindowDiffers(value)


def page_data(days: int, n: int = 3) -> PageData | IndexUnavailable:
    """Read the charts and recent costs from one index snapshot."""
    from scripts.buildlog import record

    settings = conf.read("buildlog")
    recent_days = settings.whole("TOP_COST_RECENT_DAYS")
    if recent_days > days:
        raise conf.Unusable(f"TOP_COST_RECENT_DAYS={recent_days} exceeds the page days ({days})")
    actions = {cause: CostAction(settings.text("COST_ACTION_" + key), settings.text("COST_OWNER_" + key))
               for cause, key in COST_KEYS.items()}
    zone = user_zone()
    end = datetime.now(zone)
    start = end - timedelta(days=days)
    recent_start = end - timedelta(days=recent_days)

    def collect(connection: sqlite3.Connection) -> PageData:
        reading = with_first_sources(read_snapshot(connection, start, end))
        series = chart_series(reading, start, end, zone, record.configured_targets())
        recent = build_bucket(reading, recent_start, end)
        window = build_bucket(reading, start, end)
        rows: list[TopCost] = []
        for row in recent.costs.ranked[:max(0, n)]:
            value = cost_value(recent, row.machine, row.cause)
            comparison = window_value(value, cost_value(window, row.machine, row.cause), recent_days, days)
            rows.append(TopCost(row.machine, row.cause, value, comparison, actions[row.cause]))
        top = TopCosts(recent_start, end, recent_days, tuple(rows) if rows else NoDataYet.VALUE)
        return PageData(series, top)

    return read_index(collect)


def cost_json(value: ChartCost, divisor: float, *, machine: str = "") -> dict[str, object]:
    if isinstance(value, NoDataYet):
        return {"state": value.value}
    if isinstance(value, CostUnavailable):
        reason = value.reason
        if machine and reason == "no recorded source coverage":
            reason = f"no complete indexed {machine} monthly log covers this day"
        elif reason == "no recorded candidate coverage":
            reason = "no complete indexed natedev monthly log covers candidate waits"
        return {"state": "unavailable", "reason": reason}
    if isinstance(value, MacIdleUpperBound):
        return {"state": "upper bound", "value": value.known_seconds/divisor,
                "unknown": value.unknown_seconds/divisor, "upper": (value.known_seconds+value.unknown_seconds)/divisor}
    return {"state": "measured", "value": value.seconds/divisor}


def chart_data(series: ChartSet) -> dict[str, object]:
    buckets: list[dict[str, object]] = []
    for bucket in series.buckets:
        hana = [phase for phase in bucket.costs.phases if moments.hana_name(phase.working_dir) and phase.host == "natedev"]
        durations = [phase.wall_s for phase in hana if isinstance(phase.wall_s, float)]
        phase: dict[str, object] = {"finished": len(hana), "valid": len(durations), "missing": len(hana)-len(durations)}
        if durations:
            phase.update(state="measured", median=moments.nearest_rank(durations, 50), p90=moments.nearest_rank(durations, 90))
        else:
            phase.update(state="unavailable", reason="no valid durations" if hana else "no finished phases")
        rows: list[dict[str, object]] = []
        days = (bucket.end-bucket.start).total_seconds()/86400
        for machine in series.machines:
            for cause in CAUSES:
                rows.append({"machine": machine, "cause": cause,
                             "daily": cost_json(cost_value(bucket, machine, cause), 3600*days, machine=machine),
                             "perPhase": cost_json(cost_value(bucket, machine, cause, per_phase=True),
                                                   3600*len(bucket.costs.phases) if bucket.costs.phases else 1,
                                                   machine=machine)})
        idle = bucket.costs.mac_idle
        buckets.append({"start": bucket.start.timestamp()*1000, "end": bucket.end.timestamp()*1000,
                        "label": bucket.start.astimezone(ZoneInfo(series.zone)).strftime("%a %b %d"),
                        "thin": bucket.thin, "phase": phase, "allPhases": len(bucket.costs.phases), "rows": rows,
                        "idle": {"candidate": idle.candidate_wait_seconds, "execution": idle.execution_overlap_seconds,
                                 "holds": idle.recorded_hold_seconds, "known": idle.upper_bound_seconds-idle.unknown_seconds,
                                 "unknown": idle.unknown_seconds, "upper": idle.upper_bound_seconds}})
    live: list[dict[str, object]] = []
    pending: list[str] = []
    for event in series.events:
        if isinstance(event, moments.PendingMemoryMoment):
            pending.append(event.name+": not live yet")
        elif series.start <= event.at < series.end:
            scope = event.scope if isinstance(event, moments.BuildMoment) else event.track.value
            live.append({"at": event.at.timestamp()*1000, "name": event.name, "scope": scope,
                         "local": event.at.astimezone(ZoneInfo(series.zone)).strftime("%b %d %H:%M %Z")})
    return {"start": series.start.timestamp()*1000, "end": series.end.timestamp()*1000,
            "zone": series.zone, "machines": series.machines, "causes": CAUSES, "buckets": buckets,
            "moments": live, "pending": pending}


def local_display(environ: Mapping[str, str], platform: str) -> bool:
    if "SSH_CONNECTION" in environ:
        return False
    return platform == "darwin" or bool(environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY"))


def main(args: list[str]) -> int:
    no_open = "--no-open" in args or os.environ.get("BUILDLOG_CHARTS_NO_OPEN") == "1"
    words = [word for word in args if word != "--no-open"]
    if words not in ([], ["--days", "7"], ["--days", "30"]):
        print("usage: buildlog charts [--days 7|30] [--no-open]", file=sys.stderr)
        return 2
    try:
        result = subprocess.run([sys.executable, str(MEMORY_FEED), "link"],
                                capture_output=True, text=True, check=False)
    except (OSError, UnicodeDecodeError) as error:
        print(f"buildlog charts: {error}", file=sys.stderr)
        return 1
    if result.returncode:
        reason = result.stderr.strip() or f"link command exited {result.returncode}"
        print(f"buildlog charts: {reason}", file=sys.stderr)
        return 1
    url = result.stdout.strip()
    try:
        link = urlsplit(url)
        _ = link.port
        valid = (link.scheme in ("http", "https") and bool(link.hostname) and url.isprintable()
                 and not any(char.isspace() for char in url))
    except ValueError:
        valid = False
    if not valid:
        reason = result.stderr.strip() or "link command did not return one http/https URL"
        print(f"buildlog charts: {reason}", file=sys.stderr)
        return 1
    print(url)
    if not no_open and local_display(os.environ, sys.platform):
        with suppress(OSError, webbrowser.Error):
            _ = webbrowser.open(url)
    return 0
