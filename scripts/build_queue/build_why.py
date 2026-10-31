#!/usr/bin/env python3
"""Explain the first waiting build from admission's own read-only snapshot."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
import io
import os
from pathlib import Path
import sys
from contextlib import redirect_stderr
from typing import assert_never
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.build_queue import build_queue, head_facts
from scripts.lint import memory_admit as gate
from scripts.lib.user_zone import user_zone
from scripts.lib.terminal_cells import terminal_width
from scripts.message import unit_labels


def gib(value: int) -> str:
    return f"{value / gate.GIB:.1f} GiB"


def minute(instant: float, zone: ZoneInfo) -> str:
    return datetime.fromtimestamp(instant, zone).strftime("%H:%M")


def aligned_table(headers: Sequence[str], rows: Sequence[Sequence[str]], numeric: frozenset[int]) -> list[str]:
    widths = [max(terminal_width(header), *(terminal_width(row[index]) for row in rows))
              for index, header in enumerate(headers)]

    def row_text(row: Sequence[str]) -> str:
        cells = [(" " * (widths[index] - terminal_width(cell)) + cell) if index in numeric
                 else (cell + " " * (widths[index] - terminal_width(cell)))
                 for index, cell in enumerate(row)]
        return "| " + " | ".join(cells) + " |"

    return [row_text(headers), *(row_text(row) for row in rows)]


def charge_text(basis: gate.ChargeBasis, zone: ZoneInfo) -> str:
    if isinstance(basis, gate.PromiseNoReading):
        return "promise: no reading yet"
    if isinstance(basis, gate.RampPromise):
        return f"ramp promise: first {gate.RAMP_SECONDS // 60} min after admission"
    if isinstance(basis, gate.LifetimePromise):
        return "release build promise: holds until the step ends"
    if isinstance(basis, gate.PeakSoFar):
        text = f"compiling, peak so far {gib(basis.bytes)}"
        if isinstance(basis.peak_at, float):
            text += f" at {minute(basis.peak_at, zone)}"
        if basis.old_sampler:
            text += "; old-sampler rule: peak drops 25 min after its last new peak"
        return text
    if isinstance(basis, gate.PeakDecayed):
        text = f"peak decayed at {minute(basis.decayed_at, zone)}; using now"
        if basis.old_sampler:
            text += "; old-sampler rule: peak drops 25 min after its last new peak"
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


def need_text(source: gate.RecordedNeed | gate.NeedSourceNotRecorded) -> str:
    if isinstance(source, gate.NeedSourceNotRecorded):
        return "source not recorded"
    basis = source.basis
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
                         + minute(datetime.fromisoformat(basis.floor.ended_at).timestamp(), user_zone()))
            else:
                text += "; no failed-build floor"
            if isinstance(basis.factor, gate.ReleaseHeadroom):
                text += f"; release headroom × {basis.factor.factor:.3f}"
            elif isinstance(basis.factor, gate.DevHeadroom):
                text += f"; dev promise factor × {basis.factor.factor:.3f}"
            else:
                text += "; no headroom factor"
        text += (f"; capped at {gib(gate.build_budget_bytes())} (uncapped {gib(basis.cap.uncapped)})"
                 if isinstance(basis.cap, gate.CappedAtBudget) else "; under build budget")
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
    else:
        assert_never(source.source)
    text += f", last {source.history_days} days"
    if source.scope == "filtered":
        text += f", filtered tests ({source.package}, {len(source.filters)} filters)"
    else:
        text += ", full tests" if source.package else ", full scope"
    if isinstance(source.test_threads, int):
        text += f", width {source.test_threads}"
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


def pressure_text(pressure: gate.MemoryPressure, zone: ZoneInfo) -> str:
    if pressure.next_start_at:
        return f"recovery spacing: starts 30 s apart, next start at {minute(pressure.next_start_at, zone)}"
    return (f"memory pressure: full avg10 {pressure.full_avg10:.1f}%, "
            + f"some avg10 {pressure.some_avg10:.1f}%, swap used {pressure.swap_used_percent:.1f}%")


def estimate_refresh_text(reason: gate.EstimateExpired, zone: ZoneInfo) -> str:
    return ("the estimate expired at "
            + datetime.fromtimestamp(reason.estimated_at, zone).strftime("%H:%M")
            + "; it refreshes on the next poll")


def release_text(explanation: gate.QueueExplanation,
                 labels: Mapping[str, unit_labels.UnitLabels], zone: ZoneInfo) -> str:
    release = gate.smallest_head_release(explanation)
    if isinstance(release, gate.HeadRelease):
        if not release.ended:
            text = "no running step needs to end"
        else:
            names = [build_queue.step_labels(explanation.running[index].step, labels).session
                     + " " + explanation.running[index].step.step for index in release.ended]
            text = (f"{' + '.join(names)} end (budget charge drops {gib(release.charge_drop)}; "
                    + f"free memory rises {gib(release.freed)} and promise not yet used drops {gib(release.growth_drop)})")
        if isinstance(release, gate.HeadStartsAt):
            starts_at = datetime.fromtimestamp(release.at, zone).strftime("%H:%M:%S %Z")
            text += ("; then " if release.ended else "; ") + f"recovery spacing starts it at {starts_at}"
    elif isinstance(release, gate.EstimateExpired):
        text = estimate_refresh_text(release, zone)
    elif isinstance(release, gate.HeadStillHeld):
        text = "all running steps ending still leaves " + ", ".join(release.causes) + " holding the head"
    elif isinstance(release, gate.ReadingUnavailable):
        text = "admission memory reading unavailable"
    else:
        text = "nothing waits"
    head = explanation.head
    if (isinstance(head, gate.HeadExplanation) and isinstance(head.decision.pressure, gate.MemoryPressure)
            and not isinstance(release, gate.HeadStartsAt)):
        text += "; the pressure hold or recovery spacing must also clear"
    return text


def render_explanation(explanation: gate.QueueExplanation, zone: ZoneInfo,
                       labels: Mapping[str, unit_labels.UnitLabels],
                       facts: head_facts.HeadFacts | gate.NoWaiter) -> list[str]:
    host = explanation.host
    if isinstance(host, gate.ReadingUnavailable):
        raise ValueError("host memory unavailable")
    total = gib(host.total) if isinstance(host.total, int) else "total unavailable"
    header = (f"Build queue at {minute(explanation.now, zone)}: free {gib(host.available)} of {total}; "
              + f"build charges {gib(explanation.totals.charged)} of {gib(host.budget)} budget")
    if isinstance(host.pressure, gate.MemoryPressure):
        header += "; " + pressure_text(host.pressure, zone)
    load_path = Path(os.environ.get("BUILDLOG_PROC", "/proc")) / "loadavg"
    try:
        load = float(load_path.read_text().split()[0])
        header += f"; load {load:.2f} (1 min) on {os.cpu_count() or 1} cores"
    except (OSError, ValueError, IndexError):
        header += "; load unavailable"
    lines = [header, "", "Running:"]
    rows: list[list[str]] = []
    for running in explanation.running:
        identity = build_queue.step_labels(running.step, labels)
        rows.append([build_queue.field(cell) for cell in (
            identity.showrunner, identity.unit, identity.session, running.step.step,
            (f"{build_queue.age(running.admitted_at, explanation.now)} min"
             if isinstance(running.admitted_at, float) else "—"), gib(running.promise),
            gib(running.charged), gib(running.measured), charge_text(running.basis, zone))])
    if rows:
        lines.extend(aligned_table(("Showrunner", "Unit", "Session", "Step", "Age", "Promise", "Charged",
                                    "Using now", "Charge basis"), rows, frozenset((4, 5, 6, 7))))
    else:
        lines.append("None.")
    lines.append(f"Total: charged {gib(explanation.totals.charged)}, using {gib(explanation.totals.measured)}")
    head = explanation.head
    if isinstance(head, gate.NoWaiter) or isinstance(facts, gate.NoWaiter):
        return [*lines, "", "Nothing waits."]
    identity = build_queue.step_labels(head.waiter, labels)
    lines.extend(("", f"First in line: {identity.showrunner} / {identity.unit} / {identity.session}, "
        + f"{head.waiter.step}; waiting {build_queue.age(head.waiter.since, explanation.now)} min; "
        + f"need {gib(head.waiter.need)}; {need_text(head.need_source)}",
        head_facts.bump_text(facts, zone)))
    bumps = [[build_queue.field(cell) for cell in row] for row in head_facts.bump_rows(facts, zone, labels)]
    if bumps:
        lines.extend(aligned_table(("Step", "Session", "Folder", "Bumps", "By", "Last bump"), bumps, frozenset((3,))))
    remainder = head_facts.bump_remainder(facts)
    if remainder:
        lines.append(remainder)
    lines.append("Order rule: " + head_facts.order_text(facts, zone))
    lines.extend(head_facts.cargo_token_lines(facts, zone, labels))
    lines.extend(("", budget_text(head.budget), free_text(head.free)))
    if isinstance(head.decision.pressure, gate.MemoryPressure):
        lines.append("Pressure: " + pressure_text(head.decision.pressure, zone))
    line = explanation.line
    lines.append(f"The line: {line.count} waiting behind it; {line.fitting} would fit now under both checks; "
                 + f"needs from {line.smallest_need / gate.GIB:.1f} to {gib(line.largest_need)}; "
                 + f"oldest wait {build_queue.age(line.oldest_since, explanation.now)} min")
    causes: list[str] = []
    if isinstance(head.waiter.reason, gate.EstimateExpired):
        causes.append("estimate refresh: " + estimate_refresh_text(head.waiter.reason, zone))
    if not head.budget.passes:
        causes.append("build budget")
    if not head.free.passes:
        causes.append("free memory")
    if isinstance(head.decision.pressure, gate.MemoryPressure):
        causes.append(pressure_text(head.decision.pressure, zone))
    if head.decision.state == "admit":
        causes = ["ready for the next admission check"]
    elif not causes:
        causes = ["queue order"]
    releases = release_text(explanation, labels, zone)
    lines.extend(("", "Waiting because: " + ", ".join(causes) + ". Frees when: " + releases + "."))
    return lines


def main() -> int:
    try:
        # A damaged recovery record must not turn the one-line failure protocol into a traceback.
        with redirect_stderr(io.StringIO()):
            reading = gate.queue_read()
            if isinstance(reading, gate.QueueBusy):
                print("queue busy; try again")
                return 0
            explanation = reading.explanation
            facts = head_facts.read_head_facts(reading)
            session_ids = [row.step.session_id for row in explanation.running]
            if isinstance(explanation.head, gate.HeadExplanation):
                session_ids.append(explanation.head.waiter.session_id)
            session_ids.extend(head_facts.session_ids(facts))
            labels = unit_labels.unit_labels_for(session_ids, os.environ)
            lines = render_explanation(explanation, user_zone(), labels, facts)
    except Exception:
        print("build-why: the admission state could not be read.")
        return 2
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
