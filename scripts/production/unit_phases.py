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
from datetime import datetime
from dataclasses import replace
from zoneinfo import ZoneInfo
from collections.abc import Callable
from typing import NamedTuple, TypedDict, cast

from scripts.production import live_units
from scripts.production import session_staff, showrunners, staff, staff_sources, unit_lookup
from scripts.production.production_doc import Refusal, production_field, read_production
from scripts.agents import remote
from scripts.lib import conf
from scripts.lib import names as display_names
from scripts.lib.user_zone import user_zone
from scripts.production import unit_config
from scripts.message import session_send
from scripts.lib.process_failure import failure_detail

from scripts.style import style_config
from scripts.direct import phase_table, worker_runs
from scripts.direct import plan_history

from scripts.production import display_cells
from scripts.production import unit_queue
from scripts.lib import unit_runs

# A task longer than this is cut, so one verification command cannot widen the whole table.
TASK_WIDTH = 80
RunEventsReader = Callable[[Path], list[dict[str, object]]]


WORK_EVENT = re.compile(r'"event_type"\s*:\s*"([^"\\]+)"')
WORK_EVENTS = frozenset(("run_started", "phase_started", "phase_finished", "run_finished", "pass_started", "pass_finished",
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


class PlanCellPhase(TypedDict, total=False):
    phase: str
    title: str
    status: str


class CurrentPlanPhase(TypedDict):
    phase: str
    of: int
    title: str


class PlanCellRecord(TypedDict, total=False):
    plan: str
    current: CurrentPlanPhase | None
    phases: list[PlanCellPhase]
    unavailable: str


def record_fields(record: object) -> PlanCellRecord:
    """Expose the existing phase cell fields from a built plan or its JSON form."""
    if isinstance(record, phase_table.PhaseRecord):
        current = record.current
        return {"plan": str(record.plan),
                "current": ({"phase": current.phase, "of": len(record.phases), "title": current.title}
                            if isinstance(current, phase_table.OpenPhase) else None),
                "phases": [{"phase": row.phase, "title": row.title,
                            "status": ("done" if isinstance(row, phase_table.DonePhase) else
                                       "running" if isinstance(row, phase_table.OpenPhase) else "todo")}
                           for row in record.phases]}
    return cast(PlanCellRecord, cast(object, record)) if isinstance(record, dict) else {}


def progress_cell(record: object) -> str:
    """Show phase states as a colored bar."""
    rows = plan_phase_rows(record_fields(record))
    if not rows:
        return display_cells.NONE
    size = min(20, len(rows))
    cells: list[str] = []
    for index in range(size):
        states = {row.get("status") for row in rows[index * len(rows) // size:(index + 1) * len(rows) // size]}
        cells.append("🟨" if states & {"running", "open"} else "⬜" if "todo" in states else "🟩")
    return "".join(cells)


def elapsed_text(seconds: float) -> str:
    minutes = max(0, int(seconds // 60))
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def short_time(moment: datetime, *, now: float, zone: ZoneInfo) -> str:
    local = moment.astimezone(zone)
    days = abs((local.date() - datetime.fromtimestamp(now, zone).date()).days)
    return local.strftime("%H:%M" if days == 0 else "%a %H:%M" if days <= 6 else "%d %b")


def eta_cell(record: object, *, now: float, zone: ZoneInfo,
             open_run_phases: tuple[phase_table.OpenPhase, ...] = ()) -> str:
    """Show the promise for open work or the next promised plan phase."""
    phases = record.phases if isinstance(record, phase_table.PhaseRecord) else []
    phase_ids = {phase.phase for phase in phases}
    opened = [phase for phase in open_run_phases if phase.phase in phase_ids]
    if not opened:
        opened = [phase for phase in phases if isinstance(phase, phase_table.OpenPhase)]
    current = record.current if isinstance(record, phase_table.PhaseRecord) else phase_table.NoOpenPhase()
    selected: phase_table.OpenPhase | phase_table.TodoPhase | phase_table.NoOpenPhase
    if opened:
        selected = (current if isinstance(current, phase_table.OpenPhase)
                    and any(phase.phase == current.phase for phase in opened) else opened[-1])
    elif open_run_phases:
        selected = open_run_phases[-1]
    elif isinstance(current, phase_table.OpenPhase):
        selected = current
    else:
        selected = next((phase for phase in phases if isinstance(phase, phase_table.TodoPhase) and phase.eta_trail),
                        phase_table.NoOpenPhase())
    if isinstance(selected, phase_table.NoOpenPhase):
        return display_cells.NONE
    if not selected.eta_trail:
        return "no ETA"
    promise = selected.eta_trail[-1][0]
    text = short_time(promise, now=now, zone=zone)
    if now > promise.timestamp():
        text += " · late " + elapsed_text(now - promise.timestamp())
    return shortened(text)


def running_plan(record: object) -> bool:
    """Return whether the record has unfinished plan phases."""
    return any(phase.get("status") != "done" for phase in plan_phase_rows(record_fields(record)))


def eta_plan_cell(record: object, *, now: float, zone: ZoneInfo) -> str:
    """Show the predicted finish of an unfinished plan."""
    if (running_plan(record) and isinstance(record, phase_table.PhaseRecord)
            and isinstance(record.plan_finish, phase_table.PredictedFinish)):
        return short_time(record.plan_finish.at, now=now, zone=zone)
    return display_cells.NONE


def workers_cell(rows: list[dict[str, object]], *, now: float) -> str:
    """Show active workers with detached runs before workers that are working."""
    bearing: list[str] = []
    working: list[str] = []
    idle = 0
    for row in rows:
        state = row.get("state")
        name = str(row.get("slot", ""))
        run = row.get("run")
        if state in ("waiting on run", "result queued", "run gone") and isinstance(run, dict):
            run = cast(dict[str, object], run)
            text = f"{name} {state}: {run.get('label', '')}"
            started = run.get("started_at")
            if state == "waiting on run" and isinstance(started, int | float):
                text += " " + elapsed_text(now - started)
            bearing.append(text)
        elif state in ("working", "running", "starting"):
            working.append(f"{name} working")
        elif state == "idle":
            idle += 1
    result = " · ".join([*bearing, *working])
    if result and idle:
        result += f" +{idle} idle"
    return shortened(result) if result else display_cells.NONE


def plan_phase_rows(fields: PlanCellRecord) -> list[PlanCellPhase]:
    return fields.get("phases", [])


def phase_cell(record: object) -> str:
    """Show the completed phase count and total."""
    rows = plan_phase_rows(record_fields(record))
    return f"{sum(row.get('status') == 'done' for row in rows)}/{len(rows)}" if rows else display_cells.NONE


def notes_cell(record: object, *, read_run: RunEventsReader = read_work_events) -> str:
    """Show the current phase, task, and repair rounds."""
    fields = record_fields(record)
    if not fields:
        return display_cells.NONE
    unavailable = fields.get("unavailable")
    if isinstance(unavailable, str):
        return "no plan"
    current = fields.get("current")
    phases = plan_phase_rows(fields)
    if (not isinstance(current, dict)
            or current.get("phase") not in {row.get("phase") for row in phases}):
        done = sum(row.get("status") == "done" for row in phases)
        if not phases:
            return "no phases"
        if done == len(phases):
            return "plan done"
        return "between phases" if done else "not started"
    phase = current
    task = task_cell(fields, read_run=read_run)
    if task == display_cells.NONE:
        task = str(phase.get("title", ""))
    parts = [f"phase {phase.get('phase')} — {task}"]
    if isinstance(record, phase_table.PhaseRecord) and isinstance(record.current, phase_table.OpenPhase):
        current = record.current
        if current.fix_rounds:
            parts.append(f"fix rounds {current.fix_rounds}" + (" + 1 running" if current.fix_running else ""))
    return shortened(" · ".join(parts))


def task_cell(record: object, *, read_run: RunEventsReader = read_work_events) -> str:
    """The open step of the newest run of the unit's plan, when a phase is open."""
    fields = record_fields(record)
    if not fields:
        return display_cells.NONE
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
    start: dict[str, object]


class NoOpenUnitRun(NamedTuple):
    """The unit's newest run has no open phase."""


class OpenPhaseHistory(NamedTuple):
    """Keep the events for one phase until it closes."""

    phase_id: str
    events: list[dict[str, object]]


def newest_unit_runs(snapshot: staff.StaffSnapshot) -> dict[staff.StaffId, NewestUnitRun]:
    """Find each unit's newest run from its recorded owner."""
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
            newest[owner.id] = NewestUnitRun(started, path, started_event)
    return newest


def open_unit_runs(snapshot: staff.StaffSnapshot, *,
                   read_run: RunEventsReader = read_work_events,
                   newest: dict[staff.StaffId, NewestUnitRun] | None = None) -> dict[staff.StaffId, OpenUnitRun]:
    """Read each unit's staff pointer and retain its open phases."""
    result: dict[staff.StaffId, OpenUnitRun] = {}
    for owner in snapshot.find(kind="unit"):
        if not isinstance(owner.run, staff.OpenRun) or not unit_runs.run_is_open(owner.run.path):
            continue
        path = plan_history.history_root() / "runs" / f"{owner.run.path.name}.jsonl"
        events = read_run(path)
        start = next((event for event in events if event.get("event_type") == "run_started"), dict[str, object]())
        started = next((float(value) for value in (start.get("run_started_at"), start.get("timestamp_epoch"))
                        if isinstance(value, int | float) and not isinstance(value, bool)), 0.0)
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
        previous = newest.get(owner.id) if newest is not None else None
        if previous is not None and previous.path == path:
            started = previous.started
        if phases:
            result[owner.id] = OpenUnitRun(started, path, tuple(
                OpenRunPhase(phase.phase_id, display_cells.open_step(phase.events)) for phase in phases.values()))
    return result


def non_phase_cell(record: object, run: OpenUnitRun | NoOpenUnitRun) -> str:
    if isinstance(run, NoOpenUnitRun):
        return display_cells.NONE
    fields = record_fields(record)
    phase_ids = {str(row.get("phase")) for row in plan_phase_rows(fields)}
    work: list[str] = []
    for identifier, step in run.phases:
        if identifier and identifier not in phase_ids:
            text = (f"{step.label}: {step.text}" if step.label and step.text else step.label or step.text
                    ) if isinstance(step, display_cells.OpenStep) else display_cells.NONE
            work.append(shortened(f"{identifier}: {text}"))
    return (work[-1] + (f" +{len(work) - 1} more" if len(work) > 1 else "")) if work else display_cells.NONE


def queued_cell(staff_id: staff.StaffId, record: object = None) -> str:
    try:
        items = unit_queue.read(staff_id)
    except (OSError, ValueError) as error:
        print(f"unit_phases: {unit_queue.state_path(staff_id)}: {error}", file=sys.stderr)
        queue = "queue unreadable"
    else:
        queue = (shortened(items[0]) + (f" +{len(items) - 1} more" if len(items) > 1 else "")) if items else ""
    rows = [row for row in plan_phase_rows(record_fields(record)) if row.get("status") not in ("done", "running", "open")]
    plan = ""
    if rows:
        plan = (f"plan: phase {rows[0].get('phase')}: {shortened(rows[0].get('title', ''))}"
                + (f" +{len(rows) - 1} more" if len(rows) > 1 else ""))
    return " · ".join(part for part in (queue, plan) if part) or display_cells.NONE


def session_directory(run: NewestUnitRun) -> Path:
    candidates = [root / run.path.stem for root in unit_runs.run_roots()]
    return next((path for path in candidates if path.is_dir()), candidates[0])


def fallback_plan(member: staff.StaffRow, run: NewestUnitRun, opened: OpenUnitRun | NoOpenUnitRun,
                  record: object, *, read_plan: Callable[[Path], list[plan_history.PlanPhase]]) -> PlanCellRecord:
    """Read unfinished plan headings against the newest run's open phases."""
    if not isinstance(member.worktree, staff.PresentWorktree):
        return {}
    value = run.start.get("project_plan_doc") or run.start.get("plan_doc")
    if not isinstance(value, str) or not value:
        return {}
    previous = run.start.get("working_dir")
    if isinstance(previous, str) and previous and value.startswith(previous + "/"):
        value = str(Path(value).relative_to(previous))
    path = plan_history.resolve_plan_path(member.worktree.path, value)
    if isinstance(record, phase_table.PhaseRecord) and record.plan == path:
        return record_fields(record)
    running = {phase.phase_id for phase in opened.phases} if isinstance(opened, OpenUnitRun) else set[str]()
    return {"plan": str(path), "phases": [
        {"phase": phase["id"], "title": phase["title"],
         "status": "done" if phase["done"] else "running" if phase["id"] in running else "todo"}
        for phase in read_plan(path)]}


def worker_heading() -> str:
    """Return the worker column title from the configured display names."""
    loaded = display_names.runtime_names()
    return loaded.audience("workers").capitalize() if isinstance(loaded, display_names.Names) else display_cells.NONE


def table(units: list[live_units.LiveUnit], records: dict[str, object], *,
          now: float | None = None, zone: ZoneInfo | None = None,
          snapshot: staff.StaffSnapshot | None = None,
          pane_rows: dict[staff.StaffId, list[dict[str, object]]] | None = None,
          runs_reader: worker_runs.WorkerRuns | None = None) -> str:
    snapshot = staff.snapshot() if snapshot is None else snapshot
    resolver = session_staff.RowResolver(snapshot)
    if now is None:
        now = runs_reader.now if runs_reader is not None else plan_history.now_epoch()
    zone = user_zone() if zone is None else zone
    events: dict[Path, list[dict[str, object]]] = {}
    plans: dict[Path, list[plan_history.PlanPhase]] = {}

    def read_plan(path: Path) -> list[plan_history.PlanPhase]:
        if path not in plans:
            try:
                plans[path] = plan_history.plan_phases(path, resolver=resolver)
            except (OSError, UnicodeError):
                plans[path] = []
        return plans[path]

    def read_run(path: Path) -> list[dict[str, object]]:
        if path not in events:
            events[path] = display_cells.run_events(path)
        return events[path]

    unit_snapshot = staff.StaffSnapshot(tuple(
        current for unit in units if isinstance(unit.member, staff.StaffRow)
        if (current := snapshot.by_id(unit.member.id)) is not None))
    newest = newest_unit_runs(unit_snapshot)
    runs = open_unit_runs(unit_snapshot, read_run=read_run, newest=newest)
    runs_reader = worker_runs.WorkerRuns(now) if runs_reader is None else runs_reader
    if pane_rows is None:
        try:
            unit_ids = {member.id for member in unit_snapshot.rows}
            worker_snapshot = replace(snapshot, rows=tuple(
                row for row in snapshot.rows if row.id in unit_ids
                or isinstance(row.parent, staff.ReportsTo) and row.parent.id in unit_ids))
            pane_rows = worker_runs.pane_worker_rows(worker_snapshot)
        except (OSError, ValueError) as error:
            print(f"unit_phases: workers: {error}", file=sys.stderr)
            pane_rows = {}
    machines = display_cells.machine_column([unit.machine for unit in units])
    table_rows: list[tuple[str, ...]] = []
    show_plan_eta = False
    for index, unit in enumerate(units):
        record = records.get(unit.unit)
        progress_record = record
        member = unit.member
        other = display_cells.NONE
        queued = display_cells.NONE
        workers = display_cells.NONE
        open_run_phases: tuple[phase_table.OpenPhase, ...] = ()
        if isinstance(member, staff.StaffRow):
            run = newest.get(member.id)
            queued_record = record
            opened = runs.get(member.id, NoOpenUnitRun())
            if not plan_phase_rows(record_fields(record)) and run is not None:
                queued_record = fallback_plan(member, run, opened,
                                              record, read_plan=read_plan)
            phase_ids = {phase.get("phase") for phase in plan_phase_rows(record_fields(record))}
            if isinstance(opened, OpenUnitRun) and (
                not phase_ids or any(phase.phase_id not in phase_ids for phase in opened.phases)
            ):
                plan = (record_fields(record).get("plan") or record_fields(queued_record).get("plan")
                        or (str(run.start.get("project_plan_doc") or run.start.get("plan_doc") or "")
                            if run is not None else ""))
                run_record = phase_table.build_run(Path(plan), read_run(opened.path))
                open_run_phases = tuple(phase for phase in run_record.phases if isinstance(phase, phase_table.OpenPhase))
                if not phase_ids:
                    progress_record = run_record
            other = non_phase_cell(record, opened)
            queued = queued_cell(member.id, queued_record)
            if (run is not None and isinstance(member.worktree, staff.PresentWorktree)
                    and member.worktree.machine == remote.local_machine()):
                rows = worker_runs.observe_pane_rows(session_directory(run), pane_rows.get(member.id, []),
                                                     snapshot, member.id, read_run(run.path), runs_reader)
                workers = workers_cell(rows, now=now)
        notes_record = progress_record if plan_phase_rows(record_fields(progress_record)) else record
        show_plan_eta = show_plan_eta or running_plan(progress_record)
        cells = (unit.unit, unit.status, *((machines[index],) if machines else ()),
                 progress_cell(progress_record), phase_cell(progress_record), notes_cell(notes_record, read_run=read_run),
                 eta_cell(progress_record, now=now, zone=zone, open_run_phases=open_run_phases),
                 eta_plan_cell(progress_record, now=now, zone=zone), workers, other, queued)
        table_rows.append(cells)
    headers = ("Unit", "Status", *(("Machine",) if machines else ()), "Progress", "Phase", "Notes", "ETA Next",
               *(("ETA Plan",) if show_plan_eta else ()), worker_heading(), "Non-Phase Work", "Queued")
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(("---",) * len(headers)) + " |"]
    plan_column = headers.index("ETA Next") + 1
    for cells in table_rows:
        if not show_plan_eta:
            cells = cells[:plan_column] + cells[plan_column + 1:]
        lines.append("| " + " | ".join(display_cells.escaped(cell) for cell in cells) + " |")
    return "\n".join(lines)


def production_records(doc: Path) -> dict[str, object]:
    """Build phase facts once for each configured unit plan."""
    snapshot = staff.snapshot()
    resolver = session_staff.RowResolver(snapshot)
    records: dict[str, object] = {}
    plans: dict[Path, phase_table.PhaseRecord | dict[str, object]] = {}
    for member in unit_lookup.doc_units(doc, snapshot):
        try:
            tree = resolver.unit_worktree(member)
            config = resolver.unit_config(member)
            if not isinstance(tree, staff.PresentWorktree):
                records[member.name] = {"unavailable": "the worktree is not recorded"}
            elif not isinstance(config, unit_config.UnitConfig):
                records[member.name] = {"unavailable": ("configuration is unavailable"
                                                     if isinstance(config, session_staff.NoUnitConfig) else config.reason)}
            elif isinstance(config.plan, unit_config.PlanFile):
                path = (tree.path / config.plan.path).resolve()
                if path not in plans:
                    try:
                        plans[path] = phase_table.build_plan(path, resolver=resolver)
                    except phase_table.NoPlan as error:
                        plans[path] = {"unavailable": str(error)}
                records[member.name] = plans[path]
        except ValueError as error:
            records[member.name] = {"unavailable": str(error)}
    return records


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
        case ["--help"] | ["-h"]:
            print("usage: unit_phases.py [--production <staff name>]")
            return 0
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
            production = read_production(name)
            doc = production.doc
            style = is_style_production(doc)
            with ThreadPoolExecutor(max_workers=2) as reads:
                sessions = reads.submit(live_units.read_units, name,
                                        check=staff_sources.CodexProcessThreads().check)
                records = (cast(dict[str, object], json.loads(phase_table.show_production(doc)))
                           if style else production_records(doc))
                listing = sessions.result()
                units = sorted(listing.units, key=lambda unit: unit.unit.casefold())
            configured: set[str] = set()
            if not style and style_config.has_style_units(doc.read_text().splitlines()):
                configured = {assignment.unit for assignment in style_config.read_config(doc).assignments}
            ticket_units = [unit for unit in units if style or unit.unit in configured]
            product_units = [unit for unit in units if unit not in ticket_units]
            paths = style_ledger_paths(doc, ticket_units) if ticket_units else {}
            if not units:
                output = "no live units"
            else:
                sections: list[str] = []
                if product_units:
                    sections.append(table(product_units, records, zone=production.zone))
                if ticket_units:
                    from scripts.production import style_units
                    sections.append(style_units.table(ticket_units, records, doc=doc, ledger_paths=paths))
                output = "\n\n".join(sections)
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
