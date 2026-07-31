#!/usr/bin/env python3
"""PostToolUse hook: report context-window usage to the agent (model-only).

Reads the live token count from the session transcript and injects a short
line into the agent's context via `hookSpecificOutput.additionalContext`.
The user never sees it (suppressOutput), so this is cheap situational
awareness for whichever agent just ran a tool -- main thread or subagent.

Above the handoff threshold it escalates to an instruction telling the agent
to write a handoff doc before auto-compaction fires. The measurement itself
lives in `context_usage.py`, shared with `stop-delegate-continue.py` so the
two hooks cannot drift onto different thresholds.

Env knobs:
  CLAUDE_CONTEXT_HOOK_MODE   always (default) | warn -- `warn` stays silent
                             until the handoff threshold is crossed.
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
    resolve_transcript,
    response_bytes,
    trigger_tokens,
)

DEBUG_LOG = Path("/tmp/claude/context-hook-debug.jsonl")


def build_context(tokens: int, window: int | None, is_subagent: bool) -> str | None:
    """The line handed to the agent, or None when there is nothing to say."""
    warn_only = os.environ.get("CLAUDE_CONTEXT_HOOK_MODE", "always") == "warn"
    label = "Your context usage" if is_subagent else "Context usage"
    if window is None:
        return None if warn_only else f"{label}: {tokens:,} tokens."
    trigger = trigger_tokens(window)
    threshold = handoff_threshold(window)
    percent = round(100 * tokens / trigger) if trigger else 100
    if tokens < threshold:
        if warn_only:
            return None
        return f"{label}: {tokens:,} / {trigger:,} tokens ({percent}%)."
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
        "read it back to pick up where you left off, and delete it once you have."
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
    window = auto_compact_window()
    reading = None if transcript is None else latest_reading(transcript)
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
