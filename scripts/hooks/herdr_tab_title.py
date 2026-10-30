"""Keep a Claude session's tab and pane names current.

A launcher titles the tab with the name it gives the session. A later `/rename` changes only
the session record, so at the end of each turn this hook updates both names
and orders changed tabs again. It acts only in a pane of the shared herdr
session, and it never stops the turn: a fault leaves the title as it was.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "herdr"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "message"))
import herdr  # noqa: E402
import sessions  # noqa: E402


class StopPayload(TypedDict, total=False):
    session_id: str


def current_name(session_id: str) -> str:
    """The name in this session's record now, or empty when no record carries the id."""
    for path in sessions.sessions_dir().glob("*.json"):
        record = sessions.read_session(path)
        if isinstance(record, sessions.UnreadableSessionRecord):
            continue
        if record["sessionId"] == session_id and not sessions.headless(record):
            return record["name"]
    return ""


def sync(payload: StopPayload, environment: Mapping[str, str]) -> bool:
    """Retitle this session's tab when its name changed. True when a title changed."""
    pane = environment.get("HERDR_PANE_ID", "")
    session_id = payload.get("session_id", "")
    if not pane or not session_id or environment.get("TMUX_PANE"):
        return False
    shared = herdr.shared_session()
    if environment.get("HERDR_SESSION") != shared:
        return False
    name = current_name(session_id)
    if not name:
        return False
    return herdr.Herdr(shared).retitle(pane, name)


def main() -> int:
    try:
        payload = cast(StopPayload, json.loads(sys.stdin.read() or "{}"))
        _ = sync(payload, os.environ)
    except (OSError, ValueError, herdr.HerdrFailure):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
