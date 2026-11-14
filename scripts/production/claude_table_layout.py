"""Fit phase tables for Claude Code 2.1.319.

The renderer is ~/.local/share/claude/versions/2.1.319.
It reserves four columns and stacks cells that exceed four lines.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from scripts.lib.terminal_cells import terminal_width

# The renderer includes the reply indent in its four-column allowance.
REPLY_INDENT = 0
_FIXED_COLUMNS = frozenset(("Unit", "Status", "Machine", "Progress"))


class ColumnFit(NamedTuple):
    """The renderer's column widths and word wrapping mode."""

    widths: tuple[int, ...]
    hard_wrap: bool

    def line_count(self, text: str, column: int) -> int:
        return len(_lines(text, self.widths[column], self.hard_wrap))

    def keeps_table(self, headers: Sequence[str], rows: Sequence[Sequence[str]],
                    render_width: int, line_cap: int) -> bool:
        return (1 + sum(width + 3 for width in self.widths) <= render_width - 4
                and all(self.line_count(cell, column) <= line_cap
                        for row in (headers, *rows) for column, cell in enumerate(row)))


def _available(render_width: int, columns: int) -> int:
    return max(render_width - (1 + 3 * columns) - 4, 3 * columns)


def _floors(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> tuple[int, ...]:
    return tuple(max(3, *(terminal_width(word) for row in (headers, *rows)
                          for word in row[column].split())) for column in range(len(headers)))


def fit(headers: Sequence[str], rows: Sequence[Sequence[str]], render_width: int) -> ColumnFit:
    """Return the widths and wrapping mode from the Claude Code renderer."""
    floors = _floors(headers, rows)
    full = tuple(max(3, *(terminal_width(row[column]) for row in (headers, *rows)))
                 for column in range(len(headers)))
    available = _available(render_width, len(headers))
    if sum(full) <= available:
        return ColumnFit(full, False)
    if sum(floors) <= available:
        spare = available - sum(floors)
        growth = sum(full) - sum(floors)
        return ColumnFit(tuple(floor + int((width - floor) / growth * spare)
                               for floor, width in zip(floors, full, strict=True)), False)
    ratio = available / sum(floors)
    return ColumnFit(tuple(max(int(floor * ratio), 3) for floor in floors), True)


def _id_breaks(word: str) -> list[int]:
    """Return each position just after a `-`, `/` or `_` that joins two letters or digits."""
    return [index + 1 for index in range(1, len(word) - 1)
            if word[index] in "-/_" and word[index - 1].isalnum() and word[index + 1].isalnum()]


def _break_at(word: str, share: int) -> int:
    """Break after the last id mark within the share, else at the share."""
    breaks = _id_breaks(word)
    within = [end for end in breaks if terminal_width(word[:end]) <= share]
    if within:
        return within[-1]
    if breaks:
        return min(breaks, key=lambda end: max(terminal_width(word[:end]), terminal_width(word[end:])))
    return max((end for end in range(1, len(word)) if terminal_width(word[:end]) <= share), default=1)


def _lines(text: str, width: int, hard: bool) -> list[str]:
    lines = [""]
    for index, word in enumerate(text.rstrip().split(" ")):
        used = terminal_width(lines[-1])
        if index:
            if used >= width:
                lines.append("")
                used = 0
            # The renderer uses trim=False and retains this separator.
            lines[-1] += " "
            used += 1
        word_width = terminal_width(word)
        if hard and word_width > width:
            remaining = max(0, width - used)
            breaks_here = 1 + max(0, word_width - remaining - 1) // width
            breaks_next = (word_width - 1) // width
            if breaks_next < breaks_here:
                lines.append("")
                used = 0
            for char in word:
                size = terminal_width(char)
                if used + size > width and size:
                    lines.append("")
                    used = 0
                lines[-1] += char
                used += size
        else:
            if used + word_width > width and used and word_width:
                lines.append("")
            lines[-1] += word
    return [line for line in lines if line] or [""]


def _break_ids(headers: Sequence[str], rows: list[tuple[str, ...]], render_width: int,
               break_width: int) -> list[tuple[str, ...]]:
    """Break the longest word of a column until the column floors fit.

    A word breaks when it has an id mark or is at least `break_width` columns wide.
    When no such word is left, any longest word over 3 columns breaks:
    one break costs less than the hard wrap of every column.
    """
    available = _available(render_width, len(headers))
    floors = _floors(headers, rows)
    while sum(floors) > available:
        longest = [(column, word) for column, title in enumerate(headers) if title not in _FIXED_COLUMNS
                   for word in {part for row in rows for part in row[column].split()}
                   if terminal_width(word) == floors[column] > 3]
        candidates = ([(column, word) for column, word in longest
                       if _id_breaks(word) or terminal_width(word) >= break_width] or longest)
        if not candidates:
            break
        column, word = max(candidates, key=lambda item: (floors[item[0]], item[1]))
        share = max(3, int(floors[column] * available / sum(floors)))
        end = _break_at(word, share)
        replacement = word[:end] + " " + word[end:]
        rows = [(*row[:column], " ".join(replacement if part == word else part for part in row[column].split()),
                 *row[column + 1:]) for row in rows]
        floors = _floors(headers, rows)
    return rows


def _pieces(text: str, width: int, line_cap: int) -> list[str]:
    pieces: list[str] = []
    remaining = text
    while len(_lines(remaining, width, False)) > line_cap:
        words = remaining.split()
        end = 0
        for word in words:
            candidate = end + len(word) + (1 if end else 0)
            if len(_lines(remaining[:candidate], width, False)) > line_cap:
                break
            end = candidate
        for mark in (" · ", "; "):
            boundary = remaining.rfind(mark, 0, end + 1)
            if boundary >= 0:
                end = boundary + len(mark.rstrip())
                break
        pieces.append(remaining[:end].rstrip())
        remaining = remaining[end:].lstrip()
    return [*pieces, remaining]


def continued(headers: Sequence[str], rows: Sequence[Sequence[str]], render_width: int,
              line_cap: int, break_width: int) -> list[tuple[str, ...]]:
    """Split long cells into rows that fit at the configured floor."""
    table_rows = _break_ids(headers, [tuple(row) for row in rows], render_width, break_width)
    while True:
        layout = fit(headers, table_rows, render_width)
        continued_rows: list[tuple[str, ...]] = []
        changed = False
        for row in table_rows:
            first = list(row)
            extra: list[tuple[str, ...]] = []
            for column, cell in enumerate(row):
                if headers[column] in _FIXED_COLUMNS or layout.line_count(cell, column) <= line_cap:
                    continue
                pieces = _pieces(cell, layout.widths[column], line_cap)
                if len(pieces) == 1:
                    continue
                first[column] = pieces[0]
                for piece in pieces[1:]:
                    continuation = [""] * len(headers)
                    continuation[0] = row[0]
                    continuation[column] = piece
                    extra.append(tuple(continuation))
                changed = True
            continued_rows.extend((tuple(first), *extra))
        if not changed:
            return table_rows
        table_rows = continued_rows
