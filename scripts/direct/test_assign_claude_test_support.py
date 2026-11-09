"""Check output capture through the launcher helper adapter."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import cast
import unittest

from scripts.direct.assign_claude_test_support import DIRECT_DIR, direct_launcher_helpers
from scripts.direct.recorder_fixture import launcher_environment
from scripts.lint.memory_test_support import config_environment


class DirectLauncherHelperTests(unittest.TestCase):
    def test_helper_capture_matches_the_cli_for_separate_and_merged_output(self) -> None:
        modes: tuple[dict[str, object], ...] = (
            {"capture_output": True},
            {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE},
            {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT},
        )
        real_run = cast(Callable[..., subprocess.CompletedProcess[str]], subprocess.run)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = {**launcher_environment(root), **config_environment(root)}
            for argument in ("--help", "--invalid-command"):
                for options in modes:
                    with self.subTest(argument=argument, options=options):
                        command = [sys.executable, str(DIRECT_DIR / "progress_history.py"), argument]
                        expected = real_run(command, env=environment, text=True, check=False, **options)
                        with direct_launcher_helpers():
                            adapted_run = cast(Callable[..., subprocess.CompletedProcess[str]], subprocess.run)
                            actual = adapted_run(command, env=environment, text=True, check=False, **options)
                        self.assertEqual((actual.returncode, actual.stdout, actual.stderr),
                                         (expected.returncode, expected.stdout, expected.stderr))
