#!/usr/bin/env python3
"""PostToolUse hook: block a tool call whose written text or output has a banned word.

What is scanned per tool is defined once, in tool_call_text.scan_text: text a
call removes or searches for (an Edit's old_string, the old side of a scripted
replacement or the pattern of a grep in a Bash command) is never scanned, and a
banned word in that text is not reported in the tool's output either; the rest
of the output is.

One JSON object carries the whole verdict: `decision: block` with a short
reason, so the agent must address the violation before moving on; a one-line
systemMessage for the user; and the full violation list and recovery
instructions for the agent via hookSpecificOutput.additionalContext. Local
counters are updated by the hook itself.

The hook has no matcher, so it runs after every tool call. A read-only tool
exits on its name, before the banned-word machinery is imported.
"""

import json
import sys
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from tool_call_text import HookPayload

# Tools that only read — they never author content this turn. Their output is
# just file/search content the agent is inspecting, so scanning it only produces
# false positives (a Read of a file that legitimately uses a banned term, a Grep
# whose pattern is the term itself).
READ_ONLY_TOOLS = frozenset({"Read", "Grep", "Glob", "NotebookRead", "LS"})


def main() -> None:
    try:
        data = cast("HookPayload", json.load(sys.stdin))
    except json.JSONDecodeError:
        sys.exit(0)

    tool_name: str = data.get("tool_name", "") or ""
    if tool_name in READ_ONLY_TOOLS:
        sys.exit(0)

    # Imported past the read-only exit: the banned-word machinery is most of
    # this hook's start-up time.
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    from banned_words_lib import (
        COUNTER_STATE,
        STYLE_GUIDE,
        bump_counters,
        find_violations,
        format_counter_totals,
        get_stem_guidance,
        hooks_enabled,
        is_guide_reproduction,
        is_introspection_command,
        is_read_only_command,
    )
    from tool_call_text import ToolInput, ToolResponse, scan_text

    if not hooks_enabled():
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
    file_path: str = tool_input.get("file_path", "") or tool_input.get("notebook_path", "") or ""

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

    text: str = scan_text(tool_name, tool_input, tool_response)
    if not text:
        sys.exit(0)

    violations = find_violations(text)
    if not violations:
        sys.exit(0)

    seen: set[tuple[str, int]] = set()
    bullets: list[str] = []
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
        snippet = v.line[:140]
        bullets.append(
            f"  - line {v.line_no}: matched {v.match!r} (banned stem: {v.stem!r})\n      > {snippet}"
        )

    # Skip content that reproduces the banned-word list/machinery (docs about
    # the mechanism, a copy of the guide) so it does not block on its own
    # self-reference or bump every counter.
    if is_guide_reproduction(text, len(stems_in_order)):
        sys.exit(0)

    parts = [
        f"{stem} (line{'s' if len(lines_by_stem[stem]) > 1 else ''} {', '.join(str(n) for n in lines_by_stem[stem])})"
        for stem in stems_in_order
    ]
    block = {
        "decision": "block",
        "reason": f"⛔ fix banned word(s): {', '.join(parts)}",
    }

    try:
        bumped = bump_counters(stems_in_order)
    except OSError:
        # The counter lock could not be opened. The block does not depend on
        # the counters, so it still goes out; the detail, which reports counter
        # totals, does not.
        print(json.dumps(block))
        return

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
            "  • Re-emit your ENTIRE previous output verbatim, with every banned word corrected in place — do NOT reply with only the fixed sentence or a surrounding snippet. The user needs the whole message reproduced so they don't have to splice the correction back into the original.",
            "  • Rewrite the sentence — don't just swap one word.",
            "  • If no precise substitute fits, the sentence isn't making a claim — delete it.",
            "  • Use `allow-banned: <reason>` on the line if the use is genuinely legitimate (quoting the user, naming the rule itself).",
            "  • Do NOT edit forbidden-words.md just to update counters.",
            "",
            *guidance_blocks,
            "",
            f"Local counter totals updated by the hook: {format_counter_totals(bumped)}.",
            f"Counter state file: {COUNTER_STATE}.",
            f"Style guide: {STYLE_GUIDE}.",
        ]
    )

    output = {
        **block,
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
