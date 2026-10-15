"""Convert earlier account-role and journal names at their storage boundary."""
from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO, cast


def accounts_config_file() -> Path:
    return Path.home() / ".claude" / "config" / "accounts.json"


@contextmanager
def roles_lock() -> Generator[None]:
    """Serialize role writers and history conversion before locking the journal."""
    path = accounts_config_file().with_suffix(".lock")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def configuration(values: dict[str, object]) -> dict[str, object]:
    """Keep an explicitly selected worker account from older configurations."""
    result = dict(values)
    if "seats" in result:
        if "workers" not in result:
            result["workers"] = result["seats"]
        del result["seats"]
    return result


def journal_entry(value: object) -> object:
    """Return only worker names while retaining all other recorded evidence."""
    if not isinstance(value, dict):
        return value
    result = dict(cast(dict[str, object], value))
    for field in ("role", "subject"):
        if result.get(field) == "seats":
            result[field] = "workers"
    if result.get("kind") == "confirmed" and "seat" in result:
        if "worker" not in result:
            result["worker"] = result["seat"]
        del result["seat"]
    return result


def migrate_locked_journal(source: TextIO) -> list[str]:
    """Convert under the caller's exclusive append lock, without replacing its inode."""
    _ = source.seek(0)
    lines = source.readlines()
    converted: list[str] = []
    changed = False
    for line in lines:
        try:
            previous = cast(object, json.loads(line))
        except ValueError:
            converted.append(line)
            continue
        current = journal_entry(previous)
        if current != previous:
            converted.append(json.dumps(current, sort_keys=True) + ("\n" if line.endswith("\n") else ""))
            changed = True
        else:
            converted.append(line)
    if changed:
        _ = source.seek(0)
        _ = source.write("".join(converted))
        _ = source.truncate()
        source.flush()
    return converted


def role_history_lines(path: Path) -> list[str]:
    """Read canonical role history while earlier processes may append old names."""
    if not path.exists():
        raise FileNotFoundError(path)
    with roles_lock():
        with path.open("r+", encoding="utf-8") as source:
            fcntl.flock(source.fileno(), fcntl.LOCK_EX)
            return migrate_locked_journal(source)
