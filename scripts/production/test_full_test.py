"""Full tests select affected modules through the shared real runner."""
from __future__ import annotations

import json
import os
from typing import ClassVar
from unittest import mock

from scripts.production import merge_gate
from scripts.production.affected_modules import NoCertifiedBase
from scripts.production.merge_gate_test_support import MergeGateFixture, SUITE_MODULE, gate_suites


class FullTestTests(MergeGateFixture):
    suite_mode: ClassVar[merge_gate.SuiteMode] = merge_gate.SuiteMode.FULL_TEST

    def test_a_changed_suite_set_cannot_reuse_or_certify_an_old_full_pass(self) -> None:
        first = self.run_gate()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        original, = self.pass_records()
        self.write(self.checkout / "scripts" / "added_folder" / "test_added.py", SUITE_MODULE)
        _ = self.commit(self.checkout, "new test folder")
        tree = merge_gate.clean_test_tree(self.checkout)
        folders = sorted([*self.suites, "added_folder"])
        ledger = merge_gate.PassedTreeLedger(self.checkout, "tests-only", tree, folders)
        with mock.patch.dict(os.environ, self.environment):
            # The old full pass cannot certify an ancestor after a folder is added.
            self.assertIsInstance(ledger.certified_base(), NoCertifiedBase)
            # A pass from the old gate could have this same tree but omit its new folder.
            original["tree"] = self.git(self.checkout, "rev-parse", "HEAD^{tree}")
            self.write(ledger.path, json.dumps(original) + "\n")
            self.assertFalse(ledger.reuse())
            self.assertIsInstance(ledger.certified_base(), NoCertifiedBase)
        self.clear_gate_events()
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("tests reused from", result.stdout)
        self.assertIn("full suite: no certified base", result.stdout)
        starts = [(row["suite"], row["module"]) for row in self.module_events() if row["event"] == "start"]
        self.assertCountEqual(starts, [("hooks", "test_first.py"), ("added_folder", "test_added.py")])
        self.assertEqual(self.pass_records()[-1]["suites"], folders)

    @gate_suites("lib", "hooks", "message")
    def test_affected_modules_are_reported_run_and_recorded_without_certifying_a_full_pass(self) -> None:
        subject, base = self.prepare_affected_subject()
        first = self.run_gate()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.write(subject, "VALUE = 2\n")
        _ = self.commit(self.checkout, "subject changed")
        for _attempt in range(2):
            self.clear_gate_events()
            result = self.run_gate()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            starts = [(row["suite"], row["module"]) for row in self.module_events() if row["event"] == "start"]
            self.assertEqual(starts, [("hooks", "test_first.py")])
            self.assertIn(f"affected: 1 of {self.module_count} modules (base {base[:12]}, ", result.stdout)
            self.assertIn("), 1 changed files\nhooks/test_first.py\n", result.stdout)
            self.assertIn("message: skipped (not affected)", result.stdout)
            self.assertIn("Tree-scanning modules: 0 (always affected)", result.stdout)
            self.assertNotIn("tests reused from", result.stdout)
        self.assertEqual([record["kind"] for record in self.pass_records()], ["full", "affected", "affected"])
        self.assertEqual(self.gate_run_records()[-1]["step"], "full-test")

    def test_unrelated_change_can_pass_with_zero_selected_modules(self) -> None:
        first = self.run_gate()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.write(self.checkout / "subject.txt", "unrelated document\n")
        _ = self.commit(self.checkout, "documentation changed")
        self.clear_gate_events()
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"affected: 0 of {self.module_count} modules", result.stdout)
        self.assertEqual(self.module_events(), [])
        self.assertEqual(self.pass_records()[-1]["kind"], "affected")
