#!/usr/bin/env python3
"""The Codex daemon reader speaks WebSocket JSON-RPC over a Unix socket."""

from __future__ import annotations

import importlib
import os
import socket
import subprocess
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import cast, override
from unittest import mock

from scripts.lib.socket_test_support import short_socket_directory
import codex_daemon


from codex_test_support import FakeDaemon, receive_frame, reply, send_frame, write_codex_account


class CodexDaemonTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(short_socket_directory()))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.root)}, clear=True)))

    def test_loaded_sessions_initializes_pages_and_reads_each_thread(self) -> None:
        methods: list[str] = []

        def conversation(connection: socket.socket) -> None:
            initialized = receive_frame(connection)
            methods.append(cast(str, initialized["method"]))
            self.assertEqual(initialized["params"], {
                "clientInfo": {"name": "roster", "version": "1"},
            })
            reply(connection, initialized, {})

            notification = receive_frame(connection)
            methods.append(cast(str, notification["method"]))
            self.assertNotIn("id", notification)

            first_page = receive_frame(connection)
            methods.append(cast(str, first_page["method"]))
            reply(connection, first_page, {
                "data": ["thread-one"], "nextCursor": "more",
            })
            second_page = receive_frame(connection)
            methods.append(cast(str, second_page["method"]))
            self.assertEqual(second_page["params"], {"cursor": "more"})
            reply(connection, second_page, {
                "data": ["thread-two"], "nextCursor": None,
            })

            for thread_id, name, cwd, status in (
                ("thread-one", "First window", "/work/one", "active"),
                ("thread-two", "Second window", "/work/two", "idle"),
            ):
                request = receive_frame(connection)
                methods.append(cast(str, request["method"]))
                params = cast(dict[str, object], request["params"])
                self.assertEqual(params["threadId"], thread_id)
                reply(connection, request, {"thread": {
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
            initialize = receive_frame(connection)
            reply(connection, initialize, {})
            self.assertEqual(receive_frame(connection)["method"], "initialized")
            self.assertEqual(receive_frame(connection)["method"], "thread/loaded/list")
            connection.shutdown(socket.SHUT_RDWR)

        socket_path = self.root / "closing.sock"
        with FakeDaemon(socket_path, conversation):
            found = codex_daemon.loaded_sessions(socket_path)

        self.assertIsInstance(found, codex_daemon.DaemonUnreadable)
        assert isinstance(found, codex_daemon.DaemonUnreadable)
        self.assertTrue(found.reason)

    def test_unrelated_notifications_cannot_extend_the_shared_deadline(self) -> None:
        def conversation(connection: socket.socket) -> None:
            initialize = receive_frame(connection)
            reply(connection, initialize, {})
            self.assertEqual(receive_frame(connection)["method"], "initialized")
            self.assertEqual(receive_frame(connection)["method"], "thread/loaded/list")
            try:
                while True:
                    send_frame(connection, {
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
            initialize = receive_frame(connection)
            reply(connection, initialize, {})
            self.assertEqual(receive_frame(connection)["method"], "initialized")
            for next_cursor in ("A", "B", "A"):
                request = receive_frame(connection)
                params = cast(dict[str, object], request["params"])
                requested_cursors.append(params.get("cursor", ""))
                reply(connection, request, {"data": [], "nextCursor": next_cursor})

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

    def test_daemon_socket_override_replaces_all_account_homes(self) -> None:
        chosen = self.root / "chosen.sock"
        self.assertEqual(codex_daemon.daemons({"ROSTER_CODEX_SOCKET": str(chosen)}),
                         [codex_daemon.CodexDaemon(chosen.parent.parent, chosen)])

    def test_daemon_discovery_lists_accounts_independently_of_the_process_home(self) -> None:
        (self.root / ".codex2").mkdir()
        (self.root / ".codex1").mkdir()
        chosen = self.root / "custom home"
        suffix = Path("app-server-control/app-server-control.sock")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(chosen)}):
            self.assertEqual(codex_daemon.daemons(os.environ), [
                codex_daemon.CodexDaemon(home, home / suffix)
                for home in (self.root / ".codex", self.root / ".codex1", self.root / ".codex2")
            ])

    def test_default_and_numbered_daemons_remain_discoverable_when_workers_move(self) -> None:
        notes = self.root / "notes"
        home = self.root / ".codex1"
        write_codex_account(home, notes, "codex1")
        with mock.patch.dict(os.environ, {"AGENT_NOTES_DIR": str(notes)}):
            module = importlib.import_module("scripts.accounts.accounts")
            run_accounts = cast(Callable[[list[str]], int], module.main)
            self.assertEqual(run_accounts(["set-role", "workers", "codex1"]), 0)
            found = codex_daemon.daemons(os.environ)
        self.assertEqual([daemon.home for daemon in found], [self.root / ".codex", home])

    def test_daemon_queue_keeps_the_owner_after_the_callers_environment_changes(self) -> None:
        chosen = self.root / "chosen.sock"
        owner = codex_daemon.daemons({"ROSTER_CODEX_SOCKET": str(chosen)})[0]
        def conversation(connection: socket.socket) -> None:
            reply(connection, receive_frame(connection), {})
            _ = receive_frame(connection)
            request = receive_frame(connection)
            self.assertEqual(request["method"], "thread/queue/add")
            self.assertEqual(cast(dict[str, object], request["params"])["threadId"], "thread")
            reply(connection, request, {})
        with FakeDaemon(chosen, conversation), mock.patch.dict(os.environ, {
            "CODEX_HOME": str(self.root / "caller"), "ROSTER_CODEX_SOCKET": str(self.root / "other.sock"),
        }):
            self.assertEqual(codex_daemon.queue(owner, "thread", "message"), codex_daemon.Queued())

    def test_explicit_daemon_socket_receives_the_message_at_its_exact_path(self) -> None:
        chosen = self.root / "arbitrary.sock"
        methods: list[str] = []
        def conversation(connection: socket.socket) -> None:
            initialize = receive_frame(connection)
            methods.append(cast(str, initialize["method"]))
            params = cast(dict[str, object], initialize["params"])
            self.assertEqual(params["capabilities"], {"experimentalApi": True})
            reply(connection, initialize, {})
            initialized = receive_frame(connection)
            methods.append(cast(str, initialized["method"]))
            request = receive_frame(connection)
            methods.append(cast(str, request["method"]))
            params = cast(dict[str, object], request["params"])
            self.assertEqual(params["threadId"], "thread")
            self.assertEqual(params["input"], [{"type": "text", "text": "message"}])
            reply(connection, request, {"queuedSubmission": {"id": "queued-message"}})
        with FakeDaemon(chosen, conversation), mock.patch.dict(os.environ, {
            "CODEX_HOME": str(self.root / "caller"), "ROSTER_CODEX_SOCKET": str(chosen),
        }), mock.patch.object(subprocess, "run") as run:
            self.assertEqual(codex_daemon.daemons(os.environ), [codex_daemon.CodexDaemon(chosen.parent.parent, chosen)])
            self.assertEqual(codex_daemon.queue(codex_daemon.CodexDaemon(chosen.parent.parent, chosen), "thread", "message", timeout=1), codex_daemon.Queued())
        run.assert_not_called()
        self.assertEqual(methods, ["initialize", "initialized", "thread/queue/add"])

    def test_explicit_daemon_socket_failure_does_not_fall_back_to_another_home(self) -> None:
        chosen = self.root / "refused.sock"
        def conversation(connection: socket.socket) -> None:
            initialize = receive_frame(connection)
            reply(connection, initialize, {})
            _ = receive_frame(connection)
            request = receive_frame(connection)
            send_frame(connection, {"id": request["id"], "error": {"message": "thread refused"}})
        with FakeDaemon(chosen, conversation), mock.patch.dict(os.environ, {
            "CODEX_HOME": str(self.root / "caller"), "ROSTER_CODEX_SOCKET": str(chosen),
        }), mock.patch.object(subprocess, "run") as run:
            result = codex_daemon.queue(codex_daemon.CodexDaemon(chosen.parent.parent, chosen), "thread", "message", timeout=1)
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
                    initialize = receive_frame(connection)
                    self.assertEqual(initialize["method"], "initialize")
                    if waiting_on == "thread/queue/add":
                        reply(connection, initialize, {})
                        self.assertEqual(receive_frame(connection)["method"], "initialized")
                        self.assertEqual(receive_frame(connection)["method"], "thread/queue/add")
                    connection.settimeout(2)
                    self.assertEqual(connection.recv(1), b"")

                with FakeDaemon(chosen, conversation), mock.patch.dict(os.environ, {
                    "CODEX_HOME": str(self.root / "caller"), "ROSTER_CODEX_SOCKET": str(chosen),
                }), mock.patch.object(subprocess, "run") as run:
                    result = codex_daemon.queue(codex_daemon.CodexDaemon(chosen.parent.parent, chosen), "thread", "message", timeout=0.25)

                self.assertEqual(result, codex_daemon.QueueTimedOut(0.25))
                run.assert_not_called()

    def test_timeout_cases_fail_when_queue_returns_without_contacting_the_daemon(self) -> None:
        result = unittest.TestResult()
        case = CodexDaemonTests("test_explicit_daemon_socket_timeout_never_falls_back_to_the_process_home")
        with mock.patch.object(codex_daemon, "queue", return_value=codex_daemon.QueueTimedOut(0.25)):
            _ = case.run(result)
        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.failures), 2)
        for test, failure in result.failures:
            self.assertIn("fake daemon conversation did not run to completion", failure)
            self.assertIn("waiting_on=", str(test))

    def test_queue_requires_a_live_daemon_instead_of_writing_an_offline_queue(self) -> None:
        owner = codex_daemon.CodexDaemon(self.root, self.root / "missing.sock")
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("no CLI queue")):
            result = codex_daemon.queue(owner, "thread-one", "please review", timeout=4.5)
        self.assertIsInstance(result, codex_daemon.QueueFailed)
        self.assertFalse((self.root / "queue_1.sqlite").exists())

    def test_queue_requires_an_object_acknowledgement_from_the_owner(self) -> None:
        chosen = self.root / "invalid-result.sock"
        def conversation(connection: socket.socket) -> None:
            reply(connection, receive_frame(connection), {})
            _ = receive_frame(connection)
            request = receive_frame(connection)
            send_frame(connection, {"id": request["id"], "result": None})
        with FakeDaemon(chosen, conversation):
            result = codex_daemon.queue(codex_daemon.CodexDaemon(self.root, chosen), "thread-one", "hello")
        self.assertEqual(result, codex_daemon.QueueFailed("thread/queue/add: response has no result object"))

    def test_queue_reports_a_stale_socket_as_failure(self) -> None:
        chosen = self.root / "stale.sock"
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(chosen))
        result = codex_daemon.queue(codex_daemon.CodexDaemon(self.root, chosen), "thread-one", "hello")
        self.assertIsInstance(result, codex_daemon.QueueFailed)


if __name__ == "__main__":
    _ = unittest.main()
