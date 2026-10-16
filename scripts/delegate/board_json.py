#!/usr/bin/env python3
"""Write a production's board snapshot from its durable records."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if not __package__:
    sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts" / "production"))

from scripts.delegate import phase_table, plan_history
from scripts.lib import unit_runs
from scripts.production import production_units, showrunners, unit_run, units_table


def text(value: object) -> str:
    return value if isinstance(value, str) else ""


def number(value: object, default: float = 0) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def fields(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def read_object(path: Path) -> dict[str, object]:
    try:
        return fields(cast(object, json.loads(path.read_text(encoding="utf-8"))))
    except (OSError, UnicodeError, ValueError):
        return {}


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

    def gate(self, gate: phase_table.NoGate | phase_table.TimedGate | phase_table.UntimedGate) -> dict[str, object]:
        if isinstance(gate, phase_table.NoGate):
            return {"kind": "none"}
        result: dict[str, object] = {"kind": "untimed", "gate": gate.gate, "waits_on": gate.waits_on}
        if isinstance(gate, phase_table.TimedGate):
            result.update(kind="timed", until=self.time(gate.until))
        return result

    def phase(self, row: phase_table.DonePhase | phase_table.OpenPhase | phase_table.TodoPhase) -> dict[str, object]:
        status = "done" if isinstance(row, phase_table.DonePhase) else "open"
        gate: phase_table.NoGate | phase_table.TimedGate | phase_table.UntimedGate = phase_table.NoGate()
        start: object = None
        finish: object = None
        took: object = None
        if isinstance(row, phase_table.TodoPhase):
            gate = row.gate
            status = "todo" if isinstance(gate, phase_table.NoGate) else "gated"
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
            "start": start, "finish": finish, "gate": self.gate(gate),
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

    def finish(self, finish: phase_table.FinishedAt | phase_table.PredictedFinish | phase_table.UnknownFinish | phase_table.GateBlockedFinish) -> dict[str, object]:
        if isinstance(finish, phase_table.GateBlockedFinish):
            return {"kind": "after_gate", "gate": finish.gate, "waits_on": finish.waits_on}
        if isinstance(finish, phase_table.FinishedAt | phase_table.PredictedFinish):
            return {"kind": "finished" if isinstance(finish, phase_table.FinishedAt) else "predicted", "at": self.time(finish.at)}
        return {"kind": "unknown"}


def existing_sources(paths: set[Path]) -> list[str]:
    return sorted({str(path.resolve()) for path in paths if path.exists()})


def session_directory(run_id: str) -> Path:
    candidates = [root / run_id for root in unit_runs.run_roots()]
    return next((path for path in candidates if path.is_dir()), candidates[0])


def workers(session: Path, sources: set[Path]) -> list[dict[str, object]]:
    roster = read_object(session / "mesh_roster.json")
    sources.update((session, session / "mesh_roster.json", session / "board.log"))
    metadata = list(session.glob("impl_worker_*.json"))
    sources.update(metadata)
    by_name: dict[str, dict[str, object]] = {}
    for path in metadata:
        record = read_object(path)
        record["slot"] = text(record.get("slot")) or path.stem.removeprefix("impl_worker_")
        by_name[text(record.get("name"))] = record
    newest: dict[str, str] = {}
    try:
        lines = (session / "board.log").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        lines = []
    for line in lines:
        match = re.match(r"\S+ \[([^]]+)\] [^:]+: (.*)", line)
        if match:
            newest[match[1]] = match[2][:80]
    result: list[dict[str, object]] = []
    for name, value in roster.items():
        record = fields(value)
        meta = by_name.get(name, {})
        slot = text(meta.get("slot")) or text(record.get("slot")) or name
        result.append({"slot": slot, "role": text(record.get("role")), "state": text(record.get("status")),
                       "model": "/".join(part for part in (text(meta.get("family")), text(meta.get("model"))) if part),
                       "doing": newest.get(name, newest.get(slot, ""))})
    return result


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


def stage(events: list[dict[str, object]], state: str, rows: list[phase_table.DonePhase | phase_table.OpenPhase | phase_table.TodoPhase], current: phase_table.OpenPhase | phase_table.NoOpenPhase) -> str:
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
    step = units_table.open_step(events)
    if isinstance(step, units_table.OpenStep):
        return step.label
    if state == "run-finished":
        return "Run done"
    for row in rows:
        if isinstance(row, phase_table.TodoPhase) and not isinstance(row.gate, phase_table.NoGate):
            return f"Waiting on {row.gate.gate}"
        if not isinstance(row, phase_table.DonePhase):
            break
    return "Standing by"


def unit(cells: dict[str, str], clock: BoardClock, sources: set[Path]) -> dict[str, object]:
    value = production_units.cell_value
    worktree = Path(value(cells.get("Worktree", ""))).expanduser().resolve()
    plan_cell = value(cells.get("Plan", ""))
    plan = plan_history.resolve_plan_path(worktree, plan_cell.split(" ", 1)[0])
    sources.add(plan)
    sources.update(plan_history.plan_runs(plan))
    sources.add(plan_history.history_root() / "runs")
    if not (plan_history.history_root() / "runs").exists():
        sources.add(plan_history.history_root())
    sources.update(unit_runs.run_roots())
    found = unit_run.local_run(plan_history.history_root(), str(worktree))
    events: list[dict[str, object]] = []
    worker_rows: list[dict[str, object]] = []
    if isinstance(found, unit_run.RunFound):
        path = plan_history.history_root() / "runs" / f"{found.run_id}.jsonl"
        sources.add(path)
        events = units_table.run_events(path)
        worker_rows = workers(session_directory(found.run_id), sources)
    try:
        record = phase_table.build_plan(plan)
    except phase_table.NoPlan:
        record = phase_table.PhaseRecord(plan, datetime.fromtimestamp(clock.now, clock.zone), phase_table.NoOpenPhase(), [], phase_table.UnknownFinish(), phase_table.NothingToStart("no plan"))
    state = "standing-by"
    if isinstance(found, unit_run.RunFound):
        state = "run-finished" if events and events[-1].get("event_type") == "run_finished" else "running"
    current = record.current
    summary = next((text(event.get("activity")) for event in reversed(events) if event.get("event_type") == "progress_reported"), "")
    return {"key": value(cells.get("Unit", "")).removesuffix("-unit"), "plan": plan_cell, "state": state,
            "phase": {"id": current.phase, "of": len(record.phases), "title": current.title} if isinstance(current, phase_table.OpenPhase) else None,
            "phases": [clock.phase(row) for row in record.phases], "finish": clock.finish(record.plan_finish),
            "stage": stage(events, state, record.phases, current), "eta": clock.eta(current) if isinstance(current, phase_table.OpenPhase) else None,
            "summary": summary, "steps": phase_steps(events, clock, current), "workers": worker_rows}


def checkout(document: Path) -> Path:
    return next((path for path in document.parents if (path / ".git").exists()), document.parent.parent if document.parent.name == "docs" else document.parent)


def resolve(cwd: str, production: str) -> tuple[list[showrunners.Showrunner], str | None]:
    registered = showrunners.registered_showrunners()
    if production:
        return [runner for runner in registered if runner["slug"] == production], None
    directory = Path(cwd).expanduser().resolve()
    for runner in registered:
        document = Path(runner["doc"])
        rows = production_units.live_unit_table(document.read_text(encoding="utf-8").splitlines(), runner["slug"])
        for cells in rows:
            worktree = Path(production_units.cell_value(cells.get("Worktree", ""))).expanduser().resolve()
            if directory.is_relative_to(worktree):
                return [runner], production_units.cell_value(cells.get("Unit", "")).removesuffix("-unit")
    for runner in registered:
        if directory.is_relative_to(checkout(Path(runner["doc"]))):
            return [runner], None
    return [runner for runner in registered if runner["session"]], None


def build(runner: showrunners.Showrunner) -> dict[str, object]:
    clock = BoardClock(ZoneInfo(runner["zone"]), plan_history.now_epoch())
    document = Path(runner["doc"]).expanduser().resolve()
    sources = {document}
    rows = production_units.live_unit_table(document.read_text(encoding="utf-8").splitlines(), runner["slug"])
    offset = datetime.fromtimestamp(clock.now, clock.zone).utcoffset()
    return {"production": runner["slug"], "as_of": clock.time(clock.now), "zone": runner["zone"],
            "utc_offset_minutes": int(offset.total_seconds() / 60) if offset else 0,
            "units": [unit(cells, clock, sources) for cells in rows], "sources": existing_sources(sources)}


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
    group = parser.add_mutually_exclusive_group(required=True)
    _ = group.add_argument("--cwd", default="")
    _ = group.add_argument("--production", default="")
    args = parser.parse_args(arguments)
    runners, focus = resolve(cast(str, args.cwd), cast(str, args.production))
    root = Path(os.environ.get("UNIT_BOARD_DIR") or str(Path(os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")) / "unit-board")).expanduser().resolve()
    files: list[str] = []
    for runner in runners:
        path = root / f"{runner['slug']}.json"
        write_board(path, build(runner))
        files.append(str(path))
    print(json.dumps({"files": files, "focus": focus}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
