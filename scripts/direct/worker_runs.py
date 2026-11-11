"""Read worker activity and detached run states for the Units report."""
from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from scripts.agents.worker_lifecycle import DETACHED_RUNS_DIR
from scripts.agents.detached_runs import RunRecord
from scripts.production import session_staff, staff


type ProcessTable = set[int]


@dataclass(frozen=True)
class ProcessSnapshotFailed:
    reason: str


@dataclass(frozen=True)
class ProcessSnapshotUnread:
    pass


class WorkerRun(TypedDict):
    label: str
    started_at: float


class WorkerRunState(TypedDict):
    state: str
    run: NotRequired[WorkerRun]


def parse_process_table(ps_output: str) -> ProcessTable:
    """Parse process identities from the shared process snapshot."""
    return {int(line.strip()) for line in ps_output.splitlines() if line.strip().isdigit() and int(line.strip()) > 0}


def read_process_table() -> ProcessTable | ProcessSnapshotFailed:
    """Take one process snapshot for all shown runs."""
    try:
        result = subprocess.run(["ps", "-A", "-o", "pid="], capture_output=True,
                                text=True, check=False)
    except OSError as error:
        return ProcessSnapshotFailed(str(error))
    if result.returncode != 0:
        return ProcessSnapshotFailed(result.stderr.strip() or f"ps exited {result.returncode}")
    return parse_process_table(result.stdout)


@dataclass
class WorkerRuns:
    """Share one lazy process snapshot and each session's runs across the command."""

    now: float
    process_reader: Callable[[], ProcessTable | ProcessSnapshotFailed] = read_process_table
    snapshot: ProcessTable | ProcessSnapshotFailed | ProcessSnapshotUnread = field(default_factory=ProcessSnapshotUnread)
    sessions: dict[Path, dict[str, RunRecord]] = field(default_factory=dict)

    def processes(self) -> ProcessTable | ProcessSnapshotFailed:
        if isinstance(self.snapshot, ProcessSnapshotUnread):
            self.snapshot = self.process_reader()
        return self.snapshot

    def session_runs(self, session: Path) -> dict[str, RunRecord]:
        if session not in self.sessions:
            self.sessions[session] = _session_runs(session)
        return self.sessions[session]


def _session_runs(session: Path) -> dict[str, RunRecord]:
    newest: dict[str, RunRecord] = {}
    for path in sorted((session / DETACHED_RUNS_DIR).glob("*.json")):
        try:
            value = cast(object, json.loads(path.read_text(encoding="utf-8")))
            if not isinstance(value, dict):
                continue
            row = cast(dict[str, object], value)
            if (row.get("status") not in ("running", "finished")
                    or not all(isinstance(row.get(key), str) for key in ("run_id", "staff_id", "label", "log"))
                    or not isinstance(row.get("started_at"), int | float)
                    or not isinstance(row.get("pid"), int)):
                continue
            if (any(key in row and not isinstance(row[key], str) for key in ("delivery", "finished_at"))
                    or ("exit_code" in row and not isinstance(row["exit_code"], int))):
                continue
            run = cast(RunRecord, cast(object, row))
            current = newest.get(run["staff_id"])
            if current is None or run["started_at"] > current["started_at"]:
                newest[run["staff_id"]] = run
        except (OSError, UnicodeError, ValueError):
            continue
    return newest


def _finished_epoch(value: str) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0


def observe_session(session: Path, pane_states: Mapping[str, str], pass_starts: Mapping[str, float],
                    runs: WorkerRuns) -> dict[str, WorkerRunState]:
    """Match runs by staff id and apply worker state priority."""
    records = runs.session_runs(session)
    result: dict[str, WorkerRunState] = {}
    for staff_id, pane_state in pane_states.items():
        if pane_state == "closed":
            result[staff_id] = WorkerRunState(state="closed")
            continue
        run = records.get(staff_id)
        row = WorkerRunState(state=pane_state)
        if run is not None and run["status"] == "running":
            if pane_state not in ("working", "running", "starting"):
                processes = runs.processes()
                row["state"] = ("waiting on run" if isinstance(processes, ProcessSnapshotFailed)
                                or run["pid"] in processes else "run gone")
                row["run"] = WorkerRun(label=run["label"], started_at=run["started_at"])
        elif (run is not None and run.get("delivery", "").startswith("queued")
              and pass_starts.get(staff_id, 0) <= _finished_epoch(run.get("finished_at", ""))):
            row["state"] = "result queued"
            row["run"] = WorkerRun(label=run["label"], started_at=run["started_at"])
        if pane_state in ("working", "running", "starting"):
            row = WorkerRunState(state="working")
        result[staff_id] = row
    return result


def observe_pane_rows(session: Path, pane_rows: Sequence[Mapping[str, object]], record: staff.StaffSnapshot,
                      owner_id: staff.StaffId, events: Sequence[Mapping[str, object]],
                      runs: WorkerRuns) -> list[dict[str, object]]:
    """Match pane rows and pass starts to staff before applying detached run states."""
    rows = [dict(row) for row in pane_rows]
    members = {row.name: str(row.id) for row in record.find(kind="worker", parent=owner_id)}
    states: dict[str, str] = {}
    for row in rows:
        slot, state = row.get("slot"), row.get("state")
        if isinstance(slot, str) and slot in members:
            states[members[slot]] = state if isinstance(state, str) else ""
    pass_starts: dict[str, float] = {}
    for event in events:
        if event.get("event_type") != "pass_started":
            continue
        slot = event.get("team_slot") or event.get("team_role")
        identity = event.get("worker_id") or (members.get(slot, "") if isinstance(slot, str) else "")
        started = event.get("pass_started_at") or event.get("timestamp_epoch")
        if (isinstance(identity, str) and identity and isinstance(started, int | float)
                and not isinstance(started, bool)):
            pass_starts[identity] = max(pass_starts.get(identity, 0), float(started))
    observed = observe_session(session, states, pass_starts, runs)
    for row in rows:
        slot = row.get("slot")
        identity = members.get(slot, "") if isinstance(slot, str) else ""
        if identity in observed:
            _ = row.pop("run", None)
            row.update(observed[identity])
    return rows


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
                print(f"worker_runs: {worker.row.name}: {state}: {reason}", file=sys.stderr)
            rows.append({"slot": worker.row.name, "role": "", "state": state,
                         "model": "", "effort": "", "doing": ": ".join(part for part in (pane, reason) if part)})
        result[parent] = rows
    return result
