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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_SCRIPTS = REPOSITORY_ROOT / "scripts" / "production"
if not __package__:
    sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(PRODUCTION_SCRIPTS))

from scripts.delegate import progress_history
from scripts.production import showrunners, unit_lookup
from scripts.production.add_unit import Refusal, read_production


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
    """The latest target explicitly stated for a running phase."""

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


class DonePhase(NamedTuple):
    """A plan phase marked done in the current plan."""

    phase: str
    title: str
    times: CompletedPhaseTiming | PhaseTimingNotRecorded


class OpenPhase(NamedTuple):
    """A phase started but not finished in the newest run's events.

    This says nothing about whether the unit's session is still alive.
    """

    phase: str
    title: str
    started: datetime
    progress: ReportedPhaseProgress | PhaseProgressNotReported
    eta: StatedEta | ProjectedEta | EtaUnavailable
    first_stated: FirstStatedEtaTarget | EtaNeverStated


class TodoPhase(NamedTuple):
    """An unfinished plan phase that is not currently running."""

    phase: str
    title: str
    times: PredictedPhaseTiming | PhaseTimingNotPredicted


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


class PhaseRecord(NamedTuple):
    """The complete read-only phase view for one delegate session."""

    plan: Path
    updated: datetime
    current: OpenPhase | NoOpenPhase
    phases: list[DonePhase | OpenPhase | TodoPhase]
    plan_finish: FinishedAt | PredictedFinish | UnknownFinish


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
    """The production and unit named by a delegate plan."""

    production: str
    unit: str
    document: str


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
    """The requested delegate session has no readable progress state."""


class NoPlan(Exception):
    """The requested delegate session does not identify a readable plan."""


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
    state_path = session_dir / progress_history.STATE_FILENAME
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
            progress_event = _last_progress(runs, instance.instance_id)
            stated_event = _latest_stated_eta(runs, instance.instance_id)
            projection = _reported_eta(progress_event, instance.start)
            if isinstance(stated_event, LatestStatedEtaEvent):
                first_stated: FirstStatedEtaTarget | EtaNeverStated = (
                    stated_event.first
                )
                eta: StatedEta | ProjectedEta | EtaUnavailable
                if stated_event.eta.time.timestamp() > progress_history.now_epoch():
                    eta = stated_event.eta
                else:
                    eta = projection
            else:
                first_stated = EtaNeverStated()
                eta = projection
            return OpenPhaseInstance(
                phase=instance,
                progress=_reported_progress(progress_event),
                eta=eta,
                first_stated=first_stated,
            )
    return NoOpenPhase()


def _normalized_title(title: str) -> str:
    return " ".join(title.replace("`", "").casefold().split())


def _instances_by_phase(
    phases: list[progress_history.PlanPhase],
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
            if at is None:
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
        spread=progress_history.percent_spread(calibration),
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
    band = progress_history.eta_band_seconds(
        report.percent,
        elapsed_seconds,
        report.spread,
    )
    if isinstance(band, progress_history.EtaProjectionUnavailable):
        return EtaUnavailable()
    return ProjectedEta(
        time=as_of + timedelta(seconds=band.remaining),
        earliest=as_of + timedelta(seconds=band.earliest),
        latest=as_of + timedelta(seconds=band.latest),
        as_of=as_of,
    )


def _latest_stated_eta(
    runs: list[list[dict[str, object]]], instance_id: str
) -> LatestStatedEtaEvent | NoStatedEtaEvent:
    first: FirstStatedEtaTarget | EtaNeverStated = EtaNeverStated()
    latest: StatedEta | NoStatedEtaEvent = NoStatedEtaEvent()
    for events in runs:
        for event in events:
            if (
                _text(event.get("event_type")) != "eta_stated"
                or _text(event.get("phase_instance_id")) != instance_id
            ):
                continue
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
                first = FirstStatedEtaTarget(time=stated.time)
            latest = stated
    if isinstance(latest, NoStatedEtaEvent) or isinstance(first, EtaNeverStated):
        return NoStatedEtaEvent()
    return LatestStatedEtaEvent(eta=latest, first=first)


def _done_phase(
    phase: progress_history.PlanPhase,
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
    last, last_finish = max(completed, key=lambda pair: pair[1].written_at)
    title = _normalized_title(last.title)
    kept = [
        instance
        for instance in instances
        if _normalized_title(instance.title) == title
    ]
    seconds = sum(
        instance.finish.seconds
        for instance in kept
        if isinstance(instance.finish, InstanceFinished)
    )
    return DonePhase(
        phase=phase["id"],
        title=phase["title"],
        times=CompletedPhaseTiming(
            start=min(instance.start for instance in kept),
            finish=last_finish.at,
            seconds=seconds,
        ),
    )


def _plan_finish(
    phases: list[DonePhase | OpenPhase | TodoPhase],
) -> FinishedAt | PredictedFinish | UnknownFinish:
    if not phases:
        return UnknownFinish()
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


def build_plan(plan_path: Path) -> PhaseRecord:
    """Build one plan's phase view entirely from its durable events."""
    plan_path = plan_path.expanduser().resolve()
    try:
        phases = progress_history.plan_phases(plan_path)
    except (OSError, UnicodeError) as error:
        raise NoPlan(f"Unable to read plan {plan_path}") from error

    run_events = [_events(path) for path in progress_history.plan_runs(plan_path)]
    instances = _phase_instances(run_events)
    open_instance = _newest_open_instance(run_events)
    assigned = _instances_by_phase(phases, instances)
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
    for index, phase in enumerate(phases):
        if (
            isinstance(current_position, PlanPhasePosition)
            and index == current_position.offset
            and isinstance(open_instance, OpenPhaseInstance)
        ):
            recorded_title = _normalized_title(open_instance.phase.title)
            related = [
                instance
                for instance in assigned[index]
                if _normalized_title(instance.title) == recorded_title
            ]
            earliest = min(
                (instance.start for instance in related),
                default=open_instance.phase.start,
            )
            current = OpenPhase(
                phase=phase["id"],
                title=phase["title"],
                started=earliest,
                progress=open_instance.progress,
                eta=open_instance.eta,
                first_stated=open_instance.first_stated,
            )
            rows.append(current)
        elif phase["done"]:
            rows.append(_done_phase(phase, assigned[index]))
        else:
            rows.append(
                TodoPhase(
                    phase=phase["id"],
                    title=phase["title"],
                    times=PhaseTimingNotPredicted(),
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
    if isinstance(typical, TypicalDuration):
        now = datetime.fromtimestamp(progress_history.now_epoch(), UTC)
        gap_before_first = False
        if isinstance(current, OpenPhase):
            gap_before_first = True
            if isinstance(current.eta, StatedEta | ProjectedEta):
                previous_finish = current.eta.time
            else:
                previous_finish = max(
                    now,
                    current.started + timedelta(seconds=typical.seconds),
                )
        else:
            previous_finish = now
        for index, row in enumerate(rows):
            if isinstance(row, DonePhase):
                continue
            if isinstance(row, OpenPhase):
                continue
            start = previous_finish
            if gap_before_first:
                start += timedelta(seconds=gap)
            finish = start + timedelta(seconds=typical.seconds)
            rows[index] = TodoPhase(
                phase=row.phase,
                title=row.title,
                times=PredictedPhaseTiming(start=start, finish=finish),
            )
            previous_finish = finish
            gap_before_first = True

    return PhaseRecord(
        plan=plan_path,
        updated=datetime.fromtimestamp(
            progress_history.now_epoch(), UTC
        ),
        current=current,
        phases=rows,
        plan_finish=_plan_finish(rows),
    )


def build(session_dir: Path) -> PhaseRecord:
    """Build a session's phase view without changing its state or history."""
    state = _state(session_dir)
    working_dir_text = _text(state.get("working_dir"))
    plan_doc = _text(state.get("project_plan_doc")) or _text(state.get("plan_doc"))
    if not plan_doc:
        raise NoState(f"Progress state does not name a plan in {session_dir}")
    working_dir = Path(working_dir_text) if working_dir_text else Path.cwd()
    return build_plan(progress_history.resolve_plan_path(working_dir, plan_doc))


def _clock(moment: datetime, zone: ZoneInfo) -> str:
    return moment.astimezone(zone).strftime("%m-%d %H:%M")


def _time(moment: datetime, zone: ZoneInfo) -> str:
    return moment.astimezone(zone).strftime("%H:%M")


def _cell_text(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def _elapsed(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    return f"{hours}:{remainder // 60:02d}"


def _row_status(row: DonePhase | OpenPhase | TodoPhase) -> str:
    if isinstance(row, DonePhase):
        if isinstance(row.times, CompletedPhaseTiming):
            return f"done in {_elapsed(row.times.seconds)}"
        return "done"
    if isinstance(row, OpenPhase):
        if isinstance(row.progress, ReportedPhaseProgress):
            return f"running, {row.progress.percent}%"
        return "running"
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


def render(record: PhaseRecord, zone: ZoneInfo) -> str:
    """Render a phase record as the compact Markdown report."""
    current = record.current
    if isinstance(current, NoOpenPhase):
        done = sum(isinstance(row, DonePhase) for row in record.phases)
        heading = f"**No phase running — {done} of {len(record.phases)} done**"
    else:
        heading = (
            f"**Phase {current.phase} of {len(record.phases)} — {current.title}**"
        )

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
            f"| {row.phase} | {row.title} | {_row_status(row)} | "
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
    }


def _json_record(record: PhaseRecord, zone: ZoneInfo) -> dict[str, object]:
    current_json: dict[str, object] | None = None
    if isinstance(record.current, OpenPhase):
        current = record.current
        eta_json: dict[str, object] | None = None
        percent: int | None = None
        if isinstance(current.progress, ReportedPhaseProgress):
            percent = current.progress.percent
        first: str | None = None
        if isinstance(current.first_stated, FirstStatedEtaTarget):
            first = _iso(current.first_stated.time, zone)
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
                "first": first,
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
                "first": first,
            }
        current_json = {
            "phase": current.phase,
            "of": len(record.phases),
            "title": current.title,
            "started": _iso(current.started, zone),
            "percent": percent,
            "eta": eta_json,
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
    }


def show(session_dir: Path, zone: ZoneInfo, json_output: bool = False) -> str:
    """Return one session's Markdown or JSON phase table."""
    record = build(session_dir)
    if json_output:
        return json.dumps(_json_record(record, zone), indent=2)
    return render(record, zone)


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
    root = Path(_git_output(plan_path.parent, "rev-parse", "--show-toplevel"))
    document = Path(reference.document).expanduser()
    return document.resolve() if document.is_absolute() else (root / document).resolve()


def _phase_note_name(production: str, unit: str) -> str:
    name = unit
    try:
        marked = unit_lookup.marked_units(production).get(unit)
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
    production = fields.get("production", "")
    unit = fields.get("unit", "")
    if not production or not unit:
        return NoteWithoutPhaseTableOwnership()
    return NoteOwnership(production=production, unit=unit)


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
    production: str,
    unit: str,
    record: PhaseRecord,
    zone: ZoneInfo,
) -> str:
    return "\n".join(
        (
            "---",
            "phase_table: true",
            f"production: {production}",
            f"unit: {unit}",
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


def _write_note(target: Path, content: str, ownership: NoteOwnership) -> None:
    target_existed = target.exists()
    if target_existed and _note_ownership(target) != ownership:
        raise Refusal(
            "phase note target is not owned by "
            + f"{ownership.production}/{ownership.unit}: {target}"
        )
    temporary: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
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


def refresh(session_dir: Path) -> None:
    """Rewrite a production unit's generated phase note, when it has one."""
    record = build(session_dir)
    reference = _production_reference(record.plan)
    if isinstance(reference, PlanOutsideProduction):
        return
    vault_root = Path(
        os.environ.get("PHASE_TABLE_VAULT")
        or Path.home() / "rust" / "hanadocs" / "showrunners"
    ).expanduser()
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
        _note_text(name, production.slug, reference.unit, record, production.zone),
        ownership,
    )
    _remove_stale_notes(vault_root, target, ownership)


def _local_zone() -> ZoneInfo:
    configured = os.environ.get("TZ")
    if configured:
        try:
            return ZoneInfo(configured)
        except ZoneInfoNotFoundError:
            pass
    try:
        localtime = Path("/etc/localtime").resolve()
        marker = "/zoneinfo/"
        if marker in str(localtime):
            return ZoneInfo(str(localtime).split(marker, 1)[1])
    except (OSError, ZoneInfoNotFoundError):
        pass
    local = datetime.now().astimezone().tzinfo
    return local if isinstance(local, ZoneInfo) else ZoneInfo("UTC")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Show or refresh a unit run's plan phases."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    show_parser = commands.add_parser("show")
    _ = show_parser.add_argument("--session-dir", type=Path, required=True)
    _ = show_parser.add_argument("--zone", default="")
    _ = show_parser.add_argument("--json", action="store_true")
    refresh_parser = commands.add_parser("refresh")
    _ = refresh_parser.add_argument("--session-dir", type=Path, required=True)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    command_value: object = getattr(args, "command", "")
    session_value: object = getattr(args, "session_dir", Path())
    zone_value: object = getattr(args, "zone", "")
    json_value: object = getattr(args, "json", False)
    command = command_value if isinstance(command_value, str) else ""
    session_dir = session_value if isinstance(session_value, Path) else Path()
    zone_name = zone_value if isinstance(zone_value, str) else ""
    try:
        if command == "refresh":
            refresh(session_dir.expanduser().resolve())
            return 0
        zone = ZoneInfo(zone_name) if zone_name else _local_zone()
        output = show(session_dir.expanduser().resolve(), zone, json_value is True)
    except (NoState, NoPlan, Refusal, ZoneInfoNotFoundError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
