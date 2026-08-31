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
         With --resident, stay attached across turns instead: each finished
         turn is printed to stdout and written to the summary file, the
         thread keeps accepting `send`, and only `end` releases the block.
  send   Queue a message for a named delegate, delivered at its next turn.
  steer  Inject into a named delegate's running turn.
  end    Finish a resident delegate: interrupt its running turn, if any, and
         release its `start`.
  stop   Stop the session's app-server.
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
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TextIO, TypedDict, cast, final

SERVER_FILE = "mesh_server.json"
ROSTER_FILE = "mesh_roster.json"
CONNECT_TIMEOUT_SECS = 20
CALL_TIMEOUT_SECS = 300
SERVER_START_TIMEOUT_SECS = 30
# How long to keep watching after a turn ends for a queued turn already in flight.
QUEUE_GRACE_SECS = 3.0
# A resident delegate sits between turns with nothing on the socket, so its loop
# wakes this often to look for the end marker.
RESIDENT_POLL_SECS = 1.0


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


class ThreadRecord(TypedDict):
    thread_id: str
    turn_id: str
    status: str


# The three places an untyped value enters this module. Each is confined to one
# helper so the rest of the file stays fully typed.


def _loads(text: str) -> object:
    """json.loads as an `object`, so callers must narrow before use."""
    try:
        return json.loads(text)  # pyright: ignore[reportAny]
    except json.JSONDecodeError:
        return None


def _attr(args: argparse.Namespace, key: str) -> object:
    return getattr(args, key)  # pyright: ignore[reportAny]


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
        except (TimeoutError, OSError):
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
# Session state. Three delegates write the roster concurrently, so every
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
        roster[name] = record
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


def _live_server_port(session_dir: str) -> int | None:
    """The session app-server's port when it is running, else None.

    `ensure_server` would start one; the verbs that only ever wind things down
    must not, or ending a delegate whose server already died would leave a
    fresh server behind for nothing.
    """
    record = _read_json_object(_session_path(session_dir, SERVER_FILE))
    port = record.get("port")
    pid = record.get("pid")
    if isinstance(port, int) and isinstance(pid, int) and _pid_alive(pid):
        return port
    return None


def _end_marker_path(session_dir: str, name: str) -> Path:
    return _session_path(session_dir, f"{name}.end")


def _message_text(args: argparse.Namespace) -> str:
    """The message for send/steer: --message-file wins, so a multi-line body
    never has to survive shell quoting."""
    path = _as_str(_attr(args, "message_file"))
    if path:
        return Path(path).read_text(encoding="utf-8")
    return _as_str(_attr(args, "message"))


def _deliver_reply(
    name: str, index: int, text: str, failure: str, summary_path: Path, log: TextIO
) -> None:
    """Hand one finished resident turn to whoever is watching.

    stdout is for a caller polling the terminal this runs in; the summary file
    is for a reader that comes later; the log line keeps the heartbeat honest.
    """
    if text and failure:
        body = f"{text}\n[error] {failure}"
    elif text:
        body = text
    elif failure:
        body = f"[error] {failure}"
    else:
        body = f"The delegate {name} produced no reply."
    _ = summary_path.write_text(body + "\n", encoding="utf-8")
    print(f"=== reply from {name} ({index}) ===\n{body}\n=== end reply ===", flush=True)
    _ = log.write(f"[{_now_stamp()}] reply {index} delivered\n")
    log.flush()


def ensure_server(session_dir: str) -> int:
    """Return the port for this session's app-server, starting it if needed."""
    path = _session_path(session_dir, SERVER_FILE)
    record = _read_json_object(path)
    port = record.get("port")
    pid = record.get("pid")
    if isinstance(port, int) and isinstance(pid, int) and _pid_alive(pid):
        return port

    chosen = _free_port()
    Path(session_dir).mkdir(parents=True, exist_ok=True)
    log_path = _session_path(session_dir, "mesh_server.log")
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
            raise SystemExit(f"codex_mesh: app-server exited immediately; see {log_path}")
        try:
            probe = WebSocket(chosen)
        except (OSError, ConnectionError):
            time.sleep(0.4)
            continue
        probe.close()
        served: ServerRecord = {"port": chosen, "pid": process.pid}
        _ = path.write_text(json.dumps(served, indent=2), encoding="utf-8")
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


def command_start(args: argparse.Namespace) -> int:
    session_dir = _as_str(_attr(args, "session_dir"))
    name = _as_str(_attr(args, "name"))
    timeout = _as_float(_attr(args, "timeout"), 86400.0)
    port = ensure_server(session_dir)
    prompt = Path(_as_str(_attr(args, "prompt_file"))).read_text(encoding="utf-8")
    log_path = Path(_as_str(_attr(args, "log_file")))
    summary_path = Path(_as_str(_attr(args, "summary_file")))

    client = Client(port, name)
    thread_params: dict[str, object] = {
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
        thread_params["model"] = model
    started = _require(client.call("thread/start", thread_params), "thread/start")
    thread_id = _as_str(_as_dict(started.get("thread")).get("id"))
    if not thread_id:
        raise SystemExit("codex_mesh: thread/start returned no thread id")

    # Best effort: the server-side name is only a convenience for `codex agents`,
    # and send/steer address delegates through this session's roster, written
    # just below. A rename that fails is not worth losing a delegate over.
    _ = client.call("thread/name/set", {"threadId": thread_id, "name": name})
    _update_roster(
        session_dir, name, {"thread_id": thread_id, "turn_id": "", "status": "starting"}
    )

    turn_params: dict[str, object] = {
        "threadId": thread_id,
        "input": [{"type": "text", "text": prompt}],
    }
    effort = _as_str(_attr(args, "effort"))
    if effort:
        turn_params["effort"] = effort
    turn = _require(client.call("turn/start", turn_params), "turn/start")
    turn_id = _as_str(_as_dict(turn.get("turn")).get("id"))
    _update_roster(
        session_dir,
        name,
        {"thread_id": thread_id, "turn_id": turn_id, "status": "running"},
    )

    resident = _attr(args, "resident") is True
    end_marker = _end_marker_path(session_dir, name)
    end_marker.unlink(missing_ok=True)

    final_answer = ""
    failure = ""
    replies = 0
    deadline = time.time() + timeout
    with log_path.open("a", encoding="utf-8") as log:
        _ = log.write(f"[{_now_stamp()}] mesh: {name} thread {thread_id}\n")
        log.flush()
        while time.time() < deadline:
            # A resident thread is silent between turns, so a full-deadline wait
            # would sleep straight through `end`; wake often enough to see it.
            if resident:
                wait_until = min(deadline, time.time() + RESIDENT_POLL_SECS)
            else:
                wait_until = deadline
            frame = client.next_frame(wait_until)
            if frame is None:
                if resident and end_marker.exists():
                    break
                continue
            method = frame.get("method")
            params = _as_dict(frame.get("params"))
            if _as_str(params.get("threadId")) not in ("", thread_id):
                continue
            if method == "turn/started":
                live = _as_str(_as_dict(params.get("turn")).get("id"))
                if live:
                    turn_id = live
                    _update_roster(
                        session_dir,
                        name,
                        {"thread_id": thread_id, "turn_id": live, "status": "running"},
                    )
            elif method == "item/completed":
                item = _as_dict(params.get("item"))
                _ = log.write(f"[{_now_stamp()}] {_describe_item(item)}\n")
                log.flush()
                if _as_str(item.get("type")) == "agentMessage":
                    text = _as_str(item.get("text"))
                    if text:
                        final_answer = text
            elif method in ("turn/completed", "turn/failed"):
                if method == "turn/completed":
                    completed = _as_dict(params.get("turn"))
                    failure = _as_str(_as_dict(completed.get("error")).get("message"))
                else:
                    detail = _as_str(_as_dict(params.get("error")).get("message"))
                    failure = detail or "turn failed"
                if not resident:
                    if (
                        method == "turn/failed"
                        or failure
                        or not _more_work_coming(client, thread_id, deadline)
                    ):
                        break
                    # A peer's message is waiting. The server starts that turn
                    # by itself, so the only thing to do is keep streaming --
                    # exiting here would strand the message and kill the
                    # delegate mid-reply.
                    continue
                # Resident: every finished turn is a reply to deliver, an error
                # included -- the caller is waiting on this one and would
                # otherwise wait forever. The thread stays open for the next
                # message; only `end` closes it.
                replies += 1
                _deliver_reply(name, replies, final_answer, failure, summary_path, log)
                final_answer = ""
                failure = ""
                _update_roster(
                    session_dir,
                    name,
                    {"thread_id": thread_id, "turn_id": "", "status": "running"},
                )
                if end_marker.exists():
                    break
        else:
            failure = f"no turn/completed within {timeout:.0f}s"

    # A background thread has no output redirect, so the summary file is the
    # only place the caller can read the delegate's answer. A resident thread
    # wrote each reply as it landed; only one that never replied needs this.
    if not resident or replies == 0:
        if not final_answer:
            final_answer = failure or f"The delegate {name} produced no summary."
        _ = summary_path.write_text(final_answer + "\n", encoding="utf-8")
    _update_roster(
        session_dir,
        name,
        {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "status": "failed" if failure else "done",
        },
    )
    client.close()
    if failure:
        print(f"codex_mesh: {name}: {failure}", file=sys.stderr)
        return 1
    return 0


def command_send(args: argparse.Namespace) -> int:
    session_dir = _as_str(_attr(args, "session_dir"))
    target = _as_str(_attr(args, "to"))
    record = _lookup(session_dir, target)
    status = record.get("status", "unknown")
    if status != "running":
        # The thread outlives the launcher, so the server would happily start a
        # turn for this message -- with nothing streaming it and nothing writing
        # the summary. Silent work is worse than a refusal.
        raise SystemExit(
            f"codex_mesh: {target} is {status}, not running; read its summary file"
        )
    port = ensure_server(session_dir)
    client = Client(port, f"send-{os.getpid()}")
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
    port = ensure_server(session_dir)
    client = Client(port, f"steer-{os.getpid()}")
    _ = _require(
        client.call(
            "turn/steer",
            {
                "threadId": record["thread_id"],
                "expectedTurnId": turn_id,
                "input": [{"type": "text", "text": _message_text(args)}],
            },
        ),
        "turn/steer",
    )
    client.close()
    print(f"steered {target}")
    return 0


def command_end(args: argparse.Namespace) -> int:
    """Release a resident delegate: interrupt the turn it is on, then leave the
    marker its `start` loop polls for. Nothing here starts a server -- a
    delegate whose server is already gone is ended by the marker alone."""
    session_dir = _as_str(_attr(args, "session_dir"))
    target = _as_str(_attr(args, "to"))
    record = _lookup(session_dir, target)
    status = record.get("status", "unknown")
    if status != "running":
        print(f"{target} is {status}; nothing to end")
        return 0
    turn_id = record.get("turn_id", "")
    port = _live_server_port(session_dir)
    if turn_id and port is not None:
        client = Client(port, f"end-{os.getpid()}")
        # Best effort: a turn that finished between the roster read and this
        # call answers with an error, and the marker below ends it either way.
        _ = client.call(
            "turn/interrupt", {"threadId": record["thread_id"], "turnId": turn_id}
        )
        client.close()
    _end_marker_path(session_dir, target).touch()
    print(f"ending {target}")
    return 0


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
    path = _session_path(session_dir, SERVER_FILE)
    record = _read_json_object(path)
    pid = record.get("pid")
    if not isinstance(pid, int) or not _pid_alive(pid):
        print("no app-server running")
        path.unlink(missing_ok=True)
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        print(f"could not signal app-server {pid}: {exc}")
        return 0
    deadline = time.time() + 5.0
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.2)
    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    path.unlink(missing_ok=True)
    print(f"stopped app-server {pid}")
    return 0


def command_serve(args: argparse.Namespace) -> int:
    print(ensure_server(_as_str(_attr(args, "session_dir"))))
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
    _ = start.add_argument("--log-file", required=True)
    _ = start.add_argument("--model", default="")
    _ = start.add_argument("--effort", default="")
    _ = start.add_argument("--sandbox", default="danger-full-access")
    _ = start.add_argument("--timeout", type=float, default=86400.0)
    _ = start.add_argument(
        "--resident",
        action="store_true",
        help="stay attached across turns, delivering each reply, until `end`",
    )
    start.set_defaults(handler=command_start)

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

    roster = subparsers.add_parser("list", help="print the delegate roster")
    _ = roster.add_argument("--session-dir", required=True)
    roster.set_defaults(handler=command_list)

    args = parser.parse_args(argv)
    handler = cast("Callable[[argparse.Namespace], int]", _attr(args, "handler"))
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
