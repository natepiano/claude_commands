#!/usr/bin/env python3
"""List every Claude and Codex session of interest on this machine."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shlex
import socket
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, TypedDict, cast

SCRIPTS = Path(__file__).resolve().parents[1]
for _directory in (SCRIPTS / "production", SCRIPTS / "delegate"):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

import add_unit  # noqa: E402
import broadcast  # noqa: E402
import live_units  # noqa: E402
import remove_seats  # noqa: E402
import showrunners  # noqa: E402
import unit_lookup  # noqa: E402
import codex_daemon  # noqa: E402
from sessions import SessionRecord, live_sessions  # noqa: E402

Kind = Literal["claude", "codex"]
Role = Literal["showrunner", "unit director", "worker", "freestanding"]
KNOWN_SLOT_ROLES = frozenset(("impl", "test", "fix", "review"))


@dataclass(frozen=True)
class RosterEntry:
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


@dataclass(frozen=True)
class Roster:
    entries: list[RosterEntry]
    problems: list[str]


@dataclass(frozen=True)
class _ProductionPlace:
    production: str
    showrunner: str
    showrunner_address: str
    unit: str


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
    director: str
    director_address: str
    unit: str
    production: str
    showrunner: str
    showrunner_address: str
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
    for record in live_sessions():
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
        try:
            units = live_units.production_units(doc)
        except (OSError, ValueError) as error:
            _problem(problems, f"tmux for {runner['slug']} was not read", error)
            try:
                rows = _production_rows(doc, runner["slug"])
            except (OSError, UnicodeError, ValueError, add_unit.Refusal) as fallback_error:
                _problem(problems, f"units in {doc}", fallback_error)
                continue
            for row in rows:
                unit = add_unit.cell_value(row.get("Unit", ""))
                worktree = add_unit.cell_value(row.get("Worktree", ""))
                for record in records:
                    if (
                        record["sessionId"] not in ledger_workers
                        and worktree
                        and details[record["sessionId"]].cwd == worktree
                    ):
                        _ = places.setdefault(
                            record["sessionId"],
                            _ProductionPlace(
                                runner["slug"], runner["session"], runner_address, unit
                            ),
                        )
            continue
        for unit in units:
            marked = unit.session
            claude = marked.claude if marked is not None else None
            if isinstance(claude, unit_lookup.LiveClaude):
                _ = places.setdefault(
                    claude.session_id,
                    _ProductionPlace(
                        runner["slug"], runner["session"], runner_address, unit.unit
                    ),
                )
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
    run: _LiveRun, claude_entries: Mapping[str, RosterEntry]
) -> _RunPlace:
    if isinstance(run.director, _NoRunningDirector):
        return _RunPlace("", "", "", "", "", "", "")
    director = claude_entries.get(run.director.session_id)
    if director is None:
        return _RunPlace("", "", "", "", "", "", "")
    return _RunPlace(
        director=director.name,
        director_address=director.address,
        unit=director.unit,
        production=director.production,
        showrunner=director.showrunner,
        showrunner_address=director.showrunner_address,
        cwd=director.cwd,
    )


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


def _process_cwds(table: Mapping[int, broadcast.Process]) -> dict[tuple[str, str], str]:
    found: dict[tuple[str, str], str] = {}
    for process in table.values():
        try:
            words = shlex.split(process.arguments)
        except ValueError:
            words = process.arguments.split()
        options = dict(zip(words, words[1:]))
        name = options.get("--name") or options.get("--to")
        directory = options.get("--session-dir")
        if broadcast.is_codex_agent(process) and name and directory:
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
        production = showrunner = showrunner_address = unit = ""
        director = director_address = slot_role = session_dir = ""
        if session_id in runner_ids:
            role: Role = "showrunner"
            production = runner_ids[session_id]["slug"]
        elif session_id in production_places:
            role = "unit director"
            place = production_places[session_id]
            production = place.production
            showrunner = place.showrunner
            showrunner_address = place.showrunner_address
            unit = place.unit
        elif session_id in marker_directors:
            role = "unit director"
        elif session_id in workers:
            role = "worker"
            worker = workers[session_id]
            session_dir = str(worker.run.path)
            run_director = worker.run.director
            director_entry = (
                records_by_id.get(run_director.session_id)
                if isinstance(run_director, _DirectorSession) else None
            )
            if director_entry is not None and isinstance(run_director, _DirectorSession):
                director = director_entry["name"]
                director_address = f"session:{run_director.session_id}"
                director_place = production_places.get(run_director.session_id)
                if director_place is not None:
                    production = director_place.production
                    showrunner = director_place.showrunner
                    showrunner_address = director_place.showrunner_address
                    unit = director_place.unit
            slot_role = _slot_role(worker.seat_name or record["name"])
        else:
            role = "freestanding"
        detail = details[session_id]
        entries[session_id] = RosterEntry(
            machine, "claude", role, record["name"], detail.status,
            f"session:{session_id}", detail.cwd, production, showrunner,
            showrunner_address, unit, director, director_address, slot_role, session_dir,
        )
    return entries


def _process_words(process: broadcast.Process) -> list[str]:
    try:
        return shlex.split(process.arguments)
    except ValueError:
        return process.arguments.split()


def _descends_from(
    process: broadcast.Process, ancestor: int, table: Mapping[int, broadcast.Process]
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
    launcher_pid: int, table: Mapping[int, broadcast.Process]
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
    table: Mapping[int, broadcast.Process], problems: list[str],
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
            machine, kind, "worker", name, "running", "",
            _reviewer_cwd(process, place.cwd), place.production, place.showrunner,
            place.showrunner_address, place.unit, place.director, place.director_address,
            slot_role, str(run.path),
        ))
    return entries


def _delegate_workers(
    machine: str, runs: list[_LiveRun], claude_entries: Mapping[str, RosterEntry],
    problems: list[str],
) -> tuple[list[RosterEntry], set[str]]:
    try:
        table = broadcast.processes()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        _problem(problems, "process table", error)
        table = {}
    process_cwds = _process_cwds(table)
    entries: list[RosterEntry] = []
    worker_threads: set[str] = set()
    for run in runs:
        server_alive = _server_alive(run, problems)
        place = _run_place(run, claude_entries)
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
                machine, "codex", "worker", name, _worker_status(status), name,
                cwd, place.production, place.showrunner, place.showrunner_address,
                place.unit, place.director, place.director_address,
                _codex_slot_role(record), run_path,
            ))
        entries.extend(_review_workers(machine, run, place, table, problems))
    return entries, worker_threads


def roster(environment: Mapping[str, str]) -> Roster:
    problems: list[str] = []
    machine = socket.gethostname().split(".", 1)[0]
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
    delegate_workers, worker_threads = _delegate_workers(machine, runs, claude, problems)
    daemon = codex_daemon.loaded_sessions(codex_daemon.daemon_socket(environment))
    codex_sessions: list[RosterEntry] = []
    if isinstance(daemon, codex_daemon.DaemonUnreadable):
        _problem(problems, "Codex daemon", daemon.reason)
    else:
        codex_sessions = [
            RosterEntry(
                machine, "codex", "freestanding", session.name, session.status,
                f"codex:{session.thread_id}", session.cwd,
                "", "", "", "", "", "", "", "",
            )
            for session in daemon
            if session.thread_id not in worker_threads
        ]
    entries = [*claude.values(), *delegate_workers, *codex_sessions]
    return Roster(entries, problems)


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
        details.append(entry.slot_role)
    if entry.unit:
        details.append(entry.unit)
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
                if worker.director_address
                and entry.role == "unit director"
                and entry.address == worker.director_address
            ),
            None,
        )
        if director is not None and director not in chosen:
            contexts.add(director)
        runner = next(
            (
                entry
                for entry in entries
                if worker.showrunner_address
                and entry.role == "showrunner"
                and entry.address == worker.showrunner_address
            ),
            None,
        )
        if runner is not None and runner not in chosen:
            contexts.add(runner)
    return [*chosen, *contexts], contexts


def _print_text(
    entries: list[RosterEntry], contexts: set[RosterEntry], current_session: str
) -> None:
    by_machine: dict[str, list[RosterEntry]] = {}
    for entry in entries:
        by_machine.setdefault(entry.machine, []).append(entry)
    for machine in sorted(by_machine):
        print(machine)
        rows = by_machine[machine]
        printed: set[RosterEntry] = set()

        def emit(entry: RosterEntry, depth: int) -> None:
            print("  " * depth + _entry_line(
                entry,
                you=entry.kind == "claude" and entry.address == f"session:{current_session}",
                context=entry in contexts,
            ))
            printed.add(entry)

        runners = sorted(
            (entry for entry in rows if entry.role == "showrunner"),
            key=lambda entry: (entry.name, entry.address),
        )
        for runner in runners:
            emit(runner, 1)
            directors = sorted(
                (
                    entry for entry in rows
                    if entry.role == "unit director"
                    and entry.showrunner_address == runner.address
                ),
                key=lambda entry: (entry.unit, entry.name),
            )
            for director in directors:
                emit(director, 2)
                for worker in sorted(
                    (
                        entry for entry in rows
                        if entry.role == "worker"
                        and entry.director_address == director.address
                    ),
                    key=lambda entry: (entry.slot_role, entry.name),
                ):
                    emit(worker, 3)
        independent = sorted(
            (
                entry for entry in rows
                if entry.role == "unit director" and entry not in printed
            ),
            key=lambda entry: (entry.unit, entry.name),
        )
        for director in independent:
            emit(director, 1)
            for worker in sorted(
                (
                    entry for entry in rows
                    if entry.role == "worker"
                    and entry.director_address == director.address
                ),
                key=lambda entry: (entry.slot_role, entry.name),
            ):
                emit(worker, 2)
        orphan_groups: dict[str, list[RosterEntry]] = {}
        for worker in (entry for entry in rows if entry.role == "worker" and entry not in printed):
            orphan_groups.setdefault(worker.session_dir, []).append(worker)
        for session_dir, workers in sorted(orphan_groups.items()):
            print(f"  director not running: {session_dir}")
            for worker in sorted(workers, key=lambda entry: (entry.slot_role, entry.name)):
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
    arguments = parser.parse_args(argv)
    result = roster(os.environ)
    raw_kind = getattr(arguments, "kind", None)
    kind = raw_kind if raw_kind in ("claude", "codex") else None
    entries, contexts = _chosen_entries(result.entries, kind)
    if getattr(arguments, "as_json", False) is True:
        print(json.dumps({"entries": [asdict(entry) for entry in entries], "problems": result.problems}))
    else:
        _print_text(entries, contexts, os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    for problem in result.problems:
        print(f"could not read: {problem}", file=sys.stderr)
    return 1 if result.problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
