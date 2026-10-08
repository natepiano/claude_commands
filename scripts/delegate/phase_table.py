#!/usr/bin/env python3
"""Build and render a delegate run's plan-phase table from durable events."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import statistics
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.delegate import progress_history


class ProjectedEta(NamedTuple):
    """A progress report's projected finish and uncertainty band."""

    time: datetime
    earliest: datetime
    latest: datetime


class NoEta(NamedTuple):
    """A report is absent or cannot produce a projected finish."""


class Reported(NamedTuple):
    """The last progress report written for a running phase."""

    percent: int
    eta: ProjectedEta | NoEta


class NotReported(NamedTuple):
    """A running phase has no valid progress report."""


class Completed(NamedTuple):
    """Recorded timing for the completed instance family of a done phase."""

    start: datetime
    finish: datetime
    seconds: int


class NotRecorded(NamedTuple):
    """A done plan phase has no completed instance on record."""


class Predicted(NamedTuple):
    """Projected timing for a future plan phase."""

    start: datetime
    finish: datetime


class NotPredicted(NamedTuple):
    """A future plan phase cannot yet be projected."""


class DonePhase(NamedTuple):
    """A plan phase marked done in the current plan."""

    phase: str
    title: str
    times: Completed | NotRecorded


class RunningPhase(NamedTuple):
    """The phase instance active in session state at read time."""

    phase: str
    title: str
    started: datetime
    progress: Reported | NotReported


class TodoPhase(NamedTuple):
    """An unfinished plan phase that is not currently running."""

    phase: str
    title: str
    times: Predicted | NotPredicted


class NoPhaseRunning(NamedTuple):
    """Session state has no active phase."""


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
    current: RunningPhase | NoPhaseRunning
    phases: list[DonePhase | RunningPhase | TodoPhase]
    plan_finish: FinishedAt | PredictedFinish | UnknownFinish


class InstanceFinished(NamedTuple):
    """The finish event written for one phase instance."""

    at: datetime
    status: str
    seconds: int
    written_at: int


class InstanceStillRunning(NamedTuple):
    """No finish event was written for this phase instance."""


class PhaseInstance(NamedTuple):
    """One recorded attempt at a phase, independent of its current plan id."""

    instance_id: str
    phase: str
    title: str
    start: datetime
    finish: InstanceFinished | InstanceStillRunning


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
    except OSError as error:
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
                if started is None:
                    continue
                instance = PhaseInstance(
                    instance_id=instance_id,
                    phase=_text(event.get("phase_id")),
                    title=_text(event.get("phase_title")),
                    start=started,
                    finish=InstanceStillRunning(),
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
) -> dict[str, object] | None:
    last: dict[str, object] | None = None
    for events in runs:
        for event in events:
            if (
                _text(event.get("event_type")) == "progress_reported"
                and _text(event.get("phase_instance_id")) == instance_id
            ):
                last = event
    return last


def _reported_eta(
    report: dict[str, object] | None, started: datetime
) -> Reported | NotReported:
    if report is None:
        return NotReported()
    percent_value = report.get("phase_percent")
    if isinstance(percent_value, bool) or not isinstance(percent_value, int):
        return NotReported()
    percent = percent_value
    as_of_epoch = _epoch(report.get("timestamp_epoch"))
    if as_of_epoch is None:
        return Reported(percent=percent, eta=NoEta())
    elapsed = _duration(report.get("phase_elapsed_seconds"))
    if elapsed is None:
        elapsed = max(0, int(as_of_epoch - started.timestamp()))
    calibration = _object_dict(report.get("phase_calibration"))
    if calibration is None:
        calibration = _object_dict(report.get("calibration"))
    band = progress_history.eta_band_seconds(
        percent,
        elapsed,
        progress_history.percent_spread(calibration),
    )
    if band is None:
        return Reported(percent=percent, eta=NoEta())
    eta, earliest, latest = band
    as_of = datetime.fromtimestamp(as_of_epoch, UTC)
    return Reported(
        percent=percent,
        eta=ProjectedEta(
            time=as_of + timedelta(seconds=eta),
            earliest=as_of + timedelta(seconds=earliest),
            latest=as_of + timedelta(seconds=latest),
        ),
    )


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
        return DonePhase(phase=phase["id"], title=phase["title"], times=NotRecorded())
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
        times=Completed(
            start=min(instance.start for instance in kept),
            finish=last_finish.at,
            seconds=seconds,
        ),
    )


def _plan_finish(
    phases: list[DonePhase | RunningPhase | TodoPhase],
) -> FinishedAt | PredictedFinish | UnknownFinish:
    if not phases:
        return UnknownFinish()
    last = phases[-1]
    if isinstance(last, DonePhase) and isinstance(last.times, Completed):
        return FinishedAt(last.times.finish)
    if isinstance(last, RunningPhase):
        if isinstance(last.progress, Reported) and isinstance(
            last.progress.eta, ProjectedEta
        ):
            return PredictedFinish(last.progress.eta.time)
        return UnknownFinish()
    if isinstance(last, TodoPhase) and isinstance(last.times, Predicted):
        return PredictedFinish(last.times.finish)
    return UnknownFinish()


def build(session_dir: Path) -> PhaseRecord:
    """Build a session's phase view without changing its state or history."""
    state = _state(session_dir)
    working_dir_text = _text(state.get("working_dir"))
    plan_doc = _text(state.get("project_plan_doc")) or (
        _text(state.get("plan_doc"))
    )
    if not plan_doc:
        raise NoPlan(f"No plan recorded for {session_dir}")
    working_dir = Path(working_dir_text) if working_dir_text else Path.cwd()
    plan_path = progress_history.resolve_plan_path(working_dir, plan_doc)
    try:
        phases = progress_history.plan_phases(plan_path)
    except OSError as error:
        raise NoPlan(f"Unable to read plan {plan_path}") from error

    run_events = [_events(path) for path in progress_history.plan_runs(plan_path)]
    instances = _phase_instances(run_events)
    active = _object_dict(state.get("phase"))
    if active is not None and _text(active.get("status")) != "active":
        active = None
    active_id = _text(active.get("id")) if active is not None else ""
    current: RunningPhase | NoPhaseRunning = NoPhaseRunning()
    current_position: PlanPhasePosition | OutsidePlan = OutsidePlan()
    active_instance = ""
    active_title = ""
    active_started = datetime.min.replace(tzinfo=UTC)
    active_progress: Reported | NotReported = NotReported()
    if active is not None:
        started = _moment(active.get("started_at"))
        if started is None:
            raise NoState(f"Active phase has no valid started_at in {session_dir}")
        active_started = started
        active_instance = _text(active.get("instance_id"))
        active_title = _text(active.get("title"), "Untitled phase")
        for index, phase in enumerate(phases):
            if phase["id"] == active_id:
                current_position = PlanPhasePosition(index)
                active_title = phase["title"]
                break
        active_progress = _reported_eta(
            _last_progress(run_events, active_instance), active_started
        )

    historical = [
        instance for instance in instances if instance.instance_id != active_instance
    ]
    assigned = _instances_by_phase(phases, historical)
    rows: list[DonePhase | RunningPhase | TodoPhase] = []
    for index, phase in enumerate(phases):
        if (
            isinstance(current_position, PlanPhasePosition)
            and index == current_position.offset
        ):
            recorded_title = _normalized_title(_text(active.get("title"))) if active else ""
            related = [
                instance
                for instance in assigned[index]
                if _normalized_title(instance.title) == recorded_title
            ]
            earliest = min(
                (instance.start for instance in related),
                default=active_started,
            )
            current = RunningPhase(
                phase=active_id,
                title=active_title,
                started=earliest,
                progress=active_progress,
            )
            rows.append(current)
        elif phase["done"]:
            rows.append(_done_phase(phase, assigned[index]))
        else:
            rows.append(
                TodoPhase(
                    phase=phase["id"],
                    title=phase["title"],
                    times=NotPredicted(),
                )
            )

    if active is not None and isinstance(current_position, OutsidePlan):
        current = RunningPhase(
            phase=active_id,
            title=active_title,
            started=active_started,
            progress=active_progress,
        )

    durations = [
        row.times.seconds
        for row in rows
        if isinstance(row, DonePhase) and isinstance(row.times, Completed)
    ]
    typical: TypicalDuration | UnknownDuration
    if durations:
        typical = TypicalDuration(int(statistics.median(durations)))
    elif isinstance(current, RunningPhase) and isinstance(
        current.progress, Reported
    ) and isinstance(current.progress.eta, ProjectedEta):
        typical = TypicalDuration(
            max(0, int((current.progress.eta.time - current.started).total_seconds()))
        )
    else:
        typical = UnknownDuration()
    gap_samples = _gap_samples(run_events)
    gap = int(statistics.median(gap_samples)) if gap_samples else 0
    if (
        isinstance(current_position, PlanPhasePosition)
        and isinstance(current, RunningPhase)
        and isinstance(current.progress, Reported)
        and isinstance(current.progress.eta, ProjectedEta)
    ):
        previous_finish = current.progress.eta.time
        for index in range(current_position.offset + 1, len(rows)):
            row = rows[index]
            if isinstance(row, DonePhase):
                if isinstance(row.times, Completed):
                    previous_finish = row.times.finish
                continue
            if isinstance(row, RunningPhase) or isinstance(typical, UnknownDuration):
                break
            start = previous_finish + timedelta(seconds=gap)
            finish = start + timedelta(seconds=typical.seconds)
            rows[index] = TodoPhase(
                phase=row.phase,
                title=row.title,
                times=Predicted(start=start, finish=finish),
            )
            previous_finish = finish

    return PhaseRecord(
        plan=plan_path,
        updated=datetime.fromtimestamp(
            progress_history.now_epoch(), UTC
        ),
        current=current,
        phases=rows,
        plan_finish=_plan_finish(rows),
    )


def _clock(moment: datetime, zone: ZoneInfo) -> str:
    return moment.astimezone(zone).strftime("%m-%d %H:%M")


def _time(moment: datetime, zone: ZoneInfo) -> str:
    return moment.astimezone(zone).strftime("%H:%M")


def _elapsed(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    return f"{hours}:{remainder // 60:02d}"


def _row_status(row: DonePhase | RunningPhase | TodoPhase) -> str:
    if isinstance(row, DonePhase):
        if isinstance(row.times, Completed):
            return f"done in {_elapsed(row.times.seconds)}"
        return "done"
    if isinstance(row, RunningPhase):
        if isinstance(row.progress, Reported):
            return f"running, {row.progress.percent}%"
        return "running"
    return "predicted" if isinstance(row.times, Predicted) else "todo"


def _row_clocks(
    row: DonePhase | RunningPhase | TodoPhase, zone: ZoneInfo
) -> tuple[str, str]:
    if isinstance(row, DonePhase):
        if isinstance(row.times, Completed):
            return _clock(row.times.start, zone), _clock(row.times.finish, zone)
        return "—", "—"
    if isinstance(row, RunningPhase):
        finish = "—"
        if isinstance(row.progress, Reported) and isinstance(
            row.progress.eta, ProjectedEta
        ):
            finish = _clock(row.progress.eta.time, zone)
        return _clock(row.started, zone), finish
    if isinstance(row.times, Predicted):
        return _clock(row.times.start, zone), _clock(row.times.finish, zone)
    return "—", "—"


def render(record: PhaseRecord, zone: ZoneInfo) -> str:
    """Render a phase record as the compact Markdown report."""
    current = record.current
    if isinstance(current, NoPhaseRunning):
        done = sum(isinstance(row, DonePhase) for row in record.phases)
        heading = f"**No phase running — {done} of {len(record.phases)} done**"
    else:
        heading = (
            f"**Phase {current.phase} of {len(record.phases)} — {current.title}**"
        )

    started = "—"
    percent = "—"
    if isinstance(current, RunningPhase):
        started = _clock(current.started, zone)
        if isinstance(current.progress, Reported):
            percent = f"{current.progress.percent}%"
    summary = [
        "| | |",
        "| --- | --- |",
        f"| Started | {started} |",
        f"| Done | {percent} |",
    ]
    if isinstance(current, RunningPhase):
        eta_cell = "—"
        if isinstance(current.progress, Reported) and isinstance(
            current.progress.eta, ProjectedEta
        ):
            eta = current.progress.eta
            eta_cell = (
                f"{_clock(eta.time, zone)} "
                + f"({_time(eta.earliest, zone)} to {_time(eta.latest, zone)})"
            )
        summary.append(f"| ETA | {eta_cell} |")
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
    row: DonePhase | RunningPhase | TodoPhase, zone: ZoneInfo
) -> dict[str, object]:
    start: str | None = None
    finish: str | None = None
    seconds: int | None = None
    if isinstance(row, DonePhase):
        status = "done"
        if isinstance(row.times, Completed):
            start = _iso(row.times.start, zone)
            finish = _iso(row.times.finish, zone)
            seconds = row.times.seconds
    elif isinstance(row, RunningPhase):
        status = "running"
        start = _iso(row.started, zone)
        if isinstance(row.progress, Reported) and isinstance(
            row.progress.eta, ProjectedEta
        ):
            finish = _iso(row.progress.eta.time, zone)
    else:
        status = "todo"
        if isinstance(row.times, Predicted):
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
    if isinstance(record.current, RunningPhase):
        current = record.current
        eta_json: dict[str, object] | None = None
        percent: int | None = None
        if isinstance(current.progress, Reported):
            percent = current.progress.percent
        if isinstance(current.progress, Reported) and isinstance(
            current.progress.eta, ProjectedEta
        ):
            eta = current.progress.eta
            eta_json = {
                "time": _iso(eta.time, zone),
                "earliest": _iso(eta.earliest, zone),
                "latest": _iso(eta.latest, zone),
                "source": "projected",
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
    parser = argparse.ArgumentParser(description="Show a delegate run's plan phases.")
    commands = parser.add_subparsers(dest="command", required=True)
    show_parser = commands.add_parser("show")
    _ = show_parser.add_argument("--session-dir", type=Path, required=True)
    _ = show_parser.add_argument("--zone", default="")
    _ = show_parser.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    session_value: object = getattr(args, "session_dir", Path())
    zone_value: object = getattr(args, "zone", "")
    json_value: object = getattr(args, "json", False)
    session_dir = session_value if isinstance(session_value, Path) else Path()
    zone_name = zone_value if isinstance(zone_value, str) else ""
    try:
        zone = ZoneInfo(zone_name) if zone_name else _local_zone()
        output = show(session_dir.expanduser().resolve(), zone, json_value is True)
    except (NoState, NoPlan, ZoneInfoNotFoundError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
