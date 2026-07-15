#!/usr/bin/env python3
"""Print a cheap stable signature for files that should wake backlog ranking."""

from __future__ import annotations

import hashlib
import json
import stat
import sys
from pathlib import Path


ISSUES_DIR = Path("/Users/natemccoy/rust/hanadocs/issues")
GOALS_FILE = Path("/Users/natemccoy/rust/hanadocs/prioritization goals.md")


class SignatureError(RuntimeError):
    """The watched file set cannot be inspected safely."""


def _signature(path: Path) -> tuple[int, int, int, int, int]:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise SignatureError(f"cannot inspect {path}: {error}") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise SignatureError(f"refusing to follow symlink: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise SignatureError(f"expected a regular file: {path}")
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def build_signature(issues_dir: Path, goals_file: Path) -> str:
    try:
        metadata = issues_dir.lstat()
    except OSError as error:
        raise SignatureError(f"cannot inspect {issues_dir}: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise SignatureError(f"expected a real issue directory: {issues_dir}")

    try:
        paths = tuple(sorted(issues_dir.glob("*.md"), key=lambda path: str(path)))
        records = [
            (path.name, _signature(path))
            for path in paths
        ]
        if tuple(sorted(issues_dir.glob("*.md"), key=lambda path: str(path))) != paths:
            raise SignatureError("issue membership changed during signature scan")
        payload = {
            "goals": _signature(goals_file),
            "issues": records,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except OSError as error:
        raise SignatureError(f"cannot enumerate {issues_dir}: {error}") from error
    return hashlib.sha256(encoded).hexdigest()


def main() -> int:
    try:
        print(build_signature(ISSUES_DIR, GOALS_FILE))
        return 0
    except SignatureError as error:
        print(f"watch signature error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
