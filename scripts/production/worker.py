#!/usr/bin/env python3
"""Select staff workers and dispatch work through their native conversations."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections.abc import Callable, Generator, Iterable
from contextlib import ExitStack, redirect_stderr
from dataclasses import dataclass, replace
from enum import Enum, IntEnum
from importlib import import_module
from functools import partial
import io
import json
import os
import re
import time
from pathlib import Path
import subprocess
import sys
from typing import Literal, cast, final

from scripts.agents import detached_runs, worker_lifecycle
from scripts.herdr import herdr_types
from scripts.accounts import accounts, session_state
from scripts.direct import progress_history
from scripts.herdr import worker_panes
from scripts.usage import rollouts
from scripts.lib import atomic_file, conf, unit_runs
from scripts.lib.transcripts import open_transcript
from scripts.lib.resume_message import BusyPass, IdleWorker, NoOwnPass, NO_OWN_PASS, PaneWorkerResume, ResumeWorkers, Unavailable, launcher_prompt, resume_message
from scripts.lib.resume_message import UnsubmittedAssignment as UnsubmittedAssignment
from scripts.lib.script_envelope import script_prompt
from scripts.production import display_cells, session_staff, staff, worker_admission

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

    reply: str = ""


@dataclass(frozen=True)
class DispatchComplete:
    reply: str
    anomalies: tuple[str, ...] = ()


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

    last_reply: str = ""
    runs: tuple[str, ...] = ()


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


@dataclass(frozen=True)
class DispatchPrompt:
    """Find the saved dispatch prompt without rebuilding its command or path."""

    dispatch_id: str


def transcript_receipt(entries: Iterable[str | worker_admission.TornTranscriptRecord],
                       line: str | DispatchPrompt, completion: DoneReply | FinalReply) -> TranscriptReceipt:
    """Read prompt acceptance and its reply through the shared transcript parser."""
    submitted = False
    responding = False
    incomplete = False
    received: set[str] = set()
    reply: DispatchSubmitted | DispatchComplete | DispatchAwaitingReply = DispatchSubmitted()
    try:
        for entry in entries:
            if isinstance(entry, worker_admission.TornTranscriptRecord):
                incomplete = True
                continue
            if entry.startswith("user:\n"):
                text = entry.removeprefix("user:\n").strip()
                results = {part.removeprefix("Detached run: ") for part in text.splitlines()
                           if part.startswith("Detached run: ")}
                received.update(results)
                if (text in (line.strip(), script_prompt(line).strip()) if isinstance(line, str) else
                        any(f"End your final reply with DONE {identity}." in text
                            or f"End your final reply with this line alone: DONE {identity}\n" in text for identity in
                            (line.dispatch_id, worker_panes.stage_two_completion_id(line.dispatch_id)))):
                    submitted = True
                    responding = True
                    reply = DispatchSubmitted()
                elif isinstance(completion, FinalReply):
                    responding = False
                    reply = DispatchSubmitted()
                elif "Read the unchanged prompt file" in text and any(instruction in text for instruction in (
                        "End your final reply with DONE ", "End your final reply with this line alone: DONE ")):
                    responding = False
                elif submitted and responding and results:
                    reply = DispatchAwaitingReply(
                        reply.last_reply if isinstance(reply, DispatchAwaitingReply) else reply.reply,
                        tuple(sorted(results.union(reply.runs if isinstance(reply, DispatchAwaitingReply) else ()))))
            elif (responding or isinstance(line, DispatchPrompt)) and entry.startswith("assistant:\n"):
                text = entry.removeprefix("assistant:\n")
                if isinstance(completion, FinalReply):
                    complete = bool(text.strip())
                else:
                    complete = worker_admission.reply_ends_dispatch(text, completion.dispatch_id)
                    if isinstance(line, DispatchPrompt):
                        complete = complete or worker_admission.reply_ends_dispatch(
                            text, worker_panes.stage_two_completion_id(completion.dispatch_id))
                if complete and isinstance(line, DispatchPrompt):
                    submitted = True
                if not (isinstance(line, DispatchPrompt) and isinstance(reply, DispatchComplete)):
                    reply = DispatchComplete(text) if complete else DispatchSubmitted(text)
    except (OSError, ValueError) as error:
        return TranscriptReceipt(DispatchEvidenceUnknown(str(error)), frozenset(received))
    if incomplete and not submitted:
        return TranscriptReceipt(DispatchEvidenceUnknown("worker transcript has incomplete records"), frozenset(received))
    return TranscriptReceipt(reply if submitted else DispatchNotSubmitted(), frozenset(received))


def consultation_reply(client: herdr.Herdr, pane: str, source: Path, question: str) -> str:
    """Require acceptance before the turn end and its final reply."""
    deadline = time.monotonic() + herdr.PROGRAM_TIMEOUT_SECONDS
    receipt = transcript_receipt(worker_admission.transcript_entries(source, set()), question, FinalReply()).receipt
    while True:
        accepted = isinstance(receipt, (DispatchSubmitted, DispatchComplete))
        if not accepted and time.monotonic() >= deadline:
            raise ValueError("question was not accepted")
        ended = client.wait_turn_end(pane, int(herdr.PROGRAM_TIMEOUT_SECONDS * 1000))
        if not accepted:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.1, remaining))
        receipt = transcript_receipt(worker_admission.transcript_entries(source, set()), question, FinalReply()).receipt
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
    transcript = transcript_receipt(worker_admission.conversation_entries(row, backend, set()), line, DoneReply(dispatch_id))
    if isinstance(transcript.receipt, DispatchNotSubmitted):
        transcript = transcript_receipt(worker_admission.conversation_entries(row, backend, set()), DispatchPrompt(dispatch_id), DoneReply(dispatch_id))
    reply = transcript.receipt
    if isinstance(reply, (DispatchNotSubmitted, DispatchEvidenceUnknown)):
        return reply
    received = transcript.received_runs
    passed = worker_panes.worker_pass(row, run)
    started = passed.get("started_at", 0)
    since = float(started) if isinstance(started, (int, float)) else 0
    directory = run / worker_lifecycle.DETACHED_RUNS_DIR
    pending = detached_runs.delivered_worker_results(directory, row.name, since=since) - received
    live = detached_runs.live_runs(directory, row.name)
    pending.update(record["run_id"] for record in live)
    if isinstance(reply, DispatchAwaitingReply):
        pending.update(reply.runs)
    if pending and not live and isinstance(reply, DispatchComplete):
        return replace(reply, anomalies=tuple(
            f"run {run_id} finished, but its result is not in the transcript" for run_id in sorted(pending)))
    return DispatchWaitingForRuns(tuple(sorted(pending)), bool(live), reply) if pending else reply


def _client() -> herdr.Herdr:
    return herdr.Herdr(herdr.shared_session())


def _agent_command(client: herdr.Herdr, worker: worker_panes.OpenWorker, action: str, line: str = "", *,
                   on_submission: Callable[[], object] = lambda: None) -> None:
    if not herdr.check_target(str(worker.row.current_session), action="keys",
            place=herdr.HerdrPlace(client.session, worker.pane), terminal=worker.terminal, text=line, client=client):
        raise herdr_types.HerdrFailure(f"{worker.row.name}: dispatch target is unproved", "wrong_target")
    arguments = ["agent", "prompt" if action == "compact" else action, worker.pane]
    if action == "prompt":
        arguments.append(script_prompt(line))
    elif action == "compact":
        arguments.extend(("/compact", "--wait"))
    command = client.argv(*arguments)
    _ = on_submission()
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    herdr.require_success(result, f"herdr agent {action}")


def _compact_before_dispatch(client: herdr.Herdr, current: worker_panes.OpenWorker) -> None:
    """Compact a large Codex conversation between assignments."""
    backend = staff.backend(current.row)
    if not isinstance(backend, staff.CodexBackend):
        return
    try:
        source = worker_admission.transcript_source(current.row, backend)
    except ValueError:
        return
    tokens = 0
    with open_transcript(source) as stream, io.TextIOWrapper(stream, encoding="utf-8") as transcript:
        for line in transcript:
            record = rollouts.decode_rollout(line)
            if record is None:
                continue
            if rollouts.is_compaction(record):
                tokens = 0
            usage = rollouts.token_usage(record)
            if usage is not None:
                tokens = usage.uncached_input + usage.cached_input + usage.output
    if tokens > conf.read("direct").whole("UNIT_COMPACT_ABOVE_TOKENS"):
        _agent_command(client, current, "compact")
        ended = client.wait_turn_end(current.pane, int(conf.read("direct").number("WORKER_SETTLE_SECONDS") * 1000))
        if isinstance(ended, herdr.WaitTimedOut):
            raise herdr_types.HerdrFailure("compaction did not finish before the settle bound", "timeout")


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
        print(f"{name.value}: publication failed: {result.cause}; {worker_panes.unpublished_cleanup_text(result)}", file=sys.stderr)
    # The shared scan already reports unknown and structural evidence.
    return ExitCode.UNCERTAIN


def save_reply(path: Path, reply: str) -> None:
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


class DispatchSubmission(IntEnum):
    """Submission leaves the pass active for its completion hook."""

    SUBMITTED = 0


class LaunchRow(IntEnum):
    CREATED = 1
    RETAINED = 2


def rows_before_launch() -> frozenset[StaffId]:
    return frozenset(row.id for row in staff.snapshot().rows)


def arm_launch_cleanup(cleanup: ExitStack, before: frozenset[StaffId],
                       result: worker_panes.WorkerStart) -> None:
    if isinstance(result, (worker_panes.OpenWorker, worker_panes.ProcessUnknownWorker, worker_panes.IncompleteStart)):
        if isinstance(result.created_pane, worker_panes.CreatedWorkerPane) and result.created_pane.terminal:
            row = LaunchRow.RETAINED if result.row.id in before else LaunchRow.CREATED
            _ = cleanup.callback(cleanup_failed_launch, result.row, result.pane, result.created_pane.terminal, row)


def cleanup_failed_launch(row: staff.StaffRow, pane: str, terminal: str, ownership: LaunchRow) -> None:
    """Close only the saved terminal and retire only a newly created row."""
    try:
        with redirect_stderr(io.StringIO()) as diagnostics:
            client = _client()
            closed = (client.close_tab_confirmed(pane, target=str(row.current_session), terminal=terminal)
                      and client.close_retained_pane(pane, terminal=terminal))
        if closed:
            if ownership is LaunchRow.CREATED:
                _ = staff.retire(row.id)
            return
        reason = " ".join(line.removeprefix("anomaly: ") for line in diagnostics.getvalue().splitlines())
        reason = reason or "pane close is incomplete"
    except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError,
            staff.StaffRefused, staff.StaffUnreadable) as error:
        reason = str(error)
    print(f"anomaly: {pane}: cleanup failed: {' '.join(reason.splitlines())}", file=sys.stderr)


@dataclass(frozen=True)
class SendNoticesAfterDispatch:
    """This dispatch owns the lock and sends notices after its return."""


SEND_NOTICES_AFTER_DISPATCH = SendNoticesAfterDispatch()


def dispatch(parent: StaffId, name: WorkerName, role: WorkerRole, prompt: Path, dispatch_id: str = "", *,
             session_dir: Path, slot: str, phase: str, kind: str, launcher_command: list[str],
             lens: str = "", reply: Path | ReplyBesidePrompt = REPLY_BESIDE_PROMPT, fix_cause: str = "",
             fix_pass: int = 0, activity: str = "", early_ready: Path | SingleStageReview = SINGLE_STAGE_REVIEW,
             policy: worker_panes.LaunchPolicy | worker_panes.UseRolePolicy = worker_panes.UseRolePolicy.CURRENT,
             started: worker_panes.OpenWorker | StartAtDispatch = START_AT_DISPATCH,
             completion_actions: progress_history.CompletionActions = progress_history.NO_COMPLETION_ACTIONS,
             on_submission: Callable[[], object] = lambda: None,
             lock: worker_panes.WorkerLockHold | worker_panes.WorkerLockAtUse = worker_panes.WorkerLockAtUse.ACQUIRE,
             notices: ExitStack | SendNoticesAfterDispatch = SEND_NOTICES_AFTER_DISPATCH,
             ) -> DispatchSubmission | ExitCode:
    """Record the hook destinations, submit one prompt, and return at once."""
    prompt = prompt.resolve()
    with ExitStack() as after_dispatch, ExitStack() as operations, ExitStack() as cleanup:
        notice_delivery = notices if isinstance(notices, ExitStack) else after_dispatch
        hold = operations.enter_context(worker_panes.worker_lock(parent, name)) if isinstance(lock, worker_panes.WorkerLockAtUse) else lock
        if isinstance(hold, worker_panes.Kept):
            print(hold.blocker, file=sys.stderr)
            return ExitCode.BUSY
        matched = progress_history.dispatch_worker(session_dir, slot=slot, phase=phase, role=role,
            prompt=prompt, launcher_command=launcher_command)
        row = worker_admission.find_worker(staff.snapshot(), parent, name.value)
        if (isinstance(matched, progress_history.MatchedDispatch) and matched.worker_name == name.value
                and row is not None and worker_panes.worker_pass(row, session_dir).get("status") == "completed"):
            delivery = progress_history.dispatch_delivery(worker_panes.worker_pass(row, session_dir))
            receipt = dispatch_receipt(row, delivery.line, delivery.completion_id, run=session_dir)
            if isinstance(receipt, DispatchComplete):
                save_reply(Path(delivery.reply_file), receipt.reply)
            repeat = cast(Callable[[str, staff.StaffRow, Path, dict[str, object]], Callable[[], None]],
                          import_module("scripts.herdr.turn_end").repeat_completion_notice)
            _ = notice_delivery.callback(repeat(row.name, worker_admission.parent(staff.snapshot(), run=session_dir), session_dir,
                                           worker_panes.worker_pass(row, session_dir)))
            return DispatchSubmission.SUBMITTED
        if row is not None:
            owner = staff.snapshot().parent_of(row)
            if owner is not None:
                finish_late = cast(Callable[[staff.StaffRow, staff.StaffRow, worker_panes.WorkerLockHold, str], Callable[[], None]],
                                   import_module("scripts.herdr.turn_end").finish_late_completion)
                _ = notice_delivery.callback(finish_late(row, owner, hold, completion_actions.held_role))
        if isinstance(started, StartAtDispatch):
            before = rows_before_launch()
            current_start = worker_panes.start_or_resume(parent, name, role, lock=hold, policy=policy)
            arm_launch_cleanup(cleanup, before, current_start)
        else:
            if not started.row.reports_to(parent) or started.row.name != name.value or not hold.owns(started.row):
                print("dispatch startup does not belong to this worker", file=sys.stderr)
                return ExitCode.REFUSED
            current_start = started
        if not isinstance(current_start, worker_panes.OpenWorker):
            return startup_result(name, current_start)
        started = current_start
        if not isinstance(matched, progress_history.MatchedDispatch):
            ready = _dispatch_ready(parent, started, _client())
            if not isinstance(ready, worker_panes.OpenWorker):
                return ready
            started = ready
            if started.status not in (herdr.AgentStatus.IDLE, herdr.AgentStatus.DONE):
                print(f"{started.row.name}: agent is {started.status.value}", file=sys.stderr)
                return ExitCode.BUSY

        def submitted() -> None:
            _ = cleanup.pop_all()
            _ = on_submission()

        instance = progress_history.begin_dispatch(session_dir, slot=slot, phase=phase, role=role,
            worker_name=name.value, worker_id=started.row.id, launcher_command=launcher_command, prompt=prompt, kind=kind, lens=lens,
            fix_cause=fix_cause, fix_pass=fix_pass, activity=activity)
        if dispatch_id and dispatch_id not in (instance, worker_panes.stage_two_completion_id(instance)):
            print("dispatch id differs from the recorder pass", file=sys.stderr)
            return ExitCode.REFUSED
        saved = progress_history.dispatch_delivery(worker_panes.worker_pass(started.row, session_dir))
        if not saved.line:
            dispatch_id = dispatch_id or instance
            line = (f"Read the unchanged prompt file {json.dumps(str(prompt))}. "
                    f"End your final reply with this line alone: DONE {dispatch_id}\n"
                    "A reply that waits on a detached run is not final.")
            handoff = worker_admission.dispatch_record(session_dir, name.value)
            if handoff.state in (worker_admission.SwitchState.CREATION_INTENT_RECORDED, worker_admission.SwitchState.DISPATCH_IN_FLIGHT):
                extract = worker_admission.handoff_path(session_dir, started.row)
                if extract.is_file():
                    line = f"Read the transcript extract: {extract}.\n" + line
            destination = reply if isinstance(reply, Path) else prompt.with_name(prompt.stem + "_reply.txt")
            saved = progress_history.DispatchDelivery(line, dispatch_id, str(destination),
                str(early_ready) if isinstance(early_ready, Path) else "", completion_actions)
            progress_history.save_dispatch_delivery(session_dir, instance, saved)
        result = dispatch_turn(parent, started, _client(), saved.line, saved.completion_id,
                               run=session_dir, on_submission=submitted)
        if result is DispatchSubmission.SUBMITTED:
            submitted()
        handoff = worker_admission.dispatch_record(session_dir, name.value)
        if (result is DispatchSubmission.SUBMITTED and handoff.state in
                (worker_admission.SwitchState.CREATION_INTENT_RECORDED, worker_admission.SwitchState.DISPATCH_IN_FLIGHT)):
            allowed = worker_admission.WorkerStartAllowed(name.value, parent, session_dir, worker_admission.ExistingWorkerSlot(started.row))
            worker_admission.record_dispatch(allowed, handoff.assignment_id, worker_admission.SwitchState.ASSIGNMENT_ACCEPTED,
                                            expected_request=handoff.assignment_id)
        return result


def _dispatch_ready(parent: StaffId, current: worker_panes.OpenWorker,
                    client: herdr.Herdr) -> worker_panes.OpenWorker | ExitCode:
    """Use the existing bounded wait before replacing a submitted pass."""
    codex = isinstance(staff.backend(current.row), staff.CodexBackend)
    if current.status == herdr.AgentStatus.BLOCKED and codex:
        return _blocked_question(client, current)
    if current.status == herdr.AgentStatus.WORKING:
        try:
            _ = client.wait_turn_end(current.pane, int(conf.read("direct").number("WORKER_SETTLE_SECONDS") * 1000))
            observed = worker_panes.observe_worker(parent, current.row, staff.snapshot())
        except herdr.AgentBlocked as error:
            if codex:
                observed = worker_panes.observe_worker(parent, current.row, staff.snapshot())
                if isinstance(observed, worker_panes.OpenWorker):
                    return _blocked_question(client, observed)
            print(f"{error}; no prompt sent", file=sys.stderr)
            return ExitCode.UNCERTAIN
        except (OSError, herdr_types.HerdrFailure, subprocess.TimeoutExpired) as error:
            print(f"{error}; no prompt sent", file=sys.stderr)
            return ExitCode.UNCERTAIN
        if not isinstance(observed, worker_panes.OpenWorker):
            print(f"{current.row.name}: worker state is unavailable; no prompt sent", file=sys.stderr)
            return ExitCode.UNCERTAIN
        current = observed
        if current.status == herdr.AgentStatus.BLOCKED and codex:
            return _blocked_question(client, current)
    return current


def dispatch_turn(parent: StaffId, started: worker_panes.OpenWorker, client: herdr.Herdr,
                   line: str, dispatch_id: str, *, run: Path,
                   on_submission: Callable[[], object] = lambda: None) -> DispatchSubmission | ExitCode:
    """Submit the saved prompt after the bounded idle checks."""
    if started.status == herdr.AgentStatus.BLOCKED and isinstance(staff.backend(started.row), staff.CodexBackend):
        return _blocked_question(client, started)
    receipt = dispatch_receipt(started.row, line, dispatch_id, run=run)
    if isinstance(receipt, DispatchEvidenceUnknown):
        print(f"anomaly: {started.row.name}: {receipt.reason}; no prompt sent", file=sys.stderr)
        return ExitCode.UNCERTAIN
    if not isinstance(receipt, DispatchNotSubmitted):
        return DispatchSubmission.SUBMITTED
    ready = _dispatch_ready(parent, started, client)
    if not isinstance(ready, worker_panes.OpenWorker):
        return ready
    current = ready
    codex = isinstance(staff.backend(current.row), staff.CodexBackend)
    if started.status == herdr.AgentStatus.WORKING:
        receipt = dispatch_receipt(current.row, line, dispatch_id, run=run)
        if isinstance(receipt, DispatchEvidenceUnknown):
            print(f"{current.row.name}: {receipt.reason}; no prompt sent", file=sys.stderr)
            return ExitCode.UNCERTAIN
        if not isinstance(receipt, DispatchNotSubmitted):
            return DispatchSubmission.SUBMITTED
    if current.status not in (herdr.AgentStatus.IDLE, herdr.AgentStatus.DONE):
        print(f"{current.row.name}: agent is {current.status.value}", file=sys.stderr)
        return ExitCode.BUSY
    try:
        _compact_before_dispatch(client, current)
    except (OSError, herdr_types.HerdrFailure, subprocess.TimeoutExpired) as error:
        reason = " ".join(str(error).splitlines())
        print(f"{current.row.name}: compaction failed: {reason}; prompt is sent without compaction", file=sys.stderr)
    try:
        _agent_command(client, current, "prompt", line, on_submission=on_submission)
    except OSError as error:
        reason = " ".join(str(error).splitlines())
        raise OSError(f"{reason}; prompt delivered to pane {current.pane}") from error
    except (herdr_types.HerdrFailure, subprocess.TimeoutExpired) as error:
        if isinstance(error, herdr.AgentBlocked) and codex:
            return _blocked_question(client, current, delivered_to=current.pane)
        reason = " ".join(str(error).splitlines())
        print(f"{reason}; prompt delivered to pane {current.pane}", file=sys.stderr)
        return ExitCode.UNCERTAIN
    if codex:
        deadline = time.monotonic() + conf.read("direct").number("WORKER_SETTLE_SECONDS")
        while True:
            receipt = dispatch_receipt(current.row, line, dispatch_id, run=run)
            if not isinstance(receipt, (DispatchNotSubmitted, DispatchEvidenceUnknown)):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                if isinstance(client.pane_activity(current.pane), herdr.PaneAtShell):
                    break
            except (OSError, herdr_types.HerdrFailure, subprocess.TimeoutExpired):
                pass
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        if isinstance(receipt, (DispatchNotSubmitted, DispatchEvidenceUnknown)):
            reason = receipt.reason if isinstance(receipt, DispatchEvidenceUnknown) else "dispatch prompt is not in the transcript"
            print(f"anomaly: {current.row.name}: {' '.join(reason.splitlines())}; submission is unproved", file=sys.stderr)
            return ExitCode.UNCERTAIN
    return DispatchSubmission.SUBMITTED


def _blocked_question(client: herdr.Herdr, worker: worker_panes.OpenWorker, *, delivered_to: str = "") -> ExitCode:
    print(f"question: {worker.row.name} pane {worker.pane}", file=sys.stderr)
    try:
        question = client.read(worker.pane, 40, joined=False, ansi=False)
    except (herdr_types.HerdrFailure, OSError, subprocess.SubprocessError) as error:
        reason = f"anomaly: {worker.row.name}: {' '.join(str(error).splitlines())}"
        if delivered_to:
            reason += f"; prompt delivered to pane {delivered_to}"
        print(reason, file=sys.stderr)
    else:
        print(question.rstrip("\n"), file=sys.stderr)
    return ExitCode.BLOCKED


def _role_number(name: str, role: WorkerRole) -> int:
    if name == role:
        return 1
    suffix = name.removeprefix(role)
    return int(suffix) if name.startswith(role) and suffix.isdigit() and int(suffix) >= 2 else 0


def select_worker(parent: StaffId, role: WorkerRole, record: staff.StaffSnapshot) -> (
        WorkerName | worker_panes.StartRefused | worker_panes.ProcessUnknownWorker | worker_panes.UncertainWorker):
    """Select a proved idle worker, a resumable worker, or the lowest free name."""
    names = [WorkerName.parse(row.name) for row in record.find(kind="worker", parent=parent)
             if _role_number(row.name, role)]
    candidates = worker_panes.observe_selected(parent, record, names)
    candidates.sort(key=lambda item: _role_number(item.row.name, role))
    for item in candidates:
        if isinstance(item, worker_panes.OpenWorker) and item.status in (herdr.AgentStatus.IDLE, herdr.AgentStatus.DONE):
            return WorkerName.parse(item.row.name)
        if isinstance(item, (worker_panes.StoppedWorker, worker_panes.ClosedWorker)):
            return WorkerName.parse(item.row.name)
    for item in candidates:
        if isinstance(item, (worker_panes.ProcessUnknownWorker, worker_panes.UncertainWorker)):
            return item
    if candidates:
        unavailable = ", ".join(item.row.name for item in candidates)
        return worker_panes.StartRefused(worker_panes.StartRefusal.BUSY_ROLE_CHANGE, f"workers unavailable: {unavailable}")
    return _free_name(parent, role, record)


def _free_name(parent: StaffId, role: WorkerRole, record: staff.StaffSnapshot) -> WorkerName:
    names = {row.name for row in record.find(kind="worker", parent=parent)}
    number = 1
    while (name := role + (str(number) if number > 1 else "")) in names:
        number += 1
    return WorkerName.parse(name)


def launch(target: worker_panes.StaffWorker | worker_panes.DurableDesignRole,
           policy: worker_panes.LaunchPolicy, *,
           lock: worker_panes.WorkerLockHold | worker_panes.WorkerLockAtUse = worker_panes.WorkerLockAtUse.ACQUIRE,
           ) -> worker_panes.WorkerStart:
    """Start an explicit staff target with the supplied policy."""
    with staff.row_operation(target.parent):
        return worker_panes.start_or_resume(target.parent, target.name, target.role, policy=policy, lock=lock)


def add(parent: StaffId, role: WorkerRole, *, name: str = "") -> worker_panes.WorkerStart:
    with staff.row_operation(parent):
        return worker_panes.start_or_resume(parent, WorkerName.parse(name) if name else _free_name(parent, role, staff.snapshot()), role)


def assign(parent: StaffId, role: WorkerRole, prompt: Path, *, session_dir: Path, slot: str,
           phase: str, kind: str, launcher_command: list[str], lens: str = "", name: str = "",
           reply: Path | ReplyBesidePrompt = REPLY_BESIDE_PROMPT, dispatch_id: str = "", fix_cause: str = "",
           fix_pass: int = 0, activity: str = "", early_ready: Path | SingleStageReview = SINGLE_STAGE_REVIEW,
           policy: worker_panes.UseRolePolicy | worker_panes.LaunchPolicy = worker_panes.UseRolePolicy.CURRENT,
           completion_actions: progress_history.CompletionActions = progress_history.NO_COMPLETION_ACTIONS) -> DispatchSubmission | ExitCode:
    with ExitStack() as cleanup:
        if not name:
            matched = progress_history.dispatch_worker(session_dir, slot=slot, phase=phase, role=role,
                prompt=prompt.resolve(), launcher_command=launcher_command)
            if isinstance(matched, progress_history.MatchedDispatch):
                name = matched.worker_name
        record = staff.snapshot()
        rows = record.find(kind="worker", parent=parent)
        needs_team = False
        if not name and role in ("impl", "fix", "review", "test"):
            owner = record.by_id(parent)
            runs = unit_runs.held(str(owner.current_session) if owner is not None else "",
                                 os.environ.get("ROSTER_RUN_ROOT"), record=record)
            with progress_history.dispatch_reads(record, dict.fromkeys([*(Path(run) for run in runs), session_dir])):
                needs_team = not any(isinstance(worker_panes.dispatch_role(row), str) for row in rows)
        if needs_team:
            names = {row.name for row in rows}
            for default_role in ("impl", "fix", "review", "test"):
                if default_role in names or default_role == role:
                    continue
                before = rows_before_launch()
                with redirect_stderr(io.StringIO()) as diagnostics:
                    result = worker_panes.start_or_resume(parent, WorkerName.parse(default_role), default_role)
                if isinstance(result, worker_panes.OpenWorker):
                    arm_launch_cleanup(cleanup, before, result)
                    print(diagnostics.getvalue(), end="", file=sys.stderr)
                    continue
                with ExitStack() as failed:
                    arm_launch_cleanup(failed, before, result)
                detail = (result.detail if isinstance(result, worker_panes.StartRefused) else
                          result.cause if isinstance(result, worker_panes.UnpublishedWorker) else result.reason)
                print(f"anomaly: {default_role}: {detail}", file=sys.stderr)
            record = staff.snapshot()
        chosen = WorkerName.parse(name) if name else select_worker(parent, role, record)
        if not isinstance(chosen, WorkerName):
            return startup_result(WorkerName.parse(role), chosen)
        if chosen.value != slot:
            if completion_actions.status_file:
                status = Path(completion_actions.status_file)
                target = progress_history.worker_artifact(status, slot, chosen.value)
                completion_actions = replace(completion_actions, status_file=str(target))
                if status.is_file():
                    _ = status.replace(target)
            if completion_actions.summary_file:
                target = progress_history.worker_artifact(Path(completion_actions.summary_file), slot, chosen.value)
                completion_actions = replace(completion_actions, summary_file=str(target))
            if isinstance(reply, Path):
                reply = progress_history.worker_artifact(reply, slot, chosen.value)
        result = dispatch(parent, chosen, role, prompt, dispatch_id, session_dir=session_dir, slot=chosen.value,
            phase=phase, kind=kind, lens=lens, launcher_command=launcher_command, reply=reply,
            fix_cause=fix_cause, fix_pass=fix_pass, activity=activity, early_ready=early_ready, policy=policy,
            completion_actions=completion_actions, on_submission=cleanup.pop_all)
        if result is DispatchSubmission.SUBMITTED:
            _ = cleanup.pop_all()
        return result


def answer(parent: StaffId, name: WorkerName, text: str) -> ExitCode:
    record = staff.snapshot()
    row = worker_admission.find_worker(record, parent, name.value)
    if row is None:
        print(f"{name.value}: no worker staff row", file=sys.stderr)
        return ExitCode.REFUSED
    state = worker_panes.observe_worker(parent, row, record)
    if not isinstance(state, worker_panes.OpenWorker):
        return ExitCode.UNCERTAIN
    client = _client()
    backend = record.backends.get(row.id, staff.BACKEND_UNAVAILABLE)
    if state.status is herdr.AgentStatus.BLOCKED and isinstance(backend, staff.CodexBackend):
        client.answer_question(state.pane, text, target=str(row.current_session))
    else:
        client.prompt(state.pane, text, target=str(row.current_session))
    return ExitCode.OK


def end(parent: StaffId, name: WorkerName, *,
        lock: worker_panes.WorkerLockHold | worker_panes.WorkerLockAtUse = worker_panes.WorkerLockAtUse.ACQUIRE,
        retire: bool = True) -> ExitCode:
    with ExitStack() as operations:
        hold = operations.enter_context(worker_panes.worker_lock(parent, name, blocking=True)) if isinstance(lock, worker_panes.WorkerLockAtUse) else lock
        if isinstance(hold, worker_panes.Kept):
            print(hold.blocker, file=sys.stderr)
            return ExitCode.KEPT
        row = worker_admission.find_worker(staff.snapshot(), parent, name.value)
        if row is None:
            return ExitCode.OK
        result = worker_panes.close(parent, name, lock=hold)
        if isinstance(result, worker_panes.Kept):
            print(result.blocker, file=sys.stderr)
            return ExitCode.KEPT
        if isinstance(result, worker_panes.UncertainWorker):
            return ExitCode.UNCERTAIN
        if retire:
            _ = staff.retire(row.id)
        return ExitCode.OK


def resume_receipt(row: staff.StaffRow, dispatch: progress_history.ActiveDispatch
                   ) -> DispatchNotSubmitted | DispatchSubmitted | DispatchComplete | DispatchEvidenceUnknown | DispatchAwaitingReply:
    """Read acceptance and closure once before choosing the worker's resume step."""
    backend = staff.backend(row)
    if isinstance(backend, staff.BackendUnavailable):
        return DispatchEvidenceUnknown("worker transcript backend is unavailable")
    return transcript_receipt(worker_admission.conversation_entries(row, backend, set()),
        DispatchPrompt(dispatch.instance_id), DoneReply(dispatch.instance_id)).receipt


def resume_step(row: staff.StaffRow, dispatch: progress_history.ActiveDispatch
                ) -> BusyPass | UnsubmittedAssignment | NoOwnPass | DispatchEvidenceUnknown:
    """Use the transcript receipt to select one step for every resume path."""
    receipt = resume_receipt(row, dispatch)
    if isinstance(receipt, DispatchNotSubmitted):
        return UnsubmittedAssignment(dispatch.instance_id, dispatch.launcher_command)
    if isinstance(receipt, (DispatchSubmitted, DispatchAwaitingReply)):
        return BusyPass(dispatch.instance_id)
    return receipt if isinstance(receipt, DispatchEvidenceUnknown) else NO_OWN_PASS


@dataclass(frozen=True)
class NoLaunchCleanup:
    """The restore owns no new pane to close."""


NO_LAUNCH_CLEANUP = NoLaunchCleanup()


def _resume_launcher(row: staff.StaffRow,
                     dispatch: progress_history.ActiveDispatch | progress_history.NoActiveDispatch, *,
                     detached: str = "",
                     on_submission: Callable[[], object] | NoLaunchCleanup = NO_LAUNCH_CLEANUP) -> ExitCode:
    """Read and deliver the resume step while restore holds the dispatch lock."""
    record = staff.snapshot()
    parent = record.parent_of(row)
    step = resume_step(row, dispatch) if isinstance(dispatch, progress_history.ActiveDispatch) else NO_OWN_PASS
    if isinstance(step, NoOwnPass) and not detached:
        return ExitCode.OK
    recipient = row if isinstance(step, (BusyPass, NoOwnPass)) else parent
    client = _client()
    pane = next((item for item in client.tab_sessions()
                 if recipient is not None and item.session_id == recipient.current_session), None)
    if pane is None or parent is None or recipient is None:
        print(f"anomaly: {row.name}: {'worker' if recipient == row else 'director'} session is unavailable", file=sys.stderr)
        return ExitCode.UNCERTAIN
    if isinstance(step, DispatchEvidenceUnknown):
        print(f"anomaly: {row.name}: {step.reason}", file=sys.stderr)
        return ExitCode.UNCERTAIN
    if isinstance(step, (BusyPass, NoOwnPass)):
        status = next((agent["agent_status"] for agent in client.agents() if agent["pane_id"] == pane.pane_id),
                      herdr.AgentStatus.UNKNOWN)
        if status not in (herdr.AgentStatus.IDLE, herdr.AgentStatus.DONE):
            return ExitCode.OK
    text = (launcher_prompt(row.id, step.dispatch_id, step.command) + ("\n" + detached if detached else "")
            if isinstance(step, UnsubmittedAssignment) else resume_message("", own_pass=step, detached=detached))
    submit = (client.prompt if isinstance(on_submission, NoLaunchCleanup) else
              partial(client.prompt, on_submission=on_submission))
    submit(pane.pane_id, text, target=str(recipient.current_session))
    return ExitCode.OK


def own_pass(row: staff.StaffRow, record: staff.StaffSnapshot, *,
             run: worker_panes.DispatchRun | worker_panes.HolderRun = worker_panes.HolderRun.CURRENT
             ) -> BusyPass | UnsubmittedAssignment | NoOwnPass:
    """Read the worker's active pass from its own run."""
    try:
        parent = record.parent_of(row)
        if parent is None:
            raise ValueError("worker parent is unavailable")
        directory = run.directory if isinstance(run, worker_panes.DispatchRun) else worker_panes.worker_run(row, record=record)
        active = progress_history.active_dispatch(row.id, directory)
        if isinstance(active, progress_history.ActiveDispatchUnavailable):
            print(f"anomaly: {row.name}: {active.reason}", file=sys.stderr)
        elif isinstance(active, progress_history.ActiveDispatch):
            step = resume_step(row, active)
            if not isinstance(step, DispatchEvidenceUnknown):
                return step
            print(f"anomaly: {row.name}: {step.reason}", file=sys.stderr)
    except (OSError, ValueError) as error:
        print(f"anomaly: {row.name}: {error}", file=sys.stderr)
    return NO_OWN_PASS


def resume_workers(parent: StaffId, record: staff.StaffSnapshot) -> ResumeWorkers:
    """Supply Claude child launchers and pane dispatch ids to the shared builder."""
    from scripts.agents.inventory import worker_resumes

    holder = record.by_id(parent)
    if holder is None:
        return ResumeWorkers()
    results = {staff.StaffId(result.staff_id if isinstance(result, Unavailable) else result.seat["staff_id"]): result
               for result in worker_resumes(holder, record)}
    members = record.find(kind="worker", parent=parent)
    unavailable = [row for row in members if isinstance(results.get(row.id), Unavailable)]
    recorded = session_staff.worker_run_entries(record, unavailable) if unavailable else {}
    results = {identity: result for identity, result in results.items()
               if not isinstance(result, Unavailable) or identity in recorded}
    panes: list[PaneWorkerResume] = []
    runs = [Path(run) for run in unit_runs.held(str(holder.current_session),
        os.environ.get("ROSTER_RUN_ROOT"), record=record)]
    scanned = {str(holder.current_session): [str(path) for path in runs]}
    with progress_history.dispatch_reads(record, runs):
        for row in members:
            run = worker_panes.worker_run(row, record=record, scanned=scanned)
            active = progress_history.active_dispatch(row.id, run)
            if isinstance(active, progress_history.ActiveDispatchUnavailable):
                print(f"anomaly: {row.name}: {active.reason}", file=sys.stderr)
                continue
            if isinstance(active, progress_history.ActiveDispatch):
                _ = results.pop(row.id, None)
                step = resume_step(row, active)
                if isinstance(step, (BusyPass, UnsubmittedAssignment)):
                    panes.append(PaneWorkerResume(row.id, step))
                elif isinstance(step, DispatchEvidenceUnknown):
                    print(f"anomaly: {row.name}: {step.reason}", file=sys.stderr)
            elif row.id not in results:
                latest = progress_history.last_dispatch(row.id)
                if (isinstance(latest, progress_history.RecordedDispatch)
                        and isinstance(latest.status, str) and latest.status != "active"):
                    continue
                panes.append(PaneWorkerResume(row.id, IdleWorker()))
    return ResumeWorkers(list(results.values()), str(session_staff.worker_directory(record, parent)) if results else "", panes)


@dataclass(frozen=True)
class PaneWorkerStates:
    states: list[worker_panes.WorkerPane]
    runs: list[Path]


def _read_workers(parent: staff.StaffRow, record: staff.StaffSnapshot, *,
                  only: frozenset[StaffId] | Literal["all"] = "all",
                  scanned: dict[str, list[str]] | None = None) -> PaneWorkerStates:
    runs = unit_runs.held(str(parent.current_session), os.environ.get("ROSTER_RUN_ROOT"), scanned=scanned, record=record)
    states = (worker_panes.observe(parent.id, record) if only == "all" else
              worker_panes.observe_selected(parent.id, record, [WorkerName.parse(row.name)
                  for row in record.find(kind="worker", parent=parent.id) if row.id in only]))
    return PaneWorkerStates(states, [Path(run) for run in runs])


def restore(parent: StaffId, *, record: staff.StaffSnapshot | None = None,
            only: frozenset[StaffId] | Literal["all"] = "all", account: str | accounts.AccountSelection = "",
            started_panes: dict[StaffId, str] | Literal["discard"] = "discard",
            runs: dict[str, list[str]] | None = None,
            codex: herdr.CodexConversations | herdr.CodexLaunchRead = herdr.CodexLaunchRead.CURRENT) -> ExitCode:
    record = staff.snapshot() if record is None else record
    holder = next(row for row in record.rows if row.id == parent)
    changed = False
    outcome = ExitCode.OK
    launch_account = ((worker_panes.DestinationWorkerAccount(account) if account else
                       worker_panes.ConfiguredWorkerAccount.CURRENT) if isinstance(account, str) else account)
    observed = _read_workers(holder, record, only=only, scanned=runs)
    runs = runs if runs is not None else {str(holder.current_session): [str(path) for path in observed.runs]}
    with progress_history.dispatch_reads(record, observed.runs):
        for state in observed.states:
            if isinstance(state, (worker_panes.ProcessUnknownWorker, worker_panes.UncertainWorker)):
                outcome = ExitCode.UNCERTAIN
                continue
            try:
                with worker_panes.worker_lock(parent, WorkerName.parse(state.row.name)) as hold, ExitStack() as cleanup:
                    if isinstance(hold, worker_panes.Kept):
                        outcome = ExitCode.REFUSED
                        continue
                    if isinstance(state, worker_panes.OpenWorker) and not state.status.idle:
                        continue
                    run = worker_panes.worker_run(state.row, record=record, scanned=runs)
                    active = progress_history.active_dispatch(state.row.id, run)
                    if isinstance(active, progress_history.ActiveDispatchUnavailable):
                        print(f"anomaly: {state.row.name}: {active.reason}", file=sys.stderr)
                        outcome = ExitCode.UNCERTAIN
                        continue
                    detached = detached_runs.resume_context(run / worker_lifecycle.DETACHED_RUNS_DIR, state.row.name)
                    if not isinstance(active, progress_history.ActiveDispatch) and not detached:
                        if isinstance(state, (worker_panes.OpenWorker, worker_panes.StoppedWorker)):
                            allowed = worker_panes.can_close(state, run=worker_panes.DispatchRun(run), lock=hold)
                            if isinstance(allowed, worker_panes.Kept):
                                continue
                            closed = worker_panes.close(parent, WorkerName.parse(state.row.name), lock=hold)
                            if not isinstance(closed, worker_panes.Closed):
                                outcome = ExitCode.UNCERTAIN
                                continue
                            changed = True
                        if only != "all":
                            print(f"{state.row.name}: closed; its next assign resumes it")
                        continue
                    resume = partial(_resume_launcher, detached=detached) if detached else _resume_launcher
                    if not isinstance(state, worker_panes.OpenWorker):
                        role = worker_panes.dispatch_role(state.row)
                        before = rows_before_launch()
                        result = worker_panes.start_or_resume(parent, WorkerName.parse(state.row.name), role,
                            account=launch_account, lock=hold, runs=runs, codex=codex)
                        arm_launch_cleanup(cleanup, before, result)
                        code = startup_result(WorkerName.parse(state.row.name), result)
                        if code != ExitCode.OK:
                            outcome = code
                            continue
                        if isinstance(started_panes, dict):
                            started_panes[state.row.id] = cast(worker_panes.OpenWorker, result).pane
                        opened = cast(worker_panes.OpenWorker, result)
                        if isinstance(opened.created_pane, worker_panes.CreatedWorkerPane):
                            resume = partial(resume, on_submission=cleanup.pop_all)
                        changed = True
                        row = opened.row
                    else:
                        row = state.row
                    code = resume(row, active)
                    if code != ExitCode.OK:
                        outcome = code
                    else:
                        _ = cleanup.pop_all()
            except (herdr_types.HerdrFailure, OSError, ValueError) as error:
                print(f"anomaly: {state.row.name}: {' '.join(str(error).splitlines())}", file=sys.stderr)
                outcome = ExitCode.UNCERTAIN
    if outcome == ExitCode.OK:
        print(f"{holder.name}: workers {'restored' if changed else 'unchanged'}")
    return outcome


def _worker_list_row(row: staff.StaffRow, facts: dict[str, object]) -> dict[str, object]:
    fields: dict[str, object] = {"name": row.name, "id": str(row.id), "current_session": str(row.current_session), **facts}
    fields["role"] = (row.role.value if isinstance(row.role, staff.KnownWorkerRole)
                      else {"state": "WorkerRoleUnavailable", "reason": "dispatch role is unavailable"})
    if isinstance(row.home, staff.HomeUnknown):
        fields["account"] = {"state": "AccountUnavailable", "reason": "worker home is unavailable"}
    else:
        fields.update(account=accounts.claude_config_label(row.home.path), home=str(row.home.path))
    return fields


@dataclass(frozen=True)
class WorkerDispatchRecord:
    fields: dict[str, object]
    timestamp: float


WORKER_HISTORY_BYTES = 64 * 1024
type WorkerHistoryTargets = dict[bytes, staff.StaffRow]


def _worker_history_events(path: Path, wanted: WorkerHistoryTargets) -> Generator[dict[str, object]]:
    identity_pattern = re.compile(rb'"worker_id"\s*:\s*"([^"\\]*)"')
    name_pattern = re.compile(rb'"(?:worker_name|team_slot)"\s*:\s*("(?:[^"\\]|\\.)*")')
    bad_line_reported = False
    try:
        with path.open("rb") as history:
            offset = max(0, history.seek(0, os.SEEK_END) - WORKER_HISTORY_BYTES)
            _ = history.seek(offset - 1 if offset else 0)
            tail = history.read(WORKER_HISTORY_BYTES + bool(offset))
        if offset:
            boundary, tail = tail[:1], tail[1:]
            if boundary != b"\n":
                tail = tail.partition(b"\n")[2]
        for line in reversed(tail.split(b"\n")):
            if b'"pass_started"' not in line and b'"pass_finished"' not in line:
                continue
            identity = identity_pattern.search(line)
            if identity is not None and identity[1]:
                if identity[1] not in wanted:
                    continue
            elif not any(name[1] == json.dumps(row.name).encode()
                         for name in name_pattern.finditer(line) for row in wanted.values()):
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


@dataclass(frozen=True)
class RecordedWorkerDispatches:
    by_worker: dict[StaffId, WorkerDispatchRecord]
    runs: tuple[Path, ...]


def _recorded_worker_dispatches(record: staff.StaffSnapshot) -> RecordedWorkerDispatches:
    workers = record.find(kind="worker")
    parents = {parent.id: parent for row in workers if (parent := record.parent_of(row)) is not None}
    names = {(row.parent.id, row.name): row for row in workers if isinstance(row.parent, staff.ReportsTo)}
    runs = unit_runs.run_folders(os.environ.get("ROSTER_RUN_ROOT"))
    for parent in parents.values():
        runs.append(unit_runs.state_dir() / "staff/workers" / parent.id)
        if isinstance(parent.run, staff.OpenRun):
            runs.append(parent.run.path)
    if current := unit_runs.environ("RUN_DIR"):
        runs.append(Path(current))
    found: dict[StaffId, WorkerDispatchRecord] = {}
    histories: dict[Path, WorkerHistoryTargets] = {}

    def keep(row: staff.StaffRow, entry: dict[str, object]) -> None:
        stamp = entry.get("finished_at", entry.get("timestamp_epoch", entry.get("started_at", 0)))
        timestamp = float(stamp) if isinstance(stamp, (int, float)) else 0
        previous = found.get(row.id)
        if previous is None or timestamp > previous.timestamp:
            found[row.id] = WorkerDispatchRecord({
                "kind": entry.get("kind", entry.get("pass_kind", "")),
                "activity": entry.get("activity", entry.get("pass_activity", "")),
                "called_agent": entry.get("called_agent", {}),
                "status": entry.get("status", "active" if entry.get("event_type") == "pass_started" else "unknown")},
                timestamp)

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
        agent = state.get("main_agent", {})
        session = cast(dict[str, object], agent).get("session_id", "") if isinstance(agent, dict) else ""
        reference = state.get(unit_runs.OWNER_FIELD) or session or state.get("owner_session") or state.get("director_session")
        owner = record.owner_of(reference) if isinstance(reference, str) else None
        parent_id = owner.id if owner is not None else StaffId("")
        history = state.get("history_file")
        if isinstance(history, str) and history and parent_id in parents:
            histories.setdefault(Path(history).resolve(), {}).update({str(row.id).encode(): row
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
            keep(row, entry)
    for path, targets in histories.items():
        pending = targets.copy()
        for entry in _worker_history_events(path, pending):
            identity = entry.get("worker_id")
            target = pending.get(identity.encode()) if isinstance(identity, str) and identity else None
            if target is None and not identity:
                matches = [pair for pair in pending.values()
                           if pair.name == (entry.get("worker_name") or entry.get("team_slot"))]
                if len(matches) == 1:
                    target = matches[0]
            if target is not None:
                keep(target, entry)
                del pending[str(target.id).encode()]
            if not pending:
                break
    return RecordedWorkerDispatches(found, tuple(runs))


class WorkerFilter(Enum):
    OPEN = "open"
    CLOSED = "closed"
    ALL = "all"


def _open_worker(state: worker_panes.WorkerPane) -> bool:
    return isinstance(state, (worker_panes.OpenWorker, worker_panes.StoppedWorker, worker_panes.ProcessUnknownWorker))


def _listed_worker(state: worker_panes.WorkerPane, selection: WorkerFilter) -> bool:
    return (selection is WorkerFilter.ALL or selection is WorkerFilter.OPEN and _open_worker(state)
            or selection is WorkerFilter.CLOSED and isinstance(state, worker_panes.ClosedWorker))


def _waiting_on(row: staff.StaffRow, detached: session_state.RunningDetachedWork) -> str:
    return ", ".join(detached.labels.get(row.id, ())) or "-"


def list_all_workers(record: staff.StaffSnapshot, *, selection: WorkerFilter = WorkerFilter.ALL,
                     as_json: bool = True) -> list[dict[str, object]]:
    dispatches = _recorded_worker_dispatches(record)
    detached = (session_state.running_detached_work(record, additional_runs=dispatches.runs)
                if not as_json else session_state.NO_DETACHED_WORK)
    states = {state.row.id: state for parent_id in dict.fromkeys(
        row.parent.id for row in record.find(kind="worker") if isinstance(row.parent, staff.ReportsTo))
        for state in worker_panes.observe(parent_id, record)} if selection is not WorkerFilter.ALL or not as_json else {}
    listing: list[dict[str, object]] = []
    for row in record.find(kind="worker"):
        parent = record.parent_of(row)
        saved = dispatches.by_worker.get(row.id, WorkerDispatchRecord({}, 0))
        if parent is None:
            continue
        if selection is not WorkerFilter.ALL and (row.id not in states or not _listed_worker(states[row.id], selection)):
            continue
        fields = saved.fields
        status = fields.get("status", "unknown")
        status = status if isinstance(status, str) else "unknown"
        activity = fields.get("kind") if status in ("active", "refused") else ""
        if isinstance(row.exited, staff.Exited):
            status = "exited"
        elif isinstance(row.paused, staff.Paused):
            status = "paused"
        work = "busy" if status == "active" else "idle" if status in ("completed", "error", "canceled", "interrupted", "paused", "exited") else "unknown"
        if status == "active":
            try:
                run = worker_panes.worker_run(row, record=record)
                dispatch = (worker_panes.worker_dispatch_state(row, run) if as_json else
                            worker_panes.worker_dispatch_state(row, run, detached=detached))
                if isinstance(dispatch, worker_panes.ReleasedWorkerPass):
                    status, work = dispatch.value, "idle"
                elif isinstance(dispatch, worker_panes.CompletedWorkerPass):
                    status, work = "completed", "idle"
            except (OSError, ValueError) as error:
                print(f"anomaly: {row.name}: {' '.join(str(error).splitlines())}", file=sys.stderr)
        item = _worker_list_row(row, {"manager": parent.name, "state": status, "work": work,
            "activity": fields.get("activity") or status})
        if not as_json:
            item["list_activity"] = activity or item["role"]
            observed = states.get(row.id)
            if observed is not None and isinstance(observed, worker_panes.OpenWorker):
                item["pane"], item["state"] = _worker_pane_state(observed, detached)
            item["waiting_on"] = _waiting_on(row, detached)
        listing.append(item)
    if not as_json:
        listing.sort(key=lambda fields: not (StaffId(str(fields["id"])) in states
                     and _open_worker(states[StaffId(str(fields["id"]))])))
    return listing


def _worker_pane_state(state: worker_panes.WorkerPane,
                       detached: session_state.RunningDetachedWork) -> tuple[str, str]:
    if isinstance(state, worker_panes.OpenWorker):
        activity = worker_panes.worker_state(state)
        classified = session_state.classify(state.row, session_state.LiveActivity(activity), detached=detached)
        return "open", activity if activity == "unknown" and classified == "idle" else classified
    if isinstance(state, worker_panes.StoppedWorker):
        return "open", "exited"
    if isinstance(state, worker_panes.ClosedWorker):
        return "closed", "-"
    return "unknown", "-"


def list_workers(parent: StaffId, record: staff.StaffSnapshot, *, as_json: bool = True,
                 selection: WorkerFilter = WorkerFilter.ALL) -> list[dict[str, object]]:
    listing: list[dict[str, object]] = []
    detached = session_state.running_detached_work(record)
    observed = _read_workers(next(row for row in record.rows if row.id == parent), record,
                             scanned=detached.launcher_runs.by_session)
    with progress_history.dispatch_reads(record, observed.runs):
        states = observed.states if as_json else sorted(observed.states, key=lambda state: not _open_worker(state))
        for state in states:
            if not _listed_worker(state, selection):
                continue
            pane, displayed_state = _worker_pane_state(state, detached)
            work = displayed_state if isinstance(state, worker_panes.OpenWorker) else worker_panes.worker_state(state)
            facts: dict[str, object] = {"state": type(state).__name__, "work": work}
            fields = _worker_list_row(state.row, facts)
            if isinstance(state, worker_panes.OpenWorker):
                fields["activity"] = state.status.value
            if isinstance(state, (worker_panes.ProcessUnknownWorker, worker_panes.UncertainWorker)):
                fields["reason"] = state.reason
            if not as_json:
                fields["pane"], fields["state"] = pane, displayed_state
                dispatch = progress_history.last_dispatch(state.row.id)
                fields["list_activity"] = (dispatch.role if isinstance(dispatch, progress_history.RecordedDispatch)
                                           and dispatch.status in ("active", "refused") else fields["role"])
                fields["waiting_on"] = _waiting_on(state.row, detached)
            listing.append(fields)
    return listing


def _print_workers(listing: list[dict[str, object]], as_json: bool, record: staff.StaffSnapshot) -> ExitCode:
    if as_json:
        print(json.dumps(listing))
    else:
        locations: list[str] = []
        for fields in listing:
            row = record.by_id(StaffId(str(fields.get("id", ""))))
            parent = record.parent_of(row) if row is not None else None
            locations.append(staff.worktree_machine(parent) if parent is not None else "")
        machines = display_cells.machine_column(locations)
        rows = [["#", "Name", "Account", "Activity", "Pane", "State", "Waiting on"]]
        for number, fields in enumerate(listing, 1):
            activity = fields.get("list_activity", fields.get("role"))
            activity = activity if isinstance(activity, str) and activity else "activity unknown"
            account = fields.get("account")
            account = account if isinstance(account, str) else "account unknown"
            pane = fields.get("pane", "-")
            state = fields["state"]
            if pane == "-":
                state = state if state in ("paused", "exited") else "busy" if state == "active" else "idle"
            elif pane != "open":
                account = "-"
            machine = f" ({machines[number - 1]})" if machines else ""
            reason = f" ({fields['reason']})" if fields.get("reason") else ""
            rows.append([str(number), str(fields["name"]), account + machine, activity, str(pane), str(state) + reason,
                         " ".join(str(fields.get("waiting_on", "-")).splitlines())])
        widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
        for row in rows:
            print("  ".join(cell.rjust(widths[column]) if column == 0 else cell.ljust(widths[column])
                            for column, cell in enumerate(row)).rstrip())
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
    text: str = ""
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
    model: str = ""
    effort: str = ""
    completion_actions: str = ""
    json: bool = False
    all_parents: bool = False
    selection: WorkerFilter = WorkerFilter.OPEN


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("list", "restore", "check"):
        child = commands.add_parser(command)
        if command == "list":
            _ = child.add_argument("--json", action="store_true")
            _ = child.add_argument("selection", nargs="?", type=WorkerFilter,
                                   choices=list(WorkerFilter), default=WorkerFilter.OPEN, metavar="open|closed|all")
            _ = child.add_argument("--all-parents", action="store_true", help="read saved dispatches for every parent")
    answering = commands.add_parser("answer")
    _ = answering.add_argument("name")
    _ = answering.add_argument("--text", required=True)
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
    for flag in ("session-dir", "slot", "phase", "kind", "lens", "name", "reply", "dispatch-id", "fix-cause", "activity", "early-ready", "model", "effort"):
        aliases = {"name": "--to", "reply": "--reply-file"}
        flags = ["--" + flag, *([aliases[flag]] if flag in aliases else [])]
        _ = assignment.add_argument(*flags, default="", required=flag in ("session-dir", "slot", "phase", "kind"))
    _ = assignment.add_argument("--fix-pass", "--pass", type=int, default=0)
    _ = assignment.add_argument("--launcher-command", required=True, help="JSON list of launcher arguments")
    _ = assignment.add_argument("--completion-actions", default="{}")
    args = parser.parse_args(arguments, namespace=Arguments())
    try:
        if args.command == "list" and args.all_parents:
            record = staff.local_snapshot()
            return _print_workers(list_all_workers(record, selection=args.selection, as_json=args.json), args.json, record)
        record = staff.snapshot()
        owner = record.by_id(StaffId(args.parent)) if args.parent else record.by_current_session(staff.runtime_id())
        if owner is None or owner.kind not in ("unit", "showrunner"):
            print("staff: caller: not opened: caller is not a unit or showrunner", file=sys.stderr)
            return ExitCode.REFUSED
        if args.command == "list":
            return _print_workers(list_workers(owner.id, record, as_json=args.json, selection=args.selection), args.json, record)
        if args.command == "check":
            observed = _read_workers(owner, record)
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
        if args.command == "answer":
            code = answer(owner.id, WorkerName.parse(args.name), args.text)
        elif args.command == "end":
            code = end(owner.id, WorkerName.parse(args.name))
        elif args.command == "add":
            result = add(owner.id, cast(WorkerRole, args.role), name=args.name)
            if args.json and isinstance(result, worker_panes.OpenWorker):
                print(json.dumps(result.row.to_json()))
                return ExitCode.OK
            code = startup_result(WorkerName.parse(args.role), result)
        else:
            policy: worker_panes.UseRolePolicy | worker_panes.LaunchPolicy = worker_panes.UseRolePolicy.CURRENT
            if args.model or args.effort:
                selected = worker_panes.launch_policy(cast(WorkerRole, args.role))
                policy = replace(selected, settings=selected.settings._replace(
                    model=args.model or selected.settings.model, effort=args.effort or selected.settings.effort))
            code = assign(owner.id, cast(WorkerRole, args.role), Path(args.prompt), session_dir=Path(args.session_dir),
                slot=args.slot, phase=args.phase, kind=args.kind, lens=args.lens, name=args.name,
                reply=Path(args.reply) if args.reply else REPLY_BESIDE_PROMPT, dispatch_id=args.dispatch_id,
                launcher_command=_launcher_arguments(args.launcher_command), fix_cause=args.fix_cause,
                fix_pass=args.fix_pass, activity=args.activity,
                early_ready=Path(args.early_ready) if args.early_ready else SINGLE_STAGE_REVIEW, policy=policy,
                completion_actions=progress_history.dispatch_delivery({"delivery": {"actions": cast(object, json.loads(args.completion_actions))}}).actions)
        if isinstance(code, DispatchSubmission):
            print("submitted")
        elif code == ExitCode.OK:
            print("done")
        return code
    except (staff.StaffUnreadable, staff.StaffRefused, herdr_types.HerdrFailure, OSError, ValueError) as error:
        print(f"anomaly: {' '.join(str(error).splitlines())}", file=sys.stderr)
        return ExitCode.UNCERTAIN


if __name__ == "__main__":
    raise SystemExit(main())
