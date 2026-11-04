#!/usr/bin/env python3
"""Unit-owned workers and recoverable transfers between run rosters.

The ledger lock precedes the giving roster, taking roster and pending inbox
locks. No caller may enter this module while holding one of those run locks.
The run's ``workers`` launch log is a different file with a different lifetime.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from enum import Enum
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "production"))

if TYPE_CHECKING:
    from scripts.production import staff, unit_lookup
    from scripts.message.sessions import RegistryRead, SessionRecord
    from scripts.lib.names import JobKey

from scripts.agents import worker_lifecycle
from scripts.production import unit_names, worker_admission
from scripts.lib.atomic_file import write_bytes_atomic, write_text_atomic

ROSTER = "mesh_roster.json"
STATE = "progress_history_state.json"
ROLES = ("impl", "test", "fix", "review")
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
class Adopted:
    to_run: str

    def __post_init__(self) -> None:
        if not self.to_run:
            raise WorkerUnavailable("Adopted requires to_run")


@dataclass(frozen=True)
class Retired:
    """This worker must never be allocated again."""


@dataclass
class WorkerEvent:
    kind: Literal["opened", "replaced", "adopted"]
    cause: str
    run: str
    at: float
    details: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ExistingRun:
    path: Path
    before: dict[str, object]


@dataclass(frozen=True)
class RemovedRun:
    path: Path


class AdoptionQueuePolicy(Enum):
    TRANSFER = "transfer"
    LEGACY_REFUSE_QUEUED = "legacy_refuse_queued"


@dataclass(frozen=True)
class AdoptionIntent:
    identity: str
    name: str
    giving: ExistingRun | RemovedRun
    destination: Path
    after: dict[str, object]
    destination_before: dict[str, object]
    account_move: dict[str, object]
    queue_policy: AdoptionQueuePolicy = AdoptionQueuePolicy.TRANSFER


@dataclass
class GivingRoster:
    """An existing giving run whose roster lock is held during recovery."""

    handle: worker_lifecycle.WorkerStateFile
    records: dict[str, object]
    before: dict[str, object]


@dataclass(frozen=True)
class RemovedGivingRoster:
    """There is no source roster to recreate or mutate."""


@dataclass
class UnitWorker:
    name: str
    role: str
    family: str
    model: str
    effort: str
    access_policy: dict[str, object]
    backend: CodexThread | ClaudeSession
    ownership: HeldBy | Paused | Adopted | Retired
    events: list[WorkerEvent] = field(default_factory=list)


@dataclass
class UnitWorkers:
    workers: dict[str, UnitWorker] = field(default_factory=dict)
    adoptions: dict[str, AdoptionIntent] = field(default_factory=dict)
    quarantined_adoptions: dict[str, dict[str, object]] = field(default_factory=dict)
    quarantined_workers: dict[str, dict[str, object]] = field(default_factory=dict)


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
    return worker_lifecycle.read_worker_state(path)


def _read_roster(run: Path) -> dict[str, object]:
    return _read(run / ROSTER)


def _atomic(path: Path, data: Mapping[str, object]) -> None:
    _ = _read(path)
    _ = worker_lifecycle.validate_worker_state(path, data)
    write_text_atomic(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def _encode_worker(worker: UnitWorker) -> dict[str, object]:
    backend = worker.backend
    ownership = worker.ownership
    if isinstance(backend, CodexThread):
        encoded_backend: dict[str, object] = {
            "kind": "CodexThread", "thread_id": backend.thread_id, "codex_home": backend.codex_home}
    else:
        encoded_backend = {"kind": "ClaudeSession", "session_id": backend.session_id}
    if isinstance(ownership, HeldBy):
        encoded_owner: dict[str, object] = {"kind": "HeldBy", "run": ownership.run}
    elif isinstance(ownership, Adopted):
        encoded_owner = {"kind": "Adopted", "to_run": ownership.to_run}
    else:
        encoded_owner = {"kind": "Paused" if isinstance(ownership, Paused) else "Retired"}
    return {"name": worker.name, "role": worker.role, "family": worker.family,
            "model": worker.model, "effort": worker.effort, "access_policy": worker.access_policy,
            "backend": encoded_backend, "ownership": encoded_owner,
            "events": [{"kind": event.kind, "cause": event.cause, "run": event.run,
                        "at": event.at, **event.details} for event in worker.events]}


def _decode_worker(name: str, record: dict[str, object]) -> UnitWorker:
    backend = _object(record.get("backend"))
    owner = _object(record.get("ownership"))
    if backend.get("kind") == "CodexThread":
        identity: CodexThread | ClaudeSession = CodexThread(
            _required(backend, "thread_id"), _required(backend, "codex_home"))
    elif backend.get("kind") == "ClaudeSession":
        identity = ClaudeSession(_required(backend, "session_id"))
    else:
        raise WorkerUnavailable(f"{name}: invalid backend")
    if owner.get("kind") == "HeldBy":
        ownership: HeldBy | Paused | Adopted | Retired = HeldBy(_required(owner, "run"))
    elif owner.get("kind") == "Adopted":
        ownership = Adopted(_required(owner, "to_run"))
    elif owner.get("kind") == "Paused":
        ownership = Paused()
    elif owner.get("kind") == "Retired":
        ownership = Retired()
    else:
        raise WorkerUnavailable(f"{name}: invalid ownership")
    events = record.get("events", [])
    if not isinstance(events, list):
        raise WorkerUnavailable(f"{name}: invalid events")
    return UnitWorker(name, _required(record, "role"), _required(record, "family"),
                      _text(record.get("model")), _text(record.get("effort")),
                      _object(record.get("access_policy")), identity, ownership,
                      [_decode_event(_object(event)) for event in cast(list[object], events)])


def _decode_event(record: dict[str, object]) -> WorkerEvent:
    kind = _required(record, "kind")
    if kind not in ("opened", "replaced", "adopted"):
        raise WorkerUnavailable(f"invalid worker event: {kind}")
    at = record.get("at")
    if not isinstance(at, (int, float)):
        raise WorkerUnavailable("invalid worker event timestamp")
    return WorkerEvent(kind,
                       _required(record, "cause"), _required(record, "run"), float(at),
                       {key: _text(record[key]) for key in ("adoption_id", "from_run", "old_thread_id", "thread_id")
                        if key in record})


def _encode_intent(intent: AdoptionIntent) -> dict[str, object]:
    return {"id": intent.identity, "name": intent.name, "source": str(intent.giving.path),
            "source_removed": isinstance(intent.giving, RemovedRun),
            "before": intent.giving.before if isinstance(intent.giving, ExistingRun) else {},
            "destination": str(intent.destination), "after": intent.after,
            "destination_before": intent.destination_before, "account_move": intent.account_move,
            "queue_policy": intent.queue_policy.value}


def _decode_intent(identity: str, record: dict[str, object]) -> AdoptionIntent:
    if _required(record, "id") != identity:
        raise WorkerUnavailable("adoption identity differs from journal key")
    source, destination = Path(_required(record, "source")), Path(_required(record, "destination"))
    if not source.is_absolute() or not destination.is_absolute() or source == destination:
        raise WorkerUnavailable("invalid adoption run paths")
    name = _required(record, "name")
    if Path(name).name != name:
        raise WorkerUnavailable("invalid adoption worker name")
    for key in ("before", "after", "destination_before", "account_move"):
        if not isinstance(record.get(key), dict):
            raise WorkerUnavailable(f"invalid adoption {key}")
    after = _object(record["after"])
    _ = _required(after, "thread_id")
    if after.get("adoption_id") != identity:
        raise WorkerUnavailable("invalid adoption destination identity")
    if after.get("adopted_from") != str(source) or after.get("status") != "paused":
        raise WorkerUnavailable("invalid adoption destination state")
    if not isinstance(record.get("source_removed", False), bool):
        raise WorkerUnavailable("invalid adoption source state")
    giving = RemovedRun(source) if record.get("source_removed") is True else ExistingRun(source, _object(record["before"]))
    if isinstance(giving, ExistingRun):
        if _required(giving.before, "thread_id") != after.get("thread_id"):
            raise WorkerUnavailable("adoption changes thread identity")
    policy = record.get("queue_policy")
    if policy is None:
        # Pre-transfer journals stored this transport flag. Only those journals
        # can refuse newly queued work instead of transferring it.
        queue_policy = (AdoptionQueuePolicy.TRANSFER if record.get("carries_work") is True
                        else AdoptionQueuePolicy.LEGACY_REFUSE_QUEUED)
    elif isinstance(policy, str):
        try:
            queue_policy = AdoptionQueuePolicy(policy)
        except ValueError as error:
            raise WorkerUnavailable("invalid adoption queue policy") from error
    else:
        raise WorkerUnavailable("invalid adoption queue policy")
    return AdoptionIntent(identity, name, giving, destination, after,
                          _object(record["destination_before"]), _object(record["account_move"]), queue_policy)


@dataclass(frozen=True)
class UnitWorkerLedger:
    path: Path

    def save(self, ledger: UnitWorkers) -> None:
        """The caller holds locked(); intermediate transaction writes are durable."""
        _atomic(self.path, {"version": 1,
                           "workers": {name: _encode_worker(worker) for name, worker in ledger.workers.items()},
                           "adoptions": {key: _encode_intent(intent) for key, intent in ledger.adoptions.items()},
                           "quarantined_adoptions": ledger.quarantined_adoptions,
                           "quarantined_workers": ledger.quarantined_workers})

    @contextmanager
    def locked(self, *, write: bool = True, deadline: float = worker_lifecycle.NO_LOCK_DEADLINE) -> Generator[UnitWorkers, None, None]:
        with worker_lifecycle.operation_deadline(deadline=deadline):
            journal = self.path.with_suffix(".move.json")
            if journal.exists():
                move = _read(journal)
                _finish_move(move)
                if not self.path.exists():
                    raise WorkerUnavailable(f"worker ledger moved to {move.get('destination')}; retry using current unit identity")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.with_suffix(".lock").open("a+", encoding="utf-8") as lock:
                worker_lifecycle.acquire_flock(lock, deadline=deadline)
                data = _read(self.path)
                ledger = UnitWorkers(
                    quarantined_adoptions={key: _object(value) for key, value in
                                           _object(data.get("quarantined_adoptions")).items()},
                    quarantined_workers={key: _object(value) for key, value in
                                         _object(data.get("quarantined_workers")).items()})
                invalid_workers: list[str] = []
                for name, record in _object(data.get("workers")).items():
                    try:
                        ledger.workers[name] = _decode_worker(name, _object(record))
                    except WorkerUnavailable as error:
                        ledger.quarantined_workers[name] = {"reason": str(error), "record": record}
                        invalid_workers.append(name)
                stored_adoptions = data.get("adoptions", {})
                if not isinstance(stored_adoptions, dict):
                    ledger.quarantined_adoptions["invalid-journal"] = {
                        "reason": "adoption journal is not an object", "intent": stored_adoptions}
                    self.save(ledger)
                    raise WorkerUnavailable(f"invalid adoption journal quarantined in {self.path}; retry command")
                invalid: list[str] = []
                for key, record in cast(dict[str, object], stored_adoptions).items():
                    try:
                        intent = _decode_intent(key, _object(record))
                        if intent.name not in ledger.workers:
                            raise WorkerUnavailable("adoption has no registered worker")
                        backend = ledger.workers[intent.name].backend
                        if not isinstance(backend, CodexThread) or backend.thread_id != intent.after.get("thread_id"):
                            raise WorkerUnavailable("adoption differs from saved backend")
                        ledger.adoptions[key] = intent
                    except WorkerUnavailable as error:
                        ledger.quarantined_adoptions[key] = {"reason": str(error), "intent": record}
                        invalid.append(key)
                if invalid or invalid_workers:
                    self.save(ledger)
                    failures: list[str] = []
                    if invalid_workers:
                        failures.append(f"worker entries: {', '.join(invalid_workers)}")
                    if invalid:
                        failures.append(f"adoption journal: {', '.join(invalid)}")
                    raise WorkerUnavailable(f"invalid {'; '.join(failures)} quarantined in {self.path}; retry command")
                yield ledger
                if write:
                    self.save(ledger)


def _repository_name(worktree: Path) -> str:
    """Name linked worktrees for their repository, keeping their path hash separate."""
    for directory in (worktree, *worktree.parents):
        marker = directory / ".git"
        if marker.is_dir():
            return directory.name
        if not marker.is_file():
            continue
        try:
            location = marker.read_text(encoding="utf-8").strip()
            if not location.startswith("gitdir:"):
                break
            git_directory = (directory / location.removeprefix("gitdir:").strip()).resolve()
            common = (git_directory / "commondir").read_text(encoding="utf-8").strip()
            repository = (git_directory / common).resolve()
            return repository.parent.name if repository.name == ".git" else repository.name
        except OSError:
            break
    return worktree.name


def _ledger_for_run(run: Path) -> UnitWorkerLedger:
    """Use explicit unit metadata, then the plan's production header, else the worktree."""
    from scripts.direct.progress_history import ProductionUnitIdentity, resolve_production_unit

    state = worker_lifecycle.read_json_object(run / STATE)
    worktree = Path(_text(state.get("working_dir")) or str(run)).expanduser().resolve()
    identity = resolve_production_unit({**state, "working_dir": str(worktree)})
    if isinstance(identity, ProductionUnitIdentity):
        from scripts.production import showrunners
        notifier = Path(os.environ.get("NOTIFIER_STATE_DIR", str(Path.home() / ".local/state/notifier")))
        production = showrunners.canonical_slug(identity.production, notifier)
        unit = identity.unit
    else:
        production = "adhoc"
        digest = hashlib.sha256(str(worktree).encode()).hexdigest()[:12]
        unit = f"{_repository_name(worktree)}-{digest}"
    if any(value in ("", ".", "..") or "/" in value or "\\" in value for value in (production, unit)):
        raise WorkerUnavailable("invalid production or unit identity")
    root = Path(os.environ.get("UNIT_WORKERS_STATE", str(Path.home() / ".local/state/unit-workers")))
    return UnitWorkerLedger(root.expanduser() / production / f"{unit}.json")


def ledger_for_run(run: Path) -> UnitWorkerLedger:
    store = _ledger_for_run(run)
    journal = store.path.with_suffix(".move.json")
    if journal.exists():
        _finish_move(_read(journal))
        store = _ledger_for_run(run)
    if store.path.parent.name != "adhoc" and (run / STATE).exists():
        state = worker_lifecycle.read_json_object(run / STATE)
        if not state.get("owner_staff_id") and (state.get("production"), state.get("unit")) != (store.path.parent.name, store.path.stem):
            from scripts.direct import progress_history
            progress_history.set_unit(run, store.path.parent.name, store.path.stem)
    return store


@contextmanager
def _roster_locked(run: Path) -> Generator[tuple[worker_lifecycle.WorkerStateFile, dict[str, object]], None, None]:
    with worker_lifecycle.locked_state(run / ROSTER) as state:
        yield state, state.read_object()


def _write_roster(handle: worker_lifecycle.WorkerStateFile, roster: dict[str, object]) -> None:
    handle.publish(json.dumps(roster, indent=2, sort_keys=True))


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
        if record.get("adopted_to") or record.get("allocation_id") != identity:
            return
        clear_allocation(record)
        from scripts.agents import codex_mesh
        codex_mesh.advance_worker_roster_revision(record)
        _write_roster(handle, roster)


def _event(kind: Literal["opened", "replaced", "adopted"], cause: str, run: Path,
           **details: str) -> WorkerEvent:
    return WorkerEvent(kind, cause, str(run), time.time(), details)


def _worker_from_record(name: str, run: Path, record: dict[str, object]) -> UnitWorker:
    from scripts.agents import codex_mesh
    from scripts.astra.design_roles import PhaseBoundDesigner
    access = codex_mesh.roster_access_policy(record)
    policy: dict[str, object] = {"kind": type(access).__name__}
    if isinstance(access, PhaseBoundDesigner):
        policy.update({"binding_generation": access.binding_generation, "launch_sandbox": access.launch_sandbox})
    settings = codex_mesh.recorded_turn_settings(record)
    return UnitWorker(name, _text(record.get("role")), "codex", settings.model,
                      settings.effort, policy,
                      CodexThread(_text(record.get("thread_id")), _text(record.get("codex_home")) or
                                  str(codex_mesh.server_record_home(_read(run / "mesh_server.json")))),
                      HeldBy(str(run)), [_event("opened", "registered existing roster", run)])


def _quarantine(store: UnitWorkerLedger, ledger: UnitWorkers, intent: AdoptionIntent, reason: str) -> None:
    ledger.quarantined_adoptions[intent.identity] = {"reason": reason, "intent": _encode_intent(intent)}
    _ = ledger.adoptions.pop(intent.identity)
    store.save(ledger)
    raise WorkerUnavailable(f"adoption {intent.identity}: {reason}; journal quarantined in {store.path}; retry command")


def _read_inbox(handle: worker_lifecycle.WorkerStateFile) -> dict[str, object]:
    return handle.read_object()


def _giving_inbox(locks: ExitStack, source: Path, name: str) -> tuple[worker_lifecycle.WorkerStateFile, dict[str, object]]:
    try:
        inbox = locks.enter_context(worker_lifecycle.locked_state(
            source / f"{name}.pending.json", mode=fcntl.LOCK_EX | fcntl.LOCK_NB))
    except BlockingIOError as error:
        raise WorkerUnavailable(f"{name}: pending inbox is in use; retry adoption recovery") from error
    return inbox, _read_inbox(inbox)


def _merge_pending(inbox: worker_lifecycle.WorkerStateFile, target_inbox: worker_lifecycle.WorkerStateFile, after: dict[str, object],
                   identity: str, messages: dict[str, object], saved: dict[str, object]) -> None:
    if saved.get("adoption_id") != identity:
        queue = cast(list[str], messages.get("messages", [])) + cast(list[str], saved.get("messages", []))
        _write_inbox(target_inbox, {"thread_id": after["thread_id"], "messages": queue,
                                    "accepting_messages": True, "adoption_id": identity})
    _write_inbox(inbox, {"thread_id": after["thread_id"], "messages": [], "accepting_messages": False})


@dataclass(frozen=True)
class AdoptionInboxTransfer:
    giving: worker_lifecycle.WorkerStateFile
    taking: worker_lifecycle.WorkerStateFile
    saved: dict[str, object]


@dataclass(frozen=True)
class NoAdoptionInboxTransfer:
    """The source is removed or the adoption refuses queued transport."""


def _complete_adoption(store: UnitWorkerLedger, ledger: UnitWorkers, transaction: AdoptionIntent) -> None:
    """Roll forward under ledger, membership, lifecycle, inbox, then roster locks."""
    from scripts.agents import codex_mesh
    source, destination = transaction.giving.path, transaction.destination
    name, identity = transaction.name, transaction.identity
    after = transaction.after
    with worker_lifecycle.membership_operation(destination, *((source,) if isinstance(transaction.giving, ExistingRun) else ())):
        with worker_lifecycle.lock_workers(
                worker_lifecycle.WorkerIdentity(destination, name),
                *(() if isinstance(transaction.giving, RemovedRun) else (worker_lifecycle.WorkerIdentity(source, name),))
        ), ExitStack() as locks:
            messages: dict[str, object] = {}
            inbox_transfer: AdoptionInboxTransfer | NoAdoptionInboxTransfer = NoAdoptionInboxTransfer()
            if isinstance(transaction.giving, ExistingRun):
                inbox, messages = _giving_inbox(locks, source, name)
                if transaction.queue_policy is AdoptionQueuePolicy.TRANSFER:
                    target_inbox, saved = _giving_inbox(locks, destination, name)
                    inbox_transfer = AdoptionInboxTransfer(inbox, target_inbox, saved)
            # A removed run has no roster to recreate. Its durable backend and owner
            # remain in the ledger; only the taking roster participates in recovery.
            if isinstance(transaction.giving, ExistingRun):
                handle, records = locks.enter_context(_roster_locked(source))
                giving: GivingRoster | RemovedGivingRoster = GivingRoster(handle, records, transaction.giving.before)
            else:
                giving = RemovedGivingRoster()
            taking, new = locks.enter_context(_roster_locked(destination))
            current = _object(giving.records.get(name)) if isinstance(giving, GivingRoster) else {}
            target = _object(new.get(name))
            source_after: dict[str, object] = {}
            if isinstance(giving, GivingRoster):
                source_after = dict(giving.before)
                source_after.update({"status": "adopted", "adopted_to": str(destination), "adoption_id": identity})
                codex_mesh.advance_worker_roster_revision(source_after)
            source_done = isinstance(giving, RemovedGivingRoster) or current == source_after
            destination_done = target == after
            # Unknown revisions are never overwritten by transaction recovery.
            if ((not source_done and isinstance(giving, GivingRoster) and current != giving.before)
                    or (not destination_done and target != transaction.destination_before)):
                # Undo only our own writes. Preserve newer owners and their messages.
                if isinstance(giving, GivingRoster) and source_done:
                    restored = dict(giving.before)
                    restored["revision"] = current.get("revision", 0)
                    codex_mesh.advance_worker_roster_revision(restored)
                    giving.records[name] = restored
                    _write_roster(giving.handle, giving.records)
                if destination_done:
                    if transaction.destination_before:
                        restored_target = dict(transaction.destination_before)
                        restored_target["revision"] = target.get("revision", 0)
                        codex_mesh.advance_worker_roster_revision(restored_target)
                        new[name] = restored_target
                    else:
                        _ = new.pop(name)
                    _write_roster(taking, new)
                worker = ledger.workers[name]
                if not isinstance(worker.ownership, Retired):
                    worker.ownership = HeldBy(str(source))
                _quarantine(store, ledger, transaction, "roster revision changed; rolled back owned writes")
            if messages.get("messages") and transaction.queue_policy is AdoptionQueuePolicy.LEGACY_REFUSE_QUEUED:
                if isinstance(giving, GivingRoster) and source_done:
                    restored = dict(giving.before)
                    restored["revision"] = current.get("revision", 0)
                    codex_mesh.advance_worker_roster_revision(restored)
                    giving.records[name] = restored
                    _write_roster(giving.handle, giving.records)
                if destination_done:
                    if transaction.destination_before:
                        restored_target = dict(transaction.destination_before)
                        restored_target["revision"] = target.get("revision", 0)
                        codex_mesh.advance_worker_roster_revision(restored_target)
                        new[name] = restored_target
                    else:
                        _ = new.pop(name)
                    _write_roster(taking, new)
                # Record the rolled-back destination even when no worker was
                # published there. Stable locking no longer creates data files.
                if not (destination / ROSTER).exists():
                    _write_roster(taking, new)
                ledger.workers[name].ownership = HeldBy(str(source))
                _quarantine(store, ledger, transaction, "source received a message; rolled back owned writes")
            if not destination_done:
                marker = transaction.account_move
                if marker:
                    _atomic(destination / f"{name}.account-move.json", marker)
                else:
                    (destination / f"{name}.account-move.json").unlink(missing_ok=True)
                new[name] = dict(after)
                _write_roster(taking, new)
            if not source_done and isinstance(giving, GivingRoster):
                giving.records[name] = source_after
                _write_roster(giving.handle, giving.records)
            if isinstance(inbox_transfer, AdoptionInboxTransfer):
                inbox, target_inbox, saved = inbox_transfer.giving, inbox_transfer.taking, inbox_transfer.saved
                # Publish the taking roster before routing source senders to it.
                # After the source fence, no sender can append behind this copy.
                _merge_pending(inbox, target_inbox, after, identity, messages, saved)
            worker = ledger.workers[name]
            worker.ownership = Adopted(str(destination))
            if not any(event.details.get("adoption_id") == identity for event in worker.events):
                worker.events.append(_event("adopted", "new run", destination, adoption_id=identity,
                                            from_run=str(source)))
            store.save(ledger)
            _ = ledger.adoptions.pop(identity)
            store.save(ledger)
        # Admission projection belongs here, with membership held after worker locks.


def _recover(store: UnitWorkerLedger, ledger: UnitWorkers) -> None:
    for transaction in list(ledger.adoptions.values()):
        _complete_adoption(store, ledger, transaction)


def recover_adoptions(run: Path) -> None:
    store = ledger_for_run(run)
    # Reading an unrelated mesh must not create a unit ledger as a side effect.
    if not store.path.exists():
        return
    with store.locked() as ledger:
        _recover(store, ledger)


def _adopt(store: UnitWorkerLedger, ledger: UnitWorkers, source: Path, destination: Path, name: str,
           *, permission: worker_admission.WorkerStartAllowed, owner_pid: int, reserve: bool = False) -> str:
    from scripts.agents import codex_mesh
    if not name or Path(name).name != name:
        raise WorkerUnavailable("invalid worker name")
    if source == destination:
        raise WorkerUnavailable("source and destination runs must differ")
    source_present = source.is_dir()
    if source_present and _ledger_for_run(source).path != store.path:
        raise WorkerUnavailable("worker belongs to another unit")
    history = codex_mesh.worker_run_history(str(source), name) if source_present else ()
    saved = ledger.workers.get(name)
    native = (_text(_object(_read_roster(source).get(name)).get("thread_id")) if source_present
              else saved.backend.thread_id if saved is not None and isinstance(saved.backend, CodexThread) else "")
    if not native:
        raise WorkerUnavailable(f"{name}: adoption has no native backend identity")
    published = worker_admission.publish(permission, native)
    if published.row.name != name:
        raise WorkerUnavailable("published worker names another adoption")
    with worker_lifecycle.membership_operation(destination, *((source,) if source_present else ())):
        before: dict[str, object]
        with worker_lifecycle.lock_workers(
                worker_lifecycle.WorkerIdentity(destination, name),
                *((worker_lifecycle.WorkerIdentity(source, name),) if source_present else ())
        ), ExitStack() as locks:
            if source_present:
                before = _object(_read_roster(source).get(name))
                giving: ExistingRun | RemovedRun = ExistingRun(source, before)
            else:
                worker = ledger.workers.get(name)
                if worker is None or not isinstance(worker.backend, CodexThread):
                    raise WorkerUnavailable(f"{name}: removed run has no saved Codex backend")
                before = {"thread_id": worker.backend.thread_id, "codex_home": worker.backend.codex_home,
                          "role": worker.role, "family": worker.family, "turn_model": worker.model,
                          "turn_effort": worker.effort, "access_policy": worker.access_policy, "status": "paused"}
                giving = RemovedRun(source)
            new = _read_roster(destination)
            if (before.get("adopted_to") == str(destination)
                    and _object(new.get(name)).get("thread_id") == before.get("thread_id")):
                return name
            if before.get("adopted_to"):
                raise WorkerUnavailable(f"{name}: already adopted to {before['adopted_to']}")
            destination_before = _object(new.get(name))
            if name in new and not destination_before.get("adopted_to"):
                raise WorkerUnavailable(f"{name}: destination already has this name")
            registered = ledger.workers.get(name)
            paused_owner = registered is not None and isinstance(registered.ownership, Paused)
            reason = codex_mesh.worker_idle_reason(str(source if source_present else destination), name, before,
                                                  prior_runs=history[1:], paused=paused_owner, adopting=True)
            if reason:
                raise WorkerUnavailable(f"{name}: {reason}")
            if name not in ledger.workers:
                ledger.workers[name] = _worker_from_record(name, source, before)
            worker = ledger.workers[name]
            owner = worker.ownership
            if (isinstance(owner, Retired)
                    or isinstance(owner, HeldBy) and owner.run != str(source)
                    or isinstance(owner, Adopted) and owner.to_run != str(source)):
                raise WorkerUnavailable(f"{name}: ownership changed")
            # Eligibility probes acquire an inbox; no roster lock may precede them.
            if source_present:
                _, current_source = locks.enter_context(_roster_locked(source))
                if _object(current_source.get(name)) != before:
                    raise WorkerUnavailable(f"{name}: source changed during adoption eligibility")
            _, current_destination = locks.enter_context(_roster_locked(destination))
            if current_destination != new:
                raise WorkerUnavailable(f"{name}: destination changed during adoption eligibility")
            identity = str(uuid.uuid4())
            after = dict(before)
            for key in ("launcher_pid", "port", "turn_id", "paused_from", "paused_turn"):
                _ = after.pop(key, None)
            after.update({"status": "paused", "adoption_id": identity, "adopted_from": str(source)})
            after["preserved_work"] = True
            if not paused_owner:
                clear_allocation(after)
            after["giving_runs"] = list(dict.fromkeys([str(source), *history[1:]]))
            after["unit_ledger"] = str(store.path)
            if reserve and not allocation_is_live(after):
                _reserve_allocation(after, owner_pid)
            codex_mesh.advance_worker_roster_revision(after)
            transaction = AdoptionIntent(identity, name, giving, destination, after, destination_before,
                                         _read(source / f"{name}.account-move.json"))
            ledger.adoptions[identity] = transaction
            store.save(ledger)
        _complete_adoption(store, ledger, transaction)
        if _object(_read_roster(destination).get(name)).get("adoption_id") != identity:
            raise WorkerUnavailable(f"{name}: adoption rolled back because the source changed")
        return name


def adopt(source: Path, destination: Path, name: str) -> str:
    source, destination = source.resolve(), destination.resolve()
    store = ledger_for_run(destination)
    with store.locked() as ledger:
        _recover(store, ledger)
        source_entry = _object(_read_roster(source).get(name))
        saved = ledger.workers.get(name)
        if not source_entry and saved is None:
            raise WorkerUnavailable(f"{name}: no worker to adopt")
        role = _text(source_entry.get("role")) if source_entry else saved.role if saved is not None else ""
    allowed = worker_admission.admit(name, destination, role=role)
    with store.locked() as ledger:
        _recover(store, ledger)
        return _adopt(store, ledger, source, destination, name, permission=allowed, owner_pid=os.getpid())


def note_backend(run: Path, name: str, thread_id: str, home: Path) -> None:
    """A successful resume records its actual home, only for the current owner."""
    run = run.expanduser().resolve()
    store = ledger_for_run(run)
    with store.locked() as ledger:
        _recover(store, ledger)
        worker = ledger.workers.get(name)
        if worker is None:
            return
        owner = worker.ownership
        if ((isinstance(owner, HeldBy) and owner.run == str(run))
                or (isinstance(owner, Adopted) and owner.to_run == str(run))):
            with worker_lifecycle.lock_worker(run, name), _roster_locked(run) as (handle, roster):
                record = dict(_object(roster.get(name)))
                if record.get("adopted_to") or record.get("thread_id") != thread_id:
                    return
                backend = worker.backend
                if isinstance(backend, CodexThread) and backend.thread_id != thread_id:
                    worker.events.append(_event("replaced", "resume refused old thread", run,
                                                old_thread_id=backend.thread_id, thread_id=thread_id))
                worker.role = _text(record.get("role")) or worker.role
                worker.access_policy = _object(record.get("access_policy")) or worker.access_policy
                from scripts.agents import codex_mesh
                settings = codex_mesh.recorded_turn_settings(record)
                worker.model, worker.effort = settings.model, settings.effort
                worker.backend = CodexThread(thread_id, str(home.resolve()))
                worker.ownership = HeldBy(str(run))
                # The first exact-thread resume completes the incoming handoff.
                # Future follows use ordinary live-turn checks and replacement.
                _ = record.pop("adopted_from", None)
                record.update({"codex_home": str(home.resolve()), "unit_ledger": str(store.path),
                               "role": worker.role,
                               "access_policy": worker.access_policy})
                codex_mesh.advance_worker_roster_revision(record)
                roster[name] = record
                _write_roster(handle, roster)


def _reserve(run: Path, name: str, store: UnitWorkerLedger, owner_pid: int, *, paused: bool = False) -> None:
    from scripts.agents import codex_mesh
    with worker_lifecycle.lock_worker(run, name), _roster_locked(run) as (handle, roster):
        record = dict(_object(roster.get(name)))
        if not record or record.get("adopted_to"):
            raise WorkerUnavailable(f"{name}: no longer owned by this run")
        if codex_mesh.worker_launcher_attached(record):
            raise WorkerUnavailable(f"{name}: launcher attached during resolution")
        if allocation_is_live(record) or (record.get("continuation_owed") and not paused):
            raise WorkerUnavailable(f"{name}: work already reserved or owed")
        with ExitStack() as locks:
            _, messages = _giving_inbox(locks, run, name)
            if messages.get("messages") and not paused:
                raise WorkerUnavailable(f"{name}: pending messages are queued")
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


@dataclass(frozen=True)
class NoLauncherWorkingDirectory:
    """Reviewer naming requires a working directory in progress state."""


NO_LAUNCHER_WORKING_DIRECTORY = NoLauncherWorkingDirectory()


def _owned_by_run(worker: UnitWorker, run: Path) -> bool:
    owner = worker.ownership
    if isinstance(owner, HeldBy):
        return Path(owner.run).resolve() == run.resolve()
    if isinstance(owner, Adopted):
        return Path(owner.to_run).resolve() == run.resolve()
    return (isinstance(owner, Paused) and bool(worker.events)
            and Path(worker.events[-1].run).resolve() == run.resolve())


def _roster_repair_hint(run: Path, name: str, reason: str) -> WorkerUnavailable:
    import shlex
    return WorkerUnavailable(f"{name}: {run / ROSTER}: {reason}; "
                             + "run workers.py repair-roster --session-dir " + shlex.quote(str(run)))


def _reconcile_ledger_roster(run: Path, ledger: UnitWorkers, roster: Mapping[str, object]) -> None:
    """Absence is not admission evidence when the ledger already owns this run."""
    for name, worker in sorted(ledger.workers.items()):
        if not _owned_by_run(worker, run):
            continue
        record = _object(roster.get(name))
        if not record:
            raise _roster_repair_hint(run, name, "ledger worker is missing from roster")
        if isinstance(worker.backend, CodexThread) and record.get("thread_id") != worker.backend.thread_id:
            raise _roster_repair_hint(run, name, "ledger and roster thread differ")


def _repair_design_limits(name: str, thread: str, record: dict[str, object]) -> None:
    """Restore held-role limits from their authority, never from a thread listing."""
    from dataclasses import asdict
    from scripts.astra import design_roles
    matched = False
    for path in sorted(design_roles.state_root().glob("*/design.json")):
        design = design_roles.read_design(path.parent.name)
        for role in design.roles:
            if isinstance(role.lifecycle, design_roles.RoleNotOpened) or role.lifecycle.thread_id != thread:
                continue
            if role.name != name or matched:
                raise WorkerUnavailable(f"{name}: design authorities disagree about thread ownership")
            matched = True
            if role.assignment_claim:
                raise WorkerUnavailable(f"{name}: design assignment is in progress; finish design recovery first")
            for journal in ("transition.json", "transfer.json"):
                if (path.parent / journal).exists():
                    raise WorkerUnavailable(f"{name}: design transfer is unfinished: {path.parent / journal}")
            policy = design_roles.access_policy(design, role.role)
            record.update({"held_design_id": design.placement.design_id, "held_role": role.role,
                           "access_policy": {"kind": type(policy).__name__, **asdict(policy)}})
    if not matched and (record.get("held_design_id")
                        or _object(record.get("access_policy")).get("kind") == "PhaseBoundDesigner"):
        raise WorkerUnavailable(f"{name}: access_policy or held_design_id lacks design authority; repair refused")


def _confirm_repair_restrictions(run: Path, name: str, damaged: Mapping[str, object],
                                 authority: Mapping[str, object]) -> None:
    """Only execution/history fields may be rebuilt without authorization evidence.

    Unknown fields fail closed too: a future restriction must not silently become
    disposable telemetry when an older repair command encounters it.
    """
    execution_history = {
        "thread_id", "status", "revision", "launcher_pid", "settled_launcher_pid", "port",
        "turn_id", "context_tokens", "paused_from", "paused_turn", "previous_status",
        "continuation_owed", "preserved_work", "task", "model", "effort", "turn_model",
        "turn_effort", "adopted_from", "adoption_id", "codex_home",
    }
    for field, value in damaged.items():
        if field in execution_history:
            continue
        if (field not in authority
                or json.dumps(value, sort_keys=True) != json.dumps(authority[field], sort_keys=True)):
            raise WorkerUnavailable(f"{name}: {run / ROSTER}: damaged {field} is not confirmed by "
                                    + "ledger and design authority; repair refused")


def _repair_live_state(run: Path, name: str, record: dict[str, object]) -> None:
    """An exact thread read supplies state; no server or no evidence refuses repair."""
    from scripts.agents import codex_mesh
    thread = _required(record, "thread_id")
    evidence: list[tuple[int, codex_mesh.ThreadIdle | codex_mesh.ThreadLive | codex_mesh.ThreadActiveWithoutTurn]] = []
    errors: list[str] = []
    for port in codex_mesh.recorded_run_ports(str(run)):
        try:
            client = codex_mesh.Client(port, f"repair-roster-{os.getpid()}")
            try:
                state = codex_mesh.read_worker_thread_state(client, thread)
                if isinstance(state, codex_mesh.ThreadStateUnknown):
                    errors.append(state.reason)
                else:
                    evidence.append((port, state))
            finally:
                client.close()
        except (OSError, ConnectionError, SystemExit) as error:
            errors.append(str(error))
    if len(evidence) != 1:
        raise WorkerUnavailable(f"{name}: exact thread state is ambiguous or unavailable; " + "; ".join(errors))
    port, state = evidence[0]
    record["port"] = port
    if isinstance(state, codex_mesh.ThreadIdle):
        record["status"] = "paused"
        record["paused_turn"] = False
    elif isinstance(state, codex_mesh.ThreadLive):
        record.update({"status": "running", "turn_id": state.turn_id})
    else:
        record["status"] = "running"
    # There is no launcher to invent. Running without one remains busy until reopen proves idle.


def repair_roster(run: Path) -> str:
    """Rebuild only missing identities, retaining evidence and refusing ambiguous ownership."""
    from scripts.agents import codex_mesh
    run = run.expanduser().resolve()
    store = _ledger_for_run(run)
    if not store.path.exists():
        raise WorkerUnavailable(f"{store.path}: no ledger; cannot establish worker ownership")
    with store.locked(write=False) as ledger:
        if ledger.adoptions or ledger.quarantined_adoptions or ledger.quarantined_workers:
            raise WorkerUnavailable(f"{store.path}: unresolved or quarantined ownership; finish transaction recovery first")
        owned = {name: worker for name, worker in ledger.workers.items() if _owned_by_run(worker, run)}
        with worker_lifecycle.membership_operation(run), worker_lifecycle.lock_workers(*(
                worker_lifecycle.WorkerIdentity(run, name) for name in sorted(set(owned) | set(_repair_survivors(run))))):
            with worker_lifecycle.locked_state(run / ROSTER) as target:
                survivors = _repair_survivors(run, locked=True)
                damaged = False
                try:
                    original = target.read_object()
                except worker_lifecycle.WorkerStateUnreadable:
                    original = survivors
                    damaged = True
                snapshot = (run / ROSTER).read_bytes() if (run / ROSTER).exists() else b""
                ownership_rows = original
                if damaged:
                    try:
                        damaged_object = cast(object, json.loads(snapshot))
                    except (ValueError, UnicodeError):
                        damaged_object = None
                    if isinstance(damaged_object, dict):
                        ownership_rows = cast(dict[str, object], damaged_object)
                        unknown = set(ownership_rows) - set(survivors) - set(owned)
                        if unknown:
                            raise WorkerUnavailable(f"{run / ROSTER}: damaged rows lack ledger ownership: "
                                                    + ", ".join(sorted(unknown)))
            # Server evidence is probed without a roster lock. Membership and all worker
            # lifecycles remain owned; final publication also compares the complete bytes.
            _repair_ownership_evidence(run, ledger, ownership_rows, original)
            repaired = dict(original)
            changed: list[str] = []
            for name, worker in owned.items():
                entry = _object(original.get(name))
                if not isinstance(worker.backend, CodexThread):
                    raise WorkerUnavailable(f"{name}: cannot rebuild a non-Codex mesh entry")
                if entry:
                    if entry.get("thread_id") != worker.backend.thread_id:
                        raise _roster_repair_hint(run, name, "surviving identity conflicts with ledger; refused")
                    if codex_mesh.worker_launcher_attached(entry):
                        raise WorkerUnavailable(f"{name}: launcher is live; quiesce launchers before repair")
                    continue
                # Full earlier roster entries retain their restrictions and revision evidence.
                evidence: list[dict[str, object]] = []
                for prior in _runs(ledger):
                    if prior == run or not (prior / ROSTER).exists():
                        continue
                    previous = _object(_read_roster(prior).get(name))
                    if previous.get("thread_id") == worker.backend.thread_id:
                        if codex_mesh.worker_launcher_attached(previous):
                            raise WorkerUnavailable(f"{name}: earlier launcher is live: {prior}")
                        if previous.get("transferring_role") or previous.get("role_redirect"):
                            raise WorkerUnavailable(f"{name}: design transfer fence requires design recovery")
                        if previous.get("adopted_to") not in (None, "", str(run)):
                            raise WorkerUnavailable(f"{name}: another run owns the earlier roster entry")
                        evidence.append(previous)
                entry = dict(evidence[-1]) if evidence else {}
                damaged_entry = _object(ownership_rows.get(name))
                if damaged_entry:
                    evidence.append(damaged_entry)
                authority: dict[str, object] = {
                    "thread_id": worker.backend.thread_id, "codex_home": worker.backend.codex_home,
                    "role": worker.role, "model": worker.model, "effort": worker.effort,
                    "access_policy": worker.access_policy, "unit_ledger": str(store.path)}
                _repair_design_limits(name, worker.backend.thread_id, authority)
                _confirm_repair_restrictions(run, name, damaged_entry, authority)
                entry.update(damaged_entry)
                entry.update(authority)
                for key in ("adopted_to", "launcher_pid", "turn_id", "context_tokens"):
                    _ = entry.pop(key, None)
                _repair_live_state(run, name, entry)
                # A new fence is greater than all observed revisions, never a reset to zero.
                revisions = [codex_mesh.worker_roster_revision(value) for value in evidence]
                entry["revision"] = max(time.time_ns(), max(revisions, default=0) + 1)
                repaired[name] = entry
                changed.append(name)
            if not changed and not damaged:
                return f"roster already consistent: {run / ROSTER}"
            if not owned and damaged:
                raise WorkerUnavailable(f"{run / ROSTER}: no ledger identities permit reconstruction")
            with worker_lifecycle.locked_state(run / ROSTER):
                current = (run / ROSTER).read_bytes() if (run / ROSTER).exists() else b""
                if current != snapshot:
                    raise WorkerUnavailable(f"{run / ROSTER}: changed during repair; retry")
                if damaged:
                    backup = (run / ROSTER).with_name(ROSTER + ".damaged")
                    if backup.exists() and backup.read_bytes() != snapshot:
                        digest = hashlib.sha256(snapshot).hexdigest()
                        backup = backup.with_name(backup.name + "." + digest)
                    if backup.exists():
                        if backup.read_bytes() != snapshot:
                            raise WorkerUnavailable(f"{backup}: conflicting damaged evidence; "
                                                    + "preserve and move that evidence aside before retrying repair-roster")
                    else:
                        write_bytes_atomic(backup, snapshot)
                _ = worker_lifecycle.validate_worker_state(run / ROSTER, repaired)
                # Explicit repair is the sole bypass of the unreadable-current publication guard.
                write_text_atomic(run / ROSTER, json.dumps(repaired, indent=2, sort_keys=True) + "\n")
                return f"repaired roster: {run / ROSTER}; restored " + ", ".join(changed)


def _repair_ownership_evidence(run: Path, ledger: UnitWorkers, rows: Mapping[str, object],
                               survivors: Mapping[str, object]) -> None:
    """Damage to telemetry never removes a launcher's or transfer's ownership fence."""
    from scripts.agents import codex_mesh
    for name, raw in rows.items():
        entry = _object(raw)
        malformed = name not in survivors
        if "launcher_pid" in entry and (type(entry["launcher_pid"]) is not int or entry["launcher_pid"] < 0):
            raise WorkerUnavailable(f"{name}: ambiguous launcher evidence in {run / ROSTER}; repair refused")
        if codex_mesh.worker_launcher_attached(entry):
            raise WorkerUnavailable(f"{name}: launcher is live; quiesce launchers before repair")
        for key in ("transferring_role", "role_redirect"):
            if key in entry and (not isinstance(entry[key], dict) or entry[key]):
                raise WorkerUnavailable(f"{name}: unresolved transfer fence {key}; finish design recovery first")
        for key in ("adopted_to", "held_design_id", "thread_id"):
            if key in entry and not isinstance(entry[key], str):
                raise WorkerUnavailable(f"{name}: ambiguous {key} evidence in {run / ROSTER}; repair refused")
        registered = ledger.workers.get(name)
        if (registered is not None and not _owned_by_run(registered, run) and not entry.get("adopted_to")):
            raise WorkerUnavailable(f"{name}: ledger ownership belongs elsewhere; repair refused")
        if not malformed:
            continue
        if entry.get("adopted_to"):
            raise WorkerUnavailable(f"{name}: damaged row records adoption to {entry['adopted_to']}; finish adoption recovery first")
        if (entry.get("thread_id") and registered is not None and isinstance(registered.backend, CodexThread)
                and entry["thread_id"] != registered.backend.thread_id):
            raise WorkerUnavailable(f"{name}: damaged thread identity conflicts with ledger; repair refused")
        revision = entry.get("revision", 0)
        if type(revision) is not int or revision < 0:
            raise WorkerUnavailable(f"{name}: ambiguous revision evidence; repair refused")


def _repair_survivors(run: Path, *, locked: bool = False) -> dict[str, object]:
    """Preserve every valid row of an otherwise damaged object; never guess partial JSON."""
    path = run / ROSTER
    try:
        return worker_lifecycle.read_worker_state(path) if locked else _read_roster(run)
    except worker_lifecycle.WorkerStateUnreadable:
        try:
            raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, UnicodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        surviving: dict[str, object] = {}
        for name, value in cast(dict[str, object], raw).items():
            try:
                _ = worker_lifecycle.validate_worker_state(path, {name: value})
                surviving[name] = value
            except worker_lifecycle.WorkerStateUnreadable:
                continue
        return surviving


def resolve(run: Path, role: str, *, model: str = "", effort: str = "",
            working_dir: Path | NoLauncherWorkingDirectory = NO_LAUNCHER_WORKING_DIRECTORY) -> str:
    """Return an idle worker, carry one over, reopen one, or open the next name."""
    return _resolve(run, role, os.getpid(), model=model, effort=effort, working_dir=working_dir)


def _resolve(run: Path, role: str, owner_pid: int, *, model: str = "", effort: str = "",
             new_name: str = "",
             working_dir: Path | NoLauncherWorkingDirectory = NO_LAUNCHER_WORKING_DIRECTORY) -> str:
    from scripts.agents import codex_mesh
    if role not in ROLES:
        raise WorkerUnavailable(f"unsupported worker role: {role}")
    run = run.expanduser().resolve()
    store = ledger_for_run(run)
    reviewer_base = _reviewer_destination(run, working_dir) if role == "review" else role
    with store.locked(write=False) as ledger:
        try:
            with worker_lifecycle.locked_state(run / ROSTER, mode=fcntl.LOCK_SH) as roster:
                _reconcile_ledger_roster(run, ledger, roster.read_object())
        except worker_lifecycle.WorkerStateUnreadable as error:
            name = next((name for name, worker in ledger.workers.items() if _owned_by_run(worker, run)), "worker")
            raise _roster_repair_hint(run, name, str(error)) from error
    permissions = {name: worker_admission.admit(name, run, role=role)
                   for name, worker in ledger.workers.items()
                   if worker.role == role and not isinstance(worker.ownership, Retired)}
    for name, value in _read_roster(run).items():
        if _object(value).get("role") == role and name not in permissions:
            permissions[name] = worker_admission.admit(name, run, role=role)
    with store.locked() as ledger:
        _recover(store, ledger)
        if codex_mesh.finished_run_status(str(run)):
            raise WorkerUnavailable("run is finished; use progress_history.py start-phase to reactivate it, " +
                                    "or workers.py resolve --session-dir <new-run> to carry the worker forward")
        roster = _read_roster(run)
        _reconcile_ledger_roster(run, ledger, roster)
        for name, value in sorted(roster.items(), key=lambda item: (role == "review" and item[0] != reviewer_base, item[0])):
            record = _object(value)
            if record.get("role") != role or record.get("adopted_to") or new_name:
                continue
            registered = ledger.workers.get(name)
            if registered is not None and isinstance(registered.ownership, Retired):
                continue
            paused = record.get("status") == "paused" or registered is not None and isinstance(registered.ownership, Paused)
            if record.get("status") in ("done", "failed", "paused") and not codex_mesh.worker_idle_reason(
                    str(run), name, record, prior_runs=codex_mesh.worker_run_history(str(run), name)[1:], paused=paused):
                if name not in ledger.workers:
                    ledger.workers[name] = _worker_from_record(name, run, record)
                try:
                    permission = permissions.get(name)
                    if permission is None:
                        raise WorkerUnavailable(f"{name}: worker changed during admission")
                    _ = worker_admission.publish(permission, _text(record.get("thread_id")))
                    _reserve(run, name, store, owner_pid, paused=paused)
                except WorkerUnavailable:
                    continue
                ledger.workers[name].ownership = HeldBy(str(run))
                return name
        for name, worker in list(ledger.workers.items()):
            if new_name or worker.role != role or isinstance(worker.ownership, Retired):
                continue
            owner = worker.ownership
            previous_runs: list[str] = []
            if isinstance(owner, HeldBy):
                previous_runs.append(owner.run)
            elif isinstance(owner, Adopted):
                previous_runs.append(owner.to_run)
            if isinstance(owner, Paused):
                previous_runs = [event.run for event in reversed(worker.events)][:1]
            for previous in previous_runs:
                if previous == str(run):
                    continue
                try:
                    permission = permissions.get(name)
                    if permission is None:
                        raise WorkerUnavailable(f"{name}: worker changed during admission")
                    return _adopt(store, ledger, Path(previous), run, name, permission=permission,
                                  reserve=True, owner_pid=owner_pid)
                except WorkerUnavailable:
                    continue
        for name, value in sorted(roster.items(), key=lambda item: (role == "review" and item[0] != reviewer_base, item[0])):
            record = _object(value)
            if (record.get("role") != role or new_name
                    or record.get("status") not in ("ended", "running", "starting")):
                continue
            if codex_mesh.worker_idle_reason(str(run), name, record,
                                             prior_runs=codex_mesh.worker_run_history(str(run), name)[1:]):
                continue
            if codex_mesh.command_reopen(argparse.Namespace(
                    session_dir=str(run), to=name, adoption_recovered=True, quiet=True)) == 0:
                if name not in ledger.workers:
                    ledger.workers[name] = _worker_from_record(name, run, record)
                try:
                    _reserve(run, name, store, owner_pid)
                except WorkerUnavailable:
                    continue
                ledger.workers[name].ownership = HeldBy(str(run))
                return name
        # Roles are metadata; suffixes only distinguish concurrent workers.
        occupied = dict(roster)
        name = _next_worker_name(ledger, occupied, reviewer_base)
        if new_name:
            if new_name in ledger.workers or new_name in roster:
                raise WorkerUnavailable(f"{new_name}: worker name already exists")
            name = new_name
        record = codex_mesh.open_unit_worker(str(run), name, role, model=model, effort=effort)
        ledger.workers[name] = _worker_from_record(name, run, record)
        ledger.workers[name].events = [_event("opened", "no idle worker for role", run)]
        _reserve(run, name, store, owner_pid)
        return name


def _reviewer_destination(
    run: Path, working_dir: Path | NoLauncherWorkingDirectory = NO_LAUNCHER_WORKING_DIRECTORY,
) -> str:
    from scripts.direct import plan_history
    from scripts.production import staff

    state = worker_lifecycle.read_json_object(run / STATE)
    current = plan_history.staff_directory(dict(state), staff.initial_snapshot())
    directory = _text(current.get("working_dir"))
    if not directory and isinstance(working_dir, Path):
        directory = str(working_dir.expanduser().resolve())
    if not directory:
        raise WorkerUnavailable("invalid worker state: missing working_dir")
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

    return sorted(
        (item.record for item in registry.entries if sessions.running_session(item.record)),
        key=lambda session: session["updatedAt"], reverse=True,
    )


def resolve_live_unit_director(
    run: Path, record: staff.StaffSnapshot, sources: unit_lookup.MemberSources,
    running: Sequence[SessionRecord], dependencies: Callable[[Path], None],
) -> LiveUnitDirector | NoLiveUnitDirector:
    """Resolve the current director from the live staff membership."""
    from scripts.lib import unit_runs
    from scripts.production import unit_lookup

    director = unit_runs.director(run, dependencies=dependencies)
    for session in running:
        if director and session["sessionId"] == director:
            return LiveUnitDirector(session["sessionId"], session["pid"])
    dependencies(run / STATE)
    context = worker_lifecycle.read_json_object(run / STATE)
    production, unit = context.get("production"), context.get("unit")
    if isinstance(production, str) and isinstance(unit, str) and production and unit:
        try:
            marked = unit_lookup.marked_unit(production, unit, record=record, sources=sources, running=running)
        except OSError:
            return NoLiveUnitDirector()
        if marked is not None and isinstance(marked.claude, unit_lookup.LiveClaude):
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


def ledger_path(production: str, unit: str) -> Path:
    if any(not value or value in (".", "..") or "/" in value or "\\" in value for value in (production, unit)):
        raise WorkerUnavailable("invalid production or unit identity")
    root = Path(os.environ.get("UNIT_WORKERS_STATE", str(Path.home() / ".local/state/unit-workers")))
    return root.expanduser() / production / f"{unit}.json"


def _runs(ledger: UnitWorkers) -> tuple[Path, ...]:
    runs: set[Path] = set()
    for worker in ledger.workers.values():
        runs.update(Path(event.run) for event in worker.events)
        if isinstance(worker.ownership, HeldBy):
            runs.add(Path(worker.ownership.run))
        elif isinstance(worker.ownership, Adopted):
            runs.add(Path(worker.ownership.to_run))
    return tuple(sorted(runs))


def pause_unit(production: str, unit: str) -> None:
    from scripts.agents import codex_mesh
    with worker_lifecycle.operation_deadline(timeout=codex_mesh.PAUSE_INTERRUPT_SECS):
        store = UnitWorkerLedger(ledger_path(production, unit))
        if not store.path.exists():
            return
        with store.locked() as ledger:
            _recover(store, ledger)
            for worker in ledger.workers.values():
                if not isinstance(worker.ownership, Retired):
                    worker.ownership = Paused()


def _no_pause_operation() -> None:
    pass


def pause_run(run: Path, *, pause_workers: Callable[[], None] = _no_pause_operation,
              deadline: float = worker_lifecycle.NO_LOCK_DEADLINE) -> None:
    """Hold the ledger before a validated bulk pause, then save its ownership."""
    from scripts.agents import codex_mesh
    with worker_lifecycle.operation_deadline(deadline=deadline, timeout=codex_mesh.PAUSE_INTERRUPT_SECS):
        store = ledger_for_run(run)
        if not store.path.exists():
            pause_workers()
            return
        with store.locked(deadline=deadline) as ledger:
            _recover(store, ledger)
            pause_workers()
            for worker in ledger.workers.values():
                owner = worker.ownership
                if ((isinstance(owner, HeldBy) and owner.run == str(run.resolve()))
                        or isinstance(owner, Adopted) and owner.to_run == str(run.resolve())):
                    worker.ownership = Paused()


def live_detached_runs(run: Path) -> tuple[tuple[Path, dict[str, object]], ...]:
    """Recorded supervisors only; no process discovery by name."""
    from scripts.agents import mesh_runs
    records: list[tuple[Path, dict[str, object]]] = []
    for path in sorted((run / "mesh_runs").glob("*.json")):
        record = worker_lifecycle.read_json_object(path)
        pid = record.get("pid")
        if record.get("status") == "running" and isinstance(pid, int) and mesh_runs.supervises(pid, path):
            records.append((path, record))
    return tuple(records)


def protected_working_directories(run: Path) -> tuple[Path, ...]:
    return tuple(Path(_required(record, "cwd")).resolve()
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
    root = ledger_path("adhoc", "unused").parent.parent
    held: set[str] = set()
    for path in root.glob("*/*.json"):
        if path.name.endswith(".move.json"):
            continue
        with UnitWorkerLedger(path).locked() as ledger:
            held.update(worker.backend.session_id for worker in ledger.workers.values()
                        if isinstance(worker.backend, ClaudeSession) and not isinstance(worker.ownership, Retired))
    return held


def end_unit(production: str, unit: str) -> None:
    from scripts.agents import codex_mesh, mesh_runs
    # Run cleanup reads this ledger too; load only its binary lookup interface
    # here so neither module owns a static import cycle.
    with worker_lifecycle.operation_deadline(timeout=codex_mesh.THREAD_RELEASE_TIMEOUT_SECS):
        remove_workers = cast(ClaudeCommandLookup, cast(object, import_module("scripts.direct.remove_workers")))
        store = UnitWorkerLedger(ledger_path(production, unit))
        if not store.path.exists():
            return
        # Quarantine is a one-time diagnostic during ordinary work; retirement also
        # removes already quarantined state and must not be held up by that notice.
        try:
            with store.locked():
                pass
        except WorkerUnavailable:
            pass
        with store.locked() as ledger:
            _recover(store, ledger)
            histories = _runs(ledger)
            for name, worker in ledger.workers.items():
                for run in histories:
                    if not run.is_dir():
                        continue
                    with worker_lifecycle.membership_operation(run):
                        with worker_lifecycle.lock_worker(run, name):
                            with worker_lifecycle.locked_state(run / f"{name}.pending.json") as inbox:
                                pending = inbox.read_object()
                                raw_messages = pending.get("messages")
                                queued = [item for item in cast(list[object], raw_messages) if isinstance(item, str)] if isinstance(raw_messages, list) else []
                                for message in queued:
                                    print(f"cancelled pending message of {name}: {message}")
                                _write_inbox(inbox, {})
                            current = _object(_read_roster(run).get(name))
                            if (isinstance(worker.backend, CodexThread) and current
                                    and not current.get("adopted_to") and current.get("status") != "ended"):
                                result = codex_mesh.end_worker_locked(argparse.Namespace(session_dir=str(run), to=name))
                                if result:
                                    raise WorkerUnavailable(f"cannot end worker {name} in {run}; retry retirement")
                            # A giving roster is terminal after adoption, but its detached
                            # supervisors remain there until they finish or the unit retires.
                            for path, detached in live_detached_runs(run):
                                if detached.get("worker") != name:
                                    continue
                                pid = detached["pid"]
                                if isinstance(pid, int) and mesh_runs.supervises(pid, path):
                                    try:
                                        os.kill(pid, signal.SIGTERM)
                                    except ProcessLookupError:
                                        pass
                                    print(f"cancelled detached run {path.stem} of {name} (pid {pid})")
                            with _roster_locked(run) as (handle, roster):
                                record = _object(roster.get(name))
                                if not record or record.get("adopted_to"):
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
                worker.ownership = Retired()
                print(f"ended worker {name}")
            for name, record in ledger.quarantined_workers.items():
                print(f"removed quarantined worker {name}: {record.get('reason', '')}; {store.path}")
            for name, record in ledger.quarantined_adoptions.items():
                print(f"removed quarantined adoption {name}: {record.get('reason', '')}; {store.path}")
            ledger.workers.clear()
            ledger.quarantined_workers.clear()
            ledger.quarantined_adoptions.clear()
        store.path.unlink(missing_ok=True)


@dataclass(frozen=True)
class LedgerMigration:
    identity: str
    source: Path
    destination: Path
    runs: tuple[Path, ...]
    payload: dict[str, object]

    def encode(self) -> dict[str, object]:
        return {"id": self.identity, "source": str(self.source), "destination": str(self.destination),
                "runs": [str(run) for run in self.runs], "payload": self.payload}


def _migration(record: dict[str, object]) -> LedgerMigration:
    runs = record.get("runs")
    if not isinstance(runs, list) or not isinstance(record.get("payload"), dict):
        raise WorkerUnavailable("invalid unit worker migration journal")
    source, destination = Path(_required(record, "source")), Path(_required(record, "destination"))
    if not source.is_absolute() or not destination.is_absolute() or source == destination:
        raise WorkerUnavailable("invalid unit worker migration paths")
    return LedgerMigration(_required(record, "id"), source, destination,
                           tuple(Path(run) for run in cast(list[object], runs) if isinstance(run, str)),
                           _object(record["payload"]))


@contextmanager
def _migration_locks(source: Path, destination: Path) -> Generator[None, None, None]:
    with ExitStack() as locks:
        for path in sorted((source, destination), key=str):
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = locks.enter_context(path.with_suffix(".lock").open("a+", encoding="utf-8"))
            worker_lifecycle.acquire_flock(handle, mode=fcntl.LOCK_EX)
        yield


def _apply_move(move: LedgerMigration) -> None:
    """Both ledger locks are held before any roster or progress lock."""
    from scripts.agents import codex_mesh
    from scripts.direct import progress_history
    target = _read(move.destination)
    if target and target.get("migration_id") != move.identity:
        raise WorkerUnavailable(f"destination worker ledger has other owners: {move.destination}")
    payload = dict(move.payload)
    payload["migration_id"] = move.identity
    _atomic(move.destination, payload)
    for run in move.runs:
        if not run.is_dir():
            continue
        if (run / ROSTER).exists():
            with worker_lifecycle.membership_operation(run), worker_lifecycle.lock_workers(*(worker_lifecycle.WorkerIdentity(run, name)
                    for name in _read_roster(run))), _roster_locked(run) as (handle, roster):
                changed = False
                for name, raw in roster.items():
                    record = _object(raw)
                    if (record.get("unit_ledger") == str(move.source)
                            or name in _object(move.payload.get("workers"))):
                        if record.get("unit_ledger") != str(move.destination):
                            record["unit_ledger"] = str(move.destination)
                            codex_mesh.advance_worker_roster_revision(record)
                            changed = True
                if changed:
                    _write_roster(handle, roster)
        if (run / STATE).exists():
            if not worker_lifecycle.read_json_object(run / STATE).get("owner_staff_id"):
                progress_history.set_unit(run, move.destination.parent.name, move.destination.stem)
    move.source.unlink(missing_ok=True)
    move.source.with_suffix(".move.json").unlink(missing_ok=True)
    move.destination.with_suffix(".move.json").unlink(missing_ok=True)


def _finish_move(record: dict[str, object]) -> None:
    move = _migration(record)
    with _migration_locks(move.source, move.destination):
        # A second reader may have completed the exact same move already.
        if not any(path.with_suffix(".move.json").exists() for path in (move.source, move.destination)):
            return
        _apply_move(move)


def _move_ledger(source: Path, destination: Path, runs: tuple[Path, ...]) -> None:
    if source == destination:
        return
    for path in (source, destination):
        journal = path.with_suffix(".move.json")
        if journal.exists():
            _finish_move(_read(journal))
    if not source.exists():
        return
    # Complete adoptions before taking the two migration locks. Recheck under
    # both locks below, so a new adoption cannot slip into the saved payload.
    with UnitWorkerLedger(source).locked() as ledger:
        _recover(UnitWorkerLedger(source), ledger)
        histories = _runs(ledger)
    with _migration_locks(source, destination):
        payload = _read(source)
        if not payload:
            return
        if _object(payload.get("adoptions")):
            raise WorkerUnavailable("an adoption started before migration; retry move")
        if _read(destination):
            raise WorkerUnavailable(f"destination worker ledger has other owners: {destination}")
        move = LedgerMigration(str(uuid.uuid4()), source, destination,
                               tuple(sorted(set((*runs, *histories)))), payload)
        _atomic(source.with_suffix(".move.json"), move.encode())
        _atomic(destination.with_suffix(".move.json"), move.encode())
        _apply_move(move)


def move_unit(from_production: str, to_production: str, unit: str, *, runs: tuple[Path, ...] = ()) -> None:
    """Migrate a unit between productions without replacing any worker."""
    _move_ledger(ledger_path(from_production, unit), ledger_path(to_production, unit), runs)


def rename_unit(production: str, old: str, new: str, *, runs: tuple[Path, ...] = ()) -> None:
    """Change shown unit context; worker addresses and backend identities stay."""
    _move_ledger(ledger_path(production, old), ledger_path(production, new), runs)


def shown_name(run: Path, address: str, *, role: str = "", held_design_id: str = "", held_role: str = "") -> str:
    """Render current unit context and stored assignment without changing the address."""
    # Notices can render while holding an inbox lock. Recover ownership at
    # command entry, never while rendering: ledger_for_run can finish a move
    # and take ledger/roster locks. Ledger saves are atomic snapshots.
    store = _ledger_for_run(run)
    roster = _read_roster(run)
    record = _object(roster.get(address))
    role = _text(record.get("role")) or role
    if not role:
        saved = _object(_object(_read(store.path).get("workers")).get(address))
        role = _text(saved.get("role"))
    role = role or "worker"
    if held_design_id:
        record = {**record, "held_design_id": held_design_id, "held_role": held_role}
    titles = cast(TitleRenderer, cast(object, import_module("scripts.message.roster")))
    # A held design or a run outside a production takes its placement from
    # the roster; a unit run takes it from its ledger. Both render one way.
    if store.path.parent.name == "adhoc" or record.get("held_design_id"):
        return titles.mesh_title(str(run), address, {**record, "role": role})
    jobs: dict[str, JobKey] = {"impl": "implementer", "test": "tester", "fix": "repair_worker",
                               "review": "code_reviewer"}
    peers = [name for name, value in roster.items() if _object(value).get("role") == role]
    ordinal = peers.index(address) + 1 if address in peers else len(peers) + 1
    return titles.unit_worker_title(store.path.parent.name, store.path.stem, jobs.get(role, "worker"), ordinal, address)


def _ledger_runs(path: Path) -> tuple[Path, ...]:
    return _runs(UnitWorkers(workers={name: _decode_worker(name, _object(record))
                                    for name, record in _object(_read(path).get("workers")).items()}))


def _moved_path(value: str, old: Path, new: Path) -> str:
    if value == str(old) or value.startswith(f"{old}/"):
        return str(new) + value[len(str(old)):]
    return value


@dataclass(frozen=True)
class UnitWorktree:
    production: str
    unit: str
    path: Path
    branch: str


@dataclass(frozen=True)
class MovedUnitRuns:
    current: UnitWorktree
    old: Path
    runs: tuple[Path, ...]


@dataclass(frozen=True)
class WorkerRepairSearch:
    moves: tuple[MovedUnitRuns, ...]
    skipped: tuple[str, ...]


def _former_worktrees(current: UnitWorktree, former: tuple[str, ...]) -> tuple[Path, ...]:
    return tuple(current.path.with_name(current.path.name.replace(
        f"-{current.production}-", f"-{name}-", 1)) for name in former if name != current.production)


def _run_unit(state: dict[str, object]) -> tuple[str, str]:
    from scripts.direct.progress_history import ProductionUnitIdentity, resolve_production_unit
    # A candidate folder is not evidence of the run's ownership.
    identity = resolve_production_unit(state)
    return (identity.production, identity.unit) if isinstance(identity, ProductionUnitIdentity) else ("", "")


def find_moved_unit_runs(units: tuple[UnitWorktree, ...], former: tuple[str, ...]) -> WorkerRepairSearch:
    """Match missing run or worker paths to current unit staff rows without changing state."""
    from scripts.lib import unit_runs
    grouped: dict[tuple[UnitWorktree, Path], set[Path]] = {}
    skipped: list[str] = []
    paths = {path.parent for root in unit_runs.run_roots(
        os.environ.get("UNIT_RUN_ROOT") or os.environ.get("RETIRE_UNIT_RUN_ROOT"))
        for name in (STATE, ROSTER) for path in root.glob(f"*/{name}")}
    for run in sorted(paths):
        state = worker_lifecycle.read_json_object(run / STATE)
        working = _text(state.get("working_dir"))
        roster = _read(run / ROSTER)
        worker_paths = [_text(_object(value).get("cwd")) for value in roster.values()]
        matches: set[tuple[UnitWorktree, Path]] = set()
        for current in units:
            if not current.path.is_dir():
                continue
            known_old = _former_worktrees(current, former)
            old_paths = {path for path in known_old if path != current.path and not path.exists()
                         and any(value == str(path) or value.startswith(f"{path}/")
                                 for value in (working, *worker_paths))}
            # Explicit identity can also name a move outside the folder convention.
            if working and Path(working).is_absolute() and not Path(working).exists():
                old_paths.add(Path(working))
            for old in old_paths:
                production, unit = _run_unit(state)
                if production == "adhoc":
                    production, unit = "", ""
                if production and unit:
                    try:
                        accepted = (unit_names.name(unit) == unit_names.name(current.unit)
                                    and production in (*former, current.production))
                    except unit_names.BadName:
                        accepted = False
                else:
                    accepted = old in known_old
                if accepted:
                    matches.add((current, old))
        owners = {match[0] for match in matches}
        if len(owners) > 1:
            skipped.append(f"run {run}: ambiguous unit ({', '.join(sorted(item.unit for item in owners))}); skipped")
            continue
        for current, old in matches:
            grouped.setdefault((current, old), set()).add(run)
    return WorkerRepairSearch(tuple(MovedUnitRuns(current, old, tuple(sorted(runs)))
        for (current, old), runs in sorted(grouped.items(), key=lambda item: (item[0][0].unit, str(item[0][1])))),
        tuple(skipped))


def repoint_moved_unit_runs(move: MovedUnitRuns, *, from_production: str, dry_run: bool = False) -> tuple[str, ...]:
    current = move.current
    return _repoint_worktree_runs(current.production, current.unit, move.old, current.path, current.branch,
                                 set(move.runs), from_production=from_production, dry_run=dry_run)


def repoint_unit_worktree(production: str, unit: str, old: Path, new: Path, branch: str,
                          *, from_production: str, dry_run: bool = False) -> tuple[str, ...]:
    """Repair unit identity and paths after a stopped unit's worktree moved.

    The production rename journal retains both paths until this completes.
    Each write repeats safely if the caller stops before marking it complete.
    """
    from scripts.lib import unit_runs
    old, new = old.expanduser().resolve(), new.expanduser().resolve()
    runs: set[Path] = set()
    for directory in unit_runs.run_roots(os.environ.get("UNIT_RUN_ROOT") or os.environ.get("RETIRE_UNIT_RUN_ROOT")):
        for path in directory.glob(f"*/{STATE}"):
            state = worker_lifecycle.read_json_object(path)
            if (state.get("working_dir") in (str(old), str(new))
                    or (state.get("production") in (from_production, production) and state.get("unit") == unit)):
                runs.add(path.parent)
    return _repoint_worktree_runs(production, unit, old, new, branch, runs,
                                 from_production=from_production, dry_run=dry_run)


def _repoint_worktree_runs(production: str, unit: str, old: Path, new: Path, branch: str,
                           runs: set[Path], *, from_production: str, dry_run: bool) -> tuple[str, ...]:
    from scripts.agents import codex_mesh
    old, new = old.expanduser().resolve(), new.expanduser().resolve()
    target = ledger_path(production, unit)
    former = ledger_path(from_production, unit)
    root = target.parent.parent
    sources: set[Path] = ({former} if former != target and (former.exists() or former.with_suffix(".move.json").exists())
                         else set())
    for path in (target, former):
        runs.update(_ledger_runs(path))
    # Older callers may have lost their plan before the identity was saved.
    # Their ledger history still names the run, even if its roster is gone.
    for path in (root / "adhoc").glob("*.json"):
        if path.name.endswith(".move.json"):
            continue
        histories = _ledger_runs(path)
        if runs.intersection(histories):
            sources.add(path)
            runs.update(histories)
    changes: list[str] = []
    for source in sorted(sources):
        changes.append(f"worker ledger {source} -> {target}")
        if not dry_run:
            _move_ledger(source, target, tuple(sorted(runs)))
    def repoint_roster(run: Path, roster: dict[str, object]) -> bool:
        changed = False
        for name, raw in roster.items():
            record = _object(raw)
            cwd = _text(record.get("cwd"))
            moved = _moved_path(cwd, old, new)
            if moved != cwd or record.get("unit_ledger") != str(target):
                changes.append(f"worker {run}/{name}: {moved or 'cwd unchanged'}; ledger {target}")
                if not dry_run:
                    changed = True
                    if moved != cwd:
                        record["cwd"] = moved
                    record["unit_ledger"] = str(target)
                    codex_mesh.advance_worker_roster_revision(record)
        return changed

    # Apply order matches adoption: ledger, lifecycle, roster, progress state.
    # Preview reads create neither ledger state nor lifecycle lock files.
    def repoint_runs() -> None:
        for run in sorted(runs):
            if not run.is_dir():
                continue
            if (run / ROSTER).exists():
                if dry_run:
                    # Atomic snapshots suffice for a preview; do not create lock
                    # files. Apply rereads under membership/lifecycle/roster locks.
                    _ = repoint_roster(run, _read(run / ROSTER))
                else:
                    with worker_lifecycle.membership_operation(run), worker_lifecycle.lock_workers(*(worker_lifecycle.WorkerIdentity(run, name)
                            for name in _read_roster(run))), _roster_locked(run) as (handle, roster):
                        if repoint_roster(run, roster):
                            _write_roster(handle, roster)
            if not (run / STATE).exists():
                continue
            with ExitStack() as locks:
                if not dry_run:
                    lock = locks.enter_context((run / ".progress_history.lock").open("a+", encoding="utf-8"))
                    worker_lifecycle.acquire_flock(lock, mode=fcntl.LOCK_EX)
                state = worker_lifecycle.read_json_object(run / STATE)
                updated = dict(state)
                if not state.get("owner_staff_id"):
                    updated.update({"production": production, "unit": unit})
                if state.get("working_dir") in (str(old), str(new)):
                    updated.update({"working_dir": str(new), "worktree": new.name, "branch": branch})
                for key in ("plan_doc", "project_plan_doc"):
                    if isinstance(state.get(key), str):
                        updated[key] = _moved_path(_text(state[key]), old, new)
                if state != updated:
                    changes.append(f"run {run}: unit {production}/{unit}; worktree {updated.get('working_dir', '')}")
                    if not dry_run:
                        _atomic(run / STATE, updated)
                marker = run / "working_dir"
                if marker.exists() and marker.read_text().strip() == str(old):
                    changes.append(f"run {run}: working_dir -> {new}")
                    if not dry_run:
                        write_text_atomic(marker, str(new) + "\n")
    if dry_run:
        repoint_runs()
    elif runs or target.exists():
        store = UnitWorkerLedger(target)
        with store.locked() as ledger:
            _recover(store, ledger)
            repoint_runs()
    return tuple(changes)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("resolve")
    _ = command.add_argument("--session-dir", type=Path, required=True)
    _ = command.add_argument("--role", choices=ROLES, required=True)
    _ = command.add_argument("--working-dir", default="", help="reviewer naming fallback when progress state has no working_dir")
    _ = command.add_argument("--owner-pid", type=int, default=0)
    _ = command.add_argument("--model", default="")
    _ = command.add_argument("--effort", default="")
    _ = command.add_argument("--new-worker", dest="name", default="")
    repair = commands.add_parser("repair-roster", help="rebuild missing roster identities from ownership and live evidence")
    _ = repair.add_argument("--session-dir", type=Path, required=True)
    display = commands.add_parser("shown-name")
    _ = display.add_argument("--session-dir", type=Path, required=True)
    _ = display.add_argument("--name", required=True)
    args = parser.parse_args(namespace=WorkerArguments())
    run = args.session_dir
    role = args.role
    try:
        if args.command == "repair-roster":
            print(repair_roster(run))
            return 0
        if args.command == "shown-name":
            print(shown_name(run, args.name))
        else:
            print(_resolve(run, role, args.owner_pid or director_pid(run),
                           model=args.model, effort=args.effort, new_name=args.name,
                           working_dir=Path(args.working_dir) if args.working_dir else NO_LAUNCHER_WORKING_DIRECTORY))
        return 0
    except (WorkerUnavailable, OSError, ValueError) as error:
        print(f"workers: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    # Mesh imports this module by its package name; use that same exception
    # and domain type identity when this file is invoked as a CLI.
    from scripts.direct.workers import main as workers_main
    raise SystemExit(workers_main())
