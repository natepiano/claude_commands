"""A standalone chart page from one continuous, read-only history calculation."""

from __future__ import annotations

import bisect
import html
import json
import os
import sqlite3
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

from scripts.lib import conf
from scripts.buildlog import costs, index, moments
from scripts.lib.user_zone import user_zone
from scripts.buildlog.costs import CostUnavailable as CostUnavailable

from scripts.review_links import review_link


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


def render_html(series: ChartSet) -> str:
    # Escape HTML delimiters before placing JSON inside a script element.
    payload = json.dumps(chart_data(series), ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return (PAGE.replace("@DATA@", payload).replace("@ZONE@", html.escape(series.zone))
            .replace("@COLD@", str(moments.COLD_BUILD_MIN_CRATES))
            .replace("@REPEAT_MINUTES@", str(moments.SAME_WORKER_REPEAT_SECONDS // 60)))


def publish(series: ChartSet, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    _ = temporary.write_text(render_html(series))
    _ = temporary.replace(path)


def local_display(environ: Mapping[str, str], platform: str) -> bool:
    if "SSH_CONNECTION" in environ:
        return False
    return platform == "darwin" or bool(environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY"))


def present_page(series: ChartSet, path: Path, no_open: bool) -> int:
    try:
        publish(series, path)
        url = review_link.snapshot([path])[0]
    except review_link.SNAPSHOT_ERRORS as error:
        reason = " ".join(str(error).splitlines())
        print(f"buildlog charts: publication failed: {reason}", file=sys.stderr)
        return 1
    print(url)
    if not no_open and local_display(os.environ, sys.platform):
        with suppress(OSError, webbrowser.Error):
            _ = webbrowser.open(url)
    return 0


def main(args: list[str]) -> int:
    from scripts.buildlog.number_option import positive_window

    no_open = "--no-open" in args or os.environ.get("BUILDLOG_CHARTS_NO_OPEN") == "1"
    words = [word for word in args if word != "--no-open"]
    if words not in ([], ["--days", "7"], ["--days", "30"]):
        print("usage: buildlog charts [--days 7|30] [--no-open]", file=sys.stderr)
        return 2
    window = positive_window(words, "--days", 7, "charts", integer=True)
    if window is None:
        return 2
    start, end = window[:2]
    series = read_index(lambda connection: collect_charts(connection, start, end, user_zone()))
    if isinstance(series, IndexUnavailable):
        print(f"buildlog charts: index unavailable: {series.reason}", file=sys.stderr)
        return 1
    path = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home()/".cache")))/"buildlog/charts/latest.html"
    return present_page(series, path, no_open)


PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Build history · phase time</title>
<style>
:root{color-scheme:light;--bg:#f9f9f7;--card:#fcfcfb;--ink:#0b0b0b;--muted:#52514e;--grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);--series-1:#2a78d6;--series-2:#eb6834;--series-3:#1baf7a;--series-4:#eda100;--series-5:#e87ba4;--series-6:#559b16;--series-7:#6250d6;--series-8:#e34948}
@media(prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;--bg:#0d0d0d;--card:#1a1a19;--ink:#f0efec;--muted:#c3c2b7;--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);--series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;--series-4:#c98500;--series-5:#d55181;--series-6:#559b16;--series-7:#9085e9;--series-8:#e66767}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#0d0d0d;--card:#1a1a19;--ink:#f0efec;--muted:#c3c2b7;--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);--series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;--series-4:#c98500;--series-5:#d55181;--series-6:#559b16;--series-7:#9085e9;--series-8:#e66767}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1240px;margin:auto;padding:36px 24px}header{display:flex;justify-content:space-between;gap:24px;align-items:start}
h1{font-size:32px;letter-spacing:-.035em;margin:0}h2{font-size:19px;margin:0}p{margin:8px 0;color:var(--muted)}
.eyebrow{text-transform:uppercase;letter-spacing:.14em;font-size:11px;font-weight:700;color:var(--muted)}
.card{position:relative;background:var(--card);border:1px solid var(--border);border-radius:14px;padding:24px;margin:24px 0}
.controls{display:flex;gap:12px;flex-wrap:wrap;align-items:end;margin:22px 0}label{font-size:12px;color:var(--muted)}
select,button{display:block;max-width:100%;font:inherit;color:var(--ink);background:var(--card);border:1px solid var(--border);border-radius:6px;padding:7px 9px}
svg{display:block;width:100%;height:auto;overflow:visible}svg text{fill:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}svg .grid{stroke:var(--grid);stroke-width:1}svg .axis{stroke:var(--axis);stroke-width:1}
.legend{display:flex;flex-wrap:wrap;gap:6px 20px;font-size:12px;margin:8px 0}.line-key{display:inline-block;width:20px;height:0;border-top:2px solid;vertical-align:middle;margin-right:7px}
.detail{font-size:12px;color:var(--muted);border-top:1px solid var(--border);padding-top:12px;margin-top:14px;overflow-wrap:anywhere}
.tooltip{display:none;position:absolute;z-index:3;max-width:min(430px,calc(100% - 32px));max-height:290px;overflow:auto;pointer-events:none;background:var(--card);border:1px solid var(--axis);border-radius:8px;padding:10px 14px;box-shadow:0 4px 18px #0002;font-size:12px;color:var(--muted)}
.tooltip strong{color:var(--ink)}.tooltip p{margin:3px 0}.tooltip .date{color:var(--ink);font-weight:600;margin-bottom:6px}
#moments{display:flex;flex-wrap:wrap;gap:8px}#moments span{border:1px solid var(--border);border-radius:6px;padding:4px 9px;font-size:12px}
footer{font-size:12px;color:var(--muted)}:focus-visible{outline:2px solid var(--ink);outline-offset:3px}
details{margin-top:14px;font-size:12px}summary{cursor:pointer;color:var(--ink)}.table-scroll{overflow:auto;max-height:450px;margin-top:8px}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{padding:8px 10px;text-align:right;border-bottom:1px solid var(--grid);min-width:120px}th:first-child,td:first-child{text-align:left;min-width:145px}th{font-weight:600;color:var(--ink)}td{color:var(--muted)}caption{text-align:left;padding-bottom:8px;color:var(--muted)}
@media(max-width:600px){main{padding:22px 12px}.card{padding:16px}header{display:block}h1{font-size:26px}.controls{gap:10px}.controls label{flex:1 1 130px}select{width:100%}}
@media print{.controls,.tooltip{display:none}details{display:block}details .table-scroll{max-height:none}body{background:var(--card)}.card{break-inside:avoid}}
</style></head><body><main>
<header><div><div class="eyebrow">Build improvement loop</div><h1>Build history</h1><p>Phase time is the score. Compare changes against recorded costs.</p></div><p id="range"></p></header>
<div class="controls" aria-label="Chart filters">
<label>Window<select id="window"><option>7 days</option><option>30 days</option></select></label>
<label>Machine for causes<select id="machine"></select></label>
<label>Cause for machines<select id="causeSelect"></select></label>
<label>Cost unit<select id="unit"><option value="daily">h/day</option><option value="perPhase">h/finished phase</option></select></label>
<label>Theme<select id="theme"><option value="system">Auto</option><option value="light">Light</option><option value="dark">Dark</option></select></label></div>
<p id="windowHelp">Generate another window with <code>buildlog charts --days 7</code> or <code>buildlog charts --days 30</code>.</p>
<div id="moments" aria-label="Build changes"></div>
<section class="card"><h2>Hana phase time</h2><p>Daily median and p90 · hours per phase · finishes select the day</p><svg id="phase" viewBox="0 0 1080 300" role="img" aria-label="Hana phase time"></svg><div class="legend" id="phaseLegend"></div><div class="tooltip" id="phaseTooltip" role="tooltip"></div><div class="detail" id="phaseDetail" aria-live="polite"></div><details><summary>Table view: Hana phase time</summary><div class="table-scroll" id="phaseTable"></div></details></section>
<section class="card"><h2>Costs by cause</h2><p id="causeCaption"></p><svg id="cause" viewBox="0 0 1080 300" role="img" aria-label="Build costs by cause"></svg><div class="legend" id="causeLegend"></div><div class="tooltip" id="causeTooltip" role="tooltip"></div><div class="detail" id="causeDetail" aria-live="polite"></div><details><summary>Table view: Costs by cause</summary><div class="table-scroll" id="causeTable"></div></details></section>
<section class="card"><h2>Costs by machine</h2><p id="machineCaption"></p><svg id="machinePlot" viewBox="0 0 1080 300" role="img" aria-label="Build costs by machine"></svg><div class="legend" id="machineLegend"></div><div class="tooltip" id="machineTooltip" role="tooltip"></div><div class="detail" id="machineDetail" aria-live="polite"></div><details><summary>Table view: Costs by machine</summary><div class="table-scroll" id="machineTable"></div></details></section>
<footer><p>Time zone: @ZONE@. Partial calendar buckets show thin. Day rates use actual elapsed seconds, including daylight-saving changes.</p>
<p>Step and CI costs use start time. Repeats use the later start. Phase durations use finish time. Mac idle uses clipped interval time.</p>
<p>Costs overlap; lines do not form a total. Per-phase costs divide by all finished phases, including finishes without valid durations.</p>
<p>Hatched bands show unknown idle time above the known part. Gaps mean unavailable; recorded zero remains zero.</p>
<p>Memory wait uses recorded wait. Cold rebuilds compile @COLD@+ crates. Failed runs use counted failures. CI costs use job wall time.</p>
<p>Reruns after a pass require the same worker, tree, worktree, and build input within @REPEAT_MINUTES@ minutes. Mac idle excludes executions and complete holds.</p>
<p>Coverage refers to recorded events. Missing records cannot prove machine inactivity. This page makes no network requests.</p></footer>
</main><script>
'use strict';const data=@DATA@;
const $=id=>document.getElementById(id),ns='http://www.w3.org/2000/svg';let selected=0;
const machineOrder=[...['natedev','mac','CI'].filter(m=>data.machines.includes(m)),...data.machines.filter(m=>!['natedev','mac','CI'].includes(m))];
const machineGroups=machineOrder.length<=8?machineOrder.map(label=>({label,members:[label]})):[...machineOrder.slice(0,7).map(label=>({label,members:[label]})),{label:'Other',members:machineOrder.slice(7)}];
function selectedGroup(){return machineGroups.find(group=>group.label===$('machine').value)}
const noData=series=>series.every(s=>s.values.every(point=>point.state==='no data yet'));
const color=slot=>'var(--series-'+(slot+1)+')';
function node(tag,attrs={},text=''){const n=document.createElementNS(ns,tag);for(const[k,v]of Object.entries(attrs))n.setAttribute(k,v);if(text)n.textContent=text;return n}
function element(tag,text=''){const n=document.createElement(tag);n.textContent=text;return n}
function option(select,value,text){const n=element('option',text);n.value=value;select.append(n)}
function sumPoints(points){points=points.filter(point=>point.state!=='no data yet');if(!points.length)return{state:'no data yet'};if(points.some(p=>p.state==='unavailable'))return{state:'unavailable',reason:[...new Set(points.filter(p=>p.state==='unavailable').map(p=>p.reason))].join('; ')};return{state:points.some(p=>p.state==='upper bound')?'upper bound':'measured',value:points.reduce((s,p)=>s+p.value,0),unknown:points.reduce((s,p)=>s+(p.unknown||0),0)}}
function point(bucket,machine,cause){return bucket.rows.find(r=>r.machine===machine&&r.cause===cause)?.[$('unit').value]||{state:'unavailable',reason:'no recorded source coverage'}}
function groupSeries(members){return data.causes.map((label,slot)=>({label,color:color(slot),values:data.buckets.map(b=>sumPoints(members.map(m=>point(b,m,label))))}))}
function causeSeries(){return groupSeries(selectedGroup()?.members||data.machines)}
function machineSeries(){return machineGroups.map((group,slot)=>({label:group.label,color:color(slot),values:data.buckets.map(b=>sumPoints(group.members.map(m=>point(b,m,$('causeSelect').value))))}))}
function phaseSeries(){return['median','p90'].map((key,slot)=>({label:key,color:color(slot),dash:slot?'6 4':'',values:data.buckets.map(b=>b.phase.state==='measured'?{state:'measured',value:b.phase[key]/3600}:{state:'unavailable',reason:b.phase.reason})}))}
const configs=[{id:'phase',prefix:'phase',series:phaseSeries,unit:()=> 'h/phase'},{id:'cause',prefix:'cause',series:causeSeries,unit:()=> $('unit').selectedOptions[0].text},{id:'machinePlot',prefix:'machine',series:machineSeries,unit:()=> $('unit').selectedOptions[0].text}];
function describe(p,unit){if(p.state==='no data yet')return'no data yet';if(p.state==='unavailable')return'unavailable ('+p.reason+')';const value=p.value.toLocaleString(undefined,{maximumFractionDigits:2})+' '+unit;return p.unknown>0?value+' known; '+p.unknown.toFixed(2)+' '+unit+' unknown; upper bound '+(p.value+p.unknown).toFixed(2)+' '+unit:value}
function key(series){const n=element('i');n.className='line-key';n.style.borderColor=series.color;if(series.dash)n.style.borderTopStyle='dashed';return n}
function table(config,series){if(config.id==='phase'){series=series.map(s=>({...s,values:s.values.map(p=>p.state==='unavailable'?p:{...p,value:p.value*3600})}));config={...config,unit:()=> 's/phase'}}const table=element('table'),caption=element('caption',config.unit()+'; unavailable values retain their reason.'),head=element('thead'),row=element('tr');row.append(element('th','Local day'));series.forEach(s=>row.append(element('th',s.label+' ('+config.unit()+')')));if(config.id==='phase')row.append(element('th','Finished / valid / missing starts'));head.append(row);table.append(caption,head);const body=element('tbody');data.buckets.forEach((b,i)=>{const tr=element('tr');tr.append(element('th',b.label+(b.thin?' · thin':'')));series.forEach(s=>tr.append(element('td',describe(s.values[i],config.unit()))));if(config.id==='phase')tr.append(element('td',b.phase.finished+' / '+b.phase.valid+' / '+b.phase.missing));body.append(tr)});table.append(body);$(config.prefix+'Table').replaceChildren(table)}
function showTooltip(config,i){const box=$(config.prefix+'Tooltip'),b=data.buckets[i];box.replaceChildren(element('div',b.label+(b.thin?' · thin':'')));box.firstChild.className='date';config.series().forEach(s=>{const row=element('p'),value=element('strong',describe(s.values[i],config.unit()));row.append(key(s),value,document.createTextNode(' — '+s.label));box.append(row)});if(config.id==='phase')box.append(element('p',b.phase.finished+' finished; '+b.phase.valid+' valid; '+b.phase.missing+' missing starts'));box.style.display='block';const svg=$(config.id),card=svg.parentElement,width=svg.viewBox.baseVal.width,x=Number(svg.querySelector('.cursor')?.getAttribute('x1')||24)/width*svg.clientWidth;box.style.left=Math.min(Math.max(16,x+24),Math.max(16,card.clientWidth-box.offsetWidth-16))+'px';box.style.top=(svg.offsetTop+14)+'px'}
function chart(config){const svg=$(config.id),allSeries=config.series(),width=Math.max(300,Math.min(1120,svg.clientWidth));svg.replaceChildren();
const empty=noData(allSeries);
if(empty){svg.setAttribute('viewBox','0 0 '+width+' 70');svg.append(node('text',{x:24,y:36,class:'no-data'},(config.id==='cause'?selectedGroup()?.label||'All machines':config.id==='machinePlot'?'Machines':'Hana phase time')+': no data yet'))}
else{
const right=width-Math.min(150,width*.30),left=62,facets=config.id==='cause',panels=facets?allSeries.map(s=>[s]):[allSeries];
const x=t=>left+(t-data.start)/(data.end-data.start)*(right-left),mid=b=>(b.start+b.end)/2;
// Reserve separate rows for moment numbers below the panel title.
const momentRows=[],momentPositions=data.moments.map(event=>{const px=x(event.at);let row=0;while(momentRows[row]?.some(prior=>Math.abs(prior-px)<26))row++;(momentRows[row]??=[]).push(px);return{x:px,y:(facets?40:18)+row*18}});
const top=momentRows.length?(facets?40:18)+(momentRows.length-1)*18+18:(facets?42:32),bottom=Math.max(facets?165:245,top+(facets?90:170)),panelHeight=bottom+(facets?45:55);
svg.setAttribute('viewBox','0 0 '+width+' '+panelHeight*panels.length);svg.dataset.right=right;svg.dataset.left=left;
const defs=node('defs'),pattern=node('pattern',{id:config.id+'Hatch',width:7,height:7,patternUnits:'userSpaceOnUse'});pattern.append(node('path',{d:'M0 7 L7 0',stroke:'currentColor','stroke-width':1,opacity:.6}));defs.append(pattern);svg.append(defs);
panels.forEach((series,panelIndex)=>{const panel=node('g',{class:'plot-panel',transform:'translate(0 '+panelIndex*panelHeight+')'});svg.append(panel);
const direct=series.length<=4,values=series.flatMap(s=>s.values.filter(p=>p.state==='measured'||p.state==='upper bound').map(p=>p.value+(p.unknown||0))),raw=Math.max(0,...values),step=raw?Math.pow(10,Math.floor(Math.log10(raw/4))):.25,nice=raw?Math.ceil(raw/4/step)*step:.25,maximum=nice*4,y=v=>bottom-v/maximum*(bottom-top);
panel.dataset.maximum=maximum;
if(facets)panel.append(node('text',{x:left,y:16,class:'panel-title'},series[0].label+' · '+config.unit()));
for(let tick=0;tick<=4;tick++){const value=nice*tick,at=y(value);panel.append(node('line',{x1:left,x2:right,y1:at,y2:at,class:tick?'grid':'axis'}),node('text',{x:left-9,y:at+4,'text-anchor':'end'},value.toLocaleString(undefined,{maximumFractionDigits:2})))}
if(!values.length)panel.append(node('text',{x:(left+right)/2,y:(top+bottom)/2,'text-anchor':'middle'},'Unavailable — use the table view for reasons'));
data.buckets.forEach((b,i)=>{if(i%Math.max(1,Math.ceil(data.buckets.length/Math.max(2,Math.floor((right-left)/85))))===0)panel.append(node('text',{x:x(mid(b)),y:bottom+24,'text-anchor':'middle'},b.label.slice(4)+(b.thin?' *':'')))});
for(const [index,event]of data.moments.entries()){const marker=node('g',{tabindex:0,role:'img','aria-label':(index+1)+'. '+event.name+' · '+event.local});marker.append(node('title',{},(index+1)+'. '+event.name+' · '+event.scope+' · '+event.local),node('line',{class:'moment-line',x1:x(event.at),x2:x(event.at),y1:top,y2:bottom,stroke:'var(--axis)','stroke-dasharray':'3 5'}),node('text',{class:'moment-number',x:x(event.at),y:momentPositions[index].y,'text-anchor':'middle'},String(index+1)));panel.append(marker)}
const ends=[],segments=new Map();series.forEach(s=>{let previous=null,last=null;s.values.forEach((p,i)=>{if(p.state!=='measured'&&p.state!=='upper bound'){previous=null;return}const px=x(mid(data.buckets[i])),py=y(p.value);if(p.unknown>0){const b=data.buckets[i];panel.append(node('rect',{x:x(b.start)+1,y:y(p.value+p.unknown),width:Math.max(1,x(b.end)-x(b.start)-2),height:y(p.value)-y(p.value+p.unknown),fill:'url(#'+config.id+'Hatch)',color:s.color}))}if(previous){const d='M'+previous.x+' '+previous.y+' L'+px+' '+py,overlap=segments.get(d)||[];const path=node('path',{d,fill:'none',stroke:s.color,'stroke-width':2,'stroke-linecap':'round','stroke-linejoin':'round','stroke-dasharray':s.dash||''});overlap.push(path);segments.set(d,overlap);panel.append(path)}panel.append(node('circle',{class:'series-point','data-series':s.label,cx:px,cy:py,r:4,fill:s.color,stroke:'var(--card)','stroke-width':2}));previous={x:px,y:py};last={x:px,y:py,index:i,series:s}});if(last)ends.push(last)});
// Alternate coincident strokes so each series remains visible.
for(const paths of segments.values())if(paths.length>1)paths.forEach((path,i)=>{path.setAttribute('stroke-dasharray','6 '+6*(paths.length-1));path.setAttribute('stroke-dashoffset',String(-6*i));path.setAttribute('stroke-linecap','butt')});
const coincident=new Map();panel.querySelectorAll('.series-point').forEach(mark=>{const key=mark.getAttribute('cx')+','+mark.getAttribute('cy'),marks=coincident.get(key)||[];marks.push(mark);coincident.set(key,marks)});
for(const marks of coincident.values())if(marks.length>1){const cx=Number(marks[0].getAttribute('cx')),cy=Number(marks[0].getAttribute('cy'));marks.forEach((mark,i)=>{const a=i*2*Math.PI/marks.length,b=(i+1)*2*Math.PI/marks.length,r=6;panel.append(node('path',{class:'overlap-mark','data-series':mark.dataset.series,d:'M'+cx+' '+cy+' L'+(cx+r*Math.cos(a))+' '+(cy+r*Math.sin(a))+' A'+r+' '+r+' 0 '+(b-a>Math.PI?1:0)+' 1 '+(cx+r*Math.cos(b))+' '+(cy+r*Math.sin(b))+' Z',fill:mark.getAttribute('fill'),stroke:'var(--card)','stroke-width':2}))})}
if(direct&&!facets){
 const groups=[];for(const end of ends){const group=groups.find(g=>g.index===end.index&&g.value===end.series.values[end.index].value);if(group)group.ends.push(end);else groups.push({x:end.x,y:end.y,index:end.index,value:end.series.values[end.index].value,ends:[end]})}
 const labels=[],notes=[];
 function keepLabel(label){panel.append(label);const box=label.getBBox();if(box.x+box.width>width-4||labels.some(other=>box.x<other.x+other.width+4&&box.x+box.width+4>other.x&&box.y<other.y+other.height+4&&box.y+box.height+4>other.y)){label.remove();return false}labels.push(box);return true}
 for(const group of groups){const end=group.ends[0],members=group.ends.map(e=>e.series.label),text=members.join(', ')+(members.length>1?' · '+end.series.values[end.index].value.toLocaleString(undefined,{maximumFractionDigits:2}):'');
 const label=node('text',{class:'end-label',x:group.x+10,y:group.y,'dominant-baseline':'middle','data-point-x':group.x,'data-members':JSON.stringify(members)},text);
 // Keep labels at their endpoints. The legend covers labels that cannot fit.
 if(ends.some(other=>!members.includes(other.series.label)&&Math.hypot(group.x+10-other.x,group.y-other.y)<9.9)||!keepLabel(label))continue;
 label.append(node('title',{},members.join(', ')));
 if(end.index<data.buckets.length-1){const reason=end.series.values[end.index+1].reason||'no data',note=node('text',{class:'gap-label',x:group.x+10,y:group.y+22},'Unavailable after '+data.buckets[end.index].label.slice(4));note.append(node('title',{},' — '+reason));notes.push(note)}
 }
 // Give endpoint labels priority over optional gap notes.
 notes.forEach(keepLabel);
}

panel.append(node('line',{class:'cursor',x1:x(mid(data.buckets[selected])),x2:x(mid(data.buckets[selected])),y1:top,y2:bottom,stroke:'var(--muted)','stroke-width':1}));
data.buckets.forEach((b,i)=>{const hit=node('rect',{x:x(b.start),y:top,width:Math.max(1,x(b.end)-x(b.start)),height:bottom-top,fill:'transparent',tabindex:0,role:'button','aria-label':b.label+(b.thin?' thin':'')});hit.addEventListener('pointermove',()=>choose(i,config));hit.addEventListener('focus',()=>choose(i,config));hit.addEventListener('keydown',e=>{if(e.key==='ArrowRight'||e.key==='ArrowLeft'){e.preventDefault();const next=Math.max(0,Math.min(data.buckets.length-1,selected+(e.key==='ArrowRight'?1:-1)));choose(next,config);panel.querySelectorAll('rect[role=button]')[next].focus()}});panel.append(hit)});
});
svg.onpointerleave=()=>{$(config.prefix+'Tooltip').style.display='none'};svg.onfocusout=e=>{if(!svg.contains(e.relatedTarget))$(config.prefix+'Tooltip').style.display='none'};
}
const legend=$(config.prefix+'Legend');legend.replaceChildren();if(!empty&&allSeries.length>=2)allSeries.forEach(s=>{const label=element('span');label.append(key(s),document.createTextNode(s.label));legend.append(label)});table(config,allSeries)}
function details(){const b=data.buckets[selected],p=b.phase;$('phaseDetail').textContent=b.label+(b.thin?' · thin':'')+' — '+p.finished+' hana finishes; '+p.valid+' valid durations; '+p.missing+' missing starts. '+(p.state==='measured'?'Median '+(p.median/3600).toFixed(2)+' h; p90 '+(p.p90/3600).toFixed(2)+' h.':p.reason+'.');$('causeDetail').textContent=b.label+' — '+causeSeries().map(s=>s.label+': '+describe(s.values[selected],$('unit').selectedOptions[0].text)).join(' · ');const idle=b.idle;$('machineDetail').textContent=b.label+' — '+machineSeries().map(s=>s.label+': '+describe(s.values[selected],$('unit').selectedOptions[0].text)).join(' · ')+'. Mac idle seconds: candidate '+idle.candidate.toFixed(0)+', execution '+idle.execution.toFixed(0)+', complete holds '+idle.holds.toFixed(0)+', known '+idle.known.toFixed(0)+', unknown '+idle.unknown.toFixed(0)+', upper bound '+idle.upper.toFixed(0)+'.'}
function choose(i,config){selected=i;configs.forEach(c=>{const svg=$(c.id),b=data.buckets[i],right=Number(svg.dataset.right),x=62+((b.start+b.end)/2-data.start)/(data.end-data.start)*(right-62),cursors=svg.querySelectorAll('.cursor');cursors.forEach(cursor=>{cursor.setAttribute('x1',x);cursor.setAttribute('x2',x)});$(c.prefix+'Tooltip').style.display='none'});details();if(config)showTooltip(config,i)}
function redraw(){$('causeCaption').textContent=$('machine').selectedOptions[0].text+' · '+$('unit').selectedOptions[0].text;$('machineCaption').textContent=$('causeSelect').value+' · '+$('unit').selectedOptions[0].text;configs.forEach(chart);details()}
option($('machine'),'all','All machines');machineGroups.forEach(g=>option($('machine'),g.label,g.label+(noData(groupSeries(g.members))?' · no data yet':'')));data.causes.forEach(c=>option($('causeSelect'),c,c));
for(const id of ['machine','causeSelect','unit'])$(id).addEventListener('change',redraw);
$('theme').addEventListener('change',()=>{if($('theme').value==='system')delete document.documentElement.dataset.theme;else document.documentElement.dataset.theme=$('theme').value;redraw()});
const windowLabel=Math.round((data.end-data.start)/86400000)+' days';$('window').value=windowLabel;$('window').addEventListener('change',()=>{$('windowHelp').textContent='Run buildlog charts --days '+$('window').value.split(' ')[0]+' to generate that window. This page keeps its recorded window.';$('window').value=windowLabel});
$('range').textContent=new Date(data.start).toLocaleDateString(undefined,{timeZone:data.zone})+' – '+new Date(data.end).toLocaleDateString(undefined,{timeZone:data.zone});window.addEventListener('resize',redraw);
for(const text of [...data.moments.map((m,i)=>(i+1)+'. '+m.name+' · '+m.local),...data.pending])$('moments').append(element('span',text));redraw();
</script></body></html>'''
