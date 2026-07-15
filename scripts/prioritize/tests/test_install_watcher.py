from __future__ import annotations

import unittest
from pathlib import Path


PRIORITIZE_DIR = Path(__file__).parents[1]
INSTALL_WATCHER = PRIORITIZE_DIR / "install_watcher.sh"


class InstallWatcherContractTests(unittest.TestCase):
    def test_installer_accepts_a_canonical_incomplete_backlog(self) -> None:
        content = INSTALL_WATCHER.read_text(encoding="utf-8")

        self.assertNotIn("--require-complete", content)
        self.assertIn('"$RENUMBER_TOOL" --check', content)
        self.assertIn("incomplete issues remain unranked", content)
        self.assertIn("INITIAL_PASS_ATTEMPTS=120", content)


if __name__ == "__main__":
    unittest.main()
