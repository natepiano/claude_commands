"""An isolated, single-loop admission service. No live caller uses it yet."""
from __future__ import annotations

import argparse
import errno
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import os
from pathlib import Path
import select
import socket
import sys
import time
from typing import Callable, Protocol, cast
import uuid

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.lib import process_start
from scripts.build_queue import admission_protocol as wire
from scripts.build_queue.queue_view import reason_phrase
from scripts.lint import memory_admit as gate
from scripts.lint.build_shape import build_shape
from scripts.lib.atomic_file import write_text_atomic


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
                    processes: gate.ProcessTable, proc: Path, policy: gate.AdmissionPolicy) -> gate.StepMemory: ...


class SystemReadings:
    def host(self) -> gate.HostMemory | gate.ReadingUnavailable:
        try:
            return gate.read_meminfo(Path(os.environ.get("BUILDLOG_MEMINFO", "/proc/meminfo")))
        except (OSError, ValueError):
            return gate.ReadingUnavailable()

    def pressure(self, host: gate.HostMemory, policy: gate.AdmissionPolicy) -> gate.Pressure:
        return gate.read_pressure(host, policy=policy)

    def compilers(self) -> list[gate.StepMemory]:
        return gate.sccache_memory()

    def reservation(self, path: Path, row: gate.Reservation, now: datetime,
                    processes: gate.ProcessTable, proc: Path, policy: gate.AdmissionPolicy) -> gate.StepMemory:
        return gate.reservation_memory(path, row, now, processes, proc, policy)


class Proc(Protocol):
    @property
    def path(self) -> Path: ...
    def processes(self) -> gate.ProcessTable: ...
    def owner_check(self, owner: wire.Owner) -> process_start.OwnerCheck: ...
    def pidfd(self, pid: int) -> int: ...
    def lifetime(self, row: gate.Reservation, owner_live: bool
                 ) -> gate.PopulatedStepScope | gate.LiveOwnerProcess | gate.EndedStep: ...


class SystemProc:
    @property
    def path(self) -> Path:
        return Path(os.environ.get("BUILDLOG_PROC", "/proc"))

    def processes(self) -> gate.ProcessTable:
        return gate.process_table(self.path)

    def owner_check(self, owner: wire.Owner) -> process_start.OwnerCheck:
        return process_start.check_owner(owner.pid, process_start.parse_start(owner.start_time))

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
    test_width: wire.TestWidth
    call: wire.Call
    reservation_id: str
    queue_waiter: gate.Waiter
    repo_identity: str = ""
    test_scope: gate.TestScope = "full"
    package: str = ""
    filters: tuple[str, ...] = ()
    shape: str = ""
    test_phase_need: gate.TestPhaseNeed | gate.NoTestPhaseNeed = gate.NoTestPhaseNeed("not_recorded")
    ending: NotEnding | ReleaseStatus | OwnerExited = NotEnding()


def reservation_row(record: ReservationRecord) -> gate.Reservation:
    """The only service-to-legacy nullable width/call conversion."""
    row: gate.Reservation = {
        "pid": record.owner.pid, "start_time": record.owner.start_time, "need": record.need,
        "repo": record.repo, "step": record.step, "worktree": record.worktree,
        "admitted_at": record.admitted_at, "sidecar": record.sidecar,
        "test_threads": record.test_width.threads if isinstance(record.test_width, wire.RequestedTestWidth) else None,
        "call_id": record.call.call_id if isinstance(record.call, wire.CallRecord) else None,
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
    return row


def reservation_record(episode: str, path: str, row: gate.Reservation) -> ReservationRecord:
    """The only legacy-to-service nullable width/call conversion."""
    width, call = row["test_threads"], row["call_id"]
    return ReservationRecord(episode, path, wire.Owner(row["pid"], row["start_time"]), row["need"],
        row["repo"], row["step"], row["worktree"], row["admitted_at"], row["sidecar"],
        wire.RequestedTestWidth(width) if width is not None else
        wire.UnspecifiedTestWidth() if row["step"] == "nextest" else wire.NoTestWidth(),
        wire.CallRecord(call) if call is not None else wire.NoCallRecord(),
        row.get("reservation_id", ""), row.get("queue_waiter", {
            "pid": row["pid"], "start_time": row["start_time"], "need": row["need"],
            "since": datetime.fromisoformat(row["admitted_at"]).timestamp()}),
        row.get("repo_identity", ""), row.get("test_scope", "full"), row.get("package", ""),
        tuple(row.get("filters", [])), row.get("shape", ""), gate.read_test_phase_need(row.get("test_phase_need")))


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


def reservation_stem(state_dir: Path, owner: wire.Owner, admitted_ns: int) -> Path:
    return state_dir / f"{owner.pid}-{owner.start_time}-{admitted_ns}"


def _place_key(owner: wire.Owner, since: float) -> str:
    return f"{owner.pid}-{owner.start_time}:{since:.17g}"


def _notify(message: str) -> None:
    address = os.environ.get("NOTIFY_SOCKET", "")
    if address:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
            notifier.connect("\0" + address[1:] if address.startswith("@") else address)
            _ = notifier.send(message.encode())


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
        self.ledger: dict[str, ReplayEntry] = {}
        self.recovery: gate.RecoverySpacing = gate.NoRecoverySpacing()
        self.override: gate.HeadAdmissionOverride | gate.NoHeadAdmissionOverride = gate.NoHeadAdmissionOverride()
        self.last_inputs: gate.QueueInputs | NoPass = NoPass()
        self.last_result: gate.PassResult | NoPass = NoPass()
        self.last_limits: gate.AdmissionLimits | NoPass = NoPass()
        self.last_policy: gate.AdmissionPolicy = gate.DEFAULT_ADMISSION_POLICY
        self._pending_replies: dict[str, list[wire.Reply]] = {}
        self._persisted: str = ""
        self.override_reconnect_until: ReconnectDeadline | ReconnectAwaitingListener | NoReconnectGrace = NoReconnectGrace()
        self.receiving_requests: bool = False
        self.connections: dict[int, Connection] = {}
        self.watchers: dict[int, OwnerWatch | ScopeWatch] = {}
        self.epoll: select.epoll = select.epoll()
        self.listener: socket.socket | NoConnection = NoConnection()
        self.stopped: bool = False
        self.next_pass: float = float("inf")
        self.next_source_check: float = clock.monotonic() + gate.source_check_seconds()
        self.sources: dict[Path, tuple[int, int] | SourceMissing] = self._source_stamps()
        self.source_change_at: float | SourcesUnchanged = SourcesUnchanged()
        state_dir.mkdir(parents=True, exist_ok=True)
        self._restore()
        _ = self.run_pass()

    def _restore(self) -> None:
        path = self.state_dir / "state.json"
        if path.exists():
            state = wire.parse_record(path.read_text(), ServiceState)
            self.reservations, self.places, self.ledger = state.reservations, state.places, state.ledger
            self.recovery, self.override = state.recovery, state.override
            self._persisted = wire.encode_record(state)
            if not isinstance(self.override, gate.NoHeadAdmissionOverride):
                self.override_reconnect_until = ReconnectAwaitingListener()
        for episode, record in list(self.reservations.items()):
            path = Path(record.path)
            row = gate.read_reservation(path) if path.exists() else reservation_row(record)
            live = not self.proc.owner_check(record.owner).stale
            lifetime = self.proc.lifetime(row, live)
            if "finalization" in row or not isinstance(record.ending, NotEnding) or isinstance(lifetime, gate.EndedStep):
                self._end(episode, record.ending if not isinstance(record.ending, NotEnding) else OwnerExited())
            else:
                self._watch_owner(record.owner)
                self._watch_scope(lifetime, episode)
        self._prune_places()

    def _persist(self, config: gate.MemoryConfig | gate.ReadMemoryConfiguration = gate.READ_MEMORY_CONFIGURATION) -> None:
        now = self.clock.now()
        resolved = gate.read_memory_config() if isinstance(config, gate.ReadMemoryConfiguration) else config
        self.ledger = {key: entry for key, entry in self.ledger.items()
                       if isinstance(entry.ended_at, LiveGrant) or now - entry.ended_at < resolved.replay_retention_seconds}
        encoded = wire.encode_record(ServiceState(
            self.reservations, self.places, self.ledger, self.recovery, self.override))
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
        for descriptor, watch in list(self.watchers.items()):
            if (isinstance(watch, OwnerWatch) and watch.owner not in owners
                    or isinstance(watch, ScopeWatch) and watch.episode not in self.reservations):
                self._unwatch(descriptor)

    def _ordered_waiters(self) -> list[tuple[Path, gate.Waiter]]:
        return sorted(((self.state_dir / gate.waiter_owner(waiting.row), waiting.row.copy())
                       for waiting in self.waiters.values()), key=lambda item: gate.waiter_order(item[1]))

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

    def _prune_places(self) -> None:
        self.places = {key: place for key, place in self.places.items()
                       if not self.proc.owner_check(place.owner).stale}

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
            if isinstance(request.test_width, wire.RequestedTestWidth):
                row["test_threads"] = request.test_width.threads
        return row

    def _inputs(self, config: gate.MemoryConfig) -> tuple[gate.QueueInputs, gate.AdmissionLimits]:
        policy = config.policy
        now = self.clock.now()
        running: list[gate.RunningStep] = []
        rows: list[gate.Reservation] = []
        owners = {episode: self.proc.owner_check(record.owner) for episode, record in self.reservations.items()}
        lifetimes = {episode: self.proc.lifetime(reservation_row(record), not owners[episode].stale)
                     for episode, record in self.reservations.items()}
        processes: dict[int, gate.ProcessIdentity | gate.UnreadableProcessIdentity] = (dict(self.proc.processes()) if any(
            isinstance(lifetime, gate.LiveOwnerProcess) and owners[episode] is process_start.OwnerCheck.ALIVE
            for episode, lifetime in lifetimes.items()) else {})
        for episode, record in self.reservations.items():
            owner = owners[episode]
            if (owner is process_start.OwnerCheck.ALIVE
                    and not isinstance(processes.get(record.owner.pid), gate.UnreadableProcessIdentity)):
                processes[record.owner.pid] = gate.ProcessIdentity(0, record.owner.start_time)
            elif owner.stale:
                _ = processes.pop(record.owner.pid, None)
        for episode, record in self.reservations.items():
            if owners[episode] is process_start.OwnerCheck.UNKNOWN:
                processes[record.owner.pid] = gate.UnreadableProcessIdentity("owner identity is unknown")
        for episode, record in list(self.reservations.items()):
            row = reservation_row(record)
            memory = self.readings.reservation(Path(record.path), row, datetime.fromtimestamp(now, UTC),
                                               processes, self.proc.path, policy)
            if isinstance(memory, gate.GoneMemory):
                self._end(episode, OwnerExited())
                continue
            identity = row.get("queue_waiter", {})
            running.append(gate.RunningStep(identity.get("session_id", ""), identity.get("session_name", ""),
                record.step, identity.get("folder", record.worktree), record.need, memory))
            rows.append(row)
            self._watch_scope(lifetimes[episode], episode)
        for compiler in self.readings.compilers():
            running.append(gate.RunningStep("", "sccache.service", "compile", "builds.slice", 0, compiler))
        host = self.readings.host()
        pressure = self.readings.pressure(host if isinstance(host, gate.HostMemory) else gate.HostMemory(0, gate.UnknownHostTotal()), policy)
        ordered = self._ordered_waiters()
        limits = gate.resolve_admission_limits(host.total if isinstance(host, gate.HostMemory) else gate.UnknownHostTotal(), config)
        return gate.QueueInputs(ordered, tuple(running), tuple(rows), host, pressure,
                                self.recovery, self.override, now), limits

    def run_pass(self) -> gate.PassResult:
        config = gate.read_memory_config()
        policy = config.policy
        inputs, limits = self._inputs(config)
        if self.launches and (self.waiters or self.reservations or isinstance(inputs.pressure, gate.MemoryPressure)):
            for episode, waiting in list(self.launches.items()):
                self.waiters[episode] = waiting
                self._save_place(waiting)
                del self.launches[episode]
            inputs = replace(inputs, ordered=self._ordered_waiters())
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
                gate.record_test_phase_charge_use(Path(matching.path), record,
                                                 gate.charge_basis(step.measured), inputs.now)
        for mark in result.head_marks:
            for waiting in self.waiters.values():
                if gate.waiter_owner(waiting.row) == mark.path.name:
                    waiting.row = mark.waiter.copy()
                    self._save_place(waiting)
        by_owner = {gate.waiter_owner(waiting.row): episode for episode, waiting in self.waiters.items()}
        starts = sorted((row for row in result.rows if isinstance(row.admission, gate.ProposedAdmission)),
                        key=lambda row: cast(gate.ProposedAdmission, row.admission).rank)
        if not starts:
            self._effects(result.recovery, result.override, result.totals, preserve_override)
        for decision in starts:
            episode = by_owner[decision.owner]
            waiting = self.waiters[episode]
            if self.proc.owner_check(waiting.request.owner) is not process_start.OwnerCheck.ALIVE:
                continue
            proposed = cast(gate.ProposedAdmission, decision.admission)
            self._effects(proposed.recovery, proposed.override, decision.decision, preserve_override)
            self._admit(waiting, config)
        for episode, waiting in list(self.launches.items()):
            request = waiting.request
            if self.proc.owner_check(request.owner) is not process_start.OwnerCheck.ALIVE:
                continue
            if not isinstance(inputs.host, gate.HostMemory):
                reply_launch: wire.Reply = wire.Wait(gate.ReadingUnavailable(), 0, "", "memory reading unavailable")
            else:
                decision_launch = gate.decide(inputs.host.available, inputs.host.total, request.profile.peak.bytes,
                    [step.measured for step in inputs.running], inputs.pressure, limits=limits)
                if decision_launch.state == "admit":
                    reply_launch = wire.LaunchAdmitted(request.profile.peak.bytes, request.profile.peak.source)
                    self.ledger[episode] = ReplayEntry(reply_launch, self.clock.now())
                    self._persist(config)
                else:
                    reason = gate.queue_wait_reason(decision_launch, request.profile.peak.bytes,
                                                    inputs.host.available, gate.RecordedQueueIdentities())
                    reply_launch = wire.Wait(reason, 0, "", reason_phrase(reason, ()))
            if waiting.last_reply != reply_launch or inputs.now - waiting.last_sent >= config.wait_reply_seconds:
                self._deliver(episode, reply_launch)
                if isinstance(reply_launch, wire.Wait):
                    waiting.last_reply, waiting.last_sent = reply_launch, inputs.now
            if isinstance(reply_launch, wire.LaunchAdmitted):
                del self.launches[episode]
        self._persist(config)
        waiting_steps = tuple(gate.WaitingStep(row["pid"], row.get("session_id", ""), row.get("session_name", ""),
            row.get("step", ""), row.get("folder", ""), row["since"], row["need"], decision.reason)
            for (_, row), decision in zip(inputs.ordered, result.rows, strict=True))
        for position, decision in enumerate(result.rows, 1):
            episode = by_owner[decision.owner]
            if episode in self.waiters:
                head = inputs.ordered[0][1].get("session_name", "") if inputs.ordered else ""
                reply = wire.Wait(decision.reason, position, head, reason_phrase(decision.reason, waiting_steps))
                waiting = self.waiters[episode]
                if waiting.last_reply != reply or inputs.now - waiting.last_sent >= config.wait_reply_seconds:
                    self._deliver(episode, reply)
                    waiting.last_reply, waiting.last_sent = reply, inputs.now
        interval = config.waiting_pass_seconds if self.waiters or self.launches else config.running_pass_seconds if self.reservations else float("inf")
        self.next_pass = self.clock.monotonic() + interval
        if isinstance(self.override_reconnect_until, ReconnectDeadline):
            self.next_pass = min(self.next_pass, self.override_reconnect_until.at)
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
        self.places[_place_key(request.owner, row["since"])] = WaiterPlace(request.owner, row["since"], request.episode,
            row.get("priority_rank", 1), row.get("priority_since", OrderNotStarted()),
            row.get("token_left_at", TokenNotLeft()))

    def _test_phase_need(self, record: ReservationRecord, config: gate.MemoryConfig) -> gate.TestPhaseNeed | gate.NoTestPhaseNeed:
        return gate.test_phase_need(reservation_row(record), gate.store.host_name(),
            datetime.fromtimestamp(self.clock.now(), UTC), self.state_dir,
            history_path=self.state_dir / "anon_peaks.jsonl", config=config)

    def _admit(self, waiting: WaitingAdmission, config: gate.MemoryConfig) -> None:
        request, profile = waiting.request, waiting.request.profile
        if isinstance(request, wire.LaunchRequest):
            reply: wire.Reply = wire.LaunchAdmitted(profile.peak.bytes, profile.peak.source)
            self.ledger[request.episode] = ReplayEntry(reply, self.clock.now())
        else:
            stem = reservation_stem(self.state_dir, request.owner, int(self.clock.now() * 1_000_000_000))
            # The episode disambiguates grants at the same clock tick after release.
            while any(record.path == str(stem) for record in self.reservations.values()):
                stem = stem.with_name(stem.name + "0")
            record = ReservationRecord(request.episode, str(stem), request.owner, profile.peak.bytes,
                profile.repo, profile.step, profile.worktree, gate.store.utc_iso(self.clock.now()), request.sidecar,
                request.test_width, request.call, uuid.uuid4().hex, waiting.row.copy(),
                profile.repository.key if isinstance(profile.repository, gate.RepositoryIdentity) else "",
                profile.scope, gate.test_package(list(request.argv)), tuple(gate.test_filters(list(request.argv))),
                build_shape(list(request.argv)).key if profile.step == "build" else "")
            if profile.step == "nextest":
                try:
                    need = self._test_phase_need(record, config)
                    record = replace(record, test_phase_need=need)
                except (OSError, ValueError):
                    pass
            self.reservations[request.episode] = record
            reply = wire.Admitted(str(stem), record.need, profile.peak.source, str(stem.with_suffix(".max")),
                                  str(stem.with_suffix(".wave")))
            self.ledger[request.episode] = ReplayEntry(reply, LiveGrant())
            self._watch_owner(request.owner)
        self._save_place(waiting)
        # Durable grant and replay reply precede any terminal reply.
        self._persist(config)
        self._deliver(request.episode, reply)
        del self.waiters[request.episode]

    def _end(self, episode: str, ending: ReleaseStatus | OwnerExited | NotEnding) -> None:
        record = self.reservations[episode]
        record = replace(record, ending=ending)
        self.reservations[episode] = record
        self._persist()
        path = Path(record.path)
        row = gate.read_reservation(path) if path.exists() else reservation_row(record)
        if isinstance(ending, ReleaseStatus):
            status: int | gate.OutcomeNotRecorded = ending.status
            outcome: gate.FinalOutcome = (gate.Completed() if ending.status == 0 else
                gate.Killed() if ending.status >= 128 else gate.Failed())
        else:
            status, outcome = gate.OUTCOME_NOT_RECORDED, gate.OwnerGone()
        _ = gate.finalize_reservation(path, row, outcome, datetime.fromtimestamp(self.clock.now(), UTC), status)
        self.fault("after_history_append")
        del self.reservations[episode]
        self._prune_watches()
        for key, entry in list(self.ledger.items()):
            if entry.reservation == record.path:
                self.ledger[key] = replace(entry, ended_at=self.clock.now())
        previous = self.ledger.get(episode)
        if previous is not None:
            self.ledger[episode] = replace(previous, ended_at=self.clock.now())
        if isinstance(ending, ReleaseStatus):
            self.ledger[ending.request_id] = ReplayEntry(wire.Ok(), self.clock.now())
        self._persist()

    def handle_request(self, request: wire.Request, connection: Connection | NoConnection = NO_CONNECTION
                       ) -> list[wire.Reply]:
        if isinstance(request, (wire.ViewRequest, wire.ExplainRequest, wire.MineRequest)):
            if not isinstance(self.last_inputs, gate.QueueInputs) or not isinstance(self.last_limits, gate.AdmissionLimits):
                raise RuntimeError("service has no pass")
            reading = gate.queue_read_from(self.last_inputs, self.last_limits, self.last_policy)
            if isinstance(request, wire.ExplainRequest):
                return [wire.ExplainReply(reading.explanation)]
            return [wire.MineReply(reading) if isinstance(request, wire.MineRequest) else wire.ViewReply(reading)]
        key = request.episode if isinstance(request, (wire.AdmitRequest, wire.LaunchRequest)) else request.request_id
        replay = self.ledger.get(key)
        if replay is not None:
            return [replay.reply]
        if isinstance(request, (wire.AdmitRequest, wire.LaunchRequest)):
            if self.proc.owner_check(request.owner) is not process_start.OwnerCheck.ALIVE:
                return [wire.Refused("owner no longer exists")]
            if any(record.owner == request.owner for record in self.reservations.values()):
                return [wire.Refused("owner already has a reservation")]
            if any(waiting.request.owner == request.owner and episode != key
                   for episode, waiting in (*self.waiters.items(), *self.launches.items())):
                return [wire.Refused("owner already has an admission episode")]
            self._pending_replies[key] = []
            if isinstance(request, wire.LaunchRequest) and not self.waiters and not self.reservations:
                place = WaiterPlace(request.owner, request.since, key)
                self.launches[key] = WaitingAdmission(request, self._row(request, place), connection)
                self._watch_owner(request.owner)
                _ = self.run_pass()
                return self._pending_replies.pop(key, [])
            place_key = _place_key(request.owner, request.since)
            place = self.places.get(place_key, WaiterPlace(request.owner, request.since, key))
            self.waiters[key] = WaitingAdmission(request, self._row(request, place), connection)
            self._save_place(self.waiters[key])
            self._watch_owner(request.owner)
        elif isinstance(request, wire.ReleaseRequest):
            episode = next((episode for episode, record in self.reservations.items() if record.path == request.reservation), None)
            if episode is not None:
                self._end(episode, ReleaseStatus(key, request.status))
            self.ledger[key] = ReplayEntry(wire.Ok(), self.clock.now())
        elif isinstance(request, wire.WidthRequest):
            if request.width <= 0:
                return self._mutation_reply(key, wire.Refused("test width must be positive"), request.reservation)
            for episode, record in list(self.reservations.items()):
                if record.path == request.reservation:
                    updated = replace(record, test_width=wire.RequestedTestWidth(request.width))
                    if record.step == "nextest":
                        need = self._test_phase_need(updated, gate.read_memory_config())
                        updated = replace(updated, test_phase_need=need)
                    self.reservations[episode] = updated
            live = any(record.path == request.reservation for record in self.reservations.values())
            self.ledger[key] = ReplayEntry(wire.Ok(), LiveGrant() if live else self.clock.now(), request.reservation)
        else:
            ordered = self._ordered_waiters()
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
        live = any(record.path == reservation for record in self.reservations.values())
        self.ledger[key] = ReplayEntry(reply, LiveGrant() if live else self.clock.now(), reservation)
        self._persist()
        return [reply]

    def _deliver(self, episode: str, reply: wire.Reply) -> None:
        if episode in self._pending_replies:
            self._pending_replies[episode].append(reply)
        waiting = self.waiters.get(episode) or self.launches.get(episode)
        if waiting is not None and isinstance(waiting.connection, Connection):
            self._send(waiting.connection, reply)

    def _send(self, connection: Connection, reply: wire.Reply) -> None:
        if connection.finished:
            return
        connection.sending += wire.encode_reply(reply).encode()
        connection.finished = not isinstance(reply, wire.Wait)
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
            if not isinstance(connection.request, NoRequest):
                raise ValueError("one request per connection")
            connection.received += chunk
            if len(connection.received) > limit:
                raise ValueError(f"admission request exceeds {limit} bytes")
            if b"\n" in connection.received:
                line, _, tail = connection.received.partition(b"\n")
                if tail:
                    raise ValueError("one request per connection")
                request = wire.parse_request(line)
                connection.request = request
                connection.received = b""
                replies = self.handle_request(request, connection)
                # Admission replies were delivered as part of the pass.
                for reply in replies:
                    if not connection.sending:
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
            self._persist()
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
                for episode, waiting in list(self.waiters.items()):
                    if self.proc.owner_check(waiting.request.owner).stale:
                        del self.waiters[episode]
                for episode, waiting in list(self.launches.items()):
                    if self.proc.owner_check(waiting.request.owner).stale:
                        del self.launches[episode]
                self._prune_places()
                self._prune_watches()
                _ = self.run_pass()
        if self.clock.monotonic() >= self.next_pass:
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=("serve",))
    _ = parser.add_argument("--state-dir", type=Path, required=True)
    class Arguments(argparse.Namespace):
        state_dir: Path = Path()
    args = parser.parse_args(namespace=Arguments())
    server = AdmissionServer(args.state_dir, SystemClock(), SystemReadings(), SystemProc())
    try:
        server.serve_forever()
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
