"""Own run membership, worker operations and server maintenance flocks.

Order: ledger, design/pool, membership, future staff, lifecycle, server, inbox, roster.

An operation owns this lock through its server effects and final publication,
but releases it before watching a turn. Nested acquisition is a programming
error; callers must pass ownership down instead of reacquiring it.
"""
from __future__ import annotations

import fcntl
import contextlib
import os
import inspect
import threading
import time
import select
from collections.abc import Generator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO, final


@dataclass(frozen=True, order=True)
class WorkerIdentity:
    session: Path
    name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "session", self.session.resolve())
        if not self.name or Path(self.name).name != self.name or self.name in (".", ".."):
            raise ValueError(f"invalid worker name: {self.name!r}")


NO_LOCK_DEADLINE = float("inf")


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


def acquire_flock(handle: TextIO, *, mode: int = fcntl.LOCK_EX,
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
            raise OperationDeadlineExpired(f"shutdown incomplete: timed out acquiring lock {handle.name}")
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
