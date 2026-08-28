#!/usr/bin/env python3
"""Stop hook: keep a /plan:delegate run going across auto-compaction.

Auto-compaction fires on the *next request*, never mid-turn. So an agent that
ends its turn near the limit is doing the one thing that guarantees compaction
never runs -- the run just sits there waiting for the user. `delegate.md` tells
the agent not to do this, but prose loses at 85% context.

This hook fires right before the response concludes and returns
`decision: block`, which refuses the stop and hands the agent a reason. The
continuation is itself a new request, which is what actually lets compaction
happen.

It stays out of the way unless all of these hold:
  * a /plan:delegate run is active in this session (marker file)
  * context is at or past the same handoff threshold the PostToolUse hook uses
  * this turn is not already the product of a block (`stop_hook_active`)
  * the agent is the main thread, not a subagent
  * nothing is in flight that will wake the session by itself -- a live delegate
    dispatch, or anything in the payload's `background_tasks`. That wake-up is
    the next request, so compaction gets its chance without a block

Blocking is deliberately once-only. The agent legitimately stops above the
threshold to ask the user things -- verbose pre/post-phase gates, unresolved
pending decisions, review conflicts, the fix-pass cap -- and a hook cannot tell
those from giving up. So it blocks once and says how to stop for real; the
retry sets `stop_hook_active` and sails through. The cost of a false positive is
one restated question, not a wedged-shut gate.

The `reason` is screen text as well as steering text: Stop is not one of the
events that support model-only `additionalContext`, and the blocked-stop path
renders the reason verbatim under a hardcoded "Stop hook error:" label that
`suppressOutput` does not reach. Keep it short for that reason alone.
"""

from __future__ import annotations

import json
import sys
from typing import cast

from context_usage import (
    HookInput,
    auto_compact_window,
    handoff_threshold,
    measure,
    pending_background_tasks,
    trigger_tokens,
)
from delegate_run import active_run, delegate_working

REASON = """\
Delegate run active, {tokens:,} / {trigger:,} tokens ({percent}%). Do not end the \
turn — compaction fires on the next request, never mid-turn, so stopping here is \
what prevents it. Take the next workflow action and let it fire underneath you; \
re-read ~/.claude/commands/plan/delegate.md afterwards.

Waiting on a user decision instead? State it in one line and end the turn — this \
will not block twice."""


def build_reason(tokens: int, trigger: int) -> str:
    percent = round(100 * tokens / trigger) if trigger else 100
    return REASON.format(tokens=tokens, trigger=trigger, percent=percent)


def main() -> None:
    payload = cast(HookInput, json.loads(sys.stdin.read()))

    # Already continuing because of a previous block -- let this one through.
    if payload.get("stop_hook_active"):
        return
    # Subagents do not orchestrate delegate runs; their stop is a real result.
    if payload.get("agent_id"):
        return

    session_id = payload.get("session_id")
    if not session_id:
        return
    session_dir = active_run(session_id)
    if session_dir is None:
        return
    # Parked on a completion waiter: the delegate's own notification will supply
    # the next request, and compaction fires there. Nothing to force.
    if delegate_working(session_dir):
        return
    # The same situation, read from the CLI instead of from a delegate
    # heartbeat: anything in `background_tasks` wakes this session on its own,
    # and that wake-up is the request compaction needs. The heartbeat covers
    # delegate dispatches only, so a run waiting on ordinary Agent-tool
    # subagents looked idle to `delegate_working` and got ordered back to work
    # with nothing to do -- a blocked stop, a restated "still waiting", and no
    # compaction, since the count was nowhere near the trigger.
    if pending_background_tasks(payload):
        return

    # Measure first: the window is clamped to the model's own context window, and
    # only the transcript names the model.
    measurement = measure(payload)
    if measurement is None:
        return
    window = auto_compact_window(measurement["model"])
    if window is None:
        return
    tokens = measurement["tokens"]
    if tokens < handoff_threshold(window):
        return

    print(json.dumps({"decision": "block", "reason": build_reason(tokens, trigger_tokens(window))}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # A hook must never wedge the turn it guards.
        pass
    sys.exit(0)
