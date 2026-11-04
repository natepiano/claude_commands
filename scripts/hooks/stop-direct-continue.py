#!/usr/bin/env python3
"""Record turn waits for an active /unit:direct run without blocking the stop."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast

from context_usage import HookInput
from direct_run import active_run


class StopPayload(HookInput, total=False):
    last_assistant_message: str


def main() -> None:
    payload = cast(StopPayload, json.loads(sys.stdin.read()))

    if payload.get("agent_id"):
        return

    session_id = payload.get("session_id")
    if not session_id:
        return
    session_dir = active_run(session_id)
    if session_dir is None:
        return
    final_message = payload.get("last_assistant_message", "")
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.direct.progress_history import record_turn_wait

    _ = record_turn_wait(session_dir, session_id, final_message)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
