"""Exercise checkpoint merges against disposable repositories and command stubs."""

from __future__ import annotations

import argparse
import os
import sys
import unittest
from unittest import mock
from pathlib import Path
from typing import final

sys.path.insert(0, str(Path(__file__).resolve().parent))
from merge_checkpoint import CodeCheckpoint, LastMerged, MergeEntry, NoMerge, ShrinkCommit, merge_branch_history, merge_history, request_from

# Importable once merge_checkpoint has put scripts/lib on the path.


from scripts.production.merge_checkpoint_test_support import DELIVERS, MergeCheckpointFixture, PARALLEL, STARTED, TRIAL, configure_test_environment


def setUpModule() -> None:
    configure_test_environment()


@final
class MergeCheckpointTests(MergeCheckpointFixture):
    def test_shrink_merges_only_plan_and_next_items_after_code_checkpoint(self) -> None:
        code = self.checkpoint()
        first = self.run_checkpoint(code)
        self.assertEqual(first.process.returncode, 0, first.process.stderr + first.process.stdout)
        self.write(self.unit, "docs/plans/alpha.md", "short as built\n")
        self.write(self.unit, "docs/plans/alpha-next.md", "next items\n")
        _ = self.git("add", "docs/plans/alpha.md", "docs/plans/alpha-next.md", cwd=self.unit)
        _ = self.git("commit", "-m", "shrink", cwd=self.unit)
        sha = self.git("rev-parse", "HEAD", cwd=self.unit)
        _ = self.git("push", "origin", "alpha-branch", cwd=self.unit)
        result = self.run_checkpoint(sha, "--shrink", review=False)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        self.assertEqual(len(self.events("review_regime.py")), 1)
        self.assertNotIn("unblocks: G1", result.process.stdout)
        self.assertIn("docs/plans/alpha-next.md", self.git("ls-tree", "-r", "--name-only", "HEAD"))
    def test_shrink_after_criss_cross_merge_scopes_only_its_doc(self) -> None:
        code = self.checkpoint()
        self.write(self.checkout, "src/beta.txt", "beta on the merge branch\n")
        _ = self.git("commit", "-am", "beta lands first")
        _ = self.git("push", "origin", "production")
        _ = self.git("merge", "--no-ff", "-m", "merge production", "production", cwd=self.unit)
        _ = self.git("push", "origin", "alpha-branch", cwd=self.unit)
        first = self.run_checkpoint(code)
        self.assertEqual(first.process.returncode, 0, first.process.stderr + first.process.stdout)
        shrink = self.checkpoint("docs/plans/alpha.md", "short as built\n", subject="shrink")
        bases = self.git("merge-base", "--all", "production", shrink).splitlines()
        self.assertEqual(len(bases), 2, bases)
        result = self.run_checkpoint(shrink, "--shrink", review=False)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        self.assertIn("scope: ok — 1 changed paths are allowed", result.process.stdout)
    def test_as_built_merges_the_docs_and_the_plans_deletion_after_code_checkpoint(self) -> None:
        first = self.run_checkpoint(self.checkpoint())
        self.assertEqual(first.process.returncode, 0, first.process.stderr + first.process.stdout)
        result = self.run_checkpoint(self.as_built_commit(), "--as-built", review=False)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        self.assertIn("test: ok — as-built has no package tests", result.process.stdout)
        tree = self.git("ls-tree", "-r", "--name-only", "HEAD")
        self.assertIn("docs/as-built/alpha.md", tree)
        self.assertNotIn("docs/plans/alpha.md", tree)
        self.assertRegex(self.git("log", "-1", "--format=%s"), r"^Merge alpha-unit phase \S+ as-built \(")
    def test_as_built_holds_a_commit_that_changes_code(self) -> None:
        first = self.run_checkpoint(self.checkpoint())
        self.assertEqual(first.process.returncode, 0, first.process.stderr + first.process.stdout)
        result = self.run_checkpoint(self.as_built_commit("src/alpha.rs"), "--as-built", review=False)
        self.assertNotEqual(result.process.returncode, 0)
        self.assertIn("an as-built commit carries only docs", result.process.stdout)
    def test_reruns_report_their_own_code_and_shrink_merge_hashes(self) -> None:
        code = self.checkpoint()
        first = self.run_checkpoint(code)
        self.assertEqual(first.process.returncode, 0, first.process.stderr + first.process.stdout)
        code_merge = self.git("rev-parse", "HEAD")
        shrink = self.checkpoint("docs/plans/alpha.md", "short as built\n", subject="shrink")
        shrink_result = self.run_checkpoint(shrink, "--shrink", review=False, delivers=None)
        self.assertEqual(shrink_result.process.returncode, 0,
                         shrink_result.process.stderr + shrink_result.process.stdout)
        self.assertIn("\n\nPhase plan shrink.\n\n", self.git("log", "-1", "--format=%B"))
        shrink_merge = self.git("rev-parse", "HEAD")
        self.assertNotEqual(code_merge, shrink_merge)
        code_rerun = self.run_checkpoint(code)
        self.assertEqual(code_rerun.process.returncode, 0,
                         code_rerun.process.stderr + code_rerun.process.stdout)
        self.assertIn(f"send alpha-unit: phase 2 merged and pushed as {code_merge}", code_rerun.process.stdout)
        shrink_rerun = self.run_checkpoint(shrink, "--shrink", review=False, delivers=None)
        self.assertEqual(shrink_rerun.process.returncode, 0,
                         shrink_rerun.process.stderr + shrink_rerun.process.stdout)
        self.assertIn(f"send alpha-unit: phase 2 merged and pushed as {shrink_merge}",
                      shrink_rerun.process.stdout)
    def test_shrink_holds_before_code_checkpoint(self) -> None:
        sha = self.checkpoint("docs/plans/alpha.md", "short as built\n")
        early = self.run_checkpoint(sha, "--shrink", review=False)
        self.assert_stopped_before_merge(early, "held", "phase 2")
    def test_shrink_holds_with_unrelated_staged_path(self) -> None:
        code = self.checkpoint(content="actual code\n")
        self.assertEqual(self.run_checkpoint(code).process.returncode, 0)
        shrink = self.checkpoint("docs/plans/alpha.md", "short as built\n")
        self.write(self.checkout, "unrelated.txt", "staged\n")
        _ = self.git("add", "unrelated.txt")
        before = self.git("rev-parse", "HEAD")
        staged = self.run_checkpoint(shrink, "--shrink", review=False)
        self.assertIn("scope: held", staged.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "unrelated.txt")
    def test_merge_branch_reader_reports_no_merge_before_checkpoints(self) -> None:
        history = merge_branch_history(self.checkout, "production")
        self.assertIsInstance(history.last_merge(), NoMerge)
        self.assertIsInstance(history.last_for_unit("alpha-unit"), NoMerge)
        self.assertIsInstance(history.last_code_for_unit("alpha-unit"), NoMerge)
        self.assertEqual(history.code_merge_count(), 0)
        self.assertFalse(history.has_code_merge("alpha-unit", "2"))
    def test_merge_branch_reader_ignores_checkpoint_subject_on_merged_side_branch(self) -> None:
        _ = self.checkpoint(subject="Merge alpha-unit phase 9 (abcdef0) into production")
        _ = self.git("merge", "--no-ff", "-m", "Bring side branch into production", "alpha-branch")

        history = merge_branch_history(self.checkout, "production")
        self.assertIsInstance(history.last_merge(), NoMerge)
        self.assertIsInstance(history.last_for_unit("alpha-unit"), NoMerge)
        self.assertIsInstance(history.last_code_for_unit("alpha-unit"), NoMerge)
        self.assertEqual(history.code_merge_count(), 0)
        self.assertFalse(history.has_code_merge("alpha-unit", "9"))
    def test_merge_reader_keeps_subject_from_earlier_branch_name(self) -> None:
        code = self.checkpoint()
        _ = self.git("merge", "--no-ff", "-m",
                     f"Merge alpha-unit phase 2 ({code[:7]}) into old-name", "alpha-branch")
        history = merge_branch_history(self.checkout, "production")
        entry = history.last_code_for_unit("alpha-unit")
        self.assertNotIsInstance(entry, NoMerge)
        if isinstance(entry, NoMerge):
            self.fail("merge under earlier branch name was not read")
        self.assertEqual((entry.unit, entry.phase, entry.short), ("alpha-unit", "2", code[:7]))
        self.assertIsInstance(entry.kind, CodeCheckpoint)
        self.assertTrue(history.has_code_merge("alpha-unit", "2"))
    def test_merge_history_uses_code_checkpoint_after_shrink(self) -> None:
        code = self.checkpoint()
        _ = self.git("merge", "--no-ff", "-m",
                     f"Merge alpha-unit phase 2 ({code[:7]}) into production", "alpha-branch")
        code_merge = self.git("rev-parse", "HEAD")
        shrink = self.checkpoint("docs/plans/alpha.md", "short as built\n", subject="shrink")
        _ = self.git("merge", "--no-ff", "-m",
                     f"Merge alpha-unit phase 2 shrink ({shrink[:7]}) into production", "alpha-branch")
        shrink_merge = self.git("rev-parse", "HEAD")
        history = merge_branch_history(self.checkout, "production")
        last_any = history.last_for_unit("alpha-unit")
        last_code = history.last_code_for_unit("alpha-unit")
        self.assertNotIsInstance(last_any, NoMerge)
        self.assertNotIsInstance(last_code, NoMerge)
        if isinstance(last_any, NoMerge) or isinstance(last_code, NoMerge):
            self.fail("checkpoint history was not read")
        self.assertEqual(last_any.merge_hash, shrink_merge)
        self.assertIsInstance(last_any.kind, ShrinkCommit)
        self.assertEqual(last_code.merge_hash, code_merge)
        self.assertIsInstance(last_code.kind, CodeCheckpoint)
        with mock.patch.dict(os.environ, {"PATH": self.env["PATH"]}):  # Merge tests names a stub on the test PATH
            request = request_from(argparse.Namespace(
                production=str(self.doc), unit="alpha-unit", phase="2", hash=code,
                also=[], shrink=False, as_built=False, trailer=[], review_trial=TRIAL, delivers=DELIVERS, parallel=PARALLEL,
                cancel_prior=False, started=STARTED, regime="after", holds=0,
                merge_defects=0, excluded=None, scratch=str(self.root / "scratch"),
            ))
        latest = merge_history(request)
        self.assertIsInstance(latest, LastMerged)
        if isinstance(latest, LastMerged):
            self.assertEqual(latest.hash, code)
    def test_merge_history_follows_a_unit_rename_commit(self) -> None:
        code = self.checkpoint()
        _ = self.git("merge", "--no-ff", "-m",
                     f"Merge old-alpha-unit phase 2 ({code[:7]}) into production", "alpha-branch")
        _ = self.git("commit", "--allow-empty", "-m", "chore(example): rename unit old-alpha-unit to alpha-unit")
        history = merge_branch_history(self.checkout, "production")
        entry = history.last_code_for_unit("alpha-unit")
        if isinstance(entry, NoMerge):
            self.fail("the merge before the rename was not carried to the new name")
        self.assertEqual((entry.unit, entry.phase, entry.short), ("alpha-unit", "2", code[:7]))
        self.assertTrue(history.has_code_merge("alpha-unit", "2"))
        self.assertIsInstance(history.last_for_unit("old-alpha-unit"), NoMerge)
        with mock.patch.dict(os.environ, {"PATH": self.env["PATH"]}):  # Merge tests names a stub on the test PATH
            request = request_from(argparse.Namespace(
                production=str(self.doc), unit="alpha-unit", phase="3", hash=code,
                also=[], shrink=False, as_built=False, trailer=[], review_trial=TRIAL, delivers=DELIVERS, parallel=PARALLEL,
                cancel_prior=False, started=STARTED, regime="after", holds=0,
                merge_defects=0, excluded=None, scratch=str(self.root / "scratch"),
            ))
        latest = merge_history(request)
        if not isinstance(latest, LastMerged):
            self.fail("a renamed unit read as never merged, so its first-merge gate would fire again")
        self.assertEqual(latest.hash, code)
    def test_merge_history_follows_a_rename_chain_and_keeps_a_reused_name(self) -> None:
        for subject in (
            "Merge a-unit phase 1 (aaaaaaa) into production",
            "chore(example): rename unit a-unit to b-unit",
            "Merge b-unit phase 2 (bbbbbbb) into production",
            "chore(example): rename unit b-unit to c-unit",
            "Merge a-unit phase 1 (ccccccc) into production",
        ):
            _ = self.git("commit", "--allow-empty", "-m", subject)
        history = merge_branch_history(self.checkout, "production")
        self.assertEqual([(entry.unit, entry.short) for entry in history.entries if isinstance(entry, MergeEntry)],
                         [("a-unit", "ccccccc"), ("c-unit", "bbbbbbb"), ("c-unit", "aaaaaaa")])
        self.assertTrue(history.has_code_merge("c-unit", "1"))
        self.assertIsInstance(history.last_for_unit("b-unit"), NoMerge)
        self.assertEqual(history.code_merge_count(), 3)
    def test_merge_branch_reader_queries_last_any_last_unit_and_code_count(self) -> None:
        alpha_code = self.checkpoint(content="alpha phase two\n")
        _ = self.git("merge", "--no-ff", "-m",
                     f"Merge alpha-unit phase 2 ({alpha_code[:7]}) into production", "alpha-branch")
        alpha_merge = self.git("rev-parse", "HEAD")
        self.write(self.other, "src/beta.txt", "beta phase one\n")
        _ = self.git("add", "src/beta.txt", cwd=self.other)
        _ = self.git("commit", "-m", "beta phase one", cwd=self.other)
        beta_code = self.git("rev-parse", "HEAD", cwd=self.other)
        _ = self.git("merge", "--no-ff", "-m",
                     f"Merge beta-unit phase 1 ({beta_code[:7]}) into production", "beta-branch")
        beta_merge = self.git("rev-parse", "HEAD")
        shrink = self.checkpoint("docs/plans/alpha.md", "short as built\n", subject="shrink")
        _ = self.git("merge", "--no-ff", "-m",
                     f"Merge alpha-unit phase 2 shrink ({shrink[:7]}) into production", "alpha-branch")
        shrink_merge = self.git("rev-parse", "HEAD")

        history = merge_branch_history(self.checkout, "production")
        latest = history.last_merge()
        self.assertNotIsInstance(latest, NoMerge)
        if not isinstance(latest, MergeEntry):
            self.fail("shrink merge was not read")
        self.assertEqual((latest.unit, latest.phase, latest.short, latest.merge_hash),
                         ("alpha-unit", "2", shrink[:7], shrink_merge))
        self.assertIsInstance(latest.kind, ShrinkCommit)
        alpha = history.last_for_unit("alpha-unit")
        self.assertEqual(alpha, latest)
        alpha_code = history.last_code_for_unit("alpha-unit")
        self.assertNotIsInstance(alpha_code, NoMerge)
        if isinstance(alpha_code, NoMerge):
            self.fail("alpha code merge was not read")
        self.assertEqual(alpha_code.merge_hash, alpha_merge)
        beta = history.last_for_unit("beta-unit")
        self.assertNotIsInstance(beta, NoMerge)
        if isinstance(beta, NoMerge):
            self.fail("beta merge was not read")
        self.assertEqual((beta.unit, beta.merge_hash), ("beta-unit", beta_merge))
        self.assertIsInstance(beta.kind, CodeCheckpoint)
        self.assertEqual(history.code_merge_count(), 2)
        self.assertTrue(history.has_code_merge("alpha-unit", "2"))
        self.assertTrue(history.has_code_merge("beta-unit", "1"))
        self.assertFalse(history.has_code_merge("alpha-unit", "3"))
        self.assertEqual(history.entries[-1].merge_hash, alpha_merge)


if __name__ == "__main__":
    _ = unittest.main()
