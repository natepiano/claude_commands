#!/usr/bin/env python3
"""PostToolUse hook: report context-window usage to the agent (model-only).

Reads the live token count from the session transcript and injects a short
line into the agent's context via `hookSpecificOutput.additionalContext`.
The user never sees it (suppressOutput), so this is cheap situational
awareness for whichever agent just ran a tool -- main thread or subagent.

Above the handoff threshold it escalates to an instruction telling the agent
to write a handoff doc before auto-compaction fires.

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
from typing import TypedDict, cast

# Auto-compaction does NOT wait for the window. Reconstructed from the 2.1.220
# binary (`Hds`/`Sfo`/`CSe`), the trigger is
#     window - min(max_output_tokens, 20_000) - 13_000
# so a 200_000 window really compacts at ~167_000. Quoting the window as the
# deadline is what let the first live test sail past the warning entirely.
OUTPUT_RESERVE_TOKENS = 20_000
PRECOMPUTE_RESERVE_TOKENS = 13_000

# How far ahead of that trigger to start asking for a handoff doc.
# 167_000 trigger - 25_000 margin = warn at 142_000.
#
# The margin covers the residual lag plus the cost of writing the doc. With the
# pending-bytes correction below, ordinary turns land within ~1k; the rare
# >30k-token jump still trails by ~13k, which this absorbs. A p05 jump (~41k)
# can still overshoot -- accepted, since covering it means warning near 60%.
HANDOFF_MARGIN_TOKENS = 25_000

# Divisor turning bytes of not-yet-billed transcript into a token estimate.
# Measured across ~20k real turns: median 4.23 bytes/token, p25 2.63. Deliberately
# below the median so the estimate errs high -- warning early costs a sentence,
# warning late costs the handoff entirely.
PENDING_BYTES_PER_TOKEN = 3.0

# Bytes of transcript tail to scan for the most recent usage record.
TAIL_BYTES = 512 * 1024
TAIL_BYTES_RETRY = 4 * 1024 * 1024

DEBUG_LOG = Path("/tmp/claude/context-hook-debug.jsonl")


class Usage(TypedDict, total=False):
    input_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    output_tokens: int


class Message(TypedDict, total=False):
    usage: Usage


class TranscriptEntry(TypedDict, total=False):
    type: str
    isSidechain: bool
    message: Message


class HookInput(TypedDict, total=False):
    session_id: str
    transcript_path: str
    hook_event_name: str
    tool_name: str
    # Present only when the hook fires inside a subagent.
    agent_id: str
    agent_type: str


class Settings(TypedDict, total=False):
    autoCompactWindow: int
    autoCompactEnabled: bool


class Reading(TypedDict):
    tokens: int
    pending_bytes: int
    is_sidechain: bool


def parse_window(text: str) -> int | None:
    """Parse `200k` / `200000` / `200` (shorthand for 200k) into tokens."""
    value = text.strip().lower()
    if not value:
        return None
    multiplier = 1
    if value.endswith("k"):
        multiplier, value = 1_000, value[:-1]
    elif value.endswith("m"):
        multiplier, value = 1_000_000, value[:-1]
    try:
        number = int(float(value) * multiplier)
    except ValueError:
        return None
    # Bare small numbers are the CLI's k-shorthand ("200" means 200k).
    if multiplier == 1 and number < 10_000:
        number *= 1_000
    return number if number > 0 else None


def auto_compact_window() -> int | None:
    """Effective auto-compact window: env override wins over settings.json."""
    from_env = os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW")
    if from_env:
        parsed = parse_window(from_env)
        if parsed is not None:
            return parsed
    settings_path = Path.home() / ".claude" / "settings.json"
    try:
        settings = cast(Settings, json.loads(settings_path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    window = settings.get("autoCompactWindow")
    return window if isinstance(window, int) and window > 0 else None


def resolve_transcript(payload: HookInput) -> Path | None:
    """The transcript belonging to the agent that just ran a tool.

    Subagents get their own file under `<session-dir>/subagents/`, but the
    hook payload's `transcript_path` always points at the main session
    transcript. Reading that from inside a subagent would report the main
    thread's usage, so a subagent with no transcript yet stays silent rather
    than quoting a number that isn't its own.
    """
    transcript = payload.get("transcript_path")
    if not transcript:
        return None
    main = Path(transcript)
    agent_id = payload.get("agent_id")
    if agent_id:
        own = main.with_suffix("") / "subagents" / f"agent-{agent_id}.jsonl"
        return own if own.is_file() else None
    return main if main.is_file() else None


def read_tail(path: Path, size: int) -> list[str]:
    """Return complete lines from the last `size` bytes of a file."""
    with path.open("rb") as handle:
        _ = handle.seek(0, os.SEEK_END)
        end = handle.tell()
        start = max(0, end - size)
        _ = handle.seek(start)
        chunk = handle.read(end - start)
    lines = chunk.decode("utf-8", errors="replace").splitlines()
    # The first line is probably truncated unless we read from byte 0.
    return lines if start == 0 else lines[1:]


def latest_reading(path: Path) -> Reading | None:
    """Tokens in the context window as of the most recent assistant turn.

    Usage records only exist for turns that have already been sent, so the raw
    number lags by one: the tool results that just landed are in the context but
    are not counted anywhere until the next request. `pending_bytes` measures
    that gap -- everything written to the transcript after the newest assistant
    turn -- so the caller can add it back in.
    """
    for size in (TAIL_BYTES, TAIL_BYTES_RETRY):
        lines = read_tail(path, size)
        for offset, line in enumerate(reversed(lines)):
            if '"usage"' not in line or '"assistant"' not in line:
                continue
            try:
                entry = cast(TranscriptEntry, json.loads(line))
            except ValueError:
                continue
            if entry.get("type") != "assistant":
                continue
            message = entry.get("message")
            if message is None:
                continue
            usage = message.get("usage")
            if usage is None:
                continue
            tokens = (
                usage.get("input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
            )
            if tokens <= 0:
                continue
            after = lines[len(lines) - offset :]
            return {
                "tokens": tokens,
                "pending_bytes": sum(len(text) + 1 for text in after),
                "is_sidechain": entry.get("isSidechain", False),
            }
    return None


def build_context(tokens: int, window: int | None, is_subagent: bool) -> str | None:
    """The line handed to the agent, or None when there is nothing to say."""
    warn_only = os.environ.get("CLAUDE_CONTEXT_HOOK_MODE", "always") == "warn"
    label = "Your context usage" if is_subagent else "Context usage"
    if window is None:
        return None if warn_only else f"{label}: {tokens:,} tokens."
    trigger = max(0, window - OUTPUT_RESERVE_TOKENS - PRECOMPUTE_RESERVE_TOKENS)
    threshold = max(0, trigger - HANDOFF_MARGIN_TOKENS)
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
        "If you are partway through a task, write a handoff doc NOW, before "
        "compaction: what you were doing, what is done, what is left, the "
        "files and decisions involved, and the exact next step. Save it to a "
        "durable file in the repo — NOT the session scratchpad, which does not "
        "survive — and state its path. Then continue working. After compaction, "
        "read it back to pick up where you left off, and delete it once you have."
    )


def log_debug(
    payload: HookInput,
    transcript: Path | None,
    reading: Reading | None,
    window: int | None,
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
    log_debug(payload, transcript, reading, window)
    if reading is None:
        return
    tokens = reading["tokens"] + int(reading["pending_bytes"] / PENDING_BYTES_PER_TOKEN)
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
