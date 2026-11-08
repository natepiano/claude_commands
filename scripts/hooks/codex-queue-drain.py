#!/usr/bin/env python3
"""Give a Codex session its queued messages at startup or after a tool."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import sys
from typing import cast

from scripts.message import send


def main() -> int:
    payload = cast(object, json.loads(sys.stdin.read()))
    if not isinstance(payload, dict):
        return 0
    fields = cast(dict[str, object], payload)
    event = fields.get("hook_event_name")
    identity = fields.get("session_id")
    if event not in ("SessionStart", "PostToolUse") or not isinstance(identity, str) or not identity:
        return 0
    _ = send.pending(send.CodexQueueOwner(identity), emit=True,
                     output=send.CodexHookContext(event))
    return 0


if __name__ == "__main__":
    sys.exit(main())
