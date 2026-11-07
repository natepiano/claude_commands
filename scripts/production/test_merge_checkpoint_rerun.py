"""A killed checkpoint resumes unfinished work using its own merge evidence."""
from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
import shutil
import subprocess
from types import ModuleType
from typing import cast, final
import unittest
from unittest.mock import patch

from scripts.production import merge_checkpoint
from scripts.production import change_review
from scripts.production import staff, unit_lookup
from scripts.production.staff_test_support import isolated_staff_file
from scripts.production.merge_checkpoint_test_support import MergeCheckpointFixture, configure_test_environment


class Killed(BaseException):
    """Leave disk state intact, as a process killed without exception cleanup would."""


def setUpModule() -> None:
    configure_test_environment()


@final
class CheckpointRerunTests(MergeCheckpointFixture):
    def review_checkpoint(self, *changes: str, unit: str = "alpha") -> tuple[Path, str]:
        review = Path(self.env["HOME"]) / "rust/hanadocs/change-review/change-review - example.md"
        self.promotions = str(self.promote)
        self.production_doc(change_review=review)
        return review, self.dated_checkpoint(f"checkpoint({unit}): change\n\n" + "\n".join(
            f"Change-Review: {change}" for change in changes))

    def test_checkpoint_reuses_its_staff_snapshot_after_the_file_changes(self) -> None:
        review, checkpoint = self.review_checkpoint("One change | Open it | Result")
        with patch.dict("os.environ", self.env):
            unit = next(row for row in staff.snapshot().rows if row.name == "alpha")
        readers = ModuleType("checkpoint_staff_fixture")
        vars(readers).update(vars(staff))
        with (patch.object(readers, "snapshot", wraps=staff.snapshot) as snapshot,
              patch.object(merge_checkpoint, "staff", readers),
              patch.object(change_review, "staff", readers),
              self.after_input(lambda path: path.write_bytes(b"invalid staff JSON\n"))):
            result = self.run_checkpoint(checkpoint).process
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(snapshot.call_count, 1)
        self.assertEqual([row["Producer"] for row in change_review.pending(review)],
                         [unit.id], result.stdout)

    def save_case(self) -> Path:
        snapshot = self.root.with_name("rerun-seed")
        _ = shutil.copytree(self.root, snapshot)
        self.addCleanup(shutil.rmtree, snapshot)
        # Merge commits must be identical in the uninterrupted and resumed cases.
        self.env["GIT_COMMITTER_DATE"] = "2026-11-06T12:00:00-05:00"
        self.env["GIT_AUTHOR_DATE"] = self.env["GIT_COMMITTER_DATE"]
        return snapshot

    def after_input(self, change: Callable[[Path], object]) -> AbstractContextManager[object]:
        """Change the staff file after the checkpoint reads its unit from that file."""
        staff_file = Path(self.env["STAFF_FILE"])
        read = merge_checkpoint.request_from

        def request_from(args: argparse.Namespace) -> merge_checkpoint.MergeRequest:
            request = read(args)
            _ = change(staff_file)
            return request

        return patch.object(merge_checkpoint, "request_from", side_effect=request_from)

    def restore_case(self, snapshot: Path) -> None:
        shutil.rmtree(self.root)
        _ = shutil.copytree(snapshot, self.root)

    def assert_review_recovered(self, stop: str) -> None:
        review, checkpoint = self.review_checkpoint("First change | Open first | First result", "Second change | Open second | Second result")
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
        self.assert_review_recovered("record_publication_changes")

    def test_review_rows_written_before_receipt_are_not_duplicated(self) -> None:
        review, checkpoint = self.review_checkpoint("A change | Open it | Result")
        original = Path.touch

        def touch(path: Path, mode: int = 0o666, exist_ok: bool = True) -> None:
            if path.name == "promotion":
                raise Killed
            original(path, mode=mode, exist_ok=exist_ok)

        # The list write completes before the promotion receipt records completion.
        with patch.object(Path, "touch", touch), self.assertRaises(Killed):
            _ = self.run_checkpoint(checkpoint)
        expected = review.read_bytes()
        result = self.run_checkpoint(checkpoint).process
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("change review: failed", result.stdout)
        self.assertEqual(review.read_bytes(), expected)

    def assert_staff_refusal(self, refused: subprocess.CompletedProcess[str], review: Path,
                             baseline: str, origin: str) -> None:
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("change review: failed", refused.stdout)
        self.assertIn("the staff record is read before the push", refused.stdout)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), origin)
        self.assertEqual(self.git("rev-parse", "HEAD"), baseline)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertFalse(review.exists())
        self.assertEqual(self.events("checkpoint-test"), [])

    def test_an_unreadable_staff_record_stops_before_push_and_a_repair_writes_each_change_once(self) -> None:
        review, checkpoint = self.review_checkpoint("First change | Open first | First result", "Second change | Open second | Second result")
        baseline = self.git("rev-parse", "HEAD")
        origin = self.git("rev-parse", "production", cwd=self.origin)
        staff_file = Path(self.env["STAFF_FILE"])
        saved_staff = staff_file.read_bytes()
        _ = staff_file.write_bytes(b"invalid staff JSON\n")
        refused = self.run_checkpoint(checkpoint).process
        self.assert_staff_refusal(refused, review, baseline, origin)

        _ = staff_file.write_bytes(saved_staff)
        repaired = self.run_checkpoint(checkpoint).process
        self.assertEqual(repaired.returncode, 0, repaired.stdout + repaired.stderr)
        merge = self.git("rev-parse", "HEAD")
        self.assertNotEqual(merge, baseline)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), merge)
        self.assertEqual([row["Change"] for row in change_review.pending(review)],
                         ["First change", "Second change"])

    def test_an_unreadable_staff_record_undoes_a_resumed_unpushed_merge_before_tests(self) -> None:
        review, checkpoint = self.review_checkpoint("A change | Open it | Result")
        baseline = self.git("rev-parse", "HEAD")
        origin = self.git("rev-parse", "production", cwd=self.origin)
        with patch.object(merge_checkpoint, "checkpoint_tests", side_effect=Killed), self.assertRaises(Killed):
            _ = self.run_checkpoint(checkpoint)
        self.assertNotEqual(self.git("rev-parse", "HEAD"), baseline)
        self.assertEqual(self.events("checkpoint-test"), [])
        with patch.object(change_review, "publication_changes", side_effect=staff.StaffUnreadable("invalid staff JSON")):
            refused = self.run_checkpoint(checkpoint).process
        self.assert_staff_refusal(refused, review, baseline, origin)

    def test_a_production_without_a_review_list_pushes_with_an_unreadable_staff_record(self) -> None:
        checkpoint = self.dated_checkpoint(
            "checkpoint(alpha): change\n\nChange-Review: A change | Open it | Result")
        with self.after_input(Path.unlink):
            result = self.run_checkpoint(checkpoint).process
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("the production doc has no Change review line; no rows written", result.stdout)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), self.git("rev-parse", "HEAD"))

    def test_a_merge_keeps_its_units_producer_after_the_staff_row_goes(self) -> None:
        review, checkpoint = self.review_checkpoint("First change | Open first | First result",
                                                    "Second change | Open second | Second result")
        with isolated_staff_file(Path(self.env["STAFF_FILE"]), self.env):
            unit_staff_id = unit_lookup.unit_row("alpha", staff.snapshot()).id

        def unregister(staff_file: Path) -> None:
            document = cast(dict[str, list[dict[str, object]]], json.loads(staff_file.read_text(encoding="utf-8")))
            document["rows"] = [row for row in document["rows"] if row["name"] != "alpha"]
            _ = staff_file.write_text(json.dumps(document), encoding="utf-8")

        with self.after_input(unregister):
            result = self.run_checkpoint(checkpoint).process
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        notes = [line for line in result.stdout.splitlines() if line.startswith("change review: no staff row for unit")]
        self.assertEqual(notes, [])
        self.assertEqual([row["Producer"] for row in change_review.pending(review)], [unit_staff_id, unit_staff_id])

    def test_an_already_pushed_merge_keeps_the_row_failure_report_for_an_unreadable_staff_record(self) -> None:
        review, checkpoint = self.review_checkpoint("A change | Open it | Result")
        with patch.object(merge_checkpoint, "record_publication_changes", side_effect=Killed), self.assertRaises(Killed):
            _ = self.run_checkpoint(checkpoint)
        merge = self.git("rev-parse", "HEAD")
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), merge)
        with patch.object(change_review, "publication_changes", side_effect=staff.StaffUnreadable("invalid staff JSON")):
            result = self.run_checkpoint(checkpoint).process
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("change review: failed", result.stdout)
        self.assertIn("retry after the repair", result.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), merge)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), merge)
        self.assertFalse(review.exists())

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
