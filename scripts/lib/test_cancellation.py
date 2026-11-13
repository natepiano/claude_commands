"""Check cancellation masks in the creator and helper threads."""
from __future__ import annotations

import signal
import threading
import unittest

from scripts.lib import cancellation
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class CancellationTests(unittest.TestCase):
    def test_masked_thread_blocks_cancellation_and_preserves_the_creators_mask(self) -> None:
        before = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        masks: list[frozenset[int]] = []

        def read_mask() -> None:
            masks.append(frozenset(signal.pthread_sigmask(signal.SIG_BLOCK, set())))

        worker = threading.Thread(target=read_mask, daemon=True)
        cancellation.start_masked_thread(worker)
        worker.join(TEST_HANG_GUARD_SECS)
        self.assertFalse(worker.is_alive())
        self.assertTrue(worker.daemon)
        self.assertEqual(masks, [before | {signal.SIGTERM, signal.SIGHUP, signal.SIGINT}])
        self.assertEqual(signal.pthread_sigmask(signal.SIG_BLOCK, set()), before)

    def test_masked_thread_keeps_the_callers_non_daemon_setting(self) -> None:
        masks: list[frozenset[int]] = []
        worker = threading.Thread(
            target=lambda: masks.append(frozenset(signal.pthread_sigmask(signal.SIG_BLOCK, set()))),
            daemon=False)
        cancellation.start_masked_thread(worker)
        worker.join(TEST_HANG_GUARD_SECS)
        self.assertFalse(worker.is_alive())
        self.assertFalse(worker.daemon)
        self.assertTrue(set(cancellation.CANCELLATION_SIGNALS).issubset(masks[0]))
