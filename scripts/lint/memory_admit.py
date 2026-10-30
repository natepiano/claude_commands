#!/usr/bin/env python3
"""Expected process-memory peaks and atomic build admission."""

from __future__ import annotations

import fcntl
import importlib
import io
import json
import math
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import redirect_stderr
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, NoReturn, NotRequired, Protocol, TypedDict, cast
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "buildlog"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from user_zone import user_zone


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
    def queue_bump(self, actor: str, session: str, step: str, folder: str,
                   old_position: int, at: float) -> None: ...


class BuildLogStore(Protocol):
    INDEX_NAME: str

    def root(self) -> Path: ...
    def host_name(self) -> str: ...
    def utc_iso(self, epoch: float) -> str: ...
    def append_line(self, path: Path, record: object) -> None: ...
    def host_file(self, host: str, epoch: float) -> Path: ...


parse: StepParser = cast(StepParser, cast(object, importlib.import_module("parse")))
record: GitIdentityReader = cast(GitIdentityReader, cast(object, importlib.import_module("record")))
store: BuildLogStore = cast(BuildLogStore, cast(object, importlib.import_module("store")))

GIB = 1 << 30
FALLBACK = 12 * GIB
# 111 natedev filtered single-test runs with no recompilation in the 14 days
# to 2026-10-29 had anon p90 0.671 GiB and p95 0.784 GiB.
FILTERED_FLOOR = GIB
# On natedev, isolated hana nextest scopes had p90 anon / memory.peak 61%;
# clippy had 65%, so 0.65 covers the measured p90 process-memory share.
ANON_SHARE = 0.65
HISTORY_DAYS = 14
# Build anon at 16–20 GiB averaged 10.4% full PSI; at 20–24 GiB it averaged 16.4%.
BUILD_BUDGET_BYTES = 21 * GIB
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


@dataclass(frozen=True)
class PackageTestSelection:
    package: str
    filters: tuple[str, ...]


@dataclass(frozen=True)
class BroadTestSelection:
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


@dataclass(frozen=True)
class ExpectedPeak:
    bytes: int
    source: Literal["measured", "buildlog", "pooled", "fallback"]
    count: int


@dataclass(frozen=True)
class PromisedMemory:
    bytes: int


@dataclass(frozen=True)
class MeasuredMemory:
    bytes: int
    # Only a step inside its ramp window reserves growth.
    ramp_promise: int = 0
    # Swap counts toward the build charge, but its release does not free RAM.
    swap_bytes: int = 0
    observed_peak: int = 0
    # An existing peak file could not be read; its promise remains a floor.
    unknown_peak_promise: int = 0


@dataclass(frozen=True)
class CompilerServiceMemory(MeasuredMemory):
    """Compiler charge, without a build reservation or run-alone veto."""


@dataclass(frozen=True)
class GoneMemory:
    pass


StepMemory = PromisedMemory | MeasuredMemory | GoneMemory


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
class HostMemory:
    available: int
    total: int | None
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
    bump_order: NotRequired[int]
    holds_younger: NotRequired[bool]
    priority_since: NotRequired[float]
    estimated_at: NotRequired[float]
    repo_identity: NotRequired[str]
    repository_name: NotRequired[str]
    legacy_name: NotRequired[str]
    test_scope: NotRequired[TestScope]
    package: NotRequired[str]
    filters: NotRequired[list[str]]


@dataclass(frozen=True)
class HeadPriority:
    waiter: Waiter
    window: StrictUntil | BackfillUntil


@dataclass(frozen=True)
class NoHeadPriority:
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
    budget: int = BUILD_BUDGET_BYTES


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
    head_need: int = 0


@dataclass(frozen=True)
class BehindHead:
    head_name: str
    head_since: float
    head_session_id: str


@dataclass(frozen=True)
class BackfillUntil:
    deadline: float


@dataclass(frozen=True)
class StrictUntil:
    deadline: float


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


WaitReason = NeedsBuildBudget | NeedsMemory | BehindHead | HeadAdmissionStatus | ReadyForAdmission | ReadingUnavailable | MemoryPressure


def head_wait_reason(decision: AdmissionDecision, need: int, available: int,
                     waiter: Waiter, now: float) -> HeadAdmissionStatus:
    return HeadAdmissionStatus(decision, need, available, head_window(waiter, now))


def head_window(waiter: Waiter, now: float) -> BackfillUntil | StrictUntil:
    if (not waiter.get("holds_younger", False) and waiter.get("bump_order", 0) >= 0
            and now - waiter["since"] < HEAD_SECONDS):
        return BackfillUntil(waiter["since"] + HEAD_SECONDS)
    start = waiter.get("priority_since", now)
    cycle = STRICT_SECONDS + BACKFILL_SECONDS
    elapsed = max(0.0, now - start)
    cycle_start = start + (elapsed // cycle) * cycle
    if elapsed % cycle < STRICT_SECONDS:
        return StrictUntil(cycle_start + STRICT_SECONDS)
    return BackfillUntil(cycle_start + cycle)


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
    until = datetime.fromtimestamp(reason.backfill.deadline, zone).strftime("%H:%M")
    if isinstance(reason.backfill, BackfillUntil):
        parts.append(f"younger steps may start until {until}")
    else:
        parts.append(f"younger steps wait until {until}")
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
    profile: StepProfile
    head_need: int = 0
    head_label: str = ""
    head_reason: HeadAdmissionStatus | ReadyForAdmission = ReadyForAdmission()
    blocker: Blocker = NoSingleBlocker()


def build_budget_bytes() -> int:
    try:
        value = float(os.environ["BUILDLOG_BUILD_BUDGET_GIB"]) * GIB
        if math.isfinite(value) and value > 0:
            return max(1, int(value))
    except (KeyError, ValueError):
        pass
    return BUILD_BUDGET_BYTES


def decide(available: int, total: int | None, need: int, running: list[StepMemory],
           head_need: int = 0, pressure: Pressure = NO_PRESSURE_HOLD) -> AdmissionDecision:
    """Apply the build budget, free-memory floor and pressure limit together."""
    promised = sum(growth_headroom(row) for row in running)
    measured = sum(row.bytes for row in running if isinstance(row, MeasuredMemory))
    charged = sum(build_charge(row) for row in running)
    budget = build_budget_bytes()
    reserve = total * 5 // 100 if total is not None else 0
    threshold = need + promised + reserve + head_need
    fits_budget = charged + need + head_need <= budget or (not has_running_step(running) and not head_need)
    admit = fits_budget and available >= threshold and isinstance(pressure, NoPressureHold)
    return AdmissionDecision("admit" if admit else "hold", threshold, promised, reserve, measured,
                             pressure, charged, budget)


def has_running_step(running: list[StepMemory]) -> bool:
    return any(not isinstance(row, (CompilerServiceMemory, GoneMemory)) for row in running)


def build_charge(memory: StepMemory) -> int:
    if isinstance(memory, PromisedMemory):
        return memory.bytes
    if isinstance(memory, MeasuredMemory):
        return max(memory.ramp_promise, memory.bytes, memory.observed_peak, memory.unknown_peak_promise)
    return 0


def growth_headroom(memory: StepMemory) -> int:
    if isinstance(memory, PromisedMemory):
        return memory.bytes
    if isinstance(memory, MeasuredMemory):
        return max(0, memory.ramp_promise - memory.bytes)
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
                  filters: tuple[str, ...] = (), package: str = "") -> ExpectedPeak:
    """The p90 process-memory peak of a step over HISTORY_DAYS.

    The gate pools every test width: one width's history is thin and rebuild-heavy, and the
    claim of a width comes only after admission. `test_threads` narrows the measured history to
    one width, for width_trial.py's report.
    """
    if isinstance(repository, UnknownRepository):
        return ExpectedPeak(FALLBACK, "fallback", 0)
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
        with (root / "admission/anon_peaks.jsonl").open() as lines:
            for line in lines:
                try:
                    item = cast(dict[str, object], json.loads(line))
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


def history_arguments(raw: object) -> list[str]:
    """Decode argv from the build-log index; absent arguments prove no selection."""
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
        with (root / "admission/anon_peaks.jsonl").open() as lines:
            for line in lines:
                try:
                    item = cast(dict[str, object], json.loads(line))
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


def step_memory(row: Reservation, now: datetime, processes: dict[int, ProcessIdentity],
                proc: Path = PROC) -> StepMemory:
    identity = processes.get(row["pid"])
    if identity is None or identity.start != row["start_time"]:
        return GoneMemory()
    reading = tree_memory(row["pid"], row["start_time"], processes, proc)
    if isinstance(reading, MeasuredMemory) and now.timestamp() - _epoch(row["admitted_at"]) < RAMP_SECONDS:
        if reading.bytes == 0:
            return PromisedMemory(row["need"])
        return replace(reading, ramp_promise=row["need"])
    return reading


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
            return replace(reading, unknown_peak_promise=row["need"])
        return reading
    try:
        near_peak_bytes, last_near_peak_at = map(int, path.with_suffix(".wave").read_text().split())
        if near_peak_bytes == peak and last_near_peak_at >= 0 and now.timestamp() - last_near_peak_at >= PEAK_DECAY_SECONDS:
            peak = 0
    except (OSError, ValueError):
        pass  # Old samplers and unknown times retain the peak charge.
    if isinstance(reading, MeasuredMemory):
        return replace(reading, observed_peak=peak)
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
    return HostMemory(values["MemAvailable"], values.get("MemTotal"),
                      values.get("SwapTotal", 0), values.get("SwapFree", 0))


NO_HOST_SWAP = HostMemory(0, None)


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


def pressure_with_recovery(recovery: RecoverySpacing, pressure: Pressure, now: float) -> Pressure:
    if isinstance(pressure, MemoryPressure):
        return pressure
    if isinstance(recovery, RecoveryStarts) and now < min(recovery.ends_at, recovery.next_start_at):
        return MemoryPressure(0, next_start_at=min(recovery.ends_at, recovery.next_start_at))
    return pressure


def recovery_pressure(directory: Path, pressure: Pressure, now: float) -> Pressure:
    """Read the bounded recovery window; only check writes it under its lock."""
    return pressure_with_recovery(read_recovery(directory), pressure, now)


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


def live_reservations(directory: Path) -> list[tuple[Path, Reservation]]:
    live: list[tuple[Path, Reservation]] = []
    for path in directory.glob("*.reservation"):
        try:
            row = cast(Reservation, json.loads(path.read_text()))
            if process_start_time(row["pid"]) == row["start_time"]:
                live.append((path, row))
                continue
        except (OSError, ValueError, KeyError, TypeError):
            pass
        path.unlink(missing_ok=True)
        path.with_suffix(".max").unlink(missing_ok=True)
        path.with_suffix(".wave").unlink(missing_ok=True)
    return live


def live_waiters(directory: Path) -> list[tuple[Path, Waiter]]:
    live: list[tuple[Path, Waiter]] = []
    for path in directory.glob("*.waiting"):
        try:
            row = cast(Waiter, json.loads(path.read_text()))
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
    return row.get("bump_order", 0), row["since"], row["pid"]


def head_priority(waiters: list[Waiter], available: int, total: int | None,
                  running: list[StepMemory], now: float) -> HeadPriority | NoHeadPriority:
    ordered = sorted(waiters, key=waiter_order)
    if not ordered:
        return NoHeadPriority()
    head = ordered[0]
    if (head.get("holds_younger", False) or head.get("bump_order", 0) < 0
            or now - head["since"] >= HEAD_SECONDS):
        decision = decide(available, total, head["need"], running)
        if (head.get("holds_younger", False) or decision.state == "admit"
                or available + decision.measured >= head["need"] + decision.reserve):
            return HeadPriority(head, head_window(head, now))
    return NoHeadPriority()


def queue_decision(available: int, total: int | None, row: Waiter,
                   running: list[StepMemory], head: HeadPriority | NoHeadPriority,
                   pressure: Pressure) -> AdmissionDecision:
    younger = (isinstance(head, HeadPriority) and isinstance(head.window, StrictUntil)
               and waiter_order(head.waiter) < waiter_order(row))
    head_need = head.waiter["need"] if younger and isinstance(head, HeadPriority) else 0
    decision = decide(available, total, row["need"], running, head_need, pressure)
    if younger:
        return replace(decision, state="hold")
    if isinstance(head, HeadPriority) and waiter_order(head.waiter) == waiter_order(row) and not has_running_step(running):
        return replace(decision, state="admit" if isinstance(pressure, NoPressureHold) else "hold")
    return decision


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


def check(meminfo: Path, pid: int, sidecar: str, argv: list[str], since: float = 0.0,
          root: Path | None = None, now: datetime | None = None,
          profile: StepProfile | None = None) -> CheckResult:
    """Admit or hold one step. A held step is a waiter from `since` (epoch seconds; 0 means now), so a
    step that gave its reservation back for the cargo token keeps its place."""
    root = root if root is not None else store.root()
    estimate_time = now if now is not None else datetime.now(UTC)
    width = test_threads(argv)
    package, filters = test_package(argv), test_filters(argv)
    if profile is not None and profile.estimated_at and estimate_time.timestamp() - profile.estimated_at >= HEAD_SECONDS:
        profile = None
    if profile is None:
        repository, worktree = git_identity(argv, os.getcwd())
        repo = repository.name if isinstance(repository, RepositoryIdentity) else ""
        scope = test_scope(argv)
        step = parse.step_name(argv) if argv else "build"
        host = store.host_name()
        peak = expected_peak(repository, step, host, estimate_time, root, scope=scope,
                             package=package, filters=filters)
        profile = StepProfile(repo, step, worktree, peak, repository, scope, estimate_time.timestamp())
    elif not profile.estimated_at:
        profile = replace(profile, estimated_at=estimate_time.timestamp())
    repo, step, worktree, peak = profile.repo, profile.step, profile.worktree, profile.peak
    if not argv and not (root / "admission").exists():
        host = read_meminfo(meminfo)
        available, total = host.available, host.total
        pressure = read_pressure(host)
        if isinstance(pressure, NoPressureHold):
            return CheckResult(decide(available, total, peak.bytes, sccache_memory(), pressure=pressure), peak, [], NotReserved("launch"), repo, step, available, profile)
        # A launch can be the first caller to observe a pressure hold. Record
        # it under the lock so launches after it share the recovery window.
    directory = admission_dir(root)
    with (directory / "lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Lock contention must not consume the recovery spacing or ramp window.
        # Use one time for every decision and ledger write in this transaction.
        now = now if now is not None else datetime.now(UTC)
        proc = Path(os.environ.get("BUILDLOG_PROC", "/proc"))
        processes = process_table(proc)
        live: list[tuple[Path, Reservation]] = []
        running: list[StepMemory] = []
        for path, row in live_reservations(directory):
            memory = reservation_memory(path, row, now, processes, proc)
            if isinstance(memory, GoneMemory):
                path.unlink(missing_ok=True)
                path.with_suffix(".max").unlink(missing_ok=True)
                path.with_suffix(".wave").unlink(missing_ok=True)
                continue
            live.append((path, row))
            running.append(memory)
        running.extend(sccache_memory())
        start = process_start_time(pid)
        mine = directory / f"{pid}-{start}.waiting"
        waiting = live_waiters(directory)
        previous = next((row for path, row in waiting if path == mine), None)
        waited_from = since or now.timestamp()
        if previous is not None:
            waited_from = min(waited_from, previous["since"])
        waiter: Waiter = {"pid": pid, "start_time": start or "", "need": peak.bytes, "since": waited_from,
                          "label": f"{worktree or repo} {step} ({os.environ.get('BUILDLOG_CALLER') or 'caller'} pid {pid})"}
        waiter["test_scope"] = profile.scope
        waiter["package"] = package
        waiter["filters"] = list(filters)
        waiter["estimated_at"] = profile.estimated_at
        waiter["repo_identity"] = ""
        if isinstance(profile.repository, RepositoryIdentity):
            waiter["repo_identity"] = profile.repository.key
            waiter["repository_name"] = profile.repository.name
            waiter["legacy_name"] = profile.repository.legacy_name
        if previous is not None:
            if "bump_order" in previous:
                waiter["bump_order"] = previous["bump_order"]
            if previous.get("holds_younger", False):
                waiter["holds_younger"] = True
            if "priority_since" in previous:
                waiter["priority_since"] = previous["priority_since"]
        host = read_meminfo(meminfo)
        available, total = host.available, host.total
        pressure = read_pressure(host)
        recovery = read_recovery(directory)
        if isinstance(pressure, MemoryPressure):
            recovery = PressureHoldRecorded()
            write_recovery(directory, recovery)
        elif isinstance(recovery, PressureHoldRecorded):
            recovery = RecoveryStarts(now.timestamp() + RECOVERY_WINDOW_SECONDS, now.timestamp())
            write_recovery(directory, recovery)
        elif isinstance(recovery, RecoveryStarts) and now.timestamp() >= recovery.ends_at:
            (directory / "pressure-recovery").unlink()
            recovery = NoRecoverySpacing()
        pressure = pressure_with_recovery(recovery, pressure, now.timestamp())
        candidates = sorted([row for path, row in waiting if path != mine] + ([waiter] if argv else []),
                            key=waiter_order)
        head = head_priority(candidates, available, total, running, now.timestamp())
        if isinstance(head, HeadPriority) and "priority_since" not in head.waiter:
            head.waiter["holds_younger"] = True
            head.waiter["priority_since"] = now.timestamp()
            for head_path, head_row in waiting:
                if head_path != mine and head_row is head.waiter:
                    _ = head_path.write_text(json.dumps(head_row, separators=(",", ":")))
        head_need, head_label = 0, ""
        if (isinstance(head, HeadPriority) and isinstance(head.window, StrictUntil)
                and waiter_order(head.waiter) < waiter_order(waiter)):
            head_need, head_label = head.waiter["need"], waiter_label(head.waiter)
        decision = queue_decision(available, total, waiter, running, head, pressure)
        # History lookup stays outside the lock. If contention outlives the
        # estimate, keep its timestamp so the next shell poll refreshes it.
        if now.timestamp() - profile.estimated_at >= HEAD_SECONDS:
            decision = replace(decision, state="hold")
            print("memory admission: estimate expired while waiting for the lock; refresh on the next poll", file=sys.stderr)
        blocker = reservation_blocker(waiter, candidates, [row for _, row in live], running,
                                      available, total, pressure, now.timestamp(), processes, proc)
        reservation: Reserved | NotReserved = NotReserved("held" if argv else "launch")
        if argv and decision.state == "hold" and start is not None:
            identity = waiter_identity(step, worktree)
            waiter["session_id"], waiter["session_name"] = identity["session_id"], identity["session_name"]
            waiter["step"], waiter["folder"] = identity["step"], identity["folder"]
            _ = mine.write_text(json.dumps(waiter, separators=(",", ":")))
        if decision.state == "admit" and isinstance(recovery, RecoveryStarts):
            write_recovery(directory, replace(recovery, next_start_at=now.timestamp() + RECOVERY_START_SECONDS))
        if argv and decision.state == "admit":
            mine.unlink(missing_ok=True)
            if start is not None:
                path = directory / f"{pid}-{start}-{time.time_ns()}.reservation"
                row: Reservation = {"pid": pid, "start_time": start, "need": peak.bytes,
                                    "repo": repo, "step": step, "worktree": worktree,
                                    "admitted_at": store.utc_iso(now.timestamp()), "sidecar": sidecar,
                                    "test_threads": width,
                                    "call_id": os.environ.get("BUILDLOG_CALL_ID") or None}
                row["test_scope"] = profile.scope
                row["package"] = package
                row["filters"] = list(filters)
                if isinstance(profile.repository, RepositoryIdentity):
                    row["repo_identity"] = profile.repository.key
                _ = path.write_text(json.dumps(row, separators=(",", ":")))
                reservation = Reserved(path)
            else:
                reservation = NotReserved("untracked")
        head_reason = (head_wait_reason(decision, peak.bytes, available, waiter, now.timestamp())
                       if candidates and candidates[0] is waiter else ReadyForAdmission())
        return CheckResult(decision, peak, [row for _, row in live], reservation, repo, step, available, profile,
                           head_need, head_label, head_reason, blocker)


def waiter_identity(step: str, worktree: str) -> WaiterIdentity:
    session = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    return {"session_id": session, "session_name": os.environ.get("CLAUDE_SESSION_NAME", ""),
            "step": step, "folder": os.getcwd() if worktree else ""}


class WaiterIdentity(TypedDict):
    session_id: str
    session_name: str
    step: str
    folder: str


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


def queue_rows(directory: Path, suffix: str, processes: dict[int, ProcessIdentity]) -> list[tuple[Path, dict[str, object]]]:
    rows: list[tuple[Path, dict[str, object]]] = []
    for path in directory.glob(f"*.{suffix}"):
        try:
            row = cast(dict[str, object], json.loads(path.read_text()))
            pid = row["pid"]
            identity = processes.get(pid) if isinstance(pid, int) else None
            if identity is not None and identity.start == row["start_time"]:
                rows.append((path, row))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return rows


def reservation_blocker(waiter: Waiter, waiters: list[Waiter], reservations: list[Reservation],
                        running: list[StepMemory], available: int, total: int | None,
                        pressure: Pressure, now: float, processes: dict[int, ProcessIdentity],
                        proc: Path) -> Blocker:
    """Name the unique reservation whose release would admit this waiter."""
    head = head_priority(waiters, available, total, running, now)
    if queue_decision(available, total, waiter, running, head, pressure).state == "admit":
        return NoSingleBlocker()
    blockers: list[int] = []
    for index, memory in enumerate(running):
        remaining = running[:index] + running[index + 1:]
        freed = memory.bytes - memory.swap_bytes if isinstance(memory, MeasuredMemory) else 0
        after_head = head_priority(waiters, available + freed, total, remaining, now)
        if queue_decision(available + freed, total, waiter, remaining, after_head, pressure).state == "admit":
            blockers.append(index)
    if len(blockers) != 1:
        return NoSingleBlocker()
    if blockers[0] >= len(reservations):
        return UnitBlocker("sccache.service")
    row = reservations[blockers[0]]
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
    ordered = [(path, cast(Waiter, cast(object, row))) for path, row in queue_rows(directory, "waiting", processes)]
    matching = [row for path, row in ordered if path == waiter_path]
    if not matching:
        if not waiter_path.exists():
            raise WaiterGone(f"waiter file is gone: {waiter_path}")
        return NoSingleBlocker()
    now = time.time()
    reservations: list[Reservation] = []
    running: list[StepMemory] = []
    for path, row in queue_rows(directory, "reservation", processes):
        reservation = cast(Reservation, cast(object, row))
        memory = reservation_memory(path, reservation, datetime.fromtimestamp(now, UTC), processes, proc)
        if not isinstance(memory, GoneMemory):
            reservations.append(reservation)
            running.append(memory)
    running.extend(sccache_memory())
    try:
        host = read_meminfo(Path(os.environ.get("BUILDLOG_MEMINFO", "/proc/meminfo")))
    except (OSError, ValueError):
        return NoSingleBlocker()
    available, total = host.available, host.total
    pressure = recovery_pressure(directory, read_pressure(host), now)
    return reservation_blocker(matching[0], [row for _, row in ordered], reservations, running,
                               available, total, pressure, now, processes, proc)


def current_blocker(waiter_path: Path) -> Blocker:
    """Read the waiter's blocker without changing the admission ledger."""
    try:
        lock = (waiter_path.parent / "lock").open("r")
    except FileNotFoundError:
        return blocker_snapshot(waiter_path)
    with lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        return blocker_snapshot(waiter_path)


def read_queue(directory: Path, processes: dict[int, ProcessIdentity], now: float) -> tuple[QueueView, list[tuple[Path, Waiter]]]:
    proc = Path(os.environ.get("BUILDLOG_PROC", "/proc"))
    identities = QueueIdentityReader(processes, proc)
    running: list[RunningStep] = []
    memory: list[StepMemory] = []
    for path, row in queue_rows(directory, "reservation", processes):
        reservation = cast(Reservation, cast(object, row))
        pid = reservation["pid"]
        reading = reservation_memory(path, reservation, datetime.fromtimestamp(now, UTC), processes, proc)
        if isinstance(reading, GoneMemory):
            continue
        identity = identities.identity(pid, row)
        running.append(RunningStep(identity["session_id"], identity["session_name"], identity["step"],
                                   identity["folder"], reservation["need"], reading))
        memory.append(reading)
    for compiler in sccache_memory():
        memory.append(compiler)
        running.append(RunningStep("", "sccache.service", "compile", "builds.slice", 0, compiler))
    host = HostMemory(0, None)
    unavailable = False
    try:
        host = read_meminfo(Path(os.environ.get("BUILDLOG_MEMINFO", "/proc/meminfo")))
    except (OSError, ValueError):
        unavailable = True
    available, total = host.available, host.total
    pressure = recovery_pressure(directory, read_pressure(host), now)
    ordered = sorted(((path, cast(Waiter, cast(object, row))) for path, row in queue_rows(directory, "waiting", processes)),
                     key=lambda item: waiter_order(item[1]))
    head = head_priority([row for _, row in ordered], available, total, memory, now)
    pending = sum(growth_headroom(item) for item in memory)
    waiting: list[WaitingStep] = []
    for _, row in ordered:
        identity = identities.identity(row["pid"], cast(dict[str, object], cast(object, row)))
        own = queue_decision(available, total, row, memory, head, pressure)
        reason: WaitReason = ReadyForAdmission()
        if unavailable:
            reason = ReadingUnavailable()
        elif (isinstance(head, HeadPriority) and isinstance(head.window, StrictUntil)
              and waiter_order(head.waiter) < waiter_order(row)):
            head_identity = identities.identity(head.waiter["pid"], cast(dict[str, object], cast(object, head.waiter)))
            reason = BehindHead(head_identity["session_name"] or Path(head_identity["folder"]).name or str(head.waiter["pid"]),
                                head.waiter["since"], head_identity["session_id"])
        elif row is ordered[0][1]:
            reason = head_wait_reason(own, row["need"], available, row, now)
        elif isinstance(own.pressure, MemoryPressure):
            reason = own.pressure
        elif own.state == "hold":
            reason = (NeedsMemory(row["need"], available, own.threshold, own.promised, own.reserve,
                                  own.threshold - row["need"] - own.promised - own.reserve)
                      if available < own.threshold
                      else NeedsBuildBudget(row["need"], own.charged, own.budget))
        waiting.append(WaitingStep(row["pid"], identity["session_id"], identity["session_name"], identity["step"],
                                   identity["folder"], row["since"], row["need"], reason))
    return QueueView(tuple(waiting), tuple(running), available, pending, total * 5 // 100 if total else 0, now), ordered


def _queue_lock_timeout(_signum: int, _frame: object) -> NoReturn:
    raise TimeoutError(f"admission lock busy for {QUEUE_LOCK_TIMEOUT_SECONDS:g} s; try again")


def queue_view() -> QueueView:
    directory = store.root() / "admission"
    try:
        lock = (directory / "lock").open("r")
    except FileNotFoundError:
        return read_queue(directory, process_table(Path(os.environ.get("BUILDLOG_PROC", "/proc"))), time.time())[0]
    with lock:
        previous = signal.signal(signal.SIGALRM, _queue_lock_timeout)
        try:
            _ = signal.setitimer(signal.ITIMER_REAL, QUEUE_LOCK_TIMEOUT_SECONDS)
            fcntl.flock(lock, fcntl.LOCK_SH)
        finally:
            _ = signal.setitimer(signal.ITIMER_REAL, 0)
            _ = signal.signal(signal.SIGALRM, previous)
        return read_queue(directory, process_table(Path(os.environ.get("BUILDLOG_PROC", "/proc"))), time.time())[0]


def bump_waiter(selector: str, actor: str) -> WaitingStep:
    directory = admission_dir(store.root())
    with (directory / "lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        view, ordered = read_queue(directory, process_table(Path(os.environ.get("BUILDLOG_PROC", "/proc"))), time.time())
        matches: list[int] = []
        if selector.isdecimal():
            position = int(selector) - 1
            if 0 <= position < len(view.waiting):
                matches = [position]
        else:
            matches = [position for position, item in enumerate(view.waiting)
                       if selector in (item.session_id, item.session_name, item.folder, Path(item.folder).name)]
        if len(matches) != 1:
            labels = ", ".join(f"{position + 1}: {item.session_name or item.session_id or item.pid} {item.folder}"
                               for position, item in enumerate(view.waiting) if not matches or position in matches)
            raise ValueError(f"{selector!r}: {'unknown' if not matches else 'ambiguous'} waiter; matches: {labels or 'none'}")
        position = matches[0]
        moved = view.waiting[position]
        path, row = ordered[position]
        # Each move gets an order before all current waiters. Keep the wait time.
        row["bump_order"] = min(0, *(item.get("bump_order", 0) for _, item in ordered)) - 1
        row["holds_younger"] = True
        row["priority_since"] = time.time()
        temporary = path.with_suffix(".bump")
        _ = temporary.write_text(json.dumps(row, separators=(",", ":")))
        os.replace(temporary, path)
        record.queue_bump(actor, moved.session_id or moved.session_name, moved.step, moved.folder, position + 1, view.now)
        reason = moved.reason if isinstance(moved.reason, (NeedsBuildBudget, NeedsMemory, ReadingUnavailable, MemoryPressure)) else ReadyForAdmission()
        return WaitingStep(moved.pid, moved.session_id, moved.session_name, moved.step, moved.folder,
                           row["since"], moved.need, reason)


def release(path: Path, status: int, root: Path | None = None) -> None:
    root = root if root is not None else store.root()
    directory = admission_dir(root)
    with (directory / "lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            row = cast(Reservation, json.loads(path.read_text()))
        except (OSError, ValueError, TypeError):
            return
        maximum = path.with_suffix(".max")
        try:
            peak = int(maximum.read_text())
            if peak > 0 and row["sidecar"] and Path(row["sidecar"]).is_file():
                store.append_line(directory / "anon_peaks.jsonl", {
                    "host": store.host_name(), "repo": row["repo"], "step": row["step"],
                    "worktree": row["worktree"], "anon_peak_bytes": peak, "status": status,
                    "test_threads": row.get("test_threads"), "call_id": row.get("call_id"),
                    "repo_identity": row.get("repo_identity"), "test_scope": row.get("test_scope", "full"),
                    "package": row.get("package", ""), "filters": row.get("filters", []),
                    "ended_at": store.utc_iso(time.time()),
                })
        except (OSError, ValueError):
            pass
        path.unlink(missing_ok=True)
        maximum.unlink(missing_ok=True)
        path.with_suffix(".wave").unlink(missing_ok=True)
        if row["sidecar"]:
            Path(row["sidecar"]).unlink(missing_ok=True)


def set_width(path: Path, width: int, root: Path | None = None) -> None:
    """Record the test width a reserved step runs at, claimed after its admission."""
    root = root if root is not None else store.root()
    with (admission_dir(root) / "lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            row = cast(Reservation, json.loads(path.read_text()))
        except (OSError, ValueError, TypeError):
            return
        row["test_threads"] = width
        _ = path.write_text(json.dumps(row, separators=(",", ":")))


def _epoch(instant: str) -> float:
    try:
        return datetime.fromisoformat(instant.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def clean_field(value: str) -> str:
    return value.replace("|", " ").replace("\n", " ")


def run_action() -> None:
    action = sys.argv[1]
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
    _, _, meminfo, pid, sidecar, since, cached_repo, cached_step, cached_worktree, cached_need, cached_source, cached_count, *argv = sys.argv
    profile = None
    if cached_need != "-":
        start = process_start_time(int(pid))
        try:
            row = cast(Waiter, json.loads((store.root() / "admission" / f"{pid}-{start}.waiting").read_text()))
            # An empty key is the explicit unknown identity. Old rows must refresh.
            if ("repo_identity" in row and "test_scope" in row and "estimated_at" in row
                    and (row["test_scope"] == "full"
                         or (row.get("package") == test_package(argv)
                             and row.get("filters") == list(test_filters(argv))))
                    and 0 <= time.time() - row["estimated_at"] < HEAD_SECONDS):
                key = row["repo_identity"]
                repository: Repository = (RepositoryIdentity(key, row.get("repository_name", cached_repo), row.get("legacy_name", ""))
                                          if key else UnknownRepository())
                profile = StepProfile(cached_repo, cached_step, cached_worktree,
                                      ExpectedPeak(int(cached_need), cast(Literal["measured", "buildlog", "pooled", "fallback"], cached_source), int(cached_count)),
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
    except LiveMemoryReading as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
