#!/usr/bin/env python3
"""Exercise doc verification against real Git trees and fake Cargo."""
from __future__ import annotations

import os
from pathlib import Path
import unittest

from scripts.direct.verify_doc_test_support import VerifyDocFixture


class VerifyDocCheckoutTests(VerifyDocFixture):
    def test_doc_reuses_checkout_and_target_preserving_unchanged_mtimes(self) -> None:
        removed = self.repo / "deleted.txt"
        _ = removed.write_text("remove in second tree\n", encoding="utf-8")
        _ = self.git("add", "deleted.txt")
        first = self.verify()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        initial = self.cargo_calls()[0]
        checkout = Path(initial["cwd"])
        self.assertTrue(checkout.is_relative_to(self.root / "target/verify-doc"))
        self.assertEqual(initial["target"], str(self.root / "target"))
        self.assertTrue((checkout / "deleted.txt").is_file())
        # Set a distinct old mtime: this proves preservation without a sleep or
        # dependence on the filesystem's clock resolution between doc runs.
        unchanged = checkout / "beta/src/lib.rs"
        stamp = 1_000_000_000_000_000_000
        os.utime(unchanged, ns=(stamp, stamp))
        _ = (self.repo / "alpha/src/lib.rs").write_text("pub fn next_tree() {}\n", encoding="utf-8")
        _ = self.git("add", "alpha/src/lib.rs")
        removed.unlink()
        _ = self.git("add", "-u", "--", "deleted.txt")
        second = self.verify()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        current = self.cargo_calls()[1]
        self.assertEqual(current["cwd"], initial["cwd"])
        self.assertEqual(current["target"], initial["target"])
        self.assertEqual(unchanged.stat().st_mtime_ns, stamp, second.stdout + second.stderr)
        self.assertEqual(current["sources"]["alpha/src/lib.rs"], "pub fn next_tree() {}\n")
        self.assertFalse((checkout / "deleted.txt").exists())


    def test_doc_target_defaults_to_repo_metadata_and_reuses_checkout(self) -> None:
        del self.environment["CARGO_TARGET_DIR"]
        for run in range(2):
            with self.subTest(run=run):
                result = self.verify()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        first, second = self.cargo_calls()
        self.assertEqual(first["target"], str(self.root / "target"))
        self.assertEqual(second["target"], first["target"])
        self.assertEqual(second["cwd"], first["cwd"])



if __name__ == "__main__":
    _ = unittest.main()
