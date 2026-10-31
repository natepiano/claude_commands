"""Exercise checkpoint merges against disposable repositories and command stubs."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import final

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Importable once merge_checkpoint has put scripts/lib on the path.


from scripts.production.merge_checkpoint_test_support import MergeCheckpointFixture, configure_test_environment


def setUpModule() -> None:
    configure_test_environment()


@final
class MergeCheckpointTests(MergeCheckpointFixture):
    def test_child_handoff_records_the_pushed_merge_in_the_style_ledger(self) -> None:
        _ = self.child_production()
        checkpoint = self.checkpoint()
        self.prepare_handoff_ledger(checkpoint)
        self.assertEqual(self.handoff_ledger_row(checkpoint), ("sent", ""))
        result = self.run_handoff(checkpoint)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        merge = self.git("rev-parse", "HEAD")
        self.assertEqual(self.handoff_ledger_row(checkpoint), ("merged", merge))
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), merge)
        self.assertEqual(len(self.events("send.py")), 1)
        self.assertNotIn("hand-off ledger:", result.stdout)
        duplicate = self.run_handoff(checkpoint)
        self.assertEqual((duplicate.returncode, duplicate.stdout, duplicate.stderr),
                         (0, f"hand-off {checkpoint[:7]} already merged as {merge[:7]}\n", ""))
        self.assertEqual(self.handoff_ledger_row(checkpoint), ("merged", merge))
        self.assertEqual(len(self.events("send.py")), 2)
    def test_child_handoff_ledger_failure_still_pushes_and_acknowledges(self) -> None:
        _ = self.child_production()
        checkpoint = self.checkpoint()
        self.prepare_handoff_ledger(checkpoint, add_handoff=False)
        result = self.run_handoff(checkpoint)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        merge = self.git("rev-parse", "HEAD")
        self.assertEqual(self.git("rev-parse", "HEAD^2"), checkpoint)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), merge)
        self.assertEqual(len(self.events("send.py")), 1)
        self.assertEqual([line for line in result.stdout.splitlines() if line.startswith("hand-off ledger:")],
                         [f"hand-off ledger: stderr: style_ledger: unknown hand-off {checkpoint}"])
    def test_duplicate_child_notice_records_a_ledger_write_missed_at_the_merge(self) -> None:
        _ = self.child_production()
        checkpoint = self.checkpoint()
        self.prepare_handoff_ledger(checkpoint)
        self.env["MERGE_CHECKPOINT_STYLE_LEDGER"] = str(self.bin / "style_ledger.py")
        self.assertEqual(self.run_handoff(checkpoint).returncode, 0)
        merge = self.git("rev-parse", "HEAD")
        self.assertEqual(self.handoff_ledger_row(checkpoint), ("sent", ""))
        self.env["MERGE_CHECKPOINT_STYLE_LEDGER"] = str(Path(__file__).resolve().parents[1] / "style/style_ledger.py")
        result = self.run_handoff(checkpoint)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (0, f"hand-off {checkpoint[:7]} already merged as {merge[:7]}\n", ""))
        self.assertEqual(self.handoff_ledger_row(checkpoint), ("merged", merge))
        self.assertEqual(self.git("rev-parse", "HEAD"), merge)
        self.assertEqual(len(self.events("send.py")), 2)


if __name__ == "__main__":
    _ = unittest.main()
