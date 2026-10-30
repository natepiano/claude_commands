"""Tests for codex_mesh's recovery from an app-server that has wedged.

The failure these cover is silent by construction: a stuck app-server replays a
provider message it cached earlier to every thread that attaches, so a local
fault arrives wearing the exact words of a usage limit. What the code can check
is not the message but the circumstances -- who started the server, how fast it
failed, and whether a thread already exists.

ReplyDeliveryTests covers the other silent failure: a worker's own summary
file replaced by its last chat reply.
"""

from __future__ import annotations

from scripts.lib import names
from scripts.lib.test_names import fixture_names

from scripts.whoami.note_file import note_name

import argparse
import base64
import contextlib
import dataclasses
import fcntl
import functools
import io
import json
import os
import pwd
import select
import shlex
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Literal, TextIO, TypedDict, cast, final, override
from unittest.mock import MagicMock, patch

from scripts.agents import codex_mesh
from scripts.direct import plan_history, workers as unit_workers
from scripts.direct.recorder_fixture import invoke_recorder
from scripts.lib import nix_env, unit_runs
from scripts.message import process_table, sessions, roster as message_roster
from scripts.usage import agents_log
from scripts.accounts import accounts, moves, refusals, role_map
from scripts.astra import astra_work, design_roles
from scripts.whoami import account, agent_accounts


REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


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
    for name in ("HOME", "CODEX_HOME", "AGENT_NOTES_DIR"):
        value = os.environ.get(name)
        if not value:
            raise AssertionError(f"{name} is unset; codex_mesh fixture requires setUpModule")
        refuse_real_home(Path(value), name)


TEST_HANG_GUARD_SECS = 300  # load 136 on natedev, 2026-10-29


LegacyWorkerMoveRequest = TypedDict("LegacyWorkerMoveRequest", {
    "kind": Literal["requested"], "at": str, "tool": Literal["codex"],
    "subject": Literal["seats"], "move_id": str, "from": str, "to": str,
    "why": str, "window": Literal["unknown"],
})


class LegacyWorkerMoveConfirmation(TypedDict):
    kind: Literal["confirmed"]
    at: str
    tool: Literal["codex"]
    subject: Literal["seats"]
    move_id: str
    home: str
    seat: str
    run: str


@dataclasses.dataclass
class FixtureLoopBudget:
    """Fail a repeating fixture before fake time or retained calls exhaust memory."""

    name: str
    seconds: float = 120.0
    max_calls: int = 2_000
    started: float = dataclasses.field(default_factory=time.perf_counter)
    calls: int = 0

    def check(self) -> float:
        self.calls += 1
        if self.calls > self.max_calls:
            raise AssertionError(f"{self.name}: exceeded {self.max_calls} fixture calls")
        remaining = self.seconds - (time.perf_counter() - self.started)
        if remaining <= 0:
            raise AssertionError(f"{self.name}: exceeded {self.seconds}s real-time fixture deadline")
        return remaining


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
    # Registered first, checked last; unittest reports cleanup assertions as module errors.
    unittest.addModuleCleanup(assert_real_codex_unchanged)
    # No test sees a real sign-in; the synthetic selection predates every fake server.
    home = tempfile.TemporaryDirectory(dir="/tmp", prefix="codex-mesh-home-")
    usage = tempfile.TemporaryDirectory()
    restore_usage = set_environment("USAGE_STATE_DIR", usage.name)
    restore_home = set_environment("HOME", home.name)
    restore_notes = set_environment("AGENT_NOTES_DIR", home.name)
    # Tier tests never read the caller's registry, config, or pacer choice.
    tier_files = {
        "AGENTS_CONFIG_FILE": str(Path(home.name) / "agents.conf"),
        "CODEX_CONFIG_FILE": str(Path(home.name) / "codex.toml"),
        "CODEX_PACER_TIER_FILE": str(Path(home.name) / "pacer-tier"),
    }
    for path in tier_files.values():
        _ = Path(path).write_text("", encoding="utf-8")
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
    retries = patch.object(codex_mesh, "REAP_POLL_SECS", 0.001)
    _ = retries.start()
    unittest.addModuleCleanup(retries.stop)
    grace = patch.object(codex_mesh, "REAP_WAIT_SECS", 0.05)
    _ = grace.start()
    unittest.addModuleCleanup(grace.stop)
    # Holderless legacy fixtures have no synthetic ancestry; never inspect this test runner's parents.
    def fixture_processes() -> dict[int, process_table.Process]:
        return {}

    processes = patch.object(process_table, "snapshot", new=fixture_processes)
    _ = processes.start()
    unittest.addModuleCleanup(processes.stop)
    # Legacy server fixtures must never inspect native daemon processes.
    def fixture_daemons() -> list[codex_mesh.DaemonTarget]:
        return []

    daemons = patch.object(codex_mesh, "_running_daemons", new=fixture_daemons)
    _ = daemons.start()
    unittest.addModuleCleanup(daemons.stop)
    def fixture_executable(_pid: int) -> Path:
        return Path("/opt/bin/codex")

    executable = patch.object(codex_mesh, "_process_executable", new=fixture_executable)
    _ = executable.start()
    unittest.addModuleCleanup(executable.stop)

USAGE_LIMIT = "You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage"
CAPACITY = "Selected model is at capacity. Please try a different model."
STALE_SIGN_IN = ("Your access token could not be refreshed because you have since logged out or signed in to "
                 "another account. Please sign in again.")
THREAD_ID = "thread-capacity-test"
WRITE_POLICY: dict[str, object] = {"type": "dangerFullAccess"}
READ_ONLY_POLICY: dict[str, object] = {"type": "readOnly", "networkAccess": False}


def running_holder(session_id: str) -> sessions.SessionRecord:
    """A process-validated holder with no messaging socket is still running."""
    return sessions.SessionRecord(pid=os.getpid(), sessionId=session_id, name=session_id,
        messagingSocketPath="", updatedAt=1, tmux="", formerNames=[])


def _read_exact(sock: socket.socket, count: int) -> bytes:
    received = bytearray()
    budget = FixtureLoopBudget("stub socket read", seconds=120, max_calls=100_000)
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
        self.request_budget = FixtureLoopBudget("stub RPC requests", seconds=180)
        self.connection_budget = FixtureLoopBudget("stub connections", seconds=180, max_calls=128)
        self.errors: list[Exception] = []
        self.close_after_turn_start: bool = False
        self.hold_turn_completion: bool = False
        self.coalesce_response_frames: bool = False
        self.delta_batch_pause: float = 0.0
        self.held_turn_completes_successfully: bool = False
        self.hold_turn_timeout: float = 30.0
        self.answer_requests_during_held_turn: bool = False
        self.held_turn_ready: threading.Event = threading.Event()
        self.held_turn_interrupted: threading.Event = threading.Event()
        self.release_held_turn: threading.Event = threading.Event()
        self.block_compact_turn_completion: bool = False
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
        shutdown_deadline = time.perf_counter() + 5
        self.thread.join(timeout=5)
        self.listener.close()
        for connection in self.connections:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
        self.close_reader.close()
        self.close_writer.close()
        for conversation in self.conversations:
            conversation.join(timeout=max(0, shutdown_deadline - time.perf_counter()))
            if conversation.is_alive():
                raise AssertionError("stub conversation did not stop within 5s")
        if self.thread.is_alive():
            raise AssertionError("stub app-server did not stop")
        if self.errors:
            raise AssertionError(f"stub app-server failed: {self.errors}")

    def _serve(self) -> None:
        while not self.stopping.is_set():
            try:
                self.accept_waiting.set()
                remaining = self.connection_budget.check()
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
        handshake_budget = FixtureLoopBudget("stub handshake", seconds=5, max_calls=32)
        while b"\r\n\r\n" not in handshake:
            connection.settimeout(handshake_budget.check())
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
            connection.settimeout(self.request_budget.check())
            request = _read_request(connection)
            self.requests.append(request)
            frames = self.respond(request)
            if frames is None:
                return
            if len(frames) > 20_000:
                raise AssertionError("stub response exceeded 20,000 frames")
            if self.coalesce_response_frames:
                with send_lock:
                    connection.sendall(b"".join(_encoded_frame(frame) for frame in frames))
                continue
            delta_count = 0
            frame_budget = FixtureLoopBudget("stub response frames", seconds=30, max_calls=20_000)
            for frame in frames:
                _ = frame_budget.check()
                if frame.get("method") == "item/agentMessage/delta":
                    delta_count += 1
                    if self.delta_batch_pause and delta_count % 100 == 0:
                        _ = self.stopping.wait(timeout=self.delta_batch_pause)
                params = _as_test_dict(frame.get("params"))
                turn = _as_test_dict(params.get("turn"))
                if (
                    self.block_compact_turn_completion
                    and frame.get("method") == "turn/completed"
                    and turn.get("id") == "compact-turn"
                ):
                    self.compaction_event_sent.set()
                    _ = self.release_compact_turn_completion.wait(timeout=2)
                send_frame(frame)
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
        released = self.release_held_turn.wait(timeout=self.hold_turn_timeout)
        if self.stopping.is_set():
            return
        params = _as_test_dict(request.get("params"))
        result = _as_test_dict(reply.get("result"))
        turn_id = _as_test_dict(result.get("turn")).get("id")
        if released and self.held_turn_completes_successfully:
            turn: dict[str, object] = {"id": turn_id, "status": "completed"}
        elif self.held_turn_interrupted.is_set():
            turn = {"id": turn_id, "status": "interrupted"}
        else:
            turn = {"id": turn_id, "error": {"message": "held test turn was not interrupted" if released
                                           else "held test turn timed out"}}
        with contextlib.suppress(ConnectionError, OSError):
            send_frame(_notice("turn/completed", {"threadId": params["threadId"], "turn": turn}))


def _reply(request: dict[str, object], result: dict[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request["id"], "result": result}


def _notice(method: str, params: dict[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "method": method, "params": params}


def _as_test_dict(value: object) -> dict[str, object]:
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


class StubServerShutdownTests(unittest.TestCase):
    def test_close_wakes_a_blocking_listener_and_reaps_its_thread(self) -> None:
        server = StubAppServer(lambda _request: [])
        try:
            self.assertTrue(server.accept_waiting.wait(timeout=300))
            self.assertIsNone(server.listener.gettimeout())
        finally:
            server.close()
        self.assertFalse(server.thread.is_alive())
        self.assertEqual(server.listener.fileno(), -1)
        self.assertEqual(server.close_reader.fileno(), -1)
        self.assertEqual(server.close_writer.fileno(), -1)


def reported_stopped_context(report: MagicMock, index: int = -1) -> codex_mesh.RefusalCommandContext:
    return cast(codex_mesh.RefusalCommandContext, cast(object,
        report.call_args_list[index].kwargs.get("stopped", codex_mesh.REFUSAL_WITHOUT_COMMAND_EXIT)))


class MeshCommandFixture(unittest.TestCase):
    """Exercise the launcher and addressable verbs against a local app-server."""

    guard_frame_reads: bool = True

    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    home: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    session_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    server: StubAppServer  # pyright: ignore[reportUninitializedInstanceVariable]
    outcomes: list[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    turn_starts: int  # pyright: ignore[reportUninitializedInstanceVariable]
    turn_inputs: list[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    context_sizes: list[int]  # pyright: ignore[reportUninitializedInstanceVariable]
    compact_after_tokens: int  # pyright: ignore[reportUninitializedInstanceVariable]
    compact_uses_item: bool  # pyright: ignore[reportUninitializedInstanceVariable]
    compact_never_finishes: bool  # pyright: ignore[reportUninitializedInstanceVariable]
    live_turn: str  # pyright: ignore[reportUninitializedInstanceVariable]
    announce_live_completion: bool  # pyright: ignore[reportUninitializedInstanceVariable]
    queued: list[dict[str, object]]  # pyright: ignore[reportUninitializedInstanceVariable]
    read_failure: str  # pyright: ignore[reportUninitializedInstanceVariable]
    thread_statuses: dict[str, str]  # pyright: ignore[reportUninitializedInstanceVariable]
    error_threads: set[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    interrupt_error: str  # pyright: ignore[reportUninitializedInstanceVariable]
    interrupt_error_ends_turn: bool  # pyright: ignore[reportUninitializedInstanceVariable]
    hide_turn_id_from_steer: bool  # pyright: ignore[reportUninitializedInstanceVariable]
    hide_live_turn_id: bool  # pyright: ignore[reportUninitializedInstanceVariable]
    stale_read_turn: str  # pyright: ignore[reportUninitializedInstanceVariable]
    stale_reads: int  # pyright: ignore[reportUninitializedInstanceVariable]
    # Every turn this server started, with the status it ends in; a read that
    # asks for turns lists each one that is not live.
    started_turns: dict[str, str]  # pyright: ignore[reportUninitializedInstanceVariable]
    # More turn records such a read lists.
    silent_ended: list[dict[str, object]]  # pyright: ignore[reportUninitializedInstanceVariable]
    # A started turn the server reads idle but does not list yet.
    unlisted_turn: str  # pyright: ignore[reportUninitializedInstanceVariable]
    unlisted_reads: int  # pyright: ignore[reportUninitializedInstanceVariable]
    next_thread_id: str  # pyright: ignore[reportUninitializedInstanceVariable]
    fork_thread_id: str  # pyright: ignore[reportUninitializedInstanceVariable]
    history_turns: list[dict[str, object]]  # pyright: ignore[reportUninitializedInstanceVariable]
    failing_method: str  # pyright: ignore[reportUninitializedInstanceVariable]
    auto_turn_starts: int  # pyright: ignore[reportUninitializedInstanceVariable]
    waits: list[float]  # pyright: ignore[reportUninitializedInstanceVariable]
    elapsed: float  # pyright: ignore[reportUninitializedInstanceVariable]
    start_args_timeout: float  # pyright: ignore[reportUninitializedInstanceVariable]
    loop_budget: FixtureLoopBudget  # pyright: ignore[reportUninitializedInstanceVariable]
    patches: contextlib.ExitStack  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        _ = self.enterContext(patch.object(names, "load_names", return_value=fixture_names()))
        assert_fixture_environment()
        self.loop_budget = FixtureLoopBudget(self.id())
        self.temporary = tempfile.TemporaryDirectory(dir="/tmp", prefix="codex-mesh-test-")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve() / "home"
        self.home.mkdir()
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        _ = cast(object, self.patches.enter_context(patch.dict(os.environ, {
            "HOME": str(self.home), "CODEX_HOME": str(self.home / ".codex"),
            "AGENT_NOTES_DIR": str(self.home),
        })))
        # resolve(): macOS /tmp is a link to /private/tmp, and the mesh records resolved paths.
        self.session_dir = Path(self.temporary.name).resolve() / "run"
        self.session_dir.mkdir()
        _ = (self.session_dir / "prompt.txt").write_text("Original worker task", encoding="utf-8")
        self.outcomes = []
        self.turn_starts = 0
        self.turn_inputs = []
        self.context_sizes = []
        self.compact_after_tokens = 20000
        self.compact_uses_item = False
        self.compact_never_finishes = False
        self.live_turn = ""
        self.announce_live_completion = False
        self.queued = []
        self.read_failure = ""
        self.thread_statuses = {}
        self.error_threads = set()
        self.interrupt_error = ""
        self.interrupt_error_ends_turn = False
        self.hide_turn_id_from_steer = False
        self.hide_live_turn_id = False
        self.stale_read_turn = ""
        self.stale_reads = 0
        self.started_turns = {}
        self.silent_ended = []
        self.unlisted_turn = ""
        self.unlisted_reads = 0
        self.next_thread_id = THREAD_ID
        self.fork_thread_id = "thread-review-fork"
        self.history_turns = []
        self.failing_method = ""
        self.auto_turn_starts = 0
        self.waits = []
        self.elapsed = 0.0
        self.start_args_timeout = 300.0
        select_fixture_workers(self.home, notes=self.home)
        self.server = StubAppServer(self.respond)
        _ = (self.session_dir / codex_mesh.SERVER_FILE).write_text(
            json.dumps({"port": self.server.port, "pid": os.getpid(), "codex_home": str(agent_accounts.workers_home()), "auto_compact_tokens": codex_mesh.AUTO_COMPACT_TOKENS}),
            encoding="utf-8",
        )
        read_frame = codex_mesh.Client.next_frame

        def bounded_read(client: codex_mesh.Client, deadline: float) -> codex_mesh.RpcMessage | None:
            remaining = self.loop_budget.check()
            return read_frame(client, min(deadline, codex_mesh.time.time() + remaining))  # pyright: ignore[reportPrivateLocalImportUsage]

        if self.guard_frame_reads:
            _ = self.patches.enter_context(patch.object(codex_mesh.Client, "next_frame", bounded_read))
        _ = cast(object, self.patches.enter_context(patch.dict(os.environ, {
            "DESIGN_ROLES_STATE": str(self.session_dir / "design-roles"),
            "DESIGN_OWNERSHIP_STATE": str(self.session_dir / "design-ownership"),
            "UNIT_RUN_DIR": "", "PLAN_DELEGATE_RUN_DIR": "", "PLAN_DELEGATE_BOARD_DIR": "",
        })))
        _ = self.patches.callback(set_environment("USAGE_STATE_DIR", str(self.session_dir / "usage-state")))
        _ = self.patches.enter_context(patch.object(codex_mesh, "QUEUE_GRACE_SECS", 0.0))
        _ = self.patches.enter_context(patch.object(codex_mesh, "_capacity_clock", self.clock, create=True))
        _ = self.patches.enter_context(patch.object(codex_mesh, "_capacity_sleep", self.wait, create=True))

    @override
    def tearDown(self) -> None:
        self.patches.close()
        self.server.close()
        self.temporary.cleanup()

    def clock(self) -> float:
        _ = self.loop_budget.check()
        return self.elapsed

    def wait(self, seconds: float) -> None:
        _ = self.loop_budget.check()
        record = self.worker_record()
        self.assertEqual(record["thread_id"], THREAD_ID)
        self.assertEqual(record["status"], "waiting_capacity")
        self.waits.append(seconds)
        self.elapsed += seconds

    def respond(self, request: dict[str, object]) -> list[dict[str, object]] | None:
        method = request.get("method")
        params = cast("dict[str, object]", request.get("params", {}))
        if method == self.failing_method:
            return [{"id": request["id"], "error": {"message": "scripted request failure"}}]
        if method == "thread/start":
            return [_reply(request, {"thread": {"id": self.next_thread_id}})]
        if method == "thread/resume":
            return [_reply(request, {"thread": {"id": params["threadId"]}})]
        if method == "thread/fork":
            return [_reply(request, {"thread": {"id": self.fork_thread_id}})]
        if method == "thread/turns/list":
            turns = [*self.history_turns, *self.listed_turns()]
            if params.get("sortDirection") != "asc":
                turns.reverse()
            limit = params.get("limit")
            if isinstance(limit, int):
                turns = turns[:limit]
            return [_reply(request, {"data": turns, "nextCursor": None})]
        if method == "turn/start":
            self.error_threads.discard(cast("str", params.get("threadId", THREAD_ID)))
            self.turn_starts += 1
            inputs = cast("list[dict[str, object]]", params.get("input", []))
            submitted_text = cast("str", inputs[0].get("text", "")) if inputs else ""
            self.turn_inputs.append(submitted_text)
            self.queued.clear()
            turn_id = f"turn-{self.turn_starts}"
            turn_submission_frames = [
                _reply(request, {"turn": {"id": turn_id}}),
                _notice("item/completed", {
                    "threadId": THREAD_ID,
                    "turnId": turn_id,
                    "item": {
                        "id": f"user-message-{self.turn_starts}",
                        "type": "userMessage",
                        "content": [{"type": "inputText", "text": submitted_text}],
                    },
                }),
            ]
            outcome = self.outcomes.pop(0) if self.outcomes else "completed"
            self.started_turns[turn_id] = "failed" if outcome in {
                "capacity", "capacity_final_queued", "capacity_queued", "capacity_structured",
                "capacity_completed", "overflow", "progress_overflow", "failed",
                "failed_with_queued", "capacity_with_peer",
            } else "completed"
            if outcome in {
                "capacity", "capacity_final_queued", "capacity_queued",
                "capacity_structured", "capacity_completed", "failed",
                "completed_then_capacity",
            }:
                self.error_threads.add(THREAD_ID)
            if outcome == "disconnect":
                return turn_submission_frames
            if outcome == "earlier_completed":
                return [
                    *turn_submission_frames,
                    _notice("turn/completed", {
                        "threadId": THREAD_ID, "turn": {"id": "earlier-turn"}
                    }),
                    _notice("turn/completed", {
                        "threadId": THREAD_ID, "turn": {"id": turn_id}
                    }),
                ]
            if outcome == "earlier_only":
                return [
                    *turn_submission_frames,
                    _notice("turn/completed", {
                        "threadId": THREAD_ID, "turn": {"id": "earlier-turn"}
                    }),
                ]
            if outcome == "completed_without_id":
                return [
                    *turn_submission_frames,
                    _notice("turn/completed", {"threadId": THREAD_ID, "turn": {}}),
                ]
            if outcome == "completed_then_instant_peer":
                # A peer turn opens the instant the followed one ends. While it runs,
                # a thread read still lists the followed turn as the one in progress.
                self.live_turn = "peer-turn"
                self.stale_read_turn = turn_id
                return [
                    *turn_submission_frames,
                    _notice("turn/completed", {
                        "threadId": THREAD_ID, "turn": {"id": turn_id}
                    }),
                    _notice("turn/started", {
                        "threadId": THREAD_ID, "turn": {"id": "peer-turn"}
                    }),
                ]
            if outcome == "answered_without_completion":
                return [
                    *turn_submission_frames,
                    _notice("item/completed", {
                        "threadId": THREAD_ID, "item": {"type": "agentMessage", "text": "late answer"}
                    }),
                ]
            if outcome == "idle_before_listed":
                self.unlisted_turn = turn_id
                return turn_submission_frames
            if outcome == "listed_before_notices":
                # The server reads the thread idle and lists the new turn as
                # ended before it sends any notice for it.
                return turn_submission_frames[:1]
            if outcome == "completed_then_peer":
                self.live_turn = "peer-turn"
                self.announce_live_completion = True
                return [
                    *turn_submission_frames,
                    _notice("turn/completed", {
                        "threadId": THREAD_ID, "turn": {"id": turn_id}
                    }),
                    _notice("turn/started", {
                        "threadId": THREAD_ID, "turn": {"id": "peer-turn"}
                    }),
                ]
            if outcome == "capacity":
                finished = _notice(
                    "turn/failed",
                    {"threadId": THREAD_ID, "error": {"message": CAPACITY}},
                )
            elif outcome == "capacity_final_queued":
                self.queued.append({"id": "final-peer", "input": [
                    {"type": "text", "text": "Queued before refusal"}
                ], "clientUserMessageId": "final-peer"})
                finished = _notice(
                    "turn/failed", {"threadId": THREAD_ID, "error": {"message": CAPACITY}}
                )
            elif outcome == "capacity_queued":
                self.queued.append({"id": "waiting-peer", "input": [
                    {"type": "text", "text": "Server queued message"}
                ], "clientUserMessageId": "waiting-peer"})
                finished = _notice(
                    "turn/failed", {"threadId": THREAD_ID, "error": {"message": CAPACITY}}
                )
            elif outcome == "capacity_structured":
                finished = _notice(
                    "turn/failed",
                    {"threadId": THREAD_ID, "error": {
                        "message": "provider unavailable", "codexErrorInfo": "serverOverloaded"
                    }},
                )
            elif outcome == "capacity_completed":
                finished = _notice(
                    "turn/completed",
                    {"threadId": THREAD_ID, "turn": {
                        "id": turn_id, "error": {"message": CAPACITY}
                    }},
                )
            elif outcome == "overflow":
                finished = _notice(
                    "turn/completed",
                    {"threadId": THREAD_ID, "turn": {
                        "id": turn_id,
                        "error": {
                            "message": "context window exceeded",
                            "codexErrorInfo": "contextWindowExceeded",
                        },
                    }},
                )
            elif outcome == "progress_overflow":
                frames = turn_submission_frames.copy()
                frames.append(_notice("item/completed", {
                    "threadId": THREAD_ID,
                    "turnId": turn_id,
                    "item": {"type": "commandExecution", "command": "true"},
                }))
                frames.append(_notice(
                    "turn/completed",
                    {"threadId": THREAD_ID, "turn": {
                        "id": turn_id,
                        "error": {
                            "message": "context window exceeded",
                            "codexErrorInfo": "contextWindowExceeded",
                        },
                    }},
                ))
                return frames
            elif outcome == "failed":
                finished = _notice(
                    "turn/failed",
                    {"threadId": THREAD_ID, "error": {"message": "ordinary failure"}},
                )
            elif outcome == "failed_with_queued":
                self.live_turn = "peer-turn"
                self.announce_live_completion = True
                return [
                    *turn_submission_frames,
                    _notice("turn/failed", {
                        "threadId": THREAD_ID, "error": {"message": "ordinary failure"}
                    }),
                    _notice("turn/started", {
                        "threadId": THREAD_ID, "turn": {"id": "peer-turn"}
                    }),
                ]
            elif outcome == "capacity_with_peer":
                self.live_turn = "peer-turn"
                self.announce_live_completion = True
                return [
                    *turn_submission_frames,
                    _notice("turn/failed", {
                        "threadId": THREAD_ID, "error": {"message": CAPACITY}
                    }),
                    _notice("turn/started", {
                        "threadId": THREAD_ID, "turn": {"id": "peer-turn"}
                    }),
                ]
            elif outcome == "completed_then_capacity":
                return [
                    *turn_submission_frames,
                    _notice("turn/completed", {
                        "threadId": THREAD_ID, "turn": {"id": turn_id}
                    }),
                    _notice("turn/started", {
                        "threadId": THREAD_ID, "turn": {"id": "next-turn"}
                    }),
                    _notice("turn/failed", {
                        "threadId": THREAD_ID, "error": {"message": CAPACITY}
                    }),
                ]
            else:
                finished = _notice(
                    "turn/completed", {"threadId": THREAD_ID, "turn": {"id": turn_id}}
                )
            frames = turn_submission_frames.copy()
            if self.context_sizes:
                frames.append(_notice("thread/tokenUsage/updated", {
                    "threadId": THREAD_ID,
                    "turnId": turn_id,
                    "tokenUsage": {
                        "last": {"inputTokens": self.context_sizes.pop(0)},
                        "total": {"inputTokens": 0},
                    },
                }))
            frames.append(finished)
            return frames
        if method == "thread/compact/start":
            compact_turn = "compact-turn"
            if self.compact_never_finishes:
                self.live_turn = compact_turn
                return [
                    _reply(request, {}),
                    _notice("turn/started", {"threadId": params["threadId"], "turn": {"id": compact_turn}}),
                ]
            completion = _notice(
                "item/completed",
                {
                    "threadId": params["threadId"],
                    "turnId": compact_turn,
                    "item": {"id": "compact-item", "type": "contextCompaction"},
                },
            ) if self.compact_uses_item else _notice(
                "thread/compacted",
                {"threadId": params["threadId"], "turnId": compact_turn},
            )
            return [
                _reply(request, {}),
                _notice("thread/tokenUsage/updated", {
                    "threadId": params["threadId"],
                    "turnId": compact_turn,
                    "tokenUsage": {
                        "last": {"inputTokens": self.compact_after_tokens},
                        "total": {"inputTokens": 0},
                    },
                }),
                completion,
                _notice("turn/completed", {
                    "threadId": params["threadId"],
                    "turn": {"id": compact_turn},
                }),
            ]
        if method == "thread/queue/list":
            return [_reply(request, {"data": self.queued.copy(), "nextCursor": None})]
        if method == "thread/loaded/list":
            return [_reply(request, {"data": [], "nextCursor": None})]
        if method == "thread/unsubscribe":
            return [_reply(request, {}), _notice("thread/closed", {"threadId": params["threadId"]})]
        if method == "thread/queue/add":
            item = {
                "id": f"queued-{len(self.queued) + 1}", "input": params["input"],
                "clientUserMessageId": params["clientUserMessageId"],
            }
            if self.live_turn:
                self.queued.append(item)
            else:
                self.error_threads.discard(THREAD_ID)
                self.auto_turn_starts += 1
                self.live_turn = "queued-turn"
                self.announce_live_completion = True
                return [
                    _reply(request, {"queuedSubmission": item}),
                    _notice("turn/started", {
                        "threadId": THREAD_ID, "turn": {"id": self.live_turn}
                    }),
                ]
            return [_reply(request, {"queuedSubmission": item})]
        if method == "thread/queue/delete":
            queued_id = params.get("queuedSubmissionId")
            prior = len(self.queued)
            self.queued = [item for item in self.queued if item["id"] != queued_id]
            return [_reply(request, {"deleted": len(self.queued) != prior})]
        if method == "thread/read":
            if self.read_failure == "error":
                return [{"id": request["id"], "error": {"message": "read refused"}}]
            if self.read_failure == "timeout":
                return []
            thread_id = cast("str", params.get("threadId", THREAD_ID))
            if self.stale_read_turn:
                self.stale_reads += 1
                frames = [_reply(request, {"thread": {
                    "id": thread_id, "status": {"type": "active"},
                    "turns": [{"id": self.stale_read_turn, "status": "inProgress"}],
                }})]
                # The peer turn ends after the second read; no frame arrives unasked here.
                if self.stale_reads == 2:
                    frames.append(_notice("turn/completed", {
                        "threadId": THREAD_ID, "turn": {"id": self.live_turn}
                    }))
                    self.stale_read_turn = self.live_turn = ""
                return frames
            turns = self.listed_turns() if params.get("includeTurns") else []
            unlisted_ends: list[dict[str, object]] = []
            if self.unlisted_turn:
                self.unlisted_reads += 1
                # The turn completes after the second read, with its notice.
                if self.unlisted_reads == 2:
                    unlisted_ends = [
                        _notice("item/completed", {
                            "threadId": THREAD_ID, "item": {"type": "agentMessage", "text": "listed late"}
                        }),
                        _notice("turn/completed", {"threadId": THREAD_ID, "turn": {"id": self.unlisted_turn}}),
                    ]
                    self.unlisted_turn = ""
            # Codex 0.163 queues this notice ahead of every full-history read.
            frames = [_notice("deprecationNotice", {
                "summary": "Full-history hydration is deprecated"})] if params.get("includeTurns") else []
            frames += [_reply(request, {"thread": {
                "id": thread_id,
                "status": {"type": self.thread_statuses.get(thread_id) or (
                    "active" if self.live_turn else
                    "systemError" if thread_id in self.error_threads else "idle"
                )},
                "turns": turns,
            }}), *unlisted_ends]
            if self.live_turn and self.announce_live_completion:
                frames.append(_notice("turn/completed", {
                    "threadId": THREAD_ID, "turn": {"id": self.live_turn}
                }))
                self.live_turn = ""
            return frames
        if method == "turn/steer":
            if self.live_turn:
                if self.hide_turn_id_from_steer:
                    return [{"id": request["id"], "error": {"message": "turn id unavailable"}}]
                frames: list[dict[str, object]] = [
                    {"id": request["id"], "error": {"message": f"expected turn, but found `{self.live_turn}`"}}
                ]
                if self.announce_live_completion:
                    frames.append(_notice("turn/completed", {
                        "threadId": THREAD_ID, "turn": {"id": self.live_turn}
                    }))
                    self.live_turn = ""
                return frames
            return [{"id": request["id"], "error": {"message": "no live turn"}}]
        if method == "turn/interrupt":
            if self.interrupt_error:
                if self.interrupt_error_ends_turn:
                    self.live_turn = ""
                return [{"id": request["id"], "error": {"message": self.interrupt_error}}]
            if params.get("turnId") == self.live_turn:
                self.live_turn = ""
                return [_reply(request, {})]
            return [{"id": request["id"], "error": {"message": "wrong turn id"}}]
        return [_reply(request, {})]

    def start_args(self) -> argparse.Namespace:
        return argparse.Namespace(
            session_dir=str(self.session_dir), name="worker", cwd=str(self.session_dir),
            prompt_file=str(self.session_dir / "prompt.txt"),
            summary_file=str(self.session_dir / "summary.txt"),
            reply_file="", log_file=str(self.session_dir / "worker.log"),
            model="gpt-test", effort="high", service_tier="", sandbox="danger-full-access",
            timeout=self.start_args_timeout, resident=False, role="impl", lens=None,
        )

    def roster(self) -> dict[str, object]:
        with (self.session_dir / codex_mesh.ROSTER_FILE).open(encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_SH)
            return cast("dict[str, object]", json.load(handle))

    def worker_record(self) -> dict[str, object]:
        return cast("dict[str, object]", self.roster()["worker"])

    def usage_path(self) -> Path:
        return self.session_dir / "usage-state/agents.jsonl"

    def usage_records(self) -> list[dict[str, object]]:
        if not self.usage_path().exists():
            return []
        return [cast("dict[str, object]", json.loads(line))
                for line in self.usage_path().read_text(encoding="utf-8").splitlines()]

    def listed_turns(self) -> list[dict[str, object]]:
        """Turns the server lists, oldest first: ended ones, then any in progress."""
        turns: list[dict[str, object]] = [
            *({"id": turn, "status": status} for turn, status in self.started_turns.items()
              if turn not in (self.live_turn, self.unlisted_turn, self.stale_read_turn)),
            *self.silent_ended,
        ]
        if self.stale_read_turn:
            turns.append({"id": self.stale_read_turn, "status": "inProgress"})
        elif self.live_turn and not self.hide_live_turn_id:
            turns.append({"id": self.live_turn, "status": "inProgress"})
        return turns

    def methods(self, method: str) -> list[dict[str, object]]:
        return [request for request in self.server.requests if request.get("method") == method]

    def run_start(self) -> tuple[int, str]:
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            result = codex_mesh.command_start(self.start_args())
        return result, errors.getvalue()

    def run_follow(
        self, message: str = "Repair the open finding", role: str = "impl", lens: str = ""
    ) -> tuple[int, str]:
        message_file = self.session_dir / "follow-up.txt"
        _ = message_file.write_text(message, encoding="utf-8")
        args = self.start_args()
        args.to = "worker"
        args.role = role
        args.lens = lens or None
        args.message_file = str(message_file)
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            result = codex_mesh.command_follow(args)
        return result, errors.getvalue()

    def run_mesh(self, arguments: list[str]) -> tuple[int, str, str]:
        with (
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            code = codex_mesh.main([
                arguments[0], "--session-dir", str(self.session_dir), *arguments[1:]
            ])
        return code, output.getvalue(), errors.getvalue()

    def write_pool_roster(self, entries: dict[str, object]) -> None:
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(
            json.dumps(entries), encoding="utf-8"
        )

    def prime_cli(self, *options: str) -> list[str]:
        instructions_file = self.session_dir / "review-instructions.txt"
        _ = instructions_file.write_text("Review only.\nKeep café text intact.\n", encoding="utf-8")
        return [
            "prime", "--name", "template", "--instructions-file", str(instructions_file),
            "--cwd", str(self.session_dir), *options,
        ]

    def follow_cli(self, *options: str) -> list[str]:
        message_file = self.session_dir / "review-request.txt"
        _ = message_file.write_text("Review the second worktree", encoding="utf-8")
        return [
            "follow", "--to", "worker", "--role", "review", "--message-file", str(message_file),
            "--summary-file", str(self.session_dir / "summary.txt"),
            "--log-file", str(self.session_dir / "worker.log"), "--timeout", "5", *options,
        ]

    def assert_review_record(self, name: str, thread_id: str) -> None:
        record = _as_test_dict(self.roster().get(name))
        self.assertEqual(
            {key: record.get(key) for key in ("thread_id", "status", "role")},
            {"thread_id": thread_id, "status": "done", "role": "review"},
        )

    def assert_request_failure_closes_clients(self, arguments: list[str], name: str) -> None:
        client_type = codex_mesh.Client
        original_close = client_type.close
        opened: list[codex_mesh.Client] = []
        closed: list[codex_mesh.Client] = []

        def open_client(port: int, client_name: str) -> codex_mesh.Client:
            client = client_type(port, client_name)
            opened.append(client)
            return client

        def close_client(client: codex_mesh.Client) -> None:
            closed.append(client)
            original_close(client)

        with (
            patch.object(codex_mesh, "Client", open_client),
            patch.object(client_type, "close", close_client),
        ):
            code, output, errors = self.run_mesh(arguments)

        self.assertEqual(code, 1, errors)
        self.assertEqual(output, "")
        self.assertEqual(len(errors.splitlines()), 1, errors)
        self.assertTrue(errors.startswith(f"codex_mesh: {name}: "), errors)
        self.assertIn("scripted request failure", errors)
        self.assertTrue(opened, "the failed request must use a client")
        self.assertCountEqual(opened, closed)

    def replacement_record(self) -> dict[str, object]:
        return {"thread_id": "replacement-thread", "status": "running", "turn_id": "replacement-turn",
                "context_tokens": 777, "role": "test", "launcher_pid": os.getpid(),
                "port": self.server.port, "instructions_file": "replacement-instructions.txt"}

    def replace_worker(self, name: str = "worker") -> dict[str, object]:
        entries = self.roster()
        previous = _as_test_dict(entries.get(name))
        entries[name] = {**previous, "status": "ended"}
        self.write_pool_roster(entries)
        replacement = self.replacement_record()
        entries[name] = replacement
        self.write_pool_roster(entries)
        return replacement

    def held_reviewer_record(self, lifecycle: design_roles.Lifecycle) -> design_roles.HeldDesign:
        prompt = self.session_dir / "authorized.md"
        design = design_roles.HeldDesign(design_roles.DesignPlacement("00000000-0000-4000-8000-000000000026", "fixture-host", 3),
            "fixture-repo", str(self.session_dir), str(self.session_dir / "design.md"), "design",
            "2026-10-21T12:00:00+00:00", design_roles.UnitHolder("demo", "design-unit"), design_roles.DesignUnbound(),
            (design_roles.RoleInstance("designer", "unused-designer", design_roles.RoleNotOpened()),
             design_roles.RoleInstance("reviewer", "worker", lifecycle, str(prompt))))
        design_roles._save(design)  # pyright: ignore[reportPrivateUsage]
        _ = prompt.write_text("Current authorized reviewer instructions.")
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "done", "role": "review",
            "held_design_id": design.placement.design_id, "held_role": "reviewer", "port": self.server.port,
            "authorized_generation": 3, "authorized_holder": "fixture-holder", "authorized_prompt": str(prompt),
            "access_policy": {"kind": "ReadOnlyReviewer"}}})
        _ = self.patches.enter_context(patch.object(design_roles, "resolve_holder", return_value=design_roles.HolderReach("fixture-holder")))
        _ = cast(object, self.patches.enter_context(patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "fixture-holder",
                                                               "DESIGN_ROLES_HOST": "fixture-host"})))
        return design

    def prepare_refusing_account(self, *, stub_ensure: bool = True) -> agent_accounts.KnownCodexAccount:
        home = self.session_dir / "account-home"
        home.mkdir()
        notes = home / "notes"
        notes.mkdir()
        _ = cast(object, self.patches.enter_context(patch.dict(os.environ, {"HOME": str(home), "AGENT_NOTES_DIR": str(notes)})))
        selected = home / ".codex"
        selected.mkdir()
        login = "serving@example.invalid"
        claims = base64.urlsafe_b64encode(json.dumps({"email": login}).encode()).decode().rstrip("=")
        _ = (selected / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"header.{claims}.signature"}}))
        _ = (notes / "codex2.md").write_text(f"---\nlogin: {login}\n---\n")
        self.assertEqual(accounts.main(["set-role", "workers", "codex2"]), 0)
        _ = (self.session_dir / codex_mesh.SERVER_FILE).write_text(json.dumps({
            "port": self.server.port, "pid": os.getpid(), "codex_home": str(selected), "auto_compact_tokens": codex_mesh.AUTO_COMPACT_TOKENS,
        }))
        if stub_ensure:
            _ = self.patches.enter_context(patch.object(codex_mesh, "ensure_server", return_value=(self.server.port, True)))
        return agent_accounts.KnownCodexAccount(selected, login, "codex2")


    def configure_context(self) -> None:
        self.patches.enter_context(patch.dict(os.environ, {
            "UNIT_WORKERS_STATE": str(self.session_dir / "unit-workers"),
        }))
        _ = (self.session_dir / "progress_history_state.json").write_text(json.dumps({
            "production": "natedev", "unit": "models-unit", "working_dir": str(self.session_dir),
        }))


@final
class WorkerShownNameTests(MeshCommandFixture):
    def test_replacement_thread_and_its_followup_notice_use_the_current_unit_context(self) -> None:
        self.configure_context()
        self.write_pool_roster({"worker": {"thread_id": "old-follow-thread", "status": "done",
                                          "role": "review", "port": self.server.port}})
        self.failing_method = "thread/resume"

        code, errors = self.run_follow(role="review")

        self.assertEqual(code, 0, errors)
        names = [_as_test_dict(request["params"])["name"] for request in self.methods("thread/name/set")]
        self.assertTrue(names)
        for name in names:
            self.assertTrue(str(name).startswith("natedev · models · "), name)
            self.assertNotIn("(worker)", str(name))
        notice = (self.session_dir / "board.log").read_text()
        self.assertIn(str(names[-1]), notice)
        self.assertIn(f"continues as new thread {THREAD_ID}", notice)
        self.assertEqual(len(self.methods("turn/start")), 1)

    def test_a_refused_unit_reviewer_follow_keeps_the_existing_thread_title(self) -> None:
        self.configure_context()
        self.start_args_timeout = 0.1
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "done",
                                          "role": "review", "port": self.server.port,
                                          "unit_ledger": str(unit_workers.ledger_for_run(self.session_dir).path)}})
        old_title = "A title kept from the last accepted review"
        title = old_title
        respond = self.server.respond

        @bounded_fixture
        def remember_title(request: dict[str, object]) -> list[dict[str, object]] | None:
            nonlocal title
            if request.get("method") == "thread/resume":
                self.live_turn = "another-review-turn"
            if request.get("method") == "thread/name/set":
                title = str(_as_test_dict(request["params"])["name"])
            return respond(request)

        self.server.respond = remember_title
        code, errors = self.run_follow(role="review")
        self.assertNotEqual(code, 0, errors)
        self.assertIn("live turn", errors)
        self.assertTrue(self.methods("thread/resume"), "follow must reach the later idle check")
        self.assertTrue(self.methods("thread/read"))
        self.assertEqual(self.methods("thread/name/set"), [])
        self.assertEqual(self.methods("turn/start"), [])
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)
        self.assertEqual(title, old_title)

    def test_an_accepted_unit_reviewer_follow_uses_the_configured_shown_name(self) -> None:
        self.configure_context()
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "done", "role": "review",
                                          "port": self.server.port,
                                          "unit_ledger": str(unit_workers.ledger_for_run(self.session_dir).path)}})
        configured = fixture_names()
        jobs = dict(configured.jobs)
        jobs["code_reviewer"] = ("code checker",)
        with patch.object(names, "runtime_names", return_value=names.Names(jobs, configured.audiences)):
            code, errors = self.run_follow(role="review")
        self.assertEqual(code, 0, errors)
        titles = self.methods("thread/name/set")
        self.assertTrue(titles)
        for request in titles:
            self.assertEqual(_as_test_dict(request["params"]), {
                "threadId": THREAD_ID, "name": "natedev · models · code checker"})
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)
        self.assertEqual(self.worker_record()["role"], "review")
        self.assertEqual(self.methods("thread/start"), [])

    def test_help_for_addressable_commands_uses_the_worker_vocabulary(self) -> None:
        for command in ([], ["start"], ["follow"], ["prime"], ["fork"], ["reopen"], ["adopt"]):
            with self.subTest(command=command), contextlib.redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as ended:
                    _ = codex_mesh.main([*command, "--help"])
                self.assertEqual(ended.exception.code, 0)
                self.assertNotRegex(output.getvalue().lower(), r"\bseats?\b")


class FixtureHomeSafetyTests(unittest.TestCase):
    def test_worker_selection_refuses_real_home_descendants_and_symlinks(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp")))
        real = root / "real"
        real.mkdir()
        alias = root / "alias"
        alias.symlink_to(real, target_is_directory=True)
        with patch(f"{__name__}.REAL_HOME", real):
            for home in (real, real / "nested", alias):
                with self.subTest(home=home), self.assertRaisesRegex(AssertionError, "real home"):
                    select_fixture_workers(home, notes=root / "notes")
        self.assertEqual(list(real.iterdir()), [])
        self.assertFalse((root / "notes").exists())

    def test_base_setup_rejects_unsafe_environment_before_creating_files(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp")))
        real = root / "real"
        real.mkdir()
        for name in ("HOME", "CODEX_HOME", "AGENT_NOTES_DIR"):
            environment = {key: str(root / "safe") for key in ("HOME", "CODEX_HOME", "AGENT_NOTES_DIR")}
            environment[name] = str(real / "nested")
            with (self.subTest(variable=name), patch(f"{__name__}.REAL_HOME", real),
                  patch.dict(os.environ, environment),
                  patch.object(tempfile, "TemporaryDirectory", wraps=tempfile.TemporaryDirectory) as temporary):
                with self.assertRaisesRegex(AssertionError, name + " points into the real home"):
                    MeshCommandFixture().setUp()
                temporary.assert_not_called()

    def test_metadata_guard_catches_a_bypassed_refusal_in_an_absent_or_existing_tree(self) -> None:
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory(dir="/tmp") as directory:
                real = Path(directory)
                auth = real / ".codex/auth.json"
                if existing:
                    auth.parent.mkdir()
                    _ = auth.write_text("temporary account placeholder")
                before = fixture_write_metadata(real)
                with (patch(f"{__name__}.REAL_HOME", real),
                      patch(f"{__name__}.REAL_FIXTURE_METADATA", before)):
                    with self.assertRaisesRegex(AssertionError, "real home"):
                        select_fixture_workers(real, notes=real / "notes")
                    assert_real_codex_unchanged()
                    # Live sessions may create and change unrelated files while tests run.
                    session = real / ".codex/sessions/x.jsonl"
                    session.parent.mkdir(parents=True, exist_ok=True)
                    notes = real / "rust/hanadocs/agents"
                    notes.mkdir(parents=True)
                    live = [real / name for name in (".codex/config.toml", ".codex/hooks.json",
                                                     ".claude/scripts/lib/py", ".local/state/accounts/moves.jsonl")]
                    live.extend(notes / note_name(label) for label in ("codex1", "codex2"))
                    for text in ("first event", "another event"):
                        _ = session.write_text(text)
                        for path in live:
                            path.parent.mkdir(parents=True, exist_ok=True)
                            _ = path.write_text(text)
                        assert_real_codex_unchanged()
                    # Fault injection only targets this disposable stand-in, never the login home.
                    with patch(f"{__name__}.refuse_real_home"):
                        select_fixture_workers(real, notes=real / "rust/hanadocs/agents")
                    with self.assertRaises(AssertionError) as changed:
                        assert_real_codex_unchanged()
                    for name in (".codex/auth.json", ".claude/config/accounts.json",
                                 "rust/hanadocs/agents/codex9.md"):
                        self.assertIn(str(real / name), str(changed.exception))
        # A sign-in in a Codex home that did not exist at import is caught too.
        for name in (".codex22/auth.json",):
            with self.subTest(path=name), tempfile.TemporaryDirectory(dir="/tmp") as directory:
                real = Path(directory)
                path = real / name
                before = fixture_write_metadata(real)
                with (patch(f"{__name__}.REAL_HOME", real),
                      patch(f"{__name__}.REAL_FIXTURE_METADATA", before)):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    _ = path.write_text("fixture content")
                    with self.assertRaisesRegex(AssertionError, "fixture targets"):
                        assert_real_codex_unchanged()
                    written = fixture_write_metadata(real)
                    with patch(f"{__name__}.REAL_FIXTURE_METADATA", written):
                        _ = path.write_text("changed fixture content")
                        with self.assertRaisesRegex(AssertionError, "fixture targets"):
                            assert_real_codex_unchanged()
                        path.unlink()
                        with self.assertRaisesRegex(AssertionError, "fixture targets"):
                            assert_real_codex_unchanged()
                    assert_real_codex_unchanged()


@final
class MeshCommandTests(MeshCommandFixture):
    """Launcher and recovery behavior against the shared bounded fake server."""

    def test_held_turn_with_current_binding_reuses_the_server_without_blocking(self) -> None:
        design = self.held_reviewer_record(design_roles.RoleOpen(THREAD_ID))
        binding = design_roles.DesignBoundToPhase(str(self.session_dir / "phase-run"), "1",
                                          str(self.session_dir), ("design.md",))
        design = dataclasses.replace(design, binding=binding)
        design_roles._save(design)  # pyright: ignore[reportPrivateUsage]
        context_path = self.session_dir / "design_context.json"
        context = {"design_id": design.placement.design_id, "generation": design.placement.generation,
                   "holder_session": "fixture-holder", "cwd": design.repository,
                   "binding": {"kind": "DesignBoundToPhase", **dataclasses.asdict(binding)}}
        _ = context_path.write_text(json.dumps(context))
        server_path = self.session_dir / codex_mesh.SERVER_FILE
        server_record = cast(dict[str, object], json.loads(server_path.read_text()))
        server_record["design_context"] = context
        _ = server_path.write_text(json.dumps(server_record))
        before = server_path.read_bytes()
        args = self.start_args()
        args.to, args.role = "worker", "review"
        args.message_file = str(self.session_dir / "authorized.md")
        results: list[int] = []
        errors: list[BaseException] = []

        def follow() -> None:
            try:
                results.append(codex_mesh.command_follow(args))
            except BaseException as error:
                errors.append(error)

        with patch.object(codex_mesh, "_holds_old_sign_in", return_value=False), \
                patch.object(codex_mesh, "_reap", return_value=False), \
                patch.object(codex_mesh, "_start_server", side_effect=AssertionError("the fixture server must be reused")):
            launcher = threading.Thread(target=follow, daemon=True)
            launcher.start()
            launcher.join(timeout=5)
        self.assertFalse(launcher.is_alive(), "unchanged binding blocked the turn on its server lock")
        if errors:
            raise errors[0]
        self.assertEqual(results, [0])
        self.assertEqual(len(self.methods("turn/start")), 1)
        self.assertEqual(server_path.read_bytes(), before)
        self.assertEqual(self.methods("thread/unsubscribe"), [])
        self.assertFalse((self.session_dir / "serving_home.json").exists())
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)

    def test_sweep_or_idle_stop_of_a_held_design_server_resumes_same_thread_with_pending_text(self) -> None:
        fixture_root = self.session_dir
        for stop_route in ("idle-stop", "sweep"):
            with self.subTest(stop_route=stop_route):
                design_id = "00000000-0000-4000-8000-000000000027"
                self.session_dir = design_roles.mesh_directory(design_id)
                self.session_dir.mkdir(parents=True, exist_ok=True)
                directory = design_roles.design_directory(design_id)
                prompt = directory / "prompts/assignment.md"
                reply = directory / "replies/assignment.md"
                prompt.parent.mkdir(exist_ok=True)
                reply.parent.mkdir(exist_ok=True)
                binding = design_roles.DesignBoundToPhase(str(fixture_root / "phase-run"), "1",
                                                  str(fixture_root), ("design.md",))
                design = design_roles.HeldDesign(design_roles.DesignPlacement(design_id, "fixture-host", 3),
                    "fixture-repo", str(fixture_root), str(fixture_root / "design.md"), "design",
                    "2026-10-21T12:00:00+00:00", design_roles.UnitHolder("demo", "design-unit"), binding,
                    (design_roles.RoleInstance("designer", "worker", design_roles.RoleOpen(THREAD_ID), str(prompt)),
                     design_roles.RoleInstance("reviewer", "unused-reviewer", design_roles.RoleNotOpened())))
                design_roles._save(design)  # pyright: ignore[reportPrivateUsage]
                header = astra_work.AstraWork("astra-designer", "worker", design.document,
                    design.created_at_utc, str(reply), astra_work.StandaloneWork())
                _ = prompt.write_text(astra_work.header_line(header) + "\nContinue the authorized design.\n")
                _ = reply.write_text("Previous saved reply.\n")
                policy = {"kind": "PhaseBoundDesigner", "binding_generation": 3,
                          "launch_sandbox": "danger-full-access"}
                self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "done", "role": "impl",
                    "held_design_id": design_id, "held_role": "designer", "access_policy": policy,
                    "authorized_generation": 3, "authorized_holder": "fixture-holder",
                    "authorized_prompt": str(prompt), "port": self.server.port}})
                context = {"design_id": design_id, "generation": 3, "holder_session": "fixture-holder",
                           "cwd": design.repository, "binding": {"kind": "DesignBoundToPhase",
                                                                   **dataclasses.asdict(binding)}}
                _ = (self.session_dir / "design_context.json").write_text(json.dumps(context))
                pending = self.session_dir / "worker.pending.json"
                _ = pending.write_text(json.dumps({"thread_id": THREAD_ID,
                    "messages": ["Keep this pending design correction."], "accepting_messages": True}))
                _ = design_roles.queue_message(design_id, "designer", "fixture-holder", "Keep this holder message.",
                                        message_id="00000000-0000-4000-8000-000000000028")
                process = subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()"],
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL)
                # Reap this child as soon as the real stop code signals it, so kill(0) sees it gone.
                reaper = threading.Thread(target=process.wait, daemon=True)
                reaper.start()
                server_path = self.session_dir / codex_mesh.SERVER_FILE
                _ = server_path.write_text(json.dumps({"pid": process.pid, "port": self.server.port,
                    "codex_home": str(agent_accounts.workers_home()),
                    "auto_compact_tokens": codex_mesh.AUTO_COMPACT_TOKENS, "design_context": context}))
                old = codex_mesh.SERVER_IDLE_SECS + 60
                for name in codex_mesh.SWEEP_ACTIVITY_FILES:
                    path = self.session_dir / name
                    if path.exists():
                        os.utime(path, (time.time() - old, time.time() - old))
                preserved = {path: path.read_bytes() for path in directory.rglob("*") if path.is_file()
                             and path != server_path and path.name != "lock"}
                outside_roots = fixture_root / "ordinary-runs"
                outside_roots.mkdir(exist_ok=True)
                try:
                    with (
                        patch.object(codex_mesh, "_running_servers", return_value=[(process.pid, self.server.port, old)]),
                        patch.object(codex_mesh, "_client_counts", return_value={}),
                        patch.object(codex_mesh, "_running_daemons", return_value=[]),
                        patch.object(codex_mesh, "_server_folder", return_value=codex_mesh.MeshServerLog(self.session_dir)),
                        contextlib.redirect_stdout(io.StringIO()),
                    ):
                        if stop_route == "idle-stop":
                            code = codex_mesh.command_idle_stop(argparse.Namespace(
                                session_dir=str(self.session_dir), pid=process.pid, port=self.server.port))
                        else:
                            code = codex_mesh.command_sweep(argparse.Namespace(root=str(outside_roots), stop=True))
                    self.assertEqual(code, 0)
                    reaper.join(timeout=5)
                    self.assertFalse(reaper.is_alive(), "the stopped child was not reaped")
                    self.assertIsNotNone(process.poll())
                    self.assertFalse(codex_mesh._pid_alive(process.pid))  # pyright: ignore[reportPrivateUsage]
                    self.assertFalse(server_path.exists())
                    for path, contents in preserved.items():
                        self.assertEqual(path.read_bytes(), contents, str(path))
                    self.assertFalse((directory / "transition.json").exists())
                    self.assertEqual(list(self.session_dir.glob("*.account-move.json")), [])

                    started: list[dict[str, str]] = []

                    def start_fixture_server(session_dir: str, home: Path) -> int:
                        self.assertFalse(server_path.exists())
                        self.server.close()
                        self.server = StubAppServer(self.respond)
                        started.append(codex_mesh._server_environment(session_dir, home))  # pyright: ignore[reportPrivateUsage]
                        _ = server_path.write_text(json.dumps({"pid": os.getpid(), "port": self.server.port,
                            "codex_home": str(home), "auto_compact_tokens": codex_mesh.AUTO_COMPACT_TOKENS,
                            "design_context": context}))
                        return self.server.port

                    args = self.start_args()
                    args.to = "worker"
                    args.message_file = str(prompt)
                    args.summary_file = str(reply)
                    with (
                        patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "fixture-holder", "DESIGN_ROLES_HOST": "fixture-host"}),
                        patch.object(design_roles, "resolve_holder", return_value=design_roles.HolderReach("fixture-holder")),
                        patch.object(codex_mesh, "_start_server", start_fixture_server),
                        contextlib.redirect_stderr(io.StringIO()) as errors,
                    ):
                        self.assertEqual(codex_mesh.command_follow(args), 0, errors.getvalue())
                    self.assertEqual(len(started), 1)
                    self.assertEqual(started[0]["HELD_DESIGN_ID"], design_id)
                    self.assertEqual(started[0]["UNIT_RUN_DIR"], binding.run_directory)
                    self.assertEqual([_as_test_dict(request["params"])["threadId"]
                                      for request in self.methods("thread/resume")], [THREAD_ID])
                    self.assertEqual(self.methods("thread/start"), [])
                    titles = self.methods("thread/name/set")
                    self.assertTrue(titles)
                    for request in titles:
                        self.assertEqual(request["params"], {
                            "threadId": THREAD_ID, "name": "demo · design · designer"})
                    turn = _as_test_dict(self.methods("turn/start")[0]["params"])
                    self.assertIn("Keep this pending design correction.", json.dumps(turn["input"]))
                    self.assertEqual(turn["sandboxPolicy"], {"type": "dangerFullAccess"})
                    entry = self.worker_record()
                    self.assertEqual((entry["thread_id"], entry["held_design_id"], entry["access_policy"]),
                                     (THREAD_ID, design_id, policy))
                    self.assertEqual([message.text for message in design_roles.pending_messages(design_id, "designer")],
                                     ["Keep this holder message."])
                finally:
                    if process.poll() is None:
                        process.kill()
                    reaper.join(timeout=5)
                self.session_dir = fixture_root

    def test_account_move_resume_refuses_a_fenced_source_before_touching_its_queue(self) -> None:
        _ = self.held_reviewer_record(design_roles.RoleOpen(THREAD_ID))
        record = self.worker_record()
        record["transferring_role"] = {"adoption_id": "transfer-one", "destination": "/fixture/destination"}
        self.write_pool_roster({"worker": record})
        before = (self.session_dir / codex_mesh.ROSTER_FILE).read_bytes()
        with self.assertRaisesRegex(SystemExit, "TransferringRole"):
            _ = codex_mesh.command_resume_account_move(argparse.Namespace(session_dir=str(self.session_dir), to="worker"))
        self.assertEqual((self.session_dir / codex_mesh.ROSTER_FILE).read_bytes(), before)
        self.assertFalse((self.session_dir / "worker.account-move.json").exists())
        self.assertEqual(self.methods("thread/queue/list"), [])
        self.assertEqual(self.methods("turn/interrupt"), [])
        self.assertEqual(self.methods("turn/start"), [])

    def test_account_move_recovery_saves_the_held_reply_at_its_assignment_path(self) -> None:
        _ = self.held_reviewer_record(design_roles.RoleOpen(THREAD_ID))
        reply = self.session_dir / "saved-replies/assignment.md"
        prompt = self.session_dir / "authorized.md"
        header = astra_work.AstraWork("astra-reviewer", "worker", str(self.session_dir / "design.md"),
            "2026-10-21T12:00:00+00:00", str(reply), astra_work.StandaloneWork())
        _ = prompt.write_text(astra_work.header_line(header) + "\nCurrent authorized reviewer instructions.\n")
        record = self.worker_record()
        record.update({"instructions_file": str(prompt), "status": "running", "turn_id": "interrupted-turn"})
        self.write_pool_roster({"worker": record})
        self.live_turn = "interrupted-turn"
        moved = codex_mesh._interrupt_worker_for_account_move(str(self.session_dir), "worker", record,  # pyright: ignore[reportPrivateUsage]
                                                             self.server.port, "codex1")
        self.assertIsInstance(moved, codex_mesh.InterruptedWorkerMove)
        original = self.respond

        def respond(request: dict[str, object]) -> list[dict[str, object]] | None:
            frames = original(request)
            if request.get("method") == "turn/start" and frames:
                frames.insert(-1, _notice("item/completed", {"threadId": THREAD_ID,
                    "item": {"type": "agentMessage", "text": "Recovered reviewer evidence."}}))
            return frames

        self.server.respond = respond
        code = codex_mesh.command_resume_account_move(argparse.Namespace(session_dir=str(self.session_dir), to="worker"))
        self.assertEqual(code, 0)
        self.assertTrue(reply.is_file())
        self.assertEqual(reply.read_text().strip(), "Recovered reviewer evidence.")
        self.assertFalse((self.session_dir / "worker.account-move-summary.txt").exists())
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)
        self.assertEqual(self.methods("thread/start"), [])

    def test_invalid_account_move_header_preserves_the_roster_for_a_later_follow(self) -> None:
        _ = self.held_reviewer_record(design_roles.RoleOpen(THREAD_ID))
        prompt = self.session_dir / "authorized.md"
        _ = prompt.write_text("<!-- astra_work {invalid json} -->\nReviewer instructions.\n")
        record = self.worker_record()
        record.update({"instructions_file": str(prompt), "status": "running", "turn_id": "interrupted-turn"})
        self.write_pool_roster({"worker": record})
        self.live_turn = "interrupted-turn"
        moved = codex_mesh._interrupt_worker_for_account_move(str(self.session_dir), "worker", record,  # pyright: ignore[reportPrivateUsage]
                                                             self.server.port, "codex1")
        self.assertIsInstance(moved, codex_mesh.InterruptedWorkerMove)
        record.update({"status": "failed", "launcher_pid": 0})
        self.write_pool_roster({"worker": record})
        roster_path = self.session_dir / codex_mesh.ROSTER_FILE
        before = roster_path.read_bytes()
        code = codex_mesh.command_resume_account_move(argparse.Namespace(session_dir=str(self.session_dir), to="worker"))
        self.assertEqual(code, 1)
        result = cast(dict[str, object], json.loads((self.session_dir / "worker.account-move-result.json").read_text()))
        self.assertIn("header is invalid", str(result["error"]))
        self.assertEqual(roster_path.read_bytes(), before)
        self.assertEqual(self.methods("turn/start"), [])
        reply = self.session_dir / "reply.md"
        header = astra_work.AstraWork("astra-reviewer", "worker", str(self.session_dir / "design.md"),
            "2026-10-21T12:00:00+00:00", str(reply), astra_work.StandaloneWork())
        _ = prompt.write_text(astra_work.header_line(header) + "\nReviewer instructions.\n")
        self.assertTrue(codex_mesh._claim_follow(str(self.session_dir), "worker", THREAD_ID,  # pyright: ignore[reportPrivateUsage]
                                                os.getpid(), "review"))
        self.assertEqual(self.worker_record()["status"], "starting")

    def test_held_instruction_and_turn_claim_has_one_winner_and_uses_current_prompt(self) -> None:
        _ = self.held_reviewer_record(design_roles.RoleOpen(THREAD_ID))
        def claim(pid: int) -> bool:
            return codex_mesh._claim_follow(str(self.session_dir), "worker", THREAD_ID, pid, "review")  # pyright: ignore[reportPrivateUsage]

        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(claim, (os.getpid(), os.getpid())))
        self.assertCountEqual(claims, [True, False])
        entry = self.worker_record()
        self.assertEqual(entry["instructions_file"], str(self.session_dir / "authorized.md"))
        self.assertEqual((entry["thread_id"], entry["status"]), (THREAD_ID, "starting"))
        self.assertNotIn("authorized_prompt", entry)
        self.assertEqual(self.methods("thread/start"), [])

    def test_stale_held_assignment_cannot_claim_a_turn_or_replace_instructions(self) -> None:
        _ = self.held_reviewer_record(design_roles.RoleOpen(THREAD_ID))
        record = self.worker_record()
        record.update({"authorized_generation": 2, "instructions_file": "previous.md"})
        self.write_pool_roster({"worker": record})
        self.assertFalse(codex_mesh._claim_follow(str(self.session_dir), "worker", THREAD_ID,  # pyright: ignore[reportPrivateUsage]
                                                 os.getpid(), "review"))
        self.assertEqual(self.worker_record(), record)
        self.assertEqual(self.methods("turn/start"), [])

    def test_held_thread_resume_failure_does_not_start_a_replacement(self) -> None:
        _ = self.held_reviewer_record(design_roles.RoleOpen(THREAD_ID))
        self.failing_method = "thread/resume"
        args = self.start_args()
        args.to = "worker"
        args.role = "review"
        args.message_file = str(self.session_dir / "authorized.md")
        with contextlib.redirect_stderr(io.StringIO()) as output:
            code = codex_mesh.command_follow(args)
        errors = output.getvalue()
        self.assertEqual(code, 1, errors)
        self.assertIn("RoleNeedsRecovery", errors)
        self.assertEqual(len(self.methods("thread/resume")), 1)
        self.assertEqual(self.methods("thread/start"), [])
        self.assertEqual(self.methods("turn/start"), [])
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)

    def test_held_roles_refuse_raw_pause_reopen_and_instruction_replacement(self) -> None:
        _ = self.held_reviewer_record(design_roles.RoleOpen(THREAD_ID))
        instructions = self.session_dir / "replacement.md"
        _ = instructions.write_text("This text must not replace the authorized assignment.")
        before = (self.session_dir / codex_mesh.ROSTER_FILE).read_bytes()
        for command in (["stop", "--pause"], ["reopen", "--to", "worker"],
                        ["set-instructions", "--to", "worker", "--instructions-file", str(instructions)]):
            with self.subTest(command=command), self.assertRaises(SystemExit):
                _ = self.run_mesh(command)
            self.assertEqual((self.session_dir / codex_mesh.ROSTER_FILE).read_bytes(), before)
        self.assertEqual(self.methods("turn/start"), [])
        self.assertEqual(self.methods("thread/resume"), [])

    def test_adoption_fence_blocks_old_follow_reopen_move_and_cleanup_routes(self) -> None:
        _ = self.held_reviewer_record(design_roles.RoleOpen(THREAD_ID))
        entry = self.worker_record()
        entry["transferring_role"] = {"adoption_id": "transfer-one", "destination": "/fixture/destination"}
        self.write_pool_roster({"worker": entry})
        before = (self.session_dir / codex_mesh.ROSTER_FILE).read_bytes()
        for command in (self.follow_cli(), ["reopen", "--to", "worker"], ["stop", "--pause"]):
            with self.subTest(command=command), self.assertRaisesRegex(SystemExit, "TransferringRole"):
                _ = self.run_mesh(command)
        with self.assertRaisesRegex(SystemExit, "TransferringRole"):
            _ = codex_mesh._switch_run_workers(self.session_dir, self.server.port, "codex1", dry_run=False)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual((self.session_dir / codex_mesh.ROSTER_FILE).read_bytes(), before)
        self.assertEqual(self.methods("thread/start"), [])
        self.assertEqual(self.methods("turn/start"), [])

    def test_explicit_designer_access_stays_writable_with_standing_instructions(self) -> None:
        instructions = self.session_dir / "designer.md"
        _ = instructions.write_text("Edit only the phase output paths by patch.")
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "done", "role": "impl",
            "instructions_file": str(instructions), "access_policy": {"kind": "PhaseBoundDesigner",
                "binding_generation": 1, "launch_sandbox": "danger-full-access"}}})
        code, errors = self.run_follow()
        self.assertEqual(code, 0, errors)
        self.assertEqual(_as_test_dict(self.methods("thread/resume")[0]["params"])["developerInstructions"], instructions.read_text())
        self.assertEqual(_as_test_dict(self.methods("turn/start")[0]["params"])["sandboxPolicy"], {"type": "dangerFullAccess"})

    def test_end_from_recovery_marks_lost_thread_ended_before_failed_server_cleanup(self) -> None:
        _ = self.held_reviewer_record(design_roles.RoleNeedsRecovery(THREAD_ID, "thread lost"))
        entry = self.worker_record()
        entry["status"] = "failed"
        self.write_pool_roster({"worker": entry})
        self.read_failure = "error"
        code, _output, errors = self.run_mesh(["end", "--to", "worker"])
        self.assertEqual(code, 1, errors)
        self.assertEqual(self.worker_record()["status"], "ended")
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)
        self.assertTrue((self.session_dir / "worker.end").exists())
        self.assertEqual(self.methods("thread/start"), [])

    def test_move_without_a_prior_marker_parks_queued_text_before_interrupting(self) -> None:
        _ = self.held_reviewer_record(design_roles.RoleOpen(THREAD_ID))
        entry = self.worker_record()
        entry.update({"status": "running", "turn_id": "live-one"})
        self.write_pool_roster({"worker": entry})
        self.live_turn = "live-one"
        self.queued = [{"id": "queued-one", "input": [{"type": "text", "text": "Keep this queued turn."}]}]
        original = self.respond

        def respond(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") in ("thread/queue/delete", "turn/interrupt"):
                self.assertIn("Keep this queued turn.", (self.session_dir / "worker.pending.json").read_text())
            return original(request)

        self.server.respond = respond
        moved = codex_mesh._interrupt_worker_for_account_move(str(self.session_dir), "worker", entry,  # pyright: ignore[reportPrivateUsage]
                                                             self.server.port, "codex1")
        self.assertIsInstance(moved, codex_mesh.InterruptedWorkerMove)
        self.assertTrue((self.session_dir / "worker.account-move.json").exists())
        self.assertIn("Keep this queued turn.", (self.session_dir / "worker.pending.json").read_text())
        self.assertEqual(len(self.methods("turn/interrupt")), 1)
        self.assertEqual(self.methods("thread/start"), [])

    def test_idle_server_release_parks_queue_before_unsubscribe_and_preserves_thread(self) -> None:
        _ = self.held_reviewer_record(design_roles.RoleOpen(THREAD_ID))
        self.queued = [{"id": "queued-one", "input": [{"type": "text", "text": "Idle queued evidence."}]}]
        original = self.respond

        def respond(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/unsubscribe":
                self.assertIn("Idle queued evidence.", (self.session_dir / "worker.pending.json").read_text())
            return original(request)

        self.server.respond = respond
        with patch.object(codex_mesh, "_reap", return_value=True):
            codex_mesh._release_held_server(str(self.session_dir))  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)
        self.assertEqual(self.worker_record()["status"], "done")
        self.assertFalse((self.session_dir / codex_mesh.SERVER_FILE).exists())
        self.assertIn("Idle queued evidence.", (self.session_dir / "worker.pending.json").read_text())

    def test_idle_durable_account_move_without_run_marker_parks_queue_before_server_stop(self) -> None:
        design = self.held_reviewer_record(design_roles.RoleOpen(THREAD_ID))
        mesh = design_roles.mesh_directory(design.placement.design_id)
        mesh.mkdir(parents=True)
        _ = (mesh / codex_mesh.ROSTER_FILE).write_text(json.dumps({"worker": self.worker_record()}))
        _ = (mesh / codex_mesh.SERVER_FILE).write_bytes((self.session_dir / codex_mesh.SERVER_FILE).read_bytes())
        design_roles._mesh_context(design, design_roles.HolderReach("fixture-holder"))  # pyright: ignore[reportPrivateUsage]
        self.queued = [{"id": "queued-one", "input": [{"type": "text", "text": "Keep idle account-move text."}]}]
        stopped: list[int] = []

        def stop(roots: object, pid: int, port: int, location: object = None) -> bool:
            del roots, port, location
            self.assertIn("Keep idle account-move text.", (mesh / "worker.pending.json").read_text())
            stopped.append(pid)
            return True

        with patch.object(codex_mesh, "SWEEP_ROOTS", (self.session_dir / "empty-runs",)), \
                patch.object(codex_mesh, "_stop_unused", side_effect=stop), \
                patch.object(codex_mesh, "_notify_holder"), \
                patch.object(codex_mesh, "_start_watcher", side_effect=AssertionError("idle server should stop")):
            switches = codex_mesh.retire_workers_servers(self.session_dir / "next-account", "codex1")
        self.assertEqual([switch.folder for switch in switches], [mesh])
        self.assertEqual(switches[0].holders, ("fixture-holder",))
        self.assertEqual(stopped, [os.getpid()])
        self.assertEqual(self.methods("turn/start"), [])
        self.assertEqual(self.methods("thread/start"), [])
        self.assertEqual(cast(dict[str, dict[str, object]], json.loads((mesh / codex_mesh.ROSTER_FILE).read_text()))["worker"]["thread_id"], THREAD_ID)

    def test_old_launcher_normal_end_preserves_the_replacement_roster(self) -> None:
        replacement = self.replacement_record()
        def replace_at_end(_port: int, _thread_id: str, _log_path: Path) -> codex_mesh.RelaunchAllowed:
            self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)
            self.assertEqual(self.replace_worker(), replacement)
            return codex_mesh.RelaunchAllowed()
        with patch.object(codex_mesh, "_end_unwatched_turn", side_effect=replace_at_end):
            code, errors = self.run_start()
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.worker_record(), replacement)

    def test_old_launcher_exception_preserves_the_replacement_roster(self) -> None:
        original = codex_mesh.Client.next_frame
        replacement = self.replacement_record()
        failed = [False]
        @bounded_fixture
        def fail_after_replacement(client: codex_mesh.Client, deadline: float) -> codex_mesh.RpcMessage | None:
            if not failed[0]:
                failed[0] = True
                self.assertEqual(self.replace_worker(), replacement)
                raise ConnectionError("invented stream failure")
            return original(client, deadline)
        with patch.object(codex_mesh.Client, "next_frame", fail_after_replacement):
            code, errors = self.run_start()
        self.assertEqual(code, 1)
        self.assertIn("invented stream failure", errors)
        self.assertEqual(self.worker_record(), replacement)

    def test_old_thread_start_and_token_notices_preserve_every_replacement_field(self) -> None:
        original = codex_mesh.Client.next_frame
        replacement = self.replacement_record()
        notices = [
            codex_mesh.RpcMessage(method="turn/started", params={"threadId": THREAD_ID, "turn": {"id": "turn-1"}}),
            codex_mesh.RpcMessage(method="thread/tokenUsage/updated", params={"threadId": THREAD_ID, "turnId": "turn-1",
                                 "tokenUsage": {"last": {"inputTokens": 99999}, "total": {"inputTokens": 0}}}),
        ]
        observed: list[dict[str, object]] = []
        @bounded_fixture
        def next_frame(client: codex_mesh.Client, deadline: float) -> codex_mesh.RpcMessage | None:
            if len(notices) == 2:
                self.assertEqual(self.replace_worker(), replacement)
            else:
                observed.append(self.worker_record())
            if notices:
                return notices.pop(0)
            return original(client, deadline)
        with patch.object(codex_mesh.Client, "next_frame", next_frame):
            code, errors = self.run_start()
        self.assertEqual((code, errors), (0, ""))
        self.assertGreaterEqual(len(observed), 2)
        for entry in observed:
            self.assertEqual(entry, replacement)
        self.assertEqual(self.worker_record(), replacement)

    def test_compaction_reply_after_replacement_preserves_its_context_and_identity(self) -> None:
        self.context_sizes = [150000]
        self.assertEqual(self.run_start()[0], 0)
        replacement = self.replacement_record()
        respond = self.respond
        def reply(request: dict[str, object]) -> list[dict[str, object]] | None:
            frames = respond(request)
            if request.get("method") == "thread/compact/start":
                self.assertEqual(self.replace_worker(), replacement)
            return frames
        self.server.respond = reply
        code, output, errors = self.run_mesh(["compact", "--to", "worker"])
        self.assertEqual((code, output, errors), (0, "compacted worker: 150000 -> 20000 tokens\n", ""))
        self.assertEqual(self.worker_record(), replacement)

    def test_compaction_checks_ownership_after_acquiring_the_roster_lock(self) -> None:
        self.context_sizes = [150000]
        self.assertEqual(self.run_start()[0], 0)
        replacement = self.replacement_record()
        armed = [False]
        writes = [0]
        flock = fcntl.flock
        respond = self.respond
        def reply(request: dict[str, object]) -> list[dict[str, object]] | None:
            frames = respond(request)
            if request.get("method") == "thread/compact/start":
                armed[0] = True
            return frames
        path = self.session_dir / codex_mesh.ROSTER_FILE
        def acquire(descriptor: int, operation: int) -> None:
            # Matched by inode, not /proc/self/fd, which macOS lacks.
            locks_roster = path.exists() and os.path.samestat(os.fstat(descriptor), path.stat())
            if armed[0] and operation == fcntl.LOCK_EX and locks_roster:
                armed[0] = False
                with path.open("r+") as other:
                    flock(other.fileno(), fcntl.LOCK_EX)
                    entries = cast(dict[str, object], json.loads(other.read()))
                    entries["worker"] = replacement
                    _ = other.seek(0)
                    _ = other.truncate()
                    _ = other.write(json.dumps(entries))
                    other.flush()
                    flock(other.fileno(), fcntl.LOCK_UN)
                writes[0] += 1
            flock(descriptor, operation)
        self.server.respond = reply
        with patch.object(fcntl, "flock", acquire):
            code, output, errors = self.run_mesh(["compact", "--to", "worker"])
        self.assertEqual((code, output, errors), (0, "compacted worker: 150000 -> 20000 tokens\n", ""))
        self.assertEqual(writes[0], 1)
        self.assertEqual(self.worker_record(), replacement)

    def test_steer_on_the_same_thread_keeps_fields_and_records_the_live_turn(self) -> None:
        original = {**self.replacement_record(), "thread_id": THREAD_ID, "turn_id": "stale-turn"}
        self.write_pool_roster({"worker": original})
        calls = [0]
        respond = self.respond
        def reply(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "turn/steer":
                calls[0] += 1
                if calls[0] == 1:
                    return [{"id": request["id"], "error": {"message": "expected turn, but found `live-turn`"}}]
                return [_reply(request, {})]
            return respond(request)
        self.server.respond = reply
        code, output, errors = self.run_mesh(["steer", "--to", "worker", "--message", "invented correction"])
        self.assertEqual((code, output, errors), (0, "steered worker\n", ""))
        self.assertEqual(self.worker_record(), {
            **original, "turn_id": "live-turn", "revision": 1,
            "access_policy": {"kind": "ReadOnlyReviewer"},
        })

    def test_steer_reply_after_replacement_preserves_its_context_and_identity(self) -> None:
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "running",
                                      "turn_id": "stale-turn", "context_tokens": 150000,
                                      "port": self.server.port}})
        replacement = self.replacement_record()
        calls = [0]
        respond = self.respond
        def reply(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "turn/steer":
                calls[0] += 1
                if calls[0] == 1:
                    return [{"id": request["id"], "error": {"message": "expected turn, but found `live-turn`"}}]
                self.assertEqual(self.replace_worker(), replacement)
                return [_reply(request, {})]
            return respond(request)
        self.server.respond = reply
        code, output, errors = self.run_mesh(["steer", "--to", "worker", "--message", "invented correction"])
        self.assertEqual((code, output, errors), (0, "steered worker\n", ""))
        self.assertEqual(calls[0], 2)
        self.assertEqual(self.worker_record(), replacement)

    def taken_fork(self, *, release_unknown: bool, initially_absent: bool = False) -> tuple[int, str, str]:
        entries: dict[str, object] = {"template": {"thread_id": THREAD_ID, "status": "done"}}
        if not initially_absent:
            entries["reviewer"] = {"thread_id": "ended-thread", "status": "ended"}
        self.write_pool_roster(entries)
        self.server.requests.clear()
        marker = self.session_dir / "reviewer.end"
        pending = self.session_dir / "reviewer.pending.json"
        _ = marker.write_text("replacement marker\n", encoding="utf-8")
        _ = pending.write_text('{"messages": ["replacement message"]}\n', encoding="utf-8")
        respond = self.respond
        replacement = self.replacement_record()
        def reply(request: dict[str, object]) -> list[dict[str, object]] | None:
            frames = respond(request)
            if request.get("method") == "thread/name/set":
                self.assertEqual(self.replace_worker("reviewer"), replacement)
            if request.get("method") == "thread/unsubscribe" and release_unknown:
                return [{"id": request["id"], "error": {"message": "invented release refusal"}}]
            return frames
        self.server.respond = reply
        result = self.run_mesh(["fork", "--from", "template", "--name", "reviewer"])
        self.assertEqual(self.roster()["reviewer"], replacement)
        self.assertEqual(marker.read_text(encoding="utf-8"), "replacement marker\n")
        self.assertEqual(pending.read_text(encoding="utf-8"), '{"messages": ["replacement message"]}\n')
        self.assertEqual([_as_test_dict(request["params"]).get("threadId")
                          for request in self.methods("thread/unsubscribe")], [self.fork_thread_id])
        return result

    def test_fork_taken_during_request_releases_only_its_new_thread_and_refuses(self) -> None:
        for initially_absent in (False, True):
            with self.subTest(initially_absent=initially_absent):
                code, output, errors = self.taken_fork(release_unknown=False, initially_absent=initially_absent)
                self.assertEqual((code, output, errors), (2, "",
                                 "codex_mesh: reviewer: name was taken while the fork ran; fork refused\n"))

    def test_fork_taken_during_request_reports_unconfirmed_release_without_touching_replacement(self) -> None:
        code, output, errors = self.taken_fork(release_unknown=True)
        self.assertEqual((code, output), (1, ""))
        self.assertEqual(errors, f"codex_mesh: reviewer: name was taken while the fork ran; "
                         + f"fork thread {self.fork_thread_id} not released: "
                         + 'old thread release could not be confirmed: codex_mesh: thread/unsubscribe failed: '
                         + '{"message": "invented release refusal"}\n')

    def test_prime_creates_and_names_a_read_only_review_template_without_a_turn(self) -> None:
        self.configure_context()
        arguments = self.prime_cli("--model", "gpt-test", "--service-tier", "fast", "--task", "delegate.review")
        instructions = (self.session_dir / "review-instructions.txt").read_text(encoding="utf-8")
        self.write_pool_roster({"other": {"thread_id": "other-thread", "status": "done"}})

        with patch.object(codex_mesh, "ensure_server", return_value=(self.server.port, False)) as ensure:
            code, output, errors = self.run_mesh(arguments)
        self.assertEqual(ensure.call_args.kwargs.get("cwd"), str(self.session_dir))

        self.assertEqual((code, output, errors), (0, f"{THREAD_ID}\n", ""))
        requests = [request for request in self.server.requests if request.get("method") != "initialize"]
        self.assertEqual([request["method"] for request in requests], ["thread/start", "thread/name/set"])
        params = _as_test_dict(requests[0].get("params"))
        self.assertEqual(params.get("developerInstructions"), instructions)
        self.assertEqual(params.get("sandbox"), "read-only")
        self.assertEqual(params.get("approvalPolicy"), "never")
        self.assertEqual(params.get("cwd"), str(self.session_dir))
        self.assertEqual(params.get("model"), "gpt-test")
        self.assertEqual(params.get("serviceTier"), "fast")
        self.assertEqual(requests[1]["params"], {
            "threadId": THREAD_ID, "name": "natedev · models · code reviewer"})
        self.assert_review_record("template", THREAD_ID)
        self.assertEqual(self.roster()["other"], {"thread_id": "other-thread", "status": "done"})
        self.assertEqual(_as_test_dict(self.roster()["template"]).get("context_tokens"), 0)
        self.assertEqual(_as_test_dict(self.roster()["template"]).get("task"), "delegate.review")
        self.assertEqual(self.methods("turn/start"), [])

    def test_prime_accepts_omitted_model_and_service_tier(self) -> None:
        code, output, errors = self.run_mesh(self.prime_cli())

        self.assertEqual((code, output, errors), (0, f"{THREAD_ID}\n", ""))
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assert_review_record("template", THREAD_ID)

    def test_prime_reports_request_failures_and_closes_every_client(self) -> None:
        for method in ("thread/start", "thread/name/set"):
            with self.subTest(method=method):
                self.write_pool_roster({})
                self.failing_method = method
                self.assert_request_failure_closes_clients(self.prime_cli(), "template")
                self.assertNotIn("template", self.roster())

    def test_prime_reports_an_unreadable_instructions_file_as_a_named_failure(self) -> None:
        code, output, errors = self.run_mesh([
            "prime", "--name", "template", "--cwd", str(self.session_dir),
            "--instructions-file", str(self.session_dir / "missing-instructions.txt"),
        ])

        self.assertEqual(code, 1, errors)
        self.assertEqual(output, "")
        self.assertTrue(errors.startswith("codex_mesh: template: "), errors)
        self.assertIn("missing-instructions.txt", errors)
        self.assertEqual(self.methods("thread/start"), [])

    def test_fork_resumes_the_source_before_creating_and_naming_a_read_only_review_thread(self) -> None:
        self.configure_context()
        source = {"thread_id": THREAD_ID, "status": "done", "role": "review"}
        self.write_pool_roster({"template": source})

        code, output, errors = self.run_mesh(["fork", "--from", "template", "--name", "reviewer"])

        self.assertEqual((code, output, errors), (0, f"{self.fork_thread_id}\n", ""))
        requests = [request for request in self.server.requests if request.get("method") != "initialize"]
        self.assertEqual([request["method"] for request in requests],
                         ["thread/resume", "thread/fork", "thread/name/set"])
        self.assertEqual(_as_test_dict(requests[0]["params"]).get("threadId"), THREAD_ID)
        self.assertEqual(requests[1]["params"], {
            "threadId": THREAD_ID, "sandbox": "read-only", "approvalPolicy": "never",
        })
        self.assertEqual(requests[2]["params"], {
            "threadId": self.fork_thread_id, "name": "natedev · models · code reviewer 2"})
        self.assert_review_record("reviewer", self.fork_thread_id)
        self.assertEqual(self.roster()["template"], source)
        self.assertEqual(self.methods("turn/start"), [])

    def test_fork_refuses_a_destination_with_any_nonended_thread(self) -> None:
        for status in ("done", "failed", "running", "starting", "waiting_capacity", "exhausted", "unknown", ""):
            with self.subTest(status=status):
                entries: dict[str, object] = {
                    "template": {"thread_id": THREAD_ID, "status": "done"},
                    "reviewer": {"thread_id": "occupied-thread", "status": status},
                }
                self.write_pool_roster(entries)
                self.server.requests.clear()

                code, output, errors = self.run_mesh(["fork", "--from", "template", "--name", "reviewer"])

                self.assertEqual(code, 2, errors)
                self.assertEqual(output, "")
                self.assertTrue(errors.startswith("codex_mesh: reviewer: "), errors)
                self.assertEqual(self.roster(), entries)
                self.assertEqual(self.methods("thread/fork"), [])
                self.assertEqual(self.methods("thread/name/set"), [])

    def test_fork_replaces_an_ended_destination_thread(self) -> None:
        self.write_pool_roster({
            "template": {"thread_id": THREAD_ID, "status": "done"},
            "reviewer": {"thread_id": "ended-thread", "status": "ended", "context_tokens": 150000},
        })

        code, output, errors = self.run_mesh(["fork", "--from", "template", "--name", "reviewer"])

        self.assertEqual((code, output, errors), (0, f"{self.fork_thread_id}\n", ""))
        self.assertEqual(len(self.methods("thread/fork")), 1)
        self.assert_review_record("reviewer", self.fork_thread_id)
        self.assertEqual(_as_test_dict(self.roster()["reviewer"]).get("context_tokens"), 0)

    def standing_instructions(self) -> Path:
        path = self.session_dir / "standing-instructions.txt"
        _ = path.write_text("Review only.\nEnd with café.\n", encoding="utf-8")
        return path

    def seed_instruction_thread(self, path: Path) -> None:
        self.write_pool_roster({"worker": {
            "thread_id": THREAD_ID, "status": "done", "role": "review",
            "context_tokens": 150000, "instructions_file": str(path),
            "access_policy": {"kind": "ReadOnlyReviewer"},
        }})

    def assert_instruction_resume(self, path: Path) -> None:
        resumes = self.methods("thread/resume")
        self.assertTrue(resumes, "the verb must resume the roster thread")
        for request in resumes:
            self.assertEqual(request["params"], {
                "threadId": THREAD_ID,
                "developerInstructions": path.read_text(encoding="utf-8"),
                "sandbox": "read-only", "approvalPolicy": "never",
            })

    def test_prime_records_the_absolute_instructions_path(self) -> None:
        path = self.standing_instructions()

        code, _output, errors = self.run_mesh([
            "prime", "--name", "template", "--cwd", str(self.session_dir),
            "--instructions-file", os.path.relpath(path),
        ])

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(_as_test_dict(self.roster()["template"]).get("instructions_file"), str(path))

    def test_prime_uses_the_numbered_worker_home_despite_the_process_home(self) -> None:
        target, new = self.prepare_workers_move()
        self.assertEqual(accounts.main(["set-role", "workers", "codex1"]), 0)
        self.assertNotEqual(agent_accounts.codex_home(), target)

        with patch.object(codex_mesh, "_stop_unused_with_lock_held", return_value=False):
            code, _output, errors = self.run_mesh(self.prime_cli())

        self.assertEqual((code, errors), (0, ""))
        record = cast(codex_mesh.ServerRecord, json.loads((self.session_dir / codex_mesh.SERVER_FILE).read_text()))
        self.assertEqual(record.get("codex_home"), str(target))
        self.assertTrue(any(request.get("method") == "thread/start" for request in new.requests))
        self.assertEqual(self.methods("thread/start"), [])

    def test_moved_worker_resume_restores_its_standing_instructions(self) -> None:
        path = self.standing_instructions()
        self.seed_instruction_thread(path)
        _target, new = self.prepare_workers_move()
        self.assertEqual(accounts.main(["set-role", "workers", "codex1"]), 0)
        client = codex_mesh.Client(self.server.port, "worker")
        try:
            with patch.object(codex_mesh, "_stop_unused_with_lock_held", return_value=False):
                moved = codex_mesh._move_resident(  # pyright: ignore[reportPrivateUsage]
                    self.start_args(), str(self.session_dir), "worker", THREAD_ID, client)
            self.assertIsInstance(moved, codex_mesh.MovedWorker)
            assert isinstance(moved, codex_mesh.MovedWorker)
            moved.client.close()
        finally:
            client.close()

        resumes = [request for request in new.requests if request.get("method") == "thread/resume"]
        self.assertEqual([request["params"] for request in resumes], [{
            "threadId": THREAD_ID, "developerInstructions": path.read_text(encoding="utf-8"),
            "sandbox": "read-only", "approvalPolicy": "never",
        }])
        self.assertEqual(self.worker_record().get("instructions_file"), str(path))

        self.assertEqual(self.worker_record()["role"], "review")
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)
        self.assertEqual(set(self.roster()), {"worker"})

    def test_replacement_worker_keeps_standing_instructions_and_read_only_policy(self) -> None:
        path = self.standing_instructions()
        self.seed_instruction_thread(path)
        _target, new = self.prepare_workers_move()
        self.assertEqual(accounts.main(["set-role", "workers", "codex1"]), 0)
        replacement = "replacement-review-thread"
        respond = new.respond
        def refuse_resume(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/resume":
                return [{"id": request["id"], "error": {"message": "resume refused"}}]
            if request.get("method") == "thread/start":
                return [_reply(request, {"thread": {"id": replacement}})]
            return respond(request)
        new.respond = refuse_resume
        client = codex_mesh.Client(self.server.port, "worker")
        try:
            with patch.object(codex_mesh, "_stop_unused_with_lock_held", return_value=False):
                moved = codex_mesh._move_resident(  # pyright: ignore[reportPrivateUsage]
                    self.start_args(), str(self.session_dir), "worker", THREAD_ID, client)
            self.assertIsInstance(moved, codex_mesh.MovedWorker)
            assert isinstance(moved, codex_mesh.MovedWorker)
            self.assertTrue(moved.thread_replaced)
            self.assertEqual(moved.thread_id, replacement)
            moved.client.close()
        finally:
            client.close()

        starts = [request for request in new.requests if request.get("method") == "thread/start"]
        self.assertEqual(len(starts), 1)
        params = _as_test_dict(starts[0]["params"])
        self.assertEqual(params.get("developerInstructions"), path.read_text(encoding="utf-8"))
        self.assertEqual((params.get("sandbox"), params.get("approvalPolicy")), ("read-only", "never"))
        self.assertEqual(self.worker_record().get("instructions_file"), str(path))
        self.assertIn("new thread", (self.session_dir / "board.log").read_text())

    def test_reset_and_fork_refuse_a_thread_held_by_a_retired_live_turn(self) -> None:
        path = self.standing_instructions()
        _target, new = self.prepare_workers_move()
        self.assertEqual(accounts.main(["set-role", "workers", "codex1"]), 0)
        respond = self.server.respond
        def held_thread(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/loaded/list":
                return [_reply(request, {"data": [THREAD_ID], "nextCursor": None})]
            if request.get("method") == "thread/read":
                return [_reply(request, {"thread": {"status": {"type": "active"}, "turns": []}})]
            if request.get("method") == "thread/turns/list":
                return [_reply(request, {"data": [{"id": "outgoing-turn", "status": "inProgress"}], "nextCursor": None})]
            return respond(request)
        self.server.respond = held_thread
        for arguments in (["reset", "--to", "worker"], ["fork", "--from", "worker", "--name", "reviewer"]):
            with self.subTest(verb=arguments[0]):
                self.seed_instruction_thread(path)
                before = self.roster()
                new.requests.clear()
                with patch.object(codex_mesh, "_stop_unused_with_lock_held", return_value=False), \
                        patch.object(codex_mesh, "_stop_unused", return_value=False):
                    code, output, errors = self.run_mesh(arguments)
                self.assertEqual((code, output), (2, ""))
                self.assertIn(f"{arguments[0]} refused", errors)
                self.assertIn("old server still has a live turn", errors)
                self.assertIn("worker worker", errors)
                expected = ({**before, "worker": {**_as_test_dict(before["worker"]), "revision": 2}}
                            if arguments[0] == "reset" else before)
                self.assertEqual(self.roster(), expected)
                self.assertFalse(any(request.get("method") in ("thread/resume", "thread/fork", "thread/revert")
                                     for request in new.requests))

    def test_reset_and_fork_stop_unused_retired_writer_before_reopening(self) -> None:
        path = self.standing_instructions()
        _target, new = self.prepare_workers_move()
        self.assertEqual(accounts.main(["set-role", "workers", "codex1"]), 0)
        stopped: list[int] = []
        respond = new.respond
        def reopen_after_stop(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/resume" and self.server.port not in stopped:
                return [{"id": request["id"], "error": {"message": "retired writer was not stopped"}}]
            return respond(request)
        def stop(_root: Path | None, _pid: int, port: int, _owner: codex_mesh.ServerOwnership) -> bool:
            stopped.append(port)
            return True
        new.respond = reopen_after_stop
        for arguments in (["reset", "--to", "worker"], ["fork", "--from", "worker", "--name", "reviewer"]):
            with self.subTest(verb=arguments[0]):
                self.seed_instruction_thread(path)
                stopped.clear()
                new.requests.clear()
                with patch.object(codex_mesh, "_stop_unused_with_lock_held", return_value=False), \
                        patch.object(codex_mesh, "_stop_unused", side_effect=stop):
                    code, _output, errors = self.run_mesh(arguments)
                self.assertEqual((code, errors), (0, ""))
                self.assertEqual(stopped, [self.server.port])
                self.assertTrue(any(request.get("method") == "thread/resume" for request in new.requests))

    def test_reset_and_fork_confirm_retired_thread_release_before_reopening(self) -> None:
        path = self.standing_instructions()
        _target, new = self.prepare_workers_move()
        self.assertEqual(accounts.main(["set-role", "workers", "codex1"]), 0)
        released = [False]
        respond = self.server.respond
        def release_thread(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/loaded/list":
                return [_reply(request, {"data": [] if released[0] else [THREAD_ID], "nextCursor": None})]
            if request.get("method") == "thread/unsubscribe":
                released[0] = True
            return respond(request)
        self.server.respond = release_thread
        target_respond = new.respond
        def require_release(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/resume" and not released[0]:
                return [{"id": request["id"], "error": {"message": "retired thread was not released"}}]
            return target_respond(request)
        new.respond = require_release
        for arguments in (["reset", "--to", "worker"], ["fork", "--from", "worker", "--name", "reviewer"]):
            with self.subTest(verb=arguments[0]):
                self.seed_instruction_thread(path)
                released[0] = False
                with patch.object(codex_mesh, "_stop_unused_with_lock_held", return_value=False), \
                        patch.object(codex_mesh, "_stop_unused", return_value=False):
                    code, _output, errors = self.run_mesh(arguments)
                self.assertEqual((code, errors), (0, ""))
                self.assertTrue(released[0])

    def test_held_send_reopens_on_the_worker_home_with_standing_instructions_before_delivery(self) -> None:
        path = self.standing_instructions()
        self.seed_instruction_thread(path)
        _target, new = self.prepare_workers_move()
        sent: list[int] = []
        def send_after_turn(_client: codex_mesh.Client, _thread_id: str, _deadline: float) -> bool:
            if not sent:
                self.assertEqual(accounts.main(["set-role", "workers", "codex1"]), 0)
                sent.append(codex_mesh.command_send(argparse.Namespace(
                    session_dir=str(self.session_dir), to="worker", message="review again", message_file="")))
            return False
        with patch.object(codex_mesh, "_stop_unused_with_lock_held", return_value=False), \
                patch.object(codex_mesh, "_more_work_coming", side_effect=send_after_turn):
            code, _output, errors = self.run_mesh(self.follow_cli())

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(sent, [0])
        methods = [request.get("method") for request in new.requests]
        self.assertLess(methods.index("thread/resume"), methods.index("turn/start"))
        resumes = [request for request in new.requests if request.get("method") == "thread/resume"]
        self.assertEqual(resumes[0]["params"], {
            "threadId": THREAD_ID, "developerInstructions": path.read_text(encoding="utf-8"),
            "sandbox": "read-only", "approvalPolicy": "never",
        })
        starts = [request for request in new.requests if request.get("method") == "turn/start"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(_as_test_dict(starts[0]["params"])["input"], [{"type": "text", "text": "review again"}])
        self.assertEqual(self.methods("thread/queue/add"), [])
        self.assertEqual(self.worker_record().get("instructions_file"), str(path))

    def test_reset_and_fork_refuse_an_unconfirmed_retired_writer(self) -> None:
        path = self.standing_instructions()
        for arguments in (["reset", "--to", "worker"], ["fork", "--from", "worker", "--name", "reviewer"]):
            with self.subTest(verb=arguments[0]):
                self.seed_instruction_thread(path)
                before = self.roster()
                self.server.requests.clear()
                with patch.object(codex_mesh, "_release_other_worker_writers", return_value=codex_mesh.WorkerThreadStateUnknown("writer release not confirmed")):
                    code, output, errors = self.run_mesh(arguments)
                self.assertEqual((code, output), (2, ""))
                self.assertIn("writer release not confirmed", errors)
                self.assertIn(f"{arguments[0]} refused", errors)
                expected = ({**before, "worker": {**_as_test_dict(before["worker"]), "revision": 2}}
                            if arguments[0] == "reset" else before)
                self.assertEqual(self.roster(), expected)
                self.assertEqual(self.methods("thread/resume"), [])

    def test_fork_supplies_and_copies_the_source_standing_instructions(self) -> None:
        path = self.standing_instructions()
        self.seed_instruction_thread(path)

        code, _output, errors = self.run_mesh(["fork", "--from", "worker", "--name", "reviewer"])

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.methods("thread/fork")[0]["params"], {
            "threadId": THREAD_ID, "sandbox": "read-only", "approvalPolicy": "never",
            "developerInstructions": path.read_text(encoding="utf-8"),
        })
        self.assertEqual(_as_test_dict(self.roster()["reviewer"]).get("instructions_file"), str(path))

    def test_fork_source_resume_supplies_standing_instructions(self) -> None:
        path = self.standing_instructions()
        self.seed_instruction_thread(path)

        code, _output, errors = self.run_mesh(["fork", "--from", "worker", "--name", "reviewer"])

        self.assertEqual((code, errors), (0, ""))
        self.assert_instruction_resume(path)

    def test_follow_resume_supplies_standing_instructions(self) -> None:
        path = self.standing_instructions()
        self.seed_instruction_thread(path)

        code, _output, errors = self.run_mesh(self.follow_cli())

        self.assertEqual((code, errors), (0, ""))
        self.assert_instruction_resume(path)

    def test_compact_resume_supplies_standing_instructions(self) -> None:
        path = self.standing_instructions()
        self.seed_instruction_thread(path)

        code, _output, errors = self.run_mesh(["compact", "--to", "worker"])

        self.assertEqual((code, errors), (0, ""))
        self.assert_instruction_resume(path)

    def test_reset_resume_supplies_standing_instructions(self) -> None:
        path = self.standing_instructions()
        self.seed_instruction_thread(path)

        code, _output, errors = self.run_mesh(["reset", "--to", "worker"])

        self.assertEqual((code, errors), (0, ""))
        self.assert_instruction_resume(path)

    def test_set_instructions_repoints_an_idle_reviewer_and_its_next_resume_sends_the_new_prompt(self) -> None:
        old = self.standing_instructions()
        self.seed_instruction_thread(old)
        new = old.with_name("new-standing.md")
        _ = new.write_text("new standing prompt\n", encoding="utf-8")

        code, _output, errors = self.run_mesh(["set-instructions", "--to", "worker", "--instructions-file", str(new)])

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)
        self.assertEqual(self.worker_record().get("instructions_file"), str(new))
        self.server.requests.clear()
        code, _output, errors = self.run_mesh(["reset", "--to", "worker"])
        self.assertEqual((code, errors), (0, ""))
        self.assert_instruction_resume(new)

    def test_set_instructions_refuses_a_busy_reviewer_and_a_missing_file(self) -> None:
        old = self.standing_instructions()
        self.seed_instruction_thread(old)
        code, _output, errors = self.run_mesh(["set-instructions", "--to", "worker", "--instructions-file",
                                               str(old.with_name("absent.md"))])
        self.assertEqual(code, 1)
        self.assertIn("no instructions file", errors)
        record = self.worker_record()
        self.write_pool_roster({"worker": {**record, "status": "running"}})
        code, _output, errors = self.run_mesh(["set-instructions", "--to", "worker", "--instructions-file", str(old)])
        self.assertEqual(code, 2)
        self.assertIn("instructions unchanged", errors)

    def test_follow_completion_keeps_the_standing_instructions_path(self) -> None:
        path = self.standing_instructions()
        self.seed_instruction_thread(path)

        code, _output, errors = self.run_mesh(self.follow_cli())

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.worker_record()["status"], "done")
        self.assertEqual(self.worker_record().get("instructions_file"), str(path))

    def test_resume_without_standing_instructions_keeps_the_original_request(self) -> None:
        cases = [self.follow_cli(), ["compact", "--to", "worker"],
                 ["reset", "--to", "worker"], ["fork", "--from", "worker", "--name", "reviewer"]]
        for arguments in cases:
            with self.subTest(verb=arguments[0]):
                self.write_pool_roster({"worker": {
                    "thread_id": THREAD_ID, "status": "done", "role": "review", "context_tokens": 150000,
                }})
                self.server.requests.clear()

                code, _output, errors = self.run_mesh(arguments)

                self.assertEqual((code, errors), (0, ""))
                resumes = self.methods("thread/resume")
                self.assertTrue(resumes)
                for request in resumes:
                    self.assertEqual(request["params"], {"threadId": THREAD_ID})

    def test_unreadable_standing_instructions_fail_before_any_turn(self) -> None:
        self.configure_context()
        path = self.session_dir / "missing-standing-instructions.txt"
        cases = [(self.follow_cli(), "worker"), (["compact", "--to", "worker"], "worker"),
                 (["reset", "--to", "worker"], "worker"),
                 (["fork", "--from", "worker", "--name", "reviewer"], "reviewer")]
        for arguments, name in cases:
            with self.subTest(verb=arguments[0]):
                self.seed_instruction_thread(path)
                self.server.requests.clear()

                code, output, errors = self.run_mesh(arguments)

                self.assertEqual(code, 1, errors)
                self.assertEqual(output, "")
                label = "natedev · models · code reviewer" if arguments[0] == "follow" else name
                self.assertTrue(errors.startswith(f"codex_mesh: {label}: "), errors)
                self.assertIn(path.name, errors)
                self.assertEqual(self.methods("turn/start"), [])
                self.assertEqual(self.methods("thread/compact/start"), [])
                self.assertEqual(self.methods("thread/revert"), [])
                self.assertEqual(self.methods("thread/fork"), [])

    def claim_review_follow(self) -> bool:
        return codex_mesh._claim_follow(  # pyright: ignore[reportPrivateUsage]
            str(self.session_dir), "worker", THREAD_ID, os.getpid(), "review"
        )

    def test_reset_and_a_follow_claim_between_idle_read_and_revert_have_one_winner(self) -> None:
        self.write_pool_roster({"worker": {
            "thread_id": THREAD_ID, "status": "done", "role": "review", "context_tokens": 9876,
        }})
        self.history_turns = [{"id": "first-turn", "status": "completed", "items": []}]
        claims: list[bool] = []
        claimed_records: list[dict[str, object]] = []
        respond = self.server.respond

        def claim_during_list(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/turns/list":
                claims.append(self.claim_review_follow())
                claimed_records.append(self.worker_record())
            return respond(request)

        self.server.respond = claim_during_list

        code, _output, errors = self.run_mesh(["reset", "--to", "worker"])

        self.assertEqual(len(claims), 1, "the claim must race after idle read, before revert")
        if claims[0]:
            self.assertEqual(code, 2, errors)
            self.assertEqual(self.methods("thread/revert"), [])
            self.assertEqual(self.worker_record(), claimed_records[0])
            self.assertEqual(self.worker_record()["status"], "starting")
            self.assertEqual(self.worker_record()["launcher_pid"], os.getpid())
        else:
            self.assertEqual((code, errors), (0, ""))
            self.assertEqual(len(self.methods("thread/revert")), 1)
            self.assertEqual(self.worker_record()["context_tokens"], 0)
            self.assertTrue(self.claim_review_follow(), "reset must release its winning claim")

    def test_reset_refuses_a_worker_with_a_follow_claim_and_preserves_the_launcher(self) -> None:
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "done", "role": "review"}})
        self.assertTrue(self.claim_review_follow())
        claimed = self.worker_record()

        code, _output, errors = self.run_mesh(["reset", "--to", "worker"])

        self.assertEqual(code, 2, errors)
        self.assertEqual(self.methods("thread/revert"), [])
        self.assertEqual(self.worker_record(), claimed)

    def test_reset_releases_its_claim_after_success_with_or_without_history(self) -> None:
        histories: list[list[dict[str, object]]] = [
            [], [{"id": "first-turn", "status": "completed", "items": []}],
        ]
        for history in histories:
            with self.subTest(history=history):
                self.write_pool_roster({"worker": {
                    "thread_id": THREAD_ID, "status": "done", "role": "review", "context_tokens": 9876,
                }})
                self.history_turns = history

                code, _output, errors = self.run_mesh(["reset", "--to", "worker"])

                self.assertEqual((code, errors), (0, ""))
                self.assertNotIn("launcher_pid", self.worker_record())
                self.assertTrue(self.claim_review_follow(), "follow must be claimable immediately")

    def test_reset_releases_its_claim_after_each_failed_request(self) -> None:
        self.history_turns = [{"id": "first-turn", "status": "completed", "items": []}]
        for method in ("thread/resume", "thread/read", "thread/turns/list", "thread/revert"):
            with self.subTest(method=method):
                self.write_pool_roster({"worker": {
                    "thread_id": THREAD_ID, "status": "done", "role": "review", "context_tokens": 9876,
                }})
                self.failing_method = method

                code, _output, errors = self.run_mesh(["reset", "--to", "worker"])

                self.assertEqual(code, 1, errors)
                self.assertEqual(self.worker_record()["context_tokens"], 9876)
                self.assertNotIn("launcher_pid", self.worker_record())
                self.assertTrue(self.claim_review_follow(), "failure must release reset ownership")

    def test_start_namespace_keeps_cwd_on_thread_start_and_out_of_turn_start(self) -> None:
        code, errors = self.run_start()

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.methods("thread/start")[0]["params"], {
            "cwd": str(self.session_dir), "approvalPolicy": "never",
            "sandbox": "danger-full-access", "model": "gpt-test",
        })
        self.assertEqual(self.methods("turn/start")[0]["params"], {
            "threadId": THREAD_ID,
            "input": [{"type": "text", "text": "Original worker task"}],
            "model": "gpt-test", "effort": "high", "sandboxPolicy": WRITE_POLICY, "serviceTier": "default",
        })

    def test_follow_cwd_is_parsed_into_the_turn_cwd_destination(self) -> None:
        parsed: list[argparse.Namespace] = []

        def capture(args: argparse.Namespace) -> int:
            parsed.append(args)
            return 0

        with patch.object(codex_mesh, "command_follow", capture):
            code, _output, errors = self.run_mesh(self.follow_cli("--cwd", str(self.session_dir)))

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(len(parsed), 1)
        self.assertEqual(getattr(parsed[0], "turn_cwd", ""), str(self.session_dir))
        self.assertFalse(hasattr(parsed[0], "cwd"))

    def test_fork_reports_request_failures_and_closes_every_client(self) -> None:
        for method in ("thread/resume", "thread/fork", "thread/name/set"):
            with self.subTest(method=method):
                self.write_pool_roster({"template": {"thread_id": THREAD_ID, "status": "done"}})
                self.failing_method = method
                self.assert_request_failure_closes_clients(
                    ["fork", "--from", "template", "--name", "reviewer"], "reviewer"
                )
                self.assertNotIn("reviewer", self.roster())

    def test_reset_reverts_before_the_earliest_turn_and_clears_context_for_followable_states(self) -> None:
        self.history_turns = [
            {"id": "first-turn", "status": "completed", "items": []},
            {"id": "later-turn", "status": "completed", "items": []},
        ]
        for status in ("done", "failed"):
            with self.subTest(status=status):
                original = {"thread_id": THREAD_ID, "status": status, "role": "review", "context_tokens": 9876}
                self.write_pool_roster({"worker": original})
                self.server.requests.clear()

                code, _output, errors = self.run_mesh(["reset", "--to", "worker"])

                self.assertEqual((code, errors), (0, ""))
                requests = [request for request in self.server.requests if request.get("method") != "initialize"]
                methods = [request["method"] for request in requests]
                self.assertIn("thread/read", methods)
                self.assertLess(methods.index("thread/read"), methods.index("thread/turns/list"))
                self.assertEqual([method for method in methods if method != "thread/read"],
                                 ["thread/resume", "thread/turns/list", "thread/revert"])
                self.assertEqual(_as_test_dict(self.methods("thread/resume")[0]["params"]).get("threadId"), THREAD_ID)
                self.assertEqual(_as_test_dict(self.methods("thread/turns/list")[0]["params"]).get("threadId"), THREAD_ID)
                self.assertEqual(self.methods("thread/revert")[0]["params"], {
                    "threadId": THREAD_ID, "beforeTurnId": "first-turn",
                })
                self.assertEqual(self.worker_record(), {**original, "context_tokens": 0, "revision": 3})
                self.assertEqual(self.methods("turn/start"), [])

    def test_reset_of_a_thread_without_turns_clears_context_without_reverting(self) -> None:
        self.write_pool_roster({"worker": {
            "thread_id": THREAD_ID, "status": "done", "role": "review", "context_tokens": 9876,
        }})

        code, _output, errors = self.run_mesh(["reset", "--to", "worker"])

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(len(self.methods("thread/resume")), 1)
        self.assertEqual(len(self.methods("thread/turns/list")), 1)
        self.assertEqual(self.methods("thread/revert"), [])
        self.assertEqual(self.worker_record()["context_tokens"], 0)

    def test_reset_refuses_nonfollowable_roster_states_without_reverting(self) -> None:
        for status in ("running", "starting", "waiting_capacity", "exhausted", "ended", "unknown", ""):
            with self.subTest(status=status):
                entries: dict[str, object] = {"worker": {
                    "thread_id": THREAD_ID, "status": status, "context_tokens": 9876,
                }}
                self.write_pool_roster(entries)
                self.server.requests.clear()

                code, output, errors = self.run_mesh(["reset", "--to", "worker"])

                self.assertEqual(code, 2, errors)
                self.assertEqual(output, "")
                self.assertTrue(errors.startswith("codex_mesh: worker: "), errors)
                self.assertEqual(self.roster(), entries)
                self.assertEqual(self.methods("thread/revert"), [])
                self.assertEqual(self.methods("thread/turns/list"), [])

    def test_reset_clears_a_thread_with_no_active_turn_after_a_refusal_or_unload(self) -> None:
        self.history_turns = [{"id": "refused-turn", "status": "failed", "items": []}]
        for status in ("systemError", "notLoaded"):
            with self.subTest(status=status):
                self.write_pool_roster({"worker": {
                    "thread_id": THREAD_ID, "status": "failed", "context_tokens": 9876,
                }})
                self.thread_statuses[THREAD_ID] = status
                self.server.requests.clear()

                code, _output, errors = self.run_mesh(["reset", "--to", "worker"])

                self.assertEqual((code, errors), (0, ""))
                self.assertEqual(self.methods("thread/revert")[0]["params"], {
                    "threadId": THREAD_ID, "beforeTurnId": "refused-turn",
                })
                self.assertEqual(self.worker_record()["context_tokens"], 0)
                self.assertEqual(self.methods("turn/start"), [])

    def test_reset_refuses_active_or_unknown_thread_states(self) -> None:
        for status in ("active", "unrecognized"):
            with self.subTest(status=status):
                entries: dict[str, object] = {"worker": {
                    "thread_id": THREAD_ID, "status": "done", "context_tokens": 9876,
                }}
                self.write_pool_roster(entries)
                self.thread_statuses[THREAD_ID] = status
                self.server.requests.clear()

                code, output, errors = self.run_mesh(["reset", "--to", "worker"])

                self.assertEqual(code, 2, errors)
                self.assertEqual(output, "")
                self.assertTrue(errors.startswith("codex_mesh: worker: "), errors)
                self.assertTrue(self.methods("thread/read"))
                self.assertEqual(self.methods("thread/turns/list"), [])
                self.assertEqual(self.methods("thread/revert"), [])
                self.assertEqual(self.roster(), {**entries, "worker": {**_as_test_dict(entries["worker"]), "revision": 2}})

    def test_reset_refuses_a_live_turn_without_erasing_its_context(self) -> None:
        self.live_turn = "live-review-turn"
        entries: dict[str, object] = {"worker": {
            "thread_id": THREAD_ID, "status": "done", "context_tokens": 9876,
        }}
        self.write_pool_roster(entries)

        code, output, errors = self.run_mesh(["reset", "--to", "worker"])

        self.assertEqual(code, 2, errors)
        self.assertEqual(output, "")
        self.assertTrue(errors.startswith("codex_mesh: worker: "), errors)
        self.assertEqual(self.methods("thread/revert"), [])
        self.assertEqual(self.roster(), {**entries, "worker": {**_as_test_dict(entries["worker"]), "revision": 2}})

    def test_reset_reports_request_failures_without_clearing_context_and_closes_clients(self) -> None:
        self.history_turns = [{"id": "first-turn", "status": "completed", "items": []}]
        for method in ("thread/resume", "thread/read", "thread/turns/list", "thread/revert"):
            with self.subTest(method=method):
                entries: dict[str, object] = {"worker": {
                    "thread_id": THREAD_ID, "status": "done", "context_tokens": 9876,
                }}
                self.write_pool_roster(entries)
                self.failing_method = method
                self.assert_request_failure_closes_clients(["reset", "--to", "worker"], "worker")
                self.assertEqual(self.roster(), {**entries, "worker": {**_as_test_dict(entries["worker"]), "revision": 2}})

    def test_follow_cli_sends_worktree_read_only_policy_and_images_after_text_in_order(self) -> None:
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "done", "role": "review"}})
        worktree = self.session_dir / "second-worktree"
        worktree.mkdir()
        images = [self.session_dir / "first.png", self.session_dir / "second.png"]
        for path in images:
            _ = path.write_bytes(b"image fixture")

        with patch.object(codex_mesh, "ensure_server", return_value=(self.server.port, False)) as ensure:
            code, _output, errors = self.run_mesh(self.follow_cli(
                "--cwd", str(worktree), "--read-only", "--image", str(images[0]), "--image", str(images[1]),
                "--model", "gpt-test", "--effort", "high",
            ))
        self.assertTrue(ensure.called)
        self.assertTrue(all(call.kwargs.get("cwd") == str(worktree) for call in ensure.call_args_list))

        self.assertEqual((code, errors), (0, ""))
        starts = self.methods("turn/start")
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["params"], {
            "threadId": THREAD_ID,
            "input": [
                {"type": "text", "text": "Review the second worktree"},
                {"type": "localImage", "path": str(images[0])},
                {"type": "localImage", "path": str(images[1])},
            ],
            "model": "gpt-test", "effort": "high", "cwd": str(worktree),
            "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
            "serviceTier": "default",
        })

    def test_follow_cli_without_new_flags_sends_the_seat_write_policy(self) -> None:
        cases: list[tuple[list[str], dict[str, object]]] = [
            ([], {}),
            (["--model", "gpt-test", "--effort", "high"], {"model": "gpt-test", "effort": "high"}),
        ]
        for options, overrides in cases:
            with self.subTest(options=options):
                self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "done", "role": "review"}})
                self.server.requests.clear()

                code, _output, errors = self.run_mesh(self.follow_cli(*options))

                self.assertEqual((code, errors), (0, ""))
                starts = self.methods("turn/start")
                self.assertEqual(len(starts), 1)
                self.assertEqual(starts[0]["params"], {
                    "threadId": THREAD_ID,
                    "input": [{"type": "text", "text": "Review the second worktree"}],
                    "sandboxPolicy": WRITE_POLICY,
                    "serviceTier": "default",
                    **overrides,
                })

    def test_follow_turn_sets_the_default_service_tier(self) -> None:
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "done", "role": "review"}})

        code, _output, errors = self.run_mesh(self.follow_cli("--service-tier", "default"))

        self.assertEqual((code, errors), (0, ""))
        starts = self.methods("turn/start")
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["params"], {
            "threadId": THREAD_ID,
            "input": [{"type": "text", "text": "Review the second worktree"}],
            "sandboxPolicy": WRITE_POLICY,
            "serviceTier": "default",
        })

    def tier_environment(self, registry: str, config: str = "") -> dict[str, str]:
        registry_file = self.session_dir / "agents.conf"
        config_file = self.session_dir / "codex.toml"
        _ = registry_file.write_text(registry, encoding="utf-8")
        _ = config_file.write_text(config, encoding="utf-8")
        return {"AGENTS_CONFIG_FILE": str(registry_file), "CODEX_CONFIG_FILE": str(config_file),
                "CODEX_PACER_TIER_FILE": str(self.session_dir / "pacer-tier")}

    def test_finished_run_wake_uses_the_recorded_task_and_not_the_launch_tier(self) -> None:
        for task, follow in (("delegate.review", True), ("ask_a_friend.consultation", False)):
            with self.subTest(task=task):
                function, row = task.split(".")
                environment = self.tier_environment(
                    f"[delegate.options]\ncodex_service_tier=flex\n"
                    + f"[{function}.options]\ncodex_service_tier.{row}=fast\n")
                self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "done"}})
                self.server.requests.clear()
                read = codex_mesh.Client.next_frame
                delivered = False

                @bounded_fixture
                def wake(client: codex_mesh.Client, deadline: float) -> codex_mesh.RpcMessage | None:
                    nonlocal delivered
                    frame = read(client, deadline)
                    if frame and frame.get("method") == "turn/completed" and not delivered:
                        delivered = True
                        with contextlib.redirect_stdout(io.StringIO()):
                            self.assertEqual(codex_mesh.command_send(argparse.Namespace(
                                session_dir=str(self.session_dir), to="worker",
                                message="Run finished: fixture build passed", message_file="")), 0)
                        _ = Path(environment["AGENTS_CONFIG_FILE"]).write_text(
                            "[delegate.options]\ncodex_service_tier=flex\n"
                            + f"[{function}.options]\ncodex_service_tier.{row}=default\n")
                    return frame

                with patch.dict(os.environ, environment), patch.object(codex_mesh.Client, "next_frame", wake):
                    if follow:
                        code, _output, errors = self.run_mesh(
                            self.follow_cli("--task", task, "--service-tier", "fast"))
                    else:
                        code, _output, errors = self.run_mesh([
                            "start", "--name", "worker", "--cwd", str(self.session_dir),
                            "--prompt-file", str(self.session_dir / "prompt.txt"),
                            "--summary-file", str(self.session_dir / "summary.txt"),
                            "--log-file", str(self.session_dir / "agent.log"),
                            "--task", task, "--service-tier", "fast", "--timeout", "5",
                        ])
                self.assertEqual((code, errors), (0, ""))
                params = [_as_test_dict(start["params"]) for start in self.methods("turn/start")]
                self.assertEqual([item.get("serviceTier") for item in params], ["fast", "default"])
                self.assertEqual(self.turn_inputs[-1], "Run finished: fixture build passed")
                self.assertEqual(self.worker_record().get("task"), task)

    def test_follow_resolves_registry_and_paced_tiers_instead_of_the_launch_tier(self) -> None:
        cases = [
            ("[delegate.options]\ncodex_service_tier=fast\ncodex_service_tier.review=default\n", "", "", "default"),
            ("", 'service_tier = "fast"\n', "", "default"),
            ("[delegate.options]\ncodex_service_tier=default\n", 'service_tier = "fast"\n', "", "default"),
            ("[delegate.options]\ncodex_service_tier=pace\n", "", "", "default"),
            ("[delegate.options]\ncodex_service_tier=fast\n", "", "default", "fast"),
        ]
        for registry, config, supplied, expected in cases:
            with self.subTest(registry=registry, supplied=supplied):
                environment = self.tier_environment(registry, config)
                self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "done",
                    "role": "review", "task": "delegate.review"}})
                self.server.requests.clear()
                with patch.dict(os.environ, environment):
                    code, _output, errors = self.run_mesh(self.follow_cli("--service-tier", supplied))
                self.assertEqual((code, errors), (0, ""))
                self.assertEqual(_as_test_dict(self.methods("turn/start")[0]["params"])["serviceTier"], expected)

    def test_unknown_task_uses_the_function_tier_and_registry_errors_do_not_stop_a_turn(self) -> None:
        for registry in ("[delegate.options]\ncodex_service_tier=default\n",
                         "[delegate.options]\ncodex_service_tier=broken\n"):
            with self.subTest(registry=registry):
                environment = self.tier_environment(registry)
                self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "done", "role": "review"}})
                self.server.requests.clear()
                with patch.dict(os.environ, environment):
                    code, _output, errors = self.run_mesh(self.follow_cli())
                self.assertEqual((code, errors), (0, ""))
                self.assertEqual(_as_test_dict(self.methods("turn/start")[0]["params"])["serviceTier"], "default")
                if "broken" in registry:
                    self.assertIn("cannot resolve service tier", (self.session_dir / "worker.log").read_text())
                    self.assertIn("broken", (self.session_dir / "worker.log").read_text())

    def test_follow_cli_accepts_each_new_flag_independently(self) -> None:
        image = self.session_dir / "review.png"
        _ = image.write_bytes(b"image fixture")
        cases: list[tuple[list[str], dict[str, object]]] = [
            (["--cwd", str(self.session_dir)], {"cwd": str(self.session_dir)}),
            (["--read-only"], {"sandboxPolicy": {"type": "readOnly", "networkAccess": False}}),
            (["--image", str(image)], {"input": [
                {"type": "text", "text": "Review the second worktree"},
                {"type": "localImage", "path": str(image)},
            ]}),
        ]
        for options, overrides in cases:
            with self.subTest(options=options):
                self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "done", "role": "review"}})
                self.server.requests.clear()

                code, _output, errors = self.run_mesh(self.follow_cli(*options))

                self.assertEqual((code, errors), (0, ""))
                starts = self.methods("turn/start")
                self.assertEqual(len(starts), 1)
                self.assertEqual(starts[0]["params"], {
                    "threadId": THREAD_ID,
                    "input": [{"type": "text", "text": "Review the second worktree"}],
                    "sandboxPolicy": WRITE_POLICY,
                    "serviceTier": "default",
                    **overrides,
                })

    def test_follow_after_a_read_only_follow_restores_the_write_policy(self) -> None:
        policies: list[object] = []
        for options in (["--read-only"], []):
            self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "done", "role": "test"}})
            self.server.requests.clear()

            code, _output, errors = self.run_mesh(self.follow_cli(*options))

            self.assertEqual((code, errors), (0, ""))
            starts = self.methods("turn/start")
            self.assertEqual(len(starts), 1)
            policies.append(_as_test_dict(starts[0]["params"]).get("sandboxPolicy"))
        self.assertEqual(policies, [READ_ONLY_POLICY, WRITE_POLICY])

    def test_follow_without_read_only_keeps_a_standing_reviewer_read_only(self) -> None:
        instructions = self.session_dir / "standing.txt"
        _ = instructions.write_text("Review only.", encoding="utf-8")
        self.write_pool_roster({"worker": {
            "thread_id": THREAD_ID, "status": "done", "role": "review",
            "instructions_file": str(instructions),
            "access_policy": {"kind": "ReadOnlyReviewer"},
        }})

        code, _output, errors = self.run_mesh(self.follow_cli())

        self.assertEqual((code, errors), (0, ""))
        starts = self.methods("turn/start")
        self.assertEqual(len(starts), 1)
        self.assertEqual(_as_test_dict(starts[0]["params"]).get("sandboxPolicy"), READ_ONLY_POLICY)

    def test_legacy_reviewer_resumes_and_follows_read_only_without_rewriting_on_read(self) -> None:
        instructions = self.standing_instructions()
        self.write_pool_roster({"worker": {
            "thread_id": THREAD_ID, "status": "done", "role": "review",
            "instructions_file": str(instructions),
        }})
        roster_path = self.session_dir / codex_mesh.ROSTER_FILE
        before = roster_path.read_bytes()
        modified = roster_path.stat().st_mtime_ns
        params = codex_mesh._resume_params(  # pyright: ignore[reportPrivateUsage]
            str(self.session_dir), "worker", THREAD_ID)
        client = codex_mesh.Client(self.server.port, "legacy-reviewer")
        try:
            _ = client.call("thread/resume", params)
        finally:
            client.close()
        self.assert_instruction_resume(instructions)
        self.assertEqual(roster_path.read_bytes(), before)
        self.assertEqual(roster_path.stat().st_mtime_ns, modified)

        code, errors = self.run_follow(role="review")

        self.assertEqual((code, errors), (0, ""))
        self.assert_instruction_resume(instructions)
        starts = self.methods("turn/start")
        self.assertEqual(len(starts), 1)
        self.assertEqual(_as_test_dict(starts[0]["params"]).get("sandboxPolicy"), READ_ONLY_POLICY)
        self.assertEqual(self.methods("thread/start"), [])

    def test_first_locked_roster_write_migrates_every_legacy_reviewer(self) -> None:
        instructions = self.standing_instructions()
        for writer in ("update", "compare-update", "set-instructions"):
            with self.subTest(writer=writer):
                explicit: dict[str, object] = {
                    "thread_id": "explicit-thread", "status": "done", "instructions_file": str(instructions),
                    "access_policy": {"kind": "PhaseBoundDesigner", "binding_generation": 2,
                                      "launch_sandbox": "danger-full-access"},
                }
                worker: dict[str, object] = {"thread_id": "writer-thread", "status": "done"}
                damaged: dict[str, object] = {
                    "thread_id": "held-thread", "status": "done", "held_design_id": "damaged-design",
                    "instructions_file": str(instructions),
                }
                self.write_pool_roster({
                    "worker": {"thread_id": THREAD_ID, "status": "done", "role": "review",
                               "instructions_file": str(instructions)},
                    "template": {"thread_id": "template-thread", "status": "done", "role": "review",
                                 "instructions_file": str(instructions)},
                    "explicit": explicit, "writer": worker, "damaged": damaged,
                })
                roster_path = self.session_dir / codex_mesh.ROSTER_FILE
                before = roster_path.read_bytes()
                modified = roster_path.stat().st_mtime_ns
                _ = codex_mesh._lookup(str(self.session_dir), "worker")  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(roster_path.read_bytes(), before)
                self.assertEqual(roster_path.stat().st_mtime_ns, modified)
                update: codex_mesh.ThreadRecord = {"thread_id": THREAD_ID, "status": "done"}
                if writer == "update":
                    codex_mesh._update_roster(str(self.session_dir), "worker", update)  # pyright: ignore[reportPrivateUsage]
                elif writer == "compare-update":
                    self.assertTrue(codex_mesh._update_roster_if_expected(  # pyright: ignore[reportPrivateUsage]
                        str(self.session_dir), "worker", update, codex_mesh.RosterOnThread(THREAD_ID)))
                else:
                    code, output, errors = self.run_mesh([
                        "set-instructions", "--to", "worker", "--instructions-file", str(instructions)])
                    self.assertEqual((code, output, errors), (0, "", ""))
                stored = self.roster()
                for name in ("worker", "template"):
                    self.assertEqual(_as_test_dict(stored[name]).get("access_policy"),
                                     {"kind": "ReadOnlyReviewer"})
                self.assertEqual(stored["explicit"], explicit)
                self.assertEqual(stored["writer"], worker)
                self.assertEqual(stored["damaged"], damaged)

    def test_held_record_without_policy_names_recovery_and_stays_unchanged(self) -> None:
        instructions = self.standing_instructions()
        self.write_pool_roster({"worker": {
            "thread_id": THREAD_ID, "status": "done", "held_design_id": "damaged-design",
            "instructions_file": str(instructions),
        }})
        roster_path = self.session_dir / codex_mesh.ROSTER_FILE
        before = roster_path.read_bytes()
        with self.assertRaisesRegex(SystemExit, r"design_roles\.py recover"):
            _ = codex_mesh._worker_sandbox(  # pyright: ignore[reportPrivateUsage]
                self.start_args(), str(self.session_dir), "worker")
        with self.assertRaisesRegex(SystemExit, r"design_roles\.py recover"):
            _ = codex_mesh._resume_params(  # pyright: ignore[reportPrivateUsage]
                str(self.session_dir), "worker", THREAD_ID)
        self.assertEqual(roster_path.read_bytes(), before)
        self.assertEqual(self.methods("thread/resume"), [])
        self.assertEqual(self.methods("turn/start"), [])

    def test_worker_without_instructions_keeps_each_launch_sandbox(self) -> None:
        for sandbox, expected in (("read-only", READ_ONLY_POLICY), ("danger-full-access", WRITE_POLICY)):
            with self.subTest(sandbox=sandbox):
                self.write_pool_roster({"worker": {
                    "thread_id": THREAD_ID, "status": "done", "role": "impl",
                }})
                self.server.requests.clear()
                args = self.start_args()
                args.sandbox = sandbox
                args.to = "worker"
                args.message_file = str(self.session_dir / "prompt.txt")
                code = codex_mesh.command_follow(args)
                self.assertEqual(code, 0)
                starts = self.methods("turn/start")
                self.assertEqual(len(starts), 1)
                self.assertEqual(_as_test_dict(starts[0]["params"]).get("sandboxPolicy"), expected)
                self.assertNotIn("access_policy", self.worker_record())

    def test_pool_verbs_report_server_failures_with_the_destination_name(self) -> None:
        cases = [
            (self.prime_cli(), "template"),
            (["fork", "--from", "template", "--name", "reviewer"], "reviewer"),
            (["reset", "--to", "worker"], "worker"),
        ]
        for arguments, name in cases:
            with self.subTest(verb=arguments[0]):
                self.write_pool_roster({
                    "template": {"thread_id": THREAD_ID, "status": "done"},
                    "worker": {"thread_id": THREAD_ID, "status": "done"},
                })
                with patch.object(codex_mesh, "ensure_server", side_effect=OSError("server unavailable")):
                    code, output, errors = self.run_mesh(arguments)

                self.assertEqual((code, output, errors), (1, "", f"codex_mesh: {name}: server unavailable\n"))

    def test_follow_records_its_server_before_it_reopens_the_thread(self) -> None:
        # A message sent while the follow-up is starting reaches the worker through this port.
        code, errors = self.run_start()
        self.assertEqual((code, errors), (0, ""))
        ports: list[object] = []
        respond = self.server.respond

        def watch(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/resume":
                ports.append(self.worker_record().get("port"))
            return respond(request)

        self.server.respond = watch
        code, errors = self.run_follow()
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(ports, [self.server.port])

    def test_paused_seat_can_be_claimed_and_followed_on_its_original_thread(self) -> None:
        self.write_pool_roster({"worker": {
            "thread_id": THREAD_ID, "status": "paused", "role": "impl",
            "paused_from": "running", "paused_turn": True,
        }})
        code, _output, errors = self.run_mesh(["can-follow", "--to", "worker", "--role", "impl"])
        self.assertEqual((code, errors), (0, ""))
        code, errors = self.run_follow()
        self.assertEqual((code, errors), (0, ""))
        record = self.worker_record()
        self.assertEqual(record["thread_id"], THREAD_ID)
        self.assertEqual(record["status"], "done")
        self.assertNotIn("paused_from", record)
        self.assertNotIn("paused_turn", record)
        self.assertEqual(self.methods("thread/start"), [])
        self.assertEqual([request["params"] for request in self.methods("thread/resume")],
                         [{"threadId": THREAD_ID}])

    def test_reopen_recovers_an_ended_thread_and_follow_resumes_it(self) -> None:
        self.write_pool_roster({"worker": {
            "thread_id": THREAD_ID, "status": "ended", "role": "impl",
            "launcher_pid": 99999999,
        }})
        marker = codex_mesh._end_marker_path(str(self.session_dir), "worker")  # pyright: ignore[reportPrivateUsage]
        marker.touch()
        (self.session_dir / codex_mesh.SERVER_FILE).unlink()
        code, output, errors = self.run_mesh(["reopen", "--to", "worker"])
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(output, "reopened worker; send its next turn with --to worker\n")
        self.assertFalse(marker.exists())
        self.assertEqual(self.worker_record()["status"], "paused")
        self.assertEqual(self.worker_record()["paused_from"], "ended")
        self.assertIs(self.worker_record()["paused_turn"], False)
        with patch.object(codex_mesh, "ensure_server", return_value=(self.server.port, True)):
            code, errors = self.run_follow()
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)
        self.assertEqual(self.worker_record()["role"], "impl")
        self.assertEqual(set(self.roster()), {"worker"})
        self.assertEqual(self.methods("thread/start"), [])
        self.assertEqual(len(self.methods("thread/resume")), 1)

    def test_reopen_on_a_finished_run_names_reactivation_and_a_new_run_as_remedies(self) -> None:
        self.write_pool_roster({"worker": {
            "thread_id": THREAD_ID, "status": "ended", "role": "impl",
            "launcher_pid": 99999999,
        }})
        marker = codex_mesh._end_marker_path(str(self.session_dir), "worker")  # pyright: ignore[reportPrivateUsage]
        marker.touch()
        (self.session_dir / codex_mesh.SERVER_FILE).unlink()
        state_path = self.session_dir / plan_history.STATE_FILENAME
        _ = state_path.write_text(json.dumps({"status": "completed", "finished_at": 1760658000}))
        before = self.roster()
        code, output, errors = self.run_mesh(["reopen", "--to", "worker"])
        self.assertEqual(code, 2, output + errors)
        self.assertIn("workers.py resolve", output + errors)
        self.assertIn("start-phase", output + errors)
        self.assertIn("--session-dir <new-run>", output + errors)
        self.assertEqual(self.roster(), before)
        self.assertTrue(marker.exists())

        _ = state_path.write_text(json.dumps({"status": "active"}))
        code, output, errors = self.run_mesh(["reopen", "--to", "worker"])
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(output, "reopened worker; send its next turn with --to worker\n")
        self.assertEqual(self.worker_record()["status"], "paused")
        self.assertFalse(marker.exists())

    def test_reopen_refuses_missing_thread_or_live_launcher_without_mutation(self) -> None:
        for fields in ({"launcher_pid": os.getpid(), "thread_id": THREAD_ID},
                       {"launcher_pid": 99999999}):
            with self.subTest(fields=fields):
                self.write_pool_roster({"worker": {"status": "ended", **fields}})
                marker = codex_mesh._end_marker_path(str(self.session_dir), "worker")  # pyright: ignore[reportPrivateUsage]
                marker.touch()
                before = self.roster()
                code, output, errors = self.run_mesh(["reopen", "--to", "worker"])
                self.assertEqual((code, output), (2, ""))
                self.assertEqual(len(errors.splitlines()), 1)
                self.assertIn("worker", errors)
                self.assertEqual(self.roster(), before)
                self.assertTrue(marker.exists())

    def test_reopen_recovers_orphaned_active_seats_only_after_the_server_is_gone(self) -> None:
        for status in ("running", "starting"):
            with self.subTest(status=status):
                self.write_pool_roster({"worker": {
                    "thread_id": THREAD_ID, "status": status, "role": "impl",
                    "turn_id": "interrupted-turn", "launcher_pid": 99999999,
                }})
                before = self.roster()
                code, output, errors = self.run_mesh(["reopen", "--to", "worker"])
                self.assertEqual((code, output), (2, ""))
                self.assertEqual(len(errors.splitlines()), 1)
                self.assertEqual(self.roster(), before)

                server_record = self.session_dir / codex_mesh.SERVER_FILE
                saved_server = server_record.read_text()
                server_record.unlink()
                code, output, errors = self.run_mesh(["reopen", "--to", "worker"])
                self.assertEqual((code, errors), (0, ""))
                self.assertEqual(output, "reopened worker; send its next turn with --to worker\n")
                self.assertEqual(self.worker_record()["status"], "paused")
                self.assertEqual(self.worker_record()["paused_from"], status)
                self.assertIs(self.worker_record()["paused_turn"], True)
                resumes_before = len(self.methods("thread/resume"))
                with patch.object(codex_mesh, "ensure_server", return_value=(self.server.port, True)):
                    code, errors = self.run_follow()
                self.assertEqual((code, errors), (0, ""))
                self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)
                self.assertEqual(len(self.methods("thread/resume")), resumes_before + 1)
                self.assertEqual(self.methods("thread/start"), [])
                _ = server_record.write_text(saved_server)

    def test_reopen_accepts_ended_seat_without_launcher_and_refuses_live_orphans(self) -> None:
        (self.session_dir / codex_mesh.SERVER_FILE).unlink()
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "ended"}})
        code, _output, errors = self.run_mesh(["reopen", "--to", "worker"])
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.worker_record()["status"], "paused")
        for status in ("running", "starting"):
            with self.subTest(status=status):
                self.write_pool_roster({"worker": {
                    "thread_id": THREAD_ID, "status": status,
                    "turn_id": "live-turn", "launcher_pid": os.getpid(),
                }})
                before = self.roster()
                code, output, errors = self.run_mesh(["reopen", "--to", "worker"])
                self.assertEqual((code, output), (2, ""))
                self.assertEqual(len(errors.splitlines()), 1)
                self.assertEqual(self.roster(), before)

    def test_reopen_checks_the_seats_server_port_when_another_server_is_alive(self) -> None:
        old_port = self.server.port + 1
        self.write_pool_roster({"worker": {
            "thread_id": THREAD_ID, "status": "running", "role": "impl",
            "turn_id": "interrupted-turn", "launcher_pid": 99999999, "port": old_port,
        }})
        _ = (self.session_dir / codex_mesh.RETIRED_FILE).write_text(json.dumps({
            "servers": [{"pid": 99999999, "port": old_port}],
        }))
        code, output, errors = self.run_mesh(["reopen", "--to", "worker"])
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(output, "reopened worker; send its next turn with --to worker\n")
        self.assertEqual(self.worker_record()["status"], "paused")
        self.assertIs(self.worker_record()["paused_turn"], True)
        self.assertTrue((self.session_dir / codex_mesh.SERVER_FILE).exists())

    def test_reopen_refuses_a_live_retired_server_on_the_workers_port(self) -> None:
        retired_path = self.session_dir / codex_mesh.RETIRED_FILE
        _ = retired_path.write_text(json.dumps({
            "servers": [{"pid": os.getpid(), "port": self.server.port}],
        }))
        (self.session_dir / codex_mesh.SERVER_FILE).unlink()
        for status in ("running", "starting"):
            with self.subTest(status=status):
                self.write_pool_roster({"worker": {
                    "thread_id": THREAD_ID, "status": status,
                    "turn_id": "interrupted-turn", "launcher_pid": 99999999,
                    "port": self.server.port,
                }})
                before = self.roster()
                code, output, errors = self.run_mesh(["reopen", "--to", "worker"])
                self.assertEqual((code, output), (2, ""))
                self.assertEqual(len(errors.splitlines()), 1)
                self.assertEqual(self.roster(), before)
                self.assertTrue(retired_path.exists())

    def test_follow_reuses_a_done_thread_and_records_its_new_turn(self) -> None:
        code, errors = self.run_start()
        self.assertEqual((code, errors), (0, ""))
        code, errors = self.run_follow()
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(len(self.methods("thread/start")), 1)
        resumes = self.methods("thread/resume")
        self.assertEqual(len(resumes), 1)
        self.assertEqual(
            cast("dict[str, object]", resumes[0]["params"])["threadId"], THREAD_ID
        )
        starts = self.methods("turn/start")
        self.assertEqual(len(starts), 2)
        self.assertEqual(
            cast("dict[str, object]", starts[1]["params"])["threadId"], THREAD_ID
        )
        self.assertIn("Repair the open finding", str(starts[1]["params"]))
        self.assertEqual(self.worker_record()["status"], "done")

    def test_follow_overrides_the_model_for_the_reassigned_role(self) -> None:
        code, errors = self.run_start()
        self.assertEqual((code, errors), (0, ""))

        code, errors = self.run_follow(role="review")

        self.assertEqual((code, errors), (0, ""))
        starts = self.methods("turn/start")
        follow_params = cast("dict[str, object]", starts[1]["params"])
        self.assertEqual(follow_params["model"], "gpt-test")

    def follow_with_name_taken(self, take_at: str, *, release_unknown: bool = False) -> tuple[int, str]:
        self.write_pool_roster({"worker": {"thread_id": "old-follow-thread", "status": "done",
                                       "role": "impl", "context_tokens": 123, "port": self.server.port}})
        replacement = self.replacement_record()
        marker = self.session_dir / "worker.end"
        pending = self.session_dir / "worker.pending.json"
        marker_text = "new owner's end marker\n"
        pending_text = json.dumps({"thread_id": replacement["thread_id"], "messages": ["new owner's message"]})
        respond = self.respond
        taken = [False]

        def reply(request: dict[str, object]) -> list[dict[str, object]] | None:
            method = request.get("method")
            if method == take_at:
                taken[0] = True
                self.assertEqual(self.replace_worker(), replacement)
                _ = marker.write_text(marker_text, encoding="utf-8")
                _ = pending.write_text(pending_text, encoding="utf-8")
            if method == "thread/resume":
                return [{"id": request["id"], "error": {"message": "invented resume refusal"}}]
            if method == "thread/unsubscribe" and release_unknown:
                return [{"id": request["id"], "error": {"message": "invented release refusal"}}]
            return respond(request)

        self.server.respond = reply
        result = self.run_follow()
        self.assertTrue(taken[0])
        self.assertEqual(self.worker_record(), replacement)
        self.assertEqual(marker.read_text(encoding="utf-8"), marker_text)
        self.assertEqual(pending.read_text(encoding="utf-8"), pending_text)
        self.assertEqual([_as_test_dict(request["params"]).get("threadId")
                          for request in self.methods("thread/unsubscribe")], [THREAD_ID])
        self.assertEqual(self.methods("turn/start"), [])
        self.assertNotIn(THREAD_ID, [record["id"] for record in self.usage_records()])
        if take_at == "thread/resume":
            self.assertEqual(self.methods("thread/name/set"), [])
            board = self.session_dir / "board.log"
            self.assertNotIn("worker continues as new thread", board.read_text() if board.exists() else "")
        return result

    def test_follow_name_taken_during_resume_releases_fallback_without_touching_new_owner(self) -> None:
        self.configure_context()
        code, errors = self.follow_with_name_taken("thread/resume")
        self.assertEqual((code, errors), (1, "codex_mesh: natedev · models · implementer: name was taken while the follow-up ran; "
                                         + "follow-up did not start\n"))

    def test_follow_name_taken_after_fallback_is_named_releases_it_without_starting_a_turn(self) -> None:
        self.configure_context()
        code, errors = self.follow_with_name_taken("thread/name/set")
        self.assertEqual((code, errors), (1, "codex_mesh: natedev · models · implementer: name was taken while the follow-up ran; "
                                         + "follow-up did not start\n"))

    def test_follow_name_taken_reports_unconfirmed_fallback_release_and_keeps_new_owner(self) -> None:
        self.configure_context()
        code, errors = self.follow_with_name_taken("thread/resume", release_unknown=True)
        self.assertEqual(code, 1)
        self.assertEqual(errors, "codex_mesh: natedev · models · implementer: name was taken while the follow-up ran; "
                         + f"replacement thread {THREAD_ID} not released: "
                         + "old thread release could not be confirmed: codex_mesh: thread/unsubscribe failed: "
                         + '{"message": "invented release refusal"}\n')

    def test_follow_refused_resume_without_replacement_owner_runs_on_the_fallback_thread(self) -> None:
        self.write_pool_roster({"worker": {"thread_id": "old-follow-thread", "status": "done",
                                       "role": "impl", "context_tokens": 123, "port": self.server.port}})
        respond = self.respond

        def reply(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/resume":
                return [{"id": request["id"], "error": {"message": "invented resume refusal"}}]
            return respond(request)

        self.server.respond = reply
        self.assertEqual(self.run_follow(), (0, ""))
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)
        self.assertEqual(self.worker_record()["status"], "done")
        self.assertEqual(self.worker_record()["context_tokens"], 123)
        self.assertEqual([_as_test_dict(request["params"]).get("threadId")
                          for request in self.methods("thread/name/set")], [THREAD_ID, THREAD_ID])
        self.assertEqual([_as_test_dict(request["params"]).get("threadId")
                          for request in self.methods("turn/start")], [THREAD_ID])
        self.assertEqual([record["id"] for record in self.usage_records()], ["old-follow-thread", THREAD_ID])
        self.assertIn(f"worker continues as new thread {THREAD_ID}",
                      (self.session_dir / "board.log").read_text(encoding="utf-8"))

    def test_follow_carries_held_messages_to_a_replacement_thread(self) -> None:
        self.write_pool_roster({"worker": {"thread_id": "old-follow-thread", "status": "done",
                                       "role": "impl", "port": self.server.port}})
        with codex_mesh._pending_file(str(self.session_dir), "worker") as handle:  # pyright: ignore[reportPrivateUsage]
            codex_mesh._write_pending(handle, "old-follow-thread", ["Run finished: verify.sh check"])  # pyright: ignore[reportPrivateUsage]
        self.failing_method = "thread/resume"

        self.assertEqual(self.run_follow("next launch prompt"), (0, ""))

        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)
        starts = self.methods("turn/start")
        self.assertEqual([_as_test_dict(request["params"]).get("threadId") for request in starts], [THREAD_ID])
        inputs = cast(list[dict[str, object]], _as_test_dict(starts[0]["params"])["input"])
        self.assertEqual([item["text"] for item in inputs], ["next launch prompt", "Run finished: verify.sh check"])
        pending = cast(codex_mesh.WorkerPendingMessages,
                       json.loads((self.session_dir / "worker.pending.json").read_text(encoding="utf-8")))
        self.assertEqual((pending["thread_id"], pending["messages"]), (THREAD_ID, []))

    def test_start_records_the_assigned_role(self) -> None:
        code, errors = self.run_start()

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.worker_record()["role"], "impl")

    def test_thread_titles_follow_assignment_at_each_turn_without_renaming_the_worker(self) -> None:
        roster = message_roster
        place = {unit_runs.director(self.session_dir): roster._ProductionPlace(  # pyright: ignore[reportPrivateUsage]
            roster.ProductionWithoutRunningShowrunner("sdlc"), roster.DirectsUnit("roles-unit"))}
        with patch.object(roster, "_production_places", return_value=place):
            self.assertEqual(self.run_start(), (0, ""))
            self.assertEqual(self.methods("thread/name/set")[-1]["params"], {
                "threadId": THREAD_ID, "name": "sdlc · roles · worker worker"})
            self.server.requests.clear()
            self.assertEqual(self.run_follow(role="review"), (0, ""))
        self.assertEqual(self.methods("thread/name/set")[-1]["params"], {
            "threadId": THREAD_ID, "name": "sdlc · roles · code reviewer worker"})
        self.assertEqual(self.worker_record()["role"], "review")
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)
        self.assertEqual(set(self.roster()), {"worker"})
        self.assertEqual([_as_test_dict(request["params"]).get("threadId")
                          for request in self.methods("turn/start")], [THREAD_ID])

    def test_display_rename_reaches_the_next_thread_title_without_changing_record_identity(self) -> None:
        roster = message_roster
        place = {unit_runs.director(self.session_dir): roster._ProductionPlace(  # pyright: ignore[reportPrivateUsage]
            roster.ProductionWithoutRunningShowrunner("sdlc"), roster.DirectsUnit("roles-unit"))}
        with patch.object(roster, "_production_places", return_value=place):
            self.assertEqual(self.run_start(), (0, ""))
            before = dict(self.worker_record())
            with patch.object(names, "runtime_names", return_value=fixture_names(worker="builder")):
                self.assertEqual(self.run_follow(), (0, ""))
        self.assertEqual(self.methods("thread/name/set")[-1]["params"], {
            "threadId": THREAD_ID, "name": "sdlc · roles · builder worker"})
        after = self.worker_record()
        self.assertEqual((after["role"], after["thread_id"]), (before["role"], before["thread_id"]))
        self.assertEqual(set(self.roster()), {"worker"})

    def test_unplaced_thread_keeps_its_address_as_title(self) -> None:
        roster = message_roster
        with patch.object(roster, "_production_places", return_value={}):
            self.assertEqual(self.run_start(), (0, ""))
        self.assertEqual(self.methods("thread/name/set")[-1]["params"], {"threadId": THREAD_ID, "name": "worker"})
        self.assertEqual(self.worker_record()["role"], "impl")
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)

    def test_prime_and_fork_send_shown_titles_and_keep_mesh_addresses(self) -> None:
        roster = message_roster
        place = {unit_runs.director(self.session_dir): roster._ProductionPlace(  # pyright: ignore[reportPrivateUsage]
            roster.ProductionWithoutRunningShowrunner("sdlc"), roster.DirectsUnit("roles-unit"))}
        with patch.object(roster, "_production_places", return_value=place):
            result, output, errors = self.run_mesh(self.prime_cli())
            self.assertEqual((result, output, errors), (0, f"{THREAD_ID}\n", ""))
            self.assertEqual(self.methods("thread/name/set")[-1]["params"], {
                "threadId": THREAD_ID, "name": "sdlc · roles · code reviewer template"})
            result, output, errors = self.run_mesh(["fork", "--from", "template", "--name", "reviewer"])
        self.assertEqual((result, output, errors), (0, f"{self.fork_thread_id}\n", ""))
        self.assertEqual(self.methods("thread/name/set")[-1]["params"], {
            "threadId": self.fork_thread_id, "name": "sdlc · roles · code reviewer reviewer"})
        self.assert_review_record("template", THREAD_ID)
        self.assert_review_record("reviewer", self.fork_thread_id)
        self.assertEqual(set(self.roster()), {"template", "reviewer"})

    def test_prime_held_titles_use_the_design_holder_before_the_mesh_record_exists(self) -> None:
        for index, (holder, prefix) in enumerate((
                (design_roles.UnitHolder("sdlc", "roles-unit"), "sdlc · roles"),
                (design_roles.ShowrunnerHolder("sdlc"), "sdlc"))):
            for role, job in (("designer", "designer"), ("reviewer", "design reviewer")):
                with self.subTest(holder=holder, role=role):
                    design_id = f"00000000-0000-4000-8000-00000000009{index}"
                    directory = design_roles.design_directory(design_id)
                    claim = directory / f"{role}.creation-claimed"
                    claim.unlink(missing_ok=True)
                    design = design_roles.HeldDesign(design_roles.DesignPlacement(design_id, "fixture-host", 1),
                        "fixture-repo", str(self.session_dir), str(self.session_dir / "design.md"), "design",
                        "2026-10-21T12:00:00+00:00", holder, design_roles.DesignUnbound(),
                        (design_roles.RoleInstance("designer", "template" if role == "designer" else "other-designer",
                                            design_roles.RoleNotOpened("claim")),
                         design_roles.RoleInstance("reviewer", "template" if role == "reviewer" else "other-reviewer",
                                            design_roles.RoleNotOpened("claim"))))
                    design_roles._save(design)  # pyright: ignore[reportPrivateUsage]
                    self.write_pool_roster({})
                    self.server.requests.clear()
                    with patch.object(design_roles, "resolve_holder", return_value=design_roles.HolderReach("fixture-holder")), \
                            patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "fixture-holder"}):
                        result, output, errors = self.run_mesh(self.prime_cli(
                            "--held-design", design_id, "--held-role", role))
                    self.assertEqual((result, output, errors), (0, f"{THREAD_ID}\n", ""))
                    self.assertEqual(self.methods("thread/name/set")[-1]["params"], {
                        "threadId": THREAD_ID, "name": f"{prefix} · {job}"})
                    record = _as_test_dict(self.roster()["template"])
                    self.assertEqual((record["held_design_id"], record["held_role"], record["thread_id"]),
                                     (design_id, role, THREAD_ID))

    def test_resume_fallback_sets_shown_title_and_retains_assignment(self) -> None:
        roster = message_roster
        self.write_pool_roster({"worker": {"thread_id": "old-thread", "status": "done", "role": "impl"}})
        self.failing_method = "thread/resume"
        place = {unit_runs.director(self.session_dir): roster._ProductionPlace(  # pyright: ignore[reportPrivateUsage]
            roster.ProductionWithoutRunningShowrunner("sdlc"), roster.DirectsUnit("roles-unit"))}
        with patch.object(roster, "_production_places", return_value=place):
            self.assertEqual(self.run_follow(), (0, ""))
        titles = self.methods("thread/name/set")
        self.assertTrue(titles)
        for request in titles:
            self.assertEqual(request["params"], {"threadId": THREAD_ID, "name": "sdlc · roles · worker worker"})
        self.assertEqual(self.worker_record()["role"], "impl")
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)
        self.assertEqual(set(self.roster()), {"worker"})

    def test_start_appends_usage_without_a_lens(self) -> None:
        code, errors = self.run_start()

        self.assertEqual((code, errors), (0, ""))
        records = self.usage_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(set(records[0]), {"id", "role", "at"})
        self.assertEqual((records[0]["id"], records[0]["role"]), (THREAD_ID, "impl"))
        self.assertNotIn("lens", self.worker_record())

    def test_start_cli_records_role_and_lens_only_in_the_usage_log(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            code = codex_mesh.main([
                "start", "--session-dir", str(self.session_dir), "--name", "worker",
                "--role", "review", "--lens", "contract", "--cwd", str(self.session_dir),
                "--prompt-file", str(self.session_dir / "prompt.txt"),
                "--summary-file", str(self.session_dir / "summary.txt"),
                "--log-file", str(self.session_dir / "worker.log"), "--timeout", "5",
            ])

        self.assertEqual((code, errors.getvalue()), (0, ""))
        records = self.usage_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(set(records[0]), {"id", "role", "lens", "at"})
        self.assertEqual((records[0]["id"], records[0]["role"], records[0]["lens"]),
                         (THREAD_ID, "review", "contract"))
        self.assertEqual(self.worker_record()["role"], "review")
        self.assertNotIn("lens", self.worker_record())

    def test_follow_records_each_role_and_lens_assignment_on_the_same_thread(self) -> None:
        self.assertEqual(self.run_start(), (0, ""))

        self.assertEqual(self.run_follow(role="review", lens="adversary"), (0, ""))
        self.assertEqual(self.run_follow(role="review", lens="adversary"), (0, ""))
        self.assertEqual(self.run_follow(role="fix"), (0, ""))

        records = self.usage_records()
        self.assertEqual([(record["id"], record["role"], record.get("lens"))
                          for record in records], [
            (THREAD_ID, "impl", None),
            (THREAD_ID, "review", "adversary"),
            (THREAD_ID, "review", "adversary"),
            (THREAD_ID, "fix", None),
        ])
        self.assertTrue(all("approximate" not in record for record in records))
        self.assertEqual(self.worker_record()["role"], "fix")
        self.assertNotIn("lens", self.worker_record())

    def test_follow_cli_accepts_a_review_lens(self) -> None:
        self.assertEqual(self.run_start(), (0, ""))
        message_file = self.session_dir / "follow-up.txt"
        _ = message_file.write_text("Review the contract", encoding="utf-8")

        with contextlib.redirect_stderr(io.StringIO()) as errors:
            code = codex_mesh.main([
                "follow", "--session-dir", str(self.session_dir), "--to", "worker",
                "--role", "review", "--lens", "ux", "--message-file", str(message_file),
                "--summary-file", str(self.session_dir / "summary.txt"),
                "--log-file", str(self.session_dir / "worker.log"), "--timeout", "5",
            ])

        self.assertEqual((code, errors.getvalue()), (0, ""))
        self.assertEqual(len(self.usage_records()), 2)
        self.assertEqual(self.usage_records()[1]["lens"], "ux")
        self.assertNotIn("lens", self.worker_record())

    def test_reserved_follow_records_one_assignment_with_its_lens(self) -> None:
        self.assertEqual(self.run_start(), (0, ""))
        args = self.start_args()
        args.to = "worker"
        args.role = "review"
        args.lens = "craft"
        args.claim_pid = os.getpid()

        with contextlib.redirect_stderr(io.StringIO()) as claim_errors:
            claim_code = codex_mesh.main([
                "can-follow", "--session-dir", str(self.session_dir), "--to", "worker",
                "--role", "review", "--lens", "craft", "--claim-pid", str(os.getpid()),
            ])
        self.assertEqual((claim_code, claim_errors.getvalue()), (0, ""))
        self.assertEqual(len(self.usage_records()), 2)
        self.assertEqual(self.usage_records()[1]["lens"], "craft")
        self.assertNotIn("lens", self.worker_record())
        args.message_file = str(self.session_dir / "follow-up.txt")
        _ = Path(args.message_file).write_text("Review the code", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            code = codex_mesh.command_follow(args)

        self.assertEqual((code, errors.getvalue()), (0, ""))
        self.assertEqual(len(self.usage_records()), 2)
        self.assertNotIn("lens", self.worker_record())

    def test_follow_probe_without_a_claim_does_not_record_an_assignment(self) -> None:
        self.assertEqual(self.run_start(), (0, ""))
        args = self.start_args()
        args.to = "worker"
        args.role = "review"
        args.lens = "adversary"

        self.assertEqual(codex_mesh.command_can_follow(args), 0)

        self.assertEqual(len(self.usage_records()), 1)
        self.assertEqual(self.worker_record()["role"], "impl")

    def test_usage_write_failure_does_not_stop_a_new_thread(self) -> None:
        self.usage_path().mkdir(parents=True)

        code, errors = self.run_start()

        self.assertEqual(code, 0, errors)
        self.assertEqual(len(errors.splitlines()), 1)
        self.assertTrue(errors.startswith("agents_log: "), errors)
        self.assertEqual(self.worker_record()["status"], "done")
        self.assertEqual(len(self.methods("turn/start")), 1)

    def test_start_reports_unexpected_usage_write_failure_and_continues(self) -> None:
        with patch.object(agents_log, "append",
                          side_effect=RuntimeError("usage write failed\ntry later")):
            code, errors = self.run_start()

        self.assertEqual(code, 0, errors)
        self.assertEqual(errors, "agents_log: usage write failed try later\n")
        self.assertEqual(self.worker_record()["status"], "done")
        self.assertEqual(len(self.methods("turn/start")), 1)

    def test_usage_write_failure_does_not_stop_a_follow_up(self) -> None:
        self.assertEqual(self.run_start(), (0, ""))
        self.usage_path().unlink()
        self.usage_path().mkdir()

        code, errors = self.run_follow(role="review", lens="adversary")

        self.assertEqual(code, 0, errors)
        self.assertEqual(len(errors.splitlines()), 1)
        self.assertTrue(errors.startswith("agents_log: "), errors)
        self.assertEqual(self.worker_record()["status"], "done")
        self.assertEqual(len(self.methods("turn/start")), 2)

    def test_a_preclaimed_follow_reports_handoff_if_ownership_moves_before_the_child_check(self) -> None:
        self.assertEqual(self.run_start(), (0, ""))
        args = self.start_args()
        args.to = "worker"
        args.role = "review"
        args.claim_pid = os.getpid()
        self.assertEqual(codex_mesh.command_can_follow(args), 0)
        record = self.worker_record()
        destination = str(self.session_dir.parent / "taking-run")
        record["adopted_to"] = destination
        codex_mesh.advance_worker_roster_revision(record)
        self.write_pool_roster({"worker": record})
        args.message_file = str(self.session_dir / "follow-up.txt")
        _ = Path(args.message_file).write_text("Do not send work after losing ownership")
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            code = codex_mesh.command_follow(args)
        self.assertEqual(code, 2, errors.getvalue())
        self.assertIn(f"worker handed off to {destination}", errors.getvalue())
        self.assertEqual(len(self.methods("turn/start")), 1)
        self.assertEqual(self.worker_record()["adopted_to"], destination)

    def test_preclaimed_follow_survives_a_deeply_nested_usage_line(self) -> None:
        self.assertEqual(self.run_start(), (0, ""))
        args = self.start_args()
        args.to = "worker"
        args.role = "review"
        args.lens = "adversary"
        args.claim_pid = os.getpid()
        self.assertEqual(codex_mesh.command_can_follow(args), 0)
        with self.usage_path().open("a", encoding="utf-8") as ledger:
            _ = ledger.write("[" * 10_000 + "]" * 10_000 + "\n")
        args.message_file = str(self.session_dir / "follow-up.txt")
        _ = Path(args.message_file).write_text("Review the code", encoding="utf-8")

        with contextlib.redirect_stderr(io.StringIO()) as errors:
            code = codex_mesh.command_follow(args)

        self.assertEqual(code, 0, errors.getvalue())
        self.assertEqual(self.worker_record()["status"], "done")
        self.assertEqual(len(self.methods("turn/start")), 2)

    def test_preclaimed_follow_reports_unexpected_usage_reader_failure_and_continues(self) -> None:
        self.assertEqual(self.run_start(), (0, ""))
        args = self.start_args()
        args.to = "worker"
        args.role = "review"
        args.claim_pid = os.getpid()
        self.assertEqual(codex_mesh.command_can_follow(args), 0)
        args.message_file = str(self.session_dir / "follow-up.txt")
        _ = Path(args.message_file).write_text("Review the code", encoding="utf-8")

        with patch.object(agents_log, "assignments",
                          side_effect=RecursionError("ledger is too deeply nested\nread failed")), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            code = codex_mesh.command_follow(args)

        self.assertEqual(code, 0, errors.getvalue())
        self.assertEqual(errors.getvalue(), "agents_log: ledger is too deeply nested read failed\n")
        self.assertEqual(self.worker_record()["status"], "done")
        self.assertEqual(len(self.methods("turn/start")), 2)

    def test_resident_start_without_role_parses_and_omits_role_from_roster(self) -> None:
        args = self.start_args()
        args.role = ""
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            code = codex_mesh.command_start(args)
        self.assertEqual((code, errors.getvalue()), (0, ""))
        self.assertNotIn("role", self.worker_record())
        self.assertEqual(self.usage_records(), [])

        parsed: list[argparse.Namespace] = []

        def capture(parsed_args: argparse.Namespace) -> int:
            parsed.append(parsed_args)
            return 0

        with patch.object(codex_mesh, "command_start", capture):
            parsed_code = codex_mesh.main([
                "start", "--resident",
                "--session-dir", str(self.session_dir),
                "--name", "friend",
                "--cwd", str(self.session_dir),
                "--prompt-file", str(self.session_dir / "prompt.txt"),
                "--summary-file", str(self.session_dir / "friend.txt"),
                "--log-file", str(self.session_dir / "friend.log"),
            ])
        self.assertEqual(parsed_code, 0)
        self.assertEqual(len(parsed), 1)
        parsed_values = cast("dict[str, object]", cast("object", vars(parsed[0])))
        self.assertIs(parsed_values.get("resident"), True)
        self.assertEqual(parsed_values.get("role"), "")

    def test_follow_records_a_reassigned_role_without_losing_context(self) -> None:
        self.context_sizes = [123456]
        self.assertEqual(self.run_start()[0], 0)

        code, errors = self.run_follow(role="review")

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.worker_record()["role"], "review")
        self.assertEqual(self.worker_record()["context_tokens"], 123456)

    def test_turn_final_usage_records_the_context_size_in_the_roster(self) -> None:
        self.context_sizes = [123456]

        code, errors = self.run_start()

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.worker_record()["context_tokens"], 123456)

    def test_compact_refuses_a_running_worker(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        roster = self.roster()
        worker = cast("dict[str, object]", roster["worker"])
        worker["status"] = "running"
        worker["turn_id"] = "live-turn"
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(
            json.dumps(roster), encoding="utf-8"
        )
        args = argparse.Namespace(session_dir=str(self.session_dir), to="worker")

        with contextlib.redirect_stderr(io.StringIO()) as errors:
            code = codex_mesh.command_compact(args)

        self.assertEqual(code, 2)
        self.assertIn("compact requires a finished worker", errors.getvalue())
        self.assertEqual(self.methods("thread/compact/start"), [])

    def compact_with_zero_usage(self) -> dict[str, object]:
        self.context_sizes = [150000]
        self.compact_after_tokens = 0
        self.assertEqual(self.run_start()[0], 0)
        code, output, errors = self.run_mesh(["compact", "--to", "worker"])
        self.assertEqual((code, output, errors), (
            0, "compacted worker: 150000 tokens, size not measured until its next turn\n", "",
        ))
        record = self.worker_record()
        self.assertEqual(record.get("context_tokens"), "not_measured_since_compaction")
        return record

    def test_zero_compaction_reports_an_unmeasured_size_and_list_keeps_three_fields(self) -> None:
        _ = self.compact_with_zero_usage()
        code, output, errors = self.run_mesh(["list"])
        self.assertEqual((code, output, errors), (0, f"worker\tdone\t{THREAD_ID}\n", ""))

    def test_next_turn_usage_replaces_the_unmeasured_compaction_size(self) -> None:
        _ = self.compact_with_zero_usage()
        self.context_sizes = [23456]
        self.assertEqual(self.run_follow(), (0, ""))
        self.assertEqual(self.worker_record()["context_tokens"], 23456)

    def compact_after_a_later_turn(self, after_tokens: int, *, finished: bool = False) -> None:
        self.context_sizes = [150000]
        self.compact_after_tokens = after_tokens
        self.assertEqual(self.run_start(), (0, ""))
        original = self.worker_record()
        compact = cast(Callable[[codex_mesh.Client, str], codex_mesh.KnownContextSize],
                       getattr(codex_mesh, "_compact_thread"))
        update = cast(Callable[[str, str, codex_mesh.ThreadRecord, codex_mesh.ExpectedRoster], bool],
                      getattr(codex_mesh, "_update_roster_if_expected"))
        newer: list[dict[str, object]] = []

        def finish_then_record_turn(client: codex_mesh.Client, thread_id: str) -> codex_mesh.KnownContextSize:
            after = compact(client, thread_id)
            if finished:
                self.context_sizes = [150000]
                self.assertEqual(self.run_follow(), (0, ""))
                self.assertEqual(self.worker_record()["status"], original["status"])
                self.assertEqual(self.worker_record()["context_tokens"], original["context_tokens"])
                self.assertNotIn("turn_id", self.worker_record())
            else:
                self.assertTrue(update(str(self.session_dir), "worker", {
                    "thread_id": thread_id, "status": "running", "turn_id": "later-turn",
                    "context_tokens": 23456,
                }, codex_mesh.RosterOnThread(thread_id)))
            newer.append(self.worker_record())
            return after

        with patch.object(codex_mesh, "_compact_thread", finish_then_record_turn):
            code, output, errors = self.run_mesh(["compact", "--to", "worker"])
        line = (f"compacted worker: 150000 -> {after_tokens} tokens\n" if after_tokens > 0
                else "compacted worker: 150000 tokens, size not measured until its next turn\n")
        self.assertEqual((code, output, errors), (0, line, ""))
        self.assertEqual(len(newer), 1)
        self.assertEqual(self.worker_record(), newer[0])

    def test_late_zero_compaction_keeps_the_newer_turns_complete_record(self) -> None:
        self.compact_after_a_later_turn(0)

    def test_late_measured_compaction_keeps_the_newer_turns_complete_record(self) -> None:
        self.compact_after_a_later_turn(18000)

    def test_late_compaction_keeps_a_finished_turn_with_the_same_status_and_size(self) -> None:
        self.compact_after_a_later_turn(0, finished=True)

    def test_compact_refuses_an_unmeasured_size_with_its_specific_reason(self) -> None:
        record = {"thread_id": THREAD_ID, "status": "done",
                  "context_tokens": "not_measured_since_compaction"}
        self.write_pool_roster({"worker": record})
        self.assertEqual(self.run_mesh(["compact", "--to", "worker"]), (
            2, "", "codex_mesh: worker: size not measured since its last compaction; compact refused\n",
        ))
        self.assertEqual(self.worker_record(), record)
        self.assertEqual(self.server.requests, [])

    def test_compact_without_a_recorded_size_keeps_the_existing_refusal(self) -> None:
        record = {"thread_id": THREAD_ID, "status": "done"}
        self.write_pool_roster({"worker": record})
        self.assertEqual(self.run_mesh(["compact", "--to", "worker"]), (
            2, "", "codex_mesh: worker: no recorded context size; compact refused\n",
        ))
        self.assertEqual(self.worker_record(), record)
        self.assertEqual(self.server.requests, [])

    def test_each_roster_write_advances_revision_even_when_ended_fields_are_kept(self) -> None:
        update = cast(Callable[[str, str, codex_mesh.ThreadRecord], None],
                      getattr(codex_mesh, "_update_roster"))
        guarded = cast(Callable[[str, str, codex_mesh.ThreadRecord, codex_mesh.ExpectedRoster], bool],
                       getattr(codex_mesh, "_update_roster_if_expected"))
        original = {"thread_id": THREAD_ID, "status": "done", "context_tokens": 150000}
        self.write_pool_roster({"worker": original})
        self.assertNotIn("revision", self.worker_record())
        update(str(self.session_dir), "worker", {"thread_id": THREAD_ID, "status": "done"})
        self.assertEqual(self.worker_record(), {**original, "revision": 1})
        self.assertTrue(guarded(str(self.session_dir), "worker", {
            "thread_id": THREAD_ID, "status": "failed",
        }, codex_mesh.RosterOnThread(THREAD_ID)))
        self.assertEqual(self.worker_record(), {**original, "status": "failed", "revision": 2})
        update(str(self.session_dir), "worker", {"thread_id": THREAD_ID, "status": "ended"})
        ended = self.worker_record()
        self.assertEqual(ended, {**original, "status": "ended", "revision": 3})
        self.assertTrue(guarded(str(self.session_dir), "worker", {
            "thread_id": THREAD_ID, "status": "done",
        }, codex_mesh.RosterOnThread(THREAD_ID)))
        self.assertEqual(self.worker_record(), {**ended, "revision": 4})
        before_refusal = self.worker_record()
        self.assertFalse(guarded(str(self.session_dir), "worker", {
            "thread_id": "different-thread", "status": "done",
        }, codex_mesh.RosterOnThread("different-thread")))
        self.assertEqual(self.worker_record(), before_refusal)
        update(str(self.session_dir), "worker", {"thread_id": "different-thread", "status": "done"})
        self.assertEqual(self.worker_record(), {
            "thread_id": "different-thread", "status": "done", "context_tokens": 150000, "revision": 5,
        })

    def test_compaction_reads_a_legacy_entry_as_revision_zero(self) -> None:
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "done",
                                        "context_tokens": 150000}})
        self.compact_after_tokens = 18000
        self.assertEqual(self.run_mesh(["compact", "--to", "worker"]), (
            0, "compacted worker: 150000 -> 18000 tokens\n", "",
        ))
        self.assertEqual(self.worker_record(), {
            "thread_id": THREAD_ID, "status": "done", "context_tokens": 18000, "revision": 1,
        })

    def test_follow_up_without_usage_keeps_the_unmeasured_compaction_size(self) -> None:
        original = self.compact_with_zero_usage()
        self.assertEqual(self.run_follow(), (0, ""))
        self.assertEqual(self.worker_record().get("context_tokens"), original.get("context_tokens"))

    def test_roster_writes_without_size_preserve_the_unmeasured_compaction_state(self) -> None:
        original = self.compact_with_zero_usage()
        codex_mesh._update_roster(str(self.session_dir), "worker", {  # pyright: ignore[reportPrivateUsage]
            "thread_id": THREAD_ID, "status": "failed",
        })
        self.assertEqual(self.worker_record(), {**original, "status": "failed", "revision": cast(int, original["revision"]) + 1})
        self.assertTrue(codex_mesh._update_roster_if_expected(str(self.session_dir), "worker", {  # pyright: ignore[reportPrivateUsage]
            "thread_id": THREAD_ID, "status": "done",
        }, codex_mesh.RosterOnThread(THREAD_ID)))
        self.assertEqual(self.worker_record(), {**original, "revision": cast(int, original["revision"]) + 2})
        codex_mesh._update_roster(str(self.session_dir), "worker", {  # pyright: ignore[reportPrivateUsage]
            "thread_id": THREAD_ID, "status": "ended",
        })
        ended = self.worker_record()
        self.assertTrue(codex_mesh._update_roster_if_expected(str(self.session_dir), "worker", {  # pyright: ignore[reportPrivateUsage]
            "thread_id": THREAD_ID, "status": "done",
        }, codex_mesh.RosterOnThread(THREAD_ID)))
        self.assertEqual(self.worker_record(), {**ended, "revision": cast(int, ended["revision"]) + 1})
        self.assertEqual(ended.get("context_tokens"), original.get("context_tokens"))

    def test_reset_replaces_unmeasured_compaction_state_with_measured_zero(self) -> None:
        _ = self.compact_with_zero_usage()
        code, _output, errors = self.run_mesh(["reset", "--to", "worker"])
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.worker_record()["context_tokens"], 0)

    def test_failed_compaction_keeps_the_complete_recorded_size(self) -> None:
        self.context_sizes = [150000]
        self.assertEqual(self.run_start()[0], 0)
        original = self.worker_record()
        self.failing_method = "thread/compact/start"
        code, output, errors = self.run_mesh(["compact", "--to", "worker"])
        self.assertEqual((code, output), (1, ""))
        self.assertIn("scripted request failure", errors)
        self.assertEqual(self.worker_record(), original)

    def test_roster_writes_without_size_keep_a_measured_size(self) -> None:
        self.context_sizes = [150000]
        self.assertEqual(self.run_start()[0], 0)
        original = self.worker_record()
        codex_mesh._update_roster(str(self.session_dir), "worker", {  # pyright: ignore[reportPrivateUsage]
            "thread_id": THREAD_ID, "status": "failed",
        })
        self.assertEqual(self.worker_record(), {**original, "status": "failed", "revision": cast(int, original["revision"]) + 1})
        self.assertTrue(codex_mesh._update_roster_if_expected(str(self.session_dir), "worker", {  # pyright: ignore[reportPrivateUsage]
            "thread_id": THREAD_ID, "status": "done",
        }, codex_mesh.RosterOnThread(THREAD_ID)))
        self.assertEqual(self.worker_record(), {**original, "revision": cast(int, original["revision"]) + 2})

    def test_compact_waits_for_compaction_and_records_the_smaller_context(self) -> None:
        self.context_sizes = [150000]
        self.compact_after_tokens = 18000
        self.assertEqual(self.run_start()[0], 0)
        args = argparse.Namespace(session_dir=str(self.session_dir), to="worker")

        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = codex_mesh.command_compact(args)

        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue(), "compacted worker: 150000 -> 18000 tokens\n")
        self.assertEqual(self.worker_record()["context_tokens"], 18000)
        self.assertEqual(len(self.methods("thread/compact/start")), 1)

    def test_compact_accepts_the_current_context_compaction_item(self) -> None:
        self.context_sizes = [150000]
        self.compact_after_tokens = 17000
        self.compact_uses_item = True
        self.assertEqual(self.run_start()[0], 0)
        args = argparse.Namespace(session_dir=str(self.session_dir), to="worker")

        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = codex_mesh.command_compact(args)

        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue(), "compacted worker: 150000 -> 17000 tokens\n")
        self.assertEqual(self.worker_record()["context_tokens"], 17000)

    def test_compact_waits_for_turn_completion_after_the_compaction_event(self) -> None:
        self.context_sizes = [150000]
        self.compact_uses_item = True
        self.assertEqual(self.run_start()[0], 0)
        self.server.block_compact_turn_completion = True
        args = argparse.Namespace(session_dir=str(self.session_dir), to="worker")
        codes: list[int] = []
        failures: list[BaseException] = []
        output = io.StringIO()

        def compact() -> None:
            try:
                with contextlib.redirect_stdout(output):
                    codes.append(codex_mesh.command_compact(args))
            except BaseException as exc:
                failures.append(exc)

        worker = threading.Thread(target=compact)
        worker.start()
        self.assertTrue(self.server.compaction_event_sent.wait(timeout=2))
        self.assertTrue(worker.is_alive(), "compact returned before turn/completed")
        self.server.release_compact_turn_completion.set()
        worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(codes, [0])
        self.assertIn("compacted worker:", output.getvalue())

    def compact_with_short_waits(self) -> tuple[int, str]:
        args = argparse.Namespace(session_dir=str(self.session_dir), to="worker")
        with (
            patch.object(codex_mesh, "CALL_TIMEOUT_SECS", 1),
            patch.object(codex_mesh, "COMPACT_IDLE_WAIT_SECS", 0.3),
            patch.object(codex_mesh, "COMPACT_IDLE_POLL_SECS", 0.05),
            contextlib.redirect_stderr(io.StringIO()) as errors,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = codex_mesh.command_compact(args)
        return code, errors.getvalue()

    def test_a_compaction_that_times_out_is_interrupted_so_the_worker_can_follow(self) -> None:
        self.context_sizes = [150000]
        self.assertEqual(self.run_start()[0], 0)
        self.compact_never_finishes = True

        code, errors = self.compact_with_short_waits()

        self.assertEqual(code, 1)
        self.assertIn("no thread/compacted within 1s", errors)
        self.assertIn("interrupted the compaction turn, the worker is idle and not compacted", errors)
        self.assertEqual(len(self.methods("turn/interrupt")), 1)
        self.assertEqual(self.live_turn, "")
        self.assertEqual(self.worker_record()["context_tokens"], 150000)

    def test_a_compaction_that_cannot_be_interrupted_exits_with_the_still_live_status(self) -> None:
        self.context_sizes = [150000]
        self.assertEqual(self.run_start()[0], 0)
        self.compact_never_finishes = True
        self.interrupt_error = "interrupt refused"

        code, errors = self.compact_with_short_waits()

        self.assertEqual(code, codex_mesh.COMPACT_TURN_STILL_LIVE)
        self.assertIn("a turn is still live on the thread", errors)
        self.assertEqual(self.live_turn, "compact-turn")

    def test_compact_waits_for_a_live_turn_on_a_finished_worker_to_end(self) -> None:
        self.context_sizes = [150000]
        self.compact_after_tokens = 18000
        self.assertEqual(self.run_start()[0], 0)
        self.live_turn = "left-over-turn"
        self.announce_live_completion = True

        code, errors = self.compact_with_short_waits()

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.worker_record()["context_tokens"], 18000)

    def test_compact_refuses_a_finished_worker_whose_turn_stays_live(self) -> None:
        self.context_sizes = [150000]
        self.assertEqual(self.run_start()[0], 0)
        self.live_turn = "left-over-turn"

        code, errors = self.compact_with_short_waits()

        self.assertEqual(code, codex_mesh.COMPACT_TURN_STILL_LIVE)
        self.assertIn("thread has a live turn; compact refused", errors)
        self.assertEqual(self.methods("thread/compact/start"), [])
        self.assertEqual(self.methods("turn/interrupt"), [])

    def test_context_overflow_starts_one_continue_turn_and_posts_once(self) -> None:
        self.outcomes = ["overflow", "completed"]

        code, errors = self.run_start()

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(
            self.turn_inputs,
            ["Original worker task", "Continue where you stopped."],
        )
        board = (self.session_dir / "board.log").read_text(encoding="utf-8")
        self.assertEqual(board.count("context window filled; sent"), 1)

    def quota_completion(self, request: dict[str, object]) -> list[dict[str, object]]:
        params = cast(dict[str, object], request["params"])
        self.started_turns["quota-turn"] = "failed"
        return [_reply(request, {"turn": {"id": "quota-turn"}}),
                _notice("turn/completed", {"threadId": params["threadId"],
                    "turn": {"id": "quota-turn", "error": {"message": USAGE_LIMIT}}})]

    def assert_reset_failure_names_serving_account(self, method: str) -> None:
        account = self.prepare_refusing_account()
        self.write_pool_roster({"worker": {
            "thread_id": THREAD_ID, "status": "done", "role": "review", "context_tokens": 9876,
        }})
        self.failing_method = method
        with patch.object(codex_mesh, "_report_quota_refusal") as report:
            code, _output, errors = self.run_mesh(["reset", "--to", "worker"])
        self.assertEqual(code, 1, errors)
        self.assertIn(account.label, errors)
        self.assertIn(agent_accounts.codex_home_display(account.home), errors)
        self.assertIn("scripted request failure", errors)
        self.assertEqual(self.worker_record()["context_tokens"], 9876)
        self.assertNotIn("launcher_pid", self.worker_record())
        report.assert_not_called()

    def test_reset_thread_read_failure_names_the_serving_account_and_home(self) -> None:
        self.assert_reset_failure_names_serving_account("thread/read")

    def test_reset_turn_list_failure_names_the_serving_account_and_home(self) -> None:
        self.assert_reset_failure_names_serving_account("thread/turns/list")

    def test_client_without_a_binding_uses_a_named_server_state(self) -> None:
        account = self.prepare_refusing_account()
        client = codex_mesh.Client(self.server.port, "worker")
        try:
            self.assertIsInstance(client.serving, codex_mesh.NotBoundCodexServer)
            self.assertEqual(client.serving.home, account.home)
        finally:
            client.close()

    def assert_routing_then_exited_handoff(self, report: MagicMock) -> None:
        self.assertEqual(report.call_count, 2)
        self.assertIsInstance(reported_stopped_context(report, 0),
                              codex_mesh.RefusalWithoutCommandExit)
        stopped = reported_stopped_context(report, 1)
        self.assertIsInstance(stopped, codex_mesh.CommandStoppedOnThread)
        assert isinstance(stopped, codex_mesh.CommandStoppedOnThread)
        self.assertEqual(stopped.session_dir, str(self.session_dir))

    def prepare_stream_refusal_wait(self) -> agent_accounts.KnownCodexAccount:
        source = self.prepare_refusing_account()
        self.start_args_timeout = 2.0
        original = self.respond
        self.server.respond = lambda request: (self.quota_completion(request)
            if request.get("method") == "turn/start" else original(request))
        return source

    def record_worker_phase(self) -> Path:
        history = self.session_dir.parent / "wait-history"
        _ = cast(object, self.patches.enter_context(patch.dict(os.environ, {
            "UNIT_RUN_DIR": str(self.session_dir), "UNIT_HISTORY_DIR": str(history),
            "UNIT_TEAM_ROLE": "impl", "UNIT_PHASE_ID": "6", "UNIT_PASS_OWNER": "launcher",
            "UNIT_SYNC_WRITES": "0", "CLAUDE_PROBE_SESSION": "",
        })))
        for arguments in (
            ["start-run", "--session-dir", str(self.session_dir), "--working-dir", str(self.session_dir),
             "--main-family", "codex", "--main-model", "fixture", "--main-session-id", "director"],
            ["start-phase", "--session-dir", str(self.session_dir), "--phase-id", "6", "--phase-title", "Old"],
            ["finish-phase", "--session-dir", str(self.session_dir), "--phase-id", "6", "--status", "completed"],
            ["start-phase", "--session-dir", str(self.session_dir), "--phase-id", "7", "--phase-title", "Current"],
            ["start-pass", "--session-dir", str(self.session_dir), "--phase-id", "7", "--pass-kind", "impl",
             "--activity", "worker", "--called-task", "direct.impl", "--called-family", "codex",
             "--called-model", "fixture", "--worker-name", "worker"],
        ):
            result = invoke_recorder(arguments, dict(os.environ), self.session_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
        return history / "runs" / f"{self.session_dir.name}.jsonl"

    def test_controller_wait_uses_explicit_context_with_absent_or_wrong_environment_thread(self) -> None:
        history = self.record_worker_phase()
        for arguments in (
            ["start-phase", "--session-dir", str(self.session_dir), "--phase-id", "8", "--phase-title", "Other"],
            ["start-pass", "--session-dir", str(self.session_dir), "--phase-id", "8", "--pass-kind", "impl",
             "--activity", "other", "--called-task", "direct.impl", "--called-family", "codex",
             "--called-model", "fixture", "--worker-name", "other-worker"],
        ):
            result = invoke_recorder(arguments, {**dict(os.environ), "UNIT_TEAM_ROLE": "other"}, self.session_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID}, "other-worker": {"thread_id": "other-thread"}})
        for environment_thread in (None, "other-thread"):
            with self.subTest(environment_thread=environment_thread), patch.dict(os.environ):
                if environment_thread is None:
                    _ = os.environ.pop("CODEX_THREAD_ID", None)
                else:
                    os.environ["CODEX_THREAD_ID"] = environment_thread
                # The controller's run environment also points elsewhere.
                os.environ["UNIT_RUN_DIR"] = str(self.session_dir / "wrong-run")
                codex_mesh._mesh_phase_wait(str(self.session_dir), "worker", THREAD_ID, "ReadyWithoutCapacity", "room")  # pyright: ignore[reportPrivateUsage]
                codex_mesh._mesh_phase_wait(str(self.session_dir), "worker", THREAD_ID, "ReadyWithoutCapacity", finished=True)  # pyright: ignore[reportPrivateUsage]
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        worker_pass, = [row for row in rows if row["event_type"] == "pass_started" and row["worker_name"] == "worker"]
        waits = [row for row in rows if row["event_type"] in ("wait_started", "wait_finished")]
        self.assertEqual(len(waits), 4)
        for wait in waits:
            self.assertEqual((wait["phase_id"], wait["pass_instance_id"], wait["worker_name"], wait["owner_session"]),
                             ("7", worker_pass["pass_instance_id"], "worker", THREAD_ID))

    def test_two_controller_workers_keep_distinct_passes_in_one_phase(self) -> None:
        history = self.record_worker_phase()
        result = invoke_recorder([
            "start-pass", "--session-dir", str(self.session_dir), "--phase-id", "7", "--pass-kind", "test",
            "--activity", "test", "--called-task", "direct.test", "--called-family", "codex",
            "--called-model", "fixture", "--worker-name", "worker-fix",
        ], {**dict(os.environ), "UNIT_TEAM_ROLE": "test"}, self.session_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID}, "worker-fix": {"thread_id": "test-thread"}})
        for name, thread in (("worker", THREAD_ID), ("worker-fix", "test-thread")):
            codex_mesh._mesh_phase_wait(str(self.session_dir), name, thread, "ReadyWithoutCapacity", "room")  # pyright: ignore[reportPrivateUsage]
        for name, thread in (("worker", THREAD_ID), ("worker-fix", "test-thread")):
            codex_mesh._mesh_phase_wait(str(self.session_dir), name, thread, "ReadyWithoutCapacity", finished=True)  # pyright: ignore[reportPrivateUsage]
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        passes = {row["worker_name"]: row for row in rows if row["event_type"] == "pass_started"}
        starts = [row for row in rows if row["event_type"] == "wait_started"]
        ends = {row["wait_id"]: row for row in rows if row["event_type"] == "wait_finished"}
        self.assertEqual(len(starts), 2)
        self.assertEqual(len({row["pass_instance_id"] for row in starts}), 2)
        for start in starts:
            worker_pass = passes[start["worker_name"]]
            for wait in (start, ends[start["wait_id"]]):
                self.assertEqual((wait["phase_instance_id"], wait["pass_instance_id"], wait["worker_name"]),
                                 (worker_pass["phase_instance_id"], worker_pass["pass_instance_id"], worker_pass["worker_name"]))

    def test_provider_refusal_wait_ends_when_the_worker_runs_again(self) -> None:
        _ = self.prepare_refusing_account()
        history = self.record_worker_phase()
        reporter = self.session_dir / "quota-report.py"
        _ = reporter.write_text("raise SystemExit(0)\n")
        respond = self.respond
        self.server.respond = lambda request: (self.quota_completion(request)
            if request.get("method") == "turn/start" else respond(request))
        with (patch.object(codex_mesh, "QUOTA_REPORT", reporter),
              patch.object(codex_mesh, "_quota_continuation_ready", return_value=False)):
            code, errors = self.run_start()
        self.assertEqual(code, 1, errors)
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        quota, = [row for row in rows if row["event_type"] == "wait_started"
                  and row["wait_kind"] == "WaitingForQuotaRefusal"]
        self.assertEqual((quota["phase_id"], quota["owner_session"]), ("7", THREAD_ID))
        self.assertFalse(any(row["event_type"] == "wait_finished" for row in rows))
        self.server.respond = respond
        with patch.object(codex_mesh, "_quota_continuation_ready", return_value=True):
            code, errors = self.run_follow()
        self.assertEqual(code, 0, errors)
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        ended, = [row for row in rows if row["event_type"] == "wait_finished" and row["wait_id"] == quota["wait_id"]]
        self.assertEqual(cast(dict[str, object], ended["wait_end"])["state"], "Finished")

    def test_account_room_wait_closes_on_dispatch_without_recording_a_quota_refusal(self) -> None:
        _ = self.prepare_refusing_account()
        history = self.record_worker_phase()
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "failed",
                                          "continuation_owed": True, "port": self.server.port}})
        with patch.object(codex_mesh, "_quota_continuation_ready", return_value=False):
            code, errors = self.run_follow()
        self.assertEqual(code, 1, errors)
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        wait, = [row for row in rows if row["event_type"] == "wait_started"]
        phase, = [row for row in rows if row["event_type"] == "phase_started" and row["phase_id"] == "7"]
        self.assertEqual((wait["wait_kind"], wait["phase_instance_id"]),
                         ("ReadyWithoutCapacity", phase["phase_instance_id"]))
        with patch.object(codex_mesh, "_quota_continuation_ready", return_value=True):
            code, errors = self.run_follow()
        self.assertEqual(code, 0, errors)
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        finished, = [row for row in rows if row["event_type"] == "wait_finished"]
        self.assertEqual(finished["wait_id"], wait["wait_id"])

    def test_probe_account_room_wait_writes_no_phase_wait(self) -> None:
        _ = self.prepare_refusing_account()
        history = self.record_worker_phase()
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "failed",
                                          "continuation_owed": True, "port": self.server.port}})
        with (patch.dict(os.environ, {"CLAUDE_PROBE_SESSION": "1"}),
              patch.object(codex_mesh, "_quota_continuation_ready", return_value=False)):
            code, errors = self.run_follow()
        self.assertEqual(code, 1, errors)
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        self.assertFalse(any(str(row["event_type"]).startswith("wait_") for row in rows))

    def save_unresolved_refusal(self, source: agent_accounts.KnownCodexAccount) -> None:
        now = datetime.now(timezone.utc)
        refusals.write_refusal(refusals.AccountRefusal("codex", source.login,
            refusals.KnownWindow("five_hour"), refusals.KnownReset(now + timedelta(minutes=10)), now))

    def skip_quota_wait_time(self) -> None:
        """Let one quota wait reach the deadline without a real sleep."""
        epoch = time.time()
        elapsed = 0.0

        @bounded_fixture
        def sleep(_seconds: float) -> None:
            nonlocal elapsed
            elapsed += self.start_args_timeout + 1

        _ = self.patches.enter_context(patch.object(codex_mesh, "time", SimpleNamespace(
            time=bounded_fixture(lambda: epoch + elapsed), monotonic=bounded_fixture(lambda: elapsed), sleep=sleep)))

    def assert_finished_quota_continuation_has_no_rerun(self, later_failure: str) -> None:
        source = self.prepare_refusing_account()
        self.start_args_timeout = 2.0
        original = self.respond
        commands: list[list[str]] = []
        submissions = 0

        def respond(request: dict[str, object]) -> list[dict[str, object]] | None:
            nonlocal submissions
            if request.get("method") == "turn/start":
                submissions += 1
                if submissions == 1:
                    return self.quota_completion(request)
                if later_failure == "disconnect":
                    self.server.close_after_turn_start = True
                frames = original(request)
                assert frames is not None
                frames.insert(-1, _notice("item/completed", {"threadId": THREAD_ID,
                    "turnId": "turn-1", "item": {"type": "agentMessage", "text": "recovered task finished"}}))
                if later_failure == "capacity":
                    frames += [
                        _notice("turn/started", {"threadId": THREAD_ID, "turn": {"id": "later-task"}}),
                        _notice("turn/completed", {"threadId": THREAD_ID,
                            "turn": {"id": "later-task", "error": {"message": CAPACITY}}}),
                    ]
                return frames
            return original(request)

        def report(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "bash":
                self.assertEqual(command[-2], str(Path(codex_mesh.__file__).with_name("agents_config.sh")))
                return subprocess.CompletedProcess(command, 0, "default", "")
            self.assertEqual(command[1], str(codex_mesh.QUOTA_REPORT))
            commands.append(command)
            # The quota handler has routed the refusal; its short wait has now expired.
            now = datetime.now(timezone.utc)
            refusals.write_refusal(refusals.AccountRefusal("codex", source.login,
                refusals.KnownWindow("five_hour"), refusals.KnownReset(now - timedelta(seconds=1)),
                now - timedelta(minutes=19)))
            return subprocess.CompletedProcess(command, 0, "", "")

        self.server.respond = respond
        args = self.start_args()
        args.resident = True
        with (patch.object(codex_mesh, "_marker_sessions", return_value=["own-director"]),
              patch.object(codex_mesh, "_launcher_sessions", return_value=()),
              patch.object(codex_mesh, "CAPACITY_BUDGET_SECS", 0),
              patch.object(subprocess, "run", side_effect=report),
              contextlib.redirect_stderr(io.StringIO()) as errors):
            code = codex_mesh.command_start(args)
        self.assertEqual(code, 1, errors.getvalue())
        self.assertEqual(submissions, 2)
        self.assertEqual(self.turn_inputs, ["Continue where you stopped."])
        self.assertIn("recovered task finished", (self.session_dir / "worker.log").read_text())
        self.assertEqual(len(commands), 1, commands)
        self.assertFalse(any("--stopped" in command for command in commands))
        self.assertIs(self.worker_record().get("continuation_owed"), False)
        if later_failure == "timeout":
            self.assertIn("no turn/completed within", errors.getvalue())
        elif later_failure == "capacity":
            self.assertEqual(self.worker_record()["status"], codex_mesh.CAPACITY_EXHAUSTED)
            self.assertIn("capacity", errors.getvalue())
        else:
            self.assertEqual(self.worker_record()["status"], "failed")
            self.assertTrue(errors.getvalue().strip())

    def test_finished_quota_continuation_then_disconnect_sends_no_rerun(self) -> None:
        self.assert_finished_quota_continuation_has_no_rerun("disconnect")

    def test_finished_quota_continuation_then_timeout_sends_no_rerun(self) -> None:
        self.assert_finished_quota_continuation_has_no_rerun("timeout")

    def test_finished_quota_continuation_then_capacity_exhaustion_sends_no_rerun(self) -> None:
        self.assert_finished_quota_continuation_has_no_rerun("capacity")

    def assert_retried_quota_continuation_timeout(self, *, continuation_completed: bool) -> None:
        source = self.prepare_refusing_account()
        self.start_args_timeout = 2.0
        original = self.respond
        commands: list[list[str]] = []
        recovery_reads: list[dict[str, object]] = []
        submissions = 0

        def respond(request: dict[str, object]) -> list[dict[str, object]] | None:
            nonlocal submissions
            if request.get("method") == "turn/start":
                submissions += 1
                if submissions == 1:
                    if continuation_completed:
                        return self.quota_completion(request)
                    return [{"id": request["id"], "error": {"message": USAGE_LIMIT}}]
                self.outcomes = ["disconnect"]  # Accept the turn without a completion notification.
                frames = original(request)
                assert frames is not None
                if continuation_completed:
                    frames.append(_notice("item/completed", {"threadId": THREAD_ID,
                        "turnId": "turn-1", "item": {
                            "type": "agentMessage", "text": "recovered task finished without notification"}}))
                else:
                    self.live_turn = "turn-1"
                return frames
            if request.get("method") == "thread/read" and submissions == 2:
                params = cast(dict[str, object], request["params"])
                recovery_reads.append(params)
                return [_reply(request, {"thread": {"id": THREAD_ID,
                    "status": {"type": "idle" if continuation_completed else "active"},
                    "turns": [{"id": "turn-1",
                               "status": "completed" if continuation_completed else "inProgress"}]}})]
            return original(request)

        def report(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "bash":
                self.assertEqual(command[-2], str(Path(codex_mesh.__file__).with_name("agents_config.sh")))
                return subprocess.CompletedProcess(command, 0, "default", "")
            self.assertEqual(command[1], str(codex_mesh.QUOTA_REPORT))
            commands.append(command)
            now = datetime.now(timezone.utc)
            refusals.write_refusal(refusals.AccountRefusal("codex", source.login,
                refusals.KnownWindow("five_hour"), refusals.KnownReset(now - timedelta(seconds=1)),
                now - timedelta(minutes=19)))
            return subprocess.CompletedProcess(command, 0, "", "")

        self.server.respond = respond
        args = self.start_args()
        args.resident = True
        with (patch.object(codex_mesh, "_marker_sessions", return_value=["own-director"]),
              patch.object(codex_mesh, "_launcher_sessions", return_value=()),
              patch.object(subprocess, "run", side_effect=report),
              contextlib.redirect_stderr(io.StringIO()) as errors):
            code = codex_mesh.command_start(args)
        self.assertEqual(code, 1, errors.getvalue())
        self.assertIn("no turn/completed within", errors.getvalue())
        self.assertEqual(submissions, 2)
        self.assertEqual(self.turn_inputs, ["Continue where you stopped."])
        handoffs = [command for command in commands if "--stopped" in command]
        self.assertEqual(len(handoffs), 0 if continuation_completed else 1)
        self.assertEqual(len(commands), 1 if continuation_completed else 2)
        self.assertEqual(self.worker_record()["status"], "failed")
        self.assertIs(self.worker_record().get("continuation_owed"), False)
        if continuation_completed:
            self.assertFalse(any(read.get("includeTurns") is True for read in recovery_reads))
            self.assertTrue(self.methods("thread/turns/list"))
            self.assertIn("recovered task finished without notification",
                          (self.session_dir / "worker.log").read_text())
        else:
            self.assertEqual(handoffs[0][handoffs[0].index("--stopped") + 1:],
                             ["own-director", "worker", shlex.join(sys.argv)])

    def test_idle_completed_quota_continuation_without_notification_then_timeout_sends_no_rerun(self) -> None:
        self.assert_retried_quota_continuation_timeout(continuation_completed=True)

    def test_submission_retry_continuation_still_live_at_timeout_sends_one_rerun(self) -> None:
        self.assert_retried_quota_continuation_timeout(continuation_completed=False)

    def test_failed_quota_continuation_then_completed_held_message_still_sends_one_rerun(self) -> None:
        source = self.prepare_refusing_account()
        self.start_args_timeout = 2.0
        original = self.respond
        deliver = codex_mesh._deliver_reply  # pyright: ignore[reportPrivateUsage]
        commands: list[list[str]] = []
        delivered: list[tuple[int, str]] = []
        submissions = 0

        def respond(request: dict[str, object]) -> list[dict[str, object]] | None:
            nonlocal submissions
            if request.get("method") == "turn/start":
                submissions += 1
                if submissions == 1:
                    return self.quota_completion(request)
                if submissions == 2:
                    self.outcomes = ["failed"]
            return original(request)

        def reply(name: str, index: int, text: str, failure: str, summary_path: Path,
                  reply_path: Path | None, log: TextIO) -> None:
            deliver(name, index, text, failure, summary_path, reply_path, log)
            delivered.append((index, failure))
            if index == 2:
                # Queue only after the accepted continuation has failed and released its submission lock.
                _ = (self.session_dir / "worker.pending.json").write_text(json.dumps({
                    "thread_id": THREAD_ID, "messages": ["Report the current date only"],
                    "accepting_messages": True}))

        def report(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "bash":
                self.assertEqual(command[-2], str(Path(codex_mesh.__file__).with_name("agents_config.sh")))
                return subprocess.CompletedProcess(command, 0, "default", "")
            self.assertEqual(command[1], str(codex_mesh.QUOTA_REPORT))
            commands.append(command)
            now = datetime.now(timezone.utc)
            refusals.write_refusal(refusals.AccountRefusal("codex", source.login,
                refusals.KnownWindow("five_hour"), refusals.KnownReset(now - timedelta(seconds=1)),
                now - timedelta(minutes=19)))
            return subprocess.CompletedProcess(command, 0, "", "")

        self.server.respond = respond
        args = self.start_args()
        args.resident = True
        with (patch.object(codex_mesh, "_marker_sessions", return_value=["own-director"]),
              patch.object(codex_mesh, "_launcher_sessions", return_value=()),
              patch.object(codex_mesh, "_deliver_reply", side_effect=reply),
              patch.object(subprocess, "run", side_effect=report),
              contextlib.redirect_stderr(io.StringIO()) as errors):
            code = codex_mesh.command_start(args)
        self.assertEqual(code, 1, errors.getvalue())
        self.assertIn("no turn/completed within", errors.getvalue())
        self.assertEqual(submissions, 3)
        self.assertEqual(self.turn_inputs, ["Continue where you stopped.", "Report the current date only"])
        self.assertIn((2, "ordinary failure"), delivered)
        self.assertIn((3, ""), delivered)
        handoffs = [command for command in commands if "--stopped" in command]
        self.assertEqual(len(handoffs), 1, commands)
        self.assertEqual(len(commands), 2)
        self.assertEqual(handoffs[0][handoffs[0].index("--stopped") + 1:],
                         ["own-director", "worker", shlex.join(sys.argv)])
        self.assertEqual(self.worker_record()["status"], "failed")
        self.assertIs(self.worker_record().get("continuation_owed"), False)

    def test_stream_refusal_waits_for_room_without_handing_off_or_clearing_continuation(self) -> None:
        source = self.prepare_stream_refusal_wait()
        commands: list[list[str]] = []

        def report(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "bash":
                self.assertEqual(command[-2], str(Path(codex_mesh.__file__).with_name("agents_config.sh")))
                return subprocess.CompletedProcess(command, 0, "default", "")
            self.assertEqual(command[1], str(codex_mesh.QUOTA_REPORT))
            self.save_unresolved_refusal(source)
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with (patch.object(codex_mesh, "_marker_sessions", return_value=["own-director"]),
              patch.object(codex_mesh, "_launcher_sessions", return_value=()),
              patch.object(subprocess, "run", side_effect=report)):
            code, errors = self.run_start()
        self.assertEqual(code, 1)
        self.assertIn("worker waits for room on codex2: the refused account has not recovered", errors)
        self.assertNotIn("no turn/completed within", errors)
        self.assertEqual(len(commands), 1)
        self.assertFalse(any("--stopped" in command for command in commands))
        self.assertIs(self.worker_record().get("continuation_owed"), True)
        self.assertEqual(self.worker_record()["status"], "failed")
        self.assertEqual(len(self.methods("turn/start")), 1)

    def test_replacement_thread_refusal_waits_for_room_and_keeps_its_continuation(self) -> None:
        target, replacement = self.prepare_workers_move()
        self.start_args_timeout = 2.0
        original = self.respond
        commands: list[list[str]] = []

        def respond(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/resume":
                return [{"id": request["id"], "error": {"message": "thread cannot resume"}}]
            if request.get("method") == "turn/start":
                return [{"id": request["id"], "error": {"message": USAGE_LIMIT}}]
            return original(request)

        def move_after_capacity_refusal(seconds: float) -> None:
            self.wait(seconds)
            self.assertEqual(accounts.main(["set-role", "workers", "codex1"]), 0)
            self.next_thread_id = "replacement-thread"

        def report(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "bash":
                self.assertEqual(command[-2], str(Path(codex_mesh.__file__).with_name("agents_config.sh")))
                return subprocess.CompletedProcess(command, 0, "default", "")
            self.assertEqual(command[1], str(codex_mesh.QUOTA_REPORT))
            self.assertEqual(Path(command[3]), target)
            self.save_unresolved_refusal(agent_accounts.KnownCodexAccount(target, command[4], command[5]))
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        replacement.respond = respond
        self.outcomes = ["capacity"]
        with (patch.object(codex_mesh, "_capacity_sleep", side_effect=move_after_capacity_refusal),
              patch.object(codex_mesh, "_stop_unused_with_lock_held", return_value=False),
              patch.object(codex_mesh, "_post_board_status"),
              patch.object(codex_mesh, "_marker_sessions", return_value=["own-director"]),
              patch.object(codex_mesh, "_launcher_sessions", return_value=()),
              patch.object(subprocess, "run", side_effect=report)):
            code, errors = self.run_start()
        self.assertEqual(code, 1)
        self.assertIn("worker waits for room on codex1: the refused account has not recovered", errors)
        self.assertNotIn("still live at the timeout", errors)
        self.assertEqual(len(commands), 1)
        self.assertFalse(any("--stopped" in command for command in commands))
        self.assertEqual(self.worker_record()["thread_id"], "replacement-thread")
        self.assertIs(self.worker_record().get("continuation_owed"), True)
        self.assertEqual(self.worker_record()["status"], "failed")
        self.assertNotIn("launcher_pid", self.worker_record())
        self.assertEqual([request["method"] for request in replacement.requests
                          if request["method"] in ("thread/resume", "thread/start", "turn/start")],
                         ["thread/resume", "thread/start", "turn/start"])

    def test_markerless_room_wait_keeps_its_continuation_without_a_director_handoff(self) -> None:
        source = self.prepare_stream_refusal_wait()
        launcher_pid, director_pid = os.getpid(), 700
        table = {
            launcher_pid: process_table.Process(launcher_pid, director_pid, "python3", "python3 codex_mesh.py start"),
            director_pid: process_table.Process(director_pid, 1, "claude", "claude"),
        }
        director = sessions.SessionRecord(pid=director_pid, sessionId="launcher-director", name="director",
            procStart="fixture-start", messagingSocketPath="", updatedAt=1, tmux="", formerNames=[])
        handoffs: list[list[str]] = []
        routing_reports: list[list[str]] = []

        def alive(pid: int) -> bool:
            return pid in table

        def report(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "bash":
                self.assertEqual(command[-2], str(Path(codex_mesh.__file__).with_name("agents_config.sh")))
                return subprocess.CompletedProcess(command, 0, "default", "")
            self.assertEqual(command[1], str(codex_mesh.QUOTA_REPORT))
            self.save_unresolved_refusal(source)
            if "--stopped" in command:
                self.assertEqual(self.worker_record()["status"], "failed")
                self.assertNotIn("launcher_pid", self.worker_record())
                handoffs.append(command[command.index("--stopped") + 1:])
            else:
                routing_reports.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with (patch.object(codex_mesh, "_marker_sessions", return_value=[]),
              patch.object(codex_mesh, "_pid_alive", side_effect=alive),
              patch.object(process_table, "snapshot", return_value=table) as snapshot,
              patch.object(sessions, "running_sessions", return_value=[director]),
              patch.object(sessions, "process_start", return_value="fixture-start"),
              patch.object(subprocess, "run", side_effect=report)):
            code, errors = self.run_start()
        self.assertEqual(code, 1, errors)
        snapshot.assert_called()
        self.assertEqual(len(routing_reports), 1, errors)
        self.assertEqual(handoffs, [])
        self.assertIn("worker waits for room on codex2: the refused account has not recovered", errors)
        self.assertIs(self.worker_record().get("continuation_owed"), True)

    def test_old_command_exit_leaves_a_replacement_workers_thread_and_continuation_untouched(self) -> None:
        source = self.prepare_refusing_account()
        serving = codex_mesh.ServingCodexServer(self.server.port, os.getpid(), source.home, source, True)
        replacement: dict[str, object] = {"thread_id": "replacement-thread", "status": "running",
            "continuation_owed": True, "launcher_pid": 12345, "port": 4002}

        def finish_old(_args: argparse.Namespace, _port: int, *, started_here: bool) -> codex_mesh.FailedWithThread:
            self.assertTrue(started_here)
            self.write_pool_roster({"worker": replacement})
            return codex_mesh.FailedWithThread(THREAD_ID, USAGE_LIMIT, 10, serving, True, True,
                pending_refusal=codex_mesh.PendingQuotaRefusal(serving, USAGE_LIMIT),
                director=codex_mesh.KnownWorkerDirector("old-director"))

        with (patch.object(codex_mesh, "_run_worker", side_effect=finish_old),
              patch.object(codex_mesh, "_marker_sessions", return_value=["old-director"]),
              patch.object(codex_mesh, "_launcher_sessions", return_value=()),
              patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as report):
            code, errors = self.run_start()
        self.assertEqual(code, 1, errors)
        self.assertEqual(self.worker_record(), replacement)
        report.assert_called_once()
        command = cast(list[str], report.call_args.args[0])
        self.assertEqual(command[command.index("--stopped") + 1:][:2], ["old-director", "worker"])

    def test_refusal_without_a_server_record_retains_an_unknown_account_at_the_selected_home(self) -> None:
        account = self.prepare_refusing_account()
        (self.session_dir / codex_mesh.SERVER_FILE).unlink()
        original = self.respond
        def respond(request: dict[str, object]) -> list[dict[str, object]] | None:
            return self.quota_completion(request) if request.get("method") == "turn/start" else original(request)
        self.server.respond = respond
        with patch.object(codex_mesh, "_report_quota_refusal") as report:
            self.assertEqual(self.run_start()[0], 1)
        self.assert_routing_then_exited_handoff(report)
        serving = cast(codex_mesh.CodexServingState, cast(object, report.call_args.args[1]))
        self.assertIsInstance(serving, codex_mesh.NotBoundCodexServer)
        self.assertIsInstance(serving.account, agent_accounts.UnknownCodexAccount)
        self.assertEqual(serving.account.home, account.home)
        self.assertNotEqual(str(serving.account.home), ".")

    def test_attaching_to_a_fresh_server_keeps_start_tracking_unchanged(self) -> None:
        _ = self.prepare_refusing_account()
        started: set[tuple[int, int]] = set()
        with patch.object(codex_mesh, "_STARTED_SERVERS", started):
            self.assertEqual(self.run_start()[0], 0)
        self.assertEqual(started, set())

    def test_started_refusal_keeps_the_account_after_its_server_record_disappears(self) -> None:
        account = self.prepare_refusing_account()
        self.save_unresolved_refusal(account)
        original = self.respond
        def respond(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "turn/start":
                (self.session_dir / codex_mesh.SERVER_FILE).unlink()
                return self.quota_completion(request)
            return original(request)
        self.server.respond = respond
        with patch.object(codex_mesh, "_report_quota_refusal") as report:
            code, _errors = self.run_start()
        self.assertEqual(code, 1)
        report.assert_called_once()
        self.assertIsInstance(reported_stopped_context(report), codex_mesh.RefusalWithoutCommandExit)
        self.assertIs(self.worker_record().get("continuation_owed"), True)
        serving = cast(codex_mesh.ServingCodexServer, cast(object, report.call_args.args[1]))
        self.assertEqual(serving.account, account)
        self.assertEqual((serving.port, serving.pid, serving.home), (self.server.port, os.getpid(), account.home))

    def test_late_refusal_keeps_the_outgoing_account_when_a_port_record_is_reused(self) -> None:
        account = self.prepare_refusing_account()
        next_home = account.home.parent / ".codex1"
        next_home.mkdir()
        login = "next@example.invalid"
        claims = base64.urlsafe_b64encode(json.dumps({"email": login}).encode()).decode().rstrip("=")
        _ = (next_home / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"header.{claims}.signature"}}))
        _ = (account.home.parent / "notes" / "codex1.md").write_text(f"---\nlogin: {login}\n---\n")
        original = self.respond
        def respond(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "turn/start":
                self.assertEqual(accounts.main(["set-role", "workers", "codex1"]), 0)
                _ = (self.session_dir / codex_mesh.SERVER_FILE).write_text(json.dumps({
                    "port": self.server.port, "pid": os.getpid() + 1, "codex_home": str(next_home), "auto_compact_tokens": codex_mesh.AUTO_COMPACT_TOKENS,
                }))
                return self.quota_completion(request)
            return original(request)
        self.server.respond = respond
        with patch.object(codex_mesh, "_report_quota_refusal") as report:
            self.assertEqual(self.run_start()[0], 1)
        self.assert_routing_then_exited_handoff(report)
        self.assertEqual(cast(codex_mesh.ServingCodexServer, cast(object, report.call_args.args[1])).account, account)
        self.assertEqual(cast(codex_mesh.ServingCodexServer, cast(object, report.call_args.args[1])).pid, os.getpid())
        self.assertEqual(agent_accounts.workers_home(), next_home)

    def test_followed_turn_reports_its_refusing_account_once(self) -> None:
        account = self.prepare_refusing_account()
        self.assertEqual(self.run_start()[0], 0)
        self.save_unresolved_refusal(account)
        self.skip_quota_wait_time()
        original = self.respond
        def respond(request: dict[str, object]) -> list[dict[str, object]] | None:
            return self.quota_completion(request) if request.get("method") == "turn/start" else original(request)
        self.server.respond = respond
        with patch.object(codex_mesh, "_report_quota_refusal") as report:
            self.assertEqual(self.run_follow()[0], 1)
        report.assert_called_once()
        self.assertIsInstance(reported_stopped_context(report),
                              codex_mesh.RefusalWithoutCommandExit)
        self.assertTrue(self.worker_record().get("continuation_owed"))
        self.assertEqual(cast(codex_mesh.ServingCodexServer, cast(object, report.call_args.args[1])).account, account)

    def test_parked_message_refusal_routes_then_hands_the_exited_command_back(self) -> None:
        account = self.prepare_refusing_account()
        original = self.respond
        starts = [0]
        def respond(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "turn/start":
                starts[0] += 1
                if starts[0] == 2:
                    return self.quota_completion(request)
            return original(request)
        self.server.respond = respond
        next_frame = codex_mesh.Client.next_frame
        parked = [False]
        @bounded_fixture
        def park(client: codex_mesh.Client, deadline: float) -> codex_mesh.RpcMessage | None:
            frame = next_frame(client, deadline)
            if frame and frame.get("method") == "turn/completed" and not parked[0]:
                parked[0] = True
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(codex_mesh.command_send(argparse.Namespace(
                        session_dir=str(self.session_dir), to="worker", message="Parked task", message_file="")), 0)
            elif frame and frame.get("method") == "turn/completed" and starts[0] == 2:
                # The caller now owns this refusal frame; a file wake cannot
                # end the stream before it consumes the quota failure.
                _ = (self.session_dir / "worker.end").write_text("end")
            return frame
        args = self.start_args()
        args.resident = True
        args.timeout = 3
        with patch.object(codex_mesh.Client, "next_frame", park), \
                patch.object(codex_mesh, "_report_quota_refusal") as report, contextlib.redirect_stderr(io.StringIO()):
            code = codex_mesh.command_start(args)
        self.assertEqual(code, 1)
        self.assertEqual(starts[0], 2)
        self.assert_routing_then_exited_handoff(report)
        self.assertEqual(cast(codex_mesh.CommandStoppedOnThread, reported_stopped_context(report)).thread_id, THREAD_ID)
        self.assertEqual(cast(codex_mesh.ServingCodexServer, cast(object, report.call_args.args[1])).account, account)
        self.assertIn("Parked task", str(self.methods("turn/start")[1]))

    def test_thread_start_refusal_reports_the_exited_command_context(self) -> None:
        account = self.prepare_refusing_account()
        original = self.respond
        def respond(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/start":
                return [{"id": request["id"], "error": {"message": USAGE_LIMIT}}]
            return original(request)
        self.server.respond = respond
        with patch.object(codex_mesh, "_report_quota_refusal") as report:
            self.assertEqual(self.run_start()[0], 1)
        report.assert_called_once()
        self.assertIsInstance(reported_stopped_context(report), codex_mesh.CommandStoppedBeforeThread)
        self.assertEqual(cast(codex_mesh.CommandStoppedBeforeThread | codex_mesh.CommandStoppedOnThread, reported_stopped_context(report)).session_dir, str(self.session_dir))
        self.assertEqual(cast(codex_mesh.ServingCodexServer, cast(object, report.call_args.args[1])).account, account)

    def test_submission_refusal_reports_its_account_and_keeps_the_continuation_for_room(self) -> None:
        account = self.prepare_refusing_account()
        self.save_unresolved_refusal(account)
        original = self.respond
        def respond(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "turn/start":
                return [{"id": request["id"], "error": {"message": USAGE_LIMIT}}]
            return original(request)
        self.server.respond = respond
        with patch.object(codex_mesh, "_report_quota_refusal") as report:
            self.assertEqual(self.run_start()[0], 1)
        report.assert_called_once()
        self.assertIsInstance(reported_stopped_context(report), codex_mesh.RefusalWithoutCommandExit)
        self.assertIs(self.worker_record().get("continuation_owed"), True)
        self.assertEqual(cast(codex_mesh.ServingCodexServer, cast(object, report.call_args.args[1])).account, account)

    def test_failed_fork_preserves_the_account_without_reporting_a_turn_refusal(self) -> None:
        _ = self.prepare_refusing_account()
        self.write_pool_roster({"template": {"thread_id": THREAD_ID, "status": "done"}})
        original = self.respond
        def respond(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/fork":
                return [{"id": request["id"], "error": {"message": USAGE_LIMIT}}]
            return original(request)
        self.server.respond = respond
        with patch.object(codex_mesh, "_report_quota_refusal") as report:
            code, _out, errors = self.run_mesh(["fork", "--from", "template", "--name", "reviewer"])
        self.assertEqual(code, 1)
        self.assertIn(USAGE_LIMIT, errors)
        report.assert_not_called()

    def test_unproven_fast_refusal_does_not_report_or_open_fallback(self) -> None:
        _ = self.prepare_refusing_account()
        original = self.respond
        def respond(request: dict[str, object]) -> list[dict[str, object]] | None:
            return self.quota_completion(request) if request.get("method") == "turn/start" else original(request)
        self.server.respond = respond
        with patch.object(codex_mesh, "ensure_server", return_value=(self.server.port, False)), \
                patch.object(codex_mesh, "_report_quota_refusal") as report:
            self.assertEqual(self.run_start()[0], 1)
        report.assert_not_called()

    def test_unreadable_serving_sign_in_reports_an_explicit_unknown_account(self) -> None:
        account = self.prepare_refusing_account()
        (account.home / "auth.json").unlink()
        home = account.home.parent
        select_fixture_workers(home, notes=home / "notes")
        selected = home / ".codex9"
        selected.mkdir()
        _ = (account.home / "auth.json").rename(selected / "auth.json")
        original = self.respond
        def respond(request: dict[str, object]) -> list[dict[str, object]] | None:
            return self.quota_completion(request) if request.get("method") == "turn/start" else original(request)
        self.server.respond = respond
        with patch.object(codex_mesh, "_report_quota_refusal") as report:
            self.assertEqual(self.run_start()[0], 1)
        self.assert_routing_then_exited_handoff(report)
        unknown = cast(codex_mesh.ServingCodexServer, cast(object, report.call_args.args[1])).account
        self.assertIsInstance(unknown, agent_accounts.UnknownCodexAccount)
        self.assertEqual(unknown.home, account.home)

    def test_replacement_conversations_first_refusal_names_the_target_account(self) -> None:
        target, new = self.prepare_workers_move()
        account = agent_accounts.codex_account(target)
        assert isinstance(account, agent_accounts.KnownCodexAccount)
        self.save_unresolved_refusal(account)
        original = self.respond
        def respond(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/resume":
                return [{"id": request["id"], "error": {"message": "thread cannot resume"}}]
            if request.get("method") == "turn/start":
                return self.quota_completion(request)
            return original(request)
        new.respond = respond
        @bounded_fixture
        def wait(seconds: float) -> None:
            self.wait(seconds)
            self.assertEqual(accounts.main(["set-role", "workers", "codex1"]), 0)
            self.next_thread_id = "replacement-thread"
        self.outcomes = ["capacity"]
        with patch.object(codex_mesh, "_capacity_sleep", side_effect=wait), \
                patch.object(codex_mesh, "_report_quota_refusal") as report:
            self.assertEqual(self.run_start()[0], 1)
        report.assert_called_once()
        self.assertIsInstance(reported_stopped_context(report), codex_mesh.RefusalWithoutCommandExit)
        self.assertIs(self.worker_record().get("continuation_owed"), True)
        serving = cast(codex_mesh.ServingCodexServer, cast(object, report.call_args.args[1]))
        self.assertEqual(serving.home, target)
        self.assertIsInstance(serving.account, agent_accounts.KnownCodexAccount)
        assert isinstance(serving.account, agent_accounts.KnownCodexAccount)
        self.assertEqual(serving.account.label, "codex1")
        target_starts = [request for request in new.requests if request.get("method") == "turn/start"]
        self.assertEqual(len(target_starts), 1)
        self.assertIn("replacement-thread", str(target_starts[0]))

    def test_release_refusal_does_not_report_a_provider_turn_refusal(self) -> None:
        _target, new = self.prepare_workers_move()
        self.assertEqual(accounts.main(["set-role", "workers", "codex1"]), 0)
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "worker": {"thread_id": THREAD_ID, "port": self.server.port, "status": "running"},
        }))
        original = self.respond
        def respond(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/unsubscribe":
                return [{"id": request["id"], "error": {"message": USAGE_LIMIT}}]
            if request.get("method") == "thread/loaded/list":
                return [_reply(request, {"data": [THREAD_ID]})]
            return original(request)
        self.server.respond = respond
        client = codex_mesh.Client(self.server.port, "worker")
        try:
            with patch.object(codex_mesh, "THREAD_RELEASE_TIMEOUT_SECS", 0.02), \
                    patch.object(codex_mesh, "_report_quota_refusal") as report:
                result = codex_mesh._move_resident(self.start_args(), str(self.session_dir), "worker", THREAD_ID, client)  # pyright: ignore[reportPrivateUsage]
        finally:
            client.close()
        self.assertIsInstance(result, codex_mesh.WorkerThreadStateUnknown)
        report.assert_not_called()
        self.assertFalse(any(request.get("method") == "turn/start" for request in new.requests))

    def prepare_workers_move(self) -> tuple[Path, StubAppServer]:
        home = self.session_dir / "home"
        home.mkdir()
        _ = cast(object, self.patches.enter_context(patch.dict(os.environ, {"HOME": str(home), "AGENT_NOTES_DIR": str(home)})))
        target = home / ".codex1"
        target.mkdir()
        login = "target@example.invalid"
        claims = base64.urlsafe_b64encode(json.dumps({"email": login}).encode()).decode().rstrip("=")
        _ = (target / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"header.{claims}.signature"}}))
        _ = (home / "codex1.md").write_text(f"---\nlogin: {login}\n---\n")
        source = home / ".codex"
        source.mkdir()
        source_login = "source@example.invalid"
        source_claims = base64.urlsafe_b64encode(json.dumps({"email": source_login}).encode()).decode().rstrip("=")
        _ = (source / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"header.{source_claims}.signature"}}))
        _ = (home / "codex9.md").write_text(f"---\nlogin: {source_login}\n---\n")
        self.assertEqual(accounts.main(["set-role", "workers", "codex9"]), 0)
        _ = (self.session_dir / codex_mesh.SERVER_FILE).write_text(json.dumps({
            "port": self.server.port, "pid": os.getpid(), "codex_home": str(home / ".codex"), "auto_compact_tokens": codex_mesh.AUTO_COMPACT_TOKENS,
        }))
        new = StubAppServer(self.respond)
        _ = self.patches.callback(new.close)
        def start(session_dir: str, *_args: object) -> int:
            _ = (Path(session_dir) / codex_mesh.SERVER_FILE).write_text(json.dumps({
                "port": new.port, "pid": os.getpid(), "codex_home": str(agent_accounts.workers_home()), "auto_compact_tokens": codex_mesh.AUTO_COMPACT_TOKENS,
            }))
            return new.port
        _ = self.patches.enter_context(patch.object(codex_mesh, "_start_server", side_effect=start))
        _ = self.patches.enter_context(patch.object(codex_mesh, "_start_watcher"))
        _ = self.patches.enter_context(patch.object(codex_mesh, "_holds_old_sign_in", return_value=False))
        return target, new

    def prepare_running_account_swap(self) -> tuple[Path, StubAppServer]:
        _ = self.patches.enter_context(patch.object(codex_mesh, "_server_folder",
                                                   return_value=codex_mesh.NonMeshServerStderr()))
        run = self.session_dir / "scratch-runs" / "run"
        run.mkdir(parents=True)
        _ = (run / "prompt.txt").write_text("Original worker task")
        self.session_dir = run
        target, new = self.prepare_workers_move()
        active = run.parent / "active"
        active.mkdir()
        _ = (active / "scratch-holder").write_text(str(run) + "\n")
        _ = self.patches.enter_context(patch.object(sessions, "running_sessions",
                                                   return_value=[running_holder("scratch-holder")]))
        self.outcomes = ["disconnect", "completed"]
        self.server.hold_turn_completion = True
        respond = self.server.respond
        def old(request: dict[str, object]) -> list[dict[str, object]] | None:
            frames = respond(request)
            if request.get("method") == "turn/start":
                self.live_turn = "turn-1"
            if request.get("method") == "turn/interrupt":
                marker_path = self.session_dir / "worker.account-move.json"
                self.assertTrue(marker_path.exists(), "account-move intent must precede the interrupt")
                marker = _as_test_dict(cast(object, json.loads(marker_path.read_text())))
                self.assertEqual(marker["thread_id"], THREAD_ID)
                self.assertEqual(marker["turn_id"], "turn-1")
                self.assertFalse((self.session_dir / "worker.end").exists())
            return frames
        self.server.respond = old
        _ = self.patches.enter_context(patch.object(codex_mesh, "SWEEP_ROOTS", (self.session_dir.parent,)))
        _ = self.patches.enter_context(patch.object(codex_mesh, "_stop_unused", return_value=False))
        _ = self.patches.enter_context(patch.object(codex_mesh, "_stop_unused_with_lock_held", return_value=False))
        _ = self.patches.enter_context(patch.object(codex_mesh, "_notify_holder"))
        return target, new

    def assert_account_move_continuation(self, new: StubAppServer) -> None:
        starts = [request for request in new.requests if request.get("method") == "turn/start"]
        self.assertEqual(len(starts), 1)
        params = _as_test_dict(starts[0]["params"])
        self.assertEqual(params["threadId"], THREAD_ID)
        inputs = cast(list[dict[str, object]], params["input"])
        self.assertEqual([item["text"] for item in inputs], [
            "Your turn was interrupted to move your Codex account; any command you had running was stopped "
            + "with it, so run it again. Continue where you stopped."])
        self.assertFalse(any(request.get("method") in ("thread/start", "thread/fork") for request in new.requests))
        resumes = [request for request in new.requests if request.get("method") == "thread/resume"]
        self.assertTrue(resumes)
        self.assertTrue(all(_as_test_dict(request["params"])["threadId"] == THREAD_ID for request in resumes))

    def assert_waiting_launcher_account_move(self, *, sign_in_change: bool, ended_run: bool = False) -> None:
        target, new = self.prepare_running_account_swap()
        ended_runs: list[tuple[Path, StubAppServer]] = []
        if ended_run:
            ended = self.session_dir.with_name("ended-run")
            ended.mkdir()
            def idle(request: dict[str, object]) -> list[dict[str, object]]:
                method = request.get("method")
                if method == "thread/loaded/list":
                    return [_reply(request, {"data": ["ended-thread"]})]
                if method == "thread/queue/list":
                    return [_reply(request, {"data": [], "nextCursor": None})]
                if method == "thread/read":
                    return [_reply(request, {"thread": {"id": "ended-thread", "status": {"type": "idle"}, "turns": []}})]
                self.assertNotIn(method, ("turn/interrupt", "turn/start", "thread/resume"))
                return [_reply(request, {})]
            server = StubAppServer(idle)
            _ = self.patches.callback(server.close)
            ended_runs.append((ended, server))
            def process_alive(pid: int) -> bool:
                return pid in (os.getpid(), 81234)
            _ = self.patches.enter_context(patch.object(codex_mesh, "_pid_alive",
                side_effect=process_alive))
            _ = (ended / codex_mesh.SERVER_FILE).write_text(json.dumps({"pid": 81234,
                "port": server.port, "codex_home": str(target.parent / ".codex")}))
            # Stale roster and launcher survive their holder; neither makes the run live.
            _ = (ended / codex_mesh.ROSTER_FILE).write_text(json.dumps({"unread-worker": {
                "thread_id": "ended-thread", "turn_id": "stale-turn", "status": "running",
                "launcher_pid": os.getpid(), "role": "impl", "port": server.port}}))
            _ = (ended.parent / "active/dead-holder").write_text(str(ended) + "\n")
        label = "codex9" if sign_in_change else "codex1"
        if sign_in_change:
            _ = cast(object, self.patches.enter_context(patch.dict(os.environ, {"CODEX_HOME": str(target.parent / ".codex")})))
            _ = self.patches.enter_context(patch.object(codex_mesh, "_sign_in_changed_at", return_value=2000))
        waiting = threading.Event()
        failures: list[BaseException] = []
        switches: list[codex_mesh.WorkersRunSwitch] = []
        read = codex_mesh.Client.next_frame
        @bounded_fixture
        def frame(client: codex_mesh.Client, deadline: float) -> codex_mesh.RpcMessage | None:
            path = self.session_dir / codex_mesh.ROSTER_FILE
            if client.port == self.server.port and path.exists():
                worker = _as_test_dict(_as_test_dict(cast(object, json.loads(path.read_text()))).get("worker"))
                if worker.get("status") == "running" and worker.get("turn_id") == "turn-1":
                    waiting.set()
            return read(client, deadline)
        def swap() -> None:
            try:
                if not waiting.wait(timeout=30):
                    raise AssertionError("launcher never waited for its running turn")
                if sign_in_change:
                    def old_sign_in(_pid: int, _home: Path) -> bool:
                        path = self.session_dir / codex_mesh.SERVER_FILE
                        return _pid == 81234 or (path.exists() and
                            _as_test_dict(cast(object, json.loads(path.read_text()))).get("port") == self.server.port)
                    with patch.object(codex_mesh, "_holds_old_sign_in", side_effect=old_sign_in):
                        self.assertEqual(codex_mesh.command_signin_changed(argparse.Namespace(
                            root=str(self.session_dir.parent))), 0)
                else:
                    self.assertEqual(accounts.main(["set-role", "workers", "codex1"]), 0)
                    switches.extend(codex_mesh.retire_workers_servers(target, "codex1"))
            except BaseException as error:
                failures.append(error)
            finally:
                self.server.release_held_turn.set()
        mover = threading.Thread(target=swap, daemon=True)
        def stop(_roots: tuple[Path, ...] | None, _pid: int, port: int,
                 ownership: codex_mesh.ServerOwnership) -> bool:
            for folder, server in ended_runs:
                if port == server.port:
                    self.assertEqual(ownership, codex_mesh.RetiredRunServer(folder))
                    self.assertEqual(codex_mesh._busy_threads(port, {"ended-thread": "unread-worker"}), ())  # pyright: ignore[reportPrivateUsage]
                    return True
            return False
        with (patch.object(codex_mesh.Client, "next_frame", frame),
              patch.object(codex_mesh, "_notify_holder") as notify,
              patch.object(codex_mesh, "_resume_account_move_detached",
                           side_effect=AssertionError("a live holder's launcher resumes its own worker")) as detach,
              patch.object(codex_mesh, "_stop_unused", side_effect=stop) as stopped,
              contextlib.redirect_stdout(io.StringIO()) as output):
            mover.start()
            try:
                code, errors = self.run_start()
            finally:
                self.server.release_held_turn.set()
                mover.join(timeout=30)
        self.assertFalse(mover.is_alive(), "swap did not finish after launcher continuation")
        if failures:
            raise failures[0]
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(len(self.methods("turn/interrupt")), 1)
        self.assertEqual(_as_test_dict(self.methods("turn/interrupt")[0]["params"]),
                         {"threadId": THREAD_ID, "turnId": "turn-1"})
        self.assert_account_move_continuation(new)
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)
        self.assertEqual(self.worker_record()["status"], "done")
        self.assertFalse((self.session_dir / "worker.account-move.json").exists())
        self.assertFalse((self.session_dir / "worker.end").exists())
        board = (self.session_dir / "board.log").read_text()
        self.assertEqual(board.count(f"account moved to {label}; resumed"), 1)
        notify.assert_called_once()
        notice = str(cast(object, notify.call_args.args[1]))
        self.assertIn("running workers were interrupted and resumed", notice)
        self.assertNotIn("finish first", notice)
        self.assertEqual(notify.call_args.args[2], codex_mesh.CodexNoticeKind.SIGN_IN_CHANGED if sign_in_change
                         else codex_mesh.CodexNoticeKind.WORKERS_CHANGED)
        self.assertEqual(notify.call_args.args[0], "scratch-holder")
        detach.assert_not_called()
        for folder, server in ended_runs:
            self.assertFalse((folder / "unread-worker.account-move.json").exists())
            self.assertFalse((folder / codex_mesh.SERVER_FILE).exists())
            self.assertFalse(any(request.get("method") in ("turn/interrupt", "turn/start", "thread/resume")
                                 for request in server.requests))
            self.assertTrue(any(call.args == (None, 81234, server.port, codex_mesh.RetiredRunServer(folder))
                                for call in stopped.call_args_list))
            if sign_in_change:
                self.assertIn("1 leftover servers from ended sessions, 1 stopped", output.getvalue())
            else:
                leftover = next(run for run in switches if run.folder == folder)
                self.assertEqual((leftover.workers, leftover.holders, leftover.leftover_servers,
                                  leftover.stopped_servers), ((), (), 1, 1))
        if not sign_in_change:
            live = [run for run in switches if run.workers]
            self.assertEqual(len(live), 1)
            self.assertTrue(live[0].workers[0].mid_turn)

    def test_swap_resumes_socketless_live_holder_but_leaves_dead_holders_worker_alone(self) -> None:
        self.assert_waiting_launcher_account_move(sign_in_change=False, ended_run=True)

    def test_account_move_between_marker_read_and_idle_check_resumes_same_thread(self) -> None:
        target, new = self.prepare_running_account_swap()
        self.server.hold_turn_completion = False
        interrupted = False

        @bounded_fixture
        def interrupt_after_marker_read(
            session_dir: str, name: str, thread_id: str, runs: codex_mesh.DetachedRunWatch,
        ) -> bool:
            nonlocal interrupted
            self.assertEqual((runs.session_dir, runs.name), (session_dir, name))
            if not interrupted:
                interrupted = True
                self.assertEqual(thread_id, THREAD_ID)
                self.assertEqual(self.live_turn, "turn-1")
                self.assertFalse((self.session_dir / "worker.account-move.json").exists())
                self.assertEqual(accounts.main(["set-role", "workers", "codex1"]), 0)
                self.assertEqual(agent_accounts.workers_home(), target)
                move = codex_mesh._interrupt_worker_for_account_move(  # pyright: ignore[reportPrivateUsage]
                    session_dir, name, self.worker_record(), self.server.port, "codex1")
                self.assertIsInstance(move, codex_mesh.InterruptedWorkerMove)
                self.assertEqual(self.live_turn, "")
            return False

        with (patch.object(codex_mesh, "_waiting_on_detached_run", side_effect=interrupt_after_marker_read),
              contextlib.redirect_stdout(io.StringIO()) as output):
            code, errors = self.run_start()

        self.assertTrue(interrupted)
        self.assertEqual((code, errors), (0, ""))
        self.assert_account_move_continuation(new)
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)
        self.assertEqual(self.worker_record()["status"], "done")
        self.assertNotIn("idle with no completion notice", output.getvalue() + errors)
        self.assertNotIn("no completion notice arrived", (self.session_dir / "worker.log").read_text())

    def test_sign_in_change_resumes_live_holder_but_leaves_dead_holders_worker_alone(self) -> None:
        self.assert_waiting_launcher_account_move(sign_in_change=True, ended_run=True)

    def test_quiet_poll_resumes_a_marked_account_move_after_a_system_error(self) -> None:
        _target, destination = self.prepare_workers_move()
        self.outcomes = ["disconnect", "completed"]
        # The run ends on the resumed turn's completion; the budget only bounds
        # a failure, so it sits far above full-suite load.
        self.start_args_timeout = 120
        marker_injected = False
        on_frame = codex_mesh._on_frame  # pyright: ignore[reportPrivateUsage]

        @bounded_fixture
        def mark_move_before_quiet_end(
            stream: codex_mesh.TurnStream, log: TextIO, frame: codex_mesh.RpcMessage,
        ) -> codex_mesh.StreamStep | codex_mesh.FailedWithThread:
            nonlocal marker_injected
            step = on_frame(stream, log, frame)
            item = _as_test_dict(_as_test_dict(frame.get("params")).get("item"))
            if stream.client.port == self.server.port and item.get("type") == "userMessage" and not marker_injected:
                # The submission notice must drain before the quiet lifecycle
                # check. Install the marker here so idle cannot win that race.
                marker_injected = True
                self.error_threads.add(THREAD_ID)
                self.assertEqual(accounts.main(["set-role", "workers", "codex1"]), 0)
                moved = codex_mesh._interrupt_worker_for_account_move(  # pyright: ignore[reportPrivateUsage]
                    str(self.session_dir), "worker", self.worker_record(), self.server.port, "codex1")
                self.assertIsInstance(moved, codex_mesh.InterruptedWorkerMove)
            return step

        with patch.object(codex_mesh, "_on_frame", mark_move_before_quiet_end):
            code, errors = self.run_start()

        self.assertEqual((code, errors), (0, ""))
        self.assertTrue(marker_injected)
        self.assert_account_move_continuation(destination)
        self.assertEqual(self.worker_record()["status"], "done")
        self.assertFalse((self.session_dir / "worker.account-move.json").exists())

    def assert_ended_busy_server_is_left_running(self, *, sign_in_change: bool) -> None:
        target, new = self.prepare_running_account_swap()
        self.live_turn = "turn-1"
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "turn_id": "turn-1", "status": "running",
            "launcher_pid": os.getpid(), "role": "impl", "port": self.server.port}})
        if sign_in_change:
            current = self.session_dir / codex_mesh.SERVER_FILE
            _ = (self.session_dir / codex_mesh.RETIRED_FILE).write_text(json.dumps({
                "servers": [json.loads(current.read_text())]}))
            current.unlink()
        before = {path.name: path.read_bytes() for path in self.session_dir.iterdir() if path.is_file()}
        with (patch.object(sessions, "running_sessions", return_value=[]),
              patch.object(codex_mesh, "_client_counts", return_value={}),
              patch.object(codex_mesh, "_stop_unused", return_value=False) as stop,
              patch.object(codex_mesh, "_notify_holder") as notify,
              patch.object(codex_mesh, "_resume_account_move_detached") as detach,
              contextlib.redirect_stdout(io.StringIO()) as output):
            preview = codex_mesh.retire_workers_servers(target, "codex1", dry_run=True)
            stop.assert_not_called()
            self.assertFalse((self.session_dir / "worker.account-move.json").exists())
            self.assertEqual({path.name: path.read_bytes() for path in self.session_dir.iterdir()
                              if path.is_file() and path.name in before}, before)
            self.assertEqual((preview[0].workers, preview[0].holders, preview[0].leftover_servers), ((), (), 1))
            kept = preview[0].kept_servers
            self.assertEqual([(server.pid, server.port) for server in kept], [(os.getpid(), self.server.port)])
            self.assertIn("conversation", kept[0].reason)
            self.assertIn("worker", kept[0].reason)
            if sign_in_change:
                with (patch.dict(os.environ, {"CODEX_HOME": str(target.parent / ".codex")}),
                      patch.object(codex_mesh, "_sign_in_changed_at", return_value=2000),
                      patch.object(codex_mesh, "_holds_old_sign_in", return_value=True)):
                    self.assertEqual(codex_mesh.command_signin_changed(argparse.Namespace(
                        root=str(self.session_dir.parent))), 0)
                text = output.getvalue()
                self.assertIn("1 leftover servers from ended sessions, 0 stopped", text)
                self.assertIn(f"left running: {os.getpid()} on port {self.server.port} ({self.session_dir}):", text)
                self.assertIn("conversation", text)
            else:
                switched = codex_mesh.retire_workers_servers(target, "codex1")
                self.assertEqual((switched[0].workers, switched[0].stopped_servers), ((), 0))
                self.assertEqual(switched[0].kept_servers, kept)
            stop.assert_called_once_with(None, os.getpid(), self.server.port,
                                         codex_mesh.RetiredRunServer(self.session_dir))
            notify.assert_not_called()
            detach.assert_not_called()
        self.assertEqual(self.methods("turn/interrupt"), [])
        self.assertEqual(new.requests, [])
        self.assertEqual(self.live_turn, "turn-1")
        self.assertFalse((self.session_dir / "worker.account-move.json").exists())
        self.assertFalse((self.session_dir / codex_mesh.SERVER_FILE).exists())

    def test_swap_keeps_ended_run_busy_server_and_preview_never_interrupts_it(self) -> None:
        self.assert_ended_busy_server_is_left_running(sign_in_change=False)

    def test_sign_in_change_finds_ended_retired_only_busy_server_and_names_why_it_stays(self) -> None:
        self.assert_ended_busy_server_is_left_running(sign_in_change=True)

    def test_swap_without_holders_requires_a_live_launcher_and_any_running_holder_keeps_a_run_live(self) -> None:
        target, _ = self.prepare_running_account_swap()
        active = self.session_dir.parent / "active"
        (active / "scratch-holder").unlink()
        cases = (
            ((), (), os.getpid(), True),
            ((), (), 99999999, False),
            (("dead-holder",), (), os.getpid(), False),
            (("dead-holder", "socketless-holder"), ("socketless-holder",), 99999999, True),
        )
        for holders, running, launcher, expected_live in cases:
            with self.subTest(holders=holders, running=running, live_launcher=launcher == os.getpid()):
                for marker in active.iterdir():
                    marker.unlink()
                for holder in holders:
                    _ = (active / holder).write_text(str(self.session_dir) + "\n")
                self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "running",
                    "turn_id": "turn-1", "launcher_pid": launcher, "port": self.server.port}})
                with (patch.object(sessions, "running_sessions", return_value=[running_holder(holder) for holder in running]),
                      patch.object(sessions, "live_sessions", side_effect=AssertionError("sockets do not decide liveness")),
                      patch.object(codex_mesh, "_client_counts", return_value={}),
                      patch.object(codex_mesh, "_stop_unused") as stop):
                    preview = codex_mesh.retire_workers_servers(target, "codex1", dry_run=True)
                self.assertEqual(bool(preview[0].workers), expected_live)
                self.assertEqual(preview[0].leftover_servers, 0 if expected_live else 1)
                stop.assert_not_called()
        self.assertEqual(self.methods("turn/interrupt"), [])

    def test_sign_in_change_stops_ended_retired_only_server_without_resuming_its_workers(self) -> None:
        target, new = self.prepare_running_account_swap()
        current = self.session_dir / codex_mesh.SERVER_FILE
        _ = (self.session_dir / codex_mesh.RETIRED_FILE).write_text(json.dumps({"servers": [json.loads(current.read_text())]}))
        current.unlink()
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "running", "turn_id": "stale-turn",
            "launcher_pid": os.getpid(), "port": self.server.port, "role": "impl"}})
        with (patch.dict(os.environ, {"CODEX_HOME": str(target.parent / ".codex")}),
              patch.object(sessions, "running_sessions", return_value=[]),
              patch.object(codex_mesh, "_sign_in_changed_at", return_value=2000),
              patch.object(codex_mesh, "_holds_old_sign_in", return_value=True),
              patch.object(codex_mesh, "_stop_unused", return_value=True) as stop,
              patch.object(codex_mesh, "_notify_holder") as notify,
              patch.object(codex_mesh, "_resume_account_move_detached") as detach,
              contextlib.redirect_stdout(io.StringIO()) as output):
            self.assertEqual(codex_mesh.command_signin_changed(argparse.Namespace(root=str(self.session_dir.parent))), 0)
        stop.assert_called_once_with(None, os.getpid(), self.server.port,
                                     codex_mesh.RetiredRunServer(self.session_dir))
        self.assertIn("1 leftover servers from ended sessions, 1 stopped", output.getvalue())
        self.assertNotIn("left running:", output.getvalue())
        self.assertEqual(self.methods("turn/interrupt"), [])
        self.assertEqual(new.requests, [])
        self.assertFalse((self.session_dir / "worker.account-move.json").exists())
        notify.assert_not_called()
        detach.assert_not_called()

    def assert_account_move_interrupts_all_runs_before_waiting(self, *, sign_in_change: bool) -> None:
        target, new = self.prepare_running_account_swap()
        first_run = self.session_dir
        second_run = first_run.with_name("run-z")
        second_run.mkdir()
        source = target.parent / ".codex"
        _ = (second_run / codex_mesh.SERVER_FILE).write_text(json.dumps({
            "port": self.server.port, "pid": os.getpid(), "codex_home": str(source),
        }))
        locations = {"thread-a": (first_run, "first"), "thread-b": (first_run, "second"),
                     "thread-c": (second_run, "third")}
        for folder in (first_run, second_run):
            _ = (folder / codex_mesh.ROSTER_FILE).write_text(json.dumps({
                name: {"thread_id": thread_id, "turn_id": "live-turn", "status": "running", "role": "impl",
                       "port": self.server.port, "launcher_pid": os.getpid()}
                for thread_id, (run, name) in locations.items() if run == folder
            }))
        interrupts: list[tuple[str, float]] = []
        respond = self.respond
        def old(request: dict[str, object]) -> list[dict[str, object]] | None:
            params = _as_test_dict(request.get("params"))
            thread_id = str(params.get("threadId", ""))
            if request.get("method") == "thread/read":
                return [_reply(request, {"thread": {"id": thread_id, "status": {"type": "active"}, "turns": []}})]
            if request.get("method") == "thread/turns/list":
                return [_reply(request, {"data": [{"id": "live-turn", "status": "inProgress"}], "nextCursor": None})]
            if request.get("method") == "turn/interrupt":
                folder, name = locations[thread_id]
                marker = _as_test_dict(cast(object, json.loads((folder / f"{name}.account-move.json").read_text())))
                self.assertEqual(marker["thread_id"], thread_id)
                interrupts.append((thread_id, self.elapsed))
                return [_reply(request, {})]
            return respond(request)
        self.server.respond = old
        @bounded_fixture
        def advance(seconds: float) -> None:
            self.elapsed += seconds
        with (patch.object(codex_mesh, "ACCOUNT_MOVE_RESUME_TIMEOUT_SECS", 0.2),
              patch.object(time, "monotonic", new=self.clock),
              patch.object(time, "sleep", new=advance),
              patch.object(codex_mesh, "_resume_account_move_detached") as detached,
              contextlib.redirect_stdout(io.StringIO())):
            if sign_in_change:
                with (patch.dict(os.environ, {"CODEX_HOME": str(source)}),
                      patch.object(codex_mesh, "_sign_in_changed_at", return_value=2000),
                      patch.object(codex_mesh, "_holds_old_sign_in", return_value=True)):
                    self.assertEqual(codex_mesh.command_signin_changed(argparse.Namespace(root=str(first_run.parent))), 0)
            else:
                switches = codex_mesh.retire_workers_servers(target, "codex1")
                self.assertEqual([len(run.workers) for run in switches], [2, 1])
                self.assertTrue(all("timeout" in worker.failure for run in switches for worker in run.workers))
        self.assertEqual(interrupts, [("thread-a", 0.0), ("thread-b", 0.0), ("thread-c", 0.0)],
                         "all runs must be interrupted before a stalled continuation is awaited")
        self.assertAlmostEqual(self.elapsed, 0.2, msg="all continuations share a single acknowledgement deadline")
        detached.assert_not_called()
        self.assertEqual(new.requests, [])

    def test_swap_interrupts_all_runs_before_a_stalled_account_move_times_out(self) -> None:
        self.assert_account_move_interrupts_all_runs_before_waiting(sign_in_change=False)

    def test_sign_in_change_interrupts_all_runs_before_a_stalled_account_move_times_out(self) -> None:
        self.assert_account_move_interrupts_all_runs_before_waiting(sign_in_change=True)

    def test_swap_interrupts_waiting_launcher_and_finishes_the_same_pass_on_new_account(self) -> None:
        self.assert_waiting_launcher_account_move(sign_in_change=False)

    def test_sign_in_change_interrupts_and_resumes_a_waiting_launcher_on_a_fresh_server(self) -> None:
        self.assert_waiting_launcher_account_move(sign_in_change=True)

    def assert_detached_account_move(
        self, *, recorded_launcher: int, review_lens: agents_log.Lens | None = None,
    ) -> None:
        target, new = self.prepare_running_account_swap()
        self.outcomes = ["completed"]
        self.live_turn = "turn-1"
        worker: dict[str, object] = {"thread_id": THREAD_ID, "turn_id": "turn-1", "status": "running",
                                   "role": "impl", "port": self.server.port}
        if recorded_launcher:
            worker["launcher_pid"] = recorded_launcher
        if review_lens:
            worker["role"] = "review"
            agents_log.append(THREAD_ID, "review", review_lens, datetime.now(timezone.utc))
        self.write_pool_roster({"worker": worker})
        results: list[int] = []
        failures: list[BaseException] = []
        launchers: list[threading.Thread] = []
        def detached(session_dir: str, name: str) -> None:
            def resume() -> None:
                try:
                    results.append(codex_mesh.command_resume_account_move(argparse.Namespace(
                        session_dir=session_dir, to=name)))
                except BaseException as error:
                    failures.append(error)
            launcher = threading.Thread(target=resume, daemon=True)
            launchers.append(launcher)
            launcher.start()
        with (patch.object(codex_mesh, "_resume_account_move_detached", side_effect=detached),
              contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(accounts.main(["set-role", "workers", "codex1"]), 0)
            switches = codex_mesh.retire_workers_servers(target, "codex1")
            for launcher in launchers:
                launcher.join(timeout=30)
        self.assertEqual(len(launchers), 1)
        self.assertTrue(all(not launcher.is_alive() for launcher in launchers))
        if failures:
            raise failures[0]
        self.assertEqual((results, errors.getvalue()), ([0], ""))
        self.assertEqual(len(self.methods("turn/interrupt")), 1)
        self.assert_account_move_continuation(new)
        self.assertEqual(self.worker_record()["status"], "done")
        self.assertFalse((self.session_dir / "worker.account-move.json").exists())
        self.assertEqual(switches[0].workers[0].failure, "")
        self.assertEqual((self.session_dir / "board.log").read_text().count("account moved to codex1; resumed"), 1)
        if review_lens:
            assignment = agents_log.role_at(agents_log.assignments(agents_log.state_dir() / "agents.jsonl"),
                                            THREAD_ID, datetime.now(timezone.utc))
            self.assertIsInstance(assignment, agents_log.Assignment)
            assert isinstance(assignment, agents_log.Assignment)
            self.assertEqual((assignment.role, assignment.lens), ("review", review_lens))
            self.assertEqual(self.worker_record()["role"], "review")

    def test_swap_detached_account_move_keeps_the_review_lens(self) -> None:
        self.assert_detached_account_move(recorded_launcher=0, review_lens="craft")

    def test_swap_resumes_running_worker_with_dead_launcher_using_detached_continuation(self) -> None:
        self.assert_detached_account_move(recorded_launcher=99999999)

    def test_swap_resumes_running_worker_without_launcher_pid_using_detached_continuation(self) -> None:
        self.assert_detached_account_move(recorded_launcher=0)

    def test_swap_reports_interrupt_rejection_without_detaching_or_resuming_worker(self) -> None:
        target, new = self.prepare_running_account_swap()
        self.live_turn = "turn-1"
        self.interrupt_error = "interrupt rejected by app-server"
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "turn_id": "turn-1", "status": "running",
            "role": "impl", "port": self.server.port}})
        with patch.object(codex_mesh, "_resume_account_move_detached") as detach:
            switches = codex_mesh.retire_workers_servers(target, "codex1")
        worker = switches[0].workers[0]
        self.assertIn("interrupt failed", worker.failure)
        self.assertIn("interrupt rejected by app-server", worker.failure)
        self.assertIn("resume-account-move", worker.resume_command)
        self.assertIn("--to worker", worker.resume_command)
        self.assertEqual(len(self.methods("turn/interrupt")), 1)
        self.assertEqual(self.live_turn, "turn-1")
        self.assertEqual(new.requests, [])
        self.assertFalse((self.session_dir / "worker.account-move.json").exists())
        self.assertFalse((self.session_dir / "worker.end").exists())
        detach.assert_not_called()

    def test_swap_detached_resume_rejection_keeps_assignment_for_the_manual_retry(self) -> None:
        target, new = self.prepare_running_account_swap()
        self.live_turn = "turn-1"
        self.outcomes = ["completed"]
        agents_log.append(THREAD_ID, "review", "contract", datetime.now(timezone.utc))
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "turn_id": "turn-1", "status": "running",
            "role": "review", "port": self.server.port}})
        respond = new.respond
        def rejected_resume(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/resume":
                return [{"id": request["id"], "error": {"message": "resume rejected by app-server"}}]
            return respond(request)
        new.respond = rejected_resume
        results: list[int] = []
        def detached(session_dir: str, name: str) -> None:
            results.append(codex_mesh.command_resume_account_move(argparse.Namespace(session_dir=session_dir, to=name)))
        with (patch.object(codex_mesh, "_resume_account_move_detached", side_effect=detached),
              contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO())):
            self.assertEqual(accounts.main(["set-role", "workers", "codex1"]), 0)
            switches = codex_mesh.retire_workers_servers(target, "codex1")
        worker = switches[0].workers[0]
        self.assertEqual(len(results), 1)
        self.assertNotEqual(results[0], 0)
        self.assertIn("resume failed", worker.failure)
        self.assertIn("resume-account-move", worker.resume_command)
        self.assertEqual(len(self.methods("turn/interrupt")), 1)
        self.assertTrue(any(request.get("method") == "thread/resume" for request in new.requests))
        self.assertFalse(any(request.get("method") in ("thread/start", "thread/fork", "turn/start") for request in new.requests))
        self.assertEqual(agent_accounts.workers_home(), target)
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)
        self.assertTrue((self.session_dir / "worker.account-move.json").exists())
        self.assertEqual(self.worker_record()["role"], "review")
        new.respond = respond
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
            code = codex_mesh.main(shlex.split(worker.resume_command)[2:])
        self.assertEqual((code, errors.getvalue()), (0, ""))
        self.assert_account_move_continuation(new)
        self.assertEqual(self.worker_record()["status"], "done")
        self.assertEqual(self.worker_record()["role"], "review")
        assignment = agents_log.role_at(agents_log.assignments(agents_log.state_dir() / "agents.jsonl"),
                                        THREAD_ID, datetime.now(timezone.utc))
        self.assertIsInstance(assignment, agents_log.Assignment)
        assert isinstance(assignment, agents_log.Assignment)
        self.assertEqual((assignment.role, assignment.lens), ("review", "contract"))
        self.assertFalse((self.session_dir / "worker.account-move.json").exists())

    def test_swap_preview_does_not_interrupt_or_write_account_move_intent(self) -> None:
        target, new = self.prepare_running_account_swap()
        self.live_turn = "turn-1"
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "turn_id": "turn-1", "status": "running",
            "role": "impl", "port": self.server.port, "launcher_pid": os.getpid()}})
        before = {path.name: path.read_bytes() for path in self.session_dir.iterdir() if path.is_file()}
        with patch.object(codex_mesh, "_resume_account_move_detached") as detach:
            switches = codex_mesh.retire_workers_servers(target, "codex1", dry_run=True)
        after = {path.name: path.read_bytes() for path in self.session_dir.iterdir()
                 if path.is_file() and path.name in before}
        self.assertEqual(after, before)
        self.assertTrue(switches[0].workers[0].mid_turn)
        self.assertEqual(self.methods("turn/interrupt"), [])
        self.assertEqual(new.requests, [])
        self.assertEqual(self.live_turn, "turn-1")
        self.assertFalse((self.session_dir / "worker.account-move.json").exists())
        self.assertFalse((self.session_dir / codex_mesh.RETIRED_FILE).exists())
        detach.assert_not_called()

    def test_interrupted_turn_without_account_move_intent_keeps_failure_handling(self) -> None:
        respond = self.server.respond
        def interrupted(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "turn/start":
                return [_reply(request, {"turn": {"id": "turn-1"}}), _notice("turn/completed", {
                    "threadId": THREAD_ID, "turn": {"id": "turn-1", "status": "interrupted"}})]
            return respond(request)
        self.server.respond = interrupted
        code, errors = self.run_start()
        self.assertNotEqual(code, 0)
        self.assertIn("turn failed", errors)
        self.assertEqual(len(self.methods("turn/start")), 1)
        self.assertEqual(self.worker_record()["status"], "failed")
        self.assertFalse((self.session_dir / "worker.account-move.json").exists())
        board = self.session_dir / "board.log"
        self.assertFalse(board.exists() and "account moved" in board.read_text())

    def test_automatic_continuation_after_a_role_change_starts_once_on_the_new_home(self) -> None:
        target, new = self.prepare_workers_move()
        old_response = self.server.respond
        def respond(request: dict[str, object]) -> list[dict[str, object]] | None:
            frames = old_response(request)
            if request.get("method") == "turn/start":
                self.assertEqual(accounts.main(["set-role", "workers", "codex1"]), 0)
            return frames
        self.server.respond = respond
        self.outcomes = ["overflow", "completed"]
        code, errors = self.run_start()
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(len(self.methods("turn/start")), 1)
        starts = [request for request in new.requests if request.get("method") == "turn/start"]
        self.assertEqual(len(starts), 1)
        self.assertIn("Continue where you stopped", str(starts[0]))
        record = cast(dict[str, object], json.loads((self.session_dir / codex_mesh.SERVER_FILE).read_text()))
        self.assertEqual(record["port"], new.port)
        self.assertEqual(agent_accounts.workers_home(), target)

    def assert_quota_continuation_after_move(self, held: tuple[str, ...]) -> None:
        # This checks the handoff's ordering. Its many socket/file round trips must
        # not race a five-second task deadline when the machine is busy.
        self.start_args_timeout = 30.0
        target, new = self.prepare_workers_move()
        respond = self.server.respond
        def refuse(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "turn/start":
                return self.quota_completion(request)
            return respond(request)
        self.server.respond = refuse
        def move_on_refusal(*_arguments: object, **_options: object) -> None:
            requested: moves.RequestedMove = {
                "kind": "requested", "at": "2026-10-13T12:00:00+00:00", "tool": "codex",
                "subject": "workers", "move_id": "refused-turn-move", "from": "codex9",
                "to": "codex1", "why": "quota refusal", "window": "unknown",
            }
            moves.append_move(requested)
            self.assertEqual(accounts.main(["set-role", "workers", "codex1"]), 0)
            if held:
                _ = (self.session_dir / "worker.pending.json").write_text(json.dumps({
                    "thread_id": THREAD_ID, "messages": list(held), "accepting_messages": True,
                }))
        args = self.start_args()
        with patch.object(codex_mesh, "_report_quota_refusal", side_effect=move_on_refusal) as report:
            outcome = codex_mesh._run_worker(args, self.server.port, started_here=True)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(outcome, codex_mesh.RunCompleted)
        report.assert_called_once()
        self.assertIsInstance(reported_stopped_context(report),
                              codex_mesh.RefusalWithoutCommandExit)
        starts = [request for request in new.requests if request.get("method") == "turn/start"]
        self.assertEqual(len(self.methods("turn/start")), 1)
        self.assertEqual(len(starts), 1)
        params = cast(dict[str, object], starts[0]["params"])
        inputs = cast(list[dict[str, object]], params["input"])
        self.assertEqual([item["text"] for item in inputs], ["Continue where you stopped.", *held])
        self.assertEqual(agent_accounts.workers_home(), target)
        confirmed = [entry for entry in moves.read_moves() if entry["kind"] == "confirmed"]
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]["move_id"], "refused-turn-move")
        self.assertEqual(confirmed[0]["subject"], "workers")
        assert "worker" in confirmed[0] and "home" in confirmed[0] and "run" in confirmed[0]
        self.assertEqual(confirmed[0].get("worker"), "worker")
        self.assertEqual(confirmed[0]["run"], str(self.session_dir))
        self.assertEqual(confirmed[0]["home"], str(target))

    def test_worker_move_reads_old_request_and_deduplicates_late_old_confirmation(self) -> None:
        target, destination = self.prepare_workers_move()
        source_home = target.parent / ".codex"
        source = codex_mesh.ServingCodexServer(self.server.port, os.getpid(), source_home,
                                               agent_accounts.codex_account(source_home))
        journal = moves.store_file()
        journal.parent.mkdir(parents=True, exist_ok=True)
        request: LegacyWorkerMoveRequest = {"kind": "requested", "at": "2026-10-13T12:00:00Z", "tool": "codex",
                   "subject": "seats", "move_id": "old-worker-move", "from": "codex9",
                   "to": "codex1", "why": "quota refusal", "window": "unknown"}
        _ = journal.write_text(json.dumps(request) + "\n")
        matched = codex_mesh._requested_worker_move(source, target)  # pyright: ignore[reportPrivateUsage]
        self.assertNotIsInstance(matched, codex_mesh.NoRequestedWorkerMove)
        confirmation: LegacyWorkerMoveConfirmation = {"kind": "confirmed", "at": "2026-10-13T12:01:00Z", "tool": "codex",
                        "subject": "seats", "move_id": "old-worker-move", "home": str(target),
                        "seat": "worker", "run": str(self.session_dir.resolve())}
        with journal.open("a") as old_process:
            _ = old_process.write(json.dumps(confirmation) + "\n")
        serving = codex_mesh.ServingCodexServer(destination.port, os.getpid(), target,
                                                agent_accounts.codex_account(target))
        codex_mesh._confirm_worker_move(str(self.session_dir), "worker", matched, serving)  # pyright: ignore[reportPrivateUsage]
        confirmed = [entry for entry in moves.read_moves() if entry["kind"] == "confirmed"]
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]["move_id"], "old-worker-move")
        self.assertEqual(confirmed[0]["subject"], "workers")
        self.assertEqual(confirmed[0]["at"], confirmation["at"])
        self.assertEqual(confirmed[0].get("worker"), "worker")
        self.assertEqual(confirmed[0].get("run"), str(self.session_dir.resolve()))
        self.assertNotIn('"seat"', journal.read_text())
        self.assertNotIn('"seats"', journal.read_text())

    def request_workers_move(self, source: str, target: str, move_id: str) -> None:
        request: moves.RequestedMove = {
            "kind": "requested", "at": datetime.now(timezone.utc).isoformat(), "tool": "codex",
            "subject": "workers", "move_id": move_id, "from": source, "to": target,
            "why": "quota refusal", "window": "unknown",
        }
        moves.append_move(request)

    def assert_destination_refusal_continues(self, *, initial_submission_refused: bool) -> None:
        # Real socket servers, two moves and two one-second retry waits need more than the default deadline.
        self.start_args_timeout = 30.0
        target, rejected = self.prepare_workers_move()
        final_home = target.parent / ".codex2"
        final_home.mkdir()
        login = "recovered@example.invalid"
        claims = base64.urlsafe_b64encode(json.dumps({"email": login}).encode()).decode().rstrip("=")
        _ = (final_home / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"header.{claims}.signature"}}))
        _ = (final_home.parent / "codex2.md").write_text(f"---\nlogin: {login}\n---\n")
        recovered = StubAppServer(self.respond)
        _ = self.patches.callback(recovered.close)
        respond = self.server.respond
        def refuse_initial(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "turn/start":
                if initial_submission_refused:
                    return [{"id": request["id"], "error": {"message": USAGE_LIMIT}}]
                return self.quota_completion(request)
            return respond(request)
        self.server.respond = refuse_initial
        destination_respond = rejected.respond
        def refuse_submission(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "turn/start":
                return [{"id": request["id"], "error": {"message": USAGE_LIMIT}}]
            return destination_respond(request)
        rejected.respond = refuse_submission
        def start(session_dir: str, home: Path) -> int:
            server = rejected if home == target else recovered
            _ = (Path(session_dir) / codex_mesh.SERVER_FILE).write_text(json.dumps({
                "port": server.port, "pid": os.getpid(), "codex_home": str(home), "auto_compact_tokens": codex_mesh.AUTO_COMPACT_TOKENS,
            }))
            return server.port
        def move_on_refusal(_name: str, serving: codex_mesh.CodexServingState, _detail: str, *,
                            session_dir: str, thread_id: str,
                            stopped: codex_mesh.RefusalCommandContext = codex_mesh.REFUSAL_WITHOUT_COMMAND_EXIT) -> None:
            self.assertEqual(session_dir, str(self.session_dir))
            self.assertTrue(thread_id)
            self.assertIsInstance(stopped, codex_mesh.RefusalWithoutCommandExit)
            next_label = "codex1" if serving.home != target else "codex2"
            self.request_workers_move(agent_accounts.codex_home_role_label(serving.home), next_label,
                                   "move-to-" + next_label)
            # The role file is the temporary boundary; provider and account checks belong to blocked's tests.
            config = accounts.accounts_config_file()
            config.parent.mkdir(parents=True, exist_ok=True)
            _ = config.write_text(json.dumps({"workers": next_label}))
        with (patch.object(codex_mesh, "_start_server", side_effect=start),
              patch.object(codex_mesh, "_report_quota_refusal", side_effect=move_on_refusal) as report):
            outcome = codex_mesh._run_worker(self.start_args(), self.server.port, started_here=True)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(outcome, codex_mesh.RunCompleted)
        self.assertEqual(report.call_count, 2)
        self.assertTrue(all(isinstance(call.kwargs.get("stopped", codex_mesh.REFUSAL_WITHOUT_COMMAND_EXIT),
                                       codex_mesh.RefusalWithoutCommandExit) for call in report.call_args_list))
        final_starts = [request for request in recovered.requests if request.get("method") == "turn/start"]
        self.assertEqual(len(final_starts), 1)
        inputs = cast(list[dict[str, object]], cast(dict[str, object], final_starts[0]["params"])["input"])
        self.assertEqual([item["text"] for item in inputs], ["Continue where you stopped."])
        self.assertFalse(self.worker_record().get("continuation_owed"))
        self.assertEqual(agent_accounts.workers_home(), final_home)

    def test_destination_submission_refusal_continues_once_after_the_following_move(self) -> None:
        self.assert_destination_refusal_continues(initial_submission_refused=False)

    def test_initial_submission_refusal_and_destination_refusal_continue_once_on_final_home(self) -> None:
        self.assert_destination_refusal_continues(initial_submission_refused=True)

    def test_new_worker_on_requested_destination_does_not_confirm_another_workers_move(self) -> None:
        _target, new = self.prepare_workers_move()
        self.request_workers_move("codex9", "codex1", "another-worker-move")
        self.assertEqual(accounts.main(["set-role", "workers", "codex1"]), 0)
        self.assertEqual(self.run_start()[0], 0)
        self.assertTrue(any(request.get("method") == "turn/start" for request in new.requests))
        self.assertEqual([entry for entry in moves.read_moves() if entry["kind"] == "confirmed"], [])

    def test_resumed_worker_confirms_only_a_request_from_its_actual_source_home(self) -> None:
        _target, new = self.prepare_workers_move()
        self.request_workers_move("codex2", "codex1", "different-source-move")
        self.assertEqual(accounts.main(["set-role", "workers", "codex1"]), 0)
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "done"}})
        client = codex_mesh.Client(self.server.port, "worker")
        try:
            result = codex_mesh._move_resident(self.start_args(), str(self.session_dir), "worker", THREAD_ID, client)  # pyright: ignore[reportPrivateUsage]
            self.assertIsInstance(result, codex_mesh.MovedWorker)
            assert isinstance(result, codex_mesh.MovedWorker)
            result.client.close()
        finally:
            client.close()
        self.assertTrue(any(request.get("method") == "thread/resume" for request in new.requests))
        self.assertEqual([entry for entry in moves.read_moves() if entry["kind"] == "confirmed"], [])

    def test_resumed_worker_confirms_a_pending_spaced_label_request(self) -> None:
        target, new = self.prepare_workers_move()
        source = target.parent / ".codex"
        source.mkdir(exist_ok=True)
        login = "source@example.invalid"
        claims = base64.urlsafe_b64encode(json.dumps({"email": login}).encode()).decode().rstrip("=")
        _ = (source / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"header.{claims}.signature"}}))
        _ = (target.parent / "codex2.md").write_text(f"---\nlogin: {login}\n---\n")
        self.request_workers_move("codex 2", "codex 1", "spaced-pending-move")
        journal = moves.store_file()
        legacy = journal.read_bytes().replace(b"codex2", b"codex 2").replace(b"codex1", b"codex 1")
        _ = journal.write_bytes(legacy)
        self.assertEqual(accounts.main(["set-role", "workers", "codex1"]), 0)
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "done"}})
        client = codex_mesh.Client(self.server.port, "worker")
        try:
            result = codex_mesh._move_resident(self.start_args(), str(self.session_dir), "worker", THREAD_ID, client)  # pyright: ignore[reportPrivateUsage]
            self.assertIsInstance(result, codex_mesh.MovedWorker)
            assert isinstance(result, codex_mesh.MovedWorker)
            result.client.close()
        finally:
            client.close()
        self.assertTrue(any(request.get("method") == "thread/resume" for request in new.requests))
        confirmations = [entry for entry in moves.read_moves() if entry["kind"] == "confirmed"]
        self.assertEqual(len(confirmations), 1)
        self.assertEqual(confirmations[0]["move_id"], "spaced-pending-move")
        self.assertEqual(confirmations[0].get("home"), str(target))
        self.assertEqual(journal.read_bytes()[:len(legacy)], legacy)

    def test_resumed_worker_confirms_a_pending_account_move_among_work_routing_entries(self) -> None:
        target, new = self.prepare_workers_move()
        stamp = datetime.now(timezone.utc).isoformat()
        moves.append_routing(moves.RoutedToClaude(kind="routed to claude", at=stamp, tool="codex",
            fallback_at=stamp, owner="codex1", cause="refusal", why="Codex refused work for quota", moved=["direct"]))
        self.request_workers_move("codex9", "codex1", "pending-among-routing")
        moves.append_routing(moves.RoutedBack(kind="routed back", at=stamp, tool="codex",
            fallback_at=stamp, owner="codex1", restored=["direct"], kept=[]))
        before = moves.store_file().read_bytes()
        self.assertEqual(accounts.main(["set-role", "workers", "codex1"]), 0)
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "done"}})
        client = codex_mesh.Client(self.server.port, "worker")
        try:
            result = codex_mesh._move_resident(self.start_args(), str(self.session_dir), "worker", THREAD_ID, client)  # pyright: ignore[reportPrivateUsage]
            self.assertIsInstance(result, codex_mesh.MovedWorker)
            assert isinstance(result, codex_mesh.MovedWorker)
            result.client.close()
        finally:
            client.close()
        self.assertTrue(any(request.get("method") == "thread/resume" for request in new.requests))
        confirmed = [entry for entry in moves.read_moves() if entry["kind"] == "confirmed"]
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]["move_id"], "pending-among-routing")
        self.assertEqual(confirmed[0].get("home"), str(target))
        self.assertEqual(moves.store_file().read_bytes()[:len(before)], before)

    def assert_legacy_default_move_confirmed(self, *, default_destination: bool) -> None:
        numbered, new = self.prepare_workers_move()
        default = numbered.parent / ".codex"
        default.mkdir(exist_ok=True)
        login = "home@example.invalid"
        claims = base64.urlsafe_b64encode(json.dumps({"email": login}).encode()).decode().rstrip("=")
        _ = (default / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"header.{claims}.signature"}}))
        _ = (numbered.parent / "codex2.md").write_text(f"---\nlogin: {login}\n---\n")
        source, destination = (numbered, default) if default_destination else (default, numbered)
        source_label, destination_label = ("codex1", "default") if default_destination else ("default", "codex1")
        _ = (self.session_dir / codex_mesh.SERVER_FILE).write_text(json.dumps({
            "port": self.server.port, "pid": os.getpid(), "codex_home": str(source), "auto_compact_tokens": codex_mesh.AUTO_COMPACT_TOKENS,
        }))
        evidence = accounts.roles_history_file()
        evidence.parent.mkdir(parents=True, exist_ok=True)
        _ = evidence.write_text(json.dumps({"at": "2026-10-01T00:00:00Z", "role": "workers",
                                           "label": "default", "account_label": "codex2"}) + "\n")
        journal = moves.store_file()
        # Seed the old spelling directly; new writers take numbered labels only.
        legacy = json.dumps({"kind": "requested", "at": "2026-10-13T12:00:00Z", "tool": "codex",
            "subject": "workers", "move_id": "legacy-default-move", "from": source_label,
            "to": destination_label, "why": "quota refusal", "window": "unknown"}).encode() + b"\n"
        _ = journal.write_bytes(legacy)
        self.assertEqual(accounts.main(["set-role", "workers", "codex2" if default_destination else "codex1"]), 0)
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "done"}})
        client = codex_mesh.Client(self.server.port, "worker")
        try:
            result = codex_mesh._move_resident(self.start_args(), str(self.session_dir), "worker", THREAD_ID, client)  # pyright: ignore[reportPrivateUsage]
            self.assertIsInstance(result, codex_mesh.MovedWorker)
            assert isinstance(result, codex_mesh.MovedWorker)
            result.client.close()
        finally:
            client.close()
        self.assertTrue(any(request.get("method") == "thread/resume" for request in new.requests))
        entries = moves.read_moves()
        confirmed = [entry for entry in entries if entry["kind"] == "confirmed"]
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]["move_id"], "legacy-default-move")
        assert "home" in confirmed[0]
        self.assertEqual(confirmed[0]["home"], str(destination))
        requested = [entry for entry in entries if entry["kind"] == "requested"]
        self.assertEqual((requested[0]["from"], requested[0]["to"]),
                         ("codex1", "codex2") if default_destination else ("codex2", "codex1"))
        self.assertEqual(journal.read_bytes()[:len(legacy)], legacy)

    def test_resumed_worker_confirms_a_legacy_default_source_under_its_numbered_label(self) -> None:
        """F004: a pending default-source request survives numbered account display."""
        self.assert_legacy_default_move_confirmed(default_destination=False)

    def test_resumed_worker_confirms_a_legacy_default_destination_under_its_numbered_label(self) -> None:
        """F004: a default destination is matched and confirmed without rewriting history."""
        self.assert_legacy_default_move_confirmed(default_destination=True)

    def test_worker_resume_failure_records_the_current_move_request_and_reason(self) -> None:
        _target, new = self.prepare_workers_move()
        self.request_workers_move("codex9", "codex1", "resume-failure-move")
        self.assertEqual(accounts.main(["set-role", "workers", "codex1"]), 0)
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "done"}})
        respond = new.respond
        def failed_resume(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/start":
                self.request_workers_move("codex1", "codex2", "later-request")
            if request.get("method") in ("thread/resume", "thread/start"):
                return [{"id": request["id"], "error": {"message": "destination cannot restore thread"}}]
            return respond(request)
        new.respond = failed_resume
        client = codex_mesh.Client(self.server.port, "worker")
        try:
            result = codex_mesh._move_resident(self.start_args(), str(self.session_dir), "worker", THREAD_ID, client)  # pyright: ignore[reportPrivateUsage]
        finally:
            client.close()
        self.assertIsInstance(result, codex_mesh.WorkerThreadStateUnknown)
        failures = [entry for entry in moves.read_moves() if entry["kind"] == "failed"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["move_id"], "resume-failure-move")
        self.assertIn("destination cannot restore thread", failures[0]["reason"])

    def test_start_on_a_closed_ephemeral_port_fails_before_creating_a_thread(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = cast(tuple[str, int], listener.getsockname())[1]
        outcome = codex_mesh._run_worker(self.start_args(), port)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(outcome, codex_mesh.FailedBeforeThread)
        assert isinstance(outcome, codex_mesh.FailedBeforeThread)
        self.assertIn("refused", outcome.failure.lower())
        self.assertEqual(self.methods("thread/start"), [])

    def test_refused_worker_without_queued_messages_continues_once_on_new_home(self) -> None:
        self.assert_quota_continuation_after_move(())

    def test_refused_worker_continues_before_held_messages_in_order(self) -> None:
        self.assert_quota_continuation_after_move(("first held message", "second held message"))

    def test_refused_worker_without_messages_continues_once_after_wait_reset_passes(self) -> None:
        identity = self.prepare_refusing_account()
        respond = self.server.respond
        refused = [False]
        def refuse_once(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "turn/start" and not refused[0]:
                refused[0] = True
                return self.quota_completion(request)
            return respond(request)
        self.server.respond = refuse_once
        def expired_wait(*_arguments: object, **_options: object) -> None:
            now = datetime.now(timezone.utc)
            reset = now - timedelta(seconds=1)
            refusals.write_refusal(refusals.AccountRefusal("codex", identity.login,
                refusals.KnownWindow("five_hour"), refusals.KnownReset(reset), reset - timedelta(minutes=19)))
            moves.append_move(moves.WaitedMove(kind="waited", at=(reset - timedelta(minutes=19)).isoformat(),
                tool="codex", subject="workers", move_id="refused-turn-wait", window="five_hour", reset=reset.isoformat()))
        with patch.object(codex_mesh, "_report_quota_refusal", side_effect=expired_wait) as report:
            code, _errors = self.run_start()
        self.assertEqual(code, 0)
        report.assert_called_once()
        self.assertIsInstance(reported_stopped_context(report),
                              codex_mesh.RefusalWithoutCommandExit)
        self.assertEqual(len(self.methods("turn/start")), 2)
        self.assertEqual(self.turn_inputs, ["Continue where you stopped."])
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["waited", "wait ended"])
        self.assertEqual({entry["move_id"] for entry in entries}, {"refused-turn-wait"})

    def test_follow_with_an_owed_continuation_and_no_room_returns_without_watching_a_turn(self) -> None:
        _ = self.prepare_refusing_account()
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "failed",
                                          "continuation_owed": True, "port": self.server.port}})
        pending = self.session_dir / "worker.pending.json"
        _ = pending.write_text(json.dumps({"thread_id": THREAD_ID, "messages": ["Keep this result"]}))
        self.start_args_timeout = 0.2
        with (patch.object(refusals, "load", return_value={}),
              patch.object(refusals, "stored_reading", return_value=refusals.NoReading()),
              patch.object(codex_mesh, "_stream_frames", side_effect=AssertionError("no turn to watch")),
              patch.object(codex_mesh, "time", SimpleNamespace(time=time.time, monotonic=time.monotonic,
                  sleep=fixture_no_sleep))):
            code, errors = self.run_follow()
        self.assertEqual(code, 1, errors)
        line = "worker waits for room on codex2: no fresh reading shows room on the account; the continuation stays owed"
        self.assertIn(line, errors)
        log = (self.session_dir / "worker.log").read_text()
        self.assertEqual(log.count(line), 1)
        self.assertNotIn("no turn/completed within", log + errors)
        self.assertNotIn("state unknown", errors)
        self.assertIs(self.worker_record().get("continuation_owed"), True)
        self.assertEqual(json.loads(pending.read_text()), {"thread_id": THREAD_ID,
            "messages": ["Keep this result"], "accepting_messages": False})
        self.assertEqual(self.methods("turn/start"), [])

    def test_unstored_refusal_ends_one_turn_launch_and_keeps_its_continuation_and_messages(self) -> None:
        _ = self.prepare_refusing_account()
        original = self.respond
        self.server.respond = lambda request: (self.quota_completion(request)
            if request.get("method") == "turn/start" else original(request))
        pending = self.session_dir / "worker.pending.json"

        def hold_result(*_arguments: object, **_options: object) -> None:
            _ = pending.write_text(json.dumps({"thread_id": THREAD_ID, "messages": ["Run finished"]}))

        with (patch.object(refusals, "load", return_value={}),
              patch.object(refusals, "stored_reading", return_value=refusals.NoReading()),
              patch.object(codex_mesh, "_report_quota_refusal", side_effect=hold_result) as report,
              patch.object(codex_mesh, "time", SimpleNamespace(time=time.time, monotonic=time.monotonic,
                  sleep=fixture_no_sleep))):
            code, errors = self.run_start()
        self.assertEqual(code, 1, errors)
        report.assert_called_once()
        self.assertIn("worker waits for room on codex2:", errors)
        self.assertIs(self.worker_record().get("continuation_owed"), True)
        self.assertEqual(json.loads(pending.read_text()), {"thread_id": THREAD_ID,
            "messages": ["Run finished"], "accepting_messages": False})
        self.assertEqual(len(self.methods("turn/start")), 1)

    def test_resident_waits_for_room_without_repeating_its_log_or_ending_its_seat(self) -> None:
        account = self.prepare_refusing_account()
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "running",
            "turn_id": "prior-turn", "launcher_pid": os.getpid(),
            "continuation_owed": True, "port": self.server.port}})
        polls = 0

        @bounded_fixture
        def quiet(_client: codex_mesh.Client, _deadline: float) -> None:
            nonlocal polls
            polls += 1
            self.assertLessEqual(polls, 3, "resident ignored the scripted end marker")
            if polls == 3:
                _ = (self.session_dir / "worker.end").write_text("end")

        args = self.start_args()
        args.resident = True
        with (contextlib.closing(codex_mesh.Client(self.server.port, "worker")) as client,
              patch.object(refusals, "load", return_value={}),
              patch.object(refusals, "stored_reading", return_value=refusals.NoReading()),
              patch.object(codex_mesh.Client, "next_frame", quiet),
              patch.object(codex_mesh, "time", SimpleNamespace(time=time.time, monotonic=time.monotonic,
                  sleep=fixture_no_sleep))):
            client.serving = codex_mesh.ServingCodexServer(self.server.port, os.getpid(), account.home, account)
            outcome = codex_mesh._stream_turn(args, client, self.server.port, str(self.session_dir),  # pyright: ignore[reportPrivateUsage]
                "worker", THREAD_ID, "next work", 5, time.time(), follow=True)
        self.assertIsInstance(outcome, codex_mesh.RunCompleted)
        self.assertEqual(polls, 3)
        self.assertEqual((self.session_dir / "worker.log").read_text().count("worker waits for room on codex2:"), 1)
        self.assertIs(self.worker_record().get("continuation_owed"), True)
        self.assertEqual(self.methods("turn/start"), [])

    def test_room_wait_after_a_destination_refusal_keeps_and_closes_the_new_connection(self) -> None:
        target, destination = self.prepare_workers_move()
        self.next_thread_id = "replacement-waiting-thread"
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "failed",
                                          "continuation_owed": True, "port": self.server.port}})
        self.assertEqual(accounts.main(["set-role", "workers", "codex1"]), 0)
        respond = destination.respond

        def refuse(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") in {"thread/resume", "turn/start"}:
                return [{"id": request["id"], "error": {"message": USAGE_LIMIT}}]
            return respond(request)

        destination.respond = refuse
        closed: list[codex_mesh.Client] = []
        close = codex_mesh.Client.close

        def close_client(client: codex_mesh.Client) -> None:
            closed.append(client)
            close(client)

        source_home = target.parent / ".codex"
        client = codex_mesh.Client(self.server.port, "worker")
        self.addCleanup(client.close)
        client.serving = codex_mesh.ServingCodexServer(self.server.port, os.getpid(), source_home,
                                                       agent_accounts.codex_account(source_home))
        with (patch.object(refusals, "load", return_value={}),
              patch.object(refusals, "stored_reading", return_value=refusals.NoReading()),
              patch.object(codex_mesh, "_report_quota_refusal") as report,
              patch.object(codex_mesh, "time", SimpleNamespace(time=time.time, monotonic=time.monotonic,
                  sleep=bounded_fixture(fixture_skip_sleep))),
              patch.object(codex_mesh.Client, "close", close_client),
              patch.object(codex_mesh, "_end_unwatched_turn", return_value=codex_mesh.RelaunchAllowed()) as cleanup):
            outcome = codex_mesh._stream_turn(self.start_args(), client, self.server.port,  # pyright: ignore[reportPrivateUsage]
                str(self.session_dir), "worker", THREAD_ID, "next work", 5, time.time(), follow=True)
        self.assertIsInstance(outcome, codex_mesh.FailedWithThread)
        assert isinstance(outcome, codex_mesh.FailedWithThread)
        self.assertEqual(outcome.thread_id, self.next_thread_id)
        self.assertIn("worker waits for room on codex1:", outcome.failure)
        self.assertNotIn("waits for room on codex9:", (self.session_dir / "worker.log").read_text())
        report.assert_called_once()
        cleanup.assert_called_once_with(destination.port, self.next_thread_id, self.session_dir / "worker.log")
        self.assertEqual([item.port for item in closed].count(destination.port), 1)
        self.assertIn(client, closed)
        self.assertIs(self.worker_record().get("continuation_owed"), True)
        self.assertEqual(self.worker_record()["status"], "failed")
        pending = cast(dict[str, object], json.loads((self.session_dir / "worker.pending.json").read_text()))
        self.assertEqual(pending["thread_id"], self.next_thread_id)
        self.assertIs(pending["accepting_messages"], False)

    def test_resident_capacity_retry_hands_its_debt_to_one_quota_continuation_after_room_returns(self) -> None:
        account = self.prepare_refusing_account()
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "running",
            "turn_id": "capacity-turn", "launcher_pid": os.getpid(), "port": self.server.port}})
        original = self.server.respond
        rejected = False

        def refuse_once(request: dict[str, object]) -> list[dict[str, object]] | None:
            nonlocal rejected
            if request.get("method") == "turn/start" and not rejected:
                rejected = True
                return [{"id": request["id"], "error": {"message": USAGE_LIMIT}}]
            return original(request)

        self.server.respond = refuse_once
        args = self.start_args()
        args.resident = True
        with (contextlib.closing(codex_mesh.Client(self.server.port, "worker")) as client,
              patch.object(refusals, "load", return_value={}),
              patch.object(refusals, "stored_reading", return_value=refusals.NoReading()) as reading,
              patch.object(codex_mesh, "_report_quota_refusal"),
              patch.object(codex_mesh, "time", SimpleNamespace(time=time.time, monotonic=time.monotonic,
                  sleep=bounded_fixture(fixture_skip_sleep))),
              contextlib.redirect_stdout(io.StringIO())):
            client.session_dir = str(self.session_dir)
            client.serving = codex_mesh.ServingCodexServer(self.server.port, os.getpid(), account.home,
                                                           account, started_here=True)
            stream = codex_mesh.TurnStream(args=args, session_dir=str(self.session_dir), name="worker",
                follow=False, resident=True, timeout=5, opened=time.time(), deadline=time.time() + 5,
                log_path=self.session_dir / "worker.log", summary_path=self.session_dir / "summary.txt",
                reply_path=None, end_marker=self.session_dir / "worker.end",
                director=codex_mesh.UnknownWorkerDirector(), client=client, port=self.server.port,
                thread_id=THREAD_ID, failure_serving=client.serving,
                runs=codex_mesh.DetachedRunWatch(str(self.session_dir), "worker"),
                turn_id="capacity-turn", resume_owed=True)
            log = io.StringIO()
            step = codex_mesh._resume_owed_turn(stream, log, refused_for_capacity=False)  # pyright: ignore[reportPrivateUsage]
            self.assertIs(step, codex_mesh.StreamStep.NEXT_FRAME)
            self.assertIn("worker waits for room on codex2:", log.getvalue())
            self.assertIs(self.worker_record().get("continuation_owed"), True)
            now = datetime.now(timezone.utc)
            room = refusals.ReadingWindow(100, refusals.NoResetKnown(), now)
            reading.return_value = refusals.FreshReading(room, refusals.NoWindow(), "")
            codex_mesh._keep_resident_on_selected_server(stream, log)  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(self.turn_inputs, ["Continue where you stopped."])
            step = codex_mesh._on_turn_end(stream, log, "turn/completed", {  # pyright: ignore[reportPrivateUsage]
                "threadId": THREAD_ID, "turn": {"id": stream.turn_id, "status": "completed"}})
            self.assertIs(step, codex_mesh.StreamStep.NEXT_FRAME)
            codex_mesh._keep_resident_on_selected_server(stream, log)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(len(self.methods("turn/start")), 2, "one refused retry and one quota continuation")
        self.assertEqual(self.turn_inputs, ["Continue where you stopped."])
        self.assertIs(stream.resume_owed, False)
        self.assertIs(self.worker_record().get("continuation_owed"), False)

    def test_owed_continuation_without_a_refusal_starts_on_its_serving_account_with_room(self) -> None:
        account = self.prepare_refusing_account()
        now = datetime.now(timezone.utc)
        window = refusals.ReadingWindow(100, refusals.NoResetKnown(), now)
        for five_hour in (refusals.NoWindow(), window):
            with self.subTest(five_hour=five_hour):
                self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "failed",
                                                  "continuation_owed": True}})
                starts_before = len(self.methods("turn/start"))
                with (contextlib.closing(codex_mesh.Client(self.server.port, "worker")) as client,
                      patch.object(refusals, "load", return_value={}),
                      patch.object(refusals, "stored_reading",
                                   return_value=refusals.FreshReading(window, five_hour, "")) as reading):
                    client.serving = codex_mesh.ServingCodexServer(self.server.port, os.getpid(), account.home, account)
                    result = codex_mesh._move_resident(self.start_args(), str(self.session_dir), "worker", THREAD_ID, client)  # pyright: ignore[reportPrivateUsage]
                self.assertIsInstance(result, codex_mesh.MovedWorker)
                reading.assert_called_once()
                self.assertEqual(reading.call_args.args[:2], ("codex", account.login))
                self.assertEqual(len(self.methods("turn/start")), starts_before + 1)
                start = self.methods("turn/start")[-1]
                params = cast(dict[str, object], start["params"])
                self.assertEqual(params["threadId"], THREAD_ID)
                self.assertEqual(params["input"], [{"type": "text", "text": "Continue where you stopped."}])
                self.assertIs(self.worker_record().get("continuation_owed"), False)
                self.assertEqual(self.methods("thread/resume"), [])

    def test_owed_continuation_without_a_refusal_waits_then_starts_when_a_reading_shows_room(self) -> None:
        account = self.prepare_refusing_account()
        now = datetime.now(timezone.utc)
        room = refusals.ReadingWindow(100, refusals.NoResetKnown(), now)
        empty = refusals.ReadingWindow(0, refusals.NoResetKnown(), now)
        unavailable = (refusals.NoReading(), refusals.StaleReading(),
                       refusals.FreshReading(empty, room, ""), refusals.FreshReading(room, empty, ""))
        for reading in unavailable:
            with self.subTest(reading=reading):
                self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "failed",
                                                  "continuation_owed": True}})
                previous = self.worker_record()
                starts_before = len(self.methods("turn/start"))
                with (contextlib.closing(codex_mesh.Client(self.server.port, "worker")) as client,
                      patch.object(refusals, "load", return_value={}),
                      patch.object(refusals, "stored_reading", return_value=reading) as stored):
                    client.serving = codex_mesh.ServingCodexServer(self.server.port, os.getpid(), account.home, account)
                    result = codex_mesh._move_resident(self.start_args(), str(self.session_dir), "worker", THREAD_ID, client)  # pyright: ignore[reportPrivateUsage]
                    self.assertIsInstance(result, codex_mesh.WorkerWaitingForRoom)
                    self.assertEqual(self.worker_record(), previous)
                    self.assertEqual(len(self.methods("turn/start")), starts_before)
                    stored.return_value = refusals.FreshReading(room, room, "")
                    result = codex_mesh._move_resident(self.start_args(), str(self.session_dir), "worker", THREAD_ID, client)  # pyright: ignore[reportPrivateUsage]
                self.assertIsInstance(result, codex_mesh.MovedWorker)
                self.assertEqual(len(self.methods("turn/start")), starts_before + 1)
                params = cast(dict[str, object], self.methods("turn/start")[-1]["params"])
                self.assertEqual(params["threadId"], THREAD_ID)
                self.assertEqual(params["input"], [{"type": "text", "text": "Continue where you stopped."}])
                self.assertIs(self.worker_record().get("continuation_owed"), False)

    def test_owed_continuation_on_an_unknown_account_stays_unknown(self) -> None:
        account = self.prepare_refusing_account()
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "failed",
                                          "continuation_owed": True}})
        previous = self.worker_record()
        with (contextlib.closing(codex_mesh.Client(self.server.port, "worker")) as client,
              patch.object(refusals, "stored_reading") as reading):
            client.serving = codex_mesh.ServingCodexServer(self.server.port, os.getpid(), account.home,
                agent_accounts.UnknownCodexAccount(account.home, "fixture identity unavailable"))
            result = codex_mesh._move_resident(self.start_args(), str(self.session_dir), "worker", THREAD_ID, client)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(result, codex_mesh.WorkerThreadStateUnknown)
        reading.assert_not_called()
        self.assertEqual(self.worker_record(), previous)
        self.assertEqual(self.methods("turn/start"), [])

    def test_capacity_retry_after_a_role_change_starts_once_on_the_new_home(self) -> None:
        _, new = self.prepare_workers_move()
        @bounded_fixture
        def wait(seconds: float) -> None:
            self.wait(seconds)
            self.assertEqual(accounts.main(["set-role", "workers", "codex1"]), 0)
        self.outcomes = ["capacity", "completed"]
        with patch.object(codex_mesh, "_capacity_sleep", side_effect=wait):
            code, errors = self.run_start()
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(len(self.methods("turn/start")), 1)
        starts = [request for request in new.requests if request.get("method") == "turn/start"]
        self.assertEqual(len(starts), 1)
        self.assertIn("capacity", str(starts[0]).lower())
        record = cast(dict[str, object], json.loads((self.session_dir / codex_mesh.SERVER_FILE).read_text()))
        self.assertEqual(record["port"], new.port)

    def test_checked_initial_turn_waits_for_a_live_thread_then_starts_once(self) -> None:
        self.live_turn = "outgoing-turn"
        self.announce_live_completion = True
        code, errors = self.run_start()
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(len(self.methods("turn/start")), 1)
        self.assertGreaterEqual(len(self.methods("thread/read")), 2)
        self.assertEqual(self.live_turn, "")
        self.assertEqual(self.worker_record()["status"], "done")

    def test_initial_turn_waits_for_an_event_before_retrying_live_state(self) -> None:
        with contextlib.closing(codex_mesh.Client(self.server.port, "worker")) as client:
            moved = codex_mesh.MovedWorker(client, self.server.port, "next-turn", THREAD_ID, False)

            @bounded_fixture
            def wait_for_event(events: codex_mesh.FollowEvents, deadline: float) -> None:
                self.assertIsInstance(events, codex_mesh.FollowEvents)
                self.assertAlmostEqual(deadline - time.monotonic(), 60, delta=1,
                                       msg="live-turn retry retained a short polling timeout")

            with patch.object(codex_mesh, "_move_resident", side_effect=[codex_mesh.WorkerWaitingOnLiveTurn(), moved]) as move, \
                    patch.object(client, "wait_for_activity", side_effect=wait_for_event) as waiting, \
                    patch.object(codex_mesh, "_continuation_is_owed", return_value=False):
                result = codex_mesh._start_workers_turn(  # pyright: ignore[reportPrivateUsage]
                    self.start_args(), str(self.session_dir), "worker", THREAD_ID, client, "next work")
                self.assertIs(result, moved)
                self.assertEqual(move.call_count, 2)
                waiting.assert_called_once()
                self.assertIs(client.wake, codex_mesh.SOCKET_ONLY_WAIT)

    def test_message_for_retired_live_server_is_held_and_steer_stays_on_live_turn(self) -> None:
        _, new = self.prepare_workers_move()
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({"worker": {
            "thread_id": THREAD_ID, "turn_id": "live-turn", "status": "running",
            "launcher_pid": os.getpid(), "port": self.server.port,
        }}))
        self.live_turn = "live-turn"
        self.assertEqual(accounts.main(["set-role", "workers", "codex1"]), 0)
        args = argparse.Namespace(session_dir=str(self.session_dir), to="worker", message="peer message", message_file="")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(codex_mesh.command_send(args), 0)
        self.assertEqual(self.methods("thread/queue/add"), [])
        self.assertEqual(self.methods("turn/start"), [])
        self.assertEqual(new.requests, [])
        pending = (self.session_dir / "worker.pending.json").read_text()
        self.assertIn("peer message", pending)
        old_response = self.server.respond
        def respond(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "turn/steer":
                return [_reply(request, {})]
            return old_response(request)
        self.server.respond = respond
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(codex_mesh.command_steer(args), 0)
        self.assertEqual(len(self.methods("turn/steer")), 1)
        self.assertEqual(new.requests, [])
        self.assertEqual(self.methods("turn/start"), [])
        self.live_turn = ""
        client = codex_mesh.Client(self.server.port, "worker")
        moved = codex_mesh._move_resident(self.start_args(), str(self.session_dir), "worker", THREAD_ID, client)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(moved, codex_mesh.MovedWorker)
        if not isinstance(moved, codex_mesh.MovedWorker):
            self.fail("An idle worker must move its held message")
        moved.client.close()
        starts = [request for request in new.requests if request.get("method") == "turn/start"]
        self.assertEqual(len(starts), 1)
        self.assertIn("peer message", str(starts[0]))
        self.assertNotIn("peer message", (self.session_dir / "worker.pending.json").read_text())

    def test_continuation_user_message_does_not_clear_overflow_guard(self) -> None:
        self.outcomes = ["overflow", "overflow"]

        code, errors = self.run_start()

        self.assertEqual(code, 1)
        self.assertEqual(
            self.turn_inputs,
            ["Original worker task", "Continue where you stopped."],
        )
        self.assertIn("context window filled twice in a row without progress", errors)
        board = (self.session_dir / "board.log").read_text(encoding="utf-8")
        self.assertEqual(board.count("context window filled; sent"), 1)

    def test_later_overflow_after_progress_gets_one_more_continue_turn(self) -> None:
        self.outcomes = ["overflow", "progress_overflow", "completed"]

        code, errors = self.run_start()

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(
            self.turn_inputs,
            [
                "Original worker task",
                "Continue where you stopped.",
                "Continue where you stopped.",
            ],
        )
        board = (self.session_dir / "board.log").read_text(encoding="utf-8")
        self.assertEqual(board.count("context window filled; sent"), 2)
        self.assertEqual(len(self.methods("turn/start")), 3)
        self.assertEqual(self.worker_record()["status"], "done")

    def test_follow_refuses_a_running_worker_before_sending(self) -> None:
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "worker": {"thread_id": THREAD_ID, "turn_id": "live-turn", "status": "running"}
        }), encoding="utf-8")
        code, errors = self.run_follow()
        self.assertEqual(code, 2, errors)
        self.assertEqual(self.methods("thread/resume"), [])
        self.assertEqual(self.methods("turn/start"), [])

    def test_follow_refuses_failed_worker_with_live_turn(self) -> None:
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "worker": {"thread_id": THREAD_ID, "turn_id": "", "status": "failed"}
        }), encoding="utf-8")
        self.live_turn = "peer-turn"
        code, errors = self.run_follow()
        self.assertEqual(code, 2, errors)
        self.assertEqual(self.methods("thread/resume"), [])
        self.assertEqual(self.methods("turn/start"), [])

    def test_follow_accepts_failed_worker_only_after_idle_read(self) -> None:
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "worker": {"thread_id": THREAD_ID, "turn_id": "", "status": "failed"}
        }), encoding="utf-8")

        code, errors = self.run_follow()

        self.assertEqual((code, errors), (0, ""))
        self.assertTrue(self.methods("thread/read"))
        self.assertEqual(len(self.methods("thread/resume")), 1)
        self.assertEqual(len(self.methods("turn/start")), 1)
        self.assertEqual(self.worker_record()["status"], "done")

    def test_earlier_completion_cannot_finish_follow_turn(self) -> None:
        code, errors = self.run_start()
        self.assertEqual((code, errors), (0, ""))
        self.outcomes = ["earlier_completed"]
        code, errors = self.run_follow()
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(len(self.methods("turn/start")), 2)
        self.assertEqual(self.worker_record()["status"], "done")

    def test_earlier_completion_alone_does_not_finish_follow_turn(self) -> None:
        code, errors = self.run_start()
        self.assertEqual((code, errors), (0, ""))
        self.outcomes = ["earlier_only"]
        self.start_args_timeout = 0.1

        code, errors = self.run_follow()

        self.assertNotEqual(code, 0, errors)
        self.assertEqual(len(self.methods("turn/start")), 2)

    def test_follow_sees_a_peer_turn_end_when_the_thread_read_still_names_the_followed_turn(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        self.outcomes = ["completed_then_instant_peer"]
        self.start_args_timeout = 3

        # The interval wake only makes the stand-in server hand over the peer turn's end.
        with patch.object(codex_mesh, "UNSEEN_END_CHECK_SECS", 0.2):
            code, errors = self.run_follow()

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.worker_record()["status"], "done")
        self.assertEqual(self.methods("turn/interrupt"), [])

    def test_a_launcher_finishes_when_the_thread_is_idle_and_no_completion_notice_came(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        self.outcomes = ["answered_without_completion"]
        self.start_args_timeout = 5

        with patch.object(codex_mesh, "UNSEEN_END_CHECK_SECS", 0.2):
            code, errors = self.run_follow()

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.worker_record()["status"], "done")
        self.assertIn("late answer", (self.session_dir / "summary.txt").read_text(encoding="utf-8"))
        self.assertIn("thread is idle and no completion notice arrived",
                      (self.session_dir / "worker.log").read_text(encoding="utf-8"))

    def test_an_idle_read_that_does_not_list_the_turn_as_ended_does_not_end_it(self) -> None:
        # The server can read a thread idle just after its turn starts, before it lists the turn.
        self.assertEqual(self.run_start()[0], 0)
        self.outcomes = ["idle_before_listed"]
        self.start_args_timeout = 5

        with patch.object(codex_mesh, "UNSEEN_END_CHECK_SECS", 0.05):
            code, errors = self.run_follow()

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.unlisted_reads, 2)
        self.assertEqual(self.methods("turn/interrupt"), [])
        self.assertEqual(self.worker_record()["status"], "done")
        self.assertIn("listed late", (self.session_dir / "summary.txt").read_text(encoding="utf-8"))

    def test_an_idle_thread_with_no_notice_and_no_answer_finishes_as_failed(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        self.outcomes = ["earlier_only"]
        self.start_args_timeout = 5

        with patch.object(codex_mesh, "UNSEEN_END_CHECK_SECS", 0.2):
            code, errors = self.run_follow()

        self.assertNotEqual(code, 0)
        self.assertIn("went idle with no completion notice and no answer", errors)
        self.assertEqual(self.worker_record()["status"], "failed")

    def assert_system_error_delivers_a_finished_run_result(self, *, follow: bool, room_wait: bool = False) -> None:
        account = self.prepare_refusing_account()
        if follow:
            self.assertEqual(self.run_start()[0], 0)
        stalled_turn = self.turn_starts + 1
        self.outcomes = ["disconnect", "completed"]
        self.start_args_timeout = 180
        runs = self.session_dir / codex_mesh.DETACHED_RUNS_DIR
        runs.mkdir()
        run = runs / "scratch-run.json"
        _ = run.write_text(json.dumps({"worker": "worker", "status": "running", "pid": os.getpid()}))
        result = "Run finished: scratch verification passed"
        now = time.time()
        elapsed = 0.0
        quiet_polls = 0
        read = codex_mesh.Client.next_frame
        call = codex_mesh.Client.call
        progress = FixtureLoopBudget("finished-run recovery frames", seconds=15, max_calls=64)
        requests = FixtureLoopBudget("finished-run recovery RPCs", seconds=15, max_calls=128)
        started = time.perf_counter()

        @bounded_fixture
        def rpc(client: codex_mesh.Client, method: str, params: dict[str, object],
                timeout: float = codex_mesh.CALL_TIMEOUT_SECS) -> codex_mesh.RpcMessage:
            remaining = requests.check()
            return call(client, method, params, timeout=min(timeout, remaining))

        @bounded_fixture
        def monotonic() -> float:
            # Fake time jumps over the scan interval; real time can still expire it.
            return elapsed + time.perf_counter() - started

        @bounded_fixture
        def wall_clock() -> float:
            return now + monotonic()

        @bounded_fixture
        def frame(client: codex_mesh.Client, deadline: float) -> codex_mesh.RpcMessage | None:
            nonlocal elapsed, quiet_polls
            remaining = progress.check()
            deadline = min(deadline, wall_clock() + remaining)
            # Readback RPCs can buffer notices before their reply. Never hide them.
            if client.has_pending_frames:
                return read(client, deadline)
            if self.turn_starts == stalled_turn and quiet_polls < 2:
                quiet_polls += 1
                elapsed += 1 if quiet_polls == 1 else 59
                self.error_threads.add(THREAD_ID)
                if quiet_polls == 2:
                    # The first quiet poll must keep the launcher for the live run.
                    self.assertEqual(self.methods("thread/read")[-1]["params"], {
                        "threadId": THREAD_ID, "includeTurns": False,
                    })
                    _ = run.write_text(json.dumps({"worker": "worker", "status": "finished", "pid": os.getpid()}))
                    if room_wait:
                        self.save_unresolved_refusal(account)
                        worker = self.worker_record()
                        worker["continuation_owed"] = True
                        self.write_pool_roster({"worker": worker})
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(codex_mesh.command_send(argparse.Namespace(
                            session_dir=str(self.session_dir), to="worker", message=result, message_file="")), 0)
                return None
            return read(client, deadline)

        with (patch.object(codex_mesh.Client, "next_frame", frame),
              patch.object(codex_mesh.Client, "call", rpc),
              patch.object(codex_mesh, "time", SimpleNamespace(time=wall_clock,
                  monotonic=monotonic, sleep=fixture_no_sleep))):
            code, errors = self.run_follow() if follow else self.run_start()

        self.assertEqual(quiet_polls, 2)
        self.assertLess(elapsed, self.start_args_timeout / 2)
        pending = cast(dict[str, object], json.loads((self.session_dir / "worker.pending.json").read_text()))
        if room_wait:
            line = ("worker waits for room on codex2: the refused account has not recovered; "
                    "the continuation stays owed")
            self.assertEqual(code, 1, errors)
            self.assertIn(line, errors)
            log = (self.session_dir / "worker.log").read_text()
            self.assertEqual(log.count(line), 1)
            self.assertNotIn("no turn/completed within", log + errors)
            self.assertIs(self.worker_record().get("continuation_owed"), True)
            self.assertEqual(self.turn_starts, stalled_turn)
            self.assertEqual(pending["messages"], [result])
            self.assertIs(pending["accepting_messages"], False)
            return
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.turn_starts, stalled_turn + 1)
        self.assertEqual(self.turn_inputs[-1], result)
        self.assertEqual(self.worker_record()["status"], "done")
        self.assertEqual(pending["messages"], [])

    def test_one_turn_launcher_delivers_a_finished_run_result_after_a_system_error(self) -> None:
        self.assert_system_error_delivers_a_finished_run_result(follow=False)

    def test_follow_delivers_a_finished_run_result_after_a_system_error(self) -> None:
        self.assert_system_error_delivers_a_finished_run_result(follow=True)

    def test_one_turn_launcher_keeps_a_finished_run_result_while_the_refused_account_has_no_room(self) -> None:
        self.assert_system_error_delivers_a_finished_run_result(follow=False, room_wait=True)

    def test_follow_keeps_a_finished_run_result_while_the_refused_account_has_no_room(self) -> None:
        self.assert_system_error_delivers_a_finished_run_result(follow=True, room_wait=True)

    def test_a_thread_with_a_queued_message_or_a_live_turn_has_not_ended(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        client = codex_mesh.Client(self.server.port, "probe")
        try:
            ended = codex_mesh._ended_unseen  # pyright: ignore[reportPrivateUsage]
            self.assertTrue(ended(client, THREAD_ID))
            # Given the watched turn, an idle thread must also list that turn as ended.
            self.assertFalse(ended(client, THREAD_ID, "turn-9"))
            self.silent_ended = [{"id": "turn-9", "status": "inProgress"}]
            self.assertFalse(ended(client, THREAD_ID, "turn-9"))
            self.silent_ended = [{"id": "turn-9", "status": "interrupted"}]
            self.assertTrue(ended(client, THREAD_ID, "turn-9"))
            self.silent_ended = []
            self.queued = [{"id": "queued-1", "input": [], "clientUserMessageId": "m1"}]
            self.assertFalse(ended(client, THREAD_ID))
            self.queued = []
            self.live_turn = "peer-turn"
            self.assertFalse(ended(client, THREAD_ID))
            self.live_turn = ""
            self.read_failure = "error"
            self.assertFalse(ended(client, THREAD_ID))
        finally:
            client.close()

    def test_follow_failure_without_turn_id_finishes_as_failed(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        self.outcomes = ["failed"]
        self.start_args_timeout = 300

        code, errors = self.run_follow()

        self.assertEqual(code, 1)
        self.assertIn("ordinary failure", errors)
        self.assertNotIn("no turn/completed", errors)

    def test_follow_completion_without_turn_id_finishes_as_done(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        self.outcomes = ["completed_without_id"]
        # The socket completion notice advances this test; its deadline only
        # catches a hung launcher, including when the server is starved of CPU.
        self.start_args_timeout = 300
        submission_delay = [0.0]
        respond = self.server.respond

        @bounded_fixture
        def clock() -> float:
            return time.time() + submission_delay[0]

        def submission_uses_cpu(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "turn/start":
                # Model scheduling delay without sleeping or comparing wall time.
                submission_delay[0] += 1
            return respond(request)

        self.server.respond = submission_uses_cpu

        with patch.object(codex_mesh, "time", SimpleNamespace(time=clock, monotonic=time.monotonic, sleep=time.sleep)):
            code, errors = self.run_follow()

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.worker_record()["status"], "done")

    def test_follow_watches_peer_turn_queued_during_its_turn(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        self.outcomes = ["completed_then_peer"]

        code, errors = self.run_follow()

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.live_turn, "")
        self.assertEqual(self.methods("turn/interrupt"), [])
        self.assertEqual(self.worker_record()["status"], "done")

    def test_failed_followed_turn_remains_failed_after_peer_completes(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        self.outcomes = ["failed_with_queued"]

        code, errors = self.run_follow()

        self.assertEqual(code, 1, errors)
        self.assertIn("ordinary failure", errors)
        self.assertEqual(self.live_turn, "")
        self.assertEqual(self.worker_record()["status"], "failed")

    def test_claimed_follow_refuses_another_launcher_before_start(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        args = self.start_args()
        args.to = "worker"
        args.claim_pid = os.getpid()

        self.assertEqual(codex_mesh.command_can_follow(args), 0)
        self.assertEqual(self.worker_record()["status"], "starting")
        self.assertEqual(codex_mesh.command_can_follow(args), 2)
        self.assertEqual(len(self.methods("turn/start")), 1)

    def test_released_follow_claim_restores_the_prior_role_in_roster_and_usage(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        args = self.start_args()
        args.to = "worker"
        args.role = "review"
        args.lens = "adversary"
        args.claim_pid = os.getpid()

        self.assertEqual(codex_mesh.command_can_follow(args), 0)
        self.assertEqual(self.worker_record()["role"], "review")
        self.assertEqual(codex_mesh.command_release_follow(args), 0)

        self.assertEqual(self.worker_record()["status"], "done")
        self.assertEqual(self.worker_record()["role"], "impl")
        self.assertNotIn("previous_role", self.worker_record())
        self.assertEqual([(record["role"], record.get("lens")) for record in self.usage_records()],
                         [("impl", None), ("review", "adversary"), ("impl", None)])

    def test_released_follow_claim_restores_an_absent_role(self) -> None:
        args = self.start_args()
        args.role = ""
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(codex_mesh.command_start(args), 0)
        args.to = "worker"
        args.role = "review"
        args.claim_pid = os.getpid()

        self.assertEqual(codex_mesh.command_can_follow(args), 0)
        self.assertEqual(self.worker_record()["role"], "review")
        self.assertEqual(codex_mesh.command_release_follow(args), 0)

        self.assertEqual(self.worker_record()["status"], "done")
        self.assertNotIn("role", self.worker_record())
        self.assertNotIn("previous_role", self.worker_record())
        self.assertEqual([record["role"] for record in self.usage_records()], ["review"])

    def test_claim_restore_records_prior_role_while_a_peer_turn_remains_running(self) -> None:
        self.assertEqual(self.run_start(), (0, ""))
        args = self.start_args()
        args.to = "worker"
        args.role = "review"
        args.lens = "craft"
        args.claim_pid = os.getpid()
        self.assertEqual(codex_mesh.command_can_follow(args), 0)
        self.live_turn = "peer-turn"
        args.message_file = str(self.session_dir / "follow-up.txt")
        _ = Path(args.message_file).write_text("Review the code", encoding="utf-8")

        with patch.object(codex_mesh, "_wait_for_claimed_peer", return_value=False), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            code = codex_mesh.command_follow(args)

        self.assertEqual(code, 1, errors.getvalue())
        self.assertIn("live turn", errors.getvalue())
        self.assertEqual(self.worker_record()["status"], "running")
        self.assertEqual(self.worker_record()["turn_id"], "peer-turn")
        self.assertEqual(self.worker_record()["role"], "impl")
        self.assertEqual(self.live_turn, "peer-turn")
        self.assertEqual([(record["role"], record.get("lens")) for record in self.usage_records()],
                         [("impl", None), ("review", "craft"), ("impl", None)])
        self.assertEqual(len(self.methods("turn/start")), 1)

    def test_live_turn_after_resume_restores_preclaim_roster_when_peer_finishes(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        args = self.start_args()
        args.to = "worker"
        args.role = "review"
        args.lens = "ux"
        args.claim_pid = os.getpid()
        self.assertEqual(codex_mesh.command_can_follow(args), 0)
        self.live_turn = "peer-turn"
        self.announce_live_completion = True
        args.message_file = str(self.session_dir / "follow-up.txt")
        _ = Path(args.message_file).write_text("Follow up", encoding="utf-8")

        with contextlib.redirect_stderr(io.StringIO()) as errors:
            code = codex_mesh.command_follow(args)

        self.assertEqual(code, 1, errors.getvalue())
        self.assertEqual(self.worker_record()["status"], "done")
        self.assertEqual(self.live_turn, "")
        self.assertEqual([(record["role"], record.get("lens")) for record in self.usage_records()],
                         [("impl", None), ("review", "ux"), ("impl", None)])

    def test_claim_changed_after_dispatch_fails_without_overwriting_peer(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        args = self.start_args()
        args.to = "worker"
        args.claim_pid = os.getpid()
        self.assertEqual(codex_mesh.command_can_follow(args), 0)
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "worker": {"thread_id": THREAD_ID, "turn_id": "peer-turn", "status": "running"}
        }), encoding="utf-8")

        with contextlib.redirect_stderr(io.StringIO()) as errors:
            code = codex_mesh.command_follow(args)

        self.assertEqual(code, 1, errors.getvalue())
        self.assertEqual(self.worker_record()["status"], "running")
        self.assertEqual(len(self.methods("turn/start")), 1)

    def test_follow_recovers_dead_launcher_only_after_thread_is_idle(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "worker": {"thread_id": THREAD_ID, "turn_id": "turn-1",
                     "status": "running", "launcher_pid": 99999999}
        }), encoding="utf-8")
        self.live_turn = "still-running"
        refused, _errors = self.run_follow()
        self.assertEqual(refused, 2)
        self.live_turn = ""

        code, errors = self.run_follow()

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.worker_record()["status"], "done")

    def test_follow_recovers_a_capacity_wait_whose_launcher_died(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "worker": {"thread_id": THREAD_ID, "status": codex_mesh.WAITING_CAPACITY,
                     "launcher_pid": 99999999}
        }), encoding="utf-8")

        code, errors = self.run_follow()

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.worker_record()["status"], "done")

    def test_running_roster_without_turn_id_has_no_active_state(self) -> None:
        state = codex_mesh._roster_thread({  # pyright: ignore[reportPrivateUsage]
            "thread_id": THREAD_ID, "status": "running", "turn_id": ""
        })

        self.assertIsInstance(state, codex_mesh.InvalidThread)

    def test_follow_refuses_running_roster_without_turn_id(self) -> None:
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "worker": {"thread_id": THREAD_ID, "status": "running"}
        }), encoding="utf-8")

        code, errors = self.run_follow()

        self.assertEqual(code, 2, errors)
        self.assertEqual(self.methods("turn/start"), [])

    def test_capacity_resumes_its_thread_without_repeating_the_prompt(self) -> None:
        self.outcomes = ["capacity", "completed"]
        code, errors = self.run_start()
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(len(self.methods("thread/start")), 1)
        starts = self.methods("turn/start")
        self.assertEqual(len(starts), 2)
        first = cast("dict[str, object]", starts[0]["params"])
        resumed = cast("dict[str, object]", starts[1]["params"])
        self.assertEqual(first["threadId"], THREAD_ID)
        self.assertEqual(resumed["threadId"], THREAD_ID)
        self.assertIn("Original worker task", str(first["input"]))
        self.assertNotIn("Original worker task", str(resumed["input"]))
        self.assertIn("capacity", str(resumed["input"]).lower())
        self.assertEqual(self.waits, [30.0])
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)

    def test_structured_capacity_error_resumes_the_same_thread(self) -> None:
        self.outcomes = ["capacity_structured", "completed"]
        code, _errors = self.run_start()
        self.assertEqual(code, 0)
        self.assertEqual(self.waits, [30.0])
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)

    def test_capacity_on_completed_notification_resumes_the_thread(self) -> None:
        self.outcomes = ["capacity_completed", "completed"]
        code, _errors = self.run_start()
        self.assertEqual(code, 0)
        self.assertEqual(self.waits, [30.0])
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)

    def test_other_turn_failure_does_not_use_capacity_backoff(self) -> None:
        self.outcomes = ["failed"]
        code, errors = self.run_start()
        self.assertEqual(code, 1)
        self.assertIn("ordinary failure", errors)
        self.assertNotIn("thread/read failed", errors)
        self.assertEqual(self.waits, [])
        self.assertEqual(len(self.methods("turn/start")), 1)
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)

    def test_capacity_budget_resets_after_a_completed_turn(self) -> None:
        self.outcomes = ["capacity", "completed_then_capacity"] + ["capacity"] * 20
        args = self.start_args()
        args.resident = True

        outcome = codex_mesh._run_worker(args, self.server.port)  # pyright: ignore[reportPrivateUsage]

        self.assertIsInstance(outcome, codex_mesh.CapacityRetriesExhausted)
        self.assertEqual(self.waits, [30.0, 30.0, 60.0, 120.0, 240.0, 300.0, 300.0, 150.0])
        self.assertEqual(sum(self.waits[1:]), 1200.0)
        self.assertEqual(len(self.methods("thread/start")), 1)

    def test_capacity_budget_resets_after_a_peer_turn_completes(self) -> None:
        self.outcomes = ["capacity", "capacity_with_peer"] + ["capacity"] * 20

        code, errors = self.run_start()

        self.assertEqual(code, 1)
        self.assertIn("model still at capacity", errors)
        self.assertIn(THREAD_ID, errors)
        self.assertEqual(self.waits, [30.0, 30.0, 60.0, 120.0, 240.0, 300.0, 300.0, 150.0])
        self.assertEqual(sum(self.waits[1:]), 1200.0)
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)
        self.assertEqual(self.worker_record()["status"], "capacity_exhausted")

    def test_failed_turn_does_not_abandon_a_peers_queued_turn(self) -> None:
        self.outcomes = ["failed_with_queued"]
        code, _errors = self.run_start()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertEqual(len(self.methods("turn/start")), 1)
        self.assertTrue(self.methods("thread/read"))
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)

    def test_capacity_peer_turn_finishes_before_task_resume(self) -> None:
        """An unfinished peer turn cannot consume the task's retry."""
        self.outcomes = ["capacity_with_peer", "completed"]
        code, _errors = self.run_start()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertEqual(len(self.methods("turn/start")), 2)
        self.assertEqual(self.waits, [])
        self.assertNotIn("capacity retry", (self.session_dir / "worker.log").read_text(encoding="utf-8"))
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)

    def test_capacity_backoff_waits_for_peer_notices_from_an_earlier_readback(self) -> None:
        """A lifecycle read can finish the peer before its refusal is dispatched."""
        self.assert_capacity_readback_resumes_after_peer(completion_notice=True)

    def test_capacity_peer_idle_readback_recovers_a_missing_completion_notice(self) -> None:
        self.assert_capacity_readback_resumes_after_peer(completion_notice=False)

    def assert_capacity_readback_resumes_after_peer(self, *, completion_notice: bool) -> None:
        self.outcomes = ["capacity_with_peer", "completed"]
        code, errors = self.run_after_peer_readback(completion_notice=completion_notice)
        self.assertEqual(code, 0, errors)
        self.assertEqual(self.waits, [])
        self.assertEqual(len(self.methods("turn/start")), 2)
        self.assertEqual(self.worker_record()["status"], "done")

    def test_failed_turn_readback_preserves_a_finished_peers_queued_completion(self) -> None:
        self.outcomes = ["failed_with_queued"]
        code, errors = self.run_after_peer_readback(completion_notice=True)
        self.assertEqual(code, 0, errors)
        self.assertEqual(self.waits, [])
        self.assertEqual(len(self.methods("turn/start")), 1)
        self.assertEqual(self.worker_record()["status"], "done")

    def run_after_peer_readback(self, *, completion_notice: bool) -> tuple[int, str]:
        self.start_args_timeout = 3
        self.server.coalesce_response_frames = True
        respond = self.respond

        def peer_readback(request: dict[str, object]) -> list[dict[str, object]] | None:
            frames = respond(request)
            if not completion_notice and request.get("method") == "thread/read" and frames is not None:
                return [frame for frame in frames if frame.get("method") != "turn/completed"]
            return frames

        self.server.respond = peer_readback
        start = codex_mesh._start_streamed_turn  # pyright: ignore[reportPrivateUsage]
        readbacks = 0

        def read_before_dispatch(stream: codex_mesh.TurnStream, prompt: str, log: TextIO) -> codex_mesh.StreamStep:
            nonlocal readbacks
            result = start(stream, prompt, log)
            # Model a file-wake readback that overtakes delivery of the refusal.
            # The active reply finishes the peer and buffers its notices; the
            # capacity handler's next read will already say idle.
            self.assertFalse(codex_mesh._ended_unseen(stream.client, stream.thread_id))  # pyright: ignore[reportPrivateUsage]
            self.assertTrue(stream.client.has_pending_frames)
            readbacks += 1
            return result

        with patch.object(codex_mesh, "_start_streamed_turn", read_before_dispatch):
            code, errors = self.run_start()
        self.assertEqual(readbacks, 1)
        return code, errors

    def test_disconnect_after_turn_start_never_repeats_the_prompt(self) -> None:
        self.outcomes = ["disconnect"]
        self.server.close_after_turn_start = True
        self.start_args_timeout = 0.1
        code, _errors = self.run_start()
        self.assertEqual(code, 1)
        self.assertEqual(len(self.methods("thread/start")), 1)
        starts = self.methods("turn/start")
        self.assertEqual(len(starts), 1)
        self.assertIn("Original worker task", str(starts[0]["params"]))
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)

    def test_capacity_waits_stop_at_twenty_minutes_and_name_the_thread(self) -> None:
        self.outcomes = ["capacity"] * 20
        code, errors = self.run_start()
        self.assertEqual(code, 1)
        self.assertEqual(self.waits[:5], [30.0, 60.0, 120.0, 240.0, 300.0])
        self.assertEqual(sum(self.waits), 1200.0)
        self.assertEqual(
            errors.splitlines()[-1],
            f"codex_mesh: worker: model still at capacity after {len(self.waits)} "
            + f"retries over 20 min; thread {THREAD_ID} stays on the roster "
            + "(codex_mesh.py end --to worker)",
        )
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)
        self.assertEqual(self.worker_record()["status"], "capacity_exhausted")
        log = (self.session_dir / "worker.log").read_text(encoding="utf-8")
        for retry in range(1, len(self.waits) + 1):
            self.assertIn(f"retry {retry}", log)
        self.assertEqual(log.count("next turn at"), len(self.waits))

    def test_waiting_send_joins_resume_input_without_opening_a_turn(self) -> None:
        """Waiting sends stay local until the launcher resumes."""
        self.outcomes = ["capacity_queued", "completed"]
        original_wait = self.wait

        def send_while_waiting(seconds: float) -> None:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(codex_mesh.command_send(argparse.Namespace(
                    session_dir=str(self.session_dir), to="worker",
                    message="Message while waiting", message_file="",
                )), 0)
            self.assertIn("worker", output.getvalue())
            self.assertEqual(self.methods("thread/queue/add"), [])
            self.assertEqual(self.auto_turn_starts, 0)
            original_wait(seconds)

        with patch.object(codex_mesh, "_capacity_sleep", send_while_waiting):
            code, errors = self.run_start()
        self.assertEqual((code, errors), (0, ""))
        resumed = cast("dict[str, object]", self.methods("turn/start")[1]["params"])
        self.assertIn("Continue from where you stopped", str(resumed["input"]))
        self.assertIn("Server queued message", str(resumed["input"]))
        self.assertIn("Message while waiting", str(resumed["input"]))
        self.assertEqual(self.queued, [])

    def test_exhausted_send_refuses_without_opening_a_turn(self) -> None:
        """An exhausted worker has no launcher to watch new work."""
        self.outcomes = ["capacity"] * 20
        code, _errors = self.run_start()
        self.assertEqual(code, 1)
        pending = self.session_dir / "worker.pending.json"
        before = pending.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            sent = codex_mesh.command_send(argparse.Namespace(
                session_dir=str(self.session_dir), to="worker",
                message="Too late", message_file="",
            ))
        self.assertEqual(sent, 2)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn("end --to worker", errors.getvalue())
        self.assertEqual(self.methods("thread/queue/add"), [])
        self.assertEqual(self.auto_turn_starts, 0)
        self.assertEqual(pending.read_bytes(), before)

    def test_capacity_exhaustion_drains_the_server_queue(self) -> None:
        """No queued message can start unwatched after start returns."""
        self.outcomes = ["capacity"] * 7 + ["capacity_final_queued"]
        code, errors = self.run_start()
        self.assertEqual(code, 1)
        self.assertIn("model still at capacity", errors)
        self.assertEqual(self.queued, [])
        self.assertTrue(self.methods("thread/queue/delete"))
        self.assertEqual(self.worker_record()["status"], "capacity_exhausted")

    def test_live_turn_found_before_resume_is_streamed_then_task_resumes(self) -> None:
        self.outcomes = ["capacity", "completed"]
        self.announce_live_completion = True
        original_wait = self.wait

        def peer_starts_turn(seconds: float) -> None:
            original_wait(seconds)
            self.live_turn = "peer-turn"

        with patch.object(codex_mesh, "_capacity_sleep", peer_starts_turn):
            code, _errors = self.run_start()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertEqual(len(self.methods("turn/start")), 2)
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)

    def test_relaunch_refuses_a_live_thread_without_starting_another(self) -> None:
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "worker": {"thread_id": THREAD_ID, "turn_id": "peer-turn", "status": "running"}
        }), encoding="utf-8")
        self.live_turn = "peer-turn"
        code, errors = self.run_start()
        self.assertEqual(code, 2)
        self.assertIn(THREAD_ID, errors)
        self.assertIn("end", errors)
        self.assertEqual(self.methods("thread/start"), [])
        self.assertEqual(self.methods("turn/steer"), [])
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)

    def test_relaunch_refuses_a_waiting_worker_with_a_live_launcher(self) -> None:
        """The waiting launcher owns its thread until it exits."""
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "worker": {"thread_id": THREAD_ID, "turn_id": "", "status": "waiting_capacity",
                     "launcher_pid": os.getpid()}
        }), encoding="utf-8")
        code, errors = self.run_start()
        self.assertEqual(code, 2)
        self.assertIn(THREAD_ID, errors)
        self.assertIn("waiting for capacity", errors)
        self.assertIn("end", errors)
        self.assertEqual(self.methods("thread/start"), [])

    def test_relaunch_refuses_live_waiting_launcher_without_server_record(self) -> None:
        """Retiring the server cannot release a live launcher's worker."""
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "worker": {"thread_id": THREAD_ID, "turn_id": "", "status": "waiting_capacity",
                     "launcher_pid": os.getpid()}
        }), encoding="utf-8")
        (self.session_dir / codex_mesh.SERVER_FILE).unlink()
        with patch.object(codex_mesh, "ensure_server", return_value=(self.server.port, True)) as ensure:
            code, errors = self.run_start()
        self.assertEqual(code, 2)
        self.assertIn("waiting for capacity", errors)
        self.assertIn(THREAD_ID, errors)
        ensure.assert_not_called()
        self.assertEqual(self.methods("thread/start"), [])

    def test_dead_waiting_launcher_drops_held_messages_on_relaunch(self) -> None:
        """Replacing a worker clears messages held for its old thread."""
        old_thread = "old-waiting-thread"
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "worker": {"thread_id": old_thread, "turn_id": "", "status": "waiting_capacity",
                     "launcher_pid": 999999}
        }), encoding="utf-8")
        _ = (self.session_dir / "worker.pending.json").write_text(json.dumps({
            "thread_id": old_thread, "messages": ["First", "Second"]
        }), encoding="utf-8")
        code, _errors = self.run_start()
        self.assertEqual(code, 0)
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)
        pending = cast(dict[str, object], json.loads((self.session_dir / "worker.pending.json").read_text(encoding="utf-8")))
        self.assertEqual(pending["messages"], [])
        log = (self.session_dir / "worker.log").read_text(encoding="utf-8")
        self.assertIn("2 held messages", log)
        self.assertIn(old_thread, log)

    def test_relaunch_replaces_waiting_worker_whose_launcher_died(self) -> None:
        """A stale waiting entry does not block recovery."""
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "worker": {"thread_id": THREAD_ID, "turn_id": "", "status": "waiting_capacity",
                     "launcher_pid": 999999}
        }), encoding="utf-8")
        code, _errors = self.run_start()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertFalse(any(str(request.get("id", "")).startswith("relaunch")
                             for request in self.methods("thread/read")))
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)

    def test_failed_worker_relaunches_when_old_thread_has_unknown_status(self) -> None:
        old_thread = "old-failed-thread"
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "worker": {"thread_id": old_thread, "turn_id": "", "status": "failed"}
        }), encoding="utf-8")
        self.thread_statuses[old_thread] = "retired"

        code, errors = self.run_start()

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)
        log = (self.session_dir / "worker.log").read_text(encoding="utf-8")
        self.assertIn(old_thread, log)
        self.assertIn("could not be interrupted", log)

    def test_failed_worker_relaunches_when_old_thread_has_system_error(self) -> None:
        old_thread = "old-failed-thread"
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "worker": {"thread_id": old_thread, "turn_id": "", "status": "failed"}
        }), encoding="utf-8")
        self.thread_statuses[old_thread] = "systemError"

        code, errors = self.run_start()

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertEqual(self.methods("turn/interrupt"), [])
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)
        log = (self.session_dir / "worker.log").read_text(encoding="utf-8")
        self.assertNotIn("could not be interrupted", log)

    def test_failed_worker_interrupts_old_turn_before_starting_thread(self) -> None:
        old_thread = "old-failed-thread"
        old_turn = "old-live-turn"
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "worker": {"thread_id": old_thread, "turn_id": old_turn, "status": "failed"}
        }), encoding="utf-8")
        self.live_turn = old_turn

        code, errors = self.run_start()

        self.assertEqual((code, errors), (0, ""))
        interrupts = self.methods("turn/interrupt")
        self.assertEqual(len(interrupts), 1)
        self.assertEqual(cast("dict[str, object]", interrupts[0]["params"]), {
            "threadId": old_thread, "turnId": old_turn,
        })
        self.assertLess(self.server.requests.index(interrupts[0]),
                        self.server.requests.index(self.methods("thread/start")[0]))
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)

    def test_failed_worker_refuses_relaunch_when_interrupt_errors_and_turn_stays_live(self) -> None:
        old_thread = "old-failed-thread"
        old_turn = "old-live-turn"
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "worker": {"thread_id": old_thread, "turn_id": old_turn, "status": "failed"}
        }), encoding="utf-8")
        self.live_turn = old_turn
        self.interrupt_error = "interrupt refused"

        code, errors = self.run_start()

        self.assertEqual(code, 2)
        self.assertIn(f"thread {old_thread} still has a live turn", errors)
        self.assertEqual(self.methods("thread/start"), [])
        self.assertEqual(self.live_turn, old_turn)
        self.assertIn(
            f"thread {old_thread} could not be interrupted (interrupt refused)",
            (self.session_dir / "worker.log").read_text(encoding="utf-8"),
        )

    def test_failed_worker_relaunches_when_turn_ends_after_interrupt_error(self) -> None:
        old_thread = "old-failed-thread"
        old_turn = "old-live-turn"
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "worker": {"thread_id": old_thread, "turn_id": old_turn, "status": "failed"}
        }), encoding="utf-8")
        self.live_turn = old_turn
        self.interrupt_error = "turn already ended"
        self.interrupt_error_ends_turn = True

        code, errors = self.run_start()

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(len(self.methods("turn/interrupt")), 1)
        old_reads = [request for request in self.methods("thread/read")
                     if cast("dict[str, object]", request["params"])["threadId"] == old_thread]
        self.assertEqual(len(old_reads), 2)
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)

    def test_failed_worker_refuses_relaunch_when_active_turn_has_no_id(self) -> None:
        old_thread = "old-failed-thread"
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "worker": {"thread_id": old_thread, "turn_id": "", "status": "failed"}
        }), encoding="utf-8")
        self.live_turn = "old-live-turn"
        self.hide_live_turn_id = True
        self.hide_turn_id_from_steer = True

        code, errors = self.run_start()

        self.assertEqual(code, 2)
        self.assertIn(f"thread {old_thread} still has a live turn", errors)
        self.assertEqual(self.methods("thread/start"), [])
        self.assertEqual(self.methods("turn/interrupt"), [])
        self.assertIn(
            f"thread {old_thread} could not be interrupted (no turn id)",
            (self.session_dir / "worker.log").read_text(encoding="utf-8"),
        )

    def test_failed_worker_without_server_record_starts_on_new_server(self) -> None:
        old_thread = "old-failed-thread"
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "worker": {"thread_id": old_thread, "turn_id": "", "status": "failed"}
        }), encoding="utf-8")
        (self.session_dir / codex_mesh.SERVER_FILE).unlink()

        with patch.object(codex_mesh, "ensure_server", return_value=(self.server.port, True)) as ensure:
            code, errors = self.run_start()

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(ensure.call_count, 2)
        self.assertEqual(ensure.call_args.args, (str(self.session_dir), agent_accounts.workers_home()))
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertFalse(any(
            cast("dict[str, object]", request.get("params", {})).get("threadId") == old_thread
            for request in self.methods("thread/read")
        ))
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)

    def test_replaced_waiting_launcher_starts_no_resume_turn(self) -> None:
        """A stale launcher cannot overwrite its replacement."""
        self.outcomes = ["capacity"]
        original_wait = self.wait

        def replace_during_wait(seconds: float) -> None:
            original_wait(seconds)
            _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
                "worker": {"thread_id": "replacement-thread", "turn_id": "new-turn",
                         "status": "running"}
            }), encoding="utf-8")

        with patch.object(codex_mesh, "_capacity_sleep", replace_during_wait):
            code, _errors = self.run_start()
        self.assertEqual(code, 1)
        self.assertEqual(len(self.methods("turn/start")), 1)
        self.assertEqual(self.worker_record()["thread_id"], "replacement-thread")
        self.assertIn("replaced", (self.session_dir / "worker.log").read_text(encoding="utf-8"))

    def test_relaunch_refuses_when_thread_read_errors(self) -> None:
        """An unreadable active thread is never assumed idle."""
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "worker": {"thread_id": THREAD_ID, "turn_id": "", "status": "running"}
        }), encoding="utf-8")
        self.read_failure = "error"
        code, errors = self.run_start()
        self.assertEqual(code, 2)
        self.assertIn("could not be read", errors)
        self.assertIn("read refused", errors)
        self.assertEqual(self.methods("thread/start"), [])

    def test_relaunch_refuses_when_thread_read_times_out(self) -> None:
        """A timed out read cannot authorize a relaunch."""
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "worker": {"thread_id": THREAD_ID, "turn_id": "", "status": "running"}
        }), encoding="utf-8")
        self.read_failure = "timeout"
        original_call = codex_mesh.Client.call

        def short_read(client: codex_mesh.Client, method: str,
                       params: dict[str, object], timeout: float = 300.0) -> codex_mesh.RpcMessage:
            return original_call(client, method, params,
                                 timeout=0.01 if method == "thread/read" else timeout)

        with patch.object(codex_mesh.Client, "call", short_read):
            code, errors = self.run_start()
        self.assertEqual(code, 2)
        self.assertIn("timed out", errors)
        self.assertEqual(self.methods("thread/start"), [])

    def test_stop_pause_interrupts_live_work_preserves_queued_messages_and_threads(self) -> None:
        entries: dict[str, object] = {
            "working": {"thread_id": THREAD_ID, "status": "running", "turn_id": "live-turn", "role": "impl"},
            "idle": {"thread_id": "idle-thread", "status": "done", "role": "review"},
            "ended": {"thread_id": "ended-thread", "status": "ended", "role": "impl"},
        }
        self.write_pool_roster(entries)
        self.live_turn = "live-turn"
        self.thread_statuses["idle-thread"] = "idle"
        self.queued = [{"id": "queued-task", "input": [{"type": "text", "text": "preserved queued task"}]}]
        ended_marker = codex_mesh._end_marker_path(str(self.session_dir), "ended")  # pyright: ignore[reportPrivateUsage]
        ended_marker.touch()
        respond = self.server.respond
        @bounded_fixture
        def loaded_workers(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/loaded/list":
                return [_reply(request, {"data": [THREAD_ID, "idle-thread"], "nextCursor": None})]
            return respond(request)
        self.server.respond = loaded_workers
        with patch.object(codex_mesh, "_reap") as reap:
            code, _output, errors = self.run_mesh(["stop", "--pause"])
        self.assertEqual((code, errors), (0, ""))
        reap.assert_called_once_with(os.getpid())
        self.assertFalse((self.session_dir / codex_mesh.SERVER_FILE).exists())
        roster = self.roster()
        for worker, previous, live in (("working", "running", True), ("idle", "done", False)):
            paused = cast(dict[str, object], roster[worker])
            self.assertEqual(paused["status"], "paused")
            self.assertEqual(paused["paused_from"], previous)
            self.assertIs(paused["paused_turn"], live)
            self.assertFalse(codex_mesh._end_marker_path(str(self.session_dir), worker).exists())  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(cast(dict[str, object], roster["working"])["thread_id"], THREAD_ID)
        self.assertEqual(roster["ended"], entries["ended"])
        self.assertTrue(ended_marker.exists())
        self.assertEqual(self.queued, [])
        self.assertEqual(_as_test_dict(cast(object, json.loads(
            (self.session_dir / "working.pending.json").read_text())))["messages"], ["preserved queued task"])
        interrupts = self.methods("turn/interrupt")
        self.assertEqual(len(interrupts), 1)
        self.assertEqual(cast(dict[str, object], interrupts[0]["params"]), {"threadId": THREAD_ID, "turnId": "live-turn"})

    def test_launcher_completion_after_pause_cannot_overwrite_resume_metadata(self) -> None:
        self.write_pool_roster({"worker": {
            "thread_id": THREAD_ID, "status": "running", "role": "impl", "turn_id": "live-turn",
        }})
        self.live_turn = "live-turn"
        respond = self.server.respond

        @bounded_fixture
        def loaded_worker(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/loaded/list":
                return [_reply(request, {"data": [THREAD_ID], "nextCursor": None})]
            return respond(request)

        self.server.respond = loaded_worker
        with patch.object(codex_mesh, "_reap"):
            code, _output, errors = self.run_mesh(["stop", "--pause"])
        self.assertEqual((code, errors), (0, ""))
        # An interrupted launcher's final completion/failure writes arrive after stop.
        late_records: list[codex_mesh.FollowableRecord] = [
            {"thread_id": THREAD_ID, "status": "done"},
            {"thread_id": THREAD_ID, "status": "failed"},
        ]
        for late_record in late_records:
            codex_mesh._update_roster(str(self.session_dir), "worker", late_record)  # pyright: ignore[reportPrivateUsage]
            entry = self.worker_record()
            self.assertEqual(entry["status"], "paused")
            self.assertEqual(entry["paused_from"], "running")
            self.assertIs(entry["paused_turn"], True)
        with patch.object(codex_mesh, "ensure_server", return_value=(self.server.port, True)):
            code, errors = self.run_follow()
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.worker_record()["status"], "done")
        self.assertNotIn("paused_from", self.worker_record())
        self.assertNotIn("paused_turn", self.worker_record())
        self.assertEqual(self.methods("thread/start"), [])
        self.assertEqual(len(self.methods("thread/resume")), 1)

    def test_end_interrupts_real_id_when_read_has_no_turn(self) -> None:
        """An active status without persisted turn uses a real id."""
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "worker": {"thread_id": THREAD_ID, "turn_id": "", "status": "running"}
        }), encoding="utf-8")
        self.live_turn = "peer-turn"
        self.hide_live_turn_id = True
        with contextlib.redirect_stdout(io.StringIO()):
            code = codex_mesh.command_end(argparse.Namespace(
                session_dir=str(self.session_dir), to="worker"
            ))
        self.assertEqual(code, 0)
        self.assertEqual(self.live_turn, "")
        interrupts = self.methods("turn/interrupt")
        self.assertEqual(len(interrupts), 1)
        self.assertEqual(cast("dict[str, object]", interrupts[0]["params"])["turnId"], "peer-turn")

    def test_end_on_system_error_thread_has_no_turn_to_interrupt(self) -> None:
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "worker": {"thread_id": THREAD_ID, "turn_id": "", "status": "running"}
        }), encoding="utf-8")
        self.thread_statuses[THREAD_ID] = "systemError"

        with contextlib.redirect_stdout(io.StringIO()) as output, \
             contextlib.redirect_stderr(io.StringIO()) as errors:
            code = codex_mesh.command_end(argparse.Namespace(
                session_dir=str(self.session_dir), to="worker"
            ))

        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue(), "ending worker\n")
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(self.methods("turn/interrupt"), [])

    def test_end_marks_a_finished_worker_ended_and_keeps_its_metadata(self) -> None:
        self.context_sizes = [123456]
        self.assertEqual(self.run_start()[0], 0)

        with contextlib.redirect_stdout(io.StringIO()):
            code = codex_mesh.command_end(argparse.Namespace(
                session_dir=str(self.session_dir), to="worker"
            ))

        self.assertEqual(code, 0)
        self.assertEqual(self.worker_record()["status"], "ended")
        self.assertEqual(self.worker_record()["role"], "impl")
        self.assertEqual(self.worker_record()["context_tokens"], 123456)

    def test_capacity_exhaustion_has_a_distinct_run_outcome(self) -> None:
        """Retry exhaustion is control state, not failure text."""
        self.outcomes = ["capacity"] * 20
        port = self.server.port
        outcome = codex_mesh._run_worker(self.start_args(), port)  # pyright: ignore[reportPrivateUsage]
        assert isinstance(outcome, codex_mesh.CapacityRetriesExhausted)
        self.assertEqual(outcome.thread_id, THREAD_ID)
        self.assertEqual(outcome.retries, len(self.waits))

    def test_turn_outcome_cases_carry_only_their_own_data(self) -> None:
        """Completion has no failure detail or kind discriminator."""
        completed = codex_mesh._turn_outcome("turn/completed", {  # pyright: ignore[reportPrivateUsage]
            "turn": {"status": "completed"}
        })
        refused = codex_mesh._turn_outcome("turn/failed", {  # pyright: ignore[reportPrivateUsage]
            "error": {"message": CAPACITY}
        })
        assert isinstance(completed, codex_mesh.TurnCompleted)
        self.assertEqual(vars(completed), {})
        assert isinstance(refused, codex_mesh.TurnRefusedForCapacity)
        self.assertEqual(refused.detail, CAPACITY)

    def test_end_reaches_a_thread_during_capacity_wait(self) -> None:
        self.outcomes = ["capacity", "completed"]
        original_wait = self.wait
        ended = False

        def end_while_waiting(seconds: float) -> None:
            nonlocal ended
            if not ended:
                ended = True
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(codex_mesh.command_send(argparse.Namespace(
                        session_dir=str(self.session_dir), to="worker",
                        message="Drop on end", message_file="",
                    )), 0)
                    self.assertEqual(codex_mesh.command_end(argparse.Namespace(
                        session_dir=str(self.session_dir), to="worker"
                    )), 0)
                self.waits.append(seconds)
                self.elapsed += seconds
            else:
                original_wait(seconds)

        with patch.object(codex_mesh, "_capacity_sleep", end_while_waiting):
            _code, _errors = self.run_start()
        self.assertTrue(ended)
        self.assertTrue(self.methods("thread/queue/list"))
        self.assertTrue((self.session_dir / "worker.end").exists())
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)
        self.assertEqual(self.worker_record()["status"], "ended")
        pending = cast("dict[str, object]", json.loads(
            (self.session_dir / "worker.pending.json").read_text(encoding="utf-8") or "{}"
        ))
        self.assertEqual(pending, {})

    def test_send_after_end_refuses_waiting_worker_and_keeps_pending_empty(self) -> None:
        """An end marker closes the waiting worker to new messages."""
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "worker": {"thread_id": THREAD_ID, "turn_id": "", "status": "waiting_capacity",
                     "launcher_pid": os.getpid()}
        }), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(codex_mesh.command_end(argparse.Namespace(
                session_dir=str(self.session_dir), to="worker"
            )), 0)
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            sent = codex_mesh.command_send(argparse.Namespace(
                session_dir=str(self.session_dir), to="worker",
                message="Too late", message_file="",
            ))
        self.assertEqual(sent, 2)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn("being ended", errors.getvalue())
        self.assertEqual((self.session_dir / "worker.pending.json").read_text(encoding="utf-8"), "")

    def test_send_to_ended_status_without_end_marker_refuses_and_keeps_pending_empty(self) -> None:
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "ended"}})
        code, output, errors = self.run_mesh(["send", "--to", "worker", "--message", "Too late"])
        self.assertEqual((code, output), (2, ""))
        self.assertEqual(errors, "codex_mesh: worker is ended; message not delivered\n")
        self.assertEqual((self.session_dir / "worker.pending.json").read_text(), "")
        self.assertEqual(self.server.requests, [])

    def test_send_to_worker_missing_from_roster_names_the_known_workers_and_holds_nothing(self) -> None:
        self.write_pool_roster({"peer": {"thread_id": THREAD_ID, "status": "done"}})
        with self.assertRaises(SystemExit) as refused:
            _ = self.run_mesh(["send", "--to", "worker", "--message", "No recipient"])
        self.assertEqual(str(refused.exception.code), "codex_mesh: no worker named 'worker'. Known: peer")
        self.assertNotIn("No recipient", "".join(
            path.read_text() for path in self.session_dir.glob("*.pending.json")))
        self.assertEqual(self.server.requests, [])

    def test_unreadable_thread_after_completion_ends_and_cleans_up(self) -> None:
        """Failed liveness reads end the launcher through cleanup."""
        self.outcomes = ["completed"]
        original_read = self.respond
        clock_now = [1000.0]

        @bounded_fixture
        def clock() -> float:
            return clock_now[0]

        def unreadable_after_completion(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/read" and self.turn_starts:
                self.read_failure = "error"
            return original_read(request)

        original_next_frame = codex_mesh.Client.next_frame

        @bounded_fixture
        def no_second_wait(client: codex_mesh.Client, deadline: float) -> codex_mesh.RpcMessage | None:
            if self.methods("thread/read") and self.read_failure == "error":
                clock_now[0] = deadline + 1
            return original_next_frame(client, deadline)

        self.server.respond = unreadable_after_completion
        # The fake epoch must not age the stub process into a sign-in replacement.
        with patch.object(codex_mesh, "time", SimpleNamespace(time=clock, monotonic=clock, sleep=time.sleep)), \
             patch.object(codex_mesh, "ensure_server", return_value=(self.server.port, False)), \
             patch.object(codex_mesh.Client, "next_frame", no_second_wait):
            code, errors = self.run_start()
        self.assertEqual(code, 1)
        self.assertIn(THREAD_ID, errors)
        self.assertIn("thread/read failed", errors)
        self.assertIn("read refused", errors)
        self.assertGreaterEqual(len(self.methods("thread/queue/list")), 2)
        self.assertIn("could not be interrupted", (self.session_dir / "worker.log").read_text(encoding="utf-8"))


    def test_compaction_after_home_change_releases_old_writer_before_target_resume(self) -> None:
        self.context_sizes = [150000]
        self.assertEqual(self.run_start()[0], 0)
        _target, new = self.prepare_workers_move()
        self.assertEqual(accounts.main(["set-role", "workers", "codex1"]), 0)
        calls: list[tuple[str, str]] = []
        loaded = [True]
        old_respond = self.server.respond
        new_respond = new.respond
        def old(request: dict[str, object]) -> list[dict[str, object]] | None:
            method = str(request.get("method"))
            calls.append(("old", method))
            if method == "thread/unsubscribe":
                loaded[0] = False
            if method == "thread/loaded/list":
                return [_reply(request, {"data": [THREAD_ID] if loaded[0] else []})]
            return old_respond(request)
        def target(request: dict[str, object]) -> list[dict[str, object]] | None:
            calls.append(("target", str(request.get("method"))))
            return new_respond(request)
        self.server.respond = old
        new.respond = target
        with patch.object(codex_mesh, "_stop_unused", return_value=False), contextlib.redirect_stdout(io.StringIO()):
            code = codex_mesh.command_compact(argparse.Namespace(session_dir=str(self.session_dir), to="worker"))
        self.assertEqual(code, 0)
        self.assertLess(calls.index(("old", "thread/unsubscribe")), calls.index(("target", "thread/resume")))

    def test_swap_stops_unused_old_writer_and_next_follow_resumes_on_target(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        target, new = self.prepare_workers_move()
        self.assertEqual(accounts.main(["set-role", "workers", "codex1"]), 0)
        _ = (self.session_dir / codex_mesh.SERVER_FILE).write_text(json.dumps({
            "pid": 123456, "port": self.server.port, "codex_home": str(target.parent / ".codex"), "auto_compact_tokens": codex_mesh.AUTO_COMPACT_TOKENS,
        }))
        alive = [True]
        def running(pid: int) -> bool:
            return alive[0] if pid == 123456 else pid == os.getpid()
        def stop(_root: Path | None, pid: int, _port: int, _owner: codex_mesh.ServerOwnership) -> bool:
            self.assertEqual(pid, 123456)
            alive[0] = False
            return True
        with patch.object(codex_mesh, "SWEEP_ROOTS", (self.session_dir.parent,)), patch.object(codex_mesh, "_pid_alive", side_effect=running), patch.object(codex_mesh, "_stop_unused", side_effect=stop) as stopped, contextlib.redirect_stdout(io.StringIO()):
            _ = codex_mesh.retire_workers_servers(target, "codex1")
            code, errors = self.run_follow()
        self.assertEqual((code, errors), (0, ""))
        stopped.assert_called_once()
        self.assertTrue(any(request.get("method") == "thread/resume" for request in new.requests))

    def test_compaction_with_unconfirmed_old_writer_posts_one_refusal_and_never_resumes(self) -> None:
        self.context_sizes = [150000]
        self.assertEqual(self.run_start()[0], 0)
        _target, new = self.prepare_workers_move()
        self.assertEqual(accounts.main(["set-role", "workers", "codex1"]), 0)
        with patch.object(codex_mesh, "_release_other_worker_writers", return_value=codex_mesh.WorkerThreadStateUnknown("release not confirmed")), patch.object(codex_mesh, "_post_board_status") as board, contextlib.redirect_stderr(io.StringIO()) as errors:
            code = codex_mesh.command_compact(argparse.Namespace(session_dir=str(self.session_dir), to="worker"))
        self.assertEqual(code, 2)
        self.assertIn("release not confirmed", errors.getvalue())
        board.assert_called_once()
        self.assertFalse(any(request.get("method") == "thread/resume" for request in new.requests))

    def test_send_between_last_pending_check_and_exit_starts_one_message_turn(self) -> None:
        sent: list[int] = []
        def last_check(_client: codex_mesh.Client, _thread_id: str, _deadline: float) -> bool:
            if not sent:
                sent.append(codex_mesh.command_send(argparse.Namespace(
                    session_dir=str(self.session_dir), to="worker", message="late peer message", message_file="")))
            return False
        with patch.object(codex_mesh, "_more_work_coming", side_effect=last_check), contextlib.redirect_stdout(io.StringIO()):
            code, errors = self.run_start()
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(sent, [0])
        self.assertEqual(self.turn_inputs, ["Original worker task", "late peer message"])
        pending = cast(dict[str, object], json.loads((self.session_dir / "worker.pending.json").read_text()))
        self.assertEqual(pending["messages"], [])
        self.assertIs(pending["accepting_messages"], False)

    def test_send_after_launcher_closes_inbox_is_held_until_follow_after_its_prompt(self) -> None:
        sent: list[int] = []
        output = io.StringIO()
        def after_close(_port: int, _thread_id: str, _log_path: Path) -> codex_mesh.RelaunchAllowed:
            with contextlib.redirect_stdout(output):
                sent.append(codex_mesh.command_send(argparse.Namespace(
                    session_dir=str(self.session_dir), to="worker", message="late peer message", message_file="")))
            return codex_mesh.RelaunchAllowed()
        with patch.object(codex_mesh, "_end_unwatched_turn", side_effect=after_close):
            code, errors = self.run_start()
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(sent, [0])
        self.assertEqual(output.getvalue(),
                         "queued for worker; no launcher is attached, so the message waits for its next dispatch\n")
        self.assertEqual(len(self.methods("turn/start")), 1)
        self.assert_held_message_delivered_on_follow("late peer message")

    def assert_held_message_delivered_on_follow(self, message: str) -> None:
        pending_path = self.session_dir / "worker.pending.json"
        pending = cast(codex_mesh.WorkerPendingMessages, json.loads(pending_path.read_text()))
        self.assertEqual(pending["thread_id"], THREAD_ID)
        self.assertEqual(pending["messages"], [message])
        self.server.requests.clear()
        self.assertEqual(self.run_follow("next dispatch prompt"), (0, ""))
        starts = self.methods("turn/start")
        self.assertEqual(len(starts), 1)
        params = _as_test_dict(starts[0].get("params"))
        self.assertEqual(params["threadId"], THREAD_ID)
        inputs = cast(list[dict[str, object]], params["input"])
        self.assertEqual([item["text"] for item in inputs], ["next dispatch prompt", message])
        pending = cast(codex_mesh.WorkerPendingMessages, json.loads(pending_path.read_text()))
        self.assertEqual(pending["messages"], [])
        self.assertIs(pending["accepting_messages"], False)

    def assert_send_without_launcher_waits_for_follow(self, status: str, launcher_pid: int) -> None:
        self.assertEqual(self.run_start(), (0, ""))
        record = {**self.worker_record(), "status": status, "launcher_pid": launcher_pid}
        if status == "running":
            record["turn_id"] = "turn-1"
        self.write_pool_roster({"worker": record})
        # Keep the inbox open so status and PID are each tested independently of inbox closure.
        _ = (self.session_dir / "worker.pending.json").write_text(json.dumps({
            "thread_id": THREAD_ID, "messages": [], "accepting_messages": True,
        }))
        self.server.requests.clear()
        def launcher_alive(pid: int) -> bool:
            return pid == os.getpid()

        with patch.object(codex_mesh, "_pid_alive", side_effect=launcher_alive):
            code, output, errors = self.run_mesh(["send", "--to", "worker", "--message", "held peer message"])
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(output,
                         "queued for worker; no launcher is attached, so the message waits for its next dispatch\n")
        self.assertEqual(self.server.requests, [])
        self.assert_held_message_delivered_on_follow("held peer message")

    def test_send_to_paused_worker_is_held_until_follow_after_its_prompt(self) -> None:
        self.assert_send_without_launcher_waits_for_follow("paused", os.getpid())

    def test_send_to_done_worker_is_held_until_follow_after_its_prompt(self) -> None:
        self.assert_send_without_launcher_waits_for_follow("done", os.getpid())

    def test_send_with_dead_launcher_is_held_until_follow_after_its_prompt(self) -> None:
        self.assert_send_without_launcher_waits_for_follow("running", 99999999)

    def test_resident_failed_turn_delivers_one_reply_and_ends_without_repeating_failure(self) -> None:
        self.outcomes = ["failed"]
        args = self.start_args()
        args.resident = True
        deliver = codex_mesh._deliver_reply  # pyright: ignore[reportPrivateUsage]
        def reply(name: str, index: int, answer: str, failure: str, summary: Path, replies: Path | None, log: TextIO) -> None:
            deliver(name, index, answer, failure, summary, replies, log)
            _ = (self.session_dir / "worker.end").write_text("end")
        with patch.object(codex_mesh, "_deliver_reply", side_effect=reply) as sent, contextlib.redirect_stdout(io.StringIO()):
            outcome = codex_mesh._run_worker(args, self.server.port)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(outcome, codex_mesh.RunCompleted)
        sent.assert_called_once()
        self.assertEqual(self.worker_record()["status"], "done")

    def test_resident_idle_ticks_skip_move_requests_when_home_and_port_match(self) -> None:
        args = self.start_args()
        args.resident = True
        empty = [0]
        read = codex_mesh.Client.next_frame
        @bounded_fixture
        def poll(client: codex_mesh.Client, deadline: float) -> codex_mesh.RpcMessage | None:
            frame = read(client, deadline)
            if frame is None:
                empty[0] += 1
                if empty[0] >= 3:
                    _ = (self.session_dir / "worker.end").write_text("end")
            return frame
        with patch.object(codex_mesh.Client, "next_frame", poll), patch.object(codex_mesh, "_move_resident", wraps=codex_mesh._move_resident) as move, contextlib.redirect_stdout(io.StringIO()):  # pyright: ignore[reportPrivateUsage]
            outcome = codex_mesh._run_worker(args, self.server.port)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(outcome, codex_mesh.RunCompleted)
        self.assertEqual(empty[0], 3)
        self.assertEqual(move.call_count, 2)

    def test_lost_new_server_turn_reply_cleans_up_that_server_and_thread(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        target, new = self.prepare_workers_move()
        self.assertEqual(accounts.main(["set-role", "workers", "codex1"]), 0)
        args = self.start_args()
        _ = (self.session_dir / "worker.pending.json").write_text(json.dumps({"thread_id": THREAD_ID, "messages": ["held message"]}))
        with patch.object(codex_mesh.Client, "call", autospec=True) as call, patch.object(codex_mesh, "_end_unwatched_turn", return_value=codex_mesh.RelaunchAllowed()) as cleanup:
            @bounded_fixture
            def rpc(_client: codex_mesh.Client, method: str, _params: dict[str, object], **_options: object) -> codex_mesh.RpcMessage:
                if method == "thread/read":
                    return {"result": {"thread": {"status": {"type": "idle"}}}}
                if method in ("thread/queue/list", "thread/loaded/list"):
                    return {"result": {"data": []}}
                if method == "turn/start":
                    raise ConnectionError("reply lost after acceptance")
                return {"result": {}}
            call.side_effect = rpc
            client = codex_mesh.Client(self.server.port, "worker")
            outcome = codex_mesh._stream_turn(args, client, self.server.port, str(self.session_dir), "worker", THREAD_ID, "next task", 5, time.time(), follow=True)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(outcome, codex_mesh.FailedWithThread)
        self.assertEqual(cast(int, cleanup.call_args.args[0]), new.port)
        self.assertEqual(cast(str, cleanup.call_args.args[1]), THREAD_ID)
        self.assertEqual(agent_accounts.workers_home(), target)
        self.assertEqual(sum(entry.args[1] == "turn/start" for entry in call.call_args_list), 1)

    def test_resident_resume_failure_retries_on_a_later_counted_tick(self) -> None:
        target, new = self.prepare_workers_move()
        args = self.start_args()
        args.resident = True
        args.timeout = 600
        resumes: list[int] = []
        resume_ticks: list[int] = []
        ticks = 0
        offset = 0.0
        first_reply_delivered = False
        monotonic = time.monotonic
        call = codex_mesh.Client.call
        read = codex_mesh.Client.next_frame
        deliver = codex_mesh._deliver_reply  # pyright: ignore[reportPrivateUsage]

        @bounded_fixture
        def reply(name: str, index: int, answer: str, failure: str, summary: Path, replies: Path | None, log: TextIO) -> None:
            nonlocal first_reply_delivered
            deliver(name, index, answer, failure, summary, replies, log)
            first_reply_delivered = True

        @bounded_fixture
        def rpc(client: codex_mesh.Client, method: str, params: dict[str, object], timeout: float = codex_mesh.CALL_TIMEOUT_SECS) -> codex_mesh.RpcMessage:
            if client.port == new.port and method == "thread/resume":
                resumes.append(client.port)
                resume_ticks.append(ticks)
                if len(resumes) == 1:
                    raise ConnectionError("resume connection failed before any turn")
            return call(client, method, params, timeout=timeout)

        @bounded_fixture
        def poll(client: codex_mesh.Client, deadline: float) -> codex_mesh.RpcMessage | None:
            nonlocal ticks, offset
            frame = read(client, min(deadline, time.time() + 0.02))
            if frame is None and first_reply_delivered:
                ticks += 1
                self.assertLessEqual(ticks, 8, "resume retry exceeded its tick budget")
                offset += 61
                if not resumes:
                    self.assertEqual(accounts.main(["set-role", "workers", "codex1"]), 0)
                elif client.port == new.port:
                    _ = (self.session_dir / "worker.end").write_text("end")
            return frame

        # Finish the original turn before selecting the new account: startup
        # file wakes do not establish an idle resident or a retry tick.
        # Advance the simulated clock after real quiet reads, independently of load.
        with (patch.object(codex_mesh.Client, "call", rpc),
              patch.object(codex_mesh.Client, "next_frame", poll),
              patch.object(codex_mesh, "_deliver_reply", reply),
              patch.object(time, "monotonic", new=lambda: monotonic() + offset),
              patch.object(codex_mesh, "_stop_unused", return_value=False),
              contextlib.redirect_stdout(io.StringIO())):
            outcome = codex_mesh._run_worker(args, self.server.port)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(outcome, codex_mesh.RunCompleted)
        self.assertEqual(resumes, [new.port, new.port])
        self.assertGreater(resume_ticks[1], resume_ticks[0])
        self.assertIn("cannot move: resume connection failed before any turn; waiting", (self.session_dir / "worker.log").read_text())
        self.assertEqual(self.turn_starts, 1)
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)
        self.assertEqual(agent_accounts.workers_home(), target)

    def prepare_post_turn_home_change(
        self, messages: tuple[str, ...], *, late: bool = False,
    ) -> tuple[StubAppServer, Callable[[], None], Callable[[], None]]:
        """Keep the old writer loaded while selecting a different fake server."""
        target, new = self.prepare_workers_move()
        selected_home = [target.parent / ".codex"]
        loaded = [True]
        changed = [False]
        _ = self.patches.enter_context(patch.object(codex_mesh, "workers_home", side_effect=lambda: selected_home[0]))
        _ = self.patches.enter_context(patch.object(agent_accounts, "workers_home", side_effect=lambda: selected_home[0]))
        _ = self.patches.enter_context(patch.object(codex_mesh, "_stop_unused", return_value=False))
        _ = self.patches.enter_context(patch.object(codex_mesh, "_stop_unused_with_lock_held", return_value=False))
        _ = self.patches.enter_context(patch.object(codex_mesh, "THREAD_RELEASE_TIMEOUT_SECS", 0.03))
        _ = self.patches.enter_context(patch.object(codex_mesh, "UNSEEN_END_CHECK_SECS", 0.02))
        respond = self.server.respond

        def old(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/loaded/list":
                return [_reply(request, {"data": [THREAD_ID] if loaded[0] else [], "nextCursor": None})]
            if request.get("method") == "thread/unsubscribe":
                return [_reply(request, {})]
            frames = respond(request)
            if request.get("method") == "turn/start" and frames is not None:
                frames.insert(-1, _notice("item/completed", {
                    "threadId": THREAD_ID,
                    "item": {"type": "agentMessage", "text": "phase 13 turn answer"},
                }))
            return frames

        self.server.respond = old

        def change_home_and_park() -> None:
            if changed[0]:
                return
            changed[0] = True
            selected_home[0] = target
            for message in messages:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(codex_mesh.command_send(argparse.Namespace(
                        session_dir=str(self.session_dir), to="worker", message=message, message_file="",
                    )), 0)

        read = codex_mesh.Client.next_frame

        @bounded_fixture
        def frame(client: codex_mesh.Client, deadline: float) -> codex_mesh.RpcMessage | None:
            notice = read(client, deadline)
            if not late and notice is not None and notice.get("method") in ("turn/completed", "turn/failed", "item/completed"):
                if notice.get("method") != "item/completed" or _as_test_dict(_as_test_dict(notice.get("params")).get("item")).get("type") == "agentMessage":
                    change_home_and_park()
            return notice

        _ = self.patches.enter_context(patch.object(codex_mesh.Client, "next_frame", frame))

        def release_old_writer() -> None:
            loaded[0] = False

        return new, change_home_and_park, release_old_writer

    def assert_post_turn_messages_held(
        self, new: StubAppServer, messages: tuple[str, ...], board: MagicMock,
    ) -> None:
        pending = cast(codex_mesh.WorkerPendingMessages, json.loads(
            (self.session_dir / "worker.pending.json").read_text(encoding="utf-8")))
        self.assertEqual(pending["thread_id"], THREAD_ID)
        self.assertEqual(pending["messages"], list(messages))
        self.assertIs(pending["accepting_messages"], False)
        count = "1 message" if len(messages) == 1 else f"{len(messages)} messages"
        board.assert_called_once()
        board_message = cast(str, board.call_args.args[2])
        prefix = f"launcher: {count} held for the next launch: "
        self.assertTrue(board_message.startswith(prefix), board_message)
        reason = board_message[len(prefix):]
        self.assertIn("old server has not released the thread", reason)
        log = (self.session_dir / "worker.log").read_text(encoding="utf-8")
        self.assertEqual(log.count(f"mesh: worker cannot move: {reason}; {count} held for the next launch"), 1)
        self.assertFalse(any(request.get("method") in ("thread/resume", "thread/start", "turn/start") for request in new.requests))
        self.assertEqual(len(self.methods("thread/unsubscribe")), 1, "a held launch must not attempt a second move")

    def test_completed_follow_holds_one_message_and_finishes_done_after_home_change(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        self.server.requests.clear()
        new, _change, _release = self.prepare_post_turn_home_change(("parked peer message",))
        with patch.object(codex_mesh, "_post_board_status") as board, patch.object(codex_mesh, "_more_work_coming") as more:
            code, errors = self.run_follow()
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.worker_record()["status"], "done")
        self.assertEqual((self.session_dir / "summary.txt").read_text().strip(), "phase 13 turn answer")
        self.assert_post_turn_messages_held(new, ("parked peer message",), board)
        more.assert_not_called()

    def test_hold_closes_inbox_and_keeps_matching_messages_under_one_lock(self) -> None:
        pending = self.session_dir / "worker.pending.json"
        for stored_thread, messages, kept in (
            (THREAD_ID, ["first message", "second message"], 2),
            (THREAD_ID, [], 0),
            ("previous-thread", ["old thread message"], 0),
        ):
            with self.subTest(thread=stored_thread, messages=messages):
                _ = pending.write_text(json.dumps({
                    "thread_id": stored_thread, "messages": messages, "accepting_messages": True,
                }))
                with patch.object(codex_mesh, "_pending_file", wraps=codex_mesh._pending_file) as lock, patch.object(codex_mesh, "_write_pending", wraps=codex_mesh._write_pending) as write:  # pyright: ignore[reportPrivateUsage]
                    count = codex_mesh._close_messages_and_keep_pending(str(self.session_dir), "worker", THREAD_ID)  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(count, kept)
                lock.assert_called_once_with(str(self.session_dir), "worker")
                write.assert_called_once()
                self.assertEqual(cast(dict[str, object], json.loads(pending.read_text())), {
                    "thread_id": THREAD_ID, "messages": messages if kept else [], "accepting_messages": False,
                })

    def test_completed_start_holds_two_messages_with_plural_wording_and_finishes_done(self) -> None:
        messages = ("first peer message", "second peer message")
        new, _change, _release = self.prepare_post_turn_home_change(messages)
        with patch.object(codex_mesh, "_post_board_status") as board, patch.object(codex_mesh, "_more_work_coming") as more:
            code, errors = self.run_start()
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.worker_record()["status"], "done")
        self.assertEqual((self.session_dir / "summary.txt").read_text().strip(), "phase 13 turn answer")
        self.assert_post_turn_messages_held(new, messages, board)
        more.assert_not_called()

    def test_message_arriving_at_inbox_close_is_held_after_unconfirmed_move(self) -> None:
        new, change, _release = self.prepare_post_turn_home_change(("late peer message",), late=True)

        def last_check(_client: codex_mesh.Client, _thread: str, _deadline: float) -> bool:
            change()
            return False

        with patch.object(codex_mesh, "_post_board_status") as board, patch.object(codex_mesh, "_more_work_coming", side_effect=last_check) as more:
            code, errors = self.run_start()
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.worker_record()["status"], "done")
        self.assert_post_turn_messages_held(new, ("late peer message",), board)
        more.assert_called_once()

    def test_ended_unseen_answer_holds_parked_message_and_finishes_done(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        self.server.requests.clear()
        self.outcomes = ["answered_without_completion"]
        new, _change, _release = self.prepare_post_turn_home_change(("unseen peer message",))
        with patch.object(codex_mesh, "_post_board_status") as board, patch.object(codex_mesh, "_more_work_coming") as more:
            code, errors = self.run_follow()
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.worker_record()["status"], "done")
        self.assertEqual((self.session_dir / "summary.txt").read_text().strip(), "late answer")
        self.assert_post_turn_messages_held(new, ("unseen peer message",), board)
        more.assert_not_called()

    def test_ended_unseen_without_answer_keeps_its_failure_while_holding_message(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        self.server.requests.clear()
        self.outcomes = ["earlier_only"]
        new, change, _release = self.prepare_post_turn_home_change(("unanswered peer message",), late=True)
        respond = self.server.respond

        def no_answer(request: dict[str, object]) -> list[dict[str, object]] | None:
            frames = respond(request)
            if request.get("method") == "turn/start" and frames is not None:
                return [frame for frame in frames if _as_test_dict(_as_test_dict(frame.get("params")).get("item")).get("type") != "agentMessage"]
            return frames

        self.server.respond = no_answer
        ended = codex_mesh._ended_unseen  # pyright: ignore[reportPrivateUsage]

        def check(client: codex_mesh.Client, thread: str, turn: str = "", *, start_seen: bool = True) -> bool:
            change()
            return ended(client, thread, turn, start_seen=start_seen)

        with patch.object(codex_mesh, "_post_board_status") as board, patch.object(codex_mesh, "_ended_unseen", side_effect=check):
            code, errors = self.run_follow()
        self.assertEqual(code, 1)
        self.assertIn("the thread went idle with no completion notice and no answer", errors)
        self.assertNotIn("state unknown", errors)
        self.assertEqual(self.worker_record()["status"], "failed")
        self.assert_post_turn_messages_held(new, ("unanswered peer message",), board)

    def test_failed_follow_holds_messages_and_exits_with_its_own_failure_detail(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        self.server.requests.clear()
        self.outcomes = ["failed"]
        new, _change, _release = self.prepare_post_turn_home_change(("failure peer message",))
        with patch.object(codex_mesh, "_post_board_status") as board:
            code, errors = self.run_follow()
        self.assertEqual(code, 1)
        self.assertIn("ordinary failure", errors)
        self.assertNotIn("state unknown", errors)
        self.assertEqual(self.worker_record()["status"], "failed")
        self.assert_post_turn_messages_held(new, ("failure peer message",), board)

    def test_follow_after_hold_submits_prompt_then_held_messages_once_and_empties_inbox(self) -> None:
        messages = ("first held message", "second held message")
        new, _change, release = self.prepare_post_turn_home_change(messages)
        with patch.object(codex_mesh, "_post_board_status"):
            self.assertEqual(self.run_start(), (0, ""))
        release()
        code, errors = self.run_follow("next launch prompt")
        self.assertEqual((code, errors), (0, ""))
        starts = [request for request in new.requests if request.get("method") == "turn/start"]
        self.assertEqual(len(starts), 1)
        inputs = cast(list[dict[str, object]], _as_test_dict(starts[0].get("params"))["input"])
        self.assertEqual([item["text"] for item in inputs], ["next launch prompt", *messages])
        pending = cast(codex_mesh.WorkerPendingMessages, json.loads((self.session_dir / "worker.pending.json").read_text()))
        self.assertEqual(pending["messages"], [])
        self.assertIs(pending["accepting_messages"], False)
        self.assertEqual(self.worker_record()["status"], "done")

    def assert_accepted_target_turn_write_failure(self, write_site: str) -> None:
        message = "peer message accepted by target"
        new, _change, release = self.prepare_post_turn_home_change((message,))
        release()
        self.assertNotEqual(self.server.port, new.port)
        events: list[str] = []
        closed_ports: list[int] = []
        start_turn = codex_mesh._start_turn  # pyright: ignore[reportPrivateUsage]
        write_pending = codex_mesh._write_pending  # pyright: ignore[reportPrivateUsage]
        update_roster = codex_mesh._update_roster  # pyright: ignore[reportPrivateUsage]
        close = codex_mesh.Client.close
        detail = f"local {write_site} write failed after target accepted turn"

        def accepted(
            client: codex_mesh.Client, args: argparse.Namespace, thread: str, sandbox: str,
            message: str, pending: tuple[str, ...] = (),
        ) -> str:
            turn = start_turn(client, args, thread, sandbox, message, pending)
            if client.port == new.port:
                self.assertTrue(turn, "the target must accept the turn before the write fails")
                events.append("accepted")
            return turn

        def pending(handle: TextIO, thread: str, messages: list[str], *, accepting_messages: bool = True) -> None:
            if write_site == "pending" and events == ["accepted"]:
                self.assertEqual(messages, [])
                events.append("write failed")
                raise OSError(detail)
            write_pending(handle, thread, messages, accepting_messages=accepting_messages)

        def roster(session: str, name: str, fields: codex_mesh.ThreadRecord) -> None:
            if write_site == "roster" and events == ["accepted"]:
                self.assertEqual(fields.get("status"), "running")
                self.assertEqual(fields.get("port"), new.port)
                events.append("write failed")
                raise OSError(detail)
            update_roster(session, name, fields)

        def closed(client: codex_mesh.Client) -> None:
            closed_ports.append(client.port)
            close(client)

        with (
            # This fixture already released the old writer; its confirmation only needs a hang guard.
            patch.object(codex_mesh, "THREAD_RELEASE_TIMEOUT_SECS", TEST_HANG_GUARD_SECS),
            patch.object(codex_mesh, "_start_turn", wraps=accepted),
            patch.object(codex_mesh, "_write_pending", side_effect=pending),
            patch.object(codex_mesh, "_update_roster", side_effect=roster),
            patch.object(codex_mesh.Client, "close", closed),
            patch.object(codex_mesh, "_close_messages_and_keep_pending") as hold,
            patch.object(codex_mesh, "_post_board_status") as board,
            patch.object(codex_mesh, "_end_unwatched_turn", return_value=codex_mesh.RelaunchAllowed()) as cleanup,
        ):
            code, errors = self.run_start()
        self.assertEqual(events, ["accepted", "write failed"],
                         "exactly one write must fail, after target turn acceptance")
        self.assertEqual(code, 1)
        self.assertIn(f"state unknown ({detail})", errors)
        self.assertEqual(self.worker_record()["status"], "failed")
        cleanup.assert_called_once_with(new.port, THREAD_ID, self.session_dir / "worker.log")
        self.assertIn(new.port, closed_ports, "the failed move must close its target client")
        hold.assert_not_called()
        board.assert_not_called()
        self.assertNotIn("held for the next launch", (self.session_dir / "worker.log").read_text())
        self.assertEqual(len(self.methods("turn/start")), 1)
        starts = [request for request in new.requests if request.get("method") == "turn/start"]
        self.assertEqual(len(starts), 1)
        inputs = cast(list[dict[str, object]], _as_test_dict(starts[0].get("params"))["input"])
        self.assertEqual([item["text"] for item in inputs], [message])
        if write_site == "roster":
            stored = cast(codex_mesh.WorkerPendingMessages, json.loads((self.session_dir / "worker.pending.json").read_text()))
            self.assertEqual(stored["messages"], [])
            client = codex_mesh.Client(new.port, "worker")
            try:
                with patch.object(codex_mesh, "_update_roster", side_effect=OSError("idle roster write failed")):
                    result = codex_mesh._move_resident(self.start_args(), str(self.session_dir), "worker", THREAD_ID, client)  # pyright: ignore[reportPrivateUsage]
            finally:
                client.close()
            self.assertIsInstance(result, codex_mesh.WorkerThreadStateUnknown)
            assert isinstance(result, codex_mesh.WorkerThreadStateUnknown)
            self.assertIsInstance(result.cleanup, codex_mesh.NoWorkerTurnToCleanUp)
            self.assertEqual(result.reason, "idle roster write failed")
            self.assertEqual(len([request for request in new.requests if request.get("method") == "turn/start"]), 1)

    def test_accepted_target_turn_pending_write_failure_fails_and_cleans_distinct_target(self) -> None:
        self.assert_accepted_target_turn_write_failure("pending")

    def test_accepted_target_turn_roster_write_failure_fails_and_cleans_distinct_target(self) -> None:
        self.assert_accepted_target_turn_write_failure("roster")

    def test_completed_turn_with_worker_ended_before_move_leaves_pending_untouched_and_reports_no_hold(self) -> None:
        new, _change, _release = self.prepare_post_turn_home_change(("message for ended worker",))
        read = codex_mesh.Client.next_frame
        pending_path = self.session_dir / "worker.pending.json"
        saved_pending = [""]
        writes_after_end: list[str] = []
        write_pending = codex_mesh._write_pending  # pyright: ignore[reportPrivateUsage]

        @bounded_fixture
        def frame(client: codex_mesh.Client, deadline: float) -> codex_mesh.RpcMessage | None:
            notice = read(client, deadline)
            if notice is not None and notice.get("method") == "turn/completed":
                saved_pending[0] = pending_path.read_text()
                _ = (self.session_dir / "worker.end").write_text("end")
            return notice

        def pending(handle: TextIO, thread: str, messages: list[str], *, accepting_messages: bool = True) -> None:
            if (self.session_dir / "worker.end").exists():
                writes_after_end.append(thread)
            write_pending(handle, thread, messages, accepting_messages=accepting_messages)

        with (
            patch.object(codex_mesh.Client, "next_frame", frame),
            patch.object(codex_mesh, "_write_pending", side_effect=pending),
            patch.object(codex_mesh, "_post_board_status") as board,
        ):
            code, errors = self.run_start()
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.worker_record()["status"], "done")
        self.assertEqual((self.session_dir / "summary.txt").read_text().strip(), "phase 13 turn answer")
        self.assertTrue(saved_pending[0], "the worker must end after its message is parked")
        self.assertEqual(writes_after_end, [])
        self.assertEqual(pending_path.read_text(), saved_pending[0])
        board.assert_not_called()
        self.assertNotIn("held for the next launch", (self.session_dir / "worker.log").read_text())
        self.assertFalse(any(request.get("method") in ("thread/resume", "thread/start", "turn/start") for request in new.requests))

    def test_resident_delivered_reply_waits_for_old_writer_and_moves_on_later_tick(self) -> None:
        new, _change, release = self.prepare_post_turn_home_change(())
        args = self.start_args()
        args.resident = True
        delivered = codex_mesh._deliver_reply  # pyright: ignore[reportPrivateUsage]
        move = codex_mesh._move_resident  # pyright: ignore[reportPrivateUsage]
        attempts = [0]

        def retry(args: argparse.Namespace, session: str, name: str, thread: str, client: codex_mesh.Client, *, message: str = "") -> codex_mesh.WorkerMoveResult:
            if self.turn_starts:
                attempts[0] += 1
            if attempts[0] == 3:
                release()
                # Release has happened; confirmation now needs only a hang guard.
                _ = self.patches.enter_context(patch.object(codex_mesh, "THREAD_RELEASE_TIMEOUT_SECS", TEST_HANG_GUARD_SECS))
            result = move(args, session, name, thread, client, message=message)
            if isinstance(result, codex_mesh.MovedWorker) and result.port == new.port:
                _ = (self.session_dir / "worker.end").write_text("end")
            return result

        with patch.object(codex_mesh, "_move_resident", side_effect=retry), patch.object(codex_mesh, "_deliver_reply", wraps=delivered) as replies, contextlib.redirect_stdout(io.StringIO()):
            outcome = codex_mesh._run_worker(args, self.server.port)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(outcome, codex_mesh.RunCompleted)
        replies.assert_called_once()
        self.assertEqual(attempts[0], 3)
        log = (self.session_dir / "worker.log").read_text()
        self.assertEqual(log.count("; waiting"), 2)
        self.assertIn("old server has not released the thread", log)
        self.assertEqual(len([request for request in new.requests if request.get("method") == "thread/resume"]), 1)
        self.assertFalse(any(request.get("method") in ("thread/start", "turn/start") for request in new.requests))
        self.assertEqual(self.turn_starts, 1)
        self.assertEqual(self.worker_record()["status"], "done")

    def test_move_failure_after_thread_replacement_retains_target_cleanup(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        _target, new = self.prepare_workers_move()
        self.assertEqual(accounts.main(["set-role", "workers", "codex1"]), 0)
        replacement = "replacement-thread"
        @bounded_fixture
        def rpc(_client: codex_mesh.Client, method: str, _params: dict[str, object], **_options: object) -> codex_mesh.RpcMessage:
            if method == "thread/read":
                return {"result": {"thread": {"status": {"type": "idle"}}}}
            if method in ("thread/queue/list", "thread/loaded/list"):
                return {"result": {"data": []}}
            if method == "thread/resume":
                return {"error": {"message": "thread cannot resume"}}
            if method == "thread/start":
                return {"result": {"thread": {"id": replacement}}}
            if method == "thread/name/set":
                raise ConnectionError("connection lost after replacement")
            return {"result": {}}
        with patch.object(codex_mesh.Client, "call", autospec=True, side_effect=rpc), patch.object(codex_mesh, "_stop_unused", return_value=False):
            client = codex_mesh.Client(self.server.port, "worker")
            try:
                result = codex_mesh._move_resident(self.start_args(), str(self.session_dir), "worker", THREAD_ID, client)  # pyright: ignore[reportPrivateUsage]
            finally:
                client.close()
        self.assertIsInstance(result, codex_mesh.WorkerThreadStateUnknown)
        assert isinstance(result, codex_mesh.WorkerThreadStateUnknown)
        self.assertEqual(result.cleanup, codex_mesh.WorkerTurnCleanup(new.port, replacement))


@final
class FollowEventRegressionTests(MeshCommandFixture):
    """Real socket/file waits: no next_frame substitution or fabricated quiet wakes."""

    guard_frame_reads: bool = False

    def test_adopted_worker_wakes_a_real_file_wait_and_refuses_old_follow(self) -> None:
        self.write_pool_roster({"worker": {
            "thread_id": THREAD_ID, "status": "done", "role": "impl",
        }})
        destination = self.session_dir.parent / "taking-run"
        destination.mkdir()
        destination_roster = destination / codex_mesh.ROSTER_FILE
        owner = json.dumps({"worker": {"thread_id": THREAD_ID, "role": "impl", "status": "done"}})
        _ = destination_roster.write_text(owner)
        ready = threading.Event()
        with (contextlib.closing(codex_mesh.FollowEvents(str(self.session_dir), "worker")) as events,
              contextlib.closing(codex_mesh.Client(self.server.port, "adoption-wait",
                                                  deadline=time.time() + 4)) as client,
              ThreadPoolExecutor(max_workers=1) as executor):
            def wait_for_handoff() -> None:
                ready.set()
                client.wait_for_activity(events, time.monotonic() + 3)

            future = executor.submit(wait_for_handoff)
            self.assertTrue(ready.wait(timeout=1))
            record = self.worker_record()
            # Change only ownership: a status change alone would let this test
            # pass even if the event filter forgot adopted_to.
            record["adopted_to"] = str(destination)
            self.write_pool_roster({"worker": record})
            _ = future.result(timeout=1)
        before = (self.session_dir / codex_mesh.ROSTER_FILE).read_bytes()
        try:
            code, output, errors = self.run_mesh(self.follow_cli())
        except SystemExit as exc:
            code, output, errors = 2, "", str(exc)
        self.assertNotEqual(code, 0, output + errors)
        self.assertIn("adopt", (output + errors).lower())
        self.assertEqual((self.session_dir / codex_mesh.ROSTER_FILE).read_bytes(), before)
        self.assertEqual(destination_roster.read_text(), owner)
        self.assertEqual(self.methods("turn/start"), [])
        self.assertEqual(self.methods("thread/resume"), [])

    def test_an_attached_follow_stops_on_handoff_without_overwriting_the_new_owner(self) -> None:
        state = {"status": "active", "production": "fixture-production", "unit": "models",
                 "working_dir": str(self.session_dir.parent)}
        _ = (self.session_dir / plan_history.STATE_FILENAME).write_text(json.dumps(state))
        self.patches.enter_context(patch.dict(os.environ, {
            "UNIT_WORKERS_STATE": str(self.session_dir.parent / "unit-workers"),
        }))
        self.assertEqual(self.run_start(), (0, ""))
        self.start_args_timeout = 4
        self.outcomes = ["disconnect"]
        self.server.hold_turn_completion = True
        self.server.answer_requests_during_held_turn = True
        respond = self.respond

        @bounded_fixture
        def keep_submitted_turn_live(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "turn/start":
                self.live_turn = f"turn-{self.turn_starts + 1}"
            return respond(request)

        self.server.respond = keep_submitted_turn_live
        destination = self.session_dir.parent / "new-owner"
        destination.mkdir()
        _ = (destination / plan_history.STATE_FILENAME).write_text(json.dumps(state))
        owner_path = destination / codex_mesh.ROSTER_FILE
        owner = json.dumps({"worker": {"thread_id": THREAD_ID, "status": "running",
                                       "role": "impl", "turn_id": "new-owner-turn"}})
        _ = owner_path.write_text(owner)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.run_follow)
            try:
                self.assertTrue(self.server.held_turn_ready.wait(timeout=4))
                # A recovering handoff can fence an old launcher after its
                # original idle precondition was checked. Only ownership
                # changes, so the watcher must observe adopted_to itself.
                record = self.worker_record()
                store = unit_workers.ledger_for_run(destination)
                with store.locked() as ledger:
                    ledger.workers["worker"] = unit_workers.UnitWorker(
                        "worker", "impl", "codex", "new-owner-model", "high",
                        {"kind": "WorkerLaunchPolicy"},
                        unit_workers.CodexThread(THREAD_ID, str(self.session_dir.parent / "new-owner-home")),
                        unit_workers.Adopted(str(destination)),
                    )
                ledger_before = store.path.read_bytes()
                record["adopted_to"] = str(destination)
                record["unit_ledger"] = str(store.path)
                self.write_pool_roster({"worker": record})
                fenced = (self.session_dir / codex_mesh.ROSTER_FILE).read_bytes()
                code, errors = future.result(timeout=1)
            finally:
                self.live_turn = ""
                self.server.release_held_turn.set()
        self.assertNotEqual(code, 0, "a handed-off turn must not be reported as completed")
        self.assertIn(str(destination), errors)
        self.assertEqual((self.session_dir / codex_mesh.ROSTER_FILE).read_bytes(), fenced)
        self.assertEqual(owner_path.read_text(), owner)
        self.assertEqual(store.path.read_bytes(), ledger_before, "the old launcher changed the unit's owner")
        self.assertEqual(self.methods("turn/interrupt"), [])
        self.assertEqual(self.turn_starts, 2)

    def test_handoff_drains_buffered_notices_before_reporting_an_incomplete_assignment(self) -> None:
        self.server.coalesce_response_frames = True
        respond = self.respond

        @bounded_fixture
        def buffered_answer(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "test/answer-before-handoff":
                return [
                    _notice("item/completed", {"threadId": THREAD_ID,
                        "item": {"type": "agentMessage", "text": "answer buffered before handoff"}}),
                    _notice("account/rateLimits/updated", {"rateLimits": {}}),
                    _reply(request, {}),
                ]
            return respond(request)

        self.server.respond = buffered_answer
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "running",
                                           "adopted_to": str(self.session_dir.parent / "owner")}})
        before = (self.session_dir / codex_mesh.ROSTER_FILE).read_bytes()
        with contextlib.closing(codex_mesh.Client(self.server.port, "worker", deadline=time.time() + 4)) as client:
            _ = client.call("test/answer-before-handoff", {})
            self.assertTrue(client.has_pending_frames)
            stream = self.stream_for(client)
            log = io.StringIO()
            step = codex_mesh._on_quiet_poll(stream, log)  # pyright: ignore[reportPrivateUsage]
            self.assertIs(step, codex_mesh.StreamStep.NEXT_FRAME)
            _ = codex_mesh._stream_frames(stream, log)  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(stream.final_answer, "answer buffered before handoff")
            self.assertFalse(client.has_pending_frames)
            outcome = codex_mesh._finish_stream(stream)  # pyright: ignore[reportPrivateUsage]
            self.assertIsInstance(outcome, codex_mesh.WorkerHandedOff)
        self.assertEqual((self.session_dir / codex_mesh.ROSTER_FILE).read_bytes(), before)
        self.assertEqual(self.methods("turn/interrupt"), [])

    def test_submission_wait_preserves_buffered_and_new_peer_notices_without_self_waking(self) -> None:
        for failed in (False, True):
            with self.subTest(failed=failed):
                self.assert_submission_wait_preserves_peer_notices(failed=failed)

    def assert_submission_wait_preserves_peer_notices(self, *, failed: bool) -> None:
        self.server.coalesce_response_frames = True
        respond = self.respond
        notices = [
            _notice("turn/started", {"threadId": THREAD_ID, "turn": {"id": "queued-peer"}}),
            _notice("item/completed", {"threadId": THREAD_ID, "turnId": "queued-peer",
                "item": {"type": "agentMessage", "text": "queued peer answer"}}),
            _notice("turn/failed" if failed else "turn/completed", {
                "threadId": THREAD_ID, "turn": {"id": "queued-peer", "status": "failed" if failed else "completed"},
                **({"error": {"message": "queued peer failed"}} if failed else {}),
            }),
        ]

        @bounded_fixture
        def queued_peer(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "test/queued-peer":
                # Two notices are buffered by the RPC; the third arrives as
                # unread WebSocket bytes after its reply.
                return [*notices[:2], _reply(request, {}), notices[2]]
            if request.get("method") == "test/drain-peer":
                return [_reply(request, {})]
            return respond(request)

        self.server.respond = queued_peer
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "running"}})
        attempts = 0
        wake_marker = self.session_dir / "worker.pending.json"
        activity_wait = codex_mesh.FollowEvents.wait
        wakes: list[codex_mesh.FileWaitResult] = []

        def wake_submission(events: codex_mesh.FollowEvents, connection: socket.socket,
                            deadline: float) -> codex_mesh.FileWaitResult:
            self.assertEqual(attempts, 2)
            wake_marker.touch()
            result = activity_wait(events, connection, deadline)
            wakes.append(result)
            self.assertIsInstance(result, codex_mesh.StateFilesChanged)
            assert isinstance(result, codex_mesh.StateFilesChanged)
            self.assertIn(wake_marker, result.paths)
            return result

        with contextlib.closing(codex_mesh.Client(
                self.server.port, "worker", deadline=time.time() + TEST_HANG_GUARD_SECS)) as client:
            @bounded_fixture
            def move(
                _args: argparse.Namespace, _session: str, _name: str, _thread: str, moving: codex_mesh.Client,
                *, message: str,
            ) -> codex_mesh.WorkerMoveResult:
                nonlocal attempts
                self.assertEqual(message, "resume after overflow")
                self.assertIs(moving, client)
                attempts += 1
                if attempts == 1:
                    _ = client.call("test/queued-peer", {})
                    return codex_mesh.WorkerWaitingOnLiveTurn()
                if attempts == 2:
                    _ = client.call("test/drain-peer", {})
                    return codex_mesh.WorkerWaitingOnLiveTurn()
                self.assertEqual(attempts, 3, "retained notices caused a busy submission retry")
                self.assertEqual(len(wakes), 1, "pending notices bypassed the wait for new activity")
                return codex_mesh.MovedWorker(client, self.server.port, "queued-peer", THREAD_ID, False)

            args = self.start_args()
            args.timeout = TEST_HANG_GUARD_SECS
            with (patch.object(codex_mesh, "_move_resident", new=move),
                  patch.object(codex_mesh.FollowEvents, "wait", new=wake_submission)):
                _ = codex_mesh._start_workers_turn(  # pyright: ignore[reportPrivateUsage]
                    args, str(self.session_dir), "worker", THREAD_ID, client, "resume after overflow")
            self.assertEqual(attempts, 3)
            self.assertEqual(len(wakes), 1)
            stream = self.stream_for(client)
            stream.follow = False
            stream.followed_turn = ""
            log = io.StringIO()
            with patch.object(codex_mesh, "_after_turn_end", return_value=codex_mesh.StreamStep.FINISH):
                for expected in notices:
                    frame = client.next_frame(time.time() + TEST_HANG_GUARD_SECS)
                    self.assertEqual(frame, expected, "submission wait lost or reordered a peer notice")
                    assert frame is not None
                    _ = codex_mesh._on_frame(stream, log, frame)  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(stream.turn_id, "queued-peer")
            self.assertEqual(self.worker_record()["turn_id"], "queued-peer")
            self.assertEqual(stream.final_answer, "queued peer answer")
            self.assertIn("queued peer answer", log.getvalue())
            self.assertEqual(len(stream.reported_turns), 1 if failed else 0)

    def test_idle_readback_rechecks_after_buffered_rate_notice_without_another_timeout(self) -> None:
        for resident in (False, True):
            with self.subTest(resident=resident):
                self.assert_idle_readback_rechecks_after_notice(resident=resident)

    def assert_idle_readback_rechecks_after_notice(self, *, resident: bool) -> None:
        self.server.coalesce_response_frames = True
        respond = self.respond
        buffered = False

        @bounded_fixture
        def rate_notice_before_idle_reply(request: dict[str, object]) -> list[dict[str, object]] | None:
            nonlocal buffered
            frames = respond(request)
            if request.get("method") == "thread/read" and not buffered:
                buffered = True
                assert frames is not None
                return [_notice("account/rateLimits/updated", {"rateLimits": {}}), *frames]
            return frames

        self.server.respond = rate_notice_before_idle_reply
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "done" if resident else "running"}})
        with contextlib.closing(codex_mesh.Client(
                self.server.port, "worker", deadline=time.time() + TEST_HANG_GUARD_SECS)) as client:
            stream = self.stream_for(client, resident=resident)
            stream.timeout = TEST_HANG_GUARD_SECS
            stream.deadline = time.time() + TEST_HANG_GUARD_SECS
            stream.final_answer = "answer without completion"
            # The answer arrived, so the turn's notices did too.
            stream.turn_start_seen = True
            stream.state_change_owed = True
            next_frame = client.next_frame

            def drain_notice(deadline: float) -> codex_mesh.RpcMessage | None:
                self.assertTrue(client.has_pending_frames,
                                "draining the rate notice postponed the idle check until another read")
                frame = next_frame(deadline)
                self.assertIsNotNone(frame)
                assert frame is not None
                self.assertEqual(frame.get("method"), "account/rateLimits/updated")
                return frame

            with patch.object(client, "next_frame", wraps=drain_notice) as reads:
                _ = codex_mesh._stream_frames(stream, io.StringIO())  # pyright: ignore[reportPrivateUsage]
            reads.assert_called_once()
            self.assertTrue(buffered)
            self.assertEqual(stream.failure, "")

    def stream_for(self, client: codex_mesh.Client, *, resident: bool = False) -> codex_mesh.TurnStream:
        client.session_dir = str(self.session_dir)
        args = self.start_args()
        args.resident = resident
        # The stand-in server started the watched turn; a turn read lists it unless it is live.
        self.started_turns["buffered-turn"] = "completed"
        return codex_mesh.TurnStream(
            args=args, session_dir=str(self.session_dir), name="worker", follow=True, resident=resident,
            timeout=3, opened=time.time(), deadline=time.time() + 3,
            log_path=self.session_dir / "worker.log", summary_path=self.session_dir / "summary.txt",
            reply_path=None, end_marker=self.session_dir / "worker.end",
            director=codex_mesh.UnknownWorkerDirector(), client=client, port=self.server.port,
            thread_id=THREAD_ID, failure_serving=client.serving,
            runs=codex_mesh.DetachedRunWatch(str(self.session_dir), "worker"),
            turn_id="buffered-turn", followed_turn="buffered-turn",
        )

    def test_due_run_scan_drains_raw_buffered_completion_before_its_next_wait(self) -> None:
        self.server.coalesce_response_frames = True
        respond = self.respond

        @bounded_fixture
        def buffer_completion(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "test/buffer-completion":
                return [_reply(request, {}),
                    _notice("item/completed", {"threadId": THREAD_ID,
                        "item": {"type": "agentMessage", "text": "buffered answer"}}),
                    _notice("turn/completed", {"threadId": THREAD_ID,
                        "turn": {"id": "buffered-turn", "status": "completed"}})]
            return respond(request)

        self.server.respond = buffer_completion
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "running"}})
        frame_handler = codex_mesh._on_frame  # pyright: ignore[reportPrivateUsage]
        budget = FixtureLoopBudget("due scan frame handling", seconds=4, max_calls=8)
        due = 0.0

        def expire_scan_after_answer(
            stream: codex_mesh.TurnStream, log: TextIO, frame: codex_mesh.RpcMessage,
        ) -> codex_mesh.StreamStep | codex_mesh.FailedWithThread:
            nonlocal due
            _ = budget.check()
            step = frame_handler(stream, log, frame)
            if frame.get("method") == "item/completed":
                # These are actual WebSocket bytes, not a pushed-back notice.
                self.assertEqual(stream.client._pending, [])  # pyright: ignore[reportPrivateUsage]
                self.assertTrue(stream.client._ws._buffer)  # pyright: ignore[reportPrivateUsage]
                due = time.monotonic() - 0.01
                stream.runs.next_check = due
            return step

        with contextlib.closing(codex_mesh.Client(self.server.port, "worker", deadline=time.time() + 4)) as client:
            _ = client.call("test/buffer-completion", {})
            stream = self.stream_for(client)
            with (contextlib.closing(codex_mesh.FollowEvents(str(self.session_dir), "worker")) as events,
                  patch.object(codex_mesh, "_on_frame", expire_scan_after_answer)):
                stream.events = events
                result = codex_mesh._stream_frames(stream, io.StringIO())  # pyright: ignore[reportPrivateUsage]
            self.assertIsNone(result)
            self.assertEqual(stream.failure, "")
            self.assertEqual(stream.final_answer, "buffered answer")
            self.assertGreater(stream.runs.next_check, due + 1)
            self.assertIsInstance(codex_mesh._finish_stream(stream), codex_mesh.RunCompleted)  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(self.worker_record()["status"], "done")

    def assert_retry_does_not_wake_itself(self, *, room_wait: bool) -> None:
        self.start_args_timeout = 3
        if room_wait:
            account = self.prepare_refusing_account(stub_ensure=False)
        else:
            account = agent_accounts.codex_account(agent_accounts.workers_home())
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "running",
            "port": self.server.port, "continuation_owed": True}})
        pending = self.session_dir / "worker.pending.json"
        _ = pending.write_text(json.dumps({"thread_id": THREAD_ID, "messages": [], "accepting_messages": False}))
        first_pass = threading.Event()
        repeated = threading.Event()
        quiet = codex_mesh._on_quiet_poll  # pyright: ignore[reportPrivateUsage]
        budget = FixtureLoopBudget("owed retry quiet passes", seconds=5, max_calls=8)
        passes = 0

        def observed_quiet(stream: codex_mesh.TurnStream, log: TextIO) -> codex_mesh.StreamStep | codex_mesh.FailedWithThread:
            nonlocal passes
            _ = budget.check()
            step = quiet(stream, log)
            passes += 1
            if passes == 1:
                first_pass.set()
            else:
                repeated.set()
            return step

        def no_refusals() -> dict[str, refusals.AccountRefusal]:
            return {}

        with contextlib.ExitStack() as stack:
            client = stack.enter_context(contextlib.closing(
                codex_mesh.Client(self.server.port, "worker", deadline=time.time() + 4)))
            client.serving = codex_mesh.ServingCodexServer(self.server.port, os.getpid(), account.home, account)
            stream = self.stream_for(client, resident=room_wait)
            stream.state_change_owed = True
            events = stack.enter_context(contextlib.closing(codex_mesh.FollowEvents(str(self.session_dir), "worker")))
            stream.events = events
            _ = stack.enter_context(patch.object(codex_mesh, "_on_quiet_poll", observed_quiet))
            if room_wait:
                _ = stack.enter_context(patch.object(refusals, "load", new=no_refusals))
                # These mocks are scoped to this test's eight-pass budget.
                _ = stack.enter_context(patch.object(refusals, "stored_reading", return_value=refusals.NoReading()))
                # The fake server uses this process's PID, not a native Codex
                # process with an inherited credential or dev-shell environment.
                _ = stack.enter_context(patch.object(codex_mesh, "_holds_old_sign_in", return_value=False))
                _ = stack.enter_context(patch.object(codex_mesh, "_server_dev_shell",
                    return_value=codex_mesh.InheritedServerEnvironment()))
                # Reusing the fixture port must exercise ensure_server's real utime.
                _ = stack.enter_context(patch.object(codex_mesh, "_start_server",
                    side_effect=AssertionError("room wait attempted to launch a real server")))
            else:
                _ = stack.enter_context(patch.object(codex_mesh, "_move_resident",
                    new=bounded_fixture(lambda *_args: codex_mesh.WorkerThreadStateUnknown("readback unavailable"))))
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(codex_mesh._stream_frames, stream, io.StringIO())  # pyright: ignore[reportPrivateUsage]
                try:
                    self.assertTrue(first_pass.wait(timeout=1), "owed retry never ran")
                    self.assertFalse(repeated.wait(timeout=0.3), "retry woke itself without new work")
                    self.assertFalse(future.done(), "owed continuation was abandoned")
                finally:
                    # Cleanup must also stop the one-turn quota wait, whose
                    # end-marker check follows its normal lifecycle deadline.
                    stream.deadline = time.time()
                    _ = stream.end_marker.write_text("end")
                _ = future.result(timeout=3)
        self.assertEqual(self.turn_starts, 0)
        self.assertLess(len(self.server.requests), 16, "self wakes accumulated RPCs")

    def test_resident_room_retry_does_not_wake_on_its_own_server_utime(self) -> None:
        self.assert_retry_does_not_wake_itself(room_wait=True)

    def test_quota_owed_retry_does_not_wake_on_an_identical_closed_inbox_write(self) -> None:
        self.assert_retry_does_not_wake_itself(room_wait=False)

    def assert_trailing_status_finishes_promptly(self, *, failed: bool) -> None:
        self.start_args_timeout = TEST_HANG_GUARD_SECS
        self.server.coalesce_response_frames = True
        respond = self.respond

        @bounded_fixture
        def trailing_status(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") != "turn/start":
                return respond(request)
            self.outcomes = ["disconnect"]
            frames = respond(request)
            assert frames is not None
            turn: dict[str, object] = {"id": f"turn-{self.turn_starts}", "status": "completed"}
            if failed:
                turn.update({"status": "failed", "error": {"message": "ordinary failure"}})
            return [*frames,
                _notice("item/completed", {"threadId": THREAD_ID,
                    "item": {"type": "agentMessage", "text": "answer before trailing status"}}),
                _notice("turn/completed", {"threadId": THREAD_ID, "turn": turn}),
                _notice("thread/status/changed", {"threadId": THREAD_ID, "status": {"type": "idle"}}),
                _notice("account/rateLimits/updated", {"rateLimits": {}})]

        self.server.respond = trailing_status
        check_more_work = codex_mesh._more_work_coming  # pyright: ignore[reportPrivateUsage]
        finish_stream = codex_mesh._finish_stream  # pyright: ignore[reportPrivateUsage]
        next_frame = codex_mesh.Client.next_frame
        read = codex_mesh.Client._read  # pyright: ignore[reportPrivateUsage]
        grace_active = False
        grace_checked = False
        frame_read_active = False
        grace_run_deadline = 0.0
        opened_windows = 0
        test = self
        # Adding the real interval opens a grace window; notices must not open
        # another. Counting additions tolerates rounding and early file wakes.
        class SingleGraceWindow(float):
            @override
            def __radd__(self, opened: float, /) -> float:
                nonlocal opened_windows
                opened_windows += 1
                test.assertEqual(opened_windows, 1, "a notice opened another grace window")
                return opened + float(self)

        def observed_more_work(client: codex_mesh.Client, thread_id: str, deadline: float) -> bool:
            nonlocal grace_active, grace_checked, grace_run_deadline
            grace_run_deadline = deadline
            grace_active = True
            try:
                result = check_more_work(client, thread_id, deadline)
            finally:
                grace_active = False
            grace_checked = True
            self.assertFalse(result, "trailing notices were counted as another turn")
            return result

        def observed_next_frame(client: codex_mesh.Client, read_until: float) -> codex_mesh.RpcMessage | None:
            nonlocal frame_read_active
            if grace_active:
                self.assertEqual(opened_windows, 1, "grace skipped its window and waited until the run deadline")
                self.assertLess(read_until, grace_run_deadline, "grace waited until the run deadline")
            elif grace_checked:
                self.assertTrue(client.has_pending_frames, "stream waited again after the notices drained")
            frame_read_active = True
            try:
                return next_frame(client, read_until)
            finally:
                frame_read_active = False

        def observed_read(
            client: codex_mesh.Client, deadline: float,
            wake: codex_mesh.FollowEvents | codex_mesh.SocketOnlyWait = codex_mesh.SOCKET_ONLY_WAIT,
        ) -> codex_mesh.RpcMessage | None:
            frame = read(client, deadline, wake)
            if grace_active and frame is None:
                self.assertTrue(frame_read_active, "grace bypassed next_frame for a blocking read")
            return frame

        def observed_finish(stream: codex_mesh.TurnStream) -> codex_mesh.RunOutcome:
            self.assertLess(time.time(), stream.deadline, "run finished at its hang guard")
            return finish_stream(stream)

        with (
            patch.object(codex_mesh, "QUEUE_GRACE_SECS", SingleGraceWindow(0.05)),
            patch.object(codex_mesh, "_more_work_coming", wraps=observed_more_work) as more_work,
            patch.object(codex_mesh.Client, "next_frame", new=observed_next_frame),
            patch.object(codex_mesh.Client, "_read", new=observed_read),
            patch.object(codex_mesh, "_finish_stream", wraps=observed_finish) as finish,
        ):
            code, errors = self.run_start()
        more_work.assert_called_once()
        self.assertEqual(opened_windows, 1)
        finish.assert_called_once()
        self.assertEqual(code, 1 if failed else 0, errors)
        self.assertEqual(self.worker_record()["status"], "failed" if failed else "done")
        if failed:
            self.assertIn("ordinary failure", errors)
        self.assertNotIn("no turn/completed", errors)

    def test_trailing_status_notices_do_not_delay_one_turn_exit(self) -> None:
        self.assert_trailing_status_finishes_promptly(failed=False)

    def test_failed_turn_with_answer_and_trailing_status_is_recorded_failed(self) -> None:
        self.assert_trailing_status_finishes_promptly(failed=True)

    def assert_inbox_stays_open_for_late_run(self, *, resident: bool) -> None:
        self.start_args_timeout = 4
        self.assertEqual(self.run_start(), (0, ""))
        self.outcomes = ["answered_without_completion"]
        created = threading.Event()
        checked = threading.Event()
        frame_handler = codex_mesh._on_frame  # pyright: ignore[reportPrivateUsage]
        quiet = codex_mesh._on_quiet_poll  # pyright: ignore[reportPrivateUsage]
        budget = FixtureLoopBudget("new detached run discovery", seconds=6, max_calls=24)
        run = self.session_dir / codex_mesh.DETACHED_RUNS_DIR / "new.json"
        run.parent.mkdir()

        def start_run_after_scan(
            stream: codex_mesh.TurnStream, log: TextIO, frame: codex_mesh.RpcMessage,
        ) -> codex_mesh.StreamStep | codex_mesh.FailedWithThread:
            _ = budget.check()
            step = frame_handler(stream, log, frame)
            item = _as_test_dict(_as_test_dict(frame.get("params")).get("item"))
            if item.get("type") == "agentMessage" and not created.is_set():
                self.assertFalse(stream.runs.last_observed.live)
                self.assertGreater(stream.runs.next_check, time.monotonic())
                _ = run.write_text(json.dumps({"worker": "worker", "status": "running", "pid": os.getpid()}))
                pending = self.session_dir / "worker.pending.json"
                _ = pending.write_text(pending.read_text() + "\n")
                if resident:
                    record = self.worker_record()
                    record["status"] = "done"
                    self.write_pool_roster({"worker": record})
                created.set()
            return step

        def observe_discovery(stream: codex_mesh.TurnStream, log: TextIO) -> codex_mesh.StreamStep | codex_mesh.FailedWithThread:
            _ = budget.check()
            step = quiet(stream, log)
            if created.is_set():
                checked.set()
            return step

        args = self.start_args()
        args.resident = resident
        with (patch.object(self, "start_args", new=lambda: args),
              patch.object(codex_mesh, "_on_frame", start_run_after_scan),
              patch.object(codex_mesh, "_on_quiet_poll", observe_discovery),
              ThreadPoolExecutor(max_workers=1) as executor):
            future = executor.submit(self.run_follow)
            try:
                # Submission/setup time is separate from the file-wake bound below.
                self.assertTrue(created.wait(timeout=3), "answer never started the run fixture")
                self.assertTrue(checked.wait(timeout=1), "file event never checked the new run")
                self.assertFalse(future.done(), "launcher abandoned a detached run created after its scan")
                inbox = _as_test_dict(cast(object, json.loads((self.session_dir / "worker.pending.json").read_text())))
                self.assertIs(inbox.get("accepting_messages"), True)
            finally:
                _ = run.write_text('{"worker":"worker","status":"finished"}')
                if resident:
                    _ = (self.session_dir / "worker.end").write_text("end")
                else:
                    with contextlib.redirect_stdout(io.StringIO()):
                        _ = codex_mesh.command_send(argparse.Namespace(session_dir=str(self.session_dir),
                            to="worker", message="detached run result", message_file=""))
            code, errors = future.result(timeout=3)
        self.assertEqual((code, errors), (0, ""))
        # A full-history read queues a notice that kept the idle check from
        # ever reaching delivery.
        self.assertFalse([read for read in self.methods("thread/read")
                          if _as_test_dict(read["params"]).get("includeTurns")])
        if not resident:
            self.assertEqual(self.turn_inputs[-1], "detached run result")
        self.assertTrue(run.exists(), "run discovery removed a lookup record")

    def test_last_detached_run_result_turn_is_watched_not_interrupted(self) -> None:
        self.start_args_timeout = 60
        self.assertEqual(self.run_start(), (0, ""))
        self.outcomes = ["completed", "listed_before_notices"]
        run = self.session_dir / codex_mesh.DETACHED_RUNS_DIR / "last.json"
        run.parent.mkdir()
        _ = run.write_text(json.dumps({"worker": "worker", "status": "running", "pid": os.getpid()}))
        held_on_run = threading.Event()
        polled_result_turn = threading.Event()
        steps: list[codex_mesh.StreamStep | codex_mesh.FailedWithThread] = []
        waiting = codex_mesh._waiting_on_detached_run  # pyright: ignore[reportPrivateUsage]
        quiet = codex_mesh._on_quiet_poll  # pyright: ignore[reportPrivateUsage]
        budget = FixtureLoopBudget("last run result turn", seconds=20, max_calls=200)

        def note_wait(session_dir: str, name: str, thread_id: str, runs: codex_mesh.DetachedRunWatch) -> bool:
            held = waiting(session_dir, name, thread_id, runs)
            if held:
                held_on_run.set()
            return held

        def note_result_turn_poll(
            stream: codex_mesh.TurnStream, log: TextIO,
        ) -> codex_mesh.StreamStep | codex_mesh.FailedWithThread:
            _ = budget.check()
            watched = stream.turn_id
            step = quiet(stream, log)
            if self.server.held_turn_ready.is_set() and watched == f"turn-{self.turn_starts}":
                steps.append(step)
                polled_result_turn.set()
            return step

        with (patch.object(codex_mesh, "_waiting_on_detached_run", note_wait),
              patch.object(codex_mesh, "_on_quiet_poll", note_result_turn_poll),
              ThreadPoolExecutor(max_workers=1) as executor):
            future = executor.submit(self.run_follow)
            try:
                self.assertTrue(held_on_run.wait(timeout=5), "launcher never waited on the live run")
                self.server.hold_turn_completion = True
                self.server.answer_requests_during_held_turn = True
                self.server.held_turn_completes_successfully = True
                _ = run.write_text(json.dumps({"worker": "worker", "status": "finished", "pid": os.getpid()}))
                held = codex_mesh.hold_message(str(self.session_dir), "worker", "detached run result",
                                               without_launcher=False)
                self.assertIsInstance(held, codex_mesh.HeldForLauncher)
                self.assertTrue(self.server.held_turn_ready.wait(timeout=5), "the held result opened no turn")
                self.assertTrue(polled_result_turn.wait(timeout=5), "no lifecycle check ran on the result turn")
                self.assertNotIn(codex_mesh.StreamStep.FINISH, steps)
                self.assertFalse(future.done(), "launcher left the result turn before its completion")
            finally:
                self.server.release_held_turn.set()
            code, errors = future.result(timeout=10)
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.methods("turn/interrupt"), [])
        self.assertEqual(self.turn_inputs[-1], "detached run result")
        log = (self.session_dir / "worker.log").read_text() if (self.session_dir / "worker.log").exists() else ""
        self.assertNotIn("no completion notice arrived", log)

    def test_a_result_held_past_the_wall_clock_deadline_opens_the_next_turn(self) -> None:
        self.start_args_timeout = TEST_HANG_GUARD_SECS
        self.assertEqual(self.run_start(), (0, ""))
        self.outcomes = ["completed", "completed"]
        run = self.session_dir / codex_mesh.DETACHED_RUNS_DIR / "late.json"
        run.parent.mkdir()
        _ = run.write_text(json.dumps({"worker": "worker", "status": "running", "pid": os.getpid()}))
        held_on_run = threading.Event()
        clock_jumped = threading.Event()
        waiting = codex_mesh._waiting_on_detached_run  # pyright: ignore[reportPrivateUsage]
        quiet = codex_mesh._on_quiet_poll  # pyright: ignore[reportPrivateUsage]
        budget = FixtureLoopBudget("held result past the deadline", seconds=TEST_HANG_GUARD_SECS, max_calls=200)

        def note_wait(session_dir: str, name: str, thread_id: str, runs: codex_mesh.DetachedRunWatch) -> bool:
            held = waiting(session_dir, name, thread_id, runs)
            if held:
                held_on_run.set()
            return held

        def jump_wall_clock(
            stream: codex_mesh.TurnStream, log: TextIO,
        ) -> codex_mesh.StreamStep | codex_mesh.FailedWithThread:
            _ = budget.check()
            step = quiet(stream, log)
            if held_on_run.is_set() and not clock_jumped.is_set():
                self.assertEqual(json.loads(run.read_text())["status"], "running",
                                 "the result was published before the deadline jump")
                # A suspended machine moves wall time past the deadline while
                # the frame loop's monotonic clock keeps it running.
                stream.deadline = time.time() - 1
                clock_jumped.set()
            return step

        with (patch.object(codex_mesh, "_waiting_on_detached_run", note_wait),
              patch.object(codex_mesh, "_on_quiet_poll", jump_wall_clock),
              ThreadPoolExecutor(max_workers=1) as executor):
            future = executor.submit(self.run_follow)
            self.assertTrue(held_on_run.wait(timeout=TEST_HANG_GUARD_SECS),
                            "launcher never waited on the live run")
            # The live-run check can precede the quiet poll that applies the jump.
            (self.session_dir / "worker.pending.json").touch()
            self.assertTrue(clock_jumped.wait(timeout=TEST_HANG_GUARD_SECS),
                            "launcher never observed the deadline jump")
            held = codex_mesh.hold_message(str(self.session_dir), "worker", "detached run result",
                                           without_launcher=False)
            self.assertIsInstance(held, codex_mesh.HeldForLauncher)
            # Keep the inbox owned until its result is held, then announce run completion.
            _ = run.write_text(json.dumps({"worker": "worker", "status": "finished", "pid": os.getpid()}))
            (self.session_dir / "worker.pending.json").touch()
            code, errors = future.result(timeout=TEST_HANG_GUARD_SECS)
        self.assertTrue(clock_jumped.is_set())
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.turn_inputs[-1], "detached run result")

    def test_missing_completion_keeps_the_inbox_open_for_a_new_detached_run(self) -> None:
        self.assert_inbox_stays_open_for_late_run(resident=False)

    def test_terminal_resident_keeps_the_inbox_open_for_a_new_detached_run(self) -> None:
        self.assert_inbox_stays_open_for_late_run(resident=True)

    def assert_event_driven_follow(self) -> None:
        """Observe real socket and marker waits while a turn stays live."""
        self.start_args_timeout = 8
        self.assertEqual(self.run_start(), (0, ""))
        self.outcomes = ["disconnect"]
        self.server.hold_turn_completion = True
        self.server.hold_turn_timeout = 8
        self.server.answer_requests_during_held_turn = True
        self.server.held_turn_completes_successfully = True
        ready = threading.Event()
        finished = threading.Event()
        marker_seen = threading.Event()
        quiet_wakes: list[float] = []
        thread_reads: list[float] = []
        respond = self.respond
        on_frame = codex_mesh._on_frame  # pyright: ignore[reportPrivateUsage]
        on_quiet = codex_mesh._on_quiet_poll  # pyright: ignore[reportPrivateUsage]
        call = codex_mesh.Client.call
        budget = FixtureLoopBudget("real streaming follow", seconds=12, max_calls=128)

        def measure_stream_submission(
            client: codex_mesh.Client, method: str, params: dict[str, object],
            timeout: float = codex_mesh.CALL_TIMEOUT_SECS,
        ) -> codex_mesh.RpcMessage:
            _ = budget.check()
            return call(client, method, params, timeout=timeout)

        def held_response(request: dict[str, object]) -> list[dict[str, object]] | None:
            _ = budget.check()
            if request.get("method") == "thread/read":
                thread_reads.append(time.monotonic())
            frames = respond(request)
            if request.get("method") == "turn/start" and frames is not None:
                self.live_turn = f"turn-{self.turn_starts}"
                frames.append(_notice("item/completed", {
                    "threadId": THREAD_ID, "turnId": self.live_turn,
                    "item": {"id": "wait-ready", "type": "agentMessage", "text": "stream complete"},
                }))
            return frames

        def observed_frame(
            stream: codex_mesh.TurnStream, log: TextIO, frame: codex_mesh.RpcMessage,
        ) -> codex_mesh.StreamStep | codex_mesh.FailedWithThread:
            _ = budget.check()
            step = on_frame(stream, log, frame)
            item = _as_test_dict(_as_test_dict(frame.get("params")).get("item"))
            if item.get("id") == "wait-ready":
                ready.set()
            return step

        def observed_quiet(stream: codex_mesh.TurnStream, log: TextIO) -> codex_mesh.StreamStep | codex_mesh.FailedWithThread:
            _ = budget.check()
            quiet_wakes.append(time.monotonic())
            marker_seen.set()
            return on_quiet(stream, log)

        def follow() -> tuple[int, str]:
            try:
                return self.run_follow()
            finally:
                finished.set()

        self.server.respond = held_response
        with (
            patch.object(codex_mesh, "_on_frame", observed_frame),
            patch.object(codex_mesh, "_on_quiet_poll", observed_quiet),
            patch.object(codex_mesh.Client, "call", measure_stream_submission),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            future = executor.submit(follow)
            try:
                self.assertTrue(ready.wait(timeout=5), "follower never consumed the stream")
                self.assertTrue(self.server.held_turn_ready.wait(timeout=1))
                # Allow only startup events during the first second. A 1s
                # polling regression must then wake during the next two.
                quiet_started = time.monotonic() + 1
                self.assertFalse(finished.wait(timeout=3), "follow abandoned its live turn")
                self.assertEqual([wake for wake in quiet_wakes if wake >= quiet_started], [])
                self.assertEqual([read for read in thread_reads if read >= quiet_started], [])
                marker_seen.clear()
                marker = self.session_dir / "worker.pending.json"
                _ = marker.write_text(marker.read_text() + "\n")
                self.assertTrue(marker_seen.wait(timeout=1), "marker write did not wake follow within 1s")
                self.assertFalse(future.done(), "a marker wake finished an in-flight turn")
            finally:
                self.live_turn = ""
                self.server.release_held_turn.set()
            code, errors = future.result(timeout=3)
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.worker_record()["status"], "done")
        self.assertIn("stream complete", (self.session_dir / "summary.txt").read_text())

    def test_follow_blocks_without_periodic_wakes_and_a_marker_wakes_it(self) -> None:
        self.assert_event_driven_follow()

    def test_file_wake_keeps_completion_frames_received_before_the_idle_readback_reply(self) -> None:
        self.start_args_timeout = 3
        self.assertEqual(self.run_start(), (0, ""))
        self.outcomes = ["disconnect"]
        respond = self.respond
        completion_sent = False

        @bounded_fixture
        def complete_during_readback(request: dict[str, object]) -> list[dict[str, object]] | None:
            nonlocal completion_sent
            if request.get("method") == "thread/read" and self.turn_starts == 2 and not completion_sent:
                completion_sent = True
                self.live_turn = ""
                reply = respond(request)
                assert reply is not None
                return [
                    _notice("item/completed", {
                        "threadId": THREAD_ID, "turnId": "turn-2",
                        "item": {"type": "agentMessage", "text": "answer ahead of idle reply"},
                    }),
                    _notice("turn/completed", {"threadId": THREAD_ID, "turn": {"id": "turn-2"}}),
                    *reply,
                ]
            frames = respond(request)
            if request.get("method") == "turn/start":
                self.live_turn = "turn-2"
                # Guarantee a file event while this turn has no socket notice.
                (self.session_dir / "worker.pending.json").touch()
            return frames

        self.server.respond = complete_during_readback
        self.assertEqual(self.run_follow(), (0, ""))
        self.assertTrue(completion_sent, "file wake never requested idle readback")
        self.assertEqual(self.worker_record()["status"], "done")
        self.assertIn("answer ahead of idle reply", (self.session_dir / "summary.txt").read_text())
        self.assertNotIn("thread is idle and no completion notice arrived",
                         (self.session_dir / "worker.log").read_text())

    def test_follow_consumes_ten_thousand_deltas_with_bounded_work_and_no_sleeps(self) -> None:
        self.start_args_timeout = 60
        self.assertEqual(self.run_start(), (0, ""))
        self.server.coalesce_response_frames = True
        respond = self.respond
        reads = codex_mesh.WebSocket.recv
        next_frame = codex_mesh.Client.next_frame
        quiet = codex_mesh._on_quiet_poll  # pyright: ignore[reportPrivateUsage]
        counts = {"reads": 0, "passes": 0, "quiet": 0}
        budget = FixtureLoopBudget("delta receive work", seconds=60, max_calls=30_300)

        def streamed(request: dict[str, object]) -> list[dict[str, object]] | None:
            frames = respond(request)
            if request.get("method") == "turn/start" and frames is not None:
                frames[-1:-1] = [
                    *(_notice("item/agentMessage/delta", {"threadId": THREAD_ID,
                        "delta": "x" * 600}) for _index in range(10_000)),
                    _notice("item/completed", {"threadId": THREAD_ID,
                        "item": {"type": "agentMessage", "text": "stream complete"}})]
            return frames

        def read(ws: codex_mesh.WebSocket, wake: codex_mesh.FollowEvents | codex_mesh.SocketOnlyWait = codex_mesh.SOCKET_ONLY_WAIT) -> codex_mesh.RpcMessage | None:
            _ = budget.check()
            counts["reads"] += 1
            return reads(ws, wake)

        def frame(client: codex_mesh.Client, deadline: float) -> codex_mesh.RpcMessage | None:
            _ = budget.check()
            counts["passes"] += 1
            return next_frame(client, deadline)

        def poll(stream: codex_mesh.TurnStream, log: TextIO) -> codex_mesh.StreamStep | codex_mesh.FailedWithThread:
            _ = budget.check()
            counts["quiet"] += 1
            return quiet(stream, log)

        self.server.respond = streamed
        sleeps = MagicMock(side_effect=AssertionError("delta processing slept"))
        with (patch.object(codex_mesh.WebSocket, "recv", read),
              patch.object(codex_mesh.Client, "next_frame", frame),
              patch.object(codex_mesh, "_on_quiet_poll", poll),
              patch.object(codex_mesh, "time", SimpleNamespace(time=time.time, monotonic=time.monotonic,
                  sleep=sleeps))):
            self.assertEqual(self.run_follow(), (0, ""))
        self.assertIn("stream complete", (self.session_dir / "summary.txt").read_text())
        self.assertEqual(self.worker_record()["status"], "done")
        self.assertLessEqual(counts["reads"], 10_100)
        self.assertLessEqual(counts["passes"], 10_100)
        self.assertLessEqual(counts["quiet"], 20, "deltas caused lifecycle work")
        sleeps.assert_not_called()

    def assert_follow_exits_after_terminal_marker(self, *, finished_run: bool, resident: bool) -> None:
        self.assertEqual(self.run_start(), (0, ""))
        self.start_args_timeout = 4
        self.outcomes = ["disconnect"]
        self.server.hold_turn_completion = True
        self.server.answer_requests_during_held_turn = True
        self.server.held_turn_completes_successfully = True
        answer_consumed = threading.Event()
        respond = self.respond
        on_frame = codex_mesh._on_frame  # pyright: ignore[reportPrivateUsage]

        @bounded_fixture
        def observed_frame(
            stream: codex_mesh.TurnStream, log: TextIO, frame: codex_mesh.RpcMessage,
        ) -> codex_mesh.StreamStep | codex_mesh.FailedWithThread:
            step = on_frame(stream, log, frame)
            item = _as_test_dict(_as_test_dict(frame.get("params")).get("item"))
            if item.get("id") == "already-finished":
                answer_consumed.set()
            return step

        @bounded_fixture
        def held_response(request: dict[str, object]) -> list[dict[str, object]] | None:
            frames = respond(request)
            if request.get("method") == "turn/start" and frames is not None:
                self.live_turn = f"turn-{self.turn_starts}"
                frames.append(_notice("item/completed", {
                    "threadId": THREAD_ID, "turnId": self.live_turn,
                    "item": {"id": "already-finished", "type": "agentMessage", "text": "already finished"},
                }))
            return frames

        self.server.respond = held_response
        args = self.start_args()
        args.resident = resident
        with (patch.object(codex_mesh, "_on_frame", observed_frame),
              patch.object(self, "start_args", new=lambda: args),
              ThreadPoolExecutor(max_workers=1) as executor):
            future = executor.submit(self.run_follow)
            try:
                self.assertTrue(self.server.held_turn_ready.wait(timeout=5))
                self.assertTrue(answer_consumed.wait(timeout=5))
                # The server is idle, but its turn/completed notice is lost.
                self.live_turn = ""
                if finished_run:
                    _ = (self.session_dir / plan_history.STATE_FILENAME).write_text(json.dumps({
                        "status": "done", "finished_at": "2026-10-28T12:00:00Z",
                    }))
                else:
                    record = self.worker_record()
                    record["status"] = "ended"
                    self.write_pool_roster({"worker": record})
                code, errors = future.result(timeout=1)
            finally:
                self.live_turn = ""
                self.server.release_held_turn.set()
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.turn_starts, 2, "finished follow opened another turn")

    def test_idle_follow_exits_when_the_worker_roster_becomes_terminal(self) -> None:
        self.assert_follow_exits_after_terminal_marker(finished_run=False, resident=False)

    def test_idle_follow_exits_when_the_run_is_finished_without_a_turn_notice(self) -> None:
        self.assert_follow_exits_after_terminal_marker(finished_run=True, resident=False)

    def test_resident_follow_exits_when_the_worker_roster_becomes_terminal(self) -> None:
        self.assert_follow_exits_after_terminal_marker(finished_run=False, resident=True)

    def test_resident_follow_exits_when_the_run_is_finished_without_a_turn_notice(self) -> None:
        self.assert_follow_exits_after_terminal_marker(finished_run=True, resident=True)


@final
class DirectorLifetimeTests(MeshCommandFixture):
    def assert_run_end_keeps_directed_workers(self, *, changed_account: bool, loaded: bool = True) -> None:
        root = self.session_dir.parent
        home = root / "director-home"
        home.mkdir()
        select_fixture_workers(home, notes=self.home)
        registry = root / "director-sessions"
        registry.mkdir()
        active = root / "active"
        active.mkdir()
        old_session = "old-director"
        current_session = "new-director" if changed_account else old_session
        cwd = root / "repository"
        cwd.mkdir()
        state = {"production": "demo", "unit": "builders", "working_dir": str(cwd), "status": "active"}
        _ = (self.session_dir / plan_history.STATE_FILENAME).write_text(json.dumps(state))
        _ = (self.session_dir / "working_dir").write_text(str(cwd))
        _ = (self.session_dir / "director").write_text(old_session)
        destination = root / "next-run"
        destination.mkdir()
        _ = (destination / plan_history.STATE_FILENAME).write_text(json.dumps(state))
        _ = (destination / "working_dir").write_text(str(cwd))
        _ = (destination / "director").write_text(current_session)
        _ = (destination / codex_mesh.SERVER_FILE).write_bytes((self.session_dir / codex_mesh.SERVER_FILE).read_bytes())
        marker = active / old_session
        _ = marker.write_text(str(self.session_dir))
        endpoint = socket.socket(socket.AF_UNIX)
        self.addCleanup(endpoint.close)
        address = root / "director.sock"
        endpoint.bind(str(address))
        _ = (registry / "director.json").write_text(json.dumps({
            "pid": os.getpid(), "procStart": sessions.process_start(os.getpid()),
            "sessionId": current_session, "name": "demo · builders · director",
            "messagingSocketPath": str(address), "updatedAt": 2, "tmux": "", "formerNames": [],
            "entrypoint": "cli"}))
        shutdown = root / "shutdown"
        history = shutdown / "old@example.invalid" / "history"
        history.mkdir(parents=True)
        _ = (history / "director.json").write_text(json.dumps({
            "login": "old@example.invalid", "label": "claude1", "machine": "natedev",
            "requested_at": "2026-10-28T20:00:00+00:00", "request_id": "director-account-move",
            "requested_by": {"kind": "terminal"}, "hold": "session", "then": "restart",
            "force": "wait for ready", "alerts": "none", "probe": False,
            "session": {"kind": "unit", "session_id": old_session, "pid": 99999999,
                "proc_start": "expired", "name": "demo · builders · director", "cwd": str(cwd),
                "config_dir": str(home / ".claude1"), "probe": False, "status": "idle", "updated_at": 0,
                "model": {"kind": "no reply yet"}, "checkout": {"kind": "not a checkout"},
                "run_dirs": [str(self.session_dir)], "codex_servers": [], "timers": [],
                "host": {"kind": "unit", "production": "demo", "unit": "builders",
                    "doc": str(root / "production.md"), "session_name": "demo-builders",
                    "session_host": "herdr", "plan": {"kind": "no run record"}}},
            "workers": [], "timers": [], "settle_message": {"kind": "not sent"},
            "where": {"kind": "not said"}, "progress": {"kind": "restarted", "at": "2026-10-28T20:01:00+00:00"},
            "trail": [], "stop_issues": []}))
        environment = {"HOME": str(home), "NOTIFIER_SESSIONS_DIR": str(registry),
            "SHUTDOWN_STATE_DIR": str(shutdown), "UNIT_WORKERS_STATE": str(root / "unit-workers"),
            "UNIT_RUN_ROOT": str(root), "UNIT_ACTIVE_DIR": str(active),
            "CLAUDE_CODE_SESSION_ID": old_session, "FIXTURE_RUN": str(self.session_dir),
            "FIXTURE_REAPS": str(root / "reaps"), "FIXTURE_PORT": str(self.server.port),
            "FIXTURE_SERVER_PID": str(os.getpid()), "SCRATCH_ROOT": str(root / "scratch"),
            "SCRATCH_VISIBLE_ROOT": str(root / "visible"), "SCRATCH_HOLDS": str(root / "holds.json")}
        self.patches.enter_context(patch.dict(os.environ, environment))
        roster = {"project-impl": {"thread_id": THREAD_ID, "status": "done", "role": "impl", "launcher_pid": 0},
                  "project-test": {"thread_id": "thread-test", "status": "paused", "role": "test", "launcher_pid": 0}}
        roster_entries: dict[str, object] = dict(roster)
        self.write_pool_roster(roster_entries)
        store = unit_workers.ledger_for_run(self.session_dir)
        with store.locked() as ledger:
            for name, record in roster.items():
                ledger.workers[name] = unit_workers.UnitWorker(name, str(record["role"]), "codex", "sol", "high", {},
                    unit_workers.CodexThread(str(record["thread_id"]), os.environ["CODEX_HOME"]),
                    unit_workers.HeldBy(str(self.session_dir)),
                    [unit_workers.WorkerEvent("opened", "fixture", str(self.session_dir), 1.0)])
        inboxes: dict[Path, bytes] = {}
        for name, record in roster.items():
            inbox = self.session_dir / f"{name}.pending.json"
            _ = inbox.write_text(json.dumps({"thread_id": record["thread_id"], "messages": [f"task for {name}"],
                                            "accepting_messages": True}))
            inboxes[inbox] = inbox.read_bytes()
        server_before = (self.session_dir / codex_mesh.SERVER_FILE).read_bytes()
        self.queued = [{"id": "still-queued", "input": [{"type": "text", "text": "do not delete"}]}]
        respond = self.server.respond

        @bounded_fixture
        def director_loaded_threads(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/loaded/list":
                return [_reply(request, {"data": [THREAD_ID] if loaded else [], "nextCursor": None})]
            return respond(request)

        self.server.respond = director_loaded_threads
        shim = home / ".claude" / "scripts" / "lib" / "py"
        shim.parent.mkdir(parents=True)
        # Only process discovery/signalling and unrelated design-role/Claude cleanup are synthetic.
        # The shell, stop, sweep, registry, shutdown history and worker ledger execute real code.
        _ = shim.write_text("#!/usr/bin/env python3\n" + "import sys\n"
            + f"sys.path.insert(0, {str(Path(codex_mesh.__file__).resolve().parents[2])!r})\n"
            + '''import os, pathlib, runpy, sys
from unittest.mock import patch
from scripts.agents import codex_mesh
script, *arguments = sys.argv[1:]
if script.endswith('design_roles.py'):
    raise SystemExit(0)
if script.endswith('remove_workers.py'):
    from scripts.direct import remove_workers
    sys.argv = [script, *arguments]
    invoke = remove_workers.subprocess.run
    def no_implicit_sweep(command, *args, **kwargs):
        if 'sweep' in command:
            pathlib.Path(os.environ['FIXTURE_REAPS']).write_text('implicit sweep')
            return remove_workers.subprocess.CompletedProcess(command, 1)
        return invoke(command, *args, **kwargs)
    with patch.object(remove_workers.subprocess, 'run', no_implicit_sweep), patch.object(remove_workers, 'DEFAULT_ROOTS', (pathlib.Path(os.environ['FIXTURE_RUN']).parent,)), patch.object(remove_workers, 'list_sessions', return_value=[]), patch.object(remove_workers, 'shutdown_held_sessions', return_value={'kind': 'held sessions', 'session_ids': set()}):
        raise SystemExit(remove_workers.main())
if script.endswith('codex_mesh.py'):
    def reap(pid):
        with pathlib.Path(os.environ['FIXTURE_REAPS']).open('a') as output:
            output.write(str(pid) + '\\n')
        return True
    run = pathlib.Path(os.environ['FIXTURE_RUN'])
    servers = [(int(os.environ['FIXTURE_SERVER_PID']), int(os.environ['FIXTURE_PORT']), 7200.0)]
    with patch.object(codex_mesh, '_reap', reap), patch.object(codex_mesh, '_running_servers', return_value=servers), patch.object(codex_mesh, '_running_daemons', return_value=[]), patch.object(codex_mesh, '_client_counts', return_value={}), patch.object(codex_mesh, '_process_executable', return_value=pathlib.Path('/fixture/codex')), patch.object(codex_mesh, '_server_folder', return_value=codex_mesh.MeshServerLog(run)):
        raise SystemExit(codex_mesh.main(arguments))
sys.argv = [script, *arguments]
runpy.run_path(script, run_name='__main__')
''')
        shim.chmod(0o755)
        ended = subprocess.run(["bash", str(Path(codex_mesh.__file__).parents[1] / "direct" / "end_session.sh")],
            capture_output=True, text=True, check=False, timeout=20, env=os.environ.copy())
        self.assertEqual(ended.returncode, 0, ended.stdout + ended.stderr)
        self.assertFalse(marker.exists())
        for name, expected in roster.items():
            actual = cast(dict[str, dict[str, object]], json.loads((self.session_dir / codex_mesh.ROSTER_FILE).read_text()))[name]
            self.assertEqual(actual["thread_id"], expected["thread_id"])
            self.assertEqual(actual["status"], expected["status"])
            self.assertFalse((self.session_dir / f"{name}.end").exists())
        for inbox, before in inboxes.items():
            self.assertEqual(inbox.read_bytes(), before)
        self.assertFalse((root / "reaps").exists(), ended.stdout + ended.stderr)
        self.assertEqual((self.session_dir / codex_mesh.SERVER_FILE).read_bytes(), server_before)
        self.assertEqual(self.methods("thread/queue/delete"), [])
        self.assertEqual(len(self.queued), 1)
        self.queued.clear()
        self.assertEqual(unit_workers.resolve(destination, "impl"), "project-impl")
        taken = cast(dict[str, dict[str, object]], json.loads((destination / codex_mesh.ROSTER_FILE).read_text()))
        self.assertEqual(taken["project-impl"]["thread_id"], THREAD_ID)
        self.assertEqual(self.methods("thread/start"), [])

    def test_run_end_keeps_workers_and_server_when_the_director_stays_on_its_account(self) -> None:
        self.assert_run_end_keeps_directed_workers(changed_account=False)

    def test_run_end_keeps_workers_and_server_after_the_director_changes_accounts(self) -> None:
        self.assert_run_end_keeps_directed_workers(changed_account=True)

    def test_run_end_does_not_reclaim_an_empty_server_before_the_next_directed_dispatch(self) -> None:
        self.assert_run_end_keeps_directed_workers(changed_account=False, loaded=False)


@final
class GuardedWorkerEndTests(MeshCommandFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        respond = self.server.respond

        @bounded_fixture
        def loaded_worker(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/loaded/list":
                return [_reply(request, {"data": [THREAD_ID], "nextCursor": None})]
            return respond(request)

        self.server.respond = loaded_worker

    def guarded_end(self, *, status: str, expected: str, live_turn: str = "") -> tuple[int, str, str]:
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": status,
                                           "role": "impl", "launcher_pid": 0}})
        self.live_turn = live_turn
        self.queued = [{"id": "server-task", "input": [{"type": "text", "text": "queued task"}]}]
        inbox = self.session_dir / "worker.pending.json"
        _ = inbox.write_text(json.dumps({"thread_id": THREAD_ID, "messages": ["local task"],
                                        "accepting_messages": True}))
        roster = self.session_dir / codex_mesh.ROSTER_FILE
        before = roster.read_bytes(), inbox.read_bytes()
        code, output, errors = self.run_mesh(["end", "--to", "worker",
            "--expected-thread-id", expected, "--idle-only"])
        if code:
            self.assertEqual((roster.read_bytes(), inbox.read_bytes()), before)
            self.assertFalse((self.session_dir / "worker.end").exists())
            self.assertEqual(len(self.queued), 1)
            self.assertEqual(self.methods("thread/queue/delete"), [])
            self.assertEqual(self.methods("turn/interrupt"), [])
        return code, output, errors

    def test_guarded_end_refuses_a_replacement_thread_without_mutating_its_work(self) -> None:
        code, output, errors = self.guarded_end(status="done", expected="obsolete-thread")
        self.assertNotEqual(code, 0, output + errors)

    def test_guarded_end_refuses_a_running_roster_without_mutating_its_work(self) -> None:
        code, output, errors = self.guarded_end(status="running", expected=THREAD_ID, live_turn="active-turn")
        self.assertNotEqual(code, 0, output + errors)

    def test_guarded_end_refuses_an_idle_roster_with_a_live_server_turn(self) -> None:
        code, output, errors = self.guarded_end(status="done", expected=THREAD_ID, live_turn="active-turn")
        self.assertNotEqual(code, 0, output + errors)

    def test_guarded_end_cancels_the_matching_idle_workers_work(self) -> None:
        code, output, errors = self.guarded_end(status="done", expected=THREAD_ID)
        self.assertEqual(code, 0, output + errors)
        self.assertEqual(self.worker_record()["status"], "ended")
        self.assertTrue((self.session_dir / "worker.end").is_file())
        self.assertEqual((self.session_dir / "worker.pending.json").read_text(), "")
        self.assertEqual(self.queued, [])
        self.assertEqual(self.methods("turn/interrupt"), [])

    def test_retrying_end_finishes_queue_cleanup_after_its_budget_expires(self) -> None:
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "done", "role": "impl"}})
        self.live_turn = "turn-before-end"
        self.queued = [{"id": "remaining-task", "input": [{"type": "text", "text": "must not open another turn"}]}]
        original_call = codex_mesh.Client.call
        clock_offset = 0.0
        exhausted = False

        def wall_clock() -> float:
            return time.time() + clock_offset

        @bounded_fixture
        def exhaust_after_queue_read(client: codex_mesh.Client, method: str, params: dict[str, object],
                                     timeout: float = codex_mesh.CALL_TIMEOUT_SECS) -> codex_mesh.RpcMessage:
            nonlocal clock_offset, exhausted
            reply = original_call(client, method, params, timeout)
            if method == "thread/queue/list" and not exhausted:
                exhausted = True
                # Model a slow successful read consuming the entire operation
                # budget, before any queued submission can be deleted.
                clock_offset += codex_mesh.END_OPERATION_TIMEOUT_SECS + 1
            return reply

        with (patch.object(codex_mesh, "time", SimpleNamespace(time=wall_clock, monotonic=time.monotonic)),
              patch.object(codex_mesh.Client, "call", exhaust_after_queue_read)):
            code, output, errors = self.run_mesh(["end", "--to", "worker"])
            self.assertNotEqual(code, 0, output + errors)
            self.assertTrue(exhausted, "cleanup never reached the server queue")
            self.assertEqual(len(self.queued), 1)
            self.assertEqual(self.methods("thread/queue/delete"), [])
            self.assertEqual(self.methods("turn/interrupt"), [])
            self.assertEqual(self.worker_record()["status"], "ended")
            clock_offset = 0
            code, output, errors = self.run_mesh(["end", "--to", "worker"])
        self.assertEqual(code, 0, output + errors)
        self.assertEqual(self.queued, [])
        self.assertEqual(self.live_turn, "")
        self.assertEqual(len(self.methods("thread/queue/delete")), 1)
        self.assertEqual(len(self.methods("turn/interrupt")), 1)
        self.assertEqual(self.methods("turn/start"), [])

    def assert_busy_worker_is_not_ended(self, *, detached: bool) -> None:
        record: dict[str, object] = {"thread_id": THREAD_ID, "status": "done", "role": "impl", "launcher_pid": 0}
        if detached:
            runs = self.session_dir / "mesh_runs"
            runs.mkdir()
            path = runs / "live.json"
            child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()",
                "mesh_runs.py", "supervise", str(path)], stdin=subprocess.PIPE)
            self.addCleanup(child.communicate, timeout=4)
            _ = path.write_text(json.dumps({"worker": "worker", "status": "running", "pid": child.pid,
                                           "cwd": str(self.session_dir)}))
        else:
            record.update(allocation_pending=True, allocation_id="next-dispatch",
                allocation_owner_pid=os.getpid(), allocation_expires_at=time.time() + 120)
        self.write_pool_roster({"worker": record})
        inbox = self.session_dir / "worker.pending.json"
        _ = inbox.write_text(json.dumps({"thread_id": THREAD_ID, "messages": []}))
        reason = codex_mesh.worker_idle_reason(str(self.session_dir), "worker", record)
        self.assertIn("detached" if detached else "reserved", reason)
        before = (self.session_dir / codex_mesh.ROSTER_FILE).read_bytes(), inbox.read_bytes()
        code, output, errors = self.run_mesh(["end", "--to", "worker", "--expected-thread-id", THREAD_ID, "--idle-only"])
        self.assertNotEqual(code, 0, output + errors)
        self.assertEqual(((self.session_dir / codex_mesh.ROSTER_FILE).read_bytes(), inbox.read_bytes()), before)
        self.assertFalse((self.session_dir / "worker.end").exists())
        self.assertEqual(self.methods("turn/interrupt"), [])

    def test_idle_end_refuses_a_worker_reserved_for_the_next_dispatch(self) -> None:
        self.assert_busy_worker_is_not_ended(detached=False)

    def test_idle_end_refuses_a_worker_with_a_live_detached_command(self) -> None:
        self.assert_busy_worker_is_not_ended(detached=True)

    @unittest.skipUnless(Path("/proc/self/wchan").exists(), "requires Linux kernel lock-wait observation")
    def test_end_and_a_launcher_holding_the_inbox_finish_without_deadlock(self) -> None:
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "done", "role": "impl"}})
        # Announce the child's first blocking flock attempt before it takes it.
        # The launcher already holds the inbox and then performs its real roster
        # update, exactly the nesting used by capacity and new-turn dispatch.
        child_code = """import fcntl, os, sys
from scripts.agents import codex_mesh
flock = fcntl.flock
announced = False
def observed(handle, operation):
    global announced
    if operation == fcntl.LOCK_EX and not announced:
        announced = True
        print('locking', flush=True)
    return flock(handle, operation)
fcntl.flock = observed
raise SystemExit(codex_mesh.main(['end', '--session-dir', sys.argv[1], '--to', 'worker']))
"""
        with codex_mesh._pending_file(str(self.session_dir), "worker"):  # pyright: ignore[reportPrivateUsage]
            child = subprocess.Popen([sys.executable, "-c", child_code, str(self.session_dir)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            assert child.stdout is not None
            try:
                self.assertTrue(select.select([child.stdout], [], [], 5)[0], "end never attempted its lock")
                self.assertEqual(cast(TextIO, child.stdout).readline().strip(), "locking")
                # The announcement precedes flock. Observe the actual kernel
                # wait before allowing the launcher to compete for the roster,
                # or an inverted order could pass by winning that race.
                lock_wait = Path(f"/proc/{child.pid}/wchan")
                observation = FixtureLoopBudget("end blocked in flock", seconds=5, max_calls=1_000)
                while "lock" not in lock_wait.read_text():
                    remaining = observation.check()
                    self.assertIsNone(child.poll(), "end exited before blocking on the held inbox")
                    _ = select.select([], [], [], min(0.01, remaining))
                with ThreadPoolExecutor(max_workers=1) as executor:
                    update = executor.submit(codex_mesh._set_roster_assignment, str(self.session_dir),  # pyright: ignore[reportPrivateUsage]
                        "worker", "impl", "launcher lock probe")
                    try:
                        _ = update.result(timeout=2)
                    finally:
                        # Only our child may be stopped; this also frees its roster lock when the lock order is inverted.
                        if not update.done():
                            child.kill()
                            _ = child.wait(timeout=3)
            except BaseException:
                if child.poll() is None:
                    child.kill()
                _ = child.communicate(timeout=3)
                raise
        output, errors = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, output + errors)
        self.assertEqual(self.worker_record()["task"], "launcher lock probe")
        self.assertEqual(self.worker_record()["status"], "ended")


@final
class DirectorServerReclamationTests(MeshCommandFixture):
    def assert_reclamation(self, *, retired: bool, queue: str) -> None:
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "done", "role": "impl"}})
        record = self.session_dir / codex_mesh.SERVER_FILE
        if retired:
            _ = record.rename(self.session_dir / "newer-server.json")
            _ = (self.session_dir / codex_mesh.RETIRED_FILE).write_text(json.dumps({
                "servers": [{"pid": os.getpid(), "port": self.server.port}]}))
        old = time.time() - codex_mesh.SERVER_IDLE_SECS - 60
        for name in codex_mesh.SWEEP_ACTIVITY_FILES:
            activity = self.session_dir / name
            if activity.exists():
                os.utime(activity, (old, old))
        respond = self.respond

        @bounded_fixture
        def loaded(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/loaded/list":
                return [_reply(request, {"data": [THREAD_ID], "nextCursor": None})]
            if request.get("method") == "thread/queue/list" and queue == "unreadable":
                return [{"id": request["id"], "error": {"message": "queue unreadable"}}]
            return respond(request)

        queue_reads = 0
        original_loaded = loaded

        @bounded_fixture
        def queue_arrives_before_second_judgment(request: dict[str, object]) -> list[dict[str, object]] | None:
            nonlocal queue_reads
            if request.get("method") == "thread/queue/list":
                queue_reads += 1
                if queue == "arriving" and queue_reads == 2:
                    self.queued = [{"id": "late", "input": [{"type": "text", "text": "arrived after first judgment"}]}]
            return original_loaded(request)

        self.server.respond = queue_arrives_before_second_judgment
        if queue == "queued":
            self.queued = [{"id": "kept", "input": [{"type": "text", "text": "preserve me"}]}]
        # Process discovery/signalling are isolated; the queue, both judgments,
        # ownership discovery, and server mutation lock run their real paths.
        with (patch.object(unit_workers, "run_has_live_director", return_value=True),
              patch.object(codex_mesh, "_running_servers", return_value=[(os.getpid(), self.server.port, 7200.0)]),
              patch.object(codex_mesh, "_client_counts", return_value={}),
              patch.object(codex_mesh, "_process_executable", return_value=Path("/fixture/codex")),
              patch.object(codex_mesh, "_server_folder", return_value=codex_mesh.MeshServerLog(self.session_dir)),
              patch.object(codex_mesh, "_reap", return_value=True) as reaped,
              contextlib.redirect_stdout(io.StringIO())):
            code = codex_mesh.command_idle_stop(argparse.Namespace(session_dir=str(self.session_dir),
                pid=os.getpid(), port=self.server.port))
        if queue == "empty":
            self.assertEqual(code, 0)
            reaped.assert_called_once_with(os.getpid())
        else:
            self.assertEqual(code, 1)
            reaped.assert_not_called()
            self.assertGreater(queue_reads, 0, "reclamation never examined queued work")
            if queue == "queued":
                self.assertEqual(len(self.queued), 1)
            if queue == "arriving":
                self.assertGreaterEqual(queue_reads, 2, "work never reached the second reclamation check")
                self.assertEqual(len(self.queued), 1)
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)
        self.assertEqual(self.methods("thread/start"), [])

    def test_idle_stop_reclaims_an_empty_server_even_while_its_director_lives(self) -> None:
        self.assert_reclamation(retired=False, queue="empty")

    def test_retired_server_reclamation_is_not_vetoed_by_a_live_director(self) -> None:
        self.assert_reclamation(retired=True, queue="empty")

    def test_reclamation_keeps_a_loaded_threads_queued_submission(self) -> None:
        for retired in (False, True):
            with self.subTest(retired=retired):
                self.assert_reclamation(retired=retired, queue="queued")

    def test_reclamation_keeps_a_server_when_its_loaded_queue_is_unreadable(self) -> None:
        for retired in (False, True):
            with self.subTest(retired=retired):
                self.assert_reclamation(retired=retired, queue="unreadable")

    def test_the_second_reclamation_check_keeps_work_queued_after_the_first_check(self) -> None:
        for retired in (False, True):
            with self.subTest(retired=retired):
                self.queued.clear()
                self.assert_reclamation(retired=retired, queue="arriving")


@final
class WorkerPauseTests(MeshCommandFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        respond = self.server.respond
        @bounded_fixture
        def loaded_worker(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/loaded/list":
                return [_reply(request, {"data": [THREAD_ID], "nextCursor": None})]
            return respond(request)
        self.server.respond = loaded_worker

    def test_pause_does_not_query_a_server_that_never_loaded_the_worker(self) -> None:
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "done", "role": "impl"}})
        requests: list[str] = []
        @bounded_fixture
        def unloaded(request: dict[str, object]) -> list[dict[str, object]] | None:
            method = str(request.get("method"))
            requests.append(method)
            if method == "thread/loaded/list":
                return [_reply(request, {"data": [], "nextCursor": None})]
            if method in ("thread/queue/list", "thread/read"):
                return [{"id": request["id"], "error": {"message": "thread not loaded"}}]
            return self.respond(request)
        retired = StubAppServer(unloaded)
        self.addCleanup(retired.close)
        _ = (self.session_dir / codex_mesh.RETIRED_FILE).write_text(json.dumps({
            "servers": [{"pid": os.getpid(), "port": retired.port}]}))
        with patch.object(codex_mesh, "_reap", return_value=True):
            code, _output, errors = self.run_mesh(["stop", "--pause"])
        self.assertEqual(code, 0, errors)
        self.assertIn("thread/loaded/list", requests)
        self.assertNotIn("thread/queue/list", requests)
        self.assertNotIn("thread/read", requests)

    def test_partial_pause_failure_still_protects_every_detached_commands_checkout(self) -> None:
        from scripts.direct import workers, remove_run_scratch
        self.patches.enter_context(patch.dict(os.environ, {"UNIT_WORKERS_STATE": str(self.session_dir / "state")}))
        _ = (self.session_dir / workers.STATE).write_text(json.dumps({"production": "demo", "unit": "builders"}))
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "done", "role": "impl"}})
        store = workers.ledger_for_run(self.session_dir)
        with store.locked() as ledger:
            ledger.workers["worker"] = workers.UnitWorker("worker", "impl", "codex", "sol", "high", {},
                workers.CodexThread(THREAD_ID, os.environ["CODEX_HOME"]), workers.HeldBy(str(self.session_dir)),
                [workers.WorkerEvent("opened", "fixture", str(self.session_dir), 1.0)])
        records = self.session_dir / "mesh_runs"
        records.mkdir()
        protected: list[Path] = []
        for ordinal in range(2):
            checkout = self.session_dir / "phases" / str(ordinal) / "gate"
            (checkout / ".git").mkdir(parents=True)
            protected.append(checkout)
            record = records / f"command-{ordinal}.json"
            child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()",
                "mesh_runs.py", "supervise", str(record)], stdin=subprocess.PIPE)
            self.addCleanup(child.communicate, timeout=4)
            _ = record.write_text(json.dumps({"worker": "worker", "status": "running", "pid": child.pid,
                                             "cwd": str(checkout)}))
        @bounded_fixture
        def unreadable_queue(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/loaded/list":
                return [_reply(request, {"data": [THREAD_ID], "nextCursor": None})]
            if request.get("method") == "thread/queue/list":
                return [{"id": request["id"], "error": {"message": "queue unavailable"}}]
            return self.respond(request)
        retired = StubAppServer(unreadable_queue)
        self.addCleanup(retired.close)
        _ = (self.session_dir / codex_mesh.RETIRED_FILE).write_text(json.dumps({
            "servers": [{"pid": os.getpid(), "port": retired.port}]}))
        with patch.object(codex_mesh, "_reap", return_value=True) as reaped:
            with self.assertRaisesRegex(SystemExit, "servers kept alive"):
                _ = self.run_mesh(["stop", "--pause"])
        reaped.assert_not_called()
        with (contextlib.redirect_stdout(io.StringIO()),
              patch.object(sys, "argv", ["remove_run_scratch.py", str(self.session_dir)])):
            self.assertEqual(remove_run_scratch.main(), 0)
        for checkout in protected:
            self.assertTrue(checkout.is_dir(), "failed pause deleted an owned command cwd")

    def test_failed_ledger_pause_still_protects_a_held_workers_command_checkout(self) -> None:
        from scripts.direct import workers, remove_run_scratch
        self.patches.enter_context(patch.dict(os.environ, {"UNIT_WORKERS_STATE": str(self.session_dir / "state")}))
        _ = (self.session_dir / workers.STATE).write_text(json.dumps({"production": "demo", "unit": "builders"}))
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "done", "role": "impl"}})
        store = workers.ledger_for_run(self.session_dir)
        with store.locked() as ledger:
            ledger.workers["worker"] = workers.UnitWorker("worker", "impl", "codex", "sol", "high", {},
                workers.CodexThread(THREAD_ID, os.environ["CODEX_HOME"]), workers.HeldBy(str(self.session_dir)),
                [workers.WorkerEvent("opened", "fixture", str(self.session_dir), 1.0)])
        checkout = self.session_dir / "phases" / "5" / "gate"
        (checkout / ".git").mkdir(parents=True)
        old = self.session_dir / "phases" / "4" / "gate"
        (old / ".git").mkdir(parents=True)
        records = self.session_dir / "mesh_runs"
        records.mkdir()
        record = records / "held-command.json"
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()",
            "mesh_runs.py", "supervise", str(record)], stdin=subprocess.PIPE)
        self.addCleanup(child.communicate, timeout=4)
        _ = record.write_text(json.dumps({"worker": "worker", "status": "running", "pid": child.pid,
                                         "cwd": str(checkout)}))
        with patch.object(workers, "pause_run", side_effect=workers.WorkerUnavailable("ledger quarantine")):
            with self.assertRaisesRegex(workers.WorkerUnavailable, "ledger quarantine"):
                _ = self.run_mesh(["stop", "--pause"])
        with store.locked() as ledger:
            self.assertIsInstance(ledger.workers["worker"].ownership, workers.HeldBy)
        with (contextlib.redirect_stdout(io.StringIO()),
              patch.object(sys, "argv", ["remove_run_scratch.py", str(self.session_dir)])):
            self.assertEqual(remove_run_scratch.main(), 0)
        self.assertTrue(checkout.is_dir())
        self.assertFalse(old.exists())
        self.assertIsNone(child.poll())

    def test_pause_parks_server_messages_before_deleting_them_and_preserves_local_work(self) -> None:
        reservation = {"allocation_pending": True, "allocation_id": "reserved-dispatch",
                       "allocation_owner_pid": os.getpid(), "allocation_expires_at": time.time() + 120}
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "done", "role": "impl",
            "port": self.server.port, "continuation_owed": True, **reservation}})
        inbox = self.session_dir / "worker.pending.json"
        _ = inbox.write_text(json.dumps({"thread_id": THREAD_ID, "messages": ["local queued task"],
                                        "accepting_messages": True}))
        records = self.session_dir / "mesh_runs"
        records.mkdir()
        detached = records / "retained.json"
        _ = detached.write_text(json.dumps({"status": "running", "worker": "worker", "pid": os.getpid()}))
        before = detached.read_bytes()
        self.queued = [{"id": "park-me", "input": [{"type": "text", "text": "server queued task"}]}]
        respond = self.server.respond

        @bounded_fixture
        def require_durable_queue(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/queue/delete":
                self.assertIn("server queued task", inbox.read_text())
                self.assertIn("local queued task", inbox.read_text())
            return respond(request)

        self.server.respond = require_durable_queue
        with patch.object(codex_mesh, "_reap", return_value=True), contextlib.redirect_stdout(io.StringIO()):
            code, _output, errors = self.run_mesh(["stop", "--pause"])
        self.assertEqual(code, 0, errors)
        after = self.worker_record()
        self.assertEqual(after["status"], "paused")
        self.assertTrue(after["continuation_owed"])
        self.assertEqual({key: after[key] for key in reservation}, reservation)
        pending = _as_test_dict(cast(object, json.loads(inbox.read_text())))
        self.assertEqual(pending["messages"], ["local queued task", "server queued task"])
        self.assertEqual(self.queued, [])
        self.assertEqual(detached.read_bytes(), before)

    def test_pause_preserves_queues_on_current_and_retired_servers_before_reaping_either(self) -> None:
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "done", "role": "impl",
                                           "port": self.server.port}})
        self.queued = [{"id": "current-task", "input": [{"type": "text", "text": "current server task"}]}]
        retired_queue: list[dict[str, object]] = [{"id": "retired-task", "input": [
            {"type": "text", "text": "retired server task"}]}]
        inbox = self.session_dir / "worker.pending.json"

        @bounded_fixture
        def retired_response(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/loaded/list":
                return [_reply(request, {"data": [THREAD_ID], "nextCursor": None})]
            if request.get("method") == "thread/queue/list":
                return [_reply(request, {"data": retired_queue.copy(), "nextCursor": None})]
            if request.get("method") == "thread/queue/delete":
                self.assertIn("retired server task", inbox.read_text())
                retired_queue.clear()
                return [_reply(request, {})]
            return self.respond(request)

        retired = StubAppServer(retired_response)
        self.addCleanup(retired.close)
        _ = (self.session_dir / codex_mesh.RETIRED_FILE).write_text(json.dumps({
            "servers": [{"pid": os.getpid(), "port": retired.port}]}))

        def reap_after_preservation(pid: int) -> bool:
            self.assertEqual(pid, os.getpid())
            self.assertEqual(self.queued, [])
            self.assertEqual(retired_queue, [])
            messages = _as_test_dict(cast(object, json.loads(inbox.read_text())))["messages"]
            self.assertCountEqual(cast(list[object], messages), ["current server task", "retired server task"])
            return True

        with patch.object(codex_mesh, "_reap", side_effect=reap_after_preservation) as reaped:
            code, _output, errors = self.run_mesh(["stop", "--pause"])
        self.assertEqual(code, 0, errors)
        self.assertEqual(reaped.call_count, 2)
        self.assertFalse((self.session_dir / codex_mesh.RETIRED_FILE).exists())

    def test_pause_reaps_only_its_recorded_servers_and_keeps_another_runs_server(self) -> None:
        self.write_pool_roster({"worker": {"thread_id": THREAD_ID, "status": "done", "role": "impl"}})
        other = self.session_dir.parent / "other-run"
        other.mkdir()
        other_server = other / codex_mesh.SERVER_FILE
        _ = other_server.write_text(json.dumps({"pid": 99999998, "port": 49152}))
        before = other_server.read_bytes()
        with patch.object(codex_mesh, "_reap", return_value=True) as reap:
            code, _output, errors = self.run_mesh(["stop", "--pause"])
        self.assertEqual(code, 0, errors)
        self.assertEqual([call.args[0] for call in reap.call_args_list], [os.getpid()])
        self.assertEqual(other_server.read_bytes(), before)

    def test_board_slot_comes_from_stored_role_for_an_arbitrary_address(self) -> None:
        self.configure_context()
        self.write_pool_roster({"w7": {"thread_id": THREAD_ID, "status": "done", "role": "fix"}})
        codex_mesh._post_board_status(str(self.session_dir), "w7", "Waiting for assignment")  # pyright: ignore[reportPrivateUsage]
        line = (self.session_dir / "board.log").read_text()
        self.assertEqual(len(line.splitlines()), 1)
        self.assertIn("[fix] status: natedev · models · repair worker: Waiting for assignment", line)


@final
class WorkerAdoptionTests(MeshCommandFixture):
    """Move real fake-server threads across isolated run rosters."""

    @override
    def setUp(self) -> None:
        super().setUp()
        self.patches.enter_context(patch.dict(os.environ, {
            "UNIT_WORKERS_STATE": str(self.session_dir.parent / "unit-workers"),
        }))
        self.write_unit_context(self.session_dir)

    def write_unit_context(self, run: Path) -> None:
        _ = (run / plan_history.STATE_FILENAME).write_text(json.dumps({
            "status": "active", "production": "fixture-production", "unit": "models",
            "working_dir": str(self.session_dir.parent),
        }))

    def destination(self, name: str) -> Path:
        destination = self.session_dir.parent / name
        destination.mkdir()
        self.write_unit_context(destination)
        _ = (destination / codex_mesh.SERVER_FILE).write_bytes(
            (self.session_dir / codex_mesh.SERVER_FILE).read_bytes())
        return destination

    def adoption_roster(self, run: Path) -> dict[str, object]:
        """A taking roster opened before refusal may have no entries yet."""
        path = run / codex_mesh.ROSTER_FILE
        if not path.exists():
            return {}
        content = path.read_text()
        return _as_test_dict(cast(object, json.loads(content))) if content.strip() else {}

    def idle_worker(self) -> None:
        self.write_pool_roster({"worker": {
            "thread_id": THREAD_ID, "status": "done", "role": "impl",
            "model": "gpt-fixture-sol", "cwd": str(self.session_dir),
            "codex_home": str(agent_accounts.workers_home()), "port": self.server.port,
        }})

    def configure_allocation(self) -> None:
        registry = self.session_dir / "workers-agents.conf"
        _ = registry.write_text("[codex.agents]\ngpt-fixture-sol=high,xhigh,max\n" +
                                "[assignments]\ndirect=codex\n[direct.codex]\n" +
                                "impl=gpt-fixture-sol:xhigh\n[direct.options]\n" +
                                "codex_service_tier.impl=fast\n")
        checkpoint = self.session_dir / "catalog-current"
        checkpoint.touch()
        self.patches.enter_context(patch.dict(os.environ, {
            "AGENTS_CONFIG_FILE": str(registry),
            "CODEX_CATALOG_SYNC_STATE_FILE": str(checkpoint),
        }))

    def pause_registered_worker(self) -> None:
        store = unit_workers.ledger_for_run(self.session_dir)
        with store.locked() as ledger:
            ledger.workers["worker"] = unit_workers.UnitWorker(
                "worker", "impl", "codex", "gpt-fixture-sol", "high", {"kind": "WorkerLaunchPolicy"},
                unit_workers.CodexThread(THREAD_ID, str(agent_accounts.workers_home())),
                unit_workers.HeldBy(str(self.session_dir)),
                [unit_workers.WorkerEvent("opened", "fixture", str(self.session_dir), 1.0)])
        unit_workers.pause_unit("fixture-production", "models")

    def test_adopting_a_paused_worker_carries_queued_work_before_the_new_assignment_once(self) -> None:
        self.idle_worker()
        self.pause_registered_worker()
        record = self.worker_record()
        record.update({"status": "paused", "continuation_owed": True})
        self.write_pool_roster({"worker": record})
        source_pending = self.session_dir / "worker.pending.json"
        _ = source_pending.write_text(json.dumps({"thread_id": THREAD_ID,
            "messages": ["preserved local task", "Run finished: preserved verification passed"],
            "accepting_messages": True}))
        destination = self.destination("taking-paused-work")
        self.assertEqual(unit_workers.adopt(self.session_dir, destination, "worker"), "worker")
        target_pending = destination / "worker.pending.json"
        carried = _as_test_dict(cast(object, json.loads(target_pending.read_text())))
        self.assertEqual(carried["messages"], ["preserved local task", "Run finished: preserved verification passed"])
        self.assertEqual(_as_test_dict(cast(object, json.loads(source_pending.read_text())))["messages"], [])
        prompt = destination / "next.txt"
        _ = prompt.write_text("new assignment")
        room = refusals.ReadingWindow(100, refusals.NoResetKnown(), datetime.now(timezone.utc))
        with (patch.object(refusals, "stored_reading", return_value=refusals.FreshReading(room, refusals.NoWindow(), "")),
              contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors):
            code = codex_mesh.main(["follow", "--session-dir", str(destination), "--to", "worker", "--role", "impl",
                "--message-file", str(prompt), "--summary-file", str(destination / "summary"),
                "--log-file", str(destination / "turn.log"), "--timeout", "8"])
        self.assertEqual(code, 0, errors.getvalue())
        starts = self.methods("turn/start")
        self.assertEqual(len(starts), 1, starts)
        inputs = cast(list[dict[str, object]], _as_test_dict(starts[0]["params"])["input"])
        ordered = ["Run finished: preserved verification passed", "preserved local task",
                   "Continue where you stopped.", "new assignment"]
        self.assertEqual([item.get("text") for item in inputs], ordered)
        self.assertEqual(self.methods("thread/start"), [])
        taken = _as_test_dict(self.adoption_roster(destination)["worker"])
        self.assertFalse(taken.get("continuation_owed"))
        self.assertEqual(taken["thread_id"], THREAD_ID)

    def test_a_paused_worker_keeps_its_live_detached_record_in_the_giving_run(self) -> None:
        self.idle_worker()
        self.pause_registered_worker()
        runs = self.session_dir / codex_mesh.DETACHED_RUNS_DIR
        runs.mkdir()
        detached = runs / "carried-command.json"
        _ = detached.write_text(json.dumps({"worker": "worker", "status": "running", "pid": os.getpid()}))
        before = detached.read_bytes()
        destination = self.destination("taking-running-command")
        self.assertEqual(unit_workers.adopt(self.session_dir, destination, "worker"), "worker")
        watch = codex_mesh.DetachedRunWatch(str(destination), "worker")
        self.assertTrue(watch.current().live)
        self.assertEqual(detached.read_bytes(), before)
        self.assertFalse((destination / codex_mesh.DETACHED_RUNS_DIR).exists())
        self.assertIn(str(self.session_dir), codex_mesh.worker_run_history(str(destination), "worker"))
        _ = detached.write_text(json.dumps({"worker": "worker", "status": "finished", "pid": os.getpid()}))
        self.assertFalse(watch.current().live)
        self.assertTrue(detached.exists())

    def test_opening_a_unit_worker_passes_the_configured_service_tier_to_the_server(self) -> None:
        self.configure_allocation()
        record = codex_mesh.open_unit_worker(str(self.session_dir), "impl", "impl")
        self.assertEqual(record["thread_id"], THREAD_ID)
        starts = self.methods("thread/start")
        self.assertEqual(len(starts), 1)
        self.assertEqual(_as_test_dict(starts[0]["params"]).get("serviceTier"), "fast")
        self.assertEqual(self.methods("turn/start"), [], "allocating an idle worker submitted work")

    def test_new_review_thread_has_its_configured_shown_name_before_any_turn(self) -> None:
        self.configure_allocation()
        registry = self.session_dir / "workers-agents.conf"
        _ = registry.write_text(registry.read_text().replace("impl=gpt-fixture-sol:xhigh\n",
                                                          "impl=gpt-fixture-sol:xhigh\nreview=gpt-fixture-sol:xhigh\n"))
        state = self.session_dir / plan_history.STATE_FILENAME
        _ = state.write_text(json.dumps({"working_dir": str(self.session_dir),
                                         "production": "sdlc", "unit": "roles-unit"}))
        configured = fixture_names()
        jobs = dict(configured.jobs)
        jobs["code_reviewer"] = ("code checker",)
        with patch.object(names, "runtime_names", return_value=names.Names(jobs, configured.audiences)):
            record = codex_mesh.open_unit_worker(str(self.session_dir), "repository-review2", "review")
        self.assertEqual(self.methods("thread/name/set")[-1]["params"], {
            "threadId": THREAD_ID, "name": "sdlc · roles · code checker"})
        self.assertEqual(record["role"], "review")
        self.assertEqual(record["access_policy"], {"kind": "ReadOnlyReviewer"})
        self.assertEqual(self.methods("turn/start"), [])

    def test_a_new_thread_is_released_if_naming_or_recording_its_worker_fails(self) -> None:
        self.configure_allocation()
        for failure in ("naming", "recording"):
            with self.subTest(failure=failure):
                self.server.requests.clear()
                self.failing_method = "thread/name/set" if failure == "naming" else ""
                try:
                    with contextlib.ExitStack() as patches:
                        if failure == "recording":
                            _ = patches.enter_context(patch.object(
                                codex_mesh, "_update_roster_if_expected", side_effect=OSError("fixture roster write failed")))
                        with self.assertRaises((SystemExit, unit_workers.WorkerUnavailable, OSError)):
                            _ = codex_mesh.open_unit_worker(str(self.session_dir), "impl", "impl")
                finally:
                    self.failing_method = ""
                releases = self.methods("thread/unsubscribe")
                self.assertEqual([_as_test_dict(request["params"]).get("threadId") for request in releases],
                                 [THREAD_ID], "an unrecorded thread retained its writer")
                self.assertEqual(self.methods("turn/start"), [])
                self.assertNotIn("impl", self.adoption_roster(self.session_dir))

    def adopt(self, destination: Path) -> int:
        result = subprocess.run([
            sys.executable, str(Path(codex_mesh.__file__)), "adopt", "--session-dir", str(destination),
            "--from", str(self.session_dir), "--name", "worker",
        ], capture_output=True, text=True, timeout=8, check=False)
        return result.returncode

    def test_idle_adoption_keeps_the_thread_and_refuses_follow_from_the_old_run(self) -> None:
        self.idle_worker()
        destination = self.destination("next-run")
        self.assertEqual(self.adopt(destination), 0)
        adopted = _as_test_dict(_as_test_dict(cast(object, json.loads(
            (destination / codex_mesh.ROSTER_FILE).read_text()))).get("worker"))
        self.assertEqual(adopted["thread_id"], THREAD_ID)
        self.assertEqual(adopted["role"], "impl")
        self.assertEqual(adopted["model"], "gpt-fixture-sol")
        self.assertEqual(self.worker_record()["adopted_to"], str(destination))
        before = (destination / codex_mesh.ROSTER_FILE).read_bytes()
        try:
            code, _output, errors = self.run_mesh(self.follow_cli())
        except SystemExit as exc:
            code, errors = 2, str(exc)
        self.assertNotEqual(code, 0, errors)
        self.assertEqual((destination / codex_mesh.ROSTER_FILE).read_bytes(), before)
        self.assertEqual(self.methods("turn/start"), [])
        self.assertEqual(self.methods("thread/start"), [])

    def test_held_worker_adoption_refuses_live_activity_without_losing_work(self) -> None:
        respond = self.respond

        @bounded_fixture
        def loaded_live_thread(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/loaded/list" and self.live_turn:
                return [_reply(request, {"data": [THREAD_ID], "nextCursor": None})]
            return respond(request)

        self.server.respond = loaded_live_thread
        for obstacle in ("live-turn", "launcher", "detached"):
            with self.subTest(obstacle=obstacle):
                self.idle_worker()
                destination = self.destination(f"blocked-{obstacle}")
                pending = self.session_dir / "worker.pending.json"
                pending.unlink(missing_ok=True)
                detached = self.session_dir / codex_mesh.DETACHED_RUNS_DIR / "active.json"
                detached.unlink(missing_ok=True)
                self.live_turn = "live-one" if obstacle == "live-turn" else ""
                record = self.worker_record()
                if obstacle == "launcher":
                    record["launcher_pid"] = os.getpid()
                elif obstacle == "detached":
                    detached.parent.mkdir(exist_ok=True)
                    _ = detached.write_text(json.dumps({"worker": "worker", "status": "running",
                                                        "pid": os.getpid()}))
                self.write_pool_roster({"worker": record})
                source_before = (self.session_dir / codex_mesh.ROSTER_FILE).read_bytes()
                pending_before = pending.read_bytes() if pending.exists() else b""
                try:
                    code = self.adopt(destination)
                except SystemExit:
                    code = 2
                self.assertNotEqual(code, 0, f"adopted despite {obstacle}")
                self.assertEqual((self.session_dir / codex_mesh.ROSTER_FILE).read_bytes(), source_before)
                if pending_before:
                    self.assertEqual(pending.read_bytes(), pending_before)
                self.assertNotIn("worker", self.adoption_roster(destination))

    def test_adopted_thread_resumes_through_a_symlink_and_records_its_current_account_home(self) -> None:
        self.idle_worker()
        destination = self.destination("moved-account-run")
        address = self.session_dir.parent / "run-address"
        address.symlink_to(destination, target_is_directory=True)
        move = {"thread_id": THREAD_ID, "turn_id": "old-account-turn",
                "label": "fixture-account", "move_id": "move-after-adoption"}
        _ = (self.session_dir / "worker.account-move.json").write_text(json.dumps(move))
        self.assertEqual(self.adopt(destination), 0)
        moved_home = self.session_dir.parent / "moved-codex-home"
        moved_home.mkdir()
        _ = (moved_home / "auth.json").write_bytes(
            (agent_accounts.workers_home() / "auth.json").read_bytes())
        with contextlib.closing(StubAppServer(self.respond)) as moved_server:
            _ = (destination / codex_mesh.SERVER_FILE).write_text(json.dumps({
                "port": moved_server.port, "pid": os.getpid(), "codex_home": str(moved_home),
                "auto_compact_tokens": codex_mesh.AUTO_COMPACT_TOKENS,
            }))
            marker = destination / "worker.account-move.json"
            self.assertEqual(_as_test_dict(cast(object, json.loads(marker.read_text()))), move)
            selected: list[Path] = []

            def ensure_current(run: str, home: Path | None = None, *, cwd: str | None = None) -> tuple[int, bool]:
                self.assertEqual(Path(run).resolve(), destination)
                self.assertIn(cwd, (None, str(self.session_dir)))
                if home is not None:
                    selected.append(home)
                return moved_server.port, True

            with (patch.object(codex_mesh, "workers_home", new=lambda: moved_home),
                  patch.object(codex_mesh, "ensure_server", new=ensure_current)):
                args = self.start_args()
                args.session_dir = str(address)
                args.to = "worker"
                args.message_file = str(self.session_dir / "follow-after-move.txt")
                _ = Path(args.message_file).write_text("Continue on the current account.")
                args.account_move = True
                code = codex_mesh.command_follow(args)
            self.assertEqual(code, 0)
            self.assertIn(moved_home, selected)
            resumes = [request for request in moved_server.requests if request.get("method") == "thread/resume"]
            self.assertTrue(resumes)
            self.assertTrue(all(_as_test_dict(request["params"])["threadId"] == THREAD_ID for request in resumes))
            self.assertFalse(any(request.get("method") in ("thread/start", "thread/fork")
                                 for request in moved_server.requests))
            self.assertFalse(marker.exists(), "the resumed move was left owed")
        owner = _as_test_dict(_as_test_dict(cast(object, json.loads(
            (destination / codex_mesh.ROSTER_FILE).read_text()))).get("worker"))
        self.assertEqual(owner["thread_id"], THREAD_ID)
        self.assertEqual(owner["status"], "done")
        self.assertNotIn("adopted_from", owner, "completed transfer still changes future follow semantics")
        with unit_workers.ledger_for_run(destination).locked() as ledger:
            self.assertEqual(ledger.workers["worker"].backend,
                             unit_workers.CodexThread(THREAD_ID, str(moved_home)))
            self.assertEqual(ledger.workers["worker"].ownership, unit_workers.HeldBy(str(destination)))

    def test_completed_adoption_checks_live_turns_and_can_replace_a_later_lost_rollout(self) -> None:
        self.idle_worker()
        destination = self.destination("ordinary-follow-after-adoption")
        self.assertEqual(self.adopt(destination), 0)
        original_run = self.session_dir
        self.session_dir = destination
        try:
            self.start_args_timeout = 5
            self.assertEqual(self.run_follow("Complete the adoption."), (0, ""))
            self.live_turn = "peer-is-still-working"
            code, _output, errors = self.run_mesh(["can-follow", "--to", "worker", "--role", "impl"])
            self.assertEqual(code, 2, errors)
            self.assertIn("live turn", errors)
            self.live_turn = ""
            replacement = "replacement-after-adoption"
            self.next_thread_id = replacement
            respond = self.respond

            @bounded_fixture
            def missing_rollout(request: dict[str, object]) -> list[dict[str, object]] | None:
                if request.get("method") == "thread/resume":
                    return [{"id": request["id"], "error": {"message": "rollout is no longer available"}}]
                frames = respond(request)
                if frames is not None:
                    for frame in frames:
                        params = _as_test_dict(frame.get("params"))
                        if params.get("threadId") == THREAD_ID:
                            params["threadId"] = replacement
                return frames

            self.server.respond = missing_rollout
            self.assertEqual(self.run_follow("Recover this later missing rollout."), (0, ""))
            self.assertEqual(self.worker_record()["thread_id"], replacement)
            self.assertEqual(len(self.methods("thread/start")), 1)
            with unit_workers.ledger_for_run(destination).locked() as ledger:
                self.assertEqual(ledger.workers["worker"].backend,
                                 unit_workers.CodexThread(replacement, str(agent_accounts.workers_home())))
            persisted = _as_test_dict(cast(object, json.loads(unit_workers.ledger_for_run(destination).path.read_text())))
            entry = _as_test_dict(_as_test_dict(persisted["workers"])["worker"])
            events = cast(list[object], entry["events"])
            self.assertEqual(len([event for event in events if _as_test_dict(event).get("kind") == "replaced"]), 1)
        finally:
            self.session_dir = original_run

    def test_two_racing_adoptions_deliver_queued_work_to_exactly_one_owner(self) -> None:
        self.idle_worker()
        destinations = [self.destination("queued-race-a"), self.destination("queued-race-b")]
        code, _output, errors = self.run_mesh(["send", "--to", "worker", "--message", "Deliver this once."])
        self.assertEqual(code, 0, errors)
        gate = threading.Barrier(2, timeout=4)

        def claim(destination: Path) -> int:
            _ = gate.wait()
            return self.adopt(destination)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(claim, destination) for destination in destinations]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(results.count(0), 1, results)
        owner = destinations[results.index(0)]
        self.assertEqual(self.worker_record()["adopted_to"], str(owner))
        pending = self.session_dir / "worker.pending.json"
        self.assertEqual(_as_test_dict(cast(object, json.loads(pending.read_text())))["messages"], [])
        for destination, code in zip(destinations, results):
            self.assertEqual("worker" in self.adoption_roster(destination), code == 0)
            inbox = destination / "worker.pending.json"
            messages: object = (_as_test_dict(cast(object, json.loads(inbox.read_text()))).get("messages")
                                if inbox.exists() else [])
            self.assertEqual(messages, ["Deliver this once."] if code == 0 else [])
        with unit_workers.ledger_for_run(owner).locked() as ledger:
            self.assertEqual(ledger.workers["worker"].ownership, unit_workers.Adopted(str(owner)))

    def test_two_racing_adoptions_leave_exactly_one_destination_owner(self) -> None:
        self.idle_worker()
        destinations = [self.destination("racing-a"), self.destination("racing-b")]
        gate = threading.Barrier(2, timeout=4)

        def claim(destination: Path) -> int:
            _ = gate.wait()
            try:
                return self.adopt(destination)
            except SystemExit:
                return 2

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(claim, destination) for destination in destinations]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(results.count(0), 1, results)
        owner = destinations[results.index(0)]
        self.assertEqual(self.worker_record()["adopted_to"], str(owner))
        for destination, code in zip(destinations, results):
            entries = self.adoption_roster(destination)
            self.assertEqual("worker" in entries, code == 0)
        with unit_workers.ledger_for_run(owner).locked() as ledger:
            self.assertEqual(ledger.workers["worker"].ownership, unit_workers.Adopted(str(owner)))


class DetachedRunWatchTests(unittest.TestCase):
    """A follower shares one scan and never reopens immutable run records."""

    def test_three_thousand_finished_records_are_read_once_and_live_runs_refresh_at_sixty_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary)
            runs = session / codex_mesh.DETACHED_RUNS_DIR
            runs.mkdir()
            finished_names = {f"finished-{index:04d}.json" for index in range(3_000)}
            for name in finished_names:
                _ = (runs / name).write_text('{"worker":"worker","status":"finished"}')
            active = runs / "active.json"
            _ = active.write_text(json.dumps({"worker": "worker", "status": "running", "pid": os.getpid()}))
            watch = codex_mesh.DetachedRunWatch(str(session), "worker")
            elapsed = 0.0
            read = codex_mesh._read_json_object  # pyright: ignore[reportPrivateUsage]
            with (
                patch.object(codex_mesh, "time", SimpleNamespace(time=time.time, monotonic=bounded_fixture(lambda: elapsed))),
                patch.object(codex_mesh, "_read_json_object", wraps=read) as reads,
            ):
                codex_mesh._end_dead_runs(str(session), "worker", watch)  # pyright: ignore[reportPrivateUsage]
                watch.refresh()
                self.assertTrue(watch.last_observed.live)
                self.assertEqual(reads.call_count, 3_001, "dead/live checks did separate initial scans")
                reads.reset_mock()
                elapsed = 59.9
                codex_mesh._end_dead_runs(str(session), "worker", watch)  # pyright: ignore[reportPrivateUsage]
                watch.refresh()
                self.assertTrue(watch.last_observed.live)
                reads.assert_not_called()
                elapsed = 60.0
                codex_mesh._end_dead_runs(str(session), "worker", watch)  # pyright: ignore[reportPrivateUsage]
                watch.refresh()
                self.assertTrue(watch.last_observed.live)
                reads.assert_called_once_with(active)
                reads.reset_mock()
                _ = active.write_text('{"worker":"worker","status":"finished"}')
                elapsed = 119.9
                watch.refresh()
                self.assertTrue(watch.last_observed.live)
                reads.assert_not_called()
                elapsed = 120.0
                watch.refresh()
                self.assertFalse(watch.last_observed.live)
                reads.assert_called_once_with(active)
                reads.reset_mock()
                elapsed = 180.0
                codex_mesh._end_dead_runs(str(session), "worker", watch)  # pyright: ignore[reportPrivateUsage]
                watch.refresh()
                self.assertFalse(watch.last_observed.live)
                reads.assert_not_called()
            self.assertEqual({path.name for path in runs.glob("*.json")}, finished_names | {"active.json"},
                             "run caching moved or deleted discoverable records")

    def test_lifecycle_decisions_discover_a_run_created_after_the_last_scan(self) -> None:
        for decision in ("close", "wait", "interrupted"):
            with self.subTest(decision=decision), tempfile.TemporaryDirectory() as temporary:
                session = Path(temporary)
                pending = session / "worker.pending.json"
                _ = pending.write_text(json.dumps({"thread_id": THREAD_ID,
                    "messages": [], "accepting_messages": True}))
                watch = codex_mesh.DetachedRunWatch(str(session), "worker")
                watch.refresh()
                self.assertFalse(watch.last_observed.live)
                runs = session / codex_mesh.DETACHED_RUNS_DIR
                runs.mkdir()
                record = runs / "started-after-scan.json"
                _ = record.write_text(json.dumps({"worker": "worker", "status": "running", "pid": os.getpid()}))
                if decision == "close":
                    self.assertFalse(codex_mesh._close_messages_if_empty(  # pyright: ignore[reportPrivateUsage]
                        str(session), "worker", THREAD_ID, watch))
                elif decision == "wait":
                    self.assertTrue(codex_mesh._waiting_on_detached_run(  # pyright: ignore[reportPrivateUsage]
                        str(session), "worker", THREAD_ID, watch))
                else:
                    self.assertTrue(codex_mesh._interrupted_for_held_work(  # pyright: ignore[reportPrivateUsage]
                        "turn/completed", {"turn": {"status": "interrupted"}},
                        str(session), "worker", THREAD_ID, watch))
                self.assertIs(_as_test_dict(cast(object, json.loads(pending.read_text()))).get("accepting_messages"), True)
                self.assertTrue(record.exists(), "liveness discovery removed a lookup record")

    def test_a_supervisor_that_dies_is_reaped_and_its_result_is_held_by_the_next_sixty_second_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary)
            runs = session / codex_mesh.DETACHED_RUNS_DIR
            runs.mkdir()
            _ = (session / codex_mesh.ROSTER_FILE).write_text(json.dumps({"worker": {
                "thread_id": THREAD_ID, "status": "running", "launcher_pid": os.getpid(),
            }}))
            supervisor = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
                                          stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            record = runs / "orphan.json"
            _ = record.write_text(json.dumps({
                "run_id": "orphan", "worker": "worker", "thread_id": THREAD_ID,
                "status": "running", "pid": supervisor.pid, "started_at": time.time(),
                "session_dir": str(session), "label": "supervisor regression",
                "command": ["test-command"], "cwd": str(session), "log": str(runs / "orphan.log"),
                "director_dir": "", "done_message_file": "",
            }))
            watch = codex_mesh.DetachedRunWatch(str(session), "worker")
            elapsed = 0.0
            try:
                with (
                    patch.dict(os.environ, {"DESIGN_ROLES_STATE": str(session / "design-roles"),
                                           "DESIGN_OWNERSHIP_STATE": str(session / "design-ownership")}),
                    patch.object(codex_mesh, "time", SimpleNamespace(time=time.time, monotonic=bounded_fixture(lambda: elapsed))),
                ):
                    codex_mesh._end_dead_runs(str(session), "worker", watch)  # pyright: ignore[reportPrivateUsage]
                    supervisor.terminate()
                    _ = supervisor.wait(timeout=5)
                    elapsed = 59.9
                    codex_mesh._end_dead_runs(str(session), "worker", watch)  # pyright: ignore[reportPrivateUsage]
                    self.assertEqual(cast(dict[str, object], json.loads(record.read_text()))["status"], "running")
                    elapsed = 60.0
                    codex_mesh._end_dead_runs(str(session), "worker", watch)  # pyright: ignore[reportPrivateUsage]
                ended = cast(dict[str, object], json.loads(record.read_text()))
                self.assertEqual(ended.get("status"), "finished")
                self.assertEqual(ended.get("ended"), "supervisor gone")
                pending = cast(dict[str, object], json.loads((session / "worker.pending.json").read_text()))
                self.assertEqual(pending.get("thread_id"), THREAD_ID)
                self.assertIn("supervisor regression", str(pending.get("messages")))
                self.assertIn("FAIL (no exit status)", str(pending.get("messages")))
                self.assertTrue(record.exists(), "reaping removed the externally discoverable run record")
            finally:
                with contextlib.suppress(ProcessLookupError):
                    supervisor.terminate()
                _ = supervisor.wait(timeout=5)
                if supervisor.stdin is not None:
                    supervisor.stdin.close()


def _attempt(
    seconds: float = 3.0, thread_exists: bool = False
) -> codex_mesh.RunOutcome:
    if thread_exists:
        return codex_mesh.FailedWithThread(THREAD_ID, USAGE_LIMIT, seconds)
    return codex_mesh.FailedBeforeThread(USAGE_LIMIT, seconds)


class RetryDecisionTests(unittest.TestCase):
    """Which failures earn a second run against a server this process started."""

    def test_a_fast_failure_on_an_inherited_server_is_retried(self) -> None:
        # The incident this exists for: three workers attach to a server left by
        # an earlier dispatch and all three die in seconds with identical text.
        self.assertTrue(
            codex_mesh._retry_warranted(  # pyright: ignore[reportPrivateUsage]
                _attempt(), fresh_server=False, resident=False
            )
        )

    def test_a_failure_on_a_server_this_call_started_is_final(self) -> None:
        # Nothing was inherited, so the refusal came from the provider and a
        # retry would only ask the same question of the same fresh process.
        self.assertFalse(
            codex_mesh._retry_warranted(  # pyright: ignore[reportPrivateUsage]
                _attempt(), fresh_server=True, resident=False
            )
        )

    def test_a_failure_with_a_thread_is_never_retried(self) -> None:
        # Even if no item was streamed, turn/start may already be doing work.
        self.assertFalse(
            codex_mesh._retry_warranted(  # pyright: ignore[reportPrivateUsage]
                _attempt(thread_exists=True), fresh_server=False, resident=False
            )
        )

    def test_a_sign_in_the_inherited_server_no_longer_holds_is_retried_with_a_thread(self) -> None:
        # Codex was signed in again after the server started, so it cannot refresh its token. The
        # provider refused the turn, so no work was done; a server started now reads the new sign-in.
        self.assertTrue(
            codex_mesh._retry_warranted(  # pyright: ignore[reportPrivateUsage]
                codex_mesh.FailedWithThread(THREAD_ID, STALE_SIGN_IN, 1.0), fresh_server=False, resident=False
            )
        )

    def test_a_stale_sign_in_on_a_fresh_server_or_after_work_is_final(self) -> None:
        for outcome, fresh in ((codex_mesh.FailedWithThread(THREAD_ID, STALE_SIGN_IN, 1.0), True),
                               (codex_mesh.FailedWithThread(
                                   THREAD_ID, STALE_SIGN_IN, codex_mesh.RETRY_FAST_FAILURE_SECS + 1), False)):
            with self.subTest(fresh=fresh, seconds=outcome.seconds):
                self.assertFalse(
                    codex_mesh._retry_warranted(  # pyright: ignore[reportPrivateUsage]
                        outcome, fresh_server=fresh, resident=False
                    )
                )

    def test_start_retries_a_stale_sign_in_on_a_new_server(self) -> None:
        outcomes: list[codex_mesh.RunOutcome] = [
            codex_mesh.FailedWithThread(THREAD_ID, STALE_SIGN_IN, 1.0), codex_mesh.RunCompleted()]
        with tempfile.TemporaryDirectory() as scratch:
            args = argparse.Namespace(session_dir=scratch, name="worker", resident=False,
                                      log_file=str(Path(scratch) / "worker.log"))
            errors = io.StringIO()
            with patch.object(codex_mesh, "ensure_server", side_effect=[(4001, False), (4002, True)]), \
                 patch.object(codex_mesh, "_retire_server", return_value=True) as retire, \
                 patch.object(codex_mesh, "_run_worker", side_effect=outcomes) as run, \
                 contextlib.redirect_stderr(errors):
                code = codex_mesh.command_start(args)
        self.assertEqual(code, 0, errors.getvalue())
        retire.assert_called_once_with(scratch, 4001)
        self.assertEqual([call.args[1] for call in run.call_args_list], [4001, 4002])
        self.assertIn("holds a Codex sign-in that has since changed", errors.getvalue())

    def test_a_slow_failure_reached_the_provider(self) -> None:
        # A cached answer comes back instantly; one that travelled does not.
        self.assertFalse(
            codex_mesh._retry_warranted(  # pyright: ignore[reportPrivateUsage]
                _attempt(seconds=codex_mesh.RETRY_FAST_FAILURE_SECS + 1),
                fresh_server=False,
                resident=False,
            )
        )

    def test_a_resident_worker_is_never_retried(self) -> None:
        # Its caller is already holding replies; a silent second attempt would
        # arrive behind them.
        self.assertFalse(
            codex_mesh._retry_warranted(  # pyright: ignore[reportPrivateUsage]
                _attempt(), fresh_server=False, resident=True
            )
        )


class QuotaRefusalTests(unittest.TestCase):
    """Which usage-limit failures are the provider's own, and so reported to the quota alert."""

    home: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp")))

    @staticmethod
    def refused(outcome: codex_mesh.RunOutcome, tested: bool, resident: bool = False) -> bool:
        return codex_mesh._quota_refused(outcome, tested, resident)  # pyright: ignore[reportPrivateUsage]

    def test_a_refusal_from_a_server_this_launcher_started_is_the_providers(self) -> None:
        self.assertTrue(self.refused(_attempt(), tested=True))
        self.assertTrue(self.refused(_attempt(thread_exists=True), tested=True, resident=True))

    def test_a_fast_refusal_from_an_inherited_server_proves_nothing(self) -> None:
        # The wedged server's replay: same words, no round trip.
        self.assertFalse(self.refused(_attempt(thread_exists=True), tested=False))

    def test_a_refusal_after_real_work_reached_the_provider(self) -> None:
        slow = _attempt(seconds=codex_mesh.RETRY_FAST_FAILURE_SECS + 1, thread_exists=True)
        self.assertTrue(self.refused(slow, tested=False))
        # A resident's clock spans every turn it ever ran.
        self.assertFalse(self.refused(slow, tested=False, resident=True))

    def test_other_failures_and_capacity_are_not_quota(self) -> None:
        self.assertFalse(self.refused(codex_mesh.FailedWithThread(THREAD_ID, CAPACITY, 3.0), tested=True))
        self.assertFalse(self.refused(codex_mesh.RunCompleted(), tested=True))
        self.assertFalse(self.refused(codex_mesh.CapacityRetriesExhausted(THREAD_ID, 3, 20), tested=True))

    def test_refusal_handoff_preserves_named_window_or_unknown_and_observation_time(self) -> None:
        identity = agent_accounts.KnownCodexAccount(self.home / ".codex", "serving@example.invalid", "codex1")
        serving = codex_mesh.ServingCodexServer(4001, 99, identity.home, identity)
        for detail, window in ((USAGE_LIMIT + " Weekly limit reached", "weekly"),
                               (USAGE_LIMIT + " 5-hour limit reached", "five_hour"),
                               (USAGE_LIMIT, "unknown")):
            with self.subTest(window=window), patch.object(subprocess, "run",
                    return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
                codex_mesh._report_quota_refusal("worker", serving, detail, session_dir="/fixture/run", thread_id=THREAD_ID)  # pyright: ignore[reportPrivateUsage]
                run.assert_called_once()
                self.assertNotIn("env", run.call_args.kwargs)
                command = cast(list[str], run.call_args.args[0])
                self.assertEqual(command[2:6], ["blocked", str(identity.home), identity.login, identity.label])
                self.assertEqual(command[6:9], ["--window", window, "--refused-at"])
                self.assertIsNotNone(datetime.fromisoformat(command[9]).tzinfo)


    def test_exited_refusal_handoff_prefers_marker_then_launcher_and_clears_only_its_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            identity = agent_accounts.KnownCodexAccount(folder / "codex", "serving@example.invalid", "codex1")
            serving = codex_mesh.ServingCodexServer(4001, 99, identity.home, identity)
            argv = ["codex_mesh.py", "start", "--prompt", "a task with 'quotes' and spaces"]
            for marked, launched, expected in ((["marker-director"], ("launcher-director",), "marker-director"),
                                               ([], ("launcher-director",), "launcher-director"),
                                               ([], (), ""), (["first", "second"], (), "")):
                with self.subTest(marked=marked, launched=launched):
                    _ = (folder / codex_mesh.ROSTER_FILE).write_text(json.dumps({
                        "worker": {"thread_id": THREAD_ID, "continuation_owed": True},
                        "other": {"thread_id": "other-thread", "continuation_owed": True}}))
                    with (patch.object(codex_mesh, "_marker_sessions", return_value=marked) as marker,
                          patch.object(codex_mesh, "_launcher_sessions", return_value=launched) as launcher,
                          patch.object(sys, "argv", argv), patch.object(subprocess, "run",
                            return_value=subprocess.CompletedProcess([], 0, "", "")) as run):
                        codex_mesh._report_quota_refusal("worker", serving, USAGE_LIMIT, session_dir=str(folder), thread_id=THREAD_ID, stopped=codex_mesh.CommandStoppedOnThread(str(folder), THREAD_ID))  # pyright: ignore[reportPrivateUsage]
                    marker.assert_called_once_with(codex_mesh.SWEEP_ROOTS, folder)
                    if marked:
                        launcher.assert_not_called()
                    else:
                        launcher.assert_called_once_with(folder)
                    command = cast(list[str], run.call_args.args[0])
                    if expected:
                        self.assertEqual(command[command.index("--stopped") + 1:],
                                         [expected, "worker", shlex.join(argv)])
                    else:
                        self.assertNotIn("--stopped", command)
                    roster = cast(dict[str, dict[str, object]], json.loads((folder / codex_mesh.ROSTER_FILE).read_text()))
                    self.assertIs(roster["worker"]["continuation_owed"], False)
                    self.assertIs(roster["other"]["continuation_owed"], True)

    def test_before_thread_exit_uses_its_director_without_rewriting_a_roster(self) -> None:
        identity = agent_accounts.KnownCodexAccount(Path("/fixture/codex"), "worker@example.invalid", "codex1")
        serving = codex_mesh.ServingCodexServer(4001, 99, identity.home, identity)
        for director in (codex_mesh.KnownWorkerDirector("own-director"), codex_mesh.UnknownWorkerDirector()):
            with (self.subTest(director=director),
                  patch.object(codex_mesh, "_marker_sessions") as marker,
                  patch.object(codex_mesh, "_launcher_sessions") as launcher,
                  patch.object(codex_mesh, "_update_roster_if_expected") as update,
                  patch.object(subprocess, "run",
                      return_value=subprocess.CompletedProcess([], 0, "", "")) as run):
                codex_mesh._report_quota_refusal("worker", serving, USAGE_LIMIT, session_dir="/fixture/run", thread_id="",  # pyright: ignore[reportPrivateUsage]
                    stopped=codex_mesh.CommandStoppedBeforeThread("/fixture/run", director))
                marker.assert_not_called()
                launcher.assert_not_called()
                update.assert_not_called()
                command = cast(list[str], run.call_args.args[0])
                if isinstance(director, codex_mesh.KnownWorkerDirector):
                    self.assertEqual(command[command.index("--stopped") + 1:],
                                     ["own-director", "worker", shlex.join(sys.argv)])
                else:
                    self.assertNotIn("--stopped", command)

    def test_waiting_refusal_handoff_does_not_resolve_a_director_or_clear_continuation(self) -> None:
        identity = agent_accounts.KnownCodexAccount(Path("/fixture/codex"), "worker@example.invalid", "codex1")
        serving = codex_mesh.ServingCodexServer(4001, 99, identity.home, identity)
        with (patch.object(codex_mesh, "_marker_sessions") as marker,
              patch.object(codex_mesh, "_launcher_sessions") as launcher,
              patch.object(codex_mesh, "_update_roster_if_expected") as update,
              patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run):
            codex_mesh._report_quota_refusal("worker", serving, USAGE_LIMIT, session_dir="/fixture/run", thread_id=THREAD_ID)  # pyright: ignore[reportPrivateUsage]
        marker.assert_not_called()
        launcher.assert_not_called()
        update.assert_not_called()
        self.assertNotIn("--stopped", cast(list[str], run.call_args.args[0]))

    def test_the_report_runs_the_blocked_verb_and_survives_its_failure(self) -> None:
        identity = agent_accounts.KnownCodexAccount(self.home / ".codex", "serving@example.invalid", "codex1")
        serving = codex_mesh.ServingCodexServer(4001, 99, identity.home, identity)
        done = subprocess.CompletedProcess([], 0, "switched to claude for codex1: worker\n", "")
        with patch.object(subprocess, "run", return_value=done) as run, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            codex_mesh._report_quota_refusal("worker", serving, session_dir="/fixture/run", thread_id=THREAD_ID)  # pyright: ignore[reportPrivateUsage]
        command = cast("list[str]", run.call_args.args[0])
        self.assertEqual(command[1:6], [str(codex_mesh.QUOTA_REPORT), "blocked", str(identity.home), identity.login, identity.label])
        self.assertTrue(codex_mesh.QUOTA_REPORT.is_file())
        self.assertIn("codex_mesh: worker: quota: switched to claude for codex1: worker", errors.getvalue())
        with patch.object(subprocess, "run", side_effect=OSError("no interpreter")), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            codex_mesh._report_quota_refusal("worker", serving, session_dir="/fixture/run", thread_id=THREAD_ID)  # pyright: ignore[reportPrivateUsage]
        self.assertIn("quota refusal not reported: no interpreter", errors.getvalue())


class ThreadStartTests(unittest.TestCase):
    """The speed tier reaches the thread only when the registry sets one."""

    @staticmethod
    def params(service_tier: str) -> dict[str, object]:
        return codex_mesh._thread_start_params(  # pyright: ignore[reportPrivateUsage]
            argparse.Namespace(
                cwd="/work",
                sandbox="danger-full-access",
                model="gpt-test",
                service_tier=service_tier,
            )
        )

    def test_a_registry_tier_is_sent_in_the_config_spelling(self) -> None:
        self.assertEqual(self.params("fast").get("serviceTier"), "fast")

    def test_no_tier_leaves_the_codex_config_in_charge(self) -> None:
        # Sending any value here, even "default", would override the user's
        # ~/.codex/config.toml; omitting the key is what inherits it.
        self.assertNotIn("serviceTier", self.params(""))


class ReplyDeliveryTests(unittest.TestCase):
    """Where a worker's replies land, and whose file the summary is."""

    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    summary: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    replies: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.summary = root / "impl_summary_impl.txt"
        self.replies = root / "impl_reply_impl.txt"

    @override
    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_without_a_reply_file_the_summary_receives_the_answer(self) -> None:
        # The friend launcher's contract: the answer file is the reply.
        _ = self.summary.write_text("stale\n", encoding="utf-8")
        codex_mesh._finish_summary(  # pyright: ignore[reportPrivateUsage]
            self.summary, None, "worker", 1, "the answer"
        )
        self.assertEqual(self.summary.read_text(encoding="utf-8"), "the answer\n")

    def test_the_workers_own_summary_survives_its_last_reply(self) -> None:
        # The incident: every worker's summary came back as its one-line
        # acknowledgement, because the reply had been written over it.
        _ = self.summary.write_text("files, doubts, unverified\n", encoding="utf-8")
        codex_mesh._finish_summary(  # pyright: ignore[reportPrivateUsage]
            self.summary, self.replies, "worker", 1, "Done."
        )
        self.assertEqual(
            self.summary.read_text(encoding="utf-8"), "files, doubts, unverified\n"
        )
        self.assertIn("Done.", self.replies.read_text(encoding="utf-8"))

    def test_a_summary_the_worker_never_wrote_is_filled_from_the_reply(self) -> None:
        # implement.sh truncates the file at launch, so empty at exit means the
        # worker skipped its last act and the reply is all there is.
        _ = self.summary.write_text("", encoding="utf-8")
        codex_mesh._finish_summary(  # pyright: ignore[reportPrivateUsage]
            self.summary, self.replies, "worker", 1, "Done."
        )
        self.assertEqual(self.summary.read_text(encoding="utf-8"), "Done.\n")

    def test_resident_replies_accumulate_in_order(self) -> None:
        _ = self.summary.write_text("the worker's summary\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), io.StringIO() as log:
            for index, text in enumerate(("first", "second"), start=1):
                codex_mesh._deliver_reply(  # pyright: ignore[reportPrivateUsage]
                    "worker", index, text, "", self.summary, self.replies, log
                )
        delivered = self.replies.read_text(encoding="utf-8")
        self.assertLess(delivered.index("first"), delivered.index("second"))
        self.assertEqual(
            self.summary.read_text(encoding="utf-8"), "the worker's summary\n"
        )


@final
class MacCleanupTests(unittest.TestCase):
    home: Path = Path()
    read_executable: Callable[[int], Path] = staticmethod(codex_mesh._process_executable)  # pyright: ignore[reportPrivateUsage]

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HOME": str(self.home)}, clear=True)))
        _ = self.enterContext(patch.object(sys, "platform", "darwin"))
        _ = self.enterContext(patch.object(subprocess, "run", side_effect=AssertionError("unconfigured command")))
        _ = self.enterContext(patch.object(codex_mesh, "user_zone", return_value=ZoneInfo("UTC")))
        _ = self.enterContext(patch.object(account, "mac_process_record", side_effect=AssertionError("no native process record")))
        _ = self.enterContext(patch.object(account, "process_codex_home", side_effect=AssertionError("no native environment")))
        _ = self.enterContext(patch.object(sessions, "process_start", side_effect=AssertionError("no native process start")))
        _ = self.enterContext(patch.object(os, "kill", side_effect=AssertionError("no real signal")))

    def test_mac_server_listing_parses_all_elapsed_forms_and_excludes_non_websocket_targets(self) -> None:
        listing = ("101 03:04 codex app-server --listen ws://127.0.0.1:4101\n"
                   "102 02:03:04 /opt/bin/codex -c key=value app-server --listen ws://[::1]:4102\n"
                   "103 2-03:04:05 codex app-server --listen ws://0.0.0.0:4103\n"
                   "104 03:04 codex app-server daemon\n"
                   "105 03:04 codex app-server --listen unix:///scratch/socket\n"
                   "106 03:04 python tool.py codex app-server --listen ws://localhost:4106\n"
                   "107 03:04 /Applications/ChatGPT.app/Contents/MacOS/codex app-server --listen ws://localhost:4107\n")
        def executable(pid: int) -> Path:
            return Path("/Applications/ChatGPT.app/Contents/MacOS/codex" if pid == 107 else "/opt/bin/codex")
        with (patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, listing, "")) as run,
              patch.object(codex_mesh, "_process_executable", side_effect=executable)):
            found = codex_mesh._running_servers()  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(found, [(101, 4101, 184.0), (102, 4102, 7384.0), (103, 4103, 183845.0)])
        self.assertEqual(cast(tuple[str, ...], run.call_args.args[0]), ("ps", "-eo", "pid=,etime=,args="))

    def test_bundled_codex_executable_cannot_hide_behind_plain_argument_zero_on_either_platform(self) -> None:
        executable = Path("/Applications/ChatGPT.app/Contents/MacOS/codex")
        for platform in ("darwin", "linux"):
            elapsed = "02:00:00" if platform == "darwin" else "7200"
            with (self.subTest(platform=platform), patch.object(sys, "platform", platform),
                  patch.object(codex_mesh, "_list_server_processes", return_value=f"101 {elapsed} codex app-server --listen ws://localhost:4101\n"),
                  patch.object(codex_mesh, "_process_executable", side_effect=self.read_executable),
                  patch.object(account, "mac_process_record", return_value=account.MacProcessRecord(
                      ("codex", "app-server", "daemon"), account.ProcessWithoutCodexHome(), executable)),
                  patch.object(Path, "read_bytes", return_value=b"codex\0app-server\0daemon\0"),
                  patch.object(os, "readlink", return_value=str(executable)),
                  patch.object(sessions, "process_start", return_value="start-101")):
                self.assertEqual(codex_mesh._running_servers(), [])  # pyright: ignore[reportPrivateUsage]
                target = codex_mesh._daemon_record(101)  # pyright: ignore[reportPrivateUsage]
            self.assertIsInstance(target, codex_mesh.UnclassifiedCodexProcess)

    def test_unread_executable_path_keeps_a_websocket_server_instead_of_judging_it_unused(self) -> None:
        with (patch.object(codex_mesh, "_process_executable", side_effect=PermissionError("denied")),
              patch.object(codex_mesh, "_server_folder", return_value=codex_mesh.NonMeshServerStderr()),
              patch.object(codex_mesh, "_busy_threads", return_value=()),
              patch.object(codex_mesh, "_reap") as reap):
            judgment = codex_mesh._judge((self.home,), 101, 4101, 7200, 0)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(judgment.verdict, codex_mesh.ServerInUse)
        self.assertIn("executable", judgment.verdict.reason)
        reap.assert_not_called()

    def test_mac_connections_count_only_the_local_endpoint_for_ipv4_and_ipv6(self) -> None:
        listing = ("COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
                   "codex 101 user 12u IPv4 0x0 0t0 TCP 127.0.0.1:4101->127.0.0.1:60001 (ESTABLISHED)\n"
                   "codex 101 user 13u IPv6 0x0 0t0 TCP [::1]:4101->[::1]:60002 (ESTABLISHED)\n"
                   "python 202 user 4u IPv4 0x0 0t0 TCP 127.0.0.1:60001->127.0.0.1:4101 (ESTABLISHED)\n")
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, listing, "")) as run:
            counts = codex_mesh._client_counts()  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(counts, {4101: 2, 60001: 1})
        self.assertEqual(cast(tuple[str, ...], run.call_args.args[0]), ("lsof", "-nP", "-iTCP", "-sTCP:ESTABLISHED"))

    def test_mac_stderr_distinguishes_a_mesh_log_a_non_mesh_log_and_an_unread_handle(self) -> None:
        cases = (
            (subprocess.CompletedProcess([], 0, "p101\nf2\nn/scratch/run with spaces/mesh_server.log\n", ""),
             codex_mesh.MeshServerLog(Path("/scratch/run with spaces"))),
            (subprocess.CompletedProcess([], 0, "p101\nf2\nn/dev/null\n", ""), codex_mesh.NonMeshServerStderr()),
        )
        for result, expected in cases:
            with self.subTest(expected=expected), patch.object(subprocess, "run", return_value=result) as run:
                self.assertEqual(codex_mesh._server_folder(101), expected)  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(cast(tuple[str, ...], run.call_args.args[0]), ("lsof", "-p", "101", "-a", "-d", "2", "-Fn"))
        for result in (subprocess.CompletedProcess([], 1, "", "permission denied"),
                       subprocess.CompletedProcess([], 2, "", "")):
            with self.subTest(code=result.returncode), patch.object(subprocess, "run", return_value=result):
                unread = codex_mesh._server_folder(101)  # pyright: ignore[reportPrivateUsage]
            self.assertIsInstance(unread, codex_mesh.UnreadServerStderr)
            assert isinstance(unread, codex_mesh.UnreadServerStderr)
            self.assertIn("lsof", unread.reason)

    def test_empty_mac_stderr_selection_requires_positive_evidence_that_the_pid_is_ours(self) -> None:
        for ours in (False, True):
            def command(arguments: tuple[str, ...], **_options: object) -> subprocess.CompletedProcess[str]:
                if arguments[0] == "ps":
                    return subprocess.CompletedProcess(arguments, 0, f"{os.getuid() if ours else os.getuid() + 1}\n", "")
                self.assertEqual(arguments[0], "lsof")
                if "-d" not in arguments and ours:
                    return subprocess.CompletedProcess(arguments, 0, "p101\n", "")
                return subprocess.CompletedProcess(arguments, 1, "", "")
            with self.subTest(ours=ours), patch.object(subprocess, "run", side_effect=command):
                stderr = codex_mesh._server_folder(101)  # pyright: ignore[reportPrivateUsage]
            if ours:
                self.assertIsInstance(stderr, codex_mesh.NonMeshServerStderr)
            else:
                self.assertIsInstance(stderr, codex_mesh.UnreadServerStderr)
                assert isinstance(stderr, codex_mesh.UnreadServerStderr)
                self.assertTrue(stderr.reason)

    def test_empty_lsof_selection_means_zero_clients_but_missing_denied_and_other_exits_refuse(self) -> None:
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "")):
            self.assertEqual(codex_mesh._client_counts(), {})  # pyright: ignore[reportPrivateUsage]
        failures = (FileNotFoundError("missing"),
                    subprocess.CompletedProcess([], 1, "", "denied"),
                    subprocess.CompletedProcess([], 2, "", ""))
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), patch.object(
                subprocess, "run", side_effect=[failure]
            ):
                with self.assertRaisesRegex(OSError, "lsof.*ESTABLISHED"):
                    _ = codex_mesh._client_counts()  # pyright: ignore[reportPrivateUsage]

    def test_failed_mac_ps_or_connection_listing_keeps_sweep_and_idle_stop_from_signalling(self) -> None:
        for failed_command in ("ps", "lsof"):
            def command(arguments: tuple[str, ...], **_options: object) -> subprocess.CompletedProcess[str]:
                if arguments[0] == failed_command:
                    return subprocess.CompletedProcess(arguments, 2, "", "denied")
                return subprocess.CompletedProcess(arguments, 0,
                    "101 02:00:00 codex app-server --listen ws://localhost:4101\n", "")
            with (self.subTest(command=failed_command), patch.object(subprocess, "run", side_effect=command),
                  patch.object(codex_mesh, "_reap") as reap,
                  contextlib.redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(codex_mesh.command_sweep(argparse.Namespace(root=str(self.home), stop=True)), 1)
                self.assertEqual(codex_mesh.command_idle_stop(argparse.Namespace(
                    session_dir=str(self.home), pid=101, port=4101)), 1)
            self.assertIn(failed_command, errors.getvalue())
            self.assertIn("nothing judged", errors.getvalue())
            reap.assert_not_called()

    def test_mac_sweep_and_idle_stop_apply_the_same_idle_and_connected_server_verdicts(self) -> None:
        for operation in ("sweep", "idle-stop"):
            for connected in (False, True):
                with self.subTest(operation=operation, connected=connected):
                    run = self.home / f"{operation}-{connected}"
                    run.mkdir()
                    record = run / codex_mesh.SERVER_FILE
                    _ = record.write_text(json.dumps({"pid": 101, "port": 4101}))
                    old = time.time() - codex_mesh.SERVER_IDLE_SECS - 60
                    os.utime(record, (old, old))
                    def command(arguments: tuple[str, ...], **_options: object) -> subprocess.CompletedProcess[str]:
                        if arguments[0] == "ps":
                            text = "101 02:00:00 codex app-server --listen ws://localhost:4101\n"
                        elif "-p" in arguments:
                            text = f"p101\nf2\nn{run / codex_mesh.SERVER_LOG}\n"
                        else:
                            text = ("codex 101 u 12u IPv4 x 0t0 TCP 127.0.0.1:4101->127.0.0.1:6000 (ESTABLISHED)\n"
                                    if connected else "")
                        return subprocess.CompletedProcess(arguments, 0, text, "")
                    with (patch.object(subprocess, "run", side_effect=command),
                          patch.object(codex_mesh, "_busy_threads", return_value=()),
                          patch.object(codex_mesh, "_reap", return_value=True) as reap,
                          contextlib.redirect_stdout(io.StringIO()) as output):
                        if operation == "sweep":
                            code = codex_mesh.command_sweep(argparse.Namespace(root=str(self.home), stop=True))
                            self.assertEqual(code, 0)
                            self.assertIn("\tin use\t" if connected else "\tstopped\t", output.getvalue())
                        else:
                            code = codex_mesh.command_idle_stop(argparse.Namespace(session_dir=str(run), pid=101, port=4101))
                            self.assertEqual(code, 1 if connected else 0)
                    self.assertEqual([call.args[0] for call in reap.call_args_list], [] if connected else [101])
                    self.assertEqual(record.exists(), connected)

    def test_unread_mac_stderr_keeps_and_names_the_server_in_sweep_and_idle_stop(self) -> None:
        run = self.home / "runs/ended-run"
        run.mkdir(parents=True)
        home = self.home / ".codex"
        record = run / codex_mesh.SERVER_FILE
        for operation in ("sweep", "idle-stop"):
            with self.subTest(operation=operation):
                _ = record.write_text(json.dumps({"pid": 101, "port": 4101, "codex_home": str(home)}))
                def command(arguments: tuple[str, ...], **_options: object) -> subprocess.CompletedProcess[str]:
                    if arguments[0] == "ps":
                        return subprocess.CompletedProcess(arguments, 0,
                            "101 02:00:00 codex app-server --listen ws://localhost:4101\n", "")
                    return subprocess.CompletedProcess(arguments, 2 if "-p" in arguments else 0, "", "")
                with (patch.object(subprocess, "run", side_effect=command),
                      patch.object(codex_mesh, "_pid_alive", return_value=True),
                      patch.object(sessions, "running_sessions", return_value=[]),
                      patch.object(codex_mesh, "_sign_in_changed_at", return_value=2000),
                      patch.object(codex_mesh, "_holds_old_sign_in", return_value=True),
                      patch.object(codex_mesh, "_start_watcher"),
                      patch.object(codex_mesh, "_busy_threads", return_value=()),
                      patch.object(codex_mesh, "_reap") as reap,
                      contextlib.redirect_stdout(io.StringIO()) as output):
                    if operation == "sweep":
                        self.assertEqual(codex_mesh.command_sweep(argparse.Namespace(root=str(run.parent), stop=True)), 0)
                    elif operation == "idle-stop":
                        self.assertEqual(codex_mesh.command_idle_stop(argparse.Namespace(
                            session_dir=str(run), pid=101, port=4101)), 1)
                    self.assertIn("stderr log could not be read", output.getvalue())
                    self.assertIn("lsof", output.getvalue())
                    reap.assert_not_called()

    def test_signin_change_retires_an_unread_server_and_moves_live_workers_before_keeping_it(self) -> None:
        run = self.home / "runs/live-run"
        run.mkdir(parents=True)
        _ = (run / codex_mesh.SERVER_FILE).write_text(json.dumps({
            "pid": 101, "port": 4101, "codex_home": str(self.home / ".codex")}))
        with (patch.object(codex_mesh, "_pid_alive", return_value=True),
              patch.object(codex_mesh, "_marker_sessions", return_value=("director",)),
              patch.object(codex_mesh, "_run_is_live", return_value=True),
              patch.object(codex_mesh, "_sign_in_changed_at", return_value=2000),
              patch.object(codex_mesh, "_holds_old_sign_in", return_value=True),
              patch.object(codex_mesh, "_server_folder", return_value=codex_mesh.UnreadServerStderr("lsof: denied")),
              patch.object(codex_mesh, "_running_servers", return_value=[(101, 4101, 7200.0)]),
              patch.object(codex_mesh, "_client_counts", return_value={}),
              patch.object(codex_mesh, "_switch_run_workers", return_value=()) as switch,
              patch.object(codex_mesh, "workers_home", return_value=self.home / ".codex1"),
              patch.object(codex_mesh, "codex_home_role_label", return_value="codex1"),
              patch.object(codex_mesh, "_notify_holder"),
              patch.object(codex_mesh, "_start_watcher") as watcher,
              patch.object(codex_mesh, "_reap") as reap,
              contextlib.redirect_stdout(io.StringIO()) as output):
            self.assertEqual(codex_mesh.command_signin_changed(argparse.Namespace(root=str(run.parent))), 0)
        switch.assert_called_once_with(run, 4101, "codex1", dry_run=False)
        self.assertFalse((run / codex_mesh.SERVER_FILE).exists())
        retired = cast(dict[str, object], json.loads((run / codex_mesh.RETIRED_FILE).read_text()))
        self.assertIn({"pid": 101, "port": 4101, "codex_home": str(self.home / ".codex")},
                      cast(list[dict[str, object]], retired["servers"]))
        self.assertIn("1 run(s) moved", output.getvalue())
        watcher.assert_called_once_with(str(run), 101, 4101)
        reap.assert_not_called()

    def test_mac_signin_change_stops_an_idle_ended_server_through_the_same_second_look(self) -> None:
        run = self.home / "runs/ended-run"
        run.mkdir(parents=True)
        _ = (run / codex_mesh.SERVER_FILE).write_text(json.dumps({
            "pid": 101, "port": 4101, "codex_home": str(self.home / ".codex")}))
        def command(arguments: tuple[str, ...], **_options: object) -> subprocess.CompletedProcess[str]:
            if arguments[0] == "ps":
                output = "101 02:00:00 codex app-server --listen ws://localhost:4101\n"
            elif "-p" in arguments:
                output = f"p101\nf2\nn{run / codex_mesh.SERVER_LOG}\n"
            else:
                return subprocess.CompletedProcess(arguments, 1, "", "")
            return subprocess.CompletedProcess(arguments, 0, output, "")
        with (patch.object(subprocess, "run", side_effect=command),
              patch.object(codex_mesh, "_pid_alive", return_value=True),
              patch.object(sessions, "running_sessions", return_value=[]),
              patch.object(codex_mesh, "_sign_in_changed_at", return_value=2000),
              patch.object(codex_mesh, "_holds_old_sign_in", return_value=True),
              patch.object(codex_mesh, "_busy_threads", return_value=()),
              patch.object(codex_mesh, "_start_watcher") as watcher,
              patch.object(codex_mesh, "_reap", return_value=True) as reap,
              contextlib.redirect_stdout(io.StringIO()) as output):
            self.assertEqual(codex_mesh.command_signin_changed(argparse.Namespace(root=str(run.parent))), 0)
        reap.assert_called_once_with(101)
        watcher.assert_not_called()
        self.assertFalse((run / codex_mesh.SERVER_FILE).exists())
        self.assertIn("1 leftover servers from ended sessions, 1 stopped", output.getvalue())

    def test_mac_signin_change_keeps_the_record_and_names_a_failed_start_time_command(self) -> None:
        run = self.home / "runs/ended-run"
        run.mkdir(parents=True)
        record = run / codex_mesh.SERVER_FILE
        _ = record.write_text(json.dumps({"pid": 101, "port": 4101,
                                         "codex_home": str(self.home / ".codex")}))
        before = record.read_bytes()
        with (patch.object(subprocess, "run", side_effect=PermissionError("denied")),
              patch.object(codex_mesh, "_pid_alive", return_value=True),
              patch.object(sessions, "running_sessions", return_value=[]),
              patch.object(codex_mesh, "_sign_in_changed_at", return_value=2000),
              patch.object(codex_mesh, "_switch_run_workers") as switch,
              patch.object(codex_mesh, "_reap") as reap,
              contextlib.redirect_stderr(io.StringIO()) as errors,
              contextlib.redirect_stdout(io.StringIO())):
            self.assertEqual(codex_mesh.command_signin_changed(argparse.Namespace(root=str(run.parent))), 0)
        self.assertIn("ps -o etime= -p 101", errors.getvalue())
        self.assertIn("kept", errors.getvalue())
        self.assertEqual(record.read_bytes(), before)
        self.assertFalse((run / codex_mesh.RETIRED_FILE).exists())
        switch.assert_not_called()
        reap.assert_not_called()


@final
class CodexDaemonCleanupTests(unittest.TestCase):
    home: Path = Path()
    discover: Callable[[], list[codex_mesh.DaemonTarget]] = staticmethod(codex_mesh._running_daemons)  # pyright: ignore[reportPrivateUsage]

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.home / ".codex").mkdir()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HOME": str(self.home)}, clear=True)))
        _ = self.enterContext(patch.object(sys, "platform", "darwin"))
        _ = self.enterContext(patch.object(subprocess, "run", side_effect=AssertionError("no native commands")))
        _ = self.enterContext(patch.object(os, "kill", side_effect=AssertionError("no real signal")))
        _ = self.enterContext(patch.object(account, "mac_process_record", side_effect=AssertionError("no native arguments")))
        _ = self.enterContext(patch.object(account, "process_codex_home", side_effect=AssertionError("no native environment")))
        def process_start(pid: int) -> str:
            return f"start-{pid}"
        _ = self.enterContext(patch.object(sessions, "process_start", side_effect=process_start))
        _ = self.enterContext(patch.object(codex_mesh, "_daemon_process_tree", return_value={}))
        _ = self.enterContext(patch.object(codex_mesh, "_list_family_processes", return_value=""))
        _ = self.enterContext(patch.object(codex_mesh, "_process_session", return_value=codex_mesh.ProcessSession(1)))
        _ = self.enterContext(patch.object(codex_mesh, "_daemon_process_state", return_value=codex_mesh.ProcessRunState.RUNNING))
        _ = self.enterContext(patch.object(codex_mesh, "_process_terminal", return_value=codex_mesh.ProcessTerminal("??")))

    def test_sweep_lists_and_stops_only_signed_out_shared_home_daemons_and_their_descendants(self) -> None:
        settings = {
            101: account.ProcessWithoutCodexHome(),
            102: account.ProcessCodexHome(self.home / ".codex"),
            103: account.ProcessCodexHome(self.home / ".codex1"),
            104: account.ProcessEnvironmentUnread("denied"),
            105: account.ProcessWithoutCodexHome(),
        }
        def record(pid: int) -> account.MacProcessRecord | account.ProcessEnvironmentUnread:
            if pid == 106:
                return account.ProcessEnvironmentUnread("denied")
            executable = "/Applications/ChatGPT.app/Contents/MacOS/codex" if pid == 105 else "/opt/bin/codex"
            return account.MacProcessRecord((executable, "app-server", "daemon"), account.ProcessWithoutCodexHome(), Path(executable))
        parents = {101: 1, 102: 1, 103: 1, 104: 1, 105: 1, 201: 101, 202: 201, 203: 102, 999: 1}
        table = {pid: codex_mesh.FamilyProcess(parent, codex_mesh.ProcessIdentity(
            pid, f"start-{pid}", ("/opt/bin/codex", "app-server", "daemon"), Path("/opt/bin/codex")), codex_mesh.ProcessSession(1))
                 for pid, parent in parents.items()}
        table[105] = codex_mesh.FamilyProcess(1, dataclasses.replace(
            cast(codex_mesh.ProcessIdentity, table[105].identity),
            executable=Path("/Applications/ChatGPT.app/Contents/MacOS/codex")), codex_mesh.ProcessSession(1))
        def selected(pid: int) -> account.ProcessCodexHome | account.ProcessWithoutCodexHome | account.ProcessEnvironmentUnread:
            return settings.get(pid, account.ProcessWithoutCodexHome())
        stopped: set[int] = set()
        killed: list[int] = []
        def identity(pid: int) -> codex_mesh.ProcessIdentity | codex_mesh.UnreadCodexProcess:
            member = table.get(pid)
            return member.identity if member is not None else codex_mesh.UnreadCodexProcess(pid, "process record could not be read")
        def state(pid: int) -> codex_mesh.ProcessRunState:
            if pid not in table:
                return codex_mesh.ProcessRunState.GONE
            return codex_mesh.ProcessRunState.STOPPED if pid in stopped else codex_mesh.ProcessRunState.RUNNING
        def signal_process(pid: int, sig: int) -> None:
            if sig == signal.SIGSTOP:
                stopped.add(pid)
            elif sig == signal.SIGCONT:
                stopped.discard(pid)
            elif sig == signal.SIGKILL:
                self.assertIn(pid, stopped)
                killed.append(pid)
                stopped.remove(pid)
                del table[pid]
            else:
                self.fail(f"unexpected signal {sig}")
        def command(arguments: tuple[str, ...], **_options: object) -> subprocess.CompletedProcess[str]:
            self.assertEqual(arguments[0], "ps")
            if "-eo" not in arguments:
                self.assertEqual(arguments, ("ps", "-u", str(os.getuid()), "-o", "pid=,ucomm="))
            listing = "" if "-eo" in arguments else (
                "101 codex   \n102 codex\n103 codex\n104 codex\n105 codex\n106 codex\n999 python\n")
            return subprocess.CompletedProcess(arguments, 0, listing, "")
        for stop in (False, True):
            with (self.subTest(stop=stop), patch.object(subprocess, "run", side_effect=command),
                  patch.object(codex_mesh, "_client_counts", return_value={}),
                  patch.object(codex_mesh, "_running_daemons", side_effect=self.discover),
                  patch.object(account, "mac_process_record", side_effect=record),
                  patch.object(account, "process_codex_home", side_effect=selected),
                  patch.object(codex_mesh, "_daemon_process_tree", return_value=table),
                  patch.object(codex_mesh, "_process_identity", side_effect=identity),
                  patch.object(codex_mesh, "_daemon_process_state", side_effect=state),
                  patch.object(os, "kill", side_effect=signal_process),
                  contextlib.redirect_stdout(io.StringIO()) as output):
                self.assertEqual(codex_mesh.command_sweep(argparse.Namespace(root=str(self.home), stop=stop)), 0)
            rows = {int(parts[0]): parts for line in output.getvalue().splitlines()
                    if len(parts := line.split("\t")) == 5}
            self.assertEqual(set(rows), {101, 102, 103, 104, 106})
            for pid in (101, 102):
                self.assertEqual(rows[pid][1:3], ["daemon", "stopped" if stop else "unused"])
            for pid in (103, 104):
                self.assertEqual(rows[pid][2], "in use")
            self.assertIn("another Codex home", rows[103][3])
            self.assertIn("could not be read", rows[104][3])
            self.assertEqual(rows[106][1:3], ["unclassified", "kept"])
            self.assertIn("could not be read", rows[106][3])
            self.assertIn("4 app-server(s)", output.getvalue())
            self.assertEqual(killed, [202, 201, 101, 203, 102] if stop else [])
            self.assertEqual(stopped, set())

    def test_daemon_is_kept_when_shared_home_auth_exists_or_its_absence_cannot_be_confirmed(self) -> None:
        target = codex_mesh.CodexDaemon(101, "start-101", ("codex", "app-server", "daemon"), Path("/opt/bin/codex"))
        identity = self.home / ".codex/auth.json"
        lstat = Path.lstat
        def denied(path: Path) -> os.stat_result:
            if path == identity:
                raise PermissionError("denied")
            return lstat(path)
        with patch.object(account, "process_codex_home", return_value=account.ProcessWithoutCodexHome()):
            self.assertIsInstance(codex_mesh._daemon_verdict(target), codex_mesh.ServerUnused)  # pyright: ignore[reportPrivateUsage]
            with patch.object(Path, "lstat", denied):
                verdict = codex_mesh._daemon_verdict(target)  # pyright: ignore[reportPrivateUsage]
            self.assertIsInstance(verdict, codex_mesh.ServerInUse)
            self.assertIn("cannot establish", verdict.reason)
            _ = identity.write_text("synthetic identity presence only")
            verdict = codex_mesh._daemon_verdict(target)  # pyright: ignore[reportPrivateUsage]
            self.assertIsInstance(verdict, codex_mesh.ServerInUse)
            self.assertIn("login", verdict.reason)

    def test_unread_codex_invocation_is_reported_without_being_counted_as_an_app_server(self) -> None:
        with (patch.object(codex_mesh, "_running_servers", return_value=[]),
              patch.object(codex_mesh, "_client_counts", return_value={}),
              patch.object(codex_mesh, "_list_codex_processes", return_value="106 codex   \n"),
              patch.object(codex_mesh, "_running_daemons", side_effect=self.discover),
              patch.object(account, "mac_process_record", return_value=account.ProcessEnvironmentUnread("denied")),
              patch.object(codex_mesh, "_reap") as reap,
              contextlib.redirect_stdout(io.StringIO()) as output):
            self.assertEqual(codex_mesh.command_sweep(argparse.Namespace(root=str(self.home), stop=True)), 0)
        self.assertIn("106\t", output.getvalue())
        self.assertIn("could not be read", output.getvalue())
        self.assertIn("0 app-server(s), 0 unused; 0 stopped", output.getvalue())
        self.assertNotIn("106\tdaemon\t", output.getvalue())
        reap.assert_not_called()

    def test_daemon_report_names_its_own_numbered_home(self) -> None:
        target = codex_mesh.CodexDaemon(101, "start-101", ("codex", "app-server", "daemon"), Path("/opt/bin/codex"))
        with (patch.object(codex_mesh, "_running_servers", return_value=[]),
              patch.object(codex_mesh, "_client_counts", return_value={}),
              patch.object(codex_mesh, "_running_daemons", return_value=[target]),
              patch.object(account, "process_codex_home", return_value=account.ProcessCodexHome(self.home / ".codex1")),
              contextlib.redirect_stdout(io.StringIO()) as output):
            self.assertEqual(codex_mesh.command_sweep(argparse.Namespace(root=str(self.home), stop=False)), 0)
        row = next(line.split("\t") for line in output.getvalue().splitlines() if line.startswith("101\t"))
        self.assertEqual(row[1:3], ["daemon", "in use"])
        self.assertEqual(row[4], "~/.codex1")

    def test_sweep_discovers_reports_and_resumes_a_stopped_shared_home_non_daemon(self) -> None:
        arguments = ("codex", "--remote-control")
        member = codex_mesh.UnclassifiedCodexProcess(201, "start-201", arguments, Path("/opt/bin/codex"))
        stopped = {201}
        signals: list[tuple[int, int]] = []
        def state(pid: int) -> codex_mesh.ProcessRunState:
            self.assertEqual(pid, 201)
            return codex_mesh.ProcessRunState.STOPPED if pid in stopped else codex_mesh.ProcessRunState.RUNNING
        def send_signal(pid: int, sig: int) -> None:
            signals.append((pid, sig))
            self.assertEqual((pid, sig), (201, signal.SIGCONT))
            stopped.remove(pid)
        with (patch.object(codex_mesh, "_running_servers", return_value=[]),
              patch.object(codex_mesh, "_client_counts", return_value={}),
              patch.object(codex_mesh, "_list_codex_processes", return_value="201 codex\n"),
              patch.object(codex_mesh, "_running_daemons", side_effect=self.discover),
              patch.object(codex_mesh, "_daemon_process_tree", return_value={
                  201: codex_mesh.FamilyProcess(1, member, codex_mesh.ProcessSession(201))}),
              patch.object(account, "mac_process_record", return_value=account.MacProcessRecord(
                  arguments, account.ProcessWithoutCodexHome(), Path("/opt/bin/codex"))),
              patch.object(account, "process_codex_home", return_value=account.ProcessWithoutCodexHome()),
              patch.object(codex_mesh, "_daemon_process_state", side_effect=state),
              patch.object(os, "kill", side_effect=send_signal),
              contextlib.redirect_stdout(io.StringIO()) as output,
              contextlib.redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(codex_mesh.command_sweep(argparse.Namespace(root=str(self.home), stop=True)), 0)
        self.assertEqual(signals, [(201, signal.SIGCONT)])
        self.assertEqual(stopped, set())
        diagnostic = output.getvalue() + errors.getvalue()
        self.assertIn("201", diagnostic)
        self.assertIn("stopped", diagnostic)
        self.assertIn("0 app-server(s)", output.getvalue())

    def test_linux_daemon_discovery_reads_cmdline_and_uses_the_same_home_and_login_verdict(self) -> None:
        with (patch.object(sys, "platform", "linux"),
              patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "101 codex\n", "")) as command,
              patch.object(Path, "read_bytes", return_value=b"/opt/bin/codex\0app-server\0daemon\0") as arguments,
              patch.object(account, "process_codex_home", return_value=account.ProcessWithoutCodexHome())):
            found = self.discover()
            self.assertEqual(found, [codex_mesh.CodexDaemon(101, "start-101", ("/opt/bin/codex", "app-server", "daemon"), Path("/opt/bin/codex"))])
            self.assertIsInstance(codex_mesh._daemon_verdict(found[0]), codex_mesh.ServerUnused)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(cast(tuple[str, ...], command.call_args.args[0]), ("ps", "-u", str(os.getuid()), "-o", "pid=,comm="))
        arguments.assert_called_once_with()

    def test_daemon_discovery_excludes_exec_prompts_and_accepts_real_subcommands_after_config_flags(self) -> None:
        arguments = {
            101: ("codex", "exec", "app-server", "daemon"),
            102: ("codex", "exec", "app-server daemon"),
            103: ("codex", "-c", "prompt=app-server daemon", "exec", "task"),
            104: ("codex", "-c", "key=value", "--enable=feature", "app-server", "daemon"),
        }
        def record(pid: int) -> account.MacProcessRecord:
            return account.MacProcessRecord(arguments[pid], account.ProcessWithoutCodexHome(), Path("/opt/bin/codex"))
        listing = "".join(f"{pid} codex\n" for pid in arguments)
        with (patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, listing, "")),
              patch.object(account, "mac_process_record", side_effect=record)):
            found = self.discover()
        self.assertEqual(found, [codex_mesh.CodexDaemon(104, "start-104", arguments[104], Path("/opt/bin/codex"))])

    def test_readable_codex_exec_record_is_unclassified_even_when_its_prompt_names_a_daemon(self) -> None:
        arguments = ("codex", "exec", "app-server", "daemon")
        with patch.object(account, "mac_process_record", return_value=account.MacProcessRecord(
                arguments, account.ProcessWithoutCodexHome(), Path("/opt/bin/codex"))):
            target = codex_mesh._daemon_record(101)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(target, codex_mesh.UnclassifiedCodexProcess)
        assert isinstance(target, codex_mesh.UnclassifiedCodexProcess)
        self.assertEqual(target.arguments, arguments)



@final
class DaemonProcessStateTests(unittest.TestCase):
    def test_session_reader_uses_the_kernel_id_and_tags_unreadable_sessions_on_both_platforms(self) -> None:
        for platform in ("darwin", "linux"):
            with (self.subTest(platform=platform), patch.object(sys, "platform", platform),
                  patch.object(os, "getsid", return_value=101) as get_session):
                self.assertEqual(codex_mesh._process_session(201), codex_mesh.ProcessSession(101))  # pyright: ignore[reportPrivateUsage]
                get_session.assert_called_once_with(201)
            for error, expected in ((ProcessLookupError("synthetic exit"), codex_mesh.SessionGone),
                                    (PermissionError("synthetic denied"), codex_mesh.SessionUnread)):
                with (self.subTest(platform=platform, error=type(error).__name__),
                      patch.object(sys, "platform", platform), patch.object(os, "getsid", side_effect=error)):
                    self.assertIsInstance(codex_mesh._process_session(201), expected)  # pyright: ignore[reportPrivateUsage]

    def test_own_user_listing_preserves_the_complete_command_path_and_arguments_with_spaces(self) -> None:
        arguments = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --profile-directory="Profile 2"'
        for platform in ("darwin", "linux"):
            with (self.subTest(platform=platform), patch.object(sys, "platform", platform),
                  patch.object(os, "getuid", return_value=1000),
                  patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                      [], 0, f"  201   101 {arguments}\n", "")) as command):
                table = codex_mesh._own_process_table()  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(set(table), {201})
            self.assertEqual((table[201].pid, table[201].parent, table[201].arguments), (201, 101, arguments))
            self.assertEqual(cast(tuple[str, ...], command.call_args.args[0]),
                             ("ps", "-u", "1000", "-o", "pid=,ppid=,args="))

    def test_mac_process_state_distinguishes_stopped_running_zombie_and_gone(self) -> None:
        cases = (("T+\n", 0, codex_mesh.ProcessRunState.STOPPED),
                 ("S\n", 0, codex_mesh.ProcessRunState.RUNNING),
                 ("Z\n", 0, codex_mesh.ProcessRunState.GONE),
                 ("", 1, codex_mesh.ProcessRunState.GONE))
        for output, exit_code, expected in cases:
            with (self.subTest(output=output), patch.object(sys, "platform", "darwin"),
                  patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], exit_code, output, "")) as command):
                self.assertEqual(codex_mesh._daemon_process_state(101), expected)  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(cast(tuple[str, ...], command.call_args.args[0]), ("ps", "-p", "101", "-o", "stat="))

    def test_linux_process_state_reads_state_after_the_complete_parenthesized_command(self) -> None:
        for state, expected in (("T", codex_mesh.ProcessRunState.STOPPED),
                                ("S", codex_mesh.ProcessRunState.RUNNING),
                                ("Z", codex_mesh.ProcessRunState.GONE)):
            with (self.subTest(state=state), patch.object(sys, "platform", "linux"),
                  patch.object(Path, "read_text", return_value=f"101 (codex with ) in name) {state} 1 0 0"),
                  patch.object(subprocess, "run", side_effect=AssertionError("Linux reads only fake stat"))):
                self.assertEqual(codex_mesh._daemon_process_state(101), expected)  # pyright: ignore[reportPrivateUsage]

    def test_denied_process_state_refuses_instead_of_claiming_a_process_is_gone(self) -> None:
        for platform in ("linux", "darwin"):
            with (self.subTest(platform=platform), patch.object(sys, "platform", platform),
                  patch.object(Path, "read_text", side_effect=PermissionError("denied")),
                  patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 2, "", "denied"))):
                with self.assertRaises(OSError):
                    _ = codex_mesh._daemon_process_state(101)  # pyright: ignore[reportPrivateUsage]


@final
class DaemonFamilySafetyTests(unittest.TestCase):
    home: Path = Path()
    identities: dict[int, codex_mesh.ProcessIdentity | codex_mesh.UnreadCodexProcess] = {}
    parents: dict[int, int] = {}
    starts: dict[int, str] = {}
    process_sessions: dict[int, int] = {}
    stopped: set[int] = set()
    signals: list[tuple[int, int]] = []
    target = codex_mesh.CodexDaemon(101, "parent-start", ("codex", "app-server", "daemon"), Path("/opt/bin/codex"))
    child = codex_mesh.ProcessIdentity(201, "child-start", ("codex", "--remote-control"), Path("/opt/bin/codex"))
    native_listing: Callable[[], str] = staticmethod(codex_mesh._list_family_processes)  # pyright: ignore[reportPrivateUsage]
    discover: Callable[[], list[codex_mesh.DaemonTarget]] = staticmethod(codex_mesh._running_daemons)  # pyright: ignore[reportPrivateUsage]
    native_session: Callable[[int], codex_mesh.ProcessSession | codex_mesh.SessionGone | codex_mesh.SessionUnread] = staticmethod(codex_mesh._process_session)  # pyright: ignore[reportPrivateUsage]
    native_terminal: Callable[[int], codex_mesh.ProcessTerminal | codex_mesh.TerminalUnread] = staticmethod(codex_mesh._process_terminal)  # pyright: ignore[reportPrivateUsage]
    snapshot: Callable[[int, Mapping[int, codex_mesh.ProcessIdentity], frozenset[int]], dict[int, codex_mesh.FamilyProcess]] = staticmethod(codex_mesh._daemon_process_tree)  # pyright: ignore[reportPrivateUsage]

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.home / ".codex").mkdir()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HOME": str(self.home)}, clear=True)))
        _ = self.enterContext(patch.object(sys, "platform", "darwin"))
        _ = self.enterContext(patch.object(subprocess, "run", side_effect=AssertionError("no native command")))
        _ = self.enterContext(patch.object(account, "mac_process_record", side_effect=AssertionError("no native record")))
        self.identities = {101: self.target, 201: self.child}
        self.parents = {101: 1, 201: 101}
        self.starts = {101: self.target.started, 201: self.child.started}
        self.process_sessions = {101: 101, 201: 101}
        _ = self.enterContext(patch.object(codex_mesh, "_process_session", side_effect=self.process_session))
        _ = self.enterContext(patch.object(codex_mesh, "_process_terminal", return_value=codex_mesh.ProcessTerminal("??")))
        _ = self.enterContext(patch.object(codex_mesh, "_list_family_processes", side_effect=self.listing))
        _ = self.enterContext(patch.object(sessions, "process_start", side_effect=self.starts.__getitem__))
        self.stopped = set()
        self.signals = []
        _ = self.enterContext(patch.object(account, "process_codex_home", return_value=account.ProcessWithoutCodexHome()))
        _ = self.enterContext(patch.object(codex_mesh, "_process_identity", side_effect=self.identity))
        _ = self.enterContext(patch.object(codex_mesh, "_daemon_process_tree", side_effect=self.tree))
        _ = self.enterContext(patch.object(os, "kill", side_effect=self.signal_process))
        _ = self.enterContext(patch.object(codex_mesh, "_daemon_process_state", side_effect=self.state))
        _ = self.enterContext(patch.object(time, "sleep", side_effect=AssertionError("no real sleep")))

    def identity(self, pid: int) -> codex_mesh.ProcessIdentity | codex_mesh.UnreadCodexProcess:
        return self.identities.get(pid, codex_mesh.UnreadCodexProcess(pid, "process exited"))

    def process_session(self, pid: int) -> codex_mesh.ProcessSession | codex_mesh.SessionUnread:
        sid = self.process_sessions.get(pid, self.process_sessions.get(self.parents.get(pid, 1), pid))
        return codex_mesh.ProcessSession(sid)

    def listing(self) -> str:
        return "".join(f"{pid} {parent} synthetic-process-{pid}\n" for pid, parent in self.parents.items())

    def tree(self, *_arguments: object) -> dict[int, codex_mesh.FamilyProcess]:
        return {pid: codex_mesh.FamilyProcess(parent, self.identities[pid], self.process_session(pid))
                for pid, parent in self.parents.items()}

    def state(self, pid: int) -> codex_mesh.ProcessRunState:
        if pid not in self.parents:
            return codex_mesh.ProcessRunState.GONE
        return codex_mesh.ProcessRunState.STOPPED if pid in self.stopped else codex_mesh.ProcessRunState.RUNNING

    def signal_process(self, pid: int, sig: int) -> None:
        self.assertIn(pid, self.parents)
        self.signals.append((pid, sig))
        if sig == signal.SIGSTOP:
            self.stopped.add(pid)
        elif sig == signal.SIGCONT:
            self.stopped.discard(pid)
        elif sig == signal.SIGKILL:
            self.assertIn(pid, self.stopped, "only a confirmed frozen process may be killed")
            self.stopped.remove(pid)
            del self.parents[pid]
            del self.identities[pid]
        else:
            self.fail(f"unexpected daemon family signal {sig}")

    def test_signed_out_family_is_frozen_parent_first_and_killed_children_first(self) -> None:
        self.assertTrue(codex_mesh._stop_daemon(self.target))  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(self.signals, [(101, signal.SIGSTOP), (201, signal.SIGSTOP),
                                        (201, signal.SIGKILL), (101, signal.SIGKILL)])
        self.assertEqual(self.parents, {})
        self.assertEqual(self.stopped, set())

    def test_sweep_reports_a_kept_daemons_stopped_shell_child_and_resumes_only_on_stop(self) -> None:
        shell = dataclasses.replace(self.child, arguments=("sh", "helper"), executable=Path("/bin/sh"))
        self.identities[201] = shell
        self.identities[101] = codex_mesh.ProcessIdentity(
            self.target.pid, self.target.started, self.target.arguments, self.target.executable)
        for stop in (False, True):
            self.stopped = {201}
            self.signals = []
            with (self.subTest(stop=stop),
                  patch.object(codex_mesh, "_running_servers", return_value=[]),
                  patch.object(codex_mesh, "_client_counts", return_value={}),
                  patch.object(codex_mesh, "_list_codex_processes", return_value="101 codex\n201 sh\n"),
                  patch.object(codex_mesh, "_running_daemons", side_effect=self.discover),
                  patch.object(codex_mesh, "_daemon_record", return_value=self.target),
                  patch.object(codex_mesh, "_daemon_process_tree", side_effect=self.snapshot),
                  patch.object(codex_mesh, "_daemon_verdict", return_value=codex_mesh.ServerInUse("shared home has a login")),
                  contextlib.redirect_stdout(io.StringIO()) as output,
                  contextlib.redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(codex_mesh.command_sweep(argparse.Namespace(root=str(self.home), stop=stop)), 0)
            diagnostic = output.getvalue() + errors.getvalue()
            self.assertIn("201", diagnostic)
            self.assertIn("stopped", diagnostic)
            self.assertEqual(self.signals, [(201, signal.SIGCONT)] if stop else [])
            self.assertEqual(self.stopped, set() if stop else {201})
            self.assertEqual(set(self.parents), {101, 201})

    def test_sweep_keeps_and_names_stopped_shell_members_with_unsafe_recovery_evidence(self) -> None:
        shell = dataclasses.replace(self.child, arguments=("sh", "helper"), executable=Path("/bin/sh"))
        cases = (
            (dataclasses.replace(shell, executable=Path("/Applications/ChatGPT.app/Contents/MacOS/codex")),
             account.ProcessWithoutCodexHome(), False),
            (shell, account.ProcessCodexHome(self.home / ".codex1"), False),
            (shell, account.ProcessEnvironmentUnread("synthetic denied"), False),
            (codex_mesh.UnreadCodexProcess(201, "synthetic unread identity"), account.ProcessWithoutCodexHome(), False),
            (shell, account.ProcessWithoutCodexHome(), True),
        )
        for identity, setting, changed in cases:
            self.identities[201] = identity
            self.stopped = {201}
            self.signals = []
            discovered = self.tree()
            if changed:
                self.identities[201] = dataclasses.replace(shell, started="replacement-start")
            def selected(pid: int) -> account.ProcessCodexHome | account.ProcessWithoutCodexHome | account.ProcessEnvironmentUnread:
                return setting if pid == 201 else account.ProcessWithoutCodexHome()
            with (self.subTest(identity=identity, setting=setting, changed=changed),
                  patch.object(codex_mesh, "_running_servers", return_value=[]),
                  patch.object(codex_mesh, "_client_counts", return_value={}),
                  patch.object(codex_mesh, "_running_daemons", return_value=[self.target]),
                  patch.object(codex_mesh, "_daemon_process_tree", return_value=discovered),
                  patch.object(codex_mesh, "_daemon_verdict", return_value=codex_mesh.ServerInUse("shared home has a login")),
                  patch.object(account, "process_codex_home", side_effect=selected),
                  contextlib.redirect_stdout(io.StringIO()) as output,
                  contextlib.redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(codex_mesh.command_sweep(argparse.Namespace(root=str(self.home), stop=True)), 0)
            diagnostic = output.getvalue() + errors.getvalue()
            self.assertIn("201", diagnostic)
            self.assertIn("kept", diagnostic.lower())
            self.assertEqual(self.signals, [])
            self.assertEqual(self.stopped, {201})

    def test_sweep_preserves_job_control_stops_and_recovers_only_detached_members(self) -> None:
        member = codex_mesh.UnclassifiedCodexProcess(
            self.child.pid, self.child.started, self.child.arguments, self.child.executable)
        self.identities[201] = member
        for platform, held, detached in (("linux", "pts/3", "?"), ("darwin", "ttys003", "??")):
            for terminal in (held, detached, ""):
                self.stopped = {201}
                self.signals = []
                with (self.subTest(platform=platform, terminal=terminal),
                      patch.object(sys, "platform", platform),
                      patch.object(codex_mesh, "_running_servers", return_value=[]),
                      patch.object(codex_mesh, "_client_counts", return_value={}),
                      patch.object(codex_mesh, "_running_daemons", return_value=[member]),
                      patch.object(codex_mesh, "_process_terminal", side_effect=self.native_terminal),
                      patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                          [], 0, terminal + "\n" if terminal else "", "")) as command,
                      contextlib.redirect_stdout(io.StringIO()) as output,
                      contextlib.redirect_stderr(io.StringIO()) as errors):
                    self.assertEqual(codex_mesh.command_sweep(argparse.Namespace(root=str(self.home), stop=True)), 0)
                self.assertEqual(cast(tuple[str, ...], command.call_args.args[0]), ("ps", "-p", "201", "-o", "tty="))
                diagnostic = output.getvalue() + errors.getvalue()
                self.assertIn("201", diagnostic)
                if terminal == detached:
                    self.assertEqual(self.signals, [(201, signal.SIGCONT)])
                    self.assertEqual(self.stopped, set())
                else:
                    self.assertEqual(self.signals, [])
                    self.assertEqual(self.stopped, {201})
                    self.assertIn("kept", diagnostic)
                    self.assertIn(held if terminal else "could not be read", diagnostic)

    def test_session_candidate_exit_is_skipped_but_unreadable_session_refuses_and_resumes_the_family(self) -> None:
        for denied in (False, True):
            self.identities = {101: self.target, 201: self.child,
                               203: dataclasses.replace(self.child, pid=203, started="short-lived")}
            self.parents = {101: 1, 201: 101, 203: 1}
            self.stopped = set()
            self.signals = []
            listings = 0
            candidate_reads = 0
            def listing() -> str:
                nonlocal listings
                listings += 1
                return self.listing()
            def get_session(pid: int) -> int:
                nonlocal candidate_reads
                if pid == 203:
                    candidate_reads += 1
                    if denied:
                        raise PermissionError("synthetic denied")
                    raise ProcessLookupError("synthetic exited")
                return self.process_sessions[pid]
            with (self.subTest(denied=denied),
                  patch.object(codex_mesh, "_list_family_processes", side_effect=listing),
                  patch.object(codex_mesh, "_process_session", side_effect=self.native_session),
                  patch.object(codex_mesh, "_daemon_process_tree", side_effect=self.snapshot),
                  patch.object(os, "getsid", side_effect=get_session),
                  contextlib.redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(codex_mesh._stop_daemon(self.target), not denied)  # pyright: ignore[reportPrivateUsage]
            self.assertGreater(listings, 0)
            self.assertEqual(candidate_reads, listings, "read a candidate's session once per process table")
            self.assertFalse(any(pid == 203 for pid, _sig in self.signals))
            if denied:
                self.assertIn("203", errors.getvalue())
                self.assertIn("could not be read", errors.getvalue())
                self.assertEqual(self.signals, [(101, signal.SIGSTOP), (201, signal.SIGSTOP),
                                                (201, signal.SIGCONT), (101, signal.SIGCONT)])
                self.assertEqual(set(self.parents), {101, 201, 203})
            else:
                self.assertEqual(self.signals, [(101, signal.SIGSTOP), (201, signal.SIGSTOP),
                                                (201, signal.SIGKILL), (101, signal.SIGKILL)])
                self.assertNotIn("not stopped", errors.getvalue())
                self.assertEqual(self.parents, {203: 1})
            self.assertEqual(self.stopped, set())

    def test_kept_family_recovery_names_the_cleanup_holding_its_lock(self) -> None:
        self.stopped = {201}
        with (codex_mesh._daemon_stop_lock(),  # pyright: ignore[reportPrivateUsage]
              contextlib.redirect_stderr(io.StringIO()) as errors):
            self.assertFalse(codex_mesh._recover_kept_daemon(self.target))  # pyright: ignore[reportPrivateUsage]
        self.assertIn("another daemon cleanup holds the stop lock", errors.getvalue())
        self.assertNotIn("recovery failed", errors.getvalue())
        self.assertEqual(self.signals, [])
        self.assertEqual(self.stopped, {201})

    def test_session_orphan_uses_its_table_session_without_a_second_racy_read(self) -> None:
        table_number = 0
        orphan_reads: dict[int, int] = {}
        def listing() -> str:
            nonlocal table_number
            table_number += 1
            return self.listing()
        def get_session(pid: int) -> int:
            if pid == 203 and pid not in self.stopped:
                orphan_reads[table_number] = orphan_reads.get(table_number, 0) + 1
                if orphan_reads[table_number] > 1:
                    raise ProcessLookupError("candidate exited after its table session was read")
            return self.process_sessions[pid]
        def signal_process(pid: int, sig: int) -> None:
            self.signal_process(pid, sig)
            if (pid, sig) == (201, signal.SIGSTOP):
                self.parents[203] = 1
                self.identities[203] = dataclasses.replace(self.child, pid=203, started="orphan-start")
                self.starts[203] = "orphan-start"
                self.process_sessions[203] = 101
        with (patch.object(codex_mesh, "_list_family_processes", side_effect=listing),
              patch.object(codex_mesh, "_process_session", side_effect=self.native_session),
              patch.object(codex_mesh, "_daemon_process_tree", side_effect=self.snapshot),
              patch.object(os, "getsid", side_effect=get_session),
              patch.object(os, "kill", side_effect=signal_process)):
            self.assertTrue(codex_mesh._stop_daemon(self.target))  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(len(orphan_reads), 2, "both tables bind the orphan to its session before freezing it")
        self.assertTrue(all(count == 1 for count in orphan_reads.values()))
        self.assertIn((203, signal.SIGSTOP), self.signals)
        self.assertIn((203, signal.SIGKILL), self.signals)
        self.assertEqual(self.parents, {})

    def test_reparented_member_remains_tracked_until_frozen_and_killed(self) -> None:
        def signal_process(pid: int, sig: int) -> None:
            self.signal_process(pid, sig)
            if (pid, sig) == (101, signal.SIGSTOP):
                self.parents[201] = 1
        with patch.object(os, "kill", side_effect=signal_process):
            self.assertTrue(codex_mesh._stop_daemon(self.target))  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(self.signals, [(101, signal.SIGSTOP), (201, signal.SIGSTOP),
                                        (201, signal.SIGKILL), (101, signal.SIGKILL)])
        self.assertNotIn(201, self.parents)

    def test_reparented_member_must_be_gone_before_shutdown_reports_success(self) -> None:
        pending: set[int] = set()
        waited: list[int] = []
        def signal_process(pid: int, sig: int) -> None:
            if sig == signal.SIGKILL:
                self.assertIn(pid, self.stopped)
                self.signals.append((pid, sig))
                pending.add(pid)
            else:
                self.signal_process(pid, sig)
                if (pid, sig) == (101, signal.SIGSTOP):
                    self.parents[201] = 1
        @bounded_fixture
        def advance_shutdown(duration: float) -> None:
            self.assertGreaterEqual(duration, 0)
            self.assertTrue(pending, "only a killed member may need an exit wait")
            for pid in tuple(pending):
                waited.append(pid)
                pending.remove(pid)
                self.stopped.remove(pid)
                del self.parents[pid]
                del self.identities[pid]
        with (patch.object(os, "kill", side_effect=signal_process),
              patch.object(time, "sleep", new=advance_shutdown)):
            self.assertTrue(codex_mesh._stop_daemon(self.target))  # pyright: ignore[reportPrivateUsage]
        self.assertIn(201, waited)
        self.assertEqual(self.parents, {})
        self.assertEqual(pending, set())

    def test_stop_signal_is_confirmed_by_process_state_before_any_member_is_killed(self) -> None:
        confirmed: set[int] = set()
        needs_confirmation: set[int] = set()
        def state(pid: int) -> codex_mesh.ProcessRunState:
            value = self.state(pid)
            if value == codex_mesh.ProcessRunState.STOPPED:
                if pid not in needs_confirmation:
                    needs_confirmation.add(pid)
                    return codex_mesh.ProcessRunState.RUNNING
                confirmed.add(pid)
            return value
        def signal_process(pid: int, sig: int) -> None:
            if sig == signal.SIGKILL:
                self.assertEqual(confirmed, {101, 201})
            self.signal_process(pid, sig)
        with (patch.object(codex_mesh, "_daemon_process_state", side_effect=state),
              patch.object(os, "kill", side_effect=signal_process),
              patch.object(time, "sleep", new=bounded_fixture(fixture_skip_sleep))):
            self.assertTrue(codex_mesh._stop_daemon(self.target))  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(confirmed, {101, 201})

    def test_member_exiting_before_freeze_resumes_the_family_and_reports_unconfirmed_absence(self) -> None:
        def signal_process(pid: int, sig: int) -> None:
            self.signal_process(pid, sig)
            if (pid, sig) == (101, signal.SIGSTOP):
                del self.parents[201]
                del self.identities[201]
                self.parents[202] = 1
                self.identities[202] = dataclasses.replace(self.child, pid=202, started="orphan-start")
        with (patch.object(os, "kill", side_effect=signal_process),
              contextlib.redirect_stderr(io.StringIO()) as errors):
            self.assertFalse(codex_mesh._stop_daemon(self.target))  # pyright: ignore[reportPrivateUsage]
        self.assertIn("member exited before the family was frozen; family absence is unconfirmed", errors.getvalue())
        self.assertEqual(self.signals, [(101, signal.SIGSTOP), (101, signal.SIGCONT)])
        self.assertEqual(self.stopped, set())
        self.assertIn(202, self.parents)

    def test_child_born_while_freezing_is_discovered_on_the_next_pass_before_any_kill(self) -> None:
        def signal_process(pid: int, sig: int) -> None:
            if (pid, sig) == (201, signal.SIGSTOP):
                self.parents[202] = 201
                self.identities[202] = dataclasses.replace(self.child, pid=202, started="late-child-start")
            self.signal_process(pid, sig)
        with patch.object(os, "kill", side_effect=signal_process):
            self.assertTrue(codex_mesh._stop_daemon(self.target))  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(self.signals, [(101, signal.SIGSTOP), (201, signal.SIGSTOP), (202, signal.SIGSTOP),
                                        (202, signal.SIGKILL), (201, signal.SIGKILL), (101, signal.SIGKILL)])
        self.assertEqual(self.parents, {})

    def test_unseen_intermediates_orphan_is_found_in_a_frozen_leaders_session_before_any_kill(self) -> None:
        self.process_sessions[201] = 201
        def signal_process(pid: int, sig: int) -> None:
            if (pid, sig) == (201, signal.SIGSTOP):
                # An intermediate forked and exited between listings. Only its
                # orphan and the frozen session leader remain observable.
                self.parents[203] = 1
                self.identities[203] = dataclasses.replace(self.child, pid=203, started="orphan-start")
                self.process_sessions[203] = 201
                self.starts[203] = "orphan-start"
            if sig == signal.SIGKILL and not any(event[1] == signal.SIGKILL for event in self.signals):
                self.assertEqual(self.stopped, {101, 201, 203})
            self.signal_process(pid, sig)
        with patch.object(os, "kill", side_effect=signal_process):
            self.assertTrue(codex_mesh._stop_daemon(self.target))  # pyright: ignore[reportPrivateUsage]
        self.assertIn((203, signal.SIGSTOP), self.signals)
        self.assertIn((203, signal.SIGKILL), self.signals)
        self.assertEqual(self.parents, {})

    def test_survivor_in_a_swept_session_refuses_success_and_names_its_pid(self) -> None:
        def signal_process(pid: int, sig: int) -> None:
            self.signal_process(pid, sig)
            if (pid, sig) == (101, signal.SIGKILL):
                self.parents[203] = 1
                self.identities[203] = dataclasses.replace(self.child, pid=203, started="survivor-start")
                self.process_sessions[203] = 101
        with (patch.object(os, "kill", side_effect=signal_process),
              patch.object(time, "sleep", new=bounded_fixture(fixture_skip_sleep)),
              contextlib.redirect_stderr(io.StringIO()) as errors):
            self.assertFalse(codex_mesh._stop_daemon(self.target))  # pyright: ignore[reportPrivateUsage]
        self.assertIn("not stopped", errors.getvalue())
        self.assertIn("203", errors.getvalue())
        self.assertIn(203, self.parents)
        self.assertFalse(any(event[0] == 203 for event in self.signals))

    def test_excluded_session_orphan_is_never_stopped_and_the_frozen_family_is_resumed(self) -> None:
        self.parents[203] = 1
        self.process_sessions[203] = 101
        orphan = dataclasses.replace(self.child, pid=203, started="orphan-start")
        cases = (
            (orphan, account.ProcessCodexHome(self.home / ".codex1")),
            (orphan, account.ProcessEnvironmentUnread("synthetic denied")),
            (dataclasses.replace(orphan, executable=Path("/Applications/ChatGPT.app/Contents/MacOS/codex")),
             account.ProcessWithoutCodexHome()),
            (codex_mesh.UnreadCodexProcess(203, "synthetic unread identity"), account.ProcessWithoutCodexHome()),
        )
        for identity, setting in cases:
            self.identities[203] = identity
            self.signals = []
            def selected(pid: int) -> account.ProcessCodexHome | account.ProcessWithoutCodexHome | account.ProcessEnvironmentUnread:
                return setting if pid == 203 else account.ProcessWithoutCodexHome()
            with (self.subTest(identity=identity, setting=setting),
                  patch.object(account, "process_codex_home", side_effect=selected),
                  contextlib.redirect_stderr(io.StringIO()) as errors):
                self.assertFalse(codex_mesh._stop_daemon(self.target))  # pyright: ignore[reportPrivateUsage]
            self.assertIn("203", errors.getvalue())
            self.assertFalse(any(pid == 203 or sig == signal.SIGKILL for pid, sig in self.signals))
            self.assertEqual({pid for pid, sig in self.signals if sig == signal.SIGSTOP},
                             {pid for pid, sig in self.signals if sig == signal.SIGCONT})
            self.assertEqual(self.stopped, set())

    def test_a_session_with_an_untracked_leader_is_never_swept(self) -> None:
        self.process_sessions = {101: 900, 201: 900, 203: 900}
        self.parents[203] = 1
        self.identities[203] = dataclasses.replace(self.child, pid=203, started="unrelated-session-member")
        self.assertTrue(codex_mesh._stop_daemon(self.target))  # pyright: ignore[reportPrivateUsage]
        self.assertFalse(any(event[0] == 203 for event in self.signals))
        self.assertEqual(self.parents, {203: 1})

    def test_overlapping_daemon_stops_hold_one_lock_until_the_first_family_is_released(self) -> None:
        other = dataclasses.replace(self.target, pid=301, started="other-daemon-start")
        self.identities[301] = other
        self.parents[301] = 1
        self.process_sessions[301] = 301
        self.starts[301] = other.started
        overlaps: list[bool] = []
        def signal_process(pid: int, sig: int) -> None:
            self.signal_process(pid, sig)
            if (pid, sig) == (101, signal.SIGSTOP):
                overlaps.append(codex_mesh._stop_daemon(other))  # pyright: ignore[reportPrivateUsage]
                self.assertFalse(any(event[0] == 301 for event in self.signals))
        with (patch.object(os, "kill", side_effect=signal_process),
              contextlib.redirect_stderr(io.StringIO()) as errors):
            self.assertTrue(codex_mesh._stop_daemon(self.target))  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(overlaps, [False])
            self.assertIn("lock", errors.getvalue().lower())
            self.assertTrue(codex_mesh._stop_daemon(other))  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(self.parents, {})
        self.assertEqual(self.signals[-2:], [(301, signal.SIGSTOP), (301, signal.SIGKILL)])

    def test_session_sweep_never_reads_or_signals_another_users_process(self) -> None:
        commands: list[tuple[str, ...]] = []
        foreign = "302 101 codex --remote-control\n"
        def command(arguments: tuple[str, ...], **_options: object) -> subprocess.CompletedProcess[str]:
            commands.append(arguments)
            text = self.listing()
            if arguments != ("ps", "-u", "1000", "-o", "pid=,ppid=,args="):
                text += foreign
            return subprocess.CompletedProcess(arguments, 0, text, "")
        with (patch.object(os, "getuid", return_value=1000),
              patch.object(subprocess, "run", side_effect=command),
              patch.object(codex_mesh, "_list_family_processes", side_effect=self.native_listing),
              patch.object(codex_mesh, "_daemon_process_tree", side_effect=self.snapshot)):
            self.assertTrue(codex_mesh._stop_daemon(self.target))  # pyright: ignore[reportPrivateUsage]
        self.assertTrue(commands)
        self.assertTrue(all(arguments == ("ps", "-u", "1000", "-o", "pid=,ppid=,args=")
                            for arguments in commands))
        self.assertFalse(any(event[0] == 302 for event in self.signals))

    def test_snapshot_reads_full_identities_only_for_family_and_swept_session_candidates(self) -> None:
        self.parents.update({203: 1, 900: 1})
        self.identities[203] = dataclasses.replace(self.child, pid=203, started="session-orphan")
        self.identities[900] = dataclasses.replace(self.child, pid=900, started="unrelated")
        self.process_sessions.update({203: 101, 900: 900})
        with patch.object(codex_mesh, "_process_identity", side_effect=self.identity) as identity:
            snapshot = self.snapshot(101, {101: self.target, 201: self.child}, frozenset({101}))
        self.assertEqual(set(snapshot), {101, 201, 203})
        self.assertEqual({call.args[0] for call in identity.call_args_list}, {101, 201, 203})
        self.assertEqual(self.signals, [])

    def test_kept_family_resumes_and_reports_a_shared_home_member_found_already_stopped(self) -> None:
        self.stopped = {201}
        def selected(pid: int) -> account.ProcessCodexHome | account.ProcessWithoutCodexHome:
            return account.ProcessCodexHome(self.home / ".codex1") if pid == 101 else account.ProcessWithoutCodexHome()
        with (patch.object(account, "process_codex_home", side_effect=selected),
              contextlib.redirect_stdout(io.StringIO()) as output,
              contextlib.redirect_stderr(io.StringIO()) as errors):
            self.assertFalse(codex_mesh._stop_daemon(self.target))  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(self.signals, [(201, signal.SIGCONT)])
        self.assertEqual(self.stopped, set())
        diagnostic = output.getvalue() + errors.getvalue()
        self.assertIn("201", diagnostic)
        self.assertIn("stopped", diagnostic)

    def test_excluded_member_found_after_freeze_resumes_every_stopped_member_without_killing(self) -> None:
        for setting in (account.ProcessCodexHome(self.home / ".codex1"), account.ProcessEnvironmentUnread("denied")):
            def selected(pid: int) -> account.ProcessCodexHome | account.ProcessWithoutCodexHome | account.ProcessEnvironmentUnread:
                return setting if pid == 201 and self.stopped == {101, 201} else account.ProcessWithoutCodexHome()
            self.signals = []
            with self.subTest(setting=setting), patch.object(account, "process_codex_home", side_effect=selected):
                self.assertFalse(codex_mesh._stop_daemon(self.target))  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(self.signals[:2], [(101, signal.SIGSTOP), (201, signal.SIGSTOP)])
            self.assertCountEqual(self.signals[2:], [(101, signal.SIGCONT), (201, signal.SIGCONT)])
            self.assertEqual(self.stopped, set())
            self.assertEqual(set(self.parents), {101, 201})

    def test_identity_unread_after_stop_still_resumes_the_member_by_pid_and_start(self) -> None:
        def signal_process(pid: int, sig: int) -> None:
            self.signal_process(pid, sig)
            if (pid, sig) == (201, signal.SIGSTOP):
                self.identities[201] = codex_mesh.UnreadCodexProcess(201, "argv and executable unavailable")
        with (patch.object(os, "kill", side_effect=signal_process),
              contextlib.redirect_stderr(io.StringIO()) as errors):
            self.assertFalse(codex_mesh._stop_daemon(self.target))  # pyright: ignore[reportPrivateUsage]
        self.assertIn("pid 201", errors.getvalue())
        self.assertEqual(self.signals, [(101, signal.SIGSTOP), (201, signal.SIGSTOP),
                                        (201, signal.SIGCONT), (101, signal.SIGCONT)])
        self.assertEqual(self.stopped, set())
        self.assertIsInstance(self.identities[201], codex_mesh.UnreadCodexProcess)
        self.assertEqual(set(self.parents), {101, 201})

    def test_unconfirmed_start_after_stop_keeps_the_member_stopped_and_names_it(self) -> None:
        for start in ("", "replacement-start", PermissionError("synthetic start denied")):
            self.identities[201] = self.child
            self.stopped = set()
            self.signals = []
            def signal_process(pid: int, sig: int) -> None:
                self.signal_process(pid, sig)
                if (pid, sig) == (201, signal.SIGSTOP):
                    self.identities[201] = codex_mesh.UnreadCodexProcess(201, "argv and executable unavailable")
            def process_start(pid: int) -> str:
                if pid == 201:
                    if isinstance(start, OSError):
                        raise start
                    return start
                return self.starts[pid]
            with (self.subTest(start=start), patch.object(os, "kill", side_effect=signal_process),
                  patch.object(sessions, "process_start", side_effect=process_start),
                  contextlib.redirect_stderr(io.StringIO()) as errors):
                self.assertFalse(codex_mesh._stop_daemon(self.target))  # pyright: ignore[reportPrivateUsage]
            self.assertIn("pid 201", errors.getvalue())
            self.assertIn("may still be stopped", errors.getvalue())
            self.assertEqual(self.signals, [(101, signal.SIGSTOP), (201, signal.SIGSTOP),
                                            (101, signal.SIGCONT)])
            self.assertEqual(self.stopped, {201})
            self.assertEqual(set(self.parents), {101, 201})

    def test_exclusion_after_freezing_keeps_a_pre_stopped_member_on_another_home_stopped(self) -> None:
        self.stopped = {201}
        excluded: list[int] = []
        snapshots = 0
        def tree(*_arguments: object) -> dict[int, codex_mesh.FamilyProcess]:
            nonlocal snapshots
            snapshots += 1
            return self.tree()
        def selected(pid: int) -> account.ProcessCodexHome | account.ProcessWithoutCodexHome:
            if pid == 201 and snapshots >= 2:
                if not excluded:
                    self.assertEqual(self.stopped, {101, 201})
                excluded.append(pid)
                return account.ProcessCodexHome(self.home / ".codex1")
            return account.ProcessWithoutCodexHome()
        with (patch.object(account, "process_codex_home", side_effect=selected),
              patch.object(codex_mesh, "_daemon_process_tree", side_effect=tree)):
            self.assertFalse(codex_mesh._stop_daemon(self.target))  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(set(excluded), {201})
        self.assertEqual(self.signals, [(101, signal.SIGSTOP), (101, signal.SIGCONT)])
        self.assertEqual(self.stopped, {201})
        self.assertEqual(set(self.parents), {101, 201})

    def test_failed_child_stop_resumes_the_parent_without_killing_any_member(self) -> None:
        def signal_process(pid: int, sig: int) -> None:
            if (pid, sig) == (201, signal.SIGSTOP):
                raise PermissionError("synthetic stop denied")
            self.signal_process(pid, sig)
        with patch.object(os, "kill", side_effect=signal_process):
            self.assertFalse(codex_mesh._stop_daemon(self.target))  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(self.signals, [(101, signal.SIGSTOP), (101, signal.SIGCONT)])
        self.assertEqual(self.stopped, set())

    def test_failed_frozen_child_kill_resumes_every_surviving_member(self) -> None:
        def signal_process(pid: int, sig: int) -> None:
            if sig == signal.SIGKILL:
                raise PermissionError("synthetic kill denied")
            self.signal_process(pid, sig)
        with patch.object(os, "kill", side_effect=signal_process):
            self.assertFalse(codex_mesh._stop_daemon(self.target))  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(self.signals[:2], [(101, signal.SIGSTOP), (201, signal.SIGSTOP)])
        self.assertCountEqual(self.signals[2:], [(101, signal.SIGCONT), (201, signal.SIGCONT)])
        self.assertEqual(self.stopped, set())
        self.assertEqual(set(self.parents), {101, 201})

    def test_unrelated_snapshot_processes_do_not_prevent_verified_family_shutdown(self) -> None:
        snapshot_pid = 900
        def tree(*_arguments: object) -> dict[int, codex_mesh.FamilyProcess]:
            nonlocal snapshot_pid
            snapshot_pid += 1
            unrelated = codex_mesh.ProcessIdentity(snapshot_pid, f"ps-{snapshot_pid}", ("ps", "-eo", "pid,ppid"), Path("/bin/ps"))
            return {**self.tree(), snapshot_pid: codex_mesh.FamilyProcess(1, unrelated, codex_mesh.ProcessSession(snapshot_pid))}
        with patch.object(codex_mesh, "_daemon_process_tree", side_effect=tree):
            self.assertTrue(codex_mesh._stop_daemon(self.target))  # pyright: ignore[reportPrivateUsage]
        self.assertTrue(all(event[0] in (101, 201) for event in self.signals))
        self.assertEqual(self.parents, {})

    def test_bundled_descendant_with_plain_argument_zero_is_never_signalled(self) -> None:
        self.identities[201] = dataclasses.replace(self.child, executable=Path("/Applications/ChatGPT.app/Contents/MacOS/codex"))
        self.assertFalse(codex_mesh._stop_daemon(self.target))  # pyright: ignore[reportPrivateUsage]
        self.assertFalse(any(event[0] == 201 for event in self.signals))
        self.assertFalse(any(event[1] == signal.SIGKILL for event in self.signals))
        self.assertEqual(self.stopped, set())

    def test_changed_child_identity_is_never_signalled_and_parent_is_resumed(self) -> None:
        snapshot = self.tree()
        self.identities[201] = dataclasses.replace(self.child, started="reused-start")
        with patch.object(codex_mesh, "_daemon_process_tree", return_value=snapshot):
            self.assertFalse(codex_mesh._stop_daemon(self.target))  # pyright: ignore[reportPrivateUsage]
        self.assertFalse(any(event[0] == 201 for event in self.signals))
        self.assertEqual(self.stopped, set())

    def test_pid_reused_between_ancestry_listing_and_identity_read_is_not_a_trusted_child(self) -> None:
        before = {
            101: process_table.Process(101, 1, "codex", "codex app-server daemon"),
            201: process_table.Process(201, 101, "codex", "codex --remote-control"),
        }
        after = {**before, 201: process_table.Process(201, 1, "codex", "codex exec unrelated")}
        self.identities[201] = dataclasses.replace(self.child, started="replacement-start", arguments=("codex", "exec", "unrelated"))
        listings = ["".join(f"{item.pid} {item.parent} {item.arguments}\n"
                            for item in table.values()) for table in (before, after)]
        with patch.object(codex_mesh, "_list_family_processes", side_effect=listings):
            snapshot = self.snapshot(101, {201: self.child}, frozenset())
        self.assertIsInstance(snapshot[201].identity, codex_mesh.UnreadCodexProcess)
        self.assertEqual(self.signals, [])

    def test_changed_parent_or_returned_login_never_kills_the_family(self) -> None:
        self.identities[101] = dataclasses.replace(self.target, started="new-start")
        self.assertFalse(codex_mesh._stop_daemon(self.target))  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(self.signals, [])
        self.identities[101] = self.target
        _ = (self.home / ".codex/auth.json").write_text("synthetic presence only")
        self.assertFalse(codex_mesh._stop_daemon(self.target))  # pyright: ignore[reportPrivateUsage]
        self.assertFalse(any(event[1] == signal.SIGKILL for event in self.signals))
        self.assertEqual(self.stopped, set())



class SweepTests(unittest.TestCase):
    """The report of app-servers nothing is using. Every doubt must read as in use."""

    @override
    def setUp(self) -> None:
        _ = self.enterContext(patch.object(sys, "platform", "linux"))
        roles_state = self.enterContext(tempfile.TemporaryDirectory())
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"DESIGN_ROLES_STATE": roles_state})))

    @staticmethod
    def write_sweep_server(folder: Path, quiet_secs: float, *, held: bool = False) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        worker: dict[str, object] = {"thread_id": "design-thread", "status": "done"}
        if held:
            worker["held_design_id"] = folder.parent.name
        _ = (folder / codex_mesh.SERVER_FILE).write_text(json.dumps({"pid": 9, "port": 4009}))
        _ = (folder / codex_mesh.ROSTER_FILE).write_text(json.dumps({"designer": worker}))
        changed = time.time() - quiet_secs
        for name in (codex_mesh.SERVER_FILE, codex_mesh.ROSTER_FILE):
            os.utime(folder / name, (changed, changed))

    def sweep_recorded_server(
        self, root: Path, folder: Path, *, age_secs: float,
        clients: int = 0, busy: tuple[str, ...] = (), mesh_log: bool = True,
    ) -> tuple[str, list[int]]:
        """Exercise both sweep judgments and record cleanup, without signalling a real process."""
        reaped: list[int] = []

        def reap(pid: int) -> bool:
            reaped.append(pid)
            return True

        stderr = codex_mesh.MeshServerLog(folder) if mesh_log else codex_mesh.NonMeshServerStderr()
        with (
            patch.object(codex_mesh, "_running_servers", return_value=[(9, 4009, age_secs)]),
            patch.object(codex_mesh, "_client_counts", return_value={4009: clients}),
            patch.object(codex_mesh, "_server_folder", return_value=stderr),
            patch.object(codex_mesh, "_busy_threads", return_value=busy) as conversations,
            patch.object(codex_mesh, "_reap", side_effect=reap),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(codex_mesh.command_sweep(argparse.Namespace(root=str(root), stop=True)), 0)
        if busy and not clients:
            conversations.assert_called_with(4009, {"design-thread": "designer"})
        label = output.getvalue().splitlines()[0].split("\t")[2]
        return label, reaped

    def test_held_design_server_waits_for_the_quiet_window_before_sweep_stops_it(self) -> None:
        now = time.time()
        for mesh_log in (True, False):
            for quiet in (codex_mesh.SERVER_IDLE_SECS - 1, codex_mesh.SERVER_IDLE_SECS):
                with self.subTest(mesh_log=mesh_log, quiet=quiet), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    mesh = design_roles.state_root() / "held-design" / "mesh"
                    with patch.object(time, "time", new=bounded_fixture(lambda: now)):
                        self.write_sweep_server(mesh, quiet, held=True)
                        label, reaped = self.sweep_recorded_server(
                            root / "ordinary-runs", mesh, age_secs=codex_mesh.SERVER_IDLE_SECS + 60,
                            mesh_log=mesh_log,
                        )
                    expired = quiet >= codex_mesh.SERVER_IDLE_SECS
                    self.assertEqual(label, "stopped" if expired else "in use")
                    self.assertEqual(reaped, [9] if expired else [])
                    self.assertEqual((mesh / codex_mesh.SERVER_FILE).exists(), not expired)

    def test_held_design_server_keeps_its_startup_quiet_window_with_old_activity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mesh = design_roles.state_root() / "held-design" / "mesh"
            self.write_sweep_server(mesh, codex_mesh.SERVER_IDLE_SECS + 60, held=True)
            self.assertEqual(self.sweep_recorded_server(
                root / "ordinary-runs", mesh, age_secs=codex_mesh.SERVER_IDLE_SECS - 1,
            ), ("in use", []))
            self.assertTrue((mesh / codex_mesh.SERVER_FILE).exists())

    def test_held_design_server_with_a_live_turn_or_client_survives_sweep(self) -> None:
        for clients, busy in ((1, ()), (0, ("designer: ThreadLive",))):
            with self.subTest(clients=clients, busy=busy), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                mesh = design_roles.state_root() / "held-design" / "mesh"
                old = codex_mesh.SERVER_IDLE_SECS + 60
                self.write_sweep_server(mesh, old, held=True)
                self.assertEqual(self.sweep_recorded_server(
                    root / "ordinary-runs", mesh, age_secs=old, clients=clients, busy=busy,
                ), ("in use", []))
                self.assertTrue((mesh / codex_mesh.SERVER_FILE).exists())

    def test_design_mesh_without_a_held_owner_keeps_unowned_sweep_behavior(self) -> None:
        for folder_exists in (True, False):
            with self.subTest(folder_exists=folder_exists), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                design = "released-design" if folder_exists else "missing-design"
                mesh = design_roles.state_root() / design / "mesh"
                if folder_exists:
                    self.write_sweep_server(mesh, 1)
                self.assertEqual(self.sweep_recorded_server(
                    root / "ordinary-runs", mesh, age_secs=codex_mesh.SERVER_IDLE_SECS + 60,
                ), ("stopped", [9]))
                self.assertFalse((mesh / codex_mesh.SERVER_FILE).exists())
                self.assertEqual(mesh.exists(), folder_exists)

    def test_run_server_keeps_its_quiet_window_with_durable_design_meshes_present(self) -> None:
        for quiet in (60, codex_mesh.SERVER_IDLE_SECS + 60):
            with self.subTest(quiet=quiet), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                design_mesh = design_roles.state_root() / "held-design" / "mesh"
                self.write_sweep_server(design_mesh, 1, held=True)
                _ = (design_mesh / codex_mesh.SERVER_FILE).write_text(json.dumps({"pid": 10, "port": 4010}))
                runs = root / "ordinary-runs"
                run = runs / "run-a"
                self.write_sweep_server(run, quiet)
                result = self.sweep_recorded_server(runs, run, age_secs=codex_mesh.SERVER_IDLE_SECS + 60)
                expired = quiet >= codex_mesh.SERVER_IDLE_SECS
                self.assertEqual(result, ("stopped", [9]) if expired else ("in use", []))
                self.assertEqual((run / codex_mesh.SERVER_FILE).exists(), not expired)
                self.assertTrue((design_mesh / codex_mesh.SERVER_FILE).exists())

    @staticmethod
    def facts(**changes: object) -> codex_mesh.ServerFacts:
        quiet = codex_mesh.SERVER_IDLE_SECS + 60
        base = codex_mesh.ServerFacts(
            pid=10,
            port=4000,
            age_secs=quiet,
            clients=0,
            ownership=codex_mesh.CurrentRunServer(Path("/run")),
            folder_exists=True,
            run_lifecycle=codex_mesh.UncheckedRunLifecycle(),
            live_launchers=(),
            quiet_secs=quiet,
        )
        return dataclasses.replace(base, **changes)

    @staticmethod
    def verdict(facts: codex_mesh.ServerFacts, busy: tuple[str, ...] = ()) -> codex_mesh.SweepVerdict:
        return codex_mesh._sweep_verdict(facts, lambda: busy)  # pyright: ignore[reportPrivateUsage]

    def test_only_a_server_cleared_by_every_check_is_unused(self) -> None:
        self.assertIsInstance(self.verdict(self.facts()), codex_mesh.ServerUnused)
        self.assertIsInstance(self.verdict(self.facts(quiet_secs=None)), codex_mesh.ServerUnused)
        deleted = self.verdict(self.facts(folder_exists=False, quiet_secs=None))
        self.assertIsInstance(deleted, codex_mesh.ServerUnused)
        self.assertIn("deleted", deleted.reason)
        for change in ({"clients": 1}, {"live_launchers": ("impl",)}, {"age_secs": 60.0},
                       {"quiet_secs": 60.0}):
            self.assertIsInstance(self.verdict(self.facts(**change)), codex_mesh.ServerInUse, change)
        self.assertIsInstance(self.verdict(self.facts(), ("impl: ThreadLive",)), codex_mesh.ServerInUse)
        self.assertIsInstance(self.verdict(self.facts(folder_exists=False), ("t1: ThreadLive",)),
                              codex_mesh.ServerInUse)

    def test_a_retired_server_is_judged_only_on_its_clients_and_conversations(self) -> None:
        # Its run goes on using a newer server, so the run's marker, launchers and activity say
        # nothing about this one.
        retired = self.facts(
            ownership=codex_mesh.RetiredRunServer(Path("/run")),
            run_lifecycle=codex_mesh.CheckedRunLifecycle(True, ("impl",)),
            live_launchers=("impl",),
            age_secs=60.0,
            quiet_secs=60.0,
        )
        unused = self.verdict(retired)
        self.assertIsInstance(unused, codex_mesh.ServerUnused)
        self.assertIn("retired", unused.reason)
        self.assertIsInstance(self.verdict(dataclasses.replace(retired, clients=1)), codex_mesh.ServerInUse)
        self.assertIsInstance(self.verdict(retired, ("impl: ThreadLive",)), codex_mesh.ServerInUse)

    def test_an_old_unowned_server_is_unused_only_when_its_conversations_are_idle(self) -> None:
        unowned = self.facts(ownership=codex_mesh.UnownedServerWithoutLog())
        self.assertIsInstance(self.verdict(unowned), codex_mesh.ServerUnused)
        self.assertIsInstance(
            self.verdict(unowned, ("thread-1: ThreadLive",)), codex_mesh.ServerInUse
        )
        self.assertIsInstance(
            self.verdict(dataclasses.replace(unowned, age_secs=10.0)), codex_mesh.ServerInUse
        )

    def test_dev_shell_startup_budget_does_not_extend_the_unowned_sweep_grace(self) -> None:
        unowned = self.facts(ownership=codex_mesh.UnownedServerWithoutLog(), age_secs=45.0)
        self.assertIsInstance(self.verdict(unowned), codex_mesh.ServerUnused)
        self.assertIsInstance(self.verdict(unowned, ("thread-1: ThreadLive",)), codex_mesh.ServerInUse)

    def test_current_server_without_a_marker_must_clear_every_live_and_idle_signal(self) -> None:
        ended = self.facts(run_lifecycle=codex_mesh.CheckedRunLifecycle(False, ()))
        self.assertIsInstance(self.verdict(ended), codex_mesh.ServerUnused)
        for change in (
            {"clients": 1},
            {"live_launchers": ("friend",)},
            {"age_secs": 60.0},
            {"quiet_secs": 60.0},
        ):
            self.assertIsInstance(
                self.verdict(dataclasses.replace(ended, **change)),
                codex_mesh.ServerInUse,
                change,
            )
        self.assertIsInstance(
            self.verdict(ended, ("impl: ThreadLive",)), codex_mesh.ServerInUse
        )
        marked = self.facts(run_lifecycle=codex_mesh.CheckedRunLifecycle(True, ()))
        self.assertIsInstance(self.verdict(marked), codex_mesh.ServerInUse)
        running = self.facts(run_lifecycle=codex_mesh.CheckedRunLifecycle(False, ("impl",)))
        self.assertIsInstance(self.verdict(running), codex_mesh.ServerInUse)

    def test_server_discovery_accepts_the_actual_zero_unload_delay_launch_arguments(self) -> None:
        commands = (
            ["codex", "app-server", "--listen", "ws://127.0.0.1:47165"],
            ["codex", "-c", "thread_unload_delay_secs=0", "app-server", "--listen", "ws://127.0.0.1:47165"],
            ["codex", "-c", "thread_unload_delay_secs=0", "-c", "model_auto_compact_token_limit=200000",
             "app-server", "--listen", "ws://127.0.0.1:47165"],
            ["codex", "-c", "thread_unload_delay_secs=0", "-c", "model_auto_compact_token_limit=200000",
             "-c", "background_terminal_max_timeout=300000", "app-server", "--listen", "ws://127.0.0.1:47165"],
        )
        for command in commands:
            with self.subTest(command=command), patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "111 7200 " + " ".join(command) + "\n", "")):
                self.assertEqual(codex_mesh._running_servers(), [(111, 47165, 7200.0)])  # pyright: ignore[reportPrivateUsage]

    def test_servers_and_connections_are_read_from_ps_and_ss(self) -> None:
        ps = ("  111 7200 codex app-server --listen ws://127.0.0.1:47165\n"
              "  222   30 /nix/store/x/bin/codex app-server --listen ws://0.0.0.0:51009\n"
              "  333   99 zsh -c pgrep -af codex app-server --listen ws://127.0.0.1: | head\n"
              "  444   99 codex app-server --listen unix:///tmp/codex.sock\n"
              "  555   99 codex app-server daemon --listen ws://127.0.0.1:55555\n")
        ss = "0 0 127.0.0.1:47165 127.0.0.1:60000\n0 0 127.0.0.1:60000 127.0.0.1:47165\n0 0 [::1]:47165 [::1]:60001\n"
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, ps, "")):
            servers = codex_mesh._running_servers()  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(servers, [(111, 47165, 7200.0), (222, 51009, 30.0)])
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, ss, "")):
            counts = codex_mesh._client_counts()  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(counts, {47165: 2, 60000: 1})

    def test_process_text_that_only_mentions_the_server_command_is_not_a_target(self) -> None:
        ps = (
            "  666 99 python3 worker.py codex app-server --listen ws://127.0.0.1:4444\n"
        )
        with patch.object(
            subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, ps, ""),
        ):
            servers = codex_mesh._running_servers()  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(servers, [])

    def test_unix_daemon_and_app_server_helper_are_never_sweep_targets(self) -> None:
        ps = (
            "  444 99 codex app-server --listen unix:///tmp/codex.sock\n"
            "  555 99 codex app-server daemon --listen ws://127.0.0.1:55555\n"
        )
        with patch.object(
            subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, ps, ""),
        ):
            servers = codex_mesh._running_servers()  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(servers, [])

    def test_the_run_folder_comes_from_the_servers_own_log_handle(self) -> None:
        folder = codex_mesh._server_folder  # pyright: ignore[reportPrivateUsage]
        with patch.object(os, "readlink", return_value="/tmp/x/run-a/mesh_server.log"):
            self.assertEqual(folder(5), codex_mesh.MeshServerLog(Path("/tmp/x/run-a")))
        with patch.object(os, "readlink", return_value="/tmp/x/gone/mesh_server.log (deleted)"):
            self.assertEqual(folder(5), codex_mesh.MeshServerLog(Path("/tmp/x/gone")))
        with patch.object(os, "readlink", return_value="/dev/null"):
            self.assertIsInstance(folder(5), codex_mesh.NonMeshServerStderr)
        with patch.object(os, "readlink", side_effect=OSError("no such process")):
            self.assertIsInstance(folder(5), codex_mesh.UnreadServerStderr)

    def test_judgment_preserves_the_explicit_unowned_without_log_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(codex_mesh, "_server_folder", return_value=codex_mesh.NonMeshServerStderr()), \
                patch.object(codex_mesh, "_busy_threads", return_value=()):
            judgment = codex_mesh._judge(  # pyright: ignore[reportPrivateUsage]
                (Path(directory),), 5, 4005, codex_mesh.SERVER_IDLE_SECS + 60, 0
            )

        self.assertIsInstance(judgment.verdict, codex_mesh.ServerUnused)
        self.assertEqual(judgment.ownership, codex_mesh.UnownedServerWithoutLog())

    def test_a_run_folder_reports_its_marker_its_quiet_time_and_its_live_launchers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run-a"
            (root / "active").mkdir()
            run.mkdir()
            self.assertFalse(codex_mesh._marked_active((root,), run))  # pyright: ignore[reportPrivateUsage]
            self.assertIsNone(codex_mesh._quiet_secs(run, 0.0))  # pyright: ignore[reportPrivateUsage]
            _ = (root / "active" / "session-1").write_text(f"{run}\n")
            self.assertTrue(codex_mesh._marked_active((root,), run))  # pyright: ignore[reportPrivateUsage]
            _ = (run / "board.log").write_text("x")
            quiet = codex_mesh._quiet_secs(run, (run / "board.log").stat().st_mtime + 5)  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(quiet, 5.0)
            _ = (run / codex_mesh.ROSTER_FILE).write_text(json.dumps({
                "impl": {"thread_id": "t1", "launcher_pid": os.getpid()},
                "tests": {"thread_id": "t2", "launcher_pid": 0},
                "queued": {},
            }))
            self.assertEqual(codex_mesh._live_launchers(run), ("impl",))  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(codex_mesh._roster_threads(run), {"t1": "impl", "t2": "tests"})  # pyright: ignore[reportPrivateUsage]

    def busy(self, loaded: dict[str, object], statuses: dict[str, str]) -> tuple[str, ...]:
        """What `_busy_threads` says of a server answering with `loaded` and these thread statuses."""

        @final
        class FakeClient:
            def __init__(self, _port: int, _name: str) -> None:
                pass

            @bounded_fixture
            def call(self, method: str, params: dict[str, object], **_options: object) -> dict[str, object]:
                if method == "thread/loaded/list":
                    return loaded
                if method == "thread/queue/list":
                    return {"result": {"data": [], "nextCursor": None}}
                if method == "thread/turns/list":
                    return {"result": {"data": [{"id": "u", "status": "inProgress"}]}}
                status = statuses[cast("str", params["threadId"])]
                return {"result": {"thread": {"status": {"type": status}, "turns": []}}}

            def close(self) -> None:
                pass

        with patch.object(codex_mesh, "Client", FakeClient):
            return codex_mesh._busy_threads(4000, {"t1": "impl"})  # pyright: ignore[reportPrivateUsage]

    def test_the_server_is_asked_for_every_conversation_it_holds(self) -> None:
        listed: dict[str, object] = {"result": {"data": ["t9"], "nextCursor": None}}
        self.assertEqual(self.busy(listed, {"t1": "idle", "t9": "notLoaded"}), ())
        # A conversation no roster names is still seen mid-turn.
        self.assertEqual(self.busy(listed, {"t1": "idle", "t9": "active"}), ("t9: ThreadLive",))
        self.assertEqual(self.busy(listed, {"t1": "active", "t9": "idle"}), ("impl: ThreadLive",))
        self.assertEqual(self.busy(listed, {"t1": "odd", "t9": "idle"}), ("impl: ThreadStateUnknown",))

    def test_a_server_that_cannot_be_asked_in_full_reads_as_busy(self) -> None:
        replies: list[dict[str, object]] = [
            {"error": {"message": "no"}}, {"result": {}}, {"result": {"data": [], "nextCursor": "more"}},
        ]
        for reply in replies:
            self.assertEqual(len(self.busy(reply, {})), 1, reply)
        with patch.object(codex_mesh, "Client", side_effect=ConnectionError("refused")):
            busy = codex_mesh._busy_threads(4000, {})  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(busy, ("the server could not be asked (refused)",))

    def test_the_sweep_fails_closed_when_it_cannot_look(self) -> None:
        with patch.object(codex_mesh, "_running_servers", side_effect=OSError("no ps")), \
                patch.object(codex_mesh, "_stop_unused") as stop, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(codex_mesh.command_sweep(argparse.Namespace(root="", stop=True)), 1)
        self.assertIn("nothing judged", errors.getvalue())
        stop.assert_not_called()

    def sweep(self, stop: bool, stops: bool = True) -> tuple[list[list[str]], list[int]]:
        """The rows `sweep` prints for a busy, an unused and an unplaced server, and the pids it stopped."""
        old = codex_mesh.SERVER_IDLE_SECS + 60
        asked: list[int] = []

        def stopper(
            _root: Path, pid: int, _port: int, _ownership: codex_mesh.ServerOwnership
        ) -> bool:
            asked.append(pid)
            return stops

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(codex_mesh, "_running_servers", return_value=[(1, 4001, old), (2, 4002, old), (3, 4003, old)]), \
                patch.object(codex_mesh, "_client_counts", return_value={4001: 1}), \
                patch.object(codex_mesh, "_server_folder",
                             side_effect=[codex_mesh.MeshServerLog(Path(directory) / "gone"),
                                          codex_mesh.MeshServerLog(Path(directory) / "gone"),
                                          codex_mesh.NonMeshServerStderr()]), \
                patch.object(codex_mesh, "_busy_threads", return_value=()) as busy, \
                patch.object(codex_mesh, "_stop_unused", side_effect=stopper), \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(codex_mesh.command_sweep(argparse.Namespace(root=directory, stop=stop)), 0)
        # Both recordless servers are old enough to be judged on their conversations.
        self.assertEqual(
            [entry.args for entry in busy.call_args_list],
            [(4002, {}), (4003, {})],
        )
        return [line.split("\t") for line in printed.getvalue().splitlines()], asked

    def test_the_report_lists_each_server_and_stops_nothing(self) -> None:
        rows, asked = self.sweep(stop=False)
        self.assertEqual([row[2] for row in rows[:3]], ["in use", "unused", "unused"])
        self.assertEqual(rows[3], ["3 app-server(s), 2 unused; report only, nothing was stopped"])
        self.assertEqual(asked, [])

    def test_stop_reaches_each_old_unowned_server(self) -> None:
        rows, asked = self.sweep(stop=True)
        self.assertEqual([row[2] for row in rows[:3]], ["in use", "stopped", "stopped"])
        self.assertEqual(rows[3], ["3 app-server(s), 2 unused; 2 stopped"])
        self.assertEqual(asked, [2, 3])
        rows, _asked = self.sweep(stop=True, stops=False)
        self.assertEqual([row[2] for row in rows[:3]], ["in use", "kept", "kept"])
        self.assertEqual(rows[3], ["3 app-server(s), 2 unused; 0 stopped"])

    def test_unowned_server_is_reaped_only_after_the_second_look(self) -> None:
        old = codex_mesh.SERVER_IDLE_SECS + 60
        reaped: list[int] = []

        def reap(pid: int) -> bool:
            reaped.append(pid)
            return True

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(codex_mesh, "_running_servers", return_value=[(8, 4008, old)]), \
                patch.object(codex_mesh, "_client_counts", return_value={}), \
                patch.object(codex_mesh, "_server_folder", return_value=codex_mesh.NonMeshServerStderr()), \
                patch.object(codex_mesh, "_busy_threads", return_value=()), \
                patch.object(codex_mesh, "_reap", side_effect=reap), \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            code = codex_mesh.command_sweep(argparse.Namespace(root=directory, stop=True))
        self.assertEqual(code, 0)
        self.assertEqual(reaped, [8])
        self.assertIn("8\t4008\tstopped\tno current or retired server record", printed.getvalue())

    def test_sweep_preserves_paused_threads_but_plain_stop_ends_them(self) -> None:
        old = codex_mesh.SERVER_IDLE_SECS + 60
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "paused-run"
            run.mkdir()
            server_path = run / codex_mesh.SERVER_FILE
            _ = server_path.write_text(json.dumps({"pid": 9, "port": 4009}))
            roster_path = run / codex_mesh.ROSTER_FILE
            roster = {
                "impl": {"thread_id": "paused-thread", "status": "paused", "role": "impl",
                         "paused_from": "running", "paused_turn": True},
                "review": {"thread_id": "done-thread", "status": "done", "role": "review"},
            }
            _ = roster_path.write_text(json.dumps(roster))
            stale = time.time() - old
            for activity in (server_path, roster_path):
                os.utime(activity, (stale, stale))
            with (
                patch.object(codex_mesh, "_running_servers", return_value=[(9, 4009, old)]),
                patch.object(codex_mesh, "_client_counts", return_value={}),
                patch.object(codex_mesh, "_server_folder", return_value=codex_mesh.MeshServerLog(run)),
                patch.object(codex_mesh, "_busy_threads", return_value=()),
                patch.object(codex_mesh, "_reap", return_value=True) as reap,
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                code = codex_mesh.main(["sweep", "--root", directory, "--stop"])
            self.assertEqual(code, 0)
            self.assertIn("1 stopped", output.getvalue())
            reap.assert_called_once_with(9)
            self.assertFalse(server_path.exists())
            remaining = cast(dict[str, dict[str, object]], json.loads(roster_path.read_text()))
            self.assertEqual(remaining, roster)
            self.assertFalse(codex_mesh._end_marker_path(str(run), "impl").exists())  # pyright: ignore[reportPrivateUsage]
            with contextlib.redirect_stdout(io.StringIO()):
                code = codex_mesh.main(["stop", "--session-dir", str(run)])
            self.assertEqual(code, 0)
            ended = cast(dict[str, dict[str, object]], json.loads(roster_path.read_text()))
            self.assertEqual({seat: entry["status"] for seat, entry in ended.items()},
                             {"impl": "ended", "review": "ended"})
            self.assertEqual(ended["impl"]["thread_id"], "paused-thread")

    def test_recorded_server_stops_when_ended_and_stays_for_each_live_signal(self) -> None:
        old = codex_mesh.SERVER_IDLE_SECS + 60
        for marker, running, expected in (
            (False, False, "stopped"),
            (True, False, "in use"),
            (False, True, "in use"),
        ):
            with self.subTest(marker=marker, running=running), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                run = root / "run-a"
                run.mkdir()
                _ = (run / codex_mesh.SERVER_FILE).write_text(
                    json.dumps({"pid": 9, "port": 4009}), encoding="utf-8"
                )
                _ = (run / codex_mesh.ROSTER_FILE).write_text(json.dumps({
                    "impl": {"thread_id": "thread-1", "status": "running" if running else "done"}
                }), encoding="utf-8")
                stale = time.time() - old
                for activity in (codex_mesh.SERVER_FILE, codex_mesh.ROSTER_FILE):
                    os.utime(run / activity, (stale, stale))
                if marker:
                    (root / "active").mkdir()
                    _ = (root / "active" / "session-1").write_text(f"{run}\n", encoding="utf-8")
                reaped: list[int] = []

                def reap(pid: int) -> bool:
                    reaped.append(pid)
                    return True

                with patch.object(codex_mesh, "_running_servers", return_value=[(9, 4009, old)]), \
                        patch.object(codex_mesh, "_client_counts", return_value={}), \
                        patch.object(codex_mesh, "_server_folder", return_value=codex_mesh.MeshServerLog(run)), \
                        patch.object(codex_mesh, "_busy_threads", return_value=()), \
                        patch.object(codex_mesh, "_reap", side_effect=reap), \
                        contextlib.redirect_stdout(io.StringIO()) as printed:
                    code = codex_mesh.command_sweep(argparse.Namespace(root=directory, stop=True))
                self.assertEqual(code, 0)
                self.assertEqual(printed.getvalue().splitlines()[0].split("\t")[2], expected)
                self.assertEqual(reaped, [9] if expected == "stopped" else [])

    def second_look(
        self,
        folder: Path,
        running: list[tuple[int, int, float]],
        clients: dict[int, int],
        verdict: codex_mesh.SweepVerdict,
        ownership: codex_mesh.ServerOwnership,
    ) -> tuple[bool, list[int]]:
        """Whether `_stop_unused` stopped pid 2 on port 4002, and the pids it signalled."""
        reaped: list[int] = []

        def reap(pid: int) -> bool:
            reaped.append(pid)
            return True

        with patch.object(codex_mesh, "_running_servers", return_value=running), \
                patch.object(codex_mesh, "_client_counts", return_value=clients), \
                patch.object(
                    codex_mesh,
                    "_judge",
                    return_value=codex_mesh.SweepJudgment(verdict, ownership),
                ) as judge, \
                patch.object(codex_mesh, "_reap", side_effect=reap):
            stopped = codex_mesh._stop_unused(  # pyright: ignore[reportPrivateUsage]
                (folder.parent,), 2, 4002, ownership
            )
        if judge.called:
            self.assertEqual(judge.call_args.args[1:], (2, 4002, 9000.0, clients.get(4002, 0)))
        return stopped, reaped

    def test_a_server_is_stopped_only_when_the_second_look_agrees(self) -> None:
        unused, in_use = codex_mesh.ServerUnused("idle"), codex_mesh.ServerInUse("1 client connection(s)")
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run-a"
            run.mkdir()
            record = run / codex_mesh.SERVER_FILE
            _ = record.write_text(json.dumps({"pid": 2, "port": 4002}))
            current = codex_mesh.CurrentRunServer(run)
            # The pid stopped running this server, or now runs it on another port: nothing is signalled.
            self.assertEqual(self.second_look(run, [], {}, unused, current), (False, []))
            self.assertEqual(self.second_look(run, [(2, 4999, 9000.0)], {}, unused, current), (False, []))
            # A client arrived between the two looks.
            self.assertEqual(
                self.second_look(run, [(2, 4002, 9000.0)], {4002: 1}, in_use, current),
                (False, []),
            )
            self.assertTrue(record.exists())
            self.assertEqual(
                self.second_look(run, [(2, 4002, 9000.0)], {}, unused, current),
                (True, [2]),
            )
            self.assertFalse(record.exists())
            # A record that names a newer server is left for that server.
            _ = record.write_text(json.dumps({"pid": 7, "port": 4007}))
            self.assertEqual(
                self.second_look(run, [(2, 4002, 9000.0)], {}, unused, current),
                (True, [2]),
            )
            self.assertTrue(record.exists())
            # A deleted run folder is not created again.
            gone = Path(directory) / "gone"
            gone_owner = codex_mesh.UnownedServerWithLog(gone)
            self.assertEqual(
                self.second_look(gone, [(2, 4002, 9000.0)], {}, unused, gone_owner),
                (True, [2]),
            )
            self.assertFalse(gone.exists())
        with patch.object(codex_mesh, "_running_servers", side_effect=OSError("no ps")), \
                patch.object(codex_mesh, "_reap") as reap:
            self.assertFalse(codex_mesh._stop_unused(  # pyright: ignore[reportPrivateUsage]
                (gone.parent,), 2, 4002, gone_owner
            ))
        reap.assert_not_called()

    def idle_stop(self, folder: Path, running: list[tuple[int, int, float]] | OSError,
                  verdict: codex_mesh.SweepVerdict, stops: bool = True) -> tuple[int, list[tuple[object, ...]]]:
        stopped: list[tuple[object, ...]] = []

        def stopper(*arguments: object) -> bool:
            stopped.append(arguments)
            return stops

        with patch.object(codex_mesh, "_running_servers", side_effect=[running]), \
                patch.object(codex_mesh, "_client_counts", return_value={}), \
                patch.object(
                    codex_mesh,
                    "_judge",
                    return_value=codex_mesh.SweepJudgment(
                        verdict, codex_mesh.CurrentRunServer(folder)
                    ),
                ) as judge, \
                patch.object(codex_mesh, "_stop_unused", side_effect=stopper):
            result = codex_mesh.command_idle_stop(argparse.Namespace(session_dir=str(folder), pid=2, port=4002))
        if judge.called:
            # A server judging itself never reads the run-active markers.
            self.assertEqual(judge.call_args.args[:1], (None,))
        return result, stopped

    def test_a_server_stops_itself_only_when_nothing_uses_it(self) -> None:
        unused, in_use = codex_mesh.ServerUnused("quiet for 1.0 h"), codex_mesh.ServerInUse("1 client connection(s)")
        old = time.time() - 7200
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            record = run / codex_mesh.SERVER_FILE
            _ = record.write_text(json.dumps({"pid": 2, "port": 4002}))

            def aged() -> float:
                os.utime(record, (old, old))
                return old

            # In use, or unused at the first look only: kept, and the record is touched so the
            # watcher waits out the idle time before it asks again.
            for verdict, stops in ((in_use, True), (unused, False)):
                _ = aged()
                self.assertEqual(self.idle_stop(run, [(2, 4002, 9000.0)], verdict, stops)[0], 1)
                self.assertGreater(record.stat().st_mtime, old + 3600)
            # It cannot look: kept.
            _ = aged()
            self.assertEqual(self.idle_stop(run, OSError("no ps"), unused), (1, []))
            self.assertGreater(record.stat().st_mtime, old + 3600)
            # A record that names a newer server is that server's to touch.
            _ = record.write_text(json.dumps({"pid": 7, "port": 4007}))
            _ = aged()
            self.assertEqual(self.idle_stop(run, [(2, 4002, 9000.0)], in_use)[0], 1)
            self.assertLess(record.stat().st_mtime, old + 1)
            # The server no longer runs: the watcher is told to leave, and nothing is judged.
            self.assertEqual(self.idle_stop(run, [(2, 4999, 9000.0)], unused), (0, []))
            # Unused at both looks: stopped, with a line in the server's log saying why.
            self.assertEqual(
                self.idle_stop(run, [(2, 4002, 9000.0)], unused),
                (0, [(None, 2, 4002, codex_mesh.CurrentRunServer(run))]),
            )
            self.assertIn("app-server 2 stopped itself: quiet for 1.0 h",
                          (run / codex_mesh.SERVER_LOG).read_text(encoding="utf-8"))

    def test_a_server_judging_itself_ignores_the_run_active_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run-a"
            run.mkdir()
            record = run / codex_mesh.SERVER_FILE
            _ = record.write_text(json.dumps({"pid": 2, "port": 4002}))
            old = time.time() - codex_mesh.SERVER_IDLE_SECS - 60
            os.utime(record, (old, old))
            with patch.object(codex_mesh, "_server_folder", return_value=codex_mesh.MeshServerLog(run)), \
                    patch.object(codex_mesh, "_marked_active", return_value=True) as marked, \
                    patch.object(codex_mesh, "_busy_threads", return_value=()):
                judge = codex_mesh._judge  # pyright: ignore[reportPrivateUsage]
                self.assertIsInstance(
                    judge(None, 2, 4002, 9000.0, 0).verdict,
                    codex_mesh.ServerUnused,
                )
                marked.assert_not_called()
                self.assertIsInstance(
                    judge((root,), 2, 4002, 9000.0, 0).verdict,
                    codex_mesh.ServerInUse,
                )

    def test_a_server_the_run_retired_is_read_as_retired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run-a"
            run.mkdir()
            _ = (run / codex_mesh.RETIRED_FILE).write_text(json.dumps({"servers": [{"port": 4002, "pid": 2}]}))
            with patch.object(codex_mesh, "_server_folder", return_value=codex_mesh.MeshServerLog(run)), \
                    patch.object(codex_mesh, "_marked_active", return_value=True), \
                    patch.object(codex_mesh, "_busy_threads", return_value=()):
                judge = codex_mesh._judge  # pyright: ignore[reportPrivateUsage]
                self.assertIsInstance(
                    judge((root,), 2, 4002, 60.0, 0).verdict,
                    codex_mesh.ServerUnused,
                )
                self.assertIsInstance(
                    judge((root,), 3, 4003, 10.0, 0).verdict,
                    codex_mesh.ServerInUse,
                )

    def test_any_live_current_record_protects_a_server_from_stale_duplicate_owners(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ended = root / "a-ended"
            live = root / "z-live"
            ended.mkdir()
            live.mkdir()
            for run in (ended, live):
                _ = (run / codex_mesh.SERVER_FILE).write_text(
                    json.dumps({"pid": 2, "port": 4002}), encoding="utf-8"
                )
            old = time.time() - codex_mesh.SERVER_IDLE_SECS - 60
            os.utime(ended / codex_mesh.SERVER_FILE, (old, old))
            (root / "active").mkdir()
            _ = (root / "active" / "session-1").write_text(f"{live}\n", encoding="utf-8")
            with patch.object(codex_mesh, "_server_folder", return_value=codex_mesh.MeshServerLog(ended)), \
                    patch.object(codex_mesh, "_busy_threads", return_value=()):
                judgment = codex_mesh._judge(  # pyright: ignore[reportPrivateUsage]
                    (root,), 2, 4002, 9000.0, 0
                )
        self.assertIsInstance(judgment.verdict, codex_mesh.ServerInUse)
        self.assertEqual(judgment.ownership, codex_mesh.CurrentRunServer(live))

    def test_a_retired_owner_wins_over_an_ended_stale_current_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stale = root / "a-stale"
            retired = root / "z-retired"
            stale.mkdir()
            retired.mkdir()
            _ = (stale / codex_mesh.SERVER_FILE).write_text(
                json.dumps({"pid": 2, "port": 4002}), encoding="utf-8"
            )
            _ = (retired / codex_mesh.RETIRED_FILE).write_text(
                json.dumps({"servers": [{"pid": 2, "port": 4002}]}),
                encoding="utf-8",
            )
            old = time.time() - codex_mesh.SERVER_IDLE_SECS - 60
            os.utime(stale / codex_mesh.SERVER_FILE, (old, old))

            ownership = codex_mesh._server_ownership(  # pyright: ignore[reportPrivateUsage]
                (root,), 2, 4002, codex_mesh.MeshServerLog(stale)
            )

        self.assertEqual(ownership, codex_mesh.RetiredRunServer(retired))

    def test_stopping_a_retired_server_drops_it_from_the_retired_list(self) -> None:
        # `stop` signals every pid on that list at the run's end; one already stopped could by then
        # be another process.
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run-a"
            run.mkdir()
            retired = run / codex_mesh.RETIRED_FILE
            _ = retired.write_text(json.dumps({"servers": [{"port": 4002, "pid": 2}, {"port": 4009, "pid": 9}]}))
            stopped = self.second_look(
                run,
                [(2, 4002, 9000.0)],
                {},
                codex_mesh.ServerUnused("retired"),
                codex_mesh.RetiredRunServer(run),
            )
            self.assertEqual(stopped, (True, [2]))
            self.assertEqual(json.loads(retired.read_text()), {"servers": [{"port": 4009, "pid": 9}]})

    def worker_client(self, roster: dict[str, object], verb: str) -> tuple[list[int], bool]:
        """The ports `send` or `steer` opened for worker `worker`, and whether it asked for the run's server."""
        ports: list[int] = []

        class FakeClient:
            def __init__(self, port: int, _name: str) -> None:
                ports.append(port)

            @bounded_fixture
            def call(self, _method: str, _params: dict[str, object]) -> dict[str, object]:
                return {"result": {"data": ["thread-1"], "turnId": "turn-1"}}

            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as directory:
            _ = (Path(directory) / codex_mesh.ROSTER_FILE).write_text(json.dumps({"worker": roster}))
            arguments = argparse.Namespace(session_dir=directory, to="worker", message="again", message_file="")
            command = codex_mesh.command_send if verb == "send" else codex_mesh.command_steer
            with patch.object(codex_mesh, "ensure_server", return_value=(4002, False)) as ensure, \
                    patch.object(codex_mesh, "Client", FakeClient), \
                    contextlib.redirect_stdout(io.StringIO()):
                _ = command(arguments)
        return ports, ensure.called

    def test_live_launcher_holds_send_locally_and_steer_reaches_outgoing_server(self) -> None:
        # After the run moves to a new server, a worker still attached to the old one is reached there.
        attached: dict[str, object] = {"thread_id": "thread-1", "status": "running", "turn_id": "turn-1",
                    "launcher_pid": os.getpid(), "port": 4001}
        for verb in ("send", "steer"):
            with self.subTest(verb=verb):
                self.assertEqual(self.worker_client(attached, verb), ([], False) if verb == "send" else ([4001], False))
                if verb == "steer":
                    self.assertEqual(self.worker_client({**attached, "launcher_pid": 0}, verb), ([4002], True))

    def test_message_without_live_launcher_is_held_without_contacting_a_server(self) -> None:
        for launcher in (0, 99999999, None):
            with self.subTest(launcher=launcher), tempfile.TemporaryDirectory() as directory:
                record: dict[str, object] = {"thread_id": "thread-1", "status": "running"}
                if launcher is not None:
                    record["launcher_pid"] = launcher
                roster = Path(directory) / codex_mesh.ROSTER_FILE
                _ = roster.write_text(json.dumps({"worker": record}))
                before = roster.read_bytes()
                arguments = argparse.Namespace(session_dir=directory, to="worker", message="message", message_file="")
                with (patch.object(codex_mesh, "ensure_server") as ensure,
                      patch.object(codex_mesh, "Client") as client,
                      contextlib.redirect_stdout(io.StringIO()) as output,
                      contextlib.redirect_stderr(io.StringIO()) as errors):
                    self.assertEqual(codex_mesh.command_send(arguments), 0)
                self.assertEqual(errors.getvalue(), "")
                self.assertEqual(output.getvalue(),
                                 "queued for worker; no launcher is attached, so the message waits for its next dispatch\n")
                ensure.assert_not_called()
                client.assert_not_called()
                self.assertEqual(roster.read_bytes(), before)
                pending = cast(codex_mesh.WorkerPendingMessages, json.loads(
                    (Path(directory) / "worker.pending.json").read_text()))
                self.assertEqual(pending["thread_id"], "thread-1")
                self.assertEqual(pending["messages"], ["message"])


class SignInChangeTests(unittest.TestCase):
    """Moving runs off an app-server that holds a Codex sign-in from before the last change."""

    @override
    def setUp(self) -> None:
        _ = self.enterContext(patch.object(codex_mesh, "_server_folder", return_value=codex_mesh.NonMeshServerStderr()))

    def test_the_time_a_server_has_run_is_read_from_ps(self) -> None:
        elapsed = codex_mesh._elapsed_secs  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(elapsed("05:03"), 303.0)
        self.assertEqual(elapsed("  01:02:03\n"), 3723.0)
        self.assertEqual(elapsed("2-01:02:03"), 2 * 86400 + 3723.0)
        self.assertIsNone(elapsed(""))
        self.assertIsNone(elapsed("soon"))

    def test_the_sign_in_file_is_only_ever_looked_at_never_read(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            sign_in = Path(home) / "auth.json"
            _ = sign_in.write_text("secret")
            sign_in.chmod(0)
            os.utime(sign_in, (1000.0, 1000.0))
            with patch.dict(os.environ, {"CODEX_HOME": home}):
                self.assertEqual(codex_mesh._sign_in_changed_at(Path(home)), 1000.0)  # pyright: ignore[reportPrivateUsage]
            sign_in.unlink()
            with patch.dict(os.environ, {"CODEX_HOME": home}):
                self.assertIsNone(codex_mesh._sign_in_changed_at(Path(home)))  # pyright: ignore[reportPrivateUsage]

    def test_notice_kind_controls_sender_even_when_the_text_names_the_other_kind(self) -> None:
        for kind, text, sender, summary in (
            (codex_mesh.CodexNoticeKind.WORKERS_CHANGED, "Codex signed in", "codex-workers", "Codex workers account changed"),
            (codex_mesh.CodexNoticeKind.SIGN_IN_CHANGED, "Workers account changed", "codex-sign-in", "Codex sign-in changed"),
        ):
            with self.subTest(kind=kind), patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as send:
                codex_mesh._notify_holder("holder", text, kind)  # pyright: ignore[reportPrivateUsage]
            command = cast(list[str], send.call_args.args[0])
            self.assertEqual(command[command.index("--from") + 1], sender)
            self.assertEqual(command[command.index("--summary") + 1], summary)

    def test_the_holder_is_told_by_its_session_id(self) -> None:
        with patch.object(subprocess, "run") as run:
            codex_mesh._notify_holder("abc-123", "Codex was signed in again", codex_mesh.CodexNoticeKind.SIGN_IN_CHANGED)  # pyright: ignore[reportPrivateUsage]
        command = cast("list[str]", run.call_args.args[0])
        self.assertEqual(Path(command[1]).name, "send.py")
        self.assertEqual(command[command.index("--to") + 1], "session:abc-123")
        self.assertEqual(command[command.index("--text") + 1], "Codex was signed in again")

    def signin_changed(self, root: Path, starts: dict[int, float], stops: bool) -> tuple[list[tuple[str, str]], list[int]]:
        """Run `signin-changed` with the sign-in changed at 2000.0: who was told what, and which
        retired servers were left a watcher."""
        told: list[tuple[str, str]] = []

        def tell(session_id: str, line: str, _kind: codex_mesh.CodexNoticeKind) -> None:
            told.append((session_id, line))

        with patch.object(codex_mesh, "_sign_in_changed_at", return_value=2000.0), \
                patch.object(sessions, "running_sessions", return_value=[running_holder("session-a")]), \
                patch.object(codex_mesh, "_server_started_at", side_effect=starts.get), \
                patch.object(codex_mesh, "_stop_unused", return_value=stops) as stop, \
                patch.object(codex_mesh, "_start_watcher") as watch, \
                patch.object(codex_mesh, "_notify_holder", side_effect=tell), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(codex_mesh.command_signin_changed(argparse.Namespace(root=str(root))), 0)
        for call in stop.call_args_list:
            self.assertIsNone(cast("object", call.args[0]))
        return told, [cast("int", call.args[1]) for call in watch.call_args_list]

    def test_each_run_on_an_older_server_moves_and_its_holder_is_told(self) -> None:
        old, new = subprocess.Popen(["sleep", "30"]), subprocess.Popen(["sleep", "30"])
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "active").mkdir()
                for run, (port, process) in {"run-a": (4001, old), "run-b": (4002, new)}.items():
                    (root / run).mkdir()
                    _ = (root / run / codex_mesh.SERVER_FILE).write_text(json.dumps({"port": port, "pid": process.pid,
                        "codex_home": str(agent_accounts.workers_home()), "auto_compact_tokens": codex_mesh.AUTO_COMPACT_TOKENS}))
                (root / "run-c").mkdir()
                _ = (root / "active" / "session-a").write_text(f"{root / 'run-a'}\n")
                starts = {old.pid: 1000.0, new.pid: 3000.0}
                told, watched = self.signin_changed(root, starts, stops=False)
                self.assertFalse((root / "run-a" / codex_mesh.SERVER_FILE).exists())
                self.assertTrue((root / "run-b" / codex_mesh.SERVER_FILE).exists())
                self.assertEqual(watched, [old.pid])
                self.assertEqual([holder for holder, _line in told], ["session-a"])
                line = told[0][1]
                self.assertIn(f"app-server {old.pid}", line)
                self.assertIn("run-a", line)
                self.assertIn("running workers were interrupted and resumed on a new server", line)
                self.assertNotIn("when that turn ends", line)
                log = (root / "run-a" / codex_mesh.SERVER_LOG).read_text()
                self.assertIn("app-server", log)
                self.assertIn("running workers were interrupted and resumed on a new server", log)
                # The same change seen again moves nothing and tells no one.
                self.assertEqual(self.signin_changed(root, starts, stops=False), ([], []))
        finally:
            for process in (old, new):
                process.kill()
                _ = process.wait(timeout=5)

    def test_a_holder_that_ends_while_earlier_runs_move_leaves_its_run_ended(self) -> None:
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(sessions, "running_sessions", side_effect=[
                    [running_holder("session-a"), running_holder("session-b")], [running_holder("session-a")]]):
            folder = Path(directory)
            self.assertTrue(codex_mesh._run_is_live(folder, ("session-a",)))  # pyright: ignore[reportPrivateUsage]
            self.assertFalse(codex_mesh._run_is_live(folder, ("session-b",)))  # pyright: ignore[reportPrivateUsage]

    def test_an_idle_run_has_its_old_server_stopped_at_once(self) -> None:
        old = subprocess.Popen(["sleep", "30"])
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "run-a").mkdir()
                _ = (root / "run-a" / codex_mesh.SERVER_FILE).write_text(json.dumps({"port": 4001, "pid": old.pid,
                    "codex_home": str(agent_accounts.workers_home()), "auto_compact_tokens": codex_mesh.AUTO_COMPACT_TOKENS}))
                told, watched = self.signin_changed(root, {old.pid: 1000.0}, stops=True)
                # No run-active marker names it, so there is no one to tell.
                self.assertEqual((told, watched), ([], []))
                self.assertTrue((root / "run-a" / codex_mesh.RETIRED_FILE).exists())
        finally:
            old.kill()
            _ = old.wait(timeout=5)

    def test_live_run_with_only_retired_old_sign_in_is_not_moved_or_notified(self) -> None:
        for has_current in (False, True):
            with self.subTest(has_current=has_current), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                run = root / "already-moved"
                run.mkdir()
                (root / "active").mkdir()
                _ = (root / "active" / "session-a").write_text(str(run) + "\n")
                home = root / "home"
                retired = run / codex_mesh.RETIRED_FILE
                _ = retired.write_text(json.dumps({"servers": [
                    {"port": 4001, "pid": 101, "codex_home": str(home)}]}))
                if has_current:
                    _ = (run / codex_mesh.SERVER_FILE).write_text(json.dumps(
                        {"port": 4002, "pid": 102, "codex_home": str(home)}))
                _ = (run / codex_mesh.ROSTER_FILE).write_text(json.dumps({"worker": {
                    "thread_id": "same-thread", "turn_id": "live-turn", "status": "running", "port": 4002}}))
                before = {path.name: path.read_bytes() for path in run.iterdir()}
                with (patch.dict(os.environ, {"CODEX_HOME": str(home)}),
                      patch.object(sessions, "running_sessions", return_value=[running_holder("session-a")]),
                      patch.object(codex_mesh, "_sign_in_changed_at", return_value=2000.0),
                      patch.object(codex_mesh, "_server_started_at", side_effect={101: 1000.0, 102: 3000.0}.get),
                      patch.object(codex_mesh, "_pid_alive", return_value=True),
                      patch.object(codex_mesh, "_switch_run_workers") as switch,
                      patch.object(codex_mesh, "_stop_unused") as stop,
                      patch.object(codex_mesh, "_start_watcher") as watch,
                      patch.object(codex_mesh, "_notify_holder") as notify,
                      contextlib.redirect_stdout(io.StringIO()) as output):
                    self.assertEqual(codex_mesh.command_signin_changed(argparse.Namespace(root=str(root))), 0)
                self.assertIn("0 run(s) moved", output.getvalue())
                switch.assert_not_called()
                stop.assert_not_called()
                watch.assert_not_called()
                notify.assert_not_called()
                for name, contents in before.items():
                    self.assertEqual((run / name).read_bytes(), contents)
                self.assertFalse((run / "worker.account-move.json").exists())

    def test_a_resident_worker_moves_to_the_new_server_between_turns(self) -> None:
        calls: list[tuple[int, str]] = []
        clients: list[object] = []
        queued: list[list[object]] = [[{"id": "q1", "input": [{"type": "text", "text": "from a peer"}]}]]

        class FakeClient:
            port: int
            closed: bool

            def __init__(self, port: int, _name: str) -> None:
                self.port = port
                home = Path("/fixture/codex")
                self.serving: codex_mesh.ServingCodexServer = codex_mesh.ServingCodexServer(
                    port, 0, home, agent_accounts.UnknownCodexAccount(home, "fixture has no sign-in"))
                self.name: str = _name
                self.turn_submitted_at: float = 0.0
                self.closed = False
                clients.append(self)

            @bounded_fixture
            def call(self, method: str, _params: dict[str, object], **_options: object) -> dict[str, object]:
                calls.append((self.port, method))
                if method == "thread/read":
                    return {"result": {"thread": {"status": {"type": "idle"}}}}
                if method == "thread/queue/list":
                    return {"result": {"data": queued.pop(0) if queued else []}}
                if method == "thread/loaded/list":
                    return {"result": {"data": []}}
                if method == "turn/start":
                    return {"result": {"turn": {"id": "turn-2"}}}
                return {"result": {}}

            def close(self) -> None:
                self.closed = True

        with tempfile.TemporaryDirectory() as directory:
            _ = (Path(directory) / codex_mesh.ROSTER_FILE).write_text(json.dumps({"worker": {
                "thread_id": "thread-1", "status": "running", "turn_id": "turn-1", "launcher_pid": os.getpid(),
                "port": 4001}}))
            old = FakeClient(4001, "worker")
            args = argparse.Namespace(effort="", cwd="/original-worktree", turn_cwd="/turn-worktree")
            with patch.object(codex_mesh, "ensure_server", return_value=(4002, True)) as ensure, \
                    patch.object(codex_mesh, "Client", FakeClient):
                moved = codex_mesh._move_resident(  # pyright: ignore[reportPrivateUsage]
                    args, directory, "worker", "thread-1", cast("codex_mesh.Client", cast("object", old)))
            self.assertEqual(ensure.call_args.kwargs.get("cwd"), "/turn-worktree")
            roster = cast("dict[str, dict[str, object]]", json.loads((Path(directory) / codex_mesh.ROSTER_FILE).read_text()))
            pending = (Path(directory) / "worker.pending.json").read_text()
        self.assertIsInstance(moved, codex_mesh.MovedWorker)
        assert isinstance(moved, codex_mesh.MovedWorker)
        self.assertEqual((moved.port, moved.turn_id), (4002, "turn-2"))
        self.assertEqual(moved.thread_id, "thread-1")
        self.assertFalse(moved.thread_replaced)
        self.assertTrue(old.closed)
        self.assertIn((4002, "thread/resume"), calls)
        self.assertIn((4002, "turn/start"), calls)
        self.assertEqual((roster["worker"]["port"], roster["worker"]["turn_id"]), (4002, "turn-2"))
        self.assertEqual(json.loads(pending)["messages"], [])

    def test_a_resident_worker_mid_turn_stays_until_its_turn_ends(self) -> None:
        class BusyClient:
            @bounded_fixture
            def call(self, method: str, _params: dict[str, object], **_options: object) -> dict[str, object]:
                if method == "thread/turns/list":
                    return {"result": {"data": [{"id": "t", "status": "inProgress"}]}}
                return {"result": {"thread": {"status": {"type": "active"}, "turns": []}}}

            def close(self) -> None:
                raise AssertionError("a worker mid-turn keeps its connection")

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(codex_mesh, "ensure_server") as ensure:
            moved = codex_mesh._move_resident(  # pyright: ignore[reportPrivateUsage]
                argparse.Namespace(effort=""), directory, "worker", "thread-1",
                cast("codex_mesh.Client", cast("object", BusyClient())))
        self.assertIsInstance(moved, codex_mesh.WorkerWaitingOnLiveTurn)
        ensure.assert_not_called()

    def test_unreadable_thread_state_never_starts_a_replacement_turn(self) -> None:
        class UnknownClient:
            @bounded_fixture
            def call(self, _method: str, _params: dict[str, object]) -> dict[str, object]:
                return {"error": {"message": "state cannot be read"}}
            def close(self) -> None:
                raise AssertionError("Unknown thread state keeps its connection")
        with tempfile.TemporaryDirectory() as directory, patch.object(codex_mesh, "ensure_server") as ensure, patch.object(codex_mesh, "_start_turn") as start:
            result = codex_mesh._move_resident(  # pyright: ignore[reportPrivateUsage]
                argparse.Namespace(effort=""), directory, "worker", "thread-1",
                cast(codex_mesh.Client, cast(object, UnknownClient())))
        self.assertIsInstance(result, codex_mesh.WorkerThreadStateUnknown)
        ensure.assert_not_called()
        start.assert_not_called()

    def test_refused_cross_home_resume_creates_a_new_thread_and_says_so(self) -> None:
        calls: list[tuple[int, str]] = []
        class ResumeClient:
            def __init__(self, port: int, _name: str) -> None:
                self.port: int = port
                home = Path("/fixture/codex")
                self.serving: codex_mesh.ServingCodexServer = codex_mesh.ServingCodexServer(
                    port, 0, home, agent_accounts.UnknownCodexAccount(home, "fixture has no sign-in"))
                self.name: str = _name
                self.turn_submitted_at: float = 0.0
            @bounded_fixture
            def call(self, method: str, _params: dict[str, object], **_options: object) -> dict[str, object]:
                calls.append((self.port, method))
                if method == "thread/read":
                    return {"result": {"thread": {"status": {"type": "idle"}}}}
                if method == "thread/queue/list":
                    return {"result": {"data": []}}
                if method == "thread/loaded/list":
                    return {"result": {"data": []}}
                if method == "thread/resume":
                    return {"error": {"message": "thread cannot resume in this home"}}
                if method == "thread/start":
                    return {"result": {"thread": {"id": "new-thread"}}}
                if method == "turn/start":
                    return {"result": {"turn": {"id": "new-turn"}}}
                return {"result": {}}
            def close(self) -> None:
                pass
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            _ = (run / codex_mesh.ROSTER_FILE).write_text(json.dumps({"worker": {
                "thread_id": "old-thread", "turn_id": "old-turn", "status": "running", "port": 4001,
                "role": "impl",
            }}))
            _ = (run / "worker.pending.json").write_text(json.dumps({"thread_id": "old-thread", "messages": ["held message"]}))
            args = argparse.Namespace(model="gpt-test", effort="high", cwd=directory, sandbox="danger-full-access", resident=True, service_tier="")
            with patch.object(codex_mesh, "ensure_server", return_value=(4002, True)), patch.object(codex_mesh, "Client", ResumeClient):
                result = codex_mesh._move_resident(args, directory, "worker", "old-thread",  # pyright: ignore[reportPrivateUsage]
                    cast(codex_mesh.Client, cast(object, ResumeClient(4001, "worker"))))
            self.assertIsInstance(result, codex_mesh.MovedWorker)
            if not isinstance(result, codex_mesh.MovedWorker):
                self.fail("An idle worker whose resume is refused must get a new thread")
            self.assertEqual(result.thread_id, "new-thread")
            self.assertTrue(result.thread_replaced)
            roster = cast(dict[str, dict[str, object]], json.loads((run / codex_mesh.ROSTER_FILE).read_text()))
            self.assertEqual(roster["worker"]["thread_id"], "new-thread")
            self.assertEqual(calls.count((4002, "thread/start")), 1)
            self.assertEqual(calls.count((4002, "turn/start")), 1)
            board = (run / "board.log").read_text()
            self.assertIn("new thread", board.lower())
            self.assertIn("worker", board)


    def test_home_migration_confirms_old_writer_release_before_resuming(self) -> None:
        calls: list[tuple[int, str]] = []
        released = [False]
        confirmed = [False]
        class MigrationClient:
            def __init__(self, port: int, _name: str) -> None:
                self.port: int = port
                home = Path("/fixture/codex")
                self.serving: codex_mesh.ServingCodexServer = codex_mesh.ServingCodexServer(
                    port, 0, home, agent_accounts.UnknownCodexAccount(home, "fixture has no sign-in"))
                self.name: str = _name
                self.turn_submitted_at: float = 0.0
            @bounded_fixture
            def call(self, method: str, params: dict[str, object], **_options: object) -> dict[str, object]:
                calls.append((self.port, method))
                if method == "thread/read":
                    return {"result": {"thread": {"status": {"type": "idle"}}}}
                if method == "thread/queue/list":
                    return {"result": {"data": []}}
                if method == "thread/unsubscribe":
                    released[0] = True
                    return {"result": {}}
                if method == "thread/loaded/list":
                    if not params.get("cursor"):
                        return {"result": {"data": ["another-thread"], "nextCursor": "last"}}
                    confirmed[0] = released[0]
                    return {"result": {"data": [] if released[0] else ["old-thread"], "nextCursor": None}}
                if method == "thread/resume" and not confirmed[0]:
                    raise AssertionError("Target resume must follow confirmed old-writer release")
                return {"result": {}}
            def close(self) -> None:
                pass
        with tempfile.TemporaryDirectory() as directory:
            _ = (Path(directory) / codex_mesh.ROSTER_FILE).write_text(json.dumps({"worker": {
                "thread_id": "old-thread", "turn_id": "old-turn", "status": "running", "port": 4001,
            }}))
            with patch.object(codex_mesh, "ensure_server", return_value=(4002, True)), patch.object(codex_mesh, "Client", MigrationClient):
                result = codex_mesh._move_resident(  # pyright: ignore[reportPrivateUsage]
                    argparse.Namespace(effort=""), directory, "worker", "old-thread",
                    cast(codex_mesh.Client, cast(object, MigrationClient(4001, "worker"))))
        self.assertIsInstance(result, codex_mesh.MovedWorker)
        self.assertLess(calls.index((4001, "thread/unsubscribe")), calls.index((4002, "thread/resume")))
        self.assertEqual(calls.count((4001, "thread/loaded/list")), 2)

    def test_unconfirmed_old_writer_release_starts_no_thread_or_turn_on_target(self) -> None:
        calls: list[str] = []
        quiet = threading.Event()
        class HeldClient:
            port: int = 4001
            def __init__(self) -> None:
                home = Path("/fixture/codex")
                self.serving: codex_mesh.ServingCodexServer = codex_mesh.ServingCodexServer(
                    self.port, 0, home, agent_accounts.UnknownCodexAccount(home, "fixture has no sign-in"))
                self.name: str = "worker"
                self.turn_submitted_at: float = 0.0
            @bounded_fixture
            def call(self, method: str, params: dict[str, object], **_options: object) -> dict[str, object]:
                calls.append(method)
                if method == "thread/read":
                    return {"result": {"thread": {"status": {"type": "idle"}}}}
                if method == "thread/queue/list":
                    return {"result": {"data": []}}
                if method == "thread/loaded/list":
                    return {"result": {"data": ["old-thread"] if params.get("cursor") else [],
                        "nextCursor": None if params.get("cursor") else "last"}}
                return {"result": {}}
            @bounded_fixture
            def next_frame(self, deadline: float) -> codex_mesh.RpcMessage | None:
                _ = quiet.wait(timeout=max(0.0, deadline - time.time()))
                return None
            def close(self) -> None:
                raise AssertionError("An unconfirmed release keeps the outgoing connection")
        with tempfile.TemporaryDirectory() as directory:
            _ = (Path(directory) / codex_mesh.ROSTER_FILE).write_text(json.dumps({"worker": {
                "thread_id": "old-thread", "turn_id": "old-turn", "status": "running", "port": 4001,
            }}))
            with patch.object(codex_mesh, "ensure_server", return_value=(4002, True)), patch.object(codex_mesh, "THREAD_RELEASE_TIMEOUT_SECS", 0.01), patch.object(codex_mesh, "Client") as target:
                result = codex_mesh._move_resident(  # pyright: ignore[reportPrivateUsage]
                    argparse.Namespace(effort=""), directory, "worker", "old-thread",
                    cast(codex_mesh.Client, cast(object, HeldClient())))
            self.assertIsInstance(result, codex_mesh.WorkerThreadStateUnknown)
            target.assert_not_called()
            self.assertNotIn("thread/resume", calls)
            self.assertNotIn("thread/start", calls)
            self.assertNotIn("turn/start", calls)
            self.assertEqual(cast(dict[str, dict[str, object]], json.loads((Path(directory) / codex_mesh.ROSTER_FILE).read_text()))["worker"]["thread_id"], "old-thread")


@final
class LauncherSessionSwapTests(unittest.TestCase):
    def test_swap_holderless_launcher_finds_nearest_session_with_matching_process_start(self) -> None:
        cases: tuple[tuple[dict[int, str], tuple[str, ...]], ...] = (
            ({700: "start-700", 600: "start-600"}, ("near-session",)),
            ({700: "reused-pid", 600: "start-600"}, ("far-session",)),
            ({700: "", 600: "start-600"}, ("far-session",)),
            ({}, ()),
        )
        for registered, expected in cases:
            with self.subTest(registered=registered), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                run = root / "runs/holderless-run"
                run.mkdir(parents=True)
                registry = root / "home/.claude/sessions"
                registry.mkdir(parents=True)
                for pid, started in registered.items():
                    _ = (registry / f"{pid}.json").write_text(json.dumps(sessions.SessionRecord(
                        pid=pid, sessionId="near-session" if pid == 700 else "far-session",
                        name="launching director", procStart=started, messagingSocketPath="",
                        updatedAt=1, tmux="", formerNames=[])))
                _ = (run / codex_mesh.SERVER_FILE).write_text(json.dumps({
                    "pid": 800, "port": 4001, "codex_home": str(root / "outgoing-home")}))
                _ = (run / codex_mesh.ROSTER_FILE).write_text(json.dumps({
                    "project-test": {"thread_id": "scratch-thread", "status": "running",
                                     "turn_id": "scratch-turn", "launcher_pid": 900, "port": 4001}}))
                table = {
                    900: process_table.Process(900, 850, "python3", "python3 codex_mesh.py start"),
                    850: process_table.Process(850, 700, "sh", "sh -c launch"),
                    700: process_table.Process(700, 600, "claude", "claude"),
                    600: process_table.Process(600, 1, "claude", "claude"),
                }
                def alive(pid: int) -> bool:
                    return pid in {*table, 800}
                def started_at(pid: int) -> str:
                    return f"start-{pid}"
                before = {path.name: path.read_bytes() for path in run.iterdir()}
                with (patch.dict(os.environ, {"HOME": str(root / "home"),
                                             "NOTIFIER_SESSIONS_DIR": str(registry)}, clear=True),
                      patch.object(codex_mesh, "SWEEP_ROOTS", (run.parent,)),
                      patch.object(codex_mesh, "_pid_alive", side_effect=alive),
                      patch.object(process_table, "snapshot", return_value=table),
                      patch.object(sessions, "process_start", side_effect=started_at),
                      # A record without a process start runs while its socket exists.
                      patch.object(sessions, "messaging_socket_exists", return_value=True),
                      patch.object(os, "kill"),
                      patch.object(codex_mesh, "_stop_unused") as stop,
                      patch.object(codex_mesh, "_interrupt_worker_for_account_move") as interrupt,
                      patch.object(codex_mesh, "_resume_account_move_detached") as detach):
                    switches = codex_mesh.retire_workers_servers(root / "target-home", "codex2", dry_run=True)
                self.assertEqual(len(switches), 1)
                self.assertEqual(switches[0].launcher_sessions, expected)
                self.assertEqual(switches[0].holders, ())
                self.assertEqual([worker.name for worker in switches[0].workers], ["project-test"])
                self.assertEqual(switches[0].leftover_servers, 0)
                self.assertEqual({path.name: path.read_bytes() for path in run.iterdir()
                                  if path.name in before}, before)
                self.assertFalse((run / "project-test.account-move.json").exists())
                stop.assert_not_called()
                interrupt.assert_not_called()
                detach.assert_not_called()


@final
class WorkersHomeTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.home = Path()
        self.session_dir = Path()

    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = root / "home"
        self.home.mkdir()
        self.session_dir = root / "run"
        self.session_dir.mkdir()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HOME": str(self.home), "AGENT_NOTES_DIR": str(self.home)}, clear=True)))
        select_fixture_workers(self.home, notes=self.home)
        _ = self.enterContext(patch.object(codex_mesh, "_server_folder", return_value=codex_mesh.NonMeshServerStderr()))
        _ = self.enterContext(patch.object(sessions, "running_sessions", return_value=[
            running_holder(holder) for holder in ("director-one", "director-two", "holder-id", "test-holder")]))

    def mark_live_holder(self) -> None:
        active = self.session_dir.parent / "active"
        active.mkdir(exist_ok=True)
        _ = (active / "test-holder").write_text(str(self.session_dir) + "\n")
        _ = self.enterContext(patch.object(codex_mesh, "_notify_holder"))

    def select(self, label: str = "codex1") -> Path:
        home = self.home / ("." + note_name(label).removesuffix(".md"))
        home.mkdir(exist_ok=True)
        login = f"{label.replace(' ', '-')}@example.invalid"
        claims = base64.urlsafe_b64encode(json.dumps({"email": login}).encode()).decode().rstrip("=")
        _ = (home / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"header.{claims}.signature"}}))
        _ = (self.home / note_name(label)).write_text(f"---\nlogin: {login}\n---\n")
        self.assertEqual(accounts.main(["set-role", "workers", label]), 0)
        return home

    def record(self, home: Path, port: int = 4001) -> None:
        _ = (self.session_dir / codex_mesh.SERVER_FILE).write_text(json.dumps({"port": port, "pid": os.getpid(), "codex_home": str(home), "auto_compact_tokens": codex_mesh.AUTO_COMPACT_TOKENS}))

    def writer_process_is_live(self, pid: int) -> bool:
        return pid == os.getpid()

    def test_mesh_server_child_and_record_follow_role_with_conflicting_caller_home(self) -> None:
        home = self.select()
        process = MagicMock(spec=subprocess.Popen)
        process.pid = 99999999
        poll = cast(MagicMock, process.poll)
        poll.return_value = None
        with patch.dict(os.environ, {"CODEX_HOME": str(self.home / "caller")}), patch.object(subprocess, "Popen", return_value=process) as launch, patch.object(codex_mesh, "_free_port", return_value=4001), patch.object(codex_mesh, "WebSocket"), patch.object(codex_mesh, "_start_watcher"):
            port = codex_mesh._start_server(str(self.session_dir), agent_accounts.workers_home())  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(port, 4001)
        command = cast(list[str], launch.call_args.args[0])
        self.assertIn("thread_unload_delay_secs=0", command)
        self.assertIn(f"model_auto_compact_token_limit={codex_mesh.AUTO_COMPACT_TOKENS}", command)
        self.assertIn("background_terminal_max_timeout=300000", command)
        environment = cast(dict[str, str], launch.call_args.kwargs["env"])
        self.assertEqual(environment["CODEX_HOME"], str(home))
        record = cast(dict[str, object], json.loads((self.session_dir / codex_mesh.SERVER_FILE).read_text()))
        self.assertEqual(record["codex_home"], str(home))
        self.assertEqual(record["auto_compact_tokens"], codex_mesh.AUTO_COMPACT_TOKENS)

    def started_environment(self, caller: dict[str, str]) -> dict[str, str]:
        _ = self.select()
        process = MagicMock(spec=subprocess.Popen)
        process.pid = 99999999
        cast(MagicMock, process.poll).return_value = None
        with patch.dict(os.environ, caller), patch.object(subprocess, "Popen", return_value=process) as launch, patch.object(codex_mesh, "_free_port", return_value=4001), patch.object(codex_mesh, "WebSocket"), patch.object(codex_mesh, "_start_watcher"):
            _ = codex_mesh._start_server(str(self.session_dir), agent_accounts.workers_home())  # pyright: ignore[reportPrivateUsage]
        return cast(dict[str, str], launch.call_args.kwargs["env"])

    def test_a_run_folders_server_names_its_run_whatever_the_caller_carries(self) -> None:
        # implement.sh's claim step can start the server before the launcher exports the run.
        with patch.object(codex_mesh, "RUN_ROOTS", (self.session_dir.parent,)):
            for caller in ({}, {"UNIT_RUN_DIR": str(self.home / "other-run")}):
                with self.subTest(caller=caller):
                    environment = self.started_environment(caller)
                    self.assertEqual(environment["UNIT_RUN_DIR"], str(self.session_dir))

    def test_a_server_outside_the_run_roots_keeps_the_callers_environment(self) -> None:
        # A review pool's mesh directory serves many units' turns, so it names no run.
        with patch.object(codex_mesh, "RUN_ROOTS", (self.home,)):
            self.assertNotIn("UNIT_RUN_DIR", self.started_environment({}))

    def test_home_change_retires_server_even_when_sign_in_timestamps_are_unchanged(self) -> None:
        home = self.select()
        self.record(self.home / ".codex")
        with patch.object(codex_mesh, "_sign_in_changed_at", return_value=1000), patch.object(codex_mesh, "_server_started_at", return_value=2000), patch.object(codex_mesh, "_start_server", return_value=4002) as start, patch.object(codex_mesh, "_start_watcher") as watch:
            self.assertEqual(codex_mesh.ensure_server(str(self.session_dir)), (4002, True))
        self.assertFalse((self.session_dir / codex_mesh.SERVER_FILE).exists())
        start.assert_called_once()
        watch.assert_called_once()
        self.assertEqual(agent_accounts.workers_home(), home)

    def test_legacy_server_is_retired_when_role_moves_to_numbered_home_without_stopping_live_turn(self) -> None:
        _ = self.select()
        _ = (self.session_dir / codex_mesh.SERVER_FILE).write_text(json.dumps({"port": 4001, "pid": os.getpid()}))
        with patch.object(codex_mesh, "_start_server", return_value=4002), patch.object(codex_mesh, "_start_watcher"), patch.object(os, "kill") as signal:
            self.assertEqual(codex_mesh.ensure_server(str(self.session_dir)), (4002, True))
        self.assertTrue(all(invocation.args[1] == 0 for invocation in signal.call_args_list))
        retired = cast(dict[str, object], json.loads((self.session_dir / codex_mesh.RETIRED_FILE).read_text()))
        self.assertIn("4001", str(retired))

    def test_legacy_server_is_kept_when_role_stays_default_and_sign_in_is_current(self) -> None:
        record = self.session_dir / codex_mesh.SERVER_FILE
        _ = record.write_text(json.dumps({"port": 4001, "pid": os.getpid(), "auto_compact_tokens": codex_mesh.AUTO_COMPACT_TOKENS}))
        before = record.read_bytes()
        with patch.dict(os.environ, {"CODEX_HOME": str(self.home / "caller")}), patch.object(codex_mesh, "_holds_old_sign_in", return_value=False) as identity, patch.object(codex_mesh, "_start_server") as start, patch.object(codex_mesh, "_stop_unused_with_lock_held") as stop:
            self.assertEqual(codex_mesh.ensure_server(str(self.session_dir)), (4001, False))
        identity.assert_called_once_with(os.getpid(), self.home / ".codex")
        start.assert_not_called()
        stop.assert_not_called()
        self.assertEqual(record.read_bytes(), before)
        self.assertFalse((self.session_dir / codex_mesh.RETIRED_FILE).exists())

    def test_server_started_without_the_compaction_limit_is_retired_without_stopping_live_turns(self) -> None:
        home = self.select()
        for recorded in ({"port": 4001, "pid": os.getpid(), "codex_home": str(home)},
                         {"port": 4001, "pid": os.getpid(), "codex_home": str(home), "auto_compact_tokens": 100_000}):
            with self.subTest(recorded=recorded):
                _ = (self.session_dir / codex_mesh.SERVER_FILE).write_text(json.dumps(recorded))
                with patch.object(codex_mesh, "_holds_old_sign_in", return_value=False), patch.object(codex_mesh, "_start_server", return_value=4002) as start, patch.object(codex_mesh, "_start_watcher"), patch.object(os, "kill") as signal:
                    self.assertEqual(codex_mesh.ensure_server(str(self.session_dir)), (4002, True))
                start.assert_called_once()
                self.assertTrue(all(invocation.args[1] == 0 for invocation in signal.call_args_list))
                retired = cast(dict[str, object], json.loads((self.session_dir / codex_mesh.RETIRED_FILE).read_text()))
                self.assertIn("4001", str(retired))
                (self.session_dir / codex_mesh.RETIRED_FILE).unlink()

    def test_sign_in_notice_retires_legacy_server_when_default_sign_in_is_newer(self) -> None:
        _ = (self.session_dir / codex_mesh.SERVER_FILE).write_text(json.dumps({"port": 4001, "pid": os.getpid()}))
        with patch.object(codex_mesh, "_sign_in_changed_at", return_value=2000) as identity, patch.object(codex_mesh, "_server_started_at", return_value=1000), patch.object(codex_mesh, "_stop_unused", return_value=False), patch.object(codex_mesh, "_start_watcher") as watch, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(codex_mesh.command_signin_changed(argparse.Namespace(root=str(self.session_dir.parent))), 0)
        self.assertFalse((self.session_dir / codex_mesh.SERVER_FILE).exists())
        self.assertTrue(all(entry.args == (self.home / ".codex",) for entry in identity.call_args_list))
        watch.assert_called_once()
        retired = cast(dict[str, object], json.loads((self.session_dir / codex_mesh.RETIRED_FILE).read_text()))
        self.assertIn("4001", str(retired))

    def test_send_keeps_legacy_default_server_and_holds_message_for_live_launcher(self) -> None:
        record = self.session_dir / codex_mesh.SERVER_FILE
        _ = record.write_text(json.dumps({"port": 4001, "pid": os.getpid()}))
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({"worker": {
            "thread_id": "thread-1", "status": "running", "turn_id": "turn-1", "launcher_pid": os.getpid(), "port": 4001,
        }}))
        before = record.read_bytes()
        with patch.dict(os.environ, {"CODEX_HOME": str(self.home / "caller")}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(codex_mesh.command_send(argparse.Namespace(session_dir=str(self.session_dir), to="worker", message="next task", message_file="")), 0)
        self.assertEqual(record.read_bytes(), before)
        self.assertFalse((self.session_dir / codex_mesh.RETIRED_FILE).exists())
        self.assertIn("next task", (self.session_dir / "worker.pending.json").read_text())

    def test_swap_reads_legacy_source_as_default_home(self) -> None:
        self.mark_live_holder()
        home = self.select()
        _ = (self.session_dir / codex_mesh.SERVER_FILE).write_text(json.dumps({"port": 4001, "pid": os.getpid()}))
        with patch.object(codex_mesh, "SWEEP_ROOTS", (self.session_dir.parent,)), patch.object(codex_mesh, "_stop_unused", return_value=True):
            lines = codex_mesh.retire_workers_servers(home, "codex1")
        self.assertEqual([line.folder for line in lines], [self.session_dir])
        log = (self.session_dir / codex_mesh.SERVER_LOG).read_text()
        self.assertIn("interrupted and resumed on the new account", log)
        self.assertNotIn("working worker finishes first", log)
        self.assertIn("codex1 (~/.codex1)", log)

    def test_unsubscribe_not_loaded_proves_release_without_another_state_read(self) -> None:
        class UnloadedClient:
            port: int = 4001
            @bounded_fixture
            def call(self, method: str, _params: dict[str, object], **_options: object) -> codex_mesh.RpcMessage:
                if method != "thread/unsubscribe":
                    raise AssertionError("notLoaded already confirms release")
                return {"result": {"status": "notLoaded"}}
        result = codex_mesh._release_worker_thread(  # pyright: ignore[reportPrivateUsage]
            cast(codex_mesh.Client, cast(object, UnloadedClient())), "thread-1", str(self.session_dir))
        self.assertNotIsInstance(result, codex_mesh.WorkerThreadStateUnknown)

    def test_unsubscribe_not_subscribed_does_not_prove_a_loaded_thread_released(self) -> None:
        calls: list[str] = []
        quiet = threading.Event()
        class StillLoadedClient:
            port: int = 4001
            @bounded_fixture
            def call(self, method: str, _params: dict[str, object], **_options: object) -> codex_mesh.RpcMessage:
                calls.append(method)
                if method == "thread/unsubscribe":
                    return {"result": {"status": "notSubscribed"}}
                return {"result": {"data": ["thread-1"]}}
            @bounded_fixture
            def next_frame(self, deadline: float) -> codex_mesh.RpcMessage | None:
                _ = quiet.wait(timeout=max(0.0, deadline - time.time()))
                return None
        with patch.object(codex_mesh, "THREAD_RELEASE_TIMEOUT_SECS", 0.01):
            result = codex_mesh._release_worker_thread(  # pyright: ignore[reportPrivateUsage]
                cast(codex_mesh.Client, cast(object, StillLoadedClient())), "thread-1", str(self.session_dir))
        self.assertIsInstance(result, codex_mesh.WorkerThreadStateUnknown)
        self.assertIn("thread/loaded/list", calls)
        self.assertNotIn("thread/resume", calls)
        self.assertLess(len(calls), 10, "an unchanged loaded thread must block instead of busy retrying")

    def test_matching_home_sign_in_check_uses_recorded_home_instead_of_caller(self) -> None:
        home = self.select()
        self.record(home)
        with patch.dict(os.environ, {"CODEX_HOME": str(self.home / "caller")}), patch.object(codex_mesh, "_sign_in_changed_at", return_value=1000) as identity_time, patch.object(codex_mesh, "_server_started_at", return_value=2000), patch.object(codex_mesh, "_start_server") as start:
            self.assertEqual(codex_mesh.ensure_server(str(self.session_dir)), (4001, False))
        identity_time.assert_called_once_with(home)
        start.assert_not_called()

    def test_sign_in_notice_retires_only_servers_on_the_changed_home(self) -> None:
        selected = self.select()
        other_run = self.session_dir.parent / "other-run"
        other_run.mkdir()
        self.record(selected)
        _ = (other_run / codex_mesh.SERVER_FILE).write_text(json.dumps({"port": 4002, "pid": os.getpid(), "codex_home": str(self.home / ".codex"), "auto_compact_tokens": codex_mesh.AUTO_COMPACT_TOKENS}))
        with patch.dict(os.environ, {"CODEX_HOME": str(selected)}), patch.object(codex_mesh, "_sign_in_changed_at", return_value=2000), patch.object(codex_mesh, "_server_started_at", return_value=1000), patch.object(codex_mesh, "_stop_unused", return_value=False), patch.object(codex_mesh, "_start_watcher"), patch.object(codex_mesh, "_notify_holder"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(codex_mesh.command_signin_changed(argparse.Namespace(root=str(self.session_dir.parent))), 0)
        self.assertFalse((self.session_dir / codex_mesh.SERVER_FILE).exists())
        self.assertTrue((other_run / codex_mesh.SERVER_FILE).exists())

    def test_swap_retires_all_temp_runs_and_notifies_each_director_once(self) -> None:
        home = self.select()
        root = self.session_dir.parent
        (root / "active").mkdir()
        other_run = root / "other-run"
        other_run.mkdir()
        self.record(self.home / ".codex")
        _ = (other_run / codex_mesh.SERVER_FILE).write_text(json.dumps({"port": 4002, "pid": os.getpid(), "codex_home": str(self.home / ".codex"), "auto_compact_tokens": codex_mesh.AUTO_COMPACT_TOKENS}))
        _ = (root / "active" / "director-one").write_text(str(self.session_dir) + "\n")
        _ = (root / "active" / "director-two").write_text(str(other_run) + "\n")
        with patch.object(codex_mesh, "SWEEP_ROOTS", (root,)), patch.object(codex_mesh, "_stop_unused", return_value=False), patch.object(codex_mesh, "_start_watcher"), patch.object(codex_mesh, "_notify_holder") as notify:
            _ = codex_mesh.retire_workers_servers(home, "codex1")
        self.assertFalse((self.session_dir / codex_mesh.SERVER_FILE).exists())
        self.assertFalse((other_run / codex_mesh.SERVER_FILE).exists())
        self.assertCountEqual([entry.args[0] for entry in notify.call_args_list], ["director-one", "director-two"])
        for entry in notify.call_args_list:
            self.assertIn("codex1", str(cast(object, entry.args[1])))
            self.assertIn("~/.codex1", str(cast(object, entry.args[1])))

    def test_swap_stops_unused_old_server_immediately_without_starting_a_watcher(self) -> None:
        home = self.select()
        self.record(self.home / ".codex")
        with patch.object(codex_mesh, "SWEEP_ROOTS", (self.session_dir.parent,)), patch.object(codex_mesh, "_stop_unused", return_value=True) as stop, patch.object(codex_mesh, "_start_watcher") as watch, patch.object(codex_mesh, "_notify_holder"):
            _ = codex_mesh.retire_workers_servers(home, "codex1")
        stop.assert_called_once_with(None, os.getpid(), 4001, codex_mesh.RetiredRunServer(self.session_dir))
        watch.assert_not_called()

    def test_ensure_server_stops_an_unused_retired_server_before_starting_target(self) -> None:
        home = self.select()
        self.record(self.home / ".codex")
        order: list[str] = []
        def stop(_root: Path | None, _pid: int, _port: int) -> bool:
            order.append("stop")
            return True
        def start(_session_dir: str, _home: Path) -> int:
            order.append("start")
            return 4002
        with patch.object(codex_mesh, "_stop_unused_with_lock_held", side_effect=stop), patch.object(codex_mesh, "_start_server", side_effect=start), patch.object(codex_mesh, "_start_watcher") as watch:
            self.assertEqual(codex_mesh.ensure_server(str(self.session_dir), home), (4002, True))
        self.assertEqual(order, ["stop", "start"])
        watch.assert_not_called()

    def test_dead_retired_writer_counts_as_released_even_when_the_connection_fails(self) -> None:
        _ = (self.session_dir / codex_mesh.RETIRED_FILE).write_text(json.dumps({"servers": [{"port": 4001, "pid": 123456}]}))
        with patch.object(codex_mesh, "_run_servers", return_value=[4001]), patch.object(codex_mesh, "_pid_alive", side_effect=[True, False]), patch.object(codex_mesh, "_stop_unused", return_value=False), patch.object(codex_mesh, "Client", side_effect=ConnectionError("server exited")):
            result = codex_mesh._release_other_worker_writers(str(self.session_dir), "worker", "thread-1", 4002)  # pyright: ignore[reportPrivateUsage]
        self.assertNotIsInstance(result, (codex_mesh.WorkerThreadStateUnknown, codex_mesh.WorkerWaitingOnLiveTurn))

    def test_dead_record_on_reused_port_does_not_confirm_live_server_is_gone(self) -> None:
        for current in (False, True):
            with self.subTest(current=current):
                records = [{"port": 4001, "pid": 123456}]
                if current:
                    self.record(self.home / ".codex")
                else:
                    records.append({"port": 4001, "pid": os.getpid()})
                _ = (self.session_dir / codex_mesh.RETIRED_FILE).write_text(json.dumps({"servers": records}))
                with patch.object(codex_mesh, "_pid_alive", side_effect=self.writer_process_is_live):
                    self.assertFalse(codex_mesh._recorded_server_gone(str(self.session_dir), 4001))  # pyright: ignore[reportPrivateUsage]

    def test_live_writer_on_reused_port_is_probed_despite_an_earlier_dead_record(self) -> None:
        class LiveWriter:
            port: int = 4001
            @bounded_fixture
            def call(self, method: str, _params: dict[str, object], **_options: object) -> codex_mesh.RpcMessage:
                if method == "thread/loaded/list":
                    return {"result": {"data": ["thread-1"]}}
                if method == "thread/read":
                    return {"result": {"thread": {"status": {"type": "active"}, "turns": []}}}
                if method == "thread/turns/list":
                    return {"result": {"data": [{"id": "turn-1", "status": "inProgress"}]}}
                raise AssertionError(f"Live writer must not receive {method}")
            def close(self) -> None:
                pass

        for current in (False, True):
            with self.subTest(current=current):
                records = [{"port": 4001, "pid": 123456}]
                if current:
                    self.record(self.home / ".codex")
                else:
                    records.append({"port": 4001, "pid": os.getpid()})
                _ = (self.session_dir / codex_mesh.RETIRED_FILE).write_text(json.dumps({"servers": records}))
                with patch.object(codex_mesh, "_pid_alive", side_effect=self.writer_process_is_live), patch.object(codex_mesh, "_stop_unused", return_value=False) as stop, patch.object(codex_mesh, "Client", return_value=LiveWriter()) as probe:
                    result = codex_mesh._release_other_worker_writers(str(self.session_dir), "worker", "thread-1", 4002)  # pyright: ignore[reportPrivateUsage]
                self.assertIsInstance(result, codex_mesh.WorkerWaitingOnLiveTurn)
                probe.assert_called_once_with(4001, "release-worker")
                if current:
                    stop.assert_not_called()
                else:
                    stop.assert_called_once_with(None, os.getpid(), 4001, codex_mesh.RetiredRunServer(self.session_dir))

    def test_live_record_on_reused_port_keeps_failed_probe_unconfirmed(self) -> None:
        _ = (self.session_dir / codex_mesh.RETIRED_FILE).write_text(json.dumps({"servers": [
            {"port": 4001, "pid": 123456}, {"port": 4001, "pid": os.getpid()},
        ]}))
        with patch.object(codex_mesh, "_pid_alive", side_effect=self.writer_process_is_live), patch.object(codex_mesh, "_stop_unused", return_value=False), patch.object(codex_mesh, "Client", side_effect=ConnectionError("live server could not be reached")):
            result = codex_mesh._release_other_worker_writers(str(self.session_dir), "worker", "thread-1", 4002)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(result, codex_mesh.WorkerThreadStateUnknown)

    def test_loaded_idle_thread_on_reused_port_waits_for_live_writer_release(self) -> None:
        calls: list[str] = []
        elapsed = [0.0]
        class HeldWriter:
            port: int = 4001
            @bounded_fixture
            def call(self, method: str, _params: dict[str, object], **_options: object) -> codex_mesh.RpcMessage:
                calls.append(method)
                if method == "thread/loaded/list":
                    return {"result": {"data": ["thread-1"]}}
                if method == "thread/read":
                    return {"result": {"thread": {"status": {"type": "idle"}}}}
                if method == "thread/queue/list":
                    return {"result": {"data": []}}
                if method == "thread/unsubscribe":
                    return {"result": {"status": "notSubscribed"}}
                raise AssertionError(f"Unreleased writer must not receive {method}")
            @bounded_fixture
            def next_frame(self, _deadline: float) -> codex_mesh.RpcMessage | None:
                elapsed[0] += 1.0
                return None
            def close(self) -> None:
                pass
        _ = (self.session_dir / codex_mesh.RETIRED_FILE).write_text(json.dumps({"servers": [
            {"port": 4001, "pid": 123456}, {"port": 4001, "pid": os.getpid()},
        ]}))
        with patch.object(codex_mesh, "_pid_alive", side_effect=self.writer_process_is_live), patch.object(codex_mesh, "_stop_unused", return_value=False), patch.object(codex_mesh, "Client", return_value=HeldWriter()), patch.object(codex_mesh, "THREAD_RELEASE_TIMEOUT_SECS", 0.01), patch.object(time, "monotonic", new=bounded_fixture(lambda: elapsed[0])):
            result = codex_mesh._release_other_worker_writers(str(self.session_dir), "worker", "thread-1", 4002)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(result, codex_mesh.WorkerThreadStateUnknown)
        self.assertIn("thread/unsubscribe", calls)

    def test_live_peer_on_retired_server_names_run_and_worker_until_turn_ends(self) -> None:
        elapsed = [0.0]
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "worker": {"thread_id": "idle-thread", "status": "done"},
            "peer": {"thread_id": "busy-thread", "turn_id": "busy-turn", "status": "running"},
        }))
        class HeldClient:
            port: int = 4001
            @bounded_fixture
            def call(self, method: str, params: dict[str, object], **_options: object) -> codex_mesh.RpcMessage:
                if method == "thread/loaded/list":
                    return {"result": {"data": ["idle-thread", "busy-thread"]}}
                if method == "thread/read":
                    return {"result": {"thread": {"status": {"type": "active" if params["threadId"] == "busy-thread" else "idle"}}}}
                if method == "thread/queue/list":
                    return {"result": {"data": []}}
                return {"result": {}}
            @bounded_fixture
            def next_frame(self, _deadline: float) -> codex_mesh.RpcMessage | None:
                elapsed[0] += 1.0
                return None
            def close(self) -> None:
                pass
        with patch.object(codex_mesh, "_run_servers", return_value=[4001]), patch.object(codex_mesh, "Client", return_value=HeldClient()), patch.object(codex_mesh, "THREAD_RELEASE_TIMEOUT_SECS", 0.01), patch.object(time, "monotonic", new=bounded_fixture(lambda: elapsed[0])):
            result = codex_mesh._release_other_worker_writers(str(self.session_dir), "worker", "idle-thread", 4002)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(result, codex_mesh.WorkerThreadStateUnknown)
        assert isinstance(result, codex_mesh.WorkerThreadStateUnknown)
        self.assertIn(self.session_dir.name, result.reason)
        self.assertIn("peer", result.reason)
        self.assertIn("once that turn ends", result.reason)

    def test_both_notices_use_the_shared_user_zone(self) -> None:
        self.mark_live_holder()
        home = self.select()
        root = self.session_dir.parent
        for zone in (ZoneInfo("Asia/Tokyo"), None):
            with self.subTest(zone=zone):
                self.record(home)
                with patch.dict(os.environ, {"CODEX_HOME": str(home)}), patch.object(codex_mesh, "user_zone", return_value=zone), patch.object(codex_mesh, "_sign_in_changed_at", return_value=2000), patch.object(codex_mesh, "_server_started_at", return_value=1000), patch.object(codex_mesh, "_stop_unused", return_value=True), contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(codex_mesh.command_signin_changed(argparse.Namespace(root=str(root))), 0)
                self.assertIn(datetime.fromtimestamp(2000).astimezone(zone).strftime("%H:%M %Z"), output.getvalue())
                self.record(self.home / ".codex")
                with patch.object(codex_mesh, "SWEEP_ROOTS", (root,)), patch.object(codex_mesh, "user_zone", return_value=zone), patch.object(codex_mesh, "_stop_unused", return_value=True):
                    lines = codex_mesh.retire_workers_servers(home, "codex1")
                self.assertTrue(lines)
                self.assertIn(datetime.now().astimezone(zone).strftime("%H:%M %Z"),
                              (self.session_dir / codex_mesh.SERVER_LOG).read_text())

    def test_swap_dry_run_lists_runs_without_retiring_or_notifying(self) -> None:
        home = self.select()
        self.record(self.home / ".codex")
        before = (self.session_dir / codex_mesh.SERVER_FILE).read_bytes()
        with patch.object(codex_mesh, "SWEEP_ROOTS", (self.session_dir.parent,)), patch.object(codex_mesh, "_start_watcher") as watch, patch.object(codex_mesh, "_notify_holder") as notify:
            lines = codex_mesh.retire_workers_servers(home, "codex1", dry_run=True)
        self.assertEqual((self.session_dir / codex_mesh.SERVER_FILE).read_bytes(), before)
        self.assertFalse((self.session_dir / codex_mesh.RETIRED_FILE).exists())
        self.assertTrue(lines)
        self.assertEqual([line.folder for line in lines], [self.session_dir])
        watch.assert_not_called()
        notify.assert_not_called()

    def test_swap_returns_worker_states_and_marker_holders_while_preserving_notices(self) -> None:
        home = self.select()
        root = self.session_dir.parent
        active = root / "active"
        active.mkdir()
        _ = (active / "holder-id").write_text(str(self.session_dir) + "\n")
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "project-impl": {"thread_id": "a", "status": "done"},
            "project-review": {"thread_id": "b", "status": "running", "turn_id": "turn-b"},
            "project-ended": {"thread_id": "c", "status": "ended"},
        }))
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                self.record(self.home / ".codex")
                with (patch.object(codex_mesh, "SWEEP_ROOTS", (root,)),
                      patch.object(codex_mesh, "_stop_unused", return_value=True),
                      patch.object(codex_mesh, "_notify_holder") as notify,
                      patch.object(codex_mesh, "_interrupt_worker_for_account_move", return_value=
                          codex_mesh.SwitchingWorker("project-review", True, codex_mesh.WorkerMoveResumed()))):
                    result = codex_mesh.retire_workers_servers(home, "codex1", dry_run=dry_run)
                self.assertEqual(result, (codex_mesh.WorkersRunSwitch(self.session_dir, ("holder-id",), (
                    codex_mesh.SwitchingWorker("project-impl", False),
                    codex_mesh.SwitchingWorker("project-review", True,
                        codex_mesh.WorkerMoveReady() if dry_run else codex_mesh.WorkerMoveResumed()),
                )),))
                if dry_run:
                    notify.assert_not_called()
                else:
                    self.assertEqual(notify.call_args.args[0], "holder-id")
                    notice = cast(str, notify.call_args.args[1])
                    self.assertIn("running workers were interrupted and resumed on the new account", notice)
                    self.assertNotIn("working workers finish first", notice)
                    self.assertEqual(notify.call_args.args[2], codex_mesh.CodexNoticeKind.WORKERS_CHANGED)
                    self.assertIn(self.session_dir.name, notice)
                    log = (self.session_dir / codex_mesh.SERVER_LOG).read_text()
                    self.assertIn("interrupted and resumed on the new account", log)
                    self.assertNotIn("working worker finishes first", log)


@final
class DevShellServerTests(unittest.TestCase):
    """A unit's shared server owns the dev-shell selected by its turn's cwd."""

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.session_dir = Path()
        self.worktree = Path()
        self.workers_home = Path()
        self.watcher = MagicMock()
        self.stop = MagicMock()
        self.shell_load = MagicMock()
        self.process = MagicMock(spec=subprocess.Popen)
        self.launch = MagicMock()

    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.session_dir = root / "runs" / "unit"
        self.session_dir.mkdir(parents=True)
        # Spaces and shell metacharacters must remain one argv element.
        self.worktree = root / "worktree with spaces;literal"
        self.worktree.mkdir()
        _ = (self.worktree / ".envrc").write_text("use flake\n", encoding="utf-8")
        self.workers_home = root / "workers"
        self.workers_home.mkdir()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "PATH": "/caller/bin", "CODEX_HOME": "/caller/home",
            "UNIT_RUN_DIR": "/caller/run", "TMPDIR": str(root),
            "NIX_LDFLAGS": "caller linker flags",
        }, clear=True)))
        _ = self.enterContext(patch.object(codex_mesh, "RUN_ROOTS", (self.session_dir.parent,)))
        _ = self.enterContext(patch.object(codex_mesh, "_design_context", return_value={}))
        _ = self.enterContext(patch.object(codex_mesh, "_holds_old_sign_in", return_value=False))
        _ = self.enterContext(patch.object(codex_mesh, "_pid_alive", return_value=True))
        _ = self.enterContext(patch.object(codex_mesh, "_free_port", return_value=4321))
        _ = self.enterContext(patch.object(codex_mesh, "WebSocket"))
        self.watcher = cast(MagicMock, self.enterContext(patch.object(codex_mesh, "_start_watcher")))
        self.stop = cast(MagicMock, self.enterContext(patch.object(codex_mesh, "_stop_unused_with_lock_held", return_value=False)))

        def load_shell(directory: Path, environment: Mapping[str, str]) -> nix_env.Loaded:
            loaded = {**environment, "IN_NIX_SHELL": "impure", "NIX_LDFLAGS": "nix linker flags",
                      "PKG_CONFIG_PATH": "/nix/pkg", "PATH": "/nix/bin"}
            for key in ("CODEX_HOME", "UNIT_RUN_DIR", "TMPDIR", "CLAUDE_CODE_SESSION_ID",
                        "HELD_DESIGN_ID", "DESIGN_BINDING_GENERATION"):
                loaded[key] = "envrc must not choose this"
            return nix_env.Loaded(loaded, directory / ".envrc")

        self.shell_load = cast(MagicMock, self.enterContext(patch.object(nix_env, "ensure", side_effect=load_shell)))
        self.process = MagicMock(spec=subprocess.Popen)
        self.process.pid = 87654
        cast(MagicMock, self.process.poll).return_value = None
        self.launch = cast(MagicMock, self.enterContext(patch.object(subprocess, "Popen", return_value=self.process)))
        self.addCleanup(codex_mesh._STARTED_SERVERS.discard, (4321, 87654))  # pyright: ignore[reportPrivateUsage]

    def ensure(self) -> tuple[int, bool]:
        return codex_mesh.ensure_server(str(self.session_dir), self.workers_home, cwd=str(self.worktree))

    def record(self) -> dict[str, object]:
        return _as_test_dict(cast(object, json.loads((self.session_dir / codex_mesh.SERVER_FILE).read_text(encoding="utf-8"))))

    def live_record(self, **extra: object) -> None:
        _ = (self.session_dir / codex_mesh.SERVER_FILE).write_text(json.dumps({
            "pid": 76543, "port": 4001, "codex_home": str(self.workers_home),
            "auto_compact_tokens": codex_mesh.AUTO_COMPACT_TOKENS, **extra,
        }), encoding="utf-8")

    def assert_plain_launch(self) -> None:
        self.assertEqual(self.ensure(), (4321, True))
        command = cast(list[str], self.launch.call_args.args[0])
        self.assertEqual(command, [
            "codex", "-c", "thread_unload_delay_secs=0", "-c",
            f"model_auto_compact_token_limit={codex_mesh.AUTO_COMPACT_TOKENS}", "-c",
            f"background_terminal_max_timeout={codex_mesh.BACKGROUND_TERMINAL_MAX_TIMEOUT_MS}",
            "app-server", "--listen", "ws://127.0.0.1:4321",
        ])
        environment = cast(dict[str, str], self.launch.call_args.kwargs["env"])
        self.assertEqual(environment["CODEX_HOME"], str(self.workers_home))
        self.assertEqual(environment["NIX_LDFLAGS"], "caller linker flags")
        self.assertFalse(self.record().get("dev_shell_worktree"))

    def test_loaded_worktree_launches_codex_directly_and_records_its_pid(self) -> None:
        self.assertEqual(self.ensure(), (4321, True))
        self.launch.assert_called_once()
        command = cast(list[str], self.launch.call_args.args[0])
        self.assertEqual(command[0], "codex")
        self.assertEqual(command.count("app-server"), 1)
        self.assertEqual(command[-3:], ["app-server", "--listen", "ws://127.0.0.1:4321"])
        self.assertTrue(self.launch.call_args.kwargs["start_new_session"])
        self.assertEqual(self.record()["dev_shell_worktree"], str(self.worktree))
        self.assertEqual(self.record()["pid"], 87654)
        self.watcher.assert_called_once_with(str(self.session_dir), 87654, 4321)
        self.shell_load.assert_called_once()
        self.assertEqual(self.shell_load.call_args.args[0], self.worktree)
        environment = cast(dict[str, str], self.launch.call_args.kwargs["env"])
        self.assertEqual(environment["IN_NIX_SHELL"], "impure")
        self.assertEqual(environment["NIX_LDFLAGS"], "nix linker flags")
        self.assertEqual(environment["PKG_CONFIG_PATH"], "/nix/pkg")

    def test_run_owned_values_are_reapplied_after_direnv_without_overwriting_dev_shell_flags(self) -> None:
        _ = self.ensure()
        command = cast(list[str], self.launch.call_args.args[0])
        self.assertEqual(command[0], "codex")
        environment = cast(dict[str, str], self.launch.call_args.kwargs["env"])
        self.assertEqual(environment["CODEX_HOME"], str(self.workers_home))
        self.assertEqual(environment["UNIT_RUN_DIR"], str(self.session_dir))
        self.assertEqual(environment["TMPDIR"], str(self.session_dir / "tmp"))
        self.assertEqual(environment["NIX_LDFLAGS"], "nix linker flags")
        self.assertEqual(environment["PATH"], "/nix/bin")
        for key in ("CLAUDE_CODE_SESSION_ID", "HELD_DESIGN_ID", "DESIGN_BINDING_GENERATION"):
            self.assertNotIn(key, environment)
        caller = cast(dict[str, str], self.shell_load.call_args.args[1])
        self.assertEqual(caller["NIX_LDFLAGS"], "caller linker flags")

    def test_no_envrc_keeps_the_existing_launch(self) -> None:
        (self.worktree / ".envrc").unlink()
        self.assert_plain_launch()
        self.shell_load.assert_not_called()

    def test_a_shared_mesh_does_not_load_a_callers_worktree(self) -> None:
        with patch.object(codex_mesh, "RUN_ROOTS", ()):
            self.assert_plain_launch()
        self.shell_load.assert_not_called()

    def test_a_caller_without_cwd_starts_the_existing_plain_server(self) -> None:
        self.assertEqual(codex_mesh.ensure_server(str(self.session_dir), self.workers_home), (4321, True))
        self.assertEqual(cast(list[str], self.launch.call_args.args[0])[0], "codex")
        self.shell_load.assert_not_called()

    def test_legacy_and_other_worktree_servers_are_retired_before_the_new_shell_is_started(self) -> None:
        for extra in ({}, {"dev_shell_worktree": str(self.worktree.parent / "old-tree")}):
            with self.subTest(extra=extra):
                self.live_record(**extra)
                self.stop.reset_mock()
                self.assertEqual(self.ensure(), (4321, True))
                retired = _as_test_dict(cast(object, json.loads((self.session_dir / codex_mesh.RETIRED_FILE).read_text())))
                self.assertIn(4001, [_as_test_dict(record).get("port")
                                     for record in cast(list[object], retired["servers"])])
                self.stop.assert_called_once_with(None, 76543, 4001)
                self.assertEqual(self.record()["dev_shell_worktree"], str(self.worktree))

    def test_the_same_dev_shell_reuses_the_server(self) -> None:
        self.live_record(dev_shell_worktree=str(self.worktree))
        self.assertEqual(self.ensure(), (4001, False))
        self.launch.assert_not_called()
        self.stop.assert_not_called()

    def test_no_cwd_never_retires_a_dev_shell_server(self) -> None:
        self.live_record(dev_shell_worktree=str(self.worktree))
        self.assertEqual(codex_mesh.ensure_server(str(self.session_dir), self.workers_home), (4001, False))
        self.shell_load.assert_not_called()
        self.launch.assert_not_called()
        self.stop.assert_not_called()

    def test_dev_shell_server_can_start_after_thirty_seconds(self) -> None:
        elapsed = [0.0]
        probes: list[float] = []

        @bounded_fixture
        def advance(seconds: float) -> None:
            elapsed[0] += seconds

        def connect(_port: int) -> SimpleNamespace:
            probes.append(elapsed[0])
            if elapsed[0] < 45:
                raise ConnectionRefusedError("app-server is still starting")
            return SimpleNamespace(close=lambda: None)

        clock = SimpleNamespace(time=bounded_fixture(lambda: elapsed[0]), sleep=advance)
        with patch.object(codex_mesh, "time", clock), patch.object(codex_mesh, "WebSocket", side_effect=connect):
            self.assertEqual(self.ensure(), (4321, True))
        self.assertGreater(probes[-1], 30)
        self.assertEqual(self.record()["pid"], 87654)
        cast(MagicMock, self.process.terminate).assert_not_called()

    def test_a_plain_start_still_times_out_after_thirty_seconds(self) -> None:
        elapsed = [0.0]

        @bounded_fixture
        def advance(seconds: float) -> None:
            elapsed[0] += seconds

        clock = SimpleNamespace(time=bounded_fixture(lambda: elapsed[0]), sleep=advance)
        with (patch.object(codex_mesh, "time", clock),
              patch.object(codex_mesh, "WebSocket", side_effect=ConnectionRefusedError),
              patch.object(os, "killpg")):
            with self.assertRaisesRegex(SystemExit, "did not accept connections"):
                _ = codex_mesh.ensure_server(str(self.session_dir), self.workers_home)
        self.assertGreaterEqual(elapsed[0], 30)
        self.assertLess(elapsed[0], 31)
        self.assertFalse((self.session_dir / codex_mesh.SERVER_FILE).exists())

    def test_mac_sweep_keeps_a_slow_dev_shell_server_before_its_current_record_is_written(self) -> None:
        elapsed = [0.0]
        recorded_ages: list[float] = []
        record_started = codex_mesh._record_started_server  # pyright: ignore[reportPrivateUsage]

        @bounded_fixture
        def advance(seconds: float) -> None:
            elapsed[0] += seconds

        def connect(_port: int) -> SimpleNamespace:
            if elapsed[0] < 45:
                raise ConnectionRefusedError("app-server is still starting")
            return SimpleNamespace(close=lambda: None)

        def sweep_before_record(session_dir: str, served: codex_mesh.ServerRecord) -> None:
            recorded_ages.append(elapsed[0])
            self.assertFalse((self.session_dir / codex_mesh.SERVER_FILE).exists())
            with (patch.object(sys, "platform", "darwin"),
                  patch.object(codex_mesh, "_running_servers", return_value=[(87654, 4321, elapsed[0])]),
                  patch.object(codex_mesh, "_client_counts", return_value={}),
                  patch.object(codex_mesh, "_server_folder", return_value=codex_mesh.NonMeshServerStderr()),
                  patch.object(codex_mesh, "_busy_threads", return_value=()) as conversations,
                  contextlib.redirect_stdout(io.StringIO()) as output):
                self.assertEqual(codex_mesh.command_sweep(argparse.Namespace(
                    root=str(self.session_dir.parent), stop=True)), 0)
            self.assertIn("87654\t4321\tin use\t", output.getvalue())
            conversations.assert_not_called()
            self.stop.assert_not_called()
            record_started(session_dir, served)

        clock = SimpleNamespace(time=bounded_fixture(lambda: elapsed[0]), sleep=advance)
        with (patch.object(codex_mesh, "time", clock),
              patch.object(codex_mesh, "WebSocket", side_effect=connect),
              patch.object(codex_mesh, "_record_started_server", side_effect=sweep_before_record)):
            self.assertEqual(self.ensure(), (4321, True))
        self.assertEqual(len(recorded_ages), 1)
        self.assertGreater(recorded_ages[0], 30)
        self.assertEqual(self.record()["pid"], 87654)

    def test_failed_dev_shell_server_falls_back_and_logs_the_failure(self) -> None:
        self.live_record()
        failed = MagicMock()
        failed.pid = 87653
        cast(MagicMock, failed.poll).return_value = 1
        self.launch.side_effect = [failed, self.process]
        with patch.object(os, "killpg") as kill_group:
            self.assertEqual(self.ensure(), (4321, True))
        kill_group.assert_any_call(87653, signal.SIGTERM)
        commands = [cast(list[str], call.args[0]) for call in self.launch.call_args_list]
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[0][0], "codex")
        self.assertEqual(commands[1][0], "codex")
        self.assertFalse(self.record().get("dev_shell_worktree"))
        self.assertEqual(self.record()["pid"], 87654)
        self.stop.assert_called_once_with(None, 76543, 4001)
        self.watcher.assert_any_call(str(self.session_dir), 87654, 4321)
        log = (self.session_dir / codex_mesh.SERVER_LOG).read_text().lower()
        self.assertIn("dev-shell app-server", log)
        self.assertRegex(log, r"failed|exited")
        self.assertRegex(log, r"plain|fall(?:ing)? back|fallback")
        (self.session_dir / codex_mesh.SERVER_FILE).unlink()
        self.launch.side_effect = None
        self.launch.reset_mock()
        self.assertEqual(codex_mesh.ensure_server(str(self.session_dir), self.workers_home), (4321, True))
        self.assertEqual(cast(list[str], self.launch.call_args.args[0])[0], "codex")

    def test_codex_missing_from_loaded_path_falls_back(self) -> None:
        self.launch.side_effect = [FileNotFoundError("codex missing from loaded PATH"), self.process]
        self.assertEqual(self.ensure(), (4321, True))
        self.assertEqual(self.launch.call_count, 2)
        self.assertEqual(cast(list[str], self.launch.call_args.args[0])[0], "codex")
        self.assertFalse(self.record().get("dev_shell_worktree"))

    def start_plain_fallback_after_evaluation_failure(self) -> None:
        failed = MagicMock()
        failed.pid = 87653
        cast(MagicMock, failed.poll).return_value = 1
        self.launch.side_effect = [failed, self.process]
        with patch.object(os, "killpg"):
            self.assertEqual(self.ensure(), (4321, True))
        self.assertEqual(self.launch.call_count, 2)
        self.assertFalse(self.record().get("dev_shell_worktree"))
        self.launch.side_effect = None
        self.launch.reset_mock()

    def test_a_cwd_caller_reuses_the_plain_fallback_for_an_unchanged_failed_shell(self) -> None:
        epoch = 100_000.0
        with patch("scripts.agents.codex_mesh.time.time", new=bounded_fixture(lambda: epoch)):
            self.start_plain_fallback_after_evaluation_failure()
            self.assertEqual(self.ensure(), (4321, False))
            epoch = 101_799.0
            self.assertEqual(self.ensure(), (4321, False))
        self.launch.assert_not_called()
        self.stop.assert_not_called()
        self.assertFalse(self.record().get("dev_shell_worktree"))

    def test_editing_a_nix_input_retries_the_failed_dev_shell_without_editing_envrc(self) -> None:
        for name in ("flake.nix", "flake.lock", "shell.nix", "default.nix", "devenv.nix"):
            with self.subTest(input=name):
                source = self.worktree / name
                _ = source.write_text("broken configuration\n", encoding="utf-8")
                (self.session_dir / codex_mesh.SERVER_FILE).unlink(missing_ok=True)
                self.launch.reset_mock()
                self.start_plain_fallback_after_evaluation_failure()
                envrc_before = (self.worktree / ".envrc").stat()
                previous = source.stat()
                _ = source.write_text("repaired configuration\n", encoding="utf-8")
                os.utime(source, ns=(previous.st_atime_ns, previous.st_mtime_ns + 2_000_000_000))
                self.assertEqual(self.ensure(), (4321, True))
                self.launch.assert_called_once()
                self.assertEqual(cast(list[str], self.launch.call_args.args[0])[0], "codex")
                self.assertEqual(self.record()["dev_shell_worktree"], str(self.worktree))
                self.assertEqual((self.worktree / ".envrc").stat(), envrc_before)

    def test_creating_a_previously_absent_nix_input_retries_the_failed_dev_shell(self) -> None:
        for name in ("flake.nix", "flake.lock", "shell.nix", "default.nix", "devenv.nix"):
            with self.subTest(input=name):
                (self.session_dir / codex_mesh.SERVER_FILE).unlink(missing_ok=True)
                self.launch.reset_mock()
                self.start_plain_fallback_after_evaluation_failure()
                _ = (self.worktree / name).write_text("new configuration\n", encoding="utf-8")
                self.assertEqual(self.ensure(), (4321, True))
                self.launch.assert_called_once()
                self.assertEqual(cast(list[str], self.launch.call_args.args[0])[0], "codex")
                self.assertEqual(self.record()["dev_shell_worktree"], str(self.worktree))

    def test_removing_a_nix_input_retries_the_failed_dev_shell(self) -> None:
        for name in ("flake.nix", "flake.lock", "shell.nix", "default.nix", "devenv.nix"):
            with self.subTest(input=name):
                source = self.worktree / name
                _ = source.write_text("obsolete configuration\n", encoding="utf-8")
                (self.session_dir / codex_mesh.SERVER_FILE).unlink(missing_ok=True)
                self.launch.reset_mock()
                self.start_plain_fallback_after_evaluation_failure()
                source.unlink()
                self.assertEqual(self.ensure(), (4321, True))
                self.launch.assert_called_once()
                self.assertEqual(cast(list[str], self.launch.call_args.args[0])[0], "codex")
                self.assertEqual(self.record()["dev_shell_worktree"], str(self.worktree))

    def test_a_failure_older_than_thirty_minutes_allows_an_unchanged_dev_shell_to_retry(self) -> None:
        epoch = 100_000.0
        with patch("scripts.agents.codex_mesh.time.time", new=bounded_fixture(lambda: epoch)):
            self.start_plain_fallback_after_evaluation_failure()
            epoch = 101_801.0
            self.assertEqual(self.ensure(), (4321, True))
        self.launch.assert_called_once()
        self.assertEqual(cast(list[str], self.launch.call_args.args[0])[0], "codex")
        self.assertEqual(self.record()["dev_shell_worktree"], str(self.worktree))
        self.stop.assert_called_once_with(None, 87654, 4321)

    def test_a_failed_shell_is_not_retried_after_the_live_server_record_is_removed(self) -> None:
        self.start_plain_fallback_after_evaluation_failure()
        (self.session_dir / codex_mesh.SERVER_FILE).unlink()
        self.assertEqual(self.ensure(), (4321, True))
        self.assertEqual(cast(list[str], self.launch.call_args.args[0])[0], "codex")
        self.assertFalse(self.record().get("dev_shell_worktree"))
        self.launch.reset_mock()
        self.assertEqual(self.ensure(), (4321, False))
        self.launch.assert_not_called()

    def test_editing_the_failed_envrc_allows_a_new_dev_shell_attempt(self) -> None:
        self.start_plain_fallback_after_evaluation_failure()
        envrc = self.worktree / ".envrc"
        previous = envrc.stat()
        _ = envrc.write_text("use flake ./fixed\n", encoding="utf-8")
        os.utime(envrc, ns=(previous.st_atime_ns, previous.st_mtime_ns + 2_000_000_000))
        self.assertEqual(self.ensure(), (4321, True))
        self.launch.assert_called_once()
        self.assertEqual(cast(list[str], self.launch.call_args.args[0])[0], "codex")
        self.assertEqual(self.record()["dev_shell_worktree"], str(self.worktree))
        self.stop.assert_called_once_with(None, 87654, 4321)

    def test_another_worktree_can_load_even_when_its_envrc_has_the_failed_files_mtime(self) -> None:
        self.start_plain_fallback_after_evaluation_failure()
        another = self.worktree.parent / "another-worktree"
        another.mkdir()
        envrc = another / ".envrc"
        _ = envrc.write_text("use flake\n", encoding="utf-8")
        failed_mtime = (self.worktree / ".envrc").stat().st_mtime_ns
        os.utime(envrc, ns=(failed_mtime, failed_mtime))
        self.assertEqual(codex_mesh.ensure_server(
            str(self.session_dir), self.workers_home, cwd=str(another)), (4321, True))
        self.assertEqual(cast(list[str], self.launch.call_args.args[0])[0], "codex")
        self.assertEqual(self.record()["dev_shell_worktree"], str(another))

    def test_a_failed_plain_fallback_is_reported_without_retrying_forever(self) -> None:
        cast(MagicMock, self.process.poll).return_value = 1
        with patch.object(os, "killpg"):
            with self.assertRaisesRegex(SystemExit, "exited immediately"):
                _ = self.ensure()
        self.assertEqual(self.launch.call_count, 2)
        self.assertEqual(cast(list[str], self.launch.call_args.args[0])[0], "codex")
        self.assertFalse((self.session_dir / codex_mesh.SERVER_FILE).exists())

    def test_definite_absence_of_an_envrc_retires_a_dev_shell_server(self) -> None:
        self.live_record(dev_shell_worktree=str(self.worktree))
        (self.worktree / ".envrc").unlink()
        self.assert_plain_launch()
        self.stop.assert_called_once_with(None, 76543, 4001)

    def test_a_subdirectory_starts_and_reuses_its_nearest_ancestor_dev_shell(self) -> None:
        subdirectory = self.worktree / "src" / "nested"
        subdirectory.mkdir(parents=True)
        self.assertEqual(codex_mesh.ensure_server(
            str(self.session_dir), self.workers_home, cwd=str(subdirectory)), (4321, True))
        self.assertEqual(cast(list[str], self.launch.call_args.args[0])[0], "codex")
        self.assertEqual(self.record()["dev_shell_worktree"], str(self.worktree))
        self.launch.reset_mock()
        self.assertEqual(codex_mesh.ensure_server(
            str(self.session_dir), self.workers_home, cwd=str(subdirectory)), (4321, False))
        self.assertEqual(self.ensure(), (4321, False))
        self.launch.assert_not_called()
        self.stop.assert_not_called()

    def test_cwd_free_restart_remembers_the_successfully_loaded_dev_shell_after_idle_stop(self) -> None:
        self.assertEqual(self.ensure(), (4321, True))
        (self.session_dir / codex_mesh.SERVER_FILE).unlink()
        self.launch.reset_mock()
        self.assertEqual(codex_mesh.ensure_server(str(self.session_dir), self.workers_home), (4321, True))
        self.assertEqual(cast(list[str], self.launch.call_args.args[0])[0], "codex")
        self.assertEqual(self.record()["dev_shell_worktree"], str(self.worktree))
        self.assertEqual(self.ensure(), (4321, False))
        self.launch.assert_called_once()

    def test_reusing_a_dev_shell_record_remembers_it_for_a_later_cwd_free_restart(self) -> None:
        self.live_record(dev_shell_worktree=str(self.worktree))
        self.assertEqual(self.ensure(), (4001, False))
        (self.session_dir / codex_mesh.SERVER_FILE).unlink()
        self.assertEqual(codex_mesh.ensure_server(str(self.session_dir), self.workers_home), (4321, True))
        self.assertEqual(cast(list[str], self.launch.call_args.args[0])[0], "codex")

    def test_failed_start_terminates_the_server_process_group_and_escalates(self) -> None:
        cast(MagicMock, self.process.wait).side_effect = [subprocess.TimeoutExpired("codex", 5), 0]
        with (patch.object(codex_mesh, "SERVER_START_TIMEOUT_SECS", 0),
              patch.object(os, "killpg") as kill_group):
            with self.assertRaises(SystemExit):
                _ = codex_mesh.ensure_server(str(self.session_dir), self.workers_home)
        self.assertEqual([call.args for call in kill_group.call_args_list],
                         [(87654, signal.SIGTERM), (87654, signal.SIGKILL)])
        self.assertFalse((self.session_dir / codex_mesh.SERVER_FILE).exists())

    def test_held_design_identity_is_also_applied_after_the_dev_shell(self) -> None:
        context: dict[str, object] = {
            "holder_session": "design-holder", "design_id": "design-one", "generation": 7,
            "binding": {"kind": "DesignBoundToPhase", "run_directory": str(self.session_dir)},
        }
        with patch.object(codex_mesh, "_design_context", return_value=context), patch.object(design_roles, "refresh_census"):
            _ = self.ensure()
        command = cast(list[str], self.launch.call_args.args[0])
        self.assertEqual(command[0], "codex")
        environment = cast(dict[str, str], self.launch.call_args.kwargs["env"])
        self.assertEqual(environment["CLAUDE_CODE_SESSION_ID"], "design-holder")
        self.assertEqual(environment["HELD_DESIGN_ID"], "design-one")
        self.assertEqual(environment["DESIGN_BINDING_GENERATION"], "7")
        self.assertEqual(environment["IN_NIX_SHELL"], "impure")

    def test_start_forwards_the_worktree_on_the_initial_launch_and_recovery(self) -> None:
        args = argparse.Namespace(
            session_dir=str(self.session_dir), name="worker", cwd=str(self.worktree), resident=False,
            log_file=str(self.session_dir / "worker.log"),
        )
        outcomes = [codex_mesh.FailedWithThread(THREAD_ID, STALE_SIGN_IN, 1.0), codex_mesh.RunCompleted()]
        with (patch.object(codex_mesh, "ensure_server", side_effect=[(4001, False), (4321, True)]) as ensure,
              patch.object(codex_mesh, "_retire_server", return_value=True),
              patch.object(codex_mesh, "_run_worker", side_effect=outcomes),
              contextlib.redirect_stderr(io.StringIO())):
            self.assertEqual(codex_mesh.command_start(args), 0)
        self.assertEqual(len(ensure.call_args_list), 2)
        self.assertEqual([call.kwargs.get("cwd") for call in ensure.call_args_list], [str(self.worktree)] * 2)

    def test_no_envrc_result_uses_the_caller_environment_and_records_no_shell(self) -> None:
        self.shell_load.side_effect = None
        self.shell_load.return_value = nix_env.NoEnvrc({"IN_NIX_SHELL": "must not be used"})
        self.assert_plain_launch()
        self.shell_load.assert_called_once()
        self.assertEqual(cast(dict[str, str], self.launch.call_args.kwargs["env"]),
                         self.shell_load.call_args.args[1])
        self.assertFalse((self.session_dir / codex_mesh.FAILED_DEV_SHELL_FILE).exists())

    def test_refused_shell_logs_its_line_once_and_reuses_the_fallback(self) -> None:
        reason = f"nix_env: {self.worktree}: .envrc loaded but no nix shell (IN_NIX_SHELL unset)"
        self.shell_load.side_effect = None
        self.shell_load.return_value = nix_env.Refused(reason)
        self.assert_plain_launch()
        self.assertEqual(self.ensure(), (4321, False))
        self.shell_load.assert_called_once()
        self.launch.assert_called_once()
        log = (self.session_dir / codex_mesh.SERVER_LOG).read_text()
        self.assertEqual(log.count(reason), 1)


class ServerRecordTests(unittest.TestCase):
    """Dropping a wedged server, and reaping what was dropped."""

    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    session_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    spawned: list[subprocess.Popen[bytes]]  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.session_dir = Path(self.temporary.name)
        self.spawned = []

    @override
    def tearDown(self) -> None:
        for process in self.spawned:
            with contextlib.suppress(OSError):
                process.kill()
            _ = process.wait(timeout=5)
        self.temporary.cleanup()

    def sleeper(self) -> int:
        """A live pid to stand in for an app-server, with no codex installed."""
        process = subprocess.Popen(["sleep", "30"])
        self.spawned.append(process)
        return process.pid

    def test_reap_uses_configured_probes_and_escalates_only_for_a_live_server(self) -> None:
        elapsed = [0.0]
        waits: list[float] = []

        @bounded_fixture
        def wait(seconds: float) -> None:
            waits.append(seconds)
            elapsed[0] += 1

        for grace in (5.0, 2.0):
            with self.subTest(grace=grace):
                elapsed[0] = 0.0
                waits.clear()
                with patch.object(codex_mesh, "REAP_WAIT_SECS", grace), \
                        patch.object(codex_mesh, "time", SimpleNamespace(time=bounded_fixture(lambda: elapsed[0]), sleep=wait)), \
                        patch.object(codex_mesh, "_pid_alive", return_value=True), \
                        patch.object(os, "kill") as kill:
                    self.assertTrue(codex_mesh._reap(12345))  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(waits, [0.001] * int(grace))
                self.assertEqual([call.args for call in kill.call_args_list],
                                 [(12345, signal.SIGTERM), (12345, signal.SIGKILL)])
        with patch.object(codex_mesh, "_pid_alive", side_effect=[True, False, False]), \
                patch.object(time, "sleep") as sleeping, \
                patch.object(os, "kill") as kill:
            self.assertTrue(codex_mesh._reap(12345))  # pyright: ignore[reportPrivateUsage]
        kill.assert_called_once_with(12345, signal.SIGTERM)
        sleeping.assert_not_called()

    def write_server(self, port: int, pid: int) -> None:
        _ = (self.session_dir / codex_mesh.SERVER_FILE).write_text(
            json.dumps({"port": port, "pid": pid, "codex_home": str(agent_accounts.workers_home()), "auto_compact_tokens": codex_mesh.AUTO_COMPACT_TOKENS}), encoding="utf-8"
        )

    def retired_ports(self) -> list[object]:
        path = self.session_dir / codex_mesh.RETIRED_FILE
        if not path.exists():
            return []
        stored = codex_mesh._as_dict(  # pyright: ignore[reportPrivateUsage]
            json.loads(path.read_text(encoding="utf-8"))  # pyright: ignore[reportAny]
        )
        servers = stored.get("servers")
        if not isinstance(servers, list):
            return []
        return [
            codex_mesh._as_dict(record).get("port")  # pyright: ignore[reportPrivateUsage]
            for record in cast("list[object]", servers)
        ]

    def test_a_live_record_is_attached_to_rather_than_replaced(self) -> None:
        pid = self.sleeper()
        self.write_server(4321, pid)
        port, fresh = codex_mesh.ensure_server(str(self.session_dir))
        # Never started a server, so `codex` need not even be installed here --
        # which is the point: an inherited server is exactly what goes unchecked.
        self.assertEqual((port, fresh), (4321, False))

    def test_using_a_live_server_marks_its_record_as_used(self) -> None:
        self.write_server(4321, self.sleeper())
        record = self.session_dir / codex_mesh.SERVER_FILE
        old = time.time() - 7200
        os.utime(record, (old, old))
        _ = codex_mesh.ensure_server(str(self.session_dir))
        self.assertGreater(record.stat().st_mtime, old + 3600)

    def test_replacing_a_server_record_retires_the_old_live_server(self) -> None:
        old_pid = self.sleeper()
        self.write_server(4321, old_pid)
        replacement: codex_mesh.ServerRecord = {"port": 9876, "pid": 99999999}

        codex_mesh._record_started_server(  # pyright: ignore[reportPrivateUsage]
            str(self.session_dir), replacement
        )

        current = cast(
            "dict[str, object]",
            cast("object", json.loads(
                (self.session_dir / codex_mesh.SERVER_FILE).read_text(encoding="utf-8")
            )),
        )
        self.assertEqual(current, replacement)
        self.assertEqual(self.retired_ports(), [4321])
        self.assertTrue(codex_mesh._pid_alive(old_pid))  # pyright: ignore[reportPrivateUsage]

    def test_a_server_that_never_accepts_is_stopped_before_start_fails(self) -> None:
        process = subprocess.Popen(["sleep", "30"])
        self.spawned.append(process)
        with patch.object(codex_mesh, "SERVER_START_TIMEOUT_SECS", 0), \
                patch.object(codex_mesh, "_free_port", return_value=4321), \
                patch.object(subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(SystemExit, "did not accept connections"):
                _ = codex_mesh._start_server(str(self.session_dir), agent_accounts.workers_home())  # pyright: ignore[reportPrivateUsage]

        self.assertIsNotNone(process.poll())
        self.assertFalse((self.session_dir / codex_mesh.SERVER_FILE).exists())

    def test_a_server_is_stopped_when_recording_its_start_fails(self) -> None:
        process = subprocess.Popen(["sleep", "30"])
        self.spawned.append(process)
        probe = SimpleNamespace(close=lambda: None)
        with patch.object(codex_mesh, "_free_port", return_value=4321), \
                patch.object(subprocess, "Popen", return_value=process), \
                patch.object(codex_mesh, "WebSocket", return_value=probe), \
                patch.object(codex_mesh, "_record_started_server", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                _ = codex_mesh._start_server(str(self.session_dir), agent_accounts.workers_home())  # pyright: ignore[reportPrivateUsage]

        self.assertIsNotNone(process.poll())
        self.assertFalse((self.session_dir / codex_mesh.SERVER_FILE).exists())

    def watcher(self, pid: int) -> tuple[subprocess.Popen[bytes], Path]:
        """Start the real watcher with a stand-in for the Python that would run `idle-stop`."""
        calls = self.session_dir / "calls"
        stub = self.session_dir / "python"
        _ = stub.write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\n', encoding="utf-8")
        stub.chmod(0o755)
        with patch.object(codex_mesh, "WATCH_WAKE_SECS", 0.1), \
                patch.object(codex_mesh, "SERVER_IDLE_SECS", 60.0), \
                patch.object(sys, "executable", str(stub)):
            process = codex_mesh._start_watcher(str(self.session_dir), pid, 4321)  # pyright: ignore[reportPrivateUsage]
        assert isinstance(process, subprocess.Popen)
        self.spawned.append(process)
        return process, calls

    def test_repeated_retirement_keeps_one_watcher_until_that_watcher_exits(self) -> None:
        pid = self.sleeper()
        self.write_server(4321, pid)
        process, calls = self.watcher(pid)
        with patch.object(subprocess, "Popen", wraps=subprocess.Popen) as launch:
            existing = codex_mesh._start_watcher(str(self.session_dir), pid, 4321)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(existing, codex_mesh.ExistingServerWatcher)
        launch.assert_not_called()
        self.assertFalse(calls.exists())
        old = time.time() - 7200
        os.utime(self.session_dir / codex_mesh.SERVER_FILE, (old, old))
        _ = process.wait(timeout=10)
        replacement = self.watcher(pid)[0]
        self.assertNotEqual(replacement.pid, process.pid)

    def test_the_watcher_asks_only_about_a_record_gone_untouched_and_then_leaves(self) -> None:
        pid = self.sleeper()
        self.write_server(4321, pid)
        process, calls = self.watcher(pid)
        time.sleep(0.5)
        # Several wakes on a record touched this minute: nothing is asked.
        self.assertFalse(calls.exists())
        old = time.time() - 7200
        os.utime(self.session_dir / codex_mesh.SERVER_FILE, (old, old))
        self.assertEqual(process.wait(timeout=10), 0)
        self.assertEqual(calls.read_text(encoding="utf-8").splitlines(), [
            f"{Path(codex_mesh.__file__).resolve()} idle-stop --session-dir {self.session_dir.resolve()} "
            + f"--pid {pid} --port 4321",
        ])

    def test_the_watcher_judges_a_retired_server_at_every_wake(self) -> None:
        # The run's new server keeps the record fresh, so freshness says nothing about this one.
        pid = self.sleeper()
        self.write_server(9876, self.sleeper())
        _ = (self.session_dir / codex_mesh.RETIRED_FILE).write_text(
            json.dumps({"servers": [{"port": 4321, "pid": pid}]}, indent=2), encoding="utf-8")
        process, calls = self.watcher(pid)
        self.assertEqual(process.wait(timeout=10), 0)
        self.assertIn(f"--pid {pid} --port 4321", calls.read_text(encoding="utf-8"))

    def stale(self, started: float, changed: float | None) -> tuple[tuple[int, bool], list[tuple[object, ...]]]:
        """`ensure_server` on a live record whose server started at `started`, with the sign-in
        last changed at `changed`: its answer, and the watchers it started."""
        pid = self.sleeper()
        self.write_server(4321, pid)
        with patch.object(codex_mesh, "_server_started_at", return_value=started), \
                patch.object(codex_mesh, "_sign_in_changed_at", return_value=changed), \
                patch.object(codex_mesh, "_start_server", return_value=5555), \
                patch.object(codex_mesh, "_start_watcher") as watch, \
                patch.object(codex_mesh, "_stop_unused_with_lock_held", return_value=False):
            answer = codex_mesh.ensure_server(str(self.session_dir))
        return answer, [call.args for call in watch.call_args_list]

    def test_a_server_holding_an_old_sign_in_is_replaced_at_the_next_launch(self) -> None:
        answer, watched = self.stale(started=1000.0, changed=2000.0)
        self.assertEqual(answer, (5555, True))
        self.assertEqual(self.retired_ports(), [4321])
        self.assertEqual([args[1:] for args in watched], [(self.spawned[-1].pid, 4321)])
        self.assertIn("holds the old sign-in",
                      (self.session_dir / codex_mesh.SERVER_LOG).read_text(encoding="utf-8"))

    def test_a_server_started_after_the_sign_in_is_kept(self) -> None:
        for changed in (500.0, None):
            with self.subTest(changed=changed):
                self.assertEqual(self.stale(started=1000.0, changed=changed), ((4321, False), []))
        self.assertEqual(self.retired_ports(), [])

    def test_the_watcher_leaves_with_its_server(self) -> None:
        server = subprocess.Popen(["sleep", "30"])
        self.write_server(4321, server.pid)
        process, calls = self.watcher(server.pid)
        server.kill()
        _ = server.wait(timeout=5)
        _ = process.wait(timeout=10)
        self.assertFalse(calls.exists())

    def test_a_worker_keeps_its_server_port_until_its_launcher_lets_go(self) -> None:
        directory = str(self.session_dir)
        codex_mesh._update_roster(directory, "worker", {  # pyright: ignore[reportPrivateUsage]
            "thread_id": "t1", "turn_id": "u1", "status": "running", "port": 4001})
        codex_mesh._update_roster(directory, "worker", {  # pyright: ignore[reportPrivateUsage]
            "thread_id": "t1", "turn_id": "u2", "status": "running"})
        roster = cast("dict[str, dict[str, object]]",
                      json.loads((self.session_dir / codex_mesh.ROSTER_FILE).read_text(encoding="utf-8")))
        self.assertEqual(roster["worker"]["port"], 4001)
        codex_mesh._update_roster(directory, "worker", {"thread_id": "t1", "status": "done"})  # pyright: ignore[reportPrivateUsage]
        roster = cast("dict[str, dict[str, object]]",
                      json.loads((self.session_dir / codex_mesh.ROSTER_FILE).read_text(encoding="utf-8")))
        self.assertNotIn("port", roster["worker"])

    def attached_worker(self) -> codex_mesh.ThreadRecord:
        """A running worker whose launcher lives and whose record names no server, as a launcher
        started before workers recorded one leaves it."""
        return {"thread_id": "t1", "turn_id": "u1", "status": "running", "launcher_pid": os.getpid()}

    def test_a_worker_with_no_recorded_port_is_reached_on_the_retired_server_holding_it(self) -> None:
        self.write_server(4321, self.sleeper())
        _ = codex_mesh._retire_server(str(self.session_dir), 4321)  # pyright: ignore[reportPrivateUsage]
        self.write_server(9876, self.sleeper())

        def holds(port: int, _thread_id: str) -> bool:
            return port == 4321

        with patch.object(codex_mesh, "_server_holds", side_effect=holds), \
                patch.object(codex_mesh, "ensure_server") as ensure:
            port = codex_mesh._worker_port(str(self.session_dir), self.attached_worker())  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(port, 4321)
        ensure.assert_not_called()

    def test_a_worker_no_running_server_holds_fails_without_starting_one(self) -> None:
        # The run's record was retired and its next server not yet started: a new one could not
        # open a conversation the old one still holds, and would be left running.
        self.write_server(4321, self.sleeper())
        _ = codex_mesh._retire_server(str(self.session_dir), 4321)  # pyright: ignore[reportPrivateUsage]
        with patch.object(codex_mesh, "_server_holds", return_value=False), \
                patch.object(codex_mesh, "ensure_server") as ensure, \
                self.assertRaises(SystemExit) as refused:
            _ = codex_mesh._worker_port(str(self.session_dir), self.attached_worker())  # pyright: ignore[reportPrivateUsage]
        self.assertIn("nothing was started", str(refused.exception))
        ensure.assert_not_called()

    def test_retiring_drops_the_record_and_keeps_the_pid_for_stop(self) -> None:
        pid = self.sleeper()
        self.write_server(4321, pid)
        retired = codex_mesh._retire_server(  # pyright: ignore[reportPrivateUsage]
            str(self.session_dir), 4321
        )
        self.assertTrue(retired)
        self.assertFalse((self.session_dir / codex_mesh.SERVER_FILE).exists())
        self.assertEqual(self.retired_ports(), [4321])

    def test_a_peer_that_already_replaced_the_server_is_left_alone(self) -> None:
        # The workers fail together and each tries this recovery. Only the
        # first drops a server; the others would otherwise drop the replacement
        # and restart the cycle they just ended.
        self.write_server(4321, self.sleeper())
        _ = codex_mesh._retire_server(  # pyright: ignore[reportPrivateUsage]
            str(self.session_dir), 4321
        )
        self.write_server(9876, self.sleeper())
        retired = codex_mesh._retire_server(  # pyright: ignore[reportPrivateUsage]
            str(self.session_dir), 4321
        )
        self.assertFalse(retired)
        record = codex_mesh._as_dict(  # pyright: ignore[reportPrivateUsage]
            json.loads(  # pyright: ignore[reportAny]
                (self.session_dir / codex_mesh.SERVER_FILE).read_text(encoding="utf-8")
            )
        )
        self.assertEqual(record.get("port"), 9876)
        self.assertEqual(self.retired_ports(), [4321])

    def test_stop_reaps_the_retired_server_as_well_as_the_live_one(self) -> None:
        # A retired server is abandoned rather than signalled while the run is
        # going, so the end of the run is the only place that can free it.
        stale_pid = self.sleeper()
        self.write_server(4321, stale_pid)
        _ = codex_mesh._retire_server(  # pyright: ignore[reportPrivateUsage]
            str(self.session_dir), 4321
        )
        live_pid = self.sleeper()
        self.write_server(9876, live_pid)
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "project-impl": {
                "thread_id": "thread-impl", "status": "done", "role": "impl",
                "context_tokens": 120000,
            },
            "project-test": {
                "thread_id": "thread-test", "status": "running", "role": "test",
                "turn_id": "turn-test",
            },
        }), encoding="utf-8")

        code = codex_mesh.command_stop(
            argparse.Namespace(session_dir=str(self.session_dir))
        )
        self.assertEqual(code, 0)
        # Reap before asserting: these stand-ins are children of the test
        # process, so an unwaited one leaves a pid `os.kill(pid, 0)` accepts.
        # A real app-server is nobody's child and leaves no such entry.
        self.assertEqual([stale_pid, live_pid], [each.pid for each in self.spawned])
        for process in self.spawned:
            self.assertIsNotNone(
                process.wait(timeout=5), f"app-server {process.pid} outlived stop"
            )
        self.assertFalse((self.session_dir / codex_mesh.RETIRED_FILE).exists())
        self.assertFalse((self.session_dir / codex_mesh.SERVER_FILE).exists())
        roster = cast("dict[str, dict[str, object]]", json.loads(
            (self.session_dir / codex_mesh.ROSTER_FILE).read_text(encoding="utf-8")
        ))
        self.assertEqual(
            {name: record["status"] for name, record in roster.items()},
            {"project-impl": "ended", "project-test": "ended"},
        )
        self.assertEqual(roster["project-impl"]["context_tokens"], 120000)
        self.assertEqual(roster["project-test"]["role"], "test")

    def test_stop_without_live_server_still_ends_roster_and_cleans_retired_records(self) -> None:
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "project-impl": {
                "thread_id": "thread-impl", "status": "done", "role": "impl",
            },
        }), encoding="utf-8")
        _ = (self.session_dir / codex_mesh.RETIRED_FILE).write_text(json.dumps({
            "servers": [{"port": 4321, "pid": 99999999}],
        }), encoding="utf-8")

        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = codex_mesh.command_stop(
                argparse.Namespace(session_dir=str(self.session_dir))
            )

        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue(), "no app-server running\n")
        self.assertFalse((self.session_dir / codex_mesh.RETIRED_FILE).exists())
        roster = cast("dict[str, dict[str, object]]", json.loads(
            (self.session_dir / codex_mesh.ROSTER_FILE).read_text(encoding="utf-8")
        ))
        self.assertEqual(roster["project-impl"]["status"], "ended")
        self.assertEqual(roster["project-impl"]["role"], "impl")

    def test_pause_refuses_promptly_and_keeps_a_server_whose_queue_cannot_be_preserved(self) -> None:
        # A hung server may still hold queued work; a bounded refusal preserves it.
        silent = socket.socket()
        self.addCleanup(silent.close)
        silent.bind(("127.0.0.1", 0))
        silent.listen()
        port = cast(int, silent.getsockname()[1])
        server_pid = self.sleeper()
        self.write_server(port, server_pid)
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "project-impl": {
                "thread_id": "thread-impl", "status": "running", "role": "impl",
                "turn_id": "turn-impl", "port": port,
            },
        }), encoding="utf-8")

        connect = socket.create_connection
        read = MagicMock(side_effect=TimeoutError("app-server handshake timed out"))

        def silent_connection(address: tuple[str, int], *, timeout: float) -> socket.socket:
            self.assertLessEqual(timeout, TEST_HANG_GUARD_SECS,
                                 "pause omitted its deadline before a blocking handshake")
            connection = connect(address, timeout=timeout)
            self.addCleanup(connection.close)
            # Keep real connect/send/timeout handling; deliver the hung read's timeout as an event.
            return cast(socket.socket, MagicMock(wraps=connection, recv=read))

        with (patch.object(codex_mesh, "PAUSE_INTERRUPT_SECS", TEST_HANG_GUARD_SECS),
              patch.object(codex_mesh, "CONNECT_TIMEOUT_SECS", 2 * TEST_HANG_GUARD_SECS),
              patch.object(socket, "create_connection", wraps=silent_connection) as connections,
              contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO())):
            with self.assertRaisesRegex(SystemExit, "servers kept alive"):
                _ = codex_mesh.command_stop(
                    argparse.Namespace(session_dir=str(self.session_dir), pause=True)
                )

        connections.assert_called_once()
        read.assert_called_once_with(4096)
        self.assertIsNone(self.spawned[0].poll(), "pause destroyed an unconfirmed queue")
        self.assertTrue((self.session_dir / codex_mesh.SERVER_FILE).exists())
        roster = cast("dict[str, dict[str, object]]", json.loads(
            (self.session_dir / codex_mesh.ROSTER_FILE).read_text(encoding="utf-8")
        ))
        self.assertEqual(roster["project-impl"]["status"], "paused")
        self.assertIs(roster["project-impl"]["paused_turn"], True)



class RunFolderNameSweepTests(unittest.TestCase):
    """A sweep of one run root still sees the servers of runs under the other name."""

    @override
    def setUp(self) -> None:
        roles_state = self.enterContext(tempfile.TemporaryDirectory())
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"DESIGN_ROLES_STATE": roles_state})))

    def test_naming_one_real_root_sweeps_both(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            new, former, other = Path(folder) / "unit", Path(folder) / "delegate", Path(folder) / "scratch"
            with patch.object(codex_mesh, "SWEEP_ROOTS", (new, former)):
                for given, roots in ((None, (new, former)), (str(new), (new, former)), (str(former), (new, former)), (str(other), (other,))):
                    with self.subTest(given=given):
                        self.assertEqual(codex_mesh._sweep_roots(argparse.Namespace(root=given)), roots)  # pyright: ignore[reportPrivateUsage]

    def test_a_marked_run_under_the_former_name_owns_its_server(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            new, former = Path(folder) / "unit", Path(folder) / "delegate"
            run = former / "old-run"
            run.mkdir(parents=True)
            (new / "active").mkdir(parents=True)
            (former / "active").mkdir()
            _ = (run / codex_mesh.SERVER_FILE).write_text(json.dumps({"pid": 4242, "port": 5151}))
            _ = (former / "active" / "director").write_text(f"{run}\n")
            owner = codex_mesh._server_ownership((new, former), 4242, 5151, codex_mesh.NonMeshServerStderr())  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(owner, codex_mesh.CurrentRunServer(run))


if __name__ == "__main__":
    _ = unittest.main()
