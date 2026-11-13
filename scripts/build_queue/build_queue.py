#!/usr/bin/env python3
"""Show the build queue, or one step's place and why it waits."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from dataclasses import dataclass

import json
import math
import os
from pathlib import Path
import sys
import time


from scripts.build_queue import head_facts, admission_client, admission_protocol
from scripts.build_queue.queue_card import select_waiters
from scripts.build_queue.queue_lines import json_form
from scripts.build_queue.queue_view import QueueModel, HostLoad, HostLoadUnavailable, UnknownCpuCount, render_view, running_matches_selector
from scripts.build_queue.queue_lines import Span
from scripts.buildlog.mac_idle_alert import alert_line, state_dir
from scripts.build_queue import admission_records
from scripts.lint import memory_admit
from scripts.lib.user_zone import user_zone
from scripts.message import unit_labels
from scripts.production import staff


def caller() -> str:
    return staff.caller_identity()


def default_columns() -> int:
    try:
        columns = int(os.environ.get("COLUMNS", ""))
    except ValueError:
        columns = 0
    if columns > 0:
        return columns
    if sys.stdout.isatty():
        try:
            columns = os.get_terminal_size(sys.stdout.fileno()).columns
            if columns > 0:
                return columns
        except OSError:
            pass
    return 100


@dataclass(frozen=True)
class WaitingPosition:
    place: int


@dataclass(frozen=True)
class NoWaitingStep:
    pass


@dataclass(frozen=True)
class DefaultBumpReason:
    pass


@dataclass(frozen=True)
class SuppliedBumpReason:
    text: str


DEFAULT_BUMP_REASON = DefaultBumpReason()


def bump(selector: str, actor: str, why: DefaultBumpReason | SuppliedBumpReason = DEFAULT_BUMP_REASON) -> int:
    reading = memory_admit.queue_read()
    labels = unit_labels.unit_labels_for([step.session_id for step in (*reading.view.waiting, *reading.view.running)], os.environ)
    model = QueueModel(reading, labels, admission_records.NoWaiter(), head_facts.PaceUnavailable(), user_zone(),
                       HostLoadUnavailable(), admission_records.NoWaiter())
    matches = select_waiters(model, selector)
    if matches:
        selected_place = matches[0]
        step = reading.view.waiting[selected_place - 1]
        selected = next(row for _, row in reading.ordered if row["pid"] == step.pid and row["since"] == step.since)
        selected_text = f"step #{selected_place}"
    else:
        running = next((step for step in reading.view.running if running_matches_selector(step, selector, labels)), None)
        selected = next((row["queue_waiter"] for row in reading.explanation.reservations
                         if "queue_waiter" in row and running is not None and row["pid"] == running.pid), None)
        if selected is None:
            print(f"build queue: no live step matches {selector}")
            return 3
        selected_text = "running step"
    unit = memory_admit.queue_unit(selected.get("unit", ""), selected.get("session_id", ""), labels)
    if unit == unit_labels.UnitLabels("").unit or not unit:
        print(f"build queue: {selected_text.replace('step', 'waiting step')} has no unit")
        return 3
    identity = admission_records.priority_step(selected)
    result = admission_client.priority_set(unit, why.text if isinstance(why, SuppliedBumpReason) else admission_records.queue_priority_reason(actor),
                                          actor, identity, selector=selector)
    if isinstance(result, admission_protocol.Refused):
        print(f"build queue: {result.reason}")
        return 3
    try:
        reading = memory_admit.queue_read()
    except admission_protocol.AdmissionUnavailable:
        print(f"queue priority set for {unit} ({selected_text}): the new place is not known now")
        return 0
    first = next((WaitingPosition(place) for place, (_, row) in enumerate(reading.ordered, 1)
                  if admission_records.priority_step(row) == identity), NoWaitingStep())
    detail = f"first step now #{first.place}" if isinstance(first, WaitingPosition) else "no waiting step now"
    priority = reading.explanation.priority
    identities = ([entry["step"] for entry in priority.entries]
                  if isinstance(priority, admission_records.QueuePriority) else [])
    place = f"; priority #{identities.index(identity) + 1}" if identity in identities else ""
    print(f"queue priority set for {unit} ({selected_text}): {detail}{place}")
    return 0


def read_host_load() -> HostLoad | HostLoadUnavailable:
    try:
        load = float((Path(os.environ.get("BUILDLOG_PROC", "/proc")) / "loadavg").read_text().split()[0])
        if not math.isfinite(load) or load < 0:
            return HostLoadUnavailable()
        return HostLoad(load, os.cpu_count() or UnknownCpuCount())
    except (OSError, ValueError, IndexError):
        return HostLoadUnavailable()


def main() -> int:
    import argparse

    if len(sys.argv) > 1 and sys.argv[1] in ("bump", "clear", "admit-head"):
        command = sys.argv[1]
        bump_words = sys.argv[2:]
        reason: DefaultBumpReason | SuppliedBumpReason = DefaultBumpReason()
        if command == "bump" and "--why" in bump_words:
            index = bump_words.index("--why")
            if index + 2 == len(bump_words):
                reason = SuppliedBumpReason(bump_words[index + 1])
                bump_words = bump_words[:index]
        if command == "bump" and (len(bump_words) != 1 or len(bump_words[0].split()) != 1):
            print("build queue: use /build:queue bump <position|session|folder>")
            return 3
        try:
            actor = admission_records.queue_command_actor()
            if command == "bump":
                return bump(bump_words[0], actor) if isinstance(reason, DefaultBumpReason) else bump(bump_words[0], actor, reason)
            elif sys.argv[1] == "clear" and len(sys.argv) in (2, 3):
                result = (admission_client.priority_clear(actor, sys.argv[2]) if len(sys.argv) == 3
                          else admission_client.priority_clear(actor))
            elif sys.argv[1] == "admit-head" and len(sys.argv) > 2:
                result = admission_client.admit_head(" ".join(sys.argv[2:]), actor)
            else:
                print("build queue: use bump <position|session|folder>, clear [unit], or admit-head <why>", file=sys.stderr)
                return 3
            if isinstance(result, admission_protocol.Refused):
                if command == "clear" and len(sys.argv) == 3:
                    print(f"build queue: {result.reason}")
                    return 3
                print(f"build queue: {result.reason}", file=sys.stderr)
                return 2
            if command == "clear":
                print("queue priority cleared")
            return 0
        except admission_protocol.AdmissionUnavailable as error:
            print(f"build queue: admission service unavailable: {error.reason}", file=sys.stderr)
            return 2
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--json", action="store_true")
    _ = parser.add_argument("--columns", type=int)
    _ = parser.add_argument("--you")
    _ = parser.add_argument("words", nargs="*")
    class Arguments(argparse.Namespace):
        you: str = caller()
        json: bool = False
        columns: int = default_columns()
        words: list[str] = []
    arguments = parser.parse_args(namespace=Arguments())
    if not arguments.json:
        print("build queue: the queue view is on the build page; run /build:page")
        return 3
    if arguments.columns < 1:
        parser.error("--columns must be positive")
    words = arguments.words
    if len(words) > 1:
        parser.error("use one selector")
    try:
        reading = admission_client.mine(caller()) if words == ["mine"] else memory_admit.queue_read()
        if words == ["mine"]:
            words = [caller()]
        facts = head_facts.read_head_facts(reading) if words else admission_records.NoWaiter()
        ids = [step.session_id for step in (*reading.view.running, *reading.view.waiting)]
        ids.extend(head_facts.session_ids(facts))
        labels = unit_labels.unit_labels_for(ids, os.environ)
        pace = head_facts.read_pace(reading.explanation.now) if words else head_facts.PaceUnavailable()
        load = read_host_load() if words else HostLoadUnavailable()
        release = memory_admit.smallest_head_release(reading.explanation) if words else admission_records.NoWaiter()
        model = QueueModel(reading, labels, facts, pace, user_zone(), load, release,
                           ci_memory_slice=admission_records.read_memory_config().ci_memory_slice)
        lines = render_view(model, arguments.columns, colour=arguments.json, you=arguments.you,
                            selector=words[0] if words else "")
        if words:
            from scripts.build_queue.queue_card import render_selection

            lines.extend([(), *render_selection(model, words[0], arguments.columns)])
        alert = alert_line(state_dir() / "alert.json", time.time(), user_zone())
        if alert is not None:
            lines.insert(0, (Span(alert, "error", bold=True),))
        print(json.dumps(json_form(lines, arguments.columns, reading.explanation.now),
                         ensure_ascii=False, separators=(",", ":")))
    except admission_protocol.AdmissionUnavailable as error:
        print(f"build queue: admission service unavailable: {error.reason}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as error:
        print(f"build queue: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
