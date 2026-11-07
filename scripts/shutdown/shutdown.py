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
from typing import Literal, TypedDict

from scripts.shutdown import restart, restart_support, shutdown_record, stop_resume
from scripts.production import staff
from scripts.agents import worker_lifecycle
from scripts.agents import codex_mesh
from scripts.herdr import herdr
from scripts.shutdown.inventory import CheckoutState, Host, InvalidInventory, Inventory, Session, inventory, parse_inventory
from scripts.shutdown.remote import other_machine, run_remote as run_remote
from scripts.shutdown import remote_control
from scripts.shutdown.stop_results import StopResult, ResumeResult
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
    production: str = ""
    excluded: list[str] = []
    finish_step: bool = False


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
  agents pause NAME [--finish-step] --why TEXT
  agents exit NAME [--finish-step] --why TEXT
  agents pause --production SHOWRUNNER [--except UNIT ...] [--finish-step] --why TEXT
  agents resume NAME [--why TEXT] [--to ACCOUNT]
  agents resume --production SHOWRUNNER
  agents restart [ACCOUNT] [--all] [--dry-run] [--only ID[,ID...]] [--here]
  agents stop SESSION [--dry-run] | --all [--dry-run]
"""


def _result_code(result: StopResult | ResumeResult) -> int:
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
    return 0


def _message(row: staff.StaffRow, text: str) -> restart_support.MessageSendOutcome:
    address = "codex:" if isinstance(staff.backend(row), staff.CodexBackend) else "session:"
    try:
        result = restart_support.send_message(address + row.current_session, "agents lifecycle", text)
    except (OSError, ValueError, RuntimeError) as error:
        result = restart_support.MessageUnconfirmed(kind="unconfirmed", reason=str(error))
    if not restart_support.message_was_delivered(result):
        print(f"anomaly: {row.name}: continuation delivery: {result}", file=sys.stderr)
    return result


def _finish_tasks(row: staff.StaffRow, record: staff.StaffSnapshot) -> None:
    from scripts.lib.resume_message import BusyPass
    from scripts.production import worker

    children = record.find(kind="worker", parent=row.id)
    while any(isinstance(worker.own_pass(child, record), BusyPass) for child in children):
        time.sleep(1)
    wait_for_workers(staff.StaffSnapshot(tuple(children)))


def _lifecycle(options: CommandLine) -> int:
    from scripts.production import session_staff

    try:
        record = staff.initial_snapshot()
    except (OSError, ValueError, staff.StaffRefused) as error:
        print(f"anomaly: staff hold: {error}", file=sys.stderr)
        record = staff.StaffSnapshot(())
    if options.production:
        production = session_staff.production_members(record).get(options.production, ())
        excluded = session_staff.excluded_unit_ids(production, options.excluded, options.production)
        rows = [row for row in production if row.id not in excluded]
    else:
        row = record.by_member_reference(options.session) or record.by_path(options.session)
        matches = [member for member in record.rows if member.kind == "unit" and member.name == options.session]
        rows = [row] if row is not None else matches if len(matches) == 1 else []
        if not rows:
            if options.command == "resume":
                return _result_code(stop_resume.resume(options.session, why=options.why, to=options.to))
            raise staff.StaffNotFound(staff.StaffId(options.session))
    code = 0
    for row in rows:
        if options.command == "resume":
            if isinstance(row.paused, staff.Paused):
                _ = staff.resume(str(row.id))
                delivery = _message(row, "Continue where you stopped. Resume your existing workers. Do not open new workers.")
                if restart_support.message_was_delivered(delivery):
                    print(f"{row.name}: active")
                else:
                    _ = staff.set_hold(str(row.id), row.paused)
                    code = 1
            else:
                result = stop_resume.resume(str(row.id), why=options.why, to=options.to)
                if isinstance(result, stop_resume.ClaudeUp) and isinstance(row.exited, staff.Exited):
                    _ = staff.resume(str(row.id))
                code = max(code, _result_code(result))
            continue
        held = staff.pause(str(row.id), options.why) if options.command == "pause" else staff.exit(str(row.id), options.why)
        if options.finish_step:
            _finish_tasks(row, record)
        unresolved = stop_resume.stop_unit_workers(row.id, record)
        code = max(code, int(bool(unresolved)))
        if isinstance(held.exited, staff.Exited):
            code = max(code, _result_code(stop_resume.stop(str(row.id), deadline=worker_lifecycle.effective_deadline())))
        else:
            _ = _message(row, "Finish your current turn. Hold at the prompt. Do not start new work. Wait for agents resume.")
        print(f"{row.name}: {'exited' if isinstance(held.exited, staff.Exited) else 'paused'}")
    return code


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if not arguments or arguments in (["help"], ["--help"], ["-h"]):
        print(USAGE, end="")
        return 0
    parser = argparse.ArgumentParser(prog="agents", usage=USAGE.removeprefix("usage: ").rstrip())
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    command = commands.add_parser("stop")
    selection = command.add_mutually_exclusive_group(required=True)
    _ = selection.add_argument("session", nargs="?")
    _ = selection.add_argument("--all", action="store_true")
    _ = command.add_argument("--dry-run", action="store_true")
    for verb in ("pause", "exit", "resume"):
        command = commands.add_parser(verb)
        selection = command.add_mutually_exclusive_group(required=True)
        _ = selection.add_argument("session", nargs="?")
        _ = selection.add_argument("--production", default="")
        if verb == "resume":
            _ = command.add_argument("--to")
            _ = command.add_argument("--why", default="resumed by name")
        else:
            _ = command.add_argument("--why", required=True)
            _ = command.add_argument("--finish-step", action="store_true")
            _ = command.add_argument("--except", dest="excluded", action="append", default=[])
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
    options = parser.parse_args(arguments, namespace=CommandLine())
    try:
        if options.command == "stop" and options.all:
            return restart.stop_all(dry_run=options.dry_run)
        if options.command == "restart":
            return restart.restart(stop_all=options.all, dry_run=options.dry_run,
                                   only=tuple(item for item in options.only.split(",") if item))
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
        if options.command in ("pause", "exit", "resume"):
            return _lifecycle(options)
        record = staff.initial_snapshot()
        row = record.by_member_reference(options.session) or record.by_path(options.session)
        matches = [member for member in record.rows if member.name == options.session]
        row = row or (matches[0] if len(matches) == 1 else None)
        if options.dry_run:
            if row is not None and row.kind in ("unit", "showrunner"):
                for child in record.find(kind="worker", parent=row.id):
                    print(f"{child.name}: would stop")
            print(f"{row.name if row is not None else options.session}: would stop")
            return 0
        unresolved = stop_resume.stop_unit_workers(row.id, record) if row is not None and row.kind in ("unit", "showrunner") else []
        result = stop_resume.stop(options.session, deadline=worker_lifecycle.effective_deadline())
        return max(_result_code(result), int(bool(unresolved)))
    except staff.StaffRefused as error:
        print(f"shutdown: StaffRefused: {error}", file=sys.stderr)
        return 2
    except (OSError, ValueError, staff.StaffNotFound, UnknownAccountName, UnreadableAccount, InvalidInventory) as error:
        print(f"shutdown: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
