"""Check the repository's own scripts for worker launch commands."""
from __future__ import annotations

import unittest

from scripts.direct.test_one_launch_path import ROOT, forbidden_launches


class OneLaunchPathTests(unittest.TestCase):
    def test_only_assign_starts_or_follows_workers(self) -> None:
        found = forbidden_launches(ROOT)
        self.assertFalse(found, "Worker launch commands must use assign.sh:\n" + "\n".join(found))


if __name__ == "__main__":
    _ = unittest.main()
