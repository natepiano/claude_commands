"""Production rename reruns after a failed step."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest
from typing import final

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scripts.production.rename_test_support import ProductionRenameFixture, stop_at_staff_write
from scripts.production.rename_rerun_test_support import final_files, saved_fixture


@final
class ProductionRenameRerunTests(ProductionRenameFixture):
    def test_rename_rerun(self) -> None:
        self.env.update(GIT_AUTHOR_DATE="2026-10-29T12:00:00Z", GIT_COMMITTER_DATE="2026-10-29T12:00:00Z")
        with saved_fixture(self.root) as restore:
            self.assert_success(self.run_rename("apply"))
            expected = self.outcome()
            for after in (False, True):
                with self.subTest(after=after):
                    restore()

                    with stop_at_staff_write("rename", after=after):
                        failed = self.run_rename("apply")
                    self.assertNotEqual(failed.returncode, 0)
                    self.assertIn("stopped at staff step", failed.stderr)
                    self.assert_success(self.run_rename("apply"))
                    self.assertEqual(self.outcome(), expected)
                    finished = self.run_rename("apply")
                    self.assert_success(finished)
                    self.assertIn("unchanged", finished.stdout)
                    self.assertEqual(self.outcome(), expected)

    def outcome(self) -> tuple[dict[str, bytes], str, str, str]:
        return (final_files(self.root), self.git("show-ref"), self.git("status", "--porcelain"),
                self.git("--git-dir", str(self.origin), "show-ref"))


if __name__ == "__main__":
    _ = unittest.main()
