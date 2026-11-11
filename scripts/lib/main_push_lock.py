"""Serialize main publication with explicit child handoff."""

from __future__ import annotations

import argparse
from collections.abc import Generator
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time


_HELD_LOCKS: set[str] = set()
_INHERITED_LOCK = os.environ.get("MAIN_PUSH_LOCK_HELD", "")


def _lock_path(checkout: Path) -> str:
    common = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True, text=True, check=False,
    )
    if common.returncode:
        raise OSError(common.stderr.strip())
    return str((Path(common.stdout.strip()) / "main-push.lock").resolve())


def held_environment(checkout: Path) -> dict[str, str]:
    """Give a named child access to this process's active repository hold."""
    path = _lock_path(checkout)
    if path not in _HELD_LOCKS:
        raise RuntimeError(f"this process does not hold {path}")
    return {**os.environ, "MAIN_PUSH_LOCK_HELD": path}


@contextmanager
def main_push_lock(checkout: Path) -> Generator[None]:
    """Hold the repository lock or use an active hold for this process."""
    path = _lock_path(checkout)
    if path in _HELD_LOCKS:
        yield
        return
    if _INHERITED_LOCK == path:
        _HELD_LOCKS.add(path)
        try:
            yield
        finally:
            _HELD_LOCKS.remove(path)
        return
    with Path(path).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _HELD_LOCKS.add(path)
        started = time.monotonic()
        try:
            yield
        finally:
            try:
                _ = lock.seek(0)
                _ = lock.truncate()
                _ = lock.write(json.dumps({"held_seconds": time.monotonic() - started}) + "\n")
                lock.flush()
            finally:
                _HELD_LOCKS.remove(path)
                fcntl.flock(lock, fcntl.LOCK_UN)


class LockCommand(argparse.Namespace):
    checkout: Path = Path()
    command: list[str] = []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--checkout", type=Path, required=True)
    _ = parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(namespace=LockCommand())
    command: list[str] = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("supply a command after --")
    with main_push_lock(args.checkout):
        result = subprocess.run(command, check=False)
    return result.returncode if result.returncode >= 0 else 128 - result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
