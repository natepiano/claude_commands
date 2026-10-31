"""The compact queue view and the checks shared with one-step cards."""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cached_property
from typing import assert_never
from zoneinfo import ZoneInfo

from scripts.build_queue import head_facts
from scripts.build_queue.queue_lines import Line, Span, age, clip, pad_left, pad_right, plain, trim_right, wrap
from scripts.lint import memory_admit as gate
from scripts.message.unit_labels import UnitLabels


@dataclass(frozen=True)
class QueueModel:
    reading: gate.QueueRead
    labels: Mapping[str, UnitLabels]
    facts: head_facts.HeadFacts | gate.NoWaiter
    pace: head_facts.RecordedPace | head_facts.PaceUnavailable
    zone: ZoneInfo

    @cached_property
    def moves(self) -> head_facts.QueueMoves:
        return head_facts.queue_moves(self.reading, self.facts)


@dataclass(frozen=True)
class ChecksNotRecorded:
    """Part A has no admission checks for a step behind the head."""


def step_checks(model: QueueModel, position: int) -> gate.HeadExplanation | gate.ReadingUnavailable | ChecksNotRecorded:
    # Part B plugs the admission-owned LineWaiter checks in here.
    explanation = model.reading.explanation
    if isinstance(explanation.host, gate.ReadingUnavailable):
        return explanation.host
    reason = model.reading.view.waiting[position - 1].reason
    if isinstance(reason, gate.ReadingUnavailable):
        return reason
    head = explanation.head
    return head if position == 1 and isinstance(head, gate.HeadExplanation) else ChecksNotRecorded()


def field(text: str) -> str:
    return " ".join(text.split())


def step_labels(step: gate.WaitingStep | gate.RunningStep, labels: Mapping[str, UnitLabels]) -> UnitLabels:
    identity = labels.get(step.session_id)
    if identity is not None:
        return identity
    fallback = field(step.session_name) or step.folder.rstrip("/").rpartition("/")[2] or "unknown"
    return UnitLabels(fallback)


def shown_waiters(selector: str, reading: gate.QueueRead, labels: Mapping[str, UnitLabels]) -> list[int]:
    return [position for position, step in enumerate(reading.view.waiting, 1)
            if step_labels(step, labels).session == selector]


def name_spans(step: gate.WaitingStep | gate.RunningStep, labels: Mapping[str, UnitLabels],
               *, production: bool = True) -> Line:
    identity = step_labels(step, labels)
    seen: set[str] = set()
    parts: list[Span] = []
    names = ((identity.showrunner, False),) if production else ()
    for text, session in (*names, (identity.unit, False), (identity.session, True)):
        text = field(text)
        if text == "—" or not text or text in seen:
            continue
        seen.add(text)
        if parts:
            parts.append(Span(" · "))
        parts.append(Span(text, "inactive" if session else "default"))
    return tuple(parts)


def name_text(step: gate.WaitingStep | gate.RunningStep, model: QueueModel) -> str:
    return plain([name_spans(step, model.labels)])


def clock(model: QueueModel, at: float, *, seconds: bool = False) -> str:
    return head_facts.clock(at, model.zone, seconds=seconds)


def estimate_minutes(model: QueueModel, position: int) -> int:
    return max(1, round(position * 60 / model.pace.steps)) if isinstance(model.pace, head_facts.RecordedPace) and model.pace.steps else 0


def start_text(model: QueueModel, position: int, *, clock_time: bool = False) -> str:
    minutes = estimate_minutes(model, position)
    if not minutes:
        return "—"
    return "~" + (clock(model, model.reading.explanation.now + minutes * 60) if clock_time else f"{minutes} min")


def waiting_moves(model: QueueModel, position: int) -> tuple[head_facts.BumpRecord, ...]:
    return model.moves.waiting[position - 1]


def is_moved(model: QueueModel, position: int) -> bool:
    return (isinstance(model.facts, head_facts.HeadFacts) and isinstance(model.facts.bumps, head_facts.BumpHistory)
            and has_move_priority(model, position))


def has_move_priority(model: QueueModel, position: int) -> bool:
    return model.reading.ordered[position - 1][1].get("bump_order", 0) < 0


def move_mark(model: QueueModel, position: int) -> str:
    records = waiting_moves(model, position)
    return f"↑{records[0].old_position}" if records else "↑" if is_moved(model, position) else ""


def pressure_check(pressure: gate.Pressure, model: QueueModel) -> tuple[bool, str]:
    if isinstance(pressure, gate.NoPressureHold):
        return True, "memory pressure: no hold"
    if pressure.next_start_at:
        return False, "recovery spacing: one start per 30 s; next start at " + clock(model, pressure.next_start_at, seconds=True)
    if pressure.swap_used_percent > gate.SWAP_USED_LIMIT and pressure.some_avg10 > gate.SOME_PRESSURE_LIMIT:
        return False, (f"memory pressure: swap {pressure.swap_used_percent:.1f}% used and some avg10 {pressure.some_avg10:.1f}; "
                       + f"starts when swap is at most {gate.SWAP_USED_LIMIT:g}% or some avg10 at most {gate.SOME_PRESSURE_LIMIT:.1f}")
    return False, (f"memory pressure: full avg10 {pressure.full_avg10:.1f} now; starts below {gate.FULL_PRESSURE_LIMIT:.1f}")


def check_line(passes: bool, text: str) -> Line:
    return (Span("✓ " if passes else "✗ ", "success" if passes else "error"), Span(text))


def check_lines(checks: gate.HeadExplanation | gate.ReadingUnavailable | ChecksNotRecorded,
                model: QueueModel, *, pressure_first: bool = True) -> list[Line]:
    if isinstance(checks, gate.ReadingUnavailable):
        return [(Span(reason_phrase(checks)),)]
    if isinstance(checks, ChecksNotRecorded):
        return []
    budget, free = checks.budget, checks.free
    held = f"{budget.charged / gate.GIB:.1f} of {budget.budget / gate.GIB:.1f} GiB held; needs {budget.need / gate.GIB:.1f}"
    if not budget.passes:
        held += f"; starts when held falls to {budget.starts_at / gate.GIB:.1f}"
    elif budget.need > budget.budget:
        held += " (runs alone)"
    parts = [f"this step {free.need / gate.GIB:.1f}"]
    for label, value in (("earlyoom", free.reserve), ("promised not yet used", free.growth)):
        amount = f"{value / gate.GIB:.1f}"
        if amount != "0.0":
            parts.append(f"{label} {amount}")
    available = (f"free memory {free.available / gate.GIB:.1f} GiB; needs {free.threshold / gate.GIB:.1f}: "
                 + " + ".join(parts))
    pressure = check_line(*pressure_check(checks.decision.pressure, model))
    memory = [check_line(budget.passes, "build budget: " + held), check_line(free.passes, available)]
    return [pressure, *memory] if pressure_first else [*memory, pressure]


def head_state(model: QueueModel) -> str:
    explanation = model.reading.explanation
    if isinstance(explanation.host, gate.ReadingUnavailable):
        return "memory reading unavailable"
    head = explanation.head
    if isinstance(head, gate.NoWaiter):
        return "nothing waits"
    if head.decision.state == "admit":
        return "#1 starts at the next check"
    causes: list[str] = []
    if isinstance(head.decision.pressure, gate.MemoryPressure):
        causes.append("recovery spacing" if head.decision.pressure.next_start_at else "memory pressure")
    if not head.budget.passes:
        causes.append("budget")
    if not head.free.passes:
        causes.append("free memory")
    return "#1 waits on " + ", ".join(causes)


def reason_phrase(reason: gate.WaitReason) -> str:
    match reason:
        case gate.ReadingUnavailable():
            return "memory reading unavailable"
        case gate.MemoryPressure():
            return "recovery spacing" if reason.next_start_at else "memory pressure"
        case gate.NeedsBuildBudget():
            return "build budget"
        case gate.NeedsMemory():
            return "free memory"
        case gate.BehindHead():
            return "#1's turn"
        case gate.BehindOlderFit():
            return gate.older_fit_text(reason)
        case gate.ReadyForAdmission():
            return "starts at the next check"
        case gate.HeadAdmissionStatus():
            return "#1's turn"
        case _:
            assert_never(reason)


def place_runs(places: list[int]) -> str:
    runs: list[str] = []
    start = end = 0
    for place in places:
        if runs and place == end + 1:
            end = place
            runs[-1] = f"#{start}-#{end}"
        else:
            start = end = place
            runs.append(f"#{place}")
    return " ".join(runs)


def rest_lines(model: QueueModel) -> list[Line]:
    count = len(model.reading.view.waiting)
    if count < 2:
        return []
    facts = model.facts
    if isinstance(facts, head_facts.HeadFacts) and facts.has_priority and isinstance(facts.order, gate.StrictUntil):
        line = model.reading.explanation.line
        return [(Span(f"#2-#{count}: {head_facts.order_text(facts, model.zone, include_record=False)}; {line.fitting} of {line.count} fit now"),)]
    groups: dict[str, list[int]] = {}
    for position, step in enumerate(model.reading.view.waiting[1:], 2):
        groups.setdefault(reason_phrase(step.reason), []).append(position)
    return [(Span(place_runs(places) + ": " + phrase),) for phrase, places in groups.items()]


def move_lines(model: QueueModel) -> list[Line]:
    # Identity differs across steps; a move list is its actor/time/old-place history.
    groups: dict[tuple[tuple[float, str, int], ...], tuple[tuple[head_facts.BumpRecord, ...], list[str]]] = {}

    def add_move(records: tuple[head_facts.BumpRecord, ...], place: str) -> None:
        if not records:
            return
        key = tuple((record.at, record.actor, record.old_position) for record in records)
        if key in groups:
            groups[key][1].append(place)
        else:
            groups[key] = records, [place]

    for position in range(1, len(model.reading.view.waiting) + 1):
        add_move(waiting_moves(model, position), f"#{position}")
    for running, records in zip(model.reading.explanation.running, model.moves.running):
        add_move(records, "running " + running.step.step)
    ordered = sorted(groups.values(), key=lambda item: -item[0][-1].at)
    lines = [(Span("  ↑ " + " ".join(places) + " moved up: "
                   + head_facts.move_text(records, model.zone, model.labels), "warning"),)
             for records, places in ordered[:5]]
    remaining = sum(len(item[1]) for item in ordered[5:])
    if remaining:
        lines.append((Span(f"  and {remaining} more moved-up steps", "warning"),))
    return lines


def step_row(prefix: Line, step: gate.WaitingStep | gate.RunningStep, model: QueueModel, columns: int) -> Line:
    return trim_right(clip((*prefix, *name_spans(step, model.labels, production=columns >= 100)), columns))


def render_view(model: QueueModel, columns: int) -> list[Line]:
    reading = model.reading
    running = reading.explanation.running
    waiting = reading.view.waiting
    now = reading.explanation.now
    lines: list[Line] = []

    def add(line: Line = (), indent: int = 2) -> None:
        lines.extend(wrap(line, columns, indent))

    add((Span(f"Build queue {clock(model, now, seconds=True)} · {len(running)} running · {len(waiting)} waiting · "
              + head_state(model), bold=True),))
    counts = Counter("other" if (name := field(step_labels(step, model.labels).showrunner)) in ("—", "") else name
                     for step in waiting)
    productions = " · ".join(f"{name} {count}" for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])))
    add((Span("Waiting by production: " + (productions or "none")),))
    add()
    add((Span("Running", bold=True),))
    add((Span("             ran    using  step      held  " + ("production · " if columns >= 100 else "") + "unit · session"),))
    for step, records in zip(running, model.moves.running):
        mark = f"↑{records[0].old_position}" if records else ""
        ran = "—" if isinstance(step.admitted_at, gate.AdmissionTimeNotRecorded) else f"{age(step.admitted_at, now)} min"
        using = f"{step.measured / gate.GIB:.1f}" if isinstance(step.step.measured, gate.MeasuredMemory) else "—"
        prefix = (Span("● ", "success"), Span(pad_left(mark, 5), "warning" if mark else "default"), Span(pad_left(ran, 8)),
                  Span(pad_left(using, 9) + "  " + pad_right(field(step.step.step), 7) + pad_left(f"{step.charged / gate.GIB:.1f}", 6) + "  "))
        lines.append(step_row(prefix, step.step, model, columns))
    if not running:
        add((Span("  none", "inactive"),))
    add()
    add((Span("Waiting · moved-up steps (↑) go first, newest move first; then the longest wait first."),))
    add((Span("          A long wait alone does not make a step next."),), 10)
    for line in move_lines(model):
        add(line, 4)
    add()
    starts = "  starts" if columns >= 80 else ""
    place_width = max(2, len(str(len(waiting))))
    add((Span(f"  {pad_left('#', place_width)}      waited{starts}  step     needs  " + ("production · " if columns >= 100 else "") + "unit · session"),))
    for position, step in enumerate(waiting, 1):
        waited = age(step.since, now)
        mark = move_mark(model, position)
        prefix = (Span("■ " if position == 1 else "  ", "error" if position == 1 else "default"),
                  Span(pad_left(str(position), place_width) + " "), Span(pad_right(mark, 4), "warning" if mark else "default"),
                  Span(pad_left(f"{waited} min", 7), "warning" if waited >= 30 else "default"))
        if columns >= 80:
            prefix += (Span(pad_left(start_text(model, position), 9)),)
        prefix += (Span("  " + pad_right(field(step.step), 7) + pad_left(f"{step.need / gate.GIB:.1f}", 6) + "  "),)
        lines.append(step_row(prefix, step, model, columns))
        if position == 1:
            checks = step_checks(model, 1)
            for index, check in enumerate(check_lines(checks, model)):
                add((Span("         #1 waits on  " if index == 0 else "                      "), *check), 22)
            if isinstance(model.facts, head_facts.HeadFacts):
                if isinstance(model.facts.head_time, head_facts.RecordedHeadTime):
                    add((Span("         " + head_facts.head_time_text(model.facts, model.zone)),), 9)
                token = head_facts.head_token_text(model.facts, model.zone)
                if token:
                    add((Span("         " + token),), 9)
            for line in rest_lines(model):
                add((Span("         "), *line), 9)
    if not waiting:
        add((Span("  none", "inactive"),))
    add()
    if isinstance(model.pace, head_facts.RecordedPace) and model.pace.steps:
        add((Span(f"Memory in GiB. starts: an estimate at the last hour's pace, {model.pace.steps} steps an hour."),))
    else:
        add((Span("Memory in GiB. starts: no step ended in the last hour."),))
    add((Span("One step: /build:queue 4 (or a session name or folder)"),))
    return lines
