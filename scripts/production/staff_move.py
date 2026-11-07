#!/usr/bin/env python3
"""Move a unit's staff parent and its running herdr tab together."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import sys
from dataclasses import dataclass

from scripts.production import staff, staff_sources  # noqa: E402
from scripts.herdr import herdr  # noqa: E402


@dataclass(frozen=True)
class UnitMoveResult:
    row: staff.StaffRow
    changed: bool
    workspace: str


@dataclass(frozen=True)
class PinnedUnit:
    """The caller selects fixed unit and destination IDs."""

    staff_id: staff.StaffId
    parent_staff_id: staff.StaffId


@dataclass(frozen=True)
class LocateUnit:
    """Resolve the unit through the current source member selectors."""


UnitIdentity = PinnedUnit | LocateUnit
_LOCATE_UNIT = LocateUnit()


def _place_tab(row: staff.StaffRow, record: staff.StaffSnapshot) -> bool:
    return herdr.Herdr(herdr.shared_session()).move_row_tabs(row.id, record=record)


def move_unit(production_name: str, unit: str, new_showrunner: str, *, identity: UnitIdentity = _LOCATE_UNIT) -> UnitMoveResult:
    """Change only membership, then reconcile the live tab with its parent's workspace."""
    with staff.operation():
        record = staff.snapshot()
        selector = staff_sources.UnitMember(production_name, unit)
        parent_selector = staff_sources.ShowrunnerMember(new_showrunner)
        if isinstance(identity, PinnedUnit):
            pinned = record.by_id(identity.staff_id)
            found = staff_sources.MemberFound(pinned) if pinned is not None else staff_sources.NoSuchMember(selector)
            destination = record.by_id(identity.parent_staff_id)
            parent = (staff_sources.MemberFound(destination) if destination is not None
                      else staff_sources.NoSuchMember(parent_selector))
        else:
            found = staff_sources.member(selector, record)
            parent = staff_sources.member(parent_selector, record)
        if isinstance(identity, LocateUnit) and isinstance(found, staff_sources.NoSuchMember):
            # A completed parent change precedes the source updates in a transfer.
            found = staff_sources.member(staff_sources.UnitMember(new_showrunner, unit), record)
        if isinstance(found, staff_sources.NoSuchMember) or found.row.kind != "unit":
            raise staff.StaffRefused(f"{production_name}/{unit}: no matching unit staff row")
        if isinstance(parent, staff_sources.NoSuchMember) or parent.row.kind != "showrunner":
            raise staff.StaffRefused(f"{new_showrunner}: no matching showrunner staff row")
        row = found.row if found.row.parent == staff.ReportsTo(parent.row.id) else staff.move(found.row.id, staff.ReportsTo(parent.row.id))
        try:
            current = staff.StaffSnapshot(tuple(row if member.id == row.id else member for member in record.rows))
            changed = _place_tab(row, current)
        except (OSError, ValueError, herdr.HerdrFailure) as error:
            raise staff.StaffUnreadable(f"{production_name}/{unit}: tab was not moved: {error}") from error
        return UnitMoveResult(row, found.row.parent != row.parent or changed, parent.row.name)


def report(result: UnitMoveResult, *, json_output: bool = False) -> int:
    """Report whether the parent or the tab placement changed."""
    if not json_output:
        print(f"{'done' if result.changed else 'unchanged'}: {result.row.name} under {result.workspace}")
    else:
        print(json.dumps(result.row.to_json(), sort_keys=True))
    return 0


def cli_move(target: str, new_showrunner: str, *, json_output: bool = False) -> int:
    def apply() -> int:
        parts = target.split("/")
        if len(parts) != 2 or not all(parts) or not new_showrunner:
            raise staff.StaffRefused("move needs <showrunner production_name>/<unit> <new showrunner production_name>")
        return report(move_unit(parts[0], parts[1], new_showrunner), json_output=json_output)

    return staff.cli_result(apply)


def main(argv: list[str] | None = None) -> int:
    return staff.main(["move", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    raise SystemExit(main())
