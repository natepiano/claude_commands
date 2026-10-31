#!/usr/bin/env python3
"""Admit workers through staff, and fill missing rows from existing worker readers."""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr
from dataclasses import dataclass
import io
import os
from pathlib import Path
import re
import sys
from typing import override

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.production import staff, staff_sources, unit_lookup
from scripts.astra import design_roles
from scripts.direct import workers


class AdmissionRefused(Exception):
    """No worker may start until its staff membership is durable."""


def admit(name: str, run: Path, *, production: str = "", unit: str = "") -> staff.StaffRow:
    """Write the row before acquiring lifecycle, inbox or roster locks."""
    try:
        record = staff.snapshot()
        if not production and not unit:
            state = workers._read(run / workers.STATE)  # pyright: ignore[reportPrivateUsage]
            production = workers._text(state.get("production"))  # pyright: ignore[reportPrivateUsage]
            unit = workers._text(state.get("unit"))  # pyright: ignore[reportPrivateUsage]
        if production:
            selector = (staff_sources.UnitMember(production, unit) if unit
                        else staff_sources.ShowrunnerMember(production))
            found = staff_sources.member(selector, record)
            if isinstance(found, staff_sources.NoSuchMember):
                raise ValueError(f"no unit staff row for {production}/{unit}")
            parent = found.row
        else:
            parent = record.by_id(staff.StaffId(os.environ.get("STAFF_ID", "")))
            if parent is None or parent.kind != "unit":
                raise ValueError("no recorded unit parent")
        if parent not in (*staff.active(record, "unit"), *staff.active(record, "showrunner")):
            raise ValueError(f"parent {parent.id} is retired")
        return staff.ensure_worker(name, parent.id)
    except (staff.StaffRefused, staff.StaffUnreadable, staff.StaffNotFound, OSError, ValueError) as error:
        raise AdmissionRefused(f"staff: {name}: not opened: {error}".replace("\n", " ")) from error


@dataclass(frozen=True)
class WorkerSource:
    path: Path
    key: str


class FillSources(staff_sources._WorkerSources):  # pyright: ignore[reportPrivateUsage]
    """Keep source locations around the shared collector's decoded observations."""

    def __init__(self, record: staff.StaffSnapshot, sources: unit_lookup.MemberSources) -> None:
        self.source: WorkerSource = WorkerSource(Path("."), "record")
        self.locations: dict[tuple[str, str], WorkerSource] = {}
        super().__init__(record, sources)

    @override
    def add(self, observed: staff_sources.WorkerObservation) -> None:
        super().add(observed)
        self.locations[(observed.owner.key, observed.name)] = self.source

    @override
    def ledger(self, path: Path, owner: staff_sources.WorkerOwner | None = None) -> None:
        self.source = WorkerSource(path, "workers")
        super().ledger(path, owner)

    @override
    def roster(self, run: Path, owner: staff_sources.WorkerOwner | None = None) -> None:
        self.source = WorkerSource(run / workers.ROSTER, "workers")
        super().roster(run, owner)

    @override
    def claude_workers(self, run: Path) -> None:
        self.source = WorkerSource(run / "workers", "workers")
        super().claude_workers(run)

    @override
    def design(self, design_id: str) -> None:
        self.source = WorkerSource(design_roles.design_directory(design_id) / "design.json", "roles")
        super().design(design_id)

    @override
    def astra(self, path: Path) -> None:
        self.source = WorkerSource(path, "worker")
        super().astra(path)


def fill(*, dry_run: bool = False) -> int:
    problems: list[str] = []
    placed = 0
    with redirect_stderr(io.StringIO()) as errors:
        record = staff.snapshot()
        sources = FillSources(record, unit_lookup.member_sources())
        observations = sources.collect()
    for line in errors.getvalue().splitlines():
        line = line.removeprefix("staff sources: ")
        match = re.fullmatch(r"(.*) worker (.*?): (.*)", line)
        problems.append(f"{match[1]}:{match[2]} {match[2]}: {match[3]}" if match else f"{line}:record ?: source unavailable")
    active = {row.id for row in staff.active(record, "unit")}
    for observed in observations:
        location = sources.locations[(observed.owner.key, observed.name)]
        prefix = f"{location.path}:{location.key}/{observed.name} {observed.name}"
        parent = observed.owner.match
        if not isinstance(parent, staff.StaffRow) or parent.kind != "unit":
            problems.append(f"{prefix}: showrunner-held worker needs a unit parent ruling")
            continue
        if parent.id not in active:
            problems.append(f"{prefix}: unit {parent.id} is not live")
            continue
        if any(row.name == observed.name for row in record.find(kind="worker", parent=parent.id)):
            continue
        try:
            if not dry_run:
                _ = staff.ensure_worker(observed.name, parent.id)
            placed += 1
            print(f"{'would place' if dry_run else 'placed'} {observed.name} under {parent.id}")
        except (staff.StaffRefused, staff.StaffUnreadable, OSError) as error:
            problems.append(f"{prefix}: {error}")
    for problem in dict.fromkeys(problems):
        print(problem, file=sys.stderr)
    if not placed and not problems:
        print("unchanged")
    else:
        print(f"placed {placed}; listed {len(dict.fromkeys(problems))}")
    return 1 if problems else 0


class _Arguments(argparse.Namespace):
    command: str = ""
    dry_run: bool = False
    name: str = ""
    run: Path = Path(".")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=("fill", "admit"))
    _ = parser.add_argument("--dry-run", action="store_true")
    _ = parser.add_argument("--name", default="")
    _ = parser.add_argument("--run", type=Path, default=Path("."))
    args = parser.parse_args(argv, namespace=_Arguments())
    try:
        if args.command == "admit":
            _ = admit(args.name, args.run)
            return 0
        return fill(dry_run=args.dry_run)
    except (AdmissionRefused, staff.StaffUnreadable, staff.StaffRefused) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
