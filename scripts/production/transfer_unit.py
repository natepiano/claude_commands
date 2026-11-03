#!/usr/bin/env python3
"""Change a unit's staff parent and move its Units row."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scripts.production import production_units, showrunners, staff, staff_sources, unit_move, unit_names
from scripts.lib.atomic_file import write_text_atomic
from scripts.production.rename_state import RenameRefused


@dataclass(frozen=True)
class UnitTransfer:
    member: staff.StaffRow
    parent: staff.StaffRow
    giving: production_units.Production
    receiving: production_units.Production
    unit: str


def _production(slug: str) -> production_units.Production:
    checked = showrunners.checked_doc(showrunners.NOTIFIER_STATE_DIR / f"showrunner-{slug}")
    if isinstance(checked, showrunners.NoCheckedDoc):
        raise ValueError(f"{slug}: {checked.reason}")
    return production_units.read_production(checked.path)


def build(from_slug: str, given: str, to_doc: Path) -> UnitTransfer:
    giving = _production(from_slug)
    receiving = production_units.read_production(to_doc.expanduser().resolve())
    record = staff.snapshot()
    found = staff_sources.member(staff_sources.UnitMember(giving.slug, given), record)
    if isinstance(found, staff_sources.NoSuchMember):
        found = staff_sources.member(staff_sources.UnitMember(receiving.slug, given), record)
    parent = staff_sources.member(staff_sources.ShowrunnerMember(receiving.slug), record)
    if isinstance(found, staff_sources.NoSuchMember) or isinstance(parent, staff_sources.NoSuchMember):
        raise ValueError(f"{given}: no unit or destination staff row")
    return UnitTransfer(found.row, parent.row, giving, receiving, unit_names.unit_id(found.row.name))


def _row(lines: list[str], unit: str) -> tuple[int, dict[str, str]] | None:
    rows = production_units.unit_table(lines)
    row = next((row for row in rows if production_units.cell_value(row.get("Unit", "")) == unit), None)
    if row is None:
        return None
    end, _ = production_units.unit_rows(lines)
    for index in range(lines.index("## Units") + 1, end):
        if lines[index].startswith("|") and production_units.cell_value(
                production_units.row_cells(lines[index])[0]) == unit:
            return index, row
    return None


def apply(from_slug: str, unit: str, to_doc: Path) -> UnitTransfer:
    request = build(from_slug, unit, to_doc)
    with unit_move.folder_move_lock(request.member.id), staff.operation():
        source = request.giving.doc.read_text(encoding="utf-8").splitlines()
        target = request.receiving.doc.read_text(encoding="utf-8").splitlines()
        original = _row(source, request.unit)
        existing = _row(target, request.unit)
        if original is None and existing is None:
            raise ValueError(f"{request.unit}: no Units row in either production")
        changed = request.member.parent != request.parent.id
        if changed:
            _ = staff.move(request.member.id, request.parent.id)
        if request.giving.doc != request.receiving.doc:
            if original is not None and existing is None:
                headings = production_units.unit_headings(target)
                line = "| " + " | ".join(
                    "—" if heading == "Worktree" else original[1].get(heading, "—") for heading in headings) + " |"
                position, _ = production_units.unit_rows(target)
                target.insert(position, line)
                write_text_atomic(request.receiving.doc, "\n".join(target) + "\n")
                changed = True
            if original is not None:
                del source[original[0]]
                write_text_atomic(request.giving.doc, "\n".join(source) + "\n")
                changed = True
        print("done" if changed else "unchanged")
        return request


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("action", choices=("plan", "apply", "verify"))
    _ = parser.add_argument("from_slug")
    _ = parser.add_argument("unit")
    _ = parser.add_argument("to_doc", type=Path)
    _ = parser.add_argument("--scratch", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    def run() -> int:
        try:
            if cast(str, args.action) == "plan":
                request = build(cast(str, args.from_slug), cast(str, args.unit), cast(Path, args.to_doc))
                print(f"Move {request.unit} to {request.receiving.slug}")
            else:
                _ = apply(cast(str, args.from_slug), cast(str, args.unit), cast(Path, args.to_doc))
            return 0
        except (ValueError, OSError, RenameRefused, production_units.Refusal) as error:
            print(f"transfer_unit: {error}", file=sys.stderr)
            return 1
    return staff.cli_result(run)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
