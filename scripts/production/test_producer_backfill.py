"""Check producer writers and the one-time historical backfill with real Git history."""
from __future__ import annotations

import io
from contextlib import redirect_stdout, redirect_stderr
from datetime import date
from pathlib import Path
from unittest import mock

from scripts.production import change_review, change_routing, producer_backfill, staff
from scripts.production.test_change_review import ProducerFixture
from scripts.production.staff_test_support import fixture_member


class ProducerBackfillTests(ProducerFixture):
    listing: Path = Path()
    def backfill(self, write: bool = True) -> producer_backfill.BackfillResult:
        result = producer_backfill.run(self.listing, [self.doc], staff.snapshot(), write)
        result.report(self.listing, write)
        return result

    def test_a_mapped_row_reports_without_writing_then_saves_only_the_staff_id(self) -> None:
        self.old_row(self.merge())
        before = self.listing.read_bytes()
        self.assertEqual(self.backfill(False).filled, {"C1": self.unit.id})
        self.assertEqual(self.listing.read_bytes(), before)
        self.assertEqual(self.backfill().filled, {"C1": self.unit.id})
        row = change_review.pending(self.listing)[0]
        self.assertEqual(row["Producer"], self.unit.id)
        self.assertNotIn(self.unit.name, row["Producer"])

    def test_a_retired_unit_maps_through_its_removed_worktree(self) -> None:
        self.old_row(self.merge(), reviewed=True)
        _ = staff.set_worktree(self.unit.id, staff.RemovedWorktree("natedev", self.root / "unit"),
                                   expected=self.unit.worktree)
        self.assertEqual(self.backfill().filled, {"C1": self.unit.id})
        self.assertEqual(change_review.review_snapshot(self.listing).reviewed[0]["Producer"], self.unit.id)

    def test_a_renamed_unit_maps_its_old_plan_to_its_fixed_staff_id(self) -> None:
        self.old_row(self.merge("old-plan"))
        _ = staff.rename(self.unit.id, "latest-name")
        self.assertEqual(self.backfill().filled, {"C1": self.unit.id})

    def test_an_old_prefixed_name_maps_without_a_units_key_and_saves_once(self) -> None:
        self.old_row(self.merge("build-followups-new-unit"))
        before = self.listing.read_bytes()
        self.assertEqual(self.backfill(False).filled, {"C1": self.unit.id})
        self.assertEqual(self.listing.read_bytes(), before)
        self.assertEqual(self.backfill().filled, {"C1": self.unit.id})
        self.assertEqual(change_review.pending(self.listing)[0]["Producer"], self.unit.id)
        saved = self.listing.read_bytes()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(self.backfill().filled, {})
        self.assertIn("no change", output.getvalue())
        self.assertEqual(self.listing.read_bytes(), saved)

    def assert_name_fallback_keeps_other_checkpoint_unmapped(self, name: str) -> None:
        self.old_row(self.merge("build-followups-new-unit"))
        self.old_row(self.merge(name))
        output = io.StringIO()
        with redirect_stdout(output):
            result = self.backfill()
        self.assertEqual(result.filled, {"C1": self.unit.id})
        self.assertEqual(result.unmapped, {"C2": name})
        self.assertIn(self.listing.name + "/C2: " + name, output.getvalue())
        self.assertEqual([row["Producer"] for row in change_review.pending(self.listing)], [self.unit.id, ""])

    def test_an_old_name_with_two_unit_rows_stays_unmapped_including_a_retired_unit(self) -> None:
        first = fixture_member("unit", name="style", parent=self.unit.parent,
            worktree=staff.PresentWorktree("natedev", self.root / "style"))
        runner = fixture_member("showrunner", name="other")
        second = fixture_member("unit", name="style", parent=runner.id,
            worktree=staff.RemovedWorktree("natedev", self.root / "retired-style"))
        self.assertNotEqual(first.id, second.id)
        self.assert_name_fallback_keeps_other_checkpoint_unmapped("build-followups-style")

    def test_an_old_name_of_only_a_showrunner_stays_unmapped(self) -> None:
        _ = fixture_member("showrunner", name="builds")
        self.assert_name_fallback_keeps_other_checkpoint_unmapped("build-followups-builds")

    def test_an_old_name_without_a_staff_row_stays_unmapped(self) -> None:
        self.assert_name_fallback_keeps_other_checkpoint_unmapped("build-followups-two-accounts")

    def test_an_old_name_maps_when_its_units_worktree_has_no_staff_row(self) -> None:
        unit = fixture_member("unit", name="hierarchies", parent=self.unit.parent,
            worktree=staff.PresentWorktree("natedev", self.root / "staff-hierarchies"))
        _ = self.doc.write_text(self.doc.read_text()
            + f"| hierarchies | hierarchies.md | {self.root / 'old-hierarchies'} | build-followups-hierarchies |\n")
        self.old_row(self.merge("build-followups-hierarchies"))
        self.assertEqual(self.backfill().filled, {"C1": unit.id})
        self.assertEqual(change_review.pending(self.listing)[0]["Producer"], unit.id)

    def test_a_units_worktree_producer_wins_over_a_different_staff_name(self) -> None:
        self.old_row(self.merge("build-followups-new-unit"))
        other = fixture_member("unit", name="hierarchies", parent=self.unit.parent,
            worktree=staff.PresentWorktree("natedev", self.root / "hierarchies"))
        _ = self.doc.write_text(self.doc.read_text().replace("| old-branch |", "| build-followups-hierarchies |"))
        self.old_row(self.merge("build-followups-hierarchies"))
        self.assertEqual(self.backfill().filled, {"C1": self.unit.id, "C2": self.unit.id})
        rows = change_review.pending(self.listing)
        self.assertEqual([row["Producer"] for row in rows], [self.unit.id, self.unit.id])
        self.assertNotEqual(rows[1]["Producer"], other.id)

    def test_a_name_without_the_old_prefix_stays_unmapped_despite_a_matching_unit(self) -> None:
        _ = fixture_member("unit", name="legacy-style", parent=self.unit.parent,
            worktree=staff.PresentWorktree("natedev", self.root / "legacy-style"))
        self.assert_name_fallback_keeps_other_checkpoint_unmapped("legacy-style")

    def test_an_unmapped_checkpoint_is_reported_by_name_and_stays_empty(self) -> None:
        self.old_row(self.merge("unknown-unit"))
        before = self.listing.read_bytes()
        output = io.StringIO()
        with redirect_stdout(output):
            result = self.backfill()
        self.assertEqual(result.unmapped, {"C1": "unknown-unit"})
        self.assertIn(self.listing.name + "/C1: unknown-unit", output.getvalue())
        self.assertEqual(change_review.pending(self.listing)[0]["Producer"], "")
        self.assertEqual(self.listing.read_bytes(), before)
        self.assertEqual(list(self.root.glob("*.backup-*")), [])

    def test_a_missing_checkpoint_is_reported_without_assigning_an_owner(self) -> None:
        self.old_row("abcdef123456")
        self.assertEqual(self.backfill().unmapped, {"C1": "no checkpoint found"})
        self.assertEqual(change_review.pending(self.listing)[0]["Producer"], "")

    def test_backfill_keeps_a_backup_and_a_rerun_changes_nothing(self) -> None:
        self.old_row(self.merge())
        before = self.listing.read_bytes()
        _ = self.backfill()
        backups = list(self.root.glob(self.listing.name + ".backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), before)
        saved = self.listing.read_bytes(), self.listing.stat().st_mtime_ns
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(self.backfill().filled, {})
        self.assertIn("no change", output.getvalue())
        self.assertEqual((self.listing.read_bytes(), self.listing.stat().st_mtime_ns), saved)
        self.assertEqual(list(self.root.glob(self.listing.name + ".backup-*")), backups)

    def test_each_change_in_a_multi_unit_merge_selects_its_own_checkpoint(self) -> None:
        runner = staff.snapshot().by_path("top")
        assert runner is not None
        second = fixture_member("unit", name="second-unit", parent=runner.id,
            worktree=staff.PresentWorktree("natedev", self.root / "second"))
        _ = self.doc.write_text(self.doc.read_text() + f"| second-unit | second.md | {self.root / 'second'} | second |\n")
        _ = self.git("checkout", "-b", "old-branch")
        for name, change in (("old-branch", "First"), ("second", "Second")):
            _ = self.git("commit", "--allow-empty", "-m", f"checkpoint({name}): change\n\nChange-Review: {change} | Read | Proof")
        _ = self.git("checkout", "main")
        _ = self.git("merge", "--no-ff", "old-branch", "-m", "Merge both")
        for change in ("First", "Second"):
            self.old_row(self.git("rev-parse", "HEAD"), change)
        self.assertEqual(self.backfill().filled, {"C1": self.unit.id, "C2": second.id})


    def test_a_checkpoint_merge_subject_names_the_producer_when_its_delta_has_no_matching_change(self) -> None:
        for name in ("first", "second"):
            runner = fixture_member("showrunner", name=name)
            _ = fixture_member("unit", name=name + "-unit", parent=runner.id,
                worktree=staff.PresentWorktree("natedev", self.root / name))
            _ = self.doc.write_text(self.doc.read_text()
                + f"| {name}-unit | {name}.md | {self.root / name} | {name} |\n")
        _ = self.git("checkout", "-b", "imported")
        for name in ("first", "second"):
            _ = self.git("commit", "--allow-empty", "-m",
                f"checkpoint({name}): other change\n\nChange-Review: {name} change | Read | Proof")
        _ = self.git("checkout", "main")
        _ = self.git("merge", "--no-ff", "imported", "-m",
            "checkpoint(old-branch): merge origin/main into old-branch")
        self.old_row(self.git("rev-parse", "HEAD"))
        self.assertEqual(self.backfill().filled, {"C1": self.unit.id})
        row = change_review.pending(self.listing)[0]
        assignment = change_routing.StaffRouting(staff.snapshot(), {}).owner_and_production(self.listing, row)
        self.assertIsInstance(assignment.owner, change_routing.StaffRoutedChange)
        self.assertEqual(assignment.owner.tab, "top")
        self.assertEqual(assignment.production, self.doc)

    def test_the_command_adds_a_missing_producer_column_from_the_lists_documents(self) -> None:
        self.old_row(self.merge())
        lines = self.listing.read_text().splitlines()
        for index, line in enumerate(lines):
            if line.startswith(("| # |", "| ---", "| C1 |")):
                lines[index] = "|".join(line.split("|")[:-2]) + "|"
        _ = self.listing.write_text("\n".join(lines) + "\n")
        before = self.listing.read_bytes()
        from scripts.production import showrunners
        with mock.patch.object(showrunners, "timer_docs", return_value={}), redirect_stdout(io.StringIO()):
            self.assertEqual(change_review.main(["backfill-producer", "--list", str(self.listing)]), 0)
            self.assertEqual(self.listing.read_bytes(), before)
            self.assertEqual(change_review.main(["backfill-producer", "--list", str(self.listing), "--write"]), 0)
        self.assertEqual(change_review.pending(self.listing)[0]["Producer"], self.unit.id)

    def test_one_units_checkpoint_still_maps_after_the_change_text_is_edited(self) -> None:
        _ = self.merge()
        _ = self.git("commit", "--amend", "-m", "checkpoint(old-branch): merge the unit")
        self.old_row(self.git("rev-parse", "HEAD"), "Edited change text")
        self.assertEqual(self.backfill().filled, {"C1": self.unit.id})

    def test_an_unmatched_delta_without_an_own_checkpoint_name_leaves_the_producer_empty(self) -> None:
        self.old_row(self.merge(), "Unmatched change text")
        self.assertEqual(self.backfill().unmapped, {"C1": "no checkpoint found"})
        self.assertEqual(change_review.pending(self.listing)[0]["Producer"], "")

    def test_an_exact_branch_wins_over_an_earlier_alias_during_backfill(self) -> None:
        runner = staff.snapshot().by_path("top")
        assert runner is not None
        second = fixture_member("unit", name="second-unit", parent=runner.id,
                                worktree=staff.PresentWorktree("natedev", self.root / "second"))
        _ = self.doc.write_text(self.doc.read_text().replace("| new-unit |", "| branch-b |")
            .replace("| old-branch |", "| branch-a |")
            + f"| second-unit | second.md | {self.root / 'second'} | branch-b |\n")
        self.old_row(self.merge("branch-b"))
        self.assertEqual(self.backfill().filled, {"C1": second.id})

    def test_a_failed_first_list_does_not_stop_the_second_lists_write(self) -> None:
        self.old_row(self.merge())
        bad = self.root / "change-review - a-broken.md"
        _ = bad.write_text("# The review table is missing\n")
        from scripts.production import showrunners
        output, errors = io.StringIO(), io.StringIO()
        with (mock.patch.object(change_review, "lists_directory", return_value=self.root),
              mock.patch.object(showrunners, "timer_docs", return_value={"top": self.doc}),
              redirect_stdout(output), redirect_stderr(errors)):
            result = change_review.main(["backfill-producer", "--write"])
        self.assertEqual(result, 1)
        self.assertIn(f"change review: {bad.name}:", errors.getvalue())
        self.assertEqual(change_review.pending(self.listing)[0]["Producer"], self.unit.id)
        self.assertIn(self.listing.name + ": 1 filled", output.getvalue())

    def test_an_invalid_utf8_first_list_does_not_stop_the_second_lists_write(self) -> None:
        self.old_row(self.merge())
        bad = self.root / "change-review - a-broken.md"
        _ = bad.write_bytes(b"\xff")
        from scripts.production import showrunners
        output, errors = io.StringIO(), io.StringIO()
        with (mock.patch.object(change_review, "lists_directory", return_value=self.root),
              mock.patch.object(showrunners, "timer_docs", return_value={"top": self.doc}),
              redirect_stdout(output), redirect_stderr(errors)):
            result = change_review.main(["backfill-producer", "--write"])
        self.assertEqual(result, 1)
        self.assertIn(f"change review: {bad.name}:", errors.getvalue())
        self.assertEqual(change_review.pending(self.listing)[0]["Producer"], self.unit.id)
        self.assertIn(self.listing.name + ": 1 filled", output.getvalue())

    def test_a_separator_only_first_list_row_does_not_stop_the_second_lists_write(self) -> None:
        self.old_row(self.merge())
        bad = self.root / "change-review - a-broken.md"
        _ = bad.write_text("## To review\n\n| # | Change |\n| --- | --- |\n|\n")
        from scripts.production import showrunners
        output, errors = io.StringIO(), io.StringIO()
        with (mock.patch.object(change_review, "lists_directory", return_value=self.root),
              mock.patch.object(showrunners, "timer_docs", return_value={"top": self.doc}),
              redirect_stdout(output), redirect_stderr(errors)):
            result = change_review.main(["backfill-producer", "--write"])
        self.assertEqual(result, 1)
        self.assertIn(f"change review: {bad.name}: {bad}: | has the wrong number of cells", errors.getvalue())
        self.assertEqual(change_review.pending(self.listing)[0]["Producer"], self.unit.id)
        self.assertIn(self.listing.name + ": 1 filled", output.getvalue())

    def test_an_invalid_utf8_production_document_fails_only_its_list(self) -> None:
        self.old_row(self.merge())
        bad_doc = self.root / "bad.md"
        _ = bad_doc.write_bytes(b"\xff")
        bad = self.root / "change-review - a-broken.md"
        _ = bad.write_text(self.listing.read_text().replace(str(self.doc), str(bad_doc)))
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            result = producer_backfill.run_lists([bad, self.listing], [self.doc], staff.snapshot(), True)
        self.assertEqual(result, 1)
        self.assertIn(f"change review: {bad.name}:", errors.getvalue())
        self.assertIn("utf-8", errors.getvalue())
        self.assertEqual(change_review.pending(self.listing)[0]["Producer"], self.unit.id)
        self.assertIn(self.listing.name + ": 1 filled", output.getvalue())

    def test_an_unreadable_staff_record_stops_the_run_before_any_list_changes(self) -> None:
        self.old_row(self.merge())
        before = self.listing.read_bytes()
        doc = self.root / "top-production.md"
        _ = doc.write_text(self.doc.read_text())
        record = staff.snapshot()
        _ = (self.root / "staff.json").write_bytes(b"\xff")
        with self.assertRaisesRegex(staff.StaffUnreadable, "cannot read staff record"):
            _ = producer_backfill.run_lists([self.listing], [doc], record, True)
        self.assertEqual(self.listing.read_bytes(), before)
        self.assertEqual(list(self.root.glob("*.backup-*")), [])

    def test_two_good_lists_print_both_reports_and_return_success(self) -> None:
        self.old_row(self.merge())
        other = self.root / "change-review - second.md"
        _ = other.write_bytes(self.listing.read_bytes())
        from scripts.production import showrunners
        output = io.StringIO()
        with (mock.patch.object(change_review, "lists_directory", return_value=self.root),
              mock.patch.object(showrunners, "timer_docs", return_value={"top": self.doc}), redirect_stdout(output)):
            result = change_review.main(["backfill-producer", "--write"])
        self.assertEqual(result, 0)
        for listing in (self.listing, other):
            self.assertEqual(change_review.pending(listing)[0]["Producer"], self.unit.id)
            self.assertIn(listing.name + ": 1 filled", output.getvalue())

    def track_listing(self) -> None:
        previous = self.listing
        self.listing = self.repo / previous.name
        _ = self.doc.write_text(self.doc.read_text().replace(str(previous), str(self.listing)))

    def test_a_rerun_finishes_an_interrupted_commit_without_another_backup(self) -> None:
        self.track_listing()
        self.old_row(self.merge())
        before = self.git("rev-parse", "HEAD")
        with mock.patch.object(change_review, "_commit", side_effect=change_review.ChangeReviewError("injected commit stop")):
            with self.assertRaisesRegex(change_review.ChangeReviewError, "injected commit stop"):
                _ = self.backfill()
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        saved = self.listing.read_bytes()
        backups = list(self.repo.glob(self.listing.name + ".backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(self.backfill().filled, {})
        self.assertNotEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.git("status", "--porcelain", "--", self.listing.name), "")
        self.assertEqual(self.listing.read_bytes(), saved)
        self.assertEqual(list(self.repo.glob(self.listing.name + ".backup-*")), backups)

    def test_a_finished_list_rerun_changes_no_bytes_backup_or_commit(self) -> None:
        self.track_listing()
        _ = change_review.append_rows(self.listing, [change_review.Visible("Filled", "Read", "Proof")],
             date(2026, 11, 2), self.merge(), self.doc, producers=[self.unit.id])
        before = self.listing.read_bytes(), self.git("rev-parse", "HEAD")
        self.assertEqual(self.backfill().filled, {})
        self.assertEqual((self.listing.read_bytes(), self.git("rev-parse", "HEAD")), before)
        self.assertEqual(list(self.repo.glob(self.listing.name + ".backup-*")), [])
