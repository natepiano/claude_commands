from __future__ import annotations

import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import final


PRIORITIZE_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(PRIORITIZE_DIR))
import runner_lock  # pyright: ignore[reportImplicitRelativeImport]
import writer_lock  # pyright: ignore[reportImplicitRelativeImport]


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
                    sys.executable,
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
