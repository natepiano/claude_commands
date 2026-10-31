"""Read one process identity without mistaking an unreadable start for an exit.

This module also runs under the Mac board's Python 3.9. Keep the Linux reader
lightweight: the step-record CLI uses it before loading its optional machinery.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import TYPE_CHECKING, NamedTuple


class Started(NamedTuple):
    start: str


class Zombie(NamedTuple):
    start: str


class Gone(NamedTuple):
    """The pid no longer exists."""


class Unreadable(NamedTuple):
    reason: str


if TYPE_CHECKING:
    ProcessStart = Started | Zombie | Gone | Unreadable


def read_process_start(pid: int) -> ProcessStart:
    """Return the host's stored start form and the process's observed state."""
    if pid <= 0:
        return Gone()
    if sys.platform == "darwin":
        return _darwin_start(pid)
    path = Path(os.environ.get("VERIFY_PROC_ROOT", "/proc")) / str(pid) / "stat"
    try:
        # A truncated comm can contain invalid UTF-8, spaces and parentheses.
        head, closing, tail = path.read_bytes().rpartition(b")")
    except (FileNotFoundError, ProcessLookupError):
        return Gone()
    except OSError as error:
        return Unreadable(str(error))
    fields = tail.split()
    if not closing or not head or len(fields) <= 19 or not fields[19].isdigit():
        return Unreadable(f"invalid process stat: {path}")
    start = fields[19].decode("ascii")
    return Zombie(start) if fields[0] == b"Z" else Started(start)


def matches_stored_start(pid: int, observed: Started | Zombie, stored_start: str) -> bool:
    """Match canonical starts and pre-install Darwin starts in the local zone.

    Recheck the canonical identity after the legacy read so PID reuse between
    the two ps calls cannot turn a legacy match into authority over a new group.
    The caller still decides which process states permit its operation.
    """
    if observed.start == stored_start:
        return True
    if sys.platform != "darwin":
        return False
    legacy = _darwin_start(pid, utc=False)
    if not isinstance(legacy, (Started, Zombie)) or legacy.start != stored_start:
        return False
    confirmed = read_process_start(pid)
    return type(confirmed) is type(observed) and confirmed == observed


def _darwin_start(pid: int, *, utc: bool = True) -> ProcessStart:
    import subprocess

    try:
        result = subprocess.run(
            ("ps", "-p", str(pid), "-o", "stat=", "-o", "lstart="),
            capture_output=True, text=True, timeout=5, check=False,
            env={**os.environ, "LC_ALL": "C", **({"TZ": "UTC"} if utc else {})},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return Unreadable(str(error))
    if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
        return Gone()
    if result.returncode != 0:
        return Unreadable(result.stderr.strip() or f"ps exited {result.returncode}")
    fields = result.stdout.strip().split(maxsplit=1)
    if len(fields) != 2:
        return Unreadable("ps returned no process state or start")
    state, start = fields
    date = start.split()
    if (len(date) != 5 or date[0] not in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
            or date[1] not in ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
            or not date[2].isdigit() or not 1 <= int(date[2]) <= 31
            or len(date[4]) != 4 or not date[4].isdigit()):
        return Unreadable("ps returned an invalid process start")
    clock = date[3].split(":")
    if (len(clock) != 3 or any(len(part) != 2 or not part.isdigit() for part in clock)
            or int(clock[0]) > 23 or int(clock[1]) > 59 or int(clock[2]) > 60):
        return Unreadable("ps returned an invalid process start")
    return Zombie(start) if state.startswith("Z") else Started(start)
