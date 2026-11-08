#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Expected process-memory peaks and atomic build admission."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import errno
from contextlib import closing
import importlib
import json
import math
import mmap
import os
import subprocess
import sys
import uuid
from collections.abc import Generator, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

from typing import BinaryIO, Literal, Protocol, TypedDict, cast
from itertools import chain

from scripts.build_queue import admission_records as records, progress_profile as progress, snapshot_codec as codec
from scripts.build_queue.scope_activity import (EmptyBuildScope, IdleService, LiveBuildRun, NoLiveBuildRun,
    NotServiceSlice, RunningServiceJob, ScopeActivityReader, ScopeProcesses)


from scripts.lint.build_shape import (BuildShape, recorded_build_shape)
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib import conf

from scripts.lib.test_phase import (
    CallRecord as CallRecord,
    NoCallRecord as NoCallRecord,
    TestPhaseChargeUse as TestPhaseChargeUse,
    NoTestPhaseChargeUse as NoTestPhaseChargeUse,
    parse_test_phase_charge_use,
    ObservedTestPhase,
    TestPhaseRecord,
    UnobservedTestPhase,
)

from scripts.buildlog import record, repository as repository, store


from scripts.build_queue.admission_records import (
    AdmissionDecision as AdmissionDecision,
    AdmissionPolicy as AdmissionPolicy,
    AdmissionTimeNotRecorded as AdmissionTimeNotRecorded,
    AdmissionTiming as AdmissionTiming,
    AdmissionWaitSnapshot as AdmissionWaitSnapshot,
    AnonymousRunUse as AnonymousRunUse,
    BUILDLOG_UNIT as BUILDLOG_UNIT,
    BasisNotRecorded as BasisNotRecorded,
    BehindHead as BehindHead,
    BetweenStepsSlot as BetweenStepsSlot,
    BuildHistoryGeneration as BuildHistoryGeneration,
    CIKeepAside as CIKeepAside,
    Call as Call,
    CallerRun as CallerRun,
    CapNotRecorded as CapNotRecorded,
    CapUnavailable as CapUnavailable,
    CappedAtBudget as CappedAtBudget,
    CompilerServiceMemory as CompilerServiceMemory,
    ConfigurationDiagnostic as ConfigurationDiagnostic,
    ContinuationSlot as ContinuationSlot,
    DEV_HEADROOM as DEV_HEADROOM,
    DevSizeHeadroom as DevSizeHeadroom,
    DoesNotFit as DoesNotFit,
    EstimateExpired as EstimateExpired,
    ExpectedPeak as ExpectedPeak,
    ExplainedRunningStep as ExplainedRunningStep,
    FailedPeakFloor as FailedPeakFloor,
    FinalOutcomeName as FinalOutcomeName,
    FinalizationIntent as FinalizationIntent,
    FirstProgramPeak as FirstProgramPeak,
    Fits as Fits,
    FoundRun as FoundRun,
    FreeCheck as FreeCheck,
    FreshTest as FreshTest,
    GIB as GIB,
    GoneMemory as GoneMemory,
    HeadAdmissionOverride as HeadAdmissionOverride,
    HeadAdmissionStatus as HeadAdmissionStatus,
    HeadCause as HeadCause,
    HeadExplanation as HeadExplanation,
    HeadroomRule as HeadroomRule,
    HeldBehindHead as HeldBehindHead,
    HistoryPeakSizing as HistoryPeakSizing,
    HostAvailable as HostAvailable,
    HostMemory as HostMemory,
    INFER_FROM_ARGV as INFER_FROM_ARGV,
    InferFromArgv as InferFromArgv,
    LegacyDevMultiplierHeadroom as LegacyDevMultiplierHeadroom,
    LegacyNeedSource as LegacyNeedSource,
    LifetimePromise as LifetimePromise,
    LineWaiter as LineWaiter,
    MeasuredMemory as MeasuredMemory,
    MeasuredProgramPeak as MeasuredProgramPeak,
    MemoryConfig as MemoryConfig,
    MemoryPressure as MemoryPressure,
    MemoryPromise as MemoryPromise,
    MemoryRoom as MemoryRoom,
    NO_PRESSURE_HOLD as NO_PRESSURE_HOLD,
    NO_QUEUE_PRIORITY as NO_QUEUE_PRIORITY,
    NeedBasis as NeedBasis,
    NeedSource as NeedSource,
    NeedSourceNotRecorded as NeedSourceNotRecorded,
    NeedsMemory as NeedsMemory,
    NextStartAt as NextStartAt,
    NoBuildHistoryGeneration as NoBuildHistoryGeneration,
    NoFloor as NoFloor,
    NoHeadAdmissionOverride as NoHeadAdmissionOverride,
    NoHeadroom as NoHeadroom,
    NoOrderHold as NoOrderHold,
    NoPeakRecorded as NoPeakRecorded,
    NoPressureHold as NoPressureHold,
    NoPromise as NoPromise,
    NoQueuePriority as NoQueuePriority,
    NoSource as NoSource,
    NoTestWidth as NoTestWidth,
    NoWaiter as NoWaiter,
    NormalOrder as NormalOrder,
    OverrideRoom as OverrideRoom,
    PeakDecayed as PeakDecayed,
    PeakFileUnreadable as PeakFileUnreadable,
    PeakHistory as PeakHistory,
    PeakHold as PeakHold,
    PeakSoFar as PeakSoFar,
    PeakTimeUnknown as PeakTimeUnknown,
    PendingPriorityTransition as PendingPriorityTransition,
    PidNotRecorded as PidNotRecorded,
    Pressure as Pressure,
    PressureConditionNotRecorded as PressureConditionNotRecorded,
    PressurePolicy as PressurePolicy,
    PressureUnavailable as PressureUnavailable,
    PriorityUnit as PriorityUnit,
    ProcessAnonymousUse as ProcessAnonymousUse,
    ProcessSource as ProcessSource,
    ProgramProfile as ProgramProfile,
    ProgressAdjustedMemory as ProgressAdjustedMemory,
    PromisedMemory as PromisedMemory,
    QueueExplanation as QueueExplanation,
    QueueInputs as QueueInputs,
    QueueLifeIdentity as QueueLifeIdentity,
    QueueMemoryTotals as QueueMemoryTotals,
    QueuePriority as QueuePriority,
    QueueRead as QueueRead,
    QueueView as QueueView,
    RampPromise as RampPromise,
    RankedWaiter as RankedWaiter,
    ReadingUnavailable as ReadingUnavailable,
    ReadyForAdmission as ReadyForAdmission,
    ReadyToStart as ReadyToStart,
    RecordedNeed as RecordedNeed,
    ReleaseHeadroom as ReleaseHeadroom,
    Repository as Repository,
    RepositoryIdentity as RepositoryIdentity,
    RequestedTestWidth as RequestedTestWidth,
    Reservation as Reservation,
    RoomCheck as RoomCheck,
    RunKeepAside as RunKeepAside,
    RunMemory as RunMemory,
    RunSlot as RunSlot,
    RunUseUnavailable as RunUseUnavailable,
    RunningStep as RunningStep,
    ScopeSource as ScopeSource,
    SettledPriority as SettledPriority,
    ShapeFallback as ShapeFallback,
    ShapeLargest as ShapeLargest,
    ShapeNeedBasis as ShapeNeedBasis,
    ShapeP90 as ShapeP90,
    StartBasis as StartBasis,
    StartSpacing as StartSpacing,
    StartSpacingWait as StartSpacingWait,
    StartsAlone as StartsAlone,
    StepMemory as StepMemory,
    StepProfile as StepProfile,
    TEST_PHASE_MARGIN_FALLBACK as TEST_PHASE_MARGIN_FALLBACK,
    TestPhaseNeed as TestPhaseNeed,
    TestPhaseNeedForm as TestPhaseNeedForm,
    TestPhaseStarted as TestPhaseStarted,
    TestScope as TestScope,
    TestWidth as TestWidth,
    TokenWaitersOnly as TokenWaitersOnly,
    TokenWaitingWaiter as TokenWaitingWaiter,
    UnderBudget as UnderBudget,
    UnknownHostTotal as UnknownHostTotal,
    UnknownRepository as UnknownRepository,
    UnownedSliceMemory as UnownedSliceMemory,
    UnspecifiedTestWidth as UnspecifiedTestWidth,
    WaitReason as WaitReason,
    Waiter as Waiter,
    WaiterIdentity as WaiterIdentity,
    WaitingLine as WaitingLine,
    WaitingStep as WaitingStep,
    WidthNotRecorded as WidthNotRecorded,
    WithinSlot as WithinSlot,
    configuration_diagnostics as configuration_diagnostics,
    head_wait_text as head_wait_text,
    pressure_text as pressure_text,
    process_start_time as process_start_time,
    queue_command_actor as queue_command_actor,
    read_memory_config as read_memory_config,
    run_owner as run_owner,
    stored_waiter as stored_waiter,
    test_threads as test_threads,
    waiter_identity as waiter_identity,
    waiter_owner as waiter_owner,
    waiter_priority as waiter_priority,
    warn_configuration_diagnostics as warn_configuration_diagnostics,
)
from scripts.build_queue.step_estimator import (
    ANON_SHARE as ANON_SHARE,
    BroadTestSelection as BroadTestSelection,
    BuildPeak as BuildPeak,
    Completed as Completed,
    FILTERED_FLOOR as FILTERED_FLOOR,
    Failed as Failed,
    FinalOutcome as FinalOutcome,
    GENERIC_REPO_NAMES as GENERIC_REPO_NAMES,
    HISTORY_DAYS as HISTORY_DAYS,
    HistoryOutcome as HistoryOutcome,
    HistorySelection as HistorySelection,
    Killed as Killed,
    NO_BUILD_SHAPE as NO_BUILD_SHAPE,
    NO_STEP_EVIDENCE as NO_STEP_EVIDENCE,
    OutcomeNotRecorded as OutcomeNotRecorded,
    OwnerGone as OwnerGone,
    PackageTestSelection as PackageTestSelection,
    ReusableEstimate as ReusableEstimate,
    StepEstimate as StepEstimate,
    TEST_EXPRESSION_OPTIONS as TEST_EXPRESSION_OPTIONS,
    TEST_OPTION_VALUES as TEST_OPTION_VALUES,
    TEST_PREDICATE as TEST_PREDICATE,
    VERIFY_FILTER_SELECTION as VERIFY_FILTER_SELECTION,
    VERIFY_NAME_PREDICATE as VERIFY_NAME_PREDICATE,
    VERIFY_NAME_UNION as VERIFY_NAME_UNION,
    VERIFY_PACKAGE_PREDICATE as VERIFY_PACKAGE_PREDICATE,
    VERIFY_PACKAGE_SELECTION as VERIFY_PACKAGE_SELECTION,
    VERIFY_TEST_NAMES as VERIFY_TEST_NAMES,
    anonymous_history_rows as anonymous_history_rows,
    build_expected_peak as build_expected_peak,
    build_peaks as build_peaks,
    compiling_history as compiling_history,
    current_worktree_steps as current_worktree_steps,
    epoch as epoch,
    expected_peak as expected_peak,
    filter_expressions as filter_expressions,
    filter_set as filter_set,
    filtered_peak as filtered_peak,
    fresh_test_peak as fresh_test_peak,
    git_identity as git_identity,
    history_arguments as history_arguments,
    history_matches_repository as history_matches_repository,
    history_outcome as history_outcome,
    history_selection as history_selection,
    is_fresh_test as is_fresh_test,
    memory_config as memory_config,
    no_build_history_generation as no_build_history_generation,
    percentile as percentile,
    program_peak_estimate as program_peak_estimate,
    repository_history as repository_history,
    shape_headroom_bytes as shape_headroom_bytes,
    step_estimate as step_estimate,
    test_filters as test_filters,
    test_package as test_package,
    test_scope as test_scope,
    test_selection as test_selection,
    verify_test_selection as verify_test_selection,
)

MERGE_GATE_TESTS = "tests"
MERGE_GATE_TYPES = "types"


def merge_gate_shape(argv: Sequence[str]) -> str:
    """Separate type-check profiles from test-only profiles."""
    return MERGE_GATE_TYPES if "--type-dirs" in argv else MERGE_GATE_TESTS


PROC = Path("/proc")
# These cargo subcommands compile; arbitrary programs and merge gates run.


HEADROOM_FACTOR_CLASSES: dict[str, type[ReleaseHeadroom | LegacyDevMultiplierHeadroom | DevSizeHeadroom]] = {
    "release": ReleaseHeadroom,
    "dev": LegacyDevMultiplierHeadroom,
    "dev_size": DevSizeHeadroom,
}


@dataclass(frozen=True)
class NoTestPhaseNeed:
    reason: Literal["no_width", "no_history", "not_recorded"]


@dataclass(frozen=True)
class NoTestPhaseFile:
    pass


OUTCOME_NOT_RECORDED = OutcomeNotRecorded()


@dataclass(frozen=True)
class StepEnd:
    pass


@dataclass(frozen=True)
class TokenReturn:
    pass


@dataclass(frozen=True)
class PeakSampled:
    bytes: int


@dataclass(frozen=True)
class NoPositiveSample:
    pass


LeftReason = Literal["cancelled", "owner_gone"]


class LifecycleEventIdentity[Kind: Literal["admitted", "left", "ended"]](QueueLifeIdentity):
    kind: Kind
    event_id: str


class AdmittedEvent(QueueLifeIdentity):
    kind: Literal["admitted"]
    event_id: str
    reservation_id: str
    repo: str
    admitted_at: float
    wait_seconds: float
    need: int
    need_source: NeedSource
    is_head: bool


class LeftEvent(QueueLifeIdentity):
    kind: Literal["left"]
    event_id: str
    ended_at: float
    reason: LeftReason


class EndedEvent(QueueLifeIdentity):
    kind: Literal["ended"]
    event_id: str
    reservation_id: str
    ended_at: float
    outcome: FinalOutcomeName
    end_kind: StepEnd | TokenReturn
    peak: PeakSampled | NoPositiveSample


@dataclass(frozen=True)
class HistoryRecorded:
    pass


@dataclass(frozen=True)
class NoHistoryRecorded:
    pass


def rank_waiter(row: Waiter, order: PriorityUnit | NormalOrder) -> RankedWaiter:
    return {**row, "order": order}


@dataclass(frozen=True)
class QueuedWaiter:
    waiter: Waiter


def waiter_state(row: Waiter) -> QueuedWaiter | TokenWaitingWaiter:
    """Decode the durable token-wait marker at the policy boundary."""
    if "token_left_at" in row and ("token_back_at" not in row or row["token_back_at"] < row["token_left_at"]):
        return TokenWaitingWaiter(row["token_left_at"])
    return QueuedWaiter(row)


def first_queued_waiter(rows: Iterable[Waiter]) -> QueuedWaiter | NoWaiter:
    for row in rows:
        state = waiter_state(row)
        if isinstance(state, QueuedWaiter):
            return state
    return NoWaiter()


def queue_priority_path(directory: Path) -> Path:
    return directory / "queue_priority.json"


def load_queue_priority(directory: Path) -> QueuePriority | NoQueuePriority:
    path = queue_priority_path(directory)
    try:
        value = cast(object, json.loads(path.read_text()))
        if not isinstance(value, dict):
            raise ValueError("priority is not an object")
        row = cast(dict[str, object], value)
        if (any(not isinstance(row.get(key), str) for key in ("unit", "why", "set_by"))
                or any(not isinstance(number := row.get(key), (float, int))
                       or isinstance(number, bool) or not math.isfinite(number) for key in ("set_at",))):
            raise ValueError("invalid priority fields")
        return cast(QueuePriority, codec.parse_snapshot_value(row, QueuePriority))
    except FileNotFoundError:
        return NO_QUEUE_PRIORITY
    except (OSError, ValueError, TypeError, OverflowError):
        print(f"memory admission: invalid queue priority {path}; ignored", file=sys.stderr)
        return NO_QUEUE_PRIORITY


def finish_priority_transition(priority: QueuePriority) -> None:
    transition = priority["transition"]
    if isinstance(transition, SettledPriority):
        return
    record.queue_priority(transition.action, priority["unit"], priority["why"],
                          priority["set_by"], transition.at, transition_id=transition.id, start=transition.month_row_end)


def read_queue_priority(directory: Path, *, finish: bool = False) -> QueuePriority | NoQueuePriority:
    priority = load_queue_priority(directory)
    if isinstance(priority, NoQueuePriority):
        return priority
    transition = priority["transition"]
    if isinstance(transition, PendingPriorityTransition):
        if finish:
            finish_priority_transition(priority)
        if transition.action == "clear":
            return NO_QUEUE_PRIORITY
    return priority


class UnitMembership(Protocol):
    @property
    def unit(self) -> str: ...


@dataclass(frozen=True)
class RosterUnits:
    pass


class PriorityRosterRows(Protocol):
    @property
    def entries(self) -> Iterable[object]: ...


def queue_unit(unit: str, session_id: str, labels: Mapping[str, UnitMembership]) -> str:
    """Use the caller's unit before the session's unit."""
    label = labels.get(session_id)
    return unit or (label.unit if label is not None else "")


class PriorityRoster(Protocol):
    def UnitLabels(self, session: str) -> UnitMembership: ...
    def ThisMachine(self) -> object: ...
    def roster(self, environment: Mapping[str, str], scope: object) -> PriorityRosterRows: ...
    def unit_labels(self, entries: Iterable[object]) -> Mapping[str, UnitMembership]: ...
    def unit_labels_for(self, session_ids: list[str], environment: Mapping[str, str]) -> Mapping[str, UnitMembership]: ...


def priority_labels(session_ids: list[str] | RosterUnits) -> Mapping[str, UnitMembership]:
    roster = cast(PriorityRoster, cast(object, importlib.import_module("scripts.message.roster")))
    if isinstance(session_ids, RosterUnits):
        labels = roster.unit_labels(roster.roster(os.environ, roster.ThisMachine()).entries)
    else:
        labels = roster.unit_labels_for(session_ids, os.environ)
    no_unit = roster.UnitLabels("").unit
    return {session: label for session, label in labels.items() if label.unit and label.unit != no_unit}


@dataclass(frozen=True)
class QueueOrderContext:
    priority: QueuePriority | NoQueuePriority
    labels: Mapping[str, UnitMembership]


@dataclass(frozen=True)
class QueueOrderNotResolved:
    pass


QUEUE_ORDER_NOT_RESOLVED = QueueOrderNotResolved()


def resolve_queue_order(directory: Path, rows: list[tuple[Path, Waiter]], *, finish: bool = False,
                        readings: QueueSnapshotReadings | QueueOrderNotResolved = QUEUE_ORDER_NOT_RESOLVED
                        ) -> tuple[list[tuple[Path, RankedWaiter]], QueuePriority | NoQueuePriority]:
    context = readings.order if isinstance(readings, QueueSnapshotReadings) else QUEUE_ORDER_NOT_RESOLVED
    if isinstance(context, QueueOrderNotResolved):
        priority = read_queue_priority(directory, finish=finish)
        labels: Mapping[str, UnitMembership] = (priority_labels([row.get("session_id", "") for _, row in rows])
                                                if not isinstance(priority, NoQueuePriority) else {})
        context = QueueOrderContext(priority, labels)
        if isinstance(readings, QueueSnapshotReadings):
            readings.order = context
    return apply_queue_priority(rows, context)


def apply_queue_priority(rows: Iterable[tuple[Path, Waiter]], context: QueueOrderContext
                         ) -> tuple[list[tuple[Path, RankedWaiter]], QueuePriority | NoQueuePriority]:
    ranked: list[tuple[Path, RankedWaiter]] = []
    for path, row in rows:
        unit = queue_unit(row.get("unit", ""), row.get("session_id", ""), context.labels)
        order = (PriorityUnit(context.priority["unit"]) if not isinstance(context.priority, NoQueuePriority)
                 and unit == context.priority["unit"] else NormalOrder())
        ranked.append((path, rank_waiter(row, order)))
    return sorted(ranked, key=lambda item: waiter_order(item[1])), context.priority


NO_HEAD_ADMISSION_OVERRIDE = NoHeadAdmissionOverride()


def head_override_path(directory: Path) -> Path:
    return directory / "admit-head.json"


def head_override_matches(override: HeadAdmissionOverride, path: Path, waiter: Waiter) -> bool:
    return (path.name == override["waiter"] and waiter["pid"] == override["pid"]
            and waiter["start_time"] == override["start_time"])


def read_head_override(directory: Path) -> HeadAdmissionOverride | NoHeadAdmissionOverride:
    try:
        value = cast(object, json.loads(head_override_path(directory).read_text()))
    except FileNotFoundError:
        return NoHeadAdmissionOverride()
    if not isinstance(value, dict):
        raise ValueError("invalid admit-head record")
    row = cast(dict[str, object], value)
    if (any(not isinstance(row.get(key), str) for key in ("waiter", "start_time", "unit", "step", "why", "set_by"))
            or any(not isinstance(row.get(key), int) or isinstance(row.get(key), bool)
                   for key in ("pid", "need", "free", "reserve"))):
        raise ValueError("invalid admit-head record")
    return cast(HeadAdmissionOverride, cast(object, row))


def head_override_event(action: Literal["set", "admitted", "lapsed"], override: HeadAdmissionOverride,
                        need: int, free: int, reserve: int, at: float) -> None:
    record.head_admission(action, override["unit"], override["step"], need, free,
                          reserve, override["why"], override["set_by"], at)


NO_CI_KEEP_ASIDE = CIKeepAside()


def estimate_expired(waiter: Waiter, now: float, policy: AdmissionPolicy) -> bool:
    return (not waiter.get("build_history_current", True) or now - waiter.get("estimated_at", now) >= (policy.timing.fresh_test_seconds
                 if waiter.get("fresh_test", False) else policy.timing.estimate_refresh_seconds))


def ranked_queue_waiters(inputs: QueueInputs) -> list[tuple[Path, RankedWaiter]]:
    """Resolve order metadata without adding it to captured waiter rows."""
    claims = {slot.owner for slot in inputs.slots if isinstance(slot, ContinuationSlot)}
    return sorted(((path, rank_waiter(row, inputs.waiter_orders[waiter_owner(row)]))
                   for path, row in inputs.ordered),
                  key=lambda item: (waiter_owner(item[1]) not in claims, waiter_order(item[1])))


@dataclass(frozen=True)
class OverrideKept:
    record: HeadAdmissionOverride | NoHeadAdmissionOverride = NoHeadAdmissionOverride()


@dataclass(frozen=True)
class OverrideEnded:
    record: HeadAdmissionOverride
    action: Literal["admitted", "lapsed"]


@dataclass(frozen=True)
class BlockingIndex:
    index: int


@dataclass(frozen=True)
class BlockerNotRequested:
    pass


@dataclass(frozen=True)
class ProposedAdmission:
    rank: int
    spacing: StartSpacing
    override: OverrideKept | OverrideEnded


@dataclass(frozen=True)
class NotAdmitted:
    pass


@dataclass(frozen=True)
class PassRow:
    owner: str
    decision: AdmissionDecision
    reason: WaitReason
    blocker: BlockingIndex | NoSingleBlocker | BlockerNotRequested
    admission: ProposedAdmission | NotAdmitted
    holder: records.WaitHolder = field(default_factory=records.NoHolder)


@dataclass(frozen=True)
class PassResult:
    rows: tuple[PassRow, ...]
    policy: HeadPolicy


@dataclass(frozen=True)
class HeadAdmitsAfterEnd:
    ended: tuple[int, ...]
    decision: AdmissionDecision
    charge_drop: int
    freed: int
    growth_drop: int


@dataclass(frozen=True)
class HeadStartsAt:
    at: float
    ended: tuple[int, ...]
    charge_drop: int
    freed: int
    growth_drop: int


@dataclass(frozen=True)
class HeadStillHeld:
    decision: AdmissionDecision
    causes: tuple[Literal["free memory", "pressure", "queue order", "start spacing"], ...]


HeadRelease = HeadAdmitsAfterEnd | HeadStartsAt
HeadAfterEnd = HeadRelease | HeadStillHeld | EstimateExpired | TokenWaitersOnly | NoWaiter | ReadingUnavailable


@dataclass(frozen=True)
class ProcessIdentity:
    parent: int
    start: str


@dataclass(frozen=True)
class ReservationBlocker:
    session_id: str
    session_name: str
    pool: Literal["memory"] = "memory"


@dataclass(frozen=True)
class UnitBlocker:
    unit: str


@dataclass(frozen=True)
class NoSingleBlocker:
    pass


Blocker = ReservationBlocker | UnitBlocker | NoSingleBlocker


def blocker_fields(blocker: Blocker) -> dict[str, str]:
    if isinstance(blocker, ReservationBlocker):
        return {"session_id": blocker.session_id, "session_name": blocker.session_name, "pool": blocker.pool}
    if isinstance(blocker, UnitBlocker):
        return {"unit": blocker.unit}
    return {}


@dataclass(frozen=True)
class UsableAdmissionConfiguration:
    config: MemoryConfig


@dataclass(frozen=True)
class UnusableAdmissionConfiguration:
    reason: str


def read_admission_configuration() -> UsableAdmissionConfiguration | UnusableAdmissionConfiguration:
    """Resolve one pass without preventing cleanup when a setting is unavailable."""
    try:
        return UsableAdmissionConfiguration(read_memory_config(report=False))
    except conf.Unusable as error:
        return UnusableAdmissionConfiguration(str(error))


def memory_room(host: HostMemory | ReadingUnavailable, runs: Sequence[RunMemory],
                policy: AdmissionPolicy, *,
                ci_keep_aside: CIKeepAside = NO_CI_KEEP_ASIDE,
                ci_use: int = 0) -> MemoryRoom | ReadingUnavailable:
    """Calculate room once from anonymous use and accepted promises."""
    if isinstance(host, ReadingUnavailable):
        return host
    safety = policy.safety_bytes
    holds = tuple(RunKeepAside(run,
                  max(0, run.need - run.use.bytes) + run.held
                  if isinstance(run.use, AnonymousRunUse) else run.need + run.held) for run in runs)
    used = sum(run.use.bytes for run in runs if isinstance(run.use, AnonymousRunUse))
    kept = sum(run.bytes for run in holds) + ci_keep_aside.bytes
    unavailable = tuple(run for run in runs if isinstance(run.use, RunUseUnavailable))
    cap: int | CapUnavailable = (CapUnavailable(unavailable) if unavailable
                                 else max(0, used + ci_use + host.available - safety))
    return MemoryRoom(host.available, safety, used, kept,
                      host.available - safety - kept, cap, holds, total=host.total,
                      ci_keep_aside=ci_keep_aside, ci_use=ci_use)


def ci_keep_aside(readings: Iterable[StepMemory], margin: int) -> CIKeepAside:
    """Keep one margin for a CI job with readable slice evidence."""
    running = any(isinstance(reading, UnownedSliceMemory)
                  and isinstance(reading.job_state, RunningServiceJob) for reading in readings)
    return CIKeepAside(margin if running else 0)


def has_running_step(running: list[StepMemory]) -> bool:
    return any(not isinstance(row, (CompilerServiceMemory, GoneMemory))
               and not (isinstance(row, UnownedSliceMemory)
                        and not isinstance(row.job_state, NotServiceSlice)
                        and isinstance(row.activity, EmptyBuildScope)
                        and row.bytes == 0)
               for row in running)


def _need_record(basis: NeedBasis | BasisNotRecorded | TestPhaseNeed | NoTestPhaseNeed) -> dict[str, object]:
    if isinstance(basis, TestPhaseNeed):
        return {"kind": "test_phase_need", "bytes": basis.bytes, "runs": basis.runs,
                "width": basis.width, "recorded_width": basis.recorded_width, "form": basis.form,
                "margin": basis.margin}
    if isinstance(basis, NoTestPhaseNeed):
        return {"kind": "no_test_phase_need", "reason": basis.reason}
    if isinstance(basis, (BasisNotRecorded, str)):
        return {"kind": basis if isinstance(basis, str) else "not_recorded"}
    cap: dict[str, object] = {"kind": "none"}
    if isinstance(basis.cap, CappedAtBudget):
        cap = {"kind": "budget", "uncapped": basis.cap.uncapped}
        if isinstance(basis.cap.cap, int):
            cap["cap"] = basis.cap.cap
    if isinstance(basis, ShapeFallback):
        return {"kind": "shape_fallback", "cap": cap}
    floor: dict[str, object] = ({"kind": "failed_peak", "peak": basis.floor.peak, "ended_at": basis.floor.ended_at}
                                if isinstance(basis.floor, FailedPeakFloor) else {"kind": "none"})
    factor: dict[str, object] = {"kind": "none"}
    for factor_kind, factor_class in HEADROOM_FACTOR_CLASSES.items():
        if isinstance(basis.factor, factor_class):
            factor = {"kind": factor_kind, "factor": basis.factor.factor}
            if isinstance(basis.factor, DevSizeHeadroom):
                factor["margin_bytes"] = basis.factor.margin_bytes
            break
    kind = "shape_p90" if isinstance(basis, ShapeP90) else "shape_largest"
    return {"kind": kind, "runs": basis.runs,
            "floor": floor, "factor": factor, "cap": cap}


def _read_need_record(raw: object) -> NeedBasis | BasisNotRecorded | TestPhaseNeed | NoTestPhaseNeed:
    """One boundary for cached estimates and queue explanations."""
    if not isinstance(raw, dict):
        return BasisNotRecorded()
    row = cast(dict[str, object], raw)
    kind = row.get("kind")
    if kind == "no_test_phase_need" and row.get("reason") in ("no_width", "no_history", "not_recorded"):
        return NoTestPhaseNeed(cast(Literal["no_width", "no_history", "not_recorded"], row["reason"]))
    if kind == "test_phase_need":
        numbers = [row.get(name) for name in ("bytes", "runs", "width", "recorded_width")]
        if (all(type(number) is int and number > 0 for number in numbers)
                and row.get("form") in ("same_width", "wider_unscaled", "scaled_up")):
            margin = row.get("margin")
            if type(margin) is not float or not math.isfinite(margin) or margin < 1.0:
                return NoTestPhaseNeed("not_recorded")
            return TestPhaseNeed(cast(int, numbers[0]), cast(int, numbers[1]),
                                 cast(int, numbers[2]), cast(int, numbers[3]),
                                 cast(TestPhaseNeedForm, row["form"]), margin)
        return BasisNotRecorded()
    if kind in ("measured", "buildlog", "pooled", "fallback"):
        return kind
    if kind not in ("shape_p90", "shape_largest", "shape_fallback"):
        return BasisNotRecorded()
    try:
        cap_row = cast(dict[str, object], row["cap"])
        cap: CappedAtBudget | UnderBudget = UnderBudget()
        if cap_row["kind"] == "budget":
            uncapped = cap_row["uncapped"]
            if type(uncapped) is not int or uncapped <= 0:
                return BasisNotRecorded()
            saved_cap: int | CapNotRecorded = CapNotRecorded()
            if "cap" in cap_row:
                cap_bytes = cap_row["cap"]
                if type(cap_bytes) is not int or not 0 < cap_bytes < uncapped:
                    return BasisNotRecorded()
                saved_cap = cap_bytes
            cap = CappedAtBudget(uncapped, saved_cap)
        elif cap_row["kind"] != "none":
            return BasisNotRecorded()
        if kind == "shape_fallback":
            if "floor" in row or "factor" in row:
                return BasisNotRecorded()
            return ShapeFallback(cap)
        floor_row = cast(dict[str, object], row["floor"])
        factor_row = cast(dict[str, object], row["factor"])
        floor: FailedPeakFloor | NoFloor = NoFloor()
        if floor_row["kind"] == "failed_peak":
            peak, ended_at = floor_row["peak"], floor_row["ended_at"]
            if not isinstance(peak, int) or peak <= 0 or not isinstance(ended_at, str):
                return BasisNotRecorded()
            _ = datetime.fromisoformat(ended_at)
            floor = FailedPeakFloor(peak, ended_at)
        elif floor_row["kind"] != "none":
            return BasisNotRecorded()
        factor: HeadroomRule = NoHeadroom()
        factor_kind = factor_row["kind"]
        factor_class = HEADROOM_FACTOR_CLASSES.get(factor_kind) if isinstance(factor_kind, str) else None
        if factor_class is not None:
            saved_factor = factor_row.get("factor")
            if isinstance(saved_factor, bool) or not isinstance(saved_factor, (float, int)):
                return BasisNotRecorded()
            try:
                value = float(saved_factor)
            except OverflowError:
                return BasisNotRecorded()
            if not math.isfinite(value) or value < 1:
                return BasisNotRecorded()
            if factor_class is DevSizeHeadroom:
                margin = factor_row.get("margin_bytes")
                if type(margin) is not int or margin < 0:
                    return BasisNotRecorded()
                factor = DevSizeHeadroom(value, margin)
            else:
                factor = cast(type[ReleaseHeadroom | LegacyDevMultiplierHeadroom], factor_class)(value)
        elif factor_kind != "none":
            return BasisNotRecorded()
        runs = row["runs"]
        if not isinstance(runs, int) or runs < 0:
            return BasisNotRecorded()
        if kind == "shape_p90" and runs >= 10:
            return ShapeP90(runs, floor, factor, cap)
        if kind == "shape_largest" and 1 <= runs < 10:
            return ShapeLargest(runs, floor, factor, cap)
    except (KeyError, TypeError, ValueError):
        pass
    return BasisNotRecorded()


def need_basis_record(basis: NeedBasis | BasisNotRecorded) -> dict[str, object]:
    return _need_record(basis)


def read_need_basis(raw: object) -> NeedBasis | BasisNotRecorded:
    value = _read_need_record(raw)
    return BasisNotRecorded() if isinstance(value, (TestPhaseNeed, NoTestPhaseNeed)) else value


def test_phase_need_record(need: TestPhaseNeed | NoTestPhaseNeed) -> dict[str, object]:
    return _need_record(need)


def read_test_phase_need(raw: object) -> TestPhaseNeed | NoTestPhaseNeed:
    value = _read_need_record(raw)
    return value if isinstance(value, (TestPhaseNeed, NoTestPhaseNeed)) else NoTestPhaseNeed("not_recorded")


def test_phase_need_from_rows(rows: Iterable[Mapping[str, object]],
                             row: Mapping[str, object], host: str, now: datetime, margin: float) -> TestPhaseNeed | NoTestPhaseNeed:
    """Choose narrow-family history at the closest usable width; read no external state."""
    recorded = row.get("test_threads")
    width = recorded.threads if isinstance(recorded, RequestedTestWidth) else recorded
    if type(width) is not int or width <= 0:
        return NoTestPhaseNeed("no_width")
    repository = row.get("repo_identity")
    if not isinstance(repository, str) or not repository:
        return NoTestPhaseNeed("no_history")
    cutoff = now - timedelta(days=HISTORY_DAYS)
    by_width: dict[int, list[int]] = {}
    for item in rows:
        if (item.get("host") != host or item.get("step") != "nextest"
                or item.get("repo_identity") != repository
                or item.get("test_scope", "full") != row.get("test_scope", "full")
                or item.get("package", "") != row.get("package", "")
                or sorted(history_arguments(item.get("filters"))) != sorted(history_arguments(row.get("filters")))):
            continue
        peak, recorded_width = item.get("test_phase_peak_bytes"), item.get("test_threads")
        if type(peak) is not int or peak <= 0 or type(recorded_width) is not int or recorded_width <= 0:
            continue
        try:
            ended = datetime.fromisoformat(str(item.get("ended_at", "")).replace("Z", "+00:00"))
            if not cutoff <= ended < now:
                continue
        except (ValueError, TypeError):
            continue
        by_width.setdefault(recorded_width, []).append(peak)
    if not by_width:
        return NoTestPhaseNeed("no_history")
    form: TestPhaseNeedForm
    if width in by_width:
        recorded_width, form = width, "same_width"
    elif wider := [candidate for candidate in by_width if candidate > width]:
        recorded_width, form = min(wider), "wider_unscaled"
    else:
        recorded_width, form = max(by_width), "scaled_up"
    peaks = by_width[recorded_width]
    need = percentile(peaks)
    if form == "scaled_up":
        need = (need * width + recorded_width - 1) // recorded_width
    return TestPhaseNeed(max(FILTERED_FLOOR, need), len(peaks), width, recorded_width, form, margin)


def test_phase_need(row: Mapping[str, object], host: str, now: datetime, root: Path, *,
                    history_path: Path | None = None,
                    config: MemoryConfig
                    ) -> TestPhaseNeed | NoTestPhaseNeed:
    """Freeze the history need and margin when the reservation need is written."""
    try:
        path = history_path if history_path is not None else root / "admission/anon_peaks.jsonl"
        return test_phase_need_from_rows(anonymous_history_rows(path), row, host, now, config.test_phase_margin)
    except OSError:
        return NoTestPhaseNeed("no_history")


def test_phase_path(row: Reservation) -> Path | NoTestPhaseFile:
    sidecar = row["sidecar"]
    return Path(sidecar.removesuffix(".cgroup") + ".test-phase.json") if sidecar.endswith(".cgroup") else NoTestPhaseFile()


def observed_test_phase(row: Reservation) -> ObservedTestPhase | UnobservedTestPhase:
    path = test_phase_path(row)
    if isinstance(path, Path):
        phase = record.read_test_phase(path)
        if isinstance(phase, TestPhaseRecord) and isinstance(phase.window, ObservedTestPhase):
            return phase.window
    return UnobservedTestPhase()


def process_table(proc: Path = PROC) -> dict[int, ProcessIdentity]:
    processes: dict[int, ProcessIdentity] = {}
    for entry in proc.iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(") ", 1)[1].split()
            if fields[0] not in ("Z", "X"):
                processes[int(entry.name)] = ProcessIdentity(int(fields[1]), fields[19])
        except (OSError, ValueError, IndexError):
            continue
    return processes


def process_tree(roots: Mapping[int, str], processes: dict[int, ProcessIdentity]) -> set[int]:
    """Find live roots and their descendants from one process table reading."""
    children: dict[int, list[int]] = {}
    for child, identity in processes.items():
        children.setdefault(identity.parent, []).append(child)
    pending = [pid for pid, start in roots.items() if pid in processes and processes[pid].start == start]
    seen: set[int] = set()
    while pending:
        child = pending.pop()
        if child in seen:
            continue
        seen.add(child)
        pending.extend(children.get(child, []))
    return seen


@dataclass(frozen=True)
class PopulatedStepScope:
    directory: Path


@dataclass(frozen=True)
class LiveOwnerProcess:
    pass


def scope_members(directory: Path) -> set[int]:
    """Read nested scope members and skip scopes removed during the walk."""
    members: set[int] = set()
    pending = [directory]
    while pending:
        scope = pending.pop()
        try:
            path = scope / "cgroup.procs"
            check_memory_reading(path)
            members.update(int(pid) for pid in path.read_text().split())
            pending.extend(child for child in scope.iterdir() if child.is_dir())
        except OSError as error:
            if error.errno not in (errno.ENOENT, errno.ESRCH, errno.ENODEV):
                raise
    return members


def process_memory(source: set[int] | PopulatedStepScope, proc: Path = PROC) -> MeasuredMemory:
    """Read anonymous use from the scope or processes; retain resident bytes and swap."""
    if isinstance(source, PopulatedStepScope):
        reading = slice_working_set(source.directory)
        try:
            members = frozenset(scope_members(source.directory))
        except (OSError, ValueError) as error:
            return replace(reading, anonymous=RunUseUnavailable(f"{source.directory}: {error}"),
                           anonymous_source=ScopeSource(source.directory, frozenset()))
        return replace(reading, anonymous_source=ScopeSource(source.directory, members))
    used, swapped = 0, 0
    anonymous: list[ProcessAnonymousUse] = []
    for child in sorted(source):
        path = proc / str(child) / "status"
        try:
            contents = path.read_text().splitlines()
        except OSError as error:
            use = (AnonymousRunUse(0) if error.errno in (errno.ENOENT, errno.ESRCH) else
                   RunUseUnavailable(f"{path}: {error.strerror or error}"))
            anonymous.append(ProcessAnonymousUse(child, use))
            continue
        rss_present = False
        anon_seen = False
        anonymous_fields: dict[str, int] = {}
        for line in contents:
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "VmRSS:":
                rss_present = True
            if parts[0] in ("VmRSS:", "VmSwap:"):
                try:
                    size = int(parts[1]) * 1024
                    if parts[0] == "VmSwap:":
                        swapped += size
                    else:
                        used += size
                except (ValueError, IndexError):
                    pass
            elif parts[0] == "RssAnon:" and not anon_seen:
                anon_seen = True
                anonymous_fields = kib_fields(parts)
        if not rss_present:
            use = AnonymousRunUse(0)
        elif "RssAnon" in anonymous_fields:
            use = AnonymousRunUse(anonymous_fields["RssAnon"])
        else:
            use = RunUseUnavailable(f"{path}: RssAnon missing or invalid")
        anonymous.append(ProcessAnonymousUse(child, use))
    return MeasuredMemory(used, swap_bytes=swapped, anonymous=sum_anonymous_use(item.use for item in anonymous),
                          anonymous_source=ProcessSource(tuple(anonymous)))


def sum_anonymous_use(readings: Iterable[AnonymousRunUse | RunUseUnavailable]) -> AnonymousRunUse | RunUseUnavailable:
    total = 0
    for use in readings:
        if isinstance(use, RunUseUnavailable):
            return use
        total += use.bytes
    return AnonymousRunUse(total)


def tree_memory(pid: int, start: str, processes: dict[int, ProcessIdentity],
                proc: Path = PROC) -> MeasuredMemory | GoneMemory:
    """Read resident bytes and swap separately from one process table reading."""
    members = process_tree({pid: start}, processes)
    return process_memory(members, proc) if members else GoneMemory()


@dataclass(frozen=True)
class EndedStep:
    pass


StepLifetime = PopulatedStepScope | LiveOwnerProcess | EndedStep


def reservation_lifetime(row: Reservation, owner_live: bool) -> StepLifetime:
    """A scope outlives its shell while any of its processes remain."""
    sidecar = row.get("sidecar", "")
    if sidecar:
        try:
            scope_name = Path(sidecar).read_text().strip()
        except FileNotFoundError:
            scope_name = ""
        if scope_name:
            scope = Path(scope_name)
            members = scope / "cgroup.procs"
            check_memory_reading(members)
            try:
                if members.read_text().split():
                    return PopulatedStepScope(scope)
            except FileNotFoundError:
                pass
            except OSError as error:
                if error.errno != errno.ENODEV:
                    raise
    return LiveOwnerProcess() if owner_live else EndedStep()


def step_memory(row: Reservation, processes: dict[int, ProcessIdentity],
                proc: Path = PROC) -> StepMemory:
    identity = processes.get(row["pid"])
    owner_live = identity is not None and identity.start == row["start_time"]
    lifetime = reservation_lifetime(row, owner_live)
    if isinstance(lifetime, EndedStep):
        return GoneMemory()
    if isinstance(lifetime, PopulatedStepScope):
        try:
            reading = process_memory(lifetime, proc)
        except FileNotFoundError:
            if not owner_live:
                return GoneMemory()
            reading = tree_memory(row["pid"], row["start_time"], processes, proc)
    else:
        reading = tree_memory(row["pid"], row["start_time"], processes, proc)
    return step_promise(row, reading)


def step_promise(row: Reservation, reading: StepMemory) -> StepMemory:
    """Keep the accepted need until the reservation ends."""
    return replace(reading, promise=LifetimePromise(row["need"])) if isinstance(reading, MeasuredMemory) else reading

def release_build(row: Reservation) -> bool:
    shape = recorded_build_shape(row.get("shape"))
    return row.get("step") == "build" and isinstance(shape, BuildShape) and shape.profile == "release"


def test_phase_history(row: Reservation, reading: StepMemory,
                       phase: ObservedTestPhase | UnobservedTestPhase
                       ) -> TestPhaseStarted | NoPeakRecorded:
    """Use the frozen test-phase need only after the sampler observes tests."""
    need = read_test_phase_need(row.get("test_phase_need"))
    if (row["step"] == "nextest" and isinstance(reading, MeasuredMemory)
            and isinstance(need, TestPhaseNeed) and isinstance(phase, ObservedTestPhase)):
        return TestPhaseStarted(phase.started_at, phase.peak_bytes, need)
    return NoPeakRecorded()


def reservation_memory(path: Path, row: Reservation, now: datetime,
                       processes: dict[int, ProcessIdentity], proc: Path, policy: AdmissionPolicy) -> StepMemory:
    reading = step_memory(row, processes, proc)
    phase_history = test_phase_history(row, reading, observed_test_phase(row))
    if isinstance(phase_history, TestPhaseStarted) and isinstance(reading, MeasuredMemory):
        return replace(reading, peak_history=phase_history)
    try:
        peak = int(path.with_suffix(".max").read_text())
        if peak < 0:
            raise ValueError("negative observed peak")
    except FileNotFoundError:
        peak = 0  # The sampler has not recorded a peak (unscoped steps never do).
    except (OSError, ValueError):
        if isinstance(reading, MeasuredMemory):
            return replace(reading, unknown_peak_promise=row["need"], peak_history=PeakFileUnreadable(row["need"]))
        return reading
    last_near_peak_at = now.timestamp()  # Unknown times retain the peak charge.
    peak_at: float | PeakTimeUnknown = PeakTimeUnknown()
    peak_time: float | PeakTimeUnknown = PeakTimeUnknown()
    old_sampler = False
    if peak > 0:
        try:
            peak_at = path.with_suffix(".max").stat().st_mtime
        except OSError:
            pass
    try:
        near_peak_bytes, recorded_at = map(int, path.with_suffix(".wave").read_text().split())
        if near_peak_bytes == peak and recorded_at >= 0:
            last_near_peak_at = recorded_at
            peak_time = recorded_at
    except FileNotFoundError:
        # Samplers from before patch B write no .wave; use the last peak publication.
        if peak > 0:
            old_sampler = True
            if isinstance(peak_at, float):
                last_near_peak_at = peak_at
                peak_time = peak_at
    except (OSError, ValueError):
        pass
    history: PeakHistory = PeakSoFar(peak, peak_at, peak_time, old_sampler) if peak > 0 else NoPeakRecorded()
    if not release_build(row) and now.timestamp() - last_near_peak_at >= policy.peak_hold.seconds_for(row.get("step", "")):
        history = PeakDecayed(0, peak, last_near_peak_at, last_near_peak_at + policy.peak_hold.seconds_for(row.get("step", "")), old_sampler)
        peak = 0
    if isinstance(reading, MeasuredMemory):
        return replace(reading, observed_peak=peak, peak_history=history)
    if isinstance(reading, PromisedMemory):
        return PromisedMemory(max(reading.bytes, peak))
    return reading


def member_programs(members: Iterable[int], proc: Path, *, skip_unreadable: bool = False) -> dict[int, str]:
    """Read each member's program name once."""
    programs: dict[int, str] = {}
    for pid in members:
        try:
            programs[pid] = (proc / str(pid) / "comm").read_text().strip()
        except OSError as error:
            if not skip_unreadable and error.errno != errno.ENOENT:
                raise
    return programs


def sccache_memory(proc: Path = PROC) -> list[StepMemory]:
    """Charge the compiler service once, outside all step process trees."""
    directory = Path(os.environ.get("BUILDLOG_BUILDS_CGROUP",
        f"/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/builds.slice"))
    try:
        reading = slice_working_set(directory / "sccache.service")
        try:
            members = (directory / "sccache.service" / "cgroup.procs").read_text().split()
        except OSError:
            members = []
        programs = member_programs((int(member) for member in members if member.isdecimal()), proc,
                                   skip_unreadable=True)
        servers = [pid for pid, program in programs.items() if program == "sccache"]
        return [CompilerServiceMemory(reading.bytes, swap_bytes=reading.swap_bytes,
                                      pid=min(servers) if servers else PidNotRecorded())]
    except (OSError, ValueError):
        return []


class LiveMemoryReading(RuntimeError):
    """A test tried to read the host's memory state."""


def check_memory_reading(path: Path) -> None:
    record_path = os.environ.get("BUILDLOG_LIVE_MEMORY_TRIPWIRE", "")
    if not record_path:
        return
    live_path = os.path.realpath(path)
    cgroup = Path(live_path).is_relative_to("/sys/fs/cgroup")
    if live_path not in ("/proc/meminfo", "/proc/pressure/memory") and not cgroup:
        return
    try:
        with Path(record_path).open("ab", buffering=0) as output:
            _ = output.write((json.dumps({"reading": live_path, "argv": sys.argv, "pid": os.getpid()}) + "\n").encode())
    except OSError:
        pass  # The refusal below still stops the read; callers swallow OSError.
    if cgroup:
        raise LiveMemoryReading(f"live memory reading {live_path} in a test; fake BUILDLOG_BUILDS_CGROUP (scripts/lint/memory_test_support.py)")
    raise LiveMemoryReading(
        f"live memory reading {live_path} in a test; fake BUILDLOG_MEMINFO and "
        + "BUILDLOG_MEMORY_PRESSURE (scripts/lint/memory_test_support.py)"
    )


def kib_fields(parts: list[str]) -> dict[str, int]:
    """Read a nonnegative byte value with an explicit kB unit."""
    if len(parts) == 3 and parts[2] == "kB" and parts[1].isdecimal():
        return {parts[0].removesuffix(":"): int(parts[1]) * 1024}
    return {}


def read_meminfo(path: Path) -> HostMemory:
    check_memory_reading(path)
    values: dict[str, int] = {}
    try:
        text = path.read_text()
    except (OSError, UnicodeError) as error:
        raise ValueError(f"cannot read MemAvailable from {path}") from error
    for line in text.splitlines():
        values.update(kib_fields(line.split()))
    if "MemAvailable" not in values:
        raise ValueError(f"cannot read MemAvailable from {path}")
    return HostMemory(values["MemAvailable"], values.get("MemTotal", UnknownHostTotal()),
                      values.get("SwapTotal", 0), values.get("SwapFree", 0))


NO_HOST_SWAP = HostMemory(0, UnknownHostTotal())


def read_pressure(host: HostMemory = NO_HOST_SWAP, *, policy: AdmissionPolicy) -> Pressure:
    """Read PSI once and retain missing evidence for scheduling and display."""
    readings: dict[str, float] = {}
    path = Path(os.environ.get("BUILDLOG_MEMORY_PRESSURE", "/proc/pressure/memory"))
    check_memory_reading(path)
    try:
        for line in path.read_text().splitlines():
            parts = line.split()
            if parts and parts[0] in ("full", "some"):
                value = next(float(part.removeprefix("avg10=")) for part in parts[1:] if part.startswith("avg10="))
                if math.isfinite(value) and value >= 0:
                    readings[parts[0]] = value
    except OSError as error:
        detail = f" ({error.strerror})" if error.strerror and len(error.strerror) <= 60 else ""
        return PressureUnavailable(f"pressure unreadable: {path}{detail}")
    except (ValueError, StopIteration):
        return PressureUnavailable(f"pressure unreadable: {path} (incomplete)")
    if "full" not in readings or "some" not in readings:
        return PressureUnavailable(f"pressure unreadable: {path} (incomplete)")
    full, some = readings.get("full", 0.0), readings.get("some", 0.0)
    swap = 100 * max(0, host.swap_total - host.swap_free) / host.swap_total if host.swap_total else 0.0
    return pressure_from_readings(full, some, swap, policy)


def pressure_from_readings(full: float, some: float, swap: float, policy: AdmissionPolicy) -> Pressure:
    """Use one pressure predicate for live admission and replay estimates."""
    if swap > policy.pressure.swap_used_limit and some > policy.pressure.some_avg10_limit:
        return MemoryPressure(full, some, swap, condition="swap")
    return MemoryPressure(full, some, swap, condition="full") if full > policy.pressure.full_avg10_limit else NO_PRESSURE_HOLD


def admission_dir(root: Path) -> Path:
    directory = root / "admission"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def read_reservation(path: Path) -> Reservation:
    """Read one reservation; reject bad fields before a caller uses them."""
    value = cast(object, json.loads(path.read_text()))
    if not isinstance(value, dict):
        raise ValueError("reservation must be an object")
    row = cast(dict[str, object], value)
    if (not isinstance(row.get("pid"), int) or isinstance(row.get("pid"), bool)
            or not isinstance(row.get("need"), int) or isinstance(row.get("need"), bool)
            or not all(isinstance(row.get(key), str) for key in
                       ("start_time", "repo", "step", "worktree", "admitted_at", "sidecar"))):
        raise ValueError("reservation has invalid required fields")
    if (row.get("test_threads") is not None and not isinstance(row["test_threads"], int)
            or row.get("call_id") is not None and not isinstance(row["call_id"], str)):
        raise ValueError("reservation has invalid test threads or call id")
    if "finalization" in row:
        saved = row["finalization"]
        if not isinstance(saved, dict):
            raise ValueError("finalization intent must be an object")
        intent = cast(dict[str, object], saved)
        if (not all(isinstance(intent.get(key), str) for key in ("reservation_id", "ended_at"))
                or not isinstance(intent.get("peak"), int) or isinstance(intent.get("peak"), bool)
                or not isinstance(intent.get("scoped"), bool)
                or intent.get("outcome") not in ("completed", "failed", "killed", "owner_gone")
                or "status" in intent and (not isinstance(intent["status"], int)
                                           or isinstance(intent["status"], bool))):
            raise ValueError("finalization intent has invalid fields")
    width, call = row["test_threads"], row["call_id"]
    row["test_threads"] = (RequestedTestWidth(width) if isinstance(width, int) else
                           UnspecifiedTestWidth() if row["step"] == "nextest" else NoTestWidth())
    row["call_id"] = CallRecord(call) if isinstance(call, str) else NoCallRecord()
    return cast(Reservation, cast(object, row))


def reservation_rows(directory: Path) -> list[tuple[Path, Reservation]]:
    rows: list[tuple[Path, Reservation]] = []
    for path in directory.glob("*.reservation"):
        try:
            rows.append((path, read_reservation(path)))
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return rows


class TokenWaitIntent(TypedDict):
    reservation: str
    waiter: Waiter


def read_token_wait(path: Path) -> TokenWaitIntent:
    value = cast(object, json.loads(path.read_text()))
    return cast(TokenWaitIntent, codec.parse_snapshot_value(value, TokenWaitIntent))


def read_waiter(path: Path) -> Waiter:
    """Validate persisted waiter fields with the same schema used by snapshots."""
    value = cast(object, json.loads(path.read_text()))
    return stored_waiter(cast(Waiter, codec.parse_snapshot_value(value, Waiter)))


def waiter_label(row: Waiter) -> str:
    label = row.get("label", "")
    if label:
        return label
    # Old callers wrote only a pid. Read their label without changing the record.
    process = Path("/proc") / str(row["pid"])
    try:
        folder = (process / "cwd").readlink().name
        argv = (process / "cmdline").read_bytes().decode(errors="replace").split("\0")
        caller = "verify" if any(item.endswith("/verify.sh") for item in argv) else Path(argv[0]).name
        return f"{folder} ({caller} pid {row['pid']})"
    except (OSError, IndexError):
        return f"caller pid {row['pid']}"


def waiter_order(row: Waiter) -> tuple[int, float, int]:
    return (0 if isinstance(waiter_priority(row), PriorityUnit) else 1), row.get("order_since", row["since"]), row["pid"]


@dataclass(frozen=True)
class WaiterAdmission:
    waiter: Waiter
    decision: AdmissionDecision
    state: QueuedWaiter | TokenWaitingWaiter


def snapshot_head_override(override: HeadAdmissionOverride | NoHeadAdmissionOverride,
                           ordered: list[tuple[Path, RankedWaiter]]) -> HeadAdmissionOverride | NoHeadAdmissionOverride:
    candidate = first_queued_waiter(row for _, row in ordered)
    if isinstance(override, NoHeadAdmissionOverride) or isinstance(candidate, NoWaiter):
        return NoHeadAdmissionOverride()
    path = next(path for path, row in ordered if waiter_owner(row) == waiter_owner(candidate.waiter))
    return override if head_override_matches(override, path, candidate.waiter) else NoHeadAdmissionOverride()


def queue_life_identity(row: Waiter) -> QueueLifeIdentity:
    """Identify one logical step across its reservation intervals."""
    return {"pid": row["pid"], "start_time": row["start_time"], "since": row["since"], "unit": "",
            **RecordedQueueIdentities().identity(row["pid"], cast(dict[str, object], cast(object, row)))}


def lifecycle_event[Kind: Literal["admitted", "left", "ended"]](
        identity: QueueLifeIdentity, kind: Kind, key: str) -> LifecycleEventIdentity[Kind]:
    """Give lifecycle events one identity and one retry key."""
    return {**identity, "kind": kind, "event_id": kind + ":" + key}


def queue_times_backward(handle: BinaryIO) -> Generator[dict[str, object]]:
    """Read complete queue events from the tail of an already locked file."""
    if not handle.seek(0, os.SEEK_END):
        return
    with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as contents:
        end = len(contents) - 1  # locked_append has repaired any unfinished tail.
        while end > 0:
            start = contents.rfind(b"\n", 0, end) + 1
            try:
                value = cast(object, json.loads(contents[start:end]))
            except ValueError:
                value = None
            if isinstance(value, dict):
                yield cast(dict[str, object], value)
            end = start - 1


def token_gap_recorded(rows: Iterator[dict[str, object]], event: dict[str, object]) -> bool:
    """Stop the duplicate lookup before this token wait began."""
    left = event.get("token_left_at")
    if not isinstance(left, (int, float)):
        return False
    keys = ("kind", "pid", "start_time", "token_left_at", "episode") if "episode" in event else (
        "kind", "pid", "start_time", "token_left_at")
    for row in rows:
        at = row.get("at")
        if isinstance(at, (int, float)) and at < left:
            return False
        if all(row.get(key) == event.get(key) for key in keys):
            return True
    return False


def append_queue_time(directory: Path, event: dict[str, object], now: float) -> None:
    """Append under the history lock; a token gap can be retried after a stop."""
    path = directory / "queue_times.jsonl"
    with store.locked_append(path) as handle:
        with closing(queue_times_backward(handle)) as rows:
            last = next(rows, cast(dict[str, object], {}))
            previous = chain((last,), rows)
            if event.get("kind") == "token_gap":
                if token_gap_recorded(previous, event):
                    return
            elif "event_id" in event:
                event_at = event.get("admitted_at", event.get("ended_at", now))
                lower_bound = event_at if isinstance(event_at, (int, float)) else now
                for row in previous:
                    if isinstance(stamp := row.get("at"), (int, float)) and stamp < lower_bound:
                        break
                    if row.get("event_id") == event["event_id"]:
                        return
            last_at = last.get("at", now)
            at = max(now, float(last_at)) if isinstance(last_at, (int, float)) else now
        store.append_locked(handle, {**event, "at": at})
        if os.fstat(handle.fileno()).st_size > 8 * 1024 * 1024:
            cutoff = now - HISTORY_DAYS * 86400
            kept = [row for row in store.whole_rows(handle, 0)
                    if isinstance(stamp := row.get("at"), (float, int)) and stamp >= cutoff]
            newest: list[str] = []
            size = 0
            for row in reversed(kept):
                line = json.dumps(row, separators=(",", ":")) + "\n"
                size += len(line.encode())
                if size >= 6 * 1024 * 1024:
                    break
                newest.append(line)
            write_text_atomic(path, "".join(reversed(newest)))


@dataclass(frozen=True)
class StepStillOpen:
    pass


@dataclass(frozen=True)
class ReservationEnded:
    ended_at: float
    outcome: FinalOutcomeName
    end_kind: StepEnd | TokenReturn
    peak: PeakSampled | NoPositiveSample


@dataclass(frozen=True)
class LeftQueue:
    ended_at: float
    reason: LeftReason


@dataclass(frozen=True)
class QueueTokenGap:
    token_left_at: float
    token_back_at: float


@dataclass(frozen=True)
class ReservationInterval:
    reservation_id: str
    admitted_at: float
    need: int
    need_source: NeedSource
    is_head: bool
    end: ReservationEnded | StepStillOpen


@dataclass(frozen=True)
class MemoryWaitIncomplete:
    """A later admission has no recorded token return time."""


@dataclass(frozen=True)
class QueueStepLifetime:
    pid: int
    start_time: str
    since: float
    step: str
    repo: str
    intervals: tuple[ReservationInterval, ...]
    token_gaps: tuple[QueueTokenGap, ...]
    end: ReservationEnded | LeftQueue | StepStillOpen
    memory_wait_s: float | MemoryWaitIncomplete


def read_queue_lifetimes(directory: Path) -> tuple[QueueStepLifetime, ...]:
    """Join retained lifecycle records into logical steps under a shared read lock."""
    try:
        events = list(anonymous_history_rows(directory / "queue_times.jsonl"))
    except FileNotFoundError:
        return ()
    grouped: dict[tuple[int, str, float], list[dict[str, object]]] = {}
    for event in events:
        pid, start, since = event.get("pid"), event.get("start_time"), event.get("since")
        if type(pid) is not int or not isinstance(start, str) or type(since) not in (int, float):
            continue
        arrival = float(cast(int | float, since))
        if not math.isfinite(arrival):
            continue
        grouped.setdefault((pid, start, arrival), []).append(event)
    steps: list[QueueStepLifetime] = []
    for (pid, start, since), rows in grouped.items():
        intervals: dict[str, ReservationInterval] = {}
        gaps: list[QueueTokenGap] = []
        end: ReservationEnded | LeftQueue | StepStillOpen = StepStillOpen()
        step, repo = "", ""
        # The append path preserves event order, including equal timestamps.
        for row in rows:
            try:
                kind = row.get("kind")
                step = str(row.get("step", step))
                repo = str(row.get("repo", repo))
                if kind == "admitted":
                    admitted = cast(AdmittedEvent, codec.parse_snapshot_value(row, AdmittedEvent))
                    reservation = admitted["reservation_id"]
                    intervals[reservation] = ReservationInterval(reservation, admitted["admitted_at"],
                        admitted["need"], admitted["need_source"], admitted["is_head"], StepStillOpen())
                    end = StepStillOpen()
                elif kind == "ended":
                    ended = cast(EndedEvent, codec.parse_snapshot_value(row, EndedEvent))
                    reservation = ended["reservation_id"]
                    ending = ReservationEnded(ended["ended_at"], ended["outcome"], ended["end_kind"], ended["peak"])
                    if reservation in intervals:
                        intervals[reservation] = replace(intervals[reservation], end=ending)
                    end = ending if isinstance(ending.end_kind, StepEnd) else StepStillOpen()
                elif kind == "left":
                    left = cast(LeftEvent, codec.parse_snapshot_value(row, LeftEvent))
                    end = LeftQueue(left["ended_at"], left["reason"])
                elif kind == "token_gap":
                    gaps.append(QueueTokenGap(
                        cast(float, codec.parse_snapshot_value(row["token_left_at"], float)),
                        cast(float, codec.parse_snapshot_value(row["token_back_at"], float))))
                    end = StepStillOpen()
            except (KeyError, ValueError, TypeError):
                continue
        ordered = tuple(sorted(intervals.values(), key=lambda interval: interval.admitted_at))
        wait: float | MemoryWaitIncomplete = 0.0
        if ordered:
            wait = max(0.0, ordered[0].admitted_at - since)
            for before, after in zip(ordered, ordered[1:]):
                returns = [gap.token_back_at for gap in gaps
                           if before.admitted_at <= gap.token_left_at <= gap.token_back_at <= after.admitted_at]
                if not returns:
                    wait = MemoryWaitIncomplete()
                    break
                wait += max(0.0, after.admitted_at - max(returns))
        elif isinstance(end, LeftQueue):
            wait = max(0.0, end.ended_at - since)
        else:
            wait = MemoryWaitIncomplete()
        steps.append(QueueStepLifetime(pid, start, since, step, repo, ordered, tuple(gaps), end, wait))
    return tuple(sorted(steps, key=lambda step: (step.since, step.pid, step.start_time)))


def reservation_stem(directory: Path, pid: int, start_time: str, admitted_ns: int) -> Path:
    return directory / f"{pid}-{start_time}-{admitted_ns}"


def waiter_unit_name(identity: WaiterIdentity, pid: int) -> str:
    return identity.get("unit") or identity["session_name"] or Path(identity["folder"]).name or str(pid)


class QueueIdentityReader:
    def __init__(self, processes: dict[int, ProcessIdentity], proc: Path = PROC) -> None:
        self.processes: dict[int, ProcessIdentity] = processes
        self.proc: Path = proc
        self.environments: dict[int, dict[str, str]] = {}
        self.names: dict[str, str] = {}
        directory = Path(os.environ.get("NOTIFIER_SESSIONS_DIR", str(Path.home() / ".claude/sessions")))
        for path in directory.glob("*.json"):
            try:
                row = cast(dict[str, object], json.loads(path.read_text()))
                session, name = row.get("sessionId"), row.get("name")
                if isinstance(session, str) and isinstance(name, str):
                    self.names[session] = name
            except (OSError, ValueError, TypeError, AttributeError):
                continue

    def identity(self, pid: int, row: dict[str, object]) -> WaiterIdentity:
        session = str(row.get("session_id", ""))
        name = str(row.get("session_name", ""))
        folder = str(row.get("folder") or row.get("worktree") or "")
        step = str(row.get("step", ""))
        owner = self.processes.get(pid)
        if owner is None or owner.start != row.get("start_time"):
            return {"session_id": session, "session_name": name, "step": step, "folder": folder}
        if not folder:
            try:
                folder = str((self.proc / str(pid) / "cwd").readlink())
            except OSError:
                pass
        current, seen = pid, set[int]()
        while not session and current in self.processes and current not in seen:
            seen.add(current)
            if current not in self.environments:
                try:
                    raw = (self.proc / str(current) / "environ").read_bytes().decode(errors="replace")
                    self.environments[current] = dict(part.split("=", 1) for part in raw.split("\0") if "=" in part)
                except OSError:
                    self.environments[current] = {}
            env = self.environments[current]
            session = env.get("CODEX_THREAD_ID") or env.get("CLAUDE_CODE_SESSION_ID", "")
            name = name or env.get("CLAUDE_SESSION_NAME", "")
            current = self.processes[current].parent
        if not step:
            try:
                argv = (self.proc / str(pid) / "cmdline").read_bytes().decode(errors="replace").split("\0")
                verb = next((item for item in argv if item in ("test", "lint", "check", "build", "doc", "clippy")), "build")
                step = verb
            except OSError:
                step = "build"
        return {"session_id": session, "session_name": name or self.names.get(session, ""), "step": step, "folder": folder}


def queue_order_reason(decision: AdmissionDecision, identities: QueueIdentities) -> BehindHead | ReadyForAdmission:
    hold = decision.order_hold
    if isinstance(hold, NoOrderHold):
        return ReadyForAdmission()
    waiter = hold.waiter
    identity = identities.identity(waiter["pid"], cast(dict[str, object], cast(object, waiter)))
    name = waiter_unit_name(identity, waiter["pid"])
    return BehindHead(name, waiter["since"], identity["session_id"])


def queue_wait_reason(decision: AdmissionDecision, need: int, available: int,
                       identities: QueueIdentities) -> WaitReason:
    order = queue_order_reason(decision, identities)
    if not isinstance(order, ReadyForAdmission):
        return order
    if not isinstance(decision.pressure, NoPressureHold):
        return decision.pressure
    if decision.starts_at:
        return StartSpacingWait(decision.starts_at)
    if not decision.free_passes:
        return NeedsMemory(need, available, decision.threshold, decision.promised, decision.reserve)
    return ReadyForAdmission()


@dataclass(frozen=True)
class QueuedHead:
    waiter: Waiter


@dataclass(frozen=True)
class NoQueuedHead:
    pass


@dataclass(frozen=True)
class HeadPolicy:
    walk: tuple[WaiterAdmission, ...]
    queued_head: QueuedHead | NoQueuedHead
    totals: AdmissionDecision
    override: OverrideKept | OverrideEnded
    room: MemoryRoom | ReadingUnavailable


def reservation_run(row: Reservation, step: RunningStep) -> LiveBuildRun | NoLiveBuildRun:
    """Use the recorded run before the captured ancestor run."""
    return row.get("build_run", step.run)


def reservation_owner(row: Reservation | NoReservation, step: RunningStep) -> str:
    """Use the recorded run identity or the standalone reservation identity."""
    run = reservation_run(row, step) if not isinstance(row, NoReservation) else step.run
    if isinstance(run, LiveBuildRun):
        return run_owner(run.pid, run.start_time)
    return row.get("reservation_id", run_owner(row["pid"], row["start_time"])) if not isinstance(row, NoReservation) else ""


@dataclass(frozen=True)
class NoReservation:
    """This running row has no reservation."""


@dataclass(frozen=True)
class NoEndedStepSlot:
    """The ended step has no live caller-named run."""


def ended_step_slot(row: Reservation, ended_at: float,
                    policy: AdmissionPolicy) -> BetweenStepsSlot | NoEndedStepSlot:
    """Keep a live caller's accepted need during the gap after its step ends."""
    origin = row.get("run_origin", FoundRun(row.get("build_run", NoLiveBuildRun())))
    if isinstance(origin, CallerRun) and isinstance(origin.run, LiveBuildRun):
        return BetweenStepsSlot(origin.run, row.get("reservation_id", ""), row["need"], ended_at + policy.timing.slot_gap_seconds)
    return NoEndedStepSlot()


def between_steps(slot: RunSlot) -> BetweenStepsSlot:
    """Read the held slot from either slot state."""
    return slot.slot if isinstance(slot, ContinuationSlot) else slot


def slot_in_force(slot: RunSlot, now: float) -> bool:
    """Keep a claimed slot until its waiter leaves or its run ends."""
    return isinstance(slot, ContinuationSlot) or now < slot.expires_at


def test_phase_charge(phase: TestPhaseStarted) -> int:
    """Use one margin calculation for the pass and its recorded explanation."""
    return math.ceil(max(phase.peak_so_far, phase.need.bytes) * phase.need.margin)


def reservation_need(row: Reservation, reading: StepMemory) -> int:
    """Select the full step need unless captured evidence proves a test phase."""
    if isinstance(reading, ProgressAdjustedMemory):
        return reading.remaining_need
    if row["step"] == "nextest" and isinstance(reading, MeasuredMemory) and isinstance(reading.peak_history, TestPhaseStarted):
        return test_phase_charge(reading.peak_history)
    return row["need"]


@dataclass(frozen=True)
class AttributedStepUse:
    use: AnonymousRunUse | RunUseUnavailable
    owns_source: bool


def attributed_step_uses(inputs: QueueInputs) -> tuple[tuple[Reservation, RunningStep, AttributedStepUse], ...]:
    """Assign each scope and process to one step before any grouping."""
    attributed: list[tuple[Reservation, RunningStep, AttributedStepUse]] = []
    active = tuple(zip(inputs.reservations, (step for step in inputs.running if running_has_reservation(step)), strict=True))
    scope_holders: dict[Path, tuple[str, int, MeasuredMemory]] = {}
    process_holders: dict[int, tuple[int, str, int]] = {}
    for index, (row, step) in enumerate(active):
        if not isinstance(step.measured, MeasuredMemory):
            continue
        source = step.measured.anonymous_source
        key = row.get("reservation_id", run_owner(row["pid"], row["start_time"]))
        if isinstance(source, ScopeSource):
            previous_scope = scope_holders.get(source.directory)
            if previous_scope is None or key < previous_scope[0]:
                scope_holders[source.directory] = (key, index, step.measured)
        elif isinstance(source, ProcessSource):
            priority = (len(source.processes), key, index)
            for item in source.processes:
                previous_process = process_holders.get(item.pid)
                if previous_process is None or priority[:2] < previous_process[:2]:
                    process_holders[item.pid] = priority
    scopes = set(scope_holders)
    roots = {scope for scope in scopes if scopes.isdisjoint(scope.parents)}
    children: dict[Path, list[Path]] = {}
    for scope in scopes - roots:
        parent = next(parent for parent in scope.parents if parent in scopes)
        children.setdefault(parent, []).append(scope)
    scoped_pids: set[int] = set()
    for entry in scope_holders.values():
        source = entry[2].anonymous_source
        if isinstance(source, ScopeSource):
            scoped_pids.update(source.members)
    unclaimed = set(process_holders) - scoped_pids
    for index, (row, step) in enumerate(active):
        use = step.measured.anonymous if isinstance(step.measured, MeasuredMemory) else RunUseUnavailable("scope not measured")
        owns_source = True
        if isinstance(step.measured, MeasuredMemory):
            source = step.measured.anonymous_source
            if isinstance(source, ScopeSource):
                if scope_holders[source.directory][1] != index:
                    use = AnonymousRunUse(0)
                    owns_source = False
                elif isinstance(use, AnonymousRunUse):
                    nested = sum_anonymous_use(scope_holders[child][2].anonymous
                                               for child in children.get(source.directory, ()))
                    use = (AnonymousRunUse(max(0, use.bytes - nested.bytes))
                           if isinstance(nested, AnonymousRunUse) else nested)
            elif isinstance(source, ProcessSource):
                assigned = tuple(item for item in source.processes
                                 if item.pid in unclaimed and process_holders[item.pid][2] == index)
                owns_source = bool(assigned)
                use = sum_anonymous_use(item.use for item in assigned)
                unclaimed.difference_update(item.pid for item in assigned)
        attributed.append((row, step, AttributedStepUse(use, owns_source)))
    return tuple(attributed)


def run_memories(inputs: QueueInputs) -> tuple[RunMemory, ...]:
    """Group attributed steps and ephemeral handoffs inside the pure pass."""
    groups: dict[str, RunMemory] = {}
    for row, step, attributed in attributed_step_uses(inputs):
        run = reservation_run(row, step)
        owner = reservation_owner(row, step)
        need = reservation_need(row, step.measured)
        use = attributed.use
        previous = groups.get(owner)
        if previous is None:
            groups[owner] = RunMemory(owner, need, use, run=run)
        else:
            combined = sum_anonymous_use((previous.use, use))
            groups[owner] = replace(previous, need=previous.need + need, use=combined)
    for slot in inputs.slots:
        if not slot_in_force(slot, inputs.now):
            continue
        held = between_steps(slot)
        owner = run_owner(held.run.pid, held.run.start_time)
        previous = groups.get(owner, RunMemory(owner, 0, AnonymousRunUse(0), run=held.run))
        groups[owner] = replace(previous, held=previous.held + held.bytes)
    return tuple(groups.values())


PROGRESS_STEPS = ("build", "check", "clippy", "mend", "nextest", "merge-gate", "cargo")


def reservation_profile_key(row: Reservation, host: str) -> str:
    """Use the history identity for each step rather than its run owner."""
    if row["step"] == "merge-gate":
        return progress.profile_key((host, row["step"], row.get("shape", MERGE_GATE_TESTS)))
    width = row["test_threads"].threads if isinstance(row["test_threads"], RequestedTestWidth) else "unknown"
    return progress.profile_key((host, row.get("repo_identity", row["repo"]), row["step"],
        row.get("shape", ""), row.get("test_scope", "full"), row.get("package", ""),
        sorted(history_arguments(row.get("filters"))), width))


def progress_memory(row: Reservation, reading: MeasuredMemory, attributed: AttributedStepUse,
                    counter: progress.ProgressCounter | progress.NoCounter,
                    profile: progress.ProgressProfile | progress.NoMatchingProfile,
                    policy: AdmissionPolicy) -> ProgressAdjustedMemory:
    """Use a profile only with a current attributed anonymous reading."""
    full_need = reservation_need(row, reading)
    window = (progress.window_peak(profile, counter, policy.progress_lookahead_percent)
              if isinstance(attributed.use, AnonymousRunUse) else
              progress.NoMatchingProfile("anonymous use unavailable"))
    need = full_need
    if isinstance(window, progress.WindowPeak):
        basis = read_need_basis(row.get("queue_waiter", {}).get("need_basis"))
        need = shape_headroom_bytes(window.bytes, basis.factor) if isinstance(basis, (ShapeP90, ShapeLargest)) else window.bytes
        if row["step"] == "nextest" and isinstance(reading.peak_history, TestPhaseStarted):
            phase = reading.peak_history
            if isinstance(counter, progress.ProgressCounter) and counter.phase == "test:1":
                need = test_phase_charge(replace(phase, need=replace(phase.need, bytes=window.bytes)))
            else:
                window = progress.NoMatchingProfile("test phase reset or unmatched phase")
                need = full_need
    return ProgressAdjustedMemory(reading.bytes, anonymous=reading.anonymous,
        anonymous_source=reading.anonymous_source, promise=reading.promise,
        swap_bytes=reading.swap_bytes, observed_peak=reading.observed_peak,
        unknown_peak_promise=reading.unknown_peak_promise, peak_history=reading.peak_history,
        counter=counter, window=window, remaining_need=need)


def progress_text(reading: StepMemory) -> str:
    """Explain the captured progress decision without reading current files."""
    if not isinstance(reading, ProgressAdjustedMemory):
        return ""
    counter = reading.counter
    phase = (f"{counter.phase}, {counter.percent:.0f}% done" if isinstance(counter, progress.ProgressCounter)
             else counter.reason)
    reason = (f"highest anonymous use in the next {reading.window.lookahead_percent:g}%"
              if isinstance(reading.window, progress.WindowPeak) else
              f"full need: {reading.window.reason}")
    return f"{phase}; need {reading.remaining_need / GIB:.1f} GiB; {reason}"


def inputs_room(inputs: QueueInputs, policy: AdmissionPolicy) -> MemoryRoom | ReadingUnavailable:
    """Calculate room from the captured run and CI readings."""
    readings = tuple(step.measured for step in inputs.running)
    ci_use = sum(reading.bytes for reading in readings
                 if isinstance(reading, UnownedSliceMemory)
                 and not isinstance(reading.job_state, NotServiceSlice))
    return memory_room(inputs.host, run_memories(inputs), policy,
        ci_keep_aside=ci_keep_aside(readings, policy.ci_memory_margin_bytes), ci_use=ci_use)


def head_policy(inputs: QueueInputs, now: float, policy: AdmissionPolicy, next_start: NextStartAt) -> HeadPolicy:
    ordered = ranked_queue_waiters(inputs)
    candidate = first_queued_waiter(row for _, row in ordered)
    queued_head: QueuedHead | NoQueuedHead = QueuedHead(candidate.waiter) if isinstance(candidate, QueuedWaiter) else NoQueuedHead()
    room = inputs_room(inputs, policy)
    if isinstance(room, MemoryRoom):
        kept, used = room.kept, room.used
    else:
        kept, used = 0, 0
    safety = policy.safety_bytes
    totals = AdmissionDecision("admit" if isinstance(room, MemoryRoom) and isinstance(inputs.pressure, NoPressureHold) else "hold",
        kept + safety, kept, safety, used, inputs.pressure, used + kept)
    override = snapshot_head_override(inputs.override, ordered)
    outcome: OverrideKept | OverrideEnded = OverrideKept(override)
    if not isinstance(inputs.override, NoHeadAdmissionOverride) and isinstance(override, NoHeadAdmissionOverride):
        outcome = OverrideEnded(inputs.override, "lapsed")
    admissions = admission_walk(inputs, ordered, queued_head, room, now, policy, kept, used, safety, next_start)
    return HeadPolicy(tuple(admissions), queued_head, totals, outcome, room)


def start_spacing_after(now: float, policy: AdmissionPolicy) -> NextStartAt:
    return NextStartAt(now + policy.timing.recovery_start_seconds)


def admission_walk(inputs: QueueInputs, ordered: list[tuple[Path, RankedWaiter]],
                   head: QueuedHead | NoQueuedHead, room: MemoryRoom | ReadingUnavailable,
                   now: float, policy: AdmissionPolicy, kept: int, used: int, safety: int,
                   next_start: NextStartAt) -> list[WaiterAdmission]:
    """Reuse held memory first and protect the first waiting run."""
    if isinstance(room, MemoryRoom):
        remaining, readable = room.signed_room, True
    else:
        remaining, readable = 0, False
    available = inputs.host.available if isinstance(inputs.host, HostMemory) else 0
    claims = {slot.owner: slot for slot in inputs.slots if isinstance(slot, ContinuationSlot)}
    ordinary_head = first_queued_waiter(row for _, row in ordered if waiter_owner(row) not in claims)
    decisions: list[WaiterAdmission] = []
    clear = isinstance(inputs.pressure, NoPressureHold)
    due = not isinstance(inputs.spacing, NextStartAt) or now >= inputs.spacing.at
    head_owner = waiter_owner(head.waiter) if isinstance(head, QueuedHead) else ""
    ordinary_row = stored_waiter(ordinary_head.waiter) if isinstance(ordinary_head, QueuedWaiter) else NoWaiter()
    ordinary_owner = waiter_owner(ordinary_row) if not isinstance(ordinary_row, NoWaiter) else ""
    ordinary_need = ordinary_row["need"] if not isinstance(ordinary_row, NoWaiter) else 0
    has_override = not isinstance(inputs.override, NoHeadAdmissionOverride)
    no_runs = not inputs.reservations and not inputs.slots
    new_started = False
    protected_extra = 0
    protected_claim: Waiter | NoWaiter = NoWaiter()
    for _, row in ordered:
        owner = waiter_owner(row)
        claim = claims.get(owner)
        held = claim.slot.bytes if claim is not None else 0
        need = row["need"]
        extra = max(0, need - held)
        reused = claim is not None
        within = reused and need <= held
        state = waiter_state(row)
        fresh = not estimate_expired(row, now, policy)
        first = head_owner == owner
        alone = first and no_runs
        protected_need = ordinary_need if ordinary_owner != owner and not reused else 0
        protected_need += protected_extra
        checked_room = remaining - protected_need
        override = first and has_override
        if override:
            checked_room = available - safety
        fits = extra <= checked_room
        basis: StartBasis = (WithinSlot() if within else OverrideRoom() if override and fits else
                             Fits() if fits else StartsAlone() if alone else DoesNotFit())
        room_passes = not isinstance(basis, DoesNotFit)
        spacing = (inputs.spacing.at if isinstance(inputs.spacing, NextStartAt) and not due else
                   next_start.at if new_started else 0.0)
        can_start = (within or readable and clear and room_passes) and fresh
        can_start = can_start and isinstance(state, QueuedWaiter) and (reused or due and not new_started)
        order_hold: NoOrderHold | HeldBehindHead = NoOrderHold()
        if not reused and protected_need and not fits:
            if not isinstance(protected_claim, NoWaiter):
                order_hold = HeldBehindHead(protected_claim)
            elif not isinstance(ordinary_row, NoWaiter):
                order_hold = HeldBehindHead(ordinary_row)
        decision = AdmissionDecision("admit" if can_start else "hold",
            extra + kept + safety, kept, safety, used,
            NO_PRESSURE_HOLD if within else inputs.pressure,
            used + kept,
            room_passes, order_hold, 0.0 if reused else spacing,
            reused_slot=reused, room=RoomCheck(extra, checked_room, room_passes, basis=basis))
        decisions.append(WaiterAdmission(row, decision, state))
        if can_start:
            remaining += held - need
            if not reused:
                new_started = True
        elif reused:
            protected_extra += extra
            protected_claim = stored_waiter(row)
    return decisions


def wait_holder_identity(session_id: str, session_name: str, unit: str) -> records.WaitHolder:
    """Prefer the recorded session, then the recorded unit."""
    if session_id:
        return records.SessionHolder(session_id, session_name)
    if unit:
        return records.UnitHolder(unit)
    return records.NoHolder()


def admission_wait_holder(decision: AdmissionDecision, reason: WaitReason,
                          inputs: QueueInputs, room: MemoryRoom | ReadingUnavailable) -> records.WaitHolder:
    """Name the recorded order hold or the largest grouped charge."""
    cause = reason.cause if isinstance(reason, HeadAdmissionStatus) else reason
    if (decision.state != "hold" or not isinstance(room, MemoryRoom) or not isinstance(decision.pressure, NoPressureHold)
            or decision.starts_at or not isinstance(cause, (BehindHead, NeedsMemory))):
        return records.NoHolder()
    if isinstance(decision.order_hold, HeldBehindHead):
        waiter = decision.order_hold.waiter
        return wait_holder_identity(waiter.get("session_id", ""), waiter.get("session_name", ""), waiter.get("unit", ""))
    if not room.runs:
        return records.NoHolder()
    largest = max(room.runs, key=lambda held: (
        held.bytes + (held.memory.use.bytes if isinstance(held.memory.use, AnonymousRunUse) else 0),
        held.memory.owner))
    identities: list[tuple[str, str, str]] = []
    reservations = iter(inputs.reservations)
    for step in inputs.running:
        row = next(reservations) if running_has_reservation(step) else NoReservation()
        if reservation_owner(row, step) == largest.memory.owner:
            identities.append((step.session_id, step.session_name, step.unit))
    if not identities:
        return records.NoHolder()
    session_id, session_name, unit = max(identities, key=lambda identity: (bool(identity[0]), bool(identity[2]), identity))
    return wait_holder_identity(session_id, session_name, unit)


def admission_pass(inputs: QueueInputs, now: float, policy: AdmissionPolicy) -> PassResult:
    next_start = start_spacing_after(now, policy)
    resolved = head_policy(inputs, now, policy, next_start)
    candidate = resolved.queued_head
    head_owner = waiter_owner(candidate.waiter) if isinstance(candidate, QueuedHead) else ""
    available = inputs.host.available if isinstance(inputs.host, HostMemory) else 0
    rows: list[PassRow] = []
    endings: tuple[PassResult, ...] = ()
    rank = 0
    for entry in resolved.walk:
        row, decision = entry.waiter, entry.decision
        owner = waiter_owner(row)
        first = head_owner == owner
        within = isinstance(decision.room.basis, WithinSlot)
        if isinstance(entry.state, TokenWaitingWaiter):
            reason: WaitReason = entry.state
        else:
            if isinstance(inputs.host, ReadingUnavailable) and not within:
                cause: HeadCause = inputs.host
            elif estimate_expired(row, now, policy):
                cause = EstimateExpired(row.get("estimated_at", now))
            else:
                cause = cast(HeadCause, queue_wait_reason(decision, row["need"], available, RecordedQueueIdentities()))
            reason = HeadAdmissionStatus(decision, row["need"], available, cause=cause) if first else cause
        proposal: ProposedAdmission | NotAdmitted = NotAdmitted()
        if decision.state == "admit":
            spacing = inputs.spacing if decision.reused_slot else next_start
            outcome = (OverrideEnded(inputs.override, "admitted") if first
                       and not isinstance(inputs.override, NoHeadAdmissionOverride) else resolved.override)
            rank += 1
            proposal = ProposedAdmission(rank, spacing, outcome)
        blocker: BlockingIndex | NoSingleBlocker | BlockerNotRequested = BlockerNotRequested()
        if owner in inputs.blockers_for and decision.state == "hold" and isinstance(entry.state, QueuedWaiter):
            if not endings:
                endings = tuple(admission_pass(inputs_after_steps_end(inputs, frozenset((ended,)), policy), now, policy)
                                for ended in range(len(inputs.running)))
            blocker = blocking_index(owner, endings)
        rows.append(PassRow(owner, decision, reason, blocker, proposal,
                            admission_wait_holder(decision, reason, inputs, resolved.room)))
    return PassResult(tuple(rows), resolved)

def released_resident_bytes(running: tuple[RunningStep, ...], ended: frozenset[int]) -> int:
    """Only measured resident memory of ended rows becomes available RAM."""
    return sum(step.measured.bytes
               for index, step in enumerate(running) if index in ended
               and isinstance(step.measured, MeasuredMemory))


def running_has_reservation(step: RunningStep) -> bool:
    """Identify running rows that consume one reservation in the same order."""
    return not isinstance(step.measured, (CompilerServiceMemory, UnownedSliceMemory))


def inputs_after_steps_end(inputs: QueueInputs, ended: frozenset[int], policy: AdmissionPolicy) -> QueueInputs:
    """Keep the same evidence, removing ended rows and returning their resident bytes."""
    freed = released_resident_bytes(inputs.running, ended)
    host = replace(inputs.host, available=inputs.host.available + freed) if isinstance(inputs.host, HostMemory) else inputs.host
    reservations: list[Reservation] = []
    slots = list(inputs.slots)
    reservation_index = 0
    for index, step in enumerate(inputs.running):
        if running_has_reservation(step):
            reservation = inputs.reservations[reservation_index]
            if index not in ended:
                reservations.append(reservation)
            else:
                slot = ended_step_slot(reservation, inputs.now, policy)
                if isinstance(slot, BetweenStepsSlot):
                    slots.append(slot)
            reservation_index += 1
    return replace(inputs, host=host,
        running=tuple(step for index, step in enumerate(inputs.running) if index not in ended),
        reservations=tuple(reservations), slots=tuple(slots), blockers_for=frozenset())


def blocking_index(owner: str, endings: tuple[PassResult, ...]) -> BlockingIndex | NoSingleBlocker:
    """Find a unique ending among scenarios shared by every requested row."""
    blockers = [index for index, result in enumerate(endings)
                if any(row.owner == owner and isinstance(row.admission, ProposedAdmission) for row in result.rows)]
    return BlockingIndex(blockers[0]) if len(blockers) == 1 else NoSingleBlocker()


def blocker_identity(index: BlockingIndex | NoSingleBlocker | BlockerNotRequested,
                     reservations: list[Reservation], processes: dict[int, ProcessIdentity], proc: Path) -> Blocker:
    """Resolve a pass's unique blocker only at the process/sidecar boundary."""
    if not isinstance(index, BlockingIndex):
        return NoSingleBlocker()
    if index.index >= len(reservations):
        return UnitBlocker("sccache.service")
    row = reservations[index.index]
    identity = QueueIdentityReader(processes, proc).identity(row["pid"], cast(dict[str, object], cast(object, row)))
    if identity["session_id"]:
        return ReservationBlocker(identity["session_id"], identity["session_name"])
    if row["sidecar"]:
        try:
            unit = Path(Path(row["sidecar"]).read_text().strip()).name
            if unit.endswith((".scope", ".service")):
                return UnitBlocker(unit)
        except OSError:
            pass
    return NoSingleBlocker()


NO_MEMORY_READINGS: Mapping[Path, StepMemory] = {}


@dataclass(frozen=True)
class ReadingNotTaken:
    pass


READING_NOT_TAKEN = ReadingNotTaken()


class RecordedQueueIdentities:
    """Identity evidence already resolved by gather_queue_inputs."""

    def identity(self, _pid: int, row: dict[str, object]) -> WaiterIdentity:
        identity: WaiterIdentity = {"session_id": str(row.get("session_id", "")),
            "session_name": str(row.get("session_name", "")),
            "step": str(row.get("step", "")), "folder": str(row.get("folder", ""))}
        if row.get("unit"):
            identity["unit"] = str(row["unit"])
        return identity


class QueueIdentities(Protocol):
    def identity(self, pid: int, row: dict[str, object], /) -> WaiterIdentity: ...


def decision_checks(decision: AdmissionDecision, need: int, available: int) -> tuple[RoomCheck, FreeCheck]:
    return (decision.room,
            FreeCheck(available, need, decision.reserve, decision.promised, decision.threshold, decision.free_passes))


def queue_read_from(inputs: QueueInputs, policy: AdmissionPolicy) -> QueueRead:
    return queue_read_from_pass(inputs, policy, admission_pass(inputs, inputs.now, policy))


def queue_read_from_pass(inputs: QueueInputs, policy: AdmissionPolicy,
                         result: PassResult) -> QueueRead:
    ordered = ranked_queue_waiters(inputs)
    candidate = result.policy.queued_head
    identities = RecordedQueueIdentities()
    available = inputs.host.available if isinstance(inputs.host, HostMemory) else 0
    totals = result.policy.totals
    explained: list[ExplainedRunningStep] = []
    reservations = iter(inputs.reservations)
    for step in inputs.running:
        reading = step.measured
        admitted_at: float | AdmissionTimeNotRecorded = AdmissionTimeNotRecorded()
        need = 0
        owner = reservation_owner(NoReservation(), step)
        if running_has_reservation(step):
            row = next(reservations)
            admitted_at = epoch(row["admitted_at"])
            need = reservation_need(row, reading)
            owner = reservation_owner(row, step)
        measured = reading.bytes if isinstance(reading, (MeasuredMemory, CompilerServiceMemory, UnownedSliceMemory)) else 0
        charge = max(need, measured)
        explained.append(ExplainedRunningStep(step, admitted_at, need, measured, charge, owner=owner))
    waiting: list[WaitingStep] = []
    first: HeadExplanation | TokenWaitersOnly | NoWaiter = NoWaiter()
    line: list[LineWaiter] = []
    for (_, row), admission in zip(ordered, result.rows, strict=True):
        own = admission.decision
        identity = identities.identity(row["pid"], cast(dict[str, object], cast(object, row)))
        step = WaitingStep(row["pid"], identity["session_id"], identity["session_name"], identity["step"],
                           identity["folder"], row["since"], row["need"], admission.reason, row.get("unit", ""),
                           project=step_project(row.get("repository_name", "")),
                           run=row.get("build_run", NoLiveBuildRun()))
        waiting.append(step)
        if isinstance(candidate, QueuedHead) and waiter_owner(row) == waiter_owner(candidate.waiter):
            width = row.get("test_threads")
            source: RecordedNeed | NeedSourceNotRecorded = NeedSourceNotRecorded()
            if "need_source" in row:
                source = RecordedNeed(row["need_source"], row.get("need_runs", 0), HISTORY_DAYS,
                    row.get("test_scope", "full"), row.get("package", ""), tuple(row.get("filters", [])),
                    width if width is not None else WidthNotRecorded(), read_need_basis(row.get("need_basis")))
            first = HeadExplanation(step, source, own, *decision_checks(own, step.need, available),
                                    isinstance(waiter_priority(row), PriorityUnit),
                                    cause=cast(HeadAdmissionStatus, admission.reason).cause)
        else:
            line.append(LineWaiter(step, own.state == "admit", own, *decision_checks(own, step.need, available)))
    if waiting and isinstance(first, NoWaiter):
        first = TokenWaitersOnly()
    view = QueueView(tuple(waiting), inputs.running, available, totals.promised, totals.reserve, inputs.now)
    host = (HostAvailable(inputs.host.available, inputs.host.total, policy.safety_bytes, inputs.pressure)
            if isinstance(inputs.host, HostMemory) else inputs.host)
    explanation = QueueExplanation(inputs.now, host, tuple(explained),
        QueueMemoryTotals(sum(item.charged for item in explained), sum(item.measured for item in explained),
                          sum(max(0, item.charged - item.measured) for item in explained)), first,
        WaitingLine(tuple(line), len(line), sum(item.fits_now for item in line),
                    min((item.waiter.need for item in line), default=0), max((item.waiter.need for item in line), default=0),
                    min((item.waiter.since for item in line), default=inputs.now)),
        inputs, tuple(row for _, row in ordered), inputs.reservations,
        result.policy.override.record if isinstance(result.policy.override, OverrideKept) else NoHeadAdmissionOverride(),
        inputs.spacing, inputs.priority, room=result.policy.room, policy=policy)
    return QueueRead(view, explanation, ordered)


@dataclass(frozen=True)
class ProcessesNotRead:
    pass


@dataclass
class QueueSnapshotReadings:
    policy: AdmissionPolicy = field(kw_only=True)
    processes: dict[int, ProcessIdentity] | ProcessesNotRead
    memory: dict[Path, StepMemory]
    host: HostMemory | ReadingUnavailable | ReadingNotTaken = ReadingNotTaken()
    pressure: Pressure | ReadingNotTaken = ReadingNotTaken()
    spacing: StartSpacing | ReadingNotTaken = ReadingNotTaken()
    compilers: list[StepMemory] | ReadingNotTaken = ReadingNotTaken()
    order: QueueOrderContext | QueueOrderNotResolved = QUEUE_ORDER_NOT_RESOLVED


def step_matches(selector: str, step: WaitingStep | RunningStep) -> bool:
    return selector in (step.session_id, step.session_name, step.folder, Path(step.folder).name)


def match_waiters(selector: str, view: QueueView) -> list[int]:
    if selector.isdecimal():
        place = int(selector)
        return [place] if 1 <= place <= len(view.waiting) else []
    return [place for place, step in enumerate(view.waiting, 1) if step_matches(selector, step)]


def queue_view() -> QueueView:
    return queue_read().view


def explain_queue() -> QueueExplanation:
    """Explain admission under the queue's shared lock without history or SQLite reads."""
    return queue_read().explanation


def head_after_steps_end(explanation: QueueExplanation, ended: tuple[int, ...]) -> HeadAfterEnd:
    """Reapply admission to this snapshot after the named running rows end; read no files."""
    head = explanation.head
    if isinstance(head, (NoWaiter, TokenWaitersOnly)):
        return head
    indices = frozenset(ended)
    released = [row for index, row in enumerate(explanation.running) if index in indices]
    inputs = inputs_after_steps_end(explanation.decided_from, indices, explanation.policy)
    result = admission_pass(inputs, explanation.now, explanation.policy)
    candidate = result.policy.queued_head
    if isinstance(candidate, NoQueuedHead):
        return NoWaiter()
    owner = waiter_owner(candidate.waiter)
    row = next(row for row in result.rows if row.owner == owner)
    cause = cast(HeadAdmissionStatus, row.reason).cause
    if isinstance(cause, (EstimateExpired, ReadingUnavailable)):
        return cause
    decision = row.decision
    freed = released_resident_bytes(explanation.decided_from.running, indices)
    before_room = explanation.room
    after_room = result.policy.room
    growth_drop = (before_room.kept - after_room.kept
                   if isinstance(before_room, MemoryRoom) and isinstance(after_room, MemoryRoom) else 0)
    charge_drop = sum(row.charged for row in released)
    if isinstance(cause, ReadyForAdmission):
        return HeadAdmitsAfterEnd(ended, decision, charge_drop, freed, growth_drop)
    if isinstance(cause, StartSpacingWait):
        return HeadStartsAt(cause.at, ended, charge_drop, freed, growth_drop)
    if isinstance(cause, NeedsMemory):
        return HeadStillHeld(decision, ("free memory",))
    if isinstance(cause, (MemoryPressure, PressureUnavailable)):
        return HeadStillHeld(decision, ("pressure",))
    return HeadStillHeld(decision, ("queue order",))


def smallest_head_release(explanation: QueueExplanation) -> HeadAfterEnd:
    """Fewest endings, then least charge, then running-row order, using admission's verdict."""
    current = head_after_steps_end(explanation, ())
    if not isinstance(current, HeadStillHeld):
        return current
    count = len(explanation.running)
    all_ended = head_after_steps_end(explanation, tuple(range(count)))
    if not isinstance(all_ended, HeadRelease):
        return all_ended
    head = explanation.head
    if not isinstance(head, HeadExplanation):
        return current
    # Use captured room for the bound. Rendered free checks can retain older evidence.
    # A branch that ends every admitted step can use the alone rule.
    free_gap = (head.waiter.need - explanation.room.signed_room
                if isinstance(explanation.room, MemoryRoom) else 0)
    charges = [row.charged for row in explanation.running]
    free_releases: list[int] = []
    for index in range(count):
        hypothetical = inputs_after_steps_end(explanation.decided_from, frozenset((index,)), explanation.policy)
        after_room = inputs_room(hypothetical, explanation.policy)
        free_releases.append(after_room.signed_room - explanation.room.signed_room
            if isinstance(after_room, MemoryRoom) and isinstance(explanation.room, MemoryRoom) else 0)
    builds = frozenset(index for index, row in enumerate(explanation.running)
                       if running_has_reservation(row.step))
    min_charge: list[list[int]] = []
    max_free: list[list[int]] = []
    for start in range(count + 1):
        charge_order = sorted(charges[start:])
        free_order = sorted(free_releases[start:], reverse=True)
        min_charge.append([sum(charge_order[:size]) for size in range(count - start + 1)])
        max_free.append([sum(free_order[:size]) for size in range(count - start + 1)])
    for size in range(1, count + 1):
        best: HeadRelease | HeadStillHeld = current
        def search(start: int, slots: int, selected: tuple[int, ...], charge: int, freed: int) -> None:
            nonlocal best
            if slots > count - start:
                return
            if isinstance(best, HeadRelease) and charge + min_charge[start][slots] >= best.charge_drop:
                return
            missing_builds = builds.difference(selected)
            can_empty = len(missing_builds) <= slots and all(index >= start for index in missing_builds)
            if not can_empty and freed + max_free[start][slots] < free_gap:
                return
            if slots == 0:
                result = head_after_steps_end(explanation, selected)
                if isinstance(result, HeadRelease):
                    best = result
                return
            for index in range(start, count - slots + 1):
                search(index + 1, slots - 1, (*selected, index), charge + charges[index], freed + free_releases[index])
        search(0, size, (), 0, 0)
        if isinstance(best, HeadRelease):
            return best
    return all_ended


def read_test_phase_charge_use(path: Path, reservation_id: str) -> TestPhaseChargeUse | NoTestPhaseChargeUse:
    """Read saved decision evidence; absence never becomes inferred use."""
    try:
        raw = cast(object, json.loads(path.with_suffix(".test-phase-charge.json").read_text()))
        if not isinstance(raw, dict):
            return NoTestPhaseChargeUse()
        evidence = parse_test_phase_charge_use(cast(dict[str, object], raw))
        need = read_test_phase_need(evidence["need"])
        if evidence["reservation_id"] == reservation_id and isinstance(need, TestPhaseNeed):
            return {**evidence, "need": test_phase_need_record(need)}
    except (OSError, ValueError, TypeError):
        pass
    return NoTestPhaseChargeUse()


class FinalizationIntents(Protocol):
    def save(self, row: Reservation, intent: FinalizationIntent) -> None: ...
    def published(self, row: Reservation) -> None: ...
    def clear(self, row: Reservation) -> None: ...


def finalize_reservation(path: Path, row: Reservation, outcome: FinalOutcome, now: datetime,
                         status: int | OutcomeNotRecorded = OUTCOME_NOT_RECORDED, *,
                         intents: FinalizationIntents,
                         end_kind: StepEnd | TokenReturn) -> HistoryRecorded | NoHistoryRecorded:
    """Save the peak before removing evidence; the service owns reservation state."""
    maximum = path.with_suffix(".max")
    history_path = path.parent / "anon_peaks.jsonl"
    recovering = "finalization" in row
    if not recovering:
        try:
            peak = int(maximum.read_text())
        except (FileNotFoundError, ValueError):
            peak = 0
        intent: FinalizationIntent = {
            "reservation_id": row.get("reservation_id") or uuid.uuid4().hex,
            "peak": peak,
            "scoped": row.get("scoped", bool(row["sidecar"] and Path(row["sidecar"]).is_file())),
            "outcome": ("completed" if isinstance(outcome, Completed) else "failed" if isinstance(outcome, Failed)
                        else "killed" if isinstance(outcome, Killed) else "owner_gone"),
            "ended_at": store.utc_iso(now.timestamp()),
            "end_kind": "token_return" if isinstance(end_kind, TokenReturn) else "step_end",
        }
        if "queue_waiter" in row:
            waiter = row["queue_waiter"]
            intent["queue_identity"] = queue_life_identity(waiter)
        if isinstance(status, int):
            intent["status"] = status
        phase = observed_test_phase(row)
        if isinstance(phase, ObservedTestPhase):
            intent["test_phase_peak_bytes"] = phase.peak_bytes
            intent["test_phase_started_at"] = phase.started_at
        if "test_phase_need" in row:
            intent["test_phase_need"] = test_phase_need_record(read_test_phase_need(row["test_phase_need"]))
        charge_use = row.get("test_phase_charge_use", NoTestPhaseChargeUse())
        if isinstance(charge_use, dict):
            intent["test_phase_charge_use"] = charge_use
        if peak > 0 and intent["scoped"]:
            intent["history_start"] = store.read_last_whole_row_end(history_path)
        row["finalization"] = intent
        intents.save(row, intent)
    intent = row["finalization"]
    result: HistoryRecorded | NoHistoryRecorded = NoHistoryRecorded()
    if intent["peak"] > 0 and intent["scoped"]:
        # The store repairs an unfinished tail before the identity lookup.
        with store.locked_append(history_path) as handle:
            recorded = recovering and any(previous.get("reservation_id") == intent["reservation_id"]
                                          for previous in store.whole_rows(handle, intent.get("history_start", 0)))
            if not recorded:
                history: dict[str, object] = {
                    "host": store.host_name(), "repo": row["repo"], "step": row["step"],
                    "reservation_id": intent["reservation_id"],
                    "worktree": row["worktree"], "anon_peak_bytes": intent["peak"], "outcome": intent["outcome"], "need": row["need"],
                    "test_threads": row["test_threads"].threads if isinstance(row["test_threads"], RequestedTestWidth) else None,
                    "call_id": row["call_id"].call_id if isinstance(row["call_id"], CallRecord) else None,
                    "repo_identity": row.get("repo_identity"), "test_scope": row.get("test_scope", "full"),
                    "package": row.get("package", ""), "filters": row.get("filters", []),
                    "ended_at": intent["ended_at"],
                }
                if "status" in intent:
                    history["status"] = intent["status"]
                if "shape" in row:
                    history["shape"] = row["shape"]
                for name in ("test_phase_peak_bytes", "test_phase_started_at", "test_phase_need", "test_phase_charge_use"):
                    if name in intent:
                        history[name] = cast(Mapping[str, object], intent)[name]
                store.append_locked(handle, history)
        result = HistoryRecorded()
        intents.published(row)
    if "queue_identity" in intent:
        ended_at = epoch(intent["ended_at"])
        event: EndedEvent = {
            **lifecycle_event(intent["queue_identity"], "ended", intent["reservation_id"]),
            "reservation_id": intent["reservation_id"], "ended_at": ended_at, "outcome": intent["outcome"],
            "end_kind": TokenReturn() if intent.get("end_kind") == "token_return" else StepEnd(),
            "peak": PeakSampled(intent["peak"]) if intent["peak"] > 0 else NoPositiveSample(),
        }
        append_queue_time(path.parent, cast(dict[str, object], codec.snapshot_value(event)), ended_at)
    maximum.unlink(missing_ok=True)
    path.with_suffix(".wave").unlink(missing_ok=True)
    if row["sidecar"]:
        sidecar = Path(row["sidecar"])
        for sample in (progress.progress_path(sidecar, "json"), progress.progress_path(sidecar, "log"), sidecar):
            try:
                sample.unlink(missing_ok=True)
            except PermissionError:
                pass
    intents.clear(row)
    return result


def main() -> None:
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "tripwire":
            check_memory_reading(Path(sys.argv[2]))
        else:
            raise ValueError("admission actions use admission_client.py")
    except LiveMemoryReading as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()


def step_project(repository_name: str, repo: str = "") -> str:
    """Use a recorded project name without showing an absolute repository path."""
    name = repository_name or repo
    return "" if Path(name).is_absolute() else name


def reservation_identity(row: Reservation) -> WaiterIdentity:
    """Keep recorded holder names when a legacy row has no queue waiter."""
    identity = RecordedQueueIdentities().identity(row["pid"],
        cast(dict[str, object], cast(object, row.get("queue_waiter", row))))
    return {**identity, "step": identity["step"] or row["step"],
            "folder": identity["folder"] or row["worktree"]}


def build_queue_inputs(ordered: list[tuple[Path, RankedWaiter]],
                       reservations: list[tuple[Reservation, StepMemory]],
                       readings: QueueSnapshotReadings, override: HeadAdmissionOverride | NoHeadAdmissionOverride,
                       now: float, priority: QueuePriority | NoQueuePriority = NO_QUEUE_PRIORITY,
                       unowned: tuple[UnownedSliceMemory, ...] = ()) -> QueueInputs:
    """Build queue evidence from supplied readings without external reads."""
    if (not isinstance(readings.host, (HostMemory, ReadingUnavailable))
            or isinstance(readings.pressure, ReadingNotTaken)
            or isinstance(readings.spacing, ReadingNotTaken) or isinstance(readings.compilers, ReadingNotTaken)):
        raise ValueError("queue inputs require complete pass readings")
    running: list[RunningStep] = []
    rows: list[Reservation] = []
    for row, memory in reservations:
        if isinstance(memory, GoneMemory):
            continue
        identity = reservation_identity(row)
        running.append(RunningStep(identity["session_id"], identity["session_name"],
                                   identity["step"], identity["folder"], row["need"], memory,
                                   run=row.get("build_run", NoLiveBuildRun()), started_at=epoch(row["admitted_at"]),
                                   unit=identity.get("unit", ""), pid=row["pid"],
                                   project=step_project(row.get("queue_waiter", {}).get("repository_name", ""), row["repo"])))
        rows.append(row)
    running.extend(RunningStep("", "sccache.service", "compile", "builds.slice", 0, value,
                   pid=value.pid if isinstance(value, CompilerServiceMemory) else PidNotRecorded())
                   for value in readings.compilers)
    for value in unowned:
        activity = value.activity
        if not isinstance(value.job_state, NotServiceSlice):
            running.append(RunningStep("", value.owner, "service", value.cgroup, 0, value,
                pid=value.pid,
                started_at=value.job_state.started_at if isinstance(value.job_state, RunningServiceJob) else 0.0))
            continue
        running.append(RunningStep(
            activity.session_id if isinstance(activity, ScopeProcesses) else "",
            activity.session_name if isinstance(activity, ScopeProcesses) else "",
            "between steps" if isinstance(activity, ScopeProcesses) and isinstance(activity.run, LiveBuildRun) else "unowned",
            activity.folder if isinstance(activity, ScopeProcesses) else value.cgroup, 0, value,
            pid=value.pid,
            run=activity.run if isinstance(activity, ScopeProcesses) else NoLiveBuildRun(),
            started_at=activity.started_at if isinstance(activity, ScopeProcesses) else 0.0))
    return QueueInputs(sorted(ordered, key=lambda item: waiter_order(item[1])), tuple(running), tuple(rows),
                       readings.host, readings.pressure, readings.spacing, override, now, priority)


def inputs_after_reservations_end(inputs: QueueInputs, live_reservations: set[str]) -> QueueInputs:
    """Remove ended reservations by identity without reading memory or configuration."""
    paired = {row.get("reservation_id", ""): step for row, step in zip(
        inputs.reservations, (step for step in inputs.running if running_has_reservation(step)), strict=True)}
    kept_steps = {id(step) for reservation, step in paired.items() if reservation in live_reservations}
    return replace(inputs,
        reservations=tuple(row for row in inputs.reservations if row.get("reservation_id") in live_reservations),
        running=tuple(step for step in inputs.running if not running_has_reservation(step) or id(step) in kept_steps))


def ci_slice_cgroup(name: str) -> Path | ReadingUnavailable:
    private = os.environ.get("BUILDLOG_CI_CGROUP", "")
    if private:
        return Path(private)
    try:
        reply = subprocess.run(["systemctl", "show", "--property=ControlGroup", "--value", name],
                               capture_output=True, text=True, check=True, timeout=5)
        group = reply.stdout.strip()
        if not group.startswith("/") or group == "/" or ".." in Path(group).parts:
            raise ValueError("systemd returns no slice cgroup")
        return Path("/sys/fs/cgroup") / group.lstrip("/")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return ReadingUnavailable(f"{name}: cannot read ControlGroup: {error}")

def slice_working_set(directory: Path) -> MeasuredMemory:
    """Measure resident memory without inactive file cache and record swap separately."""
    current = directory / "memory.current"
    check_memory_reading(current)
    amount = int(current.read_text())
    stat = directory / "memory.stat"
    check_memory_reading(stat)
    contents = stat.read_text()
    try:
        fields = dict(line.split() for line in contents.splitlines())
        inactive_file = int(fields["inactive_file"])
    except (KeyError, ValueError) as error:
        raise ValueError(f"{stat}: expected inactive_file bytes") from error
    swap = directory / "memory.swap.current"
    check_memory_reading(swap)
    swapped = int(swap.read_text()) if swap.exists() else 0
    try:
        anon = int(fields["anon"])
        if anon < 0:
            raise ValueError("negative anon")
        use: AnonymousRunUse | RunUseUnavailable = AnonymousRunUse(anon)
    except (KeyError, ValueError):
        use = RunUseUnavailable(f"{stat}: anonymous memory unavailable")
    return MeasuredMemory(max(0, amount - inactive_file), swap_bytes=swapped, anonymous=use)


def unowned_slice_memory(rows: list[Reservation], processes: dict[int, ProcessIdentity], proc: Path,
                         *, activity_reader: ScopeActivityReader | None = None,
                         ci_memory_slice: str = "") -> tuple[UnownedSliceMemory, ...]:
    """Count child scopes that no reservation owns, without duplicate process charges."""
    directory = Path(os.environ.get("BUILDLOG_BUILDS_CGROUP",
        f"/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/builds.slice"))
    owned: set[Path] = set()
    tree_pids: set[int] = set()
    for row in rows:
        try:
            scope = Path(row["sidecar"]).read_text().strip() if row["sidecar"] else ""
        except OSError:
            scope = ""
        if scope:
            owned.add(Path(scope))
        else:
            tree_pids.update(process_tree({row["pid"]: row["start_time"]}, processes))
    result: list[UnownedSliceMemory] = []
    activity_reader = activity_reader or ScopeActivityReader(processes, proc)
    try:
        children = list(directory.iterdir())
    except OSError:
        children = []
    ci_group = ci_slice_cgroup(ci_memory_slice) if ci_memory_slice else ReadingUnavailable("CI slice not configured")
    ci_groups = (ci_group,) if isinstance(ci_group, Path) else ()
    if ci_memory_slice and isinstance(ci_group, ReadingUnavailable):
        result.append(UnownedSliceMemory(0, pid=PidNotRecorded(), owner=ci_memory_slice, diagnostic=ci_group.reason))
    children.extend(group for group in ci_groups if group not in children)
    for child in children:
        required = child in ci_groups
        if not required and (child in owned or child.name == "sccache.service" or not child.is_dir()):
            continue
        try:
            working_set = slice_working_set(child)
            amount = working_set.bytes
            swapped = working_set.swap_bytes
            members = scope_members(child)
            duplicate = process_memory(members & tree_pids, proc)
            duplicate_use = (duplicate.anonymous.bytes if required
                and isinstance(duplicate.anonymous, AnonymousRunUse) else duplicate.bytes)
            amount = max(0, amount - duplicate_use)
            swapped = max(0, swapped - duplicate.swap_bytes)
            promise: MemoryPromise = NoPromise()
            activity = activity_reader.scope(members, child.name)
            row_pid: int | PidNotRecorded = (activity.members[0].pid
                if isinstance(activity, ScopeProcesses) and activity.members else PidNotRecorded())
            job_state: RunningServiceJob | IdleService | NotServiceSlice = NotServiceSlice()
            if required:
                job_state = IdleService()
                programs = member_programs(members.intersection(processes), proc)
                workers = [pid for pid, program in programs.items() if program == "Runner.Worker"]
                listeners = [pid for pid, program in programs.items() if program == "Runner.Listener"]
                candidates = workers or listeners
                if candidates:
                    oldest = min(candidates, key=lambda pid: (int(processes[pid].start), pid))
                    row_pid = oldest
                    if workers:
                        unregistered = [pid for pid in workers
                            if not process_tree({pid: processes[pid].start}, processes).intersection(tree_pids)]
                        if unregistered:
                            first = min(unregistered, key=lambda pid: (int(processes[pid].start), pid))
                            job_state = RunningServiceJob(activity_reader.started_at(first))
            if required or amount or members:
                result.append(UnownedSliceMemory(amount, promise=promise,
                                                 swap_bytes=swapped,
                                                 cgroup=str(child), owner=child.name,
                                                 activity=activity, pid=row_pid,
                                                 job_state=job_state))
        except (OSError, ValueError) as error:
            if required:
                result.append(UnownedSliceMemory(0, pid=PidNotRecorded(), cgroup=str(child),
                                                 owner=ci_memory_slice, diagnostic=str(error)))
            continue
    return tuple(result)


def sampling_context(row: Reservation) -> dict[str, object]:
    """Copy the grant context for the build-log sampler."""
    return {"reservation_id": row.get("reservation_id", ""), "need": row["need"],
            "repo_identity": row.get("repo_identity", ""), "package": row.get("package", ""),
            "filters": row.get("filters", []), "test_scope": row.get("test_scope", "full"),
            "test_threads": codec.snapshot_value(row["test_threads"]),
            "call_id": codec.snapshot_value(row["call_id"]),
            "test_phase_need": test_phase_need_record(read_test_phase_need(row.get("test_phase_need"))),
            "test_phase_charge_use": codec.snapshot_value(row.get("test_phase_charge_use", NoTestPhaseChargeUse()))}


class QueueServiceReader(Protocol):
    def view(self) -> QueueRead: ...


def queue_read() -> QueueRead:
    client = cast(QueueServiceReader, cast(object, importlib.import_module("scripts.build_queue.admission_client")))
    return client.view()
