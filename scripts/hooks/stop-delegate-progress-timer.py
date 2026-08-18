#!/usr/bin/env python3
"""Stop hook: refuse to end a /plan:delegate turn that leaves work unreported.

A delegate run reports progress by arming a one-shot timer, ending the turn, and
letting the timer notification re-invoke the agent. When the agent forgets to
re-arm -- easy to do right after a dispatch completes and a new command starts --
the run keeps working and the user sees nothing until it happens to finish. That
failure is silent by construction, which is why it recurs.

This hook makes it loud. It blocks the stop when a delegate run has work running
and no timer pending, and says which to fix.

Live work is judged from what the run itself writes, not from the heartbeat
alone: `delegate_working` keys on heartbeat freshness, so it sees launcher
dispatches and misses the main agent's own verification, smoke, and style runs --
exactly the case that goes unreported.

Blocking is once-only, latched on this hook's own marker rather than on the
shared `stop_hook_active` flag. That flag is set by any stop hook that blocks, so
keying on it would silence this check for the whole stop that follows a
stop-delegate-continue block -- and that stop is exactly the one that follows a
fresh dispatch, where a missing timer is most likely. A false positive costs one
extra turn, never a wedged gate.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import cast

from context_usage import HookInput
from delegate_run import active_run

REASON = """\
Delegate run has work running ({work}) and no progress timer armed. Arm one, then \
end the turn:

  bash ~/.claude/scripts/delegate/progress_timer.sh {session_dir}

Run it with run_in_background so the tick re-invokes you. Stopping without a timer \
leaves the user with no status until the work happens to finish.

Nothing actually running? Say so in one line and end the turn — this will not \
block twice."""

# Records that this hook has already blocked the current lapse, so the retry ends
# the turn. Named for this hook alone; a shared flag would couple it to whether
# some other stop hook happened to block first.
BLOCK_MARKER = "progress_timer_hook_blocked"


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def running_work(session_dir: Path) -> str:
    """A short name for work in flight, or an empty string when idle."""
    if _text(session_dir / "impl_status") == "implementing":
        return "an implementation or fix pass"
    if _text(session_dir / "review_status") == "reviewing":
        return "a review pass"
    state_text = _text(session_dir / "progress_history_state.json")
    if state_text:
        try:
            state = cast("dict[str, object]", json.loads(state_text))
        except json.JSONDecodeError:
            return ""
        activity = state.get("activity")
        if isinstance(activity, dict):
            entry = cast("dict[str, object]", activity)
            if entry.get("status") == "active":
                label = entry.get("label")
                return str(label) if isinstance(label, str) and label else "an activity"
    return ""


def timer_pending(session_dir: Path) -> bool:
    """True when an armed timer has not yet reached its deadline.

    The marker is removed by the timer's own EXIT trap, so a leftover file means
    a killed shell rather than a live timer; the deadline check covers that too.
    """
    marker = _text(session_dir / "progress_timer")
    if not marker:
        return False
    for line in marker.splitlines():
        name, _, value = line.partition("=")
        if name == "deadline_epoch":
            try:
                return time.time() < float(value)
            except ValueError:
                return False
    return False


def main() -> None:
    payload = cast(HookInput, json.loads(sys.stdin.read()))

    if payload.get("agent_id"):
        return

    session_id = payload.get("session_id")
    if not session_id:
        return
    session_dir = active_run(session_id)
    if session_dir is None:
        return

    marker = session_dir / BLOCK_MARKER
    work = running_work(session_dir)
    if not work or timer_pending(session_dir):
        # A reported stop releases the latch, so the next lapse blocks again.
        marker.unlink(missing_ok=True)
        return
    if marker.exists():
        marker.unlink(missing_ok=True)
        return

    marker.write_text(work, encoding="utf-8")
    print(
        json.dumps(
            {
                "decision": "block",
                "reason": REASON.format(work=work, session_dir=session_dir),
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # A hook must never wedge the turn it guards.
        pass
    sys.exit(0)
