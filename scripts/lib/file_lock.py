"""Hold an exclusive file lock for one operation."""
from __future__ import annotations

import fcntl
import os
from collections.abc import Generator
from contextlib import contextmanager
from enum import Enum
from pathlib import Path


PROC_LOCKS = "/proc/locks"
# Read again because the kernel can omit a line when another lock ends.
PROC_LOCKS_READS = 3


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


def lock_holder(lock: str, locks: str = PROC_LOCKS) -> int | None:
    """Read the flock holder pid. Return None when the holder is not available.

    Read again because the kernel can omit a line when another lock ends.
    """
    try:
        stat = os.stat(lock)
    except OSError:
        return None
    # A holder line has the pid and device. A waiter line contains "->".
    device = f"{os.major(stat.st_dev):02x}:{os.minor(stat.st_dev):02x}:{stat.st_ino}"
    for _ in range(PROC_LOCKS_READS):
        try:
            with open(locks, encoding="ascii", errors="replace") as handle:
                lines = handle.read().splitlines()
        except OSError:
            return None
        for line in lines:
            fields = line.split()
            if len(fields) > 5 and fields[1] == "FLOCK" and fields[5] == device and fields[4].isdigit():
                return int(fields[4])
    return None
