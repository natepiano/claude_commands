#!/usr/bin/env python3
"""Read phase costs and plan history. This command never updates a source store."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import sqlite3
import statistics
import subprocess
import sys
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Protocol, cast
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "history"))
from scripts.direct import plan_history  # noqa: E402
from scripts.history import rounds  # noqa: E402
from scripts.lib.user_zone import user_zone  # noqa: E402
from scripts.usage import collect, weights  # noqa: E402


class HistoryLabels(Protocol):
    PASS_STAGE: dict[str, str]
    ACTIVITY_STAGE: dict[str, str]

    def _clean_label(self, label: str) -> str: ...


history = cast(HistoryLabels, cast(object, importlib.import_module("history")))
MEASURE_EVENTS = frozenset(("run_started", "phase_started", "phase_finished", "pass_started",
                            "pass_finished", "activity_started", "activity_finished",
                            "finding_opened", "finding_batch_dispatched"))


@dataclass(frozen=True)
class Window:
    since: float
    until: float

    def contains(self, at: float) -> bool:
        return self.since <= at < self.until

    def label(self) -> str:
        return f"[{stamp(self.since)}, {stamp(self.until)})"


@dataclass(frozen=True)
class NotRecorded:
    source: str
    reason: str


@dataclass(frozen=True)
class DirectorSession:
    family: str
    session: str


@dataclass(frozen=True)
class Event:
    run: str
    phase: str
    phase_id: str
    kind: str
    at: float
    instance: str
    step: str
    status: str
    director: DirectorSession | NotRecorded
    directory: str
    plan: str
    branch: str
    record: dict[str, object] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class FixWorkerTime:
    seconds: float
    rounds: int


@dataclass(frozen=True)
class PhaseBoundary:
    """A completed, single-plan-phase interval, eligible for phase costs."""
    run: str
    instance: str
    identifier: str
    start: float
    end: float
    repair: FixWorkerTime = FixWorkerTime(0.0, 0)


@dataclass(frozen=True)
class OpenPhase:
    run: str
    instance: str
    start: float


@dataclass(frozen=True)
class MixedPhase:
    run: str
    instance: str
    start: float
    finish: float | OpenPhase


@dataclass(frozen=True)
class StepInterval:
    run: str
    phase: str
    step: str
    start: float
    end: float
    is_pass: bool


@dataclass(frozen=True)
class StepCost:
    step: str
    n: int
    median: float
    p90: float
    total: float
    wall_share: float


@dataclass(frozen=True)
class PhaseSteps:
    costs: tuple[StepCost, ...]
    gaps: tuple[StepCost, ...]
    phases: int
    wall: float


@dataclass(frozen=True)
class UsageTurn:
    director: DirectorSession
    at: float
    input: int
    output: int
    model: str = ""
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0

    @property
    def raw_input(self) -> int:
        return self.input + self.cache_write_5m + self.cache_write_1h + self.cache_read


@dataclass(frozen=True)
class BeforeChange:
    direct_hash: str


@dataclass(frozen=True)
class AfterChange:
    direct_hash: str


@dataclass(frozen=True)
class ExposureNotRecorded:
    reason: str


TrialExposure = BeforeChange | AfterChange | ExposureNotRecorded


@dataclass(frozen=True)
class RouteHashes:
    before: frozenset[str]
    after: frozenset[str]


@dataclass(frozen=True)
class PhaseWithGap:
    end: float


@dataclass(frozen=True)
class NoFollowingPhase:
    pass


@dataclass(frozen=True)
class SharedDirector:
    pass


@dataclass(frozen=True)
class TrialPhase:
    phase: PhaseBoundary
    exposure: TrialExposure
    interval: PhaseWithGap | NoFollowingPhase | SharedDirector | ExposureNotRecorded | NotRecorded


@dataclass(frozen=True)
class Unweighted:
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class TrialTokens:
    weighted: float | Unweighted
    raw_input: int
    output: int
    turns: int


@dataclass(frozen=True)
class NoCompaction:
    pass


@dataclass(frozen=True)
class NoReload:
    reason: str


@dataclass(frozen=True)
class CompactionBoundary:
    at: float


@dataclass(frozen=True)
class ContractRead:
    at: float
    tool_id: str
    path: str
    request: str


@dataclass(frozen=True)
class ReadResult:
    at: float
    tool_id: str
    failed: bool


@dataclass(frozen=True)
class ContextRequest:
    at: float
    request: str
    raw_input: int | NotRecorded


TranscriptEntry = CompactionBoundary | ContractRead | ReadResult | ContextRequest


@dataclass(frozen=True)
class DirectorTranscript:
    entries: tuple[TranscriptEntry, ...]


@dataclass(frozen=True)
class PhaseReloads:
    compactions: int
    contexts: tuple[int | NoReload | NotRecorded, ...] | NoCompaction


@dataclass(frozen=True)
class TrialMeasures:
    trial: TrialPhase
    tokens: TrialTokens | NotRecorded
    reloads: PhaseReloads | NotRecorded


@dataclass(frozen=True)
class TokenCost:
    turns: int
    input: int
    output: int
    before_share: float | NotRecorded
    after_share: float | NotRecorded


@dataclass(frozen=True)
class PhaseTokens:
    phase: PhaseBoundary
    cost: TokenCost | NotRecorded


@dataclass(frozen=True)
class ContractFile:
    path: str
    lines: int
    bytes: int
    cadence: str


@dataclass(frozen=True)
class DocCommit:
    sha: str
    at: float
    message: str
    text: str


@dataclass(frozen=True)
class PlanHistory:
    path: str
    runs: tuple[str, ...]
    started: float
    first_phase: float | NotRecorded
    commits: tuple[DocCommit, ...]
    sources: tuple[tuple[str, tuple[DocCommit, ...]], ...]
    problem: str
    churn_problem: str = ""


@dataclass(frozen=True)
class DocMilestone:
    plan: str
    document: str
    first_commit: DocCommit | NotRecorded
    run_started: float
    phase_started: float | NotRecorded
    commands: str
    problem: str


@dataclass(frozen=True)
class PlanChurn:
    plan: str
    commits: int
    todo_edits: int
    decisions_opened: int
    decisions_closed: int
    inserted_phases: tuple[str, ...]
    split: str
    problem: str


@dataclass(frozen=True)
class NotPhasedPlan:
    path: str


@dataclass(frozen=True)
class DocumentRevision:
    sha: str
    at: float
    message: str
    path: str


@dataclass(frozen=True)
class SourceReference:
    path: str
    relative_to_plan: bool


def report_zone() -> ZoneInfo:
    """One report uses one zone setting, read through the shared zone module."""
    return user_zone()


def stamp(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).astimezone(report_zone()).strftime("%Y-%m-%d %H:%M:%S %Z")


def text_field(raw: dict[str, object], key: str) -> str:
    value = raw.get(key)
    return value if isinstance(value, str) else ""


def parse_event(raw: dict[str, object]) -> Event | NotRecorded:
    version = raw.get("spine_version", 0)
    if not isinstance(version, int) or version > 1:
        return NotRecorded("progress_history.py", "unsupported spine version")
    epoch = raw.get("timestamp_epoch")
    if not isinstance(epoch, int | float) or isinstance(epoch, bool) or not math.isfinite(epoch):
        return NotRecorded("progress_history.py", "missing or invalid timestamp_epoch")
    identity = raw.get("main_agent")
    director: DirectorSession | NotRecorded = NotRecorded("run_started.main_agent", "session is absent")
    if isinstance(identity, dict):
        agent = cast(dict[str, object], identity)
        session = text_field(agent, "session_id")
        if session:
            director = DirectorSession(text_field(agent, "family"), session)
    kind = text_field(raw, "event_type")
    is_pass = kind.startswith("pass_")
    label = text_field(raw, "pass_kind" if is_pass else "activity_label")
    step = history.PASS_STAGE.get(label, label or "unknown") if is_pass else history._clean_label(label)  # pyright: ignore[reportPrivateUsage]
    # Fix activities and passes share the same wall-coverage label.
    if not is_pass and history.ACTIVITY_STAGE.get(step) == "fix":
        step = "fix"
    return Event(text_field(raw, "run_id"), text_field(raw, "phase_instance_id"),
                 text_field(raw, "phase_id"), kind, float(epoch),
                 text_field(raw, "pass_instance_id" if is_pass else "activity_instance_id"),
                 step, text_field(raw, "status"), director, text_field(raw, "working_dir"),
                 text_field(raw, "plan_doc"), text_field(raw, "branch"), raw)


def read_events(paths: Iterable[Path]) -> tuple[tuple[Event, ...], tuple[str, ...]]:
    events: list[Event] = []
    problems: list[str] = []
    seen: set[str] = set()
    for path in sorted(paths):
        try:
            with path.open(encoding="utf-8") as source:
                for line_no, line in enumerate(source, 1):
                    try:
                        raw = cast(object, json.loads(line))
                    except ValueError:
                        problems.append(f"{path}:{line_no}: invalid JSON")
                        continue
                    if not isinstance(raw, dict):
                        problems.append(f"{path}:{line_no}: event is not an object")
                        continue
                    fields = cast(dict[str, object], raw)
                    version = fields.get("spine_version", 0)
                    if text_field(fields, "event_type") not in MEASURE_EVENTS and isinstance(version, int) and version <= 1:
                        continue
                    identifier = text_field(fields, "event_id")
                    if identifier and identifier in seen:
                        continue
                    if identifier:
                        seen.add(identifier)
                    parsed = parse_event(fields)
                    if isinstance(parsed, Event):
                        events.append(parsed)
                    else:
                        problems.append(f"{path}:{line_no}: {parsed.reason}")
        except OSError as error:
            problems.append(f"{path}: {error}")
    return tuple(sorted(events, key=lambda event: event.at)), tuple(problems)


def rounds_records(events: Sequence[Event]) -> list[dict[str, object]]:
    """Keep source fields for the owning parser; support parsed fixture events."""
    records: list[dict[str, object]] = []
    starts: dict[tuple[str, str, str], float] = {}
    for event in sorted(events, key=lambda event: event.at):
        if event.record:
            records.append(event.record)
            continue
        raw: dict[str, object] = {
            "run_id": event.run, "phase_instance_id": event.phase,
            "phase_id": event.phase_id, "event_type": event.kind,
            "timestamp_epoch": event.at, "status": event.status,
        }
        if event.kind.startswith("pass_"):
            key = (event.run, event.phase, event.instance)
            if event.kind == "pass_started":
                starts[key] = event.at
            start = starts.get(key, event.at)
            raw.update(pass_instance_id=event.instance,
                       pass_kind="impl" if event.step == "implementation" else event.step,
                       pass_started_at=start, pass_elapsed_seconds=event.at - start)
        records.append(raw)
    return records


def phase_states(events: Sequence[Event]) -> tuple[PhaseBoundary | OpenPhase | MixedPhase, ...]:
    """Convert rounds.py's open sentinel at the API boundary, never to epoch 0."""
    result: list[PhaseBoundary | OpenPhase | MixedPhase] = []
    for phase in rounds.load_phases(rounds_records(events)):
        run, instance, start = phase["run_id"], phase["phase_instance_id"], phase["started_at"]
        finish: float | OpenPhase = (OpenPhase(run, instance, start) if phase["finished_at"] == 0.0
                                     else phase["finished_at"])
        if phase["check"] == "mixed":
            result.append(MixedPhase(run, instance, start, finish))
        elif isinstance(finish, OpenPhase):
            result.append(finish)
        elif phase["status"] == "completed" and finish >= start:
            result.append(PhaseBoundary(run, instance, phase["phase_id"], start, finish,
                                        FixWorkerTime(phase["fix_seconds"], len(phase["rounds"]))))
    return tuple(result)


def phase_boundaries(events: Sequence[Event], window: Window) -> tuple[PhaseBoundary, ...]:
    """Use completed, unmixed rounds.py phases that finish in [since, until)."""
    return tuple(sorted((phase for phase in phase_states(events)
                         if isinstance(phase, PhaseBoundary) and window.contains(phase.end)),
                        key=lambda phase: (phase.end, phase.run, phase.instance)))


def fix_time(phases: Sequence[PhaseBoundary]) -> tuple[FixWorkerTime, ...]:
    """Fix worker time and round counts come only from rounds.py."""
    return tuple(phase.repair for phase in phases)


def repaired_phase_costs(phases: Sequence[PhaseBoundary]) -> tuple[FixWorkerTime, ...]:
    return tuple(repair for repair in fix_time(phases) if repair.rounds > 0)


def step_intervals(events: Sequence[Event], phases: Sequence[PhaseBoundary]) -> tuple[StepInterval, ...]:
    bounds = {(phase.run, phase.instance): phase for phase in phases}
    opened: dict[tuple[str, str, str, bool], Event] = {}
    intervals: list[StepInterval] = []
    for event in sorted(events, key=lambda event: event.at):
        is_pass = event.kind.startswith("pass_")
        key = (event.run, event.phase, event.instance, is_pass)
        if event.kind in ("pass_started", "activity_started") and event.instance:
            opened[key] = event
        elif event.kind in ("pass_finished", "activity_finished") and key in opened:
            start = opened.pop(key)
            phase = bounds.get((start.run, start.phase))
            if phase is not None:
                left, right = max(start.at, phase.start), min(event.at, phase.end)
                if right > left:
                    label = "fix" if start.step == "fix" else ("pass: " if is_pass else "activity: ") + start.step
                    intervals.append(StepInterval(start.run, start.phase, label, left, right, is_pass))
        elif event.kind == "pass_finished" and event.step == "fix":
            # Finish records retain pass bounds even when the start event is absent.
            # Use the same start/elapsed fallback as rounds.py's pass reader.
            recorded_start = event.record.get("pass_started_at")
            elapsed = event.record.get("pass_elapsed_seconds")
            start_at = (float(recorded_start) if isinstance(recorded_start, int | float)
                        and not isinstance(recorded_start, bool) else event.at -
                        (float(elapsed) if isinstance(elapsed, int | float) and not isinstance(elapsed, bool) else 0.0))
            phase = bounds.get((event.run, event.phase))
            if phase is not None:
                left, right = max(start_at, phase.start), min(event.at, phase.end)
                if right > left:
                    intervals.append(StepInterval(event.run, event.phase, "fix", left, right, True))
    # An unclosed window is unknown coverage, never assumed to last to phase end.
    return tuple(intervals)


def summarize(step: str, values: Sequence[float], wall: float) -> StepCost:
    ordered = sorted(values)
    return StepCost(step, len(ordered), statistics.median(ordered),
                    ordered[max(0, math.ceil(len(ordered) * .9) - 1)],
                    sum(ordered), sum(ordered) / wall if wall else 0)


def allocated_step_seconds(events: Sequence[Event], phases: Sequence[PhaseBoundary]) -> tuple[dict[str, float], ...]:
    intervals: dict[tuple[str, str], list[StepInterval]] = defaultdict(list)
    for interval in step_intervals(events, phases):
        intervals[(interval.run, interval.phase)].append(interval)
    samples: list[dict[str, float]] = []
    for phase in phases:
        spans = intervals[(phase.run, phase.instance)]
        points = sorted({phase.start, phase.end, *(span.start for span in spans), *(span.end for span in spans)})
        totals: dict[str, float] = defaultdict(float)
        for left, right in zip(points, points[1:]):
            active = {span.step for span in spans if span.start <= left and span.end >= right}
            if "fix" in active:
                active.discard("activity: merge")
            if not active:
                totals["director time"] += right - left
            else:
                for step in active - {"fix"}:
                    totals[step] += (right - left) / len(active)
        samples.append(dict(totals))
    return tuple(samples)


def measure_phase_steps(events: Sequence[Event], phases: Sequence[PhaseBoundary]) -> PhaseSteps:
    samples: dict[str, list[float]] = defaultdict(list)
    wall = sum(phase.end - phase.start for phase in phases)
    for totals in allocated_step_seconds(events, phases):
        for step, value in totals.items():
            samples[step].append(value)
    gaps: list[float] = []
    starts: dict[str, list[float]] = defaultdict(list)
    for event in events:
        if event.kind == "phase_started":
            starts[event.run].append(event.at)
    for phase in phases:
        following = [at for at in starts[phase.run] if at >= phase.end]
        if following:
            gaps.append(min(following) - phase.end)
    return PhaseSteps(tuple(summarize(step, values, wall) for step, values in sorted(samples.items())),
                      (summarize("phase end to next phase start", gaps, wall),) if gaps else (), len(phases), wall)


def read_usage(path: Path, window: Window,
               directors: Sequence[DirectorSession] = ()) -> tuple[UsageTurn, ...] | NotRecorded:
    if not path.is_file():
        return NotRecorded(str(path), "usage store is absent; collect.py supplies turns")
    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
            query = ("SELECT family, agent_id, at, input, output, model, cache_write_5m, cache_write_1h, cache_read " +
                     "FROM turns WHERE machine = 'natedev' AND parent_id = '' AND at >= ? AND at < ?")
            dates = (datetime.fromtimestamp(window.since, UTC).isoformat(),
                     datetime.fromtimestamp(window.until, UTC).isoformat())
            rows: list[tuple[str, str, str, int, int, str, int, int, int]] = []
            if directors:
                # The ledger indexes machine/family/parent/session/time. Select
                # each mapped session through that index, without a store write.
                for director in set(directors):
                    rows.extend(cast(list[tuple[str, str, str, int, int, str, int, int, int]], db.execute(
                        query + " AND family = ? AND agent_id = ?", (*dates, director.family, director.session)).fetchall()))
            else:
                rows = cast(list[tuple[str, str, str, int, int, str, int, int, int]], db.execute(query, dates).fetchall())
        return tuple(UsageTurn(DirectorSession(family, session), datetime.fromisoformat(at).timestamp(),
                               inp, out, model, five, hour, cached)
                     for family, session, at, inp, out, model, five, hour, cached in rows)
    except (sqlite3.Error, ValueError) as error:
        return NotRecorded(str(path), str(error))


def measure_director_tokens(events: Sequence[Event], phases: Sequence[PhaseBoundary],
                            turns: tuple[UsageTurn, ...] | NotRecorded) -> tuple[PhaseTokens, ...]:
    by_run: dict[str, list[Event]] = defaultdict(list)
    pass_events: dict[tuple[str, str], list[Event]] = defaultdict(list)
    for event in events:
        by_run[event.run].append(event)
        if event.kind in ("pass_started", "pass_finished"):
            pass_events[(event.run, event.phase)].append(event)
    by_director: dict[DirectorSession, list[UsageTurn]] = defaultdict(list)
    if not isinstance(turns, NotRecorded):
        for turn in turns:
            by_director[turn.director].append(turn)
    for director_turns in by_director.values():
        director_turns.sort(key=lambda turn: turn.at)
    passes_by_phase: dict[tuple[str, str], list[StepInterval]] = defaultdict(list)
    for span in step_intervals(events, phases):
        if span.is_pass:
            passes_by_phase[(span.run, span.phase)].append(span)
    result: list[PhaseTokens] = []
    for phase in phases:
        if isinstance(turns, NotRecorded):
            result.append(PhaseTokens(phase, turns))
            continue
        # Only run_started identifies the director. Old worker pass events can carry
        # the worker as main_agent, so they cannot change this mapping.
        directors = {event.director for event in by_run[phase.run]
                     if event.kind == "run_started" and isinstance(event.director, DirectorSession)}
        if not directors:
            result.append(PhaseTokens(phase, NotRecorded("run_started.main_agent.session_id + usage.db turns",
                                                       "no director session maps to this run")))
            continue
        selected: list[UsageTurn] = []
        for director in directors:
            candidates = by_director[director]
            left = bisect_left(candidates, phase.start, key=lambda turn: turn.at)
            right = bisect_left(candidates, phase.end, key=lambda turn: turn.at)
            selected.extend(candidates[left:right])
        if not selected:
            result.append(PhaseTokens(phase, NotRecorded("usage.db turns (collect.py)", "no mapped turns in phase")))
            continue
        passes = passes_by_phase[(phase.run, phase.instance)]
        inp, out = sum(turn.raw_input for turn in selected), sum(turn.output for turn in selected)
        total = inp + out
        missing = NotRecorded("pass_started/pass_finished", "no complete pass in phase")
        before: float | NotRecorded = missing
        after: float | NotRecorded = missing
        if passes and total:
            first, last = min(span.start for span in passes), max(span.end for span in passes)
            before = sum(turn.raw_input + turn.output for turn in selected if turn.at < first) / total
            after = sum(turn.raw_input + turn.output for turn in selected if turn.at >= last) / total
        opened: set[str] = set()
        for entry in sorted(pass_events[(phase.run, phase.instance)], key=lambda entry: entry.at):
            if entry.at > phase.end:
                break
            if entry.kind == "pass_started" and entry.instance and entry.at < phase.end:
                opened.add(entry.instance)
            elif entry.kind == "pass_finished":
                opened.discard(entry.instance)
        if opened:
            after = NotRecorded("pass_finished", "open pass start leaves last pass end unknown")
        result.append(PhaseTokens(phase, TokenCost(len(selected), inp, out, before, after)))
    return tuple(result)


def direct_hash(event: Event) -> str | ExposureNotRecorded:
    contract = collect.object_fields(event.record.get("contract_revision"))
    direct = collect.object_fields(contract.get("direct_md"))
    value = text_field(direct, "sha256")
    if direct.get("state") != "Recorded":
        return ExposureNotRecorded(f"{event.kind}: direct.md {text_field(direct, 'state') or 'not recorded'}"
                                   + (f": {direct['reason']}" if 'reason' in direct else ""))
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        return ExposureNotRecorded(f"{event.kind}: invalid direct.md sha256")
    return value


def trial_exposure(start: Event, finish: Event,
                   route: RouteHashes | NotRecorded) -> TrialExposure:
    first, last = direct_hash(start), direct_hash(finish)
    if isinstance(first, ExposureNotRecorded):
        return first
    if isinstance(last, ExposureNotRecorded):
        return last
    if first != last:
        return ExposureNotRecorded("direct.md hash changed during phase")
    if isinstance(route, NotRecorded):
        return ExposureNotRecorded(route.reason)
    if first in route.before and first in route.after:
        return ExposureNotRecorded("direct.md hash occurs on both sides named by the exposure options")
    if first in route.before:
        return BeforeChange(first)
    if first in route.after:
        return AfterChange(first)
    return ExposureNotRecorded("direct.md hash is on neither side named by the exposure options")


def contract_history_hashes(root: Path, commits: Sequence[str]) -> frozenset[str]:
    result: set[str] = set()
    for commit in commits:
        blob = subprocess.run(["git", "--no-optional-locks", "-C", str(root),
                               "show", f"{commit}:commands/unit/direct.md"],
                              capture_output=True, timeout=8, check=False)
        if blob.returncode == 0:
            result.add(hashlib.sha256(blob.stdout).hexdigest())
    return frozenset(result)


def read_route_hashes(root: Path, revision: str) -> RouteHashes | NotRecorded:
    """Use byte identity in git history, never a deployment timestamp."""
    if not revision:
        return NotRecorded("--route-revision", "route revision not supplied")
    try:
        commit = git_output(root, ["rev-parse", "--verify", f"{revision}^{{commit}}"]).strip()
        parent = git_output(root, ["rev-parse", "--verify", f"{commit}^"]).strip() if commit else ""
        if not parent:
            return NotRecorded("--route-revision", "route revision or its parent is absent")
        direct = "commands/unit/direct.md"
        before = git_output(root, ["rev-list", parent, "--", direct]).splitlines()
        # Exclude branches which do not descend from the selected route revision.
        ancestry = subprocess.run(["git", "--no-optional-locks", "-C", str(root),
                                   "merge-base", "--is-ancestor", commit, "HEAD"],
                                  capture_output=True, timeout=8, check=False)
        if ancestry.returncode != 0:
            return NotRecorded("--route-revision", "route revision is not an ancestor of main HEAD")
        after = [commit, *git_output(root, ["rev-list", "--ancestry-path", f"{commit}..HEAD", "--", direct]).splitlines()]

        return RouteHashes(contract_history_hashes(root, before), contract_history_hashes(root, after))
    except (OSError, subprocess.TimeoutExpired) as error:
        return NotRecorded("--route-revision", str(error))


def read_exposure_hashes(root: Path, revision: str, before_at: str) -> RouteHashes | NotRecorded:
    """Name the before history independently of a future route commit."""
    if not revision and not before_at:
        return NotRecorded("trial exposure", "neither --before-at nor --route-revision supplied")
    route = read_route_hashes(root, revision) if revision else RouteHashes(frozenset(), frozenset())
    if isinstance(route, NotRecorded) or not before_at:
        return route
    try:
        anchor = git_output(root, ["rev-parse", "--verify", f"{before_at}^{{commit}}"]).strip()
        if not anchor:
            return NotRecorded("--before-at", "before anchor is not a commit")
        commits = git_output(root, ["rev-list", anchor, "--", "commands/unit/direct.md"]).splitlines()
        return RouteHashes(contract_history_hashes(root, commits), route.after)
    except (OSError, subprocess.TimeoutExpired) as error:
        return NotRecorded("--before-at", str(error))


def run_directors(events: Sequence[Event]) -> dict[str, frozenset[DirectorSession]]:
    mapped: dict[str, set[DirectorSession]] = defaultdict(set)
    for event in events:
        if event.kind == "run_started" and isinstance(event.director, DirectorSession):
            mapped[event.run].add(event.director)
    return {run: frozenset(directors) for run, directors in mapped.items()}


def select_trial_phases(events: Sequence[Event], window: Window,
                        route: RouteHashes | NotRecorded) -> tuple[TrialPhase, ...]:
    directors = run_directors(events)
    starts = sorted((event for event in events if event.kind == "phase_started"), key=lambda event: event.at)
    finishes = {(event.run, event.phase): event for event in events if event.kind == "phase_finished"}
    results: list[TrialPhase] = []
    candidates = sorted((phase for phase in phase_states(events)
                         if isinstance(phase, PhaseBoundary) and window.contains(phase.start)),
                        key=lambda phase: (phase.start, phase.run, phase.instance))
    for phase in candidates:
        own = [entry for entry in starts if (entry.run, entry.phase) == (phase.run, phase.instance)]
        finish = finishes.get((phase.run, phase.instance))
        if len(own) != 1 or finish is None:
            absent = ExposureNotRecorded("phase start or finish record is absent or ambiguous")
            results.append(TrialPhase(phase, absent, absent))
            continue
        start = own[0]
        exposure = trial_exposure(start, finish, route)
        following = next((entry for entry in starts if entry.run == phase.run
                          and entry.phase != phase.instance and entry.at >= phase.start), None)
        end = max(phase.end, following.at) if following is not None else phase.end
        mapped = directors.get(phase.run, frozenset())
        shared = any((other.run, other.phase) != (phase.run, phase.instance)
                     and mapped.intersection(directors.get(other.run, frozenset()))
                     and other.at < end
                     and (finishes[(other.run, other.phase)].at if (other.run, other.phase) in finishes
                          else math.inf) > phase.start for other in starts)
        interval: PhaseWithGap | NoFollowingPhase | SharedDirector | ExposureNotRecorded | NotRecorded
        if len(mapped) != 1:
            interval = NotRecorded("run_started.main_agent", "phase has no single mapped director")
        elif shared:
            interval = SharedDirector()
        elif following is None:
            interval = NoFollowingPhase()
        elif isinstance(exposure, ExposureNotRecorded):
            interval = exposure
        else:
            next_hash = direct_hash(following)
            interval = (next_hash if isinstance(next_hash, ExposureNotRecorded) else
                        PhaseWithGap(following.at) if next_hash == exposure.direct_hash else
                        ExposureNotRecorded("next phase starts with a different direct.md hash"))
        results.append(TrialPhase(phase, exposure, interval))
    return tuple(results)


def trial_tokens(trial: TrialPhase, director: DirectorSession,
                 turns: tuple[UsageTurn, ...] | NotRecorded) -> TrialTokens | NotRecorded:
    if isinstance(turns, NotRecorded):
        return turns
    if not isinstance(trial.interval, PhaseWithGap):
        return NotRecorded("phase plus gap", type(trial.interval).__name__)
    selected = [turn for turn in turns if turn.director == director
                and trial.phase.start <= turn.at < trial.interval.end]
    if not selected:
        return NotRecorded("usage.db turns", "no mapped turns in phase plus gap")
    total = 0.0
    unweighted: set[str] = set()
    for turn in selected:
        try:
            total += weights.weighted({"model": turn.model, "input": turn.input, "output": turn.output,
                                       "cache_write_5m": turn.cache_write_5m, "cache_write_1h": turn.cache_write_1h,
                                       "cache_read": turn.cache_read}, datetime.fromtimestamp(turn.at, UTC))
        except weights.UnweightedModel as error:
            unweighted.add(str(error))
        except ValueError as error:
            return NotRecorded("usage.db turns", str(error))
    return TrialTokens(Unweighted(tuple(sorted(unweighted))) if unweighted else total,
                       sum(turn.raw_input for turn in selected), sum(turn.output for turn in selected), len(selected))


def transcript_time(record: dict[str, object]) -> float | NotRecorded:
    value = text_field(record, "timestamp")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment).timestamp()
    except ValueError:
        return NotRecorded("director transcript", "invalid or missing timestamp")


def request_input(message: dict[str, object]) -> int | NotRecorded:
    usage = collect.object_fields(message.get("usage"))
    required = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    counts = [usage.get(key) for key in required]
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in counts):
        return NotRecorded("director transcript", "request input usage is incomplete")
    return sum(cast(list[int], counts))


def read_transcript(path: Path) -> DirectorTranscript | NotRecorded:
    entries: list[TranscriptEntry] = []
    requests: dict[str, int] = {}
    try:
        with path.open(encoding="utf-8") as source:
            for line_no, line in enumerate(source, 1):
                try:
                    raw = cast(object, json.loads(line))
                except ValueError:
                    return NotRecorded(str(path), f"invalid JSON at line {line_no}")
                record = collect.object_fields(raw)
                at = transcript_time(record)
                if record.get("type") not in ("system", "assistant", "user"):
                    continue
                if isinstance(at, NotRecorded):
                    # Non-message system notices do not carry measurement evidence.
                    if record.get("type") == "system" and record.get("subtype") != "compact_boundary":
                        continue
                    return at
                if record.get("type") == "system" and record.get("subtype") == "compact_boundary":
                    entries.append(CompactionBoundary(at))
                message = collect.object_fields(record.get("message"))
                key = json.dumps((message.get("id"), record.get("requestId")))
                if record.get("type") == "assistant":
                    if not text_field(message, "id"):
                        return NotRecorded(str(path), "assistant request has no message id")
                    inp = request_input(message)
                    if key not in requests:
                        requests[key] = len(entries)
                        entries.append(ContextRequest(at, key, inp))
                    else:
                        index = requests[key]
                        previous = entries[index]
                        if isinstance(previous, ContextRequest) and isinstance(inp, int):
                            entries[index] = ContextRequest(previous.at, key, max(inp, previous.raw_input)
                                                           if isinstance(previous.raw_input, int) else inp)
                content = message.get("content")
                for block in cast(list[object], content) if isinstance(content, list) else ():
                    fields = collect.object_fields(block)
                    if fields.get("type") == "tool_use" and fields.get("name") == "Read":
                        inputs = collect.object_fields(fields.get("input"))
                        entries.append(ContractRead(at, text_field(fields, "id"), text_field(inputs, "file_path"), key))
                    elif fields.get("type") == "tool_result":
                        entries.append(ReadResult(at, text_field(fields, "tool_use_id"), fields.get("is_error") is True))
        return DirectorTranscript(tuple(entries))
    except (OSError, UnicodeError) as error:
        return NotRecorded(str(path), str(error))


def read_director_transcripts(home: Path, directors: Sequence[DirectorSession]) -> dict[DirectorSession, DirectorTranscript | NotRecorded]:
    result: dict[DirectorSession, DirectorTranscript | NotRecorded] = {}
    for director in set(directors):
        if director.family != "claude":
            result[director] = NotRecorded("director transcript", f"{director.family} has no compact_boundary/Read evidence")
            continue
        paths = sorted((home / ".claude/projects").glob(f"*/{director.session}.jsonl"))
        result[director] = (read_transcript(paths[0]) if len(paths) == 1 else
                            NotRecorded("director transcript", "transcript is absent or has multiple paths"))
    return result


def reload_paths(events: Sequence[Event], phase: PhaseBoundary, exposure: TrialExposure) -> frozenset[str]:
    paths = {"commands/unit/direct.md"}
    if not isinstance(exposure, AfterChange):
        return frozenset(paths)
    for event in events:
        if (event.run, event.phase) == (phase.run, phase.instance) and event.kind == "phase_started":
            contract = collect.object_fields(event.record.get("contract_revision"))
            paths.update(collect.object_fields(contract.get("files")))
    return frozenset(paths)


def measure_reloads(phase: PhaseBoundary, transcript: DirectorTranscript | NotRecorded,
                    paths: frozenset[str]) -> PhaseReloads | NotRecorded:
    if isinstance(transcript, NotRecorded):
        return transcript
    entries = transcript.entries
    boundaries = [(index, entry) for index, entry in enumerate(entries)
                  if isinstance(entry, CompactionBoundary) and phase.start <= entry.at < phase.end]
    if not boundaries:
        return PhaseReloads(0, NoCompaction())
    contexts: list[int | NoReload | NotRecorded] = []
    for number, (index, _) in enumerate(boundaries):
        stop = boundaries[number + 1][0] if number + 1 < len(boundaries) else len(entries)
        tail = entries[index + 1:stop]
        following = [(offset, entry) for offset, entry in enumerate(tail)
                     if isinstance(entry, ContractRead) and entry.at < phase.end
                     and any(entry.path == path or entry.path.endswith("/" + path) for path in paths)]
        if not following:
            contexts.append(NoReload("no contract Read after compact_boundary"))
            continue
        offset, read = following[0]
        results = [(position, entry) for position, entry in enumerate(tail[offset + 1:], offset + 1)
                   if isinstance(entry, ReadResult) and entry.tool_id == read.tool_id and entry.at < phase.end]
        if not results or results[0][1].failed:
            contexts.append(NoReload("first contract Read has no successful tool result"))
            continue
        position, _ = results[0]
        requests = [entry for entry in tail[position + 1:] if isinstance(entry, ContextRequest)
                    and entry.request != read.request and entry.at < phase.end]
        contexts.append(requests[0].raw_input if requests else
                        NotRecorded("director transcript", "no request after contract Read result"))
    return PhaseReloads(len(boundaries), tuple(contexts))


def measure_trial(events: Sequence[Event], trials: Sequence[TrialPhase], turns: tuple[UsageTurn, ...] | NotRecorded,
                  transcripts: dict[DirectorSession, DirectorTranscript | NotRecorded]) -> tuple[TrialMeasures, ...]:
    directors = run_directors(events)
    result: list[TrialMeasures] = []
    for trial in trials:
        mapped = directors.get(trial.phase.run, frozenset())
        if len(mapped) != 1:
            absent = NotRecorded("run_started.main_agent", "phase has no single mapped director")
            result.append(TrialMeasures(trial, absent, absent))
            continue
        director = next(iter(mapped))
        transcript = transcripts.get(director, NotRecorded("director transcript", "transcript not read"))
        result.append(TrialMeasures(trial, trial_tokens(trial, director, turns),
                                    measure_reloads(trial.phase, transcript, reload_paths(events, trial.phase, trial.exposure))))
    return tuple(result)


def filter_units(events: Sequence[Event], units: frozenset[str]) -> tuple[Event, ...]:
    if not units:
        return tuple(events)
    runs = {event.run for event in events if event.kind == "run_started"
            and units.intersection((text_field(event.record, "unit"), text_field(event.record, "worktree"),
                                    event.branch, Path(event.directory).name))}
    return tuple(event for event in events if event.run in runs)


def measure_contract(files: Sequence[tuple[str, bytes]]) -> tuple[ContractFile, ...]:
    """Follow direct full reads, then one level from files read during a phase.

    Cadence is a minimum read per phase, not the number of repeated reads.
    Briefs and reports are mode-dependent and are counted only on request.
    """
    import re

    contents = dict(files)
    direct = contents.get("commands/unit/direct.md", b"").decode("utf-8")
    required = {"commands/unit/direct.md": "per phase"}
    cadence_by_file = {
        "docs/direct/write_prompt_contract.md": "per dispatch",
        "docs/direct/dual_review.md": "per dispatch",
        "docs/direct/final_gate_commit.md": "final gate only",
        "commands/unit/brief.md": "on request",
        "commands/unit/phase_report.md": "on request",
    }
    full_read = re.compile(r"read\s+`(?:~/.claude/)?([^`]+\.md)`\s+in full", re.IGNORECASE)
    for match in full_read.finditer(direct):
        name = match.group(1)
        required[name] = cadence_by_file.get(name, "per phase")
    # TagReferenceContract requires the whole tagged contract at its call site.
    # PhaseChecks is a per-phase full read expressed by that table, not a Read sentence.
    if re.search(r"\| <PhaseChecks/> \| `docs/direct/phase_checks.md`", direct):
        required["docs/direct/phase_checks.md"] = "per phase"
    for name, cadence in tuple(required.items()):
        if name == "commands/unit/direct.md" or cadence not in ("per phase", "per dispatch"):
            continue
        for match in full_read.finditer(contents.get(name, b"").decode("utf-8")):
            child = match.group(1)
            if child not in required:
                required[child] = cadence_by_file.get(child, "per phase")
    return tuple(ContractFile(name, len(contents[name].splitlines()), len(contents[name]), cadence)
                 for name, cadence in sorted(required.items()) if name in contents)


def git_output(directory: Path, args: Sequence[str]) -> str:
    result = subprocess.run(["git", "--no-optional-locks", "-C", str(directory), *args],
                            capture_output=True, text=True, timeout=8, check=False)
    return result.stdout if result.returncode == 0 else ""


def read_git_documents(root: Path, revisions: Sequence[DocumentRevision]) -> dict[str, str]:
    """Read all required blobs in one git process, including names before a rename."""
    if not revisions:
        return {}
    requests = "".join(f"{revision.sha}:{revision.path}\n" for revision in revisions)
    result = subprocess.run(["git", "--no-optional-locks", "-C", str(root), "cat-file", "--batch"],
                            input=requests.encode(), capture_output=True, timeout=8, check=False)
    documents: dict[str, str] = {}
    offset = 0
    for revision in revisions:
        end = result.stdout.find(b"\n", offset)
        if end < 0:
            break
        header = result.stdout[offset:end].split()
        offset = end + 1
        if len(header) != 3 or header[1] != b"blob" or not header[2].isdigit():
            continue
        length = int(header[2])
        documents[revision.sha] = result.stdout[offset:offset + length].decode("utf-8", errors="replace")
        offset += length + 1
    return documents


@cache
def document_repository(path: Path) -> Path | NotRecorded:
    """Find the document's own repository, even if its directory was removed."""
    parent = path.parent
    while not parent.is_dir() and parent != parent.parent:
        parent = parent.parent
    repository = git_output(parent, ["rev-parse", "--show-toplevel"]).strip()
    return Path(repository) if repository else NotRecorded(str(path), "document repository is absent")


@cache
def doc_commits(directory: str, path: str, since: float = -math.inf,
                until: float = math.inf, revision: str = "--all") -> tuple[DocCommit, ...]:
    """Follow the document on all refs; each SHA is one observation, even after merge."""
    document = Path(path).expanduser()
    document = (document if document.is_absolute() else Path(directory) / document).resolve()
    root = document_repository(document)
    if isinstance(root, NotRecorded):
        return ()
    path = str(document.relative_to(root))
    log = git_output(root, ["log", revision, "--follow", "--name-status", "--format=%x1e%H%x09%ct%x09%s", "--", path])
    entries: list[DocumentRevision] = []
    for block in log.split("\x1e"):
        lines = block.strip().splitlines()
        if not lines:
            continue
        parts = lines[0].split("\t", 2)
        if len(parts) != 3 or not parts[1].isdigit():
            continue
        sha, epoch, message = parts
        names = [line.split("\t") for line in lines[1:] if "\t" in line]
        name = names[0][-1] if names else path
        entries.append(DocumentRevision(sha, float(epoch), message, name))
    entries.reverse()
    entries.sort(key=lambda entry: entry.at)
    before = [entry.sha for entry in entries if entry.at < since]
    baseline = before[-1] if before else ""
    first = entries[0].sha if entries else ""
    required = [entry for entry in entries if entry.at < until and (entry.sha in (first, baseline) or entry.at >= since)]
    documents = read_git_documents(root, required)
    commits: list[DocCommit] = []
    for entry in entries:
        # Only the initial doc, the run-start snapshot and later changes need
        # full text. Old intermediate text cannot change any measure.
        if entry.at >= until:
            continue
        commits.append(DocCommit(entry.sha, entry.at, entry.message, documents.get(entry.sha, "")))
    return tuple({commit.sha: commit for commit in commits}.values())


def source_paths(text: str) -> tuple[str, ...]:
    """Only header and Source references establish document lineage."""
    return tuple(sorted({reference.path for reference in source_references(text)}))


def source_references(text: str) -> tuple[SourceReference, ...]:
    """Markdown targets use the plan directory; bare docs/issues paths use the repo."""
    import re

    first_section = re.search(r"^## ", text, re.MULTILINE)
    prefix = text[:first_section.start()] if first_section else text
    header = "\n".join(line for line in prefix.splitlines()
                       if re.search(r"source|based on|derived from", line, re.IGNORECASE))
    sources = re.findall(r"^## Source[^\n]*\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    evidence = header + "\n" + "\n".join(sources)
    links: list[SourceReference] = []
    for match in re.finditer(r"\[[^\]\n]*\]\(\s*<?([^\s<>]+?\.md)(?:#[^\s<>]*)?>?(?:\s+\"[^\"]*\")?\s*\)", evidence):
        target = match.group(1)
        if "://" not in target:
            links.append(SourceReference(target, True))
    # Do not also read a link's label or target as a bare repository path.
    evidence = re.sub(r"\[[^\]\n]*\]\([^\n)]*\)", "", evidence)
    references = cast(list[tuple[str, str]], re.findall(
        r"`([^`\n]+\.md)`|(?<![\w/])((?:~?/|docs/|issues/)[^\s`<>),;]+\.md)",
        evidence))
    bare = [SourceReference(quoted or plain, not (quoted or plain).startswith(("docs/", "issues/")))
            for quoted, plain in references]
    return tuple(sorted(set((*links, *bare)), key=lambda reference: (reference.path, reference.relative_to_plan)))


def source_history(root: Path, source: str, until: float) -> tuple[DocCommit, ...]:
    return doc_commits(str(root), source, math.inf, until)


def load_plan_histories(events: Sequence[Event], window: Window) -> tuple[PlanHistory, ...]:
    groups: dict[str, list[Event]] = defaultdict(list)
    for event in events:
        if event.kind == "run_started" and event.plan and event.directory:
            path = Path(event.plan).expanduser()
            resolved = path if path.is_absolute() else Path(event.directory) / path
            groups[str(resolved)].append(event)
    def load_plan(item: tuple[str, list[Event]]) -> PlanHistory | NotPhasedPlan:
        absolute, runs = item
        # Include a plan if a run starts in the window, but keep its first run
        # ever. A resume must not move the first-dispatch milestone forward.
        if not any(window.contains(run.at) for run in runs):
            return NotPhasedPlan(absolute)
        first = min(runs, key=lambda run: run.at)
        run_ids = tuple(run.run for run in runs)
        phase_starts = [event.at for event in events if event.run in run_ids and event.kind == "phase_started"]
        # The run directory can belong to a different repository from its plan.
        plan_path = Path(absolute).resolve()
        problem = ""
        churn_problem = ""
        try:
            repository = document_repository(plan_path)
            root = repository if isinstance(repository, Path) else plan_path.parent
            relative = str(plan_path.relative_to(root))
            revision = f"refs/heads/{first.branch}"
            if not root.is_dir() or not first.branch or not git_output(root, ["rev-parse", "--verify", revision]):
                revision = "--all"
                churn_problem = "not recorded: run branch is absent; all-ref history cannot establish churn order"
            commits = doc_commits(str(root), relative, first.at, window.until, revision)
            if not commits:
                problem = "not recorded: document git history is absent"
            current = plan_path.read_text(encoding="utf-8") if plan_path.is_file() else ""
            # A plain run without a phased plan is not a plan sample.
            evidence = current or (commits[-1].text if commits else "")
            if evidence and not plan_history.PHASE_HEADING_PATTERN.search(evidence):
                return NotPhasedPlan(absolute)
            lineage: set[str] = set()
            for version in ((commits[0].text if commits else ""), current):
                for reference in source_references(version):
                    source_path = Path(reference.path).expanduser()
                    if not source_path.is_absolute():
                        source_path = (plan_path.parent if reference.relative_to_plan else root) / source_path
                    lineage.add(str(source_path.resolve()))
            sources = tuple((source, source_history(root, source, window.until))
                            for source in sorted(lineage) if source != str(plan_path))
        except (OSError, subprocess.TimeoutExpired) as error:
            commits, sources = (), ()
            problem = f"not recorded: {error}"
        return PlanHistory(absolute, run_ids, first.at,
                           min(phase_starts) if phase_starts else NotRecorded("phase_started", "no dispatch"),
                           commits, sources, problem, churn_problem)
    # Repositories are independent read sources. Bound concurrent git readers;
    # hundreds of serial process launches exceeded the baseline's time budget.
    with ThreadPoolExecutor(max_workers=12) as readers:
        loaded = readers.map(load_plan, sorted(groups.items()))
        return tuple(plan for plan in loaded if isinstance(plan, PlanHistory))


def measure_doc_pipeline(plans: Sequence[PlanHistory]) -> tuple[DocMilestone, ...]:
    import re

    result: list[DocMilestone] = []
    for plan in plans:
        for path, commits in ((plan.path, plan.commits), *plan.sources):
            first: DocCommit | NotRecorded = commits[0] if commits else NotRecorded("git log --all --follow", "no commit")
            evidence = "\n".join(commit.message for commit in commits)
            if commits:
                evidence += "\n" + commits[0].text.split("## Delegation Context", 1)[0]
            commands = sorted(set(re.findall(r"/(?:(?:plan|producer|showrunner|unit):[a-z_]+|team_review|adversarial|api_review|module_review|astra_review|adhoc_review|issue)\b", evidence)))
            result.append(DocMilestone(plan.path, path, first, plan.started, plan.first_phase,
                                       ", ".join(commands) or "not recorded (git messages/doc header)", plan.problem))
    return tuple(result)


def todo_sections(text: str) -> dict[str, str]:
    headings = list(plan_history.PHASE_HEADING_PATTERN.finditer(text))
    sections: dict[str, str] = {}
    for index, heading in enumerate(headings):
        if "status: todo" in heading.group("rest"):
            end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            sections[heading.group("id")] = text[heading.start():end]
    return sections


def pending_decisions(text: str) -> set[str]:
    import re

    return set(re.findall(r"^\*\*Pending decision:\s*(.*?)\*\*\s*$", text, re.MULTILINE))


def measure_plan_churn(plans: Sequence[PlanHistory], events: Sequence[Event], window: Window) -> tuple[PlanChurn, ...]:
    import re

    results: list[PlanChurn] = []
    finishes = {(event.run, event.phase): event.at for event in events if event.kind == "phase_finished"}
    phase_windows = [(event.run, event.at, finishes.get((event.run, event.phase), math.inf))
                     for event in events if event.kind == "phase_started"]
    for plan in plans:
        todo_edits = opened = closed = n = 0
        inserted: set[str] = set()
        splits: list[str] = []
        previous = ""
        baseline_known = any(commit.at < plan.started and commit.text for commit in plan.commits)
        for commit in plan.commits:
            if plan.started <= commit.at < window.until:
                n += 1
                old_sections, new_sections = todo_sections(previous), todo_sections(commit.text)
                # A commit between phase windows can amend a future Work Order.
                active = any(run in plan.runs and start <= commit.at < end for run, start, end in phase_windows)
                if not active and any(old_sections[key] != new_sections[key] for key in old_sections.keys() & new_sections.keys()):
                    todo_edits += 1
                before, after = pending_decisions(previous), pending_decisions(commit.text)
                opened += len(after - before)
                closed += len(before - after)
                old_ids = {match.group("id") for match in plan_history.PHASE_HEADING_PATTERN.finditer(previous)}
                new_ids = {match.group("id") for match in plan_history.PHASE_HEADING_PATTERN.finditer(commit.text)}
                inserted.update(new_ids - old_ids)
                if re.search(r"\bsplit\b.*\bphase|\bphase\b.*\bsplit\b", commit.message, re.IGNORECASE):
                    splits.append(f"{commit.sha[:8]}: {commit.message}")
            previous = commit.text
        results.append(PlanChurn(plan.path, n, todo_edits, opened, closed, tuple(sorted(inserted)),
                                 "; ".join(splits) or "not recorded (git messages do not name a phase split)",
                                 plan.problem or plan.churn_problem or
                                 ("" if baseline_known else "not recorded: no committed snapshot at run start")))
    return tuple(results)


def duration(seconds: float) -> str:
    """Use seconds through 1,000 s, minutes below 2 h, then hours."""
    if abs(seconds) >= 7200:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 60:.1f} min" if abs(seconds) > 1000 else f"{seconds:.1f}s"


def missing(value: NotRecorded) -> str:
    return f"not recorded ({value.source}: {value.reason})"


def share(value: float | NotRecorded) -> str:
    return missing(value) if isinstance(value, NotRecorded) else f"{value:.1%}"


DETAIL_TABLES = ("phase-steps", "director-tokens", "contract-load", "doc-pipeline", "plan-churn", "trial")


@dataclass(frozen=True)
class RankedItem:
    label: str
    n: int
    values: tuple[float | NotRecorded, ...]


def top_items(items: Sequence[RankedItem]) -> tuple[RankedItem, ...]:
    """Rank by the first total; retain unknown counts outside numeric columns."""
    ranked = sorted(items, key=lambda item: item.values[0] if isinstance(item.values[0], float) else -math.inf,
                    reverse=True)
    if len(ranked) <= 10:
        return tuple(ranked)
    rest = ranked[10:]
    totals: list[float | NotRecorded] = []
    for index in range(len(rest[0].values)):
        known = [item.values[index] for item in rest if isinstance(item.values[index], float)]
        totals.append(sum(cast(list[float], known)) if known else NotRecorded("source records", "no values"))
    n = sum(item.n for item in rest)
    return (*ranked[:10], RankedItem(f"other (n={n})", n, tuple(totals)))


def count(value: float) -> str:
    return f"{value:,.0f}" if value.is_integer() else f"{value:,.1f}"


def distribution_row(name: str, values: Sequence[float], absent: int,
                     formatter: Callable[[float], str]) -> tuple[str, ...]:
    if not values:
        return (name, "0", "—", "—", "—", str(absent))
    summary = summarize(name, values, 0)
    return (name, str(summary.n), formatter(summary.median), formatter(summary.p90),
            formatter(summary.total), str(absent))


def trial_count(value: float) -> str:
    if value < 1_000:
        return count(value)
    for scale, suffix, precision in ((1_000, "K", 0), (1_000_000, "M", 1),
                                     (1_000_000_000, "B", 1)):
        scaled = value / scale
        if round(scaled, precision) < 1_000:
            return f"{scaled:.{precision}f}{suffix}"
    return f"{value / 1_000_000_000_000:.1f}T"


def render_trial_measures(measures: Sequence[tuple[str, Sequence[float], int]]) -> None:
    # The report already states the window. Keep these headings on the phone.
    print("\nTrial measures — eligible phases, natedev only")
    print("Director tokens: phase plus gap; compactions: phase")
    columns = ("measure", "n phases", "median", "p90", "total")
    rows = [distribution_row(name, values, absent, trial_count)[:-1] for name, values, absent in measures]
    widths = [max(len(row[index]) for row in [columns, *rows]) for index in range(len(columns))]
    for row in [columns, *rows]:
        print("  ".join(cell.ljust(width) if index == 0 else cell.rjust(width)
                        for index, (cell, width) in enumerate(zip(row, widths))))
    for name, _, absent in measures:
        if absent:
            print(f"not recorded: {name} n={absent}")


def item_rows(items: Sequence[RankedItem], formatters: Sequence[Callable[[float], str]]) -> list[tuple[str, ...]]:
    return [(item.label, str(item.n), *("not recorded" if isinstance(value, NotRecorded) else formatter(value)
                                      for value, formatter in zip(item.values, formatters))) for item in items]


def table(title: str, columns: Sequence[str], rows: Sequence[Sequence[str]], window: Window) -> None:
    print(f"\n{title} | {window.label()}")
    widths = [max(len(row[index]) for row in [columns, *rows]) for index in range(len(columns))]
    for row in [columns, *rows]:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())
    if not rows:
        print("not recorded; n=0")


def compact_steps(events: Sequence[Event], phases: Sequence[PhaseBoundary], steps: PhaseSteps) -> tuple[StepCost, ...]:
    """Merge small activity labels using per-phase allocated seconds, before quantiles."""
    ranked = sorted(steps.costs, key=lambda cost: cost.total, reverse=True)
    kept = [cost for cost in ranked if not cost.step.startswith("activity:")]
    activities = [cost for cost in ranked if cost.step.startswith("activity:")]
    tail: set[str] = set()
    for cost in activities:
        if cost.wall_share < .001 or len(kept) >= 10:
            tail.add(cost.step)
        else:
            kept.append(cost)
    if tail:
        # Allocation precedes merging: merging concurrent labels first would change their shares.
        samples = [sum(value for step, value in totals.items() if step in tail)
                   for totals in allocated_step_seconds(events, phases) if tail.intersection(totals)]
        kept.append(summarize(f"activity: other ({len(tail)} labels)", samples, steps.wall))
    return tuple(sorted(kept, key=lambda cost: cost.total, reverse=True))


def render_steps(events: Sequence[Event], window: Window, detailed: bool) -> None:
    phases = phase_boundaries(events, window)
    steps = measure_phase_steps(events, phases)
    costs = steps.costs if detailed else compact_steps(events, phases, steps)
    rows = [(cost.step, str(cost.n), duration(cost.median), duration(cost.p90), duration(cost.total),
             f"{cost.wall_share:.1%}") for cost in costs]
    wall = summarize("phase wall", [phase.end - phase.start for phase in phases], steps.wall) if phases else NotRecorded("phase_finished", "no finished phases")
    rows.append(("phase wall", str(steps.phases), "—" if isinstance(wall, NotRecorded) else duration(wall.median),
                 "—" if isinstance(wall, NotRecorded) else duration(wall.p90), duration(steps.wall), "100%"))
    table("Phase steps — natedev only", ("step", "n phases", "median", "p90", "total", "wall share"), rows, window)
    repairs = repaired_phase_costs(phases)
    table("Fix costs — worker time, natedev only; source: history.py rounds",
          ("measure", "n phases", "median", "p90", "total", "not recorded"),
          [distribution_row("fix worker time", [repair.seconds for repair in repairs], 0, duration),
           distribution_row("repair rounds", [float(repair.rounds) for repair in repairs], 0, count)], window)
    print(f"{len(phases) - len(repairs)} of {len(phases)} phases had no repair.")
    states = phase_states(events)
    mixed = sum(isinstance(phase, MixedPhase) and
                (window.contains(phase.finish) if isinstance(phase.finish, float) else phase.start < window.until)
                for phase in states)
    opened = sum(isinstance(phase, OpenPhase) and phase.start < window.until for phase in states)
    print(f"Excluded from per-phase costs: mixed n={mixed} (finished in window or open); open unmixed n={opened} (started before window end).")
    table("Phase end through next Work Order — natedev only", ("step", "n phases", "median", "p90", "total"),
          [(cost.step, str(cost.n), duration(cost.median), duration(cost.p90), duration(cost.total)) for cost in steps.gaps], window)


def render_tokens(tokens: Sequence[PhaseTokens], window: Window, detailed: bool) -> None:
    known = [row for row in tokens if isinstance(row.cost, TokenCost)]
    costs = [row.cost for row in known if isinstance(row.cost, TokenCost)]
    absent = len(tokens) - len(known)
    rows: list[tuple[str, ...]] = []
    if detailed:
        for row in tokens:
            label = f"{row.phase.run}/{row.phase.identifier} ({stamp(row.phase.start)})"
            cost = row.cost
            rows.append((label, "0", "not recorded", "not recorded", missing(cost), "—") if isinstance(cost, NotRecorded)
                        else (label, str(cost.turns), str(cost.input), str(cost.output), share(cost.before_share), share(cost.after_share)))
        table("Director tokens detail — natedev only", ("run/phase (start)", "n turns", "input", "output", "before first pass", "after last pass"), rows, window)
        return
    rows = [distribution_row("input per phase", [float(cost.input) for cost in costs], absent, count),
            distribution_row("output per phase", [float(cost.output) for cost in costs], absent, count)]
    for name, shares in (("before first pass share", [cost.before_share for cost in costs]),
                         ("after last pass share", [cost.after_share for cost in costs])):
        values = [value for value in shares if isinstance(value, float)]
        summary = distribution_row(name, values, len(tokens) - len(values), lambda value: f"{value:.1%}")
        rows.append((*summary[:4], "—", summary[-1]))
    table("Director tokens summary — natedev only", ("measure", "n phases", "median", "p90", "total", "not recorded"), rows, window)
    items = [RankedItem(f"{row.phase.run[:8]}/{row.phase.identifier}@{stamp(row.phase.start)}", 1,
                        (float(row.cost.input), float(row.cost.output), float(row.cost.turns)))
             for row in known if isinstance(row.cost, TokenCost)]
    table("Director tokens: top 10 phases by input — natedev only", ("run/phase@start", "n phases", "input", "output", "n turns"),
          item_rows(top_items(items), (count, count, count)), window)
    sources: dict[str, int] = defaultdict(int)
    for row in tokens:
        if isinstance(row.cost, NotRecorded):
            sources[missing(row.cost)] += 1
    for source, n in sorted(sources.items()):
        print(f"{source}; n phases={n}")


def render_contract(contract: Sequence[ContractFile], window: Window, detailed: bool) -> None:
    per_phase = [row for row in contract if row.cadence in ("per phase", "per dispatch")]
    if not detailed:
        table("Contract size summary — static checkout", ("measure", "n files", "median", "p90", "total", "not recorded"),
              [distribution_row("lines per file", [float(row.lines) for row in contract], 0, count),
               distribution_row("bytes per file", [float(row.bytes) for row in contract], 0, count)], window)
    rows: list[tuple[str, ...]] = []
    ranked = sorted(contract, key=lambda row: row.bytes, reverse=True)
    for row in ranked if detailed else ranked[:10]:
        rows.append((row.path, "1", str(row.lines), str(row.bytes), row.cadence))
    if not detailed and len(ranked) > 10:
        rest = ranked[10:]
        cadences = "; ".join(f"{cadence}: {sum(row.cadence == cadence for row in rest)}" for cadence in sorted({row.cadence for row in rest}))
        rows.append((f"other (n={len(rest)})", str(len(rest)), str(sum(row.lines for row in rest)), str(sum(row.bytes for row in rest)), cadences))
    rows.append(("sum per phase (minimum)", str(len(per_phase)), str(sum(row.lines for row in per_phase)),
                 str(sum(row.bytes for row in per_phase)), "includes one read per dispatch file"))
    table("Contract full reads — static checkout", ("file", "n files", "lines", "bytes", "cadence"), rows, window)
    print("Per-phase sum counts each per-phase/dispatch file once; repeated dispatches add reads. Brief/report reads are on request or mode-dependent.")


@dataclass(frozen=True)
class DocAfterDispatch:
    document: str
    committed_at: float
    dispatched_at: float


@dataclass(frozen=True)
class PlanLeadTime:
    plan: str
    doc_to_run: float | NotRecorded | DocAfterDispatch
    run_to_phase: float | NotRecorded | DocAfterDispatch
    doc_to_phase: float | NotRecorded | DocAfterDispatch


def plan_lead_times(pipeline: Sequence[DocMilestone]) -> tuple[PlanLeadTime, ...]:
    groups: dict[str, list[DocMilestone]] = defaultdict(list)
    for row in pipeline:
        groups[row.plan].append(row)
    results: list[PlanLeadTime] = []
    for plan, milestones in groups.items():
        commits = [(row.first_commit.at, row.document) for row in milestones if isinstance(row.first_commit, DocCommit)]
        first = milestones[0]
        absent = NotRecorded("git log + phase_started", "milestone absent")
        to_run: float | NotRecorded = absent
        lead: float | NotRecorded = absent
        if len(commits) == len(milestones):
            committed_at, document = min(commits)
            if not isinstance(first.phase_started, NotRecorded) and committed_at > first.phase_started:
                late = DocAfterDispatch(document, committed_at, first.phase_started)
                results.append(PlanLeadTime(plan, late, late, late))
                continue
            to_run = float(first.run_started - committed_at)
            if not isinstance(first.phase_started, NotRecorded):
                lead = float(first.phase_started - committed_at)
        to_phase = float(first.phase_started - first.run_started) if not isinstance(first.phase_started, NotRecorded) else absent
        results.append(PlanLeadTime(plan, to_run, to_phase, lead))
    return tuple(results)


def render_pipeline(pipeline: Sequence[DocMilestone], window: Window, detailed: bool) -> None:
    rows: list[tuple[str, ...]] = []
    if detailed:
        for row in pipeline:
            commit = row.first_commit
            first = missing(commit) if isinstance(commit, NotRecorded) else f"{stamp(commit.at)} {commit.sha[:8]}"
            phase = missing(row.phase_started) if isinstance(row.phase_started, NotRecorded) else stamp(row.phase_started)
            to_run = "not recorded" if isinstance(commit, NotRecorded) else duration(row.run_started - commit.at)
            to_phase = "not recorded" if isinstance(row.phase_started, NotRecorded) else duration(row.phase_started - row.run_started)
            lead = "not recorded" if isinstance(commit, NotRecorded) or isinstance(row.phase_started, NotRecorded) else duration(row.phase_started - commit.at)
            state = ("doc after dispatch" if isinstance(commit, DocCommit)
                     and isinstance(row.phase_started, float | int) and commit.at > row.phase_started
                     else "not recorded" if isinstance(commit, NotRecorded) else "recorded")
            rows.append((state, row.plan, row.document, "0" if isinstance(commit, NotRecorded) else "1", first,
                         stamp(row.run_started), phase, to_run, to_phase, lead, row.commands, row.problem))
        table("Doc pipeline detail — natedev only", ("state", "plan", "document", "n commits", "first commit", "first run", "first phase", "doc to run", "run to phase", "doc to phase", "commands named", "source limit"), rows, window)
        return
    leads = plan_lead_times(pipeline)
    late_count = sum(isinstance(row.doc_to_phase, DocAfterDispatch) for row in leads)
    for name, samples in (("first doc to run", [row.doc_to_run for row in leads]),
                          ("run to first phase", [row.run_to_phase for row in leads]),
                          ("first doc to first phase", [row.doc_to_phase for row in leads])):
        values = [value for value in samples if isinstance(value, float)]
        rows.append(distribution_row(name, values, len(leads) - len(values) - late_count, duration))
    table("Doc pipeline summary — natedev only", ("measure", "n plans", "median", "p90", "total", "not recorded"), rows, window)
    items = [RankedItem(Path(row.plan).name, 1, (row.doc_to_phase, row.doc_to_run, row.run_to_phase)) for row in leads
             if not isinstance(row.doc_to_phase, DocAfterDispatch)
             and not isinstance(row.doc_to_run, DocAfterDispatch)
             and not isinstance(row.run_to_phase, DocAfterDispatch)]
    print(f"doc after dispatch: n plans={late_count}; n documents={sum(isinstance(row.first_commit, DocCommit) and isinstance(row.phase_started, float | int) and row.first_commit.at > row.phase_started for row in pipeline)}; negative-lead plans excluded from all lead distributions, totals and ranking; see --detail doc-pipeline.")
    table("Doc pipeline: top 10 plans by lead time — natedev only", ("plan", "n plans", "doc to phase", "doc to run", "run to phase"),
          item_rows(top_items(items), (duration, duration, duration)), window)
    print(f"Earliest doc requires all named source commits. Document commits not recorded: n={sum(isinstance(row.first_commit, NotRecorded) for row in pipeline)}/{len(pipeline)} documents; source: git log. Commands and exact milestones: --detail doc-pipeline.")


def churn_totals(row: PlanChurn) -> tuple[float | NotRecorded, ...]:
    import re

    if row.problem:
        return (NotRecorded("plan git history", row.problem),) * 5
    splits: float | NotRecorded = (NotRecorded("git messages", "no phase split named") if row.split.startswith("not recorded")
                                   else float(len(re.findall(r"(?:^|; )[0-9a-f]+: ", row.split))))
    return (float(row.todo_edits), float(row.decisions_opened), float(row.decisions_closed), float(len(row.inserted_phases)), splits)


def render_churn(churn: Sequence[PlanChurn], window: Window, detailed: bool) -> None:
    if detailed:
        table("Plan churn detail — natedev only", ("plan", "n commits", "todo edits", "opened", "closed", "phase IDs added", "phase splits", "source limit"),
              [(row.plan, str(row.commits), "not recorded" if row.problem else str(row.todo_edits),
                "not recorded" if row.problem else str(row.decisions_opened), "not recorded" if row.problem else str(row.decisions_closed),
                "not recorded" if row.problem else (", ".join(row.inserted_phases) or "none"), row.split, row.problem) for row in churn], window)
        return
    totals = [churn_totals(row) for row in churn]
    names = ("Work Order edits", "decisions opened", "decisions closed", "phases inserted", "named phase splits")
    rows: list[tuple[str, ...]] = []
    for index, name in enumerate(names):
        values = [values[index] for values in totals if isinstance(values[index], float)]
        rows.append(distribution_row(name, cast(list[float], values), len(churn) - len(values), count))
    table("Plan churn summary — natedev only; first run through window end", ("measure", "n plans", "median", "p90", "total", "not recorded"), rows, window)
    items = [RankedItem(Path(row.plan).name, 1,
                        (sum(value for value in values if isinstance(value, float)), *values)) for row, values in zip(churn, totals) if not row.problem]
    table("Plan churn: top 10 plans by total changes — natedev only", ("plan", "n plans", "total", "todo", "opened", "closed", "inserted", "splits"),
          item_rows(top_items(items), (count,) * 6), window)
    print("Split counts use named git messages; absence is not recorded. Other totals sum known values only; unknown counts are above.")


def render_phase_summary(events: Sequence[Event], phases: Sequence[PhaseBoundary], window: Window) -> None:
    print(f"The window {window.label()} has {len(phases)} completed phases.")
    steps = measure_phase_steps(events, phases)
    if steps.costs:
        largest = max(steps.costs, key=lambda cost: cost.total)
        print(f"The largest step is {largest.step}, at {duration(largest.total)} across {largest.n} phases.")
    else:
        print("The largest step is not recorded.")
    repairs = repaired_phase_costs(phases)
    if repairs:
        median = statistics.median(repair.seconds for repair in repairs)
        print(f"Median repair time is {duration(median)} across {len(repairs)} repaired phases.")
    else:
        print("No phases had a repair; median repair time is not recorded.")


def render_baseline(events: Sequence[Event], window: Window, usage: tuple[UsageTurn, ...] | NotRecorded,
                    contract: Sequence[ContractFile], plans: Sequence[PlanHistory], problems: Sequence[str]) -> None:
    phases = phase_boundaries(events, window)
    render_phase_summary(events, phases, window)
    print("Phase process baseline — natedev only; no Mac records read. Use baseline --detail TABLE for full rows.")
    print("n counts phases, plans or files as labeled; p90 is nearest rank. Top lists show 10 plus summed other.")
    print("Distinct overlapping steps share wall time equally; parallel workers of one kind count once. Repair wall coverage is excluded from shares; fix costs are worker time, shown apart.")
    print("Director time includes uncovered waits; unmatched steps have unknown coverage. Activity tails below 0.1% are merged.")
    print("rounds.py owns phase boundaries; mixed phases are excluded. Plans are selected by run start, completed phases by finish.")
    print("Token input includes caches; pass records bound before/after shares of input + output. Shared sessions can overlap; compose before phase start is not isolated.")
    render_steps(events, window, False)
    render_tokens(measure_director_tokens(events, phases, usage), window, False)
    render_contract(contract, window, False)
    render_pipeline(measure_doc_pipeline(plans), window, False)
    render_churn(measure_plan_churn(plans, events, window), window, False)
    print("Churn counts committed snapshots from first run, possibly before this window. Removed pending blocks count as closed.")
    print("Git times are commit times; command names are evidence, not proof of execution. Uncommitted churn is not recorded.")
    print(f"Source read problems: n={len(problems)}; first 3: {'; '.join(problems[:3]) or 'none'}")


def render_detail(name: str, events: Sequence[Event], window: Window, usage: tuple[UsageTurn, ...] | NotRecorded,
                  contract: Sequence[ContractFile], plans: Sequence[PlanHistory]) -> None:
    if name == "phase-steps":
        render_steps(events, window, True)
    elif name == "director-tokens":
        render_tokens(measure_director_tokens(events, phase_boundaries(events, window), usage), window, True)
    elif name == "contract-load":
        render_contract(contract, window, True)
    elif name == "doc-pipeline":
        render_pipeline(measure_doc_pipeline(plans), window, True)
    elif name == "plan-churn":
        render_churn(measure_plan_churn(plans, events, window), window, True)


def date_epoch(value: str) -> float:
    parsed = datetime.fromisoformat(value)
    return (parsed.replace(tzinfo=user_zone()) if parsed.tzinfo is None else parsed).timestamp()


def render_trial_phase_exclusions(events: Sequence[Event], window: Window) -> None:
    states = [phase for phase in phase_states(events) if window.contains(phase.start)]
    mixed = sum(isinstance(phase, MixedPhase) for phase in states)
    opened = sum(isinstance(phase, OpenPhase) for phase in states)
    print(f"Trial exclusions before exposure filter, selected units and phase starts in {window.label()}: "
          + f"mixed n={mixed}; open unmixed n={opened}.")


def render_trial(trials: Sequence[TrialPhase], measures: Sequence[TrialMeasures], window: Window,
                 exposure_filter: str = "", detailed: bool = False) -> None:
    exposure_counts: dict[str, int] = defaultdict(int)
    exclusions: dict[str, int] = defaultdict(int)
    for trial in trials:
        state = trial.exposure
        exposure_counts[type(state).__name__] += 1
        if isinstance(state, ExposureNotRecorded):
            exclusions[f"ExposureNotRecorded: {state.reason}"] += 1
        interval = trial.interval
        if not isinstance(interval, PhaseWithGap | ExposureNotRecorded):
            label = type(interval).__name__ + (f": {interval.reason}" if isinstance(interval, NotRecorded) else "")
            exclusions[label] += 1
        elif isinstance(interval, ExposureNotRecorded) and interval != state:
            exclusions[f"ExposureNotRecorded: {interval.reason}"] += 1
    rows = [row for row in measures if isinstance(row.trial.interval, PhaseWithGap)
            and (not exposure_filter or exposure_filter == "before" and isinstance(row.trial.exposure, BeforeChange)
                 or exposure_filter == "after" and isinstance(row.trial.exposure, AfterChange))]
    tokens = [row.tokens for row in rows if isinstance(row.tokens, TrialTokens)]
    weighted = [cost.weighted for cost in tokens if isinstance(cost.weighted, float)]
    reloads = [row.reloads for row in rows if isinstance(row.reloads, PhaseReloads)]
    contexts = [context for reload in reloads if isinstance(reload.contexts, tuple) for context in reload.contexts]
    sizes = [float(context) for context in contexts if isinstance(context, int)]
    print(f"Trial exposure filter: {exposure_filter or 'both'}; select phase starts in {window.label()}; phase-plus-gap end can follow window end.")
    print("Exposure: " + "; ".join(f"{name} n={exposure_counts[name]}" for name in
                                      ("BeforeChange", "AfterChange", "ExposureNotRecorded")))
    for name in ("NoFollowingPhase", "SharedDirector"):
        _ = exclusions.setdefault(name, 0)
    print("Phase-plus-gap exclusions (states can overlap): " + "; ".join(f"{name} n={n}" for name, n in sorted(exclusions.items())))
    render_trial_measures([
        ("weighted tokens", weighted, len(rows) - len(weighted)),
        ("raw input with caches", [float(cost.raw_input) for cost in tokens], len(rows) - len(tokens)),
        ("raw output", [float(cost.output) for cost in tokens], len(rows) - len(tokens)),
        ("compactions", [float(reload.compactions) for reload in reloads], len(rows) - len(reloads)),
    ])
    table("Context size after reload — raw input including caches", ("measure", "n reloads", "median", "p90", "total", "not recorded"),
          [distribution_row("first request after contract Read result", sizes, len(contexts) - len(sizes), count)], window)
    print(f"Reload states: NoCompaction n={sum(isinstance(reload.contexts, NoCompaction) for reload in reloads)} phases; "
          + f"NoReload n={sum(isinstance(context, NoReload) for context in contexts)} compactions; "
          + f"NotRecorded n={sum(isinstance(row.reloads, NotRecorded) for row in rows)} phases, "
          + f"n={sum(isinstance(context, NotRecorded) for context in contexts)} requests.")
    print(f"Weighted states: Unweighted n={sum(isinstance(cost.weighted, Unweighted) for cost in tokens)} phases; "
          + f"NotRecorded n={len(rows) - len(tokens)} phases.")
    unknown: dict[str, int] = defaultdict(int)
    for row in rows:
        if isinstance(row.tokens, NotRecorded):
            unknown[f"tokens NotRecorded: {row.tokens.reason}"] += 1
        elif isinstance(row.tokens.weighted, Unweighted):
            unknown["Unweighted: " + "; ".join(row.tokens.weighted.reasons)] += 1
        if isinstance(row.reloads, NotRecorded):
            unknown[f"transcript NotRecorded: {row.reloads.reason}"] += 1
        elif isinstance(row.reloads.contexts, tuple):
            for context in row.reloads.contexts:
                if isinstance(context, NoReload | NotRecorded):
                    unknown[f"{type(context).__name__}: {context.reason}"] += 1
    for label, n in sorted(unknown.items()):
        print(f"{label}; n={n}")
    if detailed:
        table("Trial phase detail", ("run/phase", "exposure", "interval", "weighted", "raw input", "output", "compactions", "reload inputs/states"),
              [(f"{row.trial.phase.run}/{row.trial.phase.identifier}", type(row.trial.exposure).__name__,
                type(row.trial.interval).__name__, str(row.tokens.weighted) if isinstance(row.tokens, TrialTokens) else missing(row.tokens),
                str(row.tokens.raw_input) if isinstance(row.tokens, TrialTokens) else "—",
                str(row.tokens.output) if isinstance(row.tokens, TrialTokens) else "—",
                str(row.reloads.compactions) if isinstance(row.reloads, PhaseReloads) else missing(row.reloads),
                str(row.reloads.contexts) if isinstance(row.reloads, PhaseReloads) else "—") for row in rows], window)


def render_trial_phase_time(events: Sequence[Event], trials: Sequence[TrialPhase], window: Window) -> None:
    """Use the eligible trial sample even when its usage or transcript is missing."""
    units = {event.run: str(event.record.get("unit") or event.record.get("worktree") or
                            event.branch or event.run) for event in events if event.kind == "run_started"}
    seconds: dict[str, list[float]] = defaultdict(list)
    for trial in trials:
        if isinstance(trial.interval, PhaseWithGap):
            seconds[units.get(trial.phase.run, trial.phase.run)].append(trial.phase.end - trial.phase.start)
    table("Trial phase time per unit — eligible start-selected phases, natedev only",
          ("unit", "n phases", "median", "p90", "total", "not recorded"),
          [distribution_row(unit, values, 0, duration) for unit, values in sorted(seconds.items())], window)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=("baseline",))
    _ = parser.add_argument("--since", type=date_epoch)
    _ = parser.add_argument("--until", type=date_epoch)
    _ = parser.add_argument("--route-revision", default="", help="commit which first installs the short direct.md route; names the after side and, without --before-at, its parent history as before")
    _ = parser.add_argument("--before-at", default="", help="commit whose direct.md history names the before side, usable before the route exists")
    _ = parser.add_argument("--units", default="", help="comma-separated recorded unit, worktree or branch names")
    _ = parser.add_argument("--exposure", choices=("before", "after"), default="", help="select trial measures; exclusion counts and the other baseline tables keep all selected units")
    parser.add_argument("--detail", choices=DETAIL_TABLES, help="print full rows for one table only")  # pyright: ignore[reportUnusedCallResult]
    args = parser.parse_args()
    until = cast(float | None, args.until)
    since = cast(float | None, args.since)
    end = until if until is not None else datetime.now(UTC).timestamp()
    window = Window(since if since is not None else end - timedelta(days=14).total_seconds(), end)
    if window.since >= window.until:
        parser.error("--since must be before --until")
    all_events, problems = read_events((plan_history.history_root() / "runs").glob("*.jsonl"))
    units = frozenset(unit.strip() for unit in cast(str, args.units).split(",") if unit.strip())
    events = filter_units(all_events, units)
    route = read_exposure_hashes(Path.home() / ".claude", cast(str, args.route_revision), cast(str, args.before_at))
    selected_run_ids = {event.run for event in events}
    trials = tuple(trial for trial in select_trial_phases(all_events, window, route) if trial.phase.run in selected_run_ids)
    exposure_filter = cast(str, args.exposure)
    eligible = tuple(trial for trial in trials if isinstance(trial.interval, PhaseWithGap)
                     and (not exposure_filter or exposure_filter == "before" and isinstance(trial.exposure, BeforeChange)
                          or exposure_filter == "after" and isinstance(trial.exposure, AfterChange)))
    phases = phase_boundaries(events, window)
    usage_window = Window(min([window.since, *(phase.start for phase in phases)]),
                          max([window.until, *(trial.interval.end for trial in eligible if isinstance(trial.interval, PhaseWithGap)),
                               *(phase.end for phase in phases)]))
    selected_runs = {phase.run for phase in phases} | {trial.phase.run for trial in eligible}
    directors = tuple({event.director for event in events if event.kind == "run_started" and
                       event.run in selected_runs and isinstance(event.director, DirectorSession)})
    usage = (read_usage(collect.store_path(), usage_window, directors) if directors else
             NotRecorded("run_started.main_agent.session_id", "no selected phase has a mapped director"))
    trial_runs = {trial.phase.run for trial in eligible}
    trial_directors = tuple({event.director for event in all_events if event.kind == "run_started"
                             and event.run in trial_runs and isinstance(event.director, DirectorSession)})
    measures = measure_trial(all_events, eligible, usage, read_director_transcripts(Path.home(), trial_directors))
    root = Path(__file__).resolve().parents[2]
    paths = [*sorted((root / "commands/unit").glob("*.md")),
             *sorted((root / "commands/plan").glob("*.md")),
             *sorted((root / "docs/direct").glob("*.md"))]
    contract = measure_contract([(str(path.relative_to(root)), path.read_bytes()) for path in paths])
    plans = load_plan_histories(events, window)
    detail = cast(str | None, args.detail)
    if detail is None:
        render_baseline(events, window, usage, contract, plans, problems)
        render_trial_phase_exclusions(events, window)
        render_trial(trials, measures, window, exposure_filter)
        render_trial_phase_time(events, eligible, window)
    elif detail == "trial":
        render_trial_phase_exclusions(events, window)
        render_trial(trials, measures, window, exposure_filter, True)
        render_trial_phase_time(events, eligible, window)
    else:
        render_detail(detail, events, window, usage, contract, plans)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
