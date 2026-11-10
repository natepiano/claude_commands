#!/usr/bin/env python3
"""Exercise doc verification against real Git trees and fake Cargo."""
from __future__ import annotations

from pathlib import Path
import unittest

from scripts.direct.verify_doc_test_support import CARGO_STUB, VerifyDocFixture


class VerifyDocCheckoutTests(VerifyDocFixture):
    def test_doc_replaces_checkout_and_reuses_target_preserving_unchanged_mtimes(self) -> None:
        stamp = 1_000_000_000_000_000_000
        probe = '''
    if os.environ.get("TEST_DOC_STAMP"):
        stamp = int(os.environ["TEST_DOC_STAMP"])
        os.utime("beta/src/lib.rs", ns=(stamp, stamp))
    with Path(os.environ["TEST_DOC_TIMES"]).open("a") as output:
        output.write(str(Path("beta/src/lib.rs").stat().st_mtime_ns) + "\\n")
    assert Path("deleted.txt").is_file() == (os.environ.get("TEST_DOC_DELETED") == "present")
'''
        self.executable(self.root / "bin/cargo", CARGO_STUB.replace(
            'elif arguments[:1] == ["doc"]:', 'elif arguments[:1] == ["doc"]:' + probe))
        times = self.root / "doc-times"
        self.environment.update({"TEST_DOC_STAMP": str(stamp), "TEST_DOC_TIMES": str(times),
                                 "TEST_DOC_DELETED": "present"})
        removed = self.repo / "deleted.txt"
        _ = removed.write_text("remove in second tree\n", encoding="utf-8")
        _ = self.git("add", "deleted.txt")
        first = self.verify()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        initial = self.cargo_calls()[0]
        checkout = Path(initial["cwd"])
        self.assertTrue(checkout.is_relative_to(self.root / "tmp"))
        self.assertFalse(checkout.exists())
        self.assertEqual(initial["target"], str(self.root / "target"))
        del self.environment["TEST_DOC_STAMP"]
        self.environment["TEST_DOC_DELETED"] = "absent"
        _ = (self.repo / "alpha/src/lib.rs").write_text("pub fn next_tree() {}\n", encoding="utf-8")
        _ = self.git("add", "alpha/src/lib.rs")
        removed.unlink()
        _ = self.git("add", "-u", "--", "deleted.txt")
        second = self.verify()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        current = self.cargo_calls()[1]
        self.assertNotEqual(current["cwd"], initial["cwd"])
        self.assertFalse(Path(current["cwd"]).exists())
        self.assertEqual(current["target"], initial["target"])
        self.assertEqual(times.read_text().splitlines(), [str(stamp), str(stamp)])
        self.assertEqual(current["sources"]["alpha/src/lib.rs"], "pub fn next_tree() {}\n")

    def test_doc_target_defaults_to_repo_metadata_and_reuses_cache(self) -> None:
        del self.environment["CARGO_TARGET_DIR"]
        for run in range(2):
            with self.subTest(run=run):
                result = self.verify()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        first, second = self.cargo_calls()
        self.assertEqual(first["target"], str(self.root / "target"))
        self.assertEqual(second["target"], first["target"])
        self.assertNotEqual(second["cwd"], first["cwd"])
        self.assertFalse(any(Path(call["cwd"]).exists() for call in (first, second)))
        records = list((self.root / "home/worktrees/gates").glob("*/*/.gate.json"))
        self.assertEqual(len(records), 1)
        self.assertTrue((records[0].parent / "target").is_dir())


if __name__ == "__main__":
    _ = unittest.main()
