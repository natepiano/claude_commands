#!/usr/bin/env python3
"""Whether a /plan:delegate run is currently active in a given Claude session.

`prepare_session.sh` drops a marker file named for `CLAUDE_CODE_SESSION_ID`
(verified identical to the `session_id` in every hook payload) holding the
delegate session directory. `end_session.sh` removes it. Anything that needs to
know "is this session mid-delegate-run" reads the marker.

Markers are in /tmp and a killed run never removes its own, so a marker older
than MAX_AGE_SECONDS is treated as debris rather than an active run.
"""

from __future__ import annotations

import time
from pathlib import Path

ACTIVE_DIR = Path("/tmp/claude/delegate/active")

# Long enough for a real multi-phase run with slow delegate builds, short enough
# that yesterday's abandoned marker cannot block today's turns.
MAX_AGE_SECONDS = 12 * 60 * 60

# heartbeat_watch.sh beats every 60s by default (its INTERVAL_SECS). Three beats
# of slack distinguishes "delegate is working" from "the log stopped moving".
LIVE_HEARTBEAT_SECONDS = 180


def marker_path(session_id: str) -> Path:
    return ACTIVE_DIR / session_id


def active_run(session_id: str) -> Path | None:
    """The delegate session directory for an active run, or None."""
    marker = marker_path(session_id)
    try:
        age = time.time() - marker.stat().st_mtime
    except OSError:
        return None
    if age > MAX_AGE_SECONDS:
        return None
    try:
        recorded = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return Path(recorded) if recorded else None


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
