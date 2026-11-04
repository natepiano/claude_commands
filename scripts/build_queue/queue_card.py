"""One waiting step's place, order and admission checks."""
from __future__ import annotations

from collections.abc import Sequence

from scripts.build_queue import head_facts, queue_evidence
from scripts.build_queue.queue_lines import Line, Span, age, wrap
from scripts.build_queue.queue_view import (QueueModel, check_line, check_lines, clock, estimate_minutes,
    has_move_priority, is_moved, name_spans, name_text, pressure_check, shown_waiters, reason_phrase, running_matches_selector, start_text, step_checks, waiting_moves, HostLoad)
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
    if isinstance(reason, gate.BehindHead):
        return check_line(False, "#1's turn")
    return check_line(True, "queue order")


def _waiting_cards(model: QueueModel, matches: Sequence[int], columns: int) -> list[Line]:
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
        add((Span(f"Folder {step.folder}. Queue arrival {clock(model, step.since)}; waiting {age(step.since, model.reading.explanation.now)} min. "
                  + (f"Likely start {start_text(model, position, clock_time=True)}." if estimate_minutes(model, position) else "Likely start —.")),))
        add()
        checks = step_checks(model, position)
        if (isinstance(step.reason, gate.ReadyForAdmission)
                or isinstance(checks, gate.HeadExplanation) and checks.decision.state == "admit"):
            add((Span("Next? Yes. It starts at the next check.", bold=True),))
        elif position == 1:
            add((Span("Next? Yes, it is first. It starts when the checks below pass.", bold=True),))
        else:
            moved = sum(has_move_priority(model, place) for place in range(1, position))
            kept = sum(keeps_run_place(model, place) and not has_move_priority(model, place)
                       for place in range(1, position))
            causes = f"{moved} moved up, "
            if kept:
                causes += f"{kept} keep their run's place, "
            add((Span(f"Next? No. {position - 1} steps are ahead of it: {causes}"
                      + f"{position - 1 - moved - kept} joined earlier.", bold=True),))
        if position > 1:
            for place in range(1, min(position, 6)):
                ahead = waiting[place - 1]
                records = waiting_moves(model, place)
                if records:
                    detail = head_facts.move_text(records, model.zone, model.labels)
                elif keeps_run_place(model, place):
                    detail = "keeps its run's place"
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
        # Frees when shows the head's expired estimate.
        if position != 1 and isinstance(step.reason, gate.EstimateExpired):
            add((Span("  "), *check_line(False, "estimate       "
                + queue_evidence.estimate_refresh_text(step.reason, model.zone))), 4)
        for check in check_lines(checks, model):
            add((Span("  "), *check), 4)
        if estimate_minutes(model, position):
            add()
            pace = model.pace
            if isinstance(pace, head_facts.RecordedPace):
                add((Span(f"Likely start {start_text(model, position, clock_time=True)}: {ordinal(position)} in line "
                          + f"at {pace.steps} steps an hour, an estimate."),))
            add((Span(f"  Each step moved up ahead of it adds about {estimate_minutes(model, 1)} min."),))
    return lines


def _recorded_evidence(model: QueueModel, head_card: bool, columns: int,
                       selected_running: Sequence[gate.ExplainedRunningStep]) -> list[Line]:
    """Show shared evidence once after the selected steps."""
    explanation = model.reading.explanation
    lines: list[Line] = []

    def add(text: str = "") -> None:
        lines.extend(wrap((Span(text),), columns))

    host = explanation.host
    if isinstance(host, gate.HostAvailable):
        total = queue_evidence.gib(host.total) if isinstance(host.total, int) else "unknown"
        add(f"Host: available RAM {queue_evidence.gib(host.available)}; total RAM {total}")
        lines.extend(wrap(check_line(*pressure_check(host.pressure, model)), columns))
    else:
        add("Host memory: unavailable")
    if isinstance(model.load, HostLoad):
        cpus = model.load.cpus if isinstance(model.load.cpus, int) else "unknown"
        add(f"Host load: {model.load.load:.2f} (1 min); CPU count: {cpus}")
    else:
        add("Host load: unavailable")
    add("Running:")
    running_steps = page_running_steps(model)
    for running in running_steps:
        selected = running in selected_running
        if isinstance(running.step.measured, gate.UnownedSliceMemory):
            continue
        prefix = "Selected running step: " if selected else ""
        add(prefix + queue_evidence.running_text(running, explanation, model.labels, model.zone))
    if not running_steps:
        add("None.")
    add(f"Total current use: {queue_evidence.gib(explanation.totals.measured)}")
    head = explanation.head
    if isinstance(head, gate.NoWaiter):
        add("Nothing waits.")
        return lines
    add()
    if not head_card:
        add(f"First in line: {name_text(head.waiter, model)} {head.waiter.step}; "
            + f"queue arrival {clock(model, head.waiter.since)}; "
            + f"waiting {age(head.waiter.since, explanation.now)} min; "
            + f"need {queue_evidence.gib(head.waiter.need)}")
    add("Need source: " + queue_evidence.need_text(head.need_source,
        budget=explanation.limits.budget, zone=model.zone))
    add(queue_evidence.need_details(head.need_source))
    if isinstance(model.facts, head_facts.HeadFacts):
        facts = model.facts
        add(head_facts.bump_text(facts, model.zone))
        for text in head_facts.bump_lines(facts, model.zone, model.labels):
            add(text)
        add("Order rule: " + head_facts.order_text(facts, model.zone))
        for text in head_facts.cargo_token_lines(facts, model.zone, model.labels):
            add(text)
    else:
        add("Order history: unavailable")
        add("Cargo token waits: unknown")
    if not head_card:
        for check in check_lines(step_checks(model, 1), model):
            lines.extend(wrap(check, columns))
    followers = explanation.line
    if followers.count:
        add(f"Followers: {followers.count} waiting; {followers.fitting} fit now under both checks; "
            + f"needs from {queue_evidence.gib(followers.smallest_need)} to {queue_evidence.gib(followers.largest_need)}; "
            + f"oldest waiting {age(followers.oldest_since, explanation.now)} min")
    else:
        add("Followers: none")
    add("Frees when: " + queue_evidence.release_text(model.release, explanation, model.labels, model.zone) + ".")
    return lines


def keeps_run_place(model: QueueModel, position: int) -> bool:
    row = model.reading.ordered[position - 1][1]
    return row.get("order_since", row["since"]) < row["since"]


def page_running_steps(model: QueueModel) -> list[gate.ExplainedRunningStep]:
    return [running for running in model.reading.explanation.running
            if not isinstance(running.step.measured, gate.GoneMemory)]


def render_card(model: QueueModel, matches: Sequence[int], columns: int,
                selected_running: Sequence[gate.ExplainedRunningStep] = ()) -> list[Line]:
    head_card = 1 in matches
    lines = _waiting_cards(model, matches, columns)
    if lines:
        lines.append(())
    lines.extend(_recorded_evidence(model, head_card, columns, selected_running))
    return lines


def render_selection(model: QueueModel, selector: str, columns: int) -> list[Line]:
    matches = select_waiters(model, selector)
    running_matches = [running for running in page_running_steps(model)
        if running_matches_selector(running.step, selector, model.labels)]
    if not matches and not running_matches:
        return wrap((Span(f"No waiting or running step matches {selector}."),), columns)
    return render_card(model, matches, columns, running_matches)
