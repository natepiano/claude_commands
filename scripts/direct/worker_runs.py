"""Read worker activity and detached run states for the Units report."""
from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import NamedTuple, NotRequired, TypedDict, cast

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


class NamedWaitHolder(NamedTuple):
    """The holder named in a wait record."""

    label: str


class UnnamedWaitHolder(NamedTuple):
    """The wait record names no holder."""


class PhaseWait(NamedTuple):
    """An open worker or director wait in one phase instance."""

    reason: str
    started: float
    holder: NamedWaitHolder | UnnamedWaitHolder


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


def _phase_passes(events: Sequence[Mapping[str, object]], instance_id: str) -> list[Mapping[str, object]]:
    """Read open passes for one phase instance."""
    passes: dict[str, Mapping[str, object]] = {}
    for event in events:
        kind = event.get("event_type")
        pass_id = str(event.get("pass_instance_id", ""))
        if kind == "run_finished" or kind == "phase_finished" and event.get("phase_instance_id") == instance_id:
            passes.clear()
        elif kind == "pass_started" and event.get("phase_instance_id") == instance_id:
            passes[pass_id] = event
        elif kind == "pass_finished":
            _ = passes.pop(pass_id, None)
    return list(passes.values())


def phase_workers(session: Path, events: Sequence[Mapping[str, object]], instance_id: str,
                  runs: WorkerRuns) -> list[dict[str, object]]:
    """Show workers from open phase passes with their detached run states."""
    workers: dict[str, str] = {}
    pass_starts: dict[str, float] = {}
    for event in _phase_passes(events, instance_id):
        slot = str(event.get("team_slot") or event.get("worker_name") or event.get("team_role") or "")
        if not slot:
            continue
        identity = str(event.get("worker_id") or slot)
        workers[identity] = slot
        started = event.get("pass_started_at", event.get("timestamp_epoch", 0))
        if isinstance(started, int | float):
            pass_starts[identity] = float(started)
    observed = observe_session(session, dict.fromkeys(workers, "idle"), pass_starts, runs)
    return [{"slot": slot, **observed[identity],
             "state": "working" if observed[identity]["state"] == "idle" else observed[identity]["state"]}
            for identity, slot in workers.items()]


def _wait_holder(value: object) -> NamedWaitHolder | UnnamedWaitHolder:
    if isinstance(value, dict):
        fields = cast(dict[str, object], value)
        holder = fields.get("holder")
        if fields.get("state") == "NamedHolder" and isinstance(holder, dict):
            holder = cast(dict[str, object], holder)
            label = holder.get("unit") or holder.get("session_id")
            if isinstance(label, str) and label:
                return NamedWaitHolder(label)
    return UnnamedWaitHolder()


def phase_waits(events: Sequence[Mapping[str, object]], instance_id: str) -> list[PhaseWait]:
    """Read waits that remain open in one phase instance."""
    waits: dict[str, Mapping[str, object]] = {}
    for event in events:
        kind = event.get("event_type")
        wait_id = str(event.get("wait_id", ""))
        if kind == "run_finished" or kind == "phase_finished" and event.get("phase_instance_id") == instance_id:
            waits.clear()
        elif kind == "wait_started" and event.get("phase_instance_id") == instance_id:
            waits[wait_id] = event
        elif kind in ("wait_finished", "wait_unclosed"):
            _ = waits.pop(wait_id, None)
        elif kind == "wait_holder_seen" and wait_id in waits:
            waits[wait_id] = {**waits[wait_id], "holder": event.get("holder")}
        elif kind == "pass_finished":
            waits = {key: wait for key, wait in waits.items()
                     if wait.get("pass_instance_id") != event.get("pass_instance_id")}
    result: list[PhaseWait] = []
    for wait in waits.values():
        started = wait.get("wait_started_epoch", wait.get("timestamp_epoch"))
        if isinstance(started, int | float):
            result.append(PhaseWait(str(wait.get("wait_reason") or wait.get("wait_kind") or "wait"),
                                    float(started), _wait_holder(wait.get("holder"))))
    return result


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
