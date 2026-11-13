#!/usr/bin/env python3
"""Stop every Codex worker on this machine when Codex capacity ends.

quota_alert.py starts `stop` when no-worker mode goes on. Each Codex worker
stops with proof, each open pass of a stopped worker ends as canceled, and each
affected unit director gets one report. Claude workers are not touched.

No command starts workers again. When capacity returns, quota_alert.py sends
the notice, and the next assign of a unit director starts its worker.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import sys
import time
from dataclasses import dataclass
from typing import Literal

from scripts.agents import detached_runs, restart
from scripts.direct import progress_history
from scripts.herdr import herdr, herdr_types, worker_panes
from scripts.lib import conf
from scripts.production import broadcast, session_staff, showrunners, staff

SENDER = "worker-capacity"
# A loaded machine can pass the stop deadline between SIGTERM and SIGKILL (seen 2026-11-13).
STOP_ATTEMPTS = 2
# A dispatch holds the worker lock while it submits; the stop waits this long for it.
LOCK_WAIT_SECONDS = 60
USAGE = "usage: worker_capacity.py stop"


@dataclass(frozen=True)
class PassesNotCanceled:
    reason: str


@dataclass(frozen=True)
class WorkerStopped:
    """The worker process is proved absent."""

    name: str
    process: Literal["stopped", "absent"]
    canceled_passes: int | PassesNotCanceled
    detached_runs: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkerNotStopped:
    name: str
    reason: str


def _observe(parent: staff.StaffRow, row: staff.StaffRow) -> worker_panes.WorkerPane:
    states = worker_panes.observe(parent.id, staff.local_snapshot())
    return next((state for state in states if state.row.id == row.id),
                worker_panes.UncertainWorker(row, "worker is absent from staff"))


def _stop_seconds() -> int:
    settings = conf.read("shutdown")
    return sum(settings.whole(key) for key in ("STOP_GRACE_SECONDS", "KILL_GRACE_SECONDS", "MOVE_STOP_MARGIN_SECONDS"))


def stop_worker(parent: staff.StaffRow, row: staff.StaffRow, client: herdr.Herdr) -> WorkerStopped | WorkerNotStopped:
    """Stop one worker with proof, then end its open passes as canceled."""
    name = staff.WorkerName.parse(row.name)
    with worker_panes.worker_lock(parent.id, name, deadline=time.monotonic() + LOCK_WAIT_SECONDS) as hold:
        if isinstance(hold, worker_panes.Kept):
            return WorkerNotStopped(row.name, hold.blocker)
        state = _observe(parent, row)
        process: Literal["stopped", "absent"] = "absent"
        for _ in range(STOP_ATTEMPTS):
            if not isinstance(state, worker_panes.OpenWorker):
                break
            process = "stopped"
            _ = restart.stop_row(state.row, client, deadline=time.monotonic() + _stop_seconds())
            state = _observe(parent, row)
        if isinstance(state, (worker_panes.ProcessUnknownWorker, worker_panes.UncertainWorker)):
            return WorkerNotStopped(row.name, state.reason)
        if isinstance(state, worker_panes.OpenWorker):
            return WorkerNotStopped(row.name, "the stop did not prove that the worker stopped")
        run = worker_panes.worker_run(row)
        canceled: int | PassesNotCanceled
        try:
            canceled = progress_history.cancel_worker_passes(run, row.id)
        except (OSError, ValueError, SystemExit) as error:
            canceled = PassesNotCanceled(" ".join(str(error).split()))
        runs = run / detached_runs.DETACHED_RUNS_DIR
        detached = tuple(f"{record['run_id']}: result {runs / (record['run_id'] + '.json')}; log {record['log']}"
                         for record in detached_runs.live_runs_for_staff(runs, frozenset({row.id})))
        if isinstance(state, worker_panes.StoppedWorker):
            # The stop is complete here. The pane closes only when the close checks permit it.
            try:
                _ = worker_panes.close(parent.id, name, lock=hold)
            except (herdr_types.HerdrFailure, OSError, ValueError) as error:
                print(f"worker-capacity: {row.name}: pane not closed: {' '.join(str(error).split())}", file=sys.stderr)
        return WorkerStopped(row.name, process, canceled, detached)


def report_text(outcomes: list[WorkerStopped | WorkerNotStopped]) -> str:
    """Write one report for a unit director; the empty string when nothing changed."""
    stopped = [item for item in outcomes if isinstance(item, WorkerStopped)]
    lines: list[str] = []
    if names := [item.name for item in stopped if item.process == "stopped"]:
        lines.append(f"Stopped: {', '.join(names)}.")
    if names := [item.name for item in stopped if isinstance(item.canceled_passes, int) and item.canceled_passes]:
        lines.append(f"Passes ended as canceled: {', '.join(names)}. Continue that work yourself from the worktree.")
    for item in stopped:
        if isinstance(item.canceled_passes, PassesNotCanceled):
            lines.append(f"Pass stays open: {item.name}: {item.canceled_passes.reason}.")
        lines.extend(f"Detached run of {item.name} continues: {run}." for run in item.detached_runs)
    lines.extend(f"Not proved stopped: {item.name}: {item.reason}. Send it no work."
                 for item in outcomes if isinstance(item, WorkerNotStopped))
    if not lines:
        return ""
    return "\n".join(["Codex workers have no capacity. This is the stop report for your Codex workers.", *lines])


def _fault_lines(parent: staff.StaffRow, outcomes: list[WorkerStopped | WorkerNotStopped]) -> list[str]:
    return ([f"{parent.name}: {item.name}: not proved stopped: {item.reason}"
             for item in outcomes if isinstance(item, WorkerNotStopped)]
            + [f"{parent.name}: {item.name}: pass not canceled: {item.canceled_passes.reason}"
               for item in outcomes if isinstance(item, WorkerStopped) and isinstance(item.canceled_passes, PassesNotCanceled)])


def _send(recipient: str, text: str) -> bool:
    failure = broadcast.send(recipient, SENDER, text)
    if failure:
        print(f"worker-capacity: report to {recipient} failed: {failure}", file=sys.stderr)
    return not failure


def stop() -> int:
    """Stop the Codex workers of every local parent and report to each affected director."""
    record = staff.snapshot()
    parents = session_staff.pane_parents(record)
    observed = worker_panes.observe_parents(parents, record)
    scanned = worker_panes.parent_run_index(record)
    client = herdr.Herdr(herdr.shared_session())
    faults: list[str] = []
    delivered = True
    for parent in parents:
        outcomes: list[WorkerStopped | WorkerNotStopped] = []
        for state in observed[parent.id]:
            if not isinstance(record.backends.get(state.row.id), staff.CodexBackend):
                continue
            try:
                # A closed worker with no open pass needs no stop and no report.
                if isinstance(state, worker_panes.ClosedWorker) and isinstance(progress_history.active_dispatch(
                        state.row.id, worker_panes.worker_run(state.row, record=record, scanned=scanned)),
                        progress_history.NoActiveDispatch):
                    continue
                outcomes.append(stop_worker(parent, state.row, client))
            except (herdr_types.HerdrFailure, staff.StaffRefused, OSError, ValueError) as error:
                outcomes.append(WorkerNotStopped(state.row.name, " ".join(str(error).split())))
        faults.extend(_fault_lines(parent, outcomes))
        if text := report_text(outcomes):
            print(f"{parent.name}:\n{text}")
            delivered = _send(str(parent.id), text) and delivered
    if faults:
        text = "\n".join(["Codex worker stop is incomplete. A Codex worker that still runs can spend credits.", *faults])
        print(text, file=sys.stderr)
        delivered = _send(showrunners.load_settings_from(showrunners.CONFIG)["faults_to"], text) and delivered
    return 0 if delivered and not faults else 1


def main(arguments: list[str]) -> int:
    if arguments != ["stop"]:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        return stop()
    except (staff.StaffUnreadable, herdr_types.HerdrFailure, OSError, ValueError) as error:
        print(f"worker-capacity: {' '.join(str(error).split())}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
