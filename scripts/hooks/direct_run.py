#!/usr/bin/env python3
"""Read the open unit run from the director's staff row.

A run remains active while its own files have recent writes.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import time
from pathlib import Path
from typing import cast

from scripts.lib import unit_runs
from scripts.direct.worker_state import worker_status_paths

# Long enough to span a slow worker build plus the user's time at a gate, short
# enough that yesterday's abandoned run cannot block today's turns. It bounds
# silence, not total run length: a multi-day run stays active as long as it keeps
# writing.
MAX_AGE_SECONDS = 12 * 60 * 60


def _recently_active(session_dir: Path) -> bool:
    """Return true when the run has a file written within MAX_AGE_SECONDS."""
    try:
        newest = max(
            (entry.stat().st_mtime for entry in session_dir.iterdir() if entry.is_file()),
            default=None,
        )
    except OSError:
        return False
    if newest is None:
        return False
    return time.time() - newest <= MAX_AGE_SECONDS


def active_run(session_id: str) -> Path | None:
    """The unit session directory for an active run, or None."""
    try:
        session_dir = unit_runs.open_run(session_id)
    except OSError:
        return None
    if session_dir is None:
        return None
    return session_dir if _recently_active(session_dir) else None


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def running_work(session_dir: Path) -> str | None:
    """Name work in flight, or return None when the unit is waiting."""
    statuses = worker_status_paths(session_dir)
    if any(_text(path) in {"implementing", "running"} for path in statuses):
        return "an implementation or fix pass"
    if any(_text(path) == "reviewing" for path in statuses):
        return "a review pass"
    try:
        state = cast("dict[str, object]", json.loads(_text(session_dir / "progress_history_state.json")))
    except json.JSONDecodeError:
        return None
    activity = state.get("activity")
    if isinstance(activity, dict):
        entry = cast("dict[str, object]", activity)
        if entry.get("status") == "active":
            label = entry.get("label")
            return label if isinstance(label, str) and label else "an activity"
    return None
