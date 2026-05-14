#!/usr/bin/env python3
"""PostToolUse hook: check Write/Edit/MultiEdit content for banned words.

User sees a one-line systemMessage; agent sees the full violation list and
recovery instructions via hookSpecificOutput.additionalContext. Local counters
are updated by the hook itself.
"""

import json
import sys
from pathlib import Path
from typing import TypedDict, cast

sys.path.insert(0, str(Path(__file__).parent))
from banned_words_lib import (
    COUNTER_STATE,
    STYLE_GUIDE,
    bump_counters,
    find_violations,
    format_counter_totals,
    get_stem_guidance,
    is_introspection_command,
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

    skip_substrings = (
        "banned-words-check/SKILL.md",
        "commands/add-banned-word.md",
    )
    if any(s in file_path for s in skip_substrings):
        sys.exit(0)

    command = tool_input.get("command", "") or tool_input.get("cmd", "") or ""
    if is_introspection_command(command):
        sys.exit(0)

    if (
        "Counter state:" in (tool_response.get("output", "") or "")
        and "forbidden-word-counts.json" in (tool_response.get("output", "") or "")
    ):
        sys.exit(0)

    text: str = extract_text(tool_name, tool_input, tool_response)
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

    bumped = bump_counters(stems_in_order)

    short_file = Path(file_path).name if file_path else tool_name
    stems_label = ", ".join(stems_in_order)
    system_msg = f"⛔ banned word(s) [{stems_label}] in {short_file} — local counter(s) updated"

    guidance_blocks: list[str] = []
    for stem in stems_in_order:
        body = get_stem_guidance(stem)
        if body:
            guidance_blocks.append(f"=== rule for '{stem}' ===\n{body}")

    additional_context = "\n".join(
        [
            f"BANNED WORDS DETECTED in {tool_name} to {file_path or '(unknown path)'}.",
            "",
            "Violations:",
            *bullets,
            "",
            "How to correct your behavior:",
            "  • Rewrite the sentence — don't just swap one word.",
            "  • If no precise substitute fits, the sentence isn't making a claim — delete it.",
            "  • Use `allow-banned: <reason>` on the line if the use is genuinely legitimate (quoting the user, naming the rule itself).",
            "  • Do NOT edit forbidden-words.md just to update counters.",
            "",
            *guidance_blocks,
            "",
            f"Local counter totals updated by the hook: {format_counter_totals(bumped)}.",
            f"Counter state file: {COUNTER_STATE}.",
            f"Style guide: {STYLE_GUIDE}. Skill: `banned-words-check` for full mechanism.",
        ]
    )

    output = {
        "continue": True,
        "systemMessage": system_msg,
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": additional_context,
        },
    }
    print(json.dumps(output))


if __name__ == "__main__":
    main()
