#!/usr/bin/env python3
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
import sys
import time
from pathlib import Path

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


def main() -> None:
    _, action, directory, name, full, fd = sys.argv
    if action != "claim":
        raise SystemExit(f"unknown action {action}")
    print(claim(Path(directory), name, int(full), int(fd)))


if __name__ == "__main__":
    main()
