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


def last_assistant_text(transcript_path: str) -> str:
    last = ""
    try:
        with open(transcript_path) as f:
            for raw_line in f:
                try:
                    entry: TranscriptEntry = cast(
                        TranscriptEntry,
                        json.loads(raw_line),
                    )
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "assistant":
                    continue
                msg: AssistantMessage = entry.get("message", {}) or {}
                content: list[TextBlock] | str = msg.get("content", []) or []
                if isinstance(content, list):
                    parts: list[str] = [
                        b.get("text", "") or ""
                        for b in content
                        if b.get("type") == "text"
                    ]
                    text = "\n".join(p for p in parts if p)
                    if text.strip():
                        last = text
                elif content.strip():
                    last = content
    except OSError:
        return ""
    return last


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

    text = last_assistant_text(transcript_path)
    if not text:
        sys.exit(0)

    violations = find_violations(text)
    if not violations:
        sys.exit(0)

    seen: set[tuple[str, int]] = set()
    bullets: list[str] = []
    stems_in_order: list[str] = []
    for v in violations:
        key = (v.stem, v.line_no)
        if key in seen:
            continue
        seen.add(key)
        if v.stem not in stems_in_order:
            stems_in_order.append(v.stem)
        snippet = v.line[:140]
        bullets.append(
            f"  - line {v.line_no}: matched {v.match!r} (banned stem: {v.stem!r})\n      > {snippet}"
        )

    _ = bump_counters(stems_in_order)
    _ = bullets
    stems_label = ", ".join(stems_in_order)

    reason = f"⛔ banned word(s): {stems_label}"

    print(json.dumps({"decision": "block", "reason": reason}))


if __name__ == "__main__":
    main()
