#!/usr/bin/env python3
"""Expected process-memory peaks and atomic build admission."""

from __future__ import annotations

import fcntl
from contextlib import AbstractContextManager, closing
import importlib
import json
import math
import mmap
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from collections.abc import Generator, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from typing import BinaryIO, Callable, Literal, NotRequired, Protocol, TypedDict, cast, get_args, get_origin, get_type_hints, is_typeddict
from functools import lru_cache
from itertools import chain
from types import UnionType, MappingProxyType
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "buildlog"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from build_shape import BuildShape, NoBuildShape, build_shape, index_shapes, recorded_build_shape
from atomic_file import write_text_atomic
from scripts.lib.cargo_output import CargoOutputRoots, CargoOutputUnavailable
import conf


from test_phase import (
    NoTestPhaseRecord,
    ObservedTestPhase,
    TestPhaseRecord,
    UnobservedTestPhase,
    UnreadableTestPhaseRecord,
)


class GitFacts(TypedDict):
    repo_path: str | None
    worktree: str | None
    branch: str | None
    sha: str | None


class StepParser(Protocol):
    def step_name(self, argv: list[str]) -> str: ...


class GitIdentityReader(Protocol):
    def read_test_phase(self, path: Path) -> TestPhaseRecord | NoTestPhaseRecord | UnreadableTestPhaseRecord: ...

    def git_directory(self, argv: list[str], cwd: str) -> str: ...
    def git_facts(self, directory: str) -> GitFacts: ...
    def queue_priority(self, action: Literal["set", "clear"], unit: str,
                       why: str, set_by: str, at: float, *, transition_id: str = "", start: int = 0) -> None: ...
    def head_admission(self, action: Literal["set", "admitted", "lapsed"], unit: str,
                       step: str, need: int, free: int, reserve: int, why: str,
                       set_by: str, at: float) -> None: ...


class BuildLogStore(Protocol):
    INDEX_NAME: str
    UNKNOWN_IDENTITY: str

    def root(self) -> Path: ...
    def host_name(self) -> str: ...
    def utc_iso(self, epoch: float) -> str: ...
    def append_line(self, path: Path, record: object) -> None: ...
    def locked_append(self, path: Path) -> AbstractContextManager[BinaryIO]: ...
    def append_locked(self, handle: BinaryIO, record: object) -> None: ...
    def whole_rows(self, handle: BinaryIO, start: int) -> Iterator[dict[str, object]]: ...
    def read_last_whole_row_end(self, path: Path) -> int: ...
    def host_file(self, host: str, epoch: float) -> Path: ...


class BuildLogFileMarks(Protocol):
    def marks(self, handle: BinaryIO, offset: int) -> str: ...


parse: StepParser = cast(StepParser, cast(object, importlib.import_module("parse")))
record: GitIdentityReader = cast(GitIdentityReader, cast(object, importlib.import_module("record")))
store: BuildLogStore = cast(BuildLogStore, cast(object, importlib.import_module("store")))
file_marks: BuildLogFileMarks = cast(BuildLogFileMarks, cast(object, importlib.import_module("index")))

GIB = 1 << 30
FALLBACK = 12 * GIB
# 111 natedev filtered single-test runs with no recompilation in the 14 days
# to 2026-10-29 had anon p90 0.671 GiB and p95 0.784 GiB.
FILTERED_FLOOR = GIB
# On natedev, isolated hana nextest scopes had p90 anon / memory.peak 61%;
# clippy had 65%, so 0.65 covers the measured p90 process-memory share.
ANON_SHARE = 0.65
HISTORY_DAYS = 14
# Option C, 126 builds over 14 days to 2026-10-30: 1.314 covered every
# replayed release step (including the 15.76 GiB incident).
RELEASE_HEADROOM = 1.314
# Repair replay, same 126-build cohort to 2026-10-30: this smallest factor
# covers all 81 dev steps with prior shape history. The limiting cargo-berth
# build used 1.412826538 GiB after a 0.336112976 GiB same-shape peak.
DEV_HEADROOM = 4.2034275337646125


@dataclass(frozen=True)
class AdmissionTiming:
    ramp_seconds: int = 120
    head_seconds: int = 600
    strict_seconds: int = 600
    backfill_seconds: int = 120
    recovery_start_seconds: int = 30
    recovery_window_seconds: int = 120
    fresh_test_seconds: float = 5.0


@dataclass(frozen=True)
class PressurePolicy:
    full_avg10_limit: float = 5.0
    swap_used_limit: float = 85.0
    some_avg10_limit: float = 25.0


@dataclass(frozen=True)
class PeakHold:
    default_seconds: int = 1500
    by_kind: Mapping[str, int] = field(default_factory=dict[str, int])

    def __post_init__(self) -> None:
        object.__setattr__(self, "by_kind", MappingProxyType(dict(self.by_kind)))

    def seconds_for(self, kind: str) -> int:
        return self.by_kind.get(kind, self.default_seconds)


@dataclass(frozen=True)
class AdmissionPolicy:
    timing: AdmissionTiming = AdmissionTiming()
    pressure: PressurePolicy = PressurePolicy()
    peak_hold: PeakHold = field(default_factory=PeakHold)


DEFAULT_ADMISSION_POLICY = AdmissionPolicy()
PROC = Path("/proc")
# These cargo subcommands compile; arbitrary programs and merge gates run.
CARGO_COMPILE_KINDS = frozenset({"build", "test", "nextest", "clippy", "check", "lint", "doc", "mend", "final"})


def peak_activity(kind: str) -> str:
    return "compiling" if kind in CARGO_COMPILE_KINDS else "running"


@dataclass(frozen=True)
class RepositoryIdentity:
    key: str
    name: str
    legacy_name: str = ""


@dataclass(frozen=True)
class UnknownRepository:
    pass


Repository = RepositoryIdentity | UnknownRepository
TestScope = Literal["full", "filtered"]
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
    factor: float = RELEASE_HEADROOM


@dataclass(frozen=True)
class DevHeadroom:
    factor: float = DEV_HEADROOM


@dataclass(frozen=True)
class UnderBudget:
    pass


@dataclass(frozen=True)
class CappedAtBudget:
    uncapped: int


@dataclass(frozen=True)
class ShapeP90:
    runs: int
    floor: FailedPeakFloor | NoFloor = NoFloor()
    factor: ReleaseHeadroom | DevHeadroom | NoHeadroom = NoHeadroom()
    cap: CappedAtBudget | UnderBudget = UnderBudget()


@dataclass(frozen=True)
class ShapeLargest:
    runs: int
    floor: FailedPeakFloor | NoFloor = NoFloor()
    factor: ReleaseHeadroom | DevHeadroom | NoHeadroom = NoHeadroom()
    cap: CappedAtBudget | UnderBudget = UnderBudget()


@dataclass(frozen=True)
class ShapeFallback:
    cap: CappedAtBudget | UnderBudget = UnderBudget()


ShapeNeedBasis = ShapeP90 | ShapeLargest | ShapeFallback


@dataclass(frozen=True)
class ProgramMemoryCap:
    program: str


NeedBasis = LegacyNeedSource | ShapeNeedBasis | ProgramMemoryCap


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
    margin: float = field(default_factory=lambda: test_phase_margin())


@dataclass(frozen=True)
class NoTestPhaseNeed:
    reason: Literal["no_width", "no_history", "not_recorded"]


@dataclass(frozen=True)
class NoTestPhaseFile:
    pass


@dataclass(frozen=True)
class Completed:
    pass


@dataclass(frozen=True)
class Failed:
    pass


@dataclass(frozen=True)
class Killed:
    pass


@dataclass(frozen=True)
class OwnerGone:
    pass


@dataclass(frozen=True)
class OutcomeNotRecorded:
    pass


FinalOutcome = Completed | Failed | Killed | OwnerGone
HistoryOutcome = FinalOutcome | OutcomeNotRecorded
NO_BUILD_SHAPE = NoBuildShape()
OUTCOME_NOT_RECORDED = OutcomeNotRecorded()


@dataclass(frozen=True)
class PackageTestSelection:
    package: str
    filters: tuple[str, ...]


@dataclass(frozen=True)
class BroadTestSelection:
    pass


class TestPhaseChargeUse(TypedDict):
    bytes: int
    selected_at: float
    reservation_id: str
    need: dict[str, object]


@dataclass(frozen=True)
class NoTestPhaseChargeUse:
    pass


class FinalizationIntent(TypedDict):
    reservation_id: str
    peak: int
    scoped: bool
    outcome: Literal["completed", "failed", "killed", "owner_gone"]
    ended_at: str
    status: NotRequired[int]
    test_phase_peak_bytes: NotRequired[int]
    test_phase_started_at: NotRequired[float]
    test_phase_need: NotRequired[dict[str, object]]
    test_phase_charge_use: NotRequired[TestPhaseChargeUse]


@dataclass(frozen=True)
class HistoryRecorded:
    pass


@dataclass(frozen=True)
class NoHistoryRecorded:
    pass


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


@dataclass(frozen=True)
class CallRecord:
    call_id: str


@dataclass(frozen=True)
class NoCallRecord:
    pass


Call = CallRecord | NoCallRecord

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
    test_phase_need: NotRequired[dict[str, object]]
    test_phase_charge_use: NotRequired[TestPhaseChargeUse]


@dataclass(frozen=True)
class ExpectedPeak:
    bytes: int
    source: NeedSource
    count: int
    basis: NeedBasis | BasisNotRecorded = BasisNotRecorded()

    def __post_init__(self) -> None:
        if self.source == "shape" and not isinstance(self.basis, (ShapeP90, ShapeLargest, ShapeFallback)):
            raise ValueError("a shape estimate needs its shape basis")
        if self.source == "cap" and not isinstance(self.basis, ProgramMemoryCap):
            raise ValueError("a cap need requires its program cap basis")
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
class PromiseNoReading:
    bytes: int


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
class TestPhaseCharge:
    bytes: int
    peak_so_far: int
    need: TestPhaseNeed
    started_at: float

@dataclass(frozen=True)
class PeakFileUnreadable:
    bytes: int


@dataclass(frozen=True)
class UsingNow:
    bytes: int


@dataclass(frozen=True)
class EndedCharge:
    bytes: int = 0


ChargeBasis = PromiseNoReading | RampPromise | LifetimePromise | PeakSoFar | PeakDecayed | PeakFileUnreadable | UsingNow | EndedCharge | TestPhaseCharge
PeakHistory = NoPeakRecorded | PeakSoFar | PeakDecayed | PeakFileUnreadable | TestPhaseStarted


@dataclass(frozen=True)
class MeasuredMemory:
    bytes: int
    promise: MemoryPromise = NoPromise()
    # Swap counts toward the build charge, but its release does not free RAM.
    swap_bytes: int = 0
    observed_peak: int = 0
    # An existing peak file could not be read; its promise remains a floor.
    unknown_peak_promise: int = 0
    peak_history: PeakHistory = NoPeakRecorded()


@dataclass(frozen=True)
class CompilerServiceMemory(MeasuredMemory):
    """Compiler charge, without a build reservation or run-alone veto."""


@dataclass(frozen=True)
class UnownedSliceMemory(MeasuredMemory):
    cgroup: str = ""
    owner: str = ""


@dataclass(frozen=True)
class GoneMemory:
    pass


StepMemory = PromisedMemory | MeasuredMemory | CompilerServiceMemory | UnownedSliceMemory | GoneMemory


@dataclass(frozen=True)
class NoPressureHold:
    pass


@dataclass(frozen=True)
class MemoryPressure:
    full_avg10: float
    some_avg10: float = 0.0
    swap_used_percent: float = 0.0
    next_start_at: float = 0.0


@dataclass(frozen=True)
class UnknownHostTotal:
    pass


@dataclass(frozen=True)
class HostMemory:
    available: int
    total: int | UnknownHostTotal
    swap_total: int = 0
    swap_free: int = 0


Pressure = NoPressureHold | MemoryPressure
NO_PRESSURE_HOLD = NoPressureHold()


class Waiter(TypedDict):
    pid: int
    start_time: str
    need: int
    since: float
    order_since: NotRequired[float]
    label: NotRequired[str]
    session_id: NotRequired[str]
    session_name: NotRequired[str]
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


@dataclass(frozen=True)
class NormalOrder:
    pass


class RankedWaiter(Waiter):
    order: PriorityUnit | NormalOrder


class LegacyRankedWaiter(Waiter):
    priority_rank: NotRequired[int]


def rank_waiter(row: Waiter, order: PriorityUnit | NormalOrder) -> RankedWaiter:
    return {**row, "order": order}


def waiter_priority(row: Waiter) -> PriorityUnit | NormalOrder:
    order = cast(Mapping[str, object], row).get("order")
    return order if isinstance(order, (PriorityUnit, NormalOrder)) else NormalOrder()


@dataclass(frozen=True)
class QueuedWaiter:
    waiter: Waiter


@dataclass(frozen=True)
class TokenWaitingWaiter:
    left_at: float


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


@dataclass(frozen=True)
class SettledPriority:
    pass


@dataclass(frozen=True)
class PendingPriorityTransition:
    id: str
    action: Literal["set", "clear"]
    at: float
    month_row_end: int = 0


class QueuePriority(TypedDict):
    unit: str
    why: str
    set_at: float
    set_by: str
    transition: SettledPriority | PendingPriorityTransition


class LegacyPriorityTransition(TypedDict):
    id: str
    action: Literal["set", "clear"]
    at: float


class LegacyQueuePriority(TypedDict):
    unit: str
    why: str
    set_at: float
    set_by: str
    transition: NotRequired[LegacyPriorityTransition]


@dataclass(frozen=True)
class NoQueuePriority:
    pass


NO_QUEUE_PRIORITY = NoQueuePriority()


class PriorityRefused(ValueError):
    pass


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
        state: SettledPriority | PendingPriorityTransition = SettledPriority()
        if "transition" in row:
            transition = row["transition"]
            if not isinstance(transition, dict):
                raise ValueError("invalid priority transition")
            fields = cast(dict[str, object], transition)
            if fields.get("kind") == "SettledPriority":
                state = SettledPriority()
            else:
                at = fields.get("at")
                if (not isinstance(fields.get("id"), str) or not fields["id"]
                        or fields.get("action") not in ("set", "clear")
                        or not isinstance(at, (int, float)) or isinstance(at, bool) or not math.isfinite(at)):
                    raise ValueError("invalid priority transition")
                if "month_row_end" in fields:
                    boundary = fields["month_row_end"]
                    if not isinstance(boundary, int) or isinstance(boundary, bool) or boundary < 0:
                        raise ValueError("invalid priority transition row boundary")
                state = PendingPriorityTransition(cast(str, fields["id"]), cast(Literal["set", "clear"], fields["action"]),
                                                  at, cast(int, fields.get("month_row_end", 0)))
        return {"unit": cast(str, row["unit"]), "why": cast(str, row["why"]),
                "set_at": cast(float, row["set_at"]), "set_by": cast(str, row["set_by"]), "transition": state}
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


class PriorityRoster(Protocol):
    def UnitLabels(self, session: str) -> UnitMembership: ...
    def ThisMachine(self) -> object: ...
    def roster(self, environment: Mapping[str, str], scope: object) -> PriorityRosterRows: ...
    def unit_labels(self, entries: Iterable[object]) -> Mapping[str, UnitMembership]: ...
    def unit_labels_for(self, session_ids: list[str], environment: Mapping[str, str]) -> Mapping[str, UnitMembership]: ...


def priority_labels(session_ids: list[str] | RosterUnits) -> Mapping[str, UnitMembership]:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
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
        label = context.labels.get(row.get("session_id", ""))
        order = (PriorityUnit(context.priority["unit"]) if not isinstance(context.priority, NoQueuePriority)
                 and label is not None and label.unit == context.priority["unit"] else NormalOrder())
        ranked.append((path, rank_waiter(row, order)))
    return sorted(ranked, key=lambda item: waiter_order(item[1])), context.priority


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


NO_HEAD_ADMISSION_OVERRIDE = NoHeadAdmissionOverride()


class AdmitHeadRefused(ValueError):
    """The requested head override cannot be set."""


class ShowrunnerRegistry(Protocol):
    def production_of(self, session_id: str) -> Path | None: ...


def showrunner_is_registered(session_id: str) -> bool:
    # This path runs only for a privileged command, never on a check poll.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "production"))
    showrunners = cast(ShowrunnerRegistry, cast(object, importlib.import_module("scripts.production.showrunners")))
    return showrunners.production_of(session_id) is not None


def require_showrunner() -> str:
    """Return the registered Claude showrunner id or refuse the caller."""
    if os.environ.get("CODEX_THREAD_ID"):
        raise AdmitHeadRefused("only a registered showrunner session may run this command")
    session = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not session or not showrunner_is_registered(session):
        raise AdmitHeadRefused("only a registered showrunner session may run this command")
    return session


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


@dataclass(frozen=True)
class HeadPriority:
    waiter: Waiter
    window: StrictUntil | BackfillUntil


@dataclass(frozen=True)
class NoHeadPriority:
    pass


@dataclass(frozen=True)
class ReadHostAdmissionLimits:
    pass


READ_HOST_ADMISSION_LIMITS = ReadHostAdmissionLimits()


@dataclass(frozen=True)
class AdmissionLimits:
    budget: int
    reserve: int


@dataclass(frozen=True)
class NoOrderHold:
    pass


@dataclass(frozen=True)
class HeldBehindHead:
    waiter: Waiter


@dataclass(frozen=True)
class HeldBehindOlderFit:
    waiter: Waiter


@dataclass(frozen=True)
class HeadRoom:
    head_need: int
    used: int
    room: int
    passes: bool


@dataclass(frozen=True)
class NoHeadRoom:
    pass


@dataclass(frozen=True)
class AdmissionDecision:
    state: Literal["admit", "hold"]
    threshold: int
    promised: int
    reserve: int
    measured: int
    pressure: Pressure = NO_PRESSURE_HOLD
    charged: int = 0
    # The lambda resolves build_budget_bytes after its definition below.
    budget: int = field(default_factory=lambda: build_budget_bytes())
    budget_passes: bool = True
    free_passes: bool = True
    head_room: HeadRoom | NoHeadRoom = NoHeadRoom()
    order_hold: NoOrderHold | HeldBehindHead | HeldBehindOlderFit = NoOrderHold()


@dataclass(frozen=True)
class NeedsBuildBudget:
    need: int
    charged: int
    budget: int


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
class LegacyBehindHead:
    """The installed service names the head's join time head_since."""

    head_name: str
    head_since: float
    head_session_id: str


@dataclass(frozen=True)
class BehindOlderFit:
    older_name: str
    older_since: float
    older_session_id: str


def older_fit_text(reason: BehindOlderFit) -> str:
    return f"behind older step {reason.older_name}"


@dataclass(frozen=True)
class BackfillUntil:
    deadline: float


@dataclass(frozen=True)
class StrictUntil:
    deadline: float


@dataclass(frozen=True)
class WindowNotStarted:
    """No recorded priority window has a deadline to display."""


@dataclass(frozen=True)
class HeadAdmissionStatus:
    decision: AdmissionDecision
    need: int
    available: int
    backfill: BackfillUntil | StrictUntil


@dataclass(frozen=True)
class ReadyForAdmission:
    pass


@dataclass(frozen=True)
class ReadingUnavailable:
    reason: str = "memory reading unavailable"


@dataclass(frozen=True)
class EstimateExpired:
    estimated_at: float


def estimate_expired(waiter: Waiter, now: float, policy: AdmissionPolicy) -> bool:
    return (not waiter.get("build_history_current", True) or waiter.get("need_source") != "cap"
            and now - waiter.get("estimated_at", now) >= (policy.timing.fresh_test_seconds
                 if waiter.get("fresh_test", False) else policy.timing.head_seconds))


WaitReason = EstimateExpired | NeedsBuildBudget | NeedsMemory | BehindHead | BehindOlderFit | HeadAdmissionStatus | ReadyForAdmission | ReadingUnavailable | MemoryPressure | TokenWaitingWaiter


def head_wait_reason(decision: AdmissionDecision, need: int, available: int,
                     waiter: Waiter, now: float, policy: AdmissionPolicy) -> HeadAdmissionStatus:
    return HeadAdmissionStatus(decision, need, available, head_window(waiter, now, policy))


def head_window(waiter: Waiter, now: float, policy: AdmissionPolicy) -> BackfillUntil | StrictUntil:
    if "priority_since" not in waiter and now - waiter["since"] < policy.timing.head_seconds:
        return BackfillUntil(waiter["since"] + policy.timing.head_seconds)
    start = waiter.get("priority_since", now)
    cycle = policy.timing.strict_seconds + policy.timing.backfill_seconds
    elapsed = max(0.0, now - start)
    cycle_start = start + (elapsed // cycle) * cycle
    if elapsed % cycle < policy.timing.strict_seconds:
        return StrictUntil(cycle_start + policy.timing.strict_seconds)
    return BackfillUntil(cycle_start + cycle)


def strict_room(budget: int, head_need: int) -> int:
    """Budget room available to younger steps during the head's strict window."""
    return budget - head_need


def window_clause(window: BackfillUntil | StrictUntil, budget: int, head_need: int, zone: ZoneInfo) -> str:
    until = datetime.fromtimestamp(window.deadline, zone).strftime("%H:%M")
    if isinstance(window, BackfillUntil):
        return f"younger steps may start until {until}"
    room = strict_room(budget, head_need)
    if room <= 0:
        return f"younger steps wait until {until}"
    return f"younger steps may use {room / GIB:.1f} GiB of the budget until {until}"


def head_wait_text(reason: HeadAdmissionStatus, zone: ZoneInfo,
                   policy: AdmissionPolicy) -> str:
    decision = reason.decision
    parts: list[str] = []
    if isinstance(decision.pressure, MemoryPressure):
        parts.append(pressure_text(decision.pressure, policy))
    elif decision.state == "admit":
        parts.append("ready for the next admission check")
    else:
        if decision.charged + reason.need > decision.budget:
            parts.append(f"needs {reason.need / GIB:.1f} GiB; builds use {decision.charged / GIB:.1f} "
                         + f"of the {decision.budget / GIB:.1f} GiB budget "
                         + f"(starts when build use falls to {(decision.budget - reason.need) / GIB:.1f} GiB)")
        if reason.available < decision.threshold:
            parts.append(f"needs {decision.threshold / GIB:.1f} GiB free; {reason.available / GIB:.1f} free")
    parts.append(window_clause(reason.backfill, decision.budget, reason.need, zone))
    return "head: " + "; ".join(parts)


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


@dataclass(frozen=True)
class RunningStep:
    session_id: str
    session_name: str
    step: str
    folder: str
    promise: int
    measured: StepMemory


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
    budget: int
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
    basis: ChargeBasis


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
class BudgetCheck:
    charged: int
    need: int
    budget: int
    starts_at: int
    passes: bool


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
    budget: BudgetCheck
    free: FreeCheck
    order: StrictUntil | BackfillUntil | WindowNotStarted
    has_priority: bool


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
    budget: BudgetCheck
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
class PressureHoldRecorded:
    pass


@dataclass(frozen=True)
class RecoveryStarts:
    ends_at: float
    next_start_at: float


@dataclass(frozen=True)
class NoRecoverySpacing:
    pass


RecoverySpacing = PressureHoldRecorded | RecoveryStarts | NoRecoverySpacing


@dataclass(frozen=True)
class QueueExplanation:
    now: float
    host: HostAvailable | ReadingUnavailable
    running: tuple[ExplainedRunningStep, ...]
    totals: QueueMemoryTotals
    head: HeadExplanation | TokenWaitersOnly | NoWaiter
    line: WaitingLine
    decided_from: QueueInputs
    limits: AdmissionLimits
    policy: AdmissionPolicy = field(kw_only=True)
    ordered: tuple[RankedWaiter, ...] = ()
    reservations: tuple[Reservation, ...] = ()
    override: HeadAdmissionOverride | NoHeadAdmissionOverride = NoHeadAdmissionOverride()
    recovery: RecoverySpacing = NoRecoverySpacing()
    priority: QueuePriority | NoQueuePriority = NO_QUEUE_PRIORITY


@dataclass(frozen=True)
class QueueInputs:
    """Queue evidence without configuration; limits are resolved by each reader."""
    ordered: Sequence[tuple[Path, Waiter]]
    running: tuple[RunningStep, ...]
    reservations: tuple[Reservation, ...]
    host: HostMemory | ReadingUnavailable
    pressure: Pressure
    recovery: RecoverySpacing
    override: HeadAdmissionOverride | NoHeadAdmissionOverride
    now: float
    priority: QueuePriority | NoQueuePriority = NO_QUEUE_PRIORITY
    blockers_for: frozenset[str] = frozenset()
    waiter_orders: dict[str, PriorityUnit | NormalOrder] = field(default_factory=dict, kw_only=True)

    def __post_init__(self) -> None:
        orders = {waiter_owner(row): (waiter_priority(row) if isinstance(row.get("order"), (PriorityUnit, NormalOrder))
                                     else self.waiter_orders.get(waiter_owner(row), NormalOrder()))
                  for _, row in self.ordered}
        object.__setattr__(self, "waiter_orders", orders)
        object.__setattr__(self, "ordered", [(path, stored_waiter(row)) for path, row in self.ordered])


def ranked_queue_waiters(inputs: QueueInputs) -> list[tuple[Path, RankedWaiter]]:
    """Resolve order metadata without adding it to captured waiter rows."""
    return sorted(((path, rank_waiter(row, inputs.waiter_orders[waiter_owner(row)]))
                   for path, row in inputs.ordered), key=lambda item: waiter_order(item[1]))


@dataclass(frozen=True)
class QueueRead:
    view: QueueView
    explanation: QueueExplanation
    ordered: list[tuple[Path, RankedWaiter]]
    fallbacks: tuple[ConfigFallback, ...] = ()


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
    recovery: RecoverySpacing
    override: OverrideKept | OverrideEnded


@dataclass(frozen=True)
class NotAdmitted:
    pass


@dataclass(frozen=True)
class HeadMark:
    path: Path
    waiter: Waiter


@dataclass(frozen=True)
class PassRow:
    owner: str
    decision: AdmissionDecision
    reason: WaitReason
    blocker: BlockingIndex | NoSingleBlocker | BlockerNotRequested
    admission: ProposedAdmission | NotAdmitted


@dataclass(frozen=True)
class PassResult:
    rows: tuple[PassRow, ...]
    head: HeadPriority | NoHeadPriority
    totals: AdmissionDecision
    recovery: RecoverySpacing
    head_marks: tuple[HeadMark, ...]
    override: OverrideKept | OverrideEnded


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
    causes: tuple[Literal["build budget", "free memory", "pressure", "queue order"], ...]


HeadRelease = HeadAdmitsAfterEnd | HeadStartsAt
HeadAfterEnd = HeadRelease | HeadStillHeld | EstimateExpired | TokenWaitersOnly | NoWaiter | ReadingUnavailable


@dataclass(frozen=True)
class ProcessIdentity:
    parent: int
    start: str


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
class FixedCapSizing:
    cap_bytes: int


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
    sizing: FixedCapSizing | HistoryPeakSizing

    @property
    def step(self) -> str:
        return self.program

    @property
    def estimated_at(self) -> float:
        return self.sizing.estimated_at if isinstance(self.sizing, HistoryPeakSizing) else 0.0

    @property
    def peak(self) -> ExpectedPeak:
        if isinstance(self.sizing, FixedCapSizing):
            return ExpectedPeak(self.sizing.cap_bytes, "cap", 0, ProgramMemoryCap(self.program))
        estimate = self.sizing.estimate
        return (ExpectedPeak(estimate.bytes, "measured", estimate.runs)
                if isinstance(estimate, MeasuredProgramPeak) else ExpectedPeak(estimate.bytes, "fallback", 0))

    @property
    def repository(self) -> UnknownRepository:
        return UnknownRepository()

    @property
    def scope(self) -> TestScope:
        return "full"


def anonymous_history_rows(source: Path | BinaryIO) -> Iterator[dict[str, object]]:
    """Read whole anonymous-history rows; an open handle must already be locked."""
    if isinstance(source, Path):
        with source.open("rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_SH)
            yield from anonymous_history_rows(handle)
        return
    yield from store.whole_rows(source, 0)


def program_peak_estimate(program: str, host: str, now: datetime, root: Path,
                          first_peak_bytes: int) -> MeasuredProgramPeak | FirstProgramPeak:
    """Read one program's peak history, without using cargo estimates."""
    cutoff = now - timedelta(days=HISTORY_DAYS)
    peaks: list[int] = []
    try:
        for row in anonymous_history_rows(root / "admission/anon_peaks.jsonl"):
            try:
                if row.get("host") != host or row.get("step") != program:
                    continue
                peak, ended = row.get("anon_peak_bytes"), row.get("ended_at")
                if not isinstance(peak, int) or peak <= 0 or not isinstance(ended, str):
                    continue
                if cutoff <= datetime.fromisoformat(ended.replace("Z", "+00:00")) <= now:
                    peaks.append(peak)
            except (ValueError, TypeError, AttributeError):
                continue
    except FileNotFoundError:
        pass
    return MeasuredProgramPeak(percentile(peaks), len(peaks)) if peaks else FirstProgramPeak(first_peak_bytes)


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


def _positive_gib_bytes(raw: str) -> int | None:
    try:
        value = float(raw) * GIB
        if math.isfinite(value) and value > 0:
            return max(1, int(value))
    except ValueError:
        pass
    return None


@dataclass(frozen=True)
class ConfigFallback:
    diagnostic: str


_warned_config_fallbacks: set[ConfigFallback] = set()


def warn_config_fallbacks(fallbacks: tuple[ConfigFallback, ...]) -> None:
    for fallback in fallbacks:
        if fallback not in _warned_config_fallbacks:
            _warned_config_fallbacks.add(fallback)
            print(fallback.diagnostic, file=sys.stderr)


@dataclass(frozen=True)
class MemoryConfig:
    policy: AdmissionPolicy
    test_phase_margin: float
    build_budget_bytes: int
    running_pass_seconds: float
    waiting_pass_seconds: float
    wait_reply_seconds: float
    replay_retention_seconds: float
    reconnect_seconds: float
    silence_seconds: float
    outage_notice_seconds: float
    source_check_seconds: float
    source_settle_seconds: float
    override_reconnect_seconds: float
    request_max_bytes: int
    program_first_peaks: Mapping[str, int]
    program_sample_seconds: float
    program_stop_seconds: float
    peak_wave_seconds: float
    fallbacks: tuple[ConfigFallback, ...] = field(default=(), compare=False)


def _test_phase_margin_value(raw: object) -> float:
    """Use the live margin when an old record has no valid margin."""
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        return test_phase_margin()
    try:
        value = float(raw)
    except (ValueError, OverflowError):
        return test_phase_margin()
    return value if math.isfinite(value) and value >= 1.0 else test_phase_margin()


def test_phase_margin() -> float:
    return conf.read("lint").number("test_phase_margin")


def build_budget_bytes() -> int:
    """Read the environment override or the live configured build budget."""
    environment_budget = _positive_gib_bytes(os.environ.get("BUILDLOG_BUILD_BUDGET_GIB", ""))
    if environment_budget is not None:
        return environment_budget
    return max(1, int(conf.read("lint").number("build_budget_gib") * GIB))


def request_max_bytes() -> int:
    return conf.read("lint").whole("request_max_bytes")

def running_pass_seconds() -> float:
    return conf.read("lint").number("running_pass_seconds")

def waiting_pass_seconds() -> float:
    return conf.read("lint").number("waiting_pass_seconds")

def wait_reply_seconds() -> float:
    return conf.read("lint").number("wait_reply_seconds")

def replay_retention_seconds() -> float:
    return conf.read("lint").number("replay_retention_seconds")

def reconnect_seconds() -> float:
    return conf.read("lint").number("reconnect_seconds")

def silence_seconds() -> float:
    return conf.read("lint").number("silence_seconds")

def outage_notice_seconds() -> float:
    return conf.read("lint").number("outage_notice_seconds")

def source_check_seconds() -> float:
    return conf.read("lint").number("source_check_seconds")

def source_settle_seconds() -> float:
    return conf.read("lint").number("source_settle_seconds")

def override_reconnect_seconds() -> float:
    return conf.read("lint").number("override_reconnect_seconds")


def read_memory_config(*, report: bool = True) -> MemoryConfig:
    """Resolve the live configuration for one admission decision."""
    with conf.diagnostics(report=report) as diagnostics:
        settings_read = conf.read("lint")
        values = settings_read.values()
        timing = AdmissionTiming(*(settings_read.whole(key) for key in (
            "ramp_seconds", "head_seconds", "strict_seconds", "backfill_seconds",
            "recovery_start_seconds", "recovery_window_seconds")))
        pressure = PressurePolicy(*(settings_read.number(key) for key in (
            "full_pressure_limit", "swap_used_limit", "some_pressure_limit")))
        kinds = {key.removeprefix("peak_hold_seconds."): settings_read.whole(key)
                 for key in values if key.startswith("peak_hold_seconds.")}
        first_peaks = {key.removeprefix("program_first_peak_gib."):
                       max(1, int(settings_read.number(key) * GIB))
                       for key in values if key.startswith("program_first_peak_gib.")}
        config = MemoryConfig(
            policy=AdmissionPolicy(timing, pressure, PeakHold(settings_read.whole("peak_hold_seconds"), kinds)),
            test_phase_margin=settings_read.number("test_phase_margin"),
            build_budget_bytes=max(1, int(settings_read.number("build_budget_gib") * GIB)),
            running_pass_seconds=settings_read.number("running_pass_seconds"), waiting_pass_seconds=settings_read.number("waiting_pass_seconds"),
            wait_reply_seconds=settings_read.number("wait_reply_seconds"), replay_retention_seconds=settings_read.number("replay_retention_seconds"),
            reconnect_seconds=settings_read.number("reconnect_seconds"), silence_seconds=settings_read.number("silence_seconds"),
            outage_notice_seconds=settings_read.number("outage_notice_seconds"), source_check_seconds=settings_read.number("source_check_seconds"),
            source_settle_seconds=settings_read.number("source_settle_seconds"), override_reconnect_seconds=settings_read.number("override_reconnect_seconds"),
            request_max_bytes=settings_read.whole("request_max_bytes"), program_first_peaks=first_peaks,
            program_sample_seconds=settings_read.number("program_sample_seconds"),
            program_stop_seconds=settings_read.number("program_stop_seconds"),
            peak_wave_seconds=settings_read.number("peak_wave_seconds"))
    return replace(config, policy=replace(config.policy, timing=replace(config.policy.timing,
                   fresh_test_seconds=config.running_pass_seconds)),
                   fallbacks=tuple(ConfigFallback(message) for message in dict.fromkeys(diagnostics)))


def memory_config() -> MemoryConfig:
    return read_memory_config()


def _configured_budget(config: MemoryConfig) -> int:
    environment_budget = _positive_gib_bytes(os.environ.get("BUILDLOG_BUILD_BUDGET_GIB", ""))
    return environment_budget if environment_budget is not None else config.build_budget_bytes


@dataclass(frozen=True)
class ReadMemoryConfiguration:
    pass


READ_MEMORY_CONFIGURATION = ReadMemoryConfiguration()


def resolve_admission_limits(total: int | UnknownHostTotal,
                             config: MemoryConfig | ReadMemoryConfiguration = READ_MEMORY_CONFIGURATION) -> AdmissionLimits:
    resolved = read_memory_config() if isinstance(config, ReadMemoryConfiguration) else config
    return AdmissionLimits(_configured_budget(resolved), total * 5 // 100 if isinstance(total, int) else 0)


def decide(available: int, total: int | UnknownHostTotal, need: int, running: list[StepMemory],
           pressure: Pressure = NO_PRESSURE_HOLD, *,
           limits: AdmissionLimits | ReadHostAdmissionLimits = READ_HOST_ADMISSION_LIMITS) -> AdmissionDecision:
    """Apply the build budget, free-memory floor and pressure limit together."""
    promised = sum(growth_headroom(row) for row in running)
    measured = sum(row.bytes for row in running if isinstance(row, MeasuredMemory))
    resolved = limits if isinstance(limits, AdmissionLimits) else resolve_admission_limits(total)
    charged = sum(build_charge(row) for row in running)
    budget, reserve = resolved.budget, resolved.reserve
    threshold = need + promised + reserve
    fits_budget = charged + need <= budget or not has_running_step(running)
    fits_free = available >= threshold
    admit = fits_budget and fits_free and isinstance(pressure, NoPressureHold)
    return AdmissionDecision("admit" if admit else "hold", threshold, promised, reserve, measured,
                             pressure, charged, budget, fits_budget, fits_free)


def has_running_step(running: list[StepMemory]) -> bool:
    return any(not isinstance(row, (CompilerServiceMemory, GoneMemory)) for row in running)


def build_charge(memory: StepMemory) -> int:
    return charge_basis(memory).bytes


def charge_basis(memory: StepMemory) -> ChargeBasis:
    """Select the charge and its explanation at the same decision point."""
    if isinstance(memory, PromisedMemory):
        return PromiseNoReading(memory.bytes)
    if isinstance(memory, MeasuredMemory):
        peak = memory.peak_history
        observed: ChargeBasis = replace(peak, bytes=memory.observed_peak) if isinstance(peak, PeakSoFar) else PeakSoFar(memory.observed_peak)
        if isinstance(peak, TestPhaseStarted):
            margin = peak.need.margin
            observed = TestPhaseCharge(math.ceil(max(peak.peak_so_far, peak.need.bytes) * margin),
                                       peak.peak_so_far, peak.need, peak.started_at)
        promise = memory.promise
        terms: tuple[ChargeBasis, ...] = (UsingNow(memory.bytes), observed,
                                         PeakFileUnreadable(memory.unknown_peak_promise))
        if not isinstance(promise, NoPromise):
            terms = (promise, *terms)
        basis = max(terms, key=lambda term: term.bytes)
        if isinstance(basis, UsingNow) and isinstance(peak, PeakDecayed):
            return replace(peak, bytes=basis.bytes)
        return basis
    return EndedCharge()


def growth_headroom(memory: StepMemory) -> int:
    if isinstance(memory, PromisedMemory):
        return memory.bytes
    if isinstance(memory, MeasuredMemory):
        return (max(0, memory.promise.bytes - memory.bytes)
                if not isinstance(memory.promise, NoPromise) else 0)
    return 0


def percentile(values: list[int]) -> int:
    values.sort()
    return values[(90 * len(values) + 99) // 100 - 1]


def test_threads(argv: list[str]) -> int | None:
    """Return nextest's explicitly requested test width, when it is numeric."""
    for index, argument in enumerate(argv):
        value: str | None = None
        if argument in ("--test-threads", "-j"):
            if index + 1 < len(argv):
                value = argv[index + 1]
        elif argument.startswith("--test-threads="):
            value = argument.removeprefix("--test-threads=")
        elif argument.startswith("-j") and len(argument) > 2:
            value = argument[2:]
        if value is not None:
            try:
                return int(value)
            except ValueError:
                return None
    return None


def expected_peak(repository: Repository, step: str, host: str, now: datetime, root: Path,
                  test_threads: int | None = None, *, scope: TestScope = "full",
                  filters: tuple[str, ...] = (), package: str = "",
                  fresh: bool = False,
                  shape: BuildShape | NoBuildShape = NO_BUILD_SHAPE) -> ExpectedPeak:
    """The process-memory promise of a step over HISTORY_DAYS.

    The gate pools every test width: one width's history is thin and rebuild-heavy, and the
    claim of a width comes only after admission. `test_threads` narrows the measured history to
    one width, for width_trial.py's report.
    """
    if step == "build":
        return build_expected_peak(repository, shape, host, now, root)
    if isinstance(repository, UnknownRepository):
        return ExpectedPeak(FALLBACK, "fallback", 0)
    if fresh and step == "nextest" and package:
        compile_peak = expected_peak(repository, step, host, now, root, test_threads,
                                     scope=scope, filters=filters, package=package)
        return fresh_test_peak(repository, step, host, now, root, test_threads,
                               scope, tuple(sorted(set(filters))), package, compile_peak)
    if scope == "filtered" and package:
        filtered = filtered_peak(repository, step, host, now, root, test_threads,
                                 tuple(sorted(set(filters))), package)
        full = expected_peak(repository, step, host, now, root, test_threads, scope="full")
        # A filtered run builds the same targets and runs fewer tests than the full suite.
        return ExpectedPeak(min(filtered.bytes, full.bytes), filtered.source, filtered.count)
    cutoff = store.utc_iso((now - timedelta(days=HISTORY_DAYS)).timestamp())
    before = store.utc_iso(now.timestamp())
    measured: list[int] = []
    # Every repo's runs of this step on this host: a checkout with fewer than 5 runs of its own (a gate
    # copy, a clone) uses their p90, not FALLBACK. Doc steps held 2157 of 4702 wait minutes in the week
    # to 2026-10-28 on that fallback; the measured doc p90 over all repos was 4.15 GiB.
    pooled: list[int] = []
    try:
        for item in anonymous_history_rows(root / "admission/anon_peaks.jsonl"):
            try:
                if ((item.get("host"), item.get("step")) == (host, step)
                        and item.get("test_scope", "full") == scope
                        and (item.get("repo_identity") or item.get("repo") not in GENERIC_REPO_NAMES)
                        and (test_threads is None or item.get("test_threads") == test_threads)
                        and cutoff <= str(item.get("ended_at", "")) < before):
                    peak = item.get("anon_peak_bytes")
                    if isinstance(peak, int) and peak > 0:
                        pooled.append(peak)
                        if (item.get("repo_identity") == repository.key
                                or (not item.get("repo_identity") and repository.legacy_name
                                    and item.get("repo") == repository.legacy_name)):
                            measured.append(peak)
            except (ValueError, TypeError, AttributeError):
                continue
    except OSError:
        pass
    if len(measured) >= 5:
        return ExpectedPeak(percentile(measured), "measured", len(measured))
    peaks: list[int] = []
    index = root / store.INDEX_NAME
    try:
        connection = sqlite3.connect(f"file:{index}?mode=ro", uri=True)
        try:
            # Old index rows have no repository key. Use them only for a named
            # checkout of that repository, and read argv to separate test scopes.
            columns = {str(row[1]) for row in cast(list[tuple[object, ...]],
                       connection.execute("PRAGMA table_info(steps)").fetchall())}
            if repository.legacy_name:
                argv_column = "argv" if "argv" in columns else "NULL"
                rows = cast(list[tuple[int, str | None]], connection.execute(
                    f"SELECT peak_mem_bytes, {argv_column} FROM steps WHERE host = ? AND repo = ? AND step = ? "
                    + "AND started_at >= ? AND started_at < ? AND peak_mem_bytes > 0",
                    (host, repository.legacy_name, step, cutoff, before),
                ).fetchall())
                for value, raw_argv in rows:
                    arguments: list[str] = []
                    if raw_argv:
                        try:
                            decoded = cast(object, json.loads(raw_argv))
                            if isinstance(decoded, list):
                                arguments = [word for word in cast(list[object], decoded) if isinstance(word, str)]
                        except (ValueError, TypeError):
                            continue
                    if test_scope(arguments) == scope:
                        peaks.append(int(value))
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        pass
    if len(peaks) >= 5:
        return ExpectedPeak(int(percentile(peaks) * ANON_SHARE), "buildlog", len(peaks))
    if len(pooled) >= 5:
        return ExpectedPeak(percentile(pooled), "pooled", len(pooled))
    return ExpectedPeak(FALLBACK, "fallback", 0)


@dataclass(frozen=True)
class BuildPeak:
    shape: BuildShape
    peak: int
    ended_at: datetime
    need: int
    outcome: HistoryOutcome


def history_outcome(raw: object) -> HistoryOutcome:
    match raw:
        case "completed":
            return Completed()
        case "failed":
            return Failed()
        case "killed":
            return Killed()
        case "owner_gone":
            return OwnerGone()
        case _:
            return OutcomeNotRecorded()


def history_matches_repository(row: Mapping[str, object], repository: Repository) -> bool:
    return isinstance(repository, RepositoryIdentity) and (
        row.get("repo_identity") == repository.key
        or (not row.get("repo_identity") and bool(repository.legacy_name)
            and row.get("repo") == repository.legacy_name))


def build_peaks(repository: RepositoryIdentity, host: str, now: datetime, root: Path) -> list[BuildPeak]:
    """Read anonymous peaks; old rows need an argv join to establish their shape."""
    cutoff = now - timedelta(days=HISTORY_DAYS)
    joins: dict[tuple[str, str], BuildShape] = {}
    joined = False
    peaks: list[BuildPeak] = []
    try:
        for item in anonymous_history_rows(root / "admission/anon_peaks.jsonl"):
            try:
                if not history_matches_repository(item, repository) or item.get("host") != host or item.get("step") != "build":
                    continue
                ended_at = datetime.fromisoformat(str(item.get("ended_at", "")))
                peak = item.get("anon_peak_bytes")
                if not cutoff <= ended_at <= now or not isinstance(peak, int) or peak <= 0:
                    continue
                shape = recorded_build_shape(item.get("shape"))
                if isinstance(shape, NoBuildShape) and "shape" not in item:
                    if not joined:
                        joins = index_shapes(root / store.INDEX_NAME, host, "build", cutoff, now)
                        joined = True
                    shape = joins.get((str(item.get("call_id", "")), "build"), NoBuildShape())
                if isinstance(shape, NoBuildShape):
                    continue
                need = item.get("need")
                peaks.append(BuildPeak(shape, peak, ended_at,
                                       need if isinstance(need, int) and need > 0 else 0,
                                       history_outcome(item.get("outcome"))))
            except (ValueError, TypeError, AttributeError):
                continue
    except OSError:
        pass
    return peaks


def build_expected_peak(repository: Repository, shape: BuildShape | NoBuildShape,
                        host: str, now: datetime, root: Path) -> ExpectedPeak:
    rows = build_peaks(repository, host, now, root) if isinstance(repository, RepositoryIdentity) else []
    same = [row for row in rows if row.shape == shape]
    floor: FailedPeakFloor | NoFloor = NoFloor()
    failures = [row for row in same if not isinstance(row.outcome, OutcomeNotRecorded)
                and (isinstance(row.outcome, (Killed, OwnerGone)) or (row.need > 0 and row.peak > row.need))]
    if failures:
        newest = max(failures, key=lambda row: row.ended_at)
        floor = FailedPeakFloor(newest.peak, store.utc_iso(newest.ended_at.timestamp()))
    count = len(same)
    if count >= 10:
        base = percentile([row.peak for row in same])
        basis: ShapeNeedBasis = ShapeP90(count)
    elif same:
        base = max(row.peak for row in same)
        basis = ShapeLargest(count)
    else:
        base = max([FALLBACK, *(row.peak for row in rows
                               if isinstance(shape, BuildShape) and row.shape.profile == shape.profile)])
        basis = ShapeFallback()
    if isinstance(basis, (ShapeP90, ShapeLargest)):
        if isinstance(floor, FailedPeakFloor):
            base = max(base, floor.peak)
        factor: ReleaseHeadroom | DevHeadroom | NoHeadroom = NoHeadroom()
        if isinstance(shape, BuildShape):
            if shape.profile == "release":
                factor = ReleaseHeadroom()
            elif shape.profile == "dev":
                factor = DevHeadroom()
        uncapped = math.ceil(base * factor.factor) if isinstance(factor, (ReleaseHeadroom, DevHeadroom)) else base
        basis = replace(basis, floor=floor, factor=factor)
    else:
        uncapped = base
    budget = build_budget_bytes()
    cap: CappedAtBudget | UnderBudget = (CappedAtBudget(uncapped)
                                       if uncapped > budget else UnderBudget())
    basis = replace(basis, cap=cap)
    return ExpectedPeak(min(uncapped, budget), "shape", count, basis)


def _need_record(basis: NeedBasis | BasisNotRecorded | TestPhaseNeed | NoTestPhaseNeed) -> dict[str, object]:
    if isinstance(basis, TestPhaseNeed):
        return {"kind": "test_phase_need", "bytes": basis.bytes, "runs": basis.runs,
                "width": basis.width, "recorded_width": basis.recorded_width, "form": basis.form,
                "margin": basis.margin}
    if isinstance(basis, NoTestPhaseNeed):
        return {"kind": "no_test_phase_need", "reason": basis.reason}
    if isinstance(basis, (BasisNotRecorded, str)):
        return {"kind": basis if isinstance(basis, str) else "not_recorded"}
    if isinstance(basis, ProgramMemoryCap):
        return {"kind": "program_memory_cap", "program": basis.program}
    cap: dict[str, object] = ({"kind": "budget", "uncapped": basis.cap.uncapped}
                              if isinstance(basis.cap, CappedAtBudget) else {"kind": "none"})
    if isinstance(basis, ShapeFallback):
        return {"kind": "shape_fallback", "cap": cap}
    floor: dict[str, object] = ({"kind": "failed_peak", "peak": basis.floor.peak, "ended_at": basis.floor.ended_at}
                                if isinstance(basis.floor, FailedPeakFloor) else {"kind": "none"})
    factor: dict[str, object] = {"kind": "none"}
    if isinstance(basis.factor, (ReleaseHeadroom, DevHeadroom)):
        factor = {"kind": "release" if isinstance(basis.factor, ReleaseHeadroom) else "dev",
                  "factor": basis.factor.factor}
    kind = "shape_p90" if isinstance(basis, ShapeP90) else "shape_largest"
    return {"kind": kind, "runs": basis.runs,
            "floor": floor, "factor": factor, "cap": cap}


def _read_need_record(raw: object) -> NeedBasis | BasisNotRecorded | TestPhaseNeed | NoTestPhaseNeed:
    """One boundary for cached estimates and queue explanations."""
    if not isinstance(raw, dict):
        return BasisNotRecorded()
    row = cast(dict[str, object], raw)
    kind = row.get("kind")
    if kind == "program_memory_cap":
        program = row.get("program")
        return ProgramMemoryCap(program) if isinstance(program, str) and program else BasisNotRecorded()
    if kind == "no_test_phase_need" and row.get("reason") in ("no_width", "no_history", "not_recorded"):
        return NoTestPhaseNeed(cast(Literal["no_width", "no_history", "not_recorded"], row["reason"]))
    if kind == "test_phase_need":
        numbers = [row.get(name) for name in ("bytes", "runs", "width", "recorded_width")]
        if (all(type(number) is int and number > 0 for number in numbers)
                and row.get("form") in ("same_width", "wider_unscaled", "scaled_up")):
            margin = _test_phase_margin_value(row.get("margin"))
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
            if not isinstance(uncapped, int) or uncapped <= build_budget_bytes():
                return BasisNotRecorded()
            cap = CappedAtBudget(uncapped)
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
        factor: ReleaseHeadroom | DevHeadroom | NoHeadroom = NoHeadroom()
        if factor_row["kind"] == "release" and factor_row.get("factor") == RELEASE_HEADROOM:
            factor = ReleaseHeadroom()
        elif factor_row["kind"] == "dev" and factor_row.get("factor") == DEV_HEADROOM:
            factor = DevHeadroom()
        elif factor_row["kind"] != "none":
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
                             row: Mapping[str, object], host: str, now: datetime) -> TestPhaseNeed | NoTestPhaseNeed:
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
    return TestPhaseNeed(max(FILTERED_FLOOR, need), len(peaks), width, recorded_width, form)


def test_phase_need(row: Mapping[str, object], host: str, now: datetime, root: Path, *,
                    history_path: Path | None = None,
                    config: MemoryConfig | ReadMemoryConfiguration = READ_MEMORY_CONFIGURATION
                    ) -> TestPhaseNeed | NoTestPhaseNeed:
    try:
        path = history_path if history_path is not None else root / "admission/anon_peaks.jsonl"
        need = test_phase_need_from_rows(anonymous_history_rows(path), row, host, now)
        if isinstance(need, TestPhaseNeed):
            resolved = read_memory_config() if isinstance(config, ReadMemoryConfiguration) else config
            return replace(need, margin=resolved.test_phase_margin)
        return need
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


def history_arguments(raw: object) -> list[str]:
    """Decode indexed or canonical argv; absent arguments prove no selection."""
    if isinstance(raw, list):
        return [word for word in cast(list[object], raw) if isinstance(word, str)]
    if not isinstance(raw, str):
        return []
    try:
        decoded = cast(object, json.loads(raw))
        if isinstance(decoded, list):
            return [word for word in cast(list[object], decoded) if isinstance(word, str)]
    except (ValueError, TypeError):
        pass
    return []


def filtered_peak(repository: RepositoryIdentity, step: str, host: str, now: datetime,
                  root: Path, width: int | None, filters: tuple[str, ...],
                  package: str) -> ExpectedPeak:
    """Prefer the exact filter set, then this package's filtered runs, at any count.

    Old anon rows lack the package and cannot identify a workspace member. The
    index's argv can, so those older measured runs remain usable there. Build
    and check peaks cover compilation when no filtered run has been measured; no other
    repository's test or compile peak participates in this estimate.
    """
    cutoff = store.utc_iso((now - timedelta(days=HISTORY_DAYS)).timestamp())
    before = store.utc_iso(now.timestamp())
    measured: list[int] = []
    exact: list[int] = []
    compile_peaks: list[int] = []
    compile_index: list[int] = []
    try:
        for item in anonymous_history_rows(root / "admission/anon_peaks.jsonl"):
            try:
                own = (item.get("repo_identity") == repository.key
                       or (not item.get("repo_identity") and repository.legacy_name
                           and item.get("repo") == repository.legacy_name))
                if (not own or item.get("host") != host
                        or not cutoff <= str(item.get("ended_at", "")) < before):
                    continue
                peak = item.get("anon_peak_bytes")
                if not isinstance(peak, int) or peak <= 0:
                    continue
                sample_package = item.get("package", "")
                if (item.get("step") in ("check", "build")
                        and (sample_package == package
                             or (not sample_package and package == repository.name))):
                    compile_peaks.append(peak)
                if (item.get("step") != step or item.get("test_scope") != "filtered"
                        or sample_package != package
                        or (width is not None and item.get("test_threads") != width)):
                    continue
                measured.append(peak)
                names = item.get("filters")
                if (filters and isinstance(names, list)
                        and all(isinstance(name, str) for name in cast(list[object], names))
                        and tuple(sorted(set(cast(list[str], names)))) == filters):
                    exact.append(peak)
            except (ValueError, TypeError, AttributeError):
                continue
    except OSError:
        pass
    if exact:
        return ExpectedPeak(percentile(exact), "measured", len(exact))
    indexed: list[int] = []
    indexed_exact: list[int] = []
    if repository.legacy_name:
        try:
            connection = sqlite3.connect(f"file:{root / store.INDEX_NAME}?mode=ro", uri=True)
            try:
                columns = {str(row[1]) for row in cast(list[tuple[object, ...]],
                           connection.execute("PRAGMA table_info(steps)").fetchall())}
                argv_column = "argv" if "argv" in columns else "NULL"
                rows = cast(list[tuple[int, str | None, str]], connection.execute(
                    f"SELECT peak_mem_bytes, {argv_column}, step FROM steps "
                    + "WHERE host = ? AND repo = ? AND step IN (?, 'check', 'build') "
                    + "AND started_at >= ? AND started_at < ? AND peak_mem_bytes > 0",
                    (host, repository.legacy_name, step, cutoff, before),
                ).fetchall())
                for value, raw_argv, sample_step in rows:
                    arguments = history_arguments(raw_argv)
                    sample_package = test_package(arguments)
                    if sample_step in ("check", "build"):
                        if (sample_package == package
                                or (not sample_package and package == repository.name)):
                            compile_index.append(int(value))
                    elif (sample_package == package and test_scope(arguments) == "filtered"
                          and (width is None or test_threads(arguments) == width)):
                        indexed.append(int(value))
                        if filters and test_filters(arguments) == filters:
                            indexed_exact.append(int(value))
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            pass
    for values, source in ((indexed_exact, "buildlog"),
                           (measured, "measured"), (indexed, "buildlog")):
        if values:
            peak = percentile(values)
            if source == "buildlog":
                peak = int(peak * ANON_SHARE)
            return ExpectedPeak(peak, cast(Literal["measured", "buildlog"], source), len(values))
    if compile_peaks:
        return ExpectedPeak(max(FILTERED_FLOOR, percentile(compile_peaks)), "measured", len(compile_peaks))
    if compile_index:
        return ExpectedPeak(max(FILTERED_FLOOR, int(percentile(compile_index) * ANON_SHARE)),
                            "buildlog", len(compile_index))
    return ExpectedPeak(FILTERED_FLOOR, "fallback", 0)


def current_worktree_steps(root: Path, host: str, worktree: str,
                          now: datetime) -> list[dict[str, object]]:
    """Use a validated indexed prefix plus every newly appended recorder line.

    The index is only an acceleration: missing, replaced or incompatible data
    makes us read the canonical file instead. Partial or corrupt tails refuse
    freshness rather than hiding a more recent build.
    """
    prefixes: dict[str, tuple[int, str]] = {}
    indexed: dict[str, list[dict[str, object]]] = {}
    try:
        with closing(sqlite3.connect(f"file:{root / store.INDEX_NAME}?mode=ro", uri=True)) as connection:
            _ = connection.execute("BEGIN")
            prefixes = {path: (offset, marks) for path, offset, marks in cast(
                list[tuple[str, int, str]], connection.execute("SELECT path, offset, marks FROM files").fetchall())}
            columns = ("src", "started_at", "ended_at", "step", "argv", "test_build_key",
                       "status", "tests_run", "sweep_freed_bytes")
            cutoff = store.utc_iso((now - timedelta(days=1)).timestamp())
            result = connection.execute(
                "SELECT " + ", ".join(columns) + " FROM steps WHERE host = ? AND worktree = ? "
                + "AND (ended_at >= ? OR started_at >= ?)", (host, worktree, cutoff, cutoff))
            for values in cast(list[tuple[object, ...]], result.fetchall()):
                row: dict[str, object] = dict(zip(columns, values))
                indexed.setdefault(str(row["src"]), []).append(row)
    except sqlite3.Error:
        prefixes = {}
    rows: list[dict[str, object]] = []
    months = {now.strftime("%Y-%m"), (now - timedelta(days=1)).strftime("%Y-%m")}
    for month in sorted(months):
        path = root / host / f"{month}.jsonl"
        try:
            handle = path.open("rb")
        except FileNotFoundError:
            continue
        with handle:
            fcntl.flock(handle, fcntl.LOCK_SH)
            source = path.relative_to(root).as_posix()
            offset, marks = prefixes.get(source, (0, ""))
            if (offset <= os.fstat(handle.fileno()).st_size
                    and marks == file_marks.marks(handle, offset)):
                rows.extend(indexed.get(source, []))
            else:
                offset = 0
            _ = handle.seek(offset)
            for line in handle:
                if not line.endswith(b"\n"):
                    raise ValueError("incomplete build-log tail")
                raw = cast(object, json.loads(line))
                if not isinstance(raw, dict):
                    raise ValueError("invalid build-log record")
                row = cast(dict[str, object], raw)
                if (row.get("kind") == "step" and row.get("host") == host
                        and row.get("worktree") == worktree):
                    rows.append(row)
    return rows


def is_fresh_test(root: Path, host: str, worktree: str, package: str,
                  scope: TestScope, tree_key: str, now: datetime,
                  output_roots: CargoOutputRoots | CargoOutputUnavailable) -> bool:
    """Only the last completed matching test build can prove the targets fresh.

    The key covers Cargo build inputs, not nextest's run-only selection. Read
    the recorder's current JSONL, since the derived index can lag a finished step.
    """
    if (not tree_key or not package or not worktree
            or isinstance(output_roots, CargoOutputUnavailable)):
        return False
    before = store.utc_iso(now.timestamp())
    try:
        rows = current_worktree_steps(root, host, worktree, now)
        rows.sort(key=lambda row: (str(row.get("ended_at") or ""),
                                   str(row.get("started_at") or "")), reverse=True)
        for row in rows:
            ended = str(row.get("ended_at") or "")
            if row.get("step") != "nextest" or not ended or ended >= before:
                continue
            arguments = history_arguments(row.get("argv"))
            if test_package(arguments) != package or test_scope(arguments) != scope:
                continue
            tests_run = row.get("tests_run")
            if row.get("test_build_key") != tree_key or not (
                    row.get("status") == 0 or isinstance(tests_run, int) and tests_run > 0):
                return False
            finished = _epoch(ended)
            # Sweep deletion records are retained for one day. Older builds
            # cannot prove that their target survived an intervening sweep.
            if now.timestamp() - finished >= 24 * 60 * 60:
                return False
            targets = {output_roots.target_directory, output_roots.build_directory}
            if any(not target.is_dir() for target in targets):
                return False
            from scripts.lint import tmp_sweep

            state = tmp_sweep.state_dir()
            try:
                deleted = cast(object, json.loads((state / tmp_sweep.TARGET_DELETES_NAME).read_text()))
            except FileNotFoundError:
                deleted = {}
            if not isinstance(deleted, dict):
                return False
            for path, timestamp in cast(dict[str, object], deleted).items():
                if Path(path).resolve() in targets:
                    if (not isinstance(timestamp, (float, int)) or not math.isfinite(timestamp)
                            or timestamp >= finished):
                        return False
            for sweep in rows:
                started = str(sweep.get("started_at") or "")
                freed = sweep.get("sweep_freed_bytes")
                if (sweep.get("step") == "sweep" and ended <= started < before
                        and (not isinstance(freed, (int, float)) or freed > 0)):
                    return False
            return True
    except (OSError, ValueError, TypeError, sqlite3.Error):
        pass
    return False


def fresh_test_peak(repository: RepositoryIdentity, step: str, host: str, now: datetime,
                    root: Path, width: int | None, scope: TestScope,
                    filters: tuple[str, ...], package: str,
                    compile_peak: ExpectedPeak) -> ExpectedPeak:
    """Use only completed zero-compile measurements of this package and scope."""
    cutoff = store.utc_iso((now - timedelta(days=HISTORY_DAYS)).timestamp())
    before = store.utc_iso(now.timestamp())
    try:
        with closing(sqlite3.connect(f"file:{root / store.INDEX_NAME}?mode=ro", uri=True)) as connection:
            rows = cast(list[tuple[str | None, str, int | None, str]], connection.execute(
                "SELECT call_id, argv, peak_mem_bytes, repo FROM steps WHERE host = ? "
                + "AND step = ? AND started_at >= ? AND started_at < ? AND ended_at < ? "
                + "AND crates_compiled = 0 AND (status = 0 OR tests_run > 0)",
                (host, step, cutoff, before, before),
            ).fetchall())
    except (OSError, sqlite3.Error):
        return compile_peak
    calls: dict[str, bool] = {}
    indexed: list[int] = []
    indexed_exact: list[int] = []
    for call_id, raw_argv, peak, sample_repo in rows:
        arguments = history_arguments(raw_argv)
        if (test_package(arguments) != package or test_scope(arguments) != scope
                or (width is not None and test_threads(arguments) != width)):
            continue
        exact = bool(filters) and test_filters(arguments) == filters
        if call_id:
            calls[call_id] = exact
        if (repository.legacy_name and sample_repo == repository.legacy_name
                and isinstance(peak, int) and peak > 0):
            indexed.append(peak)
            if exact:
                indexed_exact.append(peak)
    measured: list[int] = []
    measured_exact: list[int] = []
    try:
        for item in anonymous_history_rows(root / "admission/anon_peaks.jsonl"):
            try:
                call_id = item.get("call_id")
                own = (item.get("repo_identity") == repository.key
                       or (not item.get("repo_identity") and repository.legacy_name
                           and item.get("repo") == repository.legacy_name))
                if (not own or item.get("host") != host or item.get("step") != step
                        or not cutoff <= str(item.get("ended_at", "")) < before
                        or not isinstance(call_id, str) or call_id not in calls
                        or item.get("test_scope", scope) != scope
                        or item.get("package", package) != package
                        or (width is not None and item.get("test_threads") != width)):
                    continue
                peak = item.get("anon_peak_bytes")
                if isinstance(peak, int) and peak > 0:
                    measured.append(peak)
                    if calls[call_id]:
                        measured_exact.append(peak)
            except (ValueError, TypeError, AttributeError):
                continue
    except OSError:
        pass
    for values, source in ((measured_exact, "measured"), (indexed_exact, "buildlog"),
                           (measured, "measured"), (indexed, "buildlog")):
        if values:
            peak = percentile(values)
            if source == "buildlog":
                peak = int(peak * ANON_SHARE)
            return ExpectedPeak(max(FILTERED_FLOOR, peak),
                                cast(Literal["measured", "buildlog"], source), len(values))
    return compile_peak


def process_start_time(pid: int) -> str | None:
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text()
        fields = stat.rsplit(") ", 1)[1].split()
        return fields[19] if fields[0] not in ("Z", "X") else None
    except (OSError, IndexError):
        return None


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


def process_memory(pids: set[int], proc: Path = PROC) -> MeasuredMemory:
    """Sum resident and swapped bytes for a set of processes."""
    used, swapped = 0, 0
    for child in pids:
        try:
            for line in (proc / str(child) / "status").read_text().splitlines():
                parts = line.split()
                if parts and parts[0] in ("VmRSS:", "VmSwap:"):
                    size = int(parts[1]) * 1024
                    used += size
                    if parts[0] == "VmSwap:":
                        swapped += size
        except (OSError, ValueError, IndexError):
            continue
    return MeasuredMemory(used, swap_bytes=swapped)


def tree_memory(pid: int, start: str, processes: dict[int, ProcessIdentity],
                proc: Path = PROC) -> MeasuredMemory | GoneMemory:
    """Sum resident and swapped bytes from one process table reading."""
    members = process_tree({pid: start}, processes)
    return process_memory(members, proc) if members else GoneMemory()


@dataclass(frozen=True)
class PopulatedStepScope:
    directory: Path


@dataclass(frozen=True)
class LiveOwnerProcess:
    pass


@dataclass(frozen=True)
class EndedStep:
    pass


def reservation_lifetime(row: Reservation, owner_live: bool) -> PopulatedStepScope | LiveOwnerProcess | EndedStep:
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
    return LiveOwnerProcess() if owner_live else EndedStep()


def step_memory(row: Reservation, now: datetime, processes: dict[int, ProcessIdentity],
                proc: Path = PROC, *, policy: AdmissionPolicy) -> StepMemory:
    identity = processes.get(row["pid"])
    owner_live = identity is not None and identity.start == row["start_time"]
    lifetime = reservation_lifetime(row, owner_live)
    if isinstance(lifetime, EndedStep):
        return GoneMemory()
    if isinstance(lifetime, PopulatedStepScope):
        current = lifetime.directory / "memory.current"
        swap = lifetime.directory / "memory.swap.current"
        check_memory_reading(current)
        check_memory_reading(swap)
        try:
            used, swapped = int(current.read_text()), int(swap.read_text())
            reading = MeasuredMemory(used + swapped, swap_bytes=swapped)
        except FileNotFoundError:
            if not owner_live:
                return GoneMemory()
            reading = tree_memory(row["pid"], row["start_time"], processes, proc)
    else:
        reading = tree_memory(row["pid"], row["start_time"], processes, proc)
    return step_promise(row, now, reading, policy)


def step_promise(row: Reservation, now: datetime, reading: StepMemory, policy: AdmissionPolicy) -> StepMemory:
    """Apply the reservation lifetime promise at this instant."""
    if isinstance(reading, MeasuredMemory) and (release_build(row)
            or row.get("queue_waiter", {}).get("need_source") == "cap"):
        return replace(reading, promise=LifetimePromise(row["need"]))
    if isinstance(reading, MeasuredMemory) and now.timestamp() - _epoch(row["admitted_at"]) < policy.timing.ramp_seconds:
        if reading.bytes == 0:
            return PromisedMemory(row["need"])
        return replace(reading, promise=RampPromise(row["need"]))
    return reading


def release_build(row: Reservation) -> bool:
    shape = recorded_build_shape(row.get("shape"))
    return row.get("step") == "build" and isinstance(shape, BuildShape) and shape.profile == "release"


def test_phase_history(row: Reservation, reading: StepMemory,
                       phase: ObservedTestPhase | UnobservedTestPhase
                       ) -> TestPhaseStarted | NoPeakRecorded:
    """A measured step past its promise may use its frozen test-phase need."""
    need = read_test_phase_need(row.get("test_phase_need"))
    if (isinstance(reading, MeasuredMemory) and isinstance(reading.promise, NoPromise)
            and isinstance(need, TestPhaseNeed) and isinstance(phase, ObservedTestPhase)):
        return TestPhaseStarted(phase.started_at, phase.peak_bytes, need)
    return NoPeakRecorded()


def reservation_memory(path: Path, row: Reservation, now: datetime,
                       processes: dict[int, ProcessIdentity], proc: Path, policy: AdmissionPolicy) -> StepMemory:
    reading = step_memory(row, now, processes, proc, policy=policy)
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
    phase_history = test_phase_history(row, reading, observed_test_phase(row))
    if isinstance(phase_history, TestPhaseStarted) and isinstance(reading, MeasuredMemory):
        return replace(reading, peak_history=phase_history)
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


def sccache_memory() -> list[StepMemory]:
    """Charge the compiler service once, outside all step process trees."""
    directory = Path(os.environ.get("BUILDLOG_BUILDS_CGROUP",
        f"/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/builds.slice"))
    path = directory / "sccache.service/memory.stat"
    check_memory_reading(path)
    try:
        for line in path.read_text().splitlines():
            key, _, value = line.partition(" ")
            if key == "anon":
                used = int(value)
                return [CompilerServiceMemory(used)] if used > 0 else []
    except (OSError, ValueError):
        pass
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


def read_meminfo(path: Path) -> HostMemory:
    check_memory_reading(path)
    values: dict[str, int] = {}
    try:
        text = path.read_text()
    except (OSError, UnicodeError) as error:
        raise ValueError(f"cannot read MemAvailable from {path}") from error
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] == "kB" and parts[1].isdecimal():
            values[parts[0].removesuffix(":")] = int(parts[1]) * 1024
    if "MemAvailable" not in values:
        raise ValueError(f"cannot read MemAvailable from {path}")
    return HostMemory(values["MemAvailable"], values.get("MemTotal", UnknownHostTotal()),
                      values.get("SwapTotal", 0), values.get("SwapFree", 0))


NO_HOST_SWAP = HostMemory(0, UnknownHostTotal())


def read_pressure(host: HostMemory = NO_HOST_SWAP, *, policy: AdmissionPolicy) -> Pressure:
    """An unreadable pressure file does not block; the memory rule still applies."""
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
    except (OSError, ValueError, StopIteration):
        pass
    full, some = readings.get("full", 0.0), readings.get("some", 0.0)
    swap = 100 * max(0, host.swap_total - host.swap_free) / host.swap_total if host.swap_total else 0.0
    if swap > policy.pressure.swap_used_limit and some > policy.pressure.some_avg10_limit:
        return MemoryPressure(full, some, swap)
    return MemoryPressure(full) if full > policy.pressure.full_avg10_limit else NO_PRESSURE_HOLD


def read_recovery(directory: Path) -> RecoverySpacing:
    path = directory / "pressure-recovery"
    try:
        value = cast(object, json.loads(path.read_text()))
        if not isinstance(value, dict):
            raise ValueError("expected a recovery object")
        raw = cast(dict[str, object], value)
        if raw.get("state") == "held":
            return PressureHoldRecorded()
        ends, next_start = raw.get("ends_at"), raw.get("next_start_at")
        if (raw.get("state") != "recovering" or not isinstance(ends, (int, float))
                or not isinstance(next_start, (int, float)) or isinstance(ends, bool)
                or isinstance(next_start, bool) or not math.isfinite(ends)
                or not math.isfinite(next_start)):
            raise ValueError("invalid pressure recovery deadlines")
        return RecoveryStarts(ends, next_start)
    except FileNotFoundError:
        return NoRecoverySpacing()
    except (OSError, ValueError) as error:
        print(f"memory admission: pressure recovery record {path} ignored: {error}", file=sys.stderr)
        return NoRecoverySpacing()


def next_recovery(recovery: RecoverySpacing, pressure: Pressure, now: float, policy: AdmissionPolicy) -> RecoverySpacing:
    """Advance pressure recovery without consuming a proposed admission's start slot."""
    if isinstance(pressure, MemoryPressure):
        return PressureHoldRecorded()
    if isinstance(recovery, PressureHoldRecorded):
        return RecoveryStarts(now + policy.timing.recovery_window_seconds, now)
    if isinstance(recovery, RecoveryStarts) and now >= recovery.ends_at:
        return NoRecoverySpacing()
    return recovery


def pressure_with_recovery(recovery: RecoverySpacing, pressure: Pressure, now: float) -> Pressure:
    if isinstance(pressure, MemoryPressure):
        return pressure
    if isinstance(recovery, RecoveryStarts) and now < min(recovery.ends_at, recovery.next_start_at):
        return MemoryPressure(0, next_start_at=min(recovery.ends_at, recovery.next_start_at))
    return pressure


def pressure_text(pressure: MemoryPressure, policy: AdmissionPolicy) -> str:
    if pressure.next_start_at:
        return f"memory pressure recovery: starts at least {policy.timing.recovery_start_seconds:g} s apart"
    if pressure.swap_used_percent > policy.pressure.swap_used_limit:
        return f"memory pressure: swap {pressure.swap_used_percent:.1f}% used; some avg10 {pressure.some_avg10:.1f}"
    return f"memory pressure: full avg10 {pressure.full_avg10:.1f}"


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
    _ = row.setdefault("test_threads", None)
    _ = row.setdefault("call_id", None)
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
    return cast(TokenWaitIntent, parse_snapshot_value(value, TokenWaitIntent))


def read_waiter(path: Path) -> Waiter:
    """Validate persisted waiter fields with the same schema used by snapshots."""
    value = cast(object, json.loads(path.read_text()))
    return stored_waiter(cast(Waiter, parse_snapshot_value(value, Waiter)))


def stored_waiter(row: Waiter) -> Waiter:
    """Keep only waiter fields in durable rows and published rows."""
    return cast(Waiter, cast(object, {name: value for name, value in row.items() if name in Waiter.__annotations__}))


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


def head_priority(waiters: list[Waiter], available: int, total: int | UnknownHostTotal,
                  running: list[StepMemory], now: float, policy: AdmissionPolicy, *,
                  limits: AdmissionLimits | ReadHostAdmissionLimits = READ_HOST_ADMISSION_LIMITS) -> HeadPriority | NoHeadPriority:
    candidate = first_queued_waiter(waiters)
    if isinstance(candidate, NoWaiter):
        return NoHeadPriority()
    head = candidate.waiter
    if "priority_since" in head or now - head["since"] >= policy.timing.head_seconds:
        decision = decide(available, total, head["need"], running, limits=limits)
        if ("priority_since" in head or decision.state == "admit"
                or available + decision.measured >= head["need"] + decision.reserve):
            return HeadPriority(head, head_window(head, now, policy))
    return NoHeadPriority()


@dataclass(frozen=True)
class WaiterAdmission:
    waiter: Waiter
    decision: AdmissionDecision
    state: QueuedWaiter | TokenWaitingWaiter


def head_room_used(reservations: list[Reservation], memory: list[StepMemory],
                   head: HeadPriority | NoHeadPriority) -> int:
    """Count live starts since priority at the ledger's millisecond precision."""
    if not isinstance(head, HeadPriority) or "priority_since" not in head.waiter:
        return 0
    priority = _epoch(store.utc_iso(head.waiter["priority_since"]))
    return sum(max(row["need"], build_charge(reading))
               for row, reading in zip(reservations, memory)
               if not isinstance(reading, GoneMemory) and _epoch(row["admitted_at"]) >= priority)


def snapshot_head_override(override: HeadAdmissionOverride | NoHeadAdmissionOverride,
                           ordered: list[tuple[Path, RankedWaiter]]) -> HeadAdmissionOverride | NoHeadAdmissionOverride:
    candidate = first_queued_waiter(row for _, row in ordered)
    if isinstance(override, NoHeadAdmissionOverride) or isinstance(candidate, NoWaiter):
        return NoHeadAdmissionOverride()
    row = candidate.waiter
    path = next(path for path, other in ordered if other is row)
    return override if head_override_matches(override, path, row) else NoHeadAdmissionOverride()


def admission_for_waiter(available: int, total: int | UnknownHostTotal, row: Waiter,
                         running: list[StepMemory], head: HeadPriority | NoHeadPriority,
                         pressure: Pressure, room_used: int,
                         override: HeadAdmissionOverride | NoHeadAdmissionOverride, first: bool,
                         now: float, limits: AdmissionLimits | ReadHostAdmissionLimits, policy: AdmissionPolicy) -> AdmissionDecision:
    decision = decide(available, total, row["need"], running, pressure=pressure, limits=limits)
    if (isinstance(head, HeadPriority) and isinstance(head.window, StrictUntil)
            and waiter_order(head.waiter) < waiter_order(row)):
        room = strict_room(decision.budget, head.waiter["need"])
        fits = room_used + row["need"] <= room
        decision = replace(decision, head_room=HeadRoom(head.waiter["need"], room_used, room, fits))
        if decision.state == "admit" and not fits:
            decision = replace(decision, state="hold", order_hold=HeldBehindHead(head.waiter))
    elif (isinstance(head, HeadPriority) and waiter_order(head.waiter) == waiter_order(row)
          and not has_running_step(running)):
        decision = replace(decision, state="admit" if isinstance(pressure, NoPressureHold) else "hold")
    if (first and not isinstance(override, NoHeadAdmissionOverride)
            and row["pid"] == override["pid"] and row["start_time"] == override["start_time"]):
        threshold = row["need"] + decision.reserve
        fits_free = available >= threshold
        fresh = not estimate_expired(row, now, policy)
        decision = replace(decision, state="admit" if fits_free and fresh and isinstance(pressure, NoPressureHold) else "hold",
                           threshold=threshold, promised=0, free_passes=fits_free)
    return decision


def admission_walk(available: int, total: int | UnknownHostTotal, ordered: list[Waiter],
                   running: list[StepMemory], head: HeadPriority | NoHeadPriority,
                   pressure: Pressure, *, recovery: RecoverySpacing, now: float,
                   room_used: int = 0,
                   override: HeadAdmissionOverride | NoHeadAdmissionOverride = NO_HEAD_ADMISSION_OVERRIDE,
                   limits: AdmissionLimits | ReadHostAdmissionLimits = READ_HOST_ADMISSION_LIMITS,
                   policy: AdmissionPolicy) -> list[WaiterAdmission]:
    """Keep memory, the head's budget room and the recovery slot in queue order."""
    promised = list(running)
    marked: list[Waiter] = []
    admissions: list[WaiterAdmission] = []
    recovering = isinstance(recovery, RecoveryStarts) and now < recovery.ends_at
    marked_room = 0
    candidate = first_queued_waiter(ordered)
    for row in ordered:
        state = waiter_state(row)
        if isinstance(state, TokenWaitingWaiter):
            decision = replace(decide(available, total, row["need"], running,
                                      pressure=pressure, limits=limits), state="hold")
            admissions.append(WaiterAdmission(row, decision, state))
            continue
        first = isinstance(candidate, QueuedWaiter) and candidate.waiter is row
        decision = admission_for_waiter(available, total, row, promised, head, pressure,
                                        room_used + marked_room, override, first, now, limits, policy)
        if marked and ((decision.state == "admit" and (recovering or decide(
                           available, total, row["need"],
                           [*running, *(PromisedMemory(waiter["need"]) for waiter in marked)],
                           pressure=pressure, limits=limits).state == "hold"))
                       or (decision.state == "hold" and admission_for_waiter(
                           available, total, row, running, head, pressure, room_used,
                           override, first, now, limits, policy).state == "admit")):
            decision = replace(decision, state="hold", order_hold=HeldBehindOlderFit(marked[0]))
        expired = estimate_expired(row, now, policy)
        if expired:
            decision = replace(decision, state="hold")
        admissions.append(WaiterAdmission(row, decision, state))
        if decision.state == "admit" or expired:
            marked.append(row)
            if not (isinstance(head, HeadPriority) and isinstance(head.window, StrictUntil)
                    and waiter_order(head.waiter) == waiter_order(row)):
                promised.append(PromisedMemory(row["need"]))
            if isinstance(head, HeadPriority) and waiter_order(head.waiter) < waiter_order(row):
                marked_room += row["need"]
    return admissions


GENERIC_REPO_NAMES = frozenset({"", "repo", "c", "clone", "scratch", "tmp", "repository"})


def git_output(directory: str, *arguments: str) -> str:
    try:
        result = subprocess.run(["git", "-C", directory, *arguments], capture_output=True,
                                text=True, timeout=5, check=False)
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def remote_identity(remote: str, directory: str) -> str:
    if "://" not in remote and ":" in remote and not remote.startswith(("/", ".")):
        host, path = remote.split(":", 1)
        remote = f"ssh://{host}/{path}"
    parsed = urlsplit(remote)
    if parsed.hostname:
        path = parsed.path.rstrip("/").removesuffix(".git")
        # SSH and HTTPS name the same repository; credentials are not identity.
        port = f":{parsed.port}" if parsed.port and parsed.port not in (22, 443) else ""
        return urlunsplit(("https", parsed.hostname.lower() + port, path, "", ""))
    path = Path(parsed.path if parsed.scheme == "file" else remote).expanduser()
    if not path.is_absolute():
        path = Path(directory) / path
    return str(path.resolve()).rstrip("/").removesuffix(".git")


def git_identity(argv: list[str], cwd: str) -> tuple[Repository, str]:
    if not argv:
        return UnknownRepository(), ""
    directory = record.git_directory(argv, cwd)
    facts = record.git_facts(directory)
    worktree = Path(facts["worktree"] or cwd).name
    remote = git_output(directory, "remote", "get-url", "origin")
    if remote:
        key = remote_identity(remote, directory)
        name = key.rsplit("/", 1)[-1]
        checkout_name = Path(facts["repo_path"] or cwd).name
        legacy = name if checkout_name == name and name not in GENERIC_REPO_NAMES else ""
        return RepositoryIdentity(key, name, legacy), worktree
    roots = git_output(directory, "rev-list", "--max-parents=0", "--reverse", "HEAD").splitlines()
    if roots:
        return RepositoryIdentity("git-root:" + roots[0], Path(facts["repo_path"] or cwd).name), worktree
    return UnknownRepository(), worktree


def recorded_repository_key(argv: list[str], cwd: str) -> str:
    """Encode admission's repository identity at the build-log boundary."""
    repository, _ = git_identity(argv, cwd)
    return repository.key if isinstance(repository, RepositoryIdentity) else store.UNKNOWN_IDENTITY


def filter_expressions(argv: list[str]) -> list[str]:
    expressions: list[str] = []
    arguments = iter(argv)
    for word in arguments:
        if word in ("-E", "--filter-expr", "--filterset"):
            expressions.append(next(arguments, ""))
        elif word.startswith(("--filter-expr=", "--filterset=")):
            expressions.append(word.split("=", 1)[1])
        elif word.startswith("-E") and len(word) > 2:
            expressions.append(word[2:].removeprefix("="))
    return expressions


def verify_test_selection(argv: list[str]) -> PackageTestSelection | BroadTestSelection:
    """Recognize the whole positive expression verify emits, never its fragments."""
    expressions = filter_expressions(argv)
    if len(expressions) != 1:
        return BroadTestSelection()
    package_predicate = r"package\(([A-Za-z0-9_-]+)\)"
    name_predicate = r"test\([A-Za-z0-9_:]+\)"
    union = rf"\(\s*{name_predicate}(?:\s*\|\s*{name_predicate})+\s*\)"
    match = re.fullmatch(rf"\s*{package_predicate}\s*&\s*(?:{name_predicate}|{union})\s*",
                         expressions[0])
    if match:
        names = cast(list[str], re.findall(r"\btest\(([A-Za-z0-9_:]+)\)", expressions[0]))
        return PackageTestSelection(match.group(1), tuple(sorted(set(names))))
    # Full package runs retain their package metadata, without an exact filter set.
    match = re.fullmatch(rf"\s*{package_predicate}\s*", expressions[0])
    if match and test_scope(argv) == "full":
        return PackageTestSelection(match.group(1), ())
    return BroadTestSelection()


def test_filters(argv: list[str]) -> tuple[str, ...]:
    """Verify's positive filter names, sorted and deduplicated like its cache key."""
    selection = verify_test_selection(argv)
    return selection.filters if isinstance(selection, PackageTestSelection) else ()


def test_package(argv: list[str]) -> str:
    """A verify test selection or compile package; empty means broader history."""
    if parse.step_name(argv) not in ("check", "build"):
        selection = verify_test_selection(argv)
        return selection.package if isinstance(selection, PackageTestSelection) else ""
    packages: list[str] = []
    arguments = iter(argv)
    for word in arguments:
        if word in ("-p", "--package"):
            packages.append(next(arguments, ""))
        elif word.startswith("--package="):
            packages.append(word.split("=", 1)[1])
        elif word.startswith("-p") and len(word) > 2:
            packages.append(word[2:])
    selected = set(packages)
    return selected.pop() if len(selected) == 1 else ""


def test_scope(argv: list[str]) -> TestScope:
    """Keep test selection separate from package, target and execution options."""
    step = parse.step_name(argv)
    if step not in ("nextest", "test", "t"):
        return "full"
    words = argv[argv.index(step) + 1:]
    if step == "nextest" and words and words[0] in ("run", "list"):
        words = words[1:]
    values = {"-p", "--package", "--manifest-path", "--target", "--target-dir", "--profile",
              "--features", "--test-threads", "-j", "--jobs", "--config-file", "--cargo-profile",
              "--bin", "--example", "--test", "--bench", "--status-level", "--final-status-level",
              "--failure-output", "--success-output", "--color", "--message-format", "--retries",
              "--archive-file", "--workspace-remap", "--partition", "-F", "--exclude", "-P",
              "--run-ignored", "-Z", "--no-tests", "--max-fail", "--tool-config-file", "--test-group"}
    expressions = {"-E", "--filter-expr", "--filterset"}
    arguments = iter(words)
    for word in arguments:
        option = word.split("=", 1)[0]
        if option in ("--skip", "--exact"):
            return "filtered"
        if option in expressions or word.startswith("-E"):
            expression = (word[2:].removeprefix("=") if word.startswith("-E") and len(word) > 2 else
                          word.split("=", 1)[1] if "=" in word else next(arguments, ""))
            if re.search(r"\btest\s*\(", expression):
                return "filtered"
        elif word in values:
            _ = next(arguments, "")
        elif not word.startswith("-"):
            return "filtered"
    return "full"


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
            if event.get("kind") == "token_gap" and token_gap_recorded(chain((last,), rows), event):
                return
            last_at = last.get("at", now)
            at = max(now, float(last_at)) if isinstance(last_at, (int, float)) else now
        store.append_locked(handle, {**event, "at": at})
        if os.fstat(handle.fileno()).st_size > 8 * 1024 * 1024:
            cutoff = now - HISTORY_DAYS * 86400
            kept = [row for row in store.whole_rows(handle, 0)
                    if isinstance(stamp := row.get("at"), (float, int)) and stamp >= cutoff]
            write_text_atomic(path, "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in kept))


@dataclass(frozen=True)
class FreshTest:
    pass


@dataclass(frozen=True)
class ReusableEstimate:
    pass


@dataclass(frozen=True)
class StepEstimate:
    profile: StepProfile | ProgramProfile
    cache: FreshTest | ReusableEstimate


def no_build_history_generation() -> NoBuildHistoryGeneration:
    return NoBuildHistoryGeneration()


def step_estimate(argv: list[str], cwd: str, profile: InferFromArgv | StepProfile | ProgramProfile,
                  now: datetime | Callable[[], datetime], root: Path, policy: AdmissionPolicy, *,
                  build_history_generation: Callable[[], BuildHistoryGeneration | NoBuildHistoryGeneration] = no_build_history_generation
                  ) -> StepEstimate:
    """Resolve one estimate before admission connects."""
    estimate_time = now() if callable(now) else now
    fresh = False
    package, filters = test_package(argv), test_filters(argv)
    if isinstance(profile, ProgramProfile) and isinstance(profile.sizing, HistoryPeakSizing):
        sizing = profile.sizing
        if not sizing.estimated_at or estimate_time.timestamp() - sizing.estimated_at >= policy.timing.head_seconds:
            profile = replace(profile, sizing=replace(sizing,
                estimate=program_peak_estimate(profile.program, store.host_name(), estimate_time, root,
                                               sizing.first_peak_bytes),
                estimated_at=estimate_time.timestamp()))
    elif isinstance(profile, StepProfile) and profile.estimated_at and estimate_time.timestamp() - profile.estimated_at >= policy.timing.head_seconds:
        profile = InferFromArgv()
    if isinstance(profile, InferFromArgv):
        repository, worktree = git_identity(argv, cwd)
        repo = repository.name if isinstance(repository, RepositoryIdentity) else ""
        scope = test_scope(argv)
        step = parse.step_name(argv) if argv else "build"
        host = store.host_name()
        worktree_path = ""
        if step == "nextest" and package and os.environ.get("BUILDLOG_TEST_BUILD_KEY"):
            directory = record.git_directory(argv, cwd)
            worktree_path = record.git_facts(directory)["worktree"] or ""
        target_dir = os.environ.get("BUILDLOG_TEST_TARGET_DIR", "")
        build_dir = os.environ.get("BUILDLOG_TEST_BUILD_DIR", "")
        output_roots = (CargoOutputRoots(Path(worktree_path).resolve(), Path(target_dir).resolve(),
                                        Path(build_dir).resolve())
                        if Path(target_dir).is_absolute() and Path(build_dir).is_absolute()
                        else CargoOutputUnavailable("verify did not save both output roots"))
        fresh = (step == "nextest" and is_fresh_test(
            root, host, worktree_path, package, scope,
            os.environ.get("BUILDLOG_TEST_BUILD_KEY", ""), estimate_time, output_roots))
        generation = build_history_generation() if step == "build" else NoBuildHistoryGeneration()
        if step == "build" and callable(now):
            estimate_time = now()
        peak = expected_peak(repository, step, host, estimate_time, root, scope=scope,
                             package=package, filters=filters, fresh=fresh,
                             shape=build_shape(argv) if step == "build" else NoBuildShape())
        profile = StepProfile(repo, step, worktree, peak, repository, scope, estimate_time.timestamp(),
                              generation)
    elif isinstance(profile, StepProfile) and not profile.estimated_at:
        profile = replace(profile, estimated_at=estimate_time.timestamp())
    return StepEstimate(profile, FreshTest() if fresh else ReusableEstimate())


def reservation_stem(directory: Path, pid: int, start_time: str, admitted_ns: int) -> Path:
    return directory / f"{pid}-{start_time}-{admitted_ns}"


def waiter_identity(step: str, worktree: str) -> WaiterIdentity:
    session = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    return {"session_id": session, "session_name": os.environ.get("CLAUDE_SESSION_NAME", ""),
            "step": step, "folder": os.getcwd() if worktree else ""}


class WaiterIdentity(TypedDict):
    session_id: str
    session_name: str
    step: str
    folder: str


def waiter_unit_name(identity: WaiterIdentity, pid: int) -> str:
    return identity["session_name"] or Path(identity["folder"]).name or str(pid)


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


def queue_order_reason(decision: AdmissionDecision, identities: QueueIdentities) -> BehindHead | BehindOlderFit | ReadyForAdmission:
    hold = decision.order_hold
    if isinstance(hold, NoOrderHold):
        return ReadyForAdmission()
    waiter = hold.waiter
    identity = identities.identity(waiter["pid"], cast(dict[str, object], cast(object, waiter)))
    name = waiter_unit_name(identity, waiter["pid"])
    if isinstance(hold, HeldBehindHead):
        return BehindHead(name, waiter["since"], identity["session_id"])
    return BehindOlderFit(name, waiter["since"], identity["session_id"])


def queue_wait_reason(decision: AdmissionDecision, need: int, available: int,
                       identities: QueueIdentities) -> WaitReason:
    order = queue_order_reason(decision, identities)
    if not isinstance(order, ReadyForAdmission):
        return order
    if isinstance(decision.pressure, MemoryPressure):
        return decision.pressure
    if decision.state == "hold":
        if not decision.budget_passes:
            return NeedsBuildBudget(need, decision.charged, decision.budget)
        if not decision.free_passes:
            return NeedsMemory(need, available, decision.threshold, decision.promised, decision.reserve)
    return ReadyForAdmission()


def waiter_owner(row: Waiter) -> str:
    return f"{row['pid']}-{row['start_time']}"


def admission_pass(inputs: QueueInputs, now: float, limits: AdmissionLimits, policy: AdmissionPolicy) -> PassResult:
    """Decide one frozen scenario, proposing ledger changes without committing any."""
    ordered = ranked_queue_waiters(inputs)
    candidate = first_queued_waiter(row for _, row in ordered)
    memory = [step.measured for step in inputs.running]
    host = inputs.host if isinstance(inputs.host, HostMemory) else HostMemory(0, UnknownHostTotal())
    available, total = host.available, host.total
    recovery = next_recovery(inputs.recovery, inputs.pressure, now, policy)
    pressure = pressure_with_recovery(recovery, inputs.pressure, now)
    head = head_priority([row for _, row in ordered], available, total, memory, now, policy, limits=limits)
    marks: tuple[HeadMark, ...] = ()
    if isinstance(head, HeadPriority) and "priority_since" not in head.waiter:
        marked = head.waiter.copy()
        marked["priority_since"] = now
        path = next(path for path, row in ordered if row is head.waiter)
        marks = (HeadMark(path, stored_waiter(marked)),)
        head.waiter.update(marked)
    raw_override = inputs.override
    override = snapshot_head_override(raw_override, ordered)
    outcome: OverrideKept | OverrideEnded = OverrideKept(override)
    if not isinstance(raw_override, NoHeadAdmissionOverride) and not any(
            head_override_matches(raw_override, path, row) for path, row in ordered):
        outcome = OverrideEnded(raw_override, "lapsed")
    totals = decide(available, total, 0, memory, pressure=pressure, limits=limits)
    admissions = admission_walk(available, total, [row for _, row in ordered], memory, head, pressure,
        recovery=recovery, now=now, room_used=head_room_used(list(inputs.reservations), memory, head),
        override=override, limits=limits, policy=policy)
    rows: list[PassRow] = []
    endings: tuple[PassResult, ...] = ()
    rank = 0
    for entry in admissions:
        row, decision = entry.waiter, entry.decision
        first = isinstance(candidate, QueuedWaiter) and candidate.waiter is row
        if isinstance(entry.state, TokenWaitingWaiter):
            reason: WaitReason = entry.state
        elif isinstance(inputs.host, ReadingUnavailable):
            decision = replace(decision, state="hold")
            reason = inputs.host
        elif estimate_expired(row, now, policy):
            reason = EstimateExpired(row.get("estimated_at", now))
        elif first:
            reason = head_wait_reason(decision, row["need"], available, row, now, policy)
        else:
            reason = queue_wait_reason(decision, row["need"], available, RecordedQueueIdentities())
        proposal: ProposedAdmission | NotAdmitted = NotAdmitted()
        if decision.state == "admit":
            rank += 1
            admitted_recovery = (replace(recovery, next_start_at=now + policy.timing.recovery_start_seconds)
                                 if isinstance(recovery, RecoveryStarts) else recovery)
            admitted_override = (OverrideEnded(override, "admitted") if first
                                 and not isinstance(override, NoHeadAdmissionOverride) else outcome)
            proposal = ProposedAdmission(rank, admitted_recovery, admitted_override)
        owner = waiter_owner(row)
        blocker: BlockingIndex | NoSingleBlocker | BlockerNotRequested = BlockerNotRequested()
        if owner in inputs.blockers_for:
            if (decision.state == "hold" and not isinstance(inputs.host, ReadingUnavailable)
                    and isinstance(entry.state, QueuedWaiter)):
                if not endings:
                    endings = tuple(admission_pass(inputs_after_steps_end(inputs, frozenset((ended,))), now, limits, policy)
                                    for ended in range(len(inputs.running)))
                blocker = blocking_index(owner, endings)
            else:
                blocker = NoSingleBlocker()
        rows.append(PassRow(owner, decision, reason, blocker, proposal))
    return PassResult(tuple(rows), head, totals, recovery, marks, outcome)


def released_resident_bytes(running: tuple[RunningStep, ...], ended: frozenset[int]) -> int:
    """Only measured resident memory of ended rows becomes available RAM."""
    return sum(max(0, step.measured.bytes - step.measured.swap_bytes)
               for index, step in enumerate(running) if index in ended
               and isinstance(step.measured, MeasuredMemory))


def inputs_after_steps_end(inputs: QueueInputs, ended: frozenset[int]) -> QueueInputs:
    """Keep the same evidence, removing ended rows and returning their resident bytes."""
    freed = released_resident_bytes(inputs.running, ended)
    host = replace(inputs.host, available=inputs.host.available + freed) if isinstance(inputs.host, HostMemory) else inputs.host
    reservations: list[Reservation] = []
    reservation_index = 0
    for index, step in enumerate(inputs.running):
        if not isinstance(step.measured, (CompilerServiceMemory, UnownedSliceMemory)):
            if index not in ended:
                reservations.append(inputs.reservations[reservation_index])
            reservation_index += 1
    return replace(inputs, host=host,
        running=tuple(step for index, step in enumerate(inputs.running) if index not in ended),
        reservations=tuple(reservations), blockers_for=frozenset())


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
        return {"session_id": str(row.get("session_id", "")), "session_name": str(row.get("session_name", "")),
                "step": str(row.get("step", "")), "folder": str(row.get("folder", ""))}


class QueueIdentities(Protocol):
    def identity(self, pid: int, row: dict[str, object], /) -> WaiterIdentity: ...


def decision_checks(decision: AdmissionDecision, need: int, available: int) -> tuple[BudgetCheck, FreeCheck]:
    return (BudgetCheck(decision.charged, need, decision.budget, decision.budget - need, decision.budget_passes),
            FreeCheck(available, need, decision.reserve, decision.promised, decision.threshold, decision.free_passes))


def queue_read_from(inputs: QueueInputs, limits: AdmissionLimits, policy: AdmissionPolicy) -> QueueRead:
    return queue_read_from_pass(inputs, limits, policy, admission_pass(inputs, inputs.now, limits, policy))


def queue_read_from_pass(inputs: QueueInputs, limits: AdmissionLimits, policy: AdmissionPolicy,
                         result: PassResult) -> QueueRead:
    """Apply admission to resolved evidence, with no file, process or environment reads."""
    ordered = ranked_queue_waiters(inputs)
    candidate = first_queued_waiter(row for _, row in ordered)
    running, reservations, now = inputs.running, inputs.reservations, inputs.now
    memory = [step.measured for step in running]
    identities = RecordedQueueIdentities()
    unavailable = isinstance(inputs.host, ReadingUnavailable)
    host = inputs.host if isinstance(inputs.host, HostMemory) else HostMemory(0, UnknownHostTotal())
    available, total = host.available, host.total
    recovery = result.recovery
    override = result.override.record if isinstance(result.override, OverrideKept) else NoHeadAdmissionOverride()
    pressure = result.totals.pressure
    explained: list[ExplainedRunningStep] = []
    reservation_index = 0
    for step in running:
        reading = step.measured
        admitted_at: float | AdmissionTimeNotRecorded = AdmissionTimeNotRecorded()
        if not isinstance(reading, (CompilerServiceMemory, UnownedSliceMemory)):
            admitted_at = _epoch(reservations[reservation_index]["admitted_at"])
            reservation_index += 1
        explained.append(ExplainedRunningStep(step, admitted_at, step.promise,
                         reading.bytes if isinstance(reading, MeasuredMemory) else 0,
                         build_charge(reading),
                         charge_basis(reading)))
    head, totals = result.head, result.totals
    waiting: list[WaitingStep] = []
    first: HeadExplanation | TokenWaitersOnly | NoWaiter = NoWaiter()
    line: list[LineWaiter] = []
    for (_, row), admission in zip(ordered, result.rows, strict=True):
        own = admission.decision
        identity = identities.identity(row["pid"], cast(dict[str, object], cast(object, row)))
        reason = admission.reason
        step = WaitingStep(row["pid"], identity["session_id"], identity["session_name"], identity["step"],
                           identity["folder"], row["since"], row["need"], reason)
        waiting.append(step)
        if isinstance(candidate, QueuedWaiter) and row is candidate.waiter:
            width = row.get("test_threads")
            source: RecordedNeed | NeedSourceNotRecorded = NeedSourceNotRecorded()
            if "need_source" in row:
                source = RecordedNeed(row["need_source"], row.get("need_runs", 0), HISTORY_DAYS,
                                      row.get("test_scope", "full"), row.get("package", ""),
                                      tuple(row.get("filters", [])), width if width is not None else WidthNotRecorded(),
                                      read_need_basis(row.get("need_basis")))
            has_priority = isinstance(head, HeadPriority)
            order: StrictUntil | BackfillUntil | WindowNotStarted = WindowNotStarted()
            if (has_priority and "priority_since" in row
                    or not has_priority and now - row["since"] < policy.timing.head_seconds):
                window = head_window(row, now, policy)
                if has_priority or isinstance(window, BackfillUntil):
                    order = window
            first = HeadExplanation(step, source, own,
                *decision_checks(own, step.need, available),
                order, has_priority)
        else:
            # Resource fit includes host pressure, independently of queue order.
            fit = decide(available, total, row["need"], memory, limits=limits)
            line.append(LineWaiter(step, isinstance(waiter_state(row), QueuedWaiter)
                                   and not unavailable and not isinstance(pressure, MemoryPressure)
                                   and fit.budget_passes and fit.free_passes, own,
                                   *decision_checks(own, step.need, available)))
    if waiting and isinstance(first, NoWaiter):
        first = TokenWaitersOnly()
    view = QueueView(tuple(waiting), tuple(running), available, totals.promised, totals.reserve, now)
    host_reading = (inputs.host if isinstance(inputs.host, ReadingUnavailable) else
                    HostAvailable(available, total,
                                  totals.reserve, totals.budget, pressure))
    explanation = QueueExplanation(now, host_reading, tuple(explained),
        QueueMemoryTotals(totals.charged, totals.measured, totals.promised), first,
        WaitingLine(tuple(line), len(line), sum(item.fits_now for item in line),
                    min((item.waiter.need for item in line), default=0),
                    max((item.waiter.need for item in line), default=0),
                    min((item.waiter.since for item in line), default=now)),
        inputs, limits, tuple(row for _, row in ordered), tuple(reservations), override, recovery, inputs.priority, policy=policy)
    return QueueRead(view, explanation, ordered)


@lru_cache(maxsize=None)
def snapshot_fields(schema: type) -> dict[str, object]:
    return cast(dict[str, object], get_type_hints(schema, include_extras=True))


def snapshot_value(value: object) -> object:
    """The cache's tagged records retain domain variants, including nested memory evidence."""
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {"kind": type(value).__name__, **{field.name: snapshot_value(cast(object, getattr(value, field.name)))
                                                for field in fields(value)}}
    if isinstance(value, Mapping):
        return {key: snapshot_value(item) for key, item in cast(Mapping[str, object], value).items()}
    if isinstance(value, frozenset):
        return [snapshot_value(item) for item in sorted(cast(frozenset[str], value))]
    if isinstance(value, (tuple, list)):
        return [snapshot_value(item) for item in cast(tuple[object, ...] | list[object], value)]
    return value


def installed_waiter_order(row: LegacyRankedWaiter, priority: QueuePriority | NoQueuePriority) -> PriorityUnit | NormalOrder:
    """Convert the installed service's rank into the current queue order."""
    rank = row.get("priority_rank", 1)
    if rank not in (0, 1) or rank == 0 and isinstance(priority, NoQueuePriority):
        raise ValueError("invalid installed waiter priority")
    return PriorityUnit(priority["unit"]) if rank == 0 and not isinstance(priority, NoQueuePriority) else NormalOrder()


def installed_snapshot_record(record: dict[str, object], schema: type) -> dict[str, object]:
    """Convert installed snapshot fields at the reader boundary."""
    if schema is QueuePriority and ("transition" not in record or
            isinstance(record["transition"], dict) and "kind" not in record["transition"]):
        priority = cast(LegacyQueuePriority, parse_snapshot_value(record, LegacyQueuePriority))
        transition = priority.get("transition")
        state = (SettledPriority() if transition is None else
                 PendingPriorityTransition(transition["id"], transition["action"], transition["at"]))
        return {**record, "transition": snapshot_value(state)}
    if (schema is BehindHead and record.get("kind") == "BehindHead"
            and record.keys() == snapshot_fields(LegacyBehindHead).keys() | {"kind"}):
        legacy = cast(LegacyBehindHead, parse_snapshot_value({**record, "kind": "LegacyBehindHead"}, LegacyBehindHead))
        return cast(dict[str, object], snapshot_value(BehindHead(legacy.head_name, legacy.head_since, legacy.head_session_id)))
    if schema not in (QueueInputs, QueueRead, QueueExplanation) or record.get("kind") != schema.__name__:
        return record
    if schema is QueueInputs:
        if record.keys() != snapshot_fields(schema).keys() - {"waiter_orders"} | {"kind"}:
            return record
    else:
        raw_ordered = cast(list[object], parse_snapshot_value(record["ordered"], list[object]))
        legacy_order = False
        for item in raw_ordered:
            row = item
            if schema is QueueRead:
                pair = cast(list[object], parse_snapshot_value(item, list[object]))
                if len(pair) != 2:
                    raise ValueError("invalid snapshot tuple")
                row = pair[1]
            legacy_order |= isinstance(row, dict) and "order" not in row
        if not legacy_order:
            return record
    context = record
    if schema is QueueRead:
        context = cast(dict[str, object], parse_snapshot_value(record["explanation"], dict[str, object]))
    queue_priority = cast(QueuePriority | NoQueuePriority,
                          parse_snapshot_value(context["priority"], QueuePriority | NoQueuePriority))
    if schema is QueueExplanation:
        rows = cast(tuple[LegacyRankedWaiter, ...], parse_snapshot_value(record["ordered"], tuple[LegacyRankedWaiter, ...]))
        ordered: object = [snapshot_value(rank_waiter(row, installed_waiter_order(row, queue_priority))) for row in rows]
    else:
        pairs = cast(list[tuple[Path, LegacyRankedWaiter]],
                     parse_snapshot_value(record["ordered"], list[tuple[Path, LegacyRankedWaiter]]))
        if schema is QueueInputs:
            return {**record, "waiter_orders": snapshot_value({waiter_owner(row): installed_waiter_order(row, queue_priority)
                                                               for _, row in pairs})}
        ordered = [[str(path), snapshot_value(rank_waiter(row, installed_waiter_order(row, queue_priority)))] for path, row in pairs]
    return {**record, "ordered": ordered}


def parse_snapshot_value(value: object, schema: object) -> object:
    """Validate the wire shape against the same domain fields that the writer serializes."""
    origin, args = get_origin(schema), cast(tuple[object, ...], get_args(schema))
    if schema is object:
        return value
    if origin is NotRequired:
        return parse_snapshot_value(value, args[0])
    # A union of Literal aliases still has typing.Union as its origin on 3.13.
    if origin is UnionType or origin is get_origin(NeedBasis):
        for variant in args:
            try:
                return parse_snapshot_value(value, variant)
            except (ValueError, TypeError, KeyError):
                continue
        raise ValueError("invalid snapshot variant")
    if origin is Literal:
        if value not in args:
            raise ValueError("invalid snapshot literal")
        return value
    if schema is type(None):
        if value is not None:
            raise ValueError("invalid snapshot null")
        return None
    if schema is str or schema is int or schema is float or schema is bool:
        valid = (type(value) is schema or schema is float and type(value) is int)
        if not valid or isinstance(value, (int, float)) and not math.isfinite(value):
            raise ValueError("invalid snapshot scalar")
        return value
    if schema is Path:
        return Path(cast(str, parse_snapshot_value(value, str)))
    if origin in (list, tuple, frozenset, Sequence):
        if not isinstance(value, list):
            raise ValueError("invalid snapshot sequence")
        items = cast(list[object], value)
        if origin in (list, frozenset, Sequence) or len(args) == 2 and args[1] is Ellipsis:
            parsed = [parse_snapshot_value(item, args[0]) for item in items]
        else:
            if len(items) != len(args):
                raise ValueError("invalid snapshot tuple")
            parsed = [parse_snapshot_value(item, kind) for item, kind in zip(items, args, strict=True)]
        return parsed if origin in (list, Sequence) else frozenset(parsed) if origin is frozenset else tuple(parsed)
    if origin in (dict, Mapping):
        if not isinstance(value, dict):
            raise ValueError("invalid snapshot mapping")
        return {parse_snapshot_value(key, args[0]): parse_snapshot_value(item, args[1])
                for key, item in cast(dict[str, object], value).items()}
    if not isinstance(schema, type) or not isinstance(value, dict):
        raise ValueError("invalid snapshot record")
    record = installed_snapshot_record(cast(dict[str, object], value), schema)
    annotations = snapshot_fields(schema)
    if is_typeddict(schema):
        if any(name not in record and get_origin(kind) is not NotRequired for name, kind in annotations.items()):
            raise ValueError("missing snapshot row field")
        return {name: parse_snapshot_value(item, annotations[name]) for name, item in record.items()
                if name in annotations}
    if schema is StepProfile and record.keys() == annotations.keys() - {"build_history_generation"} | {"kind"}:
        record = {**record, "build_history_generation": snapshot_value(NoBuildHistoryGeneration())}
    if not is_dataclass(schema) or record.get("kind") != schema.__name__:
        raise ValueError("invalid snapshot record tag")
    if record.keys() != annotations.keys() | {"kind"}:
        raise ValueError("invalid snapshot record fields")
    return cast(Callable[..., object], schema)(**{name: parse_snapshot_value(record[name], kind)
                                                for name, kind in annotations.items()})


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
    recovery: RecoverySpacing | ReadingNotTaken = ReadingNotTaken()
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
    host, head = explanation.host, explanation.head
    if isinstance(head, (NoWaiter, TokenWaitersOnly)):
        return head
    if isinstance(host, ReadingUnavailable):
        return host
    indices = frozenset(ended)
    released = [row for index, row in enumerate(explanation.running) if index in indices]
    inputs = inputs_after_steps_end(explanation.decided_from, indices)
    candidate = first_queued_waiter(row for _, row in inputs.ordered)
    if isinstance(candidate, NoWaiter):
        return candidate
    owner = waiter_owner(candidate.waiter)
    row = next(row for row in admission_pass(inputs, explanation.now, explanation.limits, explanation.policy).rows
               if row.owner == owner)
    if isinstance(row.reason, EstimateExpired):
        return row.reason
    decision = row.decision
    freed = released_resident_bytes(explanation.decided_from.running, indices)
    charge_drop = sum(row.charged for row in released)
    growth_drop = sum(growth_headroom(row.step.measured) for row in released)
    if decision.state == "admit":
        return HeadAdmitsAfterEnd(ended, decision, charge_drop, freed, growth_drop)
    causes: list[Literal["build budget", "free memory", "pressure", "queue order"]] = []
    if not decision.budget_passes:
        causes.append("build budget")
    if not decision.free_passes:
        causes.append("free memory")
    if isinstance(decision.pressure, MemoryPressure):
        causes.append("pressure")
        if causes == ["pressure"] and decision.pressure.next_start_at:
            return HeadStartsAt(decision.pressure.next_start_at, ended, charge_drop, freed, growth_drop)
    if not causes:
        causes.append("queue order")
    return HeadStillHeld(decision, tuple(causes))


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
    # These are bounds from the checks already recorded by admission, not admission verdicts.
    # A branch that might empty the build queue must bypass both bounds (the run-alone rule).
    charge_gap = head.budget.charged - head.budget.starts_at
    free_gap = head.free.threshold - head.free.available
    charges = [row.charged for row in explanation.running]
    free_releases = [growth_headroom(row.step.measured)
        + released_resident_bytes(explanation.decided_from.running, frozenset((index,)))
        for index, row in enumerate(explanation.running)]
    builds = frozenset(index for index, row in enumerate(explanation.running)
                       if has_running_step([row.step.measured]))
    max_charge: list[list[int]] = []
    min_charge: list[list[int]] = []
    max_free: list[list[int]] = []
    for start in range(count + 1):
        charge_order = sorted(charges[start:])
        free_order = sorted(free_releases[start:], reverse=True)
        min_charge.append([sum(charge_order[:size]) for size in range(count - start + 1)])
        max_charge.append([sum(charge_order[len(charge_order) - size:]) if size else 0
                           for size in range(count - start + 1)])
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
            if not can_empty and (charge + max_charge[start][slots] < charge_gap
                                  or freed + max_free[start][slots] < free_gap):
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
        evidence = cast(dict[str, object], raw)
        amount, selected_at = evidence.get("bytes"), evidence.get("selected_at")
        need = read_test_phase_need(evidence.get("need"))
        if (evidence.get("reservation_id") == reservation_id
                and type(amount) is int and amount > 0
                and isinstance(selected_at, (int, float)) and not isinstance(selected_at, bool)
                and math.isfinite(selected_at) and isinstance(need, TestPhaseNeed)):
            return {"bytes": amount, "selected_at": float(selected_at),
                    "reservation_id": reservation_id, "need": test_phase_need_record(need)}
    except (OSError, ValueError, TypeError):
        pass
    return NoTestPhaseChargeUse()


class FinalizationIntents(Protocol):
    def save(self, row: Reservation, intent: FinalizationIntent) -> None: ...
    def published(self, row: Reservation) -> None: ...
    def clear(self, row: Reservation) -> None: ...


def finalize_reservation(path: Path, row: Reservation, outcome: FinalOutcome, now: datetime,
                         status: int | OutcomeNotRecorded = OUTCOME_NOT_RECORDED, *,
                         intents: FinalizationIntents) -> HistoryRecorded | NoHistoryRecorded:
    """Save the peak before removing evidence; the service owns reservation state."""
    maximum = path.with_suffix(".max")
    if "finalization" not in row:
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
        }
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
        row["finalization"] = intent
        intents.save(row, intent)
    intent = row["finalization"]
    result: HistoryRecorded | NoHistoryRecorded = NoHistoryRecorded()
    history_path = path.parent / "anon_peaks.jsonl"
    if intent["peak"] > 0 and intent["scoped"]:
        # The store repairs an unfinished tail before the identity lookup.
        with store.locked_append(history_path) as handle:
            recorded = any(previous.get("reservation_id") == intent["reservation_id"]
                           for previous in anonymous_history_rows(handle))
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
    maximum.unlink(missing_ok=True)
    path.with_suffix(".wave").unlink(missing_ok=True)
    if row["sidecar"]:
        Path(row["sidecar"]).unlink(missing_ok=True)
    intents.clear(row)
    return result


def _epoch(instant: str) -> float:
    try:
        return datetime.fromisoformat(instant.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


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


@dataclass(frozen=True)
class AdmissionWaitSnapshot:
    decision: AdmissionDecision
    peak: ExpectedPeak
    running: tuple[RunningStep, ...]
    available: int
    repo: str
    step: str
    policy: AdmissionPolicy
    fallbacks: tuple[ConfigFallback, ...]
    head_need: int = 0
    head_label: str = ""
    head_reason: WaitReason = ReadyForAdmission()


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
            or isinstance(readings.recovery, ReadingNotTaken) or isinstance(readings.compilers, ReadingNotTaken)):
        raise ValueError("queue inputs require complete pass readings")
    running: list[RunningStep] = []
    rows: list[Reservation] = []
    for row, memory in reservations:
        if isinstance(memory, GoneMemory):
            continue
        identity = reservation_identity(row)
        running.append(RunningStep(identity["session_id"], identity["session_name"],
                                   identity["step"], identity["folder"], row["need"], memory))
        rows.append(row)
    running.extend(RunningStep("", "sccache.service", "compile", "builds.slice", 0, value)
                   for value in readings.compilers)
    running.extend(RunningStep("", value.owner, "unowned", value.cgroup, 0, value) for value in unowned)
    return QueueInputs(sorted(ordered, key=lambda item: waiter_order(item[1])), tuple(running), tuple(rows),
                       readings.host, readings.pressure, readings.recovery, override, now, priority)


def unowned_slice_memory(rows: list[Reservation], processes: dict[int, ProcessIdentity], proc: Path
                         ) -> tuple[UnownedSliceMemory, ...]:
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
    try:
        children = list(directory.iterdir())
    except OSError:
        return ()
    for child in children:
        if child in owned or child.name == "sccache.service" or not child.is_dir():
            continue
        try:
            current = child / "memory.current"
            check_memory_reading(current)
            amount = int(current.read_text())
            swap = child / "memory.swap.current"
            check_memory_reading(swap)
            swapped = int(swap.read_text()) if swap.exists() else 0
            members = {int(pid) for membership in child.rglob("cgroup.procs") for pid in membership.read_text().split()}
            duplicate = process_memory(members & tree_pids, proc).bytes
            amount = max(0, amount + swapped - duplicate)
            if amount:
                result.append(UnownedSliceMemory(amount, cgroup=str(child), owner=child.name))
        except (OSError, ValueError):
            continue
    return tuple(result)


def sampling_context(row: Reservation) -> dict[str, object]:
    """Copy the grant context for the build-log sampler."""
    return {"reservation_id": row.get("reservation_id", ""), "need": row["need"],
            "repo_identity": row.get("repo_identity", ""), "package": row.get("package", ""),
            "filters": row.get("filters", []), "test_scope": row.get("test_scope", "full"),
            "test_threads": snapshot_value(row["test_threads"]),
            "call_id": snapshot_value(row["call_id"]),
            "test_phase_need": test_phase_need_record(read_test_phase_need(row.get("test_phase_need"))),
            "test_phase_charge_use": snapshot_value(row.get("test_phase_charge_use", NoTestPhaseChargeUse()))}


class QueueServiceReader(Protocol):
    def view(self) -> QueueRead: ...


def queue_read() -> QueueRead:
    client = cast(QueueServiceReader, cast(object, importlib.import_module("scripts.build_queue.admission_client")))
    return client.view()
