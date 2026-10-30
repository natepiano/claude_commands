#!/usr/bin/env python3
"""Show why build steps wait, or move one to the head of the memory queue."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import lru_cache
import os
from pathlib import Path
import sys
from typing import Literal, TypedDict, assert_never
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.lint import memory_admit
from scripts.lib.user_zone import user_zone
from scripts.message import roster


class QueueArguments(argparse.Namespace):
    def __init__(self) -> None:
        super().__init__()
        self.command: str = ""
        self.selector: str = ""
        self.json: bool = False


class HeadLine(TypedDict):
    text: str
    tone: Literal["default", "error"]


class QueueTotals(TypedDict):
    promised: str
    measured: str
    free: str
    reserve: str


class QueueSnapshot(TypedDict):
    running_header: list[str]
    running: list[list[str]]
    waiting_header: list[str]
    waiting: list[list[str]]
    head: HeadLine
    totals: QueueTotals
    problems: list[str]


def field(value: str) -> str:
    return " ".join(value.split())


def step_labels(step: memory_admit.RunningStep | memory_admit.WaitingStep,
                labels: Mapping[str, roster.UnitLabels]) -> roster.UnitLabels:
    folder = step.folder.rstrip("/").rpartition("/")[2] or "unknown"
    return labels.get(step.session_id, roster.UnitLabels(field(step.session_name) or folder))


def read_roster() -> roster.Roster:
    try:
        return roster.roster(os.environ, roster.ThisMachine())
    except Exception as error:
        return roster.Roster([], [str(error) or type(error).__name__])


def read_queue_and_roster() -> tuple[memory_admit.QueueRead, roster.Roster]:
    with ThreadPoolExecutor(max_workers=1) as executor:
        sessions = executor.submit(read_roster)
        # locked_queue_read uses a signal-based lock timeout and must stay on the main thread.
        reading = memory_admit.locked_queue_read()
        return reading, sessions.result()


def age(since: float, now: float) -> int:
    return max(0, int((now - since) // 60))


def free_memory_text(threshold: int, need: int, growth: int, reserve: int, head_need: int = 0) -> str:
    parts = [f"this step {need / memory_admit.GIB:.1f}"]
    for name, value in (("running steps may grow", growth), ("head", head_need), ("earlyoom", reserve)):
        amount = f"{value / memory_admit.GIB:.1f}"
        if amount != "0.0":
            parts.append(f"{name} {amount}")
    return f"needs {threshold / memory_admit.GIB:.1f} GiB free: " + ", ".join(parts)


def head_reason_text(head: memory_admit.HeadExplanation, zone: ZoneInfo) -> str:
    decision = head.decision
    parts: list[str] = []
    if isinstance(decision.pressure, memory_admit.MemoryPressure):
        parts.append(memory_admit.pressure_text(decision.pressure))
    elif decision.state == "admit":
        parts.append("ready for the next admission check")
    else:
        if not head.budget.passes:
            parts.append(f"builds use {head.budget.charged / memory_admit.GIB:.1f} "
                         + f"of the {head.budget.budget / memory_admit.GIB:.1f} GiB budget "
                         + f"(starts when build use falls to {memory_text(head.budget.starts_at)})")
        if not head.free.passes:
            parts.append(free_memory_text(head.free.threshold, head.free.need, head.free.growth,
                                          head.free.reserve, head.free.head_need))
    until = local_minute(int(head.order.deadline // 60), zone)
    if isinstance(head.order, memory_admit.BackfillUntil):
        parts.append(f"younger steps may start until {until}")
    else:
        parts.append(f"younger steps wait until {until}")
    return "head: " + "; ".join(parts)


def reason_text(step: memory_admit.WaitingStep, reading: memory_admit.QueueRead, zone: ZoneInfo,
                labels: Mapping[str, roster.UnitLabels]) -> str:
    view = reading.view
    reason = step.reason
    match reason:
        case memory_admit.ReadingUnavailable():
            return "memory reading unavailable"
        case memory_admit.MemoryPressure():
            return memory_admit.pressure_text(reason)
        case memory_admit.NeedsBuildBudget():
            return (f"builds use {reason.charged / memory_admit.GIB:.1f} of the "
                    + f"{reason.budget / memory_admit.GIB:.1f} GiB budget")
        case memory_admit.NeedsMemory():
            return free_memory_text(reason.threshold, reason.need, reason.growth, reason.reserve, reason.head_need)
        case memory_admit.BehindHead():
            identity = labels.get(reason.head_session_id, roster.UnitLabels(field(reason.head_name)))
            return (f"behind {identity.session} "
                    + f"(waiting {age(reason.head_since, view.now)} min)")
        case memory_admit.HeadAdmissionStatus():
            head = reading.explanation.head
            return head_reason_text(head, zone) if isinstance(head, memory_admit.HeadExplanation) else "nothing waits"
        case memory_admit.ReadyForAdmission():
            return "ready for the next admission check"
        case _:
            assert_never(reason)


def table_row(cells: list[str]) -> str:
    return "| " + " | ".join(field(cell).replace("|", r"\|") for cell in cells) + " |"


@lru_cache(maxsize=256)
def memory_text(value: int) -> str:
    return f"{value / memory_admit.GIB:.1f} GiB"


@lru_cache(maxsize=512)
def local_minute(minute: int, zone: ZoneInfo) -> str:
    return f"{datetime.fromtimestamp(minute * 60, zone):%H:%M}"


def head_line(explanation: memory_admit.QueueExplanation, labels: Mapping[str, roster.UnitLabels],
              zone: ZoneInfo) -> HeadLine:
    head = explanation.head
    if isinstance(head, memory_admit.NoWaiter):
        return {"text": "", "tone": "default"}
    if isinstance(explanation.host, memory_admit.ReadingUnavailable):
        detail = "memory reading unavailable"
    else:
        detail = head_reason_text(head, zone).removeprefix("head: ")
        if not head.budget.passes and not isinstance(head.decision.pressure, memory_admit.MemoryPressure):
            for running in explanation.running:
                if running.charged <= 0:
                    continue
                used = (memory_text(running.measured) if isinstance(running.step.measured, memory_admit.MeasuredMemory)
                        else "— (starting)")
                detail += (f"; {step_labels(running.step, labels).session} {running.step.step}: "
                           + f"charged {memory_text(running.charged)}, using {used}")
        detail += f"; free {memory_text(head.free.available)}, step needs {memory_text(head.free.need)}"
    stalled = (explanation.now - head.waiter.since >= 15 * 60
               and isinstance(explanation.host, memory_admit.HostAvailable)
               and head.free.available >= head.free.need)
    prefix = "Stalled:" if stalled else "Head:"
    return {"text": field(f"{prefix} {step_labels(head.waiter, labels).session} {head.waiter.step}: {detail}"),
            "tone": "error" if stalled else "default"}


def queue_snapshot(reading: memory_admit.QueueRead, zone: ZoneInfo,
                   entries: Sequence[roster.RosterEntry] = (), problems: Sequence[str] = ()) -> QueueSnapshot:
    view = reading.view
    labels = roster.unit_labels(entries)
    running_header = ["#", "Showrunner", "Unit", "Session", "Step", "Folder", "Promised", "Using", "Delta", "State"]
    waiting_header = ["#", "Showrunner", "Unit", "Session", "Step", "Folder", "Waiting since", "Needs", "Free", "Reason"]
    running: list[list[str]] = []
    waiting: list[list[str]] = []
    promised_total, measured_total, position = 0, 0, 0
    for step in view.running:
        match step.measured:
            case memory_admit.MeasuredMemory(bytes=used):
                using, delta, state = memory_text(used), memory_text(step.promise - used), "measured"
                measured_total += used
            case memory_admit.PromisedMemory():
                using, delta, state = "—", "—", "starting"
            case memory_admit.GoneMemory():
                continue
            case _:
                assert_never(step.measured)
        position += 1
        promised_total += step.promise
        folder = step.folder.rstrip("/").rpartition("/")[2] or "unknown"
        identity = step_labels(step, labels)
        running.append([field(cell) for cell in [str(position), identity.showrunner, identity.unit, identity.session,
                        step.step, folder, memory_text(step.promise), using, delta, state]])
    free = ("—" if any(isinstance(step.reason, memory_admit.ReadingUnavailable) for step in view.waiting)
            else memory_text(view.available))
    for position, step in enumerate(view.waiting, 1):
        since = local_minute(int(step.since // 60), zone)
        folder = step.folder.rstrip("/").rpartition("/")[2] or "unknown"
        identity = step_labels(step, labels)
        waiting.append([field(cell) for cell in [str(position), identity.showrunner, identity.unit, identity.session,
                        step.step, folder, f"{since} ({age(step.since, view.now)} min)",
                        memory_text(step.need), free, reason_text(step, reading, zone, labels)]])
    return {"running_header": running_header, "running": running, "waiting_header": waiting_header, "waiting": waiting,
            "head": head_line(reading.explanation, labels, zone),
            "totals": {"promised": memory_text(promised_total), "measured": memory_text(measured_total),
                       "free": memory_text(view.available), "reserve": memory_text(view.reserve)},
            "problems": [field(problem) for problem in problems]}


def render_queue(reading: memory_admit.QueueRead, zone: ZoneInfo,
                 entries: Sequence[roster.RosterEntry] = (), problems: Sequence[str] = ()) -> list[str]:
    snapshot = queue_snapshot(reading, zone, entries, problems)
    lines: list[str] = []
    for title, header, rows in (("Running", snapshot["running_header"], snapshot["running"]),
                                ("Waiting", snapshot["waiting_header"], snapshot["waiting"])):
        if lines:
            lines.append("")
        if title == "Waiting" and snapshot["head"]["text"]:
            lines.extend([snapshot["head"]["text"], ""])
        lines.extend([f"**{title}**", "", table_row(header), table_row(["---:" if name in {"#", "Promised", "Using", "Delta", "Waiting since", "Needs", "Free"}
                                                                            else "---" for name in header])])
        lines.extend(table_row(row) for row in (rows or [["empty"] + [""] * (len(header) - 1)]))
    totals = snapshot["totals"]
    lines.extend(["", f"Totals: promised {totals['promised']}; measured {totals['measured']}; "
                  + f"free {totals['free']}; reserve {totals['reserve']}"])
    if snapshot["problems"]:
        lines.append(f"Roster: {snapshot['problems'][0]}; Showrunner and Unit show — for unmatched steps")
    return lines


def bump_selector(selector: str, view: memory_admit.QueueView, labels: Mapping[str, roster.UnitLabels]) -> str:
    matches = [(position, step) for position, step in enumerate(view.waiting, 1)
               if step_labels(step, labels).session == selector]
    if len(matches) > 1:
        descriptions = ", ".join(f"{position}: {step_labels(step, labels).session} {step.folder}"
                                 for position, step in matches)
        raise memory_admit.UnmatchedWaiter(f"{selector!r}: ambiguous waiter; matches: {descriptions}")
    if matches:
        return str(matches[0][0])
    return selector


def caller() -> str:
    return (os.environ.get("CODEX_THREAD_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID")
            or f"user:{os.environ.get('USER', 'unknown')}")


def refusal_text(refusal: memory_admit.UnmatchedWaiter) -> str:
    escaped = "".join(character if character.isprintable() else json.dumps(character)[1:-1]
                      for character in str(refusal))
    return f"build queue: {escaped}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--json", action="store_true", help="print formatted queue cells as JSON")
    commands = parser.add_subparsers(dest="command")
    bump = commands.add_parser("bump", help="move a waiting position, session or folder to the head")
    _ = bump.add_argument("selector")
    arguments = parser.parse_args(namespace=QueueArguments())
    try:
        reading, sessions = read_queue_and_roster()
        view = reading.view
        if arguments.command == "bump":
            actor = caller()
            labels = roster.unit_labels(sessions.entries)
            try:
                moved = memory_admit.bump_waiter(bump_selector(arguments.selector, view, labels), actor)
            except memory_admit.UnmatchedWaiter as error:
                print(refusal_text(error))
                return 3
            print(f"{field(actor)} moved {step_labels(moved, labels).session}: "
                  + f"{field(moved.step)} in {field(moved.folder)} to the head; memory limits still apply")
        elif arguments.json:
            print(json.dumps(queue_snapshot(reading, user_zone(), sessions.entries, sessions.problems)))
        else:
            print("\n".join(render_queue(reading, user_zone(), sessions.entries, sessions.problems)))
    except (OSError, ValueError) as error:
        print(f"build queue: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
