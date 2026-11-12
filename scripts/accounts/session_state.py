"""Classify a staff member for the accounts board and the units table."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Literal, cast

from scripts.lib.unit_runs import LauncherRunDirectories
from scripts.production import staff

SessionState = Literal["busy", "idle", "paused", "exited"]
StaffState = Literal["active", "paused", "exited"]


@dataclass(frozen=True)
class LiveActivity:
    status: str


@dataclass(frozen=True)
class NoLiveSession:
    """No live session was observed for this member."""


@dataclass(frozen=True)
class UnrecordedStaff:
    """The observed session has no staff row."""


def staff_state(member: staff.StaffRow | UnrecordedStaff) -> StaffState:
    """Read the member's recorded hold for the style table and session classifier."""
    if isinstance(member, staff.StaffRow):
        if isinstance(member.exited, staff.Exited):
            return "exited"
        if isinstance(member.paused, staff.Paused):
            return "paused"
    return "active"


@dataclass(frozen=True)
class RunningDetachedWork:
    staff_ids: frozenset[staff.StaffId] = frozenset()
    inputs: tuple[Path, ...] = ()
    labels: Mapping[staff.StaffId, tuple[str, ...]] = field(default_factory=lambda: dict[staff.StaffId, tuple[str, ...]]())
    launcher_runs: LauncherRunDirectories = field(default_factory=lambda: LauncherRunDirectories({}, {}))


NO_DETACHED_WORK = RunningDetachedWork()


def running_detached_work(record: staff.StaffSnapshot, *, additional_runs: Sequence[Path] = ()) -> RunningDetachedWork:
    """Read each run folder once for the complete display."""
    from scripts.agents.worker_lifecycle import DETACHED_RUNS_DIR

    live_runs = cast(Callable[[Path, frozenset[staff.StaffId]], Sequence[Mapping[str, object]]],
                     import_module("scripts.agents.detached_runs").live_runs_for_staff)
    worker_directories = cast(Callable[[staff.StaffSnapshot], LauncherRunDirectories],
                              import_module("scripts.herdr.worker_panes").worker_run_directories)

    owners: dict[Path, set[staff.StaffId]] = {}
    for row in record.rows:
        if isinstance(row.run, staff.OpenRun):
            owners.setdefault(row.run.path.resolve(), set()).add(row.id)
    directories = worker_directories(record)
    for identity, folders in directories.by_worker.items():
        for folder in folders:
            owners.setdefault(folder.resolve(), set()).add(identity)
    identities = {row.id for row in record.find(kind="worker")}
    for folder in additional_runs:
        owners.setdefault(folder.resolve(), set()).update(identities)
    busy: set[staff.StaffId] = set()
    labels: dict[staff.StaffId, list[str]] = {}
    inputs: set[Path] = set()
    for folder, identities in owners.items():
        directory = folder / DETACHED_RUNS_DIR
        inputs.add(directory)
        inputs.update(directory.glob("*.json"))
        for run in live_runs(directory, frozenset(identities)):
            row = record.by_id(staff.StaffId(str(run.get("staff_id", ""))))
            if row is not None:
                busy.add(row.id)
                label = run.get("label")
                if isinstance(label, str):
                    labels.setdefault(row.id, []).append(label)
    return RunningDetachedWork(frozenset(busy), tuple(sorted(inputs)),
                               {identity: tuple(items) for identity, items in labels.items()}, directories)


def classify(member: staff.StaffRow | UnrecordedStaff,
             activity: LiveActivity | NoLiveSession, *,
             detached: RunningDetachedWork = NO_DETACHED_WORK) -> SessionState:
    """Staff exit and pause records take precedence over observed activity."""
    hold = staff_state(member)
    if hold != "active":
        return hold
    if isinstance(member, staff.StaffRow) and member.id in detached.staff_ids:
        return "busy"
    if isinstance(activity, NoLiveSession):
        return "exited"
    return "busy" if activity.status == "busy" else "idle"
