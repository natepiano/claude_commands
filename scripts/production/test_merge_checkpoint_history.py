"""Exercise checkpoint merges against disposable repositories and command stubs."""

from __future__ import annotations

from scripts.agents import remote

import json
import os
import shutil
import unittest
from pathlib import Path
from typing import final
from unittest import mock


from scripts.production.merge_checkpoint_test_support import MergeCheckpointFixture, configure_test_environment

from scripts.production import merge_checkpoint
from scripts.production import staff
from scripts.production.production_doc import read_production


def setUpModule() -> None:
    configure_test_environment()


@final
class MergeCheckpointTests(MergeCheckpointFixture):
    def test_recorded_checkpoints_keep_retired_units(self) -> None:
        checkpoint = self.checkpoint()
        _ = self.git("merge", "--no-ff", "-m", "Integrate completed work", "alpha-branch")
        runs = self.root / "history/runs"
        runs.mkdir(parents=True, exist_ok=True)
        _ = (runs / "checkpoints.jsonl").write_text(json.dumps({
            "schema_version": 1, "event_type": "checkpoint", "working_dir": str(self.unit),
            "phase_id": "2", "checkpoint_commit": checkpoint,
        }) + "\n")
        record_path = self.root / "staff.json"
        _ = record_path.write_bytes(Path(self.env["STAFF_FILE"]).read_bytes())
        self.env["STAFF_FILE"] = str(record_path)
        with mock.patch.dict(os.environ, self.env):
            row = next(row for row in staff.snapshot().find(kind="unit") if row.name == "alpha")
            _ = staff.set_worktree(row.id, staff.RemovedWorktree(remote.local_machine(), self.unit), expected=row.worktree)
            found = merge_checkpoint.merged_checkpoints(read_production("example"))
        self.assertEqual(found, (merge_checkpoint.MergedCheckpoint("alpha", "2", checkpoint),))

    def test_recorded_checkpoints_use_one_git_history_walk(self) -> None:
        first = self.checkpoint()
        _ = self.git("merge", "--no-ff", "-m", "Integrate completed work", "alpha-branch")
        second = self.checkpoint(content="second change\n")
        _ = self.git("merge", "--no-ff", "-m", "Integrate more work", "alpha-branch")
        absent = self.checkpoint(content="not yet merged\n")
        runs = self.root / "history/runs"
        runs.mkdir(parents=True, exist_ok=True)
        _ = (runs / "checkpoints.jsonl").write_text("".join(json.dumps({
            "schema_version": 1, "event_type": "checkpoint", "working_dir": str(self.unit),
            "phase_id": str(number), "checkpoint_commit": commit,
        }) + "\n" for number, commit in enumerate((first, second, absent), 1)))
        with mock.patch.dict(os.environ, self.env):
            production = read_production("example")
            with mock.patch.object(merge_checkpoint, "git", wraps=merge_checkpoint.git) as calls:
                found = merge_checkpoint.merged_checkpoints(production)
        self.assertEqual(found, (merge_checkpoint.MergedCheckpoint("alpha", "1", first),
                                 merge_checkpoint.MergedCheckpoint("alpha", "2", second)))
        self.assertEqual([call.args[1] for call in calls.call_args_list], ["rev-list"])

    def test_merge_evidence_finds_first_later_and_unmerged_checkpoints_with_few_git_calls(self) -> None:
        first = self.git("rev-list", "--max-parents=0", "HEAD").splitlines()[0]
        for number in range(16):
            _ = self.git("commit", "--allow-empty", "-m", f"Unlabeled integration {number}")
        later = self.git("rev-parse", "HEAD~4")
        missing = self.checkpoint(content="not on production\n")
        tip = self.git("rev-parse", "HEAD")
        with mock.patch.dict(os.environ, self.env):
            production = read_production("example")
            for checkpoint, expected in ((first, first), (later, later), (missing, tip)):
                with self.subTest(checkpoint=checkpoint), \
                     mock.patch.object(merge_checkpoint, "git", wraps=merge_checkpoint.git) as calls:
                    self.assertEqual(merge_checkpoint.merge_evidence(production, checkpoint), expected)
                    ancestry_calls = [call for call in calls.call_args_list if call.args[1] == "merge-base"]
                    self.assertLessEqual(len(ancestry_calls), 6)

    def test_a_repair_merge_keeps_the_phase_review_row_and_logs_both_merges(self) -> None:
        self.env["MERGE_CHECKPOINT_REVIEW_REGIME"] = str(Path(__file__).with_name("review_regime.py"))
        ledger = Path(self.env["HOME"]) / ".claude/data/review_regime.jsonl"
        code = self.checkpoint()
        first = self.run_checkpoint(code)
        self.assertEqual(first.process.returncode, 0, first.process.stderr + first.process.stdout)
        self.assertIn("record: ok — review ledger and production log updated\n", first.process.stdout)
        first_row = ledger.read_bytes()
        self.assertEqual(len(first_row.splitlines()), 1)
        first_merge = self.git("rev-parse", "HEAD")
        first_merged_at = self.git("show", "-s", "--format=%cI", first_merge)

        repair = self.checkpoint(content="alpha repaired\n", subject="repair alpha phase two")
        second = self.run_checkpoint(repair)
        self.assertEqual(second.process.returncode, 0, second.process.stderr + second.process.stdout)
        self.assertIn("record: ok — kept alpha phase 2 of docs/plans/alpha.md: "
                      + f"a row exists from the merge at {first_merged_at}; nothing added; "
                      + "review ledger and production log updated\n", second.process.stdout)
        self.assertEqual(ledger.read_bytes(), first_row)
        repair_merge = self.git("rev-parse", "HEAD")
        self.assertNotEqual(repair_merge, first_merge)
        lines = self.log.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn(f"alpha phase 2 ({code}) merged as {first_merge}; ", lines[0])
        self.assertIn(f"alpha phase 2 ({repair}) merged as {repair_merge}; ", lines[1])

        repeated = self.run_checkpoint(repair)
        self.assertEqual(repeated.process.returncode, 0, repeated.process.stderr + repeated.process.stdout)
        self.assertIn("record: ok — checkpoint already recorded\n", repeated.process.stdout)
        self.assertEqual(self.log.read_text().splitlines(), lines)
        self.assertEqual(ledger.read_bytes(), first_row)

    def test_shrink_merges_only_plan_and_next_items_after_code_checkpoint(self) -> None:
        for newer_plan, closing_kind in ((False, "shrink"), (True, "shrink"), (True, "as-built")):
            with self.subTest(newer_plan=newer_plan, closing_kind=closing_kind):
                shutil.rmtree(self.root)
                _ = shutil.copytree(self.fixture_snapshot, self.root)
                self.bind_fixture(self.root)
                if closing_kind == "as-built":
                    _ = self.checkpoint("docs/plans/alpha.md", "plan before closing\n")
                code = self.checkpoint()
                first = self.run_checkpoint(code)
                self.assertEqual(first.process.returncode, 0, first.process.stderr + first.process.stdout)
                self.write(self.unit, "docs/plans/alpha.md", "short as built\n")
                self.write(self.unit, "docs/plans/alpha-next.md", "next items\n")
                _ = self.git("add", "docs/plans/alpha.md", "docs/plans/alpha-next.md", cwd=self.unit)
                subject = "shrink(alpha): phase 2 — Alpha phase" if newer_plan else "shrink"
                if closing_kind == "as-built":
                    _ = self.git("rm", "-f", "docs/plans/alpha.md", cwd=self.unit)
                    self.write(self.unit, "docs/as-built/alpha.md", "as built\n")
                    _ = self.git("add", "docs/as-built/alpha.md", cwd=self.unit)
                    subject = "docs(alpha): as-built"
                _ = self.git("commit", "-m", subject, cwd=self.unit)
                sha = self.git("rev-parse", "HEAD", cwd=self.unit)
                _ = self.git("push", "origin", "alpha-branch", cwd=self.unit)
                if newer_plan:
                    config = self.unit / ".unit.toml"
                    _ = config.write_text(config.read_text().replace("docs/plans/alpha.md", "docs/plans/new-alpha.md"))
                result = self.run_checkpoint(sha, "--" + closing_kind, review=False)
                self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
                self.assertEqual(len(self.events("review_regime.py")), 1)
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
        self.assertIn("scope: ok — 1 changed paths", result.process.stdout)
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
        self.assertIn(f"send alpha: phase 2 merged and pushed as {code_merge}", code_rerun.process.stdout)
        shrink_rerun = self.run_checkpoint(shrink, "--shrink", review=False, delivers=None)
        self.assertEqual(shrink_rerun.process.returncode, 0,
                         shrink_rerun.process.stderr + shrink_rerun.process.stdout)
        self.assertIn(f"send alpha: phase 2 merged and pushed as {shrink_merge}",
                      shrink_rerun.process.stdout)
    def test_shrink_holds_before_code_checkpoint(self) -> None:
        code = self.checkpoint()
        sha = self.checkpoint("docs/plans/alpha.md", "short as built\n")
        early = self.run_checkpoint(sha, "--shrink", review=False)
        self.assert_stopped_before_merge(early, "held", f"unmerged commit {code[:12]}: alpha work")

    def test_shrink_passes_after_code_merges_through_another_branch(self) -> None:
        code = self.checkpoint()
        _ = self.git("merge", "--ff-only", "alpha-branch", cwd=self.other)
        _ = self.git("merge", "--no-ff", "-m", "Integrate work through beta", "beta-branch")
        _ = self.git("push", "origin", "production")
        runs = self.root / "history/runs"
        runs.mkdir(parents=True, exist_ok=True)
        _ = (runs / "checkpoints.jsonl").write_text(json.dumps({
            "schema_version": 1, "event_type": "checkpoint", "working_dir": str(self.other),
            "phase_id": "2", "checkpoint_commit": code,
        }) + "\n")
        shrink = self.checkpoint("docs/plans/alpha.md", "short as built\n", subject="shrink")
        result = self.run_checkpoint(shrink, "--shrink", review=False)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        self.assertIn("scope: ok — 1 changed paths", result.process.stdout)

    def test_shrink_passes_when_parent_is_merge_branch_tip_without_a_checkpoint_event(self) -> None:
        code = self.checkpoint()
        _ = self.git("merge", "--ff-only", "alpha-branch")
        _ = self.git("push", "origin", "production")
        self.assertEqual(self.git("rev-parse", "HEAD"), code)
        shrink = self.checkpoint("docs/plans/alpha.md", "short as built\n", subject="shrink")
        result = self.run_checkpoint(shrink, "--shrink", review=False)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        self.assertIn("scope: ok — 1 changed paths", result.process.stdout)

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


if __name__ == "__main__":
    _ = unittest.main()
