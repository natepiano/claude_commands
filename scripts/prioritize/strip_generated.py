#!/usr/bin/env python3
"""git clean filter: drop generated backlog fields from issue frontmatter.

Reads a note on stdin and writes it to stdout with the ``backlog_score`` and
``backlog_rank`` lines removed from the leading YAML frontmatter, so the derived
ranking never enters git history. The working tree keeps the fields (Obsidian's
Base views sort on ``backlog_rank``); only the committed blob is normalized.

Anything that is not a frontmatter note, or that lacks these fields, passes
through byte-for-byte.
"""

from __future__ import annotations

import sys

GENERATED_KEYS = ("backlog_score:", "backlog_rank:")


def strip_generated(data: bytes) -> bytes:
    text = data.decode("utf-8")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return data
    closing: int | None = None
    for index in range(1, len(lines)):
        if lines[index].rstrip("\r\n") == "---":
            closing = index
            break
    if closing is None:
        return data
    kept: list[str] = [
        line
        for index, line in enumerate(lines)
        if not (1 <= index < closing and line.startswith(GENERATED_KEYS))
    ]
    return "".join(kept).encode("utf-8")


def main() -> int:
    _ = sys.stdout.buffer.write(strip_generated(sys.stdin.buffer.read()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
