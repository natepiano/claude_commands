"""Hold an exclusive file lock for one operation."""
from __future__ import annotations

import fcntl
from collections.abc import Generator
from contextlib import contextmanager
from enum import Enum
from pathlib import Path


class LockResult(Enum):
    ACQUIRED = "acquired"
    BUSY = "busy"


@contextmanager
def exclusive(path: Path, *, wait: bool = True) -> Generator[LockResult]:
    """Release an acquired lock when the operation ends."""
    with path.open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            if wait:
                raise
            yield LockResult.BUSY
            return
        try:
            yield LockResult.ACQUIRED
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
