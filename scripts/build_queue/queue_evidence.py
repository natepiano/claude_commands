"""Format recorded queue evidence without reading admission configuration."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from datetime import datetime
from typing import assert_never
from zoneinfo import ZoneInfo

from scripts.build_queue import head_facts
from scripts.build_queue.queue_lines import age, field
from scripts.build_queue.scope_activity import IdleService, NotServiceSlice, RunningServiceJob
from scripts.lint import memory_admit as gate
from scripts.message import unit_labels


def step_labels(step: gate.WaitingStep | gate.RunningStep,
                labels: Mapping[str, unit_labels.UnitLabels]) -> unit_labels.UnitLabels:
    identity = labels.get(step.session_id)
    unit = gate.queue_unit(step.unit, step.session_id, labels)
    if identity is not None:
        return replace(identity, unit=unit) if step.unit else identity
    fallback = " ".join(step.session_name.split()) or step.folder.rstrip("/").rpartition("/")[2] or "unknown"
    return unit_labels.UnitLabels(fallback, unit=unit or "—")


def gib(value: int) -> str:
    return f"{value / gate.GIB:.1f} GiB"


def charge_text(basis: gate.ChargeBasis, zone: ZoneInfo, policy: gate.AdmissionPolicy,
                step: str) -> str:
    if isinstance(basis, gate.PromiseNoReading):
        return "promise: no reading yet"
    if isinstance(basis, gate.RampPromise):
        return f"ramp promise: first {policy.timing.ramp_seconds / 60:g} min after admission"
    if isinstance(basis, gate.LifetimePromise):
        name = "release build" if step == "build" else "merge gate" if step == "merge-gate" else step
        return f"{name} promise: holds until the step ends"
    if isinstance(basis, gate.TestPhaseCharge):
        need = basis.need
        return (f"tests since {head_facts.clock(basis.started_at, zone, seconds=False)}: peak so far {gib(basis.peak_so_far)}, "
                f"need {gib(need.bytes)} ({need.form}, p90 of {need.runs} runs at width {need.recorded_width}), "
                f"× {basis.need.margin:g}")
    if isinstance(basis, gate.PeakSoFar):
        text = f"{gate.peak_activity(step)}, peak so far {gib(basis.bytes)}"
        if isinstance(basis.peak_at, float):
            text += f" at {head_facts.clock(basis.peak_at, zone, seconds=False)}"
        if basis.old_sampler:
            text += f"; old-sampler rule: peak drops {policy.peak_hold.seconds_for(step) / 60:g} min after its last new peak"
        return text
    if isinstance(basis, gate.PeakDecayed):
        text = f"peak decayed at {head_facts.clock(basis.decayed_at, zone, seconds=False)}; using now"
        if basis.old_sampler:
            text += f"; old-sampler rule: peak drops {policy.peak_hold.seconds_for(step) / 60:g} min after its last new peak"
        return text
    if isinstance(basis, gate.PeakFileUnreadable):
        return "peak file unreadable: promise kept"
    if isinstance(basis, gate.UsingNow):
        return "using now"
    match basis:
        case gate.EndedCharge():
            return "step ended"
        case _:
            assert_never(basis)


def need_text(source: gate.RecordedNeed | gate.NeedSourceNotRecorded, *,
              zone: ZoneInfo) -> str:
    if isinstance(source, gate.NeedSourceNotRecorded):
        return "source not recorded"
    basis = source.basis
    if isinstance(basis, gate.ProgramMemoryCap):
        program = "the merge gate" if basis.program == "merge-gate" else basis.program
        return f"{program}'s memory cap"
    if isinstance(basis, (gate.ShapeP90, gate.ShapeLargest, gate.ShapeFallback)):
        if isinstance(basis, gate.ShapeP90):
            text = f"same-shape p90 of {basis.runs} runs"
        elif isinstance(basis, gate.ShapeLargest):
            text = f"same-shape largest peak of {basis.runs} runs"
        else:
            text = "same-shape fallback: no runs; heavier of fallback and same-profile builds"
        text += f", last {source.history_days} days"
        if isinstance(basis, gate.ShapeFallback):
            text += "; no headroom factor"
        else:
            if isinstance(basis.floor, gate.FailedPeakFloor):
                text += (f"; failed-build floor {gib(basis.floor.peak)} from "
                         + head_facts.clock(datetime.fromisoformat(basis.floor.ended_at).timestamp(), zone, seconds=False))
            else:
                text += "; no failed-build floor"
            if isinstance(basis.factor, gate.ReleaseHeadroom):
                text += f"; release headroom × {basis.factor.factor:.3f}"
            elif isinstance(basis.factor, gate.DevSizeHeadroom):
                text += (f"; dev size headroom × {basis.factor.factor:.3f}"
                         + f" + margin {gib(basis.factor.margin_bytes)}")
            elif isinstance(basis.factor, gate.LegacyDevMultiplierHeadroom):
                text += f"; dev promise factor × {basis.factor.factor:.3f}"
            else:
                text += "; no headroom factor"
        if isinstance(basis.cap, gate.CappedAtBudget):
            limit = ("the build budget of that time" if isinstance(basis.cap.cap, gate.CapNotRecorded)
                     else gib(basis.cap.cap))
            text += f"; capped at {limit} (uncapped {gib(basis.cap.uncapped)})"
        else:
            text += "; under build budget"
        return text
    if source.source == "measured":
        text = f"measured process-memory p90 of {source.runs} runs"
    elif source.source == "buildlog":
        text = f"build-log peak × {gate.ANON_SHARE:.2f} of {source.runs} runs"
    elif source.source == "pooled":
        text = f"pooled over all repos, {source.runs} runs"
    elif source.source == "fallback":
        text = "fallback: fewer than 5 runs"
    elif source.source == "shape":
        text = "same-shape promise: basis not recorded"
    elif source.source == "cap":
        text = "memory cap: program not recorded"
    else:
        text = f"{source.source}: basis not recorded"
    text += f", last {source.history_days} days"
    if source.scope == "filtered":
        text += ", filtered tests"
    else:
        text += ", full tests" if source.package else ", full scope"
    return text


def budget_text(check: gate.BudgetCheck) -> str:
    total = check.charged + check.need
    terms = f"charged {check.charged / gate.GIB:.1f} + need {check.need / gate.GIB:.1f}"
    relation = "<=" if total <= check.budget else ">"
    result = "pass" if check.passes else "blocked"
    exception = "; run-alone exception" if check.passes and total > check.budget else ""
    return (f"Build budget: {terms} = {gib(total)} {relation} {gib(check.budget)}: {result}{exception}; "
            + f"starts when charged falls to {gib(check.starts_at)}")


def free_text(check: gate.FreeCheck) -> str:
    terms = f"need {check.need / gate.GIB:.1f} + earlyoom {check.reserve / gate.GIB:.1f}"
    terms += f" + promise not yet used {check.growth / gate.GIB:.1f}"
    relation = ">=" if check.passes else "<"
    result = "pass" if check.passes else "blocked"
    return f"Free memory: free {gib(check.available)} {relation} {terms} = {gib(check.threshold)}: {result}"


def pressure_text(pressure: gate.MemoryPressure, zone: ZoneInfo,
                  policy: gate.AdmissionPolicy) -> str:
    if pressure.next_start_at:
        return (f"recovery spacing: one start per {policy.timing.recovery_start_seconds:g} s; next start at "
                + head_facts.clock(pressure.next_start_at, zone))
    values = (f"memory pressure: full avg10 {pressure.full_avg10:.1f}%, "
              + f"some avg10 {pressure.some_avg10:.1f}%, swap used {pressure.swap_used_percent:.1f}%")
    if isinstance(pressure.condition, gate.PressureConditionNotRecorded):
        return values + "; condition not recorded by service"
    if pressure.condition == "swap":
        return (values + f"; starts when swap is at most {policy.pressure.swap_used_limit:g}% "
                + f"or some avg10 is at most {policy.pressure.some_avg10_limit:.1f}%")
    return values + f"; starts below full avg10 {policy.pressure.full_avg10_limit:.1f}%"


def estimate_refresh_text(reason: gate.EstimateExpired, zone: ZoneInfo) -> str:
    return ("the estimate expired at "
            + head_facts.clock(reason.estimated_at, zone, seconds=False)
            + "; the client refreshes it through the admission service")


def release_text(release: gate.HeadAfterEnd, explanation: gate.QueueExplanation,
                 labels: Mapping[str, unit_labels.UnitLabels], zone: ZoneInfo) -> str:
    if isinstance(release, gate.HeadRelease):
        if not release.ended:
            text = "no running step needs to end"
        else:
            names = [step_labels(explanation.running[index].step, labels).session
                     + " " + explanation.running[index].step.step for index in release.ended]
            text = (f"{' + '.join(names)} end (budget charge drops {gib(release.charge_drop)}; "
                    + f"free memory rises {gib(release.freed)} and promise not yet used drops {gib(release.growth_drop)})")
        if isinstance(release, gate.HeadStartsAt):
            starts_at = head_facts.clock(release.at, zone, zone_name=True)
            text += ("; then " if release.ended else "; ") + f"recovery spacing starts it at {starts_at}"
    elif isinstance(release, gate.EstimateExpired):
        text = estimate_refresh_text(release, zone)
    elif isinstance(release, gate.HeadStillHeld):
        text = "all running steps ending still leaves " + ", ".join(release.causes) + " holding the head"
    elif isinstance(release, gate.TokenWaitersOnly):
        text = "all waiting steps are waiting for the cargo token"
    elif isinstance(release, gate.ReadingUnavailable):
        text = "admission memory reading unavailable"
    else:
        text = "nothing waits"
    head = explanation.head
    if (isinstance(head, gate.HeadExplanation) and isinstance(head.decision.pressure, gate.MemoryPressure)
            and not isinstance(release, gate.HeadStartsAt)):
        text += "; the pressure hold or recovery spacing must also clear"
    return text


def service_text(memory: gate.CompilerServiceMemory | gate.UnownedSliceMemory, now: float) -> str:
    """Show service RAM and swap with the job state from the same pass."""
    state = memory.job_state
    if isinstance(state, NotServiceSlice):
        raise ValueError("a build scope has no service state")
    if isinstance(state, RunningServiceJob):
        elapsed = f"{age(state.started_at, now)} min" if state.started_at else "age unknown"
        activity = f"running job, {elapsed}"
    else:
        activity = "idle"
    name = memory.owner if isinstance(memory, gate.UnownedSliceMemory) else "sccache.service"
    return f"{field(name)}: {activity}; {gib(memory.bytes)} RAM; {gib(memory.swap_bytes)} swapped"


def running_text(running: gate.ExplainedRunningStep, explanation: gate.QueueExplanation,
                 labels: Mapping[str, unit_labels.UnitLabels], zone: ZoneInfo) -> str:
    memory = running.step.measured
    if isinstance(memory, gate.CompilerServiceMemory) or (
            isinstance(memory, gate.UnownedSliceMemory) and isinstance(memory.job_state, (IdleService, RunningServiceJob))):
        return (service_text(memory, explanation.now)
                + f"; promise {gib(running.promise)}, charge {gib(running.charged)}")
    identity = step_labels(running.step, labels)
    admitted = running.admitted_at
    started = ("admission time unknown" if isinstance(admitted, gate.AdmissionTimeNotRecorded) else
               f"admitted {head_facts.clock(admitted, zone, seconds=False)} ({age(admitted, explanation.now)} min ago)")
    using = gib(running.measured) if isinstance(running.step.measured, gate.MeasuredMemory) else "—"
    swap = f"; {gib(memory.swap_bytes)} swapped" if isinstance(memory, gate.MeasuredMemory) and memory.swap_bytes else ""
    return (f"{identity.showrunner} / {identity.unit} / {identity.session}, {running.step.step}: {started}; "
            + f"promise {gib(running.promise)}, charge {gib(running.charged)}, using now {using}; "
            + charge_text(running.basis, zone, explanation.policy, running.step.step) + swap)


def need_details(source: gate.RecordedNeed | gate.NeedSourceNotRecorded) -> str:
    if isinstance(source, gate.NeedSourceNotRecorded):
        return "Package, filters and width: not recorded"
    filters = ", ".join(source.filters) if source.filters else "none"
    package = source.package or "not recorded"
    width = str(source.test_threads) if isinstance(source.test_threads, int) else "not recorded"
    return f"Package: {package}; filters: {filters}; width: {width}; scope: {source.scope}"
