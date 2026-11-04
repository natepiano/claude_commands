#!/usr/bin/env python3
"""Print local session and worker relationships from one staff snapshot.

The JSON output keeps schema 1. Staff supplies identities and parents.
Local session and process records supply current presence and state.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone
from typing import Literal, TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "message"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from scripts.astra import astra_roles  # noqa: E402
from scripts.production import staff, staff_sources, session_staff, worker_admission
from scripts.production import showrunners  # noqa: E402
from scripts.message import roster
from scripts.usage import agents_log
import showrunner_tree
import unit_lookup  # noqa: E402
import unit_runs  # noqa: E402
from sessions import running_sessions  # noqa: E402

Role = str
# A worker in either state has ended its thread; every other status is a live thread.
ENDED_STATUSES = frozenset({"ended", "failed"})
ROSTER_FILE = "mesh_roster.json"
SERVER_FILE = "mesh_server.json"
BOARD = Path(__file__).resolve().parent.parent / "direct" / "board.sh"


class Edge(TypedDict):
    manager: str
    member: str
    role: Role


class AstraEdge(Edge):
    codex_home: str
    state: Literal["busy", "idle"]


def _warn(text: str) -> None:
    print(f"production_tree: {text}", file=sys.stderr)


def _json_object(path: Path) -> dict[str, object]:
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, ValueError):
        return {}
    return cast(dict[str, object], raw) if isinstance(raw, dict) else {}


def showrunner_sessions(*,
                        membership: staff.StaffSnapshot | showrunners.DiscoverMembership = showrunners.DISCOVER_MEMBERSHIP,
                        ) -> dict[str, str]:
    """Each production slug to its showrunner's session id, from its update timer, wherever it runs."""
    found: dict[str, str] = {}
    record = staff.initial_snapshot() if isinstance(membership, showrunners.DiscoverMembership) else membership
    for instance in sorted(showrunners.NOTIFIER_STATE_DIR.glob("showrunner-*")):
        try:
            lines = (instance / "conf").read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        target = next((line.removeprefix("TARGET=") for line in lines if line.startswith("TARGET=")), "")
        session_id = target.removeprefix("session:") if target.startswith("session:") else ""
        if session_id:
            owner = record.by_id(staff.StaffId(session_id))
            found[instance.name.removeprefix("showrunner-")] = owner.current_session if owner is not None else session_id
    return found


def showrunner_edges(sessions: dict[str, str], running: set[str], record: staff.StaffSnapshot) -> list[Edge]:
    return [
        Edge(manager=sessions[parent], member=sessions[child], role="showrunner")
        for child, parent in sorted(showrunner_tree.edges(set(sessions), record=record).items())
        if sessions[child] in running
    ]


def marked_units(*, record: staff.StaffSnapshot | None = None) -> unit_lookup.MarkedProductions | None:
    try:
        return unit_lookup.every_marked_unit(record=record)
    except OSError as error:
        _warn(f"unit marks unreadable: {error}")
        return None


def unit_edges(sessions: dict[str, str], running: set[str], marked: unit_lookup.MarkedProductions,
               record: staff.StaffSnapshot) -> list[Edge]:
    managers: dict[staff.StaffId, str] = {}
    for slug, row in staff_sources.showrunner_members(record).items():
        if slug in sessions:
            managers[row.id] = sessions[slug]
    edges: list[Edge] = []
    for slug, units in sorted(marked.units.items()):
        for unit in sorted(units):
            claude = units[unit].claude
            if not isinstance(claude, unit_lookup.LiveClaude) or claude.session_id not in running:
                continue
            identity = units[unit].staff_id
            reference = session_staff.LiveSession(identity) if identity else session_staff.NO_SESSION
            found = staff_sources.member(staff_sources.ObservedUnit(unit, reference), record)
            if isinstance(found, staff_sources.NoSuchMember) or isinstance(found.row.parent, staff.StaffRoot):
                continue
            manager = managers.get(found.row.parent.id, "")
            if manager:
                edges.append(Edge(manager=manager, member=claude.session_id, role="unit"))
    return edges


def _server_running(run: Path) -> bool:
    pid = _json_object(run / SERVER_FILE).get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def astra_edges(*, membership: staff.StaffSnapshot | None = None) -> list[AstraEdge]:
    """Read ownership from staff and process state from the Astra observer."""
    membership = membership if membership is not None else staff.local_snapshot()
    edges: list[AstraEdge] = []
    for observed in astra_roles.live_roles(membership):
        row = membership.by_current_session(observed["thread_id"])
        if row is None or not isinstance(row.parent, staff.ReportsTo):
            _warn(f"Astra thread {observed['thread_id']}: worker membership is unavailable")
            continue
        parent = membership.by_id(row.parent.id)
        backend = membership.backends.get(row.id)
        if parent is None or not isinstance(backend, staff.CodexBackend):
            _warn(f"{row.name}: worker parent or backend is unavailable")
            continue
        edges.append(AstraEdge(manager=parent.current_session, member=row.current_session,
                               role="astra-" + observed["role"], codex_home=str(backend.codex_home), state=observed["state"]))
    return edges


def worker_edges(running: set[str], astra_threads: set[str] | None = None, *,
                 membership: staff.StaffSnapshot | None = None) -> list[Edge]:
    membership = membership if membership is not None else staff.local_snapshot()
    excluded = astra_threads if astra_threads is not None else {edge["member"] for edge in astra_edges(membership=membership)}
    available: set[staff.StaffId] = set()
    owner_runs: dict[str, Path] = {}
    for root in unit_runs.run_roots():
        for path in sorted(root.glob("*/mesh_roster.json")):
            try:
                owner = worker_admission.parent(membership, run=path.parent)
            except ValueError:
                continue
            owner_runs[owner.current_session] = path.parent
            if not _server_running(path.parent):
                continue
            for value in _json_object(path).values():
                if not isinstance(value, dict):
                    continue
                entry = cast(dict[str, object], value)
                if entry.get("status") in ENDED_STATUSES:
                    continue
                available.add(staff.StaffId(str(entry.get("staff_id", ""))))
    edges: list[Edge] = []
    tasks = agents_log.assignments(agents_log.state_dir() / "agents.jsonl")
    for row in membership.find(kind="worker"):
        if not isinstance(row.parent, staff.ReportsTo):
            continue
        parent = membership.by_id(row.parent.id)
        if parent is None or parent.current_session not in running or row.current_session in excluded:
            continue
        backend = membership.backends.get(row.id)
        if (isinstance(backend, staff.CodexBackend) and row.id in available
                or isinstance(backend, staff.ClaudeBackend) and row.current_session in running):
            task = agents_log.role_at(tasks, row.current_session, datetime.now(timezone.utc))
            role = task.role if isinstance(task, agents_log.Assignment) else ""
            run = owner_runs.get(parent.current_session)
            if isinstance(backend, staff.ClaudeBackend) and run is not None:
                role_state = roster.claude_assignment(run, row, []).role
                role = role_state if isinstance(role_state, str) else ""
            if role in {"impl", "test", "review", "fix"}:
                edges.append(Edge(manager=parent.current_session, member=row.current_session, role=role))
    return edges


def tree() -> list[Edge]:
    running = {record["sessionId"] for record in running_sessions()}
    try:
        record = staff.local_snapshot()
    except staff.StaffUnreadable as error:
        _warn(f"staff unreadable: {error}")
        return []
    astra = astra_edges(membership=record)
    sessions = showrunner_sessions(membership=record)
    marked = marked_units(record=record)
    showrunner = showrunner_edges(sessions, running, record)
    directors = unit_edges(sessions, running, marked, record) if marked is not None else []
    return [*showrunner, *directors,
            *worker_edges(running, {edge["member"] for edge in astra}, membership=record), *astra]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    _ = parser.add_argument("--json", action="store_true", required=True, help="print the edges as JSON")
    _ = parser.parse_args(argv)
    print(json.dumps({"schema": 1, "edges": tree()}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
