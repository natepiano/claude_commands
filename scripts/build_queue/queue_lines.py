"""Terminal-cell layout shared by text output and the queue pane."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import re
from typing import Literal, TypedDict
import unicodedata

Tone = Literal["default", "success", "error", "warning", "inactive"]


@dataclass(frozen=True)
class Span:
    text: str
    tone: Tone = "default"
    bold: bool = False


Line = tuple[Span, ...]


class JsonSpan(TypedDict):
    text: str
    tone: Tone
    bold: bool


class QueueLines(TypedDict):
    columns: int
    as_of: float
    lines: list[list[JsonSpan]]


def age(since: float, now: float) -> int:
    return max(0, int((now - since) // 60))


def terminal_width(text: str) -> int:
    return sum(0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
               for char in text)


def pad_left(text: str, cells: int) -> str:
    return " " * max(0, cells - terminal_width(text)) + text


def pad_right(text: str, cells: int) -> str:
    return text + " " * max(0, cells - terminal_width(text))


def cut_tail(text: str, cells: int) -> str:
    return plain([clip((Span(text),), cells)])


def clip(line: Line, columns: int) -> Line:
    if columns <= 0:
        return ()
    total = sum(terminal_width(span.text) for span in line)
    if total <= columns:
        return line
    result: list[Span] = []
    left = max(0, columns - 1)
    for span in line:
        text = ""
        for char in span.text:
            size = terminal_width(char)
            if size > left:
                return (*result, Span(text + "…", span.tone, span.bold))
            text += char
            left -= size
        result.append(Span(text, span.tone, span.bold))
    return tuple(result)


def merge_spans(line: Line) -> Line:
    parts: list[Span] = []
    for span in line:
        if not span.text:
            continue
        if parts and parts[-1].tone == span.tone and parts[-1].bold == span.bold:
            previous = parts[-1]
            parts[-1] = Span(previous.text + span.text, span.tone, span.bold)
        else:
            parts.append(span)
    return tuple(parts)


def wrap(line: Line, columns: int, indent: int = 2) -> list[Line]:
    """Wrap at words, preserving tones and a hanging indent; split long words by cells."""
    columns = max(1, columns)
    continuation = max(0, min(indent, columns - 1))
    rows: list[Line] = []
    row: list[Span] = []
    width = 0

    def close_row() -> None:
        rows.append(merge_spans(trim_right(tuple(row))))

    # A style boundary is not a word boundary.
    tokens: list[list[Span]] = []
    for span in line:
        for match in re.finditer(r"\n|[^\S\n]+|\S+", span.text):
            text = match.group()
            part = Span(text, span.tone, span.bold)
            if (tokens and text != "\n" and tokens[-1][-1].text != "\n"
                    and tokens[-1][-1].text.isspace() == text.isspace()):
                tokens[-1].append(part)
            else:
                tokens.append([part])

    for token in tokens:
        if token[0].text == "\n":
            close_row()
            row = [Span(" " * continuation)] if continuation else []
            width = continuation
            continue
        if token[0].text.isspace():
            for span in token:
                text = ""
                for char in span.text:
                    size = terminal_width(char)
                    if width + size > columns:
                        break
                    text += char
                    width += size
                if text:
                    row.append(Span(text, span.tone, span.bold))
            continue
        size = sum(terminal_width(span.text) for span in token)
        if width + size > columns and width > (continuation if rows else 0):
            close_row()
            row = [Span(" " * continuation)] if continuation else []
            width = continuation
        for span in token:
            text = ""
            for char in span.text:
                size = terminal_width(char)
                if width + size > columns:
                    if text:
                        row.append(Span(text, span.tone, span.bold))
                    if any(part.text.strip() for part in row):
                        close_row()
                    row = [Span(" " * continuation)] if continuation else []
                    width, text = continuation, ""
                if width + size > columns:
                    # Leave room for a wide glyph when the indent would consume it.
                    row, width = [], 0
                if size > columns:
                    char, size = "…", 1
                text += char
                width += size
            if text:
                row.append(Span(text, span.tone, span.bold))
    close_row()
    return rows


def trim_right(line: Line) -> Line:
    parts = list(line)
    while parts and not parts[-1].text.rstrip():
        del parts[-1]
    if parts:
        span = parts[-1]
        parts[-1] = Span(span.text.rstrip(), span.tone, span.bold)
    return tuple(parts)


def plain(lines: Sequence[Line]) -> str:
    return "\n".join("".join(span.text for span in line) for line in lines)


def json_form(lines: Sequence[Line], columns: int, as_of: float) -> QueueLines:
    return {"columns": columns, "as_of": as_of,
            "lines": [[{"text": span.text, "tone": span.tone, "bold": span.bold}
                       for span in merge_spans(line)] for line in lines]}
