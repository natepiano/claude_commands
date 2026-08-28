#!/usr/bin/env python3
"""PostToolUse hook: report context-window usage to the agent (model-only).

Reads the live token count from the session transcript and injects a short
line into the agent's context via `hookSpecificOutput.additionalContext`.
The user never sees it (suppressOutput), so this is cheap situational
awareness for whichever agent just ran a tool -- main thread or subagent.

It stays quiet until the count is worth acting on. Below the notice threshold
it says nothing, between notice and handoff it reports the count together with
the point a handoff is actually requested, and at or above the handoff
threshold it escalates to the instruction to write one. All three points are
derived from the configured window in `context_usage.py`, shared with
`stop-delegate-continue.py` so the two hooks cannot drift apart.

The escalation also names the one case where stopping is the right move: an
agent with nothing left to do but wait on background work. Its next request is
the wake-up, which compacts on its own, so this hook's "do not end your turn"
must not talk the agent out of a stop it should take.

Env knobs:
  CLAUDE_CONTEXT_HOOK_MODE   dynamic (default) | always | warn.
                             `dynamic` reports from the notice threshold on,
                             `always` reports after every tool call from token
                             one, `warn` stays silent until the handoff
                             threshold is crossed.
  CLAUDE_CONTEXT_HOOK_DEBUG  0 to disable the debug log (default on).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import cast

from context_usage import (
    OUTPUT_RESERVE_TOKENS,
    PRECOMPUTE_RESERVE_TOKENS,
    HookInput,
    Reading,
    auto_compact_window,
    estimate_tokens,
    handoff_threshold,
    latest_reading,
    notice_threshold,
    resolve_transcript,
    response_bytes,
    trigger_tokens,
)

DEBUG_LOG = Path("/tmp/claude/context-hook-debug.jsonl")


def build_context(tokens: int, window: int | None, is_subagent: bool) -> str | None:
    """The line handed to the agent, or None when there is nothing to say."""
    mode = os.environ.get("CLAUDE_CONTEXT_HOOK_MODE", "dynamic")
    label = "Your context usage" if is_subagent else "Context usage"
    if window is None:
        return f"{label}: {tokens:,} tokens." if mode == "always" else None
    trigger = trigger_tokens(window)
    threshold = handoff_threshold(window)
    percent = round(100 * tokens / trigger) if trigger else 100
    if tokens < threshold:
        if mode == "warn":
            return None
        if mode != "always" and tokens < notice_threshold(window):
            return None
        # Naming the request point is the reason this band exists at all. A bare
        # percentage states a problem and no policy, so the agent supplies the
        # missing policy itself: three sessions settled on ~150k off a "67%",
        # 50k before anything had asked them for a handoff.
        action = "wrap up and return results" if is_subagent else "write a handoff doc"
        return (
            f"{label}: {tokens:,} / {trigger:,} tokens ({percent}%). "
            f"No action needed yet — you are asked to {action} at "
            f"{threshold:,} tokens, not before. Keep working."
        )
    if is_subagent:
        return (
            f"{label}: {tokens:,} / {trigger:,} tokens ({percent}%) — you are "
            f"close to the {trigger:,} auto-compact trigger. Stop expanding scope. "
            "Finish the current step and return your results now, including "
            "what you completed, what you did not get to, and the exact next "
            "step someone else would take."
        )
    return (
        f"{label}: {tokens:,} / {trigger:,} tokens ({percent}%) — "
        f"auto-compaction fires at {trigger:,}, not at the {window:,} window "
        f"({OUTPUT_RESERVE_TOKENS + PRECOMPUTE_RESERVE_TOKENS:,} is held back). "
        "This is not a reason to stop or to end your turn: compaction only "
        "happens on the next request, so ending the turn here is what prevents "
        "it. Keep working straight through it. "
        "If you are partway through a task, write a handoff doc first: what you "
        "were doing, what is done, what is left, the files and decisions "
        "involved, and the exact next step. Save it to a durable file in the "
        "repo — NOT the session scratchpad, which does not survive — and state "
        "its path. Then proceed directly to your next action. After compaction, "
        "read it back to pick up where you left off, and delete it once you have. "
        "One exception to the rule against stopping: if you have nothing left to "
        "do but wait on subagents or other background work, end the turn. The "
        "wake-up is a request like any other, so compaction fires there without "
        "you holding the turn open for it."
    )


def log_debug(
    payload: HookInput,
    transcript: Path | None,
    reading: Reading | None,
    window: int | None,
    response: int,
) -> None:
    if os.environ.get("CLAUDE_CONTEXT_HOOK_DEBUG") == "0":
        return
    record = {
        "session_id": payload.get("session_id"),
        "agent_id": payload.get("agent_id"),
        "agent_type": payload.get("agent_type"),
        "tool_name": payload.get("tool_name"),
        "transcript_read": None if transcript is None else str(transcript),
        "payload_keys": sorted(payload.keys()),
        "tokens": None if reading is None else reading["tokens"],
        "pending_bytes": None if reading is None else reading["pending_bytes"],
        "is_sidechain": None if reading is None else reading["is_sidechain"],
        "model": None if reading is None else reading["model"],
        "window": window,
        "response_bytes": response,
    }
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG.open("a", encoding="utf-8") as handle:
            _ = handle.write(json.dumps(record) + "\n")
    except OSError:
        pass


def main() -> None:
    payload = cast(HookInput, json.loads(sys.stdin.read()))
    transcript = resolve_transcript(payload)
    reading = None if transcript is None else latest_reading(transcript)
    # The window depends on the model, and only the transcript names it, so this
    # has to come after the reading.
    window = auto_compact_window(None if reading is None else reading["model"])
    response = response_bytes(payload)
    log_debug(payload, transcript, reading, window, response)
    if reading is None:
        return
    tokens = estimate_tokens(reading, response)
    context = build_context(tokens, window, bool(payload.get("agent_id")))
    if context is None:
        return
    print(
        json.dumps(
            {
                "suppressOutput": True,
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": context,
                },
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # A hook must never break the tool it follows.
        pass
    sys.exit(0)
