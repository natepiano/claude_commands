"""Exercise add_unit through its command line with isolated git and launcher state."""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path
from typing import final

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shutdown"))
import add_unit


from add_unit_test_support import AddUnitTestCase, configure_test_zone


def setUpModule() -> None:
    configure_test_zone()


@final
class AddUnitRestoreTests(AddUnitTestCase):
    def test_a_name_ending_in_unit_launches_as_the_name_without_it(self) -> None:
        def request(name: str) -> tuple[str, str, Path, str]:
            launched = add_unit.launch_request(argparse.Namespace(
                production=self.production_name, name=name, plan="docs/plans/given.md", brief=None,
                standby=False, permanent=False, port=None, timeout=90.0, check=False,
                resume=None, cwd=None, restart_note=None, session_name=None, host_session=None, probe=False,
            ))
            return launched.unit, launched.branch, launched.worktree, launched.requested_name

        expected = request("feature-log")
        self.assertEqual(expected[0], "feature-log-unit")
        self.assertEqual(request("feature-log-unit"), expected)
        self.assertEqual(request("feature_log_unit"), request("feature_log"))
        self.assertEqual(request("unit")[0], "unit-unit")





if __name__ == "__main__":
    _ = unittest.main()
