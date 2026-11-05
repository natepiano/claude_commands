#!/usr/bin/env python3
"""Change a unit's staff parent."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
from typing import cast


from scripts.production import showrunners, staff, staff_move, staff_sources, unit_names
from scripts.production.production_doc import Production, Refusal, read_production
from scripts.production.rename_state import RenameRefused


@dataclass(frozen=True)
class UnitTransfer:
    member: staff.StaffRow
    parent: staff.StaffRow
    receiving: Production
    unit: str


def _production(slug: str) -> Production:
    checked = showrunners.checked_doc(showrunners.NOTIFIER_STATE_DIR / showrunners.timer_name(slug))
    if isinstance(checked, showrunners.NoCheckedDoc):
        raise ValueError(f"{slug}: {checked.reason}")
    return read_production(checked.path)


def build(from_slug: str, given: str, to_doc: Path) -> UnitTransfer:
    giving = _production(from_slug)
    receiving = read_production(to_doc.expanduser().resolve())
    record = staff.snapshot()
    found = staff_sources.member(staff_sources.UnitMember(giving.slug, given), record)
    if isinstance(found, staff_sources.NoSuchMember):
        found = staff_sources.member(staff_sources.UnitMember(receiving.slug, given), record)
    parent = staff_sources.member(staff_sources.ShowrunnerMember(receiving.slug), record)
    if isinstance(found, staff_sources.NoSuchMember) or isinstance(parent, staff_sources.NoSuchMember):
        raise ValueError(f"{given}: no unit or destination staff row")
    return UnitTransfer(found.row, parent.row, receiving, unit_names.unit_id(found.row.name))


def apply(from_slug: str, unit: str, to_doc: Path) -> UnitTransfer:
    request = build(from_slug, unit, to_doc)
    with staff.row_operation(str(request.member.id)):
        changed = request.member.parent != staff.ReportsTo(request.parent.id)
        if changed:
            _ = staff.move(request.member.id, staff.ReportsTo(request.parent.id))
            notice = showrunners.send_to_address(
                f"staff:{request.member.id}", f"transfer:{request.member.id}:{request.parent.id}",
                f"Your unit now belongs to production {request.receiving.slug}.", recipient=request.unit)
            for line in notice.lines:
                print(line)
        _ = staff_move.move_unit(from_slug, request.unit, request.receiving.slug,
                                 identity=staff_move.PinnedUnit(request.member.id, request.parent.id))
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
        except subprocess.TimeoutExpired as error:
            print(f"transfer_unit: error: {str(error).splitlines()[0]}", file=sys.stderr)
            return 1
        except (ValueError, OSError, RenameRefused, Refusal) as error:
            print(f"transfer_unit: {error}", file=sys.stderr)
            return 1
    return staff.cli_result(run)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
