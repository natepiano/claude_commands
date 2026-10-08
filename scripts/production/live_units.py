#!/usr/bin/env python3
"""List a showrunner's units: the live rows of its production doc, each with its session now."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import NamedTuple

import showrunners
import unit_lookup
from add_unit import Refusal, cell_value, live_unit_table


class LiveUnit(NamedTuple):
    unit: str
    # None when no tmux session carries the unit's mark.
    session: unit_lookup.MarkedUnit | None

    @property
    def name(self) -> str:
        """The unit's session name now; empty when no Claude is known to run in it."""
        claude = self.session.claude if self.session is not None else None
        return claude.name if isinstance(claude, unit_lookup.LiveClaude) else ""


def production_units(doc: Path) -> list[LiveUnit]:
    """Every unit the doc's Units table lists and has not retired, found by its mark.

    Raises OSError when the doc cannot be read or tmux cannot say which sessions exist.
    """
    slug = showrunners.production_slug(str(doc))
    try:
        rows = live_unit_table(doc.read_text(encoding="utf-8").splitlines(), slug)
    except (UnicodeError, Refusal) as error:
        raise OSError(f"{doc}: {error}") from error
    marked = unit_lookup.marked_units(slug)
    units = [cell_value(cells.get("Unit", "")) for cells in rows]
    return [LiveUnit(unit, marked.get(unit)) for unit in units if unit]


def live_units(session: str) -> list[LiveUnit]:
    settings = showrunners.load_settings()
    runner = next((item for item in settings["showrunners"] if item["session"] == session), None)
    if runner is None:
        raise ValueError(f"showrunner absent from config: {session}")
    return production_units(Path(runner["doc"])) if runner["doc"] else []


def live_unit_names(session: str) -> list[str]:
    """The session names, as they are now, of the showrunner's units that have a live Claude.

    A tmux that cannot say which sessions exist leaves the list empty and says why.
    """
    try:
        return [unit.name for unit in live_units(session) if unit.name]
    except OSError as error:
        print(f"live_units: the units of {session} could not be listed: {error}", file=sys.stderr)
        return []


def main(arguments: list[str]) -> int:
    """Print one line per unit: unit id, pane, whether Claude runs (live, stopped, unknown, gone), name."""
    if len(arguments) != 1:
        print("usage: live_units.py <showrunner session>", file=sys.stderr)
        return 2
    try:
        units = live_units(arguments[0])
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    for unit in units:
        claude = unit.session.claude if unit.session is not None else None
        running = ("gone" if unit.session is None else "live" if isinstance(claude, unit_lookup.LiveClaude)
                   else "unknown" if isinstance(claude, unit_lookup.ClaudeUnknown) else "stopped")
        print("\t".join((unit.unit, unit.session.pane if unit.session is not None else "", running, unit.name)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
