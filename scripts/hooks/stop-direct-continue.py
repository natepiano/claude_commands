#!/usr/bin/env python3
"""Stop hook: keep a /unit:direct run going across auto-compaction.

Auto-compaction fires on the *next request*, never mid-turn. So an agent that
ends its turn near the limit is doing the one thing that guarantees compaction
never runs -- the run just sits there waiting for the user. `direct.md` tells
the agent not to do this, but prose loses at 85% context.

This hook fires right before the response concludes and returns
`decision: block`, which refuses the stop and hands the agent a reason. The
continuation is itself a new request, which is what actually lets compaction
happen.

It stays out of the way unless all of these hold:
  * a /unit:direct run is active in this session (marker file)
  * context is at or past the same handoff threshold the PostToolUse hook uses
  * this turn is not already the product of a block (`stop_hook_active`)
  * the agent is the main thread, not a subagent
  * the agent has not declared a user or supplier wait
  * no live entry in the payload's `background_tasks` will wake the session.
    That wake-up supplies the next request, so compaction needs no block.

Context is measured before the wake-source check. Below the handoff threshold,
or when context cannot be measured, the stop passes. A fresh worker heartbeat
does not prove a future wake-up: a finished worker writes it too.

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
from pathlib import Path
from typing import cast

from context_usage import (
    HookInput,
    auto_compact_window,
    handoff_threshold,
    measure,
    pending_background_tasks,
    trigger_tokens,
)
from direct_run import active_run

REASON = """\
/unit:direct run active, {tokens:,} / {trigger:,} tokens ({percent}%). Do not end the \
turn — compaction fires on the next request, never mid-turn, so stopping here is \
what prevents it. Take the next workflow action and let it fire underneath you; \
re-read ~/.claude/commands/unit/direct.md afterwards.

Waiting on a user decision instead? State it in one line and end the turn — this \
will not block twice."""


class StopPayload(HookInput, total=False):
    last_assistant_message: str


def build_reason(tokens: int, trigger: int) -> str:
    percent = round(100 * tokens / trigger) if trigger else 100
    return REASON.format(tokens=tokens, trigger=trigger, percent=percent)


def main() -> None:
    payload = cast(StopPayload, json.loads(sys.stdin.read()))

    # Subagents do not orchestrate /unit:direct runs; their stop is a real result.
    if payload.get("agent_id"):
        return

    session_id = payload.get("session_id")
    if not session_id:
        return
    session_dir = active_run(session_id)
    if session_dir is None:
        return
    # A declared wait is a real stop, including a restated decision after a
    # compaction block. Record before the compaction and background-work guards.
    final_message = payload.get("last_assistant_message", "")
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.direct.progress_history import record_turn_wait

    if record_turn_wait(session_dir, session_id, final_message):
        return
    # Already continuing because of a previous block -- let this one through.
    if payload.get("stop_hook_active"):
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

    # Only live tasks reported by the CLI supply a later request. A worker's
    # heartbeat can be fresh even after its completion notice has arrived.
    if pending_background_tasks(payload):
        return

    print(json.dumps({"decision": "block", "reason": build_reason(tokens, trigger_tokens(window))}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # A hook must never wedge the turn it guards.
        pass
    sys.exit(0)
