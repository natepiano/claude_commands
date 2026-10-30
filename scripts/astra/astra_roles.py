#!/usr/bin/env python3
"""Hold and release Astra roles, and list the threads that still hold them.

Records are local files under ASTRA_ROLES_STATE (default ~/.local/state/astra/roles).
Reading live roles needs only a mesh roster or a local process check.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib import durable_state

AstraRole = Literal["designer", "reviewer"]
RoleState = Literal["busy", "idle"]


class RoleRecord(TypedDict):
    """The on-disk ownership record; null fields belong to the other launch mode."""

    role: AstraRole
    thread_id: str
    codex_home: str
    manager: str
    held_at: str
    session_dir: str | None
    worker: str | None
    pid: int | None
    design_id: NotRequired[str]
    role_id: NotRequired[str]


@dataclass(frozen=True)
class MeshWorker:
    """A hold that lasts until this mesh worker ends its thread."""

    session_dir: str
    worker: str


@dataclass(frozen=True)
class ExecProcess:
    """A hold that lasts while this local exec process runs."""

    pid: int
    started: str = ""

    def __post_init__(self) -> None:
        if not self.started:
            object.__setattr__(self, "started", durable_state.ProcessLifetime.current(self.pid).started)


@dataclass(frozen=True)
class HeldDesignRole:
    """A hold tied to a durable role, independent of run directories and process ids."""

    design_id: str
    role_id: str


class HeldRole(TypedDict):
    role: AstraRole
    thread_id: str
    codex_home: str
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
    thread: str = ""
    codex_home: str = ""
    session_dir: str | None = None
    worker: str | None = None
    pid: int | None = None
    manager: str | None = None
    thread_id: str = ""
    json: bool = False


def state_root() -> Path:
    return Path(os.environ.get("ASTRA_ROLES_STATE") or "~/.local/state/astra/roles").expanduser()


def _record_path(thread_id: str) -> Path:
    if not thread_id or thread_id in (".", "..") or "/" in thread_id or "\\" in thread_id:
        raise ValueError("a thread id must be a file name")
    return state_root() / f"{thread_id}.json"


def _json_object(path: Path) -> dict[str, object]:
    value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return cast(dict[str, object], value)


def _mesh_worker(session_dir: Path, worker: str) -> dict[str, object]:
    value = _json_object(session_dir / "mesh_roster.json").get(worker)
    if not isinstance(value, dict):
        raise ValueError(f"{session_dir}: roster has no worker {worker}")
    entry = cast(dict[str, object], value)
    if not isinstance(entry.get("thread_id"), str) or not entry["thread_id"]:
        raise ValueError(f"{session_dir}: worker {worker} has no thread")
    return entry


def hold(role: AstraRole, thread_id: str = "", codex_home: str = "", *,
         session_dir: str | Path | None = None, worker: str | None = None,
         pid: int | None = None, manager: str | None = None,
         design_id: str = "", role_id: str = "") -> HeldRole:
    """Write an ownership record atomically; invalid launch details raise ValueError."""
    manager = (os.environ.get("CLAUDE_CODE_SESSION_ID", "") if manager is None else manager).strip()
    if not manager:
        raise ValueError("the edge needs a Claude session id")
    if design_id:
        if session_dir is not None or worker is not None or pid is not None or role_id != role or not codex_home:
            raise ValueError("durable holds need design_id, role_id, thread and codex_home")
    elif session_dir is not None:
        if not worker or thread_id or codex_home or pid is not None:
            raise ValueError("mesh holds need session_dir and worker only")
        session_dir = Path(session_dir).expanduser().resolve()
        entry = _mesh_worker(session_dir, worker)
        thread_id = cast(str, entry["thread_id"])
        home = _json_object(session_dir / "mesh_server.json").get("codex_home")
        if not isinstance(home, str) or not home:
            raise ValueError(f"{session_dir}: mesh server has no codex_home")
        codex_home = home
    elif worker is not None or not codex_home or isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("exec holds need thread, codex_home and a positive pid")
    record = RoleRecord(role=role, thread_id=thread_id, codex_home=codex_home, manager=manager,
                        held_at=datetime.now(timezone.utc).isoformat(),
                        session_dir=str(session_dir) if session_dir is not None else None, worker=worker, pid=pid)
    if design_id:
        record.update(design_id=design_id, role_id=role_id)
    path = _record_path(thread_id)
    if pid is not None:
        lifetime = ExecProcess(pid)
        # Keep the role's existing JSON shape readable by an older installation.
        durable_state.atomic_write_text(path.with_suffix(".process"), json.dumps({
            "pid": lifetime.pid, "started": lifetime.started}))
    held = _parse_record(record, path)
    durable_state.atomic_write_text(path, json.dumps(record))
    return held


def release(thread_id: str) -> None:
    """Remove a hold, including one whose thread has already stopped."""
    path = _record_path(thread_id)
    path.unlink(missing_ok=True)
    path.with_suffix(".process").unlink(missing_ok=True)


def _record_text(record: dict[str, object], key: str) -> str:
    value = record[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing or invalid {key}")
    return value


def _parse_record(record: dict[str, object] | RoleRecord, path: Path) -> HeldRole:
    keys = {"role", "thread_id", "codex_home", "manager", "held_at", "session_dir", "worker", "pid"}
    durable = "design_id" in record
    if set(record) != keys | ({"design_id", "role_id"} if durable else set()):
        raise ValueError("record has missing or extra fields")
    # JSON is untyped at this boundary. Build the domain value only after validation.
    fields = cast(dict[str, object], record)
    role = fields["role"]
    if role not in ("designer", "reviewer"):
        raise ValueError("unknown Astra role")
    thread_id = _record_text(fields, "thread_id")
    codex_home = _record_text(fields, "codex_home")
    manager = _record_text(fields, "manager")
    held_at = _record_text(fields, "held_at")
    if thread_id != path.stem:
        raise ValueError("thread id differs from file name")
    session_dir, worker, pid = fields["session_dir"], fields["worker"], fields["pid"]
    lifetime: MeshWorker | ExecProcess | HeldDesignRole
    if durable:
        if session_dir is not None or worker is not None or pid is not None or fields["role_id"] != role:
            raise ValueError("invalid durable role lifetime")
        lifetime = HeldDesignRole(_record_text(fields, "design_id"), _record_text(fields, "role_id"))
    elif isinstance(session_dir, str) and session_dir.strip() and isinstance(worker, str) and worker.strip() and pid is None:
        lifetime = MeshWorker(session_dir, worker)
    elif session_dir is None and worker is None and isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
        try:
            process = _json_object(path.with_suffix(".process"))
        except FileNotFoundError as error:
            raise ValueError("original exec process lifetime is unrecorded; hold it again from its launcher") from error
        started = process.get("started")
        if process.get("pid") != pid or not isinstance(started, str) or not started:
            raise ValueError("original exec process lifetime differs from the hold")
        lifetime = ExecProcess(pid, started)
    else:
        raise ValueError("record needs a mesh worker or an exec pid")
    return HeldRole(role=role, thread_id=thread_id, codex_home=codex_home,
                    manager=manager, held_at=held_at, lifetime=lifetime)


def _role_record(role: HeldRole) -> RoleRecord:
    lifetime = role["lifetime"]
    record = RoleRecord(role=role["role"], thread_id=role["thread_id"], codex_home=role["codex_home"],
                      manager=role["manager"], held_at=role["held_at"],
                      session_dir=lifetime.session_dir if isinstance(lifetime, MeshWorker) else None,
                      worker=lifetime.worker if isinstance(lifetime, MeshWorker) else None,
                      pid=lifetime.pid if isinstance(lifetime, ExecProcess) else None)
    if isinstance(lifetime, HeldDesignRole):
        record.update(design_id=lifetime.design_id, role_id=lifetime.role_id)
    return record


def live_roles() -> list[LiveRole]:
    """Return readable live holds with current state; remove holds known to have ended."""
    roles: list[LiveRole] = []
    try:
        paths = sorted(state_root().glob("*.json"))
    except OSError as error:
        print(f"astra_roles: cannot read roles: {error}", file=sys.stderr)
        return roles
    for path in paths:
        try:
            record = _parse_record(_json_object(path), path)
            lifetime = record["lifetime"]
            if isinstance(lifetime, HeldDesignRole):
                sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
                from scripts.astra import design_roles
                design = design_roles.read_design(lifetime.design_id)
                instance = next(role for role in design.roles if role.role == lifetime.role_id)
                if isinstance(instance.lifecycle, design_roles.RoleEnded):
                    path.unlink(missing_ok=True)
                    continue
                if isinstance(instance.lifecycle, design_roles.RoleNotOpened) or instance.lifecycle.thread_id != record["thread_id"]:
                    raise ValueError("durable instance thread differs from hold")
                entry = _mesh_worker(design_roles.mesh_directory(lifetime.design_id), instance.name)
                state: RoleState = "busy" if entry.get("status") in ("starting", "running", "waiting_capacity") else "idle"
                reach = design_roles.resolve_holder(design.holder)
                if isinstance(reach, design_roles.HolderReach):
                    record["manager"] = reach.session_id
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
                thread = entry.get("thread_id")
                if not isinstance(thread, str) or not thread:
                    raise ValueError("mesh worker has no thread")
                status = entry.get("status")
                if not isinstance(status, str) or not status:
                    raise ValueError("mesh worker has no status")
                if status in ("ended", "failed") or thread != record["thread_id"]:
                    path.unlink(missing_ok=True)
                    continue
                state = "busy" if status in ("starting", "running", "waiting_capacity") else "idle"
            else:
                if not durable_state.ProcessLifetime(lifetime.pid, lifetime.started).is_live():
                    release(record["thread_id"])
                    continue
                state = "busy"
            roles.append(LiveRole(role=record["role"], thread_id=record["thread_id"],
                                  codex_home=record["codex_home"], manager=record["manager"],
                                  held_at=record["held_at"], lifetime=lifetime, state=state))
        except (OSError, UnicodeError, ValueError) as error:
            message = f"astra_roles: cannot read {path}: {error}"
            print(message.replace("\n", " ").replace("\r", " "), file=sys.stderr)
    return roles


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    holding = commands.add_parser("hold")
    _ = holding.add_argument("role", choices=("designer", "reviewer"))
    for flag in ("session-dir", "worker", "thread", "codex-home", "manager"):
        _ = holding.add_argument(f"--{flag}")
    _ = holding.add_argument("--pid", type=int)
    releasing = commands.add_parser("release")
    _ = releasing.add_argument("thread_id")
    listing = commands.add_parser("list")
    _ = listing.add_argument("--json", action="store_true")
    args = parser.parse_args(argv, namespace=RoleArguments())
    try:
        if args.command == "hold":
            _ = hold(args.role, args.thread or "", args.codex_home or "",
                     session_dir=args.session_dir, worker=args.worker, pid=args.pid, manager=args.manager)
        elif args.command == "release":
            release(args.thread_id)
        else:
            roles = live_roles()
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
