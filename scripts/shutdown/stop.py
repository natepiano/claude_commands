#!/usr/bin/env python3
"""Stop one machine's sessions after shutdown settlement has completed."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import copy
import time
from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Protocol, TypedDict, cast

SCRIPTS = Path(__file__).resolve().parents[1]
for dependency in ("message", "production", "herdr", "lib"):
    sys.path.insert(0, str(SCRIPTS / dependency))

import sessions
import herdr
import relaunch
from inventory import Inventory, Session, terminal_kind, live_supervisor
from record import (
    CLAIM_STALE_SECONDS,
    NoLiveRecord,
    NotReadyToStop,
    ReadinessCheck,
    SessionProgress,
    SessionShutdown,
    SessionStopIssue,
    find_session,
    update_session,
)

class ShutdownReadiness(Protocol):
    def blocker(self, session: Session) -> ReadinessCheck: ...


settle = cast(ShutdownReadiness, cast(object, import_module("settle")))



CODEX_MESH = SCRIPTS / "agents" / "codex_mesh.py"
STOP_WAIT = timedelta(seconds=10)
STOP_POLL_SECONDS = 0.1

KillProcess = Callable[[int, int], None]
LivenessCheck = Callable[[int], bool]
SessionRecordPresence = Callable[[int], bool]
FreshInventory = Callable[[str, frozenset[str]], Inventory]
Clock = Callable[[], datetime]
CommandRunner = Callable[[list[str]], int | None]
Sleeper = Callable[[float], None]


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
    seats: list[Session]


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


def run_tmux(arguments: list[str]) -> int | None:
    return _run([os.environ.get("SHUTDOWN_TMUX", "tmux"), *arguments])


def run_systemctl(arguments: list[str]) -> int | None:
    return _run(["systemctl", *arguments])


def run_launchctl(arguments: list[str]) -> int | None:
    return _run(["launchctl", *arguments])


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
            "seats": [
                item for item in report["sessions"]
                if item["kind"] == "seat"
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
        current["stop_issues"] = issues

    return update_session(claim.login, claim.session_id, change)


def _replace_stop_issues(
    claim: StopClaim, issues: list[SessionStopIssue]
) -> SessionShutdown:
    def replace(current: SessionShutdown) -> None:
        claim.require(current)
        current["stop_issues"] = issues

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


def _json_object(path: Path) -> dict[str, object]:
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, ValueError, TypeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return cast(dict[str, object], value)


def _mesh_server_pids(run_dir: str) -> set[int]:
    folder = Path(run_dir)
    records: list[dict[str, object]] = [_json_object(folder / "mesh_server.json")]
    retired = _json_object(folder / "mesh_retired.json").get("servers")
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


def _end_busy_codex_workers(
    session: Session, codex_mesh: CommandRunner, claim: StopClaim
) -> None:
    for server in session["codex_servers"]:
        for worker in server["busy_workers"]:
            claim.check()
            _ = codex_mesh(
                ["end", "--session-dir", server["run_dir"], "--to", worker]
            )


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
        status = codex_mesh(["stop", "--session-dir", run_dir])
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


def _stop_unit_tmux(
    session: Session, tmux: CommandRunner, clock: Clock, claim: StopClaim
) -> list[SessionStopIssue]:
    if session["kind"] != "unit" or session["host"]["session_host"] != "tmux":
        return []
    tmux_session = session["host"]["session_name"]
    target = f"={tmux_session}"
    issue: SessionStopIssue = {
        "kind": "unit session left running",
        "at": _timestamp(clock),
        "host": "tmux",
        "session_name": tmux_session,
        "cause": "stop not confirmed",
    }
    try:
        claim.check()
        killed = tmux(["kill-session", "-t", target])
        if killed is None:
            return [issue]
        claim.check()
        remaining = tmux(["has-session", "-t", target])
    except (OSError, subprocess.TimeoutExpired):
        return [issue]
    if remaining is None or remaining == 0:
        return [issue]
    return []


def _stop_unit_session(
    session: Session, tmux: CommandRunner, clock: Clock, claim: StopClaim
) -> list[SessionStopIssue]:
    if session["kind"] != "unit":
        return []
    if session["host"]["session_host"] == "tmux":
        return _stop_unit_tmux(session, tmux, clock, claim)
    name = session["host"]["session_name"]
    issue: SessionStopIssue = {
        "kind": "unit session left running",
        "at": _timestamp(clock),
        "host": "herdr",
        "session_name": name,
        "cause": "stop not confirmed",
    }
    claim.check()
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
    tmux: CommandRunner,
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
    issues.extend(_stop_unit_session(session, tmux, clock, claim))
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
    if session["kind"] == "unit":
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
    tmux: CommandRunner,
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
    issues.extend(_stop_codex_servers(
        session, codex_mesh=codex_mesh, is_alive=is_alive, clock=clock, claim=claim
    ))
    if any(issue["kind"] == "unit session left running" for issue in pending):
        issues.extend(_stop_unit_session(session, tmux, clock, claim))
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
    force_now: bool,
    kill: KillProcess,
    is_alive: LivenessCheck,
    session_record_exists: SessionRecordPresence,
    fresh_inventory: FreshInventory,
    clock: Clock,
    codex_mesh: CommandRunner,
    tmux: CommandRunner,
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
    if match["kind"] == "session account unreadable":
        _ = _set_stop_outcome(
            claim, "stop failed", clock,
            [{"kind": "account unreadable", "at": _timestamp(clock)}],
            "account unreadable",
        )
        return
    if match["kind"] == "session absent":
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
            tmux=tmux,
            clock=clock,
            claim=claim,
            retain_window=retain_window,
        )
        _ = _set_stop_outcome(
            claim, "already gone", clock, issues
        )
        return

    current = match["session"]
    if not force_now and prior["kind"] != "reaping" and (
        prior["kind"] not in {"ready", "held by usage limit", "stopping", "stop failed"}
        or current["status"] != "idle"
        or any(worker["status"] != "idle" for worker in match["seats"])
        or settle.blocker(current)["kind"] != "clear"
    ):
        issue = NotReadyToStop(
            kind="not ready to stop", at=_timestamp(clock),
            status=current["status"],
            progress=("held by usage limit" if prior["kind"] == "held by usage limit"
                      else "ready" if prior["kind"] == "ready" else "waiting"),
        )
        _ = _set_stop_outcome(
            claim, "stop failed", clock, [issue],
            "session or worker is not idle and ready",
        )
        return
    if force_now:
        _end_busy_codex_workers(current, codex_mesh, claim)
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
            tmux=tmux,
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
        if second["kind"] == "session account unreadable":
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
        tmux=tmux,
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
    kill: KillProcess = os.kill,
    is_alive: LivenessCheck = process_is_alive,
    session_record_exists: SessionRecordPresence = session_record_exists,
    fresh_inventory: FreshInventory = _fresh_inventory,
    clock: Clock = now_utc,
    codex_mesh: CommandRunner = run_codex_mesh,
    tmux: CommandRunner = run_tmux,
    sleep: Sleeper = time.sleep,
) -> SessionShutdown:
    """Claim and stop one owner, checking identity before each signal."""
    claimed = False
    prior: SessionProgress = record["progress"]

    def claim(current: SessionShutdown) -> None:
        nonlocal claimed, prior
        if current["request_id"] != record["request_id"]:
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
                codex_mesh=codex_mesh, tmux=tmux, clock=clock,
            )
        if prior["kind"] in {"stopped", "already gone", "process identity lost", "restart failed"}:
            def restore_progress(changing: SessionShutdown) -> None:
                owned.require(changing)
                changing["progress"] = prior

            return update_session(current["login"], current["session"]["session_id"], restore_progress)
        _stop_owner(
            owned, current, prior=prior,
            force_now=current["force"] == "now",
            kill=kill, is_alive=is_alive,
            session_record_exists=session_record_exists,
            fresh_inventory=fresh_inventory, clock=clock,
            codex_mesh=codex_mesh, tmux=tmux, sleep=sleep,
        )
    except StopClaimLost:
        pass

    finished = find_session(current["login"], current["session"]["session_id"])
    if finished is None:
        raise NoLiveRecord("session shutdown disappeared while stopping")
    return finished
