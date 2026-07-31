#!/usr/bin/env python3
"""Shared context-window accounting for the context-usage hooks.

Two hooks need the same answer to "how full is this agent's context right now":
`post-tool-use-context-usage.py`, which reports it after every tool call, and
`stop-delegate-continue.py`, which refuses to let a delegate run end its turn
near the auto-compaction trigger. The measurement lives here so the two can
never drift onto different thresholds.
"""

from __future__ import annotations

import json
import os
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

# Same idea for the just-landed tool result, but a smaller divisor: the payload
# holds the raw result, while what reaches the context is the formatted version
# (line-number prefixes, JSON escaping, truncation notices, system reminders).
# Measured on a 1,388-line source read: 48,622 payload bytes became 109,632
# transcript bytes, a 2.2x expansion. Reusing 3.0 credited 16k of a 36.5k read.
RESPONSE_BYTES_PER_TOKEN = 1.5

# Bytes of transcript tail to scan for the most recent usage record.
TAIL_BYTES = 512 * 1024
TAIL_BYTES_RETRY = 4 * 1024 * 1024


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
    # The result the tool just produced. Typed `object` rather than a concrete
    # shape because every tool returns something different; it is only ever
    # measured, never inspected.
    tool_response: object
    # Present only when the hook fires inside a subagent.
    agent_id: str
    agent_type: str
    # Stop only: true when this turn exists because a previous Stop hook
    # blocked. Blocking again would wedge the agent in a loop.
    stop_hook_active: bool


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


def trigger_tokens(window: int) -> int:
    """Token count at which auto-compaction actually fires."""
    return max(0, window - OUTPUT_RESERVE_TOKENS - PRECOMPUTE_RESERVE_TOKENS)


def handoff_threshold(window: int) -> int:
    """Token count at which the agent should be writing a handoff doc."""
    return max(0, trigger_tokens(window) - HANDOFF_MARGIN_TOKENS)


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


def response_bytes(payload: HookInput) -> int:
    """Size of the tool result that just landed.

    PostToolUse runs before the result reaches the transcript, so it appears
    neither in the usage record nor in `pending_bytes`. Measuring it here is
    what keeps one large read from going unnoticed until the next tool call.

    Stop carries no tool result, so this returns 0 there.
    """
    response = payload.get("tool_response")
    if response is None:
        return 0
    return len(json.dumps(response, default=str))


def estimate_tokens(reading: Reading, response: int) -> int:
    """Billed tokens plus the two corrections for what has not been billed yet."""
    return (
        reading["tokens"]
        + int(reading["pending_bytes"] / PENDING_BYTES_PER_TOKEN)
        + int(response / RESPONSE_BYTES_PER_TOKEN)
    )


def measure(payload: HookInput) -> int | None:
    """Current context usage for the agent this hook fired in, or None."""
    transcript = resolve_transcript(payload)
    if transcript is None:
        return None
    reading = latest_reading(transcript)
    if reading is None:
        return None
    return estimate_tokens(reading, response_bytes(payload))
