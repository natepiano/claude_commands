#!/usr/bin/env python3
"""Exercise doc verification against real Git trees and fake Cargo."""
from __future__ import annotations

import json
import os
import time
from typing import cast
import unittest

from scripts.direct.verify_doc_test_support import DocPassRecord, VerifyDocFixture


class VerifyDocRecordsTests(VerifyDocFixture):
    def test_edits_during_doc_leave_no_pass_even_without_a_worker_session(self) -> None:
        self.environment["TEST_STAGE_DURING_DOC"] = "1"
        for name in ("alpha/src/lib.rs", "new-plan.md"):
            with self.subTest(path=name):
                self.environment["TEST_EDIT_DURING_DOC"] = str(self.repo / name)
                result = self.verify()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.pass_records(), [], result.stdout + result.stderr)


    def test_unstaged_edits_during_doc_do_not_invalidate_the_staged_verdict(self) -> None:
        staged_tree = self.git("write-tree")
        self.environment["TEST_EDIT_DURING_DOC"] = str(self.repo / "alpha/src/lib.rs")
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        records = self.pass_records()
        self.assertEqual(len(records), 1, result.stdout + result.stderr)
        record = cast(DocPassRecord, json.loads(records[0].read_text(encoding="utf-8")))
        self.assertEqual(record["tree_id"], staged_tree)
        self.assertIn("edited during rustdoc", (self.repo / "alpha/src/lib.rs").read_text(encoding="utf-8"))


    def test_doc_pass_survives_session_cleanup_and_prunes_only_old_records(self) -> None:
        self.records.mkdir(parents=True)
        old = self.records / ("0" * 40 + ".json")
        recent = self.records / ("1" * 40 + ".json")
        for path in (old, recent):
            _ = path.write_text("{}\n", encoding="utf-8")
        stamp = time.time() - 15 * 24 * 60 * 60
        os.utime(old, (stamp, stamp))
        self.environment["UNIT_SESSION_DIR"] = str(self.root / "session")
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        records = [path for path in self.pass_records() if path != recent]
        self.assertEqual(len(records), 1)
        _ = (self.root / "session").rename(self.root / "ended-session")
        self.assertTrue(records[0].exists())



if __name__ == "__main__":
    _ = unittest.main()
