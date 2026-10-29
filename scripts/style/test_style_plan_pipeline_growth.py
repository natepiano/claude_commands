"""Style planner pipeline growth contracts."""

from __future__ import annotations

import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path
from typing import final
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import style_ledger

from style_plan_test_support import StylePlanFixture


@final
class StylePlanTests(StylePlanFixture):
    def waiting_proposal_pair(self, kind: style_ledger.FindingKind = "type") -> tuple[style_ledger.Ledger, str, str]:
        """A waiting proposal and a queued proposal whose approved paths are disjoint."""
        rule = self.rule("read")
        self.sync()
        issue = self.approved_decision(rule, self.sources[:2], kind)
        ledger = self.ledger()
        findings = {finding.path: finding.finding_id for finding in ledger.proposal_findings(issue)}
        for number, path in enumerate(self.sources[:2], 1):
            ledger.ticket_add(f"names-unit-{number}", "names-unit", "proposal", "names", "crates/alpha", [path], [rule],
                              proposal=str(issue), finding_ids=(findings[path],))
        ledger.ticket_assign("names-unit-1", "demo-style-names")
        _ = ledger.ticket_base("names-unit-1")
        _ = (self.unit / self.sources[0]).write_text("pub fn checkpoint_work() {}\n")
        _ = self.git("add", self.sources[0], cwd=self.unit)
        _ = self.git("commit", "-qm", "Waiting proposal", cwd=self.unit)
        checkpoint = self.git("rev-parse", "HEAD", cwd=self.unit)
        ledger.ticket_checkpoint("names-unit-1", checkpoint)
        return ledger, rule, checkpoint

    def test_pipeline_growth_during_refill_rechecks_before_assignment(self) -> None:
        ledger, _, _ = self.waiting_proposal_pair()
        assign = style_ledger.Ledger.ticket_assign

        def widen_then_assign(current: style_ledger.Ledger, ticket: str, branch: str) -> None:
            self.widen_for_pipeline("names-unit-1", self.sources[1])
            assign(current, ticket, branch)

        before = self.plan.read_bytes()
        with mock.patch.object(style_ledger.Ledger, "ticket_assign", new=widen_then_assign):
            code, output, error = self.run_next()
        self.assertEqual((code, error), (5, ""), output)
        self.assertIn("overlap: " + self.sources[1], output)
        self.assertEqual(self.plan.read_bytes(), before)
        self.assertEqual([ticket["id"] for ticket in ledger.ticket_open("names-unit")], ["names-unit-1"])
        self.assertEqual([ticket["id"] for ticket in ledger.queued_tickets("names-unit")], ["names-unit-2"])

    def test_pipeline_growth_widen_on_either_ticket_is_atomic(self) -> None:
        ledger, _, _ = self.waiting_proposal_pair()
        ledger.ticket_assign("names-unit-2", "demo-style-names")
        for ticket, path in (("names-unit-1", self.sources[1]), ("names-unit-2", self.sources[0])):
            with self.subTest(ticket=ticket), self.assertRaisesRegex(style_ledger.LedgerError, "overlap: " + path):
                self.widen_for_pipeline(ticket, path)
            self.assertEqual(next(item for item in ledger.ticket_open("names-unit") if item["id"] == ticket)["widened"], ())
        self.widen_for_pipeline("names-unit-2", self.sources[2])
        self.assertEqual(self.run_next(), (0, "resume names-unit-2\n", ""))

    def test_pipeline_growth_resume_refuses_legacy_overlap_and_unknown_footprints(self) -> None:
        ledger, _, _ = self.waiting_proposal_pair()
        ledger.ticket_assign("names-unit-2", "demo-style-names")
        before = self.plan.read_bytes()
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("INSERT INTO ticket_widened_paths VALUES (?,?,?,?,?)",
                                   ("names-unit-1", self.sources[1], "legacy widen", "showrunner", "now"))
        code, output, error = self.run_next()
        self.assertEqual((code, error), (5, ""))
        self.assertIn("overlap: " + self.sources[1], output)
        self.assertEqual(self.plan.read_bytes(), before)
        with mock.patch.object(style_ledger.Ledger, "_pipeline_footprint", side_effect=style_ledger.LedgerError("unreadable")):
            code, output, error = self.run_next()
        self.assertEqual((code, error), (5, ""))
        self.assertIn("unreadable", output)
        self.assertEqual(self.plan.read_bytes(), before)

    def test_pipeline_growth_checkpoint_rechecks_actual_changes(self) -> None:
        ledger, rule, _ = self.waiting_ticket()
        ledger.ticket_add("names-unit-2", "names-unit", "read", "names", "crates/alpha", [self.sources[1]], [rule])
        ledger.ticket_assign("names-unit-2", "demo-style-names")
        _ = (self.unit / self.sources[0]).write_text("pub fn overlapping_work() {}\n")
        _ = self.git("add", self.sources[0], cwd=self.unit)
        _ = self.git("commit", "-qm", "Unplanned overlapping edit", cwd=self.unit)
        checkpoint = self.git("rev-parse", "HEAD", cwd=self.unit)
        self.record(rule, self.sources[1], checkpoint, "names-unit-2")
        with self.assertRaisesRegex(style_ledger.LedgerError, "overlap: " + self.sources[0]):
            ledger.ticket_checkpoint("names-unit-2", checkpoint)
        self.assertEqual(next(ticket for ticket in ledger.ticket_open("names-unit")
                              if ticket["id"] == "names-unit-2")["state"], "assigned")

    def test_pipeline_growth_new_reference_at_a_later_tip_is_rechecked(self) -> None:
        ledger, _, _ = self.waiting_proposal_pair("rename")
        ledger.ticket_assign("names-unit-2", "demo-style-names")
        self.write(self.sources[1], "fn added_reference() { Outcome::Old; }\n")
        later = self.commit()
        previous = ledger.synced_commit()
        with self.assertRaisesRegex(style_ledger.LedgerError, "overlap: " + self.sources[1]):
            ledger.set_tip(later)
        self.assertEqual(ledger.synced_commit(), previous)

    def test_pipeline_growth_follow_rechecks_and_rolls_back_the_moved_finding(self) -> None:
        ledger, _, _ = self.waiting_proposal_pair()
        ledger.ticket_assign("names-unit-2", "demo-style-names")
        origin = ledger.ticket_origin("names-unit-1")
        assert isinstance(origin, style_ledger.ApprovedProposal)
        finding = ledger.named_held(origin.finding_ids)[0]
        tip = ledger.synced_commit()
        reading = style_ledger.DeclarationReading(tip, (
            style_ledger.FindingSite(finding, style_ledger.UnchangedSite(self.sources[1], 1)),))
        with (mock.patch.object(style_ledger.Ledger, "_declaration_reading", return_value=reading),
              self.assertRaisesRegex(style_ledger.LedgerError, "overlap: " + self.sources[1])):
            _ = ledger.follow_declarations(origin.finding_ids, tip, lambda _: True)
        self.assertEqual(ledger.named_held(origin.finding_ids)[0], finding)



if __name__ == "__main__":
    _ = unittest.main()
