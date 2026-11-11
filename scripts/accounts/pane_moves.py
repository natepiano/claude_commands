"""Move a pane worker through its shared conversation and terminal APIs."""
from __future__ import annotations

import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from scripts.accounts import accounts, moves, refusal_watch
from scripts.agents.worker_lifecycle import WorkerStateUnreadable
from scripts.accounts.accounts import SessionMoveOutcome
from scripts.herdr import herdr, worker_panes
from scripts.direct import progress_history
from scripts.lib import conf
from scripts.lib.resume_message import AccountMove, BusyPass, resume_message
from scripts.production import session_staff, staff
from scripts.agents import restart, stop_resume, stop_results

WorkerStartFailure = (worker_panes.IncompleteStart | worker_panes.AdmissionRefused | worker_panes.UnpublishedWorker
                      | worker_panes.ProcessUnknownWorker | worker_panes.UncertainWorker | worker_panes.StartRefused)


@dataclass(frozen=True)
class WorkerMove:
    row: staff.StaffRow
    parent: staff.StaffId
    family: str
    target: str
    why: str
    run: Path
    client: herdr.Herdr


@dataclass(frozen=True)
class WorkerResume:
    """A worker can resume its conversation on the destination account."""

    state: worker_panes.OpenWorker | worker_panes.StoppedWorker | worker_panes.ClosedWorker
    role: staff.WorkerRole
    source: str
    close_check: worker_panes.CloseAllowed | worker_panes.Kept = worker_panes.CloseAllowed()

    @property
    def kept_open_reason(self) -> str:
        return self.close_check.blocker if isinstance(self.close_check, worker_panes.Kept) else ""


def _observe(move: WorkerMove) -> worker_panes.WorkerPane:
    states = worker_panes.observe(move.parent, staff.local_snapshot())
    return next((item for item in states if item.row.id == move.row.id),
                worker_panes.UncertainWorker(move.row, "worker is absent from staff"))


def _failed(row: staff.StaffRow, result: WorkerStartFailure) -> accounts.SessionMoveRefused:
    if isinstance(result, worker_panes.StartRefused):
        return accounts.SessionMoveRefused(row.id, f"{result.reason.value}: {result.detail}", 2)
    if isinstance(result, worker_panes.AdmissionRefused):
        return accounts.SessionMoveRefused(row.id, result.reason, 2)
    reason = result.cause if isinstance(result, worker_panes.UnpublishedWorker) else result.reason
    return accounts.SessionMoveRefused(row.id, f"anomaly: {reason}")


def _confirm(move: WorkerMove, common: moves.MoveCommon) -> None:
    confirmed = moves.ConfirmedMove(**common, kind="confirmed", worker=move.row.name, run=str(move.run))
    moves.append_move(confirmed)


def _confirm_pending(move: WorkerMove) -> None:
    entries = moves.read_moves()
    request = next((entry for entry in reversed(entries) if entry["kind"] == "requested"
                    and entry["subject"] == move.row.id and entry["to"] == move.target), None)
    if request is not None and not any(entry["kind"] == "confirmed" and entry["move_id"] == request["move_id"]
                                       for entry in entries):
        _confirm(move, moves.MoveCommon(at=request["at"], tool=request["tool"], subject=move.row.id, move_id=request["move_id"]))


def _source_account(row: staff.StaffRow) -> str:
    kind, environment, _ = restart.session_launch(str(row.current_session), row.name)
    if kind == "codex":
        return accounts.codex_home_label(Path(environment["CODEX_HOME"]))
    return accounts.claude_config_label(Path(environment.get("CLAUDE_CONFIG_DIR", str(accounts.main_config_dir()))))


def _request(move: WorkerMove, source: str, terminal: str) -> moves.MoveCommon:
    common = moves.MoveCommon(at=datetime.now(timezone.utc).isoformat(), tool="codex" if move.family == "codex" else "claude",
                              subject=move.row.id, move_id=uuid4().hex)
    requested: moves.RequestedMove = {**common, "kind": "requested", "terminal": terminal,
                                      "from": source, "to": move.target, "why": move.why, "window": "unknown"}
    moves.append_move(requested)
    refusal = refusal_watch.transcript_refusal(str(move.row.current_session))
    if isinstance(refusal, refusal_watch.TranscriptRefusal):
        progress_history.mark_pass_refused(move.run, move.row.name)
    return common


def _continue_pass(move: WorkerMove, opened: worker_panes.OpenWorker, source: str, target: str) -> None:
    passed = worker_panes.worker_pass(move.row, move.run)
    if passed.get("status") in ("active", "refused"):
        move.client.prompt(opened.pane, resume_message(move.why, move=AccountMove(source, target),
                           own_pass=BusyPass(str(passed["instance_id"]))),
                           target=str(move.row.current_session))


def _recover_source(move: WorkerMove, source: str, role: staff.WorkerRole, hold: worker_panes.WorkerLockHold) -> str:
    fresh = _observe(move)
    if not isinstance(fresh, (worker_panes.StoppedWorker, worker_panes.ClosedWorker)):
        return "source not started; no fresh proof that the target process is absent"
    rollback = worker_panes.start_or_resume(move.parent, staff.WorkerName.parse(move.row.name), role,
                account=worker_panes.DestinationWorkerAccount(source), lock=hold)
    if isinstance(rollback, worker_panes.OpenWorker):
        _continue_pass(move, rollback, move.target, source)
        return f"source resumed on {source}"
    return _failed(move.row, rollback).reason


def _resume_destination(move: WorkerMove, source: str, state: worker_panes.OpenWorker | worker_panes.StoppedWorker | worker_panes.ClosedWorker,
                        role: staff.WorkerRole, hold: worker_panes.WorkerLockHold) -> SessionMoveOutcome:
    terminal = state.terminal if isinstance(state, (worker_panes.OpenWorker, worker_panes.StoppedWorker)) else ""
    common = _request(move, source, terminal)
    began = time.monotonic()
    if isinstance(state, worker_panes.OpenWorker):
        settings = conf.read("shutdown")
        stop_seconds = sum(settings.whole(key) for key in ("STOP_GRACE_SECONDS", "KILL_GRACE_SECONDS", "MOVE_STOP_MARGIN_SECONDS"))
        stopped = restart.stop_row(move.row, move.client, deadline=time.monotonic() + stop_seconds)
        fresh = _observe(move)
        if not stopped or not isinstance(fresh, (worker_panes.StoppedWorker, worker_panes.ClosedWorker)):
            reason = "anomaly: stop did not prove the worker stopped"
            moves.append_move(moves.FailedMove(**common, kind="failed", reason=reason))
            return accounts.SessionMoveRefused(move.row.id, reason)
    result = worker_panes.start_or_resume(move.parent, staff.WorkerName.parse(move.row.name), role,
                account=worker_panes.DestinationWorkerAccount(move.target), lock=hold)
    if isinstance(result, worker_panes.OpenWorker):
        elapsed_seconds = time.monotonic() - began
        _confirm(move, common)
        _continue_pass(move, result, source, move.target)
        return stop_results.ClaudeUp(move.row.id, result.pane, move.target, elapsed_seconds=elapsed_seconds)
    failure = _failed(move.row, result)
    reason = f"{failure.reason}; {_recover_source(move, source, role, hold)}"
    moves.append_move(moves.FailedMove(**common, kind="failed", reason=reason))
    return replace(failure, reason=reason)


def _move_decision(move: WorkerMove, *, lock: worker_panes.WorkerLockHold | worker_panes.WorkerLockAtUse =
                   worker_panes.WorkerLockAtUse.ACQUIRE
                   ) -> WorkerResume | accounts.IdleWorkerPaneClose | accounts.SessionMoveRefused | stop_results.ClaudeUp:
    """Use the same evidence for a preview and a locked move."""
    locked = worker_panes.check_worker_lock(move.row, lock)
    if isinstance(locked, worker_panes.Kept):
        return accounts.SessionMoveRefused(move.row.id, locked.blocker)
    state = _observe(move)
    if isinstance(state, (worker_panes.ProcessUnknownWorker, worker_panes.UncertainWorker)):
        return accounts.SessionMoveRefused(move.row.id, f"anomaly: {state.reason}")
    active = progress_history.active_dispatch(move.row.id, move.run)
    if isinstance(active, progress_history.ActiveDispatchUnavailable):
        return accounts.SessionMoveRefused(move.row.id, f"anomaly: {' '.join(active.reason.splitlines())}")
    if isinstance(active, progress_history.NoActiveDispatch) and isinstance(state, worker_panes.ClosedWorker):
        return accounts.SessionMoveRefused(move.row.id, "nothing to move", 0)
    close_check: worker_panes.CloseAllowed | worker_panes.Kept = worker_panes.CloseAllowed()
    if (isinstance(state, (worker_panes.OpenWorker, worker_panes.StoppedWorker))
            and worker_panes.worker_state(state) == "idle"):
        close_check = worker_panes.can_close(state, run=worker_panes.DispatchRun(move.run), lock=lock)
        if isinstance(close_check, worker_panes.CloseAllowed):
            return accounts.IdleWorkerPaneClose()
    role = worker_panes.dispatch_role(move.row)
    if isinstance(role, worker_panes.WorkerRoleUnavailable):
        return accounts.SessionMoveRefused(move.row.id, role.reason)
    if isinstance(state, worker_panes.OpenWorker):
        running = stop_resume.running_on_account(str(move.row.current_session), state.pane, kind=move.family)
        if not isinstance(running, stop_results.ClaudeUp):
            reason = running.reason if isinstance(running, stop_results.ResumeFailed) else "live worker account is unproved"
            return accounts.SessionMoveRefused(move.row.id, f"anomaly: {reason}")
        source = running.account
        if source == move.target:
            return replace(running, session_id=move.row.id)
    else:
        source = _source_account(move.row)
    return WorkerResume(state, role, source, close_check)


def _move_locked(move: WorkerMove, hold: worker_panes.WorkerLockHold) -> SessionMoveOutcome:
    decision = _move_decision(move, lock=hold)
    if isinstance(decision, accounts.IdleWorkerPaneClose):
        closed = worker_panes.close(move.parent, staff.WorkerName.parse(move.row.name), lock=hold)
        if isinstance(closed, worker_panes.Closed):
            return accounts.SessionIdlePaneClosed(move.row.id)
        reason = closed.blocker if isinstance(closed, worker_panes.Kept) else closed.reason
        return accounts.SessionMoveRefused(move.row.id, reason)
    if isinstance(decision, WorkerResume):
        return _resume_destination(move, decision.source, decision.state, decision.role, hold)
    if isinstance(decision, stop_results.ClaudeUp):
        _confirm_pending(move)
    return decision


def run_for_worker(row: staff.StaffRow, record: staff.StaffSnapshot) -> Path:
    if not row.current_session:
        return Path()
    parent = record.parent_of(row)
    try:
        return worker_panes.worker_run(row, record=record) if parent is not None else Path()
    except WorkerStateUnreadable as error:
        raise ValueError(f"anomaly: {error}") from error


def _worker_move(row: staff.StaffRow, record: staff.StaffSnapshot, target: str,
                 why: str, run: Path) -> WorkerMove | accounts.SessionMoveRefused:
    if not row.current_session:
        return accounts.SessionMoveRefused(row.id, "nothing to move", 0)
    parent = record.parent_of(row)
    if parent is None:
        return accounts.SessionMoveRefused(row.id, "worker parent is unavailable")
    return WorkerMove(row, parent.id, session_staff.worker_family(record, row.id), target, why, run,
                      herdr.Herdr(herdr.shared_session()))


def preview_worker(row: staff.StaffRow, record: staff.StaffSnapshot, target: str,
                   why: str, run: Path) -> WorkerResume | accounts.IdleWorkerPaneClose | accounts.SessionMoveRefused | stop_results.ClaudeUp:
    """Read a worker's move decision without stopping or resuming it."""
    move = _worker_move(row, record, target, why, run)
    return move if isinstance(move, accounts.SessionMoveRefused) else _move_decision(move)


def move_worker(row: staff.StaffRow, record: staff.StaffSnapshot, target: str, why: str, run: Path) -> SessionMoveOutcome:
    move = _worker_move(row, record, target, why, run)
    if isinstance(move, accounts.SessionMoveRefused):
        return move
    with worker_panes.worker_lock(move.parent, staff.WorkerName.parse(row.name)) as hold:
        if isinstance(hold, worker_panes.Kept):
            return accounts.SessionMoveRefused(row.id, hold.blocker)
        return _move_locked(move, hold)
