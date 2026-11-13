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
from enum import Enum
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from zoneinfo import ZoneInfo
from datetime import datetime
from pathlib import Path
from typing import Literal, TypedDict, NamedTuple, NotRequired, cast

from scripts.accounts import accounts
from scripts.herdr import herdr_types
from scripts.agents import worker_lifecycle
from scripts.copies import copies
from scripts.direct import progress_history
from scripts.herdr import herdr, pane_launch, worker_panes
from scripts.lib import process_group, process_start, conf, berth_board, git_files, unit_runs
from scripts.lib.conf import UseConfiguredValue
from scripts.message import sessions, session_send, send_budget, send_result
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
from scripts.production.unit_move import RenameRefused, read_json, write_json
from scripts.lib.atomic_file import sync_folder, write_text_atomic
from scripts.agents import (
    restart,
    restart_support,
    shutdown_record,
    stop_resume,
    remote_control,
    remote,
    pane_record,
)
from scripts.agents.inventory import (
    CheckoutState,
    Host,
    InvalidInventory,
    Inventory,
    Session,
    inventory,
    parse_inventory,
)
from scripts.agents.remote import other_machine, run_remote as run_remote, local_machine
from scripts.agents.stop_results import StopResult, ResumeResult
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
    old: str = ""
    new: str = ""
    to_doc: str = ""
    to_branch: str = ""
    target: str = ""
    producer: str = ""


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
  agents rename OLD NEW [--to-doc PATH] [--to-branch NAME]
  agents move SESSION_OR_GROUP ACCOUNT [--dry-run] [--why TEXT]
  agents restart [ACCOUNT] [--all] [--dry-run] [--only ID[,ID...]] [--here]
  agents stop SESSION [--dry-run] | --all [--dry-run] [--only ID[,ID...]] [--account ACCOUNT]
  agents remove-stray NAME SESSION
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


@dataclass(frozen=True)
class StrayProducerPane:
    """A terminal that contains the selected stray conversation."""

    pane: str
    terminal: str


class ProducerCleanupStep(Enum):
    """The next step suggested by the last successful progress write."""

    STOP = "stop"
    ADMISSION = "admission"
    PANES = "panes"
    TRANSCRIPTS = "transcripts"
    COMPLETE = "complete"


@dataclass(frozen=True)
class ProvedStrayProcess:
    """A native process proved before cleanup changes its evidence."""

    pid: int
    started: str
    kind: Literal["claude", "codex"]


@dataclass(frozen=True)
class ProvedStrayTerminal:
    """The pane, tab, and terminal selected before cleanup starts."""

    pane: str
    tab: str
    terminal: str


@dataclass(frozen=True)
class PendingProducerCleanup:
    """Durable targets and progress for one proved stray conversation."""

    name: str
    session: str
    herdr_session: str
    processes: tuple[ProvedStrayProcess, ...]
    panes: tuple[ProvedStrayTerminal, ...]
    admission_matches: bool
    admission: dict[str, object]
    transcripts: tuple[Path, ...]
    step: ProducerCleanupStep = ProducerCleanupStep.STOP


def save_producer_cleanup(path: Path, cleanup: PendingProducerCleanup) -> None:
    """Persist the targets and the progress hint together."""
    write_text_atomic(path, json.dumps({
        "name": cleanup.name, "session": cleanup.session,
        "herdr_session": cleanup.herdr_session,
        "processes": [{"pid": process.pid, "started": process.started, "kind": process.kind} for process in cleanup.processes],
        "panes": [{"pane": pane.pane, "tab": pane.tab, "terminal": pane.terminal} for pane in cleanup.panes],
        "admission_matches": cleanup.admission_matches, "admission": cleanup.admission,
        "transcripts": [str(transcript) for transcript in cleanup.transcripts], "step": cleanup.step.value,
    }) + "\n", new_file_mode=0o600)


def read_producer_cleanup(path: Path, name: str, identity: str) -> PendingProducerCleanup:
    """Read cleanup evidence without observing its former process or pane."""
    saved = herdr.json_object(cast(object, json.loads(path.read_text())), "producer cleanup")
    if saved.get("name") != name or saved.get("session") != identity:
        raise ValueError("producer cleanup targets another name or session")
    processes: list[ProvedStrayProcess] = []
    for value in herdr.json_items(saved.get("processes"), "cleanup processes"):
        process = herdr.json_object(value, "cleanup process")
        pid = process.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise ValueError("producer cleanup has an invalid pid")
        kind = herdr.json_text(process.get("kind"), "cleanup process kind")
        if kind not in ("claude", "codex"):
            raise ValueError("producer cleanup has an invalid agent kind")
        processes.append(ProvedStrayProcess(pid, herdr.json_text(process.get("started"), "cleanup process start"), kind))
    panes: list[ProvedStrayTerminal] = []
    for value in herdr.json_items(saved.get("panes"), "cleanup panes"):
        pane = herdr.json_object(value, "cleanup pane")
        panes.append(ProvedStrayTerminal(herdr.json_text(pane.get("pane"), "cleanup pane id"),
            herdr.json_text(pane.get("tab"), "cleanup tab id"),
            herdr.json_text(pane.get("terminal"), "cleanup terminal id")))
    matches = saved.get("admission_matches")
    if not isinstance(matches, bool):
        raise ValueError("producer cleanup has no admission match result")
    transcripts = tuple(Path(herdr.json_text(value, "cleanup transcript"))
                        for value in herdr.json_items(saved.get("transcripts"), "cleanup transcripts"))
    if any(not transcript.is_absolute() or transcript.name != identity + ".jsonl" for transcript in transcripts):
        raise ValueError("producer cleanup has an invalid transcript target")
    return PendingProducerCleanup(name, identity,
        herdr.json_text(saved.get("herdr_session"), "cleanup herdr session"), tuple(processes), tuple(panes), matches,
        herdr.json_object(saved.get("admission"), "cleanup admission"), transcripts,
        ProducerCleanupStep(herdr.json_text(saved.get("step"), "cleanup step")))


def finish_producer_cleanup(path: Path, cleanup: PendingProducerCleanup, admission: Path) -> int:
    """Complete saved steps and retain their evidence through every failure."""
    client = herdr.Herdr(cleanup.herdr_session)
    for process in cleanup.processes:
        observed = sessions.process_liveness(process.pid, process.started)
        if isinstance(observed, sessions.SessionLivenessUnknown):
            raise OSError(observed.reason)
        if isinstance(observed, sessions.SessionNotLive):
            continue
        result = stop_resume.stop(cleanup.session, process.kind, deadline=worker_lifecycle.effective_deadline())
        observed = sessions.process_liveness(process.pid, process.started)
        if isinstance(observed, sessions.SessionLivenessUnknown):
            raise OSError(observed.reason)
        if isinstance(observed, sessions.LiveSessionProcess):
            reason = result.reason if isinstance(result, stop_resume.StopFailed) else "stop is incomplete"
            raise ValueError(f"saved pid {process.pid} is still running: {reason}")
    transcripts = tuple(sorted(set(cleanup.transcripts) | set(sessions.transcript_locations(cleanup.session))))
    cleanup = replace(cleanup, step=ProducerCleanupStep.ADMISSION, transcripts=transcripts)
    save_producer_cleanup(path, cleanup)
    if cleanup.admission_matches and admission.exists():
        saved = herdr.json_object(cast(object, json.loads(admission.read_text())), "producer admission")
        if saved == cleanup.admission:
            admission.unlink()
            sync_folder(admission.parent)
    cleanup = replace(cleanup, step=ProducerCleanupStep.PANES)
    save_producer_cleanup(path, cleanup)
    for pane in cleanup.panes:
        current = client.pane_row(pane.pane)
        if isinstance(current, herdr.NoPane):
            continue
        if current.get("tab_id") != pane.tab:
            raise ValueError(f"{pane.pane}: stray tab identity changed")
        _ = client.close_retained_pane(pane.pane, terminal=pane.terminal)
        if not isinstance(client.pane_row(pane.pane), herdr.NoPane):
            raise ValueError(f"{pane.pane}: stray pane closure is incomplete")
    cleanup = replace(cleanup, step=ProducerCleanupStep.TRANSCRIPTS)
    save_producer_cleanup(path, cleanup)
    for transcript in cleanup.transcripts:
        if transcript.exists():
            transcript.unlink()
        try:
            sync_folder(transcript.parent)
        except FileNotFoundError:
            pass
    cleanup = replace(cleanup, step=ProducerCleanupStep.COMPLETE)
    save_producer_cleanup(path, cleanup)
    path.unlink()
    sync_folder(path.parent)
    print(f"{cleanup.session}: removed stray")
    return 0


def stray_producer_panes(client: herdr.Herdr, identity: str) -> list[StrayProducerPane]:
    """Retain only panes with native evidence or an idle retained conversation."""
    retained = pane_record.read_strict(client.session)
    record = staff.initial_snapshot()
    observed = herdr.read_conversation(identity)
    if isinstance(observed, herdr.UnknownConversation):
        raise ValueError(observed.reason)
    selected: list[StrayProducerPane] = []
    for pane in client.panes():
        entry = retained.get(pane["pane_id"])
        retained_identity = ""
        if entry is not None:
            owner = record.by_member_reference(pane_launch.reference_id(entry))
            retained_identity = owner.current_session if owner is not None else pane_launch.reference_id(entry)
        if isinstance(observed, herdr.StoppedConversation) and retained_identity != identity:
            continue
        info = herdr.json_object(client.result("pane", "process-info", "--pane", pane["pane_id"]).get("process_info"),
                                 "stray pane process info")
        proved = False
        for value in herdr.json_items(info.get("foreground_processes"), "stray pane foreground processes"):
            process = herdr.json_object(value, "stray pane process")
            pid = process.get("pid")
            if not isinstance(observed, herdr.LiveConversation) or pid not in observed.pids:
                continue
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                raise ValueError("stray pane process has no valid pid")
            arguments = process.get("argv")
            if not isinstance(arguments, list):
                raise ValueError("stray pane process has no arguments")
            argv = [herdr.json_text(argument, "stray process argument") for argument in cast(list[object], arguments)]
            matches = herdr.is_claude_process(argv) if observed.kind == "claude" else herdr.is_codex_process(argv)
            if not matches:
                raise ValueError(f"{pid}: selected process no longer runs {observed.kind}")
            native = herdr.native_process(pid, observed.kind)
            proved = proved or native.current_identity == identity
        idle_retained = (retained_identity == identity
                         and isinstance(client.pane_activity(pane["pane_id"]), herdr.PaneAtShell))
        if proved or idle_retained:
            selected.append(StrayProducerPane(pane["pane_id"], herdr.json_text(pane.get("terminal_id"), "stray terminal")))
    return selected


def remove_stray_producer(name: str, session: str) -> int:
    """Stop a proved stray and remove its terminal, transcripts, and admission record."""
    identity = str(staff.native_id(session))
    admission = stop_resume.producer_admission_record(name)
    with staff.slot_operation("producer-first-" + name, name), staff.row_operation(name):
        record = staff.initial_snapshot()
        owners = [row for row in record.rows if row.kind == "producer" and row.name == name]
        if len(owners) > 1:
            raise ValueError(f"{name}: several producer rows match")
        if any(row.current_session == identity for row in record.rows):
            raise ValueError(f"{identity}: current_session belongs to a staff row; removal refused")
        cleanup_path = admission.with_name(f".producer-cleanup-{name}-{identity}")
        if cleanup_path.exists():
            return finish_producer_cleanup(cleanup_path, read_producer_cleanup(cleanup_path, name, identity), admission)
        client = herdr.Herdr(herdr.shared_session())
        panes = stray_producer_panes(client, identity)
        saved: dict[str, object] = {}
        if admission.exists():
            saved = herdr.json_object(cast(object, json.loads(admission.read_text())), "producer admission")
        retained = pane_record.read_strict(client.session)
        admission_matches = saved.get("session_id") == identity
        for pane in panes:
            if saved.get("pane") == pane.pane and saved.get("terminal") == pane.terminal:
                admission_matches = True
            entry = retained.get(pane.pane)
            if entry is not None and saved.get("pane") == pane.pane:
                owner = record.by_member_reference(pane_launch.reference_id(entry))
                retained_identity = owner.current_session if owner is not None else pane_launch.reference_id(entry)
                admission_matches = admission_matches or retained_identity == identity
        observed = herdr.read_conversation(identity)
        if isinstance(observed, herdr.UnknownConversation):
            raise ValueError(observed.reason)
        processes: list[ProvedStrayProcess] = []
        if isinstance(observed, herdr.LiveConversation):
            conflicts = stop_resume.producer_conflicts(name, client)
            if not admission_matches and not any(
                    found == identity or any(place == pane.pane for pane in panes) for found, place in conflicts):
                raise ValueError(f"{identity}: session has no producer name or pane proof for {name}")
            for pid in observed.pids:
                native = herdr.native_process(pid, observed.kind)
                if native.current_identity != identity:
                    raise ValueError(f"{pid}: stray native identity changed")
                processes.append(ProvedStrayProcess(pid, native.started, observed.kind))
        terminals: list[ProvedStrayTerminal] = []
        for pane in panes:
            current = client.pane_row(pane.pane)
            if isinstance(current, herdr.NoPane) or current.get("terminal_id") != pane.terminal:
                raise ValueError(f"{pane.pane}: stray terminal identity changed")
            terminals.append(ProvedStrayTerminal(pane.pane, herdr.json_text(current.get("tab_id"), "stray tab"), pane.terminal))
        transcripts = tuple(sessions.transcript_locations(identity))
        if not processes and not panes and not transcripts and not admission_matches:
            print(f"{identity}: unchanged")
            return 0
        cleanup = PendingProducerCleanup(name, identity, client.session, tuple(processes), tuple(terminals),
                                         admission_matches, saved, transcripts)
        save_producer_cleanup(cleanup_path, cleanup)
        return finish_producer_cleanup(cleanup_path, cleanup, admission)


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


def _worktree_is_remote(row: staff.StaffRow) -> bool:
    return not isinstance(row.worktree, staff.WorktreeUnfilled) and not unit_machine_is_local(row.worktree.machine)


def _paused_resume_needs_message(row: staff.StaffRow) -> bool:
    remote = (_worktree_is_remote(row) if not isinstance(row.worktree, staff.WorktreeUnfilled) else
              isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)) and not unit_machine_is_local(row.doc.machine))
    return isinstance(row.paused, staff.Paused) and (remote or isinstance(
        stop_resume.running_on_account(str(row.current_session), "",
            kind="codex" if isinstance(staff.backend(row), staff.CodexBackend) else "claude"),
        stop_resume.ClaudeUp))


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
            with staff.row_operation(str(row.id)):
                started = False
                if _paused_resume_needs_message(row):
                    _ = staff.resume(str(row.id))
                    delivery = _message(row, "Continue where you stopped. Resume your existing workers. Do not open new workers.")
                    if restart_support.message_was_delivered(delivery):
                        started = True
                        shutdown_record.remove_started([str(row.id)])
                        print(f"{row.name}: active")
                    else:
                        _ = staff.set_hold(str(row.id), row.paused)
                        code = 1
                else:
                    try:
                        if isinstance(row.paused, staff.Paused):
                            _ = staff.resume(str(row.id))
                        result = stop_resume.resume(str(row.id), why=options.why, to=options.to)
                        started = isinstance(result, stop_resume.ClaudeUp)
                        if started and isinstance(row.exited, staff.Exited):
                            _ = staff.resume(str(row.id))
                        code = max(code, _result_code(result))
                    finally:
                        if not started and isinstance(row.paused, staff.Paused):
                            _ = staff.set_hold(str(row.id), row.paused)
            continue
        already_exited = options.command == "exit" and isinstance(row.exited, staff.Exited)
        held = row if already_exited else staff.pause(str(row.id), options.why)
        remote_exit = options.command == "exit" and _worktree_is_remote(row)
        if options.finish_step and not remote_exit:
            _finish_tasks(row, record)
        if remote_exit:
            outcome = unit_remote_command("stop", row, record, finish_step=options.finish_step)
            stopped = int(outcome["code"])
        else:
            unresolved = stop_resume.stop_unit_workers(row.id, record)
            stopped = int(bool(unresolved))
            if options.command == "exit":
                stopped = max(stopped, _result_code(stop_resume.stop(str(row.id), deadline=worker_lifecycle.effective_deadline())))
        code = max(code, stopped)
        if options.command == "exit":
            if stopped:
                continue
            if not already_exited:
                held = staff.exit(str(row.id), options.why)
        else:
            _ = _message(row, "Finish your current turn. Hold at the prompt. Do not start new work. Wait for agents resume.")
        print(f"{row.name}: {'exited' if isinstance(held.exited, staff.Exited) else 'paused'}")
    return code


def _restart_account(requested: str | None) -> restart.AllRestartAccounts | accounts.AccountSelection:
    """Resolve an optional account for restart selection."""
    if not requested:
        return restart.AllRestartAccounts()
    parsed = note_file.parse_account_label(requested)
    if isinstance(parsed, note_file.NumberedAccountLabel) and parsed.tool == "codex":
        if not isinstance(note_file.read_note(note_file.note_path(parsed.label)), note_file.Note):
            raise UnknownAccountName(f"unknown account {parsed.label}")
        home = agent_accounts.codex_account_home(parsed.label)
        return accounts.CodexWorkersAccount(home.label, home.home)
    named = named_claude_account(requested)
    return accounts.NamedAccount(named.label, accounts.account_dir(named.label))


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if not arguments or arguments in (["help"], ["--help"], ["-h"]):
        print(USAGE, end="")
        return 0
    if arguments == ["_unit-local"]:
        return unit_local_command()
    parser = argparse.ArgumentParser(prog="agents", usage=USAGE.removeprefix("usage: ").rstrip())
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    command = commands.add_parser("stop")
    selection = command.add_mutually_exclusive_group(required=True)
    _ = selection.add_argument("session", nargs="?")
    _ = selection.add_argument("--all", action="store_true")
    _ = command.add_argument("--dry-run", action="store_true")
    _ = command.add_argument("--only", default="")
    _ = command.add_argument("--account", dest="account")
    command = commands.add_parser("remove-stray")
    _ = command.add_argument("producer", metavar="NAME")
    _ = command.add_argument("session", metavar="SESSION")
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
    command = commands.add_parser("rename")
    _ = command.add_argument("old")
    _ = command.add_argument("new")
    _ = command.add_argument("--to-doc", default="")
    _ = command.add_argument("--to-branch", default="")
    command = commands.add_parser("move", description="Move Claude sessions to <claudeN>.")
    _ = command.add_argument("session", metavar="SESSION_OR_GROUP")
    _ = command.add_argument("target", metavar="ACCOUNT")
    _ = command.add_argument("--dry-run", action="store_true")
    _ = command.add_argument("--why", default="")
    command = commands.add_parser("restart")
    _ = command.add_argument("--why", default=restart.DEFAULT_RESTART_CAUSE)
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
    command = commands.add_parser("held-sessions")
    _ = command.add_argument("--only", default="")
    _ = command.add_argument("account", nargs="?")
    command = commands.add_parser("remote-control")
    _ = command.add_argument("session")
    _ = command.add_argument("--pid", type=int, required=True)
    _ = command.add_argument("--proc-start", required=True)
    _ = command.add_argument("--who", required=True)
    _ = command.add_argument("--session", dest="account", required=True)
    options = parser.parse_args(arguments, namespace=CommandLine())
    try:
        if options.command == "remove-stray":
            return remove_stray_producer(options.producer, options.session)
        if options.command == "stop" and options.all:
            return restart.stop_all(dry_run=options.dry_run,
                                    only=tuple(item for item in options.only.split(",") if item),
                                    account=_restart_account(options.account))
        if options.command == "move":
            move_arguments = ["move", options.session, note_file.canonical_label(options.target)]
            if options.dry_run:
                move_arguments.append("--dry-run")
            if options.why:
                move_arguments.extend(("--why", options.why))
            return accounts.main(move_arguments)
        if options.command == "restart":
            return restart.restart(stop_all=options.all, dry_run=options.dry_run,
                                   only=tuple(item for item in options.only.split(",") if item),
                                   account=_restart_account(options.account), why=options.why)
        if options.command == "records":
            print(json.dumps(shutdown_record.read()))
            return 0
        if options.command == "held-sessions":
            record = staff.StaffSnapshot(())
            rows: list[staff.StaffRow] = []
            account = _restart_account(options.account)
            scoped = bool(options.only) or not isinstance(account, restart.AllRestartAccounts)
            try:
                record = staff.initial_snapshot()
                if options.only:
                    selection = restart.SelectedRestartRows(frozenset(row.id for reference in options.only.split(",")
                        if (row := record.by_member_reference(reference)) is not None))
                    reading = restart.live_rows(record, herdr.Herdr(herdr.shared_session()), save_tabs=True,
                                             account=account, selection=selection)
                else:
                    reading = restart.live_rows(record, herdr.Herdr(herdr.shared_session()), save_tabs=True, account=account)
            except (OSError, ValueError, staff.StaffRefused, herdr_types.HerdrFailure) as error:
                print(f"restart: save anomaly: {error}", file=sys.stderr)
            else:
                rows = reading.rows
                if not scoped:
                    restart.prune_live_rows(reading)
                else:
                    for row in rows:
                        try:
                            with staff.row_operation(str(row.id)):
                                shutdown_record.update(started=[], pending=[str(row.id)])
                        except staff.StaffRefused as error:
                            print(f"restart: save anomaly: {error}", file=sys.stderr)
            saved = "rows for restart" if scoped else "live row tabs"
            print(f"saved {len(rows)} {saved} at {time.strftime('%H:%M')}", flush=True)
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
        if options.command == "rename":
            return rename_named_member(options.old, options.new, to_doc=options.to_doc, to_branch=options.to_branch)
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
            target = stop_resume.stop_target(options.session)
            if isinstance(target, (stop_resume.SessionNotFound, stop_resume.StopFailed)):
                return _result_code(target)
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
    except (OSError, ValueError, staff.StaffNotFound, UnknownAccountName, UnreadableAccount,
            InvalidInventory, herdr_types.HerdrFailure) as error:
        print(f"shutdown: {error}", file=sys.stderr)
        return 2


SCRIPTS = Path(__file__).resolve().parent.parent

DETACHED_RUNS = "detached_runs"

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

def retirement_branch(folder: Path) -> str:
    """The checked-out branch from Git's files; "" when HEAD is detached or the folder is no checkout."""
    try:
        return git_files.current_branch(folder)
    except git_files.GitFilesError:
        return ""

def retirement_failure(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout).strip() or f"exit {result.returncode}"

def retirement_production_of(production_name: str, *, record: staff.StaffSnapshot) -> Production:
    found = staff_sources.member(staff_sources.ShowrunnerMember(production_name), record)
    if isinstance(found, staff_sources.NoSuchMember) or isinstance(found.row.doc, staff.DocUnfilled):
        raise ValueError(f"{production_name}: no showrunner staff document")
    if not unit_machine_is_local(found.row.doc.machine):
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
    """Read the host's process identity used by its claim records."""
    if sys.platform == "darwin":
        observed = process_start.process_start(pid)
        if isinstance(observed, process_start.Unreadable):
            raise OSError(observed.reason)
        return observed.start if isinstance(observed, (process_start.Started, process_start.Zombie)) else ""
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

def retirement_holds(holder_ids: set[str]) -> tuple[str, ...]:
    """Find the build holds kept under the retiring members' fixed staff IDs."""
    return tuple(sorted(key for key in holder_ids if key and (retirement_hold_directory() / key).is_file()))

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
        parent = str(pane_record.process_parent(pid)) if sys.platform == "darwin" else (
            Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        parent = "unknown"
    return (f"unowned process {pid}, parent {parent}, command {' '.join(retirement_command_line(pid, settings=settings)).strip() or 'unknown'}, "
            f"start {retirement_proc_start(pid) or 'unknown'}")

def retirement_process_session(pid: int) -> str:
    if sys.platform == "darwin":
        return herdr.darwin_process_environment(pid).get("CLAUDE_CODE_SESSION_ID", "")
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    for field in raw.split(b"\0"):
        key, _, value = field.partition(b"=")
        if key == b"CLAUDE_CODE_SESSION_ID":
            return value.decode("utf-8")
    return ""

def retirement_process_ids(*, settings: conf.ConfigRead) -> list[int]:
    if sys.platform == "darwin":
        result = retirement_run(["ps", "-axo", "pid="], settings=settings)
        if result.returncode:
            raise OSError(f"ps -axo pid=: {retirement_failure(result)}")
        return [int(value) for value in result.stdout.split() if value.isdecimal()]
    return [int(entry.name) for entry in Path("/proc").iterdir() if entry.name.isdecimal()]

def retirement_process_inside(pid: int, worktree: Path) -> bool:
    if sys.platform == "darwin":
        return pid in unit_move.processes_inside(worktree)
    return Path(f"/proc/{pid}/cwd").resolve(strict=True).is_relative_to(worktree.resolve())

def retirement_stop_orphan(unit: RetiringUnit, pid: int, recorded_session_id: str = "", *, settings: conf.ConfigRead) -> list[str]:
    """Only a proven, isolated group of this stopped session can be ended."""
    duration = settings.number("RETIRE_TERM_WAIT_SECONDS")
    operation_limit = worker_lifecycle.effective_deadline()
    if operation_limit != worker_lifecycle.NO_LOCK_DEADLINE and time.monotonic() >= operation_limit:
        raise worker_lifecycle.OperationDeadlineExpired("retirement operation deadline expired")
    poll = settings.number("RETIRE_POLL_SECONDS")
    description = f"unowned process {pid}"
    try:
        description = retirement_process_description(pid, settings=settings)
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
        for member in retirement_process_ids(settings=settings):
            try:
                if os.getpgid(member) != pid:
                    continue
                if not retirement_process_inside(member, unit.worktree) or retirement_process_session(member) != session_id:
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
               or not retirement_process_inside(member, unit.worktree)
               for member, birth in retirement_members) or retirement_proc_start(pid) != started:
            raise RetirementRefused("process identity changed before signal")
        if operation_limit != worker_lifecycle.NO_LOCK_DEADLINE and time.monotonic() >= operation_limit:
            raise worker_lifecycle.OperationDeadlineExpired("retirement operation deadline expired")
        process_group.send_process_group(pid, signal.SIGTERM)
    except worker_lifecycle.OperationDeadlineExpired:
        raise
    except ProcessLookupError:
        return []
    except (OSError, ValueError, RetirementRefused, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
        return [f"{description} works inside {unit.worktree}; stop it first: {error}"]
    deadline = min(time.monotonic() + duration, operation_limit)
    def still_running() -> bool:
        observed = process_start.process_start(pid)
        return isinstance(observed, process_start.Unreadable) or (
            isinstance(observed, process_start.Started) and observed.start == started and retirement_alive(pid))
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
    live: list[MacClaim] = []
    for claim in claims:
        if retirement_alive(claim.pid):
            started = retirement_proc_start(claim.pid)
            if not started or not claim.proc_start:
                raise OSError(f"claim {claim.what}: process identity is unreadable")
            if started == claim.proc_start:
                live.append(claim)
    return tuple(live)

def retirement_runs_of(owner: staff.StaffRow, record: staff.StaffSnapshot) -> list[Path]:
    """Read run ownership through the staff row's session identities."""
    found: list[Path] = []
    for folder in unit_runs.run_folders(os.environ.get("RETIRE_UNIT_RUN_ROOT") or None):
        if record.by_member_reference(unit_runs.director(folder, record=record)) == owner:
            found.append(folder)
    return found

def retirement_session_of(pid: int) -> int:
    try:
        return os.getsid(pid)
    except OSError:
        return 0

def retirement_run_processes(owner: staff.StaffRow, inside: list[int], *, record: staff.StaffSnapshot,
                             settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> dict[int, str]:
    """Name the detached command processes that remain in the worktree."""
    if isinstance(settings, UseConfiguredValue):
        settings = conf.read("production")
    runs = retirement_runs_of(owner, record)
    leaders: dict[int, str] = {}
    for folder in runs:
        for path in sorted((folder / DETACHED_RUNS).glob("*.json")):
            run_record = retirement_json(path)
            pid = run_record.get("pid")
            if (run_record.get("status") == "running" and isinstance(pid, int) and not isinstance(pid, bool)
                    and pid > 0 and str(path) in retirement_command_line(pid, settings=settings)):
                leaders[pid] = f"run supervisor {pid} of {run_record.get('label', path.stem)} in run {folder.name}"
    return {pid: f"process {pid} ({retirement_command_name(pid)}) of the {leaders[leader]}"
            for pid in inside if (leader := retirement_session_of(pid)) in leaders}

def retirement_command_name(pid: int) -> str:
    if sys.platform == "darwin":
        return retirement_run(["ps", "-o", "comm=", "-p", str(pid)]).stdout.strip() or "?"
    try:
        return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
    except OSError:
        return "?"

def retirement_find_unit(production_name: str, given: str, *,
              record: staff.StaffSnapshot | None = None) -> RetiringUnit:
    record = staff.snapshot() if record is None else record
    production = retirement_production_of(production_name, record=record)
    member = record.by_id(staff.StaffId(given))
    if member is None or member.kind != "unit":
        selected = staff_sources.member(staff_sources.UnitMember(production.name, given), record)
        if isinstance(selected, staff_sources.NoSuchMember):
            raise ValueError(f"{production.name}/{given}: no matching staff member")
        member = selected.row
    location = member.worktree
    if isinstance(location, staff.WorktreeUnfilled):
        raise ValueError(f"{member.name}: staff worktree is unfilled")
    branch = location.branch.value if isinstance(location, staff.RemovedWorktree) and isinstance(location.branch, staff.ParkedBranch) else ""
    if unit_machine_is_local(location.machine) and location.path.is_dir():
        branch = retirement_branch(location.path)
    return RetiringUnit(production, member.name, location.path, branch, member)

def retirement_last_session(unit: RetiringUnit) -> str:
    return unit.staff_member.current_session

def retirement_branch_commits(unit: RetiringUnit, branch: str, *, settings: conf.ConfigRead) -> list[str]:
    commits: list[str] = []
    branch_ref = f"refs/heads/{branch}"
    observed = retirement_git(unit, "show-ref", "--verify", "--quiet", branch_ref, settings=settings)
    if observed.returncode == 0:
        commits.append(branch_ref)
    elif observed.returncode != 1:
        raise OSError(retirement_failure(observed))
    remote = retirement_git(unit, "ls-remote", "--exit-code", "--heads", "origin", branch_ref, settings=settings)
    if remote.returncode not in (0, 2):
        raise OSError(retirement_failure(remote))
    if remote.returncode == 0:
        fetched = retirement_git(unit, "fetch", "--no-tags", "--no-write-fetch-head", "--refmap=", "-q", "origin", branch_ref, settings=settings)
        if fetched.returncode:
            raise OSError(retirement_failure(fetched))
        commits.append(remote.stdout.split()[0])
    return commits

def retirement_refusals(unit: RetiringUnit, *, settings: conf.ConfigRead, check_absent_branch: bool = False) -> list[str]:
    """Keep uncommitted work and commits outside the merge branch."""
    present = unit.worktree.is_dir()
    if not present and (not check_absent_branch or not unit.branch):
        return []
    found: list[str] = []
    if present:
        status = retirement_run(["git", "-C", str(unit.worktree), "status", "--short"], settings=settings)
        if status.returncode:
            raise OSError(f"cannot read {unit.worktree}: {retirement_failure(status)}")
        paths = [line[3:] for line in status.stdout.splitlines()
                 if not (line[:2] == " M" and retirement_recorder_line_only(unit.worktree, line[3:], settings=settings))]
        if paths:
            found.append(f"uncommitted work in {unit.worktree}: {', '.join(paths)}")
    refs = (retirement_branch_commits(unit, unit.branch, settings=settings) if unit.branch else
            ["HEAD"] if present else [])
    merge_refs = retirement_branch_commits(unit, unit.production.merge_branch, settings=settings)
    if not merge_refs:
        raise OSError(f"merge branch {unit.production.merge_branch} is absent from origin and this clone")
    for ref in refs:
        commits = retirement_git(unit, "rev-list", "--no-merges", ref, "--not", *merge_refs, settings=settings)
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
        observed = retirement_git(unit, "ls-remote", "--exit-code", "--heads", "origin", unit.branch, settings=settings)
        if observed.returncode not in (0, 2):
            return [retirement_failure(observed)]
        if observed.returncode == 0:
            result = retirement_git(unit, "push", "-q", "origin", "--delete", unit.branch, settings=settings)
            if result.returncode:
                failed.append(retirement_failure(result))
    return failed

def retirement_release_reservations(unit: RetiringUnit, *, settings: conf.ConfigRead) -> None:
    for key in retirement_reservations(unit.worktree):
        result = retirement_run(["cargo-berth", "release", key, "--json"], cwd=unit.worktree, settings=settings)
        if result.returncode and not (result.returncode == 5 and "already has a final disposition" in result.stdout + result.stderr):
            print(f"anomaly: {retirement_failure(result)}")

class LocalUnitOutcome(TypedDict):
    code: int
    identity: str
    session: str
    machine: str
    worktree: str
    output: str
    branch: NotRequired[str]


def unit_machine_is_local(machine: str) -> bool:
    """Match a staff hostname or the SSH alias of this workstation."""
    alias = "mac" if other_machine() == "natedev" else "natedev"
    return machine.split(".", 1)[0].casefold() in {local_machine().split(".", 1)[0].casefold(), alias}


def retirement_journal(row: staff.StaffRow) -> Path:
    return Path(os.environ.get("SHUTDOWN_STATE_DIR", str(Path.home() / ".local/state/shutdown"))) / "retire-worktrees" / f"{row.id}.json"


def unit_remote_command(action: str, row: staff.StaffRow, record: staff.StaffSnapshot,
                        *, merge_branch: str = "", zone: str = "UTC", finish_step: bool = False) -> LocalUnitOutcome:
    """Run machine-bound work and require an attributed remote answer."""
    location = row.worktree
    assert not isinstance(location, staff.WorktreeUnfilled)
    result = LocalUnitOutcome(code=1, identity=str(row.id), session=row.current_session,
                              machine=location.machine, worktree=str(location.path), output="")
    try:
        if unit_machine_is_local(location.machine):
            raise ValueError(f"worktree belongs to this machine: {location.machine}")
        payload = {"action": action, "identity": str(row.id), "merge_branch": merge_branch, "zone": zone, "finish_step": finish_step,
                   "rows": [member.to_json() for member in record.rows]}
        status, output = run_remote(["_unit-local"], stdin=json.dumps(payload), limit=remote.WhileLinkAlive(kind="while link alive"))
        if status == 255:
            raise ValueError("remote command is unreachable or has no status")
        raw = cast(object, json.loads(output))
        if not isinstance(raw, dict):
            raise ValueError("remote command returned no unit result")
        fields = cast(dict[str, object], raw)
        expected = {"identity": str(row.id), "session": row.current_session,
                    "machine": location.machine, "worktree": str(location.path)}
        if (type(fields.get("code")) is not int or fields["code"] not in (0, 1) or fields["code"] != status
                or any(not isinstance(fields.get(key), str) for key in ("identity", "session", "machine", "worktree", "output"))
                or any(fields.get(key) != value for key, value in expected.items())):
            raise ValueError("remote unit result does not match the request")
        result["output"] = cast(str, fields["output"])
        if "branch" in fields:
            if not isinstance(fields["branch"], str):
                raise ValueError("remote parked branch must be a string")
            result["branch"] = fields["branch"]
        result["code"] = status
        if result["output"]:
            print(result["output"], end="" if result["output"].endswith("\n") else "\n")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"anomaly: {row.name}: {' '.join(str(error).splitlines())}", file=sys.stderr)
    return result


def unit_local_command() -> int:
    """Run private lifecycle work on the machine that owns the worktree."""
    fields = cast(dict[str, object], json.loads(sys.stdin.read()))
    raw_rows = cast(list[object], fields["rows"])
    record = staff.StaffSnapshot(tuple(staff.StaffRow.from_json(value) for value in raw_rows))
    row = record.by_id(staff.StaffId(str(fields["identity"])))
    if row is None or isinstance(row.worktree, staff.WorktreeUnfilled):
        raise ValueError("remote unit has no recorded worktree")
    location = row.worktree
    result = LocalUnitOutcome(code=1, identity=str(row.id), session=row.current_session,
                              machine=location.machine, worktree=str(location.path), output="")
    output = StringIO()
    with redirect_stdout(output), redirect_stderr(output):
        try:
            if not unit_machine_is_local(location.machine):
                raise ValueError(f"worktree belongs to {location.machine}")
            if fields["action"] == "stop":
                if fields.get("finish_step"):
                    _finish_tasks(row, record)
                reasons = stop_resume.stop_unit_workers(row.id, record)
                for reason in reasons:
                    print(f"anomaly: {reason}")
                stopped = stop_resume.stop(row.current_session, deadline=worker_lifecycle.effective_deadline())
                result["code"] = int(bool(max(_result_code(stopped), int(bool(reasons)))))
            elif fields["action"] == "retire-complete":
                if not isinstance(location, staff.RemovedWorktree):
                    raise ValueError("retirement journal requires a retired staff row")
                journal = retirement_journal(row)
                saved = retirement_json(journal)
                if saved.get("pane_id"):
                    if saved.get("pane_terminal"):
                        pane = RetiringPane(str(saved["pane_session"]), str(saved["pane_id"]), str(saved["pane_terminal"]))
                    else:
                        print(f"anomaly: {saved['pane_session']}:{saved['pane_id']}: saved terminal identity is unavailable")
                        pane = herdr.NoPane(row.name)
                else:
                    pane = retirement_pane(row, record)
                result["code"] = 0
                try:
                    journal.unlink(missing_ok=True)
                except OSError as error:
                    print(f"anomaly: {row.name}: cannot remove retirement journal: {' '.join(str(error).splitlines())}")
                retirement_close_idle_shell(pane, row, record)
            elif fields["action"] == "retire":
                settings = conf.read("production")
                journal = retirement_journal(row)
                pane = retirement_pane(row, record)
                if location.path.exists():
                    try:
                        common = git_files.locate(location.path).common_dir
                        branch = git_files.current_branch(location.path)
                    except git_files.GitFilesError as error:
                        raise ValueError("cannot read the worktree Git identity") from error
                    if not branch:
                        raise ValueError("cannot read the worktree Git identity")
                    config_path = location.path / ".unit.toml"
                    config = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
                    if config:
                        _ = unit_config.parse(config)
                    retained = {"session": row.current_session, "worktree": str(location.path),
                                "pane_session": pane.session if isinstance(pane, RetiringPane) else "",
                                "pane_id": pane.pane if isinstance(pane, RetiringPane) else "",
                                "pane_terminal": pane.terminal if isinstance(pane, RetiringPane) else "",
                                "checkout": str(common), "branch": branch, "config": config,
                                "date": datetime.now(ZoneInfo(str(fields["zone"]))).date().isoformat(),
                                "merge_branch": str(fields["merge_branch"])}
                    journal.parent.mkdir(parents=True, exist_ok=True)
                    write_text_atomic(journal, json.dumps(retained) + "\n")
                elif journal.exists():
                    retained = cast(dict[str, str], json.loads(journal.read_text(encoding="utf-8")))
                    if retained["session"] != row.current_session or retained["worktree"] != str(location.path):
                        raise ValueError("retirement retry facts belong to another conversation or worktree")
                else:
                    print(f"anomaly: {row.name}: worktree and retirement journal are absent; branch is unknown")
                    retained = {"checkout": str(location.path.parent), "branch": "", "config": ""}
                result["branch"] = retained["branch"]
                production = Production(Path(), "", retained.get("merge_branch", str(fields["merge_branch"])),
                                        Path(retained["checkout"]), Path(), ZoneInfo("UTC"), row)
                unit = RetiringUnit(production, row.name, location.path, retained["branch"], row)
                refused = retirement_refusals(unit, settings=settings, check_absent_branch=True)
                if refused:
                    print("\n".join(f"refused: {reason}" for reason in refused))
                else:
                    panes, reasons = retirement_stop_members(row, record)
                    for reason in reasons:
                        print(f"anomaly: {reason}")
                    if not reasons:
                        result["code"] = retirement_clear_worktree(unit, settings=settings, record=record,
                                                                  pane_workers=panes, pane=pane, require_cleanup=True)
                if result["code"] == 0 and retained["config"]:
                    config = unit_config.parse(retained["config"])
                    previous = unit_config.read_parked(row.id, cleanup=False)
                    facts = (previous.retired if isinstance(previous, unit_config.UnitConfig)
                             and isinstance(previous.retired, unit_config.RetiredSession) else
                             unit_config.RetiredSession(retained["date"], row.current_session))
                    _ = unit_config.write_parked(row.id, replace(config, retired=facts))
            else:
                raise ValueError("unknown remote lifecycle action")
        except (OSError, ValueError, herdr_types.HerdrFailure, unit_config.ConfigRefused, conf.Unusable, subprocess.SubprocessError) as error:
            result["code"] = 1
            print(f"anomaly: {row.name}: {' '.join(str(error).splitlines())}")
    result["output"] = output.getvalue()
    print(json.dumps(result))
    return result["code"]


def retirement_stop_members(row: staff.StaffRow, record: staff.StaffSnapshot) -> tuple[list[worker_panes.WorkerPane], list[str]]:
    reasons: list[str] = []
    owned = [row, *record.find(kind="worker", parent=row.id)]
    pane_workers = worker_panes.observe(row.id, record)
    pane_ids = {item.row.id for item in pane_workers}
    for member in owned:
        if member.id in pane_ids or (member.kind == "worker" and isinstance(record.backends.get(member.id), staff.CodexBackend)):
            continue
        try:
            outcome = stop_resume.stop(member.current_session, deadline=worker_lifecycle.effective_deadline())
            if isinstance(outcome, stop_resume.StopFailed):
                reasons.append(f"{member.name}: {outcome.reason}")
            elif isinstance(outcome, stop_resume.SessionNotFound):
                reasons.append(f"{member.name}: no session on this machine")
        except (OSError, ValueError, herdr_types.HerdrFailure) as error:
            reasons.append(f"{member.name}: {' '.join(str(error).splitlines())}")
    stop_reasons = stop_resume.stop_unit_workers(row.id, record)
    reasons.extend(stop_reasons)
    return pane_workers, reasons

class RetiringPane(NamedTuple):
    session: str
    pane: str
    terminal: str


def retirement_pane(row: staff.StaffRow, record: staff.StaffSnapshot) -> RetiringPane | herdr.NoPane:
    try:
        publication = pane_record.publication_for_session(row.current_session, record=record)
        session = publication.path.stem if publication is not None else herdr.shared_session()
        client = herdr.Herdr(session)
        entries = {publication.entry["pane_id"]: publication.entry} if publication is not None else {}
        matches = pane_launch.reference_panes(pane_launch.StaffLaunchReference(row.id), record, client, entries)
        if matches:
            pane = matches[0]
            identity = str(pane["pane_id"])
            terminal = str(pane.get("terminal_id", ""))
            if not terminal:
                raise ValueError(f"{session}:{identity}: terminal identity is unavailable")
            return RetiringPane(session, identity, terminal)
    except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
        print(f"anomaly: {row.name}: cannot read its pane: {error}")
    return herdr.NoPane(row.name)


def retirement_idle_shell(place: RetiringPane | herdr.NoPane, pid: int) -> bool:
    if isinstance(place, herdr.NoPane):
        return False
    try:
        return (pane_record.pane_shell_pid(herdr.HerdrPlace(place.session, place.pane)) == pid
                and isinstance(herdr.Herdr(place.session).pane_activity(place.pane), herdr.PaneAtShell))
    except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError):
        return False


def retirement_close_idle_shell(place: RetiringPane | herdr.NoPane,
                                owner: staff.StaffRow, record: staff.StaffSnapshot) -> None:
    owners = {owner.id, *(member.id for member in record.find(kind="worker", parent=owner.id))}
    retained: dict[str, set[str]] = {}
    publications: dict[str, dict[str, pane_launch.PaneLaunch]] = {}
    try:
        for path in pane_record.record_directory().glob("*.json"):
            entries = pane_record.read_strict(path.stem)
            publications[path.stem] = entries
            owned = {pane for pane, entry in entries.items()
                     if isinstance(reference := pane_launch.launch_reference(entry, record), pane_launch.StaffLaunchReference)
                     and reference.id in owners}
            if owned:
                retained[path.stem] = owned
    except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
        print(f"anomaly: {owner.name}: cannot read retained panes: {error}")
        return
    if isinstance(place, RetiringPane):
        retained.setdefault(place.session, set()).add(place.pane)
        _ = publications.setdefault(place.session, {})
    for session, identities in retained.items():
        try:
            client = herdr.Herdr(session)
            panes = {pane["pane_id"]: pane for pane in client.panes()}
        except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
            print(f"anomaly: {session}: pane close failed: {error}")
            continue
        for identity in sorted(identities, key=lambda identity: (
                isinstance(place, RetiringPane) and session == place.session and identity == place.pane, identity)):
            try:
                pane = panes.get(identity)
                if isinstance(place, RetiringPane) and session == place.session and identity == place.pane:
                    terminal = place.terminal
                else:
                    terminal = str(pane.get("terminal_id", "")) if pane is not None else ""
                if pane is None or pane.get("terminal_id") != terminal:
                    if not client.close_retained_pane(identity, terminal=terminal):
                        print(f"anomaly: {session}:{identity}: pane close is not confirmed")
                    continue
                launch = publications[session].get(identity)
                reference = pane_launch.launch_reference(launch, record) if launch is not None else None
                if reference is not None and (not isinstance(reference, pane_launch.StaffLaunchReference)
                                               or reference.id not in owners):
                    continue
                if (not isinstance(client.pane_activity(identity), herdr.PaneAtShell)
                        or pane_launch.native_sessions(client, pane)):
                    print(f"anomaly: {session}:{identity}: foreground work keeps the pane open")
                    continue
                if not client.close_retained_pane(identity, terminal=terminal):
                    print(f"anomaly: {session}:{identity}: pane close is not confirmed")
            except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
                print(f"anomaly: {session}:{identity}: pane close failed: {error}")

def retirement_clear_worktree(unit: RetiringUnit, *, settings: conf.ConfigRead, record: staff.StaffSnapshot,
                             pane_workers: list[worker_panes.WorkerPane], pane: RetiringPane | herdr.NoPane,
                             require_cleanup: bool = False) -> int:
    row = unit.staff_member
    owned = retirement_members(unit, record)
    for key in retirement_holds({member.id for member in owned}):
        result = retirement_run([sys.executable, str(BUILD_HOLD), "release", "--holder", key], settings=settings)
        if result.returncode:
            print(f"anomaly: {retirement_failure(result)}")
    retirement_release_reservations(unit, settings=settings)
    claims = retirement_live_claims(retirement_mac_claims(unit.worktree))
    for reason in retirement_terminate([claim.pid for claim in claims], {claim.pid: claim.what for claim in claims}, settings=settings):
        print(f"anomaly: {reason}")
    inside = unit_move.processes_inside(unit.worktree) if unit.worktree.exists() else []
    classified = retirement_run_processes(row, inside, record=record, settings=settings)
    for reason in retirement_terminate(list(classified), classified, settings=settings):
        print(f"anomaly: {reason}")
    for pid in inside:
        if pid != os.getpid() and pid not in classified and not retirement_idle_shell(pane, pid):
            for reason in retirement_stop_orphan(unit, pid, row.current_session, settings=settings):
                print(f"anomaly: {reason}")
    if ((require_cleanup and (retirement_holds({member.id for member in owned})
                              or retirement_live_claims(retirement_mac_claims(unit.worktree))))
            or (unit.worktree.exists() and any(not retirement_idle_shell(pane, pid)
                for pid in unit_move.processes_inside(unit.worktree)))):
        print(f"refused: live holds or processes remain in {unit.worktree}")
        return 1
    # Check the work again after the sessions stop.
    refused = retirement_refusals(unit, settings=settings, check_absent_branch=require_cleanup)
    if refused:
        print("\n".join(f"refused: {reason}" for reason in refused))
        return 1
    remaining = worker_panes.observe(row.id, record) if pane_workers else []
    if any(isinstance(item, (worker_panes.OpenWorker, worker_panes.UncertainWorker, worker_panes.ProcessUnknownWorker)) for item in remaining):
        return 1
    try:
        copies.remove_trees(str(row.id))
    except copies.RemoteMirrorKept as error:
        print(f"refused: {error}")
        return 1
    except copies.CopyError as error:
        print(f"anomaly: {error}".replace("\n", " "))
        if require_cleanup:
            return 1
    if (not isinstance(row.worktree, staff.WorktreeUnfilled)
            and not require_cleanup and unit_machine_is_local(row.worktree.machine) and unit.branch):
        retained = staff.set_worktree(row.id, staff.RemovedWorktree(row.worktree.machine, unit.worktree,
                                                                  staff.ParkedBranch(unit.branch)), expected=row.worktree)
        unit = replace(unit, staff_member=retained)
    failed = retirement_remove(unit, settings=settings)
    if failed:
        if (isinstance(row.worktree, staff.PresentWorktree) and unit.worktree.is_dir()
                and isinstance(unit.staff_member.worktree, staff.RemovedWorktree)):
            _ = staff.set_worktree(row.id, row.worktree, expected=unit.staff_member.worktree)
        print("\n".join(f"anomaly: {reason}" for reason in failed))
        return 1
    return 0

def retirement_retire(unit: RetiringUnit, *, settings: conf.ConfigRead,
           record: staff.StaffSnapshot | None = None) -> int:
    record = staff.snapshot() if record is None else record
    row = unit.staff_member
    if isinstance(row.worktree, staff.WorktreeUnfilled):
        raise ValueError(f"{row.id}: staff worktree is unfilled")
    if not unit_machine_is_local(row.worktree.machine):
        if isinstance(row.worktree, staff.RemovedWorktree):
            _ = unit_remote_command("retire-complete", row, record)
            print("unchanged")
            return 0
        outcome = unit_remote_command("retire", row, record, merge_branch=unit.production.merge_branch, zone=unit.production.zone.key)
        if outcome["code"]:
            return outcome["code"]
        branch = outcome.get("branch", unit.branch)
        retained_branch = staff.ParkedBranch(branch) if branch else staff.BranchUnrecorded()
        retired = staff.set_worktree(row.id, staff.RemovedWorktree(row.worktree.machine, row.worktree.path, retained_branch),
                                     expected=row.worktree)
        updated = staff.StaffSnapshot(tuple(retired if member.id == row.id else member for member in record.rows))
        _ = unit_remote_command("retire-complete", retired, updated)
        print(f"{unit.name} retired")
        return 0
    pane = retirement_pane(row, record)
    if isinstance(row.worktree, staff.RemovedWorktree) and not unit.worktree.exists():
        if unit.branch:
            refused = retirement_refusals(unit, settings=settings, check_absent_branch=True)
            if refused:
                print("\n".join(f"refused: {reason}" for reason in refused))
                return 1
            failed = retirement_remove(unit, settings=settings)
            if failed:
                print("\n".join(f"anomaly: {reason}" for reason in failed))
                return 1
        try:
            copies.remove_trees(str(row.id))
        except copies.CopyError as error:
            print(f"anomaly: {error}".replace("\n", " "))
        print("unchanged")
        retirement_close_idle_shell(pane, row, record)
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
                                            row.current_session))
        _ = unit_config.write_parked(row.id, replace(config, retired=facts))
    pane_workers, reasons = retirement_stop_members(unit.staff_member, record)
    for reason in reasons:
        print(f"anomaly: {reason}")
    if retirement_clear_worktree(unit, settings=settings, record=record, pane_workers=pane_workers,
                                 pane=pane):
        return 1
    retained_branch = staff.ParkedBranch(unit.branch) if unit.branch else staff.BranchUnrecorded()
    _ = staff.set_worktree(row.id, staff.RemovedWorktree(row.worktree.machine, unit.worktree, retained_branch),
                           expected=row.worktree)
    print(f"{unit.name} retired")
    retirement_close_idle_shell(pane, row, record)
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
        raise RecoveryFactsMissing(f"{name}: no .unit.toml in {worktree} or {unit_config.parked_path(member.row.id)}; "
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
    try:
        return git_files.locate(production.checkout).git_dir / "resume-units" / name
    except git_files.GitFilesError as error:
        raise RecoveryRefused(f"cannot find the resume record: {error}") from error

def recovery_git(unit: RetiredUnitRecovery, *arguments: str) -> str:
    result = retirement_run(["git", "-C", str(unit.production.checkout), *arguments])
    return result.stdout.strip() if result.returncode == 0 else ""

def recovery_has_worktree(unit: RetiredUnitRecovery) -> bool:
    """Whether the worktree path holds the unit's worktree on its branch."""
    return ((unit.worktree / ".git").is_file()
            and retirement_branch(unit.worktree) == unit.branch)

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
            and entry.get("staff_id") == row.id and session_id == row.current_session)

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
    shutdown_record.remove_started([str(row.id)])
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
            unit = retirement_find_unit(unit_production_name(row, record), str(row.id), record=record)
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
        with staff.row_operation(str(row.id)):
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
                shutdown_record.remove_started([str(completed.owner.id)])
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


@dataclass(frozen=True)
class KeepDocBranch:
    """The request changes no document path or branch."""


@dataclass(frozen=True)
class StagedDocumentBlob:
    mode: str
    object_id: str


@dataclass(frozen=True)
class NoStagedDocumentBlob:
    """The Git index has no staged document content to restore."""


@dataclass(frozen=True)
class DocBranchMove:
    source: Path
    destination: Path
    checkout: Path
    old_branch: str
    branch: str
    tracked: bool
    staged_blob: StagedDocumentBlob | NoStagedDocumentBlob


@dataclass(frozen=True)
class UnitBranchMove:
    old_branch: str
    branch: str


@dataclass(frozen=True)
class RenamePlan:
    member: staff.StaffRow
    name: str
    move: KeepDocBranch | DocBranchMove | UnitBranchMove


def rename_member(old: str, new: str, record: staff.StaffSnapshot) -> staff.StaffRow:
    references = (old, f"{old.rpartition('/')[0]}/{new}" if "/" in old else new)
    for reference in references:
        row = record.by_member_reference(reference)
        if row is None and "/" in reference:
            row = record.by_path(reference) or record.by_path(reference.removesuffix("-unit"))
        if row is not None:
            return row
    rows = [row for row in record.rows if row.kind in {"showrunner", "unit", "worker"}]
    caller = record.by_current_session(os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    local = ([row for row in rows if row.id == caller.id or row.parent == staff.ReportsTo(caller.id)]
             if caller is not None and caller.kind in {"showrunner", "unit"} else [])
    for scope in (local, rows):
        for reference in references:
            matches = [row for row in scope if row.name == reference or
                       row.kind == "unit" and row.name == reference.removesuffix("-unit")]
            if len(matches) > 1:
                raise staff.StaffRefused(f"{old}: several rows match; use showrunner/name or the staff id")
            if matches:
                return matches[0]
    raise staff.StaffRefused(f"{old}: no showrunner, unit, or worker staff row")


def rename_check_branch(checkout: Path, target: str) -> str:
    _ = unit_move.git(checkout, "check-ref-format", "--branch", target)
    branch = unit_move.checked_out_branch(checkout)
    if not branch:
        raise RenameRefused(f"{checkout}: the checkout has no current branch")
    if branch != target and unit_move.branch_tip(checkout, target):
        raise RenameRefused(f"branch already exists: {target}")
    return branch


def rename_plan(old: str, new: str, *, to_doc: str = "", to_branch: str = "") -> RenamePlan:
    """Read the row and any explicit move without changing files."""
    record = staff.snapshot()
    row = rename_member(old, new, record)
    if row.kind == "worker":
        staff.validate_name(row.kind, new)
        return RenamePlan(row, new, KeepDocBranch())
    if row.kind not in {"showrunner", "unit"}:
        raise staff.StaffRefused(f"{old}: the staff row is a {row.kind}, not a showrunner")
    if row.kind == "unit":
        if to_doc:
            raise RenameRefused("--to-doc is only for showrunners; the staff row is a unit, not a showrunner")
        name = unit_names.name(new)
        staff.validate_name(row.kind, name)
        if any(other.id != row.id and other.name == name and other.parent == row.parent
               for other in record.find(kind="unit")):
            raise staff.StaffRefused(f"unit name already exists: {name}")
        if ((row.name != name or to_branch) and not isinstance(row.worktree, staff.WorktreeUnfilled)
                and row.worktree.machine != remote.local_machine()):
            raise ValueError(f"{old}: run rename on {row.worktree.machine}")
        pending = unit_move.pending_rename(row.id)
        if pending is not None and (pending.name != name or to_branch and pending.branch != to_branch):
            raise RenameRefused(f"{row.name}: finish the pending rename to {pending.name}")
        if to_branch:
            if isinstance(row.worktree, staff.WorktreeUnfilled):
                raise RenameRefused(f"{old}: no unit worktree")
            checkout = unit_move.move_checkout(record, row)
            return RenamePlan(row, name, UnitBranchMove(rename_check_branch(checkout, to_branch), to_branch))
        return RenamePlan(row, name, KeepDocBranch())
    if bool(to_doc) != bool(to_branch):
        raise RenameRefused("--to-doc and --to-branch are required together")
    staff.validate_name(row.kind, new)
    if any(other.id != row.id and other.name == new
           for other in record.find(kind="showrunner")):
        raise staff.StaffRefused(f"{new}: a showrunner already has this name")
    if not to_doc:
        return RenamePlan(row, new, KeepDocBranch())
    doc = row.doc
    if isinstance(doc, staff.DocUnfilled):
        raise RenameRefused(f"{row.name}: the staff document is unfilled")
    if doc.machine != remote.local_machine():
        raise RenameRefused(f"{row.name}: run the document move on {doc.machine}")
    destination = Path(to_doc).expanduser().resolve()
    checkout = unit_move.checkout_folders(doc.path.parent).toplevel
    _ = destination.relative_to(checkout)
    branch = rename_check_branch(checkout, to_branch)
    if doc.path != destination and doc.path.exists() and unit_move.exists(destination):
        raise RenameRefused(f"destination already exists: {destination}")
    if doc.path == destination and branch == to_branch:
        return RenamePlan(row, new, KeepDocBranch())
    source = str(doc.path.relative_to(checkout))
    target = str(destination.relative_to(checkout))
    entry = unit_move.git(checkout, "ls-files", "-s", "--", source)
    if doc.path != destination and not doc.path.exists() and unit_move.exists(destination):
        target_entry = unit_move.git(checkout, "ls-files", "-s", "--", target)
        if unit_move.git(checkout, "ls-tree", "HEAD", "--", target):
            raise RenameRefused(f"destination already exists: {destination}")
        if entry and target_entry:
            if entry.split(maxsplit=3)[:3] != target_entry.split(maxsplit=3)[:3]:
                raise RenameRefused(f"destination already exists: {destination}")
        elif not entry:
            changes = unit_move.git(checkout, "diff", "--cached", "--name-status", "-z", "--find-renames",
                                    "--", source, target).split("\0")
            if len(changes) != 4 or not changes[0].startswith("R") or changes[1:3] != [source, target]:
                raise RenameRefused(f"destination already exists: {destination}")
        entry = entry or target_entry
    staged_blob: StagedDocumentBlob | NoStagedDocumentBlob = NoStagedDocumentBlob()
    parts = entry.split(maxsplit=3)
    if len(entry.splitlines()) == 1 and len(parts) == 4 and parts[2] == "0":
        staged_blob = StagedDocumentBlob(parts[0], parts[1])
    return RenamePlan(row, new, DocBranchMove(doc.path, destination, checkout, branch, to_branch, bool(entry), staged_blob))


def rename_move_doc(request: RenamePlan, move: DocBranchMove) -> bool:
    changed = False
    source = str(move.source.relative_to(move.checkout))
    destination = str(move.destination.relative_to(move.checkout))
    document = move.source if move.source.exists() else move.destination
    if not document.is_file():
        raise RenameRefused(f"{document}: the document is unavailable")
    if move.source != move.destination and move.source.exists():
        if unit_move.exists(move.destination):
            raise RenameRefused(f"destination already exists: {move.destination}")
        move.destination.parent.mkdir(parents=True, exist_ok=True)
        if move.tracked:
            _ = unit_move.git(move.checkout, "mv", "--", source, destination)
        else:
            _ = move.source.rename(move.destination)
        changed = True
    if not move.destination.is_file():
        raise RenameRefused(f"{move.destination}: the moved document is unavailable")
    if move.tracked and move.source != move.destination:
        if not unit_move.git(move.checkout, "ls-files", "--", destination):
            if isinstance(move.staged_blob, StagedDocumentBlob):
                _ = unit_move.git(move.checkout, "update-index", "--add", "--cacheinfo",
                                  f"{move.staged_blob.mode},{move.staged_blob.object_id},{destination}")
            else:
                _ = unit_move.git(move.checkout, "add", "--", destination)
            changed = True
        if unit_move.git(move.checkout, "ls-files", "--", source):
            _ = unit_move.git(move.checkout, "rm", "--cached", "--quiet", "--", source)
            changed = True
    row = staff.by_id(request.member.id)
    if row is None or isinstance(row.doc, staff.DocUnfilled):
        raise RenameRefused(f"{request.member.id}: the staff document is unavailable")
    if row.doc.path not in (move.source, move.destination):
        raise RenameRefused(f"{row.id}: the staff document changed during the move")
    if row.doc.path != move.destination:
        _ = staff.set_doc(row.id, replace(row.doc, path=move.destination))
        changed = True
    return changed


def rename_move_branch(move: DocBranchMove) -> bool:
    branch = rename_check_branch(move.checkout, move.branch)
    if branch == move.branch:
        return False
    if branch != move.old_branch:
        raise RenameRefused(f"{move.checkout}: the current branch changed during the move")
    _ = unit_move.git(move.checkout, "branch", "-m", move.branch)
    return True


def rename_qualified_workers(owner: staff.StaffRow) -> bool:
    """Migrate one owner's old worker names without removing retained work."""
    changed = False
    client = herdr.Herdr(herdr.shared_session())
    for row in staff.snapshot().find(kind="worker", parent=owner.id):
        prefix, separator, bare = row.name.rpartition("-")
        stem = row.name.rstrip("0123456789")
        for role in staff.WORKER_ROLES:
            if "-" in role and (stem == role or stem.endswith("-" + role)):
                bare = role + row.name[len(stem):]
                prefix = row.name[:-len(bare)].removesuffix("-")
                break
        if not prefix or not separator:
            continue
        if prefix != owner.name and not prefix.endswith("-" + owner.name):
            print(f"{row.name}: kept; prefix does not end with {owner.name}; run /agents rename {row.id} {bare}", file=sys.stderr)
            continue
        name = staff.WorkerName.parse(row.name)
        with worker_panes.worker_lock(owner.id, name, blocking=True) as hold:
            record = staff.snapshot()
            twins = [other for other in record.find(kind="worker", parent=owner.id) if other.name == bare]
            pair = (f"{row.name} ({row.id}) and {twins[0].name} ({twins[0].id}): kept; "
                    if twins else f"{row.name} ({row.id}): kept; ")
            if isinstance(hold, worker_panes.Kept):
                print(pair + hold.blocker, file=sys.stderr)
                continue
            observed = worker_panes.observe_worker(owner.id, row, record)
            if not twins:
                if not isinstance(observed, (worker_panes.OpenWorker, worker_panes.StoppedWorker, worker_panes.ClosedWorker)):
                    reason = observed.value if isinstance(observed, worker_panes.WorkerNotObserved) else observed.reason
                    print(pair + " ".join(reason.splitlines()), file=sys.stderr)
                    continue
                if isinstance(observed, (worker_panes.OpenWorker, worker_panes.StoppedWorker)):
                    title = worker_panes.agent_name(owner, bare)
                    if isinstance(observed, worker_panes.OpenWorker):
                        _ = client.run("agent", "rename", observed.pane, title)
                    _ = client.retitle(observed.pane, title, tab=False)
                _ = staff.rename(row.id, bare)
                changed = True
                continue
            if not isinstance(observed, worker_panes.ClosedWorker):
                print(pair + "the old pane is not closed", file=sys.stderr)
                continue
            try:
                entries = pane_launch.read(pane_launch.record_directory() / f"{client.session}.json")
                retained = pane_launch.reference_panes(pane_launch.StaffLaunchReference(row.id), record, client, entries)
            except (OSError, ValueError, herdr_types.HerdrFailure) as error:
                print(pair + " ".join(str(error).splitlines()), file=sys.stderr)
                continue
            if retained:
                print(pair + "the old pane is not closed", file=sys.stderr)
                continue
            runs = [Path(run) for run in unit_runs.held(str(owner.current_session),
                                                       os.environ.get("ROSTER_RUN_ROOT"), record=record)]
            runs.append(worker_panes.worker_run(row, record=record))
            for run in dict.fromkeys(runs):
                dispatch = progress_history.active_dispatch(row.id, run)
                if isinstance(dispatch, progress_history.ActiveDispatch):
                    print(pair + f"active dispatch {dispatch.instance_id}", file=sys.stderr)
                    break
                if isinstance(dispatch, progress_history.ActiveDispatchUnavailable):
                    print(pair + dispatch.reason, file=sys.stderr)
                    break
            else:
                try:
                    if worker.end(owner.id, name, lock=hold) == worker.ExitCode.OK:
                        changed = True
                    else:
                        print(pair + "worker end is incomplete", file=sys.stderr)
                except staff.StaffRefused as error:
                    print(pair + " ".join(str(error).splitlines()), file=sys.stderr)
    return changed


def rename_owner(row: staff.StaffRow, name: str) -> staff.StaffRow:
    """Keep the old owner name until every worker pane has its new name."""
    client = herdr.Herdr(herdr.shared_session())
    if row.name != name:
        client.mark_tab_rename(row.id)
    worker_panes.rename_workers(replace(row, name=name), client)
    return staff.rename(row.id, name) if row.name != name else row


def rename_repair_displays(row: staff.StaffRow) -> bool:
    """Repair the current session name and its pane, tab, and workspace labels."""
    changed = False
    for session in sessions.running_sessions():
        if session["sessionId"] != row.current_session or session["name"] == row.name:
            continue
        path = sessions.sessions_dir() / f"{session['pid']}.json"
        raw = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
        current = sessions.parse_session(raw)
        if (isinstance(current, sessions.UnreadableSessionRecord)
                or current["sessionId"] != row.current_session or current["pid"] != session["pid"]
                or current.get("procStart") != session.get("procStart") or not sessions.running_session(current)):
            raise ValueError(f"{row.name}: the session changed during rename")
        if current["name"] == row.name:
            continue
        raw["name"] = row.name
        raw["formerNames"] = list(dict.fromkeys([*current["formerNames"], current["name"]]))
        write_text_atomic(path, json.dumps(raw, indent=2) + "\n")
        changed = True
    client = herdr.Herdr(herdr.shared_session())
    spaces: set[str] = set()
    for pane in client.panes():
        native_sessions: set[str] = set()
        for kind in ("claude", "codex"):
            try:
                native_sessions.add(client.native_session(pane["pane_id"], kind, herdr.PROGRAM_TIMEOUT_SECONDS))
            except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError):
                continue
        if native_sessions != {row.current_session}:
            if pane.get("session_id") == row.current_session:
                print(f"anomaly: pane {pane['pane_id']} does not run session {row.current_session}", file=sys.stderr)
                changed = True
            continue
        changed = client.retitle(pane["pane_id"], row.name) or changed
        spaces.add(pane["workspace_id"])
    if row.kind == "showrunner" and spaces:
        for space in client.listed("workspace"):
            if space.get("workspace_id") in spaces and space.get("label") != row.name:
                _ = client.run("workspace", "rename", str(space["workspace_id"]), row.name)
                changed = True
        if changed:
            client.save_layout()
    return changed


def rename_apply(old: str, new: str, *, to_doc: str = "", to_branch: str = "") -> RenamePlan:
    """Rename worker panes before the owner, then repair the other displays."""
    request = rename_plan(old, new, to_doc=to_doc, to_branch=to_branch)
    if request.member.kind == "unit":
        return rename_apply_unit(request)
    with staff.operation():
        if request.member.kind == "worker":
            _ = staff.rename(request.member.id, request.name)
            print("done" if request.member.name != request.name else "unchanged")
            return request
        move = request.move
        row = request.member
        changed = row.name != request.name
        if not changed:
            changed = rename_qualified_workers(row)
        row = rename_owner(row, request.name)
        if isinstance(move, DocBranchMove):
            try:
                changed = rename_move_doc(request, move) or changed
                changed = rename_move_branch(move) or changed
            except (OSError, ValueError, staff.StaffRefused, staff.StaffNotFound) as error:
                print(f"anomaly: document_move: {error}", file=sys.stderr)
                changed = True
        try:
            changed = rename_repair_displays(row) or changed
        except (OSError, ValueError, staff.StaffRefused, staff.StaffNotFound, herdr_types.HerdrFailure) as error:
            print(f"anomaly: set_displays: {error}", file=sys.stderr)
            changed = True
        print("done" if changed else "unchanged")
        return request


def rename_apply_unit(request: RenamePlan) -> RenamePlan:
    with staff.row_operation(str(request.member.id)):
        record = staff.snapshot()
        if (row := record.by_id(request.member.id)) is None:
            raise ValueError(f"{request.member.id}: no unit staff row")
        changed = row.name != request.name
        pending = unit_move.pending_rename(row.id)
        if pending is not None:
            if (pending.name != request.name or isinstance(request.move, UnitBranchMove)
                    and request.move.branch != pending.branch):
                raise RenameRefused(f"{row.name}: finish the pending rename to {pending.name}")
            _ = unit_move.rename_observation(record, row, pending)
        elif changed or (isinstance(request.move, UnitBranchMove) and request.move.old_branch != request.move.branch):
            if isinstance(row.worktree, staff.WorktreeUnfilled):
                print(f"anomaly: {request.name}: the staff worktree is unfilled", file=sys.stderr)
            else:
                if row.worktree.machine != remote.local_machine():
                    raise ValueError(f"{row.name}: run rename on {row.worktree.machine}")
                folder = row.worktree.path.expanduser().resolve()
                target = unit_names.swap_stem(folder.name, row.name, request.name) if changed else folder.name
                destination = folder.with_name(target) if target is not None else folder
                unit_move.require_move_destination(record, row, destination)
                checkout = unit_move.move_checkout(record, row)
                branch = unit_move.checked_out_branch(checkout) if checkout.exists() else ""
                renamed_branch = (request.move.branch if isinstance(request.move, UnitBranchMove) else
                                  unit_names.swap_stem(branch, row.name, request.name))
                if changed and target is None:
                    print(f"anomaly: folder {folder} does not end with -{row.name}", file=sys.stderr)
                if changed and renamed_branch is None:
                    print(f"anomaly: branch {branch} does not end with -{row.name}", file=sys.stderr)
                target_branch = renamed_branch if renamed_branch is not None else unit_move.KeepBranch.CURRENT
                if (destination != folder or renamed_branch is not None and renamed_branch != branch
                        or checkout.exists() and unit_move.move_locked(row.id, checkout)):
                    pending = unit_move.prepare_rename(row, destination, target_branch, request.name)
        worker_names_changed = rename_qualified_workers(row) if not changed else False
        rename_prepare_notice(row, request.name)
        row = rename_owner(row, request.name)
        try:
            changed = rename_repair_displays(row) or changed
        except (OSError, ValueError, herdr_types.HerdrFailure) as error:
            print(f"anomaly: display: {error}", file=sys.stderr)
            changed = True
        changed = rename_notify_unit(row) or changed
        if pending is None and isinstance(request.move, UnitBranchMove):
            if not isinstance(row.worktree, staff.WorktreeUnfilled):
                changed = unit_move.move_idle_folder(row.id, row.worktree.path, branch=request.move.branch) or changed
        if pending is not None:
            if not unit_move.rename_complete(staff.snapshot(), row, pending):
                changed = unit_move.move_idle_folder(row.id, pending.destination, branch=pending.branch) or changed
                record = staff.snapshot()
                if (current := record.by_id(row.id)) is None:
                    raise RenameRefused(f"{row.id}: the staff row is unavailable after the rename")
                row = current
            if unit_move.finish_rename(row, pending):
                pending = None
                changed = True
        notice = rename_notice_path(row.id)
        if notice.exists() and pending is None:
            fields = unit_move.json_object(read_json(notice))
            if not fields.get("pending"):
                notice.unlink()
        print("done" if changed or worker_names_changed else "unchanged")
        return request


def rename_notice_path(identity: staff.StaffId) -> Path:
    """Select the delivery state for one unit."""
    root = Path(os.environ.get("SHOWRUNNER_STATE_DIR") or Path.home() / ".local/state/showrunner")
    return root / identity / "rename-notices.json"


def rename_prepare_notice(row: staff.StaffRow, name: str) -> None:
    """Retain the recipients before the staff name changes."""
    if row.name == name:
        return
    path = rename_notice_path(row.id)
    record = staff.snapshot()
    members = [row, *record.find(kind="worker", parent=row.id)]
    write_json(path, {"key": f"rename:{row.id}:{time.time_ns()}",
                      "pending": [str(member.id) for member in members]})


def rename_notify_unit(row: staff.StaffRow) -> bool:
    """Send one rename notice to each retained recipient through the shared send path."""
    path = rename_notice_path(row.id)
    if not path.exists():
        return False
    saved = unit_move.json_object(read_json(path))
    fields = {key: saved[key] for key in ("key", "pending") if key in saved}
    changed = fields != saved
    if changed:
        write_json(path, fields)
    pending = fields.get("pending")
    if not isinstance(pending, list):
        return False
    recipients = [recipient for recipient in cast(list[object], pending) if isinstance(recipient, str)]
    text = f"The unit director is now named {row.name}. Continue the current work."
    for recipient in recipients.copy():
        try:
            rename_send_notice(recipient, str(fields["key"]), text)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            detail = " ".join(str(error).splitlines())
            print(f"anomaly: {row.name}: rename message to {recipient}: {detail}", file=sys.stderr)
            changed = True
            continue
        recipients.remove(recipient)
        fields["pending"] = recipients
        write_json(path, fields)
        changed = True
    return changed


def rename_send_notice(recipient: str, key: str, text: str) -> None:
    """Use the shared transport with a sender that does not request shutdown."""
    result = subprocess.run(session_send.send_argv("--to", recipient, "--from", "agents rename",
        "--summary", "Unit director renamed", "--key", key,
        "--repeat-minutes", str(100 * 365 * 24 * 60)),
        input=text, capture_output=True, text=True, check=False, timeout=send_budget.seconds())
    outcome = send_result.send_outcome(result.returncode, result.stdout)
    if (not isinstance(outcome, send_result.ConfirmedSendOutcome)
            or outcome.header.outcome not in {"sent", "queued", "skipped", "kept", "replaced"}):
        lines = (result.stderr or result.stdout).splitlines()
        raise RuntimeError(lines[0] if lines else "rename message has no confirmed send result")


def rename_named_member(old: str, new: str, *, to_doc: str = "", to_branch: str = "") -> int:
    def run() -> int:
        try:
            _ = rename_apply(old, new, to_doc=to_doc, to_branch=to_branch)
        except worker_lifecycle.OperationDeadlineExpired as error:
            print(f"rename: anomaly: {error}", file=sys.stderr)
            return 0
        except subprocess.TimeoutExpired as error:
            print(f"rename: error: {str(error).splitlines()[0]}", file=sys.stderr)
            return 1
        except (ValueError, OSError, UnicodeError, RenameRefused, herdr_types.HerdrFailure) as error:
            print(f"rename: {error}", file=sys.stderr)
            return 1
        return 0
    return staff.cli_result(run)


if __name__ == "__main__":
    raise SystemExit(main())
