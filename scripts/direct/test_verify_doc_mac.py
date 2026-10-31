#!/usr/bin/env python3
"""Exercise doc verification against real Git trees and fake Cargo."""
from __future__ import annotations

import json
from typing import cast
import unittest

from scripts.direct.verify_doc_test_support import DocPassRecord, VerifyDocFixture


class VerifyDocMacTests(VerifyDocFixture):
    def test_held_mac_doc_judges_staged_snapshot_and_records_pass_without_index_changes(self) -> None:
        self.enable_held_mac_doc()
        source = self.repo / "alpha/src/lib.rs"
        _ = source.write_text("pub fn staged() {}\n")
        _ = self.git("add", "alpha/src/lib.rs")
        tree = self.git("write-tree")
        _ = source.write_text("pub fn unstaged() {}\n")
        _ = (self.repo / "notes.md").write_text("untracked")
        index = (self.repo / ".git/index").read_bytes()
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        call, = self.cargo_calls()
        self.assertEqual(call["sources"]["alpha/src/lib.rs"], "pub fn staged() {}\n")
        self.assertFalse(call["notes_present"])
        self.assertNotEqual(call["cwd"], str(self.repo))
        record_path, = self.pass_records()
        record = cast(DocPassRecord, json.loads(record_path.read_text()))
        self.assertEqual((record["tree_id"], record["repo_top"], record["call_id"]), (tree, str(self.repo), call["call_id"]))
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)
        self.assertEqual(source.read_text(), "pub fn unstaged() {}\n")


    def test_held_mac_doc_failure_or_changed_staged_tree_does_not_record_pass(self) -> None:
        self.enable_held_mac_doc()
        self.environment["TEST_DOC_STATUS"] = "41"
        failed = self.verify()
        self.assertEqual(failed.returncode, 41, failed.stdout + failed.stderr)
        self.assertEqual(self.pass_records(), [])
        self.environment["TEST_DOC_STATUS"] = "0"
        self.environment["TEST_EDIT_DURING_DOC"] = str(self.repo / "alpha/src/lib.rs")
        self.environment["TEST_STAGE_DURING_DOC"] = "1"
        changed = self.verify()
        self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
        self.assertIn("staged tree changed", changed.stdout)
        self.assertEqual(self.pass_records(), [])



if __name__ == "__main__":
    _ = unittest.main()
