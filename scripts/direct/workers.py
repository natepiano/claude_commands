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
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Generator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from enum import Enum
from importlib import import_module
from pathlib import Path
from typing import Literal, Protocol, TextIO, cast

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

ROSTER = "mesh_roster.json"
STATE = "progress_history_state.json"
ROLES = ("impl", "test", "fix", "review")
ALLOCATION_CLAIM_WINDOW_SECS = 120.0
ALLOCATION_FIELDS = ("allocation_pending", "allocation_id", "allocation_owner_pid", "allocation_expires_at")


class WorkerUnavailable(Exception):
    """No safe ownership transition can be made from the current evidence."""


class ClaudeCommandLookup(Protocol):
    def claude_bin(self) -> str: ...


class WorkerArguments(argparse.Namespace):
    session_dir: Path = Path()
    role: str = ""
    owner_pid: int = 0


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

    handle: TextIO
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
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {}
    if not isinstance(raw, dict):
        raise WorkerUnavailable(f"invalid worker state: {path}")
    return cast(dict[str, object], raw)


def _read_roster(run: Path) -> dict[str, object]:
    try:
        with (run / ROSTER).open(encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_SH)
            content = handle.read()
            if not content.strip():
                return {}
            return _object(cast(object, json.loads(content)))
    except FileNotFoundError:
        return {}


def _atomic(path: Path, data: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=f".{path.name}.", delete=False) as output:
        temporary = Path(output.name)
        try:
            json.dump(data, output, indent=2, sort_keys=True)
            _ = output.write("\n")
            output.flush()
            os.fsync(output.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)


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
    def locked(self) -> Generator[UnitWorkers, None, None]:
        journal = self.path.with_suffix(".move.json")
        if journal.exists():
            move = _read(journal)
            _finish_move(move)
            if not self.path.exists():
                raise WorkerUnavailable(f"worker ledger moved to {move.get('destination')}; retry using current unit identity")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
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
    state = _read(run / STATE)
    production, unit = _text(state.get("production")), _text(state.get("unit"))
    worktree = Path(_text(state.get("working_dir")) or str(run)).expanduser().resolve()
    if not production or not unit:
        plan = _text(state.get("project_plan_doc")) or _text(state.get("plan_doc"))
        try:
            content = (worktree / plan).read_text(encoding="utf-8") if plan else ""
        except OSError:
            content = ""
        match = re.search(r'^> \*\*Production: (.+?)\*\* — unit `([^`]+)`;', content, re.MULTILINE)
        if match:
            production, unit = match.groups()
    if not production or not unit:
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
    return store


@contextmanager
def _roster_locked(run: Path) -> Generator[tuple[TextIO, dict[str, object]], None, None]:
    run.mkdir(parents=True, exist_ok=True)
    with (run / ROSTER).open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        _ = handle.seek(0)
        content = handle.read()
        roster = _object(cast(object, json.loads(content))) if content.strip() else {}
        yield handle, roster


def _write_roster(handle: TextIO, roster: dict[str, object]) -> None:
    _ = handle.seek(0)
    _ = handle.truncate()
    json.dump(roster, handle, indent=2, sort_keys=True)
    handle.flush()
    os.fsync(handle.fileno())


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
    with _roster_locked(run) as (handle, roster):
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
    from scripts.guilds.roles import PhaseBoundDesigner
    access = codex_mesh.roster_access_policy(record)
    policy: dict[str, object] = {"kind": type(access).__name__}
    if isinstance(access, PhaseBoundDesigner):
        policy.update({"binding_generation": access.binding_generation, "launch_sandbox": access.launch_sandbox})
    return UnitWorker(name, _text(record.get("role")), "codex", _text(record.get("model")),
                      _text(record.get("effort")), policy,
                      CodexThread(_text(record.get("thread_id")), _text(record.get("codex_home")) or
                                  str(codex_mesh.server_record_home(_read(run / "mesh_server.json")))),
                      HeldBy(str(run)), [_event("opened", "registered existing roster", run)])


def _quarantine(store: UnitWorkerLedger, ledger: UnitWorkers, intent: AdoptionIntent, reason: str) -> None:
    ledger.quarantined_adoptions[intent.identity] = {"reason": reason, "intent": _encode_intent(intent)}
    _ = ledger.adoptions.pop(intent.identity)
    store.save(ledger)
    raise WorkerUnavailable(f"adoption {intent.identity}: {reason}; journal quarantined in {store.path}; retry command")


def _giving_inbox(locks: ExitStack, source: Path, name: str) -> tuple[TextIO, dict[str, object]]:
    inbox = locks.enter_context((source / f"{name}.pending.json").open("a+", encoding="utf-8"))
    try:
        fcntl.flock(inbox, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        raise WorkerUnavailable(f"{name}: pending inbox is in use; retry adoption recovery") from error
    _ = inbox.seek(0)
    content = inbox.read()
    return inbox, _object(cast(object, json.loads(content))) if content.strip() else {}


def _transfer_pending(locks: ExitStack, inbox: TextIO, destination: Path, name: str,
                      after: dict[str, object], identity: str, messages: dict[str, object]) -> None:
    target_inbox = locks.enter_context((destination / f"{name}.pending.json").open("a+", encoding="utf-8"))
    try:
        fcntl.flock(target_inbox, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        raise WorkerUnavailable(f"{name}: destination inbox in use; retry recovery") from error
    _ = target_inbox.seek(0)
    target_content = target_inbox.read()
    saved = _object(cast(object, json.loads(target_content))) if target_content.strip() else {}
    if saved.get("adoption_id") != identity:
        def texts(value: object) -> list[str]:
            return [item for item in cast(list[object], value) if isinstance(item, str)] if isinstance(value, list) else []
        queue = texts(messages.get("messages")) + texts(saved.get("messages"))
        _write_inbox(target_inbox, {"thread_id": after["thread_id"], "messages": queue,
                                    "accepting_messages": True, "adoption_id": identity})
    _write_inbox(inbox, {"thread_id": after["thread_id"], "messages": [], "accepting_messages": False})


def _complete_adoption(store: UnitWorkerLedger, ledger: UnitWorkers, transaction: AdoptionIntent) -> None:
    """Roll forward an intent under ledger -> giving -> taking -> inbox locks."""
    from scripts.agents import codex_mesh
    source, destination = transaction.giving.path, transaction.destination
    name, identity = transaction.name, transaction.identity
    after = transaction.after
    with ExitStack() as locks:
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
        messages: dict[str, object] = {}
        if (isinstance(transaction.giving, ExistingRun)
                and transaction.queue_policy is AdoptionQueuePolicy.LEGACY_REFUSE_QUEUED):
            _, messages = _giving_inbox(locks, source, name)
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
        if (isinstance(transaction.giving, ExistingRun)
                and transaction.queue_policy is AdoptionQueuePolicy.TRANSFER):
            # Publish the taking roster before routing source senders to it.
            # After the source fence, no sender can append behind this copy.
            inbox, messages = _giving_inbox(locks, source, name)
            _transfer_pending(locks, inbox, destination, name, after, identity, messages)
        worker = ledger.workers[name]
        worker.ownership = Adopted(str(destination))
        if not any(event.details.get("adoption_id") == identity for event in worker.events):
            worker.events.append(_event("adopted", "new run", destination, adoption_id=identity,
                                        from_run=str(source)))
        store.save(ledger)
        _ = ledger.adoptions.pop(identity)
        store.save(ledger)


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
           *, owner_pid: int, reserve: bool = False) -> str:
    from scripts.agents import codex_mesh
    if not name or Path(name).name != name:
        raise WorkerUnavailable("invalid worker name")
    if source == destination:
        raise WorkerUnavailable("source and destination runs must differ")
    source_present = source.is_dir()
    if source_present and _ledger_for_run(source).path != store.path:
        raise WorkerUnavailable("worker belongs to another unit")
    history = codex_mesh.worker_run_history(str(source), name) if source_present else ()
    before: dict[str, object]
    with ExitStack() as locks:
        if source_present:
            giving_roster = locks.enter_context(_roster_locked(source))
            before = _object(giving_roster[1].get(name))
            giving: ExistingRun | RemovedRun = ExistingRun(source, before)
        else:
            worker = ledger.workers.get(name)
            if worker is None or not isinstance(worker.backend, CodexThread):
                raise WorkerUnavailable(f"{name}: removed run has no saved Codex backend")
            before = {"thread_id": worker.backend.thread_id, "codex_home": worker.backend.codex_home,
                      "role": worker.role, "family": worker.family, "model": worker.model,
                      "effort": worker.effort, "access_policy": worker.access_policy, "status": "paused"}
            giving = RemovedRun(source)
        taking_roster = locks.enter_context(_roster_locked(destination))
        new = taking_roster[1]
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
        return _adopt(store, ledger, source, destination, name, owner_pid=os.getpid())


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
            with _roster_locked(run) as (handle, roster):
                record = dict(_object(roster.get(name)))
                if record.get("adopted_to") or record.get("thread_id") != thread_id:
                    return
                backend = worker.backend
                if isinstance(backend, CodexThread) and backend.thread_id != thread_id:
                    worker.events.append(_event("replaced", "resume refused old thread", run,
                                                old_thread_id=backend.thread_id, thread_id=thread_id))
                worker.backend = CodexThread(thread_id, str(home.resolve()))
                worker.ownership = HeldBy(str(run))
                # The first exact-thread resume completes the incoming handoff.
                # Future follows use ordinary live-turn checks and replacement.
                _ = record.pop("adopted_from", None)
                record.update({"codex_home": str(home.resolve()), "unit_ledger": str(store.path),
                               "role": worker.role, "model": worker.model, "effort": worker.effort,
                               "access_policy": worker.access_policy})
                from scripts.agents import codex_mesh
                codex_mesh.advance_worker_roster_revision(record)
                roster[name] = record
                _write_roster(handle, roster)


def _reserve(run: Path, name: str, store: UnitWorkerLedger, owner_pid: int, *, paused: bool = False) -> None:
    from scripts.agents import codex_mesh
    with _roster_locked(run) as (handle, roster):
        record = dict(_object(roster.get(name)))
        if not record or record.get("adopted_to"):
            raise WorkerUnavailable(f"{name}: no longer owned by this run")
        if codex_mesh.worker_launcher_attached(record):
            raise WorkerUnavailable(f"{name}: launcher attached during resolution")
        if allocation_is_live(record) or (record.get("continuation_owed") and not paused):
            raise WorkerUnavailable(f"{name}: work already reserved or owed")
        with (run / f"{name}.pending.json").open("a+", encoding="utf-8") as inbox:
            try:
                fcntl.flock(inbox, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise WorkerUnavailable(f"{name}: message delivery in progress") from error
            _ = inbox.seek(0)
            content = inbox.read()
            messages = _object(cast(object, json.loads(content))) if content.strip() else {}
            if messages.get("messages") and not paused:
                raise WorkerUnavailable(f"{name}: pending messages are queued")
            _reserve_allocation(record, owner_pid)
            record["unit_ledger"] = str(store.path)
            codex_mesh.advance_worker_roster_revision(record)
            roster[name] = record
            _write_roster(handle, roster)


def resolve(run: Path, role: str) -> str:
    """Return an idle worker, carry one over, reopen one, or open the next name."""
    return _resolve(run, role, os.getpid())


def _resolve(run: Path, role: str, owner_pid: int) -> str:
    from scripts.agents import codex_mesh
    if role not in ROLES:
        raise WorkerUnavailable(f"unsupported worker role: {role}")
    run = run.expanduser().resolve()
    store = ledger_for_run(run)
    with store.locked() as ledger:
        _recover(store, ledger)
        if codex_mesh.finished_run_status(str(run)):
            raise WorkerUnavailable("run is finished; use progress_history.py start-phase to reactivate it, " +
                                    "or workers.py resolve --session-dir <new-run> to carry the worker forward")
        roster = _read_roster(run)
        for name, value in roster.items():
            record = _object(value)
            if record.get("role") != role or record.get("adopted_to"):
                continue
            registered = ledger.workers.get(name)
            if registered is not None and isinstance(registered.ownership, Retired):
                continue
            if record.get("status") in ("done", "failed", "paused") and not codex_mesh.worker_idle_reason(
                    str(run), name, record, prior_runs=codex_mesh.worker_run_history(str(run), name)[1:],
                    paused=registered is not None and isinstance(registered.ownership, Paused)):
                if name not in ledger.workers:
                    ledger.workers[name] = _worker_from_record(name, run, record)
                try:
                    _reserve(run, name, store, owner_pid,
                             paused=registered is not None and isinstance(registered.ownership, Paused))
                except WorkerUnavailable:
                    continue
                ledger.workers[name].ownership = HeldBy(str(run))
                return name
        for name, worker in list(ledger.workers.items()):
            if worker.role != role or isinstance(worker.ownership, Retired):
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
                    return _adopt(store, ledger, Path(previous), run, name, reserve=True, owner_pid=owner_pid)
                except WorkerUnavailable:
                    continue
        for name, value in roster.items():
            record = _object(value)
            if record.get("role") != role or record.get("status") not in ("ended", "running", "starting"):
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
        ordinal = 1
        name = role
        while name in ledger.workers or name in roster:
            ordinal += 1
            name = f"{role}{ordinal}"
        record = codex_mesh.open_unit_worker(str(run), name, role)
        ledger.workers[name] = _worker_from_record(name, run, record)
        ledger.workers[name].events = [_event("opened", "no idle worker for role", run)]
        _reserve(run, name, store, owner_pid)
        return name


@dataclass(frozen=True)
class LiveUnitDirector:
    session_id: str
    pid: int


@dataclass(frozen=True)
class NoLiveUnitDirector:
    """No current session matches the run's director or its recorded restart identity."""


def live_unit_director(run: Path) -> LiveUnitDirector | NoLiveUnitDirector:
    """Resolve the current director once, including restarts on another account.

    Shutdown history carries the unit's stable host/name when a run's director
    file still names the former session. Run markers and folders are not proof
    that the director died.
    """
    from scripts.lib import unit_runs
    from scripts.message import sessions
    from scripts.shutdown.launch_permission import shutdown_state_root
    director = unit_runs.director(run)
    live = sessions.live_sessions()
    for session in live:
        if director and session["sessionId"] == director:
            return LiveUnitDirector(session["sessionId"], session["pid"])
    context = _read(run / STATE)
    production, unit = context.get("production"), context.get("unit")
    names: set[str] = set()
    for path in shutdown_state_root().glob("*/history/*.json"):
        try:
            saved = _read(path)
        except (OSError, ValueError, WorkerUnavailable):
            continue
        previous = _object(saved.get("session"))
        host = _object(previous.get("host"))
        if host.get("kind") not in ("unit", "legacy tmux unit"):
            continue
        runs = previous.get("run_dirs")
        same_run = isinstance(runs, list) and any(
            isinstance(value, str) and Path(value).resolve() == run.resolve()
            for value in cast(list[object], runs))
        same_unit = bool(production and unit and host.get("production") == production and host.get("unit") == unit)
        if not (same_run or same_unit or (director and previous.get("session_id") == director)):
            continue
        for name in (previous.get("name"), host.get("session_name")):
            if isinstance(name, str) and name:
                names.add(name)
        # A restart may resume the original session id rather than create one.
        for session in live:
            if session["sessionId"] == previous.get("session_id"):
                return LiveUnitDirector(session["sessionId"], session["pid"])
    for session in live:
        if names.intersection((session["name"], *session["formerNames"])):
            return LiveUnitDirector(session["sessionId"], session["pid"])
    return NoLiveUnitDirector()


def run_has_live_director(run: Path) -> bool:
    return isinstance(live_unit_director(run), LiveUnitDirector)


def director_pid(run: Path) -> int:
    """The director process owns dispatches across one-call shell invocations."""
    director = live_unit_director(run)
    if isinstance(director, LiveUnitDirector):
        return director.pid
    raise WorkerUnavailable("run director pid is unknown; pass --owner-pid <pid>")


def _write_inbox(handle: TextIO, record: Mapping[str, object]) -> None:
    _ = handle.seek(0)
    _ = handle.truncate()
    json.dump(record, handle)
    handle.flush()
    os.fsync(handle.fileno())


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
    store = UnitWorkerLedger(ledger_path(production, unit))
    if not store.path.exists():
        return
    with store.locked() as ledger:
        _recover(store, ledger)
        for worker in ledger.workers.values():
            if not isinstance(worker.ownership, Retired):
                worker.ownership = Paused()


def pause_run(run: Path) -> None:
    store = ledger_for_run(run)
    if not store.path.exists():
        return
    with store.locked() as ledger:
        _recover(store, ledger)
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
        record = _read(path)
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
                with (run / f"{name}.pending.json").open("a+", encoding="utf-8") as inbox:
                    fcntl.flock(inbox, fcntl.LOCK_EX)
                    _ = inbox.seek(0)
                    content = inbox.read()
                    pending = _object(cast(object, json.loads(content))) if content.strip() else {}
                    raw_messages = pending.get("messages")
                    queued = [item for item in cast(list[object], raw_messages) if isinstance(item, str)] if isinstance(raw_messages, list) else []
                    for message in queued:
                        print(f"cancelled pending message of {name}: {message}")
                    _write_inbox(inbox, {})
                current = _object(_read_roster(run).get(name))
                if (isinstance(worker.backend, CodexThread) and current
                        and not current.get("adopted_to") and current.get("status") != "ended"):
                    result = codex_mesh.command_end(argparse.Namespace(session_dir=str(run), to=name))
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
            fcntl.flock(handle, fcntl.LOCK_EX)
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
            with _roster_locked(run) as (handle, roster):
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("resolve")
    _ = command.add_argument("--session-dir", type=Path, required=True)
    _ = command.add_argument("--role", choices=ROLES, required=True)
    _ = command.add_argument("--owner-pid", type=int, default=0)
    args = parser.parse_args(namespace=WorkerArguments())
    run = args.session_dir
    role = args.role
    try:
        print(_resolve(run, role, args.owner_pid or director_pid(run)))
        return 0
    except (WorkerUnavailable, OSError, ValueError) as error:
        print(f"workers: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    # Mesh imports this module by its package name; use that same exception
    # and domain type identity when this file is invoked as a CLI.
    from scripts.direct.workers import main as workers_main
    raise SystemExit(workers_main())
