#!/usr/bin/env python3
"""Read a production doc's Units table and identify its live and retired units."""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo

import unit_lookup

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from user_zone import user_zone  # noqa: E402


class Refusal(Exception):
    """An invalid request that must leave the production unchanged."""


class Production(NamedTuple):
    doc: Path
    slug: str
    merge_branch: str
    checkout: Path
    log: Path
    zone: ZoneInfo


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


def plan_cell_is_retired(plan_cell: str) -> bool:
    """Return whether a Plan cell begins with the retired marker."""
    return re.match(r"^\s*\(?retired\b", plan_cell) is not None


def worktree_is_linked(worktree: str) -> bool:
    """Return whether `worktree` is a linked worktree on disk.

    A main checkout, where `.git` is a directory, is shared and is no unit's own worktree.
    """
    return bool(worktree) and (Path(worktree) / ".git").is_file()


class SessionMarks:
    """Which units carry session marks, read once and only when a row needs them."""

    def __init__(self, slug: str) -> None:
        self._slug: str = slug
        self._units: set[str] | None = None
        self._unknown: bool = False

    def has(self, unit: str) -> bool:
        """Whether a session carries the unit's mark. An unreadable host has not said no."""
        if self._units is None:
            try:
                self._units = set(unit_lookup.marked_units(self._slug))
            except OSError:
                self._units, self._unknown = set(), True
        return self._unknown or unit in self._units


def unit_is_retired(cells: dict[str, str], marks: SessionMarks) -> bool:
    """Return whether a Units row's unit is retired.

    It is when its Plan cell says so, or when its worktree and its session are both gone
    (user 2026-10-08): such a unit has nothing left to address. The worktree is asked first, so a
    unit whose session is only restarting keeps its row, and session marks are read only about a unit
    with no worktree of its own. The session is the one carrying the unit's mark, whatever its name.
    """
    if plan_cell_is_retired(cells.get("Plan", "")):
        return True
    if "Worktree" not in cells:
        return False
    return (not worktree_is_linked(cell_value(cells["Worktree"]))
            and not marks.has(cell_value(cells.get("Unit", ""))))


def live_unit_table(lines: list[str], slug: str) -> list[dict[str, str]]:
    """Return Units rows, by column heading, whose unit is not retired."""
    marks = SessionMarks(slug)
    return [cells for cells in unit_table(lines) if not unit_is_retired(cells, marks)]


def retired_units(lines: list[str], slug: str) -> set[str]:
    """Return unit names from Units rows whose unit is retired."""
    marks = SessionMarks(slug)
    retired = {cell_value(cells.get("Unit", "")) for cells in unit_table(lines) if unit_is_retired(cells, marks)}
    return retired - {""}


def production_field(lines: list[str], field: str) -> str:
    """Read a Production Context field without interpreting its commentary."""
    prefix = f"- **{field}:** "
    for line in lines:
        if line.startswith(prefix):
            result = cell_value(line[len(prefix):])
            if result:
                return result
    raise Refusal(f"production doc lacks {field}")


def read_production(path: Path) -> Production:
    """Read the production fields shared with merge_checkpoint.py."""
    try:
        doc = path.resolve(strict=True)
        lines = doc.read_text(encoding="utf-8").splitlines()
        merge_branch = production_field(lines, "Merge branch")
        checkout = Path(production_field(lines, "Showrunner checkout")).expanduser().resolve()
        log = Path(production_field(lines, "Log"))
        zone = user_zone()
        slug = doc.name.removesuffix("-production.md")
        if slug == doc.name:
            raise Refusal("production doc name must end in -production.md")
        if log.is_absolute():
            raise Refusal("production Log must be relative to Showrunner checkout")
        if "## Units" not in lines:
            raise Refusal("production doc lacks Units table")
        return Production(doc, slug, merge_branch, checkout, checkout / log, zone)
    except (OSError, UnicodeError, KeyError, ValueError) as error:
        raise Refusal(f"cannot read production doc: {error}") from error
