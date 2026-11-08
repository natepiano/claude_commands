#!/usr/bin/env python3
"""Hold and release Astra roles, and list the threads that still hold them.

Records are local files under ASTRA_ROLES_STATE (default ~/.local/state/astra/roles).
Live roles use pane observations or a local process check.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib import process_start
from scripts.lib.atomic_file import write_text_atomic
from scripts.production import staff, session_staff
from scripts.herdr import worker_panes
from scripts.usage import agents_log

AstraRole = Literal["designer", "reviewer"]
RoleState = Literal["busy", "idle", "unknown"]


class RoleRecord(TypedDict):
    """The on-disk ownership record; null fields belong to the other launch mode."""

    staff_id: str
    held_at: str
    session_dir: str | None
    worker: str | None
    pid: int | None
    pid_start: NotRequired[str]
    design_id: NotRequired[str]
    role_id: NotRequired[str]


@dataclass(frozen=True)
class MeshWorker:
    """A hold that lasts until this mesh worker ends its thread."""

    session_dir: str
    worker: str


class CaptureProcessStart(Enum):
    CURRENT = "current"


@dataclass(frozen=True, init=False)
class ExecProcess:
    """A hold belongs to the original process, including its start identity."""

    pid: int
    start: process_start.ProcessStart

    def __init__(self, pid: int, start: process_start.ProcessStart | CaptureProcessStart = CaptureProcessStart.CURRENT) -> None:
        object.__setattr__(self, "pid", pid)
        object.__setattr__(self, "start", process_start.process_start(pid) if isinstance(start, CaptureProcessStart) else start)


@dataclass(frozen=True)
class HeldDesignRole:
    """A hold tied to a durable role, independent of run directories and process ids."""

    design_id: str
    role_id: str


class HeldRole(TypedDict):
    role: AstraRole
    staff_id: str
    thread_id: str
    manager: str
    held_at: str
    lifetime: MeshWorker | ExecProcess | HeldDesignRole


class LiveRole(HeldRole):
    state: RoleState


@dataclass
class RoleArguments(argparse.Namespace):
    """Command-line inputs, before hold validates their launch mode."""

    command: str = ""
    role: AstraRole = "designer"
    staff_id: str = ""
    session_dir: str | None = None
    worker: str | None = None
    pid: int | None = None
    json: bool = False


def state_root() -> Path:
    return Path(os.environ.get("ASTRA_ROLES_STATE") or "~/.local/state/astra/roles").expanduser()


def _record_path(staff_id: str) -> Path:
    if not staff_id or staff_id in (".", "..") or "/" in staff_id or "\\" in staff_id:
        raise ValueError("a staff id must be a file name")
    return state_root() / f"{staff_id}.json"


def _json_object(path: Path) -> dict[str, object]:
    value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return cast(dict[str, object], value)


def _mesh_worker(session_dir: Path, worker: str, *, membership: staff.StaffSnapshot) -> dict[str, object]:
    value = _json_object(session_dir / "mesh_roster.json").get(worker)
    if not isinstance(value, dict):
        raise ValueError(f"{session_dir}: roster has no worker {worker}")
    entry = cast(dict[str, object], value)
    if not session_staff.worker_session(entry, membership):
        raise ValueError(f"{session_dir}: worker {worker} has no thread")
    return entry


@dataclass(frozen=True)
class HoldWorkerUnavailable:
    reason: str


def hold(role: AstraRole, lifetime: ExecProcess | HeldDesignRole | MeshWorker,
         staff_id: str = "") -> HeldRole | HoldWorkerUnavailable:
    """Save a role with one explicit lifetime."""
    membership = staff.local_snapshot()
    if isinstance(lifetime, MeshWorker):
        if staff_id:
            raise ValueError("mesh holds resolve their worker staff row")
        directory = Path(lifetime.session_dir).expanduser().resolve()
        entry = _mesh_worker(directory, lifetime.worker, membership=membership)
        staff_id = str(entry.get("staff_id", ""))
        lifetime = MeshWorker(str(directory), lifetime.worker)
    elif isinstance(lifetime, HeldDesignRole):
        if not lifetime.design_id or lifetime.role_id != role:
            raise ValueError("durable hold differs from its role")
    elif isinstance(lifetime.pid, bool) or lifetime.pid <= 0 or not isinstance(lifetime.start, process_start.Started):
        raise ValueError("exec process start is unavailable; no hold was saved")
    member = membership.by_id(staff.StaffId(staff_id))
    if member is None or member.kind != "worker":
        reason = f"{staff_id}: Astra hold has no worker staff row"
        print(f"anomaly: {reason}", file=sys.stderr)
        return HoldWorkerUnavailable(reason)
    task_role = "review" if role == "reviewer" else "impl"
    task = agents_log.role_at(agents_log.assignments(agents_log.state_dir() / "agents.jsonl"),
                              member.current_session, datetime.now(timezone.utc))
    if not isinstance(task, agents_log.Assignment) or task.role != task_role:
        agents_log.append(member.current_session, task_role, None, datetime.now(timezone.utc))
    held = HeldRole(role=role, staff_id=member.id, thread_id=member.current_session,
                    manager="", held_at=datetime.now(timezone.utc).isoformat(), lifetime=lifetime)
    record = _role_record(held)
    path = _record_path(member.id)
    held = _parse_record(record, path, membership)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps(record))
    return held


def release(staff_id: str) -> None:
    """Remove a hold, including one whose thread has already stopped."""
    _record_path(staff_id).unlink(missing_ok=True)


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def _record_text(record: dict[str, object], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing or invalid {key}")
    return value


def _parse_record(record: dict[str, object] | RoleRecord, path: Path, membership: staff.StaffSnapshot) -> HeldRole:
    fields = cast(dict[str, object], record)
    expected = {"staff_id", "held_at", "session_dir", "worker", "pid"}
    if "pid_start" in fields:
        expected.add("pid_start")
    if "design_id" in fields:
        expected.update(("design_id", "role_id"))
    if set(fields) != expected:
        raise ValueError("invalid Astra hold fields")
    reference = fields.get("staff_id")
    member = membership.by_id(staff.StaffId(str(reference)))
    if member is None or member.kind != "worker" or not isinstance(member.parent, staff.ReportsTo):
        raise ValueError("Astra hold has no worker staff row")
    backend = membership.backends.get(member.id)
    parent = membership.by_id(member.parent.id)
    if not isinstance(backend, (staff.CodexBackend, staff.ClaudeBackend)) or parent is None:
        raise ValueError("Astra hold backend or parent is unavailable")
    role: AstraRole
    task = agents_log.role_at(agents_log.assignments(agents_log.state_dir() / "agents.jsonl"),
                              member.current_session, datetime.now(timezone.utc))
    if fields.get("role_id") in ("designer", "reviewer"):
        role = cast(AstraRole, fields["role_id"])
    elif isinstance(task, agents_log.Assignment) and task.role in ("impl", "review"):
        role = "reviewer" if task.role == "review" else "designer"
    else:
        raise ValueError("worker does not hold an Astra role")
    thread_id, manager = member.current_session, parent.current_session
    held_at = _record_text(fields, "held_at")
    if path.stem != member.id:
        raise ValueError("staff id differs from file name")
    lifetime = _role_lifetime(fields, role)
    return HeldRole(staff_id=member.id, role=role, thread_id=thread_id,
                    manager=manager, held_at=held_at, lifetime=lifetime)


def _role_lifetime(fields: dict[str, object], role: AstraRole) -> MeshWorker | ExecProcess | HeldDesignRole:
    session_dir, worker, pid = fields["session_dir"], fields["worker"], fields["pid"]
    lifetime: MeshWorker | ExecProcess | HeldDesignRole
    if "design_id" in fields:
        if session_dir is not None or worker is not None or pid is not None or fields["role_id"] != role:
            raise ValueError("invalid durable role lifetime")
        lifetime = HeldDesignRole(_record_text(fields, "design_id"), _record_text(fields, "role_id"))
    elif isinstance(session_dir, str) and session_dir.strip() and isinstance(worker, str) and worker.strip() and pid is None:
        lifetime = MeshWorker(session_dir, worker)
    elif session_dir is None and worker is None and isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
        saved_start = fields.get("pid_start")
        lifetime = ExecProcess(pid, process_start.Started(saved_start) if isinstance(saved_start, str) and saved_start
                               else process_start.Unreadable("legacy hold has no process start; release it explicitly"))
    else:
        raise ValueError("record needs a mesh worker or an exec pid")
    return lifetime


def _role_record(role: HeldRole) -> RoleRecord:
    lifetime = role["lifetime"]
    record = RoleRecord(staff_id=role["staff_id"], held_at=role["held_at"],
                      session_dir=lifetime.session_dir if isinstance(lifetime, MeshWorker) else None,
                      worker=lifetime.worker if isinstance(lifetime, MeshWorker) else None,
                      pid=lifetime.pid if isinstance(lifetime, ExecProcess) else None)
    if isinstance(lifetime, HeldDesignRole):
        record.update(design_id=lifetime.design_id, role_id=lifetime.role_id)
    if isinstance(lifetime, ExecProcess) and isinstance(lifetime.start, process_start.Started):
        record["pid_start"] = lifetime.start.start
    return record


def _worker_state(entry: dict[str, object]) -> RoleState:
    pid = entry.get("launcher_pid")
    return "busy" if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 and _pid_running(pid) else "idle"


class AllHeldRoles(Enum):
    ALL = "all held roles"


def live_roles(membership: staff.StaffSnapshot, *,
               selection: frozenset[staff.StaffId] | AllHeldRoles = AllHeldRoles.ALL) -> list[LiveRole]:
    """Return readable live holds with current state; remove holds known to have ended."""
    roles: list[LiveRole] = []
    parents = [row for row in membership.rows if row.kind in ("unit", "showrunner")]
    observations: dict[staff.StaffId, list[worker_panes.WorkerPane]] = {}
    scanned = False
    try:
        paths = sorted(path for path in state_root().glob("*.json")
                       if isinstance(selection, AllHeldRoles) or staff.StaffId(path.stem) in selection)
    except OSError as error:
        print(f"astra_roles: cannot read roles: {error}", file=sys.stderr)
        return roles
    for path in paths:
        try:
            record = _parse_record(_json_object(path), path, membership)
            lifetime = record["lifetime"]
            if isinstance(lifetime, HeldDesignRole):
                sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
                from scripts.astra import design_roles
                design = design_roles.read_design(lifetime.design_id)
                instance = next(role for role in design.roles if role.role == lifetime.role_id)
                if isinstance(instance.lifecycle, design_roles.RoleEnded):
                    path.unlink(missing_ok=True)
                    continue
                if isinstance(instance.lifecycle, design_roles.RoleNotOpened) or instance.lifecycle.staff_id != record["staff_id"]:
                    raise ValueError("durable instance differs from hold")
                if not scanned:
                    observations = worker_panes.observe_parents(parents, membership)
                    scanned = True
                parent = design_roles.worker_parent(design, instance.role, membership)
                matches = [item for item in observations.get(parent.id, []) if item.row.id == instance.lifecycle.staff_id]
                state = worker_panes.worker_state(matches[0]) if len(matches) == 1 else "unknown"
                record["manager"] = parent.current_session
            elif isinstance(lifetime, MeshWorker):
                session_dir = Path(lifetime.session_dir)
                if not session_dir.exists():
                    path.unlink(missing_ok=True)
                    continue
                roster = _json_object(session_dir / "mesh_roster.json")
                if lifetime.worker not in roster:
                    path.unlink(missing_ok=True)
                    continue
                value = roster[lifetime.worker]
                if not isinstance(value, dict):
                    raise ValueError("mesh worker is not a JSON object")
                entry = cast(dict[str, object], value)
                thread = session_staff.worker_session(entry, membership)
                if not thread:
                    raise ValueError("mesh worker has no thread")
                status = entry.get("status")
                if not isinstance(status, str) or not status:
                    raise ValueError("mesh worker has no status")
                if status in ("ended", "failed") or thread != record["thread_id"]:
                    path.unlink(missing_ok=True)
                    continue
                state = _worker_state(entry)
            else:
                observed = process_start.process_start(lifetime.pid)
                if (isinstance(observed, (process_start.Gone, process_start.Zombie))
                        or isinstance(observed, process_start.Started) and isinstance(lifetime.start, process_start.Started)
                        and observed.start != lifetime.start.start):
                    path.unlink(missing_ok=True)
                    continue
                if not isinstance(observed, process_start.Started) or not isinstance(lifetime.start, process_start.Started):
                    raise ValueError("exec process lifetime is unknown; hold retained")
                state = "busy"
            live = LiveRole(staff_id=record["staff_id"], role=record["role"], thread_id=record["thread_id"],
                                  manager=record["manager"],
                                  held_at=record["held_at"], lifetime=lifetime, state=state)
            roles.append(live)
        except (OSError, UnicodeError, ValueError) as error:
            message = f"astra_roles: cannot read {path}: {error}"
            print(message.replace("\n", " ").replace("\r", " "), file=sys.stderr)
    return roles


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    holding = commands.add_parser("hold")
    _ = holding.add_argument("role", choices=("designer", "reviewer"))
    for flag in ("session-dir", "worker", "staff-id"):
        _ = holding.add_argument(f"--{flag}")
    _ = holding.add_argument("--pid", type=int)
    releasing = commands.add_parser("release")
    _ = releasing.add_argument("staff_id")
    listing = commands.add_parser("list")
    _ = listing.add_argument("--json", action="store_true")
    args = parser.parse_args(argv, namespace=RoleArguments())
    try:
        if args.command == "hold":
            if args.session_dir is not None and args.worker and args.pid is None and not args.staff_id:
                lifetime = MeshWorker(args.session_dir, args.worker)
            elif args.session_dir is None and args.worker is None and args.pid is not None:
                lifetime = ExecProcess(args.pid)
            else:
                raise ValueError("hold needs one mesh worker or one exec process")
            result = hold(args.role, lifetime, args.staff_id or "")
            if isinstance(result, HoldWorkerUnavailable):
                return 1
        elif args.command == "release":
            release(args.staff_id)
        else:
            roles = live_roles(staff.local_snapshot())
            if args.json:
                print(json.dumps([_role_record(role) | {"state": role["state"]} for role in roles]))
            else:
                for record in roles:
                    print(f"{record['role']}\t{record['thread_id']}\t{record['manager']}\t{record['state']}")
    except (OSError, UnicodeError, ValueError) as error:
        print(f"astra_roles: {str(error).replace(chr(10), ' ')}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
