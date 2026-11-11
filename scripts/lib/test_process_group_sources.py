"""Check the repository's own scripts for unguarded group signals."""
from __future__ import annotations

from pathlib import Path
import unittest

from scripts.lib.test_process_group import unguarded_group_signals


class ProcessGroupSourceTests(unittest.TestCase):
    def test_scripts_route_every_group_signal_through_the_shared_check(self) -> None:
        self.assertEqual(unguarded_group_signals(Path(__file__).resolve().parents[1]), [])


if __name__ == "__main__":
    _ = unittest.main()
