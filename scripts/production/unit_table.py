"""Parse a production Units table without loading staff or session readers."""
from __future__ import annotations

import re


class Refusal(Exception):
    """An invalid request that must leave the production unchanged."""


def cell_value(value: str) -> str:
    """Read the value before a cell's commentary."""
    quoted = re.search(r"`([^`]+)`", value)
    return quoted.group(1) if quoted else value.split(" — ", 1)[0].strip()


def unit_rows(lines: list[str]) -> tuple[int, list[str]]:
    try:
        start = lines.index("## Units")
    except ValueError as error:
        raise Refusal("production doc lacks Units table") from error
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    rows = [line for line in lines[start + 1:end] if line.startswith("|")]
    if len(rows) < 2 or not rows[0].startswith("| Unit |"):
        raise Refusal("production doc has invalid Units table")
    insert_at = max(i for i in range(start + 1, end) if lines[i].startswith("|")) + 1
    return insert_at, rows[2:]


def row_cells(row: str) -> list[str]:
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def unit_headings(lines: list[str]) -> list[str]:
    """The Units table's column headings, in the doc's own order."""
    _ = unit_rows(lines)
    return row_cells(next(line for line in lines[lines.index("## Units"):] if line.startswith("| Unit |")))


def unit_table(lines: list[str]) -> list[dict[str, str]]:
    """Each Units row as its cells by column heading, as written.

    Reading by heading lets a doc that still has the retired Session column read like one without it.
    """
    headings = unit_headings(lines)
    _, rows = unit_rows(lines)
    return [dict(zip(headings, row_cells(row), strict=False)) for row in rows]
