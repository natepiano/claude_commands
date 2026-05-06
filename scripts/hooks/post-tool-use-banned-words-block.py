#!/usr/bin/env python3
"""PostToolUse hook companion: forces a block when banned words are present.

The sibling messaging hook (post-tool-use-banned-words.py) already shows the
user a one-line systemMessage and gives the agent verbose correction guidance
via additionalContext. This hook only emits `decision: block` with a minimal
reason so the agent must address the violation before moving on. The reason
is intentionally short — the messaging hook already carried the detail.
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
    command: str
    description: str


class ToolResponse(TypedDict, total=False):
    output: str
    stdout: str
    stderr: str


class HookPayload(TypedDict, total=False):
    tool_name: str
    tool_input: ToolInput
    tool_response: ToolResponse


def extract_text(tool_name: str, tool_input: ToolInput, tool_response: ToolResponse) -> str:
    parts: list[str] = []
    if tool_name == "Write":
        parts.append(tool_input.get("content", "") or "")
    elif tool_name == "Edit":
        parts.append(tool_input.get("new_string", "") or "")
    elif tool_name == "MultiEdit":
        edits: list[EditEntry] = tool_input.get("edits", []) or []
        parts.extend(e.get("new_string", "") or "" for e in edits)
    else:
        parts.append(tool_input.get("command", "") or "")
        parts.append(tool_input.get("description", "") or "")
        parts.append(tool_input.get("content", "") or "")
        parts.append(tool_input.get("new_string", "") or "")
    parts.append(tool_response.get("output", "") or "")
    parts.append(tool_response.get("stdout", "") or "")
    parts.append(tool_response.get("stderr", "") or "")
    return "\n".join(p for p in parts if p)


def main() -> None:
    try:
        data: HookPayload = cast(HookPayload, json.load(sys.stdin))
    except json.JSONDecodeError:
        sys.exit(0)

    tool_name: str = data.get("tool_name", "") or ""
    tool_input: ToolInput = data.get("tool_input", {}) or {}
    tool_response: ToolResponse = data.get("tool_response", {}) or {}
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
        "post-tool-use-banned-words-block.py",
        "stop-banned-words.py",
        "banned-words-check/SKILL.md",
        "commands/add-banned-word.md",
    )
    if any(s in file_path for s in skip_substrings):
        sys.exit(0)

    text = extract_text(tool_name, tool_input, tool_response)
    if not text:
        sys.exit(0)

    violations = find_violations(text)
    if not violations:
        sys.exit(0)

    stems: list[str] = []
    for v in violations:
        if v.stem not in stems:
            stems.append(v.stem)

    print(json.dumps({
        "decision": "block",
        "reason": f"⛔ fix banned word(s): {', '.join(stems)}",
    }))


if __name__ == "__main__":
    main()
