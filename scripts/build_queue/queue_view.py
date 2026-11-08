"""The compact queue view and the checks shared with one-step cards."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field as dataclass_field
from functools import cache, cached_property
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.buildlog import store
from scripts.build_queue import head_facts, queue_evidence, queue_lines
from scripts.build_queue import admission_protocol as wire, cloud_progress, progress_profile
from scripts.build_queue.admission_records import (reason_phrase as reason_phrase, behind_place as behind_place,
    eligible_head_position as eligible_head_position)
from scripts.build_queue.queue_evidence import step_labels as step_labels
from scripts.build_queue.queue_lines import Line, Span, age, clip, cut_tail, plain, field, wrap
from scripts.lib.terminal_cells import pad_left, pad_right, terminal_width
from scripts.lib import conf
from scripts.lib.build_steps import StepNames, read_step_names
from scripts.lint import memory_admit as gate
from scripts.message.unit_labels import UnitLabels
from scripts.message import unit_labels
from scripts.build_queue.scope_activity import LiveBuildRun, NotServiceSlice, ScopeProcesses


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
    facts: head_facts.HeadFacts | gate.TokenWaitersOnly | gate.NoWaiter
    pace: head_facts.RecordedPace | head_facts.PaceUnavailable
    zone: ZoneInfo
    load: HostLoad | HostLoadUnavailable
    release: gate.HeadAfterEnd
    ci_memory_slice: str = ""
    names: StepNames = dataclass_field(default_factory=read_step_names)

    @cached_property
    def moves(self) -> head_facts.QueueMoves:
        return head_facts.queue_moves(self.reading, self.facts)

    @cached_property
    def cloud_labels(self) -> Mapping[str, UnitLabels]:
        missing = {row.identity.session_id for row in cloud_rows(self)
                   if row.identity.session_id and row.identity.session_id not in self.labels}
        return {**unit_labels.unit_labels_for(missing, os.environ), **self.labels} if missing else self.labels


def same_waiter(left: gate.WaitingStep, right: gate.WaitingStep) -> bool:
    """Match a saved decision to its row even when display fields change."""
    return left.pid == right.pid and left.since == right.since


def step_checks(model: QueueModel, position: int) -> gate.HeadExplanation | gate.LineWaiter | gate.ReadingUnavailable:
    explanation = model.reading.explanation
    if isinstance(explanation.host, gate.ReadingUnavailable):
        return explanation.host
    step = model.reading.view.waiting[position - 1]
    if isinstance(step.reason, gate.ReadingUnavailable):
        return step.reason
    if isinstance(explanation.head, gate.HeadExplanation) and same_waiter(explanation.head.waiter, step):
        return explanation.head
    return next((item for item in explanation.line.steps if same_waiter(item.waiter, step)),
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
    return isinstance(gate.waiter_priority(model.reading.ordered[position - 1][1]), gate.PriorityUnit)


def pressure_check(pressure: gate.Pressure, model: QueueModel) -> tuple[bool, str]:
    if isinstance(pressure, gate.NoPressureHold):
        return True, "memory pressure: no hold"
    return False, queue_evidence.pressure_text(pressure, model.reading.explanation.policy)


def check_line(passes: bool, text: str) -> Line:
    return (Span("✓ " if passes else "✗ ", "success" if passes else "error"), Span(text))


def check_lines(checks: gate.HeadExplanation | gate.LineWaiter | gate.ReadingUnavailable,
                model: QueueModel) -> list[Line]:
    if isinstance(checks, gate.ReadingUnavailable):
        return [(Span(reason_phrase(checks, model.reading.view.waiting)),)]
    return [check_line(checks.room.passes, queue_evidence.room_check_text(checks.room))]








def bar_eighths(gib: float, cap: float) -> int:
    """Round to the nearest eighth on the shared one-cell-per-GiB scale."""
    return int(max(0.0, min(gib, cap)) * 8 + 0.5)


def size_bar(gib: float, cap: float) -> str:
    eighths = bar_eighths(gib, cap)
    whole, fraction = divmod(eighths, 8)
    return "█" * whole + ("▏▎▍▌▋▊▉"[fraction - 1] if fraction else "")


def head_status(model: QueueModel) -> Line:
    head = model.reading.explanation.head
    if not isinstance(head, gate.HeadExplanation):
        return ()
    result: Line = (Span(" ↑", "warning" if head.has_priority else "success"),)
    cause = head.cause
    if isinstance(cause, gate.MemoryPressure):
        return (*result, Span(" pressure", "error"))
    if isinstance(cause, gate.PressureUnavailable):
        return (*result, Span(" pressure unreadable", "error"))
    if isinstance(cause, gate.StartSpacingWait):
        return (*result, Span(" spacing", "inactive"))
    if isinstance(cause, gate.EstimateExpired):
        return (*result, Span(" estimate expired", "error"))
    if isinstance(cause, gate.ReadingUnavailable):
        return (*result, Span(" " + cause.reason, "error"))
    if isinstance(cause, gate.NeedsMemory):
        return (*result, Span(" low room", "error"))
    if isinstance(cause, gate.BehindHead):
        return (*result, Span(" queue order", "inactive"))
    return result


def cap_bar(room: gate.MemoryRoom | gate.ReadingUnavailable, width: int, *, colour: bool) -> Line:
    """Draw captured use, keep-aside, room, safety, and other programs."""
    if not isinstance(room, gate.MemoryRoom) or not isinstance(room.cap, int):
        return (Span("cap unavailable", "inactive"),)
    total = room.total if isinstance(room.total, int) else max(1, room.cap + room.safety)
    cells = max(1, min(width, (total + gate.GIB - 1) // gate.GIB))
    scale = total / cells
    cap_at = min(cells - 1, max(0, int(room.cap / scale)))
    used = room.cap_use
    kept_end = min(room.cap, used + room.kept)
    result: list[Span] = []
    for cell in range(cells):
        point = (cell + 0.5) * scale
        if cell == cap_at:
            result.append(Span("│", "error"))
        elif point < min(used, room.cap):
            result.append(Span("█", "suggestion"))
        elif point < kept_end:
            result.append(Span("░", "warning"))
        elif point < room.cap:
            result.append(Span("█" if colour else "·", "success"))
        elif point < room.cap + room.safety:
            result.append(Span("█" if colour else "▒", "warning"))
        else:
            result.append(Span("█", "inactive"))
    return tuple(result)


@dataclass(frozen=True)
class RunningBuild:
    step: gate.RunningStep
    used: int
    kept: int
    started_at: float
    measured: bool
    swap_bytes: int = 0


def anonymous_measurement(use: gate.AnonymousRunUse | gate.RunUseUnavailable) -> tuple[int, bool]:
    """Show the captured amount and whether its reading is available."""
    return (use.bytes, True) if isinstance(use, gate.AnonymousRunUse) else (0, False)


def running_builds(model: QueueModel) -> list[RunningBuild]:
    """Combine captured keep-asides and scope evidence for each run."""
    explanation = model.reading.explanation
    captured = explanation.room
    holds = {hold.memory.owner: hold for hold in captured.runs} if isinstance(captured, gate.MemoryRoom) else {}
    rows: dict[str, RunningBuild] = {}
    for index, running in enumerate(model.reading.explanation.running):
        step = running.step
        memory = step.measured
        if isinstance(memory, (gate.GoneMemory, gate.CompilerServiceMemory, gate.UnownedSliceMemory)):
            continue
        key = gate.run_owner(step.run.pid, step.run.start_time) if isinstance(step.run, LiveBuildRun) else str(index)
        started = step.run.started_at if isinstance(step.run, LiveBuildRun) else step.started_at
        if not started and isinstance(running.admitted_at, float):
            started = running.admitted_at
        previous = rows.get(key)
        used = (memory.anonymous.bytes if isinstance(memory, gate.MeasuredMemory)
                and isinstance(memory.anonymous, gate.AnonymousRunUse) else 0)
        measured = isinstance(memory, gate.MeasuredMemory) and isinstance(memory.anonymous, gate.AnonymousRunUse)
        hold = holds.get(running.owner)
        if hold is not None and not isinstance(step.run, LiveBuildRun):
            used, measured = anonymous_measurement(hold.memory.use)
        kept = hold.bytes if hold is not None and not isinstance(step.run, LiveBuildRun) else 0
        if previous is None:
            rows[key] = RunningBuild(step, used, kept, started, measured,
                                      memory.swap_bytes if isinstance(memory, gate.MeasuredMemory) else 0)
        else:
            chosen = step if step.promise and not previous.step.promise else previous.step
            rows[key] = RunningBuild(chosen, previous.used + used, previous.kept + kept,
                                     previous.started_at or started, previous.measured and measured,
                                     previous.swap_bytes + (memory.swap_bytes if isinstance(memory, gate.MeasuredMemory) else 0))
    if isinstance(captured, gate.MemoryRoom):
        for key, row in rows.items():
            if not isinstance(row.step.run, LiveBuildRun):
                continue
            hold = holds.get(gate.run_owner(row.step.run.pid, row.step.run.start_time))
            if hold is not None:
                used, measured = anonymous_measurement(hold.memory.use)
                rows[key] = RunningBuild(row.step, used, hold.bytes, row.started_at, measured, row.swap_bytes)
        owners = {gate.run_owner(row.step.run.pid, row.step.run.start_time) for row in rows.values()
                  if isinstance(row.step.run, LiveBuildRun)}
        owners.update(entry.owner for entry in explanation.running
                      if not isinstance(entry.step.measured, gate.UnownedSliceMemory))
        for hold in captured.runs:
            run = hold.memory.run
            if hold.memory.owner in owners or not isinstance(run, LiveBuildRun):
                continue
            used, measured = anonymous_measurement(hold.memory.use)
            step = gate.RunningStep("", "", "between steps", "", 0, gate.MeasuredMemory(used), pid=run.pid, run=run)
            rows[gate.run_owner(run.pid, run.start_time)] = RunningBuild(step, used, hold.bytes, 0.0, measured)
    return list(rows.values())


def running_bar(used: float, grant: float, cap: float) -> Line:
    solid = size_bar(used, cap)
    rest = max(0, (bar_eighths(max(used, grant), cap) + 7) // 8 - len(solid))
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


def leftover_age(minutes: int) -> str:
    if minutes < 120:
        return f"{minutes} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h"
    return f"{minutes // (24 * 60)} d"


@dataclass(frozen=True)
class CloudPushInterval:
    seconds: float


@dataclass(frozen=True)
class CloudPushIntervalUnavailable:
    reason: str


def cloud_push_interval() -> CloudPushInterval | CloudPushIntervalUnavailable:
    try:
        return CloudPushInterval(conf.read("cloud_gate").number("log_push_s"))
    except conf.Unusable as error:
        return CloudPushIntervalUnavailable(str(error))


def cloud_rows(model: QueueModel) -> tuple[wire.CloudRow, ...]:
    return model.reading.cloud if isinstance(model.reading, wire.CloudQueueRead) else ()


def cloud_matches(row: wire.CloudRow, selector: str, labels: Mapping[str, UnitLabels]) -> bool:
    identity = row.identity
    label = labels.get(identity.session_id)
    return selector in (str(row.owner.pid), identity.session_id, identity.name, identity.unit,
                        identity.folder, Path(identity.folder).name, label.session if label else "")


def cloud_lines(model: QueueModel, columns: int, rows: tuple[wire.CloudRow, ...] = ()) -> list[Line]:
    reading = model.reading
    all_rows = cloud_rows(model)
    if not all_rows or not isinstance(reading, wire.CloudQueueRead):
        return []
    used = sum(isinstance(row.wait, gate.ReadyForAdmission) for row in all_rows)
    lines: list[Line] = [(Span(f"Cloud · {used} of {reading.cloud_slots} slots", bold=True),)]
    interval = cloud_push_interval()
    now = reading.explanation.now
    for row in rows or all_rows:
        identity, progress = row.identity, row.progress
        unit = gate.queue_unit(identity.unit, identity.session_id, model.cloud_labels) or "—"
        if isinstance(row.wait, wire.CloudSlotWait):
            state = "waiting; " + row.wait.text
        elif isinstance(progress, cloud_progress.CloudProgress):
            state = progress.state
        else:
            state = "progress " + progress.reason
        counter = row.counter
        modules = (f"{counter.done} of {counter.total} modules"
                   if isinstance(counter, progress_profile.ProgressCounter) else "modules unknown")
        size = f"{progress.size_vcpus} vCPUs" if isinstance(progress, cloud_progress.CloudProgress) else "vCPUs unknown"
        elapsed = leftover_age(age(row.started_at or row.since, now))
        text = f"{field(unit)} · {field(identity.step)} · {state} · {modules} · {size} · elapsed {elapsed}"
        if isinstance(progress, cloud_progress.CloudProgress):
            if progress.failed_modules:
                text += f" · {progress.failed_modules} failed modules"
            if isinstance(interval, CloudPushInterval):
                stale_after = reading.cloud_gate_stale_pushes * interval.seconds
                if now - progress.updated_at >= stale_after:
                    update_age = started_ago(progress.updated_at, now).removeprefix("started ").removesuffix(" ago")
                    text += f" · no update for {update_age}"
        lines.extend(wrap((Span("   " + text),), columns, indent=3))
    return lines


def render_view(model: QueueModel, columns: int, *, colour: bool = False, you: str = "",
                selector: str = "") -> list[Line]:
    explanation = model.reading.explanation
    head = explanation.head
    room = explanation.room
    cap_bytes = room.cap if isinstance(room, gate.MemoryRoom) and isinstance(room.cap, int) else 0
    cap = cap_bytes / gate.GIB
    waiting = model.reading.view.waiting
    running = running_builds(model)
    bumped_unit = "" if isinstance(explanation.priority, gate.NoQueuePriority) else explanation.priority["unit"]

    def names(step: gate.WaitingStep | gate.RunningStep) -> tuple[str, str, bool]:
        identity = step_labels(step, model.labels)
        show = field(step.session_name if not step.session_id and step.unit else identity.showrunner) or "—"
        unit = field(identity.unit).removeprefix(show + "-")
        unit = unit or "—"
        return show, unit, bool(bumped_unit and identity.unit == bumped_unit)

    waiting_names = [names(step) for step in waiting]
    running_names = [names(row.step) for row in running]
    all_names = [*waiting_names, *running_names]
    bars = [size_bar(step.need / gate.GIB, cap) for step in waiting]
    status = head_status(model)
    waiting_status = [
        (Span(" " + reason_phrase(step.reason, waiting), "error"),)
        if isinstance(step.reason, gate.ReadingUnavailable) else
        status if isinstance(head, gate.HeadExplanation) and same_waiter(head.waiter, step) else ()
        for step in waiting]
    place_width = max(3, len(str(len(waiting))))
    minutes_width = max(3, max((len(str(age(step.since, explanation.now))) for step in waiting), default=0),
                        max((len(str(age(row.started_at, explanation.now))) for row in running if row.started_at), default=0))
    step_width = max(7, min(max(13, columns // 3), max(
        max((terminal_width(queue_lines.step_text(row.step, model.names)) for row in running), default=7),
        max((terminal_width(queue_lines.step_text(step, model.names)) for step in waiting), default=7))))
    pids = [queue_evidence.step_pid_text(step) for step in waiting]
    running_pids = [queue_evidence.step_pid_text(row.step) for row in running]
    pid_width = max((len(text) for text in [*pids, *running_pids]), default=5)
    # Reserve the text widths before the pid takes cells from the bars.
    fixed = place_width + step_width + minutes_width + 5
    show_width, unit_width = 12, 7
    # Keep the previous name widths with a fixed memory area of 24 cells.
    # The cap and the pid change the bars, not the name widths.
    name_room = columns - fixed - 24
    if columns > 60:
        longest_show = max((terminal_width(show) for show, _, _ in all_names), default=12)
        longest_unit = max((terminal_width(unit) for _, unit, _ in all_names), default=10)
        while show_width + unit_width < name_room and (show_width < longest_show or unit_width < longest_unit):
            if show_width < longest_show:
                show_width += 1
            if show_width + unit_width < name_room and unit_width < longest_unit:
                unit_width += 1
    while show_width + unit_width > name_room and (show_width > 3 or unit_width > 7):
        if show_width > 3:
            show_width -= 1
        if show_width + unit_width > name_room and unit_width > 7:
            unit_width -= 1
    machine = field(store.host_name()) if running else ""
    machine_width = max(len("machine"), terminal_width(machine)) if machine else 0
    names_fit = all(terminal_width(show) <= show_width and terminal_width(unit) <= unit_width
                    for show, unit, _ in all_names)
    if not names_fit or show_width + unit_width + machine_width + 1 > name_room:
        machine_width = 0
    machine_cells = machine_width + 1 if machine_width else 0
    bar_column = fixed + show_width + unit_width + pid_width + 1 + machine_cells
    running_text = [
        (f" {row.kept / gate.GIB:.1f} / {row.used / gate.GIB:.1f}" if row.measured
         else f" {row.kept / gate.GIB:.1f} / —")
        + (f"; {row.swap_bytes / gate.GIB:.1f} GiB swapped" if row.swap_bytes else "")
        for row in running]
    number_width = max(
        max((terminal_width(text) for text in running_text), default=0),
        max((len(f" {step.need / gate.GIB:.1f}") + terminal_width(plain([suffix]))
             for step, suffix in zip(waiting, waiting_status)), default=0))
    bar_width = max(0, columns - bar_column - number_width)
    cap_label = f"cap {cap:.1f}" if isinstance(room, gate.MemoryRoom) and isinstance(room.cap, int) else "cap unavailable"
    memory_bar = cap_bar(room, max(1, columns - bar_column), colour=colour)
    cap_cell = next((index for index, span in enumerate(memory_bar) if span.text == "│"), 0)
    lines: list[Line] = [
        (Span(f"Build queue · {clock(model, explanation.now)}", "inactive"),), (),
        (Span(" " * max(0, min(columns - len(cap_label), bar_column + cap_cell))),
         Span(cap_label, "error")),
        (Span(" " * bar_column), *memory_bar), (),
    ]
    priority = head_facts.priority_text(explanation.priority)
    if priority:
        lines.insert(2, (Span(priority),))

    def heading() -> Line:
        place, memory = "#", "GiB: kept/use; need"
        return (Span(pad_left(place, place_width) + " " + pad_right(cut_tail("showrunner", show_width), show_width) + " "
                     + pad_right(cut_tail("unit", unit_width), unit_width) + " " + pad_right("step", step_width) + " "
                     + pad_left("min", minutes_width) + " "
                     + (pad_right("machine", machine_width) + " " if machine_width else "") + pad_right("pid", pid_width) + " " + memory, "inactive"),)

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

    def append_row(row: Line) -> None:
        if terminal_width(plain([row])) <= columns:
            lines.append(row)
            return
        first, *continued = wrap(row, max(1, columns - 2), indent=0)
        lines.append(first)
        lines.extend((Span("· ", "inactive"), *line) for line in continued)

    lines.extend(wrap((Span(queue_evidence.room_text(explanation.room), "inactive"),), columns))
    lines.append(heading())
    for index, (row, pair, text) in enumerate(zip(running, running_names, running_text)):
        used = row.used / gate.GIB
        held_end = used + row.kept / gate.GIB
        bar = running_bar(min(used, bar_width), min(held_end, bar_width), cap)
        minutes = str(age(row.started_at, explanation.now)) if row.started_at else "—"
        append_row((*identity_columns("run", queue_lines.step_text(row.step, model.names), pair, minutes,
                                        "▸" if (you and row.step.session_id == you
                                                or selector and running_matches_selector(row.step, selector, model.labels)) else " "),
                      *((Span(column(machine, machine_width), "inactive"),) if machine_width else ()),
                      Span(column(running_pids[index], pid_width), "inactive"),
                      *bar, Span(text)))
    for index, (step, pair, bar, suffix) in enumerate(zip(waiting, waiting_names, bars, waiting_status)):
        row = (*identity_columns(str(index + 1), queue_lines.step_text(step, model.names), pair, str(age(step.since, explanation.now)),
                                "▸" if you and step.session_id == you else " "),
               *((Span(" " * machine_cells),) if machine_width else ()),
               Span(column(pids[index], pid_width), "inactive"), Span(bar[:bar_width], "warning"), Span(f" {step.need / gate.GIB:.1f}", "warning" if isinstance(head, gate.HeadExplanation) and same_waiter(head.waiter, step) else "inactive"))
        append_row((*row, *suffix))
    if isinstance(head, gate.TokenWaitersOnly):
        lines.extend([(), (Span("All waiting steps are waiting for the cargo token."),)])
    if isinstance(head, gate.HeadExplanation) and head.decision.state == "hold":
        lines.append((Span(f"Head: free {head.free.available / gate.GIB:.1f} GiB; "
            + f"reserve {head.free.reserve / gate.GIB:.1f} GiB; need {head.free.need / gate.GIB:.1f} GiB"),))
    if isinstance(head, gate.HeadExplanation) and isinstance(head.cause, (gate.PressureUnavailable, gate.ReadingUnavailable)):
        append_row((Span("Head: " + head.cause.reason, "error"),))
    services = [step.measured for step in model.reading.view.running
                if isinstance(step.measured, gate.CompilerServiceMemory)
                or isinstance(step.measured, gate.UnownedSliceMemory)
                and not isinstance(step.measured.job_state, NotServiceSlice)]
    if services:
        lines.extend([(), (Span("Services", bold=True),)])
        for memory in services:
            append_row((Span("   " + queue_evidence.service_text(memory, explanation.now)),))
    ended: list[tuple[gate.RunningStep, gate.UnownedSliceMemory, ScopeProcesses]] = []
    for step in model.reading.view.running:
        if not isinstance(step.measured, gate.UnownedSliceMemory):
            continue
        activity = step.measured.activity
        if not isinstance(step.measured.job_state, NotServiceSlice):
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
            elapsed = leftover_age(age(activity.started_at, explanation.now)) if activity.started_at else "—"
            lines.append((Span(f"   {queue_evidence.step_pid_text(step)} · {owner} · {field(activity.program)} · {elapsed} · {memory.bytes / gate.GIB:.1f} GiB"
                              + (f"; {memory.swap_bytes / gate.GIB:.1f} GiB swapped" if memory.swap_bytes else "")),))
            for member in activity.members[1:]:
                lines.append((Span(f"   pid {member.pid} · {field(member.program)} · {owner}"),))
    cloud = cloud_lines(model, columns)
    if cloud:
        lines.extend([(), *cloud])
    return [clip(line, columns) for line in lines]
