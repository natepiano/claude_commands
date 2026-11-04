"""Copy the paths in production Owns cells into unit configurations."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from pathlib import Path
import re
import subprocess

from scripts.production import production_doc, showrunners, staff, unit_config, unit_names
from scripts.shutdown import remote


@dataclass
class Counts:
    written: int = 0
    already_current: int = 0
    board_free: int = 0
    refused: int = 0
    skipped: int = 0

    def report(self) -> str:
        return (f"written: {self.written}, already current: {self.already_current}, "
                f"board-free: {self.board_free}, refused: {self.refused}, skipped: {self.skipped}")


class Arguments(argparse.Namespace):
    dry_run: bool = False
    doc_at: list[str] = []


@dataclass(frozen=True)
class NoUnitsTable:
    """The document has no Units table."""


def units_table(text: str) -> list[dict[str, str]] | NoUnitsTable:
    """Read the Units table under its section heading."""
    in_units = False
    headings: list[str] = []
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        if re.match(r"^##\s+Units\s*$", line):
            in_units = True
            continue
        if in_units and line.startswith("## "):
            break
        if not in_units or not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not headings:
            if "Unit" in cells and "Owns" in cells:
                headings = cells
            continue
        if all(re.fullmatch(r":?-+:?", cell.replace(" ", "")) for cell in cells):
            continue
        rows.append(dict(zip(headings, cells)))
    return rows if headings else NoUnitsTable()


def document_at(doc: Path, revision: str) -> str:
    """Read the document from its repository at the specified revision."""
    folder = doc.parent
    root = subprocess.run(["git", "-C", str(folder), "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True, check=True).stdout.strip()
    relative = doc.resolve().relative_to(Path(root).resolve())
    return subprocess.run(["git", "-C", str(folder), "show", f"{revision}:{relative.as_posix()}"],
                          capture_output=True, text=True, check=True).stdout


def fill_unit(production: str, cells: dict[str, str], snapshot: staff.StaffSnapshot,
              owner: staff.StaffRow | None, *, dry_run: bool, counts: Counts) -> None:
    name = production_doc.cell_value(cells.get("Unit", ""))
    prefix = f"{production} {name}:"
    if cells.get("Plan", "").startswith("(retired"):
        counts.skipped += 1
        print(f"{prefix} retired; skipped")
        return
    try:
        identity = unit_names.unit_id(name)
        matches = [row for row in snapshot.rows if row.kind == "unit"
                   and unit_names.unit_id(row.name) == identity
                   and owner is not None and row.parent == owner.id]
        if len(matches) != 1:
            counts.skipped += 1
            print(f"{prefix} no unique staff row; skipped")
            return
        location = matches[0].worktree
        if isinstance(location, (staff.PresentWorktree, staff.RemovedWorktree)) and location.machine != remote.local_machine():
            counts.skipped += 1
            print(f"{prefix} on {location.machine}; run there")
            return
        if not isinstance(location, staff.PresentWorktree):
            counts.skipped += 1
            print(f"{prefix} no worktree; skipped")
            return
        config = unit_config.read(location.path, cleanup=not dry_run)
        if isinstance(config, unit_config.ConfigMissing):
            counts.skipped += 1
            print(f"{prefix} missing .unit.toml; skipped")
            return
        cell = cells.get("Owns", "")
        try:
            owned = unit_config.responsibilities_from_owns(cell)
        except unit_config.ConfigRefused:
            counts.refused += 1
            print(f"{prefix} refused: {cell}")
            return
        desired = replace(config, responsibilities=tuple(dict.fromkeys(
            [name for name in config.responsibilities if "/" not in name] + list(owned))))
        if unit_config.BOARD_FREE in owned:
            counts.board_free += 1
        if dry_run:
            if config == desired:
                counts.already_current += 1
                print(f"{prefix} already current")
            else:
                print(f"{prefix} would write: {', '.join(desired.responsibilities)}")
            return
        result = unit_config.write(location.path, desired)
        if isinstance(result, unit_config.AlreadyCurrent):
            counts.already_current += 1
            print(f"{prefix} already current")
        else:
            counts.written += 1
            print(f"{prefix} written: {', '.join(desired.responsibilities)}")
    except (unit_config.ConfigRefused, unit_names.BadName, OSError) as error:
        counts.refused += 1
        print(f"{prefix} refused: {error}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--dry-run", action="store_true")
    _ = parser.add_argument("--doc-at", action="append", default=[], metavar="PRODUCTION=REV")
    args = parser.parse_args(argv, namespace=Arguments())
    revisions: dict[str, str] = {}
    for option in args.doc_at:
        production, separator, revision = option.partition("=")
        if not separator or not production or not revision:
            parser.error("--doc-at requires PRODUCTION=REV")
        revisions[production] = revision
    counts = Counts()
    discovery_status = 0
    try:
        runners = showrunners.registered_showrunners()
        discovery_status = runners.exit_code
        snapshot = staff.snapshot()
        for runner in runners:
            production, doc = runner["slug"], Path(runner["doc"])
            try:
                text = (document_at(doc, revisions[production]) if production in revisions
                        else doc.read_text(encoding="utf-8"))
                rows = units_table(text)
                if isinstance(rows, NoUnitsTable):
                    print(f"{production}: no Units table")
                    continue
                owner = snapshot.by_doc(remote.local_machine(), doc)
                for cells in rows:
                    fill_unit(production, cells, snapshot, owner, dry_run=args.dry_run, counts=counts)
            except (OSError, ValueError, subprocess.CalledProcessError) as error:
                counts.refused += 1
                print(f"{production}: refused: {error}")
    except (OSError, ValueError) as error:
        counts.refused += 1
        print(f"owns_fill: refused: {error}")
    print(counts.report())
    return int(bool(counts.refused) or bool(discovery_status))


if __name__ == "__main__":
    raise SystemExit(main())
