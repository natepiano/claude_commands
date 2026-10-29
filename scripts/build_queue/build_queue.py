#!/usr/bin/env python3
"""Show why build steps wait, or move one to the head of the memory queue."""

from __future__ import annotations

import argparse
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


class QueueArguments(argparse.Namespace):
    def __init__(self) -> None:
        super().__init__()
        self.command: str = ""
        self.selector: str = ""


def field(value: str) -> str:
    return " ".join(value.split())


def session_label(session_id: str, name: str) -> str:
    return field(name) or session_id[:8] or "no session"


def age(since: float, now: float) -> int:
    return max(0, int((now - since) // 60))


def reason_text(step: memory_admit.WaitingStep, view: memory_admit.QueueView, zone: ZoneInfo) -> str:
    reason = step.reason
    need = f"needs {step.need / memory_admit.GIB:.1f} GiB"
    capacity = f"{need}; {view.available / memory_admit.GIB:.1f} free"
    match reason:
        case memory_admit.ReadingUnavailable():
            return f"{need}; memory reading unavailable"
        case memory_admit.MemoryPressure():
            return memory_admit.pressure_text(reason)
        case memory_admit.NeedsBuildBudget():
            return (f"needs {reason.need / memory_admit.GIB:.1f} GiB; "
                    + f"builds {reason.charged / memory_admit.GIB:.1f} of "
                    + f"{reason.budget / memory_admit.GIB:.1f} GiB budget")
        case memory_admit.NeedsMemory():
            return (f"needs {reason.need / memory_admit.GIB:.1f} GiB; "
                    + f"{reason.free / memory_admit.GIB:.1f} free; "
                    + f"{view.reserve / memory_admit.GIB:.1f} kept for earlyoom")
        case memory_admit.BehindHead():
            return (f"{capacity}; behind {field(reason.head_name)} "
                    + f"(waiting {age(reason.head_since, view.now)} min)")
        case memory_admit.HeadAdmissionStatus():
            return memory_admit.head_wait_text(reason, zone)
        case memory_admit.ReadyForAdmission():
            return f"{capacity}; ready for the next admission check"
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


def render_queue(view: memory_admit.QueueView, zone: ZoneInfo) -> list[str]:
    running_header = ["#", "Session", "Step", "Folder", "Promised", "Using", "Delta", "State"]
    waiting_header = ["#", "Session", "Step", "Folder", "Waiting since", "Needs", "Reason"]
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
        lines.append(table_row([str(position), field(step.session_name) or folder, step.step, folder,
                                memory_text(step.promise), using, delta, state]))
    if not position:
        lines.append(table_row(["empty"] + [""] * (len(running_header) - 1)))
    lines.extend(["", "**Waiting**", "", table_row(waiting_header), table_row(["---"] * len(waiting_header))])
    for position, step in enumerate(view.waiting, 1):
        since = local_minute(int(step.since // 60), zone)
        folder = step.folder.rstrip("/").rpartition("/")[2] or "unknown"
        lines.append(table_row([str(position), field(step.session_name) or folder, step.step, folder,
                                f"{since} ({age(step.since, view.now)} min)",
                                memory_text(step.need), reason_text(step, view, zone)]))
    if not view.waiting:
        lines.append(table_row(["empty"] + [""] * (len(waiting_header) - 1)))
    lines.extend(["", f"Totals: promised {memory_text(promised_total)}; measured {memory_text(measured_total)}; "
                  + f"free {memory_text(view.available)}; reserve {memory_text(view.reserve)}"])
    return lines


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
        if arguments.command == "bump":
            actor = caller()
            moved = memory_admit.bump_waiter(arguments.selector, actor)
            print(f"{field(actor)} moved {session_label(moved.session_id, moved.session_name)}: "
                  + f"{field(moved.step)} in {field(moved.folder)} to the head; memory limits still apply")
        else:
            print("\n".join(render_queue(memory_admit.queue_view(), user_zone())))
    except (OSError, ValueError) as error:
        print(f"build queue: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
