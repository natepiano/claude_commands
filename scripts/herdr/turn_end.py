#!/usr/bin/env python3
"""Save a worker turn and notify its director through one completion rule."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import hashlib
import io
import json
import subprocess
import sys
from contextlib import redirect_stderr
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Callable, cast

from scripts.direct import progress_history
from scripts.herdr import herdr_types
from scripts.lib import atomic_file, scratch
from scripts.herdr import herdr, worker_panes
from scripts.production import staff, worker, worker_admission


class EventKind(StrEnum):
    TURN_END = "turn end"
    RUN_FINISHED = "run finished"
    STAGE_READY = "stage ready"


@dataclass(frozen=True)
class WorkerEvent:
    thread_id: str
    kind: EventKind = EventKind.TURN_END
    reply: str = ""


class TurnOutcome(StrEnum):
    DONE = "done"
    NO_DONE = "no DONE"
    BUSY = "busy"
    UNCERTAIN = "uncertain"
    ERROR = "error"
    WAITING_ON_RUNS = "waiting on runs"
    WAITING_ON_FRIEND = "waiting on friend"
    WAITING_ON_REPLY = "waiting on run reply"
    WAITING_ON_STAGE_TWO = "waiting on final diff"
    SUBMITTED = "submitted"
    IGNORED = "ignored"


def hook_event(value: dict[str, object]) -> WorkerEvent:
    """Convert each native hook input at the boundary."""
    if value.get("hook_event_name") == "Stop":
        return WorkerEvent(str(value.get("session_id", "")), reply=str(value.get("last_assistant_message") or ""))
    kind = value.get("event")
    if kind in (EventKind.RUN_FINISHED, EventKind.STAGE_READY):
        return WorkerEvent(str(value.get("thread_id", "")), EventKind(str(kind)))
    return WorkerEvent("")


def _friend_pending(client: herdr.Herdr, state: worker_panes.WorkerPane | worker_panes.WorkerNotObserved,
                    row: staff.StaffRow) -> bool:
    if not isinstance(state, worker_panes.OpenWorker):
        return False
    panes = client.panes()
    tab = next((pane.get("tab_id") for pane in panes if pane["pane_id"] == state.pane), "")
    kind = "codex" if isinstance(staff.backend(row), staff.CodexBackend) else "claude"
    address = ("codex:" if kind == "codex" else "session:") + str(row.current_session)
    for pane in panes:
        if not (pane.get("tab_id") == tab and pane["pane_id"] != state.pane
                and client.readiness(pane["pane_id"])["agent_status"] == herdr.AgentStatus.WORKING):
            continue
        try:
            native = client.native_pane_process(pane["pane_id"], kind, herdr.PROGRAM_TIMEOUT_SECONDS, pane_row=pane)
        except herdr_types.HerdrFailure:
            continue
        backend = (staff.CodexBackend(Path(native.environment["CODEX_HOME"])) if kind == "codex"
                   else staff.ClaudeBackend())
        source = worker_admission.transcript_source(replace(row, current_session=staff.native_id(native.current_identity)), backend)
        prompts = [entry for entry in worker_admission.transcript_entries(source, set())
                   if isinstance(entry, str) and entry.startswith("user:\n")]
        if prompts and f"--to {address} --text " in prompts[-1]:
            return True
    return False


def _kept_pane_anomaly(row: staff.StaffRow, state: worker_panes.WorkerPane | worker_panes.WorkerNotObserved,
                       reason: str) -> str:
    """Keep the close reason and pane in the director's one result message."""
    pane = state.pane if isinstance(state, (worker_panes.OpenWorker, worker_panes.StoppedWorker,
                                           worker_panes.ProcessUnknownWorker)) else "unobserved"
    reason = reason.removeprefix(f"anomaly: {row.name}: ")
    message = f"anomaly: {row.name}: {' '.join(reason.splitlines())}; kept pane {pane}"
    print(message, file=sys.stderr)
    return message


@dataclass(frozen=True)
class TurnDecision:
    outcome: TurnOutcome
    send_notice: Callable[[], None]


def _no_notice() -> None:
    pass


def _prepare_notice(worker_name: str, parent: staff.StaffRow, run: Path, passed: dict[str, object],
                    instance: str, outcome: TurnOutcome, path: Path, reply: str, details: str,
                    runs: tuple[str, ...] = ()) -> TurnDecision:
    """Decide the notice under the worker lock; send it after release."""
    from scripts.message import send

    stamp = hashlib.sha256("\n".join((outcome.value, reply, *runs)).encode()).hexdigest()
    if passed.get("hook_notice") == stamp:
        return TurnDecision(outcome, _no_notice)
    phase = passed.get("phase", {})
    phase_id = str(cast(dict[str, object], phase).get("id", "")) if isinstance(phase, dict) else ""
    address = ("codex:" if isinstance(staff.backend(parent), staff.CodexBackend) else "session:") + str(parent.current_session)
    message = f"{worker_name}: phase {phase_id}: {outcome.value}"
    if reply:
        message += f"; reply {path}"
    message += f"; pass {instance}"
    if runs:
        message += "; runs " + ", ".join(runs)
    if details:
        message += "\n" + details
    completion = progress_history.hook_completion(passed)
    if isinstance(completion, progress_history.HookCompletion) and completion.sender_native_id:
        sender = send.read_sender_stamp(completion.sender_native_id)
    else:
        member = staff.snapshot().by_id(staff.StaffId(str(passed.get("worker_id", ""))))
        identity = str(member.current_session) if member is not None else str(passed.get("worker_id", ""))
        sender = (send.ClaudeSender(identity) if member is not None and isinstance(staff.backend(member), staff.ClaudeBackend)
                  else send.CodexSender(identity))
    options = send.parse(["--to", address, "--from", worker_name, "--text", message])
    options = options._replace(sender_origin=send.ForwardedSender(sender), shown_sender=send.ShownSender(worker_name))

    def send_notice() -> None:
        current = staff.snapshot().by_id(parent.id)
        if current is None:
            raise ValueError(f"{parent.id}: notice recipient is not in staff")
        address = ("codex:" if isinstance(staff.backend(current), staff.CodexBackend) else "session:") + str(current.current_session)
        result = send.send(options._replace(selection=send.RosterLookup(address)))
        if result.outcome not in ("sent", "skipped"):
            raise ValueError(result.detail)
        progress_history.record_dispatch_notice(run, instance, stamp)

    return TurnDecision(outcome, send_notice)


def repeat_completion_notice(worker_name: str, parent: staff.StaffRow, run: Path,
                             passed: dict[str, object]) -> Callable[[], None]:
    """Prepare the saved notice for delivery after the caller releases its lock."""
    delivery = progress_history.dispatch_delivery(passed)
    completion = progress_history.hook_completion(passed)
    outcome = TurnOutcome(completion.outcome) if isinstance(completion, progress_history.HookCompletion) else TurnOutcome.DONE
    path = Path(completion.reply_file if isinstance(completion, progress_history.HookCompletion) else delivery.reply_file)
    details = "\n".join(completion.details) if isinstance(completion, progress_history.HookCompletion) else ""
    repeated = {key: value for key, value in passed.items() if key != "hook_notice"}
    decision = _prepare_notice(worker_name, parent, run, repeated, str(passed["instance_id"]), outcome, path, path.read_text(), details)

    if delivery.actions.status_file:
        atomic_file.write_text_atomic(Path(delivery.actions.status_file), "done\n")
    return decision.send_notice


def _submit_stage(parent: staff.StaffRow, row: staff.StaffRow, state: worker_panes.WorkerPane,
                  client: herdr.Herdr, run: Path, passed: dict[str, object], instance: str,
                  delivery: progress_history.DispatchDelivery) -> TurnOutcome | TurnDecision:
    with redirect_stderr(io.StringIO()) as diagnostics:
        try:
            result = (worker.dispatch_turn(parent.id, state, client, delivery.line, delivery.completion_id, run=run)
                      if isinstance(state, worker_panes.OpenWorker) else worker.ExitCode.UNCERTAIN)
        except (OSError, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
            print(str(error), file=sys.stderr)
            result = worker.ExitCode.UNCERTAIN
    if result is worker.DispatchSubmission.SUBMITTED:
        return TurnOutcome.SUBMITTED
    outcome = TurnOutcome.BUSY if result == worker.ExitCode.BUSY else TurnOutcome.UNCERTAIN
    details = diagnostics.getvalue().strip() or f"Stage 2 submission returned {result}"
    path = Path(delivery.reply_file)
    path = path.with_name(path.stem + "_turn" + path.suffix)
    worker.save_reply(path, details)
    return _prepare_notice(row.name, parent, run, passed, instance, outcome, path, details, details)


def _finish_completion(row: staff.StaffRow, parent: staff.StaffRow, run: Path, passed: dict[str, object],
                       instance: str, delivery: progress_history.DispatchDelivery,
                       completion: progress_history.HookCompletion,
                       state: worker_panes.WorkerPane | worker_panes.WorkerNotObserved,
                       hold: worker_panes.WorkerLockHold) -> TurnDecision:
    from scripts.direct import assign
    from scripts.message import send
    path = Path(completion.reply_file)
    if not completion.sender_native_id:
        sender = (send.ClaudeSender(str(row.current_session)) if isinstance(staff.backend(row), staff.ClaudeBackend)
                  else send.CodexSender(str(row.current_session)))
        completion = replace(completion, sender_native_id=send.sender_native_id(sender))
        progress_history.save_hook_completion(run, instance, completion)
    passed = {**passed, "hook_completion": asdict(completion)}
    if not completion.actions_done:
        result = assign.complete_herdr(run, row, passed, delivery.actions, path,
                                      completed=completion.outcome == TurnOutcome.DONE)
        completion = replace(completion, actions_done=True)
        if isinstance(result, assign.CompletionFailed):
            completion = replace(completion, outcome=TurnOutcome.ERROR, details=(*completion.details, result.reason))
        progress_history.save_hook_completion(run, instance, completion)
    outcome = TurnOutcome(completion.outcome)
    progress_history.complete_dispatch(run, str(passed.get("team_slot", row.name)), instance,
                                       status="completed" if outcome == TurnOutcome.DONE else "error")
    details = list(completion.details)
    try:
        _ = scratch.end(scratch.worker_task(row.id))
    except Exception as error:
        details.append(f"scratch cleanup failed for {row.name}: {' '.join(str(error).splitlines())}")
    try:
        if not completion.pane_closed:
            with redirect_stderr(io.StringIO()) as diagnostics:
                if delivery.actions.end_worker:
                    ended = worker.end(parent.id, staff.WorkerName.parse(row.name), lock=hold, retire=False)
                    closed = worker_panes.Closed() if ended == worker.ExitCode.OK else worker_panes.Kept(
                        diagnostics.getvalue().strip() or f"worker end returned {ended}")
                else:
                    closed = worker_panes.close(parent.id, staff.WorkerName.parse(row.name), lock=hold)
            if isinstance(closed, worker_panes.Closed):
                completion = replace(completion, pane_closed=True)
                progress_history.save_hook_completion(run, instance, completion)
            else:
                reason = closed.blocker if isinstance(closed, worker_panes.Kept) else closed.reason
                details.append(_kept_pane_anomaly(row, state, reason))
        if completion.pane_closed and not completion.role_released:
            if delivery.actions.held_role:
                from scripts.astra import astra_roles
                astra_roles.release(delivery.actions.held_role)
            completion = replace(completion, role_released=True)
            progress_history.save_hook_completion(run, instance, completion)
    except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError,
            staff.StaffRefused, staff.StaffUnreadable) as error:
        details.append(_kept_pane_anomaly(row, state, str(error)))
    if not completion.recording_done:
        result = assign.finish_herdr_recording(run, delivery.actions,
            completed=outcome == TurnOutcome.DONE and completion.pane_closed)
        completion = replace(completion, recording_done=True)
        if isinstance(result, assign.CompletionFailed):
            reason = result.reason if outcome == TurnOutcome.DONE else f"{outcome}: {result.reason}"
            completion = replace(completion, outcome=TurnOutcome.ERROR, details=(*completion.details, reason))
            outcome = TurnOutcome.ERROR
            details.append(reason)
    completion = replace(completion, details=tuple(details))
    progress_history.save_hook_completion(run, instance, completion)
    try:
        reply = path.read_text()
    except FileNotFoundError:
        if outcome != TurnOutcome.ERROR:
            raise
        reply = "\n".join(completion.details)
    decision = _prepare_notice(row.name, parent, run, passed, instance, outcome, path, reply, "\n".join(details))

    def send_notice() -> None:
        decision.send_notice()
        if completion.pane_closed and completion.role_released:
            with worker_panes.worker_lock(parent.id, staff.WorkerName.parse(row.name), blocking=True) as final_hold:
                if isinstance(final_hold, worker_panes.Kept):
                    raise ValueError(final_hold.blocker)
                if delivery.actions.end_worker:
                    current = worker_panes.worker_pass(row, worker_panes.worker_run(row))
                    if current.get("instance_id") == instance:
                        _ = staff.retire(row.id)
                progress_history.save_hook_completion(run, instance, replace(completion, finished=True))

    return TurnDecision(outcome, send_notice)


def finish_late_completion(row: staff.StaffRow, parent: staff.StaffRow, hold: worker_panes.WorkerLockHold,
                           new_held_role: str) -> Callable[[], None]:
    """Prepare the unfinished result before a new dispatch replaces its run link."""
    recorded = progress_history.recorded_dispatch_run(row.id)
    if not isinstance(recorded, progress_history.RecordedDispatchRun):
        return _no_notice
    passed = worker_panes.worker_pass(row, recorded.directory)
    completion = progress_history.hook_completion(passed)
    if not isinstance(completion, progress_history.HookCompletion) or completion.finished:
        return _no_notice
    delivery = progress_history.dispatch_delivery(passed)
    same_hold = delivery.actions.held_role == new_held_role
    instance = str(passed.get("instance_id", ""))
    late = f"late result of pass {instance} in run {recorded.directory.name}"
    details = completion.details if late in completion.details else (*completion.details, late)
    state = worker_panes.observe_worker(parent.id, row, staff.snapshot())
    try:
        decision = _finish_completion(row, parent, recorded.directory, passed, instance,
            replace(delivery, actions=replace(delivery.actions, end_worker=False)),
            replace(completion, pane_closed=True,
                    role_released=completion.role_released or same_hold, details=details), state, hold)
    except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError,
            staff.StaffRefused, staff.StaffUnreadable) as error:
        print(f"anomaly: {row.name}: late result of pass {instance} was not reported: {error}; the result is in {completion.reply_file}", file=sys.stderr)
        return _no_notice

    def send_notice() -> None:
        try:
            decision.send_notice()
        except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError,
                staff.StaffRefused, staff.StaffUnreadable) as error:
            print(f"anomaly: {row.name}: late result of pass {instance} was not reported: {error}; the result is in {completion.reply_file}", file=sys.stderr)

    return send_notice


def _ignored(event: WorkerEvent, reason: str, worker_name: str = "unknown") -> TurnOutcome:
    """Save each ignored event for the director without sending a notice."""
    path = herdr.layout_path(herdr.shared_session()).parent / "turn-end-ignored.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        _ = stream.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": reason, "thread_id": event.thread_id, "worker_name": worker_name,
            "event": event.kind}) + "\n")
    return TurnOutcome.IGNORED


def handle(event: WorkerEvent) -> TurnOutcome:
    """Send the decided notice after the worker lock is released."""
    decision = _handle_locked(event)
    if isinstance(decision, TurnDecision):
        decision.send_notice()
        return decision.outcome
    return decision


def _handle_locked(event: WorkerEvent) -> TurnOutcome | TurnDecision:
    """Recover completion records while holding the worker lock."""
    record = staff.snapshot()
    row = record.by_current_session(event.thread_id) if event.thread_id else None
    if row is None:
        return _ignored(event, "no staff row for the current thread")
    if row.kind != "worker":
        return _ignored(event, "the staff row is not a worker", row.name)
    parent = record.parent_of(row)
    if parent is None:
        return _ignored(event, "the worker has no parent", row.name)
    with worker_panes.worker_lock(parent.id, staff.WorkerName.parse(row.name), blocking=True) as hold:
        if isinstance(hold, worker_panes.Kept):
            return _ignored(event, f"the worker lock was kept: {hold.blocker}", row.name)
        run = worker_panes.worker_run(row, record=record)
        passed = worker_panes.worker_pass(row, run)

        def ignore_turn(reason: str) -> TurnOutcome:
            if event.kind == EventKind.TURN_END:
                dispatch = worker_panes.worker_dispatch_state(row, run, hold)
                if not isinstance(dispatch, worker_panes.DispatchHoldsWorker):
                    _ = worker_panes.close(parent.id, staff.WorkerName.parse(row.name), lock=hold)
            return _ignored(event, reason, row.name)

        completion = progress_history.hook_completion(passed)
        if isinstance(completion, progress_history.HookCompletion) and completion.finished:
            return ignore_turn("the hook completion is already finished")
        active = progress_history.active_dispatch(row.id, run)
        if not isinstance(active, progress_history.ActiveDispatch) and not (
                isinstance(completion, progress_history.HookCompletion) or
                passed.get("status") in ("completed", "error")):
            return ignore_turn("no active dispatch or recoverable completion exists")
        instance = active.instance_id if isinstance(active, progress_history.ActiveDispatch) else str(passed.get("instance_id", ""))
        delivery = progress_history.dispatch_delivery(passed)
        if not delivery.line:
            return ignore_turn("the dispatch delivery line is empty")
        client = herdr.Herdr(herdr.shared_session())
        state = worker_panes.observe_worker(parent.id, row, record)
        if isinstance(state, worker_panes.WorkerNotObserved):
            return _ignored(event, "the worker pane was not observed", row.name)
        if isinstance(completion, progress_history.HookCompletion):
            return _finish_completion(row, parent, run, passed, instance, delivery, completion, state, hold)
        receipt = worker.dispatch_receipt(row, delivery.line, delivery.completion_id, run=run)
        if isinstance(receipt, worker.DispatchNotSubmitted):
            if event.kind == EventKind.STAGE_READY and delivery.completion_id != instance:
                return _submit_stage(parent, row, state, client, run, passed, instance, delivery)
            return ignore_turn("the transcript has no matching dispatch prompt")
        if isinstance(receipt, worker.DispatchEvidenceUnknown):
            return _ignored(event, f"the transcript evidence is unknown: {receipt.reason}", row.name)
        turn_reply = receipt.reply if isinstance(receipt, worker.DispatchWaitingForRuns) else receipt
        reply = turn_reply.last_reply if isinstance(turn_reply, worker.DispatchAwaitingReply) else turn_reply.reply
        if (event.kind == EventKind.TURN_END and reply != event.reply
                and worker_admission.reply_ends_dispatch(event.reply, delivery.completion_id)
                and not (isinstance(receipt, worker.DispatchWaitingForRuns) and receipt.has_live_run)):
            receipt = worker.DispatchComplete(event.reply,
                receipt.anomalies if isinstance(receipt, worker.DispatchComplete) else ())
            reply = event.reply
        if (event.kind == EventKind.RUN_FINISHED and not isinstance(receipt, worker.DispatchComplete)
                and isinstance(state, worker_panes.OpenWorker) and state.status == herdr.AgentStatus.WORKING):
            return TurnOutcome.WAITING_ON_REPLY
        if isinstance(receipt, worker.DispatchSubmitted) and not receipt.reply:
            if event.kind == EventKind.STAGE_READY:
                return TurnOutcome.SUBMITTED
            if event.kind == EventKind.TURN_END and not event.reply and receipt.compacted_after_prompt:
                return _prepare_notice(row.name, parent, run, passed, instance, TurnOutcome.NO_DONE,
                                       Path(delivery.reply_file), "", "")
            return _ignored(event, "the submitted dispatch has no final reply", row.name)
        anomalies = receipt.anomalies if isinstance(receipt, worker.DispatchComplete) else ()
        if event.kind == EventKind.TURN_END and (not reply or reply != event.reply):
            reason = "the transcript reply is empty" if not reply else "the transcript reply differs from the turn reply"
            return _ignored(event, reason, row.name)
        if isinstance(receipt, worker.DispatchComplete):
            if delivery.early_ready and delivery.completion_id == instance:
                if not Path(delivery.early_ready).exists():
                    outcome = TurnOutcome.WAITING_ON_STAGE_TWO
                else:
                    completion_id = worker_panes.stage_two_completion_id(instance)
                    diff = delivery.early_ready.removesuffix(".ready") + ".diff"
                    line = (f"Stage 2: the final diff is at {json.dumps(diff)}. "
                            + delivery.line.replace(f"DONE {instance}", f"DONE {completion_id}"))
                    delivery = replace(delivery, line=line, completion_id=completion_id)
                    progress_history.save_dispatch_delivery(run, instance, delivery)
                    return _submit_stage(parent, row, state, client, run, passed, instance, delivery)
            else:
                outcome = TurnOutcome.DONE
        elif isinstance(receipt, worker.DispatchWaitingForRuns):
            outcome = TurnOutcome.WAITING_ON_RUNS if receipt.has_live_run else TurnOutcome.WAITING_ON_REPLY
        elif isinstance(receipt, worker.DispatchAwaitingReply):
            outcome = TurnOutcome.WAITING_ON_REPLY
        elif _friend_pending(client, state, row):
            outcome = TurnOutcome.WAITING_ON_FRIEND
        else:
            outcome = TurnOutcome.NO_DONE
        path = Path(delivery.reply_file)
        if outcome != TurnOutcome.DONE:
            path = path.with_name(path.stem + "_turn" + path.suffix)
        if reply:
            worker.save_reply(path, reply)
        if outcome == TurnOutcome.DONE or outcome == TurnOutcome.NO_DONE and delivery.actions.end_worker:
            completion = progress_history.HookCompletion(outcome, str(path), anomalies)
            progress_history.save_hook_completion(run, instance, completion)
            return _finish_completion(row, parent, run, passed, instance, delivery, completion, state, hold)
        details = "\n".join(anomalies) or reply
        runs = receipt.runs if isinstance(receipt, worker.DispatchWaitingForRuns) else ()
        return _prepare_notice(row.name, parent, run, passed, instance, outcome, path, reply, details, runs)


def report_quota_refusals() -> list[str]:
    """Report a pane Codex worker's quota refusal once per pass; Codex runs no Stop hook for that turn."""
    from scripts.accounts import refusal_watch
    from scripts.whoami import agent_accounts, note_file, quota_alert

    record = staff.local_snapshot()
    lines: list[str] = []
    for row in record.rows:
        parent = record.parent_of(row)
        recorded = progress_history.recorded_dispatch_run(row.id)
        if row.kind != "worker" or parent is None or not isinstance(recorded, progress_history.RecordedDispatchRun):
            continue
        active = progress_history.active_dispatch(row.id, recorded.directory)
        backend = staff.backend(row)
        if (not isinstance(active, progress_history.ActiveDispatch) or not isinstance(backend, staff.CodexBackend)
                or worker_panes.worker_pass(row, recorded.directory).get("quota_refusal")):
            continue
        refusal = refusal_watch.transcript_refusal(str(row.current_session), config_dir=backend.codex_home)
        if not isinstance(refusal, refusal_watch.TranscriptRefusal):
            continue
        try:
            stopped = quota_alert.StoppedWorker(str(parent.current_session), row.name)
            lines += quota_alert.blocked(list(note_file.read_notes()), agent_accounts.codex_account(backend.codex_home),
                                         window=refusal.window, refused_at=refusal.refused_at, stopped=stopped)
            progress_history.record_quota_refusal(recorded.directory, active.instance_id, refusal.entry_id)
        except (OSError, ValueError) as error:
            lines.append(f"anomaly: {row.name}: quota refusal not reported: {' '.join(str(error).splitlines())}")
    return lines


def run_finished(thread_id: str) -> None:
    """Receive a finished run without importing worker types into its supervisor."""
    _ = handle(WorkerEvent(thread_id, EventKind.RUN_FINISHED))


def main(arguments: list[str] | None = None) -> int:
    args = sys.argv[1:] if arguments is None else arguments
    try:
        if len(args) == 2 and args[0] == "stage-ready":
            record = staff.snapshot()
            parent = worker_admission.parent(record, staff_id=staff.runtime_id())
            row = worker_admission.find_worker(record, parent.id, staff.WorkerName.parse(args[1]).value)
            if row is None:
                raise ValueError(f"worker {args[1]} is not in this director's staff")
            _ = handle(WorkerEvent(str(row.current_session), EventKind.STAGE_READY))
            return 0
        separate = args == ["--handle-hook"]
        payload = args[0] if args and not separate else sys.stdin.read()
        value = cast(dict[str, object], json.loads(payload))
        if separate:
            _ = handle(hook_event(value))
        else:
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--handle-hook"],
                start_new_session=True, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with cast(io.BufferedWriter, child.stdin) as stream:
                _ = stream.write(payload.encode())
        return 0
    except (OSError, ValueError, staff.StaffUnreadable, staff.StaffRefused, herdr_types.HerdrFailure) as error:
        print(f"turn_end: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
