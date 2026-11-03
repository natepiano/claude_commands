"""One waiting step's place, order and admission checks."""
from __future__ import annotations

from collections.abc import Sequence

from scripts.build_queue import head_facts
from scripts.build_queue.queue_lines import Line, Span, age, wrap
from scripts.build_queue.queue_view import (QueueModel, check_line, check_lines, clock, estimate_minutes,
    has_move_priority, is_moved, name_spans, name_text, shown_waiters, reason_phrase, start_text, step_checks, step_labels, waiting_moves)
from scripts.lint import memory_admit as gate


def select_waiters(model: QueueModel, selector: str) -> list[int]:
    shown = shown_waiters(selector, model.reading, model.labels)
    if shown:
        return shown
    return gate.match_waiters(selector, model.reading.view)


def ordinal(number: int) -> str:
    return str(number) + ("th" if 10 <= number % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th"))


def order_check(model: QueueModel, position: int) -> Line:
    reason = model.reading.view.waiting[position - 1].reason
    if isinstance(reason, gate.BehindOlderFit):
        return check_line(False, "queue order      " + reason_phrase(reason, model.reading.view.waiting))
    if isinstance(reason, gate.BehindHead) and isinstance(model.facts, head_facts.HeadFacts):
        return check_line(False, "#1's turn      " + head_facts.order_text(model.facts, model.zone))
    return check_line(True, "queue order")


def render_card(model: QueueModel, matches: Sequence[int], columns: int) -> list[Line]:
    lines: list[Line] = []

    def add(line: Line = (), indent: int = 2) -> None:
        lines.extend(wrap(line, columns, indent))

    waiting = model.reading.view.waiting
    for card, position in enumerate(matches):
        step = waiting[position - 1]
        if card:
            add()
        add((Span(f"#{position} of {len(waiting)} · ", bold=True), *name_spans(step, model.labels),
             Span(f" · {step.step} · {step.need / gate.GIB:.1f} GiB", bold=True)))
        add((Span(f"Folder {step.folder}. Waited {age(step.since, model.reading.explanation.now)} min (since {clock(model, step.since)}). "
                  + (f"Likely start {start_text(model, position, clock_time=True)}." if estimate_minutes(model, position) else "Likely start —.")),))
        priority = head_facts.priority_text(model.reading.explanation.priority)
        if priority:
            add((Span(priority),))
        add()
        checks = step_checks(model, position)
        if (isinstance(step.reason, gate.ReadyForAdmission)
                or isinstance(checks, gate.HeadExplanation) and checks.decision.state == "admit"):
            add((Span("Next? Yes. It starts at the next check.", bold=True),))
        elif position == 1:
            add((Span("Next? Yes, it is first. It starts when the checks below pass.", bold=True),))
        else:
            moved = sum(has_move_priority(model, place) for place in range(1, position))
            add((Span(f"Next? No. {position - 1} steps are ahead of it: {moved} moved up, "
                      + f"{position - 1 - moved} joined earlier.", bold=True),))
        if position > 1:
            for place in range(1, min(position, 6)):
                ahead = waiting[place - 1]
                records = waiting_moves(model, place)
                if records:
                    detail = head_facts.move_text(records, model.zone, model.labels)
                else:
                    later = ahead.since > step.since
                    minutes = age(step.since, ahead.since) if later else age(ahead.since, step.since)
                    detail = f"joined {clock(model, ahead.since)}, {minutes} min {'after' if later else 'before'} it"
                add((Span(f"  #{place} "), Span("↑  " if is_moved(model, place) else "   "),
                     *name_spans(ahead, model.labels), Span(f" {ahead.step}  {detail}")), 6)
            if position > 6:
                add((Span(f"  and {position - 6} more"),))
            joined = 1 + sum(other.since < step.since for other in waiting)
            add((Span("  Moved-up steps go first, then the longest wait. It joined "
                      + f"{ordinal(joined)} of the steps waiting now."),))
        add()
        add((Span("Waits on", bold=True),))
        if not isinstance(checks, gate.ReadingUnavailable):
            add((Span("  "), *order_check(model, position)), 4)
        if isinstance(step.reason, gate.EstimateExpired):
            add((Span("  "), *check_line(False, "estimate       expired at "
                + clock(model, step.reason.estimated_at) + "; refreshes on its next poll")), 4)
        for check in check_lines(checks, model, pressure_first=False):
            add((Span("  "), *check), 4)
        if estimate_minutes(model, position):
            add()
            pace = model.pace
            if isinstance(pace, head_facts.RecordedPace):
                add((Span(f"Likely start {start_text(model, position, clock_time=True)}: {ordinal(position)} in line "
                          + f"at {pace.steps} steps an hour, an estimate."),))
            add((Span(f"  Each step moved up ahead of it adds about {estimate_minutes(model, 1)} min."),))
    return lines


def render_selection(model: QueueModel, selector: str, columns: int) -> list[Line]:
    matches = select_waiters(model, selector)
    lines = render_card(model, matches, columns)
    for running in model.reading.explanation.running:
        step = running.step
        if selector != step_labels(step, model.labels).session and not gate.step_matches(selector, step):
            continue
        if lines:
            lines.append(())
        started = ("started —" if isinstance(running.admitted_at, gate.AdmissionTimeNotRecorded) else
                   f"started {clock(model, running.admitted_at)} ({age(running.admitted_at, model.reading.explanation.now)} min ago)")
        using = f"{running.measured / gate.GIB:.1f} GiB" if isinstance(step.measured, gate.MeasuredMemory) else "—"
        lines.extend(wrap((Span(f"{name_text(step, model)} {step.step} runs now: {started}, using {using}, "
                                + f"held {running.charged / gate.GIB:.1f} GiB."),), columns))
    return lines or wrap((Span(f"No waiting or running step matches {selector}."),), columns)
