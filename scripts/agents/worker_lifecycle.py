"""Own run membership, worker operations and server maintenance flocks.

Order: ledger, design, membership, future staff, lifecycle, server, inbox, roster.

An operation owns this lock through its server effects and final publication,
but releases it before watching a turn. Nested acquisition is a programming
error; callers must pass ownership down instead of reacquiring it.
"""
from __future__ import annotations

import fcntl
import contextlib
import os
import inspect
import json
import threading
import time
import select
from collections.abc import Generator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeGuard, final, cast

from scripts.lib.atomic_file import write_text_atomic


@dataclass(frozen=True, order=True)
class WorkerIdentity:
    session: Path
    name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "session", self.session.resolve())
        if not self.name or Path(self.name).name != self.name or self.name in (".", ".."):
            raise ValueError(f"invalid worker name: {self.name!r}")


NO_LOCK_DEADLINE = float("inf")


def is_process_pid(pid: object) -> TypeGuard[int]:
    """Only an actual process ID above init is eligible for signalling."""
    return type(pid) is int and pid > 1


@dataclass(frozen=True)
class LockIdentity:
    session: Path
    name: str
    kind: str


@dataclass(frozen=True)
class MembershipLease:
    """Runs whose admission boundary this operation currently owns."""
    sessions: tuple[Path, ...]

    def covers(self, session_dir: str | Path) -> bool:
        return Path(session_dir).resolve() in self.sessions and Path(session_dir).resolve() in held_memberships()


@final
class _OperationState(threading.local):
    def __init__(self) -> None:
        self.deadline: float = NO_LOCK_DEADLINE
        self.sites: dict[tuple[int, LockIdentity], str] = {}


_operation = _OperationState()


class OperationDeadlineExpired(TimeoutError):
    """The scoped operation budget, rather than a transport limit, expired."""


def effective_deadline(deadline: float = NO_LOCK_DEADLINE) -> float:
    """An explicit monotonic limit may shorten, but never extend, this scope."""
    return min(deadline, _operation.deadline)


@contextmanager
def operation_deadline(*, deadline: float = NO_LOCK_DEADLINE,
                       timeout: float = NO_LOCK_DEADLINE) -> Generator[float]:
    """Scope one monotonic budget; nested operations inherit the earlier limit.

    State is local to this thread. A handoff to another thread or process must
    explicitly pass the yielded deadline. Restoration also runs on exceptions.
    """
    previous = _operation.deadline
    _operation.deadline = min(previous, deadline, time.monotonic() + timeout)
    try:
        yield _operation.deadline
    finally:
        _operation.deadline = previous


class LockFileDescriptor(Protocol):
    """An open descriptor eligible for the shared flock primitive."""

    def fileno(self) -> int: ...


def acquire_flock(handle: int | LockFileDescriptor, *, mode: int = fcntl.LOCK_EX,
                  deadline: float = NO_LOCK_DEADLINE) -> None:
    """The sole flock primitive: SH/EX inherit the budget; NB stays immediate.

    Unlocks must always succeed, including after the operation has expired.
    """
    if mode == fcntl.LOCK_UN:
        fcntl.flock(handle, mode)
        return
    deadline = effective_deadline(deadline)
    while True:
        if time.monotonic() >= deadline:
            name = cast(object, getattr(handle, "name", handle))
            raise OperationDeadlineExpired(f"shutdown incomplete: timed out acquiring lock {name}")
        try:
            fcntl.flock(handle, mode if deadline == NO_LOCK_DEADLINE else mode | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if mode & fcntl.LOCK_NB:
                raise
            _ = select.select([], [], [], min(0.05, max(0.0, deadline - time.monotonic())))


@contextmanager
def _locks(identities: Sequence[LockIdentity], deadline: float) -> Generator[None]:
    caller = inspect.currentframe()
    assert caller is not None
    while caller.f_back and caller.f_code.co_filename in (__file__, contextlib.__file__):
        caller = caller.f_back
    site = f"{caller.f_code.co_filename}:{caller.f_lineno} ({caller.f_code.co_name})"
    del caller
    keys = [(os.getpid(), identity) for identity in identities]
    for key in keys:
        if key in _operation.sites:
            raise RuntimeError(f"recursive worker {key[1].kind} lock for {key[1]}: {_operation.sites[key]} -> {site}")
    for key in keys:
        _operation.sites[key] = site
    try:
        with ExitStack() as locks:
            for identity in identities:
                identity.session.mkdir(parents=True, exist_ok=True)
                handle = locks.enter_context((identity.session / identity.name).open("a"))
                acquire_flock(handle, deadline=deadline)
            yield
    finally:
        for key in keys:
            del _operation.sites[key]


@contextmanager
def lock_memberships(*session_dirs: str | Path, deadline: float = NO_LOCK_DEADLINE) -> Generator[MembershipLease]:
    """Own admission for sorted runs, before lifecycle and server locks."""
    sessions = tuple(sorted({Path(session).resolve() for session in session_dirs}))
    with _locks([LockIdentity(session, "membership.lock", "membership") for session in sessions], deadline):
        yield MembershipLease(sessions)


@contextmanager
def lock_membership(session_dir: str | Path, *, deadline: float = NO_LOCK_DEADLINE) -> Generator[MembershipLease]:
    with lock_memberships(session_dir, deadline=deadline) as membership:
        yield membership


@contextmanager
def lock_worker(session_dir: str | Path, name: str, *, deadline: float = NO_LOCK_DEADLINE) -> Generator[None]:
    """Own one worker operation; report both sites on recursive acquisition."""
    with lock_workers(WorkerIdentity(Path(session_dir), name), deadline=deadline):
        yield


@contextmanager
def lock_workers(*workers: WorkerIdentity, deadline: float = NO_LOCK_DEADLINE) -> Generator[None]:
    """Own a transfer's workers in canonical (session, name) order."""
    with _locks([LockIdentity(worker.session, f"{worker.name}.lifecycle.lock", "lifecycle")
                 for worker in sorted(set(workers))], deadline):
        yield


@contextmanager
def lock_server(session_dir: str | Path, filename: str, *, deadline: float = NO_LOCK_DEADLINE) -> Generator[None]:
    """Serialize server selection/retirement/reaping after lifecycle, before inbox."""
    with _locks([LockIdentity(Path(session_dir).resolve(), filename, "server")], deadline):
        yield


def _held(kind: str) -> tuple[LockIdentity, ...]:
    return tuple(identity for (pid, identity) in _operation.sites
                 if pid == os.getpid() and identity.kind == kind)


def held_workers() -> tuple[WorkerIdentity, ...]:
    """Operation ownership passed down to shared-server maintenance in this thread."""
    return tuple(sorted(WorkerIdentity(identity.session, identity.name.removesuffix(".lifecycle.lock"))
                        for identity in _held("lifecycle")))


def held_memberships() -> tuple[Path, ...]:
    return tuple(sorted(identity.session for identity in _held("membership")))


@contextmanager
def membership_operation(*session_dirs: str | Path, deadline: float = NO_LOCK_DEADLINE) -> Generator[MembershipLease]:
    """Join an explicitly enclosing admission, or acquire a new outer boundary.

    Public lock_membership still rejects recursion. Composition passes the
    existing lease through this operation boundary; it never inverts lifecycle.
    """
    sessions = tuple(sorted({Path(session).resolve() for session in session_dirs}))
    held = held_memberships()
    missing = tuple(session for session in sessions if session not in held)
    if any(worker.session in missing for worker in held_workers()):
        raise RuntimeError("membership must precede worker lifecycle acquisition")
    if missing and held and min(missing) < max(held):
        raise RuntimeError("membership runs must be acquired in sorted order")
    with lock_memberships(*missing, deadline=deadline):
        yield MembershipLease(sessions)


class WorkerStateUnreadable(ValueError):
    """Existing worker evidence cannot be interpreted safely; never overwrite it."""

    def __init__(self, path: Path, reason: str) -> None:
        self.path: Path = path
        super().__init__(f"{path}: worker state unreadable: {reason}")


def _state_object(path: Path, value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise WorkerStateUnreadable(path, "expected a JSON object")
    return cast(dict[str, object], value)


def validate_worker_state(path: Path, value: object) -> dict[str, object]:
    """Validate nested ownership and queues without coercing damaged evidence."""
    record = _state_object(path, value)
    def objects(value: object, field: str) -> dict[str, object]:
        result = _state_object(path, value)
        for key, entry in result.items():
            if not key or not isinstance(entry, dict):
                raise WorkerStateUnreadable(path, f"invalid {field} entry {key!r}")
        return result
    if path.name == "mesh_roster.json":
        for name, raw in objects(record, "roster").items():
            entry = _state_object(path, raw)
            for key in ("thread_id", "status", "role", "adopted_to", "held_design_id", "held_role", "unit_ledger"):
                if key in entry and not isinstance(entry[key], str):
                    raise WorkerStateUnreadable(path, f"{name}: invalid {key}")
            for key in ("access_policy", "transferring_role", "role_redirect"):
                if key in entry:
                    _ = _state_object(path, entry[key])
            revision = entry.get("revision", 0)
            if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
                raise WorkerStateUnreadable(path, f"{name}: invalid revision")
    elif path.name.endswith(".pending.json"):
        if (not isinstance(record.get("thread_id"), str) or not isinstance(record.get("messages"), list)
                or any(not isinstance(item, str) for item in cast(list[object], record.get("messages", [])))
                or ("accepting_messages" in record and not isinstance(record["accepting_messages"], bool))
                or ("adoption_id" in record and not isinstance(record["adoption_id"], str))
                or ("reviewer_inbox_sources" in record and (not isinstance(record["reviewer_inbox_sources"], list)
                    or any(not isinstance(item, str) for item in cast(list[object], record["reviewer_inbox_sources"]))))):
            raise WorkerStateUnreadable(path, "invalid queue fields")
    elif path.name in ("mesh_server.json", "mesh_server_start.json"):
        if not is_process_pid(record.get("pid")):
            raise WorkerStateUnreadable(path, "invalid server pid")
        if type(record.get("port")) is not int:
            raise WorkerStateUnreadable(path, "invalid server port")
        for key in ("codex_home", "dev_shell_worktree"):
            if key in record and not isinstance(record[key], str):
                raise WorkerStateUnreadable(path, f"invalid server {key}")
        if "design_context" in record:
            _ = _state_object(path, record["design_context"])
    elif path.name == "mesh_retired.json":
        servers = record.get("servers")
        if not isinstance(servers, list):
            raise WorkerStateUnreadable(path, "invalid retired server list")
        for server in cast(list[object], servers):
            try:
                _ = validate_worker_state(path.with_name("mesh_server.json"), server)
            except WorkerStateUnreadable as error:
                raise WorkerStateUnreadable(path, f"invalid retired server: {error}") from error
    elif path.name == "serving_home.json":
        if not isinstance(record.get("codex_home"), str) or not record["codex_home"]:
            raise WorkerStateUnreadable(path, "invalid serving home")
    if path.name == "design.json":
        for key in ("placement", "holder", "binding"):
            _ = _state_object(path, record.get(key))
        roles = record.get("roles")
        if not isinstance(roles, list):
            raise WorkerStateUnreadable(path, "missing role instances")
        for raw in cast(list[object], roles):
            role = _state_object(path, raw)
            _ = _state_object(path, role.get("lifecycle"))
    if path.name.endswith(".account-move.json"):
        for key in ("thread_id", "turn_id", "label", "move_id"):
            if not isinstance(record.get(key), str) or not record[key]:
                raise WorkerStateUnreadable(path, f"invalid account move {key}")
    for key in ("workers", "adoptions", "quarantined_workers", "quarantined_adoptions", "roles"):
        if key in record:
            # Design role instances are a list; their decoder checks their tagged variants.
            if key == "roles" and isinstance(record[key], list):
                if any(not isinstance(item, dict) for item in cast(list[object], record[key])):
                    raise WorkerStateUnreadable(path, "invalid role instances")
            else:
                _ = objects(record[key], key)
    if "workers" in record:
        for name, raw in _state_object(path, record["workers"]).items():
            worker = _state_object(path, raw)
            for key in ("backend", "ownership", "access_policy"):
                if key not in worker:
                    raise WorkerStateUnreadable(path, f"{name}: missing {key}")
                _ = _state_object(path, worker[key])
    return record


def validate_worker_state_value(path: Path, value: object) -> object:
    """The prompt's message-id manifest is the sole list-valued control record."""
    if path.name.endswith(".messages.json"):
        if not isinstance(value, list) or any(not isinstance(item, str) for item in cast(list[object], value)):
            raise WorkerStateUnreadable(path, "invalid pending message-id list")
        return cast(list[object], value)
    return validate_worker_state(path, value)


def read_worker_value(path: Path) -> object:
    """One strict JSON parser for object state and the message-id manifest."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OperationDeadlineExpired:
        raise
    except (OSError, UnicodeError) as error:
        raise WorkerStateUnreadable(path, str(error)) from error
    try:
        value = cast(object, json.loads(text))
    except (ValueError, UnicodeError) as error:
        raise WorkerStateUnreadable(path, str(error)) from error
    return validate_worker_state_value(path, value)


def read_worker_state(path: Path) -> dict[str, object]:
    """Strict lock-free atomic snapshot; absence still requires ownership reconciliation.

    Readers see one complete publication without creating lock files. Writers
    must instead hold locked_state through their read and replacement.
    """
    return _state_object(path, read_worker_value(path))


@dataclass(frozen=True)
class WorkerStateFile:
    """Current pathname protected by a stable sidecar; never retains a data inode."""
    path: Path

    @property
    def name(self) -> str:
        return str(self.path)

    def read_object(self) -> dict[str, object]:
        return read_worker_state(self.path)

    def read(self) -> str:
        record = self.read_object()
        return json.dumps(record) if record else ""

    def publish(self, text: str) -> None:
        # Always validate the current evidence first. Repair alone may bypass this.
        _ = self.read_object()
        value = cast(object, json.loads(text))
        _ = validate_worker_state(self.path, value)
        write_text_atomic(self.path, text)


@contextmanager
def locked_state(path: Path, *, mode: int = fcntl.LOCK_EX,
                 deadline: float = NO_LOCK_DEADLINE) -> Generator[WorkerStateFile]:
    """Lock before opening data; replacement never changes the lock's inode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a", encoding="utf-8") as lock:
        acquire_flock(lock, mode=mode, deadline=deadline)
        try:
            yield WorkerStateFile(path)
        finally:
            acquire_flock(lock, mode=fcntl.LOCK_UN)


def empty_inbox() -> dict[str, object]:
    """The sole empty-queue representation, before a thread has been assigned."""
    return {"thread_id": "", "messages": [], "accepting_messages": True}
