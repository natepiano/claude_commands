"""Read worker titles, director identity, and detached command guards."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as package_root:
        exec(package_root.read(), {"__file__": package_root.name})
except FileNotFoundError:
    pass

import argparse
import sys

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from scripts.agents import detached_run_process, worker_lifecycle
from scripts.production import staff, worker_admission, session_staff
from scripts.lib import unit_runs

if TYPE_CHECKING:
    from scripts.production import unit_lookup
    from scripts.message.sessions import RegistryRead, SessionRecord
    from scripts.lib.names import JobKey

STATE = "progress_history_state.json"


class WorkerUnavailable(Exception):
    """No safe ownership transition can be made from the current evidence."""


class TitleRenderer(Protocol):
    def unit_worker_title(self, production: str, unit: str, job: JobKey, ordinal: int, address: str) -> str: ...

    def worker_title(self, session_dir: str, name: str, record: Mapping[str, object]) -> str: ...


@dataclass(frozen=True)
class LiveUnitDirector:
    session_id: str
    pid: int


@dataclass(frozen=True)
class NoLiveUnitDirector:
    """Neither the registry nor the unit mark identifies a live director."""


def running_director_sessions(registry: RegistryRead) -> list[SessionRecord]:
    """Running director candidates, newest first, including sessions without sockets."""
    from scripts.message import sessions

    return sessions.running_records(registry)


def resolve_live_unit_director(
    run: Path, record: staff.StaffSnapshot, sources: unit_lookup.MemberSources,
    running: Sequence[SessionRecord], dependencies: Callable[[Path], None], *,
    use_live_marks: bool = True,
) -> LiveUnitDirector | NoLiveUnitDirector:
    """Resolve the current director from worker membership or the unit staff row."""
    from scripts.production import unit_lookup

    dependencies(run / STATE)
    context = worker_lifecycle.read_json_object(run / STATE)
    owner = unit_runs.run_owner(context, record)
    parents = ({owner.id: owner} if isinstance(owner, staff.StaffRow)
               and owner.kind in ("unit", "showrunner") else {})
    if len(parents) != 1:
        return NoLiveUnitDirector()
    parent = next(iter(parents.values()))
    for session in running:
        if session["sessionId"] == parent.current_session:
            return LiveUnitDirector(session["sessionId"], session["pid"])
    production = record.parent_of(parent)
    if use_live_marks and production is not None:
        try:
            marked = unit_lookup.labeled_session(production.name, parent.name, record=record, sources=sources, running=running)
        except OSError:
            return NoLiveUnitDirector()
        if marked is not None and isinstance(marked.claude, unit_lookup.LiveClaude):
            if marked.claude.session_id == parent.current_session:
                return LiveUnitDirector(marked.claude.session_id, marked.claude.pid)
    return NoLiveUnitDirector()


def live_unit_director(run: Path) -> LiveUnitDirector | NoLiveUnitDirector:
    """Resolve the director across restarts with one owner snapshot."""
    from scripts.production import staff, unit_lookup
    from scripts.message import sessions

    dependencies = staff.RecordInputs()
    record = staff.initial_snapshot()
    registry = sessions.read_registry(dependencies=dependencies)
    running = running_director_sessions(registry)
    return resolve_live_unit_director(run, record, unit_lookup.member_sources(dependencies=dependencies),
                                      running, dependencies)


def run_has_live_director(run: Path) -> bool:
    return isinstance(live_unit_director(run), LiveUnitDirector)


def live_detached_runs(run: Path) -> tuple[tuple[Path, dict[str, object]], ...]:
    """Recorded supervisors only; no process discovery by name."""
    records: list[tuple[Path, dict[str, object]]] = []
    for path in sorted((run / "detached_runs").glob("*.json")):
        record = worker_lifecycle.read_json_object(path)
        pid = record.get("pid")
        if record.get("status") == "running" and isinstance(pid, int) and detached_run_process.supervises(pid, path):
            records.append((path, record))
    return tuple(records)


def protected_working_directories(run: Path) -> tuple[Path, ...]:
    membership = staff.local_snapshot()
    return tuple(session_staff.worker_directory(membership, staff.StaffId(str(record["staff_id"]))).resolve()
                 for _, record in retained_detached_runs(run))


def retained_detached_runs(run: Path) -> tuple[tuple[Path, dict[str, object]], ...]:
    """Commands owned by unit workers, including workers about to be paused."""
    membership = staff.local_snapshot()
    owner = worker_admission.parent(membership, run=run)
    members = membership.find(kind="worker", parent=owner.id)
    references = {str(row.id) for row in members}
    histories = {run, *(path for row in members for path in session_staff.worker_run_index(membership).get(row.id, ()))}
    return tuple((path, record) for history in sorted(histories)
                 for path, record in live_detached_runs(history)
                 if record.get("staff_id") in references)


def held_claude_sessions() -> set[str]:
    record = staff.local_snapshot()
    return {row.current_session for row in record.find(kind="worker")
            if isinstance(record.backends.get(row.id), staff.ClaudeBackend)}


def shown_name(run: Path, address: str, *, role: str = "", held_design_id: str = "", held_role: str = "") -> str:
    """Render current unit context and stored assignment without changing the address."""
    titles = cast(TitleRenderer, cast(object, import_module("scripts.message.roster")))
    record: dict[str, object] = {}
    if held_design_id:
        return titles.worker_title(str(run), address, {"held_design_id": held_design_id, "held_role": held_role})
    membership = staff.local_snapshot()
    member = worker_admission.member(run, address, membership)
    if member is None:
        return address
    parent = membership.parent_of(member)
    if parent is None:
        return address
    if parent.kind != "unit":
        return titles.worker_title(str(run), address, record)
    owner = membership.parent_of(parent)
    assigned_role = role
    if not assigned_role:
        assigned_role = member.role.value if isinstance(member.role, staff.KnownWorkerRole) else ""
    jobs: dict[str, JobKey] = {"impl": "implementer", "test": "tester", "fix": "repair_worker",
                               "review": "code_reviewer"}
    peers = [row.name for row in membership.find(kind="worker", parent=parent.id)]
    ordinal = peers.index(member.name) + 1
    return titles.unit_worker_title(owner.name if owner is not None else "", parent.name,
                                    jobs.get(assigned_role, "worker"),
                                    ordinal, address)


class WorkerTitleArguments(argparse.Namespace):
    session_dir: Path = Path()
    name: str = ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(required=True)
    display = commands.add_parser("shown-name")
    _ = display.add_argument("--session-dir", type=Path, required=True)
    _ = display.add_argument("--name", required=True)
    args = parser.parse_args(namespace=WorkerTitleArguments())
    try:
        print(shown_name(args.session_dir, args.name))
        return 0
    except (WorkerUnavailable, OSError, ValueError) as error:
        print(f"workers: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
