"""One waiting step's place, order and admission checks."""
from __future__ import annotations

from collections.abc import Sequence

from scripts.buildlog import store
from scripts.build_queue import head_facts, queue_evidence, queue_lines
from scripts.build_queue import admission_protocol as wire
from scripts.build_queue.queue_lines import Line, Span, age, wrap
from scripts.build_queue.scope_activity import NotServiceSlice
from scripts.build_queue.queue_view import (QueueModel, behind_place, eligible_head_position, check_line, check_lines, clock, estimate_minutes,
    cloud_lines, cloud_matches, cloud_rows, has_move_priority, is_moved, name_spans, name_text, pressure_check, shown_waiters, reason_phrase, running_matches_selector, start_text, step_checks, same_waiter, HostLoad)
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
    if isinstance(reason, gate.TokenWaitingWaiter):
        return check_line(False, reason_phrase(reason, model.reading.view.waiting))
    if isinstance(reason, gate.BehindHead):
        text = f"#{behind_place(reason, model.reading.view.waiting) or eligible_head_position(model.reading.view.waiting)}'s turn"
        if isinstance(model.facts, head_facts.HeadFacts):
            text += "      " + head_facts.order_rule(model.facts)
        return check_line(False, text)
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
        add((Span(f"#{position} of {len(waiting)} · {queue_evidence.step_pid_text(step)} · ", bold=True), *name_spans(step, model.labels),
             Span(f" · {queue_lines.step_text(step, model.names)} · {step.need / gate.GIB:.1f} GiB", bold=True)))
        add((Span(f"Folder {step.folder}. Queue arrival {clock(model, step.since)}; waiting {age(step.since, model.reading.explanation.now)} min. "
                  + (f"Likely start {start_text(model, position, clock_time=True)}." if estimate_minutes(model, position) else "Likely start —.")),))
        add()
        checks = step_checks(model, position)
        if isinstance(step.reason, gate.TokenWaitingWaiter):
            add((Span("Next? No. It is waiting for the cargo token.", bold=True),))
        elif (isinstance(step.reason, gate.ReadyForAdmission)
                or isinstance(checks, gate.HeadExplanation) and checks.decision.state == "admit"):
            add((Span("Next? Yes. It starts at the next check.", bold=True),))
        elif isinstance(checks, gate.HeadExplanation):
            add((Span("Next? Yes, it is first. It starts when the checks below pass.", bold=True),))
        else:
            moved = sum(has_move_priority(model, place) for place in range(1, position))
            kept = sum(keeps_run_place(model, place) and not has_move_priority(model, place)
                       for place in range(1, position))
            priority = model.reading.explanation.priority
            unit = priority["unit"] if not isinstance(priority, gate.NoQueuePriority) else ""
            causes = f"{moved} of {unit} (queue priority), " if moved else ""
            if kept:
                causes += f"{kept} keep their run's place, "
            add((Span(f"Next? No. {position - 1} steps are ahead of it: {causes}"
                      + f"{position - 1 - moved - kept} joined earlier.", bold=True),))
        if position > 1:
            for place in range(1, min(position, 6)):
                ahead = waiting[place - 1]
                row = model.reading.ordered[place - 1][1]
                priority_order = gate.waiter_priority(row)
                if keeps_run_place(model, place):
                    detail = "keeps its run's place"
                else:
                    order_since = row.get("order_since", ahead.since)
                    step_row = model.reading.ordered[position - 1][1]
                    step_order_since = step_row.get("order_since", step.since)
                    later = order_since > step_order_since
                    minutes = age(step_order_since, order_since) if later else age(order_since, step_order_since)
                    detail = f"joined {clock(model, order_since)}, {minutes} min {'after' if later else 'before'} it"
                if isinstance(priority_order, gate.PriorityUnit):
                    detail += f"; {priority_order.unit} (queue priority)"
                add((Span(f"  #{place} {queue_evidence.step_pid_text(ahead)} · "), Span("↑  " if is_moved(model, place) else "   "),
                     *name_spans(ahead, model.labels), Span(f" {queue_lines.step_text(ahead, model.names)}  {detail}")), 6)
            if position > 6:
                add((Span(f"  and {position - 6} more"),))
            joined = 1 + sum(other.since < step.since for other in waiting)
            add((Span("  Queue-priority steps go first, then the longest wait. It joined "
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
            add((Span(f"  Each queue-priority step ahead of it adds about {estimate_minutes(model, 1)} min."),))
    return lines


def _recorded_evidence(model: QueueModel, head_card: bool, columns: int,
                       selected_running: Sequence[gate.ExplainedRunningStep]) -> list[Line]:
    """Show shared evidence once after the selected steps."""
    explanation = model.reading.explanation
    lines: list[Line] = []

    def add(text: str = "") -> None:
        lines.extend(wrap((Span(text),), columns))

    add(queue_evidence.room_text(explanation.room))
    host = explanation.host
    if isinstance(host, gate.HostAvailable):
        total = queue_evidence.gib(host.total) if isinstance(host.total, int) else "unknown"
        add(f"Host: available RAM {queue_evidence.gib(host.available)}; total RAM {total}")
        lines.extend(wrap(check_line(*pressure_check(host.pressure, model)), columns))
    else:
        add("Host memory: " + host.reason)
    if isinstance(model.load, HostLoad):
        cpus = model.load.cpus if isinstance(model.load.cpus, int) else "unknown"
        add(f"Host load: {model.load.load:.2f} (1 min); CPU count: {cpus}")
    else:
        add("Host load: unavailable")
    priority = head_facts.priority_text(explanation.priority)
    if priority:
        add(priority)
    add("Running:")
    running_steps = page_running_steps(model)
    machine = store.host_name() if running_steps else ""
    for running in running_steps:
        selected = running in selected_running
        if (not selected and isinstance(running.step.measured, gate.UnownedSliceMemory)
                and isinstance(running.step.measured.job_state, NotServiceSlice)):
            continue
        prefix = "Selected running step: " if selected else ""
        text = queue_evidence.running_text(running, explanation, model.labels, model.zone, names=model.names)
        if not isinstance(running.step.measured, (gate.CompilerServiceMemory, gate.UnownedSliceMemory)):
            pid, separator, detail = text.partition(" · ")
            text = pid + separator + machine + separator + detail
        add(prefix + text)
    if not running_steps:
        add("None.")
    add(f"Total current use: {queue_evidence.gib(explanation.totals.measured)}")
    head = explanation.head
    if isinstance(head, gate.NoWaiter):
        add("Nothing waits.")
        return lines
    if isinstance(head, gate.TokenWaitersOnly):
        add("All waiting steps are waiting for the cargo token.")
        add("Frees when: " + queue_evidence.release_text(model.release, explanation, model.labels, model.zone, names=model.names) + ".")
        return lines
    add()
    if not head_card:
        add(f"First in line: {queue_evidence.step_pid_text(head.waiter)} · {name_text(head.waiter, model)} {queue_lines.step_text(head.waiter, model.names)}; "
            + f"queue arrival {clock(model, head.waiter.since)}; "
            + f"waiting {age(head.waiter.since, explanation.now)} min; "
            + f"need {queue_evidence.gib(head.waiter.need)}")
    add("Need source: " + queue_evidence.need_text(head.need_source, zone=model.zone))
    add(queue_evidence.need_details(head.need_source))
    if isinstance(model.facts, head_facts.HeadFacts):
        facts = model.facts
        add(head_facts.bump_text(facts, model.zone, names=model.names))
        for text in head_facts.bump_lines(facts, model.zone, model.labels, names=model.names):
            add(text)
        add("Order rule: " + head_facts.order_rule(facts))
        for text in head_facts.cargo_token_lines(facts, model.zone, model.labels, names=model.names):
            add(text)
    else:
        add("Order history: unavailable")
        add("Cargo token waits: unknown")
    if not head_card:
        position = next(place for place, step in enumerate(model.reading.view.waiting, 1)
                        if same_waiter(step, head.waiter))
        for check in check_lines(step_checks(model, position), model):
            lines.extend(wrap(check, columns))
    followers = explanation.line
    if followers.count:
        add(f"Followers: {followers.count} waiting; {followers.fitting} fit now with head protection; "
            + f"needs from {queue_evidence.gib(followers.smallest_need)} to {queue_evidence.gib(followers.largest_need)}; "
            + f"oldest waiting {age(followers.oldest_since, explanation.now)} min")
    else:
        add("Followers: none")
    add("Frees when: " + queue_evidence.release_text(model.release, explanation, model.labels, model.zone, names=model.names) + ".")
    return lines


def keeps_run_place(model: QueueModel, position: int) -> bool:
    row = model.reading.ordered[position - 1][1]
    return row.get("order_since", row["since"]) < row["since"]


def page_running_steps(model: QueueModel) -> list[gate.ExplainedRunningStep]:
    return [running for running in model.reading.explanation.running
            if not isinstance(running.step.measured, gate.GoneMemory)]


def render_card(model: QueueModel, matches: Sequence[int], columns: int,
                selected_running: Sequence[gate.ExplainedRunningStep] = (),
                selected_cloud: tuple[wire.CloudRow, ...] = ()) -> list[Line]:
    head = model.reading.explanation.head
    head_card = isinstance(head, gate.HeadExplanation) and any(
        same_waiter(model.reading.view.waiting[position - 1], head.waiter) for position in matches)
    lines = _waiting_cards(model, matches, columns)
    if lines:
        lines.append(())
    lines.extend(_recorded_evidence(model, head_card, columns, selected_running))
    cloud = cloud_lines(model, columns, selected_cloud) if selected_cloud else []
    if cloud:
        lines.extend([(), *cloud])
    return lines


def render_selection(model: QueueModel, selector: str, columns: int) -> list[Line]:
    matches = select_waiters(model, selector)
    running_matches = [running for running in page_running_steps(model)
        if running_matches_selector(running.step, selector, model.labels)]
    cloud = tuple(row for row in cloud_rows(model) if cloud_matches(row, selector, model.cloud_labels))
    if cloud and not matches and not running_matches:
        return cloud_lines(model, columns, cloud)
    if not matches and not running_matches and not cloud:
        return wrap((Span(f"No waiting or running step matches {selector}."),), columns)
    return render_card(model, matches, columns, running_matches, cloud)
