"""Serialize worker operations, between design/pool locks and inbox/roster locks.

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
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, order=True)
class WorkerIdentity:
    session: Path
    name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "session", self.session.resolve())
        if not self.name or Path(self.name).name != self.name or self.name in (".", ".."):
            raise ValueError(f"invalid worker name: {self.name!r}")


_sites: dict[tuple[int, int, WorkerIdentity], str] = {}
_sites_lock = threading.Lock()


@contextmanager
def lock_worker(session_dir: str | Path, name: str) -> Generator[None]:
    """Own one worker operation; report both sites on recursive acquisition."""
    with lock_workers(WorkerIdentity(Path(session_dir), name)):
        yield


@contextmanager
def lock_workers(*workers: WorkerIdentity) -> Generator[None]:
    """Own a transfer's workers in canonical (session, name) order."""
    caller = inspect.currentframe()
    assert caller is not None
    while caller.f_back and caller.f_code.co_filename in (__file__, contextlib.__file__):
        caller = caller.f_back
    site = f"{caller.f_code.co_filename}:{caller.f_lineno} ({caller.f_code.co_name})"
    del caller
    identities = sorted(set(workers))
    keys = [(os.getpid(), threading.get_ident(), worker) for worker in identities]
    with _sites_lock:
        for key in keys:
            if key in _sites:
                raise RuntimeError(f"recursive worker lifecycle lock for {key[2]}: {_sites[key]} -> {site}")
        for key in keys:
            _sites[key] = site
    try:
        with ExitStack() as locks:
            for worker in identities:
                worker.session.mkdir(parents=True, exist_ok=True)
                handle = locks.enter_context((worker.session / f"{worker.name}.lifecycle.lock").open("a"))
                fcntl.flock(handle, fcntl.LOCK_EX)
            yield
    finally:
        with _sites_lock:
            for key in keys:
                del _sites[key]


def held_workers() -> tuple[WorkerIdentity, ...]:
    """Operation ownership passed down to shared-server maintenance in this thread."""
    with _sites_lock:
        return tuple(sorted(worker for (pid, thread, worker) in _sites
                            if pid == os.getpid() and thread == threading.get_ident()))
