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
from scripts.accounts.role_map import stored_role_map


class WorkerClass(str, Enum):
    OwnedLive = "OwnedLive"
    PooledReviewer = "PooledReviewer"
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
class PooledReviewerOwner:
    pass


@dataclass(frozen=True)
class ConflictingOwners:
    candidates: tuple[LiveDirectorOwner | RecordedDirectorOwner, ...]

    def __post_init__(self) -> None:
        if len({(item.director.session_id, item.director.unit) for item in self.candidates}) < 2:
            raise ValueError("conflicting ownership requires distinct candidate directors")


WorkerOwnership = LiveDirectorOwner | RecordedDirectorOwner | MissingDirectorRecord | PooledReviewerOwner | ConflictingOwners


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
        if isinstance(self.ownership, PooledReviewerOwner):
            return WorkerClass.PooledReviewer
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
class OwnershipEvidence:
    name: str
    thread_id: str
    run: str
    owner: Director | MissingDirectorRecord
    evidence: tuple[str, ...]
    pooled: bool = False

    @classmethod
    def from_worker(cls, worker: Worker) -> OwnershipEvidence:
        if isinstance(worker.ownership, ConflictingOwners):
            raise ValueError("conflicting owners cannot become a single ownership record")
        owner = worker.ownership.director if isinstance(worker.ownership, (LiveDirectorOwner, RecordedDirectorOwner)) else MissingDirectorRecord(worker.unit)
        return cls(worker.name, worker.thread_id, worker.run, owner, worker.evidence,
                   worker.classification == WorkerClass.PooledReviewer)

    def worker(self) -> Worker:
        ownership = PooledReviewerOwner() if self.pooled else ownership_record(self.owner)
        return Worker(self.name, self.thread_id, self.run, ownership, self.evidence)


@dataclass(frozen=True)
class OwnershipSources:
    workers: list[Worker]
    directors: list[Director]
    provenance: list[OwnershipEvidence]
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
    if isinstance(worker.ownership, (ConflictingOwners, PooledReviewerOwner)):
        return worker
    matches = [director for director in directors if (
        (worker.director and director.session_id == worker.director)
        or (worker.unit and director.unit == worker.unit)
        or (isinstance(worker.ownership, MissingDirectorRecord) and director.unit and director.worker_prefix
            and worker.name.startswith(director.worker_prefix + "-"))
    )]
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
    if any(isinstance(item.ownership, PooledReviewerOwner) for item in candidates):
        ownership = PooledReviewerOwner()
    elif len(owners) > 1:
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


def recover_worker(worker: Worker, provenance: Sequence[OwnershipEvidence], directors: Sequence[Director]) -> Worker:
    current = classify_worker(worker, directors)
    if not isinstance(current.ownership, MissingDirectorRecord):
        return current
    matches = [item for item in provenance if worker.thread_id and item.thread_id == worker.thread_id]
    if not matches:
        return replace(current, recovery_attempted=True, evidence=evidence_lines(current.evidence,
            ("no owner found in shutdown history, message log or launcher records",)))
    recovered = merge_observations([current, *(item.worker() for item in matches)], directors)
    recovered = replace(recovered, recovery_attempted=True, status=current.status)
    if isinstance(recovered.ownership, MissingDirectorRecord):
        return replace(recovered, evidence=evidence_lines(recovered.evidence,
            ("no owner found in shutdown history, message log or launcher records",)))
    if isinstance(recovered.ownership, ConflictingOwners):
        return recovered
    missing = (f"recorded run folder missing: {recovered.run}; known case: 2026-10-28 run-folder loss; deletion date unrecorded",) if recovered.run and not Path(recovered.run).is_dir() else ()
    return replace(recovered, evidence=evidence_lines(recovered.evidence, missing, ("ownership recovered by thread id",)))


def provenance_records(value: object, source: str, inherited: Director | MissingDirectorRecord = MISSING_DIRECTOR_RECORD,
                       run: str = "", worker_context: bool = False) -> list[OwnershipEvidence]:
    """Read shutdown seats, worker snapshots and launcher records by thread id."""
    if isinstance(value, list):
        return [worker for item in list_items(cast(object, value)) for worker in provenance_records(item, source, inherited, run, worker_context)]
    record = object_fields(value)
    if not record:
        return []
    session = object_fields(record.get("session"))
    if session.get("kind") == "worker":
        return provenance_records(session, source, inherited, run, True)
    host = object_fields(session.get("host"))
    session_id = text_field(session, "session_id")
    identity = Director(session_id, text_field(session, "name"), text_field(host, "unit")) if session_id else inherited
    owner = object_fields(record.get("owner"))
    director = text_field(owner, "session_id") or text_field(record, "director_session_id") or text_field(record, "director") or (identity.session_id if isinstance(identity, Director) else "")
    unit = text_field(record, "unit") or identity.unit
    run_path = text_field(record, "run_dir") or text_field(record, "session_dir") or run
    thread = text_field(record, "thread_id")
    if not thread and record.get("family") == "codex":
        thread = text_field(record, "worker_id")
    if not thread and record.get("kind") == "worker":
        thread = text_field(record, "session_id")
    found: list[OwnershipEvidence] = []
    worker_named = (worker_context or bool(text_field(record, "seat")) or record.get("kind") == "worker"
                    or (record.get("family") == "codex" and bool(text_field(record, "worker_id")))
                    or bool(text_field(record, "director_session_id")) or bool(text_field(owner, "session_id")))
    if thread and worker_named:
        director_record = (replace(identity, unit=unit) if isinstance(identity, Director) and identity.session_id == director
                           else Director(director, "", unit)) if director else MissingDirectorRecord(unit)
        found.append(OwnershipEvidence(text_field(record, "seat") or text_field(record, "name"), thread,
                                       run_path, director_record, (source,)))
    for key, item in record.items():
        if key not in ("session", "owner") and isinstance(item, (dict, list)):
            found.extend(provenance_records(cast(object, item), source, identity, run_path,
                                             worker_context or key in ("codex_seats", "workers", "seats")))
    return found


def source_files(root: Path, pattern: str, problems: list[str]) -> list[Path]:
    try:
        if not root.is_dir():
            problems.append(f"{root}: source directory missing")
            return []
        return sorted(root.glob(pattern))
    except OSError as error:
        problems.append(f"{root}: {error}")
        return []


def message_ownership_records(record: JsonObject, source: str, directors: Sequence[Director],
                              routes: Sequence[OwnershipEvidence]) -> list[OwnershipEvidence]:
    """Join a director's worker address to recorded worker thread identities."""
    address = text_field(record, "to")
    if not address.startswith("worker:"):
        return []
    run_key, separator, name = address.removeprefix("worker:").rpartition("/")
    if not separator or not run_key or not name:
        return []
    sender = text_field(record, "from")
    owners = {item.session_id: item for item in directors if item.unit and sender in (item.name, f"session:{item.session_id}")}
    if len(owners) != 1:
        return []
    matching = [item for item in routes if item.name == name and item.run and
                (item.run == run_key if Path(run_key).is_absolute() else Path(item.run).name == run_key)]
    if len({item.run for item in matching}) != 1:
        return []
    owner = next(iter(owners.values()))
    return [OwnershipEvidence(item.name, item.thread_id, item.run, owner, (source,)) for item in matching]


def load_provenance(roots: Sequence[Path], directors: Sequence[Director], problems: list[str],
                    routes: Sequence[OwnershipEvidence]) -> list[OwnershipEvidence]:
    found: list[OwnershipEvidence] = []
    shutdown = Path(os.environ.get("SHUTDOWN_STATE_DIR", str(Path.home() / ".local/state/shutdown")))
    for path in source_files(shutdown, "**/*.json", problems):
        found.extend(provenance_records(read_object(path, problems), f"shutdown record {path}"))
    for root in roots:
        for path in source_files(root, "**/impl_worker_*.json", problems):
            owner = unit_runs.director(path.parent)
            identity = next((item for item in directors if item.session_id == owner),
                            Director(owner, "", "") if owner else MISSING_DIRECTOR_RECORD)
            found.extend(provenance_records(read_object(path, problems), f"launcher record {path}", identity, str(path.parent), True))
    known_directors = [*directors, *(item.owner for item in found if isinstance(item.owner, Director))]
    message_routes = [*routes, *found]
    log = Path(os.environ.get("WORKER_MESSAGE_LOG", str(Path.home() / ".local/state/message/log.jsonl")))
    try:
        with log.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                try:
                    record = object_fields(cast(object, json.loads(line)))
                except ValueError:
                    problems.append(f"{log}:{number}: invalid JSON")
                    continue
                found.extend(message_ownership_records(record, f"message log {log}:{number}", known_directors, message_routes))
    except (OSError, UnicodeError) as error:
        problems.append(f"{log}: {error}")
    return found


def state_directory() -> Path:
    return Path(os.environ.get("WORKER_OWNERS_STATE", str(Path.home() / ".local/state/worker-owners")))


def saved_ownership(record: JsonObject) -> OwnershipEvidence:
    evidence = tuple(item for item in list_items(record.get("evidence")) if isinstance(item, str))
    director = text_field(record, "director")
    owner = Director(director, "", text_field(record, "unit")) if director else MISSING_DIRECTOR_RECORD
    return OwnershipEvidence(text_field(record, "name"), text_field(record, "thread_id"), text_field(record, "run"),
                             owner, evidence, record.get("classification") == WorkerClass.PooledReviewer.value)


def enrich_director_record(worker: Worker, provenance: Sequence[OwnershipEvidence]) -> Worker:
    """Complete an already recorded session's unit before checking its liveness."""
    if not isinstance(worker.ownership, RecordedDirectorOwner) or worker.unit:
        return worker
    matching = [item for item in provenance if isinstance(item.owner, Director) and item.owner.session_id == worker.director and item.owner.unit]
    units = {item.owner.unit for item in matching}
    if len(units) == 1:
        owner = replace(worker.ownership.director, unit=next(iter(units)))
        return replace(worker, ownership=RecordedDirectorOwner(owner),
                       evidence=evidence_lines(worker.evidence, *(item.evidence for item in matching)))
    return worker


def collect_sources() -> OwnershipSources:
    with stored_role_map():
        observed = roster.roster(os.environ, roster.ThisMachine())
    problems = list(observed.problems)
    directors: list[Director] = []
    for entry in observed.entries:
        if isinstance(entry.session, roster.ClaudeSession):
            unit = entry.director.unit if isinstance(entry.director, roster.DirectsUnit) else ""
            prefix = re.sub(r"[^A-Za-z0-9._-]", "-", Path(entry.cwd).name)[:40] if entry.cwd else ""
            directors.append(Director(entry.session.session_id, entry.name, unit, prefix))
    roots = unit_runs.run_roots(os.environ.get("ROSTER_RUN_ROOT"))
    pool = Path(os.environ.get("REVIEW_POOL_STATE", str(Path.home() / ".local/state/review-pool")))
    excluded = {entry.thread.thread_id for entry in observed.entries
                if entry.role.startswith("astra-") and isinstance(entry.thread, roster.CodexThread)}
    observations: dict[str, list[Worker]] = {}
    mesh_paths = [path for root in roots for path in source_files(root, "*/mesh_roster.json", problems)]
    mesh_paths.extend(source_files(pool, "*/mesh/*/mesh_roster.json", problems))
    for path in mesh_paths:
        owner = unit_runs.director(path.parent)
        unit = next((item.unit for item in directors if item.session_id == owner), "")
        pooled = path.is_relative_to(pool)
        for name, raw in read_object(path, problems).items():
            record = object_fields(raw)
            thread = text_field(record, "thread_id")
            if record.get("held_design_id") or text_field(record, "role").startswith("astra-"):
                if thread:
                    excluded.add(thread)
                continue
            if record.get("status") == "ended" or record.get("role_redirect") or record.get("transferring_role") or thread in excluded:
                continue
            if not thread:
                problems.append(f"{path}: {name}: thread id missing")
                continue
            identity = Director(owner, "", unit) if owner else MISSING_DIRECTOR_RECORD
            ownership = PooledReviewerOwner() if pooled else ownership_record(identity)
            observations.setdefault(thread, []).append(Worker(name, thread, str(path.parent), ownership,
                (f"mesh roster {path}", f"recorded status {text_field(record, 'status')}"),
                status=WorkerTurnState.from_status(text_field(record, "status"))))
    routes = [OwnershipEvidence.from_worker(item) for records in observations.values() for item in records]
    provenance = load_provenance(roots, directors, problems, routes)
    saved = state_directory() / "recovered.json"
    saved_provenance: list[OwnershipEvidence] = []
    if saved.exists():
        saved_provenance = [saved_ownership(object_fields(item)) for item in list_items(read_object(saved, problems).get("workers"))]
        provenance.extend(saved_provenance)
    worker_threads = {item.thread_id for item in [*routes, *provenance]}
    for entry in observed.entries:
        if entry.kind != "codex" or not isinstance(entry.thread, roster.CodexThread) or entry.thread.thread_id in excluded:
            continue
        if entry.role != "worker" and entry.thread.thread_id not in worker_threads:
            continue
        relation = entry.director
        director = relation.address.removeprefix("session:") if isinstance(relation, roster.UnderDirector) else ""
        unit = relation.unit if isinstance(relation, roster.UnderDirector) else ""
        run = entry.assignment.session_dir if isinstance(entry.assignment, roster.WorkerAssignment) else ""
        identity = Director(director, "", unit) if director else MissingDirectorRecord(unit)
        observations.setdefault(entry.thread.thread_id, []).append(Worker(entry.name, entry.thread.thread_id, run,
            ownership_record(identity), ("live session roster",), status=WorkerTurnState.from_status(entry.status)))
    classified: list[Worker] = []
    for thread, records in observations.items():
        if thread in excluded:
            continue
        provenance.extend(OwnershipEvidence.from_worker(item) for item in records)
        enriched = [enrich_director_record(item, provenance) for item in records]
        worker = merge_observations(enriched, directors)
        if isinstance(worker.ownership, MissingDirectorRecord):
            prior = [item.worker() for item in saved_provenance if item.thread_id == thread]
            if prior:
                worker = merge_observations([worker, *prior], directors)
        classified.append(worker)
    return OwnershipSources(sorted(classified, key=lambda item: (item.run, item.name)), directors, provenance, problems)


def recover_sources(sources: OwnershipSources) -> OwnershipSources:
    return replace(sources, workers=[recover_worker(worker, sources.provenance, sources.directors) for worker in sources.workers])


def write_json(path: Path, value: object) -> None:
    staged = path.with_suffix(f".{os.getpid()}.tmp")
    with staged.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2)
        handle.writelines(("\n",))
    os.replace(staged, path)


def save_recoveries(sources: OwnershipSources, folder: Path) -> None:
    """Retain saved ownership even when a worker or its original source disappears."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "recovered.json"
    prior = read_object(path, sources.problems) if path.exists() else {}
    records = [object_fields(item) for item in list_items(prior.get("workers"))]
    saved = {text_field(item, "thread_id"): item for item in records if text_field(item, "thread_id")}
    for worker in sources.workers:
        if worker.recovery_attempted and worker.classification != WorkerClass.OwnerUnknown:
            saved[worker.thread_id] = worker_record(worker)
    write_json(path, {"workers": list(saved.values())})


@dataclass(frozen=True)
class WorkerKept:
    reason: str


@dataclass(frozen=True)
class EndEligible:
    pass


def end_decision(worker: Worker, selected_class: WorkerClass, problems: Sequence[str], first_run: bool) -> WorkerKept | EndEligible:
    if isinstance(worker.ownership, LiveDirectorOwner):
        return WorkerKept("unit director is live")
    if isinstance(worker.ownership, PooledReviewerOwner):
        return WorkerKept("pooled reviewer")
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
    if route_problems or record.get("thread_id") != worker.thread_id:
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
    decision = end_decision(matching[0], worker.classification, fresh.problems, False)
    if isinstance(decision, WorkerKept):
        return decision
    # Check the requested route too; source collection may have found a different run.
    decision = end_decision(worker, worker.classification, fresh.problems, False)
    if isinstance(decision, WorkerKept):
        return decision
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
    return 0 if not problems and current.get("thread_id") == worker.thread_id and current.get("status") == "ended" else 3


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
    if route.get("thread_id") != record.thread_id:
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
            decision = end_decision(worker, selected_class, recovered.problems, not marker.exists())
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
            decision = end_decision(worker, selected_class, recovered.problems, first_run)
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
        save_recoveries(sources, state_directory())
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
