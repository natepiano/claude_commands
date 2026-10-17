"""What started the turn a Stop hook is checking, read backwards from the session's transcript.

A turn opens with one or more prompt entries, each a `user` entry with an `origin`; a prompt
queued while the turn runs is a `queued_command` attachment with its own `origin`. Tool results,
meta reminders and compaction summaries carry no origin and are skipped.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

# A turn the user did not start: a cross-session message (the update timer's tick is one) or a
# background task's notification.
MACHINE_KINDS = frozenset({"peer", "task-notification"})
AUTO_MODE_DENIAL = "denied by the Claude Code auto mode classifier"
CHUNK_BYTES = 1 << 20
# A turn longer than this is read as unknown, so the rule that needs it stands down.
READ_LIMIT_BYTES = 64 << 20


@dataclass(frozen=True)
class TurnInput:
    kinds: frozenset[str]
    denied: bool

    @property
    def machine_only(self) -> bool:
        return bool(self.kinds) and self.kinds <= MACHINE_KINDS


def lines_backwards(path: Path) -> Iterator[str]:
    with path.open("rb") as transcript:
        position = transcript.seek(0, os.SEEK_END)
        start = position
        tail = b""
        while position > 0 and start - position < READ_LIMIT_BYTES:
            step = min(CHUNK_BYTES, position)
            position -= step
            _ = transcript.seek(position)
            block = transcript.read(step) + tail
            parts = block.split(b"\n")
            tail = parts[0]
            for part in reversed(parts[1:]):
                if part.strip():
                    yield part.decode("utf-8", errors="replace")
        if position == 0 and tail.strip():
            yield tail.decode("utf-8", errors="replace")


def _origin_kind(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    kind = cast(dict[str, object], value).get("kind")
    return kind if isinstance(kind, str) else None


def _queued_kind(queued: dict[str, object]) -> str:
    """An older queued prompt has no origin; its mode or its opening tag says who sent it."""
    kind = _origin_kind(queued.get("origin"))
    if kind is not None:
        return kind
    prompt = queued.get("prompt")
    if queued.get("commandMode") == "task-notification":
        return "task-notification"
    if isinstance(prompt, str) and prompt.lstrip().startswith("<cross-session-message"):
        return "peer"
    return "human"


def _tool_results(fields: dict[str, object]) -> list[dict[str, object]]:
    message = fields.get("message")
    content = cast(dict[str, object], message).get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [cast(dict[str, object], block) for block in cast(list[object], content)
            if isinstance(block, dict) and cast(dict[str, object], block).get("type") == "tool_result"]


def _refused(result: dict[str, object]) -> bool:
    """A tool call auto mode refused: an error result whose own text names the classifier."""
    content = result.get("content")
    blocks = cast(list[object], content) if isinstance(content, list) else [content]
    text = " ".join(block if isinstance(block, str) else str(cast(dict[str, object], block).get("text", ""))
                    for block in blocks if isinstance(block, (str, dict)))
    return result.get("is_error") is True and AUTO_MODE_DENIAL in text


def read_turn(path: Path) -> TurnInput | None:
    """The kinds of input that started the last turn and whether a tool call in it was auto-mode denied.

    None when the transcript cannot be read or the turn's start lies past the read limit.
    """
    kinds: set[str] = set()
    denied = False
    opened = False
    try:
        for line in lines_backwards(path):
            try:
                entry = cast(object, json.loads(line))
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            fields = cast(dict[str, object], entry)
            kind_of_entry = fields.get("type")
            if kind_of_entry == "assistant":
                if opened:
                    return TurnInput(frozenset(kinds), denied)
                continue
            if kind_of_entry == "attachment":
                attachment = fields.get("attachment")
                if isinstance(attachment, dict):
                    queued = cast(dict[str, object], attachment)
                    if queued.get("type") == "queued_command":
                        kinds.add(_queued_kind(queued))
                continue
            if kind_of_entry != "user":
                continue
            kind = _origin_kind(fields.get("origin"))
            if results := _tool_results(fields):
                denied = denied or (not opened and any(_refused(result) for result in results))
            elif kind is not None or not (fields.get("isMeta") or fields.get("isCompactSummary")):
                # A typed prompt written before prompts carried an origin is the user's.
                kinds.add(kind or "human")
                opened = True
    except OSError:
        return None
    return TurnInput(frozenset(kinds), denied) if opened else None
