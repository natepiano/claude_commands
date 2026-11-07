#!/usr/bin/env python3
"""Tell a resumed showrunner to read its current next list.

SessionStart compact or resume adds the production resume command to the context.
Only a session named by a production update timer receives this text.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from typing import TypedDict, cast

PRODUCTION = Path(__file__).resolve().parent.parent / "production"
sys.path.insert(0, str(PRODUCTION))
from scripts.production import showrunners  # noqa: E402


class HookInput(TypedDict, total=False):
    session_id: str
    hook_event_name: str
    source: str


CONTEXT = """\
You are the showrunner of {doc}, and this session was just {event}. Before any \
other action, run `/showrunner:produce {doc} resume`. Skip this command if the \
resuming prompt already runs it. This command prints the current `next` list from \
the state file. Act on it first. Do not stop to announce the compaction."""


def main() -> None:
    payload = cast(HookInput, json.loads(sys.stdin.read()))
    session_id = payload.get("session_id", "")
    doc = showrunners.production_of(session_id) if session_id else None
    if doc is None:
        return
    event = payload.get("hook_event_name")
    source = payload.get("source")
    if event != "SessionStart" or source not in ("compact", "resume"):
        return
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": CONTEXT.format(doc=doc, event="compacted" if source == "compact" else "resumed"),
    }}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # A hook must never break the session it serves.
        pass
    sys.exit(0)
