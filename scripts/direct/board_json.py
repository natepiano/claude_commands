#!/usr/bin/env python3
"""Write a production's board snapshot from its durable records."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo


from scripts.production import display_cells

from scripts.direct import phase_table, plan_history, worker_runs
from scripts.lib import unit_runs
from scripts.production import session_staff, showrunners, staff, unit_run, unit_config
from scripts.agents import remote


def text(value: object) -> str:
    return value if isinstance(value, str) else ""


def number(value: object, default: float = 0) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def fields(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def read_object(path: Path, *, required: bool = False) -> dict[str, object]:
    try:
        return fields(cast(object, json.loads(path.read_text(encoding="utf-8"))))
    except (OSError, UnicodeError, ValueError):
        if required:
            raise
        return {}


@dataclass(frozen=True)
class TicketInProgress:
    id: str


@dataclass(frozen=True)
class NoTicketInProgress:
    pass


@dataclass(frozen=True)
class UnitTicketProgress:
    merged: int = 0
    open_ticket: TicketInProgress | NoTicketInProgress = NoTicketInProgress()


@dataclass(frozen=True)
class BoardClock:
    zone: ZoneInfo
    now: float

    def time(self, value: datetime | float) -> dict[str, object]:
        epoch = value.timestamp() if isinstance(value, datetime) else value
        local = datetime.fromtimestamp(epoch, self.zone)
        today = datetime.fromtimestamp(self.now, self.zone).date()
        return {"at": epoch, "text": local.strftime("%H:%M" if local.date() == today else "%a %d %b %H:%M")}

    def trail(self, row: phase_table.PhaseHistory) -> list[list[dict[str, object]]]:
        return [[self.time(target), self.time(stated)] for target, stated in row.eta_trail]

    def phase(self, row: phase_table.DonePhase | phase_table.OpenPhase | phase_table.TodoPhase) -> dict[str, object]:
        status = "done" if isinstance(row, phase_table.DonePhase) else "open"
        start: object = None
        finish: object = None
        took: object = None
        if isinstance(row, phase_table.TodoPhase):
            status = "todo"
        if isinstance(row, phase_table.DonePhase | phase_table.TodoPhase):
            if isinstance(row.times, phase_table.CompletedPhaseTiming | phase_table.PredictedPhaseTiming):
                start, finish = self.time(row.times.start), self.time(row.times.finish)
            if isinstance(row.times, phase_table.CompletedPhaseTiming):
                took = row.times.seconds
        else:
            start = self.time(row.started)
            took = max(0, self.now - row.started.timestamp())
        return {
            "id": row.phase, "title": row.title, "status": status, "took": took,
            "start": start, "finish": finish, "gate": {"kind": "none"},
            "fix_done": row.fix_done, "fix_running": row.fix_running, "findings": row.findings,
            "eta": {"first": self.time(row.first_eta) if isinstance(row.first_eta, datetime) else None,
                    "trail": self.trail(row),
                    "finished": self.time(row.finished_at) if isinstance(row.finished_at, datetime) else None,
                    "delta_minutes": row.delta_minutes if isinstance(row.delta_minutes, float) else None},
        }

    def eta(self, row: phase_table.OpenPhase) -> dict[str, object]:
        trail = row.eta_trail
        schedule = row.schedule_end
        return {
            "at": self.time(trail[-1][0]) if trail else None,
            "first": self.time(trail[0][0]) if trail else None,
            "trail": self.trail(row), "moved": row.moves,
            "delta_minutes": (trail[-1][0] - trail[0][0]).total_seconds() / 60 if trail else None,
            "schedule_end": self.time(schedule) if isinstance(schedule, datetime) else None,
            "fix_done": row.fix_done, "fix_running": row.fix_running,
        }

    def finish(self, finish: phase_table.FinishedAt | phase_table.PredictedFinish | phase_table.UnknownFinish) -> dict[str, object]:
        if isinstance(finish, phase_table.FinishedAt | phase_table.PredictedFinish):
            return {"kind": "finished" if isinstance(finish, phase_table.FinishedAt) else "predicted", "at": self.time(finish.at)}
        return {"kind": "unknown"}


def existing_sources(paths: set[Path]) -> list[str]:
    return sorted({str(path.resolve()) for path in paths if path.exists()})


def session_directory(run_id: str) -> Path:
    candidates = [root / run_id for root in unit_runs.run_roots()]
    return next((path for path in candidates if path.is_dir()), candidates[0])


def pane_worker_rows(record: staff.StaffSnapshot) -> dict[staff.StaffId, list[dict[str, object]]]:
    parents = session_staff.pane_parents(record)
    if not parents:
        return {}
    from scripts.herdr import worker_panes

    result: dict[staff.StaffId, list[dict[str, object]]] = {}
    for parent, observed in worker_panes.observe_parents(parents, record).items():
        rows: list[dict[str, object]] = []
        for worker in observed:
            reason = ""
            pane = ""
            if isinstance(worker, worker_panes.OpenWorker):
                state, pane = worker.status.value, worker.pane
            elif isinstance(worker, worker_panes.StoppedWorker):
                state, pane = "stopped", worker.pane
            elif isinstance(worker, worker_panes.ClosedWorker):
                state = "closed"
            elif isinstance(worker, worker_panes.ProcessUnknownWorker):
                state, pane, reason = "unknown", worker.pane, worker.reason
            else:
                state, reason = "incomplete", worker.reason
            if reason:
                print(f"board_json: {worker.row.name}: {state}: {reason}", file=sys.stderr)
            rows.append({"slot": worker.row.name, "role": "", "state": state,
                         "model": "", "effort": "", "doing": ": ".join(part for part in (pane, reason) if part)})
        result[parent] = rows
    return result


class ReadPaneRows(Enum):
    CURRENT = "read the current pane workers"


class ReadWorkerRuns(Enum):
    CURRENT = "measure the current worker runs"


def board_root() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state"))
    return Path(os.environ.get("UNIT_BOARD_DIR") or str(state / "unit-board")).expanduser().resolve()


def resolve_worker_readings(readings: worker_runs.BoardRunReadings | ReadWorkerRuns,
                            now: float) -> worker_runs.BoardRunReadings:
    if isinstance(readings, ReadWorkerRuns):
        return worker_runs.BoardRunReadings(board_root(), now, worker_runs.read_process_table)
    return readings


@dataclass(frozen=True)
class PaneRowsUnreadable:
    reason: str


def read_pane_worker_rows(record: staff.StaffSnapshot) -> dict[staff.StaffId, list[dict[str, object]]] | PaneRowsUnreadable:
    try:
        return pane_worker_rows(record)
    except (staff.StaffUnreadable, OSError, ValueError) as error:
        print(f"board_json: {error}", file=sys.stderr)
        return PaneRowsUnreadable(str(error))


def worker_activity(session: Path, rows: list[dict[str, object]], record: staff.StaffSnapshot,
                    sources: set[Path]) -> list[dict[str, object]]:
    """Combine pane status with task settings and the latest worker post."""
    tasks: dict[str, dict[str, object]] = {}
    for path in session.glob("impl_worker_*.json"):
        sources.add(path)
        value = fields(cast(object, json.loads(path.read_text(encoding="utf-8"))))
        member = record.by_id(staff.StaffId(text(value.get("staff_id"))))
        if member is not None:
            tasks[member.name] = value
    posts: dict[str, str] = {}
    log = session / "board.log"
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            match = re.match(r"\S+ \[([^]]+)\] [^:]+: (.*)", line)
            if match:
                post = match[2].removeprefix("launcher: ")
                posts[match[1]] = re.split(r"; (?:summary at|findings in)\s", post)[0].strip()[:80]
    result: list[dict[str, object]] = []
    for row in rows:
        name = text(row.get("slot"))
        task = tasks.get(name, {})
        result.append({**row,
                       **{key: task[key] for key in ("slot", "role", "model", "effort") if key in task},
                       **({"doing": posts[name]} if name in posts else {})})
    return result


def workers(session: Path, sources: set[Path], *,
            record: staff.StaffSnapshot | unit_runs.RunStaffAtUse = unit_runs.RunStaffAtUse.LOCAL,
            pane_rows: dict[staff.StaffId, list[dict[str, object]]] | PaneRowsUnreadable | ReadPaneRows = ReadPaneRows.CURRENT,
            events: Sequence[Mapping[str, object]] = (),
            readings: worker_runs.BoardRunReadings | ReadWorkerRuns = ReadWorkerRuns.CURRENT) -> list[dict[str, object]]:
    try:
        record = staff.snapshot() if isinstance(record, unit_runs.RunStaffAtUse) else record
        state_path = session / unit_runs.STATE_FILE
        pane_rows = read_pane_worker_rows(record) if isinstance(pane_rows, ReadPaneRows) else pane_rows
        if isinstance(pane_rows, PaneRowsUnreadable):
            return [{"slot": "?", "role": "", "state": "incomplete", "model": "", "effort": "", "doing": pane_rows.reason}]
        owner: staff.StaffRow | unit_runs.RunOwnerUnrecorded = unit_runs.RunOwnerUnrecorded("run has no unit staff owner")
        if state_path.exists():
            state = cast(object, json.loads(state_path.read_text(encoding="utf-8")))
            if not isinstance(state, dict):
                raise ValueError(f"{state_path}: expected an object")
            owner = unit_runs.run_owner(cast(dict[str, object], state), record)
            if isinstance(owner, staff.StaffRow):
                sources.update((state_path, session, session / "board.log"))
                run_readings = resolve_worker_readings(readings, plan_history.now_epoch())
                panes = pane_rows.get(owner.id, [])
                observed = worker_runs.observe_pane_rows(session, panes, record, owner.id, events, run_readings)
                for row, pane in zip(observed, panes):
                    if row.get("state") == "working" and pane.get("state") in ("running", "starting"):
                        row["state"] = pane["state"]
                rows = worker_activity(session, observed, record, sources)
                sources.update(run_readings.sources)
                if isinstance(readings, ReadWorkerRuns):
                    run_readings.save()
                return rows
        raise staff.StaffUnreadable(f"{session}: {owner.reason}")
    except (staff.StaffUnreadable, OSError, ValueError) as error:
        print(f"board_json: {session}: {error}", file=sys.stderr)
        return [{"slot": "?", "role": "", "state": "incomplete", "model": "", "effort": "", "doing": str(error)}]


def phase_steps(events: list[dict[str, object]], clock: BoardClock, current: phase_table.OpenPhase | phase_table.NoOpenPhase) -> list[list[object]]:
    if isinstance(current, phase_table.NoOpenPhase):
        return []
    instance = current.instance_id
    result: list[list[object]] = []
    opened: dict[tuple[str, str], int] = {}
    for event in events:
        if event.get("phase_instance_id") != instance:
            continue
        kind = text(event.get("event_type"))
        prefix = "pass" if kind.startswith("pass_") else "activity"
        key = (prefix, text(event.get(f"{prefix}_instance_id")))
        if kind in ("pass_started", "activity_started"):
            started = number(event.get("timestamp_epoch"))
            what = text(event.get("pass_activity")) if prefix == "pass" else text(event.get("activity_text"))
            what = what or text(event.get("pass_kind" if prefix == "pass" else "activity_label"))
            opened[key] = len(result)
            result.append(["running", what, clock.time(started), max(0, clock.now - started)])
        elif kind in ("pass_finished", "activity_finished") and key in opened:
            index = opened.pop(key)
            result[index][0] = text(event.get("status")) or "done"
            result[index][3] = number(event.get(f"{prefix}_elapsed_seconds"))
    return result


def stage(events: list[dict[str, object]], state: str, current: phase_table.OpenPhase | phase_table.NoOpenPhase, ticket: TicketInProgress | NoTicketInProgress) -> str:
    events = [event for event in events if isinstance(current, phase_table.OpenPhase)
              and event.get("phase_instance_id") == current.instance_id]
    open_passes: dict[str, dict[str, object]] = {}
    for event in events:
        kind = event.get("event_type")
        if kind in ("phase_started", "phase_finished", "run_finished"):
            open_passes.clear()
        elif kind == "pass_started":
            open_passes[text(event.get("pass_instance_id"))] = event
        elif kind == "pass_finished":
            _ = open_passes.pop(text(event.get("pass_instance_id")), None)
    if open_passes:
        event = list(open_passes.values())[-1]
        kind = text(event.get("pass_kind"))
        if kind == "fix":
            return f"Repair round {int(number(event.get('fix_pass')))}"
        label = {"impl": "Build", "test": "Tests", "review": "Review"}.get(kind)
        if label:
            return label
    dispatched = 0
    repairing = False
    for event in events:
        if event.get("event_type") == "finding_batch_dispatched":
            dispatched += 1
            repairing = True
        elif event.get("event_type") == "finding_batch_landed":
            repairing = False
    if repairing:
        return f"Repair round {dispatched}"
    step = display_cells.open_step(events)
    if isinstance(step, display_cells.OpenStep):
        return step.label
    if isinstance(current, phase_table.OpenPhase):
        return f"Ticket {ticket.id} running" if isinstance(ticket, TicketInProgress) else f"Phase {current.phase} running"
    if state == "run-finished":
        return "Run done"
    return "Standing by"


def is_standing_plan(plan: Path) -> bool:
    try:
        return any(line.startswith("> **Standing:**") for line in plan.read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeError):
        return False


def unit_ticket_progress(worktree: Path, unit_id: staff.StaffId, sources: set[Path]) -> UnitTicketProgress:
    try:
        common = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, check=False, timeout=10,
        )
        if common.returncode or not common.stdout.strip():
            return UnitTicketProgress()
        main_repo = Path(common.stdout.rstrip("\n")).parent
        state = Path(os.environ.get("STYLE_LEDGER_STATE_DIR", str(Path.home() / ".local/state/style"))).expanduser()
        database = state / main_repo.name / "ledger.sqlite"
        if not database.exists():
            return UnitTicketProgress()
        sources.add(database)
        with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=0)) as connection:
            row = cast(tuple[int, str | None], connection.execute(
                """SELECT
                    (SELECT count(*) FROM tickets WHERE unit = ? AND state = 'merged'),
                    (SELECT id FROM tickets WHERE unit = ? AND state IN ('assigned', 'checkpointed')
                     ORDER BY created_at DESC LIMIT 1)""", (unit_id, unit_id),
            ).fetchone())
            return UnitTicketProgress(row[0], TicketInProgress(row[1]) if row[1] else NoTicketInProgress())
    except (OSError, UnicodeError, subprocess.TimeoutExpired, sqlite3.Error):
        return UnitTicketProgress()


def unit(unit_name: str, unit_id: staff.StaffId, worktree: Path, plan_text: str, clock: BoardClock, sources: set[Path],
         *, resolver: session_staff.RowResolver | None = None,
         pane_rows: dict[staff.StaffId, list[dict[str, object]]] | PaneRowsUnreadable | ReadPaneRows = ReadPaneRows.CURRENT,
         readings: worker_runs.BoardRunReadings | ReadWorkerRuns = ReadWorkerRuns.CURRENT) -> dict[str, object]:
    resolver = resolver if resolver is not None else session_staff.RowResolver(staff.snapshot())
    worktree = worktree.expanduser().resolve()
    plan = plan_history.resolve_plan_path(worktree, plan_text)
    sources.add(plan)
    sources.add(plan_history.history_root() / "runs")
    if not (plan_history.history_root() / "runs").exists():
        sources.add(plan_history.history_root())
    sources.update(unit_runs.run_roots())
    found = unit_run.local_run(plan_history.history_root(), str(worktree), record=resolver.record)
    events: list[dict[str, object]] = []
    worker_rows: list[dict[str, object]] = []
    if isinstance(found, unit_run.RunFound):
        path = plan_history.history_root() / "runs" / f"{found.run_id}.jsonl"
        sources.add(path)
        events = display_cells.run_events(path)
        worker_rows = workers(session_directory(found.run_id), sources,
                              record=resolver.record, pane_rows=pane_rows, events=events, readings=readings)
    header = events[0] if events else {}
    run_plan = text(header.get("project_plan_doc")) or text(header.get("plan_doc"))
    record_plan = plan_history.resolve_plan_path(worktree, run_plan) if run_plan else plan
    sources.add(record_plan)
    sources.update(plan_history.plan_runs(record_plan, record=resolver.record))
    try:
        record = phase_table.build_plan(record_plan, resolver=resolver)
    except phase_table.NoPlan:
        record = phase_table.PhaseRecord(record_plan, datetime.fromtimestamp(clock.now, clock.zone), phase_table.NoOpenPhase(), [], phase_table.UnknownFinish(), phase_table.NothingToStart("no plan"))
    if not record.phases:
        record = phase_table.build_run(record_plan, events)
    state = "standing-by"
    if isinstance(found, unit_run.RunFound):
        state = "run-finished" if events and events[-1].get("event_type") == "run_finished" else "running"
    current = record.current
    standing = is_standing_plan(record.plan)
    tickets = unit_ticket_progress(worktree, unit_id, sources) if standing else UnitTicketProgress()
    ticket = tickets.open_ticket
    if isinstance(ticket, TicketInProgress) and isinstance(current, phase_table.OpenPhase):
        current = next((row for row in phase_table.build_run(record.plan, events).phases if isinstance(row, phase_table.OpenPhase)
                        and (row.phase == ticket.id or re.match(
                            rf"{re.escape(ticket.id)}(?=\s|$)", row.title))), current)
    current_phase: dict[str, object] | None = None
    if isinstance(current, phase_table.OpenPhase):
        if standing:
            if isinstance(ticket, NoTicketInProgress):
                title_id = re.match(rf"{re.escape(unit_name)}-\d+(?=\s|$)", current.title)
                ticket = TicketInProgress(title_id[0] if title_id else current.phase)
            title = re.sub(rf"^{re.escape(ticket.id)}(?:\s+—(?:\s+|$)|\s+|$)", "", current.title, count=1)
            current_phase = {"kind": "ticket", "id": ticket.id, "title": title, "merged": tickets.merged}
        else:
            current_phase = {"kind": "plan", "id": current.phase, "of": len(record.phases), "title": current.title}
    summary = next((text(event.get("activity")) for event in reversed(events) if event.get("event_type") == "progress_reported"), "")
    return {"key": unit_name, "plan": plan_text if plan.is_file() else "", "state": state,
            "phase": current_phase,
            "phases": [clock.phase(row) for row in record.phases], "finish": clock.finish(record.plan_finish),
            "stage": stage(events, state, current, ticket), "eta": clock.eta(current) if isinstance(current, phase_table.OpenPhase) else None,
            "summary": summary, "steps": phase_steps(events, clock, current), "workers": worker_rows}


@dataclass(frozen=True)
class BoardProduction:
    owner: staff.StaffRow
    runner: showrunners.Showrunner


@dataclass(frozen=True)
class BoardSubtree:
    record: staff.StaffSnapshot
    runners: tuple[BoardProduction, ...]


@dataclass(frozen=True)
class NoBoardSubtree:
    pass


def resolve(production: str, session: str = "") -> BoardSubtree | NoBoardSubtree:
    try:
        record = staff.snapshot()
        runners: dict[staff.StaffId, showrunners.Showrunner] = {}
        for owner in staff.active(record, "showrunner"):
            timer = showrunners.timer_for(owner, record)
            if isinstance(timer, showrunners.NoTimer):
                continue
            runners[owner.id] = showrunners.Showrunner(
                name=owner.name, session=owner.name, socket="", doc=(str(owner.doc.path) if isinstance(owner.doc, (staff.LiveDoc, staff.WrappedDoc)) else ""),
                zone=timer.zone)
        if production:
            root = next((row for row in staff.active(record, "showrunner")
                         if row.name == production and row.id in runners), None)
        else:
            root = record.by_current_session(session)
            while root is not None and root.kind != "showrunner":
                root = record.by_id(root.parent.id) if isinstance(root.parent, staff.ReportsTo) else None
    except staff.StaffUnreadable as error:
        print(f"board_json: {error}", file=sys.stderr)
        return NoBoardSubtree()
    if root is None or root.id not in runners:
        return NoBoardSubtree()
    boards: list[BoardProduction] = []
    level = [root]
    while level:
        for row in level:
            boards.append(BoardProduction(row, runners[row.id]))
        level = sorted((child for row in level for child in staff.active(record, "showrunner")
                        if child.parent == staff.ReportsTo(row.id) and child.id in runners),
                       key=lambda row: row.name)
    return BoardSubtree(record, tuple(boards))


def build(runner: showrunners.Showrunner, *, record: staff.StaffSnapshot,
          resolver: session_staff.RowResolver | None = None,
          pane_rows: dict[staff.StaffId, list[dict[str, object]]] | PaneRowsUnreadable | ReadPaneRows = ReadPaneRows.CURRENT,
          readings: worker_runs.BoardRunReadings | ReadWorkerRuns = ReadWorkerRuns.CURRENT) -> dict[str, object]:
    resolver = resolver if resolver is not None else session_staff.RowResolver(record)
    pane_rows = read_pane_worker_rows(record) if isinstance(pane_rows, ReadPaneRows) else pane_rows
    clock = BoardClock(ZoneInfo(runner["zone"]), plan_history.now_epoch())
    run_readings = resolve_worker_readings(readings, clock.now)
    document = Path(runner["doc"]).expanduser().resolve()
    sources = {document}
    owner = record.by_doc(remote.local_machine(), document)
    units: list[dict[str, object]] = []
    if owner is None:
        print(f"board_json: {runner['name']}: the production document is not recorded", file=sys.stderr)
    for row in staff.active(record, "unit"):
        if owner is None or row.parent != staff.ReportsTo(owner.id):
            continue
        tree = resolver.unit_worktree(row)
        try:
            config = resolver.unit_config(row)
        except ValueError as error:
            config = session_staff.ConfigMissing(str(error))
        doc = resolver.production_doc(row)
        problems: list[str] = []
        if not isinstance(tree, staff.PresentWorktree):
            problems.append("the worktree is not recorded")
        if not isinstance(config, unit_config.UnitConfig):
            problems.append(getattr(config, "reason", "the unit configuration is unavailable"))
        if not isinstance(doc, session_staff.ProductionDoc):
            problems.append(doc.reason)
        if problems:
            reason = "; ".join(dict.fromkeys(problems))
            print(f"board_json: {row.name}: {reason}", file=sys.stderr)
            units.append({"key": row.name, "state": "unavailable", "problem": reason})
        elif isinstance(config, unit_config.UnitConfig) and isinstance(tree, staff.PresentWorktree):
            plan = str(tree.path / config.plan.path) if isinstance(config.plan, unit_config.PlanFile) else "standby" if isinstance(config.plan, unit_config.StandbyPlan) else "permanent"
            units.append(unit(row.name, row.id, tree.path, plan, clock, sources, resolver=resolver,
                              pane_rows=pane_rows, readings=run_readings))
    if isinstance(readings, ReadWorkerRuns):
        run_readings.save()
    live = any(fields(worker).get("state") in ("working", "running", "starting", "waiting on run") for row in units
               for worker in cast(list[object], row.get("workers", [])))
    offset = datetime.fromtimestamp(clock.now, clock.zone).utcoffset()
    return {"production": runner["name"], "as_of": clock.time(clock.now), "zone": runner["zone"],
            "utc_offset_minutes": int(offset.total_seconds() / 60) if offset else 0,
            "units": units, "sources": existing_sources(sources), "live": live}


def write_board(path: Path, board: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = ""
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = stream.name
            _ = stream.write(json.dumps(board, ensure_ascii=False) + "\n")
        os.replace(temporary, path)
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--session", default="")
    _ = parser.add_argument("--production", default="")
    args = parser.parse_args(arguments)
    session, production = cast(str, args.session), cast(str, args.production)
    root = board_root()
    if not session and not production:
        parser.error("--session or --production is required")
    subtree = resolve(production, session)
    files: list[str] = []
    if isinstance(subtree, BoardSubtree):
        resolver = session_staff.RowResolver(subtree.record)
        pane_rows = read_pane_worker_rows(subtree.record)
        readings = worker_runs.BoardRunReadings(root, plan_history.now_epoch(), worker_runs.read_process_table)
        for production_board in subtree.runners:
            path = root / f"{production_board.owner.id}.json"
            write_board(path, build(production_board.runner, record=subtree.record, resolver=resolver,
                                    pane_rows=pane_rows, readings=readings))
            files.append(str(path))
        readings.save()
    watch_sources = {showrunners.NOTIFIER_STATE_DIR, showrunners.SHOWRUNNER_STATE_DIR}
    if isinstance(subtree, BoardSubtree):
        for row in staff.active(subtree.record, "showrunner"):
            timer = showrunners.timer_for(row, subtree.record)
            if isinstance(timer, showrunners.TimerRegistration):
                watch_sources.add(showrunners.SHOWRUNNER_STATE_DIR / row.id)
    watch = existing_sources(watch_sources)
    print(json.dumps({"files": files, "watch": watch}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
