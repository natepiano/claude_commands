"""Final gate edits checks."""
from __future__ import annotations


import unittest

from scripts.direct.verify_final_pass_test_support import FinalPassFixture


class FinalPassTests(FinalPassFixture):
    def test_edit_during_final_writes_no_record(self) -> None:
        self.environment["EDIT_DURING_TEST"] = "1"
        result = self.verify("final")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.pass_files(), [])
        self.assertIn("no final pass record was written: the tree is dirty", result.stdout)


if __name__ == "__main__":
    _ = unittest.main()
