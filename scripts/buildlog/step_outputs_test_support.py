"""Create private step rows for reader and caller tests."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

from scripts.buildlog import index


def create_step_index(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        columns = ", ".join(f"{column[0]} {column[1].split()[0]}" for column in index.STEP_COLUMNS)
        _ = connection.execute(f"CREATE TABLE IF NOT EXISTS steps({columns})")


def record_root_attempt(path: Path, worktree: str, target: str, build: str, *, cwd: str = "",
                        host: str = "natedev", step: str = "build", call_id: str = "attempt",
                        argv: Sequence[str] = (), target_folder: str = "", started_at: str | None = None) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        _ = connection.execute("INSERT INTO steps(host, step, call_id, worktree, cwd, "
                               + "target_directory, build_directory, argv, target_folder, started_at, started_at_us) "
                               + "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (host, step, call_id, worktree, cwd, target, build, json.dumps(list(argv)),
                                target_folder, started_at, index.epoch_microseconds(started_at)))
