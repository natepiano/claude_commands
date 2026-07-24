#!/usr/bin/env python3
"""Run one watcher process under a crash-safe, nonblocking runner lock."""

from __future__ import annotations

import argparse
import fcntl
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import writer_lock  # pyright: ignore[reportImplicitRelativeImport]


BUSY_EXIT = 75


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)

    status = subparsers.add_parser("status", help="report whether a runner owns the lock")
    _ = status.add_argument("lock_path", type=Path)

    run = subparsers.add_parser("run", help="run one command when the lock is free")
    _ = run.add_argument("lock_path", type=Path)
    _ = run.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _argument_parser().parse_args(argv)
    lock_path = cast(Path, arguments.lock_path)
    operation = cast(str, arguments.operation)
    if operation == "status":
        held = writer_lock.lock_is_held(lock_path)
        print("held" if held else "free")
        return 1 if held else 0

    command = cast(list[str], arguments.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        _argument_parser().error("run requires a command")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return BUSY_EXIT
        os.fchmod(descriptor, 0o600)
        owner = f"pid={os.getpid()}\n".encode("ascii")
        _ = os.ftruncate(descriptor, 0)
        _ = os.lseek(descriptor, 0, os.SEEK_SET)
        _ = os.write(descriptor, owner)
        os.set_inheritable(descriptor, True)
        os.execvp(command[0], command)
    except OSError as error:
        print(f"runner lock error: {error}", file=sys.stderr)
        return 2
    finally:
        _ = os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
