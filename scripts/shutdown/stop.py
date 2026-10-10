#!/usr/bin/env python3
"""Stop one machine's sessions after shutdown settlement has completed."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, TypedDict, cast

SCRIPTS = Path(__file__).resolve().parents[1]
for dependency in ("message", "production"):
    sys.path.insert(0, str(SCRIPTS / dependency))

import sessions
from inventory import Inventory, Session, UnattributedSession, terminal_kind
from record import (
    Conductor,
    OrchestrationStopIssue,
    NoLiveRecord,
    NotReadyToStop,
    SessionStopIssue,
    ShutdownRecord,
    ShutdownScope,
    ShutdownSessionEntry,
    StopTiming,
    find_live,
    parse_records,
    stop_lock,
    update,
)


CODEX_MESH = SCRIPTS / "agents" / "codex_mesh.py"
STOP_WAIT = timedelta(seconds=10)
STOP_POLL_SECONDS = 0.1

KillProcess = Callable[[int, int], None]
LivenessCheck = Callable[[int], bool]
SessionRecordPresence = Callable[[int], bool]
FreshInventory = Callable[[str, ShutdownScope], Inventory]
Clock = Callable[[], datetime]
CommandRunner = Callable[[list[str]], int | None]
Sleeper = Callable[[float], None]


class StopReport(TypedDict):
    record: ShutdownRecord
    unattributed: list[UnattributedSession]
    counts: dict[str, int]


class StopUnderway(TypedDict):
    kind: Literal["stop underway"]
    record: ShutdownRecord


class StopFinished(TypedDict):
    kind: Literal["stop finished"]
    record: ShutdownRecord


EnterStoppingOutcome = StopUnderway | StopFinished


class CancelClaimed(TypedDict):
    kind: Literal["cancel claimed"]
    record: ShutdownRecord


class CancelAlreadyStopping(TypedDict):
    kind: Literal["already stopping"]
    label: str


class CancelEnded(TypedDict):
    kind: Literal["ended"]


CancelClaim = CancelClaimed | CancelAlreadyStopping | CancelEnded


class MatchingSession(TypedDict):
    kind: Literal["matching session"]
    session: Session


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


def stop_conductor(
    conductor: Conductor,
    *,
    systemctl: CommandRunner = run_systemctl,
    launchctl: CommandRunner = run_launchctl,
) -> None:
    """Stop a detached conductor without letting service-manager noise escape."""
    if conductor["kind"] == "systemd":
        _ = systemctl(["--user", "stop", conductor["unit"]])
    elif conductor["kind"] == "launchd":
        _ = launchctl(["remove", conductor["label"]])


def claim_stop_as_conductor(login: str) -> bool:
    """Claim a settling record while preserving its requested stop timing."""
    claimed = False

    def claim(current: ShutdownRecord) -> None:
        nonlocal claimed
        if current["state"] == "settling":
            current["state"] = "stopping"
            claimed = True

    try:
        _ = update(login, claim)
    except NoLiveRecord:
        return False
    return claimed


def claim_stop_as_peer(login: str, timing: StopTiming) -> bool:
    """Claim a peer record, replaying it without undoing a now upgrade."""
    claimed = False

    def claim(current: ShutdownRecord) -> None:
        nonlocal claimed
        if current["state"] == "settling":
            current["state"] = "stopping"
            current["force"] = timing
            claimed = True
        elif current["state"] == "stopping":
            if timing == "now":
                current["force"] = "now"
            claimed = True

    try:
        _ = update(login, claim)
    except NoLiveRecord:
        return False
    return claimed


def claim_cancel(login: str) -> CancelClaim:
    """Atomically win cancellation, or report that stopping already won."""
    won = False
    stopping = False

    def claim(current: ShutdownRecord) -> None:
        nonlocal won, stopping
        if current["state"] == "settling":
            current["state"] = "cancelled"
            won = True
        elif current["state"] == "stopping":
            stopping = True

    try:
        current = update(login, claim)
    except NoLiveRecord:
        return {"kind": "ended"}
    if won:
        return {"kind": "cancel claimed", "record": current}
    if stopping:
        return {"kind": "already stopping", "label": current["label"]}
    return {"kind": "ended"}


def _enter_stopping(login: str) -> EnterStoppingOutcome:
    accepted = False
    finished = False

    def enter(current: ShutdownRecord) -> None:
        nonlocal accepted, finished
        if current["state"] == "settling":
            current["state"] = "stopping"
            accepted = True
        elif current["state"] == "stopping":
            accepted = True
        elif current["state"] in {"down", "stop partial"}:
            accepted = True
            finished = True

    record = update(login, enter)
    if not accepted:
        raise NoLiveRecord(f"shutdown for {login} is no longer settling")
    if finished:
        return {"kind": "stop finished", "record": record}
    return {"kind": "stop underway", "record": record}


def close_failed_stop(login: str, issue: OrchestrationStopIssue) -> None:
    """Close a claimed stop and persist the orchestration failure that ended it."""
    with stop_lock(login):

        def close(current: ShutdownRecord) -> None:
            if current["state"] in {"stopping", "down", "stop partial"}:
                duplicate = any(
                    existing["kind"] == issue["kind"]
                    and existing["machine"] == issue["machine"]
                    and existing["reason"] == issue["reason"]
                    for existing in current["stop_issues"]
                )
                if not duplicate:
                    current["stop_issues"].append(issue)
                current["state"] = "stop partial"

        try:
            _ = update(login, close)
        except NoLiveRecord:
            return


def _timestamp(clock: Clock) -> str:
    return clock().astimezone(timezone.utc).isoformat(timespec="seconds")


def _only(scope: ShutdownScope) -> frozenset[str]:
    if scope["kind"] == "selected":
        return frozenset(scope["session_ids"])
    return frozenset()


def _fresh_inventory(login: str, scope: ShutdownScope) -> Inventory:
    from inventory import inventory

    return inventory(login, _only(scope))


def _fresh_session(stored: Session, report: Inventory) -> FreshSession:
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
        return {"kind": "matching session", "session": current}
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
    login: str,
    session_id: str,
    progress: Literal[
        "stopped", "already gone", "process identity lost", "stop failed"
    ],
    clock: Clock,
    issues: list[SessionStopIssue],
    reason: str = "",
) -> ShutdownRecord:
    def change(current: ShutdownRecord) -> None:
        entry = next(
            item
            for item in current["entries"]
            if item["session"]["session_id"] == session_id
        )
        at = _timestamp(clock)
        if progress == "stop failed":
            entry["progress"] = {
                "kind": "stop failed",
                "at": at,
                "reason": reason,
            }
        elif progress == "stopped":
            entry["progress"] = {"kind": "stopped", "at": at}
        elif progress == "already gone":
            entry["progress"] = {"kind": "already gone", "at": at}
        else:
            entry["progress"] = {"kind": "process identity lost", "at": at}
        entry["stop_issues"] = issues

    return update(login, change)


def _replace_stop_issues(
    login: str, session_id: str, issues: list[SessionStopIssue]
) -> ShutdownRecord:
    def replace(current: ShutdownRecord) -> None:
        entry = _record_entry(current, session_id)
        entry["stop_issues"] = issues

    return update(login, replace)


def _wait_for_exit(
    pid: int,
    *,
    is_alive: LivenessCheck,
    session_record_exists: SessionRecordPresence,
    clock: Clock,
    sleep: Sleeper,
) -> bool:
    deadline = clock() + STOP_WAIT
    for _ in range(101):
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


def _end_busy_codex_seats(
    session: Session, codex_mesh: CommandRunner
) -> None:
    for server in session["codex_servers"]:
        for seat in server["busy_seats"]:
            _ = codex_mesh(
                ["end", "--session-dir", server["run_dir"], "--to", seat]
            )


def _stop_codex_servers(
    session: Session,
    *,
    codex_mesh: CommandRunner,
    is_alive: LivenessCheck,
    clock: Clock,
) -> list[SessionStopIssue]:
    issues: list[SessionStopIssue] = []
    for run_dir in _run_directories(session):
        server_pids = _mesh_server_pids(run_dir)
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
    session: Session, tmux: CommandRunner, clock: Clock
) -> list[SessionStopIssue]:
    if session["kind"] != "unit":
        return []
    tmux_session = session["host"]["tmux_session"]
    target = f"={tmux_session}"
    issue: SessionStopIssue = {
        "kind": "unit tmux session left running",
        "at": _timestamp(clock),
        "tmux_session": tmux_session,
        "cause": "stop not confirmed",
    }
    try:
        killed = tmux(["kill-session", "-t", target])
        if killed is None:
            return [issue]
        remaining = tmux(["has-session", "-t", target])
    except (OSError, subprocess.TimeoutExpired):
        return [issue]
    if remaining is None or remaining == 0:
        return [issue]
    return []


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
) -> None:
    host = session["host"]
    if host["kind"] != "ghostty":
        return
    shell = host["window_shell"]
    if not is_alive(shell) or not _ghostty_still_owns_shell(shell):
        return
    try:
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
) -> list[SessionStopIssue]:
    _close_ghostty_window(session, kill=kill, is_alive=is_alive)
    issues = _stop_codex_servers(
        session, codex_mesh=codex_mesh, is_alive=is_alive, clock=clock
    )
    issues.extend(_stop_unit_tmux(session, tmux, clock))
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
                "kind": "unit tmux session left running",
                "at": _timestamp(clock),
                "tmux_session": session["host"]["tmux_session"],
                "cause": "owner identity lost",
            }
        )
    return issues


def _ready_to_stop(entry: ShutdownSessionEntry, current: Session) -> bool:
    expected = "passive seat ready" if current["kind"] == "seat" else "ready"
    return entry["progress"]["kind"] == expected and current["status"] == "idle"


def _entry_order(record: ShutdownRecord) -> list[str]:
    requester = (
        record["requested_by"]["session_id"]
        if record["requested_by"]["kind"] == "session"
        else ""
    )
    priorities = {"unit": 0, "showrunner": 1, "top-level": 2, "seat": 3}
    indexed = list(enumerate(record["entries"]))
    indexed.sort(
        key=lambda item: (
            item[1]["session"]["session_id"] == requester,
            priorities[item[1]["session"]["kind"]],
            item[0],
        )
    )
    return [item["session"]["session_id"] for _, item in indexed]


def _record_entry(record: ShutdownRecord, session_id: str) -> ShutdownSessionEntry:
    return next(
        item
        for item in record["entries"]
        if item["session"]["session_id"] == session_id
    )


def _stop_owner(
    login: str,
    entry: ShutdownSessionEntry,
    *,
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
    report = fresh_inventory(login, _scope_for(login))
    match = _fresh_session(stored, report)
    if match["kind"] == "session identity lost":
        _ = _set_stop_outcome(
            login,
            stored["session_id"],
            "process identity lost",
            clock,
            _owner_identity_lost_issues(stored, clock),
        )
        return
    if match["kind"] == "session account unreadable":
        _ = _replace_stop_issues(
            login,
            stored["session_id"],
            [{"kind": "account unreadable", "at": _timestamp(clock)}],
        )
        return
    if match["kind"] == "session absent":
        issues = _stop_owner_resources(
            stored,
            kill=kill,
            is_alive=is_alive,
            codex_mesh=codex_mesh,
            tmux=tmux,
            clock=clock,
        )
        _ = _set_stop_outcome(
            login, stored["session_id"], "already gone", clock, issues
        )
        return

    current = match["session"]
    if not force_now and not _ready_to_stop(entry, current):
        match entry["progress"]["kind"]:
            case "waiting":
                not_ready_progress: Literal[
                    "waiting", "ready", "passive seat ready"
                ] = "waiting"
            case "ready":
                not_ready_progress = "ready"
            case "passive seat ready":
                not_ready_progress = "passive seat ready"
            case _:
                return
        issue = NotReadyToStop(
            kind="not ready to stop",
            at=_timestamp(clock),
            status=current["status"],
            progress=not_ready_progress,
        )
        _ = _replace_stop_issues(login, stored["session_id"], [issue])
        return
    if force_now:
        _end_busy_codex_seats(current, codex_mesh)
    try:
        kill(stored["pid"], signal.SIGTERM)
    except ProcessLookupError:
        issues = _stop_owner_resources(
            stored,
            kill=kill,
            is_alive=is_alive,
            codex_mesh=codex_mesh,
            tmux=tmux,
            clock=clock,
        )
        _ = _set_stop_outcome(
            login, stored["session_id"], "already gone", clock, issues
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
            login,
            stored["session_id"],
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
    ):
        second_report = fresh_inventory(login, _scope_for(login))
        second = _fresh_session(stored, second_report)
        if second["kind"] == "session identity lost":
            _ = _set_stop_outcome(
                login,
                stored["session_id"],
                "process identity lost",
                clock,
                _owner_identity_lost_issues(stored, clock),
            )
            return
        if second["kind"] == "session account unreadable":
            _ = _replace_stop_issues(
                login,
                stored["session_id"],
                [{"kind": "account unreadable", "at": _timestamp(clock)}],
            )
            return
        if second["kind"] == "session absent":
            stopped_progress = "already gone"
        else:
            try:
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
                    login,
                    stored["session_id"],
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
                        login,
                        stored["session_id"],
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
    )
    _ = _set_stop_outcome(
        login, stored["session_id"], stopped_progress, clock, issues
    )


def _scope_for(login: str) -> ShutdownScope:
    found = find_live(login)
    if found["kind"] == "no shutdown":
        raise NoLiveRecord(f"no live shutdown for {login}")
    return found["record"]["scope"]


def _stop_seats(
    login: str,
    *,
    fresh_inventory: FreshInventory,
    clock: Clock,
) -> None:
    found = find_live(login)
    if found["kind"] == "no shutdown":
        raise NoLiveRecord(f"no live shutdown for {login}")
    record = found["record"]
    report = fresh_inventory(login, record["scope"])
    live = {item["session_id"]: item for item in report["sessions"]}
    unattributed = {item["pid"]: item["reason"] for item in report["unattributed"]}
    entries = {
        item["session"]["session_id"]: item for item in record["entries"]
    }
    for entry in record["entries"]:
        session = entry["session"]
        if session["kind"] != "seat":
            continue
        if entry["progress"]["kind"] in {
            "stopped",
            "already gone",
            "process identity lost",
            "stop failed",
        }:
            continue
        if unattributed.get(session["pid"]) == "account unreadable":
            _ = _replace_stop_issues(
                login,
                session["session_id"],
                [{"kind": "account unreadable", "at": _timestamp(clock)}],
            )
            continue
        owner = session["owner"]
        owner_stopped = False
        owner_absent = owner["kind"] == "no director"
        if owner["kind"] == "director":
            owner_entry = entries.get(owner["session_id"])
            if owner_entry is None:
                owner_absent = owner["session_id"] not in live
            else:
                owner_stopped = owner_entry["progress"]["kind"] in {
                    "stopped",
                    "already gone",
                }
                owner_absent = (
                    owner["session_id"] not in live
                    and unattributed.get(owner_entry["session"]["pid"])
                    != "account unreadable"
                )
        seat_live = (
            session["session_id"] in live or session["pid"] in unattributed
        )
        if owner_stopped or (owner_absent and not seat_live):
            _ = _set_stop_outcome(
                login, session["session_id"], "stopped", clock, []
            )
        else:
            _ = _replace_stop_issues(
                login,
                session["session_id"],
                [{"kind": "seat still live", "at": _timestamp(clock)}],
            )


def _issue_allows_down(issue: SessionStopIssue) -> bool:
    if issue["kind"] == "codex server left running":
        return issue["cause"] == "owner identity lost"
    if issue["kind"] == "unit tmux session left running":
        return issue["cause"] == "owner identity lost"
    return False


def _finish(login: str) -> ShutdownRecord:
    def finish(current: ShutdownRecord) -> None:
        terminal = {
            "stopped",
            "already gone",
            "process identity lost",
        }
        complete = all(
            entry["progress"]["kind"] in terminal for entry in current["entries"]
        )
        only_identity_lost = all(
            _issue_allows_down(issue)
            for entry in current["entries"]
            for issue in entry["stop_issues"]
        )
        current["state"] = (
            "down"
            if complete and only_identity_lost and not current["stop_issues"]
            else "stop partial"
        )

    return update(login, finish)


def _stop_report(
    record: ShutdownRecord, fresh_inventory: FreshInventory
) -> StopReport:
    final_inventory = fresh_inventory(record["login"], record["scope"])
    counts: dict[str, int] = {}
    for entry in record["entries"]:
        kind = entry["session"]["kind"]
        counts[kind] = counts.get(kind, 0) + 1
    return {
        "record": record,
        "unattributed": list(final_inventory["unattributed"]),
        "counts": counts,
    }


def stop(
    login: str,
    *,
    kill: KillProcess = os.kill,
    is_alive: LivenessCheck = process_is_alive,
    session_record_exists: SessionRecordPresence = session_record_exists,
    fresh_inventory: FreshInventory = _fresh_inventory,
    clock: Clock = now_utc,
    codex_mesh: CommandRunner = run_codex_mesh,
    tmux: CommandRunner = run_tmux,
    sleep: Sleeper = time.sleep,
) -> StopReport:
    """Stop one machine's record in safe order with all live effects injectable."""
    with stop_lock(login):
        entered = _enter_stopping(login)
        record = entered["record"]
        if entered["kind"] == "stop finished":
            return _stop_report(record, fresh_inventory)
        for session_id in _entry_order(record):
            current = find_live(login)
            if current["kind"] == "no shutdown":
                raise NoLiveRecord(f"no live shutdown for {login}")
            entry = _record_entry(current["record"], session_id)
            progress = entry["progress"]
            if progress["kind"] in {
                "stopped",
                "already gone",
                "process identity lost",
                "stop failed",
            }:
                continue
            if entry["session"]["kind"] == "seat":
                continue
            _stop_owner(
                login,
                entry,
                force_now=current["record"]["force"] == "now",
                kill=kill,
                is_alive=is_alive,
                session_record_exists=session_record_exists,
                fresh_inventory=fresh_inventory,
                clock=clock,
                codex_mesh=codex_mesh,
                tmux=tmux,
                sleep=sleep,
            )
        _stop_seats(login, fresh_inventory=fresh_inventory, clock=clock)
        return _stop_report(_finish(login), fresh_inventory)


def parse_stop_report(text: str) -> StopReport:
    """Validate the JSON returned by the internal remote stop verb."""
    try:
        value = cast(object, json.loads(text))
    except (TypeError, ValueError) as error:
        raise ValueError("stop report is not valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("stop report is not an object")
    fields = cast(dict[str, object], value)
    record = parse_records(json.dumps([fields.get("record")]))[0]

    raw_counts = fields.get("counts")
    if not isinstance(raw_counts, dict):
        raise ValueError("stop report counts is not an object")
    counts: dict[str, int] = {}
    for name, count in cast(dict[object, object], raw_counts).items():
        if not isinstance(name, str) or not isinstance(count, int) or isinstance(count, bool):
            raise ValueError("stop report counts is invalid")
        counts[name] = count
    raw_unattributed = fields.get("unattributed")
    if not isinstance(raw_unattributed, list):
        raise ValueError("stop report unattributed is not a list")
    unattributed_items = cast(list[object], raw_unattributed)
    if not all(isinstance(item, dict) for item in unattributed_items):
        raise ValueError("stop report unattributed is not a list")
    unattributed: list[UnattributedSession] = []
    for index, item in enumerate(unattributed_items):
        values = cast(dict[str, object], item)
        pid = values.get("pid")
        name = values.get("name")
        reason = values.get("reason")
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or not isinstance(name, str)
            or reason not in {"account unreadable", "process start mismatch"}
        ):
            raise ValueError(f"stop report unattributed[{index}] is invalid")
        unattributed.append(
            {
                "pid": pid,
                "name": name,
                "reason": cast(
                    Literal["account unreadable", "process start mismatch"], reason
                ),
            }
        )
    return {
        "record": record,
        "unattributed": unattributed,
        "counts": counts,
    }
