"""Keep launcher synchronization independent of worker lifecycle deadlines."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from typing import final, override
import unittest
from unittest.mock import patch

from scripts.agents import codex_mesh
from scripts.agents.test_codex_mesh import FixtureLoopBudget, LifecycleTestClock, StubAppServer
from scripts.direct.recorder_fixture import LauncherHangGuard


@final
class LauncherWaitTests(unittest.TestCase):
    # unittest constructs the live watcher and its clock together in setUp.
    clock: LifecycleTestClock  # pyright: ignore[reportUninitializedInstanceVariable]
    target: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    watch: codex_mesh.LinuxFileChanges | codex_mesh.KqueueFileChanges  # pyright: ignore[reportUninitializedInstanceVariable]
    guard: LauncherHangGuard  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.clock = LifecycleTestClock()
        self.clock.freeze()
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.target = Path(directory) / "ready"
        _ = self.enterContext(patch.object(codex_mesh, "time", self.clock))
        self.watch = (codex_mesh.LinuxFileChanges({self.target}) if sys.platform.startswith("linux")
                      else codex_mesh.KqueueFileChanges({self.target}))
        self.addCleanup(self.watch.close)
        self.guard = LauncherHangGuard(self.id())

    def test_file_wait_uses_controlled_lifecycle_time_despite_wall_clock_progress(self) -> None:
        deadline = self.clock.monotonic() + 1
        # Simulate a loaded machine without making the test sleep.
        with patch.object(time, "monotonic", return_value=self.guard.started + 120):
            self.assertLess(self.clock.monotonic(), deadline)
            with ThreadPoolExecutor(max_workers=1) as executor:
                waiting = executor.submit(self.guard.change, self.watch.wait, self.clock.monotonic)
                self.target.touch()
                event = self.guard.result(waiting)
            self.assertIsInstance(event, codex_mesh.StateFilesChanged)
        self.clock.advance(1)
        wake, peer = socket.socketpair()
        with wake, peer:
            self.assertEqual(self.watch.wait(wake, deadline), codex_mesh.WaitDeadlineExpired.EXPIRED)

    def test_hang_guard_wakes_a_file_wait_even_when_lifecycle_time_is_frozen(self) -> None:
        # Exercise the real watchdog/socket wakeup without waiting out the hang budget.
        with patch.object(self.guard, "remaining", return_value=0):
            with self.assertRaisesRegex(TimeoutError, "launcher state never changed"):
                _ = self.guard.change(self.watch.wait, self.clock.monotonic)

    def test_partial_socket_handshake_uses_the_launcher_hang_deadline(self) -> None:
        server = StubAppServer(lambda _request: None)
        server.hang_guard = self.guard
        self.addCleanup(server.close)
        fixture_time = time.perf_counter()
        real_recv = socket.socket.recv
        partial_read = threading.Event()

        def delayed_recv(connection: socket.socket, count: int, flags: int = 0) -> bytes:
            nonlocal fixture_time
            received = real_recv(connection, count, flags)
            if received and connection.getsockname()[1] == server.port and not partial_read.is_set():
                # Scheduling delay after the first real partial read. The old
                # handshake budget expires, while the shared guard still has time.
                fixture_time += 6
                self.assertGreater(self.guard.remaining(), 6)
                partial_read.set()
            return received

        # The budget's default clock is a retained callable, so replace it at
        # its constructor boundary to model elapsed time without sleeping.
        def budget(name: str, *, seconds: float = 120, max_calls: int = 2_000) -> FixtureLoopBudget:
            return FixtureLoopBudget(name, seconds=seconds, max_calls=max_calls, clock=lambda: fixture_time)

        with (patch("scripts.agents.test_codex_mesh.FixtureLoopBudget", side_effect=budget),
              patch.object(socket.socket, "recv", new=delayed_recv),
              socket.create_connection(("127.0.0.1", server.port)) as client):
            client.settimeout(self.guard.remaining())
            client.sendall(b"GET / HTTP/1.1\r\n")
            self.guard.event(partial_read)
            client.sendall(b"Host: fixture\r\n\r\n")
            response = client.recv(4096)
            self.assertIn(b"101 Switching Protocols", response)
        self.assertTrue(partial_read.is_set())

    def test_socket_loop_count_still_bounds_repeated_handshake_work(self) -> None:
        budget = FixtureLoopBudget("handshake", max_calls=1)
        budget.check_calls()
        with self.assertRaisesRegex(AssertionError, "exceeded 1 fixture calls"):
            budget.check_calls()
