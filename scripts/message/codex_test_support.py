"""One fake Codex control daemon and account fixture for messaging tests."""
from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, cast, final

import codex_daemon
if TYPE_CHECKING:
    import roster

# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

RpcMessage = dict[str, object]


def receive_until(connection: socket.socket, delimiter: bytes) -> bytes:
    received = b""
    while delimiter not in received:
        part = connection.recv(65_536)
        if not part:
            raise EOFError("connection closed")
        received += part
    return received


def receive_frame(connection: socket.socket) -> RpcMessage:
    header = receive_exactly(connection, 2)
    masked = bool(header[1] & 0x80)
    length = header[1] & 0x7f
    if length == 126:
        length = struct.unpack(">H", receive_exactly(connection, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", receive_exactly(connection, 8))[0]
    mask = receive_exactly(connection, 4) if masked else b""
    payload = receive_exactly(connection, length)
    if masked:
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    parsed = cast(object, json.loads(payload))
    if not isinstance(parsed, dict):
        raise AssertionError(f"expected an object frame, got {parsed!r}")
    return cast(RpcMessage, parsed)


def receive_exactly(connection: socket.socket, length: int) -> bytes:
    received = b""
    while len(received) < length:
        part = connection.recv(length - len(received))
        if not part:
            raise EOFError("connection closed")
        received += part
    return received


def send_frame(connection: socket.socket, message: RpcMessage) -> None:
    payload = json.dumps(message).encode()
    if len(payload) < 126:
        header = bytes((0x81, len(payload)))
    elif len(payload) < 65_536:
        header = bytes((0x81, 126)) + struct.pack(">H", len(payload))
    else:
        header = bytes((0x81, 127)) + struct.pack(">Q", len(payload))
    connection.sendall(header + payload)


def reply(connection: socket.socket, request: RpcMessage, result: RpcMessage) -> None:
    send_frame(connection, {"id": request["id"], "result": result})


@final
class FakeDaemon:
    def __init__(
        self, path: Path, conversation: Callable[[socket.socket], None], *,
        repeat: bool = False, expect_contact: bool = True,
    ) -> None:
        self.path = path
        self.conversation = conversation
        self.ready = threading.Event()
        self.stopping = threading.Event()
        self.repeat = repeat
        self.expect_contact = expect_contact
        self.completed_conversations = 0
        self.errors: list[BaseException] = []
        self.server = socket.socket(socket.AF_UNIX)
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> FakeDaemon:
        self.thread.start()
        if not self.ready.wait(TEST_HANG_GUARD_SECS):
            raise AssertionError("fake daemon did not start")
        return self

    def __exit__(self, *details: object) -> None:
        self.stopping.set()
        self.thread.join(TEST_HANG_GUARD_SECS)
        self.server.close()
        if self.thread.is_alive():
            raise AssertionError("fake daemon did not finish")
        if self.errors:
            raise self.errors[0]
        if details[0] is None:
            if self.expect_contact and not self.completed_conversations:
                raise AssertionError("fake daemon conversation did not run to completion")
            if not self.expect_contact and self.completed_conversations:
                raise AssertionError("fake daemon was contacted despite expecting no contact")

    def _serve(self) -> None:
        try:
            self.server.bind(str(self.path))
            self.server.listen(1)
            self.server.settimeout(0.1)
            self.ready.set()
            while not self.stopping.is_set():
                try:
                    connection = self.server.accept()[0]
                except TimeoutError:
                    continue
                with connection:
                    self._conversation(connection)
                if not self.repeat:
                    return
        except BaseException as error:
            self.errors.append(error)
            self.ready.set()

    def _conversation(self, connection: socket.socket) -> None:
        request = receive_until(connection, b"\r\n\r\n")
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
        self.completed_conversations += 1


def write_codex_account(home: Path, notes: Path, label: str) -> None:
    """Give a temporary home an account identity readable by the real account map."""
    home.mkdir(parents=True, exist_ok=True)
    notes.mkdir(parents=True, exist_ok=True)
    login = f"{label}@example.invalid"
    _ = (notes / f"{label}.md").write_text(f"---\nlogin: {login}\n---\n")
    claims = base64.urlsafe_b64encode(json.dumps({"email": login}).encode()).decode().rstrip("=")
    _ = (home / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"header.{claims}.signature"}}))


def loaded_session_daemon(home: Path, names: dict[str, str], *, expect_contact: bool = True) -> FakeDaemon:
    """Serve roster discovery only; any attempt to queue a message fails the test."""
    def conversation(connection: socket.socket) -> None:
        initialize = receive_frame(connection)
        assert initialize["method"] == "initialize", initialize
        reply(connection, initialize, {})
        assert receive_frame(connection)["method"] == "initialized"
        listing = receive_frame(connection)
        assert listing["method"] == "thread/loaded/list", listing
        reply(connection, listing, {"data": list(names)})
        for thread in names:
            request = receive_frame(connection)
            assert request["method"] == "thread/read", request
            assert request["params"] == {"threadId": thread, "includeTurns": False}, request
            reply(connection, request, {"thread": {
                "id": thread, "name": names[thread], "cwd": str(home), "status": {"type": "idle"},
            }})

    return FakeDaemon(home / "app-server-control/app-server-control.sock", conversation,
                      repeat=True, expect_contact=expect_contact)


def daemon_session_row(owner: codex_daemon.CodexDaemon, thread: str, name: str) -> roster.RosterEntry:
    """A loaded session row for tests that isolate send's formatting or transport errors."""
    import roster

    return roster.RosterEntry(
        "machine", "codex", "freestanding", name, "idle", roster.Messageable(f"codex:{thread}"),
        "/work", roster.NoShowrunner(), roster.NoDirector(), roster.NotAWorker(),
        thread=roster.CodexThread(thread), daemon=owner,
    )
