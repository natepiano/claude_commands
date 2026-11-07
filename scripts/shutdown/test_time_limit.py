#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

# Hang guard: natedev reached load 136 on 2026-10-29; scheduling delays are not failures.
HANG_GUARD_SECONDS = 300

SHUTDOWN = Path(__file__).resolve().parent
RUNAWAY = """
import time
import unittest
import test_support
test_support.TEST_TIME_LIMIT_SECONDS = 1
class Runaway(test_support.ShutdownTestCase):
    def test_never_finishes(self) -> None:
        while True:
            time.sleep(0.01)
unittest.main(argv=["runaway"])
"""


class TimeLimitTests(unittest.TestCase):
    def test_a_shutdown_test_that_never_finishes_ends_the_run_with_its_stack(self) -> None:
        paths = os.pathsep.join((str(SHUTDOWN), str(SHUTDOWN.parents[1])))
        result = subprocess.run([sys.executable, "-c", RUNAWAY], cwd=SHUTDOWN, capture_output=True, text=True,
                                timeout=HANG_GUARD_SECONDS, check=False, env={**os.environ, "PYTHONPATH": paths})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Timeout", result.stderr)
        self.assertIn("test_never_finishes", result.stderr)


if __name__ == "__main__":
    _ = unittest.main()
