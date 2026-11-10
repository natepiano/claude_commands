"""Style planner pipeline growth contracts."""

from __future__ import annotations

from scripts.style.ledger_test_support import DEFAULT_STYLE_ASSIGNMENTS, ticket_owner_id

import json
import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from typing import final
from unittest import mock


from scripts.style import rules, style_ledger

from scripts.style.style_plan_test_support import StylePlanFixture


@final
class StylePlanTests(StylePlanFixture):
    def parallel_tickets(self, capacity: int, count: int = 4) -> tuple[style_ledger.Ledger, str]:
        """Create queued tickets with disjoint paths and the specified unit capacity."""
        self.style_units(tuple(replace(row, open_tickets=capacity) if row.unit == "names" else row
                               for row in DEFAULT_STYLE_ASSIGNMENTS))
        rule = self.rule("read")
        self.sync()
        ledger = self.ledger()
        for index, path in enumerate(self.sources[:count], 1):
            crate = "/".join(path.split("/")[:2])
            ledger.ticket_add(str(index), ticket_owner_id("names"), "read", "names", crate, [path], [rule])
        return ledger, rule

    def publish_ticket(self, ledger: style_ledger.Ledger, rule: str, ticket: str) -> str:
        """Publish and record only the specified ticket's source file."""
        path, = ledger.ticket_get(ticket)["files"]
        _ = self.git("add", path, cwd=self.unit)
        _ = self.git("commit", "-qm", "Publish ticket " + ticket, cwd=self.unit)
        checkpoint = self.git("rev-parse", "HEAD", cwd=self.unit)
        self.record(rule, path, checkpoint, ticket)
        ledger.ticket_checkpoint(ticket, checkpoint)
        return checkpoint

    def test_parallel_capacity_one_two_and_three(self) -> None:
        ledger, _ = self.parallel_tickets(1)
        ledger.ticket_assign("1", "demo-style-names")
        with self.assertRaisesRegex(style_ledger.LedgerError, "already has 1 in state assigned"):
            ledger.ticket_assign("2", "demo-style-names")
        for capacity in (2, 3):
            self.style_units(tuple(replace(row, open_tickets=capacity) if row.unit == "names" else row
                                   for row in DEFAULT_STYLE_ASSIGNMENTS))
            ledger.ticket_assign(str(capacity), "demo-style-names")
            self.assertEqual(len(ledger.ticket_open(ticket_owner_id("names"))), capacity)
            with self.assertRaisesRegex(style_ledger.LedgerError, "already has"):
                ledger.ticket_assign(str(capacity + 1), "demo-style-names")
        self.assertEqual({ticket["state"] for ticket in ledger.ticket_open(ticket_owner_id("names"))}, {"assigned"})
        with closing(sqlite3.connect(ledger.database)) as connection:
            self.assertEqual(connection.execute("SELECT ticket,predecessor FROM ticket_predecessors ORDER BY ticket,predecessor").fetchall(),
                             [("2", "1"), ("3", "1"), ("3", "2")])

    def test_parallel_capacity_race_rechecks_under_the_write_lock(self) -> None:
        ledger, _ = self.parallel_tickets(2, 3)
        ledger.ticket_assign("1", "demo-style-names")
        capacity = ledger._ticket_capacity  # pyright: ignore[reportPrivateUsage]
        raced = False

        def assign_second(unit: str) -> int:
            nonlocal raced
            if not raced:
                raced = True
                self.ledger().ticket_assign("2", "demo-style-names")
            return capacity(unit)

        with mock.patch.object(ledger, "_ticket_capacity", side_effect=assign_second):
            with self.assertRaisesRegex(style_ledger.LedgerError, "already has"):
                ledger.ticket_assign("3", "demo-style-names")
        self.assertEqual(ledger.ticket_get("3")["state"], "queued")
        self.assertEqual({ticket["id"] for ticket in ledger.ticket_open(ticket_owner_id("names"))}, {"1", "2"})

    def test_parallel_lower_capacity_preserves_assigned_tickets_until_they_finish(self) -> None:
        ledger, _ = self.parallel_tickets(3)
        for ticket in ("1", "2", "3"):
            ledger.ticket_assign(ticket, "demo-style-names")
        self.style_units(DEFAULT_STYLE_ASSIGNMENTS)
        for ticket in ("1", "2", "3"):
            ledger.check_ticket_pipeline(ticket)
            self.assertEqual(ledger.ticket_get(ticket)["state"], "assigned")
        for completed in ("3", "2", "1"):
            with self.assertRaisesRegex(style_ledger.LedgerError, "already has"):
                ledger.ticket_assign("4", "demo-style-names")
            ledger.ticket_drop(completed)
        ledger.ticket_assign("4", "demo-style-names")

    def test_parallel_obsolete_peer_does_not_cancel_an_independent_ticket(self) -> None:
        ledger, rule = self.parallel_tickets(2, 1)
        independent = self.rule("independent")
        ledger.ticket_add("2", ticket_owner_id("names"), "read", "names", "crates/alpha", [self.sources[1]], [independent])
        for ticket in ("1", "2"):
            ledger.ticket_assign(ticket, "demo-style-names")
        definition = next(definition for definition in rules.load_rules(self.repo) if definition.rule_id == rule)
        _ = definition.path.write_text(definition.path.read_text() + "Added requirement.\n")
        _ = ledger.withdraw_superseded({definition.rule_id: definition for definition in rules.load_rules(self.repo)})
        self.assertEqual(ledger.ticket_get("1")["state"], "dropped")
        self.assertEqual(ledger.ticket_get("2")["state"], "assigned")
        ledger.check_ticket_pipeline("2")

    def test_parallel_admission_stops_at_two_waiting_checkpoints(self) -> None:
        ledger, rule = self.parallel_tickets(3, 3)
        for ticket in ("1", "2"):
            ledger.ticket_assign(ticket, "demo-style-names")
            _ = ledger.ticket_base(ticket)
            self.record(rule, ledger.ticket_get(ticket)["files"][0], self.tip, ticket)
            ledger.ticket_checkpoint(ticket, self.tip)
        with self.assertRaisesRegex(style_ledger.LedgerError, "already has"):
            ledger.ticket_assign("3", "demo-style-names")
        _ = ledger.ticket_merge("1")
        ledger.ticket_assign("3", "demo-style-names")

    def test_parallel_first_and_third_overlap_refuses_assignment_atomically(self) -> None:
        ledger, _ = self.parallel_tickets(3, 3)
        for ticket in ("1", "2"):
            ledger.ticket_assign(ticket, "demo-style-names")
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE tickets SET files=? WHERE id='3'", (json.dumps([self.sources[0]]),))
        with self.assertRaisesRegex(style_ledger.LedgerError, "overlap: " + self.sources[0]):
            ledger.ticket_assign("3", "demo-style-names")
        self.assertEqual((ledger.ticket_get("3")["state"], ledger.ticket_get("3")["base"]), ("queued", ""))
        with closing(sqlite3.connect(ledger.database)) as connection:
            self.assertEqual(connection.execute("SELECT * FROM ticket_predecessors WHERE ticket='3'").fetchall(), [])

    def test_parallel_first_and_third_overlap_rechecks_every_peer_boundary(self) -> None:
        ledger, rule = self.parallel_tickets(3, 3)
        for ticket in ("1", "2", "3"):
            ledger.ticket_assign(ticket, "demo-style-names")
        self.record(rule, self.sources[1], self.tip, "2")

        def widen_first() -> None:
            with closing(sqlite3.connect(ledger.database)) as connection, connection:
                _ = connection.execute("INSERT INTO ticket_widened_paths VALUES (?,?,?,?,?)",
                                       ("1", self.sources[2], "legacy widen", "director", "now"))

        widen_first()
        for boundary in (lambda: ledger.check_ticket_pipeline("2"), lambda: ledger.set_tip(self.tip),
                         lambda: ledger.ticket_checkpoint("2", self.tip)):
            with self.assertRaisesRegex(style_ledger.LedgerError, "overlap: " + self.sources[2]):
                boundary()
        self.assertEqual(ledger.ticket_get("2")["state"], "assigned")
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("DELETE FROM ticket_widened_paths WHERE ticket='1'")
        ledger.ticket_checkpoint("2", self.tip)
        widen_first()
        with self.assertRaisesRegex(style_ledger.LedgerError, "overlap: " + self.sources[2]):
            _ = ledger.ticket_merge("2")
        self.assertEqual(ledger.ticket_get("2")["state"], "checkpointed")

    def test_parallel_publication_order_controls_merge_and_reconcile_order(self) -> None:
        ledger, rule = self.parallel_tickets(2, 2)
        for ticket in ("1", "2"):
            ledger.ticket_assign(ticket, "demo-style-names")
        _ = (self.unit / self.sources[0]).write_text("pub fn first_ticket() {}\n")
        _ = (self.unit / self.sources[1]).write_text("pub fn second_ticket() {}\n")
        second = self.publish_ticket(ledger, rule, "2")
        first = self.publish_ticket(ledger, rule, "1")
        self.assertEqual(self.git("rev-parse", first + "^", cwd=self.unit), second)
        _ = self.git("update-ref", "refs/heads/style/main", first)
        with self.assertRaisesRegex(style_ledger.LedgerError, "waiting for predecessor 2"):
            _ = ledger.ticket_merge("1")
        changes = ledger.reconcile("main", "style/main")
        self.assertEqual([line for line in changes if line.startswith("ticket ")],
                         ["ticket 2: checkpointed -> merged", "ticket 1: checkpointed -> merged"])

    def test_parallel_dropped_peer_preserves_other_proofs_and_merge_order(self) -> None:
        ledger, rule = self.parallel_tickets(3, 3)
        for ticket in ("1", "2", "3"):
            ledger.ticket_assign(ticket, "demo-style-names")
        ledger.ticket_drop("2")
        ledger.check_ticket_pipeline("1")
        ledger.check_ticket_pipeline("3")
        _ = (self.unit / self.sources[0]).write_text("pub fn first_ticket() {}\n")
        _ = (self.unit / self.sources[2]).write_text("pub fn third_ticket() {}\n")
        _ = self.publish_ticket(ledger, rule, "3")
        first = self.publish_ticket(ledger, rule, "1")
        _ = self.git("update-ref", "refs/heads/style/main", first)
        with self.assertRaisesRegex(style_ledger.LedgerError, "waiting for predecessor 3"):
            _ = ledger.ticket_merge("1")
        _ = ledger.ticket_merge("3")
        _ = ledger.ticket_merge("1")
        self.assertEqual(ledger.ticket_get("2")["state"], "dropped")

    def test_parallel_dropped_published_peer_does_not_block_merge(self) -> None:
        ledger, rule = self.parallel_tickets(2, 2)
        for ticket in ("1", "2"):
            ledger.ticket_assign(ticket, "demo-style-names")
        _ = (self.unit / self.sources[1]).write_text("pub fn second_ticket() {}\n")
        _ = self.publish_ticket(ledger, rule, "2")
        ledger.ticket_drop("2")
        _ = (self.unit / self.sources[0]).write_text("pub fn first_ticket() {}\n")
        first = self.publish_ticket(ledger, rule, "1")
        _ = self.git("update-ref", "refs/heads/style/main", first)
        _ = ledger.ticket_merge("1")

    def waiting_proposal_pair(self, kind: style_ledger.FindingKind = "type") -> tuple[style_ledger.Ledger, str, str]:
        """A waiting proposal and a queued proposal whose approved paths are disjoint."""
        rule = self.rule("read")
        self.sync()
        issue = self.approved_decision(rule, self.sources[:2], kind)
        ledger = self.ledger()
        findings = {finding.path: finding.finding_id for finding in ledger.proposal_findings(issue)}
        for number, path in enumerate(self.sources[:2], 1):
            ledger.ticket_add(f"{number}", ticket_owner_id("names"), "proposal", "names", "crates/alpha", [path], [rule],
                              proposal=str(issue), finding_ids=(findings[path],))
        ledger.ticket_assign("1", "demo-style-names")
        _ = ledger.ticket_base("1")
        _ = (self.unit / self.sources[0]).write_text("pub fn checkpoint_work() {}\n")
        _ = self.git("add", self.sources[0], cwd=self.unit)
        _ = self.git("commit", "-qm", "Waiting proposal", cwd=self.unit)
        checkpoint = self.git("rev-parse", "HEAD", cwd=self.unit)
        ledger.ticket_checkpoint("1", checkpoint)
        return ledger, rule, checkpoint

    def test_pipeline_growth_during_refill_rechecks_before_assignment(self) -> None:
        ledger, _, _ = self.waiting_proposal_pair()
        assign = style_ledger.Ledger.ticket_assign

        def widen_then_assign(current: style_ledger.Ledger, ticket: str, branch: str) -> None:
            self.widen_for_pipeline("1", self.sources[1])
            assign(current, ticket, branch)

        before = self.plan.read_bytes()
        with mock.patch.object(style_ledger.Ledger, "ticket_assign", new=widen_then_assign):
            code, output, error = self.run_next()
        self.assertEqual((code, error), (5, ""), output)
        self.assertIn("overlap: " + self.sources[1], output)
        self.assertEqual(self.plan.read_bytes(), before)
        self.assertEqual([ticket["id"] for ticket in ledger.ticket_open(ticket_owner_id("names"))], ["1"])
        self.assertEqual([ticket["id"] for ticket in ledger.queued_tickets(ticket_owner_id("names"))], ["2"])

    def test_pipeline_growth_widen_on_either_ticket_is_atomic(self) -> None:
        ledger, _, _ = self.waiting_proposal_pair()
        ledger.ticket_assign("2", "demo-style-names")
        for ticket, path in (("1", self.sources[1]), ("2", self.sources[0])):
            with self.subTest(ticket=ticket), self.assertRaisesRegex(style_ledger.LedgerError, "overlap: " + path):
                self.widen_for_pipeline(ticket, path)
            self.assertEqual(next(item for item in ledger.ticket_open(ticket_owner_id("names")) if item["id"] == ticket)["widened"], ())
        self.widen_for_pipeline("2", self.sources[2])
        self.assertEqual(self.run_next(), (0, "resume 2\n", ""))

    def test_pipeline_growth_resume_refuses_legacy_overlap_and_unknown_footprints(self) -> None:
        ledger, _, _ = self.waiting_proposal_pair()
        ledger.ticket_assign("2", "demo-style-names")
        before = self.plan.read_bytes()
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("INSERT INTO ticket_widened_paths VALUES (?,?,?,?,?)",
                                   ("1", self.sources[1], "legacy widen", "showrunner", "now"))
        code, output, error = self.run_next()
        self.assertEqual((code, error), (5, ""))
        self.assertIn("overlap: " + self.sources[1], output)
        self.assertEqual(self.plan.read_bytes(), before)
        with mock.patch.object(style_ledger.Ledger, "pipeline_footprint", side_effect=style_ledger.LedgerError("unreadable")):
            code, output, error = self.run_next()
        self.assertEqual((code, error), (5, ""))
        self.assertIn("unreadable", output)
        self.assertEqual(self.plan.read_bytes(), before)

    def test_pipeline_growth_checkpoint_rechecks_actual_changes(self) -> None:
        ledger, rule, _ = self.waiting_ticket()
        ledger.ticket_add("2", ticket_owner_id("names"), "read", "names", "crates/alpha", [self.sources[1]], [rule])
        ledger.ticket_assign("2", "demo-style-names")
        _ = (self.unit / self.sources[0]).write_text("pub fn overlapping_work() {}\n")
        _ = self.git("add", self.sources[0], cwd=self.unit)
        _ = self.git("commit", "-qm", "Unplanned overlapping edit", cwd=self.unit)
        checkpoint = self.git("rev-parse", "HEAD", cwd=self.unit)
        self.record(rule, self.sources[1], checkpoint, "2")
        with self.assertRaisesRegex(style_ledger.LedgerError, "overlap: " + self.sources[0]):
            ledger.ticket_checkpoint("2", checkpoint)
        self.assertEqual(next(ticket for ticket in ledger.ticket_open(ticket_owner_id("names"))
                              if ticket["id"] == "2")["state"], "assigned")

    def test_merged_predecessor_stays_protected_before_successor_publication(self) -> None:
        ledger, rule, checkpoint = self.waiting_ticket()
        ledger.ticket_add("2", ticket_owner_id("names"), "read", "names", "crates/alpha", [self.sources[1]], [rule])
        ledger.ticket_assign("2", "demo-style-names")
        _ = self.git("update-ref", "refs/heads/style/main", checkpoint)
        _ = ledger.ticket_merge("1")
        _ = (self.unit / self.sources[0]).write_text("pub fn successor_overlaps_merged_work() {}\n")
        _ = self.git("add", self.sources[0], cwd=self.unit)
        _ = self.git("commit", "-qm", "Successor changes the merged predecessor path", cwd=self.unit)
        self.assertEqual(ledger.ticket_get("2")["checkpoint"], "")
        with self.assertRaisesRegex(style_ledger.LedgerError, "overlap: " + self.sources[0]):
            ledger.check_ticket_pipeline("2")

    def test_pipeline_growth_new_reference_at_a_later_tip_is_rechecked(self) -> None:
        ledger, _, _ = self.waiting_proposal_pair("rename")
        ledger.ticket_assign("2", "demo-style-names")
        self.write(self.sources[1], "fn added_reference() { Outcome::Old; }\n")
        later = self.commit()
        previous = ledger.synced_commit()
        with self.assertRaisesRegex(style_ledger.LedgerError, "overlap: " + self.sources[1]):
            ledger.set_tip(later)
        self.assertEqual(ledger.synced_commit(), previous)

    def test_pipeline_growth_follow_rechecks_and_rolls_back_the_moved_finding(self) -> None:
        ledger, _, _ = self.waiting_proposal_pair()
        ledger.ticket_assign("2", "demo-style-names")
        origin = ledger.ticket_origin("1")
        assert isinstance(origin, style_ledger.ApprovedProposal)
        finding = ledger.named_held(origin.finding_ids)[0]
        tip = ledger.synced_commit()
        reading = style_ledger.DeclarationReading(tip, (
            style_ledger.FindingSite(finding, style_ledger.UnchangedSite(self.sources[1], 1,
                style_ledger.FindingAnchor(tip, finding.path, finding.line))),))
        with (mock.patch.object(style_ledger.Ledger, "_declaration_reading", return_value=reading),
              self.assertRaisesRegex(style_ledger.LedgerError, "overlap: " + self.sources[1])):
            _ = ledger.follow_declarations(origin.finding_ids, tip, lambda _: True)
        self.assertEqual(ledger.named_held(origin.finding_ids)[0], finding)



if __name__ == "__main__":
    _ = unittest.main()
