#!/usr/bin/env python3
"""PreCompact and SessionStart(compact|resume) hook: keep a showrunner's state across a compaction.

A showrunner wrote its STATE block only when it remembered to run
`update_registration.py log --before-compaction`. PreCompact now writes it: the
block carries the showrunner state file's `next` list, the actions it owes and
what sets each off. SessionStart then tells the resumed showrunner to run
`/showrunner:produce <doc> resume` and act on that list first, in text the
summary cannot drop.

A session is a showrunner when a production's update timer is addressed to its
session id (`showrunners.production_of`). Any other session gets nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from typing import TypedDict, cast

PRODUCTION = Path(__file__).resolve().parent.parent / "production"
sys.path.insert(0, str(PRODUCTION))
from scripts.production import showrunners  # noqa: E402

UPDATE_REGISTRATION = Path(os.environ.get("SHOWRUNNER_UPDATE_REGISTRATION") or PRODUCTION / "update_registration.py")
LOG_TIMEOUT_SECONDS = 30


class HookInput(TypedDict, total=False):
    session_id: str
    hook_event_name: str
    # PreCompact: manual | auto. SessionStart: startup | resume | clear | compact.
    trigger: str
    source: str


CONTEXT = """\
You are the showrunner of {doc}, and this session was just {event}. Before any \
other action, run `/showrunner:produce {doc} resume`, unless the prompt that \
resumed you already runs it. The last `### STATE` block \
in its log was written before the compaction. Act first on what you had in \
flight and on its `Next:` line, then go on. Do not stop to announce the \
compaction."""


def write_state(doc: Path, trigger: str) -> None:
    """Append the STATE block; a state file the log refuses gives way to a block without it."""
    slug = showrunners.production_slug(str(doc))
    state = showrunners.session_state_dir(slug) / "showrunner_state.json"
    base = [sys.executable, str(UPDATE_REGISTRATION), "log", f"before compaction ({trigger or 'auto'})",
            "--production", str(doc), "--before-compaction"]
    attempts = [[*base, "--state", str(state)], base] if state.exists() else [base]
    for command in attempts:
        if subprocess.run(command, capture_output=True, timeout=LOG_TIMEOUT_SECONDS).returncode == 0:
            return


def main() -> None:
    payload = cast(HookInput, json.loads(sys.stdin.read()))
    session_id = payload.get("session_id", "")
    doc = showrunners.production_of(session_id) if session_id else None
    if doc is None:
        return
    event = payload.get("hook_event_name")
    if event == "PreCompact":
        write_state(doc, payload.get("trigger", ""))
        return
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
