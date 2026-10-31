#!/usr/bin/env python3
"""Match production members and import missing staff once; remove this importer on 2026-11-13."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# These source modules still use sibling imports; keep their existing module identity.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import production_units  # noqa: E402
import showrunners  # noqa: E402
import unit_names  # noqa: E402
from scripts.production import staff  # noqa: E402


# Matchers live below production_units, so lower readers use the same join without an import cycle.
from unit_lookup import (  # noqa: E402
    ShowrunnerMember as ShowrunnerMember, UnitMember as UnitMember, ObservedUnit as ObservedUnit,
    MemberSelector as MemberSelector, MemberFound as MemberFound, NoSuchMember as NoSuchMember,
    UnitProduction as UnitProduction, MemberSources as _MemberSources,
    member_sources as _member_sources,
)
import unit_lookup  # noqa: E402


def member(selector: MemberSelector, record: staff.StaffSnapshot) -> MemberFound | NoSuchMember:
    """Resolve pane identity or current production membership through the shared join."""
    return _member_sources().member(selector, record)


def showrunner_members(record: staff.StaffSnapshot) -> dict[str, staff.StaffRow]:
    """Resolve local production slugs to rows with the same launch-first matching rule."""
    return _member_sources().showrunner_members(record)


def unit_parent(unit: str, session_id: str = "") -> UnitProduction:
    """Resolve the current staff edge; old launch marks supply identity only."""
    return unit_lookup.unit_parent(unit, session_id, record=staff.snapshot())


def _showrunner_key(slug: str) -> str:
    return f"showrunner:{slug}"


def _seed_member(key: str, kind: Literal["showrunner", "unit"], name: str, found: MemberFound | NoSuchMember,
                 parent: staff.SeedParent | staff.KeepSeedParent | staff.RootSeedParent) -> staff.SeedMember:
    return staff.SeedMember(key, kind, name if isinstance(found, NoSuchMember) else found.row.name,
                           staff.NewSeedMember() if isinstance(found, NoSuchMember) else found.row.id, parent)


def _collect(record: staff.StaffSnapshot, sources: _MemberSources) -> staff.SeedInput:
    members: list[staff.SeedMember] = []
    imported_unit_names = {row.name for row in record.find(kind="unit")}
    for slug, source in sources.productions.items():
        try:
            cells = production_units.unit_table(source.document.splitlines())
            found = sources.member(ShowrunnerMember(slug), record)
            production_members = [_seed_member(_showrunner_key(slug), "showrunner",
                                               showrunners.space_from_text(slug, source.document), found,
                                               staff.KeepSeedParent())]
            for unit_cells in cells:
                unit = production_units.cell_value(unit_cells.get("Unit", ""))
                name = unit_names.name(unit)
                selected = sources.member(UnitMember(slug, unit), record)
                if isinstance(selected, MemberFound):
                    production_members.append(_seed_member(f"unit:{slug}/{unit}", "unit", name, selected,
                                                           staff.KeepSeedParent()))
                    continue
                # A moved unit can remain in its old Units table. Import never recreates it there.
                if name in imported_unit_names:
                    continue
                path_text = production_units.cell_value(unit_cells.get("Worktree", ""))
                if (not path_text or not Path(path_text).expanduser().is_dir()
                        or production_units.plan_cell_is_retired(unit_cells.get("Plan", ""))):
                    continue
                production_members.append(_seed_member(f"unit:{slug}/{unit}", "unit", name, selected,
                                                       staff.SeedParent(_showrunner_key(slug))))
        except (OSError, UnicodeError, production_units.Refusal, unit_names.BadName) as error:
            print(f"seed: {slug} ({source.timer.doc}): {error}", file=sys.stderr)
            continue
        members.extend(production_members)
    return staff.SeedInput(tuple(members), ())


def _print_plan(seed_input: staff.SeedInput) -> bool:
    changed = False
    for entry in seed_input.members:
        if isinstance(entry.match, staff.NewSeedMember):
            action, identity = "add", entry.key
            changed = True
        else:
            action, identity = "keep", entry.match
        print(f"{action} {identity} {entry.kind} {entry.name}")
    return changed


def seed(*, dry_run: bool = False) -> None:
    """Import missing local members without moving or deleting any recorded member."""
    with staff.operation():
        record = staff.initial_snapshot()
        sources = _member_sources()
        seed_input = _collect(record, sources)
        changed = _print_plan(seed_input)
        if dry_run:
            if not changed:
                print("unchanged", file=sys.stderr)
        else:
            _ = staff.apply_seed(seed_input)


class _Arguments(argparse.Namespace):
    command: str = ""
    dry_run: bool = False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=["seed"])
    _ = parser.add_argument("--dry-run", action="store_true")
    arguments = _Arguments()
    _ = parser.parse_args(argv, namespace=arguments)

    def run() -> int:
        seed(dry_run=arguments.dry_run)
        return 0

    return staff.cli_result(run)


if __name__ == "__main__":
    raise SystemExit(main())
