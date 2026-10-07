#!/usr/bin/env python3
"""Record when the first reply after a pausing message finishes."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import TypedDict, cast


class StopPayload(TypedDict, total=False):
    session_id: str
    agent_id: str


def pause_record_path(session_id: str) -> Path:
    root = Path(os.environ.get("CONVERSATION_PAUSE_STATE_DIR")
                or Path.home() / ".local/state/conversation-pause")
    return root / f"{session_id}.json"


def main() -> None:
    payload = cast(StopPayload, json.loads(sys.stdin.read()))
    if "agent_id" in payload:
        return
    session_id = payload.get("session_id", "")
    if not session_id or not pause_record_path(session_id).exists():
        return

    import conversation_pause

    conversation_pause.mark_answered(session_id, conversation_pause.now_epoch())


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        lines = str(error).splitlines()
        detail = lines[0] if lines else ""
        print(f"conversation-pause: {type(error).__name__}: {detail}", file=sys.stderr)
    raise SystemExit(0)
