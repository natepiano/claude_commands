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


@contextmanager
def claude_config_lock(path: Path) -> Generator[None]:
    """Wait for the kernel lock until the current holder releases it."""
    with path.with_name(path.name + ".lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def device_key(path: str | Path) -> str:
    """Identify the device and inode used by the kernel lock table."""
    status = os.stat(path)
    return f"{os.major(status.st_dev):02x}:{os.minor(status.st_dev):02x}:{status.st_ino}"


def held_flock_keys(locks: str = PROC_LOCKS) -> dict[str, int]:
    """Read the kernel table once and map held flock keys to process ids."""
    with open(locks, encoding="ascii", errors="replace") as handle:
        lines = handle.read().splitlines()
    held: dict[str, int] = {}
    for line in lines:
        fields = line.split()
        if len(fields) > 5 and fields[1] == "FLOCK" and fields[4].isdigit():
            _ = held.setdefault(fields[5], int(fields[4]))
    return held


def lock_holder(lock: str, locks: str = PROC_LOCKS, *, reads: int = PROC_LOCKS_READS) -> int | None:
    """Read the flock holder pid; repeat when the kernel omits a holder line."""
    try:
        device = device_key(lock)
    except OSError:
        return None
    for _ in range(reads):
        try:
            held = held_flock_keys(locks)
        except OSError:
            return None
        if device in held:
            return held[device]
    return None
