"""Production rename: a merge branch not named for the production."""

from __future__ import annotations

import unittest
from typing import final

from scripts.production import rename_test_support as support
from scripts.production.rename_test_support import NEW, OLD, UNITS


@final
class ProductionRenameTests(support.ProductionRenameFixture):
    def test_a_merge_branch_not_named_for_the_production_is_kept(self) -> None:
        self.prepare_unit_moves()
        _ = self.git("branch", "-m", "merge-main")
        _ = self.git("branch", OLD)
        retired = "| retired-unit | (retired; use `/unit:report`) | — | old-retired-unit |\n"
        self.write(self.doc, self.doc.read_text().replace("- **Merge branch:** `old`",
                                                        "- **Merge branch:** `merge-main`").replace(
            "\n## Production rules", retired + "\n## Production rules"))
        _ = self.git("add", str(self.doc))
        _ = self.git("commit", "-m", "Declare a different merge branch and a retired unit")
        _ = self.git("push", "origin", "merge-main", OLD)
        preserved = self.git("rev-parse", OLD)
        self.assert_success(self.run_rename("apply"))
        self.assertEqual(self.git("branch", "--show-current"), "merge-main")
        self.assertEqual(self.git("for-each-ref", "--format=%(refname)", f"refs/heads/{NEW}"), "")
        self.assertEqual(self.git("ls-remote", "origin", f"refs/heads/{NEW}"), "")
        self.assertEqual(self.git("rev-parse", "merge-main"), self.git("rev-parse", "origin/merge-main"))
        self.assertIn("- **Merge branch:** `merge-main`", self.new_doc.read_text())
        before = self.snapshot()
        refused = self.run_command("unit", NEW, "retired-unit")
        self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
        self.assertIn("retired", refused.stderr)
        self.assertEqual(self.snapshot(), before)
        for unit in UNITS:
            self.assert_success(self.run_command("unit", NEW, unit))
        tip = self.git("rev-parse", "merge-main")
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/merge-main"), f"{tip}\trefs/heads/merge-main")
        self.assertEqual(self.git("rev-parse", OLD), preserved)
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/old"), f"{preserved}\trefs/heads/old")
        self.assertIn(retired, self.new_doc.read_text())


if __name__ == "__main__":
    _ = unittest.main()
