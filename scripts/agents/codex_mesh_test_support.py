"""Private homes, clocks and app-server fixtures for worker tests."""
from __future__ import annotations

import base64
import contextlib
import dataclasses
import functools
import json
import os
import pwd
import select
import socket
import struct
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import cast, final
from unittest.mock import patch

from scripts.accounts import role_map
from scripts.lib.test_hang_guard import LauncherHangGuard, TEST_HANG_GUARD_SECS
from scripts.lib.signal_test_support import install_signal_pid_guard
from scripts.lib.test_models_registry import write_registry
from scripts.message import process_table
from scripts.message.repeat_guard_test_support import install_guard_test_budget
from scripts.production import staff, showrunners
from scripts.production.staff_test_support import fixture_runtime_id, fixture_session, source_environment
from scripts.whoami import agent_accounts

REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
THREAD_ID = fixture_runtime_id("codex-mesh-thread")

def fixture_write_metadata(home: Path) -> dict[Path, tuple[int, int]]:
    """Observe the synthetic sign-in's three files, never account contents or live trees.

    Other home paths change during normal work (account moves, a pull of ~/.claude), so the
    real-home refusal guards them; a snapshot of them would fail gates no test caused.
    """
    paths = [root / "auth.json" for root in home.glob(".codex*") if root.is_dir()]
    # note_file.notes_dir's production default, anchored to passwd home rather than HOME.
    paths.extend((home / ".claude/config/accounts.json", home / "rust/hanadocs/agents/codex9.md"))
    metadata: dict[Path, tuple[int, int]] = {}
    for path in paths:
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        metadata[path] = (info.st_size, info.st_mtime_ns)
    return metadata


REAL_FIXTURE_METADATA = fixture_write_metadata(REAL_HOME)


def assert_real_codex_unchanged() -> None:
    current = fixture_write_metadata(REAL_HOME)
    changed = sorted(str(path) for path in REAL_FIXTURE_METADATA.keys() | current.keys()
                     if REAL_FIXTURE_METADATA.get(path) != current.get(path))
    if changed:
        raise AssertionError("codex_mesh tests changed real-home fixture targets:\n" + "\n".join(changed))


def refuse_real_home(path: Path, source: str) -> None:
    if path.resolve().is_relative_to(REAL_HOME):
        raise AssertionError(f"{source} points into the real home: {path}; use a fixture temp home (setUpModule required)")


def assert_fixture_environment() -> None:
    for name in ("HOME", "CODEX_HOME", "AGENT_NOTES_DIR", "MODELS_CONFIG_FILE"):
        value = os.environ.get(name)
        if not value:
            raise AssertionError(f"{name} is unset; codex_mesh fixture requires setUpModule")
        refuse_real_home(Path(value), name)


@dataclasses.dataclass
class FixtureLoopBudget:
    """Fail a repeating fixture before fake time or retained calls exhaust memory."""

    name: str
    seconds: float = TEST_HANG_GUARD_SECS
    max_calls: int = 2_000
    clock: Callable[[], float] = time.perf_counter
    started: float = dataclasses.field(init=False)
    hang_guard: LauncherHangGuard = dataclasses.field(init=False)
    calls: int = 0

    def __post_init__(self) -> None:
        self.started = self.clock()
        self.hang_guard = LauncherHangGuard(self.name)

    def check_calls(self) -> None:
        self.calls += 1
        if self.calls > self.max_calls:
            raise AssertionError(f"{self.name}: exceeded {self.max_calls} fixture calls")

    def check(self) -> float:
        self.check_calls()
        remaining = min(self.hang_guard.remaining(), self.seconds - (self.clock() - self.started))
        if remaining <= 0:
            raise AssertionError(f"{self.name}: exceeded {self.seconds}s fixture deadline")
        return remaining


@dataclasses.dataclass
class LifecycleTestClock:
    """Use one registry-free clock for worker deadlines and submission readiness."""

    registry_seconds: float = 0.0
    registry_calls: int = 0
    registry_started: float = 0.0
    lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)
    controlled: bool = False
    wall_seconds: float = 0.0
    monotonic_seconds: float = 0.0

    def freeze(self) -> None:
        # Whole-second seeds keep scripted deadlines exact. A fractional seed
        # rounds (seed + 600) - seed to 599.9999999999999 for about one seed in
        # ten while the monotonic clock is small, as it is after a reboot.
        self.wall_seconds = float(int(self.time()))
        self.monotonic_seconds = float(int(self.monotonic()))
        self.controlled = True

    def advance(self, seconds: float) -> None:
        if not self.controlled:
            raise AssertionError("freeze the lifecycle clock before advancing it")
        self.wall_seconds += seconds
        self.monotonic_seconds += seconds

    def registry_elapsed(self) -> float:
        with self.lock:
            running = time.perf_counter() - self.registry_started if self.registry_calls else 0.0
            return self.registry_seconds + running

    def time(self) -> float:
        if self.controlled:
            return self.wall_seconds
        return time.time() - self.registry_elapsed()

    def monotonic(self) -> float:
        if self.controlled:
            return self.monotonic_seconds
        return time.monotonic() - self.registry_elapsed()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def registry_resolution[**P, R](self, resolve: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(resolve)
        def measured(*args: P.args, **kwargs: P.kwargs) -> R:
            with self.lock:
                if not self.registry_calls:
                    self.registry_started = time.perf_counter()
                self.registry_calls += 1
            try:
                return resolve(*args, **kwargs)
            finally:
                with self.lock:
                    self.registry_calls -= 1
                    if not self.registry_calls:
                        self.registry_seconds += time.perf_counter() - self.registry_started

        return measured

    def wait_for_submission(self, ready: threading.Event, *, timeout: float) -> bool:
        """Keep the existing readiness bound, excluding real registry setup only."""
        deadline = self.monotonic() + timeout
        budget = FixtureLoopBudget("submission readiness")
        while not ready.is_set():
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                return ready.is_set()
            _ = ready.wait(timeout=min(remaining, budget.check()))
        return True

    def result[T](self, future: Future[T], *, timeout: float) -> T:
        ready = threading.Event()
        future.add_done_callback(lambda _future: ready.set())
        if not self.wait_for_submission(ready, timeout=timeout):
            raise TimeoutError("worker did not finish within its lifecycle deadline")
        return future.result()


def bounded_fixture[**P, R](callback: Callable[P, R]) -> Callable[P, R]:
    # Created inside each test: no call histories or budgets survive across tests.
    budget = FixtureLoopBudget(callback.__qualname__)

    @functools.wraps(callback)
    def bounded(*args: P.args, **kwargs: P.kwargs) -> R:
        _ = budget.check()
        return callback(*args, **kwargs)

    return bounded


def fixture_skip_sleep(_seconds: float) -> None:
    pass


def fixture_no_sleep(_seconds: float) -> None:
    raise AssertionError("fixture must not sleep")


def set_environment(name: str, value: str) -> Callable[[], None]:
    """Set one environment variable and return the call that puts it back."""
    previous = os.environ.get(name)
    os.environ[name] = value

    def restore() -> None:
        if previous is None:
            _ = os.environ.pop(name, None)
        else:
            os.environ[name] = previous

    return restore


def select_fixture_workers(home: Path, *, notes: Path) -> None:
    """Give fake-server tests an explicit selection backed by a synthetic sign-in."""
    default = home / ".codex"
    auth = default / "auth.json"
    config = home / ".claude/config/accounts.json"
    for source, path in (("fixture home", home), ("fixture sign-in", auth),
                         ("fixture note", notes / "codex9.md"), ("fixture account config", config)):
        refuse_real_home(path, source)
    default.mkdir(exist_ok=True)
    login = "mesh-fixture@example.invalid"
    claims = base64.urlsafe_b64encode(json.dumps({"email": login}).encode()).decode().rstrip("=")
    _ = auth.write_text(json.dumps({"tokens": {"id_token": f"header.{claims}.signature"}}))
    os.utime(auth, (1, 1))
    notes.mkdir(parents=True, exist_ok=True)
    _ = (notes / "codex9.md").write_text(f"---\nlogin: {login}\n---\n")
    config.parent.mkdir(parents=True, exist_ok=True)
    _ = config.write_text('{"workers": "codex9"}')


def setUpModule() -> None:
    install_signal_pid_guard()
    install_guard_test_budget()
    # Registered first, checked last; unittest reports cleanup assertions as module errors.
    unittest.addModuleCleanup(assert_real_codex_unchanged)
    pane_environment = {key: value for key, value in os.environ.items() if key.startswith("HERDR_")}
    for key in pane_environment:
        del os.environ[key]

    def restore_pane_environment() -> None:
        os.environ.update(pane_environment)

    unittest.addModuleCleanup(restore_pane_environment)
    # No test sees a real sign-in; the synthetic selection predates every fake server.
    home = tempfile.TemporaryDirectory(dir="/tmp", prefix="codex-mesh-home-")
    usage = tempfile.TemporaryDirectory()
    restore_usage = set_environment("USAGE_STATE_DIR", usage.name)
    restore_home = set_environment("HOME", home.name)
    temporary_root = patch.object(tempfile, "tempdir", "/tmp")
    _ = temporary_root.start()
    unittest.addModuleCleanup(temporary_root.stop)
    for key in ("CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "UNIT_RUN_DIR",
                "PLAN_DELEGATE_RUN_DIR", "PLAN_DELEGATE_BOARD_DIR"):
        unittest.addModuleCleanup(set_environment(key, ""))
    environment = source_environment(Path(home.name) / "sources")
    for key, directory in environment.items():
        Path(directory).mkdir(parents=True, exist_ok=True)
        unittest.addModuleCleanup(set_environment(key, directory))
    private_staff = Path(home.name) / "staff.json"
    _ = private_staff.write_text(json.dumps({"schema": 2, "rows": []}))
    unittest.addModuleCleanup(set_environment("STAFF_FILE", str(private_staff)))
    unittest.addModuleCleanup(set_environment("STAFF_CONFIG", str(Path(__file__).resolve().parents[2] / "config/staff.conf")))
    unittest.addModuleCleanup(set_environment("XDG_STATE_HOME", str(Path(home.name) / "state")))
    # Account-change notices resolve the user zone through discovery, whose paths
    # were cached at import before HOME changed. Isolate both it and child processes.
    for attribute, variable, directory in (
        ("NOTIFIER_STATE_DIR", "NOTIFIER_STATE_DIR", "notifier"),
        ("SESSIONS_DIR", "NOTIFIER_SESSIONS_DIR", "sessions"),
    ):
        path = Path(home.name) / directory
        path.mkdir()
        unittest.addModuleCleanup(set_environment(variable, str(path)))
        if attribute != "SESSIONS_DIR":
            discovery_path = patch.object(showrunners, attribute, path)
            _ = discovery_path.start()
            unittest.addModuleCleanup(discovery_path.stop)
    restore_notes = set_environment("AGENT_NOTES_DIR", home.name)
    unittest.addModuleCleanup(set_environment("MODELS_CONFIG_PUBLISH", "0"))
    # Tier tests never read the caller's registry, config, or pacer choice.
    tier_files = {
        "MODELS_CONFIG_FILE": str(Path(home.name) / "models.conf"),
        "CODEX_CATALOG_SYNC_STATE_FILE": str(Path(home.name) / "catalog-sync"),
        "CODEX_CONFIG_FILE": str(Path(home.name) / "codex.toml"),
        "CODEX_MODELS_CACHE_FILE": str(Path(home.name) / "models_cache.json"),
        "CODEX_PACER_TIER_FILE": str(Path(home.name) / "pacer-tier"),
    }
    for path in tier_files.values():
        _ = Path(path).write_text("", encoding="utf-8")
    _ = write_registry(Path(tier_files["MODELS_CONFIG_FILE"]))
    for path in tier_files.values():
        os.utime(path, (1, 1))
    os.utime(tier_files["CODEX_CATALOG_SYNC_STATE_FILE"], (2, 2))
    for key, value in tier_files.items():
        unittest.addModuleCleanup(set_environment(key, value))
    authority = patch.object(role_map, "is_role_map_host", new=lambda: True)
    _ = authority.start()
    unittest.addModuleCleanup(authority.stop)
    select_fixture_workers(Path(home.name), notes=Path(home.name))
    previous = os.environ.get("CODEX_HOME")
    os.environ["CODEX_HOME"] = str(Path(home.name) / ".codex")

    def restore() -> None:
        if previous is None:
            _ = os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = previous
        restore_usage()
        restore_home()
        restore_notes()
        usage.cleanup()
        home.cleanup()

    unittest.addModuleCleanup(restore)
    # Holderless legacy fixtures have no synthetic ancestry; never inspect this test runner's parents.
    def fixture_processes() -> dict[int, process_table.Process]:
        return {}

    processes = patch.object(process_table, "snapshot", new=fixture_processes)
    _ = processes.start()
    unittest.addModuleCleanup(processes.stop)


def _read_exact(sock: socket.socket, count: int) -> bytes:
    received = bytearray()
    budget = FixtureLoopBudget("stub socket read", max_calls=100_000)
    while len(received) < count:
        _ = budget.check()
        chunk = sock.recv(count - len(received))
        if not chunk:
            raise ConnectionError("stub client disconnected")
        received.extend(chunk)
    return bytes(received)


def _read_request(sock: socket.socket) -> dict[str, object]:
    header = _read_exact(sock, 2)
    length = header[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", _read_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _read_exact(sock, 8))[0]
    if length > 16 * 1024 * 1024:
        raise AssertionError("stub request exceeded 16 MiB")
    mask = _read_exact(sock, 4)
    data = _read_exact(sock, length)
    decoded = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
    return cast("dict[str, object]", json.loads(decoded))


def _encoded_frame(message: dict[str, object]) -> bytes:
    data = json.dumps(message).encode()
    if len(data) < 126:
        header = bytes((0x81, len(data)))
    elif len(data) < 65536:
        header = bytes((0x81, 126)) + struct.pack(">H", len(data))
    else:
        header = bytes((0x81, 127)) + struct.pack(">Q", len(data))
    return header + data


def _send_frame(sock: socket.socket, message: dict[str, object]) -> None:
    sock.sendall(_encoded_frame(message))


@final
class StubAppServer:
    """A loopback JSON-RPC server with a scripted reply for each request."""

    def __init__(
        self,
        respond: Callable[[dict[str, object]], list[dict[str, object]] | None],
    ) -> None:
        self.respond: Callable[[dict[str, object]], list[dict[str, object]] | None] = respond
        self.requests: list[dict[str, object]] = []
        self.backend_home: Callable[[], Path] = agent_accounts.workers_home
        self.hang_guard: LauncherHangGuard = LauncherHangGuard("stub app-server")
        self.request_budget = FixtureLoopBudget("stub RPC requests")
        self.connection_budget = FixtureLoopBudget("stub connections", max_calls=128)
        self.errors: list[Exception] = []
        self.close_after_turn_start: bool = False
        self.hold_turn_completion: bool = False
        self.coalesce_response_frames: bool = False
        self.held_turn_completes_successfully: bool = False
        self.answer_requests_during_held_turn: bool = False
        self.held_turn_ready: threading.Event = threading.Event()
        self.held_turn_interrupted: threading.Event = threading.Event()
        self.release_held_turn: threading.Event = threading.Event()
        self.block_compact_turn_completion: bool = False
        self.broadcast_compaction: bool = False
        self.subscribers: list[tuple[socket.socket, Callable[[dict[str, object]], None]]] = []
        self.compaction_event_sent: threading.Event = threading.Event()
        self.release_compact_turn_completion: threading.Event = threading.Event()
        self.listener: socket.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.close_reader, self.close_writer = socket.socketpair()
        self.port: int = cast("tuple[str, int]", self.listener.getsockname())[1]
        self.stopping: threading.Event = threading.Event()
        self.accept_waiting: threading.Event = threading.Event()
        self.connections: list[socket.socket] = []
        self.conversations: list[threading.Thread] = []
        self.thread: threading.Thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.stopping.set()
        self.release_compact_turn_completion.set()
        self.release_held_turn.set()
        _ = self.close_writer.send(b"close")
        guard = LauncherHangGuard("stub app-server shutdown")
        guard.join(self.thread)
        self.listener.close()
        for connection in self.connections:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
        self.close_reader.close()
        self.close_writer.close()
        for conversation in self.conversations:
            guard.join(conversation)
            if conversation.is_alive():
                raise AssertionError("stub conversation did not stop before the shared hang deadline")
        if self.thread.is_alive():
            raise AssertionError("stub app-server did not stop")
        if self.errors:
            raise AssertionError(f"stub app-server failed: {self.errors}")

    def _serve(self) -> None:
        while not self.stopping.is_set():
            try:
                self.accept_waiting.set()
                self.connection_budget.check_calls()
                remaining = self.hang_guard.remaining()
                ready, _, _ = select.select([self.listener, self.close_reader], [], [], remaining)
                if not ready:
                    raise AssertionError("stub listener exceeded its real-time fixture deadline")
                if self.close_reader in ready:
                    return
                connection = self.listener.accept()[0]
            except OSError:
                return
            except AssertionError as exc:
                self.errors.append(exc)
                return
            self.connections.append(connection)
            conversation = threading.Thread(
                target=self._handle_connection, args=(connection,), daemon=True
            )
            self.conversations.append(conversation)
            conversation.start()

    def _handle_connection(self, connection: socket.socket) -> None:
        with connection:
            try:
                self._conversation(connection)
            except (ConnectionError, OSError):
                # The client closes a connection after each command.
                pass
            except Exception as exc:
                self.errors.append(exc)

    def _conversation(self, connection: socket.socket) -> None:
        handshake = bytearray()
        handshake_budget = FixtureLoopBudget("stub handshake", max_calls=32)
        while b"\r\n\r\n" not in handshake:
            handshake_budget.check_calls()
            connection.settimeout(self.hang_guard.remaining())
            chunk = connection.recv(4096)
            if not chunk:
                raise ConnectionError("stub client disconnected during handshake")
            handshake.extend(chunk)
        connection.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
        send_lock = threading.Lock()

        def send_frame(frame: dict[str, object]) -> None:
            with send_lock:
                _send_frame(connection, frame)

        while not self.stopping.is_set():
            self.request_budget.check_calls()
            connection.settimeout(self.hang_guard.remaining())
            request = _read_request(connection)
            self.requests.append(request)
            frames = self.respond(request)
            if frames is None:
                return
            if request.get("method") in {"thread/start", "thread/resume", "thread/fork"}:
                for frame in frames:
                    identity = test_object(test_object(frame.get("result")).get("thread")).get("id")
                    if isinstance(identity, str):
                        fixture_session(identity, staff.CodexBackend(self.backend_home()))
            if request.get("method") == "thread/resume":
                self.subscribers.append((connection, send_frame))
            if len(frames) > 20_000:
                raise AssertionError("stub response exceeded 20,000 frames")
            if self.coalesce_response_frames:
                with send_lock:
                    connection.sendall(b"".join(_encoded_frame(frame) for frame in frames))
                continue
            frame_budget = FixtureLoopBudget("stub response frames", max_calls=20_000)
            for frame in frames:
                frame_budget.check_calls()
                connection.settimeout(self.hang_guard.remaining())
                params = test_object(frame.get("params"))
                turn = test_object(params.get("turn"))
                if (
                    self.block_compact_turn_completion
                    and frame.get("method") == "turn/completed"
                    and turn.get("id") == "compact-turn"
                ):
                    self.compaction_event_sent.set()
                    self.hang_guard.event(self.release_compact_turn_completion)
                send_frame(frame)
                if self.broadcast_compaction and request.get("method") == "thread/compact/start" and "method" in frame:
                    for subscriber, notify in tuple(self.subscribers):
                        if subscriber is not connection:
                            notify(frame)
            if self.hold_turn_completion and request.get("method") == "turn/interrupt":
                if any("result" in frame for frame in frames):
                    self.held_turn_interrupted.set()
                    self.release_held_turn.set()
            if self.hold_turn_completion and request.get("method") == "turn/start":
                self.held_turn_ready.set()
                if self.answer_requests_during_held_turn:
                    completion = threading.Thread(target=self._complete_held_turn,
                                                  args=(send_frame, request, frames[0]), daemon=True)
                    self.conversations.append(completion)
                    completion.start()
                else:
                    self._complete_held_turn(send_frame, request, frames[0])
            if request.get("method") == "turn/start" and self.close_after_turn_start:
                return

    def _complete_held_turn(
        self, send_frame: Callable[[dict[str, object]], None],
        request: dict[str, object], reply: dict[str, object],
    ) -> None:
        self.hang_guard.event(self.release_held_turn)
        if self.stopping.is_set():
            return
        params = test_object(request.get("params"))
        result = test_object(reply.get("result"))
        turn_id = test_object(result.get("turn")).get("id")
        if self.held_turn_completes_successfully:
            turn: dict[str, object] = {"id": turn_id, "status": "completed"}
        elif self.held_turn_interrupted.is_set():
            turn = {"id": turn_id, "status": "interrupted"}
        else:
            turn = {"id": turn_id, "error": {"message": "held test turn was not interrupted"}}
        with contextlib.suppress(ConnectionError, OSError):
            send_frame(notice("turn/completed", {"threadId": params["threadId"], "turn": turn}))


def reply(request: dict[str, object], result: dict[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request["id"], "result": result}


def notice(method: str, params: dict[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "method": method, "params": params}


def test_object(value: object) -> dict[str, object]:
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}

