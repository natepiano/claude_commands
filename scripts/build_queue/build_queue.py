#!/usr/bin/env python3
"""Show why build steps wait, or move one to the head of the memory queue."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import lru_cache
import os
from pathlib import Path
import sys
from typing import assert_never
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


def read_queue_and_roster() -> tuple[memory_admit.QueueView, roster.Roster]:
    with ThreadPoolExecutor(max_workers=1) as executor:
        sessions = executor.submit(read_roster)
        # queue_view uses a signal-based lock timeout and must stay on the main thread.
        view = memory_admit.queue_view()
        return view, sessions.result()


def age(since: float, now: float) -> int:
    return max(0, int((now - since) // 60))


def free_memory_text(threshold: int, need: int, growth: int, reserve: int, head_need: int = 0) -> str:
    parts = [f"this step {need / memory_admit.GIB:.1f}"]
    for name, value in (("running steps may grow", growth), ("head", head_need), ("earlyoom", reserve)):
        amount = f"{value / memory_admit.GIB:.1f}"
        if amount != "0.0":
            parts.append(f"{name} {amount}")
    return f"needs {threshold / memory_admit.GIB:.1f} GiB free: " + ", ".join(parts)


def head_reason_text(reason: memory_admit.HeadAdmissionStatus, zone: ZoneInfo) -> str:
    decision = reason.decision
    parts: list[str] = []
    if isinstance(decision.pressure, memory_admit.MemoryPressure):
        parts.append(memory_admit.pressure_text(decision.pressure))
    elif decision.state == "admit":
        parts.append("ready for the next admission check")
    else:
        if decision.charged + reason.need > decision.budget:
            parts.append(f"builds use {decision.charged / memory_admit.GIB:.1f} "
                         + f"of the {decision.budget / memory_admit.GIB:.1f} GiB budget "
                         + f"(starts when build use falls to {(decision.budget - reason.need) / memory_admit.GIB:.1f} GiB)")
        if reason.available < decision.threshold:
            parts.append(free_memory_text(decision.threshold, reason.need, decision.promised, decision.reserve))
    until = local_minute(int(reason.backfill.deadline // 60), zone)
    if isinstance(reason.backfill, memory_admit.BackfillUntil):
        parts.append(f"younger steps may start until {until}")
    else:
        parts.append(f"younger steps wait until {until}")
    return "head: " + "; ".join(parts)


def reason_text(step: memory_admit.WaitingStep, view: memory_admit.QueueView, zone: ZoneInfo,
                labels: Mapping[str, roster.UnitLabels]) -> str:
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
            head = labels.get(reason.head_session_id, roster.UnitLabels(field(reason.head_name)))
            return (f"behind {head.session} "
                    + f"(waiting {age(reason.head_since, view.now)} min)")
        case memory_admit.HeadAdmissionStatus():
            return head_reason_text(reason, zone)
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


def render_queue(view: memory_admit.QueueView, zone: ZoneInfo,
                 entries: Sequence[roster.RosterEntry] = (), problems: Sequence[str] = ()) -> list[str]:
    labels = roster.unit_labels(entries)
    running_header = ["#", "Showrunner", "Unit", "Session", "Step", "Folder", "Promised", "Using", "Delta", "State"]
    waiting_header = ["#", "Showrunner", "Unit", "Session", "Step", "Folder", "Waiting since", "Needs", "Free", "Reason"]
    lines = ["**Running**", "", table_row(running_header), table_row(["---"] * len(running_header))]
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
        lines.append(table_row([str(position), identity.showrunner, identity.unit, identity.session, step.step, folder,
                                memory_text(step.promise), using, delta, state]))
    if not position:
        lines.append(table_row(["empty"] + [""] * (len(running_header) - 1)))
    lines.extend(["", "**Waiting**", "", table_row(waiting_header), table_row(["---"] * len(waiting_header))])
    free = ("—" if any(isinstance(step.reason, memory_admit.ReadingUnavailable) for step in view.waiting)
            else memory_text(view.available))
    for position, step in enumerate(view.waiting, 1):
        since = local_minute(int(step.since // 60), zone)
        folder = step.folder.rstrip("/").rpartition("/")[2] or "unknown"
        identity = step_labels(step, labels)
        lines.append(table_row([str(position), identity.showrunner, identity.unit, identity.session, step.step, folder,
                                f"{since} ({age(step.since, view.now)} min)",
                                memory_text(step.need), free, reason_text(step, view, zone, labels)]))
    if not view.waiting:
        lines.append(table_row(["empty"] + [""] * (len(waiting_header) - 1)))
    lines.extend(["", f"Totals: promised {memory_text(promised_total)}; measured {memory_text(measured_total)}; "
                  + f"free {memory_text(view.available)}; reserve {memory_text(view.reserve)}"])
    if problems:
        lines.append(f"Roster: {field(problems[0])}; Showrunner and Unit show — for unmatched steps")
    return lines


def bump_selector(selector: str, view: memory_admit.QueueView, labels: Mapping[str, roster.UnitLabels]) -> str:
    matches = [(position, step) for position, step in enumerate(view.waiting, 1)
               if step_labels(step, labels).session == selector]
    if len(matches) > 1:
        descriptions = ", ".join(f"{position}: {step_labels(step, labels).session} {step.folder}"
                                 for position, step in matches)
        raise ValueError(f"{selector!r}: ambiguous waiter; matches: {descriptions}")
    if matches:
        return str(matches[0][0])
    return selector


def caller() -> str:
    return (os.environ.get("CODEX_THREAD_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID")
            or f"user:{os.environ.get('USER', 'unknown')}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command")
    bump = commands.add_parser("bump", help="move a waiting position, session or folder to the head")
    _ = bump.add_argument("selector")
    arguments = parser.parse_args(namespace=QueueArguments())
    try:
        view, sessions = read_queue_and_roster()
        if arguments.command == "bump":
            actor = caller()
            labels = roster.unit_labels(sessions.entries)
            moved = memory_admit.bump_waiter(bump_selector(arguments.selector, view, labels), actor)
            print(f"{field(actor)} moved {step_labels(moved, labels).session}: "
                  + f"{field(moved.step)} in {field(moved.folder)} to the head; memory limits still apply")
        else:
            print("\n".join(render_queue(view, user_zone(), sessions.entries, sessions.problems)))
    except (OSError, ValueError) as error:
        print(f"build queue: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
