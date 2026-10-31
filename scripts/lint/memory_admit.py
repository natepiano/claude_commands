#!/usr/bin/env python3
"""Expected process-memory peaks and atomic build admission."""

from __future__ import annotations

import argparse
import fcntl
from contextlib import AbstractContextManager, closing, contextmanager
import importlib
import io
import json
import math
import mmap
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from collections.abc import Generator, Iterator, Mapping
from contextlib import redirect_stderr
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from typing import BinaryIO, Callable, Literal, NoReturn, NotRequired, Protocol, TypedDict, cast, get_args, get_origin, get_type_hints, is_typeddict
from functools import lru_cache
from itertools import chain
from types import UnionType
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "buildlog"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from build_shape import BuildShape, NoBuildShape, build_shape, index_shapes, recorded_build_shape
from user_zone import user_zone
from atomic_file import write_text_atomic
from config_values import config_values


class GitFacts(TypedDict):
    repo_path: str | None
    worktree: str | None
    branch: str | None
    sha: str | None


class StepParser(Protocol):
    def step_name(self, argv: list[str]) -> str: ...


class GitIdentityReader(Protocol):
    def git_directory(self, argv: list[str], cwd: str) -> str: ...
    def git_facts(self, directory: str) -> GitFacts: ...
    def queue_priority(self, action: Literal["set", "clear"], unit: str,
                       why: str, set_by: str, at: float, *, transition_id: str = "") -> None: ...
    def head_admission(self, action: Literal["set", "admitted", "lapsed"], unit: str,
                       step: str, need: int, free: int, reserve: int, why: str,
                       set_by: str, at: float) -> None: ...


class BuildLogStore(Protocol):
    INDEX_NAME: str

    def root(self) -> Path: ...
    def host_name(self) -> str: ...
    def utc_iso(self, epoch: float) -> str: ...
    def append_line(self, path: Path, record: object) -> None: ...
    def locked_append(self, path: Path) -> AbstractContextManager[BinaryIO]: ...
    def append_locked(self, handle: BinaryIO, record: object) -> None: ...
    def whole_rows(self, handle: BinaryIO, start: int) -> Iterator[dict[str, object]]: ...
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
# Used only when neither the environment nor lint.conf supplies a valid budget.
FALLBACK_BUILD_BUDGET_BYTES = 21 * GIB
# Option C, 126 builds over 14 days to 2026-10-30: 1.314 covered every
# replayed release step (including the 15.76 GiB incident).
RELEASE_HEADROOM = 1.314
# Repair replay, same 126-build cohort to 2026-10-30: this smallest factor
# covers all 81 dev steps with prior shape history. The limiting cargo-berth
# build used 1.412826538 GiB after a 0.336112976 GiB same-shape peak.
DEV_HEADROOM = 4.2034275337646125
RAMP_SECONDS = 120
# Hana nextest: the longest measured gap between waves at >=50% of peak
# was 954 s (2026-10-29); 1500 s adds margin for sparse slice samples.
PEAK_DECAY_SECONDS = 1500
# 2026-10-29 freeze: full avg10 stayed below 10 during about 30 s of thrash.
FULL_PRESSURE_LIMIT = 5.0
# The same freeze had swap 80-87% used with some avg10 29-37%.
SWAP_USED_LIMIT = 85.0
SOME_PRESSURE_LIMIT = 25.0
# Four starts promised 10.3 GiB in 42 s before that 30 s thrash.
RECOVERY_START_SECONDS = 30
# Bound recovery after the freeze: one ramp window after pressure clears.
RECOVERY_WINDOW_SECONDS = 120
PROC = Path("/proc")
# A step held this long gets room first when it can start.
HEAD_SECONDS = 600
STRICT_SECONDS = 600
BACKFILL_SECONDS = 120
QUEUE_LOCK_TIMEOUT_SECONDS = 10.0
QUEUE_READ_LOCK_SECONDS = 0.5
SNAPSHOT_MAX_AGE_SECONDS = 10


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
NeedSource = LegacyNeedSource | Literal["shape"]


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
NeedBasis = LegacyNeedSource | ShapeNeedBasis


@dataclass(frozen=True)
class BasisNotRecorded:
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


class FinalizationIntent(TypedDict):
    reservation_id: str
    peak: int
    scoped: bool
    outcome: Literal["completed", "failed", "killed", "owner_gone"]
    ended_at: str
    status: NotRequired[int]


@dataclass(frozen=True)
class HistoryRecorded:
    pass


@dataclass(frozen=True)
class NoHistoryRecorded:
    pass


class Reservation(TypedDict):
    pid: int
    start_time: str
    need: int
    repo: str
    step: str
    worktree: str
    admitted_at: str
    sidecar: str
    test_threads: int | None
    call_id: str | None
    repo_identity: NotRequired[str]
    test_scope: NotRequired[TestScope]
    package: NotRequired[str]
    filters: NotRequired[list[str]]
    shape: NotRequired[str]
    reservation_id: NotRequired[str]
    scoped: NotRequired[bool]
    finalization: NotRequired[FinalizationIntent]
    queue_waiter: NotRequired[Waiter]


@dataclass(frozen=True)
class ExpectedPeak:
    bytes: int
    source: NeedSource
    count: int
    basis: NeedBasis | BasisNotRecorded = BasisNotRecorded()

    def __post_init__(self) -> None:
        if self.source == "shape" and not isinstance(self.basis, (ShapeP90, ShapeLargest, ShapeFallback)):
            raise ValueError("a shape estimate needs its shape basis")
        if isinstance(self.basis, BasisNotRecorded):
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
class PeakFileUnreadable:
    bytes: int


@dataclass(frozen=True)
class UsingNow:
    bytes: int


@dataclass(frozen=True)
class EndedCharge:
    bytes: int = 0


ChargeBasis = PromiseNoReading | RampPromise | LifetimePromise | PeakSoFar | PeakDecayed | PeakFileUnreadable | UsingNow | EndedCharge
PeakHistory = NoPeakRecorded | PeakSoFar | PeakDecayed | PeakFileUnreadable


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
class GoneMemory:
    pass


StepMemory = PromisedMemory | MeasuredMemory | CompilerServiceMemory | GoneMemory


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
    label: NotRequired[str]
    session_id: NotRequired[str]
    session_name: NotRequired[str]
    step: NotRequired[str]
    folder: NotRequired[str]
    priority_rank: NotRequired[int]
    token_left_at: NotRequired[float]
    priority_since: NotRequired[float]
    estimated_at: NotRequired[float]
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


class PriorityTransition(TypedDict):
    id: str
    action: Literal["set", "clear"]
    at: float


class QueuePriority(TypedDict):
    unit: str
    why: str
    set_at: float
    set_by: str
    transition: NotRequired[PriorityTransition]


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
        if "transition" in row:
            transition = row["transition"]
            if not isinstance(transition, dict):
                raise ValueError("invalid priority transition")
            fields = cast(dict[str, object], transition)
            at = fields.get("at")
            if (not isinstance(fields.get("id"), str) or not fields["id"]
                    or fields.get("action") not in ("set", "clear")
                    or not isinstance(at, (int, float)) or isinstance(at, bool) or not math.isfinite(at)):
                raise ValueError("invalid priority transition")
        return cast(QueuePriority, cast(object, row))
    except FileNotFoundError:
        return NO_QUEUE_PRIORITY
    except (OSError, ValueError, TypeError, OverflowError):
        print(f"memory admission: invalid queue priority {path}; ignored", file=sys.stderr)
        return NO_QUEUE_PRIORITY


def finish_priority_transition(directory: Path, priority: QueuePriority) -> None:
    transition = priority.get("transition")
    if transition is None:
        return
    record.queue_priority(transition["action"], priority["unit"], priority["why"],
                          priority["set_by"], transition["at"], transition_id=transition["id"])
    if transition["action"] == "set":
        del priority["transition"]
        write_text_atomic(queue_priority_path(directory), json.dumps(priority, separators=(",", ":")))
    else:
        queue_priority_path(directory).unlink(missing_ok=True)


def begin_priority_transition(directory: Path, priority: QueuePriority,
                              action: Literal["set", "clear"], at: float) -> None:
    priority["transition"] = {"id": uuid.uuid4().hex, "action": action, "at": at}
    write_text_atomic(queue_priority_path(directory), json.dumps(priority, separators=(",", ":")))
    finish_priority_transition(directory, priority)


def read_queue_priority(directory: Path, *, finish: bool = False) -> QueuePriority | NoQueuePriority:
    priority = load_queue_priority(directory)
    if isinstance(priority, NoQueuePriority):
        return priority
    transition = priority.get("transition")
    if transition is not None:
        if finish:
            finish_priority_transition(directory, priority)
        if transition["action"] == "clear":
            return NO_QUEUE_PRIORITY
    return priority


def priority_set(unit: str, why: str) -> str:
    actor = require_showrunner()
    if not unit.strip():
        raise PriorityRefused("unit is required")
    directory = admission_dir(store.root())
    with exclusive_queue(directory) as readings:
        previous = read_queue_priority(directory, finish=True)
        if (isinstance(previous, NoQueuePriority)
                or (previous["unit"], previous["why"], previous["set_by"]) != (unit, why, actor)):
            now = time.time()
            priority: QueuePriority = {"unit": unit, "why": why, "set_at": now, "set_by": actor}
            begin_priority_transition(directory, priority, "set", now)
        processes = process_table(Path(os.environ.get("BUILDLOG_PROC", "/proc")))
        readings.processes = processes
        snapshot = gather_queue_inputs(directory, processes, time.time(), readings=readings)
        place = next((place for place, (_, row) in enumerate(snapshot.ordered, 1)
                      if row.get("priority_rank") == 0), 0)
    result = f"first step now #{place}" if place else "no waiting step yet"
    return f"queue priority set for {unit}: {result}"


def priority_clear() -> str:
    _ = require_showrunner()
    directory = admission_dir(store.root())
    with exclusive_queue(directory):
        priority = read_queue_priority(directory, finish=True)
        if not isinstance(priority, NoQueuePriority):
            begin_priority_transition(directory, priority, "clear", time.time())
    return "queue priority cleared"


class PriorityArguments(argparse.Namespace):
    command: str = ""
    unit: str = ""
    why: str = ""


def run_priority(arguments: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="memory_admit.py priority")
    commands = parser.add_subparsers(dest="command", required=True)
    setting = commands.add_parser("set")
    _ = setting.add_argument("--unit", required=True)
    _ = setting.add_argument("--why", required=True)
    _ = commands.add_parser("clear")
    args = parser.parse_args(arguments, namespace=PriorityArguments())
    try:
        result = priority_set(args.unit, args.why) if args.command == "set" else priority_clear()
    except AdmitHeadRefused:
        if args.command != "set":
            raise
        print(f"build queue: type /build:queue bump {args.unit} in a showrunner pane")
        raise SystemExit(3) from None
    print(result)


class UnitMembership(Protocol):
    @property
    def unit(self) -> str: ...


class PriorityRoster(Protocol):
    def unit_labels_for(self, session_ids: list[str], environment: Mapping[str, str]) -> Mapping[str, UnitMembership]: ...


def priority_labels(session_ids: list[str]) -> Mapping[str, UnitMembership]:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    roster = cast(PriorityRoster, cast(object, importlib.import_module("scripts.message.roster")))
    return roster.unit_labels_for(session_ids, os.environ)


@dataclass(frozen=True)
class QueueOrderContext:
    priority: QueuePriority | NoQueuePriority
    labels: Mapping[str, UnitMembership]


@dataclass(frozen=True)
class QueueOrderNotResolved:
    pass


QUEUE_ORDER_NOT_RESOLVED = QueueOrderNotResolved()


def resolve_queue_order(directory: Path, rows: list[tuple[Path, Waiter]], *, finish: bool = False,
                        readings: QueueSnapshotReadings | None = None) -> tuple[list[tuple[Path, Waiter]], QueuePriority | NoQueuePriority]:
    context = readings.order if readings is not None else QUEUE_ORDER_NOT_RESOLVED
    if isinstance(context, QueueOrderNotResolved):
        priority = read_queue_priority(directory, finish=finish)
        labels: Mapping[str, UnitMembership] = (priority_labels([row.get("session_id", "") for _, row in rows])
                                                if not isinstance(priority, NoQueuePriority) else {})
        context = QueueOrderContext(priority, labels)
        if readings is not None:
            readings.order = context
    for _, row in rows:
        label = context.labels.get(row.get("session_id", ""))
        row["priority_rank"] = (0 if not isinstance(context.priority, NoQueuePriority)
                                and label is not None and label.unit == context.priority["unit"] else 1)
    return sorted(rows, key=lambda item: waiter_order(item[1])), context.priority


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


def end_head_override(directory: Path, action: Literal["admitted", "lapsed"], override: HeadAdmissionOverride,
                      need: int, free: int, reserve: int, at: float) -> None:
    head_override_event(action, override, need, free, reserve, at)
    head_override_path(directory).unlink()


def admit_head(why: str = "") -> str:
    actor = require_showrunner()
    directory = admission_dir(store.root())
    with exclusive_queue(directory):
        ordered, _ = resolve_queue_order(directory, live_waiters(directory), finish=True)
        if not ordered:
            raise AdmitHeadRefused("nothing waits; no head to admit")
        path, row = ordered[0]
        proc = Path(os.environ.get("BUILDLOG_PROC", "/proc"))
        identities = QueueIdentityReader(process_table(proc), proc)
        identity = identities.identity(row["pid"], cast(dict[str, object], cast(object, row)))
        host = read_meminfo(Path(os.environ.get("BUILDLOG_MEMINFO", "/proc/meminfo")))
        reserve = decide(host.available, host.total, row["need"], []).reserve
        override: HeadAdmissionOverride = {
            "waiter": path.name, "pid": row["pid"], "start_time": row["start_time"],
            "unit": waiter_unit_name(identity, row["pid"]),
            "step": row.get("step", ""), "need": row["need"], "free": host.available,
            "reserve": reserve, "why": why, "set_by": actor,
        }
        write_text_atomic(head_override_path(directory), json.dumps(override, separators=(",", ":")))
        head_override_event("set", override, row["need"], host.available, reserve, time.time())
        return f"head admission set for {override['unit']} {override['step']}"


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
    pass


@dataclass(frozen=True)
class EstimateExpired:
    estimated_at: float


def estimate_expired(waiter: Waiter, now: float) -> bool:
    return now - waiter.get("estimated_at", now) >= HEAD_SECONDS


WaitReason = EstimateExpired | NeedsBuildBudget | NeedsMemory | BehindHead | BehindOlderFit | HeadAdmissionStatus | ReadyForAdmission | ReadingUnavailable | MemoryPressure


def head_wait_reason(decision: AdmissionDecision, need: int, available: int,
                     waiter: Waiter, now: float) -> HeadAdmissionStatus:
    return HeadAdmissionStatus(decision, need, available, head_window(waiter, now))


def head_window(waiter: Waiter, now: float) -> BackfillUntil | StrictUntil:
    if "priority_since" not in waiter and now - waiter["since"] < HEAD_SECONDS:
        return BackfillUntil(waiter["since"] + HEAD_SECONDS)
    start = waiter.get("priority_since", now)
    cycle = STRICT_SECONDS + BACKFILL_SECONDS
    elapsed = max(0.0, now - start)
    cycle_start = start + (elapsed // cycle) * cycle
    if elapsed % cycle < STRICT_SECONDS:
        return StrictUntil(cycle_start + STRICT_SECONDS)
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


def head_wait_text(reason: HeadAdmissionStatus, zone: ZoneInfo) -> str:
    decision = reason.decision
    parts: list[str] = []
    if isinstance(decision.pressure, MemoryPressure):
        parts.append(pressure_text(decision.pressure))
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
    head: HeadExplanation | NoWaiter
    line: WaitingLine
    decided_from: QueueInputs
    ordered: tuple[Waiter, ...] = ()
    reservations: tuple[Reservation, ...] = ()
    override: HeadAdmissionOverride | NoHeadAdmissionOverride = NoHeadAdmissionOverride()
    recovery: RecoverySpacing = NoRecoverySpacing()
    priority: QueuePriority | NoQueuePriority = NO_QUEUE_PRIORITY


@dataclass(frozen=True)
class CurrentReading:
    pass


@dataclass(frozen=True)
class StaleReading:
    written_at: float


@dataclass(frozen=True)
class QueueBusy:
    pass


@dataclass(frozen=True)
class QueueInputs:
    """All resolved evidence for a queue decision; independent of the reader's host."""
    ordered: list[tuple[Path, Waiter]]
    running: tuple[RunningStep, ...]
    reservations: tuple[Reservation, ...]
    host: HostMemory | ReadingUnavailable
    pressure: Pressure
    recovery: RecoverySpacing
    limits: AdmissionLimits
    override: HeadAdmissionOverride | NoHeadAdmissionOverride
    now: float
    priority: QueuePriority | NoQueuePriority = NO_QUEUE_PRIORITY
    blockers_for: frozenset[str] = frozenset()


@dataclass(frozen=True)
class QueueRead:
    view: QueueView
    explanation: QueueExplanation
    ordered: list[tuple[Path, Waiter]]
    freshness: CurrentReading | StaleReading = CurrentReading()


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
HeadAfterEnd = HeadRelease | HeadStillHeld | EstimateExpired | NoWaiter | ReadingUnavailable


@dataclass(frozen=True)
class ProcessIdentity:
    parent: int
    start: str


@dataclass(frozen=True)
class Reserved:
    path: Path


@dataclass(frozen=True)
class NotReserved:
    reason: Literal["held", "launch", "untracked"]


@dataclass(frozen=True)
class StepProfile:
    repo: str
    step: str
    worktree: str
    peak: ExpectedPeak
    repository: Repository = UnknownRepository()
    scope: TestScope = "full"
    estimated_at: float = 0.0


@dataclass(frozen=True)
class InferFromArgv:
    pass


INFER_FROM_ARGV = InferFromArgv()


@dataclass(frozen=True)
class MeasuredGatePeak:
    bytes: int
    runs: int


@dataclass(frozen=True)
class NoGateHistory:
    cap_bytes: int


GateEstimate = MeasuredGatePeak | NoGateHistory


@dataclass(frozen=True)
class GateProfile:
    repo: str
    worktree: str
    cap_bytes: int
    estimate: GateEstimate
    estimated_at: float = 0.0

    @property
    def step(self) -> Literal["merge-gate"]:
        return "merge-gate"

    @property
    def peak(self) -> ExpectedPeak:
        estimate = self.estimate
        return (ExpectedPeak(estimate.bytes, "measured", estimate.runs)
                if isinstance(estimate, MeasuredGatePeak) else ExpectedPeak(estimate.cap_bytes, "fallback", 0))

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


def gate_estimate(cap_bytes: int, host: str, now: datetime, root: Path) -> GateEstimate:
    """Read the gate's one peak history, without using cargo estimates."""
    cutoff = now - timedelta(days=HISTORY_DAYS)
    peaks: list[int] = []
    try:
        for row in anonymous_history_rows(root / "admission/anon_peaks.jsonl"):
            try:
                if row.get("host") != host or row.get("step") != "merge-gate":
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
    return MeasuredGatePeak(percentile(peaks), len(peaks)) if peaks else NoGateHistory(cap_bytes)


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


class WaiterGone(FileNotFoundError):
    """The requested waiter has left the admission ledger."""


def blocker_fields(blocker: Blocker) -> dict[str, str]:
    if isinstance(blocker, ReservationBlocker):
        return {"session_id": blocker.session_id, "session_name": blocker.session_name, "pool": blocker.pool}
    if isinstance(blocker, UnitBlocker):
        return {"unit": blocker.unit}
    return {}


@dataclass(frozen=True)
class CheckResult:
    decision: AdmissionDecision
    peak: ExpectedPeak
    live: list[Reservation]
    reservation: Reserved | NotReserved
    repo: str
    step: str
    available: int
    profile: StepProfile | GateProfile
    head_need: int = 0
    head_label: str = ""
    head_reason: WaitReason = ReadyForAdmission()
    blocker: Blocker = NoSingleBlocker()


def _positive_gib_bytes(raw: str) -> int | None:
    try:
        value = float(raw) * GIB
        if math.isfinite(value) and value > 0:
            return max(1, int(value))
    except ValueError:
        pass
    return None


def _lint_config_path() -> str:
    return os.path.expanduser(os.environ.get("LINT_CONFIG_FILE") or "~/.claude/config/lint.conf")


def build_budget_bytes() -> int:
    """Resolve the live budget: environment, lint.conf, then the fallback."""
    environment_budget = _positive_gib_bytes(os.environ.get("BUILDLOG_BUILD_BUDGET_GIB", ""))
    if environment_budget is not None:
        return environment_budget
    config_budget = _positive_gib_bytes(config_values(_lint_config_path()).get("build_budget_gib", ""))
    if config_budget is not None:
        return config_budget
    return FALLBACK_BUILD_BUDGET_BYTES


def resolve_admission_limits(total: int | UnknownHostTotal) -> AdmissionLimits:
    return AdmissionLimits(build_budget_bytes(), total * 5 // 100 if isinstance(total, int) else 0)


def decide(available: int, total: int | UnknownHostTotal, need: int, running: list[StepMemory],
           pressure: Pressure = NO_PRESSURE_HOLD, *,
           limits: AdmissionLimits | ReadHostAdmissionLimits = READ_HOST_ADMISSION_LIMITS) -> AdmissionDecision:
    """Apply the build budget, free-memory floor and pressure limit together."""
    promised = sum(growth_headroom(row) for row in running)
    measured = sum(row.bytes for row in running if isinstance(row, MeasuredMemory))
    charged = sum(build_charge(row) for row in running)
    resolved = limits if isinstance(limits, AdmissionLimits) else resolve_admission_limits(total)
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
        observed = replace(peak, bytes=memory.observed_peak) if isinstance(peak, PeakSoFar) else PeakSoFar(memory.observed_peak)
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
                if not cutoff <= ended_at < now or not isinstance(peak, int) or peak <= 0:
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


def need_basis_record(basis: NeedBasis | BasisNotRecorded) -> dict[str, object]:
    if isinstance(basis, (BasisNotRecorded, str)):
        return {"kind": basis if isinstance(basis, str) else "not_recorded"}
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


def read_need_basis(raw: object) -> NeedBasis | BasisNotRecorded:
    """One boundary for cached estimates and queue explanations."""
    if not isinstance(raw, dict):
        return BasisNotRecorded()
    row = cast(dict[str, object], raw)
    kind = row.get("kind")
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
                  target_dir: str = "") -> bool:
    """Only the last completed matching test build can prove the targets fresh.

    The key covers Cargo build inputs, not nextest's run-only selection. Read
    the recorder's current JSONL, since the derived index can lag a finished step.
    """
    if not tree_key or not package or not worktree or not target_dir:
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
            target = Path(target_dir)
            if not target.is_absolute():
                target = Path(worktree) / target
            target = target.resolve()
            if not target.is_dir():
                return False
            state = Path(os.environ.get("LINT_SWEEP_STATE_DIR", "~/.local/state/lint-sweep")).expanduser()
            try:
                deleted = cast(object, json.loads((state / "target-deletes.json").read_text()))
            except FileNotFoundError:
                deleted = {}
            if not isinstance(deleted, dict):
                return False
            for path, timestamp in cast(dict[str, object], deleted).items():
                if Path(path).resolve() == target:
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


def tree_memory(pid: int, start: str, processes: dict[int, ProcessIdentity],
                proc: Path = PROC) -> MeasuredMemory | GoneMemory:
    """Sum resident and swapped bytes from one process table reading."""
    identity = processes.get(pid)
    if identity is None or identity.start != start:
        return GoneMemory()
    children: dict[int, list[int]] = {}
    for child, identity in processes.items():
        children.setdefault(identity.parent, []).append(child)
    pending, seen, used, swapped = [pid], set[int](), 0, 0
    while pending:
        child = pending.pop()
        if child in seen:
            continue
        seen.add(child)
        pending.extend(children.get(child, []))
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
                proc: Path = PROC) -> StepMemory:
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
    if isinstance(reading, MeasuredMemory) and release_build(row):
        return replace(reading, promise=LifetimePromise(row["need"]))
    if isinstance(reading, MeasuredMemory) and now.timestamp() - _epoch(row["admitted_at"]) < RAMP_SECONDS:
        if reading.bytes == 0:
            return PromisedMemory(row["need"])
        return replace(reading, promise=RampPromise(row["need"]))
    return reading


def release_build(row: Reservation) -> bool:
    shape = recorded_build_shape(row.get("shape"))
    return row.get("step") == "build" and isinstance(shape, BuildShape) and shape.profile == "release"


def reservation_memory(path: Path, row: Reservation, now: datetime,
                       processes: dict[int, ProcessIdentity], proc: Path) -> StepMemory:
    reading = step_memory(row, now, processes, proc)
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
    if not release_build(row) and now.timestamp() - last_near_peak_at >= PEAK_DECAY_SECONDS:
        history = PeakDecayed(0, peak, last_near_peak_at, last_near_peak_at + PEAK_DECAY_SECONDS, old_sampler)
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
        with Path(record_path).open("a") as output:
            fcntl.flock(output, fcntl.LOCK_EX)
            _ = output.write(json.dumps({"reading": live_path, "argv": sys.argv, "pid": os.getpid()}) + "\n")
            output.flush()
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
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] == "kB" and parts[1].isdecimal():
            values[parts[0].removesuffix(":")] = int(parts[1]) * 1024
    if "MemAvailable" not in values:
        raise ValueError(f"MemAvailable missing from {path}")
    return HostMemory(values["MemAvailable"], values.get("MemTotal", UnknownHostTotal()),
                      values.get("SwapTotal", 0), values.get("SwapFree", 0))


NO_HOST_SWAP = HostMemory(0, UnknownHostTotal())


def read_pressure(host: HostMemory = NO_HOST_SWAP) -> Pressure:
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
    if swap > SWAP_USED_LIMIT and some > SOME_PRESSURE_LIMIT:
        return MemoryPressure(full, some, swap)
    return MemoryPressure(full) if full > FULL_PRESSURE_LIMIT else NO_PRESSURE_HOLD


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


def write_recovery(directory: Path, recovery: PressureHoldRecorded | RecoveryStarts) -> None:
    data = ({"state": "held"} if isinstance(recovery, PressureHoldRecorded) else
            {"state": "recovering", "ends_at": recovery.ends_at, "next_start_at": recovery.next_start_at})
    # All writers hold the admission lock; readers see one complete snapshot.
    temporary = directory / "pressure-recovery.tmp"
    try:
        _ = temporary.write_text(json.dumps(data))
        os.replace(temporary, directory / "pressure-recovery")
    finally:
        temporary.unlink(missing_ok=True)


def next_recovery(recovery: RecoverySpacing, pressure: Pressure, now: float) -> RecoverySpacing:
    """Advance pressure recovery without consuming a proposed admission's start slot."""
    if isinstance(pressure, MemoryPressure):
        return PressureHoldRecorded()
    if isinstance(recovery, PressureHoldRecorded):
        return RecoveryStarts(now + RECOVERY_WINDOW_SECONDS, now)
    if isinstance(recovery, RecoveryStarts) and now >= recovery.ends_at:
        return NoRecoverySpacing()
    return recovery


def pressure_with_recovery(recovery: RecoverySpacing, pressure: Pressure, now: float) -> Pressure:
    if isinstance(pressure, MemoryPressure):
        return pressure
    if isinstance(recovery, RecoveryStarts) and now < min(recovery.ends_at, recovery.next_start_at):
        return MemoryPressure(0, next_start_at=min(recovery.ends_at, recovery.next_start_at))
    return pressure


def pressure_text(pressure: MemoryPressure) -> str:
    if pressure.next_start_at:
        return "memory pressure recovery: starts at least 30 s apart"
    if pressure.swap_used_percent > SWAP_USED_LIMIT:
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
    return cast(Reservation, cast(object, row))


def reservation_rows(directory: Path) -> list[tuple[Path, Reservation]]:
    rows: list[tuple[Path, Reservation]] = []
    for path in directory.glob("*.reservation"):
        try:
            rows.append((path, read_reservation(path)))
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return rows


def live_reservations(directory: Path) -> list[tuple[Path, Reservation]]:
    """A read-only lookup: only the locked check or release finalizes rows."""
    return [(path, row) for path, row in reservation_rows(directory)
            if not isinstance(reservation_lifetime(row, process_start_time(row["pid"]) == row["start_time"]), EndedStep)]


def read_waiter(path: Path) -> Waiter:
    """Validate persisted waiter fields with the same schema used by snapshots."""
    value = cast(object, json.loads(path.read_text()))
    return stored_waiter(cast(Waiter, parse_snapshot_value(value, Waiter)))


def stored_waiter(row: Waiter) -> Waiter:
    """Keep resolved queue order in the snapshot, never in a durable row."""
    saved = row.copy()
    _ = saved.pop("priority_rank", None)
    return saved


def live_waiters(directory: Path) -> list[tuple[Path, Waiter]]:
    live: list[tuple[Path, Waiter]] = []
    for path in directory.glob("*.waiting"):
        try:
            row = read_waiter(path)
            if process_start_time(row["pid"]) == row["start_time"]:
                live.append((path, row))
                continue
        except (OSError, ValueError, KeyError, TypeError):
            pass
        path.unlink(missing_ok=True)
    return live


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
    return row.get("priority_rank", 1), row["since"], row["pid"]


def head_priority(waiters: list[Waiter], available: int, total: int | UnknownHostTotal,
                  running: list[StepMemory], now: float, *,
                  limits: AdmissionLimits | ReadHostAdmissionLimits = READ_HOST_ADMISSION_LIMITS) -> HeadPriority | NoHeadPriority:
    ordered = waiters
    if not ordered:
        return NoHeadPriority()
    head = ordered[0]
    if "priority_since" in head or now - head["since"] >= HEAD_SECONDS:
        decision = decide(available, total, head["need"], running, limits=limits)
        if ("priority_since" in head or decision.state == "admit"
                or available + decision.measured >= head["need"] + decision.reserve):
            return HeadPriority(head, head_window(head, now))
    return NoHeadPriority()


@dataclass(frozen=True)
class WaiterAdmission:
    waiter: Waiter
    decision: AdmissionDecision


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
                           ordered: list[tuple[Path, Waiter]]) -> HeadAdmissionOverride | NoHeadAdmissionOverride:
    if isinstance(override, NoHeadAdmissionOverride) or not ordered:
        return NoHeadAdmissionOverride()
    path, row = ordered[0]
    return override if head_override_matches(override, path, row) else NoHeadAdmissionOverride()


def admission_for_waiter(available: int, total: int | UnknownHostTotal, row: Waiter,
                         running: list[StepMemory], head: HeadPriority | NoHeadPriority,
                         pressure: Pressure, room_used: int,
                         override: HeadAdmissionOverride | NoHeadAdmissionOverride, first: bool,
                         now: float, limits: AdmissionLimits | ReadHostAdmissionLimits) -> AdmissionDecision:
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
        fresh = not estimate_expired(row, now)
        decision = replace(decision, state="admit" if fits_free and fresh and isinstance(pressure, NoPressureHold) else "hold",
                           threshold=threshold, promised=0, free_passes=fits_free)
    return decision


def admission_walk(available: int, total: int | UnknownHostTotal, ordered: list[Waiter],
                   running: list[StepMemory], head: HeadPriority | NoHeadPriority,
                   pressure: Pressure, *, recovery: RecoverySpacing, now: float,
                   room_used: int = 0,
                   override: HeadAdmissionOverride | NoHeadAdmissionOverride = NO_HEAD_ADMISSION_OVERRIDE,
                   limits: AdmissionLimits | ReadHostAdmissionLimits = READ_HOST_ADMISSION_LIMITS) -> list[WaiterAdmission]:
    """Keep memory, the head's budget room and the recovery slot in queue order."""
    promised = list(running)
    marked: list[Waiter] = []
    admissions: list[WaiterAdmission] = []
    recovering = isinstance(recovery, RecoveryStarts) and now < recovery.ends_at
    marked_room = 0
    for index, row in enumerate(ordered):
        decision = admission_for_waiter(available, total, row, promised, head, pressure,
                                        room_used + marked_room, override, index == 0, now, limits)
        if marked and ((decision.state == "admit" and (recovering or decide(
                           available, total, row["need"],
                           [*running, *(PromisedMemory(waiter["need"]) for waiter in marked)],
                           pressure=pressure, limits=limits).state == "hold"))
                       or (decision.state == "hold" and admission_for_waiter(
                           available, total, row, running, head, pressure, room_used,
                           override, index == 0, now, limits).state == "admit")):
            decision = replace(decision, state="hold", order_hold=HeldBehindOlderFit(marked[0]))
        expired = estimate_expired(row, now)
        if expired:
            decision = replace(decision, state="hold")
        admissions.append(WaiterAdmission(row, decision))
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
    for row in rows:
        at = row.get("at")
        if isinstance(at, (int, float)) and at < left:
            return False
        if all(row.get(key) == event.get(key)
               for key in ("kind", "pid", "start_time", "token_left_at")):
            return True
    return False


def append_queue_time(directory: Path, event: dict[str, object], now: float) -> None:
    """Append under the admission lock; a token gap can be retried after a stop."""
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


class TokenWaitIntent(TypedDict):
    reservation: str
    waiter: Waiter


def finish_token_wait(path: Path) -> None:
    """Finish one token-wait transaction under the admission lock."""
    intent = cast(TokenWaitIntent, parse_snapshot_value(cast(object, json.loads(path.read_text())), TokenWaitIntent))
    reservation = Path(intent["reservation"])
    waiter = stored_waiter(intent["waiter"])
    try:
        row = read_reservation(reservation)
    except FileNotFoundError:
        pass
    else:
        _ = finalize_reservation(reservation, row, Completed(), datetime.now(UTC), 0)
    if process_start_time(waiter["pid"]) == waiter["start_time"]:
        target = path.parent / f"{waiter['pid']}-{waiter['start_time']}.waiting"
        write_text_atomic(target, json.dumps(waiter, separators=(",", ":")))
    path.unlink(missing_ok=True)


def token_wait(path: Path, root: Path | None = None) -> None:
    directory = admission_dir(root if root is not None else store.root())
    with exclusive_queue(directory):
        intent_path = path.with_suffix(".token-wait")
        if not intent_path.exists():
            try:
                row = read_reservation(path)
            except FileNotFoundError:
                return
            if "queue_waiter" not in row:
                raise ValueError("reservation has no recorded queue place")
            waiter = stored_waiter(row["queue_waiter"])
            waiter["token_left_at"] = time.time()
            intent: TokenWaitIntent = {"reservation": str(path), "waiter": waiter}
            write_text_atomic(intent_path, json.dumps(intent, separators=(",", ":")))
        finish_token_wait(intent_path)


def check(meminfo: Path, pid: int, sidecar: str, argv: list[str], since: float = 0.0,
          root: Path | None = None, now: datetime | None = None,
          profile: InferFromArgv | StepProfile | GateProfile = INFER_FROM_ARGV) -> CheckResult:
    """Admit or hold one step. A held step is a waiter from `since` (epoch seconds; 0 means now), so a
    step that gave its reservation back for the cargo token keeps its place."""
    root = root if root is not None else store.root()
    estimate_time = now if now is not None else datetime.now(UTC)
    fresh = False
    width = test_threads(argv)
    package, filters = test_package(argv), test_filters(argv)
    if isinstance(profile, GateProfile) and (not profile.estimated_at or estimate_time.timestamp() - profile.estimated_at >= HEAD_SECONDS):
        profile = replace(profile, estimate=gate_estimate(profile.cap_bytes, store.host_name(), estimate_time, root),
                          estimated_at=estimate_time.timestamp())
    elif isinstance(profile, StepProfile) and profile.estimated_at and estimate_time.timestamp() - profile.estimated_at >= HEAD_SECONDS:
        profile = InferFromArgv()
    if isinstance(profile, InferFromArgv):
        repository, worktree = git_identity(argv, os.getcwd())
        repo = repository.name if isinstance(repository, RepositoryIdentity) else ""
        scope = test_scope(argv)
        step = parse.step_name(argv) if argv else "build"
        host = store.host_name()
        worktree_path = ""
        if step == "nextest" and package and os.environ.get("BUILDLOG_TEST_BUILD_KEY"):
            directory = record.git_directory(argv, os.getcwd())
            worktree_path = record.git_facts(directory)["worktree"] or ""
        target_dir = os.environ.get("BUILDLOG_TEST_TARGET_DIR", "")
        fresh = (step == "nextest" and is_fresh_test(
            root, host, worktree_path, package, scope,
            os.environ.get("BUILDLOG_TEST_BUILD_KEY", ""), estimate_time, target_dir))
        peak = expected_peak(repository, step, host, estimate_time, root, scope=scope,
                             package=package, filters=filters, fresh=fresh,
                             shape=build_shape(argv) if step == "build" else NoBuildShape())
        profile = StepProfile(repo, step, worktree, peak, repository, scope, estimate_time.timestamp())
    elif not profile.estimated_at:
        profile = replace(profile, estimated_at=estimate_time.timestamp())
    repo, step, worktree, peak = profile.repo, profile.step, profile.worktree, profile.peak
    if not argv and not (root / "admission").exists():
        host = read_meminfo(meminfo)
        available = host.available
        pressure = read_pressure(host)
        if isinstance(pressure, NoPressureHold):
            decision = decide(available, host.total, peak.bytes, sccache_memory(), pressure)
            return CheckResult(decision, peak, [], NotReserved("launch"), repo, step, available, profile)
        # A launch can be the first caller to observe a pressure hold. Record
        # it under the lock so launches after it share the recovery window.
    directory = admission_dir(root)
    with exclusive_queue(directory) as snapshot:
        # Lock contention must not consume the recovery spacing or ramp window.
        # Use one time for every decision and ledger write in this transaction.
        now = now if now is not None else datetime.now(UTC)
        proc = Path(os.environ.get("BUILDLOG_PROC", "/proc"))
        processes = process_table(proc)
        snapshot.processes = processes
        live: list[tuple[Path, Reservation]] = []
        for intent_path in directory.glob("*.token-wait"):
            finish_token_wait(intent_path)
        for path, row in reservation_rows(directory):
            memory = reservation_memory(path, row, now, processes, proc)
            snapshot.memory[path] = memory
            if isinstance(memory, GoneMemory):
                finalized = finalize_reservation(path, row, OwnerGone(), now)
                if (isinstance(finalized, HistoryRecorded) and step == row["step"] == "build"
                        and history_matches_repository(row, profile.repository)
                        and ("shape" not in row or recorded_build_shape(row["shape"]) == build_shape(argv))):
                    # Use the existing expiry path; the next poll estimates outside the lock.
                    # An old row may get its shape from the index outside this lock.
                    profile = replace(profile, estimated_at=now.timestamp() - HEAD_SECONDS)
                continue
            live.append((path, row))
        compilers = sccache_memory()
        snapshot.compilers = compilers
        start = process_start_time(pid)
        mine = directory / f"{pid}-{start}.waiting"
        waiting = live_waiters(directory)
        previous = next((row for path, row in waiting if path == mine), None)
        waited_from = since or now.timestamp()
        if previous is not None:
            waited_from = min(waited_from, previous["since"])
        waiter: Waiter = {"pid": pid, "start_time": start or "", "need": peak.bytes, "since": waited_from,
                          "label": f"{worktree or repo} {step} ({os.environ.get('BUILDLOG_CALLER') or 'caller'} pid {pid})"}
        waiter["need_source"] = peak.source
        waiter["test_scope"] = profile.scope
        waiter["package"] = package
        waiter["filters"] = list(filters)
        waiter["estimated_at"] = profile.estimated_at
        waiter["fresh_test"] = fresh
        waiter["need_source"] = peak.source
        waiter["need_runs"] = peak.count
        waiter["need_basis"] = need_basis_record(peak.basis)
        waiter["test_threads"] = width
        waiter["repo_identity"] = ""
        if isinstance(profile.repository, RepositoryIdentity):
            waiter["repo_identity"] = profile.repository.key
            waiter["repository_name"] = profile.repository.name
            waiter["legacy_name"] = profile.repository.legacy_name
        if previous is not None:
            if "priority_since" in previous:
                waiter["priority_since"] = previous["priority_since"]
            if "token_left_at" in previous:
                waiter["token_left_at"] = previous["token_left_at"]
        identity = waiter_identity(step, worktree)
        waiter["session_id"], waiter["session_name"] = identity["session_id"], identity["session_name"]
        waiter["step"], waiter["folder"] = identity["step"], profile.worktree if isinstance(profile, GateProfile) else identity["folder"]
        host = read_meminfo(meminfo)
        snapshot.host = host
        available = host.available
        pressure = read_pressure(host)
        snapshot.pressure = pressure
        recovery = read_recovery(directory)
        snapshot.recovery = recovery
        if "token_left_at" in waiter and argv and os.environ.get("BUILDLOG_MEM_TOKEN_BACK_AT"):
            back = float(os.environ["BUILDLOG_MEM_TOKEN_BACK_AT"])
            append_queue_time(directory, {
                "kind": "token_gap", "session_id": identity["session_id"], "step": step,
                "pid": pid, "start_time": start or "", "token_left_at": waiter["token_left_at"],
                "token_back_at": back}, now.timestamp())
            del waiter["token_left_at"]
            write_text_atomic(mine, json.dumps(stored_waiter(waiter), separators=(",", ":")))
        ordered, priority = resolve_queue_order(directory,
            [(path, row) for path, row in waiting if path != mine] + [(mine, waiter)],
            finish=True, readings=snapshot)
        inputs = gather_queue_inputs(directory, processes, now.timestamp(), snapshot.memory, readings=snapshot)
        inputs = replace(inputs, ordered=ordered, priority=priority,
                         blockers_for=frozenset((waiter_owner(waiter),)))
        result = admission_pass(inputs, now.timestamp())
        own = next(row for row in result.rows if row.owner == waiter_owner(waiter))
        decision = own.decision
        for mark in result.head_marks:
            if mark.path == mine:
                waiter = mark.waiter
            else:
                write_text_atomic(mark.path, json.dumps(stored_waiter(mark.waiter), separators=(",", ":")))
        head_need, head_label = 0, ""
        if isinstance(decision.order_hold, HeldBehindHead) and isinstance(decision.head_room, HeadRoom):
            head_need = decision.head_room.head_need
            held = decision.order_hold.waiter
            head_label = held.get("label") or waiter_unit_name(
                RecordedQueueIdentities().identity(held["pid"], cast(dict[str, object], cast(object, held))), held["pid"])
        if isinstance(own.reason, EstimateExpired):
            print("memory admission: estimate expired while waiting for the lock; refresh on the next poll", file=sys.stderr)
        blocker = blocker_identity(own.blocker, list(inputs.reservations), processes, proc)
        applied_recovery = own.admission.recovery if isinstance(own.admission, ProposedAdmission) else result.recovery
        applied_override = own.admission.override if isinstance(own.admission, ProposedAdmission) else result.override
        commit_pass_effects(directory, applied_recovery, applied_override, available, decision.reserve,
                            peak.bytes, now.timestamp())
        snapshot.recovery = applied_recovery
        reservation: Reserved | NotReserved = NotReserved("held" if argv else "launch")
        if argv and decision.state == "hold" and start is not None:
            write_text_atomic(mine, json.dumps(stored_waiter(waiter), separators=(",", ":")))
        if argv and decision.state == "admit":
            if start is not None:
                path = directory / f"{pid}-{start}-{time.time_ns()}.reservation"
                row: Reservation = {"pid": pid, "start_time": start, "need": peak.bytes,
                                    "repo": repo, "step": step, "worktree": worktree,
                                    "admitted_at": store.utc_iso(now.timestamp()), "sidecar": sidecar,
                                    "test_threads": width,
                                    "call_id": os.environ.get("BUILDLOG_CALL_ID") or None,
                                    "reservation_id": uuid.uuid4().hex, "scoped": bool(sidecar)}
                row["queue_waiter"] = stored_waiter(waiter)
                row["test_scope"] = profile.scope
                row["package"] = package
                row["filters"] = list(filters)
                if step == "build":
                    row["shape"] = build_shape(argv).key
                if isinstance(profile.repository, RepositoryIdentity):
                    row["repo_identity"] = profile.repository.key
                write_text_atomic(path, json.dumps(row, separators=(",", ":")))
                reservation = Reserved(path)
            else:
                reservation = NotReserved("untracked")
            mine.unlink(missing_ok=True)
        head_reason = own.reason
        return CheckResult(decision, peak, [row for _, row in live], reservation, repo, step, available, profile,
                           head_need, head_label, head_reason, blocker)


def commit_pass_effects(directory: Path, recovery: RecoverySpacing,
                        override: OverrideKept | OverrideEnded, available: int, reserve: int,
                        need: int, now: float) -> None:
    if isinstance(recovery, NoRecoverySpacing):
        (directory / "pressure-recovery").unlink(missing_ok=True)
    else:
        write_recovery(directory, recovery)
    if isinstance(override, OverrideEnded):
        end_head_override(directory, override.action, override.record,
                          need if override.action == "admitted" else override.record["need"],
                          available, reserve, now)


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


def queue_rows(directory: Path, suffix: str, processes: dict[int, ProcessIdentity]) -> list[tuple[Path, dict[str, object]]]:
    rows: list[tuple[Path, dict[str, object]]] = []
    for path in sorted(directory.glob(f"*.{suffix}")):
        try:
            row = (cast(dict[str, object], cast(object, read_reservation(path))) if suffix == "reservation"
                   else cast(dict[str, object], cast(object, read_waiter(path))))
            pid = row["pid"]
            identity = processes.get(pid) if isinstance(pid, int) else None
            if isinstance(pid, int) and (suffix == "reservation" or (identity is not None and identity.start == row["start_time"])):
                rows.append((path, row))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return rows


def waiter_owner(row: Waiter) -> str:
    return f"{row['pid']}-{row['start_time']}"


def admission_pass(inputs: QueueInputs, now: float) -> PassResult:
    """Decide one frozen scenario, proposing ledger changes without committing any."""
    ordered = sorted(((path, row.copy()) for path, row in inputs.ordered), key=lambda item: waiter_order(item[1]))
    memory = [step.measured for step in inputs.running]
    host = inputs.host if isinstance(inputs.host, HostMemory) else HostMemory(0, UnknownHostTotal())
    available, total = host.available, host.total
    recovery = next_recovery(inputs.recovery, inputs.pressure, now)
    pressure = pressure_with_recovery(recovery, inputs.pressure, now)
    head = head_priority([row for _, row in ordered], available, total, memory, now, limits=inputs.limits)
    marks: tuple[HeadMark, ...] = ()
    if isinstance(head, HeadPriority) and "priority_since" not in head.waiter:
        marked = head.waiter.copy()
        marked["priority_since"] = now
        path = next(path for path, row in ordered if row is head.waiter)
        marks = (HeadMark(path, marked),)
        head.waiter.update(marked)
    raw_override = inputs.override
    override = snapshot_head_override(raw_override, ordered)
    outcome: OverrideKept | OverrideEnded = OverrideKept(override)
    if not isinstance(raw_override, NoHeadAdmissionOverride) and not any(
            head_override_matches(raw_override, path, row) for path, row in ordered):
        outcome = OverrideEnded(raw_override, "lapsed")
    totals = decide(available, total, 0, memory, pressure=pressure, limits=inputs.limits)
    admissions = admission_walk(available, total, [row for _, row in ordered], memory, head, pressure,
        recovery=recovery, now=now, room_used=head_room_used(list(inputs.reservations), memory, head),
        override=override, limits=inputs.limits)
    rows: list[PassRow] = []
    endings: tuple[PassResult, ...] = ()
    rank = 0
    for index, entry in enumerate(admissions):
        row, decision = entry.waiter, entry.decision
        if isinstance(inputs.host, ReadingUnavailable):
            decision = replace(decision, state="hold")
            reason: WaitReason = ReadingUnavailable()
        elif estimate_expired(row, now):
            reason = EstimateExpired(row.get("estimated_at", now))
        elif index == 0:
            reason = head_wait_reason(decision, row["need"], available, row, now)
        else:
            reason = queue_wait_reason(decision, row["need"], available, RecordedQueueIdentities())
        proposal: ProposedAdmission | NotAdmitted = NotAdmitted()
        if decision.state == "admit":
            rank += 1
            admitted_recovery = (replace(recovery, next_start_at=now + RECOVERY_START_SECONDS)
                                 if isinstance(recovery, RecoveryStarts) else recovery)
            admitted_override = (OverrideEnded(override, "admitted") if index == 0
                                 and not isinstance(override, NoHeadAdmissionOverride) else outcome)
            proposal = ProposedAdmission(rank, admitted_recovery, admitted_override)
        owner = waiter_owner(row)
        blocker: BlockingIndex | NoSingleBlocker | BlockerNotRequested = BlockerNotRequested()
        if owner in inputs.blockers_for:
            if decision.state == "hold" and not isinstance(inputs.host, ReadingUnavailable):
                if not endings:
                    endings = tuple(admission_pass(inputs_after_steps_end(inputs, frozenset((ended,))), now)
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
        if not isinstance(step.measured, CompilerServiceMemory):
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


def blocker_snapshot(waiter_path: Path) -> Blocker:
    # queue_rows is the queue view's read-only counterpart to live_reservations
    # and live_waiters: stale rows are ignored, never removed under a shared lock.
    if not waiter_path.exists():
        raise WaiterGone(f"waiter file is gone: {waiter_path}")
    directory = waiter_path.parent
    proc = Path(os.environ.get("BUILDLOG_PROC", "/proc"))
    processes = process_table(proc)
    inputs = gather_queue_inputs(directory, processes, time.time())
    matching = [row for path, row in inputs.ordered if path == waiter_path]
    if not matching:
        if not waiter_path.exists():
            raise WaiterGone(f"waiter file is gone: {waiter_path}")
        return NoSingleBlocker()
    owner = waiter_owner(matching[0])
    result = admission_pass(replace(inputs, blockers_for=frozenset((owner,))), inputs.now)
    row = next((row for row in result.rows if row.owner == owner), None)
    return (blocker_identity(row.blocker, list(inputs.reservations), processes, proc)
            if row is not None else NoSingleBlocker())


def current_blocker(waiter_path: Path) -> Blocker:
    """Read the waiter's blocker without changing the admission ledger."""
    try:
        lock = (waiter_path.parent / "lock").open("r")
    except FileNotFoundError:
        return blocker_snapshot(waiter_path)
    with lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        return blocker_snapshot(waiter_path)


NO_MEMORY_READINGS: Mapping[Path, StepMemory] = {}


@dataclass(frozen=True)
class ReadingNotTaken:
    pass


READING_NOT_TAKEN = ReadingNotTaken()


def gather_queue_inputs(directory: Path, processes: dict[int, ProcessIdentity], now: float,
                        memory: Mapping[Path, StepMemory] = NO_MEMORY_READINGS, *,
                        readings: QueueSnapshotReadings | ReadingNotTaken = READING_NOT_TAKEN) -> QueueInputs:
    """Resolve every file, process and environment input before the pure decision walk."""
    if isinstance(readings, ReadingNotTaken):
        readings = QueueSnapshotReadings(ProcessesNotRead(), {})
    proc = Path(os.environ.get("BUILDLOG_PROC", "/proc"))
    identities = QueueIdentityReader(processes, proc)
    running: list[RunningStep] = []
    reservations: list[Reservation] = []
    for path, row in queue_rows(directory, "reservation", processes):
        reservation = cast(Reservation, cast(object, row))
        pid = reservation["pid"]
        reading = memory[path] if path in memory else reservation_memory(
            path, reservation, datetime.fromtimestamp(now, UTC), processes, proc)
        if isinstance(reading, GoneMemory):
            continue
        identity = identities.identity(pid, row)
        step = RunningStep(identity["session_id"], identity["session_name"], identity["step"],
                           identity["folder"], reservation["need"], reading)
        running.append(step)
        reservations.append(reservation)
    compilers = readings.compilers
    if isinstance(compilers, ReadingNotTaken):
        compilers = sccache_memory()
        readings.compilers = compilers
    for compiler in compilers:
        running.append(RunningStep("", "sccache.service", "compile", "builds.slice", 0, compiler))
    host = readings.host
    if isinstance(host, ReadingNotTaken):
        try:
            host = read_meminfo(Path(os.environ.get("BUILDLOG_MEMINFO", "/proc/meminfo")))
        except (OSError, ValueError):
            host = ReadingUnavailable()
        readings.host = host
    recovery = readings.recovery
    if isinstance(recovery, ReadingNotTaken):
        recovery = read_recovery(directory)
        readings.recovery = recovery
    pressure = readings.pressure
    if isinstance(pressure, ReadingNotTaken):
        pressure = read_pressure(host if isinstance(host, HostMemory) else HostMemory(0, UnknownHostTotal()))
        readings.pressure = pressure
    rows = [(path, cast(Waiter, cast(object, row))) for path, row in queue_rows(directory, "waiting", processes)]
    for _, row in rows:
        identity = identities.identity(row["pid"], cast(dict[str, object], cast(object, row)))
        row["session_id"], row["session_name"] = identity["session_id"], identity["session_name"]
        row["step"], row["folder"] = identity["step"], identity["folder"]
    ordered, priority = resolve_queue_order(directory, rows, readings=readings)
    limits = resolve_admission_limits(host.total if isinstance(host, HostMemory) else UnknownHostTotal())
    return QueueInputs(ordered, tuple(running), tuple(reservations), host, pressure, recovery, limits,
                       read_head_override(directory), now, priority=priority)


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


def queue_read_from(inputs: QueueInputs) -> QueueRead:
    """Apply admission to resolved evidence, with no file, process or environment reads."""
    ordered = sorted(inputs.ordered, key=lambda item: waiter_order(item[1]))
    running, reservations, now = inputs.running, inputs.reservations, inputs.now
    memory = [step.measured for step in running]
    identities = RecordedQueueIdentities()
    unavailable = isinstance(inputs.host, ReadingUnavailable)
    host = inputs.host if isinstance(inputs.host, HostMemory) else HostMemory(0, UnknownHostTotal())
    available, total = host.available, host.total
    result = admission_pass(inputs, now)
    recovery, limits = result.recovery, inputs.limits
    override = result.override.record if isinstance(result.override, OverrideKept) else NoHeadAdmissionOverride()
    pressure = result.totals.pressure
    explained: list[ExplainedRunningStep] = []
    reservation_index = 0
    for step in running:
        reading = step.measured
        admitted_at: float | AdmissionTimeNotRecorded = AdmissionTimeNotRecorded()
        if not isinstance(reading, CompilerServiceMemory):
            admitted_at = _epoch(reservations[reservation_index]["admitted_at"])
            reservation_index += 1
        explained.append(ExplainedRunningStep(step, admitted_at, step.promise,
                         reading.bytes if isinstance(reading, MeasuredMemory) else 0,
                         build_charge(reading), charge_basis(reading)))
    head, totals = result.head, result.totals
    waiting: list[WaitingStep] = []
    first: HeadExplanation | NoWaiter = NoWaiter()
    line: list[LineWaiter] = []
    for (_, row), admission in zip(ordered, result.rows, strict=True):
        own = admission.decision
        identity = identities.identity(row["pid"], cast(dict[str, object], cast(object, row)))
        reason = admission.reason
        step = WaitingStep(row["pid"], identity["session_id"], identity["session_name"], identity["step"],
                           identity["folder"], row["since"], row["need"], reason)
        waiting.append(step)
        if row is ordered[0][1]:
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
                    or not has_priority and now - row["since"] < HEAD_SECONDS):
                window = head_window(row, now)
                if has_priority or isinstance(window, BackfillUntil):
                    order = window
            first = HeadExplanation(step, source, own,
                *decision_checks(own, step.need, available),
                order, has_priority)
        else:
            # Resource fit includes host pressure, independently of queue order.
            fit = decide(available, total, row["need"], memory, limits=limits)
            line.append(LineWaiter(step, not unavailable and not isinstance(pressure, MemoryPressure)
                                   and fit.budget_passes and fit.free_passes, own,
                                   *decision_checks(own, step.need, available)))
    view = QueueView(tuple(waiting), tuple(running), available, totals.promised, totals.reserve, now)
    host_reading = (ReadingUnavailable() if unavailable else
                    HostAvailable(available, total,
                                  totals.reserve, totals.budget, pressure))
    explanation = QueueExplanation(now, host_reading, tuple(explained),
        QueueMemoryTotals(totals.charged, totals.measured, totals.promised), first,
        WaitingLine(tuple(line), len(line), sum(item.fits_now for item in line),
                    min((item.waiter.need for item in line), default=0),
                    max((item.waiter.need for item in line), default=0),
                    min((item.waiter.since for item in line), default=now)),
        inputs, tuple(row for _, row in ordered), tuple(reservations), override, recovery, inputs.priority)
    return QueueRead(view, explanation, ordered)



def read_queue_pass(directory: Path, processes: dict[int, ProcessIdentity], now: float) -> QueueRead:
    return queue_read_from(gather_queue_inputs(directory, processes, now))


def read_queue(directory: Path, processes: dict[int, ProcessIdentity], now: float) -> tuple[QueueView, list[tuple[Path, Waiter]]]:
    result = read_queue_pass(directory, processes, now)
    return result.view, result.ordered


@dataclass(frozen=True)
class QueueSnapshot:
    written_at: float
    inputs: QueueInputs


@dataclass(frozen=True)
class NoQueueSnapshot:
    pass


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
    if isinstance(value, dict):
        return {key: snapshot_value(item) for key, item in cast(dict[str, object], value).items()}
    if isinstance(value, frozenset):
        return [snapshot_value(item) for item in sorted(cast(frozenset[str], value))]
    if isinstance(value, (tuple, list)):
        return [snapshot_value(item) for item in cast(tuple[object, ...] | list[object], value)]
    return value


def parse_snapshot_value(value: object, schema: object) -> object:
    """Validate the wire shape against the same domain fields that the writer serializes."""
    origin, args = get_origin(schema), cast(tuple[object, ...], get_args(schema))
    if schema is object:
        return value
    if origin is NotRequired:
        return parse_snapshot_value(value, args[0])
    # A union of Literal aliases still has typing.Union as its origin on 3.13.
    if origin is UnionType or origin is get_origin(NeedSource):
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
    if origin in (list, tuple, frozenset):
        if not isinstance(value, list):
            raise ValueError("invalid snapshot sequence")
        items = cast(list[object], value)
        if origin in (list, frozenset) or len(args) == 2 and args[1] is Ellipsis:
            parsed = [parse_snapshot_value(item, args[0]) for item in items]
        else:
            if len(items) != len(args):
                raise ValueError("invalid snapshot tuple")
            parsed = [parse_snapshot_value(item, kind) for item, kind in zip(items, args, strict=True)]
        return parsed if origin is list else frozenset(parsed) if origin is frozenset else tuple(parsed)
    if origin is dict:
        if not isinstance(value, dict):
            raise ValueError("invalid snapshot mapping")
        return {parse_snapshot_value(key, args[0]): parse_snapshot_value(item, args[1])
                for key, item in cast(dict[str, object], value).items()}
    if not isinstance(schema, type) or not isinstance(value, dict):
        raise ValueError("invalid snapshot record")
    record = cast(dict[str, object], value)
    annotations = snapshot_fields(schema)
    if is_typeddict(schema):
        if any(name not in record and get_origin(kind) is not NotRequired for name, kind in annotations.items()):
            raise ValueError("missing snapshot row field")
        return {name: parse_snapshot_value(item, annotations[name]) for name, item in record.items()
                if name in annotations}
    if not is_dataclass(schema) or record.get("kind") != schema.__name__:
        raise ValueError("invalid snapshot record tag")
    if record.keys() != annotations.keys() | {"kind"}:
        raise ValueError("invalid snapshot record fields")
    return cast(Callable[..., object], schema)(**{name: parse_snapshot_value(record[name], kind)
                                                for name, kind in annotations.items()})


def write_queue_snapshot(directory: Path, inputs: QueueInputs) -> None:
    """Publish a cache atomically under LOCK_EX; durability is deliberately unnecessary."""
    encoded = json.dumps({"v": 1, "written_at": time.time(), "inputs": snapshot_value(inputs)},
                         separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    temporary = directory / "queue.json.tmp"
    try:
        _ = temporary.write_text(encoded)
        os.replace(temporary, directory / "queue.json")
    finally:
        temporary.unlink(missing_ok=True)


def read_queue_snapshot(directory: Path) -> QueueSnapshot | NoQueueSnapshot:
    try:
        raw = cast(object, json.loads((directory / "queue.json").read_text()))
        if not isinstance(raw, dict):
            return NoQueueSnapshot()
        envelope = cast(dict[str, object], raw)
        if type(envelope.get("v")) is not int or envelope["v"] != 1:
            return NoQueueSnapshot()
        written_at = cast(float, parse_snapshot_value(envelope["written_at"], float))
        inputs = cast(QueueInputs, parse_snapshot_value(envelope["inputs"], QueueInputs))
        if sum(not isinstance(step.measured, CompilerServiceMemory) for step in inputs.running) != len(inputs.reservations):
            return NoQueueSnapshot()
        return QueueSnapshot(written_at, inputs)
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        return NoQueueSnapshot()


def queue_read() -> QueueRead | QueueBusy:
    snapshot = read_queue_snapshot(store.root() / "admission")
    if isinstance(snapshot, QueueSnapshot) and 0 <= time.time() - snapshot.written_at <= SNAPSHOT_MAX_AGE_SECONDS:
        return queue_read_from(snapshot.inputs)
    try:
        return locked_queue_read(timeout=QUEUE_READ_LOCK_SECONDS)
    except TimeoutError:
        if isinstance(snapshot, QueueSnapshot):
            return replace(queue_read_from(snapshot.inputs), freshness=StaleReading(snapshot.written_at))
        return QueueBusy()


@dataclass(frozen=True)
class ProcessesNotRead:
    pass


@dataclass
class QueueSnapshotReadings:
    processes: dict[int, ProcessIdentity] | ProcessesNotRead
    memory: dict[Path, StepMemory]
    host: HostMemory | ReadingUnavailable | ReadingNotTaken = ReadingNotTaken()
    pressure: Pressure | ReadingNotTaken = ReadingNotTaken()
    recovery: RecoverySpacing | ReadingNotTaken = ReadingNotTaken()
    compilers: list[StepMemory] | ReadingNotTaken = ReadingNotTaken()
    order: QueueOrderContext | QueueOrderNotResolved = QUEUE_ORDER_NOT_RESOLVED


@contextmanager
def exclusive_queue(directory: Path) -> Generator[QueueSnapshotReadings]:
    """Every exclusive exit publishes the resulting ledger, including exceptional exits."""
    with (directory / "lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        readings = QueueSnapshotReadings(ProcessesNotRead(), {})
        try:
            yield readings
        finally:
            try:
                processes = (process_table(Path(os.environ.get("BUILDLOG_PROC", "/proc")))
                             if isinstance(readings.processes, ProcessesNotRead) else readings.processes)
                write_queue_snapshot(directory, gather_queue_inputs(
                    directory, processes, time.time(), readings.memory, readings=readings))
            except Exception:
                # A cache failure cannot change admission or mask the section's exception.
                pass


def step_matches(selector: str, step: WaitingStep | RunningStep) -> bool:
    return selector in (step.session_id, step.session_name, step.folder, Path(step.folder).name)


def match_waiters(selector: str, view: QueueView) -> list[int]:
    if selector.isdecimal():
        place = int(selector)
        return [place] if 1 <= place <= len(view.waiting) else []
    return [place for place, step in enumerate(view.waiting, 1) if step_matches(selector, step)]


def _queue_lock_timeout(_signum: int, _frame: object) -> NoReturn:
    raise TimeoutError(f"admission lock busy for {QUEUE_LOCK_TIMEOUT_SECONDS:g} s; try again")


def locked_queue_read(timeout: float = QUEUE_LOCK_TIMEOUT_SECONDS) -> QueueRead:
    directory = store.root() / "admission"
    try:
        lock = (directory / "lock").open("r")
    except FileNotFoundError:
        return read_queue_pass(directory, process_table(Path(os.environ.get("BUILDLOG_PROC", "/proc"))), time.time())
    with lock:
        previous = signal.signal(signal.SIGALRM, _queue_lock_timeout)
        try:
            _ = signal.setitimer(signal.ITIMER_REAL, timeout)
            fcntl.flock(lock, fcntl.LOCK_SH)
        finally:
            _ = signal.setitimer(signal.ITIMER_REAL, 0)
            _ = signal.signal(signal.SIGALRM, previous)
        return read_queue_pass(directory, process_table(Path(os.environ.get("BUILDLOG_PROC", "/proc"))), time.time())


def queue_view() -> QueueView:
    return locked_queue_read().view


def explain_queue() -> QueueExplanation:
    """Explain admission under the queue's shared lock without history or SQLite reads."""
    return locked_queue_read().explanation


def head_after_steps_end(explanation: QueueExplanation, ended: tuple[int, ...]) -> HeadAfterEnd:
    """Reapply admission to this snapshot after the named running rows end; read no files."""
    host, head = explanation.host, explanation.head
    if isinstance(head, NoWaiter):
        return head
    if isinstance(host, ReadingUnavailable):
        return host
    indices = frozenset(ended)
    released = [row for index, row in enumerate(explanation.running) if index in indices]
    inputs = inputs_after_steps_end(explanation.decided_from, indices)
    row = admission_pass(inputs, explanation.now).rows[0]
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


def cancel_waiter(pid: int, root: Path | None = None) -> None:
    """Remove this caller's queue place when its in-process wait stops."""
    root = root if root is not None else store.root()
    directory = admission_dir(root)
    with exclusive_queue(directory):
        start = process_start_time(pid)
        (directory / f"{pid}-{start}.waiting").unlink(missing_ok=True)


def finalize_reservation(path: Path, row: Reservation, outcome: FinalOutcome, now: datetime,
                         status: int | OutcomeNotRecorded = OUTCOME_NOT_RECORDED) -> HistoryRecorded | NoHistoryRecorded:
    """Keep a scoped step's peak before removing its evidence; caller owns the lock."""
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
        row["finalization"] = intent
        write_text_atomic(path, json.dumps(row, separators=(",", ":")))
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
                    "test_threads": row.get("test_threads"), "call_id": row.get("call_id"),
                    "repo_identity": row.get("repo_identity"), "test_scope": row.get("test_scope", "full"),
                    "package": row.get("package", ""), "filters": row.get("filters", []),
                    "ended_at": intent["ended_at"],
                }
                if "status" in intent:
                    history["status"] = intent["status"]
                if "shape" in row:
                    history["shape"] = row["shape"]
                store.append_locked(handle, history)
        result = HistoryRecorded()
    maximum.unlink(missing_ok=True)
    path.with_suffix(".wave").unlink(missing_ok=True)
    if row["sidecar"]:
        Path(row["sidecar"]).unlink(missing_ok=True)
    path.unlink(missing_ok=True)
    return result


def release(path: Path, status: int, root: Path | None = None) -> None:
    root = root if root is not None else store.root()
    directory = admission_dir(root)
    with exclusive_queue(directory):
        try:
            row = read_reservation(path)
        except (OSError, ValueError, TypeError):
            return
        outcome = Completed() if status == 0 else Killed() if status >= 128 else Failed()
        _ = finalize_reservation(path, row, outcome, datetime.now(UTC), status)


def set_width(path: Path, width: int, root: Path | None = None) -> None:
    """Record the test width a reserved step runs at, claimed after its admission."""
    root = root if root is not None else store.root()
    with exclusive_queue(admission_dir(root)):
        try:
            row = cast(Reservation, json.loads(path.read_text()))
        except (OSError, ValueError, TypeError):
            return
        row["test_threads"] = width
        temporary = path.with_suffix(".reservation.tmp")
        try:
            _ = temporary.write_text(json.dumps(row, separators=(",", ":")))
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _epoch(instant: str) -> float:
    try:
        return datetime.fromisoformat(instant.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def clean_field(value: str) -> str:
    return value.replace("|", " ").replace("\n", " ")


def run_action() -> None:
    action = sys.argv[1]
    if action == "priority":
        run_priority(sys.argv[2:])
        return
    if action == "token-wait":
        token_wait(Path(sys.argv[2]))
        return
    if action == "admit-head":
        if len(sys.argv) != 2 and not (len(sys.argv) == 4 and sys.argv[2] == "--why"):
            raise AdmitHeadRefused('usage: memory_admit.py admit-head [--why "<text>"]')
        print(admit_head(sys.argv[3] if len(sys.argv) == 4 else ""))
        return
    if action == "blocker":
        if len(sys.argv) != 4 or sys.argv[2] != "--waiter":
            raise ValueError("usage: memory_admit.py blocker --waiter <waiter file>")
        try:
            blocker = current_blocker(Path(sys.argv[3]))
        except WaiterGone as error:
            print(str(error), file=sys.stderr)
            sys.exit(2)
        print(json.dumps(blocker_fields(blocker), separators=(",", ":")))
        return
    if action == "tripwire":
        check_memory_reading(Path(sys.argv[2]))
        return
    if action == "release":
        release(Path(sys.argv[2]), int(sys.argv[3]))
        return
    if action == "width":
        set_width(Path(sys.argv[2]), int(sys.argv[3]))
        return
    meminfo, pid, sidecar, since, cached_repo, cached_step, cached_worktree, cached_need = sys.argv[2:10]
    argv = sys.argv[12:]
    profile: InferFromArgv | StepProfile = InferFromArgv()
    if cached_need != "-":
        start = process_start_time(int(pid))
        try:
            row = read_waiter(store.root() / "admission" / f"{pid}-{start}.waiting")
            basis = read_need_basis(row.get("need_basis"))
            # An empty key is the explicit unknown identity. Old rows must refresh.
            if (not row.get("fresh_test", False) and not isinstance(basis, BasisNotRecorded)
                    and "repo_identity" in row and "test_scope" in row and "estimated_at" in row
                    and (row["test_scope"] == "full"
                         or (row.get("package") == test_package(argv)
                             and row.get("filters") == list(test_filters(argv))))
                    and 0 <= time.time() - row["estimated_at"] < HEAD_SECONDS):
                key = row["repo_identity"]
                repository: Repository = (RepositoryIdentity(key, row.get("repository_name", cached_repo), row.get("legacy_name", ""))
                                          if key else UnknownRepository())
                profile = StepProfile(cached_repo, cached_step, cached_worktree,
                                      ExpectedPeak(row["need"], basis if isinstance(basis, str) else "shape",
                                                   row.get("need_runs", 0), basis),
                                      repository, row["test_scope"], row["estimated_at"])
        except (OSError, ValueError, TypeError, KeyError):
            pass
    # The shell combines stdout and stderr and requires one protocol line.
    # Keep nonfatal read diagnostics visible in its existing summary field.
    diagnostics = io.StringIO()
    with redirect_stderr(diagnostics):
        result = check(Path(meminfo), int(pid), sidecar, argv, float(since or 0), profile=profile)
    decision, peak, live = result.decision, result.peak, result.live
    summary = ", ".join(f"{row['repo']} {row['step']} in {row['worktree']}" for row in live)
    if isinstance(result.head_reason, HeadAdmissionStatus):
        summary = f"{head_wait_text(result.head_reason, user_zone())}; {summary}".rstrip("; ")
    elif isinstance(result.head_reason, BehindOlderFit):
        summary = f"{older_fit_text(result.head_reason)}; {summary}".rstrip("; ")
    elif isinstance(decision.pressure, MemoryPressure):
        summary = f"{pressure_text(decision.pressure)}; {summary}".rstrip("; ")
    if diagnostics.getvalue():
        summary = "; ".join(filter(None, [summary, *diagnostics.getvalue().splitlines()]))
    reservation = result.reservation
    path = str(reservation.path) if isinstance(reservation, Reserved) else ""
    reason = "reserved" if isinstance(reservation, Reserved) else reservation.reason
    fields: list[str | int] = [decision.state, decision.threshold, peak.bytes, peak.source, peak.count,
                               decision.promised, len(live), decision.reserve, summary, path, result.repo, result.step,
                               result.available, reason, result.profile.worktree]
    # Running shells may still read the old field count.
    if os.environ.get("BUILDLOG_MEM_DETAIL") == "1":
        fields.extend([decision.measured, result.head_need, result.head_label])
    else:
        details = f"{decision.measured / GIB:.1f} GiB measured"
        if result.head_need:
            details += f"; head {result.head_label} needs {result.head_need / GIB:.1f} GiB"
        fields[8] = f"{summary}; {details}" if summary else details
    output = "|".join(clean_field(str(field)) for field in fields)
    if os.environ.get("BUILDLOG_MEM_BLOCKER") == "1":
        # Old shells enforce their field count. New callers opt into one JSON field.
        output += "|" + json.dumps(blocker_fields(result.blocker), separators=(",", ":")).replace("|", "\\u007c")
    print(output)


def main() -> None:
    try:
        run_action()
    except (LiveMemoryReading, AdmitHeadRefused, PriorityRefused) as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
