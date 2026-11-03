#!/usr/bin/env python3
"""Exercise doc verification against real Git trees and fake Cargo."""
from __future__ import annotations

from pathlib import Path
import unittest

from scripts.direct.verify_doc_test_support import VerifyDocFixture


class VerifyDocRecoveryTests(VerifyDocFixture):
    def test_doc_recovers_a_missing_or_damaged_private_index(self) -> None:
        for damage in ("missing", "corrupt"):
            with self.subTest(damage=damage):
                before = self.verify()
                self.assertEqual(before.returncode, 0, before.stdout + before.stderr)
                checkout = Path(self.cargo_calls()[-1]["cwd"])
                index = checkout.parent / "index"
                self.assertTrue(index.is_file())
                if damage == "missing":
                    index.unlink()
                else:
                    _ = index.write_text("damaged index\n", encoding="utf-8")
                _ = (checkout / "beta/src/lib.rs").write_text("damaged checkout\n", encoding="utf-8")
                result = self.verify()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                call = self.cargo_calls()[-1]
                self.assertEqual(call["cwd"], str(checkout))
                self.assertEqual(call["sources"]["beta/src/lib.rs"], "pub fn value() {}\n")



if __name__ == "__main__":
    _ = unittest.main()
