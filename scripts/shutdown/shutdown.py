#!/usr/bin/env python3
"""Inspect and, in later phases, stop or restart one Claude account's sessions."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal, TypedDict, cast
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parents[1]
for dependency in ("whoami", "message", "production"):
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
    parse_inventory,
)
from record import (
    InvalidRecord,
    LiveShutdownRecord,
    NoLiveRecord,
    NoShutdown,
    ShutdownRecord,
    ShutdownInProgress,
    StopTiming,
    find_live,
    live_records,
    parse_records,
)
from remote import other_machine, run_remote
import settle
import stop as stop_work


class CommandLine(argparse.Namespace):
    command: str = ""
    account: str | None = None
    as_json: bool = False
    here: bool = False
    only: str | None = None
    requested_by: str | None = None
    message: list[str] | None = None
    where: str = ""
    force: str = ""


class UnreachableMachine(TypedDict):
    machine: str
    unreachable: Literal[True]


class UnavailableMachine(TypedDict):
    machine: str
    status: Literal["unavailable"]
    rc: int
    reason: str


MachineStatusReport = Inventory | UnreachableMachine | UnavailableMachine
MachineRecordReport = ShutdownRecord | UnreachableMachine | UnavailableMachine


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


LocalShutdownRecordOutcome = LiveShutdownRecord | NoShutdown | UnreadableShutdownRecord
RemoteShutdownRecordOutcome = (
    LiveShutdownRecord
    | NoShutdown
    | UnreadableShutdownRecord
    | UnreachableShutdownRecord
    | UnavailableShutdownRecord
)


def _selected_account(requested: str | None) -> Account:
    return own_claude_account() if requested is None else named_claude_account(requested)


def _host_text(host: Host) -> str:
    match host["kind"]:
        case "unit":
            return f"unit {host['production']}/{host['unit']}"
        case "tmux":
            return f"tmux {host['tmux_session']}"
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
            "busy: " + ", ".join(server["busy_seats"])
            if server["busy_seats"]
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


def _status(
    selected: Account, here: bool
) -> tuple[list[MachineStatusReport], list[str]]:
    local = inventory(selected.login)
    reports: list[MachineStatusReport] = [local]
    lines = _inventory_lines(local)
    if here:
        return reports, lines

    status, output = run_remote(["status", selected.login, "--json", "--here"])
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
        remote = parse_inventory(output)
    except InvalidInventory as error:
        reports.append(_unavailable(machine, status, str(error)))
        lines.append(f"{machine}: unavailable (rc {status})")
        return reports, lines
    reports.append(remote)
    lines.extend(_inventory_lines(remote))
    return reports, lines


def _local_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return parsed.astimezone(ZoneInfo("America/Los_Angeles")).strftime(
        "%Y-%m-%d %H:%M:%S %Z"
    )


def _record_line(record: ShutdownRecord) -> str:
    return (
        f"{record['machine']} {record['label']} {record['state']} "
        f"since {_local_time(record['requested_at'])}"
    )


def _local_shutdown_record(
    login: str, machine: str
) -> LocalShutdownRecordOutcome:
    try:
        return find_live(login)
    except InvalidRecord as error:
        return {"kind": "unreadable", "machine": machine, "reason": str(error)}


def _remote_shutdown_record(login: str) -> RemoteShutdownRecordOutcome:
    machine = other_machine()
    status, output = run_remote(["records", "--json", "--here"])
    if status == 255:
        return {"kind": "unreachable", "machine": machine}
    if status != 0:
        return {"kind": "unavailable", "machine": machine, "rc": status}
    try:
        records = parse_records(output)
    except InvalidRecord as error:
        return {"kind": "unreadable", "machine": machine, "reason": str(error)}
    for record in records:
        if record["login"] == login:
            return {"kind": "live", "record": record}
    return {"kind": "no shutdown"}


def _shutdown_record_lines(
    outcome: LocalShutdownRecordOutcome | RemoteShutdownRecordOutcome,
) -> list[str]:
    match outcome["kind"]:
        case "live":
            return settle.status_record_lines(outcome["record"])
        case "no shutdown":
            return []
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


def _status_record_lines(
    selected: Account, here: bool, local_machine: str
) -> list[str]:
    lines = _shutdown_record_lines(
        _local_shutdown_record(selected.login, local_machine)
    )
    if not here:
        lines.extend(_shutdown_record_lines(_remote_shutdown_record(selected.login)))
    return lines


def _only_ids(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(dict.fromkeys(item for item in value.split(",") if item))


def _records(here: bool) -> tuple[list[MachineRecordReport], list[str]]:
    local = live_records()
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
        remote = parse_records(output)
    except InvalidRecord as error:
        reports.append(_unavailable(machine, status, str(error)))
        lines.append(f"{machine}: unavailable (rc {status})")
        return reports, lines
    reports.extend(remote)
    lines.extend(_record_line(item) for item in remote)
    return reports, lines


def held_session_ids() -> list[str]:
    """Return sessions whose run folders must survive shutdown and restart."""
    held_states = {
        "settling",
        "stopping",
        "down",
        "stop partial",
        "restarting",
        "restart partial",
    }
    return sorted(
        {
            entry["session"]["session_id"]
            for record in live_records()
            if record["state"] in held_states
            for entry in record["entries"]
        }
    )


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(
        dest="command", required=True, metavar="{down,status,now,cancel}"
    )
    down = commands.add_parser("down", help="start a safe account shutdown")
    _ = down.add_argument("account", nargs="?")
    _ = down.add_argument("--here", action="store_true")
    _ = down.add_argument("--only", help=argparse.SUPPRESS)
    status = commands.add_parser("status", help="show what runs for one Claude account")
    _ = status.add_argument("account", nargs="?")
    _ = status.add_argument("--json", action="store_true", dest="as_json")
    _ = status.add_argument("--here", action="store_true")
    records = commands.add_parser("records")
    _ = records.add_argument("--json", action="store_true", dest="as_json")
    _ = records.add_argument("--here", action="store_true")
    begin = commands.add_parser("begin")
    _ = begin.add_argument("account")
    _ = begin.add_argument("--requested-by")
    _ = begin.add_argument("--only", help=argparse.SUPPRESS)
    refresh = commands.add_parser("refresh")
    _ = refresh.add_argument("account")
    _ = refresh.add_argument("--message", nargs="*")
    conduct = commands.add_parser("conduct")
    _ = conduct.add_argument("account")
    _ = conduct.add_argument("--here", action="store_true")
    ready = commands.add_parser("ready")
    _ = ready.add_argument("--where", required=True)
    cancel = commands.add_parser("cancel", help="undo a settling shutdown")
    _ = cancel.add_argument("account", nargs="?")
    _ = cancel.add_argument("--here", action="store_true")
    now = commands.add_parser("now", help="stop without waiting for idle sessions")
    _ = now.add_argument("account", nargs="?")
    _ = now.add_argument("--here", action="store_true", help=argparse.SUPPRESS)
    stop = commands.add_parser("stop")
    _ = stop.add_argument("account")
    claim_stop = commands.add_parser("claim-stop")
    _ = claim_stop.add_argument("account")
    _ = claim_stop.add_argument(
        "--force", required=True, choices=("wait for ready", "now")
    )
    _ = commands.add_parser("held-sessions")
    options = cast(CommandLine, parser.parse_args(arguments))

    if options.command == "held-sessions":
        try:
            lines = held_session_ids()
        except (InvalidRecord, OSError, ValueError) as error:
            print(f"shutdown: {error}", file=sys.stderr)
            return 3
        if lines:
            print("\n".join(lines))
        return 0

    if options.command == "stop":
        try:
            report = stop_work.stop(options.account or "")
        except (NoLiveRecord, InvalidRecord, OSError, RuntimeError, ValueError) as error:
            print(f"shutdown: {error}", file=sys.stderr)
            return 1
        print(json.dumps(report, sort_keys=True))
        return 0

    if options.command == "claim-stop":
        force = cast(StopTiming, options.force)
        return 0 if stop_work.claim_stop(options.account or "", force) else 1

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

    if options.command == "begin":
        try:
            _ = settle.begin(
                options.account or "",
                options.requested_by,
                _only_ids(options.only),
            )
        except (ShutdownInProgress, ValueError, RuntimeError) as error:
            print(f"shutdown: {error}", file=sys.stderr)
            return 1
        return 0

    if options.command == "refresh":
        try:
            refreshed = settle.refresh(
                options.account or "", options.message or ()
            )
        except (NoLiveRecord, ValueError, RuntimeError) as error:
            print(f"shutdown: {error}", file=sys.stderr)
            return 1
        print(json.dumps([refreshed], sort_keys=True))
        return 0

    if options.command == "conduct":
        return settle.conduct(options.account or "", options.here)

    if options.command == "ready":
        return settle.ready(options.where)

    try:
        selected = _selected_account(options.account)
    except (UnreadableAccount, UnknownAccountName) as error:
        print(f"shutdown: {error}", file=sys.stderr)
        return 2
    if options.command == "down":
        return settle.down(selected, options.here, _only_ids(options.only))
    if options.command == "now":
        return settle.now(selected, options.here)
    if options.command == "cancel":
        return settle.cancel(selected, options.here)
    reports, lines = _status(selected, options.here)
    if options.as_json:
        print(json.dumps(reports[0] if options.here else reports, sort_keys=True))
    else:
        print(f"{selected.label} · {selected.login}")
        print("\n".join(lines))
        record_lines = _status_record_lines(
            selected, options.here, reports[0]["machine"]
        )
        if record_lines:
            print("\n".join(record_lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
