"""Join director requests to completed plan-delegate phases."""

from __future__ import annotations

import json
import os
import tempfile
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal, TypedDict, cast

from compare import OPUS, SONNET, load_turns
from stats import bootstrap_diff, percentile, request_cost
from turns import Turn, load_roster, session_ids

DEFAULT_RUNS = Path.home() / ".local/state/plan-delegate/runs"
DEFAULT_ROSTER = Path(__file__).with_name("roster.json")
DEFAULT_REGISTRY = Path.home() / ".claude/sessions"

type PhaseArm = Literal["opus", "sonnet", "mixed"]
type MetricName = Literal["requests_per_1000_words", "seconds_per_1000_words", "cost_per_1000_words", "repair_rounds"]
METRICS: tuple[MetricName, ...] = ("requests_per_1000_words", "seconds_per_1000_words", "cost_per_1000_words", "repair_rounds")


class AgentIdentity(TypedDict, total=False):
    session_id: str


class HistoryEvent(TypedDict, total=False):
    event_type: str
    timestamp: str
    main_agent: AgentIdentity
    phase_instance_id: str
    phase_id: str
    phase_title: str
    plan_doc: str
    status: str
    round: int
    work_order_words: int
    work_order_lines: int


class PhaseRow(TypedDict):
    director: str
    plan: str
    phase_id: str
    phase_title: str
    arm: PhaseArm
    requests: int
    director_seconds: float
    cost_usd: float
    output_tokens: int
    repair_rounds: int
    findings_opened: int
    abandoned_batches: int
    phase_elapsed_seconds: float
    work_order_words: int | None
    work_order_lines: int | None
    requests_per_1000_words: float | None
    seconds_per_1000_words: float | None
    cost_per_1000_words: float | None


class PhaseDifference(TypedDict):
    opus_n: int
    sonnet_n: int
    opus_median: float | None
    sonnet_median: float | None
    value: float | None
    low: float | None
    high: float | None
    label: str


class PhaseComparison(TypedDict):
    director: str
    metrics: dict[str, PhaseDifference]


class PhaseDrops(TypedDict):
    stopped: int
    errored: int
    empty: int


class PhaseReport(TypedDict):
    phases: list[PhaseRow]
    comparisons: list[PhaseComparison]
    drops: PhaseDrops


def read_events(path: Path) -> list[HistoryEvent]:
    events: list[HistoryEvent] = []
    with path.open() as source:
        for line in source:
            try:
                value = cast(object, json.loads(line))
            except ValueError:
                continue
            if isinstance(value, dict):
                events.append(cast(HistoryEvent, cast(object, value)))
    return events


def event_time(event: HistoryEvent) -> datetime | None:
    raw = event.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        instant = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return instant if instant.tzinfo is not None else None


def nonnegative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def recorded_work_order_size(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def phase_arm(turns: Sequence[Turn]) -> PhaseArm:
    models = {turn.model for turn in turns}
    if models == {OPUS}:
        return "opus"
    if models == {SONNET}:
        return "sonnet"
    return "mixed"


def phase_row(start: HistoryEvent, finish: HistoryEvent, director: str, turns: Sequence[Turn], events: Sequence[HistoryEvent]) -> PhaseRow:
    started = event_time(start)
    ended = event_time(finish)
    assert started is not None and ended is not None
    instance_id = start.get("phase_instance_id")
    related = [event for event in events if event.get("phase_instance_id") == instance_id]
    words = recorded_work_order_size(start.get("work_order_words"))
    requests = len(turns)
    seconds = sum(turn.seconds for turn in turns if turn.seconds is not None)
    cost = sum(request_cost(turn) for turn in turns)
    rate = 1000 / words if words else None
    return {
        "director": director,
        "plan": Path(start.get("plan_doc", "")).stem,
        "phase_id": start.get("phase_id", ""),
        "phase_title": start.get("phase_title", ""),
        "arm": phase_arm(turns),
        "requests": requests,
        "director_seconds": seconds,
        "cost_usd": cost,
        "output_tokens": sum(turn.output for turn in turns),
        "repair_rounds": max((nonnegative_int(event.get("round")) for event in related if event.get("event_type") == "finding_batch_dispatched"), default=0),
        "findings_opened": sum(event.get("event_type") == "finding_opened" for event in related),
        "abandoned_batches": sum(event.get("event_type") == "finding_batch_abandoned" for event in related),
        "phase_elapsed_seconds": (ended - started).total_seconds(),
        "work_order_words": words,
        "work_order_lines": recorded_work_order_size(start.get("work_order_lines")),
        "requests_per_1000_words": requests * rate if rate is not None else None,
        "seconds_per_1000_words": seconds * rate if rate is not None else None,
        "cost_per_1000_words": cost * rate if rate is not None else None,
    }


def phase_difference(opus: Sequence[float], sonnet: Sequence[float]) -> PhaseDifference:
    opus_median = percentile(opus, 50) if opus else None
    sonnet_median = percentile(sonnet, 50) if sonnet else None
    result: PhaseDifference = {
        "opus_n": len(opus), "sonnet_n": len(sonnet),
        "opus_median": opus_median, "sonnet_median": sonnet_median,
        "value": sonnet_median - opus_median if opus_median is not None and sonnet_median is not None else None,
        "low": None, "high": None,
        "label": f"too few phases (n={len(opus)} against {len(sonnet)})",
    }
    if len(opus) >= 8 and len(sonnet) >= 8:
        value, low, high = bootstrap_diff(opus, sonnet, lambda values: percentile(values, 50))
        result.update(value=value, low=low, high=high)
        result["label"] = "fewer" if high < 0 else "more" if low > 0 else "no measurable difference"
    return result


def comparison(name: str, rows: Sequence[PhaseRow]) -> PhaseComparison:
    metrics: dict[str, PhaseDifference] = {}
    for metric in METRICS:
        arms: dict[str, list[float]] = {"opus": [], "sonnet": []}
        for row in rows:
            if row["arm"] in arms:
                value = row[metric]
                if value is not None:
                    arms[row["arm"]].append(float(value))
        metrics[metric] = phase_difference(arms["opus"], arms["sonnet"])
    return {"director": name, "metrics": metrics}


def atomic_json(path: Path, report: PhaseReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as output:
            temporary = output.name
            json.dump(report, output, indent=2)
            _ = output.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def phases(state_dir: Path, runs_dir: Path = DEFAULT_RUNS, roster: Path = DEFAULT_ROSTER, registry_dir: Path = DEFAULT_REGISTRY) -> PhaseReport:
    """Write phase measurements without printing transcript or run paths."""
    entries = load_roster(roster)
    extract_path = state_dir / "extract.json"
    saved_states: dict[str, dict[str, object]] = {}
    if extract_path.exists():
        extraction = cast(dict[str, object], json.loads(extract_path.read_text()))
        saved_states = cast(dict[str, dict[str, object]], extraction.get("session_states", {}))
    by_session: dict[str, tuple[str, list[str]]] = {}
    for entry in entries:
        ids = list(dict.fromkeys([
            *(session_id for session_id, state in saved_states.items() if state.get("director") == entry.name),
            *session_ids(entry, registry_dir),
        ]))
        for session_id in ids:
            by_session[session_id] = (entry.name, ids)
    switched = [entry.name for entry in entries if entry.switched_by_pdt is not None]
    turns_by_session: dict[str, list[tuple[datetime, Turn]]] = defaultdict(list)
    for turn in load_turns(state_dir / "turns.jsonl"):
        turns_by_session[turn.session].append((datetime.fromisoformat(turn.started), turn))
    rows: list[PhaseRow] = []
    stopped = errored = empty = 0
    for path in sorted(runs_dir.glob("*.jsonl")):
        events = read_events(path)
        run_start = next((event for event in events if event.get("event_type") == "run_started"), None)
        if run_start is None:
            continue
        director_identity = by_session.get(run_start.get("main_agent", {}).get("session_id", ""))
        if director_identity is None:
            continue
        director, director_ids = director_identity
        finishes = {event.get("phase_instance_id"): event for event in events if event.get("event_type") == "phase_finished"}
        for start in events:
            if start.get("event_type") != "phase_started":
                continue
            instance_id = start.get("phase_instance_id")
            finish = finishes.get(instance_id)
            if finish is None:
                continue
            if finish.get("status") != "completed":
                if finish.get("status") == "stopped":
                    stopped += 1
                else:
                    errored += 1
                continue
            started, ended = event_time(start), event_time(finish)
            if started is None or ended is None or ended < started:
                continue
            requests = [turn for session_id in director_ids for at, turn in turns_by_session[session_id] if started <= at < ended]
            if not requests:
                empty += 1
                continue
            rows.append(phase_row(start, finish, director, requests, events))
    rows.sort(key=lambda row: row["director"])
    comparisons = [comparison(name, [row for row in rows if row["director"] == name]) for name in switched]
    comparisons.append(comparison("Pooled switched", [row for row in rows if row["director"] in switched]))
    report: PhaseReport = {"phases": rows, "comparisons": comparisons, "drops": {"stopped": stopped, "errored": errored, "empty": empty}}
    atomic_json(state_dir / "phases.json", report)
    return report


def formatted(value: float | int | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:,.{digits}f}"


def work_order_size_text(value: int | None) -> str:
    return "—" if value is None else str(value)


def markdown(report: PhaseReport) -> str:
    lines = ["## Plan-delegate phases", "", "### Every completed phase", "",
             "| Director | Plan | Phase | Title | Arm | Requests | Director s | Cost USD | Output | Repair rounds | Findings | Abandoned batches | Elapsed s | Work Order words / lines | Requests / 1k words | Seconds / 1k words | Cost / 1k words |",
             "| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in report["phases"]:
        lines.append(
            f"| {row['director']} | {row['plan']} | {row['phase_id']} | {row['phase_title']} | {row['arm']} | "
            + f"{row['requests']} | {formatted(row['director_seconds'])} | {formatted(row['cost_usd'], 5)} | "
            + f"{row['output_tokens']} | {row['repair_rounds']} | {row['findings_opened']} | {row['abandoned_batches']} | "
            + f"{formatted(row['phase_elapsed_seconds'])} | {work_order_size_text(row['work_order_words'])} / {work_order_size_text(row['work_order_lines'])} | "
            + f"{formatted(row['requests_per_1000_words'])} | {formatted(row['seconds_per_1000_words'])} | "
            + f"{formatted(row['cost_per_1000_words'], 5)} |"
        )
    lines.extend(["", f"Dropped phases: stopped {report['drops']['stopped']}; errored {report['drops']['errored']}; no requests {report['drops']['empty']}.",
                  "", "### Opus-only against Sonnet-only", "",
                  "| Director | Metric | Opus n / median | Sonnet n / median | Sonnet − Opus [95% interval] | Result |",
                  "| --- | --- | ---: | ---: | --- | --- |"])
    labels = {"requests_per_1000_words": "Requests / 1k words", "seconds_per_1000_words": "Seconds / 1k words",
              "cost_per_1000_words": "Cost / 1k words", "repair_rounds": "Repair rounds"}
    for entry in report["comparisons"]:
        for metric, result in entry["metrics"].items():
            interval = f"{formatted(result['value'])} [{formatted(result['low'])}, {formatted(result['high'])}]"
            lines.append(f"| {entry['director']} | {labels[metric]} | {result['opus_n']} / {formatted(result['opus_median'])} | "
                         + f"{result['sonnet_n']} / {formatted(result['sonnet_median'])} | {interval} | {result['label']} |")
    return "\n".join([*lines, ""])
