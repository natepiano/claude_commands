#!/usr/bin/env python3
"""Worker allocation and run history. Staff holds worker ownership."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "production"))

if TYPE_CHECKING:
    from scripts.production import unit_lookup
    from scripts.message.sessions import RegistryRead, SessionRecord
    from scripts.lib.names import JobKey

from scripts.agents import mesh_run_process, worker_lifecycle
from scripts.production import staff, worker_admission, session_staff
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib import models_registry, unit_runs
from scripts.usage import agents_log

ROSTER = "mesh_roster.json"
STATE = "progress_history_state.json"
ROLES = staff.WORKER_ROLES
ALLOCATION_CLAIM_WINDOW_SECS = 120.0
ALLOCATION_FIELDS = ("allocation_pending", "allocation_id", "allocation_owner_pid", "allocation_expires_at")


class WorkerUnavailable(Exception):
    """No safe ownership transition can be made from the current evidence."""


class ClaudeCommandLookup(Protocol):
    def claude_bin(self) -> str: ...


class TitleRenderer(Protocol):
    def unit_worker_title(self, production: str, unit: str, job: JobKey, ordinal: int, address: str) -> str: ...

    def mesh_title(self, session_dir: str, name: str, record: Mapping[str, object]) -> str: ...


class WorkerArguments(argparse.Namespace):
    command: str = ""
    session_dir: Path = Path()
    role: str = ""
    working_dir: str = ""
    owner_pid: int = 0
    model: str = ""
    effort: str = ""
    name: str = ""


@dataclass(frozen=True)
class CodexThread:
    thread_id: str
    codex_home: str

    def __post_init__(self) -> None:
        if not self.thread_id or not self.codex_home:
            raise WorkerUnavailable("Codex backend requires thread_id and codex_home")


@dataclass(frozen=True)
class ClaudeSession:
    session_id: str

    def __post_init__(self) -> None:
        if not self.session_id:
            raise WorkerUnavailable("Claude backend requires session_id")


@dataclass(frozen=True)
class HeldBy:
    run: str

    def __post_init__(self) -> None:
        if not self.run:
            raise WorkerUnavailable("HeldBy requires a run")


@dataclass(frozen=True)
class Paused:
    """The worker has no active run owner."""


@dataclass(frozen=True)
class Retired:
    """This worker must never be allocated again."""


@dataclass
class WorkerEvent:
    kind: Literal["opened", "replaced", "ended"]
    cause: str
    run: str
    at: float
    details: dict[str, str] = field(default_factory=dict)


@dataclass
class UnitWorker:
    name: str
    role: str
    family: str
    model: str
    effort: str
    access_policy: dict[str, object]
    backend: CodexThread | ClaudeSession
    ownership: HeldBy | Paused | Retired
    events: list[WorkerEvent] = field(default_factory=list)
    staff_id: staff.StaffId = staff.StaffId("")


@dataclass
class UnitWorkers:
    membership: staff.StaffSnapshot
    assignments: agents_log.RoleLog
    workers: dict[str, UnitWorker] = field(default_factory=dict)
    history: dict[staff.StaffId, list[WorkerEvent]] = field(default_factory=dict)
    unresolved: dict[str, object] = field(default_factory=dict)


def _object(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _required(record: Mapping[str, object], key: str) -> str:
    value = _text(record.get(key))
    if not value:
        raise WorkerUnavailable(f"invalid worker state: missing {key}")
    return value


def _read(path: Path) -> dict[str, object]:
    """Read worker evidence; other record kinds use read_json_object instead."""
    records = worker_lifecycle.read_worker_state(path)
    return worker_admission.resolve_roster(path.parent, records) if path.name == ROSTER else records


def _read_roster(run: Path) -> dict[str, object]:
    return worker_admission.resolve_roster(run, session_staff.read_worker_roster(run / ROSTER))


def _atomic(path: Path, data: Mapping[str, object]) -> None:
    _ = _read(path)
    _ = worker_lifecycle.validate_worker_state(path, data)
    write_text_atomic(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def _encode_worker(worker: UnitWorker) -> dict[str, object]:
    ownership = worker.ownership
    if isinstance(ownership, HeldBy):
        encoded_owner: dict[str, object] = {"kind": "HeldBy", "run": ownership.run}
    else:
        encoded_owner = {"kind": "Paused" if isinstance(ownership, Paused) else "Retired"}
    return {"staff_id": worker.staff_id,
            "model": worker.model, "effort": worker.effort, "access_policy": worker.access_policy,
            "ownership": encoded_owner,
            "events": [_encode_event(event) for event in worker.events]}


def _encode_event(event: WorkerEvent) -> dict[str, object]:
    return {"kind": event.kind, "cause": event.cause, "run": event.run, "at": event.at, **event.details}


def _worker_task_role(record: Mapping[str, object], membership: staff.StaffSnapshot,
                      assignments: agents_log.RoleLog) -> str:
    from scripts.agents import codex_mesh
    assignment = codex_mesh.task_role(session_staff.worker_session(record, membership), assignments,
                                     default_role=session_staff.worker_name(record, membership))
    return (assignment.role if isinstance(assignment, codex_mesh.AssignedWorkerRole)
            else session_staff.worker_name(record, membership))


def _decode_worker(name: str, record: dict[str, object], membership: staff.StaffSnapshot,
                   assignments: agents_log.RoleLog) -> UnitWorker:
    owner = _object(record.get("ownership"))
    found = session_staff.worker_backend(membership, staff.StaffId(_text(record.get("staff_id"))))
    if isinstance(found, session_staff.WorkerBackendUnavailable):
        raise WorkerUnavailable(found.reason)
    member = found.row
    identity = (CodexThread(member.current_session, str(found.backend.codex_home))
                if isinstance(found.backend, staff.CodexBackend) else ClaudeSession(member.current_session))
    if owner.get("kind") == "HeldBy":
        ownership: HeldBy | Paused | Retired = HeldBy(_required(owner, "run"))
    elif owner.get("kind") == "Paused":
        ownership = Paused()
    elif owner.get("kind") == "Retired":
        ownership = Retired()
    else:
        raise WorkerUnavailable(f"{name}: invalid ownership")
    events = record.get("events", [])
    if not isinstance(events, list):
        raise WorkerUnavailable(f"{name}: invalid events")
    return UnitWorker(member.name,
                      _worker_task_role(record, membership, assignments),
                      "codex" if isinstance(identity, CodexThread) else "claude",
                      _text(record.get("model")), _text(record.get("effort")),
                      _object(record.get("access_policy")), identity, ownership,
                      [_decode_event(_object(event)) for event in cast(list[object], events)],
                      member.id)


def _decode_event(record: dict[str, object]) -> WorkerEvent:
    kind = _required(record, "kind")
    if kind not in ("opened", "replaced", "ended"):
        raise WorkerUnavailable(f"invalid worker event: {kind}")
    at = record.get("at")
    if not isinstance(at, (int, float)):
        raise WorkerUnavailable("invalid worker event timestamp")
    return WorkerEvent(kind,
                       _required(record, "cause"), _required(record, "run"), float(at),
                       {key: _text(record[key]) for key in ("old_thread_id", "thread_id")
                        if key in record})


@dataclass(frozen=True)
class UnitWorkerLedger:
    path: Path

    def save(self, ledger: UnitWorkers) -> None:
        """The caller holds locked(); intermediate transaction writes are durable."""
        _atomic(self.path, {"version": 1,
                           "workers": {**ledger.unresolved, **{worker.staff_id: _encode_worker(worker) for worker in ledger.workers.values()}},
                           "history": {identity: [_encode_event(event) for event in events]
                                       for identity, events in ledger.history.items()}})

    def record_end(self, ledger: UnitWorkers, identity: staff.StaffId, worker: UnitWorker, run: Path) -> None:
        """Keep stopped evidence before staff retirement can remove the backend."""
        events = ledger.history.setdefault(identity, list(worker.events))
        if not any(event.kind == "ended" for event in events):
            native = (worker.backend.thread_id if isinstance(worker.backend, CodexThread)
                      else worker.backend.session_id)
            events.append(_event("ended", "permanent end", run, thread_id=native))
        data = _read(self.path)
        data["history"] = {key: [_encode_event(event) for event in saved] for key, saved in ledger.history.items()}
        _atomic(self.path, data)

    @contextmanager
    def locked(self, *, membership: staff.StaffSnapshot | None = None, write: bool = True,
               deadline: float = worker_lifecycle.NO_LOCK_DEADLINE) -> Generator[UnitWorkers, None, None]:
        """None reads one fresh snapshot under the lock; allocation passes the snapshot that found the ledger."""
        from scripts.agents import codex_mesh
        with worker_lifecycle.operation_deadline(deadline=deadline):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.with_suffix(".lock").open("a+", encoding="utf-8") as lock:
                worker_lifecycle.acquire_flock(lock, deadline=deadline)
                try:
                    data = _read(self.path)
                except worker_lifecycle.WorkerStateUnreadable as error:
                    raise WorkerUnavailable(str(error)) from error
                ledger = UnitWorkers(membership if membership is not None else staff.local_snapshot(), codex_mesh.task_assignments())
                history = data.get("history", {})
                if not isinstance(history, dict):
                    raise WorkerUnavailable("invalid worker history")
                for identity, raw_events in cast(dict[str, object], history).items():
                    if not isinstance(raw_events, list):
                        raise WorkerUnavailable(f"{identity}: invalid worker history")
                    ledger.history[staff.native_id(identity)] = [
                        _decode_event(_object(event)) for event in cast(list[object], raw_events)]
                for name, record in _object(data.get("workers")).items():
                    try:
                        worker = _decode_worker(name, _object(record), ledger.membership, ledger.assignments)
                        ledger.workers[worker.name] = worker
                    except WorkerUnavailable as error:
                        ledger.unresolved[name] = record
                        print(f"anomaly: {name}: {error}", file=sys.stderr)
                yield ledger
                if write:
                    self.save(ledger)


def _ledger_for_run(run: Path) -> tuple[staff.StaffRow, UnitWorkerLedger, staff.StaffSnapshot]:
    membership = staff.local_snapshot()
    owner = worker_admission.parent(membership, staff_id=staff.runtime_id(), run=run)
    return owner, UnitWorkerLedger(_ledger_path(owner.id)), membership


def ledger_for_run(run: Path) -> UnitWorkerLedger:
    return _ledger_for_run(run)[1]


@contextmanager
def _roster_locked(run: Path) -> Generator[tuple[worker_lifecycle.WorkerStateFile, dict[str, object]], None, None]:
    with worker_lifecycle.locked_state(run / ROSTER) as state:
        yield state, _read(state.path)


def _write_roster(handle: worker_lifecycle.WorkerStateFile, roster: dict[str, object]) -> None:
    session_staff.publish_worker_roster(handle, roster)


def allocation_is_live(record: Mapping[str, object]) -> bool:
    """Only a living caller within its claim window can reserve a dispatch."""
    owner = record.get("allocation_owner_pid")
    expires = record.get("allocation_expires_at")
    if (record.get("allocation_pending") is not True
            or not record.get("allocation_id")
            or not isinstance(owner, int) or owner <= 0
            or not isinstance(expires, (int, float)) or not time.time() < expires):
        return False
    try:
        os.kill(owner, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def clear_allocation(record: dict[str, object]) -> None:
    for key in ALLOCATION_FIELDS:
        _ = record.pop(key, None)


def _reserve_allocation(record: dict[str, object], owner_pid: int) -> None:
    record.update({"allocation_pending": True, "allocation_id": str(uuid.uuid4()),
                   "allocation_owner_pid": owner_pid,
                   "allocation_expires_at": time.time() + ALLOCATION_CLAIM_WINDOW_SECS})


def allocation_for_follow(run: Path, name: str) -> dict[str, object]:
    """Capture the dispatch identity before recovery or refusal can fail."""
    record = _object(_read_roster(run).get(name))
    return {key: record[key] for key in ALLOCATION_FIELDS if key in record}


def release_allocation(run: Path, name: str, expected: Mapping[str, object]) -> None:
    """An abandoned follow cannot clear a newer dispatch's reservation."""
    identity = expected.get("allocation_id")
    if not identity or not (run / ROSTER).exists():
        return
    with worker_lifecycle.lock_worker(run, name), _roster_locked(run) as (handle, roster):
        record = _object(roster.get(name))
        if record.get("allocation_id") != identity:
            return
        clear_allocation(record)
        from scripts.agents import codex_mesh
        codex_mesh.advance_worker_roster_revision(record)
        _write_roster(handle, roster)


def _event(kind: Literal["opened", "replaced", "ended"], cause: str, run: Path,
           **details: str) -> WorkerEvent:
    return WorkerEvent(kind, cause, str(run), time.time(), details)


def _worker_from_record(name: str, run: Path, record: dict[str, object], membership: staff.StaffSnapshot,
                         assignments: agents_log.RoleLog) -> UnitWorker:
    from scripts.agents import codex_mesh
    from scripts.astra.design_roles import PhaseBoundDesigner
    access = codex_mesh.roster_access_policy(record)
    policy: dict[str, object] = {"kind": type(access).__name__}
    if isinstance(access, PhaseBoundDesigner):
        policy.update({"binding_generation": access.binding_generation, "launch_sandbox": access.launch_sandbox})
    settings = worker_lifecycle.recorded_turn_settings(record)
    return UnitWorker(name, _worker_task_role(record, membership, assignments), "codex", settings.model,
                      settings.effort, policy,
                      CodexThread(_text(session_staff.worker_session(record, membership)), _text(session_staff.worker_home(record, membership)) or
                                  str(codex_mesh.server_record_home(_read(run / "mesh_server.json")))),
                      HeldBy(str(run)), [_event("opened", "registered existing roster", run,
                                               thread_id=_text(session_staff.worker_session(record, membership)))], staff.StaffId(_text(record.get("staff_id"))))


@contextmanager
def ending_worker(run: Path, name: str) -> Generator[Callable[[], None], None, None]:
    """Record the end under the ledger lock and retain the worker for permanent removal."""
    raw = worker_lifecycle.read_json_object(run / ROSTER)
    entry = _object(raw.get(name))
    location = _text(entry.get("unit_ledger"))
    def unchanged() -> None:
        return
    if not location:
        yield unchanged
        return
    store = UnitWorkerLedger(Path(location))
    with store.locked(write=False) as ledger:
        worker = ledger.workers.get(name)
        if worker is None:
            yield unchanged
            return
        reference = _text(entry.get("staff_id"))
        if reference and worker.staff_id != reference:
            raise WorkerUnavailable(f"{name}: ledger membership changed")
        def remember_end() -> None:
            identity = worker.staff_id
            store.record_end(ledger, identity, worker, run)
            store.save(ledger)
        yield remember_end


def note_backend(run: Path, name: str, thread_id: str, home: Path) -> None:
    """A successful resume records its actual home, only for the current owner."""
    run = run.expanduser().resolve()
    store = ledger_for_run(run)
    with store.locked() as ledger:
        worker = ledger.workers.get(name)
        if worker is None:
            return
        owner = worker.ownership
        if isinstance(owner, HeldBy) and owner.run == str(run):
            with worker_lifecycle.lock_worker(run, name), _roster_locked(run) as (handle, roster):
                record = dict(_object(roster.get(name)))
                if session_staff.worker_session(record) != thread_id:
                    return
                backend = worker.backend
                previous = next((event.details["thread_id"] for event in reversed(worker.events)
                                 if event.kind in ("opened", "replaced") and event.details.get("thread_id")),
                                worker.staff_id or (backend.thread_id if isinstance(backend, CodexThread) else ""))
                if previous and previous != thread_id:
                    worker.events.append(_event("replaced", "resume refused old thread", run,
                                                old_thread_id=previous, thread_id=thread_id))
                worker.access_policy = _object(record.get("access_policy")) or worker.access_policy
                from scripts.agents import codex_mesh
                settings = worker_lifecycle.recorded_turn_settings(record)
                worker.model, worker.effort = settings.model, settings.effort
                worker.backend = CodexThread(thread_id, str(home.resolve()))
                worker.ownership = HeldBy(str(run))
                record.update({"unit_ledger": str(store.path), "access_policy": worker.access_policy})
                codex_mesh.advance_worker_roster_revision(record)
                roster[name] = record
                _write_roster(handle, roster)


def _reserve(run: Path, name: str, store: UnitWorkerLedger, owner_pid: int, *, paused: bool = False) -> None:
    from scripts.agents import codex_mesh
    with worker_lifecycle.lock_worker(run, name), _roster_locked(run) as (handle, roster):
        record = dict(_object(roster.get(name)))
        if not record:
            raise WorkerUnavailable(f"{name}: no longer owned by this run")
        if codex_mesh.worker_launcher_attached(record):
            raise WorkerUnavailable(f"{name}: launcher attached during resolution")
        if allocation_is_live(record) or (record.get("continuation_owed") and not paused):
            raise WorkerUnavailable(f"{name}: work already reserved or owed")
        _reserve_allocation(record, owner_pid)
        record["unit_ledger"] = str(store.path)
        codex_mesh.advance_worker_roster_revision(record)
        roster[name] = record
        _write_roster(handle, roster)


def _next_worker_name(ledger: UnitWorkers, roster: Mapping[str, object], role: str) -> str:
    ordinal = 1
    name = role
    while name in ledger.workers or name in roster:
        ordinal += 1
        name = f"{role}{ordinal}"
    return name


def resolve(run: Path, role: str, *, model: str = "", effort: str = "", working_dir: Path | None = None) -> str:
    """Return an idle worker, carry one over, reopen one, or open the next name."""
    return _resolve(run, role, os.getpid(), model=model, effort=effort, working_dir=working_dir)


def _resolve(run: Path, role: str, owner_pid: int, *, model: str = "", effort: str = "",
             new_name: str = "", working_dir: Path | None = None) -> str:
    from scripts.agents import codex_mesh
    if role not in ROLES:
        raise WorkerUnavailable(f"unsupported worker role: {role}")
    run = run.expanduser().resolve()
    owner, store, membership = _ledger_for_run(run)
    reviewer_base = _reviewer_destination(owner) if role == "review" else role
    with store.locked(membership=membership) as ledger:
        assignments = ledger.assignments
        roster = _read_roster(run)
        candidates = sorted(roster.items(), key=lambda item: (
            _object(item[1]).get("status") not in ("done", "failed", "paused"),
            role == "review" and item[0] != reviewer_base, item[0]))
        for name, value in candidates:
            record = _object(value)
            if new_name or _worker_task_role(record, membership, assignments) != role:
                continue
            registered = ledger.workers.get(name)
            paused = record.get("status") == "paused" or registered is not None and isinstance(registered.ownership, Paused)
            try:
                if record.get("status") in ("done", "failed", "paused"):
                    if registered is not None and isinstance(registered.ownership, Retired):
                        continue
                    if codex_mesh.worker_idle_reason(str(run), name, record, paused=paused, membership=membership):
                        continue
                    permission = worker_admission.admit(name, run)
                    _ = worker_admission.publish(permission, _text(session_staff.worker_session(record, membership)))
                elif record.get("status") in ("ended", "running", "starting"):
                    if codex_mesh.worker_idle_reason(str(run), name, record, membership=membership):
                        continue
                    if codex_mesh.command_reopen(argparse.Namespace(session_dir=str(run), to=name, quiet=True)):
                        continue
                    paused = False
                else:
                    continue
                if name not in ledger.workers:
                    ledger.workers[name] = _worker_from_record(name, run, record, membership, assignments)
                _reserve(run, name, store, owner_pid, paused=paused)
            except worker_admission.AdmissionRefused as error:
                print(str(error), file=sys.stderr)
                continue
            except WorkerUnavailable:
                continue
            ledger.workers[name].ownership = HeldBy(str(run))
            return name
        name = _next_worker_name(ledger, roster, reviewer_base)
        carried: UnitWorker | None = None
        if not roster and not new_name:
            for row in membership.find(kind="worker", parent=owner.id):
                if _worker_task_role({"staff_id": row.id}, membership, assignments) != role:
                    continue
                worker = ledger.workers.get(row.name)
                if worker is not None:
                    if isinstance(worker.ownership, Retired):
                        continue
                    if isinstance(worker.ownership, HeldBy):
                        previous_run = Path(worker.ownership.run)
                        if (previous_run / ROSTER).exists():
                            previous = _object(_read_roster(previous_run).get(row.name))
                            if codex_mesh.worker_idle_reason(str(previous_run), row.name, previous, adopting=True, membership=membership):
                                continue
                name, carried = row.name, worker
                break
        if new_name:
            if new_name in ledger.workers or new_name in roster:
                raise WorkerUnavailable(f"{new_name}: worker name already exists")
            name = new_name
        configuration = models_registry.launch_configuration(os.environ)
        record = codex_mesh.open_unit_worker(str(run), name, role, configuration=configuration,
            model=model or (carried.model if carried is not None else ""),
            effort=effort or (carried.effort if carried is not None else ""),
            cwd=str(working_dir) if working_dir is not None else "")
        if carried is not None:
            record.update({"turn_model": carried.model, "turn_effort": carried.effort})
            with _roster_locked(run) as (handle, current):
                current[name] = record
                _write_roster(handle, current)
        membership = staff.local_snapshot()
        ledger.workers[name] = _worker_from_record(name, run, record, membership, assignments)
        ledger.workers[name].events = [_event("opened", "no idle worker for role", run,
                                             thread_id=_text(session_staff.worker_session(record, membership)))]
        _reserve(run, name, store, owner_pid)
        return name


def _reviewer_destination(owner: staff.StaffRow) -> str:
    if owner.kind == "unit" and not isinstance(owner.worktree, staff.PresentWorktree):
        raise WorkerUnavailable("invalid worker state: missing working_dir")
    from scripts.production.production_doc import read_production
    directory = str(read_production(owner).checkout if owner.kind == "showrunner" and isinstance(owner.doc, staff.LiveDoc)
                    else owner.worktree.path if isinstance(owner.worktree, staff.PresentWorktree) else Path.cwd())
    result = subprocess.run(
        ["bash", str(Path(__file__).with_name("worker_name.sh")),
         directory, "review", "review"],
        capture_output=True, text=True, check=False, timeout=10)
    if result.returncode or not result.stdout.strip():
        raise WorkerUnavailable(result.stderr.strip() or "cannot name reviewer")
    return result.stdout.strip()


@dataclass(frozen=True)
class LiveUnitDirector:
    session_id: str
    pid: int


@dataclass(frozen=True)
class NoLiveUnitDirector:
    """Neither the registry nor the unit mark identifies a live director."""


def running_director_sessions(registry: RegistryRead) -> list[SessionRecord]:
    """Running director candidates, newest first, including sessions without sockets."""
    from scripts.message import sessions

    return sessions.running_records(registry)


def resolve_live_unit_director(
    run: Path, record: staff.StaffSnapshot, sources: unit_lookup.MemberSources,
    running: Sequence[SessionRecord], dependencies: Callable[[Path], None], *,
    use_live_marks: bool = True,
) -> LiveUnitDirector | NoLiveUnitDirector:
    """Resolve the current director from worker membership or the unit staff row."""
    from scripts.production import unit_lookup

    dependencies(run / ROSTER)
    rosters = worker_lifecycle.read_json_object(run / ROSTER)
    parents: dict[staff.StaffId, staff.StaffRow] = {}
    for raw in rosters.values():
        if not isinstance(raw, dict):
            continue
        entry = cast(dict[str, object], raw)
        if entry.get("status") == "ended":
            continue
        reference = _text(entry.get("staff_id"))
        worker = record.by_id(staff.StaffId(reference))
        if worker is None or worker.kind != "worker":
            continue
        parent = record.by_id(worker.parent.id) if isinstance(worker.parent, staff.ReportsTo) else None
        if parent is not None:
            parents[parent.id] = parent
    dependencies(run / STATE)
    context = worker_lifecycle.read_json_object(run / STATE)
    if not parents:
        owner = unit_runs.run_owner(context, record)
        if isinstance(owner, staff.StaffRow) and owner.kind in ("unit", "showrunner"):
            parents[owner.id] = owner
    if len(parents) != 1:
        return NoLiveUnitDirector()
    parent = next(iter(parents.values()))
    for session in running:
        if session["sessionId"] == parent.current_session:
            return LiveUnitDirector(session["sessionId"], session["pid"])
    production = record.parent_of(parent)
    if use_live_marks and production is not None:
        try:
            marked = unit_lookup.labeled_session(production.name, parent.name, record=record, sources=sources, running=running)
        except OSError:
            return NoLiveUnitDirector()
        if marked is not None and isinstance(marked.claude, unit_lookup.LiveClaude):
            if marked.claude.session_id == parent.current_session:
                return LiveUnitDirector(marked.claude.session_id, marked.claude.pid)
    return NoLiveUnitDirector()


def live_unit_director(run: Path) -> LiveUnitDirector | NoLiveUnitDirector:
    """Resolve the director across restarts with one owner snapshot."""
    from scripts.production import staff, unit_lookup
    from scripts.message import sessions

    dependencies = staff.RecordInputs()
    record = staff.initial_snapshot()
    registry = sessions.read_registry(dependencies=dependencies)
    running = running_director_sessions(registry)
    return resolve_live_unit_director(run, record, unit_lookup.member_sources(dependencies=dependencies),
                                      running, dependencies)


def run_has_live_director(run: Path) -> bool:
    return isinstance(live_unit_director(run), LiveUnitDirector)


def director_pid(run: Path) -> int:
    """The director process owns dispatches across one-call shell invocations."""
    director = live_unit_director(run)
    if isinstance(director, LiveUnitDirector):
        return director.pid
    raise WorkerUnavailable("run director pid is unknown; pass --owner-pid <pid>")


def _write_inbox(handle: worker_lifecycle.WorkerStateFile, record: Mapping[str, object]) -> None:
    handle.publish(json.dumps(record if record else worker_lifecycle.empty_inbox()))


def _ledger_path(identity: staff.StaffId) -> Path:
    root = Path(os.environ.get("UNIT_WORKERS_STATE", str(Path.home() / ".local/state/unit-workers")))
    return root.expanduser() / f"{identity}.json"


def ledger_path(production: str, unit: str) -> Path:
    owner = worker_admission.parent(staff.local_snapshot(), production=production, unit=unit)
    return _ledger_path(owner.id)


def _runs(ledger: UnitWorkers) -> tuple[Path, ...]:
    runs: set[Path] = set()
    for worker in ledger.workers.values():
        runs.update(Path(event.run) for event in worker.events)
        if isinstance(worker.ownership, HeldBy):
            runs.add(Path(worker.ownership.run))
    return tuple(sorted(runs))


def live_detached_runs(run: Path) -> tuple[tuple[Path, dict[str, object]], ...]:
    """Recorded supervisors only; no process discovery by name."""
    records: list[tuple[Path, dict[str, object]]] = []
    for path in sorted((run / "mesh_runs").glob("*.json")):
        record = worker_lifecycle.read_json_object(path)
        pid = record.get("pid")
        if record.get("status") == "running" and isinstance(pid, int) and mesh_run_process.supervises(pid, path):
            records.append((path, record))
    return tuple(records)


def protected_working_directories(run: Path) -> tuple[Path, ...]:
    membership = staff.local_snapshot()
    return tuple(session_staff.worker_directory(membership, staff.StaffId(str(record["staff_id"]))).resolve()
                 for _, record in retained_detached_runs(run))


def retained_detached_runs(run: Path) -> tuple[tuple[Path, dict[str, object]], ...]:
    """Commands owned by unit workers, including workers about to be paused."""
    store = ledger_for_run(run)
    if not store.path.exists():
        return ()
    with store.locked() as ledger:
        retained = {worker.name for worker in ledger.workers.values()
                    if not isinstance(worker.ownership, Retired)}
        return tuple((path, record) for history in _runs(ledger)
                     for path, record in live_detached_runs(history)
                     if record.get("worker") in retained)


def held_claude_sessions() -> set[str]:
    record = staff.local_snapshot()
    return {row.current_session for row in record.find(kind="worker")
            if isinstance(record.backends.get(row.id), staff.ClaudeBackend)}


def end_unit(owner: staff.StaffRow) -> None:
    from scripts.agents import codex_mesh
    # Run cleanup reads this ledger too; load only its binary lookup interface
    # here so neither module owns a static import cycle.
    with worker_lifecycle.operation_deadline(timeout=codex_mesh.THREAD_RELEASE_TIMEOUT_SECS):
        remove_workers = cast(ClaudeCommandLookup, cast(object, import_module("scripts.direct.remove_workers")))
        store = UnitWorkerLedger(_ledger_path(owner.id))
        if not store.path.exists():
            return
        saved = _read(store.path)
        if saved.get("history") and not saved.get("workers"):
            return
        with store.locked() as ledger:
            membership = staff.local_snapshot()
            histories = _runs(ledger)
            with worker_lifecycle.membership_operation(*histories):
                for name, worker in list(ledger.workers.items()):
                    member = membership.by_id(worker.staff_id)
                    if member is None or not member.reports_to(owner.id):
                        continue
                    for run in histories:
                        if not run.is_dir():
                            continue
                        with worker_lifecycle.membership_operation(run):
                            with worker_lifecycle.lock_worker(run, name):
                                with worker_lifecycle.locked_state(session_staff.worker_inbox_path(run, name)) as inbox:
                                    pending = inbox.read_object()
                                    raw_messages = pending.get("messages")
                                    queued = [item for item in cast(list[object], raw_messages) if isinstance(item, str)] if isinstance(raw_messages, list) else []
                                    for message in queued:
                                        print(f"cancelled pending message of {name}: {message}")
                                    _write_inbox(inbox, {})
                                current = _object(_read_roster(run).get(name))
                                if isinstance(worker.backend, CodexThread) and current:
                                    result = codex_mesh.end_worker_locked(argparse.Namespace(session_dir=str(run), to=name))
                                    if result:
                                        raise WorkerUnavailable(f"cannot end worker {name} in {run}; retry retirement")
                                for path, detached in live_detached_runs(run):
                                    if detached.get("worker") != name:
                                        continue
                                    pid = detached["pid"]
                                    if isinstance(pid, int) and mesh_run_process.supervises(pid, path):
                                        try:
                                            os.kill(pid, signal.SIGTERM)
                                        except ProcessLookupError:
                                            pass
                                        print(f"cancelled detached run {path.stem} of {name} (pid {pid})")
                                with _roster_locked(run) as (handle, roster):
                                    record = _object(roster.get(name))
                                    if not record:
                                        continue
                                    if record.get("continuation_owed"):
                                        print(f"cancelled owed continuation of {name}")
                                    if record.get("allocation_pending"):
                                        print(f"cancelled reservation of {name}")
                                    clear_allocation(record)
                                    record.update({"continuation_owed": False, "status": "ended"})
                                    codex_mesh.advance_worker_roster_revision(record)
                                    _write_roster(handle, roster)
                            # Worker locks are released before the retirement projection.
                    if isinstance(worker.backend, ClaudeSession):
                        claude = remove_workers.claude_bin()
                        result = subprocess.run([claude, "rm", worker.backend.session_id], capture_output=True,
                                                text=True, timeout=30, check=False)
                        if result.returncode:
                            raise WorkerUnavailable(f"cannot end Claude worker {name}: {result.stderr.strip()}")
                    store.record_end(ledger, member.id, worker, histories[-1] if histories else store.path.parent)
                    _ = staff.retire(member.id)
                    del ledger.workers[name]
                    worker.ownership = Retired()
                    print(f"ended worker {name}: {store.path}")
        if not ledger.history and not ledger.workers and not ledger.unresolved:
            store.path.unlink(missing_ok=True)


def shown_name(run: Path, address: str, *, role: str = "", held_design_id: str = "", held_role: str = "") -> str:
    """Render current unit context and stored assignment without changing the address."""
    from scripts.agents import codex_mesh
    titles = cast(TitleRenderer, cast(object, import_module("scripts.message.roster")))
    record = _object(_read_roster(run).get(address))
    if held_design_id or record.get("held_design_id"):
        return titles.mesh_title(str(run), address, {
            **record, "held_design_id": held_design_id or record.get("held_design_id", ""),
            "held_role": held_role or record.get("held_role", "")})
    membership = staff.local_snapshot()
    member = worker_admission.member(run, address, membership)
    if member is None:
        return address
    parent = membership.parent_of(member)
    if parent is None:
        return address
    if parent.kind != "unit":
        return titles.mesh_title(str(run), address, record)
    owner = membership.parent_of(parent)
    jobs: dict[str, JobKey] = {"impl": "implementer", "test": "tester", "fix": "repair_worker",
                               "review": "code_reviewer"}
    peers = [row.name for row in membership.find(kind="worker", parent=parent.id)]
    ordinal = peers.index(address) + 1
    return titles.unit_worker_title(owner.name if owner is not None else "", parent.name,
                                    jobs.get(role or _worker_task_role({"staff_id": member.id}, membership, codex_mesh.task_assignments()), "worker"),
                                    ordinal, address)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("resolve")
    _ = command.add_argument("--session-dir", type=Path, required=True)
    _ = command.add_argument("--role", choices=ROLES, required=True)
    _ = command.add_argument("--working-dir", default="", help="working directory for a new worker")
    _ = command.add_argument("--owner-pid", type=int, default=0)
    _ = command.add_argument("--model", default="")
    _ = command.add_argument("--effort", default="")
    _ = command.add_argument("--new-worker", dest="name", default="")
    display = commands.add_parser("shown-name")
    _ = display.add_argument("--session-dir", type=Path, required=True)
    _ = display.add_argument("--name", required=True)
    args = parser.parse_args(namespace=WorkerArguments())
    run = args.session_dir
    role = args.role
    try:
        if args.command == "shown-name":
            print(shown_name(run, args.name))
        else:
            print(_resolve(run, role, args.owner_pid or director_pid(run),
                           model=args.model, effort=args.effort, new_name=args.name,
                           working_dir=Path(args.working_dir) if args.working_dir else None))
        return 0
    except (WorkerUnavailable, OSError, ValueError) as error:
        print(f"workers: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    # Mesh imports this module by its package name; use that same exception
    # and domain type identity when this file is invoked as a CLI.
    from scripts.direct.workers import main as workers_main
    raise SystemExit(workers_main())
