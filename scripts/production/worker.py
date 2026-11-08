#!/usr/bin/env python3
"""Select staff workers and dispatch work through their native conversations."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections.abc import Generator
from contextlib import ExitStack, closing, redirect_stderr
from dataclasses import dataclass, replace
import heapq
import io
import json
import os
import socket
import time
from pathlib import Path
import subprocess
import sys
from typing import Literal, cast, final
from uuid import UUID

from scripts.agents import codex_mesh, mesh_runs
from scripts.accounts import accounts, role_map
from scripts.direct import board_json, progress_history
from scripts.herdr import worker_panes
from scripts.lib import atomic_file, conf, unit_runs
from scripts.lib.resume_message import BusyPass, IdleWorker, NoOwnPass, NO_OWN_PASS, PaneWorkerResume, ResumeWorkers, launcher_prompt
from scripts.lib.script_envelope import script_prompt
from scripts.production import session_staff, staff, worker_admission
from scripts.shutdown import member_account

from scripts.herdr import herdr

StaffId = staff.StaffId
WorkerName = staff.WorkerName
WorkerRole = staff.WorkerRole
ExitCode = worker_panes.ExitCode


@dataclass(frozen=True)
class DispatchNotSubmitted:
    """The complete transcript contains no prompt for this dispatch."""


@dataclass(frozen=True)
class DispatchSubmitted:
    """The transcript contains the prompt without a matching final reply."""


@dataclass(frozen=True)
class DispatchComplete:
    reply: str


@dataclass(frozen=True)
class DispatchEvidenceUnknown:
    reason: str


@dataclass(frozen=True)
class DispatchWaitingForRuns:
    runs: tuple[str, ...]
    has_live_run: bool
    reply: DispatchSubmitted | DispatchComplete | DispatchAwaitingReply


@dataclass(frozen=True)
class DispatchAwaitingReply:
    """A detached result has no worker reply yet."""


DispatchReceipt = (DispatchNotSubmitted | DispatchSubmitted | DispatchComplete | DispatchEvidenceUnknown
                   | DispatchWaitingForRuns | DispatchAwaitingReply)


@dataclass(frozen=True)
class ReplyBesidePrompt:
    """Write the reply beside the prompt when no destination is supplied."""


@dataclass(frozen=True)
class SingleStageReview:
    """Complete the dispatch without a second review stage."""


REPLY_BESIDE_PROMPT = ReplyBesidePrompt()
SINGLE_STAGE_REVIEW = SingleStageReview()


@dataclass(frozen=True)
class DoneReply:
    dispatch_id: str


@dataclass(frozen=True)
class FinalReply:
    """Accept the final message after the exact question."""


@dataclass(frozen=True)
class TranscriptReceipt:
    receipt: DispatchNotSubmitted | DispatchSubmitted | DispatchComplete | DispatchEvidenceUnknown | DispatchAwaitingReply
    received_runs: frozenset[str]


def transcript_receipt(source: Path, line: str, completion: DoneReply | FinalReply) -> TranscriptReceipt:
    """Read prompt acceptance and its reply through the shared transcript parser."""
    submitted = False
    responding = False
    incomplete = False
    received: set[str] = set()
    reply: DispatchSubmitted | DispatchComplete | DispatchAwaitingReply = DispatchSubmitted()
    try:
        for entry in worker_admission.transcript_entries(source, set()):
            if isinstance(entry, worker_admission.TornTranscriptRecord):
                incomplete = True
                continue
            if entry.startswith("user:\n"):
                text = entry.removeprefix("user:\n").strip()
                results = {part.removeprefix("Detached run: ") for part in text.splitlines()
                           if part.startswith("Detached run: ")}
                received.update(results)
                if text in (line.strip(), script_prompt(line).strip()):
                    submitted = True
                    responding = True
                    reply = DispatchSubmitted()
                elif isinstance(completion, FinalReply):
                    responding = False
                    reply = DispatchSubmitted()
                elif "Read the unchanged prompt file" in text and "End your final reply with DONE " in text:
                    responding = False
                elif submitted and responding and results:
                    reply = DispatchAwaitingReply()
            elif responding and entry.startswith("assistant:\n"):
                text = entry.removeprefix("assistant:\n")
                complete = (bool(text.strip()) if isinstance(completion, FinalReply) else
                            text.rstrip().splitlines()[-1:] == [f"DONE {completion.dispatch_id}"])
                reply = DispatchComplete(text) if complete else DispatchSubmitted()
    except (OSError, ValueError) as error:
        return TranscriptReceipt(DispatchEvidenceUnknown(str(error)), frozenset(received))
    if incomplete and not submitted:
        return TranscriptReceipt(DispatchEvidenceUnknown("worker transcript has incomplete records"), frozenset(received))
    return TranscriptReceipt(reply if submitted else DispatchNotSubmitted(), frozenset(received))


def consultation_reply(client: herdr.Herdr, pane: str, source: Path, question: str) -> str:
    """Require acceptance before the turn end and its final reply."""
    deadline = time.monotonic() + herdr.PROGRAM_TIMEOUT_SECONDS
    receipt = transcript_receipt(source, question, FinalReply()).receipt
    while True:
        accepted = isinstance(receipt, (DispatchSubmitted, DispatchComplete))
        if not accepted and time.monotonic() >= deadline:
            raise ValueError("question was not accepted")
        ended = client.wait_turn_end(pane, int(herdr.PROGRAM_TIMEOUT_SECONDS * 1000))
        if not accepted:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.1, remaining))
        receipt = transcript_receipt(source, question, FinalReply()).receipt
        if isinstance(receipt, DispatchEvidenceUnknown):
            raise ValueError(receipt.reason)
        if accepted and isinstance(ended, herdr.ObservedAgentState):
            if isinstance(receipt, DispatchComplete):
                return receipt.reply
            raise ValueError("no review was written")


def dispatch_receipt(row: staff.StaffRow, line: str, dispatch_id: str, *, run: Path) -> DispatchReceipt:
    """Read the shared receipt and require delivery of detached results."""
    backend = staff.backend(row)
    if isinstance(backend, staff.BackendUnavailable):
        return DispatchEvidenceUnknown("worker transcript backend is unavailable")
    transcript = transcript_receipt(worker_admission.transcript_source(row, backend), line, DoneReply(dispatch_id))
    reply = transcript.receipt
    if isinstance(reply, (DispatchNotSubmitted, DispatchEvidenceUnknown)):
        return reply
    received = transcript.received_runs
    passed = worker_panes.worker_pass(row, run)
    started = passed.get("started_at", 0)
    since = float(started) if isinstance(started, (int, float)) else 0
    directory = run / codex_mesh.DETACHED_RUNS_DIR
    pending = mesh_runs.delivered_worker_results(directory, row.name, since=since) - received
    live = mesh_runs.live_runs(directory, row.name)
    pending.update(record["run_id"] for record in live)
    return DispatchWaitingForRuns(tuple(sorted(pending)), bool(live), reply) if pending else reply


def _client() -> herdr.Herdr:
    return herdr.Herdr(herdr.shared_session())


def _agent_command(client: herdr.Herdr, worker: worker_panes.OpenWorker, action: str, line: str = "") -> None:
    if not herdr.check_target(str(worker.row.current_session), action="keys",
            place=herdr.HerdrPlace(client.session, worker.pane), terminal=worker.terminal, text=line, client=client):
        raise herdr.HerdrFailure(f"{worker.row.name}: dispatch target is unproved", "wrong_target")
    arguments = ["agent", action, worker.pane]
    if action == "prompt":
        arguments.extend((script_prompt(line), "--wait"))
    result = subprocess.run(client.argv(*arguments), text=True, capture_output=True, check=False)
    herdr.require_success(result, f"herdr agent {action}")


def startup_result(name: WorkerName, result: worker_panes.WorkerStart) -> ExitCode:
    if isinstance(result, worker_panes.OpenWorker):
        return ExitCode.OK
    if isinstance(result, worker_panes.AdmissionRefused):
        print(f"staff: {name.value}: not opened: {result.reason}", file=sys.stderr)
        return ExitCode.REFUSED
    if isinstance(result, worker_panes.StartRefused):
        print(f"{result.reason.value}: {result.detail}", file=sys.stderr)
        return ExitCode.REFUSED
    if isinstance(result, worker_panes.IncompleteStart):
        print(f"{name.value}: incomplete startup: {result.reason}", file=sys.stderr)
    elif isinstance(result, worker_panes.UnpublishedWorker):
        print(f"{name.value}: publication failed: {result.cause}; pane {result.pane} retained", file=sys.stderr)
    # The shared scan already reports unknown and structural evidence.
    return ExitCode.UNCERTAIN


def _save_reply(path: Path, reply: str) -> None:
    text = reply.rstrip() + "\n"
    try:
        if path.read_text(encoding="utf-8") == text:
            return
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_file.write_text_atomic(path, text)


@dataclass(frozen=True)
class StartAtDispatch:
    """Start the worker while dispatch holds its lock."""


START_AT_DISPATCH = StartAtDispatch()


def dispatch(parent: StaffId, name: WorkerName, role: WorkerRole, prompt: Path, dispatch_id: str = "", *,
             session_dir: Path, slot: str, phase: str, kind: str, launcher_command: list[str],
             lens: str = "", reply: Path | ReplyBesidePrompt = REPLY_BESIDE_PROMPT, fix_cause: str = "",
             fix_pass: int = 0, activity: str = "", early_ready: Path | SingleStageReview = SINGLE_STAGE_REVIEW,
             policy: worker_panes.LaunchPolicy | None = None,
             started: worker_panes.OpenWorker | StartAtDispatch = START_AT_DISPATCH) -> ExitCode:
    """Hold one worker through startup, pass recording, submission, and completion."""
    prompt = prompt.resolve()
    with worker_panes.worker_lock(parent, name) as hold:
        if isinstance(hold, worker_panes.Kept):
            print(hold.blocker, file=sys.stderr)
            return ExitCode.BUSY
        if isinstance(started, StartAtDispatch):
            current_start = worker_panes.start_or_resume(parent, name, role, lock=hold, policy=policy)
        else:
            if not started.row.reports_to(parent) or started.row.name != name.value or not hold.owns(started.row):
                print("dispatch startup does not belong to this worker", file=sys.stderr)
                return ExitCode.REFUSED
            current_start = started
        if not isinstance(current_start, worker_panes.OpenWorker):
            return startup_result(name, current_start)
        started = current_start
        instance = progress_history.begin_dispatch(session_dir, slot=slot, phase=phase, role=role,
            worker_name=name.value, worker_id=started.row.id, launcher_command=launcher_command, prompt=prompt, kind=kind, lens=lens,
            fix_cause=fix_cause, fix_pass=fix_pass, activity=activity)
        if dispatch_id and dispatch_id not in (instance, worker_panes.stage_two_completion_id(instance)):
            print("dispatch id differs from the recorder pass", file=sys.stderr)
            return ExitCode.REFUSED
        dispatch_id = dispatch_id or instance
        line = (f"Read the unchanged prompt file {json.dumps(str(prompt))}. End your final reply with DONE {dispatch_id}. "
                "A reply that waits on a detached run is not final.")
        destination = reply if isinstance(reply, Path) else prompt.with_name(prompt.stem + "_reply.txt")
        client = _client()
        result = _dispatch_turn(parent, started, client, line, dispatch_id, run=session_dir)
        if not isinstance(result, DispatchComplete):
            return result
        if isinstance(early_ready, Path):
            _wait_for_ready(early_ready)
            second_id = worker_panes.stage_two_completion_id(instance)
            second_line = (f"Stage 2: the final diff is at {json.dumps(str(early_ready).removesuffix('.ready') + '.diff')}. "
                f"Read the unchanged prompt file {json.dumps(str(prompt))}. End your final reply with DONE {second_id}. "
                "A reply that waits on a detached run is not final.")
            record = staff.snapshot()
            current = next((state for state in worker_panes.observe(parent, record) if state.row.id == started.row.id), None)
            if not isinstance(current, worker_panes.OpenWorker):
                return ExitCode.UNCERTAIN
            result = _dispatch_turn(parent, current, client, second_line, second_id, run=session_dir)
            if not isinstance(result, DispatchComplete):
                return result
        _save_reply(destination, result.reply)
        return ExitCode.OK


def _wait_for_ready(ready: Path) -> None:
    watch = codex_mesh.LinuxFileChanges({ready}) if sys.platform.startswith("linux") else codex_mesh.KqueueFileChanges({ready})
    try:
        wake, cancel = socket.socketpair()
        with wake, cancel:
            while not ready.exists():
                _ = watch.wait(wake, time.monotonic() + herdr.PROGRAM_TIMEOUT_SECONDS)
    finally:
        watch.close()


def _wait_for_dispatch(row: staff.StaffRow, line: str, dispatch_id: str, run: Path) -> bool:
    backend = staff.backend(row)
    if isinstance(backend, staff.BackendUnavailable):
        return False
    directory = run / codex_mesh.DETACHED_RUNS_DIR
    paths = {worker_admission.transcript_source(row, backend), *directory.glob("*.json")}
    watch = codex_mesh.LinuxFileChanges(paths) if sys.platform.startswith("linux") else codex_mesh.KqueueFileChanges(paths)
    try:
        wake, cancel = socket.socketpair()
        with wake, cancel:
            if isinstance(dispatch_receipt(row, line, dispatch_id, run=run),
                          (DispatchWaitingForRuns, DispatchAwaitingReply)):
                return isinstance(watch.wait(wake, time.monotonic() + herdr.PROGRAM_TIMEOUT_SECONDS),
                                  codex_mesh.StateFilesChanged)
            return True
    finally:
        watch.close()


def _dispatch_turn(parent: StaffId, started: worker_panes.OpenWorker, client: herdr.Herdr,
                   line: str, dispatch_id: str, *, run: Path) -> DispatchComplete | ExitCode:
    name = WorkerName.parse(started.row.name)
    current = started
    receipt = dispatch_receipt(current.row, line, dispatch_id, run=run)
    if isinstance(receipt, DispatchEvidenceUnknown):
        print(f"anomaly: {name.value}: {receipt.reason}; no prompt sent", file=sys.stderr)
        return ExitCode.UNCERTAIN
    if isinstance(receipt, DispatchComplete) and worker_panes.worker_pass(started.row, run).get("status") == "completed":
        return receipt
    if isinstance(receipt, DispatchNotSubmitted):
        if current.status == herdr.AgentStatus.WORKING:
            seconds = conf.read("direct").number("WORKER_SETTLE_SECONDS")
            try:
                settled = client.wait_turn_end(current.pane, int(seconds * 1000))
            except (herdr.HerdrFailure, subprocess.TimeoutExpired) as error:
                return _delivery_failure(error)
            if isinstance(settled, herdr.ObservedAgentState):
                current = replace(current, status=settled.status)
        if current.status not in (herdr.AgentStatus.IDLE, herdr.AgentStatus.DONE):
            print(f"{name.value}: agent is {current.status.value}", file=sys.stderr)
            return ExitCode.BUSY
        try:
            _agent_command(client, current, "prompt", line)
        except (herdr.HerdrFailure, subprocess.TimeoutExpired) as error:
            return _delivery_failure(error)
    last_wait_was_quiet = False
    while True:
        record = staff.snapshot()
        row = record.by_id(started.row.id)
        if row is None:
            print(f"anomaly: {name.value}: worker row is unavailable", file=sys.stderr)
            return ExitCode.UNCERTAIN
        state = next((item for item in worker_panes.observe(parent, record) if item.row.id == row.id), None)
        receipt = dispatch_receipt(row, line, dispatch_id, run=run)
        if isinstance(receipt, DispatchEvidenceUnknown):
            print(f"anomaly: {name.value}: {receipt.reason}; no prompt sent", file=sys.stderr)
            return ExitCode.UNCERTAIN
        if isinstance(receipt, DispatchComplete) and (
                isinstance(state, (worker_panes.StoppedWorker, worker_panes.ClosedWorker))
                or isinstance(state, worker_panes.OpenWorker) and state.status in (herdr.AgentStatus.IDLE, herdr.AgentStatus.DONE)):
            return receipt
        if isinstance(state, (worker_panes.StoppedWorker, worker_panes.ClosedWorker)):
            return ExitCode.NO_DONE
        if not isinstance(state, worker_panes.OpenWorker):
            if not isinstance(state, (worker_panes.ProcessUnknownWorker, worker_panes.UncertainWorker)):
                print(f"anomaly: {name.value}: live dispatch evidence is unavailable", file=sys.stderr)
            return ExitCode.UNCERTAIN
        current = state
        live_run = isinstance(receipt, DispatchWaitingForRuns) and receipt.has_live_run
        if current.status == herdr.AgentStatus.WORKING or live_run:
            last_wait_was_quiet = False
        if current.status == herdr.AgentStatus.BLOCKED and not live_run:
            return ExitCode.BLOCKED
        if isinstance(receipt, (DispatchWaitingForRuns, DispatchAwaitingReply)):
            if last_wait_was_quiet:
                if (isinstance(receipt, DispatchWaitingForRuns) and not receipt.has_live_run
                        and isinstance(receipt.reply, DispatchComplete)
                        and current.status in (herdr.AgentStatus.IDLE, herdr.AgentStatus.DONE)):
                    for run_id in receipt.runs:
                        print(f"anomaly: {name.value}: run {run_id} finished, but its result is not in the transcript",
                              file=sys.stderr)
                    return receipt.reply
                return ExitCode.NO_DONE
            last_wait_was_quiet = not _wait_for_dispatch(row, line, dispatch_id, run)
            continue
        if current.status != herdr.AgentStatus.WORKING:
            return ExitCode.NO_DONE
        try:
            _agent_command(client, current, "wait")
        except (herdr.HerdrFailure, subprocess.TimeoutExpired) as error:
            return _delivery_failure(error)

def _delivery_failure(error: herdr.HerdrFailure | subprocess.TimeoutExpired) -> ExitCode:
    print(str(error), file=sys.stderr)
    if isinstance(error, herdr.AgentBlocked):
        return ExitCode.BLOCKED
    if isinstance(error, subprocess.TimeoutExpired) or error.code in ("agent_prompt_stalled", "timeout"):
        return ExitCode.STALLED
    return ExitCode.UNCERTAIN


def _role_number(name: str, role: WorkerRole) -> int:
    if name == role:
        return 1
    suffix = name.removeprefix(role)
    return int(suffix) if name.startswith(role) and suffix.isdigit() and int(suffix) >= 2 else 0


def select_worker(parent: StaffId, role: WorkerRole, record: staff.StaffSnapshot) -> (
        WorkerName | worker_panes.StartRefused | worker_panes.ProcessUnknownWorker | worker_panes.UncertainWorker):
    """Select a proved idle worker, a resumable worker, or the lowest free name."""
    states = worker_panes.observe(parent, record)
    candidates = sorted((item for item in states if _role_number(item.row.name, role)),
                        key=lambda item: _role_number(item.row.name, role))
    for item in candidates:
        if isinstance(item, worker_panes.OpenWorker) and item.status in (herdr.AgentStatus.IDLE, herdr.AgentStatus.DONE):
            return WorkerName.parse(item.row.name)
        if isinstance(item, (worker_panes.StoppedWorker, worker_panes.ClosedWorker)):
            return WorkerName.parse(item.row.name)
    for item in candidates:
        if isinstance(item, (worker_panes.ProcessUnknownWorker, worker_panes.UncertainWorker)):
            return item
    if candidates:
        names = ", ".join(item.row.name for item in candidates)
        return worker_panes.StartRefused(worker_panes.StartRefusal.BUSY_ROLE_CHANGE, f"workers unavailable: {names}")
    return _free_name(parent, role, record)


def _free_name(parent: StaffId, role: WorkerRole, record: staff.StaffSnapshot) -> WorkerName:
    names = {row.name for row in record.find(kind="worker", parent=parent)}
    number = 1
    while (name := role + (str(number) if number > 1 else "")) in names:
        number += 1
    return WorkerName.parse(name)


def launch(target: worker_panes.StaffWorker | worker_panes.DurableDesignRole,
           policy: worker_panes.LaunchPolicy) -> worker_panes.WorkerStart:
    """Start an explicit staff target with the supplied policy."""
    with staff.row_operation(target.parent):
        return worker_panes.start_or_resume(target.parent, target.name, target.role, policy=policy)


def add(parent: StaffId, role: WorkerRole, *, name: str = "") -> worker_panes.WorkerStart:
    with staff.row_operation(parent):
        return worker_panes.start_or_resume(parent, WorkerName.parse(name) if name else _free_name(parent, role, staff.snapshot()), role)


def assign(parent: StaffId, role: WorkerRole, prompt: Path, *, session_dir: Path, slot: str,
           phase: str, kind: str, launcher_command: list[str], lens: str = "", name: str = "",
           reply: Path | ReplyBesidePrompt = REPLY_BESIDE_PROMPT, dispatch_id: str = "", fix_cause: str = "",
           fix_pass: int = 0, activity: str = "", early_ready: Path | SingleStageReview = SINGLE_STAGE_REVIEW) -> ExitCode:
    if not name:
        matched = progress_history.dispatch_worker(session_dir, slot=slot, phase=phase, role=role,
            prompt=prompt.resolve(), launcher_command=launcher_command)
        if isinstance(matched, progress_history.MatchedDispatch):
            name = matched.worker_name
    record = staff.snapshot()
    rows = record.find(kind="worker", parent=parent)
    needs_team = False
    if role in ("impl", "fix", "review", "test"):
        owner = record.by_id(parent)
        runs = unit_runs.held(str(owner.current_session) if owner is not None else "",
                             os.environ.get("ROSTER_RUN_ROOT"), record=record)
        with progress_history.dispatch_reads(record, dict.fromkeys([*(Path(run) for run in runs), session_dir])):
            needs_team = not any(isinstance(worker_panes.dispatch_role(row), str) for row in rows)
    if needs_team:
        names = {row.name for row in rows}
        for default_role in ("impl", "fix", "review", "test"):
            if default_role in names:
                continue
            with redirect_stderr(io.StringIO()) as diagnostics:
                result = worker_panes.start_or_resume(parent, WorkerName.parse(default_role), default_role)
            if isinstance(result, worker_panes.OpenWorker):
                print(diagnostics.getvalue(), end="", file=sys.stderr)
                continue
            detail = (result.detail if isinstance(result, worker_panes.StartRefused) else
                      result.cause if isinstance(result, worker_panes.UnpublishedWorker) else result.reason)
            print(f"anomaly: {default_role}: {detail}", file=sys.stderr)
        record = staff.snapshot()
    chosen = WorkerName.parse(name) if name else select_worker(parent, role, record)
    if not isinstance(chosen, WorkerName):
        return startup_result(WorkerName.parse(role), chosen)
    return dispatch(parent, chosen, role, prompt, dispatch_id, session_dir=session_dir, slot=slot,
        phase=phase, kind=kind, lens=lens, launcher_command=launcher_command, reply=reply,
        fix_cause=fix_cause, fix_pass=fix_pass, activity=activity, early_ready=early_ready)


def end(parent: StaffId, name: WorkerName, *,
        run: worker_panes.DispatchRun | worker_panes.HolderRun = worker_panes.HolderRun.CURRENT) -> ExitCode:
    with worker_panes.worker_lock(parent, name) as hold:
        if isinstance(hold, worker_panes.Kept):
            print(hold.blocker, file=sys.stderr)
            return ExitCode.KEPT
        row = worker_admission.find_worker(staff.snapshot(), parent, name.value)
        if row is None:
            return ExitCode.OK
        result = worker_panes.close(parent, name, run=run, lock=hold)
        if isinstance(result, worker_panes.Kept):
            print(result.blocker, file=sys.stderr)
            return ExitCode.KEPT
        if isinstance(result, worker_panes.UncertainWorker):
            return ExitCode.UNCERTAIN
        _ = staff.retire(row.id)
        return ExitCode.OK


def _resume_launcher(row: staff.StaffRow, dispatch: progress_history.ActiveDispatch) -> ExitCode:
    """Ask the restored director to run the saved launcher from its session shell."""
    record = staff.snapshot()
    parent = record.parent_of(row)
    client = _client()
    pane = next((item for item in client.tab_sessions()
                 if parent is not None and item.session_id == parent.current_session), None)
    if pane is None or parent is None:
        print(f"anomaly: {row.name}: director session is unavailable", file=sys.stderr)
        return ExitCode.UNCERTAIN
    with worker_panes.worker_lock(parent.id, WorkerName.parse(row.name)) as hold:
        kept = isinstance(hold, worker_panes.Kept)
    if kept:
        return ExitCode.OK
    client.prompt(pane.pane_id, launcher_prompt(row.id, dispatch.instance_id, dispatch.launcher_command),
                  target=str(parent.current_session))
    return ExitCode.OK


def own_pass(row: staff.StaffRow, record: staff.StaffSnapshot) -> BusyPass | NoOwnPass:
    """Read the worker's active pass through its parent."""
    try:
        parent = record.parent_of(row)
        if parent is None:
            raise ValueError("worker parent is unavailable")
        active = progress_history.active_dispatch(row.id, worker_panes.worker_run(parent))
        if isinstance(active, progress_history.ActiveDispatchUnavailable):
            print(f"anomaly: {row.name}: {active.reason}", file=sys.stderr)
        elif isinstance(active, progress_history.ActiveDispatch):
            return BusyPass(active.instance_id)
    except (OSError, ValueError) as error:
        print(f"anomaly: {row.name}: {error}", file=sys.stderr)
    return NO_OWN_PASS


def resume_workers(parent: StaffId, record: staff.StaffSnapshot) -> ResumeWorkers:
    """Supply pane dispatch ids and retained mesh seats to the shared builder."""
    from scripts.shutdown.inventory import worker_resumes

    holder = record.by_id(parent)
    if holder is None:
        return ResumeWorkers()
    runs = [Path(run) for run in unit_runs.held(str(holder.current_session),
        os.environ.get("ROSTER_RUN_ROOT"), record=record)]
    mesh = [run for run in runs if unit_runs.worker_transport(run) == "mesh"]
    panes: list[PaneWorkerResume] = []
    run = worker_panes.worker_run(holder)
    for row in record.find(kind="worker", parent=parent) if not mesh else ():
        active = progress_history.active_dispatch(row.id, run)
        if isinstance(active, progress_history.ActiveDispatchUnavailable):
            print(f"anomaly: {row.name}: {active.reason}", file=sys.stderr)
            continue
        panes.append(PaneWorkerResume(row.id, BusyPass(active.instance_id)
            if isinstance(active, progress_history.ActiveDispatch) else IdleWorker()))
    return ResumeWorkers(worker_resumes(holder, record) if mesh else [], str(session_staff.worker_directory(record, parent)) if mesh else "", panes)

@dataclass(frozen=True)
class MeshWorkerRows:
    rows: list[dict[str, object]]


@dataclass(frozen=True)
class PaneWorkerStates:
    states: list[worker_panes.WorkerPane]
    runs: list[Path]


def _read_workers(parent: staff.StaffRow, record: staff.StaffSnapshot, *,
                  scanned: dict[str, list[str]] | None = None) -> MeshWorkerRows | PaneWorkerStates:
    runs = unit_runs.held(str(parent.current_session), os.environ.get("ROSTER_RUN_ROOT"), scanned=scanned, record=record)
    if runs and unit_runs.parent_transport(str(parent.current_session),
            scanned={str(parent.current_session): runs}, record=record) == "mesh":
        return MeshWorkerRows(board_json.workers(Path(runs[0]), set(), record=record))
    return PaneWorkerStates(worker_panes.observe(parent.id, record), [Path(run) for run in runs])


def restore(parent: StaffId, *, record: staff.StaffSnapshot | None = None,
            runs: dict[str, list[str]] | None = None,
            codex: herdr.CodexConversations | herdr.CodexLaunchRead = herdr.CodexLaunchRead.CURRENT) -> ExitCode:
    record = staff.snapshot() if record is None else record
    holder = next(row for row in record.rows if row.id == parent)
    changed = False
    outcome = ExitCode.OK
    observed = _read_workers(holder, record, scanned=runs)
    if isinstance(observed, MeshWorkerRows):
        print(f"{holder.name}: workers unchanged")
        return ExitCode.OK
    runs = runs if runs is not None else {str(holder.current_session): [str(path) for path in observed.runs]}
    run = worker_panes.worker_run(holder, record=record, scanned=runs)
    with progress_history.dispatch_reads(record, observed.runs):
        for state in observed.states:
            if isinstance(state, (worker_panes.ProcessUnknownWorker, worker_panes.UncertainWorker)):
                outcome = ExitCode.UNCERTAIN
                continue
            try:
                active = progress_history.active_dispatch(state.row.id, run)
                if isinstance(active, progress_history.ActiveDispatchUnavailable):
                    print(f"anomaly: {state.row.name}: {active.reason}", file=sys.stderr)
                    outcome = ExitCode.UNCERTAIN
                    continue
                if not isinstance(active, progress_history.ActiveDispatch):
                    continue
                if not isinstance(state, worker_panes.OpenWorker):
                    role = worker_panes.dispatch_role(state.row)
                    result = worker_panes.start_or_resume(parent, WorkerName.parse(state.row.name), role, runs=runs, codex=codex)
                    code = startup_result(WorkerName.parse(state.row.name), result)
                    if code != ExitCode.OK:
                        outcome = code
                        continue
                    changed = True
                code = _resume_launcher(state.row, active)
                if code != ExitCode.OK:
                    outcome = code
            except (herdr.HerdrFailure, OSError, ValueError) as error:
                print(f"anomaly: {state.row.name}: {' '.join(str(error).splitlines())}", file=sys.stderr)
                outcome = ExitCode.UNCERTAIN
    if outcome == ExitCode.OK:
        print(f"{holder.name}: workers {'restored' if changed else 'unchanged'}")
    return outcome


@dataclass(frozen=True)
class WorkerAccountUnavailable:
    reason: str


def _worker_list_row(row: staff.StaffRow, role: WorkerRole | worker_panes.WorkerRoleUnavailable,
                     account: accounts.AccountSelection | WorkerAccountUnavailable,
                     facts: dict[str, object]) -> dict[str, object]:
    fields: dict[str, object] = {"name": row.name, "id": str(row.id), "current_session": str(row.current_session), **facts}
    fields["role"] = role if isinstance(role, str) else {"state": "WorkerRoleUnavailable", "reason": role.reason}
    if isinstance(account, WorkerAccountUnavailable):
        fields["account"] = {"state": "AccountUnavailable", "reason": account.reason}
    else:
        home = account.home if isinstance(account, accounts.CodexWorkersAccount) else account.dir
        fields.update(account=account.label, home=str(home.absolute()))
    return fields


@dataclass(frozen=True)
class WorkerDispatchRecord:
    fields: dict[str, object]
    transport: Literal["mesh", "herdr"]
    timestamp: float


def _worker_history_events(path: Path) -> Generator[dict[str, object]]:
    bad_line_reported = False
    try:
        with path.open("rb") as history:
            position = history.seek(0, os.SEEK_END)
            carry = b""
            while position:
                size = min(position, 65536)
                position -= size
                _ = history.seek(position)
                lines = (history.read(size) + carry).split(b"\n")
                carry = lines.pop(0) if position else b""
                for line in reversed(lines):
                    if b'"pass_started"' not in line and b'"pass_finished"' not in line:
                        continue
                    try:
                        value = cast(object, json.loads(line))
                    except ValueError as error:
                        if not bad_line_reported:
                            print(f"anomaly: {path}: {error}", file=sys.stderr)
                            bad_line_reported = True
                        continue
                    if isinstance(value, dict):
                        entry = cast(dict[str, object], value)
                        if entry.get("event_type") in ("pass_started", "pass_finished"):
                            yield entry
    except FileNotFoundError:
        pass
    except OSError as error:
        print(f"anomaly: {path}: {error}", file=sys.stderr)


def _recorded_worker_dispatches(record: staff.StaffSnapshot) -> tuple[dict[StaffId, WorkerDispatchRecord], set[StaffId]]:
    workers = record.find(kind="worker")
    parents = {parent.id: parent for row in workers if (parent := record.parent_of(row)) is not None}
    names = {(row.parent.id, row.name): row for row in workers if isinstance(row.parent, staff.ReportsTo)}
    runs = unit_runs.run_folders(os.environ.get("ROSTER_RUN_ROOT"))
    for parent in parents.values():
        runs.append(unit_runs.state_dir() / "staff/workers" / parent.id)
        try:
            runs.append(Path(unit_runs.marker(parent.current_session).read_text().strip()))
        except FileNotFoundError:
            pass
    if current := unit_runs.environ("RUN_DIR"):
        runs.append(Path(current))
    found: dict[StaffId, WorkerDispatchRecord] = {}
    # A fixed UUIDv7 staff id records the first conversation's creation time.
    earliest = {row.id: (identity.int >> 80) / 1000 if (identity := UUID(row.id)).version == 7 else 0.0
                for row in workers}
    mesh: set[StaffId] = set()
    panes: set[StaffId] = set()
    histories: dict[Path, dict[bytes, tuple[staff.StaffRow, Literal["mesh", "herdr"]]]] = {}

    def keep(row: staff.StaffRow, entry: dict[str, object], transport: Literal["mesh", "herdr"]) -> None:
        stamp = entry.get("finished_at", entry.get("timestamp_epoch", entry.get("started_at", 0)))
        timestamp = float(stamp) if isinstance(stamp, (int, float)) else 0
        previous = found.get(row.id)
        if previous is None or timestamp > previous.timestamp:
            earliest[row.id] = max(earliest[row.id], timestamp)
            found[row.id] = WorkerDispatchRecord({
                "kind": entry.get("kind", entry.get("pass_kind", "")),
                "activity": entry.get("activity", entry.get("pass_activity", "")),
                "called_agent": entry.get("called_agent", {}),
                "status": entry.get("status", "active" if entry.get("event_type") == "pass_started" else "unknown")},
                transport, timestamp)

    for run in dict.fromkeys(path.resolve() for path in runs):
        path = run / unit_runs.STATE_FILE
        try:
            value = cast(object, json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as error:
            print(f"anomaly: {path}: {error}", file=sys.stderr)
            continue
        if not isinstance(value, dict):
            continue
        state = cast(dict[str, object], value)
        pin = unit_runs.run_pin(run)
        transport: Literal["mesh", "herdr"] = "mesh" if pin == "mesh" else "herdr"
        agent = state.get("main_agent", {})
        session = cast(dict[str, object], agent).get("session_id", "") if isinstance(agent, dict) else ""
        reference = state.get(unit_runs.OWNER_FIELD) or session or state.get("owner_session") or state.get("director_session")
        owner = record.owner_of(reference) if isinstance(reference, str) else None
        parent_id = owner.id if owner is not None else StaffId("")
        (mesh if transport == "mesh" else panes).add(parent_id)
        history = state.get("history_file")
        if isinstance(history, str) and history and parent_id in parents:
            histories.setdefault(Path(history).resolve(), {}).update({str(row.id).encode(): (row, transport)
                for row in workers if row.parent == staff.ReportsTo(parent_id)})
        entries: list[dict[str, object]] = []
        raw = state.get("pass", {})
        if isinstance(raw, dict):
            slots = cast(dict[str, object], raw)
            entries.extend([slots] if "instance_id" in slots else
                           [cast(dict[str, object], value) for value in slots.values() if isinstance(value, dict)])
        for entry in entries:
            identity = entry.get("worker_id")
            row = (record.by_id(StaffId(identity)) if isinstance(identity, str) and identity else
                   names.get((parent_id, str(entry.get("worker_name") or entry.get("team_slot") or ""))))
            if row is None or row.kind != "worker":
                continue
            keep(row, entry, transport)
    # Merge append-ordered histories so another file can satisfy an absent worker.
    sources = list(histories.items())
    with ExitStack() as opened:
        events = [opened.enter_context(closing(_worker_history_events(path))) for path, _targets in sources]
        newest: list[tuple[float, int, dict[str, object]]] = []
        for index, source in enumerate(events):
            entry = next(source, dict[str, object]())
            if entry:
                stamp = entry.get("timestamp_epoch", float("inf"))
                heapq.heappush(newest, (-float(stamp) if isinstance(stamp, (int, float)) else -float("inf"), index, entry))
        while newest:
            negative_stamp, index, entry = heapq.heappop(newest)
            _path, targets = sources[index]
            pending = {key: pair for key, pair in targets.items()
                       if earliest[pair[0].id] <= -negative_stamp}
            if not pending:
                continue
            identity = entry.get("worker_id")
            target = pending.get(identity.encode()) if isinstance(identity, str) and identity else None
            if target is None and not identity:
                matches = [pair for pair in pending.values()
                           if pair[0].name == (entry.get("worker_name") or entry.get("team_slot"))]
                if len(matches) == 1:
                    target = matches[0]
            if target is not None:
                keep(target[0], entry, target[1])
            if all(earliest[pair[0].id] >= -negative_stamp for pair in targets.values()):
                continue
            entry = next(events[index], dict[str, object]())
            if entry:
                stamp = entry.get("timestamp_epoch", float("inf"))
                heapq.heappush(newest, (-float(stamp) if isinstance(stamp, (int, float)) else -float("inf"), index, entry))
    return found, mesh - panes


def _stored_unit_account(configured: role_map.RoleMap) -> str:
    selection = configured.groups.get("units", "")
    label = selection["label"] if isinstance(selection, dict) else selection
    evidence, stamp, login = label, "", ""
    if label in ("default", "~/.claude"):
        try:
            for line in accounts.roles_history_file().read_text(encoding="utf-8").splitlines():
                value = cast(object, json.loads(line))
                if not isinstance(value, dict):
                    continue
                row = cast(dict[str, object], value)
                if row.get("group") != "units" and row.get("role") != "unit":
                    continue
                evidence, stamp, login = label, "", ""
                if row.get("label") == label:
                    recorded = row.get("account_label", label)
                    evidence = recorded if isinstance(recorded, str) and recorded.startswith("claude") else label
                    at, held_login = row.get("at"), row.get("login")
                    stamp = at if isinstance(at, str) else ""
                    login = held_login if isinstance(held_login, str) else ""
        except (OSError, ValueError):
            pass
    resolved = accounts.resolve_history_label(evidence, stamp, login=login)
    return resolved.label if isinstance(resolved, accounts.ResolvedHistoryLabel) else ""


def list_all_workers(record: staff.StaffSnapshot) -> list[dict[str, object]]:
    dispatches, mesh_parents = _recorded_worker_dispatches(record)
    with role_map.stored_role_map():
        configured = accounts.read_role_map()
        claude = _stored_unit_account(configured)
    labels = {"claude": claude, "codex": configured.workers}
    selections: dict[str, accounts.AccountSelection | WorkerAccountUnavailable] = {}
    for family, label in labels.items():
        try:
            selections[family] = (accounts.CodexWorkersAccount(label, accounts.codex_setup_home(label)) if family == "codex"
                                  else accounts.NamedAccount(label, accounts.account_dir(label)))
        except ValueError as error:
            selections[family] = WorkerAccountUnavailable(str(error))
    listing: list[dict[str, object]] = []
    for row in record.find(kind="worker"):
        parent = record.parent_of(row)
        saved = dispatches.get(row.id, WorkerDispatchRecord({}, "herdr", 0))
        if parent is None or parent.id in mesh_parents or saved.transport == "mesh":
            continue
        fields = saved.fields
        status = fields.get("status", "unknown")
        status = status if isinstance(status, str) else "unknown"
        if isinstance(row.exited, staff.Exited):
            status = "exited"
        elif isinstance(row.paused, staff.Paused):
            status = "paused"
        work = "busy" if status == "active" else "idle" if status in ("completed", "error", "canceled", "interrupted", "paused", "exited") else "unknown"
        role = fields.get("kind")
        role = role if isinstance(role, str) and role in staff.WORKER_ROLES else worker_panes.WorkerRoleUnavailable("dispatch role is unavailable")
        agent = fields.get("called_agent", {})
        family = cast(dict[str, object], agent).get("family") if isinstance(agent, dict) else ""
        if family not in ("claude", "codex"):
            family = {4: "claude", 7: "codex"}.get(UUID(row.current_session).version or 0, "")
        account = selections.get(str(family), WorkerAccountUnavailable("worker backend is unavailable"))
        listing.append(_worker_list_row(row, role, account, {"manager": parent.name, "state": status, "work": work,
            "activity": fields.get("activity") or status}))
    return listing


def list_workers(parent: StaffId, record: staff.StaffSnapshot) -> list[dict[str, object]]:
    listing: list[dict[str, object]] = []
    observed = _read_workers(next(row for row in record.rows if row.id == parent), record)
    if isinstance(observed, MeshWorkerRows):
        members = {row.name: row for row in record.find(kind="worker", parent=parent)}
        for row in observed.rows:
            fields = {key: row.get(key, "") for key in ("name", "role", "state")}
            fields["activity"] = row.get("doing", "")
            member = members.get(str(fields["name"]))
            if member is not None:
                fields.update(id=str(member.id), current_session=str(member.current_session))
            listing.append(fields)
        return listing
    with progress_history.dispatch_reads(record, observed.runs):
        for state in observed.states:
            role = worker_panes.dispatch_role(state.row)
            facts: dict[str, object] = {"state": type(state).__name__, "work": worker_panes.worker_state(state)}
            backend = record.backends.get(state.row.id, staff.BACKEND_UNAVAILABLE)
            account = (member_account.resume_account("codex" if isinstance(backend, staff.CodexBackend) else "claude", state.row)
                       if isinstance(backend, (staff.CodexBackend, staff.ClaudeBackend))
                       else WorkerAccountUnavailable("worker backend is unavailable"))
            fields = _worker_list_row(state.row, role, account, facts)
            if isinstance(state, worker_panes.OpenWorker):
                fields["activity"] = state.status.value
            if isinstance(state, (worker_panes.ProcessUnknownWorker, worker_panes.UncertainWorker)):
                fields["reason"] = state.reason
            listing.append(fields)
    return listing


def _print_workers(listing: list[dict[str, object]], as_json: bool) -> ExitCode:
    if as_json:
        print(json.dumps(listing))
    else:
        width = len(str(len(listing)))
        for number, fields in enumerate(listing, 1):
            print(f"{number:>{width}} {fields['name']} {fields['role']} {fields['state']} {fields.get('activity', '')} {fields.get('reason', '')}".rstrip())
    return ExitCode.OK


def _launcher_arguments(text: str) -> list[str]:
    value = cast(object, json.loads(text))
    if not isinstance(value, list) or not value or any(not isinstance(item, str) for item in cast(list[object], value)):
        raise ValueError("--launcher-command requires a nonempty JSON list of arguments")
    return cast(list[str], value)


@final
class Arguments(argparse.Namespace):
    command: str = ""
    parent: str = ""
    role: str = ""
    name: str = ""
    prompt: str = ""
    session_dir: str = ""
    slot: str = ""
    phase: str = ""
    kind: str = ""
    lens: str = ""
    reply: str = ""
    dispatch_id: str = ""
    fix_cause: str = ""
    fix_pass: int = 0
    activity: str = ""
    early_ready: str = ""
    launcher_command: str = ""
    json: bool = False
    all: bool = False


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("list", "restore", "check"):
        child = commands.add_parser(command)
        if command == "list":
            _ = child.add_argument("--json", action="store_true")
            _ = child.add_argument("--all", action="store_true")
    ending = commands.add_parser("end")
    _ = ending.add_argument("name")
    _ = ending.add_argument("--session-dir", default="")
    _ = ending.add_argument("--parent", default="")
    adding = commands.add_parser("add")
    _ = adding.add_argument("role", choices=staff.WORKER_ROLES)
    _ = adding.add_argument("--name", default="")
    _ = adding.add_argument("--json", action="store_true")
    _ = adding.add_argument("--session-dir", default="")
    assignment = commands.add_parser("assign")
    _ = assignment.add_argument("--parent", default="")
    _ = assignment.add_argument("role", choices=staff.WORKER_ROLES)
    _ = assignment.add_argument("prompt")
    for flag in ("session-dir", "slot", "phase", "kind", "lens", "name", "reply", "dispatch-id", "fix-cause", "activity", "early-ready"):
        aliases = {"name": "--to", "reply": "--reply-file"}
        flags = ["--" + flag, *([aliases[flag]] if flag in aliases else [])]
        _ = assignment.add_argument(*flags, default="", required=flag in ("session-dir", "slot", "phase", "kind"))
    _ = assignment.add_argument("--fix-pass", "--pass", type=int, default=0)
    _ = assignment.add_argument("--launcher-command", required=True, help="JSON list of launcher arguments")
    args = parser.parse_args(arguments, namespace=Arguments())
    try:
        if args.command == "list" and args.all:
            return _print_workers(list_all_workers(staff.local_snapshot()), args.json)
        record = staff.snapshot()
        owner = record.by_id(StaffId(args.parent)) if args.parent else record.by_current_session(staff.runtime_id())
        if owner is None or owner.kind not in ("unit", "showrunner"):
            print("staff: caller: not opened: caller is not a unit or showrunner", file=sys.stderr)
            return ExitCode.REFUSED
        if args.command == "list":
            return _print_workers(list_workers(owner.id, record), args.json)
        if args.command == "check":
            observed = _read_workers(owner, record)
            if isinstance(observed, MeshWorkerRows):
                incomplete = [row for row in observed.rows if row.get("state") == "incomplete"]
                for row in incomplete:
                    print(f"{row.get('slot') or row.get('name')}: {row.get('doing', '')}")
                return int(bool(incomplete))
            with progress_history.dispatch_reads(record, observed.runs):
                wrong = [state for state in observed.states
                         if not isinstance(state, (worker_panes.OpenWorker, worker_panes.StoppedWorker, worker_panes.ClosedWorker))
                         or (not isinstance(state, worker_panes.OpenWorker)
                             and isinstance(worker_panes.dispatch_role(state.row), worker_panes.WorkerRoleUnavailable))]
            for state in wrong:
                print(f"{state.row.name}: {getattr(state, 'reason', 'dispatch role is unavailable')}")
            return int(bool(wrong))
        if args.command == "restore":
            return restore(owner.id)
        if args.command == "end":
            code = end(owner.id, WorkerName.parse(args.name),
                run=worker_panes.DispatchRun(Path(args.session_dir)) if args.session_dir else worker_panes.HolderRun.CURRENT)
        elif args.command == "add":
            result = add(owner.id, cast(WorkerRole, args.role), name=args.name)
            if args.json and isinstance(result, worker_panes.OpenWorker):
                print(json.dumps(result.row.to_json()))
                return ExitCode.OK
            code = startup_result(WorkerName.parse(args.role), result)
        else:
            code = assign(owner.id, cast(WorkerRole, args.role), Path(args.prompt), session_dir=Path(args.session_dir),
                slot=args.slot, phase=args.phase, kind=args.kind, lens=args.lens, name=args.name,
                reply=Path(args.reply) if args.reply else REPLY_BESIDE_PROMPT, dispatch_id=args.dispatch_id,
                launcher_command=_launcher_arguments(args.launcher_command), fix_cause=args.fix_cause,
                fix_pass=args.fix_pass, activity=args.activity,
                early_ready=Path(args.early_ready) if args.early_ready else SINGLE_STAGE_REVIEW)
        if code == ExitCode.OK:
            print("done")
        return code
    except (staff.StaffUnreadable, staff.StaffRefused, herdr.HerdrFailure, OSError, ValueError) as error:
        print(f"anomaly: {' '.join(str(error).splitlines())}", file=sys.stderr)
        return ExitCode.UNCERTAIN


if __name__ == "__main__":
    raise SystemExit(main())
