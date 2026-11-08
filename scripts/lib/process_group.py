"""Validate process-group ids before sending a signal."""

from __future__ import annotations

import os
import signal
import sys
from collections.abc import Sequence


def check_process_group(pid: object) -> int:
    """Refuse special ids, non-integers and the caller's own group."""
    if type(pid) is not int:
        raise ValueError("process group pid must be a real int")
    if pid <= 1:
        raise ValueError("process group pid must be greater than 1")
    if pid == os.getpgrp():
        raise ValueError("refusing to signal the caller's own process group")
    return pid


def send_process_group(pid: object, signum: int) -> None:
    """Check the group id and send; let the caller handle a missing group."""
    os.killpg(check_process_group(pid), signum)


def main(argv: Sequence[str]) -> int:
    """CLI: process_group.py PID SIGNAL (TERM, SIGTERM or a signal number)."""
    if len(argv) != 2:
        print("usage: process_group.py PID SIGNAL", file=sys.stderr)
        return 2
    try:
        pid = int(argv[0])
        name = argv[1]
        if name.isdecimal():
            signum = int(name)
        else:
            signum = signal.Signals[name if name.startswith("SIG") else "SIG" + name].value
        send_process_group(pid, signum)
    except ProcessLookupError:
        return 3
    except (ValueError, KeyError, OSError) as error:
        print(f"process_group: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
