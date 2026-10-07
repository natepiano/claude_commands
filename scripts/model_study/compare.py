"""Compare eligible director requests and compactions by model."""

from __future__ import annotations

import json
import os
import statistics
import tempfile
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, TypedDict, cast
from zoneinfo import ZoneInfo

from stats import PRICES, bootstrap_diff, label_difference, percentile, request_cost
from turns import Compaction, Turn

OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"
BASELINE_REQUESTS = 300
PDT = ZoneInfo("America/Los_Angeles")
CLASSES = ("continuation", "continuation/tool_use", "continuation/end_turn", "prompt")

type ExtractedRow = dict[str, object]
type DirectorStatus = Literal["switched", "Sonnet-only", "no eligible requests", "control candidate", "pooled"]


class DropSummary(TypedDict):
    switch_turn: int
    effort: int
    speed: int


class ArmCounts(TypedDict):
    opus_all: int
    opus_baseline: int
    sonnet: int
    other: int


class TurnClassStatistics(TypedDict):
    n: int
    timed_n: int
    seconds_median: float | None
    seconds_p25: float | None
    seconds_p75: float | None
    seconds_p90: float | None
    output_median: float | None
    thinking_median: float | None
    fresh_input_median: float | None
    cache_read_median: float | None
    context_median: float | None
    output_per_second_median: float | None
    cost_mean: float | None


class DifferenceEstimate(TypedDict):
    value: float | None
    low: float | None
    high: float | None
    label: str
    opus_n: int
    sonnet_n: int


class DirectorComparison(TypedDict):
    name: str
    status: DirectorStatus
    first_sonnet_pdt: str | None
    counts: ArmCounts
    drops: dict[str, DropSummary]
    classes: dict[str, dict[str, TurnClassStatistics]]
    differences: dict[str, DifferenceEstimate] | None


class ControlDirector(TypedDict):
    name: str
    before_n: int
    after_n: int
    before_median: float | None
    after_median: float | None
    difference: DifferenceEstimate


class ConcurrentControl(TypedDict):
    at_pdt: str | None
    directors: list[ControlDirector]


class CompactionComparison(TypedDict):
    name: str
    arm: str
    requests: int
    compactions: int
    active_hours: float
    per_100_requests: float | None
    per_active_hour: float | None
    pre_tokens_median: float | None
    duration_ms_median: float | None
    seconds_per_active_hour: float | None
    post_requests: int
    other_requests: int
    post_seconds_ratio: float | None
    post_write_ratio: float | None


class PriceColumns(TypedDict):
    input: float
    output: float
    write_5m: float
    write_1h: float
    cache_read: float


class ComparisonReport(TypedDict):
    baseline_requests: int
    prices_usd_per_million: dict[str, PriceColumns]
    directors: list[DirectorComparison]
    pooled: DirectorComparison | None
    control: ConcurrentControl
    compactions: list[CompactionComparison]


@dataclass
class DropCounts:
    switch_turn: int = 0
    effort: int = 0
    speed: int = 0

    def to_json(self) -> DropSummary:
        return {"switch_turn": self.switch_turn, "effort": self.effort, "speed": self.speed}


def text_field(row: ExtractedRow, key: str) -> str:
    return cast(str, row[key])


def int_field(row: ExtractedRow, key: str) -> int:
    return cast(int, row[key])


def optional_number(row: ExtractedRow, key: str) -> float | None:
    return cast(float | None, row[key])


def load_turns(path: Path) -> list[Turn]:
    turns: list[Turn] = []
    with path.open() as source:
        for line in source:
            row = cast(ExtractedRow, json.loads(line))
            turns.append(Turn(
                session=text_field(row, "session"), name=text_field(row, "name"),
                request_id=text_field(row, "request_id"), started=text_field(row, "started"),
                ended=text_field(row, "ended"), seconds=optional_number(row, "seconds"),
                model=text_field(row, "model"), effort=text_field(row, "effort"),
                speed=text_field(row, "speed"), stop=text_field(row, "stop"),
                kind=text_field(row, "kind"), input=int_field(row, "input"),
                output=int_field(row, "output"), thinking=int_field(row, "thinking"),
                cache_read=int_field(row, "cache_read"), write_5m=int_field(row, "write_5m"),
                write_1h=int_field(row, "write_1h"), context=int_field(row, "context"),
                switch_turn=cast(bool, row["switch_turn"]), after_compact=int_field(row, "after_compact"),
            ))
    return turns


def load_compactions(path: Path) -> list[Compaction]:
    compactions: list[Compaction] = []
    with path.open() as source:
        for line in source:
            row = cast(ExtractedRow, json.loads(line))
            compactions.append(Compaction(
                session=text_field(row, "session"), name=text_field(row, "name"),
                at=text_field(row, "at"), trigger=text_field(row, "trigger"),
                pre_tokens=int_field(row, "pre_tokens"), post_tokens=int_field(row, "post_tokens"),
                duration_ms=cast(int | None, row["duration_ms"]), model=text_field(row, "model"),
            ))
    return compactions


def director_names(turns: Sequence[Turn]) -> list[str]:
    return list(dict.fromkeys(turn.name for turn in turns))


def eligible_turns(turns: Sequence[Turn]) -> tuple[dict[str, dict[str, list[Turn]]], dict[str, dict[str, DropCounts]], dict[str, int]]:
    arms: dict[str, dict[str, list[Turn]]] = defaultdict(lambda: {"opus": [], "sonnet": []})
    drops: dict[str, dict[str, DropCounts]] = defaultdict(lambda: {
        "opus": DropCounts(), "sonnet": DropCounts(), "other": DropCounts(),
    })
    other: dict[str, int] = defaultdict(int)
    for turn in turns:
        arm = "opus" if turn.model == OPUS else "sonnet" if turn.model == SONNET else "other"
        if arm == "other":
            other[turn.name] += 1
        dropped = drops[turn.name][arm]
        if turn.switch_turn:
            dropped.switch_turn += 1
        elif turn.effort != "xhigh":
            dropped.effort += 1
        elif turn.speed != "standard":
            dropped.speed += 1
        elif arm != "other":
            arms[turn.name][arm].append(turn)
    for director_arms in arms.values():
        for arm_turns in director_arms.values():
            arm_turns.sort(key=lambda turn: turn.started)
    return arms, drops, other


def request_class(turn: Turn) -> str:
    if turn.kind == "continuation" and turn.stop in ("tool_use", "end_turn"):
        return f"continuation/{turn.stop}"
    return "prompt"


def class_turns(turns: Sequence[Turn], name: str) -> list[Turn]:
    if name == "continuation":
        return [turn for turn in turns if request_class(turn).startswith("continuation/")]
    return [turn for turn in turns if request_class(turn) == name]


def median(values: Sequence[float | int]) -> float | None:
    return percentile(values, 50) if values else None


def mean(values: Sequence[float | int]) -> float | None:
    return statistics.fmean(values) if values else None


def class_statistics(turns: Sequence[Turn], name: str) -> TurnClassStatistics:
    chosen = class_turns(turns, name)
    timed = [turn.seconds for turn in chosen if turn.seconds is not None] if name != "prompt" else []
    seconds = timed
    rates = [turn.output / turn.seconds for turn in chosen if turn.seconds is not None and turn.seconds >= 0.5] if name != "prompt" else []
    return {
        "n": len(chosen), "timed_n": len(seconds),
        "seconds_median": median(seconds), "seconds_p25": percentile(seconds, 25) if seconds else None,
        "seconds_p75": percentile(seconds, 75) if seconds else None,
        "seconds_p90": percentile(seconds, 90) if seconds else None,
        "output_median": median([turn.output for turn in chosen]),
        "thinking_median": median([turn.thinking for turn in chosen]),
        "fresh_input_median": median([turn.input + turn.write_5m + turn.write_1h for turn in chosen]),
        "cache_read_median": median([turn.cache_read for turn in chosen]),
        "context_median": median([turn.context for turn in chosen]),
        "output_per_second_median": median(rates),
        "cost_mean": mean([request_cost(turn) for turn in chosen]),
    }


def difference(a: Sequence[float | int], b: Sequence[float | int], metric: str, use_mean: bool = False) -> DifferenceEstimate:
    if not a or not b:
        return {"value": None, "low": None, "high": None, "label": "too few", "opus_n": len(a), "sonnet_n": len(b)}
    statistic: Callable[[Sequence[float | int]], float] = statistics.fmean if use_mean else lambda values: percentile(values, 50)
    value, low, high = bootstrap_diff(a, b, statistic)
    return {
        "value": value, "low": low, "high": high,
        "label": label_difference(low, high, len(a), len(b), metric),
        "opus_n": len(a), "sonnet_n": len(b),
    }


def arm_differences(opus: Sequence[Turn], sonnet: Sequence[Turn]) -> dict[str, DifferenceEstimate]:
    opus_continuation = class_turns(opus, "continuation")
    sonnet_continuation = class_turns(sonnet, "continuation")
    return {
        "seconds": difference(
            [turn.seconds for turn in opus_continuation if turn.seconds is not None],
            [turn.seconds for turn in sonnet_continuation if turn.seconds is not None], "seconds",
        ),
        "output": difference([turn.output for turn in opus], [turn.output for turn in sonnet], "tokens"),
        "cost": difference([request_cost(turn) for turn in opus], [request_cost(turn) for turn in sonnet], "cost", True),
    }


def first_sonnet_time(turns: Sequence[Turn], name: str) -> datetime | None:
    times = [datetime.fromisoformat(turn.started) for turn in turns if turn.name == name and turn.model == SONNET]
    return min(times) if times else None


def pdt_time(instant: datetime | None) -> str | None:
    return instant.astimezone(PDT).strftime("%Y-%m-%d %H:%M PDT") if instant else None


def director_result(name: str, status: DirectorStatus, opus_all: list[Turn], sonnet: list[Turn], first_sonnet: datetime | None, dropped: dict[str, DropCounts], other: int) -> DirectorComparison:
    opus = [turn for turn in opus_all if first_sonnet is None or datetime.fromisoformat(turn.started) < first_sonnet][-BASELINE_REQUESTS:]
    classes: dict[str, dict[str, TurnClassStatistics]] = {}
    if status != "no eligible requests":
        for arm_name, arm_turns in (("opus_all", opus_all), ("opus", opus), ("sonnet", sonnet)):
            classes[arm_name] = {name: class_statistics(arm_turns, name) for name in CLASSES}
    return {
        "name": name, "status": status, "first_sonnet_pdt": pdt_time(first_sonnet),
        "counts": {"opus_all": len(opus_all), "opus_baseline": len(opus), "sonnet": len(sonnet), "other": other},
        "drops": {arm: counts.to_json() for arm, counts in dropped.items()},
        "classes": classes,
        "differences": arm_differences(opus, sonnet) if status == "switched" else None,
    }


def active_hours(turns: Sequence[Turn]) -> float:
    ordered = sorted(datetime.fromisoformat(turn.started) for turn in turns)
    seconds = sum((next_at - at).total_seconds() for at, next_at in zip(ordered, ordered[1:]) if (next_at - at).total_seconds() <= 900)
    return seconds / 3600


def ratio(post: Sequence[float | int], other: Sequence[float | int]) -> float | None:
    post_mean = mean(post)
    other_mean = mean(other)
    return post_mean / other_mean if post_mean is not None and other_mean not in (None, 0) else None


def compaction_result(name: str, arm: str, turns: Sequence[Turn], compactions: Sequence[Compaction]) -> CompactionComparison:
    model = OPUS if arm == "opus" else SONNET
    attributed = [compact for compact in compactions if compact.name == name and compact.model == model]
    hours = active_hours(turns)
    post = [turn for turn in turns if 1 <= turn.after_compact <= 10]
    other = [turn for turn in turns if not 1 <= turn.after_compact <= 10]
    post_seconds = [turn.seconds for turn in post if turn.seconds is not None]
    other_seconds = [turn.seconds for turn in other if turn.seconds is not None]
    duration = [compact.duration_ms for compact in attributed if compact.duration_ms is not None]
    return {
        "name": name, "arm": arm, "requests": len(turns), "compactions": len(attributed),
        "active_hours": hours,
        "per_100_requests": 100 * len(attributed) / len(turns) if turns else None,
        "per_active_hour": len(attributed) / hours if hours else None,
        "pre_tokens_median": median([compact.pre_tokens for compact in attributed]),
        "duration_ms_median": median(duration),
        "seconds_per_active_hour": sum(duration) / 1000 / hours if hours and len(duration) == len(attributed) else None,
        "post_requests": len(post), "other_requests": len(other),
        "post_seconds_ratio": ratio(post_seconds, other_seconds),
        "post_write_ratio": ratio(
            [turn.write_5m + turn.write_1h for turn in post],
            [turn.write_5m + turn.write_1h for turn in other],
        ),
    }


def control_results(directors: Sequence[DirectorComparison], arms: dict[str, dict[str, list[Turn]]], first_times: dict[str, datetime]) -> ConcurrentControl:
    switched_times = sorted(first_times[row["name"]].timestamp() for row in directors if row["status"] == "switched")
    if not switched_times:
        return {"at_pdt": None, "directors": []}
    boundary = datetime.fromtimestamp(statistics.median(switched_times), tz=PDT)
    controls: list[ControlDirector] = []
    for row in directors:
        if row["status"] != "control candidate":
            continue
        name = row["name"]
        opus = arms[name]["opus"]
        before_window = [turn for turn in opus if datetime.fromisoformat(turn.started) < boundary][-BASELINE_REQUESTS:]
        before = class_turns(before_window, "continuation")
        after = class_turns([turn for turn in opus if datetime.fromisoformat(turn.started) >= boundary], "continuation")
        if len(before) < 30 or len(after) < 30:
            continue
        before_seconds = [turn.seconds for turn in before if turn.seconds is not None]
        after_seconds = [turn.seconds for turn in after if turn.seconds is not None]
        controls.append({
            "name": name, "before_n": len(before), "after_n": len(after),
            "before_median": median(before_seconds), "after_median": median(after_seconds),
            "difference": difference(before_seconds, after_seconds, "seconds"),
        })
    return {"at_pdt": pdt_time(boundary), "directors": controls}


def formatted(value: object, digits: int = 2) -> str:
    if value is None:
        return "—"
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        return f"{value:.{digits}f}"
    return str(value)


def difference_text(value: DifferenceEstimate | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    if value["value"] is None:
        return "— (too few)"
    return f"{formatted(value['value'], digits)} [{formatted(value['low'], digits)}, {formatted(value['high'], digits)}] {value['label']}"


def drop_line(directors: Sequence[DirectorComparison]) -> str:
    fragments: list[str] = []
    for row in directors:
        drops = row["drops"]
        for arm in ("opus", "sonnet", "other"):
            counts = drops[arm]
            fragments.append(f"{row['name']} {arm} switch:{counts['switch_turn']} effort:{counts['effort']} speed:{counts['speed']}")
    return "Drops — " + "; ".join(fragments)


def markdown(result: ComparisonReport) -> str:
    directors = result["directors"]
    pooled = result["pooled"]
    controls = result["control"]
    compactions = result["compactions"]
    lines = ["# Director model comparison", "", "API-equivalent USD per request; 95% bootstrap intervals are Sonnet minus Opus.", "",
             "| Director | Opus all / baseline n | Sonnet n | Other n | Continuation median s O / S | Δ seconds | Δ output tokens | Δ cost USD |",
             "| --- | ---: | ---: | ---: | ---: | --- | --- | --- |"]
    main_rows = [row for row in directors if row["status"] != "control candidate"]
    if pooled is not None:
        position = sum(row["status"] == "switched" for row in directors)
        main_rows.insert(position, pooled)
    for row in main_rows:
        counts = row["counts"]
        classes = row["classes"]
        differences = row["differences"]
        opus_seconds = classes["opus"]["continuation"]["seconds_median"] if counts["opus_all"] else None
        sonnet_seconds = classes["sonnet"]["continuation"]["seconds_median"] if counts["sonnet"] else None
        lines.append(
            f"| {row['name']} | {counts['opus_all']} / {counts['opus_baseline']} | {counts['sonnet']} | {counts['other']} | {formatted(opus_seconds)} / {formatted(sonnet_seconds)} | "
            + f"{difference_text(differences['seconds']) if differences else '—'} | {difference_text(differences['output']) if differences else '—'} | {difference_text(differences['cost'], 5) if differences else '—'} |"
        )
    lines.extend(["", drop_line(directors), "", "## By turn class", "",
                  "| Director | Arm | Class | n / timed | Seconds median [p25, p75, p90] | Output | Thinking | Fresh input | Cache read | Context | Output/s | Cost USD |",
                  "| --- | --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"])
    for row in main_rows:
        classes = row["classes"]
        counts = row["counts"]
        for arm in ("opus_all", "opus", "sonnet"):
            if counts["sonnet" if arm == "sonnet" else "opus_all"] == 0:
                continue
            if arm == "opus_all" and row["status"] not in ("switched", "pooled"):
                continue
            for class_name in CLASSES:
                stats = classes[arm][class_name]
                lines.append(
                    f"| {row['name']} | {arm} | {class_name} | {stats['n']} / {stats['timed_n']} | "
                    + f"{formatted(stats['seconds_median'])} [{formatted(stats['seconds_p25'])}, {formatted(stats['seconds_p75'])}, {formatted(stats['seconds_p90'])}] | "
                    + f"{formatted(stats['output_median'])} | {formatted(stats['thinking_median'])} | {formatted(stats['fresh_input_median'])} | "
                    + f"{formatted(stats['cache_read_median'])} | {formatted(stats['context_median'])} | {formatted(stats['output_per_second_median'])} | {formatted(stats['cost_mean'], 5)} |"
                )
    lines.extend(["", drop_line(directors), "", "## Concurrent control", "",
                  f"Boundary: {controls['at_pdt'] or '—'}", "",
                  "| Director | Switched Δ seconds | Control before / after n | Control median s before / after | Control Δ seconds |",
                  "| --- | --- | ---: | ---: | --- |"])
    for row in directors:
        if row["status"] == "switched":
            differences = row["differences"]
            assert differences is not None
            lines.append(f"| {row['name']} | {difference_text(differences['seconds'])} | — | — | — |")
    for control in controls["directors"]:
        lines.append(
            f"| {control['name']} | — | {control['before_n']} / {control['after_n']} | "
            + f"{formatted(control['before_median'])} / {formatted(control['after_median'])} | {difference_text(control['difference'])} |"
        )
    lines.extend(["", drop_line(directors), "", "## Compactions", "",
                  "| Director | Arm | Compactions / requests | Per 100 requests | Per active h | Pre tokens median | Duration ms median | Seconds per active h | Post / other n | Post seconds ratio | Post write ratio |",
                  "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"])
    for row in compactions:
        lines.append(
            f"| {row['name']} | {row['arm']} | {row['compactions']} / {row['requests']} | "
            + f"{formatted(row['per_100_requests'])} | {formatted(row['per_active_hour'])} | {formatted(row['pre_tokens_median'])} | "
            + f"{formatted(row['duration_ms_median'])} | {formatted(row['seconds_per_active_hour'])} | "
            + f"{row['post_requests']} / {row['other_requests']} | {formatted(row['post_seconds_ratio'])} | {formatted(row['post_write_ratio'])} |"
        )
    lines.extend(["", drop_line(directors), ""])
    return "\n".join(lines)


def atomic_json(path: Path, result: ComparisonReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as output:
            temporary = output.name
            json.dump(result, output, indent=2)
            _ = output.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def compare(state_dir: Path) -> ComparisonReport:
    """Atomically write comparison data without printing its tables."""
    turns = load_turns(state_dir / "turns.jsonl")
    compactions = load_compactions(state_dir / "compactions.jsonl")
    names = director_names(turns)
    arms, drops, other = eligible_turns(turns)
    directors: list[DirectorComparison] = []
    first_times: dict[str, datetime] = {}
    for name in names:
        opus = arms[name]["opus"]
        sonnet = arms[name]["sonnet"]
        status: DirectorStatus = (
            "switched" if opus and sonnet else "control candidate" if opus else
            "Sonnet-only" if sonnet else "no eligible requests"
        )
        first_sonnet = first_sonnet_time(turns, name)
        if first_sonnet is not None:
            first_times[name] = first_sonnet
        directors.append(director_result(name, status, opus, sonnet, first_sonnet, drops[name], other[name]))
    order = {"switched": 0, "Sonnet-only": 1, "no eligible requests": 2, "control candidate": 3, "pooled": 4}
    directors.sort(key=lambda row: order[row["status"]])
    switched = [row for row in directors if row["status"] == "switched"]
    pooled: DirectorComparison | None = None
    if switched:
        pooled_opus = [turn for row in switched for turn in arms[row["name"]]["opus"]]
        pooled_sonnet = [turn for row in switched for turn in arms[row["name"]]["sonnet"]]
        pooled_baseline = [
            turn for row in switched
            for turn in [eligible for eligible in arms[row["name"]]["opus"]
                         if datetime.fromisoformat(eligible.started) < first_times[row["name"]]][-BASELINE_REQUESTS:]
        ]
        pooled = director_result("Pooled switched", "pooled", pooled_opus, pooled_sonnet, None,
                                 {"opus": DropCounts(), "sonnet": DropCounts(), "other": DropCounts()}, 0)
        pooled["counts"] = {"opus_all": len(pooled_opus), "opus_baseline": len(pooled_baseline), "sonnet": len(pooled_sonnet), "other": sum(other[row["name"]] for row in switched)}
        pooled_classes = pooled["classes"]
        pooled_classes["opus"] = {class_name: class_statistics(pooled_baseline, class_name) for class_name in CLASSES}
        pooled["differences"] = arm_differences(pooled_baseline, pooled_sonnet)
    control = control_results(directors, arms, first_times)
    compaction_rows: list[CompactionComparison] = []
    for row in directors:
        name = row["name"]
        for arm in ("opus", "sonnet"):
            if arms[name][arm]:
                compaction_rows.append(compaction_result(name, arm, arms[name][arm], compactions))
    result: ComparisonReport = {
        "baseline_requests": BASELINE_REQUESTS,
        "prices_usd_per_million": {model: {
            "input": price.input, "output": price.output, "write_5m": price.write_5m,
            "write_1h": price.write_1h, "cache_read": price.cache_read,
        } for model, price in PRICES.items()},
        "directors": directors, "pooled": pooled, "control": control, "compactions": compaction_rows,
    }
    atomic_json(state_dir / "compare.json", result)
    return result
