"""Real operation-lock contention with bounded flock waits."""
from __future__ import annotations

from pathlib import Path

import subprocess
import sys
import time
import fcntl
import math
from unittest import mock

from scripts.production import staff
from scripts.agents import worker_lifecycle
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
                _ = self.staff_config(lock_seconds=limit)
                # A subprocess hang guard also makes this fail on the unbounded implementation.
                contender = ("from scripts.production import staff; "
                             "raise SystemExit(staff.cli_result(lambda: "
                             "staff.operation().__enter__()))")
                started = time.monotonic()
                refused = subprocess.run([str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-c", contender], cwd=CHECKOUT,
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


    def test_row_transaction_stops_at_the_operation_deadline_without_a_write(self) -> None:
        owner, _, _ = self.tree()
        before = self.staff_file.read_bytes()
        lock = self.staff_file.with_name("staff.json.lock")
        program = (
            "from scripts.production import staff; "
            "from scripts.agents import worker_lifecycle; "
            "import sys; "
            "scope = worker_lifecycle.operation_deadline(timeout=0.15); scope.__enter__(); "
            "staff.rename(staff.StaffId(sys.argv[1]), 'blocked')"
        )
        with lock.open("a") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            result = subprocess.run([str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-c", program, owner.id], cwd=CHECKOUT,
                                    capture_output=True, text=True, check=False, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("OperationDeadlineExpired", result.stderr)
        self.assertIn(str(lock), result.stderr)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_remote_timeout_at_a_shorter_operation_deadline_keeps_its_exception_type(self) -> None:
        _ = self.staff_config(remote_seconds=2)
        run = subprocess.run

        def blocked_remote(arguments: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            self.assertEqual(arguments[0], "ssh")
            timeout = options["timeout"]
            assert isinstance(timeout, (int, float))
            return run([sys.executable, "-c", "import signal; signal.pause()"],
                       capture_output=True, text=True, check=False, timeout=timeout)

        with (mock.patch.object(staff, "is_owner", return_value=False),
              mock.patch.object(subprocess, "run", side_effect=blocked_remote),
              worker_lifecycle.operation_deadline(timeout=0.1)):
            with self.assertRaises(worker_lifecycle.OperationDeadlineExpired):
                _ = staff.snapshot()

    def test_remote_limit_refreshes_and_an_operation_deadline_only_shortens_it(self) -> None:
        _ = self.tree()
        for configured in (1.25, 2.5):
            _ = self.staff_config(remote_seconds=configured)
            for budget in (math.inf, 0.2, configured + 10):
                with self.subTest(configured=configured, budget=budget), self.machine("mac"):
                    observed: list[tuple[list[str], float]] = []

                    def remote_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                        observed.append((command, float(str(kwargs["timeout"]))))
                        raise subprocess.TimeoutExpired(command, float(str(kwargs["timeout"])))

                    started = time.monotonic()
                    with (mock.patch.object(subprocess, "run", side_effect=remote_run),
                          worker_lifecycle.operation_deadline(timeout=budget)):
                        with self.assertRaises((staff.StaffUnreadable, worker_lifecycle.OperationDeadlineExpired)):
                            _ = staff.snapshot()
                    self.assertEqual(len(observed), 1)
                    command, timeout = observed[0]
                    expected = min(configured, budget)
                    self.assertLessEqual(timeout, expected)
                    self.assertGreater(timeout, expected - (time.monotonic() - started) - 0.01)
                    connect = next(value for value in command if value.startswith("ConnectTimeout="))
                    self.assertLessEqual(int(connect.split("=", 1)[1]), math.ceil(expected))
