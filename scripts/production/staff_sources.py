#!/usr/bin/env python3
"""Match production members and import missing staff once; remove this importer on 2026-11-13."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# These source modules still use sibling imports; keep their existing module identity.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import production_units  # noqa: E402
import showrunners  # noqa: E402
import unit_names  # noqa: E402
from scripts.production import staff  # noqa: E402
from scripts.shutdown import pane_record  # noqa: E402
import herdr  # noqa: E402


@dataclass(frozen=True)
class ShowrunnerMember:
    slug: str


@dataclass(frozen=True)
class UnitMember:
    slug: str
    unit: str


MemberSelector = ShowrunnerMember | UnitMember


@dataclass(frozen=True)
class MemberFound:
    row: staff.StaffRow


@dataclass(frozen=True)
class NoSuchMember:
    selector: MemberSelector


@dataclass(frozen=True)
class _MemberSources:
    productions: dict[str, showrunners.ShowrunnerSource]
    launches: tuple[pane_record.PaneLaunch, ...]

    def member(self, selector: MemberSelector, record: staff.StaffSnapshot) -> MemberFound | NoSuchMember:
        source = self.productions.get(selector.slug)
        if isinstance(selector, ShowrunnerMember):
            if source is None:
                return NoSuchMember(selector)
            launches = [launch for launch in self.launches
                        if f"session:{launch['session_id']}" == source.timer.target]
        else:
            launches = [launch for launch in self.launches
                        if launch["environment"].get("SHOWRUNNER_UNIT") == selector.slug
                        and launch["environment"].get("SHOWRUNNER_UNIT_ID") == selector.unit]
        if launches:
            newest = max(launches, key=lambda launch: launch["written_at"])
            staff_id = newest["environment"].get("STAFF_ID", "")
            if staff_id:
                row = record.by_id(staff.StaffId(staff_id))
                kind = "showrunner" if isinstance(selector, ShowrunnerMember) else "unit"
                return MemberFound(row) if row is not None and row.kind == kind else NoSuchMember(selector)
        if isinstance(selector, ShowrunnerMember):
            assert source is not None
            row = record.by_path(showrunners.space_from_text(selector.slug, source.document))
        else:
            parent = self.member(ShowrunnerMember(selector.slug), record)
            if isinstance(parent, NoSuchMember):
                return NoSuchMember(selector)
            row = record.by_path(f"{parent.row.name}/{unit_names.name(selector.unit)}")
        return MemberFound(row) if row is not None else NoSuchMember(selector)


def _member_sources() -> _MemberSources:
    session = herdr.shared_session()
    try:
        launches = pane_record.read_strict(session)
    except OSError as error:
        raise staff.StaffUnreadable(f"{pane_record.record_path(session)}: {error}") from error
    except ValueError as error:
        raise staff.StaffUnreadable(str(error)) from error
    sources = showrunners.read_sources(unreadable=showrunners.UnreadableTimerPolicy.SKIP)
    return _MemberSources({source.timer.slug: source for source in sources}, tuple(launches.values()))


def member(selector: MemberSelector, record: staff.StaffSnapshot) -> MemberFound | NoSuchMember:
    """Resolve the newest launch identity first, converting missing rows at the boundary."""
    return _member_sources().member(selector, record)


def showrunner_members(record: staff.StaffSnapshot) -> dict[str, staff.StaffRow]:
    """Resolve local production slugs to rows with the same launch-first matching rule."""
    sources = _member_sources()
    found: dict[str, staff.StaffRow] = {}
    for slug in sources.productions:
        selected = sources.member(ShowrunnerMember(slug), record)
        if isinstance(selected, MemberFound):
            found[slug] = selected.row
    return found


@dataclass(frozen=True)
class UnitProduction:
    """The current parent and its source document, resolved from the staff edge."""

    slug: str
    doc: str


def _unit_member(unit: str, session_id: str, record: staff.StaffSnapshot,
                 sources: _MemberSources) -> staff.StaffRow:
    launches = [launch for launch in sources.launches
                if session_id and launch["session_id"] == session_id]
    if launches:
        environment = max(launches, key=lambda launch: launch["written_at"])["environment"]
        found = sources.member(UnitMember(environment.get("SHOWRUNNER_UNIT", ""),
                                          environment.get("SHOWRUNNER_UNIT_ID", unit)), record)
        members = [found.row] if isinstance(found, MemberFound) else []
    else:
        members = list({found.row.id: found.row for slug in sources.productions
                        if isinstance(found := sources.member(UnitMember(slug, unit), record), MemberFound)}.values())
    if len(members) != 1:
        raise staff.StaffUnreadable(f"{unit}: no unique staff entry")
    return members[0]


def unit_parent(unit: str, session_id: str = "") -> UnitProduction:
    """Resolve the current staff edge; old launch marks supply identity only."""
    record, sources = staff.snapshot(), _member_sources()
    parent = _unit_member(unit, session_id, record, sources).parent
    parents = [source for slug, source in sources.productions.items()
               if isinstance(found := sources.member(ShowrunnerMember(slug), record), MemberFound)
               and found.row.id == parent]
    if len(parents) != 1:
        raise staff.StaffUnreadable(f"{unit}: staff parent has no unique production document")
    return UnitProduction(parents[0].timer.slug, str(parents[0].timer.doc))


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
            marks = production_units.SessionMarks(slug)
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
                        or production_units.unit_is_retired(unit_cells, marks)):
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
