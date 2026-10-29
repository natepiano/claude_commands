"""Include hook and source-scan regressions in the required helper discovery."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.hooks import test_session_start_nix_shell
from scripts.lib import test_claude_launch_scan


def load_tests(loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None) -> unittest.TestSuite:
    del pattern
    tests.addTests(loader.loadTestsFromModule(test_session_start_nix_shell))
    tests.addTests(loader.loadTestsFromModule(test_claude_launch_scan))
    return tests
