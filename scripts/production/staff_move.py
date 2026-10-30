#!/usr/bin/env python3
"""Move a unit's staff parent and its running herdr tab together."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from scripts.production import staff, staff_sources  # noqa: E402
import herdr  # noqa: E402


@dataclass(frozen=True)
class TabMoved:
    workspace: str


@dataclass(frozen=True)
class TabAlreadyPlaced:
    workspace: str


@dataclass(frozen=True)
class NoLiveTab:
    workspace: str


@dataclass(frozen=True)
class UnitMoveResult:
    row: staff.StaffRow
    membership_changed: bool
    tab: TabMoved | TabAlreadyPlaced | NoLiveTab


@dataclass(frozen=True)
class PinnedUnit:
    """A transfer journal fixed the unit and destination before changing sources."""

    staff_id: staff.StaffId
    parent_staff_id: staff.StaffId


@dataclass(frozen=True)
class LocateUnit:
    """Resolve the unit through the current source member selectors."""


UnitIdentity = PinnedUnit | LocateUnit
_LOCATE_UNIT = LocateUnit()


def _place_tab(slug: str, unit: str, row: staff.StaffRow, workspace: str,
               record: staff.StaffSnapshot) -> TabMoved | TabAlreadyPlaced | NoLiveTab:
    client = herdr.Herdr(herdr.shared_session())
    found = client.lookup(row.name)
    if isinstance(found, herdr.NoPane):
        return NoLiveTab(workspace)
    if isinstance(found, herdr.SeveralPanes):
        raise herdr.HerdrFailure(f"{slug}/{unit}: several panes ({found.count}) for label {row.name!r}")
    panes = [herdr.json_object(item, "herdr pane") for item in herdr.json_items(
        client.result("pane", "list").get("panes"), "herdr pane list")]
    current = next((item for item in panes if item.get("pane_id") == found.pane_id), None)
    if current is None:
        return NoLiveTab(workspace)
    spaces = [herdr.json_object(item, "herdr workspace") for item in herdr.json_items(
        client.result("workspace", "list").get("workspaces"), "herdr workspace list")]
    if any(space.get("label") == workspace and space.get("workspace_id") == current.get("workspace_id")
           for space in spaces):
        # A previous move may have finished just before its tab rename failed.
        tab = herdr.json_text(current.get("tab_id"), "herdr pane tab id")
        workspace_id = herdr.json_text(current.get("workspace_id"), "herdr pane workspace id")
        tabs = [herdr.json_object(item, "herdr tab") for item in herdr.json_items(
            client.result("tab", "list", "--workspace", workspace_id).get("tabs"), "herdr tab list")]
        if not any(item.get("tab_id") == tab and item.get("label") == row.name for item in tabs):
            _ = client.run("tab", "rename", tab, row.name)
        placed = TabAlreadyPlaced(workspace)
    else:
        _ = client.move_pane_to_workspace(found.pane_id, workspace, row.name, record=record)
        placed = TabMoved(workspace)
    _ = client.sort_tabs(workspace_names={workspace}, record=record)
    return placed


def move_unit(slug: str, unit: str, new_showrunner: str, *, identity: UnitIdentity = _LOCATE_UNIT) -> UnitMoveResult:
    """Change only membership, then reconcile the live tab with its parent's workspace."""
    with staff.operation():
        record = staff.snapshot()
        selector = staff_sources.UnitMember(slug, unit)
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
            raise staff.StaffRefused(f"{slug}/{unit}: no matching unit staff row")
        if isinstance(parent, staff_sources.NoSuchMember) or parent.row.kind != "showrunner":
            raise staff.StaffRefused(f"{new_showrunner}: no matching showrunner staff row")
        row = staff.move(found.row.id, parent.row.id)
        try:
            current = replace(record, rows=tuple(row if member.id == row.id else member for member in record.rows))
            tab = _place_tab(slug, unit, row, parent.row.name, current)
        except (OSError, ValueError, herdr.HerdrFailure) as error:
            raise staff.StaffUnreadable(f"{slug}/{unit}: tab was not moved: {error}") from error
        return UnitMoveResult(row, found.row.parent != row.parent, tab)


def report(result: UnitMoveResult, *, json_output: bool = False) -> int:
    """Use the same done/no-live-tab output in the staff and transfer commands."""
    if isinstance(result.tab, NoLiveTab):
        message = f"done: {result.row.name}: tab was not moved (no live pane); staff parent is {result.tab.workspace}"
        print(message, file=sys.stderr if json_output else sys.stdout)
    elif not json_output:
        print(f"done: {result.row.name} under {result.tab.workspace}")
    if json_output:
        print(json.dumps(result.row.to_json(), sort_keys=True))
    return 0


def cli_move(target: str, new_showrunner: str, *, json_output: bool = False) -> int:
    def apply() -> int:
        parts = target.split("/")
        if len(parts) != 2 or not all(parts) or not new_showrunner:
            raise staff.StaffRefused("move needs <showrunner slug>/<unit> <new showrunner slug>")
        return report(move_unit(parts[0], parts[1], new_showrunner), json_output=json_output)

    return staff.cli_result(apply)


def main(argv: list[str] | None = None) -> int:
    return staff.main(["move", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    raise SystemExit(main())
