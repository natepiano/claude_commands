"""Share the compaction marker location and session id rule."""

from __future__ import annotations

import re
from pathlib import Path


SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def marker_folder() -> Path:
    return Path.home() / ".local/state/compaction"


def clear(session_id: str) -> None:
    """Delete one session's marker without a lock or a directory scan."""
    if SESSION_ID_PATTERN.fullmatch(session_id):
        (marker_folder() / f"{session_id}.json").unlink(missing_ok=True)
