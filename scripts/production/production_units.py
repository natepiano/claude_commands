#!/usr/bin/env python3
"""Read a production doc's Units table and identify its live and retired units."""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    import unit_lookup
from scripts.production import showrunners
from collections.abc import Iterable
from scripts.production import staff
import unit_names

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


def plan_arguments(plan: str) -> list[str]:
    """Translate a Units Plan cell into add_unit's launch mode."""
    return ["--" + plan] if plan in ("standby", "permanent") else ["--plan", plan]


def plan_cell_is_retired(plan_cell: str) -> bool:
    """Return whether a Plan cell begins with the retired marker."""
    return re.match(r"^\s*\(?retired\b", plan_cell) is not None


def worktree_is_linked(worktree: str) -> bool:
    """Return whether `worktree` is a linked worktree on disk.

    A main checkout, where `.git` is a directory, is shared and is no unit's own worktree.
    """
    return bool(worktree) and (Path(worktree) / ".git").is_file()


class ProductionMembers:
    """Recorded membership of a production, read once without probing live panes."""

    def __init__(self, slug: str, *, record: staff.StaffSnapshot | None = None) -> None:
        self._slug: str = slug
        self._record: staff.StaffSnapshot | None = record
        self._units: set[str] | None = None

    def has(self, unit: str) -> bool:
        """Whether the unit is a current staff child, including an offline child."""
        if self._units is None:
            record = self._record if self._record is not None else staff.snapshot()
            import unit_lookup

            sources = unit_lookup.member_sources()
            members = unit_lookup.production_members(record, sources)
            self._units = {row.name for row in members.get(showrunners.canonical_slug(self._slug), ())}
        return unit_names.name(unit) in self._units


def unit_is_retired(cells: dict[str, str], marks: ProductionMembers) -> bool:
    """A unit is retired when it has no current row under this showrunner."""
    unit = cell_value(cells.get("Unit", ""))
    return not unit or not marks.has(unit)


def order_unit_names(lines: list[str], names: Iterable[str], *, offline: Iterable[str] = ()) -> list[str]:
    """Keep document presentation order without letting it decide membership."""
    wanted = set(names)
    by_name = {unit_names.name(name): name for name in wanted}
    unplaced = set(offline)
    ordered: list[str] = []
    try:
        for cells in unit_table(lines):
            displayed = cell_value(cells.get("Unit", ""))
            name = by_name.get(unit_names.name(displayed), "")
            if name in wanted:
                ordered.append(displayed if name in unplaced else name)
                wanted.remove(name)
    except (Refusal, unit_names.BadName):
        pass
    return ordered + sorted(wanted)


def unit_metadata(record: staff.StaffSnapshot, sources: unit_lookup.MemberSources, *,
                  documents: dict[str, list[str]] | None = None) -> dict[staff.StaffId, dict[str, str]]:
    """Bind document cells to staff identities, preferring the current parent's row."""
    import unit_lookup

    members = unit_lookup.production_members(record, sources)
    documents = ({slug: source.document.splitlines() for slug, source in sources.productions.items()}
                 | (documents or {}))
    direct: dict[staff.StaffId, dict[str, str]] = {}
    orphans: dict[str, list[tuple[str, dict[str, str]]]] = {}
    for slug, lines in documents.items():
        local = {row.name: row for row in members.get(slug, ())}
        try:
            rows = unit_table(lines)
        except Refusal:
            continue
        for cells in rows:
            try:
                name = unit_names.name(cell_value(cells.get("Unit", "")))
            except unit_names.BadName:
                continue
            if name in local:
                direct[local[name].id] = cells
            else:
                orphans.setdefault(name, []).append((slug, cells))
    units = record.find(kind="unit")
    for row in units:
        if row.id in direct:
            continue
        candidates = orphans.get(row.name, [])
        if len(candidates) == 1 and sum(other.name == row.name for other in units) == 1:
            direct[row.id] = candidates[0][1]
            continue
        parents = [slug for slug, rows in members.items() if row in rows]
        production = ", ".join(parents) or str(row.parent)
        reason = "ambiguous orphan rows" if len(candidates) > 1 else "no unique document binding"
        print(f"production_units: {production}/{row.name}: metadata unavailable: {reason}", file=sys.stderr)
    return direct


def live_unit_table(lines: list[str], slug: str, *, record: staff.StaffSnapshot | None = None) -> list[dict[str, str]]:
    """Staff members with metadata bound by identity; a table cannot change a parent."""
    import unit_lookup

    record = record if record is not None else staff.snapshot()
    sources = unit_lookup.member_sources()
    slug = showrunners.canonical_slug(slug)
    members = unit_lookup.production_members(record, sources).get(slug, ())
    metadata = unit_metadata(record, sources, documents={slug: lines})
    rows = {unit_names.unit_id(row.name): {**metadata.get(row.id, {"Worktree": "—", "Plan": "—"}),
                                          "Unit": metadata.get(row.id, {}).get("Unit", f"`{unit_names.unit_id(row.name)}`")}
            for row in members}
    return [rows[name] for name in order_unit_names(lines, rows)]


def retired_units(lines: list[str], slug: str, *, record: staff.StaffSnapshot | None = None) -> set[str]:
    """Document unit names that are no longer recorded under this showrunner."""
    marks = ProductionMembers(slug, record=record)
    return {cell_value(cells.get("Unit", "")) for cells in unit_table(lines) if unit_is_retired(cells, marks)} - {""}


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
