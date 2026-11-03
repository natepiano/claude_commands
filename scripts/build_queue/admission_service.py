"""The single admission loop owns reservations and durable queue state."""
from __future__ import annotations

import argparse
from collections.abc import Mapping
import errno
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
import os
from pathlib import Path
import select
import socket
import sys
import time
import threading
from queue import SimpleQueue, Empty
from typing import Callable, Protocol, cast

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.build_queue import admission_protocol as wire
from scripts.build_queue import queue_snapshot
from scripts.build_queue.queue_view import reason_phrase
from scripts.lint import memory_admit as gate
from scripts.build_queue.scope_activity import LiveBuildRun, ScopeActivityReader, ScopeProcesses
from scripts.lint.build_shape import build_shape, recorded_build_shape
from scripts.lib.atomic_file import write_text_atomic
from scripts.accounts import outage_alerts


class Clock(Protocol):
    def now(self) -> float: ...
    def monotonic(self) -> float: ...


class SystemClock:
    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class Readings(Protocol):
    def host(self) -> gate.HostMemory | gate.ReadingUnavailable: ...
    def pressure(self, host: gate.HostMemory, policy: gate.AdmissionPolicy) -> gate.Pressure: ...
    def compilers(self) -> list[gate.StepMemory]: ...
    def reservation(self, path: Path, row: gate.Reservation, now: datetime,
                    processes: dict[int, gate.ProcessIdentity], proc: Path, policy: gate.AdmissionPolicy) -> gate.StepMemory: ...


class SystemReadings:
    def host(self) -> gate.HostMemory | gate.ReadingUnavailable:
        try:
            return gate.read_meminfo(Path(os.environ.get("BUILDLOG_MEMINFO", "/proc/meminfo")))
        except (OSError, ValueError) as error:
            return gate.ReadingUnavailable(str(error) or "memory reading unavailable")

    def pressure(self, host: gate.HostMemory, policy: gate.AdmissionPolicy) -> gate.Pressure:
        return gate.read_pressure(host, policy=policy)

    def compilers(self) -> list[gate.StepMemory]:
        return gate.sccache_memory()

    def reservation(self, path: Path, row: gate.Reservation, now: datetime,
                    processes: dict[int, gate.ProcessIdentity], proc: Path, policy: gate.AdmissionPolicy) -> gate.StepMemory:
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
    def lifetime(self, row: gate.Reservation, owner_live: bool
                 ) -> gate.PopulatedStepScope | gate.LiveOwnerProcess | gate.EndedStep: ...


class SystemProc:
    @property
    def path(self) -> Path:
        return Path(os.environ.get("BUILDLOG_PROC", "/proc"))

    def processes(self) -> dict[int, gate.ProcessIdentity]:
        return gate.process_table(self.path)

    def owner_start(self, pid: int) -> LiveOwnerIdentity | OwnerProcessGone:
        start = gate.process_start_time(pid)
        return LiveOwnerIdentity(start) if start is not None else OwnerProcessGone()

    def pidfd(self, pid: int) -> int:
        return os.pidfd_open(pid)

    def lifetime(self, row: gate.Reservation, owner_live: bool
                 ) -> gate.PopulatedStepScope | gate.LiveOwnerProcess | gate.EndedStep:
        return gate.reservation_lifetime(row, owner_live)


@dataclass(frozen=True)
class OrderNotStarted:
    pass


@dataclass(frozen=True)
class TokenNotLeft:
    pass


def matches_build_estimate(repo: str, repo_identity: str, shape: str,
                           profile: gate.StepProfile, estimate_shape: str) -> bool:
    return (gate.history_matches_repository({"repo": repo, "repo_identity": repo_identity}, profile.repository)
            and (not shape or not estimate_shape
                 or recorded_build_shape(shape) == recorded_build_shape(estimate_shape)))


@dataclass(frozen=True)
class WaiterPlace:
    owner: wire.Owner
    since: float
    episode: str
    priority_rank: int = 1
    priority_since: float | OrderNotStarted = OrderNotStarted()
    token_left_at: float | TokenNotLeft = TokenNotLeft()


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
    test_width: gate.TestWidth
    call: gate.Call
    reservation_id: str
    queue_waiter: gate.Waiter
    repo_identity: str = ""
    test_scope: gate.TestScope = "full"
    package: str = ""
    filters: tuple[str, ...] = ()
    shape: str = ""
    test_phase_need: gate.TestPhaseNeed | gate.NoTestPhaseNeed = gate.NoTestPhaseNeed("not_recorded")
    ending: NotEnding | ReleaseStatus | OwnerExited | TokenWaiting = NotEnding()
    finalization: gate.FinalizationIntent | NoFinalizationIntent = NoFinalizationIntent()
    charge_use: gate.TestPhaseChargeUse | gate.NoTestPhaseChargeUse = gate.NoTestPhaseChargeUse()
    estimate_revision: int = 0


def reservation_row(record: ReservationRecord) -> gate.Reservation:
    """Project service state into the shared reservation record."""
    row: gate.Reservation = {
        "pid": record.owner.pid, "start_time": record.owner.start_time, "need": record.need,
        "repo": record.repo, "step": record.step, "worktree": record.worktree,
        "admitted_at": record.admitted_at, "sidecar": record.sidecar,
        "test_threads": record.test_width, "call_id": record.call,
        "reservation_id": record.reservation_id, "queue_waiter": record.queue_waiter,
        "scoped": bool(record.sidecar), "test_scope": record.test_scope,
        "package": record.package, "filters": list(record.filters),
    }
    if record.repo_identity:
        row["repo_identity"] = record.repo_identity
    if record.shape:
        row["shape"] = record.shape
    if not isinstance(record.test_phase_need, gate.NoTestPhaseNeed) or record.test_phase_need.reason != "not_recorded":
        row["test_phase_need"] = gate.test_phase_need_record(record.test_phase_need)
    if not isinstance(record.finalization, NoFinalizationIntent):
        row["finalization"] = record.finalization
    if not isinstance(record.charge_use, gate.NoTestPhaseChargeUse):
        row["test_phase_charge_use"] = record.charge_use
    return row


def reservation_record(episode: str, path: str, row: gate.Reservation) -> ReservationRecord:
    """Build service state from a validated reservation record."""
    width, call = row["test_threads"], row["call_id"]
    return ReservationRecord(episode, path, wire.Owner(row["pid"], row["start_time"]), row["need"],
        row["repo"], row["step"], row["worktree"], row["admitted_at"], row["sidecar"],
        width, call,
        row.get("reservation_id", ""), row.get("queue_waiter", {
            "pid": row["pid"], "start_time": row["start_time"], "need": row["need"],
            "since": datetime.fromisoformat(row["admitted_at"]).timestamp(), **gate.reservation_identity(row)}),
        row.get("repo_identity", ""), row.get("test_scope", "full"), row.get("package", ""),
        tuple(row.get("filters", [])), row.get("shape", ""), gate.read_test_phase_need(row.get("test_phase_need")),
        finalization=row.get("finalization", NoFinalizationIntent()),
        charge_use=row.get("test_phase_charge_use", gate.NoTestPhaseChargeUse()))


@dataclass(frozen=True)
class LiveGrant:
    pass


@dataclass(frozen=True)
class ReplayEntry:
    reply: wire.Reply
    ended_at: float | LiveGrant
    reservation: str = ""


@dataclass(frozen=True)
class ServiceState:
    reservations: dict[str, ReservationRecord]
    places: dict[str, WaiterPlace]
    ledger: dict[str, ReplayEntry]
    recovery: gate.RecoverySpacing
    override: gate.HeadAdmissionOverride | gate.NoHeadAdmissionOverride
    priority: gate.QueuePriority | gate.NoQueuePriority = gate.NO_QUEUE_PRIORITY
    legacy_cleanup: tuple[str, ...] = ()
    build_history_generation: int = 0
    run_places: dict[str, wire.RunPlace] = field(default_factory=dict)


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


@dataclass
class WaitingAdmission:
    request: wire.AdmitRequest | wire.LaunchRequest
    row: gate.Waiter
    connection: Connection | NoConnection
    last_reply: wire.Wait | NoWaitReply = NoWaitReply()
    last_sent: float = float("-inf")


@dataclass(frozen=True)
class NoPass:
    pass


def _no_fault(_stage: str) -> None:
    pass


def _place_key(owner: wire.Owner, since: float) -> str:
    return f"{owner.pid}-{owner.start_time}:{since:.17g}"


def _notify(message: str) -> None:
    address = os.environ.get("NOTIFY_SOCKET", "")
    if address:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
            notifier.connect("\0" + address[1:] if address.startswith("@") else address)
            _ = notifier.send(message.encode())


@dataclass(frozen=True)
class EstimateJob:
    episode: str
    revision: int
    width: gate.TestWidth
    row: gate.Reservation
    config: gate.MemoryConfig
    now: datetime
    root: Path


@dataclass(frozen=True)
class StopEstimates:
    pass


@dataclass(frozen=True)
class EstimateFailed:
    pass


@dataclass(frozen=True)
class EstimateResult:
    episode: str
    revision: int
    width: gate.TestWidth
    need: gate.TestPhaseNeed | gate.NoTestPhaseNeed | EstimateFailed


def estimate_worker(jobs: SimpleQueue[EstimateJob | StopEstimates], results: SimpleQueue[EstimateResult],
                    descriptor: int, fault: Callable[[str], None]) -> None:
    """Resolve history outside the loop; publish only tagged results."""
    try:
        while True:
            job = jobs.get()
            if isinstance(job, StopEstimates):
                return
            try:
                fault("before_test_phase_need")
                need = gate.test_phase_need(job.row, gate.store.host_name(), job.now, job.root,
                    history_path=job.root / "anon_peaks.jsonl", config=job.config)
                result = EstimateResult(job.episode, job.revision, job.width, need)
            except Exception:
                result = EstimateResult(job.episode, job.revision, job.width, EstimateFailed())
            results.put(result)
            try:
                _ = os.write(descriptor, b"x")
            except OSError:
                return
    finally:
        os.close(descriptor)


def finalization_outcome(ending: ReleaseStatus | OwnerExited | NotEnding | TokenWaiting
                         ) -> tuple[gate.FinalOutcome, int | gate.OutcomeNotRecorded]:
    if isinstance(ending, TokenWaiting):
        return gate.Completed(), 0
    if isinstance(ending, ReleaseStatus):
        return (gate.Completed() if ending.status == 0 else
                gate.Killed() if ending.status >= 128 else gate.Failed()), ending.status
    return gate.OwnerGone(), gate.OUTCOME_NOT_RECORDED


class StateIntents:
    """Persist finalization before history; remove the entry only after history."""
    def __init__(self, reservations: dict[str, ReservationRecord],
                 advance_generation: Callable[[], None], episode: str,
                 persist: Callable[[], None], fault: Callable[[str], None]) -> None:
        self.reservations: dict[str, ReservationRecord] = reservations
        self.advance_generation: Callable[[], None] = advance_generation
        self.episode: str = episode
        self.persist: Callable[[], None] = persist
        self.fault: Callable[[str], None] = fault

    def save(self, row: gate.Reservation, intent: gate.FinalizationIntent) -> None:
        record = self.reservations[self.episode]
        if record.reservation_id != row.get("reservation_id"):
            raise ValueError("finalization reservation changed")
        self.reservations[self.episode] = replace(record, finalization=intent)
        self.persist()
        self.fault("after_finalization_intent")

    def published(self, row: gate.Reservation) -> None:
        if row["step"] == "build":
            self.advance_generation()

    def clear(self, row: gate.Reservation) -> None:
        if self.reservations[self.episode].reservation_id != row.get("reservation_id"):
            raise ValueError("finalization reservation changed")
        self.fault("after_history_append")
        del self.reservations[self.episode]
        self.persist()


class AdmissionServer:
    def __init__(self, state_dir: Path, clock: Clock, readings: Readings, proc: Proc, *,
                 fault: Callable[[str], None] = _no_fault, socket_path: Path | None = None) -> None:
        self.state_dir: Path = state_dir
        self.clock: Clock = clock
        self.readings: Readings = readings
        self.proc: Proc = proc
        self.fault: Callable[[str], None] = fault
        self.socket_path: Path = socket_path if socket_path is not None else wire.admission_socket_path()
        self.waiters: dict[str, WaitingAdmission] = {}
        self.launches: dict[str, WaitingAdmission] = {}
        self.reservations: dict[str, ReservationRecord] = {}
        self.places: dict[str, WaiterPlace] = {}
        self.run_places: dict[str, wire.RunPlace] = {}
        self.build_history_generation: int = 0
        self.ledger: dict[str, ReplayEntry] = {}
        self.recovery: gate.RecoverySpacing = gate.NoRecoverySpacing()
        self.override: gate.HeadAdmissionOverride | gate.NoHeadAdmissionOverride = gate.NoHeadAdmissionOverride()
        self.last_inputs: gate.QueueInputs | NoPass = NoPass()
        self.last_result: gate.PassResult | NoPass = NoPass()
        self.last_limits: gate.AdmissionLimits | NoPass = NoPass()
        self.last_policy: gate.AdmissionPolicy = gate.DEFAULT_ADMISSION_POLICY
        self.priority: gate.QueuePriority | gate.NoQueuePriority = gate.NO_QUEUE_PRIORITY
        self.last_fallbacks: tuple[gate.ConfigFallback, ...] = ()
        self._pending_replies: dict[str, list[wire.Reply]] = {}
        self.legacy_cleanup: tuple[str, ...] = ()
        self._persisted: str = ""
        self.override_reconnect_until: ReconnectDeadline | ReconnectAwaitingListener | NoReconnectGrace = NoReconnectGrace()
        self.receiving_requests: bool = False
        self.connections: dict[int, Connection] = {}
        self.watchers: dict[int, OwnerWatch | ScopeWatch] = {}
        self.epoll: select.epoll = select.epoll()
        self.listener: socket.socket | NoConnection = NoConnection()
        self.stopped: bool = False
        self.closed: bool = False
        self.next_pass: float = float("inf")
        self.next_source_check: float = clock.monotonic() + gate.source_check_seconds()
        self.sources: dict[Path, tuple[int, int] | SourceMissing] = self._source_stamps()
        self.source_change_at: float | SourcesUnchanged = SourcesUnchanged()
        state_dir.mkdir(parents=True, exist_ok=True)
        self.estimate_jobs: SimpleQueue[EstimateJob | StopEstimates] = SimpleQueue()
        self.estimate_results: SimpleQueue[EstimateResult] = SimpleQueue()
        self.estimate_read: int
        self.estimate_write: int
        self.estimate_read, self.estimate_write = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        self.epoll.register(self.estimate_read, select.EPOLLIN)
        self.estimate_thread: threading.Thread = threading.Thread(target=estimate_worker,
            args=(self.estimate_jobs, self.estimate_results, self.estimate_write, self.fault), daemon=True)
        self.estimate_thread.start()
        self._restore()
        _ = self.run_pass()

    def _finish_priority(self) -> None:
        if isinstance(self.priority, gate.NoQueuePriority):
            return
        transition = self.priority.get("transition")
        if transition is None:
            return
        gate.finish_priority_transition(self.priority)
        if transition["action"] == "clear":
            self.priority = gate.NO_QUEUE_PRIORITY
        else:
            del self.priority["transition"]
        self.persist()

    def _import_legacy(self) -> None:
        """Import once, before a state file exists; remove files only after persistence."""
        imported: list[Path] = []
        token_intents = {path: gate.read_token_wait(path) for path in self.state_dir.glob("*.token-wait")}
        for path, row in gate.reservation_rows(self.state_dir):
            stem = path.with_suffix("")
            row["reservation_id"] = row.get("reservation_id") or stem.name
            evidence = gate.read_test_phase_charge_use(path, row["reservation_id"])
            if not isinstance(evidence, gate.NoTestPhaseChargeUse):
                row["test_phase_charge_use"] = evidence
            episode = row["reservation_id"]
            current = reservation_record(episode, str(stem), row)
            intent = next((value for value in token_intents.values()
                           if value["reservation"] == str(path)), None)
            if intent is not None:
                current = replace(current, queue_waiter=intent["waiter"],
                    ending=TokenWaiting("import-" + episode, intent["waiter"].get("token_left_at", self.clock.now())))
            self.reservations[episode] = current
            imported.append(path)
            evidence_path = path.with_suffix(".test-phase-charge.json")
            if evidence_path.exists():
                imported.append(evidence_path)
        for path in self.state_dir.glob("*.waiting"):
            row = gate.read_waiter(path)
            owner = wire.Owner(row["pid"], row["start_time"])
            self.places[_place_key(owner, row["since"])] = WaiterPlace(owner, row["since"], path.stem,
                1, row.get("priority_since", OrderNotStarted()), row.get("token_left_at", TokenNotLeft()))
            imported.append(path)
        for path, intent in token_intents.items():
            row = intent["waiter"]
            owner = wire.Owner(row["pid"], row["start_time"])
            self.places[_place_key(owner, row["since"])] = WaiterPlace(owner, row["since"], path.stem,
                1, row.get("priority_since", OrderNotStarted()), row.get("token_left_at", TokenNotLeft()))
            imported.append(path)
        self.priority = gate.load_queue_priority(self.state_dir)
        self.recovery = gate.read_recovery(self.state_dir)
        self.override = gate.read_head_override(self.state_dir)
        if not isinstance(self.override, gate.NoHeadAdmissionOverride):
            self.override_reconnect_until = ReconnectAwaitingListener()
        for name in ("queue_priority.json", "pressure-recovery", gate.head_override_path(self.state_dir).name):
            path = self.state_dir / name
            if path.exists():
                imported.append(path)
        self.legacy_cleanup = tuple(str(path) for path in imported)
        if not isinstance(self.override, gate.NoHeadAdmissionOverride):
            self.override["waiter"] = Path(self.override["waiter"]).stem
        self.persist()
        self.fault("after_import_state")
        self._finish_import()

    def _finish_import(self) -> None:
        for name in self.legacy_cleanup:
            Path(name).unlink(missing_ok=True)
        if self.legacy_cleanup:
            self.legacy_cleanup = ()
            self.persist()
        self.fault("after_import_cleanup")

    def _restore(self) -> None:
        path = self.state_dir / "state.json"
        if path.exists():
            state = read_state(self.state_dir)
            self.reservations, self.places, self.ledger = state.reservations, state.places, state.ledger
            self.recovery, self.override, self.priority = state.recovery, state.override, state.priority
            self.legacy_cleanup = state.legacy_cleanup
            self.build_history_generation = state.build_history_generation
            self.run_places = state.run_places
            self._persisted = wire.encode_record(state)
            self._finish_import()
            if not isinstance(self.override, gate.NoHeadAdmissionOverride):
                self.override_reconnect_until = ReconnectAwaitingListener()
        else:
            self._import_legacy()
        for episode, record in list(self.reservations.items()):
            row = reservation_row(record)
            live = self._live(record.owner)
            lifetime = self.proc.lifetime(row, live)
            if "finalization" in row or not isinstance(record.ending, NotEnding) or isinstance(lifetime, gate.EndedStep):
                try:
                    self._end(episode, record.ending if not isinstance(record.ending, NotEnding) else OwnerExited())
                except OSError:
                    # The saved intent blocks matching estimates until history publication succeeds.
                    pass
            else:
                self._watch_owner(record.owner)
                self._watch_scope(lifetime, episode)
        self._prune_places()
        self._finish_priority()

    def persist(self, config: gate.MemoryConfig | gate.ReadMemoryConfiguration = gate.READ_MEMORY_CONFIGURATION) -> None:
        now = self.clock.now()
        resolved = gate.read_memory_config(report=False) if isinstance(config, gate.ReadMemoryConfiguration) else config
        live_reservations = {record.reservation_id for record in self.reservations.values()}
        self.ledger = {key: replace(entry, ended_at=now)
                       if isinstance(entry.ended_at, LiveGrant)
                       and key not in self.reservations and entry.reservation not in live_reservations
                       else entry for key, entry in self.ledger.items()}
        self.ledger = {key: entry for key, entry in self.ledger.items()
                       if isinstance(entry.ended_at, LiveGrant) or now - entry.ended_at < resolved.replay_retention_seconds}
        encoded = wire.encode_record(ServiceState(
            self.reservations, self.places, self.ledger, self.recovery, self.override, self.priority,
            self.legacy_cleanup, self.build_history_generation, self.run_places))
        if encoded == self._persisted:
            return
        write_text_atomic(self.state_dir / "state.json", encoded)
        self._persisted = encoded
        self.fault("after_state_write")

    def _unwatch(self, descriptor: int) -> None:
        _ = self.watchers.pop(descriptor, None)
        try:
            self.epoll.unregister(descriptor)
        except (OSError, FileNotFoundError):
            pass
        os.close(descriptor)

    def _watch_owner(self, owner: wire.Owner) -> None:
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

    def _watch_scope(self, lifetime: gate.PopulatedStepScope | gate.LiveOwnerProcess | gate.EndedStep,
                     episode: str) -> None:
        if not isinstance(lifetime, gate.PopulatedStepScope):
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
        owners.update(waiting.request.owner for waiting in (*self.waiters.values(), *self.launches.values()))
        owners.update(place.owner for place in self.places.values())
        owners.update(wire.Owner(place.run.pid, place.run.start_time) for place in self.run_places.values())
        for descriptor, watch in list(self.watchers.items()):
            if (isinstance(watch, OwnerWatch) and watch.owner not in owners
                    or isinstance(watch, ScopeWatch) and watch.episode not in self.reservations):
                self._unwatch(descriptor)

    def _ordered_waiters(self) -> list[tuple[Path, gate.Waiter]]:
        rows: list[tuple[Path, gate.Waiter]] = []
        for waiting in self.waiters.values():
            row = waiting.row.copy()
            row["order_since"] = row["since"]
            if isinstance(waiting.request, wire.AdmitRequest) and isinstance(waiting.request.run, wire.RunIdentity):
                place = self.run_places.get(self._run_key(waiting.request.run))
                if place is not None:
                    row["order_since"] = min(place.since, row["since"])
            rows.append((self.state_dir / gate.waiter_owner(row), row))
        return sorted(rows, key=lambda item: gate.waiter_order(item[1]))

    def _queue_order(self) -> tuple[list[tuple[Path, gate.Waiter]], gate.QueuePriority | gate.NoQueuePriority]:
        labels: Mapping[str, gate.UnitMembership] = (
            gate.priority_labels([waiting.row.get("session_id", "") for waiting in self.waiters.values()])
            if not isinstance(self.priority, gate.NoQueuePriority) else {})
        return gate.apply_queue_priority(self._ordered_waiters(), gate.QueueOrderContext(self.priority, labels))

    def _override_pending_reconnect(self) -> bool:
        if isinstance(self.override_reconnect_until, NoReconnectGrace):
            return False
        if (isinstance(self.override, gate.NoHeadAdmissionOverride)
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
        for key, place in list(self.run_places.items()):
            owner = wire.Owner(place.run.pid, place.run.start_time)
            if self._live(owner):
                self._watch_owner(owner)
            else:
                del self.run_places[key]

    @staticmethod
    def _run_key(run: wire.RunIdentity) -> str:
        return f"{run.pid}-{run.start_time}"

    def _run_place(self, request: wire.AdmitRequest | wire.LaunchRequest, place: WaiterPlace) -> None:
        if not isinstance(request, wire.AdmitRequest) or not isinstance(request.run, wire.RunIdentity):
            return
        run = request.run
        key = self._run_key(run)
        if key not in self.run_places and self._live(wire.Owner(run.pid, run.start_time)):
            self.run_places[key] = wire.RunPlace(run, place.since, request.owner)

    def _live(self, owner: wire.Owner) -> bool:
        return self.proc.owner_start(owner.pid) == LiveOwnerIdentity(owner.start_time)

    def _prune_places(self) -> None:
        self.places = {key: place for key, place in self.places.items() if self._live(place.owner)}

    def _row(self, request: wire.AdmitRequest | wire.LaunchRequest, place: WaiterPlace) -> gate.Waiter:
        profile, identity = request.profile, request.identity
        row: gate.Waiter = {
            "pid": request.owner.pid, "start_time": request.owner.start_time,
            "need": profile.peak.bytes, "since": place.since, "priority_rank": place.priority_rank,
            "session_id": identity.session_id, "session_name": identity.name,
            "step": identity.step, "folder": identity.folder,
            "estimated_at": profile.estimated_at or self.clock.now(),
            "fresh_test": isinstance(request, wire.AdmitRequest) and request.fresh_test,
            "need_source": profile.peak.source, "need_runs": profile.peak.count,
            "need_basis": gate.need_basis_record(profile.peak.basis), "test_scope": profile.scope,
        }
        if not isinstance(place.priority_since, OrderNotStarted):
            row["priority_since"] = place.priority_since
        if not isinstance(place.token_left_at, TokenNotLeft):
            row["token_left_at"] = place.token_left_at
        if isinstance(profile.repository, gate.RepositoryIdentity):
            row["repo_identity"] = profile.repository.key
            row["repository_name"] = profile.repository.name
            row["legacy_name"] = profile.repository.legacy_name
        if isinstance(request, wire.AdmitRequest):
            row["package"] = gate.test_package(list(request.argv))
            row["filters"] = list(gate.test_filters(list(request.argv)))
            if isinstance(request.test_width, gate.RequestedTestWidth):
                row["test_threads"] = request.test_width.threads
        return row

    def _advance_build_history(self) -> None:
        self.build_history_generation += 1
        self.persist()

    def _build_estimate_expired(self, request: wire.AdmitRequest | wire.LaunchRequest) -> bool:
        profile = request.profile
        if not isinstance(profile, gate.StepProfile) or profile.step != "build":
            return False
        generation = profile.build_history_generation
        if not isinstance(generation, gate.BuildHistoryGeneration) or generation.value != self.build_history_generation:
            return True
        shape = build_shape(list(request.argv)).key if isinstance(request, wire.AdmitRequest) else ""
        return any(record.step == "build" and isinstance(record.finalization, dict)
                   and matches_build_estimate(record.repo, record.repo_identity, record.shape, profile, shape)
                   for record in self.reservations.values())

    def _inputs(self, config: gate.MemoryConfig) -> tuple[gate.QueueInputs, gate.AdmissionLimits]:
        now = self.clock.now()
        policy = config.policy
        lifetimes = {episode: self.proc.lifetime(reservation_row(record),
            self._live(record.owner))
            for episode, record in self.reservations.items()}
        processes = self.proc.processes()
        activity = ScopeActivityReader(processes, self.proc.path,
            tuple(LiveBuildRun(place.run.pid, place.run.start_time, place.since)
                  for place in self.run_places.values()))
        memories: list[tuple[gate.Reservation, gate.StepMemory]] = []
        for episode, record in list(self.reservations.items()):
            if isinstance(record.finalization, dict) or not isinstance(record.ending, NotEnding):
                try:
                    self._end(episode, record.ending)
                except OSError:
                    pass
                if episode not in self.reservations:
                    continue
            row = reservation_row(record)
            row["build_run"] = activity.run(record.owner.pid)
            memory = (gate.GoneMemory() if isinstance(lifetimes[episode], gate.EndedStep) else
                      self.readings.reservation(Path(record.path), row, datetime.fromtimestamp(now, UTC),
                                                processes, self.proc.path, policy))
            if isinstance(memory, gate.GoneMemory):
                try:
                    self._end(episode, record.ending if not isinstance(record.ending, NotEnding) else OwnerExited())
                except OSError:
                    # Keep the intent for the next pass.
                    pass
                continue
            memories.append((row, memory))
            self._watch_scope(lifetimes[episode], episode)
        for waiting in (*self.waiters.values(), *self.launches.values()):
            waiting.row["build_history_current"] = not self._build_estimate_expired(waiting.request)
        host = self.readings.host()
        pressure = self.readings.pressure(host if isinstance(host, gate.HostMemory) else
                                         gate.HostMemory(0, gate.UnknownHostTotal()), policy)
        readings = gate.QueueSnapshotReadings(processes, {}, host, pressure, self.recovery,
                                             self.readings.compilers(), policy=policy)
        ordered, priority = self._queue_order()
        unowned = gate.unowned_slice_memory([item[0] for item in memories], processes, self.proc.path,
                                           activity_reader=activity)
        inputs = gate.build_queue_inputs(ordered, memories, readings, self.override, now, priority, unowned)
        represented = {(step.run.pid, step.run.start_time) for step in inputs.running
                       if isinstance(step.run, LiveBuildRun)}
        between: list[gate.RunningStep] = []
        for place in self.run_places.values():
            run = place.run
            if (run.pid, run.start_time) in represented:
                continue
            identity = processes.get(run.pid)
            if identity is None or identity.start != run.start_time:
                continue
            evidence = activity.scope({run.pid})
            if not isinstance(evidence, ScopeProcesses):
                continue
            live = LiveBuildRun(run.pid, run.start_time, activity.started_at(run.pid) or place.since)
            measured = gate.tree_memory(run.pid, run.start_time, processes, self.proc.path)
            if isinstance(measured, gate.GoneMemory):
                continue
            memory = gate.UnownedSliceMemory(measured.bytes, swap_bytes=measured.swap_bytes,
                                             activity=replace(evidence, run=live))
            between.append(gate.RunningStep(evidence.session_id, evidence.session_name, "between steps",
                                           evidence.folder, 0, memory, live, live.started_at))
        if between:
            inputs = replace(inputs, running=(*inputs.running, *between))
        limits = gate.resolve_admission_limits(host.total if isinstance(host, gate.HostMemory)
                                             else gate.UnknownHostTotal(), config)
        return inputs, limits

    def run_pass(self) -> gate.PassResult:
        self._prune_runs()
        config = gate.read_memory_config(report=False)
        policy = config.policy
        self.last_fallbacks = config.fallbacks
        inputs, limits = self._inputs(config)
        if self.launches and (self.waiters or self.reservations or isinstance(inputs.pressure, gate.MemoryPressure)):
            for episode, waiting in list(self.launches.items()):
                self.waiters[episode] = waiting
                self._save_place(waiting)
                del self.launches[episode]
            ordered, priority = self._queue_order()
            inputs = replace(inputs, ordered=ordered, priority=priority)
        preserve_override = self._override_pending_reconnect()
        if preserve_override:
            inputs = replace(inputs, override=gate.NoHeadAdmissionOverride())
        result = gate.admission_pass(inputs, inputs.now, limits, policy)
        self.last_inputs, self.last_limits, self.last_result, self.last_policy = inputs, limits, result, policy
        # Evidence is an effect of this completed pass, never an input to it.
        for record, step in zip(inputs.reservations, inputs.running):
            matching = next((item for item in self.reservations.values()
                             if item.reservation_id == record.get("reservation_id")), None)
            if matching is not None:
                self._record_charge_use(matching, gate.charge_basis(step.measured), inputs.now, config)
        for mark in result.head_marks:
            for waiting in self.waiters.values():
                if gate.waiter_owner(waiting.row) == mark.path.name:
                    waiting.row = mark.waiter.copy()
                    _ = waiting.row.pop("order_since", None)
                    self._save_place(waiting)
        by_owner = {gate.waiter_owner(waiting.row): episode for episode, waiting in self.waiters.items()}
        starts = sorted((row for row in result.rows if isinstance(row.admission, gate.ProposedAdmission)),
                        key=lambda row: cast(gate.ProposedAdmission, row.admission).rank)
        if not starts:
            self._effects(result.recovery, result.override, result.totals, preserve_override)
        for decision in starts:
            episode = by_owner[decision.owner]
            waiting = self.waiters[episode]
            proposed = cast(gate.ProposedAdmission, decision.admission)
            self._effects(proposed.recovery, proposed.override, decision.decision, preserve_override)
            self._admit(waiting, config)
        for episode, waiting in list(self.launches.items()):
            request = waiting.request
            if self._build_estimate_expired(request):
                expired = gate.EstimateExpired(waiting.row.get("estimated_at", inputs.now))
                reply_launch: wire.Reply = wire.Wait(expired, 0, "", reason_phrase(expired, ()),
                    self._wait_snapshot(waiting, result.totals, inputs, config, expired))
            elif not isinstance(inputs.host, gate.HostMemory):
                reply_launch = wire.Wait(inputs.host, 0, "", inputs.host.reason,
                    self._wait_snapshot(waiting, result.totals, inputs, config, inputs.host))
            else:
                decision_launch = gate.decide(inputs.host.available, inputs.host.total, request.profile.peak.bytes,
                    [step.measured for step in inputs.running], inputs.pressure, limits=limits)
                if decision_launch.state == "admit":
                    reply_launch = wire.LaunchAdmitted(request.profile.peak.bytes, request.profile.peak.source)
                    self.ledger[episode] = ReplayEntry(reply_launch, self.clock.now())
                    self.persist(config)
                else:
                    reason = gate.queue_wait_reason(decision_launch, request.profile.peak.bytes,
                                                    inputs.host.available, gate.RecordedQueueIdentities())
                    reply_launch = wire.Wait(reason, 0, "", reason_phrase(reason, ()),
                        self._wait_snapshot(waiting, decision_launch, inputs, config, reason))
            if waiting.last_reply != reply_launch or inputs.now - waiting.last_sent >= config.wait_reply_seconds:
                self._deliver(waiting, reply_launch)
                if isinstance(reply_launch, wire.Wait):
                    waiting.last_reply, waiting.last_sent = reply_launch, inputs.now
            if isinstance(reply_launch, wire.LaunchAdmitted):
                del self.launches[episode]
        self.persist(config)
        waiting_steps = tuple(gate.WaitingStep(row["pid"], row.get("session_id", ""), row.get("session_name", ""),
            row.get("step", ""), row.get("folder", ""), row["since"], row["need"], decision.reason)
            for (_, row), decision in zip(inputs.ordered, result.rows, strict=True))
        for position, decision in enumerate(result.rows, 1):
            episode = by_owner[decision.owner]
            if episode in self.waiters:
                head = inputs.ordered[0][1].get("session_name", "") if inputs.ordered else ""
                reply = wire.Wait(decision.reason, position, head, reason_phrase(decision.reason, waiting_steps),
                    self._wait_snapshot(self.waiters[episode], decision.decision, inputs, config, decision.reason))
                waiting = self.waiters[episode]
                if waiting.last_reply != reply or inputs.now - waiting.last_sent >= config.wait_reply_seconds:
                    self._deliver(waiting, reply)
                    waiting.last_reply, waiting.last_sent = reply, inputs.now
        interval = config.waiting_pass_seconds if self.waiters or self.launches else config.running_pass_seconds
        self.next_pass = self.clock.monotonic() + interval
        if isinstance(self.override_reconnect_until, ReconnectDeadline):
            self.next_pass = min(self.next_pass, self.override_reconnect_until.at)
        if starts:
            self.next_pass = self.clock.monotonic()
        try:
            ordered, priority = self._queue_order()
            queue_snapshot.write_queue_snapshot(self.state_dir,
                replace(inputs, ordered=ordered, recovery=self.recovery, override=self.override, priority=priority), policy)
        except OSError:
            # An old snapshot expires without changing the admission result.
            pass
        return result

    def _effects(self, recovery: gate.RecoverySpacing, override: gate.OverrideKept | gate.OverrideEnded,
                 decision: gate.AdmissionDecision, preserve_override: bool) -> None:
        self.recovery = recovery
        if preserve_override:
            return
        if isinstance(override, gate.OverrideEnded):
            available = (self.last_inputs.host.available if isinstance(self.last_inputs, gate.QueueInputs)
                         and isinstance(self.last_inputs.host, gate.HostMemory) else 0)
            gate.head_override_event(override.action, override.record, override.record["need"],
                available, decision.reserve, self.clock.now())
            self.override = gate.NoHeadAdmissionOverride()
        else:
            self.override = override.record

    def _save_place(self, waiting: WaitingAdmission) -> None:
        row, request = waiting.row, waiting.request
        key = _place_key(request.owner, row["since"])
        self.places[key] = WaiterPlace(request.owner, row["since"], request.episode,
            row.get("priority_rank", 1), row.get("priority_since", OrderNotStarted()),
            row.get("token_left_at", TokenNotLeft()))

    def _queue_estimate(self, record: ReservationRecord, config: gate.MemoryConfig) -> None:
        self.estimate_jobs.put(EstimateJob(record.episode, record.estimate_revision, record.test_width,
            reservation_row(record), config, datetime.fromtimestamp(self.clock.now(), UTC), self.state_dir))

    def _accept_estimates(self) -> None:
        try:
            _ = os.read(self.estimate_read, 65536)
        except BlockingIOError:
            pass
        while True:
            try:
                result = self.estimate_results.get_nowait()
            except Empty:
                break
            current = self.reservations.get(result.episode)
            if (current is not None and current.estimate_revision == result.revision
                    and current.test_width == result.width and not isinstance(result.need, EstimateFailed)):
                self.reservations[result.episode] = replace(current, test_phase_need=result.need)
        self.persist()
        _ = self.run_pass()

    def _record_charge_use(self, record: ReservationRecord, basis: gate.ChargeBasis,
                           now: float, config: gate.MemoryConfig) -> None:
        if not isinstance(basis, gate.TestPhaseCharge) or not isinstance(record.charge_use, gate.NoTestPhaseChargeUse):
            return
        evidence: gate.TestPhaseChargeUse = {"bytes": basis.bytes, "selected_at": now,
            "reservation_id": record.reservation_id, "need": gate.test_phase_need_record(basis.need)}
        self.reservations[record.episode] = replace(record, charge_use=evidence)
        try:
            self.persist(config)
        except Exception:
            self.reservations[record.episode] = record

    def _wait_snapshot(self, waiting: WaitingAdmission, decision: gate.AdmissionDecision,
                       inputs: gate.QueueInputs, config: gate.MemoryConfig,
                       reason: gate.WaitReason) -> gate.AdmissionWaitSnapshot:
        head_need = inputs.ordered[0][1]["need"] if inputs.ordered else 0
        head_label = inputs.ordered[0][1].get("session_name", "") if inputs.ordered else ""
        return gate.AdmissionWaitSnapshot(decision, waiting.request.profile.peak, inputs.running,
            inputs.host.available if isinstance(inputs.host, gate.HostMemory) else 0,
            waiting.request.profile.repo, waiting.request.profile.step, config.policy, config.fallbacks,
            head_need if not isinstance(decision.order_hold, gate.NoOrderHold) else 0, head_label, reason)

    def _admit(self, waiting: WaitingAdmission, config: gate.MemoryConfig) -> None:
        request, profile = waiting.request, waiting.request.profile
        if isinstance(request, wire.LaunchRequest):
            reply: wire.Reply = wire.LaunchAdmitted(profile.peak.bytes, profile.peak.source)
            self.ledger[request.episode] = ReplayEntry(reply, self.clock.now())
        else:
            stem = gate.reservation_stem(self.state_dir, request.owner.pid, request.owner.start_time, int(self.clock.now() * 1_000_000_000))
            # The episode disambiguates grants at the same clock tick after release.
            while any(record.path == str(stem) for record in self.reservations.values()):
                stem = stem.with_name(stem.name + "0")
            record = ReservationRecord(request.episode, str(stem), request.owner, profile.peak.bytes,
                profile.repo, profile.step, profile.worktree, gate.store.utc_iso(self.clock.now()), request.sidecar,
                request.test_width, request.call, stem.name, gate.stored_waiter(waiting.row),
                profile.repository.key if isinstance(profile.repository, gate.RepositoryIdentity) else "",
                profile.scope, gate.test_package(list(request.argv)), tuple(gate.test_filters(list(request.argv))),
                build_shape(list(request.argv)).key if profile.step == "build" else "")
            self.reservations[request.episode] = record
            reply = wire.Admitted(stem.name, record.need, profile.peak.source, str(stem.with_suffix(".max")),
                                  str(stem.with_suffix(".wave")), gate.sampling_context(reservation_row(record)))
            self.ledger[request.episode] = ReplayEntry(reply, LiveGrant())
            self._watch_owner(request.owner)
            if record.step == "nextest":
                self._queue_estimate(record, config)
        self._save_place(waiting)
        # Durable grant and replay reply precede any terminal reply.
        self.persist(config)
        del self.waiters[request.episode]
        self._deliver(waiting, reply)

    def _end(self, episode: str, ending: ReleaseStatus | OwnerExited | NotEnding | TokenWaiting) -> None:
        record = self.reservations[episode]
        record = replace(record, ending=ending)
        self.reservations[episode] = record
        self.persist()
        path = Path(record.path)
        row = reservation_row(record)
        outcome, status = finalization_outcome(ending)
        if isinstance(ending, TokenWaiting):
            row_waiter = record.queue_waiter.copy()
            row_waiter["token_left_at"] = ending.left_at
            self.places[_place_key(record.owner, row_waiter["since"])] = WaiterPlace(
                record.owner, row_waiter["since"], record.episode, 1,
                row_waiter.get("priority_since", OrderNotStarted()), ending.left_at)
        _ = gate.finalize_reservation(path, row, outcome, datetime.fromtimestamp(self.clock.now(), UTC), status,
            intents=StateIntents(self.reservations, self._advance_build_history, episode, self.persist, self.fault))
        if not isinstance(ending, TokenWaiting):
            _ = self.places.pop(_place_key(record.owner, record.queue_waiter["since"]), None)
        self._prune_watches()
        for key, entry in list(self.ledger.items()):
            if entry.reservation == record.reservation_id:
                self.ledger[key] = replace(entry, ended_at=self.clock.now())
        previous = self.ledger.get(episode)
        if previous is not None:
            self.ledger[episode] = replace(previous, ended_at=self.clock.now())
        if isinstance(ending, (ReleaseStatus, TokenWaiting)):
            self.ledger[ending.request_id] = ReplayEntry(wire.Ok(), self.clock.now())
        self.persist()

    def handle_request(self, request: wire.Request, connection: Connection | NoConnection = NO_CONNECTION
                       ) -> list[wire.Reply]:
        if isinstance(request, wire.BuildHistoryRequest):
            return [wire.BuildHistoryReply(self.build_history_generation)]
        if isinstance(request, (wire.ViewRequest, wire.ExplainRequest, wire.MineRequest)):
            if not isinstance(self.last_inputs, gate.QueueInputs) or not isinstance(self.last_limits, gate.AdmissionLimits):
                raise RuntimeError("service has no pass")
            reading = gate.queue_read_from_pass(self.last_inputs, self.last_limits, self.last_policy,
                cast(gate.PassResult, self.last_result))
            reading = replace(reading, fallbacks=self.last_fallbacks)
            if isinstance(request, wire.ExplainRequest):
                return [wire.ExplainReply(reading.explanation, self.last_fallbacks)]
            return [wire.MineReply(reading) if isinstance(request, wire.MineRequest) else wire.ViewReply(reading)]
        key = request.episode if isinstance(request, (wire.AdmitRequest, wire.LaunchRequest)) else request.request_id
        replay = self.ledger.get(key)
        if replay is not None:
            return [replay.reply]
        if isinstance(request, (wire.AdmitRequest, wire.LaunchRequest)):
            if not self._live(request.owner):
                return self._mutation_reply(key, wire.Refused("owner no longer exists"))
            if any(record.owner == request.owner for record in self.reservations.values()):
                return self._mutation_reply(key, wire.Refused("owner already has a reservation"))
            if any(waiting.request.owner == request.owner and episode != key
                   for episode, waiting in (*self.waiters.items(), *self.launches.items())):
                return self._mutation_reply(key, wire.Refused("owner already has an admission episode"))
            self._pending_replies[key] = []
            if isinstance(request, wire.LaunchRequest) and not self.waiters and not self.reservations:
                place = WaiterPlace(request.owner, request.since, key)
                self.launches[key] = WaitingAdmission(request, self._row(request, place), connection)
                self._watch_owner(request.owner)
                _ = self.run_pass()
                return self._pending_replies.pop(key, [])
            place_key = _place_key(request.owner, request.since)
            place = self.places.get(place_key, WaiterPlace(request.owner, request.since, key))
            self._run_place(request, place)
            self.waiters[key] = WaitingAdmission(request, self._row(request, place), connection)
            self._save_place(self.waiters[key])
            self._watch_owner(request.owner)
            if (isinstance(request, wire.AdmitRequest) and request.token_back_at
                    and not isinstance(place.token_left_at, TokenNotLeft)):
                gate.append_queue_time(self.state_dir, {"kind": "token_gap", "episode": key,
                    "pid": request.owner.pid, "start_time": request.owner.start_time,
                    "step": request.profile.step, "session_id": request.identity.session_id,
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
                    updated = replace(record, test_width=gate.RequestedTestWidth(request.width))
                    updated = replace(updated, estimate_revision=record.estimate_revision + 1,
                                      test_phase_need=gate.NoTestPhaseNeed("not_recorded"))
                    if record.step == "nextest":
                        self._queue_estimate(updated, gate.read_memory_config(report=False))
                    self.reservations[episode] = updated
            live = any(record.reservation_id == request.reservation for record in self.reservations.values())
            self.ledger[key] = ReplayEntry(wire.Ok(), LiveGrant() if live else self.clock.now(), request.reservation)
        elif isinstance(request, wire.TokenWaitRequest):
            for episode, record in list(self.reservations.items()):
                if record.reservation_id == request.reservation:
                    self._end(episode, TokenWaiting(key, self.clock.now()))
            self.ledger[key] = ReplayEntry(wire.Ok(), self.clock.now())
        elif isinstance(request, (wire.PrioritySetRequest, wire.PriorityClearRequest)):
            if isinstance(request, wire.PrioritySetRequest):
                if not request.unit.strip():
                    return self._mutation_reply(key, wire.Refused("unit is required"))
                sessions = [waiting.row.get("session_id", "") for waiting in self.waiters.values()]
                sessions.extend(record.queue_waiter.get("session_id", "") for record in self.reservations.values())
                labels = gate.priority_labels(sessions)
                if request.unit == "—" or not any(
                        session in labels and labels[session].unit == request.unit for session in sessions):
                    return self._mutation_reply(key, wire.Refused(
                        f"no waiting step or current reservation belongs to unit {request.unit}"))
                priority: gate.QueuePriority = {"unit": request.unit, "why": request.why,
                    "set_at": self.clock.now(), "set_by": request.actor,
                    "transition": {"id": key, "action": "set", "at": self.clock.now()}}
            else:
                priority = (self.priority.copy() if not isinstance(self.priority, gate.NoQueuePriority) else
                    {"unit": "", "why": "", "set_at": self.clock.now(), "set_by": request.actor})
                priority["transition"] = {"id": key, "action": "clear", "at": self.clock.now()}
            self.priority = priority
            self.ledger[key] = ReplayEntry(wire.Ok(), self.clock.now())
            self.persist()
            self._finish_priority()
        else:
            ordered = self.last_inputs.ordered if isinstance(self.last_inputs, gate.QueueInputs) else []
            if not ordered:
                return self._mutation_reply(key, wire.Refused("nothing waits; no head to admit"))
            if not isinstance(self.last_inputs, gate.QueueInputs) or not isinstance(self.last_inputs.host, gate.HostMemory):
                return self._mutation_reply(key, wire.Refused("memory reading unavailable"))
            row = ordered[0][1]
            host = self.last_inputs.host
            limits = cast(gate.AdmissionLimits, self.last_limits)
            reserve = gate.decide(host.available, host.total, row["need"], [], limits=limits).reserve
            self.override = {"waiter": gate.waiter_owner(row), "pid": row["pid"], "start_time": row["start_time"],
                "unit": gate.waiter_unit_name(gate.RecordedQueueIdentities().identity(row["pid"],
                    cast(dict[str, object], cast(object, row))), row["pid"]),
                "step": row.get("step", ""), "need": row["need"], "free": host.available, "reserve": reserve,
                "why": request.why, "set_by": request.actor}
            gate.head_override_event("set", self.override, row["need"], host.available, reserve, self.clock.now())
            self.ledger[key] = ReplayEntry(wire.Ok(), self.clock.now())
        _ = self.run_pass()
        if isinstance(request, (wire.AdmitRequest, wire.LaunchRequest)):
            return self._pending_replies.pop(key, [])
        return [self.ledger[key].reply]

    def _mutation_reply(self, key: str, reply: wire.Ok | wire.Refused, reservation: str = "") -> list[wire.Reply]:
        live = any(record.reservation_id == reservation for record in self.reservations.values())
        self.ledger[key] = ReplayEntry(reply, LiveGrant() if live else self.clock.now(), reservation)
        self.persist()
        return [reply]

    def _deliver(self, waiting: WaitingAdmission, reply: wire.Reply) -> None:
        episode = waiting.request.episode
        if episode in self._pending_replies:
            self._pending_replies[episode].append(reply)
        if isinstance(waiting.connection, Connection):
            self._send(waiting.connection, reply)

    def _send(self, connection: Connection, reply: wire.Reply) -> None:
        if connection.finished:
            return
        connection.sending += wire.encode_reply(reply).encode()
        connection.finished = not isinstance(reply, (wire.Wait, wire.BuildHistoryReply))
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
                    probe.settimeout(gate.reconnect_seconds())
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
        if isinstance(self.override_reconnect_until, ReconnectAwaitingListener):
            self.override_reconnect_until = ReconnectDeadline(self.clock.monotonic() + max(
                gate.override_reconnect_seconds(), gate.reconnect_seconds()))
            self.next_pass = min(self.next_pass, self.override_reconnect_until.at)
        _ = outage_alerts.clear("build-admission", self.clock.now())
        _notify("READY=1")

    def _disconnect(self, connection: Connection) -> None:
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
                del self.waiters[episode]
                changed = True
        for episode, waiting in list(self.launches.items()):
            if waiting.connection is connection:
                del self.launches[episode]
                changed = True
        return changed

    def _refuse_connection(self, connection: Connection, reason: str) -> None:
        _ = self._detach_admission(connection)
        connection.received = b""
        connection.sending = b""
        self._send(connection, wire.Refused(reason))

    def _read_request(self, connection: Connection) -> None:
        limit = gate.request_max_bytes()
        if len(connection.received) > limit:
            raise ValueError(f"admission request exceeds {limit} bytes")
        while not connection.finished:
            chunk = connection.socket.recv(min(65536, limit + 1 - len(connection.received)))
            if not chunk:
                self._disconnect(connection)
                return
            connection.received += chunk
            if len(connection.received) > limit:
                raise ValueError(f"admission request exceeds {limit} bytes")
            if b"\n" in connection.received:
                line, _, tail = connection.received.partition(b"\n")
                if tail:
                    raise ValueError("one request per connection")
                request = wire.parse_request(line)
                if not isinstance(request, wire.BuildHistoryRequest):
                    if not isinstance(connection.request, NoRequest):
                        raise ValueError("one request for admission per connection")
                    connection.request = request
                connection.received = b""
                replies = self.handle_request(request, connection)
                # Admission replies were delivered as part of the pass.
                for reply in replies:
                    if isinstance(request, wire.BuildHistoryRequest) or not connection.sending:
                        self._send(connection, reply)
                return

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
        current = {path: self._stamp(path) for path in self.sources}
        now = self.clock.monotonic()
        if current != self.sources:
            self.sources = current
            self.source_change_at = now
        elif not isinstance(self.source_change_at, SourcesUnchanged) and now - self.source_change_at >= gate.source_settle_seconds():
            self.persist()
            _notify("STOPPING=1")
            self.stopped = True
        self.next_source_check = now + gate.source_check_seconds()

    def run_once(self, timeout: float = 0) -> None:
        self.start()
        now = self.clock.monotonic()
        delay = min(timeout, max(0, min(self.next_pass, self.next_source_check) - now))
        ready = self.epoll.poll(delay)
        # Drain ready requests before any expiry pass, including requests queued
        # on a connection accepted this turn. A silent peer cannot delay expiry.
        self.receiving_requests = True
        try:
            self._receive_requests(ready)
        finally:
            self.receiving_requests = False
        estimates_ready = False
        for descriptor, events in ready:
            if descriptor in self.connections:
                self._connection_event(self.connections[descriptor], events & ~select.EPOLLIN)
            elif descriptor == self.estimate_read:
                estimates_ready = True
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
                for episode, waiting in list(self.waiters.items()):
                    if not self._live(waiting.request.owner):
                        del self.waiters[episode]
                for episode, waiting in list(self.launches.items()):
                    if not self._live(waiting.request.owner):
                        del self.launches[episode]
                self._prune_places()
                self._prune_watches()
                self.next_pass = self.clock.monotonic()
        if estimates_ready:
            self._accept_estimates()
        elif self.clock.monotonic() >= self.next_pass:
            _ = self.run_pass()
        if self.clock.monotonic() >= self.next_source_check:
            self._check_sources()
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

    def serve_forever(self) -> None:
        while not self.stopped:
            self.run_once(gate.source_check_seconds())

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.stopped = True
        self.estimate_jobs.put(StopEstimates())
        os.close(self.estimate_read)
        for connection in list(self.connections.values()):
            connection.socket.close()
        self.connections.clear()
        for descriptor in self.watchers:
            os.close(descriptor)
        self.watchers.clear()
        if not isinstance(self.listener, NoConnection):
            self.listener.close()
            if not os.environ.get("LISTEN_FDS"):
                self.socket_path.unlink(missing_ok=True)
        self.epoll.close()


# These scripts retain admission functions for the lifetime of their process.
ADMISSION_CALLERS = ("verify.sh", "invoke.sh", "pre-tool-use-brp-launch-gate.sh", "merge_gate.py")


def read_state(directory: Path) -> ServiceState:
    record = wire.parse_record((directory / "state.json").read_text(), dict[str, object])
    if record.keys() == {"kind", "reservations", "places", "ledger", "recovery", "override"}:
        # The first state form stores neither a queue priority nor pending import cleanup.
        record["priority"] = gate.snapshot_value(gate.NO_QUEUE_PRIORITY)
        record["legacy_cleanup"] = gate.snapshot_value(())
    if "run_places" not in record:
        record["run_places"] = {}
    old_fields = gate.snapshot_fields(ServiceState).keys() - {"build_history_generation"} | {"kind"}
    if record.keys() == old_fields | {"build_invalidations"}:
        del record["build_invalidations"]
    if record.keys() == old_fields:
        record["build_history_generation"] = 0
    places = record.get("places")
    if isinstance(places, dict):
        legacy_fields = gate.snapshot_fields(WaiterPlace).keys()
        for place in cast(dict[str, object], places).values():
            if isinstance(place, dict) and place.keys() == legacy_fields | {"kind", "build_estimate"}:
                del cast(dict[str, object], place)["build_estimate"]
    return cast(ServiceState, gate.parse_snapshot_value(record, ServiceState))


def drain_check(directory: Path, *, service: bool, include_gate_callers: bool, proc: Proc) -> int:
    """Read admission owners and callers without changing admission state."""
    waiting: list[str] = []
    reserved: list[str] = []
    owners: list[wire.Owner] = []
    if service:
        try:
            state = read_state(directory)
        except FileNotFoundError:
            state = ServiceState({}, {}, {}, gate.NoRecoverySpacing(), gate.NoHeadAdmissionOverride())
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
            waiting.append(f"{owner.pid}-{owner.start_time}")
    for name, row in rows:
        live = proc.owner_start(row["pid"]) == LiveOwnerIdentity(row["start_time"])
        if not isinstance(proc.lifetime(row, live), gate.EndedStep):
            reserved.append(name)
    clients: list[str] = []
    names = ADMISSION_CALLERS if not service or include_gate_callers else ()
    if service:
        names = (*names, "admission_client.py")
    for pid in proc.processes():
        if pid == os.getpid():
            continue
        try:
            args = (proc.path / str(pid) / "cmdline").read_bytes().decode().split("\0")
        except (OSError, UnicodeError):
            continue
        if any(Path(arg).name in names for arg in args):
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
        outcome, status = finalization_outcome(record.ending)
        try:
            _ = gate.finalize_reservation(Path(record.path), reservation_row(record), outcome,
                datetime.now(UTC), status, intents=StateIntents(state.reservations,
                    advance_generation, episode, persist, _no_fault))
        except Exception as error:
            raise ValueError(f"cannot finalize reservation {record.reservation_id}: {error}; service state kept") from error
    path = gate.queue_priority_path(directory)
    if isinstance(state.priority, gate.NoQueuePriority):
        path.unlink(missing_ok=True)
    else:
        # The legacy reader uses the same domain codec without an envelope.
        write_text_atomic(path, wire.encode_record(state.priority))
    (directory / "state.json").unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=("serve", "drain-check", "export-priority"))
    default_state_dir = gate.store.root() / "admission"
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
