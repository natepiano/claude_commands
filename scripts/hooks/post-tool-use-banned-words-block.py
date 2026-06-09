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
from banned_words_lib import (
    STYLE_GUIDE,
    find_violations,
    is_guide_reproduction,
    is_introspection_command,
    is_read_only_command,
    is_read_only_tool,
)


class EditEntry(TypedDict, total=False):
    new_string: str


class ToolInput(TypedDict, total=False):
    content: str
    cmd: str
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
        parts.append(tool_input.get("cmd", "") or "")
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
    if is_read_only_tool(tool_name):
        sys.exit(0)
    # Some MCP tools deliver `tool_input`/`tool_response` as a string instead
    # of a dict — defend against that before calling .get() on them.
    raw_tool_input: object = data.get("tool_input", {})
    tool_input: ToolInput = (
        cast(ToolInput, raw_tool_input) if isinstance(raw_tool_input, dict) else ToolInput()
    )
    raw_tool_response: object = data.get("tool_response", {})
    tool_response: ToolResponse = (
        cast(ToolResponse, raw_tool_response)
        if isinstance(raw_tool_response, dict)
        else ToolResponse()
    )
    file_path: str = tool_input.get("file_path", "") or ""

    if file_path:
        try:
            if Path(file_path).resolve() == STYLE_GUIDE.resolve():
                sys.exit(0)
        except OSError:
            pass

    skip_substrings = ("commands/add-banned-word.md", "commands/add_banned_word.md")
    if any(s in file_path for s in skip_substrings):
        sys.exit(0)

    command = tool_input.get("command", "") or tool_input.get("cmd", "") or ""
    if is_introspection_command(command) or is_read_only_command(command):
        sys.exit(0)

    if (
        "Counter state:" in (tool_response.get("output", "") or "")
        and "forbidden-word-counts.json" in (tool_response.get("output", "") or "")
    ):
        sys.exit(0)

    text = extract_text(tool_name, tool_input, tool_response)
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

    # Skip content that reproduces the banned-word list/machinery so it does
    # not block on its own self-reference.
    if is_guide_reproduction(text, len(stems_in_order)):
        sys.exit(0)

    parts = [
        f"{stem} (line{'s' if len(lines_by_stem[stem]) > 1 else ''} {', '.join(str(n) for n in lines_by_stem[stem])})"
        for stem in stems_in_order
    ]

    print(json.dumps({
        "decision": "block",
        "reason": f"⛔ fix banned word(s): {', '.join(parts)}",
    }))


if __name__ == "__main__":
    main()
