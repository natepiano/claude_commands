#!/usr/bin/env python3
"""Report the pre-registered nextest-width switchback trial."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean, median
from typing import Literal, NamedTuple, TypedDict, cast
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lint"))

import store
from memory_admit import expected_peak, test_threads

BLOCK_S = 6000
WASHOUT_S = 900
WIDTHS = {16: "physical", 32: "logical"}
GIB = 1 << 30
PACIFIC = ZoneInfo("America/Los_Angeles")
BOOTSTRAP_SEED = 20261009
MIN_RUNS = 60
MIN_BLOCKS = 15


class Step(TypedDict):
    request: float
    ended: float
    block: int
    width: int | None
    whole_hana: bool
    hana: bool
    wait: float
    wait_recorded: bool
    exec_s: float | None
    failed: bool
    call_id: str | None
    step_id: str


class AnonPeakRecord(TypedDict, total=False):
    host: str
    step: str
    call_id: str | None
    anon_peak_bytes: int
    ended_at: str


class AnonPeak(NamedTuple):
    ended: float
    bytes: int


class Interval(NamedTuple):
    low: float
    high: float


@dataclass(frozen=True)
class ComparedMetric:
    physical: float
    logical: float
    comparison: float
    interval: Interval


UncomparedReason = Literal["needs both arms", "zero denominator", "no resamples"]


@dataclass(frozen=True)
class UncomparedMetric:
    physical: float | None
    logical: float | None
    reason: UncomparedReason


Metric = ComparedMetric | UncomparedMetric


def _epoch(stamp: object) -> float:
    if not isinstance(stamp, str):
        raise ValueError("timestamp is not text")
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("timestamp has no UTC offset")
    return parsed.timestamp()


def _argv(value: object) -> list[str]:
    if not isinstance(value, str):
        return []
    try:
        decoded = cast(object, json.loads(value))
    except (TypeError, ValueError):
        return []
    if not isinstance(decoded, list):
        return []
    arguments: list[str] = []
    for item in cast(list[object], decoded):
        if not isinstance(item, str):
            return []
        arguments.append(item)
    return arguments


def _filter_expression(argv: list[str]) -> str | None:
    for index, argument in enumerate(argv[:-1]):
        if argument == "-E":
            return argv[index + 1]
    return None


def _whole_hana(argv: list[str], repo: object, caller: object) -> bool:
    has_test_target = any(argument == "--test" or argument.startswith("--test=") for argument in argv)
    return caller == "verify" and repo == "hana" and _filter_expression(argv) == "package(hana)" and not has_test_target


def load_steps(index: Path, since: float, until: float, host: str) -> list[Step]:
    """Load nextest steps selected by their pre-admission request time."""
    connection = sqlite3.connect(f"file:{index}?mode=ro", uri=True)
    try:
        rows = cast(list[tuple[object, ...]], connection.execute(
            """SELECT id, started_at, ended_at, duration_s, mem_wait_s, argv, repo, caller,
                      status, finished_s, call_id
                 FROM steps WHERE host = ? AND step = 'nextest'
                 ORDER BY ended_at, id""",
            (host,),
        ).fetchall())
    finally:
        connection.close()

    steps: list[Step] = []
    for row in rows:
        (step_id, started_at, ended_at, duration_s, mem_wait_s, raw_argv, repo, caller,
         status, finished_s, call_id) = row
        wait_recorded = isinstance(mem_wait_s, (int, float))
        wait = float(mem_wait_s) if wait_recorded else 0.0
        request = _epoch(started_at) - wait
        if not since <= request < until:
            continue
        argv = _argv(raw_argv)
        duration = float(duration_s) if isinstance(duration_s, (int, float)) else None
        finished = float(finished_s) if isinstance(finished_s, (int, float)) else None
        steps.append({
            "request": request,
            "ended": _epoch(ended_at),
            "block": int(request // BLOCK_S),
            "width": test_threads(argv),
            "whole_hana": _whole_hana(argv, repo, caller),
            "hana": repo == "hana",
            "wait": wait,
            "wait_recorded": wait_recorded,
            "exec_s": duration - finished if duration is not None and finished is not None else None,
            "failed": status != 0,
            "call_id": call_id if isinstance(call_id, str) and call_id else None,
            "step_id": str(step_id),
        })
    return steps


def anon_by_call(path: Path, host: str) -> dict[str, list[AnonPeak]]:
    """Return ordered nextest anon peaks grouped by build-log call."""
    peaks: dict[str, list[AnonPeak]] = defaultdict(list)
    try:
        with path.open() as lines:
            for line in lines:
                try:
                    decoded = cast(object, json.loads(line))
                    if not isinstance(decoded, dict):
                        continue
                    item = cast(AnonPeakRecord, cast(object, decoded))
                    call_id = item.get("call_id")
                    peak = item.get("anon_peak_bytes")
                    ended_at = item.get("ended_at")
                    if (item.get("host") == host and item.get("step") == "nextest"
                            and isinstance(call_id, str) and call_id
                            and isinstance(peak, int) and not isinstance(peak, bool) and peak > 0
                            and isinstance(ended_at, str)):
                        peaks[call_id].append(AnonPeak(_epoch(ended_at), peak))
                except (AttributeError, TypeError, ValueError):
                    continue
    except OSError:
        pass
    return {call_id: sorted(records) for call_id, records in peaks.items()}


def anon_by_step(steps: list[Step], peaks_by_call: Mapping[str, list[AnonPeak]]) -> dict[str, int]:
    """Pair attempts and anon peaks only where a call has an unambiguous count."""
    steps_by_call: dict[str, list[Step]] = defaultdict(list)
    for step in steps:
        call_id = step["call_id"]
        if call_id is not None:
            steps_by_call[call_id].append(step)

    peaks_by_step: dict[str, int] = {}
    for call_id, call_steps in steps_by_call.items():
        call_peaks = peaks_by_call.get(call_id, [])
        if len(call_steps) != len(call_peaks):
            continue
        ordered_steps = sorted(call_steps, key=lambda step: (step["ended"], step["step_id"]))
        for step, peak in zip(ordered_steps, call_peaks, strict=True):
            peaks_by_step[step["step_id"]] = peak.bytes
    return peaks_by_step


def _resample_blocks(steps: list[Step], rng: random.Random) -> list[Step]:
    grouped: dict[int, list[Step]] = defaultdict(list)
    for step in steps:
        grouped[step["block"]].append(step)
    blocks = list(grouped.values())
    if not blocks:
        return []
    return [step for _ in blocks for step in rng.choice(blocks)]


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = int(position)
    fraction = position - lower
    return ordered[lower] + (ordered[lower + 1] - ordered[lower]) * fraction


def block_bootstrap(arms: tuple[list[Step], list[Step]], stat: Callable[[list[Step]], float | None],
                    combine: Callable[[float, float], float], rng: random.Random, n: int = 2000) -> Interval:
    """Return the central 90% interval from arm-wise block resamples."""
    samples: list[float] = []
    for _ in range(n):
        physical = stat(_resample_blocks(arms[0], rng))
        logical = stat(_resample_blocks(arms[1], rng))
        if physical is not None and logical is not None:
            try:
                samples.append(combine(physical, logical))
            except ZeroDivisionError:
                continue
    if not samples:
        return Interval(float("nan"), float("nan"))
    return Interval(_quantile(samples, 0.05), _quantile(samples, 0.95))


def _w_result(metric: Metric) -> Literal["proven", "partial", "none"] | None:
    if isinstance(metric, UncomparedMetric):
        return None
    if metric.interval.high >= 1.0:
        return "none"
    return "proven" if metric.comparison <= 0.50 else "partial"


def _t_failed(metric: Metric) -> bool | None:
    if isinstance(metric, UncomparedMetric):
        return None
    return metric.comparison > 1.11 and metric.interval.low > 1.00


def _f_failed(metric: Metric) -> bool | None:
    if isinstance(metric, UncomparedMetric):
        return None
    return metric.comparison > 5.0 and metric.interval.low > 0.0


def verdict(w: Metric, t: Metric, f: Metric) -> Literal["keep", "revert", "trade-off", "no verdict"]:
    w_result = _w_result(w)
    t_failed = _t_failed(t)
    f_failed = _f_failed(f)
    if w_result is None or t_failed is None or f_failed is None:
        return "no verdict"
    if w_result == "none":
        return "revert"
    if t_failed or f_failed:
        return "trade-off"
    return "keep"


def _mean(steps: list[Step], select: Callable[[Step], bool], value: Callable[[Step], float]) -> float | None:
    values = [value(step) for step in steps if select(step)]
    return fmean(values) if values else None


def _median(steps: list[Step], select: Callable[[Step], bool], value: Callable[[Step], float]) -> float | None:
    values = [value(step) for step in steps if select(step)]
    return float(median(values)) if values else None


def _ratio(physical: float, logical: float) -> float:
    return physical / logical


def _difference(physical: float, logical: float) -> float:
    return physical - logical


def compare_metric(arms: tuple[list[Step], list[Step]], stat: Callable[[list[Step]], float | None],
                   combine: Callable[[float, float], float], rng: random.Random) -> Metric:
    physical = stat(arms[0])
    logical = stat(arms[1])
    if physical is None or logical is None:
        return UncomparedMetric(physical, logical, "needs both arms")
    try:
        comparison = combine(physical, logical)
    except ZeroDivisionError:
        return UncomparedMetric(physical, logical, "zero denominator")
    interval = block_bootstrap(arms, stat, combine, rng)
    if interval.low != interval.low or interval.high != interval.high:
        return UncomparedMetric(physical, logical, "no resamples")
    return ComparedMetric(physical, logical, comparison, interval)


def _arm_label(width: int | None) -> str:
    return "unset" if width is None else WIDTHS.get(width, str(width))


def _arm_order(width: int | None) -> tuple[int, int]:
    if width == 16:
        return (0, 0)
    if width == 32:
        return (1, 0)
    if width is None:
        return (2, 0)
    return (3, width)


def _value_by_arm(arms: Mapping[int | None, list[Step]], stat: Callable[[list[Step]], float | None]) -> dict[int | None, float | None]:
    return {width: stat(steps) for width, steps in arms.items()}


def _test_timeouts(index: Path, step_ids: set[str]) -> int:
    if not step_ids:
        return 0
    connection = sqlite3.connect(f"file:{index}?mode=ro", uri=True)
    try:
        placeholders = ",".join("?" for _ in step_ids)
        row = cast(tuple[object], connection.execute(
            f"SELECT count(*) FROM tests WHERE status = 'timed_out' AND step_id IN ({placeholders})",
            tuple(step_ids),
        ).fetchone())
        return row[0] if isinstance(row[0], int) else 0
    except sqlite3.Error:
        return 0
    finally:
        connection.close()


def _uses_memory_gate(step_name: object, raw_argv: object) -> bool:
    argv = _argv(raw_argv)
    if step_name == "sweep" and any(argument.endswith("/sweep.py") for argument in argv):
        return False
    if not argv or argv[0] != "cargo":
        return True
    subcommand = 1
    if len(argv) > subcommand and argv[subcommand].startswith("+"):
        subcommand += 1
    return len(argv) <= subcommand or step_name != "fmt" or argv[subcommand] != "fmt"


def _all_waits(index: Path, since: float, until: float, host: str) -> list[tuple[int, float]]:
    connection = sqlite3.connect(f"file:{index}?mode=ro", uri=True)
    try:
        rows = cast(list[tuple[object, object, object, object]], connection.execute(
            "SELECT started_at, mem_wait_s, step, argv FROM steps WHERE host = ?",
            (host,),
        ).fetchall())
    finally:
        connection.close()
    waits: list[tuple[int, float]] = []
    for started_at, mem_wait_s, step_name, raw_argv in rows:
        if not isinstance(mem_wait_s, (int, float)) or not _uses_memory_gate(step_name, raw_argv):
            continue
        wait = float(mem_wait_s)
        request = _epoch(started_at) - wait
        if since <= request < until:
            waits.append((int(request // BLOCK_S), wait))
    return waits


def _block_arms(steps: list[Step]) -> dict[int, int | None]:
    widths: dict[int, list[int | None]] = defaultdict(list)
    for step in steps:
        widths[step["block"]].append(step["width"])
    return {block: Counter(values).most_common(1)[0][0] for block, values in widths.items()}


def _all_wait_values(index: Path, since: float, until: float, host: str, steps: list[Step],
                     widths: list[int | None]) -> dict[int | None, float | None]:
    by_arm: dict[int | None, list[float]] = {width: [] for width in widths}
    block_arms = _block_arms(steps)
    for block, wait in _all_waits(index, since, until, host):
        width = block_arms.get(block)
        if width in by_arm:
            by_arm[width].append(wait)
    return {width: fmean(values) if values else None for width, values in by_arm.items()}


def _fmt_value(value: float | None, decimals: int) -> str:
    return "—" if value is None else f"{value:.{decimals}f}"


def _fmt_interval(interval: Interval | None, decimals: int, suffix: str = "") -> str:
    if interval is None:
        return "—"
    return f"{interval.low:.{decimals}f}–{interval.high:.{decimals}f}{suffix}"


def _comparison(metric: Metric) -> float | None:
    return metric.comparison if isinstance(metric, ComparedMetric) else None


def _comparison_interval(metric: Metric) -> Interval | None:
    return metric.interval if isinstance(metric, ComparedMetric) else None


def _metric_row(name: str, widths: list[int | None], values: Mapping[int | None, float | None],
                value_decimals: int, comparison: str, interval: str, rule: str, result: str) -> str:
    cells = [f"{name:<43}"]
    cells.extend(f"{_fmt_value(values.get(width), value_decimals):>10}" for width in widths)
    cells.extend((f"{comparison:>9}", f"{interval:>15}", f"{rule:<29}", result))
    return " ".join(cells).rstrip()


def _detail_row(name: str, widths: list[int | None], values: Mapping[int | None, float | None], decimals: int) -> str:
    cells = [f"{name:<43}"]
    cells.extend(f"{_fmt_value(values.get(width), decimals):>10}" for width in widths)
    return " ".join(cells).rstrip()


def _script_hash() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]


def _clock(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, PACIFIC).strftime("%Y-%m-%d %H:%M %Z")


def report(since: float, until: float, host: str = "natedev", root: Path | None = None) -> str:
    root = root if root is not None else store.root()
    index = root / store.INDEX_NAME
    steps = load_steps(index, since, until, host)
    arms: dict[int | None, list[Step]] = defaultdict(list)
    for step in steps:
        arms[step["width"]].append(step)
    widths = sorted(arms, key=_arm_order)

    physical_logical = (arms.get(16, []), arms.get(32, []))
    anon = anon_by_step(steps, anon_by_call(root / "admission/anon_peaks.jsonl", host))

    def w_stat(rows: list[Step]) -> float | None:
        return _mean(rows, lambda step: (step["hana"] and step["wait_recorded"]
                                         and step["request"] % BLOCK_S >= WASHOUT_S),
                     lambda step: step["wait"])

    def p_stat(rows: list[Step]) -> float | None:
        return _median(rows, lambda step: step["whole_hana"] and step["step_id"] in anon,
                       lambda step: float(anon[step["step_id"]]))

    def t_stat(rows: list[Step]) -> float | None:
        return _median(rows, lambda step: step["whole_hana"] and step["exec_s"] is not None,
                       lambda step: cast(float, step["exec_s"]))

    def f_stat(rows: list[Step]) -> float | None:
        return _mean(rows, lambda step: step["whole_hana"],
                     lambda step: 100.0 if step["failed"] else 0.0)

    rng = random.Random(BOOTSTRAP_SEED)
    w_metric = compare_metric(physical_logical, w_stat, _ratio, rng)
    p_metric = compare_metric(physical_logical, p_stat, _ratio, rng)
    t_metric = compare_metric(physical_logical, t_stat, _ratio, rng)
    f_metric = compare_metric(physical_logical, f_stat, _difference, rng)

    w_values = _value_by_arm(arms, w_stat)
    p_values = {width: (value / GIB if value is not None else None)
                for width, value in _value_by_arm(arms, p_stat).items()}
    t_values = _value_by_arm(arms, t_stat)
    f_values = _value_by_arm(arms, f_stat)
    r_values: dict[int | None, float | None] = {
        width: expected_peak("hana", "nextest", host, datetime.fromtimestamp(until, UTC), root, width).bytes / GIB
        for width in widths
    }

    timed_out: dict[int | None, float | None] = {}
    for width in widths:
        whole = [step for step in arms[width] if step["whole_hana"]]
        count = _test_timeouts(index, {step["step_id"] for step in whole})
        timed_out[width] = count * 100.0 / len(whole) if whole else None
    all_wait = _all_wait_values(index, since, until, host, steps, widths)

    duration_h = (until - since) / 3600
    lines = [f"Width trial {host} {_clock(since)} – {_clock(until)} ({duration_h:.1f} h) · width_trial.py sha256 {_script_hash()}"]
    lines.append(f"{'arm':<10} {'blocks':>6} {'hana steps':>11} {'whole-hana runs':>16}")
    for width in widths:
        arm = arms[width]
        blocks = len({step["block"] for step in arm})
        hana_steps = sum(step["hana"] for step in arm)
        whole_hana_runs = sum(step["whole_hana"] for step in arm)
        lines.append(f"{_arm_label(width):<10} {blocks:>6,} {hana_steps:>11,} {whole_hana_runs:>16,}")

    shortage: str | None = None
    sample_widths = [width for width in WIDTHS if width in arms] or widths
    for width in sample_widths:
        whole_count = sum(step["whole_hana"] for step in arms[width])
        block_count = len({step["block"] for step in arms[width]})
        if whole_count < MIN_RUNS:
            shortage = f"{_arm_label(width)} has {whole_count} whole-hana runs (< {MIN_RUNS})"
            break
        if block_count < MIN_BLOCKS:
            shortage = f"{_arm_label(width)} has {block_count} blocks (< {MIN_BLOCKS})"
            break
    lines.append("sample: ok" if shortage is None else f"sample: short — {shortage}")

    heading = f"{'metric':<43}" + "".join(f" {_arm_label(width):>10}" for width in widths)
    lines.append(heading + f" {'ratio':>9} {'90% interval':>15} {'rule':<29} result")
    w_result = _w_result(w_metric)
    lines.append(_metric_row(
        "W memory wait per hana test step (s)", widths, w_values, 1,
        _fmt_value(_comparison(w_metric), 2), _fmt_interval(_comparison_interval(w_metric), 2),
        "≤ 0.50, high < 1.00", w_result or "—",
    ))
    p_result = "—" if p_metric.physical is None else ("met" if p_metric.physical / GIB <= 6.50 else "unmet")
    lines.append(_metric_row(
        "P whole-hana anon peak p50 (GiB)", widths, p_values, 2,
        _fmt_value(_comparison(p_metric), 2), _fmt_interval(_comparison_interval(p_metric), 2),
        "physical ≤ 6.50", p_result,
    ))
    r_physical = r_values.get(16)
    r_result = "—" if r_physical is None else ("met" if r_physical <= 9.00 else "unmet")
    lines.append(_metric_row(
        "R gate reservation, hana nextest (GiB)", widths, r_values, 2, "—", "—",
        "physical ≤ 9.00", r_result,
    ))
    t_failed = _t_failed(t_metric)
    lines.append(_metric_row(
        "T whole-hana exec p50 (s)", widths, t_values, 0,
        _fmt_value(_comparison(t_metric), 2), _fmt_interval(_comparison_interval(t_metric), 2),
        "fails > 1.11 and low > 1", "—" if t_failed is None else ("fail" if t_failed else "pass"),
    ))
    f_failed = _f_failed(f_metric)
    f_difference = _comparison(f_metric)
    f_comparison = "—" if f_difference is None else f"{f_difference:+.1f}pt".replace("-", "−")
    lines.append(_metric_row(
        "F whole-hana failed runs (%)", widths, f_values, 1, f_comparison,
        _fmt_interval(_comparison_interval(f_metric), 1, "pt").replace("-", "−"),
        "fails > +5pt and low > 0", "—" if f_failed is None else ("fail" if f_failed else "pass"),
    ))
    lines.append(_detail_row("  timed-out tests per 100 whole-hana runs", widths, timed_out, 1))
    lines.append(_detail_row("  memory wait per gated step, all natedev", widths, all_wait, 1))

    decision = verdict(w_metric, t_metric, f_metric)
    if decision == "keep":
        lines.append(f"verdict: keep — W {w_result}; T and F pass")
    elif decision == "revert":
        lines.append("verdict: revert — W none")
    elif decision == "trade-off":
        failed = " and ".join(name for name, state in (("T", t_failed), ("F", f_failed)) if state)
        lines.append(f"verdict: trade-off — W {w_result}; {failed} fails")
    elif 16 not in arms or 32 not in arms:
        lines.append("verdict: no verdict — needs both arms")
    else:
        lines.append("verdict: no verdict — metrics unavailable")
    return "\n".join(lines)


def _window(value: str) -> float:
    if value == "now":
        return datetime.now(UTC).timestamp()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be ISO-8601 with an offset, or now") from error
    if parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("must include a UTC offset")
    return parsed.timestamp()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    report_parser = commands.add_parser("report", help="print the frozen width-trial scorecard")
    _ = report_parser.add_argument("--since", required=True, type=_window)
    _ = report_parser.add_argument("--until", required=True, type=_window)
    arguments = parser.parse_args(argv)
    since = cast(float, arguments.since)
    until = cast(float, arguments.until)
    if until <= since:
        parser.error("--until must be later than --since")
    print(report(since, until))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
