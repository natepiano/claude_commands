#!/usr/bin/env python3
"""Stop a session or restart staff rows on this machine."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import json
import sys
import time
from contextlib import closing
from typing import Literal, TypedDict, cast

from scripts.shutdown import restart, shutdown_record, stop_resume
from scripts.production import staff
from scripts.accounts import accounts
from scripts.whoami import agent_accounts, note_file
from scripts.agents import worker_lifecycle
from scripts.agents import codex_mesh
from scripts.herdr import herdr
from scripts.shutdown.inventory import CheckoutState, Host, InvalidInventory, Inventory, Session, inventory, parse_inventory
from scripts.shutdown.remote import other_machine, run_remote as run_remote
from scripts.shutdown import remote_control
from scripts.whoami.account import Account, UnknownAccountName, UnreadableAccount, named_claude_account, own_claude_account


class CommandLine(argparse.Namespace):
    command: str = ""
    session: str = ""
    account: str | None = None
    to: str | None = None
    why: str = "resumed by name"
    all: bool = False
    dry_run: bool = False
    only: str = ""
    as_json: bool = False
    here: bool = False
    pid: int = 0
    proc_start: str = ""
    who: str = ""


class UnreachableMachine(TypedDict):
    machine: str
    unreachable: Literal[True]

class UnavailableMachine(TypedDict):
    machine: str
    status: Literal["unavailable"]
    rc: int
    reason: str

MachineStatusReport = Inventory | UnreachableMachine | UnavailableMachine


def _selected_account(requested: str | None) -> Account:
    return own_claude_account() if requested is None else named_claude_account(requested)

def _host_text(host: Host) -> str:
    match host["kind"]:
        case "unit":
            return f"unit {host['unit']} (herdr {host['session_name']})"
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

def _session_lines(session: Session, _report: Inventory) -> list[str]:
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


def wait_for_workers(record: staff.StaffSnapshot) -> None:
    """Name local worker turns and wait until each turn ends."""
    names = {str(row.current_session): f"{row.name}: {row.current_session}"
             for row in record.rows if row.kind == "worker"}
    announced: set[str] = set()
    while True:
        waiting: set[str] = set()
        notices: set[str] = set()
        for server in codex_mesh._running_servers():  # pyright: ignore[reportPrivateUsage]
            try:
                with closing(codex_mesh.Client(server.port, "shutdown")) as client:
                    loaded = codex_mesh._loaded_thread_ids(client, time.monotonic() + 10)  # pyright: ignore[reportPrivateUsage]
                    for identity in names.keys() & loaded:
                        state = codex_mesh._read_live_turn(client, identity)  # pyright: ignore[reportPrivateUsage]
                        if isinstance(state, (codex_mesh.ThreadLive, codex_mesh.ThreadActiveWithoutTurn)):
                            waiting.add(names[identity])
                        elif isinstance(state, codex_mesh.ThreadStateUnknown):
                            notices.add(f"{names[identity]}: readiness anomaly: {state.reason}")
            except (ConnectionError, OSError, SystemExit) as error:
                notices.add(f"worker readiness anomaly: {error}")
        for line in sorted((notices | waiting) - announced):
            print(line if line in notices else f"{line}: waiting for worker turn",
                  file=sys.stderr if line in notices else sys.stdout, flush=True)
        announced.update(notices | waiting)
        if not waiting:
            return
        time.sleep(1)


USAGE = """usage: agents COMMAND
  agents help
  agents status [ACCOUNT] [--json] [--here]
  agents resume NAME [--why TEXT] [--to ACCOUNT]
  agents restart [ACCOUNT] [--all] [--dry-run] [--only ID[,ID...]] [--here]
  agents stop SESSION
"""


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if not arguments or arguments in (["help"], ["--help"], ["-h"]):
        print(USAGE, end="")
        return 0
    parser = argparse.ArgumentParser(prog="agents", usage=USAGE.removeprefix("usage: ").rstrip())
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for verb in ("stop", "resume"):
        command = commands.add_parser(verb)
        _ = command.add_argument("session")
        if verb == "resume":
            _ = command.add_argument("--to")
            _ = command.add_argument("--why", default="resumed by name")
    command = commands.add_parser("restart")
    _ = command.add_argument("--all", action="store_true")
    _ = command.add_argument("--dry-run", action="store_true")
    _ = command.add_argument("--only", default="")
    _ = command.add_argument("--here", action="store_true")
    _ = command.add_argument("account", nargs="?")
    command = commands.add_parser("status")
    _ = command.add_argument("account", nargs="?")
    _ = command.add_argument("--json", action="store_true", dest="as_json")
    _ = command.add_argument("--here", action="store_true")
    command = commands.add_parser("records")
    _ = command.add_argument("--json", action="store_true", dest="as_json")
    _ = command.add_argument("--here", action="store_true")
    _ = commands.add_parser("held-sessions")
    command = commands.add_parser("remote-control")
    _ = command.add_argument("session")
    _ = command.add_argument("--pid", type=int, required=True)
    _ = command.add_argument("--proc-start", required=True)
    _ = command.add_argument("--who", required=True)
    _ = command.add_argument("--session", dest="account", required=True)
    options = cast(CommandLine, parser.parse_args(arguments))
    try:
        if options.command == "restart":
            selected_account: restart.AllRestartAccounts | accounts.AccountSelection = restart.AllRestartAccounts()
            if options.account:
                parsed = note_file.parse_account_label(options.account)
                if isinstance(parsed, note_file.NumberedAccountLabel) and parsed.tool == "codex":
                    if not isinstance(note_file.read_note(note_file.note_path(parsed.label)), note_file.Note):
                        raise UnknownAccountName(f"unknown account {parsed.label}")
                    home = agent_accounts.codex_account_home(parsed.label)
                    selected_account = accounts.CodexWorkersAccount(home.label, home.home)
                else:
                    named = named_claude_account(options.account)
                    selected_account = accounts.NamedAccount(named.label, accounts.account_dir(named.label))
            return restart.restart(stop_all=options.all, dry_run=options.dry_run,
                                   only=tuple(item for item in options.only.split(",") if item),
                                   account=selected_account)
        if options.command == "records":
            print(json.dumps(shutdown_record.read()))
            return 0
        if options.command == "held-sessions":
            record = staff.StaffSnapshot(())
            rows: list[staff.StaffRow] = []
            try:
                record = staff.initial_snapshot()
                rows = restart.live_rows(record, herdr.Herdr(herdr.shared_session()), save_tabs=True)
            except (OSError, ValueError, staff.StaffRefused, herdr.HerdrFailure) as error:
                print(f"restart: save anomaly: {error}", file=sys.stderr)
            shutdown_record.update(started=[], pending=[str(row.id) for row in rows])
            print(f"saved {len(rows)} rows for restart at {time.strftime('%H:%M')}", flush=True)
            wait_for_workers(record)
            return 0
        if options.command == "remote-control":
            result = remote_control.fix(options.session, options.pid, options.proc_start,
                                        options.who, options.account or "")
            return int(isinstance(result, remote_control.Failed))
        if options.command == "status":
            selected = _selected_account(options.account)
            reports = _status(selected, options.here)
            if options.as_json:
                print(json.dumps(reports[0] if options.here else reports, sort_keys=True))
            else:
                print(f"{selected.label} · {selected.login}")
                for report in reports:
                    if "sessions" in report:
                        print("\n".join(_inventory_lines(report)))
                    elif "unreachable" in report:
                        print(f"{report['machine']}: unreachable")
                    else:
                        print(f"{report['machine']}: unavailable (rc {report['rc']})")
            return 0
        if options.command == "stop":
            record = staff.initial_snapshot()
            row = record.by_member_reference(options.session)
            unresolved = stop_resume.stop_unit_workers(row.id, record) if row is not None and row.kind in ("unit", "showrunner") else []
            result = stop_resume.stop(options.session, deadline=worker_lifecycle.effective_deadline())
        else:
            unresolved = []
            result = stop_resume.resume(options.session, why=options.why, to=options.to)
        if isinstance(result, stop_resume.SessionNotFound):
            print(f"{result.name}: no session on this machine")
            return 2
        if isinstance(result, (stop_resume.StopFailed, stop_resume.ResumeFailed)):
            print(f"{result.session_id}: {result.reason}", file=sys.stderr)
            return 1
        if isinstance(result, stop_resume.ClaudeUp):
            print(stop_resume.claude_up_line(result))
        elif isinstance(result, stop_resume.Stopped):
            print(f"{result.session_id}: stopped pid {result.pid}")
        else:
            print(f"{result.session_id}: unchanged")
        return int(bool(unresolved))
    except (OSError, ValueError, staff.StaffNotFound, UnknownAccountName, UnreadableAccount, InvalidInventory) as error:
        print(f"shutdown: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
