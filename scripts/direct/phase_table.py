#!/usr/bin/env python3
"""Build and render a unit run's plan-phase table from durable events."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_SCRIPTS = REPOSITORY_ROOT / "scripts" / "production"
if not __package__:
    sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(PRODUCTION_SCRIPTS))

from scripts.direct import plan_history
from scripts.lib.user_zone import user_zone
from scripts.production import showrunners, unit_lookup
from scripts.production.add_unit import Production, read_production
from production_units import Refusal, cell_value, live_unit_table
from scripts.production.production_doc import PromoteTo, promotion_from_doc

# The finding_opened event schema uses these severities. Importing findings to
# read its SEVERITIES also validates command-only convergence configuration.
FINDING_SEVERITIES = ("blocker", "minor", "nit")


class ProjectedEta(NamedTuple):
    """A progress report's projected finish and uncertainty band."""

    time: datetime
    earliest: datetime
    latest: datetime
    as_of: datetime


class EtaRange(NamedTuple):
    """The earliest and latest targets stated with an ETA."""

    earliest: datetime
    latest: datetime


class NoEtaRange(NamedTuple):
    """A stated ETA was an exact target without a range."""


class StatedEta(NamedTuple):
    """A running phase's stated schedule, advanced to now when overdue.

    The original promises remain in the phase's ETA trail.
    """

    time: datetime
    range: EtaRange | NoEtaRange
    stated_at: datetime
    basis: str


class EtaUnavailable(NamedTuple):
    """No current stated or projected finish can be shown."""


class ReportedPhaseProgress(NamedTuple):
    """The last progress report written for a running phase."""

    percent: int


class PhaseProgressNotReported(NamedTuple):
    """A running phase has no valid progress report."""


class FirstStatedEtaTarget(NamedTuple):
    """The target promised by a phase instance's first stated ETA."""

    time: datetime
    repair_rounds: int


class EtaNeverStated(NamedTuple):
    """A phase instance has never had a stated ETA."""


class CompletedPhaseTiming(NamedTuple):
    """Recorded timing for the completed instance family of a done phase."""

    start: datetime
    finish: datetime
    seconds: int


class PhaseTimingNotRecorded(NamedTuple):
    """A done plan phase has no completed instance on record."""


class PredictedPhaseTiming(NamedTuple):
    """Projected timing for a future plan phase."""

    start: datetime
    finish: datetime


class PhaseTimingNotPredicted(NamedTuple):
    """A future plan phase cannot yet be projected."""


class PhaseNotFinished(NamedTuple):
    """This phase has no recorded completion time."""


class FinishDeltaUnavailable(NamedTuple):
    """A finish cannot be compared with a stated ETA."""


@dataclass(frozen=True, kw_only=True)
class PhaseHistory:
    """Promises and work recorded across a plan phase's instances."""

    first_eta: datetime | EtaNeverStated = EtaNeverStated()
    moves: int = 0
    eta_trail: list[tuple[datetime, datetime]] = field(default_factory=list)
    finished_at: datetime | PhaseNotFinished = PhaseNotFinished()
    delta_minutes: float | FinishDeltaUnavailable = FinishDeltaUnavailable()
    fix_done: int = 0
    fix_open_passes: int = 0
    findings: dict[str, int] = field(default_factory=lambda: dict.fromkeys(FINDING_SEVERITIES, 0))

    @property
    def fix_running(self) -> int:
        """Whether any fix pass is still open."""
        return int(self.fix_open_passes > 0)

    @property
    def fix_rounds(self) -> int:
        """All fix passes started, including parallel open passes."""
        return self.fix_done + self.fix_open_passes


class NoGate(NamedTuple):
    """No production gate restricts the phase's prediction."""


class TimedGate(NamedTuple):
    """A production gate supplies the earliest possible start."""

    gate: str
    until: datetime
    waits_on: str


class UntimedGate(NamedTuple):
    """A production gate has no known clearing time."""

    gate: str
    waits_on: str


@dataclass(frozen=True)
class DonePhase(PhaseHistory):
    """A plan phase marked done in the current plan."""

    phase: str
    title: str
    times: CompletedPhaseTiming | PhaseTimingNotRecorded


@dataclass(frozen=True)
class OpenPhase(PhaseHistory):
    """A phase started but not finished in the newest run's events.

    This says nothing about whether the unit's session is still alive.
    """

    phase: str
    title: str
    started: datetime
    progress: ReportedPhaseProgress | PhaseProgressNotReported
    eta: StatedEta | ProjectedEta | EtaUnavailable
    first_stated: FirstStatedEtaTarget | EtaNeverStated
    instance_id: str
    schedule_end: datetime | EtaUnavailable = EtaUnavailable()


@dataclass(frozen=True)
class TodoPhase(PhaseHistory):
    """An unfinished plan phase that is not currently running."""

    phase: str
    title: str
    times: PredictedPhaseTiming | PhaseTimingNotPredicted
    gate: NoGate | TimedGate | UntimedGate = NoGate()


class NoOpenPhase(NamedTuple):
    """The newest recorded run has no unfinished phase instance."""


class FinishedAt(NamedTuple):
    """The plan's recorded completion time."""

    at: datetime


class PredictedFinish(NamedTuple):
    """The plan's projected completion time."""

    at: datetime


class UnknownFinish(NamedTuple):
    """The plan's completion time cannot yet be determined."""


class GateBlockedFinish(NamedTuple):
    """The plan cannot finish before an untimed production gate clears."""

    gate: str
    waits_on: str


class CouldStart(NamedTuple):
    """Waiting phases whose dependencies are all done."""

    phases: tuple[str, ...]


class NothingToStart(NamedTuple):
    """No waiting phase can start; names what the first one waits on."""

    reason: str


class DependenciesUnknown(NamedTuple):
    """Waiting phases with no `**Depends on:**` line in the plan."""

    phases: tuple[str, ...]


ParallelCheck = CouldStart | NothingToStart | DependenciesUnknown


class PhaseRecord(NamedTuple):
    """The complete read-only phase view for one worker session."""

    plan: Path
    updated: datetime
    current: OpenPhase | NoOpenPhase
    phases: list[DonePhase | OpenPhase | TodoPhase]
    plan_finish: FinishedAt | PredictedFinish | UnknownFinish | GateBlockedFinish
    parallel: ParallelCheck


class InstanceFinished(NamedTuple):
    """The finish event written for one phase instance."""

    at: datetime
    status: str
    seconds: int
    written_at: int


class PhaseInstanceWithoutFinish(NamedTuple):
    """No finish event was written for this phase instance."""


class PhaseInstance(NamedTuple):
    """One recorded attempt at a phase, independent of its current plan id."""

    instance_id: str
    phase: str
    title: str
    start: datetime
    finish: InstanceFinished | PhaseInstanceWithoutFinish


class OpenPhaseInstance(NamedTuple):
    """The unfinished event-backed instance carried through the builder."""

    phase: PhaseInstance
    progress: ReportedPhaseProgress | PhaseProgressNotReported
    eta: StatedEta | ProjectedEta | EtaUnavailable
    first_stated: FirstStatedEtaTarget | EtaNeverStated


class ProgressReportTimeNotRecorded(NamedTuple):
    """A progress event has no usable report time."""


class ProgressElapsedTimeNotRecorded(NamedTuple):
    """A progress event has no recorded elapsed duration."""


class LastPhaseProgressEvent(NamedTuple):
    """The latest valid progress event for one phase instance."""

    percent: int
    reported_at: datetime | ProgressReportTimeNotRecorded
    elapsed: int | ProgressElapsedTimeNotRecorded
    spread: float


class PhaseProgressEventNotRecorded(NamedTuple):
    """A phase instance has no valid progress event."""


class LatestStatedEtaEvent(NamedTuple):
    """The latest stated ETA and the first target for one phase instance."""

    eta: StatedEta
    first: FirstStatedEtaTarget


class NoStatedEtaEvent(NamedTuple):
    """A phase instance has no valid stated ETA event."""


class TypicalDuration(NamedTuple):
    """The duration used to project unfinished phases."""

    seconds: int


class UnknownDuration(NamedTuple):
    """History cannot yet supply a typical phase duration."""


class PlanPhasePosition(NamedTuple):
    """The active phase's row in the current plan."""

    offset: int


class OutsidePlan(NamedTuple):
    """The active phase has no heading in the current plan."""


class ProductionReference(NamedTuple):
    """The production and unit named by a worker plan."""

    production: str
    unit: str
    document: str


class ProductionUnitPlan(NamedTuple):
    """A live production unit and the plan inside its own worktree."""

    unit: str
    plan: Path


class PlanOutsideProduction(NamedTuple):
    """The plan does not belong to a production."""


class NoteOwnership(NamedTuple):
    """The identity keys on a generated phase note."""

    production: str
    unit: str


class NoteWithoutPhaseTableOwnership(NamedTuple):
    """A note cannot be identified as this script's output."""


class MostRecentOwnedPhaseNote(NamedTuple):
    """The newest generated note found for one production unit."""

    path: Path


class NoOwnedPhaseNote(NamedTuple):
    """The vault has no generated note for one production unit."""


class NoState(Exception):
    """The requested worker session has no readable progress state."""


class NoPlan(Exception):
    """The requested worker session does not identify a readable plan."""


def _json_object(text: str) -> dict[str, object] | None:
    try:
        parsed: object = json.loads(text)  # pyright: ignore[reportAny]
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return cast(dict[str, object], parsed)


def _object_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return cast(dict[str, object], value)


def _text(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _state(session_dir: Path) -> dict[str, object]:
    state_path = session_dir / plan_history.STATE_FILENAME
    try:
        text = state_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise NoState(f"No progress state in {session_dir}") from error
    state = _json_object(text)
    if state is None:
        raise NoState(f"Invalid progress state in {session_dir}")
    return state


def _events(path: Path) -> list[dict[str, object]]:
    try:
        with path.open(encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            lines = handle.read().splitlines()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        return []
    events: list[dict[str, object]] = []
    for line in lines:
        event = _json_object(line)
        if event is not None:
            events.append(event)
    return events


def _epoch(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _moment(value: object) -> datetime | None:
    epoch = _epoch(value)
    return None if epoch is None else datetime.fromtimestamp(epoch, UTC)


def _duration(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return max(0, int(value))


def _phase_instances(
    runs: list[list[dict[str, object]]],
) -> list[PhaseInstance]:
    instances: dict[str, PhaseInstance] = {}
    written_at = 0
    for events in runs:
        for event in events:
            written_at += 1
            instance_id = _text(event.get("phase_instance_id"))
            if not instance_id:
                continue
            event_type = _text(event.get("event_type"))
            existing = instances.get(instance_id)
            started = _moment(event.get("phase_started_at"))
            if started is None and event_type == "phase_started":
                started = _moment(event.get("timestamp_epoch"))
            if existing is None:
                if event_type != "phase_started" or started is None:
                    continue
                instance = PhaseInstance(
                    instance_id=instance_id,
                    phase=_text(event.get("phase_id")),
                    title=_text(event.get("phase_title")),
                    start=started,
                    finish=PhaseInstanceWithoutFinish(),
                )
            else:
                instance = PhaseInstance(
                    instance_id=instance_id,
                    phase=existing.phase or _text(event.get("phase_id")),
                    title=existing.title or _text(event.get("phase_title")),
                    start=existing.start,
                    finish=existing.finish,
                )
            if event_type == "phase_finished":
                finished = _moment(event.get("timestamp_epoch"))
                seconds = _duration(event.get("phase_elapsed_seconds"))
                if finished is not None and seconds is not None:
                    instance = instance._replace(
                        finish=InstanceFinished(
                            at=finished,
                            status=_text(event.get("status")),
                            seconds=seconds,
                            written_at=written_at,
                        )
                    )
            instances[instance_id] = instance
    return list(instances.values())


def _newest_open_instance(
    runs: list[list[dict[str, object]]],
) -> OpenPhaseInstance | NoOpenPhase:
    if not runs:
        return NoOpenPhase()
    events = runs[-1]
    if any(_text(event.get("event_type")) == "run_finished" for event in events):
        return NoOpenPhase()
    started: list[PhaseInstance] = []
    closed: set[str] = set()
    for event in events:
        event_type = _text(event.get("event_type"))
        instance_id = _text(event.get("phase_instance_id"))
        if not instance_id:
            continue
        if event_type == "phase_finished":
            closed.add(instance_id)
            continue
        if event_type != "phase_started":
            continue
        start = _moment(event.get("phase_started_at")) or _moment(
            event.get("timestamp_epoch")
        )
        if start is None:
            continue
        started.append(
            PhaseInstance(
                instance_id=instance_id,
                phase=_text(event.get("phase_id")),
                title=_text(event.get("phase_title")),
                start=start,
                finish=PhaseInstanceWithoutFinish(),
            )
        )
    for instance in reversed(started):
        if instance.instance_id not in closed:
            return _open_instance(runs, instance)
    return NoOpenPhase()


def _open_instance(
    runs: list[list[dict[str, object]]], instance: PhaseInstance
) -> OpenPhaseInstance:
    """Read progress and promises for one unfinished phase instance."""
    progress_event = _last_progress(runs, instance.instance_id)
    stated_event = _latest_stated_eta(runs, instance)
    projection = _reported_eta(progress_event, instance.start)
    if isinstance(stated_event, LatestStatedEtaEvent):
        first_stated: FirstStatedEtaTarget | EtaNeverStated = (
            stated_event.first
        )
        eta: StatedEta | ProjectedEta | EtaUnavailable
        eta = stated_event.eta
        now = datetime.fromtimestamp(plan_history.now_epoch(), UTC)
        if eta.time < now:
            delay = now - eta.time
            eta_range = eta.range
            if isinstance(eta_range, EtaRange):
                eta_range = EtaRange(
                    earliest=eta_range.earliest + delay,
                    latest=eta_range.latest + delay,
                )
            eta = eta._replace(time=now, range=eta_range)
    else:
        first_stated = EtaNeverStated()
        eta = projection
    return OpenPhaseInstance(
        phase=instance,
        progress=_reported_progress(progress_event),
        eta=eta,
        first_stated=first_stated,
    )


def _open_phase_ids(runs: list[list[dict[str, object]]]) -> set[str]:
    if not runs or any(
        _text(event.get("event_type")) == "run_finished" for event in runs[-1]
    ):
        return set()
    opened: dict[str, str] = {}
    for event in runs[-1]:
        event_type = _text(event.get("event_type"))
        instance_id = _text(event.get("phase_instance_id"))
        if event_type == "phase_started" and instance_id:
            opened[instance_id] = _text(event.get("phase_id"))
        elif event_type == "phase_finished":
            _ = opened.pop(instance_id, None)
    return set(opened.values())


def _parallel_check(
    phases: list[plan_history.PlanPhase], open_ids: set[str]
) -> ParallelCheck:
    states = {
        phase["id"]: "done"
        if phase["done"]
        else "running"
        if phase["id"] in open_ids
        else "todo"
        for phase in phases
    }
    waiting = [phase for phase in phases if states[phase["id"]] == "todo"]
    unknown = tuple(phase["id"] for phase in waiting if phase["depends_on"] is None)
    if unknown:
        return DependenciesUnknown(unknown)
    ready: list[str] = []
    first_wait = ""
    for phase in waiting:
        unmet = [
            f"Phase {needed} ({states.get(needed, 'not in the plan')})"
            for needed in phase["depends_on"] or []
            if states.get(needed) != "done"
        ]
        if phase["blocked_by"]:
            unmet.append(phase["blocked_by"])
        if not unmet:
            ready.append(phase["id"])
        elif not first_wait:
            first_wait = f"Phase {phase['id']} needs {', '.join(unmet)}"
    if ready:
        return CouldStart(tuple(ready))
    return NothingToStart(first_wait or "no phase is waiting")


def parallel_line(check: ParallelCheck) -> str:
    """The checkpoint notice's `Parallel:` line."""
    if isinstance(check, CouldStart):
        return f"Parallel: {', '.join(f'Phase {phase}' for phase in check.phases)} could start"
    if isinstance(check, NothingToStart):
        return f"Parallel: none — {check.reason}"
    named = ", ".join(check.phases)
    which = f"Phase {named} has" if len(check.phases) == 1 else f"Phases {named} have"
    return (
        f"Parallel: unknown — {which} no **Depends on:** line; "
        + "add one under each phase heading and rerun"
    )


def _normalized_title(title: str) -> str:
    return " ".join(title.replace("`", "").casefold().split())


def _instances_by_phase(
    phases: list[plan_history.PlanPhase],
    instances: list[PhaseInstance],
) -> list[list[PhaseInstance]]:
    by_title: dict[str, list[int]] = {}
    by_id = {phase["id"]: index for index, phase in enumerate(phases)}
    for index, phase in enumerate(phases):
        by_title.setdefault(_normalized_title(phase["title"]), []).append(index)
    assigned: list[list[PhaseInstance]] = [[] for _ in phases]
    for instance in instances:
        title_matches = by_title.get(_normalized_title(instance.title), [])
        if len(title_matches) == 1:
            index = title_matches[0]
        else:
            index = by_id.get(instance.phase, -1)
        if index >= 0:
            assigned[index].append(instance)
    return assigned


def _gap_samples(runs: list[list[dict[str, object]]]) -> list[int]:
    samples: list[int] = []
    for events in runs:
        pending_finishes: list[float] = []
        for event in events:
            at = _epoch(event.get("timestamp_epoch"))
            # A phase recorded after the fact is written out of time order, so
            # the gap before it measures nothing.
            if at is None or event.get("backfilled") is True:
                continue
            event_type = _text(event.get("event_type"))
            if (
                event_type == "phase_finished"
                and _text(event.get("status")) == "completed"
            ):
                pending_finishes.append(at)
            elif event_type == "phase_started" and pending_finishes:
                samples.extend(max(0, int(at - finish)) for finish in pending_finishes)
                pending_finishes.clear()
    return samples


def _last_progress(
    runs: list[list[dict[str, object]]], instance_id: str
) -> LastPhaseProgressEvent | PhaseProgressEventNotRecorded:
    last_event: dict[str, object] | None = None
    for events in runs:
        for event in events:
            if (
                _text(event.get("event_type")) == "progress_reported"
                and _text(event.get("phase_instance_id")) == instance_id
            ):
                last_event = event
    if last_event is None:
        return PhaseProgressEventNotRecorded()
    percent_value = last_event.get("phase_percent")
    if isinstance(percent_value, bool) or not isinstance(percent_value, int):
        return PhaseProgressEventNotRecorded()
    reported_at = _moment(last_event.get("timestamp_epoch"))
    elapsed = _duration(last_event.get("phase_elapsed_seconds"))
    calibration = _object_dict(last_event.get("phase_calibration"))
    if calibration is None:
        calibration = _object_dict(last_event.get("calibration"))
    return LastPhaseProgressEvent(
        percent=percent_value,
        reported_at=(
            reported_at
            if reported_at is not None
            else ProgressReportTimeNotRecorded()
        ),
        elapsed=(
            elapsed if elapsed is not None else ProgressElapsedTimeNotRecorded()
        ),
        spread=plan_history.percent_spread(calibration),
    )


def _reported_progress(
    report: LastPhaseProgressEvent | PhaseProgressEventNotRecorded,
) -> ReportedPhaseProgress | PhaseProgressNotReported:
    if isinstance(report, PhaseProgressEventNotRecorded):
        return PhaseProgressNotReported()
    return ReportedPhaseProgress(percent=report.percent)


def _reported_eta(
    report: LastPhaseProgressEvent | PhaseProgressEventNotRecorded,
    started: datetime,
) -> ProjectedEta | EtaUnavailable:
    if isinstance(report, PhaseProgressEventNotRecorded) or isinstance(
        report.reported_at, ProgressReportTimeNotRecorded
    ):
        return EtaUnavailable()
    as_of = report.reported_at
    elapsed = report.elapsed
    if isinstance(elapsed, ProgressElapsedTimeNotRecorded):
        elapsed_seconds = max(0, int((as_of - started).total_seconds()))
    else:
        elapsed_seconds = elapsed
    band = plan_history.eta_band_seconds(
        report.percent,
        elapsed_seconds,
        report.spread,
    )
    if isinstance(band, plan_history.EtaProjectionUnavailable):
        return EtaUnavailable()
    return ProjectedEta(
        time=as_of + timedelta(seconds=band.remaining),
        earliest=as_of + timedelta(seconds=band.earliest),
        latest=as_of + timedelta(seconds=band.latest),
        as_of=as_of,
    )


def _repair_rounds_started_after(
    runs: list[list[dict[str, object]]],
    instance_id: str,
    stated_at: datetime,
) -> int:
    starts: dict[int, datetime] = {}
    for events in runs:
        for event in events:
            fix_pass = event.get("fix_pass")
            if (
                _text(event.get("event_type")) != "pass_started"
                or _text(event.get("phase_instance_id")) != instance_id
                or isinstance(fix_pass, bool)
                or not isinstance(fix_pass, int)
                or fix_pass < 1
            ):
                continue
            started = _moment(event.get("timestamp_epoch"))
            if started is not None and (fix_pass not in starts or started < starts[fix_pass]):
                starts[fix_pass] = started
    return sum(started > stated_at for started in starts.values())


def _instance_eta_events(
    runs: list[list[dict[str, object]]], instance: PhaseInstance
) -> list[dict[str, object]]:
    """Keep pre-start promises, but exclude other runs and earlier attempts."""
    for events in runs:
        start = next(
            (
                index
                for index, event in enumerate(events)
                if _text(event.get("event_type")) == "phase_started"
                and _text(event.get("phase_instance_id")) == instance.instance_id
            ),
            -1,
        )
        last = len(events)
        if start < 0:
            # Run records also retain completions whose start event is absent.
            start = next(
                (index for index, event in enumerate(events)
                 if _text(event.get("event_type")) == "phase_finished"
                 and _text(event.get("phase_instance_id")) == instance.instance_id),
                -1,
            )
            if start < 0:
                continue
            last = start + 1
        first = 0
        for index, event in enumerate(events[:start]):
            if (
                _text(event.get("phase_id")) == instance.phase
                and _text(event.get("event_type")) in ("phase_started", "phase_finished")
            ):
                first = index + 1
        for index in range(start + 1, last):
            event = events[index]
            if (
                _text(event.get("event_type")) == "phase_started"
                and _text(event.get("phase_id")) == instance.phase
            ):
                last = index
                break
        return [
            event
            for event in events[first:last]
            if _text(event.get("event_type")) == "eta_stated"
            and _text(event.get("eta_phase_id", event.get("phase_id"))) == instance.phase
        ]
    return []


def _latest_stated_eta(
    runs: list[list[dict[str, object]]], instance: PhaseInstance
) -> LatestStatedEtaEvent | NoStatedEtaEvent:
    first: FirstStatedEtaTarget | EtaNeverStated = EtaNeverStated()
    latest: StatedEta | NoStatedEtaEvent = NoStatedEtaEvent()
    for event in _instance_eta_events(runs, instance):
        time = _moment(event.get("eta_at"))
        stated_at = _moment(event.get("timestamp_epoch"))
        if time is None or stated_at is None:
            continue
        earliest = _moment(event.get("eta_earliest_at"))
        latest_range = _moment(event.get("eta_latest_at"))
        eta_range: EtaRange | NoEtaRange
        if earliest is not None and latest_range is not None:
            eta_range = EtaRange(earliest=earliest, latest=latest_range)
        else:
            eta_range = NoEtaRange()
        stated = StatedEta(
            time=time,
            range=eta_range,
            stated_at=stated_at,
            basis=_text(event.get("basis")),
        )
        if isinstance(first, EtaNeverStated):
            first = FirstStatedEtaTarget(
                time=stated.time,
                repair_rounds=_repair_rounds_started_after(
                    runs, instance.instance_id, stated.stated_at
                ),
            )
        latest = stated
    if isinstance(latest, NoStatedEtaEvent) or isinstance(first, EtaNeverStated):
        return NoStatedEtaEvent()
    return LatestStatedEtaEvent(eta=latest, first=first)


def _done_phase(
    phase: plan_history.PlanPhase,
    instances: list[PhaseInstance],
) -> DonePhase:
    completed: list[tuple[PhaseInstance, InstanceFinished]] = []
    for instance in instances:
        finish = instance.finish
        if isinstance(finish, InstanceFinished) and finish.status == "completed":
            completed.append((instance, finish))
    if not completed:
        return DonePhase(
            phase=phase["id"],
            title=phase["title"],
            times=PhaseTimingNotRecorded(),
        )
    last_finish = max(completed, key=lambda pair: pair[1].written_at)[1]
    seconds = sum(
        instance.finish.seconds
        for instance in instances
        if isinstance(instance.finish, InstanceFinished)
    )
    return DonePhase(
        phase=phase["id"],
        title=phase["title"],
        times=CompletedPhaseTiming(
            start=min(instance.start for instance in instances),
            finish=last_finish.at,
            seconds=seconds,
        ),
    )


def _plan_finish(
    phases: list[DonePhase | OpenPhase | TodoPhase],
) -> FinishedAt | PredictedFinish | UnknownFinish | GateBlockedFinish:
    if not phases:
        return UnknownFinish()
    for phase in phases:
        if isinstance(phase, TodoPhase) and isinstance(phase.gate, UntimedGate):
            return GateBlockedFinish(phase.gate.gate, phase.gate.waits_on)
    if all(isinstance(phase, DonePhase) for phase in phases):
        finishes = [
            phase.times.finish
            for phase in phases
            if isinstance(phase, DonePhase)
            and isinstance(phase.times, CompletedPhaseTiming)
        ]
        if finishes:
            return FinishedAt(max(finishes))
        return UnknownFinish()
    projections = [
        phase.eta.time
        for phase in phases
        if isinstance(phase, OpenPhase)
        and isinstance(phase.eta, StatedEta | ProjectedEta)
    ]
    projections.extend(
        phase.times.finish
        for phase in phases
        if isinstance(phase, TodoPhase)
        and isinstance(phase.times, PredictedPhaseTiming)
    )
    if projections:
        return PredictedFinish(max(projections))
    return UnknownFinish()


def _phase_history(
    runs: list[list[dict[str, object]]],
    instances: list[PhaseInstance],
    row: DonePhase | OpenPhase | TodoPhase,
) -> PhaseHistory:
    instance_ids = {instance.instance_id for instance in instances}
    eta_events = {
        id(event)
        for instance in instances
        for event in _instance_eta_events(runs, instance)
    }
    trail: list[tuple[datetime, datetime]] = []
    fix_passes: set[str] = set()
    finished_fixes: set[str] = set()
    findings: dict[str, int] = dict.fromkeys(FINDING_SEVERITIES, 0)
    for events in runs:
        for index, event in enumerate(events):
            if id(event) in eta_events:
                target = _moment(event.get("eta_at"))
                stated = _moment(event.get("timestamp_epoch"))
                if target is not None and stated is not None:
                    trail.append((target, stated))
            if _text(event.get("phase_instance_id")) not in instance_ids:
                continue
            event_type = _text(event.get("event_type"))
            if event_type == "pass_started" and event.get("pass_kind") == "fix":
                fix_passes.add(_text(event.get("pass_instance_id")) or f"{id(events)}:{index}")
            if event_type == "pass_finished":
                finished_fixes.add(_text(event.get("pass_instance_id")))
            if event_type == "finding_opened":
                severity = _text(event.get("severity"))
                if severity in findings:
                    findings[severity] += 1
    finished: datetime | PhaseNotFinished = PhaseNotFinished()
    delta: float | FinishDeltaUnavailable = FinishDeltaUnavailable()
    if isinstance(row, DonePhase) and isinstance(row.times, CompletedPhaseTiming):
        finished = row.times.finish
        if trail:
            delta = (finished - trail[-1][0]).total_seconds() / 60
    return PhaseHistory(
        first_eta=trail[0][0] if trail else EtaNeverStated(),
        moves=sum(previous[0] != current[0] for previous, current in zip(trail, trail[1:])),
        eta_trail=trail,
        finished_at=finished,
        delta_minutes=delta,
        fix_done=len(fix_passes & finished_fixes),
        fix_open_passes=len(fix_passes - finished_fixes),
        findings=findings,
    )


def _with_history(
    row: DonePhase | OpenPhase | TodoPhase,
    runs: list[list[dict[str, object]]],
    instances: list[PhaseInstance],
) -> DonePhase | OpenPhase | TodoPhase:
    history = _phase_history(runs, instances, row)
    return replace(
        row,
        first_eta=history.first_eta,
        moves=history.moves,
        eta_trail=history.eta_trail,
        finished_at=history.finished_at,
        delta_minutes=history.delta_minutes,
        fix_done=history.fix_done,
        fix_open_passes=history.fix_open_passes,
        findings=history.findings,
    )


def _production_gates(plan_path: Path) -> dict[str, NoGate | TimedGate | UntimedGate]:
    reference = _production_reference(plan_path)
    if isinstance(reference, PlanOutsideProduction):
        return {}
    try:
        document = _production_document(plan_path, reference)
        lines = document.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError, Refusal):
        return {}
    gates: dict[str, NoGate | TimedGate | UntimedGate] = {}
    in_gates = False
    for line in lines:
        if line.startswith("## "):
            in_gates = line.strip() == "## Gates"
            continue
        if not in_gates or not line.lstrip().startswith("|"):
            continue
        cells = [
            cell.strip().replace(r"\|", "|")
            for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))
        ]
        if len(cells) != 4 or re.fullmatch(r"G\d+", cell_value(cells[0])) is None:
            continue
        gate, _, waits_on, clears = cells
        gate = cell_value(gate)
        if clears.casefold().startswith("cleared"):
            gates[gate] = NoGate()
            continue
        match = re.search(r"\b\d{4}-\d{2}-\d{2} \d{2}:\d{2}\b", clears)
        if match is not None:
            try:
                until = datetime.strptime(match.group(), "%Y-%m-%d %H:%M").replace(
                    tzinfo=user_zone()
                )
            except ValueError:
                pass
            else:
                gates[gate] = TimedGate(gate, until, waits_on)
                continue
        gates[gate] = UntimedGate(gate, waits_on)
    return gates


def build_plan(plan_path: Path) -> PhaseRecord:
    """Build one plan's phase view entirely from its durable events."""
    plan_path = plan_path.expanduser().resolve()
    try:
        phases = plan_history.plan_phases(plan_path)
    except (OSError, UnicodeError) as error:
        raise NoPlan(f"Unable to read plan {plan_path}") from error

    run_events = [_events(path) for path in plan_history.plan_runs(plan_path)]
    instances = _phase_instances(run_events)
    open_instance = _newest_open_instance(run_events)
    return _build_phase_record(
        plan_path, phases, run_events, _instances_by_phase(phases, instances),
        [open_instance] if isinstance(open_instance, OpenPhaseInstance) else [],
    )


def build_run(plan_path: Path, events: list[dict[str, object]]) -> PhaseRecord:
    """Build a phase record from one run after its plan headings are gone.

    Each recorded instance keeps its own row, including unfinished phases in
    a closed run. Event titles and completion events replace plan headings.
    """
    run_events = [[event for event in events if event.get("event_type") != "run_finished"]]
    instances = _phase_instances(run_events)
    by_instance = {instance.instance_id: instance for instance in instances}
    started_ids: set[str] = set()
    finished_ids: set[str] = set()
    finished: list[PhaseInstance] = []
    for index, event in enumerate(run_events[0]):
        instance_id = _text(event.get("phase_instance_id"))
        event_type = _text(event.get("event_type"))
        if event_type == "phase_started":
            started_ids.add(instance_id)
        if event_type != "phase_finished":
            continue
        finished_ids.add(instance_id)
        at = _moment(event.get("timestamp_epoch"))
        if at is None:
            continue
        seconds = _duration(event.get("phase_elapsed_seconds")) or 0
        instance = by_instance.get(instance_id) if instance_id in started_ids else None
        finished.append(PhaseInstance(
            instance_id=instance_id,
            phase=_text(event.get("phase_id")) or (instance.phase if instance else ""),
            title=_text(event.get("phase_title")) or (instance.title if instance else ""),
            start=instance.start if instance else at - timedelta(seconds=seconds),
            finish=InstanceFinished(at=at, status="completed", seconds=seconds, written_at=index),
        ))
    opened = [instance for instance in instances if instance.instance_id not in finished_ids]
    ordered = finished + opened
    phases: list[plan_history.PlanPhase] = [
        {"id": instance.phase, "title": instance.title,
         "done": isinstance(instance.finish, InstanceFinished),
         "depends_on": None, "blocked_by": ""}
        for instance in ordered
    ]
    return _build_phase_record(
        plan_path.expanduser().resolve(), phases, run_events,
        [[instance] for instance in ordered],
        [_open_instance(run_events, instance) for instance in opened],
    )


def _build_phase_record(
    plan_path: Path,
    phases: list[plan_history.PlanPhase],
    run_events: list[list[dict[str, object]]],
    assigned: list[list[PhaseInstance]],
    open_instances: list[OpenPhaseInstance],
) -> PhaseRecord:
    """Apply the same timing and history rules to plan and run phase rows."""
    open_instance = open_instances[-1] if open_instances else NoOpenPhase()
    open_ids = _open_phase_ids(run_events)
    in_production = any(phase["blocked_by"] and not phase["done"] for phase in phases) and isinstance(
        _production_reference(plan_path), ProductionReference
    )
    gates = _production_gates(plan_path) if in_production else {}
    current: OpenPhase | NoOpenPhase = NoOpenPhase()
    current_position: PlanPhasePosition | OutsidePlan = OutsidePlan()
    if isinstance(open_instance, OpenPhaseInstance):
        for index, phase_instances in enumerate(assigned):
            if any(
                instance.instance_id == open_instance.phase.instance_id
                for instance in phase_instances
            ):
                current_position = PlanPhasePosition(offset=index)
                break
    rows: list[DonePhase | OpenPhase | TodoPhase] = []
    row_instances: list[list[PhaseInstance]] = []
    for index, phase in enumerate(phases):
        title = _normalized_title(phase["title"])
        if (
            isinstance(current_position, PlanPhasePosition)
            and index == current_position.offset
            and isinstance(open_instance, OpenPhaseInstance)
        ):
            title = _normalized_title(open_instance.phase.title)
        elif phase["done"]:
            completed = [
                (instance, instance.finish) for instance in assigned[index]
                if isinstance(instance.finish, InstanceFinished)
                and instance.finish.status == "completed"
            ]
            if completed:
                last = max(completed, key=lambda pair: pair[1].written_at)[0]
                title = _normalized_title(last.title)
        related = [
            instance for instance in assigned[index]
            if _normalized_title(instance.title) == title
        ]
        row_instances.append(related)
        row_open = next(
            (candidate for candidate in open_instances if any(
                instance.instance_id == candidate.phase.instance_id for instance in assigned[index]
            )),
            NoOpenPhase(),
        )
        if isinstance(row_open, OpenPhaseInstance):
            earliest = min(
                (instance.start for instance in related),
                default=row_open.phase.start,
            )
            current = OpenPhase(
                phase=phase["id"],
                title=phase["title"],
                started=earliest,
                progress=row_open.progress,
                eta=row_open.eta,
                first_stated=row_open.first_stated,
                instance_id=row_open.phase.instance_id,
            )
            rows.append(current)
            open_ids.add(phase["id"])
        elif phase["done"]:
            rows.append(_done_phase(phase, related))
        else:
            rows.append(
                TodoPhase(
                    phase=phase["id"],
                    title=phase["title"],
                    times=PhaseTimingNotPredicted(),
                    gate=(
                        gates.get(
                            phase["blocked_by"],
                            UntimedGate(phase["blocked_by"], "gate not found"),
                        )
                        if in_production and phase["blocked_by"]
                        else NoGate()
                    ),
                )
            )

    if isinstance(open_instance, OpenPhaseInstance) and isinstance(
        current_position, OutsidePlan
    ):
        current = OpenPhase(
            phase=open_instance.phase.phase,
            title=open_instance.phase.title or "Untitled phase",
            started=open_instance.phase.start,
            progress=open_instance.progress,
            eta=open_instance.eta,
            first_stated=open_instance.first_stated,
            instance_id=open_instance.phase.instance_id,
        )

    durations = [
        row.times.seconds
        for row in rows
        if isinstance(row, DonePhase)
        and isinstance(row.times, CompletedPhaseTiming)
    ]
    typical: TypicalDuration | UnknownDuration
    if durations:
        typical = TypicalDuration(int(statistics.median(durations)))
    elif isinstance(current, OpenPhase) and isinstance(
        current.eta, StatedEta | ProjectedEta
    ):
        typical = TypicalDuration(
            max(0, int((current.eta.time - current.started).total_seconds()))
        )
    else:
        typical = UnknownDuration()
    gap_samples = _gap_samples(run_events)
    gap = int(statistics.median(gap_samples)) if gap_samples else 0
    now = datetime.fromtimestamp(plan_history.now_epoch(), UTC)
    gap_before_first = isinstance(current, OpenPhase)
    previous_finish = now
    if isinstance(current, OpenPhase):
        if isinstance(current.eta, StatedEta | ProjectedEta):
            previous_finish = current.eta.time
        elif isinstance(typical, TypicalDuration):
            previous_finish = max(
                now,
                current.started + timedelta(seconds=typical.seconds),
            )
    if isinstance(current, OpenPhase) and (
        isinstance(current.eta, StatedEta | ProjectedEta)
        or isinstance(typical, TypicalDuration)
    ):
        scheduled = replace(current, schedule_end=previous_finish)
        rows = [scheduled if row is current else row for row in rows]
        current = scheduled
    blocked: UntimedGate | NoGate = NoGate()
    for index, row in enumerate(rows):
        if not isinstance(row, TodoPhase):
            continue
        if isinstance(blocked, NoGate) and isinstance(row.gate, UntimedGate):
            blocked = row.gate
        if isinstance(blocked, UntimedGate):
            rows[index] = replace(row, times=PhaseTimingNotPredicted(), gate=blocked)
            continue
        if isinstance(typical, UnknownDuration):
            continue
        start = previous_finish
        if gap_before_first:
            start += timedelta(seconds=gap)
        if isinstance(row.gate, TimedGate):
            start = max(start, row.gate.until)
        finish = start + timedelta(seconds=typical.seconds)
        rows[index] = replace(row, times=PredictedPhaseTiming(start=start, finish=finish))
        previous_finish = finish
        gap_before_first = True

    for index, row in enumerate(rows):
        enriched = _with_history(row, run_events, row_instances[index])
        rows[index] = enriched
        if row is current and isinstance(enriched, OpenPhase):
            current = enriched
    if (
        isinstance(current, OpenPhase)
        and isinstance(current_position, OutsidePlan)
        and isinstance(open_instance, OpenPhaseInstance)
    ):
        enriched = _with_history(current, run_events, [open_instance.phase])
        if isinstance(enriched, OpenPhase):
            current = enriched

    return PhaseRecord(
        plan=plan_path,
        updated=datetime.fromtimestamp(
            plan_history.now_epoch(), UTC
        ),
        current=current,
        phases=rows,
        plan_finish=_plan_finish(rows),
        parallel=_parallel_check(phases, open_ids),
    )


def build(session_dir: Path) -> PhaseRecord:
    """Build a session's phase view without changing its state or history."""
    state = _state(session_dir)
    working_dir_text = _text(state.get("working_dir"))
    plan_doc = _text(state.get("project_plan_doc")) or _text(state.get("plan_doc"))
    if not plan_doc:
        raise NoState(f"Progress state does not name a plan in {session_dir}")
    working_dir = Path(working_dir_text) if working_dir_text else Path.cwd()
    return build_plan(plan_history.resolve_plan_path(working_dir, plan_doc))


def _clock(moment: datetime, zone: ZoneInfo) -> str:
    return moment.astimezone(zone).strftime("%m-%d %H:%M")


def _time(moment: datetime, zone: ZoneInfo) -> str:
    return moment.astimezone(zone).strftime("%H:%M")


def _cell_text(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def _elapsed(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    return f"{hours}:{remainder // 60:02d}"


def _row_status(row: DonePhase | OpenPhase | TodoPhase, zone: ZoneInfo) -> str:
    if isinstance(row, DonePhase):
        if isinstance(row.times, CompletedPhaseTiming):
            return f"done in {_elapsed(row.times.seconds)}"
        return "done"
    if isinstance(row, OpenPhase):
        if isinstance(row.progress, ReportedPhaseProgress):
            return f"running, {row.progress.percent}%"
        return "running"
    if isinstance(row.gate, TimedGate):
        return f"gated until {row.gate.until.astimezone(zone):%a %d %b %H:%M}"
    if isinstance(row.gate, UntimedGate):
        return f"gated by {row.gate.gate}: {_cell_text(row.gate.waits_on)}"
    return (
        "predicted"
        if isinstance(row.times, PredictedPhaseTiming)
        else "todo"
    )


def _row_clocks(
    row: DonePhase | OpenPhase | TodoPhase, zone: ZoneInfo
) -> tuple[str, str]:
    if isinstance(row, DonePhase):
        if isinstance(row.times, CompletedPhaseTiming):
            return _clock(row.times.start, zone), _clock(row.times.finish, zone)
        return "—", "—"
    if isinstance(row, OpenPhase):
        finish = "—"
        if isinstance(row.eta, StatedEta | ProjectedEta):
            finish = _clock(row.eta.time, zone)
        return _clock(row.started, zone), finish
    if isinstance(row.times, PredictedPhaseTiming):
        return _clock(row.times.start, zone), _clock(row.times.finish, zone)
    return "—", "—"


def _heading_text(record: PhaseRecord) -> str:
    current = record.current
    if isinstance(current, NoOpenPhase):
        done = sum(isinstance(row, DonePhase) for row in record.phases)
        return f"No phase running — {done} of {len(record.phases)} done"
    if not any(row is current for row in record.phases):
        # A phase from another plan has no place in this plan's count.
        return f"Phase {current.phase} — {current.title} · not in this plan"
    return f"Phase {current.phase} of {len(record.phases)} — {current.title}"


def render(record: PhaseRecord, zone: ZoneInfo) -> str:
    """Render a phase record as the compact Markdown report."""
    current = record.current
    heading = f"**{_heading_text(record)}**"

    started = "—"
    percent = "—"
    if isinstance(current, OpenPhase):
        started = _clock(current.started, zone)
        if isinstance(current.progress, ReportedPhaseProgress):
            percent = f"{current.progress.percent}%"
    summary = [
        "| | |",
        "| --- | --- |",
        f"| Started | {started} |",
        f"| Done | {percent} |",
    ]
    if isinstance(current, OpenPhase):
        eta_cell = "—"
        eta_from = "—"
        if isinstance(current.eta, ProjectedEta):
            eta = current.eta
            eta_cell = (
                f"{_clock(eta.time, zone)} "
                + f"({_time(eta.earliest, zone)} to {_time(eta.latest, zone)})"
            )
            if isinstance(current.progress, ReportedPhaseProgress):
                eta_from = f"projected from {current.progress.percent}% done"
        elif isinstance(current.eta, StatedEta):
            eta = current.eta
            eta_cell = _clock(eta.time, zone)
            if isinstance(eta.range, EtaRange):
                eta_cell += (
                    f" ({_time(eta.range.earliest, zone)} "
                    + f"to {_time(eta.range.latest, zone)})"
                )
            eta_from = (
                f"stated {_time(eta.stated_at, zone)}: {_cell_text(eta.basis)}"
            )
        summary.append(f"| ETA | {eta_cell} |")
        summary.append(f"| ETA from | {eta_from} |")
    plan_finish = "—"
    if isinstance(record.plan_finish, FinishedAt):
        plan_finish = _clock(record.plan_finish.at, zone)
    elif isinstance(record.plan_finish, PredictedFinish):
        plan_finish = f"{_clock(record.plan_finish.at, zone)}, predicted"
    elif isinstance(record.plan_finish, GateBlockedFinish):
        plan_finish = f"after {record.plan_finish.gate}"
    summary.extend(
        [
            f"| Plan finish | {plan_finish} |",
            f"| Updated | {_clock(record.updated, zone)} "
            + f"{record.updated.astimezone(zone):%Z} |",
        ]
    )

    phase_table = [
        "| Phase | What it delivers | Status | Start | Finish |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in reversed(record.phases):
        start, finish = _row_clocks(row, zone)
        phase_table.append(
            f"| {row.phase} | {row.title} | {_row_status(row, zone)} | "
            + f"{start} | {finish} |"
        )
    return "\n".join([heading, "", *summary, "", *phase_table])


def _iso(moment: datetime, zone: ZoneInfo) -> str:
    return moment.astimezone(zone).isoformat(timespec="seconds")


def _json_phase(
    row: DonePhase | OpenPhase | TodoPhase, zone: ZoneInfo
) -> dict[str, object]:
    start: str | None = None
    finish: str | None = None
    seconds: int | None = None
    if isinstance(row, DonePhase):
        status = "done"
        if isinstance(row.times, CompletedPhaseTiming):
            start = _iso(row.times.start, zone)
            finish = _iso(row.times.finish, zone)
            seconds = row.times.seconds
    elif isinstance(row, OpenPhase):
        status = "running"
        start = _iso(row.started, zone)
        if isinstance(row.eta, StatedEta | ProjectedEta):
            finish = _iso(row.eta.time, zone)
    else:
        status = "todo"
        if isinstance(row.times, PredictedPhaseTiming):
            start = _iso(row.times.start, zone)
            finish = _iso(row.times.finish, zone)
    return {
        "phase": row.phase,
        "title": row.title,
        "status": status,
        "start": start,
        "finish": finish,
        "seconds": seconds,
        "first_eta": _iso(row.first_eta, zone) if isinstance(row.first_eta, datetime) else None,
        "moves": row.moves,
        "eta_trail": [[_iso(target, zone), _iso(stated, zone)] for target, stated in row.eta_trail],
        "finished_at": _iso(row.finished_at, zone) if isinstance(row.finished_at, datetime) else None,
        "delta_minutes": row.delta_minutes if isinstance(row.delta_minutes, float) else None,
        "fix_rounds": row.fix_rounds,
        "findings": row.findings,
    }


def _json_record(record: PhaseRecord, zone: ZoneInfo) -> dict[str, object]:
    current_json: dict[str, object] | None = None
    if isinstance(record.current, OpenPhase):
        current = record.current
        eta_json: dict[str, object] | None = None
        percent: int | None = None
        if isinstance(current.progress, ReportedPhaseProgress):
            percent = current.progress.percent
        first_stated_eta_target: str | None = None
        repair_rounds_since_first_stated_eta: int | None = None
        if isinstance(current.first_stated, FirstStatedEtaTarget):
            first_stated_eta_target = _iso(current.first_stated.time, zone)
            repair_rounds_since_first_stated_eta = current.first_stated.repair_rounds
        if isinstance(current.eta, ProjectedEta):
            eta = current.eta
            eta_json = {
                "time": _iso(eta.time, zone),
                "earliest": _iso(eta.earliest, zone),
                "latest": _iso(eta.latest, zone),
                "source": "projected",
                "stated_at": None,
                "basis": None,
                "as_of": _iso(eta.as_of, zone),
            }
        elif isinstance(current.eta, StatedEta):
            eta = current.eta
            earliest: str | None = None
            latest: str | None = None
            if isinstance(eta.range, EtaRange):
                earliest = _iso(eta.range.earliest, zone)
                latest = _iso(eta.range.latest, zone)
            eta_json = {
                "time": _iso(eta.time, zone),
                "earliest": earliest,
                "latest": latest,
                "source": "stated",
                "stated_at": _iso(eta.stated_at, zone),
                "basis": eta.basis,
                "as_of": None,
            }
        current_json = {
            "phase": current.phase,
            "of": len(record.phases),
            "title": current.title,
            "started": _iso(current.started, zone),
            "percent": percent,
            "eta": eta_json,
            "first_stated_eta_target": first_stated_eta_target,
            "repair_rounds_since_first_stated_eta": repair_rounds_since_first_stated_eta,
        }
    plan_finish: str | None = None
    if isinstance(record.plan_finish, FinishedAt | PredictedFinish):
        plan_finish = _iso(record.plan_finish.at, zone)
    return {
        "plan": str(record.plan),
        "updated": _iso(record.updated, zone),
        "plan_finish": plan_finish,
        "current": current_json,
        "phases": [_json_phase(row, zone) for row in record.phases],
        "parallel": {
            "could_start": list(record.parallel.phases)
            if isinstance(record.parallel, CouldStart)
            else [],
            "line": parallel_line(record.parallel),
        },
    }


def show(session_dir: Path, zone: ZoneInfo, json_output: bool = False) -> str:
    """Return one session's Markdown or JSON phase table."""
    record = build(session_dir)
    if json_output:
        return json.dumps(_json_record(record, zone), indent=2)
    return render(record, zone)


def _production_lines(production: Production) -> list[str]:
    try:
        return production.doc.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise Refusal(f"cannot read production doc: {error}") from error


def _production_unit_plans(production: Production) -> list[ProductionUnitPlan]:
    plans: list[ProductionUnitPlan] = []
    for cells in live_unit_table(_production_lines(production), production.slug):
        unit = cell_value(cells.get("Unit", ""))
        worktree = Path(cell_value(cells.get("Worktree", ""))).expanduser()
        plan_text = cell_value(cells.get("Plan", "")).split(" ", 1)[0]
        plan = Path(plan_text).expanduser()
        if not plan.is_absolute():
            plan = worktree / plan
        plans.append(ProductionUnitPlan(unit=unit, plan=plan.resolve()))
    return plans


def show_production(production_doc: Path) -> str:
    """Return every live unit's phase record as one JSON object."""
    production = read_production(production_doc)
    records: dict[str, object] = {}
    for unit_plan in _production_unit_plans(production):
        try:
            record: object = _json_record(
                build_plan(unit_plan.plan), production.zone
            )
        except NoPlan as error:
            record = {"unavailable": str(error)}
        records[unit_plan.unit] = record
    return json.dumps(records, indent=2)


PRODUCTION_PATTERN = re.compile(
    r"^> \*\*Production: (?P<production>.+?)\*\* — unit `(?P<unit>[^`]+)`; "
    + r"production doc `(?P<document>[^`]+)`$",
    re.MULTILINE,
)


def _production_reference(plan_path: Path) -> ProductionReference | PlanOutsideProduction:
    try:
        text = plan_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise NoPlan(f"Unable to read plan {plan_path}") from error
    match = PRODUCTION_PATTERN.search(text)
    if match is None:
        return PlanOutsideProduction()
    return ProductionReference(
        production=match.group("production"),
        unit=match.group("unit"),
        document=match.group("document"),
    )


def _git_output(directory: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Refusal(f"cannot inspect Git checkout: {error}") from error
    if result.returncode != 0:
        reason = result.stderr.strip() or f"git exited {result.returncode}"
        raise Refusal(f"cannot inspect Git checkout: {reason}")
    return result.stdout.strip()


def _production_document(plan_path: Path, reference: ProductionReference) -> Path:
    document = Path(reference.document).expanduser()
    if document.is_absolute():
        return document.resolve()
    root = Path(_git_output(plan_path.parent, "rev-parse", "--show-toplevel"))
    return (root / document).resolve()


def _phase_note_name(production: str, unit: str) -> str:
    name = unit
    try:
        marked = unit_lookup.marked_unit(production, unit)
        if marked is not None and isinstance(marked.claude, unit_lookup.LiveClaude):
            if marked.claude.name:
                name = marked.claude.name
    except OSError:
        pass
    cleaned = name.replace("/", "-").replace("\0", "-").lstrip(".")
    return cleaned or unit


def _note_ownership(path: Path) -> NoteOwnership | NoteWithoutPhaseTableOwnership:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return NoteWithoutPhaseTableOwnership()
    if not lines or lines[0] != "---":
        return NoteWithoutPhaseTableOwnership()
    fields: dict[str, str] = {}
    closed = False
    for line in lines[1:]:
        if line == "---":
            closed = True
            break
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip()] = value.strip()
    if not closed or fields.get("phase_table") != "true":
        return NoteWithoutPhaseTableOwnership()
    production = _production_slug_from_link(fields.get("production_file", ""))
    # Notes written before `production_file` carry the slug as `production`.
    production = production or fields.get("production", "")
    unit = fields.get("unit", "")
    if not production or not unit:
        return NoteWithoutPhaseTableOwnership()
    return NoteOwnership(production=production, unit=unit)


def _production_slug_from_link(link: str) -> str:
    """Return the production slug named by a note's `production_file` link."""
    if link.startswith('"'):
        try:
            decoded = cast(object, json.loads(link))
        except json.JSONDecodeError:
            return ""
        link = decoded if isinstance(decoded, str) else ""
    name = link.removeprefix("[[").removesuffix("]]").rsplit("/", 1)[-1]
    slug = name.removesuffix(".md").removesuffix("-production")
    return slug if slug and slug != name.removesuffix(".md") else ""


def _github_blob_url(checkout: Path, relative: Path) -> str:
    """Return the GitHub page of `relative` on `checkout`'s branch, or empty off GitHub."""
    try:
        remote = _git_output(checkout, "remote", "get-url", "origin")
        branch = _git_output(checkout, "symbolic-ref", "--short", "HEAD")
    except Refusal:
        return ""
    match = re.fullmatch(
        r"(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/)"
        + r"(?P<repository>[^/]+/[^/]+?)(?:\.git)?/?",
        remote,
    )
    if match is None:
        return ""
    return (
        f"https://github.com/{match.group('repository')}/blob/{branch}/"
        + relative.as_posix()
    )


def _production_file_link(document: Path, vault: Path) -> str:
    """Link the production doc's main-branch copy, which outlives the production."""
    try:
        root = Path(_git_output(document.parent, "rev-parse", "--show-toplevel")).resolve()
        relative = document.relative_to(root)
        lines = document.read_text(encoding="utf-8").splitlines()
    except (Refusal, ValueError, OSError, UnicodeError):
        return document.as_uri()
    promotion = promotion_from_doc(lines)
    checkout = (
        promotion.checkouts[0].resolve()
        if isinstance(promotion, PromoteTo) and promotion.checkouts
        else root
    )
    main_copy = checkout / relative
    try:
        in_vault = main_copy.relative_to(vault.resolve())
    except ValueError:
        return _github_blob_url(checkout, relative) or main_copy.as_uri()
    return f"[[{in_vault.with_suffix('').as_posix()}]]"


def _most_recent_owned_note(
    vault_root: Path, ownership: NoteOwnership
) -> MostRecentOwnedPhaseNote | NoOwnedPhaseNote:
    matches = [
        path
        for path in vault_root.glob("*/*.md")
        if _note_ownership(path) == ownership
    ]
    if not matches:
        return NoOwnedPhaseNote()
    try:
        newest = max(
            matches,
            key=lambda path: (path.stat().st_mtime_ns, path.as_posix()),
        )
    except OSError as error:
        raise Refusal(f"cannot inspect phase notes in {vault_root}: {error}") from error
    return MostRecentOwnedPhaseNote(path=newest)


def _note_text(
    name: str,
    showrunner: str,
    production_file: str,
    unit: str,
    record: PhaseRecord,
    zone: ZoneInfo,
) -> str:
    return "\n".join(
        (
            "---",
            "phase_table: true",
            f"showrunner: {showrunner}",
            f"production_file: {json.dumps(production_file, ensure_ascii=False)}",
            f"unit: {unit}",
            f"now: {json.dumps(_heading_text(record), ensure_ascii=False)}",
            "---",
            "",
            f"# {name}",
            "",
            render(record, zone),
            "",
        )
    )


def _ensure_showrunners_excluded(vault_root: Path) -> None:
    try:
        checkout = Path(
            _git_output(vault_root.parent, "rev-parse", "--show-toplevel")
        ).resolve()
    except Refusal:
        return
    try:
        relative = vault_root.resolve().relative_to(checkout)
    except ValueError:
        return
    if relative == Path():
        return
    exclude_line = f"{relative.as_posix()}/"
    git_path = Path(
        _git_output(checkout, "rev-parse", "--git-path", "info/exclude")
    )
    exclude = git_path if git_path.is_absolute() else checkout / git_path
    try:
        before = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if exclude_line in before.splitlines():
            return
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a", encoding="utf-8") as handle:
            if before and not before.endswith("\n"):
                _ = handle.write("\n")
            _ = handle.write(f"{exclude_line}\n")
    except OSError as error:
        raise Refusal(f"cannot update {exclude}: {error}") from error


@contextmanager
def _phase_note_lock(folder: Path) -> Generator[None]:
    """Hold the folder lock while checking and writing a phase note."""
    descriptor = os.open(folder, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _write_note(target: Path, content: str, ownership: NoteOwnership) -> None:
    temporary: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with _phase_note_lock(target.parent):
            target_existed = target.exists()
            if target_existed and _note_ownership(target) != ownership:
                raise Refusal(
                    "phase note target is not owned by "
                    + f"{ownership.production}/{ownership.unit}: {target}"
                )
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=target.parent,
                prefix=f".{target.name}.",
                delete=False,
            ) as handle:
                _ = handle.write(content)
                os.fchmod(handle.fileno(), 0o644)
                temporary = Path(handle.name)
            if target_existed:
                os.replace(temporary, target)
            else:
                try:
                    os.link(temporary, target)
                except FileExistsError as error:
                    raise Refusal(
                        f"phase note appeared before publication: {target}"
                    ) from error
    except OSError as error:
        raise Refusal(f"cannot write phase note {target}: {error}") from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _remove_stale_notes(
    vault_root: Path, target: Path, ownership: NoteOwnership
) -> None:
    for candidate in vault_root.glob("*/*.md"):
        if candidate == target or _note_ownership(candidate) != ownership:
            continue
        try:
            candidate.unlink()
            candidate.parent.rmdir()
        except OSError as error:
            if candidate.exists():
                raise Refusal(
                    f"cannot remove stale phase note {candidate}: {error}"
                ) from error


def _vault_root() -> Path:
    return Path(
        os.environ.get("PHASE_TABLE_VAULT")
        or Path.home() / "rust" / "hanadocs" / "showrunners"
    ).expanduser()


def retarget_phase_notes(old_slug: str, new_doc: Path) -> list[Path]:
    """Point each phase note owned by the old production at the new doc."""
    vault_root = _vault_root()
    if not vault_root.exists():
        return []
    production_file = _production_file_link(new_doc, vault_root.parent)
    field = f"production_file: {json.dumps(production_file, ensure_ascii=False)}".encode("utf-8")
    rewritten: list[Path] = []
    for path in sorted(vault_root.glob("*/*.md")):
        temporary: Path | None = None
        try:
            with _phase_note_lock(path.parent):
                ownership = _note_ownership(path)
                if not isinstance(ownership, NoteOwnership) or ownership.production != old_slug:
                    continue
                before = path.read_bytes()
                lines = before.splitlines(keepends=True)
                end = next(index for index in range(1, len(lines)) if lines[index].rstrip(b"\r\n") == b"---")
                header: list[bytes] = [lines[0]]
                has_link = False
                for line in lines[1:end]:
                    key, separator, _ = line.partition(b":")
                    if separator and key.strip() == b"production":
                        continue
                    if separator and key.strip() == b"production_file":
                        newline = line[len(line.rstrip(b"\r\n")):]
                        line = field + newline
                        has_link = True
                    header.append(line)
                if not has_link:
                    header.append(field + lines[0][3:])
                after = b"".join((*header, *lines[end:]))
                if after == before:
                    continue
                with tempfile.NamedTemporaryFile(
                    "wb", dir=path.parent, prefix=f".{path.name}.", delete=False
                ) as handle:
                    temporary = Path(handle.name)
                    _ = handle.write(after)
                    os.fchmod(handle.fileno(), path.stat().st_mode & 0o7777)
                os.replace(temporary, path)
                rewritten.append(path)
        except OSError as error:
            raise Refusal(f"cannot repair phase note {path}: {error}") from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return rewritten


def _archive_note(candidate: Path, vault_root: Path, slug: str) -> Path:
    """Move one generated phase note into the vault's archive and return its new path."""
    # Outside showrunners, the archive stays out of every scan and in the vault's Git history.
    folder = vault_root.parent / "archive" / candidate.parent.name / slug
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise Refusal(f"cannot archive phase note {candidate}: {error}") from error

    target = folder / candidate.name
    suffix = 2
    while True:
        try:
            os.link(candidate, target)
            break
        except FileExistsError:
            target = folder / f"{candidate.stem}-{suffix}.md"
            suffix += 1
        except OSError as error:
            raise Refusal(f"cannot archive phase note {candidate}: {error}") from error

    try:
        candidate.unlink()
    except OSError as error:
        try:
            os.unlink(target)
        except OSError:
            pass
        raise Refusal(f"cannot archive phase note {candidate}: {error}") from error

    try:
        candidate.parent.rmdir()
    except OSError as error:
        try:
            if any(candidate.parent.iterdir()):
                return target
        except OSError:
            pass
        raise Refusal(
            f"cannot remove empty phase note directory {candidate.parent}: {error}"
        ) from error
    return target


def archive(production_doc: Path) -> list[Path]:
    """Move a production's generated phase notes into the vault's archive."""
    production = read_production(production_doc)
    vault_root = _vault_root()
    if not vault_root.parent.exists():
        return []
    archived: list[Path] = []
    for candidate in sorted(vault_root.glob("*/*.md")):
        ownership = _note_ownership(candidate)
        if (
            not isinstance(ownership, NoteOwnership)
            or ownership.production != production.slug
        ):
            continue
        archived.append(_archive_note(candidate, vault_root, production.slug))
    return archived


def prune(production_doc: Path) -> None:
    """Archive generated notes for units no longer live in a production."""
    production = read_production(production_doc)
    live_units = {
        unit_plan.unit for unit_plan in _production_unit_plans(production)
    }
    vault_root = _vault_root()
    for candidate in vault_root.glob("*/*.md"):
        ownership = _note_ownership(candidate)
        if (
            not isinstance(ownership, NoteOwnership)
            or ownership.production != production.slug
            or ownership.unit in live_units
        ):
            continue
        _ = _archive_note(candidate, vault_root, production.slug)


def owned_notes(production: str, unit: str) -> list[Path]:
    """The generated phase notes that name `unit` of `production` as their owner."""
    owner = NoteOwnership(production=production, unit=unit)
    return [candidate for candidate in sorted(_vault_root().glob("*/*.md")) if _note_ownership(candidate) == owner]


def rename_unit(production: str, old: str, new: str) -> list[Path]:
    """Point a renamed unit's generated phase notes at its new id, so prune keeps them.

    Only the `unit:` key changes; the unit's next refresh rewrites the rest of each note.
    """
    renamed: list[Path] = []
    for candidate in owned_notes(production, old):
        try:
            lines = candidate.read_text(encoding="utf-8").splitlines(keepends=True)
        except (OSError, UnicodeError) as error:
            raise Refusal(f"cannot read phase note {candidate}: {error}") from error
        end = next(index for index, line in enumerate(lines[1:], 1) if line.rstrip("\n") == "---")
        lines[1:end] = [f"unit: {new}\n" if line.startswith("unit:") else line for line in lines[1:end]]
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=candidate.parent, prefix=f".{candidate.name}.", delete=False
            ) as handle:
                _ = handle.write("".join(lines))
                os.fchmod(handle.fileno(), 0o644)
            os.replace(handle.name, candidate)
        except OSError as error:
            raise Refusal(f"cannot write phase note {candidate}: {error}") from error
        renamed.append(candidate)
    return renamed


def refresh(session_dir: Path) -> None:
    """Rewrite a production unit's generated phase note, when it has one."""
    record = build(session_dir)
    reference = _production_reference(record.plan)
    if isinstance(reference, PlanOutsideProduction):
        return
    vault_root = _vault_root()
    if not vault_root.parent.exists():
        return
    production = read_production(_production_document(record.plan, reference))
    ownership = NoteOwnership(production=production.slug, unit=reference.unit)
    try:
        showrunner = showrunners.current_name(production.slug)
    except OSError as error:
        raise Refusal(
            f"cannot look up the showrunner of {production.slug}: {error}"
        ) from error
    if showrunner:
        if showrunner in {".", ".."} or "/" in showrunner or "\0" in showrunner:
            raise Refusal("production Showrunner session must be one directory name")
        folder = vault_root / showrunner
    else:
        previous = _most_recent_owned_note(vault_root, ownership)
        if isinstance(previous, NoOwnedPhaseNote):
            raise Refusal(f"the showrunner of {production.slug} is not running")
        folder = previous.path.parent
    name = _phase_note_name(production.slug, reference.unit)
    target = folder / f"{name}.md"
    if target.exists() and _note_ownership(target) != ownership:
        raise Refusal(
            "phase note target is not owned by "
            + f"{production.slug}/{reference.unit}: {target}"
        )
    _ensure_showrunners_excluded(vault_root)
    _write_note(
        target,
        _note_text(
            name,
            folder.name,
            _production_file_link(production.doc, vault_root.parent),
            reference.unit,
            record,
            production.zone,
        ),
        ownership,
    )
    _remove_stale_notes(vault_root, target, ownership)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Show or refresh a unit run's plan phases."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    show_parser = commands.add_parser("show")
    show_target = show_parser.add_mutually_exclusive_group(required=True)
    _ = show_target.add_argument("--session-dir", type=Path)
    _ = show_target.add_argument("--production-doc", type=Path)
    _ = show_parser.add_argument("--zone", default="")
    _ = show_parser.add_argument("--json", action="store_true")
    parallel_parser = commands.add_parser(
        "parallel", help="print the checkpoint notice's Parallel: line"
    )
    _ = parallel_parser.add_argument("--session-dir", type=Path, required=True)
    refresh_parser = commands.add_parser("refresh")
    _ = refresh_parser.add_argument("--session-dir", type=Path, required=True)
    prune_parser = commands.add_parser("prune")
    _ = prune_parser.add_argument("--production-doc", type=Path, required=True)
    archive_parser = commands.add_parser("archive")
    _ = archive_parser.add_argument("--production-doc", type=Path, required=True)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    command_value: object = getattr(args, "command", "")
    session_value: object = getattr(args, "session_dir", Path())
    production_value: object = getattr(args, "production_doc", None)
    zone_value: object = getattr(args, "zone", "")
    json_value: object = getattr(args, "json", False)
    command = command_value if isinstance(command_value, str) else ""
    session_dir = session_value if isinstance(session_value, Path) else Path()
    production_doc = production_value if isinstance(production_value, Path) else None
    zone_name = zone_value if isinstance(zone_value, str) else ""
    try:
        if command == "refresh":
            refresh(session_dir.expanduser().resolve())
            return 0
        if command == "parallel":
            print(parallel_line(build(session_dir.expanduser().resolve()).parallel))
            return 0
        if command == "prune":
            if production_doc is None:
                raise Refusal("prune requires a production document")
            prune(production_doc.expanduser().resolve())
            return 0
        if command == "archive":
            if production_doc is None:
                raise Refusal("archive requires a production document")
            for archived_path in archive(production_doc.expanduser().resolve()):
                print(archived_path)
            return 0
        if production_doc is not None:
            output = show_production(production_doc.expanduser().resolve())
            print(output)
            return 0
        zone = ZoneInfo(zone_name) if zone_name else user_zone()
        output = show(session_dir.expanduser().resolve(), zone, json_value is True)
    except (NoState, NoPlan, Refusal, ZoneInfoNotFoundError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
