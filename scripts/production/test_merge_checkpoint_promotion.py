"""Exercise checkpoint merges against disposable repositories and command stubs."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from io import StringIO
import os
import re
import sys
import unittest
from unittest import mock
from pathlib import Path
from typing import final

sys.path.insert(0, str(Path(__file__).resolve().parent))
from merge_checkpoint import promote, request_from

# Importable once merge_checkpoint has put scripts/lib on the path.


from scripts.production.merge_checkpoint_test_support import DELIVERS, MergeCheckpointFixture, PARALLEL, STARTED, TRIAL, configure_test_environment


def setUpModule() -> None:
    configure_test_environment()


@final
class MergeCheckpointTests(MergeCheckpointFixture):
    def test_rerun_after_origin_main_moves_skips_merge_branch_push_and_promotes_pushed_tip(self) -> None:
        self.promotions = str(self.promote)
        self.production_doc()
        sha = self.checkpoint()
        first = self.run_checkpoint(sha)
        self.assertEqual(first.process.returncode, 0, first.process.stderr + first.process.stdout)
        pushed = self.git("rev-parse", "refs/heads/production", cwd=self.origin)
        head = self.git("rev-parse", "HEAD")
        writer = self.root / "main-writer"
        _ = self.git("clone", "-b", "main", str(self.origin), str(writer), cwd=self.root)
        _ = self.git("config", "user.name", "Checkpoint Test", cwd=writer)
        _ = self.git("config", "user.email", "checkpoint@example.invalid", cwd=writer)
        self.write(writer, "later-main.txt", "main moved after push\n")
        _ = self.git("add", "later-main.txt", cwd=writer)
        _ = self.git("commit", "-m", "later main", cwd=writer)
        _ = self.git("push", "origin", "main", cwd=writer)
        rerun = self.run_checkpoint(sha)
        self.assertEqual(rerun.process.returncode, 0, rerun.process.stderr + rerun.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), pushed)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), pushed)
        self.assertIn(f"already pushed at {pushed}", rerun.process.stdout)
    def test_retry_pushes_main_after_initial_promotion_push_rejection(self) -> None:
        self.promotions = str(self.promote) + " — live checkout"
        self.production_doc()
        marker = self.root / "reject-main"
        _ = marker.touch()
        hook = ("#!/bin/sh\nwhile read old new ref; do\n"
                + f'  if [ "$ref" = "refs/heads/main" ] && [ -e "{marker}" ]; then exit 1; fi\n'
                + "done\nexit 0\n")
        self.executable(self.origin / "hooks/pre-receive", hook)
        sha = self.checkpoint()
        failed = self.run_checkpoint(sha)
        self.assertEqual(failed.process.returncode, 1)
        self.assertIn("promote: failed", failed.process.stdout)
        pushed = self.git("rev-parse", "refs/heads/production", cwd=self.origin)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), pushed)
        self.assertNotEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin), pushed)
        marker.unlink()
        retried = self.run_checkpoint(sha)
        self.assertEqual(retried.process.returncode, 0, retried.process.stderr + retried.process.stdout)
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin), pushed)
        self.assertIn(f"already pushed at {pushed}", retried.process.stdout)
        self.assertIn(f"promote: ok — {self.promote} already at {pushed}\n", retried.process.stdout)
    def test_local_foreign_main_commit_never_reaches_origin(self) -> None:
        self.promotions = str(self.promote)
        self.production_doc()
        self.write(self.promote, "foreign-main.txt", "another session\n")
        _ = self.git("add", "foreign-main.txt", cwd=self.promote)
        _ = self.git("commit", "-m", "foreign main commit", cwd=self.promote)
        origin_main = self.git("rev-parse", "refs/heads/main", cwd=self.origin)
        sha = self.checkpoint()
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 1)
        self.assertIn("promote: failed", result.process.stdout)
        self.assertIn(str(self.promote), result.process.stdout)
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin), origin_main)
    def test_rerun_never_pushes_a_main_that_holds_a_foreign_commit(self) -> None:
        self.promotions = str(self.promote)
        self.production_doc()
        sha = self.checkpoint()
        first = self.run_checkpoint(sha)
        self.assertEqual(first.process.returncode, 0, first.process.stderr + first.process.stdout)
        pushed = self.git("rev-parse", "refs/heads/main", cwd=self.origin)
        self.write(self.promote, "foreign-main.txt", "another session\n")
        _ = self.git("add", "foreign-main.txt", cwd=self.promote)
        _ = self.git("commit", "-m", "foreign main commit", cwd=self.promote)
        rerun = self.run_checkpoint(sha)
        self.assertEqual(rerun.process.returncode, 0, rerun.process.stderr + rerun.process.stdout)
        self.assertIn(f"promote: ok — {self.promote} main at {pushed}; "
                      + "main holds commits not on origin/production and was not pushed\n",
                      rerun.process.stdout)
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin), pushed)
    def test_promote_at_tip_with_foreign_remote_history_says_already_at_without_push(self) -> None:
        self.promotions = str(self.promote)
        self.production_doc()
        sha = self.checkpoint()
        first = self.run_checkpoint(sha)
        self.assertEqual(first.process.returncode, 0, first.process.stderr + first.process.stdout)
        tip = self.git("rev-parse", "HEAD")
        base = self.git("rev-parse", "HEAD^1")
        _ = self.git("push", "--force", "origin", f"{base}:refs/heads/main")
        _ = self.git("push", "--force", "origin", f"{base}:refs/heads/production")
        with mock.patch.dict(os.environ, {"PATH": self.env["PATH"]}):  # Merge tests names a stub on the test PATH
            request = request_from(argparse.Namespace(
                production=str(self.doc), unit="alpha-unit", phase="2", hash=sha,
                also=[], shrink=False, as_built=False, trailer=[], review_trial=TRIAL, delivers=DELIVERS, parallel=PARALLEL,
                cancel_prior=False, started=STARTED, regime="after", holds=0,
                merge_defects=0, excluded=None, scratch=str(self.root / "scratch"),
            ))
        output = StringIO()
        with redirect_stdout(output):
            promote(request, tip)
        self.assertIn(f"promote: ok — {self.promote} already at {tip}; "
                      + "main holds commits not on origin/production and was not pushed\n", output.getvalue())
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin), base)
    def test_promote_reports_already_at_tip_when_both_mains_match(self) -> None:
        self.promotions = str(self.promote)
        self.production_doc()
        sha = self.checkpoint()
        first = self.run_checkpoint(sha)
        self.assertEqual(first.process.returncode, 0, first.process.stderr + first.process.stdout)
        tip = self.git("rev-parse", "refs/heads/main", cwd=self.origin)
        second = self.run_checkpoint(sha)
        self.assertEqual(second.process.returncode, 0, second.process.stderr + second.process.stdout)
        self.assertIn(f"promote: ok — {self.promote} already at {tip}\n", second.process.stdout)
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin), tip)
    def test_git_push_merges_diverged_main_promotes_pushed_tip_and_records(self) -> None:
        self.promotions = str(self.promote) + ", mac " + str(self.root / "mac-installed")
        self.production_doc()
        sha = self.checkpoint()
        main_writer = self.root / "main-writer"
        _ = self.git("clone", "-b", "main", str(self.origin), str(main_writer), cwd=self.root)
        _ = self.git("config", "user.name", "Checkpoint Test", cwd=main_writer)
        _ = self.git("config", "user.email", "checkpoint@example.invalid", cwd=main_writer)
        self.write(main_writer, "main-only.txt", "another session\n")
        _ = self.git("add", "main-only.txt", cwd=main_writer)
        _ = self.git("commit", "-m", "main moved", cwd=main_writer)
        _ = self.git("push", "origin", "main", cwd=main_writer)
        result = self.run_checkpoint(sha, "--holds", "2", "--merge-defects", "1",
                                     "--regime", "trial", "--excluded", "no merge design check")
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        tip = self.git("rev-parse", "HEAD")
        self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), tip)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), tip)
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin), tip)
        self.assertIn(f"promote: ok — {self.promote} main at {tip}\n", result.process.stdout)
        self.assertIn(f"promote: ok — mac {self.root / 'mac-installed'} main pulled\n",
                      result.process.stdout)
        self.assertEqual(self.git("merge-base", "--is-ancestor", sha, tip), "")
        self.assertIn("main-only.txt", self.git("ls-tree", "-r", "--name-only", tip))
        receipts = self.receipts()
        self.assertEqual({row["merge_commit"] for row in receipts},
                         set(self.git("rev-list", "--first-parent", "--merges", "HEAD").splitlines()))
        integration, = [row for row in receipts if row["merge_commit"] == tip]
        self.assertEqual(integration["source_checkpoint"], self.git("rev-parse", "HEAD^2"))
        self.assertRegex(result.process.stdout, re.escape(f"into: {self.checkout} (production) — ") + ".*unblocks: G1: beta-unit Phase 4")
        self.assertRegex(result.process.stdout, re.escape(f"into: {self.promote} (main) — ") + ".*unblocks: the live gate of alpha-unit phase 2")
        self.assertIn("the merge branch collects every unit's checkpoints", result.process.stdout)
        self.assertIn("sessions on", result.process.stdout)
        self.assertIn("into: " + str(self.root / "mac-installed"), result.process.stdout)
        self.assertEqual(len(self.events("ssh")), 1)
        review = self.events("review_regime.py")
        self.assertEqual(len(review), 1)
        merged_at = review[0][review[0].index("--merged") + 1]
        self.assertRegex(merged_at, r"^\d{4}-\d\d-\d\dT")
        self.assertEqual(review[0], [
            "add", "--unit", "alpha-unit", "--plan", "docs/plans/alpha.md", "--phase", "2", "--regime", "trial",
            "--started", STARTED, "--merged", merged_at, "--holds", "2",
            "--merge-defects", "1", "--ux-findings", "2", "--code-findings", "3",
            "--review-minutes", "14", "--ux-check-minutes", "4",
            "--ux-repair-minutes", "1", "--excluded", "no merge design check",
        ])
        self.assertRegex(self.log.read_text(), r"- \d\d:\d\d PDT: alpha-unit phase 2 \([0-9a-f]+\) merged as [0-9a-f]+; .*green; pushed; promoted")
        self.assertIn("send alpha-unit:", result.process.stdout)
    def test_promotion_refusal_keeps_pushed_merge_and_resume_reaches_only_missing_place(self) -> None:
        self.promotions = str(self.promote)
        self.gate = False
        self.production_doc()
        sha = self.checkpoint()
        self.write(self.promote, "src/alpha.txt", "local edit\n")
        failed = self.run_checkpoint(sha)
        self.assertIn("failed", failed.process.stdout)
        self.assertIn("src/alpha.txt", failed.process.stdout)
        pushed = self.git("rev-parse", "refs/heads/production", cwd=self.origin)
        self.assertEqual(self.git("rev-parse", "HEAD"), pushed)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), failed.before)
        self.assertIn(f"into: {self.checkout} (production)", failed.process.stdout)
        self.assertIn("unblocks: no gate", failed.process.stdout)
        self.assertNotIn(f"into: {self.promote} (main)", failed.process.stdout)
        _ = self.git("restore", "src/alpha.txt", cwd=self.promote)
        resumed = self.run_checkpoint(sha)
        self.assertEqual(resumed.process.returncode, 0, resumed.process.stderr + resumed.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), pushed)
        self.assertIn(f"into: {self.promote} (main)", resumed.process.stdout)
        self.assertNotIn("unblocks: G1", resumed.process.stdout)
    def test_mac_rc_failure_is_reported_after_local_promotion(self) -> None:
        self.promotions = str(self.promote) + ", mac " + str(self.root / "mac-installed")
        self.production_doc()
        _ = (self.state / "mac-rc").write_text("1\n")
        sha = self.checkpoint()
        result = self.run_checkpoint(sha)
        self.assertIn("failed", result.process.stdout)
        self.assertIn(f"into: {self.checkout} (production)", result.process.stdout)
        self.assertIn(f"into: {self.promote} (main)", result.process.stdout)
        self.assertNotIn("into: " + str(self.root / "mac-installed"), result.process.stdout)
    def test_record_failure_resumes_after_push_without_second_merge(self) -> None:
        sha = self.checkpoint()
        _ = (self.state / "fail-review_regime.py").touch()
        failed = self.run_checkpoint(sha)
        self.assertIn("record: failed", failed.process.stdout)
        pushed = self.git("rev-parse", "refs/heads/production", cwd=self.origin)
        self.assertNotEqual(pushed, failed.before)
        self.assertFalse(self.log.exists())
        (self.state / "fail-review_regime.py").unlink()
        resumed = self.run_checkpoint(sha)
        self.assertEqual(resumed.process.returncode, 0, resumed.process.stderr + resumed.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), pushed)
        self.assertEqual(len(self.events("checkpoint-test")), 1)
        self.assertEqual(len(self.log.read_text().splitlines()), 1)
        self.assertIn("unblocks: no gate", resumed.process.stdout)
    def test_a_merge_killed_before_its_push_is_tested_before_a_rerun_pushes_it(self) -> None:
        # Killed after the merge and before its tests finished: nothing proves the merge green.
        sha = self.checkpoint()
        before = self.git("rev-parse", "refs/heads/production", cwd=self.origin)
        subject = f"Merge alpha-unit phase 2 ({sha[:7]}) into production"
        _ = self.git("merge", "--no-ff", "-q", "-m", subject, sha, cwd=self.checkout)
        _ = (self.state / "fail-checkpoint-test").touch()
        red = self.run_checkpoint(sha)
        self.assertEqual(red.process.returncode, 1, red.process.stderr + red.process.stdout)
        self.assertIn("red: failed", red.process.stdout)
        self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), before)
        (self.state / "fail-checkpoint-test").unlink()
        green = self.run_checkpoint(sha)
        self.assertEqual(green.process.returncode, 0, green.process.stderr + green.process.stdout)
        self.assertNotEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), before)
    def test_promote_none_merges_and_records_without_promotion(self) -> None:
        self.promotions = "none"
        self.production_doc()
        result = self.run_checkpoint(self.checkpoint())
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        self.assertIn("promote: ok — no promotion configured", result.process.stdout)
        self.assertIn("record: ok", result.process.stdout)
    def test_a_missing_promote_checkout_is_refused_before_anything_is_pushed(self) -> None:
        self.promotions = str(self.unit / "no-such-checkout")
        self.production_doc()
        before = self.git("rev-parse", "refs/heads/production", cwd=self.origin)
        result = self.run_checkpoint(self.checkpoint())
        self.assertNotEqual(result.process.returncode, 0)
        self.assertIn("no-such-checkout does not exist", result.process.stdout + result.process.stderr)
        self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), before)


if __name__ == "__main__":
    _ = unittest.main()
