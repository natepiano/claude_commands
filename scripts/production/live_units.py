#!/usr/bin/env python3
"""List a showrunner's recorded units, each with its session now."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import sys
from collections.abc import Callable
from typing import NamedTuple

from scripts.production import session_staff, showrunners, staff
from scripts.production import unit_lookup
from scripts.accounts import session_state
from scripts.message import sessions
from scripts.herdr import herdr


class LiveUnit(NamedTuple):
    unit: str
    # None when no observed herdr session has the unit identity.
    session: unit_lookup.LabeledSession | None
    status: session_state.SessionState = "exited"
    member: staff.StaffRow | session_state.UnrecordedStaff = session_state.UnrecordedStaff()
    machine: str = ""

    @property
    def name(self) -> str:
        """The unit's session name now; empty when no Claude is known to run in it."""
        claude = self.session.claude if self.session is not None else None
        return claude.name if isinstance(claude, unit_lookup.LiveClaude) else ""


class UnitListing(NamedTuple):
    units: list[LiveUnit]
    anomalies: tuple[str, ...]


def read_units(production_name: str, *,
               check: Callable[[int, str], session_staff.ReportProcessIdentity] | None = None) -> UnitListing:
    """Return staff units and every anomaly from this observation."""
    record = staff.snapshot()
    sources = unit_lookup.member_sources()
    metadata = unit_lookup.unit_metadata(record, name=production_name)
    for problem in metadata.problems:
        print(f"live_units: {problem.name}: unavailable: {problem.reason}", file=sys.stderr)
    with session_staff.anomalies(check) as lines:
        every = unit_lookup.every_labeled_session(record=record, sources=sources)
    units = _with_status(_joined(production_name, every), session_staff.production_members(record).get(production_name, ()),
                         record=record)
    return UnitListing(units, tuple(lines))


def units_of(production_name: str) -> list[LiveUnit]:
    """List recorded unit children with their current panes."""
    listing = read_units(production_name)
    for line in listing.anomalies:
        print(line, file=sys.stderr)
    return listing.units


def _with_status(units: list[LiveUnit], rows: tuple[staff.StaffRow, ...], *,
                 record: staff.StaffSnapshot) -> list[LiveUnit]:
    detached = session_state.running_detached_work(record)
    members = {row.name: row for row in rows}
    activities = {session["sessionId"]: session_state.LiveActivity(session.get("status", ""))
                  for session in sessions.running_sessions()}
    result: list[LiveUnit] = []
    for unit in units:
        row = members.get(unit.unit)
        activity = activities.get(row.current_session, session_state.NoLiveSession()) if row is not None else session_state.NoLiveSession()
        if isinstance(activity, session_state.NoLiveSession) and unit.session is not None:
            observed = unit.session
            if (isinstance(observed.claude, unit_lookup.LiveClaude)
                    or (isinstance(observed.process, unit_lookup.ObservedProcess)
                        and observed.process.pid > 0
                        and (herdr.is_codex_process([observed.process.command])
                             or herdr.is_claude_process([observed.process.command])))):
                activity = session_state.LiveActivity("idle")
        member = row if row is not None else session_state.UnrecordedStaff()
        state = session_state.classify(member, activity, detached=detached)
        machine = staff.worktree_machine(row) if row is not None else ""
        result.append(unit._replace(status=state, member=member, machine=machine))
    return result


def units_in(production_name: str, every: unit_lookup.LabeledProductions) -> list[LiveUnit]:
    """Join the named production to the caller's session snapshot."""
    units = _joined(production_name, every)
    if not units:
        return units
    record = staff.snapshot()
    return _with_status(units, session_staff.production_members(record).get(production_name, ()), record=record)


def _joined(production_name: str, every: unit_lookup.LabeledProductions) -> list[LiveUnit]:
    """Return units in staff row order, each with its pane."""
    marked = every.status_units(production_name)
    return [LiveUnit(unit, marked.get(unit))
            for unit in every.members.get(production_name, ())]


def live_unit_ids(runner: showrunners.Showrunner) -> list[str]:
    """The staff ids of the showrunner's units that have a live Claude, read through one staff snapshot.

    A herdr that cannot say which sessions exist leaves the list empty and says why.
    """
    record = staff.snapshot()
    try:
        return [str(member.id) for unit in units_of(runner["name"])
                if unit.session is not None and isinstance(unit.session.claude, unit_lookup.LiveClaude)
                and (member := record.by_current_session(unit.session.claude.session_id)) is not None]
    except OSError as error:
        print(f"live_units: the units of {runner['name']} could not be listed: {error}", file=sys.stderr)
        return []


def main(arguments: list[str]) -> int:
    """Print one line per unit: unit id, pane, whether Claude runs (live, stopped, unknown, gone), name."""
    status = arguments[:1] == ["--status"]
    if status:
        arguments = arguments[1:]
    try:
        match arguments:
            case ["--production", name]:
                record = staff.snapshot()
                units = units_in(name, unit_lookup.every_labeled_session(record=record))
            case [showrunner] if not showrunner.startswith("-"):
                units = units_of(showrunners.named(showrunner)["name"])
            case _:
                print("usage: live_units.py --production <staff name>", file=sys.stderr)
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
