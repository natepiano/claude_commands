"""Read style family owners, ticket limits and merge routes from production documents."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import cast, get_args

from scripts.production import production_doc, staff
from scripts.style import rules


class StyleConfigError(ValueError):
    """The style configuration cannot supply the requested assignment or route."""


@dataclass(frozen=True)
class StyleUnitAssignment:
    unit: str
    families: tuple[rules.Family, ...]
    open_tickets: int = 1


@dataclass(frozen=True)
class StyleShowrunnerRoute:
    receiving_branch: str
    branch_point: str
    doc: Path
    checkout: Path


@dataclass(frozen=True)
class ParentUnitRoute:
    receiving_branch: str
    branch_point: str
    doc: Path
    checkout: Path


@dataclass(frozen=True)
class StyleRepositoryConfig:
    assignments: tuple[StyleUnitAssignment, ...]
    route: StyleShowrunnerRoute | ParentUnitRoute

    def owner(self, family: rules.Family) -> StyleUnitAssignment:
        """Return the assignment that owns the family."""
        return next(assignment for assignment in self.assignments if family in assignment.families)

    def assignment(self, unit: str) -> StyleUnitAssignment:
        """Return the assignment for the staff name."""
        for assignment in self.assignments:
            if assignment.unit == unit:
                return assignment
        raise StyleConfigError(f"{unit}: no style assignment")


_STYLE_UNITS_HEADING = re.compile(r"(#{1,6})\s+Style units\s*#*\s*")


def has_style_units(lines: Iterable[str]) -> bool:
    """Return whether the document lines contain a Style units heading."""
    return any(_STYLE_UNITS_HEADING.fullmatch(line) for line in lines)


def _assignments(lines: list[str]) -> tuple[StyleUnitAssignment, ...]:
    section = next(((index, len(match[1])) for index, line in enumerate(lines)
                    if (match := _STYLE_UNITS_HEADING.fullmatch(line))), None)
    if section is None:
        raise StyleConfigError("production doc lacks Style units section")
    start, level = section
    rows: list[list[str]] = []
    for line in lines[start + 1:]:
        heading = re.match(r"^(#{1,6})\s", line)
        if heading and len(heading[1]) <= level:
            break
        if line.strip().startswith("|"):
            rows.append([cell.strip().strip("`") for cell in line.strip().strip("|").split("|")])
    assignments: list[StyleUnitAssignment] = []
    units: set[str] = set()
    owners: dict[rules.Family, str] = {}
    families = cast(tuple[rules.Family, ...], get_args(rules.Family))
    for row in rows:
        if row == ["Unit", "Families", "Open tickets"] or all(re.fullmatch(r":?-+:?", cell) for cell in row):
            continue
        unit, family_text, capacity = (row + ["", "", ""])[:3]
        if unit in units:
            raise StyleConfigError(f"{unit}: duplicate style unit")
        units.add(unit)
        try:
            open_tickets = int(capacity) if capacity else 1
        except ValueError as error:
            raise StyleConfigError(f"{unit}: Open tickets must be a positive integer") from error
        if open_tickets < 1:
            raise StyleConfigError(f"{unit}: Open tickets must be a positive integer")
        owned: list[rules.Family] = []
        for value in family_text.split(","):
            value = value.strip().strip("`")
            if value not in families:
                raise StyleConfigError(f"{unit}: unknown style family {value!r}")
            family = value
            if family in owners:
                raise StyleConfigError(f"{family}: two style owners: {owners[family]}, {unit}")
            owners[family] = unit
            owned.append(family)
        assignments.append(StyleUnitAssignment(unit, tuple(owned), open_tickets))
    for family in families:
        if family not in owners:
            raise StyleConfigError(f"{family}: no style owner")
    return tuple(assignments)


def read_config(doc: Path) -> StyleRepositoryConfig:
    """Read the document once and select its merge route."""
    try:
        lines = doc.read_text(encoding="utf-8").splitlines()
        assignments = _assignments(lines)
        receiving_branch = production_doc.production_field(lines, "Merge branch")
        checkout = Path(production_doc.production_field(lines, "Showrunner checkout")).expanduser().resolve()
        if production_doc.optional_field(lines, "Kind") == production_doc.FieldPresent("style"):
            branch_point = production_doc.production_field(lines, "Branch point").removeprefix("refs/heads/")
            route = StyleShowrunnerRoute(receiving_branch, branch_point, doc, checkout)
        else:
            route = ParentUnitRoute(receiving_branch, receiving_branch, doc, checkout)
        return StyleRepositoryConfig(assignments, route)
    except (OSError, UnicodeError, production_doc.Refusal) as error:
        raise StyleConfigError(f"{doc}: {error}") from error


def config_for_unit(unit: str | staff.StaffRow, record: staff.StaffSnapshot) -> StyleRepositoryConfig:
    """Find the unit's parent document through staff and read its configuration."""
    row = unit if isinstance(unit, staff.StaffRow) else next(
        (row for row in record.find(kind="unit") if row.name == unit or row.id == unit), None)
    if row is None or row.kind != "unit":
        raise StyleConfigError(f"{unit}: no unit staff row")
    parent = record.parent_of(row)
    if parent is None or parent.kind != "showrunner" or not isinstance(parent.doc, (staff.LiveDoc, staff.WrappedDoc)):
        raise StyleConfigError(f"{row.name}: no parent production document")
    config = read_config(parent.doc.path)
    _ = config.assignment(row.name)
    return config
