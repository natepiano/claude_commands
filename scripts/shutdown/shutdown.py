#!/usr/bin/env python3
"""Inspect, stop and restore each Claude session of one account."""

from __future__ import annotations

import argparse
import copy
import io
from contextlib import redirect_stdout
import os
import json
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, TypedDict, cast

SCRIPTS = Path(__file__).resolve().parents[1]
for dependency in ("whoami", "message", "production", "lib"):
    sys.path.insert(0, str(SCRIPTS / dependency))

from account import (
    Account,
    UnknownAccountName,
    UnreadableAccount,
    named_claude_account,
    own_claude_account,
    claude_account,
)
from inventory import (
    CheckoutState,
    Host,
    InvalidInventory,
    Inventory,
    Session,
    inventory,
    listing,
    parse_inventory,
)
import inventory as inventory_work
import shutdown_record as recovery_store
from launch_permission import (
    RELEASED_HOLD_PREFIX,
    LaunchPurpose,
    NewWorkLaunchPurpose,
    RecordedSessionRestoreLaunchPurpose,
    shutdown_state_root,
)
from shutdown_record import (
    CheckoutMove,
    InvalidRecord,
    SessionShutdown,
    ShutdownContinuation,
    AccountOpen,
    HoldReleased,
    RequestOrigin,
    ShutdownFound,
    ConductorRunning,
    ConductorNotRunning,
    ConductorUnavailable,
    conductor_status,
    lookup_session,
    release_finished_account_hold,
    all_account_holds,
    all_session_records,
    parse_account_holds,
    parse_session_records,
    session_records,
)
from remote import other_machine, run_remote
import settle
import stop as stop_work
import restart as restart_work
import remote_control
import note_file
from user_zone import user_zone


class CommandLine(argparse.Namespace):
    command: str = ""
    account: str | None = None
    as_json: bool = False
    here: bool = False
    also: str = ""
    only: str | None = None
    request_id: str = ""
    requested_by: str | None = None
    message: list[str] | None = None
    where: str = ""
    force: str = ""
    reason: str = ""
    dry_run: bool = False
    no_alert: bool = False
    restart_of: str | None = None
    restart: bool = False
    target: str = ""
    why: str = ""
    move_id: str = ""
    refused: str = ""
    auto: bool = False
    reset: datetime | None = None
    session_id: str = ""
    pid: int = 0
    proc_start: str = ""
    who: str = ""
    session: str = ""
    then: ShutdownContinuation = "stop"
    forget_name: str = ""


class UnreachableMachine(TypedDict):
    machine: str
    unreachable: Literal[True]


class UnavailableMachine(TypedDict):
    machine: str
    status: Literal["unavailable"]
    rc: int
    reason: str


class UnresolvedRecoverySession(TypedDict):
    session_id: str
    name: str
    agent: Literal["claude", "codex"]
    last_seen: str


class SnapshotOutcomeRecorded(TypedDict):
    kind: Literal["recorded"]
    last_success: recovery_store.LastSuccessfulSnapshot | recovery_store.NoSuccessfulSnapshotRecorded
    latest: recovery_store.SnapshotTaken | recovery_store.SnapshotFailed


class NoSnapshotOutcomeRecorded(TypedDict):
    kind: Literal["no outcome recorded"]


class SnapshotOutcomeUnreadable(TypedDict):
    kind: Literal["unreadable"]
    error: str


SnapshotStatus = SnapshotOutcomeRecorded | NoSnapshotOutcomeRecorded | SnapshotOutcomeUnreadable


class RecoveryStatusInventory(Inventory):
    recovery: list[UnresolvedRecoverySession]
    snapshot: SnapshotStatus


MachineStatusReport = Inventory | UnreachableMachine | UnavailableMachine
MachineRecordReport = SessionShutdown | UnreachableMachine | UnavailableMachine


class AccountShutdownRecords(TypedDict):
    """One machine's session shutdowns for an account, oldest first."""

    kind: Literal["records"]
    records: list[SessionShutdown]


class UnreadableShutdownRecord(TypedDict):
    kind: Literal["unreadable"]
    machine: str
    reason: str


class UnreachableShutdownRecord(TypedDict):
    kind: Literal["unreachable"]
    machine: str


class UnavailableShutdownRecord(TypedDict):
    kind: Literal["unavailable"]
    machine: str
    rc: int


LocalShutdownRecordOutcome = AccountShutdownRecords | UnreadableShutdownRecord
RemoteShutdownRecordOutcome = (
    AccountShutdownRecords
    | UnreadableShutdownRecord
    | UnreachableShutdownRecord
    | UnavailableShutdownRecord
)
def _selected_account(requested: str | None) -> Account:
    return own_claude_account() if requested is None else named_claude_account(requested)


def _host_text(host: Host) -> str:
    match host["kind"]:
        case "unit":
            return f"unit {host['production']}/{host['unit']} (herdr {host['session_name']})"
        case "legacy tmux" | "legacy tmux unit":
            return f"{host['kind']} {host['session_name']} (manual recovery)"
        case "herdr":
            return f"herdr {host['herdr_session']}"
        case "ghostty" | "zed":
            desktop = host["desktop"]
            return (
                f"{host['kind']} on {desktop['name']}"
                if desktop["kind"] == "named"
                else host["kind"]
            )
        case kind:
            return kind


def _checkout_text(checkout: CheckoutState) -> str:
    if checkout["kind"] == "not a checkout":
        return "not a checkout"
    head = checkout["head"]
    name = head["name"] if head["kind"] == "branch" else head["commit"][:12]
    upstream = checkout["upstream"]
    tracking = (
        f"ahead {upstream['ahead']}"
        if upstream["kind"] == "tracking"
        else "no upstream"
    )
    return f"{name} {tracking}, {len(checkout['dirty'])} dirty"


def _session_lines(session: Session, report: Inventory) -> list[str]:
    summary = f"  {session['kind']} {session['name']} · {_host_text(session['host'])}"
    summary += f" · {session['status']} · {_checkout_text(session['checkout'])}"
    lines = [summary]
    if session["host"]["kind"] in {"legacy tmux", "legacy tmux unit"}:
        at = settle.record_time()
        snapshot = recovery_store.RecoverySnapshot(machine=report["machine"], login=report["login"],
            label=report["label"], taken_at=at, sessions=[])
        entry = recovery_store.SnapshotClaudeSession(agent="claude", session=session, workers=[],
            timers=[], last_seen=at, state="unresolved")
        saved = recovery_store.snapshot_restore_record(snapshot, entry)
        lines.append(f"    manual restart: {restart_work.legacy_resume_command(saved)}")
    for server in session["codex_servers"]:
        activity = (
            "busy: " + ", ".join(server["busy_workers"])
            if server["busy_workers"]
            else "idle"
        )
        lines.append(f"    Codex {server['run_dir']} · pid {server['pid']} · {activity}")
    lines.extend(f"    timer {timer}" for timer in session["timers"])
    return lines


def _inventory_lines(report: Inventory) -> list[str]:
    lines = [f"{report['machine']}:"]
    for session in report["sessions"]:
        lines.extend(_session_lines(session, report))
    lines.extend(
        f"  unknown account · {item['pid']} {item['name']} · {item['reason']}"
        for item in report["unattributed"]
    )
    if not report["sessions"] and not report["unattributed"]:
        lines.append(f"  no sessions on {report['label']}")
    return lines


def _unavailable(machine: str, rc: int, reason: str) -> UnavailableMachine:
    return {
        "machine": machine,
        "status": "unavailable",
        "rc": rc,
        "reason": reason.strip() or "remote command failed",
    }


def _status(selected: Account, here: bool, only: tuple[str, ...] = ()) -> list[MachineStatusReport]:
    local = inventory(selected.login, only=frozenset(only)) if only else inventory(selected.login)
    unresolved = _unresolved_snapshot_status(selected.login, only)
    local = RecoveryStatusInventory(**local, recovery=unresolved, snapshot=_snapshot_status())
    reports: list[MachineStatusReport] = [local]
    if here:
        return reports
    arguments = ["status", selected.login, "--json", "--here"]
    status, output = run_remote(arguments)
    machine = other_machine()
    if status == 255:
        reports.append({"machine": machine, "unreachable": True})
        return reports
    if status != 0:
        reports.append(_unavailable(machine, status, output))
        return reports
    try:
        remote = parse_inventory(output)
    except InvalidInventory as error:
        reports.append(_unavailable(machine, status, str(error)))
        return reports
    if only:
        remote["sessions"] = [session for session in remote["sessions"]
            if session["session_id"] in only or (session["kind"] == "worker"
                and session["owner"]["kind"] == "director" and session["owner"]["session_id"] in only)]
        remote["unattributed"] = []
    reports.append(remote)
    return reports


def _local_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return parsed.astimezone(user_zone()).strftime(
        "%Y-%m-%d %H:%M:%S %Z"
    )


def _shutdown_hold_time(value: str) -> str:
    return datetime.fromisoformat(value).astimezone(
        user_zone()
    ).strftime("%Y-%m-%d %H:%M %Z")


def _requester(origin: RequestOrigin) -> str:
    match origin["kind"]:
        case "session":
            return f"requested by session {origin['session_id'][:8]}"
        case "terminal":
            return "requested in a terminal"
        case "snapshot":
            return "requested by a recovery snapshot"


def launch_blocked(selected: Account, purpose: LaunchPurpose) -> int:
    """Permit recorded restorable progress, even while the account is held.

    Stopped, already gone, process identity lost, restarting, restart failed
    and manual restart permit restore. Other recorded progress blocks it.
    Without a record, only the account hold decides, as for new work.
    The restore purpose alone does not authorize a launch. A finished
    account hold is removed and the launch permitted, with one
    `released account hold: <JSON>` line."""
    try:
        _ = session_records(selected.login)
        if isinstance(purpose, RecordedSessionRestoreLaunchPurpose):
            found = lookup_session(selected.login, purpose.session_id)
            if isinstance(found, ShutdownFound):
                restoring = found.record
                if restoring["progress"]["kind"] in {
                    "stopped", "already gone", "process identity lost", "restarting",
                    "restart failed", "manual restart",
                }:
                    return 1
                hold_time = _shutdown_hold_time(restoring["requested_at"])
                print(f"{restoring['session']['name']} is held by a shutdown (since {hold_time})")
                return 0
        outcome = release_finished_account_hold(selected.login)
    except (InvalidRecord, OSError, ValueError) as error:
        print(f"shutdown state unreadable: {error}", file=sys.stderr)
        return 3
    if isinstance(outcome, HoldReleased):
        print(RELEASED_HOLD_PREFIX + json.dumps(outcome.hold, sort_keys=True))
        return 1
    if isinstance(outcome, AccountOpen):
        return 1
    hold_time = _shutdown_hold_time(outcome.hold["requested_at"])
    print(f"{outcome.hold['label']} is held by a shutdown (since {hold_time}), {_requester(outcome.hold['requested_by'])}")
    return 0


def _unexpected_launch_blocked_error(error: Exception) -> int:
    print(
        f"shutdown state unreadable: {type(error).__name__}: {error}",
        file=sys.stderr,
    )
    return 3


def _record_line(record: SessionShutdown) -> str:
    return (
        f"{record['machine']} {record['label']} {record['session']['name']}: {record['progress']['kind']} "
        + f"since {_local_time(record['requested_at'])}"
    )


def _local_shutdown_records(
    login: str, machine: str
) -> LocalShutdownRecordOutcome:
    try:
        return {"kind": "records", "records": restart_work.clear_running_on_target(session_records(login)).remaining}
    except InvalidRecord as error:
        return {"kind": "unreadable", "machine": machine, "reason": str(error)}


def _remote_shutdown_records(login: str) -> RemoteShutdownRecordOutcome:
    machine = other_machine()
    status, output = run_remote(["records", "--json", "--here"])
    if status == 255:
        return {"kind": "unreachable", "machine": machine}
    if status != 0:
        return {"kind": "unavailable", "machine": machine, "rc": status}
    try:
        records = parse_session_records(output)
    except InvalidRecord as error:
        return {"kind": "unreadable", "machine": machine, "reason": str(error)}
    return {
        "kind": "records",
        "records": [record for record in records if record["login"].casefold() == login.casefold()],
    }


def _shutdown_record_lines(
    outcome: LocalShutdownRecordOutcome | RemoteShutdownRecordOutcome,
    observations: tuple[Inventory, ...] = (),
    *, local_observation: bool = True,
) -> list[str]:
    match outcome["kind"]:
        case "records":
            lines: list[str] = []
            for record in outcome["records"]:
                if (record["progress"]["kind"] == "restart failed"
                        and isinstance(_restart_failed_liveness(record, observations), restart_work.SessionLive)):
                    record_lines = ["  " + settle.running_restart_line(record)]
                else:
                    record_lines = settle.status_record_lines(
                        record, _observed_owner(record, observations), local_observation=local_observation)
                if local_observation:
                    record_lines[1:1] = settle.failed_prompt_screen_lines(record)
                lines.extend(record_lines)
            return lines
        case "unreachable":
            return [
                f"{outcome['machine']}: shutdown record not reached (unreachable)"
            ]
        case "unavailable":
            return [
                f"{outcome['machine']}: shutdown record not reached (unavailable, rc {outcome['rc']})"
            ]
        case "unreadable":
            return [
                f"{outcome['machine']}: shutdown record unreadable: {outcome['reason']}"
            ]


def _observed_owner(record: SessionShutdown, observations: tuple[Inventory, ...]) -> stop_work.FreshSession:
    for report in observations:
        if report["machine"].casefold() == record["machine"].casefold() and report["login"].casefold() == record["login"].casefold():
            return stop_work.fresh_session(record["session"], report)
    return stop_work.SessionAbsent(kind="session absent")



@dataclass(frozen=True)
class _SelectableSession:
    session_id: str
    name: str
    machine: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedSessions:
    session_ids: tuple[str, ...]
    report: Inventory


class SessionSelectionError(ValueError):
    """The requested items do not identify exactly one account session."""


class SessionNotFound(SessionSelectionError):
    """No session of this account matches the requested id or name."""


def resolve_sessions(login: str, items: Iterable[str], *, here: bool = False, recovery: bool = False) -> ResolvedSessions:
    """Resolve names from membership, then build one filtered inventory."""
    requested = list(dict.fromkeys(items))
    if not requested or any(not item for item in requested):
        raise SessionSelectionError("--only needs a session id or exact name")
    local_machine = settle.local_machine()
    local: dict[str, _SelectableSession] = {}
    for saved in session_records(login):
        owner = saved["session"]
        local[owner["session_id"]] = _SelectableSession(owner["session_id"], owner["name"], saved["machine"])
    if recovery:
        snapshot = recovery_store.read_snapshot(login)
        if not isinstance(snapshot, recovery_store.NoRecoverySnapshot):
            for item in snapshot["sessions"]:
                owner = item["session"]
                host = owner["host"]
                aliases = (host["herdr_session"],) if host["kind"] == "herdr" else ()
                _ = local.setdefault(owner["session_id"], _SelectableSession(
                    owner["session_id"], owner["name"], snapshot["machine"], aliases))
    recorded = dict(local)
    for owner in listing(login):
        if owner.kind != "worker":
            local[owner.session_id] = _SelectableSession(owner.session_id, owner.name, local_machine)
    candidates = dict(local)
    peer_unavailable = False
    needs_peer = not here and any(item not in local for item in requested)
    if needs_peer:
        peer = _remote_shutdown_records(login)
        if peer["kind"] != "records":
            peer_unavailable = True
        else:
            for saved in peer["records"]:
                owner = saved["session"]
                candidate = _SelectableSession(owner["session_id"], owner["name"], saved["machine"])
                _ = recorded.setdefault(owner["session_id"], candidate)
                _ = candidates.setdefault(owner["session_id"], candidate)
            status, output = run_remote(["status", login, "--json", "--here"])
            if status != 0:
                peer_unavailable = True
            else:
                try:
                    report = parse_inventory(output)
                    if report["login"].casefold() != login.casefold():
                        raise InvalidInventory("peer inventory is for another account")
                except InvalidInventory:
                    peer_unavailable = True
                else:
                    for owner in report["sessions"]:
                        if owner["kind"] != "worker":
                            _ = candidates.setdefault(owner["session_id"], _SelectableSession(owner["session_id"], owner["name"], report["machine"]))
        if peer_unavailable:
            print(f"{other_machine()}: not reached; names checked on {local_machine} only", file=sys.stderr)
    result: list[str] = []
    for item in requested:
        saved_matches = ([recorded[item]] if item in recorded else
                         [session for session in recorded.values() if session.name == item or item in session.aliases])
        if recovery and saved_matches:
            matches = saved_matches
        elif item in local:
            matches = [local[item]]
        elif item in candidates:
            matches = [candidates[item]]
        else:
            matches = [session for session in candidates.values() if session.name == item or item in session.aliases]
        if len(matches) != 1:
            reason = "matches no session" if not matches else "matches more than one session"
            missing = f"; {other_machine()} not reached" if peer_unavailable else ""
            listed = matches or list(candidates.values())
            lines = [f"{item}: {reason}{missing}"]
            lines.extend(f"  {session.machine}: {session.name} ({session.session_id})" for session in listed)
            error_type = SessionSelectionError if matches else SessionNotFound
            raise error_type("\n".join(lines))
        if matches[0].session_id not in result:
            result.append(matches[0].session_id)
    report = (Inventory(machine=local_machine, login=login, label=login, sessions=[], unattributed=[])
              if login.startswith("codex-") else inventory(login, only=frozenset(result)))
    return ResolvedSessions(tuple(result), report)


def _snapshot() -> int:
    observed = inventory_work.snapshot_inventory()
    at = settle.record_time()
    if isinstance(observed, inventory_work.PartialInventory):
        _record_snapshot_outcome(recovery_store.SnapshotFailed(kind="failed", at=at, reasons=list(observed.failures)))
        print("snapshot incomplete: " + "; ".join(observed.failures), file=sys.stderr)
        return 1
    try:
        saved = recovery_store.merge_snapshots(observed, at)
    except (InvalidRecord, OSError) as error:
        _record_snapshot_outcome(recovery_store.SnapshotFailed(kind="failed", at=at, reasons=[str(error)]))
        raise
    _record_snapshot_outcome(recovery_store.SnapshotTaken(kind="taken", at=at, accounts=len(saved)))
    print(f"recovery snapshot saved for {len(saved)} accounts")
    return 0


def _record_snapshot_outcome(outcome: recovery_store.SnapshotTaken | recovery_store.SnapshotFailed) -> None:
    try:
        recovery_store.record_snapshot_attempt(outcome, settle.local_machine())
    except (InvalidRecord, OSError) as error:
        print(f"snapshot outcome not recorded: {error}", file=sys.stderr)


def _snapshot_status() -> SnapshotStatus:
    try:
        attempts = recovery_store.read_snapshot_attempts()
    except (InvalidRecord, OSError) as error:
        return SnapshotOutcomeUnreadable(kind="unreadable", error=str(error))
    if isinstance(attempts, recovery_store.NoSnapshotAttempt):
        return NoSnapshotOutcomeRecorded(kind="no outcome recorded")
    return SnapshotOutcomeRecorded(kind="recorded", last_success=attempts["last_success"], latest=attempts["latest"])


def _snapshot_outcome_lines(status: SnapshotStatus) -> list[str]:
    if status["kind"] == "unreadable":
        return [f"  recovery snapshot: outcome unreadable: {status['error']}"]
    lines = ["  recovery snapshot: no successful snapshot recorded"]
    if status["kind"] == "recorded":
        success = status["last_success"]
        if success["kind"] == "taken":
            elapsed = datetime.fromisoformat(settle.record_time()) - datetime.fromisoformat(success["at"])
            age = settle.elapsed_text(int(elapsed.total_seconds()))
            lines = [f"  recovery snapshot: last taken {_local_time(success['at'])} ({age} ago)"]
        latest = status["latest"]
        if latest["kind"] == "failed":
            lines.append(f"  recovery snapshot failed {_local_time(latest['at'])}: {'; '.join(latest['reasons'])}")
    return lines


@dataclass
class ForgetCandidate:
    login: str
    label: str
    session_id: str
    name: str
    names: set[str]


def _forget(text: str, account: str = "") -> int:
    if account:
        try:
            account = _selected_account(account).login
        except (UnreadableAccount, UnknownAccountName) as error:
            print(f"shutdown: {error}", file=sys.stderr)
            return 2
    candidates: dict[tuple[str, str], ForgetCandidate] = {}
    machine = settle.local_machine()

    def add(login: str, label: str, owner: Session) -> None:
        if account and account.casefold() != login.casefold():
            return
        key = (login.casefold(), owner["session_id"])
        names = {owner["session_id"], owner["name"]}
        host = owner["host"]
        if host["kind"] == "herdr":
            names.add(host["herdr_session"])
        if key in candidates:
            candidates[key].names.update(names)
        else:
            candidates[key] = ForgetCandidate(login, label, owner["session_id"], owner["name"], names)

    for snapshot in recovery_store.all_snapshots():
        if snapshot["machine"].casefold() != machine.casefold():
            continue
        for item in snapshot["sessions"]:
            add(snapshot["login"], snapshot["label"], item["session"])
    for saved in all_session_records():
        if saved["machine"].casefold() == machine.casefold():
            add(saved["login"], saved["label"], saved["session"])
    exact = [item for item in candidates.values() if item.session_id == text]
    matches = exact or [item for item in candidates.values() if text in item.names]
    # After a move one session can have a record on each account; forgetting it forgets every copy.
    if len({item.session_id for item in matches}) != 1:
        print(f"forget: {text} matches {'no local session' if not matches else 'more than one local session'}", file=sys.stderr)
        for item in matches:
            print(f"  {item.label}: {item.name} ({item.session_id})", file=sys.stderr)
        return 2
    code = 0
    for item in matches:
        outcome = recovery_store.forget_session(item.login, item.session_id, machine)
        if isinstance(outcome, recovery_store.SessionRetained):
            print(f"forget: {item.name}: cannot forget shutdown in progress ({outcome.record['progress']['kind']})", file=sys.stderr)
            code = 1
            continue
        _ = recovery_store.acknowledge_snapshot(item.login, item.session_id)
        print(f"{item.name}: forgotten" + (f" on {item.label}" if len(matches) > 1 else ""))
    return code


def _unresolved_snapshot_lines(login: str, only: tuple[str, ...] = ()) -> list[str]:
    snapshot = recovery_store.read_snapshot(login)
    if isinstance(snapshot, recovery_store.NoRecoverySnapshot):
        return []
    lines: list[str] = []
    for item in snapshot["sessions"]:
        owner = item["session"]
        if item["state"] != "unresolved" or (only and owner["session_id"] not in only):
            continue
        lines.append(f"  {owner['name']}: unresolved; last seen {_local_time(item['last_seen'])}")
    return lines


def _unresolved_snapshot_status(login: str, only: tuple[str, ...] = ()) -> list[UnresolvedRecoverySession]:
    snapshot = recovery_store.read_snapshot(login)
    if isinstance(snapshot, recovery_store.NoRecoverySnapshot):
        return []
    return [UnresolvedRecoverySession(session_id=item["session"]["session_id"], name=item["session"]["name"],
            agent=item["agent"], last_seen=item["last_seen"])
            for item in snapshot["sessions"] if item["state"] == "unresolved"
            and (not only or item["session"]["session_id"] in only)]


def _remote_conductor_status(login: str) -> ConductorRunning | ConductorNotRunning | ConductorUnavailable:
    status, output = run_remote(["conductor-status", login, "--json", "--here"])
    if status != 0:
        return ConductorUnavailable(f"{other_machine()}: not reached")
    try:
        value = cast(object, json.loads(output))
        if not isinstance(value, dict):
            raise ValueError("invalid conductor response")
        fields = cast(dict[str, object], value)
        if fields.get("kind") == "running":
            return ConductorRunning()
        if fields.get("kind") == "not running":
            return ConductorNotRunning()
        if fields.get("kind") == "unavailable" and isinstance(fields.get("reason"), str):
            return ConductorUnavailable(cast(str, fields["reason"]))
        raise ValueError("invalid conductor response")
    except (ValueError, TypeError) as error:
        return ConductorUnavailable(str(error))


def _restart_failed_liveness(saved: SessionShutdown, observations: tuple[Inventory, ...] = ()) -> restart_work.SessionLive | restart_work.SessionNotLive | restart_work.SessionLivenessUnknown:
    if saved["machine"].casefold() == settle.local_machine().casefold():
        return restart_work.restart_failed_liveness(saved)
    for report in observations:
        if report["machine"].casefold() == saved["machine"].casefold():
            if any(owner["session_id"] == saved["session"]["session_id"] for owner in report["sessions"]):
                return restart_work.SessionLive()
    return restart_work.SessionLivenessUnknown(f"session liveness not confirmed on {saved['machine']}")


def _status_actions(records: list[SessionShutdown], ownership: ConductorRunning | ConductorNotRunning | ConductorUnavailable,
                    observations: tuple[Inventory, ...] = ()) -> list[str]:
    lines: list[str] = []
    take_out: list[SessionShutdown] = []
    stop_now: list[SessionShutdown] = []
    bring_back: list[SessionShutdown] = []
    waiting = any(saved["progress"]["kind"] in {"waiting", "ready", "held by usage limit", "reaping"} for saved in records)
    if waiting:
        if isinstance(ownership, ConductorNotRunning):
            pending = [saved for saved in records if saved["progress"]["kind"] in {"waiting", "ready", "held by usage limit", "reaping"}]
            first = pending[0]
            command = settle.shutdown_command("down", first["login"], "--here", "--only", ",".join(saved["session"]["session_id"] for saved in pending))
            lines.append(f"  no conductor running; in a terminal on {first['machine']}: {command}")
        elif isinstance(ownership, ConductorUnavailable):
            lines.append(f"  conductor ownership unavailable: {ownership.reason}")
    for saved in records:
        progress = saved["progress"]["kind"]
        if progress == "restart failed":
            observed = _restart_failed_liveness(saved, observations)
            if isinstance(observed, restart_work.SessionLive):
                continue
            if isinstance(observed, restart_work.SessionLivenessUnknown):
                lines.append(f"  {saved['session']['name']}: liveness unknown: {observed.reason}")
        if progress in {"waiting", "ready", "held by usage limit", "reaping"}:
            take_out.append(saved)
            stop_now.append(saved)
        elif progress == "stop failed":
            take_out.append(saved)
            stop_now.append(saved)
            bring_back.append(saved)
        elif progress in {"stopped", "already gone", "process identity lost", "restart failed", "manual restart", "timers pending", "restarting"}:
            if saved["stop_issues"] and progress in {"stopped", "already gone", "process identity lost"}:
                stop_now.append(saved)
            bring_back.append(saved)
    for label, verb, names in (("take out", "cancel", take_out), ("stop now", "now", stop_now), ("bring back", "restart", bring_back)):
        if names:
            for machine in sorted({saved["machine"] for saved in names}):
                group = [saved for saved in names if saved["machine"] == machine]
                command = settle.shutdown_command(verb, group[0]["login"], "--here", "--only", ",".join(saved["session"]["session_id"] for saved in group))
                lines.append(f"  {label}: in a terminal on {machine}: {command} · {', '.join(saved['session']['name'] for saved in group)}")
    return lines


@dataclass
class RequestHistory:
    read_paths: set[Path] = field(default_factory=set)
    records: list[SessionShutdown] = field(default_factory=list)
    taken_out: set[str] = field(default_factory=set)


def _request_records(login: str, request_id: str, history: RequestHistory) -> list[SessionShutdown]:
    # Read active files first: an archive between these reads is present in at
    # least one source. Deduplication prefers the finished copy in history.
    records = session_records(login)
    directory = shutdown_state_root() / login.casefold() / "history"
    for path in sorted(directory.glob("*.json")):
        if path in history.read_paths:
            continue
        try:
            text = path.read_text(encoding="utf-8")
            value = cast(object, json.loads(text))
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError, ValueError) as error:
            raise InvalidRecord(f"{path} cannot be read: {error}") from error
        if not isinstance(value, dict):
            history.read_paths.add(path)
            continue
        fields = cast(dict[str, object], value)
        if fields.get("request_id") == request_id:
            archived = parse_session_records("[" + text + "]")
            for saved in archived:
                if saved["login"].casefold() == login.casefold():
                    history.records.append(saved)
                    if saved["progress"]["kind"] not in {"restarted", "stopped", "already gone"}:
                        history.taken_out.add(saved["session"]["session_id"])
        history.read_paths.add(path)
    records.extend(history.records)
    return [saved for saved in records if saved["login"].casefold() == login.casefold() and saved["request_id"] == request_id]


def _follow_action(saved: SessionShutdown) -> str:
    progress = saved["progress"]
    kind = progress["kind"]
    place = f"in a terminal on {saved['machine']}"
    liveness_reason = ""
    if kind == "restart failed":
        observed = _restart_failed_liveness(saved)
        if isinstance(observed, restart_work.SessionLive):
            return "\n".join([settle.running_restart_line(saved), *settle.failed_prompt_screen_lines(saved)])
        if isinstance(observed, restart_work.SessionLivenessUnknown):
            liveness_reason = f"; liveness unknown: {observed.reason}"
    if progress["kind"] == "manual restart":
        return f"{saved['session']['name']}: {place}: {progress['command']}"
    verb = "now" if kind in {"stop failed", "waiting", "stopped", "already gone"} else "restart"
    command = settle.shutdown_command(verb, saved["login"], "--here", "--only", saved["session"]["session_id"])
    if kind == "process identity lost":
        return f"{saved['session']['name']}: process identity lost; inspect its window before restarting; {place}: {command}"
    if kind == "waiting":
        return settle.holdout_line(saved)
    return "\n".join([f"{saved['session']['name']}: {kind}{liveness_reason}; {place}: {command}",
                      *settle.failed_prompt_screen_lines(saved)])


def follow(login: str, request_id: str) -> int:
    """Follow one local request through its active records and archived outcomes."""
    known: dict[str, SessionShutdown] = {}
    printed: dict[str, int] = {}
    history = RequestHistory()
    reported_taken_out: set[str] = set()
    command = settle.shutdown_command("follow", login, "--request", request_id)
    try:
        while True:
            for saved in _request_records(login, request_id, history):
                identity = saved["session"]["session_id"]
                known[identity] = saved
            if not known:
                print(f"no shutdown records for request {request_id}; in this terminal: {command}", flush=True)
                return 1
            for identity, saved in known.items():
                trail = saved.get("trail", [])
                for step in trail[printed.get(identity, 0):]:
                    at = datetime.fromisoformat(step["at"]).astimezone(user_zone()).strftime("%H:%M:%S")
                    detail = settle.located_detail(saved, step["detail"])
                    print(f"{at} {saved['session']['name']}: {step['kind']} {detail}".rstrip(), flush=True)
                printed[identity] = len(trail)
                if identity in history.taken_out and identity not in reported_taken_out:
                    at = datetime.now(user_zone()).strftime("%H:%M:%S")
                    print(f"{at} {saved['session']['name']}: taken out", flush=True)
                    reported_taken_out.add(identity)
            for identity, saved in known.items():
                if identity in history.taken_out:
                    continue
                kind = saved["progress"]["kind"]
                if kind in {"process identity lost", "stop failed", "restart failed", "manual restart", "timers pending"} or (kind == "waiting" and "alerted_at" in saved) or (kind in {"stopped", "already gone"} and saved["stop_issues"]):
                    print(_follow_action(saved), flush=True)
                    return 1
            if all(identity in history.taken_out or saved["progress"]["kind"] == "restarted" or (
                saved["then"] == "stop" and saved["progress"]["kind"] in {"stopped", "already gone"}
            ) for identity, saved in known.items()):
                destinations = {saved.get("target") or saved["label"] for saved in known.values() if saved["progress"]["kind"] == "restarted"}
                if history.taken_out:
                    outcomes: list[str] = []
                    for destination in sorted(destinations):
                        count = sum(saved["progress"]["kind"] == "restarted" and (saved.get("target") or saved["label"]) == destination for saved in known.values())
                        outcomes.append(f"{count} back on {destination}")
                    stopped = sum(identity not in history.taken_out and saved["progress"]["kind"] in {"stopped", "already gone"} for identity, saved in known.items())
                    if stopped:
                        outcomes.append(f"{stopped} stopped")
                    outcomes.append(f"{len(history.taken_out)} taken out")
                    summary = f"all {len(known)} done: {', '.join(outcomes)}"
                elif all(saved["progress"]["kind"] == "restarted" for saved in known.values()):
                    summary = f"all {len(known)} back on {', '.join(sorted(destinations))}"
                elif not destinations:
                    summary = f"all {len(known)} stopped"
                else:
                    summary = f"all {len(known)} done: stopped or back on {', '.join(sorted(destinations))}"
                print(summary, flush=True)
                return 0
            time.sleep(1)
    except KeyboardInterrupt:
        print(f"the shutdown keeps running; to follow it again, in this terminal: {command}", flush=True)
        return 130


def _only_ids(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(dict.fromkeys(item.strip() for item in value.split(",")))


def _records(here: bool) -> tuple[list[MachineRecordReport], list[str]]:
    local = restart_work.clear_running_on_target(all_session_records()).remaining
    reports: list[MachineRecordReport] = list(local)
    lines = [_record_line(item) for item in local]
    if here:
        return reports, lines

    status, output = run_remote(["records", "--json", "--here"])
    machine = other_machine()
    if status == 255:
        reports.append({"machine": machine, "unreachable": True})
        lines.append(f"{machine}: unreachable")
        return reports, lines
    if status != 0:
        reports.append(_unavailable(machine, status, output))
        lines.append(f"{machine}: unavailable (rc {status})")
        return reports, lines
    try:
        remote = parse_session_records(output)
    except InvalidRecord as error:
        reports.append(_unavailable(machine, status, str(error)))
        lines.append(f"{machine}: unavailable (rc {status})")
        return reports, lines
    reports.extend(remote)
    lines.extend(_record_line(item) for item in remote)
    return reports, lines


def held_session_ids() -> list[str]:
    """Return sessions whose run folders must survive shutdown and restart."""
    return sorted(
        {
            session["session_id"]
            for record in all_session_records()
            for session in [record["session"], *record["workers"]]
        }
    )


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        usage="in this terminal: ~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py <verb> [arguments]",
        epilog="Run in this terminal: ~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py <verb> <account> [--only ID[,ID...]].\nFollow in this terminal: ~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py follow <account> --request <request-id>.\nInternal fixer in this terminal: ~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py remote-control <session-id> --pid <pid> --proc-start <start> --who <name> --session <session-name>.",
    )
    # Commands without these flags still read them.
    parser.set_defaults(also="", target="")
    commands = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="{down,status,follow,now,cancel,restart,snapshot,forget,remote-control,launch-blocked}",
    )
    _ = commands.add_parser("snapshot", help="save a read-only local recovery observation")
    forget = commands.add_parser("forget", help="forget one local recovery entry or stopped session's restore")
    _ = forget.add_argument("forget_name", metavar="NAME_OR_ID")
    _ = forget.add_argument("--account", help="account login or label containing this session")
    down = commands.add_parser("down", help="start a safe account shutdown")
    _ = down.add_argument("account", nargs="?")
    _ = down.add_argument("--all", dest="down_all", action="store_true",
                          help="stop every local Claude session, with a shared 60-second deadline")
    _ = down.add_argument("--here", action="store_true")
    _ = down.add_argument("--also", metavar="MACHINE", default="",
                          help="also stop the account on the other machine; a stop covers only this machine by default")
    _ = down.add_argument("--only", metavar="NAME[,NAME...]", help="session ids or exact names, separated by commas")
    _ = down.add_argument("--restart", action="store_true")
    from typing import override

    class NumberedClaudeTarget(argparse.Action):
        @override
        def __call__(self, parser: argparse.ArgumentParser, namespace: argparse.Namespace,
                     values: object, option_string: str | None = None) -> None:
            from scripts.accounts import accounts

            assert isinstance(values, str)
            try:
                namespace.target = accounts.numbered_account_label(values, "claude").label
            except accounts.AccountsError as error:
                raise argparse.ArgumentError(self, str(error)) from error

    _ = down.add_argument("--to", dest="target", action=NumberedClaudeTarget, default="", help="resume on claudeN")

    class AwareReset(argparse.Action):
        @override
        def __call__(self, parser: argparse.ArgumentParser, namespace: argparse.Namespace,
                     values: object, option_string: str | None = None) -> None:
            assert isinstance(values, str)
            try:
                reset = datetime.fromisoformat(values)
                if reset.tzinfo is None:
                    raise ValueError("reset needs a timezone offset")
            except ValueError as error:
                raise argparse.ArgumentError(self, "--reset requires an aware ISO time") from error
            namespace.reset = reset

    origin = down.add_mutually_exclusive_group()
    _ = origin.add_argument("--refused", choices=("weekly", "five_hour", "unknown"), default="")
    _ = origin.add_argument("--auto", action="store_true", help="move requested by an automatic account rule")
    _ = down.add_argument("--reset", action=AwareReset)
    _ = down.add_argument("--move-id", default="", help=argparse.SUPPRESS)
    # rename.py checkout: the conductor moves the showrunner's checkout between its stop and its launch.
    _ = down.add_argument("--checkout-move", nargs=2, metavar=("OLD", "NEW"), help=argparse.SUPPRESS)
    _ = down.add_argument("--why", default="", help="reason for the account move")
    _ = down.add_argument("--requested-by", help=argparse.SUPPRESS)
    _ = down.add_argument("--request-id", default="", help=argparse.SUPPRESS)
    _ = down.add_argument("--no-alert", action="store_true", help=argparse.SUPPRESS)
    status = commands.add_parser("status", help="show what runs for one Claude account")
    _ = status.add_argument("account", nargs="?")
    _ = status.add_argument("--json", action="store_true", dest="as_json")
    _ = status.add_argument("--here", action="store_true")
    _ = status.add_argument("--only", metavar="NAME[,NAME...]", help="session ids or exact names, separated by commas")
    follow_command = commands.add_parser("follow", help="follow one local shutdown request")
    _ = follow_command.add_argument("account")
    _ = follow_command.add_argument("--request", dest="request_id", required=True)
    control = commands.add_parser("remote-control", help="enable Remote Control for one restarted process")
    _ = control.add_argument("session_id")
    _ = control.add_argument("--pid", type=int, required=True)
    _ = control.add_argument("--proc-start", required=True)
    _ = control.add_argument("--who", required=True)
    _ = control.add_argument("--session", required=True)
    records = commands.add_parser("records")
    _ = records.add_argument("--json", action="store_true", dest="as_json")
    _ = records.add_argument("--here", action="store_true")
    holds = commands.add_parser("holds")
    _ = holds.add_argument("--json", action="store_true", dest="as_json")
    _ = holds.add_argument("--here", action="store_true")
    conduct = commands.add_parser("conduct")
    _ = conduct.add_argument("account")
    _ = conduct.add_argument("--here", action="store_true")
    ready = commands.add_parser("ready")
    _ = ready.add_argument("--where", required=True)
    cancel = commands.add_parser("cancel", help="take sessions not yet stopped out of a shutdown")
    _ = cancel.add_argument("account", nargs="?")
    _ = cancel.add_argument("--here", action="store_true")
    _ = cancel.add_argument("--only", metavar="NAME[,NAME...]", help="session ids or exact names, separated by commas")
    now = commands.add_parser("now", help="stop without waiting for idle sessions")
    _ = now.add_argument("account", nargs="?")
    _ = now.add_argument("--here", action="store_true", help=argparse.SUPPRESS)
    _ = now.add_argument("--also", metavar="MACHINE", default="",
                         help="also stop the account on the other machine; a stop covers only this machine by default")
    _ = now.add_argument("--only", metavar="NAME[,NAME...]", help="session ids or exact names, separated by commas")
    restart = commands.add_parser(
        "restart", help="bring back stopped sessions; --all restores this machine across accounts"
    )
    _ = restart.add_argument("account", nargs="?")
    _ = restart.add_argument("--all", action="store_true", dest="all_restart", help="restore every account on this machine")
    _ = restart.add_argument("--dry-run", action="store_true")
    _ = restart.add_argument("--only", metavar="NAME[,NAME...]", help="session ids or exact names, separated by commas")
    _ = restart.add_argument("--here", action="store_true")
    _ = restart.add_argument(
        "--no-alert", action="store_true", help=argparse.SUPPRESS
    )
    launch_blocked_command = commands.add_parser("launch-blocked")
    _ = launch_blocked_command.add_argument("account", nargs="?")
    _ = launch_blocked_command.add_argument("--restart-of")
    up = commands.add_parser("up")
    _ = up.add_argument("account")
    _ = up.add_argument("--dry-run", action="store_true")
    _ = up.add_argument("--only")
    _ = up.add_argument("--here", action="store_true")
    _ = up.add_argument("--json", action="store_true", dest="as_json")
    # The peer owns alert delivery for its local sessions.
    _ = up.add_argument("--no-alert", action="store_true", help=argparse.SUPPRESS)
    ownership = commands.add_parser("conductor-status")
    _ = ownership.add_argument("account")
    _ = ownership.add_argument("--json", action="store_true", dest="as_json")
    _ = ownership.add_argument("--here", action="store_true")
    _ = commands.add_parser("held-sessions")
    options = cast(CommandLine, parser.parse_args(arguments))
    if options.command == "down" and options.reset is not None and not options.refused:
        parser.error("--reset requires --refused")
    if options.command == "remote-control" and (options.pid <= 0 or not options.proc_start):
        parser.error("remote-control requires a positive pid and nonempty process start")
    if options.command == "restart":
        try:
            with restart_work.restore_history_pass():
                if cast(bool, getattr(options, "all_restart", False)):
                    if options.account is not None or options.only is not None:
                        parser.error("restart --all cannot join an account or --only")
                    return restart_work.restart_all(dry_run=options.dry_run,
                        alerts=restart_work.NeverAlert() if options.no_alert else restart_work.AlertWhenNotBack())
                return _dispatch(options)
        except (InvalidRecord, OSError, ValueError) as error:
            print(f"shutdown: {error}", file=sys.stderr)
            return 3
    return _dispatch(options)


@dataclass(frozen=True)
class TerminalAccountSelection:
    accounts: tuple[Account, ...]
    unavailable: tuple[str, ...] = ()


def _terminal_accounts(options: CommandLine) -> TerminalAccountSelection:
    """Terminal restarts cover records and holds; other verbs cover configured logins, and `now` also record logins."""
    selected: dict[str, Account] = {}
    unavailable: list[str] = []
    if options.command == "restart":
        candidates = {saved["login"].casefold(): Account("claude", saved["login"], saved["label"])
                      for saved in all_session_records()}
        candidates.update({held["login"].casefold(): Account("claude", held["login"], held["label"])
                           for held in all_account_holds()})
        for snapshot in recovery_store.all_snapshots():
            if snapshot["sessions"]:
                tool = "codex" if all(item["agent"] == "codex" for item in snapshot["sessions"]) else "claude"
                _ = candidates.setdefault(snapshot["login"].casefold(), Account(tool, snapshot["login"], snapshot["label"]))
        if not options.here:
            for command in ("records", "holds"):
                status, output = run_remote([command, "--json", "--here"])
                if status != 0:
                    unavailable.append(f"{other_machine()}: shutdown {command} not reached (rc {status})")
                    continue
                try:
                    peer = parse_session_records(output) if command == "records" else parse_account_holds(output)
                    for saved in peer:
                        _ = candidates.setdefault(saved["login"].casefold(), Account("claude", saved["login"], saved["label"]))
                except InvalidRecord as error:
                    unavailable.append(f"{other_machine()}: shutdown {command} unreadable: {error}")
        selected.update(candidates)
    else:
        default = claude_account(Path.home() / ".claude")
        if default is not None:
            selected[default.login.casefold()] = default
        for note in note_file.read_notes():
            login = note.get("login")
            if note.tool == "claude" and login:
                selected[login.casefold()] = Account("claude", login, note.label)
        if options.command == "now":
            # A paused stop may sit on an account that is no longer configured here.
            for saved in all_session_records():
                _ = selected.setdefault(saved["login"].casefold(), Account("claude", saved["login"], saved["label"]))
        if options.command == "status":
            for snapshot in recovery_store.all_snapshots():
                if snapshot["sessions"]:
                    tool = "codex" if all(item["agent"] == "codex" for item in snapshot["sessions"]) else "claude"
                    _ = selected.setdefault(snapshot["login"].casefold(), Account(tool, snapshot["login"], snapshot["label"]))
    return TerminalAccountSelection(tuple(sorted(selected.values(), key=lambda account: account.label)),
                                    tuple(unavailable))


def _restart_item_running(item: str) -> bool:
    """Skip a live session on any account when no shutdown record owns it."""
    import sessions

    matches = [owner for owner in sessions.live_sessions()
               if item == owner["sessionId"] or item == owner["name"] or item in owner.get("formerNames", [])]
    if len(matches) != 1:
        return False
    identity = matches[0]["sessionId"]
    if any(saved["session"]["session_id"] == identity for saved in all_session_records()):
        return False
    print(f"{matches[0]['name']}: already running; skipped")
    return True


def _all_accounts(options: CommandLine) -> int:
    selection_failed = False
    try:
        scope = _terminal_accounts(options)
        selected = scope.accounts
        for problem in scope.unavailable:
            print(f"shutdown: {problem}; to retry there, in a terminal on {other_machine()}: agents restart --here", file=sys.stderr)
        ids_by_login: dict[str, list[str]] = {account.login: [] for account in selected}
        choices = restart_work.restore_account_choices() if options.command == "restart" else {}
        if options.only is not None:
            for item in _only_ids(options.only):
                try:
                    matches: list[tuple[Account, tuple[str, ...]]] = []
                    recorded_accounts = [account for account in selected if any(
                        item in {saved["session"]["session_id"], saved["session"]["name"]}
                        for saved in session_records(account.login))] if options.command == "restart" else []
                    for account in recorded_accounts or selected:
                        try:
                            resolved = resolve_sessions(account.login, (item,), here=options.here or account.tool == "codex",
                                                        recovery=options.command == "restart")
                        except SessionNotFound:
                            continue
                        matches.append((account, resolved.session_ids))
                    identities = {identity for _account, found in matches for identity in found}
                    if options.command == "restart" and len(identities) == 1:
                        identity = next(iter(identities))
                        decision = choices.get(identity)
                        if isinstance(decision, restart_work.RestoreAccountConflict):
                            raise SessionSelectionError(f"{item}: conflicting account evidence: " + ", ".join(decision.accounts))
                        if isinstance(decision, restart_work.RestoreAccountChosen):
                            matches = [(account, found) for account, found in matches
                                       if account.login.casefold() == decision.source_login]
                    if not matches and options.command == "restart" and _restart_item_running(item):
                        continue
                    if len(matches) != 1:
                        candidates = ", ".join(account.label for account, _ids in matches)
                        raise SessionSelectionError(f"{item}: matches {'more than one account: ' + candidates if matches else 'no session'}")
                    account, ids = matches[0]
                    if options.command == "restart" and len(ids) == 1 and _restart_item_running(ids[0]):
                        continue
                    ids_by_login[account.login].extend(ids)
                except SessionSelectionError as error:
                    if options.command != "restart":
                        raise
                    print(f"shutdown: {error}", file=sys.stderr)
                    selection_failed = True
    except SessionSelectionError as error:
        print(f"shutdown: {error}", file=sys.stderr)
        return 2
    except (InvalidRecord, InvalidInventory, OSError, ValueError) as error:
        print(f"shutdown: {error}", file=sys.stderr)
        return 3
    status = 1 if scope.unavailable or selection_failed else 0
    multiple_accounts = sum(1 for account in selected
                            if options.only is None or ids_by_login[account.login]) > 1
    reports: list[MachineStatusReport] = []
    for account in selected:
        ids = tuple(dict.fromkeys(ids_by_login[account.login]))
        if options.only is not None and not ids:
            continue
        if account.tool == "codex":
            if options.command == "restart":
                status = max(status, restart_work.up(account.login, options.dry_run, ids))
            elif options.command == "status" and not options.as_json:
                print(f"{account.label} · {account.login}")
                print("\n".join(_snapshot_outcome_lines(_snapshot_status())))
                print("\n".join(_unresolved_snapshot_lines(account.login, ids)))
            elif options.command == "status":
                reports.append(RecoveryStatusInventory(machine=settle.local_machine(), login=account.login,
                    label=account.label, sessions=[], unattributed=[],
                    recovery=_unresolved_snapshot_status(account.login, ids), snapshot=_snapshot_status()))
            continue
        if options.command == "status" and options.as_json:
            try:
                reports.extend(_status(account, options.here, ids))
            except (InvalidRecord, InvalidInventory, OSError) as error:
                print(f"shutdown: {account.label}: {error}", file=sys.stderr)
                status = max(status, 3)
            continue
        per_account = copy.copy(options)
        per_account.account = account.login
        if options.only is not None:
            per_account.only = ",".join(ids)
        if options.command == "down" and multiple_accounts:
            # Do not enter the first account's TTY follow before starting the rest.
            with redirect_stdout(io.StringIO()) as account_output:
                result = _dispatch(per_account)
            print(account_output.getvalue(), end="")
        else:
            result = _dispatch(per_account)
        status = max(status, result)
    if options.command == "status" and options.as_json:
        print(json.dumps(reports, sort_keys=True))
    elif not selected:
        print("nothing to restore" if options.command == "restart" else "no configured Claude accounts")
    return status


def _stop_here(options: CommandLine, ids: tuple[str, ...]) -> bool:
    """Whether a stop stays on this machine.

    Stopping a whole account covers only this machine unless --also names the other one.
    Named sessions are stopped where they run, and a move keeps both machines in scope.
    """
    return options.here or not (options.also or ids or options.target)


def _dispatch(options: CommandLine) -> int:
    if options.command == "down" and getattr(options, "down_all", False):
        if options.account or options.only or options.also or options.target or options.restart:
            print("shutdown: down --all is local and cannot join an account, --only, --also, --to or --restart", file=sys.stderr)
            return 2
        return settle.down_all()
    if options.command in {"snapshot", "forget"}:
        try:
            return _snapshot() if options.command == "snapshot" else _forget(options.forget_name, options.account or "")
        except (InvalidRecord, InvalidInventory, OSError, ValueError) as error:
            print(f"shutdown: {error}", file=sys.stderr)
            return 1
    if ((options.command in {"down", "restart", "status"} or (options.command == "now" and options.only is not None))
            and options.account is None and not os.environ.get("CLAUDE_CODE_SESSION_ID")):
        return _all_accounts(options)

    if options.command == "remote-control":
        outcome = remote_control.fix(options.session_id, options.pid, options.proc_start, options.who, options.session)
        return 1 if isinstance(outcome, remote_control.Failed) else 0

    if options.command == "held-sessions":
        try:
            lines = held_session_ids()
        except (InvalidRecord, OSError, ValueError) as error:
            print(f"shutdown: {error}", file=sys.stderr)
            return 3
        if lines:
            print("\n".join(lines))
        return 0

    if options.command == "launch-blocked":
        try:
            selected = _selected_account(options.account)
        except (UnreadableAccount, UnknownAccountName) as error:
            print(f"shutdown: {error}", file=sys.stderr)
            return 2
        except Exception as error:
            return _unexpected_launch_blocked_error(error)
        try:
            purpose: LaunchPurpose = (
                RecordedSessionRestoreLaunchPurpose(options.restart_of)
                if options.restart_of is not None
                else NewWorkLaunchPurpose()
            )
            return launch_blocked(selected, purpose)
        except Exception as error:
            return _unexpected_launch_blocked_error(error)

    if options.command == "conductor-status":
        observed = conductor_status(options.account or "")
        response = ({"kind": "running"} if isinstance(observed, ConductorRunning) else
                    {"kind": "not running"} if isinstance(observed, ConductorNotRunning) else
                    {"kind": "unavailable", "reason": observed.reason})
        print(json.dumps(response, sort_keys=True))
        return 0

    if options.command == "up":
        if options.as_json:
            result = restart_work.up_result(options.account or "", options.dry_run, _only_ids(options.only),
                alerts=restart_work.NeverAlert() if options.no_alert else restart_work.AlertWhenNotBack())
            print(json.dumps(result, sort_keys=True))
            return 1 if result["outcome"] == "partial" or result["refused"] else 0
        return restart_work.up(options.account or "", options.dry_run, _only_ids(options.only))

    if options.command == "restart":
        requested: restart_work.AccountNamed | restart_work.AccountNotNamed = (
            restart_work.AccountNamed(options.account)
            if options.account is not None
            else restart_work.AccountNotNamed()
        )
        alerts: restart_work.AlertWhenNotBack | restart_work.NeverAlert = (
            restart_work.NeverAlert()
            if options.no_alert
            else restart_work.AlertWhenNotBack()
        )
        restart_ids: list[str] = []
        selection_failed = False
        if options.only is not None:
            try:
                selected = restart_work.select_account(requested)
            except UnknownAccountName as error:
                print(f"shutdown: {error}", file=sys.stderr)
                return 2
            except (restart_work.RestartAccountNotDetermined, UnreadableAccount, InvalidRecord, OSError, ValueError) as error:
                print(f"shutdown: {error}", file=sys.stderr)
                return 1
            selection_failed = False
            for item in _only_ids(options.only):
                try:
                    ids = resolve_sessions(selected.login, (item,), here=options.here, recovery=True).session_ids
                    if len(ids) == 1 and _restart_item_running(ids[0]):
                        continue
                    restart_ids.extend(ids)
                except SessionNotFound as error:
                    if not _restart_item_running(item):
                        print(f"shutdown: {error}", file=sys.stderr)
                        selection_failed = True
                except SessionSelectionError as error:
                    print(f"shutdown: {error}", file=sys.stderr)
                    selection_failed = True
                except (InvalidRecord, InvalidInventory, OSError) as error:
                    print(f"shutdown: {error}", file=sys.stderr)
                    return 3
            if not restart_ids:
                return 1 if selection_failed else 0
            requested = restart_work.AccountNamed(selected.login)
        result = restart_work.restart(requested, dry_run=options.dry_run, alerts=alerts, only=restart_ids, here=options.here)
        return max(result, int(selection_failed))

    if options.command == "holds":
        try:
            print(json.dumps(all_account_holds(), sort_keys=True))
        except InvalidRecord as error:
            print(f"shutdown: {error}", file=sys.stderr)
            return 3
        return 0

    if options.command == "records":
        try:
            reports, lines = _records(options.here)
        except InvalidRecord as error:
            print(f"shutdown: {error}", file=sys.stderr)
            return 3
        if options.as_json:
            print(json.dumps(reports, sort_keys=True))
        elif lines:
            print("\n".join(lines))
        return 0

    try:
        if options.command == "conduct":
            return settle.conduct(options.account or "")

        if options.command == "ready":
            return settle.ready(options.where)

        try:
            selected = _selected_account(options.account)
        except (UnreadableAccount, UnknownAccountName) as error:
            print(f"shutdown: {error}", file=sys.stderr)
            return 2
        if options.command == "follow":
            return follow(selected.login, options.request_id)
        if options.also and (options.also != other_machine() or options.here):
            print(f"shutdown: --also names the other machine ({other_machine()}) and cannot join --here", file=sys.stderr)
            return 2
        ids: tuple[str, ...] = ()
        down_report: Inventory | settle.InventoryNeeded = settle.InventoryNeeded()
        if options.command in {"down", "now", "cancel"} and options.only is not None:
            try:
                resolved = resolve_sessions(selected.login, _only_ids(options.only), here=options.here)
                ids, down_report = resolved.session_ids, resolved.report
            except SessionSelectionError as error:
                print(f"shutdown: {error}", file=sys.stderr)
                return 2
            except (InvalidRecord, InvalidInventory, OSError) as error:
                print(f"shutdown: {error}", file=sys.stderr)
                return 3
        if options.command == "down":
            checkout_move: CheckoutMove | None = None
            if moved := cast(list[str] | None, options.checkout_move):
                if not (options.here and options.restart and len(ids) == 1 and not options.target):
                    print("shutdown: --checkout-move needs --here, --restart and one --only session, without --to", file=sys.stderr)
                    return 2
                checkout_move = {"old": moved[0], "new": moved[1]}
            if isinstance(down_report, settle.InventoryNeeded):
                down_report = inventory(selected.login)
            from scripts.accounts import accounts
            from scripts.accounts.refusals import KnownReset, NoResetKnown
            move_origin: accounts.MoveOrigin = accounts.HandOrigin()
            if options.auto:
                move_origin = accounts.AutoOrigin()
            elif options.refused:
                move_origin = accounts.RefusalOrigin(cast(Literal["weekly", "five_hour", "unknown"], options.refused),
                    KnownReset(options.reset) if options.reset is not None else NoResetKnown())
            return settle.down(
                selected, _stop_here(options, ids), ids,
                then="restart" if options.restart else "stop",
                alerts="none" if options.no_alert else "user",
                requested_by=options.requested_by, request_id=options.request_id,
                target=options.target, why=options.why, report=down_report,
                origin=move_origin, move_id=options.move_id,
                checkout_move=checkout_move,
            )
        if options.command == "now":
            return settle.now(selected, _stop_here(options, ids), ids)
        if options.command == "cancel":
            return settle.cancel(selected, options.here, ids)
        status_ids = resolve_sessions(selected.login, _only_ids(options.only), here=options.here).session_ids if options.only is not None else ()
        reports = _status(selected, options.here, status_ids)
        if options.as_json:
            print(json.dumps(reports[0] if options.here else reports, sort_keys=True))
        else:
            print(f"{selected.label} · {selected.login}")
            observations = tuple(report for report in reports if "sessions" in report)
            for index, report in enumerate(reports):
                if "sessions" in report:
                    print("\n".join(_inventory_lines(report)))
                elif "unreachable" in report:
                    print(f"{report['machine']}: unreachable")
                else:
                    print(f"{report['machine']}: unavailable (rc {report['rc']})")
                outcome = (_local_shutdown_records(selected.login, report["machine"]) if index == 0
                           else _remote_shutdown_records(selected.login))
                if status_ids and outcome["kind"] == "records":
                    outcome["records"] = [saved for saved in outcome["records"]
                                          if saved["session"]["session_id"] in status_ids]
                record_lines = _shutdown_record_lines(outcome, observations, local_observation=index == 0)
                if outcome["kind"] == "records":
                    waiting = any(saved["progress"]["kind"] in {"waiting", "ready", "held by usage limit", "reaping"} for saved in outcome["records"])
                    ownership = (conductor_status(selected.login) if index == 0 else _remote_conductor_status(selected.login)) if waiting else ConductorNotRunning()
                    record_lines.extend(_status_actions(outcome["records"], ownership, observations))
                if record_lines:
                    print("\n".join(record_lines))
                if index == 0:
                    for line in _snapshot_outcome_lines(_snapshot_status()):
                        print(line)
                    for line in _unresolved_snapshot_lines(selected.login, status_ids):
                        print(line)
        return 0
    except SessionSelectionError as error:
        print(f"shutdown: {error}", file=sys.stderr)
        return 2
    except (InvalidRecord, InvalidInventory, OSError) as error:
        print(f"shutdown: {error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
