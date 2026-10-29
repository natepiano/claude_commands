"""Final gate records behavior with real Git and Bash."""
from __future__ import annotations

import os
from pathlib import Path
import time
import unittest

from scripts.direct.verify_final_pass_test_support import FinalPassFixture


class FinalPassTests(FinalPassFixture):
    def test_write_prunes_old_records_and_query_does_not_write(self) -> None:
        folder = Path(self.environment["VERIFY_STATE_DIR"]) / "final_pass"
        folder.mkdir(parents=True)
        old = folder / ("a" * 40 + ".json")
        _ = old.write_text("{}")
        expired = time.time() - 15 * 24 * 60 * 60
        os.utime(old, (expired, expired))
        result = self.verify("final-pass")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("no final pass record", result.stderr)
        self.assertTrue(old.exists())
        result = self.verify("final")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(old.exists())
        self.assertEqual(len(self.pass_files()), 1)


if __name__ == "__main__":
    _ = unittest.main()
