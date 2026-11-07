"""Keep the merge branch when the rename has no branch destination."""

from __future__ import annotations

import unittest
from typing import final

from scripts.production import rename_test_support as support
from scripts.production.rename_test_support import NEW, OLD


@final
class ProductionRenameTests(support.ProductionRenameFixture):
    def test_a_merge_branch_not_named_for_the_production_is_kept(self) -> None:
        _ = self.git("branch", "-m", "merge-main")
        _ = self.git("branch", OLD)
        _ = self.git("push", "origin", "merge-main")
        tip = self.git("rev-parse", "HEAD")
        document = self.doc.read_bytes(), self.doc.stat().st_mtime_ns
        self.assert_success(self.run_rename("apply"))
        self.assertEqual(self.git("branch", "--show-current"), "merge-main")
        self.assertEqual(self.git("for-each-ref", "--format=%(refname)", f"refs/heads/{NEW}"), "")
        self.assertEqual(self.git("ls-remote", "origin", f"refs/heads/{NEW}"), "")
        self.assertEqual(self.git("rev-parse", "HEAD"), tip)
        self.assertEqual(self.git("rev-parse", "merge-main"), self.git("rev-parse", "origin/merge-main"))
        self.assertEqual((self.doc.read_bytes(), self.doc.stat().st_mtime_ns), document)
        self.assertFalse(self.new_doc.exists())
        self.assertEqual(self.git("rev-parse", OLD), tip)
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/old"), f"{tip}\trefs/heads/old")


if __name__ == "__main__":
    _ = unittest.main()
