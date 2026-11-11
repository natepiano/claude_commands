#!/usr/bin/env python3
"""Change a unit's staff parent."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from dataclasses import dataclass
import subprocess
import sys
from typing import cast


from scripts.production import showrunners, staff, staff_move, staff_sources
from scripts.production.production_doc import Production, Refusal, read_production


@dataclass(frozen=True)
class UnitTransfer:
    member: staff.StaffRow
    parent: staff.StaffRow
    receiving: Production
    unit: str


def build(from_name: str, given: str, to_name: str) -> UnitTransfer:
    record = staff.snapshot()
    giving = read_production(from_name, record=record)
    receiving = read_production(to_name, record=record)
    found = staff_sources.member(staff_sources.UnitMember(giving.name, given), record)
    if isinstance(found, staff_sources.NoSuchMember):
        found = staff_sources.member(staff_sources.UnitMember(receiving.name, given), record)
    parent = staff_sources.member(staff_sources.ShowrunnerMember(receiving.name), record)
    if isinstance(found, staff_sources.NoSuchMember) or isinstance(parent, staff_sources.NoSuchMember):
        raise ValueError(f"{given}: no unit or destination staff row")
    return UnitTransfer(found.row, parent.row, receiving, found.row.name)


def apply(from_name: str, unit: str, to_name: str) -> UnitTransfer:
    request = build(from_name, unit, to_name)
    with staff.row_operation(str(request.member.id)):
        changed = request.member.parent != staff.ReportsTo(request.parent.id)
        if changed:
            _ = staff.move(request.member.id, staff.ReportsTo(request.parent.id))
            notice = showrunners.send_to_address(
                str(request.member.id), f"transfer:{request.member.id}:{request.parent.id}",
                f"Your unit now belongs to production {request.receiving.name}.", recipient=request.unit)
            for line in notice.lines:
                print(line)
        place_unit(from_name, request)
        print("done" if changed else "unchanged")
        return request


def place_unit(from_name: str, request: UnitTransfer) -> None:
    """Place the retained unit tab in its parent's workspace."""
    _ = staff_move.move_unit(from_name, request.unit, request.receiving.name,
                             identity=staff_move.PinnedUnit(request.member.id, request.parent.id))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("action", choices=("plan", "apply", "verify"))
    _ = parser.add_argument("from_name")
    _ = parser.add_argument("unit")
    _ = parser.add_argument("to_name")
    _ = parser.add_argument("--scratch", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    def run() -> int:
        try:
            if cast(str, args.action) == "plan":
                request = build(cast(str, args.from_name), cast(str, args.unit), cast(str, args.to_name))
                print(f"Move {request.unit} to {request.receiving.name}")
            else:
                _ = apply(cast(str, args.from_name), cast(str, args.unit), cast(str, args.to_name))
            return 0
        except subprocess.TimeoutExpired as error:
            print(f"transfer_unit: error: {str(error).splitlines()[0]}", file=sys.stderr)
            return 1
        except (ValueError, OSError, Refusal) as error:
            print(f"transfer_unit: {error}", file=sys.stderr)
            return 1
    return staff.cli_result(run)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
