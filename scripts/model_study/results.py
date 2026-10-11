"""Render reproducible results documents from one study pass."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from compare import ComparisonReport
from phases import PhaseReport
import report

PDT = ZoneInfo("America/Los_Angeles")
MAX_JSON_BYTES = 300_000
QUESTION = "Do Sonnet directors keep their turn time, tokens and work per phase against Opus?"
METHOD = (
    "One request is one director-thread turn; native subagents are excluded.",
    "The headline is the continuation turn triggered by a tool result; prompt turns contribute tokens only.",
    "Both arms use xhigh effort and standard speed; filter drops are counted.",
    "The transcript model fixes each switch; the first request after a model change is excluded.",
    "The Opus baseline is the last 300 filtered requests before Sonnet; concurrent Opus directors form the clock control.",
    "Median differences use 2,000 bootstrap resamples at seed 1; an arm below 30 requests reads too few.",
    "Harm on time, turns or repair rounds recommends Opus; otherwise Sonnet stays.",
    "G1 holds at four switched directors with 150 filtered Sonnet continuation requests each, or at the deadline.",
    "Each run rebuilds its outputs; missing transcripts retain measured rows and reports record what moved.",
    "The scope is natedev; ledger holds and subscription weights are outside this measure.",
)


def metric_line(label: str, unit: str, value: str, before: float | None, after: float | None) -> str:
    return f"{label}: {unit} {value} (Opus {report.number(before)}; Sonnet {report.number(after)})."


def learning(comparison: ComparisonReport, phases: PhaseReport) -> list[str]:
    pooled = comparison["pooled"]
    opus = report.continuation(pooled, "opus") if pooled is not None else None
    sonnet = report.continuation(pooled, "sonnet") if pooled is not None else None
    work = report.phase_comparison(phases, "Pooled switched")
    requests = work["metrics"]["requests_per_1000_words"] if work is not None else None
    repairs = work["metrics"]["repair_rounds"] if work is not None else None
    opus_rate = report.compaction_rate(comparison, "opus")
    sonnet_rate = report.compaction_rate(comparison, "sonnet")
    control = comparison["control"]["directors"]
    control_text = "no control candidate qualified" if not control else "; ".join(
        f"{row['name']} {row['difference']['label']} (before {report.number(row['before_median'])}; after {report.number(row['after_median'])})"
        for row in control)
    net = report.difference(pooled, "net_seconds") if pooled is not None else None
    opus_seconds = opus["seconds_median"] if opus else None
    sonnet_seconds = sonnet["seconds_median"] if sonnet else None
    raw_percent = report.percent_change(opus_seconds, sonnet_seconds)
    net_value = net["value"] if net is not None else None
    raw_text = f"{raw_percent:+.1f}%" if raw_percent is not None else "n/a"
    net_text = f"{net_value:+.2f}" if net_value is not None else "n/a"
    time_line = (f"Time: median continuation seconds {net['label'] if net else 'no control'} net of the clock "
                 f"(Opus {report.number(opus_seconds)}; Sonnet {report.number(sonnet_seconds)}; raw {raw_text}; "
                 f"net {net_text} s, interval [{report.number(net['low'] if net else None)}, "
                 f"{report.number(net['high'] if net else None)}]).")
    return [
        time_line,
        f"Tokens and cost: median output tokens per request {report.metric_label(pooled, 'output')} (Opus {report.number(opus['output_median'] if opus else None)}; Sonnet {report.number(sonnet['output_median'] if sonnet else None)}); mean cost in dollars per request {report.metric_label(pooled, 'cost')} (Opus {report.number(report.request_cost_mean(pooled, 'opus') if pooled else None, 5)}; Sonnet {report.number(report.request_cost_mean(pooled, 'sonnet') if pooled else None, 5)}).",
        f"Turns and repair rounds: requests per 1,000 words {requests['label'] if requests else 'too few phases (n=0 against 0)'} (Opus {report.number(requests['opus_median'] if requests else None)}; Sonnet {report.number(requests['sonnet_median'] if requests else None)}); repair rounds per phase {repairs['label'] if repairs else 'too few phases (n=0 against 0)'} (Opus {report.number(repairs['opus_median'] if repairs else None)}; Sonnet {report.number(repairs['sonnet_median'] if repairs else None)}).",
        f"Compactions: seconds per active hour, Opus {report.number(opus_rate)}; Sonnet {report.number(sonnet_rate)}.",
        f"Control: {control_text}.",
    ]


def render(comparison: ComparisonReport, phases: PhaseReport, extracted: report.ExtractReport,
           now: datetime, history: report.FirstRun | report.FollowingRun) -> str:
    clock = now.replace(tzinfo=PDT) if now.tzinfo is None else now.astimezone(PDT)
    gate, gate_lines = report.ready(comparison, now)
    clause = "four directors at 150" if sum(
        (report.continuation(row, "sonnet") or {"n": 0})["n"] >= 150
        for row in comparison["directors"] if row["status"] == "switched"
    ) >= 4 else "deadline" if gate else "neither clause yet"
    drops = sum(sum(session["dropped"].values()) for session in extracted["sessions"])
    lines = [f"Sample as of {clock:%Y-%m-%d %H:%M PDT}", "", "## Question", "", QUESTION, "",
             "## Data", "", f"Roster: {len(extracted['sessions'])} directors; {len(comparison['directors'])} with measured requests.",
             f"Filters: xhigh, standard speed; {drops} extract drops; Opus baseline last {comparison['baseline_requests']} filtered requests per director.",
             f"Counts: {history.current['pooled']['opus_n']} pooled Opus continuation requests; {history.current['pooled']['sonnet_n']} pooled Sonnet continuation requests.",
             "", "## Method", "", *(f"- {line}" for line in METHOD), "",
             "## The user's measure", "", *report.measure_lines(comparison), "",
             "## Per-director comparison", "", *report.director_table(comparison, phases), "",
             "## Verdict", "", *report.verdict_lines(comparison, phases), "",
             "## Sample gate", "", *gate_lines, f"G1 holds: {'yes' if gate else 'no'}; clause: {clause}.", "",
             "## Learning", "", *learning(comparison, phases), "",
             "## Limits", "", *report.limits(comparison, phases, extracted), "",
             "## Since the last run", "", *report.since_lines(history), "",
             "## How to re-run", "", "Run `python3 scripts/model_study/model_study.py results`.",
             "Per-request rows live in `~/.local/state/model-study/turns.jsonl`, not in git.", ""]
    return "\n".join(lines)


def compact_json(comparison: ComparisonReport, phases: PhaseReport, history: list[report.HistoryRun]) -> str:
    return json.dumps({"compare": comparison, "phases": phases, "history": history[-20:]},
                      separators=(",", ":"), sort_keys=True) + "\n"


def document_paths(docs_dir: Path) -> tuple[Path, Path]:
    return docs_dir / "director-model-study-results.md", docs_dir / "director-model-study-results.json"
