"""Terminal-cell layout shared by text output and the queue pane."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
import re
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict
from types import MappingProxyType
from scripts.lib.terminal_cells import terminal_width

if TYPE_CHECKING:
    from scripts.build_queue.admission_records import RunningStep, WaitingStep
    from scripts.lib.build_steps import StepNames
    from scripts.message.unit_labels import UnitLabels

NO_UNIT_LABELS: Mapping[str, UnitLabels] = MappingProxyType({})

Tone = Literal["default", "success", "error", "warning", "inactive", "suggestion"]


@dataclass(frozen=True)
class Span:
    text: str
    tone: Tone = "default"
    bold: bool = False
    background: Tone = "default"


Line = tuple[Span, ...]


class JsonSpan(TypedDict):
    text: str
    tone: NotRequired[Tone]
    bold: NotRequired[bool]
    background: NotRequired[Tone]


class QueueLines(TypedDict):
    columns: int
    as_of: float
    lines: list[list[JsonSpan]]


def field(text: str) -> str:
    return " ".join(text.split())


def step_owner_name(step: WaitingStep | RunningStep, labels: Mapping[str, UnitLabels]) -> str:
    """Use the recorded unit before the repository name."""
    identity = labels.get(step.session_id)
    unit = step.unit or (identity.unit if identity is not None else "")
    if identity is not None:
        unit = unit.removeprefix(identity.showrunner + "-")
    return unit if unit not in ("", "—", "owner not recorded") else step.project


def step_text(step: WaitingStep | RunningStep, names: StepNames, *,
              labels: Mapping[str, UnitLabels] = NO_UNIT_LABELS) -> str:
    """Show the owner unit with the step's shown name."""
    return field(f"{step_owner_name(step, labels)} {names.shown(step.step)}")


def step_prose(step: WaitingStep | RunningStep, names: StepNames, *,
               labels: Mapping[str, UnitLabels] = NO_UNIT_LABELS) -> str:
    """Show the owner unit with the step's sentence name."""
    return field(f"{step_owner_name(step, labels)} {names.prose(step.step)}")


def age(since: float, now: float) -> int:
    return max(0, int((now - since) // 60))


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
                return (*result, replace(span, text=text + "…"))
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
                and parts[-1].background == span.background):
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
            row.append(part)
        result.append(row)
    return {"columns": columns, "as_of": as_of, "lines": result}
