#!/usr/bin/env python3
"""List a showrunner's recorded units, each with its session now."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.production import showrunners
import unit_lookup
from production_units import order_unit_names


class LiveUnit(NamedTuple):
    unit: str
    # None when no herdr session carries the unit's mark.
    session: unit_lookup.MarkedUnit | None

    @property
    def name(self) -> str:
        """The unit's session name now; empty when no Claude is known to run in it."""
        claude = self.session.claude if self.session is not None else None
        return claude.name if isinstance(claude, unit_lookup.LiveClaude) else ""


def production_units(doc: Path) -> list[LiveUnit]:
    """Every recorded unit child, joined with the current pane when one exists."""
    return units_in(doc, unit_lookup.every_marked_unit())


def units_in(doc: Path, every: unit_lookup.MarkedProductions) -> list[LiveUnit]:
    """Join one production to a caller's membership and pane snapshot."""
    slug = showrunners.production_slug(str(doc))
    members = every.members.get(slug, ())
    offline = set(members) - every.units.get(slug, {}).keys()
    names = order_unit_names(doc.read_text(encoding="utf-8").splitlines(), members, offline=offline)
    return _joined(slug, names, every)


def _joined(slug: str, units: list[str], every: unit_lookup.MarkedProductions) -> list[LiveUnit]:
    if fault := every.production_fault(slug):
        raise OSError(fault)
    marked = every.units.get(slug, {})
    return [LiveUnit(unit, marked.get(unit)) for unit in units]


def units_of(runner: showrunners.Showrunner) -> list[LiveUnit]:
    """The showrunner's units. A showrunner whose update timer names no doc has none to list."""
    return production_units(Path(runner["doc"])) if runner["doc"] else []


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
    try:
        match arguments:
            case ["--production", doc]:
                units = production_units(Path(doc))
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
        print("\t".join((unit.unit, unit_lookup.place_text(unit.session.place) if unit.session is not None else "",
                         running, unit.name)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
