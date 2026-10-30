"""A killed checkpoint resumes unfinished work using its own merge evidence."""
from __future__ import annotations

from pathlib import Path
import shutil
import sys
from typing import final
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import merge_checkpoint
from scripts.production.merge_checkpoint_test_support import MergeCheckpointFixture, configure_test_environment


class Killed(BaseException):
    """Leave disk state intact, as a process killed without exception cleanup would."""


def setUpModule() -> None:
    configure_test_environment()


@final
class CheckpointRerunTests(MergeCheckpointFixture):
    def save_case(self) -> Path:
        snapshot = self.root.with_name("rerun-seed")
        _ = shutil.copytree(self.root, snapshot)
        self.addCleanup(shutil.rmtree, snapshot)
        # Merge commits must be identical in the uninterrupted and resumed cases.
        self.env["GIT_COMMITTER_DATE"] = "2026-10-29T12:00:00-04:00"
        self.env["GIT_AUTHOR_DATE"] = self.env["GIT_COMMITTER_DATE"]
        return snapshot

    def restore_case(self, snapshot: Path) -> None:
        shutil.rmtree(self.root)
        _ = shutil.copytree(snapshot, self.root)

    def assert_review_recovered(self, stop: str) -> None:
        review = self.root / "change-review.md"
        self.production_doc(change_review=review)
        checkpoint = self.dated_checkpoint(
            "checkpoint(alpha): two changes\n\n"
            + "Change-Review: First change | Open first | First result\n"
            + "Change-Review: Second change | Open second | Second result")
        snapshot = self.save_case()
        uninterrupted = self.run_checkpoint(checkpoint).process
        self.assertEqual(uninterrupted.returncode, 0, uninterrupted.stdout + uninterrupted.stderr)
        expected = review.read_bytes()
        merge = self.git("rev-parse", "HEAD")
        expected_tests = self.events("checkpoint-test")
        self.restore_case(snapshot)
        with patch.object(merge_checkpoint, stop, side_effect=Killed), self.assertRaises(Killed):
            _ = self.run_checkpoint(checkpoint)
        self.assertFalse(review.exists())
        self.assertEqual(self.git("rev-parse", "HEAD"), merge)
        resumed = self.run_checkpoint(checkpoint).process
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertTrue(review.exists(), "rerun must restore the missing Change-Review rows")
        self.assertEqual(review.read_bytes(), expected)
        self.assertEqual(self.git("rev-parse", "HEAD"), merge)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), merge)
        self.assertEqual(self.events("checkpoint-test"), expected_tests)
        repeated = self.run_checkpoint(checkpoint).process
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertNotIn("change review: failed", repeated.stdout)
        self.assertEqual(review.read_bytes(), expected)

    def test_review_rows_recover_after_merge_before_tests(self) -> None:
        self.assert_review_recovered("tests")

    def test_review_rows_recover_after_push_before_recording(self) -> None:
        self.assert_review_recovered("record_changes")

    def test_review_rows_written_before_receipt_are_not_duplicated(self) -> None:
        review = self.root / "change-review.md"
        self.production_doc(change_review=review)
        checkpoint = self.dated_checkpoint(
            "checkpoint(alpha): change\n\nChange-Review: A change | Open it | Result")
        original = Path.touch

        def touch(path: Path, mode: int = 0o666, exist_ok: bool = True) -> None:
            if path.name == "change-review":
                raise Killed
            original(path, mode=mode, exist_ok=exist_ok)

        # The write completes but the command dies before recording completion.
        with patch.object(Path, "touch", touch), self.assertRaises(Killed):
            _ = self.run_checkpoint(checkpoint)
        expected = review.read_bytes()
        result = self.run_checkpoint(checkpoint).process
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("change review: failed", result.stdout)
        self.assertEqual(review.read_bytes(), expected)

    def test_handoff_killed_after_merge_retests_before_push(self) -> None:
        _ = self.child_production()
        checkpoint = self.checkpoint()
        snapshot = self.save_case()
        uninterrupted = self.run_handoff(checkpoint)
        self.assertEqual(uninterrupted.returncode, 0, uninterrupted.stdout + uninterrupted.stderr)
        merge = self.git("rev-parse", "HEAD")
        expected_tests = self.events("checkpoint-test")
        self.assertTrue(expected_tests)
        self.restore_case(snapshot)
        with patch.object(merge_checkpoint, "checkpoint_tests", side_effect=Killed), self.assertRaises(Killed):
            _ = self.run_handoff(checkpoint)
        self.assertEqual(self.git("rev-parse", "HEAD"), merge)
        self.assertNotEqual(self.git("rev-parse", "production", cwd=self.origin), merge)
        self.assertEqual(self.events("checkpoint-test"), [])
        result = self.run_handoff(checkpoint)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.events("checkpoint-test"), expected_tests)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), merge)
        self.assertEqual(self.git("rev-parse", "HEAD"), merge)
        self.assertEqual(len(self.events("send.py")), 1)

    def assert_handoff_promotion_recovered(self, failed_promotion: bool) -> None:
        self.promotions = str(self.promote)
        self.production_doc()
        _ = self.child_production()
        checkpoint = self.checkpoint()
        snapshot = self.save_case()
        uninterrupted = self.run_handoff(checkpoint)
        self.assertEqual(uninterrupted.returncode, 0, uninterrupted.stdout + uninterrupted.stderr)
        merge = self.git("rev-parse", "HEAD")
        expected_tests = self.events("checkpoint-test")
        expected_notice = self.events("send.py")
        expected_log = self.log.read_text().split(": ", 1)[1]
        self.restore_case(snapshot)
        if failed_promotion:
            with patch.object(merge_checkpoint, "promote", side_effect=merge_checkpoint.Stop(
                    "promote", "failed", "injected failure", "retry promotion")):
                failed = self.run_handoff(checkpoint)
            self.assertNotEqual(failed.returncode, 0)
        else:
            with patch.object(merge_checkpoint, "promote", side_effect=Killed), self.assertRaises(Killed):
                _ = self.run_handoff(checkpoint)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), merge)
        self.assertNotEqual(self.git("rev-parse", "HEAD", cwd=self.promote), merge)
        self.assertEqual(self.events("send.py"), [])
        result = self.run_handoff(checkpoint)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), merge)
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), merge)
        self.assertEqual(self.git("rev-parse", "HEAD"), merge)
        self.assertEqual(self.events("checkpoint-test"), expected_tests)
        self.assertEqual(self.events("send.py"), expected_notice)
        self.assertEqual(self.log.read_text().split(": ", 1)[1], expected_log)
        self.assertEqual(self.events("style_sync.py"), [["notify", "--repo-dir", str(self.checkout),
                                                        "--branch", "production", "--tip", merge]])

    def test_handoff_killed_after_push_retries_promotion_before_acknowledgment(self) -> None:
        self.assert_handoff_promotion_recovered(False)

    def test_handoff_failed_promotion_retries_before_acknowledgment(self) -> None:
        self.assert_handoff_promotion_recovered(True)


if __name__ == "__main__":
    _ = unittest.main()
