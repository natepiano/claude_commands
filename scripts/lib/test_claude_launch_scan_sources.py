"""Check launch guards in scans of the repository's own scripts."""
from __future__ import annotations

from pathlib import Path
import unittest

from scripts.lib.test_claude_launch_scan import unguarded_launches


class ClaudeLaunchScanTests(unittest.TestCase):
    def test_repository_launches_go_through_nix_env(self) -> None:
        self.assertEqual(unguarded_launches(Path(__file__).resolve().parents[1]), [])


if __name__ == "__main__":
    _ = unittest.main()
