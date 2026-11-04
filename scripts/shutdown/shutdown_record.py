"""Keep only the staff row IDs that still need a start."""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import cast

from scripts.shutdown.stop_results import shutdown_state_root


def record_path() -> Path:
    return shutdown_state_root() / "restart.json"


def read() -> list[str]:
    try:
        value = cast(object, json.loads(record_path().read_text(encoding="utf-8")))
    except FileNotFoundError:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in cast(list[object], value)):
        raise ValueError("restart record must contain only staff row IDs")
    return list(dict.fromkeys(cast(list[str], value)))


def save(row_ids: list[str]) -> None:
    path = record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(list(dict.fromkeys(row_ids)), stream)
            _ = stream.write("\n")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def update(*, started: list[str], pending: list[str]) -> None:
    """Apply this run's changes without replacing another run's pending rows."""
    path = record_path().with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        save([row for row in read() if row not in started] + pending)
