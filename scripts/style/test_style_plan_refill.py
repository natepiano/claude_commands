"""Unmerged product changes hold only the affected selected work."""

from __future__ import annotations

import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import final
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import style_plan
import style_proposals

import berth_board
import style_ledger

from style_plan_test_support import StylePlanFixture


@final
class StylePlanTests(StylePlanFixture):
    def test_repair_first_does_not_read_the_board_or_change_the_plan(self) -> None:
        text = self.plan.read_text() + "\n### Phase 3 — repair round 1 · status: todo\nFix a review finding.\n"
        _ = self.plan.write_text(text)
        with mock.patch.object(berth_board, "read_board", side_effect=AssertionError("board read")):
            self.assertEqual(self.run_next(), (0, "repair first\n", ""))
        self.assertEqual(self.plan.read_text(), text)
        self.assertFalse((self.root / "state").exists())

    def test_unknown_board_stops_next_phase_and_queue_without_writing_or_recording(self) -> None:
        _ = self.rule("read")
        self.sync()
        before = self.plan.read_bytes()
        with mock.patch.object(berth_board, "read_board", return_value=berth_board.BoardUnavailable("unreadable")):
            self.assertEqual(self.run_next(), (4, "board unavailable: unreadable\n", ""))
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(style_plan.main(["queue", "--repo", str(self.repo)]), 4)
            self.assertEqual(output.getvalue(), "board unavailable: unreadable\n")
        self.assertEqual(self.plan.read_bytes(), before)
        self.assertEqual(self.count_tickets(), 0)
        self.assertFalse((self.plan.parent.parent / "rules").exists())

    def test_refill_holds_the_selected_ticket_when_main_touches_its_file(self) -> None:
        _ = self.rule("read")
        self.sync()
        before = self.plan.read_bytes()
        self.write(self.sources[0], "pub fn changed_on_main() {}\n")
        _ = self.commit()
        self.assertEqual(self.run_next(), (
            3, f"waiting for sync: read crates/alpha: main touched {self.sources[0]}\n", ""))
        self.assertEqual(self.plan.read_bytes(), before)
        self.assertEqual(self.count_tickets(), 0)
        self.assertEqual([row[0] for row in self.sync_wait_rows()], ["names-unit"])

    def test_refill_starts_on_the_old_synced_tip_when_main_touches_only_another_crate(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.write(self.sources[2], "pub fn other_crate_on_main() {}\n")
        _ = self.commit()
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        ticket = self.ledger().ticket_open("names-unit")[0]
        self.assertEqual((ticket["crate"], ticket["base"]), ("crates/alpha", self.tip))
        self.assertEqual(ticket["files"], list(self.sources[:2]))
        self.assertEqual(self.sync_wait_rows(), [])

    def test_refill_holds_for_crate_and_workspace_build_inputs(self) -> None:
        _ = self.rule("read")
        self.sync()
        for path in ("crates/alpha/Cargo.toml", "crates/alpha/build.rs", "Cargo.toml", "Cargo.lock"):
            with self.subTest(path=path):
                _ = self.git("reset", "--hard", self.tip)
                content = (self.repo / path).read_text() if (self.repo / path).exists() else ""
                self.write(path, content + "# incoming build input\n")
                _ = self.commit()
                self.assertEqual(self.run_next(), (3, f"waiting for sync: read crates/alpha: main touched {path}\n", ""))
                self.assertEqual(self.count_tickets(), 0)

    def test_refill_holds_for_a_rename_in_another_crate(self) -> None:
        _ = self.rule("read")
        self.sync()
        _ = self.git("mv", self.sources[2], "crates/beta/src/moved.rs")
        _ = self.commit()
        self.assertEqual(self.run_next(), (3, "waiting for sync: touched set unknown: rename or copy in main range\n", ""))
        self.assertEqual(self.count_tickets(), 0)

    def test_refill_holds_for_a_copy_in_another_crate(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.write("crates/beta/src/copied.rs", (self.repo / self.sources[2]).read_text())
        _ = self.commit()
        self.assertEqual(self.run_next(), (3, "waiting for sync: touched set unknown: rename or copy in main range\n", ""))
        self.assertEqual(self.count_tickets(), 0)

    def test_refill_holds_when_the_synced_tip_is_missing(self) -> None:
        _ = self.rule("read")
        self.sync()
        with mock.patch.object(style_plan, "synced_tip", return_value="0" * 40):
            code, output, errors = self.run_next()
        self.assertEqual(code, 3, errors)
        self.assertIn("waiting for sync: touched set unknown:", output)
        self.assertEqual(self.count_tickets(), 0)

    def test_refill_holds_when_the_synced_tip_and_main_diverge(self) -> None:
        _ = self.rule("read")
        self.sync()
        _ = (self.style / "style.txt").write_text("style tip\n")
        _ = self.git("add", "style.txt", cwd=self.style)
        _ = self.git("commit", "-qm", "Style tip", cwd=self.style)
        self.tip = self.git("rev-parse", "HEAD", cwd=self.style)
        self.sync()
        self.write("README.md", "main tip\n")
        _ = self.commit()
        self.assertEqual(self.run_next(), (
            3, "waiting for sync: touched set unknown: synced tip is not an ancestor of main\n", ""))

    def test_refill_checks_every_main_commit_even_when_the_edit_was_reverted(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.write(self.sources[0], "pub fn temporary() {}\n")
        _ = self.commit()
        self.write(self.sources[0], "pub fn value() {}\n")
        _ = self.commit()
        self.assertEqual(self.run_next(), (
            3, f"waiting for sync: read crates/alpha: main touched {self.sources[0]}\n", ""))

    def test_refill_preserves_a_queued_ticket_when_main_touches_its_file(self) -> None:
        rule = self.rule("read")
        self.sync()
        ledger = self.ledger()
        ledger.ticket_add("names-unit-1", "names-unit", "read", "names", "crates/alpha", [self.sources[0]], [rule])
        self.write(self.sources[0], "pub fn changed_on_main() {}\n")
        _ = self.commit()
        self.assertEqual(self.run_next(), (
            3, f"waiting for sync: read crates/alpha: main touched {self.sources[0]}\n", ""))
        self.assertEqual([ticket["id"] for ticket in ledger.queued_tickets("names-unit")], ["names-unit-1"])
        self.assertEqual(ledger.ticket_open("names-unit"), ())

    def test_refill_holds_the_selected_decision_before_starting_it(self) -> None:
        rule = self.rule("read")
        self.sync()
        issue = self.approved_decision(rule, self.sources[:1], "rename")
        self.write(self.sources[0], "pub fn changed_on_main() {}\n")
        _ = self.commit()
        with mock.patch.object(style_proposals, "proposal_command") as start:
            self.assertEqual(self.run_next(), (
                3, f"waiting for sync: proposal crates/alpha {rule}: main touched {self.sources[0]}\n", ""))
        start.assert_not_called()
        self.assertEqual(self.count_tickets(), 0)
        self.assertEqual(self.ledger().proposal_findings(issue)[0].build_ticket, style_ledger.NoBuildTicket())

    def test_refill_holds_a_rename_when_main_adds_a_reference(self) -> None:
        rule = self.rule("read")
        issue = self.file_rename_decision(rule, (("crates/alpha/src/probe.rs", "Rename probe.rs to frame_log.rs"),))
        path = "crates/alpha/src/gauge.rs"
        self.write(path, "pub fn level() { crate::probe::sample(); }\n")
        _ = self.commit()
        before = self.plan.read_bytes()
        with mock.patch.object(style_proposals, "proposal_command") as start:
            self.assertEqual(self.run_next(), (
                3, f"waiting for sync: proposal crates/alpha {rule}: main touched {path}\n", ""))
        start.assert_not_called()
        self.assertEqual(self.count_tickets(), 0)
        self.assertEqual(self.plan.read_bytes(), before)
        self.assertEqual(self.ledger().proposal_findings(issue)[0].build_ticket, style_ledger.NoBuildTicket())

    def test_refill_preserves_a_queued_rename_when_main_adds_a_reference(self) -> None:
        rule = self.rule("read")
        issue = self.file_rename_decision(rule, (("crates/alpha/src/probe.rs", "Rename probe.rs to frame_log.rs"),))
        ledger = self.ledger()
        finding = ledger.proposal_findings(issue)[0]
        ledger.ticket_add("names-unit-1", "names-unit", "proposal", "names", "crates/alpha", [finding.path], [rule],
                          proposal=str(issue), finding_ids=(finding.finding_id,))
        path = "crates/alpha/src/gauge.rs"
        self.write(path, "pub fn level() { crate::probe::sample(); }\n")
        _ = self.commit()
        with mock.patch.object(style_proposals, "proposal_command") as start:
            self.assertEqual(self.run_next(), (
                3, f"waiting for sync: proposal crates/alpha: main touched {path}\n", ""))
        start.assert_not_called()
        self.assertEqual([ticket["id"] for ticket in ledger.queued_tickets("names-unit")], ["names-unit-1"])
        self.assertEqual(ledger.ticket_open("names-unit"), ())

    def test_refill_holds_when_the_selected_footprint_at_main_cannot_be_read(self) -> None:
        rule = self.rule("read")
        _ = self.file_rename_decision(rule, (("crates/alpha/src/probe.rs", "Rename probe.rs to frame_log.rs"),))
        self.write(self.sources[2], "pub fn unrelated() {}\n")
        main = self.commit()
        original = style_plan.EditedFiles.of

        def footprint(edits: style_plan.EditedFiles, finding: style_ledger.HeldFinding) -> tuple[str, ...]:
            if edits.base == style_plan.UnitBranchTip(main):
                raise style_ledger.CheckCouldNotRun("main footprint unavailable")
            return original(edits, finding)

        with (mock.patch.object(style_plan.EditedFiles, "of", footprint),
              mock.patch.object(style_proposals, "proposal_command") as start):
            self.assertEqual(self.run_next(), (
                3, f"waiting for sync: proposal crates/alpha {rule}: touched set unknown: "
                + "cannot read selected footprint at main: main footprint unavailable\n", ""))
        start.assert_not_called()
        self.assertEqual(self.count_tickets(), 0)

    def test_refill_holds_when_main_touches_a_filename_with_a_carriage_return(self) -> None:
        rule = self.rule("read")
        path = "crates/alpha/src/line\rbreak.rs"
        self.write(path, "pub fn before() {}\n")
        self.commit_to_every_checkout()
        ledger = self.ledger()
        ledger.ticket_add("names-unit-1", "names-unit", "read", "names", "crates/alpha", [path], [rule])
        self.write(path, "pub fn after() {}\n")
        _ = self.commit()
        self.assertEqual(self.run_next(), (
            3, f"waiting for sync: read crates/alpha: main touched {path}\n", ""))
        self.assertEqual([ticket["id"] for ticket in ledger.queued_tickets("names-unit")], ["names-unit-1"])
        self.assertEqual(ledger.ticket_open("names-unit"), ())

    def test_main_changes_refuses_a_path_that_cannot_cross_the_ledger_text_boundary(self) -> None:
        path = os.fsdecode(b"crates/alpha/src/non_utf8_\xff.rs")
        self.write(path, "pub fn new_file() {}\n")
        _ = self.commit()
        self.assertIsInstance(style_plan.main_changes(self.ledger(), "main", self.tip), style_plan.UnknownMainChanges)

    def test_refill_selects_work_despite_an_unpushed_local_branch_point_merge(self) -> None:
        _ = self.rule("read")
        self.sync()
        origin = self.root / "origin.git"
        _ = self.git("init", "-q", "--bare", str(origin))
        _ = self.git("remote", "add", "origin", str(origin))
        _ = self.git("push", "-q", "origin", "main")
        _ = (self.style / "README.md").write_text("Style checkpoint.\n")
        _ = self.git("add", "README.md", cwd=self.style)
        _ = self.git("commit", "-qm", "Style checkpoint", cwd=self.style)
        _ = self.git("merge", "--no-ff", "-qm", "Unpushed integration", "style/main")

        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))

        self.assertEqual(self.count_tickets(), 1)
        self.assertEqual(self.sync_wait_rows(), [])

    def test_refill_selects_work_after_an_unrelated_local_branch_point_merge_is_pushed(self) -> None:
        _ = self.rule("read")
        self.sync()
        origin = self.root / "origin.git"
        _ = self.git("init", "-q", "--bare", str(origin))
        _ = self.git("remote", "add", "origin", str(origin))
        _ = self.git("push", "-q", "origin", "main")
        _ = (self.style / "README.md").write_text("Style checkpoint.\n")
        _ = self.git("add", "README.md", cwd=self.style)
        _ = self.git("commit", "-qm", "Style checkpoint", cwd=self.style)
        _ = self.git("merge", "--no-ff", "-qm", "Integration", "style/main")
        _ = self.git("push", "-q", "origin", "main")
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        self.assertEqual(self.ledger().ticket_open("names-unit")[0]["base"], self.tip)
        self.assertEqual(self.sync_wait_rows(), [])

    def test_refill_selects_work_after_sync_merges_the_branch_point_into_a_distinct_style_tip(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.write(self.sources[0], "pub fn incoming_branch_point_work() {}\n")
        branch_tip = self.commit()
        self.assertEqual(self.run_next(), (
            3, f"waiting for sync: read crates/alpha: main touched {self.sources[0]}\n", ""))
        _ = self.git("merge", "--no-ff", "-qm", "Sync branch point", "main", cwd=self.style)
        self.tip = self.git("rev-parse", "HEAD", cwd=self.style)
        self.assertNotEqual(self.tip, branch_tip)
        self.sync()
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        self.assertEqual(self.count_tickets(), 1)
        self.assertEqual(self.sync_wait_rows(), [])

    def test_assigned_ticket_resumes_unchanged_while_the_branch_point_is_unsynced(self) -> None:
        _ = self.rule("read")
        self.sync()
        self.assertEqual(self.run_next(), (0, "phase written: names-unit-1\n", ""))
        before = self.plan.read_bytes()
        ticket = self.ledger().ticket_open("names-unit")[0]
        self.write("README.md", "Branch point moved during an assigned ticket.\n")
        _ = self.commit()
        with mock.patch.object(berth_board, "read_board", side_effect=AssertionError("board read on resume")):
            self.assertEqual(self.run_next(), (0, "resume names-unit-1\n", ""))
        self.assertEqual(self.plan.read_bytes(), before)
        self.assertEqual(self.ledger().ticket_open("names-unit")[0], ticket)
        self.assertEqual(self.count_tickets(), 1)
        self.assertEqual(self.sync_wait_rows(), [])

if __name__ == "__main__":
    _ = unittest.main()
