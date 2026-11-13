"""Shared admission records, configuration and wait phrases."""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
import math
import os
import re
from types import MappingProxyType
from typing import Callable, Literal, NotRequired, TypedDict, assert_never, cast
from zoneinfo import ZoneInfo

import sys
from pathlib import Path


from scripts.build_queue import progress_profile as progress
from scripts.build_queue import placement
from scripts.build_queue.scope_activity import (EmptyBuildScope, IdleService, LiveBuildRun, NoLiveBuildRun,
    NotServiceSlice, RunningServiceJob, ScopeProcesses)

from scripts.buildlog import parse
from scripts.lib import conf
from scripts.lib.test_phase import (CallRecord as CallRecord, NoCallRecord as NoCallRecord,
                                   TestPhaseChargeUse as TestPhaseChargeUse)

GIB = 1 << 30


@dataclass(frozen=True)
class SavedPlacementAssignment:
    assignment: placement.PlacementAssignment
    state: placement.AssignmentState = placement.CurrentAssignment()
    permission: placement.LaunchPermission = placement.PermissionAvailable()
    provider: placement.ProviderClaimReference | placement.NoProviderClaim = placement.NoProviderClaim()

# Historical multiplier for saved dev bases from the Phase 24 replay.
DEV_HEADROOM = 4.2034275337646125

TestScope = Literal["full", "filtered"]


@dataclass(frozen=True)
class AdmissionTiming:
    recovery_start_seconds: int
    estimate_refresh_seconds: int
    slot_gap_seconds: int
    fresh_test_seconds: float


@dataclass(frozen=True)
class PressurePolicy:
    full_avg10_limit: float
    swap_used_limit: float
    some_avg10_limit: float


@dataclass(frozen=True)
class PeakHold:
    default_seconds: int
    by_kind: Mapping[str, int] = field(default_factory=dict[str, int])

    def __post_init__(self) -> None:
        object.__setattr__(self, "by_kind", MappingProxyType(dict(self.by_kind)))

    def seconds_for(self, kind: str) -> int:
        return self.by_kind.get(kind, self.default_seconds)


@dataclass(frozen=True)
class AdmissionPolicy:
    timing: AdmissionTiming
    pressure: PressurePolicy
    peak_hold: PeakHold
    safety_bytes: int = 3 * GIB
    progress_lookahead_percent: float = field(kw_only=True)


@dataclass(frozen=True)
class RepositoryIdentity:
    key: str
    name: str
    legacy_name: str = ""


@dataclass(frozen=True)
class UnknownRepository:
    pass

Repository = RepositoryIdentity | UnknownRepository

LegacyNeedSource = Literal["measured", "buildlog", "pooled", "fallback"]

NeedSource = str


@dataclass(frozen=True)
class NoFloor:
    pass


@dataclass(frozen=True)
class FailedPeakFloor:
    peak: int
    ended_at: str


@dataclass(frozen=True)
class NoHeadroom:
    pass


@dataclass(frozen=True)
class ReleaseHeadroom:
    factor: float


@dataclass(frozen=True)
class LegacyDevMultiplierHeadroom:
    """The multiplier recorded before the dev size rule."""

    factor: float = DEV_HEADROOM


@dataclass(frozen=True)
class DevSizeHeadroom:
    factor: float
    margin_bytes: int

HeadroomRule = ReleaseHeadroom | LegacyDevMultiplierHeadroom | DevSizeHeadroom | NoHeadroom


@dataclass(frozen=True)
class CapNotRecorded:
    pass


@dataclass(frozen=True)
class UnderBudget:
    pass


@dataclass(frozen=True)
class CappedAtBudget:
    uncapped: int
    cap: int | CapNotRecorded


@dataclass(frozen=True)
class ShapeRankedPeak:
    runs: int
    share_percent: int
    floor: FailedPeakFloor | NoFloor = NoFloor()
    factor: HeadroomRule = NoHeadroom()
    cap: CappedAtBudget | UnderBudget = UnderBudget()


@dataclass(frozen=True)
class ShapeLargest:
    runs: int
    floor: FailedPeakFloor | NoFloor = NoFloor()
    factor: HeadroomRule = NoHeadroom()
    cap: CappedAtBudget | UnderBudget = UnderBudget()


@dataclass(frozen=True)
class ShapeFallback:
    cap: CappedAtBudget | UnderBudget = UnderBudget()

ShapeNeedBasis = ShapeRankedPeak | ShapeLargest | ShapeFallback

NeedBasis = LegacyNeedSource | ShapeNeedBasis


@dataclass(frozen=True)
class BasisNotRecorded:
    pass

TestPhaseNeedForm = Literal["same_width", "wider_unscaled", "scaled_up"]


@dataclass(frozen=True)
class TestPhaseNeed:
    bytes: int
    runs: int
    width: int
    recorded_width: int
    form: TestPhaseNeedForm
    margin: float

FinalOutcomeName = Literal["completed", "failed", "killed", "owner_gone"]


class QueueLifeIdentity(TypedDict):
    pid: int
    start_time: str
    since: float
    session_id: str
    session_name: str
    step: str
    folder: str
    unit: NotRequired[str]


class FinalizationIntent(TypedDict):
    reservation_id: str
    peak: int
    scoped: bool
    outcome: FinalOutcomeName
    ended_at: str
    end_kind: NotRequired[Literal["step_end", "token_return"]]
    history_start: NotRequired[int]
    queue_identity: NotRequired[QueueLifeIdentity]
    status: NotRequired[int]
    test_phase_peak_bytes: NotRequired[int]
    test_phase_started_at: NotRequired[float]
    test_phase_need: NotRequired[dict[str, object]]
    test_phase_charge_use: NotRequired[TestPhaseChargeUse]


@dataclass(frozen=True)
class RequestedTestWidth:
    threads: int

    def __post_init__(self) -> None:
        if self.threads <= 0:
            raise ValueError("test width must be positive")


@dataclass(frozen=True)
class UnspecifiedTestWidth:
    pass


@dataclass(frozen=True)
class NoTestWidth:
    pass

TestWidth = RequestedTestWidth | UnspecifiedTestWidth | NoTestWidth

Call = CallRecord | NoCallRecord


@dataclass(frozen=True)
class CallerRun:
    """The caller names this run; the pass captures whether it still lives."""

    run: LiveBuildRun | NoLiveBuildRun


@dataclass(frozen=True)
class FoundRun:
    """The process ancestry supplies this run without a caller claim."""

    run: LiveBuildRun | NoLiveBuildRun


class Reservation(TypedDict):
    pid: int
    start_time: str
    need: int
    repo: str
    step: str
    worktree: str
    admitted_at: str
    sidecar: str
    test_threads: TestWidth
    call_id: Call
    repo_identity: NotRequired[str]
    test_scope: NotRequired[TestScope]
    package: NotRequired[str]
    filters: NotRequired[list[str]]
    shape: NotRequired[str]
    reservation_id: NotRequired[str]
    scoped: NotRequired[bool]
    finalization: NotRequired[FinalizationIntent]
    queue_waiter: NotRequired[Waiter]
    session_id: NotRequired[str]
    session_name: NotRequired[str]
    folder: NotRequired[str]
    unit: NotRequired[str]
    test_phase_need: NotRequired[dict[str, object]]
    test_phase_charge_use: NotRequired[TestPhaseChargeUse]
    build_run: NotRequired[LiveBuildRun | NoLiveBuildRun]
    run_origin: NotRequired[CallerRun | FoundRun]


@dataclass(frozen=True)
class ExpectedPeak:
    bytes: int
    source: NeedSource
    count: int
    basis: NeedBasis | BasisNotRecorded = BasisNotRecorded()

    def __post_init__(self) -> None:
        if self.source == "shape" and not isinstance(self.basis, (ShapeRankedPeak, ShapeLargest, ShapeFallback)):
            raise ValueError("a shape estimate needs its shape basis")
        if isinstance(self.basis, BasisNotRecorded):
            if self.source in ("measured", "buildlog", "pooled", "fallback"):
                object.__setattr__(self, "basis", self.source)


@dataclass(frozen=True)
class PromisedMemory:
    bytes: int


@dataclass(frozen=True)
class PeakTimeUnknown:
    pass


@dataclass(frozen=True)
class NoPeakRecorded:
    pass


@dataclass(frozen=True)
class RampPromise:
    bytes: int


@dataclass(frozen=True)
class LifetimePromise:
    bytes: int


@dataclass(frozen=True)
class NoPromise:
    pass

MemoryPromise = RampPromise | LifetimePromise | NoPromise


@dataclass(frozen=True)
class PeakSoFar:
    bytes: int
    peak_at: float | PeakTimeUnknown = PeakTimeUnknown()
    last_near_peak_at: float | PeakTimeUnknown = PeakTimeUnknown()
    old_sampler: bool = False


@dataclass(frozen=True)
class PeakDecayed:
    bytes: int
    peak_bytes: int
    last_near_peak_at: float
    decayed_at: float
    old_sampler: bool


@dataclass(frozen=True)
class TestPhaseStarted:
    started_at: float
    peak_so_far: int
    need: TestPhaseNeed


@dataclass(frozen=True)
class PeakFileUnreadable:
    bytes: int

PeakHistory = NoPeakRecorded | PeakSoFar | PeakDecayed | PeakFileUnreadable | TestPhaseStarted


@dataclass(frozen=True)
class PidNotRecorded:
    pass


@dataclass(frozen=True)
class ProcessAnonymousUse:
    """Retain one process's resident swap-backed use or its unavailable reading."""
    pid: int
    use: AnonymousRunUse | RunUseUnavailable


@dataclass(frozen=True)
class ScopeSource:
    directory: Path
    members: frozenset[int]


@dataclass(frozen=True)
class ProcessSource:
    processes: tuple[ProcessAnonymousUse, ...]


@dataclass(frozen=True)
class NoSource:
    pass


@dataclass(frozen=True)
class MeasuredMemory:
    bytes: int
    anonymous: AnonymousRunUse | RunUseUnavailable = field(default_factory=lambda: RunUseUnavailable("swap-backed use unavailable"), kw_only=True)
    anonymous_source: ScopeSource | ProcessSource | NoSource = field(default_factory=NoSource, kw_only=True)
    promise: MemoryPromise = NoPromise()
    # Swapped bytes stay separate from resident swap-backed use.
    swap_bytes: int = 0
    observed_peak: int = 0
    # An existing peak file could not be read; its promise remains a floor.
    unknown_peak_promise: int = 0
    peak_history: PeakHistory = NoPeakRecorded()


@dataclass(frozen=True)
class ProgressAdjustedMemory(MeasuredMemory):
    counter: progress.ProgressCounter | progress.NoCounter = field(kw_only=True)
    window: progress.WindowPeak | progress.NoMatchingProfile = field(kw_only=True)
    remaining_need: int = field(kw_only=True)


@dataclass(frozen=True)
class CompilerServiceMemory(MeasuredMemory):
    """Compiler charge, without a build reservation or run-alone veto."""

    job_state: IdleService = IdleService()
    pid: int | PidNotRecorded = field(kw_only=True)


@dataclass(frozen=True)
class ServiceJobStateNotRecorded:
    pass


@dataclass(frozen=True)
class UnownedSliceMemory(MeasuredMemory):
    diagnostic: str = ""
    cgroup: str = ""
    owner: str = ""
    activity: ScopeProcesses | EmptyBuildScope = EmptyBuildScope()
    pid: int | PidNotRecorded = field(kw_only=True)
    # Keep this wire union stable for pinned clients.
    job_state: RunningServiceJob | IdleService | NotServiceSlice = field(
        default=NotServiceSlice(), metadata={"acquisition_unused": True})
    # New service states belong in this optional field.
    service_job_state: ServiceJobStateNotRecorded = field(
        default_factory=ServiceJobStateNotRecorded, kw_only=True, metadata={"acquisition_unused": True})


@dataclass(frozen=True)
class GoneMemory:
    pass

StepMemory = PromisedMemory | MeasuredMemory | ProgressAdjustedMemory | CompilerServiceMemory | UnownedSliceMemory | GoneMemory


@dataclass(frozen=True)
class NoPressureHold:
    pass


@dataclass(frozen=True)
class PressureUnavailable:
    reason: str


@dataclass(frozen=True)
class PressureConditionNotRecorded:
    pass


@dataclass(frozen=True)
class MemoryPressure:
    full_avg10: float
    some_avg10: float = 0.0
    swap_used_percent: float = 0.0
    next_start_at: float = 0.0
    condition: Literal["full", "swap"] | PressureConditionNotRecorded = PressureConditionNotRecorded()


@dataclass(frozen=True)
class UnknownHostTotal:
    pass


@dataclass(frozen=True)
class HostMemory:
    available: int
    total: int | UnknownHostTotal
    swap_total: int = 0
    swap_free: int = 0

Pressure = NoPressureHold | MemoryPressure | PressureUnavailable

NO_PRESSURE_HOLD = NoPressureHold()


class Waiter(TypedDict):
    pid: int
    start_time: str
    build_run: NotRequired[LiveBuildRun | NoLiveBuildRun]
    need: int
    since: float
    order_since: NotRequired[float]
    label: NotRequired[str]
    session_id: NotRequired[str]
    session_name: NotRequired[str]
    unit: NotRequired[str]
    step: NotRequired[str]
    folder: NotRequired[str]
    token_left_at: NotRequired[float]
    token_back_at: NotRequired[float]
    priority_since: NotRequired[float]
    estimated_at: NotRequired[float]
    build_history_current: NotRequired[bool]
    fresh_test: NotRequired[bool]
    repo_identity: NotRequired[str]
    repository_name: NotRequired[str]
    legacy_name: NotRequired[str]
    test_scope: NotRequired[TestScope]
    package: NotRequired[str]
    filters: NotRequired[list[str]]
    need_source: NotRequired[NeedSource]
    need_runs: NotRequired[int]
    need_basis: NotRequired[dict[str, object]]
    test_threads: NotRequired[int | None]


@dataclass(frozen=True)
class PriorityUnit:
    unit: str
    position: int = 0


@dataclass(frozen=True)
class NormalOrder:
    pass


class RankedWaiter(Waiter):
    order: PriorityUnit | NormalOrder


def waiter_priority(row: Waiter) -> PriorityUnit | NormalOrder:
    order = cast(Mapping[str, object], row).get("order")
    return order if isinstance(order, (PriorityUnit, NormalOrder)) else NormalOrder()


@dataclass(frozen=True)
class TokenWaitingWaiter:
    left_at: float


@dataclass(frozen=True)
class SettledPriority:
    pass


@dataclass(frozen=True)
class PendingPriorityTransition:
    id: str
    action: Literal["set", "clear"]
    at: float
    month_row_end: int = 0
    unit: str = ""
    why: str = ""
    set_by: str = ""


class PriorityEntry(TypedDict):
    unit: str
    why: str
    set_at: float
    set_by: str


@dataclass(frozen=True)
class QueuePriority:
    entries: tuple[PriorityEntry, ...]
    transition: SettledPriority | PendingPriorityTransition = SettledPriority()


def upgrade_queue_priority(value: dict[str, object]) -> dict[str, object]:
    """Read an older priority without changing its stored record."""
    if "entries" in value or not all(key in value for key in ("unit", "why", "set_at", "set_by")):
        return value
    entry = {key: value[key] for key in ("unit", "why", "set_at", "set_by")}
    transition = value.get("transition", {"kind": "SettledPriority"})
    clearing = False
    if isinstance(transition, dict):
        old = cast(dict[str, object], transition)
        clearing = old.get("action") == "clear"
        if old.get("kind") == "PendingPriorityTransition":
            transition = {**old, "unit": entry["unit"], "why": entry["why"], "set_by": entry["set_by"]}
    return {"kind": "QueuePriority", "entries": [] if clearing else [entry], "transition": transition}


@dataclass(frozen=True)
class NoQueuePriority:
    pass

NO_QUEUE_PRIORITY = NoQueuePriority()


class HeadAdmissionOverride(TypedDict):
    waiter: str
    pid: int
    start_time: str
    unit: str
    step: str
    need: int
    free: int
    reserve: int
    why: str
    set_by: str


@dataclass(frozen=True)
class NoHeadAdmissionOverride:
    pass


def queue_command_actor() -> str:
    """Identify the user command without a session-role check."""
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_THREAD_ID") or "user"


@dataclass(frozen=True)
class AnonymousRunUse:
    """Count resident anon plus shmem bytes, without swap; the codec uses this name."""
    bytes: int


@dataclass(frozen=True)
class RunUseUnavailable:
    reason: str


@dataclass(frozen=True)
class BetweenStepsSlot:
    run: LiveBuildRun
    reservation: str
    bytes: int
    expires_at: float


@dataclass(frozen=True)
class ContinuationSlot:
    slot: BetweenStepsSlot
    owner: str

RunSlot = BetweenStepsSlot | ContinuationSlot


@dataclass(frozen=True)
class RunMemory:
    owner: str
    need: int
    use: AnonymousRunUse | RunUseUnavailable
    held: int = 0
    run: LiveBuildRun | NoLiveBuildRun = field(default_factory=NoLiveBuildRun, kw_only=True)


@dataclass(frozen=True)
class RunKeepAside:
    memory: RunMemory
    bytes: int


@dataclass(frozen=True)
class CapUnavailable:
    runs: tuple[RunMemory, ...]


@dataclass(frozen=True)
class MemoryRoom:
    available: int
    safety: int
    used: int
    kept: int
    signed_room: int
    cap: int | CapUnavailable
    runs: tuple[RunKeepAside, ...]
    total: int | UnknownHostTotal = field(default_factory=UnknownHostTotal, kw_only=True)
    ci_use: int = field(default=0, kw_only=True)

    @property
    def cap_use(self) -> int:
        """Return the swap-backed use and CI use shown inside the cap."""
        return self.used + self.ci_use

    @property
    def room(self) -> int:
        return max(0, self.signed_room)

    @property
    def shortfall(self) -> int:
        return max(0, -self.signed_room)


@dataclass(frozen=True)
class Fits:
    pass


@dataclass(frozen=True)
class StartsAlone:
    pass


@dataclass(frozen=True)
class WithinSlot:
    pass


@dataclass(frozen=True)
class OverrideRoom:
    pass


@dataclass(frozen=True)
class DoesNotFit:
    pass

StartBasis = Fits | StartsAlone | WithinSlot | OverrideRoom | DoesNotFit


@dataclass(frozen=True)
class RoomCheck:
    need: int
    room: int
    passes: bool
    basis: StartBasis = field(kw_only=True)


@dataclass(frozen=True)
class NoOrderHold:
    pass


@dataclass(frozen=True)
class HeldBehindHead:
    waiter: Waiter


@dataclass(frozen=True)
class AdmissionDecision:
    state: Literal["admit", "hold"]
    threshold: int
    promised: int
    reserve: int
    measured: int
    pressure: Pressure = NO_PRESSURE_HOLD
    charged: int = 0
    free_passes: bool = True
    order_hold: NoOrderHold | HeldBehindHead = NoOrderHold()
    starts_at: float = 0.0
    reused_slot: bool = False
    room: RoomCheck = RoomCheck(0, 0, True, basis=Fits())


@dataclass(frozen=True)
class StartSpacingWait:
    at: float


@dataclass(frozen=True)
class NeedsMemory:
    need: int = 0
    free: int = 0
    threshold: int = 0
    growth: int = 0
    reserve: int = 0


@dataclass(frozen=True)
class BehindHead:
    head_name: str
    joined_at: float
    head_session_id: str


@dataclass(frozen=True)
class ReadyForAdmission:
    pass


@dataclass(frozen=True)
class ReadingUnavailable:
    reason: str = "memory reading unavailable"


@dataclass(frozen=True)
class EstimateExpired:
    estimated_at: float

HeadCause = MemoryPressure | PressureUnavailable | ReadingUnavailable | StartSpacingWait | NeedsMemory | EstimateExpired | BehindHead | ReadyForAdmission


@dataclass(frozen=True)
class HeadAdmissionStatus:
    decision: AdmissionDecision
    need: int
    available: int
    cause: HeadCause = field(kw_only=True)

WaitReason = StartSpacingWait | PressureUnavailable | EstimateExpired | NeedsMemory | BehindHead | HeadAdmissionStatus | ReadyForAdmission | ReadingUnavailable | MemoryPressure | TokenWaitingWaiter


def head_wait_text(reason: HeadAdmissionStatus, zone: ZoneInfo) -> str:
    cause = reason.cause
    if isinstance(cause, (PressureUnavailable, ReadingUnavailable)):
        return "head: " + cause.reason
    if isinstance(cause, MemoryPressure):
        return "head: " + pressure_text(cause)
    if isinstance(cause, StartSpacingWait):
        return "head: next start at " + datetime.fromtimestamp(cause.at, zone).strftime("%H:%M:%S")
    if isinstance(cause, EstimateExpired):
        return "head: estimate expired"
    if isinstance(cause, BehindHead):
        return "head: behind " + cause.head_name
    if isinstance(cause, ReadyForAdmission):
        return "head: ready for admission"
    return f"head: needs {reason.need / GIB:.1f} GiB; room {max(0, reason.decision.room.room) / GIB:.1f} GiB"


@dataclass(frozen=True)
class WaitingStep:
    pid: int
    session_id: str
    session_name: str
    step: str
    folder: str
    since: float
    need: int
    reason: WaitReason
    unit: str = ""
    project: str = ""
    run: LiveBuildRun | NoLiveBuildRun = field(default=NoLiveBuildRun(), kw_only=True)


@dataclass(frozen=True)
class RunningStep:
    session_id: str
    session_name: str
    step: str
    folder: str
    promise: int
    measured: StepMemory
    pid: int | PidNotRecorded = field(kw_only=True)
    run: LiveBuildRun | NoLiveBuildRun = NoLiveBuildRun()
    started_at: float = 0.0
    unit: str = ""
    project: str = ""


@dataclass(frozen=True)
class QueueView:
    waiting: tuple[WaitingStep, ...]
    running: tuple[RunningStep, ...]
    available: int
    promised: int
    reserve: int
    now: float


@dataclass(frozen=True)
class HostAvailable:
    available: int
    total: int | UnknownHostTotal
    reserve: int
    pressure: Pressure


@dataclass(frozen=True)
class AdmissionTimeNotRecorded:
    pass


@dataclass(frozen=True)
class ExplainedRunningStep:
    step: RunningStep
    admitted_at: float | AdmissionTimeNotRecorded
    promise: int
    measured: int
    charged: int
    owner: str = field(default="", kw_only=True)


@dataclass(frozen=True)
class QueueMemoryTotals:
    charged: int
    measured: int
    growth: int


@dataclass(frozen=True)
class WidthNotRecorded:
    pass


@dataclass(frozen=True)
class NeedSourceNotRecorded:
    pass


@dataclass(frozen=True)
class RecordedNeed:
    source: NeedSource
    runs: int
    history_days: int
    scope: TestScope
    package: str
    filters: tuple[str, ...]
    test_threads: int | WidthNotRecorded
    basis: NeedBasis | BasisNotRecorded = BasisNotRecorded()


@dataclass(frozen=True)
class FreeCheck:
    available: int
    need: int
    reserve: int
    growth: int
    threshold: int
    passes: bool


@dataclass(frozen=True)
class HeadExplanation:
    waiter: WaitingStep
    need_source: RecordedNeed | NeedSourceNotRecorded
    decision: AdmissionDecision
    room: RoomCheck
    free: FreeCheck
    has_priority: bool
    cause: HeadCause = field(kw_only=True)


@dataclass(frozen=True)
class NoWaiter:
    pass


@dataclass(frozen=True)
class TokenWaitersOnly:
    """Rows retain their places, but all are waiting for the cargo token."""


@dataclass(frozen=True)
class LineWaiter:
    waiter: WaitingStep
    fits_now: bool
    decision: AdmissionDecision
    room: RoomCheck
    free: FreeCheck


@dataclass(frozen=True)
class WaitingLine:
    steps: tuple[LineWaiter, ...]
    count: int
    fitting: int
    smallest_need: int
    largest_need: int
    oldest_since: float


@dataclass(frozen=True)
class ReadyToStart:
    pass


@dataclass(frozen=True)
class NextStartAt:
    at: float

StartSpacing = ReadyToStart | NextStartAt


@dataclass(frozen=True)
class QueueExplanation:
    now: float
    host: HostAvailable | ReadingUnavailable
    running: tuple[ExplainedRunningStep, ...]
    totals: QueueMemoryTotals
    head: HeadExplanation | TokenWaitersOnly | NoWaiter
    line: WaitingLine
    decided_from: QueueInputs
    policy: AdmissionPolicy = field(kw_only=True)
    ordered: tuple[RankedWaiter, ...] = ()
    reservations: tuple[Reservation, ...] = ()
    override: HeadAdmissionOverride | NoHeadAdmissionOverride = NoHeadAdmissionOverride()
    spacing: StartSpacing = ReadyToStart()
    priority: QueuePriority | NoQueuePriority = NO_QUEUE_PRIORITY
    room: MemoryRoom | ReadingUnavailable = ReadingUnavailable()


@dataclass(frozen=True)
class QueueInputs:
    """Keep queue evidence separate from the admission policy."""
    ordered: Sequence[tuple[Path, Waiter]]
    running: tuple[RunningStep, ...]
    reservations: tuple[Reservation, ...]
    host: HostMemory | ReadingUnavailable
    pressure: Pressure
    spacing: StartSpacing
    override: HeadAdmissionOverride | NoHeadAdmissionOverride
    now: float
    priority: QueuePriority | NoQueuePriority = NO_QUEUE_PRIORITY
    blockers_for: frozenset[str] = frozenset()
    waiter_orders: dict[str, PriorityUnit | NormalOrder] = field(default_factory=dict, kw_only=True)
    slots: tuple[RunSlot, ...] = field(default=(), kw_only=True)

    def __post_init__(self) -> None:
        orders = {waiter_owner(row): (waiter_priority(row) if isinstance(row.get("order"), (PriorityUnit, NormalOrder))
                                     else self.waiter_orders.get(waiter_owner(row), NormalOrder()))
                  for _, row in self.ordered}
        object.__setattr__(self, "waiter_orders", orders)
        object.__setattr__(self, "ordered", [(path, stored_waiter(row)) for path, row in self.ordered])


@dataclass(frozen=True)
class QueueRead:
    view: QueueView
    explanation: QueueExplanation
    ordered: list[tuple[Path, RankedWaiter]]
    diagnostics: tuple[ConfigurationDiagnostic, ...] = ()


@dataclass(frozen=True)
class NoBuildHistoryGeneration:
    pass


@dataclass(frozen=True)
class BuildHistoryGeneration:
    value: int


@dataclass(frozen=True)
class StepProfile:
    repo: str
    step: str
    worktree: str
    peak: ExpectedPeak
    repository: Repository = UnknownRepository()
    scope: TestScope = "full"
    estimated_at: float = 0.0
    build_history_generation: BuildHistoryGeneration | NoBuildHistoryGeneration = NoBuildHistoryGeneration()


@dataclass(frozen=True)
class InferFromArgv:
    pass

INFER_FROM_ARGV = InferFromArgv()


@dataclass(frozen=True)
class MeasuredProgramPeak:
    bytes: int
    runs: int


@dataclass(frozen=True)
class FirstProgramPeak:
    bytes: int


@dataclass(frozen=True)
class HistoryPeakSizing:
    first_peak_bytes: int
    estimate: MeasuredProgramPeak | FirstProgramPeak
    estimated_at: float


@dataclass(frozen=True)
class ProgramProfile:
    program: str
    repo: str
    worktree: str
    sizing: HistoryPeakSizing

    @property
    def step(self) -> str:
        return self.program

    @property
    def estimated_at(self) -> float:
        return self.sizing.estimated_at

    @property
    def peak(self) -> ExpectedPeak:
        estimate = self.sizing.estimate
        return (ExpectedPeak(estimate.bytes, "measured", estimate.runs)
                if isinstance(estimate, MeasuredProgramPeak) else ExpectedPeak(estimate.bytes, "fallback", 0))

    @property
    def repository(self) -> UnknownRepository:
        return UnknownRepository()

    @property
    def scope(self) -> TestScope:
        return "full"


@dataclass(frozen=True)
class ConfigurationDiagnostic:
    diagnostic: str

_warned_configuration_diagnostics: set[ConfigurationDiagnostic] = set()


def warn_configuration_diagnostics(diagnostics: tuple[ConfigurationDiagnostic, ...]) -> None:
    for diagnostic in diagnostics:
        if diagnostic not in _warned_configuration_diagnostics:
            _warned_configuration_diagnostics.add(diagnostic)
            print(diagnostic.diagnostic, file=sys.stderr)

TEST_PHASE_MARGIN_FALLBACK = 1.2


@dataclass(frozen=True)
class MemoryConfig:
    policy: AdmissionPolicy
    ci_memory_slice: str
    ci_label: str = field(kw_only=True)
    test_phase_margin: float
    dev_size_factor: float
    dev_size_margin_bytes: int
    release_headroom: float
    running_pass_seconds: float
    waiting_pass_seconds: float
    wait_reply_seconds: float
    query_timeout_seconds: float
    replay_retention_seconds: float
    reconnect_seconds: float
    silence_seconds: float
    outage_notice_seconds: float
    reading_alert_seconds: float
    unreachable_limit_seconds: float
    source_check_seconds: float
    source_settle_seconds: float
    override_reconnect_seconds: float
    request_max_bytes: int
    program_first_peaks: Mapping[str, int]
    program_sample_seconds: float
    program_stop_seconds: float
    peak_wave_seconds: float
    diagnostics: tuple[ConfigurationDiagnostic, ...] = field(default=(), compare=False)


def _gib_bytes(value: float) -> int:
    return max(1, int(value * GIB))


def _ci_slice_name(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@:-]*\.slice", value) is None:
        raise ValueError("expected a systemd slice name")
    return value


def _ci_label(value: str) -> str:
    try:
        _ = value.format(runner="runner", job="job")
    except (KeyError, IndexError, AttributeError, TypeError, ValueError) as error:
        raise ValueError("expected a CI label with only {runner} and {job} fields") from error
    return value


def configuration_diagnostics(messages: Iterable[str]) -> tuple[ConfigurationDiagnostic, ...]:
    return tuple(ConfigurationDiagnostic(message) for message in dict.fromkeys(messages))


def read_memory_config(*, report: bool = True) -> MemoryConfig:
    """Resolve the live configuration for one admission decision."""
    with conf.diagnostics(report=False) as messages:
        settings_read = conf.read("build_admission")
        keys = settings_read.settings
        values = keys if isinstance(keys, dict) else settings_read.values()

        def value[Value](key: str, convert: Callable[[str], Value]) -> Value:
            result = settings_read.origin(key, convert)
            if isinstance(result, conf.UnavailableValue):
                raise conf.Unusable(f"{result.reason}; no usable committed value")
            if isinstance(result, conf.RecoveredValue):
                messages.append(f"warning: {result.reason}")
            return result.value

        def number(key: str) -> float:
            return value(key, float)

        def whole(key: str) -> int:
            return value(key, int)

        def test_margin() -> float:
            def valid_margin(raw: str) -> float:
                margin = float(raw)
                if not math.isfinite(margin) or margin < 1:
                    raise ValueError("expected a finite margin of at least 1")
                return margin

            result = settings_read.origin("test_phase_margin", valid_margin)
            if isinstance(result, (conf.UnavailableValue, conf.RecoveredValue)):
                messages.append(f"warning: {result.reason}; using test_phase_margin={TEST_PHASE_MARGIN_FALLBACK}")
                return TEST_PHASE_MARGIN_FALLBACK
            return result.value

        def gib(key: str) -> int:
            return value(key, lambda raw: _gib_bytes(float(raw)))

        try:
            running_interval = number("running_pass_seconds")
            timing = AdmissionTiming(*(whole(key) for key in (
                "recovery_start_seconds", "estimate_refresh_seconds", "slot_gap_seconds")), fresh_test_seconds=running_interval)
            pressure = PressurePolicy(*(number(key) for key in (
                "full_pressure_limit", "swap_used_limit", "some_pressure_limit")))
            kinds = {key.removeprefix("peak_hold_seconds."): whole(key)
                     for key in values if key.startswith("peak_hold_seconds.")}
            first_peaks = {key.removeprefix("program_first_peak_gib."): gib(key)
                           for key in values if key.startswith("program_first_peak_gib.")}
            if "cargo" not in first_peaks:
                first_peaks["cargo"] = gib("program_first_peak_gib.cargo")
            config = MemoryConfig(
                policy=AdmissionPolicy(timing, pressure, PeakHold(whole("peak_hold_seconds"), kinds), gib("safety_gib"),
                                       progress_lookahead_percent=number("progress_lookahead_percent")),
                ci_memory_slice=value("ci_memory_slice", _ci_slice_name),
                ci_label=value("ci_label", _ci_label),
                test_phase_margin=test_margin(),
                dev_size_factor=number("dev_size_factor"),
                dev_size_margin_bytes=value("dev_size_margin_gib", lambda raw: math.ceil(float(raw) * GIB)),
                release_headroom=number("release_headroom"),
                running_pass_seconds=running_interval, waiting_pass_seconds=number("waiting_pass_seconds"),
                wait_reply_seconds=number("wait_reply_seconds"), query_timeout_seconds=number("query_timeout_seconds"),
                replay_retention_seconds=number("replay_retention_seconds"),
                reconnect_seconds=number("reconnect_seconds"), silence_seconds=number("silence_seconds"),
                outage_notice_seconds=number("outage_notice_seconds"),
                reading_alert_seconds=number("reading_alert_seconds"),
                unreachable_limit_seconds=number("unreachable_limit_seconds"),
                source_check_seconds=number("source_check_seconds"),
                source_settle_seconds=number("source_settle_seconds"), override_reconnect_seconds=number("override_reconnect_seconds"),
                request_max_bytes=whole("request_max_bytes"), program_first_peaks=first_peaks,
                program_sample_seconds=number("program_sample_seconds"), program_stop_seconds=number("program_stop_seconds"),
                peak_wave_seconds=number("peak_wave_seconds"))
        finally:
            diagnostics = configuration_diagnostics(messages)
    config = replace(config, diagnostics=diagnostics)
    if report:
        warn_configuration_diagnostics(diagnostics)
    return config

test_threads = parse.test_threads


def process_start_time(pid: int) -> str | None:
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text()
        fields = stat.rsplit(") ", 1)[1].split()
        return fields[19] if fields[0] not in ("Z", "X") else None
    except (OSError, IndexError):
        return None


def format_percent(value: float, *, complete: bool = True) -> str:
    """Round down to one decimal place without showing an early completion."""
    tenths = math.floor(value * 10)
    return f"{(tenths if complete else min(tenths, 999)) / 10:.1f}%"


def pressure_text(pressure: MemoryPressure) -> str:
    if isinstance(pressure.condition, PressureConditionNotRecorded):
        return (f"memory pressure: condition not recorded by service; full avg10 {pressure.full_avg10:.1f}; "
                f"some avg10 {pressure.some_avg10:.1f}; swap {format_percent(pressure.swap_used_percent)} used")
    if pressure.condition == "swap":
        return f"memory pressure: swap {format_percent(pressure.swap_used_percent)} used; some avg10 {pressure.some_avg10:.1f}"
    return f"memory pressure: full avg10 {pressure.full_avg10:.1f}"


def stored_waiter(row: Waiter) -> Waiter:
    """Keep only waiter fields in durable rows and published rows."""
    return cast(Waiter, cast(object, {name: value for name, value in row.items() if name in Waiter.__annotations__}))


@dataclass(frozen=True)
class FreshTest:
    pass

BUILDLOG_UNIT = "BUILDLOG_UNIT"


def waiter_identity(step: str, worktree: str) -> WaiterIdentity:
    from scripts.production import staff

    session = staff.caller_identity()
    identity: WaiterIdentity = {"session_id": session, "session_name": os.environ.get("CLAUDE_SESSION_NAME", ""),
                                "step": step, "folder": os.getcwd() if worktree else ""}
    if unit := os.environ.get(BUILDLOG_UNIT):
        identity["unit"] = unit
    return identity


class WaiterIdentity(TypedDict):
    unit: NotRequired[str]
    session_id: str
    session_name: str
    step: str
    folder: str


def waiter_owner(row: Waiter) -> str:
    return run_owner(row["pid"], row["start_time"])


def run_owner(pid: int, start_time: str) -> str:
    """Use one key for a captured process identity."""
    return f"{pid}-{start_time}"


@dataclass(frozen=True)
class SessionHolder:
    session_id: str
    session_name: str


@dataclass(frozen=True)
class UnitHolder:
    unit: str


@dataclass(frozen=True)
class NoHolder:
    pass


WaitHolder = SessionHolder | UnitHolder | NoHolder


@dataclass(frozen=True)
class AdmissionWaitSnapshot:
    decision: AdmissionDecision
    peak: ExpectedPeak
    running: tuple[RunningStep, ...]
    available: int
    repo: str
    step: str
    policy: AdmissionPolicy
    diagnostics: tuple[ConfigurationDiagnostic, ...]
    head_need: int = 0
    head_label: str = ""
    head_reason: WaitReason = ReadyForAdmission()
    holder: WaitHolder = field(default_factory=NoHolder)


def eligible_head_position(waiting: Sequence[WaitingStep]) -> int:
    """Position of the first row eligible for the head rule; requires an eligible row."""
    return next(place for place, step in enumerate(waiting, 1)
                if not isinstance(step.reason, TokenWaitingWaiter))


def behind_place(reason: BehindHead, waiting: Sequence[WaitingStep]) -> int:
    """Find the captured position of the step that holds this waiter."""
    return next((place for place, step in enumerate(waiting, 1)
                 if step.session_id == reason.head_session_id and step.since == reason.joined_at), 0)


def reason_phrase(reason: WaitReason, waiting: Sequence[WaitingStep]) -> str:
    match reason:
        case TokenWaitingWaiter():
            return "waiting for the cargo token"
        case EstimateExpired():
            return "estimate expired"
        case ReadingUnavailable():
            return reason.reason
        case MemoryPressure():
            return "memory pressure"
        case PressureUnavailable():
            return reason.reason
        case StartSpacingWait():
            return "start spacing"
        case NeedsMemory():
            return "free memory"
        case BehindHead():
            place = behind_place(reason, waiting)
            return f"behind #{place}" if place else "behind the first step"
        case ReadyForAdmission():
            return "starts at the next check"
        case HeadAdmissionStatus():
            return (f"#{eligible_head_position(waiting)}'s turn" if isinstance(reason.cause, ReadyForAdmission)
                    else reason_phrase(reason.cause, waiting))
        case _:
            assert_never(reason)
