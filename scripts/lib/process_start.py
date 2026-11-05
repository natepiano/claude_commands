"""Read a process start without losing the reason for an absent identity."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from typing import override
else:
    _Method = TypeVar("_Method")

    def override(method: _Method) -> _Method:
        return method


class Started:
    """A live process has this stored start identity."""

    def __init__(self, start: str) -> None:
        self.start: str = start

    @override
    def __eq__(self, other: object) -> bool:
        return type(other) is Started and self.start == other.start


class Zombie:
    """An ended process still owns its PID and start identity."""

    def __init__(self, start: str) -> None:
        self.start: str = start

    @override
    def __eq__(self, other: object) -> bool:
        return type(other) is Zombie and self.start == other.start


class Gone:
    """The process no longer exists."""

    @override
    def __eq__(self, other: object) -> bool:
        return type(other) is Gone


class Unreadable:
    """The process can exist, but its start cannot be read."""

    def __init__(self, reason: str) -> None:
        self.reason: str = reason

    @override
    def __eq__(self, other: object) -> bool:
        return type(other) is Unreadable and self.reason == other.reason


if TYPE_CHECKING:
    ProcessStart = Started | Zombie | Gone | Unreadable
else:
    # Python 3.9 imports this module from board.sh. Annotations stay deferred.
    ProcessStart = object


def process_start_format() -> str:
    """Name the identity format used on this host."""
    return "darwin-lstart-utc-c-v1" if sys.platform == "darwin" else "linux-proc-stat22-v1"


def process_start(pid: int) -> ProcessStart:
    """Use one identity form per host for every owner record."""
    if pid <= 0:
        return Unreadable("invalid process id")
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "lstart=", "-o", "stat="],
                capture_output=True, text=True, timeout=5, check=False,
                env={**os.environ, "LC_ALL": "C", "TZ": "UTC"})
        except (OSError, UnicodeError, subprocess.SubprocessError) as error:
            return Unreadable(str(error))
        if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
            return Gone()
        if result.returncode:
            return Unreadable(result.stderr.strip() or "ps failed")
        fields = result.stdout.strip().rsplit(None, 1)
        if (len(fields) != 2 or not re.fullmatch(
                r"[A-Z][a-z]{2} [A-Z][a-z]{2} +[0-9]{1,2} [0-9]{2}:[0-9]{2}:[0-9]{2} [0-9]{4}", fields[0])
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9+<>-]*", fields[1])):
            return Unreadable("invalid ps process identity")
        return Zombie(fields[0]) if fields[1].startswith("Z") else Started(fields[0])
    try:
        path = Path(os.environ.get("VERIFY_PROC_ROOT", "/proc")) / str(pid) / "stat"
        _, separator, rest = path.read_bytes().rpartition(b")")
        fields = rest.split()
        if not separator or len(fields) <= 19 or not fields[19].isdigit() or fields[0] not in (b"R", b"S", b"D", b"Z", b"T", b"t", b"X", b"x", b"K", b"W", b"P", b"I"):
            return Unreadable("invalid process stat fields")
        start = fields[19].decode("ascii")
        return Zombie(start) if fields[0] == b"Z" else Started(start)
    except (FileNotFoundError, ProcessLookupError):
        return Gone()
    except (OSError, UnicodeError) as error:
        return Unreadable(str(error))
