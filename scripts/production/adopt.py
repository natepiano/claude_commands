#!/usr/bin/env python3
"""Move one production from written-down unit session names to the lookup.

Its showrunner runs this once, in its own checkout. Before, a unit's session name was copied into
the production doc's Session column, the registry and the showrunner's saved report state. This
marks each unit's tmux session as that unit, moves the saved state from the old names to unit ids,
registers the doc and removes the column. It changes nothing unless every unit's session is found,
and a second run finds nothing left to do.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple, cast

import add_unit
import rename_state
import showrunners
import unit_lookup
from unit_lookup import UnitState

from sessions import UnreadableSessionRecord, live_session, read_session  # noqa: E402

COLUMN = "Session"


class Marked(NamedTuple):
    """The unit's tmux session already carries its mark, set by hand before this run."""

    pane: str
    state: UnitState


class ToMark(NamedTuple):
    # The tmux session id ("$3") and its label now.
    target: str
    label: str


class StatedGone(NamedTuple):
    """The showrunner said this unit has no session now."""


class NotFound(NamedTuple):
    pass


class UnitRow(NamedTuple):
    unit: str
    old_name: str
    session: Marked | ToMark | StatedGone | NotFound


def names_to_panes() -> dict[str, set[str]]:
    """Each name a live Claude has or had, to the tmux panes of the Claudes that carry it."""
    panes: dict[str, set[str]] = {}
    for path in sorted(path for path in unit_lookup.sessions_dir().iterdir() if path.suffix == ".json"):
        record = read_session(path)
        if isinstance(record, UnreadableSessionRecord) or not live_session(record):
            continue
        pane = record["tmux"].rpartition(".")[2]
        if not pane:
            continue
        former = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8"))).get("formerNames")
        earlier = [name for name in cast(list[object], former) if isinstance(name, str)] if isinstance(former, list) else []
        for name in (record["name"], *earlier):
            panes.setdefault(name, set()).add(pane)
    return panes


def unit_rows(production: add_unit.Production, lines: list[str], gone: set[str]) -> list[UnitRow]:
    """Every row that is not retired, with the tmux session its old name leads to."""
    marked = unit_lookup.marked_units(production.slug)
    tmux = unit_lookup.tmux_sessions()
    owner = {pane: session for session, (_, panes) in tmux.items() for pane in panes}
    claudes = names_to_panes()
    rows: list[UnitRow] = []
    for cells in add_unit.unit_table(lines):
        unit, old_name = add_unit.cell_value(cells.get("Unit", "")), add_unit.cell_value(cells.get(COLUMN, ""))
        if not unit or add_unit.plan_cell_is_retired(cells.get("Plan", "")):
            continue
        # The session of a Claude that has or had the name, or the tmux session still labelled with it.
        found = ({owner[pane] for pane in claudes.get(old_name, ()) if pane in owner}
                 | {session for session, (label, _) in tmux.items() if label == old_name})
        if unit in marked:
            rows.append(UnitRow(unit, old_name, Marked(marked[unit].pane, marked[unit].state)))
        elif len(found) == 1:
            target = next(iter(found))
            rows.append(UnitRow(unit, old_name, ToMark(target, tmux[target][0])))
        elif unit in gone:
            rows.append(UnitRow(unit, old_name, StatedGone()))
        elif not found and not add_unit.worktree_is_linked(add_unit.cell_value(cells.get("Worktree", ""))):
            # Neither a session nor a worktree of its own: the unit is retired, as every reader takes it.
            continue
        else:
            rows.append(UnitRow(unit, old_name, NotFound()))
    return rows


def registry_states(showrunner: str) -> dict[str, UnitState]:
    """The run state the registry held under each unit session name, while it still listed units."""
    try:
        data = cast(dict[str, object], json.loads(showrunners.CONFIG.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}
    states: dict[str, UnitState] = {}
    for runner in cast(list[dict[str, object]], data.get("showrunners") or []):
        if runner.get("session") != showrunner:
            continue
        for item in cast(list[object], runner.get("units") or []):
            entry = cast(dict[str, object], item) if isinstance(item, dict) else {}
            name, status = entry.get("session"), entry.get("status")
            if isinstance(name, str) and status in [state.value for state in UnitState]:
                states[name] = UnitState(status)
    return states


def without_column(lines: list[str]) -> list[str]:
    """The doc with the Units table's Session column removed."""
    index = add_unit.unit_headings(lines).index(COLUMN)
    start = lines.index("## Units")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    changed = list(lines)
    for position in range(start + 1, end):
        if lines[position].startswith("|"):
            cells = add_unit.row_cells(lines[position])
            changed[position] = "| " + " | ".join(cells[:index] + cells[index + 1:]) + " |"
    return changed


def adopt(production: add_unit.Production, scratch: Path, gone: set[str]) -> int:
    text = production.doc.read_text(encoding="utf-8")
    lines = text.splitlines()
    if COLUMN not in add_unit.unit_headings(lines):
        showrunners.change("add", production.showrunner_session, production.zone.key, str(production.doc))
        print(f"adopt: nothing to do — the Units table of {production.doc.name} has no {COLUMN} column")
        return 0
    rows = unit_rows(production, lines, gone)
    missing = [row for row in rows if isinstance(row.session, NotFound)]
    if missing:
        print("adopt: nothing was changed — no one tmux session was found for:")
        for row in missing:
            print(f"  {row.unit} (written down as {row.old_name or 'nothing'})")
        print("For a unit whose session runs under a name not written down, mark it and run adopt again:")
        print(f"  unit_lookup.py mark {production.slug} <unit> <tmux session>")
        print("For a unit that has no session now, run adopt again with: --gone <unit>")
        return 1
    states = registry_states(production.showrunner_session)
    for row in rows:
        if isinstance(row.session, ToMark):
            unit_lookup.mark(row.session.target, production.slug, row.unit)
            if row.old_name in states:
                unit_lookup.set_state(row.session.target, states[row.old_name])
            print(f"adopt: {row.unit} is the tmux session {row.session.label}")
        elif isinstance(row.session, Marked):
            # A mark set by hand says nothing of the run state, so the session reads as running.
            held = states.get(row.old_name, UnitState.RUNNING)
            if row.session.state is UnitState.RUNNING and held is not UnitState.RUNNING:
                unit_lookup.set_state(row.session.pane, held)
            print(f"adopt: {row.unit} was already marked")
        elif isinstance(row.session, StatedGone):
            print(f"adopt: {row.unit} has no session now")
        if row.old_name and row.old_name != row.unit:
            for store in rename_state.rename_scratch(row.old_name, row.unit, scratch):
                print(f"adopt: {store}: {row.old_name} is now {row.unit}")
    showrunners.change("add", production.showrunner_session, production.zone.key, str(production.doc))
    # Last, so a run stopped part way still has the old names to finish from.
    _ = production.doc.write_text("\n".join(without_column(lines)) + ("\n" if text.endswith("\n") else ""),
                                  encoding="utf-8")
    add_unit.commit_paths(production, [production.doc],
                          f"production({production.slug}): unit sessions are looked up, not written down")
    print(f"adopt: done — {len(rows)} units; the {COLUMN} column is removed and the change is committed")
    return 0


def main(argv: list[str]) -> int:
    """Exit 0 adopted or nothing to do, 1 a unit's session was not found, 2 it could not be done."""
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("production", help="the production doc")
    _ = parser.add_argument("--scratch", required=True, help="the showrunner's scratchpad directory")
    _ = parser.add_argument("--gone", action="append", default=[], metavar="UNIT",
                            help="a unit that has no session now; repeat for each")
    arguments = parser.parse_args(argv)
    try:
        production = add_unit.read_production(Path(cast(str, arguments.production)))
        return adopt(production, Path(cast(str, arguments.scratch)), set(cast(list[str], arguments.gone)))
    except (add_unit.Refusal, rename_state.RenameRefused, subprocess.CalledProcessError, OSError,
            ValueError) as error:
        print(f"adopt: stopped — {error}. Run it again once that is put right; it picks up where it stopped.",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
