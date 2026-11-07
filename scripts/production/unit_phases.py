#!/usr/bin/env python3
"""Print a showrunner's phase table or style ticket table.

Usage: unit_phases.py [--production <staff name>]

Without `--production`, the caller's staff row names the production.
The phase and task come from the unit's run records.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import sys
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

from scripts.production import live_units
from scripts.production import session_staff, showrunners, staff, staff_sources, unit_lookup
from scripts.production.production_doc import Refusal, production_field, read_production
from scripts.shutdown import remote
from scripts.lib import conf
from scripts.lib.process_failure import failure_detail

from scripts.direct import phase_table
from scripts.direct import plan_history

from scripts.production import display_cells

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
    lines = ["| Unit | Status | Phase | Task |", "| --- | --- | --- | --- |"]
    for unit in units:
        record = records.get(unit.unit)
        cells = (unit.unit, unit.status, phase_cell(record), task_cell(record))
        lines.append("| " + " | ".join(display_cells.escaped(cell) for cell in cells) + " |")
    return "\n".join(lines)


def is_style_production(doc: Path) -> bool:
    try:
        return production_field(doc.read_text(encoding="utf-8").splitlines(), "Kind") == "style"
    except Refusal:
        return False


def style_ledger_paths(doc: Path, units: list[live_units.LiveUnit]) -> dict[str, Path]:
    """Resolve ledgers from the staff rows, even when phase records fail."""
    from scripts.style import style_ledger

    try:
        rows = {row.name: row for row in unit_lookup.doc_units(doc, staff.snapshot())}
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


def send_anomalies(lines: tuple[str, ...]) -> None:
    """Send one notice; a delivery failure does not change the table result."""
    if not lines:
        return
    try:
        target = conf.read("production").text("UNIT_ANOMALIES_TO")
        done = subprocess.run([sys.executable, str(showrunners.SEND), "--to", target,
                               "--from", "unit_phases.py", "--text", "\n".join(lines)],
                              capture_output=True, text=True, check=False)
        if done.returncode:
            detail = " ".join(failure_detail(done).splitlines())
            print(f"anomaly: could not send unit anomalies to {target}: {detail}")
    except (OSError, conf.Unusable) as error:
        print(f"anomaly: could not send unit anomalies: {' '.join(str(error).splitlines())}")


def main(arguments: list[str]) -> int:
    match arguments:
        case ["--production", name]:
            pass
        case []:
            name = showrunners.own_name()
            if not name:
                print("unit_phases: this session is no production's showrunner; pass --production <staff name>",
                      file=sys.stderr)
                return 1
        case _:
            print("usage: unit_phases.py [--production <staff name>]", file=sys.stderr)
            return 2
    with session_staff.anomalies() as rendering_lines:
        try:
            doc = read_production(name).doc
            style = is_style_production(doc)
            with ThreadPoolExecutor(max_workers=2) as reads:
                sessions = reads.submit(live_units.read_units, name,
                                        check=staff_sources.CodexProcessThreads().check)
                records = cast(dict[str, object], json.loads(phase_table.show_production(doc)))
                listing = sessions.result()
                units = listing.units
            paths = style_ledger_paths(doc, units) if style and units else {}
            if not units:
                output = "no live units"
            elif style:
                from scripts.production import style_units
                output = style_units.table(units, records, doc=doc, ledger_paths=paths)
            else:
                output = table(units, records)
        except (OSError, ValueError, Refusal) as error:
            print(f"unit_phases: {error}", file=sys.stderr)
            return 1
    print(output)
    lines = tuple(dict.fromkeys((*listing.anomalies, *rendering_lines)))
    for line in lines:
        print(line)
    send_anomalies(lines)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
