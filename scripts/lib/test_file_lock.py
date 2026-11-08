"""Keep exclusive locks across concurrent operations and exceptions."""
from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from scripts.lib import file_lock


class FileLockTests(unittest.TestCase):
    def test_blocking_acquire_continues_after_the_holder_releases(self) -> None:
        with tempfile.TemporaryDirectory() as directory, ThreadPoolExecutor(max_workers=1) as executor:
            path = Path(directory) / "operation.lock"
            entered, acquired = threading.Event(), threading.Event()

            def acquire() -> file_lock.LockResult:
                entered.set()
                with file_lock.exclusive(path) as result:
                    acquired.set()
                    return result

            with file_lock.exclusive(path) as held:
                self.assertIs(held, file_lock.LockResult.ACQUIRED)
                contender = executor.submit(acquire)
                self.assertTrue(entered.wait(5))
                self.assertFalse(acquired.is_set())
            self.assertIs(contender.result(5), file_lock.LockResult.ACQUIRED)
            self.assertTrue(acquired.is_set())

    def test_nonblocking_acquire_reports_a_busy_lock_without_entering_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operation.lock"
            with file_lock.exclusive(path):
                with file_lock.exclusive(path, wait=False) as result:
                    self.assertIs(result, file_lock.LockResult.BUSY)
            with file_lock.exclusive(path, wait=False) as result:
                self.assertIs(result, file_lock.LockResult.ACQUIRED)

    def test_an_exception_releases_the_lock_for_the_next_operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operation.lock"
            with self.assertRaisesRegex(RuntimeError, "operation failed"):
                with file_lock.exclusive(path):
                    raise RuntimeError("operation failed")
            with file_lock.exclusive(path, wait=False) as result:
                self.assertIs(result, file_lock.LockResult.ACQUIRED)
