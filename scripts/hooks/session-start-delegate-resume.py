#!/usr/bin/env python3
"""SessionStart(compact) hook: re-seat a /plan:delegate run after compaction.

`delegate.md` already says to re-read the command file after compaction and
resume the same active waits. The problem is where that instruction lives: in
the conversation being summarized. Summarization drops the rules that were not
firing at the moment it ran, which is exactly the set that matters on the far
side.

A SessionStart hook with `source == "compact"` runs after the summary is built
and its `additionalContext` is injected into the fresh context. So this text
cannot be summarized away -- it is the first thing the resumed agent reads.

Silent unless a delegate run is actually active in this session; an ordinary
compaction gets nothing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TypedDict, cast

from delegate_run import active_run


class SessionStartInput(TypedDict, total=False):
    session_id: str
    transcript_path: str
    cwd: str
    hook_event_name: str
    # startup | resume | clear | compact
    source: str
    model: str


CONTEXT = """\
A /plan:delegate run is active in this session and was just compacted. The \
summary above is not the whole picture -- compaction is a normal, expected event \
in a long run, and the run continues.

Before any further workflow action:

1. Re-read ~/.claude/commands/plan/delegate.md in full. Do not reconstruct the \
workflow from the summary; a summarized workflow silently drops rules, and the \
ones it drops are the ones that were not firing when compaction hit.
2. Read back the handoff doc named in the summary and resume the same active \
waits and control flow it describes. The authorization state is the one thing the \
summary has no reason to preserve -- take it from the handoff doc, not from \
inference, and do not assume a phase is approved.
3. Delete the handoff doc once the phase it describes is committed.

Delegate session directory: {session_dir}
Status on demand (single read, never a wait loop): {session_dir}/heartbeat.log

Then continue the run. Do not stop to announce the compaction, do not ask whether \
to continue, and do not re-run work that the handoff doc reports as done."""


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
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": CONTEXT.format(session_dir=session_dir),
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
