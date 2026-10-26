"""Prompts a script typed into a herdr pane, so the prompt hook does not count them as the user's.

`Herdr.prompt` records each prompt before it types it: one empty file per pane and text, named by a
hash. The UserPromptSubmit hook runs in the pane's Claude process, reads its own `HERDR_PANE_ID` and
asks `typed_here`. A record counts for KEEP_SECONDS: a prompt typed into a busy session waits in its
queue until the turn ends. Each record call removes the records older than that.
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

KEEP_SECONDS = 6 * 3600


def root() -> Path:
    state = os.environ.get("CONVERSATION_PAUSE_STATE_DIR") or Path.home() / ".local/state/conversation-pause"
    return Path(state) / "script-prompts"


def _path(pane: str, text: str) -> Path:
    words = " ".join(text.split())
    return root() / hashlib.sha256(f"{pane}\0{words}".encode()).hexdigest()


def record(pane: str, text: str) -> None:
    folder = root()
    folder.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for old in folder.iterdir():
        try:
            if now - old.stat().st_mtime > KEEP_SECONDS:
                old.unlink(missing_ok=True)
        except OSError:
            continue
    _path(pane, text).touch()


def typed_by_script(pane: str, text: str) -> bool:
    if not pane:
        return False
    try:
        return time.time() - _path(pane, text).stat().st_mtime <= KEEP_SECONDS
    except OSError:
        return False


def typed_here(text: str) -> bool:
    """A script typed this prompt into the herdr pane this process runs in."""
    return typed_by_script(os.environ.get("HERDR_PANE_ID", ""), text)
