#!/usr/bin/env python3
"""Print the last blocks of a saved Claude or Codex conversation.

Usage: catch_up.py NATIVE_ID [--blocks N] [--chars N]

NATIVE_ID is `session:<id>`, `codex:<id>` or an id alone. An id alone selects the
newest transcript of either family. A block is one user message, assistant message,
tool call or tool result. A tool block is one shortened line. A Claude compaction
summary is one block. A Codex compaction summary is encrypted, so only its time shows.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, tzinfo
import json
from pathlib import Path
import re
import sys
from typing import cast

from scripts.lib import transcripts

WINDOW_BYTES = 8 * 1024 * 1024
TOOL_CHARS = 200
CODEX_FOLDERS = ("sessions", "archived_sessions")


@dataclass(frozen=True)
class Block:
    at: str
    label: str
    text: str
    one_line: bool = False


class NoSavedTranscript(LookupError):
    """No account on this machine holds a transcript for the native id."""


def find_transcript(native_id: str) -> Path:
    """Find the newest saved transcript; ValueError names an address that is not a native id."""
    from scripts.message import session_records

    kind, _, identity = native_id.rpartition(":")
    if kind not in ("", "session", "codex") or not re.fullmatch(r"[\w-]+", identity):
        raise ValueError(f"{native_id!r} is not session:<id>, codex:<id> or an id")
    paths: list[Path] = []
    if kind != "codex":
        paths.extend(session_records.transcript_locations(identity))
    if kind != "session":
        paths.extend(path for home in transcripts.codex_transcript_homes() for folder in CODEX_FOLDERS
                     for path in transcripts.transcript_paths(home / folder, f"**/*{identity}.jsonl"))
    if not paths:
        raise NoSavedTranscript(f"no saved transcript for {native_id}")
    return max(transcripts.distinct_files(paths), key=lambda path: path.stat().st_mtime)


def _fields(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _parts(value: object) -> list[dict[str, object]]:
    return [_fields(part) for part in cast(list[object], value)] if isinstance(value, list) else []


def _text(content: object, kinds: tuple[str, ...]) -> str:
    """Join the text parts of one message."""
    if isinstance(content, str):
        return content
    return "\n".join(text for part in _parts(content)
                     if part.get("type") in kinds and isinstance(text := part.get("text"), str))


def _codex_blocks(at: str, kind: object, payload: dict[str, object]) -> Iterator[Block]:
    if kind == "compacted":
        yield Block(at, "compaction", "The Codex summary is encrypted.", one_line=True)
        return
    item = payload.get("type")
    if item == "message" and payload.get("role") in ("user", "assistant"):
        yield Block(at, str(payload["role"]), _text(payload.get("content"), ("input_text", "output_text")))
    elif item in ("custom_tool_call", "function_call"):
        detail = payload.get("input", payload.get("arguments"))
        yield Block(at, f"tool {payload.get('name')}", detail if isinstance(detail, str) else "", one_line=True)
    elif item in ("custom_tool_call_output", "function_call_output"):
        yield Block(at, "result", _text(payload.get("output"), ("input_text", "output_text")), one_line=True)


def _claude_blocks(at: str, record: dict[str, object]) -> Iterator[Block]:
    if record.get("isSidechain") or record.get("isMeta"):
        return
    content = _fields(record.get("message")).get("content")
    if record.get("type") == "assistant":
        for part in _parts(content):
            if part.get("type") == "text" and isinstance(text := part.get("text"), str):
                yield Block(at, "assistant", text)
            elif part.get("type") == "tool_use":
                yield Block(at, f"tool {part.get('name')}", json.dumps(part.get("input"), ensure_ascii=False),
                            one_line=True)
        return
    if record.get("isCompactSummary"):
        yield Block(at, "compaction summary", _text(content, ("text",)))
        return
    if text := _text(content, ("text",)):
        yield Block(at, "user", text)
    for part in _parts(content):
        if part.get("type") == "tool_result":
            yield Block(at, "result", _text(part.get("content"), ("text",)), one_line=True)


def record_blocks(line: bytes) -> Iterator[Block]:
    """Read the blocks of one transcript line of either family; other lines give none."""
    try:
        record = _fields(cast(object, json.loads(line)))
    except ValueError:
        return
    at = record.get("timestamp")
    at = at if isinstance(at, str) else ""
    kind = record.get("type")
    if kind in ("response_item", "compacted"):
        yield from _codex_blocks(at, kind, _fields(record.get("payload")))
    elif kind in ("user", "assistant"):
        yield from _claude_blocks(at, record)


def last_blocks(path: Path, count: int) -> list[Block]:
    """Read from the end in doubling windows, so a large transcript is not read in full."""
    with transcripts.open_transcript(path) as stream:
        size = stream.seek(0, 2)
        window = WINDOW_BYTES
        while True:
            start = max(0, size - window)
            _ = stream.seek(start)
            if start:
                # The window can start inside a line.
                _ = stream.readline()
            found: deque[Block] = deque(maxlen=count)
            for line in stream:
                found.extend(block for block in record_blocks(line) if block.text.strip())
            if len(found) == count or not start:
                return list(found)
            window *= 2


def render(block: Block, chars: int, zone: tzinfo) -> str:
    try:
        shown = datetime.fromisoformat(block.at).astimezone(zone).strftime("%m-%d %H:%M:%S")
    except ValueError:
        shown = block.at or "no time"
    if block.one_line:
        text = " ".join(block.text.split())
        return f"[{shown}] {block.label}: {text[:TOOL_CHARS]}{'…' if len(text) > TOOL_CHARS else ''}"
    text = block.text.strip()
    if len(text) > chars:
        text = f"{text[:chars]}\n[{len(text) - chars} more characters]"
    return f"\n[{shown}] {block.label}\n{text}"


def main(arguments: list[str] | None = None) -> int:
    from scripts.lib.user_zone import user_zone

    parser = argparse.ArgumentParser(prog="catch_up.py", description="Print the last blocks of a saved conversation.")
    _ = parser.add_argument("native_id", metavar="NATIVE_ID")
    _ = parser.add_argument("--blocks", type=int, default=150, help="blocks to print (default 150)")
    _ = parser.add_argument("--chars", type=int, default=4000, help="characters kept in one message (default 4000)")
    options = parser.parse_args(arguments)
    native_id, blocks, chars = cast(str, options.native_id), cast(int, options.blocks), cast(int, options.chars)
    if blocks < 1 or chars < 1:
        parser.error("--blocks and --chars must be 1 or more")
    try:
        path = find_transcript(native_id)
    except ValueError as error:
        parser.error(str(error))
    except NoSavedTranscript as error:
        print(f"catch_up.py: {error}", file=sys.stderr)
        return 1
    found = last_blocks(path, blocks)
    zone = user_zone()
    print(f"{path}: last {len(found)} blocks, times in {zone.key}")
    for block in found:
        print(render(block, chars, zone))
    return 0


if __name__ == "__main__":
    sys.exit(main())
