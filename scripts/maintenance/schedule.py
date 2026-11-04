"""Lock and persist the interval history of independent maintenance work."""
from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from scripts.lib.atomic_file import write_text_atomic


@dataclass(frozen=True)
class ScheduleBusy:
    """Another process owns this schedule's current pass."""

    owner_id: str


@dataclass
class IntervalHistory:
    """Persisted times that determine when scheduled work may run again."""

    path: Path
    times: dict[str, float]

    def is_due(self, name: str, interval_seconds: float, now: float) -> bool:
        return now - self.times.get(name, float("-inf")) >= interval_seconds

    def record(self, name: str, at: float) -> None:
        self.times[name] = at
        write_text_atomic(self.path, json.dumps(self.times) + "\n")


def schedule_directory(name: str) -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "agent-notes" / name


@contextmanager
def locked_history(directory: Path) -> Generator[IntervalHistory | ScheduleBusy]:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "lock").open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _ = lock.seek(0)
            yield ScheduleBusy(lock.read().strip() or directory.name)
            return
        _ = lock.seek(0)
        _ = lock.truncate()
        _ = lock.write(str(os.getpid()))
        lock.flush()
        path = directory / "completed.json"
        try:
            saved = cast(object, json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            saved = {}
        if not isinstance(saved, dict):
            raise ValueError(f"{path}: expected completion times")
        times: dict[str, float] = {}
        for name, value in cast(dict[str, object], saved).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                times[name] = float(value)
        yield IntervalHistory(path, times)
