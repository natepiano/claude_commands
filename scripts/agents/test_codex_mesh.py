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

import argparse
import contextlib
import dataclasses
import io
import json
import os
import socket
import struct
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, cast, final, override
from unittest.mock import patch

from scripts.agents import codex_mesh

USAGE_LIMIT = "You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage"
CAPACITY = "Selected model is at capacity. Please try a different model."
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
        self.listener: socket.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.listener.settimeout(0.1)
        self.port: int = cast("tuple[str, int]", self.listener.getsockname())[1]
        self.stopping: threading.Event = threading.Event()
        self.conversations: list[threading.Thread] = []
        self.thread: threading.Thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.stopping.set()
        self.listener.close()
        self.thread.join(timeout=2)
        for conversation in self.conversations:
            conversation.join(timeout=2)
        if self.thread.is_alive():
            raise AssertionError("stub app-server did not stop")
        if self.errors:
            raise AssertionError(f"stub app-server failed: {self.errors}")

    def _serve(self) -> None:
        while not self.stopping.is_set():
            try:
                connection = self.listener.accept()[0]
            except TimeoutError:
                continue
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
                _send_frame(connection, frame)
            if request.get("method") == "turn/start" and self.close_after_turn_start:
                return


def _reply(request: dict[str, object], result: dict[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request["id"], "result": result}


def _notice(method: str, params: dict[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "method": method, "params": params}


class MeshCommandTests(unittest.TestCase):
    """Exercise the launcher and addressable verbs against a local app-server."""

    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    session_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    server: StubAppServer  # pyright: ignore[reportUninitializedInstanceVariable]
    outcomes: list[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    turn_starts: int  # pyright: ignore[reportUninitializedInstanceVariable]
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
    next_thread_id: str  # pyright: ignore[reportUninitializedInstanceVariable]
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
        self.next_thread_id = THREAD_ID
        self.auto_turn_starts = 0
        self.waits = []
        self.elapsed = 0.0
        self.start_args_timeout = 5.0
        self.server = StubAppServer(self.respond)
        _ = (self.session_dir / codex_mesh.SERVER_FILE).write_text(
            json.dumps({"port": self.server.port, "pid": os.getpid()}),
            encoding="utf-8",
        )
        self.patches = contextlib.ExitStack()
        _ = self.patches.enter_context(patch.object(codex_mesh, "QUEUE_GRACE_SECS", 0.0))
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
        record = self.seat_record()
        self.assertEqual(record["thread_id"], THREAD_ID)
        self.assertEqual(record["status"], "waiting_capacity")
        self.waits.append(seconds)
        self.elapsed += seconds

    def respond(self, request: dict[str, object]) -> list[dict[str, object]] | None:
        method = request.get("method")
        params = cast("dict[str, object]", request.get("params", {}))
        if method == "thread/start":
            return [_reply(request, {"thread": {"id": self.next_thread_id}})]
        if method == "thread/resume":
            return [_reply(request, {"thread": {"id": params["threadId"]}})]
        if method == "turn/start":
            self.error_threads.discard(cast("str", params.get("threadId", THREAD_ID)))
            self.turn_starts += 1
            self.queued.clear()
            turn_id = f"turn-{self.turn_starts}"
            outcome = self.outcomes.pop(0) if self.outcomes else "completed"
            if outcome in {
                "capacity", "capacity_final_queued", "capacity_queued",
                "capacity_structured", "capacity_completed", "failed",
                "completed_then_capacity",
            }:
                self.error_threads.add(THREAD_ID)
            if outcome == "disconnect":
                return [_reply(request, {"turn": {"id": turn_id}})]
            if outcome == "earlier_completed":
                return [
                    _reply(request, {"turn": {"id": turn_id}}),
                    _notice("turn/completed", {
                        "threadId": THREAD_ID, "turn": {"id": "earlier-turn"}
                    }),
                    _notice("turn/completed", {
                        "threadId": THREAD_ID, "turn": {"id": turn_id}
                    }),
                ]
            if outcome == "earlier_only":
                return [
                    _reply(request, {"turn": {"id": turn_id}}),
                    _notice("turn/completed", {
                        "threadId": THREAD_ID, "turn": {"id": "earlier-turn"}
                    }),
                ]
            if outcome == "completed_without_id":
                return [
                    _reply(request, {"turn": {"id": turn_id}}),
                    _notice("turn/completed", {"threadId": THREAD_ID, "turn": {}}),
                ]
            if outcome == "completed_then_peer":
                self.live_turn = "peer-turn"
                self.announce_live_completion = True
                return [
                    _reply(request, {"turn": {"id": turn_id}}),
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
            elif outcome == "failed":
                finished = _notice(
                    "turn/failed",
                    {"threadId": THREAD_ID, "error": {"message": "ordinary failure"}},
                )
            elif outcome == "failed_with_queued":
                self.live_turn = "peer-turn"
                self.announce_live_completion = True
                return [
                    _reply(request, {"turn": {"id": turn_id}}),
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
                    _reply(request, {"turn": {"id": turn_id}}),
                    _notice("turn/failed", {
                        "threadId": THREAD_ID, "error": {"message": CAPACITY}
                    }),
                    _notice("turn/started", {
                        "threadId": THREAD_ID, "turn": {"id": "peer-turn"}
                    }),
                ]
            elif outcome == "completed_then_capacity":
                return [
                    _reply(request, {"turn": {"id": turn_id}}),
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
            return [_reply(request, {"turn": {"id": turn_id}}), finished]
        if method == "thread/queue/list":
            return [_reply(request, {"data": self.queued.copy(), "nextCursor": None})]
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
            session_dir=str(self.session_dir), name="seat", cwd=str(self.session_dir),
            prompt_file=str(self.session_dir / "prompt.txt"),
            summary_file=str(self.session_dir / "summary.txt"),
            reply_file="", log_file=str(self.session_dir / "seat.log"),
            model="gpt-test", effort="high", service_tier="", sandbox="danger-full-access",
            timeout=self.start_args_timeout, resident=False,
        )

    def roster(self) -> dict[str, object]:
        return cast("dict[str, object]", json.loads(
            (self.session_dir / codex_mesh.ROSTER_FILE).read_text(encoding="utf-8")
        ))

    def seat_record(self) -> dict[str, object]:
        return cast("dict[str, object]", self.roster()["seat"])

    def methods(self, method: str) -> list[dict[str, object]]:
        return [request for request in self.server.requests if request.get("method") == method]

    def run_start(self) -> tuple[int, str]:
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            result = codex_mesh.command_start(self.start_args())
        return result, errors.getvalue()

    def run_follow(self, message: str = "Repair the open finding") -> tuple[int, str]:
        message_file = self.session_dir / "follow-up.txt"
        _ = message_file.write_text(message, encoding="utf-8")
        args = self.start_args()
        args.to = "seat"
        args.message_file = str(message_file)
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            result = codex_mesh.command_follow(args)
        return result, errors.getvalue()

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
        self.assertEqual(self.seat_record()["status"], "done")

    def test_follow_refuses_a_running_seat_before_sending(self) -> None:
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "seat": {"thread_id": THREAD_ID, "turn_id": "live-turn", "status": "running"}
        }), encoding="utf-8")
        code, errors = self.run_follow()
        self.assertEqual(code, 2, errors)
        self.assertEqual(self.methods("thread/resume"), [])
        self.assertEqual(self.methods("turn/start"), [])

    def test_follow_refuses_failed_seat_with_live_turn(self) -> None:
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "seat": {"thread_id": THREAD_ID, "turn_id": "", "status": "failed"}
        }), encoding="utf-8")
        self.live_turn = "peer-turn"
        code, errors = self.run_follow()
        self.assertEqual(code, 2, errors)
        self.assertEqual(self.methods("thread/resume"), [])
        self.assertEqual(self.methods("turn/start"), [])

    def test_follow_accepts_failed_seat_only_after_idle_read(self) -> None:
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "seat": {"thread_id": THREAD_ID, "turn_id": "", "status": "failed"}
        }), encoding="utf-8")

        code, errors = self.run_follow()

        self.assertEqual((code, errors), (0, ""))
        self.assertTrue(self.methods("thread/read"))
        self.assertEqual(len(self.methods("thread/resume")), 1)
        self.assertEqual(len(self.methods("turn/start")), 1)
        self.assertEqual(self.seat_record()["status"], "done")

    def test_earlier_completion_cannot_finish_follow_turn(self) -> None:
        code, errors = self.run_start()
        self.assertEqual((code, errors), (0, ""))
        self.outcomes = ["earlier_completed"]
        code, errors = self.run_follow()
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(len(self.methods("turn/start")), 2)
        self.assertEqual(self.seat_record()["status"], "done")

    def test_earlier_completion_alone_does_not_finish_follow_turn(self) -> None:
        code, errors = self.run_start()
        self.assertEqual((code, errors), (0, ""))
        self.outcomes = ["earlier_only"]
        self.start_args_timeout = 0.1

        code, errors = self.run_follow()

        self.assertNotEqual(code, 0, errors)
        self.assertEqual(len(self.methods("turn/start")), 2)

    def test_follow_failure_without_turn_id_finishes_as_failed(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        self.outcomes = ["failed"]
        self.start_args_timeout = 0.1

        code, errors = self.run_follow()

        self.assertEqual(code, 1)
        self.assertIn("ordinary failure", errors)
        self.assertNotIn("no turn/completed", errors)

    def test_follow_completion_without_turn_id_finishes_as_done(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        self.outcomes = ["completed_without_id"]
        self.start_args_timeout = 0.1

        code, errors = self.run_follow()

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.seat_record()["status"], "done")

    def test_follow_watches_peer_turn_queued_during_its_turn(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        self.outcomes = ["completed_then_peer"]

        code, errors = self.run_follow()

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.live_turn, "")
        self.assertEqual(self.methods("turn/interrupt"), [])
        self.assertEqual(self.seat_record()["status"], "done")

    def test_failed_followed_turn_remains_failed_after_peer_completes(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        self.outcomes = ["failed_with_queued"]

        code, errors = self.run_follow()

        self.assertEqual(code, 1, errors)
        self.assertIn("ordinary failure", errors)
        self.assertEqual(self.live_turn, "")
        self.assertEqual(self.seat_record()["status"], "failed")

    def test_claimed_follow_refuses_another_launcher_before_start(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        args = self.start_args()
        args.to = "seat"
        args.claim_pid = os.getpid()

        self.assertEqual(codex_mesh.command_can_follow(args), 0)
        self.assertEqual(self.seat_record()["status"], "starting")
        self.assertEqual(codex_mesh.command_can_follow(args), 2)
        self.assertEqual(len(self.methods("turn/start")), 1)

    def test_live_turn_after_resume_restores_preclaim_roster_when_peer_finishes(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        args = self.start_args()
        args.to = "seat"
        args.claim_pid = os.getpid()
        self.assertEqual(codex_mesh.command_can_follow(args), 0)
        self.live_turn = "peer-turn"
        self.announce_live_completion = True
        args.message_file = str(self.session_dir / "follow-up.txt")
        _ = Path(args.message_file).write_text("Follow up", encoding="utf-8")

        with contextlib.redirect_stderr(io.StringIO()) as errors:
            code = codex_mesh.command_follow(args)

        self.assertEqual(code, 1, errors.getvalue())
        self.assertEqual(self.seat_record()["status"], "done")
        self.assertEqual(self.live_turn, "")

    def test_claim_changed_after_dispatch_fails_without_overwriting_peer(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        args = self.start_args()
        args.to = "seat"
        args.claim_pid = os.getpid()
        self.assertEqual(codex_mesh.command_can_follow(args), 0)
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "seat": {"thread_id": THREAD_ID, "turn_id": "peer-turn", "status": "running"}
        }), encoding="utf-8")

        with contextlib.redirect_stderr(io.StringIO()) as errors:
            code = codex_mesh.command_follow(args)

        self.assertEqual(code, 1, errors.getvalue())
        self.assertEqual(self.seat_record()["status"], "running")
        self.assertEqual(len(self.methods("turn/start")), 1)

    def test_follow_recovers_dead_launcher_only_after_thread_is_idle(self) -> None:
        self.assertEqual(self.run_start()[0], 0)
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "seat": {"thread_id": THREAD_ID, "turn_id": "turn-1",
                     "status": "running", "launcher_pid": 99999999}
        }), encoding="utf-8")
        self.live_turn = "still-running"
        refused, _errors = self.run_follow()
        self.assertEqual(refused, 2)
        self.live_turn = ""

        code, errors = self.run_follow()

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.seat_record()["status"], "done")

    def test_running_roster_without_turn_id_has_no_active_state(self) -> None:
        state = codex_mesh._roster_thread({  # pyright: ignore[reportPrivateUsage]
            "thread_id": THREAD_ID, "status": "running", "turn_id": ""
        })

        self.assertIsInstance(state, codex_mesh.InvalidThread)

    def test_follow_refuses_running_roster_without_turn_id(self) -> None:
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "seat": {"thread_id": THREAD_ID, "status": "running"}
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
        self.assertEqual(self.seat_record()["thread_id"], THREAD_ID)

    def test_structured_capacity_error_resumes_the_same_thread(self) -> None:
        self.outcomes = ["capacity_structured", "completed"]
        code, _errors = self.run_start()
        self.assertEqual(code, 0)
        self.assertEqual(self.waits, [30.0])
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertEqual(self.seat_record()["thread_id"], THREAD_ID)

    def test_capacity_on_completed_notification_resumes_the_thread(self) -> None:
        self.outcomes = ["capacity_completed", "completed"]
        code, _errors = self.run_start()
        self.assertEqual(code, 0)
        self.assertEqual(self.waits, [30.0])
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertEqual(self.seat_record()["thread_id"], THREAD_ID)

    def test_other_turn_failure_does_not_use_capacity_backoff(self) -> None:
        self.outcomes = ["failed"]
        code, errors = self.run_start()
        self.assertEqual(code, 1)
        self.assertIn("ordinary failure", errors)
        self.assertNotIn("thread/read failed", errors)
        self.assertEqual(self.waits, [])
        self.assertEqual(len(self.methods("turn/start")), 1)
        self.assertEqual(self.seat_record()["thread_id"], THREAD_ID)

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
        self.assertEqual(self.seat_record()["thread_id"], THREAD_ID)
        self.assertEqual(self.seat_record()["status"], "capacity_exhausted")

    def test_failed_turn_does_not_abandon_a_peers_queued_turn(self) -> None:
        self.outcomes = ["failed_with_queued"]
        code, _errors = self.run_start()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertEqual(len(self.methods("turn/start")), 1)
        self.assertTrue(self.methods("thread/read"))
        self.assertEqual(self.seat_record()["thread_id"], THREAD_ID)

    def test_capacity_peer_turn_finishes_before_task_resume(self) -> None:
        """An unfinished peer turn cannot consume the task's retry."""
        self.outcomes = ["capacity_with_peer", "completed"]
        code, _errors = self.run_start()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertEqual(len(self.methods("turn/start")), 2)
        self.assertEqual(self.waits, [])
        self.assertNotIn("capacity retry", (self.session_dir / "seat.log").read_text(encoding="utf-8"))
        self.assertEqual(self.seat_record()["thread_id"], THREAD_ID)

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
        self.assertEqual(self.seat_record()["thread_id"], THREAD_ID)

    def test_capacity_waits_stop_at_twenty_minutes_and_name_the_thread(self) -> None:
        self.outcomes = ["capacity"] * 20
        code, errors = self.run_start()
        self.assertEqual(code, 1)
        self.assertEqual(self.waits[:5], [30.0, 60.0, 120.0, 240.0, 300.0])
        self.assertEqual(sum(self.waits), 1200.0)
        self.assertEqual(
            errors.splitlines()[-1],
            f"codex_mesh: seat: model still at capacity after {len(self.waits)} "
            + f"retries over 20 min; thread {THREAD_ID} stays on the roster "
            + "(codex_mesh.py end --to seat)",
        )
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertEqual(self.seat_record()["thread_id"], THREAD_ID)
        self.assertEqual(self.seat_record()["status"], "capacity_exhausted")
        log = (self.session_dir / "seat.log").read_text(encoding="utf-8")
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
                    session_dir=str(self.session_dir), to="seat",
                    message="Message while waiting", message_file="",
                )), 0)
            self.assertIn("delivered when", output.getvalue())
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
        """An exhausted seat has no launcher to watch new work."""
        self.outcomes = ["capacity"] * 20
        code, _errors = self.run_start()
        self.assertEqual(code, 1)
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            sent = codex_mesh.command_send(argparse.Namespace(
                session_dir=str(self.session_dir), to="seat",
                message="Too late", message_file="",
            ))
        self.assertEqual(sent, 2)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn("end --to seat", errors.getvalue())
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
        self.assertEqual(self.seat_record()["status"], "capacity_exhausted")

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
        self.assertEqual(self.seat_record()["thread_id"], THREAD_ID)

    def test_relaunch_refuses_a_live_thread_without_starting_another(self) -> None:
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "seat": {"thread_id": THREAD_ID, "turn_id": "peer-turn", "status": "running"}
        }), encoding="utf-8")
        self.live_turn = "peer-turn"
        code, errors = self.run_start()
        self.assertEqual(code, 2)
        self.assertIn(THREAD_ID, errors)
        self.assertIn("end", errors)
        self.assertEqual(self.methods("thread/start"), [])
        self.assertEqual(self.methods("turn/steer"), [])
        self.assertEqual(self.seat_record()["thread_id"], THREAD_ID)

    def test_relaunch_refuses_a_waiting_seat_with_a_live_launcher(self) -> None:
        """The waiting launcher owns its thread until it exits."""
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "seat": {"thread_id": THREAD_ID, "turn_id": "", "status": "waiting_capacity",
                     "launcher_pid": os.getpid()}
        }), encoding="utf-8")
        code, errors = self.run_start()
        self.assertEqual(code, 2)
        self.assertIn(THREAD_ID, errors)
        self.assertIn("waiting for capacity", errors)
        self.assertIn("end", errors)
        self.assertEqual(self.methods("thread/start"), [])

    def test_relaunch_refuses_live_waiting_launcher_without_server_record(self) -> None:
        """Retiring the server cannot release a live launcher's seat."""
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "seat": {"thread_id": THREAD_ID, "turn_id": "", "status": "waiting_capacity",
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
        """Replacing a seat clears messages held for its old thread."""
        old_thread = "old-waiting-thread"
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "seat": {"thread_id": old_thread, "turn_id": "", "status": "waiting_capacity",
                     "launcher_pid": 999999}
        }), encoding="utf-8")
        _ = (self.session_dir / "seat.pending.json").write_text(json.dumps({
            "thread_id": old_thread, "messages": ["First", "Second"]
        }), encoding="utf-8")
        code, _errors = self.run_start()
        self.assertEqual(code, 0)
        self.assertEqual(self.seat_record()["thread_id"], THREAD_ID)
        self.assertEqual((self.session_dir / "seat.pending.json").read_text(encoding="utf-8"), "")
        log = (self.session_dir / "seat.log").read_text(encoding="utf-8")
        self.assertIn("2 held messages", log)
        self.assertIn(old_thread, log)

    def test_relaunch_replaces_waiting_seat_whose_launcher_died(self) -> None:
        """A stale waiting entry does not block recovery."""
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "seat": {"thread_id": THREAD_ID, "turn_id": "", "status": "waiting_capacity",
                     "launcher_pid": 999999}
        }), encoding="utf-8")
        code, _errors = self.run_start()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertFalse(any(str(request.get("id", "")).startswith("relaunch")
                             for request in self.methods("thread/read")))
        self.assertEqual(self.seat_record()["thread_id"], THREAD_ID)

    def test_failed_seat_relaunches_when_old_thread_has_unknown_status(self) -> None:
        old_thread = "old-failed-thread"
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "seat": {"thread_id": old_thread, "turn_id": "", "status": "failed"}
        }), encoding="utf-8")
        self.thread_statuses[old_thread] = "retired"

        code, errors = self.run_start()

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertEqual(self.seat_record()["thread_id"], THREAD_ID)
        log = (self.session_dir / "seat.log").read_text(encoding="utf-8")
        self.assertIn(old_thread, log)
        self.assertIn("could not be interrupted", log)

    def test_failed_seat_relaunches_when_old_thread_has_system_error(self) -> None:
        old_thread = "old-failed-thread"
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "seat": {"thread_id": old_thread, "turn_id": "", "status": "failed"}
        }), encoding="utf-8")
        self.thread_statuses[old_thread] = "systemError"

        code, errors = self.run_start()

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertEqual(self.methods("turn/interrupt"), [])
        self.assertEqual(self.seat_record()["thread_id"], THREAD_ID)
        log = (self.session_dir / "seat.log").read_text(encoding="utf-8")
        self.assertNotIn("could not be interrupted", log)

    def test_failed_seat_interrupts_old_turn_before_starting_thread(self) -> None:
        old_thread = "old-failed-thread"
        old_turn = "old-live-turn"
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "seat": {"thread_id": old_thread, "turn_id": old_turn, "status": "failed"}
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
        self.assertEqual(self.seat_record()["thread_id"], THREAD_ID)

    def test_failed_seat_refuses_relaunch_when_interrupt_errors_and_turn_stays_live(self) -> None:
        old_thread = "old-failed-thread"
        old_turn = "old-live-turn"
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "seat": {"thread_id": old_thread, "turn_id": old_turn, "status": "failed"}
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
            (self.session_dir / "seat.log").read_text(encoding="utf-8"),
        )

    def test_failed_seat_relaunches_when_turn_ends_after_interrupt_error(self) -> None:
        old_thread = "old-failed-thread"
        old_turn = "old-live-turn"
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "seat": {"thread_id": old_thread, "turn_id": old_turn, "status": "failed"}
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
        self.assertEqual(self.seat_record()["thread_id"], THREAD_ID)

    def test_failed_seat_refuses_relaunch_when_active_turn_has_no_id(self) -> None:
        old_thread = "old-failed-thread"
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "seat": {"thread_id": old_thread, "turn_id": "", "status": "failed"}
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
            (self.session_dir / "seat.log").read_text(encoding="utf-8"),
        )

    def test_failed_seat_without_server_record_starts_on_new_server(self) -> None:
        old_thread = "old-failed-thread"
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "seat": {"thread_id": old_thread, "turn_id": "", "status": "failed"}
        }), encoding="utf-8")
        (self.session_dir / codex_mesh.SERVER_FILE).unlink()

        with patch.object(codex_mesh, "ensure_server", return_value=(self.server.port, True)) as ensure:
            code, errors = self.run_start()

        self.assertEqual((code, errors), (0, ""))
        ensure.assert_called_once()
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertFalse(any(
            cast("dict[str, object]", request.get("params", {})).get("threadId") == old_thread
            for request in self.methods("thread/read")
        ))
        self.assertEqual(self.seat_record()["thread_id"], THREAD_ID)

    def test_replaced_waiting_launcher_starts_no_resume_turn(self) -> None:
        """A stale launcher cannot overwrite its replacement."""
        self.outcomes = ["capacity"]
        original_wait = self.wait

        def replace_during_wait(seconds: float) -> None:
            original_wait(seconds)
            _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
                "seat": {"thread_id": "replacement-thread", "turn_id": "new-turn",
                         "status": "running"}
            }), encoding="utf-8")

        with patch.object(codex_mesh, "_capacity_sleep", replace_during_wait):
            code, _errors = self.run_start()
        self.assertEqual(code, 1)
        self.assertEqual(len(self.methods("turn/start")), 1)
        self.assertEqual(self.seat_record()["thread_id"], "replacement-thread")
        self.assertIn("replaced", (self.session_dir / "seat.log").read_text(encoding="utf-8"))

    def test_relaunch_refuses_when_thread_read_errors(self) -> None:
        """An unreadable active thread is never assumed idle."""
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "seat": {"thread_id": THREAD_ID, "turn_id": "", "status": "running"}
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
            "seat": {"thread_id": THREAD_ID, "turn_id": "", "status": "running"}
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
            "seat": {"thread_id": THREAD_ID, "turn_id": "", "status": "running"}
        }), encoding="utf-8")
        self.live_turn = "peer-turn"
        self.hide_live_turn_id = True
        with contextlib.redirect_stdout(io.StringIO()):
            code = codex_mesh.command_end(argparse.Namespace(
                session_dir=str(self.session_dir), to="seat"
            ))
        self.assertEqual(code, 0)
        self.assertEqual(self.live_turn, "")
        interrupts = self.methods("turn/interrupt")
        self.assertEqual(len(interrupts), 1)
        self.assertEqual(cast("dict[str, object]", interrupts[0]["params"])["turnId"], "peer-turn")

    def test_end_on_system_error_thread_has_no_turn_to_interrupt(self) -> None:
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "seat": {"thread_id": THREAD_ID, "turn_id": "", "status": "running"}
        }), encoding="utf-8")
        self.thread_statuses[THREAD_ID] = "systemError"

        with contextlib.redirect_stdout(io.StringIO()) as output, \
             contextlib.redirect_stderr(io.StringIO()) as errors:
            code = codex_mesh.command_end(argparse.Namespace(
                session_dir=str(self.session_dir), to="seat"
            ))

        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue(), "ending seat\n")
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(self.methods("turn/interrupt"), [])

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
                        session_dir=str(self.session_dir), to="seat",
                        message="Drop on end", message_file="",
                    )), 0)
                    self.assertEqual(codex_mesh.command_end(argparse.Namespace(
                        session_dir=str(self.session_dir), to="seat"
                    )), 0)
            original_wait(seconds)

        with patch.object(codex_mesh, "_capacity_sleep", end_while_waiting):
            _code, _errors = self.run_start()
        self.assertTrue(ended)
        self.assertTrue(self.methods("thread/queue/list"))
        self.assertTrue((self.session_dir / "seat.end").exists())
        self.assertEqual(self.seat_record()["thread_id"], THREAD_ID)
        pending = cast("dict[str, object]", json.loads(
            (self.session_dir / "seat.pending.json").read_text(encoding="utf-8") or "{}"
        ))
        self.assertEqual(pending, {})

    def test_send_after_end_refuses_waiting_seat_and_keeps_pending_empty(self) -> None:
        """An end marker closes the waiting seat to new messages."""
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            "seat": {"thread_id": THREAD_ID, "turn_id": "", "status": "waiting_capacity",
                     "launcher_pid": os.getpid()}
        }), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(codex_mesh.command_end(argparse.Namespace(
                session_dir=str(self.session_dir), to="seat"
            )), 0)
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            sent = codex_mesh.command_send(argparse.Namespace(
                session_dir=str(self.session_dir), to="seat",
                message="Too late", message_file="",
            ))
        self.assertEqual(sent, 2)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn("being ended", errors.getvalue())
        self.assertEqual((self.session_dir / "seat.pending.json").read_text(encoding="utf-8"), "")

    def test_unreadable_thread_after_completion_ends_and_cleans_up(self) -> None:
        """Failed liveness reads end the launcher through cleanup."""
        self.outcomes = ["completed"]
        original_read = self.respond
        clock_now = [1000.0]

        def clock() -> float:
            return clock_now[0]

        def unreadable_after_completion(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/read":
                self.read_failure = "error"
            return original_read(request)

        original_next_frame = codex_mesh.Client.next_frame

        def no_second_wait(client: codex_mesh.Client, deadline: float) -> codex_mesh.RpcMessage | None:
            if self.methods("thread/read") and self.read_failure == "error":
                clock_now[0] = deadline + 1
            return original_next_frame(client, deadline)

        self.server.respond = unreadable_after_completion
        with patch.object(codex_mesh, "time", SimpleNamespace(time=clock)), \
             patch.object(codex_mesh.Client, "next_frame", no_second_wait):
            code, errors = self.run_start()
        self.assertEqual(code, 1)
        self.assertIn(THREAD_ID, errors)
        self.assertIn("thread/read failed", errors)
        self.assertIn("read refused", errors)
        self.assertGreaterEqual(len(self.methods("thread/queue/list")), 2)
        self.assertIn("could not be interrupted", (self.session_dir / "seat.log").read_text(encoding="utf-8"))


def _attempt(
    seconds: float = 3.0, thread_exists: bool = False
) -> codex_mesh.RunOutcome:
    if thread_exists:
        return codex_mesh.FailedWithThread(THREAD_ID, USAGE_LIMIT, seconds)
    return codex_mesh.FailedBeforeThread(USAGE_LIMIT, seconds)


class RetryDecisionTests(unittest.TestCase):
    """Which failures earn a second run against a server this process started."""

    def test_a_fast_failure_on_an_inherited_server_is_retried(self) -> None:
        # The incident this exists for: three seats attach to a server left by
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

    def test_the_report_runs_the_blocked_verb_and_survives_its_failure(self) -> None:
        done = subprocess.CompletedProcess([], 0, "switched to claude for codex 1: delegate\n", "")
        with patch.object(subprocess, "run", return_value=done) as run, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            codex_mesh._report_quota_refusal("seat")  # pyright: ignore[reportPrivateUsage]
        command = cast("list[str]", run.call_args.args[0])
        self.assertEqual(command[1:], [str(codex_mesh.QUOTA_REPORT), "blocked"])
        self.assertTrue(codex_mesh.QUOTA_REPORT.is_file())
        self.assertIn("codex_mesh: seat: quota: switched to claude for codex 1: delegate", errors.getvalue())
        with patch.object(subprocess, "run", side_effect=OSError("no interpreter")), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            codex_mesh._report_quota_refusal("seat")  # pyright: ignore[reportPrivateUsage]
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
            self.summary, None, "seat", 1, "the answer"
        )
        self.assertEqual(self.summary.read_text(encoding="utf-8"), "the answer\n")

    def test_the_delegates_own_summary_survives_its_last_reply(self) -> None:
        # The incident: every seat's summary came back as its one-line
        # acknowledgement, because the reply had been written over it.
        _ = self.summary.write_text("files, doubts, unverified\n", encoding="utf-8")
        codex_mesh._finish_summary(  # pyright: ignore[reportPrivateUsage]
            self.summary, self.replies, "seat", 1, "Done."
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
            self.summary, self.replies, "seat", 1, "Done."
        )
        self.assertEqual(self.summary.read_text(encoding="utf-8"), "Done.\n")

    def test_resident_replies_accumulate_in_order(self) -> None:
        _ = self.summary.write_text("the delegate's summary\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), io.StringIO() as log:
            for index, text in enumerate(("first", "second"), start=1):
                codex_mesh._deliver_reply(  # pyright: ignore[reportPrivateUsage]
                    "seat", index, text, "", self.summary, self.replies, log
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
        quiet = codex_mesh.SWEEP_IDLE_SECS + 60
        base = codex_mesh.ServerFacts(pid=10, port=4000, age_secs=quiet, clients=0, session_dir=Path("/run"),
                                      folder_exists=True, marked_active=False, live_launchers=(), quiet_secs=quiet)
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
        for change in ({"clients": 1}, {"marked_active": True}, {"live_launchers": ("impl",)},
                       {"age_secs": 60.0}, {"quiet_secs": 60.0}):
            self.assertIsInstance(self.verdict(self.facts(**change)), codex_mesh.ServerInUse, change)
        self.assertIsInstance(self.verdict(self.facts(), ("impl: ThreadLive",)), codex_mesh.ServerInUse)
        self.assertIsInstance(self.verdict(self.facts(folder_exists=False), ("t1: ThreadLive",)),
                              codex_mesh.ServerInUse)

    def test_a_server_whose_log_names_no_run_folder_is_never_called_unused(self) -> None:
        self.assertIsInstance(self.verdict(self.facts(session_dir=None)), codex_mesh.ServerUnknown)

    def test_servers_and_connections_are_read_from_ps_and_ss(self) -> None:
        ps = ("  111 7200 codex app-server --listen ws://127.0.0.1:47165\n"
              "  222   30 /nix/store/x/bin/codex app-server --listen ws://127.0.0.1:51009\n"
              "  333   99 zsh -c pgrep -af codex app-server --listen ws://127.0.0.1: | head\n"
              "  444   99 codex app-server --listen stdio://\n")
        ss = "0 0 127.0.0.1:47165 127.0.0.1:60000\n0 0 127.0.0.1:60000 127.0.0.1:47165\n0 0 [::1]:47165 [::1]:60001\n"
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, ps, "")):
            servers = codex_mesh._running_servers()  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(servers, [(111, 47165, 7200.0), (222, 51009, 30.0)])
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, ss, "")):
            counts = codex_mesh._client_counts()  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(counts, {47165: 2, 60000: 1})

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
        old = codex_mesh.SWEEP_IDLE_SECS + 60
        asked: list[int] = []

        def stopper(_root: Path, pid: int, _port: int, _folder: Path) -> bool:
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
        # Only the server every other check cleared is asked about its conversations.
        busy.assert_called_once_with(4002, {})
        return [line.split("\t") for line in printed.getvalue().splitlines()], asked

    def test_the_report_lists_each_server_and_stops_nothing(self) -> None:
        rows, asked = self.sweep(stop=False)
        self.assertEqual([row[2] for row in rows[:3]], ["in use", "unused", "unknown"])
        self.assertEqual(rows[3], ["3 app-server(s), 1 unused; report only, nothing was stopped"])
        self.assertEqual(asked, [])

    def test_stop_reaches_only_the_unused_server(self) -> None:
        rows, asked = self.sweep(stop=True)
        self.assertEqual([row[2] for row in rows[:3]], ["in use", "stopped", "unknown"])
        self.assertEqual(rows[3], ["3 app-server(s), 1 unused; 1 stopped"])
        self.assertEqual(asked, [2])
        rows, _asked = self.sweep(stop=True, stops=False)
        self.assertEqual([row[2] for row in rows[:3]], ["in use", "kept", "unknown"])
        self.assertEqual(rows[3], ["3 app-server(s), 1 unused; 0 stopped"])

    def second_look(self, folder: Path, running: list[tuple[int, int, float]], clients: dict[int, int],
                    verdict: codex_mesh.SweepVerdict) -> tuple[bool, list[int]]:
        """Whether `_stop_unused` stopped pid 2 on port 4002, and the pids it signalled."""
        reaped: list[int] = []

        def reap(pid: int) -> bool:
            reaped.append(pid)
            return True

        with patch.object(codex_mesh, "_running_servers", return_value=running), \
                patch.object(codex_mesh, "_client_counts", return_value=clients), \
                patch.object(codex_mesh, "_judge", return_value=(verdict, folder)) as judge, \
                patch.object(codex_mesh, "_reap", side_effect=reap):
            stopped = codex_mesh._stop_unused(folder.parent, 2, 4002, folder)  # pyright: ignore[reportPrivateUsage]
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
            # The pid stopped running this server, or now runs it on another port: nothing is signalled.
            self.assertEqual(self.second_look(run, [], {}, unused), (False, []))
            self.assertEqual(self.second_look(run, [(2, 4999, 9000.0)], {}, unused), (False, []))
            # A client arrived between the two looks.
            self.assertEqual(self.second_look(run, [(2, 4002, 9000.0)], {4002: 1}, in_use), (False, []))
            self.assertTrue(record.exists())
            self.assertEqual(self.second_look(run, [(2, 4002, 9000.0)], {}, unused), (True, [2]))
            self.assertFalse(record.exists())
            # A record that names a newer server is left for that server.
            _ = record.write_text(json.dumps({"pid": 7, "port": 4007}))
            self.assertEqual(self.second_look(run, [(2, 4002, 9000.0)], {}, unused), (True, [2]))
            self.assertTrue(record.exists())
            # A deleted run folder is not created again.
            gone = Path(directory) / "gone"
            self.assertEqual(self.second_look(gone, [(2, 4002, 9000.0)], {}, unused), (True, [2]))
            self.assertFalse(gone.exists())
        with patch.object(codex_mesh, "_running_servers", side_effect=OSError("no ps")), \
                patch.object(codex_mesh, "_reap") as reap:
            self.assertFalse(codex_mesh._stop_unused(gone.parent, 2, 4002, gone))  # pyright: ignore[reportPrivateUsage]
        reap.assert_not_called()


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

    def write_server(self, port: int, pid: int) -> None:
        _ = (self.session_dir / codex_mesh.SERVER_FILE).write_text(
            json.dumps({"port": port, "pid": pid}), encoding="utf-8"
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
        # The seats fail together and each tries this recovery. Only the
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


if __name__ == "__main__":
    _ = unittest.main()
