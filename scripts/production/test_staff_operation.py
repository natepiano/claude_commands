"""Real operation-lock contention with bounded flock waits."""
from __future__ import annotations

import subprocess
import sys
import time

from scripts.production import staff
from scripts.production.staff_test_support import CHECKOUT, StaffTestCase


class StaffOperationTests(StaffTestCase):
    def test_foreign_holder_refuses_by_lock_pid_and_elapsed_wait(self) -> None:
        lock = self.staff_file.parent / "operation.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        program = ("import fcntl, sys; "
                   "lock = open(sys.argv[1], 'a'); fcntl.flock(lock, fcntl.LOCK_EX); "
                   "print('locked', flush=True); sys.stdin.read()")
        with subprocess.Popen([sys.executable, "-c", program, str(lock)], cwd=CHECKOUT,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True) as holder:
            assert holder.stdout is not None
            self.assertEqual(holder.stdout.readline(), "locked\n")
            try:
                limit = 0.2
                # A subprocess hang guard also makes this fail on the unbounded implementation.
                contender = ("from scripts.production import staff; "
                             f"staff.OPERATION_LOCK_TIMEOUT_SECONDS = {limit}; "
                             "raise SystemExit(staff.cli_result(lambda: "
                             "staff.operation().__enter__()))")
                started = time.monotonic()
                refused = subprocess.run([sys.executable, "-c", contender], cwd=CHECKOUT,
                                         capture_output=True, text=True, check=False, timeout=3)
                elapsed = time.monotonic() - started
                self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
                self.assertIn(str(lock), refused.stderr)
                self.assertIn(f"limit {limit:.2f}s", refused.stderr)
                self.assertRegex(refused.stderr, r"refused after \d+\.\d{2}s")
                if sys.platform == "linux":
                    self.assertIn(f"pid {holder.pid}", refused.stderr)
                self.assertGreaterEqual(elapsed, limit)
                self.assertLess(elapsed, 3)
            finally:
                _ = holder.communicate(timeout=3)
        with staff.operation():
            self.assertTrue(staff.operation_fds())
