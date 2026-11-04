"""Find the session registry shared by Claude Code accounts."""

from __future__ import annotations

import os
from pathlib import Path


def sessions_directory() -> Path:
    """Read the override or the current home folder at each call."""
    return Path(os.environ.get("NOTIFIER_SESSIONS_DIR") or Path.home() / ".claude/sessions")
