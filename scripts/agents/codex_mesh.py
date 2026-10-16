#!/usr/bin/env python3
"""codex_mesh.py — Run codex delegates as addressable threads on one app-server.

Why this exists rather than agent_exec.sh: a delegate launched with `codex exec`
is unreachable. Nothing outside its own process can hand it a message, so peers
coordinate only through the board and a delegate that goes down the wrong path
stays there until its turn ends. `codex app-server` fixes that by hosting every
delegate as a thread on one local websocket: any process that can open a socket
can queue a message for a named delegate, or steer one mid-turn.

The pieces this depends on, each verified against codex 0.162.0:

  * `initialize` must declare `capabilities.experimentalApi`. Without it the
    queue methods return -32600 with no hint that a flag is missing.
  * `sandbox` takes the SandboxMode STRING ("danger-full-access"), not the
    SandboxPolicy object the generated schema shows for other fields.
  * `thread/queue/add` reaches a thread between turns; `turn/steer` interrupts a
    running one and needs the turn id the delegate is currently on.

`start` deliberately blocks for the delegate's lifetime. Launches that return
immediately would break every caller that waits on a pid, so this stays in the
foreground and implement.sh's `wait`, heartbeat watch, and pass recording work
unchanged.

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
  compact
         Compact a finished named thread and print its context size before and
         after compaction.
         A compaction that does not finish in time is interrupted, so the
         worker can still take a follow-up (exit 1). Exit 3 means a turn is
         still live on the thread and no follow-up can be sent.
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
import re
import secrets
import shlex
import signal
import socket
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NoReturn, NotRequired, TextIO, TypedDict, cast, final

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib import unit_runs
from scripts.usage import agents_log
from scripts.whoami.agent_accounts import (
    workers_home, codex_home, codex_home_display, user_zone,
    KnownCodexAccount, UnknownCodexAccount, RefusingCodexAccount, codex_account, codex_home_role_label,
    codex_home_matches_label,
)


if TYPE_CHECKING:
    from scripts.accounts.moves import RequestedMove


@dataclass(frozen=True)
class ServingCodexServer:
    """The process and sign-in retained before a request reaches its server."""

    port: int
    pid: int
    home: Path
    account: RefusingCodexAccount
    started_here: bool = False


@dataclass(frozen=True)
class NotBoundCodexServer:
    """No serving process was identified; the selected home remains context only."""

    home: Path = field(default_factory=workers_home)
    reason: str = "serving server is not bound"
    started_here: bool = False

    @property
    def account(self) -> UnknownCodexAccount:
        return UnknownCodexAccount(self.home, self.reason)


CodexServingState = ServingCodexServer | NotBoundCodexServer


_STARTED_SERVERS: set[tuple[int, int]] = set()


def _capture_serving_server(session_dir: str, port: int) -> CodexServingState:
    records = [record for record in _recorded_run_servers(session_dir)
               if record.get("port") == port and isinstance(record.get("pid"), int)]
    live = [record for record in records if _pid_alive(_as_int(record.get("pid")))]
    candidates = live
    identities = {(_as_int(record.get("pid")), server_record_home(record)) for record in candidates}
    if len(identities) != 1:
        return NotBoundCodexServer(reason="serving process cannot be identified")
    pid, home = identities.pop()
    return ServingCodexServer(port, pid, home, codex_account(home), (port, pid) in _STARTED_SERVERS)


def _bind_serving_server(client: Client, session_dir: str, *, started_here: bool = False) -> CodexServingState:
    serving = _capture_serving_server(session_dir, client.port)
    if started_here:
        serving = replace(serving, started_here=True)
    client.serving = serving
    client.session_dir = session_dir
    return serving


class ServingRequestFailure(SystemExit):
    """An RPC failed with its serving identity retained, including removed records."""

    def __init__(self, detail: str, serving: CodexServingState, reported: bool) -> None:
        super().__init__(detail)
        self.serving: CodexServingState = serving
        self.reported: bool = reported


def _serving_request(
    client: Client, method: str, params: dict[str, object], *, submits_work: bool = False,
) -> dict[str, object]:
    opened = time.time()
    try:
        return _require(client.call(method, params), method)
    except (ConnectionError, OSError, SystemExit) as error:
        serving = client.serving
        detail = str(error) or type(error).__name__
        proven = submits_work and QUOTA_REFUSAL in detail and (
            serving.started_here or time.time() - opened > RETRY_FAST_FAILURE_SECS)
        if proven:
            _owe_quota_continuation(client, _as_str(params.get("threadId")))
            _report_quota_refusal(client.name, serving, detail)
        raise ServingRequestFailure(detail, serving, proven) from error

SERVER_FILE = "mesh_server.json"
SERVER_LOG = "mesh_server.log"
ROSTER_FILE = "mesh_roster.json"
BOARD_HELPER = Path(__file__).resolve().parent.parent / "delegate" / "board.sh"
# Servers dropped by _retire_server, kept so `stop` can still reap them. A
# retired server is abandoned rather than signalled: it may still be finishing a
# peer delegate's turn, and the end of the run is the only moment that knows
# nobody needs it.
RETIRED_FILE = "mesh_retired.json"
LOCK_FILE = "mesh_server.lock"
CONNECT_TIMEOUT_SECS = 20
CALL_TIMEOUT_SECS = 300
THREAD_RELEASE_TIMEOUT_SECS = 20.0
# How long `compact` waits for a thread to read idle: after it interrupts a compaction turn that
# did not finish, and before it refuses a finished worker that still has a live turn.
COMPACT_IDLE_WAIT_SECS = 60
COMPACT_IDLE_POLL_SECS = 1
# `compact` exits with this when the thread still has a live turn, so no follow-up can be sent.
COMPACT_TURN_STILL_LIVE = 3
SERVER_START_TIMEOUT_SECS = 30
# How long to keep watching after a turn ends for a queued turn already in flight.
QUEUE_GRACE_SECS = 3.0
# A resident delegate sits between turns with nothing on the socket, so its loop
# wakes this often to look for the end marker.
RESIDENT_POLL_SECS = 1.0
# Production gives a server five seconds to stop; disposable child processes need no long grace.
REAP_WAIT_SECS = 5.0
REAP_POLL_SECS = 0.2
# A launcher waiting on a turn wakes this often to check that the turn has not ended unseen.
UNSEEN_END_CHECK_SECS = 60.0
UNSEEN_END_CALL_SECS = 10.0
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
ENDABLE_STATUSES = frozenset((
    "starting", "running", "done", "failed", WAITING_CAPACITY, CAPACITY_EXHAUSTED,
))


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
    codex_home: NotRequired[str]


@dataclass(frozen=True)
class LiveServer:
    port: int


@dataclass(frozen=True)
class ServerRestartRequired:
    """The recorded app-server is gone; resume from the thread rollout."""


ServerAvailability = LiveServer | ServerRestartRequired


WorkerRole = Literal["impl", "test", "fix", "review"]
WORKER_ROLES = ("impl", "test", "fix", "review")


@dataclass(frozen=True)
class AssignedDelegateRole:
    """A delegate-run thread whose current assignment belongs in the roster."""

    role: WorkerRole


@dataclass(frozen=True)
class RolelessThread:
    """A mesh thread outside a delegate run, such as a consultation friend."""


RosterRole = AssignedDelegateRole | RolelessThread


class FollowableRecord(TypedDict):
    thread_id: str
    status: Literal["done", "failed"]
    revision: NotRequired[int]
    role: NotRequired[WorkerRole]
    context_tokens: NotRequired[int | Literal["not_measured_since_compaction"]]
    instructions_file: NotRequired[str]
    continuation_owed: NotRequired[bool]


# `port` is the app-server the worker's launcher is attached to. It stays the worker's way in while
# that launcher lives, even after the run has moved to a newer server.
class ActiveRecord(TypedDict):
    thread_id: str
    status: Literal["running"]
    revision: NotRequired[int]
    turn_id: str
    launcher_pid: NotRequired[int]
    port: NotRequired[int]
    role: NotRequired[WorkerRole]
    context_tokens: NotRequired[int | Literal["not_measured_since_compaction"]]
    instructions_file: NotRequired[str]
    continuation_owed: NotRequired[bool]


class StartingRecord(TypedDict):
    thread_id: str
    status: Literal["starting"]
    previous_role: NotRequired[WorkerRole]
    previous_status: NotRequired[Literal["done", "failed"]]
    revision: NotRequired[int]
    launcher_pid: NotRequired[int]
    port: NotRequired[int]
    role: NotRequired[WorkerRole]
    context_tokens: NotRequired[int | Literal["not_measured_since_compaction"]]
    instructions_file: NotRequired[str]
    continuation_owed: NotRequired[bool]


class WaitingCapacityRecord(TypedDict):
    thread_id: str
    status: Literal["waiting_capacity"]
    revision: NotRequired[int]
    launcher_pid: int
    port: NotRequired[int]
    role: NotRequired[WorkerRole]
    context_tokens: NotRequired[int | Literal["not_measured_since_compaction"]]
    instructions_file: NotRequired[str]
    continuation_owed: NotRequired[bool]


class ExhaustedRecord(TypedDict):
    thread_id: str
    status: Literal["capacity_exhausted"]
    revision: NotRequired[int]
    role: NotRequired[WorkerRole]
    context_tokens: NotRequired[int | Literal["not_measured_since_compaction"]]
    instructions_file: NotRequired[str]
    continuation_owed: NotRequired[bool]


class EndedRecord(TypedDict):
    thread_id: str
    status: Literal["ended"]
    revision: NotRequired[int]
    role: NotRequired[WorkerRole]
    context_tokens: NotRequired[int | Literal["not_measured_since_compaction"]]
    instructions_file: NotRequired[str]
    continuation_owed: NotRequired[bool]


ThreadRecord = (FollowableRecord | ActiveRecord | StartingRecord |
                WaitingCapacityRecord | ExhaustedRecord | EndedRecord)


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
class EndedThread:
    thread_id: str


@dataclass(frozen=True)
class InvalidThread:
    reason: str


RosterThread = (FollowableThread | ActiveThread | WaitingCapacityThread |
                StartingThread | ExhaustedThread | EndedThread | InvalidThread)


@dataclass(frozen=True)
class KnownContextSize:
    """A context size reported by the app-server or saved in the roster."""

    tokens: int


@dataclass(frozen=True)
class ContextSizeUnavailable:
    """No trustworthy context size was present at this protocol boundary."""


ContextSize = KnownContextSize | ContextSizeUnavailable


@dataclass(frozen=True)
class NotMeasuredSinceCompaction:
    """Compaction finished, but the next turn has not measured the worker's size."""


RecordedContextSize = KnownContextSize | NotMeasuredSinceCompaction | ContextSizeUnavailable


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
    if status == "ended":
        return EndedThread(thread_id)
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


def _roster_role(value: object) -> RosterRole:
    role = _as_str(value)
    if not role:
        return RolelessThread()
    if role not in WORKER_ROLES:
        raise SystemExit(
            f"codex_mesh: role must be impl, test, fix, or review; got '{role}'"
        )
    return AssignedDelegateRole(role)


def _worker_role(args: argparse.Namespace) -> WorkerRole:
    role = _roster_role(_attr(args, "role"))
    if isinstance(role, RolelessThread):
        raise SystemExit("codex_mesh: this delegate action requires a role")
    return role.role


def _dispatch_lens(args: argparse.Namespace) -> agents_log.Lens | None:
    lens = _attr(args, "lens")
    return cast(agents_log.Lens, lens) if lens else None


def _record_assignment(thread_id: str, role: WorkerRole, lens: agents_log.Lens | None,
                       only_if_changed: bool = False) -> None:
    try:
        now = datetime.now(timezone.utc)
        if only_if_changed:
            log = agents_log.assignments(agents_log.state_dir() / "agents.jsonl")
            current = agents_log.role_at(log, thread_id, now)
            if isinstance(current, agents_log.Assignment) and current.role == role and current.lens == lens:
                return
        agents_log.append(thread_id, role, lens, now)
    except Exception as exc:
        # Recording must never interrupt dispatch.
        print("agents_log: " + " ".join(str(exc).splitlines()), file=sys.stderr)


def _as_float(value: object, fallback: float) -> float:
    return float(value) if isinstance(value, (int, float)) else fallback


def _context_size_from_record(value: object) -> RecordedContextSize:
    tokens = _as_dict(value).get("context_tokens")
    if tokens == "not_measured_since_compaction":
        return NotMeasuredSinceCompaction()
    if isinstance(tokens, int) and tokens >= 0:
        return KnownContextSize(tokens)
    return ContextSizeUnavailable()


def _context_size_from_usage(params: dict[str, object]) -> ContextSize:
    usage = _as_dict(_as_dict(params.get("tokenUsage")).get("last"))
    tokens = usage.get("inputTokens")
    if isinstance(tokens, int) and tokens >= 0:
        return KnownContextSize(tokens)
    return ContextSizeUnavailable()


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
        self.port: int = port
        self.name: str = name
        self.session_dir: str = ""
        self.serving: CodexServingState = NotBoundCodexServer()
        self.turn_submitted_at: float = time.time()
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


def _post_board_status(session_dir: str, name: str, message: str) -> None:
    """Best-effort lifecycle narration under the worker's board slot."""
    slot = name.rsplit("-", 1)[-1]
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        _ = subprocess.run(
            ["bash", str(BOARD_HELPER), "post", session_dir, slot, "status", message],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )


@dataclass(frozen=True)
class RosterRevision:
    value: int


def _roster_revision(value: object) -> RosterRevision:
    revision = _as_dict(value).get("revision")
    if isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0:
        return RosterRevision(revision)
    return RosterRevision(0)


def _advance_roster_revision(entry: dict[str, object]) -> None:
    entry["revision"] = _roster_revision(entry).value + 1


@dataclass(frozen=True)
class RosterOnThread:
    thread_id: str


@dataclass(frozen=True)
class RosterAbsent:
    pass


@dataclass(frozen=True)
class RosterEndedOnThread:
    thread_id: str


@dataclass(frozen=True)
class RosterUnchanged:
    thread_id: str
    revision: RosterRevision


ExpectedRoster = RosterOnThread | RosterAbsent | RosterEndedOnThread | RosterUnchanged


def _write_roster_record(handle: TextIO, roster: dict[str, object], name: str,
                        record: ThreadRecord) -> None:
    previous = _as_dict(roster.get(name))
    stored = dict(record)
    if "continuation_owed" not in record and previous.get("thread_id") == record["thread_id"]:
        owed = previous.get("continuation_owed")
        if isinstance(owed, bool):
            stored["continuation_owed"] = owed
    size = _context_size_from_record(record if "context_tokens" in record else previous)
    role = record.get("role", previous.get("role"))
    if isinstance(role, str) and role in WORKER_ROLES:
        stored["role"] = role
    instructions_file = record.get("instructions_file")
    if instructions_file is None and previous.get("thread_id") == record["thread_id"]:
        instructions_file = previous.get("instructions_file")
    if isinstance(instructions_file, str) and instructions_file:
        stored["instructions_file"] = instructions_file
    if record["status"] in ("starting", "running", WAITING_CAPACITY):
        pid = record.get("launcher_pid", previous.get("launcher_pid"))
        if isinstance(pid, int) and pid > 0:
            stored["launcher_pid"] = pid
        port = record.get("port", previous.get("port"))
        if isinstance(port, int) and port > 0:
            stored["port"] = port
    if (
        previous.get("status") == "ended"
        and previous.get("thread_id") == record["thread_id"]
        and record["status"] != "ended"
    ):
        stored = dict(previous)
    if isinstance(size, KnownContextSize):
        stored["context_tokens"] = size.tokens
    elif isinstance(size, NotMeasuredSinceCompaction):
        stored["context_tokens"] = "not_measured_since_compaction"
    stored["revision"] = _roster_revision(previous).value + 1
    roster[name] = stored
    _ = handle.seek(0)
    _ = handle.truncate()
    _ = handle.write(json.dumps(roster, indent=2, sort_keys=True))
    handle.flush()


def _update_roster(session_dir: str, name: str, record: ThreadRecord) -> None:
    path = _session_path(session_dir, ROSTER_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _ = handle.seek(0)
        text = handle.read()
        roster = _as_dict(_loads(text)) if text.strip() else {}
        _write_roster_record(handle, roster, name, record)


def _update_roster_if_expected(session_dir: str, name: str, record: ThreadRecord,
                              expected: ExpectedRoster) -> bool:
    """Compare the name's owner and write while holding the same roster lock."""
    path = _session_path(session_dir, ROSTER_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _ = handle.seek(0)
        text = handle.read()
        roster = _as_dict(_loads(text)) if text.strip() else {}
        previous = _as_dict(roster.get(name))
        if isinstance(expected, RosterAbsent):
            matches = name not in roster
        else:
            matches = previous.get("thread_id") == expected.thread_id
            if isinstance(expected, RosterEndedOnThread):
                matches = matches and previous.get("status") == "ended"
            elif isinstance(expected, RosterUnchanged):
                matches = matches and _roster_revision(previous) == expected.revision
        if not matches:
            return False
        _write_roster_record(handle, roster, name, record)
        return True


def _set_roster_role(session_dir: str, name: str, role: WorkerRole) -> None:
    """Record the work assigned to an existing worker without changing its state."""
    path = _session_path(session_dir, ROSTER_FILE)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _ = handle.seek(0)
        roster = _as_dict(_loads(handle.read()))
        entry = _as_dict(roster.get(name))
        if entry:
            entry["role"] = role
            _advance_roster_revision(entry)
            roster[name] = entry
            _ = handle.seek(0)
            _ = handle.truncate()
            _ = handle.write(json.dumps(roster, indent=2, sort_keys=True))
            handle.flush()


def _mark_roster_ended(session_dir: str, name: str) -> None:
    """Make one worker terminal while retaining its identity and metadata."""
    path = _session_path(session_dir, ROSTER_FILE)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _ = handle.seek(0)
        roster = _as_dict(_loads(handle.read()))
        entry = _as_dict(roster.get(name))
        if entry:
            entry["status"] = "ended"
            _advance_roster_revision(entry)
            roster[name] = entry
            _ = handle.seek(0)
            _ = handle.truncate()
            _ = handle.write(json.dumps(roster, indent=2, sort_keys=True))
            handle.flush()


def _mark_all_roster_ended(session_dir: str) -> None:
    """Make every worker in this run terminal while retaining its metadata."""
    path = _session_path(session_dir, ROSTER_FILE)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _ = handle.seek(0)
        roster = _as_dict(_loads(handle.read()))
        for name, value in roster.items():
            entry = _as_dict(value)
            if entry:
                entry["status"] = "ended"
                _advance_roster_revision(entry)
                roster[name] = entry
        _ = handle.seek(0)
        _ = handle.truncate()
        _ = handle.write(json.dumps(roster, indent=2, sort_keys=True))
        handle.flush()


def _lookup(session_dir: str, name: str) -> ThreadRecord:
    roster = _read_json_object(_session_path(session_dir, ROSTER_FILE))
    entry = roster.get(name)
    if not isinstance(entry, dict):
        known = ", ".join(sorted(roster)) or "(none)"
        raise SystemExit(f"codex_mesh: no delegate named '{name}'. Known: {known}")
    return cast("ThreadRecord", cast("object", entry))


def _lookup_state(session_dir: str, name: str) -> RosterThread:
    return _roster_thread(_lookup(session_dir, name))


def _resume_params(session_dir: str, name: str, thread_id: str) -> dict[str, object]:
    """Restore a roster thread's standing instructions whenever it is reopened."""
    params: dict[str, object] = {"threadId": thread_id}
    record = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
    instructions_file = _as_str(record.get("instructions_file"))
    if instructions_file:
        try:
            params["developerInstructions"] = Path(instructions_file).read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise OSError(f"cannot read instructions file {instructions_file}: {exc}") from exc
    return params


def _roster_still_on_thread(session_dir: str, name: str, thread_id: str) -> bool:
    entry = _read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name)
    state = _roster_thread(entry)
    return not isinstance(state, InvalidThread) and state.thread_id == thread_id


def _claim_follow(
    session_dir: str, name: str, thread_id: str, launcher_pid: int, role: WorkerRole,
    lens: agents_log.Lens | None = None,
) -> bool:
    """Take a finished roster entry before another follow can take it."""
    path = _session_path(session_dir, ROSTER_FILE)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _ = handle.seek(0)
        roster = _as_dict(_loads(handle.read()))
        entry = _as_dict(roster.get(name))
        current = _roster_thread(entry)
        stale = isinstance(current, (ActiveThread, StartingThread)) and (
            current.launcher_pid > 0 and not _pid_alive(current.launcher_pid)
        )
        if isinstance(current, InvalidThread) or not (
            isinstance(current, FollowableThread) or stale
        ) or current.thread_id != thread_id:
            return False
        previous_status = current.status if isinstance(current, FollowableThread) else "failed"
        previous_role = _roster_role(entry.get("role"))
        claimed = dict(entry)
        _ = claimed.pop("turn_id", None)
        _ = claimed.pop("previous_role", None)
        if isinstance(previous_role, AssignedDelegateRole):
            claimed["previous_role"] = previous_role.role
        claimed.update({
            "thread_id": thread_id,
            "status": "starting",
            "launcher_pid": launcher_pid,
            "previous_status": previous_status,
            "role": role,
        })
        _advance_roster_revision(claimed)
        roster[name] = claimed
        _ = handle.seek(0)
        _ = handle.truncate()
        _ = handle.write(json.dumps(roster, indent=2, sort_keys=True))
        handle.flush()
        _record_assignment(thread_id, role, lens)
        return True


def _restore_follow_claim(session_dir: str, name: str, launcher_pid: int,
                          live_turn: str = "", *, finished: bool = False,
                          expected_thread_id: str = "") -> None:
    """Release only this launcher's unstarted claim; retain a peer's live turn."""
    path = _session_path(session_dir, ROSTER_FILE)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _ = handle.seek(0)
        roster = _as_dict(_loads(handle.read()))
        entry = _as_dict(roster.get(name))
        status = entry.get("status")
        if expected_thread_id and entry.get("thread_id") != expected_thread_id:
            return
        if entry.get("launcher_pid") != launcher_pid or not (
            status == "starting" or (finished and status == "running")
        ):
            return
        thread_id = _as_str(entry.get("thread_id"))
        previous_status = entry.get("previous_status")
        previous_role = _roster_role(entry.get("previous_role"))
        restored = dict(entry)
        _advance_roster_revision(restored)
        if isinstance(previous_role, AssignedDelegateRole):
            restored["role"] = previous_role.role
        else:
            _ = restored.pop("role", None)
        if live_turn and not finished:
            restored.update({
                "thread_id": thread_id,
                "status": "running",
                "turn_id": live_turn,
                "launcher_pid": launcher_pid,
            })
            roster[name] = restored
        else:
            _ = restored.pop("previous_status", None)
            _ = restored.pop("previous_role", None)
            _ = restored.pop("turn_id", None)
            _ = restored.pop("launcher_pid", None)
            _ = restored.pop("port", None)
            restored.update({
                "thread_id": thread_id,
                "status": previous_status if previous_status in FOLLOWABLE_STATES else "failed",
            })
            roster[name] = restored
        _ = handle.seek(0)
        _ = handle.truncate()
        _ = handle.write(json.dumps(roster, indent=2, sort_keys=True))
        handle.flush()
        if isinstance(previous_role, AssignedDelegateRole) and entry.get("role") != previous_role.role:
            _record_assignment(thread_id, previous_role.role, None)


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


def _worker_port(session_dir: str, record: ThreadRecord) -> int:
    """The server that reaches a worker: the one its launcher is attached to while that launcher
    lives, which after a sign-in change can be one the run has retired; otherwise the run's own.

    A worker whose launcher lives is never given a new server. Its conversation stays open on the
    server it is on, and a second server cannot open it while that one holds it.
    """
    port, pid = record.get("port"), record.get("launcher_pid")
    if not (isinstance(pid, int) and pid > 0 and _pid_alive(pid)):
        return ensure_server(session_dir)[0]
    if isinstance(port, int):
        return port
    # A launcher that has not recorded its server yet, or one started before workers recorded it.
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


def _recorded_run_servers(session_dir: str) -> list[dict[str, object]]:
    """Retired process records followed by the run's current process record."""
    stored = _read_json_object(_session_path(session_dir, RETIRED_FILE)).get("servers")
    records = [_as_dict(record) for record in (cast("list[object]", stored) if isinstance(stored, list) else [])]
    records.append(_read_json_object(_session_path(session_dir, SERVER_FILE)))
    return records


def _run_servers(session_dir: str) -> list[int]:
    """Ports of the run's servers still running: those it retired, oldest first, then its current one."""
    ports: list[int] = []
    for record in _recorded_run_servers(session_dir):
        port, pid = record.get("port"), record.get("pid")
        if isinstance(port, int) and isinstance(pid, int) and _pid_alive(pid) and port not in ports:
            ports.append(port)
    return ports


def _loaded_thread_ids(client: Client, deadline: float) -> set[str]:
    threads: set[str] = set()
    cursor = ""
    seen: set[str] = set()
    while time.monotonic() < deadline:
        params: dict[str, object] = {"cursor": cursor} if cursor else {}
        listed = _require(client.call("thread/loaded/list", params,
                                      timeout=max(0.01, deadline - time.monotonic())), "thread/loaded/list")
        data = listed.get("data")
        if not isinstance(data, list):
            raise SystemExit("server returned no loaded-thread list")
        for item in cast(list[object], data):
            if not isinstance(item, str) or not item:
                raise SystemExit("server returned an invalid loaded-thread id")
            threads.add(item)
        next_cursor = listed.get("nextCursor")
        if next_cursor is None or next_cursor == "":
            return threads
        if not isinstance(next_cursor, str) or next_cursor in seen:
            raise SystemExit("server returned an invalid loaded-thread cursor")
        cursor = next_cursor
        seen.add(cursor)
    raise SystemExit("loaded-thread list timed out")


def _server_holds(port: int, thread_id: str) -> bool:
    try:
        client = Client(port, f"worker-lookup-{os.getpid()}")
    except (ConnectionError, OSError, SystemExit):
        return False
    try:
        return thread_id in _loaded_thread_ids(client, time.monotonic() + THREAD_RELEASE_TIMEOUT_SECS)
    except (ConnectionError, OSError, SystemExit):
        return False
    finally:
        client.close()


def _note_worker_port(session_dir: str, name: str, launcher_pid: int, port: int) -> None:
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
        _advance_roster_revision(entry)
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


ACCOUNT_MOVE_CONTINUATION = (
    "Your turn was interrupted to move your Codex account; any command you had running "
    "was stopped with it, so run it again. Continue where you stopped."
)
ACCOUNT_MOVE_RESUME_TIMEOUT_SECS = 30.0


class StoredAccountMove(TypedDict):
    thread_id: str
    turn_id: str
    label: str
    move_id: str


@dataclass(frozen=True)
class AccountMoveMarker:
    thread_id: str
    turn_id: str
    label: str
    move_id: str


@dataclass(frozen=True)
class NoAccountMove:
    """This thread has no pending account-move interrupt."""


def _account_move_path(session_dir: str, name: str) -> Path:
    return _session_path(session_dir, f"{name}.account-move.json")


def _account_move_result_path(session_dir: str, name: str) -> Path:
    return _session_path(session_dir, f"{name}.account-move-result.json")


def _write_account_move_json(path: Path, value: Mapping[str, object]) -> None:
    temporary = path.with_name(path.name + f".{secrets.token_hex(8)}.tmp")
    try:
        _ = temporary.write_text(json.dumps(value), encoding="utf-8")
        _ = temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_account_move(session_dir: str, name: str, thread_id: str) -> AccountMoveMarker | NoAccountMove:
    record = _read_json_object(_account_move_path(session_dir, name))
    thread, turn, label, move_id = (record.get(key) for key in ("thread_id", "turn_id", "label", "move_id"))
    if (thread == thread_id and isinstance(thread, str) and thread
            and isinstance(turn, str) and turn and isinstance(label, str) and label
            and isinstance(move_id, str) and move_id):
        return AccountMoveMarker(thread, turn, label, move_id)
    return NoAccountMove()


def _finish_account_move(session_dir: str, name: str, marker: AccountMoveMarker) -> None:
    current = _read_account_move(session_dir, name, marker.thread_id)
    if isinstance(current, AccountMoveMarker) and current.move_id == marker.move_id:
        _account_move_path(session_dir, name).unlink(missing_ok=True)
    _write_account_move_json(_account_move_result_path(session_dir, name),
                            {"move_id": marker.move_id, "resumed": True})
    _post_board_status(session_dir, name, f"account moved to {marker.label}; resumed")


def _fail_account_move(session_dir: str, name: str, marker: AccountMoveMarker, reason: str) -> None:
    _write_account_move_json(_account_move_result_path(session_dir, name),
                            {"move_id": marker.move_id, "error": reason})


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


class WorkerPendingMessages(TypedDict):
    thread_id: str
    messages: list[str]
    accepting_messages: bool


def _messages_closed(handle: TextIO, thread_id: str) -> bool:
    _ = handle.seek(0)
    stored = _as_dict(_loads(handle.read()))
    return stored.get("thread_id") == thread_id and stored.get("accepting_messages") is False


def _write_pending(handle: TextIO, thread_id: str, messages: list[str], *, accepting_messages: bool = True) -> None:
    stored: WorkerPendingMessages = {
        "thread_id": thread_id, "messages": messages, "accepting_messages": accepting_messages,
    }
    _ = handle.seek(0)
    _ = handle.truncate()
    _ = handle.write(json.dumps(stored))
    handle.flush()


def _close_messages_if_empty(session_dir: str, name: str, thread_id: str) -> bool:
    """Exclude senders while claiming an empty inbox and closing its launcher."""
    with _pending_file(session_dir, name) as handle:
        if _pending_for_thread(handle, thread_id):
            return False
        _write_pending(handle, thread_id, [], accepting_messages=False)
        return True


def _close_messages_and_keep_pending(session_dir: str, name: str, thread_id: str) -> int:
    """Close the launcher's inbox while keeping its parked messages for the next launch."""
    with _pending_file(session_dir, name) as handle:
        messages = _pending_for_thread(handle, thread_id)
        _write_pending(handle, thread_id, messages, accepting_messages=False)
        return len(messages)


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

    The workers of a phase launch in one message and reach `ensure_server`
    within the same second. Unlocked, each reads no record, each starts a server
    and each writes the file: all but one are then orphaned, listening and
    unreachable, and a retry that started a fresh server for every failed worker
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

    Only when the record still names the server that failed: a peer worker racing
    the same recovery may already have replaced it, and dropping its new server
    would restart the cycle it just ended. The process is left running -- see
    RETIRED_FILE -- and `stop` reaps it with the rest at the end of the run.

    The roster is deliberately untouched. Its entries for other names may point
    at threads that are live on a server this worker knows nothing about, and this
    worker rewrites its own entry when it re-attaches.
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


def _record_started_server(session_dir: str, served: ServerRecord) -> None:
    """Install a new current record without orphaning the server it replaces.

    The caller holds the server lock. A live prior server stays reachable through
    RETIRED_FILE, whose watcher stops it once its conversations are idle. A prior
    record whose process is already gone needs only to be removed.
    """
    path = _session_path(session_dir, SERVER_FILE)
    previous = _read_json_object(path)
    same_server = (
        previous.get("pid") == served["pid"]
        and previous.get("port") == served["port"]
    )
    if previous and not same_server:
        previous_pid = previous.get("pid")
        if isinstance(previous_pid, int) and _pid_alive(previous_pid):
            _retire_record(session_dir, previous)
        else:
            path.unlink(missing_ok=True)
    _ = path.write_text(json.dumps(served, indent=2), encoding="utf-8")


def _log_server(session_dir: str, line: str) -> None:
    with contextlib.suppress(OSError), _session_path(session_dir, SERVER_LOG).open("a", encoding="utf-8") as log:
        _ = log.write(f"[{_now_stamp()}] mesh: {line}\n")


def _sign_in_changed_at(home: Path) -> float | None:
    """When Codex's sign-in file last changed. Only its change time is read, never its contents."""
    try:
        return (home / "auth.json").stat().st_mtime
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


def _holds_old_sign_in(pid: int, home: Path) -> bool:
    """Whether the server started before Codex's sign-in last changed. It read the sign-in once, at
    start, and cannot refresh a token the change revoked."""
    changed = _sign_in_changed_at(home)
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


def server_record_home(record: dict[str, object]) -> Path:
    """A recorded home, or the default used by launchers before homes were recorded."""
    recorded = _as_str(record.get("codex_home"))
    return Path(recorded) if recorded else Path.home() / ".codex"


def ensure_server(session_dir: str, home: Path | None = None) -> tuple[int, bool]:
    """This session's app-server port, and whether this call is what started it.

    A caller that started the server just proved the provider was reachable
    through a process nothing else has touched, so a failure against it is the
    provider's answer. A caller that attached to one already running has proved
    nothing of the kind -- see _retry_warranted.
    """
    selected_home = workers_home() if home is None else home
    with _server_lock(session_dir):
        path = _session_path(session_dir, SERVER_FILE)
        record = _read_json_object(path)
        port = record.get("port")
        pid = record.get("pid")
        if isinstance(port, int) and isinstance(pid, int) and _pid_alive(pid):
            recorded_home = server_record_home(record)
            matching_home = recorded_home == selected_home
            old_sign_in = matching_home and _holds_old_sign_in(pid, recorded_home)
            if matching_home and not old_sign_in:
                # The record's change time is when the server was last used; its watcher reads it.
                os.utime(path)
                return port, False
            # A sign-in change `signin-changed` did not catch. The workers still on this server keep
            # it until they let go; its watcher then stops it.
            _retire_record(session_dir, record)
            cause = (f"holds the old sign-in at {codex_home_display(selected_home)}" if old_sign_in
                     else f"no longer serves the workers home {codex_home_display(selected_home)}")
            _log_server(session_dir, f"app-server {pid} {cause}; retired, starting a new one")
            if not _stop_unused_with_lock_held(None, pid, port):
                _ = _start_watcher(session_dir, pid, port)
        return _start_server(session_dir, selected_home), True


def _start_server(session_dir: str, workers_home: Path) -> int:
    """Start this session's app-server and record it. The caller holds the server lock."""
    chosen = _free_port()
    log_path = _session_path(session_dir, SERVER_LOG)
    with log_path.open("ab") as log_handle:
        process = subprocess.Popen(
            ["codex", "-c", "thread_unload_delay_secs=0", "app-server", "--listen", f"ws://127.0.0.1:{chosen}"],
            stdout=subprocess.DEVNULL,
            stderr=log_handle,
            start_new_session=True,
            env={**os.environ, "CODEX_HOME": str(workers_home)},
        )
    served: ServerRecord = {"port": chosen, "pid": process.pid, "codex_home": str(workers_home)}
    try:
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
            _record_started_server(session_dir, served)
            _STARTED_SERVERS.add((chosen, process.pid))
            _ = _start_watcher(session_dir, process.pid, chosen)
            return chosen
        raise SystemExit(f"codex_mesh: app-server did not accept connections on {chosen}")
    except BaseException:
        _discard_failed_server_start(session_dir, served, process)
        raise


def _discard_failed_server_start(
    session_dir: str, served: ServerRecord, process: subprocess.Popen[bytes]
) -> None:
    """Leave neither a process nor a current record after a failed start."""
    if process.poll() is None:
        with contextlib.suppress(OSError):
            process.terminate()
        try:
            _ = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                process.kill()
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                _ = process.wait(timeout=5)
    else:
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            _ = process.wait(timeout=0)
    path = _session_path(session_dir, SERVER_FILE)
    if _record_names_server(_read_json_object(path), served["pid"], served["port"]):
        path.unlink(missing_ok=True)


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


def _ended_unseen(client: Client, thread_id: str) -> bool:
    """True when the thread is idle with nothing queued, so no turn is left to end.

    A launcher that reads this while it waits for a completion notice has missed
    the notice. The read asks for no turns, so it stays small on a long thread.
    A call that is refused or times out says nothing, and the launcher keeps waiting.
    """
    try:
        listed = client.call("thread/queue/list", {"threadId": thread_id}, UNSEEN_END_CALL_SECS)
        items = _as_dict(listed.get("result")).get("data")
        if isinstance(items, list) and items:
            return False
        reply = client.call(
            "thread/read", {"threadId": thread_id, "includeTurns": False}, UNSEEN_END_CALL_SECS)
    except SystemExit:
        return False
    thread = _as_dict(_as_dict(reply.get("result")).get("thread"))
    return _as_str(_as_dict(thread.get("status")).get("type")) == "idle"


def _more_work_coming(client: Client, thread_id: str, deadline: float) -> bool:
    """True when a turn that just ended is not the delegate's last.

    Peer sends land in the worker's locked pending file, checked by the caller
    before it closes the launcher. This check covers older server-queued work:
    a message still queued, or a dequeued turn whose start notice is in flight.
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
class TurnContextWindowExceeded:
    detail: str


@dataclass(frozen=True)
class TurnFailed:
    detail: str


TurnOutcome = TurnCompleted | TurnRefusedForCapacity | TurnContextWindowExceeded | TurnFailed


@dataclass(frozen=True)
class RunCompleted:
    """The delegate ended without a failure."""


@dataclass(frozen=True)
class FailedBeforeThread:
    """No thread id exists, so a fast retry may repeat the original prompt."""

    failure: str
    seconds: float
    serving: CodexServingState = field(default_factory=NotBoundCodexServer)
    refusal_reported: bool = False


@dataclass(frozen=True)
class FailedWithThread:
    """The thread exists; its original prompt must never be submitted again."""

    thread_id: str
    failure: str
    seconds: float
    serving: CodexServingState = field(default_factory=NotBoundCodexServer)
    refusal_reported: bool = False
    refusal_checked: bool = False


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
    if info == "contextWindowExceeded" or (
        isinstance(info, dict) and "contextWindowExceeded" in info
    ):
        return TurnContextWindowExceeded(detail or "context window exceeded")
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


def _quota_window(detail: str) -> str:
    weekly = bool(re.search(r"\bweek(?:ly)?\b|7[ -]?day", detail, re.IGNORECASE))
    five = bool(re.search(r"5[ -]?(?:hour|hr)|five[ -]?hour", detail, re.IGNORECASE))
    return "weekly" if weekly and not five else "five_hour" if five and not weekly else "unknown"


def _continuation_is_owed(session_dir: str, name: str) -> bool:
    record = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
    return record.get("continuation_owed") is True


def _owe_quota_continuation(client: Client, thread_id: str) -> None:
    if client.session_dir and thread_id:
        _ = _update_roster_if_expected(client.session_dir, client.name,
            {"thread_id": thread_id, "status": "starting", "continuation_owed": True},
            RosterOnThread(thread_id))


def _report_quota_refusal(name: str, serving: CodexServingState, detail: str = "") -> None:
    """Hand the serving identity, named limit and refusal time to the account mover."""
    try:
        account = serving.account
        identity = ([str(account.home), account.login, account.label]
                    if isinstance(account, KnownCodexAccount)
                    else [str(account.home), account.reason])
        done = subprocess.run(
            [sys.executable, str(QUOTA_REPORT), "blocked", *identity,
             "--window", _quota_window(detail), "--refused-at", datetime.now(timezone.utc).isoformat()],
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
    outcome = _run_delegate(args, port, started_here=fresh_server)
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
        outcome = _run_delegate(args, port, started_here=_fresh)
        tested = True
    if isinstance(outcome, (FailedBeforeThread, FailedWithThread)):
        print(f"codex_mesh: {name}: {outcome.failure}", file=sys.stderr)
        if not outcome.refusal_reported and not (isinstance(outcome, FailedWithThread) and outcome.refusal_checked) and _quota_refused(outcome, tested, resident):
            _report_quota_refusal(name, outcome.serving, outcome.failure)
        return 1
    if isinstance(outcome, CapacityRetriesExhausted):
        print(f"codex_mesh: {name}: {outcome.message(name)}", file=sys.stderr)
        return 1
    return 0


def command_follow(args: argparse.Namespace) -> int:
    session_dir = _as_str(_attr(args, "session_dir"))
    name = _as_str(_attr(args, "to"))
    role = _worker_role(args)
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
    if not claim_pid and not _claim_follow(
        session_dir, name, thread_id, os.getpid(), role, _dispatch_lens(args)
    ):
        print(f"codex_mesh: {name} was already claimed", file=sys.stderr)
        return 2
    if claim_pid:
        _set_roster_role(session_dir, name, role)
        _record_assignment(thread_id, role, _dispatch_lens(args), only_if_changed=True)
    owner_pid = claim_pid or os.getpid()
    opened = time.time()
    client: Client | None = None
    live_turn = ""
    try:
        home = workers_home()
        port, _fresh = ensure_server(session_dir, home)
        _note_worker_port(session_dir, name, owner_pid, port)
        released = _release_other_worker_writers(session_dir, name, thread_id, port)
        if isinstance(released, WorkerThreadStateUnknown):
            raise SystemExit(f"thread {thread_id}: {released.reason}; follow-up did not start")
        if isinstance(released, WorkerWaitingOnLiveTurn):
            raise SystemExit(released.reason)
        client = Client(port, name)
        _ = _bind_serving_server(client, session_dir, started_here=_fresh)
        resumed = _resume_worker_thread(args, session_dir, name, thread_id, client, home)
        if resumed.thread_replaced:
            thread_id = resumed.thread_id
            if not _update_roster_if_expected(session_dir, name, {
                "thread_id": thread_id, "status": "starting", "launcher_pid": owner_pid, "port": port,
            }, RosterOnThread(thread_id)):
                _refuse_taken_follow_up(client, thread_id, session_dir)
            _record_assignment(thread_id, role, _dispatch_lens(args))
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


class CompactionTimedOut(Exception):
    """The compaction turn did not finish in time, and may still be live on the thread."""

    def __init__(self, message: str, turn_id: str) -> None:
        super().__init__(message)
        # Empty when the server never named the compaction turn.
        self.turn_id: str = turn_id


def _compact_thread(client: Client, thread_id: str) -> KnownContextSize:
    """Start compaction and return the usage update for its compact turn.

    Codex 0.162.0 schemas retain `thread/compacted` but mark it deprecated in
    favor of the `contextCompaction` item. Servers in the same release may send
    either completion shape, so both identify the compact turn here.
    """
    _ = _require(
        client.call("thread/compact/start", {"threadId": thread_id}),
        "thread/compact/start",
    )
    deadline = time.time() + CALL_TIMEOUT_SECS
    compacted_turn = ""
    started_turn = ""
    sizes_by_turn: dict[str, KnownContextSize] = {}
    completed_turns: set[str] = set()
    while time.time() < deadline:
        frame = client.next_frame(deadline)
        if frame is None:
            continue
        params = _as_dict(frame.get("params"))
        if _as_str(params.get("threadId")) != thread_id:
            continue
        method = frame.get("method")
        if method == "thread/tokenUsage/updated":
            size = _context_size_from_usage(params)
            usage_turn = _as_str(params.get("turnId"))
            if isinstance(size, KnownContextSize) and usage_turn:
                sizes_by_turn[usage_turn] = size
        elif method == "turn/started":
            started_turn = _as_str(_as_dict(params.get("turn")).get("id")) or started_turn
        elif method == "thread/compacted":
            compacted_turn = _as_str(params.get("turnId"))
        elif method == "item/completed":
            item = _as_dict(params.get("item"))
            if _as_str(item.get("type")) == "contextCompaction":
                compacted_turn = _as_str(params.get("turnId"))
        elif method == "turn/completed":
            completed = _as_str(_as_dict(params.get("turn")).get("id"))
            if completed:
                completed_turns.add(completed)
        if (
            compacted_turn
            and compacted_turn in sizes_by_turn
            and compacted_turn in completed_turns
        ):
            return sizes_by_turn[compacted_turn]
    if compacted_turn and compacted_turn in sizes_by_turn:
        raise CompactionTimedOut(
            f"thread {thread_id}: compaction turn {compacted_turn} did not complete", compacted_turn
        )
    if compacted_turn:
        raise CompactionTimedOut(
            f"thread {thread_id}: compacted without a final token-usage update", compacted_turn
        )
    raise CompactionTimedOut(
        f"thread {thread_id}: no thread/compacted within {CALL_TIMEOUT_SECS}s", started_turn
    )


def _wait_for_idle(client: Client, thread_id: str) -> ThreadState:
    """Read the thread until it is idle or `COMPACT_IDLE_WAIT_SECS` pass; returns the last state read."""
    deadline = time.time() + COMPACT_IDLE_WAIT_SECS
    while True:
        state = _read_live_turn(client, thread_id)
        if isinstance(state, (ThreadIdle, ThreadStateUnknown)) or time.time() >= deadline:
            return state
        time.sleep(COMPACT_IDLE_POLL_SECS)


def _abandon_compaction(client: Client, name: str, thread_id: str, timeout: CompactionTimedOut) -> int:
    """Interrupt a compaction turn that did not finish, so the worker can take its follow-up.

    A compaction left live refuses every later follow-up and compaction of the worker.
    """
    state = _interruptible_turn_id(client, thread_id, _read_live_turn(client, thread_id))
    interrupted = False
    # A live turn that is not the compaction's belongs to someone else and is left alone.
    if isinstance(state, ThreadLive) and timeout.turn_id in ("", state.turn_id):
        # Best effort, as in `end`: a turn that finished since the read answers with an error.
        _ = _interrupt_turn(client, thread_id, state.turn_id)
        interrupted = True
        state = _wait_for_idle(client, thread_id)
    if isinstance(state, ThreadIdle):
        how = "interrupted the compaction turn, " if interrupted else ""
        print(f"codex_mesh: {name}: {timeout}; {how}the worker is idle and not compacted", file=sys.stderr)
        return 1
    reason = state.reason if isinstance(state, ThreadStateUnknown) else "a turn is still live on the thread"
    print(f"codex_mesh: {name}: {timeout}; {reason}", file=sys.stderr)
    return COMPACT_TURN_STILL_LIVE


def command_compact(args: argparse.Namespace) -> int:
    session_dir = _as_str(_attr(args, "session_dir"))
    name = _as_str(_attr(args, "to"))
    try:
        record = _lookup(session_dir, name)
        state = _roster_thread(record)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2
    if not isinstance(state, FollowableThread):
        print(
            f"codex_mesh: {name}: worker is running or otherwise busy; compact requires a finished worker",
            file=sys.stderr,
        )
        return 2
    before = _context_size_from_record(record)
    revision = _roster_revision(record)
    if isinstance(before, NotMeasuredSinceCompaction):
        print(
            f"codex_mesh: {name}: size not measured since its last compaction; compact refused",
            file=sys.stderr,
        )
        return 2
    if not isinstance(before, KnownContextSize):
        print(
            f"codex_mesh: {name}: no recorded context size; compact refused",
            file=sys.stderr,
        )
        return 2
    client: Client | None = None
    try:
        home = workers_home()
        port, _fresh = ensure_server(session_dir, home)
        released = _release_other_worker_writers(session_dir, name, state.thread_id, port)
        if not isinstance(released, _ReleasedWorkerThread):
            line = f"{name}: {released.reason}; compact refused; follow-up checks release again"
            _post_board_status(session_dir, name, line)
            print(f"codex_mesh: {line}", file=sys.stderr)
            return COMPACT_TURN_STILL_LIVE if isinstance(released, WorkerWaitingOnLiveTurn) else 2
        client = Client(port, f"compact-{name}")
        _ = _require(
            client.call("thread/resume", _resume_params(session_dir, name, state.thread_id)),
            "thread/resume",
        )
        # A finished worker with a live turn is finishing one nobody waits on; give it time to end.
        live = _wait_for_idle(client, state.thread_id)
        if isinstance(live, ThreadStateUnknown):
            print(f"codex_mesh: {name}: {live.reason}; compact refused", file=sys.stderr)
            return 2
        if not isinstance(live, ThreadIdle):
            print(f"codex_mesh: {name}: thread has a live turn; compact refused", file=sys.stderr)
            return COMPACT_TURN_STILL_LIVE
        try:
            after = _compact_thread(client, state.thread_id)
        except CompactionTimedOut as timeout:
            return _abandon_compaction(client, name, state.thread_id, timeout)
        size: RecordedContextSize = after if after.tokens > 0 else NotMeasuredSinceCompaction()
        compacted_record: FollowableRecord = {"thread_id": state.thread_id, "status": state.status}
        if isinstance(size, KnownContextSize):
            compacted_record["context_tokens"] = size.tokens
        else:
            compacted_record["context_tokens"] = "not_measured_since_compaction"
        _ = _update_roster_if_expected(
            session_dir,
            name,
            compacted_record,
            RosterUnchanged(state.thread_id, revision),
        )
        if isinstance(size, KnownContextSize):
            print(f"compacted {name}: {before.tokens} -> {size.tokens} tokens")
        else:
            print(f"compacted {name}: {before.tokens} tokens, size not measured until its next turn")
        return 0
    except (ConnectionError, OSError, SystemExit) as exc:
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
        return "worker is busy or not followable"
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
    role = _worker_role(args)
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
        if not _claim_follow(session_dir, name, state.thread_id, claim_pid, role, _dispatch_lens(args)):
            print(f"codex_mesh: {name} was already claimed", file=sys.stderr)
            return 2
    return 0


def command_release_follow(args: argparse.Namespace) -> int:
    _restore_follow_claim(_as_str(_attr(args, "session_dir")),
                          _as_str(_attr(args, "to")), _as_int(_attr(args, "claim_pid")))
    return 0


def _run_delegate(args: argparse.Namespace, port: int, *, started_here: bool = False) -> RunOutcome:
    """Attach one delegate to the app-server on `port` and run it to its end."""
    session_dir = _as_str(_attr(args, "session_dir"))
    name = _as_str(_attr(args, "name"))
    timeout = _as_float(_attr(args, "timeout"), 86400.0)
    opened = time.time()
    try:
        return _attach_and_run(args, port, session_dir, name, timeout, opened, started_here=started_here)
    except (ConnectionError, OSError, SystemExit) as exc:
        return FailedBeforeThread(str(exc) or exc.__class__.__name__, time.time() - opened,
            exc.serving if isinstance(exc, ServingRequestFailure) else NotBoundCodexServer(),
            exc.reported if isinstance(exc, ServingRequestFailure) else False)


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
    *, started_here: bool = False,
) -> RunOutcome:
    prompt = Path(_as_str(_attr(args, "prompt_file"))).read_text(encoding="utf-8")
    log_path = Path(_as_str(_attr(args, "log_file")))

    client = Client(port, name)
    _ = _bind_serving_server(client, session_dir, started_here=started_here)
    started = _serving_request(client, "thread/start", _thread_start_params(args), submits_work=True)
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
        starting: StartingRecord = {
            "thread_id": thread_id,
            "status": "starting",
            "port": port,
            "launcher_pid": os.getpid(),
        }
        role = _roster_role(_attr(args, "role"))
        if isinstance(role, AssignedDelegateRole):
            starting["role"] = role.role
        _update_roster(session_dir, name, starting)
        if isinstance(role, AssignedDelegateRole):
            _record_assignment(thread_id, role.role, _dispatch_lens(args))

    return _stream_turn(args, client, port, session_dir, name, thread_id, prompt, timeout, opened)


def _resume_account_move(
    args: argparse.Namespace,
    session_dir: str,
    name: str,
    thread_id: str,
    client: Client,
    marker: AccountMoveMarker,
) -> MovedWorker:
    """Resume the marked worker and acknowledge its accepted continuation."""
    try:
        resumed = _start_workers_turn(
            args, session_dir, name, thread_id, client, ACCOUNT_MOVE_CONTINUATION)
        if not resumed.turn_id:
            raise SystemExit("account move continuation returned no turn id")
        _ = _update_roster_if_expected(
            session_dir,
            name,
            {"thread_id": resumed.thread_id, "turn_id": resumed.turn_id,
             "status": "running", "port": resumed.port},
            RosterOnThread(resumed.thread_id),
        )
        _finish_account_move(session_dir, name, marker)
        return resumed
    except (ConnectionError, OSError, SystemExit) as error:
        _fail_account_move(session_dir, name, marker, f"resume failed: {error}")
        raise


def _stream_turn(
    args: argparse.Namespace,
    client: Client,
    initial_port: int,
    session_dir: str,
    name: str,
    initial_thread_id: str,
    prompt: str,
    timeout: float,
    opened: float,
    *,
    follow: bool = False,
) -> RunOutcome:
    """Stream a new or resumed thread's turn through the same lifecycle."""
    client.session_dir = session_dir
    port: int = initial_port
    thread_id: str = initial_thread_id
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
    waiting_for_progress_after_overflow = False
    run_outcome: RunOutcome = RunCompleted()
    refusal_reported = False
    failure_serving = client.serving
    reported_turns: set[tuple[CodexServingState, str, str]] = set()
    changed: bool
    moved: WorkerMoveResult = WorkerWaitingOnLiveTurn()
    started: MovedWorker
    deadline = time.time() + timeout
    with _pending_file(session_dir, name) as handle:
        _write_pending(handle, thread_id, _pending_for_thread(handle, thread_id))
    try:
        # The roster is written before this call: a disconnect after thread
        # creation must still leave its id reachable.
        if not follow:
            _ = client.call("thread/name/set", {"threadId": thread_id, "name": name})
        initial_move = (_read_account_move(session_dir, name, thread_id)
                        if _attr(args, "account_move") is True else NoAccountMove())
        started = _start_workers_turn(args, session_dir, name, thread_id, client, prompt)
        client, port, thread_id, turn_id = started.client, started.port, started.thread_id, started.turn_id
        if not turn_id:
            raise SystemExit("codex_mesh: turn/start returned no turn id")
        if isinstance(initial_move, AccountMoveMarker):
            _finish_account_move(session_dir, name, initial_move)
        followed_turn = turn_id if follow else ""
        _ = _update_roster_if_expected(
            session_dir,
            name,
            {
                "thread_id": thread_id,
                "turn_id": turn_id,
                "status": "running",
                "port": port,
            },
            RosterOnThread(thread_id),
        )
        with log_path.open("a", encoding="utf-8") as log:
            def hold_messages_for_next_launch(reason: str) -> None:
                if end_marker.exists():
                    return
                kept = _close_messages_and_keep_pending(session_dir, name, thread_id)
                if kept:
                    messages = "1 message" if kept == 1 else f"{kept} messages"
                    held = f"{messages} held for the next launch"
                    _post_board_status(session_dir, name, f"launcher: {held}: {reason}")
                    _ = log.write(f"[{_now_stamp()}] mesh: {name} cannot move: {reason}; {held}\n")
                    log.flush()

            _ = log.write(f"[{_now_stamp()}] mesh: {name} thread {thread_id}\n")
            log.flush()
            while time.time() < deadline:
                # A resident thread must wake to see `end` between turns. Any other
                # wakes to check that its turn has not ended without the notice.
                # Account-move intent can arrive while a completion notice is lost.
                # Check it on the resident cadence even for a one-turn launcher.
                poll = min(RESIDENT_POLL_SECS, UNSEEN_END_CHECK_SECS)
                frame = client.next_frame(min(deadline, time.time() + poll))
                if frame is None:
                    account_move = _read_account_move(session_dir, name, thread_id)
                    if (isinstance(account_move, AccountMoveMarker) and not end_marker.exists()
                            and _ended_unseen(client, thread_id)):
                        started = _resume_account_move(args, session_dir, name, thread_id, client, account_move)
                        client, port, thread_id, turn_id = started.client, started.port, started.thread_id, started.turn_id
                        if follow:
                            followed_turn = turn_id
                        failure = followed_failure = final_answer = ""
                        continue
                    if not resident and time.time() < deadline:
                        quota_owed = _continuation_is_owed(session_dir, name)
                        if not resume_owed and _ended_unseen(client, thread_id):
                            moved = _move_resident(args, session_dir, name, thread_id, client)
                            if isinstance(moved, WorkerThreadStateUnknown):
                                if isinstance(moved.cleanup, WorkerTurnCleanup):
                                    raise WorkerMoveUncertain(thread_id, moved)
                                hold_messages_for_next_launch(moved.reason)
                            else:
                                if quota_owed and isinstance(moved, WorkerWaitingOnLiveTurn):
                                    continue
                                if isinstance(moved, MovedWorker):
                                    client, port, thread_id = moved.client, moved.port, moved.thread_id
                                    if moved.turn_id or _continuation_is_owed(session_dir, name):
                                        turn_id = moved.turn_id or turn_id
                                        if follow:
                                            followed_turn = turn_id
                                        continue
                                if not _close_messages_if_empty(session_dir, name, thread_id):
                                    continue
                            if quota_owed:
                                continue
                            _ = log.write(f"[{_now_stamp()}] mesh: {name} thread is idle and no "
                                          + "completion notice arrived; finishing\n")
                            log.flush()
                            failure = followed_failure or (
                                "" if final_answer
                                else "the thread went idle with no completion notice and no answer")
                            break
                        continue
                    if end_marker.exists():
                        break
                    if resident:
                        selected_home = workers_home()
                        record = _read_json_object(_session_path(session_dir, SERVER_FILE))
                        with _pending_file(session_dir, name) as handle:
                            held_messages = bool(_pending_for_thread(handle, thread_id))
                        if (server_record_home(record) == selected_home
                                and record.get("port") == port and not held_messages
                                and not _continuation_is_owed(session_dir, name)):
                            continue
                        moved = _move_resident(args, session_dir, name, thread_id, client)
                        if isinstance(moved, WorkerThreadStateUnknown):
                            if isinstance(moved.cleanup, WorkerTurnCleanup):
                                raise WorkerMoveUncertain(thread_id, moved)
                            _ = log.write(f"[{_now_stamp()}] mesh: {name} cannot move: {moved.reason}; waiting\n")
                            log.flush()
                        elif isinstance(moved, MovedWorker):
                            changed = bool(moved.port != port or moved.thread_id != thread_id)
                            client, port, thread_id = moved.client, moved.port, moved.thread_id
                            turn_id = moved.turn_id or turn_id
                            if changed:
                                _ = log.write(f"[{_now_stamp()}] mesh: {name} moved to the workers app-server on port {port}\n")
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
                        _ = _update_roster_if_expected(
                            session_dir,
                            name,
                            {"thread_id": thread_id, "turn_id": live, "status": "running"},
                            RosterOnThread(thread_id),
                        )
                elif method == "thread/tokenUsage/updated":
                    context_size = _context_size_from_usage(params)
                    if isinstance(context_size, KnownContextSize):
                        _ = _update_roster_if_expected(
                            session_dir,
                            name,
                            {
                                "thread_id": thread_id,
                                "turn_id": turn_id,
                                "status": "running",
                                "context_tokens": context_size.tokens,
                            },
                            RosterOnThread(thread_id),
                        )
                elif method == "item/completed":
                    item = _as_dict(params.get("item"))
                    _ = log.write(f"[{_now_stamp()}] {_describe_item(item)}\n")
                    log.flush()
                    if _as_str(item.get("type")) not in ("userMessage", "contextCompaction"):
                        waiting_for_progress_after_overflow = False
                    if _as_str(item.get("type")) == "agentMessage":
                        text = _as_str(item.get("text"))
                        if text:
                            final_answer = text
                elif method in ("turn/completed", "turn/failed"):
                    notice_turn = _as_str(_as_dict(params.get("turn")).get("id"))
                    account_move = _read_account_move(session_dir, name, thread_id)
                    moved_turn = isinstance(account_move, AccountMoveMarker) and notice_turn == account_move.turn_id
                    if follow and notice_turn and followed_turn and notice_turn != followed_turn and not moved_turn:
                        continue
                    # The followed turn has ended. A later turn names itself in its own
                    # `turn/started`; a thread read at this moment can still name this one.
                    if follow:
                        followed_turn = ""
                    turn_outcome = _turn_outcome(method, params)
                    if (isinstance(account_move, AccountMoveMarker) and not end_marker.exists()
                            and (not notice_turn or notice_turn == account_move.turn_id)):
                        started = _resume_account_move(args, session_dir, name, thread_id, client, account_move)
                        client, port, thread_id, turn_id = started.client, started.port, started.thread_id, started.turn_id
                        if follow:
                            followed_turn = turn_id
                        failure = followed_failure = final_answer = ""
                        continue
                    if isinstance(turn_outcome, TurnFailed):
                        serving = client.serving
                        identity = (serving, thread_id, notice_turn or turn_id)
                        failed = FailedWithThread(thread_id, turn_outcome.detail,
                            time.time() - client.turn_submitted_at, serving)
                        if identity not in reported_turns:
                            reported_turns.add(identity)
                            if _quota_refused(failed, serving.started_here, False):
                                _owe_quota_continuation(client, thread_id)
                                _report_quota_refusal(name, serving, turn_outcome.detail)
                                refusal_reported = True
                        if not followed_failure:
                            failure_serving = serving
                    if isinstance(turn_outcome, TurnContextWindowExceeded):
                        if waiting_for_progress_after_overflow:
                            failure = (
                                "context window filled twice in a row without progress; "
                                "automatic continuation stopped"
                            )
                            _ = log.write(f"[{_now_stamp()}] {failure}\n")
                            log.flush()
                            break
                        continuation = "Continue where you stopped."
                        _post_board_status(
                            session_dir,
                            name,
                            f'context window filled; sent "{continuation}"',
                        )
                        _ = log.write(
                            f"[{_now_stamp()}] context window filled; starting one continuation turn\n"
                        )
                        log.flush()
                        started = _start_workers_turn(args, session_dir, name, thread_id, client, continuation)
                        client, port, thread_id, turn_id = started.client, started.port, started.thread_id, started.turn_id
                        if follow:
                            followed_turn = turn_id
                        _ = _update_roster_if_expected(
                            session_dir,
                            name,
                            {"thread_id": thread_id, "turn_id": turn_id, "status": "running"},
                            RosterOnThread(thread_id),
                        )
                        waiting_for_progress_after_overflow = True
                        continue
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
                            _ = _update_roster_if_expected(
                                session_dir,
                                name,
                                {
                                    "thread_id": thread_id,
                                    "status": CAPACITY_EXHAUSTED,
                                },
                                RosterOnThread(thread_id),
                            )
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
                        waiting_for_progress_after_overflow = False
                    if resume_owed:
                        if isinstance(turn_outcome, TurnRefusedForCapacity) and not retry_waited:
                            remaining = CAPACITY_BUDGET_SECS - capacity_waited
                            wait = min(CAPACITY_WAIT_SECS * 2.0 ** capacity_retries,
                                       CAPACITY_MAX_WAIT_SECS, remaining)
                            capacity_retries += 1
                            with _pending_file(session_dir, name):
                                _ = _update_roster_if_expected(
                                    session_dir,
                                    name,
                                    {
                                        "thread_id": thread_id,
                                        "status": WAITING_CAPACITY,
                                        "launcher_pid": os.getpid(),
                                    },
                                    RosterOnThread(thread_id),
                                )
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
                                    _ = _update_roster_if_expected(
                                        session_dir,
                                        name,
                                        {
                                            "thread_id": thread_id,
                                            "turn_id": turn_id,
                                            "status": "running",
                                        },
                                        RosterOnThread(thread_id),
                                    )
                                else:
                                    _ = _update_roster_if_expected(
                                        session_dir,
                                        name,
                                        {
                                            "thread_id": thread_id,
                                            "status": "starting",
                                            "launcher_pid": os.getpid(),
                                        },
                                        RosterOnThread(thread_id),
                                    )
                            continue
                        with _pending_file(session_dir, name):
                            if end_marker.exists() or not _roster_still_on_thread(session_dir, name, thread_id):
                                reason = "ended" if end_marker.exists() else "replaced"
                                _ = log.write(f"[{_now_stamp()}] capacity launcher {reason}; no resume turn started\n")
                                log.flush()
                                return FailedWithThread(thread_id, f"capacity launcher {reason}", time.time() - opened)
                        started = _start_workers_turn(
                            args, session_dir, name, thread_id, client,
                            "Your last turn stopped because the model was at capacity. Continue from where you stopped; your edits are already in the tree.",
                        )
                        client, port, thread_id, turn_id = started.client, started.port, started.thread_id, started.turn_id
                        if follow:
                            followed_turn = turn_id
                        resume_owed = False
                        continue
                    quota_owed = _continuation_is_owed(session_dir, name)
                    if follow and not resident and isinstance(turn_outcome, TurnFailed) and not followed_failure and not quota_owed:
                        followed_failure = turn_outcome.detail
                    failure = followed_failure or (turn_outcome.detail if isinstance(turn_outcome, TurnFailed) else "")
                    if resident:
                        replies += 1
                        _deliver_reply(name, replies, final_answer, failure, summary_path, reply_path, log)
                        final_answer = ""
                        failure = ""
                        if end_marker.exists():
                            break
                    with _pending_file(session_dir, name) as handle:
                        held_messages = bool(_pending_for_thread(handle, thread_id))
                    owed = _continuation_is_owed(session_dir, name)
                    if resident or held_messages or owed:
                        thread_id = _as_str(cast(object, thread_id))
                        moved = _move_resident(args, session_dir, name, thread_id, client)
                        if isinstance(moved, WorkerThreadStateUnknown):
                            if isinstance(moved.cleanup, WorkerTurnCleanup):
                                raise WorkerMoveUncertain(thread_id, moved)
                            if resident:
                                _ = log.write(f"[{_now_stamp()}] mesh: {name} cannot move: {moved.reason}; waiting\n")
                                log.flush()
                                continue
                            hold_messages_for_next_launch(moved.reason)
                            break
                        if isinstance(moved, MovedWorker):
                            client, port, thread_id = moved.client, moved.port, moved.thread_id
                            if moved.turn_id or _continuation_is_owed(session_dir, name):
                                turn_id = moved.turn_id or turn_id
                                if follow:
                                    followed_turn = turn_id
                                continue
                    if owed and isinstance(moved, WorkerWaitingOnLiveTurn):
                        time.sleep(RESIDENT_POLL_SECS)
                        continue
                    # A finished turn may have a peer turn opening behind it.
                    if not resident and _more_work_coming(client, thread_id, deadline):
                        continue
                    if not resident:
                        if _close_messages_if_empty(session_dir, name, thread_id):
                            break
                        moved = _move_resident(args, session_dir, name, thread_id, client)
                        if isinstance(moved, WorkerThreadStateUnknown):
                            if isinstance(moved.cleanup, WorkerTurnCleanup):
                                raise WorkerMoveUncertain(thread_id, moved)
                            hold_messages_for_next_launch(moved.reason)
                            break
                        if isinstance(moved, MovedWorker):
                            client, port, thread_id = moved.client, moved.port, moved.thread_id
                            if moved.turn_id or _continuation_is_owed(session_dir, name):
                                turn_id = moved.turn_id or turn_id
                                if follow:
                                    followed_turn = turn_id
                        continue
                    _ = _update_roster_if_expected(
                        session_dir,
                        name,
                        {"thread_id": thread_id, "turn_id": turn_id, "status": "running"},
                        RosterOnThread(thread_id),
                    )
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
            _ = _update_roster_if_expected(
                session_dir,
                name,
                {"thread_id": thread_id, "status": "failed" if failure else "done"},
                RosterOnThread(thread_id),
            )
        if isinstance(run_outcome, CapacityRetriesExhausted):
            return run_outcome
        return FailedWithThread(thread_id, failure, time.time() - opened, failure_serving,
                                refusal_reported, True) if failure else RunCompleted()
    except (ConnectionError, OSError, SystemExit) as exc:
        # Once thread/start returned, even a disconnected turn/start may have
        # begun work. Keep its id and never resubmit the original prompt.
        if isinstance(exc, WorkerMoveUncertain) and isinstance(exc.cleanup, WorkerTurnCleanup):
            port = exc.cleanup.port
            thread_id = exc.cleanup.thread_id
        _ = _end_unwatched_turn(port, thread_id, log_path)
        failure = str(exc) or exc.__class__.__name__
        _ = _update_roster_if_expected(
            session_dir,
            name,
            {"thread_id": thread_id, "status": "failed"},
            RosterOnThread(thread_id),
        )
        serving = exc.serving if isinstance(exc, (ServingRequestFailure, WorkerMoveUncertain)) else client.serving
        reported = exc.reported if isinstance(exc, (ServingRequestFailure, WorkerMoveUncertain)) else False
        return FailedWithThread(thread_id, failure, time.time() - opened, serving,
                                refusal_reported or reported, True)
    finally:
        if not end_marker.exists():
            with _pending_file(session_dir, name) as handle:
                _write_pending(handle, thread_id, _pending_for_thread(handle, thread_id), accepting_messages=False)
        client.close()


@dataclass(frozen=True)
class MovedWorker:
    client: Client
    port: int
    # Empty when no new turn was accepted.
    turn_id: str
    thread_id: str
    thread_replaced: bool
    serving: CodexServingState = field(default_factory=NotBoundCodexServer)


@dataclass(frozen=True)
class WorkerWaitingOnLiveTurn:
    """The outgoing thread is still working and keeps its server."""

    reason: str = "the outgoing thread still has a live turn"


@dataclass(frozen=True)
class WorkerAwaitingQuotaRecovery(MovedWorker):
    """The thread resumed, but the destination refused its owed continuation."""


@dataclass(frozen=True)
class WorkerTurnCleanup:
    """A turn may have started here while the move could not finish."""

    port: int
    thread_id: str


@dataclass(frozen=True)
class NoWorkerTurnToCleanUp:
    """No new turn was submitted during this failed move."""


@dataclass(frozen=True)
class WorkerThreadStateUnknown:
    """The outgoing thread cannot be proved idle; no new turn may start."""

    reason: str
    cleanup: WorkerTurnCleanup | NoWorkerTurnToCleanUp = field(default_factory=NoWorkerTurnToCleanUp)
    serving: CodexServingState = field(default_factory=NotBoundCodexServer)
    refusal_reported: bool = False


WorkerMoveResult = MovedWorker | WorkerWaitingOnLiveTurn | WorkerThreadStateUnknown


class WorkerMoveUncertain(SystemExit):
    def __init__(self, thread_id: str, result: WorkerThreadStateUnknown) -> None:
        super().__init__(f"thread {thread_id}: state unknown ({result.reason}); no turn resubmitted")
        self.cleanup: WorkerTurnCleanup | NoWorkerTurnToCleanUp = result.cleanup
        self.serving: CodexServingState = result.serving
        self.reported: bool = result.refusal_reported


@dataclass(frozen=True)
class _ReleasedWorkerThread:
    """The old server no longer holds the thread's writer."""


def _recorded_server_gone(session_dir: str, port: int) -> bool:
    pids = [pid for record in _recorded_run_servers(session_dir)
            if record.get("port") == port and isinstance(pid := record.get("pid"), int)]
    return bool(pids) and not any(_pid_alive(pid) for pid in pids)


def _release_blocker_reason(client: Client, session_dir: str, thread_id: str) -> str:
    workers = _roster_threads(Path(session_dir))
    busy: list[str] = []
    for identity, name in workers.items():
        try:
            reply = client.call("thread/read", {"threadId": identity, "includeTurns": False}, timeout=1.0)
        except (ConnectionError, OSError, SystemExit):
            continue
        thread = _as_dict(_as_dict(reply.get("result")).get("thread"))
        if _as_str(_as_dict(thread.get("status")).get("type")) in ("active", "running"):
            busy.append(name)
    held_by = ", ".join(busy) or workers.get(thread_id, thread_id)
    return (f"run {Path(session_dir).name}, worker {held_by}: old server has not released the thread; "
            + "follow-up goes through once that turn ends")


def _release_worker_thread(client: Client, thread_id: str, session_dir: str) -> _ReleasedWorkerThread | WorkerThreadStateUnknown:
    deadline = time.monotonic() + THREAD_RELEASE_TIMEOUT_SECS
    try:
        unsubscribed = _require(client.call("thread/unsubscribe", {"threadId": thread_id},
                                           timeout=THREAD_RELEASE_TIMEOUT_SECS), "thread/unsubscribe")
        if unsubscribed.get("status") == "notLoaded":
            return _ReleasedWorkerThread()
        while time.monotonic() < deadline:
            if thread_id not in _loaded_thread_ids(client, deadline):
                return _ReleasedWorkerThread()
            frame = client.next_frame(time.time() + min(0.1, max(0.0, deadline - time.monotonic())))
            if frame and frame.get("method") == "thread/closed" and _as_str(_as_dict(frame.get("params")).get("threadId")) == thread_id:
                return _ReleasedWorkerThread()
        if _recorded_server_gone(session_dir, client.port):
            return _ReleasedWorkerThread()
        return WorkerThreadStateUnknown(_release_blocker_reason(client, session_dir, thread_id))
    except (ConnectionError, OSError, SystemExit) as error:
        if _recorded_server_gone(session_dir, client.port):
            return _ReleasedWorkerThread()
        return WorkerThreadStateUnknown(f"old thread release could not be confirmed: {error}")


@dataclass(frozen=True)
class _WorkerThreadForHome:
    thread_id: str
    thread_replaced: bool


def _refuse_taken_follow_up(client: Client, replacement: str, session_dir: str) -> NoReturn:
    released = _release_worker_thread(client, replacement, session_dir)
    if isinstance(released, WorkerThreadStateUnknown):
        raise SystemExit("name was taken while the follow-up ran; "
                         + f"replacement thread {replacement} not released: {released.reason}")
    raise SystemExit("name was taken while the follow-up ran; follow-up did not start")


def _resume_worker_thread(
    args: argparse.Namespace, session_dir: str, name: str, thread_id: str, client: Client,
    workers_home: Path,
) -> _WorkerThreadForHome:
    params = _resume_params(session_dir, name, thread_id)
    instructions_file = _as_str(_lookup(session_dir, name).get("instructions_file"))
    resumed = client.call("thread/resume", params)
    if not resumed.get("error"):
        _ = _require(resumed, "thread/resume")
        return _WorkerThreadForHome(thread_id, False)
    if isinstance(_read_account_move(session_dir, name, thread_id), AccountMoveMarker):
        _ = _require(resumed, "thread/resume")
    start_params = _thread_start_params(args)
    if "developerInstructions" in params:
        start_params.update({
            "developerInstructions": params["developerInstructions"],
            "sandbox": "read-only", "approvalPolicy": "never",
        })
    started = _serving_request(client, "thread/start", start_params, submits_work=True)
    replacement = _as_str(_as_dict(started.get("thread")).get("id"))
    if not replacement:
        raise SystemExit("thread/start returned no replacement thread id")
    record: StartingRecord = {"thread_id": replacement, "status": "starting", "port": client.port}
    if instructions_file:
        record["instructions_file"] = instructions_file
    if not _update_roster_if_expected(session_dir, name, record, RosterOnThread(thread_id)):
        _refuse_taken_follow_up(client, replacement, session_dir)
    _ = client.call("thread/name/set", {"threadId": replacement, "name": name})
    _post_board_status(
        session_dir, name,
        f"{codex_home_display(workers_home)} refused to resume {thread_id}; worker continues as new thread {replacement}",
    )
    return _WorkerThreadForHome(replacement, True)


def _release_other_worker_writers(
    session_dir: str, name: str, thread_id: str, target_port: int,
) -> _ReleasedWorkerThread | WorkerWaitingOnLiveTurn | WorkerThreadStateUnknown:
    for port in _run_servers(session_dir):
        if port == target_port:
            continue
        records = _recorded_run_servers(session_dir)
        pids = {pid for record in records
                if record.get("port") == port and isinstance(pid := record.get("pid"), int)}
        live_pids = {pid for pid in pids if _pid_alive(pid)}
        current_pid = records[-1].get("pid") if records[-1].get("port") == port else None
        stopped_pids = {pid for pid in live_pids if pid != current_pid
                        and _stop_unused(None, pid, port, RetiredRunServer(Path(session_dir)))}
        if pids and live_pids <= stopped_pids:
            continue
        client: Client | None = None
        try:
            client = Client(port, f"release-{name}")
            if thread_id not in _loaded_thread_ids(client, time.monotonic() + THREAD_RELEASE_TIMEOUT_SECS):
                continue
            state = _read_live_turn(client, thread_id)
            if isinstance(state, ThreadStateUnknown):
                return WorkerThreadStateUnknown(state.reason)
            if not isinstance(state, ThreadIdle):
                return WorkerWaitingOnLiveTurn(
                    f"run {Path(session_dir).name}, worker {name}: old server still has a live turn; "
                    + "follow-up goes through once that turn ends")
            with _pending_file(session_dir, name) as handle:
                messages = _pending_for_thread(handle, thread_id)
                def park(message: str) -> None:
                    messages.append(message)
                    _write_pending(handle, thread_id, messages)
                _move_queue_to_pending(client, thread_id, park)
                state = _read_live_turn(client, thread_id)
                if not isinstance(state, ThreadIdle):
                    return WorkerThreadStateUnknown("old thread is no longer idle")
                released = _release_worker_thread(client, thread_id, session_dir)
                if isinstance(released, WorkerThreadStateUnknown):
                    return released
        except (ConnectionError, OSError, SystemExit) as error:
            if _recorded_server_gone(session_dir, port):
                continue
            return WorkerThreadStateUnknown(f"old writer could not be checked: {error}")
        finally:
            if client is not None:
                client.close()
    return _ReleasedWorkerThread()


def _move_resident(
    args: argparse.Namespace, session_dir: str, name: str, thread_id: str, client: Client,
    *, message: str = "",
) -> WorkerMoveResult:
    try:
        return _move_idle_worker(args, session_dir, name, thread_id, client, message=message)
    except (ConnectionError, OSError, SystemExit) as error:
        return WorkerThreadStateUnknown(str(error) or type(error).__name__)


def _quota_continuation_ready(client: Client, selected_home: Path) -> bool:
    from scripts.accounts import moves, refusals

    now = datetime.now(timezone.utc)
    serving = client.serving
    if selected_home != serving.home:
        return True
    identity = serving.account
    if isinstance(identity, UnknownCodexAccount):
        return False
    refusal = refusals.load().get(refusals.key("codex", identity.login))
    if refusal is None or not refusals.resolved(refusal, refusals.stored_reading("codex", identity.login, now), now):
        return False
    entries = moves.read_moves()
    ended = {entry["move_id"] for entry in entries if entry["kind"] == "wait ended"}
    for entry in entries:
        if (entry["tool"] == "codex" and entry["subject"] == "workers" and entry["kind"] == "waited"
                and entry["move_id"] not in ended and datetime.fromisoformat(entry["reset"]) <= now):
            moves.append_move(moves.WaitEndedMove(kind="wait ended", at=now.isoformat(), tool="codex",
                subject="workers", move_id=entry["move_id"]))
    return True


@dataclass(frozen=True)
class NoRequestedWorkerMove:
    """No workers transition requests this source-to-destination resume."""


def _requested_worker_move(source: CodexServingState, destination: Path) -> RequestedMove | NoRequestedWorkerMove:
    from scripts.accounts import moves

    if not isinstance(source, ServingCodexServer) or source.home == destination:
        return NoRequestedWorkerMove()
    requests = [entry for entry in moves.read_moves() if entry["kind"] == "requested"
                and entry["tool"] == "codex" and entry["subject"] == "workers"]
    origin = requests[-1]["from"] if requests else moves.HomeAccountUnresolved(source.home)
    target = requests[-1]["to"] if requests else moves.HomeAccountUnresolved(destination)
    if (requests and isinstance(origin, str) and isinstance(target, str)
            and codex_home_matches_label(source.home, origin)
            and codex_home_matches_label(destination, target)):
        # The reader resolves legacy home labels from historical evidence; leave the journal unchanged.
        matched = requests[-1].copy()
        matched["to"] = codex_home_role_label(destination)
        return matched
    return NoRequestedWorkerMove()


def _record_worker_move_failure(request: RequestedMove | NoRequestedWorkerMove, reason: str) -> None:
    from scripts.accounts import moves

    if isinstance(request, NoRequestedWorkerMove):
        return
    moves.append_move(moves.FailedMove(kind="failed", at=datetime.now(timezone.utc).isoformat(),
        tool="codex", subject="workers", move_id=request["move_id"], reason=reason))


def _confirm_worker_move(session_dir: str, name: str, request: RequestedMove | NoRequestedWorkerMove,
                       serving: CodexServingState) -> None:
    from scripts.accounts import moves

    if not isinstance(serving, ServingCodexServer):
        return
    if isinstance(request, NoRequestedWorkerMove) or codex_home_role_label(serving.home) != request["to"]:
        return
    moves.append_move(moves.ConfirmedMove(kind="confirmed", at=datetime.now(timezone.utc).isoformat(),
        tool="codex", subject="workers", move_id=request["move_id"], home=str(serving.home),
        worker=name, run=str(Path(session_dir).resolve())))


def _move_idle_worker(
    args: argparse.Namespace, session_dir: str, name: str, thread_id: str, client: Client,
    *, message: str = "",
) -> WorkerMoveResult:
    """Check one new-turn boundary and move an idle thread onto the workers home."""
    state = _read_live_turn(client, thread_id)
    if isinstance(state, ThreadStateUnknown):
        return WorkerThreadStateUnknown(state.reason)
    if not isinstance(state, ThreadIdle):
        return WorkerWaitingOnLiveTurn()
    if isinstance(client.serving, NotBoundCodexServer):
        _ = _bind_serving_server(client, session_dir)
    source = client.serving
    worker_home_for_turn = workers_home()
    request = _requested_worker_move(source, worker_home_for_turn)
    cleanup: WorkerTurnCleanup | NoWorkerTurnToCleanUp = NoWorkerTurnToCleanUp()
    moved = client
    replaced = False
    try:
        with _pending_file(session_dir, name) as handle:
            if _end_marker_path(session_dir, name).exists() or not _roster_still_on_thread(session_dir, name, thread_id):
                return WorkerThreadStateUnknown("worker ended or was replaced before the new turn")
            state = _read_live_turn(client, thread_id)
            if isinstance(state, ThreadStateUnknown):
                return WorkerThreadStateUnknown(state.reason)
            if not isinstance(state, ThreadIdle):
                return WorkerWaitingOnLiveTurn()
            messages = _pending_for_thread(handle, thread_id)

            def park(text: str) -> None:
                messages.append(text)
                _write_pending(handle, thread_id, messages)

            _move_queue_to_pending(client, thread_id, park)
            state = _read_live_turn(client, thread_id)
            if isinstance(state, ThreadStateUnknown):
                return WorkerThreadStateUnknown(state.reason)
            if not isinstance(state, ThreadIdle):
                return WorkerWaitingOnLiveTurn()
            port, _fresh = ensure_server(session_dir, worker_home_for_turn)
            previous = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
            old_port = client.port
            owes_continuation = previous.get("continuation_owed") is True
            if owes_continuation and not _quota_continuation_ready(client, worker_home_for_turn):
                from scripts.accounts import refusals
                identity = client.serving.account
                if (isinstance(identity, UnknownCodexAccount) or
                        refusals.key("codex", identity.login) not in refusals.load()):
                    return WorkerThreadStateUnknown("quota handoff did not establish a recoverable account; continuation is held")
                return WorkerWaitingOnLiveTurn("the refused account has not recovered")
            if isinstance(client.serving, NotBoundCodexServer):
                _ = _bind_serving_server(client, session_dir, started_here=_fresh)
            if _fresh and port == old_port:
                serving = client.serving
                client.serving = replace(serving, started_here=True)
            if port != old_port:
                released = _release_worker_thread(client, thread_id, session_dir)
                if isinstance(released, WorkerThreadStateUnknown):
                    _record_worker_move_failure(request, released.reason)
                    return released
                moved = Client(port, name)
                _ = _bind_serving_server(moved, session_dir, started_here=_fresh)
                try:
                    resumed = _resume_worker_thread(args, session_dir, name, thread_id, moved, worker_home_for_turn)
                    thread_id, replaced = resumed.thread_id, resumed.thread_replaced
                    if replaced:
                        _write_pending(handle, thread_id, messages)
                        _update_roster(session_dir, name, {"thread_id": thread_id, "status": "starting",
                            "port": port, "continuation_owed": owes_continuation})
                        role = _roster_role(previous.get("role"))
                        if isinstance(role, AssignedDelegateRole):
                            _record_assignment(thread_id, role.role, _dispatch_lens(args))
                except (ConnectionError, OSError, SystemExit) as error:
                    _record_worker_move_failure(request, str(error) or type(error).__name__)
                    moved.close()
                    current = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
                    current_thread = _as_str(current.get("thread_id")) or thread_id
                    cleanup = (WorkerTurnCleanup(port, current_thread)
                               if replaced or current_thread != thread_id else NoWorkerTurnToCleanUp())
                    return WorkerThreadStateUnknown(str(error) or type(error).__name__, cleanup,
                        error.serving if isinstance(error, ServingRequestFailure) else moved.serving,
                        error.reported if isinstance(error, ServingRequestFailure) else False)
                _confirm_worker_move(session_dir, name, request, moved.serving)
                client.close()
            turn_id = ""
            if owes_continuation or message or messages:
                _update_roster(session_dir, name, {"thread_id": thread_id, "status": "starting", "port": port})
                inputs = (["Continue where you stopped.", *([message] if message else []), *messages] if owes_continuation
                          else [message, *messages] if message else messages)
                cleanup = WorkerTurnCleanup(port, thread_id)
                turn_id = _start_turn(moved, args, thread_id, inputs[0], tuple(inputs[1:]))
                if owes_continuation and not turn_id:
                    raise SystemExit("quota continuation was not confirmed by turn/start; task is still owed")
                _write_pending(handle, thread_id, [])
            prior_turn = _as_str(previous.get("turn_id")) if not replaced else ""
            _update_roster(session_dir, name, {
                "thread_id": thread_id, "turn_id": turn_id or prior_turn, "status": "running", "port": port,
                "continuation_owed": False
            })
        return MovedWorker(moved, port, turn_id, thread_id, replaced, moved.serving)
    except (ConnectionError, OSError, SystemExit) as error:
        if isinstance(error, ServingRequestFailure) and error.reported:
            return WorkerAwaitingQuotaRecovery(moved, moved.port, "", thread_id, replaced, moved.serving)
        _record_worker_move_failure(request, str(error) or type(error).__name__)
        if isinstance(cleanup, NoWorkerTurnToCleanUp):
            raise
        if moved is not client:
            moved.close()
        return WorkerThreadStateUnknown(str(error) or type(error).__name__, cleanup,
            error.serving if isinstance(error, ServingRequestFailure) else moved.serving,
            error.reported if isinstance(error, ServingRequestFailure) else False)


def _start_workers_turn(
    args: argparse.Namespace, session_dir: str, name: str, thread_id: str,
    client: Client, message: str,
) -> MovedWorker:
    initial_client = client
    try:
        deadline = time.monotonic() + _as_float(_attr(args, "timeout"), 86400.0)
        while time.monotonic() < deadline:
            result = _move_resident(args, session_dir, name, thread_id, client, message=message)
            if isinstance(result, WorkerThreadStateUnknown):
                raise WorkerMoveUncertain(thread_id, result)
            if isinstance(result, MovedWorker) and not isinstance(result, WorkerAwaitingQuotaRecovery):
                return result
            if isinstance(result, WorkerAwaitingQuotaRecovery):
                client, thread_id = result.client, result.thread_id
            if _continuation_is_owed(session_dir, name):
                # A rejected submission's prompt was already handed to Codex; only the continuation is owed.
                message = ""
            if _end_marker_path(session_dir, name).exists():
                raise SystemExit(f"thread {thread_id}: ended while waiting; no turn started")
            time.sleep(RESIDENT_POLL_SECS)
        raise SystemExit(f"thread {thread_id}: still live at the timeout; no new turn started")

    except (ConnectionError, OSError, SystemExit):
        if client is not initial_client:
            client.close()
        raise


def _start_turn(
    client: Client, args: argparse.Namespace, thread_id: str, message: str,
    pending: tuple[str, ...] = (),
) -> str:
    inputs: list[dict[str, str]] = [{"type": "text", "text": message}]
    inputs.extend({"type": "text", "text": item} for item in pending)
    images = _attr(args, "image")
    if isinstance(images, list):
        inputs.extend(
            {"type": "localImage", "path": path}
            for path in cast("list[object]", images) if isinstance(path, str)
        )
    params: dict[str, object] = {"threadId": thread_id, "input": inputs}
    cwd = _as_str(_attr(args, "turn_cwd"))
    if cwd:
        params["cwd"] = cwd
    if _attr(args, "read_only") is True:
        params["sandboxPolicy"] = {"type": "readOnly", "networkAccess": False}
    model = _as_str(_attr(args, "model"))
    if model:
        params["model"] = model
    effort = _as_str(_attr(args, "effort"))
    if effort:
        params["effort"] = effort
    client.turn_submitted_at = time.time()
    result = _serving_request(client, "turn/start", params, submits_work=True)
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
                reply = _interrupt_turn(client, thread_id, turn_id.turn_id)
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
        if _messages_closed(pending_file, record["thread_id"]):
            print(f"codex_mesh: {target}'s launcher is closing; message not delivered; relaunch the worker", file=sys.stderr)
            return 2
        if status not in LAUNCHER_ATTACHED_STATUSES:
            raise SystemExit(
                f"codex_mesh: {target} is {status}, not running; read its summary file"
            )
        launcher_pid = record.get("launcher_pid")
        if not isinstance(launcher_pid, int) or launcher_pid <= 0 or not _pid_alive(launcher_pid):
            print(f"codex_mesh: {target} has no live launcher to read a message; end the worker and relaunch it", file=sys.stderr)
            return 2
        home = workers_home()
        # Retiring is enough here. A live launcher keeps its outgoing connection
        # and claims this held message at its next checked turn boundary.
        with _server_lock(session_dir):
            current = _read_json_object(_session_path(session_dir, SERVER_FILE))
            if current and server_record_home(current) != home:
                _retire_record(session_dir, current)
        messages = _pending_for_thread(pending_file, record["thread_id"])
        messages.append(_message_text(args))
        _write_pending(pending_file, record["thread_id"], messages)

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
    client = Client(_worker_port(session_dir, record), f"steer-{os.getpid()}")
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
            _ = _update_roster_if_expected(
                session_dir,
                target,
                {"thread_id": record["thread_id"], "turn_id": live, "status": "running"},
                RosterOnThread(record["thread_id"]),
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
    if status == "ended":
        print(f"{target} is ended; nothing to end")
        return 0
    if status not in ENDABLE_STATUSES:
        print(f"{target} is {status}; nothing to end")
        return 0
    with _pending_file(session_dir, target) as pending_file:
        _end_marker_path(session_dir, target).touch()
        _ = pending_file.seek(0)
        _ = pending_file.truncate()
    _mark_roster_ended(session_dir, target)
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
            _ = _interrupt_turn(client, thread_id, state.turn_id)
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


def _interrupt_turn(client: Client, thread_id: str, turn_id: str) -> RpcMessage:
    """Interrupt one identified turn without conflating account moves with user stops."""
    return client.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})


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
    _mark_all_roster_ended(session_dir)
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
    deadline = time.time() + REAP_WAIT_SECS
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(REAP_POLL_SECS)
    if _pid_alive(pid):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)
    return True


# The run roots, each holding run folders and their run-active markers: unit/, and delegate/ for
# a run started before the rename. A sweep always judges a server against every real root, since a
# server another root owns would otherwise look unowned.
SWEEP_ROOTS = unit_runs.run_roots()
# How long a server and its run folder must have sat untouched before it counts as unused: the
# one figure behind both the server's own watcher and the sweep.
SERVER_IDLE_SECS = 1800.0
# How often a server's watcher wakes to look at its record.
WATCH_WAKE_SECS = 300
# Files in a run folder whose change means the run is still working. `ensure_server` touches the
# server's record on every use.
SWEEP_ACTIVITY_FILES = (unit_runs.HEARTBEAT, "board.log", ROSTER_FILE, SERVER_LOG, SERVER_FILE)
SERVER_ARGUMENTS = ("app-server", "--listen")


@dataclass(frozen=True)
class ServerInUse:
    reason: str


@dataclass(frozen=True)
class ServerUnused:
    reason: str


SweepVerdict = ServerInUse | ServerUnused


@dataclass(frozen=True)
class CurrentRunServer:
    """A run's current mesh_server.json names this server."""

    session_dir: Path


@dataclass(frozen=True)
class RetiredRunServer:
    """A run's mesh_retired.json names this server until it becomes idle."""

    session_dir: Path


@dataclass(frozen=True)
class UnownedServerWithLog:
    """No record names this server, but its log handle identifies its old run."""

    session_dir: Path


@dataclass(frozen=True)
class UnownedServerWithoutLog:
    """No record or mesh log handle identifies this server's run."""


ServerOwnership = (
    CurrentRunServer
    | RetiredRunServer
    | UnownedServerWithLog
    | UnownedServerWithoutLog
)


@dataclass(frozen=True)
class SweepJudgment:
    """A verdict together with the ownership state used to reach it."""

    verdict: SweepVerdict
    ownership: ServerOwnership


@dataclass(frozen=True)
class CheckedRunLifecycle:
    """Run-liveness evidence collected by a root-wide sweep."""

    marked_active: bool
    running_workers: tuple[str, ...]


@dataclass(frozen=True)
class UncheckedRunLifecycle:
    """The per-server idle watcher deliberately did not inspect run markers."""


RunLifecycle = CheckedRunLifecycle | UncheckedRunLifecycle


@dataclass(frozen=True)
class ServerFacts:
    """What the sweep gathered about one running app-server without talking to it."""

    pid: int
    port: int
    age_secs: float
    clients: int
    ownership: ServerOwnership
    folder_exists: bool
    run_lifecycle: RunLifecycle
    # Workers in the run folder's roster whose launcher process is alive.
    live_launchers: tuple[str, ...]
    # Seconds since the run folder's newest activity file changed; None when it has none.
    quiet_secs: float | None


def _sweep_verdict(facts: ServerFacts, busy_threads: Callable[[], tuple[str, ...]]) -> SweepVerdict:
    """Whether anything still needs this app-server. Every doubt counts as in use, and the server
    is asked about its conversations last, so none can be called unused without that answer."""
    if facts.clients:
        return ServerInUse(f"{facts.clients} client connection(s) on its port")
    if isinstance(facts.ownership, CurrentRunServer) and isinstance(
        facts.run_lifecycle, CheckedRunLifecycle
    ):
        if facts.run_lifecycle.marked_active:
            return ServerInUse("its run has an active marker")
        if facts.run_lifecycle.running_workers:
            return ServerInUse(
                "its roster has a running worker: " + ", ".join(facts.run_lifecycle.running_workers)
            )
    if isinstance(facts.ownership, RetiredRunServer):
        # The run's marker, launchers and activity belong to its newer server. Only a worker still
        # attached here holds a connection, and a turn whose launcher died shows as a conversation.
        if busy := busy_threads():
            return ServerInUse("a conversation is not idle: " + "; ".join(busy))
        return ServerUnused("retired by its run; no client and no conversation mid-turn")
    if isinstance(facts.ownership, (UnownedServerWithLog, UnownedServerWithoutLog)):
        # A server may begin listening just before _start_server records it. Its run-folder lock
        # closes that race on the second look when the log names one; the age guard closes it on
        # platforms that cannot recover the log handle.
        if facts.age_secs < SERVER_START_TIMEOUT_SECS:
            return ServerInUse(
                f"unowned, but still within the {SERVER_START_TIMEOUT_SECS:.0f} s record-start window"
            )
        if busy := busy_threads():
            return ServerInUse("unowned, but a conversation is not idle: " + "; ".join(busy))
        return ServerUnused(
            "no current or retired server record; no client and no conversation mid-turn"
        )
    if facts.live_launchers:
        # A resident worker waits between turns with nothing on the socket.
        return ServerInUse("a worker's launcher is still running: " + ", ".join(facts.live_launchers))
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
    """(pid, WebSocket port, age in seconds) for each standalone app-server."""
    listed = subprocess.run(["ps", "-eo", "pid=,etimes=,args="], capture_output=True, text=True, check=True)
    servers: list[tuple[int, int, float]] = []
    for line in listed.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) < 3:
            continue
        try:
            command = shlex.split(fields[2])
        except ValueError:
            continue
        if not command or Path(command[0]).name != "codex":
            continue
        server_index = next((index for index in range(1, len(command) - 2)
                             if tuple(command[index:index + 2]) == SERVER_ARGUMENTS), -1)
        if server_index < 0:
            continue
        address = command[server_index + 2]
        if not address.startswith("ws://"):
            continue
        port = address.rpartition(":")[2]
        if port.isdigit() and fields[0].isdigit() and fields[1].isdigit():
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


def _record_names_server(record: dict[str, object], pid: int, port: int) -> bool:
    return record.get("pid") == pid and record.get("port") == port


def _retired_file_names_server(path: Path, pid: int, port: int) -> bool:
    stored = _read_json_object(path).get("servers")
    records = cast("list[object]", stored) if isinstance(stored, list) else []
    return any(_record_names_server(_as_dict(record), pid, port) for record in records)


def _server_ownership(
    roots: tuple[Path, ...] | None, pid: int, port: int, log_folder: Path | None
) -> ServerOwnership:
    """Resolve the record that owns a server; a live current owner wins stale duplicates."""
    if roots is None:
        folders = (log_folder,) if log_folder is not None else ()
    else:
        current_folders = {path.parent for root in roots for path in root.glob(f"*/{SERVER_FILE}")}
        retired_folders = {path.parent for root in roots for path in root.glob(f"*/{RETIRED_FILE}")}
        folders = tuple(sorted(current_folders | retired_folders, key=str))
    current_owners = tuple(
        folder
        for folder in folders
        if _record_names_server(_read_json_object(folder / SERVER_FILE), pid, port)
    )
    if roots is not None:
        for folder in current_owners:
            quiet = _quiet_secs(folder, time.time())
            if (
                _marked_active(roots, folder)
                or _running_roster_workers(folder)
                or _live_launchers(folder)
                or (quiet is not None and quiet < SERVER_IDLE_SECS)
            ):
                return CurrentRunServer(folder)
    for folder in folders:
        if _retired_file_names_server(folder / RETIRED_FILE, pid, port):
            return RetiredRunServer(folder)
    if current_owners:
        return CurrentRunServer(current_owners[0])
    if log_folder is not None:
        return UnownedServerWithLog(log_folder)
    return UnownedServerWithoutLog()


def _marked_active(roots: tuple[Path, ...], folder: Path) -> bool:
    """Whether a run-active marker names `folder`. A marker that cannot be read counts as naming it."""
    actives = [root / "active" for root in roots]
    for marker in (found for active in actives if active.is_dir() for found in sorted(active.iterdir())):
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


def _running_roster_workers(folder: Path) -> tuple[str, ...]:
    """Workers whose roster state says a turn is in progress, whether or not its launcher is alive."""
    roster = _read_json_object(folder / ROSTER_FILE)
    return tuple(
        name
        for name, entry in sorted(roster.items())
        if _as_dict(entry).get("status") == "running"
    )


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
    """Thread id to worker name for every conversation the run folder's roster lists."""
    roster = _read_json_object(folder / ROSTER_FILE)
    named = {_as_str(_as_dict(entry).get("thread_id")): name for name, entry in sorted(roster.items())}
    return {thread_id: name for thread_id, name in named.items() if thread_id}


def _busy_threads(port: int, workers: dict[str, str]) -> tuple[str, ...]:
    """Conversations on this server that are not idle, or why they could not be read.

    The server lists what it has loaded, so a turn left running by a launcher killed at its time
    limit is seen with no client connected and no roster. `workers` adds the roster's conversations
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
        thread_ids = {*workers, *(_as_str(thread_id) for thread_id in cast("list[object]", loaded))} - {""}
        busy: list[str] = []
        for thread_id in sorted(thread_ids):
            state = _read_live_turn(client, thread_id)
            if not isinstance(state, ThreadIdle):
                busy.append(f"{workers.get(thread_id, thread_id)}: {state.__class__.__name__}")
        return tuple(busy)
    except (ConnectionError, OSError, SystemExit) as exc:
        return (f"the server stopped answering ({str(exc) or exc.__class__.__name__})",)
    finally:
        client.close()


def _judge(roots: tuple[Path, ...] | None, pid: int, port: int, age: float,
           clients: int) -> SweepJudgment:
    """Judge one running app-server without erasing its ownership state.

    `roots` hold the run-active markers. A server judging itself passes None: a stopped server
    comes back at the next use, so a run that is only between dispatches does not need it kept.
    """
    log_folder = _server_folder(pid)
    ownership = _server_ownership(roots, pid, port, log_folder)
    folder = None if isinstance(ownership, UnownedServerWithoutLog) else ownership.session_dir
    exists = folder is not None and folder.is_dir()
    lifecycle: RunLifecycle = UncheckedRunLifecycle() if roots is None or folder is None else CheckedRunLifecycle(
        _marked_active(roots, folder),
        _running_roster_workers(folder) if exists else (),
    )
    facts = ServerFacts(
        pid, port, age, clients, ownership, exists, lifecycle,
        live_launchers=_live_launchers(folder) if folder is not None and exists else (),
        quiet_secs=_quiet_secs(folder, time.time()) if folder is not None and exists else None,
    )
    workers = _roster_threads(folder) if folder is not None and exists else {}
    return SweepJudgment(
        _sweep_verdict(facts, lambda: _busy_threads(port, workers)), ownership
    )


def _stop_unused(
    roots: tuple[Path, ...] | None, pid: int, port: int, ownership: ServerOwnership
) -> bool:
    """Stop a server a first look called unused, if a second look at this moment agrees.

    The second look runs under the run folder's server lock, so a launcher reaching
    `ensure_server` meanwhile waits and then starts a server of its own. A deleted folder has no
    launcher to wait and is not created again for the lock."""
    if isinstance(ownership, UnownedServerWithoutLog):
        return _stop_unused_with_lock_held(roots, pid, port)
    folder = ownership.session_dir
    if not folder.is_dir():
        return _stop_unused_with_lock_held(roots, pid, port)
    with _server_lock(str(folder)):
        return _stop_unused_with_lock_held(roots, pid, port)


def _stop_unused_with_lock_held(roots: tuple[Path, ...] | None, pid: int, port: int) -> bool:
    """Repeat an unused-server judgment, then stop and forget that exact owner."""
    try:
        running = {(found, at): age for found, at, age in _running_servers()}
        clients = _client_counts()
    except (OSError, subprocess.CalledProcessError):
        return False
    # A pid that no longer runs this server on this port is not the process that was judged.
    age = running.get((pid, port))
    if age is None:
        return False
    judgment = _judge(roots, pid, port, age, clients.get(port, 0))
    if not isinstance(judgment.verdict, ServerUnused) or not _reap(pid):
        return False
    if isinstance(judgment.ownership, UnownedServerWithoutLog):
        return True
    folder = judgment.ownership.session_dir
    if folder.is_dir():
        _forget_retired(folder, pid)
        record = folder / SERVER_FILE
        if _record_names_server(_read_json_object(record), pid, port):
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
        judgment = _judge(None, pid, port, age, clients.get(port, 0))
        if isinstance(judgment.verdict, ServerUnused) and _stop_unused(
            None, pid, port, judgment.ownership
        ):
            _log_server(
                str(folder),
                f"app-server {pid} stopped itself: {judgment.verdict.reason}",
            )
            return 0
    if _read_json_object(record).get("pid") == pid:
        with contextlib.suppress(OSError):
            os.utime(record)
    return 1


def _sweep_roots(args: argparse.Namespace) -> tuple[Path, ...]:
    """The roots `--root` names: every real root when it names one of them, since a run under the
    other root keeps its server too."""
    given = _as_str(_attr(args, "root"))
    if not given:
        return SWEEP_ROOTS
    real = {root.resolve() for root in SWEEP_ROOTS}
    return SWEEP_ROOTS if Path(given).resolve() in real else (Path(given),)


def command_sweep(args: argparse.Namespace) -> int:
    """Print every running app-server with whether anything still needs it. Stops one only
    under `--stop`, and then only a server two looks in a row called unused."""
    roots = _sweep_roots(args)
    stop = _attr(args, "stop") is True
    try:
        servers, clients = _running_servers(), _client_counts()
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"codex_mesh: sweep: cannot list servers or connections ({exc}); nothing judged", file=sys.stderr)
        return 1
    unused = stopped = 0
    for pid, port, age in sorted(servers):
        judgment = _judge(roots, pid, port, age, clients.get(port, 0))
        label = "unused" if isinstance(judgment.verdict, ServerUnused) else "in use"
        reason = judgment.verdict.reason
        if isinstance(judgment.verdict, ServerUnused):
            unused += 1
            if stop and _stop_unused(roots, pid, port, judgment.ownership):
                stopped += 1
                label = "stopped"
            elif stop:
                label, reason = "kept", "unused at the first look, not stopped at the second"
        owner = (
            "-"
            if isinstance(judgment.ownership, UnownedServerWithoutLog)
            else str(judgment.ownership.session_dir)
        )
        print(f"{pid}\t{port}\t{label}\t{reason}\t{owner}")
    tail = f"{stopped} stopped" if stop else "report only, nothing was stopped"
    print(f"{len(servers)} app-server(s), {unused} unused; {tail}")
    return 0


# Delivers the sign-in change notice to the session holding a run.
SEND_SCRIPT = Path(__file__).resolve().parent.parent / "message" / "send.py"
NOTICE_TIMEOUT_SECS = 120


def _marker_sessions(roots: tuple[Path, ...], folder: Path) -> list[str]:
    """The sessions whose run-active marker names `folder`: the marker's file name is the session id."""
    actives = [root / "active" for root in roots]
    holders: list[str] = []
    for marker in (found for active in actives if active.is_dir() for found in sorted(active.iterdir())):
        try:
            lines = marker.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        if lines and lines[0].strip() and Path(lines[0].strip()).resolve() == folder.resolve():
            holders.append(marker.name)
    return holders


class CodexNoticeKind(Enum):
    WORKERS_CHANGED = "workers_changed"
    SIGN_IN_CHANGED = "sign_in_changed"


def _notify_holder(session_id: str, line: str, kind: CodexNoticeKind) -> None:
    workers_changed = kind is CodexNoticeKind.WORKERS_CHANGED
    try:
        sent = subprocess.run(
            [sys.executable, str(SEND_SCRIPT), "--to", f"session:{session_id}", "--from", "codex-workers" if workers_changed else "codex-sign-in",
             "--summary", "Codex workers account changed" if workers_changed else "Codex sign-in changed", "--text", line],
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
    dispatch starts a server that reads the current sign-in. Running workers are interrupted and
    resume immediately on that server. The old server is stopped now if
    nothing uses it, otherwise by its watcher once nothing does. Run twice for one change, the
    second run finds nothing older and moves nothing.
    """
    roots = _sweep_roots(args)
    home = codex_home()
    changed = _sign_in_changed_at(home)
    if changed is None:
        print("codex_mesh: no Codex sign-in file; nothing moved")
        return 0
    at = datetime.fromtimestamp(changed).astimezone(user_zone()).strftime("%H:%M %Z")
    runs: list[WorkersRunMove] = []
    for record_path in sorted(path for root in roots for path in root.glob(f"*/{SERVER_FILE}")):
        folder = record_path.parent
        with _server_lock(str(folder)):
            record = _read_json_object(record_path)
            port, pid = record.get("port"), record.get("pid")
            recorded_home = server_record_home(record)
            if not (recorded_home == home
                    and isinstance(port, int) and isinstance(pid, int) and _pid_alive(pid)
                    and _holds_old_sign_in(pid, recorded_home)):
                continue
            _retire_record(str(folder), record)
        workers = _switch_run_workers(folder, port, codex_home_role_label(workers_home()), dry_run=False)
        runs.append(WorkersRunMove(folder, tuple(_marker_sessions(roots, folder)), ((port, pid),), workers))
    switches = _finish_run_moves(runs)
    for run, switched in zip(runs, switches, strict=True):
        folder = run.folder
        port, pid = run.servers[0]
        stopped = _stop_unused(None, pid, port, RetiredRunServer(folder))
        if not stopped:
            _ = _start_watcher(str(folder), pid, port)
        old = "is stopped" if stopped else "stops once nothing uses it"
        line = (f"Codex sign-in changed at {at}: run {folder.name}'s app-server {pid} started before it, so the "
                + "run's running workers were interrupted and resumed on a new server; "
                + f"idle workers move at their next turn; the old server {old}.")
        failures = [f"{worker.name}: {worker.failure}; resume with {worker.resume_command}"
                    for worker in switched.workers if worker.failure]
        if failures:
            line += " Worker move failures: " + "; ".join(failures)
        _log_server(str(folder), line)
        for session_id in switched.holders:
            _notify_holder(session_id, line, CodexNoticeKind.SIGN_IN_CHANGED)
        print(line)
    print(f"{len(switches)} run(s) moved off the old sign-in")
    return 0


@dataclass(frozen=True)
class WorkerMoveReady:
    """An idle worker moves at its next turn, or this is a dry-run prediction."""


@dataclass(frozen=True)
class WorkerMoveResumed:
    """The interrupted worker confirmed a continuation on its new server."""


@dataclass(frozen=True)
class WorkerMoveFailed:
    reason: str
    command: str


WorkerSwitchOutcome = WorkerMoveReady | WorkerMoveResumed | WorkerMoveFailed


@dataclass(frozen=True)
class SwitchingWorker:
    name: str
    mid_turn: bool
    outcome: WorkerSwitchOutcome = field(default_factory=WorkerMoveReady)

    @property
    def failure(self) -> str:
        return self.outcome.reason if isinstance(self.outcome, WorkerMoveFailed) else ""

    @property
    def resume_command(self) -> str:
        return self.outcome.command if isinstance(self.outcome, WorkerMoveFailed) else ""


@dataclass(frozen=True)
class WorkersRunSwitch:
    folder: Path
    holders: tuple[str, ...]
    workers: tuple[SwitchingWorker, ...]


@dataclass(frozen=True)
class InterruptedWorkerMove:
    """An interrupted worker whose continuation has not yet been acknowledged."""

    session_dir: str
    name: str
    marker: AccountMoveMarker
    launcher_pid: int


@dataclass(frozen=True)
class WorkersRunMove:
    """A retired run with all interrupts dispatched, ready to await continuations."""

    folder: Path
    holders: tuple[str, ...]
    servers: tuple[tuple[int, int], ...]  # port, pid
    workers: tuple[SwitchingWorker | InterruptedWorkerMove, ...]


def _account_move_command(session_dir: str, name: str) -> str:
    return shlex.join([sys.executable, str(Path(__file__).resolve()), "resume-account-move",
                       "--session-dir", session_dir, "--to", name])


def _resume_account_move_detached(session_dir: str, name: str) -> None:
    """Give an interrupted worker without a launcher its own continuation watcher."""
    with _session_path(session_dir, f"{name}.account-move-launcher.log").open("ab") as log:
        _ = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "resume-account-move",
             "--session-dir", session_dir, "--to", name],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
        )


def command_resume_account_move(args: argparse.Namespace) -> int:
    session_dir, name = _as_str(_attr(args, "session_dir")), _as_str(_attr(args, "to"))
    record = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
    marker = _read_account_move(session_dir, name, _as_str(record.get("thread_id")))
    if isinstance(marker, NoAccountMove):
        port = record.get("port")
        if not isinstance(port, int) or not _as_str(record.get("thread_id")):
            print(f"codex_mesh: {name}: no recorded thread to resume", file=sys.stderr)
            return 1
        retried = _interrupt_worker_for_account_move(
            session_dir, name, record, port, codex_home_role_label(workers_home()))
        outcome = (_wait_account_move_resumes((retried,))[(session_dir, name)]
                   if isinstance(retried, InterruptedWorkerMove) else retried.outcome)
        if isinstance(outcome, WorkerMoveFailed):
            print(f"codex_mesh: {name}: {outcome.reason}", file=sys.stderr)
            return 1
        return 0
    role = _roster_role(record.get("role"))
    if not isinstance(role, AssignedDelegateRole):
        _fail_account_move(session_dir, name, marker, "resume failed: worker has no recorded role")
        return 1
    launcher_pid = _as_int(record.get("launcher_pid"))
    if launcher_pid > 0 and _pid_alive(launcher_pid):
        _fail_account_move(session_dir, name, marker, "resume failed: existing launcher is still running")
        return 1
    assignment = agents_log.role_at(
        agents_log.assignments(agents_log.state_dir() / "agents.jsonl"),
        marker.thread_id, datetime.now(timezone.utc),
    )
    lens = assignment.lens if isinstance(assignment, agents_log.Assignment) and assignment.role == role.role else None
    claimed: StartingRecord = {
        "thread_id": marker.thread_id, "status": "starting", "launcher_pid": os.getpid(),
        "role": role.role, "previous_role": role.role, "previous_status": "failed",
    }
    if not _update_roster_if_expected(session_dir, name, claimed,
                                     RosterUnchanged(marker.thread_id, _roster_revision(record))):
        _fail_account_move(session_dir, name, marker, "resume failed: worker changed before launcher claim")
        return 1
    message = _session_path(session_dir, f"{name}.account-move-prompt.txt")
    _ = message.write_text(ACCOUNT_MOVE_CONTINUATION, encoding="utf-8")
    follow_args = argparse.Namespace(
        session_dir=session_dir, to=name, role=role.role, lens=lens, claim_pid=os.getpid(),
        message_file=str(message), summary_file=str(_session_path(session_dir, f"{name}.account-move-summary.txt")),
        reply_file="", log_file=str(_session_path(session_dir, f"{name}.account-move.log")),
        model="", effort="", service_tier="", turn_cwd="", read_only=False, image=[],
        timeout=86400.0, account_move=True,
    )
    try:
        result = command_follow(follow_args)
    except (ConnectionError, OSError, SystemExit) as error:
        _fail_account_move(session_dir, name, marker, f"resume failed: {error}")
        return 1
    if result and isinstance(_read_account_move(session_dir, name, marker.thread_id), AccountMoveMarker):
        _fail_account_move(session_dir, name, marker, "resume failed; see account-move launcher log")
    return result


def _wait_account_move_resumes(
    moves: tuple[InterruptedWorkerMove, ...],
) -> dict[tuple[str, str], WorkerSwitchOutcome]:
    """Await every dispatched move against one deadline, without serial worker waits."""
    pending = {(move.session_dir, move.name): move for move in moves}
    outcomes: dict[tuple[str, str], WorkerSwitchOutcome] = {}
    detached: set[tuple[str, str]] = set()
    deadline = time.monotonic() + ACCOUNT_MOVE_RESUME_TIMEOUT_SECS
    while pending:
        for key, move in tuple(pending.items()):
            try:
                result = _read_json_object(_account_move_result_path(move.session_dir, move.name))
                if result.get("move_id") == move.marker.move_id:
                    if result.get("resumed") is True:
                        outcomes[key] = WorkerMoveResumed()
                    elif reason := _as_str(result.get("error")):
                        outcomes[key] = WorkerMoveFailed(reason, _account_move_command(*key))
                if key in outcomes:
                    del pending[key]
                    continue
                if key not in detached and (move.launcher_pid <= 0 or not _pid_alive(move.launcher_pid)):
                    _resume_account_move_detached(*key)
                    detached.add(key)
            except (ConnectionError, OSError, SystemExit) as error:
                outcomes[key] = WorkerMoveFailed(f"resume failed: {error}", _account_move_command(*key))
                del pending[key]
        if not pending:
            break
        if time.monotonic() >= deadline:
            for key in pending:
                outcomes[key] = WorkerMoveFailed("resume not confirmed before timeout", _account_move_command(*key))
            break
        time.sleep(0.05)
    return outcomes


def _finish_run_moves(runs: list[WorkersRunMove]) -> tuple[WorkersRunSwitch, ...]:
    outcomes = _wait_account_move_resumes(tuple(
        worker for run in runs for worker in run.workers if isinstance(worker, InterruptedWorkerMove)
    ))
    return tuple(WorkersRunSwitch(run.folder, run.holders, tuple(
        SwitchingWorker(worker.name, True, outcomes[(worker.session_dir, worker.name)])
        if isinstance(worker, InterruptedWorkerMove) else worker for worker in run.workers
    )) for run in runs)


def _interrupt_worker_for_account_move(session_dir: str, name: str, entry: dict[str, object],
                                       port: int, label: str) -> SwitchingWorker | InterruptedWorkerMove:
    thread_id = _as_str(entry.get("thread_id"))
    marker: AccountMoveMarker | NoAccountMove = NoAccountMove()
    client: Client | None = None
    try:
        if not thread_id:
            raise SystemExit("worker has no recorded thread")
        client = Client(port, f"account-move-{name}")
        state = _interruptible_turn_id(client, thread_id, _read_live_turn(client, thread_id))
        if isinstance(state, ThreadStateUnknown):
            raise SystemExit(state.reason)
        if isinstance(state, ThreadActiveWithoutTurn):
            raise SystemExit("live turn has no interruptible id")
        # The turn may have just ended. It still needs a continuation if its launcher
        # has not consumed that completion, so retain the roster's last turn id.
        turn_id = state.turn_id if isinstance(state, ThreadLive) else _as_str(entry.get("turn_id"))
        if not turn_id:
            return SwitchingWorker(name, False)
        marker = AccountMoveMarker(thread_id, turn_id, label, secrets.token_hex(16))
        stored: StoredAccountMove = {"thread_id": marker.thread_id, "turn_id": marker.turn_id,
                                    "label": marker.label, "move_id": marker.move_id}
        with _pending_file(session_dir, name):
            if _end_marker_path(session_dir, name).exists() or not _roster_still_on_thread(session_dir, name, thread_id):
                raise SystemExit("worker ended or was replaced before interruption")
            _write_account_move_json(_account_move_path(session_dir, name), stored)
        if isinstance(state, ThreadLive):
            reply = _interrupt_turn(client, thread_id, state.turn_id)
            if "error" in reply and not isinstance(_read_live_turn(client, thread_id), ThreadIdle):
                _account_move_path(session_dir, name).unlink(missing_ok=True)
                _ = _require(reply, "turn/interrupt")
        client.close()
        client = None
        return InterruptedWorkerMove(session_dir, name, marker, _as_int(entry.get("launcher_pid")))
    except (ConnectionError, OSError, SystemExit) as error:
        reason = f"interrupt failed: {error}"
        if isinstance(marker, AccountMoveMarker):
            with contextlib.suppress(OSError):
                _fail_account_move(session_dir, name, marker, reason)
        return SwitchingWorker(name, True, WorkerMoveFailed(reason, _account_move_command(session_dir, name)))
    finally:
        if client is not None:
            client.close()


def _switch_run_workers(
    folder: Path, port: int, label: str, *, dry_run: bool,
) -> tuple[SwitchingWorker | InterruptedWorkerMove, ...]:
    entries = _read_json_object(folder / ROSTER_FILE)
    workers: list[SwitchingWorker | InterruptedWorkerMove] = []
    for name, value in sorted(entries.items()):
        entry = _as_dict(value)
        if entry.get("status") == "ended" or entry.get("port", port) != port:
            continue
        if entry.get("status") == "running" and not dry_run:
            workers.append(_interrupt_worker_for_account_move(str(folder), name, entry, port, label))
        else:
            workers.append(SwitchingWorker(name, entry.get("status") == "running"))
    return tuple(workers)


def retire_workers_servers(workers_home: Path, label: str, *, dry_run: bool = False) -> tuple[WorkersRunSwitch, ...]:
    """Retire run servers, interrupt running turns, and confirm their continuations."""
    notices: dict[str, list[str]] = {}
    notice_failures: dict[str, list[str]] = {}
    runs: list[WorkersRunMove] = []
    at = datetime.now().astimezone(user_zone()).strftime("%H:%M %Z")
    folders = sorted({path.parent for root in SWEEP_ROOTS
                      for filename in (SERVER_FILE, RETIRED_FILE) for path in root.glob(f"*/{filename}")})
    for folder in folders:
        with _server_lock(str(folder)):
            current = _read_json_object(folder / SERVER_FILE)
            outgoing: dict[int, int] = {}
            for record in _recorded_run_servers(str(folder)):
                pid, port = record.get("pid"), record.get("port")
                if (isinstance(pid, int) and isinstance(port, int) and _pid_alive(pid)
                        and server_record_home(record) != workers_home):
                    outgoing[port] = pid
            if not outgoing:
                continue
            holders = tuple(_marker_sessions(SWEEP_ROOTS, folder))
            current_port = current.get("port")
            if not dry_run and isinstance(current_port, int) and current_port in outgoing:
                _retire_record(str(folder), current)
        workers = tuple(worker for port in outgoing
                        for worker in _switch_run_workers(folder, port, label, dry_run=dry_run))
        runs.append(WorkersRunMove(folder, holders, tuple(outgoing.items()), workers))
    switches = _finish_run_moves(runs)
    for run, switched in zip(runs, switches, strict=True):
        folder, holders, workers = switched.folder, switched.holders, switched.workers
        line = (
            f"Workers account {'would move' if dry_run else 'changed'} at {at} to {label} "
            + f"({codex_home_display(workers_home)}): run {folder.name}'s running workers "
            + "were interrupted and resumed on the new account; idle workers move at their next turn."
        )
        failures = [f"{worker.name}: {worker.failure}; resume with {worker.resume_command}"
                    for worker in workers if worker.failure]
        if failures:
            line = (f"Workers account changed at {at} to {label}: running workers were interrupted; "
                    + "some continuations failed: " + "; ".join(failures))
        if not dry_run:
            _log_server(str(folder), line)
            for port, pid in run.servers:
                if not _stop_unused(None, pid, port, RetiredRunServer(folder)):
                    _ = _start_watcher(str(folder), pid, port)
        for session_id in holders:
            notices.setdefault(session_id, []).append(folder.name)
            notice_failures.setdefault(session_id, []).extend(failures)
    if not dry_run:
        for session_id, holder_runs in notices.items():
            _notify_holder(
                session_id,
                f"Workers account changed at {at} to {label} ({codex_home_display(workers_home)}): "
                + f"runs {', '.join(holder_runs)}: running workers were interrupted and resumed on the new account; "
                + "idle workers move at their next turn."
                + (" Worker move failures: " + "; ".join(notice_failures[session_id])
                   if notice_failures[session_id] else ""),
                CodexNoticeKind.WORKERS_CHANGED,
            )
    return switches


def command_serve(args: argparse.Namespace) -> int:
    port, _fresh = ensure_server(_as_str(_attr(args, "session_dir")))
    print(port)
    return 0



def _failure_account_detail(error: BaseException, serving: CodexServingState) -> str:
    account = serving.account
    name = account.label if isinstance(account, KnownCodexAccount) else "unknown Codex account"
    return f"{name} ({codex_home_display(serving.home)}): {error}"


def command_prime(args: argparse.Namespace) -> int:
    """Create a read-only reviewer template without starting a turn."""
    session_dir = _as_str(_attr(args, "session_dir"))
    name = _as_str(_attr(args, "name"))
    client: Client | None = None
    try:
        instructions_path = Path(_as_str(_attr(args, "instructions_file"))).resolve()
        instructions = instructions_path.read_text(encoding="utf-8")
        params: dict[str, object] = {
            "developerInstructions": instructions,
            "cwd": _as_str(_attr(args, "cwd")),
            "sandbox": "read-only",
            "approvalPolicy": "never",
        }
        model = _as_str(_attr(args, "model"))
        if model:
            params["model"] = model
        service_tier = _as_str(_attr(args, "service_tier"))
        if service_tier:
            params["serviceTier"] = service_tier
        port, _fresh = ensure_server(session_dir)
        client = Client(port, name)
        _ = _bind_serving_server(client, session_dir, started_here=_fresh)
        result = _serving_request(client, "thread/start", params)
        thread_id = _as_str(_as_dict(result.get("thread")).get("id"))
        if not thread_id:
            raise SystemExit("thread/start returned no thread id")
        _ = _require(client.call("thread/name/set", {"threadId": thread_id, "name": name}), "thread/name/set")
        _update_roster(session_dir, name, {
            "thread_id": thread_id, "status": "done", "role": "review",
            "context_tokens": 0, "instructions_file": str(instructions_path),
        })
        print(thread_id)
        return 0
    except (ConnectionError, OSError, UnicodeError, SystemExit) as exc:
        if isinstance(exc, ServingRequestFailure):
            detail = _failure_account_detail(exc, exc.serving)
        else:
            detail = str(exc)
        print(f"codex_mesh: {name}: {detail}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()


def command_fork(args: argparse.Namespace) -> int:
    """Load a template and create a named read-only reviewer from it."""
    session_dir = _as_str(_attr(args, "session_dir"))
    name = _as_str(_attr(args, "name"))
    client: Client | None = None
    try:
        existing = _as_dict(_read_json_object(_session_path(session_dir, ROSTER_FILE)).get(name))
        if _as_str(existing.get("thread_id")) and existing.get("status") != "ended":
            print(f"codex_mesh: {name}: name already belongs to a thread; fork refused", file=sys.stderr)
            return 2
        expected: ExpectedRoster = (RosterEndedOnThread(_as_str(existing.get("thread_id")))
                                    if _as_str(existing.get("thread_id")) else RosterAbsent())
        source_name = _as_str(_attr(args, "from_name"))
        source_record = _lookup(session_dir, source_name)
        source = _roster_thread(source_record)
        if isinstance(source, InvalidThread):
            raise SystemExit(source.reason)
        port, _fresh = ensure_server(session_dir)
        released = _release_other_worker_writers(session_dir, source_name, source.thread_id, port)
        if not isinstance(released, _ReleasedWorkerThread):
            print(f"codex_mesh: {name}: {released.reason}; fork refused", file=sys.stderr)
            return 2
        client = Client(port, name)
        _ = _bind_serving_server(client, session_dir, started_here=_fresh)
        params = _resume_params(session_dir, source_name, source.thread_id)
        _ = _serving_request(client, "thread/resume", params)
        fork_params = dict(params)
        fork_params.update({"sandbox": "read-only", "approvalPolicy": "never"})
        result = _serving_request(client, "thread/fork", fork_params)
        thread_id = _as_str(_as_dict(result.get("thread")).get("id"))
        if not thread_id:
            raise SystemExit("thread/fork returned no thread id")
        _ = _require(client.call("thread/name/set", {"threadId": thread_id, "name": name}), "thread/name/set")
        record: FollowableRecord = {
            "thread_id": thread_id, "status": "done", "role": "review", "context_tokens": 0,
        }
        instructions_file = _as_str(source_record.get("instructions_file"))
        if instructions_file:
            record["instructions_file"] = instructions_file
        if not _update_roster_if_expected(session_dir, name, record, expected):
            released = _release_worker_thread(client, thread_id, session_dir)
            if isinstance(released, WorkerThreadStateUnknown):
                print(f"codex_mesh: {name}: name was taken while the fork ran; "
                      + f"fork thread {thread_id} not released: {released.reason}", file=sys.stderr)
                return 1
            print(f"codex_mesh: {name}: name was taken while the fork ran; fork refused", file=sys.stderr)
            return 2
        print(thread_id)
        return 0
    except (ConnectionError, OSError, SystemExit) as exc:
        if isinstance(exc, ServingRequestFailure):
            detail = _failure_account_detail(exc, exc.serving)
        else:
            detail = str(exc)
        print(f"codex_mesh: {name}: {detail}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()


def _clear_reset_context(session_dir: str, name: str, thread_id: str, launcher_pid: int) -> bool:
    """Clear context only while reset still owns this thread's worker claim."""
    path = _session_path(session_dir, ROSTER_FILE)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _ = handle.seek(0)
        roster = _as_dict(_loads(handle.read()))
        entry = _as_dict(roster.get(name))
        if (entry.get("thread_id") != thread_id or entry.get("status") != "starting"
                or entry.get("launcher_pid") != launcher_pid):
            return False
        entry["context_tokens"] = 0
        _advance_roster_revision(entry)
        roster[name] = entry
        _ = handle.seek(0)
        _ = handle.truncate()
        _ = handle.write(json.dumps(roster, indent=2, sort_keys=True))
        handle.flush()
        return True


def command_reset(args: argparse.Namespace) -> int:
    """Remove all review turns from an idle, followable thread."""
    session_dir = _as_str(_attr(args, "session_dir"))
    name = _as_str(_attr(args, "to"))
    client: Client | None = None
    claimed = False
    claimed_thread_id = ""
    owner_pid = os.getpid()
    try:
        state = _lookup_state(session_dir, name)
        if not isinstance(state, FollowableThread):
            reason = state.reason if isinstance(state, InvalidThread) else "worker is busy or not followable"
            print(f"codex_mesh: {name}: {reason}; reset refused", file=sys.stderr)
            return 2
        role = _roster_role(_lookup(session_dir, name).get("role"))
        reset_role = role.role if isinstance(role, AssignedDelegateRole) else "review"
        if not _claim_follow(session_dir, name, state.thread_id, owner_pid, reset_role):
            print(f"codex_mesh: {name}: worker was already claimed; reset refused", file=sys.stderr)
            return 2
        claimed = True
        claimed_thread_id = state.thread_id
        port, _fresh = ensure_server(session_dir)
        released = _release_other_worker_writers(session_dir, name, state.thread_id, port)
        if not isinstance(released, _ReleasedWorkerThread):
            print(f"codex_mesh: {name}: {released.reason}; reset refused", file=sys.stderr)
            return 2
        client = Client(port, name)
        _ = _bind_serving_server(client, session_dir, started_here=_fresh)
        _ = _serving_request(client, "thread/resume", _resume_params(session_dir, name, state.thread_id))
        read = _serving_request(client, "thread/read", {
            "threadId": state.thread_id, "includeTurns": False,
        })
        thread = _as_dict(read.get("thread"))
        status = _as_str(_as_dict(thread.get("status")).get("type"))
        if status != "idle":
            print(f"codex_mesh: {name}: thread state is {status or 'unknown'}; reset refused", file=sys.stderr)
            return 2
        result = _serving_request(client, "thread/turns/list", {
            "threadId": state.thread_id, "sortDirection": "asc", "limit": 1,
        })
        turns = result.get("data")
        if not isinstance(turns, list):
            raise SystemExit("thread/turns/list returned no turn list")
        if turns:
            first_turn_id = _as_str(_as_dict(cast("list[object]", turns)[0]).get("id"))
            if not first_turn_id:
                raise SystemExit("thread/turns/list returned no first turn id")
            _ = _serving_request(client, "thread/revert", {
                "threadId": state.thread_id, "beforeTurnId": first_turn_id,
            })
        if not _clear_reset_context(session_dir, name, state.thread_id, owner_pid):
            print(f"codex_mesh: {name}: worker claim changed; reset refused", file=sys.stderr)
            return 2
        return 0
    except (ConnectionError, OSError, SystemExit) as exc:
        if isinstance(exc, ServingRequestFailure):
            detail = _failure_account_detail(exc, exc.serving)
        else:
            detail = str(exc)
        print(f"codex_mesh: {name}: {detail}", file=sys.stderr)
        return 1
    finally:
        try:
            if client is not None:
                client.close()
        finally:
            if claimed:
                _restore_follow_claim(session_dir, name, owner_pid, expected_thread_id=claimed_thread_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="verb", required=True)

    serve = subparsers.add_parser("serve", help="start the session app-server")
    _ = serve.add_argument("--session-dir", required=True)
    serve.set_defaults(handler=command_serve)

    start = subparsers.add_parser("start", help="launch a delegate and block")
    _ = start.add_argument("--session-dir", required=True)
    _ = start.add_argument("--name", required=True)
    _ = start.add_argument("--role", choices=WORKER_ROLES, default="")
    _ = start.add_argument("--lens", choices=agents_log.LENSES)
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

    account_move = subparsers.add_parser("resume-account-move", help="resume an interrupted account move")
    _ = account_move.add_argument("--session-dir", required=True)
    _ = account_move.add_argument("--to", required=True)
    account_move.set_defaults(handler=command_resume_account_move)

    follow = subparsers.add_parser("follow", help="run one turn on a finished delegate")
    _ = follow.add_argument("--session-dir", required=True)
    _ = follow.add_argument("--to", required=True)
    _ = follow.add_argument("--role", choices=WORKER_ROLES, required=True)
    _ = follow.add_argument("--lens", choices=agents_log.LENSES)
    _ = follow.add_argument("--claim-pid", type=int, default=0)
    _ = follow.add_argument("--message-file", required=True)
    _ = follow.add_argument("--summary-file", required=True)
    _ = follow.add_argument("--reply-file", default="")
    _ = follow.add_argument("--log-file", required=True)
    _ = follow.add_argument("--model", default="")
    _ = follow.add_argument("--effort", default="")
    _ = follow.add_argument("--service-tier", default="")
    _ = follow.add_argument("--cwd", dest="turn_cwd", default="")
    _ = follow.add_argument("--read-only", action="store_true")
    _ = follow.add_argument("--image", action="append", default=[])
    _ = follow.add_argument("--timeout", type=float, default=86400.0)
    follow.set_defaults(handler=command_follow)

    prime = subparsers.add_parser("prime", help="create a read-only reviewer template without a turn")
    _ = prime.add_argument("--session-dir", required=True)
    _ = prime.add_argument("--name", required=True)
    _ = prime.add_argument("--instructions-file", required=True)
    _ = prime.add_argument("--cwd", required=True)
    _ = prime.add_argument("--model", default="")
    _ = prime.add_argument("--service-tier", default="")
    prime.set_defaults(handler=command_prime)

    fork = subparsers.add_parser("fork", help="create a named read-only fork of a template")
    _ = fork.add_argument("--session-dir", required=True)
    _ = fork.add_argument("--from", dest="from_name", required=True)
    _ = fork.add_argument("--name", required=True)
    fork.set_defaults(handler=command_fork)

    reset = subparsers.add_parser("reset", help="remove all turns from an idle reviewer")
    _ = reset.add_argument("--session-dir", required=True)
    _ = reset.add_argument("--to", required=True)
    reset.set_defaults(handler=command_reset)

    compact = subparsers.add_parser("compact", help="compact a finished delegate")
    _ = compact.add_argument("--session-dir", required=True)
    _ = compact.add_argument("--to", required=True)
    compact.set_defaults(handler=command_compact)

    can_follow = subparsers.add_parser("can-follow", help="check whether a worker can take a follow-up")
    _ = can_follow.add_argument("--session-dir", required=True)
    _ = can_follow.add_argument("--to", required=True)
    _ = can_follow.add_argument("--role", choices=WORKER_ROLES, required=True)
    _ = can_follow.add_argument("--lens", choices=agents_log.LENSES)
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
