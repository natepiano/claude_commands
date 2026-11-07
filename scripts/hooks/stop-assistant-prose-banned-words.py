#!/usr/bin/env python3
"""Stop hook: log banned words in the assistant's just-emitted message.

The hook never blocks the reply and never asks for a rewrite. For each banned
word it appends one line to `PROSE_LOG` and bumps the local counters. It prints
nothing. A failure to write the log or the counters does not fail the hook.

Each log line has four tab-separated fields: the local time with its offset,
the session id, the stem, and an excerpt of the line that holds the match. The
hook writes one line per stem per text line.

The hook does not read `stop_hook_active`. It never blocks, so it cannot cause
a loop, and a turn that another Stop hook blocked must still be logged.

The Stop event payload includes `last_assistant_message` — the full text of the
turn that just ended. Use it directly. Reading from `transcript_path` would
race the JSONL flush: at Stop time the assistant entry has not been written
yet, so the file scan returns an empty string and the hook silently passes.
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import TypedDict, cast

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from user_zone import user_zone  # noqa: E402
from banned_words_lib import (
    PROSE_LOG,
    Violation,
    bump_counters,
    find_violations,
    hooks_enabled,
    is_guide_reproduction,
)

EXCERPT_LIMIT = 120


class StopPayload(TypedDict, total=False):
    last_assistant_message: str
    session_id: str


def excerpt(line: str, match: str) -> str:
    """Return at most `EXCERPT_LIMIT` characters of `line` with `match` in view.

    Each run of whitespace becomes one space, so the excerpt has no tab or
    newline. A long line is cut to a window around the match.
    """
    text = " ".join(line.split())
    if len(text) <= EXCERPT_LIMIT:
        return text
    needle = " ".join(match.split())
    found = text.lower().find(needle.lower())
    if found < 0:
        return text[:EXCERPT_LIMIT]
    margin = max(0, (EXCERPT_LIMIT - len(needle)) // 2)
    start = max(0, min(found - margin, len(text) - EXCERPT_LIMIT))
    return text[start : start + EXCERPT_LIMIT]


def append_log(session_id: str, violations: list[Violation]) -> None:
    """Append one tab-separated line per violation to `PROSE_LOG`."""
    stamp = datetime.now(user_zone()).isoformat(timespec="seconds")
    lines = "".join(
        f"{stamp}\t{session_id}\t{v.stem}\t{excerpt(v.line, v.match)}\n" for v in violations
    )
    PROSE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with PROSE_LOG.open("a", encoding="utf-8") as log:
        _ = log.write(lines)


def main() -> None:
    if not hooks_enabled():
        sys.exit(0)

    try:
        data: StopPayload = cast(StopPayload, json.load(sys.stdin))
    except json.JSONDecodeError:
        sys.exit(0)

    text: str = data.get("last_assistant_message", "") or ""
    if not text:
        sys.exit(0)

    seen: set[tuple[str, int]] = set()
    unique: list[Violation] = []
    for v in find_violations(text):
        key = (v.stem, v.line_no)
        if key not in seen:
            seen.add(key)
            unique.append(v)
    if not unique:
        sys.exit(0)

    stems = list(dict.fromkeys(v.stem for v in unique))
    # Skip messages that reproduce the banned-word list/machinery (analysis
    # reports, mechanism explanations, echoes of the loaded guide) so a single
    # such message does not bump every counter at once.
    if is_guide_reproduction(text, len(stems)):
        sys.exit(0)

    try:
        append_log(data.get("session_id", "") or "", unique)
    except (OSError, ValueError):
        pass
    try:
        _ = bump_counters(stems)
    except OSError:
        pass


if __name__ == "__main__":
    main()
