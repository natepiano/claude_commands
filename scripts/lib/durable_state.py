"""Atomic publication and stable locks for state that must survive a stopped writer."""
from __future__ import annotations

import fcntl
import os
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from scripts.message import sessions


def atomic_write_text(path: Path, text: str) -> None:
    """Publish a complete, flushed file; a stopped write leaves the previous file intact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=f".{path.name}.", delete=False) as output:
        temporary = Path(output.name)
        try:
            _ = output.write(text)
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


@contextmanager
def file_lock(path: Path, *, blocking: bool = True) -> Generator[None]:
    """Lock an inode that is never replaced or unlinked; the OS releases it on exit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield


@contextmanager
def locked_text(path: Path, *, blocking: bool = True) -> Generator[TextIO]:
    """Open state only after acquiring its permanent sidecar lock."""
    with file_lock(path.with_name(path.name + ".lock"), blocking=blocking):
        with path.open("a+", encoding="utf-8") as handle:
            _ = handle.seek(0)
            yield handle


def rewrite_text(handle: TextIO, text: str) -> None:
    """Publish under the caller's sidecar lock and refresh its transaction's reader."""
    path = Path(handle.name)
    atomic_write_text(path, text)
    # Some inbox transactions read again after writing. Keep that reader on the
    # newly published inode, without changing the permanent lock's descriptor.
    with path.open("r+", encoding="utf-8") as current:
        _ = os.dup2(current.fileno(), handle.fileno())
    _ = handle.seek(0)


@dataclass(frozen=True)
class ProcessLifetime:
    """One original process, distinguished from later reuse of its PID."""

    pid: int
    started: str

    @classmethod
    def current(cls, pid: int) -> ProcessLifetime:
        started = sessions.process_start(pid)
        if not started:
            raise ValueError(f"cannot identify original process {pid}")
        return cls(pid, started)

    def is_live(self) -> bool:
        state = sessions.process_liveness(self.pid, self.started)
        if isinstance(state, sessions.SessionLivenessUnknown):
            raise ValueError(state.reason)
        return isinstance(state, sessions.LiveSessionProcess)
