#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Share the machine's test threads among the local nextest runs in flight.

Each run holds an flock on its own marker for as long as it runs. The lock
belongs to the file the calling shell opened (invoke.sh's fd 9), not to this
process, so it outlives the claim and ends with the shell: a killed run leaves
an unlocked marker, which the next claim removes and does not count. flock and
fcntl.flock work alike on Linux and macOS.

The shell opens `<name>.new`; the claim locks it and renames it to
`<name>.run` under the directory lock, so a counter never sees a marker
between its creation and its lock. A `.new` older than PENDING_GRACE_S
belongs to a shell killed before it claimed.
"""

from __future__ import annotations

import fcntl
import os
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import override

# The floor engages only past 8 runs on natedev (32 / 8 = 4), where the cores
# are full anyway: thinner runs would each take longer without freeing a core,
# while 4 each keeps 12 runs at 1.5x the logical cores, not 12x as before. On
# the Mac's 12 cores it engages past 3 runs.
FLOOR = 4
PENDING_GRACE_S = 600


def shared_width(full: int, runs: int) -> int:
    """The width of one run among `runs`, this one included."""
    return min(full, max(FLOOR, full // max(runs, 1)))


def is_locked(path: Path) -> bool:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def claim(directory: Path, name: str, full: int, fd: int, now: float | None = None) -> int:
    """Lock this run's marker through `fd`, count the live runs, return the width."""
    now = now if now is not None else time.time()
    with (directory / "lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        uncounted = 0
        try:
            os.rename(directory / f"{name}.new", directory / f"{name}.run")
        except OSError:
            uncounted = 1
        for path in directory.glob("*.new"):
            try:
                if now - path.stat().st_mtime > PENDING_GRACE_S:
                    path.unlink()
            except OSError:
                pass
        runs = uncounted
        for path in directory.glob("*.run"):
            if is_locked(path):
                runs += 1
            else:
                path.unlink(missing_ok=True)
        return shared_width(full, runs)


@dataclass(frozen=True)
class SpecifiedTestWidth:
    """A known nextest width specification, including symbolic values."""

    value: str

    @override
    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class UnknownTestWidth:
    """The run has no readable width specification or CPU count."""

    @override
    def __str__(self) -> str:
        return "unknown"


TestWidth = SpecifiedTestWidth | UnknownTestWidth


def thread_line(granted: TestWidth, requested: TestWidth) -> str:
    """Report the granted width and the machine load at claim time."""
    try:
        load = f"{os.getloadavg()[0]:.2f}"
    except OSError:
        load = "unknown"
    cpus = os.cpu_count()
    host = socket.gethostname().split(".", 1)[0]
    shared = "; shared with the runs in flight" if granted != requested else ""
    return (f"nextest: {granted} of {requested} test threads; load {load} "
            + f"on {cpus if cpus is not None else 'unknown'} CPUs ({host}){shared}")


def requested_threads(argv: list[str]) -> TestWidth:
    """Read each nextest width option, then the environment or CPU default."""
    arguments = iter(argv)
    for word in arguments:
        if word == "--":
            break
        if word in ("--test-threads", "--jobs", "-j"):
            value = next(arguments, None)
            return SpecifiedTestWidth(value) if value is not None else UnknownTestWidth()
        if word.startswith(("--test-threads=", "--jobs=")):
            return SpecifiedTestWidth(word.split("=", 1)[1])
        if word.startswith("-j") and len(word) > 2:
            return SpecifiedTestWidth(word[2:].removeprefix("="))
    value = os.environ.get("NEXTEST_TEST_THREADS")
    if value is not None:
        return SpecifiedTestWidth(value)
    cpus = os.cpu_count()
    return SpecifiedTestWidth(str(cpus)) if cpus else UnknownTestWidth()


def main() -> None:
    _, action, *arguments = sys.argv
    if action == "report":
        requested = requested_threads(arguments)
        print(thread_line(requested, requested))
        return
    if action != "claim":
        raise SystemExit(f"unknown action {action}")
    directory, name, full, fd, *options = arguments
    requested = SpecifiedTestWidth(str(int(full)))
    granted = claim(Path(directory), name, int(full), int(fd))
    print(granted)
    if "--show-load" in options:
        print(thread_line(SpecifiedTestWidth(str(granted)), requested))


if __name__ == "__main__":
    main()
