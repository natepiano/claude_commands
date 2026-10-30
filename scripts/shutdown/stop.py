#!/usr/bin/env python3
"""Stop one machine's sessions after shutdown settlement has completed."""

from __future__ import annotations

import json
import os
import signal
import re
import subprocess
import sys
import copy
import fcntl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import import_module
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Protocol, TypedDict, TypeVar, cast

SCRIPTS = Path(__file__).resolve().parents[1]
for dependency in ("message", "production", "herdr", "lib"):
    sys.path.insert(0, str(SCRIPTS / dependency))

import sessions
import inventory
import herdr
import relaunch
import unit_lookup
from inventory import (Inventory, Session, ShutdownProcess, ProcessObservation, ProcessIdentity,
    ProcessLive, ProcessGone, ProcessUnknown, shutdown_identity, shutdown_processes, terminal_kind, live_supervisor, codex_seats)
from user_zone import user_zone
from record import (
    CLAIM_STALE_SECONDS,
    NoLiveRecord,
    archive_session,
    NotReadyToStop,
    ReadinessCheck,
    SessionProgress,
    SessionShutdown,
    PausedSeat,
    SessionStopIssue,
    find_session,
    update_session,
)

class ShutdownReadiness(Protocol):
    def stoppable_status(self, status: str) -> bool: ...

    def blocker(self, session: Session, *, moving: bool = False) -> ReadinessCheck: ...

    def request_pause(self, saved: SessionShutdown) -> None: ...


settle = cast(ShutdownReadiness, cast(object, import_module("settle")))



CODEX_MESH = SCRIPTS / "agents" / "codex_mesh.py"
STOP_WAIT = timedelta(seconds=10)
STOP_POLL_SECONDS = 0.1
SHUTDOWN_GRACE_SECONDS = 60.0
TERM_GRACE_SECONDS = 5.0

KillProcess = Callable[[int, int], None]
LivenessCheck = Callable[[int], bool]
SessionRecordPresence = Callable[[int], bool]
FreshInventory = Callable[[str, frozenset[str]], Inventory]
Clock = Callable[[], datetime]
CommandRunner = Callable[[list[str]], int | None]
Sleeper = Callable[[float], None]
IdleStopReason = Literal[
    "not an idle stop",
    "idle; nothing in progress",
    "idle after the pause message; no ready line",
    "idle; nothing in progress; a background shell was running",
    "idle after the pause message; no ready line; a background shell was running",
]


class StopClaimLost(Exception):
    """Another worker or request has replaced this stop claim."""


@dataclass(frozen=True)
class StopClaim:
    login: str
    session_id: str
    request_id: str
    at: str

    def require(self, current: SessionShutdown) -> None:
        progress = current["progress"]
        if (
            current["request_id"] != self.request_id
            or "stop_deadline" in current
            or progress["kind"] != "stopping"
            or progress["at"] != self.at
        ):
            raise StopClaimLost()

    def check(self) -> None:
        current = find_session(self.login, self.session_id)
        if current is None:
            raise NoLiveRecord("session shutdown disappeared while stopping")
        self.require(current)


class MatchingSession(TypedDict):
    kind: Literal["matching session"]
    session: Session
    workers: list[Session]


class SessionAbsent(TypedDict):
    kind: Literal["session absent"]


class SessionIdentityLost(TypedDict):
    kind: Literal["session identity lost"]


class SessionAccountUnreadable(TypedDict):
    kind: Literal["session account unreadable"]


FreshSession = (
    MatchingSession | SessionAbsent | SessionIdentityLost | SessionAccountUnreadable
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def process_is_alive(pid: int) -> bool:
    """Return whether the operating system still has this process."""
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def session_record_exists(pid: int) -> bool:
    """Return whether Claude's session record for this process remains."""
    return (sessions.sessions_dir() / f"{pid}.json").exists()


def run_codex_mesh(arguments: list[str]) -> int | None:
    return _run([sys.executable, str(CODEX_MESH), *arguments])


def run_systemctl(arguments: list[str]) -> int | None:
    return _run(["systemctl", *arguments])


def _run(command: list[str]) -> int | None:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.returncode


def _timestamp(clock: Clock) -> str:
    return clock().astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _fresh_inventory(login: str, only: frozenset[str]) -> Inventory:
    from inventory import inventory

    return inventory(login, only=only, refresh_desktops=False)


def _same_process(stored: Session) -> bool:
    """Whether the recorded process still runs with its recorded start."""
    return bool(stored["proc_start"]) and sessions.process_start(stored["pid"]) == stored["proc_start"]


def fresh_session(stored: Session, report: Inventory) -> FreshSession:
    session_id = stored["session_id"]
    current = next(
        (item for item in report["sessions"] if item["session_id"] == session_id),
        None,
    )
    if current is not None:
        if (
            current["pid"] != stored["pid"]
            or current["proc_start"] != stored["proc_start"]
        ):
            return {"kind": "session identity lost"}
        return {
            "kind": "matching session", "session": current,
            "workers": [
                item for item in report["sessions"]
                if item["kind"] == "worker"
                and item["owner"]["kind"] == "director"
                and item["owner"]["session_id"] == session_id
            ],
        }
    reason = next(
        (
            item["reason"]
            for item in report["unattributed"]
            if item["pid"] == stored["pid"]
        ),
        "",
    )
    if reason == "process start mismatch":
        return {"kind": "session identity lost"}
    if reason == "account unreadable":
        return {"kind": "session account unreadable"}
    return {"kind": "session absent"}


def _set_stop_outcome(
    claim: StopClaim,
    progress: Literal["stopped", "already gone", "process identity lost", "stop failed"],
    clock: Clock,
    issues: list[SessionStopIssue],
    reason: str = "",
) -> SessionShutdown:
    def change(current: SessionShutdown) -> None:
        claim.require(current)
        at = _timestamp(clock)
        if progress == "stop failed":
            current["progress"] = {"kind": "stop failed", "at": at, "reason": reason}
        elif progress == "stopped":
            current["progress"] = {"kind": "stopped", "at": at}
        elif progress == "already gone":
            current["progress"] = {"kind": "already gone", "at": at}
        else:
            current["progress"] = {"kind": "process identity lost", "at": at}
        current["stop_issues"] = [*issues, *(issue for issue in current["stop_issues"]
            if issue["kind"] == "legacy tmux session left running" and issue not in issues)]

    result = update_session(claim.login, claim.session_id, change)
    if progress in {"stopped", "already gone"}:
        elapsed = max(0.0, (clock() - datetime.fromisoformat(result["requested_at"])).total_seconds())
        print(f"{clock().astimezone(user_zone()):%H:%M:%S}  stopped  {result['session']['name']} (by itself, {elapsed:.1f} s)", flush=True)
    return result


def _replace_stop_issues(
    claim: StopClaim, issues: list[SessionStopIssue]
) -> SessionShutdown:
    def replace(current: SessionShutdown) -> None:
        claim.require(current)
        current["stop_issues"] = [*issues, *(issue for issue in current["stop_issues"]
            if issue["kind"] == "legacy tmux session left running" and issue not in issues)]

    return update_session(claim.login, claim.session_id, replace)


def _wait_for_exit(
    pid: int,
    *,
    is_alive: LivenessCheck,
    session_record_exists: SessionRecordPresence,
    clock: Clock,
    sleep: Sleeper,
    claim: StopClaim,
) -> bool:
    deadline = clock() + STOP_WAIT
    for _ in range(101):
        claim.check()
        if not is_alive(pid) and not session_record_exists(pid):
            return True
        current = clock()
        if current >= deadline:
            return False
        remaining = max(0.0, (deadline - current).total_seconds())
        sleep(min(STOP_POLL_SECONDS, remaining))
    return not is_alive(pid) and not session_record_exists(pid)


def _ignore_read_error(_error: str) -> None:
    pass


def _json_object(path: Path, report: Callable[[str], None] = _ignore_read_error) -> dict[str, object]:
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        report(f"ownership record {path}: {error}")
        return {}
    if not isinstance(value, dict):
        report(f"ownership record {path}: expected an object")
        return {}
    return cast(dict[str, object], value)


def _mesh_server_pids(run_dir: str, report: Callable[[str], None] = _ignore_read_error) -> set[int]:
    folder = Path(run_dir)
    records: list[dict[str, object]] = [_json_object(folder / "mesh_server.json", report)]
    retired = _json_object(folder / "mesh_retired.json", report).get("servers")
    if isinstance(retired, list):
        records.extend(
            cast(dict[str, object], item)
            for item in cast(list[object], retired)
            if isinstance(item, dict)
        )
    return {
        pid
        for item in records
        for pid in [item.get("pid")]
        if isinstance(pid, int) and not isinstance(pid, bool)
    }


def _run_directories(session: Session) -> list[str]:
    return list(
        dict.fromkeys(
            [*session["run_dirs"], *(item["run_dir"] for item in session["codex_servers"])]
        )
    )


RESUME_PROMPT = (
    "You were paused when your unit director stopped for an account move or restart. "
    "Continue the task you were on from where you stopped; your earlier instructions still hold."
)
IDLE_RESUME_PROMPT = (
    "You were paused while idle when your unit director stopped for an account move or restart. "
    "Nothing was in progress: reply ready and wait for your next assignment; your earlier instructions still hold."
)


def _capture_codex_seats(session: Session) -> list[PausedSeat]:
    seats = codex_seats([Path(directory) for directory in _run_directories(session)])
    for seat in seats:
        prompt = RESUME_PROMPT if seat["turn_live"] else IDLE_RESUME_PROMPT
        _ = (Path(seat["run_dir"]) / f"{seat['seat']}.resume-prompt.md").write_text(prompt + "\n", encoding="utf-8")
    return seats


def _save_codex_seats(session: Session, claim: StopClaim) -> None:
    claim.check()
    seats = _capture_codex_seats(session)
    def save(current: SessionShutdown) -> None:
        claim.require(current)
        # Retried cleanup must retain the argv read before the launcher exited.
        previous = {(seat["run_dir"], seat["seat"]): seat for seat in current.get("codex_seats", [])}
        for seat in seats:
            key = seat["run_dir"], seat["seat"]
            if key not in previous:
                previous[key] = seat
        current["codex_seats"] = list(previous.values())
    _ = update_session(claim.login, claim.session_id, save)


def _stop_codex_servers(
    session: Session,
    *,
    codex_mesh: CommandRunner,
    is_alive: LivenessCheck,
    clock: Clock,
    claim: StopClaim,
) -> list[SessionStopIssue]:
    issues: list[SessionStopIssue] = []
    for run_dir in _run_directories(session):
        server_pids = _mesh_server_pids(run_dir)
        claim.check()
        status = codex_mesh(["stop", "--pause", "--session-dir", run_dir])
        if status != 0 or any(is_alive(pid) for pid in server_pids):
            issues.append(
                {
                    "kind": "codex server left running",
                    "at": _timestamp(clock),
                    "run_dir": run_dir,
                    "cause": "stop not confirmed",
                }
            )
    return issues


def _stop_unit_session(
    session: Session, clock: Clock, claim: StopClaim
) -> list[SessionStopIssue]:
    host = session["host"]
    if session["kind"] != "unit" or host["kind"] != "unit":
        return []
    name = host["session_name"]
    issue: SessionStopIssue = {
        "kind": "unit session left running",
        "at": _timestamp(clock),
        "host": "herdr",
        "session_name": name,
        "cause": "stop not confirmed",
    }
    claim.check()
    if name == herdr.shared_session():
        # The shared session holds every production: close only the tab of the pane that carries this unit's marks.
        client = herdr.Herdr(name)
        try:
            marked = unit_lookup.marked_unit(host["production"], host["unit"])
            if marked is not None and marked.place.session == name:
                client.close_tab_of(marked.place.pane)
            left = marked is not None and any(pane["pane_id"] == marked.place.pane for pane in client.panes())
        except (herdr.HerdrFailure, OSError, subprocess.TimeoutExpired):
            left = True
        finally:
            claim.check()
        return [issue] if left else []
    try:
        herdr.stop_session(name)
    except (herdr.HerdrFailure, OSError, subprocess.TimeoutExpired):
        pass
    finally:
        claim.check()
    try:
        remaining = herdr.live_sessions()
    except (herdr.HerdrFailure, OSError, subprocess.TimeoutExpired):
        return [issue]
    finally:
        claim.check()
    return [issue] if any(row["name"] == name for row in remaining) else []


def _process_row(pid: int) -> tuple[int, str] | None:
    try:
        completed = subprocess.run(
            ("ps", "-o", "ppid=,comm=", "-p", str(pid)),
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    fields = completed.stdout.strip().split(maxsplit=1)
    if completed.returncode != 0 or len(fields) != 2:
        return None
    try:
        return int(fields[0]), fields[1]
    except ValueError:
        return None


def _ghostty_still_owns_shell(shell: int) -> bool:
    child = _process_row(shell)
    if child is None:
        return False
    parent = _process_row(child[0])
    return parent is not None and terminal_kind(parent[1]) == "ghostty"


def _close_ghostty_window(
    session: Session,
    *,
    kill: KillProcess,
    is_alive: LivenessCheck,
    claim: StopClaim,
) -> None:
    host = session["host"]
    if host["kind"] != "ghostty":
        return
    shell = host["window_shell"]
    if not is_alive(shell) or not _ghostty_still_owns_shell(shell):
        return
    try:
        claim.check()
        kill(shell, signal.SIGHUP)
    except OSError:
        pass


def _stop_owner_resources(
    session: Session,
    *,
    kill: KillProcess,
    is_alive: LivenessCheck,
    codex_mesh: CommandRunner,
    clock: Clock,
    claim: StopClaim,
    retain_window: bool = False,
) -> list[SessionStopIssue]:
    if not retain_window:
        _close_ghostty_window(session, kill=kill, is_alive=is_alive, claim=claim)
    issues = _stop_codex_servers(
        session, codex_mesh=codex_mesh, is_alive=is_alive, clock=clock, claim=claim
    )
    if session["host"]["kind"] == "herdr":
        # Leave the pane and server running; herdr session cleanup is manual for now.
        return issues
    issues.extend(_stop_unit_session(session, clock, claim))
    return issues


def _owner_identity_lost_issues(
    session: Session, clock: Clock
) -> list[SessionStopIssue]:
    issues: list[SessionStopIssue] = [
        {
            "kind": "codex server left running",
            "at": _timestamp(clock),
            "run_dir": run_dir,
            "cause": "owner identity lost",
        }
        for run_dir in _run_directories(session)
    ]
    if session["kind"] == "unit" and session["host"]["kind"] == "unit":
        issues.append(
            {
                "kind": "unit session left running",
                "at": _timestamp(clock),
                "host": session["host"]["session_host"],
                "session_name": session["host"]["session_name"],
                "cause": "owner identity lost",
            }
        )
    return issues


def _retry_owner_cleanup(
    claim: StopClaim,
    entry: SessionShutdown,
    *,
    is_alive: LivenessCheck,
    codex_mesh: CommandRunner,
    clock: Clock,
) -> None:
    """Retry only unconfirmed resources of an owner already safely stopped."""
    pending = [
        issue for issue in entry["stop_issues"]
        if (
            issue["kind"] == "codex server left running"
            or issue["kind"] == "unit session left running"
        ) and issue["cause"] == "stop not confirmed"
    ]
    if not pending:
        return
    issues = [issue for issue in entry["stop_issues"] if issue not in pending]
    session = entry["session"].copy()
    session["run_dirs"] = [
        issue["run_dir"] for issue in pending
        if issue["kind"] == "codex server left running"
    ]
    session["codex_servers"] = []
    _save_codex_seats(session, claim)
    issues.extend(_stop_codex_servers(
        session, codex_mesh=codex_mesh, is_alive=is_alive, clock=clock, claim=claim
    ))
    if any(issue["kind"] == "unit session left running" for issue in pending):
        issues.extend(_stop_unit_session(session, clock, claim))
    _ = _replace_stop_issues(claim, issues)


def _hold_supervised_window(entry: SessionShutdown, claim: StopClaim) -> bool:
    """Write a restart's hold before Claude exits, so its supervisor keeps the window."""
    host = entry["session"]["host"]
    if entry["then"] != "restart" or not (host["kind"] == "ghostty" or (
            host["kind"] == "terminal" and sys.platform == "darwin")):
        return False
    supervisor = live_supervisor(host)
    if supervisor["kind"] != "supervisor":
        return False
    claim.check()
    relaunch.write_hold(supervisor["pid"])
    return True


def _stop_owner(
    claim: StopClaim,
    entry: SessionShutdown,
    *,
    prior: SessionProgress,
    idle_reason: IdleStopReason,
    force_now: bool,
    kill: KillProcess,
    is_alive: LivenessCheck,
    session_record_exists: SessionRecordPresence,
    fresh_inventory: FreshInventory,
    clock: Clock,
    codex_mesh: CommandRunner,
    sleep: Sleeper,
) -> None:
    stored = entry["session"]
    retain_window = False
    report = fresh_inventory(claim.login, frozenset({stored["session_id"]}))
    match = fresh_session(stored, report)
    if match["kind"] == "session identity lost":
        _ = _set_stop_outcome(
            claim,
            "process identity lost",
            clock,
            _owner_identity_lost_issues(stored, clock),
        )
        return
    if match["kind"] == "session account unreadable" and not (force_now and _same_process(stored)):
        _ = _set_stop_outcome(
            claim, "stop failed", clock,
            [{"kind": "account unreadable", "at": _timestamp(clock)}],
            "account unreadable",
        )
        return
    if match["kind"] == "session absent":
        _save_codex_seats(stored, claim)
        try:
            retain_window = _hold_supervised_window(entry, claim)
        except OSError as error:
            _ = _set_stop_outcome(claim, "stop failed", clock, [], str(error))
            return
        issues = _stop_owner_resources(
            stored,
            kill=kill,
            is_alive=is_alive,
            codex_mesh=codex_mesh,
            clock=clock,
            claim=claim,
            retain_window=retain_window,
        )
        _ = _set_stop_outcome(
            claim, "already gone", clock, issues
        )
        return

    # A forced stop of a session whose account cannot be read goes by its process identity alone.
    current, workers = (match["session"], match["workers"]) if match["kind"] == "matching session" else (stored, [])
    if not force_now and prior["kind"] != "reaping" and (
        (prior["kind"] not in {"ready", "held by usage limit", "stopping", "stop failed"}
         and not (prior["kind"] == "waiting" and idle_reason != "not an idle stop"))
        or not settle.stoppable_status(current["status"])
        or any(worker["status"] != "idle" for worker in workers)
        or settle.blocker(current, moving=bool(entry.get("target")))["kind"] != "clear"
    ):
        issue = NotReadyToStop(
            kind="not ready to stop", at=_timestamp(clock),
            status=current["status"],
            progress=("held by usage limit" if prior["kind"] == "held by usage limit"
                      else "ready" if prior["kind"] == "ready" else "waiting"),
        )
        def wait_until_ready(changing: SessionShutdown) -> None:
            claim.require(changing)
            if issue["progress"] == "held by usage limit":
                changing["progress"] = {"kind": "held by usage limit", "at": issue["at"]}
            elif issue["progress"] == "ready":
                changing["progress"] = {"kind": "ready", "at": issue["at"]}
            else:
                changing["progress"] = {"kind": "waiting"}
            changing["stop_issues"] = [issue]

        _ = update_session(claim.login, claim.session_id, wait_until_ready)
        return
    _save_codex_seats(current, claim)
    try:
        retain_window = _hold_supervised_window(entry, claim)
    except OSError as error:
        _ = _set_stop_outcome(claim, "stop failed", clock, [], str(error))
        return
    try:
        claim.check()
        kill(stored["pid"], signal.SIGTERM)
    except ProcessLookupError:
        issues = _stop_owner_resources(
            stored,
            kill=kill,
            is_alive=is_alive,
            codex_mesh=codex_mesh,
            clock=clock,
            claim=claim,
            retain_window=retain_window,
        )
        _ = _set_stop_outcome(
            claim, "already gone", clock, issues
        )
        return
    except OSError as error:
        reason = str(error) or error.__class__.__name__
        issues: list[SessionStopIssue] = [
            {
                "kind": "still running",
                "at": _timestamp(clock),
                "reason": reason,
            }
        ]
        _ = _set_stop_outcome(
            claim,
            "stop failed",
            clock,
            issues,
            reason,
        )
        return

    stopped_progress: Literal["stopped", "already gone"] = "stopped"
    if not _wait_for_exit(
        stored["pid"],
        is_alive=is_alive,
        session_record_exists=session_record_exists,
        clock=clock,
        sleep=sleep,
        claim=claim,
    ):
        second_report = fresh_inventory(claim.login, frozenset({stored["session_id"]}))
        second = fresh_session(stored, second_report)
        if second["kind"] == "session identity lost":
            _ = _set_stop_outcome(
                claim,
                "process identity lost",
                clock,
                _owner_identity_lost_issues(stored, clock),
            )
            return
        if second["kind"] == "session account unreadable" and not (force_now and _same_process(stored)):
            _ = _set_stop_outcome(
                claim, "stop failed", clock,
                [{"kind": "account unreadable", "at": _timestamp(clock)}],
                "account unreadable",
            )
            return
        if second["kind"] == "session absent":
            stopped_progress = "already gone"
        else:
            try:
                claim.check()
                kill(stored["pid"], signal.SIGTERM)
            except ProcessLookupError:
                stopped_progress = "already gone"
            except OSError as error:
                reason = str(error) or error.__class__.__name__
                issues = [
                    {
                        "kind": "still running",
                        "at": _timestamp(clock),
                        "reason": reason,
                    }
                ]
                _ = _set_stop_outcome(
                    claim,
                    "stop failed",
                    clock,
                    issues,
                    reason,
                )
                return
            else:
                if not _wait_for_exit(
                    stored["pid"],
                    is_alive=is_alive,
                    session_record_exists=session_record_exists,
                    clock=clock,
                    sleep=sleep,
                    claim=claim,
                ):
                    reason = "alive after two SIGTERMs"
                    issues = [
                        {
                            "kind": "still running",
                            "at": _timestamp(clock),
                            "reason": reason,
                        }
                    ]
                    _ = _set_stop_outcome(
                        claim,
                        "stop failed",
                        clock,
                        issues,
                        reason,
                    )
                    return

    issues = _stop_owner_resources(
        stored,
        kill=kill,
        is_alive=is_alive,
        codex_mesh=codex_mesh,
        clock=clock,
        claim=claim,
        retain_window=retain_window,
    )
    _ = _set_stop_outcome(
        claim, stopped_progress, clock, issues
    )


def stop_session(
    record: SessionShutdown,
    *,
    idle_reason: IdleStopReason = "not an idle stop",
    forced: bool = False,
    kill: KillProcess = os.kill,
    is_alive: LivenessCheck = process_is_alive,
    session_record_exists: SessionRecordPresence = session_record_exists,
    fresh_inventory: FreshInventory = _fresh_inventory,
    clock: Clock = now_utc,
    codex_mesh: CommandRunner = run_codex_mesh,
    sleep: Sleeper = time.sleep,
) -> SessionShutdown:
    """Claim and stop one owner, checking identity before each signal."""
    claimed = False
    prior: SessionProgress = record["progress"]

    def claim(current: SessionShutdown) -> None:
        nonlocal claimed, prior
        if current["request_id"] != record["request_id"] or "stop_deadline" in current:
            return
        progress = current["progress"]
        if progress["kind"] == "stopping":
            age = (clock() - datetime.fromisoformat(progress["at"])).total_seconds()
            if age < CLAIM_STALE_SECONDS:
                return
        elif progress["kind"] not in {
            "waiting", "ready", "held by usage limit", "reaping", "stop failed", "stopped", "already gone", "process identity lost", "restart failed",
        }:
            return
        prior = copy.deepcopy(progress)
        current["progress"] = {"kind": "stopping", "at": _timestamp(clock)}
        if idle_reason != "not an idle stop" and prior["kind"] == "waiting":
            current["where"] = {"kind": "said", "text": idle_reason, "at": current["progress"]["at"]}
        claimed = True

    current = update_session(record["login"], record["session"]["session_id"], claim)
    if not claimed:
        return current
    progress = current["progress"]
    if progress["kind"] != "stopping":
        return current
    owned = StopClaim(current["login"], current["session"]["session_id"], current["request_id"], progress["at"])
    try:
        if prior["kind"] in {"stopped", "already gone", "process identity lost", "stop failed", "restart failed"}:
            _retry_owner_cleanup(
                owned, current, is_alive=is_alive,
                codex_mesh=codex_mesh, clock=clock,
            )
        if prior["kind"] in {"stopped", "already gone", "process identity lost", "restart failed"}:
            def restore_progress(changing: SessionShutdown) -> None:
                owned.require(changing)
                changing["progress"] = prior

            return update_session(current["login"], current["session"]["session_id"], restore_progress)
        _stop_owner(
            owned, current, prior=prior, idle_reason=idle_reason,
            force_now=forced or current["force"] == "now",
            kill=kill, is_alive=is_alive,
            session_record_exists=session_record_exists,
            fresh_inventory=fresh_inventory, clock=clock,
            codex_mesh=codex_mesh, sleep=sleep,
        )
    except StopClaimLost:
        pass

    finished = find_session(current["login"], current["session"]["session_id"])
    if finished is None:
        raise NoLiveRecord("session shutdown disappeared while stopping")
    return finished


@dataclass(frozen=True)
class StopRequested:
    """The owner may still answer its pause or exit by itself."""


@dataclass(frozen=True)
class StopSignalled:
    at: float
    forced: bool


@dataclass(frozen=True)
class StopComplete:
    """This owner's stop has been recorded and printed."""


@dataclass
class BoundedOwner:
    record: SessionShutdown
    processes: dict[int, ShutdownProcess]
    builds: dict[int, str]
    units: set[str]
    state: StopRequested | StopSignalled | StopComplete = field(default_factory=StopRequested)
    reported_builds: set[int] = field(default_factory=set)
    protected: set[int] = field(default_factory=set)
    pause_started: bool = False
    pause_done: threading.Event = field(default_factory=threading.Event)
    pause_issues: list[SessionStopIssue] = field(default_factory=list)
    targets: dict[tuple[int, str], StopTarget] = field(default_factory=dict)
    permanent_keeps: set[tuple[int, str]] = field(default_factory=set)
    worker_targets: set[tuple[int, str]] = field(default_factory=set)
    pause_observed: bool = False
    required_observation: int = 1
    pause_finished_at: float = 0
    last_signal_at: float = 0
    forced: bool = False
    claims_released: bool = False
    stop_errors: list[str] = field(default_factory=list)
    ownership_errors: list[str] = field(default_factory=list)
    replaced_parents: set[int] = field(default_factory=set)
    cgroups: set[str] = field(default_factory=set)
    initial_scopes: dict[tuple[int, str], frozenset[str]] = field(default_factory=dict)
    other_director_scopes: set[str] = field(default_factory=set)
    machine_wide: bool = False
    environment_unread: set[int] = field(default_factory=set)
    cgroup_unread: set[int] = field(default_factory=set)


@dataclass
class StopTarget:
    process: ShutdownProcess
    term_at: float = -1
    kill_sent: bool = False
    retired: bool = False


BudgetResult = TypeVar("BudgetResult")


@dataclass(frozen=True)
class StopDeadline:
    """One absolute budget, reserving the last second for observation only."""
    expires: float
    monotonic: Callable[[], float]

    def remaining(self, *, verification: bool = False) -> float:
        return max(0, self.expires - (0 if verification else 1) - self.monotonic())

    def call(self, operation: Callable[[float], BudgetResult],
             failed: Callable[[str], BudgetResult], *, verification: bool = False) -> BudgetResult:
        budget = self.remaining(verification=verification)
        if budget <= 0:
            return failed("shutdown deadline exhausted")
        values: list[BudgetResult] = []
        errors: list[str] = []
        finished = threading.Event()
        def work() -> None:
            try:
                values.append(operation(budget))
            except Exception as error:
                errors.append(str(error))
            finally:
                finished.set()
        threading.Thread(target=work, daemon=True).start()
        if not finished.wait(timeout=budget):
            return failed("operation exceeded shutdown deadline")
        if errors:
            return failed(errors[0])
        return values[0]


def _same_stop_ownership(expected: SessionShutdown, current: SessionShutdown) -> bool:
    """The stop claim covers this director and exactly these recorded owners."""
    return (expected["request_id"] == current["request_id"]
        and expected["session"] == current["session"]
        and expected["workers"] == current["workers"]
        and expected.get("codex_seats", []) == current.get("codex_seats", []))


def _predates_owners(owner: BoundedOwner, started: str) -> bool:
    starts = [owner.record["session"]["proc_start"],
              *(worker["proc_start"] for worker in owner.record["workers"])]
    def parsed(value: str) -> float | None:
        try:
            return float(int(value))
        except ValueError:
            try:
                return datetime.strptime(value, "%a %b %d %H:%M:%S %Y").timestamp()
            except ValueError:
                return None
    earliest = [number for value in starts if (number := parsed(value)) is not None]
    candidate = parsed(started)
    return candidate is not None and bool(earliest) and candidate < min(earliest)


def _cgroup_relation(owner: BoundedOwner, process: ShutdownProcess) -> Literal["owned", "unrelated", "unknown"]:
    """Readable Linux membership scopes unreadable environment evidence."""
    if (process.session_id and process.session_id != owner.record["session"]["session_id"]
            and not process.ownership_unreadable and process.pid not in owner.environment_unread):
        return "unrelated"
    if process.ownership_unreadable or process.pid in owner.cgroup_unread:
        return "unknown"
    if not process.cgroups:
        return "unknown" if process.pid in owner.environment_unread else "unrelated"
    shared_server = herdr.server_unit(herdr.shared_session())
    for group in process.cgroups:
        path = Path(group)
        if path.name in owner.units:
            return "owned"
        if path.name != shared_server and group in owner.cgroups and "app-agents.slice" in path.parts:
            return "owned"
    # Shared membership proves neither server identity nor pane ownership.
    if any(Path(group).name == shared_server for group in process.cgroups):
        return "unknown"
    if any("app-agents.slice" in Path(group).parts for group in process.cgroups):
        # Each recorded identity needs its own first-observation scope. A live
        # other director can independently exclude its scope when ours is lost.
        recorded = {(entry["pid"], entry["proc_start"])
                    for entry in (owner.record["session"], *owner.record["workers"])}
        return "unrelated" if recorded <= owner.initial_scopes.keys() or all(
            group in owner.other_director_scopes for group in process.cgroups
            if "app-agents.slice" in Path(group).parts) else "unknown"
    if process.pid not in owner.environment_unread:
        return "unrelated"
    # A child can create a transient scope with systemd-run. Its membership
    # alone cannot exclude ownership unless its start proves it predates us.
    if any(part.endswith(".scope") and part != "init.scope"
           for group in process.cgroups for part in Path(group).parts):
        return "unrelated" if _predates_owners(owner, process.started) else "unknown"
    if all(any(part in {"system.slice", "init.scope", "session.slice", "background.slice"}
                   or part.endswith(".service") and not part.startswith("user@") for part in Path(group).parts)
           for group in process.cgroups):
        return "unrelated"
    return "unknown"


def _owner_observation(owner: BoundedOwner, observation: ProcessObservation) -> ProcessObservation:
    """Only uncertainty capable of hiding this owner's targets blocks its proof."""
    session = owner.record["session"]
    runs = set(_run_directories(session))
    units = owner.units.copy()
    related = {pid for pid, start in owner.targets
               if pid in observation.processes and observation.processes[pid].started == start}
    groups = {group for target in owner.targets.values()
              for group in target.process.observed_groups | {target.process.process_session} if group}
    environment_unread = {int(match[1]) for error in observation.errors
                          if (match := re.match(r"pid (\d+): environment unread", error)) is not None}
    cgroup_unread = {int(match[1]) for error in observation.errors
                     if (match := re.match(r"pid (\d+): cgroup unread", error)) is not None}
    markers = {f"CLAUDE_CODE_SESSION_ID={session['session_id']}",
               *(f"{key}={run}" for run in runs for key in ("UNIT_RUN_DIR", "UNIT_SESSION_DIR"))}
    ambiguous: set[int] = set()
    missing_parents = {pid for pid, _start in owner.targets if pid not in observation.processes
                       and pid not in owner.replaced_parents}
    extra_errors: list[str] = []
    for pid, process in observation.processes.items():
        uncertain_markers = (pid in observation.uncertain and any(
            re.search(r"(?:^|\s)" + re.escape(marker) + r"(?=\s|$)",
                      process.unreconciled_ownership_text) is not None for marker in markers))
        if (process.session_id == session["session_id"] or process.run_dir in runs
                or uncertain_markers or process.build_unit and process.build_unit in units):
            related.add(pid)
        if pid not in related and (process.observed_parents | {process.parent}) & missing_parents:
            ambiguous.add(pid)
            extra_errors.append(f"pid {pid}: parent identity not observed; ownership unknown")
    while True:
        units.update(process.build_unit for pid, process in observation.processes.items()
                     if pid in related and process.build_unit)
        groups.update(group for pid, process in observation.processes.items() if pid in related
                      for group in process.observed_groups | {process.process_session} if group)
        expanded = related | {pid for pid, process in observation.processes.items()
            if (process.observed_parents | {process.parent}) & related
            or (process.observed_groups | {process.process_session}) & groups
            or process.build_unit and process.build_unit in units}
        if expanded == related:
            break
        related = expanded
    # No cgroups on Darwin: any adopter can be a subreaper. Only a readable
    # earlier start excludes an unreadable candidate without positive evidence.
    for pid, process in observation.processes.items():
        if pid in related or (pid, process.started) in owner.permanent_keeps:
            continue
        unknown = (pid in environment_unread and not _predates_owners(owner, process.started)
                   if sys.platform == "darwin" else
                   _cgroup_relation(owner, process) == "unknown")
        if unknown:
            ambiguous.add(pid)
            extra_errors.append(f"pid {pid}: ownership unknown")
    def relevant(error: str) -> bool:
        matched = re.match(r"pid (\d+):", error)
        if matched is None:
            return True
        pid = int(matched[1])
        process = observation.processes.get(pid)
        if process is None:
            return True
        if (pid, process.started) in owner.permanent_keeps:
            return False
        if pid not in related and _cgroup_relation(owner, process) == "unrelated":
            return False
        return pid in related or pid in ambiguous or pid in cgroup_unread or process.ownership_unreadable
    return ProcessObservation(observation.processes,
        tuple(error for error in observation.errors if relevant(error)) + tuple(extra_errors),
        observation.uncertain, observation.terminated, observation.observed_identities)


def stop_succeeded(owner: BoundedOwner, observation: ProcessObservation, *, request_current: bool,
                   generation: int, begun: float, pending: list[tuple[int, str]],
                   errors: list[str], now: float, deadline: StopDeadline) -> bool:
    """The sole completion rule, shared by persistence, output and the caller."""
    return (request_current and owner.pause_observed and owner.pause_done.is_set()
        and not owner.pause_issues and owner.claims_released and not observation.errors and not errors and not pending
        and generation >= owner.required_observation
        and begun >= max(owner.last_signal_at, owner.pause_finished_at) and now <= deadline.expires)



def _descendants(roots: set[int], observed: dict[int, ShutdownProcess]) -> set[int]:
    owned = roots.copy()
    while True:
        children = {pid for pid, process in observed.items() if (process.observed_parents | {process.parent}) & owned}
        expanded = owned | children
        if expanded == owned:
            return owned
        owned = expanded


def _recorded_builds(session: Session, observed: dict[int, ShutdownProcess], report: Callable[[str], None] = _ignore_read_error) -> dict[int, str]:
    builds: dict[int, str] = {}
    for run_dir in _run_directories(session):
        try:
            paths = list((Path(run_dir) / "mesh_runs").iterdir())
        except FileNotFoundError:
            paths = []
        except OSError as error:
            report(f"ownership directory {run_dir}/mesh_runs: {error}")
            paths = []
        for path in paths:
            if path.suffix != ".json":
                continue
            raw = _json_object(path, report)
            pid = raw.get("pid")
            if raw.get("status") != "running" or not isinstance(pid, int) or pid not in observed:
                continue
            arguments = observed[pid].arguments
            # mesh_runs records predate process-start fields: verify the exact supervisor argv.
            if ("supervise" in arguments and any(word.endswith("mesh_runs.py") for word in arguments)
                    and str(path) in arguments):
                builds[pid] = str(raw.get("label") or path.stem)
    return builds


def _owned_processes(saved: SessionShutdown, observed: dict[int, ShutdownProcess], report: Callable[[str], None] = _ignore_read_error) -> BoundedOwner:
    owner = saved["session"]
    runs = set(_run_directories(owner))
    owner_process = observed.get(owner["pid"])
    roots: set[int] = {owner["pid"]} if owner_process is not None and owner_process.started == owner["proc_start"] else set()
    roots.update(worker["pid"] for worker in saved["workers"]
                 if worker["pid"] in observed and observed[worker["pid"]].started == worker["proc_start"])
    roots.update(pid for pid in _mesh_server_roots(owner, observed, report))
    for directory in runs:
        for value in _json_object(Path(directory) / "mesh_roster.json", report).values():
            if not isinstance(value, dict):
                continue
            launcher = cast(dict[str, object], value).get("launcher_pid")
            if isinstance(launcher, int) and launcher in observed:
                process = observed[launcher]
                if process.run_dir == directory or directory in process.arguments:
                    roots.add(launcher)
    builds = _recorded_builds(owner, observed, report)
    roots.update(builds)
    # Detached supervisors own a separate POSIX session, including already-reparented children.
    roots.update(pid for pid, process in observed.items() if process.process_session in builds
                 and observed[process.process_session].process_session == process.process_session)
    # Detached verify calls retain the director/run environment, even after reparenting.
    for pid, process in observed.items():
        named_run = any(word in {"--session-dir", "--director-dir"} and following in runs
                        for word, following in zip(process.arguments, process.arguments[1:]))
        if process.session_id == owner["session_id"] or (process.run_dir and process.run_dir in runs) or named_run:
            roots.add(pid)
            if any(Path(word).name in {"verify.sh", "merge_gate.py"} for word in process.arguments):
                builds[pid] = " ".join(process.arguments)
    owned = _descendants(roots, observed)
    units = {observed[pid].build_unit for pid in owned if pid in observed and observed[pid].build_unit}
    # A scope may have reparented its members to the user manager.
    owned.update(pid for pid, process in observed.items() if process.build_unit in units)
    for pid in owned:
        if pid in observed and observed[pid].build_unit:
            _ = builds.setdefault(pid, observed[pid].build_unit)
    target = BoundedOwner(saved, {pid: observed[pid] for pid in owned if pid in observed}, builds, units)
    return target


def _mesh_server_roots(session: Session, observed: dict[int, ShutdownProcess], report: Callable[[str], None] = _ignore_read_error) -> set[int]:
    roots: set[int] = set()
    for directory in _run_directories(session):
        for pid in _mesh_server_pids(directory, report):
            process = observed.get(pid)
            if process is not None and "app-server" in process.arguments:
                roots.add(pid)
    return roots


def keep_infrastructure(session: Session) -> bool:
    """Recorded infrastructure directors remain running during machine shutdown."""
    return session["name"].casefold() in {"ups", "postmaster"}


def retire_kept_request(saved: SessionShutdown) -> None:
    """A kept, live director has no outage for restart to restore."""
    if saved["progress"]["kind"] == "restarting":
        return  # Its restore still owns timer restoration and confirmation.
    retired = False
    def kept(current: SessionShutdown) -> None:
        nonlocal retired
        if current["progress"]["kind"] == "restarting":
            return
        if current["request_id"] != saved["request_id"]:
            raise StopClaimLost()
        current["progress"] = {"kind": "restarted", "at": _timestamp(now_utc)}
        retired = True
    _ = update_session(saved["login"], saved["session"]["session_id"], kept)
    if retired:
        archive_session(saved["login"], saved["session"]["session_id"])


def _protected_processes(saved: SessionShutdown, observed: dict[int, ShutdownProcess], report: Callable[[str], None] = _ignore_read_error) -> set[int]:
    """Keep worker servers for protocol pause, and retain recorded infrastructure identities."""
    protected: set[int] = set()
    session = saved["session"]
    process = observed.get(session["pid"])
    if keep_infrastructure(session) and process is not None and process.started == session["proc_start"]:
        protected.update(_descendants({session["pid"]}, observed))
    holds = Path(os.environ.get("BUILD_HOLD_DIR", str(Path.home() / ".local/state/build-hold")))
    for path in holds.glob("*"):
        values = _json_object(path, report).get("keep")
        if isinstance(values, list):
            for value in cast(list[object], values):
                if not isinstance(value, dict):
                    continue
                keep = cast(dict[str, object], value)
                pid = keep.get("pid")
                if isinstance(pid, int) and pid in observed and keep.get("start_time") == observed[pid].started:
                    protected.update(_descendants({pid}, observed))
    return protected


def _force_stop_note(session: Session, grace_seconds: float) -> str:
    try:
        cwd = Path(session["cwd"])
        gitdir = next((parent / ".git" for parent in (cwd, *cwd.parents)
                       if (parent / ".git").exists()), cwd / ".git")
        if gitdir.is_file():
            text = gitdir.read_text().strip()
            if text.startswith("gitdir: "):
                gitdir = (gitdir.parent / text.removeprefix("gitdir: ")).resolve()
        found = [str(path) for name in ("MERGE_HEAD", "index.lock") if (path := gitdir / name).exists()]
        detail = "found: " + ", ".join(found) if found else "found no MERGE_HEAD or index.lock"
    except OSError as error:
        detail = f"could not inspect git files: {error}"
    return (f"stopped by force at {grace_seconds:g} s, no ready line; {detail}; "
            "check git status, MERGE_HEAD, .git/index.lock and workers before continuing; never remove index.lock automatically")


def _release_build_claims(owner: BoundedOwner, identity: Callable[[int], str | None], timeout: float = 30) -> None:
    """Remove only dead holders' exact records; remote jobs remain visible until confirmed ended."""
    session = owner.record["session"]
    roots = [Path(os.environ.get("BUILDLOG_DIR", str(Path.home() / ".local/state/buildlog"))) / "admission",
             Path(os.environ.get("VERIFY_STEPS_DIR", str(Path.home() / ".local/state/verify-steps"))),
             Path(os.environ.get("MAC_TEST_STATE_DIR", str(Path.home() / ".local/state/mac-test")))]
    for directory in roots:
        for path in [*directory.glob("*.json"), *directory.glob("*.waiting"),
                     *directory.glob("*.reservation"), *directory.glob("claims/*.json")]:
            if path == roots[-1] / "block.json":
                continue
            try:
                with path.open("r+", encoding="utf-8") as handle:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    raw = cast(object, json.load(handle))
                    if not isinstance(raw, dict):
                        continue
                    fields = cast(dict[str, object], raw)
                    pid = fields.get("pid")
                    process = owner.processes.get(pid) if isinstance(pid, int) else None
                    owned = (process is not None and pid not in owner.protected and identity(process.pid) != process.started
                             and fields.get("proc_start", fields.get("start_time", fields.get("start"))) == process.started)
                    if fields.get("holder_session") == session["session_id"]:
                        owned = True
                    if not owned:
                        continue
                    if fields.get("remote_host"):
                        print(f"left running build  {fields['remote_host']} ({session['name']}): remote job not stopped", flush=True)
                        continue
                    if fields.get("state") == "on_mac":
                        print(f"left running build  Mac offload ({session['name']}): remote completion unconfirmed", flush=True)
                    path.unlink(missing_ok=True)
                    if path.suffix in {".reservation", ".waiting"}:
                        path.with_suffix(".max").unlink(missing_ok=True)
            except (OSError, ValueError):
                continue
    # Build holds use the immutable session id as their key on both targets.
    holds = Path(os.environ.get("BUILD_HOLD_DIR", str(Path.home() / ".local/state/build-hold")))
    key = session["session_id"]
    if key and Path(key).name == key:
        (holds / key).unlink(missing_ok=True)
    block = roots[-1] / "block.json"
    fields = _json_object(block)
    if fields.get("holder_session") == key or fields.get("holder") == key or fields.get("session") == key:
        holder = fields.get("holder")
        if isinstance(holder, str) and holder:
            # Unblock also restores the CI switch saved by the Mac hold; unlinking loses that intent.
            try:
                result = subprocess.run([sys.executable, str(SCRIPTS / "mac_test/mac_test.py"),
                    "unblock", "--holder", holder], capture_output=True, text=True, check=False, timeout=max(0.001, timeout))
                if result.returncode:
                    print(f"shutdown: {session['name']}: Mac hold retained: {result.stderr.strip() or result.stdout.strip()}",
                          file=sys.stderr, flush=True)
            except (OSError, subprocess.TimeoutExpired) as error:
                print(f"shutdown: {session['name']}: Mac hold retained: {error}", file=sys.stderr, flush=True)


def defer_requester(saved: SessionShutdown) -> None:
    """Finish the caller last, from a detached helper after the command has exited."""
    command = [sys.executable, str(Path(__file__).resolve()), "finish-requester",
               saved["login"], saved["session"]["session_id"], str(os.getpid()), _requester_identity(os.getpid())]
    _ = subprocess.Popen(command, start_new_session=True, stdin=subprocess.DEVNULL)
    print(f"{saved['session']['name']}: requester stops last after this command exits", flush=True)


def _requester_identity(pid: int) -> str:
    state = shutdown_identity(pid)
    return state.started if isinstance(state, ProcessLive) else ""


def _bounded_mesh(arguments: list[str], timeout: float) -> int | None:
    try:
        return subprocess.run([sys.executable, str(CODEX_MESH), *arguments],
            capture_output=True, text=True, check=False, timeout=timeout).returncode
    except (OSError, subprocess.SubprocessError):
        return None


def _recorded_directors() -> dict[tuple[int, str], str]:
    """Candidate identities only; the current table must prove each live."""
    from record import all_session_records
    saved = all_session_records()
    directors = {(item["session"]["pid"], item["session"]["proc_start"]): item["session"]["session_id"]
                 for item in saved}
    workers = {worker["session_id"] for item in saved for worker in item["workers"]}
    for observed in sessions.read_registry().entries:
        entry = observed.record
        started = entry.get("procStart", "")
        if observed.details.get("kind") != "bg" and entry["sessionId"] not in workers and started:
            directors[(entry["pid"], started)] = entry["sessionId"]
    return directors


def _no_exit_request(_saved: SessionShutdown) -> None:
    pass


def bounded_stop(
    records: list[SessionShutdown], *, started: float,
    grace_seconds: float = SHUTDOWN_GRACE_SECONDS,
    machine_wide: bool = False,
    keep: tuple[Session, ...] = (),
    request_exit: Callable[[SessionShutdown], None] = _no_exit_request,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Sleeper = time.sleep,
    clock: Clock = now_utc,
    observe: Callable[[float], ProcessObservation] = shutdown_processes,
    identity: Callable[[int, float], ProcessIdentity] = shutdown_identity,
    kill: KillProcess = os.kill,
    codex_mesh: Callable[[list[str], float], int | None] = _bounded_mesh,
) -> list[SessionShutdown]:
    """Prove completion from fresh evidence; no stop path can bypass the proof."""
    deadline = StopDeadline(started + grace_seconds + TERM_GRACE_SECONDS + 1, monotonic)
    owners: list[BoundedOwner] = []
    results: list[SessionShutdown] = []
    generation = 0
    server_identities: frozenset[tuple[int, str]] = frozenset()
    server_checked = False
    latest_observation = ProcessObservation({}, ("no process table yet",))

    def line(text: str) -> None:
        print(f"{clock().astimezone(user_zone()):%H:%M:%S}  {text}", flush=True)

    def probe(pid: int) -> ProcessIdentity:
        if sys.platform == "darwin":
            process = latest_observation.processes.get(pid)
            if pid in latest_observation.uncertain:
                return ProcessUnknown(f"pid {pid}: unreconciled process tables")
            if process is not None:
                return ProcessLive(process.started)
            return ProcessUnknown(f"pid {pid}: incomplete process table") if any(
                not error.startswith("pid ") or error.startswith(f"pid {pid}:")
                for error in latest_observation.errors) else ProcessGone()
        return deadline.call(lambda budget: identity(pid, budget),
            lambda error: ProcessUnknown(f"pid {pid}: {error}"), verification=True)

    for saved in records:
        session = saved["session"]
        if keep_infrastructure(session):
            retire_kept_request(saved)
            line(f"kept  {session['name']} (protected infrastructure)")
            continue
        owner = BoundedOwner(saved, {}, {}, set(), machine_wide=machine_wide)
        for recorded in (session, *saved["workers"]):
            owner.targets[(recorded["pid"], recorded["proc_start"])] = StopTarget(
                ShutdownProcess(recorded["pid"], 0, recorded["proc_start"]))
        host = session["host"]
        if host["kind"] == "terminal" or host["kind"] == "ghostty":
            window_host = host
            supervisor = deadline.call(lambda _budget: live_supervisor(window_host), lambda _error: None)
            if supervisor is not None and supervisor["kind"] == "supervisor":
                relaunch.write_hold(supervisor["pid"])
                owner.permanent_keeps.add((supervisor["pid"], supervisor["proc_start"]))
        def seats_unread(error: str) -> list[PausedSeat]:
            owner.stop_errors.append(f"workers unread: {error}")
            return []
        seats = deadline.call(lambda _budget: _capture_codex_seats(session), seats_unread)
        def retain(current: SessionShutdown) -> None:
            if not _same_stop_ownership(saved, current):
                raise StopClaimLost()
            current["codex_seats"] = seats
        try:
            owner.record = update_session(saved["login"], session["session_id"], retain)
        except (NoLiveRecord, StopClaimLost):
            pass  # The single decision point below reports a lost request.
        if not _run_directories(session):
            owner.pause_started = True
            owner.pause_done.set()
            owner.pause_finished_at = started
        owners.append(owner)

    directors = deadline.call(lambda _budget: _recorded_directors(), lambda _error: {})

    def pause(owner: BoundedOwner) -> None:
        issues: list[SessionStopIssue] = []
        for run in _run_directories(owner.record["session"]):
            status = deadline.call(lambda budget: codex_mesh(["stop", "--pause", "--session-dir", run], budget),
                                   lambda _error: None)
            if status != 0:
                issues.append({"kind": "codex server left running", "at": _timestamp(clock),
                               "run_dir": run, "cause": "stop not confirmed"})
        owner.pause_issues = issues
        owner.pause_finished_at = monotonic()
        owner.pause_done.set()

    while any(not isinstance(owner.state, StopComplete) for owner in owners):
        # Acknowledging a pause before starting this capture makes it mandatory
        # to observe its newly born children before signalling worker ancestors.
        for owner in owners:
            if owner.pause_done.is_set() and not owner.pause_observed:
                owner.pause_observed = True
                owner.required_observation = generation + 1
        begun = monotonic()
        observation = deadline.call(observe, lambda error: ProcessObservation({}, (error,)), verification=True)
        if observation.errors and any("deadline" in error for error in observation.errors):
            # Retain the last discovered identities as unresolved evidence,
            # never as an empty successful final capture. The deadline error
            # makes this observation incomplete for the single predicate.
            observation = ProcessObservation(latest_observation.processes | observation.processes,
                latest_observation.errors + observation.errors,
                latest_observation.uncertain | observation.uncertain,
                latest_observation.terminated | observation.terminated,
                latest_observation.observed_identities | observation.observed_identities)
        generation += 1
        latest_observation = observation
        fresh = observation.processes
        if not server_checked and any(Path(group).name == herdr.server_unit(herdr.shared_session())
                                     for process in fresh.values() for group in process.cgroups):
            server_checked = True
            server_identities = deadline.call(inventory.shutdown_server_identities, lambda _error: frozenset())
        # Retain every recorded identity's scope from the first observation,
        # before asking any director to exit. Missing evidence stays missing.
        for owner in owners:
            if generation == 1:
                for entry in (owner.record["session"], *owner.record["workers"]):
                    process = fresh.get(entry["pid"])
                    if (process is not None and process.started == entry["proc_start"]
                            and process.pid not in observation.uncertain and process.cgroups):
                        owner.initial_scopes[(process.pid, process.started)] = process.cgroups
                        owner.cgroups.update(process.cgroups)
            owner.other_director_scopes = {group for (pid, start), session_id in directors.items()
                if session_id != owner.record["session"]["session_id"] and pid in fresh
                and fresh[pid].started == start and pid not in observation.uncertain
                for group in fresh[pid].cgroups}
        if generation == 1:
            for owner in owners:
                request_exit(owner.record)
        for owner in owners:
            if isinstance(owner.state, StopComplete):
                continue
            saved = owner.record
            session = saved["session"]
            key = (session["pid"], session["proc_start"])
            current = find_session(saved["login"], session["session_id"])
            request_current = current is not None and _same_stop_ownership(saved, current)
            identities = observation.observed_identities | observation.terminated | frozenset(
                (pid, process.started) for pid, process in fresh.items())
            owner.replaced_parents.update(pid for pid, start in identities
                if any(old_pid == pid and old_start != start for old_pid, old_start in owner.targets))
            owner.environment_unread = {int(match[1]) for error in observation.errors
                if (match := re.match(r"pid (\d+): environment unread", error)) is not None}
            owner.cgroup_unread = {int(match[1]) for error in observation.errors
                if (match := re.match(r"pid (\d+): cgroup unread", error)) is not None}
            ownership_errors: list[str] = []
            def ownership_unread(error: str) -> BoundedOwner:
                ownership_errors.extend(owner.ownership_errors)
                ownership_errors.append(f"ownership observation: {error}")
                return BoundedOwner(saved, {}, {}, set())
            discovered = deadline.call(lambda _budget: _owned_processes(saved,
                {pid: process for pid, process in fresh.items() if pid not in observation.uncertain}, ownership_errors.append),
                                       ownership_unread, verification=True)
            # Previously attributed identities are durable roots only while the
            # *same* process still exists. Reused PIDs never inherit ownership.
            roots = {pid for (pid, start), target in owner.targets.items()
                     if not target.retired and pid in fresh and pid not in observation.uncertain and fresh[pid].started == start}
            roots.update(discovered.processes)
            roots = _descendants(roots, fresh)
            roots.update(pid for pid, process in fresh.items() if process.build_unit and process.build_unit in owner.units)
            owner.cgroups.update(group for pid in roots for group in fresh[pid].cgroups)
            roots.update(pid for pid, process in fresh.items() if pid not in observation.uncertain
                and _cgroup_relation(owner, process) == "owned")
            roots = _descendants(roots, fresh)
            for pid in roots:
                process = fresh[pid]
                identity_key = (pid, process.started)
                if identity_key not in owner.targets:
                    owner.targets[identity_key] = StopTarget(process)
                else:
                    owner.targets[identity_key].process = process
                owner.processes[pid] = process
            owner.units.update(discovered.units)
            owner.builds.update(discovered.builds)
            for pid in roots:
                if any(Path(word).name in {"verify.sh", "merge_gate.py"} for word in fresh[pid].arguments):
                    owner.builds[pid] = " ".join(fresh[pid].arguments)
            def keeps_unread(error: str) -> set[int]:
                ownership_errors.append(f"keep observation: {error}")
                return set()
            kept_roots = {session["pid"] for session in keep if session["pid"] in fresh
                          and fresh[session["pid"]].started == session["proc_start"]}
            permanent = deadline.call(lambda _budget: _protected_processes(saved, fresh, ownership_errors.append),
                                      keeps_unread, verification=True) | _descendants({os.getpid()} | kept_roots, fresh)
            owner.permanent_keeps.update((pid, fresh[pid].started) for pid in permanent if pid in fresh)
            owner.permanent_keeps.update(server_identities)
            worker_roots = deadline.call(lambda _budget: _mesh_server_roots(session, fresh, ownership_errors.append),
                                         keeps_unread, verification=True)
            owner.ownership_errors = list(dict.fromkeys(ownership_errors))
            scoped = _owner_observation(owner, observation)
            errors = [*scoped.errors, *owner.ownership_errors]
            worker_roots.update(pid for pid, start in owner.worker_targets if pid in fresh and fresh[pid].started == start)
            owner.worker_targets.update((pid, fresh[pid].started) for pid in _descendants(worker_roots, fresh))
            pending: list[tuple[int, str]] = []
            states: dict[tuple[int, str], ProcessIdentity] = {}
            for target_key, target in owner.targets.items():
                pid, start = target_key
                shown = fresh.get(pid)
                if target.retired:
                    state: ProcessIdentity = ProcessGone()
                elif target_key in observation.terminated:
                    state = ProcessGone()
                elif shown is not None and (pid not in observation.uncertain or shown.started != start):
                    state = ProcessLive(shown.started)
                elif pid in observation.uncertain:
                    state = ProcessUnknown(f"pid {pid}: unreconciled process tables")
                elif not scoped.errors:
                    state = ProcessGone()
                else:
                    state = probe(pid)
                states[target_key] = state
                if isinstance(state, ProcessGone) or isinstance(state, ProcessLive) and state.started != start:
                    target.retired = True
                if isinstance(state, ProcessUnknown):
                    errors.append(state.reason)
                if target_key not in owner.permanent_keeps and (isinstance(state, ProcessUnknown)
                        or isinstance(state, ProcessLive) and state.started == start):
                    pending.append(target_key)
            director_state = states[key]
            director_gone = isinstance(director_state, ProcessGone) or (
                isinstance(director_state, ProcessLive) and director_state.started != key[1])
            if request_current and isinstance(owner.state, StopRequested) and (director_gone
                    or current is not None and current["progress"]["kind"] == "ready"
                    or monotonic() >= started + grace_seconds):
                owner.forced = not director_gone and monotonic() >= started + grace_seconds
                def claim(changing: SessionShutdown) -> None:
                    if not _same_stop_ownership(saved, changing):
                        raise StopClaimLost()
                    if owner.forced and changing["where"]["kind"] == "not said":
                        changing["force_stop_note"] = _force_stop_note(session, grace_seconds)
                    changing["progress"] = {"kind": "stopping", "at": _timestamp(clock)}
                try:
                    owner.record = update_session(saved["login"], session["session_id"], claim)
                    owner.state = StopSignalled(monotonic(), owner.forced)
                except (NoLiveRecord, StopClaimLost):
                    request_current = False
            if director_gone and not owner.pause_started:
                owner.pause_started = True
                if _run_directories(session):
                    threading.Thread(target=pause, args=(owner,), daemon=True).start()
                else:
                    owner.pause_finished_at = monotonic()
                    owner.pause_done.set()
            temporary: set[tuple[int, str]] = owner.worker_targets if not owner.pause_observed or owner.pause_issues else set()
            owner.protected = {pid for pid, start in owner.permanent_keeps | temporary
                               if pid in fresh and fresh[pid].started == start}
            # Each captured identity has its own TERM and KILL clock. Worker's
            # temporary protection ends only after a post-pause capture.
            order = sorted(pending, key=lambda item: len(_descendants({item[0]}, fresh)))
            for target_key in order:
                target = owner.targets[target_key]
                pid, start = target_key
                if (not request_current or ownership_errors or target_key in temporary or monotonic() > deadline.expires - 1
                        or target_key == key and isinstance(owner.state, StopRequested)):
                    continue
                # Unclassified children wait with the director; detached builds
                # are stopped immediately and all targets after the stop claim.
                if isinstance(owner.state, StopRequested) and not any(pid in _descendants({root}, owner.processes) for root in owner.builds):
                    continue
                state = probe(pid)
                if isinstance(state, ProcessUnknown):
                    errors.append(state.reason)
                    continue
                if not isinstance(state, ProcessLive) or state.started != start or monotonic() > deadline.expires - 1:
                    continue
                signum = signal.SIGTERM if target.term_at < 0 else signal.SIGKILL
                if target.term_at >= 0 and (target.kill_sent or monotonic() < target.term_at + TERM_GRACE_SECONDS):
                    continue
                if signum == signal.SIGTERM:
                    target.term_at = monotonic()
                else:
                    target.kill_sent = True
                owner.last_signal_at = monotonic()
                owner.required_observation = generation + 1
                try:
                    kill(pid, signum)
                except ProcessLookupError:
                    pass
                except OSError as error:
                    errors.append(f"pid {pid}: signal failed: {error}")
            for pid, label in owner.builds.items():
                if pid in owner.reported_builds or any(
                        kept_pid == pid for kept_pid, _start in owner.permanent_keeps):
                    continue
                members = {(target_pid, start) for target_pid, start in owner.targets if target_pid in _descendants({pid}, owner.processes)}
                if not members.intersection(pending) and not scoped.errors:
                    line(f"stopped build  {label} ({session['name']})")
                    owner.reported_builds.add(pid)
            if (not owner.claims_released and not pending and not errors and request_current
                    and owner.pause_observed and owner.pause_done.is_set() and not owner.pause_issues):
                def cleanup(budget: float) -> str:
                    def known_identity(pid: int) -> str | None:
                        state = probe(pid)
                        if isinstance(state, ProcessUnknown):
                            return owner.processes[pid].started if pid in owner.processes else "unknown"
                        return state.started if isinstance(state, ProcessLive) else None
                    _release_build_claims(owner, known_identity, budget)
                    return ""
                cleanup_error = deadline.call(cleanup, lambda error: error, verification=True)
                owner.claims_released = True
                if cleanup_error:
                    owner.stop_errors.append(f"build claim cleanup: {cleanup_error}")
            errors.extend(owner.stop_errors)
            # ONE decision point: every persisted/printed/returned success uses
            # this predicate, including the final deadline observation.
            success = stop_succeeded(owner, scoped, request_current=request_current,
                generation=generation, begun=begun, pending=pending, errors=errors,
                now=monotonic(), deadline=deadline)
            if success or not request_current or monotonic() >= deadline.expires:
                reasons = errors.copy()
                if not request_current:
                    reasons.append("shutdown record disappeared or was replaced")
                if pending:
                    reasons.append("unresolved pids: " + ", ".join(f"{pid}/{start}" for pid, start in pending))
                if not owner.pause_observed or not owner.pause_done.is_set() or owner.pause_issues:
                    reasons.append("worker pause not confirmed successful")
                if generation < owner.required_observation:
                    reasons.append("no complete observation after latest signal/pause")
                reason = "; ".join(dict.fromkeys(reasons)) or "shutdown deadline exhausted"
                def done(changing: SessionShutdown) -> None:
                    if not _same_stop_ownership(saved, changing):
                        raise StopClaimLost()
                    changing["progress"] = ({"kind": "stopped", "at": _timestamp(clock)} if success else
                        {"kind": "stop failed", "at": _timestamp(clock), "reason": reason})
                    changing["stop_issues"] = owner.pause_issues
                try:
                    result = update_session(saved["login"], session["session_id"], done)
                except (NoLiveRecord, StopClaimLost):
                    rejection = "shutdown ownership changed before final write; newer record left unchanged"
                    reason = rejection if success else f"{reason}; {rejection}"
                    result = saved.copy()
                    result["progress"] = {"kind": "stop failed", "at": _timestamp(clock), "reason": reason}
                    success = False
                results.append(result)
                if success:
                    why = f"forced at {grace_seconds:g} s" if owner.forced else f"by itself, {monotonic() - started:.1f} s"
                    line(f"stopped  {session['name']} ({why})")
                else:
                    line(f"failed  {session['name']}: {reason}")
                owner.state = StopComplete()
        if any(not isinstance(owner.state, StopComplete) for owner in owners):
            pausing = next((owner for owner in owners if owner.pause_started and not owner.pause_done.is_set()), None)
            if pausing is not None:
                _ = pausing.pause_done.wait(timeout=min(0.01, deadline.remaining(verification=True)))
            sleep(min(0.1, max(0, deadline.expires - monotonic())))
    print(f"all {sum(saved['progress']['kind'] == 'stopped' for saved in results)} stopped", flush=True)
    return results


def main(arguments: list[str]) -> int:
    if len(arguments) != 5 or arguments[0] != "finish-requester":
        return 2
    caller_pid = int(arguments[3])
    started = time.monotonic()
    deadline = StopDeadline(started + SHUTDOWN_GRACE_SECONDS + TERM_GRACE_SECONDS + 1, time.monotonic)
    command_exited = False
    while deadline.remaining() > 0:
        state = deadline.call(lambda budget: shutdown_identity(caller_pid, budget),
                              lambda error: ProcessUnknown(error))
        if isinstance(state, ProcessGone) or (isinstance(state, ProcessLive) and arguments[4]
                                             and state.started != arguments[4]):
            command_exited = True
            break
        time.sleep(min(0.05, deadline.remaining()))
    if not command_exited:
        print("shutdown: requester command liveness unresolved at deadline; director left running", flush=True)
        return 1
    pending = find_session(arguments[1], arguments[2])
    if pending is not None:
        def request_exit(saved: SessionShutdown) -> None:
            threading.Thread(target=settle.request_pause, args=(saved,), daemon=True).start()
        _ = bounded_stop([pending], started=started, request_exit=request_exit)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
