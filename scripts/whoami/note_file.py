"""Read agent-note frontmatter without account, quota or updater dependencies."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

AGENTS_DIR = Path.home() / "rust" / "hanadocs" / "agents"
FIELD = re.compile(r"^([A-Za-z_][\w-]*):[ \t]*(.*?)[ \t]*$")


def reset_time(value: str | None) -> datetime | None:
    """A note's `resets` time, aware; None when absent or unreadable.

    Notes travel between machines in the vault. A reset written with its offset reads the same on
    every machine; one written before the offset was recorded is read in this machine's zone.
    """
    if not value or value == "null":
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


@dataclass
class Note:
    path: Path
    tool: str
    text: str
    start: int
    end: int

    def lines(self) -> list[str]:
        return self.text[self.start:self.end].split("\n")

    def get(self, key: str) -> str | None:
        for line in self.lines():
            match = FIELD.match(line)
            if match and match.group(1) == key:
                return match.group(2).strip("\"'")
        return None


def read_note(path: Path) -> Note | None:
    """A note with its frontmatter located, or None if unavailable or absent."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    if not text.startswith("---\n"):
        return None
    close = text.find("\n---", 3)
    if close == -1:
        return None
    return Note(path, path.stem.split(" ")[0].lower(), text, 4, close)


def read_notes() -> list[Note]:
    return [note for path in sorted(AGENTS_DIR.glob("*.md")) if (note := read_note(path))]
