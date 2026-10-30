"""Declaration following, uncertain identities and built-decision closure."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import final

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scripts.production.issues import frontmatter

import rules
import style_ledger

from style_plan_test_support import StylePlanFixture


@final
class StylePlanTests(StylePlanFixture):
    def test_refill_uses_an_outside_follow_and_names_an_unproved_mapped_declaration(self) -> None:
        site, destination = self.sources[:2]
        source = "fn original() {\n    first();\n    second();\n    third();\n    fourth();\n}\n"
        self.commit_files({site: source})
        issue, (finding,) = self.judged_findings(self.rule("read"), ((site, 4, "third();"),))
        self.write_generated_body(issue)
        self.commit_files({site: "// removed\n", destination: source.replace("original", "renamed")})
        output = self.refill_decisions()
        self.assertNotIn("waived", output)
        row, = self.ledger().proposal_findings(issue)
        self.assertEqual((row.finding_id, row.path, row.line, row.state),
                         (finding.finding_id, destination, 4, style_ledger.Filed(issue)))
        ticket = self.ledger().ticket_open("names-unit")[0]
        self.assertEqual((ticket["files"], ticket["finding_ids"]), ([destination], [finding.finding_id]))
        self.ledger().ticket_drop(ticket["id"])
        failed = source.replace("original", "failed").replace("second();", "different();").replace("third();", "changed();").replace("fourth();", "other();")
        # The old body line is copied to another file: unresolved proof must suppress the text-move waiver.
        self.commit_files({destination: failed, site: "fn unrelated() { third(); }\n", "crates/alpha/src/failed.rs": failed})
        output = self.refill_decisions()
        self.assertIn(f"waiting {issue}:", output)
        self.assertIn("outside rename not proved", output)
        self.assertIn(f"mapped site {destination}:4", output)
        self.assertIn("crates/alpha/src/failed.rs", output)
        self.assertNotIn("waived", output)
        row, = self.ledger().proposal_findings(issue)
        self.assertEqual((row.finding_id, row.path, row.state), (finding.finding_id, destination, style_ledger.Filed(issue)))
        self.assertFalse(any(finding.finding_id in ticket["finding_ids"] for ticket in self.ledger().ticket_open("names-unit")))
        self.assertEqual(frontmatter(issue)["status"], ["open"])

    def test_decision_refill_tickets_a_finding_at_the_file_its_declaration_moved_to(self) -> None:
        issue, finding, output = self.refill_after_declaration_move({"crates/alpha/src/parent.rs": "pub struct Parent {\n    old: u32,\n}\n"})
        self.assertNotIn("waived", output)
        followed, = self.ledger().proposal_findings(issue)
        self.assertEqual((followed.finding_id, followed.path, followed.line, followed.state),
                         (finding.finding_id, "crates/alpha/src/parent.rs", 2, style_ledger.Filed(issue)))
        ticket = self.ledger().ticket_open("names-unit")[0]
        self.assertEqual((ticket["kind"], ticket["files"], ticket["finding_ids"]),
                         ("proposal", ["crates/alpha/src/parent.rs"], [finding.finding_id]))

    def test_decision_refill_waits_on_a_decision_the_declaration_step_skipped_and_does_not_waive_its_finding(self) -> None:
        issue, finding, output = self.refill_after_declaration_move(
            {"crates/alpha/src/parent.rs": "pub struct Parent {\n    old: u32,\n}\n"}, generated_body=False)
        self.assertIn(f"skipped {issue}: missing generated decision body\n", output)
        self.assertIn(f"waiting {issue}: skipped by the declaration step\n", output)
        self.assertNotIn("waived", output)
        self.assertEqual([(held.path, held.line, held.state, held.follow) for held in self.ledger().proposal_findings(issue)],
                         [(finding.path, 2, style_ledger.Filed(issue), style_ledger.NotFollowed())])
        self.assertEqual(frontmatter(issue)["status"], ["open"])

    def test_decision_refill_follows_the_findings_of_every_approved_decision_before_it_tickets_one(self) -> None:
        rule = self.rule("read")
        first, second = self.sources[:2]
        self.commit_files({first: "pub struct Parent {\n    old: u32,\n}\n", second: "pub struct Other {\n    old: u32,\n}\n"})
        issue, (stays,) = self.judged_findings(rule, ((first, 2, "old: u32,"),))
        self.write_generated_body(issue)
        # A second approved decision, after the first in the folder, holds a finding in the second source.
        later = issue.with_name("later.md")
        _ = later.write_text(issue.read_text())
        ledger = self.ledger()
        ledger.ticket_add("modules-unit-2", "modules-unit", "read", "names", "crates/alpha", [second], [rule])
        ledger.ticket_assign("modules-unit-2", "style/main")
        _ = ledger.ticket_base("modules-unit-2")
        ledger.ticket_drop("modules-unit-2")
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
        moves = ledger.hold(rule, second, 2, "old: u32,", "type", "before", "after", blob, "modules-unit-2")
        ledger.file_held(moves.finding_id, later)
        _ = self.git("rm", "-q", second)
        self.commit_files({"crates/alpha/src/moved.rs": "pub struct Other {\n    old: u32,\n}\n"})
        self.assertNotIn("waived", self.refill_decisions())
        self.assertEqual(self.ledger().ticket_open("names-unit")[0]["finding_ids"], [stays.finding_id])
        followed, = self.ledger().proposal_findings(later)
        self.assertEqual((followed.finding_id, followed.path, followed.line), (moves.finding_id, "crates/alpha/src/moved.rs", 2))

    def test_refill_names_every_ambiguous_declaration_destination(self) -> None:
        source = "pub struct Parent {\n    old: u32,\n}\n"
        first, second = "crates/alpha/src/one.rs", "crates/alpha/src/two.rs"
        issue, finding, output = self.refill_after_declaration_move({second: source, first: source})
        self.assertIn(f"waiting {issue}: {finding.finding_id} declared in 2 files: {first}, {second}", output)
        row, = self.ledger().proposal_findings(issue)
        self.assertEqual((row.path, row.state, row.follow), (finding.path, style_ledger.Filed(issue), style_ledger.NotFollowed()))

    def test_decision_refill_leaves_a_finding_declared_in_two_files_out_of_the_ticket(self) -> None:
        parent = "pub struct Parent {\n    old: u32,\n}\n"
        issue, finding, output = self.refill_after_declaration_move({"crates/alpha/src/one.rs": parent, "crates/alpha/src/two.rs": parent})
        self.assertIn(f"waiting {issue}: {finding.finding_id} declared in 2 files: crates/alpha/src/one.rs, crates/alpha/src/two.rs; outside rename not proved: old name still declared in crate; "
                      + "candidates: none; mapped site unresolved: source site deleted\n", output)
        self.assertEqual([(held.path, held.line, held.state, held.follow) for held in self.ledger().proposal_findings(issue)],
                         [(finding.path, 2, style_ledger.Filed(issue), style_ledger.NotFollowed())])
        self.assertNotEqual(self.ledger().ticket_open("names-unit")[0]["kind"], "proposal")

    def test_decision_refill_waits_on_a_finding_whose_declaration_is_gone_from_its_kept_file(self) -> None:
        site = self.sources[0]
        self.commit_files({site: "pub struct Parent {\n    old: u32,\n}\n"})
        issue, (finding,) = self.judged_findings(self.rule("read"), ((site, 2, "old: u32,"),))
        self.commit_files({site: "pub fn other() {}\n"})
        output = self.refill_decisions()
        self.assertIn(f"waiting {issue}: {finding.finding_id} outside rename not proved:", output)
        self.assertEqual([(held.path, held.line, held.state, held.follow) for held in self.ledger().proposal_findings(issue)],
                         [(site, 2, style_ledger.Filed(issue), style_ledger.NotFollowed())])
        self.assertNotEqual(self.ledger().ticket_open("names-unit")[0]["kind"], "proposal")
        self.assertEqual(frontmatter(issue)["status"], ["open"])

    def test_decision_refill_waits_when_a_findings_trait_identity_is_uncertain(self) -> None:
        site = self.sources[0]
        source = "impl Trait for Type {\n    fn method(&self) { 7 }\n}\n"
        self.commit_files({site: source})
        issue, (finding,) = self.judged_findings(self.rule("read"), ((site, 2, "7"),))
        self.write_generated_body(issue)
        self.commit_files({site: source.replace("impl Trait", "impl external::Trait")})
        output = self.refill_decisions()
        self.assertIn(f"waiting {issue}: {finding.finding_id} trait identity uncertain\n", output)
        self.assertEqual(self.ledger().proposal_findings(issue)[0].state, style_ledger.Filed(issue))
        self.assertFalse(any(finding.finding_id in ticket["finding_ids"] for ticket in self.ledger().ticket_open("names-unit")))

if __name__ == "__main__":
    _ = unittest.main()
