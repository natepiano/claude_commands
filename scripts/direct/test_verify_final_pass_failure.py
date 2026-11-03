"""Final gate failure behavior with real Git and Bash."""
from __future__ import annotations


import unittest

from scripts.direct.verify_final_pass_test_support import FinalPassFixture


class FinalPassTests(FinalPassFixture):
    def test_failed_final_writes_no_record_and_keeps_tool_status(self) -> None:
        self.environment["FAKE_CHECK_EXIT"] = "42"
        result = self.verify("final")
        self.assertEqual(result.returncode, 42, result.stdout + result.stderr)
        self.assertEqual(self.pass_files(), [])
        self.assertIn("no final pass record was written: final failed (exit 42)", result.stdout)


if __name__ == "__main__":
    _ = unittest.main()
