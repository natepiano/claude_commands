#!/usr/bin/env python3
"""Stop hook: scan the assistant's just-emitted message for banned words.

If found, block with a reason so the agent must rewrite before ending the turn.
Honors `stop_hook_active` to avoid infinite loops.

The Stop event payload includes `last_assistant_message` — the full text of the
turn that just ended. Use it directly. Reading from `transcript_path` would
race the JSONL flush: at Stop time the assistant entry has not been written
yet, so the file scan returns an empty string and the hook silently passes.
"""

import json
import sys
from pathlib import Path
from typing import TypedDict, cast

sys.path.insert(0, str(Path(__file__).parent))
from banned_words_lib import (
    bump_counters,
    find_violations,
    format_counter_totals,
    is_guide_reproduction,
)


class StopPayload(TypedDict, total=False):
    last_assistant_message: str
    stop_hook_active: bool


def main() -> None:
    try:
        data: StopPayload = cast(StopPayload, json.load(sys.stdin))
    except json.JSONDecodeError:
        sys.exit(0)

    if data.get("stop_hook_active"):
        sys.exit(0)

    text: str = data.get("last_assistant_message", "") or ""
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

    # Skip messages that reproduce the banned-word list/machinery (analysis
    # reports, mechanism explanations, echoes of the loaded guide) so a single
    # such message does not bump every counter at once.
    if is_guide_reproduction(text, len(stems_in_order)):
        sys.exit(0)

    bumped = bump_counters(stems_in_order)
    parts = [
        f"{stem} (line{'s' if len(lines_by_stem[stem]) > 1 else ''} {', '.join(str(n) for n in lines_by_stem[stem])})"
        for stem in stems_in_order
    ]
    flagged = ", ".join(parts)
    # A Stop hook has no model-only output channel — its only text fields are
    # `reason`, `systemMessage`, and `stopReason`, and `reason` is shown to both
    # the user and the model (there is no `hookSpecificOutput.additionalContext`
    # for Stop; that field is rejected by the schema). So `reason` carries the
    # short summary the user wants — which word fired and the running local
    # totals — and the verbose re-emit protocol lives as a standing agent
    # instruction (CLAUDE.md / the forbidden-words guide), not per-fire text.
    reason = f"⛔ banned word(s): {flagged}. Local totals: {format_counter_totals(bumped)}"

    print(json.dumps({"decision": "block", "reason": reason}))


if __name__ == "__main__":
    main()
