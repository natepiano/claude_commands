#!/usr/bin/env python3
"""Rename a staff unit, its folder, its Git branch, and its displays."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from pathlib import Path
import sys
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
for directory in ("production", "shutdown", "herdr", "direct", "lib", "style"):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / directory))

from scripts.production import production_units, staff, staff_sources, unit_names, unit_move
from scripts.lib.atomic_file import write_text_atomic
from scripts.shutdown import remote
from scripts.herdr import herdr
from scripts.production.rename_state import RenameRefused


@dataclass(frozen=True)
class UnitRename:
    member: staff.StaffRow
    name: str
    destination: Path


def plan(slug: str, old: str, new: str) -> UnitRename:
    record = staff.snapshot()
    found = staff_sources.member(staff_sources.UnitMember(slug, old), record)
    if isinstance(found, staff_sources.NoSuchMember):
        found = staff_sources.member(staff_sources.UnitMember(slug, new), record)
    if isinstance(found, staff_sources.NoSuchMember):
        raise ValueError(f"{slug}/{old}: no unit staff row")
    if isinstance(found.row.worktree, staff.WorktreeUnfilled):
        raise ValueError(f"{old}: the staff worktree is unfilled")
    if found.row.worktree.machine != remote.local_machine():
        raise ValueError(f"{old}: run rename on {found.row.worktree.machine}")
    name = unit_names.name(new)
    return UnitRename(found.row, name, Path.home() / "worktrees" / name)


def relocate_staff_worktree(member_id: staff.StaffId, worktree: Path) -> None:
    """Keep the location state when a production rename moves a unit."""
    row = staff.by_id(member_id)
    if row is None or isinstance(row.worktree, staff.WorktreeUnfilled):
        raise ValueError(f"{member_id}: no unit worktree")
    _ = staff.set_worktree(row.id, replace(row.worktree, path=worktree), expected=row.worktree)


def _units_display(row: staff.StaffRow, old: str) -> bool:
    """Set the Units display cells from the renamed staff row."""
    parent = staff.by_id(row.parent) if row.parent else None
    if parent is None or isinstance(parent.doc, staff.DocUnfilled):
        print(f"anomaly: {row.name}: the parent has no production document", file=sys.stderr)
        return False
    document = parent.doc.path
    text = document.read_text(encoding="utf-8")
    lines = text.splitlines()
    headings = production_units.unit_headings(lines)
    end, _ = production_units.unit_rows(lines)
    for index in range(lines.index("## Units") + 1, end):
        if not lines[index].startswith("|"):
            continue
        cells = production_units.row_cells(lines[index])
        displayed = production_units.cell_value(cells[0])
        if displayed in {"Unit", "---"} or set(displayed) <= {"-", ":"}:
            continue
        if unit_names.name(displayed) not in {unit_names.name(old), row.name}:
            continue
        for field, value in (("Unit", unit_names.unit_id(row.name)), ("Branch", f"`{row.name}`"), ("Worktree", "—")):
            if field in headings:
                cells[headings.index(field)] = value
        lines[index] = "| " + " | ".join(cells) + " |"
        updated = "\n".join(lines) + "\n"
        if updated != text:
            write_text_atomic(document, updated)
            return True
        return False
    print(f"anomaly: {row.name}: no Units display row", file=sys.stderr)
    return False


def apply(slug: str, old: str, new: str) -> UnitRename:
    with staff.operation():
        request = plan(slug, old, new)
        row = request.member
        changed = row.name != request.name
        if changed:
            row = staff.rename(row.id, request.name)
        location = row.worktree
        if isinstance(location, staff.WorktreeUnfilled):
            raise ValueError(f"{row.name}: the staff worktree is unfilled")
        if location.path != request.destination:
            if isinstance(location, staff.PresentWorktree):
                unit_move.move_folder(row.id, request.destination)
            else:
                request.destination.parent.mkdir(parents=True, exist_ok=True)
                if location.path.exists():
                    _ = unit_move.git(location.path, "worktree", "move", str(location.path), str(request.destination))
                relocate_staff_worktree(row.id, request.destination)
            changed = True
        if request.destination.is_dir():
            branch = unit_move.git(request.destination, "branch", "--show-current")
            if branch != request.name:
                _ = unit_move.git(request.destination, "branch", "-m", request.name)
                changed = True
        else:
            print(f"anomaly: folder is absent: {request.destination}", file=sys.stderr)
        try:
            changed = _units_display(row, old) or changed
        except (OSError, ValueError, production_units.Refusal) as error:
            print(f"anomaly: Units display: {error}", file=sys.stderr)
        try:
            client = herdr.Herdr(herdr.shared_session())
            for pane in client.panes():
                if pane.get("session_id") == row.current_session:
                    changed = client.retitle(pane["pane_id"], row.name) or changed
        except (OSError, ValueError, herdr.HerdrFailure) as error:
            print(f"anomaly: display: {error}", file=sys.stderr)
        print("done" if changed else "unchanged")
        return request


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("action", choices=("plan", "apply", "verify"))
    _ = parser.add_argument("slug")
    _ = parser.add_argument("old")
    _ = parser.add_argument("new")
    _ = parser.add_argument("--scratch", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    def run() -> int:
        try:
            if cast(str, args.action) == "plan":
                request = plan(cast(str, args.slug), cast(str, args.old), cast(str, args.new))
                print(f"Rename {request.member.name} to {request.name}; folder {request.destination}")
            else:
                _ = apply(cast(str, args.slug), cast(str, args.old), cast(str, args.new))
            return 0
        except (OSError, ValueError, RenameRefused, herdr.HerdrFailure) as error:
            print(f"rename_unit: {error}", file=sys.stderr)
            return 1
    return staff.cli_result(run)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
