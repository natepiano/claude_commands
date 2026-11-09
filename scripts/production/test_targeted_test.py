"""Targeted tests select affected modules through the shared real runner."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import ClassVar, override
from unittest import mock

from scripts.production import merge_gate
from scripts.production.affected_modules import NoCertifiedBase
from scripts.production.merge_gate_test_support import MergeGateFixture, SUITE_MODULE, gate_suites
from scripts.lint.memory_test_support import write_config_fixture


class TargetedTestTests(MergeGateFixture):
    suite_mode: ClassVar[merge_gate.SuiteMode] = merge_gate.SuiteMode.TARGETED_TEST

    @override
    def setUp(self) -> None:
        super().setUp()
        # The fixture's main branch has no merge gate pass.
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "merge_gate.conf",
                                 "landing_branches=fixture-landing\n")

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
            self.assertNotIn("tests reused from", result.stdout)
        self.assertEqual([record["kind"] for record in self.pass_records()], ["full", "affected", "affected"])
        self.assertEqual(self.gate_run_records()[-1]["step"], "targeted-test")

    @gate_suites("lib", "hooks", "message")
    def test_a_nightly_red_marker_keeps_selection_by_the_four_rules(self) -> None:
        subject = self.checkout / "scripts/hooks/affected_subject.py"
        self.write(subject, "VALUE = 1\n")
        self.write(self.checkout / "scripts/hooks/test_first.py", SUITE_MODULE + "\n# affected_subject\n")
        self.write(self.checkout / "scripts/lib/test_first.py", "import scripts.hooks.affected_subject\n" + SUITE_MODULE)
        self.write(self.checkout / "scripts/message/test_first.py", 'SUBJECT = "assets/changed.txt"\n' + SUITE_MODULE)
        self.write(self.checkout / "scripts/message/test_unrelated.py", SUITE_MODULE)
        self.write(self.checkout / "assets/changed.txt", "before\n")
        base = self.commit(self.checkout, "four selection routes")
        first = self.run_gate()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        marker, notice = self.write_nightly_red_marker()
        self.write(subject, "VALUE = 2\n")
        self.write(self.checkout / "assets/changed.txt", "after\n")
        self.write(self.checkout / "scripts/lib/test_second.py", SUITE_MODULE + "\n# Changed test module.\n")
        _ = self.commit(self.checkout, "change each selection input")
        self.clear_gate_events()
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        starts = [(row["suite"], row["module"]) for row in self.module_events() if row["event"] == "start"]
        self.assertCountEqual(starts, [("hooks", "test_first.py"), ("lib", "test_first.py"),
                                       ("lib", "test_second.py"), ("message", "test_first.py")])
        self.assertIn(f"affected: 4 of 5 modules (base {base[:12]}, ", result.stdout)
        self.assertNotIn("full suite:", result.stdout)
        self.assertEqual(marker.read_text(), notice)
        self.assertEqual(self.pass_records()[-1]["kind"], "affected")

    def test_a_nightly_red_marker_allows_reuse_and_stays_after_an_explicit_full_test(self) -> None:
        first = self.run_gate()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        marker, notice = self.write_nightly_red_marker()
        self.clear_gate_events()
        reused = self.run_gate()
        self.assertEqual(reused.returncode, 0, reused.stdout + reused.stderr)
        self.assertIn("tests reused from", reused.stdout)
        self.assertEqual(self.module_events(), [])
        self.assertEqual(marker.read_text(), notice)
        self.assertEqual(len(self.pass_records()), 1)
        self.assertEqual(self.gate_run_records()[-1]["selection"], "reused")
        explicit = self.run_gate("--full")
        self.assertEqual(explicit.returncode, 0, explicit.stdout + explicit.stderr)
        self.assert_all_modules_started()
        self.assertIn("full suite: --full", explicit.stdout)
        self.assertEqual(marker.read_text(), notice)

    @gate_suites("lib", "hooks", "message")
    def test_a_dirty_path_selects_affected_modules_without_reuse_or_a_pass_record(self) -> None:
        subject, base = self.prepare_affected_subject()
        first = self.run_gate()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        records = self.pass_records()
        self.write(subject, "VALUE = 2\n")
        self.clear_gate_events()
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        starts = [(row["suite"], row["module"]) for row in self.module_events() if row["event"] == "start"]
        self.assertEqual(starts, [("hooks", "test_first.py")])
        self.assertIn(f"affected: 1 of {self.module_count} modules (base {base[:12]}, ", result.stdout)
        self.assertNotIn("full suite:", result.stdout)
        self.assertNotIn("tests reused from", result.stdout)
        self.assertEqual(self.pass_records(), records)
        record = self.gate_run_records()[-1]
        self.assertEqual(record["selection"], "affected")
        self.assertEqual(record["dirty_paths"], ["scripts/hooks/affected_subject.py"])
        self.assertEqual(record["dirty_path_count"], 1)

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
