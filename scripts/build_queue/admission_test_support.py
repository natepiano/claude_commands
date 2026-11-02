"""One private admission server, fake clock and fault boundary for service tests."""
from __future__ import annotations

from scripts.lint.memory_test_support import write_config_fixture

from collections import Counter
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import tempfile
import unittest
import uuid
from unittest import mock
from typing import cast

from scripts.lint import memory_admit as gate
from scripts.lint.memory_test_support import fake_memory_readings, gate_isolation_environment
from scripts.build_queue import admission_protocol as wire
from scripts.build_queue import admission_service as service
from scripts.build_queue.queue_fixture import write_process

NOW = datetime(2026, 10, 31, 12, tzinfo=UTC).timestamp()
GIB = gate.GIB
DEFAULT_WIDTH = wire.RequestedTestWidth(8)
DEFAULT_CALL = wire.CallRecord("call-1")


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

        def observe(episode: str, reply: wire.Reply) -> None:
            before_delivery.append(self.waiters[episode].last_reply)
            deliver(episode, reply)

        with mock.patch.object(self, "_deliver", side_effect=observe):
            yield before_delivery


class TestProcesses:
    """Real pidfds with private resident-memory files."""
    def __init__(self, path: Path) -> None:
        self.path: Path = path
        path.mkdir()
        self.children: list[subprocess.Popen[bytes]] = []
        self.starts: dict[int, str] = {}

    def spawn(self, used: int = GIB) -> wire.Owner:
        child = subprocess.Popen(["sleep", "300"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(child)
        start = gate.process_start_time(child.pid)
        if start is None:
            raise RuntimeError("child has no process start time")
        self.starts[child.pid] = start
        write_process(self.path, child.pid, used)
        return wire.Owner(child.pid, start)

    def processes(self) -> dict[int, gate.ProcessIdentity]:
        return {pid: gate.ProcessIdentity(1, start) for pid, start in self.starts.items()
                if self.owner_start(pid) == start}

    def owner_start(self, pid: int) -> str | None:
        return gate.process_start_time(pid)

    def pidfd(self, pid: int) -> int:
        return os.pidfd_open(pid)

    def lifetime(self, row: gate.Reservation, owner_live: bool) -> gate.PopulatedStepScope | gate.LiveOwnerProcess | gate.EndedStep:
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
        self.counts: Counter[str] = Counter()

    def host(self) -> gate.HostMemory:
        self.counts["host"] += 1
        return gate.read_meminfo(self.root / "meminfo")

    def pressure(self, host: gate.HostMemory) -> gate.Pressure:
        self.counts["pressure"] += 1
        return gate.read_pressure(host)

    def compilers(self) -> list[gate.StepMemory]:
        self.counts["compilers"] += 1
        return gate.sccache_memory()

    def reservation(self, path: Path, row: gate.Reservation, now: datetime,
                    processes: dict[int, gate.ProcessIdentity], proc: Path) -> gate.StepMemory:
        self.counts[f"reservation:{row['pid']}"] += 1
        return gate.reservation_memory(path, row, now, processes, proc)


class SocketPeer:
    """A protocol connection pumped by the same thread as the epoll server."""
    def __init__(self, fixture: AdmissionFixture, request: wire.Request | str) -> None:
        self.fixture: AdmissionFixture = fixture
        self.socket: socket.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.connect(str(fixture.socket_path))
        self.socket.sendall((request if isinstance(request, str) else wire.encode_request(request)).encode())
        self.socket.setblocking(False)
        self.buffer: bytes = b""

    def replies(self) -> list[wire.Reply]:
        for _ in range(4):
            self.fixture.server.run_once(0)
        while select.select([self.socket], [], [], 0)[0]:
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
    def __init__(self) -> None:
        super().__init__()
        if not hasattr(os, "pidfd_open") or not Path("/sys/fs/cgroup/cgroup.controllers").exists():
            raise unittest.SkipTest("admission service needs Linux pidfd and cgroup v2")
        self.root: Path = Path(self.enter_context(tempfile.TemporaryDirectory(prefix="admit-test-")))
        # AF_UNIX addresses have a small byte limit; TMPDIR may be a deep merge checkout.
        self.socket_dir: Path = Path(self.enter_context(
            tempfile.TemporaryDirectory(prefix="admit-sock-", dir="/tmp")))
        self.state_dir: Path = self.root / "state"
        self.state_dir.mkdir()
        self.socket_path: Path = self.socket_dir / "admission.sock"
        self.clock: FakeClock = FakeClock()
        self.fault: Faults = Faults()
        self.proc: TestProcesses = TestProcesses(self.root / "proc")
        _ = self.callback(self.proc.close)
        environment = {**gate_isolation_environment(self.root), **fake_memory_readings(
            self.root, available_kib=60 * GIB // 1024, total_kib=60 * GIB // 1024),
            "ADMISSION_SOCKET": str(self.socket_path), "BUILDLOG_DIR": str(self.root / "log"),
            "BUILDLOG_PROC": str(self.proc.path), "LINT_CONFIG_FILE": str(self.root / "lint.conf"), "CONF_DIR": str(Path(self.root / "lint.conf").parent),
            "BUILDLOG_BUILD_BUDGET_GIB": "21", "NOTIFY_SOCKET": "", "LISTEN_FDS": "",
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"), "USER_ZONE": "UTC"}
        self.enter_context(mock.patch.dict(os.environ, environment))
        self.readings: PrivateReadings = PrivateReadings(self.root)
        try:
            self.server: ObservedAdmissionServer = self.new_server()
        except BaseException:
            self.close()
            raise
        _ = self.callback(lambda: self.server.close())

    def new_server(self, *, start: bool = True) -> ObservedAdmissionServer:
        server = ObservedAdmissionServer(self.state_dir, self.clock, self.readings, self.proc,
                                         fault=self.fault, socket_path=self.socket_path)
        if start:
            server.start()
        return server

    def restart(self) -> None:
        self.server.close()
        self.server = self.new_server()

    def configure(self, **seconds: float) -> None:
        _ = write_config_fixture(self.root / "lint.conf", "".join(f"{name}={value}\n" for name, value in seconds.items()))

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

    def request(self, *, need: int = 8 * GIB, owner: wire.Owner | None = None,
                episode: str = "", since: float | None = None, age: float = 0,
                width: wire.TestWidth = DEFAULT_WIDTH,
                call: wire.Call = DEFAULT_CALL, step: str = "nextest") -> wire.AdmitRequest:
        owner = owner or self.proc.spawn()
        profile = gate.StepProfile("fixture", step, "tree", gate.ExpectedPeak(need, "measured", 5),
                                   estimated_at=self.clock.now() - age)
        return wire.AdmitRequest(episode or f"episode-{owner.pid}", owner, ("cargo", step),
            str(self.root), self.clock.now() if since is None else since, "",
            wire.Identity(f"session-{owner.pid}", "fixture", step, str(self.root)), call, width,
            profile, False)

    def launch(self, *, need: int = 8 * GIB, since: float | None = None) -> wire.LaunchRequest:
        request = self.request(need=need, since=since)
        return wire.LaunchRequest(request.episode, request.owner, request.since,
                                  request.identity, request.profile)

    def connect(self, request: wire.Request | str) -> SocketPeer:
        peer = SocketPeer(self, request)
        _ = self.callback(peer.close)
        return peer

    def free(self, available: int) -> None:
        _ = fake_memory_readings(self.root, available_kib=available // 1024,
                                 total_kib=60 * GIB // 1024)

    def compiler(self, used: int) -> None:
        path = self.root / "builds.slice/sccache.service/memory.stat"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(f"anon {used}\n")

    def history(self) -> list[dict[str, object]]:
        path = self.state_dir / "anon_peaks.jsonl"
        return list(gate.anonymous_history_rows(path)) if path.exists() else []

    def state_text(self) -> str:
        return (self.state_dir / "state.json").read_text()

    def transient_scope(self) -> tuple[wire.Owner, Path, Callable[[], None]]:
        """A real kernfs scope event, with all memory readings still supplied privately."""
        unit = f"admission-fixture-{uuid.uuid4().hex}.scope"
        probe = subprocess.run(["systemctl", "--user", "show-environment"],
            capture_output=True, timeout=3, check=False)
        if probe.returncode:
            raise unittest.SkipTest("real transient scope needs a user systemd manager")
        child: subprocess.Popen[bytes] = subprocess.Popen(["systemd-run", "--user", "--scope", "--quiet", f"--unit={unit}",
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
        start = gate.process_start_time(pid)
        if start is None:
            raise RuntimeError("scope shell exited before the test")
        query = subprocess.run(["systemctl", "--user", "show", "--property=ControlGroup", "--value", unit],
                               capture_output=True, text=True, timeout=3, check=True)
        directory = Path("/sys/fs/cgroup") / query.stdout.strip().lstrip("/")
        self.proc.starts[pid] = start
        write_process(self.proc.path, pid, GIB)
        return wire.Owner(pid, start), directory, stop

    def scope(self, owner: wire.Owner) -> Path:
        directory = self.root / f"scope-{owner.pid}"
        directory.mkdir(exist_ok=True)
        for name, text in {"cgroup.procs": str(owner.pid), "cgroup.events": "populated 1\n",
                           "memory.current": str(GIB), "memory.swap.current": "0"}.items():
            _ = (directory / name).write_text(text)
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
