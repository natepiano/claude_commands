"""One private admission server, fake clock and fault boundary for service tests."""
from __future__ import annotations

from scripts.build_queue import step_estimator
from scripts.lib import test_phase

from scripts.build_queue.queue_fixture import write_memory_config

from collections import Counter
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from dataclasses import replace
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
import uuid
from unittest import mock
from typing import ClassVar, cast, override

from scripts.lint import memory_admit as gate

from scripts.lint.memory_test_support import admission_policy_fixture, config_environment, fake_memory_readings, gate_isolation_environment, write_pressure_reading
from scripts.build_queue import admission_protocol as wire
from scripts.build_queue import admission_records as records
from scripts.build_queue import admission_service as service
from scripts.build_queue.queue_fixture import blocking_slot, fixture_reservation, write_process, write_scope
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS, LauncherHangGuard
from scripts.lib import build_folders
from scripts.lib.socket_test_support import short_socket_directory

NOW = datetime(2026, 10, 31, 12, tzinfo=UTC).timestamp()
GIB = records.GIB
DEFAULT_WIDTH = records.RequestedTestWidth(8)
DEFAULT_CALL = test_phase.CallRecord("call-1")
FIXTURE_POLICY = admission_policy_fixture()
SERVING_POLL_SECONDS = 0.05


class FakeClock:
    def __init__(self) -> None:
        self.epoch: float = NOW
        self.elapsed: float = 0.0
        self.sleeps: list[float] = []
        self.on_sleep: Callable[[float], None] = lambda _seconds: None

    def now(self) -> float:
        return self.epoch

    def monotonic(self) -> float:
        return self.elapsed

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("fake time cannot move backwards")
        self.epoch += seconds
        self.elapsed += seconds

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)
        self.on_sleep(seconds)


class InjectedCrash(RuntimeError):
    """A test stops the loop at a persistence boundary."""


class Faults:
    def __init__(self) -> None:
        self.stage: str = ""
        self.seen: list[str] = []

    def __call__(self, stage: str) -> None:
        self.seen.append(stage)
        if stage == self.stage:
            self.stage = ""
            raise InjectedCrash(stage)


class ScopeEvents:
    """Inject kernfs wakeups while keeping real owner and socket epoll events."""
    def __init__(self, epoll: select.epoll) -> None:
        self.epoll: select.epoll = epoll
        self.scopes: set[int] = set()
        self.registered: list[int] = []
        self.unregistered: list[int] = []
        self.events: list[tuple[int, int]] = []

    def register(self, descriptor: int, events: int) -> None:
        if events & select.EPOLLPRI:
            self.scopes.add(descriptor)
            self.registered.append(descriptor)
        else:
            self.epoll.register(descriptor, events)

    def unregister(self, descriptor: int) -> None:
        if descriptor in self.scopes:
            self.scopes.remove(descriptor)
            self.unregistered.append(descriptor)
        else:
            self.epoll.unregister(descriptor)

    def modify(self, descriptor: int, events: int) -> None:
        self.epoll.modify(descriptor, events)

    def poll(self, timeout: float = 0) -> list[tuple[int, int]]:
        events, self.events = self.events, []
        return self.epoll.poll(timeout) + events


class ObservedAdmissionServer(service.AdmissionServer):
    """Expose loop instrumentation only at the shared test fixture boundary."""

    _source_paths: ClassVar[tuple[int, set[Path]]] = (-1, set())

    def __init__(self, state_dir: Path, clock: service.Clock, readings: service.Readings,
                 proc: service.Proc, *, fault: Callable[[str], None], socket_path: Path,
                 check_access: Callable[[str], None], before_admission: Callable[[], None]) -> None:
        self.check_access: Callable[[str], None] = check_access
        self.before_admission: Callable[[], None] = before_admission
        self.wake_lock: threading.Lock = threading.Lock()
        super().__init__(state_dir, clock, readings, proc, fault=fault, socket_path=socket_path)

    @override
    def close(self) -> None:
        with self.wake_lock:
            super().close()

    def wake_loop(self) -> None:
        with self.wake_lock:
            if not self.closed:
                os.eventfd_write(self.priority_events, 1)

    @override
    def _source_stamps(self) -> dict[Path, tuple[int, int] | service.SourceMissing]:
        module_count = len(sys.modules)
        previous_count, paths = ObservedAdmissionServer._source_paths
        if module_count == previous_count:
            return {path: self._stamp(path) for path in paths}
        stamps = super()._source_stamps()
        ObservedAdmissionServer._source_paths = (module_count, set(stamps))
        return stamps

    @override
    def persist(self) -> None:
        self.check_access("persist")
        super().persist()

    @override
    def run_pass(self) -> gate.PassResult | gate.UnusableAdmissionConfiguration:
        self.check_access("run_pass")
        return super().run_pass()

    @staticmethod
    def reply_filter(reply: wire.Reply) -> wire.Reply:
        return reply

    @staticmethod
    def line_filter(line: str) -> str:
        return line

    @override
    def _send(self, connection: service.Connection, reply: wire.Reply) -> None:
        queued = len(connection.sending)
        super()._send(connection, self.reply_filter(reply))
        connection.sending = connection.sending[:queued] + self.line_filter(
            connection.sending[queued:].decode()).encode()

    @override
    def handle_request(self, request: wire.Request,
                       connection: service.Connection | service.NoConnection = service.NO_CONNECTION) -> list[wire.Reply]:
        self.check_access("handle_request")
        if isinstance(request, (wire.AdmitRequest, wire.ServiceEstimateRequest, wire.LaunchRequest)):
            self.before_admission()
            if isinstance(self.proc, TestProcesses):
                self.proc.register(request.owner)
        return super().handle_request(request, connection)

    def caller_live(self, scenery: set[str]) -> bool:
        """Use the service lifetime rules for caller reservations, waiters, and slots."""
        for record in self.reservations.values():
            if (record.reservation_id not in scenery
                    and not isinstance(self.proc.lifetime(service.reservation_row(record), self._live(record.owner)), gate.EndedStep)):
                return True
        if any(self._live(waiting.request.owner) for waiting in self.waiters.values()):
            return True
        return any(gate.between_steps(slot).reservation not in scenery
                   and gate.slot_in_force(slot, self.clock.now())
                   and self._run_alive(gate.between_steps(slot).run)
                   for slot in self.slots.values())

    @contextmanager
    def silent_replies(self) -> Generator[None]:
        with mock.patch.object(self, "_send", return_value=None):
            yield

    @property
    def pending_replies(self) -> dict[str, list[wire.Reply]]:
        return self._pending_replies

    def send_wait(self, connection: service.Connection, reply: wire.Wait) -> None:
        self._send(connection, reply)

    @contextmanager
    def observe_waiter_ordering(self) -> Generator[mock.MagicMock]:
        with mock.patch.object(self, "_ordered_waiters", wraps=self._ordered_waiters) as ordered:
            yield ordered

    @contextmanager
    def observe_wait_initial_states(self) -> Generator[list[wire.Wait | service.NoWaitReply]]:
        before_delivery: list[wire.Wait | service.NoWaitReply] = []
        deliver = self._deliver

        def observe(waiting: service.WaitingAdmission, reply: wire.Reply) -> None:
            before_delivery.append(waiting.last_reply)
            deliver(waiting, reply)

        with mock.patch.object(self, "_deliver", side_effect=observe):
            yield before_delivery


class TestProcesses:
    """Real pidfds with private resident-memory files."""
    def __init__(self, path: Path) -> None:
        self.path: Path = path
        path.mkdir(parents=True, exist_ok=True)
        self.children: list[subprocess.Popen[bytes]] = []
        self.starts: dict[int, str] = {}
        self.fixture_owners: dict[int, str] = {}

    def spawn(self, used: int = GIB) -> wire.Owner:
        child = subprocess.Popen(["sleep", "300"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(child)
        start = records.process_start_time(child.pid)
        if start is None:
            raise RuntimeError("child has no process start time")
        self.starts[child.pid] = start
        self.register(wire.Owner(child.pid, start), used)
        return wire.Owner(child.pid, start)

    def register(self, owner: wire.Owner, used: int = 0) -> None:
        """Supply private memory for an owner that a caller process created."""
        self.starts[owner.pid] = owner.start_time
        if not (self.path / str(owner.pid) / "stat").exists():
            write_process(self.path, owner.pid, used, start=owner.start_time)

    def processes(self) -> dict[int, gate.ProcessIdentity]:
        return gate.process_table(self.path)

    def owner_start(self, pid: int) -> service.LiveOwnerIdentity | service.OwnerProcessGone:
        start = self.fixture_owners[pid] if pid in self.fixture_owners else records.process_start_time(pid)
        return service.OwnerProcessGone() if start is None else service.LiveOwnerIdentity(start)

    def pidfd(self, pid: int) -> int:
        return os.pidfd_open(os.getpid() if pid in self.fixture_owners else pid)

    def lifetime(self, row: records.Reservation, owner_live: bool) -> gate.StepLifetime:
        return gate.reservation_lifetime(row, owner_live)

    def kill(self, owner: wire.Owner) -> None:
        child = next(child for child in self.children if child.pid == owner.pid)
        child.kill()
        _ = child.wait(timeout=2)

    def close(self) -> None:
        for child in self.children:
            if child.poll() is None:
                child.kill()
            _ = child.wait(timeout=2)


class PrivateReadings:
    """Count each real gate reader, against only the fixture's fake files."""
    def __init__(self, root: Path) -> None:
        self.root: Path = root
        self.host_path: Path = root / "meminfo"
        self.counts: Counter[str] = Counter()

    def host(self) -> records.HostMemory | records.ReadingUnavailable:
        self.counts["host"] += 1
        try:
            return gate.read_meminfo(self.host_path)
        except Exception as error:
            return records.ReadingUnavailable(str(error))

    def pressure(self, host: records.HostMemory, policy: records.AdmissionPolicy) -> records.Pressure:
        self.counts["pressure"] += 1
        return gate.read_pressure(host, policy=policy)

    def compilers(self) -> list[records.StepMemory]:
        self.counts["compilers"] += 1
        return gate.sccache_memory()

    def reservation(self, path: Path, row: records.Reservation, now: datetime,
                    processes: dict[int, gate.ProcessIdentity], proc: Path,
                    policy: records.AdmissionPolicy) -> records.StepMemory:
        self.counts[f"reservation:{row['pid']}"] += 1
        return gate.reservation_memory(path, row, now, processes, proc, policy)


class SocketPeer:
    """A protocol connection pumped by the same thread as the epoll server."""
    def __init__(self, fixture: AdmissionFixture, request: wire.Request | str) -> None:
        self.fixture: AdmissionFixture = fixture
        self.socket: socket.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(3)
        self.socket.connect(str(fixture.socket_path))
        self.socket.sendall((request if isinstance(request, str) else wire.encode_request(request)).encode())
        self.socket.setblocking(False)
        self.buffer: bytes = b""

    def replies(self) -> list[wire.Reply]:
        for _ in range(4):
            self.fixture.server.run_once(0)
        deadline = time.monotonic() + 3
        while select.select([self.socket], [], [], 0)[0]:
            if time.monotonic() >= deadline:
                raise AssertionError("the private socket did not finish sending within 3 seconds")
            chunk = self.socket.recv(65536)
            if not chunk:
                break
            self.buffer += chunk
        lines = self.buffer.split(b"\n")
        self.buffer = lines.pop()
        return [wire.parse_reply(line) for line in lines]

    def close(self) -> None:
        self.socket.close()


class AdmissionFixture(ExitStack):
    """Every server test uses this temporary state, socket, clock and owner set."""
    def __init__(self, *, root: Path | None = None, log_dir: Path | None = None,
                 skip_idle_spacing: bool = False) -> None:
        super().__init__()
        if not hasattr(os, "pidfd_open") or not Path("/sys/fs/cgroup/cgroup.controllers").exists():
            raise unittest.SkipTest("admission service needs Linux pidfd and cgroup v2")
        if configuration := os.environ.get("CONF_DIR"):
            _ = config_environment(Path(configuration))
        self.root: Path = root if root is not None else self.enter_context(
            build_folders.temporary_build_folder(build_folders.UniqueBuildFolder(
                prefix=build_folders.FOLDER_PREFIXES["admit-test"])))
        # AF_UNIX addresses have a small byte limit; TMPDIR may be a deep merge checkout.
        self.socket_dir: Path = Path(self.enter_context(
            short_socket_directory(build_folders.FOLDER_PREFIXES["admit-sock"])))
        self.state_dir: Path = (log_dir if log_dir is not None else self.root / "log") / "admission"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.socket_path: Path = self.socket_dir / "admission.sock"
        self.scenery_reservations: set[str] = set()
        self.skip_idle_spacing: bool = skip_idle_spacing
        self.clock: FakeClock = FakeClock()
        self.fault: Faults = Faults()
        self.proc: TestProcesses = TestProcesses(self.root / "proc")
        _ = self.callback(self.proc.close)
        pressure = self.root / "pressure"
        if not pressure.exists():
            write_pressure_reading(pressure)
        memory = ({"BUILDLOG_MEMINFO": str(self.root / "meminfo"),
                   "BUILDLOG_MEMORY_PRESSURE": str(self.root / "pressure")}
                  if (self.root / "meminfo").exists() else fake_memory_readings(
                      self.root, available_kib=60 * GIB // 1024, total_kib=60 * GIB // 1024))
        environment = {**gate_isolation_environment(self.root), **memory,
            "BUILDLOG_BUILDS_CGROUP": str(self.root / "builds.slice"),
            "BUILDLOG_LIVE_MEMORY_TRIPWIRE": str(self.root / "live-memory-reads.jsonl"),
            "LINT_CONFIG_READER": str(Path(__file__).resolve().parents[1] / "lint/lint_config.sh"),
            "ADMISSION_SOCKET": str(self.socket_path), "BUILDLOG_DIR": str(self.state_dir.parent),
            "BUILDLOG_PROC": str(self.proc.path), "LINT_CONFIG_FILE": str(self.root / "lint.conf"),
            "BUILDLOG_RUN": "",
            "NOTIFY_SOCKET": "", "LISTEN_FDS": "",
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"), "USER_ZONE": "UTC",
            "HOME": str(self.root)}
        self.enter_context(mock.patch.dict(os.environ, environment))
        if not (self.root / "build_admission.conf").exists():
            write_memory_config(self.root / "build_admission.conf")
        self.readings: PrivateReadings = PrivateReadings(self.root)
        self.turn_gate: threading.Lock = threading.Lock()
        self.loop_access: threading.RLock = threading.RLock()
        self.serving_active: threading.Event = threading.Event()
        self.loop_permission: threading.local = threading.local()
        try:
            self.server: ObservedAdmissionServer = self.new_server()
        except BaseException:
            self.close()
            raise
        _ = self.callback(lambda: self.server.close())

    def new_server(self, *, start: bool = True) -> ObservedAdmissionServer:
        server = ObservedAdmissionServer(self.state_dir, self.clock, self.readings, self.proc,
                                         fault=self.fault, socket_path=self.socket_path, check_access=self.require_loop_turn,
                                         before_admission=self.advance_idle_spacing)
        if start:
            server.start()
        return server

    def restart(self) -> None:
        self.server.close()
        self.server = self.new_server()

    @contextmanager
    def serving(self) -> Generator[AdmissionFixture]:
        """Run the private loop while a caller uses the socket or starts a child."""
        stopped = threading.Event()
        failures: list[BaseException] = []

        real_monotonic = time.monotonic

        def serve() -> None:
            self.loop_permission.serves_loop = True
            previous = real_monotonic()
            try:
                while not stopped.is_set():
                    current = real_monotonic()
                    self.clock.advance(current - previous)
                    previous = current
                    with self.loop_turn():
                        self.server.run_once(SERVING_POLL_SECONDS)
            except BaseException as error:
                failures.append(error)
                stopped.set()

        worker = threading.Thread(target=serve, name="private-admission-loop")
        self.serving_active.set()
        worker.start()
        try:
            yield self
        finally:
            stopped.set()
            self.server.wake_loop()
            worker.join(timeout=TEST_HANG_GUARD_SECS)
            if worker.is_alive():
                raise AssertionError(f"the private admission loop did not stop within {TEST_HANG_GUARD_SECS} seconds")
            self.serving_active.clear()
            if failures:
                raise failures[0]

    def run_pass(self) -> gate.PassResult:
        """Keep a test pass separate from the private loop's current event."""
        with self.loop_turn():
            result = self.server.run_pass()
            if isinstance(result, gate.UnusableAdmissionConfiguration):
                raise AssertionError(result.reason)
            return result

    def require_loop_turn(self, method: str) -> None:
        if self.serving_active.is_set() and not getattr(self.loop_permission, "depth", 0):
            raise AssertionError(f"{method}: use loop_turn() while the fixture is serving")

    @contextmanager
    def loop_turn(self) -> Generator[None]:
        depth = getattr(self.loop_permission, "depth", 0)
        if depth == 0 and not self.turn_gate.acquire(timeout=TEST_HANG_GUARD_SECS):
            raise AssertionError(f"the private admission loop did not yield within {TEST_HANG_GUARD_SECS} seconds")
        try:
            if depth == 0 and self.serving_active.is_set() and not getattr(self.loop_permission, "serves_loop", False):
                self.server.wake_loop()
            if not self.loop_access.acquire(timeout=TEST_HANG_GUARD_SECS):
                raise AssertionError(f"the private admission loop did not yield within {TEST_HANG_GUARD_SECS} seconds")
        finally:
            if depth == 0:
                self.turn_gate.release()
        self.loop_permission.depth = depth + 1
        try:
            yield
        finally:
            self.loop_permission.depth -= 1
            self.loop_access.release()

    def configure(self, **seconds: float) -> None:
        write_memory_config(self.root / "build_admission.conf", **{name: str(value) for name, value in seconds.items()})

    def scope_events(self) -> ScopeEvents:
        events = ScopeEvents(self.server.epoll)
        _ = self.enter_context(mock.patch.object(self.server, "epoll", events))
        return events

    def kill_socket_server(self) -> None:
        """Leave the Unix socket inode behind by killing a real server process."""
        self.server.close()
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                _ = self.new_server()
                _ = os.write(write_fd, b"ready")
                signal.pause()
            finally:
                os._exit(1)
        os.close(write_fd)
        try:
            if not select.select([read_fd], [], [], 3)[0] or os.read(read_fd, 5) != b"ready":
                raise AssertionError("child service did not bind its socket")
        finally:
            os.close(read_fd)
            os.kill(pid, signal.SIGKILL)
            _ = os.waitpid(pid, 0)

    def fixture_owner(self, pid: int, start: str) -> wire.Owner:
        """Keep a caller's private process record alive without using a host PID."""
        owner = wire.Owner(pid, start)
        self.proc.fixture_owners[pid] = start
        self.proc.register(owner)
        return owner

    def run_owner(self) -> wire.RunIdentity:
        """Give a verify run its own process and real pidfd lifetime."""
        owner = self.proc.spawn(used=0)
        return wire.RunIdentity(owner.pid, owner.start_time)

    def end_run(self, run: wire.RunIdentity) -> None:
        """Deliver the run's real exit event while its child steps remain alive."""
        self.end_owner(wire.Owner(run.pid, run.start_time))

    def end_owner(self, owner: wire.Owner) -> None:
        """End a real fixture process and deliver its exit event."""
        self.proc.kill(owner)
        self.server.run_once(0)

    def finish_step(self, request: wire.AdmitRequest, *, end_owner: bool = False) -> wire.Admitted:
        """Admit and release a step without retaining its memory charge."""
        self.allow_next_start()
        with self.loop_turn():
            granted = only_admitted(self.server.handle_request(request))
            self.release(granted)
        if end_owner:
            self.end_owner(request.owner)
        return granted

    def queue_pids(self) -> list[int]:
        """Read the queue order through the public view request."""
        with self.loop_turn():
            reply, = self.server.handle_request(wire.ViewRequest())
        assert isinstance(reply, wire.ViewReply)
        return [row["pid"] for _, row in reply.reading.ordered]

    def end_fixture_owner(self, pid: int) -> None:
        with self.loop_turn():
            del self.proc.fixture_owners[pid]
            events = ScopeEvents(self.server.epoll)
            events.events = [(descriptor, select.EPOLLIN)
                for descriptor, watch in self.server.watchers.items()
                if isinstance(watch, service.OwnerWatch) and watch.owner.pid == pid]
            with mock.patch.object(self.server, "epoll", events):
                self.server.run_once(0)

    def build_history_generation(self) -> records.BuildHistoryGeneration:
        with self.loop_turn():
            reply, = self.server.handle_request(wire.BuildHistoryRequest())
        assert isinstance(reply, wire.BuildHistoryReply)
        return records.BuildHistoryGeneration(reply.generation)

    def request(self, *, need: int = 8 * GIB, owner: wire.Owner | None = None,
                episode: str = "", since: float | None = None, age: float = 0,
                width: records.TestWidth = DEFAULT_WIDTH,
                call: records.Call = DEFAULT_CALL, step: str = "nextest",
                run: wire.RunIdentity | wire.NoRun = wire.NO_RUN) -> wire.AdmitRequest:
        from scripts.build_queue.admission_client import LOADED_ADMISSION_CODE_VERSION

        owner = owner or self.proc.spawn()
        generation: records.BuildHistoryGeneration | records.NoBuildHistoryGeneration = records.NoBuildHistoryGeneration()
        if step == "build":
            generation = self.build_history_generation()
        profile = records.StepProfile("fixture", step, "tree", records.ExpectedPeak(need, "measured", 5),
            estimated_at=self.clock.now() - age, build_history_generation=generation)
        return wire.AdmitRequest(episode or f"episode-{owner.pid}", owner, ("cargo", step),
            str(self.root), self.clock.now() if since is None else since, "",
            wire.Identity(f"session-{owner.pid}", "fixture", step, str(self.root)), call, width,
            profile, False, run=run, code_version=LOADED_ADMISSION_CODE_VERSION)

    def launch(self, *, need: int = 8 * GIB, since: float | None = None) -> wire.LaunchRequest:
        request = self.request(need=need, since=since)
        return wire.LaunchRequest(request.episode, request.owner, request.since,
                                  request.identity, request.profile, code_version=request.code_version)

    def service_request(self, *, runner: str = "runner-a", job: str = "compile",
                        owner: wire.Owner | None = None) -> wire.ServiceEstimateRequest:
        """Request a service estimate without a client profile or history read."""
        from scripts.build_queue.admission_client import LOADED_ADMISSION_CODE_VERSION

        owner = owner or self.proc.spawn()
        sidecar = self.root / f"runner-{owner.pid}.cgroup"
        _ = sidecar.write_text("")
        return wire.ServiceEstimateRequest(f"uid:{os.getuid()}:ci-{owner.pid}", owner,
            ("cargo", "build"), str(self.root), self.clock.now(), str(sidecar),
            records.RepositoryIdentity("fixture-repository", "fixture"), str(self.root), runner, job,
            code_version=LOADED_ADMISSION_CODE_VERSION)

    def account_owner(self, uid: int) -> wire.Owner:
        """Supply an account identity through the private process status reader."""
        owner = self.proc.spawn()
        status = self.proc.path / str(owner.pid) / "status"
        _ = status.write_text(status.read_text() + f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
        return owner

    @contextmanager
    def peer_account(self, uid: int) -> Generator[None]:
        """Select credentials at the socket boundary without a real runner account."""
        with mock.patch.object(service, "peer_uid", return_value=uid):
            yield

    def connect(self, request: wire.Request | str) -> SocketPeer:
        peer = SocketPeer(self, request)
        _ = self.callback(peer.close)
        return peer

    def completed_queue_change(self, request: wire.Request) -> list[wire.Reply]:
        """Run the private loop until this change replies through its socket."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.connect(str(self.socket_path))
            peer.sendall(wire.encode_request(request).encode())
            with self.loop_turn():
                while not select.select([peer], [], [], 0)[0]:
                    self.server.run_once(float("inf"))
            with peer.makefile("rb") as stream:
                return [wire.parse_reply(stream.readline())]

    def occupy_host(self) -> wire.Admitted:
        """Keep one measured run active without another memory keep-aside."""
        with self.loop_turn():
            request = self.request(need=GIB, run=self.run_owner())
            request = replace(request, sidecar=str(self.scope(request.owner)))
            grant = only_admitted(self.server.handle_request(request))
            self.scenery_reservations.add(grant.reservation)
            self.allow_next_start()
            return grant

    def caller_reservations(self) -> dict[str, service.ReservationRecord]:
        """Return caller reservations without the occupied-host scenery."""
        with self.loop_turn():
            return {episode: record for episode, record in self.server.reservations.items()
                    if record.reservation_id not in self.scenery_reservations}

    def callers_released(self) -> bool:
        """Check that every caller released its wait and reservation."""
        with self.loop_turn():
            return not self.server.waiters and not self.caller_reservations()

    def hold_starts(self, *, unreadable: bool = False) -> None:
        """Hold new starts without creating a running reservation."""
        path = self.root / "pressure"
        if unreadable:
            path.unlink()
        else:
            write_pressure_reading(path, full_avg10=99)

    def release(self, grant: wire.Admitted) -> None:
        """Release one grant through the private service loop."""
        with self.loop_turn():
            replies = self.server.handle_request(wire.ReleaseRequest(
                f"release-{grant.reservation}", grant.reservation, 0))
        if replies != [wire.Ok()]:
            raise AssertionError(replies)

    def allow_next_start(self) -> None:
        """Move the fake clock to the next permitted new start."""
        with self.loop_turn():
            spacing = self.server.spacing
            if isinstance(spacing, records.NextStartAt) and spacing.at > self.clock.now():
                self.clock.advance(spacing.at - self.clock.now())

    def advance_idle_spacing(self) -> None:
        """Skip start spacing without changing the clock when no caller remains live."""
        if self.skip_idle_spacing and not self.server.caller_live(self.scenery_reservations):
            self.server.spacing = records.ReadyToStart()

    def free(self, available: int) -> None:
        _ = fake_memory_readings(self.root, available_kib=available // 1024,
                                 total_kib=60 * GIB // 1024)

    def compiler(self, used: int) -> None:
        path = self.root / "builds.slice/sccache.service/memory.stat"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(f"anon {used}\nshmem 0\ninactive_file 0\n")
        _ = (path.parent / "memory.current").write_text(str(used))

    def history(self) -> list[dict[str, object]]:
        path = self.state_dir / "anon_peaks.jsonl"
        return list(step_estimator.anonymous_history_rows(path))


    def state_text(self) -> str:
        return (self.state_dir / "state.json").read_text()

    def transient_scope(self) -> tuple[wire.Owner, Path, Callable[[], None]]:
        """A real kernfs scope event, with all memory readings still supplied privately."""
        unit = f"admission-fixture-{uuid.uuid4().hex}.scope"
        probe = subprocess.run(["systemctl", "--user", "show-environment"],
            capture_output=True, timeout=3, check=False)
        if probe.returncode:
            raise unittest.SkipTest("real transient scope needs a user systemd manager")
        child: subprocess.Popen[bytes] = subprocess.Popen(["systemd-run", "--user", "--scope", "--quiet", "--expand-environment=no", f"--unit={unit}",
            "sh", "-c", "echo $$; sleep 300 & wait"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        def stop() -> None:
            _ = subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True,
                               timeout=3, check=False)
            if child.poll() is None:
                child.kill()
            _ = child.wait(timeout=3)
            if child.stdout is not None:
                child.stdout.close()
        _ = self.callback(stop)
        assert child.stdout is not None
        if not select.select([child.stdout], [], [], 3)[0]:
            raise unittest.SkipTest("user systemd cannot start a transient scope")
        line = cast(bytes, child.stdout.readline()).strip()
        if not line.isdigit():
            raise unittest.SkipTest("user systemd refused the transient scope")
        pid = int(line)
        start = records.process_start_time(pid)
        if start is None:
            raise RuntimeError("scope shell exited before the test")
        query = subprocess.run(["systemctl", "--user", "show", "--property=ControlGroup", "--value", unit],
                               capture_output=True, text=True, timeout=3, check=True)
        directory = Path("/sys/fs/cgroup") / query.stdout.strip().lstrip("/")
        self.proc.starts[pid] = start
        write_process(self.proc.path, pid, GIB)
        return wire.Owner(pid, start), directory, stop

    def scope(self, owner: wire.Owner, *, folder: str = "", members: tuple[int, ...] | None = None,
              current: int = GIB, anonymous: int = GIB, swap: int = 0) -> Path:
        directory = self.root / (folder or f"scope-{owner.pid}")
        write_scope(directory, members=(owner.pid,) if members is None else members,
                    current=current, anonymous=anonymous, swap=swap)
        sidecar = self.root / f"sample-{owner.pid}.cgroup"
        _ = sidecar.write_text(str(directory))
        return sidecar

    def scoped_peak(self, admitted: wire.Admitted, peak: int = 8 * GIB) -> None:
        # The finalizer records only sampled scoped steps; give it the same evidence as a sampler.
        _ = Path(admitted.max_path).write_text(str(peak))


def only_admitted(replies: list[wire.Reply]) -> wire.Admitted:
    admissions = [reply for reply in replies if isinstance(reply, wire.Admitted)]
    if len(admissions) != 1:
        raise AssertionError(f"expected one admission, got {replies!r}")
    return admissions[0]


class TimedChunk:
    def __init__(self, seconds: float, data: bytes) -> None:
        self.seconds: float = seconds
        self.data: bytes = data


class Exchange:
    """One peer connection's sends, silence, closure or connect failure."""
    def __init__(self, *events: bytes | TimedChunk | Exception,
                 connect_error: OSError | None = None) -> None:
        self.events: list[bytes | TimedChunk | Exception] = list(events)
        self.connect_error: OSError | None = connect_error


class ScriptSocket:
    def __init__(self, transport: ScriptedTransport, exchange: Exchange) -> None:
        self.transport: ScriptedTransport = transport
        self.exchange: Exchange = exchange
        self.timeout: float = 0

    def __enter__(self) -> ScriptSocket:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout
        self.transport.timeouts.append(timeout)

    def connect(self, path: str) -> None:
        self.transport.paths.append(path)
        if self.exchange.connect_error is not None:
            raise self.exchange.connect_error

    def sendall(self, data: bytes) -> None:
        self.transport.requests.append(wire.parse_request(data))

    def recv(self, _size: int) -> bytes:
        if not self.exchange.events:
            raise AssertionError("client made an unexpected extra read")
        event = self.exchange.events.pop(0)
        if isinstance(event, TimedChunk):
            self.transport.clock.advance(event.seconds)
            return event.data
        if isinstance(event, TimeoutError):
            self.transport.clock.advance(self.timeout)
        if isinstance(event, Exception):
            raise event
        return event

    def close(self) -> None:
        pass


class ScriptedTransport:
    """The client's real framing and retry loop, with deterministic peer faults."""
    def __init__(self, clock: FakeClock, *exchanges: Exchange) -> None:
        self.clock: FakeClock = clock
        self.exchanges: list[Exchange] = list(exchanges)
        self.requests: list[wire.Request] = []
        self.paths: list[str] = []
        self.timeouts: list[float] = []
        self.attempts: list[float] = []

    def socket(self, *_args: object, **_kwargs: object) -> ScriptSocket:
        self.attempts.append(self.clock.monotonic())
        if not self.exchanges:
            raise AssertionError("client made an unexpected extra connection")
        return ScriptSocket(self, self.exchanges.pop(0))


def reply_bytes(reply: wire.Reply) -> bytes:
    return wire.encode_reply(reply).encode()


def queue_decision(available: int, total: int | records.UnknownHostTotal, need: int,
                   running: list[records.StepMemory], pressure: records.Pressure = records.NO_PRESSURE_HOLD,
                   *, policy: records.AdmissionPolicy = FIXTURE_POLICY) -> records.AdmissionDecision:
    """Check a queued request while a held slot prevents the alone exception."""
    steps: list[records.RunningStep] = []
    reservations: list[records.Reservation] = []
    for index, memory in enumerate(running, 1):
        promise = (memory.bytes if isinstance(memory, records.PromisedMemory) else
                   memory.promise.bytes if isinstance(memory, records.MeasuredMemory)
                   and not isinstance(memory.promise, records.NoPromise) else 0)
        step = records.RunningStep(str(index), "fixture", "check", "/fixture", promise, memory, pid=index)
        steps.append(step)
        if gate.running_has_reservation(step):
            reservations.append(fixture_reservation(index, promise))
    slots = () if reservations else (blocking_slot(NOW),)
    row: records.Waiter = {"pid": 1000, "start_time": "1000", "need": need, "since": NOW}
    inputs = records.QueueInputs([(Path("1000.waiting"), row)], tuple(steps), tuple(reservations),
        records.HostMemory(available, total), pressure, records.ReadyToStart(), records.NoHeadAdmissionOverride(), NOW, slots=slots)
    return gate.admission_pass(inputs, NOW, policy).rows[0].decision


def wait_reply(reason: records.WaitReason, position: int = 1, head: str = "head",
               text: str = "free memory") -> wire.Wait:
    """Build a held reply with the same decision fields as a real pass."""
    peak = records.ExpectedPeak(8 * GIB, "fallback", 0)
    decision = queue_decision(0, 60 * GIB, peak.bytes, [], policy=admission_policy_fixture())
    snapshot = records.AdmissionWaitSnapshot(decision, peak, (), 0, "repo", "nextest",
                                         admission_policy_fixture(), ())
    return wire.Wait(reason, position, head, text, snapshot)


def snapshot_with_head_cause(snapshot: records.AdmissionWaitSnapshot,
                             cause: records.HeadCause) -> records.AdmissionWaitSnapshot:
    """Supply the cause that a real admission pass captures."""
    status = records.HeadAdmissionStatus(snapshot.decision, snapshot.peak.bytes,
                                      snapshot.available, cause=cause)
    return replace(snapshot, head_reason=status)


def admitted_reply(reservation: str = "stem", need: int = GIB, source: str = "new-source",
                   max_path: str = "stem.max", wave_path: str = "stem.wave") -> wire.Admitted:
    """Supply the grant context used by sampler and client protocol tests."""
    return wire.Admitted(reservation, need, source, max_path, wave_path,
                         {"reservation_id": reservation, "need": need})


_caller_services: dict[str, AdmissionFixture] = {}


def caller_admission(test: unittest.TestCase, root: Path, environment: dict[str, str],
                     *, skip_idle_spacing: bool = True) -> AdmissionFixture:
    """Use the one server for existing caller fixtures and their child processes."""
    existing = _caller_services.get(environment.get("ADMISSION_SOCKET", ""))
    if existing is not None and existing.root == root and not existing.server.closed:
        if not skip_idle_spacing:
            existing.skip_idle_spacing = False
        return existing
    _ = test.enterContext(LauncherHangGuard(test.id()).protect())
    fixture = test.enterContext(AdmissionFixture(root=root, log_dir=Path(environment["BUILDLOG_DIR"]),
                                               skip_idle_spacing=skip_idle_spacing))
    _caller_services[str(fixture.socket_path)] = fixture
    test.addCleanup(_caller_services.pop, str(fixture.socket_path))
    fixture.clock.epoch = time.time()
    launcher = Path.home() / ".claude/scripts/lib/py"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    if not launcher.exists():
        launcher.symlink_to(Path(__file__).resolve().parents[1] / "lib/py")
    _ = environment.setdefault("LINT_CONFIG_READER", os.environ["LINT_CONFIG_READER"])
    for key in ("USER_ZONE", "ADMISSION_SOCKET", "LINT_CONFIG_FILE", "BUILDLOG_PROC", "BUILDLOG_MEMINFO",
                "BUILDLOG_MEMORY_PRESSURE", "BUILDLOG_BUILDS_CGROUP", "BUILDLOG_LIVE_MEMORY_TRIPWIRE"):
        environment[key] = os.environ[key]
    _ = fixture.enter_context(fixture.serving())
    return fixture
