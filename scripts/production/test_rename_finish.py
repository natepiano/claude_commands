"""Production rename: finish behavior."""

from __future__ import annotations

import json
import unittest
from typing import cast, final

from scripts.production import rename_test_support as support
from scripts.production.rename_test_support import INTERRUPT_UNIT_GIT, NEW, OLD, OVERWRITE_TRUST, UNITS


@final
class ProductionRenameTests(support.ProductionRenameFixture):
    def test_trust_copy_retries_memory_rewrites_and_preserves_the_original_key(self) -> None:
        self.prepare_unit_moves()
        config = self.seed_unit_config(self.home / ".claude", "alpha-unit")
        self.assert_success(self.run_rename("apply"))
        waits = self.root / "trust-waits.json"
        moved = self.run_command("unit", NEW, "alpha-unit", wrapper=OVERWRITE_TRUST,
                                 RENAME_TEST_CONFIG=str(config), RENAME_TEST_OVERWRITES="2",
                                 RENAME_TEST_WAITS=str(waits),
                                 RENAME_TEST_NEW_PATH=str(self.unit_worktree(NEW, "alpha-unit")))
        self.assert_success(moved)
        self.assertEqual(json.loads(waits.read_text()), [2, 2, 2])
        projects = cast(dict[str, dict[str, object]], json.loads(config.read_text()))["projects"]
        self.assertEqual(projects[str(self.unit_worktree(NEW, "alpha-unit"))],
                         projects[str(self.unit_worktree(OLD, "alpha-unit"))])
        self.assert_unit_moved("alpha-unit")

    def test_trust_copy_refuses_after_three_lost_writes_and_can_resume(self) -> None:
        self.prepare_unit_moves()
        config = self.seed_unit_config(self.home / ".claude", "alpha-unit")
        self.assert_success(self.run_rename("apply"))
        waits = self.root / "trust-waits.json"
        refused = self.run_command("unit", NEW, "alpha-unit", wrapper=OVERWRITE_TRUST,
                                   RENAME_TEST_CONFIG=str(config), RENAME_TEST_OVERWRITES="3",
                                   RENAME_TEST_WAITS=str(waits),
                                   RENAME_TEST_NEW_PATH=str(self.unit_worktree(NEW, "alpha-unit")))
        self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
        self.assertIn(str(config), refused.stderr)
        self.assertIn("trust prompt", refused.stderr)
        self.assertEqual(json.loads(waits.read_text()), [2, 2, 2])
        self.assertIs(self.journal()["done"], False)
        self.assertTrue(self.unit_worktree(NEW, "alpha-unit").exists())
        self.assertEqual(self.git("rev-parse", "old-alpha-unit"), self.old_tip)
        self.assert_success(self.run_command("unit", NEW, "alpha-unit"))
        self.assert_unit_moved("alpha-unit")

    def test_finish_refuses_unmoved_units_resumes_deletion_and_stays_done(self) -> None:
        self.prepare_unit_moves()
        self.assert_success(self.run_rename("apply"))
        before = self.snapshot()
        refused = self.run_command("finish", NEW)
        self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
        for unit in UNITS:
            self.assertIn(unit, refused.stderr)
        self.assertEqual(self.snapshot(), before)
        for unit in UNITS:
            self.assert_success(self.run_command("unit", NEW, unit))
        old_merge = self.git("rev-parse", OLD)
        failed = self.run_command("finish", NEW, wrapper=INTERRUPT_UNIT_GIT, RENAME_TEST_REFUSE="finish-delete")
        self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        self.assertIn("simulated finish-delete failure", failed.stderr)
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/old"), "")
        self.assertEqual(self.git("rev-parse", OLD), old_merge)
        self.assertIs(self.journal()["done"], False)
        self.assert_success(self.run_command("finish", NEW))
        self.assertEqual(self.git("for-each-ref", "--format=%(refname)", "refs/heads/old"), "")
        self.assertIs(self.journal()["done"], True)
        before = self.snapshot()
        self.assert_success(self.run_command("finish", NEW))
        self.assert_success(self.run_rename("apply"))
        self.assertEqual(self.snapshot(), before)
        self.assert_success(self.run_command("apply", NEW, "next"))
        archived = cast(dict[str, object], json.loads((self.state / "next/rename-history/new.json").read_text()))
        self.assertIs(archived["done"], True)
        self.assertEqual((archived["old"], archived["new"]), (OLD, NEW))
        current = cast(dict[str, object], json.loads((self.state / "next/rename.json").read_text()))
        self.assertIs(current["done"], False)
        self.assertEqual(len(cast(list[str], current["completed"])), 8)

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
        finished = self.run_command("finish", NEW)
        self.assert_success(finished)
        self.assertIn("kept merge branch merge-main", finished.stdout)
        tip = self.git("rev-parse", "merge-main")
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/merge-main"), f"{tip}\trefs/heads/merge-main")
        self.assertEqual(self.git("rev-parse", OLD), preserved)
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/old"), f"{preserved}\trefs/heads/old")
        self.assertIn(retired, self.new_doc.read_text())
        self.assertIs(self.journal()["done"], True)


if __name__ == "__main__":
    _ = unittest.main()
