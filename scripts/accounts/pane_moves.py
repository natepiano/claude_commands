"""Move a pane worker through its shared conversation and terminal APIs."""
from __future__ import annotations

import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from scripts.accounts import accounts, moves, refusal_watch
from scripts.accounts.accounts import SessionMoveOutcome
from scripts.herdr import herdr, worker_panes
from scripts.direct import progress_history
from scripts.lib import conf
from scripts.lib.resume_message import AccountMove, BusyPass, resume_message
from scripts.production import session_staff, staff
from scripts.shutdown import restart, stop_resume, stop_results
from scripts.whoami import agent_accounts

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
    if move.family == "codex":
        confirmed["home"] = str(agent_accounts.codex_account_home(move.target).home)
    else:
        confirmed["config_dir"] = str(accounts.claude_account_dir(move.target))
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


def _recover_source(move: WorkerMove, source: str, original: worker_panes.OpenWorker | worker_panes.StoppedWorker | worker_panes.ClosedWorker,
                    role: staff.WorkerRole, hold: worker_panes.WorkerLockHold) -> str:
    fresh = _observe(move)
    terminal = original.terminal if isinstance(original, (worker_panes.OpenWorker, worker_panes.StoppedWorker)) else ""
    if not (isinstance(fresh, worker_panes.StoppedWorker) and (not terminal or fresh.terminal == terminal)
            or isinstance(fresh, worker_panes.ClosedWorker) and isinstance(original, worker_panes.ClosedWorker)):
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
        if not stopped or not isinstance(fresh, worker_panes.StoppedWorker) or fresh.terminal != terminal:
            reason = "anomaly: stop did not prove the same worker terminal at its shell"
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
    reason = f"{failure.reason}; {_recover_source(move, source, state, role, hold)}"
    moves.append_move(moves.FailedMove(**common, kind="failed", reason=reason))
    return replace(failure, reason=reason)


def _move_locked(move: WorkerMove, hold: worker_panes.WorkerLockHold) -> SessionMoveOutcome:
    state = _observe(move)
    if isinstance(state, (worker_panes.ProcessUnknownWorker, worker_panes.UncertainWorker)):
        return accounts.SessionMoveRefused(move.row.id, f"anomaly: {state.reason}")
    active = progress_history.active_dispatch(move.row.id, move.run)
    if isinstance(active, progress_history.ActiveDispatchUnavailable):
        return accounts.SessionMoveRefused(move.row.id, f"anomaly: {' '.join(active.reason.splitlines())}")
    if isinstance(active, progress_history.NoActiveDispatch):
        if isinstance(state, (worker_panes.OpenWorker, worker_panes.StoppedWorker)):
            closed = worker_panes.close(move.parent, staff.WorkerName.parse(move.row.name),
                                        run=worker_panes.DispatchRun(move.run), lock=hold)
            if isinstance(closed, worker_panes.Kept):
                return accounts.SessionMoveRefused(move.row.id, closed.blocker)
            if isinstance(closed, worker_panes.UncertainWorker):
                return accounts.SessionMoveRefused(move.row.id, f"anomaly: {closed.reason}")
        return accounts.SessionMoveRefused(move.row.id, "nothing to move", 0)
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
            _confirm_pending(move)
            return replace(running, session_id=move.row.id)
    else:
        source = _source_account(move.row)
    return _resume_destination(move, source, state, role, hold)


def run_for_worker(row: staff.StaffRow, record: staff.StaffSnapshot) -> Path:
    parent = record.parent_of(row)
    return worker_panes.worker_run(parent) if parent is not None else Path()


def move_worker(row: staff.StaffRow, record: staff.StaffSnapshot, target: str, why: str, run: Path) -> SessionMoveOutcome:
    if not row.current_session:
        return accounts.SessionMoveRefused(row.id, "nothing to move", 0)
    parent = record.parent_of(row)
    if parent is None:
        return accounts.SessionMoveRefused(row.id, "worker parent is unavailable")
    move = WorkerMove(row, parent.id, session_staff.worker_family(record, row.id), target, why, run,
                      herdr.Herdr(herdr.shared_session()))
    with worker_panes.worker_lock(parent.id, staff.WorkerName.parse(row.name)) as hold:
        if isinstance(hold, worker_panes.Kept):
            return accounts.SessionMoveRefused(row.id, hold.blocker)
        return _move_locked(move, hold)
