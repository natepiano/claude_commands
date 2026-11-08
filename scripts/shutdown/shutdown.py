#!/usr/bin/env python3
"""Show, pause, exit, retire, resume, stop, or restart sessions."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Literal, TypedDict, NamedTuple, cast

from scripts.accounts import accounts
from scripts.agents import worker_lifecycle
from scripts.copies import copies
from scripts.direct import progress_history
from scripts.herdr import herdr, worker_panes
from scripts.lib import process_group, conf, berth_board, unit_runs
from scripts.lib.conf import UseConfiguredValue
from scripts.message import sessions
from scripts.production import (
    staff,
    staff_sources,
    unit_config,
    unit_lookup,
    unit_move,
    completion_receipt,
    worker,
    add_unit,
    unit_names,
)
from scripts.production.production_doc import Production, Refusal, read_production
from scripts.shutdown import (
    restart,
    restart_support,
    shutdown_record,
    stop_resume,
    remote_control,
    remote,
    pane_record,
)
from scripts.shutdown.inventory import (
    CheckoutState,
    Host,
    InvalidInventory,
    Inventory,
    Session,
    inventory,
    parse_inventory,
)
from scripts.shutdown.remote import other_machine, run_remote as run_remote, local_machine
from scripts.shutdown.stop_results import StopResult, ResumeResult
from scripts.whoami import agent_accounts, note_file
from scripts.whoami.account import (
    Account,
    UnknownAccountName,
    UnreadableAccount,
    named_claude_account,
    own_claude_account,
)


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
    retired: bool = False


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
    from scripts.herdr import worker_panes
    from scripts.production import session_staff

    parents = session_staff.pane_parents(record)
    announced: set[str] = set()
    while True:
        waiting: set[str] = set()
        notices: set[str] = set()
        states = worker_panes.observe_parents(parents, record)
        for rows in states.values():
            for state in rows:
                label = f"{state.row.name}: {state.row.current_session}"
                if isinstance(state, worker_panes.OpenWorker) and state.status in (herdr.AgentStatus.WORKING, herdr.AgentStatus.RUNNING):
                    waiting.add(label)
                elif isinstance(state, (worker_panes.UncertainWorker, worker_panes.ProcessUnknownWorker)):
                    notices.add(f"{label}: readiness anomaly: {state.reason}")
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
  agents resume NAME [--retired] [--why TEXT] [--to ACCOUNT]
  agents resume --production SHOWRUNNER
  agents retire NAME
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
        matches = [member for member in record.rows if member.kind == "unit" and member.name == options.session]
        row = (record.by_member_reference(options.session)
               or (matches[0] if len(matches) == 1 else record.by_path(options.session)))
        rows = [row] if row is not None else []
        if not rows:
            if options.command == "resume":
                return _result_code(stop_resume.resume(options.session, why=options.why, to=options.to))
            raise staff.StaffNotFound(staff.StaffId(options.session))
    code = 0
    for row in rows:
        if options.command == "resume":
            if isinstance(row.worktree, staff.RemovedWorktree):
                print(f"{row.name}: retired; use agents resume {row.name} --retired", file=sys.stderr)
                code = 2
                continue
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
            _ = command.add_argument("--retired", action="store_true")
            _ = command.add_argument("--to")
            _ = command.add_argument("--why", default="resumed by name")
        else:
            _ = command.add_argument("--why", required=True)
            _ = command.add_argument("--finish-step", action="store_true")
            _ = command.add_argument("--except", dest="excluded", action="append", default=[])
    command = commands.add_parser("retire")
    _ = command.add_argument("session")
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
        if options.command == "retire":
            return retire_named_unit(options.session)
        if options.command == "resume" and options.retired:
            recovery_account = (accounts.NamedAccount(options.to, accounts.account_dir(options.to))
                                if options.to else accounts.CURRENT_ACCOUNT_SETTINGS)
            return recover_named_unit(options.session, account=recovery_account)
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


SCRIPTS = Path(__file__).resolve().parent.parent

DETACHED_RUNS = "mesh_runs"

BUILD_HOLD = SCRIPTS / "build_hold" / "build_hold.py"

MAC_CLAIMS = Path.home() / ".local/state/mac-test/claims"

class RetirementRefused(Exception):
    pass

@dataclass(frozen=True)
class RetiringUnit:
    production: Production
    name: str
    worktree: Path
    branch: str
    staff_member: staff.StaffRow

class MacClaim(NamedTuple):
    path: Path
    what: str
    pid: int
    proc_start: str

def retirement_run(argv: list[str], cwd: Path | None = None, *, settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> subprocess.CompletedProcess[str]:
    """One command, without this session's id: shutdown then finds the account that holds the unit."""
    if isinstance(settings, UseConfiguredValue):
        settings = conf.read("production")
    environment = {key: value for key, value in os.environ.items() if key != "CLAUDE_CODE_SESSION_ID"}
    limit = settings.number("RETIRE_COMMAND_TIMEOUT_SECONDS")
    operation_limit = worker_lifecycle.effective_deadline()
    remaining = operation_limit
    if operation_limit != worker_lifecycle.NO_LOCK_DEADLINE:
        remaining = operation_limit - time.monotonic()
        if remaining <= 0:
            raise worker_lifecycle.OperationDeadlineExpired("retirement operation deadline expired")
    return subprocess.run(argv, pass_fds=staff.operation_fds(), cwd=cwd, env=environment, capture_output=True, text=True, check=False,
                          timeout=min(limit, remaining))

def retirement_failure(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout).strip() or f"exit {result.returncode}"

def retirement_production_of(production_name: str, *, record: staff.StaffSnapshot) -> Production:
    found = staff_sources.member(staff_sources.ShowrunnerMember(production_name), record)
    if isinstance(found, staff_sources.NoSuchMember) or isinstance(found.row.doc, staff.DocUnfilled):
        raise ValueError(f"{production_name}: no showrunner staff document")
    if found.row.doc.machine != local_machine():
        raise ValueError(f"{production_name}: run retire on {found.row.doc.machine}")
    return read_production(found.row)

def retirement_git(unit: RetiringUnit, *arguments: str, settings: conf.ConfigRead) -> subprocess.CompletedProcess[str]:
    return retirement_run(["git", "-C", str(unit.production.checkout), *arguments], settings=settings)

def retirement_recorder_line_only(worktree: Path, path: str, *, settings: conf.ConfigRead) -> bool:
    """Whether the file's whole change against HEAD is one added `Project started` line."""
    diff = retirement_run(["git", "-C", str(worktree), "diff", "--no-color", "--unified=0", "HEAD", "--", path], settings=settings)
    changed = [line for line in diff.stdout.splitlines()
               if line[:1] in ("+", "-") and not line.startswith(("+++ ", "--- "))]
    return (diff.returncode == 0 and len(changed) == 1 and changed[0].startswith("+")
            and progress_history.PROJECT_STARTED_PATTERN.fullmatch(changed[0][1:]) is not None)

def retirement_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

def retirement_proc_start(pid: int) -> str:
    """The process's start time in clock ticks, as /proc/<pid>/stat field 22 gives it."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return ""
    return stat.rsplit(")", 1)[-1].split()[19]

def retirement_json(path: Path) -> dict[str, object]:
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}
    return cast(dict[str, object], value) if isinstance(value, dict) else {}

def retirement_command_line(pid: int, *, settings: conf.ConfigRead) -> list[str]:
    """The process's arguments; empty once it has gone. The Mac has no /proc: ps splits them at spaces."""
    if sys.platform == "darwin":
        return retirement_run(["ps", "-o", "args=", "-p", str(pid)], settings=settings).stdout.split()
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")
    except OSError:
        return []

def retirement_hold_directory() -> Path:
    return Path(os.environ.get("BUILD_HOLD_DIR", str(Path.home() / ".local/state/build-hold")))

def retirement_holds(session_ids: set[str]) -> tuple[str, ...]:
    """The build holds kept under the unit's session ids: build_hold keys a hold by session id."""
    return tuple(sorted(key for key in session_ids if key and (retirement_hold_directory() / key).is_file()))

def retirement_reservations(worktree: Path) -> tuple[str, ...]:
    if not unit_move.exists(worktree):
        return ()
    board = berth_board.read_board(worktree)
    if isinstance(board, berth_board.BoardUnavailable):
        return ()
    return tuple(item.reservation_id for item in board.reservations()
                 if item.holder_worktree_root.resolve() == worktree.resolve())

def retirement_mac_claims(worktree: Path) -> tuple[MacClaim, ...]:
    found: list[MacClaim] = []
    for path in sorted(MAC_CLAIMS.glob("*.json")) if MAC_CLAIMS.is_dir() else ():
        claim = retirement_json(path)
        pid = claim.get("pid")
        if claim.get("worktree") == str(worktree) and isinstance(pid, int) and not isinstance(pid, bool):
            found.append(MacClaim(path, str(claim.get("what", path.stem)), pid, str(claim.get("proc_start", ""))))
    return tuple(found)

def retirement_terminate(pids: list[int], what: dict[int, str], wait: float | None = None, *, settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> list[str]:
    """SIGTERM each pid, observing the configured wait and poll limits."""
    if not pids:
        return []
    if isinstance(settings, UseConfiguredValue):
        settings = conf.read("production")
    duration = settings.number("RETIRE_TERM_WAIT_SECONDS")
    operation_limit = worker_lifecycle.effective_deadline()
    started = time.monotonic()
    if started >= operation_limit:
        raise worker_lifecycle.OperationDeadlineExpired("retirement operation deadline expired")
    poll = settings.number("RETIRE_POLL_SECONDS")
    if wait is not None:
        duration = min(duration, max(0, wait))
    deadline = min(started + duration, operation_limit)
    signalled: set[int] = set()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            signalled.add(pid)
        except ProcessLookupError:
            continue
        except PermissionError as error:
            what[pid] += f": {error}"
    while any(retirement_alive(pid) for pid in pids):
        now = time.monotonic()
        if now >= operation_limit:
            raise worker_lifecycle.OperationDeadlineExpired("retirement operation deadline expired")
        remaining = deadline - now
        if remaining <= 0:
            break
        time.sleep(min(poll, remaining))
    left: list[str] = []
    for pid in pids:
        if retirement_alive(pid):
            left.append(f"{what[pid]}: still runs after SIGTERM")
        elif pid in signalled:
            print(f"stopped: {what[pid]}")
    return left

def retirement_process_description(pid: int, *, settings: conf.ConfigRead) -> str:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        parent = fields[1]
    except (OSError, IndexError):
        parent = "unknown"
    return (f"unowned process {pid}, parent {parent}, command {' '.join(retirement_command_line(pid, settings=settings)).strip() or 'unknown'}, "
            f"start {retirement_proc_start(pid) or 'unknown'}")

def retirement_process_session(pid: int) -> str:
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    for field in raw.split(b"\0"):
        key, _, value = field.partition(b"=")
        if key == b"CLAUDE_CODE_SESSION_ID":
            return value.decode("utf-8")
    return ""

def retirement_stop_orphan(unit: RetiringUnit, pid: int, recorded_session_id: str = "", *, settings: conf.ConfigRead) -> list[str]:
    """Only a proven, isolated group of this stopped session can be ended."""
    duration = settings.number("RETIRE_TERM_WAIT_SECONDS")
    operation_limit = worker_lifecycle.effective_deadline()
    if operation_limit != worker_lifecycle.NO_LOCK_DEADLINE and time.monotonic() >= operation_limit:
        raise worker_lifecycle.OperationDeadlineExpired("retirement operation deadline expired")
    poll = settings.number("RETIRE_POLL_SECONDS")
    description = retirement_process_description(pid, settings=settings)
    try:
        started = retirement_proc_start(pid)
        session_id = recorded_session_id or retirement_last_session(unit)
        if not started or not session_id or retirement_process_session(pid) != session_id:
            raise RetirementRefused("no recorded session ownership")
        observed = sessions.session_processes(session_id)
        if isinstance(observed, sessions.SessionLivenessUnknown) or observed.pids:
            raise RetirementRefused("the recorded session is live or unreadable")
        # A group send must not reach a visitor or the caller. Only a leader whose
        # whole group is positively owned and inside this worktree is eligible.
        if os.getpgid(pid) != pid:
            raise RetirementRefused("process does not lead an isolated group")
        _ = process_group.check_process_group(pid)
        retirement_members: list[tuple[int, str]] = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            member = int(entry.name)
            try:
                if os.getpgid(member) != pid:
                    continue
                cwd = (entry / "cwd").resolve(strict=True)
                if not cwd.is_relative_to(unit.worktree.resolve()) or retirement_process_session(member) != session_id:
                    raise RetirementRefused(f"group member {member} has no worktree/session ownership")
                birth = retirement_proc_start(member)
                if not birth:
                    raise RetirementRefused(f"group member {member} has no start time")
                retirement_members.append((member, birth))
            except ProcessLookupError:
                continue
        if not retirement_members or not any(member == pid for member, _ in retirement_members):
            raise RetirementRefused("process group identity is unreadable")
        observed = sessions.session_processes(session_id)
        if isinstance(observed, sessions.SessionLivenessUnknown) or observed.pids:
            raise RetirementRefused("the recorded session became live or unreadable")
        if any(retirement_proc_start(member) != birth or os.getpgid(member) != pid
               or retirement_process_session(member) != session_id
               or not Path(f"/proc/{member}/cwd").resolve(strict=True).is_relative_to(unit.worktree.resolve())
               for member, birth in retirement_members) or retirement_proc_start(pid) != started:
            raise RetirementRefused("process identity changed before signal")
        if operation_limit != worker_lifecycle.NO_LOCK_DEADLINE and time.monotonic() >= operation_limit:
            raise worker_lifecycle.OperationDeadlineExpired("retirement operation deadline expired")
        process_group.send_process_group(pid, signal.SIGTERM)
    except worker_lifecycle.OperationDeadlineExpired:
        raise
    except ProcessLookupError:
        return []
    except (OSError, ValueError, RetirementRefused) as error:
        return [f"{description} works inside {unit.worktree}; stop it first: {error}"]
    deadline = min(time.monotonic() + duration, operation_limit)
    def still_running() -> bool:
        try:
            state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            return False
        return state != "Z" and retirement_proc_start(pid) == started and retirement_alive(pid)
    while still_running():
        now = time.monotonic()
        if now >= operation_limit:
            raise worker_lifecycle.OperationDeadlineExpired("retirement operation deadline expired")
        remaining = deadline - now
        if remaining <= 0:
            break
        time.sleep(min(poll, remaining))
    if still_running():
        return [f"{description}: still runs after SIGTERM"]
    print(f"stopped: {description} of session {session_id}")
    return []

def retirement_live_claims(claims: tuple[MacClaim, ...]) -> tuple[MacClaim, ...]:
    return tuple(claim for claim in claims if retirement_alive(claim.pid) and retirement_proc_start(claim.pid) == claim.proc_start)

def retirement_runs_of(worktree: Path) -> list[Path]:
    """Read run ownership through the staff row's session identities."""
    record = staff.snapshot()
    owner = record.by_worktree(local_machine(), worktree.resolve())
    if owner is None:
        return []
    found: list[Path] = []
    for root in unit_runs.run_roots(os.environ.get("RETIRE_UNIT_RUN_ROOT") or None):
        for folder in sorted(root.iterdir()) if root.is_dir() else ():
            if folder.name != "active" and record.by_member_reference(unit_runs.director(folder)) == owner:
                found.append(folder)
    return found

def retirement_session_of(pid: int) -> int:
    try:
        return os.getsid(pid)
    except OSError:
        return 0

def retirement_run_processes(worktree: Path, inside: list[int], *, settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> dict[int, str]:
    """Name the detached command processes that remain in the worktree."""
    if isinstance(settings, UseConfiguredValue):
        settings = conf.read("production")
    runs = retirement_runs_of(worktree)
    leaders: dict[int, str] = {}
    for folder in runs:
        for path in sorted((folder / DETACHED_RUNS).glob("*.json")):
            record = retirement_json(path)
            pid = record.get("pid")
            if (record.get("status") == "running" and isinstance(pid, int) and not isinstance(pid, bool)
                    and pid > 0 and str(path) in retirement_command_line(pid, settings=settings)):
                leaders[pid] = f"run supervisor {pid} of {record.get('label', path.stem)} in run {folder.name}"
    return {pid: f"process {pid} ({retirement_command_name(pid)}) of the {leaders[leader]}"
            for pid in inside if (leader := retirement_session_of(pid)) in leaders}

def retirement_command_name(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
    except OSError:
        return "?"

def retirement_find_unit(production_name: str, given: str, *,
              record: staff.StaffSnapshot | None = None,
              settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> RetiringUnit:
    record = staff.snapshot() if record is None else record
    production = retirement_production_of(production_name, record=record)
    member = record.by_id(staff.StaffId(given))
    if member is None or member.kind != "unit":
        selected = staff_sources.member(staff_sources.UnitMember(production.name, given), record)
        if isinstance(selected, staff_sources.NoSuchMember):
            raise ValueError(f"{production.name}/{given}: no matching staff member")
        member = selected.row
    location = unit_lookup.local_worktree(staff_sources.MemberFound(member))
    branch = ""
    if location.path.is_dir():
        result = retirement_run(["git", "-C", str(location.path), "branch", "--show-current"], settings=settings)
        branch = result.stdout.strip()
    return RetiringUnit(production, member.name, location.path, branch, member)

def retirement_last_session(unit: RetiringUnit) -> str:
    return unit.staff_member.current_session

def retirement_refusals(unit: RetiringUnit, *, settings: conf.ConfigRead) -> list[str]:
    """Keep uncommitted work and commits outside the merge branch."""
    if not unit.worktree.is_dir():
        return []
    status = retirement_run(["git", "-C", str(unit.worktree), "status", "--short"], settings=settings)
    if status.returncode:
        raise OSError(f"cannot read {unit.worktree}: {retirement_failure(status)}")
    paths = [line[3:] for line in status.stdout.splitlines()
             if not (line[:2] == " M" and retirement_recorder_line_only(unit.worktree, line[3:], settings=settings))]
    found = [f"uncommitted work in {unit.worktree}: {', '.join(paths)}"] if paths else []
    refs = [f"refs/heads/{unit.branch}"] if unit.branch else ["HEAD"]
    remote = retirement_git(unit, "ls-remote", "--exit-code", "--heads", "origin", unit.branch, settings=settings)
    if remote.returncode == 0:
        fetched = retirement_git(unit, "fetch", "--no-write-fetch-head", "--refmap=", "-q", "origin", unit.branch, settings=settings)
        if fetched.returncode:
            raise OSError(retirement_failure(fetched))
        refs.append(remote.stdout.split()[0])
    for ref in refs:
        commits = retirement_git(unit, "rev-list", "--no-merges", ref, "--not", unit.production.merge_branch, settings=settings)
        if commits.returncode:
            raise OSError(retirement_failure(commits))
        if commits.stdout.strip():
            found.append(f"unmerged commits on {unit.branch or ref}: {commits.stdout.splitlines()[0]}")
            break
    return found

def retirement_members(unit: RetiringUnit, record: staff.StaffSnapshot) -> list[staff.StaffRow]:
    return [unit.staff_member, *record.find(kind="worker", parent=unit.staff_member.id)]

def retirement_remove(unit: RetiringUnit, *, settings: conf.ConfigRead) -> list[str]:
    failed: list[str] = []
    if unit.worktree.exists():
        result = retirement_git(unit, "worktree", "remove", "--force", str(unit.worktree), settings=settings)
        if result.returncode:
            return [retirement_failure(result)]
        print(f"removed: worktree {unit.worktree}")
    if unit.branch:
        if retirement_git(unit, "show-ref", "--verify", "--quiet", f"refs/heads/{unit.branch}", settings=settings).returncode == 0:
            result = retirement_git(unit, "branch", "-D", unit.branch, settings=settings)
            if result.returncode:
                failed.append(retirement_failure(result))
        if retirement_git(unit, "ls-remote", "--exit-code", "--heads", "origin", unit.branch, settings=settings).returncode == 0:
            result = retirement_git(unit, "push", "-q", "origin", "--delete", unit.branch, settings=settings)
            if result.returncode:
                failed.append(retirement_failure(result))
    return failed

def retirement_release_reservations(unit: RetiringUnit, *, settings: conf.ConfigRead) -> None:
    for key in retirement_reservations(unit.worktree):
        result = retirement_run(["cargo-berth", "release", key, "--json"], cwd=unit.worktree, settings=settings)
        if result.returncode and not (result.returncode == 5 and "already has a final disposition" in result.stdout + result.stderr):
            print(f"anomaly: {retirement_failure(result)}")

def retirement_retire(unit: RetiringUnit, *, settings: conf.ConfigRead,
           record: staff.StaffSnapshot | None = None) -> int:
    record = staff.snapshot() if record is None else record
    row = unit.staff_member
    if isinstance(row.worktree, staff.RemovedWorktree) and not unit.worktree.exists():
        try:
            copies.remove_trees(str(row.id))
        except copies.CopyError as error:
            print(f"anomaly: {error}".replace("\n", " "))
        print("unchanged")
        return 0
    refused = retirement_refusals(unit, settings=settings)
    if refused:
        print("\n".join(f"refused: {reason}" for reason in refused))
        return 1
    config = unit_config.read(unit.worktree, cleanup=False)
    if isinstance(config, unit_config.UnitConfig):
        saved = unit_config.read_parked(row.id, cleanup=False)
        facts = (saved.retired if isinstance(saved, unit_config.UnitConfig)
                 and isinstance(saved.retired, unit_config.RetiredSession) else
                 unit_config.RetiredSession(datetime.now(unit.production.zone).date().isoformat(),
                                            row.current_session, unit_config.Branch(unit.branch)))
        _ = unit_config.write_parked(row.id, replace(config, retired=facts))
    owned = retirement_members(unit, record)
    pane_workers = worker_panes.observe(row.id, record)
    pane_ids = {item.row.id for item in pane_workers}
    for member in owned:
        if member.id in pane_ids or isinstance(record.backends.get(member.id), staff.CodexBackend):
            continue
        try:
            outcome = stop_resume.stop(member.current_session, deadline=worker_lifecycle.effective_deadline())
            if isinstance(outcome, stop_resume.StopFailed):
                print(f"anomaly: {member.name}: {outcome.reason}")
        except (OSError, ValueError, herdr.HerdrFailure) as error:
            print(f"anomaly: {member.name}: {' '.join(str(error).splitlines())}")
    stop_reasons = stop_resume.stop_unit_workers(row.id, record)
    for reason in stop_reasons:
        print(f"anomaly: {reason}")
    for key in retirement_holds({member.current_session for member in owned}):
        result = retirement_run([sys.executable, str(BUILD_HOLD), "release", "--holder", key], settings=settings)
        if result.returncode:
            print(f"anomaly: {retirement_failure(result)}")
    retirement_release_reservations(unit, settings=settings)
    claims = retirement_live_claims(retirement_mac_claims(unit.worktree))
    for reason in retirement_terminate([claim.pid for claim in claims], {claim.pid: claim.what for claim in claims}, settings=settings):
        print(f"anomaly: {reason}")
    inside = unit_move.processes_inside(unit.worktree) if unit.worktree.exists() else []
    classified = retirement_run_processes(unit.worktree, inside, settings=settings)
    for reason in retirement_terminate(list(classified), classified, settings=settings):
        print(f"anomaly: {reason}")
    for pid in inside:
        if pid != os.getpid() and pid not in classified:
            for reason in retirement_stop_orphan(unit, pid, row.current_session, settings=settings):
                print(f"anomaly: {reason}")
    # Recheck work after sessions stop, before removing the folder.
    refused = retirement_refusals(unit, settings=settings)
    if refused:
        print("\n".join(f"refused: {reason}" for reason in refused))
        return 1
    remaining = worker_panes.observe(row.id, staff.snapshot()) if pane_workers else []
    if any(isinstance(item, (worker_panes.OpenWorker, worker_panes.UncertainWorker)) for item in remaining):
        return 1
    try:
        copies.remove_trees(str(row.id))
    except copies.RemoteMirrorKept as error:
        print(f"refused: {error}")
        return 1
    except copies.CopyError as error:
        print(f"anomaly: {error}".replace("\n", " "))
    failed = retirement_remove(unit, settings=settings)
    if failed:
        print("\n".join(f"anomaly: {reason}" for reason in failed))
        return 1
    _ = staff.set_worktree(row.id, staff.RemovedWorktree(local_machine(), unit.worktree), expected=row.worktree)
    print(f"{unit.name} retired")
    return 0

ADD_UNIT = SCRIPTS / "production" / "add_unit.py"

class RecoveryRefused(Exception):
    pass

class RecoveryFactsMissing(RecoveryRefused):
    """The configuration must be filled before preview or apply."""

@dataclass(frozen=True)
class NoRecoveryRequired:
    production: Production
    name: str

@dataclass(frozen=True)
class RetiredUnitRecovery:
    production: Production
    name: str
    worktree: Path
    branch: str
    plan: unit_config.PlanFile | unit_config.StandbyPlan | unit_config.PermanentPlan
    session_id: str
    account: accounts.NamedAccount | accounts.ReadCurrentAccountSettings = accounts.CURRENT_ACCOUNT_SETTINGS

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise RecoveryRefused(f"{self.name}: recovery requires a session id")

def recovery_find_unit(production_name: str, given: str, *, record: staff.StaffSnapshot | None = None) -> RetiredUnitRecovery | NoRecoveryRequired:
    record = staff.initial_snapshot() if record is None else record
    production = retirement_production_of(production_name, record=record)
    row = record.by_id(staff.StaffId(given))
    member = (staff_sources.MemberFound(row) if row is not None and row.kind == "unit" else
              staff_sources.member(staff_sources.UnitMember(production.name, given), record))
    name = member.row.name if isinstance(member, staff_sources.MemberFound) else given
    if isinstance(member, staff_sources.NoSuchMember):
        raise RecoveryRefused(f"{name}: no staff row")
    location = member.row.worktree
    if isinstance(location, staff.WorktreeUnfilled):
        raise RecoveryRefused(f"{name}: staff worktree is unfilled")
    if location.machine != remote.local_machine():
        raise RecoveryRefused(f"{name}: staff worktree belongs to {location.machine}; run resume there")
    worktree = location.path
    saved = unit_config.read_parked(member.row.id, cleanup=False)
    current = unit_config.read(worktree, cleanup=False)
    if isinstance(saved, unit_config.ConfigMissing) and isinstance(current, unit_config.ConfigMissing):
        raise RecoveryFactsMissing(f"{name}: no .unit.toml in {worktree} or ~/.local/state/staff/units/{member.row.id}.toml; "
                               + "ask the showrunner")
    if isinstance(saved, unit_config.ConfigMissing):
        if isinstance(location, staff.RemovedWorktree) or not isinstance(current, unit_config.UnitConfig):
            raise RecoveryRefused(f"{name}: parked retirement facts are missing")
        return NoRecoveryRequired(production, name)
    pair = unit_config.place(member.row.id, worktree, cleanup=False)
    if isinstance(pair, unit_config.Both) and not pair.same:
        raise RecoveryRefused(f"{pair.worktree} and {pair.parked}: configurations differ; no file was changed")
    return RetiredUnitRecovery(production, name, worktree, member.row.name, saved.plan, member.row.current_session)

def recovery_resume_record(production: Production, name: str) -> Path:
    result = retirement_run(["git", "-C", str(production.checkout), "rev-parse", "--git-path",
                              f"resume-units/{name}"])
    if result.returncode:
        raise RecoveryRefused(f"cannot find the resume record: {retirement_failure(result)}")
    path = Path(result.stdout.strip())
    return path if path.is_absolute() else production.checkout / path

def recovery_git(unit: RetiredUnitRecovery, *arguments: str) -> str:
    result = retirement_run(["git", "-C", str(unit.production.checkout), *arguments])
    return result.stdout.strip() if result.returncode == 0 else ""

def recovery_has_worktree(unit: RetiredUnitRecovery) -> bool:
    """Whether the worktree path holds the unit's worktree on its branch."""
    return ((unit.worktree / ".git").is_file()
            and retirement_run(["git", "-C", str(unit.worktree), "branch", "--show-current"]).stdout.strip()
            == unit.branch)

def recovery_has_transcript(unit: RetiredUnitRecovery) -> bool:
    return bool(sessions.transcript_locations(unit.session_id))

def recovery_is_running(unit: RetiredUnitRecovery) -> bool:
    """Whether the unit's mark is on a live Claude that is the session it resumes."""
    try:
        found = unit_lookup.labeled_session(unit.production.name, unit.name)
    except OSError as error:
        raise RecoveryRefused(f"{unit.name}: cannot tell whether Claude is running: {error}") from error
    return (found is not None and isinstance(found.claude, unit_lookup.LiveClaude)
            and found.claude.session_id == unit.session_id)

def recovery_refusals(unit: RetiredUnitRecovery) -> list[str]:
    refused: list[str] = []
    try:
        if not recovery_has_transcript(unit):
            refused.append(f"{unit.name}: no account has the transcript of session {unit.session_id}")
    except (RetirementRefused, OSError) as error:
        refused.append(f"{unit.name}: {error}")
    if unit.worktree.exists() and not recovery_has_worktree(unit):
        refused.append(f"{unit.worktree} exists and is not {unit.name}'s worktree on {unit.branch}")
    elif not unit.worktree.exists():
        if recovery_git(unit, "branch", "--list", unit.branch):
            refused.append(f"branch {unit.branch} still exists; retire did not finish")
        if recovery_git(unit, "ls-remote", "--heads", "origin", unit.branch):
            refused.append(f"origin {unit.branch} still exists; retire did not finish")
    return refused

def recovery_launch_command(unit: RetiredUnitRecovery) -> list[str]:
    # The row name is both the branch and the add_unit name.
    arguments = [sys.executable, str(ADD_UNIT), "--production", unit.production.name, unit.branch,
                 "--resume", unit.session_id, "--cwd", str(unit.worktree), *unit_config.plan_arguments(unit.plan)]
    if isinstance(unit.account, accounts.NamedAccount):
        arguments.extend(["--account", unit.account.label])
    return arguments

def recovery_make_worktree(unit: RetiredUnitRecovery) -> None:
    if recovery_has_worktree(unit):
        return
    command = ["git", "-C", str(unit.production.checkout), "worktree", "add", str(unit.worktree)]
    command += ([unit.branch] if recovery_git(unit, "branch", "--list", unit.branch) else
                ["-b", unit.branch, unit.production.merge_branch])
    result = retirement_run(command)
    if result.returncode:
        raise RecoveryRefused(f"worktree {unit.worktree}: {retirement_failure(result)}")
    print(f"made: worktree {unit.worktree} on {unit.branch}, from {unit.production.merge_branch}")

def recovery_launch(unit: RetiredUnitRecovery) -> None:
    if recovery_is_running(unit) and not add_unit.pending_opening(unit.production, unit.name, unit.session_id):
        return
    result = retirement_run(recovery_launch_command(unit))
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.returncode:
        raise RecoveryRefused(f"add_unit.py --resume: {retirement_failure(result)}")
    print(f"resumed: session {unit.session_id} in {unit.worktree}")

def recovery_resumed_session_proven(production_name: str, name: str, session_id: str, row: staff.StaffRow) -> bool:
    marked = unit_lookup.labeled_session(production_name, name)
    if marked is None or not isinstance(marked.claude, unit_lookup.LiveClaude):
        return False
    entry = pane_record.read(marked.place.session).get(marked.place.pane)
    return (marked.claude.session_id == session_id and entry is not None
            and entry["session_id"] == row.id and session_id == row.current_session)

def recovery_resume(unit: RetiredUnitRecovery, record: staff.StaffSnapshot) -> int:
    refused = recovery_refusals(unit)
    if refused:
        print("\n".join(f"refused: {reason}" for reason in refused))
        return 1
    member = staff_sources.member(staff_sources.UnitMember(unit.production.name, unit.name), record)
    if isinstance(member, staff_sources.NoSuchMember):
        raise RecoveryRefused(f"{unit.name}: no retained staff row")
    recovery_make_worktree(unit)
    unit_config.restore(member.row.id, unit.worktree)
    row = staff.restore_unit(member.row.id, staff.PresentWorktree(remote.local_machine(), unit.worktree))
    recovery_launch(unit)
    if not recovery_resumed_session_proven(unit.production.name, unit.name, unit.session_id, row):
        raise RecoveryRefused(f"{unit.name}: resumed native session identity is not proven; parked facts kept")
    completed = recovery_resume_record(unit.production, unit.name).with_suffix(".done")
    completion_receipt.write_receipt(completed, row.id, {"session_id": unit.session_id,
                                                   "doc": str(unit.production.doc), "unit": unit.name})
    unit_config.discard_parked(row.id)
    code = worker.restore(row.id)
    print(f"{unit.name} resumed")
    return int(code)


def named_staff_unit(name: str, record: staff.StaffSnapshot) -> staff.StaffRow:
    matches = [member for member in record.rows if member.kind == "unit" and member.name == name]
    row = (record.by_member_reference(name)
           or (matches[0] if len(matches) == 1 else record.by_path(name)))
    if row is None or row.kind != "unit":
        raise ValueError(f"{name}: no unique unit staff row")
    return row


def unit_production_name(row: staff.StaffRow, record: staff.StaffSnapshot) -> str:
    parent = record.by_id(row.parent.id) if isinstance(row.parent, staff.ReportsTo) else None
    if parent is None:
        raise ValueError(f"{row.name}: no showrunner staff row")
    return parent.name


def retire_named_unit(name: str) -> int:
    def apply() -> int:
        try:
            settings = conf.read("production")
            record = staff.snapshot()
            row = named_staff_unit(name, record)
            unit = retirement_find_unit(unit_production_name(row, record), str(row.id), record=record, settings=settings)
            return retirement_retire(unit, settings=settings, record=record)
        except (RetirementRefused, Refusal, ValueError, OSError, copies.CopyError, conf.Unusable,
                unit_config.ConfigRefused, subprocess.SubprocessError) as error:
            print(f"retire: {error}")
            return 1
    with staff.operation():
        return staff.cli_result(apply)


def recover_named_unit(name: str, *,
                       account: accounts.NamedAccount | accounts.ReadCurrentAccountSettings) -> int:
    with staff.operation():
        return staff.cli_result(lambda: recovery_apply(name, account=account))


def recovery_apply(name: str, *,
                   account: accounts.NamedAccount | accounts.ReadCurrentAccountSettings) -> int:
    try:
        record = staff.initial_snapshot()
        row = named_staff_unit(name, record)
        unit = recovery_find_unit(unit_production_name(row, record), str(row.id), record=record)
        completed = completion_receipt.read_receipt(recovery_resume_record(unit.production, unit.name).with_suffix(".done"),
                                                record)
        if (isinstance(unit, NoRecoveryRequired) and isinstance(completed, completion_receipt.OwnedReceipt)
                and completed.owner.kind == "unit" and completed.fields.get("doc") == str(unit.production.doc)
                and completed.fields.get("unit") == unit.name
                and isinstance(completed.owner.worktree, staff.PresentWorktree)
                and isinstance(completed.fields.get("session_id"), str)
                and recovery_resumed_session_proven(unit.production.name, unit.name,
                    cast(str, completed.fields["session_id"]), completed.owner)):
            code = worker.restore(completed.owner.id)
            print(f"{unit.name} already resumed")
            return int(code)
        if isinstance(unit, NoRecoveryRequired):
            raise RecoveryRefused(f"{unit.name} is not retired")
        return recovery_resume(replace(unit, account=account), record)
    except (staff.StaffRefused, staff.StaffUnreadable):
        raise
    except RecoveryFactsMissing as error:
        print(str(error))
        return 1
    except (RecoveryRefused, RetirementRefused, Refusal, unit_names.BadName, ValueError,
            conf.Unusable, subprocess.SubprocessError, OSError) as error:
        print(f"refused: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
