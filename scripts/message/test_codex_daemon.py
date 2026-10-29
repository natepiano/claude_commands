#!/usr/bin/env python3
"""The Codex daemon reader speaks WebSocket JSON-RPC over a Unix socket."""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import os
import socket
import struct
import subprocess
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import cast, final, override
from unittest import mock

from scripts.lib.socket_test_support import short_socket_directory
import codex_daemon


# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

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
        if not self.ready.wait(TEST_HANG_GUARD_SECS):
            raise AssertionError("fake daemon did not start")
        return self

    def __exit__(self, *_details: object) -> None:
        self.thread.join(TEST_HANG_GUARD_SECS)
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
        self.root = Path(self.enterContext(short_socket_directory()))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.root)}, clear=True)))

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

    def test_daemon_discovery_follows_process_home_before_default(self) -> None:
        chosen = self.root / "custom home"
        suffix = Path("app-server-control/app-server-control.sock")
        self.assertEqual(codex_daemon.daemon_socket({"HOME": str(self.root)}), self.root / ".codex" / suffix)
        self.assertEqual(codex_daemon.daemon_socket({"HOME": str(self.root), "CODEX_HOME": str(chosen)}), chosen / suffix)
        override = self.root / "override.sock"
        self.assertEqual(codex_daemon.daemon_socket({"HOME": str(self.root), "CODEX_HOME": str(chosen),
            "ROSTER_CODEX_SOCKET": str(override)}), override)

    def test_default_daemon_stays_reachable_when_workers_select_a_numbered_home(self) -> None:
        notes = self.root / "notes"
        notes.mkdir()
        _ = (notes / "codex1.md").write_text("---\nlogin: workers@example.invalid\n---\n")
        home = self.root / ".codex1"
        home.mkdir()
        claims = base64.urlsafe_b64encode(json.dumps({"email": "workers@example.invalid"}).encode()).decode().rstrip("=")
        _ = (home / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"header.{claims}.signature"}}))
        with mock.patch.dict(os.environ, {"AGENT_NOTES_DIR": str(notes)}):
            module = importlib.import_module("scripts.accounts.accounts")
            run_accounts = cast(Callable[[list[str]], int], module.main)
            self.assertEqual(run_accounts(["set-role", "workers", "codex1"]), 0)
        self.assertEqual(codex_daemon.daemon_socket(os.environ),
            self.root / ".codex" / "app-server-control" / "app-server-control.sock")
        completed = subprocess.CompletedProcess(["codex", "queue"], 0, stdout="", stderr="")
        with mock.patch.object(subprocess, "run", return_value=completed) as run:
            self.assertEqual(codex_daemon.queue("default-thread", "message"), codex_daemon.Queued())
        environment = cast(dict[str, str], run.call_args.kwargs.get("env", dict(os.environ)))
        self.assertEqual(Path(environment.get("CODEX_HOME", str(self.root / ".codex"))), self.root / ".codex")

    def test_daemon_queue_keeps_the_home_used_for_discovery(self) -> None:
        home = self.root / "freestanding home"
        completed = subprocess.CompletedProcess(["codex", "queue"], 0, stdout="", stderr="")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}), mock.patch.object(subprocess, "run", return_value=completed) as run:
            self.assertEqual(codex_daemon.daemon_socket(os.environ), home / "app-server-control" / "app-server-control.sock")
            self.assertEqual(codex_daemon.queue("thread", "message"), codex_daemon.Queued())
            environment = cast(dict[str, str], run.call_args.kwargs.get("env", dict(os.environ)))
            self.assertEqual(environment["CODEX_HOME"], str(home))

    def test_explicit_daemon_socket_receives_the_message_at_its_exact_path(self) -> None:
        chosen = self.root / "arbitrary.sock"
        methods: list[str] = []
        def conversation(connection: socket.socket) -> None:
            initialize = _receive_frame(connection)
            methods.append(cast(str, initialize["method"]))
            params = cast(dict[str, object], initialize["params"])
            self.assertEqual(params["capabilities"], {"experimentalApi": True})
            _reply(connection, initialize, {})
            initialized = _receive_frame(connection)
            methods.append(cast(str, initialized["method"]))
            request = _receive_frame(connection)
            methods.append(cast(str, request["method"]))
            params = cast(dict[str, object], request["params"])
            self.assertEqual(params["threadId"], "thread")
            self.assertEqual(params["input"], [{"type": "text", "text": "message"}])
            _reply(connection, request, {"queuedSubmission": {"id": "queued-message"}})
        with FakeDaemon(chosen, conversation), mock.patch.dict(os.environ, {
            "CODEX_HOME": str(self.root / "caller"), "ROSTER_CODEX_SOCKET": str(chosen),
        }), mock.patch.object(subprocess, "run") as run:
            self.assertEqual(codex_daemon.daemon_socket(os.environ), chosen)
            self.assertEqual(codex_daemon.queue("thread", "message", timeout=1), codex_daemon.Queued())
        run.assert_not_called()
        self.assertEqual(methods, ["initialize", "initialized", "thread/queue/add"])

    def test_explicit_daemon_socket_failure_does_not_fall_back_to_another_home(self) -> None:
        chosen = self.root / "refused.sock"
        def conversation(connection: socket.socket) -> None:
            initialize = _receive_frame(connection)
            _reply(connection, initialize, {})
            _ = _receive_frame(connection)
            request = _receive_frame(connection)
            _send_frame(connection, {"id": request["id"], "error": {"message": "thread refused"}})
        with FakeDaemon(chosen, conversation), mock.patch.dict(os.environ, {
            "CODEX_HOME": str(self.root / "caller"), "ROSTER_CODEX_SOCKET": str(chosen),
        }), mock.patch.object(subprocess, "run") as run:
            result = codex_daemon.queue("thread", "message", timeout=1)
        self.assertIsInstance(result, codex_daemon.QueueFailed)
        if not isinstance(result, codex_daemon.QueueFailed):
            self.fail("A selected daemon refusal must be returned to its caller")
        self.assertIn("thread refused", result.reason)
        run.assert_not_called()

    def test_explicit_daemon_socket_timeout_never_falls_back_to_the_process_home(self) -> None:
        for waiting_on in ("initialize", "thread/queue/add"):
            with self.subTest(waiting_on=waiting_on):
                chosen = self.root / f"{waiting_on.replace('/', '-')}.sock"

                def conversation(connection: socket.socket) -> None:
                    initialize = _receive_frame(connection)
                    self.assertEqual(initialize["method"], "initialize")
                    if waiting_on == "thread/queue/add":
                        _reply(connection, initialize, {})
                        self.assertEqual(_receive_frame(connection)["method"], "initialized")
                        self.assertEqual(_receive_frame(connection)["method"], "thread/queue/add")
                    connection.settimeout(2)
                    self.assertEqual(connection.recv(1), b"")

                with FakeDaemon(chosen, conversation), mock.patch.dict(os.environ, {
                    "CODEX_HOME": str(self.root / "caller"), "ROSTER_CODEX_SOCKET": str(chosen),
                }), mock.patch.object(subprocess, "run") as run:
                    result = codex_daemon.queue("thread", "message", timeout=0.25)

                self.assertEqual(result, codex_daemon.QueueTimedOut(0.25))
                run.assert_not_called()

    def test_queue_runs_codex_for_one_thread(self) -> None:
        self.assertEqual(codex_daemon.QUEUE_TIMEOUT_SECONDS, 30)
        completed = subprocess.CompletedProcess(
            ["codex", "queue"], 0, stdout="queued\n", stderr="",
        )
        with mock.patch.object(subprocess, "run", return_value=completed) as run:
            result = codex_daemon.queue("thread-one", "please review", timeout=4.5)

        self.assertEqual(result, codex_daemon.Queued())
        run.assert_called_once_with(
            [
                "codex", "queue", "--thread", "thread-one",
                "--message", "please review",
            ],
            capture_output=True,
            text=True,
            timeout=4.5,
            check=False,
            env={**os.environ, "CODEX_HOME": str(self.root / ".codex")},
        )

    def test_queue_reports_the_first_stderr_line_or_exit_status(self) -> None:
        for stderr, expected in (
            ("thread is closed\nmore detail\n", "thread is closed"),
            ("", "exit 7"),
        ):
            with self.subTest(stderr=stderr):
                completed = subprocess.CompletedProcess(
                    ["codex", "queue"], 7, stdout="", stderr=stderr,
                )
                with mock.patch.object(subprocess, "run", return_value=completed):
                    result = codex_daemon.queue("thread-one", "hello")
                self.assertEqual(result, codex_daemon.QueueFailed(expected))

    def test_queue_timeout_is_a_named_result(self) -> None:
        timeout = subprocess.TimeoutExpired(["codex", "queue"], 0.25)
        with mock.patch.object(subprocess, "run", side_effect=timeout):
            result = codex_daemon.queue("thread-one", "hello", timeout=0.25)

        self.assertEqual(result, codex_daemon.QueueTimedOut(0.25))


if __name__ == "__main__":
    _ = unittest.main()
