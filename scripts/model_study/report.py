"""Render the director model study and its mechanical recommendation."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict, cast
from zoneinfo import ZoneInfo

from compare import CLASSES, ComparisonReport, DirectorComparison, DifferenceEstimate, TurnClassStatistics
from phases import PhaseComparison, PhaseReport, PhaseRow

PDT = ZoneInfo("America/Los_Angeles")
SAMPLE_DEADLINE_PDT = datetime(2026, 10, 7, 10, 30, tzinfo=PDT)
DROP_FIELDS = ("before_director", "sidechain", "no_trigger", "negative", "over_limit", "synthetic")


class ExtractSession(TypedDict):
    name: str
    requests: int
    dropped: dict[str, int]


class ExtractReport(TypedDict):
    sessions: list[ExtractSession]


class HistoryMeasure(TypedDict):
    opus_n: int
    sonnet_n: int
    opus_median_seconds: float | None
    sonnet_median_seconds: float | None
    change_percent: float | None
    net_change_percent: float | None


class DirectorHistoryMeasure(TypedDict):
    name: str
    sonnet_continuation_n: int
    opus_median_seconds: float | None
    sonnet_median_seconds: float | None
    change_percent: float | None
    net_change_percent: float | None


class HistoryRun(TypedDict):
    at: str
    mode: str
    recommendation: str
    pooled: HistoryMeasure
    directors: list[DirectorHistoryMeasure]


@dataclass(frozen=True)
class NotRecorded:
    pass


@dataclass(frozen=True)
class FirstRun:
    current: HistoryRun


@dataclass(frozen=True)
class FollowingRun:
    current: HistoryRun
    previous: HistoryRun


type RunHistory = NotRecorded | FirstRun | FollowingRun
NO_HISTORY = NotRecorded()


def percent_change(before: float | None, after: float | None) -> float | None:
    return (after - before) / before * 100 if before not in (None, 0) and after is not None else None


def history_measure(row: DirectorComparison | None) -> HistoryMeasure:
    opus = continuation(row, "opus") if row is not None else None
    sonnet = continuation(row, "sonnet") if row is not None else None
    before = opus["seconds_median"] if opus is not None else None
    after = sonnet["seconds_median"] if sonnet is not None else None
    net = difference(row, "net_seconds") if row is not None else None
    net_value = net["value"] if net is not None else None
    return {"opus_n": opus["n"] if opus is not None else 0,
            "sonnet_n": sonnet["n"] if sonnet is not None else 0,
            "opus_median_seconds": before, "sonnet_median_seconds": after,
            "change_percent": percent_change(before, after),
            "net_change_percent": net_value / before * 100 if before not in (None, 0) and net_value is not None else None}


def history_row(comparison: ComparisonReport, phases: PhaseReport, now: datetime, final: bool) -> HistoryRun:
    clock = now.replace(tzinfo=PDT) if now.tzinfo is None else now
    directors: list[DirectorHistoryMeasure] = []
    for row in comparison["directors"]:
        measured = history_measure(row)
        directors.append({"name": row["name"], "sonnet_continuation_n": measured["sonnet_n"],
                          "opus_median_seconds": measured["opus_median_seconds"],
                          "sonnet_median_seconds": measured["sonnet_median_seconds"],
                          "change_percent": measured["change_percent"],
                          "net_change_percent": measured["net_change_percent"]})
    return {"at": clock.astimezone(timezone.utc).isoformat(), "mode": "final" if final else "interim",
            "recommendation": verdict(comparison, phases), "pooled": history_measure(comparison["pooled"]),
            "directors": directors}


def load_history(path: Path) -> list[HistoryRun]:
    if not path.exists():
        return []
    return [cast(HistoryRun, json.loads(line)) for line in path.read_text().splitlines() if line]


def write_history(path: Path, runs: list[HistoryRun]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as output:
            temporary = output.name
            _ = output.write("".join(json.dumps(run, separators=(",", ":")) + "\n" for run in runs))
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def record_history(path: Path, current: HistoryRun, save: bool = True) -> tuple[list[HistoryRun], FirstRun | FollowingRun]:
    runs = load_history(path)
    previous = next((run for run in reversed(runs) if run["at"] != current["at"]), None)
    if save:
        positions = [index for index, run in enumerate(runs) if run["at"] == current["at"]]
        if positions:
            runs[positions[-1]] = current
        else:
            runs.append(current)
        write_history(path, runs)
    return runs, FirstRun(current) if previous is None else FollowingRun(current, previous)


def since_lines(history: FirstRun | FollowingRun) -> list[str]:
    if isinstance(history, FirstRun):
        return ["first run"]
    current, previous = history.current, history.previous
    at = datetime.fromisoformat(previous["at"]).astimezone(PDT)
    lines = [f"Previous run: {at:%Y-%m-%d %H:%M PDT} ({previous['mode']})."]
    prior = {row["name"]: row for row in previous["directors"]}
    for row in current["directors"]:
        earlier = prior.get(row["name"])
        old_n = earlier["sonnet_continuation_n"] if earlier is not None else 0
        old_change = earlier["change_percent"] if earlier is not None else None
        net = (f"; net {number(earlier.get('net_change_percent'))}% → {number(row.get('net_change_percent'))}%"
               if earlier is not None and earlier.get("net_change_percent") is not None and row.get("net_change_percent") is not None else "")
        lines.append(f"{row['name']}: Sonnet continuation n {old_n} → {row['sonnet_continuation_n']}; median change {number(old_change)}% → {number(row['change_percent'])}%{net}.")
    lines.append(since_pooled_line(history))
    lines.append("recommendation unchanged" if current["recommendation"] == previous["recommendation"]
                 else f"recommendation changed from {previous['recommendation']} to {current['recommendation']}")
    return lines


def since_pooled_line(history: FirstRun | FollowingRun) -> str:
    if isinstance(history, FirstRun):
        return "Pooled switched: first run."
    current, previous = history.current, history.previous
    old = previous["pooled"]
    new = current["pooled"]
    net = (f"; net then {number(old.get('net_change_percent'))}%, now {number(new.get('net_change_percent'))}%"
           if old.get("net_change_percent") is not None and new.get("net_change_percent") is not None else "")
    return f"Pooled switched: median change then {number(old['change_percent'])}%, now {number(new['change_percent'])}%{net}."


def number(value: float | int | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:,.{digits}f}"


def size(value: int | None) -> str:
    return "—" if value in (None, 0) else str(value)


def continuation(row: DirectorComparison, arm: str) -> TurnClassStatistics | None:
    return row["classes"].get(arm, {}).get("continuation")


def request_cost_mean(row: DirectorComparison, arm: str) -> float | None:
    """Weight the disjoint continuation and prompt classes by request count."""
    classes = row["classes"].get(arm, {})
    parts = [classes[name] for name in ("continuation", "prompt") if name in classes]
    count = sum(part["n"] for part in parts)
    if count == 0:
        return None
    return sum(part["cost_mean"] * part["n"] for part in parts if part["cost_mean"] is not None) / count


def difference(row: DirectorComparison, metric: str) -> DifferenceEstimate | None:
    differences = row["differences"]
    return differences.get(metric) if differences is not None else None


def measure_line(row: DirectorComparison) -> str:
    opus = continuation(row, "opus")
    sonnet = continuation(row, "sonnet")
    before = opus["seconds_median"] if opus is not None else None
    after = sonnet["seconds_median"] if sonnet is not None else None
    text = f"{row['name']}: median continuation-turn seconds Opus {number(before) if before is not None else 'n/a'} → Sonnet {number(after) if after is not None else 'n/a'}"
    if before not in (None, 0) and after is not None:
        text += f" ({(after - before) / before:+.1%})"
    net = difference(row, "net_seconds")
    if net is not None:
        value = net["value"]
        if value is None:
            text += "; net of the clock n/a (no control director qualified)" if net["label"] == "no control" else "; net of the clock n/a (too few timed requests)"
        else:
            net_percent = f"{value / before:+.1%}" if before not in (None, 0) else "n/a"
            text += (f"; net of the clock {value:+.2f} s ({net_percent} of the Opus median), "
                     f"95% interval [{number(net['low'])}, {number(net['high'])}] s — {net['label']}")
    return text


def measure_lines(comparison: ComparisonReport) -> list[str]:
    rows = [row for row in comparison["directors"] if row["status"] == "switched"]
    if comparison["pooled"] is not None:
        rows.append(comparison["pooled"])
    return [measure_line(row) for row in rows]


def phase_comparison(phases: PhaseReport, director: str) -> PhaseComparison | None:
    return next((row for row in phases["comparisons"] if row["director"] == director), None)


def metric_label(row: DirectorComparison | None, metric: str) -> str:
    if row is None:
        return "too few"
    result = difference(row, metric)
    return result["label"] if result is not None else "too few"


def work_label(phases: PhaseReport, metric: str) -> str:
    pooled = phase_comparison(phases, "Pooled switched")
    return pooled["metrics"][metric]["label"] if pooled is not None else "too few phases (n=0 against 0)"


def verdict(comparison: ComparisonReport, phases: PhaseReport) -> str:
    """Choose the default from the measured labels alone."""
    pooled = comparison["pooled"]
    time = metric_label(pooled, "net_seconds")
    cost = metric_label(pooled, "cost")
    requests = work_label(phases, "requests_per_1000_words")
    repairs = work_label(phases, "repair_rounds")
    if time == "slower" or requests == "more" or repairs == "more":
        return "opus"
    if cost == "lower" or time == "faster":
        return "sonnet"
    return "sonnet stays (no measurable difference; evidence thin)"


def recommendation_line(comparison: ComparisonReport, phases: PhaseReport) -> str:
    pooled = comparison["pooled"]
    causes: list[str] = []
    if metric_label(pooled, "net_seconds") == "slower":
        causes.append("continuation time is slower")
    if work_label(phases, "requests_per_1000_words") == "more":
        causes.append("requests per 1,000 Work Order words are more")
    if work_label(phases, "repair_rounds") == "more":
        causes.append("repair rounds per phase are more")
    if causes:
        return "Recommendation: opus — " + "; ".join(causes) + "."
    if metric_label(pooled, "cost") == "lower":
        return "Recommendation: sonnet — cost per request is lower."
    if metric_label(pooled, "net_seconds") == "faster":
        return "Recommendation: sonnet — continuation time is faster."
    return "Recommendation: sonnet stays (no measurable difference; evidence thin)."


def compaction_rate(comparison: ComparisonReport, arm: str) -> float | None:
    switched = {row["name"] for row in comparison["directors"] if row["status"] == "switched"}
    rows = [row for row in comparison["compactions"] if row["name"] in switched and row["arm"] == arm]
    if {row["name"] for row in rows} != switched or not rows:
        return None
    hours = sum(row["active_hours"] for row in rows)
    if hours == 0 or any(row["seconds_per_active_hour"] is None for row in rows):
        return None
    return sum(row["seconds_per_active_hour"] * row["active_hours"] for row in rows if row["seconds_per_active_hour"] is not None) / hours


def verdict_lines(comparison: ComparisonReport, phases: PhaseReport) -> list[str]:
    pooled = comparison["pooled"]
    opus_rate = compaction_rate(comparison, "opus")
    sonnet_rate = compaction_rate(comparison, "sonnet")
    ratio = sonnet_rate / opus_rate if opus_rate not in (None, 0) and sonnet_rate is not None else None
    return [
        f"time: pooled continuation seconds {metric_label(pooled, 'net_seconds')} net of the clock (raw {metric_label(pooled, 'seconds')})",
        f"tokens: output tokens {metric_label(pooled, 'output')}; cost per request {metric_label(pooled, 'cost')}",
        f"work: requests per 1,000 Work Order words {work_label(phases, 'requests_per_1000_words')}; repair rounds {work_label(phases, 'repair_rounds')}",
        f"compaction: Sonnet / Opus {number(ratio)}× (Opus {number(opus_rate)} s/active h; Sonnet {number(sonnet_rate)} s/active h)",
        recommendation_line(comparison, phases),
        f"Default for enh-showrunner Phase 6: {'opus' if verdict(comparison, phases) == 'opus' else 'sonnet'}",
    ]


def director_table(comparison: ComparisonReport, phases: PhaseReport) -> list[str]:
    lines = [
        "| Director | Opus n | Sonnet n | First Sonnet request PDT | Continuation median s O → S | Raw Δ seconds and label | Output median O → S | Mean cost USD O → S | Repair rounds median O → S |",
        "| --- | ---: | ---: | --- | --- | --- | --- | --- | --- |",
    ]
    for row in comparison["directors"]:
        opus = continuation(row, "opus")
        sonnet = continuation(row, "sonnet")
        delta = difference(row, "seconds")
        delta_text = f"{number(delta['value'])} ({delta['label']})" if delta is not None else "—"
        work = phase_comparison(phases, row["name"])
        repair = work["metrics"]["repair_rounds"] if work is not None else None
        repair_text = f"{number(repair['opus_median'])} → {number(repair['sonnet_median'])}" if repair is not None else "— → —"
        lines.append(
            f"| {row['name']} | {row['counts']['opus_baseline']} | {row['counts']['sonnet']} | {row['first_sonnet_pdt'] or '—'} | "
            + f"{number(opus['seconds_median'] if opus is not None else None)} → {number(sonnet['seconds_median'] if sonnet is not None else None)} | {delta_text} | "
            + f"{number(opus['output_median'] if opus is not None else None)} → {number(sonnet['output_median'] if sonnet is not None else None)} | "
            + f"{number(request_cost_mean(row, 'opus'), 5)} → {number(request_cost_mean(row, 'sonnet'), 5)} | {repair_text} |"
        )
    return lines


def class_table(comparison: ComparisonReport) -> list[str]:
    lines = [
        "| Arm | Class | n / timed | Median s [p25, p75, p90] | Output | Thinking | Fresh input | Cache read | Context | Output/s | Cost USD |",
        "| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    pooled = comparison["pooled"]
    if pooled is None:
        return [*lines, "No switched directors qualified for a pooled class table."]
    for arm in ("opus", "sonnet"):
        for name in CLASSES:
            row = pooled["classes"][arm][name]
            interval = f"{number(row['seconds_median'])} [{number(row['seconds_p25'])}, {number(row['seconds_p75'])}, {number(row['seconds_p90'])}]"
            lines.append(
                f"| {arm} | {name} | {row['n']} / {row['timed_n']} | {interval} | {number(row['output_median'])} | "
                + f"{number(row['thinking_median'])} | {number(row['fresh_input_median'])} | {number(row['cache_read_median'])} | "
                + f"{number(row['context_median'])} | {number(row['output_per_second_median'])} | {number(row['cost_mean'], 5)} |"
            )
    return lines


def control_section(comparison: ComparisonReport) -> list[str]:
    control = comparison["control"]
    pooled = control["pooled"]
    change = pooled["change_seconds"]
    change_percent = change / pooled["before_median"] if change is not None and pooled["before_median"] not in (None, 0) else None
    change_text = f"{change:+.2f} s ({change_percent:+.1%})" if change is not None and change_percent is not None else "n/a"
    pooled_line = (f"Pooled control at {control['at_pdt'] or '—'}: before n {pooled['before_n']}, median {number(pooled['before_median'])} s; "
                   f"after n {pooled['after_n']}, median {number(pooled['after_median'])} s; change {change_text}.")
    if not control["directors"]:
        return [pooled_line, "no control candidate qualified (30 filtered Opus continuation requests on each side of T)"]
    lines = [f"T: {control['at_pdt'] or '—'}", pooled_line, "", "| Director | Before n / median s | After n / median s | Δ seconds and label |", "| --- | ---: | ---: | --- |"]
    for row in control["directors"]:
        delta = row["difference"]
        lines.append(f"| {row['name']} | {row['before_n']} / {number(row['before_median'])} | {row['after_n']} / {number(row['after_median'])} | {number(delta['value'])} ({delta['label']}) |")
    return lines


def compaction_section(comparison: ComparisonReport) -> list[str]:
    lines = ["| Director | Arm | Compactions / requests | Per 100 requests | Per active h | Duration median ms | Seconds per active h | Post requests | Post seconds ratio | Post write ratio |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in comparison["compactions"]:
        lines.append(
            f"| {row['name']} | {row['arm']} | {row['compactions']} / {row['requests']} | {number(row['per_100_requests'])} | "
            + f"{number(row['per_active_hour'])} | {number(row['duration_ms_median'])} | {number(row['seconds_per_active_hour'])} | "
            + f"{row['post_requests']} | {number(row['post_seconds_ratio'])} | {number(row['post_write_ratio'])} |"
        )
    return lines


def phase_row_line(row: PhaseRow) -> str:
    return (f"| {row['director']} | {row['arm']} | {row['requests']} | {number(row['director_seconds'])} | "
            f"{number(row['cost_usd'], 5)} | {row['output_tokens']} | {row['repair_rounds']} | {row['findings_opened']} | "
            f"{row['abandoned_batches']} | {number(row['phase_elapsed_seconds'])} | {size(row['work_order_words'])} / {size(row['work_order_lines'])} | "
            f"{number(row['requests_per_1000_words'])} | {number(row['seconds_per_1000_words'])} | {number(row['cost_per_1000_words'], 5)} |")


def work_section(phases: PhaseReport) -> list[str]:
    lines =["| Director | Opus phases | Sonnet phases | Repair rounds median O → S | Repair label | Requests / 1k words label |",
             "| --- | ---: | ---: | --- | --- | --- |"]
    for work in phases["comparisons"]:
        director = work["director"]
        if director == "Pooled switched":
            continue
        repair = work["metrics"]["repair_rounds"]
        requests = work["metrics"]["requests_per_1000_words"]
        lines.append(f"| {director} | {repair['opus_n']} | {repair['sonnet_n']} | {number(repair['opus_median'])} → {number(repair['sonnet_median'])} | {repair['label']} | {requests['label']} |")
    lines.extend(["", "| Director | Arm | Requests | Director s | Cost USD | Output | Repair rounds | Findings | Abandoned batches | Elapsed s | Work Order words / lines | Requests / 1k words | Seconds / 1k words | Cost / 1k words |",
                  "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: |"])
    lines.extend(phase_row_line(row) for row in phases["phases"])
    return lines


def limits(comparison: ComparisonReport, phases: PhaseReport, extracted: ExtractReport) -> list[str]:
    lines = ["Requests are filtered xhigh, standard-speed requests; Opus uses each director's last 300 before Sonnet. Sample sizes and drops follow."]
    sessions = {row["name"]: row for row in extracted["sessions"]}
    for row in comparison["directors"]:
        name = row["name"]
        counts = row["counts"]
        lines.append(f"{name}: Opus all {counts['opus_all']}, baseline {counts['opus_baseline']}, Sonnet {counts['sonnet']}, other-model {counts['other']}; status {row['status']}.")
        session = sessions.get(name)
        if session is not None:
            drops = session["dropped"]
            lines.append(f"{name} extract drops: " + ", ".join(f"{field} {drops.get(field, 0)}" for field in DROP_FIELDS) + ".")
        for arm in ("opus", "sonnet", "other"):
            drop = row["drops"].get(arm)
            if drop is not None:
                lines.append(f"{name} {arm} filter drops: switch turn {drop['switch_turn']}, effort {drop['effort']}, speed {drop['speed']}.")
        if row["status"] == "Sonnet-only":
            lines.append(f"{name} is Sonnet-only; there is no Opus baseline.")
        if row["status"] == "no eligible requests":
            lines.append(f"{name} has no eligible requests; other-model and filter drops are shown above.")
    compared = {row["name"] for row in comparison["directors"]}
    for session in extracted["sessions"]:
        name = session["name"]
        if name in compared:
            continue
        lines.append(f"{name}: no requests extracted.")
        drops = session["dropped"]
        lines.append(f"{name} extract drops: " + ", ".join(f"{field} {drops.get(field, 0)}" for field in DROP_FIELDS) + ".")
    pooled = phase_comparison(phases, "Pooled switched")
    if pooled is not None:
        repair = pooled["metrics"]["repair_rounds"]
        requests = pooled["metrics"]["requests_per_1000_words"]
        lines.append(f"Pooled work comparison: {repair['opus_n']} Opus phases and {repair['sonnet_n']} Sonnet phases for repair rounds; {requests['opus_n']} and {requests['sonnet_n']} with recorded Work Order size for per-1,000-word rates.")
    dropped = phases["drops"]
    lines.append(f"Phase drops: stopped {dropped['stopped']}, errored {dropped['errored']}, no request {dropped['empty']}.")
    if any(row["work_order_words"] in (None, 0) for row in phases["phases"]):
        lines.append("A phase with no recorded Work Order size has no per-1,000-word rate (—).")
    unknown = [f"{row['name']} {row['arm']}" for row in comparison["compactions"] if row["compactions"] and row["seconds_per_active_hour"] is None]
    if unknown:
        lines.append("Compaction duration unknown or active hours unavailable for " + ", ".join(unknown) + "; its rate reads —.")
    control = comparison["control"]
    pooled_control = control["pooled"]
    lines.append(f"Clock control boundary {control['at_pdt'] or '—'}: {len(control['directors'])} director(s) qualified; {pooled_control['before_n']} continuation requests before and {pooled_control['after_n']} after.")
    lines.extend([
        "The study covers natedev only.",
        "Costs are API-equivalent estimates, not subscription weights.",
        "Holds and merge defects in the review ledger are not joined; decision quality beyond repair rounds is not measured.",
    ])
    return lines


def render(comparison: ComparisonReport, phases: PhaseReport, extracted: ExtractReport, now: datetime, final: bool = False,
           history: RunHistory = NO_HISTORY) -> str:
    kind = "FINAL" if final else "INTERIM"
    clock = now.replace(tzinfo=PDT) if now.tzinfo is None else now.astimezone(PDT)
    since = ["## Since the last run", "", *since_lines(history), ""] if not isinstance(history, NotRecorded) else []
    lines = [f"# {kind} director model study — {clock:%Y-%m-%d %H:%M PDT}", "", "## The user's measure", "",
             *measure_lines(comparison), "", *since, "## Verdict", "", *verdict_lines(comparison, phases), "",
             "## Per-director comparison", "", *director_table(comparison, phases), "", "## Pooled turn classes", "",
             *class_table(comparison), "", "## Control", "", *control_section(comparison), "", "## Compactions", "",
             *compaction_section(comparison), "", "## Work per phase", "", *work_section(phases), "", "## Limits", "",
             *limits(comparison, phases, extracted), ""]
    return "\n".join(lines)


def message(comparison: ComparisonReport, phases: PhaseReport, extracted: ExtractReport, now: datetime, final: bool, path: Path,
            history: RunHistory = NO_HISTORY) -> str:
    kind = "FINAL" if final else "INTERIM"
    clock = now.replace(tzinfo=PDT) if now.tzinfo is None else now.astimezone(PDT)
    dropped = sum(sum(session["dropped"].values()) for session in extracted["sessions"])
    since = [since_pooled_line(history)] if not isinstance(history, NotRecorded) else []
    measures = measure_lines(comparison)
    lines = [f"From model-study-unit: {kind} director model study — {verdict(comparison, phases)}.",
             *measures, *since,
             "Verdict:", *verdict_lines(comparison, phases), "Per-director comparison:"]
    table = director_table(comparison, phases)
    ending = ["Limits: " + f"as of {clock:%Y-%m-%d %H:%M PDT}; {len(comparison['directors'])} directors; {dropped} extract drops; {phases['drops']['stopped']} stopped, {phases['drops']['errored']} errored, {phases['drops']['empty']} phases without requests; natedev only; API-equivalent cost; clock and decision-quality limits apply.",
              str(path)]
    if len(lines) + len(table) + len(ending) > 60 and len(table) > 2:
        keep = max(0, 60 - len(lines) - 2 - len(ending) - 1)
        hidden = len(table) - 2 - keep
        table = [*table[:2 + keep], f"… {hidden} more directors in report.md"]
    if len(lines) + len(table) + len(ending) > 60:
        pooled = measures[-1:] if comparison["pooled"] is not None else []
        director_measures = measures[:-1] if pooled else measures
        keep = max(0, 60 - 1 - len(pooled) - len(since) - 1 - len(verdict_lines(comparison, phases))
                   - 1 - len(table) - len(ending) - 1)
        hidden = len(director_measures) - keep
        trimmed = [*director_measures[:keep], *pooled, f"… {hidden} more directors in report.md"]
        lines = [lines[0], *trimmed, *since, "Verdict:", *verdict_lines(comparison, phases),
                 "Per-director comparison:"]
    lines.extend([*table, *ending])
    return "\n".join(lines) + "\n"


def ready(comparison: ComparisonReport, now: datetime | None = None) -> tuple[bool, list[str]]:
    clock = now or datetime.now(PDT)
    clock = clock.replace(tzinfo=PDT) if clock.tzinfo is None else clock.astimezone(PDT)
    switched = [row for row in comparison["directors"] if row["status"] == "switched"]
    lines = [f"Deadline: {SAMPLE_DEADLINE_PDT:%Y-%m-%d %H:%M PDT}"]
    qualified = 0
    for row in switched:
        sonnet = continuation(row, "sonnet")
        count = sonnet["n"] if sonnet is not None else 0
        lines.append(f"{row['name']}: {count} filtered Sonnet continuation requests")
        qualified += count >= 150
    gate = qualified >= 4 or clock >= SAMPLE_DEADLINE_PDT
    lines.append(f"G1: {'ready' if gate else 'waiting'} ({qualified} switched directors at 150; deadline {'passed' if clock >= SAMPLE_DEADLINE_PDT else 'pending'})")
    return gate, lines
