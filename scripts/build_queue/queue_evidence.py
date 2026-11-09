"""Format recorded queue evidence without reading admission configuration."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from scripts.build_queue import head_facts, queue_lines
from scripts.build_queue.queue_lines import age, field
from scripts.build_queue.scope_activity import IdleService, LiveBuildRun, NotServiceSlice, RunningServiceJob
from scripts.lib.build_steps import StepNames
from scripts.build_queue import admission_records
from scripts.build_queue import step_estimator
from scripts.lint import memory_admit as gate
from scripts.message import unit_labels


def step_labels(step: admission_records.WaitingStep | admission_records.RunningStep,
                labels: Mapping[str, unit_labels.UnitLabels]) -> unit_labels.UnitLabels:
    identity = labels.get(step.session_id)
    unit = gate.queue_unit(step.unit, step.session_id, labels)
    if identity is not None:
        return replace(identity, unit=unit) if step.unit else identity
    fallback = " ".join(step.session_name.split()) or step.folder.rstrip("/").rpartition("/")[2] or "unknown"
    return unit_labels.UnitLabels(fallback, unit=unit or "—")


def step_pid_text(step: admission_records.WaitingStep | admission_records.RunningStep) -> str:
    """Format the captured run pid or the captured process pid."""
    return pid_text(step.run.pid if isinstance(step.run, LiveBuildRun) else step.pid)


def pid_text(pid: int | admission_records.PidNotRecorded) -> str:
    return f"pid {pid}" if isinstance(pid, int) else "pid —"


def gib(value: int) -> str:
    return f"{value / admission_records.GIB:.1f} GiB"


def ci_amount(value: int) -> str:
    """Show CI use under 0.1 GiB in MiB, so idle CI never reads 0.0 GiB."""
    return f"{value * 1024 / admission_records.GIB:.0f} MiB" if value < admission_records.GIB // 10 else gib(value)


def need_text(source: admission_records.RecordedNeed | admission_records.NeedSourceNotRecorded, *,
              zone: ZoneInfo) -> str:
    if isinstance(source, admission_records.NeedSourceNotRecorded):
        return "source not recorded"
    basis = source.basis
    if isinstance(basis, (admission_records.ShapeP90, admission_records.ShapeLargest, admission_records.ShapeFallback)):
        if isinstance(basis, admission_records.ShapeP90):
            text = f"same-shape p90 of {basis.runs} runs"
        elif isinstance(basis, admission_records.ShapeLargest):
            text = f"same-shape largest peak of {basis.runs} runs"
        else:
            text = "same-shape fallback: no runs; heavier of fallback and same-profile builds"
        text += f", last {source.history_days} days"
        if isinstance(basis, admission_records.ShapeFallback):
            text += "; no headroom factor"
        else:
            if isinstance(basis.floor, admission_records.FailedPeakFloor):
                text += (f"; failed-build floor {gib(basis.floor.peak)} from "
                         + head_facts.clock(datetime.fromisoformat(basis.floor.ended_at).timestamp(), zone, seconds=False))
            else:
                text += "; no failed-build floor"
            if isinstance(basis.factor, admission_records.ReleaseHeadroom):
                text += f"; release headroom × {basis.factor.factor:.3f}"
            elif isinstance(basis.factor, admission_records.DevSizeHeadroom):
                text += (f"; dev size headroom × {basis.factor.factor:.3f}"
                         + f" + margin {gib(basis.factor.margin_bytes)}")
            elif isinstance(basis.factor, admission_records.LegacyDevMultiplierHeadroom):
                text += f"; dev promise factor × {basis.factor.factor:.3f}"
            else:
                text += "; no headroom factor"
        if isinstance(basis.cap, admission_records.CappedAtBudget):
            limit = ("an unrecorded amount" if isinstance(basis.cap.cap, admission_records.CapNotRecorded)
                     else gib(basis.cap.cap))
            text += f"; limited to {limit} at that time (uncut {gib(basis.cap.uncapped)})"
        else:
            text += "; uncut estimate"
        return text
    if source.source == "measured":
        text = f"measured process-memory p90 of {source.runs} runs"
    elif source.source == "buildlog":
        text = f"build-log peak × {step_estimator.ANON_SHARE:.2f} of {source.runs} runs"
    elif source.source == "pooled":
        text = f"pooled over all repos, {source.runs} runs"
    elif source.source == "fallback":
        text = "fallback: fewer than 5 runs"
    elif source.source == "shape":
        text = "same-shape promise: basis not recorded"
    else:
        text = f"{source.source}: basis not recorded"
    text += f", last {source.history_days} days"
    if source.scope == "filtered":
        text += ", filtered tests"
    else:
        text += ", full tests" if source.package else ", full scope"
    return text


def room_text(room: admission_records.MemoryRoom | admission_records.ReadingUnavailable) -> str:
    """Show the calculation from the last admission pass."""
    if isinstance(room, admission_records.ReadingUnavailable):
        return "Cap unavailable: " + room.reason
    if isinstance(room.cap, int):
        ci_use = f" + CI use {ci_amount(room.ci_use)}" if room.ci_use else ""
        calculation = f"Cap {gib(room.cap)}; anonymous use {gib(room.used)}{ci_use} + free {gib(room.available)}"
    else:
        missing = "; ".join(f"{run.owner}: {run.use.reason}" for run in room.cap.runs
                            if isinstance(run.use, admission_records.RunUseUnavailable))
        calculation = "Cap unavailable"
        if missing:
            calculation += f": swap-backed use unavailable for {missing}"
        calculation += f"; free {gib(room.available)}"
    ci = f" (CI keep-aside {gib(room.ci_keep_aside.bytes)})" if room.ci_keep_aside.bytes > 0 else ""
    text = (f"{calculation} "
            f"− safety {gib(room.safety)}; kept aside {gib(room.kept)}{ci}; room {gib(room.room)}")
    if room.shortfall:
        text += f"; shortfall {gib(room.shortfall)}"
    return text


def room_check_text(check: admission_records.RoomCheck) -> str:
    relation = "<=" if check.need <= check.room else ">"
    result = "pass" if check.passes else "held"
    basis = check.basis
    exception = ("; starts alone" if isinstance(basis, admission_records.StartsAlone) else
                 "; within held slot" if isinstance(basis, admission_records.WithinSlot) else
                 "; head override" if isinstance(basis, admission_records.OverrideRoom) else "")
    return f"Room: need {gib(check.need)} {relation} room {gib(max(0, check.room))}: {result}{exception}"


def keep_aside_text(run: admission_records.RunKeepAside) -> str:
    use = (f"anonymous use {gib(run.memory.use.bytes)}" if isinstance(run.memory.use, admission_records.AnonymousRunUse)
           else f"swap-backed use unavailable: {run.memory.use.reason}")
    return (f"need {gib(run.memory.need)}; {use}; held slot {gib(run.memory.held)}; "
            f"kept aside {gib(run.bytes)}")


def pressure_text(pressure: admission_records.MemoryPressure | admission_records.PressureUnavailable, policy: admission_records.AdmissionPolicy) -> str:
    if isinstance(pressure, admission_records.PressureUnavailable):
        return pressure.reason
    values = (f"memory pressure: full avg10 {pressure.full_avg10:.1f}%, "
              + f"some avg10 {pressure.some_avg10:.1f}%, swap used {pressure.swap_used_percent:.1f}%")
    if isinstance(pressure.condition, admission_records.PressureConditionNotRecorded):
        return values + "; condition not recorded by service"
    if pressure.condition == "swap":
        return (values + f"; starts when swap is at most {policy.pressure.swap_used_limit:g}% "
                + f"or some avg10 is at most {policy.pressure.some_avg10_limit:.1f}%")
    return values + f"; starts below full avg10 {policy.pressure.full_avg10_limit:.1f}%"


def estimate_refresh_text(reason: admission_records.EstimateExpired, zone: ZoneInfo) -> str:
    return ("the estimate expired at "
            + head_facts.clock(reason.estimated_at, zone, seconds=False)
            + "; the client refreshes it through the admission service")


def release_text(release: gate.HeadAfterEnd, explanation: admission_records.QueueExplanation,
                 labels: Mapping[str, unit_labels.UnitLabels], zone: ZoneInfo, *, names: StepNames) -> str:
    if isinstance(release, gate.HeadRelease):
        if not release.ended:
            text = "no running step needs to end"
        else:
            ended_steps = [step_labels(explanation.running[index].step, labels).session
                           + " " + queue_lines.step_prose(explanation.running[index].step, names) for index in release.ended]
            text = (f"{' + '.join(ended_steps)} end (keep-aside drops {gib(release.growth_drop)}; "
                    + f"free memory rises {gib(release.freed)})")
        if isinstance(release, gate.HeadStartsAt):
            starts_at = head_facts.clock(release.at, zone, zone_name=True)
            text += ("; then " if release.ended else "; ") + f"start spacing permits it at {starts_at}"
    elif isinstance(release, admission_records.EstimateExpired):
        text = estimate_refresh_text(release, zone)
    elif isinstance(release, gate.HeadStillHeld):
        pressure = release.decision.pressure
        causes = (pressure.reason if cause == "pressure" and isinstance(pressure, admission_records.PressureUnavailable) else cause
                  for cause in release.causes)
        text = "all running steps ending still leaves " + ", ".join(causes) + " holding the head"
    elif isinstance(release, admission_records.TokenWaitersOnly):
        text = "all waiting steps are waiting for the cargo token"
    elif isinstance(release, admission_records.ReadingUnavailable):
        text = release.reason
    else:
        text = "nothing waits"
    head = explanation.head
    if (isinstance(head, admission_records.HeadExplanation) and isinstance(head.cause, admission_records.MemoryPressure)
            and not isinstance(release, gate.HeadStartsAt)):
        text += "; the pressure hold must also clear"
    return text


def service_activity_text(memory: admission_records.CompilerServiceMemory | admission_records.UnownedSliceMemory, now: float) -> str:
    """Describe the recorded service state at the pass time."""
    state = memory.job_state
    if isinstance(state, NotServiceSlice):
        raise ValueError("a build scope has no service state")
    if isinstance(state, RunningServiceJob):
        elapsed = f"{age(state.started_at, now)} min" if state.started_at else "age unknown"
        return f"running job, {elapsed}"
    return "idle"


def service_text(memory: admission_records.CompilerServiceMemory | admission_records.UnownedSliceMemory, now: float) -> str:
    """Show service RAM and swap with the job state from the same pass."""
    if isinstance(memory, admission_records.UnownedSliceMemory) and memory.diagnostic:
        return f"{pid_text(memory.pid)} · {field(memory.owner)}: unavailable; {memory.diagnostic}"
    activity = service_activity_text(memory, now)
    if isinstance(memory, admission_records.UnownedSliceMemory):
        name, amount = memory.owner, ci_amount(memory.bytes)
    else:
        name, amount = "sccache.service", gib(memory.bytes)
    return f"{pid_text(memory.pid)} · {field(name)}: {activity}; {amount} RAM; {gib(memory.swap_bytes)} swapped"


def running_text(running: admission_records.ExplainedRunningStep, explanation: admission_records.QueueExplanation,
                 labels: Mapping[str, unit_labels.UnitLabels], zone: ZoneInfo, *, names: StepNames) -> str:
    memory = running.step.measured
    if isinstance(memory, admission_records.CompilerServiceMemory) or (
            isinstance(memory, admission_records.UnownedSliceMemory) and isinstance(memory.job_state, (IdleService, RunningServiceJob))):
        ci = explanation.room.ci_keep_aside.bytes if isinstance(explanation.room, admission_records.MemoryRoom) else 0
        extra = f"; CI keep-aside {gib(ci)}" if ci and isinstance(memory, admission_records.UnownedSliceMemory) else ""
        return service_text(memory, explanation.now) + extra
    identity = step_labels(running.step, labels)
    admitted = running.admitted_at
    started = ("admission time unknown" if isinstance(admitted, admission_records.AdmissionTimeNotRecorded) else
               f"admitted {head_facts.clock(admitted, zone, seconds=False)} ({age(admitted, explanation.now)} min ago)")
    using = gib(running.measured) if isinstance(running.step.measured, admission_records.MeasuredMemory) else "—"
    swap = f"; {gib(memory.swap_bytes)} swapped" if isinstance(memory, admission_records.MeasuredMemory) and memory.swap_bytes else ""
    owner = running.owner
    room = explanation.room
    keep = next((entry for entry in room.runs if entry.memory.owner == owner), None) if isinstance(room, admission_records.MemoryRoom) else None
    evidence = keep_aside_text(keep) if keep is not None else f"anonymous use {using}; no accepted run hold"
    phase = ""
    if isinstance(memory, admission_records.MeasuredMemory) and isinstance(memory.peak_history, admission_records.TestPhaseStarted):
        observed = memory.peak_history
        phase = (f"; tests started: need {gib(gate.test_phase_charge(observed))} "
                 f"(test peak {gib(observed.peak_so_far)}, history {gib(observed.need.bytes)} x {observed.need.margin:g})")
    return (f"{step_pid_text(running.step)} · {identity.showrunner} / {identity.unit} / {identity.session}, {queue_lines.step_prose(running.step, names)}: {started}; "
            + evidence + phase + swap)


def need_details(source: admission_records.RecordedNeed | admission_records.NeedSourceNotRecorded) -> str:
    if isinstance(source, admission_records.NeedSourceNotRecorded):
        return "Package, filters and width: not recorded"
    filters = ", ".join(source.filters) if source.filters else "none"
    package = source.package or "not recorded"
    width = str(source.test_threads) if isinstance(source.test_threads, int) else "not recorded"
    return f"Package: {package}; filters: {filters}; width: {width}; scope: {source.scope}"
