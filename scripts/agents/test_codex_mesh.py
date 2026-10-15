"""Tests for codex_mesh's recovery from an app-server that has wedged.

The failure these cover is silent by construction: a stuck app-server replays a
provider message it cached earlier to every thread that attaches, so a local
fault arrives wearing the exact words of a usage limit. What the code can check
is not the message but the circumstances -- who started the server, how fast it
failed, and whether a thread already exists.

ReplyDeliveryTests covers the other silent failure: a delegate's own summary
file replaced by its last chat reply.
"""

from __future__ import annotations

from scripts.whoami.note_file import note_name

import argparse
import base64
import contextlib
import dataclasses
import fcntl
import io
import json
import os
import select
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
from typing import Callable, TextIO, cast, final, override
from unittest.mock import MagicMock, patch

from scripts.agents import codex_mesh
from scripts.usage import agents_log
from scripts.accounts import accounts, moves, refusals
from scripts.whoami import agent_accounts


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


def setUpModule() -> None:
    # No test sees the real Codex sign-in: with none, no server counts as holding an old one.
    home = tempfile.TemporaryDirectory()
    usage = tempfile.TemporaryDirectory()
    restore_usage = set_environment("USAGE_STATE_DIR", usage.name)
    restore_home = set_environment("HOME", home.name)
    previous = os.environ.get("CODEX_HOME")
    os.environ["CODEX_HOME"] = str(Path(home.name) / ".codex")

    def restore() -> None:
        if previous is None:
            _ = os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = previous
        restore_usage()
        restore_home()
        usage.cleanup()
        home.cleanup()

    unittest.addModuleCleanup(restore)
    retries = patch.object(codex_mesh, "REAP_POLL_SECS", 0.001)
    _ = retries.start()
    unittest.addModuleCleanup(retries.stop)
    grace = patch.object(codex_mesh, "REAP_WAIT_SECS", 0.05)
    _ = grace.start()
    unittest.addModuleCleanup(grace.stop)

USAGE_LIMIT = "You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage"
CAPACITY = "Selected model is at capacity. Please try a different model."
STALE_SIGN_IN = ("Your access token could not be refreshed because you have since logged out or signed in to "
                 "another account. Please sign in again.")
THREAD_ID = "thread-capacity-test"


def _read_exact(sock: socket.socket, count: int) -> bytes:
    received = bytearray()
    while len(received) < count:
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
    mask = _read_exact(sock, 4)
    data = _read_exact(sock, length)
    decoded = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
    return cast("dict[str, object]", json.loads(decoded))


def _send_frame(sock: socket.socket, message: dict[str, object]) -> None:
    data = json.dumps(message).encode()
    if len(data) < 126:
        header = bytes((0x81, len(data)))
    elif len(data) < 65536:
        header = bytes((0x81, 126)) + struct.pack(">H", len(data))
    else:
        header = bytes((0x81, 127)) + struct.pack(">Q", len(data))
    sock.sendall(header + data)


@final
class StubAppServer:
    """A loopback JSON-RPC server with a scripted reply for each request."""

    def __init__(
        self,
        respond: Callable[[dict[str, object]], list[dict[str, object]] | None],
    ) -> None:
        self.respond: Callable[[dict[str, object]], list[dict[str, object]] | None] = respond
        self.requests: list[dict[str, object]] = []
        self.errors: list[Exception] = []
        self.close_after_turn_start: bool = False
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
        self.conversations: list[threading.Thread] = []
        self.thread: threading.Thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.stopping.set()
        self.release_compact_turn_completion.set()
        _ = self.close_writer.send(b"close")
        self.thread.join(timeout=300)
        self.listener.close()
        self.close_reader.close()
        self.close_writer.close()
        for conversation in self.conversations:
            conversation.join(timeout=300)
        if self.thread.is_alive():
            raise AssertionError("stub app-server did not stop")
        if self.errors:
            raise AssertionError(f"stub app-server failed: {self.errors}")

    def _serve(self) -> None:
        while not self.stopping.is_set():
            try:
                self.accept_waiting.set()
                ready, _, _ = select.select([self.listener, self.close_reader], [], [])
                if self.close_reader in ready:
                    return
                connection = self.listener.accept()[0]
            except OSError:
                return
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
        while b"\r\n\r\n" not in handshake:
            handshake.extend(connection.recv(4096))
        connection.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
        while not self.stopping.is_set():
            request = _read_request(connection)
            self.requests.append(request)
            frames = self.respond(request)
            if frames is None:
                return
            for frame in frames:
                params = _as_test_dict(frame.get("params"))
                turn = _as_test_dict(params.get("turn"))
                if (
                    self.block_compact_turn_completion
                    and frame.get("method") == "turn/completed"
                    and turn.get("id") == "compact-turn"
                ):
                    self.compaction_event_sent.set()
                    _ = self.release_compact_turn_completion.wait(timeout=2)
                _send_frame(connection, frame)
            if request.get("method") == "turn/start" and self.close_after_turn_start:
                return


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


class MeshCommandTests(unittest.TestCase):
    """Exercise the launcher and addressable verbs against a local app-server."""

    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
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
    next_thread_id: str  # pyright: ignore[reportUninitializedInstanceVariable]
    fork_thread_id: str  # pyright: ignore[reportUninitializedInstanceVariable]
    history_turns: list[dict[str, object]]  # pyright: ignore[reportUninitializedInstanceVariable]
    failing_method: str  # pyright: ignore[reportUninitializedInstanceVariable]
    auto_turn_starts: int  # pyright: ignore[reportUninitializedInstanceVariable]
    waits: list[float]  # pyright: ignore[reportUninitializedInstanceVariable]
    elapsed: float  # pyright: ignore[reportUninitializedInstanceVariable]
    start_args_timeout: float  # pyright: ignore[reportUninitializedInstanceVariable]
    patches: contextlib.ExitStack  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.session_dir = Path(self.temporary.name)
        _ = (self.session_dir / "prompt.txt").write_text("Original delegate task", encoding="utf-8")
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
        self.next_thread_id = THREAD_ID
        self.fork_thread_id = "thread-review-fork"
        self.history_turns = []
        self.failing_method = ""
        self.auto_turn_starts = 0
        self.waits = []
        self.elapsed = 0.0
        self.start_args_timeout = 300.0
        self.server = StubAppServer(self.respond)
        _ = (self.session_dir / codex_mesh.SERVER_FILE).write_text(
            json.dumps({"port": self.server.port, "pid": os.getpid(), "codex_home": str(agent_accounts.seats_home())}),
            encoding="utf-8",
        )
        self.patches = contextlib.ExitStack()
        _ = self.patches.callback(set_environment("USAGE_STATE_DIR", str(self.session_dir / "usage-state")))
        _ = self.patches.enter_context(patch.object(codex_mesh, "QUEUE_GRACE_SECS", 0.0))
        _ = self.patches.enter_context(patch.object(codex_mesh, "RESIDENT_POLL_SECS", 0.01))
        _ = self.patches.enter_context(patch.object(codex_mesh, "_capacity_clock", self.clock, create=True))
        _ = self.patches.enter_context(patch.object(codex_mesh, "_capacity_sleep", self.wait, create=True))

    @override
    def tearDown(self) -> None:
        self.patches.close()
        self.server.close()
        self.temporary.cleanup()

    def clock(self) -> float:
        return self.elapsed

    def wait(self, seconds: float) -> None:
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
            turns = self.history_turns.copy()
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
            turns: list[dict[str, object]] = []
            if self.live_turn and not self.hide_live_turn_id:
                turns.append({"id": self.live_turn, "status": "inProgress"})
            frames = [_reply(request, {"thread": {
                "id": thread_id,
                "status": {"type": self.thread_statuses.get(thread_id) or (
                    "active" if self.live_turn else
                    "systemError" if thread_id in self.error_threads else "idle"
                )},
                "turns": turns,
            }})]
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
        return cast("dict[str, object]", json.loads(
            (self.session_dir / codex_mesh.ROSTER_FILE).read_text(encoding="utf-8")
        ))

    def worker_record(self) -> dict[str, object]:
        return cast("dict[str, object]", self.roster()["worker"])

    def usage_path(self) -> Path:
        return self.session_dir / "usage-state/agents.jsonl"

    def usage_records(self) -> list[dict[str, object]]:
        if not self.usage_path().exists():
            return []
        return [cast("dict[str, object]", json.loads(line))
                for line in self.usage_path().read_text(encoding="utf-8").splitlines()]

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
        def acquire(descriptor: int, operation: int) -> None:
            path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            if armed[0] and operation == fcntl.LOCK_EX and path.name == codex_mesh.ROSTER_FILE:
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
        self.assertEqual(self.worker_record(), {**original, "turn_id": "live-turn", "revision": 1})

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
        arguments = self.prime_cli("--model", "gpt-test", "--service-tier", "fast")
        instructions = (self.session_dir / "review-instructions.txt").read_text(encoding="utf-8")
        self.write_pool_roster({"other": {"thread_id": "other-thread", "status": "done"}})

        code, output, errors = self.run_mesh(arguments)

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
        self.assertEqual(requests[1]["params"], {"threadId": THREAD_ID, "name": "template"})
        self.assert_review_record("template", THREAD_ID)
        self.assertEqual(self.roster()["other"], {"thread_id": "other-thread", "status": "done"})
        self.assertEqual(_as_test_dict(self.roster()["template"]).get("context_tokens"), 0)
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
        self.assertEqual(requests[2]["params"], {"threadId": self.fork_thread_id, "name": "reviewer"})
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
        }})

    def assert_instruction_resume(self, path: Path) -> None:
        resumes = self.methods("thread/resume")
        self.assertTrue(resumes, "the verb must resume the roster thread")
        for request in resumes:
            self.assertEqual(request["params"], {
                "threadId": THREAD_ID,
                "developerInstructions": path.read_text(encoding="utf-8"),
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
        self.assertEqual(accounts.main(["set-role", "seats", "codex1"]), 0)
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
        self.assertEqual(accounts.main(["set-role", "seats", "codex1"]), 0)
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
        }])
        self.assertEqual(self.worker_record().get("instructions_file"), str(path))

    def test_replacement_worker_keeps_standing_instructions_and_read_only_policy(self) -> None:
        path = self.standing_instructions()
        self.seed_instruction_thread(path)
        _target, new = self.prepare_workers_move()
        self.assertEqual(accounts.main(["set-role", "seats", "codex1"]), 0)
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
        self.assertEqual(accounts.main(["set-role", "seats", "codex1"]), 0)
        respond = self.server.respond
        def held_thread(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/loaded/list":
                return [_reply(request, {"data": [THREAD_ID], "nextCursor": None})]
            if request.get("method") == "thread/read":
                return [_reply(request, {"thread": {"status": {"type": "active"},
                    "turns": [{"id": "outgoing-turn", "status": "inProgress"}]}})]
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
        self.assertEqual(accounts.main(["set-role", "seats", "codex1"]), 0)
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
        self.assertEqual(accounts.main(["set-role", "seats", "codex1"]), 0)
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
                self.assertEqual(accounts.main(["set-role", "seats", "codex1"]), 0)
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
                self.assertTrue(errors.startswith(f"codex_mesh: {name}: "), errors)
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
            "input": [{"type": "text", "text": "Original delegate task"}],
            "model": "gpt-test", "effort": "high",
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

    def test_reset_refuses_threads_that_do_not_read_idle(self) -> None:
        for status in ("active", "systemError", "notLoaded"):
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

        code, _output, errors = self.run_mesh(self.follow_cli(
            "--cwd", str(worktree), "--read-only", "--image", str(images[0]), "--image", str(images[1]),
            "--model", "gpt-test", "--effort", "high",
        ))

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
        })

    def test_follow_cli_without_new_flags_keeps_the_original_turn_request(self) -> None:
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
                    **overrides,
                })

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
                    **overrides,
                })

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
        code, errors = self.follow_with_name_taken("thread/resume")
        self.assertEqual((code, errors), (1, "codex_mesh: worker: name was taken while the follow-up ran; "
                                         + "follow-up did not start\n"))

    def test_follow_name_taken_after_fallback_is_named_releases_it_without_starting_a_turn(self) -> None:
        code, errors = self.follow_with_name_taken("thread/name/set")
        self.assertEqual((code, errors), (1, "codex_mesh: worker: name was taken while the follow-up ran; "
                                         + "follow-up did not start\n"))

    def test_follow_name_taken_reports_unconfirmed_fallback_release_and_keeps_new_owner(self) -> None:
        code, errors = self.follow_with_name_taken("thread/resume", release_unknown=True)
        self.assertEqual(code, 1)
        self.assertEqual(errors, "codex_mesh: worker: name was taken while the follow-up ran; "
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
                          for request in self.methods("thread/name/set")], [THREAD_ID])
        self.assertEqual([_as_test_dict(request["params"]).get("threadId")
                          for request in self.methods("turn/start")], [THREAD_ID])
        self.assertEqual([record["id"] for record in self.usage_records()], ["old-follow-thread", THREAD_ID])
        self.assertIn(f"worker continues as new thread {THREAD_ID}",
                      (self.session_dir / "board.log").read_text(encoding="utf-8"))

    def test_start_records_the_assigned_role(self) -> None:
        code, errors = self.run_start()

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.worker_record()["role"], "impl")

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
            ["Original delegate task", "Continue where you stopped."],
        )
        board = (self.session_dir / "board.log").read_text(encoding="utf-8")
        self.assertEqual(board.count("context window filled; sent"), 1)

    def prepare_refusing_account(self) -> agent_accounts.KnownCodexAccount:
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
        _ = (self.session_dir / codex_mesh.SERVER_FILE).write_text(json.dumps({
            "port": self.server.port, "pid": os.getpid(), "codex_home": str(selected),
        }))
        _ = self.patches.enter_context(patch.object(codex_mesh, "ensure_server", return_value=(self.server.port, True)))
        return agent_accounts.KnownCodexAccount(selected, login, "codex2")

    def quota_completion(self, request: dict[str, object]) -> list[dict[str, object]]:
        params = cast(dict[str, object], request["params"])
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

    def test_refusal_without_a_server_record_retains_an_unknown_account_at_the_selected_home(self) -> None:
        account = self.prepare_refusing_account()
        (self.session_dir / codex_mesh.SERVER_FILE).unlink()
        original = self.respond
        def respond(request: dict[str, object]) -> list[dict[str, object]] | None:
            return self.quota_completion(request) if request.get("method") == "turn/start" else original(request)
        self.server.respond = respond
        with patch.object(codex_mesh, "_report_quota_refusal") as report:
            self.assertEqual(self.run_start()[0], 1)
        report.assert_called_once()
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
                self.assertEqual(accounts.main(["set-role", "seats", "codex1"]), 0)
                _ = (self.session_dir / codex_mesh.SERVER_FILE).write_text(json.dumps({
                    "port": self.server.port, "pid": os.getpid() + 1, "codex_home": str(next_home),
                }))
                return self.quota_completion(request)
            return original(request)
        self.server.respond = respond
        with patch.object(codex_mesh, "_report_quota_refusal") as report:
            self.assertEqual(self.run_start()[0], 1)
        report.assert_called_once()
        self.assertEqual(cast(codex_mesh.ServingCodexServer, cast(object, report.call_args.args[1])).account, account)
        self.assertEqual(cast(codex_mesh.ServingCodexServer, cast(object, report.call_args.args[1])).pid, os.getpid())
        self.assertEqual(agent_accounts.seats_home(), next_home)

    def test_followed_turn_reports_its_refusing_account_once(self) -> None:
        account = self.prepare_refusing_account()
        self.assertEqual(self.run_start()[0], 0)
        original = self.respond
        def respond(request: dict[str, object]) -> list[dict[str, object]] | None:
            return self.quota_completion(request) if request.get("method") == "turn/start" else original(request)
        self.server.respond = respond
        with patch.object(codex_mesh, "_report_quota_refusal") as report:
            self.assertEqual(self.run_follow()[0], 1)
        report.assert_called_once()
        self.assertEqual(cast(codex_mesh.ServingCodexServer, cast(object, report.call_args.args[1])).account, account)

    def test_parked_message_turn_reports_its_refusing_account_once(self) -> None:
        account = self.prepare_refusing_account()
        original = self.respond
        starts = [0]
        def respond(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "turn/start":
                starts[0] += 1
                if starts[0] == 2:
                    _ = (self.session_dir / "worker.end").write_text("end")
                    return self.quota_completion(request)
            return original(request)
        self.server.respond = respond
        next_frame = codex_mesh.Client.next_frame
        parked = [False]
        def park(client: codex_mesh.Client, deadline: float) -> codex_mesh.RpcMessage | None:
            frame = next_frame(client, deadline)
            if frame and frame.get("method") == "turn/completed" and not parked[0]:
                parked[0] = True
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(codex_mesh.command_send(argparse.Namespace(
                        session_dir=str(self.session_dir), to="worker", message="Parked task", message_file="")), 0)
            return frame
        args = self.start_args()
        args.resident = True
        args.timeout = 1
        with patch.object(codex_mesh.Client, "next_frame", park), \
                patch.object(codex_mesh, "_report_quota_refusal") as report, contextlib.redirect_stderr(io.StringIO()):
            _ = codex_mesh.command_start(args)
        self.assertEqual(starts[0], 2)
        report.assert_called_once()
        self.assertEqual(cast(codex_mesh.ServingCodexServer, cast(object, report.call_args.args[1])).account, account)
        self.assertIn("Parked task", str(self.methods("turn/start")[1]))

    def test_submission_refusal_reports_the_serving_account_once(self) -> None:
        account = self.prepare_refusing_account()
        original = self.respond
        def respond(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "turn/start":
                return [{"id": request["id"], "error": {"message": USAGE_LIMIT}}]
            return original(request)
        self.server.respond = respond
        with patch.object(codex_mesh, "_report_quota_refusal") as report:
            self.assertEqual(self.run_start()[0], 1)
        report.assert_called_once()
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
        original = self.respond
        def respond(request: dict[str, object]) -> list[dict[str, object]] | None:
            return self.quota_completion(request) if request.get("method") == "turn/start" else original(request)
        self.server.respond = respond
        with patch.object(codex_mesh, "_report_quota_refusal") as report:
            self.assertEqual(self.run_start()[0], 1)
        report.assert_called_once()
        unknown = cast(codex_mesh.ServingCodexServer, cast(object, report.call_args.args[1])).account
        self.assertIsInstance(unknown, agent_accounts.UnknownCodexAccount)
        self.assertEqual(unknown.home, account.home)

    def test_replacement_conversations_first_refusal_names_the_target_account(self) -> None:
        target, new = self.prepare_workers_move()
        original = self.respond
        def respond(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/resume":
                return [{"id": request["id"], "error": {"message": "thread cannot resume"}}]
            if request.get("method") == "turn/start":
                return self.quota_completion(request)
            return original(request)
        new.respond = respond
        def wait(seconds: float) -> None:
            self.wait(seconds)
            self.assertEqual(accounts.main(["set-role", "seats", "codex1"]), 0)
            self.next_thread_id = "replacement-thread"
        self.outcomes = ["capacity"]
        with patch.object(codex_mesh, "_capacity_sleep", side_effect=wait), \
                patch.object(codex_mesh, "_report_quota_refusal") as report:
            self.assertEqual(self.run_start()[0], 1)
        report.assert_called_once()
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
        self.assertEqual(accounts.main(["set-role", "seats", "codex1"]), 0)
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
        _ = (self.session_dir / codex_mesh.SERVER_FILE).write_text(json.dumps({
            "port": self.server.port, "pid": os.getpid(), "codex_home": str(home / ".codex"),
        }))
        new = StubAppServer(self.respond)
        _ = self.patches.callback(new.close)
        def start(session_dir: str, *_args: object) -> int:
            _ = (Path(session_dir) / codex_mesh.SERVER_FILE).write_text(json.dumps({
                "port": new.port, "pid": os.getpid(), "codex_home": str(agent_accounts.seats_home()),
            }))
            return new.port
        _ = self.patches.enter_context(patch.object(codex_mesh, "_start_server", side_effect=start))
        _ = self.patches.enter_context(patch.object(codex_mesh, "_start_watcher"))
        _ = self.patches.enter_context(patch.object(codex_mesh, "_holds_old_sign_in", return_value=False))
        return target, new

    def test_automatic_continuation_after_a_role_change_starts_once_on_the_new_home(self) -> None:
        target, new = self.prepare_workers_move()
        old_response = self.server.respond
        def respond(request: dict[str, object]) -> list[dict[str, object]] | None:
            frames = old_response(request)
            if request.get("method") == "turn/start":
                self.assertEqual(accounts.main(["set-role", "seats", "codex1"]), 0)
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
        self.assertEqual(agent_accounts.seats_home(), target)

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
                "subject": "seats", "move_id": "refused-turn-move", "from": "codex9",
                "to": "codex1", "why": "quota refusal", "window": "unknown",
            }
            moves.append_move(requested)
            self.assertEqual(accounts.main(["set-role", "seats", "codex1"]), 0)
            if held:
                _ = (self.session_dir / "worker.pending.json").write_text(json.dumps({
                    "thread_id": THREAD_ID, "messages": list(held), "accepting_messages": True,
                }))
        args = self.start_args()
        with patch.object(codex_mesh, "_report_quota_refusal", side_effect=move_on_refusal) as report:
            outcome = codex_mesh._run_delegate(args, self.server.port, started_here=True)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(outcome, codex_mesh.RunCompleted)
        report.assert_called_once()
        starts = [request for request in new.requests if request.get("method") == "turn/start"]
        self.assertEqual(len(self.methods("turn/start")), 1)
        self.assertEqual(len(starts), 1)
        params = cast(dict[str, object], starts[0]["params"])
        inputs = cast(list[dict[str, object]], params["input"])
        self.assertEqual([item["text"] for item in inputs], ["Continue where you stopped.", *held])
        self.assertEqual(agent_accounts.seats_home(), target)
        confirmed = [entry for entry in moves.read_moves() if entry["kind"] == "confirmed"]
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]["move_id"], "refused-turn-move")
        self.assertEqual(confirmed[0]["subject"], "seats")
        assert "seat" in confirmed[0] and "home" in confirmed[0] and "run" in confirmed[0]
        self.assertEqual(confirmed[0]["seat"], "worker")
        self.assertEqual(confirmed[0]["run"], str(self.session_dir))
        self.assertEqual(confirmed[0]["home"], str(target))

    def request_workers_move(self, source: str, target: str, move_id: str) -> None:
        request: moves.RequestedMove = {
            "kind": "requested", "at": datetime.now(timezone.utc).isoformat(), "tool": "codex",
            "subject": "seats", "move_id": move_id, "from": source, "to": target,
            "why": "quota refusal", "window": "unknown",
        }
        moves.append_move(request)

    def assert_destination_refusal_continues(self, *, initial_submission_refused: bool) -> None:
        # Real socket servers, two moves and two one-second retry waits need more than the default deadline.
        self.start_args_timeout = 30.0
        target, rejected = self.prepare_workers_move()
        final_home = target.parent / ".codex2"
        final_home.mkdir()
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
                "port": server.port, "pid": os.getpid(), "codex_home": str(home),
            }))
            return server.port
        def move_on_refusal(_name: str, serving: codex_mesh.CodexServingState, _detail: str) -> None:
            next_label = "codex1" if serving.home != target else "codex2"
            self.request_workers_move(agent_accounts.codex_home_role_label(serving.home), next_label,
                                   "move-to-" + next_label)
            # The role file is the temporary boundary; provider and account checks belong to blocked's tests.
            config = accounts.accounts_config_file()
            config.parent.mkdir(parents=True, exist_ok=True)
            _ = config.write_text(json.dumps({"seats": next_label}))
        with (patch.object(codex_mesh, "_start_server", side_effect=start),
              patch.object(codex_mesh, "_report_quota_refusal", side_effect=move_on_refusal) as report):
            outcome = codex_mesh._run_delegate(self.start_args(), self.server.port, started_here=True)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(outcome, codex_mesh.RunCompleted)
        self.assertEqual(report.call_count, 2)
        final_starts = [request for request in recovered.requests if request.get("method") == "turn/start"]
        self.assertEqual(len(final_starts), 1)
        inputs = cast(list[dict[str, object]], cast(dict[str, object], final_starts[0]["params"])["input"])
        self.assertEqual([item["text"] for item in inputs], ["Continue where you stopped."])
        self.assertFalse(self.worker_record().get("continuation_owed"))
        self.assertEqual(agent_accounts.seats_home(), final_home)

    def test_destination_submission_refusal_continues_once_after_the_following_move(self) -> None:
        self.assert_destination_refusal_continues(initial_submission_refused=False)

    def test_initial_submission_refusal_and_destination_refusal_continue_once_on_final_home(self) -> None:
        self.assert_destination_refusal_continues(initial_submission_refused=True)

    def test_new_worker_on_requested_destination_does_not_confirm_another_workers_move(self) -> None:
        _target, new = self.prepare_workers_move()
        self.request_workers_move("codex9", "codex1", "another-worker-move")
        self.assertEqual(accounts.main(["set-role", "seats", "codex1"]), 0)
        self.assertEqual(self.run_start()[0], 0)
        self.assertTrue(any(request.get("method") == "turn/start" for request in new.requests))
        self.assertEqual([entry for entry in moves.read_moves() if entry["kind"] == "confirmed"], [])

    def test_resumed_worker_confirms_only_a_request_from_its_actual_source_home(self) -> None:
        _target, new = self.prepare_workers_move()
        self.request_workers_move("codex2", "codex1", "different-source-move")
        self.assertEqual(accounts.main(["set-role", "seats", "codex1"]), 0)
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
        self.assertEqual(accounts.main(["set-role", "seats", "codex1"]), 0)
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
            "port": self.server.port, "pid": os.getpid(), "codex_home": str(source),
        }))
        evidence = accounts.roles_history_file()
        evidence.parent.mkdir(parents=True, exist_ok=True)
        _ = evidence.write_text(json.dumps({"at": "2026-10-01T00:00:00Z", "role": "seats",
                                           "label": "default", "account_label": "codex2"}) + "\n")
        journal = moves.store_file()
        # Seed the old spelling directly; new writers take numbered labels only.
        legacy = json.dumps({"kind": "requested", "at": "2026-10-13T12:00:00Z", "tool": "codex",
            "subject": "seats", "move_id": "legacy-default-move", "from": source_label,
            "to": destination_label, "why": "quota refusal", "window": "unknown"}).encode() + b"\n"
        _ = journal.write_bytes(legacy)
        self.assertEqual(accounts.main(["set-role", "seats", "codex2" if default_destination else "codex1"]), 0)
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
        self.assertEqual(accounts.main(["set-role", "seats", "codex1"]), 0)
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
        outcome = codex_mesh._run_delegate(self.start_args(), port)  # pyright: ignore[reportPrivateUsage]
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
                tool="codex", subject="seats", move_id="refused-turn-wait", window="five_hour", reset=reset.isoformat()))
        with patch.object(codex_mesh, "_report_quota_refusal", side_effect=expired_wait) as report:
            code, _errors = self.run_start()
        self.assertEqual(code, 0)
        report.assert_called_once()
        self.assertEqual(len(self.methods("turn/start")), 2)
        self.assertEqual(self.turn_inputs, ["Continue where you stopped."])
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["waited", "wait ended"])
        self.assertEqual({entry["move_id"] for entry in entries}, {"refused-turn-wait"})

    def test_capacity_retry_after_a_role_change_starts_once_on_the_new_home(self) -> None:
        _, new = self.prepare_workers_move()
        def wait(seconds: float) -> None:
            self.wait(seconds)
            self.assertEqual(accounts.main(["set-role", "seats", "codex1"]), 0)
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

    def test_initial_turn_retries_live_state_with_the_configured_interval(self) -> None:
        with contextlib.closing(codex_mesh.Client(self.server.port, "worker")) as client:
            moved = codex_mesh.MovedWorker(client, self.server.port, "next-turn", THREAD_ID, False)
            sleeping = MagicMock()
            with patch.object(codex_mesh, "_move_resident", side_effect=[codex_mesh.WorkerWaitingOnLiveTurn(), moved]) as move, \
                    patch.object(codex_mesh, "time", SimpleNamespace(monotonic=lambda: 100.0,
                        sleep=sleeping)), \
                    patch.object(codex_mesh, "_continuation_is_owed", return_value=False):
                result = codex_mesh._start_workers_turn(  # pyright: ignore[reportPrivateUsage]
                    self.start_args(), str(self.session_dir), "worker", THREAD_ID, client, "next work")
                self.assertIs(result, moved)
                self.assertEqual(move.call_count, 2)
                sleeping.assert_called_once_with(0.01)

    def test_message_for_retired_live_server_is_held_and_steer_stays_on_live_turn(self) -> None:
        _, new = self.prepare_workers_move()
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({"worker": {
            "thread_id": THREAD_ID, "turn_id": "live-turn", "status": "running",
            "launcher_pid": os.getpid(), "port": self.server.port,
        }}))
        self.live_turn = "live-turn"
        self.assertEqual(accounts.main(["set-role", "seats", "codex1"]), 0)
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
            ["Original delegate task", "Continue where you stopped."],
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
                "Original delegate task",
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

    def test_an_idle_thread_with_no_notice_and_no_answer_finishes_as_failed(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        self.outcomes = ["earlier_only"]
        self.start_args_timeout = 5

        with patch.object(codex_mesh, "UNSEEN_END_CHECK_SECS", 0.2):
            code, errors = self.run_follow()

        self.assertNotEqual(code, 0)
        self.assertIn("went idle with no completion notice and no answer", errors)
        self.assertEqual(self.worker_record()["status"], "failed")

    def test_a_thread_with_a_queued_message_or_a_live_turn_has_not_ended(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        client = codex_mesh.Client(self.server.port, "probe")
        try:
            ended = codex_mesh._ended_unseen  # pyright: ignore[reportPrivateUsage]
            self.assertTrue(ended(client, THREAD_ID))
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
        self.assertIn("Original delegate task", str(first["input"]))
        self.assertNotIn("Original delegate task", str(resumed["input"]))
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

        outcome = codex_mesh._run_delegate(args, self.server.port)  # pyright: ignore[reportPrivateUsage]

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

    def test_disconnect_after_turn_start_never_repeats_the_prompt(self) -> None:
        self.outcomes = ["disconnect"]
        self.server.close_after_turn_start = True
        self.start_args_timeout = 0.1
        code, _errors = self.run_start()
        self.assertEqual(code, 1)
        self.assertEqual(len(self.methods("thread/start")), 1)
        starts = self.methods("turn/start")
        self.assertEqual(len(starts), 1)
        self.assertIn("Original delegate task", str(starts[0]["params"]))
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
        self.assertEqual(ensure.call_args.args, (str(self.session_dir), agent_accounts.seats_home()))
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
        outcome = codex_mesh._run_delegate(self.start_args(), port)  # pyright: ignore[reportPrivateUsage]
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

    def test_unreadable_thread_after_completion_ends_and_cleans_up(self) -> None:
        """Failed liveness reads end the launcher through cleanup."""
        self.outcomes = ["completed"]
        original_read = self.respond
        clock_now = [1000.0]

        def clock() -> float:
            return clock_now[0]

        def unreadable_after_completion(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/read" and self.turn_starts:
                self.read_failure = "error"
            return original_read(request)

        original_next_frame = codex_mesh.Client.next_frame

        def no_second_wait(client: codex_mesh.Client, deadline: float) -> codex_mesh.RpcMessage | None:
            if self.methods("thread/read") and self.read_failure == "error":
                clock_now[0] = deadline + 1
            return original_next_frame(client, deadline)

        self.server.respond = unreadable_after_completion
        with patch.object(codex_mesh, "time", SimpleNamespace(time=clock, monotonic=clock, sleep=time.sleep)), \
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
        self.assertEqual(accounts.main(["set-role", "seats", "codex1"]), 0)
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
        self.assertEqual(accounts.main(["set-role", "seats", "codex1"]), 0)
        _ = (self.session_dir / codex_mesh.SERVER_FILE).write_text(json.dumps({
            "pid": 123456, "port": self.server.port, "codex_home": str(target.parent / ".codex"),
        }))
        alive = [True]
        def running(pid: int) -> bool:
            return alive[0] if pid == 123456 else pid == os.getpid()
        def stop(_root: Path | None, pid: int, _port: int, _owner: codex_mesh.ServerOwnership) -> bool:
            self.assertEqual(pid, 123456)
            alive[0] = False
            return True
        with patch.object(codex_mesh, "SWEEP_ROOT", self.session_dir.parent), patch.object(codex_mesh, "_pid_alive", side_effect=running), patch.object(codex_mesh, "_stop_unused", side_effect=stop) as stopped, contextlib.redirect_stdout(io.StringIO()):
            _ = codex_mesh.retire_seats_servers(target, "codex1")
            code, errors = self.run_follow()
        self.assertEqual((code, errors), (0, ""))
        stopped.assert_called_once()
        self.assertTrue(any(request.get("method") == "thread/resume" for request in new.requests))

    def test_compaction_with_unconfirmed_old_writer_posts_one_refusal_and_never_resumes(self) -> None:
        self.context_sizes = [150000]
        self.assertEqual(self.run_start()[0], 0)
        _target, new = self.prepare_workers_move()
        self.assertEqual(accounts.main(["set-role", "seats", "codex1"]), 0)
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
        self.assertEqual(self.turn_inputs, ["Original delegate task", "late peer message"])
        pending = cast(dict[str, object], json.loads((self.session_dir / "worker.pending.json").read_text()))
        self.assertEqual(pending["messages"], [])
        self.assertIs(pending["accepting_messages"], False)

    def test_send_after_launcher_closes_inbox_is_refused_and_stores_nothing(self) -> None:
        sent: list[int] = []
        def after_close(_port: int, _thread_id: str, _log_path: Path) -> codex_mesh.RelaunchAllowed:
            sent.append(codex_mesh.command_send(argparse.Namespace(
                session_dir=str(self.session_dir), to="worker", message="too late", message_file="")))
            return codex_mesh.RelaunchAllowed()
        with patch.object(codex_mesh, "_end_unwatched_turn", side_effect=after_close):
            code, errors = self.run_start()
        self.assertEqual(code, 0)
        self.assertEqual(sent, [2])
        self.assertIn("closing", errors)
        self.assertEqual(len(self.methods("turn/start")), 1)
        self.assertNotIn("too late", (self.session_dir / "worker.pending.json").read_text())

    def test_resident_failed_turn_delivers_one_reply_and_ends_without_repeating_failure(self) -> None:
        self.outcomes = ["failed"]
        args = self.start_args()
        args.resident = True
        deliver = codex_mesh._deliver_reply  # pyright: ignore[reportPrivateUsage]
        def reply(name: str, index: int, answer: str, failure: str, summary: Path, replies: Path | None, log: TextIO) -> None:
            deliver(name, index, answer, failure, summary, replies, log)
            _ = (self.session_dir / "worker.end").write_text("end")
        with patch.object(codex_mesh, "_deliver_reply", side_effect=reply) as sent, contextlib.redirect_stdout(io.StringIO()):
            outcome = codex_mesh._run_delegate(args, self.server.port)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(outcome, codex_mesh.RunCompleted)
        sent.assert_called_once()
        self.assertEqual(self.worker_record()["status"], "done")

    def test_resident_idle_ticks_skip_move_requests_when_home_and_port_match(self) -> None:
        args = self.start_args()
        args.resident = True
        empty = [0]
        read = codex_mesh.Client.next_frame
        def poll(client: codex_mesh.Client, deadline: float) -> codex_mesh.RpcMessage | None:
            frame = read(client, deadline)
            if frame is None:
                empty[0] += 1
                if empty[0] >= 3:
                    _ = (self.session_dir / "worker.end").write_text("end")
            return frame
        with patch.object(codex_mesh.Client, "next_frame", poll), patch.object(codex_mesh, "_move_resident", wraps=codex_mesh._move_resident) as move, contextlib.redirect_stdout(io.StringIO()):  # pyright: ignore[reportPrivateUsage]
            outcome = codex_mesh._run_delegate(args, self.server.port)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(outcome, codex_mesh.RunCompleted)
        self.assertEqual(empty[0], 3)
        self.assertEqual(move.call_count, 2)

    def test_lost_new_server_turn_reply_cleans_up_that_server_and_thread(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        target, new = self.prepare_workers_move()
        self.assertEqual(accounts.main(["set-role", "seats", "codex1"]), 0)
        args = self.start_args()
        _ = (self.session_dir / "worker.pending.json").write_text(json.dumps({"thread_id": THREAD_ID, "messages": ["held message"]}))
        with patch.object(codex_mesh.Client, "call", autospec=True) as call, patch.object(codex_mesh, "_end_unwatched_turn", return_value=codex_mesh.RelaunchAllowed()) as cleanup:
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
        self.assertEqual(agent_accounts.seats_home(), target)
        self.assertEqual(sum(entry.args[1] == "turn/start" for entry in call.call_args_list), 1)

    def test_resident_idle_resume_failure_waits_and_moves_at_a_later_tick(self) -> None:
        target, new = self.prepare_workers_move()
        args = self.start_args()
        args.resident = True
        args.timeout = 10
        resumes: list[int] = []
        call = codex_mesh.Client.call
        read = codex_mesh.Client.next_frame

        def rpc(client: codex_mesh.Client, method: str, params: dict[str, object], timeout: float = codex_mesh.CALL_TIMEOUT_SECS) -> codex_mesh.RpcMessage:
            if client.port == new.port and method == "thread/resume":
                resumes.append(client.port)
                if len(resumes) == 1:
                    raise ConnectionError("resume connection failed before any turn")
            return call(client, method, params, timeout=timeout)

        def poll(client: codex_mesh.Client, deadline: float) -> codex_mesh.RpcMessage | None:
            frame = read(client, deadline)
            if frame is None:
                if not resumes:
                    self.assertEqual(accounts.main(["set-role", "seats", "codex1"]), 0)
                elif client.port == new.port:
                    _ = (self.session_dir / "worker.end").write_text("end")
            return frame

        with patch.object(codex_mesh.Client, "call", rpc), patch.object(codex_mesh.Client, "next_frame", poll), patch.object(codex_mesh, "_stop_unused", return_value=False), contextlib.redirect_stdout(io.StringIO()):
            outcome = codex_mesh._run_delegate(args, self.server.port)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(outcome, codex_mesh.RunCompleted)
        self.assertEqual(resumes, [new.port, new.port])
        self.assertIn("cannot move: resume connection failed before any turn; waiting", (self.session_dir / "worker.log").read_text())
        self.assertEqual(self.turn_starts, 1)
        self.assertEqual(self.worker_record()["thread_id"], THREAD_ID)
        self.assertEqual(agent_accounts.seats_home(), target)

    def prepare_post_turn_home_change(
        self, messages: tuple[str, ...], *, late: bool = False,
    ) -> tuple[StubAppServer, Callable[[], None], Callable[[], None]]:
        """Keep the old writer loaded while selecting a different fake server."""
        target, new = self.prepare_workers_move()
        selected_home = [target.parent / ".codex"]
        loaded = [True]
        changed = [False]
        _ = self.patches.enter_context(patch.object(codex_mesh, "seats_home", side_effect=lambda: selected_home[0]))
        _ = self.patches.enter_context(patch.object(agent_accounts, "seats_home", side_effect=lambda: selected_home[0]))
        _ = self.patches.enter_context(patch.object(codex_mesh, "_stop_unused", return_value=False))
        _ = self.patches.enter_context(patch.object(codex_mesh, "_stop_unused_with_lock_held", return_value=False))
        _ = self.patches.enter_context(patch.object(codex_mesh, "THREAD_RELEASE_TIMEOUT_SECS", 0.03))
        _ = self.patches.enter_context(patch.object(codex_mesh, "UNSEEN_END_CHECK_SECS", 0.02))
        _ = self.patches.enter_context(patch.object(codex_mesh, "RESIDENT_POLL_SECS", 0.02))
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

        def check(client: codex_mesh.Client, thread: str) -> bool:
            change()
            return ended(client, thread)

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
        failed = [False]
        closed_ports: list[int] = []
        write_pending = codex_mesh._write_pending  # pyright: ignore[reportPrivateUsage]
        update_roster = codex_mesh._update_roster  # pyright: ignore[reportPrivateUsage]
        close = codex_mesh.Client.close
        detail = f"local {write_site} write failed after target accepted turn"

        def target_accepted() -> bool:
            return any(request.get("method") == "turn/start" for request in new.requests)

        def pending(handle: TextIO, thread: str, messages: list[str], *, accepting_messages: bool = True) -> None:
            if write_site == "pending" and target_accepted() and not failed[0]:
                self.assertEqual(messages, [])
                failed[0] = True
                raise OSError(detail)
            write_pending(handle, thread, messages, accepting_messages=accepting_messages)

        def roster(session: str, name: str, fields: codex_mesh.ThreadRecord) -> None:
            if (write_site == "roster" and target_accepted() and not failed[0]
                    and fields.get("status") == "running" and fields.get("port") == new.port):
                failed[0] = True
                raise OSError(detail)
            update_roster(session, name, fields)

        def closed(client: codex_mesh.Client) -> None:
            closed_ports.append(client.port)
            close(client)

        with (
            patch.object(codex_mesh, "_write_pending", side_effect=pending),
            patch.object(codex_mesh, "_update_roster", side_effect=roster),
            patch.object(codex_mesh.Client, "close", closed),
            patch.object(codex_mesh, "_close_messages_and_keep_pending") as hold,
            patch.object(codex_mesh, "_post_board_status") as board,
            patch.object(codex_mesh, "_end_unwatched_turn", return_value=codex_mesh.RelaunchAllowed()) as cleanup,
        ):
            code, errors = self.run_start()
        self.assertTrue(failed[0], "the injected failure must follow target turn acceptance")
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
            result = move(args, session, name, thread, client, message=message)
            if isinstance(result, codex_mesh.MovedWorker) and result.port == new.port:
                _ = (self.session_dir / "worker.end").write_text("end")
            return result

        with patch.object(codex_mesh, "_move_resident", side_effect=retry), patch.object(codex_mesh, "_deliver_reply", wraps=delivered) as replies, contextlib.redirect_stdout(io.StringIO()):
            outcome = codex_mesh._run_delegate(args, self.server.port)  # pyright: ignore[reportPrivateUsage]
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
        self.assertEqual(accounts.main(["set-role", "seats", "codex1"]), 0)
        replacement = "replacement-thread"
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
                 patch.object(codex_mesh, "_run_delegate", side_effect=outcomes) as run, \
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

    def test_a_resident_delegate_is_never_retried(self) -> None:
        # Its caller is already holding replies; a silent second attempt would
        # arrive behind them.
        self.assertFalse(
            codex_mesh._retry_warranted(  # pyright: ignore[reportPrivateUsage]
                _attempt(), fresh_server=False, resident=True
            )
        )


class QuotaRefusalTests(unittest.TestCase):
    """Which usage-limit failures are the provider's own, and so reported to the quota alert."""

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
        identity = agent_accounts.KnownCodexAccount(Path.home() / ".codex", "serving@example.invalid", "codex1")
        serving = codex_mesh.ServingCodexServer(4001, 99, identity.home, identity)
        for detail, window in ((USAGE_LIMIT + " Weekly limit reached", "weekly"),
                               (USAGE_LIMIT + " 5-hour limit reached", "five_hour"),
                               (USAGE_LIMIT, "unknown")):
            with self.subTest(window=window), patch.object(subprocess, "run",
                    return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
                codex_mesh._report_quota_refusal("worker", serving, detail)  # pyright: ignore[reportPrivateUsage]
                run.assert_called_once()
                self.assertNotIn("env", run.call_args.kwargs)
                command = cast(list[str], run.call_args.args[0])
                self.assertEqual(command[2:6], ["blocked", str(identity.home), identity.login, identity.label])
                self.assertEqual(command[6:9], ["--window", window, "--refused-at"])
                self.assertIsNotNone(datetime.fromisoformat(command[9]).tzinfo)


    def test_the_report_runs_the_blocked_verb_and_survives_its_failure(self) -> None:
        identity = agent_accounts.KnownCodexAccount(Path.home() / ".codex", "serving@example.invalid", "codex1")
        serving = codex_mesh.ServingCodexServer(4001, 99, identity.home, identity)
        done = subprocess.CompletedProcess([], 0, "switched to claude for codex1: delegate\n", "")
        with patch.object(subprocess, "run", return_value=done) as run, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            codex_mesh._report_quota_refusal("worker", serving)  # pyright: ignore[reportPrivateUsage]
        command = cast("list[str]", run.call_args.args[0])
        self.assertEqual(command[1:6], [str(codex_mesh.QUOTA_REPORT), "blocked", str(identity.home), identity.login, identity.label])
        self.assertTrue(codex_mesh.QUOTA_REPORT.is_file())
        self.assertIn("codex_mesh: worker: quota: switched to claude for codex1: delegate", errors.getvalue())
        with patch.object(subprocess, "run", side_effect=OSError("no interpreter")), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            codex_mesh._report_quota_refusal("worker", serving)  # pyright: ignore[reportPrivateUsage]
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
    """Where a delegate's replies land, and whose file the summary is."""

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

    def test_the_delegates_own_summary_survives_its_last_reply(self) -> None:
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

    def test_a_summary_the_delegate_never_wrote_is_filled_from_the_reply(self) -> None:
        # implement.sh truncates the file at launch, so empty at exit means the
        # delegate skipped its last act and the reply is all there is.
        _ = self.summary.write_text("", encoding="utf-8")
        codex_mesh._finish_summary(  # pyright: ignore[reportPrivateUsage]
            self.summary, self.replies, "worker", 1, "Done."
        )
        self.assertEqual(self.summary.read_text(encoding="utf-8"), "Done.\n")

    def test_resident_replies_accumulate_in_order(self) -> None:
        _ = self.summary.write_text("the delegate's summary\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), io.StringIO() as log:
            for index, text in enumerate(("first", "second"), start=1):
                codex_mesh._deliver_reply(  # pyright: ignore[reportPrivateUsage]
                    "worker", index, text, "", self.summary, self.replies, log
                )
        delivered = self.replies.read_text(encoding="utf-8")
        self.assertLess(delivered.index("first"), delivered.index("second"))
        self.assertEqual(
            self.summary.read_text(encoding="utf-8"), "the delegate's summary\n"
        )


class SweepTests(unittest.TestCase):
    """The report of app-servers nothing is using. Every doubt must read as in use."""

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
            self.assertEqual(folder(5), Path("/tmp/x/run-a"))
        with patch.object(os, "readlink", return_value="/tmp/x/gone/mesh_server.log (deleted)"):
            self.assertEqual(folder(5), Path("/tmp/x/gone"))
        with patch.object(os, "readlink", return_value="/dev/null"):
            self.assertIsNone(folder(5))
        with patch.object(os, "readlink", side_effect=OSError("no such process")):
            self.assertIsNone(folder(5))

    def test_judgment_preserves_the_explicit_unowned_without_log_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(codex_mesh, "_server_folder", return_value=None), \
                patch.object(codex_mesh, "_busy_threads", return_value=()):
            judgment = codex_mesh._judge(  # pyright: ignore[reportPrivateUsage]
                Path(directory), 5, 4005, codex_mesh.SERVER_IDLE_SECS + 60, 0
            )

        self.assertIsInstance(judgment.verdict, codex_mesh.ServerUnused)
        self.assertEqual(judgment.ownership, codex_mesh.UnownedServerWithoutLog())

    def test_a_run_folder_reports_its_marker_its_quiet_time_and_its_live_launchers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run-a"
            (root / "active").mkdir()
            run.mkdir()
            self.assertFalse(codex_mesh._marked_active(root, run))  # pyright: ignore[reportPrivateUsage]
            self.assertIsNone(codex_mesh._quiet_secs(run, 0.0))  # pyright: ignore[reportPrivateUsage]
            _ = (root / "active" / "session-1").write_text(f"{run}\n")
            self.assertTrue(codex_mesh._marked_active(root, run))  # pyright: ignore[reportPrivateUsage]
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

            def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
                if method == "thread/loaded/list":
                    return loaded
                status = statuses[cast("str", params["threadId"])]
                return {"result": {"thread": {"status": {"type": status}, "turns": [{"id": "u", "status": "inProgress"}]}}}

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
                             side_effect=[Path(directory) / "gone", Path(directory) / "gone", None]), \
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
                patch.object(codex_mesh, "_server_folder", return_value=None), \
                patch.object(codex_mesh, "_busy_threads", return_value=()), \
                patch.object(codex_mesh, "_reap", side_effect=reap), \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            code = codex_mesh.command_sweep(argparse.Namespace(root=directory, stop=True))
        self.assertEqual(code, 0)
        self.assertEqual(reaped, [8])
        self.assertIn("8\t4008\tstopped\tno current or retired server record", printed.getvalue())

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
                        patch.object(codex_mesh, "_server_folder", return_value=run), \
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
                folder.parent, 2, 4002, ownership
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
                gone.parent, 2, 4002, gone_owner
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
            with patch.object(codex_mesh, "_server_folder", return_value=run), \
                    patch.object(codex_mesh, "_marked_active", return_value=True) as marked, \
                    patch.object(codex_mesh, "_busy_threads", return_value=()):
                judge = codex_mesh._judge  # pyright: ignore[reportPrivateUsage]
                self.assertIsInstance(
                    judge(None, 2, 4002, 9000.0, 0).verdict,
                    codex_mesh.ServerUnused,
                )
                marked.assert_not_called()
                self.assertIsInstance(
                    judge(root, 2, 4002, 9000.0, 0).verdict,
                    codex_mesh.ServerInUse,
                )

    def test_a_server_the_run_retired_is_read_as_retired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run-a"
            run.mkdir()
            _ = (run / codex_mesh.RETIRED_FILE).write_text(json.dumps({"servers": [{"port": 4002, "pid": 2}]}))
            with patch.object(codex_mesh, "_server_folder", return_value=run), \
                    patch.object(codex_mesh, "_marked_active", return_value=True), \
                    patch.object(codex_mesh, "_busy_threads", return_value=()):
                judge = codex_mesh._judge  # pyright: ignore[reportPrivateUsage]
                self.assertIsInstance(
                    judge(root, 2, 4002, 60.0, 0).verdict,
                    codex_mesh.ServerUnused,
                )
                self.assertIsInstance(
                    judge(root, 3, 4003, 10.0, 0).verdict,
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
            with patch.object(codex_mesh, "_server_folder", return_value=ended), \
                    patch.object(codex_mesh, "_busy_threads", return_value=()):
                judgment = codex_mesh._judge(  # pyright: ignore[reportPrivateUsage]
                    root, 2, 4002, 9000.0, 0
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
                root, 2, 4002, stale
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

    def test_message_without_live_launcher_refuses_without_holding_or_starting_work(self) -> None:
        for launcher in (0, 99999999, None):
            with self.subTest(launcher=launcher), tempfile.TemporaryDirectory() as directory:
                record: dict[str, object] = {"thread_id": "thread-1", "status": "running"}
                if launcher is not None:
                    record["launcher_pid"] = launcher
                roster = Path(directory) / codex_mesh.ROSTER_FILE
                _ = roster.write_text(json.dumps({"worker": record}))
                before = roster.read_bytes()
                arguments = argparse.Namespace(session_dir=directory, to="worker", message="message", message_file="")
                with patch.object(codex_mesh, "ensure_server") as ensure, patch.object(codex_mesh, "Client") as client, contextlib.redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(codex_mesh.command_send(arguments), 2)
                self.assertIn("no live launcher", errors.getvalue())
                self.assertIn("relaunch", errors.getvalue())
                ensure.assert_not_called()
                client.assert_not_called()
                self.assertEqual(roster.read_bytes(), before)
                self.assertNotIn("message", (Path(directory) / "worker.pending.json").read_text())


class SignInChangeTests(unittest.TestCase):
    """Moving runs off an app-server that holds a Codex sign-in from before the last change."""

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
                        "codex_home": str(agent_accounts.seats_home())}))
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
                self.assertIn("when that turn ends", line)
                self.assertIn("app-server", (root / "run-a" / codex_mesh.SERVER_LOG).read_text())
                # The same change seen again moves nothing and tells no one.
                self.assertEqual(self.signin_changed(root, starts, stops=False), ([], []))
        finally:
            for process in (old, new):
                process.kill()
                _ = process.wait(timeout=5)

    def test_an_idle_run_has_its_old_server_stopped_at_once(self) -> None:
        old = subprocess.Popen(["sleep", "30"])
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "run-a").mkdir()
                _ = (root / "run-a" / codex_mesh.SERVER_FILE).write_text(json.dumps({"port": 4001, "pid": old.pid,
                    "codex_home": str(agent_accounts.seats_home())}))
                told, watched = self.signin_changed(root, {old.pid: 1000.0}, stops=True)
                # No run-active marker names it, so there is no one to tell.
                self.assertEqual((told, watched), ([], []))
                self.assertIn("stopped", (root / "run-a" / codex_mesh.SERVER_LOG).read_text())
        finally:
            old.kill()
            _ = old.wait(timeout=5)

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
            args = argparse.Namespace(effort="")
            with patch.object(codex_mesh, "ensure_server", return_value=(4002, True)), \
                    patch.object(codex_mesh, "Client", FakeClient):
                moved = codex_mesh._move_resident(  # pyright: ignore[reportPrivateUsage]
                    args, directory, "worker", "thread-1", cast("codex_mesh.Client", cast("object", old)))
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
            def call(self, _method: str, _params: dict[str, object]) -> dict[str, object]:
                return {"result": {"thread": {"status": {"type": "active"}, "turns": [{"id": "t", "status": "inProgress"}]}}}

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
        class HeldClient:
            port: int = 4001
            def __init__(self) -> None:
                home = Path("/fixture/codex")
                self.serving: codex_mesh.ServingCodexServer = codex_mesh.ServingCodexServer(
                    self.port, 0, home, agent_accounts.UnknownCodexAccount(home, "fixture has no sign-in"))
                self.name: str = "worker"
                self.turn_submitted_at: float = 0.0
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
            def next_frame(self, _deadline: float) -> codex_mesh.RpcMessage | None:
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

    def select(self, label: str = "codex1") -> Path:
        home = self.home / ("." + note_name(label).removesuffix(".md"))
        home.mkdir(exist_ok=True)
        login = f"{label.replace(' ', '-')}@example.invalid"
        claims = base64.urlsafe_b64encode(json.dumps({"email": login}).encode()).decode().rstrip("=")
        _ = (home / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"header.{claims}.signature"}}))
        _ = (self.home / note_name(label)).write_text(f"---\nlogin: {login}\n---\n")
        self.assertEqual(accounts.main(["set-role", "seats", label]), 0)
        return home

    def record(self, home: Path, port: int = 4001) -> None:
        _ = (self.session_dir / codex_mesh.SERVER_FILE).write_text(json.dumps({"port": port, "pid": os.getpid(), "codex_home": str(home)}))

    def writer_process_is_live(self, pid: int) -> bool:
        return pid == os.getpid()

    def test_mesh_server_child_and_record_follow_role_with_conflicting_caller_home(self) -> None:
        home = self.select()
        process = MagicMock(spec=subprocess.Popen)
        process.pid = 99999999
        poll = cast(MagicMock, process.poll)
        poll.return_value = None
        with patch.dict(os.environ, {"CODEX_HOME": str(self.home / "caller")}), patch.object(subprocess, "Popen", return_value=process) as launch, patch.object(codex_mesh, "_free_port", return_value=4001), patch.object(codex_mesh, "WebSocket"), patch.object(codex_mesh, "_start_watcher"):
            port = codex_mesh._start_server(str(self.session_dir), agent_accounts.seats_home())  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(port, 4001)
        command = cast(list[str], launch.call_args.args[0])
        self.assertIn("thread_unload_delay_secs=0", command)
        environment = cast(dict[str, str], launch.call_args.kwargs["env"])
        self.assertEqual(environment["CODEX_HOME"], str(home))
        record = cast(dict[str, object], json.loads((self.session_dir / codex_mesh.SERVER_FILE).read_text()))
        self.assertEqual(record["codex_home"], str(home))

    def test_home_change_retires_server_even_when_sign_in_timestamps_are_unchanged(self) -> None:
        home = self.select()
        self.record(self.home / ".codex")
        with patch.object(codex_mesh, "_sign_in_changed_at", return_value=1000), patch.object(codex_mesh, "_server_started_at", return_value=2000), patch.object(codex_mesh, "_start_server", return_value=4002) as start, patch.object(codex_mesh, "_start_watcher") as watch:
            self.assertEqual(codex_mesh.ensure_server(str(self.session_dir)), (4002, True))
        self.assertFalse((self.session_dir / codex_mesh.SERVER_FILE).exists())
        start.assert_called_once()
        watch.assert_called_once()
        self.assertEqual(agent_accounts.seats_home(), home)

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
        _ = record.write_text(json.dumps({"port": 4001, "pid": os.getpid()}))
        before = record.read_bytes()
        with patch.dict(os.environ, {"CODEX_HOME": str(self.home / "caller")}), patch.object(codex_mesh, "_holds_old_sign_in", return_value=False) as identity, patch.object(codex_mesh, "_start_server") as start, patch.object(codex_mesh, "_stop_unused_with_lock_held") as stop:
            self.assertEqual(codex_mesh.ensure_server(str(self.session_dir)), (4001, False))
        identity.assert_called_once_with(os.getpid(), self.home / ".codex")
        start.assert_not_called()
        stop.assert_not_called()
        self.assertEqual(record.read_bytes(), before)
        self.assertFalse((self.session_dir / codex_mesh.RETIRED_FILE).exists())

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
        home = self.select()
        _ = (self.session_dir / codex_mesh.SERVER_FILE).write_text(json.dumps({"port": 4001, "pid": os.getpid()}))
        with patch.object(codex_mesh, "SWEEP_ROOT", self.session_dir.parent), patch.object(codex_mesh, "_stop_unused", return_value=True):
            lines = codex_mesh.retire_seats_servers(home, "codex1")
        self.assertIn("move from ~/.codex at their next turn", "\n".join(lines))
        self.assertIn("codex1 (~/.codex1)", "\n".join(lines))

    def test_unsubscribe_not_loaded_proves_release_without_another_state_read(self) -> None:
        class UnloadedClient:
            port: int = 4001
            def call(self, method: str, _params: dict[str, object], **_options: object) -> codex_mesh.RpcMessage:
                if method != "thread/unsubscribe":
                    raise AssertionError("notLoaded already confirms release")
                return {"result": {"status": "notLoaded"}}
        result = codex_mesh._release_worker_thread(  # pyright: ignore[reportPrivateUsage]
            cast(codex_mesh.Client, cast(object, UnloadedClient())), "thread-1", str(self.session_dir))
        self.assertNotIsInstance(result, codex_mesh.WorkerThreadStateUnknown)

    def test_unsubscribe_not_subscribed_does_not_prove_a_loaded_thread_released(self) -> None:
        calls: list[str] = []
        class StillLoadedClient:
            port: int = 4001
            def call(self, method: str, _params: dict[str, object], **_options: object) -> codex_mesh.RpcMessage:
                calls.append(method)
                if method == "thread/unsubscribe":
                    return {"result": {"status": "notSubscribed"}}
                return {"result": {"data": ["thread-1"]}}
            def next_frame(self, _deadline: float) -> codex_mesh.RpcMessage | None:
                return None
        with patch.object(codex_mesh, "THREAD_RELEASE_TIMEOUT_SECS", 0.01):
            result = codex_mesh._release_worker_thread(  # pyright: ignore[reportPrivateUsage]
                cast(codex_mesh.Client, cast(object, StillLoadedClient())), "thread-1", str(self.session_dir))
        self.assertIsInstance(result, codex_mesh.WorkerThreadStateUnknown)
        self.assertIn("thread/loaded/list", calls)
        self.assertNotIn("thread/resume", calls)

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
        _ = (other_run / codex_mesh.SERVER_FILE).write_text(json.dumps({"port": 4002, "pid": os.getpid(), "codex_home": str(self.home / ".codex")}))
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
        _ = (other_run / codex_mesh.SERVER_FILE).write_text(json.dumps({"port": 4002, "pid": os.getpid(), "codex_home": str(self.home / ".codex")}))
        _ = (root / "active" / "director-one").write_text(str(self.session_dir) + "\n")
        _ = (root / "active" / "director-two").write_text(str(other_run) + "\n")
        with patch.object(codex_mesh, "SWEEP_ROOT", root), patch.object(codex_mesh, "_stop_unused", return_value=False), patch.object(codex_mesh, "_start_watcher"), patch.object(codex_mesh, "_notify_holder") as notify:
            _ = codex_mesh.retire_seats_servers(home, "codex1")
        self.assertFalse((self.session_dir / codex_mesh.SERVER_FILE).exists())
        self.assertFalse((other_run / codex_mesh.SERVER_FILE).exists())
        self.assertCountEqual([entry.args[0] for entry in notify.call_args_list], ["director-one", "director-two"])
        for entry in notify.call_args_list:
            self.assertIn("codex1", str(cast(object, entry.args[1])))
            self.assertIn("~/.codex1", str(cast(object, entry.args[1])))

    def test_swap_stops_unused_old_server_immediately_without_starting_a_watcher(self) -> None:
        home = self.select()
        self.record(self.home / ".codex")
        with patch.object(codex_mesh, "SWEEP_ROOT", self.session_dir.parent), patch.object(codex_mesh, "_stop_unused", return_value=True) as stop, patch.object(codex_mesh, "_start_watcher") as watch, patch.object(codex_mesh, "_notify_holder"):
            _ = codex_mesh.retire_seats_servers(home, "codex1")
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
            def call(self, method: str, _params: dict[str, object], **_options: object) -> codex_mesh.RpcMessage:
                if method == "thread/loaded/list":
                    return {"result": {"data": ["thread-1"]}}
                if method == "thread/read":
                    return {"result": {"thread": {"status": {"type": "active"}, "turns": [{"id": "turn-1", "status": "inProgress"}]}}}
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
            def next_frame(self, _deadline: float) -> codex_mesh.RpcMessage | None:
                elapsed[0] += 1.0
                return None
            def close(self) -> None:
                pass
        _ = (self.session_dir / codex_mesh.RETIRED_FILE).write_text(json.dumps({"servers": [
            {"port": 4001, "pid": 123456}, {"port": 4001, "pid": os.getpid()},
        ]}))
        with patch.object(codex_mesh, "_pid_alive", side_effect=self.writer_process_is_live), patch.object(codex_mesh, "_stop_unused", return_value=False), patch.object(codex_mesh, "Client", return_value=HeldWriter()), patch.object(codex_mesh, "THREAD_RELEASE_TIMEOUT_SECS", 0.01), patch.object(time, "monotonic", side_effect=lambda: elapsed[0]):
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
            def call(self, method: str, params: dict[str, object], **_options: object) -> codex_mesh.RpcMessage:
                if method == "thread/loaded/list":
                    return {"result": {"data": ["idle-thread", "busy-thread"]}}
                if method == "thread/read":
                    return {"result": {"thread": {"status": {"type": "active" if params["threadId"] == "busy-thread" else "idle"}}}}
                if method == "thread/queue/list":
                    return {"result": {"data": []}}
                return {"result": {}}
            def next_frame(self, _deadline: float) -> codex_mesh.RpcMessage | None:
                elapsed[0] += 1.0
                return None
            def close(self) -> None:
                pass
        with patch.object(codex_mesh, "_run_servers", return_value=[4001]), patch.object(codex_mesh, "Client", return_value=HeldClient()), patch.object(codex_mesh, "THREAD_RELEASE_TIMEOUT_SECS", 0.01), patch.object(time, "monotonic", side_effect=lambda: elapsed[0]):
            result = codex_mesh._release_other_worker_writers(str(self.session_dir), "worker", "idle-thread", 4002)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(result, codex_mesh.WorkerThreadStateUnknown)
        assert isinstance(result, codex_mesh.WorkerThreadStateUnknown)
        self.assertIn(self.session_dir.name, result.reason)
        self.assertIn("peer", result.reason)
        self.assertIn("once that turn ends", result.reason)

    def test_both_notices_use_the_shared_user_zone(self) -> None:
        home = self.select()
        root = self.session_dir.parent
        for zone in (ZoneInfo("Asia/Tokyo"), None):
            with self.subTest(zone=zone):
                self.record(home)
                with patch.dict(os.environ, {"CODEX_HOME": str(home)}), patch.object(codex_mesh, "user_zone", return_value=zone), patch.object(codex_mesh, "_sign_in_changed_at", return_value=2000), patch.object(codex_mesh, "_server_started_at", return_value=1000), patch.object(codex_mesh, "_stop_unused", return_value=True), contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(codex_mesh.command_signin_changed(argparse.Namespace(root=str(root))), 0)
                self.assertIn(datetime.fromtimestamp(2000).astimezone(zone).strftime("%H:%M %Z"), output.getvalue())
                self.record(self.home / ".codex")
                with patch.object(codex_mesh, "SWEEP_ROOT", root), patch.object(codex_mesh, "user_zone", return_value=zone), patch.object(codex_mesh, "_stop_unused", return_value=True):
                    lines = codex_mesh.retire_seats_servers(home, "codex1")
                self.assertIn(datetime.now().astimezone(zone).strftime("%H:%M %Z"), "\n".join(lines))

    def test_swap_dry_run_lists_runs_without_retiring_or_notifying(self) -> None:
        home = self.select()
        self.record(self.home / ".codex")
        before = (self.session_dir / codex_mesh.SERVER_FILE).read_bytes()
        with patch.object(codex_mesh, "SWEEP_ROOT", self.session_dir.parent), patch.object(codex_mesh, "_start_watcher") as watch, patch.object(codex_mesh, "_notify_holder") as notify:
            lines = codex_mesh.retire_seats_servers(home, "codex1", dry_run=True)
        self.assertEqual((self.session_dir / codex_mesh.SERVER_FILE).read_bytes(), before)
        self.assertFalse((self.session_dir / codex_mesh.RETIRED_FILE).exists())
        self.assertTrue(lines)
        self.assertIn("codex1", "\n".join(lines))
        self.assertIn("~/.codex1", "\n".join(lines))
        watch.assert_not_called()
        notify.assert_not_called()


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

        def wait(seconds: float) -> None:
            waits.append(seconds)
            elapsed[0] += 1

        for grace in (5.0, 2.0):
            with self.subTest(grace=grace):
                elapsed[0] = 0.0
                waits.clear()
                with patch.object(codex_mesh, "REAP_WAIT_SECS", grace), \
                        patch.object(codex_mesh, "time", SimpleNamespace(time=lambda: elapsed[0], sleep=wait)), \
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
            json.dumps({"port": port, "pid": pid, "codex_home": str(agent_accounts.seats_home())}), encoding="utf-8"
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
                _ = codex_mesh._start_server(str(self.session_dir), agent_accounts.seats_home())  # pyright: ignore[reportPrivateUsage]

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
                _ = codex_mesh._start_server(str(self.session_dir), agent_accounts.seats_home())  # pyright: ignore[reportPrivateUsage]

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
        self.spawned.append(process)
        return process, calls

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


if __name__ == "__main__":
    _ = unittest.main()
