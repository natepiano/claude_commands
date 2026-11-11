"""Owned work retains approval, footprint, and idle checks."""

from __future__ import annotations

import io
import sqlite3
from contextlib import closing
from pathlib import Path
from contextlib import redirect_stdout
from unittest import mock

from scripts.style import style_plan, style_ledger, style_proposals, rules, unit_plans
from scripts.style.ledger_test_support import ticket_owner_id
from scripts.style.style_config import StyleUnitAssignment
from scripts.style.style_plan_test_support import StylePlanFixture


class StylePlanTests(StylePlanFixture):
    def test_idle_diagnostic_keeps_control_characters_on_one_line(self) -> None:
        result = style_plan.WaitingForSync(0, families=(style_plan.FamilyIdleReason("names", "waiting for sync: line\rbreak.rs"),))
        output = io.StringIO()
        with (mock.patch.object(style_plan, "next_phase", return_value=result),
              mock.patch.object(style_ledger, "record_refill"), redirect_stdout(output)):
            code = style_plan.main(["next-phase", "--unit", "names", "--plan", str(self.plan), "--repo", str(self.repo)])
        self.assertEqual((code, output.getvalue()), (3, "no available work: names waiting for sync: line break.rs\nwaiting for sync\n"))


    def owned_proposal_ticket(self) -> tuple[style_ledger.Ledger, Path, style_ledger.TicketRecord]:
        self.style_units((StyleUnitAssignment("names", ("names", "values")),
                          StyleUnitAssignment("types", ("types",)), StyleUnitAssignment("modules", ("modules",))))
        values = self.rule("owned-values", "tier: C\nfamily: values\ninput_scope: file\n")
        self.sync()
        for path in self.sources:
            self.record(values, path, self.tip)
        issue = self.borrowing_decision(values, "names", self.sources[0])
        approval = issue.read_bytes()
        with mock.patch.object(style_proposals, "proposal_command"):
            self.assertEqual(self.run_next()[0], 0)
        ledger = self.ledger()
        ticket, = ledger.ticket_open(ticket_owner_id("names"))
        self.assertEqual((ticket["family"], ticket["proposal"]), ("values", str(issue)))
        self.assertEqual(issue.read_bytes(), approval)
        return ledger, issue, ticket

    def test_owned_proposal_refuses_approval_for_an_unrelated_rule(self) -> None:
        _, issue, ticket = self.owned_proposal_ticket()
        _ = issue.write_text(issue.read_text().replace(f"style_rule: {ticket['rules'][0]}",
                                                     "style_rule: global:rust/unrelated.md"))
        self.assertEqual(self.reference_gate(ticket["id"]),
                         (1, "review: proposal approval or filed findings changed\n"))

    def test_owned_proposal_refuses_approval_for_an_unrelated_crate(self) -> None:
        _, issue, ticket = self.owned_proposal_ticket()
        _ = issue.write_text(issue.read_text().replace("style_crate: crates/alpha", "style_crate: crates/beta"))
        self.assertEqual(self.reference_gate(ticket["id"]),
                         (1, "review: proposal approval or filed findings changed\n"))

    def test_owned_proposal_keeps_its_approval_through_gate_checkpoint_and_merge(self) -> None:
        ledger, issue, ticket = self.owned_proposal_ticket()
        approval = issue.read_bytes()
        self.assertEqual(self.reference_gate(ticket["id"]), (0, "approved proposal\n"))
        # Refuse a finding that moves to another decision.
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET proposal=? WHERE finding_id=?",
                                   (str(self.root / "another-decision.md"), ticket["finding_ids"][0]))
        self.assertEqual(self.reference_gate(ticket["id"]), (1, "review: proposal ticket findings changed\n"))
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET proposal=? WHERE finding_id=?",
                                   (str(issue), ticket["finding_ids"][0]))
        self.write(self.sources[0], "pub fn changed() {}\n")
        self.assertEqual(self.reference_gate(ticket["id"]), (0, "approved proposal\n"))
        checkpoint = self.commit()
        ledger.ticket_checkpoint(ticket["id"], checkpoint)
        self.assertEqual(ledger.ticket_open(ticket_owner_id("names"))[0]["state"], "checkpointed")
        ledger.set_tip(checkpoint, "main")
        _ = ledger.ticket_merge(ticket["id"])
        self.assertEqual(ledger.ticket_open(ticket_owner_id("names")), ())
        self.assertEqual(issue.read_bytes(), approval)

    def test_owned_selection_uses_widened_paths_and_rename_references(self) -> None:
        _ = self.types_unit()
        values = self.rule("values", "tier: C\nfamily: values\ninput_scope: file\n")
        names = self.rule("read")
        self.commit_files({self.sources[2]: "pub fn refer() { Outcome::Old; }\n"})
        foreign = self.borrowing_decision(values, "values", self.sources[0])
        ledger = self.ledger()
        original, = ledger.proposal_findings(foreign)
        rename = ledger.hold(values, self.sources[0], 1, "rename item", "rename",
                             "Outcome::Old", "Outcome::New", original.rule_blob, "18001")
        ledger.file_held(rename.finding_id, foreign)
        ledger.ticket_add("12001", ticket_owner_id("values"), "proposal", "values", "crates/alpha",
                          [self.sources[0]], [values], proposal=str(foreign),
                          finding_ids=(original.finding_id, rename.finding_id))
        ledger.ticket_assign("12001", "style/main")
        self.widen_for_pipeline("12001", self.sources[1])
        for path in self.sources:
            self.record(names, path, self.tip)
        own = self.borrowing_decision(names, "names", self.sources[1])
        finding, = ledger.proposal_findings(own)
        extra = ledger.hold(names, self.sources[2], 1, "candidate reference", "type",
                            "u32", "u64", finding.rule_blob, "18001")
        ledger.file_held(extra.finding_id, own)
        self.finish_judging()
        busy = style_plan.busy_files(self.repo, self.doc, ledger, ticket_owner_id("names"), self.board())
        for path in self.sources[:3]:
            self.assertTrue(busy.contains(path), path)
        definitions = {rule.rule_id: rule for rule in rules.load_rules(self.repo)}
        for selection in (style_plan.FamilySelection.OWN, style_plan.FamilySelection.COUNT):
            with self.subTest(selection=selection), redirect_stdout(io.StringIO()):
                result = style_plan._select_phase(  # pyright: ignore[reportPrivateUsage]
                    ledger, ticket_owner_id("names"), "demo-style-names", self.plan, self.repo, self.doc,
                    self.plan.read_text(), unit_plans.AllCrates(), busy, frozenset(), definitions,
                    style_plan.MainChanges(self.tip), family="names", selection=selection)
                if isinstance(result, style_plan.BuildableFindings):
                    self.assertEqual(result.count, 0)
                else:
                    self.assertNotIsInstance(result, style_plan.PhaseWritten)
        self.assertEqual(ledger.ticket_open(ticket_owner_id("names")), ())
