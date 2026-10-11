#!/usr/bin/env python3
"""List Claude and Codex sessions on both workstations; --here reads only this one."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Literal, TypedDict, cast

SCRIPTS = Path(__file__).resolve().parents[1]
for _directory in (SCRIPTS / "production", SCRIPTS / "delegate", SCRIPTS / "shutdown"):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

import add_unit  # noqa: E402
import live_units  # noqa: E402
import remove_seats  # noqa: E402
import showrunners  # noqa: E402
import unit_lookup  # noqa: E402
import codex_daemon  # noqa: E402
import process_table  # noqa: E402
import remote as remote  # noqa: E402
from sessions import SessionRecord, messaging_socket_exists, running_sessions  # noqa: E402

Kind = Literal["claude", "codex"]
Role = Literal["showrunner", "unit director", "worker", "freestanding"]
KNOWN_SLOT_ROLES = frozenset(("impl", "test", "fix", "review"))
MESSAGEABLE_SEAT_STATUSES = frozenset(("running", "waiting_capacity"))


@dataclass(frozen=True)
class ThisMachine:
    """Read only this workstation's sources."""


@dataclass(frozen=True)
class AllMachines:
    """Read this workstation and its peer."""


RosterScope = ThisMachine | AllMachines
ALL_MACHINES = AllMachines()


@dataclass(frozen=True)
class CodexThread:
    thread_id: str


@dataclass(frozen=True)
class ClaudeSession:
    session_id: str


@dataclass(frozen=True)
class NoSession:
    """The sender runs in a shell outside a Claude or Codex session."""


SenderIdentity = CodexThread | ClaudeSession | NoSession


@dataclass(frozen=True)
class NoCodexThread:
    """This row has no Codex thread identity."""


@dataclass(frozen=True)
class NoClaudeSession:
    """This row has no Claude session identity."""


def sender_identity(environment: Mapping[str, str]) -> SenderIdentity:
    """A Codex shell can inherit its launching Claude session's id."""
    if thread_id := environment.get("CODEX_THREAD_ID", ""):
        return CodexThread(thread_id)
    if session_id := environment.get("CLAUDE_CODE_SESSION_ID", ""):
        return ClaudeSession(session_id)
    return NoSession()


@dataclass(frozen=True)
class Messageable:
    address: str


@dataclass(frozen=True)
class CannotReceiveMessages:
    reason: str


MessageReach = Messageable | CannotReceiveMessages


@dataclass(frozen=True)
class UnderShowrunner:
    production: str
    name: str
    address: str


@dataclass(frozen=True)
class RunsProduction:
    production: str


@dataclass(frozen=True)
class ProductionWithoutRunningShowrunner:
    production: str


@dataclass(frozen=True)
class NoShowrunner:
    """This session has no showrunner link."""


ShowrunnerRelationship = (
    RunsProduction
    | UnderShowrunner
    | ProductionWithoutRunningShowrunner
    | NoShowrunner
)


@dataclass(frozen=True)
class UnderDirector:
    unit: str
    name: str
    address: str


@dataclass(frozen=True)
class DirectsUnit:
    unit: str


@dataclass(frozen=True)
class NoDirector:
    """This session has no unit-director link."""


DirectorRelationship = DirectsUnit | UnderDirector | NoDirector


@dataclass(frozen=True)
class WorkerAssignment:
    slot_role: str
    session_dir: str


@dataclass(frozen=True)
class NotAWorker:
    """This session is not assigned to a delegate worker seat."""


Assignment = WorkerAssignment | NotAWorker


@dataclass(frozen=True)
class RosterEntry:
    machine: str
    kind: str
    role: str
    name: str
    status: str
    reach: MessageReach
    cwd: str
    showrunner: ShowrunnerRelationship
    director: DirectorRelationship
    assignment: Assignment
    thread: CodexThread | NoCodexThread = NoCodexThread()
    session: ClaudeSession | NoClaudeSession = NoClaudeSession()


@dataclass(frozen=True)
class Roster:
    entries: list[RosterEntry]
    problems: list[str]


SnapshotState = (MessageReach | ShowrunnerRelationship | DirectorRelationship | Assignment
                 | CodexThread | NoCodexThread | ClaudeSession | NoClaudeSession)
_SNAPSHOT_STATES: tuple[type[SnapshotState], ...] = (
    Messageable, CannotReceiveMessages, UnderShowrunner, RunsProduction,
    ProductionWithoutRunningShowrunner, NoShowrunner, UnderDirector, DirectsUnit,
    NoDirector, WorkerAssignment, NotAWorker, CodexThread, NoCodexThread,
    ClaudeSession, NoClaudeSession,
)


class SnapshotEntry(TypedDict):
    machine: str
    kind: str
    role: str
    name: str
    status: str
    cwd: str
    reach: dict[str, str]
    showrunner: dict[str, str]
    director: dict[str, str]
    assignment: dict[str, str]
    thread: dict[str, str]
    session: dict[str, str]


def _tagged_state(state: SnapshotState) -> dict[str, str]:
    return {"tag": type(state).__name__, **cast(dict[str, str], asdict(state))}


def _snapshot_entry(entry: RosterEntry) -> SnapshotEntry:
    return SnapshotEntry(
        machine=entry.machine, kind=entry.kind, role=entry.role, name=entry.name,
        status=entry.status, cwd=entry.cwd, reach=_tagged_state(entry.reach),
        showrunner=_tagged_state(entry.showrunner), director=_tagged_state(entry.director),
        assignment=_tagged_state(entry.assignment), thread=_tagged_state(entry.thread),
        session=_tagged_state(entry.session),
    )


def _snapshot_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("snapshot requires an object")
    result = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in result):
        raise ValueError("snapshot object keys must be strings")
    return cast(dict[str, object], result)


def _snapshot_string(values: Mapping[str, object], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str):
        raise ValueError(f"snapshot needs string {key}")
    return value


def _decode_state(value: object) -> SnapshotState:
    values = _snapshot_object(value)
    tag = _snapshot_string(values, "tag")
    for state in _SNAPSHOT_STATES:
        if state.__name__ == tag:
            names = [field.name for field in fields(state)]
            if set(values) != {"tag", *names}:
                raise ValueError(f"invalid fields for snapshot state {tag}")
            return state(**{name: _snapshot_string(values, name) for name in names})
    raise ValueError(f"unknown snapshot state {tag}")


def _decode_entry(value: object) -> RosterEntry:
    values = _snapshot_object(value)
    if set(values) != set(SnapshotEntry.__annotations__):
        raise ValueError("invalid snapshot entry fields")
    reach = _decode_state(values.get("reach"))
    showrunner = _decode_state(values.get("showrunner"))
    director = _decode_state(values.get("director"))
    assignment = _decode_state(values.get("assignment"))
    thread = _decode_state(values.get("thread"))
    session = _decode_state(values.get("session"))
    if not (isinstance(reach, Messageable | CannotReceiveMessages)
            and isinstance(showrunner, ShowrunnerRelationship)
            and isinstance(director, DirectorRelationship)
            and isinstance(assignment, Assignment)
            and isinstance(thread, CodexThread | NoCodexThread)
            and isinstance(session, ClaudeSession | NoClaudeSession)):
        raise ValueError("snapshot state is in the wrong field")
    return RosterEntry(
        machine=_snapshot_string(values, "machine"), kind=_snapshot_string(values, "kind"),
        role=_snapshot_string(values, "role"), name=_snapshot_string(values, "name"),
        status=_snapshot_string(values, "status"), cwd=_snapshot_string(values, "cwd"),
        reach=reach, showrunner=showrunner, director=director, assignment=assignment,
        thread=thread, session=session,
    )


@dataclass(frozen=True)
class RosterSnapshot:
    """Lossless states exchanged between the two workstations."""

    result: Roster

    def encode(self) -> str:
        return json.dumps({"tag": "RosterSnapshot", "entries": [
            _snapshot_entry(entry) for entry in self.result.entries
        ], "problems": self.result.problems})

    @classmethod
    def decode(cls, text: str) -> RosterSnapshot:
        values = _snapshot_object(cast(object, json.loads(text)))
        entries, problems = values.get("entries"), values.get("problems")
        if (values.get("tag") != "RosterSnapshot" or not isinstance(entries, list)
                or not isinstance(problems, list)):
            raise ValueError("not a roster snapshot")
        raw_problems = cast(list[object], problems)
        if not all(isinstance(problem, str) for problem in raw_problems):
            raise ValueError("snapshot problems must be strings")
        return cls(Roster([_decode_entry(entry) for entry in cast(list[object], entries)],
                          cast(list[str], raw_problems)))


def cannot_receive_message_text(entry: RosterEntry) -> str:
    """The same refusal and next step for send and broadcast."""
    if not isinstance(entry.reach, CannotReceiveMessages):
        raise ValueError("a refusal needs a CannotReceiveMessages row")
    text = f"{entry.name} cannot take messages ({entry.reach.reason})"
    if entry.role == "worker":
        if isinstance(entry.director, UnderDirector):
            text += f"; contact its unit director {entry.director.name} or wait"
    elif entry.kind == "claude" and entry.reach.reason == "no messaging socket":
        text += "; wait for it to open its messaging socket, then read the roster again and send again"
    return text


def is_sender(entry: RosterEntry, identity: SenderIdentity) -> bool:
    match identity:
        case CodexThread():
            return entry.kind == "codex" and entry.thread == identity
        case ClaudeSession():
            return entry.session == identity
        case NoSession():
            return False


def is_top_level(entry: RosterEntry) -> bool:
    return (
        entry.kind == "claude"
        and entry.role != "worker"
        and not (entry.role == "unit director" and isinstance(entry.showrunner, UnderShowrunner))
    )


def _address(entry: RosterEntry) -> str:
    return entry.reach.address if isinstance(entry.reach, Messageable) else ""


def _session_address(entry: RosterEntry) -> str:
    """Hierarchy links keep their Claude identity even without a messaging socket."""
    return f"session:{entry.session.session_id}" if isinstance(entry.session, ClaudeSession) else ""


def _production(entry: RosterEntry) -> str:
    if isinstance(
        entry.showrunner,
        (RunsProduction, UnderShowrunner, ProductionWithoutRunningShowrunner),
    ):
        return entry.showrunner.production
    return ""


def _showrunner_name(entry: RosterEntry) -> str:
    return entry.showrunner.name if isinstance(entry.showrunner, UnderShowrunner) else ""


def _showrunner_address(entry: RosterEntry) -> str:
    return entry.showrunner.address if isinstance(entry.showrunner, UnderShowrunner) else ""


def _unit(entry: RosterEntry) -> str:
    if isinstance(entry.director, (DirectsUnit, UnderDirector)):
        return entry.director.unit
    return ""


def _director_name(entry: RosterEntry) -> str:
    return entry.director.name if isinstance(entry.director, UnderDirector) else ""


def _director_address(entry: RosterEntry) -> str:
    return entry.director.address if isinstance(entry.director, UnderDirector) else ""


def _slot_role_for(entry: RosterEntry) -> str:
    return entry.assignment.slot_role if isinstance(entry.assignment, WorkerAssignment) else ""


def _session_dir(entry: RosterEntry) -> str:
    return entry.assignment.session_dir if isinstance(entry.assignment, WorkerAssignment) else ""


class SerializedRosterEntry(TypedDict):
    machine: str
    kind: str
    role: str
    name: str
    status: str
    address: str
    cwd: str
    production: str
    showrunner: str
    showrunner_address: str
    unit: str
    director: str
    director_address: str
    slot_role: str
    session_dir: str
    thread: str | None


def _serialized_entry(entry: RosterEntry) -> SerializedRosterEntry:
    """Flatten domain states at the JSON compatibility boundary."""
    return {
        "machine": entry.machine,
        "kind": entry.kind,
        "role": entry.role,
        "name": entry.name,
        "status": entry.status,
        "address": _address(entry),
        "cwd": entry.cwd,
        "production": _production(entry),
        "showrunner": _showrunner_name(entry),
        "showrunner_address": _showrunner_address(entry),
        "unit": _unit(entry),
        "director": _director_name(entry),
        "director_address": _director_address(entry),
        "slot_role": _slot_role_for(entry),
        "session_dir": _session_dir(entry),
        "thread": entry.thread.thread_id if isinstance(entry.thread, CodexThread) else None,
    }


@dataclass(frozen=True)
class _ProductionPlace:
    showrunner: UnderShowrunner | ProductionWithoutRunningShowrunner
    director: DirectsUnit


@dataclass(frozen=True)
class _DirectorSession:
    session_id: str


@dataclass(frozen=True)
class _NoRunningDirector:
    """No live Claude session owns this run."""


_RunDirector = _DirectorSession | _NoRunningDirector


@dataclass(frozen=True)
class _LiveRun:
    path: Path
    director: _RunDirector


@dataclass(frozen=True)
class _ClaudeDetails:
    cwd: str
    status: str


@dataclass(frozen=True)
class _WorkerPlace:
    run: _LiveRun
    seat_name: str


@dataclass(frozen=True)
class _RunPlace:
    director: DirectorRelationship
    showrunner: ShowrunnerRelationship
    cwd: str


@dataclass(frozen=True)
class _ReviewerProcess:
    kind: Kind
    pid: int


@dataclass(frozen=True)
class _ReviewerProcessNotFound:
    """No Claude or Codex descendant is visible in the process table."""


_ReviewerProcessState = _ReviewerProcess | _ReviewerProcessNotFound


class _MeshSeat(TypedDict, total=False):
    thread_id: str
    status: str
    launcher_pid: int
    role: str
    lens: str
    cwd: str


def _as_string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _read_object(path: Path) -> dict[str, object]:
    parsed = cast("object", json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(parsed, dict):
        raise ValueError("expected a JSON object")
    return cast("dict[str, object]", parsed)


def _problem(problems: list[str], source: str, error: BaseException | str) -> None:
    detail = str(error).splitlines()[0] if str(error).splitlines() else type(error).__name__
    message = f"{source}: {detail}"
    if message not in problems:
        problems.append(message)


def _newest_sessions() -> list[SessionRecord]:
    newest: dict[str, SessionRecord] = {}
    for record in running_sessions():
        _ = newest.setdefault(record["sessionId"], record)
    return list(newest.values())


def _raw_session_details(records: list[SessionRecord], problems: list[str]) -> dict[str, _ClaudeDetails]:
    directory = Path(os.environ.get("NOTIFIER_SESSIONS_DIR", str(Path.home() / ".claude/sessions")))
    try:
        paths = [path for path in directory.iterdir() if path.suffix == ".json"]
    except OSError as error:
        _problem(problems, "Claude session records", error)
        paths = []
    chosen = {(record["sessionId"], record["pid"]) for record in records}
    chosen_files = {str(record["pid"]) for record in records}
    raw: dict[tuple[str, int], tuple[int, _ClaudeDetails]] = {}
    for path in paths:
        try:
            data = _read_object(path)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            if path.stem in chosen_files:
                _problem(problems, f"Claude session record {path}", error)
            continue
        pid = data.get("pid")
        key = (_as_string(data.get("sessionId")), pid if isinstance(pid, int) else 0)
        if key not in chosen:
            continue
        updated_value = data.get("updatedAt")
        updated = updated_value if isinstance(updated_value, int) else 0
        if key not in raw or updated > raw[key][0]:
            raw[key] = (updated, _ClaudeDetails(
                cwd=_as_string(data.get("cwd")), status=_as_string(data.get("status"))
            ))
    details: dict[str, _ClaudeDetails] = {}
    for record in records:
        session_id = record["sessionId"]
        detail = raw.get((session_id, record["pid"]), (0, _ClaudeDetails("", "")))[1]
        cwd = detail.cwd
        if not cwd:
            try:
                cwd = os.readlink(f"/proc/{record['pid']}/cwd")
            except OSError:
                cwd = ""
        details[session_id] = _ClaudeDetails(cwd, detail.status or "running")
    return details


def _registered_showrunners(
    environment: Mapping[str, str], problems: list[str]
) -> list[showrunners.Showrunner]:
    state = Path(
        environment.get(
            "NOTIFIER_STATE_DIR", str(Path.home() / ".local/state/notifier")
        )
    )
    sessions_directory = Path(
        environment.get("NOTIFIER_SESSIONS_DIR", str(Path.home() / ".claude/sessions"))
    )
    showrunners.SESSIONS_DIR = sessions_directory
    errors = io.StringIO()
    try:
        with contextlib.redirect_stderr(errors):
            registered = showrunners.registered_showrunners(state)
    except (OSError, ValueError) as error:
        _problem(problems, "showrunners", error)
        registered = []
    for line in errors.getvalue().splitlines():
        if line.strip():
            _problem(problems, "showrunners", line.strip())
    return registered


def _showrunner_ids(
    records: list[SessionRecord], registered: Iterable[showrunners.Showrunner]
) -> dict[str, showrunners.Showrunner]:
    found: dict[str, showrunners.Showrunner] = {}
    for runner in registered:
        if runner["socket"]:
            match = next((record for record in records
                          if record["messagingSocketPath"] == runner["socket"]), None)
        else:
            match = next((record for record in records
                          if runner["session"] and record["name"] == runner["session"]), None)
        if match is not None:
            found[match["sessionId"]] = runner
    return found


def _production_rows(doc: Path, slug: str) -> list[dict[str, str]]:
    lines = doc.read_text(encoding="utf-8").splitlines()
    return add_unit.live_unit_table(lines, slug)


def _production_places(
    records: list[SessionRecord], details: Mapping[str, _ClaudeDetails],
    registered: list[showrunners.Showrunner],
    runner_ids: Mapping[str, showrunners.Showrunner], ledger_workers: set[str],
    problems: list[str],
) -> dict[str, _ProductionPlace]:
    addresses = {runner["slug"]: f"session:{session_id}" for session_id, runner in runner_ids.items()}
    places: dict[str, _ProductionPlace] = {}
    for runner in registered:
        doc = Path(runner["doc"])
        runner_address = addresses.get(runner["slug"], "")
        showrunner: UnderShowrunner | ProductionWithoutRunningShowrunner = (
            UnderShowrunner(runner["slug"], runner["session"], runner_address)
            if runner["session"] and runner_address
            else ProductionWithoutRunningShowrunner(runner["slug"])
        )
        has_tmux = shutil.which(os.environ.get("UNIT_LOOKUP_TMUX") or "tmux") is not None
        try:
            if not has_tmux:
                raise OSError("no tmux binary")
            units = live_units.production_units(doc)
        except (OSError, ValueError) as error:
            if has_tmux:
                _problem(problems, f"tmux for {runner['slug']} was not read", error)
        else:
            for unit in units:
                marked = unit.session
                if marked is None:
                    continue
                place = _ProductionPlace(showrunner, DirectsUnit(unit.unit))
                if isinstance(marked.claude, unit_lookup.LiveClaude):
                    _ = places.setdefault(marked.claude.session_id, place)
                    continue
                # A socketless director is still identified by its marked pane.
                for record in records:
                    if (record["sessionId"] not in ledger_workers and marked.pane
                            and record["tmux"].rpartition(".")[2] == marked.pane):
                        _ = places.setdefault(record["sessionId"], place)
                        break
            continue
        # Worktree ownership is a fallback only when tmux could not be read.
        try:
            rows = _production_rows(doc, runner["slug"])
        except (OSError, UnicodeError, ValueError, add_unit.Refusal) as fallback_error:
            _problem(problems, f"units in {doc}", fallback_error)
            continue
        for row in rows:
            unit_name = add_unit.cell_value(row.get("Unit", ""))
            worktree = add_unit.cell_value(row.get("Worktree", ""))
            for record in records:
                if (record["sessionId"] not in ledger_workers and worktree
                        and details[record["sessionId"]].cwd == worktree):
                    _ = places.setdefault(record["sessionId"], _ProductionPlace(showrunner, DirectsUnit(unit_name)))
    return places


def _live_runs(
    root: Path, live_session_ids: set[str], problems: list[str]
) -> list[_LiveRun]:
    by_path: dict[Path, _RunDirector] = {}
    active = root / "active"
    if active.is_dir():
        try:
            markers = list(active.iterdir())
        except OSError as error:
            _problem(problems, f"delegate markers in {active}", error)
            markers = []
        for marker in markers:
            if marker.name not in live_session_ids:
                continue
            try:
                lines = marker.read_text(encoding="utf-8").splitlines()
                if lines and lines[0].strip():
                    by_path[Path(lines[0].strip()).resolve()] = _DirectorSession(marker.name)
            except (OSError, UnicodeError) as error:
                _problem(problems, f"delegate marker {marker}", error)
    try:
        heartbeats = list(root.glob(f"*/{remove_seats.HEARTBEAT}"))
    except OSError as error:
        _problem(problems, f"delegate runs in {root}", error)
        heartbeats = []
    now = time.time()
    for heartbeat in heartbeats:
        try:
            if now - heartbeat.stat().st_mtime < remove_seats.LIVE_HEARTBEAT_SECS:
                _ = by_path.setdefault(heartbeat.parent.resolve(), _NoRunningDirector())
        except OSError as error:
            _problem(problems, f"delegate heartbeat {heartbeat}", error)
    return [
        _LiveRun(path, director)
        for path, director in sorted(by_path.items(), key=lambda item: item[0])
    ]


def _seat_ledger(run: _LiveRun, problems: list[str]) -> list[tuple[str, str]]:
    path = run.path / remove_seats.LEDGER
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        _problem(problems, f"seat ledger {path}", error)
        return []
    seats: list[tuple[str, str]] = []
    for line in lines:
        session_id, separator, name = line.partition("\t")
        if session_id.strip():
            seats.append((session_id.strip(), name.strip() if separator else ""))
    return seats


def _slot_role(name: str) -> str:
    candidate = name.rpartition("-")[2]
    return candidate if candidate in KNOWN_SLOT_ROLES else "role unknown"


def _codex_slot_role(record: _MeshSeat) -> str:
    role = _as_string(record.get("role"))
    if not role:
        return "no role"
    if role not in KNOWN_SLOT_ROLES:
        return "role unknown"
    lens = _as_string(record.get("lens"))
    return f"review ({lens})" if role == "review" and lens else role


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (ProcessLookupError, OverflowError):
        return False
    return True


def _run_place(
    run: _LiveRun,
    claude_entries: Mapping[str, RosterEntry],
    production_places: Mapping[str, _ProductionPlace],
) -> _RunPlace:
    if isinstance(run.director, _NoRunningDirector):
        return _RunPlace(NoDirector(), NoShowrunner(), "")
    director = claude_entries.get(run.director.session_id)
    if director is None:
        return _RunPlace(NoDirector(), NoShowrunner(), "")
    place = production_places.get(run.director.session_id)
    unit = place.director.unit if place is not None else ""
    director_link = UnderDirector(unit, director.name, _session_address(director))
    return _RunPlace(
        director=director_link,
        showrunner=place.showrunner if place is not None else NoShowrunner(),
        cwd=director.cwd,
    )


def _between_turns_reason(director: DirectorRelationship) -> str:
    match director:
        case UnderDirector():
            return "between turns; its unit director resumes it"
        case _:
            return "between turns; no unit director is running"


def _mesh_records(run: _LiveRun, problems: list[str]) -> dict[str, _MeshSeat]:
    path = run.path / "mesh_roster.json"
    if not path.exists():
        return {}
    try:
        stored = _read_object(path)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        _problem(problems, f"Codex roster {path}", error)
        return {}
    records: dict[str, _MeshSeat] = {}
    for name, value in stored.items():
        if isinstance(value, dict):
            records[name] = cast("_MeshSeat", cast("object", value))
    return records


def _server_alive(run: _LiveRun, problems: list[str]) -> bool:
    path = run.path / "mesh_server.json"
    if not path.exists():
        return False
    try:
        pid = _read_object(path).get("pid")
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        _problem(problems, f"Codex server {path}", error)
        return False
    return isinstance(pid, int) and not isinstance(pid, bool) and _pid_alive(pid)


def _worker_status(status: str) -> str:
    return {
        "done": "idle",
        "waiting_capacity": "waiting for capacity",
        "capacity_exhausted": "out of capacity",
    }.get(status, status)


def _process_cwds(table: Mapping[int, process_table.Process]) -> dict[tuple[str, str], str]:
    found: dict[tuple[str, str], str] = {}
    for process in table.values():
        try:
            words = shlex.split(process.arguments)
        except ValueError:
            words = process.arguments.split()
        options = dict(zip(words, words[1:]))
        name = options.get("--name") or options.get("--to")
        directory = options.get("--session-dir")
        if process_table.is_codex_agent(process) and name and directory:
            found[(name, str(Path(directory).resolve()))] = options.get("--cwd", "")
    return found


def _ledger_workers(
    runs: Iterable[_LiveRun], records: Iterable[SessionRecord], problems: list[str]
) -> dict[str, _WorkerPlace]:
    workers: dict[str, _WorkerPlace] = {}
    records_list = list(records)
    for run in runs:
        for ledger_id, seat_name in _seat_ledger(run, problems):
            for record in records_list:
                session_id = record["sessionId"]
                if session_id == ledger_id or session_id.startswith(ledger_id):
                    _ = workers.setdefault(session_id, _WorkerPlace(run, seat_name))
    return workers


def _claude_entries(
    machine: str, records: list[SessionRecord], details: Mapping[str, _ClaudeDetails],
    runner_ids: Mapping[str, showrunners.Showrunner],
    production_places: Mapping[str, _ProductionPlace], runs: list[_LiveRun],
    workers: Mapping[str, _WorkerPlace],
) -> dict[str, RosterEntry]:
    marker_directors = {
        run.director.session_id
        for run in runs
        if isinstance(run.director, _DirectorSession)
    }
    entries: dict[str, RosterEntry] = {}
    records_by_id = {record["sessionId"]: record for record in records}
    for record in records:
        session_id = record["sessionId"]
        showrunner: ShowrunnerRelationship = NoShowrunner()
        director: DirectorRelationship = NoDirector()
        assignment: Assignment = NotAWorker()
        if session_id in runner_ids:
            role: Role = "showrunner"
            showrunner = RunsProduction(runner_ids[session_id]["slug"])
        elif session_id in production_places:
            role = "unit director"
            place = production_places[session_id]
            showrunner = place.showrunner
            director = place.director
        elif session_id in marker_directors:
            role = "unit director"
        elif session_id in workers:
            role = "worker"
            worker = workers[session_id]
            assignment = WorkerAssignment(
                _slot_role(worker.seat_name or record["name"]),
                str(worker.run.path),
            )
            run_director = worker.run.director
            director_entry = (
                records_by_id.get(run_director.session_id)
                if isinstance(run_director, _DirectorSession) else None
            )
            if director_entry is not None and isinstance(run_director, _DirectorSession):
                director_place = production_places.get(run_director.session_id)
                if director_place is not None:
                    showrunner = director_place.showrunner
                    unit = director_place.director.unit
                else:
                    unit = ""
                director = UnderDirector(
                    unit,
                    director_entry["name"],
                    f"session:{run_director.session_id}",
                )
        else:
            role = "freestanding"
        detail = details[session_id]
        entries[session_id] = RosterEntry(
            machine=machine,
            kind="claude",
            role=role,
            name=record["name"],
            status=detail.status,
            reach=(
                Messageable(f"session:{session_id}")
                if messaging_socket_exists(record)
                else CannotReceiveMessages("no messaging socket")
            ),
            cwd=detail.cwd,
            showrunner=showrunner,
            director=director,
            assignment=assignment,
            session=ClaudeSession(session_id),
        )
    return entries


def _process_words(process: process_table.Process) -> list[str]:
    try:
        return shlex.split(process.arguments)
    except ValueError:
        return process.arguments.split()


def _descends_from(
    process: process_table.Process, ancestor: int, table: Mapping[int, process_table.Process]
) -> bool:
    parent = process.parent
    visited: set[int] = set()
    while parent > 0 and parent not in visited:
        if parent == ancestor:
            return True
        visited.add(parent)
        parent_process = table.get(parent)
        if parent_process is None:
            return False
        parent = parent_process.parent
    return False


def _reviewer_process(
    launcher_pid: int, table: Mapping[int, process_table.Process]
) -> _ReviewerProcessState:
    claude = _ReviewerProcessNotFound()
    for process in sorted(table.values(), key=lambda item: item.pid):
        if not _descends_from(process, launcher_pid, table):
            continue
        words = _process_words(process)
        command = Path(process.command).name
        arguments = words[1:] if words and Path(words[0]).name == command else words
        if command == "codex" and arguments[:1] == ["exec"]:
            return _ReviewerProcess("codex", process.pid)
        if command == "claude":
            claude = _ReviewerProcess("claude", process.pid)
    return claude


def _reviewer_cwd(process: _ReviewerProcessState, fallback: str) -> str:
    if isinstance(process, _ReviewerProcessNotFound):
        return fallback
    try:
        return os.readlink(f"/proc/{process.pid}/cwd")
    except OSError:
        return fallback


def _review_workers(
    machine: str, run: _LiveRun, place: _RunPlace,
    table: Mapping[int, process_table.Process], problems: list[str],
) -> list[RosterEntry]:
    entries: list[RosterEntry] = []
    try:
        statuses = sorted(run.path.glob("review_status*"))
    except OSError as error:
        _problem(problems, f"review state in {run.path}", error)
        return entries
    for status_path in statuses:
        suffix = status_path.name.removeprefix("review_status")
        if suffix and not suffix.startswith("_"):
            continue
        lens = suffix.removeprefix("_")
        try:
            status = status_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as error:
            _problem(problems, f"review status {status_path}", error)
            continue
        if status != "reviewing":
            continue
        pid_path = run.path / f"review_pid{suffix}"
        try:
            launcher_pid = int(pid_path.read_text(encoding="utf-8").strip())
        except (OSError, UnicodeError, ValueError) as error:
            _problem(problems, f"review pid {pid_path}", error)
            continue
        if not _pid_alive(launcher_pid):
            continue
        process = _reviewer_process(launcher_pid, table)
        kind: Kind = process.kind if isinstance(process, _ReviewerProcess) else "codex"
        name = f"{lens} reviewer" if lens else "blind reviewer"
        slot_role = f"review ({lens})" if lens else "review"
        entries.append(RosterEntry(
            machine=machine,
            kind=kind,
            role="worker",
            name=name,
            status="running",
            reach=CannotReceiveMessages("blind reviewer"),
            cwd=_reviewer_cwd(process, place.cwd),
            showrunner=place.showrunner,
            director=place.director,
            assignment=WorkerAssignment(slot_role, str(run.path)),
        ))
    return entries


def _delegate_workers(
    machine: str, runs: list[_LiveRun], claude_entries: Mapping[str, RosterEntry],
    production_places: Mapping[str, _ProductionPlace],
    problems: list[str],
) -> tuple[list[RosterEntry], set[str]]:
    try:
        table = process_table.snapshot()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        _problem(problems, "process table", error)
        table = {}
    process_cwds = _process_cwds(table)
    entries: list[RosterEntry] = []
    worker_threads: set[str] = set()
    for run in runs:
        server_alive = _server_alive(run, problems)
        place = _run_place(run, claude_entries, production_places)
        for name, record in sorted(_mesh_records(run, problems).items()):
            status = _as_string(record.get("status"))
            if status == "ended":
                continue
            launcher = record.get("launcher_pid")
            launcher_alive = (
                isinstance(launcher, int)
                and not isinstance(launcher, bool)
                and _pid_alive(launcher)
            )
            if not server_alive and not launcher_alive:
                continue
            thread_id = _as_string(record.get("thread_id"))
            if thread_id:
                worker_threads.add(thread_id)
            run_path = str(run.path.resolve())
            cwd = (
                _as_string(record.get("cwd"))
                or process_cwds.get((name, run_path), "")
                or place.cwd
            )
            entries.append(RosterEntry(
                machine=machine,
                kind="codex",
                role="worker",
                name=name,
                status=_worker_status(status),
                reach=(
                    Messageable(f"seat:{run.path.name}/{name}")
                    if status in MESSAGEABLE_SEAT_STATUSES
                    else CannotReceiveMessages(_between_turns_reason(place.director))
                ),
                cwd=cwd,
                showrunner=place.showrunner,
                director=place.director,
                assignment=WorkerAssignment(_codex_slot_role(record), run_path),
                thread=CodexThread(thread_id) if thread_id else NoCodexThread(),
            ))
        entries.extend(_review_workers(machine, run, place, table, problems))
    return entries, worker_threads


def machine_name() -> str:
    return socket.gethostname().split(".", 1)[0]


def _local_roster(environment: Mapping[str, str]) -> Roster:
    problems: list[str] = []
    machine = machine_name()
    records = _newest_sessions()
    details = _raw_session_details(records, problems)
    registered = _registered_showrunners(environment, problems)
    runner_ids = _showrunner_ids(records, registered)
    root = Path(environment.get("ROSTER_DELEGATE_ROOT", "/tmp/claude/delegate"))
    runs = _live_runs(root, {record["sessionId"] for record in records}, problems)
    workers = _ledger_workers(runs, records, problems)
    production_places = _production_places(
        records, details, registered, runner_ids, set(workers), problems
    )
    claude = _claude_entries(
        machine, records, details, runner_ids, production_places, runs, workers
    )
    delegate_workers, worker_threads = _delegate_workers(
        machine, runs, claude, production_places, problems
    )
    daemon = codex_daemon.loaded_sessions(codex_daemon.daemon_socket(environment))
    codex_sessions: list[RosterEntry] = []
    if isinstance(daemon, codex_daemon.DaemonUnreadable):
        _problem(problems, "Codex daemon", daemon.reason)
    else:
        codex_sessions = [
            RosterEntry(
                machine=machine,
                kind="codex",
                role="freestanding",
                name=session.name,
                status=session.status,
                reach=Messageable(f"codex:{session.thread_id}"),
                cwd=session.cwd,
                showrunner=NoShowrunner(),
                director=NoDirector(),
                assignment=NotAWorker(),
                thread=CodexThread(session.thread_id),
            )
            for session in daemon
            if session.thread_id not in worker_threads
        ]
    entries = [*claude.values(), *delegate_workers, *codex_sessions]
    return Roster(entries, problems)


def peer_roster(environment: Mapping[str, str]) -> Roster:
    """Read only the peer, decoding states immediately at the ssh boundary."""
    host = remote.other_machine()
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host,
               '\"$HOME/.claude/scripts/lib/py\" \"$HOME/.claude/scripts/message/roster.py\" --here --snapshot']
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False,
                              env={key: value for key, value in environment.items()
                                   if key not in {"CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID"}})
    except (OSError, subprocess.TimeoutExpired) as error:
        why = "timed out after 20 s" if isinstance(error, subprocess.TimeoutExpired) else str(error)
        lines = why.splitlines()
        return Roster([], [f"{host}: not reached: {lines[0] if lines else type(error).__name__}"])
    try:
        result = RosterSnapshot.decode(done.stdout).result
    except (ValueError, TypeError):
        detail = (done.stderr or "invalid roster snapshot").strip().splitlines()
        return Roster([], [f"{host}: not reached: {detail[0] if detail else 'invalid roster snapshot'}"])
    return Roster(result.entries, [f"{host}: {problem}" for problem in result.problems])


def roster(environment: Mapping[str, str], scope: RosterScope) -> Roster:
    match scope:
        case ThisMachine():
            return _local_roster(environment)
        case AllMachines():
            with ThreadPoolExecutor(max_workers=2) as pool:
                peer = pool.submit(peer_roster, environment)
                local = _local_roster(environment)
                other = peer.result()
            return Roster([*local.entries, *other.entries], [*local.problems, *other.problems])


def _home_shortened(path: str) -> str:
    if not path:
        return ""
    home = str(Path(os.environ.get("HOME", str(Path.home()))).expanduser())
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def _entry_line(entry: RosterEntry, *, you: bool, context: bool) -> str:
    name = entry.name
    if you:
        name += " (you)"
    if context:
        name += " (context)"
    details = [entry.kind, entry.status]
    if entry.role == "worker":
        details.append(_slot_role_for(entry))
    unit = _unit(entry)
    if unit:
        details.append(unit)
    cwd = _home_shortened(entry.cwd)
    if cwd:
        details.append(cwd)
    return f"{entry.role} {name} — {', '.join(details)}"


def _chosen_entries(
    entries: list[RosterEntry], kind: Kind | None
) -> tuple[list[RosterEntry], set[RosterEntry]]:
    if kind is None:
        return entries, set()
    chosen = [entry for entry in entries if entry.kind == kind]
    contexts: set[RosterEntry] = set()
    for worker in (entry for entry in chosen if entry.role == "worker"):
        director = next(
            (
                entry
                for entry in entries
                if _director_address(worker)
                and entry.machine == worker.machine
                and entry.role == "unit director"
                and _session_address(entry) == _director_address(worker)
            ),
            None,
        )
        if director is not None and director not in chosen:
            contexts.add(director)
        runner = next(
            (
                entry
                for entry in entries
                if _showrunner_address(worker)
                and entry.machine == worker.machine
                and entry.role == "showrunner"
                and _session_address(entry) == _showrunner_address(worker)
            ),
            None,
        )
        if runner is not None and runner not in chosen:
            contexts.add(runner)
    return [*chosen, *contexts], contexts


def _print_text(
    entries: list[RosterEntry], contexts: set[RosterEntry], identity: SenderIdentity
) -> None:
    by_machine: dict[str, list[RosterEntry]] = {}
    for entry in entries:
        by_machine.setdefault(entry.machine, []).append(entry)
    for machine in by_machine:
        print(machine)
        rows = by_machine[machine]
        printed: set[RosterEntry] = set()

        def emit(entry: RosterEntry, depth: int) -> None:
            print("  " * depth + _entry_line(
                entry,
                you=machine == machine_name() and is_sender(entry, identity),
                context=entry in contexts,
            ))
            printed.add(entry)

        runners = sorted(
            (entry for entry in rows if entry.role == "showrunner"),
            key=lambda entry: (entry.name, _session_address(entry)),
        )
        for runner in runners:
            emit(runner, 1)
            directors = sorted(
                (
                    entry for entry in rows
                    if entry.role == "unit director"
                    and _showrunner_address(entry) == _session_address(runner)
                ),
                key=lambda entry: (_unit(entry), entry.name),
            )
            for director in directors:
                emit(director, 2)
                for worker in sorted(
                    (
                        entry for entry in rows
                        if entry.role == "worker"
                        and _director_address(entry) == _session_address(director)
                    ),
                    key=lambda entry: (_slot_role_for(entry), entry.name),
                ):
                    emit(worker, 3)
        independent = sorted(
            (
                entry for entry in rows
                if entry.role == "unit director" and entry not in printed
            ),
            key=lambda entry: (_unit(entry), entry.name),
        )
        for director in independent:
            emit(director, 1)
            for worker in sorted(
                (
                    entry for entry in rows
                    if entry.role == "worker"
                    and _director_address(entry) == _session_address(director)
                ),
                key=lambda entry: (_slot_role_for(entry), entry.name),
            ):
                emit(worker, 2)
        orphan_groups: dict[str, list[RosterEntry]] = {}
        for worker in (entry for entry in rows if entry.role == "worker" and entry not in printed):
            orphan_groups.setdefault(_session_dir(worker), []).append(worker)
        for session_dir, workers in sorted(orphan_groups.items()):
            print(f"  director not running: {session_dir}")
            for worker in sorted(workers, key=lambda entry: (_slot_role_for(entry), entry.name)):
                emit(worker, 2)
        for entry in sorted(
            (entry for entry in rows if entry.kind == "claude" and entry.role == "freestanding"),
            key=lambda entry: entry.name,
        ):
            emit(entry, 1)
        codex = sorted(
            (entry for entry in rows if entry.kind == "codex" and entry.role == "freestanding"),
            key=lambda entry: entry.name,
        )
        if codex:
            print("  your Codex sessions")
            for entry in codex:
                emit(entry, 2)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--json", action="store_true", dest="as_json")
    _ = parser.add_argument("--kind", choices=("claude", "codex"))
    _ = parser.add_argument("--here", action="store_true", help="read this machine only")
    _ = parser.add_argument("--snapshot", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    result = roster(os.environ, ThisMachine() if getattr(arguments, "here", False) is True else AllMachines())
    if getattr(arguments, "snapshot", False) is True:
        print(RosterSnapshot(result).encode())
        return 1 if result.problems else 0
    raw_kind = getattr(arguments, "kind", None)
    kind = raw_kind if raw_kind in ("claude", "codex") else None
    entries, contexts = _chosen_entries(result.entries, kind)
    if getattr(arguments, "as_json", False) is True:
        print(json.dumps({
            "entries": [_serialized_entry(entry) for entry in entries],
            "problems": result.problems,
        }))
    else:
        _print_text(entries, contexts, sender_identity(os.environ))
    for problem in result.problems:
        print(f"could not read: {problem}", file=sys.stderr)
    return 1 if result.problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
