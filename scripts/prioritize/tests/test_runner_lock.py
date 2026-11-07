from __future__ import annotations

import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import final


PRIORITIZE_DIR = Path(__file__).parents[1]
from scripts.prioritize import runner_lock
from scripts.prioritize import writer_lock


@final
class RunnerLockTests(unittest.TestCase):
    def test_busy_lock_returns_temporary_failure_without_running(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runner.lock"
            with writer_lock.acquire_writer_lock(path):
                result = runner_lock.main(
                    ["run", str(path), sys.executable, "-c", "pass"]
                )

            self.assertEqual(result, runner_lock.BUSY_EXIT)

    def test_free_lock_runs_command_and_releases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runner.lock"
            completed = subprocess.run(
                [
                    str(PRIORITIZE_DIR.parent / "lib/py"),
                    str(PRIORITIZE_DIR / "runner_lock.py"),
                    "run",
                    str(path),
                    sys.executable,
                    "-c",
                    "raise SystemExit(3)",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 3, completed.stderr)
            self.assertFalse(writer_lock.lock_is_held(path))


if __name__ == "__main__":
    _ = unittest.main()
