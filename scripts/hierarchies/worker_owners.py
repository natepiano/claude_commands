#!/usr/bin/env python3
"""Report worker ownership; recover provenance before proposing any worker end.

Report is read-only. Recovery stores evidence without rebuilding deleted run
folders. Dry runs write nothing. The first applied end only records a report.
Missing sources prevent all applied ends.
Text reports end with Totals. Use --full to list each timer registration.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import fcntl
import json
import os
from pathlib import Path
import re
from stat import S_ISREG
import subprocess
import sys
from typing import cast

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS.parent))
for directory in ("lib", "message", "production", "whoami"):
    sys.path.insert(0, str(SCRIPTS / directory))

from scripts.lib import unit_runs
from scripts.message import roster
from scripts.production import staff, session_staff
from scripts.accounts.role_map import stored_role_map


class WorkerClass(str, Enum):
    OwnedLive = "OwnedLive"
    DirectorGone = "DirectorGone"
    OwnerUnknown = "OwnerUnknown"


# codex_mesh.py LAUNCHER_ATTACHED_STATUSES and its starting transition;
# roster.py also spells running as active and waiting_capacity as waiting for capacity.
ATTACHED_WORKER_STATUSES = frozenset(("starting", "running", "active", "waiting_capacity", "waiting for capacity"))


class WorkerTurnState(str, Enum):
    Attached = "attached"
    Idle = "idle"
    Ended = "ended"
    Unrecorded = "unrecorded"

    @classmethod
    def from_status(cls, status: str) -> WorkerTurnState:
        if status in ATTACHED_WORKER_STATUSES:
            return cls.Attached
        if status == "ended":
            return cls.Ended
        if status in ("done", "idle", "finished", "paused", "failed", "capacity_exhausted", "out of capacity"):
            return cls.Idle
        return cls.Unrecorded


@dataclass(frozen=True)
class Director:
    session_id: str
    name: str
    unit: str
    worker_prefix: str = ""

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("a recorded director requires a session id")


@dataclass(frozen=True)
class MissingDirectorRecord:
    unit: str = ""


MISSING_DIRECTOR_RECORD = MissingDirectorRecord()


@dataclass(frozen=True)
class LiveDirectorOwner:
    director: Director


@dataclass(frozen=True)
class RecordedDirectorOwner:
    director: Director


@dataclass(frozen=True)
class ConflictingOwners:
    candidates: tuple[LiveDirectorOwner | RecordedDirectorOwner, ...]

    def __post_init__(self) -> None:
        if len({(item.director.session_id, item.director.unit) for item in self.candidates}) < 2:
            raise ValueError("conflicting ownership requires distinct candidate directors")


WorkerOwnership = LiveDirectorOwner | RecordedDirectorOwner | MissingDirectorRecord | ConflictingOwners


@dataclass(frozen=True)
class Worker:
    name: str
    thread_id: str
    run: str
    ownership: WorkerOwnership
    evidence: tuple[str, ...]
    recovery_attempted: bool = False
    status: WorkerTurnState = WorkerTurnState.Unrecorded

    @property
    def classification(self) -> WorkerClass:
        if isinstance(self.ownership, LiveDirectorOwner):
            return WorkerClass.OwnedLive
        if isinstance(self.ownership, RecordedDirectorOwner):
            return WorkerClass.DirectorGone
        return WorkerClass.OwnerUnknown

    @property
    def director(self) -> str:
        return self.ownership.director.session_id if isinstance(self.ownership, (LiveDirectorOwner, RecordedDirectorOwner)) else ""

    @property
    def unit(self) -> str:
        if isinstance(self.ownership, (LiveDirectorOwner, RecordedDirectorOwner)):
            return self.ownership.director.unit
        return self.ownership.unit if isinstance(self.ownership, MissingDirectorRecord) else ""




@dataclass(frozen=True)
class OwnershipSources:
    workers: list[Worker]
    directors: list[Director]
    membership: staff.StaffSnapshot
    problems: list[str]


@dataclass(frozen=True)
class EndResult:
    workers: list[Worker]
    ended: tuple[str, ...]
    evidence: tuple[str, ...]
    first_run: bool
    failed: tuple[str, ...] = ()


@dataclass(frozen=True)
class PendingEndCleanup:
    name: str
    thread_id: str
    run: str
    classification: WorkerClass
    started_at: datetime

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (self.name, self.thread_id, self.run)):
            raise ValueError("pending end needs name, thread and run")
        if self.started_at.utcoffset() != timezone.utc.utcoffset(self.started_at):
            raise ValueError("pending end time must be UTC")


@dataclass(frozen=True)
class EndCleanupFinished:
    name: str


@dataclass(frozen=True)
class EndCleanupFailed:
    name: str
    exit_code: int


@dataclass(frozen=True)
class EndCleanupDropped:
    name: str
    reason: str


@dataclass(frozen=True)
class EndCleanupKept:
    name: str
    problem: str


EndCleanupOutcome = EndCleanupFinished | EndCleanupFailed | EndCleanupDropped | EndCleanupKept


@dataclass(frozen=True)
class RouteEnded:
    pass


@dataclass(frozen=True)
class RouteGone:
    reason: str


@dataclass(frozen=True)
class RouteUnreadable:
    problem: str


@dataclass(frozen=True)
class TimerRegistration:
    name: str
    target: str
    requested_by: str
    evidence: str


JsonObject = dict[str, object]


def object_fields(value: object) -> JsonObject:
    return cast(JsonObject, value) if isinstance(value, dict) else {}


def text_field(record: JsonObject, key: str) -> str:
    value = record.get(key)
    return value if isinstance(value, str) else ""


def list_items(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []


def read_object(path: Path, problems: list[str]) -> JsonObject:
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
        if not isinstance(value, dict):
            raise ValueError("expected an object")
        return cast(JsonObject, value)
    except (OSError, ValueError, UnicodeError) as error:
        problems.append(f"{path}: {error}")
        return {}


def evidence_lines(*groups: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(line for group in groups for line in group))


def ownership_record(owner: Director | MissingDirectorRecord) -> RecordedDirectorOwner | MissingDirectorRecord:
    return RecordedDirectorOwner(owner) if isinstance(owner, Director) else owner


def classify_worker(worker: Worker, directors: Sequence[Director]) -> Worker:
    if isinstance(worker.ownership, ConflictingOwners):
        return worker
    matches = [director for director in directors if worker.director and director.session_id == worker.director]
    if matches:
        current = next((item for item in matches if item.session_id == worker.director), matches[0])
        return replace(worker, ownership=LiveDirectorOwner(current), evidence=evidence_lines(
            worker.evidence, (f"live director {current.name} ({current.session_id})",)))
    if isinstance(worker.ownership, LiveDirectorOwner):
        return replace(worker, ownership=RecordedDirectorOwner(worker.ownership.director))
    return worker


def merge_observations(records: Sequence[Worker], directors: Sequence[Director]) -> Worker:
    """Keep all current ownership claims without calling them recovery."""
    candidates: list[Worker] = []
    for record in records:
        units = {item.unit for item in records if record.director and item.director == record.director and item.unit}
        if not record.unit and len(units) == 1 and isinstance(record.ownership, (LiveDirectorOwner, RecordedDirectorOwner)):
            owner = replace(record.ownership.director, unit=next(iter(units)))
            record = replace(record, ownership=RecordedDirectorOwner(owner))
        candidates.append(classify_worker(record, directors))
    supporting = evidence_lines(*(item.evidence for item in candidates), tuple(
        f"recorded ownership: thread={item.thread_id}; run={item.run or 'unknown'}; "
        + f"unit={item.unit or 'unknown'}; director={item.director or 'unknown'}" for item in records))
    owners: dict[tuple[str, str], LiveDirectorOwner | RecordedDirectorOwner] = {}
    for item in candidates:
        if isinstance(item.ownership, (LiveDirectorOwner, RecordedDirectorOwner)):
            owners[(item.director, item.unit)] = item.ownership
    ownership: WorkerOwnership = candidates[0].ownership
    if len(owners) > 1:
        ownership = ConflictingOwners(tuple(owners.values()))
        supporting = evidence_lines(supporting, ("conflicting ownership evidence; worker kept",))
    elif owners:
        ownership = next(iter(owners.values()))
    status = WorkerTurnState.Unrecorded
    if any(item.status == WorkerTurnState.Attached for item in candidates):
        status = WorkerTurnState.Attached
    elif any(item.status == WorkerTurnState.Ended for item in candidates):
        status = WorkerTurnState.Ended
    elif any(item.status == WorkerTurnState.Idle for item in candidates):
        status = WorkerTurnState.Idle
    base = next((item for item in candidates if item.run), candidates[0])
    return replace(base, ownership=ownership, evidence=supporting, status=status)


def _staff_director(record: staff.StaffSnapshot, member: staff.StaffRow | None) -> Director | MissingDirectorRecord:
    parent = record.parent_of(member) if member is not None and member.kind == "worker" else None
    return (Director(parent.current_session, parent.name, parent.name if parent.kind == "unit" else "")
            if parent is not None else MISSING_DIRECTOR_RECORD)


def recover_worker(worker: Worker, record: staff.StaffSnapshot, directors: Sequence[Director]) -> Worker:
    member = record.by_current_session(worker.thread_id)
    owner = ownership_record(_staff_director(record, member))
    current = classify_worker(replace(worker, ownership=owner), directors)
    return replace(current, recovery_attempted=True, evidence=evidence_lines(current.evidence,
        ("ownership read from staff" if not isinstance(owner, MissingDirectorRecord) else "worker owner is unavailable in staff",)))




def source_files(root: Path, pattern: str, problems: list[str]) -> list[Path]:
    try:
        if not root.is_dir():
            problems.append(f"{root}: source directory missing")
            return []
        return sorted(root.glob(pattern))
    except OSError as error:
        problems.append(f"{root}: {error}")
        return []






def state_directory() -> Path:
    return Path(os.environ.get("WORKER_OWNERS_STATE", str(Path.home() / ".local/state/worker-owners")))






def collect_sources() -> OwnershipSources:
    with stored_role_map():
        observed = roster.roster(os.environ, roster.ThisMachine())
    problems = list(observed.problems)
    try:
        membership = staff.local_snapshot()
    except staff.StaffUnreadable as error:
        problems.append(f"staff: {error}")
        membership = staff.StaffSnapshot(())
    directors: list[Director] = []
    for entry in observed.entries:
        if isinstance(entry.session, roster.ClaudeSession):
            unit = entry.director.unit if isinstance(entry.director, roster.DirectsUnit) else ""
            prefix = re.sub(r"[^A-Za-z0-9._-]", "-", Path(entry.cwd).name)[:40] if entry.cwd else ""
            directors.append(Director(entry.session.session_id, entry.name, unit, prefix))
    roots = unit_runs.run_roots(os.environ.get("ROSTER_RUN_ROOT"))
    excluded = {entry.thread.thread_id for entry in observed.entries
                if entry.role.startswith("astra-") and isinstance(entry.thread, roster.CodexThread)}
    observations: dict[str, list[Worker]] = {}
    mesh_paths = [path for root in roots for path in source_files(root, "*/mesh_roster.json", problems)]
    for path in mesh_paths:
        for name, raw in read_object(path, problems).items():
            record = object_fields(raw)
            member = session_staff.worker_row(record, membership)
            thread = session_staff.worker_session(record, membership)
            if record.get("held_design_id"):
                if thread:
                    excluded.add(thread)
                continue
            if record.get("status") == "ended" or thread in excluded:
                continue
            if not thread:
                problems.append(f"{path}: {name}: thread id missing")
                continue
            identity = _staff_director(membership, member)
            ownership = ownership_record(identity)
            observations.setdefault(thread, []).append(Worker(name, thread, str(path.parent), ownership,
                (f"mesh roster {path}", f"recorded status {text_field(record, 'status')}"),
                status=WorkerTurnState.from_status(text_field(record, "status"))))
    worker_threads = set(observations)
    for entry in observed.entries:
        if entry.kind != "codex" or not isinstance(entry.thread, roster.CodexThread) or entry.thread.thread_id in excluded:
            continue
        if entry.role != "worker" and entry.thread.thread_id not in worker_threads:
            continue
        member = membership.by_current_session(entry.thread.thread_id)
        run = entry.assignment.session_dir if isinstance(entry.assignment, roster.WorkerAssignment) else ""
        identity = _staff_director(membership, member)
        observations.setdefault(entry.thread.thread_id, []).append(Worker(entry.name, entry.thread.thread_id, run,
            ownership_record(identity), ("live session roster",), status=WorkerTurnState.from_status(entry.status)))
    classified: list[Worker] = []
    for thread, records in observations.items():
        if thread in excluded:
            continue
        worker = merge_observations(records, directors)
        classified.append(worker)
    return OwnershipSources(sorted(classified, key=lambda item: (item.run, item.name)), directors, membership, problems)


def recover_sources(sources: OwnershipSources) -> OwnershipSources:
    return replace(sources, workers=[recover_worker(worker, sources.membership, sources.directors) for worker in sources.workers])


def write_json(path: Path, value: object) -> None:
    staged = path.with_suffix(f".{os.getpid()}.tmp")
    with staged.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2)
        handle.writelines(("\n",))
    os.replace(staged, path)




@dataclass(frozen=True)
class WorkerKept:
    reason: str


@dataclass(frozen=True)
class EndEligible:
    pass




def end_decision(worker: Worker, selected_class: WorkerClass, problems: Sequence[str], first_run: bool, membership: staff.StaffSnapshot) -> WorkerKept | EndEligible:
    if isinstance(worker.ownership, LiveDirectorOwner):
        return WorkerKept("unit director is live")
    if isinstance(worker.ownership, ConflictingOwners):
        return WorkerKept("conflicting ownership evidence")
    if worker.status == WorkerTurnState.Attached:
        return WorkerKept("worker has an attached turn or launcher")
    if worker.status == WorkerTurnState.Ended:
        return WorkerKept("worker already ended")
    if worker.status == WorkerTurnState.Unrecorded:
        return WorkerKept("worker turn state is unrecorded")
    if worker.classification != selected_class:
        return WorkerKept("ownership class was not selected")
    if selected_class == WorkerClass.OwnerUnknown and not worker.recovery_attempted:
        return WorkerKept("ownership recovery has not run")
    if problems:
        return WorkerKept("source problems")
    route_problems: list[str] = []
    record = object_fields(read_object(Path(worker.run) / "mesh_roster.json", route_problems).get(worker.name)) if worker.run else {}
    if route_problems or session_staff.worker_session(record, membership) != worker.thread_id:
        return WorkerKept("mesh route missing or thread changed")
    status = WorkerTurnState.from_status(text_field(record, "status"))
    if status == WorkerTurnState.Ended:
        return WorkerKept("mesh route already ended")
    if status == WorkerTurnState.Attached:
        return WorkerKept("worker has an attached turn or launcher")
    if status == WorkerTurnState.Unrecorded:
        return WorkerKept("mesh turn state is unrecorded")
    if first_run:
        return WorkerKept("first applied run reports only")
    return EndEligible()


def mesh_end(worker: Worker) -> int | WorkerKept:
    fresh = recover_sources(collect_sources())
    matching = [item for item in fresh.workers if item.thread_id == worker.thread_id]
    if len(matching) != 1 or matching[0].classification != worker.classification:
        return WorkerKept("worker ownership or thread changed")
    decision = end_decision(matching[0], worker.classification, fresh.problems, False, fresh.membership)
    if isinstance(decision, WorkerKept):
        return decision
    # Check the requested route too; source collection may have found a different run.
    decision = end_decision(worker, worker.classification, fresh.problems, False, fresh.membership)
    if isinstance(decision, WorkerKept):
        return decision
    member = fresh.membership.by_current_session(worker.thread_id)
    if member is None or member.kind != "worker":
        return WorkerKept("worker staff row changed")
    # models-unit must make the handler compare under its mutation lock before any side effect.
    # Older handlers reject these flags; never retry an end by name alone.
    completed = subprocess.run([sys.executable, str(SCRIPTS / "agents/codex_mesh.py"), "end",
                                "--session-dir", worker.run, "--to", worker.name,
                                "--expected-thread-id", worker.thread_id, "--idle-only"],
                               check=False, timeout=45, capture_output=True, text=True)
    if completed.returncode:
        return completed.returncode
    problems: list[str] = []
    current = object_fields(read_object(Path(worker.run) / "mesh_roster.json", problems).get(worker.name))
    if current.get("staff_id") == member.id and current.get("status") == "ended":
        return 0 if not problems else 3
    return 0 if not problems and session_staff.worker_session(current) == worker.thread_id and current.get("status") == "ended" else 3


def worker_record(worker: Worker) -> JsonObject:
    ownership: JsonObject = {"state": type(worker.ownership).__name__}
    if isinstance(worker.ownership, ConflictingOwners):
        ownership["candidates"] = [{**asdict(item.director), "live": isinstance(item, LiveDirectorOwner)}
                                   for item in worker.ownership.candidates]
    elif isinstance(worker.ownership, (LiveDirectorOwner, RecordedDirectorOwner)):
        ownership["director"] = asdict(worker.ownership.director)
    return {"name": worker.name, "thread_id": worker.thread_id, "run": worker.run, "unit": worker.unit,
            "director": worker.director, "classification": worker.classification.value, "ownership": ownership,
            "evidence": worker.evidence, "recovery_attempted": worker.recovery_attempted, "status": worker.status.value}


def pending_end_record(record: PendingEndCleanup) -> JsonObject:
    return {"name": record.name, "thread_id": record.thread_id, "run": record.run,
            "classification": record.classification.value, "started_at": record.started_at.isoformat()}


def read_pending_ends(state_dir: Path, problems: list[str]) -> list[PendingEndCleanup]:
    path = state_dir / "end-cleanup.json"
    if not path.exists():
        return []
    document = read_object(path, problems)
    if not isinstance(document.get("pending"), list):
        problems.append(f"{path}: pending must be a list")
        return []
    pending: list[PendingEndCleanup] = []
    for index, raw in enumerate(list_items(document.get("pending"))):
        try:
            fields = object_fields(raw)
            pending.append(PendingEndCleanup(
                text_field(fields, "name"), text_field(fields, "thread_id"), text_field(fields, "run"),
                WorkerClass(text_field(fields, "classification")),
                datetime.fromisoformat(text_field(fields, "started_at"))))
        except ValueError as error:
            problems.append(f"{path}: pending[{index}]: {error}")
    return pending


def write_pending_ends(state_dir: Path, pending: Sequence[PendingEndCleanup]) -> None:
    write_json(state_dir / "end-cleanup.json", {"pending": [pending_end_record(record) for record in pending]})


def pending_route_problem(record: PendingEndCleanup) -> RouteEnded | RouteGone | RouteUnreadable:
    path = Path(record.run) / "mesh_roster.json"
    try:
        if not S_ISREG(path.stat().st_mode):
            return RouteUnreadable(f"{path}: roster is not a regular file")
    except FileNotFoundError:
        return RouteGone("mesh route missing")
    except OSError as error:
        return RouteUnreadable(f"{path}: {error}")
    problems: list[str] = []
    route = object_fields(read_object(path, problems).get(record.name))
    if problems:
        try:
            if not S_ISREG(path.stat().st_mode):
                return RouteUnreadable(f"{path}: roster is not a regular file")
        except FileNotFoundError:
            return RouteGone("mesh route missing")
        except OSError as error:
            problems.append(f"{path}: {error}")
        return RouteUnreadable("; ".join(problems))
    if not route:
        return RouteGone("mesh route missing")
    reference = text_field(route, "staff_id")
    if (reference and route.get("status") == "ended"
            and staff.local_snapshot().by_id(staff.StaffId(reference)) is None):
        return RouteGone("worker retirement confirms completed end cleanup")
    if problems:
        return RouteUnreadable("; ".join(problems))
    if session_staff.worker_session(route) != record.thread_id:
        return RouteGone("mesh thread changed")
    if route.get("status") != "ended":
        return RouteGone("mesh route is not ended")
    return RouteEnded()


def mesh_cleanup(record: PendingEndCleanup) -> int:
    completed = subprocess.run([sys.executable, str(SCRIPTS / "agents/codex_mesh.py"), "end",
                                "--session-dir", record.run, "--to", record.name,
                                "--expected-thread-id", record.thread_id, "--cleanup-only"],
                               check=False, timeout=45, capture_output=True, text=True)
    return completed.returncode


def cleanup_note(outcome: EndCleanupOutcome) -> str:
    if isinstance(outcome, EndCleanupFinished):
        return "end cleanup finished"
    if isinstance(outcome, EndCleanupFailed):
        return f"end cleanup failed: exit {outcome.exit_code}"
    if isinstance(outcome, EndCleanupKept):
        return f"end cleanup kept: roster unreadable: {outcome.problem}"
    return f"end cleanup dropped: {outcome.reason}"


def log_end_outcome(state_dir: Path, record: JsonObject, outcome: str) -> None:
    with (state_dir / "ends.jsonl").open("a", encoding="utf-8") as log:
        json.dump({"at": datetime.now(timezone.utc).isoformat(), "worker": record, "outcome": outcome}, log)
        log.writelines(("\n",))


def finish_pending_ends(state_dir: Path, cleanup: Callable[[PendingEndCleanup], int]) -> tuple[EndCleanupOutcome, ...]:
    """Finish recorded ends; the caller holds end.lock for every journal write."""
    problems: list[str] = []
    pending = read_pending_ends(state_dir, problems)
    outcomes: list[EndCleanupOutcome] = []
    for record in tuple(pending):
        route = pending_route_problem(record)
        outcome: EndCleanupOutcome
        if isinstance(route, RouteGone):
            outcome = EndCleanupDropped(record.name, route.reason)
        elif isinstance(route, RouteUnreadable):
            outcome = EndCleanupKept(record.name, route.problem)
        else:
            try:
                result = cleanup(record)
            except subprocess.TimeoutExpired:
                result = 124
            except OSError:
                result = 1
            route = pending_route_problem(record)
            if isinstance(route, RouteUnreadable):
                outcome = EndCleanupKept(record.name, route.problem)
            elif result == 0 and isinstance(route, RouteEnded):
                outcome = EndCleanupFinished(record.name)
            else:
                outcome = EndCleanupFailed(record.name, result or 3)
        log_end_outcome(state_dir, pending_end_record(record), cleanup_note(outcome))
        if isinstance(outcome, (EndCleanupFinished, EndCleanupDropped)):
            pending.remove(record)
        write_pending_ends(state_dir, pending)
        outcomes.append(outcome)
    return tuple(outcomes)


def end_workers(sources: OwnershipSources, selected_class: WorkerClass, state_dir: Path, apply: bool = False,
                end_worker: Callable[[Worker], int | WorkerKept] = mesh_end,
                cleanup: Callable[[PendingEndCleanup], int] = mesh_cleanup) -> EndResult:
    if selected_class not in (WorkerClass.DirectorGone, WorkerClass.OwnerUnknown):
        raise ValueError("only DirectorGone and OwnerUnknown may be selected")
    recovered = recover_sources(sources)
    notes: list[str] = []
    ended: list[str] = []
    failed: list[str] = []
    marker = state_dir / "first-report.json"
    if not apply:
        for record in read_pending_ends(state_dir, recovered.problems):
            notes.append(f"{record.name}: would finish end cleanup")
        for worker in recovered.workers:
            decision = end_decision(worker, selected_class, recovered.problems, not marker.exists(), recovered.membership)
            notes.append(f"{worker.name}: would keep: {decision.reason}" if isinstance(decision, WorkerKept)
                         else f"{worker.name}: would end")
        return EndResult(recovered.workers, (), tuple(notes), not marker.exists())
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "end.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        pending = read_pending_ends(state_dir, recovered.problems)
        cleanup_outcomes = finish_pending_ends(state_dir, cleanup)
        for record, cleanup_outcome in zip(pending, cleanup_outcomes, strict=True):
            notes.append(f"{cleanup_outcome.name}: {cleanup_note(cleanup_outcome)}")
            if isinstance(cleanup_outcome, (EndCleanupFailed, EndCleanupKept)):
                failed.append(record.thread_id)
        first_run = not marker.exists()
        if first_run:
            write_json(marker, {"at": datetime.now(timezone.utc).isoformat(), "workers": [worker_record(item) for item in recovered.workers]})
            notes.append("first applied run: report only; no worker ended")
        if recovered.problems:
            notes.append("source problems: no worker may be ended")
        for worker in recovered.workers:
            if worker.classification != selected_class:
                continue
            decision = end_decision(worker, selected_class, recovered.problems, first_run, recovered.membership)
            if isinstance(decision, WorkerKept):
                outcome = f"kept: {decision.reason}"
            else:
                pending = read_pending_ends(state_dir, recovered.problems)
                record = PendingEndCleanup(worker.name, worker.thread_id, worker.run, worker.classification,
                                           datetime.now(timezone.utc))
                pending.append(record)
                write_pending_ends(state_dir, pending)
                completed_end = False
                try:
                    result = end_worker(worker)
                    if isinstance(result, WorkerKept):
                        outcome = f"kept: {result.reason}"
                    else:
                        outcome = f"end exit {result}"
                        if result == 0:
                            completed_end = True
                            ended.append(worker.thread_id)
                        else:
                            failed.append(worker.thread_id)
                except (OSError, subprocess.TimeoutExpired) as error:
                    outcome = f"end failed: {error}"
                    failed.append(worker.thread_id)
                route = pending_route_problem(record)
                if not completed_end and not isinstance(route, RouteGone) and worker.thread_id not in failed:
                    failed.append(worker.thread_id)
                if not completed_end and isinstance(route, RouteUnreadable):
                    outcome = cleanup_note(EndCleanupKept(worker.name, route.problem))
                if completed_end or isinstance(route, RouteGone):
                    pending.remove(record)
            notes.append(f"{worker.name}: {outcome}")
            log_end_outcome(state_dir, worker_record(worker), outcome)
            if isinstance(decision, EndEligible):
                write_pending_ends(state_dir, pending)
    return EndResult(recovered.workers, tuple(ended), tuple(notes), first_run, tuple(failed))


def timer_registrations(directory: Path, problems: list[str]) -> list[TimerRegistration]:
    registrations: list[TimerRegistration] = []
    for path in source_files(directory, "*/conf", problems):
        try:
            fields = dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if "=" in line)
            requested = fields.get("REQUESTED_BY", "") or fields.get("APPROVED_BY", "")
            registrations.append(TimerRegistration(path.parent.name, fields.get("TARGET", ""), requested,
                                                   str(path) if requested else "request provenance not recorded; showrunner review required"))
        except (OSError, UnicodeError) as error:
            problems.append(f"{path}: {error}")
    return registrations


def main(arguments: Sequence[str] = ()) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    command_option = parser.add_argument("command", choices=("report", "recover", "end"))
    json_option = parser.add_argument("--json", action="store_true")
    full_option = parser.add_argument("--full", action="store_true", help="list each timer registration")
    class_option = parser.add_argument("--class", dest="selected_class", choices=("DirectorGone", "OwnerUnknown"))
    mode = parser.add_mutually_exclusive_group()
    dry_run_option = mode.add_argument("--dry-run", action="store_false", dest="apply")
    dry_run_option.default = False
    apply_option = mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(arguments)
    command = cast(str, getattr(args, command_option.dest))
    selected_class = cast(str, getattr(args, class_option.dest))
    apply = cast(bool, getattr(args, apply_option.dest))
    as_json = cast(bool, getattr(args, json_option.dest))
    full = cast(bool, getattr(args, full_option.dest))
    if command == "end" and not selected_class:
        parser.error("end requires --class DirectorGone|OwnerUnknown")
    sources = collect_sources()
    timers = timer_registrations(Path(os.environ.get("NOTIFIER_STATE_DIR", str(Path.home() / ".local/state/notifier"))), sources.problems)
    notes: tuple[str, ...] = ()
    end_failed = False
    if command == "recover":
        sources = recover_sources(sources)
    elif command == "end":
        result = end_workers(sources, WorkerClass(selected_class), state_directory(), apply=apply)
        sources = replace(sources, workers=result.workers)
        notes = result.evidence
        end_failed = bool(result.failed)
    pending = read_pending_ends(state_directory(), sources.problems)
    totals = {kind.value: sum(item.classification == kind for item in sources.workers) for kind in WorkerClass}
    if as_json:
        print(json.dumps({"workers": [worker_record(item) for item in sources.workers], "totals": totals,
                          "end_cleanup_pending": [pending_end_record(record) for record in pending],
                          "problems": sources.problems, "timers": [asdict(item) for item in timers], "notes": notes}, indent=2))
    else:
        for worker in sources.workers:
            print(f"{worker.name}\t{worker.classification.value}\trun={worker.run or 'unknown'}\tunit={worker.unit or 'unknown'}\tdirector={worker.director or 'unknown'}\tthread={worker.thread_id}")
            if isinstance(worker.ownership, ConflictingOwners):
                for candidate in worker.ownership.candidates:
                    print(f"  candidate owner: {candidate.director.session_id}; unit={candidate.director.unit}; live={isinstance(candidate, LiveDirectorOwner)}")
            for evidence in worker.evidence:
                print(f"  evidence: {evidence}")
        for note in notes:
            print(note)
        for problem in sources.problems:
            print(f"source problem: {problem}")
        if full:
            for timer in timers:
                print(f"timer {timer.name}\ttarget={timer.target}\trequested_by={timer.requested_by or 'unknown'}\t{timer.evidence}")
        else:
            missing_requests = sum(not timer.requested_by for timer in timers)
            print(f"Timers: {len(timers)} registered, {missing_requests} with no recorded request (the showrunner checks these; --full lists each)")
        print("Totals: " + ", ".join(f"{kind}={count}" for kind, count in totals.items()) +
              f", source_problems={len(sources.problems)}, end_cleanup_pending={len(pending)}")
    return 1 if command == "end" and (sources.problems or end_failed) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
