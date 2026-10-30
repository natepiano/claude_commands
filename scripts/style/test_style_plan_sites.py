"""Moved sites retain their judgments and follow their current files."""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from typing import final
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import style_proposals

import rules
import style_ledger

from style_plan_test_support import StylePlanFixture


@final
class StylePlanTests(StylePlanFixture):
    def test_a_merge_that_moves_a_site_into_a_judged_file_stales_it_and_waives_the_moved_finding(self) -> None:
        rule = self.rule("read")
        target, source = self.sources[:2]
        # Macro calls name no item, so the declaration step cannot read them and the moved-site waive applies.
        moved_text, kept_text = "moved_away!(1);", "stays_here!(2);"
        self.write(source, f"{moved_text}\n{kept_text}\n")
        self.commit_to_every_checkout()
        self.record(rule, target, self.tip)
        issue, (moved, kept) = self.judged_findings(rule, ((source, 1, moved_text), (source, 2, kept_text)))
        ledger = self.ledger()
        self.assertEqual(next(cell.state for cell in ledger.cells(rule) if cell.path == target), "current")
        self.write(target, f"pub fn value() {{}}\n    {moved_text}\n")
        self.write(source, f"{kept_text}\n")
        self.commit_to_every_checkout()
        self.assertEqual(next(cell.state for cell in ledger.cells(rule) if cell.path == target), "stale")

        with mock.patch.object(style_proposals, "proposal_command"):
            code, output, errors = self.run_next()
        self.assertEqual(code, 0, errors)
        self.assertIn(f"waived 1 findings in {issue}: {source} site moved to {target} in synced commit {self.tip}\n", output)
        states = {finding.finding_id: finding.state for finding in ledger.proposal_findings(issue)}
        self.assertEqual(states, {moved.finding_id: style_ledger.Waived(), kept.finding_id: style_ledger.Filed(issue)})
        ticket = ledger.ticket_open("names-unit")[0]
        self.assertEqual((ticket["kind"], ticket["files"], ticket["finding_ids"]), ("proposal", [source], [kept.finding_id]))
        self.assertTrue(any(selection.kind == "read" and target in selection.files for selection in self.candidates()))

    def test_an_edited_site_whose_old_line_another_changed_file_always_held_stays_filed(self) -> None:
        site, other = self.sources[:2]
        output, state = self.refill_after_site_merge(
            {site: "pub count: usize,\n", other: "pub count: usize,\npub fn value() {}\n"},
            {site: "pub item_count: usize,\n", other: "pub count: usize,\npub fn other() {}\n"})
        self.assertNotIn("site moved", output)
        self.assertIsInstance(state, style_ledger.Filed)

    def test_a_site_text_that_went_to_no_other_file_stays_filed(self) -> None:
        site, other = self.sources[:2]
        output, state = self.refill_after_site_merge(
            {site: "pub fn gone() {}\npub fn value() {}\n"},
            {site: "pub fn value() {}\n", other: "pub fn changed() {}\n"})
        self.assertNotIn("site moved", output)
        self.assertIsInstance(state, style_ledger.Filed)

    def test_a_site_text_that_went_to_two_files_stays_filed(self) -> None:
        site, other = self.sources[:2]
        output, state = self.refill_after_site_merge(
            {site: "pub fn twice() {}\npub fn value() {}\n"},
            {site: "pub fn value() {}\n", other: "pub fn twice() {}\n", "crates/alpha/src/added.rs": "pub fn twice() {}\n"})
        self.assertNotIn("site moved", output)
        self.assertIsInstance(state, style_ledger.Filed)

    def test_a_latin_1_file_among_the_changed_files_still_takes_the_moved_site(self) -> None:
        site, other, notes = *self.sources[:2], "crates/alpha/notes.txt"
        output, state = self.refill_after_site_merge(
            {site: "moved!();\npub fn value() {}\n", notes: "caf\u00e9\nmoved!();\n".encode("latin-1")},
            {site: "pub fn value() {}\n", other: "moved!();\n", notes: "caf\u00e9s\nmoved!();\n".encode("latin-1")})
        self.assertIn(f"{site} site moved to {other} in synced commit {self.tip}\n", output)
        self.assertEqual(state, style_ledger.Waived())

    def test_an_approved_lower_rank_finding_at_a_changed_file_builds_once_the_judgment_of_that_file_holds_it_again(self) -> None:
        issue, finding, output = self.approved_then_changed()
        self.assertIn(f"waiting {issue}: {finding.path} changed since it was judged (1 finding)", output)
        self.assertEqual(self.ledger().proposal_tickets(), ())
        self.rejudge(finding, holds=True)
        _ = self.refill_decisions()
        ticket = self.ledger().ticket_open("names-unit")[0]
        self.assertEqual((ticket["kind"], ticket["finding_ids"]), ("proposal", [finding.finding_id]))

    def test_an_approved_lower_rank_finding_at_a_changed_file_is_waived_when_the_judgment_of_that_file_does_not_hold_it(self) -> None:
        issue, finding, _ = self.approved_then_changed()
        self.rejudge(finding, holds=False)
        output = self.refill_decisions()
        self.assertRegex(output, rf"waived 1 findings in {re.escape(str(issue))}: {re.escape(finding.path)} judged again in judgment \d+ without it")
        row, = self.ledger().proposal_findings(issue)
        self.assertEqual(row.state, style_ledger.Waived())
        self.assertEqual(self.ledger().proposal_tickets(), ())

    def test_a_queued_proposal_for_a_lower_rank_finding_judged_at_an_older_file_is_dropped(self) -> None:
        issue, finding, _ = self.approved_then_changed()
        self.ledger().ticket_add("names-unit-9", "names-unit", "proposal", "names", "crates/alpha", [finding.path],
                                 [finding.rule], proposal=str(issue), finding_ids=(finding.finding_id,))
        output = self.refill_decisions()
        self.assertIn(f"dropped names-unit-9: {finding.finding_id} {finding.path} changed since it was judged", output)
        self.assertEqual([ticket["id"] for ticket in self.ledger().proposal_tickets() if ticket["state"] != "dropped"], [])

    def test_a_followed_finding_is_settled_by_a_later_judgment_of_its_new_file(self) -> None:
        rule = self.rule("read")
        site = self.sources[0]
        self.commit_files({site: "pub struct Parent {\n    old: u32,\n}\n"})
        issue, (finding,) = self.judged_findings(rule, ((site, 2, "old: u32,"),))
        self.record(rule, site, self.tip, "modules-unit-1")
        self.write_generated_body(issue)
        _ = self.git("rm", "-q", site)
        self.commit_files({"crates/alpha/src/moved.rs": "pub struct Parent {\n    old: u32,\n}\n"})
        _ = self.refill_decisions()
        followed, = self.ledger().proposal_findings(issue)
        self.assertEqual((followed.finding_id, followed.path), (finding.finding_id, "crates/alpha/src/moved.rs"))
        definition = next(item for item in rules.load_rules(self.repo) if item.rule_id == rule)
        self.assertEqual(self.ledger().judged_again_without(followed, definition), style_ledger.NotJudgedAgain())
        self.record(rule, followed.path, self.tip, "modules-unit-2")
        self.assertIsInstance(self.ledger().judged_again_without(followed, definition), style_ledger.SupersedingJudgment)

    def test_a_followed_finding_is_not_settled_by_a_later_judgment_of_an_older_new_file(self) -> None:
        rule = self.rule("read")
        site = self.sources[0]
        self.commit_files({site: "pub struct Parent {\n    old: u32,\n}\n"})
        issue, _ = self.judged_findings(rule, ((site, 2, "old: u32,"),))
        self.record(rule, site, self.tip, "modules-unit-1")
        self.write_generated_body(issue)
        _ = self.git("rm", "-q", site)
        self.commit_files({"crates/alpha/src/moved.rs": "pub struct Parent {\n    old: u32,\n}\n"})
        _ = self.refill_decisions()
        followed, = self.ledger().proposal_findings(issue)
        moved = self.tip
        self.commit_files({"crates/alpha/src/moved.rs": "pub struct Parent {\n    old: u32,\n}\n\npub struct Typed;\n"})
        definition = next(item for item in rules.load_rules(self.repo) if item.rule_id == rule)
        self.record(rule, followed.path, moved, "modules-unit-2")
        self.assertEqual(self.ledger().judged_again_without(followed, definition), style_ledger.NotJudgedAgain())
        self.record(rule, followed.path, self.tip, "modules-unit-3")
        self.assertIsInstance(self.ledger().judged_again_without(followed, definition), style_ledger.SupersedingJudgment)

if __name__ == "__main__":
    _ = unittest.main()
