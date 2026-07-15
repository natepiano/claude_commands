#!/usr/bin/env python3
"""Crash-safe single-writer lock shared by Hanadocs prioritization tools."""

from __future__ import annotations

import argparse
import fcntl
import os
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path


LOCK_PATH = Path("/tmp/hanadocs-prioritize/writer.lock")


class WriterLockError(RuntimeError):
    """The shared writer lock could not be acquired safely."""


@contextmanager
def acquire_writer_lock(
    path: Path = LOCK_PATH,
    *,
    timeout_seconds: float = 30.0,
) -> Iterator[None]:
    """Acquire an OS-released exclusive lock, waiting for a bounded interval."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(descriptor, 0o600)
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise WriterLockError(
                        f"timed out waiting for prioritization writer lock: {path}"
                    )
                time.sleep(0.1)

        owner = f"pid={os.getpid()} acquired={time.time_ns()}\n".encode("ascii")
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, owner)
        os.fsync(descriptor)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def lock_is_held(path: Path = LOCK_PATH) -> bool:
    """Return whether another process currently owns the writer lock."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--status",
        action="store_true",
        help="print whether the shared writer lock is held",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _argument_parser().parse_args(argv)
    if not arguments.status:
        _argument_parser().error("only --status is supported")
    held = lock_is_held()
    print("held" if held else "free")
    return 1 if held else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WriterLockError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
