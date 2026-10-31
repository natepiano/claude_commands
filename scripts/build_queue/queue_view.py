"""The compact queue view and the checks shared with one-step cards."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cached_property
from typing import assert_never
from zoneinfo import ZoneInfo

from scripts.build_queue import head_facts
from scripts.build_queue.queue_lines import Line, Span, age, clip, cut_tail, pad_left, pad_right, plain, terminal_width
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


def step_checks(model: QueueModel, position: int) -> gate.HeadExplanation | gate.LineWaiter | gate.ReadingUnavailable:
    explanation = model.reading.explanation
    if isinstance(explanation.host, gate.ReadingUnavailable):
        return explanation.host
    step = model.reading.view.waiting[position - 1]
    if isinstance(step.reason, gate.ReadingUnavailable):
        return step.reason
    if position == 1 and isinstance(explanation.head, gate.HeadExplanation):
        return explanation.head
    return next(item for item in explanation.line.steps if item.waiter == step)


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
    return has_move_priority(model, position)


def has_move_priority(model: QueueModel, position: int) -> bool:
    return model.reading.ordered[position - 1][1].get("priority_rank", 1) == 0


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


def check_lines(checks: gate.HeadExplanation | gate.LineWaiter | gate.ReadingUnavailable,
                model: QueueModel, *, pressure_first: bool = True) -> list[Line]:
    if isinstance(checks, gate.ReadingUnavailable):
        return [(Span(reason_phrase(checks, model.reading.view.waiting)),)]
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


def reason_phrase(reason: gate.WaitReason, waiting: Sequence[gate.WaitingStep]) -> str:
    match reason:
        case gate.EstimateExpired():
            return "estimate expired"
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
            place = next((place for place, step in enumerate(waiting, 1)
                          if step.session_id == reason.older_session_id and step.since == reason.older_since), 0)
            return f"behind #{place}, an older step that fits" if place else "behind an older step that fits"
        case gate.ReadyForAdmission():
            return "starts at the next check"
        case gate.HeadAdmissionStatus():
            return "#1's turn"
        case _:
            assert_never(reason)


def bar_eighths(gib: float, budget: float) -> int:
    """Round to the nearest eighth on the shared one-cell-per-GiB scale."""
    return int(max(0.0, min(gib, budget)) * 8 + 0.5)


def size_bar(gib: float, budget: float) -> str:
    eighths = bar_eighths(gib, budget)
    whole, fraction = divmod(eighths, 8)
    return "█" * whole + ("▏▎▍▌▋▊▉"[fraction - 1] if fraction else "")


def countdown(until: float, now: float) -> str:
    minutes, seconds = divmod(max(0, int(until - now)), 60)
    return f"{minutes}:{seconds:02d}"


def head_status(model: QueueModel, budget: int) -> Line:
    head = model.reading.explanation.head
    if isinstance(head, gate.NoWaiter):
        return ()
    recorded = bool(model.reading.ordered and "priority_since" in model.reading.ordered[0][1])
    strict = head.has_priority and (not recorded or isinstance(head.order, gate.StrictUntil))
    room = gate.strict_room(budget, head.waiter.need)
    allows_room = strict and recorded and isinstance(head.order, gate.StrictUntil) and room > 0
    tone = "error" if strict and not allows_room else "success"
    state = f" ↑ ≤{room / gate.GIB:.1f}" if allows_room else " ⊘" if strict else " ↑"
    result = (Span(state, tone),)
    if recorded and not isinstance(head.order, gate.WindowNotStarted):
        result += (Span(" "), Span(countdown(head.order.deadline, model.reading.explanation.now),
                                   tone, until=head.order.deadline))
    reason = head.waiter.reason
    if isinstance(reason, gate.EstimateExpired):
        result += (Span(" estimate expired", "error"),)
    elif isinstance(reason, gate.ReadingUnavailable):
        result += (Span(" no reading", "error"),)
    elif isinstance(reason, gate.MemoryPressure) or isinstance(head.decision.pressure, gate.MemoryPressure):
        result += (Span(" pressure", "error"),)
    elif (isinstance(reason, gate.NeedsMemory)
          or isinstance(reason, gate.HeadAdmissionStatus) and not reason.decision.free_passes):
        result += (Span(" low memory", "error"),)
    return result


def budget_bar(budget: float, free: float, *, colour: bool) -> Line:
    total_eighths = bar_eighths(budget, budget)
    free_eighths = bar_eighths(free, budget)
    width = (total_eighths + 7) // 8
    if not colour:
        free_cells = width if free_eighths == total_eighths else min(width, (free_eighths + 4) // 8)
        return (Span("░" * free_cells, "success"), Span("█" * (width - free_cells), "suggestion"))
    green = size_bar(free_eighths / 8, budget)
    whole = len(green.rstrip("▏▎▍▌▋▊▉"))
    boundary = green[whole:]
    result = (Span("█" * whole, "success"),)
    if boundary:
        result += (Span(boundary, "success", background="suggestion" if free_eighths < total_eighths else "default"),)
    remaining = max(0, total_eighths - len(green) * 8)
    return (*result, Span(size_bar(remaining / 8, budget), "suggestion"))


def render_view(model: QueueModel, columns: int, *, colour: bool = False, you: str = "") -> list[Line]:
    explanation = model.reading.explanation
    head, host = explanation.head, explanation.host
    budget_bytes = (host.budget if isinstance(host, gate.HostAvailable) else
                    head.budget.budget if isinstance(head, gate.HeadExplanation) else gate.build_budget_bytes())
    budget = budget_bytes / gate.GIB
    taken = explanation.totals.charged / gate.GIB
    free = max(0.0, budget - taken)
    waiting = model.reading.view.waiting
    identities = [step_labels(step, model.labels) for step in waiting]
    names = [(field(identity.showrunner) or "—",
              field(identity.unit).removeprefix(field(identity.showrunner) + "-") or "—") for identity in identities]
    bars = [size_bar(step.need / gate.GIB, budget) for step in waiting]
    status = head_status(model, budget_bytes)
    labels: Line = (Span(f"free {free:.1f}", "success"), Span(" │ ", "inactive"),
                    Span(f"taken {taken:.1f}", "suggestion"))
    place_width = max(2, len(str(len(waiting))))
    minutes_width = max(3, max((len(str(age(step.since, explanation.now))) for step in waiting), default=0))
    # All rows share these widths, including when the head's state needs more room.
    fixed = place_width + 1 + 1 + 1 + 7 + 1 + minutes_width + 1
    tail = max(terminal_width(plain([labels])), (bar_eighths(budget, budget) + 7) // 8,
               max((len(bar) + len(f" {step.need / gate.GIB:.1f}") +
                    (terminal_width(plain([status])) if index == 0 else 0)
                    for index, (step, bar) in enumerate(zip(waiting, bars))), default=0))
    show_width, unit_width = 12, 10
    room = columns - fixed - tail
    if columns > 60:
        longest_show = max((terminal_width(show) for show, _ in names), default=12)
        longest_unit = max((terminal_width(unit) for _, unit in names), default=10)
        while show_width + unit_width < room and (show_width < longest_show or unit_width < longest_unit):
            if show_width < longest_show:
                show_width += 1
            if show_width + unit_width < room and unit_width < longest_unit:
                unit_width += 1
    while show_width + unit_width > room and (show_width > 3 or unit_width > 3):
        if show_width > 3:
            show_width -= 1
        if show_width + unit_width > room and unit_width > 3:
            unit_width -= 1
    bar_column = fixed + show_width + unit_width
    budget_label = f"budget {budget:.1f}"
    lines: list[Line] = [
        (Span(f"Build queue · {clock(model, explanation.now)}",
              "warning" if isinstance(model.reading.freshness, gate.StaleReading) else "inactive"),), (),
        (Span(" " * bar_column), *labels),
        (Span(pad_right(budget_label, bar_column), "inactive"), *budget_bar(budget, free, colour=colour)), (),
        (Span(pad_left("#", place_width) + " " + pad_right(cut_tail("showrunner", show_width), show_width) + " "
              + pad_right(cut_tail("unit", unit_width), unit_width) + " " + pad_right("step", 7) + " "
              + pad_left("min", minutes_width) + " need", "inactive"),),
    ]
    priority = head_facts.priority_text(explanation.priority)
    if priority:
        lines.insert(2, (Span(priority),))
    for index, (step, (show, unit), bar) in enumerate(zip(waiting, names, bars)):
        tone = "warning" if index == 0 else "inactive"
        row: Line = (
            Span(pad_left(str(index + 1), place_width), tone),
            Span("▸", "suggestion") if you and step.session_id == you else Span(" "),
            Span(pad_right(cut_tail(show, show_width), show_width) + " "),
            Span(pad_right(cut_tail(unit, unit_width), unit_width) + " ", "suggestion"),
            Span(pad_right(cut_tail(field(step.step), 7), 7) + " "),
            Span(pad_left(str(age(step.since, explanation.now)), minutes_width) + " ", "inactive"),
            Span(bar, "warning"), Span(f" {step.need / gate.GIB:.1f}", tone),
        )
        lines.append((*row, *status) if index == 0 else row)
    return [clip(line, columns) for line in lines]
