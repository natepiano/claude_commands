#!/usr/bin/env python3
"""Match production members and seed staff from this machine's existing sources."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, replace
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


def _initial_snapshot() -> staff.StaffSnapshot:
    try:
        return staff.snapshot()
    except staff.StaffAbsent:
        return staff.StaffSnapshot(())


def _showrunner_key(slug: str) -> str:
    return f"showrunner:{slug}"


def _seed_member(key: str, kind: Literal["showrunner", "unit"], name: str, found: MemberFound | NoSuchMember,
                 parent: staff.SeedParent | staff.KeepSeedParent | staff.RootSeedParent) -> staff.SeedMember:
    return staff.SeedMember(key, kind, name if isinstance(found, NoSuchMember) else found.row.name,
                           staff.NewSeedMember() if isinstance(found, NoSuchMember) else found.row.id, parent)


def _collect(record: staff.StaffSnapshot, sources: _MemberSources) -> staff.SeedInput:
    members: dict[str, staff.SeedMember] = {}
    parent_slugs: dict[str, str] = {}
    present: set[staff.StaffId] = set()
    protected: set[staff.StaffId] = set()
    retire: set[staff.StaffId] = set()
    read_showrunners: set[staff.StaffId] = set()
    for slug, source in sources.productions.items():
        doc = source.timer.doc
        try:
            cells = production_units.unit_table(source.document.splitlines())
            parent_slug = showrunners.stored_parent(slug)
            found = sources.member(ShowrunnerMember(slug), record)
            production_members = [_seed_member(_showrunner_key(slug), "showrunner",
                                               showrunners.space_from_text(slug, source.document), found,
                                               staff.KeepSeedParent())]
            listed: set[staff.StaffId] = set()
            kept: set[staff.StaffId] = set()
            retired: set[staff.StaffId] = set()
            all_active_matched = True
            marks = production_units.SessionMarks(slug)
            for unit_cells in cells:
                unit = production_units.cell_value(unit_cells.get("Unit", ""))
                name = unit_names.name(unit)
                selected = sources.member(UnitMember(slug, unit), record)
                path_text = production_units.cell_value(unit_cells.get("Worktree", ""))
                local = bool(path_text) and Path(path_text).expanduser().is_dir()
                is_retired = production_units.unit_is_retired(unit_cells, marks)
                explicit_retirement = production_units.plan_cell_is_retired(unit_cells.get("Plan", ""))
                retirement_evidence = is_retired and (local or explicit_retirement)
                if isinstance(selected, MemberFound):
                    listed.add(selected.row.id)
                    if retirement_evidence:
                        retired.add(selected.row.id)
                    else:
                        # An absent local worktree alone says nothing about the other host.
                        kept.add(selected.row.id)
                elif not retirement_evidence:
                    all_active_matched = False
                if is_retired or not local:
                    continue
                production_members.append(_seed_member(f"unit:{slug}/{unit}", "unit", name, selected,
                                                       staff.SeedParent(_showrunner_key(slug))))
        except (OSError, UnicodeError, production_units.Refusal, unit_names.BadName) as error:
            print(f"seed: {slug} ({doc}): {error}", file=sys.stderr)
            continue
        for entry in production_members:
            if entry.key in members:
                raise staff.StaffRefused(f"duplicate source member: {entry.key}")
            members[entry.key] = entry
        parent_slugs[slug] = parent_slug
        if isinstance(found, MemberFound) and all_active_matched:
            read_showrunners.add(found.row.id)
        present.update(listed)
        protected.update(kept)
        retire.update(retired)
    edges = showrunners.parent_edges(parent_slugs)
    for slug, parent_slug in parent_slugs.items():
        entry = members[_showrunner_key(slug)]
        if parent_slug and parent_slug not in parent_slugs:
            desired: staff.SeedParent | staff.KeepSeedParent | staff.RootSeedParent = staff.KeepSeedParent()
        elif slug not in edges:
            desired = staff.RootSeedParent()
        else:
            key = _showrunner_key(parent_slug)
            if key not in members:
                found = sources.member(ShowrunnerMember(parent_slug), record)
                if isinstance(found, MemberFound):
                    members[key] = _seed_member(key, "showrunner", found.row.name, found,
                                                staff.KeepSeedParent())
            desired = staff.SeedParent(key) if key in members else staff.KeepSeedParent()
        members[entry.key] = replace(entry, parent=desired)
    for row in record.find(kind="unit"):
        if row.parent in read_showrunners and row.id not in present:
            retire.add(row.id)
    retirements: list[staff.SeedRetirement] = []
    for staff_id in sorted(retire - protected):
        row = record.by_id(staff_id)
        assert row is not None and row.parent is not None
        retirements.append(staff.SeedRetirement(staff_id, row.parent))
    return staff.SeedInput(tuple(members.values()), tuple(retirements))


def _print_plan(seed_input: staff.SeedInput, record: staff.StaffSnapshot, *, dry_run: bool) -> bool:
    changed = False
    entries = {entry.key: entry for entry in seed_input.members}
    for entry in seed_input.members:
        if isinstance(entry.match, staff.NewSeedMember):
            action, identity = "add", entry.key
        else:
            row = record.by_id(entry.match)
            assert row is not None
            identity = row.id
            action = "keep"
            if isinstance(entry.parent, staff.RootSeedParent) and row.parent is not None:
                action = "move"
            elif isinstance(entry.parent, staff.SeedParent):
                parent = entries[entry.parent.key]
                if isinstance(parent.match, staff.NewSeedMember) or parent.match != row.parent:
                    action = "move"
        changed = changed or action != "keep"
        print(f"{action} {identity} {entry.kind} {entry.name}")
    for retirement in seed_input.retire:
        staff_id = retirement.staff_id
        row = record.by_id(staff_id)
        assert row is not None
        if record.find(kind="worker", parent=staff_id):
            print(f"keep {row.id} unit {row.name}")
            if dry_run:
                print(f"seed: keeping unit {row.name}: worker rows remain", file=sys.stderr)
        else:
            changed = True
            print(f"retire {row.id} unit {row.name}")
    return changed


def seed(*, dry_run: bool = False) -> None:
    """Read local evidence under the operation lock and apply it in one owner write."""
    with staff.operation():
        record = _initial_snapshot()
        sources = _member_sources()
        seed_input = _collect(record, sources)
        changed = _print_plan(seed_input, record, dry_run=dry_run)
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
