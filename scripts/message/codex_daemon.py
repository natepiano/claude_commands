#!/usr/bin/env python3
"""Read the shared Codex app-server daemon's loaded sessions without changing them."""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast, final

TIMEOUT_SECONDS = 5.0
_clock = time.monotonic


@dataclass(frozen=True)
class DaemonSession:
    thread_id: str
    name: str
    cwd: str
    status: str


@dataclass(frozen=True)
class DaemonUnreadable:
    reason: str


@dataclass(frozen=True)
class _OperationDeadline:
    expires_at: float
    duration_seconds: float

    @classmethod
    def starting_now(cls, duration_seconds: float) -> _OperationDeadline:
        return cls(_clock() + duration_seconds, duration_seconds)

    @property
    def timeout_reason(self) -> str:
        return f"timed out after {self.duration_seconds:g} s"

    def remaining_seconds(self) -> float:
        remaining = self.expires_at - _clock()
        if remaining <= 0:
            raise TimeoutError(self.timeout_reason)
        return remaining


def daemon_socket(environment: Mapping[str, str]) -> Path:
    configured = environment.get("ROSTER_CODEX_SOCKET")
    if configured:
        return Path(configured).expanduser()
    home = Path(environment.get("HOME", str(Path.home()))).expanduser()
    return home / ".codex/app-server-control/app-server-control.sock"


def _as_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return cast("dict[str, object]", cast("object", value))


def _as_string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _loads(payload: bytes) -> object:
    return json.loads(payload.decode("utf-8"))  # pyright: ignore[reportAny]


@final
class _WebSocket:
    """The small RFC 6455 subset used by the app-server control socket."""

    def __init__(self, path: Path, deadline: _OperationDeadline) -> None:
        self._deadline = deadline
        self._socket = socket.socket(socket.AF_UNIX)
        self._prepare_operation()
        self._socket.connect(str(path))
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        self._send_bytes(
            (
                "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode("ascii")
        )
        response = bytearray()
        while not response.endswith(b"\r\n\r\n"):
            if len(response) >= 64 * 1024:
                raise ConnectionError("WebSocket handshake was too large")
            response.extend(self._receive_exact(1))
        first_line = bytes(response).split(b"\r\n", 1)[0]
        if not first_line.startswith(b"HTTP/1.1 101 "):
            detail = first_line.decode("utf-8", errors="replace")
            raise ConnectionError(f"WebSocket upgrade was refused: {detail}")

    def close(self) -> None:
        self._socket.close()

    def _prepare_operation(self) -> None:
        self._socket.settimeout(self._deadline.remaining_seconds())

    def _send_bytes(self, payload: bytes) -> None:
        self._prepare_operation()
        self._socket.sendall(payload)
        _ = self._deadline.remaining_seconds()

    def _receive_exact(self, size: int) -> bytes:
        received = bytearray()
        while len(received) < size:
            self._prepare_operation()
            chunk = self._socket.recv(size - len(received))
            if not chunk:
                raise ConnectionError("daemon closed the connection")
            received.extend(chunk)
        _ = self._deadline.remaining_seconds()
        return bytes(received)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header = bytes((0x80 | opcode, 0x80 | length))
        elif length < 65_536:
            header = bytes((0x80 | opcode, 0x80 | 126)) + struct.pack(">H", length)
        else:
            header = bytes((0x80 | opcode, 0x80 | 127)) + struct.pack(">Q", length)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self._send_bytes(header + mask + masked)

    def send(self, message: Mapping[str, object]) -> None:
        self._send_frame(0x1, json.dumps(message, separators=(",", ":")).encode("utf-8"))

    def receive(self) -> dict[str, object]:
        while True:
            first, second = self._receive_exact(2)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = int(struct.unpack(">H", self._receive_exact(2))[0])
            elif length == 127:
                length = int(struct.unpack(">Q", self._receive_exact(8))[0])
            mask = self._receive_exact(4) if second & 0x80 else b""
            payload = self._receive_exact(length)
            if mask:
                payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            if opcode == 0x8:
                raise ConnectionError("daemon closed the WebSocket")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode != 0x1:
                continue
            parsed = _loads(payload)
            if not isinstance(parsed, dict):
                raise ValueError("daemon sent a non-object JSON-RPC message")
            return cast("dict[str, object]", cast("object", parsed))


@final
class _DaemonClient:
    def __init__(self, path: Path, deadline: _OperationDeadline) -> None:
        self._websocket = _WebSocket(path, deadline)
        self._next_id = 1

    def close(self) -> None:
        self._websocket.close()

    def notify(self, method: str, params: Mapping[str, object] | None = None) -> None:
        message: dict[str, object] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = dict(params)
        self._websocket.send(message)

    def call(self, method: str, params: Mapping[str, object]) -> dict[str, object]:
        request_id = self._next_id
        self._next_id += 1
        self._websocket.send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}
        )
        while True:
            response = self._websocket.receive()
            if response.get("id") != request_id:
                continue
            error = _as_dict(response.get("error"))
            if error:
                reason = _as_string(error.get("message")) or "unknown JSON-RPC error"
                raise ConnectionError(f"{method}: {reason}")
            result = response.get("result")
            if not isinstance(result, dict):
                raise ValueError(f"{method}: response has no result object")
            return cast("dict[str, object]", cast("object", result))


def _loaded_thread_ids(client: _DaemonClient) -> list[str]:
    thread_ids: list[str] = []
    seen_cursors: set[str] = set()
    cursor = ""
    while True:
        params: dict[str, object] = {"cursor": cursor} if cursor else {}
        result = client.call("thread/loaded/list", params)
        data = result.get("data")
        if not isinstance(data, list):
            raise ValueError("thread/loaded/list: result has no data list")
        for value in cast("list[object]", data):
            if not isinstance(value, str) or not value:
                raise ValueError("thread/loaded/list: data contains an invalid thread id")
            if value not in thread_ids:
                thread_ids.append(value)
        next_cursor = result.get("nextCursor")
        if next_cursor is None or next_cursor == "":
            return thread_ids
        if not isinstance(next_cursor, str):
            raise ValueError("thread/loaded/list: nextCursor is invalid")
        if next_cursor in seen_cursors:
            raise ValueError("thread/loaded/list: nextCursor repeated")
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def _read_thread(client: _DaemonClient, thread_id: str) -> DaemonSession:
    result = client.call("thread/read", {"threadId": thread_id, "includeTurns": False})
    thread = _as_dict(result.get("thread"))
    if not thread:
        raise ValueError(f"thread/read: no thread for {thread_id}")
    returned_id = _as_string(thread.get("id"))
    if not returned_id:
        raise ValueError(f"thread/read: thread {thread_id} has no id")
    return DaemonSession(
        thread_id=returned_id,
        name=_as_string(thread.get("name")) or returned_id,
        cwd=_as_string(thread.get("cwd")),
        status=_as_string(_as_dict(thread.get("status")).get("type")) or "unknown",
    )


def loaded_sessions(socket_path: Path) -> list[DaemonSession] | DaemonUnreadable:
    deadline = _OperationDeadline.starting_now(TIMEOUT_SECONDS)
    if not socket_path.exists():
        return []
    client: _DaemonClient | None = None
    try:
        client = _DaemonClient(socket_path, deadline)
        _ = client.call(
            "initialize", {"clientInfo": {"name": "roster", "version": "1"}}
        )
        client.notify("initialized")
        sessions = [
            _read_thread(client, thread_id) for thread_id in _loaded_thread_ids(client)
        ]
        _ = deadline.remaining_seconds()
        return sessions
    except TimeoutError:
        return DaemonUnreadable(deadline.timeout_reason)
    except (ConnectionError, OSError, ValueError, json.JSONDecodeError) as error:
        reason = str(error).splitlines()[0] if str(error).splitlines() else type(error).__name__
        return DaemonUnreadable(reason)
    finally:
        if client is not None:
            client.close()
