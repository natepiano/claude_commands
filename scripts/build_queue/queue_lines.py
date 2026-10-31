"""Terminal-cell layout shared by text output and the queue pane."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
import re
from typing import Literal, NotRequired, TypedDict
import unicodedata

Tone = Literal["default", "success", "error", "warning", "inactive", "suggestion"]


@dataclass(frozen=True)
class Span:
    text: str
    tone: Tone = "default"
    bold: bool = False
    background: Tone = "default"
    until: float | None = None


Line = tuple[Span, ...]


class JsonSpan(TypedDict):
    text: str
    tone: NotRequired[Tone]
    bold: NotRequired[bool]
    background: NotRequired[Tone]
    until: NotRequired[float]


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
                return (*result, replace(span, text=text + "…", until=None))
            text += char
            left -= size
        result.append(replace(span, text=text))
    return tuple(result)


def merge_spans(line: Line) -> Line:
    parts: list[Span] = []
    for span in line:
        if not span.text:
            continue
        if (parts and parts[-1].tone == span.tone and parts[-1].bold == span.bold
                and parts[-1].background == span.background and parts[-1].until == span.until):
            previous = parts[-1]
            parts[-1] = replace(span, text=previous.text + span.text)
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
            part = replace(span, text=text)
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
                    row.append(replace(span, text=text))
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
                        row.append(replace(span, text=text))
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
                row.append(replace(span, text=text))
    close_row()
    return rows


def trim_right(line: Line) -> Line:
    parts = list(line)
    while parts and not parts[-1].text.rstrip():
        del parts[-1]
    if parts:
        span = parts[-1]
        parts[-1] = replace(span, text=span.text.rstrip())
    return tuple(parts)


def plain(lines: Sequence[Line]) -> str:
    return "\n".join("".join(span.text for span in line) for line in lines)


def json_form(lines: Sequence[Line], columns: int, as_of: float) -> QueueLines:
    defaults = Span("")
    result: list[list[JsonSpan]] = []
    for line in lines:
        row: list[JsonSpan] = []
        for span in merge_spans(line):
            part: JsonSpan = {"text": span.text}
            if span.tone != defaults.tone:
                part["tone"] = span.tone
            if span.bold != defaults.bold:
                part["bold"] = span.bold
            if span.background != defaults.background:
                part["background"] = span.background
            if span.until is not None:
                part["until"] = span.until
            row.append(part)
        result.append(row)
    return {"columns": columns, "as_of": as_of, "lines": result}
