#!/usr/bin/env python3
"""PostToolUse hook: check Write/Edit/MultiEdit content for banned words.

Emits decision=block with a reason so the agent gets the feedback inline.
"""

import json
import sys
from pathlib import Path
from typing import TypedDict, cast

sys.path.insert(0, str(Path(__file__).parent))
from banned_words_lib import STYLE_GUIDE, find_violations


class EditEntry(TypedDict, total=False):
    new_string: str


class ToolInput(TypedDict, total=False):
    content: str
    new_string: str
    edits: list[EditEntry]
    file_path: str


class HookPayload(TypedDict, total=False):
    tool_name: str
    tool_input: ToolInput


def extract_text(tool_name: str, tool_input: ToolInput) -> str:
    if tool_name == "Write":
        return tool_input.get("content", "") or ""
    if tool_name == "Edit":
        return tool_input.get("new_string", "") or ""
    if tool_name == "MultiEdit":
        edits: list[EditEntry] = tool_input.get("edits", []) or []
        parts: list[str] = [e.get("new_string", "") or "" for e in edits]
        return "\n".join(parts)
    return ""


def main() -> None:
    try:
        data: HookPayload = cast(HookPayload, json.load(sys.stdin))
    except json.JSONDecodeError:
        sys.exit(0)

    tool_name: str = data.get("tool_name", "") or ""
    tool_input: ToolInput = data.get("tool_input", {}) or {}
    file_path: str = tool_input.get("file_path", "") or ""

    if file_path:
        try:
            if Path(file_path).resolve() == STYLE_GUIDE.resolve():
                sys.exit(0)
        except OSError:
            pass

    skip_substrings = (
        "banned_words_lib.py",
        "post-tool-use-banned-words.py",
        "stop-banned-words.py",
        "banned-words-check/SKILL.md",
        "commands/add-banned-word.md",
    )
    if any(s in file_path for s in skip_substrings):
        sys.exit(0)

    text: str = extract_text(tool_name, tool_input)
    if not text:
        sys.exit(0)

    violations = find_violations(text)
    if not violations:
        sys.exit(0)

    seen: set[tuple[str, int]] = set()
    bullets: list[str] = []
    for v in violations:
        key = (v.stem, v.line_no)
        if key in seen:
            continue
        seen.add(key)
        snippet = v.line[:140]
        bullets.append(
            f"  - line {v.line_no}: matched {v.match!r} (banned stem: {v.stem!r})\n      > {snippet}"
        )

    reason = "\n".join(
        [
            f"BANNED WORDS DETECTED in {tool_name} to {file_path or '(unknown path)'}.",
            f"Source of truth: {STYLE_GUIDE}",
            "Recovery: invoke the `banned-words-check` skill (Skill tool) for the full mechanism + fix path.",
            "Per-line override: append `allow-banned: <reason>` to a legitimate use.",
            "",
            "Violations:",
            *bullets,
            "",
            "Action: edit the file to substitute or delete each match. The user had to be reminded — bump the counter for each stem in the style guide as part of the fix.",
        ]
    )

    output = {
        "continue": True,
        "systemMessage": f"⛔ banned words in {file_path or tool_name} — agent has been notified",
        "decision": "block",
        "reason": reason,
    }
    print(json.dumps(output))


if __name__ == "__main__":
    main()
