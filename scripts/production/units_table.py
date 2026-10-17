#!/usr/bin/env python3
"""Print a showrunner's units as one Markdown table: each unit's session, current phase and task.

Usage: units_table.py [--production <production doc>]

Without `--production`, the doc is the one held by the update timer of the calling session
(`CLAUDE_CODE_SESSION_ID`). The phase is read from the unit's run records, and the task is the
step its newest run has open; no pane is read.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import NamedTuple, cast

import live_units
import showrunners
import unit_lookup
from production_units import Refusal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "delegate"))
import phase_table
from scripts.delegate import plan_history

NONE = "—"
# A task longer than this is cut, so one verification command cannot widen the whole table.
TASK_WIDTH = 80


class OpenStep(NamedTuple):
    label: str
    text: str


class NoOpenStep(NamedTuple):
    pass


def session_cell(unit: live_units.LiveUnit) -> str:
    """The unit's session name now, or why there is none."""
    if unit.session is None:
        return "gone"
    claude = unit.session.claude
    if isinstance(claude, unit_lookup.LiveClaude):
        return claude.name
    if isinstance(claude, unit_lookup.ClaudeUnknown):
        return "unknown"
    return "not running"


def phase_cell(record: object) -> str:
    """`3 of 7 · <title>` for an open phase; otherwise why there is none."""
    if not isinstance(record, dict):
        return NONE
    fields = cast(dict[str, object], record)
    unavailable = fields.get("unavailable")
    if isinstance(unavailable, str):
        return "no plan"
    current = fields.get("current")
    if not isinstance(current, dict):
        rows = fields.get("phases")
        phases = cast(list[object], rows) if isinstance(rows, list) else []
        done = sum(1 for row in phases if isinstance(row, dict) and cast(dict[str, object], row).get("status") == "done")
        if not phases:
            return "no phases"
        if done == len(phases):
            return "plan done"
        return f"between phases, {done} of {len(phases)} done" if done else "not started"
    phase = cast(dict[str, object], current)
    return f"{phase.get('phase')} of {phase.get('of')} · {phase.get('title')}"


def open_step(events: list[dict[str, object]]) -> OpenStep | NoOpenStep:
    """The step a run has open in its open phase: the newest open activity, else the newest open pass."""
    passes: dict[str, OpenStep] = {}
    activities: dict[str, OpenStep] = {}
    for event in events:
        kind = event.get("event_type")
        if kind in ("phase_started", "phase_finished", "run_finished"):
            passes.clear()
            activities.clear()
        elif kind == "pass_started":
            passes[str(event.get("pass_instance_id"))] = OpenStep(
                str(event.get("pass_kind", "")), str(event.get("pass_activity", "")))
        elif kind == "pass_finished":
            _ = passes.pop(str(event.get("pass_instance_id")), None)
        elif kind == "activity_started":
            activities[str(event.get("activity_instance_id"))] = OpenStep(
                str(event.get("activity_label", "")), str(event.get("activity_text", "")))
        elif kind == "activity_finished":
            _ = activities.pop(str(event.get("activity_instance_id")), None)
    for steps in (activities, passes):
        if steps:
            return list(steps.values())[-1]
    return NoOpenStep()


def run_events(path: Path) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return events
    for line in lines:
        try:
            event = cast(object, json.loads(line))
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(cast(dict[str, object], event))
    return events


def task_cell(record: object) -> str:
    """The open step of the newest run of the unit's plan, when a phase is open."""
    if not isinstance(record, dict):
        return NONE
    fields = cast(dict[str, object], record)
    plan = fields.get("plan")
    if not isinstance(fields.get("current"), dict) or not isinstance(plan, str):
        return NONE
    runs = plan_history.plan_runs(Path(plan))
    step = open_step(run_events(runs[-1])) if runs else NoOpenStep()
    if isinstance(step, NoOpenStep):
        return NONE
    task = f"{step.label}: {step.text}" if step.label and step.text else step.label or step.text or NONE
    return task if len(task) <= TASK_WIDTH else task[: TASK_WIDTH - 1].rstrip() + "…"


def escaped(cell: str) -> str:
    return " ".join(cell.split()).replace("|", "\\|")


def table(units: list[live_units.LiveUnit], records: dict[str, object]) -> str:
    lines = ["| Unit | Session | Phase | Task |", "| --- | --- | --- | --- |"]
    for unit in units:
        record = records.get(unit.unit)
        cells = (unit.unit, session_cell(unit), phase_cell(record), task_cell(record))
        lines.append("| " + " | ".join(escaped(cell) for cell in cells) + " |")
    return "\n".join(lines)


def main(arguments: list[str]) -> int:
    match arguments:
        case ["--production", doc_text]:
            doc: Path | None = Path(doc_text)
        case []:
            doc = showrunners.production_of(os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
            if doc is None:
                print("units_table: this session is no production's showrunner; pass --production <doc>",
                      file=sys.stderr)
                return 1
        case _:
            print("usage: units_table.py [--production <production doc>]", file=sys.stderr)
            return 2
    try:
        units = live_units.production_units(doc)
        records = cast(dict[str, object], json.loads(phase_table.show_production(doc)))
    except (OSError, ValueError, Refusal) as error:
        print(f"units_table: {error}", file=sys.stderr)
        return 1
    if not units:
        print("no live units")
        return 0
    print(table(units, records))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
