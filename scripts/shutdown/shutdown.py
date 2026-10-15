#!/usr/bin/env python3
"""Inspect, stop and restore each Claude session of one account."""

from __future__ import annotations

import argparse
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
from launch_permission import (
    LaunchPurpose,
    NewWorkLaunchPurpose,
    ShutdownUnitRestoreLaunchPurpose,
    shutdown_state_root,
)
from record import (
    InvalidRecord,
    SessionShutdown,
    ShutdownContinuation,
    AccountHeld,
    ShutdownFound,
    ConductorRunning,
    ConductorNotRunning,
    ConductorUnavailable,
    conductor_status,
    lookup_account_hold,
    lookup_session,
    all_account_holds,
    all_session_records,
    parse_session_records,
    session_records,
)
from remote import other_machine, run_remote
import settle
import stop as stop_work
import restart as restart_work
import remote_control
from user_zone import user_zone


class CommandLine(argparse.Namespace):
    command: str = ""
    account: str | None = None
    as_json: bool = False
    here: bool = False
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
    session_id: str = ""
    pid: int = 0
    proc_start: str = ""
    who: str = ""
    alerts: Literal["user", "none"] = "none"
    then: ShutdownContinuation = "stop"


class UnreachableMachine(TypedDict):
    machine: str
    unreachable: Literal[True]


class UnavailableMachine(TypedDict):
    machine: str
    status: Literal["unavailable"]
    rc: int
    reason: str


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
            if host["session_host"] == "herdr":
                return f"unit {host['production']}/{host['unit']} (herdr {host['session_name']})"
            return f"unit {host['production']}/{host['unit']}"
        case "tmux":
            return f"tmux {host['tmux_session']}"
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


def _session_lines(session: Session) -> list[str]:
    summary = f"  {session['kind']} {session['name']} · {_host_text(session['host'])}"
    summary += f" · {session['status']} · {_checkout_text(session['checkout'])}"
    lines = [summary]
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
        lines.extend(_session_lines(session))
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


def _status(selected: Account, here: bool) -> list[MachineStatusReport]:
    local = inventory(selected.login)
    reports: list[MachineStatusReport] = [local]
    if here:
        return reports
    status, output = run_remote(["status", selected.login, "--json", "--here"])
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


def launch_blocked(selected: Account, purpose: LaunchPurpose) -> int:
    """A stopped session permits its restore; only an account hold blocks new work."""
    try:
        _ = session_records(selected.login)
        hold = lookup_account_hold(selected.login)
        if isinstance(purpose, ShutdownUnitRestoreLaunchPurpose):
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
    except (InvalidRecord, OSError, ValueError) as error:
        print(f"shutdown state unreadable: {error}", file=sys.stderr)
        return 3
    if not isinstance(hold, AccountHeld):
        return 1
    hold_time = _shutdown_hold_time(hold.hold["requested_at"])
    print(f"{hold.hold['label']} is held by a shutdown (since {hold_time})")
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
            return [
                line
                for record in outcome["records"]
                for line in settle.status_record_lines(record, _observed_owner(record, observations), local_observation=local_observation)
            ]
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


@dataclass(frozen=True)
class ResolvedSessions:
    session_ids: tuple[str, ...]
    report: Inventory


class SessionSelectionError(ValueError):
    """The requested items do not identify exactly one account session."""


def resolve_sessions(login: str, items: Iterable[str], *, here: bool = False) -> ResolvedSessions:
    """Resolve names from membership, then build one filtered inventory."""
    requested = list(dict.fromkeys(items))
    if not requested or any(not item for item in requested):
        raise SessionSelectionError("--only needs a session id or exact name")
    local_machine = settle.local_machine()
    local: dict[str, _SelectableSession] = {}
    for saved in session_records(login):
        owner = saved["session"]
        local[owner["session_id"]] = _SelectableSession(owner["session_id"], owner["name"], saved["machine"])
    for owner in listing(login):
        if owner.kind != "seat":
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
                _ = candidates.setdefault(owner["session_id"], _SelectableSession(owner["session_id"], owner["name"], saved["machine"]))
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
                        if owner["kind"] != "seat":
                            _ = candidates.setdefault(owner["session_id"], _SelectableSession(owner["session_id"], owner["name"], report["machine"]))
        if peer_unavailable:
            print(f"{other_machine()}: not reached; names checked on {local_machine} only", file=sys.stderr)
    result: list[str] = []
    for item in requested:
        if item in local:
            matches = [local[item]]
        elif item in candidates:
            matches = [candidates[item]]
        else:
            matches = [session for session in candidates.values() if session.name == item]
        if len(matches) != 1:
            reason = "matches no session" if not matches else "matches more than one session"
            missing = f"; {other_machine()} not reached" if peer_unavailable else ""
            listed = matches or list(candidates.values())
            lines = [f"{item}: {reason}{missing}"]
            lines.extend(f"  {session.machine}: {session.name} ({session.session_id})" for session in listed)
            raise SessionSelectionError("\n".join(lines))
        if matches[0].session_id not in result:
            result.append(matches[0].session_id)
    return ResolvedSessions(tuple(result), inventory(login, only=frozenset(result)))


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


def _status_actions(records: list[SessionShutdown], ownership: ConductorRunning | ConductorNotRunning | ConductorUnavailable) -> list[str]:
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
    if progress["kind"] == "manual restart":
        return f"{saved['session']['name']}: {place}: {progress['command']}"
    verb = "now" if kind in {"stop failed", "waiting", "stopped", "already gone"} else "restart"
    command = settle.shutdown_command(verb, saved["login"], "--here", "--only", saved["session"]["session_id"])
    if kind == "process identity lost":
        return f"{saved['session']['name']}: process identity lost; inspect its window before restarting; {place}: {command}"
    if kind == "waiting":
        return settle.holdout_line(saved)
    return f"{saved['session']['name']}: {kind}; {place}: {command}"


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
            for session in [record["session"], *record["seats"]]
        }
    )


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        usage="in this terminal: ~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py <verb> [arguments]",
        epilog="Run in this terminal: ~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py <verb> <account> [--only ID[,ID...]].\nFollow in this terminal: ~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py follow <account> --request <request-id>.\nInternal fixer in this terminal: ~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py remote-control <session-id> --pid <pid> --proc-start <start> --who <name> --alerts <user|none>.",
    )
    commands = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="{down,status,follow,now,cancel,restart,remote-control,launch-blocked}",
    )
    down = commands.add_parser("down", help="start a safe account shutdown")
    _ = down.add_argument("account", nargs="?")
    _ = down.add_argument("--here", action="store_true")
    _ = down.add_argument("--only", metavar="NAME[,NAME...]", help="session ids or exact names, separated by commas")
    _ = down.add_argument("--restart", action="store_true")
    _ = down.add_argument("--to", dest="target", default="", help="resume on default or claude N")
    _ = down.add_argument("--why", default="", help="reason for the account move")
    _ = down.add_argument("--requested-by", help=argparse.SUPPRESS)
    _ = down.add_argument("--request-id", default="", help=argparse.SUPPRESS)
    _ = down.add_argument("--no-alert", action="store_true", help=argparse.SUPPRESS)
    status = commands.add_parser("status", help="show what runs for one Claude account")
    _ = status.add_argument("account", nargs="?")
    _ = status.add_argument("--json", action="store_true", dest="as_json")
    _ = status.add_argument("--here", action="store_true")
    follow_command = commands.add_parser("follow", help="follow one local shutdown request")
    _ = follow_command.add_argument("account")
    _ = follow_command.add_argument("--request", dest="request_id", required=True)
    control = commands.add_parser("remote-control", help="enable Remote Control for one restarted process")
    _ = control.add_argument("session_id")
    _ = control.add_argument("--pid", type=int, required=True)
    _ = control.add_argument("--proc-start", required=True)
    _ = control.add_argument("--who", required=True)
    _ = control.add_argument("--alerts", choices=("user", "none"), required=True)
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
    _ = now.add_argument("--only", metavar="NAME[,NAME...]", help="session ids or exact names, separated by commas")
    restart = commands.add_parser(
        "restart", help="bring back every session stopped for one account"
    )
    _ = restart.add_argument("account", nargs="?")
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
    _ = up.add_argument("--no-alert", action="store_true", help=argparse.SUPPRESS)
    ownership = commands.add_parser("conductor-status")
    _ = ownership.add_argument("account")
    _ = ownership.add_argument("--json", action="store_true", dest="as_json")
    _ = ownership.add_argument("--here", action="store_true")
    _ = commands.add_parser("held-sessions")
    options = cast(CommandLine, parser.parse_args(arguments))

    if options.command == "remote-control":
        if options.pid <= 0 or not options.proc_start:
            parser.error("remote-control requires a positive pid and nonempty process start")
        outcome = remote_control.fix(options.session_id, options.pid, options.proc_start, options.who, options.alerts)
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
                ShutdownUnitRestoreLaunchPurpose(options.restart_of)
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
            result = restart_work.up_result(options.account or "", options.dry_run, _only_ids(options.only), notify=not options.no_alert)
            print(json.dumps(result, sort_keys=True))
            return 1 if result["outcome"] == "partial" or result["refused"] else 0
        return restart_work.up(options.account or "", options.dry_run, _only_ids(options.only), notify=not options.no_alert)

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
        if options.only is not None:
            try:
                selected = restart_work.select_account(requested)
            except UnknownAccountName as error:
                print(f"shutdown: {error}", file=sys.stderr)
                return 2
            except (restart_work.RestartAccountNotDetermined, UnreadableAccount, InvalidRecord, OSError, ValueError) as error:
                print(f"shutdown: {error}", file=sys.stderr)
                return 1
            try:
                restart_ids = list(resolve_sessions(selected.login, _only_ids(options.only), here=options.here).session_ids)
            except SessionSelectionError as error:
                print(f"shutdown: {error}", file=sys.stderr)
                return 2
            except (InvalidRecord, InvalidInventory, OSError) as error:
                print(f"shutdown: {error}", file=sys.stderr)
                return 3
            requested = restart_work.AccountNamed(selected.login)
        return restart_work.restart(requested, dry_run=options.dry_run, alerts=alerts, only=restart_ids, here=options.here)

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
            if isinstance(down_report, settle.InventoryNeeded):
                down_report = inventory(selected.login)
            return settle.down(
                selected, options.here, ids,
                then="restart" if options.restart else "stop",
                alerts="none" if options.no_alert else "user",
                requested_by=options.requested_by, request_id=options.request_id,
                target=options.target, why=options.why, report=down_report,
            )
        if options.command == "now":
            return settle.now(selected, options.here, ids)
        if options.command == "cancel":
            return settle.cancel(selected, options.here, ids)
        reports = _status(selected, options.here)
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
                record_lines = _shutdown_record_lines(outcome, observations, local_observation=index == 0)
                if outcome["kind"] == "records":
                    waiting = any(saved["progress"]["kind"] in {"waiting", "ready", "held by usage limit", "reaping"} for saved in outcome["records"])
                    ownership = (conductor_status(selected.login) if index == 0 else _remote_conductor_status(selected.login)) if waiting else ConductorNotRunning()
                    record_lines.extend(_status_actions(outcome["records"], ownership))
                if record_lines:
                    print("\n".join(record_lines))
        return 0
    except InvalidRecord as error:
        print(f"shutdown: {error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
