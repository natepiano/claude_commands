#!/usr/bin/env python3
"""Print a showrunner's phase table or style ticket table.

Usage: unit_phases.py [--production <production doc>]

Without `--production`, the doc is the one held by the update timer of the calling session
(`CLAUDE_CODE_SESSION_ID`). The phase is read from the unit's run records, and the task is the
step its newest run has open; no pane is read.
"""
from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.production import live_units
from scripts.production import showrunners, staff, unit_lookup, unit_names
from scripts.production.production_doc import Refusal, production_field
from scripts.shutdown import remote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "direct"))
import phase_table
from scripts.direct import plan_history

import display_cells

# A task longer than this is cut, so one verification command cannot widen the whole table.
TASK_WIDTH = 80


def phase_cell(record: object) -> str:
    """`3 of 7 · <title>` for an open phase; otherwise why there is none."""
    if not isinstance(record, dict):
        return display_cells.NONE
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


def task_cell(record: object) -> str:
    """The open step of the newest run of the unit's plan, when a phase is open."""
    if not isinstance(record, dict):
        return display_cells.NONE
    fields = cast(dict[str, object], record)
    plan = fields.get("plan")
    if not isinstance(fields.get("current"), dict) or not isinstance(plan, str):
        return display_cells.NONE
    runs = plan_history.plan_runs(Path(plan))
    step = display_cells.open_step(display_cells.run_events(runs[-1])) if runs else display_cells.NoOpenStep()
    if isinstance(step, display_cells.NoOpenStep):
        return display_cells.NONE
    task = f"{step.label}: {step.text}" if step.label and step.text else step.label or step.text or display_cells.NONE
    return task if len(task) <= TASK_WIDTH else task[: TASK_WIDTH - 1].rstrip() + "…"


def table(units: list[live_units.LiveUnit], records: dict[str, object]) -> str:
    lines = ["| Unit | Phase | Task |", "| --- | --- | --- |"]
    for unit in units:
        record = records.get(unit.unit)
        cells = (display_cells.session_cell(unit), phase_cell(record), task_cell(record))
        lines.append("| " + " | ".join(display_cells.escaped(cell) for cell in cells) + " |")
    return "\n".join(lines)


def is_style_production(doc: Path) -> bool:
    try:
        return production_field(doc.read_text(encoding="utf-8").splitlines(), "Kind") == "style"
    except Refusal:
        return False


def style_ledger_paths(doc: Path, units: list[live_units.LiveUnit]) -> dict[str, Path]:
    """Resolve ledgers from the staff rows, even when phase records fail."""
    import style_ledger

    try:
        rows = {unit_names.unit_id(row.name): row for row in unit_lookup.doc_units(doc, staff.snapshot())}
    except (OSError, staff.StaffRefused) as error:
        print(f"unit_phases: {error}", file=sys.stderr)
        return {}
    paths: dict[str, Path] = {}
    for unit in units:
        row = rows.get(unit.unit)
        if (row is None or not isinstance(row.worktree, staff.PresentWorktree)
                or row.worktree.machine != remote.local_machine()):
            continue
        try:
            paths[unit.unit] = style_ledger.ledger_location(row.worktree.path).database
        except (OSError, ValueError) as error:
            print(f"unit_phases: {unit.unit}: {error}", file=sys.stderr)
    return paths


def main(arguments: list[str]) -> int:
    match arguments:
        case ["--production", doc_text]:
            doc: Path | None = Path(doc_text)
        case []:
            doc = showrunners.production_of(os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
            if doc is None:
                print("unit_phases: this session is no production's showrunner; pass --production <doc>",
                      file=sys.stderr)
                return 1
        case _:
            print("usage: unit_phases.py [--production <production doc>]", file=sys.stderr)
            return 2
    try:
        style = is_style_production(doc)
        with ThreadPoolExecutor(max_workers=2) as reads:
            sessions = reads.submit(live_units.units_of_doc, doc)
            if style:
                import style_units
            records = cast(dict[str, object], json.loads(phase_table.show_production(doc)))
            units = sessions.result()
        paths = style_ledger_paths(doc, units) if style and units else {}
    except (OSError, ValueError, Refusal) as error:
        print(f"unit_phases: {error}", file=sys.stderr)
        return 1
    if not units:
        print("no live units")
        return 0
    if style:
        import style_units
        print(style_units.table(units, records, doc=doc, ledger_paths=paths))
    else:
        print(table(units, records))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
