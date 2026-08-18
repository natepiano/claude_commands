#!/usr/bin/env python3
"""Whether a /plan:delegate run is currently active in a given Claude session.

`prepare_session.sh` drops a marker file named for `CLAUDE_CODE_SESSION_ID`
(verified identical to the `session_id` in every hook payload) holding the
delegate session directory. `end_session.sh` removes it. Anything that needs to
know "is this session mid-delegate-run" reads the marker.

Markers are in /tmp and a killed run never removes its own, so a run with no
write inside MAX_AGE_SECONDS is treated as debris rather than an active run.
Staleness is judged from the session directory's own contents, never from the
marker's mtime -- see `_recently_active`.
"""

from __future__ import annotations

import time
from pathlib import Path

ACTIVE_DIR = Path("/tmp/claude/delegate/active")

# Long enough to span a slow delegate build plus the user's time at a gate, short
# enough that yesterday's abandoned run cannot block today's turns. It bounds
# silence, not total run length: a multi-day run stays active as long as it keeps
# writing.
MAX_AGE_SECONDS = 12 * 60 * 60

# heartbeat_watch.sh beats every 60s by default (its INTERVAL_SECS). Three beats
# of slack distinguishes "delegate is working" from "the log stopped moving".
LIVE_HEARTBEAT_SECONDS = 180


def marker_path(session_id: str) -> Path:
    return ACTIVE_DIR / session_id


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
    """The delegate session directory for an active run, or None."""
    try:
        recorded = marker_path(session_id).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not recorded:
        return None
    session_dir = Path(recorded)
    return session_dir if _recently_active(session_dir) else None


def delegate_working(session_dir: Path) -> bool:
    """True when a delegate is mid-dispatch, judged by a moving heartbeat.

    An agent parked on a completion waiter has nothing to do until the delegate
    finishes, and the completion notification re-invokes it anyway -- that
    re-invocation is a new request, so compaction fires there on its own. There
    is nothing to gain by refusing that stop.
    """
    try:
        age = time.time() - (session_dir / "heartbeat.log").stat().st_mtime
    except OSError:
        return False
    return age <= LIVE_HEARTBEAT_SECONDS
