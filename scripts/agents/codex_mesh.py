#!/usr/bin/env python3
"""codex_mesh.py — Run codex delegates as addressable threads on one app-server.

Why this exists rather than agent_exec.sh: a delegate launched with `codex exec`
is unreachable. Nothing outside its own process can hand it a message, so peers
coordinate only through the board and a delegate that goes down the wrong path
stays there until its turn ends. `codex app-server` fixes that by hosting every
delegate as a thread on one local websocket: any process that can open a socket
can queue a message for a named delegate, or steer one mid-turn.

The pieces this depends on, each verified against codex 0.150.1:

  * `initialize` must declare `capabilities.experimentalApi`. Without it the
    queue methods return -32600 with no hint that a flag is missing.
  * `sandbox` takes the SandboxMode STRING ("danger-full-access"), not the
    SandboxPolicy object the generated schema shows for other fields.
  * `thread/queue/add` reaches a thread between turns; `turn/steer` interrupts a
    running one and needs the turn id the delegate is currently on.

`start` deliberately blocks for the delegate's lifetime. Launches that return
immediately would break every caller that waits on a pid, so this stays in the
foreground and implement.sh's `wait`, heartbeat watch, awake timer, and pass
recording work unchanged.

Verbs:
  serve  Start the session's app-server and record where it listens.
  start  Launch one delegate as a named thread; block until its turn ends.
         The last reply lands in --summary-file, unless --reply-file names
         where replies go: then the summary file belongs to the delegate,
         which writes it as its own last act, and the reply fills it only
         when the delegate left it empty.
         With --resident, stay attached across turns instead: each finished
         turn is printed to stdout and delivered the same way, the thread
         keeps accepting `send`, and only `end` releases the block.
  follow Resume a finished named thread, start one turn, and block for that turn.
  send   Queue a message for a named delegate, delivered at its next turn.
  steer  Inject into a named delegate's running turn.
  end    Finish a resident delegate: drop its queued messages, interrupt its
         running turn, if any, and release its `start`.
  stop   Stop the session's app-server, and any it replaced mid-run.
  signin-changed
         Move every run off an app-server started before the Codex sign-in
         last changed, and tell the session holding each run. A path watch
         on the sign-in file runs it.
  list   Print the roster of named delegates and what each is doing.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import errno
import fcntl
import json
import os
import secrets
import signal
import socket
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, NotRequired, TextIO, TypedDict, cast, final

SERVER_FILE = "mesh_server.json"
SERVER_LOG = "mesh_server.log"
ROSTER_FILE = "mesh_roster.json"
# Servers dropped by _retire_server, kept so `stop` can still reap them. A
# retired server is abandoned rather than signalled: it may still be finishing a
# peer delegate's turn, and the end of the run is the only moment that knows
# nobody needs it.
RETIRED_FILE = "mesh_retired.json"
LOCK_FILE = "mesh_server.lock"
CONNECT_TIMEOUT_SECS = 20
CALL_TIMEOUT_SECS = 300
SERVER_START_TIMEOUT_SECS = 30
# How long to keep watching after a turn ends for a queued turn already in flight.
QUEUE_GRACE_SECS = 3.0
# A resident delegate sits between turns with nothing on the socket, so its loop
# wakes this often to look for the end marker.
RESIDENT_POLL_SECS = 1.0
# How fast a failure has to arrive to be read as this machine's fault rather
# than the provider's. An app-server that has wedged answers from what it
# cached, without a round trip, so it fails in seconds; a refusal that travelled
# to the provider and back does not arrive this quickly and this consistently.
RETRY_FAST_FAILURE_SECS = 120.0
# The provider's words for an account with no allowance or credits left to spend.
QUOTA_REFUSAL = "hit your usage limit"
# What an app-server says when Codex was signed in again after it started: it holds the old token
# and cannot refresh it. A server started now reads the current sign-in.
STALE_SIGN_IN = "access token could not be refreshed"
# agent_notes.py's `blocked` verb moves Codex work to Claude; one switch took 8 s
# and one relay 9 s when measured, and the relays run in parallel.
QUOTA_REPORT = Path(__file__).resolve().parent.parent / "whoami" / "agent_notes.py"
QUOTA_REPORT_TIMEOUT_SECS = 120
CAPACITY_WAIT_SECS = 30.0
CAPACITY_MAX_WAIT_SECS = 300.0
CAPACITY_BUDGET_SECS = 1200.0
WAITING_CAPACITY = "waiting_capacity"
CAPACITY_EXHAUSTED = "capacity_exhausted"
LAUNCHER_ATTACHED_STATUSES = frozenset(("running", WAITING_CAPACITY))
ENDABLE_STATUSES = LAUNCHER_ATTACHED_STATUSES | {CAPACITY_EXHAUSTED}


class RpcMessage(TypedDict, total=False):
    """One JSON-RPC frame. Every field is optional; which appear names the kind."""

    id: str
    method: str
    params: dict[str, object]
    result: dict[str, object]
    error: dict[str, object]


class ServerRecord(TypedDict):
    port: int
    pid: int


@dataclass(frozen=True)
class LiveServer:
    port: int


@dataclass(frozen=True)
class ServerRestartRequired:
    """The recorded app-server is gone; resume from the thread rollout."""


ServerAvailability = LiveServer | ServerRestartRequired


class FollowableRecord(TypedDict):
    thread_id: str
    status: Literal["done", "failed"]


# `port` is the app-server the seat's launcher is attached to. It stays the seat's way in while
# that launcher lives, even after the run has moved to a newer server.
class ActiveRecord(TypedDict):
    thread_id: str
    status: Literal["running"]
    turn_id: str
    launcher_pid: NotRequired[int]
    port: NotRequired[int]


class StartingRecord(TypedDict):
    thread_id: str
    status: Literal["starting"]
    launcher_pid: NotRequired[int]
    port: NotRequired[int]


class WaitingCapacityRecord(TypedDict):
    thread_id: str
    status: Literal["waiting_capacity"]
    launcher_pid: int
    port: NotRequired[int]


class ExhaustedRecord(TypedDict):
    thread_id: str
    status: Literal["capacity_exhausted"]


ThreadRecord = (FollowableRecord | ActiveRecord | StartingRecord |
                WaitingCapacityRecord | ExhaustedRecord)


FOLLOWABLE_STATES = frozenset(("done", "failed"))


@dataclass(frozen=True)
class FollowableThread:
    thread_id: str
    status: Literal["done", "failed"]


@dataclass(frozen=True)
class ActiveThread:
    thread_id: str
    turn_id: str
    launcher_pid: int


@dataclass(frozen=True)
class WaitingCapacityThread:
    thread_id: str
    launcher_pid: int


@dataclass(frozen=True)
class StartingThread:
    thread_id: str
    launcher_pid: int


@dataclass(frozen=True)
class ExhaustedThread:
    thread_id: str


@dataclass(frozen=True)
class InvalidThread:
    reason: str


RosterThread = (FollowableThread | ActiveThread | WaitingCapacityThread |
                StartingThread | ExhaustedThread | InvalidThread)


def _roster_thread(value: object) -> RosterThread:
    """Validate the readable JSON record before acting on its state."""
    entry = _as_dict(value)
    thread_id = _as_str(entry.get("thread_id"))
    status = entry.get("status")
    turn_id = _as_str(entry.get("turn_id"))
    pid = entry.get("launcher_pid")
    launcher_pid = pid if isinstance(pid, int) else 0
    if not thread_id:
        return InvalidThread("missing thread id")
    if status == "done":
        return FollowableThread(thread_id, "done")
    if status == "failed":
        return FollowableThread(thread_id, "failed")
    if status == "running":
        return ActiveThread(thread_id, turn_id, launcher_pid) if turn_id else InvalidThread("running thread has no turn id")
    if status == WAITING_CAPACITY:
        return WaitingCapacityThread(thread_id, launcher_pid)
    if status == "starting":
        return StartingThread(thread_id, launcher_pid)
    if status == CAPACITY_EXHAUSTED:
        return ExhaustedThread(thread_id)
    return InvalidThread(f"unknown status {status}")


# The three places an untyped value enters this module. Each is confined to one
# helper so the rest of the file stays fully typed.


def _loads(text: str) -> object:
    """json.loads as an `object`, so callers must narrow before use."""
    try:
        return json.loads(text)  # pyright: ignore[reportAny]
    except json.JSONDecodeError:
        return None


def _attr(args: argparse.Namespace, key: str) -> object:
    return getattr(args, key, None)


def _as_int(value: object) -> int:
    return value if isinstance(value, int) else 0


def _unpack_len(fmt: str, data: bytes) -> int:
    return int(struct.unpack(fmt, data)[0])  # pyright: ignore[reportAny]


def _bound_port(sock: socket.socket) -> int:
    return int(sock.getsockname()[1])  # pyright: ignore[reportAny]


def _now_stamp() -> str:
    return f"{datetime.now():%H:%M:%S}"


def _as_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return cast("dict[str, object]", cast("object", value))


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _as_float(value: object, fallback: float) -> float:
    return float(value) if isinstance(value, (int, float)) else fallback


def _parse_frame(text: str) -> RpcMessage | None:
    """Decode one frame, returning None for anything that is not a JSON object."""
    raw = _loads(text)
    if not isinstance(raw, dict):
        return None
    return cast("RpcMessage", cast("object", raw))


# ---------------------------------------------------------------------------
# Minimal websocket client
#
# The app-server's loopback listener needs no auth, so a client is a handshake
# plus RFC 6455 framing. Pulling in a dependency for that would put an install
# step between a delegate and its peers.
# ---------------------------------------------------------------------------


@final
class WebSocket:
    def __init__(self, port: int, host: str = "127.0.0.1") -> None:
        self._sock: socket.socket = socket.create_connection(
            (host, port), timeout=CONNECT_TIMEOUT_SECS
        )
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        self._sock.sendall(
            (
                f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\n"
                f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("app-server closed during handshake")
            buffer += chunk
        head, _, rest = buffer.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            detail = head.decode(errors="replace")[:200]
            raise ConnectionError(f"handshake refused: {detail}")
        self._buffer: bytes = rest

    def settimeout(self, seconds: float) -> None:
        self._sock.settimeout(seconds)

    def send(self, payload: str) -> None:
        data = payload.encode()
        mask = secrets.token_bytes(4)
        frame = bytearray([0x81])
        length = len(data)
        if length < 126:
            frame.append(0x80 | length)
        elif length < 65536:
            frame.append(0x80 | 126)
            frame += struct.pack(">H", length)
        else:
            frame.append(0x80 | 127)
            frame += struct.pack(">Q", length)
        frame += mask
        frame += bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        self._sock.sendall(bytes(frame))

    def _fill(self, count: int) -> None:
        while len(self._buffer) < count:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("app-server closed the connection")
            self._buffer += chunk

    def recv(self) -> RpcMessage | None:
        """Read one frame. None means a non-JSON or non-text frame."""
        self._fill(2)
        opcode = self._buffer[0] & 0x0F
        length = self._buffer[1] & 0x7F
        offset = 2
        if length == 126:
            self._fill(4)
            length = _unpack_len(">H", self._buffer[2:4])
            offset = 4
        elif length == 127:
            self._fill(10)
            length = _unpack_len(">Q", self._buffer[2:10])
            offset = 10
        self._fill(offset + length)
        payload = self._buffer[offset : offset + length]
        self._buffer = self._buffer[offset + length :]
        if opcode == 0x8:
            raise ConnectionError("app-server sent close")
        if opcode not in (0x1, 0x2):
            return None
        return _parse_frame(payload.decode(errors="replace"))

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._sock.close()


@final
class Client:
    """A JSON-RPC conversation with the app-server over one websocket."""

    def __init__(self, port: int, name: str) -> None:
        self._ws: WebSocket = WebSocket(port)
        self._name: str = name
        self._counter: int = 0
        self._pending: list[RpcMessage] = []
        # experimentalApi is what unlocks thread/queue/*; without it those calls
        # fail with a bare -32600 that never mentions a capability.
        _ = self.call(
            "initialize",
            {
                "clientInfo": {"name": name, "version": "1.0"},
                "capabilities": {"experimentalApi": True},
            },
        )

    def call(
        self, method: str, params: dict[str, object], timeout: float = CALL_TIMEOUT_SECS
    ) -> RpcMessage:
        self._counter += 1
        request_id = f"{self._name}-{self._counter}"
        self._ws.send(
            json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            for index, message in enumerate(self._pending):
                if message.get("id") == request_id:
                    return self._pending.pop(index)
            frame = self._read(deadline)
            if frame is None:
                continue
            if frame.get("id") == request_id:
                return frame
            self._pending.append(frame)
        return {"error": {"message": f"timed out waiting for {method}"}}

    def _read(self, deadline: float) -> RpcMessage | None:
        remaining = max(0.1, deadline - time.time())
        self._ws.settimeout(remaining)
        try:
            return self._ws.recv()
        except TimeoutError:
            return None

    def next_frame(self, deadline: float) -> RpcMessage | None:
        """Next frame of any kind, or None once `deadline` passes."""
        if self._pending:
            return self._pending.pop(0)
        if time.time() >= deadline:
            return None
        return self._read(deadline)

    def push_back(self, frame: RpcMessage) -> None:
        """Return a frame to the head of the stream for the next reader."""
        self._pending.insert(0, frame)

    def close(self) -> None:
        self._ws.close()


def _require(message: RpcMessage, what: str) -> dict[str, object]:
    error = message.get("error")
    if error:
        raise SystemExit(f"codex_mesh: {what} failed: {json.dumps(error)[:300]}")
    return _as_dict(message.get("result"))


# ---------------------------------------------------------------------------
# Session state. The delegates write the roster concurrently, so every
# read-modify-write takes an exclusive lock on the file itself.
# ---------------------------------------------------------------------------


def _session_path(session_dir: str, filename: str) -> Path:
    return Path(session_dir) / filename


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        with path.open(encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            text = handle.read()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        return {}
    return _as_dict(_loads(text))


def _update_roster(session_dir: str, name: str, record: ThreadRecord) -> None:
    path = _session_path(session_dir, ROSTER_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _ = handle.seek(0)
        text = handle.read()
        roster = _as_dict(_loads(text)) if text.strip() else {}
        previous = _as_dict(roster.get(name))
        stored = dict(record)
        if record["status"] in ("starting", "running", WAITING_CAPACITY):
            pid = record.get("launcher_pid", previous.get("launcher_pid"))
            if isinstance(pid, int) and pid > 0:
                stored["launcher_pid"] = pid
            port = record.get("port", previous.get("port"))
            if isinstance(port, int) and port > 0:
                stored["port"] = port
        roster[name] = stored
        _ = handle.seek(0)
        _ = handle.truncate()
        _ = handle.write(json.dumps(roster, indent=2, sort_keys=True))
        handle.flush()
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _lookup(session_dir: str, name: str) -> ThreadRecord:
    roster = _read_json_object(_session_path(session_dir, ROSTER_FILE))
    entry = roster.get(name)
    if not isinstance(entry, dict):
        known = ", ".join(sorted(roster)) or "(none)"
        raise SystemExit(f"codex_mesh: no delegate named '{name}'. Known: {known}")
    return cast("ThreadRecord", cast("object", entry))


def _lookup_state(session_dir: str, name: str) -> RosterThread:
    return _roster_thread(_lookup(session_dir, name))


def _roster_still_on_thread(session_dir: str, name: str, thread_id: str) -> bool:
    entry = _read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name)
    state = _roster_thread(entry)
    return not isinstance(state, InvalidThread) and state.thread_id == thread_id


def _claim_follow(session_dir: str, name: str, thread_id: str, launcher_pid: int) -> bool:
    """Take a finished roster entry before another follow can take it."""
    path = _session_path(session_dir, ROSTER_FILE)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _ = handle.seek(0)
        roster = _as_dict(_loads(handle.read()))
        current = _roster_thread(roster.get(name))
        stale = isinstance(current, (ActiveThread, StartingThread)) and (
            current.launcher_pid > 0 and not _pid_alive(current.launcher_pid)
        )
        if isinstance(current, InvalidThread) or not (
            isinstance(current, FollowableThread) or stale
        ) or current.thread_id != thread_id:
            return False
        previous_status = current.status if isinstance(current, FollowableThread) else "failed"
        roster[name] = {"thread_id": thread_id, "status": "starting",
                        "launcher_pid": launcher_pid, "previous_status": previous_status}
        _ = handle.seek(0)
        _ = handle.truncate()
        _ = handle.write(json.dumps(roster, indent=2, sort_keys=True))
        handle.flush()
        return True


def _restore_follow_claim(session_dir: str, name: str, launcher_pid: int,
                          live_turn: str = "", *, finished: bool = False) -> None:
    """Release only this launcher's unstarted claim; retain a peer's live turn."""
    path = _session_path(session_dir, ROSTER_FILE)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _ = handle.seek(0)
        roster = _as_dict(_loads(handle.read()))
        entry = _as_dict(roster.get(name))
        status = entry.get("status")
        if entry.get("launcher_pid") != launcher_pid or not (
            status == "starting" or (finished and status == "running")
        ):
            return
        thread_id = _as_str(entry.get("thread_id"))
        previous_status = entry.get("previous_status")
        if live_turn and not finished:
            restored: dict[str, object] = {"thread_id": thread_id, "status": "running", "turn_id": live_turn,
                                           "launcher_pid": launcher_pid, "previous_status": previous_status}
            if isinstance(port := entry.get("port"), int):
                restored["port"] = port
            roster[name] = restored
        else:
            roster[name] = {"thread_id": thread_id,
                            "status": previous_status if previous_status in FOLLOWABLE_STATES else "failed"}
        _ = handle.seek(0)
        _ = handle.truncate()
        _ = handle.write(json.dumps(roster, indent=2, sort_keys=True))
        handle.flush()


def _wait_for_claimed_peer(client: Client, thread_id: str, turn_id: str,
                           deadline: float) -> bool:
    """Keep the claimed peer turn active until it finishes or the wait expires."""
    while time.time() < deadline:
        frame = client.next_frame(min(deadline, time.time() + RESIDENT_POLL_SECS))
        if frame is not None and frame.get("method") in ("turn/completed", "turn/failed"):
            params = _as_dict(frame.get("params"))
            notice_id = _as_str(_as_dict(params.get("turn")).get("id"))
            if _as_str(params.get("threadId")) in ("", thread_id) and notice_id == turn_id:
                return True
        live = _read_live_turn(client, thread_id)
        if isinstance(live, ThreadIdle):
            return True
    return False


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return _bound_port(probe)


def _server_availability(session_dir: str) -> ServerAvailability:
    """The running app-server or the need to restart it.

    `ensure_server` would start one; the verbs that only ever wind things down
    must not, or ending a delegate whose server already died would leave a
    fresh server behind for nothing.
    """
    record = _read_json_object(_session_path(session_dir, SERVER_FILE))
    port = record.get("port")
    pid = record.get("pid")
    if isinstance(port, int) and isinstance(pid, int) and _pid_alive(pid):
        return LiveServer(port)
    return ServerRestartRequired()


def _seat_port(session_dir: str, record: ThreadRecord) -> int:
    """The server that reaches a seat: the one its launcher is attached to while that launcher
    lives, which after a sign-in change can be one the run has retired; otherwise the run's own.

    A seat whose launcher lives is never given a new server. Its conversation stays open on the
    server it is on, and a second server cannot open it while that one holds it.
    """
    port, pid = record.get("port"), record.get("launcher_pid")
    if not (isinstance(pid, int) and pid > 0 and _pid_alive(pid)):
        return ensure_server(session_dir)[0]
    if isinstance(port, int):
        return port
    # A launcher that has not recorded its server yet, or one started before seats recorded it.
    thread_id = record["thread_id"]
    servers = _run_servers(session_dir)
    for candidate in servers:
        if _server_holds(candidate, thread_id):
            return candidate
    current = _record_port(session_dir)
    if current in servers:
        return current
    raise SystemExit(f"codex_mesh: thread {thread_id}'s launcher {pid} is running, but no running app-server "
                     + "of this run holds it; nothing was started and the message was not delivered")


def _run_servers(session_dir: str) -> list[int]:
    """Ports of the run's servers still running: those it retired, oldest first, then its current one."""
    stored = _read_json_object(_session_path(session_dir, RETIRED_FILE)).get("servers")
    records = [_as_dict(record) for record in (cast("list[object]", stored) if isinstance(stored, list) else [])]
    records.append(_read_json_object(_session_path(session_dir, SERVER_FILE)))
    ports: list[int] = []
    for record in records:
        port, pid = record.get("port"), record.get("pid")
        if isinstance(port, int) and isinstance(pid, int) and _pid_alive(pid) and port not in ports:
            ports.append(port)
    return ports


def _server_holds(port: int, thread_id: str) -> bool:
    try:
        client = Client(port, f"seat-lookup-{os.getpid()}")
    except (ConnectionError, OSError, SystemExit):
        return False
    try:
        loaded = _as_dict(client.call("thread/loaded/list", {}).get("result")).get("data")
        return isinstance(loaded, list) and thread_id in cast("list[object]", loaded)
    except (ConnectionError, OSError, SystemExit):
        return False
    finally:
        client.close()


def _note_seat_port(session_dir: str, name: str, launcher_pid: int, port: int) -> None:
    """Record the server a follow-up launcher attached to, while its claim is still its own."""
    path = _session_path(session_dir, ROSTER_FILE)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _ = handle.seek(0)
        roster = _as_dict(_loads(handle.read()))
        entry = _as_dict(roster.get(name))
        if entry.get("launcher_pid") != launcher_pid:
            return
        entry["port"] = port
        roster[name] = entry
        _ = handle.seek(0)
        _ = handle.truncate()
        _ = handle.write(json.dumps(roster, indent=2, sort_keys=True))
        handle.flush()


def _record_port(session_dir: str) -> int | None:
    port = _read_json_object(_session_path(session_dir, SERVER_FILE)).get("port")
    return port if isinstance(port, int) else None


def _end_marker_path(session_dir: str, name: str) -> Path:
    return _session_path(session_dir, f"{name}.end")


@contextlib.contextmanager
def _pending_file(session_dir: str, name: str) -> Generator[TextIO]:
    path = _session_path(session_dir, f"{name}.pending.json")
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield handle
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _pending_for_thread(handle: TextIO, thread_id: str) -> list[str]:
    _ = handle.seek(0)
    stored = _as_dict(_loads(handle.read()))
    if stored.get("thread_id") != thread_id:
        return []
    messages = stored.get("messages")
    return [item for item in cast("list[object]", messages) if isinstance(item, str)] if isinstance(messages, list) else []


def _write_pending(handle: TextIO, thread_id: str, messages: list[str]) -> None:
    _ = handle.seek(0)
    _ = handle.truncate()
    _ = handle.write(json.dumps({"thread_id": thread_id, "messages": messages}))
    handle.flush()


def _add_pending(session_dir: str, name: str, thread_id: str, message: str) -> None:
    with _pending_file(session_dir, name) as handle:
        messages = _pending_for_thread(handle, thread_id)
        messages.append(message)
        _write_pending(handle, thread_id, messages)


def _move_queue_to_pending(client: Client, thread_id: str, park: Callable[[str], None]) -> None:
    """Park queued text locally before an idle server opens another turn. Each message is parked
    before it is deleted from the server's queue."""
    for _ in range(END_QUEUE_DRAIN_ROUNDS):
        listed = _require(client.call("thread/queue/list", {"threadId": thread_id}), "thread/queue/list")
        items = listed.get("data")
        if not isinstance(items, list) or not items:
            return
        for item in cast("list[object]", items):
            queued = _as_dict(item)
            submission = _as_str(queued.get("id"))
            inputs = queued.get("input")
            if not submission or not isinstance(inputs, list):
                continue
            messages = [
                _as_str(_as_dict(part).get("text")) for part in cast("list[object]", inputs)
                if _as_str(_as_dict(part).get("type")) == "text"
            ]
            if messages:
                park("\n".join(messages))
            _ = _require(client.call("thread/queue/delete", {
                "threadId": thread_id, "queuedSubmissionId": submission
            }), "thread/queue/delete")


def _message_text(args: argparse.Namespace) -> str:
    """The message for send/steer: --message-file wins, so a multi-line body
    never has to survive shell quoting."""
    path = _as_str(_attr(args, "message_file"))
    if path:
        return Path(path).read_text(encoding="utf-8")
    return _as_str(_attr(args, "message"))


def _record_reply(reply_path: Path, name: str, index: int, body: str) -> None:
    """Append one reply to the reply file, framed the way stdout frames it."""
    with reply_path.open("a", encoding="utf-8") as replies:
        _ = replies.write(f"=== reply from {name} ({index}) ===\n{body}\n=== end reply ===\n")


def _deliver_reply(
    name: str,
    index: int,
    text: str,
    failure: str,
    summary_path: Path,
    reply_path: Path | None,
    log: TextIO,
) -> None:
    """Hand one finished resident turn to whoever is watching.

    stdout is for a caller polling the terminal this runs in. The reply file,
    when the caller named one, keeps every reply in order and leaves the
    summary file to the delegate, which writes it as its own last act; without
    one, the summary file receives the reply. The log line records that it
    landed.
    """
    if text and failure:
        body = f"{text}\n[error] {failure}"
    elif text:
        body = text
    elif failure:
        body = f"[error] {failure}"
    else:
        body = f"The delegate {name} produced no reply."
    if reply_path is None:
        _ = summary_path.write_text(body + "\n", encoding="utf-8")
    else:
        _record_reply(reply_path, name, index, body)
    print(f"=== reply from {name} ({index}) ===\n{body}\n=== end reply ===", flush=True)
    _ = log.write(f"[{_now_stamp()}] reply {index} delivered\n")
    log.flush()


def _finish_summary(
    summary_path: Path, reply_path: Path | None, name: str, index: int, final_answer: str
) -> None:
    """Leave the delegate's answer where the caller will read it.

    With no reply file the summary file receives the answer. With one, the
    answer goes there, and the summary file is written only when the delegate
    left it empty: the launcher truncates it at launch and the delegate fills
    it as its last act, so a non-empty file is the delegate's own summary and
    the last chat reply must not replace it.
    """
    if reply_path is None:
        _ = summary_path.write_text(final_answer + "\n", encoding="utf-8")
        return
    _record_reply(reply_path, name, index, final_answer)
    if not summary_path.exists() or summary_path.stat().st_size == 0:
        _ = summary_path.write_text(final_answer + "\n", encoding="utf-8")


@contextlib.contextmanager
def _server_lock(session_dir: str) -> Generator[None]:
    """Serialize starting and dropping this session's app-server.

    The seats of a phase launch in one message and reach `ensure_server`
    within the same second. Unlocked, each reads no record, each starts a server
    and each writes the file: all but one are then orphaned, listening and
    unreachable, and a retry that started a fresh server for every failed seat
    would multiply them.
    """
    Path(session_dir).mkdir(parents=True, exist_ok=True)
    with _session_path(session_dir, LOCK_FILE).open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _retire_server(session_dir: str, port: int) -> bool:
    """Drop the recorded server so the next `ensure_server` starts a fresh one.

    Only when the record still names the server that failed: a peer seat racing
    the same recovery may already have replaced it, and dropping its new server
    would restart the cycle it just ended. The process is left running -- see
    RETIRED_FILE -- and `stop` reaps it with the rest at the end of the run.

    The roster is deliberately untouched. Its entries for other names may point
    at threads that are live on a server this seat knows nothing about, and this
    seat rewrites its own entry when it re-attaches.
    """
    with _server_lock(session_dir):
        record = _read_json_object(_session_path(session_dir, SERVER_FILE))
        if record.get("port") != port:
            return False
        _retire_record(session_dir, record)
        return True


def _retire_record(session_dir: str, record: dict[str, object]) -> None:
    """Move the server record onto RETIRED_FILE. The caller holds the server lock."""
    retired_path = _session_path(session_dir, RETIRED_FILE)
    stored = _read_json_object(retired_path).get("servers")
    servers: list[object] = cast("list[object]", stored) if isinstance(stored, list) else []
    servers.append(record)
    _ = retired_path.write_text(json.dumps({"servers": servers}, indent=2), encoding="utf-8")
    _session_path(session_dir, SERVER_FILE).unlink(missing_ok=True)


def _log_server(session_dir: str, line: str) -> None:
    with contextlib.suppress(OSError), _session_path(session_dir, SERVER_LOG).open("a", encoding="utf-8") as log:
        _ = log.write(f"[{_now_stamp()}] mesh: {line}\n")


def _sign_in_changed_at() -> float | None:
    """When Codex's sign-in file last changed. Only its change time is read, never its contents."""
    home = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    try:
        return (Path(home) / "auth.json").stat().st_mtime
    except OSError:
        return None


def _elapsed_secs(text: str) -> float | None:
    """Seconds from `ps`'s etime, written [[dd-]hh:]mm:ss."""
    days, _dash, clock = text.strip().rpartition("-")
    parts = clock.split(":")
    if not 2 <= len(parts) <= 3 or not all(part.isdigit() for part in parts) or (days and not days.isdigit()):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return float(seconds + int(days or "0") * 86400)


def _server_started_at(pid: int) -> float | None:
    """When the process started. `ps -o etime` reads the same on Linux and macOS."""
    try:
        listed = subprocess.run(["ps", "-o", "etime=", "-p", str(pid)], capture_output=True, text=True, check=False)
    except OSError:
        return None
    elapsed = _elapsed_secs(listed.stdout)
    return None if elapsed is None else time.time() - elapsed


def _holds_old_sign_in(pid: int) -> bool:
    """Whether the server started before Codex's sign-in last changed. It read the sign-in once, at
    start, and cannot refresh a token the change revoked."""
    changed = _sign_in_changed_at()
    if changed is None:
        return False
    started = _server_started_at(pid)
    return started is not None and started < changed


def _start_watcher(session_dir: str, pid: int, port: int) -> subprocess.Popen[bytes]:
    """Leave a sleeping shell behind that has the server stop itself once nothing uses it.

    The server is detached so it outlives each delegate, and only a run's end step stops one; a
    run folder that never reaches that step kept its server for good. The shell wakes every
    WATCH_WAKE_SECS, and only when the record has gone SERVER_IDLE_SECS without a touch does it
    run `idle-stop`, which stops the server or touches the record. A server RETIRED_FILE lists is
    asked about at every wake instead: the run's newer server keeps the record fresh. Its cost is a shell and a
    `sleep`, under 1 MB of their own memory, one `find` per wake, and one short Python run about
    twice an hour while the server is in use. It leaves with the server.
    """
    script = (
        'while kill -0 "$1" 2>/dev/null; do sleep "$5"; '
        + '[ -n "$(find "$3/$6" -mmin "-$7" 2>/dev/null)" ] '
        + '&& ! grep -Eq "\\"pid\\": $1([^0-9]|\\$)" "$3/$8" 2>/dev/null && continue; '
        + '"$4" "$0" idle-stop --session-dir "$3" --pid "$1" --port "$2" && exit 0; done'
    )
    return subprocess.Popen(
        ["sh", "-c", script, str(Path(__file__).resolve()), str(pid), str(port),
         str(Path(session_dir).resolve()), sys.executable, str(WATCH_WAKE_SECS), SERVER_FILE,
         str(int(SERVER_IDLE_SECS // 60)), RETIRED_FILE],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def ensure_server(session_dir: str) -> tuple[int, bool]:
    """This session's app-server port, and whether this call is what started it.

    A caller that started the server just proved the provider was reachable
    through a process nothing else has touched, so a failure against it is the
    provider's answer. A caller that attached to one already running has proved
    nothing of the kind -- see _retry_warranted.
    """
    with _server_lock(session_dir):
        path = _session_path(session_dir, SERVER_FILE)
        record = _read_json_object(path)
        port = record.get("port")
        pid = record.get("pid")
        if isinstance(port, int) and isinstance(pid, int) and _pid_alive(pid):
            if not _holds_old_sign_in(pid):
                # The record's change time is when the server was last used; its watcher reads it.
                os.utime(path)
                return port, False
            # A sign-in change `signin-changed` did not catch. The seats still on this server keep
            # it until they let go; its watcher then stops it.
            _retire_record(session_dir, record)
            _log_server(session_dir, f"app-server {pid} holds a Codex sign-in that has since changed; "
                        + "retired, starting a new one")
            _ = _start_watcher(session_dir, pid, port)
        return _start_server(session_dir), True


def _start_server(session_dir: str) -> int:
    """Start this session's app-server and record it. The caller holds the server lock."""
    path = _session_path(session_dir, SERVER_FILE)
    chosen = _free_port()
    log_path = _session_path(session_dir, SERVER_LOG)
    with log_path.open("ab") as log_handle:
        process = subprocess.Popen(
            ["codex", "app-server", "--listen", f"ws://127.0.0.1:{chosen}"],
            stdout=subprocess.DEVNULL,
            stderr=log_handle,
            start_new_session=True,
        )
    deadline = time.time() + SERVER_START_TIMEOUT_SECS
    while time.time() < deadline:
        if process.poll() is not None:
            raise SystemExit(
                f"codex_mesh: app-server exited immediately; see {log_path}"
            )
        try:
            probe = WebSocket(chosen)
        except (OSError, ConnectionError):
            time.sleep(0.4)
            continue
        probe.close()
        served: ServerRecord = {"port": chosen, "pid": process.pid}
        _ = path.write_text(json.dumps(served, indent=2), encoding="utf-8")
        _ = _start_watcher(session_dir, process.pid, chosen)
        return chosen
    raise SystemExit(f"codex_mesh: app-server did not accept connections on {chosen}")


# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------


def _describe_item(item: dict[str, object]) -> str:
    """One log line for a stream item, so heartbeat_watch.sh can narrate."""
    kind = _as_str(item.get("type"))
    if kind == "agentMessage":
        return f"agent: {' '.join(_as_str(item.get('text')).split())}"
    if kind == "commandExecution":
        command = _as_str(item.get("command")) or _as_str(item.get("commandLine"))
        return f"exec: {' '.join(command.split())}"
    if kind == "fileChange":
        return f"edit: {_as_str(item.get('path')) or 'file change'}"
    if kind == "reasoning":
        return "thinking"
    return kind or "working"


def _more_work_coming(client: Client, thread_id: str, deadline: float) -> bool:
    """True when a turn that just ended is not the delegate's last.

    A peer's `send` lands in the thread queue and the server starts a turn for it
    on its own. Two ways that shows up at this moment: the message is still
    queued, or it has already been dequeued and its `turn/started` is in flight.
    The queue read catches the first; a short grace window catches the second.
    """
    listed = client.call("thread/queue/list", {"threadId": thread_id})
    items = _as_dict(listed.get("result")).get("data")
    if isinstance(items, list) and items:
        return True
    live = _read_live_turn(client, thread_id)
    if isinstance(live, ThreadStateUnknown):
        raise SystemExit(f"thread {thread_id}: thread/read failed: {live.reason}")
    if not isinstance(live, ThreadIdle):
        return True
    # Frames are held locally rather than pushed back as they arrive: a frame
    # returned to the buffer is the next thing `next_frame` hands out, so
    # push-as-you-go would re-serve the same frame and never reach the socket.
    grace = min(time.time() + QUEUE_GRACE_SECS, deadline)
    held: list[RpcMessage] = []
    started = False
    while time.time() < grace:
        frame = client.next_frame(grace)
        if frame is None:
            continue
        held.append(frame)
        if frame.get("method") == "turn/started":
            started = True
            break
    for frame in reversed(held):
        client.push_back(frame)
    return started


@dataclass(frozen=True)
class TurnCompleted:
    """The turn finished successfully."""


@dataclass(frozen=True)
class TurnRefusedForCapacity:
    detail: str


@dataclass(frozen=True)
class TurnFailed:
    detail: str


TurnOutcome = TurnCompleted | TurnRefusedForCapacity | TurnFailed


@dataclass(frozen=True)
class RunCompleted:
    """The delegate ended without a failure."""


@dataclass(frozen=True)
class FailedBeforeThread:
    """No thread id exists, so a fast retry may repeat the original prompt."""

    failure: str
    seconds: float


@dataclass(frozen=True)
class FailedWithThread:
    """The thread exists; its original prompt must never be submitted again."""

    thread_id: str
    failure: str
    seconds: float


@dataclass(frozen=True)
class CapacityRetriesExhausted:
    """The original task still needs a turn, but its launcher is stopping."""

    thread_id: str
    retries: int
    minutes: int

    def message(self, name: str) -> str:
        return (
            f"model still at capacity after {self.retries} retries over {self.minutes} min; "
            f"thread {self.thread_id} stays on the roster (codex_mesh.py end --to {name})"
        )


RunOutcome = RunCompleted | FailedBeforeThread | FailedWithThread | CapacityRetriesExhausted


@dataclass(frozen=True)
class ThreadIdle:
    """The server says no turn is active."""


@dataclass(frozen=True)
class ThreadLive:
    turn_id: str


@dataclass(frozen=True)
class ThreadActiveWithoutTurn:
    """A turn is active but its id is not persisted in thread/read yet."""


@dataclass(frozen=True)
class ThreadStateUnknown:
    reason: str


ThreadState = ThreadIdle | ThreadLive | ThreadActiveWithoutTurn | ThreadStateUnknown


@dataclass(frozen=True)
class RelaunchAllowed:
    """Cleanup found no confirmed live turn that it could not interrupt."""


@dataclass(frozen=True)
class RelaunchBlockedByLiveTurn:
    """The old thread still has a turn this launcher could not stop."""


UnwatchedTurnCleanupResult = RelaunchAllowed | RelaunchBlockedByLiveTurn


def _capacity_clock() -> float:
    return time.monotonic()


def _capacity_sleep(seconds: float) -> None:
    time.sleep(seconds)


def _turn_outcome(method: str, params: dict[str, object]) -> TurnOutcome:
    turn = _as_dict(params.get("turn"))
    error = _as_dict(turn.get("error")) if method == "turn/completed" else _as_dict(params.get("error"))
    detail = _as_str(error.get("message"))
    info = error.get("codexErrorInfo")
    if info in ("serverOverloaded", "flexUnavailable") or (
        isinstance(info, dict) and ("serverOverloaded" in info or "flexUnavailable" in info)
    ) or "Selected model is at capacity" in detail:
        return TurnRefusedForCapacity(detail)
    if method == "turn/failed" or detail or turn.get("status") in ("failed", "interrupted"):
        return TurnFailed(detail or "turn failed")
    return TurnCompleted()


def _retry_warranted(outcome: RunOutcome, fresh_server: bool, resident: bool) -> bool:
    """Whether this failure is worth one more run against a clean app-server.

    A wedged app-server replays a provider message it cached earlier to every
    thread that attaches, with no round trip -- so the text reads exactly like a
    usage limit or an outage while nothing is wrong with the account. Reading
    the message cannot tell the two apart; running the same work against a
    server this process just started can, because a fresh server has cached
    nothing. Same failure twice means the provider really did refuse.

    Four conditions, each of them narrowing:
      * the failed server was inherited, so nothing has tested it this run;
      * no thread exists, so repeating the prompt cannot repeat work -- or the
        thread's turn was refused for a stale sign-in, which the provider turns
        away before any work, so a new thread repeats nothing either;
      * the failure arrived too fast to have reached the provider, or to have
        done work before the refusal;
      * and the delegate is not resident, where a caller is already holding the
        replies and a silent second attempt would arrive behind them.
    """
    return (
        not fresh_server
        and not resident
        and (isinstance(outcome, FailedBeforeThread)
             or (isinstance(outcome, FailedWithThread) and STALE_SIGN_IN in outcome.failure))
        and outcome.seconds <= RETRY_FAST_FAILURE_SECS
    )


def _quota_refused(outcome: RunOutcome, tested: bool, resident: bool) -> bool:
    """Whether the provider itself turned this run away for quota.

    A wedged app-server replays a usage limit it cached, so the words count only
    from a server this launcher started or retried on, or from a run long enough
    to have reached the provider. A resident's clock spans many turns and proves
    nothing about the last one.
    """
    return (
        isinstance(outcome, (FailedBeforeThread, FailedWithThread))
        and QUOTA_REFUSAL in outcome.failure
        and (tested or (not resident and outcome.seconds > RETRY_FAST_FAILURE_SECS))
    )


def _report_quota_refusal(name: str) -> None:
    """Hand a proven refusal to the quota alert, which decides whether Codex work moves to Claude."""
    try:
        done = subprocess.run(
            [sys.executable, str(QUOTA_REPORT), "blocked"],
            capture_output=True, text=True, timeout=QUOTA_REPORT_TIMEOUT_SECS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"codex_mesh: {name}: quota refusal not reported: {exc}", file=sys.stderr)
        return
    for line in (done.stdout + done.stderr).splitlines():
        print(f"codex_mesh: {name}: quota: {line}", file=sys.stderr)


def command_start(args: argparse.Namespace) -> int:
    session_dir = _as_str(_attr(args, "session_dir"))
    name = _as_str(_attr(args, "name"))
    resident = getattr(args, "resident", False) is True
    existing = _read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name)
    availability = _server_availability(session_dir)
    old_port = availability.port if isinstance(availability, LiveServer) else None
    if isinstance(existing, dict):
        old_entry = _as_dict(cast("object", existing))
        old_thread = _as_str(old_entry.get("thread_id"))
        if old_thread:
            launcher_pid = old_entry.get("launcher_pid")
            if old_entry.get("status") == "failed":
                # A failed entry is always its launcher's last write.
                if old_port is not None:
                    cleanup = _end_unwatched_turn(
                        old_port, old_thread, Path(_as_str(_attr(args, "log_file")))
                    )
                    if isinstance(cleanup, RelaunchBlockedByLiveTurn):
                        print(
                            f"codex_mesh: {name}: thread {old_thread} still has a live turn that could not be interrupted; relaunch once it ends",
                            file=sys.stderr,
                        )
                        return 2
                old_thread = ""
            elif old_entry.get("status") == WAITING_CAPACITY:
                if isinstance(launcher_pid, int) and _pid_alive(launcher_pid):
                    print(
                        f"codex_mesh: {name}: thread {old_thread} is waiting for capacity; use codex_mesh.py end --to {name} before relaunching",
                        file=sys.stderr,
                    )
                    return 2
                # A dead waiting launcher cannot resume this thread.
                old_thread = ""
        if old_thread and old_port is not None:
            try:
                check = Client(old_port, f"relaunch-{os.getpid()}")
                try:
                    live = _read_live_turn(check, old_thread)
                finally:
                    check.close()
            except (ConnectionError, OSError, SystemExit) as exc:
                live = ThreadStateUnknown(str(exc) or exc.__class__.__name__)
            if isinstance(live, ThreadStateUnknown):
                print(
                    f"codex_mesh: {name}: thread {old_thread}'s state could not be read ({live.reason}); use codex_mesh.py end --to {name} before relaunching",
                    file=sys.stderr,
                )
                return 2
            if not isinstance(live, ThreadIdle):
                print(
                    f"codex_mesh: {name}: thread {old_thread} has a live turn; use codex_mesh.py end --to {name} before relaunching",
                    file=sys.stderr,
                )
                return 2
    port, fresh_server = ensure_server(session_dir)
    outcome = _run_delegate(args, port)
    tested = fresh_server
    if _retry_warranted(outcome, fresh_server, resident):
        _ = _retire_server(session_dir, port)
        port, _fresh = ensure_server(session_dir)
        assert isinstance(outcome, (FailedBeforeThread, FailedWithThread))
        if isinstance(outcome, FailedWithThread):
            note = "the inherited app-server holds a Codex sign-in that has since changed"
        else:
            note = f"the inherited app-server failed in {outcome.seconds:.0f}s without reaching the provider"
        print(f"codex_mesh: {name}: {note}; retrying on a new one", file=sys.stderr)
        outcome = _run_delegate(args, port)
        tested = True
    if isinstance(outcome, (FailedBeforeThread, FailedWithThread)):
        print(f"codex_mesh: {name}: {outcome.failure}", file=sys.stderr)
        if _quota_refused(outcome, tested, resident):
            _report_quota_refusal(name)
        return 1
    if isinstance(outcome, CapacityRetriesExhausted):
        print(f"codex_mesh: {name}: {outcome.message(name)}", file=sys.stderr)
        return 1
    return 0


def command_follow(args: argparse.Namespace) -> int:
    session_dir = _as_str(_attr(args, "session_dir"))
    name = _as_str(_attr(args, "to"))
    claim_pid = _as_int(_attr(args, "claim_pid"))
    try:
        state = _lookup_state(session_dir, name)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 1 if claim_pid else 2
    if claim_pid:
        if not isinstance(state, StartingThread) or state.launcher_pid != claim_pid:
            print(f"codex_mesh: {name}: follow claim changed after dispatch", file=sys.stderr)
            return 1
    else:
        reason = _follow_refusal(session_dir, state)
        if reason:
            print(f"codex_mesh: {name}: {reason}; follow refused", file=sys.stderr)
            return 2
    assert not isinstance(state, InvalidThread)
    thread_id = state.thread_id
    if not claim_pid and not _claim_follow(session_dir, name, thread_id, os.getpid()):
        print(f"codex_mesh: {name} was already claimed", file=sys.stderr)
        return 2
    owner_pid = claim_pid or os.getpid()
    opened = time.time()
    client: Client | None = None
    live_turn = ""
    try:
        port, _fresh = ensure_server(session_dir)
        _note_seat_port(session_dir, name, owner_pid, port)
        client = Client(port, name)
        _ = _require(client.call("thread/resume", {"threadId": thread_id}), "thread/resume")
        live = _read_live_turn(client, thread_id)
        if not isinstance(live, ThreadIdle):
            if isinstance(live, ThreadLive):
                live_turn = live.turn_id
            reason = live.reason if isinstance(live, ThreadStateUnknown) else "live turn"
            raise SystemExit(f"thread {thread_id}: {reason}; follow-up failed")
        prompt = _message_text(args)
        timeout = _as_float(_attr(args, "timeout"), 86400.0)
        outcome = _stream_turn(args, client, port, session_dir, name, thread_id, prompt, timeout, opened, follow=True)
        client = None
        if isinstance(outcome, (FailedBeforeThread, FailedWithThread)):
            print(f"codex_mesh: {name}: {outcome.failure}", file=sys.stderr)
            return 1
        if isinstance(outcome, CapacityRetriesExhausted):
            print(f"codex_mesh: {name}: {outcome.message(name)}", file=sys.stderr)
            return 1
        return 0
    except (ConnectionError, OSError, SystemExit) as exc:
        _restore_follow_claim(session_dir, name, owner_pid, live_turn)
        if live_turn and client is not None:
            try:
                timeout = _as_float(_attr(args, "timeout"), 86400.0)
                if _wait_for_claimed_peer(client, thread_id, live_turn, opened + timeout):
                    _restore_follow_claim(session_dir, name, owner_pid, finished=True)
            except (ConnectionError, OSError, SystemExit):
                pass
        print(f"codex_mesh: {name}: {exc}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()


def _follow_refusal(session_dir: str, state: RosterThread) -> str:
    if isinstance(state, InvalidThread):
        return state.reason
    stale_launcher = isinstance(state, (ActiveThread, StartingThread)) and (
        state.launcher_pid > 0 and not _pid_alive(state.launcher_pid)
    )
    if not isinstance(state, FollowableThread) and not stale_launcher:
        return "seat is busy or not followable"
    availability = _server_availability(session_dir)
    if isinstance(availability, ServerRestartRequired):
        return ""
    try:
        probe = Client(availability.port, f"follow-check-{os.getpid()}")
        try:
            live = _read_live_turn(probe, state.thread_id)
        finally:
            probe.close()
    except (ConnectionError, OSError, SystemExit) as exc:
        return f"thread/read failed: {exc}"
    if isinstance(live, ThreadIdle):
        return ""
    return live.reason if isinstance(live, ThreadStateUnknown) else "live turn"


def command_can_follow(args: argparse.Namespace) -> int:
    session_dir = _as_str(_attr(args, "session_dir"))
    name = _as_str(_attr(args, "to"))
    try:
        state = _lookup_state(session_dir, name)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2
    reason = _follow_refusal(session_dir, state)
    if reason:
        print(f"codex_mesh: {name}: {reason}; follow refused", file=sys.stderr)
        return 2
    claim_pid = _as_int(_attr(args, "claim_pid"))
    if claim_pid:
        assert not isinstance(state, InvalidThread)
        if not _claim_follow(session_dir, name, state.thread_id, claim_pid):
            print(f"codex_mesh: {name} was already claimed", file=sys.stderr)
            return 2
    return 0


def command_release_follow(args: argparse.Namespace) -> int:
    _restore_follow_claim(_as_str(_attr(args, "session_dir")),
                          _as_str(_attr(args, "to")), _as_int(_attr(args, "claim_pid")))
    return 0


def _run_delegate(args: argparse.Namespace, port: int) -> RunOutcome:
    """Attach one delegate to the app-server on `port` and run it to its end."""
    session_dir = _as_str(_attr(args, "session_dir"))
    name = _as_str(_attr(args, "name"))
    timeout = _as_float(_attr(args, "timeout"), 86400.0)
    opened = time.time()
    try:
        return _attach_and_run(args, port, session_dir, name, timeout, opened)
    except (ConnectionError, OSError, SystemExit) as exc:
        return FailedBeforeThread(str(exc) or exc.__class__.__name__, time.time() - opened)


def _thread_start_params(args: argparse.Namespace) -> dict[str, object]:
    """The `thread/start` request for one delegate."""
    params: dict[str, object] = {
        "cwd": _as_str(_attr(args, "cwd")),
        "approvalPolicy": "never",
        # The SandboxMode string, not a SandboxPolicy object: the server rejects
        # {"type": "dangerFullAccess"} with "unknown variant `type`".
        "sandbox": _as_str(_attr(args, "sandbox")),
        # Deliberately NOT ephemeral, unlike `codex exec --ephemeral`. An
        # ephemeral thread refuses `thread/queue/add` outright ("ephemeral thread
        # does not support queued submissions"), which is the one call peers use
        # most -- so ephemerality would cost the mesh the thing it exists for.
        # The price is the usual codex rollout file under ~/.codex/sessions.
    }
    model = _as_str(_attr(args, "model"))
    if model:
        params["model"] = model
    # Empty inherits `service_tier` from ~/.codex/config.toml, as `codex exec`
    # does. The server takes the config spelling and maps it itself ("fast"
    # starts the thread on "priority").
    service_tier = _as_str(_attr(args, "service_tier"))
    if service_tier:
        params["serviceTier"] = service_tier
    return params


def _attach_and_run(
    args: argparse.Namespace,
    port: int,
    session_dir: str,
    name: str,
    timeout: float,
    opened: float,
) -> RunOutcome:
    prompt = Path(_as_str(_attr(args, "prompt_file"))).read_text(encoding="utf-8")
    log_path = Path(_as_str(_attr(args, "log_file")))

    client = Client(port, name)
    started = _require(
        client.call("thread/start", _thread_start_params(args)), "thread/start"
    )
    thread_id = _as_str(_as_dict(started.get("thread")).get("id"))
    if not thread_id:
        raise SystemExit("codex_mesh: thread/start returned no thread id")

    with _pending_file(session_dir, name) as pending_file:
        previous = _as_dict(_read_json_object(
            _session_path(session_dir, ROSTER_FILE)
        ).get(name))
        previous_thread = _as_str(previous.get("thread_id"))
        if previous_thread and previous_thread != thread_id:
            dropped = len(_pending_for_thread(pending_file, previous_thread))
            _ = pending_file.seek(0)
            _ = pending_file.truncate()
            with log_path.open("a", encoding="utf-8") as log:
                stamp = datetime.now().astimezone().isoformat(timespec="seconds")
                _ = log.write(
                    f"[{stamp}] mesh: dropped {dropped} held messages for thread {previous_thread} on relaunch\n"
                )
        _update_roster(
            session_dir, name, {"thread_id": thread_id, "status": "starting", "port": port}
        )

    return _stream_turn(args, client, port, session_dir, name, thread_id, prompt, timeout, opened)


def _stream_turn(
    args: argparse.Namespace,
    client: Client,
    port: int,
    session_dir: str,
    name: str,
    thread_id: str,
    prompt: str,
    timeout: float,
    opened: float,
    *,
    follow: bool = False,
) -> RunOutcome:
    """Stream a new or resumed thread's turn through the same lifecycle."""
    log_path = Path(_as_str(_attr(args, "log_file")))
    summary_path = Path(_as_str(_attr(args, "summary_file")))
    reply_file = _as_str(_attr(args, "reply_file"))
    reply_path = Path(reply_file) if reply_file else None

    turn_id = ""
    resident = getattr(args, "resident", False) is True
    end_marker = _end_marker_path(session_dir, name)
    end_marker.unlink(missing_ok=True)
    final_answer = ""
    failure = ""
    followed_failure = ""
    replies = 0
    capacity_waited = 0.0
    capacity_retries = 0
    resume_owed = False
    retry_waited = False
    run_outcome: RunOutcome = RunCompleted()
    deadline = time.time() + timeout
    try:
        # The roster is written before this call: a disconnect after thread
        # creation must still leave its id reachable.
        if not follow:
            _ = client.call("thread/name/set", {"threadId": thread_id, "name": name})
        turn_id = _start_turn(client, args, thread_id, prompt)
        if not turn_id:
            raise SystemExit("codex_mesh: turn/start returned no turn id")
        followed_turn = turn_id if follow else ""
        _update_roster(session_dir, name, {
            "thread_id": thread_id, "turn_id": turn_id, "status": "running", "port": port
        })
        with log_path.open("a", encoding="utf-8") as log:
            _ = log.write(f"[{_now_stamp()}] mesh: {name} thread {thread_id}\n")
            log.flush()
            while time.time() < deadline:
                # A resident thread must wake to see `end` between turns.
                wait_until = min(deadline, time.time() + RESIDENT_POLL_SECS) if resident else deadline
                frame = client.next_frame(wait_until)
                if frame is None:
                    if end_marker.exists():
                        break
                    if resident and _record_port(session_dir) != port:
                        moved = _move_resident(args, session_dir, name, thread_id, client)
                        if moved is not None:
                            client, port = moved.client, moved.port
                            turn_id = moved.turn_id or turn_id
                            _ = log.write(f"[{_now_stamp()}] mesh: {name} moved to the run's app-server on "
                                          + f"port {port}; the run retired the one it was on\n")
                            log.flush()
                    continue
                method = frame.get("method")
                params = _as_dict(frame.get("params"))
                if _as_str(params.get("threadId")) not in ("", thread_id):
                    continue
                if method == "turn/started":
                    live = _as_str(_as_dict(params.get("turn")).get("id"))
                    if live and (not follow or not followed_turn or live == followed_turn):
                        turn_id = live
                        if follow:
                            followed_turn = live
                        _update_roster(session_dir, name, {"thread_id": thread_id, "turn_id": live, "status": "running"})
                elif method == "item/completed":
                    item = _as_dict(params.get("item"))
                    _ = log.write(f"[{_now_stamp()}] {_describe_item(item)}\n")
                    log.flush()
                    if _as_str(item.get("type")) == "agentMessage":
                        text = _as_str(item.get("text"))
                        if text:
                            final_answer = text
                elif method in ("turn/completed", "turn/failed"):
                    notice_turn = _as_str(_as_dict(params.get("turn")).get("id"))
                    if follow and notice_turn and followed_turn and notice_turn != followed_turn:
                        continue
                    turn_outcome = _turn_outcome(method, params)
                    if isinstance(turn_outcome, TurnRefusedForCapacity):
                        resume_owed = True
                        retry_waited = False
                        _move_queue_to_pending(
                            client, thread_id, lambda message: _add_pending(session_dir, name, thread_id, message))
                        remaining = CAPACITY_BUDGET_SECS - capacity_waited
                        if remaining <= 0:
                            run_outcome = CapacityRetriesExhausted(
                                thread_id, capacity_retries, int(CAPACITY_BUDGET_SECS / 60)
                            )
                            failure = run_outcome.message(name)
                            _update_roster(session_dir, name, {
                                "thread_id": thread_id, "status": CAPACITY_EXHAUSTED
                            })
                            break
                        # A queued peer message may already have opened a turn.
                        # Stream it, then return here with the task still owed.
                        with _pending_file(session_dir, name):
                            if end_marker.exists() or not _roster_still_on_thread(session_dir, name, thread_id):
                                reason = "ended" if end_marker.exists() else "replaced"
                                _ = log.write(f"[{_now_stamp()}] capacity launcher {reason}; no resume turn started\n")
                                log.flush()
                                return FailedWithThread(thread_id, f"capacity launcher {reason}", time.time() - opened)
                        live = _read_live_turn(client, thread_id)
                        if isinstance(live, ThreadStateUnknown):
                            raise SystemExit(f"thread {thread_id}: thread/read failed: {live.reason}")
                        if not isinstance(live, ThreadIdle):
                            continue
                    else:
                        capacity_waited = 0.0
                        capacity_retries = 0
                    if resume_owed:
                        if isinstance(turn_outcome, TurnRefusedForCapacity) and not retry_waited:
                            remaining = CAPACITY_BUDGET_SECS - capacity_waited
                            wait = min(CAPACITY_WAIT_SECS * 2.0 ** capacity_retries,
                                       CAPACITY_MAX_WAIT_SECS, remaining)
                            capacity_retries += 1
                            with _pending_file(session_dir, name):
                                _update_roster(session_dir, name, {
                                    "thread_id": thread_id,
                                    "status": WAITING_CAPACITY, "launcher_pid": os.getpid(),
                                })
                            live = _read_live_turn(client, thread_id)
                            if isinstance(live, ThreadStateUnknown):
                                raise SystemExit(f"thread {thread_id}: thread/read failed: {live.reason}")
                            if not isinstance(live, ThreadIdle):
                                continue
                            next_at = datetime.now().astimezone().timestamp() + wait
                            next_stamp = datetime.fromtimestamp(next_at).astimezone().isoformat(timespec="seconds")
                            _ = log.write(f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] capacity retry {capacity_retries}: next turn at {next_stamp}\n")
                            log.flush()
                            wait_started = _capacity_clock()
                            _capacity_sleep(wait)
                            capacity_waited += max(wait, _capacity_clock() - wait_started)
                            retry_waited = True
                        live = _read_live_turn(client, thread_id)
                        if isinstance(live, ThreadStateUnknown):
                            raise SystemExit(f"thread {thread_id}: thread/read failed: {live.reason}")
                        if not isinstance(live, ThreadIdle):
                            if isinstance(live, ThreadLive):
                                turn_id = live.turn_id
                            else:
                                turn_id = ""
                            with _pending_file(session_dir, name):
                                if end_marker.exists() or not _roster_still_on_thread(session_dir, name, thread_id):
                                    reason = "ended" if end_marker.exists() else "replaced"
                                    _ = log.write(f"[{_now_stamp()}] capacity launcher {reason}; no resume turn started\n")
                                    log.flush()
                                    return FailedWithThread(thread_id, f"capacity launcher {reason}", time.time() - opened)
                                if turn_id:
                                    _update_roster(session_dir, name, {
                                        "thread_id": thread_id, "turn_id": turn_id, "status": "running"
                                    })
                                else:
                                    _update_roster(session_dir, name, {
                                        "thread_id": thread_id, "status": "starting",
                                        "launcher_pid": os.getpid(),
                                    })
                            continue
                        with _pending_file(session_dir, name) as pending_file:
                            if end_marker.exists() or not _roster_still_on_thread(session_dir, name, thread_id):
                                reason = "ended" if end_marker.exists() else "replaced"
                                _ = log.write(f"[{_now_stamp()}] capacity launcher {reason}; no resume turn started\n")
                                log.flush()
                                return FailedWithThread(thread_id, f"capacity launcher {reason}", time.time() - opened)
                            messages = _pending_for_thread(pending_file, thread_id)
                            turn_id = _start_turn(
                                client, args, thread_id,
                                "Your last turn stopped because the model was at capacity. Continue from where you stopped; your edits are already in the tree.",
                                tuple(messages),
                            )
                            if follow:
                                followed_turn = turn_id
                            _write_pending(pending_file, thread_id, [])
                            _update_roster(session_dir, name, {
                                "thread_id": thread_id, "turn_id": turn_id, "status": "running"
                            })
                        resume_owed = False
                        continue
                    # A finished turn may have a peer turn opening behind it.
                    if follow and isinstance(turn_outcome, TurnFailed) and not followed_failure:
                        followed_failure = turn_outcome.detail
                    if not resident and _more_work_coming(client, thread_id, deadline):
                        if follow:
                            live = _read_live_turn(client, thread_id)
                            if isinstance(live, ThreadLive):
                                followed_turn = live.turn_id
                            else:
                                followed_turn = ""
                        continue
                    failure = followed_failure or (turn_outcome.detail if isinstance(turn_outcome, TurnFailed) else "")
                    if not resident:
                        break
                    replies += 1
                    _deliver_reply(name, replies, final_answer, failure, summary_path, reply_path, log)
                    final_answer = ""
                    failure = ""
                    _update_roster(session_dir, name, {"thread_id": thread_id, "turn_id": turn_id, "status": "running"})
                    if end_marker.exists():
                        break
            else:
                failure = f"no turn/completed within {timeout:.0f}s"
        # A queued peer message can open a turn after the last completion
        # notification. Check once more before this launcher stops watching.
        if not resident or failure or end_marker.exists():
            _ = _end_unwatched_turn(port, thread_id, log_path)
        if not resident or replies == 0:
            _finish_summary(summary_path, reply_path, name, replies + 1, final_answer or failure or f"The delegate {name} produced no summary.")
        if not isinstance(run_outcome, CapacityRetriesExhausted):
            _update_roster(session_dir, name, {"thread_id": thread_id, "status": "failed" if failure else "done"})
        if isinstance(run_outcome, CapacityRetriesExhausted):
            return run_outcome
        return FailedWithThread(thread_id, failure, time.time() - opened) if failure else RunCompleted()
    except (ConnectionError, OSError, SystemExit) as exc:
        # Once thread/start returned, even a disconnected turn/start may have
        # begun work. Keep its id and never resubmit the original prompt.
        _ = _end_unwatched_turn(port, thread_id, log_path)
        failure = str(exc) or exc.__class__.__name__
        if _roster_still_on_thread(session_dir, name, thread_id):
            _update_roster(session_dir, name, {"thread_id": thread_id, "status": "failed"})
        return FailedWithThread(thread_id, failure, time.time() - opened)
    finally:
        client.close()


@dataclass(frozen=True)
class MovedSeat:
    client: Client
    port: int
    # The turn opened for messages that were waiting; empty when none were.
    turn_id: str


def _move_resident(
    args: argparse.Namespace, session_dir: str, name: str, thread_id: str, client: Client
) -> MovedSeat | None:
    """Move a resident seat between turns onto the run's current app-server; None while a turn runs.

    The run retired the server this seat is attached to, most often because Codex was signed in
    again after it started. The move holds the seat's pending lock, which `send` also takes, so no
    message lands between the two servers: what was queued on the old one opens the first turn on
    the new one.
    """
    if not isinstance(_read_live_turn(client, thread_id), ThreadIdle):
        return None
    with _pending_file(session_dir, name) as handle:
        if not isinstance(_read_live_turn(client, thread_id), ThreadIdle):
            return None
        messages = _pending_for_thread(handle, thread_id)

        def park(message: str) -> None:
            messages.append(message)
            _write_pending(handle, thread_id, messages)

        _move_queue_to_pending(client, thread_id, park)
        client.close()
        port, _fresh = ensure_server(session_dir)
        moved = Client(port, name)
        _ = _require(moved.call("thread/resume", {"threadId": thread_id}), "thread/resume")
        turn_id = ""
        if messages:
            turn_id = _start_turn(moved, args, thread_id, messages[0], tuple(messages[1:]))
            _write_pending(handle, thread_id, [])
        previous = _as_str(_as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name)).get("turn_id"))
        _update_roster(session_dir, name, {
            "thread_id": thread_id, "turn_id": turn_id or previous, "status": "running", "port": port
        })
    return MovedSeat(moved, port, turn_id)


def _start_turn(
    client: Client, args: argparse.Namespace, thread_id: str, message: str,
    pending: tuple[str, ...] = (),
) -> str:
    inputs: list[dict[str, str]] = [{"type": "text", "text": message}]
    inputs.extend({"type": "text", "text": item} for item in pending)
    params: dict[str, object] = {"threadId": thread_id, "input": inputs}
    effort = _as_str(_attr(args, "effort"))
    if effort:
        params["effort"] = effort
    result = _require(client.call("turn/start", params), "turn/start")
    return _as_str(_as_dict(result.get("turn")).get("id"))


def _log_uninterrupted_thread(log_path: Path, thread_id: str, reason: str) -> None:
    with log_path.open("a", encoding="utf-8") as log:
        _ = log.write(
            f"[{_now_stamp()}] thread {thread_id} could not be interrupted ({reason})\n"
        )


def _end_unwatched_turn(
    port: int, thread_id: str, log_path: Path
) -> UnwatchedTurnCleanupResult:
    """Best effort cleanup when this launcher cannot watch another turn."""
    try:
        client = Client(port, f"cleanup-{os.getpid()}")
        try:
            _ = _drop_queued_messages(client, thread_id)
            live = _read_live_turn(client, thread_id)
            turn_id = _interruptible_turn_id(client, thread_id, live)
            if isinstance(turn_id, ThreadLive):
                reply = client.call("turn/interrupt", {
                    "threadId": thread_id, "turnId": turn_id.turn_id
                })
                if "error" in reply:
                    reason = _as_str(_as_dict(reply["error"]).get("message")) or "turn/interrupt failed"
                    remaining = _read_live_turn(client, thread_id)
                    if isinstance(remaining, (ThreadLive, ThreadActiveWithoutTurn)):
                        _log_uninterrupted_thread(log_path, thread_id, reason)
                        return RelaunchBlockedByLiveTurn()
                    if isinstance(remaining, ThreadStateUnknown):
                        _log_uninterrupted_thread(log_path, thread_id, reason)
            elif isinstance(turn_id, (ThreadStateUnknown, ThreadActiveWithoutTurn)):
                reason = turn_id.reason if isinstance(turn_id, ThreadStateUnknown) else "no turn id"
                _log_uninterrupted_thread(log_path, thread_id, reason)
                if isinstance(turn_id, ThreadActiveWithoutTurn):
                    return RelaunchBlockedByLiveTurn()
        finally:
            client.close()
    except (ConnectionError, OSError, SystemExit) as exc:
        _log_uninterrupted_thread(log_path, thread_id, str(exc))
    return RelaunchAllowed()


def _load_thread(client: Client, thread_id: str) -> None:
    """Reopen a conversation this server has not loaded.

    A server started after the last one stopped accepts a queued message for a thread it has not
    loaded and never runs it. The first page of the list is enough: reopening a loaded thread is
    what every `follow` does.
    """
    loaded = _as_dict(client.call("thread/loaded/list", {}).get("result")).get("data")
    if not isinstance(loaded, list) or thread_id not in cast("list[object]", loaded):
        _ = _require(client.call("thread/resume", {"threadId": thread_id}), "thread/resume")


def command_send(args: argparse.Namespace) -> int:
    session_dir = _as_str(_attr(args, "session_dir"))
    target = _as_str(_attr(args, "to"))
    with _pending_file(session_dir, target) as pending_file:
        record = _lookup(session_dir, target)
        if _end_marker_path(session_dir, target).exists():
            print(f"codex_mesh: {target} is being ended; message not delivered", file=sys.stderr)
            return 2
        status = record.get("status", "unknown")
        if status == CAPACITY_EXHAUSTED:
            print(
                f"codex_mesh: {target} is out of retries and has no launcher; a message would start a turn nobody watches (codex_mesh.py end --to {target})",
                file=sys.stderr,
            )
            return 2
        if status not in LAUNCHER_ATTACHED_STATUSES:
            raise SystemExit(
                f"codex_mesh: {target} is {status}, not running; read its summary file"
            )
        if status == WAITING_CAPACITY:
            messages = _pending_for_thread(pending_file, record["thread_id"])
            messages.append(_message_text(args))
            _write_pending(pending_file, record["thread_id"], messages)
            print(f"message for {target} will be delivered when the seat resumes")
            return 0
        client = Client(_seat_port(session_dir, record), f"send-{os.getpid()}")
        try:
            _load_thread(client, record["thread_id"])
            _ = _require(
                client.call(
                    "thread/queue/add",
                    {
                        "threadId": record["thread_id"],
                        "clientUserMessageId": f"mesh-{secrets.token_hex(6)}",
                        "input": [{"type": "text", "text": _message_text(args)}],
                    },
                ),
                "thread/queue/add",
            )
        finally:
            client.close()
    print(f"queued for {target}")
    return 0


def command_steer(args: argparse.Namespace) -> int:
    session_dir = _as_str(_attr(args, "session_dir"))
    target = _as_str(_attr(args, "to"))
    record = _lookup(session_dir, target)
    turn_id = record.get("turn_id", "")
    status = record.get("status", "unknown")
    if not turn_id or status != "running":
        detail = f"status {status}"
        raise SystemExit(
            f"codex_mesh: {target} has no running turn to steer ({detail}); use `send`"
        )
    client = Client(_seat_port(session_dir, record), f"steer-{os.getpid()}")
    message = _message_text(args)
    reply = client.call(
        "turn/steer",
        {
            "threadId": record["thread_id"],
            "expectedTurnId": turn_id,
            "input": [{"type": "text", "text": message}],
        },
    )
    # The roster learns a new turn id only from the `start` loop streaming
    # `turn/started`. Once that launcher is gone, a queued message still opens
    # a new turn and the recorded id goes stale; the server names the live one
    # in its refusal, so steer that turn and record it.
    live = _live_turn_from_mismatch(reply)
    if live:
        reply = client.call(
            "turn/steer",
            {
                "threadId": record["thread_id"],
                "expectedTurnId": live,
                "input": [{"type": "text", "text": message}],
            },
        )
        if not reply.get("error"):
            _update_roster(
                session_dir,
                target,
                {"thread_id": record["thread_id"], "turn_id": live, "status": "running"},
            )
    _ = _require(reply, "turn/steer")
    client.close()
    print(f"steered {target}")
    return 0


def _live_turn_from_mismatch(reply: RpcMessage) -> str:
    """The active turn id a `turn/steer` refusal names, or "" for any other
    reply."""
    text = _as_str(_as_dict(reply.get("error")).get("message"))
    marker = "but found `"
    start = text.find(marker)
    if start < 0:
        return ""
    rest = text[start + len(marker) :]
    end = rest.find("`")
    return rest[:end] if end > 0 else ""


def command_end(args: argparse.Namespace) -> int:
    """Release a resident delegate: drop the messages queued for it, interrupt
    the turn it is on, then leave the marker its `start` loop polls for.
    Nothing here starts a server -- a delegate whose server is already gone is
    ended by the marker alone.

    The queue goes first because the server opens a new turn for each queued
    message on its own: a delegate ended with messages waiting comes back, one
    turn per message, long after its `start` has returned."""
    session_dir = _as_str(_attr(args, "session_dir"))
    target = _as_str(_attr(args, "to"))
    record = _lookup(session_dir, target)
    status = record.get("status", "unknown")
    if status not in ENDABLE_STATUSES:
        print(f"{target} is {status}; nothing to end")
        return 0
    with _pending_file(session_dir, target) as pending_file:
        _end_marker_path(session_dir, target).touch()
        _ = pending_file.seek(0)
        _ = pending_file.truncate()
    availability = _server_availability(session_dir)
    dropped = 0
    if isinstance(availability, LiveServer):
        client = Client(availability.port, f"end-{os.getpid()}")
        thread_id = record["thread_id"]
        dropped = _drop_queued_messages(client, thread_id)
        # A queued message opens a turn the `start` loop never saw, so the
        # roster's id may be stale; the server names the live one.
        state = _interruptible_turn_id(client, thread_id, _read_live_turn(client, thread_id))
        if isinstance(state, ThreadLive):
            # Best effort: a turn that finished between the read and this call
            # answers with an error, and the marker below ends it either way.
            _ = client.call(
                "turn/interrupt", {"threadId": thread_id, "turnId": state.turn_id}
            )
        client.close()
        if isinstance(state, ThreadStateUnknown):
            print(f"codex_mesh: {target}: thread {thread_id} could not be read ({state.reason})", file=sys.stderr)
            return 1
        if isinstance(state, ThreadActiveWithoutTurn):
            print(f"codex_mesh: {target}: thread {thread_id} could not be interrupted (no turn id)", file=sys.stderr)
            return 1
    suffix = f", dropped {dropped} queued message(s)" if dropped else ""
    print(f"ending {target}{suffix}")
    return 0


# Upper bound on queue reads while draining; a list returns one page at a time.
END_QUEUE_DRAIN_ROUNDS = 50


def _drop_queued_messages(client: Client, thread_id: str) -> int:
    """Delete every message queued for a thread; returns how many went."""
    dropped = 0
    for _ in range(END_QUEUE_DRAIN_ROUNDS):
        listed = client.call("thread/queue/list", {"threadId": thread_id})
        items = _as_dict(listed.get("result")).get("data")
        if not isinstance(items, list) or not items:
            break
        for item in cast("list[object]", items):
            submission = _as_str(_as_dict(item).get("id"))
            if not submission:
                continue
            reply = client.call(
                "thread/queue/delete",
                {"threadId": thread_id, "queuedSubmissionId": submission},
            )
            if not reply.get("error"):
                dropped += 1
    return dropped


def _read_live_turn(client: Client, thread_id: str) -> ThreadState:
    """Read the active turn without sending text into the thread."""
    reply = client.call("thread/read", {"threadId": thread_id, "includeTurns": True})
    error = reply.get("error")
    if error:
        return ThreadStateUnknown(_as_str(_as_dict(error).get("message")) or "thread/read failed")
    thread = _as_dict(_as_dict(reply.get("result")).get("thread"))
    if not thread:
        return ThreadStateUnknown("thread/read returned no thread")
    status = _as_str(_as_dict(thread.get("status")).get("type"))
    # app-server/src/thread_status.rs: systemError means the last turn failed; none is active.
    if status in ("idle", "notLoaded", "systemError"):
        return ThreadIdle()
    if status != "active":
        return ThreadStateUnknown(f"unrecognized status {status or '(missing)'}")
    turns = thread.get("turns")
    if isinstance(turns, list):
        for turn in reversed(cast("list[object]", turns)):
            record = _as_dict(turn)
            if record.get("status") == "inProgress":
                turn_id = _as_str(record.get("id"))
                if turn_id:
                    return ThreadLive(turn_id)
    # Active is enough to refuse a relaunch, even if the server has not yet
    # persisted the new turn in the read response.
    return ThreadActiveWithoutTurn()


def _interruptible_turn_id(client: Client, thread_id: str, state: ThreadState) -> ThreadState:
    if not isinstance(state, ThreadActiveWithoutTurn):
        return state
    # This probe can steer only if the expected id matches. An empty id cannot
    # match an active turn, and the mismatch names the real id.
    reply = client.call("turn/steer", {
        "threadId": thread_id, "expectedTurnId": "", "input": []
    })
    turn_id = _live_turn_from_mismatch(reply)
    if turn_id:
        return ThreadLive(turn_id)
    reread = _read_live_turn(client, thread_id)
    return reread


def command_list(args: argparse.Namespace) -> int:
    session_dir = _as_str(_attr(args, "session_dir"))
    roster = _read_json_object(_session_path(session_dir, ROSTER_FILE))
    if not roster:
        print("no delegates registered")
        return 0
    for name in sorted(roster):
        entry = roster.get(name)
        if not isinstance(entry, dict):
            continue
        record = cast("ThreadRecord", cast("object", entry))
        print(f"{name}\t{record.get('status', '?')}\t{record.get('thread_id', '')}")
    return 0


def command_stop(args: argparse.Namespace) -> int:
    """Reap the session app-server. Nothing else does: it is deliberately
    detached so it outlives each delegate, which means the end of the run is the
    only place that knows it is finished with."""
    session_dir = _as_str(_attr(args, "session_dir"))
    retired_path = _session_path(session_dir, RETIRED_FILE)
    stored = _read_json_object(retired_path).get("servers")
    retired = cast("list[object]", stored) if isinstance(stored, list) else []
    path = _session_path(session_dir, SERVER_FILE)
    stopped = [
        pid
        for record in [*retired, _read_json_object(path)]
        for pid in [_as_dict(record).get("pid")]
        if isinstance(pid, int) and _reap(pid)
    ]
    retired_path.unlink(missing_ok=True)
    path.unlink(missing_ok=True)
    if not stopped:
        print("no app-server running")
        return 0
    print(f"stopped app-server {', '.join(str(pid) for pid in stopped)}")
    return 0


def _reap(pid: int) -> bool:
    """Stop one app-server, escalating to SIGKILL. False if it was not running."""
    if not _pid_alive(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        print(f"could not signal app-server {pid}: {exc}")
        return False
    deadline = time.time() + 5.0
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.2)
    if _pid_alive(pid):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)
    return True


# Where the run-active markers live.
SWEEP_ROOT = Path("/tmp/claude/delegate")
# How long a server and its run folder must have sat untouched before it counts as unused: the
# one figure behind both the server's own watcher and the sweep.
SERVER_IDLE_SECS = 1800.0
# How often a server's watcher wakes to look at its record.
WATCH_WAKE_SECS = 300
# Files in a run folder whose change means the run is still working. `ensure_server` touches the
# server's record on every use.
SWEEP_ACTIVITY_FILES = ("heartbeat.log", "board.log", ROSTER_FILE, SERVER_LOG, SERVER_FILE)
SERVER_COMMAND = "codex app-server --listen ws://127.0.0.1:"


@dataclass(frozen=True)
class ServerInUse:
    reason: str


@dataclass(frozen=True)
class ServerUnused:
    reason: str


@dataclass(frozen=True)
class ServerUnknown:
    """Nothing proves it idle, so it is left alone like one in use."""

    reason: str


SweepVerdict = ServerInUse | ServerUnused | ServerUnknown


@dataclass(frozen=True)
class ServerFacts:
    """What the sweep gathered about one running app-server without talking to it."""

    pid: int
    port: int
    age_secs: float
    clients: int
    # The run folder that started it; None when its log handle names none.
    session_dir: Path | None
    folder_exists: bool
    marked_active: bool
    # Seats in the run folder's roster whose launcher process is alive.
    live_launchers: tuple[str, ...]
    # Seconds since the run folder's newest activity file changed; None when it has none.
    quiet_secs: float | None
    # Its run has moved to a newer server and listed this one in RETIRED_FILE.
    retired: bool = False


def _sweep_verdict(facts: ServerFacts, busy_threads: Callable[[], tuple[str, ...]]) -> SweepVerdict:
    """Whether anything still needs this app-server. Every doubt counts as in use, and the server
    is asked about its conversations last, so none can be called unused without that answer."""
    if facts.clients:
        return ServerInUse(f"{facts.clients} client connection(s) on its port")
    if facts.session_dir is None:
        return ServerUnknown("its log names no run folder, so this launcher did not start it")
    if facts.retired:
        # The run's marker, launchers and activity belong to its newer server. Only a seat still
        # attached here holds a connection, and a turn whose launcher died shows as a conversation.
        if busy := busy_threads():
            return ServerInUse("a conversation is not idle: " + "; ".join(busy))
        return ServerUnused("retired by its run; no client and no conversation mid-turn")
    if facts.live_launchers:
        # A resident seat waits between turns with nothing on the socket.
        return ServerInUse("a seat's launcher is still running: " + ", ".join(facts.live_launchers))
    if facts.marked_active:
        return ServerInUse("its run has an active marker")
    if facts.age_secs < SERVER_IDLE_SECS:
        return ServerInUse(f"started {facts.age_secs / 60:.0f} min ago")
    if facts.quiet_secs is not None and facts.quiet_secs < SERVER_IDLE_SECS:
        return ServerInUse(f"its run folder changed {facts.quiet_secs / 60:.0f} min ago")
    if busy := busy_threads():
        return ServerInUse("a conversation is not idle: " + "; ".join(busy))
    if not facts.folder_exists:
        folder = "its run folder was deleted"
    else:
        folder = "no activity file" if facts.quiet_secs is None else f"quiet for {facts.quiet_secs / 3600:.1f} h"
    return ServerUnused(f"no client, no launcher, no active marker, no conversation mid-turn, {folder}")


def _running_servers() -> list[tuple[int, int, float]]:
    """(pid, port, age in seconds) of every app-server listening on loopback."""
    listed = subprocess.run(["ps", "-eo", "pid=,etimes=,args="], capture_output=True, text=True, check=True)
    servers: list[tuple[int, int, float]] = []
    for line in listed.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) < 3:
            continue
        _head, found, port = fields[2].partition(SERVER_COMMAND)
        if found and port.isdigit() and fields[0].isdigit() and fields[1].isdigit():
            servers.append((int(fields[0]), int(port), float(fields[1])))
    return servers


def _client_counts() -> dict[int, int]:
    """Established connections per local port. A server's clients land on its own port."""
    listed = subprocess.run(["ss", "-Htn", "state", "established"], capture_output=True, text=True, check=True)
    counts: dict[int, int] = {}
    for line in listed.stdout.splitlines():
        fields = line.split()
        port = fields[2].rpartition(":")[2] if len(fields) >= 4 else ""
        if port.isdigit():
            counts[int(port)] = counts.get(int(port), 0) + 1
    return counts


def _server_folder(pid: int) -> Path | None:
    """The run folder that started this server: `ensure_server` points its stderr at that folder's
    log, and the handle outlives a rewritten record or a deleted folder. None when it names no such log."""
    try:
        target = os.readlink(f"/proc/{pid}/fd/2")
    except OSError:
        return None
    log = Path(target.removesuffix(" (deleted)"))
    return log.parent if log.name == SERVER_LOG else None


def _marked_active(root: Path, folder: Path) -> bool:
    """Whether a run-active marker names `folder`. A marker that cannot be read counts as naming it."""
    active = root / "active"
    for marker in sorted(active.iterdir()) if active.is_dir() else []:
        try:
            lines = marker.read_text(encoding="utf-8").splitlines()
        except OSError:
            return True
        if lines and lines[0].strip() and Path(lines[0].strip()).resolve() == folder.resolve():
            return True
    return False


def _quiet_secs(folder: Path, now: float) -> float | None:
    changed = [(folder / name).stat().st_mtime for name in SWEEP_ACTIVITY_FILES if (folder / name).exists()]
    return now - max(changed) if changed else None


def _live_launchers(folder: Path) -> tuple[str, ...]:
    roster = _read_json_object(folder / ROSTER_FILE)
    return tuple(
        name
        for name, entry in sorted(roster.items())
        for pid in [_as_dict(entry).get("launcher_pid")]
        if isinstance(pid, int) and _pid_alive(pid)
    )


def _retired_pids(folder: Path) -> set[int]:
    stored = _read_json_object(folder / RETIRED_FILE).get("servers")
    records = cast("list[object]", stored) if isinstance(stored, list) else []
    return {pid for record in records for pid in [_as_dict(record).get("pid")] if isinstance(pid, int)}


def _forget_retired(folder: Path, pid: int) -> None:
    """Drop a stopped server from RETIRED_FILE: `stop` signals every pid listed there, and by the
    run's end this one could belong to another process."""
    path = folder / RETIRED_FILE
    stored = _read_json_object(path).get("servers")
    if not isinstance(stored, list):
        return
    records = cast("list[object]", stored)
    kept = [record for record in records if _as_dict(record).get("pid") != pid]
    if len(kept) != len(records):
        _ = path.write_text(json.dumps({"servers": kept}, indent=2), encoding="utf-8")


def _roster_threads(folder: Path) -> dict[str, str]:
    """Thread id to seat name for every conversation the run folder's roster lists."""
    roster = _read_json_object(folder / ROSTER_FILE)
    named = {_as_str(_as_dict(entry).get("thread_id")): name for name, entry in sorted(roster.items())}
    return {thread_id: name for thread_id, name in named.items() if thread_id}


def _busy_threads(port: int, seats: dict[str, str]) -> tuple[str, ...]:
    """Conversations on this server that are not idle, or why they could not be read.

    The server lists what it has loaded, so a turn left running by a launcher killed at its time
    limit is seen with no client connected and no roster. `seats` adds the roster's conversations
    and names them."""
    try:
        client = Client(port, f"sweep-{os.getpid()}")
    except (ConnectionError, OSError, SystemExit) as exc:
        return (f"the server could not be asked ({str(exc) or exc.__class__.__name__})",)
    try:
        listed = client.call("thread/loaded/list", {})
        result = _as_dict(listed.get("result"))
        loaded = result.get("data")
        if listed.get("error") or not isinstance(loaded, list) or result.get("nextCursor"):
            return ("its loaded conversations could not be listed in full",)
        thread_ids = {*seats, *(_as_str(thread_id) for thread_id in cast("list[object]", loaded))} - {""}
        busy: list[str] = []
        for thread_id in sorted(thread_ids):
            state = _read_live_turn(client, thread_id)
            if not isinstance(state, ThreadIdle):
                busy.append(f"{seats.get(thread_id, thread_id)}: {state.__class__.__name__}")
        return tuple(busy)
    except (ConnectionError, OSError, SystemExit) as exc:
        return (f"the server stopped answering ({str(exc) or exc.__class__.__name__})",)
    finally:
        client.close()


def _judge(root: Path | None, pid: int, port: int, age: float,
           clients: int) -> tuple[SweepVerdict, Path | None]:
    """The verdict on one running app-server, and the run folder that started it.

    `root` holds the run-active markers. A server judging itself passes None: a stopped server
    comes back at the next use, so a run that is only between dispatches does not need it kept.
    """
    folder = _server_folder(pid)
    exists = folder is not None and folder.is_dir()
    facts = ServerFacts(
        pid, port, age, clients, folder, exists,
        marked_active=root is not None and folder is not None and _marked_active(root, folder),
        live_launchers=_live_launchers(folder) if folder is not None and exists else (),
        quiet_secs=_quiet_secs(folder, time.time()) if folder is not None and exists else None,
        retired=folder is not None and exists and pid in _retired_pids(folder),
    )
    seats = _roster_threads(folder) if folder is not None and exists else {}
    return _sweep_verdict(facts, lambda: _busy_threads(port, seats)), folder


def _stop_unused(root: Path | None, pid: int, port: int, folder: Path) -> bool:
    """Stop a server a first look called unused, if a second look at this moment agrees.

    The second look runs under the run folder's server lock, so a launcher reaching
    `ensure_server` meanwhile waits and then starts a server of its own. A deleted folder has no
    launcher to wait and is not created again for the lock."""
    with _server_lock(str(folder)) if folder.is_dir() else contextlib.nullcontext():
        try:
            running = {(found, at): age for found, at, age in _running_servers()}
            clients = _client_counts()
        except (OSError, subprocess.CalledProcessError):
            return False
        # A pid that no longer runs this server on this port is not the process that was judged.
        age = running.get((pid, port))
        if age is None:
            return False
        verdict, _folder = _judge(root, pid, port, age, clients.get(port, 0))
        if not isinstance(verdict, ServerUnused) or not _reap(pid):
            return False
        if folder.is_dir():
            _forget_retired(folder, pid)
        record = folder / SERVER_FILE
        if _read_json_object(record).get("pid") == pid:
            record.unlink(missing_ok=True)
        return True


def command_idle_stop(args: argparse.Namespace) -> int:
    """Stop this run folder's app-server if nothing uses it. Its watcher runs this.

    Exit 0 tells the watcher to leave: the server was stopped here, or no longer runs. Exit 1
    keeps it watching. A server that is kept has its record touched, so the watcher sleeps through
    the next SERVER_IDLE_SECS before it asks again. Any doubt keeps the server, as in the sweep.
    """
    folder = Path(_as_str(_attr(args, "session_dir")))
    pid, port = _as_int(_attr(args, "pid")), _as_int(_attr(args, "port"))
    record = folder / SERVER_FILE
    try:
        running = {(found, at): age for found, at, age in _running_servers()}
        clients = _client_counts()
    except (OSError, subprocess.CalledProcessError):
        running, clients = None, {}
    if running is not None:
        age = running.get((pid, port))
        if age is None:
            return 0
        verdict, _folder = _judge(None, pid, port, age, clients.get(port, 0))
        if isinstance(verdict, ServerUnused) and _stop_unused(None, pid, port, folder):
            _log_server(str(folder), f"app-server {pid} stopped itself: {verdict.reason}")
            return 0
    if _read_json_object(record).get("pid") == pid:
        with contextlib.suppress(OSError):
            os.utime(record)
    return 1


def command_sweep(args: argparse.Namespace) -> int:
    """Print every running app-server with whether anything still needs it. Stops one only
    under `--stop`, and then only a server two looks in a row called unused."""
    root = Path(_as_str(_attr(args, "root")) or SWEEP_ROOT)
    stop = _attr(args, "stop") is True
    try:
        servers, clients = _running_servers(), _client_counts()
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"codex_mesh: sweep: cannot list servers or connections ({exc}); nothing judged", file=sys.stderr)
        return 1
    unused = stopped = 0
    for pid, port, age in sorted(servers):
        verdict, folder = _judge(root, pid, port, age, clients.get(port, 0))
        label = {ServerInUse: "in use", ServerUnused: "unused", ServerUnknown: "unknown"}[type(verdict)]
        reason = verdict.reason
        if isinstance(verdict, ServerUnused) and folder is not None:
            unused += 1
            if stop and _stop_unused(root, pid, port, folder):
                stopped += 1
                label = "stopped"
            elif stop:
                label, reason = "kept", "unused at the first look, not stopped at the second"
        print(f"{pid}\t{port}\t{label}\t{reason}\t{folder or '-'}")
    tail = f"{stopped} stopped" if stop else "report only, nothing was stopped"
    print(f"{len(servers)} app-server(s), {unused} unused; {tail}")
    return 0


# Delivers the sign-in change notice to the session holding a run.
SEND_SCRIPT = Path(__file__).resolve().parent.parent / "message" / "send.py"
NOTICE_TIMEOUT_SECS = 120


def _marker_sessions(root: Path, folder: Path) -> list[str]:
    """The sessions whose run-active marker names `folder`: the marker's file name is the session id."""
    active = root / "active"
    holders: list[str] = []
    for marker in sorted(active.iterdir()) if active.is_dir() else []:
        try:
            lines = marker.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        if lines and lines[0].strip() and Path(lines[0].strip()).resolve() == folder.resolve():
            holders.append(marker.name)
    return holders


def _notify_holder(session_id: str, line: str) -> None:
    try:
        sent = subprocess.run(
            [sys.executable, str(SEND_SCRIPT), "--to", f"session:{session_id}", "--from", "codex-sign-in",
             "--summary", "Codex sign-in changed", "--text", line],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=NOTICE_TIMEOUT_SECS, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"codex_mesh: could not tell session {session_id}: {exc}", file=sys.stderr)
        return
    if sent.returncode:
        print(f"codex_mesh: could not tell session {session_id}: {sent.stderr.strip()}", file=sys.stderr)


def command_signin_changed(args: argparse.Namespace) -> int:
    """Move every run off an app-server started before the Codex sign-in last changed.

    A path watch on the sign-in file runs this the moment it changes; `ensure_server` makes the
    same check at each launch for a run this missed. Each run's record is retired, so its next
    dispatch starts a server that reads the current sign-in. A seat mid-turn keeps the old server
    until the turn ends, and a resident seat then moves itself. The old server is stopped now if
    nothing uses it, otherwise by its watcher once nothing does. Run twice for one change, the
    second run finds nothing older and moves nothing.
    """
    root = Path(_as_str(_attr(args, "root")) or SWEEP_ROOT)
    changed = _sign_in_changed_at()
    if changed is None:
        print("codex_mesh: no Codex sign-in file; nothing moved")
        return 0
    at = datetime.fromtimestamp(changed).astimezone().strftime("%H:%M %Z")
    moved = 0
    for record_path in sorted(root.glob(f"*/{SERVER_FILE}")):
        folder = record_path.parent
        with _server_lock(str(folder)):
            record = _read_json_object(record_path)
            port, pid = record.get("port"), record.get("pid")
            if not (isinstance(port, int) and isinstance(pid, int) and _pid_alive(pid) and _holds_old_sign_in(pid)):
                continue
            _retire_record(str(folder), record)
        stopped = _stop_unused(None, pid, port, folder)
        if not stopped:
            _ = _start_watcher(str(folder), pid, port)
        old = "is stopped" if stopped else "stops once nothing uses it"
        line = (f"Codex sign-in changed at {at}: run {folder.name}'s app-server {pid} started before it, so the "
                + "run's seats move to a new server, an idle seat at its next turn and a seat mid-turn when "
                + f"that turn ends; the old server {old}.")
        _log_server(str(folder), line)
        for session_id in _marker_sessions(root, folder):
            _notify_holder(session_id, line)
        print(line)
        moved += 1
    print(f"{moved} run(s) moved off the old sign-in")
    return 0


def command_serve(args: argparse.Namespace) -> int:
    port, _fresh = ensure_server(_as_str(_attr(args, "session_dir")))
    print(port)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="verb", required=True)

    serve = subparsers.add_parser("serve", help="start the session app-server")
    _ = serve.add_argument("--session-dir", required=True)
    serve.set_defaults(handler=command_serve)

    start = subparsers.add_parser("start", help="launch a delegate and block")
    _ = start.add_argument("--session-dir", required=True)
    _ = start.add_argument("--name", required=True)
    _ = start.add_argument("--cwd", required=True)
    _ = start.add_argument("--prompt-file", required=True)
    _ = start.add_argument("--summary-file", required=True)
    _ = start.add_argument(
        "--reply-file",
        default="",
        help="append replies here; the summary file is then the delegate's own",
    )
    _ = start.add_argument("--log-file", required=True)
    _ = start.add_argument("--model", default="")
    _ = start.add_argument("--effort", default="")
    _ = start.add_argument("--service-tier", default="")
    _ = start.add_argument("--sandbox", default="danger-full-access")
    _ = start.add_argument("--timeout", type=float, default=86400.0)
    _ = start.add_argument(
        "--resident",
        action="store_true",
        help="stay attached across turns, delivering each reply, until `end`",
    )
    start.set_defaults(handler=command_start)

    follow = subparsers.add_parser("follow", help="run one turn on a finished delegate")
    _ = follow.add_argument("--session-dir", required=True)
    _ = follow.add_argument("--to", required=True)
    _ = follow.add_argument("--claim-pid", type=int, default=0)
    _ = follow.add_argument("--message-file", required=True)
    _ = follow.add_argument("--summary-file", required=True)
    _ = follow.add_argument("--reply-file", default="")
    _ = follow.add_argument("--log-file", required=True)
    _ = follow.add_argument("--model", default="")
    _ = follow.add_argument("--effort", default="")
    _ = follow.add_argument("--service-tier", default="")
    _ = follow.add_argument("--timeout", type=float, default=86400.0)
    follow.set_defaults(handler=command_follow)

    can_follow = subparsers.add_parser("can-follow", help="check whether a seat can take a follow-up")
    _ = can_follow.add_argument("--session-dir", required=True)
    _ = can_follow.add_argument("--to", required=True)
    _ = can_follow.add_argument("--claim-pid", type=int, default=0)
    can_follow.set_defaults(handler=command_can_follow)

    release_follow = subparsers.add_parser("release-follow", help="release an unstarted follow claim")
    _ = release_follow.add_argument("--session-dir", required=True)
    _ = release_follow.add_argument("--to", required=True)
    _ = release_follow.add_argument("--claim-pid", type=int, required=True)
    release_follow.set_defaults(handler=command_release_follow)

    send = subparsers.add_parser("send", help="queue a message for a delegate")
    _ = send.add_argument("--session-dir", required=True)
    _ = send.add_argument("--to", required=True)
    send_body = send.add_mutually_exclusive_group(required=True)
    _ = send_body.add_argument("--message")
    _ = send_body.add_argument("--message-file", help="read the message from this file")
    send.set_defaults(handler=command_send)

    steer = subparsers.add_parser("steer", help="interrupt a delegate's running turn")
    _ = steer.add_argument("--session-dir", required=True)
    _ = steer.add_argument("--to", required=True)
    steer_body = steer.add_mutually_exclusive_group(required=True)
    _ = steer_body.add_argument("--message")
    _ = steer_body.add_argument("--message-file", help="read the message from this file")
    steer.set_defaults(handler=command_steer)

    end = subparsers.add_parser("end", help="release a resident delegate")
    _ = end.add_argument("--session-dir", required=True)
    _ = end.add_argument("--to", required=True)
    end.set_defaults(handler=command_end)

    stop = subparsers.add_parser("stop", help="stop the session app-server")
    _ = stop.add_argument("--session-dir", required=True)
    stop.set_defaults(handler=command_stop)

    idle_stop = subparsers.add_parser("idle-stop", help="stop this session's app-server if nothing uses it")
    _ = idle_stop.add_argument("--session-dir", required=True)
    _ = idle_stop.add_argument("--pid", type=int, required=True)
    _ = idle_stop.add_argument("--port", type=int, required=True)
    idle_stop.set_defaults(handler=command_idle_stop)

    sweep = subparsers.add_parser("sweep", help="report app-servers nothing is using")
    _ = sweep.add_argument("--root", default="")
    _ = sweep.add_argument("--stop", action="store_true", help="stop each server two looks in a row call unused")
    sweep.set_defaults(handler=command_sweep)

    signin_changed = subparsers.add_parser(
        "signin-changed", help="move every run off an app-server older than the Codex sign-in")
    _ = signin_changed.add_argument("--root", default="")
    signin_changed.set_defaults(handler=command_signin_changed)

    roster = subparsers.add_parser("list", help="print the delegate roster")
    _ = roster.add_argument("--session-dir", required=True)
    roster.set_defaults(handler=command_list)

    args = parser.parse_args(argv)
    handler = cast("Callable[[argparse.Namespace], int]", _attr(args, "handler"))
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
