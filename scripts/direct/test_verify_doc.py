#!/usr/bin/env python3
"""Exercise doc verification against real Git trees and fake Cargo."""
from __future__ import annotations

import json
from pathlib import Path
from typing import cast
import unittest

from scripts.direct.verify_doc_test_support import DocPassRecord, VerifyDocFixture


class VerifyDocTests(VerifyDocFixture):
    def test_doc_records_the_complete_judged_tree_and_preserves_the_index(self) -> None:
        source = self.repo / "alpha/src/lib.rs"
        _ = source.write_text("pub fn staged() {}\n", encoding="utf-8")
        _ = self.git("add", "alpha/src/lib.rs")
        expected_tree = self.git("write-tree")
        _ = source.write_text("pub fn unstaged() {}\n", encoding="utf-8")
        other = self.repo / "beta/src/lib.rs"
        _ = other.write_text("pub fn unrelated_unstaged() {}\n", encoding="utf-8")
        _ = (self.repo / "notes.md").write_text("untracked plan edit\n", encoding="utf-8")
        _ = (self.repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        _ = (self.repo / "ignored.txt").write_text("ignored\n", encoding="utf-8")
        index_before = (self.repo / ".git/index").read_bytes()
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.repo / ".git/index").read_bytes(), index_before)
        self.assertEqual(source.read_text(encoding="utf-8"), "pub fn unstaged() {}\n")
        self.assertEqual(other.read_text(encoding="utf-8"), "pub fn unrelated_unstaged() {}\n")
        records = self.pass_records()
        self.assertEqual(len(records), 1, result.stdout + result.stderr)
        record = cast(DocPassRecord, json.loads(records[0].read_text(encoding="utf-8")))
        self.assertEqual(record["repo_top"], str(self.repo))
        self.assertEqual(record["tree_id"], expected_tree)
        self.assertIn(expected_tree, records[0].name)
        self.assertTrue(record["time"])
        calls = self.cargo_calls()
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(Path(calls[0]["cwd"]).resolve(), self.repo.resolve())
        self.assertEqual(calls[0]["sources"], {
            "alpha/src/lib.rs": "pub fn staged() {}\n", "beta/src/lib.rs": "pub fn value() {}\n",
        })
        self.assertFalse(calls[0]["notes_present"])
        self.assertEqual(record["call_id"], calls[0]["call_id"])
        self.assertTrue(record["call_id"])
        self.assertIn("--workspace", calls[0]["args"])
        self.assertIn("--no-deps", calls[0]["args"])
        self.assertNotIn("-p", calls[0]["args"])
        self.assertIn("-D warnings", calls[0]["rustdocflags"])


    def test_doc_failure_leaves_no_durable_pass(self) -> None:
        self.environment["TEST_DOC_STATUS"] = "41"
        result = self.verify()
        self.assertEqual(result.returncode, 41, result.stdout + result.stderr)
        self.assertEqual(self.pass_records(), [])


    def test_doc_off_still_runs_the_workspace_check_and_explains_once(self) -> None:
        _ = self.config.write_text("doc=off\nsweep=off\n", encoding="utf-8")
        result = self.verify("--features", "alpha/extra")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.cargo_calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("--workspace", calls[0]["args"])
        features_at = calls[0]["args"].index("--features")
        self.assertEqual(calls[0]["args"][features_at + 1], "alpha/extra")
        notices = [line for line in (result.stdout + result.stderr).splitlines()
                   if "cannot be switched off" in line]
        self.assertEqual(len(notices), 1, result.stdout + result.stderr)
        self.assertEqual(len(self.pass_records()), 1)



if __name__ == "__main__":
    _ = unittest.main()
