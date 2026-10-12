#!/usr/bin/env python3
"""Inspect, stop and restore each Claude session of one account."""

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
from launch_permission import (
    LaunchPurpose,
    NewWorkLaunchPurpose,
    ShutdownUnitRestoreLaunchPurpose,
)
from record import (
    InvalidRecord,
    SessionShutdown,
    ShutdownContinuation,
    account_hold,
    all_account_holds,
    all_session_records,
    find_session,
    parse_session_records,
    session_records,
)
from remote import other_machine, run_remote
import settle
import stop as stop_work
import restart as restart_work


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


def _shutdown_hold_time(value: str) -> str:
    return datetime.fromisoformat(value).astimezone(
        ZoneInfo("America/Los_Angeles")
    ).strftime("%Y-%m-%d %H:%M %Z")


def launch_blocked(selected: Account, purpose: LaunchPurpose) -> int:
    """A stopped session permits its restore; only an account hold blocks new work."""
    try:
        _ = session_records(selected.login)
        hold = account_hold(selected.login)
        if isinstance(purpose, ShutdownUnitRestoreLaunchPurpose):
            restoring = find_session(selected.login, purpose.session_id)
            if restoring is not None:
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
    if hold is None:
        return 1
    hold_time = _shutdown_hold_time(hold["requested_at"])
    print(f"{hold['label']} is held by a shutdown (since {hold_time})")
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
        return {"kind": "records", "records": session_records(login)}
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


def _status_record_lines(
    selected: Account, here: bool, local_machine: str, observations: tuple[Inventory, ...] = (),
) -> list[str]:
    lines = _shutdown_record_lines(
        _local_shutdown_records(selected.login, local_machine), observations,
    )
    if not here:
        lines.extend(_shutdown_record_lines(_remote_shutdown_records(selected.login), observations, local_observation=False))
    return lines


def _only_ids(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(dict.fromkeys(item for item in value.split(",") if item))


def _records(here: bool) -> tuple[list[MachineRecordReport], list[str]]:
    local = all_session_records()
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
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="{down,status,now,cancel,restart,launch-blocked}",
    )
    down = commands.add_parser("down", help="start a safe account shutdown")
    _ = down.add_argument("account", nargs="?")
    _ = down.add_argument("--here", action="store_true")
    _ = down.add_argument("--only", help=argparse.SUPPRESS)
    _ = down.add_argument("--restart", action="store_true")
    _ = down.add_argument("--requested-by", help=argparse.SUPPRESS)
    _ = down.add_argument("--request-id", default="", help=argparse.SUPPRESS)
    _ = down.add_argument("--no-alert", action="store_true", help=argparse.SUPPRESS)
    status = commands.add_parser("status", help="show what runs for one Claude account")
    _ = status.add_argument("account", nargs="?")
    _ = status.add_argument("--json", action="store_true", dest="as_json")
    _ = status.add_argument("--here", action="store_true")
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
    cancel = commands.add_parser("cancel", help="undo a settling shutdown")
    _ = cancel.add_argument("account", nargs="?")
    _ = cancel.add_argument("--here", action="store_true")
    now = commands.add_parser("now", help="stop without waiting for idle sessions")
    _ = now.add_argument("account", nargs="?")
    _ = now.add_argument("--here", action="store_true", help=argparse.SUPPRESS)
    restart = commands.add_parser(
        "restart", help="bring back every session stopped for one account"
    )
    _ = restart.add_argument("account", nargs="?")
    _ = restart.add_argument("--dry-run", action="store_true")
    _ = restart.add_argument(
        "--no-alert", action="store_true", help=argparse.SUPPRESS
    )
    launch_blocked_command = commands.add_parser("launch-blocked")
    _ = launch_blocked_command.add_argument("account", nargs="?")
    _ = launch_blocked_command.add_argument("--restart-of")
    up = commands.add_parser("up")
    _ = up.add_argument("account")
    _ = up.add_argument("--dry-run", action="store_true")
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

    if options.command == "up":
        return restart_work.up(options.account or "", options.dry_run)

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
        return restart_work.restart(
            requested, dry_run=options.dry_run, alerts=alerts
        )

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

    if options.command == "conduct":
        return settle.conduct(options.account or "")

    if options.command == "ready":
        return settle.ready(options.where)

    try:
        selected = _selected_account(options.account)
    except (UnreadableAccount, UnknownAccountName) as error:
        print(f"shutdown: {error}", file=sys.stderr)
        return 2
    if options.command == "down":
        return settle.down(
            selected, options.here, _only_ids(options.only),
            then="restart" if options.restart else "stop",
            alerts="none" if options.no_alert else "user",
            requested_by=options.requested_by, request_id=options.request_id,
        )
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
            selected, options.here, reports[0]["machine"],
            tuple(report for report in reports if "sessions" in report),
        )
        if record_lines:
            print("\n".join(record_lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
