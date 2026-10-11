#!/usr/bin/env python3
"""Record a default role for a Claude session or subagent from a hook payload."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, cast


def main() -> None:
    if TYPE_CHECKING:
        from ..usage import agents_log
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from scripts.usage import agents_log

    value = cast(object, json.loads(sys.stdin.read()))
    if not isinstance(value, dict):
        raise ValueError("hook payload must be an object")
    payload = cast(dict[str, object], value)
    agent_id = payload.get("agent_id")
    if agent_id is not None and not isinstance(agent_id, str):
        raise ValueError("agent_id must be text")
    now = datetime.now(timezone.utc)
    if isinstance(agent_id, str) and agent_id.strip():
        _ = agents_log.register_default(agent_id, "subagent", now)
        return
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session_id is required")
    _ = agents_log.register_default(session_id, "interactive", now)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("agents_log: " + " ".join(str(exc).splitlines()), file=sys.stderr)
    sys.exit(0)
