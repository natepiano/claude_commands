#!/usr/bin/env python3
"""The Codex daemon reader speaks WebSocket JSON-RPC over a Unix socket."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import tempfile
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import cast, final, override
from unittest import mock

import codex_daemon


RpcMessage = dict[str, object]


def _receive_until(connection: socket.socket, delimiter: bytes) -> bytes:
    received = b""
    while delimiter not in received:
        part = connection.recv(65_536)
        if not part:
            raise EOFError("connection closed")
        received += part
    return received


def _receive_frame(connection: socket.socket) -> RpcMessage:
    header = _receive_exactly(connection, 2)
    masked = bool(header[1] & 0x80)
    length = header[1] & 0x7f
    if length == 126:
        length = struct.unpack(">H", _receive_exactly(connection, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _receive_exactly(connection, 8))[0]
    mask = _receive_exactly(connection, 4) if masked else b""
    payload = _receive_exactly(connection, length)
    if masked:
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    parsed = cast(object, json.loads(payload))
    if not isinstance(parsed, dict):
        raise AssertionError(f"expected an object frame, got {parsed!r}")
    return cast(RpcMessage, parsed)


def _receive_exactly(connection: socket.socket, length: int) -> bytes:
    received = b""
    while len(received) < length:
        part = connection.recv(length - len(received))
        if not part:
            raise EOFError("connection closed")
        received += part
    return received


def _send_frame(connection: socket.socket, message: RpcMessage) -> None:
    payload = json.dumps(message).encode()
    if len(payload) < 126:
        header = bytes((0x81, len(payload)))
    elif len(payload) < 65_536:
        header = bytes((0x81, 126)) + struct.pack(">H", len(payload))
    else:
        header = bytes((0x81, 127)) + struct.pack(">Q", len(payload))
    connection.sendall(header + payload)


def _reply(connection: socket.socket, request: RpcMessage, result: RpcMessage) -> None:
    _send_frame(connection, {"id": request["id"], "result": result})


@final
class FakeDaemon:
    def __init__(self, path: Path, conversation: Callable[[socket.socket], None]) -> None:
        self.path = path
        self.conversation = conversation
        self.ready = threading.Event()
        self.errors: list[BaseException] = []
        self.server = socket.socket(socket.AF_UNIX)
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> FakeDaemon:
        self.thread.start()
        if not self.ready.wait(2):
            raise AssertionError("fake daemon did not start")
        return self

    def __exit__(self, *_details: object) -> None:
        self.thread.join(2)
        self.server.close()
        if self.thread.is_alive():
            raise AssertionError("fake daemon did not finish")
        if self.errors:
            raise self.errors[0]

    def _serve(self) -> None:
        try:
            self.server.bind(str(self.path))
            self.server.listen(1)
            self.ready.set()
            connection = self.server.accept()[0]
            with connection:
                request = _receive_until(connection, b"\r\n\r\n")
                key_line = next(
                    line for line in request.decode().splitlines()
                    if line.lower().startswith("sec-websocket-key:")
                )
                key = key_line.split(":", 1)[1].strip()
                accepted = base64.b64encode(hashlib.sha1(
                    (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
                ).digest()).decode()
                connection.sendall(
                    b"HTTP/1.1 101 Switching Protocols\r\n"
                    + b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                    + f"Sec-WebSocket-Accept: {accepted}\r\n\r\n".encode()
                )
                self.conversation(connection)
        except BaseException as error:
            self.errors.append(error)
            self.ready.set()


class CodexDaemonTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_loaded_sessions_initializes_pages_and_reads_each_thread(self) -> None:
        methods: list[str] = []

        def conversation(connection: socket.socket) -> None:
            initialized = _receive_frame(connection)
            methods.append(cast(str, initialized["method"]))
            self.assertEqual(initialized["params"], {
                "clientInfo": {"name": "roster", "version": "1"},
            })
            _reply(connection, initialized, {})

            notification = _receive_frame(connection)
            methods.append(cast(str, notification["method"]))
            self.assertNotIn("id", notification)

            first_page = _receive_frame(connection)
            methods.append(cast(str, first_page["method"]))
            _reply(connection, first_page, {
                "data": ["thread-one"], "nextCursor": "more",
            })
            second_page = _receive_frame(connection)
            methods.append(cast(str, second_page["method"]))
            self.assertEqual(second_page["params"], {"cursor": "more"})
            _reply(connection, second_page, {
                "data": ["thread-two"], "nextCursor": None,
            })

            for thread_id, name, cwd, status in (
                ("thread-one", "First window", "/work/one", "active"),
                ("thread-two", "Second window", "/work/two", "idle"),
            ):
                request = _receive_frame(connection)
                methods.append(cast(str, request["method"]))
                params = cast(dict[str, object], request["params"])
                self.assertEqual(params["threadId"], thread_id)
                _reply(connection, request, {"thread": {
                    "id": thread_id,
                    "name": name,
                    "cwd": cwd,
                    "status": {"type": status},
                    "turns": [],
                }})

        socket_path = self.root / "app-server.sock"
        with FakeDaemon(socket_path, conversation):
            found = codex_daemon.loaded_sessions(socket_path)

        self.assertEqual(found, [
            codex_daemon.DaemonSession("thread-one", "First window", "/work/one", "active"),
            codex_daemon.DaemonSession("thread-two", "Second window", "/work/two", "idle"),
        ])
        self.assertEqual(methods, [
            "initialize", "initialized", "thread/loaded/list", "thread/loaded/list",
            "thread/read", "thread/read",
        ])

    def test_missing_socket_has_no_sessions(self) -> None:
        self.assertEqual(codex_daemon.loaded_sessions(self.root / "missing.sock"), [])

    def test_connection_closed_mid_call_is_unreadable(self) -> None:
        def conversation(connection: socket.socket) -> None:
            initialize = _receive_frame(connection)
            _reply(connection, initialize, {})
            self.assertEqual(_receive_frame(connection)["method"], "initialized")
            self.assertEqual(_receive_frame(connection)["method"], "thread/loaded/list")
            connection.shutdown(socket.SHUT_RDWR)

        socket_path = self.root / "closing.sock"
        with FakeDaemon(socket_path, conversation):
            found = codex_daemon.loaded_sessions(socket_path)

        self.assertIsInstance(found, codex_daemon.DaemonUnreadable)
        assert isinstance(found, codex_daemon.DaemonUnreadable)
        self.assertTrue(found.reason)

    def test_unrelated_notifications_cannot_extend_the_shared_deadline(self) -> None:
        def conversation(connection: socket.socket) -> None:
            initialize = _receive_frame(connection)
            _reply(connection, initialize, {})
            self.assertEqual(_receive_frame(connection)["method"], "initialized")
            self.assertEqual(_receive_frame(connection)["method"], "thread/loaded/list")
            try:
                while True:
                    _send_frame(connection, {
                        "jsonrpc": "2.0",
                        "method": "daemon/notification",
                    })
            except OSError:
                pass

        socket_path = self.root / "notifications.sock"
        with FakeDaemon(socket_path, conversation):
            with mock.patch.object(codex_daemon, "TIMEOUT_SECONDS", 0.05):
                found = codex_daemon.loaded_sessions(socket_path)

        self.assertEqual(
            found,
            codex_daemon.DaemonUnreadable("timed out after 0.05 s"),
        )

    def test_repeated_nonconsecutive_cursor_makes_pagination_unreadable(self) -> None:
        requested_cursors: list[object] = []

        def conversation(connection: socket.socket) -> None:
            initialize = _receive_frame(connection)
            _reply(connection, initialize, {})
            self.assertEqual(_receive_frame(connection)["method"], "initialized")
            for next_cursor in ("A", "B", "A"):
                request = _receive_frame(connection)
                params = cast(dict[str, object], request["params"])
                requested_cursors.append(params.get("cursor", ""))
                _reply(connection, request, {"data": [], "nextCursor": next_cursor})

        socket_path = self.root / "alternating-cursors.sock"
        with FakeDaemon(socket_path, conversation):
            with mock.patch.object(codex_daemon, "TIMEOUT_SECONDS", 0.1):
                found = codex_daemon.loaded_sessions(socket_path)

        self.assertEqual(requested_cursors, ["", "A", "B"])
        self.assertEqual(
            found,
            codex_daemon.DaemonUnreadable(
                "thread/loaded/list: nextCursor repeated"
            ),
        )

    def test_daemon_socket_honors_the_environment_override(self) -> None:
        chosen = self.root / "chosen.sock"
        self.assertEqual(
            codex_daemon.daemon_socket({"ROSTER_CODEX_SOCKET": str(chosen)}), chosen,
        )


if __name__ == "__main__":
    _ = unittest.main()
