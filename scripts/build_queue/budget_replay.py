"""Estimate admission waits from bounded lifecycle records and machine samples."""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import TypedDict, cast

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.build_queue import budget
from scripts.build_queue.scope_activity import IdleService
from scripts.lint import memory_admit as gate

MAX_RECORDS = 200_000
MAX_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True)
class ReplayEstimate:
    fixed_wait_seconds: float
    adaptive_wait_seconds: float
    budget_saved_seconds: float
    free_saved_seconds: float
    budget_hold_seconds: float
    free_hold_seconds: float
    pressure_hold_seconds: float
    token_hold_seconds: float
    frames: int

    @property
    def saved_seconds(self) -> float:
        return self.fixed_wait_seconds - self.adaptive_wait_seconds


@dataclass(frozen=True)
class OomEvidence:
    kernel_kills: int
    earlyoom_kills: int
    cgroup_kills: int
    incomplete: tuple[str, ...]


def estimate(frames: Sequence[gate.QueueInputs], config: gate.MemoryConfig) -> ReplayEstimate:
    """Compare checks on the observed schedule; do not claim a new execution schedule."""
    if len(frames) > MAX_RECORDS:
        raise ValueError("too many replay frames")
    if any(right.now <= left.now for left, right in zip(frames, frames[1:])):
        raise ValueError("replay times must increase")
    if not frames:
        return ReplayEstimate(0, 0, 0, 0, 0, 0, 0, 0, 0)
    state = budget.start_budget(config, frames[0].now)
    fixed_wait = adaptive_wait = budget_saved = free_saved = 0.0
    budget_hold = free_hold = pressure_hold = token_hold = 0.0
    for inputs, following in zip(frames, frames[1:]):
        seconds = following.now - inputs.now
        state, limits, decision = budget.advance_budget(state, inputs, config, inputs.now)
        fixed = gate.admission_pass(inputs, inputs.now, replace(limits, budget=decision.floor, free_check="sum"), config.policy)
        budget_only = gate.admission_pass(inputs, inputs.now, replace(limits, free_check="sum"), config.policy)
        adaptive = gate.admission_pass(inputs, inputs.now, limits, config.policy)
        for row, middle, other in zip(fixed.rows, budget_only.rows, adaptive.rows, strict=True):
            if isinstance(row.reason, gate.TokenWaitingWaiter):
                token_hold += seconds
                continue
            fixed_held = not isinstance(row.admission, gate.ProposedAdmission)
            middle_held = not isinstance(middle.admission, gate.ProposedAdmission)
            adaptive_held = not isinstance(other.admission, gate.ProposedAdmission)
            fixed_wait += seconds * fixed_held
            adaptive_wait += seconds * adaptive_held
            budget_saved += seconds * (fixed_held - middle_held)
            free_saved += seconds * (middle_held - adaptive_held)
            # Independent counters can overlap when several checks fail.
            budget_hold += seconds * (not row.decision.budget_passes)
            free_hold += seconds * (not row.decision.free_passes)
            pressure_hold += seconds * isinstance(row.decision.pressure, gate.MemoryPressure)
    return ReplayEstimate(fixed_wait, adaptive_wait, budget_saved, free_saved,
                          budget_hold, free_hold, pressure_hold, token_hold, len(frames))


def bounded_rows(paths: Sequence[Path]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    size = 0
    for path in paths:
        size += path.stat().st_size
        if size > MAX_BYTES:
            raise ValueError("replay inputs exceed 128 MiB")
        with path.open("rb") as handle:
            for line in handle:
                value = cast(object, json.loads(line))
                if not isinstance(value, dict):
                    raise ValueError(f"invalid replay record: {path}")
                rows.append(cast(dict[str, object], value))
                if len(rows) > MAX_RECORDS:
                    raise ValueError("too many replay records")
    return rows


def sample_time(row: dict[str, object]) -> float:
    value = row.get("at")
    if not isinstance(value, str):
        raise ValueError("sample timestamp is unavailable")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def sample_pressure(before: dict[str, object], after: dict[str, object], policy: gate.AdmissionPolicy
                    ) -> gate.Pressure | gate.PressureUnavailable:
    seconds = sample_time(after) - sample_time(before)
    if seconds <= 0 or not before.get("boot_id") or before.get("boot_id") != after.get("boot_id"):
        return gate.PressureUnavailable("sample boot or interval is unavailable")
    rates: list[float] = []
    for key in ("stall_full_us", "stall_some_us"):
        left, right = before.get(key), after.get(key)
        if not isinstance(left, int) or not isinstance(right, int) or right < left:
            return gate.PressureUnavailable("sample pressure counters are unavailable")
        rates.append(min(100.0, 100 * (right - left) / (seconds * 1_000_000)))
    # Samples lack swap capacity. Treat any recorded swap use as full capacity.
    swap = after.get("swap_used_bytes")
    if not isinstance(swap, int):
        return gate.PressureUnavailable("sample swap use is unavailable")
    return gate.pressure_from_readings(rates[0], rates[1], 100.0 if swap > 0 else 0.0, policy)


def sample_slice(sample: dict[str, object], name: str) -> dict[str, object]:
    slices = sample.get("slices")
    if isinstance(slices, dict):
        item = cast(dict[str, object], slices).get(name)
        if isinstance(item, dict):
            return cast(dict[str, object], item)
    return {}


def recorded_ci_memory(sample: dict[str, object]) -> tuple[gate.RunningStep, ...]:
    """Charge recorded resident CI memory without inventing a worker reservation."""
    ci = sample_slice(sample, "ci")
    if ci.get("state") == "absent":
        return ()
    current, stat = ci.get("current_bytes"), ci.get("stat")
    amount = sample.get("ci_anon_bytes")
    if isinstance(current, int) and isinstance(stat, dict):
        inactive = cast(dict[str, object], stat).get("inactive_file", 0)
        if isinstance(inactive, int):
            amount = max(0, current - inactive)
    if not isinstance(amount, int):
        if ci.get("state") == "present":
            raise ValueError("sample CI resident use is unavailable")
        return ()
    memory = gate.UnownedSliceMemory(max(0, amount), owner="CI", job_state=IdleService())
    return (gate.RunningStep("", "", "CI", "", 0, memory),)


def replay_frames(lifetimes: Sequence[gate.QueueStepLifetime], samples: Sequence[dict[str, object]],
                  total: int, config: gate.MemoryConfig) -> list[gate.QueueInputs]:
    """Carry sample readings across lifecycle boundaries within the sampled window."""
    if not samples:
        return []
    sample_times = [sample_time(sample) for sample in samples]
    if any(right <= left for left, right in zip(sample_times, sample_times[1:])):
        raise ValueError("sample times must increase")
    times = set(sample_times)
    if len(times) > MAX_RECORDS:
        raise ValueError("too many replay frames")
    for step in lifetimes:
        boundaries = [step.since]
        for interval in step.intervals:
            boundaries.append(interval.admitted_at)
            if isinstance(interval.end, gate.ReservationEnded):
                boundaries.append(interval.end.ended_at)
        for gap in step.token_gaps:
            boundaries.extend((gap.token_left_at, gap.token_back_at))
        if isinstance(step.end, (gate.ReservationEnded, gate.LeftQueue)):
            boundaries.append(step.end.ended_at)
        times.update(at for at in boundaries if sample_times[0] <= at <= sample_times[-1])
        if len(times) > MAX_RECORDS:
            raise ValueError("too many replay frames")
    frames: list[gate.QueueInputs] = []
    for now in sorted(times):
        index = bisect_right(sample_times, now) - 1
        sample = samples[index]
        used = sample.get("mem_used_bytes")
        if not isinstance(used, int):
            raise ValueError("sample resident use is unavailable")
        raw_pressure = (sample_pressure(samples[index - 1], sample, config.policy) if index else
                        gate.PressureUnavailable("no earlier sample"))
        pressure = gate.NO_PRESSURE_HOLD if isinstance(raw_pressure, gate.PressureUnavailable) else raw_pressure
        ordered: list[tuple[Path, gate.Waiter]] = []
        running: list[gate.RunningStep] = []
        for step in lifetimes:
            if step.since > now or not step.intervals:
                continue
            end = step.end
            if isinstance(end, (gate.ReservationEnded, gate.LeftQueue)) and end.ended_at <= now:
                continue
            active = next((interval for interval in step.intervals if interval.admitted_at <= now
                           and (isinstance(interval.end, gate.StepStillOpen) or interval.end.ended_at > now)), None)
            if active is not None:
                memory: gate.StepMemory = gate.PromisedMemory(active.need)
                if isinstance(active.end, gate.ReservationEnded) and isinstance(active.end.peak, gate.PeakSampled):
                    peak = active.end.peak.bytes
                    memory = gate.MeasuredMemory(peak, promise=gate.LifetimePromise(active.need), observed_peak=peak)
                running.append(gate.RunningStep("", "", step.step, "", active.need, memory))
                continue
            upcoming = next((interval for interval in step.intervals if interval.admitted_at > now), None)
            if upcoming is None:
                continue
            row: gate.Waiter = {"pid": step.pid, "start_time": step.start_time, "since": step.since,
                                "need": upcoming.need, "step": step.step}
            if any(gap.token_left_at <= now < gap.token_back_at for gap in step.token_gaps):
                row["token_left_at"] = now
            ordered.append((Path(f"{step.pid}-{step.start_time}"), row))
        running.extend(recorded_ci_memory(sample))
        frames.append(gate.QueueInputs(ordered, tuple(running), (), gate.HostMemory(max(0, total - used), total),
            pressure, gate.NoRecoverySpacing(), gate.NoHeadAdmissionOverride(), now, pressure_reading=raw_pressure))
    return frames


class CgroupOomCounter(TypedDict):
    identity: int
    kills: int


def cgroup_oom_counter(sample: dict[str, object], name: str) -> CgroupOomCounter | gate.ReadingUnavailable:
    item = sample_slice(sample, name)
    identity, events = item.get("identity"), item.get("events")
    if isinstance(identity, int) and isinstance(events, dict):
        count = cast(dict[str, object], events).get("oom_kill")
        if isinstance(count, int) and count >= 0:
            return {"identity": identity, "kills": count}
    return gate.ReadingUnavailable("cgroup identity or oom_kill unavailable")


def oom_evidence(samples: Sequence[dict[str, object]], kernel: Iterable[str], earlyoom: Iterable[str],
                 *, ci_slice: str, journals_complete: bool) -> OomEvidence:
    """Keep missing endpoints and counter resets distinct from observed zero kills."""
    missing: list[str] = [] if journals_complete else ["journal coverage unavailable"]
    kernel_kills = sum(bool(re.search(r"(?:Killed process|oom-kill:)", line))
                       and any(re.search(rf"/{re.escape(name)}(?:/|[,\s]|$)", line)
                               for name in ("builds.slice", ci_slice)) for line in kernel)
    early_kills = 0
    for line in earlyoom:
        if not re.search(r"sending SIG(?:TERM|KILL) to process", line):
            continue
        if any(re.search(rf"/{re.escape(name)}(?:/|[,\s]|$)", line) for name in ("builds.slice", ci_slice)):
            early_kills += 1
        elif not re.search(r"/[^\s]+\.slice(?:/|[,\s]|$)", line):
            missing.append("earlyoom cgroup attribution unavailable")
    counters = 0
    if len(samples) < 2:
        missing.append("cgroup counter endpoints unavailable")
    for left, right in zip(samples, samples[1:]):
        for name in ("builds", "ci"):
            before, after = cgroup_oom_counter(left, name), cgroup_oom_counter(right, name)
            if isinstance(before, gate.ReadingUnavailable) or isinstance(after, gate.ReadingUnavailable):
                missing.append(f"{name} cgroup identity changed or unavailable")
            elif before["identity"] != after["identity"]:
                missing.append(f"{name} cgroup identity changed or unavailable")
            elif (after["kills"] >= before["kills"] and left.get("boot_id")
                  and left.get("boot_id") == right.get("boot_id")):
                counters += after["kills"] - before["kills"]
            else:
                missing.append(f"{name} counters unavailable or reset")
    return OomEvidence(kernel_kills, early_kills, counters, tuple(dict.fromkeys(missing)))


def journal(arguments: list[str], start: float, end: float) -> tuple[list[str], bool]:
    try:
        result = subprocess.run(["journalctl", *arguments, "--since", f"@{start}", "--until", f"@{end}",
                                 "--no-pager", "-o", "cat"], capture_output=True, text=True, timeout=30, check=False)
        return result.stdout.splitlines(), result.returncode == 0 and bool(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return [], False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--admission", type=Path, required=True)
    _ = parser.add_argument("--samples", type=Path, nargs="+", required=True)
    _ = parser.add_argument("--start", type=float, required=True, help="UTC epoch seconds")
    _ = parser.add_argument("--end", type=float, required=True, help="UTC epoch seconds")
    _ = parser.add_argument("--ram-gib", type=float, required=True, help="historical RAM capacity")
    args = parser.parse_args()
    start, end = cast(float, args.start), cast(float, args.end)
    if not 0 < end - start <= 2 * 86400:
        parser.error("choose a positive window of at most two days")
    directory = cast(Path, args.admission)
    lifecycle = directory / "queue_times.jsonl"
    if lifecycle.stat().st_size > 8 * 1024 * 1024:
        parser.error("lifecycle input exceeds its retained size bound")
    total = cast(float, args.ram_gib)
    if not 0 < total < float("inf"):
        parser.error("historical RAM capacity must be finite and positive")
    config = gate.read_memory_config()
    lifetimes = gate.read_queue_lifetimes(directory)
    samples = sorted((row for row in bounded_rows(cast(list[Path], args.samples)) if start <= sample_time(row) <= end),
                     key=sample_time)
    frames = replay_frames(lifetimes, samples, int(total * gate.GIB), config)
    report = estimate(frames, config)
    kernel, kernel_ok = journal(["-k"], start, end)
    early, early_ok = journal(["-u", "earlyoom"], start, end)
    coverage: list[str] = []
    if not samples or sample_time(samples[0]) > start or sample_time(samples[-1]) < end:
        coverage.append("sample endpoints do not cover the requested window")
    if not lifetimes:
        coverage.append("no retained lifecycle records")
    unfinished = sum(isinstance(step.end, gate.StepStillOpen) for step in lifetimes if step.since < end)
    unmatched = sum(not step.intervals for step in lifetimes if step.since < end)
    coverage.append("lifecycle retention cannot prove complete arrivals or queue-priority history")
    evidence = oom_evidence(samples, kernel, early, ci_slice=config.ci_memory_slice,
                            journals_complete=kernel_ok and early_ok)
    if coverage:
        evidence = replace(evidence, incomplete=(*evidence.incomplete, *coverage))
    print("Estimate over the observed schedule; shifted memory peaks and kernel outcomes are not predicted.")
    print("Pressure uses sample interval averages, not historical avg10. Any swap use assumes full swap capacity.")
    print("Lifecycle truncation, unmatched steps and unfinished waits can reduce coverage.")
    print(json.dumps({"start": start, "end": end, "inputs": [str(lifecycle), *map(str, cast(list[Path], args.samples))],
                      "coverage": coverage, "unfinished_steps": unfinished, "unmatched_steps": unmatched,
                      "estimate": gate.snapshot_value(report), "oom": gate.snapshot_value(evidence)}, indent=2))


if __name__ == "__main__":
    main()
