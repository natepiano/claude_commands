#!/usr/bin/env python3
"""Whether a /unit:direct run is currently active in a given Claude session.

`prepare_session.sh` drops a marker file named for `CLAUDE_CODE_SESSION_ID`
(verified identical to the `session_id` in every hook payload) holding the
unit session directory. `end_session.sh` removes it. Anything that needs to
know "is this session mid-unit-run" reads the marker.

Markers are in /tmp and a killed run never removes its own, so a run with no
write inside MAX_AGE_SECONDS is treated as debris rather than an active run.
Staleness is judged from the session directory's own contents, never from the
marker's mtime -- see `_recently_active`.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from ..lib import unit_runs
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
    import unit_runs

# A test's own marker folder; unset, the marker is under either run-folder name.
ACTIVE_OVERRIDE = unit_runs.environ("ACTIVE_DIR") or None

# Long enough to span a slow worker build plus the user's time at a gate, short
# enough that yesterday's abandoned run cannot block today's turns. It bounds
# silence, not total run length: a multi-day run stays active as long as it keeps
# writing.
MAX_AGE_SECONDS = 12 * 60 * 60


def marker_path(session_id: str) -> Path:
    return unit_runs.marker(session_id, ACTIVE_OVERRIDE)


def _recently_active(session_dir: Path) -> bool:
    """True when the run itself wrote something inside MAX_AGE_SECONDS.

    Liveness is read from the session directory, never from the marker's own
    mtime. `prepare_session.sh` stamps the marker once at run start and nothing
    refreshes it, so marker age measures how long ago the run *began* -- which
    made every run silently unrecognizable to its own hooks twelve hours in,
    while it was still writing state every few seconds. What actually stops
    moving when a run dies is the run's own files.
    """
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
        recorded = marker_path(session_id).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not recorded:
        return None
    session_dir = Path(recorded)
    return session_dir if _recently_active(session_dir) else None


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def running_work(session_dir: Path) -> str | None:
    """Name work in flight, or return None when the unit is waiting."""
    # A launch that names its phase writes its status in that phase's directory.
    directories = [session_dir, *(path for path in session_dir.glob("phases/*") if path.is_dir())]
    if any(
        _text(path) == "implementing"
        for directory in directories
        for path in directory.glob("impl_status*")
        if path.name == "impl_status" or path.name.startswith("impl_status_")
    ):
        return "an implementation or fix pass"
    if any(_text(path) == "reviewing" for directory in directories for path in directory.glob("review_status*")):
        return "a review pass"
    state_text = _text(session_dir / "progress_history_state.json")
    if state_text:
        try:
            state = cast("dict[str, object]", json.loads(state_text))
        except json.JSONDecodeError:
            return None
        activity = state.get("activity")
        if isinstance(activity, dict):
            entry = cast("dict[str, object]", activity)
            if entry.get("status") == "active":
                label = entry.get("label")
                return label if isinstance(label, str) and label else "an activity"
    return None

