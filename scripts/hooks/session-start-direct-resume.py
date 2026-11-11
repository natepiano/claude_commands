#!/usr/bin/env python3
"""Load the recorded unit command after compaction of an active run."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import sys
from pathlib import Path
from typing import TypedDict, cast

from scripts.hooks.direct_run import active_run
from scripts.production import staff


class SessionStartInput(TypedDict, total=False):
    session_id: str
    transcript_path: str
    cwd: str
    hook_event_name: str
    # startup | resume | clear | compact
    source: str
    model: str


CONTEXT = """\
An active {command} run continues after compaction.

Before any further workflow action:

1. Read ~/.claude/commands/{command_file}.md in full.
2. Read each stub file whose moment is live.
3. Read the handoff document named in the summary.
4. Restore live dispatch state without waiting for completion.
5. Read the authorization state from the handoff document.
6. Delete the handoff document after its phase commits and any reservation release succeeds.

Unit session directory: {session_dir}
Status on demand (single read, never a wait loop): {session_dir}/heartbeat.log

Continue the run. Do not repeat completed work."""


def main() -> None:
    payload = cast(SessionStartInput, json.loads(sys.stdin.read()))
    if payload.get("source") != "compact":
        return
    session_id = payload.get("session_id")
    if not session_id:
        return
    session_dir: Path | None = active_run(session_id)
    if session_dir is None:
        return
    row = staff.local_by_current_session(session_id)
    if row is None or row.kind != "unit":
        return
    command = staff.unit_command(row)
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": CONTEXT.format(session_dir=session_dir, command=command,
                        command_file=command.removeprefix("/").replace(":", "/")),
                }
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # A hook must never break the session it is resuming.
        pass
    sys.exit(0)
