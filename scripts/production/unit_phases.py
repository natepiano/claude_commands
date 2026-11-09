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
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from collections.abc import Callable
from typing import NamedTuple, cast

from scripts.production import live_units
from scripts.production import session_staff, showrunners, staff, staff_sources, unit_lookup
from scripts.production.production_doc import Refusal, production_field, read_production
from scripts.agents import remote
from scripts.lib import conf
from scripts.message import session_send
from scripts.lib.process_failure import failure_detail

from scripts.direct import phase_table
from scripts.direct import plan_history

from scripts.production import display_cells
from scripts.production import unit_queue
from scripts.lib import unit_runs

# A task longer than this is cut, so one verification command cannot widen the whole table.
TASK_WIDTH = 80
RunEventsReader = Callable[[Path], list[dict[str, object]]]


WORK_EVENT = re.compile(r'"event_type"\s*:\s*"([^"\\]+)"')
WORK_EVENTS = frozenset(("phase_started", "phase_finished", "run_finished", "pass_started", "pass_finished",
                         "activity_started", "activity_finished"))


def read_work_events(path: Path) -> list[dict[str, object]]:
    """Read phase and step events for the two work cells."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return []
    events: list[dict[str, object]] = []
    for line in lines:
        kind = WORK_EVENT.search(line)
        if kind is None or kind.group(1) not in WORK_EVENTS:
            continue
        try:
            value = cast(object, json.loads(line))
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(cast(dict[str, object], value))
    return events


def shortened(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= TASK_WIDTH else text[: TASK_WIDTH - 1].rstrip() + "…"


def plan_phase_rows(fields: dict[str, object]) -> list[dict[str, object]]:
    rows = fields.get("phases")
    return [cast(dict[str, object], row) for row in cast(list[object], rows)
            if isinstance(row, dict)] if isinstance(rows, list) else []


def phase_cell(record: object, *, read_run: RunEventsReader = read_work_events) -> str:
    """Show the open phase and its task, or the plan's current state."""
    if not isinstance(record, dict):
        return display_cells.NONE
    fields = cast(dict[str, object], record)
    unavailable = fields.get("unavailable")
    if isinstance(unavailable, str):
        return "no plan"
    current = fields.get("current")
    phases = plan_phase_rows(fields)
    if (not isinstance(current, dict)
            or cast(dict[str, object], current).get("phase") not in {row.get("phase") for row in phases}):
        done = sum(row.get("status") == "done" for row in phases)
        if not phases:
            return "no phases"
        if done == len(phases):
            return "plan done"
        return f"between phases, {done} of {len(phases)} done" if done else "not started"
    phase = cast(dict[str, object], current)
    task = task_cell(fields, read_run=read_run)
    if task == display_cells.NONE:
        task = str(phase.get("title", ""))
    return shortened(f"phase {phase.get('phase')} of {phase.get('of')} — {task}")


def task_cell(record: object, *, read_run: RunEventsReader = read_work_events) -> str:
    """The open step of the newest run of the unit's plan, when a phase is open."""
    if not isinstance(record, dict):
        return display_cells.NONE
    fields = cast(dict[str, object], record)
    plan = fields.get("plan")
    if not isinstance(fields.get("current"), dict) or not isinstance(plan, str):
        return display_cells.NONE
    runs = plan_history.plan_runs(Path(plan))
    step = display_cells.open_step(read_run(runs[-1])) if runs else display_cells.NoOpenStep()
    if isinstance(step, display_cells.NoOpenStep):
        return display_cells.NONE
    task = f"{step.label}: {step.text}" if step.label and step.text else step.label or step.text or display_cells.NONE
    return shortened(task)


class OpenRunPhase(NamedTuple):
    """The id and current step of a phase that has not finished."""

    phase_id: str
    step: display_cells.OpenStep | display_cells.NoOpenStep


class OpenUnitRun(NamedTuple):
    """A unit's run with at least one phase still open."""

    started: float
    path: Path
    phases: tuple[OpenRunPhase, ...]


class NewestUnitRun(NamedTuple):
    """The newest run found for one unit."""

    started: float
    path: Path


class NoOpenUnitRun(NamedTuple):
    """The unit's newest run has no open phase."""


class OpenPhaseHistory(NamedTuple):
    """Keep the events for one phase until it closes."""

    phase_id: str
    events: list[dict[str, object]]


def open_unit_runs(snapshot: staff.StaffSnapshot, *,
                   read_run: RunEventsReader = read_work_events) -> dict[staff.StaffId, OpenUnitRun]:
    """Read only each unit's newest run and retain its open phases."""
    snapshot = staff.StaffSnapshot(staff.active(snapshot, "unit"))
    newest: dict[staff.StaffId, NewestUnitRun] = {}
    for path in (plan_history.history_root() / "runs").glob("*.jsonl"):
        started_event = plan_history.run_started_event(path)
        if started_event is None:
            continue
        try:
            owner = unit_runs.run_owner(started_event, snapshot)
        except staff.StaffUnreadable:
            continue
        if isinstance(owner, unit_runs.RunOwnerUnrecorded):
            continue
        started = next((float(value) for value in (started_event.get("run_started_at"), started_event.get("timestamp_epoch"))
                        if isinstance(value, int | float) and not isinstance(value, bool)), 0.0)
        previous = newest.get(owner.id)
        if previous is None or (started, path.name) > (previous.started, previous.path.name):
            newest[owner.id] = NewestUnitRun(started, path)
    result: dict[staff.StaffId, OpenUnitRun] = {}
    for owner_id, run in newest.items():
        events = read_run(run.path)
        if any(event.get("event_type") == "run_finished" for event in events):
            continue
        phases: dict[str, OpenPhaseHistory] = {}
        current = ""
        for event in events:
            kind = event.get("event_type")
            key = str(event.get("phase_instance_id") or event.get("phase_id") or current)
            if kind == "phase_started":
                current = key
                phases[key] = OpenPhaseHistory(str(event.get("phase_id", "")), [event])
            elif kind == "phase_finished":
                _ = phases.pop(key, None)
            elif key in phases:
                phases[key].events.append(event)
        if phases:
            result[owner_id] = OpenUnitRun(run.started, run.path, tuple(
                OpenRunPhase(phase.phase_id, display_cells.open_step(phase.events)) for phase in phases.values()))
    return result


def non_phase_cell(record: object, run: OpenUnitRun | NoOpenUnitRun) -> str:
    if isinstance(run, NoOpenUnitRun):
        return display_cells.NONE
    fields = cast(dict[str, object], record) if isinstance(record, dict) else {}
    phase_ids = {str(row.get("phase")) for row in plan_phase_rows(fields)}
    work: list[str] = []
    for identifier, step in run.phases:
        if identifier and identifier not in phase_ids:
            text = (f"{step.label}: {step.text}" if step.label and step.text else step.label or step.text
                    ) if isinstance(step, display_cells.OpenStep) else display_cells.NONE
            work.append(shortened(f"{identifier}: {text}"))
    return (work[-1] + (f" +{len(work) - 1} more" if len(work) > 1 else "")) if work else display_cells.NONE


def queued_cell(staff_id: staff.StaffId) -> str:
    try:
        items = unit_queue.read(staff_id)
    except (OSError, ValueError) as error:
        print(f"unit_phases: {unit_queue.state_path(staff_id)}: {error}", file=sys.stderr)
        return "queue unreadable"
    return (shortened(items[0]) + (f" +{len(items) - 1} more" if len(items) > 1 else "")) if items else display_cells.NONE


def table(units: list[live_units.LiveUnit], records: dict[str, object]) -> str:
    snapshot = staff.StaffSnapshot(tuple(unit.member for unit in units if isinstance(unit.member, staff.StaffRow)))
    events: dict[Path, list[dict[str, object]]] = {}

    def read_run(path: Path) -> list[dict[str, object]]:
        if path not in events:
            events[path] = read_work_events(path)
        return events[path]

    runs = open_unit_runs(snapshot, read_run=read_run)
    machines = display_cells.machine_column([unit.machine for unit in units])
    headers = ("Unit", "Status", *(("Machine",) if machines else ()), "Phase", "Non-Phase Work", "Queued")
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(("---",) * len(headers)) + " |"]
    for index, unit in enumerate(units):
        record = records.get(unit.unit)
        member = unit.member
        other = non_phase_cell(record, runs.get(member.id, NoOpenUnitRun())) if isinstance(member, staff.StaffRow) else display_cells.NONE
        queued = queued_cell(member.id) if isinstance(member, staff.StaffRow) else display_cells.NONE
        cells = (unit.unit, unit.status, *((machines[index],) if machines else ()), phase_cell(record, read_run=read_run), other, queued)
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
        done = subprocess.run(session_send.send_argv("--to", target, "--from", "unit_phases.py", "--text", "\n".join(lines),
                               send_script=showrunners.SEND),
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
