"""The compact queue view and the checks shared with one-step cards."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache, cached_property
from typing import assert_never
from zoneinfo import ZoneInfo

from scripts.build_queue import head_facts, queue_evidence
from scripts.build_queue.queue_evidence import step_labels as step_labels
from scripts.build_queue.queue_lines import Line, Span, age, clip, cut_tail, pad_left, pad_right, plain, field
from scripts.lib.terminal_cells import terminal_width
from scripts.lint import memory_admit as gate
from scripts.message.unit_labels import UnitLabels
from scripts.build_queue.scope_activity import LiveBuildRun, ScopeProcesses


@dataclass(frozen=True)
class UnknownCpuCount:
    pass


@dataclass(frozen=True)
class HostLoad:
    load: float
    cpus: int | UnknownCpuCount


@dataclass(frozen=True)
class HostLoadUnavailable:
    pass


@dataclass(frozen=True)
class QueueModel:
    reading: gate.QueueRead
    labels: Mapping[str, UnitLabels]
    facts: head_facts.HeadFacts | gate.NoWaiter
    pace: head_facts.RecordedPace | head_facts.PaceUnavailable
    zone: ZoneInfo
    load: HostLoad | HostLoadUnavailable
    release: gate.HeadAfterEnd
    ci_memory_slice: str = ""

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
    return next((item for item in explanation.line.steps if item.waiter == step),
                gate.ReadingUnavailable("step admission checks unavailable"))


def shown_waiters(selector: str, reading: gate.QueueRead, labels: Mapping[str, UnitLabels]) -> list[int]:
    return [position for position, step in enumerate(reading.view.waiting, 1)
            if step_labels(step, labels).session == selector]


def running_matches_selector(step: gate.RunningStep, selector: str, labels: Mapping[str, UnitLabels]) -> bool:
    return (selector == step_labels(step, labels).session
            or gate.step_matches(selector, step)
            or isinstance(step.measured, gate.UnownedSliceMemory) and selector == step.measured.owner)


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
    return False, queue_evidence.pressure_text(pressure, model.zone, model.reading.explanation.policy)


def check_line(passes: bool, text: str) -> Line:
    return (Span("✓ " if passes else "✗ ", "success" if passes else "error"), Span(text))


def check_lines(checks: gate.HeadExplanation | gate.LineWaiter | gate.ReadingUnavailable,
                model: QueueModel) -> list[Line]:
    if isinstance(checks, gate.ReadingUnavailable):
        return [(Span(reason_phrase(checks, model.reading.view.waiting)),)]
    return [check_line(checks.budget.passes, queue_evidence.budget_text(checks.budget)),
            check_line(checks.free.passes, queue_evidence.free_text(checks.free))]


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


@dataclass(frozen=True)
class RunningBuild:
    step: gate.RunningStep
    used: int
    grant: int
    started_at: float
    measured: bool


def running_builds(model: QueueModel) -> list[RunningBuild]:
    """Combine a run's grant and scope evidence without another memory read."""
    rows: dict[tuple[int, str] | int, RunningBuild] = {}
    for index, running in enumerate(model.reading.explanation.running):
        step = running.step
        memory = step.measured
        if isinstance(memory, (gate.GoneMemory, gate.CompilerServiceMemory)):
            continue
        if isinstance(memory, gate.UnownedSliceMemory) and not (
                isinstance(memory.activity, ScopeProcesses) and isinstance(memory.activity.run, LiveBuildRun)):
            continue
        key = (step.run.pid, step.run.start_time) if isinstance(step.run, LiveBuildRun) else index
        started = step.run.started_at if isinstance(step.run, LiveBuildRun) else step.started_at
        if not started and isinstance(running.admitted_at, float):
            started = running.admitted_at
        previous = rows.get(key)
        used = memory.bytes if isinstance(memory, gate.MeasuredMemory) else 0
        if previous is None:
            rows[key] = RunningBuild(step, used, step.promise, started, isinstance(memory, gate.MeasuredMemory))
        else:
            chosen = step if step.promise and not previous.grant else previous.step
            rows[key] = RunningBuild(chosen, previous.used + used, previous.grant + step.promise,
                                     previous.started_at or started, previous.measured and isinstance(memory, gate.MeasuredMemory))
    return list(rows.values())


def running_bar(used: float, grant: float, budget: float) -> Line:
    solid = size_bar(used, budget)
    rest = max(0, (bar_eighths(max(used, grant), budget) + 7) // 8 - len(solid))
    return (Span(solid, "suggestion"), Span("░" * rest, "inactive"))


def started_ago(started: float, now: float) -> str:
    if not started:
        return "start time unknown"
    seconds = max(0, int(now - started))
    for unit, scale in (("day", 86400), ("hour", 3600), ("minute", 60), ("second", 1)):
        if seconds >= scale or scale == 1:
            count = seconds // scale
            return f"started {count} {unit}{'' if count == 1 else 's'} ago"
    raise AssertionError("the seconds scale always matches")


def render_view(model: QueueModel, columns: int, *, colour: bool = False, you: str = "",
                selector: str = "") -> list[Line]:
    explanation = model.reading.explanation
    budget_bytes = explanation.limits.budget
    budget = budget_bytes / gate.GIB
    taken = explanation.totals.charged / gate.GIB
    free = max(0.0, budget - taken)
    waiting = model.reading.view.waiting
    running = running_builds(model)
    bumped_unit = "" if isinstance(explanation.priority, gate.NoQueuePriority) else explanation.priority["unit"]

    def names(step: gate.WaitingStep | gate.RunningStep) -> tuple[str, str, bool]:
        identity = step_labels(step, model.labels)
        show = field(identity.showrunner) or "—"
        unit = field(identity.unit).removeprefix(show + "-")
        unit = unit or "—"
        return show, unit, bool(bumped_unit and identity.unit == bumped_unit)

    waiting_names = [names(step) for step in waiting]
    running_names = [names(row.step) for row in running]
    all_names = [*waiting_names, *running_names]
    bars = [size_bar(step.need / gate.GIB, budget) for step in waiting]
    status = head_status(model, budget_bytes)
    labels: Line = (Span(f"free {free:.1f}", "success"), Span(" │ ", "inactive"),
                    Span(f"taken {taken:.1f}", "suggestion"))
    place_width = max(3, len(str(len(waiting))))
    minutes_width = max(3, max((len(str(age(step.since, explanation.now))) for step in waiting), default=0),
                        max((len(str(age(row.started_at, explanation.now))) for row in running if row.started_at), default=0))
    step_width = max(7, min(13, max(
        max((terminal_width(row.step.step) for row in running), default=7),
        max((terminal_width(step.step) for step in waiting), default=7))))
    fixed = place_width + step_width + minutes_width + 5
    tail = max(terminal_width(plain([labels])), (bar_eighths(budget, budget) + 7) // 8,
               max((len(bar) + len(f" {step.need / gate.GIB:.1f}") +
                    (terminal_width(plain([status])) if index == 0 else 0)
                    for index, (step, bar) in enumerate(zip(waiting, bars))), default=0))
    show_width, unit_width = 12, 10
    room = columns - fixed - tail
    if columns > 60:
        longest_show = max((terminal_width(show) for show, _, _ in all_names), default=12)
        longest_unit = max((terminal_width(unit) for _, unit, _ in all_names), default=10)
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
        (Span(f"Build queue · {clock(model, explanation.now)}", "inactive"),), (),
        (Span(" " * bar_column), *labels),
        (Span(pad_right(budget_label, bar_column), "inactive"), *budget_bar(budget, free, colour=colour)), (),
    ]
    priority = head_facts.priority_text(explanation.priority)
    if priority:
        lines.insert(2, (Span(priority),))

    def heading() -> Line:
        place, memory = "#", "GiB: reserve/use; need"
        return (Span(pad_left(place, place_width) + " " + pad_right(cut_tail("showrunner", show_width), show_width) + " "
                     + pad_right(cut_tail("unit", unit_width), unit_width) + " " + pad_right("step", step_width) + " "
                     + pad_left("min", minutes_width) + " " + memory, "inactive"),)

    @cache
    def column(text: str, width: int) -> str:
        if text.isascii():
            return (text if len(text) <= width else text[:max(0, width - 1)] + "…").ljust(width) + " "
        return pad_right(cut_tail(text, width), width) + " "

    def identity_columns(place: str, step: str, pair: tuple[str, str, bool], minutes: str, marker: str = " ") -> Line:
        show, unit, bumped = pair
        unit_spans = ((Span("●", "warning"), Span(column(unit, unit_width - 1), "suggestion"))
                      if bumped else (Span(column(unit, unit_width), "suggestion"),))
        return (Span(pad_left(place, place_width), "warning" if place == "1" else "inactive"),
                Span(marker, "suggestion"),
                Span(column(show, show_width)),
                *unit_spans,
                Span(column(field(step), step_width)),
                Span(pad_left(minutes, minutes_width) + " ", "inactive"))

    lines.append(heading())
    for row, pair in zip(running, running_names):
        used = row.used / gate.GIB
        grant = row.grant / gate.GIB
        text = f" {grant:.1f} / {used:.1f}" if row.measured else f" {grant:.1f} / —"
        bar = running_bar(used, grant, budget)
        if bar_column + terminal_width(plain([bar])) + len(text) > columns:
            text = f" {grant:.1f}"
        bar = clip(bar, max(0, columns - bar_column - len(text)))
        minutes = str(age(row.started_at, explanation.now)) if row.started_at else "—"
        lines.append((*identity_columns("run", row.step.step, pair, minutes,
                                        "▸" if (you and row.step.session_id == you
                                                or selector and running_matches_selector(row.step, selector, model.labels)) else " "), *bar, Span(text)))
    for index, (step, pair, bar) in enumerate(zip(waiting, waiting_names, bars)):
        row = (*identity_columns(str(index + 1), step.step, pair, str(age(step.since, explanation.now)),
                                "▸" if you and step.session_id == you else " "),
               Span(bar, "warning"), Span(f" {step.need / gate.GIB:.1f}", "warning" if index == 0 else "inactive"))
        lines.append((*row, *status) if index == 0 else row)
    head = explanation.head
    if isinstance(head, gate.HeadExplanation) and head.decision.state == "hold":
        lines.append((Span(f"Head: free {head.free.available / gate.GIB:.1f} GiB; "
            + f"reserve {head.free.reserve / gate.GIB:.1f} GiB; need {head.free.need / gate.GIB:.1f} GiB"),))
    ended: list[tuple[gate.RunningStep, gate.UnownedSliceMemory, ScopeProcesses]] = []
    for step in model.reading.view.running:
        if not isinstance(step.measured, gate.UnownedSliceMemory):
            continue
        activity = step.measured.activity
        if model.ci_memory_slice and step.measured.owner == model.ci_memory_slice:
            continue
        if isinstance(activity, ScopeProcesses) and not isinstance(activity.run, LiveBuildRun):
            ended.append((step, step.measured, activity))
    if ended:
        count = sum(activity.count for _, _, activity in ended)
        total = sum(memory.bytes for _, memory, _ in ended)
        noun = "process" if count == 1 else "processes"
        lines.extend([(), (Span(f"Still running after their build ended ({count} {noun}, {total / gate.GIB:.1f} GiB)",
                               bold=True),),
                      (Span("These processes outlive their builds; ask the named owner to stop them if no longer needed."),)])
        for step, memory, activity in ended:
            identity = model.labels.get(activity.session_id)
            owner = field(identity.session if identity else "") or field(activity.session_name) or "unlabelled session"
            minutes = str(age(activity.started_at, explanation.now)) if activity.started_at else "—"
            lines.append((Span(f"   {owner} · {field(activity.program)} · {minutes} min · {memory.bytes / gate.GIB:.1f} GiB"),))
    return [clip(line, columns) for line in lines]
