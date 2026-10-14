"""Read agent-note frontmatter without account, quota or updater dependencies."""

from __future__ import annotations

import re
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

# Package and sibling imports share the mapping and its relocation setting.
if __name__ == "note_file":
    _ = sys.modules.setdefault("scripts.whoami.note_file", sys.modules[__name__])
elif __name__ == "scripts.whoami.note_file":
    _ = sys.modules.setdefault("note_file", sys.modules[__name__])

AGENTS_DIR = Path.home() / "rust" / "hanadocs" / "agents"
_INITIAL_AGENTS_DIR = AGENTS_DIR
FIELD = re.compile(r"^([A-Za-z_][\w-]*):[ \t]*(.*?)[ \t]*$")
ACCOUNT_STEM = re.compile(r"(claude|codex) ?([1-9][0-9]*)")


@dataclass(frozen=True)
class AccountStem:
    tool: Literal["claude", "codex"]
    number: int

    @property
    def label(self) -> str:
        return f"{self.tool} {self.number}"


@dataclass(frozen=True)
class NotAccountStem:
    stem: str


def account_stem(stem: str) -> AccountStem | NotAccountStem:
    match = ACCOUNT_STEM.fullmatch(stem)
    if match is None:
        return NotAccountStem(stem)
    tool: Literal["claude", "codex"] = "claude" if match.group(1) == "claude" else "codex"
    return AccountStem(tool, int(match.group(2)))


def note_name(label: str) -> str:
    parsed = account_stem(label)
    if not isinstance(parsed, AccountStem) or parsed.label != label:
        raise ValueError(f"not an account label: {label}")
    return f"{parsed.tool}{parsed.number}.md"


def notes_dir() -> Path:
    if "AGENT_NOTES_DIR" in os.environ:
        return Path(os.environ["AGENT_NOTES_DIR"])
    return AGENTS_DIR if AGENTS_DIR != _INITIAL_AGENTS_DIR else Path.home() / "rust" / "hanadocs" / "agents"


def note_path(label: str) -> Path:
    numbered = notes_dir() / note_name(label)
    legacy = notes_dir() / (label + ".md")
    return legacy if not numbered.exists() and legacy.exists() else numbered


class FieldReader(Protocol):
    def get(self, key: str) -> str | None: ...
    def get_list(self, key: str) -> tuple[str, ...]: ...


def in_use(note: FieldReader) -> bool:
    if note.get_list("role"):
        return True
    try:
        return int(note.get("sessions") or "0") > 0
    except ValueError:
        return False


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
    label: str

    def lines(self) -> list[str]:
        return self.text[self.start:self.end].split("\n")

    def get(self, key: str) -> str | None:
        for line in self.lines():
            match = FIELD.match(line)
            if match and match.group(1) == key:
                return match.group(2).strip("\"'")
        return None

    def get_list(self, key: str) -> tuple[str, ...]:
        lines = self.lines()
        for index, line in enumerate(lines):
            match = FIELD.match(line)
            if match is None or match.group(1) != key:
                continue
            value = match.group(2)
            if value.startswith("[") and value.endswith("]"):
                return tuple(item.strip().strip("\"'") for item in value[1:-1].split(",") if item.strip())
            if value:
                return (value.strip("\"'"),)
            items: list[str] = []
            for following in lines[index + 1:]:
                item = re.fullmatch(r"\s*-\s+(.*?)\s*", following)
                if item is None:
                    break
                items.append(item.group(1).strip("\"'"))
            return tuple(items)
        return ()


def read_note(path: Path) -> Note | None:
    """A note with its frontmatter located, or None if unavailable or absent."""
    parsed = account_stem(path.stem)
    if isinstance(parsed, NotAccountStem):
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    if not text.startswith("---\n"):
        return None
    close = text.find("\n---", 3)
    if close == -1:
        return None
    return Note(path, parsed.tool, text, 4, close, parsed.label)


def read_notes() -> list[Note]:
    selected: dict[str, Note] = {}
    for path in sorted(notes_dir().glob("*.md")):
        parsed = account_stem(path.stem)
        if isinstance(parsed, AccountStem) and path.stem == parsed.label and (path.parent / note_name(parsed.label)).exists():
            continue
        note = read_note(path)
        if note is not None and (note.label not in selected or path.name == note_name(note.label)):
            selected[note.label] = note
    return [selected[label] for label in sorted(selected)]
