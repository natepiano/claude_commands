#!/usr/bin/env python3
"""Exercise doc verification against real Git trees and fake Cargo."""
from __future__ import annotations

from pathlib import Path
import unittest

from scripts.direct.verify_doc_test_support import CARGO_STUB, VerifyDocFixture


class VerifyDocRecoveryTests(VerifyDocFixture):
    def test_doc_replaces_a_checkout_with_a_missing_or_damaged_private_index(self) -> None:
        probe = '''
    index = Path(".git/index")
    assert index.is_file()
    damage = os.environ.get("TEST_DOC_INDEX_DAMAGE")
    if damage:
        if damage == "missing":
            index.unlink()
        else:
            index.write_text("damaged index\\n")
        Path("beta/src/lib.rs").write_text("damaged checkout\\n")
'''
        self.executable(self.root / "bin/cargo", CARGO_STUB.replace(
            '    raise SystemExit(int(os.environ.get("TEST_DOC_STATUS", "0")))',
            probe + '    raise SystemExit(int(os.environ.get("TEST_DOC_STATUS", "0")))'))
        for damage in ("missing", "corrupt"):
            with self.subTest(damage=damage):
                self.environment["TEST_DOC_INDEX_DAMAGE"] = damage
                before = self.verify()
                self.assertEqual(before.returncode, 0, before.stdout + before.stderr)
                checkout = Path(self.cargo_calls()[-1]["cwd"])
                self.assertFalse(checkout.exists())
                del self.environment["TEST_DOC_INDEX_DAMAGE"]
                result = self.verify()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                call = self.cargo_calls()[-1]
                self.assertNotEqual(call["cwd"], str(checkout))
                self.assertFalse(Path(call["cwd"]).exists())
                self.assertEqual(call["sources"]["beta/src/lib.rs"], "pub fn value() {}\n")


if __name__ == "__main__":
    _ = unittest.main()
