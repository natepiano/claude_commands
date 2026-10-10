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
from record import InvalidRecord, Record, live_records, parse_records
from remote import other_machine, run_remote


class CommandLine(argparse.Namespace):
    command: str = ""
    account: str | None = None
    as_json: bool = False
    here: bool = False


class UnreachableMachine(TypedDict):
    machine: str
    unreachable: Literal[True]


class UnavailableMachine(TypedDict):
    machine: str
    status: Literal["unavailable"]
    rc: int
    reason: str


MachineStatusReport = Inventory | UnreachableMachine | UnavailableMachine
MachineRecordReport = Record | UnreachableMachine | UnavailableMachine


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


def _record_line(record: Record) -> str:
    return (
        f"{record['machine']} {record['label']} {record['state']} "
        f"since {_local_time(record['requested_at'])}"
    )


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


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(
        dest="command", required=True, metavar="{status}"
    )
    status = commands.add_parser("status", help="show what runs for one Claude account")
    _ = status.add_argument("account", nargs="?")
    _ = status.add_argument("--json", action="store_true", dest="as_json")
    _ = status.add_argument("--here", action="store_true")
    records = commands.add_parser("records")
    _ = records.add_argument("--json", action="store_true", dest="as_json")
    _ = records.add_argument("--here", action="store_true")
    options = cast(CommandLine, parser.parse_args(arguments))

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
        selected = _selected_account(options.account)
    except (UnreadableAccount, UnknownAccountName) as error:
        print(f"shutdown: {error}", file=sys.stderr)
        return 2
    reports, lines = _status(selected, options.here)
    if options.as_json:
        print(json.dumps(reports[0] if options.here else reports, sort_keys=True))
    else:
        print(f"{selected.label} · {selected.login}")
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
