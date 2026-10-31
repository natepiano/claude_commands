"""Read agent-note frontmatter without account, quota or updater dependencies."""

from __future__ import annotations

import re
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

AGENTS_DIR = Path.home() / "rust" / "hanadocs" / "agents"
_INITIAL_AGENTS_DIR = AGENTS_DIR
FIELD = re.compile(r"^([A-Za-z_][\w-]*):[ \t]*(.*?)[ \t]*$")
ACCOUNT_STEM = re.compile(r"(claude|codex) ?([1-9][0-9]*)")


@dataclass(frozen=True)
class NumberedAccountLabel:
    tool: Literal["claude", "codex"]
    number: int

    @property
    def label(self) -> str:
        return f"{self.tool}{self.number}"


@dataclass(frozen=True)
class InvalidAccountLabel:
    stem: str


def parse_account_label(stem: str) -> NumberedAccountLabel | InvalidAccountLabel:
    match = ACCOUNT_STEM.fullmatch(stem)
    if match is None:
        return InvalidAccountLabel(stem)
    tool: Literal["claude", "codex"] = "claude" if match.group(1) == "claude" else "codex"
    return NumberedAccountLabel(tool, int(match.group(2)))


def note_name(label: str) -> str:
    parsed = parse_account_label(label)
    if not isinstance(parsed, NumberedAccountLabel):
        raise ValueError(f"not an account label: {label}")
    return f"{parsed.tool}{parsed.number}.md"


def notes_dir() -> Path:
    if "AGENT_NOTES_DIR" in os.environ:
        return Path(os.environ["AGENT_NOTES_DIR"])
    return AGENTS_DIR if AGENTS_DIR != _INITIAL_AGENTS_DIR else Path.home() / "rust" / "hanadocs" / "agents"


def note_path(label: str) -> Path:
    numbered = notes_dir() / note_name(label)
    parsed = parse_account_label(label)
    assert isinstance(parsed, NumberedAccountLabel)
    legacy = notes_dir() / f"{parsed.tool} {parsed.number}.md"
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


@dataclass(frozen=True)
class KnownReset:
    value: datetime


@dataclass(frozen=True)
class NoResetKnown:
    """The note does not specify a reset."""


@dataclass(frozen=True)
class InvalidReset:
    value: str


def reset_time(value: str | None) -> KnownReset | NoResetKnown | InvalidReset:
    """Read an aware reset time; legacy times without offsets use the local zone.

    Notes travel between machines in the vault: a reset written with its offset
    reads the same everywhere; one written without it reads in this machine zone.
    """
    if not value or value == "null":
        return NoResetKnown()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return InvalidReset(value)
    return KnownReset(parsed if parsed.tzinfo is not None else parsed.astimezone())


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


@dataclass(frozen=True)
class NotAccountNote:
    path: Path


@dataclass(frozen=True)
class UnreadableNote:
    path: Path


@dataclass(frozen=True)
class MalformedNote:
    path: Path


def read_note(path: Path) -> Note | NotAccountNote | UnreadableNote | MalformedNote:
    parsed = parse_account_label(path.stem)
    if isinstance(parsed, InvalidAccountLabel):
        return NotAccountNote(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return UnreadableNote(path)
    if not text.startswith("---\n"):
        return MalformedNote(path)
    close = text.find("\n---", 3)
    if close == -1:
        return MalformedNote(path)
    return Note(path, parsed.tool, text, 4, close, parsed.label)


def read_notes() -> list[Note]:
    selected: dict[str, Note] = {}
    for path in sorted(notes_dir().glob("*.md")):
        parsed = parse_account_label(path.stem)
        if not isinstance(parsed, NumberedAccountLabel):
            continue
        if path.stem == f"{parsed.tool} {parsed.number}" and (path.parent / note_name(parsed.label)).exists():
            continue
        note = read_note(path)
        if isinstance(note, Note) and (note.label not in selected or path.name == note_name(note.label)):
            selected[note.label] = note
    def order(note: Note) -> tuple[str, int]:
        parsed = parse_account_label(note.label)
        assert isinstance(parsed, NumberedAccountLabel)
        return parsed.tool, parsed.number
    return sorted(selected.values(), key=order)


def canonical_label(text: str) -> str:
    """Normalize a numbered label, preserving logins and default-home aliases."""
    parsed = parse_account_label(text)
    return parsed.label if isinstance(parsed, NumberedAccountLabel) else text


def canonical_label_text(text: str) -> str:
    """Normalize account mentions without changing surrounding notice text."""
    return re.sub(rf"\b{ACCOUNT_STEM.pattern}\b", lambda match: canonical_label(match.group()), text)
