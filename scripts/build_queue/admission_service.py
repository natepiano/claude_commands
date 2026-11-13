"""The single admission loop owns reservations and durable queue state."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections import deque
from collections.abc import Generator, Mapping
from concurrent.futures import Future
from contextlib import contextmanager
import errno
import hashlib
import json
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import UTC, datetime
import os
from pathlib import Path
import select
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from types import MappingProxyType
from typing import Callable, Protocol, cast

from scripts.build_hold.owning_session import NoOwningSession, OwningSession
from scripts.build_queue import snapshot_codec as codec
from scripts.lib import conf, inotify
from scripts.lib.boot_identity import boot_identity
from scripts.build_queue import admission_code
from scripts.build_queue import admission_protocol as wire
from scripts.build_queue import admission_records as records
from scripts.build_queue import placement
from scripts.build_queue import step_estimator
from scripts.build_queue import progress_profile as progress
from scripts.build_queue import cloud_progress
from scripts.build_queue import queue_snapshot
from scripts.build_queue.admission_records import AdmissionWaitSnapshot, WaitHolder, reason_phrase
from scripts.build_queue.system_clock import SystemClock as SystemClock
from scripts.lint import memory_admit as gate

from scripts.buildlog import store
from scripts.lib import test_phase

from scripts.build_queue.scope_activity import (
    IdleService, LiveBuildRun, NoLiveBuildRun, NotServiceSlice, ScopeActivityReader, ScopeProcesses,
)
from scripts.lint.build_shape import build_shape, recorded_build_shape
from scripts.lib.atomic_file import atomic_sample, write_text_atomic
from scripts.accounts import outage_alerts


CARGO_CAPTURE_ROOT = Path("/tmp/cargo-tile")
READING_ALERT_KEY = "build-admission-reading"


@dataclass(frozen=True)
class NoReadingHold:
    """No unreadable reading currently holds a queued start."""


@dataclass(frozen=True)
class UnreadableReadingHold:
    since: float
    path: Path
    reason: str


@dataclass(frozen=True)
class NoReadingAlertDelivery:
    """No reading alert sender has started."""


@dataclass(frozen=True)
class ReadingAlertDelivery:
    thread: threading.Thread


def deliver_reading_alert(claimed: outage_alerts.AlertClaim, hold: UnreadableReadingHold, elapsed: float) -> None:
    """Send the claimed alert outside the admission pass."""
    from scripts.message import send_budget, send_result
    from scripts.message.session_send import session_send_argv

    delivered = False
    try:
        settings = conf.read("lint")
        command = session_send_argv("natedev", sender="build-admission", lint_settings=settings)
        message = (f"Build admission: {hold.path} is unreadable.\nReason: {hold.reason}\n"
                   f"This reading has held build starts for {elapsed:g} seconds.\n"
                   "Restore the reading on natedev. Admission holds starts until the reading works.")
        result = subprocess.run(command, input=message, text=True, capture_output=True,
                                timeout=send_budget.seconds(settings.number("sweep_sender_timeout_seconds")))
        if result.returncode in (send_result.EXIT["sent"], send_result.EXIT["queued"]):
            outage_alerts.delivered(claimed)
            delivered = True
        else:
            print(f"memory admission: reading alert failed: {result.stderr.strip()}", file=sys.stderr)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"memory admission: reading alert failed: {error}", file=sys.stderr)
    finally:
        if not delivered:
            outage_alerts.release(claimed)


@contextmanager
def retry_cleanup(path: Path) -> Generator[None]:
    """Keep failed cleanup records for the next pass."""
    try:
        yield
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"memory admission: cannot finish cleanup {path}: {error}; will retry", file=sys.stderr)


def _live_run(run: wire.RunIdentity) -> LiveBuildRun:
    return LiveBuildRun(run.pid, run.start_time, 0.0)


class Clock(Protocol):
    def now(self) -> float: ...
    def monotonic(self) -> float: ...


class Readings(Protocol):
    def host(self) -> records.HostMemory | records.ReadingUnavailable: ...
    def pressure(self, host: records.HostMemory, policy: records.AdmissionPolicy) -> records.Pressure: ...
    def compilers(self) -> list[records.StepMemory]: ...
    def reservation(self, path: Path, row: records.Reservation, now: datetime,
                    processes: dict[int, gate.ProcessIdentity], proc: Path, policy: records.AdmissionPolicy) -> records.StepMemory: ...


class SystemReadings:
    def host(self) -> records.HostMemory | records.ReadingUnavailable:
        try:
            return gate.read_meminfo(Path(os.environ.get("BUILDLOG_MEMINFO", "/proc/meminfo")))
        except (OSError, ValueError) as error:
            return records.ReadingUnavailable(str(error) or "memory reading unavailable")

    def pressure(self, host: records.HostMemory, policy: records.AdmissionPolicy) -> records.Pressure:
        return gate.read_pressure(host, policy=policy)

    def compilers(self) -> list[records.StepMemory]:
        return gate.sccache_memory()

    def reservation(self, path: Path, row: records.Reservation, now: datetime,
                    processes: dict[int, gate.ProcessIdentity], proc: Path, policy: records.AdmissionPolicy) -> records.StepMemory:
        return gate.reservation_memory(path, row, now, processes, proc, policy)


@dataclass(frozen=True)
class LiveOwnerIdentity:
    start_time: str


@dataclass(frozen=True)
class OwnerProcessGone:
    pass


class Proc(Protocol):
    @property
    def path(self) -> Path: ...
    def processes(self) -> dict[int, gate.ProcessIdentity]: ...
    def owner_start(self, pid: int) -> LiveOwnerIdentity | OwnerProcessGone: ...
    def pidfd(self, pid: int) -> int: ...
    def lifetime(self, row: records.Reservation, owner_live: bool
                 ) -> gate.StepLifetime: ...


class SystemProc:
    @property
    def path(self) -> Path:
        return Path(os.environ.get("BUILDLOG_PROC", "/proc"))

    def processes(self) -> dict[int, gate.ProcessIdentity]:
        return gate.process_table(self.path)

    def owner_start(self, pid: int) -> LiveOwnerIdentity | OwnerProcessGone:
        start = records.process_start_time(pid)
        return LiveOwnerIdentity(start) if start is not None else OwnerProcessGone()

    def pidfd(self, pid: int) -> int:
        return os.pidfd_open(pid)

    def lifetime(self, row: records.Reservation, owner_live: bool
                 ) -> gate.StepLifetime:
        return gate.reservation_lifetime(row, owner_live)


@dataclass(frozen=True)
class OrderNotStarted:
    pass


@dataclass(frozen=True)
class TokenNotLeft:
    pass


def matches_build_estimate(repo: str, repo_identity: str, shape: str,
                           profile: records.StepProfile, estimate_shape: str) -> bool:
    return (step_estimator.history_matches_repository({"repo": repo, "repo_identity": repo_identity}, profile.repository)
            and (not shape or not estimate_shape
                 or recorded_build_shape(shape) == recorded_build_shape(estimate_shape)))


@dataclass(frozen=True)
class WaiterPlace:
    owner: wire.Owner
    since: float
    episode: str
    priority_since: float | OrderNotStarted = OrderNotStarted()
    token_left_at: float | TokenNotLeft = TokenNotLeft()
    order_since: float | OrderNotStarted = field(default_factory=OrderNotStarted, kw_only=True)
    queue_identity: records.QueueLifeIdentity = field(kw_only=True)


def waiter_place(row: records.Waiter, episode: str) -> WaiterPlace:
    """Keep a logical identity with every active place."""
    return WaiterPlace(wire.Owner(row["pid"], row["start_time"]), row["since"], episode,
        row.get("priority_since", OrderNotStarted()), row.get("token_left_at", TokenNotLeft()),
        order_since=row.get("order_since", row["since"]), queue_identity=gate.queue_life_identity(row))


@dataclass(frozen=True)
class NotEnding:
    pass


@dataclass(frozen=True)
class ReleaseStatus:
    request_id: str
    status: int


@dataclass(frozen=True)
class OwnerExited:
    pass


@dataclass(frozen=True)
class NoFinalizationIntent:
    pass


@dataclass(frozen=True)
class TokenWaiting:
    request_id: str
    left_at: float


@dataclass(frozen=True)
class ReservationRecord:
    """Service-owned reservation: width and call absence have domain names."""
    episode: str
    path: str
    owner: wire.Owner
    need: int
    repo: str
    step: str
    worktree: str
    admitted_at: str
    sidecar: str
    test_width: records.TestWidth
    call: records.Call
    reservation_id: str
    queue_waiter: records.Waiter
    repo_identity: str = ""
    test_scope: records.TestScope = "full"
    package: str = ""
    filters: tuple[str, ...] = ()
    shape: str = ""
    test_phase_need: records.TestPhaseNeed | gate.NoTestPhaseNeed = gate.NoTestPhaseNeed("not_recorded")
    ending: NotEnding | ReleaseStatus | OwnerExited | TokenWaiting = NotEnding()
    finalization: records.FinalizationIntent | NoFinalizationIntent = NoFinalizationIntent()
    charge_use: test_phase.TestPhaseChargeUse | test_phase.NoTestPhaseChargeUse = test_phase.NoTestPhaseChargeUse()
    run: wire.RunIdentity | wire.NoRun = wire.NO_RUN
    place: wire.LocalPlace | wire.CloudPlace = wire.LocalPlace()


def reservation_row(record: ReservationRecord) -> records.Reservation:
    """Project service state into the shared reservation record."""
    row: records.Reservation = {
        "pid": record.owner.pid, "start_time": record.owner.start_time, "need": record.need,
        "repo": record.repo, "step": record.step, "worktree": record.worktree,
        "admitted_at": record.admitted_at, "sidecar": record.sidecar,
        "test_threads": record.test_width, "call_id": record.call,
        "reservation_id": record.reservation_id, "queue_waiter": record.queue_waiter,
        "scoped": bool(record.sidecar) and isinstance(record.place, wire.LocalPlace), "test_scope": record.test_scope,
        "package": record.package, "filters": list(record.filters),
    }
    if isinstance(record.run, wire.RunIdentity):
        row["build_run"] = _live_run(record.run)
        row["run_origin"] = records.CallerRun(row["build_run"])
    if record.repo_identity:
        row["repo_identity"] = record.repo_identity
    if record.shape:
        row["shape"] = record.shape
    if not isinstance(record.test_phase_need, gate.NoTestPhaseNeed) or record.test_phase_need.reason != "not_recorded":
        row["test_phase_need"] = gate.test_phase_need_record(record.test_phase_need)
    if not isinstance(record.finalization, NoFinalizationIntent):
        row["finalization"] = record.finalization
    if not isinstance(record.charge_use, test_phase.NoTestPhaseChargeUse):
        row["test_phase_charge_use"] = record.charge_use
    return row


@dataclass(frozen=True)
class LiveGrant:
    pass


@dataclass(frozen=True)
class ReplayEntry:
    reply: wire.Reply
    ended_at: float | LiveGrant
    reservation: str = ""


@dataclass(frozen=True)
class PendingAdmission:
    episode: str
    need_source: records.NeedSource
    is_head: bool
    wait_seconds: float


@dataclass(frozen=True)
class PendingLeft:
    identity: records.QueueLifeIdentity
    episode: str
    reason: gate.LeftReason
    ended_at: float


@dataclass(frozen=True)
class ServiceState:
    reservations: dict[str, ReservationRecord]
    places: dict[str, WaiterPlace]
    ledger: dict[str, ReplayEntry]
    spacing: records.StartSpacing
    override: records.HeadAdmissionOverride | records.NoHeadAdmissionOverride
    priority: records.QueuePriority | records.NoQueuePriority = records.NO_QUEUE_PRIORITY
    build_history_generation: int = 0

    run_places: dict[str, wire.RunPlace] = field(default_factory=dict)
    caller_versions: dict[str, str] = field(default_factory=dict)
    lifecycle_outbox: tuple[PendingAdmission | PendingLeft, ...] = ()
    placement_requests: dict[str, wire.PlacementRequest] = field(default_factory=dict)
    assignments: dict[str, records.SavedPlacementAssignment] = field(default_factory=dict)
    current_assignments: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SystemReadingPlan:
    """The reader owns these records until it publishes the next reading."""
    config: records.MemoryConfig
    reservations: tuple[ReservationRecord, ...]
    runs: tuple[wire.RunPlace, ...]
    waiting: bool


@dataclass(frozen=True)
class MissingSample:
    """The sampler has not written this file."""


@dataclass(frozen=True)
class UnreadableSample:
    """The sampler file could not be read."""


@dataclass(frozen=True)
class SampleFile:
    text: str | MissingSample | UnreadableSample
    modified_at: float | records.PeakTimeUnknown


def sample_file(path: Path) -> SampleFile:
    try:
        text: str | MissingSample | UnreadableSample = path.read_text()
    except FileNotFoundError:
        text = MissingSample()
    except (OSError, UnicodeError):
        text = UnreadableSample()
    try:
        modified: float | records.PeakTimeUnknown = path.stat().st_mtime
    except OSError:
        modified = records.PeakTimeUnknown()
    return SampleFile(text, modified)


@dataclass(frozen=True)
class ReservationEvidence:
    """Physical memory and sampler observations without a policy charge."""
    memory: records.StepMemory
    peak: SampleFile
    wave: SampleFile
    phase: test_phase.ObservedTestPhase | test_phase.UnobservedTestPhase
    scope: str


def raw_reservation_memory(record: ReservationRecord, row: records.Reservation,
                           lifetime: gate.StepLifetime, processes: dict[int, gate.ProcessIdentity],
                           proc: Path) -> records.StepMemory:
    _ = record
    if isinstance(lifetime, gate.EndedStep):
        return records.GoneMemory()
    if isinstance(lifetime, gate.PopulatedStepScope):
        try:
            return gate.process_memory(lifetime, proc)
        except FileNotFoundError:
            identity = processes.get(row["pid"])
            if identity is None or identity.start != row["start_time"]:
                return records.GoneMemory()
    return gate.tree_memory(row["pid"], row["start_time"], processes, proc)


def reservation_charge(evidence: ReservationEvidence, row: records.Reservation,
                       now: float, policy: records.AdmissionPolicy) -> records.StepMemory:
    """Apply the current reservation and policy to unchanged physical evidence."""
    memory = gate.step_promise(row, evidence.memory)
    phase = gate.test_phase_history(row, memory, evidence.phase)
    if isinstance(memory, records.MeasuredMemory) and isinstance(phase, records.TestPhaseStarted):
        return replace(memory, peak_history=phase)
    try:
        if isinstance(evidence.peak.text, UnreadableSample):
            raise ValueError("unreadable peak")
        peak = 0 if isinstance(evidence.peak.text, MissingSample) else int(evidence.peak.text)
        if peak < 0:
            raise ValueError("negative peak")
    except ValueError:
        return (replace(memory, unknown_peak_promise=row["need"], peak_history=records.PeakFileUnreadable(row["need"]))
                if isinstance(memory, records.MeasuredMemory) else memory)
    last_near_peak = now
    peak_at = evidence.peak.modified_at if peak > 0 else records.PeakTimeUnknown()
    peak_time: float | records.PeakTimeUnknown = records.PeakTimeUnknown()
    old_sampler = False
    if isinstance(evidence.wave.text, MissingSample) and peak > 0:
        old_sampler = True
        if isinstance(peak_at, float):
            last_near_peak = peak_at
            peak_time = peak_at
    elif isinstance(evidence.wave.text, str):
        try:
            sampled, recorded = map(int, evidence.wave.text.split())
            if sampled == peak and recorded >= 0:
                last_near_peak = recorded
                peak_time = recorded
        except ValueError:
            pass
    history: records.PeakHistory = (records.PeakSoFar(peak, peak_at, peak_time, old_sampler)
                                if peak > 0 else records.NoPeakRecorded())
    hold = policy.peak_hold.seconds_for(row.get("step", ""))
    if not gate.release_build(row) and now - last_near_peak >= hold:
        history = records.PeakDecayed(0, peak, last_near_peak, last_near_peak + hold, old_sampler)
        peak = 0
    if isinstance(memory, records.MeasuredMemory):
        return replace(memory, observed_peak=peak, peak_history=history)
    if isinstance(memory, records.PromisedMemory):
        return records.PromisedMemory(max(memory.bytes, peak))
    return memory


@dataclass(frozen=True)
class SystemEvidence:
    read_at: float
    started_monotonic: float
    processes: Mapping[int, gate.ProcessIdentity]
    accounts: Mapping[int, int | OwnerProcessGone]
    lifetimes: Mapping[str, gate.StepLifetime]
    reservations: Mapping[str, ReservationEvidence]
    runs: Mapping[int, LiveBuildRun | NoLiveBuildRun]
    unowned: tuple[records.UnownedSliceMemory, ...]
    scope_members: Mapping[str, frozenset[int]]
    scope_process_memory: Mapping[int, records.MeasuredMemory]
    scope_workers: Mapping[str, frozenset[int]]
    between: tuple[records.RunningStep, ...]
    host: records.HostMemory | records.ReadingUnavailable
    pressure: records.Pressure
    compilers: tuple[records.StepMemory, ...]


def read_system(plan: SystemReadingPlan, readings: Readings, proc: Proc, clock: Clock, *,
                reservation_reader: Callable[[ReservationRecord, records.Reservation, gate.StepLifetime,
                    dict[int, gate.ProcessIdentity], Path], records.StepMemory] = raw_reservation_memory) -> SystemEvidence:
    """Read operating-system evidence without access to mutable service state."""
    started_monotonic = clock.monotonic()
    now = clock.now()
    processes = proc.processes()
    activity = ScopeActivityReader(processes, proc.path,
        tuple(LiveBuildRun(place.run.pid, place.run.start_time, place.since) for place in plan.runs))
    lifetimes: dict[str, gate.StepLifetime] = {}
    reservations: dict[str, ReservationEvidence] = {}
    for record in plan.reservations:
        row = reservation_row(record)
        live = proc.owner_start(record.owner.pid) == LiveOwnerIdentity(record.owner.start_time)
        lifetime = proc.lifetime(row, live)
        lifetimes[record.episode] = lifetime
        memory = reservation_reader(record, row, lifetime, processes, proc.path)
        if isinstance(memory, records.MeasuredMemory):
            memory = replace(memory, promise=records.NoPromise(), observed_peak=0,
                             unknown_peak_promise=0, peak_history=records.NoPeakRecorded())
        scope = ""
        if record.sidecar:
            try:
                scope = Path(record.sidecar).read_text().strip()
            except OSError:
                pass
        reservations[record.episode] = ReservationEvidence(memory,
            sample_file(Path(record.path).with_suffix(".max")),
            sample_file(Path(record.path).with_suffix(".wave")), gate.observed_test_phase(row), scope)
    # Capture every scope before the loop applies current reservation ownership.
    unowned = gate.unowned_slice_memory([], processes, proc.path,
        activity_reader=activity, ci_memory_slice=plan.config.ci_memory_slice)
    members: dict[str, frozenset[int]] = {}
    workers: dict[str, frozenset[int]] = {}
    for reading in unowned:
        if not reading.cgroup:
            continue
        try:
            members[reading.cgroup] = frozenset(gate.scope_members(Path(reading.cgroup)))
        except (OSError, ValueError):
            members[reading.cgroup] = frozenset()
        if not isinstance(reading.job_state, NotServiceSlice):
            programs = gate.member_programs(members[reading.cgroup].intersection(processes), proc.path)
            workers[reading.cgroup] = frozenset(pid for pid, name in programs.items() if name == "Runner.Worker")
    scope_process_memory = {pid: gate.process_memory({pid}, proc.path)
                            for pid in {member for group in members.values() for member in group}}
    between: list[records.RunningStep] = []
    represented = {(row.activity.run.pid, row.activity.run.start_time) for row in unowned
                   if isinstance(row.activity, ScopeProcesses) and isinstance(row.activity.run, LiveBuildRun)}
    represented.update((record.run.pid, record.run.start_time) for record in plan.reservations
                       if isinstance(record.run, wire.RunIdentity))
    for place in plan.runs:
        run = place.run
        if (run.pid, run.start_time) in represented:
            continue
        identity = processes.get(run.pid)
        if identity is None or identity.start != run.start_time:
            continue
        evidence = activity.scope({run.pid})
        if not isinstance(evidence, ScopeProcesses):
            continue
        live_run = LiveBuildRun(run.pid, run.start_time, activity.started_at(run.pid) or place.since)
        measured = gate.tree_memory(run.pid, run.start_time, processes, proc.path)
        if isinstance(measured, records.GoneMemory):
            continue
        memory = records.UnownedSliceMemory(measured.bytes, swap_bytes=measured.swap_bytes,
                                         pid=run.pid, activity=replace(evidence, run=live_run))
        between.append(records.RunningStep(evidence.session_id, evidence.session_name, "between steps",
            evidence.folder, 0, memory, live_run, live_run.started_at, pid=run.pid, unit=place.unit))
    host = readings.host()
    # Retain all pressure values so a pending policy can use the same reading.
    pressure_policy = replace(plan.config.policy,
        pressure=replace(plan.config.policy.pressure, full_avg10_limit=-1))
    pressure = readings.pressure(host if isinstance(host, records.HostMemory) else
                                 records.HostMemory(0, records.UnknownHostTotal()), pressure_policy)
    return SystemEvidence(now, started_monotonic, MappingProxyType(processes),
        MappingProxyType({pid: process_uid(pid, proc.path) for pid in processes}),
        MappingProxyType(lifetimes), MappingProxyType(reservations),
        MappingProxyType({pid: activity.run(pid) for pid in processes}),
        tuple(unowned), MappingProxyType(members), MappingProxyType(scope_process_memory), MappingProxyType(workers),
        tuple(between), host, pressure, tuple(readings.compilers()))


@dataclass(frozen=True)
class NoReadingFailure:
    """The reader has no exception for the loop."""


class SystemReader(Protocol):
    @property
    def latest(self) -> SystemEvidence: ...
    def update(self, plan: SystemReadingPlan) -> None: ...
    def request_read(self) -> None: ...
    def raise_failure(self) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class NoSystemReader:
    """Direct callers collect evidence before a pass; the serving loop uses a reader."""


class OperatingSystemReader:
    """One thread publishes complete readings at the existing pass cadence."""

    def __init__(self, plan: SystemReadingPlan, initial: SystemEvidence,
                 readings: Readings, proc: Proc, clock: Clock, wake: int) -> None:
        self.plan: SystemReadingPlan = plan
        self.reading: SystemEvidence = initial
        self.readings: Readings = readings
        self.proc: Proc = proc
        self.clock: Clock = clock
        self.wake: int = wake
        self.lock: threading.Lock = threading.Lock()
        self.stopped: threading.Event = threading.Event()
        self.requested: threading.Event = threading.Event()
        self.failure: Exception | NoReadingFailure = NoReadingFailure()
        self.thread: threading.Thread = threading.Thread(target=self._read, name="admission-readings", daemon=True)
        self.thread.start()

    @property
    def latest(self) -> SystemEvidence:
        with self.lock:
            return self.reading

    def update(self, plan: SystemReadingPlan) -> None:
        with self.lock:
            faster = plan.waiting and not self.plan.waiting
            self.plan = plan
            if faster:
                self.requested.set()

    def request_read(self) -> None:
        self.requested.set()

    def raise_failure(self) -> None:
        with self.lock:
            failure, self.failure = self.failure, NoReadingFailure()
        if isinstance(failure, Exception):
            raise failure

    def _read(self) -> None:
        while not self.stopped.is_set():
            with self.lock:
                plan = self.plan
            interval = plan.config.waiting_pass_seconds if plan.waiting else plan.config.running_pass_seconds
            _ = self.requested.wait(interval)
            self.requested.clear()
            if self.stopped.is_set():
                return
            with self.lock:
                plan = self.plan
            try:
                reading = read_system(plan, self.readings, self.proc, self.clock)
            except Exception as error:
                with self.lock:
                    self.failure = error
            else:
                with self.lock:
                    self.reading = reading
            os.eventfd_write(self.wake, 1)

    def close(self) -> None:
        self.stopped.set()
        self.requested.set()
        self.thread.join()


class HostEvidenceReader:
    """A blocked provider cannot hold the service loop or another provider reader."""

    def __init__(self, key: placement.HostEvidenceKey,
                 probe: Callable[[placement.HostEvidenceKey], placement.HostEvidence],
                 clock: Clock, wake: int, interval: float, timeout: float) -> None:
        self.key: placement.HostEvidenceKey = key
        self.probe: Callable[[placement.HostEvidenceKey], placement.HostEvidence] = probe
        self.clock: Clock = clock
        self.wake: int = wake
        self.interval: float = interval
        self.timeout: float = timeout
        self.latest: placement.HostEvidence = placement.HostEvidenceUnavailable(key, clock.now(), "not read")
        self.stopped: threading.Event = threading.Event()
        self.thread: threading.Thread = threading.Thread(target=self._read, name="placement-evidence", daemon=True)
        self.thread.start()

    def _read(self) -> None:
        while not self.stopped.is_set():
            try:
                evidence = self.probe(self.key)
                if evidence.key != self.key:
                    raise ValueError("provider evidence has a different policy key")
            except Exception as error:
                evidence = placement.HostEvidenceUnavailable(self.key, self.clock.now(), str(error))
            self.latest = evidence
            if self.stopped.is_set():
                return
            try:
                os.eventfd_write(self.wake, 1)
            except OSError:
                return
            if self.stopped.wait(self.interval):
                return

    def close(self) -> None:
        self.stopped.set()


@dataclass(frozen=True)
class DurableRecordEncoding:
    value: object
    encoded: str
    key: str


def durable_copy(value: object) -> object:
    """Retain a comparison value even when a nested record can change."""
    if is_dataclass(value) and not isinstance(value, type):
        return replace(value, **{member.name: durable_copy(cast(object, getattr(value, member.name)))
                                  for member in fields(value) if member.init})
    if isinstance(value, Mapping):
        return {key: durable_copy(item) for key, item in cast(Mapping[str, object], value).items()}
    if isinstance(value, tuple):
        return tuple(durable_copy(item) for item in cast(tuple[object, ...], value))
    if isinstance(value, list):
        return [durable_copy(item) for item in cast(list[object], value)]
    return value


@dataclass
class PublishedReplies:
    view: wire.ViewReply
    explain: wire.ExplainReply
    view_bytes: bytes = b""
    explain_bytes: bytes = b""
    mine_bytes: bytes = b""


@dataclass(frozen=True)
class NoConnection:
    pass


NO_CONNECTION = NoConnection()


@dataclass(frozen=True)
class NoRequest:
    pass


@dataclass(frozen=True)
class NoWaitReply:
    pass


@dataclass(frozen=True)
class SourcesUnchanged:
    pass


@dataclass(frozen=True)
class SourceMissing:
    pass


@dataclass(frozen=True)
class OwnerWatch:
    owner: wire.Owner


@dataclass(frozen=True)
class ScopeWatch:
    path: Path
    episode: str


@dataclass(frozen=True)
class NoReconnectGrace:
    pass


@dataclass(frozen=True)
class ReconnectAwaitingListener:
    """A restored override has not yet had any opportunity to reconnect."""


@dataclass(frozen=True)
class ReconnectDeadline:
    at: float


@dataclass
class Connection:
    socket: socket.socket
    received: bytes = b""
    sending: bytes = b""
    request: wire.Request | NoRequest = NoRequest()
    finished: bool = False
    version: int = 1


@dataclass(frozen=True)
class DeferredOwnerRequest:
    request: wire.AdmitRequest | wire.LaunchRequest | wire.ServiceEstimateRequest
    connection: Connection
    arrived_monotonic: float


QueueChange = wire.PrioritySetRequest | wire.PriorityClearRequest | wire.AdmitHeadRequest


@dataclass
class OrderedQueueChange:
    request: QueueChange
    connections: list[Connection]


@dataclass(frozen=True)
class NoPendingPriorityRoster:
    """No priority change waits for a roster read."""


@dataclass(frozen=True)
class PendingPriorityRoster:
    change: OrderedQueueChange
    result: Future[Mapping[str, gate.UnitMembership]]


def read_priority_roster(result: Future[Mapping[str, gate.UnitMembership]], wake: int) -> None:
    """Read the roster without changing service state."""
    try:
        try:
            result.set_result(gate.priority_labels(gate.RosterUnits()))
        except Exception as error:
            result.set_exception(error)
        os.eventfd_write(wake, 1)
    finally:
        os.close(wake)


@dataclass
class WaitingAdmission:
    request: wire.AdmitRequest | wire.LaunchRequest
    row: records.Waiter
    connection: Connection | NoConnection
    last_reply: wire.Wait | NoWaitReply = NoWaitReply()
    last_sent: float = float("-inf")


@dataclass(frozen=True)
class NoPass:
    pass


def _no_fault(_stage: str) -> None:
    pass


def _place_key(owner: wire.Owner, since: float) -> str:
    return f"{records.run_owner(owner.pid, owner.start_time)}:{since:.17g}"


def _notify(message: str) -> None:
    address = os.environ.get("NOTIFY_SOCKET", "")
    if address:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
            notifier.connect("\0" + address[1:] if address.startswith("@") else address)
            _ = notifier.send(message.encode())


def finalization_outcome(ending: ReleaseStatus | OwnerExited | NotEnding | TokenWaiting
                         ) -> tuple[step_estimator.FinalOutcome, int | step_estimator.OutcomeNotRecorded, gate.StepEnd | gate.TokenReturn]:
    if isinstance(ending, TokenWaiting):
        return step_estimator.Completed(), 0, gate.TokenReturn()
    if isinstance(ending, ReleaseStatus):
        return (step_estimator.Completed() if ending.status == 0 else
                step_estimator.Killed() if ending.status >= 128 else step_estimator.Failed()), ending.status, gate.StepEnd()
    return step_estimator.OwnerGone(), gate.OUTCOME_NOT_RECORDED, gate.StepEnd()


class StateIntents:
    """Persist finalization before history; remove the entry only after history."""
    def __init__(self, reservations: dict[str, ReservationRecord],
                 advance_generation: Callable[[], None], episode: str,
                 persist: Callable[[], None], fault: Callable[[str], None],
                 places: dict[str, WaiterPlace]) -> None:
        self.reservations: dict[str, ReservationRecord] = reservations
        self.places: dict[str, WaiterPlace] = places
        self.advance_generation: Callable[[], None] = advance_generation
        self.episode: str = episode
        self.persist: Callable[[], None] = persist
        self.fault: Callable[[str], None] = fault

    def save(self, row: records.Reservation, intent: records.FinalizationIntent) -> None:
        record = self.reservations[self.episode]
        if record.reservation_id != row.get("reservation_id"):
            raise ValueError("finalization reservation changed")
        self.reservations[self.episode] = replace(record, finalization=intent)
        self.persist()
        self.fault("after_finalization_intent")

    def published(self, row: records.Reservation) -> None:
        if row["step"] == "build":
            self.advance_generation()

    def clear(self, row: records.Reservation) -> None:
        if self.reservations[self.episode].reservation_id != row.get("reservation_id"):
            raise ValueError("finalization reservation changed")
        self.fault("after_history_append")
        record = self.reservations[self.episode]
        if not isinstance(record.ending, TokenWaiting):
            _ = self.places.pop(_place_key(record.owner, record.queue_waiter["since"]), None)
        del self.reservations[self.episode]
        self.persist()


@dataclass(frozen=True)
class ServiceEstimatedAdmission(wire.AdmitRequest):
    """Retain service estimation for this connection without changing saved records."""


def peer_uid(connection: Connection | NoConnection) -> int:
    """Read the account before any replay or state lookup."""
    if isinstance(connection, NoConnection):
        return os.getuid()
    credentials = connection.socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    return struct.unpack("3i", credentials)[1]


def process_uid(pid: int, proc: Path) -> int | OwnerProcessGone:
    try:
        for line in (proc / str(pid) / "status").read_text().splitlines():
            if line.startswith("Uid:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return OwnerProcessGone()


class AdmissionServer:
    def __init__(self, state_dir: Path, clock: Clock, readings: Readings, proc: Proc, *,
                 fault: Callable[[str], None] = _no_fault, socket_path: Path | None = None,
                 capture_root: Path = CARGO_CAPTURE_ROOT,
                 host_probe: Callable[[placement.HostEvidenceKey], placement.HostEvidence] | None = None) -> None:
        self.state_dir: Path = state_dir
        self.clock: Clock = clock
        self.readings: Readings = readings
        self.proc: Proc = proc
        self.fault: Callable[[str], None] = fault
        self.socket_path: Path = socket_path if socket_path is not None else wire.admission_socket_path()
        self.waiters: dict[str, WaitingAdmission] = {}
        self.reservations: dict[str, ReservationRecord] = {}
        self.lifecycle_outbox: tuple[PendingAdmission | PendingLeft, ...] = ()
        self.places: dict[str, WaiterPlace] = {}
        self.run_places: dict[str, wire.RunPlace] = {}
        self.build_history_generation: int = 0
        self.ledger: dict[str, ReplayEntry] = {}
        self.placement_requests: dict[str, wire.PlacementRequest] = {}
        self.assignments: dict[str, records.SavedPlacementAssignment] = {}
        self.current_assignments: dict[str, str] = {}
        self.execution_evidence: dict[str, placement.ExecutionEvidence] = {}
        self.host_probe: Callable[[placement.HostEvidenceKey], placement.HostEvidence] = (
            host_probe if host_probe is not None else self._read_host_evidence)
        self.host_readers: dict[placement.HostEvidenceKey, HostEvidenceReader] = {}
        self.host_observations: dict[placement.HostEvidenceKey, placement.HostEvidence] = {}
        self.host_requests: dict[placement.HostEvidenceKey, wire.PlacementRequest] = {}
        self.spacing: records.StartSpacing = records.ReadyToStart()
        self.override: records.HeadAdmissionOverride | records.NoHeadAdmissionOverride = records.NoHeadAdmissionOverride()
        self.last_inputs: records.QueueInputs | NoPass = NoPass()
        self.last_result: gate.PassResult | NoPass = NoPass()
        self.last_policy: records.AdmissionPolicy | NoPass = NoPass()
        self.reading_hold: UnreadableReadingHold | NoReadingHold = NoReadingHold()
        self.reading_alert_delivery: ReadingAlertDelivery | NoReadingAlertDelivery = NoReadingAlertDelivery()
        self.reading_alert_needs_clear: bool = True
        self.outage_alert_needs_clear: bool = True
        self.listener_started_at: float = float("inf")
        self.configuration: gate.UsableAdmissionConfiguration | gate.UnusableAdmissionConfiguration = gate.UnusableAdmissionConfiguration("configuration not read")
        self.last_usable_configuration: records.MemoryConfig | NoPass = NoPass()
        self.caller_versions: dict[str, str] = {}
        self.slots: dict[str, records.RunSlot] = {}
        self.progress_history: progress.ProfileHistory = progress.ProfileHistory(state_dir / "progress_profiles")
        self.progress_captures: progress.CaptureReader = progress.CaptureReader(capture_root)
        try:
            self.boot_id: str = boot_identity(proc.path)
        except (OSError, ValueError):
            self.boot_id = ""
        self.host: str = store.host_name()
        self.pass_stale: bool = False
        self.pending_configuration: gate.UsableAdmissionConfiguration | gate.UnusableAdmissionConfiguration | NoPass = NoPass()
        self.priority: records.QueuePriority | records.NoQueuePriority = records.NO_QUEUE_PRIORITY
        self.pending_priority: PendingPriorityRoster | NoPendingPriorityRoster = NoPendingPriorityRoster()
        self.queue_changes: deque[OrderedQueueChange] = deque()
        self.last_diagnostics: tuple[records.ConfigurationDiagnostic, ...] = ()
        self._pending_replies: dict[str, list[wire.Reply]] = {}
        self._persisted: str = ""
        self.durable_encodings: dict[tuple[str, str], DurableRecordEncoding] = {}
        self.published: PublishedReplies | NoPass = NoPass()
        self.system_reader: SystemReader | NoSystemReader = NoSystemReader()
        self.system_evidence: SystemEvidence | NoPass = NoPass()
        self.reader_requested: bool = False
        self.deferred_requests: list[DeferredOwnerRequest] = []
        self.override_reconnect_until: ReconnectDeadline | ReconnectAwaitingListener | NoReconnectGrace = NoReconnectGrace()
        self.receiving_requests: bool = False
        self.connections: dict[int, Connection] = {}
        self.watchers: dict[int, OwnerWatch | ScopeWatch] = {}
        self.epoll: select.epoll = select.epoll()
        self.priority_events: int = os.eventfd(0, os.EFD_NONBLOCK | os.EFD_CLOEXEC)
        self.epoll.register(self.priority_events, select.EPOLLIN)
        self.reading_events: int = os.eventfd(0, os.EFD_NONBLOCK | os.EFD_CLOEXEC)
        self.epoll.register(self.reading_events, select.EPOLLIN)
        self.listener: socket.socket | NoConnection = NoConnection()
        self.stopped: bool = False
        self.closed: bool = False
        self.next_pass: float = float("inf")
        self.next_source_check: float = float("inf")
        self.sources: dict[Path, tuple[int, int] | SourceMissing] = self._source_stamps()
        self.source_change_at: float | SourcesUnchanged = SourcesUnchanged()
        state_dir.mkdir(parents=True, exist_ok=True)

        self.config_path: Path = conf.path("build_admission")
        self.config_events: int = inotify.open_watch()
        self.config_watch: int = -1
        self.config_parent: Path = self.config_path.parent
        self.config_parent_identity: tuple[int, int] = (-1, -1)
        self.config_event_name: str = ""
        self.next_config_watch_retry: float = float("inf")
        self._arm_config_watch()
        self.epoll.register(self.config_events, select.EPOLLIN)

        self._restore()
        _ = self.run_pass()

    def _finish_priority(self) -> None:
        if isinstance(self.priority, records.NoQueuePriority):
            return
        transition = self.priority.transition
        if isinstance(transition, records.SettledPriority):
            return
        gate.finish_priority_transition(self.priority)
        self.priority = (records.QueuePriority(self.priority.entries) if self.priority.entries
                         else records.NO_QUEUE_PRIORITY)
        self.persist()


    def _restore(self) -> None:
        path = self.state_dir / "state.json"
        if path.exists():
            state = read_state(self.state_dir)
            self.reservations, self.places, self.ledger = state.reservations, state.places, state.ledger
            self.spacing, self.override, self.priority = state.spacing, state.override, state.priority
            self.build_history_generation = state.build_history_generation
            self.run_places = state.run_places
            self.lifecycle_outbox = state.lifecycle_outbox
            self.caller_versions = state.caller_versions
            self.placement_requests = state.placement_requests
            self.assignments = state.assignments
            self.current_assignments = state.current_assignments
            self._persisted = wire.encode_record(state)
            if not isinstance(self.override, records.NoHeadAdmissionOverride):
                self.override_reconnect_until = ReconnectAwaitingListener()
        self.persist()
        for episode, record in list(self.reservations.items()):
            if not self._end_if_finished(episode, record):
                lifetime = self._lifetime(record)
                self._watch_owner(record.owner)
                self._watch_scope(lifetime, episode)
        self._prune_places()
        self._finish_priority()
        for request in self.placement_requests.values():
            if request.owner.host == self.host and self._live(wire.Owner(request.owner.pid, request.owner.start_time)):
                self._watch_owner(request.owner)

    def _end_if_finished(self, episode: str, record: ReservationRecord) -> bool:
        lifetime = self._lifetime(record)
        if (not isinstance(record.finalization, NoFinalizationIntent)
                or not isinstance(record.ending, NotEnding) or isinstance(lifetime, gate.EndedStep)):
            with retry_cleanup(Path(record.path)):
                self._end(episode, record.ending if not isinstance(record.ending, NotEnding) else OwnerExited())
            return episode not in self.reservations
        return False

    def _live_reservation_ids(self) -> set[str]:
        return {record.reservation_id for record in self.reservations.values()}

    def _inputs_with_current_slots(self, inputs: records.QueueInputs) -> records.QueueInputs:
        self._prune_runs()
        self._claim_slots()
        remaining = gate.inputs_after_reservations_end(inputs, self._live_reservation_ids())
        if remaining.priority != self.priority:
            ordered, priority = gate.apply_queue_priority(remaining.ordered, gate.QueueOrderContext(self.priority, {}))
            remaining = replace(remaining, ordered=ordered, priority=priority,
                                waiter_orders={records.waiter_owner(row): records.waiter_priority(row) for _, row in ordered})
        return replace(remaining, slots=tuple(self.slots.values()))

    def persist(self) -> None:
        now = self.clock.now()
        live_callers: dict[str, str] = {}
        for key, version in self.caller_versions.items():
            pid, separator, start_time = key.partition("-")
            if separator and pid.isdecimal() and self._live(wire.Owner(int(pid), start_time)):
                live_callers[key] = version
        self.caller_versions = live_callers
        live_reservations = self._live_reservation_ids()
        live_dispatches = {"dispatch:" + key for key, saved in self.assignments.items()
                           if isinstance(saved.state, placement.CurrentAssignment)}
        self.ledger = {key: replace(entry, ended_at=now)
                       if isinstance(entry.ended_at, LiveGrant)
                       and key not in self.reservations and entry.reservation not in live_reservations
                       and key not in live_dispatches
                       else entry for key, entry in self.ledger.items()}
        if not isinstance(self.last_usable_configuration, NoPass):
            self.ledger = {key: entry for key, entry in self.ledger.items()
                           if isinstance(entry.ended_at, LiveGrant) or key in self.reservations
                           or key in live_dispatches
                           or entry.reservation in live_reservations
                           or now - entry.ended_at < self.last_usable_configuration.replay_retention_seconds}
        state = ServiceState(self.reservations, self.places, self.ledger, self.spacing, self.override, self.priority,
            self.build_history_generation, self.run_places, self.caller_versions, self.lifecycle_outbox,
            self.placement_requests, self.assignments, self.current_assignments)
        encodings: dict[tuple[str, str], DurableRecordEncoding] = {}

        def encoded_record(name: str, key: str, value: object) -> DurableRecordEncoding:
            previous = self.durable_encodings.get((name, key))
            if previous is None or previous.value != value:
                previous = DurableRecordEncoding(durable_copy(value), wire.encode_record(value),
                    previous.key if previous is not None else json.dumps(key, ensure_ascii=False))
            encodings[name, key] = previous
            return previous

        parts = ['"kind":"ServiceState"']
        for member in fields(state):
            value = cast(object, getattr(state, member.name))
            if isinstance(value, dict):
                records = (encoded_record(member.name, key, item)
                           for key, item in cast(dict[str, object], value).items())
                encoded_value = "{" + ",".join(record.key + ":" + record.encoded for record in records) + "}"
            else:
                encoded_value = encoded_record(member.name, "", value).encoded
            parts.append(json.dumps(member.name) + ":" + encoded_value)
        encoded = "{" + ",".join(parts) + "}"
        self.durable_encodings = encodings
        if encoded != self._persisted:
            write_text_atomic(self.state_dir / "state.json", encoded)
            self._persisted = encoded
            self.fault("after_state_write")
        self._drain_lifecycle()

    def _unwatch(self, descriptor: int) -> None:
        _ = self.watchers.pop(descriptor, None)
        try:
            self.epoll.unregister(descriptor)
        except (OSError, FileNotFoundError):
            pass
        os.close(descriptor)

    def _watch_owner(self, owner: wire.Owner | placement.SubmittingOwner) -> None:
        if isinstance(owner, placement.SubmittingOwner):
            if owner.host != self.host:
                return
            owner = wire.Owner(owner.pid, owner.start_time)
        if any(isinstance(watch, OwnerWatch) and watch.owner == owner for watch in self.watchers.values()):
            return
        try:
            descriptor = self.proc.pidfd(owner.pid)
        except (OSError, ProcessLookupError):
            return
        try:
            self.epoll.register(descriptor, select.EPOLLIN)
        except OSError:
            os.close(descriptor)
            return
        self.watchers[descriptor] = OwnerWatch(owner)

    def _watch_scope(self, lifetime: gate.StepLifetime,
                     episode: str) -> None:
        if not isinstance(self.system_reader, NoSystemReader) or not isinstance(lifetime, gate.PopulatedStepScope):
            return
        path = lifetime.directory / "cgroup.events"
        for descriptor, watch in list(self.watchers.items()):
            if isinstance(watch, ScopeWatch) and watch.episode == episode:
                if watch.path == path:
                    return
                self._unwatch(descriptor)
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            return
        try:
            self.epoll.register(descriptor, select.EPOLLPRI | select.EPOLLERR)
            self.watchers[descriptor] = ScopeWatch(path, episode)
            _ = os.read(descriptor, 4096)
        except OSError:
            self._unwatch(descriptor)

    def _prune_watches(self) -> None:
        owners = {record.owner for record in self.reservations.values()}
        owners.update(waiting.request.owner for waiting in self.waiters.values())
        owners.update(place.owner for place in self.places.values())
        owners.update(wire.Owner(place.run.pid, place.run.start_time) for place in self.run_places.values())
        owners.update(wire.Owner(request.owner.pid, request.owner.start_time)
                      for request in self.placement_requests.values() if request.owner.host == self.host)
        for descriptor, watch in list(self.watchers.items()):
            if (isinstance(watch, OwnerWatch) and watch.owner not in owners
                    or isinstance(watch, ScopeWatch) and watch.episode not in self.reservations):
                self._unwatch(descriptor)

    def _ordered_waiters(self) -> list[tuple[Path, records.RankedWaiter]]:
        rows: list[tuple[Path, records.RankedWaiter]] = []
        for waiting in self.waiters.values():
            if isinstance(waiting.request, wire.AdmitRequest) and isinstance(waiting.request.place, wire.CloudPlace):
                continue
            row = gate.rank_waiter(waiting.row, records.NormalOrder())
            row["order_since"] = row.get("order_since", row["since"])
            if isinstance(waiting.request, wire.AdmitRequest) and isinstance(waiting.request.run, wire.RunIdentity):
                place = self.run_places.get(records.run_owner(waiting.request.run.pid, waiting.request.run.start_time))
                if place is not None:
                    row["order_since"] = min(place.since, row["order_since"])
            rows.append((self.state_dir / records.waiter_owner(row), row))
        return sorted(rows, key=lambda item: gate.waiter_order(item[1]))

    def _prune_priority(self) -> bool:
        self._finish_priority()
        if not isinstance(self.priority, records.QueuePriority):
            return False
        identities = {records.priority_step(waiting.row) for waiting in self.waiters.values()}
        identities.update(records.priority_step(record.queue_waiter) for record in self.reservations.values())
        identities.update(records.SelectedPriorityStep(place.owner.pid, place.owner.start_time, place.since)
                          for place in self.places.values())
        entries = tuple(entry for entry in self.priority.entries
                        if entry["step"] in identities and self._live(wire.Owner(entry["step"].pid, entry["step"].start_time)))
        if entries == self.priority.entries:
            return False
        self.priority = records.QueuePriority(entries) if entries else records.NoQueuePriority()
        self.persist()
        return True

    def _queue_order(self) -> tuple[list[tuple[Path, records.RankedWaiter]], records.QueuePriority | records.NoQueuePriority]:
        _ = self._prune_priority()
        rows = self._ordered_waiters()
        sessions = [row.get("session_id", "") for _, row in rows if not row.get("unit")]
        labels: Mapping[str, gate.UnitMembership] = (
            gate.priority_labels(sessions)
            if sessions and not isinstance(self.priority, records.NoQueuePriority) else {})
        return gate.apply_queue_priority(rows, gate.QueueOrderContext(self.priority, labels))

    def _override_pending_reconnect(self) -> bool:
        if isinstance(self.override_reconnect_until, NoReconnectGrace):
            return False
        if (isinstance(self.override, records.NoHeadAdmissionOverride)
                or any(gate.head_override_matches(self.override, path, row)
                       for path, row in self._ordered_waiters())):
            self.override_reconnect_until = NoReconnectGrace()
            return False
        if (isinstance(self.override_reconnect_until, ReconnectDeadline)
                and not self.receiving_requests
                and self.clock.monotonic() >= self.override_reconnect_until.at):
            self.override_reconnect_until = NoReconnectGrace()
            return False
        return True

    def _prune_runs(self) -> None:
        now = self.clock.now()
        for key, slot in list(self.slots.items()):
            if (not self._run_alive(gate.between_steps(slot).run)
                    or not gate.slot_in_force(slot, now)):
                del self.slots[key]
        for key, place in list(self.run_places.items()):
            owner = wire.Owner(place.run.pid, place.run.start_time)
            if self._live(owner):
                self._watch_owner(owner)
            else:
                del self.run_places[key]

    def _free_claim(self, owner: str) -> None:
        for key, slot in list(self.slots.items()):
            if isinstance(slot, records.ContinuationSlot) and slot.owner == owner:
                if gate.slot_in_force(slot.slot, self.clock.now()) and self._run_alive(slot.slot.run):
                    self.slots[key] = slot.slot
                else:
                    del self.slots[key]

    def _claim_slots(self) -> None:
        claimed = {slot.owner for slot in self.slots.values() if isinstance(slot, records.ContinuationSlot)}
        for waiting in sorted(self.waiters.values(), key=lambda item: gate.waiter_order(item.row)):
            request = waiting.request
            if isinstance(request, wire.AdmitRequest) and isinstance(request.place, wire.CloudPlace):
                continue
            if not isinstance(request, wire.AdmitRequest) or not isinstance(request.run, wire.RunIdentity):
                continue
            owner = records.waiter_owner(waiting.row)
            if owner in claimed or isinstance(gate.waiter_state(waiting.row), records.TokenWaitingWaiter):
                continue
            candidates = [(key, slot) for key, slot in self.slots.items()
                          if isinstance(slot, records.BetweenStepsSlot) and slot.run.pid == request.run.pid
                          and slot.run.start_time == request.run.start_time]
            if not candidates:
                continue
            key, slot = min(candidates, key=lambda item: (item[1].expires_at, item[0]))
            self.slots[key] = records.ContinuationSlot(slot, owner)
            claimed.add(owner)

    def _run_place(self, request: wire.AdmitRequest | wire.LaunchRequest, place: WaiterPlace) -> None:
        if isinstance(request, wire.AdmitRequest) and isinstance(request.place, wire.CloudPlace):
            return
        if not isinstance(request, wire.AdmitRequest) or not isinstance(request.run, wire.RunIdentity):
            return
        run = request.run
        key = records.run_owner(run.pid, run.start_time)
        if key not in self.run_places and self._run_alive(run):
            self.run_places[key] = wire.RunPlace(run, place.since, request.owner, request.identity.unit)

    def _live(self, owner: wire.Owner) -> bool:
        if not isinstance(self.system_reader, NoSystemReader):
            identity = self.system_reader.latest.processes.get(owner.pid)
            return identity is not None and identity.start == owner.start_time
        return self.proc.owner_start(owner.pid) == LiveOwnerIdentity(owner.start_time)

    def _account(self, pid: int) -> int | OwnerProcessGone:
        if not isinstance(self.system_reader, NoSystemReader):
            return self.system_reader.latest.accounts.get(pid, OwnerProcessGone())
        return process_uid(pid, self.proc.path)

    def _run_alive(self, run: wire.RunIdentity | LiveBuildRun) -> bool:
        return self._live(wire.Owner(run.pid, run.start_time))

    def _prune_places(self) -> None:
        for key, place in list(self.places.items()):
            if not self._live(place.owner) and not any(record.owner == place.owner for record in self.reservations.values()):
                self._record_left(place.episode, place.queue_identity, "owner_gone")
                del self.places[key]
        self.persist()

    def _row(self, request: wire.AdmitRequest | wire.LaunchRequest, place: WaiterPlace | records.NoWaiter) -> records.Waiter:
        profile, identity = request.profile, request.identity
        since = request.since if isinstance(place, records.NoWaiter) else place.since
        row: records.Waiter = {
            "pid": request.owner.pid, "start_time": request.owner.start_time,
            "need": (0 if isinstance(request, wire.AdmitRequest) and isinstance(request.place, wire.CloudPlace)
                     else profile.peak.bytes), "since": since,
            "order_since": (place.order_since if isinstance(place, WaiterPlace)
                            and not isinstance(place.order_since, OrderNotStarted) else since),
            "session_id": identity.session_id, "session_name": identity.name,
            "step": identity.step, "folder": identity.folder, "unit": identity.unit,
            "estimated_at": profile.estimated_at or self.clock.now(),
            "fresh_test": isinstance(request, wire.AdmitRequest) and request.fresh_test,
            "need_source": profile.peak.source, "need_runs": profile.peak.count,
            "need_basis": gate.need_basis_record(profile.peak.basis), "test_scope": profile.scope,
            "repository_name": profile.repo,
        }
        if isinstance(place, WaiterPlace) and not isinstance(place.priority_since, OrderNotStarted):
            row["priority_since"] = place.priority_since
        if isinstance(place, WaiterPlace) and not isinstance(place.token_left_at, TokenNotLeft):
            row["token_left_at"] = place.token_left_at
            if isinstance(request, wire.AdmitRequest) and request.token_back_at:
                row["token_back_at"] = request.token_back_at
        if isinstance(profile.repository, records.RepositoryIdentity):
            row["repo_identity"] = profile.repository.key
            row["repository_name"] = profile.repository.name or profile.repo
            row["legacy_name"] = profile.repository.legacy_name
        if isinstance(request, wire.AdmitRequest):
            row["package"] = step_estimator.test_package(list(request.argv))
            row["filters"] = list(step_estimator.test_filters(list(request.argv)))
            if isinstance(request.test_width, records.RequestedTestWidth):
                row["test_threads"] = request.test_width.threads
        return row

    def _advance_build_history(self) -> None:
        self.build_history_generation += 1
        self.persist()

    def _build_estimate_expired(self, request: wire.AdmitRequest | wire.LaunchRequest) -> bool:
        profile = request.profile
        if not isinstance(profile, records.StepProfile) or profile.step != "build":
            return False
        generation = profile.build_history_generation
        if not isinstance(generation, records.BuildHistoryGeneration) or generation.value != self.build_history_generation:
            return True
        shape = build_shape(list(request.argv)).key if isinstance(request, wire.AdmitRequest) else ""
        return any(record.step == "build" and isinstance(record.finalization, dict)
                   and matches_build_estimate(record.repo, record.repo_identity, record.shape, profile, shape)
                   for record in self.reservations.values())

    def _reading_plan(self, config: records.MemoryConfig) -> SystemReadingPlan:
        return SystemReadingPlan(config, tuple(replace(record, queue_waiter=record.queue_waiter.copy())
            for record in self.reservations.values() if isinstance(record.place, wire.LocalPlace)),
            tuple(self.run_places.values()), bool(self._ordered_waiters()))

    def _lifetime(self, record: ReservationRecord) -> gate.StepLifetime:
        if isinstance(record.place, wire.CloudPlace):
            live = self._live(record.owner)
            if isinstance(record.run, wire.RunIdentity):
                live = live and self._run_alive(record.run)
            return gate.LiveOwnerProcess() if live else gate.EndedStep()
        if not isinstance(self.system_reader, NoSystemReader):
            return self.system_reader.latest.lifetimes.get(record.episode, gate.LiveOwnerProcess())
        return self.proc.lifetime(reservation_row(record), self._live(record.owner))

    def _inputs(self, config: records.MemoryConfig, profile_rank: int, profile_share: int) -> records.QueueInputs:
        now = self.clock.now()
        plan = self._reading_plan(config)
        if isinstance(self.system_reader, NoSystemReader):
            def direct_memory(record: ReservationRecord, row: records.Reservation, lifetime: gate.StepLifetime,
                              processes: dict[int, gate.ProcessIdentity], proc: Path) -> records.StepMemory:
                return (records.GoneMemory() if isinstance(lifetime, gate.EndedStep) else
                    self.readings.reservation(Path(record.path), row, datetime.fromtimestamp(now, UTC),
                                              processes, proc, config.policy))
            evidence = read_system(plan, self.readings, self.proc, self.clock, reservation_reader=direct_memory)
        else:
            self.system_reader.raise_failure()
            self.system_reader.update(plan)
            evidence = self.system_reader.latest
        self.system_evidence = evidence
        memories: list[tuple[records.Reservation, records.StepMemory]] = []
        for episode, record in list(self.reservations.items()):
            if isinstance(record.place, wire.CloudPlace):
                _ = self._end_if_finished(episode, record)
                continue
            if isinstance(record.finalization, dict) or not isinstance(record.ending, NotEnding):
                with retry_cleanup(Path(record.path)):
                    self._end(episode, record.ending)
                if episode not in self.reservations:
                    continue
            row = reservation_row(record)
            if "build_run" not in row:
                row["build_run"] = evidence.runs.get(record.owner.pid, NoLiveBuildRun())
                row["run_origin"] = records.FoundRun(row["build_run"])
            elif isinstance(record.run, wire.RunIdentity) and not self._run_alive(record.run):
                row["run_origin"] = records.CallerRun(NoLiveBuildRun())
            captured = evidence.reservations.get(episode)
            memory = (reservation_charge(captured, row, now, config.policy) if captured is not None
                      else records.PromisedMemory(record.need))
            if isinstance(memory, records.GoneMemory):
                with retry_cleanup(Path(record.path)):
                    self._end(episode, record.ending if not isinstance(record.ending, NotEnding) else OwnerExited())
                continue
            if record.episode.startswith("uid:") and isinstance(memory, records.MeasuredMemory):
                if isinstance(memory.anonymous, records.AnonymousRunUse):
                    self._sample_runner(record, memory.anonymous.bytes, now)
            memories.append((row, memory))
            self._watch_scope(evidence.lifetimes.get(episode, gate.LiveOwnerProcess()), episode)
        for waiting in self.waiters.values():
            if isinstance(waiting.request, wire.AdmitRequest) and isinstance(waiting.request.place, wire.CloudPlace):
                continue
            waiting.row["build_history_current"] = not self._build_estimate_expired(waiting.request)
            request = waiting.request
            if "build_run" not in waiting.row:
                waiting.row["build_run"] = (_live_run(request.run)
                    if isinstance(request, wire.AdmitRequest) and isinstance(request.run, wire.RunIdentity)
                    else evidence.runs.get(request.owner.pid, NoLiveBuildRun()))
        pressure = evidence.pressure
        if isinstance(pressure, records.MemoryPressure):
            pressure = gate.pressure_from_readings(pressure.full_avg10, pressure.some_avg10,
                                                  pressure.swap_used_percent, config.policy)
        readings = gate.QueueSnapshotReadings(dict(evidence.processes), {}, evidence.host,
            pressure, self.spacing, list(evidence.compilers), policy=config.policy)
        ordered, priority = self._queue_order()
        unowned = self._unowned_memory(evidence, [row for row, _memory in memories])
        inputs = gate.build_queue_inputs(ordered, memories, readings, self.override, now, priority, unowned)
        represented = {(step.run.pid, step.run.start_time) for step in inputs.running
                       if isinstance(step.run, LiveBuildRun)}
        between = tuple(step for step in evidence.between
                        if isinstance(step.run, LiveBuildRun) and (step.run.pid, step.run.start_time) not in represented)
        if between:
            inputs = replace(inputs, running=(*inputs.running, *between))
        return self._progress_inputs(self._inputs_with_current_slots(inputs), dict(evidence.processes),
                                     config.policy, profile_rank, profile_share)

    def _unowned_memory(self, evidence: SystemEvidence, rows: list[records.Reservation]
                        ) -> tuple[records.UnownedSliceMemory, ...]:
        owned: set[str] = set()
        scopes: dict[str, str] = {}
        scope_memory: dict[Path, records.MeasuredMemory] = {}
        active = {row.get("reservation_id", "") for row in rows}
        for record in self.reservations.values():
            if isinstance(record.place, wire.CloudPlace) and record.sidecar:
                try:
                    scope = Path(record.sidecar).read_text().strip()
                except (OSError, UnicodeError):
                    scope = ""
                if scope:
                    owned.add(scope)
            captured = evidence.reservations.get(record.episode)
            if captured is not None and captured.scope:
                owned.add(captured.scope)
                scopes[record.reservation_id] = captured.scope
                if record.reservation_id in active and isinstance(captured.memory, records.MeasuredMemory):
                    scope_memory[Path(captured.scope)] = captured.memory
        roots = {row["pid"]: row["start_time"] for row in rows if not scopes.get(row.get("reservation_id", ""))}
        tree_pids = gate.process_tree(roots, dict(evidence.processes))
        result: list[records.UnownedSliceMemory] = []
        for memory in evidence.unowned:
            required = not isinstance(memory.job_state, NotServiceSlice)
            if not required and memory.cgroup in owned:
                continue
            scoped = [scope_memory[scope] for scope in gate.owned_scope_roots(scope_memory, Path(memory.cgroup))] if required else []
            scoped_pids = {pid for reading in scoped if isinstance(reading.anonymous_source, records.ScopeSource)
                           for pid in reading.anonymous_source.members}
            duplicate = [evidence.scope_process_memory[pid]
                         for pid in (evidence.scope_members.get(memory.cgroup, frozenset()) & tree_pids) - scoped_pids
                         if pid in evidence.scope_process_memory]
            use = sum(item.anonymous.bytes if required and isinstance(item.anonymous, records.AnonymousRunUse)
                      else item.bytes for item in duplicate)
            use += sum(item.anonymous.bytes for item in scoped if isinstance(item.anonymous, records.AnonymousRunUse))
            job = memory.job_state
            workers = evidence.scope_workers.get(memory.cgroup, frozenset())
            if workers and all(gate.process_tree({pid: evidence.processes[pid].start},
                               dict(evidence.processes)).intersection(tree_pids | scoped_pids) for pid in workers):
                job = IdleService()
            result.append(replace(memory, bytes=max(0, memory.bytes - use), job_state=job,
                                  swap_bytes=max(0, memory.swap_bytes - sum(item.swap_bytes for item in (*duplicate, *scoped)))))
        return tuple(result)

    def _sample_runner(self, record: ReservationRecord, use: int, now: float) -> None:
        maximum = Path(record.path).with_suffix(".max")
        try:
            peak = int(maximum.read_text())
        except (OSError, ValueError):
            peak = 0
        runner_peak = 0
        if record.sidecar:
            try:
                runner_peak = int(Path(record.sidecar).with_suffix(".max").read_text())
            except (OSError, ValueError):
                pass
        maximum_use = max(peak, use, runner_peak)
        if maximum_use > peak:
            atomic_sample(maximum, f"{maximum_use}\n")
        wave = Path(record.path).with_suffix(".wave")
        if use > 0 and use >= maximum_use / 2:
            atomic_sample(wave, f"{maximum_use} {int(now)}\n")
        elif runner_peak > 0 and runner_peak == maximum_use:
            try:
                sampled, at = map(int, Path(record.sidecar).with_suffix(".wave").read_text().split())
                if sampled == maximum_use:
                    atomic_sample(wave, f"{sampled} {at}\n")
            except (OSError, ValueError):
                pass

    def _progress_inputs(self, inputs: records.QueueInputs, processes: dict[int, gate.ProcessIdentity],
                         policy: records.AdmissionPolicy, profile_rank: int, profile_share: int) -> records.QueueInputs:
        """Capture counters and profile samples on the service loop thread."""
        attributed = iter(gate.attributed_step_uses(inputs))
        running: list[records.RunningStep] = []
        cutoff = inputs.now - step_estimator.HISTORY_DAYS * 86400
        self.progress_history.prune(cutoff, rank=profile_rank, share_percent=profile_share)
        for step in inputs.running:
            if not gate.running_has_reservation(step):
                running.append(step)
                continue
            row, step, use = next(attributed)
            if row["step"] not in gate.PROGRESS_STEPS:
                running.append(step)
                continue
            memory = step.measured
            if not isinstance(memory, records.MeasuredMemory):
                running.append(step)
                continue
            sidecar = Path(row["sidecar"])
            counter = progress.read_counter(sidecar) if row["sidecar"] else progress.NoCounter()
            if isinstance(counter, progress.NoCounter) and row["sidecar"] and row["step"] != "merge-gate":
                counter = self.progress_captures.log_counter(progress.progress_path(sidecar, "log"))
            if isinstance(counter, progress.NoCounter) and row["step"] != "merge-gate" and self.boot_id:
                pids = gate.process_tree({row["pid"]: row["start_time"]}, processes)
                counter = self.progress_captures.counter({pid: processes[pid].start for pid in pids}, self.boot_id)
            key = gate.reservation_profile_key(row, self.host)
            profile = self.progress_history.matching(key, cutoff, rank=profile_rank, share_percent=profile_share)
            adjusted = gate.progress_memory(row, memory, use, counter, profile, policy)
            running.append(replace(step, measured=adjusted))
            if use.owns_source and isinstance(use.use, records.AnonymousRunUse) and "reservation_id" in row:
                source = memory.anonymous_source
                if isinstance(source, records.ScopeSource):
                    reading_source: progress.ReadingSource = progress.ScopeReading(source.directory)
                    pids = tuple(sorted(source.members))
                elif isinstance(source, records.ProcessSource):
                    pids = tuple(item.pid for item in source.processes)
                    reading_source = progress.ProcessTreeReading(pids)
                else:
                    pids = ()
                    reading_source = progress.ReadingSourceNotRecorded("reading source unavailable")
                self.progress_history.observe(row["reservation_id"], key, counter, use.use.bytes, inputs.now,
                    reading_source=reading_source,
                    read_processes=lambda pids=pids, source=reading_source:
                        progress.PeakProcessesNotRecorded("reading source unavailable")
                        if isinstance(source, progress.ReadingSourceNotRecorded) else progress.read_processes(pids, self.proc.path))
        self.progress_captures.end_pass()
        return replace(inputs, running=tuple(running))

    def run_pass(self) -> gate.PassResult | gate.UnusableAdmissionConfiguration:
        return self._run_pass(self._take_configuration())

    def _read_configuration(self) -> gate.UsableAdmissionConfiguration | gate.UnusableAdmissionConfiguration:
        configuration = gate.read_admission_configuration()
        if isinstance(configuration, gate.UsableAdmissionConfiguration):
            self.last_usable_configuration = configuration.config
        return configuration

    def _take_configuration(self) -> gate.UsableAdmissionConfiguration | gate.UnusableAdmissionConfiguration:
        configuration = self.pending_configuration
        self.pending_configuration = NoPass()
        return self._read_configuration() if isinstance(configuration, NoPass) else configuration

    def _arm_override_reconnect(self, config: records.MemoryConfig) -> None:
        if isinstance(self.override_reconnect_until, ReconnectAwaitingListener) and not isinstance(self.listener, NoConnection):
            self.override_reconnect_until = ReconnectDeadline(self.clock.monotonic() + max(
                config.override_reconnect_seconds, config.reconnect_seconds))
            self.next_pass = min(self.next_pass, self.override_reconnect_until.at)

    def _check_reading_alert(self, inputs: records.QueueInputs, result: gate.PassResult, config: records.MemoryConfig) -> None:
        """Measure unreadable holds with the service clock and start at most one sender."""
        if isinstance(inputs.host, records.ReadingUnavailable):
            path = Path(os.environ.get("BUILDLOG_MEMINFO", "/proc/meminfo"))
            reason = inputs.host.reason
        elif isinstance(inputs.pressure, records.PressureUnavailable):
            path = Path(os.environ.get("BUILDLOG_MEMORY_PRESSURE", "/proc/pressure/memory"))
            reason = inputs.pressure.reason
        else:
            self.reading_hold = NoReadingHold()
            if self.reading_alert_needs_clear:
                try:
                    self.reading_alert_needs_clear = not outage_alerts.clear(READING_ALERT_KEY, self.clock.now())
                except (OSError, ValueError) as error:
                    print(f"memory admission: cannot clear reading alert: {error}", file=sys.stderr)
            return
        self.reading_alert_needs_clear = True
        held = any(row.decision.state == "hold" and not isinstance(row.reason, records.TokenWaitingWaiter)
                   and not isinstance(row.decision.room.basis, records.WithinSlot)
                   for row in result.rows)
        if not held:
            self.reading_hold = NoReadingHold()
            return
        since = self.reading_hold.since if isinstance(self.reading_hold, UnreadableReadingHold) else self.clock.monotonic()
        self.reading_hold = UnreadableReadingHold(since, path, reason)
        elapsed = self.clock.monotonic() - since
        if elapsed < config.reading_alert_seconds:
            return
        if isinstance(self.reading_alert_delivery, ReadingAlertDelivery) and self.reading_alert_delivery.thread.is_alive():
            return
        try:
            claimed = outage_alerts.claim(READING_ALERT_KEY)
            if not isinstance(claimed, outage_alerts.AlertClaim):
                return
            thread = threading.Thread(target=deliver_reading_alert, args=(claimed, self.reading_hold, elapsed),
                                      name="admission-reading-alert", daemon=True)
            try:
                thread.start()
            except RuntimeError:
                outage_alerts.release(claimed)
                raise
            self.reading_alert_delivery = ReadingAlertDelivery(thread)
        except (OSError, ValueError, RuntimeError) as error:
            print(f"memory admission: cannot start reading alert: {error}", file=sys.stderr)

    def _run_pass(self, configuration: gate.UsableAdmissionConfiguration | gate.UnusableAdmissionConfiguration
                  ) -> gate.PassResult | gate.UnusableAdmissionConfiguration:
        self._prune_runs()
        self.configuration = configuration
        self._placement_pass()
        self.pass_stale = False
        if isinstance(self.configuration, gate.UnusableAdmissionConfiguration):
            self.reading_hold = NoReadingHold()
            self.next_pass = self.next_config_watch_retry
            self.next_source_check = (self.clock.monotonic() + self.last_usable_configuration.source_check_seconds
                                      if isinstance(self.last_usable_configuration, records.MemoryConfig) else float("inf"))
            self.last_inputs = self.last_result = self.last_policy = NoPass()
            self.published = NoPass()
            self.last_diagnostics = ()
            for episode, record in list(self.reservations.items()):
                if self._end_if_finished(episode, record):
                    self._prune_places()
            self.persist()
            (self.state_dir / "queue.json").unlink(missing_ok=True)
            for waiting in self.waiters.values():
                self._deliver(waiting, wire.Refused(self.configuration.reason))
                if isinstance(waiting.connection, Connection):
                    if self._detach_admission(waiting.connection):
                        self._prune_watches()
            return self.configuration
        config = self.configuration.config
        self.last_usable_configuration = config
        self._arm_override_reconnect(config)
        self.next_source_check = min(self.next_source_check,
                                     self.clock.monotonic() + config.source_check_seconds)
        policy = config.policy
        self.last_diagnostics = config.diagnostics
        inputs = self._inputs(config, self.configuration.progress_profile_rank, self.configuration.peak_history_share_percent)
        preserve_override = self._override_pending_reconnect()
        if preserve_override:
            inputs = replace(inputs, override=records.NoHeadAdmissionOverride())
        result = gate.admission_pass(inputs, inputs.now, policy)
        self._check_reading_alert(inputs, result, config)
        self.last_inputs, self.last_result, self.last_policy = inputs, result, policy
        cloud_started = self._cloud_pass(inputs, self.configuration)
        by_owner = {records.waiter_owner(waiting.row): episode for episode, waiting in self.waiters.items()}
        starts = sorted((row for row in result.rows if isinstance(row.admission, gate.ProposedAdmission)),
                        key=lambda row: cast(gate.ProposedAdmission, row.admission).rank)
        if not starts:
            self._effects(inputs.spacing, result.policy.override, result.policy.totals, preserve_override)
        for decision in starts:
            episode = by_owner[decision.owner]
            waiting = self.waiters[episode]
            proposed = cast(gate.ProposedAdmission, decision.admission)
            self._effects(proposed.spacing, proposed.override, decision.decision, preserve_override)
            self._admit(waiting, config, self.configuration.peak_history_share_percent)
        self.persist()
        waiting_steps = tuple(records.WaitingStep(row["pid"], row.get("session_id", ""), row.get("session_name", ""),
            row.get("step", ""), row.get("folder", ""), row["since"], row["need"], decision.reason, row.get("unit", ""))
            for (_, row), decision in zip(inputs.ordered, result.rows, strict=True))
        for position, decision in enumerate(result.rows, 1):
            episode = by_owner[decision.owner]
            if episode in self.waiters:
                head = inputs.ordered[0][1].get("session_name", "") if inputs.ordered else ""
                waiting = self.waiters[episode]
                reason = decision.reason
                reply = wire.Wait(reason, position, head, reason_phrase(reason, waiting_steps),
                    self._wait_snapshot(waiting, decision.decision, inputs, config, reason, decision.holder))
                if waiting.last_reply != reply or inputs.now - waiting.last_sent >= config.wait_reply_seconds:
                    self._deliver(waiting, reply)
                    waiting.last_reply, waiting.last_sent = reply, inputs.now
        interval = config.waiting_pass_seconds if self.waiters else config.running_pass_seconds
        self.next_pass = self.clock.monotonic() + interval
        if isinstance(self.override_reconnect_until, ReconnectDeadline):
            self.next_pass = min(self.next_pass, self.override_reconnect_until.at)
        if starts or cloud_started:
            self.next_pass = self.clock.monotonic()
        try:
            ordered, priority = self._queue_order()
            queue_snapshot.write_queue_snapshot(self.state_dir,
                replace(inputs, ordered=ordered, spacing=self.spacing, override=self.override, priority=priority), policy)
        except OSError:
            # An old snapshot expires without changing the admission result.
            pass
        self._publish_replies()
        if self.reader_requested:
            self.start_reader()
        if not isinstance(self.system_reader, NoSystemReader):
            self.system_reader.update(self._reading_plan(config))
        return result

    def _cloud_waiters(self) -> list[WaitingAdmission]:
        return sorted((waiting for waiting in self.waiters.values()
            if isinstance(waiting.request, wire.AdmitRequest) and isinstance(waiting.request.place, wire.CloudPlace)),
            key=lambda waiting: waiting.row["since"])

    def _cloud_pass(self, inputs: records.QueueInputs, configuration: gate.UsableAdmissionConfiguration) -> bool:
        waiting_cloud = self._cloud_waiters()
        if not waiting_cloud:
            return False
        config = configuration.config
        slots = configuration.cloud_gate_slots
        used = sum(isinstance(record.place, wire.CloudPlace) for record in self.reservations.values())
        started = False
        position = 0
        for waiting in waiting_cloud:
            if used < slots:
                self._admit(waiting, config, configuration.peak_history_share_percent)
                used += 1
                started = True
                continue
            position += 1
            reason = wire.CloudSlotWait(used, slots)
            snapshot = AdmissionWaitSnapshot(records.AdmissionDecision("hold", 0, 0, 0, 0),
                replace(waiting.request.profile.peak, bytes=0), (), 0,
                waiting.request.profile.repo, waiting.request.profile.step, config.policy, config.diagnostics)
            reply = wire.Wait(reason, position, "", reason.text, snapshot)
            if waiting.last_reply != reply or inputs.now - waiting.last_sent >= config.wait_reply_seconds:
                self._deliver(waiting, reply)
                waiting.last_reply, waiting.last_sent = reply, inputs.now
        return started

    def _cloud_reading(self, reading: records.QueueRead, configuration: gate.UsableAdmissionConfiguration
                       ) -> records.QueueRead | wire.CloudQueueRead:
        rows: list[wire.CloudRow] = []
        for record in self.reservations.values():
            if not isinstance(record.place, wire.CloudPlace):
                continue
            waiter = record.queue_waiter
            identity = wire.Identity(waiter.get("session_id", ""), waiter.get("session_name", ""),
                record.step, record.worktree, waiter.get("unit", ""))
            sidecar = Path(record.sidecar)
            rows.append(wire.CloudRow(record.owner, identity, record.place, waiter["since"],
                step_estimator.epoch(record.admitted_at), cloud_progress.read(sidecar) if record.sidecar else cloud_progress.NoCloudProgress(),
                progress.read_counter(sidecar) if record.sidecar else progress.NoCounter(), records.ReadyForAdmission()))
        used = len(rows)
        waiting_cloud = self._cloud_waiters()
        if not rows and not waiting_cloud:
            return reading
        slots = configuration.cloud_gate_slots
        for waiting in waiting_cloud:
            request = waiting.request
            assert isinstance(request, wire.AdmitRequest) and isinstance(request.place, wire.CloudPlace)
            sidecar = Path(request.sidecar)
            rows.append(wire.CloudRow(request.owner, request.identity, request.place, waiting.row["since"], 0.0,
                cloud_progress.read(sidecar) if request.sidecar else cloud_progress.NoCloudProgress(),
                progress.read_counter(sidecar) if request.sidecar else progress.NoCounter(), wire.CloudSlotWait(used, slots)))
        return wire.CloudQueueRead(reading.view, reading.explanation, reading.ordered, reading.diagnostics,
            tuple(rows), slots, cloud_gate_stale_pushes=configuration.cloud_gate_stale_pushes)

    def _publish_replies(self) -> None:
        if (not isinstance(self.configuration, gate.UsableAdmissionConfiguration)
                or not isinstance(self.last_inputs, records.QueueInputs)
                or not isinstance(self.last_policy, records.AdmissionPolicy)
                or not isinstance(self.last_result, gate.PassResult)):
            return
        reading = replace(gate.queue_read_from_pass(self.last_inputs, self.last_policy, self.last_result),
                          diagnostics=self.last_diagnostics)
        reading = self._cloud_reading(reading, self.configuration)
        view = wire.ViewReply(reading)
        explain = wire.ExplainReply(reading.explanation, self.last_diagnostics)
        self.published = PublishedReplies(view, explain)

    def reply_bytes(self, reply: wire.Reply, *, version: int = wire.VERSION) -> bytes:
        if version != wire.VERSION:
            return wire.encode_reply(reply, version=version).encode()
        published = self.published
        if isinstance(published, PublishedReplies):
            if reply is published.view:
                if not published.view_bytes:
                    published.view_bytes = wire.encode_reply(reply).encode()
                return published.view_bytes
            if reply is published.explain:
                if not published.explain_bytes:
                    published.explain_bytes = wire.encode_reply(reply).encode()
                return published.explain_bytes
            if isinstance(reply, wire.MineReply) and reply.reading is published.view.reading:
                if not published.mine_bytes:
                    published.mine_bytes = self.reply_bytes(published.view).replace(
                        b'"type":"view","data":{"kind":"ViewReply",',
                        b'"type":"mine","data":{"kind":"MineReply",', 1)
                return published.mine_bytes
        return wire.encode_reply(reply).encode()

    def _effects(self, spacing: records.StartSpacing, override: gate.OverrideKept | gate.OverrideEnded,
                 decision: records.AdmissionDecision, preserve_override: bool) -> None:
        self.spacing = spacing
        if preserve_override:
            return
        if isinstance(override, gate.OverrideEnded):
            available = (self.last_inputs.host.available if isinstance(self.last_inputs, records.QueueInputs)
                         and isinstance(self.last_inputs.host, records.HostMemory) else 0)
            gate.head_override_event(override.action, override.record, override.record["need"],
                available, decision.reserve, self.clock.now())
            self.override = records.NoHeadAdmissionOverride()
        else:
            self.override = override.record

    def _save_place(self, waiting: WaitingAdmission) -> None:
        row, request = waiting.row, waiting.request
        if isinstance(request, wire.AdmitRequest) and isinstance(request.place, wire.CloudPlace):
            return
        key = _place_key(request.owner, row["since"])
        previous = self.places.get(key)
        place = waiter_place(row, request.episode)
        self.places[key] = replace(place, queue_identity=previous.queue_identity) if previous is not None else place


    def _wait_snapshot(self, waiting: WaitingAdmission, decision: records.AdmissionDecision,
                       inputs: records.QueueInputs, config: records.MemoryConfig,
                       reason: records.WaitReason, holder: WaitHolder) -> AdmissionWaitSnapshot:
        head = gate.first_queued_waiter(row for _, row in inputs.ordered)
        head_need = head.waiter["need"] if isinstance(head, gate.QueuedWaiter) else 0
        head_label = head.waiter.get("session_name", "") if isinstance(head, gate.QueuedWaiter) else ""
        return AdmissionWaitSnapshot(decision, waiting.request.profile.peak, inputs.running,
            inputs.host.available if isinstance(inputs.host, records.HostMemory) else 0,
            waiting.request.profile.repo, waiting.request.profile.step, config.policy, config.diagnostics,
            head_need if not isinstance(decision.order_hold, records.NoOrderHold) else 0, head_label, reason, holder)

    def _drain_lifecycle(self) -> None:
        """Publish saved lifecycle facts after each durable state transition."""
        while self.lifecycle_outbox:
            pending = self.lifecycle_outbox[0]
            if isinstance(pending, PendingAdmission):
                record = self.reservations[pending.episode]
                admitted_at = datetime.fromisoformat(record.admitted_at).timestamp()
                event: gate.AdmittedEvent = {
                    **gate.lifecycle_event(gate.queue_life_identity(record.queue_waiter), "admitted", record.reservation_id),
                        "reservation_id": record.reservation_id, "repo": record.repo,
                        "admitted_at": admitted_at, "need": record.need, "need_source": pending.need_source,
                        "is_head": pending.is_head,
                        "wait_seconds": pending.wait_seconds,
                }
                gate.append_queue_time(self.state_dir, cast(dict[str, object], codec.snapshot_value(event)), admitted_at)
                self.fault("after_admitted_append")
            else:
                left: gate.LeftEvent = {
                    **gate.lifecycle_event(pending.identity, "left", pending.episode + ":" + pending.reason),
                    "reason": pending.reason, "ended_at": pending.ended_at,
                }
                gate.append_queue_time(self.state_dir, cast(dict[str, object], codec.snapshot_value(left)), pending.ended_at)
                self.fault("after_left_append")
            self.lifecycle_outbox = self.lifecycle_outbox[1:]

    def _record_left(self, episode: str, identity: records.QueueLifeIdentity, reason: gate.LeftReason) -> None:
        self.lifecycle_outbox = (*self.lifecycle_outbox, PendingLeft(identity, episode, reason, self.clock.now()))

    def _freeze_test_phase_need(self, record: ReservationRecord, config: records.MemoryConfig,
                                share_percent: int) -> ReservationRecord:
        if record.step != "nextest":
            return record
        need = gate.test_phase_need(reservation_row(record), self.host,
            datetime.fromtimestamp(self.clock.now(), UTC), self.state_dir,
            history_path=self.state_dir / "anon_peaks.jsonl", config=config, share_percent=share_percent)
        return replace(record, test_phase_need=need)

    def _admit(self, waiting: WaitingAdmission, config: records.MemoryConfig, share_percent: int) -> None:
        request, profile = waiting.request, waiting.request.profile
        now = self.clock.now()
        if isinstance(request, wire.LaunchRequest):
            reply: wire.Reply = wire.LaunchAdmitted(profile.peak.bytes, profile.peak.source)
            self.ledger[request.episode] = ReplayEntry(reply, now)
        else:
            stem = gate.reservation_stem(self.state_dir, request.owner.pid, request.owner.start_time, int(now * 1_000_000_000))
            # The episode disambiguates grants at the same clock tick after release.
            while any(record.path == str(stem) for record in self.reservations.values()):
                stem = stem.with_name(stem.name + "0")
            if profile.step == "build":
                shape = build_shape(list(request.argv)).key
            elif profile.step == "merge-gate":
                shape = gate.merge_gate_shape(request.argv)
            else:
                shape = ""
            cloud = isinstance(request.place, wire.CloudPlace)
            record = ReservationRecord(request.episode, str(stem), request.owner, 0 if cloud else profile.peak.bytes,
                profile.repo, profile.step, profile.worktree, store.utc_iso(now), request.sidecar,
                request.test_width, request.call, stem.name, records.stored_waiter(waiting.row),
                profile.repository.key if isinstance(profile.repository, records.RepositoryIdentity) else "",
                profile.scope, step_estimator.test_package(list(request.argv)), tuple(step_estimator.test_filters(list(request.argv))),
                shape, run=request.run, place=request.place)
            if not cloud:
                record = self._freeze_test_phase_need(record, config, share_percent)
            result = self.last_result
            candidate = result.policy.queued_head if isinstance(result, gate.PassResult) else gate.NoQueuedHead()
            self.lifecycle_outbox = (*self.lifecycle_outbox, PendingAdmission(request.episode, profile.peak.source,
                isinstance(candidate, gate.QueuedHead)
                    and records.waiter_owner(candidate.waiter) == records.waiter_owner(waiting.row),
                max(0.0, now - waiting.row["since"])))
            self.reservations[request.episode] = record
            for key, slot in list(self.slots.items()):
                if isinstance(slot, records.ContinuationSlot) and slot.owner == records.waiter_owner(waiting.row):
                    del self.slots[key]
            reply = wire.Admitted(stem.name, record.need, profile.peak.source, str(stem.with_suffix(".max")),
                                  str(stem.with_suffix(".wave")), {} if cloud else gate.sampling_context(reservation_row(record)),
                                  wire.CleanupTiming(config.silence_seconds, config.reconnect_seconds))
            self.ledger[request.episode] = ReplayEntry(reply, LiveGrant())
            self._watch_owner(request.owner)
        self._save_place(waiting)
        # Durable grant and replay reply precede any terminal reply.
        self.persist()
        del self.waiters[request.episode]
        self._deliver(waiting, reply)

    def _end(self, episode: str, ending: ReleaseStatus | OwnerExited | NotEnding | TokenWaiting) -> None:
        record = self.reservations[episode]
        record = replace(record, ending=ending)
        self.reservations[episode] = record
        self.persist()
        row = reservation_row(record)
        if isinstance(ending, (OwnerExited, NotEnding)):
            lifetime = self._lifetime(record)
            if isinstance(lifetime, gate.PopulatedStepScope):
                self._watch_scope(lifetime, episode)
                return
        ended_at = (step_estimator.epoch(record.finalization["ended_at"])
                    if isinstance(record.finalization, dict) else self.clock.now())
        if record.episode.startswith("uid:"):
            self._sample_runner(record, 0, ended_at)
        self.progress_history.finish(record.reservation_id, ended_at, ended_at - step_estimator.HISTORY_DAYS * 86400)
        if record.sidecar:
            self.progress_captures.remove(Path(record.sidecar))
        path = Path(record.path)
        outcome, status, end_kind = finalization_outcome(ending)
        if isinstance(ending, TokenWaiting):
            row_waiter = record.queue_waiter.copy()
            row_waiter["token_left_at"] = ending.left_at
            self.places[_place_key(record.owner, row_waiter["since"])] = waiter_place(row_waiter, record.episode)
        _ = gate.finalize_reservation(path, row, outcome, datetime.fromtimestamp(ended_at, UTC), status,
            intents=StateIntents(self.reservations, self._advance_build_history, episode, self.persist, self.fault, self.places),
            end_kind=end_kind)
        if (isinstance(record.place, wire.LocalPlace) and isinstance(record.run, wire.RunIdentity)
                and self._run_alive(record.run)
                and isinstance(self.last_usable_configuration, records.MemoryConfig)):
            slot = gate.ended_step_slot(row, ended_at, self.last_usable_configuration.policy)
            if isinstance(slot, records.BetweenStepsSlot):
                self.slots[slot.reservation] = slot
        self._prune_watches()
        for key, entry in list(self.ledger.items()):
            if entry.reservation == record.reservation_id:
                self.ledger[key] = replace(entry, ended_at=self.clock.now())
        previous = self.ledger.get(episode)
        if previous is not None:
            self.ledger[episode] = replace(previous, ended_at=self.clock.now())
        if isinstance(ending, (ReleaseStatus, TokenWaiting)):
            self.ledger[ending.request_id] = ReplayEntry(wire.Ok(), self.clock.now())
        _ = self._prune_priority()
        self.persist()

    def host_evidence_key(self, request: wire.PlacementRequest,
                          destination: placement.PlacementDestination) -> placement.HostEvidenceKey:
        from scripts.build_targets import target_config

        paths = {conf.path("build_targets"), conf.path("build_admission"), conf.path("mac_test")}
        for target in target_config.read_targets():
            if isinstance(target, target_config.SshTarget) and target.name == destination.target:
                paths.add(target.settings)
        digest = hashlib.sha256()
        for path in sorted(paths):
            digest.update(str(path).encode())
            try:
                digest.update(path.read_bytes())
            except FileNotFoundError:
                digest.update(b"missing")
        return placement.HostEvidenceKey(destination, request.constraints.session,
            request.constraints.command, digest.hexdigest(), request.constraints.repository,
            request.constraints.package, request.profile.step)

    def _host_probe_timeout(self, destination: placement.PlacementDestination) -> float:
        from scripts.build_targets import target_config

        for target in target_config.read_targets():
            if isinstance(target, target_config.SshTarget) and target.name == destination.target:
                return conf.read(target.settings).number("probe_timeout_s")
        return conf.read("mac_test").number("probe_timeout_s")

    def publish_host_evidence(self, evidence: placement.HostEvidence) -> None:
        self.host_observations[evidence.key] = evidence
        reader = self.host_readers.get(evidence.key)
        if reader is not None:
            reader.latest = evidence
        try:
            os.eventfd_write(self.reading_events, 1)
        except OSError:
            pass

    def host_evidence(self, key: placement.HostEvidenceKey) -> placement.HostEvidence:
        reader = self.host_readers.get(key)
        evidence = (reader.latest if reader is not None else self.host_observations.get(key,
                    placement.HostEvidenceUnavailable(key, self.clock.now(), "not read")))
        if isinstance(evidence, placement.FreshHostEvidence):
            interval = (self.last_usable_configuration.waiting_pass_seconds
                        if isinstance(self.last_usable_configuration, records.MemoryConfig) else 0.0)
            timeout = reader.timeout if reader is not None else self._host_probe_timeout(key.destination)
            if self.clock.now() - evidence.observed_at > interval + timeout:
                return placement.StaleHostEvidence(key, evidence.observed_at, "provider evidence expired")
        return evidence

    def _request_host_evidence(self, request: wire.PlacementRequest,
                               destination: placement.PlacementDestination) -> placement.HostEvidence:
        key = self.host_evidence_key(request, destination)
        self.host_requests[key] = request
        current = self.host_evidence(key)
        if isinstance(current, placement.FreshHostEvidence):
            return current
        if key not in self.host_readers and isinstance(self.last_usable_configuration, records.MemoryConfig):
            previous = self.host_observations.get(key)
            reader = HostEvidenceReader(key, lambda current: self.host_probe(current), self.clock,
                self.reading_events, self.last_usable_configuration.waiting_pass_seconds,
                self._host_probe_timeout(destination))
            self.host_readers[key] = reader
            if previous is not None:
                reader.latest = previous
        return self.host_evidence(key)

    def _read_host_evidence(self, key: placement.HostEvidenceKey) -> placement.HostEvidence:
        """Read provider policy and exact claims outside the service loop."""
        from scripts.build_targets import build_targets, target_config
        from scripts.mac_test import claim_state, offload

        request = self.host_requests[key]
        destination = key.destination
        if destination.provider not in ("mac", "ssh"):
            return placement.HostEvidenceUnavailable(key, self.clock.now(), "provider uses its existing admission path")
        target = next((target for target in target_config.read_targets() if target.name == destination.target), None)
        if target is None:
            return placement.HostEvidenceUnavailable(key, self.clock.now(), "target is no longer configured")
        kind = offload.verb_kind(cast(offload.MacVerb, request.profile.step))
        if kind not in ("check", "lint", "doc", "test", "build", "example", "final"):
            return placement.HostEvidenceUnavailable(key, self.clock.now(), "command policy is unavailable")
        if isinstance(target, target_config.SshTarget):
            answer = build_targets.ssh_target_eligibility(target, Path(request.constraints.repository),
                request.constraints.package, kind, target_config.ANY_ELIGIBLE_TARGET, session=key.session)
        else:
            answer = build_targets.mac_target_eligibility(target, Path(request.constraints.repository),
                request.constraints.package, kind, target_config.ANY_ELIGIBLE_TARGET, session=key.session)
        eligibility: placement.HostEligibility = (placement.TemporaryExclusion(answer.reason)
            if isinstance(answer, target_config.ExcludedTarget) else placement.AllowedHost())
        paths = claim_state.state_paths(destination.target)
        adopted: list[placement.AdoptedProviderClaim] = []
        with claim_state.locked(paths.lock):
            held = claim_state.read_claims(paths)
        generation = ""
        for claim in held:
            record = claim.record
            if "remote_host" not in record:
                continue
            reference = placement.ProviderClaimReference(destination.provider, destination.target, claim.scope,
                record["claim_id"], record["episode"], record["attempt"])
            session_id = record["submitting_session"]
            session = OwningSession(session_id) if session_id else NoOwningSession()
            adopted.append(placement.AdoptedProviderClaim(reference,
                placement.SubmittingOwner(self.host, record["pid"], record["proc_start"]),
                session, record["remote_host"], record.get("instance_generation", "")))
        return placement.FreshHostEvidence(key, self.clock.now(), generation, eligibility, tuple(adopted))

    def _placement_owner_live(self, owner: placement.SubmittingOwner) -> bool:
        return owner.host == self.host and self._live(wire.Owner(owner.pid, owner.start_time))

    def _placement_status(self, assignment: placement.PlacementAssignment, reason: str = "") -> wire.DispatchStatus:
        saved = self.assignments.get(placement.attempt_key(assignment.episode, assignment.attempt))
        if saved is None:
            return wire.DispatchStatus(placement.UnknownAssignment(assignment.episode, assignment.attempt),
                placement.ClosedAssignment("unknown attempt"), placement.NoLaunchPermission("unknown attempt"), reason)
        return wire.DispatchStatus(saved.assignment, saved.state, saved.permission, reason)

    def _placement_reply(self, request: wire.PlacementRequest) -> wire.Assignment | wire.PlacementWait | wire.DispatchStatus:
        current = self.current_assignments.get(request.episode, "")
        saved = self.assignments.get(current)
        if saved is not None:
            if isinstance(saved.state, placement.CurrentAssignment):
                return wire.Assignment(saved.assignment)
            return self._placement_status(saved.assignment)
        eligibility: list[placement.CandidateEligibility] = []
        for candidate in request.constraints.candidates:
            destination = candidate.destination
            permitted: placement.HostEligibility = candidate.eligibility
            selection = request.constraints.selection
            if isinstance(selection, placement.PinnedHost) and selection.target != destination.target:
                permitted = placement.FixedExclusion("another target is pinned")
            if isinstance(permitted, placement.AllowedHost):
                evidence = self._request_host_evidence(request, destination)
                permitted = (evidence.eligibility if isinstance(evidence, placement.FreshHostEvidence)
                             else placement.EvidenceNeeded(evidence.reason))
            eligibility.append(placement.CandidateEligibility(destination, permitted))
            if isinstance(permitted, placement.AllowedHost):
                assignment = placement.PlacementAssignment(request.episode, uuid.uuid4().hex, destination,
                    request.constraints.source_key, request.owner, request.constraints.session, request.since)
                key = placement.attempt_key(request.episode, assignment.attempt)
                self.assignments[key] = records.SavedPlacementAssignment(assignment)
                self.current_assignments[request.episode] = key
                self.persist()
                return wire.Assignment(assignment)
        ordered = sorted(self.placement_requests.values(), key=lambda item: (item.since, item.owner.pid, item.episode))
        position = next(index for index, waiting in enumerate(ordered, 1) if waiting.episode == request.episode)
        return wire.PlacementWait(request.episode, request.since, position, tuple(eligibility))

    def _placement_pass(self) -> None:
        changed = False
        for request in sorted(self.placement_requests.values(), key=lambda item: (item.since, item.owner.pid, item.episode)):
            current = self.current_assignments.get(request.episode, "")
            saved = self.assignments.get(current)
            if not self._placement_owner_live(request.owner):
                if (saved is not None and isinstance(saved.state, placement.CurrentAssignment)
                        and isinstance(saved.permission, placement.PermissionAvailable)):
                    self.assignments[current] = replace(saved, state=placement.ClosedAssignment("owner ended"))
                    changed = True
                continue
            if isinstance(self.configuration, gate.UsableAdmissionConfiguration):
                _ = self._placement_reply(request)
        if changed:
            self.persist()

    def _consume_permission(self, request: wire.ConsumePermission) -> wire.FirstAuthorization | wire.DispatchStatus:
        assignment = request.assignment
        key = placement.attempt_key(assignment.episode, assignment.attempt)
        saved = self.assignments.get(key)
        if (saved is None or saved.assignment != assignment
                or self.current_assignments.get(assignment.episode) != key
                or not isinstance(saved.state, placement.CurrentAssignment)
                or isinstance(saved.permission, placement.PermissionConsumed)):
            return self._placement_status(assignment)
        submitted = self.placement_requests.get(assignment.episode)
        if (submitted is None or submitted.constraints.source_key != assignment.source_key
                or submitted.owner != assignment.owner or not self._placement_owner_live(assignment.owner)):
            return self._placement_status(assignment, "assignment owner or source is no longer valid")
        reference = request.claim
        destination = assignment.destination
        if (not reference.claim_id or reference.episode != assignment.episode or reference.attempt != assignment.attempt
                or reference.provider != destination.provider or reference.target != destination.target
                or reference.scope != destination.scope):
            return self._placement_status(assignment, "claim does not match the assignment")
        evidence = self._request_host_evidence(submitted, destination)
        if not isinstance(evidence, placement.FreshHostEvidence):
            return self._placement_status(assignment, "adoption evidence is unavailable or stale")
        adopted = next((claim for claim in evidence.claims
                        if claim.reference == reference and claim.owner == assignment.owner
                        and claim.session == assignment.session and claim.host == destination.host
                        and claim.generation == evidence.generation), None)
        if adopted is None:
            return self._placement_status(assignment, "the exact claim is not adopted")
        saved = replace(saved, permission=placement.PermissionConsumed(self.clock.now()), provider=reference)
        self.assignments[key] = saved
        status = wire.DispatchStatus(assignment, saved.state, saved.permission)
        self.ledger["dispatch:" + key] = ReplayEntry(status, LiveGrant())
        self.persist()
        return wire.FirstAuthorization(assignment, reference)

    def _handle_placement(self, request: wire.PlacementRequest | wire.ConsumePermission | wire.ClaimFailure | wire.ExecutionReport
                          ) -> list[wire.Reply]:
        if isinstance(request, wire.ConsumePermission):
            return [self._consume_permission(request)]
        if isinstance(request, wire.PlacementRequest):
            if not self._placement_owner_live(request.owner):
                return [wire.Refused("submitting owner is not live on the coordinator")]
            previous = self.placement_requests.get(request.episode)
            if previous is not None and previous != request:
                return [wire.Refused("placement constraints changed within an episode")]
            self.placement_requests[request.episode] = request
            self._watch_owner(request.owner)
            self.persist()
            return [self._placement_reply(request)]
        assignment = request.assignment
        key = placement.attempt_key(assignment.episode, assignment.attempt)
        saved = self.assignments.get(key)
        if saved is None or saved.assignment != assignment or self.current_assignments.get(assignment.episode) != key:
            return [self._placement_status(assignment)]
        if isinstance(request, wire.ClaimFailure):
            if isinstance(saved.permission, placement.PermissionAvailable) and isinstance(saved.state, placement.CurrentAssignment):
                self.assignments[key] = replace(saved, state=placement.ClosedAssignment(request.reason))
                del self.current_assignments[assignment.episode]
                self.persist()
        elif (isinstance(saved.permission, placement.PermissionConsumed) and saved.provider == request.claim):
            self.execution_evidence[key] = request.evidence
        return [self._placement_status(assignment)]

    def _authorize(self, request: wire.Request, uid: int) -> str:
        if isinstance(request, (wire.PlacementRequest, wire.ConsumePermission, wire.ClaimFailure, wire.ExecutionReport)):
            owner = request.owner if isinstance(request, wire.PlacementRequest) else request.assignment.owner
            episode = request.episode if isinstance(request, wire.PlacementRequest) else request.assignment.episode
            if uid != os.getuid() and (not episode.startswith(f"uid:{uid}:") or self._account(owner.pid) != uid):
                return "placement belongs to another account"
            return ""
        if uid == os.getuid():
            return ""
        if isinstance(request, (wire.PrioritySetRequest, wire.PriorityClearRequest, wire.AdmitHeadRequest)):
            return "queue priority and head admission require the service account"
        if isinstance(request, (wire.BuildHistoryRequest, wire.ViewRequest, wire.ExplainRequest, wire.MineRequest)):
            return ""
        key = (request.episode if isinstance(request, (wire.AdmitRequest, wire.ServiceEstimateRequest, wire.LaunchRequest))
               else request.request_id)
        if not key.startswith(f"uid:{uid}:"):
            return "request belongs to another account"
        if isinstance(request, (wire.AdmitRequest, wire.ServiceEstimateRequest, wire.LaunchRequest)):
            if self._account(request.owner.pid) != uid or not self._live(request.owner):
                return "owner belongs to another account or no longer exists"
            if isinstance(request, wire.AdmitRequest) and isinstance(request.run, wire.RunIdentity):
                if self._account(request.run.pid) != uid or not self._run_alive(request.run):
                    return "run belongs to another account or no longer exists"
            return ""
        if key in self.ledger:
            return ""
        record = next((record for record in self.reservations.values()
                       if record.reservation_id == request.reservation), None)
        if record is None or not record.episode.startswith(f"uid:{uid}:"):
            return "reservation belongs to another account"
        return ""

    def _runner_reply(self, reply: wire.Reply) -> wire.Reply:
        if not isinstance(reply, wire.Admitted):
            return reply
        config = records.read_memory_config()
        return wire.RunnerAdmitted(reply.reservation, reply.need, reply.source, reply.max_path,
            reply.wave_path, reply.sampling_context, reply.cleanup_timing,
            execution=wire.RunnerExecutionTiming(config.program_sample_seconds,
                config.program_stop_seconds, config.peak_wave_seconds))

    def _service_estimate(self, request: wire.ServiceEstimateRequest,
                          configuration: gate.UsableAdmissionConfiguration) -> wire.AdmitRequest:
        config = configuration.config
        estimate = step_estimator.step_estimate(list(request.argv), request.cwd, records.INFER_FROM_ARGV,
            lambda: datetime.fromtimestamp(self.clock.now(), UTC), self.state_dir.parent, config.policy,
            first_peak_bytes=config.program_first_peaks["cargo"],
            share_percent=configuration.peak_history_share_percent,
            build_history_generation=lambda: records.BuildHistoryGeneration(self.build_history_generation),
            location=step_estimator.RepositoryLocation(request.repository, request.worktree))
        label = config.ci_label.format(runner=request.runner, job=request.job)
        identity = wire.Identity("", label, estimate.profile.step, request.worktree, request.runner)
        return ServiceEstimatedAdmission(request.episode, request.owner, request.argv, request.cwd, request.since,
            request.sidecar, identity, records.NoCallRecord(), request.test_width, estimate.profile,
            isinstance(estimate.cache, records.FreshTest), code_version=request.code_version)

    def handle_request(self, request: wire.Request, connection: Connection | NoConnection = NO_CONNECTION
                       ) -> list[wire.Reply]:
        started, cpu_started = self.clock.monotonic(), time.thread_time()
        try:
            return self._handle_request(request, connection)
        finally:
            self._log_request_timing(request, started, cpu_started)

    def _log_request_timing(self, request: wire.Request, started: float, cpu_started: float) -> None:
        try:
            with conf.diagnostics(report=False) as messages:
                limit = conf.read("build_admission").number("request_slow_seconds")
        except conf.Unusable as error:
            records.warn_configuration_diagnostics((records.ConfigurationDiagnostic(str(error)),))
        else:
            records.warn_configuration_diagnostics(records.configuration_diagnostics(messages))
            wall, cpu = self.clock.monotonic() - started, time.thread_time() - cpu_started
            if wall > limit:
                print(f"admission request {type(request).__name__}: wall={wall:.3f}s cpu={cpu:.3f}s",
                      file=sys.stderr, flush=True)

    def _handle_request(self, request: wire.Request, connection: Connection | NoConnection
                        ) -> list[wire.Reply]:
        refusal = self._authorize(request, peer_uid(connection))
        if refusal:
            return [wire.Refused(refusal)]
        if isinstance(connection, Connection):
            self._record_caller_version(request, connection)
        if isinstance(request, (wire.PlacementRequest, wire.ConsumePermission, wire.ClaimFailure, wire.ExecutionReport)):
            return self._handle_placement(request)
        if isinstance(request, wire.ServiceEstimateRequest):
            replay = self.ledger.get(request.episode)
            if replay is not None:
                return [self._runner_reply(replay.reply)]
            configuration = self._take_configuration()
            if isinstance(configuration, gate.UnusableAdmissionConfiguration):
                _ = self._run_pass(configuration)
                return [wire.Refused(configuration.reason)]
            request = self._service_estimate(request, configuration)
            self.pending_configuration = configuration
        if isinstance(request, (wire.AdmitRequest, wire.LaunchRequest)) and request.code_version:
            self.caller_versions[records.run_owner(request.owner.pid, request.owner.start_time)] = request.code_version
        if isinstance(request, wire.BuildHistoryRequest):
            return [wire.BuildHistoryReply(self.build_history_generation)]
        if isinstance(request, (wire.ViewRequest, wire.ExplainRequest, wire.MineRequest)):
            if self._prune_priority():
                self.pass_stale = True
            if not isinstance(self.pending_configuration, NoPass):
                _ = self.run_pass()
            elif self.pass_stale and isinstance(self.last_inputs, records.QueueInputs):
                if isinstance(self.last_policy, records.AdmissionPolicy):
                    self.last_inputs = self._inputs_with_current_slots(self.last_inputs)
                    self.last_result = gate.admission_pass(self.last_inputs, self.last_inputs.now, self.last_policy)
                self.pass_stale = False
                self._publish_replies()
            if isinstance(self.configuration, gate.UnusableAdmissionConfiguration):
                return [wire.Refused(self.configuration.reason)]
            if (not isinstance(self.last_inputs, records.QueueInputs)
                    or not isinstance(self.last_policy, records.AdmissionPolicy)):
                raise RuntimeError("service has no pass")
            published = self.published
            if not isinstance(published, PublishedReplies):
                raise RuntimeError("service has no published pass")
            if isinstance(request, wire.ExplainRequest):
                return [published.explain]
            if isinstance(request, wire.ViewRequest):
                return [published.view]
            return [wire.MineReply(published.view.reading)]
        key = request.episode if isinstance(request, (wire.AdmitRequest, wire.LaunchRequest)) else request.request_id
        replay = self.ledger.get(key)
        if replay is not None:
            return [replay.reply]
        if isinstance(request, (wire.PrioritySetRequest, wire.PriorityClearRequest, wire.AdmitHeadRequest)):
            return self._submit_queue_change(request, connection)
        configuration = (self.configuration if isinstance(request, (wire.ReleaseRequest, wire.TokenWaitRequest))
                         else self._take_configuration())
        if isinstance(request, (wire.AdmitRequest, wire.LaunchRequest)):
            if not self._live(request.owner):
                return self._mutation_reply(key, wire.Refused("owner no longer exists"))
            if any(record.owner == request.owner for record in self.reservations.values()):
                return self._mutation_reply(key, wire.Refused("owner already has a reservation"))
            if any(waiting.request.owner == request.owner and episode != key
                   for episode, waiting in self.waiters.items()):
                return self._mutation_reply(key, wire.Refused("owner already has an admission episode"))
            self._pending_replies[key] = []
            place_key = _place_key(request.owner, request.since)
            row = self._row(request, self.places.get(place_key, records.NoWaiter()))
            place = waiter_place(row, key)
            self._run_place(request, place)
            self.waiters[key] = WaitingAdmission(request, row, connection)
            self._save_place(self.waiters[key])
            self._watch_owner(request.owner)
            if (isinstance(request, wire.AdmitRequest) and request.token_back_at
                    and not isinstance(place.token_left_at, TokenNotLeft)):
                gate.append_queue_time(self.state_dir, {"kind": "token_gap", "episode": key,
                    **gate.queue_life_identity(self.waiters[key].row),
                    "token_left_at": place.token_left_at, "token_back_at": request.token_back_at}, self.clock.now())
        elif isinstance(request, wire.ReleaseRequest):
            episode = next((episode for episode, record in self.reservations.items() if record.reservation_id == request.reservation), None)
            if episode is not None:
                self._end(episode, ReleaseStatus(key, request.status))
            self.ledger[key] = ReplayEntry(wire.Ok(), self.clock.now())
        elif isinstance(request, wire.WidthRequest):
            if request.width <= 0:
                return self._mutation_reply(key, wire.Refused("test width must be positive"), request.reservation)
            for episode, record in list(self.reservations.items()):
                if record.reservation_id == request.reservation:
                    updated = replace(record, test_width=records.RequestedTestWidth(request.width),
                                      test_phase_need=gate.NoTestPhaseNeed("not_recorded"))
                    if (isinstance(record.place, wire.LocalPlace)
                            and isinstance(configuration, gate.UsableAdmissionConfiguration)):
                        updated = self._freeze_test_phase_need(updated, configuration.config,
                                                               configuration.peak_history_share_percent)
                    self.reservations[episode] = updated
            live = any(record.reservation_id == request.reservation for record in self.reservations.values())
            self.ledger[key] = ReplayEntry(wire.Ok(), LiveGrant() if live else self.clock.now(), request.reservation)
        else:
            for episode, record in list(self.reservations.items()):
                if record.reservation_id == request.reservation:
                    self._end(episode, TokenWaiting(key, self.clock.now()))
            self.ledger[key] = ReplayEntry(wire.Ok(), self.clock.now())
        if isinstance(request, (wire.ReleaseRequest, wire.TokenWaitRequest)):
            self.persist()
            if (isinstance(self.last_inputs, records.QueueInputs)
                    and isinstance(self.last_policy, records.AdmissionPolicy)):
                self.last_inputs = self._inputs_with_current_slots(self.last_inputs)
                self.pass_stale = True
            self.next_pass = self.clock.monotonic()
            return [self.ledger[key].reply]
        _ = self._run_pass(configuration)
        if isinstance(request, (wire.AdmitRequest, wire.LaunchRequest)):
            return self._pending_replies.pop(key, [])
        return [self.ledger[key].reply]

    def _submit_queue_change(self, request: QueueChange,
                             connection: Connection | NoConnection) -> list[wire.Reply]:
        connections = [connection] if isinstance(connection, Connection) else []
        if isinstance(self.pending_priority, PendingPriorityRoster):
            for change in (self.pending_priority.change, *self.queue_changes):
                if change.request.request_id == request.request_id:
                    for client in connections:
                        if all(client is not previous for previous in change.connections):
                            change.connections.append(client)
                    return []
            self.queue_changes.append(OrderedQueueChange(request, connections))
            return []
        if isinstance(request, wire.PrioritySetRequest) and request.unit.strip():
            result: Future[Mapping[str, gate.UnitMembership]] = Future()
            self.pending_priority = PendingPriorityRoster(OrderedQueueChange(request, connections), result)
            threading.Thread(target=read_priority_roster, args=(result, os.dup(self.priority_events)),
                             daemon=True, name="priority-roster").start()
            return []
        return self._apply_queue_change(request, {})

    def _finish_priority_roster(self) -> None:
        pending = self.pending_priority
        if not isinstance(pending, PendingPriorityRoster) or not pending.result.done():
            return
        self.pending_priority = NoPendingPriorityRoster()
        self._complete_queue_change(pending.change,
            lambda: self._apply_queue_change(pending.change.request, pending.result.result()))
        self._continue_queue_changes()

    def _continue_queue_changes(self) -> None:
        while self.queue_changes and isinstance(self.pending_priority, NoPendingPriorityRoster):
            change = self.queue_changes.popleft()
            self._complete_queue_change(change,
                lambda: self._submit_queue_change(change.request, NO_CONNECTION))
            if isinstance(self.pending_priority, PendingPriorityRoster):
                self.pending_priority.change.connections.extend(change.connections)

    def _complete_queue_change(self, change: OrderedQueueChange,
                               apply: Callable[[], list[wire.Reply]]) -> None:
        try:
            replies = apply()
        except (ValueError, TypeError) as error:
            for connection in change.connections:
                if connection.socket.fileno() >= 0:
                    self._refuse_connection(connection, str(error))
        else:
            self._reply_to_queue_change(change, replies)

    def _reply_to_queue_change(self, change: OrderedQueueChange, replies: list[wire.Reply]) -> None:
        for connection in change.connections:
            if connection.socket.fileno() >= 0:
                for reply in replies:
                    self._send(connection, reply)

    def _apply_queue_change(self, request: QueueChange,
                            labels: Mapping[str, gate.UnitMembership]) -> list[wire.Reply]:
        key = request.request_id
        configuration = self._take_configuration()
        if isinstance(request, (wire.PrioritySetRequest, wire.PriorityClearRequest)):
            if isinstance(request, wire.PrioritySetRequest):
                if not request.unit.strip():
                    return self._mutation_reply(key, wire.Refused("unit is required"))
                _ = self._prune_priority()
                entries = self.priority.entries if isinstance(self.priority, records.QueuePriority) else ()
                rows = [waiting.row for waiting in self.waiters.values()]
                rows.extend(record.queue_waiter for record in self.reservations.values())
                selected = next((row for row in rows if records.priority_step(row) == request.step
                                 and self._live(wire.Owner(row["pid"], row["start_time"]))), None)
                if (selected is None or request.unit == "—"
                        or gate.queue_unit(selected.get("unit", ""), selected.get("session_id", ""), labels) != request.unit):
                    return self._mutation_reply(key, wire.Refused(
                        f"no live step matches {request.selector or request.unit}; the queue priority did not change"))
                if any(entry["step"] == request.step for entry in entries):
                    return self._mutation_reply(key, wire.Ok())
                changed = True
                entry: records.PriorityEntry = {"step": request.step, "unit": request.unit, "why": request.why,
                    "set_at": self.clock.now(), "set_by": request.actor}
                entries = (*entries, entry)
                unit, why, actor = request.unit, request.why, request.actor
            else:
                entries = self.priority.entries if isinstance(self.priority, records.QueuePriority) else ()
                if request.unit and not any(entry["unit"] == request.unit for entry in entries):
                    return self._mutation_reply(key, wire.Refused(
                        f"unknown unit {request.unit}; the queue priority did not change"))
                removed = tuple(entry for entry in entries if not request.unit or entry["unit"] == request.unit)
                changed = bool(removed)
                entries = tuple(entry for entry in entries if request.unit and entry["unit"] != request.unit)
                unit = request.unit or ", ".join(entry["unit"] for entry in removed)
                why, actor = ", ".join(entry["why"] for entry in removed), request.actor
            at = self.clock.now()
            month_row_end = store.read_last_whole_row_end(store.host_file(self.host, at))
            if not changed:
                return self._mutation_reply(key, wire.Ok())
            transition = records.PendingPriorityTransition(key,
                "set" if isinstance(request, wire.PrioritySetRequest) else "clear", at,
                month_row_end, unit, why, actor)
            self.priority = records.QueuePriority(entries, transition)
            self.ledger[key] = ReplayEntry(wire.Ok(), self.clock.now())
            self.persist()
            self._finish_priority()
        else:
            ordered = self.last_inputs.ordered if isinstance(self.last_inputs, records.QueueInputs) else ()
            if not ordered:
                return self._mutation_reply(key, wire.Refused("nothing waits; no head to admit"))
            if not isinstance(self.last_inputs, records.QueueInputs) or not isinstance(self.last_inputs.host, records.HostMemory):
                return self._mutation_reply(key, wire.Refused("memory reading unavailable"))
            candidate = gate.first_queued_waiter(row for _, row in ordered)
            if isinstance(candidate, records.NoWaiter):
                return self._mutation_reply(key, wire.Refused("all waiting steps are waiting for the cargo token"))
            row = candidate.waiter
            host = self.last_inputs.host
            reserve = cast(records.AdmissionPolicy, self.last_policy).safety_bytes
            self.override = {"waiter": records.waiter_owner(row), "pid": row["pid"], "start_time": row["start_time"],
                "unit": gate.waiter_unit_name(gate.RecordedQueueIdentities().identity(row["pid"],
                    cast(dict[str, object], cast(object, row))), row["pid"]),
                "step": row.get("step", ""), "need": row["need"], "free": host.available, "reserve": reserve,
                "why": request.why, "set_by": request.actor}
            gate.head_override_event("set", self.override, row["need"], host.available, reserve, self.clock.now())
            self.ledger[key] = ReplayEntry(wire.Ok(), self.clock.now())
        _ = self._run_pass(configuration)
        return [self.ledger[key].reply]

    def _mutation_reply(self, key: str, reply: wire.Ok | wire.Refused, reservation: str = "") -> list[wire.Reply]:
        live = any(record.reservation_id == reservation for record in self.reservations.values())
        self.ledger[key] = ReplayEntry(reply, LiveGrant() if live else self.clock.now(), reservation)
        self.persist()
        return [reply]

    def _deliver(self, waiting: WaitingAdmission, reply: wire.Reply) -> None:
        if isinstance(waiting.request, ServiceEstimatedAdmission):
            reply = self._runner_reply(reply)
        episode = waiting.request.episode
        if episode in self._pending_replies:
            self._pending_replies[episode].append(reply)
        if isinstance(waiting.connection, Connection):
            self._send(waiting.connection, reply)

    def _send(self, connection: Connection, reply: wire.Reply) -> None:
        if connection.finished:
            return
        connection.sending += self.reply_bytes(reply, version=connection.version)
        connection.finished = not isinstance(reply, (wire.Wait, wire.BuildHistoryReply))
        if connection.finished:
            self._drop_deferred_requests(connection)
        self.epoll.modify(connection.socket.fileno(), select.EPOLLOUT if connection.finished
                          else select.EPOLLIN | select.EPOLLOUT)

    def start(self) -> None:
        if not isinstance(self.listener, NoConnection):
            return
        inherited = int(os.environ.get("LISTEN_FDS") or "0")
        if inherited and int(os.environ.get("LISTEN_PID", str(os.getpid()))) == os.getpid():
            listener = socket.socket(fileno=3)
        else:
            self.socket_path.parent.mkdir(parents=True, exist_ok=True)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            if self.socket_path.exists():
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.setblocking(False)
                    try:
                        probe.connect(str(self.socket_path))
                    except OSError as error:
                        if error.errno not in (errno.ECONNREFUSED, errno.ENOENT):
                            listener.close()
                            raise RuntimeError(f"cannot establish whether admission socket is live: {error}") from error
                    else:
                        listener.close()
                        raise RuntimeError(f"admission server already listening at {self.socket_path}")
                self.socket_path.unlink(missing_ok=True)
            previous_umask = os.umask(0o177)
            try:
                listener.bind(str(self.socket_path))
            except BaseException:
                listener.close()
                raise
            finally:
                _ = os.umask(previous_umask)
            listener.listen()
        listener.setblocking(False)
        self.listener = listener
        self.epoll.register(listener.fileno(), select.EPOLLIN)
        if isinstance(self.last_usable_configuration, records.MemoryConfig):
            self._arm_override_reconnect(self.last_usable_configuration)
        self.listener_started_at = self.clock.monotonic()
        _notify("READY=1")

    def _drop_deferred_requests(self, connection: Connection) -> None:
        self.deferred_requests = [pending for pending in self.deferred_requests
                                  if pending.connection is not connection]

    def _disconnect(self, connection: Connection) -> None:
        self._drop_deferred_requests(connection)
        descriptor = connection.socket.fileno()
        if descriptor < 0:
            return
        self.epoll.unregister(descriptor)
        del self.connections[descriptor]
        connection.socket.close()
        if self._detach_admission(connection):
            _ = self.run_pass()

    def _detach_admission(self, connection: Connection) -> bool:
        """End this connection's eligibility before a later pass can grant it."""
        changed = False
        for episode, waiting in list(self.waiters.items()):
            if waiting.connection is connection:
                self._record_left(episode, gate.queue_life_identity(waiting.row), "cancelled")
                _ = self.places.pop(_place_key(waiting.request.owner, waiting.row["since"]), None)
                self._free_claim(records.waiter_owner(waiting.row))
                del self.waiters[episode]
                changed = True
        if changed:
            _ = self._prune_priority()
            self.persist()
        return changed

    def _refuse_connection(self, connection: Connection, reason: str) -> None:
        self._drop_deferred_requests(connection)
        _ = self._detach_admission(connection)
        connection.received = b""
        connection.sending = b""
        self._send(connection, wire.Refused(reason))

    def _record_caller_version(self, request: wire.Request, connection: Connection) -> None:
        if not request.code_version:
            return
        pid, uid, gid = struct.unpack("3i", connection.socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        del uid, gid
        if isinstance(self.system_reader, NoSystemReader):
            identity = self.proc.owner_start(pid)
        else:
            process = self.system_reader.latest.processes.get(pid)
            identity = LiveOwnerIdentity(process.start) if process is not None else OwnerProcessGone()
        if isinstance(identity, LiveOwnerIdentity):
            self.caller_versions[records.run_owner(pid, identity.start_time)] = request.code_version
            self.persist()

    def _read_request(self, connection: Connection) -> None:
        config = (self.configuration if isinstance(self.pending_configuration, NoPass)
                  else self.pending_configuration)
        last = self.last_usable_configuration
        request_limit = last.request_max_bytes if isinstance(last, records.MemoryConfig) else wire.CLEANUP_REQUEST_MAX_BYTES
        limit = max(request_limit, wire.CLEANUP_REQUEST_MAX_BYTES)
        reason = (config.reason if isinstance(config, gate.UnusableAdmissionConfiguration) and isinstance(last, NoPass)
                  else f"admission request exceeds {limit} bytes")
        if len(connection.received) > limit:
            raise ValueError(reason)
        while not connection.finished:
            chunk = connection.socket.recv(min(65536, limit + 1 - len(connection.received)))
            if not chunk:
                self._disconnect(connection)
                return
            connection.received += chunk
            if len(connection.received) > limit:
                raise ValueError(reason)
            if b"\n" in connection.received:
                line, _, tail = connection.received.partition(b"\n")
                if tail:
                    raise ValueError("one request per connection")
                connection.version, request = wire.parse_request_envelope(line)
                cleanup = isinstance(request, (wire.ReleaseRequest, wire.TokenWaitRequest))
                if len(line) + 1 > (wire.CLEANUP_REQUEST_MAX_BYTES if cleanup else request_limit):
                    raise ValueError(reason)
                if not isinstance(request, wire.BuildHistoryRequest):
                    if not isinstance(connection.request, NoRequest):
                        raise ValueError("one request for admission per connection")
                    connection.request = request
                connection.received = b""
                if (not isinstance(self.system_reader, NoSystemReader)
                        and isinstance(request, (wire.AdmitRequest, wire.ServiceEstimateRequest, wire.LaunchRequest))
                        and ((process := self.system_reader.latest.processes.get(request.owner.pid)) is None
                             or process.start != request.owner.start_time)):
                    self.deferred_requests.append(DeferredOwnerRequest(request, connection, self.clock.monotonic()))
                    self.system_reader.request_read()
                    return
                replies = self.handle_request(request, connection)
                # Admission replies were delivered as part of the pass.
                for reply in replies:
                    if isinstance(request, wire.BuildHistoryRequest) or not connection.sending:
                        self._send(connection, reply)
                return
            if len(connection.received) > request_limit and wire.noncleanup_request_started(connection.received):
                raise ValueError(f"admission request exceeds {request_limit} bytes")

    def _connection_event(self, connection: Connection, events: int) -> None:
        try:
            if events & select.EPOLLIN and not connection.finished:
                self._read_request(connection)
                if connection.socket.fileno() < 0:
                    return
            if events & select.EPOLLOUT and connection.sending:
                sent = connection.socket.send(connection.sending)
                connection.sending = connection.sending[sent:]
                if not connection.sending:
                    if connection.finished:
                        self._disconnect(connection)
                    else:
                        self.epoll.modify(connection.socket.fileno(), select.EPOLLIN)
            if events & (select.EPOLLHUP | select.EPOLLERR) and connection.socket.fileno() >= 0:
                self._disconnect(connection)
        except BlockingIOError:
            pass
        except (ValueError, TypeError) as error:
            self._refuse_connection(connection, str(error))
        except (BrokenPipeError, ConnectionResetError):
            self._disconnect(connection)

    def _source_stamps(self) -> dict[Path, tuple[int, int] | SourceMissing]:
        scripts = Path(__file__).resolve().parents[1]
        paths: set[Path] = set()
        for module in list(sys.modules.values()):
            filename = cast(object, getattr(module, "__file__", None))
            if isinstance(filename, str):
                path = Path(filename).resolve()
                if path.is_relative_to(scripts):
                    paths.add(path)
        return {path: self._stamp(path) for path in paths}

    @staticmethod
    def _stamp(path: Path) -> tuple[int, int] | SourceMissing:
        try:
            stat = path.stat()
            return stat.st_ino, stat.st_mtime_ns
        except FileNotFoundError:
            return SourceMissing()

    def _check_sources(self) -> None:
        configuration = self.last_usable_configuration
        current = {path: self._stamp(path) for path in self.sources}
        now = self.clock.monotonic()
        if current != self.sources:
            self.sources = current
            self.source_change_at = now
        elif (isinstance(configuration, records.MemoryConfig)
              and not isinstance(self.source_change_at, SourcesUnchanged)
              and now - self.source_change_at >= configuration.source_settle_seconds):
            self.persist()
            _notify("STOPPING=1")
            self.stopped = True
        self.next_source_check = (now + configuration.source_check_seconds
                                  if isinstance(configuration, records.MemoryConfig) else float("inf"))

    def _arm_config_watch(self) -> None:
        old = self.config_watch
        for parent in self.config_path.parents:
            try:
                if not parent.is_dir():
                    continue
                identity = parent.stat()
                watch = inotify.add_watch(self.config_events, parent)
            except OSError:
                continue
            self.config_watch = watch
            self.config_parent = parent
            self.config_parent_identity = identity.st_dev, identity.st_ino
            self.config_event_name = self.config_path.relative_to(parent).parts[0]
            self.next_config_watch_retry = float("inf")
            if old >= 0 and old != watch:
                inotify.remove_watch(self.config_events, old)
            return
        if old >= 0:
            covers_path = False
            try:
                identity = self.config_parent.stat()
                covers_path = (identity.st_dev, identity.st_ino) == self.config_parent_identity
            except OSError:
                pass
            if not covers_path:
                inotify.remove_watch(self.config_events, old)
                self.config_watch = -1
        interval = (self.last_usable_configuration.source_check_seconds
                    if isinstance(self.last_usable_configuration, records.MemoryConfig)
                    else wire.PROTOCOL_CLEANUP_TIMING.reconnect_seconds)
        self.next_config_watch_retry = self.clock.monotonic() + interval
        self.next_pass = min(self.next_pass, self.next_config_watch_retry)

    def _configuration_changed(self) -> bool:
        changed = False
        for event in inotify.events(self.config_events):
            if event.mask & inotify.IN_Q_OVERFLOW:
                changed = True
            elif event.watch == self.config_watch:
                changed |= bool(event.mask & inotify.WATCH_REMOVED) or not event.name or event.name == self.config_event_name
                if event.mask & inotify.WATCH_REMOVED:
                    inotify.remove_watch(self.config_events, self.config_watch)
                    self.config_watch = -1
        if changed:
            self._arm_config_watch()
        return changed

    def _check_outage_alert(self) -> None:
        configuration = self.last_usable_configuration
        if (not self.outage_alert_needs_clear or not isinstance(configuration, records.MemoryConfig)
                or self.clock.monotonic() - self.listener_started_at < configuration.silence_seconds):
            return
        try:
            self.outage_alert_needs_clear = not outage_alerts.clear("build-admission", self.clock.now())
            if self.outage_alert_needs_clear:
                print("memory admission: cannot clear outage alert; will retry", file=sys.stderr)
        except (OSError, ValueError) as error:
            print(f"memory admission: cannot clear outage alert: {error}", file=sys.stderr)

    def run_once(self, timeout: float = 0) -> None:
        self.start()
        now = self.clock.monotonic()
        delay = min(timeout, max(0, min(self.next_pass, self.next_source_check, self.next_config_watch_retry) - now))
        ready = self.epoll.poll(-1 if delay == float("inf") else delay)
        if any(descriptor == self.priority_events for descriptor, _events in ready):
            _ = os.eventfd_read(self.priority_events)
        if any(descriptor == self.reading_events for descriptor, _events in ready):
            _ = os.eventfd_read(self.reading_events)
            if not isinstance(self.system_reader, NoSystemReader):
                self.system_reader.raise_failure()
            self.next_pass = self.clock.monotonic()
            deferred, self.deferred_requests = self.deferred_requests, []
            for pending in deferred:
                connection = pending.connection
                if not connection.finished and connection.socket.fileno() >= 0:
                    if (not isinstance(self.system_reader, NoSystemReader)
                            and self.system_reader.latest.started_monotonic < pending.arrived_monotonic):
                        self.deferred_requests.append(pending)
                        continue
                    for reply in self.handle_request(pending.request, connection):
                        if not connection.sending:
                            self._send(connection, reply)
        configuration_changed = (any(descriptor == self.config_events for descriptor, _events in ready)
                                 and self._configuration_changed())
        if self.clock.monotonic() >= self.next_config_watch_retry:
            self._arm_config_watch()
            configuration_changed = True
        if configuration_changed:
            self.pending_configuration = self._read_configuration()
            self.next_pass = self.clock.monotonic()
        # Drain ready requests before any expiry pass, including requests queued
        # on a connection accepted this turn. A silent peer cannot delay expiry.
        self.receiving_requests = True
        try:
            self._receive_requests(ready)
        finally:
            self.receiving_requests = False
        for descriptor, events in ready:
            if descriptor in self.connections:
                self._connection_event(self.connections[descriptor], events & ~select.EPOLLIN)
            elif descriptor in self.watchers:
                watch = self.watchers[descriptor]
                if isinstance(watch, ScopeWatch):
                    try:
                        _ = os.lseek(descriptor, 0, os.SEEK_SET)
                        _ = os.read(descriptor, 4096)
                    except OSError as error:
                        if error.errno not in (errno.ENODEV, errno.ENOENT):
                            raise
                        self._unwatch(descriptor)
                else:
                    self._unwatch(descriptor)
                self._prune_places()
                for episode, waiting in list(self.waiters.items()):
                    if not self._live(waiting.request.owner):
                        self._free_claim(records.waiter_owner(waiting.row))
                        del self.waiters[episode]
                self._prune_watches()
                self.next_pass = self.clock.monotonic()
        self._finish_priority_roster()
        if self.clock.monotonic() >= self.next_pass:
            _ = self.run_pass()
        if self.clock.monotonic() >= self.next_source_check:
            self._check_sources()
        self._check_outage_alert()
        _notify("WATCHDOG=1")

    def _receive_requests(self, ready: list[tuple[int, int]]) -> None:
        readable = {descriptor for descriptor, events in ready if events & select.EPOLLIN}
        for descriptor, _events in ready:
            if not isinstance(self.listener, NoConnection) and descriptor == self.listener.fileno():
                while True:
                    try:
                        client = self.listener.accept()[0]
                    except BlockingIOError:
                        break
                    client.setblocking(False)
                    self.connections[client.fileno()] = Connection(client)
                    self.epoll.register(client.fileno(), select.EPOLLIN)
                    readable.add(client.fileno())
        for connection in list(self.connections.values()):
            if connection.socket.fileno() in readable and not connection.finished:
                self._connection_event(connection, select.EPOLLIN)

    def start_reader(self) -> None:
        """Start the reader when a usable configuration exists."""
        self.reader_requested = True
        if isinstance(self.system_reader, NoSystemReader) and isinstance(self.last_usable_configuration, records.MemoryConfig):
            plan = self._reading_plan(self.last_usable_configuration)
            initial = self.system_evidence
            if not isinstance(initial, SystemEvidence):
                initial = read_system(plan, self.readings, self.proc, self.clock)
            self.system_reader = OperatingSystemReader(plan, initial, self.readings, self.proc, self.clock, self.reading_events)
            for descriptor, watch in list(self.watchers.items()):
                if isinstance(watch, ScopeWatch):
                    self._unwatch(descriptor)

    def serve_forever(self) -> None:
        self.start_reader()
        watchdog = int(os.environ.get("WATCHDOG_USEC", "0"))
        timeout = watchdog / 2_000_000 if watchdog > 0 else float("inf")
        while not self.stopped:
            self.run_once(timeout)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.stopped = True
        for reader in self.host_readers.values():
            reader.close()
        if not isinstance(self.system_reader, NoSystemReader):
            self.system_reader.close()
        os.close(self.reading_events)
        os.close(self.config_events)
        os.close(self.priority_events)
        for connection in list(self.connections.values()):
            connection.socket.close()
        self.connections.clear()
        self.deferred_requests.clear()
        for descriptor in self.watchers:
            os.close(descriptor)
        self.watchers.clear()
        if not isinstance(self.listener, NoConnection):
            self.listener.close()
            if not os.environ.get("LISTEN_FDS"):
                self.socket_path.unlink(missing_ok=True)
        self.epoll.close()


# These scripts retain admission functions for the lifetime of their process.
ADMISSION_CALLERS = {
    "verify.sh": "direct/verify.sh",
    "invoke.sh": "lint/invoke.sh",
    "pre-tool-use-brp-launch-gate.sh": "hooks/pre-tool-use-brp-launch-gate.sh",
    "merge_gate.py": "production/merge_gate.py",
    "targeted_test.py": "production/targeted_test.py",
    "admission_client.py": "build_queue/admission_client.py",
}

def read_state(directory: Path) -> ServiceState:
    return wire.parse_record((directory / "state.json").read_text(), ServiceState)


def _older_caller(pid: int, args: list[str], proc: Proc, scripts: dict[str, bytes],
                  versions: dict[str, str], current_version: str) -> bool:
    """Require matching entry content and a recorded version of the loaded admission code."""
    for argument in args:
        name = Path(argument).name
        if name not in scripts:
            continue
        candidate = Path(argument)
        if not candidate.is_absolute():
            candidate = proc.path / str(pid) / "cwd" / candidate
        try:
            if candidate.read_bytes() != scripts[name]:
                return True
        except OSError:
            return True
        identity = proc.owner_start(pid)
        if not isinstance(identity, LiveOwnerIdentity):
            return True
        version = versions.get(records.run_owner(pid, identity.start_time), "")
        if not version or not current_version or version != current_version:
            return True
    return False

def drain_check(directory: Path, *, service: bool, include_gate_callers: bool, proc: Proc) -> int:
    """Read admission owners and callers without changing admission state."""
    waiting: list[str] = []
    reserved: list[str] = []
    owners: list[wire.Owner] = []
    versions: dict[str, str] = {}
    if service:
        try:
            state = read_state(directory)
        except FileNotFoundError:
            state = ServiceState({}, {}, {}, records.ReadyToStart(), records.NoHeadAdmissionOverride())
        versions = state.caller_versions
        rows = [(record.reservation_id, reservation_row(record)) for record in state.reservations.values()]
        owners = [place.owner for place in state.places.values()
                  if place.episode not in state.reservations]
    else:
        rows = [(path.name, row) for path, row in gate.reservation_rows(directory)]
        owners = []
        for path in directory.glob("*.waiting"):
            row = gate.read_waiter(path)
            owners.append(wire.Owner(row["pid"], row["start_time"]))
    for owner in owners:
        if proc.owner_start(owner.pid) == LiveOwnerIdentity(owner.start_time):
            waiting.append(records.run_owner(owner.pid, owner.start_time))
    for name, row in rows:
        live = proc.owner_start(row["pid"]) == LiveOwnerIdentity(row["start_time"])
        if not isinstance(proc.lifetime(row, live), gate.EndedStep):
            reserved.append(name)
    clients: list[str] = []
    names: set[str] = set(ADMISSION_CALLERS) if not service or include_gate_callers else set()
    if service:
        names.add("admission_client.py")
    scripts: dict[str, bytes] = {}
    root = Path(__file__).resolve().parents[1]
    for name in names:
        try:
            scripts[name] = (root / ADMISSION_CALLERS[name]).read_bytes()
        except OSError:
            scripts[name] = b""
    current_version = admission_code.admission_code_version(root)
    for pid in proc.processes():
        if pid == os.getpid():
            continue
        try:
            args = (proc.path / str(pid) / "cmdline").read_bytes().decode().split("\0")
        except (OSError, UnicodeError):
            continue
        if _older_caller(pid, args, proc, scripts, versions, current_version):
            clients.append(str(pid))
    if not any((waiting, reserved, clients)):
        print("drained")
        return 0
    for label, members in (("waiters", waiting), ("reservations", reserved), ("clients", clients)):
        print(f"{label}: {len(members)}" + (": " + ", ".join(members[:5]) if members else ""))
    return 1


def export_priority(directory: Path) -> None:
    """Export the drained priority and retire the service state before rollback."""
    if drain_check(directory, service=True, include_gate_callers=True, proc=SystemProc()):
        raise ValueError("service is not drained; priority was not exported")
    try:
        state = read_state(directory)
    except FileNotFoundError:
        return
    def persist() -> None:
        write_text_atomic(directory / "state.json", wire.encode_record(state))
    def advance_generation() -> None:
        nonlocal state
        state = replace(state, build_history_generation=state.build_history_generation + 1)
        persist()
    for episode, record in list(state.reservations.items()):
        outcome, status, end_kind = finalization_outcome(record.ending)
        try:
            _ = gate.finalize_reservation(Path(record.path), reservation_row(record), outcome,
                datetime.now(UTC), status, intents=StateIntents(state.reservations,
                    advance_generation, episode, persist, _no_fault, state.places),
                end_kind=end_kind)
        except Exception as error:
            raise ValueError(f"cannot finalize reservation {record.reservation_id}: {error}; service state kept") from error
    path = gate.queue_priority_path(directory)
    if isinstance(state.priority, records.NoQueuePriority):
        path.unlink(missing_ok=True)
    else:
        # The legacy reader uses the same domain codec without an envelope.
        write_text_atomic(path, wire.encode_record(state.priority))
    (directory / "state.json").unlink()


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=("serve", "drain-check", "export-priority"))
    default_state_dir = store.root() / "admission"
    _ = parser.add_argument("--state-dir", type=Path, default=default_state_dir)
    _ = parser.add_argument("--service", action="store_true")
    class Arguments(argparse.Namespace):
        command: str = ""
        service: bool = False
        # argparse skips a default when the namespace already has the attribute.
        state_dir: Path = default_state_dir
    args = parser.parse_args(namespace=Arguments())
    if args.command == "drain-check":
        return drain_check(args.state_dir, service=args.service,
                           include_gate_callers=args.state_dir == default_state_dir, proc=SystemProc())
    if args.command == "export-priority":
        export_priority(args.state_dir)
        return 0
    server = AdmissionServer(args.state_dir, SystemClock(), SystemReadings(), SystemProc())
    try:
        server.serve_forever()
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
