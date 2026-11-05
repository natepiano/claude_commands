#!/usr/bin/env python3
"""List a showrunner's recorded units, each with its session now."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.production import showrunners, staff, unit_names
from scripts.shutdown import remote
import unit_lookup


class LiveUnit(NamedTuple):
    unit: str
    # None when no observed herdr session has the unit identity.
    session: unit_lookup.LabeledSession | None

    @property
    def name(self) -> str:
        """The unit's session name now; empty when no Claude is known to run in it."""
        claude = self.session.claude if self.session is not None else None
        return claude.name if isinstance(claude, unit_lookup.LiveClaude) else ""


def _production_slug(doc: Path, record: staff.StaffSnapshot,
                     sources: unit_lookup.MemberSources) -> str:
    owner = record.by_doc(remote.local_machine(), doc)
    slugs = [slug for slug, row in sources.showrunner_members(record).items() if row == owner]
    return (slugs[0] if len(slugs) == 1 else owner.name if owner is not None
            else showrunners.production_slug(str(doc)))


def units_of_doc(doc: Path) -> list[LiveUnit]:
    """List recorded unit children with their current panes."""
    record = staff.snapshot()
    sources = unit_lookup.member_sources()
    slug = _production_slug(doc, record, sources)
    metadata = unit_lookup.unit_metadata(record, sources, slug=slug)
    for problem in metadata.problems:
        print(f"live_units: {problem.name}: unavailable: {problem.reason}", file=sys.stderr)
    return _joined(slug, unit_lookup.every_labeled_session(record=record, sources=sources))


def units_in(doc: Path, every: unit_lookup.LabeledProductions) -> list[LiveUnit]:
    """Join a production through its recorded document and the caller's pane snapshot."""
    record = staff.snapshot()
    sources = unit_lookup.member_sources()
    return _joined(_production_slug(doc, record, sources), every)


def _joined(slug: str, every: unit_lookup.LabeledProductions) -> list[LiveUnit]:
    """Units in staff row order, each with its pane."""
    marked = every.status_units(slug)
    return [LiveUnit(unit if unit in marked else unit_names.unit_id(unit), marked.get(unit))
            for unit in every.members.get(slug, ())]


def units_of(runner: showrunners.Showrunner) -> list[LiveUnit]:
    """The showrunner's units. A showrunner whose update timer names no doc has none to list."""
    return units_of_doc(Path(runner["doc"])) if runner["doc"] else []


def live_unit_names(runner: showrunners.Showrunner) -> list[str]:
    """The session names, as they are now, of the showrunner's units that have a live Claude.

    A herdr that cannot say which sessions exist leaves the list empty and says why.
    """
    try:
        return [unit.name for unit in units_of(runner) if unit.name]
    except OSError as error:
        print(f"live_units: the units of {runner['slug']} could not be listed: {error}", file=sys.stderr)
        return []


def main(arguments: list[str]) -> int:
    """Print one line per unit: unit id, pane, whether Claude runs (live, stopped, unknown, gone), name."""
    status = arguments[:1] == ["--status"]
    if status:
        arguments = arguments[1:]
    try:
        match arguments:
            case ["--production", doc]:
                units = units_of_doc(Path(doc))
            # An update prompt written before a production was named by its doc names the showrunner.
            case [showrunner] if not showrunner.startswith("-"):
                units = units_of(showrunners.named(showrunner))
            case _:
                print("usage: live_units.py --production <production doc>", file=sys.stderr)
                return 2
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    for unit in units:
        claude = unit.session.claude if unit.session is not None else None
        running = ("gone" if unit.session is None else "live" if isinstance(claude, unit_lookup.LiveClaude)
                   else "unknown" if isinstance(claude, unit_lookup.ClaudeUnknown) else "stopped")
        columns = (unit.unit, unit_lookup.place_text(unit.session.place) if unit.session is not None else "", running, unit.name)
        if status:
            columns += (claude.session_id if isinstance(claude, unit_lookup.LiveClaude) else "",)
        print("\t".join(columns))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
