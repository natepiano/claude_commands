"""Test the shared wall-clock escape hatch without sleeping."""

from concurrent.futures import Future
import signal
import threading
import unittest
from unittest.mock import patch

from scripts.lib import test_hang_guard


class HangGuardTests(unittest.TestCase):
    def test_expiration_names_the_wait(self) -> None:
        guard = test_hang_guard.LauncherHangGuard("owed continuation RPC")
        with patch.object(test_hang_guard, "TEST_HANG_GUARD_SECS", 0):
            with self.assertRaisesRegex(TimeoutError, "owed continuation RPC.*expired"):
                _ = guard.remaining()

    def test_already_published_events_and_results_do_not_lose_the_wake(self) -> None:
        guard = test_hang_guard.LauncherHangGuard("published before wait")
        ready = threading.Event()
        ready.set()
        guard.event(ready)
        result: Future[str] = Future()
        result.set_result("ready")
        self.assertEqual(guard.result(result), "ready")

    def test_synchronous_io_has_a_named_escape_and_restores_the_handler(self) -> None:
        handler = signal.getsignal(signal.SIGALRM)
        with self.assertRaisesRegex(AssertionError, "provider refusal RPC.*synchronous fixture"):
            with test_hang_guard.LauncherHangGuard("provider refusal RPC").protect():
                signal.raise_signal(signal.SIGALRM)
        self.assertEqual(signal.getsignal(signal.SIGALRM), handler)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_a_task_timeout_is_preserved(self) -> None:
        result: Future[None] = Future()
        error = TimeoutError("RPC timed out")
        result.set_exception(error)
        with self.assertRaises(TimeoutError) as failure:
            _ = test_hang_guard.LauncherHangGuard("future").result(result)
        self.assertIs(failure.exception, error)
