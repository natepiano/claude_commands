#!/usr/bin/env python3
"""Stop hook: scan the most recent assistant text for banned words.

If found, block with a reason so the agent must rewrite before ending the turn.
Honors `stop_hook_active` to avoid infinite loops.
"""

import json
import sys
from pathlib import Path
from typing import TypedDict, cast

sys.path.insert(0, str(Path(__file__).parent))
from banned_words_lib import bump_counters, find_violations


class TextBlock(TypedDict, total=False):
    type: str
    text: str


class AssistantMessage(TypedDict, total=False):
    content: list[TextBlock] | str


class TranscriptEntry(TypedDict, total=False):
    type: str
    message: AssistantMessage


class StopPayload(TypedDict, total=False):
    transcript_path: str
    stop_hook_active: bool


def current_turn_assistant_text(transcript_path: str) -> str:
    """Return only the assistant text produced in the current turn.

    Scope is everything after the last non-assistant entry (user / tool result).
    If no assistant text has been written in the current turn yet — e.g. the
    JSONL flush is racing the Stop hook — return "" so the hook exits cleanly
    instead of blocking on stale text from a prior turn.
    """
    entries: list[TranscriptEntry] = []
    try:
        with open(transcript_path) as f:
            for raw_line in f:
                try:
                    entries.append(cast(TranscriptEntry, json.loads(raw_line)))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return ""

    last_non_assistant = -1
    for i, e in enumerate(entries):
        if e.get("type") != "assistant":
            last_non_assistant = i

    parts: list[str] = []
    for e in entries[last_non_assistant + 1:]:
        if e.get("type") != "assistant":
            continue
        msg: AssistantMessage = e.get("message", {}) or {}
        content: list[TextBlock] | str = msg.get("content", []) or []
        if isinstance(content, list):
            for b in content:
                if b.get("type") == "text":
                    t = b.get("text", "") or ""
                    if t:
                        parts.append(t)
        elif content.strip():
            parts.append(content)
    return "\n".join(parts)


def main() -> None:
    try:
        data: StopPayload = cast(StopPayload, json.load(sys.stdin))
    except json.JSONDecodeError:
        sys.exit(0)

    if data.get("stop_hook_active"):
        sys.exit(0)

    transcript_path: str = data.get("transcript_path", "") or ""
    if not transcript_path:
        sys.exit(0)

    text = current_turn_assistant_text(transcript_path)
    if not text:
        sys.exit(0)

    violations = find_violations(text)
    if not violations:
        sys.exit(0)

    seen: set[tuple[str, int]] = set()
    stems_in_order: list[str] = []
    lines_by_stem: dict[str, list[int]] = {}
    for v in violations:
        key = (v.stem, v.line_no)
        if key in seen:
            continue
        seen.add(key)
        if v.stem not in stems_in_order:
            stems_in_order.append(v.stem)
            lines_by_stem[v.stem] = []
        lines_by_stem[v.stem].append(v.line_no)

    _ = bump_counters(stems_in_order)
    parts = [
        f"{stem} (line{'s' if len(lines_by_stem[stem]) > 1 else ''} {', '.join(str(n) for n in lines_by_stem[stem])})"
        for stem in stems_in_order
    ]
    reason = f"⛔ banned word(s): {', '.join(parts)}"

    print(json.dumps({"decision": "block", "reason": reason}))


if __name__ == "__main__":
    main()
