#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Record role assignments that would otherwise disappear when a run ends."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import json
import os
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Literal, cast, get_args

if TYPE_CHECKING:
    from scripts.production import staff

Role = Literal["showrunner", "director", "impl", "test", "fix", "review", "worker", "interactive", "subagent"]
Lens = Literal["adversary", "contract", "craft", "ux", "rerun", "simplify"]
ROLES: tuple[Role, ...] = ("showrunner", "director", "impl", "test", "fix", "review", "worker", "interactive", "subagent")
LENSES = cast(tuple[Lens, ...], get_args(Lens))
PHASE_REVIEW_LENSES = tuple(lens for lens in LENSES if lens != "simplify")


@dataclass(frozen=True)
class Assignment:
    at: datetime
    role: Role
    lens: Lens | None
    approximate: bool = False


@dataclass(frozen=True)
class Unassigned:
    """No recorded role was in force for this agent at the requested time."""


@dataclass(frozen=True)
class RoleLog:
    assignments: dict[str, tuple[Assignment, ...]]
    problems: tuple[str, ...]


def state_dir() -> Path:
    override = os.environ.get("USAGE_STATE_DIR")
    return Path(override).expanduser() if override is not None else Path.home() / ".local/state/usage-model"


def _utc(at: datetime) -> datetime:
    return (at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at.astimezone(timezone.utc))


def _role(value: object) -> Role:
    if not isinstance(value, str) or value not in ROLES:
        raise ValueError(f"unknown role: {value!r}")
    return value


def _lens(value: object) -> Lens:
    if not isinstance(value, str) or value not in LENSES:
        raise ValueError(f"unknown lens: {value!r}")
    return value


def _write_assignment(output: BinaryIO, agent_id: str, assignment: Assignment) -> None:
    if not agent_id.strip():
        raise ValueError("agent id must not be empty")
    row: dict[str, object] = {
        "id": agent_id,
        "role": _role(assignment.role),
        "at": _utc(assignment.at).isoformat(timespec="seconds"),
    }
    if assignment.lens is not None:
        row["lens"] = _lens(assignment.lens)
    if assignment.approximate:
        row["approximate"] = True
    _ = output.write((json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8"))


def _terminate_partial_line(output: BinaryIO) -> None:
    size = os.fstat(output.fileno()).st_size
    if size and os.pread(output.fileno(), 1, size - 1) != b"\n":
        _ = output.write(b"\n")


def append(agent_id: str, role: Role, lens: Lens | None, at: datetime,
           approximate: bool = False) -> None:
    path = state_dir() / "agents.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as output:
        fcntl.flock(output.fileno(), fcntl.LOCK_EX)
        _terminate_partial_line(output)
        _write_assignment(output, agent_id, Assignment(at, role, lens, approximate))
        output.flush()


def _names_agent(recorded_id: str, agent_id: str) -> bool:
    return recorded_id == agent_id or (
        re.fullmatch(r"[0-9a-fA-F]{8}", recorded_id) is not None
        and agent_id.startswith(recorded_id)
    )


def register_default(agent_id: str, role: Role, at: datetime) -> bool:
    """Assign a default role only when no valid row names this agent yet."""
    path = state_dir() / "agents.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as output:
        fcntl.flock(output.fileno(), fcntl.LOCK_EX)
        _ = output.seek(0)
        log = _read_assignments(output, path)
        for recorded_id in log.assignments:
            if _names_agent(recorded_id, agent_id):
                return False
        _terminate_partial_line(output)
        _write_assignment(output, agent_id, Assignment(at, role, None))
        output.flush()
    return True


def _read_assignments(lines: Iterable[bytes], path: Path) -> RoleLog:
    grouped: dict[str, list[Assignment]] = {}
    problems: list[str] = []
    for number, line in enumerate(lines, 1):
        try:
            value = cast(object, json.loads(line.decode("utf-8")))
            if not isinstance(value, dict):
                raise ValueError("assignment must be an object")
            row = cast(dict[str, object], value)
            agent_id = row.get("id")
            stamp = row.get("at")
            if not isinstance(agent_id, str) or not agent_id.strip():
                raise ValueError("id must be nonempty text")
            if not isinstance(stamp, str):
                raise ValueError("at must be an ISO-8601 timestamp")
            at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if at.tzinfo is None:
                raise ValueError("at must include a UTC offset")
            approximate = row.get("approximate", False)
            if not isinstance(approximate, bool):
                raise ValueError("approximate must be a boolean")
            assignment = Assignment(_utc(at), _role(row.get("role")),
                                    _lens(row["lens"]) if "lens" in row else None, approximate)
            grouped.setdefault(agent_id, []).append(assignment)
        except Exception as exc:
            problems.append(f"{path}:{number}: {exc}")
    return RoleLog({agent_id: tuple(sorted(entries, key=lambda entry: entry.at))
                    for agent_id, entries in grouped.items()}, tuple(problems))


def assignments(path: Path) -> RoleLog:
    try:
        with path.open("rb") as source:
            fcntl.flock(source.fileno(), fcntl.LOCK_SH)
            return _read_assignments(source, path)
    except FileNotFoundError:
        return RoleLog({}, ())


def role_at(log: RoleLog, agent_id: str, at: datetime) -> Assignment | Unassigned:
    """Find the latest assignment across the full id and its worker prefix."""
    moment = _utc(at)
    history = list(log.assignments.get(agent_id, ()))
    prefix = agent_id[:8]
    if prefix != agent_id and _names_agent(prefix, agent_id):
        history.extend(log.assignments.get(prefix, ()))
    for assignment in reversed(sorted(history, key=lambda entry: entry.at)):
        if assignment.at <= moment:
            return assignment
    return Unassigned()


def _review_lens(run: Path, name: str) -> Lens | None:
    worker_name = re.compile(r"(?<![\w.-])" + re.escape(name) + r"(?![\w.-])")
    log_name = re.compile(r"review_agent_(?:\d+_)?(" + "|".join(LENSES) + r")(?:-[\w-]+)?\.log")
    evidence_mtimes: dict[Lens, float] = {}
    for path in sorted([*run.glob("review_agent_*.log"), *run.glob("phases/*/review_agent_*.log")]):
        match = log_name.fullmatch(path.name)
        if match is None:
            continue
        try:
            with path.open(encoding="utf-8") as log:
                if any(worker_name.search(line) for line in log):
                    lens = _lens(match.group(1))
                    stamp = path.stat().st_mtime
                    evidence_mtimes[lens] = max(stamp, evidence_mtimes.get(lens, float("-inf")))
        except (OSError, ValueError) as exc:
            _report_problem(f"{path}: {exc}")
    return max(evidence_mtimes, key=lambda lens: evidence_mtimes[lens]) if evidence_mtimes else None


def _run_assignments(run: Path, membership: staff.StaffSnapshot) -> Iterator[tuple[str, Assignment]]:
    from scripts.production import session_staff

    roster_path = run / "mesh_roster.json"
    try:
        if roster_path.is_file():
            stamp = datetime.fromtimestamp(roster_path.stat().st_mtime, timezone.utc)
            value = cast(object, json.loads(roster_path.read_text(encoding="utf-8")))
            if not isinstance(value, dict):
                raise ValueError("roster must be an object")
            records = cast(dict[str, object], value)
            for name, value in records.items():
                if not isinstance(value, dict):
                    continue
                worker = cast(dict[str, object], value)
                agent_id = session_staff.worker_session(worker, membership)
                if not agent_id.strip():
                    continue
                role = session_staff.worker_name(worker, membership)
                if role in ROLES:
                    current = _role(role)
                    lens = _review_lens(run, name) if current == "review" else None
                    yield agent_id, Assignment(stamp, current, lens, True)
    except (OSError, ValueError, RecursionError) as exc:
        _report_problem(f"{roster_path}: {exc}")


def _recover_directors(live: Path, membership: staff.StaffSnapshot,
                       recovered: list[tuple[str, Assignment]]) -> None:
    """Add a director assignment for each staff run under the live root."""
    from scripts.production import staff

    for row in membership.rows:
        if not isinstance(row.run, staff.OpenRun) or not row.current_session:
            continue
        run = row.run.path
        try:
            if run.is_dir() and run.resolve().parent == live.resolve():
                stamp = datetime.fromtimestamp(run.stat().st_mtime, timezone.utc)
                recovered.append((row.current_session, Assignment(stamp, "director", None, True)))
        except (OSError, ValueError) as exc:
            _report_problem(exc)


def backfill(archive: Path, *live: Path) -> int:
    """Recover assignments from the archive and each live run root."""
    from scripts.production import staff

    recovered: list[tuple[str, Assignment]] = []
    try:
        membership = staff.local_snapshot()
        for root in (archive, *live):
            if not root.exists():
                continue
            for run in sorted(root.iterdir()):
                if run.is_dir():
                    try:
                        recovered.extend(_run_assignments(run, membership))
                    except (OSError, ValueError) as exc:
                        _report_problem(exc)
        for root in live:
            _recover_directors(root, membership, recovered)
    except (OSError, ValueError) as exc:
        _report_problem(f"{staff.record_path()}: {exc}")
    path = state_dir() / "agents.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    added = 0
    with path.open("a+b") as output:
        fcntl.flock(output.fileno(), fcntl.LOCK_EX)
        _ = output.seek(0)
        log = _read_assignments(output, path)
        exact_ids = {agent_id for agent_id, entries in log.assignments.items()
                     if any(not assignment.approximate for assignment in entries)}
        known = {(agent_id, assignment.role, assignment.lens)
                 for agent_id, entries in log.assignments.items() for assignment in entries}
        for agent_id, assignment in recovered:
            if agent_id in exact_ids:
                continue
            key = (agent_id, assignment.role, assignment.lens)
            if key not in known:
                if not added:
                    _terminate_partial_line(output)
                _write_assignment(output, agent_id, assignment)
                known.add(key)
                added += 1
        output.flush()
    return added


def _report_problem(reason: object) -> None:
    print("agents_log: " + " ".join(str(reason).splitlines()), file=sys.stderr)


def _show(log: RoleLog, agent_id: str | None) -> None:
    print("| ID | Role | Lens | At (UTC) | Approximate |")
    print("| --- | --- | --- | --- | --- |")
    for identity, entries in sorted(log.assignments.items()):
        if agent_id is not None and identity != agent_id:
            continue
        label = " ".join(identity.splitlines()).replace("|", "\\|")
        for assignment in entries:
            print(f"| {label} | {assignment.role} | {assignment.lens or ''} | " +
                  f"{assignment.at.isoformat(timespec='seconds')} | {str(assignment.approximate).lower()} |")
    for problem in log.problems:
        _report_problem(problem)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    writer = commands.add_parser("append", help="record a role assignment now")
    _ = writer.add_argument("--id", required=True)
    _ = writer.add_argument("--role", choices=ROLES, required=True)
    _ = writer.add_argument("--lens", choices=LENSES)
    registrar = commands.add_parser("register", help="record a default role for an unassigned agent")
    _ = registrar.add_argument("--id", required=True)
    _ = registrar.add_argument("--role", choices=ROLES, required=True)
    importer = commands.add_parser("backfill", help="recover surviving run assignments")
    _ = importer.add_argument("--archive", type=Path, default=None,
                              help="the run archive; default: its new name, else its former one")
    _ = importer.add_argument("--live", type=Path, action="append", help="a run root; default: both run-folder names")
    reader = commands.add_parser("show", help="print recorded assignments as a Markdown table")
    _ = reader.add_argument("--id")
    args = parser.parse_args(argv)
    command = cast(str, args.command)
    try:
        if command == "append":
            append(cast(str, args.id), cast(Role, args.role), cast(Lens | None, args.lens), datetime.now(timezone.utc))
        elif command == "register":
            _ = register_default(cast(str, args.id), cast(Role, args.role), datetime.now(timezone.utc))
        elif command == "backfill":
            from scripts.lib import unit_runs

            archive = cast(Path | None, args.archive) or unit_runs.run_archive()
            added = backfill(archive, *(cast(list[Path] | None, args.live) or unit_runs.run_roots()))
            print(f"Backfilled {added} assignments.")
        else:
            _show(assignments(state_dir() / "agents.jsonl"), cast(str | None, args.id))
    except (OSError, ValueError) as exc:
        _report_problem(exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
